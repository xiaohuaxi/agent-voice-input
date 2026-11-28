"""Targeted unit tests for v0.18.1 hotwords, refiner priority/effort, OOBE, and pre-stop HUD fixes."""
from __future__ import annotations

import ast
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from agent_voice_input import __version__  # noqa: E402
from agent_voice_input import _entry as entry_mod  # noqa: E402
from agent_voice_input import cli as cli_mod  # noqa: E402
from agent_voice_input.config import (  # noqa: E402
    AudioConfig,
    AsrConfig,
    Config,
    DEFAULT_REFINER_EFFORTS,
    HotkeyConfig,
    REFINER_AUTO_PRIORITY,
    RefinerConfig,
    resolve_credentials,
    select_refiner_provider,
)
from agent_voice_input.daemon import VoiceInputDaemon  # noqa: E402
from agent_voice_input.daemon_pipeline import plan_commit_hud_calls  # noqa: E402
from agent_voice_input.hotwords import (  # noqa: E402
    DEFAULT_PERSONAL_HOTWORDS_TEMPLATE,
    DEFAULT_PERSONAL_HOTWORDS_TERMS,
    HOTWORDS_QUOTA_SPECS,
    budget_hotwords_for_provider,
    budget_stage2_hotwords,
    format_stage2_vocabulary_hints,
    load_hotwords_table,
    order_stage2_entries,
    parse_hotwords_text,
    project_stage1_hotwords,
    summarize_stage2_hotwords_budget,
)
from agent_voice_input.llm_common import AsrContext, AsrResult  # noqa: E402
from agent_voice_input.platform_common import ForegroundWindow, PasteOutcome  # noqa: E402
from agent_voice_input.platform_macos import classify_macos_mic_permission  # noqa: E402
from agent_voice_input.platform_wsl import probe_wsl_host  # noqa: E402
from agent_voice_input.session_ctx import SessionTracker  # noqa: E402


def _make_pcm_wav(*, duration_sec: float = 0.5, amp: int = 8000, sr: int = 16000) -> bytes:
    n = int(sr * duration_sec)
    samples = [int(amp * ((i % 20) - 10) / 10.0) for i in range(n)]
    pcm = struct.pack(f"<{n}h", *samples)
    hdr = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(pcm),
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        sr,
        sr * 2,
        2,
        16,
        b"data",
        len(pcm),
    )
    return hdr + pcm


class TestStage2HotwordsAndDoubaoV0181(unittest.TestCase):
    def test_stage2_over_200_mixed_cjk_english_preserves_file_order_without_az_line_jumping(self) -> None:
        lines: list[str] = []
        # Top plain Chinese + Z-starting English terms (150 entries)
        for i in range(60):
            lines.append(f"中文专名{i:03d}")
        for i in range(90):
            lines.append(f"ZetaTerm{i:03d}")
        # Mid plain entries (30 entries)
        for i in range(30):
            lines.append(f"MidTerm{i:03d}")
        # Bottom A-starting English entries (40 entries) that used to jump ahead of Chinese/Z terms
        for i in range(40):
            lines.append(f"AlphaBottom{i:03d}")
        # Effective aliased entries near bottom (should go to tier 1 in file order)
        lines.append("中文别名 <- 误听一")
        lines.append("ZebraAlias <- 斑马误听")
        # Pinned entries at very bottom (should go to tier 0 in file order)
        lines.append("!置顶中文词")
        lines.append("!ZebraPin")

        table = parse_hotwords_text("\n".join(lines) + "\n")
        ordered = order_stage2_entries(table.entries)
        kept, dropped, used_chars = budget_stage2_hotwords(ordered)
        summary = summarize_stage2_hotwords_budget(table)
        block = format_stage2_vocabulary_hints(ordered)

        self.assertEqual(len(kept), 200)
        self.assertEqual(len(dropped), 24)
        self.assertEqual(summary.kept_count, 200)
        self.assertEqual(summary.dropped_count, 24)
        self.assertEqual(summary.used_chars, used_chars)

        kept_terms = [e.term for e in kept]
        dropped_terms = [e.term for e in dropped]

        # Tier 0 (pinned) preserves file order
        self.assertEqual(kept_terms[:2], ["置顶中文词", "ZebraPin"])
        # Tier 1 (effective aliases) preserves file order
        self.assertEqual(kept_terms[2:4], ["中文别名", "ZebraAlias"])
        # Tier 2 (plain) keeps ALL 60 top Chinese + 90 ZetaTerm + 30 MidTerm + first 16 AlphaBottom in file order
        self.assertEqual(kept_terms[4:64], [f"中文专名{i:03d}" for i in range(60)])
        self.assertEqual(kept_terms[64:154], [f"ZetaTerm{i:03d}" for i in range(90)])
        self.assertEqual(kept_terms[154:184], [f"MidTerm{i:03d}" for i in range(30)])
        self.assertEqual(kept_terms[184:200], [f"AlphaBottom{i:03d}" for i in range(16)])
        # Bottom 24 AlphaBottom terms are dropped (zero A-Z line jumping!)
        self.assertEqual(dropped_terms, [f"AlphaBottom{i:03d}" for i in range(16, 40)])
        self.assertEqual(list(summary.dropped), dropped_terms)

        # Rendered <vocabulary_hints> preserves the exact selected priority/file order
        body_lines = block.splitlines()[2:-1]
        self.assertEqual(body_lines[0], "置顶中文词")
        self.assertEqual(body_lines[1], "ZebraPin")
        self.assertTrue(body_lines[2].startswith("中文别名 (ASR has misheard as:"))
        self.assertTrue(body_lines[3].startswith("ZebraAlias (ASR has misheard as:"))
        self.assertEqual(body_lines[4], "中文专名000")
        self.assertEqual(body_lines[-1], "AlphaBottom015")

    def test_stage2_char_budget_truncates_under_200_items_and_skips_oversized_entry(self) -> None:
        # 50 entries each ~65 chars (with aliases) -> hits 3,000 chars before 200 items,
        # and still fits a trailing short term after skipping an oversized entry.
        lines = [f"LongTerm{i:02d} <- {'误听' * 6}{i:02d}" for i in range(70)]
        lines.append("ShortFit")
        table = parse_hotwords_text("\n".join(lines) + "\n")
        kept, dropped, used_chars = budget_stage2_hotwords(order_stage2_entries(table.entries))
        self.assertLess(len(kept), 200)
        self.assertLessEqual(used_chars, 3000)
        self.assertTrue(dropped)
        self.assertIn("ShortFit", [e.term for e in kept])

    def test_doubao_stage1_budget_50_terms_1000_chars(self) -> None:
        spec = HOTWORDS_QUOTA_SPECS["doubao"]
        self.assertEqual(spec.max_terms, 50)
        self.assertEqual(spec.max_chars, 1000)
        # 50 terms x 18 chars = 900 chars -> previously truncated at 500 chars (~27 terms), now all 50 fit!
        terms = [f"LongIdentifier_{i:02d}X" for i in range(55)]
        res = budget_hotwords_for_provider(terms, "doubao")
        self.assertEqual(res.kept_count, 50)
        self.assertEqual(res.used_chars, 900)
        self.assertEqual(res.dropped_count, 5)


class TestStarterHotwordsInitAndGeneralModeV0181(unittest.TestCase):
    def test_init_creates_starter_personal_hotwords_and_never_overwrites_even_with_force(self) -> None:
        self.assertGreaterEqual(len(DEFAULT_PERSONAL_HOTWORDS_TERMS), 15)
        self.assertLessEqual(len(DEFAULT_PERSONAL_HOTWORDS_TERMS), 20)
        for forbidden in ("Cursor", "Copilot", "Go", "Flash", "PR", "API"):
            self.assertNotIn(forbidden, DEFAULT_PERSONAL_HOTWORDS_TERMS)

        parsed_tpl = parse_hotwords_text(DEFAULT_PERSONAL_HOTWORDS_TEMPLATE)
        self.assertEqual(parsed_tpl.warnings_count, 0)
        self.assertEqual(parsed_tpl.terms, DEFAULT_PERSONAL_HOTWORDS_TERMS)

        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            env = {"HOME": str(home), "PATH": ""}
            out1 = io.StringIO()
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", out1),
            ):
                self.assertEqual(cli_mod.main(["init"]), 0)

            hw_path = home / ".agent-voice-input" / "hotwords" / "personal.txt"
            self.assertTrue(hw_path.is_file())
            self.assertEqual(os.stat(hw_path).st_mode & 0o777, 0o600)
            self.assertIn("Hotwords  created", out1.getvalue())
            loaded = load_hotwords_table(hw_path, env=env, use_cache=False)
            self.assertEqual(loaded.terms, DEFAULT_PERSONAL_HOTWORDS_TERMS)

            # Customize personal.txt, then run `init` and `init --force` -> MUST NEVER overwrite personal.txt!
            custom_content = "# my custom terms\n!MyCustomBrand\n"
            hw_path.write_text(custom_content, encoding="utf-8")

            for argv in (["init"], ["init", "--force"]):
                out_keep = io.StringIO()
                with (
                    mock.patch.dict(os.environ, env, clear=True),
                    mock.patch.object(cli_mod.sys, "platform", "linux"),
                    mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                    mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                    mock.patch("sys.stdout", out_keep),
                ):
                    self.assertEqual(cli_mod.main(argv), 0)
                self.assertEqual(hw_path.read_text(encoding="utf-8"), custom_content)
                self.assertIn("Hotwords  kept", out_keep.getvalue())

            # Runtime general mode MUST NOT inject any invisible built-in hotwords when personal_hotwords is empty
            ctx_gen = AsrContext(
                mode="general",
                personal_hotwords=(),
                dynamic_hotwords=("DynamicTerm",),
                builtin_hotwords=("Claude Code", "Codex"),
                hotwords_bucketed=True,
            )
            proj = project_stage1_hotwords(ctx_gen, "dashscope")
            self.assertEqual(proj.kept, ())


