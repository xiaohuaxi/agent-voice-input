from __future__ import annotations

import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _REPO_ROOT / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from agent_voice_input import cli as cli_mod  # noqa: E402
from agent_voice_input.asr_providers import (  # noqa: E402
    AsrResult,
    HttpRequest,
    HttpResponse,
    _is_openai_reasoning_model,
    apply_chat_effort_params,
)
from agent_voice_input.config import (  # noqa: E402
    REFINER_AUTO_PRIORITY,
    Config,
    ConfigError,
    CredentialItem,
    HotkeyConfig,
    ProviderCredentials,
    RefinerConfig,
    _parse_simple_toml,
    has_codex_local_auth,
    load_config,
    parse_env_text,
    parse_toml,
    resolve_codex_home_dir,
    resolve_credentials,
    select_refiner_provider,
)
from agent_voice_input.daemon import VoiceInputDaemon, send_control_command  # noqa: E402
from agent_voice_input.platform_wsl import list_win_shim_sources, read_win_shim_source  # noqa: E402
from agent_voice_input.refiner import (  # noqa: E402
    CODEX_PUBLIC_FALLBACK_MODELS,
    CodexRefiner,
    NoopRefiner,
    OpenAICompatibleRefiner,
    _RefinerAttemptError,
    _is_codex_model_unsupported_error,
    create_refiner,
    resolve_codex_default_model,
)


