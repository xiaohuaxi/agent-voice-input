"""Unit tests for ``agent_voice_input.asr_providers`` and ``agent_voice_input.session_ctx``."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unittest
from unittest import mock
import urllib.error
import urllib.request

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _REPO_ROOT / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from agent_voice_input.asr_providers import (  # noqa: E402
    AsrContext,
    AsrError,
    HttpRequest,
    HttpResponse,
    _SameOriginSafeRedirectHandler,
    reset_doubao_slot_cache,
    transcribe,
)
from agent_voice_input.audio_proc import pack_pcm16_mono_wav  # noqa: E402
from agent_voice_input.config import Config, resolve_credentials  # noqa: E402
from agent_voice_input.session_ctx import (  # noqa: E402
    SessionTracker,
    encode_claude_project_dir,
    is_agy_presence_lock_held_nonproc,
    parse_window_title_hint,
    write_session_registry_entry,
)


def _sample_wav() -> bytes:
    return pack_pcm16_mono_wav([2000, -2000] * 4000)


class AsrProvidersTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_doubao_slot_cache()

    def tearDown(self) -> None:
        reset_doubao_slot_cache()

    def test_ali_qwen3_compatible_mode_exact_contract(self) -> None:
        captured: list[HttpRequest] = []

        def fake_transport(req: HttpRequest) -> HttpResponse:
            captured.append(req)
            return HttpResponse(
                status=200,
                headers={"content-type": "application/json"},
                body=json.dumps(
                    {
                        "choices": [
                            {"message": {"role": "assistant", "content": "  欢迎使用阿里云。 "}}
                        ]
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
            )

        cfg = Config()
        cfg.asr.provider = "ali"
        cfg.asr.model = "qwen3-asr-flash"
        cfg.asr.hotwords_file = ""
        env = {"API_KEY_ALI": "dummy_ali_secret_token"}
        creds = resolve_credentials(cfg, env)

        res = transcribe(
            _sample_wav(),
            cfg,
            creds=creds,
            env=env,
            context=AsrContext(hotwords=["session_ctx", "RControl"], dialog_text="讨论语音输入"),
            transport=fake_transport,
        )
        self.assertEqual(res.text, "欢迎使用阿里云。")
        self.assertEqual(res.provider, "ali")
        self.assertEqual(res.model, "qwen3-asr-flash")
        self.assertFalse(res.is_single_hop_refined)

        self.assertEqual(len(captured), 1)
        req = captured[0]
        self.assertEqual(
            req.url,
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        )
        body = json.loads(req.body.decode("utf-8"))
        self.assertEqual(body["model"], "qwen3-asr-flash")
        self.assertIs(body["stream"], False)
        self.assertEqual(body["asr_options"], {"enable_itn": True})

        sys_msg = body["messages"][0]
        self.assertEqual(sys_msg["role"], "system")
        self.assertIsInstance(sys_msg["content"], list)
        self.assertEqual(sys_msg["content"][0]["type"], "text")
        self.assertIn("RControl", sys_msg["content"][0]["text"])

        usr_msg = body["messages"][1]
        self.assertEqual(usr_msg["role"], "user")
        ia = usr_msg["content"][0]["input_audio"]
        self.assertEqual(set(ia.keys()), {"data"})
        self.assertTrue(ia["data"].startswith("data:audio/wav;base64,"))

        # Empty context omits system message altogether
        captured.clear()
        transcribe(_sample_wav(), cfg, creds=creds, env=env, context=None, transport=fake_transport)
        body_no_ctx = json.loads(captured[0].body.decode("utf-8"))
        self.assertEqual(len(body_no_ctx["messages"]), 1)
        self.assertEqual(body_no_ctx["messages"][0]["role"], "user")

    def test_ali_qwen_audio_3_1_native_route_and_no_words_empty_normalization(self) -> None:
        captured: list[HttpRequest] = []
        responses = [
            HttpResponse(
                status=200,
                headers={"content-type": "application/json"},
                body=json.dumps({"output": {"text": "原生多模态识别成功"}}, ensure_ascii=False).encode(
                    "utf-8"
                ),
            ),
            HttpResponse(
                status=200,
                headers={"content-type": "application/json"},
                body=json.dumps(
                    {
                        "output": {
                            "choices": [
                                {"message": {"content": [{"text": "候选列表结构兼容"}]}}
                            ]
                        }
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
            ),
            HttpResponse(
                status=400,
                headers={"content-type": "application/json"},
                body=json.dumps(
                    {"code": "CLIENT_ERROR", "message": "ASR_RESPONSE_HAVE_NO_WORDS"}
                ).encode("utf-8"),
            ),
            # Error body with output: null MUST raise AsrError rather than returning ""
            HttpResponse(
                status=200,
                headers={"content-type": "application/json"},
                body=json.dumps(
                    {"code": "InvalidApiKey", "message": "bad key", "output": None}
                ).encode("utf-8"),
            ),
        ]

        def fake_transport(req: HttpRequest) -> HttpResponse:
            captured.append(req)
            return responses.pop(0)

        cfg = Config()
        cfg.asr.provider = "qwen"
        cfg.asr.model = "qwen-audio-3.1-asr-flash"
        cfg.asr.hotwords_file = ""
        env = {"ALI_API_KEY": "dummy_ali_native_secret"}

        r1 = transcribe(
            _sample_wav(),
            cfg,
            env=env,
            context=AsrContext(hotwords=["tmux"]),
            transport=fake_transport,
        )
        self.assertEqual(r1.text, "原生多模态识别成功")
        req1 = captured[0]
        self.assertEqual(
            req1.url,
            "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
        )
        b1 = json.loads(req1.body.decode("utf-8"))
        self.assertEqual(b1["parameters"]["format"], "wav")
        self.assertEqual(b1["parameters"]["sample_rate"], 16000)
        self.assertTrue(
            b1["input"]["messages"][1]["content"][0]["audio"].startswith("data:audio/wav;base64,")
        )

        r2 = transcribe(_sample_wav(), cfg, env=env, transport=fake_transport)
        self.assertEqual(r2.text, "候选列表结构兼容")

        r3 = transcribe(_sample_wav(), cfg, env=env, transport=fake_transport)
        self.assertEqual(r3.text, "")

        with self.assertRaises(AsrError):
            transcribe(_sample_wav(), cfg, env=env, transport=fake_transport)

    def test_doubao_auto_slot_fallback_memory_cache_and_context_truncation(self) -> None:
        calls: list[tuple[str, str, str, dict[str, str], bytes]] = []

        def fake_transport(req: HttpRequest) -> HttpResponse:
            rid = req.headers.get("X-Api-Resource-Id", "")
            req_id = req.headers.get("X-Api-Request-Id", "")
            calls.append((req.url, rid, req_id, dict(req.headers), req.body))

            if rid in ("volc.bigasr.auc_turbo", "volc.seedasr.auc"):
                return HttpResponse(
                    status=403,
                    headers={"x-api-status-code": "45000030"},
                    body=json.dumps({"message": "requested resource not granted"}).encode("utf-8"),
                )

            if req.url.endswith("/submit") and rid == "volc.bigasr.auc":
                return HttpResponse(
                    status=200,
                    headers={"x-api-status-code": "20000000"},
                    body=b"{}",
                )

            if req.url.endswith("/query") and rid == "volc.bigasr.auc":
                q_count = sum(1 for u, r, _, _, _ in calls if u.endswith("/query") and r == rid)
                if q_count == 1:
                    return HttpResponse(
                        status=200,
                        headers={"x-api-status-code": "20000001"},
                        body=b"{}",
                    )
                return HttpResponse(
                    status=200,
                    headers={"x-api-status-code": "20000000"},
                    body=json.dumps(
                        {"result": {"text": "豆包降级缓存转写成功"}}, ensure_ascii=False
                    ).encode("utf-8"),
                )
            raise AssertionError(f"unexpected request: {req.url} rid={rid}")

        cfg = Config()
        cfg.asr.provider = "doubao"
        cfg.asr.hotwords_file = ""
        env = {
            "APP_ID_DOUBAO_ASR": "fake_appid_value",
            "API_KEY_DOUBAO_ASR": "fake_access_key_value",
        }
        many_words = [f"term_{i:02d}_identifier" for i in range(45)]
        long_dialog = "前文忽略" * 300 + "尾部核心对话内容保留"
        sleeps: list[float] = []

        res1 = transcribe(
            _sample_wav(),
            cfg,
            env=env,
            context=AsrContext(hotwords=many_words, dialog_text=long_dialog),
            transport=fake_transport,
            sleep_fn=sleeps.append,
        )
        self.assertEqual(res1.text, "豆包降级缓存转写成功")
        self.assertEqual(
            [(u.rsplit("/", 1)[-1], r) for u, r, _, _, _ in calls],
            [
                ("flash", "volc.bigasr.auc_turbo"),
                ("submit", "volc.seedasr.auc"),
                ("submit", "volc.bigasr.auc"),
                ("query", "volc.bigasr.auc"),
                ("query", "volc.bigasr.auc"),
            ],
        )
        submit_call = calls[2]
        query_call_1 = calls[3]
        query_call_2 = calls[4]
        self.assertEqual(submit_call[2], query_call_1[2])
        self.assertEqual(submit_call[2], query_call_2[2])
        self.assertEqual(submit_call[3]["X-Api-App-Key"], "fake_appid_value")
        self.assertEqual(submit_call[3]["X-Api-Access-Key"], "fake_access_key_value")
        self.assertEqual(query_call_1[4], b"{}")
        self.assertEqual(sleeps, [0.15, 0.15])

        req_json = json.loads(submit_call[4].decode("utf-8"))
        corpus_ctx = json.loads(req_json["request"]["corpus"]["context"])
        self.assertLessEqual(len(corpus_ctx["hotwords"]), 30)
        self.assertLessEqual(sum(len(w["word"]) for w in corpus_ctx["hotwords"]), 180)
        self.assertLessEqual(len(corpus_ctx["context_data"][0]["text"]), 600)
        self.assertTrue(corpus_ctx["context_data"][0]["text"].endswith("尾部核心对话内容保留"))

        # Call 2: process-local slot cache hits volc.bigasr.auc directly with ZERO 403 retries!
        calls.clear()
        res2 = transcribe(
            _sample_wav(),
            cfg,
            env=env,
            transport=fake_transport,
            sleep_fn=lambda _: None,
        )
        self.assertEqual(res2.text, "豆包降级缓存转写成功")
        self.assertEqual(
            [(u.rsplit("/", 1)[-1], r) for u, r, _, _, _ in calls],
            [
                ("submit", "volc.bigasr.auc"),
                ("query", "volc.bigasr.auc"),
                ("query", "volc.bigasr.auc"),
            ],
        )

    def test_doubao_unified_deadline_prevents_over_budget_polling(self) -> None:
        now_box = [100.0]
        calls: list[str] = []

        def fake_clock() -> float:
            return now_box[0]

        def fake_sleep(dt: float) -> None:
            now_box[0] += dt

        def slow_transport(req: HttpRequest) -> HttpResponse:
            calls.append(req.url.rsplit("/", 1)[-1])
            if req.url.endswith("/submit"):
                # Submit consumes 14.9s of the 15.0s total budget
                now_box[0] += 14.9
                return HttpResponse(
                    status=200,
                    headers={"x-api-status-code": "20000000"},
                    body=b"{}",
                )
            raise AssertionError("query should not be sent after unified deadline expires")

        cfg = Config()
        cfg.asr.provider = "doubao"
        cfg.asr.resource_id = "volc.bigasr.auc"
        cfg.asr.timeout_sec = 15.0
        cfg.asr.hotwords_file = ""
        env = {"DOUBAO_ASR_API_KEY": "deadline_test_key"}

        with self.assertRaises(AsrError) as ctx:
            transcribe(
                _sample_wav(),
                cfg,
                env=env,
                transport=slow_transport,
                clock_fn=fake_clock,
                sleep_fn=fake_sleep,
            )
        self.assertIn("timed out", str(ctx.exception))
        self.assertEqual(calls, ["submit"])
        # Total elapsed strictly bounded at 15.0s!
        self.assertAlmostEqual(now_box[0] - 100.0, 15.0, places=3)

        # Late HTTP 200 success (e.g. flash taking 30s under 15s budget) MUST also be rejected!
        def late_flash_transport(_req: HttpRequest) -> HttpResponse:
            now_box[0] += 30.0
            return HttpResponse(
                status=200,
                headers={"x-api-status-code": "20000000"},
                body=b'{"result": {"text": "late success should be rejected"}}',
            )

        cfg_flash = Config()
        cfg_flash.asr.provider = "doubao"
        cfg_flash.asr.resource_id = "volc.bigasr.auc_turbo"
        cfg_flash.asr.timeout_sec = 15.0
        cfg_flash.asr.hotwords_file = ""
        now_box[0] = 200.0
        with self.assertRaises(AsrError) as late_ctx:
            transcribe(
                _sample_wav(),
                cfg_flash,
                env=env,
                transport=late_flash_transport,
                clock_fn=fake_clock,
                sleep_fn=fake_sleep,
            )
        self.assertIn("exceeded timeout budget", str(late_ctx.exception))

        # Generic Doubao parameter error 45000000 ("invalid audio format") MUST NOT evict valid slot cache
        cfg_auto = Config()
        cfg_auto.asr.provider = "doubao"
        cfg_auto.asr.resource_id = "auto"
        cfg_auto.asr.hotwords_file = ""
        step = [0]

        def param_err_transport(req: HttpRequest) -> HttpResponse:
            step[0] += 1
            if step[0] == 1:
                return HttpResponse(
                    status=200,
                    headers={"x-api-status-code": "20000000"},
                    body=b'{"result": {"text": "ok"}}',
                )
            return HttpResponse(
                status=400,
                headers={
                    "x-api-status-code": "45000000",
                    "x-api-message": "invalid audio sample rate",
                },
                body=b"{}",
            )

        res_ok = transcribe(_sample_wav(), cfg_auto, env=env, transport=param_err_transport)
        self.assertEqual(res_ok.text, "ok")
        with self.assertRaises(AsrError) as perr_ctx:
            transcribe(_sample_wav(), cfg_auto, env=env, transport=param_err_transport)
        # Fails immediately on the cached slot without falling through 3 slots
        self.assertEqual(step[0], 2)
        self.assertIn("invalid audio sample rate", str(perr_ctx.exception))

    def test_security_exception_chain_boundary_truncation_redirect_and_custom_key_env(self) -> None:
        secret = "boundary_secret_value_1234567890"
        env = {"DOUBAO_ASR_API_KEY": secret}

        # 1. Upstream transport exception containing raw secret MUST NOT appear in full traceback!
        def transport_raising_secret(_req: HttpRequest) -> HttpResponse:
            raise RuntimeError(f"socket failure with raw credential {secret} inside cause")

        cfg = Config()
        cfg.asr.provider = "doubao"
        cfg.asr.resource_id = "volc.bigasr.auc_turbo"
        cfg.asr.hotwords_file = ""

        with self.assertRaises(AsrError) as exc_ctx:
            transcribe(_sample_wav(), cfg, env=env, transport=transport_raising_secret)
        self.assertIsNone(exc_ctx.exception.__cause__)
        full_tb = "".join(
            traceback.format_exception(
                type(exc_ctx.exception), exc_ctx.exception, exc_ctx.exception.__traceback__
            )
        )
        self.assertNotIn(secret, full_tb)
        self.assertNotIn("boundary_secret", full_tb)

        # 2. Secret straddling the 200/240 character truncation boundary MUST be redacted BEFORE slicing!
        def transport_boundary_secret(_req: HttpRequest) -> HttpResponse:
            # Place secret starting at index 190 so raw[:200] would chop it into "boundary_s"
            msg = ("x" * 190) + secret + ("y" * 40)
            return HttpResponse(status=500, headers={}, body=msg.encode("utf-8"))

        cfg_oai = Config()
        cfg_oai.asr.provider = "openai-compatible"
        cfg_oai.asr.hotwords_file = ""
        env_oai = {"OPENAI_API_KEY": secret}
        with self.assertRaises(AsrError) as bound_ctx:
            transcribe(_sample_wav(), cfg_oai, env=env_oai, transport=transport_boundary_secret)
        self.assertNotIn("boundary_s", str(bound_ctx.exception))
        self.assertNotIn("67890", str(bound_ctx.exception))
        self.assertIn("[REDACTED]", str(bound_ctx.exception))

        # 3. Cross-origin & HTTPS->HTTP downgrade redirects MUST be blocked by _SameOriginSafeRedirectHandler!
        handler = _SameOriginSafeRedirectHandler()
        orig_req = urllib.request.Request(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {secret}"},
            method="POST",
        )
        with self.assertRaises(urllib.error.URLError):
            handler.redirect_request(
                orig_req, None, 302, "Found", {}, "https://evil.example.com/collect"
            )
        with self.assertRaises(urllib.error.URLError):
            handler.redirect_request(
                orig_req, None, 302, "Found", {}, "http://api.openai.com/v1/insecure"
            )
        same_origin = handler.redirect_request(
            orig_req, None, 307, "Temp", {}, "https://api.openai.com/v2/audio/transcriptions"
        )
        self.assertIsNotNone(same_origin)

        # 4. Explicit asr.api_key_env missing MUST fail closed without falling back to default ALI_API_KEY
        cfg_custom = Config()
        cfg_custom.asr.provider = "ali"
        cfg_custom.asr.api_key_env = "MISSING_CUSTOM_ALI_KEY"
        cfg_custom.asr.hotwords_file = ""
        with self.assertRaises(AsrError) as custom_ctx:
            transcribe(
                _sample_wav(),
                cfg_custom,
                env={"ALI_API_KEY": "default_ali_should_not_be_used"},
                transport=lambda _: HttpResponse(status=200, headers={}, body=b"{}"),
            )
        self.assertIn("MISSING_CUSTOM_ALI_KEY", str(custom_ctx.exception))

        # 5. OpenAI-compatible HTTP 200 HTML/non-JSON MUST raise AsrError (never paste HTML into terminal!)
        with self.assertRaises(AsrError):
            transcribe(
                _sample_wav(),
                cfg_oai,
                env=env_oai,
                transport=lambda _: HttpResponse(
                    status=200,
                    headers={"content-type": "text/html"},
                    body=b"<html><body>SSO Login</body></html>",
                ),
            )

        # 6. String-typed "error" field in Gemini/Ali/OpenAI JSON MUST raise redacted AsrError (no AttributeError!)
        cfg_gem = Config()
        cfg_gem.asr.provider = "gemini"
        cfg_gem.asr.api_key_env = "CUSTOM_GEM_TOML_KEY"
        cfg_gem.asr.hotwords_file = ""
        gem_secret = "custom_gem_toml_secret_val"
        with self.assertRaises(AsrError) as gem_err_ctx:
            transcribe(
                _sample_wav(),
                cfg_gem,
                env={"CUSTOM_GEM_TOML_KEY": gem_secret},
                transport=lambda _: HttpResponse(
                    status=400,
                    headers={"content-type": "application/json"},
                    body=json.dumps({"error": f"bad key {gem_secret}"}).encode("utf-8"),
                ),
            )
        self.assertNotIn(gem_secret, str(gem_err_ctx.exception))
        self.assertIn("[REDACTED]", str(gem_err_ctx.exception))

    def test_gemini_single_hop_and_openai_compatible_multipart(self) -> None:
        gem_reqs: list[HttpRequest] = []

        def gem_transport(req: HttpRequest) -> HttpResponse:
            gem_reqs.append(req)
            return HttpResponse(
                status=200,
                headers={"content-type": "application/json"},
                body=json.dumps(
                    {
                        "candidates": [
                            {
                                "content": {
                                    "parts": [
                                        {"thought": True, "text": "internal thinking"},
                                        {"text": "保留 A，改为调用 B"},
                                    ]
                                }
                            }
                        ]
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
            )

        cfg = Config()
        cfg.asr.provider = "gemini-flash-audio"
        cfg.asr.hotwords_file = ""
        gem_env = {"GEMINI_API_KEY": "fake_gemini_token_val"}
        gem_res = transcribe(
            _sample_wav(),
            cfg,
            env=gem_env,
            context=AsrContext(dialog_text="修改函数 B"),
            transport=gem_transport,
        )
        self.assertTrue(gem_res.is_single_hop_refined)
        self.assertEqual(gem_res.text, "保留 A，改为调用 B")
        self.assertIn(":generateContent", gem_reqs[0].url)
        self.assertEqual(gem_reqs[0].headers["x-goog-api-key"], "fake_gemini_token_val")

        oai_reqs: list[HttpRequest] = []

        def oai_transport(req: HttpRequest) -> HttpResponse:
            oai_reqs.append(req)
            return HttpResponse(
                status=200,
                headers={"content-type": "application/json"},
                body=json.dumps({"text": "Whisper 兼容识别结果"}, ensure_ascii=False).encode("utf-8"),
            )

        cfg_oai = Config()
        cfg_oai.asr.provider = "openai-compatible"
        cfg_oai.asr.hotwords_file = ""
        oai_env = {"OPENAI_API_KEY": "fake_oai_token_val"}
        oai_res = transcribe(
            _sample_wav(),
            cfg_oai,
            env=oai_env,
            context=AsrContext(hotwords=["whisper_api"]),
            transport=oai_transport,
        )
        self.assertFalse(oai_res.is_single_hop_refined)
        self.assertEqual(oai_res.text, "Whisper 兼容识别结果")
        self.assertEqual(oai_reqs[0].url, "https://api.openai.com/v1/audio/transcriptions")
        self.assertTrue(
            oai_reqs[0].headers["Content-Type"].startswith("multipart/form-data; boundary=")
        )
        self.assertIn(b'name="model"\r\n\r\ngpt-4o-transcribe', oai_reqs[0].body)
        self.assertIn(b'filename="audio.wav"', oai_reqs[0].body)
        self.assertIn(b"RIFF", oai_reqs[0].body)


class SessionCtxTests(unittest.TestCase):
    def test_parse_window_title_hints_and_adjacent_punctuation(self) -> None:
        h1 = parse_window_title_hint("agy · ~/demo-repo [#e6546a7b]")
        assert h1 is not None
        self.assertEqual((h1.cli, h1.session_token, h1.is_full_uuid), ("agy", "e6546a7b", False))

        # Adjacent parentheses / colons MUST still match [#...]!
        h_adj = parse_window_title_hint("agy · demo-repo [#e6546a7b](Admin)")
        assert h_adj is not None
        self.assertEqual((h_adj.cli, h_adj.session_token), ("agy", "e6546a7b"))

        h_colon = parse_window_title_hint("claude · repo [#11223344]:zsh")
        assert h_colon is not None
        self.assertEqual((h_colon.cli, h_colon.session_token), ("claude", "11223344"))

        # Branch/path containing codex-launcher MUST NOT infer cli='codex'
        h_branch = parse_window_title_hint("tools/codex-launcher [#abcdef12]")
        assert h_branch is not None
        self.assertIsNone(h_branch.cli)

        h2 = parse_window_title_hint("Terminal - [claude:#A1B2C3D4]")
        assert h2 is not None
        self.assertEqual((h2.cli, h2.session_token, h2.explicit_cli), ("claude", "a1b2c3d4", True))

        uid = "0196c8e0-1234-7abc-8def-112233445566"
        h3 = parse_window_title_hint(f"⠋ voice-input · Fix audio gate · {uid}")
        assert h3 is not None
        self.assertEqual((h3.cli, h3.session_token, h3.is_full_uuid), ("codex", uid, True))

        self.assertIsNone(parse_window_title_hint("bash - /home/user"))

    def test_three_cli_jsonl_whitelist_cleaning_half_line_rewrites_and_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            proc = root / "proc"
            rt = root / "runtime" / "voice-input-test"
            home.mkdir()
            proc.mkdir()

            # Verify umask 0022 still produces 0700 runtime_dir & sessions dir and 0600 entry!
            old_umask = os.umask(0o022)
            try:
                agy_cid = "e6546a7b-b133-463c-9154-2eb8eac0748c"
                agy_logs = (
                    home
                    / ".gemini"
                    / "antigravity-cli"
                    / "brain"
                    / agy_cid
                    / ".system_generated"
                    / "logs"
                )
                agy_logs.mkdir(parents=True)
                presence_dir = home / ".gemini" / "antigravity-cli" / "presence"
                presence_dir.mkdir(parents=True)
                agy_lock = presence_dir / f"{agy_cid}.lock"
                agy_lock.write_text("", encoding="utf-8")

                # Active agy process holding agy_lock in /proc/100/fd/5
                p100 = proc / "100"
                (p100 / "fd").mkdir(parents=True)
                (p100 / "comm").write_text("agy\n", encoding="utf-8")
                os.symlink(str(root), p100 / "cwd")
                os.symlink(str(agy_lock), p100 / "fd" / "5")

                # Start with compact transcript.jsonl first to test upgrade to transcript_full.jsonl!
                agy_compact = agy_logs / "transcript.jsonl"
                agy_compact.write_text(
                    json.dumps(
                        {
                            "source": "USER_EXPLICIT",
                            "type": "USER_INPUT",
                            # Include literal </USER_REQUEST> inside user prose + <subagent_reminder>
                            # + trailing <ADDITIONAL_METADATA> to test non-truncation & no false subagent kill!
                            "content": (
                                "<USER_REQUEST>\n"
                                "检查 audio_proc.py 与 <USER_REQUEST>内嵌</USER_REQUEST> 及 <subagent_reminder> 字面量\n"
                                "</USER_REQUEST>\n"
                                "<ADDITIONAL_METADATA>secret_meta_noise</ADDITIONAL_METADATA>"
                            ),
                        },
                        ensure_ascii=False,
                    )
                    + "\n",
                    encoding="utf-8",
                )

                reg_entry = write_session_registry_entry(
                    rt, cli="agy", session_id=agy_cid, jsonl_path=str(agy_compact), cwd=str(root)
                )
                self.assertEqual(rt.stat().st_mode & 0o777, 0o700)
                self.assertEqual((rt / "sessions").stat().st_mode & 0o777, 0o700)
                self.assertEqual(reg_entry.stat().st_mode & 0o777, 0o600)
            finally:
                os.umask(old_umask)

            tracker = SessionTracker(home_dir=home, runtime_dir=rt, proc_root=proc)
            snap0 = tracker.sync_session(window_title="agy · demo-repo [#e6546a7b]")
            assert snap0 is not None
            self.assertEqual(
                list(snap0.clean_turns),
                [
                    (
                        "user",
                        "检查 audio_proc.py 与 <USER_REQUEST>内嵌</USER_REQUEST> 及 <subagent_reminder> 字面量",
                    )
                ],
            )
            self.assertIn("audio_proc.py", snap0.hotwords)

            # Create transcript_full.jsonl and verify upgrade does NOT oscillate when passing snap.match!
            agy_file = agy_logs / "transcript_full.jsonl"
            agy_lines = [
                json.dumps({"source": "SYSTEM", "type": "CONVERSATION_HISTORY", "content": "SYS_NOISE"}),
                json.dumps(
                    {
                        "source": "USER_EXPLICIT",
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>\n帮我检查 audio_proc.py 的门控阈值\n</USER_REQUEST>",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "source": "MODEL",
                        "type": "GENERIC",
                        "content": "GIANT_TOOL_OUTPUT_MUST_NEVER_BE_INCLUDED",
                    }
                ),
                json.dumps(
                    {
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "thinking": "more thinking",
                        "content": "门控阈值已对齐 -50 dBFS。",
                    },
                    ensure_ascii=False,
                ),
            ]
            agy_file.write_text("\n".join(agy_lines) + "\n", encoding="utf-8")

            snap_up1 = tracker.sync_session(match=snap0.match)
            assert snap_up1 is not None
            self.assertTrue(snap_up1.match.jsonl_path.endswith("transcript_full.jsonl"))
            gen_after_upgrade = snap_up1.generation
            snap_up2 = tracker.sync_session(match=snap_up1.match)
            assert snap_up2 is not None
            self.assertEqual(snap_up2.generation, gen_after_upgrade)

            # Test Luna delta sync + partial half-line + embedded \r / U+2028 line-separator safety
            delta1, idx1, gen1 = tracker.get_luna_delta(snap_up2.session_key, full=True)
            self.assertIn("帮我检查 audio_proc.py", delta1)
            tracker.mark_luna_synced(snap_up2.session_key, idx1, gen1)

            half_part1 = '{"source": "USER_EXPLICIT",\r "type": "USER_INPUT", "content": "<USER_REQUEST>半行\u2028并发写'
            with open(agy_file, "ab") as f:
                f.write(half_part1.encode("utf-8"))

            snap_half = tracker.sync_session(match=snap_up2.match)
            assert snap_half is not None
            self.assertEqual(snap_half.byte_offset, snap_up2.byte_offset)
            self.assertEqual(len(snap_half.clean_turns), 2)

            half_part2 = '测试完成</USER_REQUEST>"}\n'
            with open(agy_file, "ab") as f:
                f.write(half_part2.encode("utf-8"))

            snap_done = tracker.sync_session(match=snap_up2.match)
            assert snap_done is not None
            self.assertEqual(len(snap_done.clean_turns), 3)
            self.assertEqual(snap_done.clean_turns[-1], ("user", "半行\u2028并发写测试完成"))

            # Equal-length in-place overwrite (same inode, exact same byte size!) MUST reset state!
            old_bytes = agy_file.read_bytes()
            base_rewrite = json.dumps(
                {
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "content": "<USER_REQUEST>同尺寸原地覆写新内容</USER_REQUEST>",
                },
                ensure_ascii=False,
            ).encode("utf-8")
            pad_len = len(old_bytes) - len(base_rewrite) - 1
            self.assertGreater(pad_len, 0)
            same_size_payload = base_rewrite + (b" " * pad_len) + b"\n"
            self.assertEqual(len(same_size_payload), len(old_bytes))
            with open(agy_file, "r+b") as f:
                f.seek(0)
                f.write(same_size_payload)
                f.truncate()

            snap_same_size = tracker.sync_session(match=snap_done.match)
            assert snap_same_size is not None
            self.assertGreater(snap_same_size.generation, snap_done.generation)
            self.assertEqual(list(snap_same_size.clean_turns), [("user", "同尺寸原地覆写新内容")])

            # Prefix overwrite keeping exact same inode, exact same byte size, exact same mtime_ns,
            # AND exact same trailing >64 bytes MUST STILL reset state via prefix blake2b digest!
            line_a_old = (
                '{"source": "USER_EXPLICIT", "type": "USER_INPUT", '
                '"content": "<USER_REQUEST>old-command</USER_REQUEST>"}\n'
            ).encode("utf-8")
            line_b_keep = (
                '{"source": "MODEL", "type": "PLANNER_RESPONSE", '
                '"content": "保留完全相同的尾部超过六十四字节记录内容_0123456789abcdefghijklmnopqrstuvwxyz"}\n'
            ).encode("utf-8")
            self.assertGreaterEqual(len(line_b_keep), 80)
            with open(agy_file, "wb") as f:
                f.write(line_a_old + line_b_keep)
            snap_pref1 = tracker.sync_session(match=snap_done.match)
            assert snap_pref1 is not None
            self.assertEqual(snap_pref1.clean_turns[0], ("user", "old-command"))
            tracker.mark_luna_synced(snap_pref1.session_key, 2, snap_pref1.generation)
            st_before = os.stat(agy_file)

            line_a_new = line_a_old.replace(b"old-command", b"new-command")
            self.assertEqual(len(line_a_new), len(line_a_old))
            with open(agy_file, "r+b") as f:
                f.seek(0)
                f.write(line_a_new + line_b_keep)
            os.utime(agy_file, ns=(st_before.st_atime_ns, st_before.st_mtime_ns))

            snap_pref2 = tracker.sync_session(match=snap_done.match)
            assert snap_pref2 is not None
            self.assertEqual(snap_pref2.generation, snap_pref1.generation + 1)
            self.assertEqual(snap_pref2.luna_synced_idx, 0)
            self.assertEqual(snap_pref2.clean_turns[0], ("user", "new-command"))

            # 2. Claude transcript: inline backtick `<system-reminder>` MUST NOT wipe prose!
            cl_sid = "11223344-5566-7788-99aa-bbccddeeff00"
            cl_dir = home / ".claude" / "projects" / "-tmp-proj"
            cl_dir.mkdir(parents=True)
            cl_file = cl_dir / f"{cl_sid}.jsonl"
            cl_records = [
                {
                    "type": "user",
                    "isSidechain": False,
                    "sessionId": cl_sid,
                    "cwd": str(root),
                    "message": {
                        "role": "user",
                        "content": (
                            "<system-reminder>hook context</system-reminder>"
                            "保留 `<system-reminder>` 中间重要代码说明 `</system-reminder>` 结尾"
                        ),
                    },
                },
                {
                    "type": "assistant",
                    "isSidechain": False,
                    "sessionId": cl_sid,
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "SECRET_THOUGHT"},
                            {"type": "tool_use", "name": "Bash", "input": {}},
                            {"type": "text", "text": "已完成重构。"},
                        ],
                    },
                },
            ]
            cl_file.write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in cl_records) + "\n",
                encoding="utf-8",
            )
            cl_snap = tracker.sync_session(window_title="claude [#11223344]")
            assert cl_snap is not None
            self.assertEqual(
                list(cl_snap.clean_turns),
                [
                    ("user", "保留 `<system-reminder>` 中间重要代码说明 `</system-reminder>` 结尾"),
                    ("assistant", "已完成重构。"),
                ],
            )

            # 3. Codex: 75 KiB session_meta line + response_item -> item_completed stream transition!
            cx_uid = "aaaa1111-2222-3333-4444-555566667777"
            cx_dir = home / ".codex" / "sessions" / "2026" / "11" / "13"
            cx_dir.mkdir(parents=True)
            cx_file = cx_dir / f"rollout-2026-11-13-{cx_uid}.jsonl"
            huge_meta = {
                "type": "session_meta",
                "payload": {
                    "id": cx_uid,
                    "source": "cli",
                    "cwd": str(root),
                    "base_instructions": "A" * 75000,
                },
            }
            resp_legacy = {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "旧版会话历史第一句"}],
                },
            }
            resp_only = {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "首句过渡消息"}],
                },
            }
            cx_file.write_text(
                json.dumps(huge_meta)
                + "\n"
                + json.dumps(resp_legacy, ensure_ascii=False)
                + "\n"
                + json.dumps(resp_only, ensure_ascii=False)
                + "\n",
                encoding="utf-8",
            )
            cx_s1 = tracker.sync_session(window_title=f"codex {cx_uid}")
            assert cx_s1 is not None
            self.assertEqual(
                list(cx_s1.clean_turns),
                [("user", "旧版会话历史第一句"), ("user", "首句过渡消息")],
            )
            tracker.mark_luna_synced(cx_s1.session_key, 2, cx_s1.generation)

            # Non-dialog item_completed (CommandExecution) MUST NOT wipe response_item turns!
            with open(cx_file, "a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "item_completed",
                                "item": {"type": "CommandExecution", "command": "ls"},
                            },
                        }
                    )
                    + "\n"
                )
            cx_s_cmd = tracker.sync_session(window_title=f"codex {cx_uid}")
            assert cx_s_cmd is not None
            self.assertEqual(len(cx_s_cmd.clean_turns), 2)
            self.assertEqual(cx_s_cmd.generation, cx_s1.generation)

            # Now dialog item_completed arrives: must preserve legacy history, dedup tail, bump generation!
            with open(cx_file, "a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "item_completed",
                                "item": {
                                    "type": "UserMessage",
                                    "content": [{"type": "text", "text": "首句过渡消息"}],
                                },
                            },
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            cx_s2 = tracker.sync_session(window_title=f"codex {cx_uid}")
            assert cx_s2 is not None
            self.assertEqual(cx_s2.generation, cx_s1.generation + 1)
            self.assertEqual(cx_s2.luna_synced_idx, 0)
            self.assertEqual(
                list(cx_s2.clean_turns),
                [("user", "旧版会话历史第一句"), ("user", "首句过渡消息")],
            )

    def test_proc_scan_zero_msg_window_and_strict_no_guess_on_unmatched_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            proc = root / "proc"
            rt = root / "runtime"
            repo_dir = root / "my-voice-repo"
            home.mkdir()
            proc.mkdir()
            rt.mkdir()
            repo_dir.mkdir()

            subprocess.run(
                ["git", "init", "-q", str(repo_dir)],
                stdin=subprocess.DEVNULL,
                check=True,
            )
            (repo_dir / "custom_hotword_target.py").write_text("x = 1\n", encoding="utf-8")

            # --- AGY setup: 1 live root session, 1 real-format subagent, 1 dead session (stale lock) ---
            presence_dir = home / ".gemini" / "antigravity-cli" / "presence"
            presence_dir.mkdir(parents=True)
            live_cid = "11112222-3333-4444-5555-666677778888"
            sub_cid = "99998888-7777-6666-5555-444433332222"
            dead_cid = "deadbeef-dead-beef-dead-beefdeadbeef"

            live_lock = presence_dir / f"{live_cid}.lock"
            dead_lock = presence_dir / f"{dead_cid}.lock"
            live_lock.write_text("", encoding="utf-8")
            dead_lock.write_text("", encoding="utf-8")

            for cid, rec in (
                (
                    live_cid,
                    {
                        "source": "USER_EXPLICIT",
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>主窗口会话内容</USER_REQUEST>",
                    },
                ),
                (
                    sub_cid,
                    # Exact real agy subagent first-line structure!
                    {
                        "source": "SYSTEM",
                        "type": "SYSTEM_MESSAGE",
                        "content": (
                            "The following is a <SYSTEM_MESSAGE> not actually sent by the user.\n"
                            "[Message] timestamp=2026-11-13T11:20:03Z sender=00001111-2222 priority=HIGH"
                        ),
                    },
                ),
                (
                    dead_cid,
                    {
                        "source": "USER_EXPLICIT",
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>历史残留会话内容</USER_REQUEST>",
                    },
                ),
            ):
                ldir = (
                    home
                    / ".gemini"
                    / "antigravity-cli"
                    / "brain"
                    / cid
                    / ".system_generated"
                    / "logs"
                )
                ldir.mkdir(parents=True)
                (ldir / "transcript_full.jsonl").write_text(
                    json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8"
                )

            pts_agy = root / "pts_agy"
            pts_agy.write_text("", encoding="utf-8")
            os.utime(pts_agy, (1000.0, 1000.0))
            p101 = proc / "101"
            (p101 / "fd").mkdir(parents=True)
            (p101 / "comm").write_text("agy\n", encoding="utf-8")
            os.symlink(str(repo_dir), p101 / "cwd")
            os.symlink(str(pts_agy), p101 / "fd" / "0")
            os.symlink(str(live_lock), p101 / "fd" / "5")

            # --- Codex setup: 1 root CLI session + 1 subagent rollout open in /proc/202 ---
            cx_dir = home / ".codex" / "sessions" / "2026" / "11" / "13"
            cx_dir.mkdir(parents=True)
            cx_root_id = "cccc1111-2222-3333-4444-555566667777"
            cx_sub_id = "dddd1111-2222-3333-4444-555566667777"
            cx_root_f = cx_dir / f"rollout-2026-11-13-{cx_root_id}.jsonl"
            cx_sub_f = cx_dir / f"rollout-2026-11-13-{cx_sub_id}.jsonl"
            cx_root_f.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": cx_root_id, "source": "cli", "cwd": "/tmp/other"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            cx_sub_f.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": cx_sub_id,
                            "source": {"subagent": "review"},
                            "cwd": str(repo_dir),
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (home / ".codex" / "session_index.jsonl").write_text(
                json.dumps({"id": cx_root_id, "thread_name": "UniqueThreadTopic"}) + "\n",
                encoding="utf-8",
            )

            pts_cx = root / "pts_cx"
            pts_cx.write_text("", encoding="utf-8")
            os.utime(pts_cx, (2000.0, 2000.0))
            p202 = proc / "202"
            (p202 / "fd").mkdir(parents=True)
            (p202 / "comm").write_text("codex\n", encoding="utf-8")
            os.symlink("/tmp/other", p202 / "cwd")
            os.symlink(str(pts_cx), p202 / "fd" / "0")
            os.symlink(str(cx_root_f), p202 / "fd" / "3")
            os.symlink(str(cx_sub_f), p202 / "fd" / "4")

            tracker = SessionTracker(home_dir=home, runtime_dir=rt, proc_root=proc)

            # Explicit session tags for subagent, dead lock, or unknown prefix MUST return None
            # (NEVER cross-attaching to unrelated active sessions!)
            self.assertIsNone(tracker.resolve_session("[agy:#99998888]"))
            self.assertIsNone(tracker.resolve_session("[agy:#deadbeef]"))
            self.assertIsNone(tracker.resolve_session(f"[agy:#{dead_cid}]"))
            self.assertIsNone(tracker.resolve_session("[claude:#77777777]"))

            # When ALL agy processes have exited (empty /proc) OR on macOS (no /proc),
            # stale dead_cid lock MUST STILL be rejected!
            empty_proc = root / "empty_proc"
            empty_proc.mkdir()
            tracker_exited = SessionTracker(home_dir=home, runtime_dir=rt, proc_root=empty_proc)
            self.assertIsNone(tracker_exited.resolve_session("[agy:#deadbeef]"))

            tracker_macos = SessionTracker(
                home_dir=home, runtime_dir=rt, proc_root=root / "nonexistent_proc"
            )
            self.assertIsNone(tracker_macos.resolve_session("[agy:#deadbeef]"))

            # 0-message newly opened Claude window (registry written by SessionStart, JSONL not yet created!)
            zero_sid = "00001111-2222-3333-4444-555566667777"
            expected_zero_jsonl = home / ".claude" / "projects" / "-tmp" / f"{zero_sid}.jsonl"
            write_session_registry_entry(
                rt,
                cli="claude",
                session_id=zero_sid,
                jsonl_path=str(expected_zero_jsonl),
                cwd=str(repo_dir),
            )
            snap_zero = tracker.sync_session(window_title="claude · my-voice-repo [#00001111]")
            assert snap_zero is not None
            self.assertEqual(snap_zero.match.session_id, zero_sid)
            self.assertEqual(snap_zero.clean_turns, ())
            self.assertIn("custom_hotword_target.py", snap_zero.hotwords)

            # Disambiguation when title has NO session tag:
            # Path containing 'codex-launcher' MUST NOT boost codex when agy matches repo basename!
            snap_agy = tracker.sync_session(
                window_title="Terminal - tools/codex-launcher - my-voice-repo"
            )
            assert snap_agy is not None
            self.assertEqual((snap_agy.match.cli, snap_agy.match.session_id), ("agy", live_cid))

            m_topic = tracker.resolve_session("Terminal - UniqueThreadTopic")
            assert m_topic is not None
            self.assertEqual((m_topic.cli, m_topic.session_id), ("codex", cx_root_id))

            m_generic = tracker.resolve_session("Windows PowerShell")
            assert m_generic is not None
            self.assertEqual((m_generic.cli, m_generic.session_id), ("codex", cx_root_id))

            # Full UUID lookup for agy backfills live /proc cwd so git hotwords work without registry!
            snap_agy_uuid = tracker.sync_session(window_title=f"agy [{live_cid}]")
            assert snap_agy_uuid is not None
            self.assertEqual(snap_agy_uuid.match.cwd, str(repo_dir))
            self.assertIn("custom_hotword_target.py", snap_agy_uuid.hotwords)

            # Headless background agy-run (--input-format stream-json) & claude sdk-cli / agent-*.jsonl
            # MUST NEVER hijack foreground session resolution!
            bg_agy_cid = "bbbb2222-3333-4444-5555-666677778888"
            bg_agy_lock = presence_dir / f"{bg_agy_cid}.lock"
            bg_agy_lock.write_text("", encoding="utf-8")
            bg_agy_logs = home / ".gemini" / "antigravity-cli" / "brain" / bg_agy_cid / ".system_generated" / "logs"
            bg_agy_logs.mkdir(parents=True)
            bg_agy_f = bg_agy_logs / "transcript_full.jsonl"
            bg_agy_f.write_text(
                json.dumps(
                    {
                        "source": "USER_EXPLICIT",
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>\n[agy-run] Workspace: /tmp\n</USER_REQUEST>",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            os.utime(bg_agy_f, (9999.0, 9999.0))
            p303 = proc / "303"
            (p303 / "fd").mkdir(parents=True)
            (p303 / "comm").write_text("agy\n", encoding="utf-8")
            (p303 / "cmdline").write_bytes(b"agy\x00--input-format\x00stream-json\x00")
            os.symlink(str(repo_dir), p303 / "cwd")
            os.symlink("/dev/null", p303 / "fd" / "0")
            os.symlink(str(bg_agy_lock), p303 / "fd" / "3")

            self.assertIsNone(tracker.resolve_session("[agy:#bbbb2222]"))
            pts_bg = root / "pts_bg"
            pts_bg.write_text("", encoding="utf-8")
            os.utime(pts_bg, (9999.0, 9999.0))
            cx_headless_id = "cccc3333-4444-5555-6666-777788889999"
            cx_headless_f = cx_dir / f"rollout-2026-11-13T10-05-00-{cx_headless_id}.jsonl"
            cx_headless_f.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": cx_headless_id, "source": "cli", "cwd": str(repo_dir)},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            os.utime(cx_headless_f, (9999.0, 9999.0))
            p304 = proc / "304"
            (p304 / "fd").mkdir(parents=True)
            (p304 / "comm").write_text("codex\n", encoding="utf-8")
            (p304 / "cmdline").write_bytes(
                b"codex\x00--disable\x00shell_tool\x00--enable\x00skip_host_skill_discovery\x00app-server\x00--stdio\x00"
            )
            os.symlink(str(repo_dir), p304 / "cwd")
            os.symlink(str(pts_bg), p304 / "fd" / "0")
            os.symlink(str(cx_headless_f), p304 / "fd" / "3")
            m_still_interactive = tracker.resolve_session("Windows PowerShell")
            assert m_still_interactive is not None
            self.assertEqual(m_still_interactive.session_id, cx_root_id)

            # Registry path traversal, 0777 untrusted runtime_dir, and multithreaded sync safety!
            with self.assertRaises(ValueError):
                write_session_registry_entry(
                    rt, cli="../evil", session_id=zero_sid, jsonl_path="/tmp/x"
                )
            with self.assertRaises(ValueError):
                write_session_registry_entry(
                    rt, cli="claude", session_id="../../bad", jsonl_path="/tmp/x"
                )

            bad_rt = root / "world_writable_rt"
            bad_rt.mkdir()
            write_session_registry_entry(
                bad_rt, cli="claude", session_id=zero_sid, jsonl_path=str(expected_zero_jsonl)
            )
            os.chmod(bad_rt, 0o777)
            tracker_untrusted = SessionTracker(
                home_dir=home, runtime_dir=bad_rt, proc_root=empty_proc
            )
            self.assertIsNone(tracker_untrusted.resolve_session("[claude:#00001111]"))

            errs: list[Exception] = []

            def worker() -> None:
                try:
                    for _ in range(15):
                        s = tracker.sync_session(window_title=f"agy [{live_cid}]")
                        assert s is not None
                        _, idx, gen = tracker.get_luna_delta(s.session_key)
                        tracker.mark_luna_synced(s.session_key, idx, gen)
                except Exception as e:  # pragma: no cover
                    errs.append(e)

            threads = [threading.Thread(target=worker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errs, [])

    def test_nonproc_flock_presence_detection_including_readonly_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_file = Path(tmp) / "test.lock"
            lock_file.write_text("", encoding="utf-8")
            self.assertFalse(is_agy_presence_lock_held_nonproc(lock_file))

            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "import fcntl, os, sys, time\n"
                        "f = open(sys.argv[1], 'r+')\n"
                        "fcntl.flock(f.fileno(), fcntl.LOCK_EX)\n"
                        "os.chmod(sys.argv[1], 0o400)\n"
                        "sys.stdout.write('READY\\n')\n"
                        "sys.stdout.flush()\n"
                        "time.sleep(10)\n"
                    ),
                    str(lock_file),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            try:
                assert proc.stdout is not None
                ready = proc.stdout.readline()
                self.assertEqual(ready.strip(), b"READY")
                # Even with mode 0400, O_RDONLY flock probe accurately detects held LOCK_EX!
                self.assertTrue(is_agy_presence_lock_held_nonproc(lock_file))
            finally:
                os.chmod(lock_file, 0o600)
                proc.terminate()
                proc.wait(timeout=5)
                if proc.stdout is not None:
                    proc.stdout.close()

    def test_runtime_session_registry_prioritizes_agent_voice_input_with_legacy_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            xdg = root / "xdg"
            empty_proc = root / "empty_proc"
            home.mkdir()
            xdg.mkdir()
            empty_proc.mkdir()

            new_rt = xdg / "agent-voice-input"
            old_rt = xdg / "voice-input"
            old_uid_rt = xdg / f"voice-input-{os.getuid()}"

            sid_legacy_uid = "aaaa0001-1111-4222-8333-444455556666"
            sid_legacy = "bbbb0002-1111-4222-8333-444455556666"
            sid_shared_old = "cccc0003-0000-4000-8000-000000000001"
            sid_shared_new = "cccc0003-9999-4999-8999-999999999999"

            write_session_registry_entry(
                old_uid_rt,
                cli="claude",
                session_id=sid_legacy_uid,
                jsonl_path=str(home / ".claude" / "projects" / "-p" / f"{sid_legacy_uid}.jsonl"),
                cwd="/tmp/legacy-uid",
            )
            write_session_registry_entry(
                old_rt,
                cli="claude",
                session_id=sid_legacy,
                jsonl_path=str(home / ".claude" / "projects" / "-p" / f"{sid_legacy}.jsonl"),
                cwd="/tmp/legacy",
            )
            write_session_registry_entry(
                old_rt,
                cli="claude",
                session_id=sid_shared_old,
                jsonl_path=str(home / ".claude" / "projects" / "-p" / f"{sid_shared_old}.jsonl"),
                cwd="/tmp/old-shared",
            )

            # Auto-resolved runtime_dir (new_rt doesn't exist yet!) seamlessly finds legacy registries
            tracker = SessionTracker(
                home_dir=home,
                env={"HOME": str(home), "XDG_RUNTIME_DIR": str(xdg)},
                runtime_dir=new_rt,
                proc_root=empty_proc,
            )
            m_uid = tracker.resolve_session("claude [#aaaa0001]")
            self.assertIsNotNone(m_uid)
            assert m_uid is not None
            self.assertEqual(m_uid.session_id, sid_legacy_uid)

            m_old = tracker.resolve_session("claude [#bbbb0002]")
            self.assertIsNotNone(m_old)
            assert m_old is not None
            self.assertEqual(m_old.session_id, sid_legacy)

            # Once new_rt registers the same prefix, agent-voice-input/sessions strictly wins over legacy!
            write_session_registry_entry(
                new_rt,
                cli="claude",
                session_id=sid_shared_new,
                jsonl_path=str(home / ".claude" / "projects" / "-p" / f"{sid_shared_new}.jsonl"),
                cwd="/tmp/new-shared",
            )
            m_shared = tracker.resolve_session("claude [#cccc0003]")
            self.assertIsNotNone(m_shared)
            assert m_shared is not None
            self.assertEqual(m_shared.session_id, sid_shared_new)
            self.assertEqual(m_shared.cwd, "/tmp/new-shared")

    def test_ghostty_multi_split_tty_zero_hook_isolation_and_cwd_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            rt = root / "rt"
            no_proc = root / "no_proc"
            proj_alpha = home / "repos" / "proj-alpha"
            proj_beta = home / "repos" / "proj-beta"
            proj_alpha.mkdir(parents=True)
            proj_beta.mkdir(parents=True)
            rt.mkdir(parents=True)

            # Set up 2 concurrent sessions per CLI (proj_beta deliberately given newer mtime everywhere!)
            now = time.time()

            # Codex sessions
            cx_dir = home / ".codex" / "sessions" / "2026" / "11" / "14"
            cx_dir.mkdir(parents=True)
            cx_alpha = "aaaa1111-0000-4000-8000-000000000001"
            cx_beta = "bbbb2222-0000-4000-8000-000000000002"
            f_cx_alpha = cx_dir / f"rollout-2026-11-14-{cx_alpha}.jsonl"
            f_cx_beta = cx_dir / f"rollout-2026-11-14-{cx_beta}.jsonl"
            f_cx_alpha.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": cx_alpha, "source": "cli", "cwd": str(proj_alpha)}}) + "\n",
                encoding="utf-8",
            )
            f_cx_beta.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": cx_beta, "source": "cli", "cwd": str(proj_beta)}}) + "\n",
                encoding="utf-8",
            )
            os.utime(f_cx_alpha, (now - 120, now - 120))
            os.utime(f_cx_beta, (now - 5, now - 5))

            # Claude sessions
            enc_alpha = encode_claude_project_dir(str(proj_alpha))[0]
            enc_beta = encode_claude_project_dir(str(proj_beta))[0]
            cl_dir_a = home / ".claude" / "projects" / enc_alpha
            cl_dir_b = home / ".claude" / "projects" / enc_beta
            cl_dir_a.mkdir(parents=True)
            cl_dir_b.mkdir(parents=True)
            cl_alpha = "1111aaaa-0000-4000-8000-000000000001"
            cl_beta = "2222bbbb-0000-4000-8000-000000000002"
            f_cl_alpha = cl_dir_a / f"{cl_alpha}.jsonl"
            f_cl_beta = cl_dir_b / f"{cl_beta}.jsonl"
            f_cl_alpha.write_text(
                json.dumps({"type": "user", "isSidechain": False, "sessionId": cl_alpha, "cwd": str(proj_alpha), "message": {"role": "user", "content": "alpha turn"}}) + "\n",
                encoding="utf-8",
            )
            f_cl_beta.write_text(
                json.dumps({"type": "user", "isSidechain": False, "sessionId": cl_beta, "cwd": str(proj_beta), "message": {"role": "user", "content": "beta turn"}}) + "\n",
                encoding="utf-8",
            )
            os.utime(f_cl_alpha, (now - 120, now - 120))
            os.utime(f_cl_beta, (now - 5, now - 5))

            # Antigravity (agy) sessions with active flock + transcript Working Directory
            presence_dir = home / ".gemini" / "antigravity-cli" / "presence"
            presence_dir.mkdir(parents=True)
            agy_alpha = "3333cccc-0000-4000-8000-000000000001"
            agy_beta = "4444dddd-0000-4000-8000-000000000002"
            for cid, pdir, age in ((agy_alpha, proj_alpha, 120), (agy_beta, proj_beta, 5)):
                lf = presence_dir / f"{cid}.lock"
                lf.write_text("", encoding="utf-8")
                logs_dir = home / ".gemini" / "antigravity-cli" / "brain" / cid / ".system_generated" / "logs"
                logs_dir.mkdir(parents=True)
                tf = logs_dir / "transcript.jsonl"
                tf.write_text(
                    json.dumps(
                        {
                            "source": "USER_EXPLICIT",
                            "type": "USER_INPUT",
                            "content": f"<user_information>\nCommand Working Directory: {pdir}\n</user_information>\n<USER_REQUEST>fix {pdir.name}</USER_REQUEST>",
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                os.utime(tf, (now - age, now - age))

            fd_agy_a = os.open(str(presence_dir / f"{agy_alpha}.lock"), os.O_RDWR)
            fd_agy_b = os.open(str(presence_dir / f"{agy_beta}.lock"), os.O_RDWR)
            try:
                fcntl.flock(fd_agy_a, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd_agy_b, fcntl.LOCK_EX | fcntl.LOCK_NB)

                tracker = SessionTracker(
                    home_dir=home,
                    env={"HOME": str(home)},
                    runtime_dir=rt,
                    proc_root=no_proc,
                )

                tty_tables: dict[str, str] = {}
                pid_rows: dict[int, str] = {}
                lsof_rows: dict[int, str] = {}

                def fake_subproc(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
                    self.assertIs(kwargs.get("stdin"), subprocess.DEVNULL)
                    exe = os.path.basename(cmd[0])
                    if exe == "ps" and "-t" in cmd:
                        t_short = cmd[cmd.index("-t") + 1]
                        out = tty_tables.get(t_short, "")
                        return subprocess.CompletedProcess(cmd, 0 if out else 1, out.encode("utf-8"), b"")
                    if exe == "ps" and "-p" in cmd:
                        pid_q = int(cmd[cmd.index("-p") + 1])
                        out = pid_rows.get(pid_q, "")
                        return subprocess.CompletedProcess(cmd, 0 if out else 1, out.encode("utf-8"), b"")
                    if exe == "lsof" and "-p" in cmd:
                        pid_q = int(cmd[cmd.index("-p") + 1])
                        out = lsof_rows.get(pid_q, "")
                        return subprocess.CompletedProcess(cmd, 0 if out else 1, out.encode("utf-8"), b"")
                    if exe == "git":
                        return subprocess.CompletedProcess(cmd, 1, b"", b"")
                    raise AssertionError(f"unexpected cmd: {cmd}")

                with mock.patch("agent_voice_input.session_ctx.subprocess.run", side_effect=fake_subproc):
                    # 1. Two Codex splits (plus a headless `codex exec` worker on ttys011 that must be ignored)
                    tty_tables["ttys011"] = (
                        "5001 600 Ss ttys011 -zsh\n"
                        "5010 5001 S+ ttys011 /Users/dev/.local/bin/codex\n"
                        "5015 5010 S ttys011 /Users/dev/.local/bin/codex exec check\n"
                    )
                    tty_tables["ttys012"] = (
                        "6001 600 Ss ttys012 -zsh\n"
                        "6010 6001 S+ ttys012 /Users/dev/.local/bin/codex\n"
                    )
                    m_cx_1 = tracker.resolve_session(
                        "Ghostty", window_tty="/dev/ttys011", window_cwd=f"file://localhost{proj_alpha}/"
                    )
                    m_cx_2 = tracker.resolve_session(
                        "Ghostty", window_tty="/dev/ttys012", window_cwd=str(proj_beta)
                    )
                    assert m_cx_1 is not None and m_cx_2 is not None
                    self.assertEqual((m_cx_1.cli, m_cx_1.session_id, m_cx_1.matched_by), ("codex", cx_alpha, "tty_ps"))
                    self.assertEqual((m_cx_2.cli, m_cx_2.session_id, m_cx_2.matched_by), ("codex", cx_beta, "tty_ps"))

                    # 2. Two Claude splits (node wrapper + native binary; headless `claude -p` ignored)
                    tty_tables["ttys011"] = (
                        "5001 600 Ss ttys011 -zsh\n"
                        "5020 5001 S+ ttys011 node /opt/claude/versions/2.1.12\n"
                        "5025 5020 S ttys011 claude -p subcheck\n"
                    )
                    tty_tables["ttys012"] = (
                        "6001 600 Ss ttys012 -zsh\n"
                        "6020 6001 S+ ttys012 claude\n"
                    )
                    m_cl_1 = tracker.resolve_session(
                        "zsh", window_tty="/dev/ttys011", window_cwd=str(proj_alpha)
                    )
                    m_cl_2 = tracker.resolve_session(
                        "zsh", window_tty="/dev/ttys012", window_cwd=str(proj_beta)
                    )
                    assert m_cl_1 is not None and m_cl_2 is not None
                    self.assertEqual((m_cl_1.cli, m_cl_1.session_id, m_cl_1.matched_by), ("claude", cl_alpha, "tty_ps"))
                    self.assertEqual((m_cl_2.cli, m_cl_2.session_id, m_cl_2.matched_by), ("claude", cl_beta, "tty_ps"))

                    # 3. Two Antigravity (agy) splits (headless `agy -p` ignored)
                    tty_tables["ttys011"] = (
                        "5001 600 Ss ttys011 -zsh\n"
                        "5030 5001 S+ ttys011 /Users/dev/.local/bin/agy\n"
                        "5035 5030 S ttys011 /Users/dev/.local/bin/agy -p quick\n"
                    )
                    tty_tables["ttys012"] = (
                        "6001 600 Ss ttys012 -zsh\n"
                        "6030 6001 S+ ttys012 antigravity\n"
                    )
                    m_ag_1 = tracker.resolve_session(
                        "Ghostty", window_tty="/dev/ttys011", window_cwd=str(proj_alpha)
                    )
                    m_ag_2 = tracker.resolve_session(
                        "Ghostty", window_tty="/dev/ttys012", window_cwd=str(proj_beta)
                    )
                    assert m_ag_1 is not None and m_ag_2 is not None
                    self.assertEqual((m_ag_1.cli, m_ag_1.session_id, m_ag_1.matched_by), ("agy", agy_alpha, "tty_ps"))
                    self.assertEqual((m_ag_2.cli, m_ag_2.session_id, m_ag_2.matched_by), ("agy", agy_beta, "tty_ps"))

                    # 3b. Real macOS agy transcripts omit cwd in JSONL: lsof PID -> presence/<cid>.lock disambiguates even in SAME cwd!
                    lsof_rows[5030] = f"p5030\nn{presence_dir / (agy_beta + '.lock')}\n"
                    m_ag_lsof = tracker.resolve_session(
                        "Ghostty", window_tty="/dev/ttys011", window_cwd=str(proj_alpha)
                    )
                    assert m_ag_lsof is not None
                    self.assertEqual((m_ag_lsof.cli, m_ag_lsof.session_id, m_ag_lsof.matched_by), ("agy", agy_beta, "tty_ps"))
                    lsof_rows.clear()

                    # 4. Idle shell split (no agent on ttys013) MUST NOT steal neighbor split's session!
                    tty_tables["ttys013"] = "7001 600 Ss ttys013 -zsh\n"
                    self.assertIsNone(
                        tracker.resolve_session(
                            "zsh", window_tty="/dev/ttys013", window_cwd=str(proj_alpha)
                        )
                    )

                    # 4b. Unquoted ps args with paired apostrophes around headless `-p` / `exec` MUST NOT be swallowed!
                    tty_tables["ttys014"] = (
                        "7101 600 Ss ttys014 -zsh\n"
                        "7110 7101 S+ ttys014 claude --append-system-prompt don't -p can't\n"
                        "7111 7101 S ttys014 codex -c note=don't exec can't\n"
                        "7112 7101 S ttys014 agy --note don't -p can't\n"
                    )
                    self.assertIsNone(
                        tracker.resolve_session(
                            "zsh", window_tty="/dev/ttys014", window_cwd=str(proj_alpha)
                        )
                    )

                    # 4c. TTY-less GUI window_pid (e.g. Ghostty app_pid) MUST NOT pollute live_pids on empty tty
                    tty_tables["ttys015"] = ""
                    pid_rows[9999] = "9999 1 S ?? /Applications/Ghostty.app/Contents/MacOS/ghostty\n"
                    with mock.patch("agent_voice_input.session_ctx._is_pid_alive", side_effect=lambda p: p == 5020):
                        # 5. Priority 1: Explicit #tag in window title still takes top precedence over window_tty
                        m_explicit = tracker.resolve_session(
                            f"claude · [#{cl_beta[:8]}]",
                            window_tty="/dev/ttys011",
                            window_cwd=str(proj_alpha),
                        )
                        assert m_explicit is not None
                        self.assertEqual(m_explicit.session_id, cl_beta)
                        self.assertEqual(m_explicit.matched_by, "title_prefix")

                        # 6. Priority 2a: Registry tty_registry hit vs stale dead PID rejection + normalized cwd
                        tty_tables["ttys011"] = (
                            "5001 600 Ss ttys011 -zsh\n"
                            "5020 5001 S+ ttys011 claude\n"
                        )
                        reg_path = write_session_registry_entry(
                            rt,
                            cli="claude",
                            session_id=cl_alpha,
                            jsonl_path=str(f_cl_alpha),
                            cwd=f"file://localhost{proj_alpha}/",
                            tty="ttys011",
                            pid=5020,
                        )
                        assert reg_path is not None
                        self.assertEqual(json.loads(reg_path.read_text(encoding="utf-8"))["cwd"], str(proj_alpha))
                        self.assertEqual(encode_claude_project_dir(f"{proj_alpha}/"), encode_claude_project_dir(str(proj_alpha)))
                        self.assertEqual(
                            encode_claude_project_dir(f"file://localhost{proj_alpha}/"),
                            encode_claude_project_dir(str(proj_alpha)),
                        )

                        m_reg = tracker.resolve_session(
                            "zsh", window_tty="/dev/ttys011", window_cwd=str(proj_alpha)
                        )
                        assert m_reg is not None
                        self.assertEqual((m_reg.session_id, m_reg.matched_by), (cl_alpha, "tty_registry"))

                        # Stale registry entry with dead PID (888888) on idle shell ttys013 is ignored
                        write_session_registry_entry(
                            rt,
                            cli="claude",
                            session_id=cl_beta,
                            jsonl_path=str(f_cl_beta),
                            cwd=str(proj_beta),
                            tty="/dev/ttys013",
                            pid=888888,
                        )
                        self.assertIsNone(
                            tracker.resolve_session(
                                "zsh", window_tty="/dev/ttys013", window_cwd=str(proj_beta)
                            )
                        )

                        # Empty ps -t table (e.g. inside multiplexer) + TTY-less GUI window_pid=9999 falls through
                        # cleanly to Priority 3 non-/proc fallback instead of treating GUI PID 9999 as a live shell PID!
                        m_mux_fb = tracker.resolve_session(
                            "claude", window_tty="/dev/ttys015", window_cwd=str(proj_alpha), window_pid=9999
                        )
                        assert m_mux_fb is not None
                        self.assertEqual(m_mux_fb.session_id, cl_alpha)

                        # 7. Priority 3: No window_tty, but window_cwd provided -> disambiguates to proj_alpha
                        #    (including a freshly opened 0-message Claude session in a third project!)
                        m_cwd_only = tracker.resolve_session(
                            "claude", window_tty="", window_cwd=str(proj_alpha)
                        )
                        assert m_cwd_only is not None
                        self.assertEqual(m_cwd_only.cli, "claude")
                        self.assertEqual(m_cwd_only.session_id, cl_alpha)

                        proj_gamma = home / "repos" / "gamma-empty"
                        proj_gamma.mkdir(parents=True)
                        cl_gamma = "66660003-cccc-4ddd-8eee-ffff00003333"
                        cl_dir_g = home / ".claude" / "projects" / encode_claude_project_dir(str(proj_gamma))[0]
                        cl_dir_g.mkdir(parents=True, exist_ok=True)
                        f_cl_gamma = cl_dir_g / f"{cl_gamma}.jsonl"
                        f_cl_gamma.write_bytes(b"")
                        os.utime(f_cl_gamma, (now, now))
                        m_cwd_zero = tracker.resolve_session(
                            "claude", window_tty="", window_cwd=str(proj_gamma)
                        )
                        assert m_cwd_zero is not None
                        self.assertEqual((m_cwd_zero.cli, m_cwd_zero.session_id), ("claude", cl_gamma))
            finally:
                os.close(fd_agy_a)
                os.close(fd_agy_b)


if __name__ == "__main__":
    unittest.main()