class TestRefinerPriorityAndEffortV0181(unittest.TestCase):
    def test_openrouter_above_deepseek_and_dashscope_while_openai_gemini_stay_below_dashscope(self) -> None:
        from agent_voice_input.config import DEFAULT_ASR_MODELS

        self.assertEqual(DEFAULT_ASR_MODELS["openrouter"], "microsoft/mai-transcribe-2")
        self.assertEqual(HOTWORDS_QUOTA_SPECS["dashscope"].max_terms, 250)
        self.assertEqual(HOTWORDS_QUOTA_SPECS["dashscope"].max_chars, 2800)

        self.assertLess(
            REFINER_AUTO_PRIORITY.index("openrouter"),
            REFINER_AUTO_PRIORITY.index("deepseek"),
        )
        self.assertLess(
            REFINER_AUTO_PRIORITY.index("openrouter"),
            REFINER_AUTO_PRIORITY.index("dashscope"),
        )
        self.assertLess(
            REFINER_AUTO_PRIORITY.index("deepseek"),
            REFINER_AUTO_PRIORITY.index("dashscope"),
        )
        self.assertLess(
            REFINER_AUTO_PRIORITY.index("dashscope"),
            REFINER_AUTO_PRIORITY.index("gemini"),
        )
        self.assertLess(
            REFINER_AUTO_PRIORITY.index("dashscope"),
            REFINER_AUTO_PRIORITY.index("openai"),
        )

        with tempfile.TemporaryDirectory() as td:
            base = {"HOME": td, "PATH": ""}
            cfg = Config()
            env_or_ds_dk = {
                **base,
                "OPENROUTER_API_KEY": "sk-or-test-1",
                "DEEPSEEK_API_KEY": "sk-dk-test-1",
                "DASHSCOPE_API_KEY": "sk-ds-test-1",
            }
            self.assertEqual(
                select_refiner_provider(cfg, resolve_credentials(cfg, env_or_ds_dk), env_or_ds_dk),
                "openrouter",
            )

            env_ds_oai_gem = {
                **base,
                "DASHSCOPE_API_KEY": "sk-ds-test-1",
                "OPENAI_API_KEY": "sk-oai-test-1",
                "GEMINI_API_KEY": "AIza-gem-test-1",
            }
            self.assertEqual(
                select_refiner_provider(cfg, resolve_credentials(cfg, env_ds_oai_gem), env_ds_oai_gem),
                "dashscope",
            )

            self.assertEqual(DEFAULT_REFINER_EFFORTS["openrouter"], "low")
            self.assertEqual(RefinerConfig(provider="openrouter").effective_effort("openrouter"), "low")
            self.assertEqual(RefinerConfig(provider="auto").effective_effort("openrouter"), "low")