def _fake_jwt(exp_offset_sec: int = 7200, account_id: str = "acct-unit-9988") -> str:
    import base64

    hdr = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').decode("ascii").rstrip("=")
    body_obj = {
        "exp": int(time.time()) + exp_offset_sec,
        "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
    }
    body = (
        base64.urlsafe_b64encode(json.dumps(body_obj).encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )
    return f"{hdr}.{body}.sigpart"


def _sse_ok_bytes(text: str, *, in_tok: int = 120, cached_tok: int = 80, out_tok: int = 12) -> bytes:
    events = [
        {"type": "response.output_text.delta", "delta": text},
        {
            "type": "response.completed",
            "response": {
                "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
                "usage": {
                    "input_tokens": in_tok,
                    "input_tokens_details": {"cached_tokens": cached_tok},
                    "output_tokens": out_tok,
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        },
    ]
    return "".join(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n" for ev in events).encode("utf-8")


class V0101OnboardingAutoRefinerAndCliTests(unittest.TestCase):
    def test_auto_refiner_priority_zero_subprocess_and_deepseek_provider(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            base_env = {"HOME": str(home), "PATH": ""}

            # Strict zero-subprocess invariant across auto selection and create_refiner
            with (
                mock.patch("subprocess.Popen", side_effect=AssertionError("Popen forbidden in auto probe")),
                mock.patch("subprocess.run", side_effect=AssertionError("subprocess.run forbidden in auto probe")),
            ):
                cfg = Config()
                self.assertEqual(cfg.refiner.provider, "auto")
                self.assertEqual(cfg.refiner.model, "")
                self.assertEqual(cfg.refiner.effort, "")

                # 1. Empty env -> auto degrades to "off" -> NoopRefiner
                creds_empty = resolve_credentials(cfg, base_env)
                self.assertFalse(has_codex_local_auth(base_env))
                self.assertEqual(select_refiner_provider(cfg, creds_empty, base_env), "off")
                ref_noop = create_refiner(cfg, creds_empty, env=base_env)
                self.assertIsInstance(ref_noop, NoopRefiner)
                self.assertEqual(ref_noop.refine("原始口述初稿").text, "原始口述初稿")

                # 2. Walk every entry of REFINER_AUTO_PRIORITY in exact priority order
                env_var_for_prov = {
                    "deepseek": "DEEPSEEK_API_KEY",
                    "dashscope": "DASHSCOPE_API_KEY",
                    "gemini": "GEMINI_API_KEY",
                    "openrouter": "OPENROUTER_API_KEY",
                    "volcengine": "ARK_API_KEY",
                    "openai": "OPENAI_API_KEY",
                    "groq": "GROQ_API_KEY",
                    "siliconflow": "SILICONFLOW_API_KEY",
                }
                self.assertEqual(
                    REFINER_AUTO_PRIORITY,
                    ("deepseek", "dashscope", "gemini", "openrouter", "volcengine", "openai", "groq", "siliconflow"),
                )
                for idx, expected_prov in enumerate(REFINER_AUTO_PRIORITY):
                    env_subset = dict(base_env)
                    for tail_prov in REFINER_AUTO_PRIORITY[idx:]:
                        env_subset[env_var_for_prov[tail_prov]] = f"val-{tail_prov}-key"
                    creds_sub = resolve_credentials(cfg, env_subset)
                    self.assertEqual(
                        select_refiner_provider(cfg, creds_sub, env_subset),
                        expected_prov,
                    )

                # 3. Local Codex auth.json takes top priority over all API keys and preserves effort="low"
                codex_dir = home / ".codex"
                codex_dir.mkdir(mode=0o700)
                # Keyring="auto" in config.toml MUST NOT count as logged-in Codex
                (codex_dir / "config.toml").write_text('cli_auth_credentials_store = "auto"\n', encoding="utf-8")
                self.assertFalse(has_codex_local_auth(base_env))
                # Commented keyring line MUST NOT count either
                (codex_dir / "config.toml").write_text('# cli_auth_credentials_store = "keyring"\n', encoding="utf-8")
                self.assertFalse(has_codex_local_auth(base_env))
                # Explicit keyring store requires executable codex binary
                fake_bin = home / "fake-codex"
                fake_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                fake_bin.chmod(0o755)
                (codex_dir / "config.toml").write_text('cli_auth_credentials_store = "keyring"\n', encoding="utf-8")
                self.assertFalse(has_codex_local_auth(base_env))
                self.assertTrue(has_codex_local_auth({**base_env, "CODEX_BIN": str(fake_bin)}))
                (codex_dir / "config.toml").unlink()

                # Empty auth.json with OPENAI_API_KEY in env (no auth_mode="api_key") must NOT hijack auto to codex
                (codex_dir / "auth.json").write_text("{}", encoding="utf-8")
                env_oai_ds = {**base_env, "OPENAI_API_KEY": "oai_test_aaaa", "DEEPSEEK_API_KEY": "ds_test_bbbb"}
                self.assertFalse(has_codex_local_auth(env_oai_ds))
                self.assertEqual(
                    select_refiner_provider(cfg, resolve_credentials(cfg, env_oai_ds), env_oai_ds),
                    "deepseek",
                )

                # Valid ChatGPT tokens in auth.json -> selects "codex" with effective_effort="low"
                (codex_dir / "auth.json").write_text(
                    json.dumps({"tokens": {"access_token": _fake_jwt()}}),
                    encoding="utf-8",
                )
                self.assertTrue(has_codex_local_auth(env_oai_ds))
                self.assertEqual(
                    select_refiner_provider(cfg, resolve_credentials(cfg, env_oai_ds), env_oai_ds),
                    "codex",
                )
                ref_codex = create_refiner(cfg, resolve_credentials(cfg, env_oai_ds), env=env_oai_ds)
                try:
                    self.assertIsInstance(ref_codex, CodexRefiner)
                    self.assertEqual(cfg.refiner.effective_effort("codex"), "low")
                finally:
                    ref_codex.close()

            # 4. Built-in deepseek refiner wire contract: canonical DEEPSEEK_API_KEY > API_KEY_DEEPSEEK,
            #    default model deepseek-flash, thinking disabled on effort="auto", cache hit token parsing
            ds_requests: list[HttpRequest] = []

            def _ds_transport(req: HttpRequest) -> HttpResponse:
                ds_requests.append(req)
                return HttpResponse(
                    status=200,
                    headers={"content-type": "application/json"},
                    body=json.dumps(
                        {
                            "choices": [{"message": {"content": "修准后的 DeepSeek 文本。"}, "finish_reason": "stop"}],
                            "usage": {
                                "prompt_cache_hit_tokens": 1024,
                                "prompt_cache_miss_tokens": 256,
                                "completion_tokens": 18,
                            },
                        },
                        ensure_ascii=False,
                    ).encode("utf-8"),
                )

            env_ds = {
                "HOME": str(home / "no_codex"),
                "PATH": "",
                "DEEPSEEK_API_KEY": "ds_canon_aaaa1111",
                "API_KEY_DEEPSEEK": "ds_legacy_bbbb2222",
            }
            cfg_ds, merged_ds = load_config(env=env_ds, home_dir=home / "no_codex", load_default_env_files=False)
            ref_ds = create_refiner(cfg_ds, env=merged_ds, transport=_ds_transport)
            self.assertIsInstance(ref_ds, OpenAICompatibleRefiner)
            self.assertEqual(ref_ds.provider_name, "deepseek")
            res_ds = ref_ds.refine(AsrResult(text="修准后的 deep seek 文本", provider="dashscope", model="qwen3-asr-flash", latency_ms=50))
            self.assertEqual(res_ds.text, "修准后的 DeepSeek 文本。")
            self.assertEqual(res_ds.provider, "deepseek")
            self.assertEqual(res_ds.model, "deepseek-flash")
            self.assertEqual(res_ds.input_tokens, 1280)
            self.assertEqual(res_ds.cached_input_tokens, 1024)
            self.assertEqual(len(ds_requests), 1)
            self.assertEqual(ds_requests[0].url, "https://api.deepseek.com/v1/chat/completions")
            self.assertEqual(ds_requests[0].headers.get("Authorization"), "Bearer ds_canon_aaaa1111")
            sent_payload = json.loads(ds_requests[0].body.decode("utf-8"))
            self.assertEqual(sent_payload["model"], "deepseek-flash")
            self.assertEqual(sent_payload["thinking"], {"type": "disabled"})

            # DeepSeek reasoner slug skips thinking={"type":"disabled"} on effort="auto", while effort="medium" enables thinking
            p_reasoner: dict[str, object] = {}
            apply_chat_effort_params(p_reasoner, provider="deepseek", model="deepseek-reasoner", effort="auto")
            self.assertNotIn("thinking", p_reasoner)
            p_med: dict[str, object] = {}
            apply_chat_effort_params(p_med, provider="deepseek", model="deepseek-flash", effort="medium")
            self.assertEqual(p_med["thinking"], {"type": "enabled"})

            # Explicit hint validation on refiner.provider="auto"
            with self.assertRaises(ConfigError):
                load_config(
                    env={"HOME": str(home / "no_codex")},
                    home_dir=home / "no_codex",
                    cli_overrides={"refiner.base_url": "http://127.0.0.1:8001/v1"},
                    load_default_env_files=False,
                )

    def test_codex_public_model_static_and_runtime_single_hop_fallback(self) -> None:
        self.assertTrue(_is_openai_reasoning_model("codex-mini-latest"))
        self.assertTrue(_is_openai_reasoning_model("gpt-5.4-mini"))
        self.assertFalse(_is_openai_reasoning_model("gpt-4o-mini"))

        # Error classifier precision: catches model unsupported/not_found (even with version dots), rejects param/ctx 400s
        self.assertTrue(
            _is_codex_model_unsupported_error(
                _RefinerAttemptError(
                    "HTTP 400: The 'gpt-6-luna' model is not supported when using Codex with a ChatGPT account.",
                    http_status=400,
                )
            )
        )
        self.assertTrue(
            _is_codex_model_unsupported_error(
                _RefinerAttemptError(
                    "HTTP 404: The model 'gpt-6.0-luna' does not exist or you do not have access to it.",
                    http_status=404,
                )
            )
        )
        self.assertTrue(
            _is_codex_model_unsupported_error(
                _RefinerAttemptError("response.failed: model_not_found", http_status=None)
            )
        )
        self.assertFalse(
            _is_codex_model_unsupported_error(
                _RefinerAttemptError(
                    "HTTP 400: Unsupported parameter: 'temperature' is not supported with this model.",
                    http_status=400,
                )
            )
        )
        self.assertFalse(
            _is_codex_model_unsupported_error(
                _RefinerAttemptError(
                    "HTTP 400: Invalid value for model gpt-5.4-mini: reasoning_effort is not supported",
                    http_status=400,
                )
            )
        )
        self.assertFalse(
            _is_codex_model_unsupported_error(
                _RefinerAttemptError("HTTP 400: context_length_exceeded", http_status=400)
            )
        )

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            codex_dir = home / ".codex"
            codex_dir.mkdir(mode=0o700)
            (codex_dir / "auth.json").write_text(
                json.dumps({"tokens": {"access_token": _fake_jwt()}}),
                encoding="utf-8",
            )
            env = {"HOME": str(home), "PATH": ""}

            # 1. Static models_cache.json alignment
            cfg_auto = Config()
            # Absent cache -> internal default gpt-6-luna preserved
            self.assertEqual(resolve_codex_default_model(cfg_auto, env), "gpt-6-luna")

            # Cache containing gpt-6-luna -> internal default preserved
            (codex_dir / "models_cache.json").write_text(
                json.dumps({"models": [{"slug": "gpt-5.4-mini"}, {"slug": "gpt-6-luna"}]}),
                encoding="utf-8",
            )
            self.assertEqual(resolve_codex_default_model(cfg_auto, env), "gpt-6-luna")

            # Public account cache without gpt-6-luna -> picks first matching CODEX_PUBLIC_FALLBACK_MODELS
            (codex_dir / "models_cache.json").write_text(
                json.dumps({"models": [{"slug": "gpt-5-codex"}, {"slug": "gpt-5-codex-mini"}]}),
                encoding="utf-8",
            )
            self.assertEqual(resolve_codex_default_model(cfg_auto, env), "gpt-5-codex-mini")

            # Explicit refiner.model ALWAYS bypasses static fallback
            cfg_explicit, _ = load_config(
                env=env,
                home_dir=home,
                cli_overrides={"refiner.provider": "codex", "refiner.model": "gpt-6-luna"},
                load_default_env_files=False,
            )
            self.assertTrue(cfg_explicit.refiner.has_explicit_model("codex"))
            self.assertEqual(resolve_codex_default_model(cfg_explicit, env), "gpt-6-luna")
            (codex_dir / "models_cache.json").unlink()

            # 2. Runtime single-hop fallback & process-local memory (even with max_retries=0, zero Fast mode!)
            seen_models: list[str] = []
            seen_hints: list[str] = []

            def _codex_tx(req: HttpRequest) -> HttpResponse:
                body = json.loads(req.body.decode("utf-8"))
                self.assertNotIn("service_tier", body)
                self.assertEqual(body.get("reasoning"), {"effort": "low"})
                m = body["model"]
                seen_models.append(m)
                seen_hints.append(req.headers.get("x-codex-routing-hint", ""))
                if m == "gpt-6-luna":
                    return HttpResponse(
                        status=400,
                        headers={"content-type": "application/json"},
                        body=b'{"detail":"The \'gpt-6-luna\' model is not supported when using Codex with a ChatGPT account."}',
                    )
                return HttpResponse(status=200, headers={}, body=_sse_ok_bytes("公开模型回退成功。"))

            cfg_rt = Config(refiner=RefinerConfig(provider="auto", max_retries=0))
            ref_rt = CodexRefiner(cfg_rt, env=env, transport=_codex_tx)
            try:
                r1 = ref_rt.refine("公开模型回退成功")
                self.assertEqual(r1.used_fallback, "")
                self.assertEqual(r1.text, "公开模型回退成功。")
                self.assertEqual(r1.model, CODEX_PUBLIC_FALLBACK_MODELS[0])
                self.assertEqual(seen_models, ["gpt-6-luna", "gpt-5.4-mini"])
                self.assertEqual(seen_hints, ["model=gpt-6-luna", "model=gpt-5.4-mini"])

                # Second turn hits remembered fallback model directly (1 request, zero extra 400!)
                r2 = ref_rt.refine("第二句直接命中记忆模型")
                self.assertEqual(r2.model, "gpt-5.4-mini")
                self.assertEqual(seen_models, ["gpt-6-luna", "gpt-5.4-mini", "gpt-5.4-mini"])
            finally:
                ref_rt.close()

            # 3. Explicit model NEVER falls back silently on 400 model_not_found
            explicit_models: list[str] = []

            def _always_404_tx(req: HttpRequest) -> HttpResponse:
                explicit_models.append(json.loads(req.body.decode("utf-8"))["model"])
                return HttpResponse(
                    status=404,
                    headers={"content-type": "application/json"},
                    body=b'{"error":{"code":"model_not_found","message":"The model \'my-locked-model\' does not exist"}}',
                )

            cfg_locked, _ = load_config(
                env=env,
                home_dir=home,
                cli_overrides={"refiner.provider": "codex", "refiner.model": "my-locked-model"},
                load_default_env_files=False,
            )
            ref_locked = CodexRefiner(cfg_locked, env=env, transport=_always_404_tx)
            try:
                r_locked = ref_locked.refine("保持初稿不换模")
                self.assertEqual(r_locked.used_fallback, "asr_draft")
                self.assertEqual(r_locked.text, "保持初稿不换模")
                self.assertEqual(explicit_models, ["my-locked-model"])
            finally:
                ref_locked.close()

            # 4. Never cascade more than 1 hop when fallback model itself also returns model_not_found
            cascade_models: list[str] = []

            def _both_404_tx(req: HttpRequest) -> HttpResponse:
                cascade_models.append(json.loads(req.body.decode("utf-8"))["model"])
                return HttpResponse(
                    status=404,
                    headers={"content-type": "application/json"},
                    body=b'{"error":{"code":"model_not_found","message":"The model does not exist"}}',
                )

            ref_cap = CodexRefiner(Config(), env=env, transport=_both_404_tx)
            try:
                r_cap = ref_cap.refine("单跳封顶测试")
                self.assertEqual(r_cap.used_fallback, "asr_draft")
                self.assertEqual(cascade_models, ["gpt-6-luna", "gpt-5.4-mini"])
            finally:
                ref_cap.close()

    def test_daemon_status_reports_resolved_refiner_and_logs_auto_off_once(self) -> None:
        class _HeadlessPlat:
            _shim_disabled = False
            last_shim_error = ""

            def start_event_listener(self, _cb: object) -> None:
                pass

            def close(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rt = root / "rt"
            env = {
                "HOME": str(root / "empty_home"),
                "PATH": "",
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
                "DASHSCOPE_API_KEY": "sk-dash-only-asr-stage1",
            }
            # Wait: DASHSCOPE_API_KEY satisfies Stage 2 too! Use DOUBAO_ASR_API_KEY so Stage 1 is ready & Stage 2 is "off"
            env_stage1_only = {
                "HOME": str(root / "empty_home"),
                "PATH": "",
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
                "DOUBAO_ASR_APP_ID": "app-123456",
                "DOUBAO_ASR_API_KEY": "tok-doubao-stage1-only",
            }
            cfg, merged = load_config(env=env_stage1_only, home_dir=root / "empty_home", load_default_env_files=False)
            daemon = VoiceInputDaemon(cfg, env=merged, runtime_dir=rt, platform=_HeadlessPlat())
            stderr_buf = io.StringIO()
            with mock.patch("sys.stderr", stderr_buf):
                thr = threading.Thread(target=daemon.serve_forever, daemon=True)
                thr.start()
                deadline = time.monotonic() + 3.0
                st: dict[str, object] = {}
                while time.monotonic() < deadline:
                    if (rt / "control.sock").exists():
                        try:
                            st = send_control_command(rt, {"cmd": "status"}, timeout_sec=0.5)
                            if st.get("ok"):
                                break
                        except Exception:
                            pass
                    time.sleep(0.03)
                send_control_command(rt, {"cmd": "shutdown"}, timeout_sec=1.0)
                thr.join(timeout=3.0)
            self.assertEqual(st.get("asr_provider"), "doubao")
            self.assertEqual(st.get("refiner_provider"), "off")
            self.assertIn("INFO: refiner=auto found no Stage 2 credentials", stderr_buf.getvalue())
            _ = env

    def test_cli_init_permissions_commented_env_xdg_and_symlink_safe_force(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app_home = root / "avi_home"
            env = {"HOME": str(root / "home"), "AGENT_VOICE_INPUT_HOME": str(app_home)}

            out1 = io.StringIO()
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch.object(cli_mod, "_is_macbook_laptop", return_value=False),
                mock.patch("sys.stdout", out1),
            ):
                rc1 = cli_mod.main(["init"])
            self.assertEqual(rc1, 0, out1.getvalue())

            cfg_dir = app_home
            toml_p = cfg_dir / "config.toml"
            env_p = cfg_dir / ".env"
            gi_p = cfg_dir / ".gitignore"
            self.assertEqual(stat.S_IMODE(cfg_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(toml_p.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(env_p.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(gi_p.stat().st_mode), 0o644)
            self.assertEqual(gi_p.read_text(encoding="utf-8"), "/*\n!/.gitignore\n!/hotwords/\n")

            # Commented .env MUST parse to {} so placeholder strings NEVER hijack auto provider selection!
            env_template_text = env_p.read_text(encoding="utf-8")
            self.assertEqual(parse_env_text(env_template_text), {})
            self.assertIn('# DOUBAO_SPEECH_API_KEY="..."', env_template_text)
            self.assertIn("Legacy dual-header", env_template_text)
            # config.example.env stays aligned: all keys commented out & promotes DOUBAO_SPEECH_API_KEY
            example_env_text = (_REPO_ROOT / "config.example.env").read_text(encoding="utf-8")
            example_env_parsed = parse_env_text(example_env_text)
            self.assertEqual(example_env_parsed, {})
            self.assertIn('# DOUBAO_SPEECH_API_KEY="', example_env_text)
            self.assertIn('# DOUBAO_ASR_APP_ID="', example_env_text)
            self.assertIn('# DOUBAO_ASR_API_KEY="', example_env_text)
            # Both stdlib/3.10 TOML parsers and load_config parse generated config.toml cleanly
            parsed_a = parse_toml(toml_p.read_text(encoding="utf-8"))
            parsed_b = _parse_simple_toml(toml_p.read_text(encoding="utf-8"))
            self.assertEqual(parsed_a, parsed_b)
            loaded_cfg, _ = load_config(env=env)
            self.assertEqual(loaded_cfg.config_path, str(toml_p))
            self.assertEqual(loaded_cfg.asr.provider, "auto")
            self.assertEqual(loaded_cfg.refiner.provider, "auto")

            # Re-running without --force keeps existing user edits untouched (even if syntax-broken)
            toml_p.write_text("broken = [unterminated\n", encoding="utf-8")
            gi_p.write_text("# custom user gitignore\n/*\n!/hotwords/\n", encoding="utf-8")
            out2 = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=True), mock.patch("sys.stdout", out2):
                rc2 = cli_mod.main(["init"])
            self.assertEqual(rc2, 0)
            self.assertIn("Config    kept ", out2.getvalue())
            self.assertIn("Gitignore kept ", out2.getvalue())
            self.assertEqual(toml_p.read_text(encoding="utf-8"), "broken = [unterminated\n")

            # Symlink protection on --force: replaces symlink entry itself, NEVER overwrites victim target,
            # and NEVER overwrites existing .gitignore even under --force!
            victim = root / "victim_secret.txt"
            victim.write_text("DO_NOT_OVERWRITE", encoding="utf-8")
            toml_p.unlink()
            toml_p.symlink_to(victim)
            out3 = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=True), mock.patch("sys.stdout", out3):
                rc3 = cli_mod.main(["init", "--force"])
            self.assertEqual(rc3, 0, out3.getvalue())
            self.assertEqual(victim.read_text(encoding="utf-8"), "DO_NOT_OVERWRITE")
            self.assertFalse(toml_p.is_symlink())
            self.assertEqual(stat.S_IMODE(toml_p.stat().st_mode), 0o600)
            self.assertEqual(gi_p.read_text(encoding="utf-8"), "# custom user gitignore\n/*\n!/hotwords/\n")

            # AGENT_VOICE_INPUT_CONFIG pointing to an external path writes ONLY TOML (never .env or .gitignore)
            custom_toml = root / "custom_dir" / "my_voice.toml"
            env_custom = {"HOME": str(root / "home"), "AGENT_VOICE_INPUT_CONFIG": str(custom_toml)}
            out_custom = io.StringIO()
            with mock.patch.dict(os.environ, env_custom, clear=True), mock.patch("sys.stdout", out_custom):
                self.assertEqual(cli_mod.main(["init"]), 0)
            self.assertTrue(custom_toml.is_file())
            self.assertEqual(stat.S_IMODE(custom_toml.stat().st_mode), 0o600)
            self.assertFalse((custom_toml.parent / ".env").exists())
            self.assertFalse((custom_toml.parent / ".gitignore").exists())
            self.assertIn(".agent-voice-input/.env", out_custom.getvalue())

    def test_doctor_platform_matrix_ping_dedup_and_secret_redaction(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rt = root / "rt"
            home = root / "home"
            cfg_dir = home / ".agent-voice-input"
            cfg_dir.mkdir(parents=True, mode=0o700)
            # Open 0644 .env triggers WARN without failing rc=0
            env_file = cfg_dir / ".env"
            secret_ds = "dash_test_aaaa8888"
            env_file.write_text(f'DASHSCOPE_API_KEY="{secret_ds}"\n', encoding="utf-8")
            env_file.chmod(0o644)

            # 1. macOS missing swiftc/shim -> rc=1 + Fix: xcode-select --install
            env_mac = {
                "HOME": str(home),
                "PATH": "",
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
                "DASHSCOPE_API_KEY": secret_ds,
            }
            cfg_mac, merged_mac = load_config(env=env_mac, home_dir=home)
            probe_no_shim = {
                "avfoundation_ok": True,
                "audio_devices": ["[0] Built-in Mic"],
                "configured_audio_device": ":default",
                "pbcopy_ok": True,
                "tmux_active": False,
                "clt_ok": False,
                "swiftc_ok": False,
                "mac_shim_ok": False,
                "mac_shim_error": "missing CLT",
                "caller_mic_auth": "authorized",
                "caller_has_graphic_session": True,
                "caller_is_remote_session": False,
                "fg_process": "Ghostty",
                "fg_title": "zsh",
                "permissions_hint": "Microphone -> terminal",
            }
            buf = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "darwin"),
                mock.patch("agent_voice_input.platform_macos.probe_macos_host", return_value=probe_no_shim),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/opt/homebrew/bin/ffmpeg"),
                mock.patch("sys.stdout", buf),
            ):
                self.assertEqual(cli_mod._cmd_doctor(cfg_mac, merged_mac), 1)
            self.assertIn("Fix: xcode-select --install", buf.getvalue())
            self.assertIn("[env-perm]  WARN", buf.getvalue())
            self.assertNotIn(secret_ds, buf.getvalue())

            # 2. macOS local GUI mic denied -> rc=1; ax_trusted=False + non-modifier hotkey -> WARN (rc=0 when mic authorized)
            probe_mic_denied = {
                **probe_no_shim,
                "clt_ok": True,
                "swiftc_ok": True,
                "swiftc_bin": "/usr/bin/swiftc",
                "mac_shim_ok": True,
                "mac_shim_bin": "/tmp/mac_shim",
                "caller_mic_auth": "denied",
                "ax_trusted": False,
                "listen_event_access": False,
            }
            cfg_f13 = Config()
            cfg_f13.hotkey.key = "F13"
            buf_den = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "darwin"),
                mock.patch("agent_voice_input.platform_macos.probe_macos_host", return_value=probe_mic_denied),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/opt/homebrew/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_den),
            ):
                self.assertEqual(cli_mod._cmd_doctor(cfg_f13, merged_mac), 1)
            out_den = buf_den.getvalue()
            self.assertIn("Privacy_Microphone", out_den)
            self.assertIn("[macos-ax]  WARN", out_den)
            self.assertIn("[macos-im]  WARN", out_den)

            probe_ax_warn_ok = {**probe_mic_denied, "caller_mic_auth": "authorized"}
            buf_ax_ok = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "darwin"),
                mock.patch("agent_voice_input.platform_macos.probe_macos_host", return_value=probe_ax_warn_ok),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/opt/homebrew/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_ax_ok),
            ):
                self.assertEqual(cli_mod._cmd_doctor(cfg_f13, merged_mac), 0, buf_ax_ok.getvalue())

            # 3. WSL2: wasapi_ok=True keeps rc=0 even when pulse_rdp_ok=False & winmm_devs=0; all False -> rc=1
            buf_wsl_ok = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=True),
                mock.patch(
                    "agent_voice_input.platform_wsl.probe_wsl_host",
                    return_value={"powershell_ok": True, "ps_version": "5.1", "wasapi_ok": True, "pulse_rdp_ok": False, "winmm_devs": 0},
                ),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_wsl_ok),
            ):
                self.assertEqual(cli_mod._cmd_doctor(cfg_mac, merged_mac), 0, buf_wsl_ok.getvalue())
            self.assertIn("WASAPI=OK", buf_wsl_ok.getvalue())

            buf_wsl_fail = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=True),
                mock.patch(
                    "agent_voice_input.platform_wsl.probe_wsl_host",
                    return_value={"powershell_ok": True, "ps_version": "5.1", "wasapi_ok": False, "pulse_rdp_ok": False, "winmm_devs": 0},
                ),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_wsl_fail),
            ):
                self.assertEqual(cli_mod._cmd_doctor(cfg_mac, merged_mac), 1)
            self.assertIn("ms-settings:privacy-microphone", buf_wsl_fail.getvalue())

            # 4. Native Linux + auto->off (Doubao ASR ready, no Stage 2 credentials) -> rc=0 + doubao 1.0 ping OK
            env_doubao = {
                "HOME": str(root / "no_cfg_home"),
                "PATH": "",
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
                "DOUBAO_ASR_APP_ID": "db_app_9999",
                "DOUBAO_ASR_API_KEY": "db_test_aaaa7777",
            }
            db_ping_reqs: list[HttpRequest] = []

            def _doubao_ping_tx(req: HttpRequest) -> HttpResponse:
                db_ping_reqs.append(req)
                return HttpResponse(
                    status=200,
                    headers={"X-Api-Status-Code": "20000003", "X-Api-Message": "silence"},
                    body=b"{}",
                )

            buf_db = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_db),
            ):
                self.assertEqual(
                    cli_mod._cmd_doctor(Config(), env_doubao, ping=True, transport=_doubao_ping_tx),
                    0,
                    buf_db.getvalue(),
                )
            out_db = buf_db.getvalue()
            self.assertIn("[platform]  INFO native Linux", out_db)
            self.assertIn("[refiner]   WARN auto -> off", out_db)
            self.assertIn("asr(doubao): OK", out_db)
            self.assertIn("volc.bigasr.auc_turbo/flash", out_db)
            self.assertEqual(len(db_ping_reqs), 1)
            self.assertTrue(db_ping_reqs[0].url.endswith("/recognize/flash"))
            self.assertEqual(db_ping_reqs[0].headers.get("X-Api-App-Key"), "db_app_9999")
            self.assertEqual(db_ping_reqs[0].headers.get("X-Api-Access-Key"), "db_test_aaaa7777")
            self.assertNotIn("db_test_aaaa7777", out_db)

            # 4b. DOUBAO_SPEECH_API_KEY with lingering legacy DOUBAO_ASR_APP_ID -> doctor pings single-header only
            env_db_speech = {
                "HOME": str(root / "no_cfg_home"),
                "PATH": "",
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
                "DOUBAO_SPEECH_API_KEY": "db_speech_key_7777",
                "DOUBAO_ASR_APP_ID": "db_stale_app_9999",
            }
            db_speech_reqs: list[HttpRequest] = []
            buf_db_speech = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_db_speech),
            ):
                self.assertEqual(
                    cli_mod._cmd_doctor(
                        Config(),
                        env_db_speech,
                        ping=True,
                        transport=lambda req: (
                            db_speech_reqs.append(req),
                            HttpResponse(
                                status=200,
                                headers={"X-Api-Status-Code": "20000003", "X-Api-Message": "silence"},
                                body=b"{}",
                            ),
                        )[1],
                    ),
                    0,
                    buf_db_speech.getvalue(),
                )
            out_db_speech = buf_db_speech.getvalue()
            self.assertIn("doubao=OK (via DOUBAO_SPEECH_API_KEY)", out_db_speech)
            self.assertIn("legacy app_id via DOUBAO_ASR_APP_ID ignored", out_db_speech)
            self.assertEqual(len(db_speech_reqs), 1)
            self.assertEqual(db_speech_reqs[0].headers.get("X-Api-Key"), "db_speech_key_7777")
            self.assertNotIn("X-Api-App-Key", db_speech_reqs[0].headers)
            self.assertNotIn("X-Api-Access-Key", db_speech_reqs[0].headers)
            self.assertNotIn("db_speech_key_7777", out_db_speech)

            # 5. `--ping` shared endpoint deduplication (DashScope Stage 1 + auto->dashscope Stage 2 = 1 HTTP GET) & 401 redaction
            ping_reqs: list[HttpRequest] = []

            def _ping_200_tx(req: HttpRequest) -> HttpResponse:
                ping_reqs.append(req)
                return HttpResponse(status=200, headers={}, body=b'{"data":[{"id":"qwen3.8-flash"}]}')

            buf_json_ok = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_json_ok),
            ):
                rc_ping_ok = cli_mod._cmd_doctor(
                    cfg_mac, merged_mac, ping=True, as_json=True, transport=_ping_200_tx
                )
            self.assertEqual(rc_ping_ok, 0)
            self.assertEqual(len(ping_reqs), 1)
            self.assertEqual(ping_reqs[0].url, "https://dashscope.aliyuncs.com/compatible-mode/v1/models")
            parsed_json = json.loads(buf_json_ok.getvalue())
            self.assertTrue(parsed_json["ok"])
            self.assertEqual(parsed_json["refiner"]["configured_provider"], "auto")
            self.assertEqual(parsed_json["refiner"]["resolved_provider"], "dashscope")
            self.assertEqual(parsed_json["ping"]["asr"]["status"], "OK")
            self.assertEqual(parsed_json["ping"]["refiner"]["status"], "OK")
            self.assertNotIn(secret_ds, buf_json_ok.getvalue())

            # OpenRouter `/api/v1/key` verification & HTTP 401 failure with full secret redaction
            or_secret = "or_test_aaaa4011"
            env_or = {
                "HOME": str(root / "no_cfg_home"),
                "PATH": "",
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
                "OPENROUTER_API_KEY": or_secret,
            }
            or_urls: list[str] = []

            def _ping_401_tx(req: HttpRequest) -> HttpResponse:
                or_urls.append(req.url)
                return HttpResponse(
                    status=401,
                    headers={},
                    body=f'{{"error":"invalid token {or_secret}"}}'.encode("utf-8"),
                )

            buf_401 = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_401),
            ):
                self.assertEqual(cli_mod._cmd_doctor(Config(), env_or, ping=True, transport=_ping_401_tx), 1)
            self.assertEqual(or_urls, ["https://openrouter.ai/api/v1/key"])
            self.assertIn("HTTP 401", buf_401.getvalue())
            self.assertNotIn(or_secret, buf_401.getvalue())

    def test_cold_review_regressions_init_doctor_codex_and_credentials(self) -> None:
        # P2-7: Positional ProviderCredentials backward compatibility (deepseek appended at end)
        pos_creds = ProviderCredentials(
            CredentialItem("ds-key", "DASHSCOPE_API_KEY"),
            CredentialItem("vc-key", "VOLCENGINE_API_KEY"),
        )
        self.assertEqual(pos_creds.dashscope.value, "ds-key")
        self.assertEqual(pos_creds.volcengine.value, "vc-key")
        self.assertIsNone(pos_creds.deepseek)

        # P1-3: Hermetic env with tilde CODEX_HOME / CODEX_BIN (including ~username) never leaks host HOME/pwd
        self.assertIsNone(resolve_codex_home_dir({"CODEX_HOME": "~/.codex"}))
        self.assertIsNone(resolve_codex_home_dir({"HOME": "/tmp/iso", "CODEX_HOME": "~root/.codex"}))
        self.assertIsNone(resolve_codex_home_dir({"HOME": "~root"}))
        self.assertFalse(has_codex_local_auth({"CODEX_HOME": "~/.codex"}))
        self.assertFalse(has_codex_local_auth({"HOME": "/tmp/iso", "CODEX_HOME": "~root/.codex"}))

        # P2-5: OpenRouter full /chat/completions base_url strips suffix before appending /key;
        # DeepSeek /tools/asr base_url strips suffix and pings /models with Bearer auth.
        or_url, or_hdr = cli_mod._build_ping_http_spec(
            "openrouter", "https://openrouter.ai/api/v1/chat/completions", "sk-or-test"
        )
        self.assertEqual(or_url, "https://openrouter.ai/api/v1/key")
        self.assertEqual(or_hdr["Authorization"], "Bearer sk-or-test")
        ds_url_full, ds_hdr_full = cli_mod._build_ping_http_spec(
            "deepseek", "https://api.deepseek.com/v1/tools/asr", "sk-ds-ping-test"
        )
        self.assertEqual(ds_url_full, "https://api.deepseek.com/v1/models")
        self.assertEqual(ds_hdr_full["Authorization"], "Bearer sk-ds-ping-test")
        for raw_ds_base, want_ds_ping in (
            ("", "https://api.deepseek.com/v1/models"),
            ("https://api.deepseek.com", "https://api.deepseek.com/v1/models"),
            ("https://api.deepseek.com/", "https://api.deepseek.com/v1/models"),
            ("https://proxy.example/v1/tools/asr?tenant=demo", "https://proxy.example/v1/models?tenant=demo"),
            ("https://proxy.example/custom/v1?tenant=demo", "https://proxy.example/custom/v1/models?tenant=demo"),
        ):
            got_url, _ = cli_mod._build_ping_http_spec("deepseek", raw_ds_base, "sk-ds-ping-test")
            self.assertEqual(got_url, want_ds_ping, msg=f"base_url={raw_ds_base!r}")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rt = root / "rt"
            home = root / "home"
            home.mkdir(mode=0o700)

            # P1-1 & P2-1: `init --config <shared_0755>/voice.toml` preserves 0755 dir mode;
            # `load_config(config_path=...)` reads credentials solely from `<app_home>/.env` (never external dir)!
            shared_dir = root / "shared_proj"
            shared_dir.mkdir(mode=0o755)
            os.chmod(shared_dir, 0o755)
            custom_cfg_path = shared_dir / "voice.toml"
            env_clean = {"HOME": str(home), "PATH": "", "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)}
            out_init_cfg = io.StringIO()
            with mock.patch.dict(os.environ, env_clean, clear=True), mock.patch("sys.stdout", out_init_cfg):
                self.assertEqual(cli_mod.main(["init", "--config", str(custom_cfg_path)]), 0)
            self.assertEqual(stat.S_IMODE(shared_dir.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(custom_cfg_path.stat().st_mode), 0o600)
            self.assertIn("--config", out_init_cfg.getvalue())

            # External sibling .env is ignored; credentials come from <app_home>/.env
            (shared_dir / ".env").write_text('DEEPSEEK_API_KEY="external_ignored_key"\n', encoding="utf-8")
            cfg_dir_default = home / ".agent-voice-input"
            cfg_dir_default.mkdir(parents=True, mode=0o700)
            (cfg_dir_default / ".env").write_text('DEEPSEEK_API_KEY="ds_custom_aaaa9999"\n', encoding="utf-8")
            loaded_cfg, loaded_env = load_config(config_path=custom_cfg_path, env=env_clean, home_dir=home)
            self.assertEqual(loaded_env.get("DEEPSEEK_API_KEY"), "ds_custom_aaaa9999")
            self.assertEqual(
                select_refiner_provider(loaded_cfg, resolve_credentials(loaded_cfg, loaded_env), loaded_env),
                "deepseek",
            )

            # P1-2a: `init --force --config bad.json` refuses to write TOML into a .json path
            with self.assertRaises(ConfigError):
                cli_mod._cmd_init(shared_dir / "bad.json", force=True, env=env_clean, home_dir=home)

            # P1-2b: Default `init` without --force keeps existing <app_home>/config.toml untouched
            active_toml = cfg_dir_default / "config.toml"
            active_toml.write_text('[refiner]\nprovider = "off"\n', encoding="utf-8")
            out_shadow = io.StringIO()
            with mock.patch("sys.stdout", out_shadow):
                self.assertEqual(cli_mod._cmd_init(env=env_clean, home_dir=home), 0)
            self.assertIn("kept", out_shadow.getvalue())
            cfg_after_init, _ = load_config(env=env_clean, home_dir=home)
            self.assertEqual(cfg_after_init.refiner.provider, "off")

            # P2-3 & P1-4: Corrupt UTF-8 in ~/.codex/config.toml never crashes auto-selection;
            # offline `doctor` on Codex keyring / near-expiry JWT spawns ZERO subprocesses!
            codex_dir = home / ".codex"
            codex_dir.mkdir(mode=0o700)
            (codex_dir / "config.toml").write_bytes(b"\xff\xfeinvalid-utf8")
            env_ds_only = {**env_clean, "DEEPSEEK_API_KEY": "ds-valid-key"}
            self.assertFalse(has_codex_local_auth(env_ds_only))
            self.assertEqual(
                select_refiner_provider(Config(), resolve_credentials(Config(), env_ds_only), env_ds_only),
                "deepseek",
            )

            codex_spawn_marker = root / "codex_was_spawned"
            fake_codex = root / "fake-codex"
            fake_codex.write_text(f"#!/bin/sh\necho 1 > {codex_spawn_marker}\nexit 1\n", encoding="utf-8")
            fake_codex.chmod(0o755)
            (codex_dir / "config.toml").write_text('cli_auth_credentials_store = "keyring"\n', encoding="utf-8")
            env_keyring = {
                **env_clean,
                "CODEX_BIN": str(fake_codex),
                "DOUBAO_ASR_API_KEY": "db_asr_aaaa1111",
            }
            buf_doc_kr = io.StringIO()
            with (
                mock.patch.object(
                    CodexRefiner,
                    "_call_codex_get_auth_status",
                    side_effect=AssertionError("offline doctor must not spawn codex auth RPC!"),
                ),
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_doc_kr),
            ):
                self.assertEqual(cli_mod._cmd_doctor(Config(), env_keyring, ping=False), 0, buf_doc_kr.getvalue())
            self.assertFalse(codex_spawn_marker.exists())
            self.assertIn("[refiner]   WARN auto -> codex", buf_doc_kr.getvalue())
            self.assertIn("auth=keyring", buf_doc_kr.getvalue())

            # Explicit provider="codex" with no auth.json and no config.toml (macOS default Keyring)
            # emits WARN offline (rc=0) AND still runs live verification when `--ping` is passed!
            (codex_dir / "config.toml").unlink()
            cfg_explicit_codex = Config(refiner=RefinerConfig(provider="codex"))
            buf_doc_cli_kr = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_doc_cli_kr),
            ):
                self.assertEqual(cli_mod._cmd_doctor(cfg_explicit_codex, env_keyring, ping=False), 0)
            self.assertIn("[refiner]   WARN codex", buf_doc_cli_kr.getvalue())
            self.assertFalse(codex_spawn_marker.exists())

            # Codex `--ping` shares a single 5.0s deadline across auth refresh + HTTP probe
            seen_ping_timeouts: list[float] = []

            def _codex_ping_tx(req: HttpRequest) -> HttpResponse:
                seen_ping_timeouts.append(req.timeout_sec)
                return HttpResponse(status=200, headers={}, body=b'{"data":[]}')

            with mock.patch.object(
                CodexRefiner,
                "_resolve_codex_credentials",
                side_effect=lambda **_kw: (
                    time.sleep(0.05),
                    mock.Mock(mode="api_key", token="sk-test-ping-budget", account_id="", exp=None),
                )[1],
            ):
                st_cp, _, _ = cli_mod._probe_stage_ping(
                    stage="refiner",
                    provider="codex",
                    cfg=cfg_explicit_codex,
                    creds=resolve_credentials(cfg_explicit_codex, env_keyring),
                    env=env_keyring,
                    base_url="https://api.openai.com/v1",
                    transport=_codex_ping_tx,
                    cache={},
                )
            self.assertEqual(st_cp, "OK")
            self.assertEqual(len(seen_ping_timeouts), 1)
            self.assertLess(seen_ping_timeouts[0], 4.98)

            # P2-4 & P2-6: Symlinked .env -> 0644 target emits [env-perm] WARN;
            # `doctor --json` with secret="true" emits valid JSON with boolean `"ok": true` preserved!
            real_open_env = root / "real_open.env"
            real_open_env.write_text('DASHSCOPE_API_KEY="true"\n', encoding="utf-8")
            real_open_env.chmod(0o644)
            default_env_link = cfg_dir_default / ".env"
            default_env_link.unlink(missing_ok=True)
            default_env_link.symlink_to(real_open_env)

            env_bool_sec = {**env_clean, "DASHSCOPE_API_KEY": "true"}
            buf_json_bool = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_json_bool),
            ):
                rc_jb = cli_mod._cmd_doctor(Config(), env_bool_sec, ping=False, as_json=True)
            self.assertEqual(rc_jb, 0)
            jb_data = json.loads(buf_json_bool.getvalue())
            self.assertIs(jb_data["ok"], True)
            self.assertTrue(any("[env-perm]  WARN" in ln for ln in jb_data["lines"]))

            # P2-2: HTTP 404 & SSE `response.failed` carrying `code="model_not_found"` with generic message="Access denied."
            (codex_dir / "config.toml").unlink(missing_ok=True)
            (codex_dir / "auth.json").write_text(
                json.dumps({"tokens": {"access_token": _fake_jwt()}}),
                encoding="utf-8",
            )
            sse_models: list[str] = []

            def _sse_code_only_tx(req: HttpRequest) -> HttpResponse:
                m = json.loads(req.body.decode("utf-8"))["model"]
                sse_models.append(m)
                if m == "gpt-6-luna":
                    err_frame = (
                        'event: response.failed\n'
                        'data: {"type":"response.failed","response":{"error":{"code":"model_not_found","message":"Access denied."}}}\n\n'
                    ).encode("utf-8")
                    return HttpResponse(status=200, headers={}, body=err_frame)
                return HttpResponse(status=200, headers={}, body=_sse_ok_bytes("错误码触发回退成功。"))

            ref_sse_fb = CodexRefiner(Config(), env=env_clean, transport=_sse_code_only_tx)
            try:
                r_sse = ref_sse_fb.refine("测试错误码回退")
                self.assertEqual(r_sse.text, "错误码触发回退成功。")
                self.assertEqual(r_sse.model, "gpt-5.4-mini")
                self.assertEqual(sse_models, ["gpt-6-luna", "gpt-5.4-mini"])
            finally:
                ref_sse_fb.close()

    def test_v100_doctor_agy_hooks_init_templates_and_start_readiness_hints(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            rt = root / "rt"
            home.mkdir(mode=0o700)
            rt.mkdir(mode=0o700)
            env_base = {"HOME": str(home), "PATH": "", "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)}

            # 1. _inspect_hook_summary detects ~/.gemini/config/hooks.json (preferred), ~/.gemini/hooks.json,
            #    and ~/.gemini/settings.json fallback, deduplicates across all 3 paths, and follows symlinked configs.
            self.assertEqual(
                cli_mod._inspect_hook_summary(env_base, rt),
                "hooks=none configured (optional), active_registry_entries=0",
            )
            gemini_dir = home / ".gemini"
            gemini_cfg_dir = gemini_dir / "config"
            gemini_cfg_dir.mkdir(parents=True, mode=0o700)
            cfg_hooks_json = gemini_cfg_dir / "hooks.json"
            hooks_json = gemini_dir / "hooks.json"
            settings_json = gemini_dir / "settings.json"
            agy_hook_payload = (
                '{"voice-session-title":{"SessionStart":[{"command":"agent-voice-input session-hook --cli agy"}]}}\n'
            )
            # 1a. ~/.gemini/config/hooks.json alone hits gemini/agy
            cfg_hooks_json.write_text(agy_hook_payload, encoding="utf-8")
            self.assertEqual(
                cli_mod._inspect_hook_summary(env_base, rt),
                "hooks=gemini/agy, active_registry_entries=0",
            )
            # 1b. Multi-path dedup when all 3 Gemini/AGY hook paths coexist
            hooks_json.write_text(agy_hook_payload, encoding="utf-8")
            settings_json.write_text(
                '{"hooks":{"SessionStart":[{"command":"agent-voice-input session-hook --cli gemini"}]}}\n',
                encoding="utf-8",
            )
            all_three_summary = cli_mod._inspect_hook_summary(env_base, rt)
            self.assertEqual(all_three_summary, "hooks=gemini/agy, active_registry_entries=0")
            self.assertEqual(all_three_summary.count("gemini/agy"), 1)

            cfg_hooks_json.unlink()
            both_summary = cli_mod._inspect_hook_summary(env_base, rt)
            self.assertEqual(both_summary, "hooks=gemini/agy, active_registry_entries=0")
            self.assertEqual(both_summary.count("gemini/agy"), 1)

            hooks_json.unlink()
            self.assertEqual(
                cli_mod._inspect_hook_summary(env_base, rt),
                "hooks=gemini/agy, active_registry_entries=0",
            )
            settings_json.unlink()

            # 1c. Dangling symlink alone MUST NOT match; symlinked dotfiles config (e.g. ~/.harness / Stow / chezmoi) hits cleanly
            dotfiles_dir = root / "dotfiles"
            dotfiles_dir.mkdir(mode=0o700)
            cfg_hooks_json.symlink_to(dotfiles_dir / "missing_dangling.json")
            hooks_json.symlink_to(dotfiles_dir / "missing_dangling_legacy.json")
            self.assertEqual(
                cli_mod._inspect_hook_summary(env_base, rt),
                "hooks=none configured (optional), active_registry_entries=0",
            )
            cfg_hooks_json.unlink()
            hooks_json.unlink()

            real_agy_hooks = dotfiles_dir / "agy_hooks.json"
            real_agy_hooks.write_text(agy_hook_payload, encoding="utf-8")
            cfg_hooks_json.symlink_to(real_agy_hooks)
            self.assertEqual(
                cli_mod._inspect_hook_summary(env_base, rt),
                "hooks=gemini/agy, active_registry_entries=0",
            )

            claude_dir = home / ".claude"
            claude_dir.mkdir(mode=0o700)
            real_claude_settings = dotfiles_dir / "claude_settings.json"
            real_claude_settings.write_text(
                '{"hooks":{"SessionStart":[{"hooks":[{"command":"agent-voice-input session-hook --cli claude"}]}]}}\n',
                encoding="utf-8",
            )
            (claude_dir / "settings.json").symlink_to(real_claude_settings)
            self.assertEqual(
                cli_mod._inspect_hook_summary(env_base, rt),
                "hooks=claude, gemini/agy, active_registry_entries=0",
            )

            # 2. Init template two-stage split, MacBook hotkey note, and next-step guidance
            out_init = io.StringIO()
            with mock.patch.object(cli_mod, "_is_macbook_laptop", return_value=False), mock.patch("sys.stdout", out_init):
                self.assertEqual(cli_mod._cmd_init(env=env_base, home_dir=home), 0)
            init_txt = out_init.getvalue()
            self.assertIn("Stage 1", init_txt)
            for key_name in (
                "DASHSCOPE_API_KEY",
                "DEEPSEEK_API_KEY",
                "OPENAI_API_KEY",
                "GEMINI_API_KEY",
            ):
                self.assertIn(key_name, init_txt)

            gen_toml = (home / ".agent-voice-input" / "config.toml").read_text(encoding="utf-8")
            gen_env = (home / ".agent-voice-input" / ".env").read_text(encoding="utf-8")
            self.assertIn('key = "RControl"', gen_toml)
            self.assertIn("MacBook", gen_toml)
            self.assertIn("ROption", gen_toml)
            self.assertIn("Stage 1: Speech Recognition (REQUIRED", gen_env)
            self.assertIn("Stage 2 Only: Optional Refiner", gen_env)
            self.assertIn("codex login", gen_env)
            self.assertEqual(parse_env_text(gen_env), {})
            example_toml = (_REPO_ROOT / "config.example.toml").read_text(encoding="utf-8")
            example_env = (_REPO_ROOT / "config.example.env").read_text(encoding="utf-8")
            self.assertIn("MacBook", example_toml)
            self.assertIn("Stage 1: Speech Recognition (REQUIRED", example_env)
            self.assertIn("Stage 2 Only: Optional Refiner", example_env)

            # 3. _cmd_start emits explicit WARN when asr_ready==False and mode-accurate Ready hint when asr_ready==True
            parser = cli_mod._build_parser()

            class _LiveProc:
                pid = 42424
                returncode = None

                def poll(self) -> None:
                    return None

            def _run_fresh_start(cfg_in: Config, env_in: dict[str, str]) -> str:
                calls = [0]

                def _ctrl(_rt: Path, _req: dict[str, object], timeout_sec: float = 1.0) -> dict[str, object]:
                    calls[0] += 1
                    if calls[0] == 1:
                        raise ConnectionRefusedError("not running")
                    return {"ok": True, "pid": 42424, "state": "idle", "shim_ok": True}

                buf = io.StringIO()
                with (
                    mock.patch.object(cli_mod.sys, "platform", "linux"),
                    mock.patch("agent_voice_input.control_ipc.send_control_command", side_effect=_ctrl),
                    mock.patch.object(cli_mod.subprocess, "Popen", return_value=_LiveProc()),
                    mock.patch("sys.stdout", buf),
                ):
                    rc = cli_mod._cmd_start(parser.parse_args(["start"]), env_in, cfg=cfg_in)
                self.assertEqual(rc, 0)
                return buf.getvalue()

            # Stage 2-only key (ARK_API_KEY) leaves asr_ready==False -> explicit Warning
            out_no_asr = _run_fresh_start(
                Config(),
                {**env_base, "ARK_API_KEY": "ark_only_stage2_test_key"},
            )
            self.assertIn("Daemon    started (pid=42424)", out_no_asr)
            self.assertIn(
                "Warning   no Stage 1 ASR key configured; run 'agent-voice-input doctor' before dictating",
                out_no_asr,
            )
            self.assertNotIn("Hotkey    ", out_no_asr)

            # DeepSeek single key now auto-selects BOTH Stage 1 ASR (deepseek) & Stage 2 Refiner (deepseek)
            ds_ping_urls: list[str] = []

            def _ds_ping_tx(req: HttpRequest) -> HttpResponse:
                ds_ping_urls.append(req.url)
                return HttpResponse(status=200, headers={}, body=b'{"data":[]}')

            env_ds_auto = {**env_base, "DEEPSEEK_API_KEY": "sk-ds-single-key-ready"}
            buf_ds_doc = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_ds_doc),
            ):
                rc_ds = cli_mod._cmd_doctor(Config(), env_ds_auto, ping=True, as_json=True, transport=_ds_ping_tx)
            self.assertEqual(rc_ds, 0, buf_ds_doc.getvalue())
            ds_doc = json.loads(buf_ds_doc.getvalue())
            self.assertEqual(ds_doc["asr"]["provider"], "deepseek")
            self.assertEqual(ds_doc["asr"]["model"], "deepseek-asr")
            self.assertEqual(ds_doc["asr"]["api"], "deepseek-native")
            self.assertTrue(ds_doc["asr"]["ready"])
            self.assertFalse(ds_doc["asr"]["single_hop"])
            self.assertEqual(ds_doc["refiner"]["resolved_provider"], "deepseek")
            self.assertEqual(ds_doc["ping"]["asr"]["status"], "OK")
            self.assertEqual(ds_doc["ping"]["refiner"]["status"], "OK")
            self.assertIn("shared endpoint", ds_doc["ping"]["refiner"]["detail"])
            self.assertEqual(ds_ping_urls, ["https://api.deepseek.com/v1/models"])

            # Stage 1 key ready + default tap_toggle
            env_ready = {**env_base, "DASHSCOPE_API_KEY": "sk-dash-ready-test-key"}
            out_tap = _run_fresh_start(Config(), env_ready)
            self.assertIn("Daemon    started (pid=42424)", out_tap)
            self.assertIn(
                "Hotkey    tap RControl to start/stop (hold 0.8s to cancel)",
                out_tap,
            )
            self.assertNotIn("Warning   no Stage 1 ASR key", out_tap)

            # Stage 1 key ready + ptt mode with custom hotkey
            out_ptt = _run_fresh_start(
                Config(hotkey=HotkeyConfig(key="ROption", trigger_mode="ptt")),
                env_ready,
            )
            self.assertIn(
                "Hotkey    hold ROption to speak, release to transcribe (press another key to cancel)",
                out_ptt,
            )

            # Stage 1 key ready + tap_toggle with hold_cancel_ms=0 (hold-cancel disabled)
            out_no_hc = _run_fresh_start(
                Config(hotkey=HotkeyConfig(key="Fn", trigger_mode="tap_toggle", hold_cancel_ms=0)),
                env_ready,
            )
            self.assertIn("Hotkey    tap Fn to start/stop", out_no_hc)
            self.assertNotIn("hold", out_no_hc)

            # Degraded CLI-only mode (shim_ok=False): MUST NOT print hotkey Ready line
            def _run_degraded_start(cfg_in: Config, env_in: dict[str, str]) -> str:
                calls = [0]

                def _ctrl(_rt: Path, _req: dict[str, object], timeout_sec: float = 1.0) -> dict[str, object]:
                    calls[0] += 1
                    if calls[0] == 1:
                        raise ConnectionRefusedError("not running")
                    return {
                        "ok": True,
                        "pid": 42424,
                        "state": "idle",
                        "shim_ok": False,
                        "shim_error": "native helper unavailable",
                    }

                buf = io.StringIO()
                err_buf = io.StringIO()
                with (
                    mock.patch.object(cli_mod.sys, "platform", "linux"),
                    mock.patch("agent_voice_input.control_ipc.send_control_command", side_effect=_ctrl),
                    mock.patch.object(cli_mod.subprocess, "Popen", return_value=_LiveProc()),
                    mock.patch("sys.stdout", buf),
                    mock.patch("sys.stderr", err_buf),
                ):
                    rc = cli_mod._cmd_start(parser.parse_args(["start"]), env_in, cfg=cfg_in)
                self.assertEqual(rc, 0)
                return buf.getvalue()

            out_degraded = _run_degraded_start(Config(), env_ready)
            self.assertIn("Daemon    started (pid=42424)", out_degraded)
            self.assertIn(
                "Hotkey    unavailable (fix warning above, then run 'agent-voice-input restart')",
                out_degraded,
            )

            # Socket warming-up timeout: MUST NOT print Hotkey line when ASR is ready,
            # while missing Stage 1 ASR key Warning still prints independently.
            def _run_warming_up_start(cfg_in: Config, env_in: dict[str, str]) -> str:
                mono_vals = iter([0.0, 0.5, 11.0])

                def _fake_mono() -> float:
                    return next(mono_vals, 11.0)

                buf = io.StringIO()
                with (
                    mock.patch.object(cli_mod.sys, "platform", "linux"),
                    mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                    mock.patch(
                        "agent_voice_input.control_ipc.send_control_command",
                        side_effect=ConnectionRefusedError("not ready yet"),
                    ),
                    mock.patch.object(cli_mod.subprocess, "Popen", return_value=_LiveProc()),
                    mock.patch.object(cli_mod.time, "monotonic", side_effect=_fake_mono),
                    mock.patch.object(cli_mod.time, "sleep", return_value=None),
                    mock.patch("sys.stdout", buf),
                ):
                    rc = cli_mod._cmd_start(parser.parse_args(["start"]), env_in, cfg=cfg_in)
                self.assertEqual(rc, 0)
                return buf.getvalue()

            out_warm_ready = _run_warming_up_start(Config(), env_ready)
            self.assertIn("Daemon    starting (pid=42424, socket warming up)", out_warm_ready)
            self.assertNotIn("Hotkey    ", out_warm_ready)

            out_warm_no_asr = _run_warming_up_start(
                Config(),
                {**env_base, "ARK_API_KEY": "ark_only_stage2_test_key"},
            )
            self.assertIn("Daemon    starting (pid=42424, socket warming up)", out_warm_no_asr)
            self.assertIn(
                "Warning   no Stage 1 ASR key configured; run 'agent-voice-input doctor' before dictating",
                out_warm_no_asr,
            )
            self.assertNotIn("Hotkey    ", out_warm_no_asr)