class TestOobeFixesV0181(unittest.TestCase):
    def test_bin_wrapper_python_version_guard_rejects_lt_310(self) -> None:
        wrapper_path = _REPO_ROOT / "bin" / "agent-voice-input"
        code = (
            "import sys, runpy\n"
            "class _V(tuple):\n"
            "    major, minor, micro = 3, 9, 6\n"
            "sys.version_info = _V((3, 9, 6, 'final', 0))\n"
            f"runpy.run_path({str(wrapper_path)!r}, run_name='__main__')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("Python 3.10+", proc.stderr)
        self.assertIn("3.9.6", proc.stderr)
        self.assertIn("uv tool install", proc.stderr)

        # Console script entrypoint (agent_voice_input._entry:main) uses legacy-safe syntax & enforces <3.10 guard
        entry_src = (_REPO_ROOT / "src" / "agent_voice_input" / "_entry.py").read_text(encoding="utf-8")
        ast.parse(entry_src, feature_version=(3, 6))

        class _FakeV(tuple):
            major, minor, micro = 3, 9, 6

        err_entry = io.StringIO()
        with (
            mock.patch.object(entry_mod.sys, "version_info", _FakeV((3, 9, 6, "final", 0))),
            mock.patch("sys.stderr", err_entry),
        ):
            rc_entry = entry_mod.main(["doctor"])
        self.assertEqual(rc_entry, 1)
        self.assertIn("Python 3.10+", err_entry.getvalue())
        self.assertIn("3.9.6", err_entry.getvalue())

        # Supported Python 3.10+ delegates cleanly from _entry.main to cli.main
        out_entry_ver = io.StringIO()
        with mock.patch("sys.stdout", out_entry_ver):
            with self.assertRaises(SystemExit) as cm_ver:
                entry_mod.main(["--version"])
        self.assertEqual(cm_ver.exception.code, 0)
        self.assertIn(__version__, out_entry_ver.getvalue())

        # Direct cli.main() still enforces the same <3.10 guard for compatibility
        err_cli = io.StringIO()
        with (
            mock.patch.object(cli_mod.sys, "version_info", _FakeV((3, 9, 6, "final", 0))),
            mock.patch("sys.stderr", err_cli),
        ):
            rc_cli = cli_mod.main(["doctor"])
        self.assertEqual(rc_cli, 1)
        self.assertIn("Python 3.10+", err_cli.getvalue())
        self.assertIn("3.9.6", err_cli.getvalue())

        # External symlink install (e.g. ~/.local/bin/agent-voice-input -> repo/bin/agent-voice-input)
        # MUST resolve src via realpath even with PYTHONPATH cleared.
        with tempfile.TemporaryDirectory() as td_sym:
            sym_bin = Path(td_sym) / "agent-voice-input"
            sym_bin.symlink_to(wrapper_path)
            clean_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
            p_sym_ok = subprocess.run(
                [sys.executable, str(sym_bin), "--version"],
                env=clean_env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(p_sym_ok.returncode, 0, p_sym_ok.stderr)
            self.assertIn(__version__, p_sym_ok.stdout)

            # When host /usr/bin/python3 is <3.10 (e.g. macOS 3.9.6), verify wrapper symlink & _entry fail cleanly
            sys_py = Path("/usr/bin/python3")
            if sys_py.is_file():
                ver_probe = subprocess.run(
                    [str(sys_py), "-c", "import sys; print(sys.version_info < (3, 10))"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if ver_probe.returncode == 0 and ver_probe.stdout.strip() == "True":
                    p_bin = subprocess.run(
                        [str(sys_py), str(sym_bin), "--version"],
                        env=clean_env,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(p_bin.returncode, 1)
                    self.assertIn("Python 3.10+", p_bin.stderr)
                    self.assertNotIn("Traceback", p_bin.stderr)

                    env_pp = dict(clean_env)
                    env_pp["PYTHONPATH"] = str(_REPO_ROOT / "src")
                    p_mod = subprocess.run(
                        [
                            str(sys_py),
                            "-c",
                            "from agent_voice_input._entry import main; raise SystemExit(main(['doctor']))",
                        ],
                        env=env_pp,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(p_mod.returncode, 1)
                    self.assertIn("Python 3.10+", p_mod.stderr)
                    self.assertNotIn("TypeError", p_mod.stderr)
                    self.assertNotIn("Traceback", p_mod.stderr)

    def test_dotenv_placeholder_detection_is_precise_and_never_scans_process_env(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            rt = Path(td) / "rt"
            app_dir = home / ".agent-voice-input"
            app_dir.mkdir(parents=True, mode=0o700)
            env_file = app_dir / ".env"

            # 1. Exact template placeholders in .env trigger FAIL without leaking secret text
            env_file.write_text(
                'DASHSCOPE_API_KEY="sk-..."\nOPENAI_API_KEY="<your-openai-api-key>"\n',
                encoding="utf-8",
            )
            from agent_voice_input.config import load_config

            cfg, merged_env = load_config(
                env={"HOME": str(home), "PATH": "", "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)},
                home_dir=home,
            )
            buf = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", buf),
            ):
                rc = cli_mod._cmd_doctor(cfg, merged_env)
            out = buf.getvalue()
            self.assertEqual(rc, 1)
            self.assertIn("[asr]       selected=dashscope", out)
            self.assertIn("-> FAIL (PLACEHOLDER KEY)", out)
            self.assertIn("[refiner]   FAIL (PLACEHOLDER KEY) auto -> dashscope", out)
            self.assertIn("dashscope=FAIL (placeholder in .env)", out)
            self.assertIn("openai=FAIL (placeholder in .env)", out)
            self.assertNotIn("dashscope=OK (via", out)
            self.assertNotIn("openai=OK (via", out)
            self.assertIn("[keys]      FAIL DASHSCOPE_API_KEY", out)
            self.assertIn("[keys]      FAIL OPENAI_API_KEY", out)
            self.assertNotIn("sk-...", out)
            self.assertNotIn("<your-openai-api-key>", out)

            # 1b. JSON report & --ping MUST agree: ready=false for ASR/refiner and ZERO outbound ping requests
            ping_calls: list[str] = []

            def _forbidden_ping_tx(req: object) -> object:
                ping_calls.append(getattr(req, "url", str(req)))
                raise AssertionError("doctor --ping must not send requests with placeholder credentials")

            buf_json = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_json),
            ):
                rc_json = cli_mod._cmd_doctor(
                    cfg, merged_env, ping=True, as_json=True, transport=_forbidden_ping_tx  # type: ignore[arg-type]
                )
            self.assertEqual(rc_json, 1)
            self.assertEqual(ping_calls, [])
            raw_json = buf_json.getvalue()
            self.assertNotIn("sk-...", raw_json)
            self.assertNotIn("<your-openai-api-key>", raw_json)
            doc = json.loads(raw_json)
            self.assertFalse(doc["ok"])
            self.assertFalse(doc["asr"]["ready"])
            self.assertFalse(doc["pipeline"]["asr"]["ready"])
            self.assertFalse(doc["pipeline"]["refiner"]["ready"])
            self.assertEqual(doc["ping"], {})

            # 2. Process env override with real key clears placeholder FAIL for that key
            cfg2, merged_env2 = load_config(
                env={
                    "HOME": str(home),
                    "PATH": "",
                    "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
                    "DASHSCOPE_API_KEY": "sk-real-dashscope-value-1234",
                    "OPENAI_API_KEY": "sk-real-openai-value-1234",
                },
                home_dir=home,
            )
            buf2 = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", buf2),
            ):
                rc2 = cli_mod._cmd_doctor(cfg2, merged_env2)
            self.assertEqual(rc2, 0, buf2.getvalue())

            # 3. Placeholder-like value ONLY in global process env (absent from .env) is NEVER flagged
            env_file.unlink()
            cfg3, merged_env3 = load_config(
                env={
                    "HOME": str(home),
                    "PATH": "",
                    "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
                    "DASHSCOPE_API_KEY": "sk-...",
                },
                home_dir=home,
            )
            buf3 = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", buf3),
            ):
                rc3 = cli_mod._cmd_doctor(cfg3, merged_env3)
            self.assertEqual(rc3, 0, buf3.getvalue())
            self.assertNotIn("still uses a template placeholder", buf3.getvalue())

    def test_doctor_placeholder_alignment_doubao_custom_key_and_codex_auth_source(self) -> None:
        from agent_voice_input.config import load_config
        from agent_voice_input.http_transport import HttpResponse

        # 1. Doubao combined auth: custom asr.api_key_env is real, but DOUBAO_ASR_APP_ID in .env is placeholder
        #    -> MUST mark ASR FAIL (PLACEHOLDER KEY), ready=false, skip --ping, and label app_id as FAIL in [keys].
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            rt = Path(td) / "rt"
            app_dir = home / ".agent-voice-input"
            app_dir.mkdir(parents=True, mode=0o700)
            (app_dir / "config.toml").write_text(
                '[asr]\nprovider = "doubao"\napi_key_env = "MY_CUSTOM_DOUBAO_KEY"\n'
                '[refiner]\nprovider = "off"\n',
                encoding="utf-8",
            )
            (app_dir / ".env").write_text(
                'MY_CUSTOM_DOUBAO_KEY="real-custom-doubao-token-999"\n'
                'DOUBAO_ASR_APP_ID="<your-doubao-app-id>"\n',
                encoding="utf-8",
            )
            cfg, merged_env = load_config(
                env={"HOME": str(home), "PATH": "", "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)},
                home_dir=home,
            )
            ping_calls: list[str] = []

            def _forbidden_ping_tx(req: object) -> object:
                ping_calls.append(getattr(req, "url", str(req)))
                raise AssertionError("Doubao placeholder DOUBAO_ASR_APP_ID must skip --ping")

            buf_txt = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_txt),
            ):
                rc_txt = cli_mod._cmd_doctor(
                    cfg, merged_env, ping=True, transport=_forbidden_ping_tx  # type: ignore[arg-type]
                )
            out_txt = buf_txt.getvalue()
            self.assertEqual(rc_txt, 1)
            self.assertEqual(ping_calls, [])
            self.assertIn("[asr]       selected=doubao", out_txt)
            self.assertIn("-> FAIL (PLACEHOLDER KEY)", out_txt)
            self.assertIn(
                "doubao=OK (via MY_CUSTOM_DOUBAO_KEY) (app_id=FAIL (placeholder in .env))",
                out_txt,
            )
            self.assertNotIn("<your-doubao-app-id>", out_txt)

            buf_json = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_json),
            ):
                rc_json = cli_mod._cmd_doctor(
                    cfg, merged_env, ping=True, as_json=True, transport=_forbidden_ping_tx  # type: ignore[arg-type]
                )
            self.assertEqual(rc_json, 1)
            doc = json.loads(buf_json.getvalue())
            self.assertFalse(doc["asr"]["ready"])
            self.assertFalse(doc["pipeline"]["asr"]["ready"])
            self.assertEqual(doc["ping"], {})

            # 1b. Same custom asr.api_key_env when default DOUBAO_SPEECH_API_KEY is ALSO set in env:
            #     custom key takes precedence (dual-header auth), so [keys] MUST NOT say ignored
            #     and MUST still flag DOUBAO_ASR_APP_ID as placeholder failure without leaking raw value.
            cfg_both, merged_env_both = load_config(
                env={
                    "HOME": str(home),
                    "PATH": "",
                    "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
                    "DOUBAO_SPEECH_API_KEY": "real-default-doubao-speech-key-777",
                },
                home_dir=home,
            )
            buf_both = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_both),
            ):
                rc_both = cli_mod._cmd_doctor(
                    cfg_both, merged_env_both, ping=True, transport=_forbidden_ping_tx  # type: ignore[arg-type]
                )
            out_both = buf_both.getvalue()
            self.assertEqual(rc_both, 1)
            self.assertEqual(ping_calls, [])
            self.assertIn("[asr]       selected=doubao", out_both)
            self.assertIn("-> FAIL (PLACEHOLDER KEY)", out_both)
            self.assertNotIn("ignored", out_both)
            self.assertIn(
                "doubao=OK (via MY_CUSTOM_DOUBAO_KEY) (app_id=FAIL (placeholder in .env))",
                out_both,
            )
            self.assertNotIn("doubao=OK (via DOUBAO_SPEECH_API_KEY)", out_both)
            self.assertIn("asr_custom=OK (via MY_CUSTOM_DOUBAO_KEY)", out_both)
            self.assertIn("[keys]      FAIL DOUBAO_ASR_APP_ID", out_both)
            self.assertNotIn("<your-doubao-app-id>", out_both)
            self.assertNotIn("real-default-doubao-speech-key-777", out_both)

            # 1c. Default DOUBAO_SPEECH_API_KEY WITHOUT custom asr.api_key_env keeps single-header ignored behavior.
            (app_dir / "config.toml").write_text(
                '[asr]\nprovider = "doubao"\n[refiner]\nprovider = "off"\n',
                encoding="utf-8",
            )
            (app_dir / ".env").write_text(
                'DOUBAO_SPEECH_API_KEY="real-default-doubao-speech-key-777"\n'
                'DOUBAO_ASR_APP_ID="legacy-app-id-555"\n',
                encoding="utf-8",
            )
            cfg_single, merged_env_single = load_config(
                env={"HOME": str(home), "PATH": "", "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)},
                home_dir=home,
            )
            buf_single = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_single),
            ):
                rc_single = cli_mod._cmd_doctor(cfg_single, merged_env_single)
            out_single = buf_single.getvalue()
            self.assertEqual(rc_single, 0, out_single)
            self.assertIn("[asr]       selected=doubao", out_single)
            self.assertIn("-> OK", out_single)
            self.assertIn("doubao=OK (via DOUBAO_SPEECH_API_KEY) (legacy app_id via DOUBAO_ASR_APP_ID ignored)", out_single)
            self.assertNotIn("real-default-doubao-speech-key-777", out_single)
            self.assertNotIn("legacy-app-id-555", out_single)

        # 2. Codex auth precedence:
        #    (a) auth.json has real OPENAI_API_KEY + tool .env has placeholder OPENAI_API_KEY
        #        -> Codex refiner uses auth.json (ready=true, OK, NOT flagged as placeholder, allowed to --ping).
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            rt = Path(td) / "rt"
            app_dir = home / ".agent-voice-input"
            app_dir.mkdir(parents=True, mode=0o700)
            codex_home = home / ".codex"
            codex_home.mkdir(parents=True, mode=0o700)
            (app_dir / "config.toml").write_text(
                '[asr]\nprovider = "dashscope"\n[refiner]\nprovider = "codex"\n',
                encoding="utf-8",
            )
            (app_dir / ".env").write_text(
                'DASHSCOPE_API_KEY="sk-real-dashscope-9999"\n'
                'OPENAI_API_KEY="<your-openai-api-key>"\n',
                encoding="utf-8",
            )
            (codex_home / "auth.json").write_text(
                json.dumps({"auth_mode": "api_key", "OPENAI_API_KEY": "sk-codex-file-real-key-8888"}),
                encoding="utf-8",
            )
            cfg_c1, env_c1 = load_config(
                env={
                    "HOME": str(home),
                    "CODEX_HOME": str(codex_home),
                    "PATH": "",
                    "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
                },
                home_dir=home,
            )
            ping_urls: list[str] = []

            def _ok_ping_tx(req: object) -> HttpResponse:
                ping_urls.append(str(getattr(req, "url", "")))
                return HttpResponse(status=200, headers={}, body=b'{"data":[]}')

            buf_c1 = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_c1),
            ):
                rc_c1 = cli_mod._cmd_doctor(
                    cfg_c1, env_c1, ping=True, transport=_ok_ping_tx  # type: ignore[arg-type]
                )
            out_c1 = buf_c1.getvalue()
            # Overall rc is 1 because .env still contains an unused placeholder OPENAI_API_KEY,
            # but Codex refiner itself MUST be OK via auth.json and MUST be pinged!
            self.assertEqual(rc_c1, 1)
            self.assertIn("[refiner]   OK codex", out_c1)
            self.assertIn("auth=api_key (auth.json)", out_c1)
            self.assertNotIn("[refiner]   FAIL (PLACEHOLDER KEY)", out_c1)
            self.assertIn("[ping]      refiner(codex):", out_c1)
            self.assertIn("openai=FAIL (placeholder in .env)", out_c1)
            self.assertGreaterEqual(len(ping_urls), 2)

            # (b) auth.json has auth_mode=api_key WITHOUT file key -> falls back to .env placeholder OPENAI_API_KEY
            #     -> Codex refiner MUST report FAIL (PLACEHOLDER KEY) and skip refiner --ping!
            (codex_home / "auth.json").write_text(
                json.dumps({"auth_mode": "api_key"}),
                encoding="utf-8",
            )
            cfg_c2, env_c2 = load_config(
                env={
                    "HOME": str(home),
                    "CODEX_HOME": str(codex_home),
                    "PATH": "",
                    "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
                },
                home_dir=home,
            )
            ping_urls_c2: list[str] = []

            def _ok_ping_tx_c2(req: object) -> HttpResponse:
                ping_urls_c2.append(str(getattr(req, "url", "")))
                return HttpResponse(status=200, headers={}, body=b'{"data":[]}')

            buf_c2 = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_c2),
            ):
                rc_c2 = cli_mod._cmd_doctor(
                    cfg_c2, env_c2, ping=True, transport=_ok_ping_tx_c2  # type: ignore[arg-type]
                )
            out_c2 = buf_c2.getvalue()
            self.assertEqual(rc_c2, 1)
            self.assertIn("[refiner]   FAIL (PLACEHOLDER KEY) codex", out_c2)
            self.assertNotIn("[ping]      refiner(codex):", out_c2)

    def test_config_error_hint_and_offline_status_ordering(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            rt = Path(td) / "rt"
            app_dir = home / ".agent-voice-input"
            app_dir.mkdir(parents=True, mode=0o700)
            (app_dir / "config.toml").write_text("[asr\nbroken = true\n", encoding="utf-8")
            env = {"HOME": str(home), "PATH": "", "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)}

            # doctor exits 2 with actionable init --force hint noting .env is preserved
            err_doc = io.StringIO()
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("sys.stderr", err_doc),
            ):
                rc_doc = cli_mod.main(["doctor"])
            self.assertEqual(rc_doc, 2)
            self.assertIn("config error:", err_doc.getvalue())
            self.assertIn("agent-voice-input init --force", err_doc.getvalue())
            self.assertIn(".env", err_doc.getvalue())

            # status offline puts Config WARN before Fix: ... start
            out_st = io.StringIO()
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("sys.stdout", out_st),
            ):
                rc_st = cli_mod.main(["status"])
            self.assertEqual(rc_st, 1)
            txt_st = out_st.getvalue()
            self.assertIn("Config    WARN", txt_st)
            self.assertLess(txt_st.index("Config    WARN"), txt_st.index("Fix:"))
            self.assertIn("init --force", txt_st)

            # 2. Non-UTF-8 .env encoding error MUST NOT suggest `init --force` (would clobber valid config.toml)
            (app_dir / "config.toml").write_text('[asr]\nprovider = "dashscope"\n', encoding="utf-8")
            (app_dir / ".env").write_bytes(b"DASHSCOPE_API_KEY=\xff\xfe\n")

            err_env_doc = io.StringIO()
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("sys.stderr", err_env_doc),
            ):
                rc_env_doc = cli_mod.main(["doctor"])
            self.assertEqual(rc_env_doc, 2)
            self.assertIn("UTF-8", err_env_doc.getvalue())
            self.assertNotIn("init", err_env_doc.getvalue())
            self.assertNotIn("--force", err_env_doc.getvalue())

            out_env_st = io.StringIO()
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("sys.stdout", out_env_st),
            ):
                rc_env_st = cli_mod.main(["status"])
            self.assertEqual(rc_env_st, 1)
            self.assertIn("UTF-8", out_env_st.getvalue())
            self.assertNotIn("init --force", out_env_st.getvalue())
            (app_dir / ".env").unlink()

            # 3. Broken JSON config via --config MUST suggest fixing JSON, never `init --force`
            bad_json = Path(td) / "settings.json"
            bad_json.write_text('{"asr": ', encoding="utf-8")
            err_json_doc = io.StringIO()
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("sys.stderr", err_json_doc),
            ):
                rc_json_doc = cli_mod.main(["doctor", "--config", str(bad_json)])
            self.assertEqual(rc_json_doc, 2)
            self.assertIn("JSON", err_json_doc.getvalue())
            self.assertNotIn("init --config", err_json_doc.getvalue())
            self.assertNotIn("--force", err_json_doc.getvalue())

            out_json_st = io.StringIO()
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("sys.stdout", out_json_st),
            ):
                rc_json_st = cli_mod.main(["status", "--config", str(bad_json)])
            self.assertEqual(rc_json_st, 1)
            self.assertIn("JSON", out_json_st.getvalue())
            self.assertNotIn("init --config", out_json_st.getvalue())
            self.assertNotIn("--force", out_json_st.getvalue())

            # 4. Non-.toml files (e.g. README.md) passed via --config MUST NEVER suggest `init --config ... --force`
            readme_file = Path(td) / "README.md"
            readme_orig = "# Project README\nNot a TOML config!\n"
            readme_file.write_text(readme_orig, encoding="utf-8")
            for subcmd, expected_rc in (("doctor", 2), ("start", 2)):
                err_md = io.StringIO()
                with (
                    mock.patch.dict(os.environ, env, clear=True),
                    mock.patch("sys.stderr", err_md),
                ):
                    rc_md = cli_mod.main([subcmd, "--config", str(readme_file)])
                self.assertEqual(rc_md, expected_rc)
                self.assertNotIn("init --config", err_md.getvalue())
                self.assertNotIn("--force", err_md.getvalue())
                self.assertIn("--config", err_md.getvalue())

            out_md_st = io.StringIO()
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("sys.stdout", out_md_st),
            ):
                rc_md_st = cli_mod.main(["status", "--config", str(readme_file)])
            self.assertEqual(rc_md_st, 1)
            self.assertNotIn("init --config", out_md_st.getvalue())
            self.assertNotIn("--force", out_md_st.getvalue())
            self.assertEqual(readme_file.read_text(encoding="utf-8"), readme_orig)

    def test_config_pointing_to_personal_hotwords_never_suggests_or_allows_init_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            rt = Path(td) / "rt"
            hw_dir = home / ".agent-voice-input" / "hotwords"
            hw_dir.mkdir(parents=True, mode=0o700)
            hw_path = hw_dir / "personal.txt"
            orig_bytes = b"# user hotwords\n!Antigravity\nHarness <- \xe5\x93\x88\xe5\xb0\xbc\xe6\x96\xaf\n"
            hw_path.write_bytes(orig_bytes)
            env = {"HOME": str(home), "PATH": "", "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)}

            # 1. `start --config .../hotwords/personal.txt` fails with config error and MUST NOT suggest `init --force`
            err_start = io.StringIO()
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("sys.stderr", err_start),
            ):
                rc_start = cli_mod.main(["start", "--config", str(hw_path)])
            self.assertEqual(rc_start, 2)
            self.assertIn("personal.txt", err_start.getvalue())
            self.assertNotIn("init --config", err_start.getvalue())
            self.assertNotIn("--force", err_start.getvalue())

            # 2. Offline `status --config .../hotwords/personal.txt` also refuses to suggest `init --force`
            out_st = io.StringIO()
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("sys.stdout", out_st),
            ):
                rc_st = cli_mod.main(["status", "--config", str(hw_path)])
            self.assertEqual(rc_st, 1)
            self.assertNotIn("init --config", out_st.getvalue())
            self.assertNotIn("--force", out_st.getvalue())

            # 3. Direct `init --config .../hotwords/personal.txt --force` is rejected and personal.txt is byte-for-byte intact
            err_init = io.StringIO()
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("sys.stderr", err_init),
            ):
                rc_init = cli_mod.main(["init", "--config", str(hw_path), "--force"])
            self.assertEqual(rc_init, 2)
            self.assertIn("personal hotwords", err_init.getvalue())
            self.assertEqual(hw_path.read_bytes(), orig_bytes)

            # 4. Also guarded under custom AGENT_VOICE_INPUT_HOME
            custom_home = Path(td) / "custom_avi"
            custom_hw = custom_home / "hotwords" / "personal.txt"
            custom_hw.parent.mkdir(parents=True, mode=0o700)
            custom_hw.write_bytes(orig_bytes)
            env_custom = {**env, "AGENT_VOICE_INPUT_HOME": str(custom_home)}
            err_init2 = io.StringIO()
            with (
                mock.patch.dict(os.environ, env_custom, clear=True),
                mock.patch("sys.stderr", err_init2),
            ):
                rc_init2 = cli_mod.main(["init", "--config", str(custom_hw), "--force"])
            self.assertEqual(rc_init2, 2)
            self.assertEqual(custom_hw.read_bytes(), orig_bytes)

            # 5. Case-insensitive alias (e.g. Personal.txt on macOS APFS) and same-inode alias (hardlink/samefile)
            #    MUST also be blocked from recovery hint and init --force.
            case_alias = hw_dir / "Personal.txt"
            try:
                is_case_insensitive = os.path.samefile(str(hw_path), str(case_alias))
            except OSError:
                is_case_insensitive = False
            if is_case_insensitive:
                err_case_start = io.StringIO()
                with (
                    mock.patch.dict(os.environ, env, clear=True),
                    mock.patch("sys.stderr", err_case_start),
                ):
                    rc_case_start = cli_mod.main(["start", "--config", str(case_alias)])
                self.assertEqual(rc_case_start, 2)
                self.assertIn("personal.txt", err_case_start.getvalue())
                self.assertNotIn("init --config", err_case_start.getvalue())
                self.assertNotIn("--force", err_case_start.getvalue())

                err_case_init = io.StringIO()
                with (
                    mock.patch.dict(os.environ, env, clear=True),
                    mock.patch("sys.stderr", err_case_init),
                ):
                    rc_case_init = cli_mod.main(["init", "--config", str(case_alias), "--force"])
                self.assertEqual(rc_case_init, 2)
                self.assertIn("personal hotwords", err_case_init.getvalue())
                self.assertEqual(hw_path.read_bytes(), orig_bytes)

            # Cross-platform hardlink test guarantees os.path.samefile() branch is exercised even on case-sensitive FS
            hardlink_alias = Path(td) / "Personal.txt"
            os.link(str(hw_path), str(hardlink_alias))
            err_hl_init = io.StringIO()
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("sys.stderr", err_hl_init),
            ):
                rc_hl_init = cli_mod.main(["init", "--config", str(hardlink_alias), "--force"])
            self.assertEqual(rc_hl_init, 2)
            self.assertIn("personal hotwords", err_hl_init.getvalue())
            self.assertEqual(hw_path.read_bytes(), orig_bytes)

    def test_start_skip_init_shows_init_hint_ffmpeg_warning_and_keeps_hotkey(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "fresh_home"
            home.mkdir()
            rt = Path(td) / "rt"
            env = {"HOME": str(home), "PATH": "", "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)}

            class _LiveProc:
                pid = 51515

                def poll(self) -> None:
                    return None

            calls = [0]

            def _ctrl(*_a: object, **_kw: object) -> dict[str, object]:
                calls[0] += 1
                if calls[0] == 1:
                    raise ConnectionRefusedError("not running")
                return {"ok": True, "pid": 51515, "state": "idle", "shim_ok": True}

            out = io.StringIO()
            err = io.StringIO()
            parser = cli_mod._build_parser()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value=""),
                mock.patch("agent_voice_input.control_ipc.send_control_command", side_effect=_ctrl),
                mock.patch.object(cli_mod.subprocess, "Popen", return_value=_LiveProc()),
                mock.patch.object(cli_mod, "_run_startup_preflight_ping", return_value=[]),
                mock.patch("sys.stdout", out),
                mock.patch("sys.stderr", err),
            ):
                rc = cli_mod._cmd_start(parser.parse_args(["start"]), env, cfg=Config())
            self.assertEqual(rc, 0)
            txt = out.getvalue()
            self.assertIn("agent-voice-input init", txt)
            self.assertIn("personal.txt", txt)
            self.assertIn("Warning   ffmpeg not found", txt)
            self.assertIn("Hotkey    tap RControl", txt)

    def test_macos_ssh_warning_convergence_keeps_authorized_semantics(self) -> None:
        # classify_macos_mic_permission keeps authorized -> None even with SSH env
        self.assertIsNone(
            classify_macos_mic_permission(
                {"mic_auth": "authorized", "has_graphic_session": True},
                {"SSH_CONNECTION": "1.2.3.4 111 5.6.7.8 22"},
            )
        )
        # doctor under darwin + SSH + authorized stays rc=0 and prints non-failing NOTE
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            rt = Path(td) / "rt"
            home.mkdir()
            env = {
                "HOME": str(home),
                "PATH": "",
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
                "DASHSCOPE_API_KEY": "sk-test-mac-ssh-1234",
                "SSH_CONNECTION": "1.2.3.4 111 5.6.7.8 22",
            }
            fake_probe = {
                "avfoundation_ok": True,
                "audio_devices": ["[0] Built-in Mic"],
                "configured_audio_device": ":0",
                "pbcopy_ok": True,
                "tmux_active": True,
                "clt_ok": True,
                "swiftc_ok": True,
                "swiftc_bin": "/usr/bin/swiftc",
                "mac_shim_ok": True,
                "mac_shim_bin": "/tmp/mac_shim",
                "caller_mic_auth": "authorized",
                "caller_has_graphic_session": True,
                "caller_is_remote_session": False,
                "caller_mic_warning": "",
                "permissions_hint": "hint",
                "fg_process": "Ghostty",
                "fg_title": "codex",
            }
            buf = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "darwin"),
                mock.patch("agent_voice_input.platform_macos.probe_macos_host", return_value=fake_probe),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", buf),
            ):
                rc = cli_mod._cmd_doctor(Config(), env)
            self.assertEqual(rc, 0, buf.getvalue())
            # Local GUI session with stale SSH_* env reports SSH environment variable detected (non-failing NOTE)
            self.assertIn("[macos-tcc] NOTE SSH environment variable detected", buf.getvalue())

            # True remote/non-GUI session with authorized TCC reports remote/SSH shell detected
            fake_probe_remote = {**fake_probe, "caller_is_remote_session": True}
            buf_rem = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "darwin"),
                mock.patch("agent_voice_input.platform_macos.probe_macos_host", return_value=fake_probe_remote),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_rem),
            ):
                rc_rem = cli_mod._cmd_doctor(Config(), env)
            self.assertEqual(rc_rem, 0, buf_rem.getvalue())
            self.assertIn("[macos-tcc] NOTE remote/SSH shell detected", buf_rem.getvalue())

    def test_wsl_doctor_no_bare_cmd_exe_wasapi_categories_and_interop_hint_and_probe_env(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            rt = Path(td) / "rt"
            home.mkdir()
            # Fake custom mount /c/Windows/System32/cmd.exe
            sys32 = Path(td) / "c" / "Windows" / "System32"
            ps_dir = sys32 / "WindowsPowerShell" / "v1.0"
            ps_dir.mkdir(parents=True)
            fake_ps = ps_dir / "powershell.exe"
            fake_cmd = sys32 / "cmd.exe"
            fake_ps.write_text("", encoding="utf-8")
            fake_cmd.write_text("", encoding="utf-8")

            env_wsl = {
                "HOME": str(home),
                "PATH": "",
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
                "WSL_DISTRO_NAME": "Ubuntu",
                "DASHSCOPE_API_KEY": "sk-wsl-test-1234",
            }

            # 1. probe_wsl_host forwards env=dict(raw_env) to PowerShell subprocess
            seen_envs: list[object] = []

            def _fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
                if str(cmd[0]).endswith("powershell.exe"):
                    seen_envs.append(kw.get("env"))
                    return subprocess.CompletedProcess(cmd, 0, stdout='{"ok": true, "ps_version": "5.1"}\n', stderr="")
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            with (
                mock.patch("agent_voice_input.platform_wsl.resolve_powershell_exe", return_value=str(fake_ps)),
                mock.patch("agent_voice_input.platform_wsl.to_windows_path", return_value=r"C:\win_shim.ps1"),
                mock.patch("agent_voice_input.platform_wsl.subprocess.run", side_effect=_fake_run),
            ):
                probe_wsl_host(Config(), env={**env_wsl, "WSL_INTEROP": "/run/WSL/123_interop"})
            self.assertEqual(len(seen_envs), 1)
            self.assertIsInstance(seen_envs[0], dict)
            self.assertEqual(seen_envs[0]["WSL_INTEROP"], "/run/WSL/123_interop")  # type: ignore[index]

            # 2. WASAPI no_default -> ms-settings:sound with resolved absolute cmd.exe (not bare cmd.exe)
            buf_nodef = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=True),
                mock.patch("agent_voice_input.platform_wsl.resolve_powershell_exe", return_value=str(fake_ps)),
                mock.patch(
                    "agent_voice_input.platform_wsl.probe_wsl_host",
                    return_value={
                        "powershell_ok": True,
                        "ps_version": "5.1",
                        "wasapi_ok": False,
                        "wasapi_state": "no_default:0x80070490",
                        "pulse_rdp_ok": False,
                        "winmm_devs": 0,
                    },
                ),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_nodef),
            ):
                self.assertEqual(cli_mod._cmd_doctor(Config(), env_wsl), 1)
            out_nodef = buf_nodef.getvalue()
            self.assertIn("ms-settings:sound", out_nodef)
            self.assertIn(str(fake_cmd), out_nodef)
            self.assertNotIn("Fix: cmd.exe ", out_nodef)

            # 3. Stale WSL_INTEROP (Invalid argument) -> explicit new Windows Terminal tab hint
            buf_interop = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=True),
                mock.patch(
                    "agent_voice_input.platform_wsl.probe_wsl_host",
                    return_value={
                        "powershell_ok": False,
                        "ps_error": "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe: Invalid argument",
                        "pulse_rdp_ok": True,
                        "winmm_devs": 0,
                    },
                ),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_interop),
            ):
                self.assertEqual(cli_mod._cmd_doctor(Config(), env_wsl), 1)
            self.assertIn("open a new Windows Terminal WSL tab", buf_interop.getvalue())

            # 4. Explicit wsl_source="pulse" with unavailable Pulse source -> FAIL + PulseAudio fix (not WARN + WASAPI privacy fix)
            cfg_pulse = Config(audio=AudioConfig(wsl_source="pulse"))
            env_wsl_pulse = {**env_wsl, "AGENT_VOICE_INPUT_PULSE_SOURCE": "missing_pulse_mic"}
            buf_pulse = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=True),
                mock.patch("agent_voice_input.platform_wsl.resolve_powershell_exe", return_value=str(fake_ps)),
                mock.patch(
                    "agent_voice_input.platform_wsl.probe_wsl_host",
                    return_value={
                        "powershell_ok": True,
                        "ps_version": "5.1",
                        "wasapi_ok": True,
                        "wasapi_state": "ok",
                        "pulse_source_ok": False,
                        "pulse_rdp_ok": False,
                        "winmm_devs": 1,
                    },
                ),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", buf_pulse),
            ):
                self.assertEqual(cli_mod._cmd_doctor(cfg_pulse, env_wsl_pulse), 1)
            out_pulse = buf_pulse.getvalue()
            self.assertIn("PulseAudio (missing_pulse_mic)=FAIL", out_pulse)
            self.assertIn("pactl list short sources", out_pulse)
            self.assertIn("missing_pulse_mic", out_pulse)
            self.assertNotIn("ms-settings:privacy-microphone", out_pulse)
            self.assertNotIn("ms-settings:sound", out_pulse)


class TestPreStopHudBoundariesV0181(unittest.TestCase):
    def _make_daemon(
        self,
        tmp: Path,
        *,
        wav: bytes | None = None,
        stop_delay: float = 0.0,
        stop_exc: Exception | None = None,
        asr_text: str = "测试文本",
        asr_delay: float = 0.0,
        max_duration_sec: float = 600.0,
    ) -> tuple[VoiceInputDaemon, list[str], list[tuple[str, dict[str, object]]], threading.Event]:
        events: list[str] = []
        hud_calls: list[tuple[str, dict[str, object]]] = []
        done_evt = threading.Event()
        eff_wav = _make_pcm_wav() if wav is None else wav

        class _FakePlatform:
            last_audio_error = ""
            _last_stop_recorder_lost = False

            def start_recording(self, runtime_dir: Path, **kw: object) -> None:
                del runtime_dir, kw
                events.append("start_rec")

            def stop_recording(self, **kw: object) -> tuple[bytes, str]:
                del kw
                events.append("stop_rec_begin")
                if stop_delay > 0:
                    time.sleep(stop_delay)
                if stop_exc is not None:
                    raise stop_exc
                events.append("stop_rec_end")
                return eff_wav, "fake"

            def cancel_recording(self) -> None:
                events.append("cancel_rec")

            def hud(self, state: str, text: str = "", duration_ms: int = 0, **kw: object) -> None:
                del text, duration_ms
                events.append(f"hud:{state}")
                hud_calls.append((state, dict(kw)))
                if state in ("done", "empty", "cancelled", "hide", "error"):
                    done_evt.set()

            def beep(self, kind: str) -> None:
                events.append(f"beep:{kind}")

            def resolve_foreground_window(self, w: ForegroundWindow | None = None) -> ForegroundWindow:
                return w or ForegroundWindow(1, "Ghostty", "codex")

            def deliver_text(self, text: str, seq_id: int, target_hwnd: int = 0, **kw: object) -> PasteOutcome:
                del text, seq_id, target_hwnd, kw
                return PasteOutcome(True, False, "ok", "Ghostty")

            def switch_to_fallback_audio(self) -> bool:
                return False

            def close(self) -> None:
                pass

        class _FakeAsr:
            provider_name = "dashscope"

            def transcribe(self, wav_bytes: bytes, **kw: object) -> AsrResult:
                del wav_bytes, kw
                if asr_delay > 0:
                    time.sleep(asr_delay)
                return AsrResult(text=asr_text, provider="dashscope", model="qwen3-asr-flash", latency_ms=5)

            def close(self) -> None:
                pass

        cfg = Config(
            asr=AsrConfig(provider="dashscope"),
            refiner=RefinerConfig(provider="off", include_git_status=False),
            audio=AudioConfig(beep=True, max_duration_sec=max_duration_sec),
            hotkey=HotkeyConfig(debounce_ms=0, scope="all"),
        )
        tracker = SessionTracker(env={"HOME": str(tmp)}, runtime_dir=tmp / "rt")
        daemon = VoiceInputDaemon(
            cfg,
            env={"HOME": str(tmp), "DASHSCOPE_API_KEY": "sk-test-hud"},
            runtime_dir=tmp / "rt",
            platform=_FakePlatform(),
            asr_provider=_FakeAsr(),
            tracker=tracker,
            paste_settle_sec=0.0,
        )
        return daemon, events, hud_calls, done_evt

    def _wait_idle(self, daemon: VoiceInputDaemon, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and daemon.state != "idle":
            time.sleep(0.01)

    def test_pre_stop_hud_normal_failure_cancel_autostop_silence_and_silent_mode(self) -> None:
        # 1. Normal stop: hud:recognizing fires BEFORE stop_rec_begin; beep:stop fires AFTER stop_rec_end
        with tempfile.TemporaryDirectory() as td:
            daemon, events, _hud_calls, done_evt = self._make_daemon(Path(td), stop_delay=0.05)
            try:
                self.assertTrue(daemon.handle_trigger("toggle")["ok"])
                events.clear()
                self.assertTrue(daemon.handle_trigger("toggle")["ok"])
                self.assertTrue(done_evt.wait(timeout=2.0))
                self._wait_idle(daemon)
                self.assertLess(events.index("hud:recognizing"), events.index("stop_rec_begin"))
                self.assertLess(events.index("stop_rec_end"), events.index("beep:stop"))
                self.assertFalse(daemon._main_hud_visible)
            finally:
                daemon.close()

        # 2. Stop failure: hud:recognizing is immediately cleared by error toast (clear_main=True), no stuck HUD
        with tempfile.TemporaryDirectory() as td:
            daemon, events, hud_calls, _ = self._make_daemon(
                Path(td), stop_exc=RuntimeError("capture crash")
            )
            try:
                self.assertTrue(daemon.handle_trigger("toggle")["ok"])
                events.clear()
                hud_calls.clear()
                res = daemon.handle_trigger("toggle")
                self.assertFalse(res["ok"])
                self.assertEqual(daemon.state, "idle")
                self.assertEqual(events[0], "hud:recognizing")
                self.assertEqual(hud_calls[-1][0], "error")
                self.assertTrue(hud_calls[-1][1].get("clear_main"))
                self.assertFalse(daemon._main_hud_visible)
            finally:
                daemon.close()

        # 3. Recording cancel (ptt_cancel): NEVER emits hud:recognizing; HUD clears cleanly
        with tempfile.TemporaryDirectory() as td:
            daemon, events, _hud_calls, _ = self._make_daemon(Path(td))
            try:
                self.assertTrue(daemon.handle_trigger("ptt_start")["ok"])
                events.clear()
                res = daemon.handle_trigger("ptt_cancel")
                self.assertTrue(res["ok"])
                self.assertNotIn("hud:recognizing", events)
                self.assertFalse(daemon._main_hud_visible)
            finally:
                daemon.close()

        # 4. Cancel arriving during stop_recording() tail_drain: cancels task and clears recognizing HUD
        with tempfile.TemporaryDirectory() as td:
            daemon, events, _hud_calls, done_evt = self._make_daemon(
                Path(td), stop_delay=0.12, asr_delay=0.25
            )
            try:
                self.assertTrue(daemon.handle_trigger("toggle")["ok"])
                events.clear()
                cancel_res: list[dict[str, object]] = []

                def _late_cancel() -> None:
                    time.sleep(0.03)
                    cancel_res.append(daemon.handle_cancel())

                t_c = threading.Thread(target=_late_cancel)
                t_c.start()
                self.assertTrue(daemon.handle_trigger("toggle")["ok"])
                t_c.join(timeout=2.0)
                self.assertTrue(done_evt.wait(timeout=2.0))
                self._wait_idle(daemon)
                self.assertTrue(cancel_res and cancel_res[0].get("ok"))
                self.assertFalse(daemon._main_hud_visible)
            finally:
                daemon.close()

        # 5. Max-duration auto-stop: emits hud:recognizing before stop_recording and finishes cleanly
        with tempfile.TemporaryDirectory() as td:
            daemon, events, _hud_calls, done_evt = self._make_daemon(
                Path(td), stop_delay=0.02, max_duration_sec=0.08
            )
            try:
                self.assertTrue(daemon.handle_trigger("toggle")["ok"])
                self.assertTrue(done_evt.wait(timeout=2.5))
                self._wait_idle(daemon)
                self.assertIn("hud:recognizing", events)
                self.assertLess(events.index("hud:recognizing"), events.index("stop_rec_begin"))
                self.assertLess(events.index("stop_rec_end"), events.index("beep:stop"))
                self.assertFalse(daemon._main_hud_visible)
            finally:
                daemon.close()

        # 6. Silence discard: audio gate rejects silent WAV and clears recognizing HUD cleanly
        with tempfile.TemporaryDirectory() as td:
            silent_wav = _make_pcm_wav(duration_sec=0.5, amp=0)
            daemon, events, _hud_calls, done_evt = self._make_daemon(Path(td), wav=silent_wav)
            try:
                self.assertTrue(daemon.handle_trigger("toggle")["ok"])
                events.clear()
                self.assertTrue(daemon.handle_trigger("toggle")["ok"])
                self.assertTrue(done_evt.wait(timeout=2.0))
                self._wait_idle(daemon)
                self.assertIn("hud:recognizing", events)
                self.assertIn("hud:empty", events)
                self.assertFalse(daemon._main_hud_visible)
            finally:
                daemon.close()

        # 7. Silent mode: ZERO HUD calls and ZERO beeps on stop
        with tempfile.TemporaryDirectory() as td:
            daemon, events, _hud_calls, _ = self._make_daemon(Path(td))
            try:
                self.assertTrue(daemon.handle_trigger("toggle", silent_hud=True)["ok"])
                events.clear()
                self.assertTrue(daemon.handle_trigger("toggle", silent_hud=True)["ok"])
                self._wait_idle(daemon)
                self.assertFalse(any(e.startswith("hud:") or e.startswith("beep:") for e in events), events)
            finally:
                daemon.close()

    def test_plan_commit_hud_transitions_refining_to_recognizing_during_stopping_capture(self) -> None:
        calls, beep = plan_commit_hud_calls(
            silent_hud=False,
            rec_active=True,
            recording_active=False,
            stopping_capture=True,
            cur_silent_hud=False,
            main_hud_state="refining",
            main_hud_visible=True,
            main_hud_bg_count=0,
            next_vis_stage="",
            vis_inflight=0,
            hud_st="done",
            copied_hud_text="",
            hud_dur_ms=0,
            beep_k="done",
        )
        self.assertEqual(beep, "")
        self.assertEqual(calls[0][0], "recognizing")

        # Silent stopping capture MUST NOT set recognizing HUD; last visible task clears HUD with no beep
        calls_silent_stop, beep_silent_stop = plan_commit_hud_calls(
            silent_hud=False,
            rec_active=True,
            recording_active=False,
            stopping_capture=True,
            cur_silent_hud=True,
            main_hud_state="recognizing",
            main_hud_visible=True,
            main_hud_bg_count=0,
            next_vis_stage="",
            vis_inflight=0,
            hud_st="",
            copied_hud_text="",
            hud_dur_ms=0,
            beep_k="done",
        )
        self.assertEqual(beep_silent_stop, "")
        self.assertEqual([c[0] for c in calls_silent_stop], ["hide"])

    def test_silent_stop_interleaved_with_visible_completion_and_stop_error_hides_hud(self) -> None:
        """P0-1 regression: visible A running -> silent B enters stop -> A finishes -> B stop raises."""
        from agent_voice_input.refiner import BaseRefiner, RefinerResult

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            events: list[str] = []
            hud_calls: list[tuple[str, dict[str, object]]] = []
            a_asr_started = threading.Event()
            allow_a_asr = threading.Event()
            b_stop_entered = threading.Event()
            allow_b_stop_fail = threading.Event()
            a_committed = threading.Event()
            eff_wav = _make_pcm_wav()
            stop_count = [0]

            class _SyncPlatform:
                last_audio_error = ""
                _last_stop_recorder_lost = False

                def start_recording(self, runtime_dir: Path, **kw: object) -> None:
                    del runtime_dir, kw
                    events.append("start_rec")

                def stop_recording(self, **kw: object) -> tuple[bytes, str]:
                    del kw
                    stop_count[0] += 1
                    idx = stop_count[0]
                    events.append(f"stop_rec_begin:{idx}")
                    if idx == 2:
                        b_stop_entered.set()
                        self.assert_wait = allow_b_stop_fail.wait(timeout=3.0)
                        raise RuntimeError("silent stop failure")
                    events.append(f"stop_rec_end:{idx}")
                    return eff_wav, "fake"

                def cancel_recording(self) -> None:
                    events.append("cancel_rec")

                def hud(self, state: str, text: str = "", duration_ms: int = 0, **kw: object) -> None:
                    del text, duration_ms
                    events.append(f"hud:{state}")
                    hud_calls.append((state, dict(kw)))
                    if state in ("done", "hide"):
                        a_committed.set()

                def beep(self, kind: str) -> None:
                    events.append(f"beep:{kind}")

                def resolve_foreground_window(self, w: ForegroundWindow | None = None) -> ForegroundWindow:
                    return w or ForegroundWindow(1, "Ghostty", "codex")

                def deliver_text(self, text: str, seq_id: int, target_hwnd: int = 0, **kw: object) -> PasteOutcome:
                    del text, seq_id, target_hwnd, kw
                    return PasteOutcome(True, False, "ok", "Ghostty")

                def switch_to_fallback_audio(self) -> bool:
                    return False

                def close(self) -> None:
                    pass

            class _SyncAsr:
                provider_name = "dashscope"

                def transcribe(self, wav_bytes: bytes, **kw: object) -> AsrResult:
                    del wav_bytes, kw
                    a_asr_started.set()
                    allow_a_asr.wait(timeout=3.0)
                    return AsrResult(text="A文本", provider="dashscope", model="qwen3-asr-flash", latency_ms=5)

                def close(self) -> None:
                    pass

            cfg = Config(
                asr=AsrConfig(provider="dashscope"),
                refiner=RefinerConfig(provider="off", include_git_status=False),
                audio=AudioConfig(beep=True),
                hotkey=HotkeyConfig(debounce_ms=0, scope="all"),
            )
            tracker = SessionTracker(env={"HOME": str(tmp)}, runtime_dir=tmp / "rt")
            daemon = VoiceInputDaemon(
                cfg,
                env={"HOME": str(tmp), "DASHSCOPE_API_KEY": "sk-test-p01"},
                runtime_dir=tmp / "rt",
                platform=_SyncPlatform(),
                asr_provider=_SyncAsr(),
                tracker=tracker,
                paste_settle_sec=0.0,
            )
            try:
                # 1. Start and stop visible utterance A -> blocks in ASR with recognizing HUD active
                self.assertTrue(daemon.handle_trigger("toggle")["ok"])
                self.assertTrue(daemon.handle_trigger("toggle")["ok"])
                self.assertTrue(a_asr_started.wait(timeout=2.0))
                self.assertTrue(daemon._main_hud_visible)

                # 2. Start silent utterance B, then enter B's stop_recording() on a background thread
                self.assertTrue(daemon.handle_trigger("toggle", silent_hud=True)["ok"])
                events.clear()
                hud_calls.clear()
                b_res: list[dict[str, object]] = []

                def _stop_b() -> None:
                    b_res.append(daemon.handle_trigger("toggle", silent_hud=True))

                t_b = threading.Thread(target=_stop_b)
                t_b.start()
                self.assertTrue(b_stop_entered.wait(timeout=2.0))

                # 3. While silent B is stopping, let visible A finish & commit
                allow_a_asr.set()
                self.assertTrue(a_committed.wait(timeout=2.0))
                # Wait briefly for commit worker to finish updating state
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and daemon._main_hud_visible:
                    time.sleep(0.01)

                # 4. Now let silent B's stop_recording raise an error
                allow_b_stop_fail.set()
                t_b.join(timeout=2.0)
                self._wait_idle(daemon)

                self.assertTrue(b_res and b_res[0].get("ok") is False)
                self.assertEqual(daemon.state, "idle")
                self.assertFalse(daemon._main_hud_visible)
                self.assertEqual(daemon._main_hud_state, "")
                # Never re-emitted persistent recognizing HUD during silent stop, explicitly hid HUD, and zero beeps!
                self.assertIn("hud:hide", events)
                self.assertNotIn("hud:recognizing", events)
                self.assertFalse(any(e.startswith("beep:") for e in events), events)
            finally:
                allow_a_asr.set()
                allow_b_stop_fail.set()
                daemon.close()

    def test_normal_stop_does_not_miss_background_refining_hud_transition(self) -> None:
        """P1-1 regression: A recognizing -> B enters stop -> A transitions to refining -> B finishes stop."""
        from agent_voice_input.refiner import BaseRefiner, RefinerResult

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            events: list[str] = []
            a_asr_started = threading.Event()
            allow_a_asr = threading.Event()
            a_refine_started = threading.Event()
            allow_a_refine = threading.Event()
            b_stop_entered = threading.Event()
            allow_b_stop = threading.Event()
            eff_wav = _make_pcm_wav()
            stop_count = [0]
            asr_count = [0]

            class _SyncPlatform:
                last_audio_error = ""
                _last_stop_recorder_lost = False

                def start_recording(self, runtime_dir: Path, **kw: object) -> None:
                    del runtime_dir, kw
                    events.append("start_rec")

                def stop_recording(self, **kw: object) -> tuple[bytes, str]:
                    del kw
                    stop_count[0] += 1
                    idx = stop_count[0]
                    events.append(f"stop_rec_begin:{idx}")
                    if idx == 2:
                        b_stop_entered.set()
                        allow_b_stop.wait(timeout=3.0)
                    events.append(f"stop_rec_end:{idx}")
                    return eff_wav, "fake"

                def cancel_recording(self) -> None:
                    events.append("cancel_rec")

                def hud(self, state: str, text: str = "", duration_ms: int = 0, **kw: object) -> None:
                    del text, duration_ms, kw
                    events.append(f"hud:{state}")

                def beep(self, kind: str) -> None:
                    events.append(f"beep:{kind}")

                def resolve_foreground_window(self, w: ForegroundWindow | None = None) -> ForegroundWindow:
                    return w or ForegroundWindow(1, "Ghostty", "codex")

                def deliver_text(self, text: str, seq_id: int, target_hwnd: int = 0, **kw: object) -> PasteOutcome:
                    del text, seq_id, target_hwnd, kw
                    return PasteOutcome(True, False, "ok", "Ghostty")

                def switch_to_fallback_audio(self) -> bool:
                    return False

                def close(self) -> None:
                    pass

            class _SyncAsr:
                provider_name = "dashscope"

                def transcribe(self, wav_bytes: bytes, **kw: object) -> AsrResult:
                    del wav_bytes, kw
                    asr_count[0] += 1
                    if asr_count[0] == 1:
                        a_asr_started.set()
                        allow_a_asr.wait(timeout=3.0)
                        return AsrResult(text="A草稿", provider="dashscope", model="qwen3-asr-flash", latency_ms=5)
                    return AsrResult(text="B草稿", provider="dashscope", model="qwen3-asr-flash", latency_ms=5)

                def close(self) -> None:
                    pass

            class _SyncRefiner(BaseRefiner):
                provider_name = "dashscope"

                def refine(self, asr_result: AsrResult, **kw: object) -> RefinerResult:
                    del kw
                    if asr_result.text == "A草稿":
                        a_refine_started.set()
                        allow_a_refine.wait(timeout=3.0)
                    return RefinerResult(
                        text=asr_result.text + "精修",
                        provider="dashscope",
                        model="qwen3.8-flash",
                        latency_ms=5,
                    )

                def close(self) -> None:
                    pass

            cfg = Config(
                asr=AsrConfig(provider="dashscope"),
                refiner=RefinerConfig(provider="dashscope", include_git_status=False),
                audio=AudioConfig(beep=True),
                hotkey=HotkeyConfig(debounce_ms=0, scope="all"),
            )
            tracker = SessionTracker(env={"HOME": str(tmp)}, runtime_dir=tmp / "rt")
            daemon = VoiceInputDaemon(
                cfg,
                env={"HOME": str(tmp), "DASHSCOPE_API_KEY": "sk-test-p11"},
                runtime_dir=tmp / "rt",
                platform=_SyncPlatform(),
                asr_provider=_SyncAsr(),
                refiner=_SyncRefiner(),
                tracker=tracker,
                paste_settle_sec=0.0,
            )
            try:
                # 1. Start and stop visible A -> blocks in Stage 1 ASR
                self.assertTrue(daemon.handle_trigger("toggle")["ok"])
                self.assertTrue(daemon.handle_trigger("toggle")["ok"])
                self.assertTrue(a_asr_started.wait(timeout=2.0))

                # 2. Start visible B and enter B's stop_recording()
                self.assertTrue(daemon.handle_trigger("toggle")["ok"])
                events.clear()
                b_res: list[dict[str, object]] = []

                def _stop_b() -> None:
                    b_res.append(daemon.handle_trigger("toggle"))

                t_b = threading.Thread(target=_stop_b)
                t_b.start()
                self.assertTrue(b_stop_entered.wait(timeout=2.0))
                self.assertEqual(daemon._main_hud_state, "recognizing")

                # 3. While B is stopping, let A finish ASR and enter Stage 2 refining
                allow_a_asr.set()
                self.assertTrue(a_refine_started.wait(timeout=2.0))

                # 4. Now let B finish stop_recording() and enqueue
                allow_b_stop.set()
                t_b.join(timeout=2.0)
                self.assertTrue(b_res and b_res[0].get("ok"))

                # Main HUD MUST reflect A's refining stage (not stuck on recognizing!)
                self.assertEqual(daemon._main_hud_state, "refining")
                self.assertIn("hud:refining", events)

                # 5. Release A refine and ensure both A & B complete cleanly to idle
                allow_a_refine.set()
                self._wait_idle(daemon)
                self.assertEqual(daemon.state, "idle")
                self.assertFalse(daemon._main_hud_visible)
            finally:
                allow_a_asr.set()
                allow_a_refine.set()
                allow_b_stop.set()
                daemon.close()

    def test_visible_start_then_silent_stop_error_or_silent_cancel_clears_main_hud(self) -> None:
        """P1-1 regression: visible start followed by silent stop failure or silent cancel clears recording HUD."""
        # 1. Visible start -> silent stop raises error: hides main HUD, no error toast, no beep
        with tempfile.TemporaryDirectory() as td:
            daemon, events, hud_calls, _ = self._make_daemon(
                Path(td), stop_exc=RuntimeError("mic boom")
            )
            try:
                self.assertTrue(daemon.handle_trigger("toggle")["ok"])
                self.assertTrue(daemon._main_hud_visible)
                self.assertEqual(daemon._main_hud_state, "recording")
                events.clear()
                hud_calls.clear()

                res = daemon.handle_trigger("toggle", silent_hud=True)
                self.assertFalse(res["ok"])
                self.assertEqual(daemon.state, "idle")
                self.assertFalse(daemon._main_hud_visible)
                self.assertEqual(daemon._main_hud_state, "")
                self.assertIn("hud:hide", events)
                self.assertNotIn("hud:error", events)
                self.assertNotIn("hud:recognizing", events)
                self.assertFalse(any(e.startswith("beep:") for e in events), events)
            finally:
                daemon.close()

        # 2. Visible start -> silent handle_cancel: hides main HUD, no cancelled toast, no beep
        with tempfile.TemporaryDirectory() as td:
            daemon, events, hud_calls, _ = self._make_daemon(Path(td))
            try:
                self.assertTrue(daemon.handle_trigger("toggle")["ok"])
                self.assertTrue(daemon._main_hud_visible)
                self.assertEqual(daemon._main_hud_state, "recording")
                events.clear()
                hud_calls.clear()

                res = daemon.handle_cancel(silent_hud=True)
                self.assertTrue(res["ok"])
                self.assertEqual(daemon.state, "idle")
                self.assertFalse(daemon._main_hud_visible)
                self.assertEqual(daemon._main_hud_state, "")
                self.assertIn("hud:hide", events)
                self.assertNotIn("hud:cancelled", events)
                self.assertFalse(any(e.startswith("beep:") for e in events), events)
            finally:
                daemon.close()

        # 3. Visible start -> silent ptt_cancel: hides main HUD, no toast, no beep
        with tempfile.TemporaryDirectory() as td:
            daemon, events, hud_calls, _ = self._make_daemon(Path(td))
            try:
                self.assertTrue(daemon.handle_trigger("ptt_start")["ok"])
                self.assertTrue(daemon._main_hud_visible)
                events.clear()
                hud_calls.clear()

                res = daemon.handle_trigger("ptt_cancel", silent_hud=True)
                self.assertTrue(res["ok"])
                self.assertEqual(daemon.state, "idle")
                self.assertFalse(daemon._main_hud_visible)
                self.assertEqual(daemon._main_hud_state, "")
                self.assertIn("hud:hide", events)
                self.assertNotIn("hud:cancelled", events)
                self.assertFalse(any(e.startswith("beep:") for e in events), events)
            finally:
                daemon.close()

        # 4. Visible background task A running -> visible start B -> silent cancel B restores A's stage without toast/beep
        with tempfile.TemporaryDirectory() as td:
            daemon, events, _hud_calls, done_evt = self._make_daemon(Path(td), asr_delay=0.25)
            try:
                self.assertTrue(daemon.handle_trigger("toggle")["ok"])
                self.assertTrue(daemon.handle_trigger("toggle")["ok"])
                self.assertTrue(daemon.handle_trigger("toggle")["ok"])
                self.assertEqual(daemon._main_hud_state, "recording")
                events.clear()

                res = daemon.handle_cancel(silent_hud=True)
                self.assertTrue(res["ok"])
                self.assertEqual(daemon._main_hud_state, "recognizing")
                self.assertIn("hud:recognizing", events)
                self.assertNotIn("hud:cancelled", events)
                self.assertFalse(any(e.startswith("beep:") for e in events), events)

                self.assertTrue(done_evt.wait(timeout=2.0))
                self._wait_idle(daemon)
                self.assertEqual(daemon.state, "idle")
                self.assertFalse(daemon._main_hud_visible)
            finally:
                daemon.close()


if __name__ == "__main__":
    unittest.main()