class V100ContractAndFirstRunPolishTests(unittest.TestCase):
    def test_remote_hosts_timeout_ms_and_toml_error_path_prefix(self) -> None:
        from agent_voice_input.config import ConfigError, RemoteHostConfig, load_config

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ok_cfg = root / "ok.toml"
            ok_cfg.write_text(
                '[remote]\ntimeout_ms = 400\n'
                '[[remote.hosts]]\nname = "gpu1"\nssh_target = "gpu1.internal"\ntimeout_ms = 1500\n',
                encoding="utf-8",
            )
            cfg, _ = load_config(config_path=ok_cfg, env={"HOME": str(root)}, load_default_env_files=False)
            self.assertEqual(len(cfg.remote.hosts), 1)
            self.assertEqual(cfg.remote.hosts[0].timeout_ms, 1500)

            for idx, bad_tmo in enumerate((-1, 10001, True, 1.5, '"not_a_num"')):
                bad_cfg = root / f"bad_{idx}.toml"
                bad_cfg.write_text(
                    f'[[remote.hosts]]\nname = "gpu1"\ntimeout_ms = {str(bad_tmo).lower()}\n',
                    encoding="utf-8",
                )
                with self.assertRaises(ConfigError, msg=f"expected ConfigError for timeout_ms={bad_tmo}"):
                    load_config(config_path=bad_cfg, env={"HOME": str(root)}, load_default_env_files=False)

            with self.assertRaises(ConfigError):
                RemoteHostConfig(name="gpu1", ssh_target="gpu1", timeout_ms=-5)
            with self.assertRaises(ConfigError):
                RemoteHostConfig(name="gpu1", ssh_target="gpu1", timeout_ms=20000)

            broken_toml = root / "broken_syntax.toml"
            broken_toml.write_text('[hotkey]\nkey = "unterminated\n', encoding="utf-8")
            with self.assertRaises(ConfigError) as ctx_err:
                load_config(config_path=broken_toml, env={"HOME": str(root)}, load_default_env_files=False)
            self.assertTrue(str(ctx_err.exception).startswith(f"{broken_toml}: "), str(ctx_err.exception))

    def test_hotkey_normalization_whitelist_and_win_shim_invariants(self) -> None:
        from agent_voice_input.config import ConfigError, load_config, normalize_hotkey_key

        expected_pairs = [
            ("RControl", "RControl"),
            ("right_ctrl", "RControl"),
            ("Right-Control", "RControl"),
            ("lctrl", "LControl"),
            ("left control", "LControl"),
            ("RAlt", "RAlt"),
            ("right_alt", "RAlt"),
            ("rmenu", "RAlt"),
            ("right-option", "ROption"),
            ("ROption", "ROption"),
            ("left_alt", "LAlt"),
            ("left_option", "LOption"),
            ("right_shift", "RShift"),
            ("Left Shift", "LShift"),
            ("rwin", "RCommand"),
            ("rsuper", "RCommand"),
            ("right_cmd", "RCommand"),
            ("lwin", "LCommand"),
            ("left_command", "LCommand"),
            ("globe", "Fn"),
            ("function", "Fn"),
            ("caps_lock", "CapsLock"),
            ("scroll-lock", "ScrollLock"),
            ("f13", "F13"),
            ("F001", "F1"),
            ("f024", "F24"),
            ("F24", "F24"),
            ("0x7b", "0x7B"),
            ("0xA3", "0xA3"),
            ("0x00A3", "0xA3"),
            ("0x003E", "0x3E"),
            ("0x0000", "0x00"),
        ]
        for raw, canon in expected_pairs:
            self.assertEqual(normalize_hotkey_key(raw), canon, raw)

        for invalid in ("SpaceBar", "F0", "F00", "F25", "0x100", "0x0100", "0xZZ", "Ctrl+Alt+V", "MenuKey"):
            with self.assertRaises(ConfigError) as err:
                normalize_hotkey_key(invalid)
            self.assertIn("unsupported hotkey", str(err.exception))
            self.assertIn("RControl", str(err.exception))

        # Portable load_config accepts cross-platform union keys; host validation rejects OS-exclusive keys
        from agent_voice_input.config import validate_hotkey_for_platform
        from agent_voice_input.platform_macos import MacOSPlatform, run_macos_key_test
        from agent_voice_input.platform_wsl import WslPlatform, run_key_test

        with tempfile.TemporaryDirectory() as td:
            p_fn = Path(td) / "hk_fn.toml"
            p_fn.write_text('[hotkey]\nkey = "Fn"\n', encoding="utf-8")
            cfg_fn, _ = load_config(config_path=p_fn, env={"HOME": td}, load_default_env_files=False)
            self.assertEqual(cfg_fn.hotkey.key, "Fn")

            p_sl = Path(td) / "hk_sl.toml"
            p_sl.write_text('[hotkey]\nkey = "ScrollLock"\n', encoding="utf-8")
            cfg_sl, _ = load_config(config_path=p_sl, env={"HOME": td}, load_default_env_files=False)
            self.assertEqual(cfg_sl.hotkey.key, "ScrollLock")

            p_rc = Path(td) / "hk.toml"
            p_rc.write_text('[hotkey]\nkey = "right_ctrl"\n', encoding="utf-8")
            cfg_rc, _ = load_config(config_path=p_rc, env={"HOME": td}, load_default_env_files=False)
            self.assertEqual(cfg_rc.hotkey.key, "RControl")

            with self.assertRaises(ConfigError) as w_err:
                validate_hotkey_for_platform("Globe", "wsl")
            self.assertIn("not supported on Windows/WSL2", str(w_err.exception))
            self.assertIn("F1..F24", str(w_err.exception))

            for mac_bad in ("ScrollLock", "Pause", "Insert", "F21", "F24"):
                with self.assertRaises(ConfigError) as m_err:
                    validate_hotkey_for_platform(mac_bad, "darwin")
                self.assertIn("not supported on macOS", str(m_err.exception))
                self.assertIn("F1..F20", str(m_err.exception))

            with self.assertRaises(ConfigError):
                run_key_test(cfg_fn, seconds=1)
            with self.assertRaises(ConfigError):
                run_macos_key_test(cfg_sl, seconds=1, env={"HOME": td})

            wplat_fn = WslPlatform(cfg_fn, env={"HOME": td})
            try:
                with self.assertRaises(ConfigError):
                    wplat_fn._build_shim_cmd()
                self.assertIn("Windows/WSL2", wplat_fn.last_shim_error)
            finally:
                wplat_fn.close()

            mplat_sl = MacOSPlatform(cfg_sl, env={"HOME": td})
            try:
                self.assertIsNone(mplat_sl._build_shim_cmd())
                self.assertIn("macOS", mplat_sl.last_shim_error)
            finally:
                mplat_sl.close()

        for src_path in list_win_shim_sources():
            raw_bytes = src_path.read_bytes()
            self.assertFalse(raw_bytes.startswith(b"\xef\xbb\xbf"), f"{src_path.name} must have no UTF-8 BOM")
            non_ascii_b = [(i + 1, b) for i, b in enumerate(raw_bytes) if b >= 128]
            self.assertEqual(non_ascii_b, [], f"{src_path.name} has non-ASCII bytes: {non_ascii_b[:5]}")
        win_ps1 = read_win_shim_source()
        self.assertNotIn("Add-Type -Path $dllPath", win_ps1)
        self.assertEqual(win_ps1.count("Add-Type -LiteralPath $dllPath"), 2)
        self.assertIn("Write-InitFailureAndExit", win_ps1)
        self.assertLess(win_ps1.index("function Write-JsonLine"), win_ps1.index("Add-Type -AssemblyName"))
        for alias in ("rightcontrol", "rightctrl", "rightalt", "leftcontrol", "leftctrl", "leftalt", "rightshift", "leftshift"):
            self.assertIn(f'"{alias}"', win_ps1)

    def test_wsl_shim_failure_reports_shim_ok_false_and_recovers_when_ready(self) -> None:
        from agent_voice_input.daemon import VoiceInputDaemon
        from agent_voice_input.platform_wsl import WslPlatform
        from agent_voice_input.refiner import NoopRefiner

        bad_shim_code = (
            "import json, sys\n"
            "sys.stdout.write(json.dumps({'ok': False, 'error': 'Unsupported hotkey name: BadKey'}) + '\\n')\n"
            "sys.stdout.flush()\n"
            "sys.exit(1)\n"
        )
        ok_shim_code = (
            "import json, sys\n"
            "sys.stdout.write(json.dumps({'event': 'ready', 'ui_culture': 'en-US'}) + '\\n')\n"
            "sys.stdout.flush()\n"
            "for line in sys.stdin:\n"
            "    if 'quit' in line:\n"
            "        break\n"
        )
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            plat_bad = WslPlatform(
                Config(),
                env={},
                shim_cmd_factory=lambda: [sys.executable, "-u", "-c", bad_shim_code],
            )
            plat_bad._shim_respawn_delays = ()
            d_bad = VoiceInputDaemon(
                Config(),
                platform=plat_bad,
                refiner=NoopRefiner(),
                runtime_dir=rt / "bad",
                env={"HOME": td},
            )
            try:
                plat_bad.start_event_listener(lambda _e: None)
                st_bad = d_bad.handle_control({"cmd": "status"})
                self.assertFalse(st_bad["shim_ok"])
                self.assertIn("Unsupported hotkey name: BadKey", st_bad.get("shim_error", ""))
            finally:
                d_bad.close()

            plat_ok = WslPlatform(
                Config(),
                env={},
                shim_cmd_factory=lambda: [sys.executable, "-u", "-c", ok_shim_code],
            )
            d_ok = VoiceInputDaemon(
                Config(),
                platform=plat_ok,
                refiner=NoopRefiner(),
                runtime_dir=rt / "ok",
                env={"HOME": td},
            )
            try:
                plat_ok.start_event_listener(lambda _e: None)
                st_ok = d_ok.handle_control({"cmd": "status"})
                self.assertTrue(st_ok["shim_ok"])
                self.assertNotIn("shim_error", st_ok)
            finally:
                d_ok.close()

            # Host-unsupported hotkey (Fn on WSL) degrades shim cleanly without crashing daemon startup
            plat_fn = WslPlatform(Config(hotkey=HotkeyConfig(key="Fn")), env={"HOME": td})
            d_fn = VoiceInputDaemon(
                Config(hotkey=HotkeyConfig(key="Fn")),
                platform=plat_fn,
                refiner=NoopRefiner(),
                runtime_dir=rt / "wsl_fn",
                env={"HOME": td},
            )
            try:
                err_capture = io.StringIO()
                with mock.patch("sys.stderr", err_capture):
                    plat_fn.start_event_listener(lambda _e: None)
                self.assertTrue(plat_fn._shim_disabled)
                self.assertFalse(plat_fn._respawn_scheduled)
                st_fn = d_fn.handle_control({"cmd": "status"})
                self.assertTrue(st_fn["ok"])
                self.assertFalse(st_fn["shim_ok"])
                self.assertIn("not supported on Windows/WSL2", st_fn.get("shim_error", ""))
            finally:
                d_fn.close()

    def test_copied_hud_accurate_reasons_and_daemon_forwarding(self) -> None:
        from agent_voice_input.asr_providers import AsrResult
        from agent_voice_input.audio_proc import pack_pcm16_mono_wav
        from agent_voice_input.config import UiConfig
        from agent_voice_input.daemon import VoiceInputDaemon
        from agent_voice_input.hud import format_copied_hud_text
        from agent_voice_input.platform_common import ForegroundWindow, PasteOutcome
        from agent_voice_input.refiner import NoopRefiner

        self.assertEqual(format_copied_hud_text("accessibility_denied", "zh"), "✓ 已复制(需开启辅助功能)")
        self.assertEqual(format_copied_hud_text("accessibility_denied (grant ...)", "en"), "✓ Copied (grant Accessibility)")
        self.assertEqual(
            format_copied_hud_text("automation_denied (grant Automation permission for Ghostty)", "zh"),
            "✓ 已复制(需开启自动化权限)",
        )
        self.assertEqual(
            format_copied_hud_text("automation_denied (grant Automation permission for Ghostty)", "en"),
            "✓ Copied (grant Automation)",
        )
        self.assertEqual(format_copied_hud_text("key_held", "zh"), "✓ 已复制(检测到按键按住)")
        self.assertEqual(format_copied_hud_text("key_held", "en"), "✓ Copied (modifier held)")
        self.assertEqual(format_copied_hud_text("hotkey_held", "zh"), "✓ 已复制(检测到按键按住)")
        self.assertEqual(format_copied_hud_text("hotkey_held", "en"), "✓ Copied (modifier held)")
        self.assertEqual(format_copied_hud_text("send_input_failed", "zh"), "✓ 已复制(无法自动粘贴)")
        self.assertEqual(format_copied_hud_text("send_input_failed", "en"), "✓ Copied (manual paste needed)")
        self.assertEqual(format_copied_hud_text("focus_changed", "zh"), "✓ 已复制(窗口已切换)")
        self.assertEqual(format_copied_hud_text("focus_changed", "en"), "✓ Copied (window switched)")

        voiced = pack_pcm16_mono_wav(b"\x00\x20\x00\xe0" * 4000)

        class _StaticAsr:
            provider_name = "mock"
            model_name = "mock"
            def transcribe(self, *_a, **_kw):
                return AsrResult(text="hello", provider="mock", model="mock", latency_ms=1)

        class _SpyPlat:
            def __init__(self, outcome: PasteOutcome) -> None:
                self.outcome = outcome
                self.hud_calls: list[tuple[str, dict[str, object]]] = []
            def start_event_listener(self, _cb) -> None: pass
            def is_allowed_terminal(self, _w) -> bool: return True
            def get_foreground_window(self) -> ForegroundWindow: return ForegroundWindow(1, "Terminal", "zsh")
            def start_recording(self, *_a, **_kw) -> None: pass
            def stop_recording(self, **_kw): return voiced, "mock"
            def cancel_recording(self) -> None: pass
            def hud(self, state: str, **kw: object) -> None:
                self.hud_calls.append((state, dict(kw)))
            def beep(self, _k: str) -> None: pass
            def deliver_text(self, *_a, **_kw) -> PasteOutcome: return self.outcome
            def close(self) -> None: pass

        for reason, lang, exp_text in (
            ("accessibility_denied (grant Accessibility)", "zh", "✓ 已复制(需开启辅助功能)"),
            ("automation_denied (grant Automation permission for Ghostty)", "zh", "✓ 已复制(需开启自动化权限)"),
            ("automation_denied", "en", "✓ Copied (grant Automation)"),
            ("key_held", "zh", "✓ 已复制(检测到按键按住)"),
            ("hotkey_held", "en", "✓ Copied (modifier held)"),
            ("send_input_failed", "en", "✓ Copied (manual paste needed)"),
            ("focus_changed", "en", None),
        ):
            with tempfile.TemporaryDirectory() as td:
                plat = _SpyPlat(PasteOutcome(False, True, reason, "Notes"))
                d = VoiceInputDaemon(
                    Config(hotkey=HotkeyConfig(debounce_ms=0), ui=UiConfig(language=lang)),
                    platform=plat,  # type: ignore[arg-type]
                    asr_provider=_StaticAsr(),  # type: ignore[arg-type]
                    refiner=NoopRefiner(),
                    runtime_dir=Path(td),
                    env={"HOME": td},
                )
                try:
                    d.handle_trigger("toggle", ForegroundWindow(1, "Terminal", "zsh"), from_hotkey=True)
                    d.handle_trigger("toggle", ForegroundWindow(1, "Terminal", "zsh"), from_hotkey=True)
                    deadline = time.monotonic() + 3.0
                    while time.monotonic() < deadline:
                        st_now = d.handle_control({"cmd": "status"})
                        if st_now.get("state") == "idle" and int(st_now.get("pending_count") or 0) == 0:
                            break
                        time.sleep(0.02)
                    self.assertEqual(d.handle_control({"cmd": "status"}).get("state"), "idle")
                finally:
                    d.close()
                copied_calls = [kw for st, kw in plat.hud_calls if st == "copied"]
                self.assertEqual(len(copied_calls), 1, plat.hud_calls)
                self.assertEqual(copied_calls[0].get("text"), exp_text)

    def test_wsl_probe_powershell_resolution_and_doctor_and_start_polish(self) -> None:
        from agent_voice_input import platform_wsl as pw
        from agent_voice_input.config import AudioConfig

        # 1. resolve_powershell_exe lookup order: POWERSHELL_EXE -> PATH -> /c/... -> None
        with (
            mock.patch("agent_voice_input.platform_wsl.os.path.isfile", side_effect=lambda p: str(p) == pw.POWERSHELL_EXE),
            mock.patch("agent_voice_input.platform_wsl.shutil.which", return_value="/custom/powershell.exe"),
        ):
            self.assertEqual(pw.resolve_powershell_exe(), pw.POWERSHELL_EXE)
        with (
            mock.patch("agent_voice_input.platform_wsl.os.path.isfile", side_effect=lambda p: str(p) == "/custom/powershell.exe"),
            mock.patch("agent_voice_input.platform_wsl.shutil.which", return_value="/custom/powershell.exe"),
        ):
            self.assertEqual(pw.resolve_powershell_exe({"PATH": "/custom"}), "/custom/powershell.exe")
        with (
            mock.patch("agent_voice_input.platform_wsl.os.path.isfile", side_effect=lambda p: str(p) == pw._POWERSHELL_EXE_ROOT_MOUNT),
            mock.patch("agent_voice_input.platform_wsl.shutil.which", return_value=None),
        ):
            self.assertEqual(pw.resolve_powershell_exe({"PATH": ""}), pw._POWERSHELL_EXE_ROOT_MOUNT)
        with (
            mock.patch("agent_voice_input.platform_wsl.os.path.isfile", return_value=False),
            mock.patch("agent_voice_input.platform_wsl.shutil.which", return_value=None),
        ):
            self.assertIsNone(pw.resolve_powershell_exe({"PATH": ""}))

        # 2. probe_wsl_host timeout=180.0 + redacted first-line ps_error; WslPlatform._shim_ready_timeout=180.0
        self.assertEqual(pw.WslPlatform._shim_ready_timeout, 180.0)
        seen_timeouts: list[object] = []

        def _fake_probe_run(cmd, **kw):
            if cmd[0] == pw.POWERSHELL_EXE:
                seen_timeouts.append(kw.get("timeout"))
                return subprocess.CompletedProcess(
                    cmd, 1, stdout="", stderr="AuthorizationManager check failed: ExecutionPolicy blocked secret_tok_999\nline2\n"
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="RDPSource\n", stderr="")

        with (
            mock.patch("agent_voice_input.platform_wsl.resolve_powershell_exe", return_value=pw.POWERSHELL_EXE),
            mock.patch("agent_voice_input.platform_wsl.to_windows_path", return_value=r"C:\win_shim.ps1"),
            mock.patch("agent_voice_input.platform_wsl.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
            mock.patch("agent_voice_input.platform_wsl.subprocess.run", side_effect=_fake_probe_run),
        ):
            pinfo = pw.probe_wsl_host(Config(), env={"OPENAI_API_KEY": "secret_tok_999"})
        self.assertEqual(seen_timeouts, [180.0])
        self.assertFalse(pinfo["powershell_ok"])
        self.assertIn("ExecutionPolicy", pinfo["ps_error"])
        self.assertNotIn("secret_tok_999", pinfo["ps_error"])
        self.assertNotIn("line2", pinfo["ps_error"])

        # 3. WSL doctor: wasapi_ok=False with WSLg pulse_rdp_ok=True MUST fail (no false green) unless wsl_source="pulse";
        #    not ps_ok surfaces ps_error + ExecutionPolicy hint
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            env_wsl = {
                "HOME": td,
                "PATH": "",
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
                "DASHSCOPE_API_KEY": "ds_test_aaaa",
            }
            buf_nomic = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=True),
                mock.patch(
                    "agent_voice_input.platform_wsl.probe_wsl_host",
                    return_value={
                        "powershell_ok": True,
                        "ps_version": "5.1",
                        "wasapi_ok": False,
                        "wasapi_state": "mic_permission:0x80070005",
                        "pulse_rdp_ok": True,
                        "winmm_devs": 1,
                    },
                ),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_nomic),
            ):
                self.assertEqual(cli_mod._cmd_doctor(Config(), env_wsl), 1)
            out_nomic = buf_nomic.getvalue()
            self.assertIn("WASAPI=FAIL (mic_permission:0x80070005)", out_nomic)
            self.assertIn("ms-settings:privacy-microphone", out_nomic)

            # Explicit wsl_source="winmm" with wasapi_ok=False & winmm_devs=1 MUST still fail (no false green)
            buf_winmm_fail = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=True),
                mock.patch(
                    "agent_voice_input.platform_wsl.probe_wsl_host",
                    return_value={
                        "powershell_ok": True,
                        "ps_version": "5.1",
                        "wasapi_ok": False,
                        "wasapi_state": "mic_permission:0x80070005",
                        "pulse_rdp_ok": True,
                        "winmm_devs": 1,
                    },
                ),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_winmm_fail),
            ):
                self.assertEqual(cli_mod._cmd_doctor(Config(audio=AudioConfig(wsl_source="winmm")), env_wsl), 1)
            self.assertIn("WASAPI=FAIL", buf_winmm_fail.getvalue())

            # auto + custom AGENT_VOICE_INPUT_PULSE_SOURCE selects Pulse backend & checks custom source (even when RDPSource=False & wasapi_ok=False)
            env_custom_pulse = {**env_wsl, "AGENT_VOICE_INPUT_PULSE_SOURCE": "alsa_input.usb-Blue_Yeti"}
            with (
                mock.patch("agent_voice_input.platform_wsl.resolve_powershell_exe", return_value=None),
                mock.patch("agent_voice_input.platform_wsl.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch(
                    "agent_voice_input.platform_wsl.subprocess.run",
                    return_value=subprocess.CompletedProcess(
                        ["ffmpeg"], 0, stdout="  alsa_input.usb-Blue_Yeti [Blue Yeti]\n", stderr=""
                    ),
                ),
            ):
                p_custom = pw.probe_wsl_host(Config(), env=env_custom_pulse)
            self.assertFalse(p_custom["pulse_rdp_ok"])
            self.assertEqual(p_custom["pulse_source"], "alsa_input.usb-Blue_Yeti")
            self.assertTrue(p_custom["pulse_source_ok"])

            # Exact token match: prefix substring `alsa_input.usb-Mic` MUST NOT match `alsa_input.usb-Mic_2`
            env_prefix_pulse = {**env_wsl, "AGENT_VOICE_INPUT_PULSE_SOURCE": "alsa_input.usb-Mic"}
            with (
                mock.patch("agent_voice_input.platform_wsl.resolve_powershell_exe", return_value=None),
                mock.patch("agent_voice_input.platform_wsl.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch(
                    "agent_voice_input.platform_wsl.subprocess.run",
                    return_value=subprocess.CompletedProcess(
                        ["ffmpeg"], 0, stdout="Auto-detected sources for pulse:\n* alsa_input.usb-Mic_2 [USB Microphone]\n", stderr=""
                    ),
                ),
            ):
                p_prefix = pw.probe_wsl_host(Config(), env=env_prefix_pulse)
            self.assertFalse(p_prefix["pulse_source_ok"])

            buf_custom_pulse = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=True),
                mock.patch(
                    "agent_voice_input.platform_wsl.probe_wsl_host",
                    return_value={
                        "powershell_ok": True,
                        "ps_version": "5.1",
                        "wasapi_ok": False,
                        "pulse_rdp_ok": False,
                        "pulse_source": "alsa_input.usb-Blue_Yeti",
                        "pulse_source_ok": True,
                        "winmm_devs": 0,
                    },
                ),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_custom_pulse),
            ):
                self.assertEqual(cli_mod._cmd_doctor(Config(), env_custom_pulse), 0, buf_custom_pulse.getvalue())
            self.assertIn("PulseAudio (alsa_input.usb-Blue_Yeti)=OK", buf_custom_pulse.getvalue())

            # Host-incompatible hotkey flagged by doctor on WSL2 (Fn) and macOS (F24)
            buf_wsl_badhk = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=True),
                mock.patch(
                    "agent_voice_input.platform_wsl.probe_wsl_host",
                    return_value={"powershell_ok": True, "ps_version": "5.1", "wasapi_ok": True, "pulse_rdp_ok": False, "winmm_devs": 1},
                ),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_wsl_badhk),
            ):
                self.assertEqual(cli_mod._cmd_doctor(Config(hotkey=HotkeyConfig(key="Fn")), env_wsl), 1)
            self.assertIn("[hotkey]    FAIL", buf_wsl_badhk.getvalue())
            self.assertIn("Windows/WSL2", buf_wsl_badhk.getvalue())

            # Explicit wsl_source="pulse" allows pulse_rdp_ok=True to pass
            buf_pulse = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=True),
                mock.patch(
                    "agent_voice_input.platform_wsl.probe_wsl_host",
                    return_value={
                        "powershell_ok": True,
                        "ps_version": "5.1",
                        "wasapi_ok": False,
                        "pulse_rdp_ok": True,
                        "winmm_devs": 0,
                    },
                ),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_pulse),
            ):
                self.assertEqual(cli_mod._cmd_doctor(Config(audio=AudioConfig(wsl_source="pulse")), env_wsl), 0)

            # PowerShell failure surfaces ps_error + ExecutionPolicy hint
            buf_psfail = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=True),
                mock.patch(
                    "agent_voice_input.platform_wsl.probe_wsl_host",
                    return_value={
                        "powershell_ok": False,
                        "ps_error": "blocked by ConstrainedLanguage policy",
                        "pulse_rdp_ok": True,
                        "winmm_devs": 0,
                    },
                ),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_psfail),
            ):
                self.assertEqual(cli_mod._cmd_doctor(Config(), env_wsl), 1)
            self.assertIn("ConstrainedLanguage", buf_psfail.getvalue())
            self.assertIn("[win-shim]  HINT", buf_psfail.getvalue())

            # 4. macOS doctor: not_determined local GUI -> non-blocking INFO; xcodebuild -license -> sudo xcodebuild -license accept
            probe_mac_nd = {
                "avfoundation_ok": True,
                "audio_devices": ["[0] Mic"],
                "configured_audio_device": ":default",
                "pbcopy_ok": True,
                "tmux_active": False,
                "clt_ok": True,
                "swiftc_ok": True,
                "swiftc_bin": "/usr/bin/swiftc",
                "mac_shim_ok": True,
                "mac_shim_bin": "/tmp/mac_shim",
                "caller_mic_auth": "not_determined",
                "caller_has_graphic_session": True,
                "caller_is_remote_session": False,
                "fg_process": "Ghostty",
                "fg_title": "zsh",
                "permissions_hint": "Microphone -> Ghostty",
            }
            buf_nd = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "darwin"),
                mock.patch("agent_voice_input.platform_macos.probe_macos_host", return_value=probe_mac_nd),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/opt/homebrew/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_nd),
            ):
                self.assertEqual(cli_mod._cmd_doctor(Config(), env_wsl), 0, buf_nd.getvalue())
            self.assertIn("[macos-tcc] INFO", buf_nd.getvalue())

            probe_mac_lic = {
                **probe_mac_nd,
                "mac_shim_ok": False,
                "mac_shim_error": "You have not agreed to the Xcode license. Run 'sudo xcodebuild -license'",
                "caller_mic_auth": "authorized",
            }
            buf_lic = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "darwin"),
                mock.patch("agent_voice_input.platform_macos.probe_macos_host", return_value=probe_mac_lic),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/opt/homebrew/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_lic),
            ):
                self.assertEqual(cli_mod._cmd_doctor(Config(), env_wsl), 1)
            self.assertIn("Fix: sudo xcodebuild -license accept", buf_lic.getvalue())
            self.assertNotIn("Fix: xcode-select --install", buf_lic.getvalue())

            buf_mac_badhk = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "darwin"),
                mock.patch("agent_voice_input.platform_macos.probe_macos_host", return_value=probe_mac_nd),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/opt/homebrew/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_mac_badhk),
            ):
                self.assertEqual(cli_mod._cmd_doctor(Config(hotkey=HotkeyConfig(key="F24")), env_wsl), 1)
            self.assertIn("[hotkey]    FAIL", buf_mac_badhk.getvalue())
            self.assertIn("macOS", buf_mac_badhk.getvalue())

            # 5. _inspect_hook_summary validates structured hooks.*.command fields & rejects unrelated JSON keys
            claude_dir = Path(td) / ".claude"
            claude_dir.mkdir(parents=True, exist_ok=True)
            gemini_dir = Path(td) / ".gemini"
            gemini_dir.mkdir(parents=True, exist_ok=True)
            codex_dir = Path(td) / ".codex"
            codex_dir.mkdir(parents=True, exist_ok=True)

            # Negative cases: unrelated `cmd`, `permissions`, or non-event nested `command` entries MUST NOT match
            (claude_dir / "settings.json").write_text('{"cmd": "~/.local/bin/voice-title.sh"}', encoding="utf-8")
            (gemini_dir / "hooks.json").write_text(
                '{"metadata": {"permissions": [{"command": "voice-title.sh"}]}}', encoding="utf-8"
            )
            (codex_dir / "config.toml").write_text('[tui]\nterminal_title = ["spinner"]\n# session-hook\n', encoding="utf-8")
            self.assertIn("hooks=none configured (optional)", cli_mod._inspect_hook_summary(env_wsl, rt))

            (claude_dir / "settings.json").write_text(
                '{"permissions": {"allow": ["Bash(agent-voice-input session-hook *)"]}}', encoding="utf-8"
            )
            self.assertIn("hooks=none configured (optional)", cli_mod._inspect_hook_summary(env_wsl, rt))

            # Positive case: Codex native [tui].terminal_title containing "thread-id" MUST match
            (codex_dir / "config.toml").write_text(
                '[tui]\nterminal_title = ["spinner", "project-name", "thread-title", "thread-id"]\n',
                encoding="utf-8",
            )
            self.assertIn("hooks=codex", cli_mod._inspect_hook_summary(env_wsl, rt))

            # Positive cases: official repo Claude/Gemini hook JSON schemas + voice-title.sh + Codex notify
            claude_official = json.loads(
                (_REPO_ROOT / "integrations" / "claude" / "settings.hooks.json").read_text(encoding="utf-8")
            )
            claude_official["hooks"]["SessionStart"][0]["hooks"][0]["command"] = "~/.local/bin/voice-title.sh"
            (claude_dir / "settings.json").write_text(json.dumps(claude_official), encoding="utf-8")
            (gemini_dir / "hooks.json").write_text(
                (_REPO_ROOT / "integrations" / "gemini" / "hooks.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (codex_dir / "config.toml").write_text(
                'notify = ["agent-voice-input", "session-hook", "--cli", "codex"]\n',
                encoding="utf-8",
            )
            hook_sum = cli_mod._inspect_hook_summary(env_wsl, rt)
            self.assertIn("claude", hook_sum)
            self.assertIn("gemini/agy", hook_sum)
            self.assertIn("codex", hook_sum)

            # 6. _cmd_start missing ffmpeg warns non-blockingly; WSL waits >10s (up to 18s) for slow cold socket
            parser = cli_mod._build_parser()
            self.assertNotIn("Two-stage", parser.description or "")

            class _FakeProc:
                pid = 51515
                returncode = None
                def poll(self): return None

            # WSL slow cold start: socket becomes ready at t=12.5s (past 10s, within 18s WSL budget)
            wsl_times = iter([0.0, 5.0, 12.5, 13.0])
            wsl_ctrl_calls = [0]

            def _wsl_slow_ctrl(_rt, _req, timeout_sec: float = 1.0):
                wsl_ctrl_calls[0] += 1
                if wsl_ctrl_calls[0] <= 2:
                    raise ConnectionRefusedError("starting win_shim")
                return {"ok": True, "pid": 51515, "state": "idle", "shim_ok": True}

            out_wsl_slow = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=True),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("agent_voice_input.control_ipc.send_control_command", side_effect=_wsl_slow_ctrl),
                mock.patch.object(cli_mod.subprocess, "Popen", return_value=_FakeProc()),
                mock.patch.object(cli_mod.time, "monotonic", side_effect=lambda: next(wsl_times, 25.0)),
                mock.patch.object(cli_mod.time, "sleep", return_value=None),
                mock.patch("sys.stdout", out_wsl_slow),
            ):
                rc_wsl_slow = cli_mod._cmd_start(parser.parse_args(["start"]), env_wsl, cfg=Config())
            self.assertEqual(rc_wsl_slow, 0)
            self.assertIn("started (pid=51515)", out_wsl_slow.getvalue())
            self.assertNotIn("socket warming up", out_wsl_slow.getvalue())

            for plat_name, exp_cmd in (("linux", "sudo apt install ffmpeg"), ("darwin", "brew install ffmpeg")):
                calls = [0]
                def _ctrl(_rt, _req, timeout_sec: float = 1.0):
                    calls[0] += 1
                    if calls[0] == 1:
                        raise ConnectionRefusedError("stopped")
                    return {"ok": True, "pid": 51515, "state": "idle", "shim_ok": True}
                out_s, err_s = io.StringIO(), io.StringIO()
                with (
                    mock.patch.object(cli_mod.sys, "platform", plat_name),
                    mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", side_effect=FileNotFoundError("no ffmpeg")),
                    mock.patch("agent_voice_input.platform_macos.compile_mac_shim", return_value=(None, "")),
                    mock.patch("agent_voice_input.control_ipc.send_control_command", side_effect=_ctrl),
                    mock.patch.object(cli_mod.subprocess, "Popen", return_value=_FakeProc()),
                    mock.patch("sys.stdout", out_s),
                    mock.patch("sys.stderr", err_s),
                ):
                    rc_s = cli_mod._cmd_start(parser.parse_args(["start"]), env_wsl, cfg=Config())
                self.assertEqual(rc_s, 0)
                self.assertIn("WARN: ffmpeg not found", err_s.getvalue())
                self.assertIn(exp_cmd, err_s.getvalue())

        # 7. pyproject.toml & __init__.py metadata updated while preserving version & Development Status
        import agent_voice_input as pkg_init
        from agent_voice_input.config import parse_toml

        self.assertEqual(pkg_init.__version__, "0.17.0")
        self.assertNotIn("Two-stage", pkg_init.__doc__ or "")
        pyp = parse_toml((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(pyp["project"]["version"], "0.17.0")
        self.assertIn("Development Status :: 4 - Beta", pyp["project"]["classifiers"])
        self.assertNotIn("Two-stage", pyp["project"]["description"])
        for kw in ("antigravity", "agy", "dictation", "tmux"):
            self.assertIn(kw, pyp["project"]["keywords"])


class OobeP0CliIntegrationTests(unittest.TestCase):
    def test_is_macbook_laptop_detection_matrix(self) -> None:
        # 1. Non-darwin always returns False without invoking subprocess
        with (
            mock.patch.object(cli_mod.sys, "platform", "linux"),
            mock.patch.object(cli_mod.subprocess, "run", side_effect=AssertionError("must not run on linux")),
        ):
            self.assertFalse(cli_mod._is_macbook_laptop())

        # 2. Intel/M1 MacBook via sysctl hw.model -> True directly
        with (
            mock.patch.object(cli_mod.sys, "platform", "darwin"),
            mock.patch.object(
                cli_mod.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(["sysctl"], 0, stdout="MacBookPro18,1\n", stderr=""),
            ) as m_run,
        ):
            self.assertTrue(cli_mod._is_macbook_laptop())
            self.assertEqual(m_run.call_count, 1)

        # 3. Desktop Macs (Mac mini / Mac Studio / iMac / Mac Pro) -> False directly
        for desktop_model in ("Macmini9,1", "MacStudio1,1", "iMac21,1", "MacPro7,1"):
            with (
                mock.patch.object(cli_mod.sys, "platform", "darwin"),
                mock.patch.object(
                    cli_mod.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess(["sysctl"], 0, stdout=f"{desktop_model}\n", stderr=""),
                ) as m_run,
            ):
                self.assertFalse(cli_mod._is_macbook_laptop(), desktop_model)
                self.assertEqual(m_run.call_count, 1)

        # 4. Apple Silicon Mac16,10 (M4 Mac mini) -> sysctl generic, pmset AC Power only -> False
        def _run_mini(cmd, **_kw):
            if cmd[0] == "sysctl":
                return subprocess.CompletedProcess(cmd, 0, stdout="Mac16,10\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="Now drawing from 'AC Power'\n", stderr="")

        with (
            mock.patch.object(cli_mod.sys, "platform", "darwin"),
            mock.patch.object(cli_mod.subprocess, "run", side_effect=_run_mini),
        ):
            self.assertFalse(cli_mod._is_macbook_laptop())

        # 5. Apple Silicon Mac15,3 (M3 MacBook Air) -> sysctl generic, pmset has InternalBattery -> True
        def _run_mba(cmd, **_kw):
            if cmd[0] == "sysctl":
                return subprocess.CompletedProcess(cmd, 0, stdout="Mac15,3\n", stderr="")
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout="Now drawing from 'Battery Power'\n -InternalBattery-0 (id=123)\t88%; discharging\n",
                stderr="",
            )

        with (
            mock.patch.object(cli_mod.sys, "platform", "darwin"),
            mock.patch.object(cli_mod.subprocess, "run", side_effect=_run_mba),
        ):
            self.assertTrue(cli_mod._is_macbook_laptop())

        # 6. Subprocess errors / timeouts fall back conservatively to False
        with (
            mock.patch.object(cli_mod.sys, "platform", "darwin"),
            mock.patch.object(
                cli_mod.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(cmd="sysctl", timeout=0.5),
            ),
        ):
            self.assertFalse(cli_mod._is_macbook_laptop())

    def test_init_env_aware_macbook_vs_desktop_flags_and_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            # 1. MacBook without --hotkey -> auto writes ROption + clear note
            h_mb = root / "macbook"
            h_mb.mkdir()
            env_mb = {"HOME": str(h_mb), "PATH": ""}
            out_mb = io.StringIO()
            with (
                mock.patch.dict(os.environ, env_mb, clear=True),
                mock.patch.object(cli_mod.sys, "platform", "darwin"),
                mock.patch.object(cli_mod, "_is_macbook_laptop", return_value=True),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/opt/homebrew/bin/ffmpeg"),
                mock.patch("sys.stdout", out_mb),
            ):
                self.assertEqual(cli_mod.main(["init"]), 0, out_mb.getvalue())
            toml_mb = (h_mb / ".agent-voice-input" / "config.toml").read_text(encoding="utf-8")
            self.assertIn('key = "ROption"', toml_mb)
            self.assertIn("Hotkey    ROption (detected MacBook built-in keyboard", out_mb.getvalue())
            self.assertIn("--hotkey RControl", out_mb.getvalue())

            # 2. MacBook with explicit --hotkey RControl + --asr bailian + --refiner off
            out_mb_exp = io.StringIO()
            with (
                mock.patch.dict(os.environ, env_mb, clear=True),
                mock.patch.object(cli_mod.sys, "platform", "darwin"),
                mock.patch.object(cli_mod, "_is_macbook_laptop", return_value=True),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/opt/homebrew/bin/ffmpeg"),
                mock.patch("sys.stdout", out_mb_exp),
            ):
                rc_exp = cli_mod.main(
                    ["init", "--force", "--asr", "bailian", "--refiner", "off", "--hotkey", "RControl"]
                )
            self.assertEqual(rc_exp, 0, out_mb_exp.getvalue())
            toml_mb_exp = (h_mb / ".agent-voice-input" / "config.toml").read_text(encoding="utf-8")
            self.assertIn('key = "RControl"', toml_mb_exp)
            self.assertIn('provider = "dashscope"', toml_mb_exp)
            self.assertIn('provider = "off"', toml_mb_exp)
            self.assertNotIn("MacBook built-in keyboard", out_mb_exp.getvalue())

            # 3. Desktop Mac (Mac mini) -> keeps RControl without MacBook message
            h_mini = root / "macmini"
            h_mini.mkdir()
            env_mini = {"HOME": str(h_mini), "PATH": ""}
            out_mini = io.StringIO()
            with (
                mock.patch.dict(os.environ, env_mini, clear=True),
                mock.patch.object(cli_mod.sys, "platform", "darwin"),
                mock.patch.object(cli_mod, "_is_macbook_laptop", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/opt/homebrew/bin/ffmpeg"),
                mock.patch("sys.stdout", out_mini),
            ):
                self.assertEqual(cli_mod.main(["init"]), 0, out_mini.getvalue())
            self.assertIn(
                'key = "RControl"',
                (h_mini / ".agent-voice-input" / "config.toml").read_text(encoding="utf-8"),
            )
            self.assertNotIn("MacBook built-in keyboard", out_mini.getvalue())

            # 4. Invalid --asr / --refiner / --hotkey return exit code 2 without creating files
            h_bad = root / "bad_flags"
            h_bad.mkdir()
            env_bad = {"HOME": str(h_bad), "PATH": ""}
            for argv in (
                ["init", "--asr", "unknown_vendor"],
                ["init", "--refiner", "unknown_ref"],
                ["init", "--hotkey", "NotAKey"],
            ):
                err_b = io.StringIO()
                with (
                    mock.patch.dict(os.environ, env_bad, clear=True),
                    mock.patch.object(cli_mod.sys, "platform", "linux"),
                    mock.patch("sys.stderr", err_b),
                ):
                    self.assertEqual(cli_mod.main(argv), 2, f"argv={argv} stderr={err_b.getvalue()}")
                self.assertIn("config error:", err_b.getvalue())
            self.assertFalse((h_bad / ".config" / "agent-voice-input" / "config.toml").exists())

            # 5. Ready out of the box (Stage 1 DEEPSEEK_API_KEY + local Codex ChatGPT login)
            h_ready = root / "ready_home"
            (h_ready / ".codex").mkdir(parents=True)
            (h_ready / ".codex" / "auth.json").write_text(
                json.dumps({"tokens": {"access_token": _fake_jwt()}}), encoding="utf-8"
            )
            env_ready = {"HOME": str(h_ready), "PATH": "", "DEEPSEEK_API_KEY": "sk-ds-init-ready-1234"}
            out_ready = io.StringIO()
            with (
                mock.patch.dict(os.environ, env_ready, clear=True),
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", out_ready),
            ):
                self.assertEqual(cli_mod.main(["init"]), 0)
            txt_ready = out_ready.getvalue()
            self.assertIn("Status    ready out of the box", txt_ready)
            self.assertIn("ASR       deepseek / deepseek-asr", txt_ready)
            self.assertIn("Refiner   codex / ", txt_ready)
            self.assertIn("No need to edit", txt_ready)
            self.assertIn("agent-voice-input doctor --ping && agent-voice-input start", txt_ready)
            self.assertNotIn("sk-ds-init-ready-1234", txt_ready)

            # 6. No Stage 1 key + local Codex login + WSL2 with unconfigured ~/.claude -> highlights free Codex,
            #    lists 4 1-key recommendations, hints integrate, and NEVER modifies ~/.claude/settings.json
            h_wsl = root / "wsl_home"
            (h_wsl / ".codex").mkdir(parents=True)
            (h_wsl / ".codex" / "auth.json").write_text(
                json.dumps({"tokens": {"access_token": _fake_jwt()}}), encoding="utf-8"
            )
            (h_wsl / ".claude").mkdir(parents=True)
            env_wsl = {"HOME": str(h_wsl), "PATH": "", "WSL_DISTRO_NAME": "Ubuntu"}
            out_wsl = io.StringIO()
            with (
                mock.patch.dict(os.environ, env_wsl, clear=True),
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=True),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", out_wsl),
            ):
                self.assertEqual(cli_mod.main(["init"]), 0)
            txt_wsl = out_wsl.getvalue()
            self.assertIn("local subscription ready for free", txt_wsl)
            for k in ("DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
                self.assertIn(k, txt_wsl)
            self.assertIn("agent-voice-input integrate", txt_wsl)
            self.assertFalse((h_wsl / ".claude" / "settings.json").exists())

    def test_doctor_hooks_json_macbook_warn_vs_mini_and_next_line(self) -> None:
        from agent_voice_input.integrate import AGY_HOOK_COMMAND, CLAUDE_HOOK_COMMAND

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            rt = root / "rt"
            (home / ".codex").mkdir(parents=True)
            (home / ".claude").mkdir(parents=True)
            (home / ".gemini" / "config").mkdir(parents=True)
            (home / ".codex" / "config.toml").write_text(
                '[tui]\nterminal_title = ["spinner", "project-name", "thread-title", "thread-id"]\n',
                encoding="utf-8",
            )
            (home / ".claude" / "settings.json").write_text(
                json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": CLAUDE_HOOK_COMMAND}]}]}}),
                encoding="utf-8",
            )
            (home / ".gemini" / "config" / "hooks.json").write_text(
                json.dumps({"voice-session-title": {"SessionStart": [{"type": "command", "command": AGY_HOOK_COMMAND}]}}),
                encoding="utf-8",
            )

            self.assertTrue(cli_mod._command_matches_voice_hook(CLAUDE_HOOK_COMMAND))
            self.assertTrue(cli_mod._command_matches_voice_hook(AGY_HOOK_COMMAND))
            self.assertTrue(cli_mod._command_matches_voice_hook("~/.local/bin/voice-title.sh"))
            self.assertFalse(cli_mod._command_matches_voice_hook("echo hello"))

            env = {
                "HOME": str(home),
                "PATH": "",
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
                "DEEPSEEK_API_KEY": "sk-ds-doc-test-9999",
            }
            probe_ok = {
                "avfoundation_ok": True,
                "audio_devices": ["[0] Mic"],
                "configured_audio_device": ":default",
                "pbcopy_ok": True,
                "tmux_active": False,
                "clt_ok": True,
                "swiftc_ok": True,
                "swiftc_bin": "/usr/bin/swiftc",
                "mac_shim_ok": True,
                "mac_shim_bin": "/tmp/mac_shim",
                "caller_mic_auth": "authorized",
                "caller_has_graphic_session": True,
                "caller_is_remote_session": False,
                "fg_process": "Ghostty",
                "fg_title": "zsh",
                "permissions_hint": "Microphone -> Ghostty",
            }

            # 1. MacBook + RControl -> gentle [hotkey] WARN, rc=0, Next: start
            buf_mb = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "darwin"),
                mock.patch.object(cli_mod, "_is_macbook_laptop", return_value=True),
                mock.patch("agent_voice_input.platform_macos.probe_macos_host", return_value=probe_ok),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/opt/homebrew/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_mb),
            ):
                self.assertEqual(cli_mod._cmd_doctor(Config(), env), 0)
            txt_mb = buf_mb.getvalue()
            self.assertIn("[hotkey]    WARN", txt_mb)
            self.assertIn("ROption", txt_mb)
            self.assertIn("Next: run 'agent-voice-input start'", txt_mb)
            self.assertIn("hooks=claude, gemini/agy, codex", txt_mb)

            # 2. Desktop Mac (Mac mini) + RControl -> ZERO [hotkey] WARN!
            buf_mini = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "darwin"),
                mock.patch.object(cli_mod, "_is_macbook_laptop", return_value=False),
                mock.patch("agent_voice_input.platform_macos.probe_macos_host", return_value=probe_ok),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/opt/homebrew/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_mini),
            ):
                self.assertEqual(cli_mod._cmd_doctor(Config(), env), 0)
            self.assertNotIn("[hotkey]    WARN", buf_mini.getvalue())

            # 3. doctor --json emits structured `hooks` field with claude/codex/agy entries
            buf_js = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=True),
                mock.patch(
                    "agent_voice_input.platform_wsl.probe_wsl_host",
                    return_value={"powershell_ok": True, "ps_version": "5.1", "wasapi_ok": True, "pulse_rdp_ok": False, "winmm_devs": 1},
                ),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_js),
            ):
                self.assertEqual(cli_mod._cmd_doctor(Config(), env, as_json=True), 0)
            rep = json.loads(buf_js.getvalue())
            self.assertIn("hooks", rep)
            self.assertEqual(rep["hooks"]["necessity"], "recommended")
            for cli_name in ("claude", "codex", "agy"):
                entry = rep["hooks"]["clis"][cli_name]
                self.assertTrue(entry["installed"])
                self.assertTrue(entry["configured"])
                self.assertTrue(entry["recommended"])
                self.assertTrue(entry["config_path"])

            # 4. Next: variants (running healthy -> Ready; running drifted -> restart; failed -> Fix:)
            buf_run = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch(
                    "agent_voice_input.control_ipc.probe_daemon_status",
                    return_value={"ok": True, "pid": 1234, "state": "idle"},
                ),
                mock.patch("sys.stdout", buf_run),
            ):
                self.assertEqual(cli_mod._cmd_doctor(Config(), env), 0)
            self.assertIn("Next: Ready", buf_run.getvalue())

            buf_drift = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch(
                    "agent_voice_input.control_ipc.probe_daemon_status",
                    return_value={"ok": True, "pid": 1234, "state": "idle", "version": "0.1.0"},
                ),
                mock.patch("sys.stdout", buf_drift),
            ):
                self.assertEqual(cli_mod._cmd_doctor(Config(), env), 0)
            self.assertIn("Next: run 'agent-voice-input restart'", buf_drift.getvalue())

            buf_fail = io.StringIO()
            env_nokey = {"HOME": str(home), "PATH": "", "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)}
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_fail),
            ):
                self.assertEqual(cli_mod._cmd_doctor(Config(), env_nokey), 1)
            self.assertIn("Next: fix the FAIL item(s) above (see Fix:)", buf_fail.getvalue())
            self.assertIn("agent-voice-input doctor --ping", buf_fail.getvalue())

    def test_start_first_run_acceptance_tip_and_integrate_cli_subcommand(self) -> None:
        parser = cli_mod._build_parser()

        class _LiveProc:
            pid = 61616
            returncode = None

            def poll(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            rt = root / "rt"
            cache = root / "cache"
            home.mkdir()
            rt.mkdir()
            env = {
                "HOME": str(home),
                "XDG_CACHE_HOME": str(cache),
                "PATH": "",
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
                "DEEPSEEK_API_KEY": "sk-ds-start-tip-1234",
            }

            def _run_start(cfg_in: Config) -> str:
                calls = [0]

                def _ctrl(_rt: Path, _req: dict[str, object], timeout_sec: float = 1.0) -> dict[str, object]:
                    calls[0] += 1
                    if calls[0] == 1:
                        raise ConnectionRefusedError("stopped")
                    return {"ok": True, "pid": 61616, "state": "idle", "shim_ok": True}

                buf = io.StringIO()
                with (
                    mock.patch.object(cli_mod.sys, "platform", "linux"),
                    mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                    mock.patch("agent_voice_input.control_ipc.send_control_command", side_effect=_ctrl),
                    mock.patch.object(cli_mod.subprocess, "Popen", return_value=_LiveProc()),
                    mock.patch("sys.stdout", buf),
                ):
                    rc = cli_mod._cmd_start(parser.parse_args(["start"]), env, cfg=cfg_in)
                self.assertEqual(rc, 0)
                return buf.getvalue()

            # 1. First start (no telemetry.jsonl) emits Tip with configured hotkey
            out_first = _run_start(Config(hotkey=HotkeyConfig(key="ROption")))
            self.assertIn(
                "Tip       tap ROption to test live, or run 'agent-voice-input once --duration 4 --dry-run'",
                out_first,
            )

            # 2. Subsequent start (telemetry.jsonl exists) suppresses first-run Tip
            (rt / "telemetry.jsonl").write_text('{"ok": true}\n', encoding="utf-8")
            out_second = _run_start(Config(hotkey=HotkeyConfig(key="ROption")))
            self.assertNotIn("once --duration 4 --dry-run", out_second)

            # 3. `integrate` CLI subcommand forwarding (--status, --dry-run, --cli comma & repeat, --uninstall, --json, errors)
            (home / ".claude").mkdir()
            (home / ".codex").mkdir()
            (home / ".gemini").mkdir()

            out_st = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=True), mock.patch("sys.stdout", out_st):
                self.assertEqual(cli_mod.main(["integrate", "--status", "--json"]), 0)
            st_payload = json.loads(out_st.getvalue())
            self.assertTrue(st_payload["ok"])
            self.assertEqual(st_payload["mode"], "status")
            self.assertEqual([x["cli"] for x in st_payload["items"]], ["claude", "codex", "agy"])

            out_dry = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=True), mock.patch("sys.stdout", out_dry):
                self.assertEqual(cli_mod.main(["integrate", "--dry-run", "--cli", "claude,codex", "--json"]), 0)
            dry_payload = json.loads(out_dry.getvalue())
            self.assertTrue(dry_payload["ok"])
            self.assertTrue(dry_payload["dry_run"])
            self.assertEqual([a["cli"] for a in dry_payload["actions"]], ["claude", "codex"])
            self.assertEqual([a["action"] for a in dry_payload["actions"]], ["would_install", "would_install"])
            self.assertFalse((home / ".claude" / "settings.json").exists())

            out_apply = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=True), mock.patch("sys.stdout", out_apply):
                self.assertEqual(cli_mod.main(["integrate", "--cli", "claude", "--cli", "agy", "--json"]), 0)
            apply_payload = json.loads(out_apply.getvalue())
            self.assertTrue(apply_payload["ok"])
            self.assertEqual([a["action"] for a in apply_payload["actions"]], ["installed", "installed"])
            self.assertTrue((home / ".claude" / "settings.json").is_file())
            self.assertTrue((home / ".gemini" / "config" / "hooks.json").is_file())

            out_un = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=True), mock.patch("sys.stdout", out_un):
                self.assertEqual(cli_mod.main(["integrate", "--uninstall", "--cli", "claude,agy", "--json"]), 0)
            un_payload = json.loads(out_un.getvalue())
            self.assertTrue(un_payload["ok"])
            self.assertEqual(un_payload["mode"], "uninstall")
            self.assertEqual([a["action"] for a in un_payload["actions"]], ["uninstalled", "uninstalled"])

            # Invalid CLI returns exit code 2
            err_bad = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=True), mock.patch("sys.stderr", err_bad):
                self.assertEqual(cli_mod.main(["integrate", "--cli", "unknown"]), 2)
            self.assertIn("unsupported CLI target", err_bad.getvalue())

            # Corrupt config triggers error action -> non-zero exit code 1
            (home / ".claude" / "settings.json").write_text("{broken-json", encoding="utf-8")
            out_err = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=True), mock.patch("sys.stdout", out_err):
                self.assertEqual(cli_mod.main(["integrate", "--cli", "claude", "--json"]), 1)
            err_payload = json.loads(out_err.getvalue())
            self.assertFalse(err_payload["ok"])
            self.assertEqual(err_payload["actions"][0]["action"], "error")

    def test_cli_integrate_respects_codex_home_and_xdg_cache_home(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            custom_codex = root / "custom-codex"
            custom_cache = root / "custom-xdg-cache"
            home.mkdir()
            custom_codex.mkdir()
            codex_toml = custom_codex / "config.toml"
            codex_toml.write_text('model = "gpt-5"\n', encoding="utf-8")

            env = {
                "HOME": str(home),
                "PATH": "",
                "CODEX_HOME": str(custom_codex),
                "XDG_CACHE_HOME": str(custom_cache),
            }

            out_st1 = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=True), mock.patch("sys.stdout", out_st1):
                self.assertEqual(cli_mod.main(["integrate", "--status", "--cli", "codex", "--json"]), 0)
            st1 = json.loads(out_st1.getvalue())
            self.assertTrue(st1["ok"])
            self.assertEqual(len(st1["items"]), 1)
            self.assertEqual(st1["items"][0]["config_path"], str(codex_toml))
            self.assertTrue(st1["items"][0]["installed"])
            self.assertFalse(st1["items"][0]["configured"])

            out_apply = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=True), mock.patch("sys.stdout", out_apply):
                self.assertEqual(cli_mod.main(["integrate", "--cli", "codex", "--json"]), 0)
            applied = json.loads(out_apply.getvalue())
            self.assertTrue(applied["ok"])
            act = applied["actions"][0]
            self.assertEqual(act["action"], "installed")
            self.assertEqual(act["config_path"], str(codex_toml))
            expected_backup_root = str(custom_cache / "agent-voice-input" / "integrate-backups")
            self.assertTrue(act["backup_path"].startswith(expected_backup_root), act["backup_path"])
            self.assertTrue(Path(act["backup_path"]).is_file())
            self.assertIn("thread-id", parse_toml(codex_toml.read_text(encoding="utf-8"))["tui"]["terminal_title"])
            self.assertFalse((home / ".codex").exists())
            self.assertFalse((home / ".cache").exists())

            out_st2 = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=True), mock.patch("sys.stdout", out_st2):
                self.assertEqual(cli_mod.main(["integrate", "--status", "--cli", "codex", "--json"]), 0)
            st2 = json.loads(out_st2.getvalue())
            self.assertTrue(st2["items"][0]["configured"])

    def test_doctor_hooks_consistent_under_custom_codex_home_and_ptt_next_line(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            rt = root / "rt"
            custom_codex = root / "custom-codex"
            home.mkdir()
            custom_codex.mkdir()
            codex_toml = custom_codex / "config.toml"
            codex_toml.write_text('[tui]\nterminal_title = ["spinner", "thread-id"]\n', encoding="utf-8")

            env = {
                "HOME": str(home),
                "PATH": "",
                "CODEX_HOME": str(custom_codex),
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
                "DEEPSEEK_API_KEY": "sk-ds-codex-home-9999",
            }

            buf_txt = io.StringIO()
            buf_json = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
            ):
                with mock.patch("sys.stdout", buf_txt):
                    self.assertEqual(cli_mod._cmd_doctor(Config(), env, as_json=False), 0)
                with mock.patch("sys.stdout", buf_json):
                    self.assertEqual(cli_mod._cmd_doctor(Config(), env, as_json=True), 0)

            txt_out = buf_txt.getvalue()
            rep = json.loads(buf_json.getvalue())
            self.assertIn("[hooks]     hooks=codex,", txt_out)
            self.assertNotIn("none configured", txt_out)
            self.assertTrue(rep["hooks"]["clis"]["codex"]["configured"])
            self.assertEqual(rep["hooks"]["clis"]["codex"]["config_path"], str(codex_toml))
            self.assertTrue(any("[hooks]     hooks=codex," in ln for ln in rep["lines"]))
            self.assertFalse((home / ".codex").exists())

            # Running daemon Next: line respects trigger_mode ("ptt" -> hold, "tap_toggle" -> tap)
            for mode, expected_verb in (("ptt", "hold"), ("tap_toggle", "tap")):
                buf_mode = io.StringIO()
                with (
                    mock.patch.object(cli_mod.sys, "platform", "linux"),
                    mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                    mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                    mock.patch(
                        "agent_voice_input.control_ipc.probe_daemon_status",
                        return_value={"ok": True, "pid": 4321, "state": "idle"},
                    ),
                    mock.patch("sys.stdout", buf_mode),
                ):
                    rc = cli_mod._cmd_doctor(
                        Config(hotkey=HotkeyConfig(key="ROption", trigger_mode=mode)), env
                    )
                self.assertEqual(rc, 0)
                self.assertIn(
                    f"Next: Ready — daemon is running; {expected_verb} ROption to dictate",
                    buf_mode.getvalue(),
                )

    def test_pypi_readme_links_no_github_alerts_and_docs_url(self) -> None:
        import re

        readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
        # Strip fenced code blocks before inspecting Markdown links and callouts
        stripped = re.sub(r"```.*?```", "", readme, flags=re.DOTALL)
        self.assertNotRegex(stripped, r"(?m)^>\s*\[!(?:TIP|NOTE|IMPORTANT|WARNING|CAUTION)\]")
        rel_links: list[str] = []
        for m in re.finditer(r"\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", stripped):
            target = m.group(1).strip()
            if not target.startswith(("https://", "http://", "mailto:", "#")):
                rel_links.append(target)
        self.assertEqual(rel_links, [], f"Relative links break on PyPI: {rel_links}")
        self.assertIn(
            "https://github.com/xiaohuaxi/agent-voice-input/blob/main/README.zh-CN.md",
            readme,
        )
        self.assertIn("> **Tip:**", readme)

        pyp = parse_toml((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(pyp["project"]["version"], "0.17.0")
        self.assertEqual(
            pyp["project"]["urls"]["Documentation"],
            "https://github.com/xiaohuaxi/agent-voice-input#readme",
        )

    def test_dotenv_proxy_precedence_transport_routing_and_doctor_socks5_warn(self) -> None:
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from agent_voice_input.asr_providers import create_asr_provider
        from agent_voice_input.config import load_env_files
        from agent_voice_input.http_transport import (
            HttpTransportError,
            inspect_proxy_config_warning,
            make_urllib_http_transport,
        )

        # 1. Loopback HTTP proxy + origin verifying .env proxy is honored by ASR, Refiner, and doctor --ping
        proxy_hits: list[str] = []

        class _OriginHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                body = b'{"data":[]}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                if length:
                    self.rfile.read(length)
                body = json.dumps(
                    {"choices": [{"message": {"content": "proxied ok"}}]}
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                pass

        origin_srv = HTTPServer(("127.0.0.1", 0), _OriginHandler)
        origin_port = origin_srv.server_address[1]
        origin_thr = threading.Thread(target=origin_srv.serve_forever, daemon=True)
        origin_thr.start()

        class _ProxyHandler(BaseHTTPRequestHandler):
            def _forward(self) -> None:
                import http.client
                import urllib.parse

                proxy_hits.append(f"{self.command} {self.path}")
                parsed = urllib.parse.urlsplit(self.path)
                length = int(self.headers.get("Content-Length", "0"))
                req_body = self.rfile.read(length) if length else b""
                conn = http.client.HTTPConnection(parsed.hostname or "127.0.0.1", parsed.port or 80, timeout=5)
                try:
                    path_q = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
                    conn.request(self.command, path_q, body=req_body, headers={"Host": parsed.netloc})
                    resp = conn.getresponse()
                    data = resp.read()
                    self.send_response(resp.status)
                    self.send_header("Content-Type", resp.getheader("Content-Type", "application/json"))
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                finally:
                    conn.close()

            def do_GET(self) -> None:
                self._forward()

            def do_POST(self) -> None:
                self._forward()

            def log_message(self, *_args: object) -> None:
                pass

        proxy_srv = HTTPServer(("127.0.0.1", 0), _ProxyHandler)
        proxy_port = proxy_srv.server_address[1]
        proxy_thr = threading.Thread(target=proxy_srv.serve_forever, daemon=True)
        proxy_thr.start()

        try:
            with tempfile.TemporaryDirectory() as td:
                home = Path(td) / "home"
                rt = Path(td) / "rt"
                cfg_dir = home / ".agent-voice-input"
                cfg_dir.mkdir(parents=True)
                (cfg_dir / "config.toml").write_text(
                    f'[asr]\nprovider = "openai-compatible"\nbase_url = "http://127.0.0.1:{origin_port}/v1"\n'
                    f'[refiner]\nprovider = "openai-compatible"\nbase_url = "http://127.0.0.1:{origin_port}/v1"\nmodel = "m1"\n',
                    encoding="utf-8",
                )
                (cfg_dir / ".env").write_text(
                    f'HTTP_PROXY="http://127.0.0.1:{proxy_port}"\n'
                    'http_proxy="http://127.0.0.1:1"\n',
                    encoding="utf-8",
                )

                # Shell/system env MUST win over .env regardless of uppercase/lowercase key differences
                merged_sys_wins = load_env_files(
                    {"HOME": str(home), "HTTP_PROXY": f"http://127.0.0.1:{proxy_port}"},
                    home_dir=home,
                )
                self.assertEqual(merged_sys_wins.get("HTTP_PROXY"), f"http://127.0.0.1:{proxy_port}")
                self.assertNotIn("http_proxy", merged_sys_wins)

                # Higher-priority .env uppercase HTTPS_PROXY MUST NOT be shadowed by lower-priority .env lowercase https_proxy
                primary_env = cfg_dir / ".env"
                secondary_env = cfg_dir / "secondary.env"
                primary_env.write_text(
                    f'HTTPS_PROXY="http://127.0.0.1:{proxy_port}"\n',
                    encoding="utf-8",
                )
                secondary_env.write_text('https_proxy="http://127.0.0.1:1"\n', encoding="utf-8")
                try:
                    merged_cross_env = load_env_files(
                        {"HOME": str(home)},
                        env_paths=[primary_env, secondary_env],
                        home_dir=home,
                    )
                finally:
                    secondary_env.unlink()
                self.assertEqual(merged_cross_env.get("HTTPS_PROXY"), f"http://127.0.0.1:{proxy_port}")
                self.assertNotIn("https_proxy", merged_cross_env)

                # Clean .env with valid HTTP_PROXY drives ASR transport, Refiner transport, and doctor --ping
                (cfg_dir / ".env").write_text(
                    f'HTTP_PROXY="http://127.0.0.1:{proxy_port}"\n',
                    encoding="utf-8",
                )
                clean_os_env = {
                    k: v
                    for k, v in os.environ.items()
                    if not (k.lower().endswith("_proxy") or k.lower() == "no_proxy")
                }
                with mock.patch.dict(os.environ, clean_os_env, clear=True):
                    cfg_p, env_p = load_config(
                        env={"HOME": str(home), "PATH": "", "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)},
                        home_dir=home,
                    )
                    self.assertEqual(env_p.get("HTTP_PROXY"), f"http://127.0.0.1:{proxy_port}")

                    # ASR provider default transport uses .env HTTP_PROXY
                    asr_prov = create_asr_provider(cfg_p, env=env_p)
                    r_asr = asr_prov.transport(
                        HttpRequest(f"http://127.0.0.1:{origin_port}/v1/models", "GET", {}, b"", 5.0)
                    )
                    self.assertEqual(r_asr.status, 200)

                    # HTTP Refiner default transport uses .env HTTP_PROXY
                    ref_prov = create_refiner(cfg_p, env=env_p)
                    r_ref = ref_prov.refine("hello")
                    self.assertEqual(r_ref.text, "proxied ok")

                    # doctor --ping uses .env HTTP_PROXY
                    buf_ping = io.StringIO()
                    with (
                        mock.patch.object(cli_mod.sys, "platform", "linux"),
                        mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                        mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                        mock.patch("sys.stdout", buf_ping),
                    ):
                        rc_ping = cli_mod._cmd_doctor(cfg_p, env_p, ping=True)
                    self.assertEqual(rc_ping, 0, buf_ping.getvalue())
                    self.assertIn("[ping]      asr(openai-compatible): OK", buf_ping.getvalue())
                    self.assertGreaterEqual(len(proxy_hits), 3, proxy_hits)

                # 2. SOCKS5-only ALL_PROXY fails closed on transport AND warns cleanly in doctor without leaking creds
                socks_env = {
                    "HOME": str(home),
                    "PATH": "",
                    "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
                    "DEEPSEEK_API_KEY": "sk-ds-proxy-warn-1111",
                    "ALL_PROXY": "socks5://alice:SuperSecretProxyPw99@127.0.0.1:1080",
                }
                warn = inspect_proxy_config_warning(socks_env)
                self.assertIsNotNone(warn)
                assert warn is not None
                self.assertIn("socks5", warn[0].lower())
                self.assertNotIn("SuperSecretProxyPw99", warn[0])
                self.assertIn("HTTPS_PROXY=http://127.0.0.1:<port>", warn[1])

                tx_socks = make_urllib_http_transport(socks_env)
                with self.assertRaises(HttpTransportError) as cm_socks:
                    tx_socks(HttpRequest("https://api.deepseek.com/v1/models", "GET", {}, b"", 3.0))
                self.assertNotIn("SuperSecretProxyPw99", str(cm_socks.exception))

                buf_socks = io.StringIO()
                with (
                    mock.patch.object(cli_mod.sys, "platform", "linux"),
                    mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                    mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                    mock.patch("sys.stdout", buf_socks),
                ):
                    self.assertEqual(cli_mod._cmd_doctor(Config(), socks_env), 0)
                txt_socks = buf_socks.getvalue()
                self.assertIn("[proxy]     WARN", txt_socks)
                self.assertIn("HTTPS_PROXY=http://127.0.0.1:<port>", txt_socks)
                self.assertNotIn("SuperSecretProxyPw99", txt_socks)

                # 3. Dual env (HTTPS_PROXY=http://... + ALL_PROXY=socks5://...) MUST NOT false-warn
                dual_env = {**socks_env, "HTTPS_PROXY": f"http://127.0.0.1:{proxy_port}"}
                self.assertIsNone(inspect_proxy_config_warning(dual_env))

                # 4. config.example.env documents proxy support while staying 100% commented
                example_env_txt = (_REPO_ROOT / "config.example.env").read_text(encoding="utf-8")
                self.assertIn("HTTPS_PROXY", example_env_txt)
                self.assertIn("SOCKS5", example_env_txt)
                self.assertEqual(parse_env_text(example_env_txt), {})
        finally:
            proxy_srv.shutdown()
            proxy_srv.server_close()
            origin_srv.shutdown()
            origin_srv.server_close()

    def test_init_doctor_start_first_run_stage2_only_clt_and_degraded_hotkey(self) -> None:
        parser = cli_mod._build_parser()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            rt = root / "rt"
            home.mkdir()
            rt.mkdir()

            # 1. Stage 2-only key (ARK_API_KEY) + no Stage 1 key: init & doctor clearly explain Stage 2 cannot do ASR
            env_ark = {
                "HOME": str(home),
                "PATH": "",
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
                "ARK_API_KEY": "ark-stage2-only-test-9999",
            }
            out_init_ark = io.StringIO()
            with (
                mock.patch.dict(os.environ, env_ark, clear=True),
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", out_init_ark),
            ):
                self.assertEqual(cli_mod.main(["init"]), 0)
            txt_init_ark = out_init_ark.getvalue()
            self.assertIn("Stage 2 refiner-only key (ARK_API_KEY)", txt_init_ark)
            self.assertIn("cannot transcribe", txt_init_ark)
            self.assertIn("OPENAI_API_KEY", txt_init_ark)

            # doctor after init (config/env exist): no selected=dashscope, no redundant 'run init', includes OPENAI_API_KEY
            cfg_after, env_after = load_config(env=env_ark, home_dir=home)
            buf_doc_ark = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_doc_ark),
            ):
                self.assertEqual(cli_mod._cmd_doctor(cfg_after, env_after), 1)
            txt_doc_ark = buf_doc_ark.getvalue()
            self.assertIn("[asr]       selected=auto (none configured; no Stage 1 ASR key found) -> MISSING KEY", txt_doc_ark)
            self.assertNotIn("selected=dashscope", txt_doc_ark)
            self.assertIn("NOTE detected Stage 2 refiner-only key (ARK_API_KEY)", txt_doc_ark)
            self.assertIn("OPENAI_API_KEY", txt_doc_ark)
            self.assertNotIn("run 'agent-voice-input init' then", txt_doc_ark)

            # doctor before init (no config/env exist) STILL suggests 'agent-voice-input init' + OPENAI_API_KEY
            empty_home = root / "empty_home"
            empty_home.mkdir()
            env_fresh = {"HOME": str(empty_home), "PATH": "", "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)}
            buf_doc_fresh = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_doc_fresh),
            ):
                self.assertEqual(cli_mod._cmd_doctor(Config(), env_fresh), 1)
            txt_doc_fresh = buf_doc_fresh.getvalue()
            self.assertIn("run 'agent-voice-input init' then", txt_doc_fresh)
            self.assertIn("OPENAI_API_KEY", txt_doc_fresh)

            # 2. macOS init proactively runs zero-modal check_macos_clt_ready() and warns with xcode-select --install
            mac_home = root / "mac_noclts"
            mac_home.mkdir()
            env_mac = {"HOME": str(mac_home), "PATH": ""}
            out_mac_init = io.StringIO()
            with (
                mock.patch.dict(os.environ, env_mac, clear=True),
                mock.patch.object(cli_mod.sys, "platform", "darwin"),
                mock.patch.object(cli_mod, "_is_macbook_laptop", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/opt/homebrew/bin/ffmpeg"),
                mock.patch(
                    "agent_voice_input.platform_macos.check_macos_clt_ready",
                    return_value=(False, "Xcode Command Line Tools not installed; run 'xcode-select --install'"),
                ),
                mock.patch("sys.stdout", out_mac_init),
            ):
                self.assertEqual(cli_mod.main(["init"]), 0)
            self.assertIn("Warning   Xcode Command Line Tools not installed; run 'xcode-select --install'", out_mac_init.getvalue())

            # 3. start with shim_ok=False prints explicit Hotkey unavailable row (both with and without ASR key)
            class _FakeProc:
                pid = 71717
                returncode = None

                def poll(self) -> None:
                    return None

            for env_case, expect_warn in (
                ({**env_fresh, "DEEPSEEK_API_KEY": "sk-ds-degraded-1234"}, False),
                (env_fresh, True),
            ):
                calls = [0]

                def _ctrl_degraded(_rt: Path, _req: dict[str, object], timeout_sec: float = 1.0) -> dict[str, object]:
                    calls[0] += 1
                    if calls[0] == 1:
                        raise ConnectionRefusedError("stopped")
                    return {"ok": True, "pid": 71717, "state": "idle", "shim_ok": False, "shim_error": "no CLT"}

                out_deg, err_deg = io.StringIO(), io.StringIO()
                with (
                    mock.patch.object(cli_mod.sys, "platform", "linux"),
                    mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                    mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                    mock.patch("agent_voice_input.control_ipc.send_control_command", side_effect=_ctrl_degraded),
                    mock.patch.object(cli_mod.subprocess, "Popen", return_value=_FakeProc()),
                    mock.patch("sys.stdout", out_deg),
                    mock.patch("sys.stderr", err_deg),
                ):
                    self.assertEqual(cli_mod._cmd_start(parser.parse_args(["start"]), env_case, cfg=Config()), 0)
                txt_deg = out_deg.getvalue()
                self.assertIn(
                    "Hotkey    unavailable (fix warning above, then run 'agent-voice-input restart')",
                    txt_deg,
                )
                if expect_warn:
                    self.assertIn("Warning   no Stage 1 ASR key configured", txt_deg)

    def test_wsl_cold_start_180s_timeout_and_slow_stderr_notice(self) -> None:
        from agent_voice_input import platform_wsl as pw

        self.assertEqual(pw.WSL_SHIM_COLD_TIMEOUT_SEC, 180.0)
        self.assertEqual(pw.WslPlatform._shim_ready_timeout, 180.0)

        # 1. probe_wsl_host(announce_slow=True): fast run prints nothing; slow run prints one stderr progress line
        ok_probe_json = json.dumps(
            {
                "ok": True,
                "ps_version": "5.1",
                "wave_in_devs": 1,
                "wasapi_ok": True,
                "hwnd": 1,
                "process": "WindowsTerminal",
                "title": "bash",
                "ui_culture": "en-US",
            }
        )

        def _fast_run(cmd, **_kw):
            if cmd[0] == pw.POWERSHELL_EXE:
                return subprocess.CompletedProcess(cmd, 0, stdout=ok_probe_json + "\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="RDPSource\n", stderr="")

        err_fast = io.StringIO()
        with (
            mock.patch("agent_voice_input.platform_wsl.resolve_powershell_exe", return_value=pw.POWERSHELL_EXE),
            mock.patch("agent_voice_input.platform_wsl.to_windows_path", return_value=r"C:\win_shim.ps1"),
            mock.patch("agent_voice_input.platform_wsl.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
            mock.patch("agent_voice_input.platform_wsl.subprocess.run", side_effect=_fast_run),
            mock.patch("sys.stderr", err_fast),
        ):
            info_fast = pw.probe_wsl_host(Config(), env={"PATH": ""}, announce_slow=True)
        self.assertTrue(info_fast["powershell_ok"])
        self.assertEqual(err_fast.getvalue(), "")

        notice_emitted = threading.Event()

        class _NoticeStderr(io.StringIO):
            def write(self, s: str) -> int:
                written = super().write(s)
                if s:
                    notice_emitted.set()
                return written

        def _slow_run(cmd, **kw):
            if cmd and cmd[0] == pw.POWERSHELL_EXE:
                notice_emitted.wait(timeout=2.0)
                return subprocess.CompletedProcess(cmd, 0, stdout=ok_probe_json + "\n", stderr="")
            if kw.get("text"):
                return subprocess.CompletedProcess(cmd, 0, stdout="RDPSource\n", stderr="")
            return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"")

        err_slow = _NoticeStderr()
        with (
            mock.patch.object(pw, "_WSL_SLOW_START_NOTICE_SEC", 0.02),
            mock.patch("agent_voice_input.platform_wsl.resolve_powershell_exe", return_value=pw.POWERSHELL_EXE),
            mock.patch("agent_voice_input.platform_wsl.to_windows_path", return_value=r"C:\win_shim.ps1"),
            mock.patch("agent_voice_input.platform_wsl.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
            mock.patch("agent_voice_input.platform_wsl.subprocess.run", side_effect=_slow_run),
            mock.patch("sys.stderr", err_slow),
        ):
            info_slow = pw.probe_wsl_host(Config(), env={"PATH": ""}, announce_slow=True)
        self.assertTrue(notice_emitted.is_set())
        self.assertTrue(info_slow["powershell_ok"])
        self.assertIn("Initializing Windows helper", err_slow.getvalue())
        self.assertIn("Ctrl+C", err_slow.getvalue())

        # doctor --json must pass announce_slow=False so stdout JSON & stderr stay clean
        def _slow_json_run(cmd, **kw):
            if cmd and cmd[0] == pw.POWERSHELL_EXE:
                time.sleep(0.05)
                return subprocess.CompletedProcess(cmd, 0, stdout=ok_probe_json + "\n", stderr="")
            if kw.get("text"):
                return subprocess.CompletedProcess(cmd, 0, stdout="RDPSource\n", stderr="")
            return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"")

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            env_wsl = {
                "HOME": td,
                "PATH": "",
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
                "DEEPSEEK_API_KEY": "sk-ds-wsl-json-1234",
            }
            out_js, err_js = io.StringIO(), io.StringIO()
            with (
                mock.patch.object(pw, "_WSL_SLOW_START_NOTICE_SEC", 0.02),
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=True),
                mock.patch("agent_voice_input.platform_wsl.resolve_powershell_exe", return_value=pw.POWERSHELL_EXE),
                mock.patch("agent_voice_input.platform_wsl.to_windows_path", return_value=r"C:\win_shim.ps1"),
                mock.patch("agent_voice_input.platform_wsl.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("agent_voice_input.platform_wsl.subprocess.run", side_effect=_slow_json_run),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", out_js),
                mock.patch("sys.stderr", err_js),
            ):
                self.assertEqual(cli_mod._cmd_doctor(Config(), env_wsl, as_json=True), 0)
            self.assertEqual(err_js.getvalue(), "")
            self.assertTrue(json.loads(out_js.getvalue())["ok"])

            # 2. _cmd_start on WSL waits up to 180s and prints one stderr progress line once >= 2s elapsed
            parser = cli_mod._build_parser()

            class _FakeProc:
                pid = 81818
                returncode = None

                def poll(self) -> None:
                    return None

            times_cold = iter([0.0, 0.5, 3.0, 95.0, 120.0])
            ctrl_n = [0]

            def _cold_ctrl(_rt: Path, _req: dict[str, object], timeout_sec: float = 1.0) -> dict[str, object]:
                ctrl_n[0] += 1
                if ctrl_n[0] <= 3:
                    raise ConnectionRefusedError("compiling C# shim")
                return {"ok": True, "pid": 81818, "state": "idle", "shim_ok": True}

            out_cold, err_cold = io.StringIO(), io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=True),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("agent_voice_input.control_ipc.send_control_command", side_effect=_cold_ctrl),
                mock.patch.object(cli_mod.subprocess, "Popen", return_value=_FakeProc()),
                mock.patch.object(cli_mod.time, "monotonic", side_effect=lambda: next(times_cold, 190.0)),
                mock.patch.object(cli_mod.time, "sleep", return_value=None),
                mock.patch("sys.stdout", out_cold),
                mock.patch("sys.stderr", err_cold),
            ):
                rc_cold = cli_mod._cmd_start(parser.parse_args(["start"]), env_wsl, cfg=Config())
            self.assertEqual(rc_cold, 0)
            self.assertIn("started (pid=81818)", out_cold.getvalue())
            self.assertNotIn("socket warming up", out_cold.getvalue())
            self.assertEqual(err_cold.getvalue().count("Initializing Windows helper"), 1)

            # Fast warm start (< 2s) stays completely quiet on stderr
            times_warm = iter([0.0, 0.4, 0.8])
            warm_n = [0]

            def _warm_ctrl(_rt: Path, _req: dict[str, object], timeout_sec: float = 1.0) -> dict[str, object]:
                warm_n[0] += 1
                if warm_n[0] == 1:
                    raise ConnectionRefusedError("starting")
                return {"ok": True, "pid": 81818, "state": "idle", "shim_ok": True}

            out_warm, err_warm = io.StringIO(), io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=True),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("agent_voice_input.control_ipc.send_control_command", side_effect=_warm_ctrl),
                mock.patch.object(cli_mod.subprocess, "Popen", return_value=_FakeProc()),
                mock.patch.object(cli_mod.time, "monotonic", side_effect=lambda: next(times_warm, 190.0)),
                mock.patch.object(cli_mod.time, "sleep", return_value=None),
                mock.patch("sys.stdout", out_warm),
                mock.patch("sys.stderr", err_warm),
            ):
                self.assertEqual(cli_mod._cmd_start(parser.parse_args(["start"]), env_wsl, cfg=Config()), 0)
            self.assertEqual(err_warm.getvalue(), "")

    def test_cli_hotwords_aliases_stage2_budget_and_agent_tips(self) -> None:
        from agent_voice_input.hotwords import clear_hotwords_cache

        def _run_cli(argv: list[str], env: dict[str, str]) -> tuple[int, str, str]:
            out_buf, err_buf = io.StringIO(), io.StringIO()
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("sys.stdout", out_buf),
                mock.patch("sys.stderr", err_buf),
            ):
                rc = cli_mod.main(argv)
            return rc, out_buf.getvalue(), err_buf.getvalue()

        # 1. Help text mentions '<-' aliases & '--alias', and surrogate recovery handles '--alias'
        parser = cli_mod._build_parser()
        hw_help = io.StringIO()
        with mock.patch("sys.stdout", hw_help):
            with self.assertRaises(SystemExit) as cm_h1:
                parser.parse_args(["hotwords", "--help"])
        self.assertEqual(cm_h1.exception.code, 0)
        self.assertIn("<-", hw_help.getvalue())
        self.assertIn("--alias", hw_help.getvalue())

        add_help = io.StringIO()
        with mock.patch("sys.stdout", add_help):
            with self.assertRaises(SystemExit) as cm_h2:
                parser.parse_args(["hotwords", "add", "--help"])
        self.assertEqual(cm_h2.exception.code, 0)
        self.assertIn("--alias", add_help.getvalue())

        surrogate_alias = "哈尼斯,哈尼思".encode("utf-8").decode("ascii", errors="surrogateescape")
        surrogate_cfg = "/tmp/" + "中文路径".encode("utf-8").decode("ascii", errors="surrogateescape") + "/config.toml"
        parsed_sur = parser.parse_args(
            ["hotwords", "add", "Harness", "--alias", surrogate_alias, "--config", surrogate_cfg]
        )
        cli_mod._recover_parsed_text_args(parsed_sur)
        self.assertEqual(parsed_sur.alias, ["哈尼斯,哈尼思"])
        self.assertEqual(parsed_sur.config, surrogate_cfg)

        clear_hotwords_cache()
        try:
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td)
                home = tmp / "home"
                home.mkdir()
                app_home = tmp / "avi_home"
                cfg_path = tmp / "config.toml"
                cfg_path.write_text(
                    '[asr]\nprovider = "doubao"\n',
                    encoding="utf-8",
                )
                env = {
                    "HOME": str(home),
                    "AGENT_VOICE_INPUT_HOME": str(app_home),
                    "DOUBAO_SPEECH_API_KEY": "test-doubao-key",
                }

                # 2. Empty table list: text shows Stage 2 budget + copyable Agent tip; JSON keeps new flat schema
                rc, out, err = _run_cli(["hotwords", "list", "--config", str(cfg_path)], env)
                self.assertEqual(rc, 0, err)
                self.assertIn("Stage 2 / Single-hop wide vocabulary budget", out)
                self.assertIn("stage2: 0/200 items, 0/3000 chars [OK]", out)
                self.assertIn("Agent tip:", out)
                self.assertIn("~/.agent-voice-input/hotwords/personal.txt", out)

                rc, out, err = _run_cli(["hotwords", "list", "--json", "--config", str(cfg_path)], env)
                self.assertEqual(rc, 0, err)
                empty_js = json.loads(out)
                self.assertFalse(empty_js["exists"])
                self.assertEqual(empty_js["entries"], [])
                self.assertEqual(empty_js["terms"], [])
                self.assertEqual(empty_js["counts"]["total"], 0)
                self.assertEqual(empty_js["stage2_budget"]["max_items"], 200)
                self.assertEqual(empty_js["stage2_budget"]["max_chars"], 3000)
                self.assertFalse(empty_js["stage2_budget"]["truncated"])
                self.assertNotIn("scopes", empty_js)

                # Legacy --scope flag is completely removed
                with mock.patch("sys.stderr", io.StringIO()):
                    with self.assertRaises(SystemExit) as cm_scope:
                        parser.parse_args(["hotwords", "list", "--scope", "global"])
                self.assertEqual(cm_scope.exception.code, 2)

                # 3. Fresh add with comma-separated & repeatable --alias
                rc, out, err = _run_cli(
                    [
                        "hotwords",
                        "add",
                        "Harness",
                        "--alias",
                        "哈尼斯,哈尼思",
                        "--alias",
                        "哈尼丝",
                        "--config",
                        str(cfg_path),
                    ],
                    env,
                )
                self.assertEqual(rc, 0, err)
                self.assertIn("Added (1):   Harness <- 哈尼斯, 哈尼思, 哈尼丝", out)
                self.assertNotIn("Warning:", out)

                # 4. In-place merge (text + JSON) & duplicate skip
                rc, out, err = _run_cli(
                    ["hotwords", "add", "harness", "--alias", "哈尼四", "--config", str(cfg_path)],
                    env,
                )
                self.assertEqual(rc, 0, err)
                self.assertIn("Updated (1): Harness (+aliases: 哈尼四)", out)

                rc, out, err = _run_cli(
                    [
                        "hotwords",
                        "add",
                        "harness",
                        "--alias",
                        "哈尼五",
                        "--json",
                        "--config",
                        str(cfg_path),
                    ],
                    env,
                )
                self.assertEqual(rc, 0, err)
                upd_js = json.loads(out)
                self.assertTrue(upd_js["changed"])
                self.assertEqual(upd_js["added"], [])
                self.assertEqual(upd_js["updated"], ["Harness"])
                self.assertEqual(upd_js["aliases_added"], ["哈尼五"])
                self.assertEqual(upd_js["warnings"], [])

                rc, out, err = _run_cli(
                    [
                        "hotwords",
                        "add",
                        "Harness",
                        "--alias",
                        "哈尼斯",
                        "--json",
                        "--config",
                        str(cfg_path),
                    ],
                    env,
                )
                self.assertEqual(rc, 0, err)
                dup_js = json.loads(out)
                self.assertFalse(dup_js["changed"])
                self.assertEqual(dup_js["skipped"], ["Harness"])

                # 5. 2-character alias emits non-blocking Warning in text & JSON (with --pin support)
                rc, out, err = _run_cli(
                    [
                        "hotwords",
                        "add",
                        "豆包",
                        "--pin",
                        "--alias",
                        "兜包",
                        "--config",
                        str(cfg_path),
                    ],
                    env,
                )
                self.assertEqual(rc, 0, err)
                self.assertIn("Added (1):   豆包 <- 兜包 (pinned)", out)
                self.assertIn("Warning:", out)
                self.assertIn("兜包", out)

                rc, out, err = _run_cli(
                    [
                        "hotwords",
                        "add",
                        "GrokBot",
                        "--alias",
                        "格克",
                        "--json",
                        "--config",
                        str(cfg_path),
                    ],
                    env,
                )
                self.assertEqual(rc, 0, err)
                warn_js = json.loads(out)
                self.assertEqual(warn_js["added"], ["GrokBot"])
                self.assertEqual(warn_js["aliases_added"], ["格克"])
                self.assertEqual(len(warn_js["warnings"]), 1)
                self.assertIn("格克", warn_js["warnings"][0])

                # 6. Guardrail rejections return exit code 2
                for bad_argv, expected_err in (
                    (
                        ["hotwords", "add", "TermA", "TermB", "--alias", "误识词", "--config", str(cfg_path)],
                        "--alias can only be used",
                    ),
                    (
                        ["hotwords", "add", "SingleChar", "--alias", "哈", "--config", str(cfg_path)],
                        "too short",
                    ),
                    (
                        ["hotwords", "add", "OtherTerm", "--alias", "Harness", "--config", str(cfg_path)],
                        "conflicts with an existing canonical hotword",
                    ),
                    (
                        ["hotwords", "add", "哈尼斯", "--config", str(cfg_path)],
                        "conflicts with existing mishearing alias",
                    ),
                    (
                        ["hotwords", "add", "Harness", "--alias", "哈尼六", "--config", str(cfg_path)],
                        "exceeds maximum of 5",
                    ),
                ):
                    rc, _out, err = _run_cli(bad_argv, env)
                    self.assertEqual(rc, 2, f"argv={bad_argv!r} stderr={err!r}")
                    self.assertIn(expected_err, err)

                # 7. Populated list: renders '!Term <- alias1, ...', Stage 1 100% & 70/30 sim, Stage 2 budget, JSON
                rc, out, err = _run_cli(["hotwords", "list", "--config", str(cfg_path)], env)
                self.assertEqual(rc, 0, err)
                self.assertIn("!豆包 <- 兜包", out)
                self.assertIn("Harness <- 哈尼丝, 哈尼五, 哈尼四, 哈尼思, 哈尼斯", out)
                self.assertIn("3 total, 1 pinned, 3 with aliases", out)
                self.assertIn("[general 100% personal]", out)
                self.assertIn("[agent 70%/30% sim]", out)
                self.assertIn("stage2: 3/200 items,", out)
                self.assertNotIn("Agent tip:", out)
                self.assertIn("README -> Hotwords", out)

                rc, out, err = _run_cli(["hotwords", "list", "--json", "--config", str(cfg_path)], env)
                self.assertEqual(rc, 0, err)
                pop_js = json.loads(out)
                self.assertEqual(pop_js["terms"], ["豆包", "GrokBot", "Harness"])
                self.assertEqual(pop_js["counts"], {"total": 3, "pinned": 1, "with_aliases": 3, "warnings": 0})
                harness_entry = [e for e in pop_js["entries"] if e["term"] == "Harness"][0]
                self.assertEqual(
                    harness_entry["aliases"],
                    ["哈尼丝", "哈尼五", "哈尼四", "哈尼思", "哈尼斯"],
                )
                self.assertFalse(harness_entry["pinned"])
                doubao_entry = [e for e in pop_js["entries"] if e["term"] == "豆包"][0]
                self.assertTrue(doubao_entry["pinned"])
                self.assertEqual(pop_js["stage2_budget"]["kept_count"], 3)
                self.assertIn("active_general", pop_js["stage1_budgets"])
                self.assertIn("active_agent_70_30", pop_js["stage1_budgets"])
                self.assertEqual(pop_js["stage1_budgets"]["active_agent_70_30"]["dynamic_reserve_terms"], 15)

                # 8. Stage 2 truncation surfaces in both text and JSON when >200 items
                overflow_terms = [f"Stage2Term{i:03d}" for i in range(205)]
                rc, _out, err = _run_cli(
                    ["hotwords", "add", *overflow_terms, "--config", str(cfg_path)],
                    env,
                )
                self.assertEqual(rc, 0, err)
                rc, out, err = _run_cli(["hotwords", "list", "--config", str(cfg_path)], env)
                self.assertEqual(rc, 0, err)
                self.assertIn("stage2: 200/200 items,", out)
                self.assertIn("[TRUNCATED: 8 dropped:", out)

                rc, out, err = _run_cli(["hotwords", "list", "--json", "--config", str(cfg_path)], env)
                self.assertEqual(rc, 0, err)
                trunc_js = json.loads(out)
                self.assertTrue(trunc_js["stage2_budget"]["truncated"])
                self.assertEqual(trunc_js["stage2_budget"]["dropped_count"], 8)
        finally:
            clear_hotwords_cache()

    def test_step4_init_symlink_home_external_config_gitignore_and_migration_hint(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            # 1. Whole app_home symlinked (~/.agent-voice-input -> real_avi with 0755): init succeeds without chmodding target!
            home1 = root / "home1"
            home1.mkdir()
            real_avi = root / "real_avi"
            real_avi.mkdir(mode=0o755)
            os.chmod(real_avi, 0o755)
            avi_link = home1 / ".agent-voice-input"
            avi_link.symlink_to(real_avi)

            # Seed legacy XDG config to verify one-time migration hint before writing config.toml
            legacy_dir = home1 / ".config" / "agent-voice-input"
            legacy_dir.mkdir(parents=True)
            (legacy_dir / "agent-voice-input.env").write_text("# legacy\n", encoding="utf-8")

            env1 = {"HOME": str(home1), "PATH": ""}
            out1 = io.StringIO()
            with (
                mock.patch.dict(os.environ, env1, clear=True),
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", out1),
            ):
                rc1 = cli_mod.main(["init"])
            self.assertEqual(rc1, 0, out1.getvalue())
            self.assertTrue(avi_link.is_symlink())
            self.assertEqual(stat.S_IMODE(real_avi.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE((real_avi / "config.toml").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((real_avi / ".env").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((real_avi / ".gitignore").stat().st_mode), 0o644)
            self.assertEqual((real_avi / ".gitignore").read_text(encoding="utf-8"), "/*\n!/.gitignore\n!/hotwords/\n")
            self.assertIn("Migrate   legacy config found in", out1.getvalue())

            # Second init: migration hint disappears because <app_home>/config.toml now exists;
            # passing --config <real_avi>/config.toml matches app_home by realpath!
            (real_avi / ".gitignore").write_text("# user custom\n", encoding="utf-8")
            out1b = io.StringIO()
            with (
                mock.patch.dict(os.environ, env1, clear=True),
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", out1b),
            ):
                rc1b = cli_mod.main(["init", "--force", "--config", str(real_avi / "config.toml")])
            self.assertEqual(rc1b, 0, out1b.getvalue())
            self.assertNotIn("Migrate", out1b.getvalue())
            self.assertIn("Gitignore kept ", out1b.getvalue())
            self.assertEqual((real_avi / ".gitignore").read_text(encoding="utf-8"), "# user custom\n")

            # 2. Reject --config pointing to .env or .gitignore (both inside app_home and external dir)
            ext_dir = root / "ext_cfg"
            ext_dir.mkdir()
            (ext_dir / ".env").write_text("KEEP_ME=1\n", encoding="utf-8")
            (ext_dir / ".gitignore").write_text("KEEP_GI\n", encoding="utf-8")
            for bad_target in (
                avi_link / ".env",
                avi_link / ".gitignore",
                ext_dir / ".env",
                ext_dir / ".gitignore",
            ):
                err_bad = io.StringIO()
                with (
                    mock.patch.dict(os.environ, env1, clear=True),
                    mock.patch("sys.stderr", err_bad),
                ):
                    rc_bad = cli_mod.main(["init", "--force", "--config", str(bad_target)])
                self.assertEqual(rc_bad, 2, msg=f"target={bad_target} stderr={err_bad.getvalue()}")
                self.assertIn("config error:", err_bad.getvalue())
            self.assertEqual((ext_dir / ".env").read_text(encoding="utf-8"), "KEEP_ME=1\n")
            self.assertEqual((ext_dir / ".gitignore").read_text(encoding="utf-8"), "KEEP_GI\n")

            # 3. External --config writes ONLY TOML, never .gitignore or .env, and points user to <app_home>/.env
            ext_clean = root / "ext_clean"
            ext_clean.mkdir(mode=0o755)
            os.chmod(ext_clean, 0o755)
            ext_toml = ext_clean / "custom.toml"
            out_ext = io.StringIO()
            with (
                mock.patch.dict(os.environ, env1, clear=True),
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", out_ext),
            ):
                rc_ext = cli_mod.main(["init", "--config", str(ext_toml)])
            self.assertEqual(rc_ext, 0, out_ext.getvalue())
            self.assertTrue(ext_toml.is_file())
            self.assertEqual(stat.S_IMODE(ext_toml.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(ext_clean.stat().st_mode), 0o755)
            self.assertFalse((ext_clean / ".env").exists())
            self.assertFalse((ext_clean / ".gitignore").exists())
            self.assertIn("skipped in external --config directory", out_ext.getvalue())
            self.assertIn(str(avi_link / ".env"), out_ext.getvalue())

    def test_step4_hotwords_path_symlink_pin_diagnostics_and_doctor_privacy(self) -> None:
        from agent_voice_input.hotwords import clear_hotwords_cache

        def _run_cli(argv: list[str], env: dict[str, str]) -> tuple[int, str, str]:
            out_buf, err_buf = io.StringIO(), io.StringIO()
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("sys.stdout", out_buf),
                mock.patch("sys.stderr", err_buf),
            ):
                rc = cli_mod.main(argv)
            return rc, out_buf.getvalue(), err_buf.getvalue()

        clear_hotwords_cache()
        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                home = root / "home"
                home.mkdir()
                rt = root / "rt"
                rt.mkdir(mode=0o700)
                app_home = home / ".agent-voice-input"
                app_home.mkdir(mode=0o700)

                # Symlink hotwords/ to external harness directory
                harness_hw = root / "harness_hotwords"
                harness_hw.mkdir(mode=0o755)
                os.chmod(harness_hw, 0o755)
                (app_home / "hotwords").symlink_to(harness_hw)

                # Legacy XDG file triggers [migrate] in doctor (because config.toml not yet created)
                legacy_dir = home / ".config" / "agent-voice-input"
                legacy_dir.mkdir(parents=True)
                (legacy_dir / "hotwords.txt").write_text("OldTerm\n", encoding="utf-8")

                env = {
                    "HOME": str(home),
                    "PATH": "",
                    "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
                    "DASHSCOPE_API_KEY": "sk-dash-step4-test-1234",
                }

                # 1. `hotwords path` & `--json` surface personal.txt path + hotwords/ symlink realpath
                rc, out_p, err_p = _run_cli(["hotwords", "path"], env)
                self.assertEqual(rc, 0, err_p)
                self.assertIn(str(app_home / "hotwords" / "personal.txt"), out_p)
                self.assertIn("hotwords/ symlink ->", out_p)
                self.assertIn(os.path.realpath(str(harness_hw)), out_p)

                rc, out_pj, err_pj = _run_cli(["hotwords", "path", "--json"], env)
                self.assertEqual(rc, 0, err_pj)
                pj = json.loads(out_pj)
                self.assertEqual(pj["path"], str(app_home / "hotwords" / "personal.txt"))
                self.assertTrue(pj["dir_is_symlink"])
                self.assertEqual(pj["dir_realpath"], os.path.realpath(str(harness_hw)))

                # 2. `hotwords add` + in-place `--pin` upgrade + `remove` (removes all .lower() dups, keeps comments/CRLF)
                personal = harness_hw / "personal.txt"
                personal.write_bytes(b"# my comment\r\nAlpha\r\nalpha\r\nBeta\r\n")
                rc, out_up, err_up = _run_cli(["hotwords", "add", "beta", "--pin"], env)
                self.assertEqual(rc, 0, err_up)
                self.assertIn("Updated (1): Beta (+pinned)", out_up)
                self.assertIn(b"!Beta\r\n", personal.read_bytes())

                rc, out_rm, err_rm = _run_cli(["hotwords", "remove", "ALPHA", "--json"], env)
                self.assertEqual(rc, 0, err_rm)
                rm_js = json.loads(out_rm)
                self.assertTrue(rm_js["changed"])
                self.assertEqual(rm_js["removed"], ["Alpha"])
                raw_after_rm = personal.read_bytes()
                self.assertIn(b"# my comment\r\n", raw_after_rm)
                self.assertNotIn(b"Alpha", raw_after_rm)
                self.assertNotIn(b"alpha", raw_after_rm)
                self.assertIn(b"!Beta\r\n", raw_after_rm)
                # Ensure directory-fd flock left zero .lock files in symlinked harness_hw
                self.assertEqual([p.name for p in harness_hw.iterdir()], ["personal.txt"])

                # 3. Diagnostics in `hotwords list` AND zero-text-leak `doctor --json` + `[migrate]` + `[hotwords]`
                secret_lines = [
                    "[coding]",
                    "UltraSecretVocabXYZ <- 共享秘词",
                    "AnotherSecretTerm <- 共享秘词",
                    "Bad<Xml>Line",
                    "<<<<<<< HEAD",
                    "ConflictedSecretTerm",
                    "=======",
                    "OtherConflictedSecret",
                    ">>>>>>> main",
                    *[f"SecTerm{i:03d}" for i in range(205)],
                ]
                personal.write_text("\n".join(secret_lines) + "\n", encoding="utf-8")

                rc, out_list, err_list = _run_cli(["hotwords", "list"], env)
                self.assertEqual(rc, 0, err_list)
                self.assertIn("Hotwords dir:", out_list)
                self.assertIn("[Diagnostics]", out_list)
                self.assertIn("legacy section header", out_list)
                self.assertIn("ambiguous alias", out_list)
                self.assertIn("conflict block", out_list)
                self.assertIn("invalid line", out_list)
                self.assertIn("TRUNCATED:", out_list)

                rc, out_lj, err_lj = _run_cli(["hotwords", "list", "--json"], env)
                self.assertEqual(rc, 0, err_lj)
                lj = json.loads(out_lj)
                self.assertEqual(lj["diagnostics"]["legacy_sections"], ["[coding]"])
                self.assertEqual(len(lj["diagnostics"]["ambiguous_aliases"]), 1)
                self.assertEqual(lj["diagnostics"]["conflict_lines"], [5])
                self.assertEqual(len(lj["diagnostics"]["invalid_lines"]), 1)
                self.assertTrue(lj["stage2_budget"]["truncated"])

                # Doctor text & JSON: WARN does not fail rc=0; shows [migrate] + [hotwords]; ZERO secret term text in JSON!
                cfg, eff_env = load_config(env=env)
                out_doc_txt = io.StringIO()
                with (
                    mock.patch.object(cli_mod.sys, "platform", "linux"),
                    mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                    mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                    mock.patch("sys.stdout", out_doc_txt),
                ):
                    rc_dt = cli_mod._cmd_doctor(cfg, eff_env, as_json=False)
                self.assertEqual(rc_dt, 0, out_doc_txt.getvalue())
                dt = out_doc_txt.getvalue()
                self.assertIn("[migrate]   INFO legacy config found in", dt)
                self.assertIn("[hotwords]  WARN ", dt)
                self.assertIn("symlink ->", dt)
                self.assertIn("git conflict block(s)", dt)
                self.assertIn("stage2 overflow", dt)

                out_doc_js = io.StringIO()
                with (
                    mock.patch.object(cli_mod.sys, "platform", "linux"),
                    mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                    mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                    mock.patch("sys.stdout", out_doc_js),
                ):
                    rc_dj = cli_mod._cmd_doctor(cfg, eff_env, as_json=True)
                self.assertEqual(rc_dj, 0)
                raw_doc_json = out_doc_js.getvalue()
                doc_obj = json.loads(raw_doc_json)
                self.assertTrue(doc_obj["ok"])
                self.assertIn("legacy config found", doc_obj["migration_hint"])
                hw_rep = doc_obj["hotwords"]
                self.assertTrue(hw_rep["exists"])
                self.assertTrue(hw_rep["dir_is_symlink"])
                self.assertEqual(
                    set(hw_rep["counts"].keys()),
                    {
                        "total",
                        "kept",
                        "dropped_count",
                        "stage2_kept",
                        "stage2_dropped",
                        "used_chars",
                        "max_items",
                        "max_chars",
                        "warnings_count",
                    },
                )
                self.assertEqual(hw_rep["counts"]["kept"], 200)
                self.assertEqual(hw_rep["counts"]["dropped_count"], 7)
                self.assertEqual(hw_rep["counts"]["stage2_kept"], 200)
                self.assertEqual(hw_rep["counts"]["stage2_dropped"], 7)
                self.assertEqual(hw_rep["issues"]["conflict_lines"], [5])
                self.assertEqual(hw_rep["issues"]["ambiguous_aliases_count"], 1)
                self.assertEqual(hw_rep["issues"]["legacy_sections_count"], 1)
                self.assertEqual(hw_rep["issues"]["invalid_lines_count"], 1)
                self.assertTrue(hw_rep["issues"]["stage2_truncated"])
                # Privacy invariant: ZERO hotword terms / aliases / dropped words in entire doctor --json output!
                for forbidden in (
                    "UltraSecretVocabXYZ",
                    "AnotherSecretTerm",
                    "共享秘词",
                    "ConflictedSecretTerm",
                    "SecTerm",
                    "Bad<Xml>Line",
                ):
                    self.assertNotIn(forbidden, raw_doc_json)

                # Healthy clean personal.txt -> [hotwords] OK (no WARN)
                personal.write_text("!Codex\nGhostty\n", encoding="utf-8")
                out_doc_ok = io.StringIO()
                with (
                    mock.patch.object(cli_mod.sys, "platform", "linux"),
                    mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                    mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                    mock.patch("sys.stdout", out_doc_ok),
                ):
                    rc_dok = cli_mod._cmd_doctor(cfg, eff_env, as_json=False)
                self.assertEqual(rc_dok, 0)
                self.assertIn("[hotwords]  OK ", out_doc_ok.getvalue())
                self.assertIn("total=2, stage2=2/200", out_doc_ok.getvalue())
        finally:
            clear_hotwords_cache()


if __name__ == "__main__":
    unittest.main()
