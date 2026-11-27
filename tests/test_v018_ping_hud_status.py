"""Unit tests for v0.18.0: model-level ping, cold-start preflight, refiner-fallback HUD, and status/JSON contracts."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

from agent_voice_input import cli as cli_mod
from agent_voice_input.asr_providers import AsrResult, BaseAsrProvider
from agent_voice_input.config import AsrConfig, Config, HotkeyConfig, RefinerConfig, UiConfig, resolve_credentials
from agent_voice_input.control_ipc import _sanitize_hotwords_status_counts
from agent_voice_input.daemon import VoiceInputDaemon
from agent_voice_input.daemon_pipeline import (
    classify_delivered_commit_hud,
    plan_commit_hud_calls,
)
from agent_voice_input.diagnose import collect_live_diagnostics
from agent_voice_input.http_transport import HttpRequest, HttpResponse
from agent_voice_input.hud import (
    default_hud_duration_ms,
    format_hud_text,
    format_refiner_fallback_hud_text,
    hud_layer_for_state,
    is_toast_hud_state,
)
from agent_voice_input.platform_common import ForegroundWindow, PasteOutcome
from agent_voice_input.refiner import BaseRefiner, RefinerResult, create_refiner
from agent_voice_input.session_ctx import SessionTracker


class _FakeWavRecorderPlatform:
    def __init__(self, *, paste_outcome: PasteOutcome | None = None, lang: str = "zh-CN") -> None:
        self.fg = ForegroundWindow(hwnd=11, process="Ghostty", title="codex")
        self.paste_outcome = paste_outcome or PasteOutcome(True, True, "ok", "Ghostty", False)
        self.hud_calls: list[tuple[str, str, int, str | None, bool | None, bool | None, int]] = []
        self.pasted_texts: list[str] = []
        self._lang = lang
        self.last_audio_error = ""
        self.last_shim_error = ""

    def _host_ui_culture(self) -> str:
        return self._lang

    def is_shim_ok(self) -> bool:
        return True

    def set_recorder_lost_callback(self, cb) -> None:
        self._lost_cb = cb

    def set_recorder_recovered_callback(self, cb) -> None:
        self._rec_cb = cb

    def start_event_listener(self, cb) -> None:
        self._on_hk = cb

    def is_allowed_terminal(self, win: ForegroundWindow) -> bool:
        return True

    def get_foreground_window(self) -> ForegroundWindow:
        return self.fg

    def resolve_foreground_window(self, hint: ForegroundWindow | None = None) -> ForegroundWindow:
        return hint if (hint and hint.process) else self.fg

    def start_recording(self, runtime_dir: Path, *, loopback: bool = False, seq_id: int | None = None) -> None:
        del runtime_dir, loopback, seq_id

    def stop_recording(self, *, drain: bool = True) -> tuple[bytes, str]:
        del drain
        from agent_voice_input.audio_proc import pack_pcm16_mono_wav
        import math

        # 0.6s 440Hz tone so acoustic gate passes cleanly offline
        samples = [int(12000 * math.sin(2.0 * math.pi * 440.0 * i / 16000.0)) for i in range(9600)]
        return pack_pcm16_mono_wav(samples, sample_rate=16000), "fake_mic"

    def cancel_recording(self) -> None:
        pass

    def hud(
        self,
        state: str,
        text: str = "",
        duration_ms: int = 0,
        *,
        layer: str | None = None,
        clear_main: bool | None = None,
        clear_toast: bool | None = None,
        bg_count: int = 0,
    ) -> None:
        self.hud_calls.append((state, text, duration_ms, layer, clear_main, clear_toast, bg_count))

    def beep(self, kind: str) -> None:
        del kind

    def deliver_text(self, text: str, seq_id: int, target_hwnd: int = 0, **kwargs) -> PasteOutcome:
        del seq_id, target_hwnd, kwargs
        self.pasted_texts.append(text)
        return self.paste_outcome

    def close(self) -> None:
        pass


class _StubAsr(BaseAsrProvider):
    provider_name = "dashscope"

    def __init__(self, text: str = "原始口述内容") -> None:
        self._text = text

    def transcribe(self, wav_bytes: bytes, *, context=None, is_cancelled=None, cancel_token=None) -> AsrResult:
        del wav_bytes, context, is_cancelled, cancel_token
        return AsrResult(text=self._text, provider="dashscope", model="qwen3-asr-flash", latency_ms=12)


class _FailingFallbackRefiner(BaseRefiner):
    provider_name = "deepseek"

    def __init__(self, warning: str) -> None:
        self._warning = warning

    def refine(self, asr_input, **kwargs) -> RefinerResult:
        del kwargs
        draft = asr_input.text if isinstance(asr_input, AsrResult) else str(asr_input)
        return RefinerResult(
            text=draft,
            provider="deepseek",
            model="deepseek-flash",
            latency_ms=15,
            used_fallback="asr_draft",
            warning=self._warning,
        )


class ModelLevelPingAndPreflightTests(unittest.TestCase):
    def test_gemini_page_size_and_model_list_verification_matrix(self) -> None:
        # 1. Gemini URL uses pageSize=1000
        g_url, g_hdrs = cli_mod._build_ping_http_spec("gemini", "", "AIza-test-key-0001")
        self.assertIn("pageSize=1000", g_url)
        self.assertNotIn("pageSize=1&", g_url)
        self.assertFalse(g_url.endswith("pageSize=1"))
        self.assertEqual(g_hdrs["x-goog-api-key"], "AIza-test-key-0001")

        cfg_g = Config(asr=AsrConfig(provider="gemini", model="gemini-3.7-flash"))
        env_g = {"GEMINI_API_KEY": "AIza-test-key-0001"}
        creds_g = resolve_credentials(cfg_g, env_g)

        # 2. Gemini model present (stripped models/ prefix) -> OK
        st_ok, _, msg_ok = cli_mod._probe_stage_ping(
            stage="asr",
            provider="gemini",
            cfg=cfg_g,
            creds=creds_g,
            env=env_g,
            base_url=cfg_g.asr.effective_base_url("gemini"),
            transport=lambda _req: HttpResponse(
                200,
                {},
                json.dumps({"models": [{"name": "models/gemini-3.7-flash"}]}).encode("utf-8"),
            ),
            cache={},
            target_model="gemini-3.7-flash",
        )
        self.assertEqual(st_ok, "OK", msg_ok)

        # 3. Gemini model missing + complete list -> FAIL
        st_miss, _, msg_miss = cli_mod._probe_stage_ping(
            stage="asr",
            provider="gemini",
            cfg=cfg_g,
            creds=creds_g,
            env=env_g,
            base_url=cfg_g.asr.effective_base_url("gemini"),
            transport=lambda _req: HttpResponse(
                200,
                {},
                json.dumps({"models": [{"name": "models/gemini-2.5-pro"}]}).encode("utf-8"),
            ),
            cache={},
            target_model="gemini-3.7-flash",
        )
        self.assertEqual(st_miss, "FAIL")
        self.assertIn("not enabled or not found", msg_miss)
        self.assertIn("gemini-3.7-flash", msg_miss)

        # 4. Gemini paginated response (nextPageToken present) -> NO false positive
        st_page, _, msg_page = cli_mod._probe_stage_ping(
            stage="asr",
            provider="gemini",
            cfg=cfg_g,
            creds=creds_g,
            env=env_g,
            base_url=cfg_g.asr.effective_base_url("gemini"),
            transport=lambda _req: HttpResponse(
                200,
                {},
                json.dumps(
                    {"models": [{"name": "models/gemini-2.5-pro"}], "nextPageToken": "tok-next"}
                ).encode("utf-8"),
            ),
            cache={},
            target_model="gemini-3.7-flash",
        )
        self.assertEqual(st_page, "OK", msg_page)

        # 5. Empty models list or non-JSON -> NO false positive
        for raw_body in (b'{"models":[]}', b"not-json", b'{"unexpected":1}'):
            st_emp, _, _ = cli_mod._probe_stage_ping(
                stage="asr",
                provider="gemini",
                cfg=cfg_g,
                creds=creds_g,
                env=env_g,
                base_url=cfg_g.asr.effective_base_url("gemini"),
                transport=lambda _req, b=raw_body: HttpResponse(200, {}, b),
                cache={},
                target_model="gemini-3.7-flash",
            )
            self.assertEqual(st_emp, "OK")

    def test_dedicated_stt_anti_false_positive_shared_cache_and_auth_messages(self) -> None:
        # 1. DeepSeek shared endpoint: /models only lists chat models, NOT deepseek-asr.
        #    Stage 1 ASR must stay OK; Stage 2 Refiner reuses cached model list (1 HTTP call) and flags missing model!
        cfg_ds = Config(
            asr=AsrConfig(provider="deepseek", model="deepseek-asr"),
            refiner=RefinerConfig(provider="deepseek", model="deepseek-flash"),
        )
        env_ds = {"DEEPSEEK_API_KEY": "sk-ds-shared-0001"}
        creds_ds = resolve_credentials(cfg_ds, env_ds)
        calls: list[str] = []

        def _ds_tx(req: HttpRequest) -> HttpResponse:
            calls.append(req.url)
            return HttpResponse(200, {}, b'{"data":[{"id":"deepseek-chat"}]}')

        cache: dict = {}
        st_asr, _, msg_asr = cli_mod._probe_stage_ping(
            stage="asr",
            provider="deepseek",
            cfg=cfg_ds,
            creds=creds_ds,
            env=env_ds,
            base_url=cfg_ds.asr.effective_base_url("deepseek"),
            transport=_ds_tx,
            cache=cache,
            target_model="deepseek-asr",
        )
        st_ref, _, msg_ref = cli_mod._probe_stage_ping(
            stage="refiner",
            provider="deepseek",
            cfg=cfg_ds,
            creds=creds_ds,
            env=env_ds,
            base_url=cfg_ds.refiner.effective_base_url("deepseek"),
            transport=_ds_tx,
            cache=cache,
            target_model="deepseek-flash",
        )
        self.assertEqual(len(calls), 1, "shared endpoint must issue only 1 HTTP GET")
        self.assertEqual(st_asr, "OK", msg_asr)
        self.assertEqual(st_ref, "FAIL")
        self.assertIn("deepseek-flash", msg_ref)
        self.assertIn("not enabled or not found", msg_ref)

        # 2. OpenAI shared endpoint: Stage 1 missing model fails, Stage 2 present model succeeds on cached 200!
        cfg_oa = Config(
            asr=AsrConfig(provider="openai", model="gpt-transcribe-missing"),
            refiner=RefinerConfig(provider="openai", model="gpt-6-luna"),
        )
        env_oa = {"OPENAI_API_KEY": "sk-oa-shared-0001"}
        creds_oa = resolve_credentials(cfg_oa, env_oa)
        oa_calls: list[str] = []

        def _oa_tx(req: HttpRequest) -> HttpResponse:
            oa_calls.append(req.url)
            return HttpResponse(200, {}, b'{"data":[{"id":"gpt-6-luna"},{"id":"gpt-transcribe"}]}')

        oa_cache: dict = {}
        st_oa_asr, _, msg_oa_asr = cli_mod._probe_stage_ping(
            stage="asr",
            provider="openai",
            cfg=cfg_oa,
            creds=creds_oa,
            env=env_oa,
            base_url=cfg_oa.asr.effective_base_url("openai"),
            transport=_oa_tx,
            cache=oa_cache,
            target_model="gpt-transcribe-missing",
        )
        st_oa_ref, _, msg_oa_ref = cli_mod._probe_stage_ping(
            stage="refiner",
            provider="openai",
            cfg=cfg_oa,
            creds=creds_oa,
            env=env_oa,
            base_url=cfg_oa.refiner.effective_base_url("openai"),
            transport=_oa_tx,
            cache=oa_cache,
            target_model="gpt-6-luna",
        )
        self.assertEqual(len(oa_calls), 1)
        self.assertEqual(st_oa_asr, "FAIL")
        self.assertIn("gpt-transcribe-missing", msg_oa_asr)
        self.assertEqual(st_oa_ref, "OK")
        self.assertIn("shared endpoint", msg_oa_ref)

        # 3. DashScope: dedicated STT (qwen3-asr-flash) skips model-list failure; single-hop omni checks model list
        cfg_dash_stt = Config(asr=AsrConfig(provider="dashscope", model="qwen3-asr-flash"))
        cfg_dash_omni = Config(asr=AsrConfig(provider="dashscope", model="qwen3.8-omni-flash"))
        env_dash = {"DASHSCOPE_API_KEY": "sk-dash-0001"}
        creds_dash = resolve_credentials(cfg_dash_stt, env_dash)
        dash_tx = lambda _req: HttpResponse(200, {}, b'{"data":[{"id":"qwen3.8-flash"}]}')
        st_dstt, _, _ = cli_mod._probe_stage_ping(
            stage="asr",
            provider="dashscope",
            cfg=cfg_dash_stt,
            creds=creds_dash,
            env=env_dash,
            base_url=cfg_dash_stt.asr.effective_base_url("dashscope"),
            transport=dash_tx,
            cache={},
            target_model="qwen3-asr-flash",
        )
        st_domni, _, msg_domni = cli_mod._probe_stage_ping(
            stage="asr",
            provider="dashscope",
            cfg=cfg_dash_omni,
            creds=creds_dash,
            env=env_dash,
            base_url=cfg_dash_omni.asr.effective_base_url("dashscope"),
            transport=dash_tx,
            cache={},
            target_model="qwen3.8-omni-flash",
        )
        self.assertEqual(st_dstt, "OK")
        self.assertEqual(st_domni, "FAIL")
        self.assertIn("qwen3.8-omni-flash", msg_domni)

        # 4. OpenAI-compat pagination (has_more=true / next_page) -> NO false positive
        for page_payload in (
            b'{"data":[{"id":"other"}],"has_more":true}',
            b'{"data":[{"id":"other"}],"next_page":"page-2"}',
        ):
            st_pg, _, _ = cli_mod._probe_stage_ping(
                stage="refiner",
                provider="openai",
                cfg=cfg_oa,
                creds=creds_oa,
                env=env_oa,
                base_url=cfg_oa.refiner.effective_base_url("openai"),
                transport=lambda _req, b=page_payload: HttpResponse(200, {}, b),
                cache={},
                target_model="gpt-6-luna",
            )
            self.assertEqual(st_pg, "OK")

        # 5. OpenRouter official /key validates auth (401 fails, 200 OK without /models check); custom proxy /models checks model
        cfg_or = Config(refiner=RefinerConfig(provider="openrouter", model="google/gemini-3.8-flash"))
        env_or = {"OPENROUTER_API_KEY": "sk-or-test-0001"}
        creds_or = resolve_credentials(cfg_or, env_or)
        seen_or_url: list[str] = []

        def _or_tx(req: HttpRequest) -> HttpResponse:
            seen_or_url.append(req.url)
            return HttpResponse(200, {}, b'{"data":{"label":"ok"}}')

        st_or, _, _ = cli_mod._probe_stage_ping(
            stage="refiner",
            provider="openrouter",
            cfg=cfg_or,
            creds=creds_or,
            env=env_or,
            base_url=cfg_or.refiner.effective_base_url("openrouter"),
            transport=_or_tx,
            cache={},
            target_model="google/gemini-3.8-flash",
        )
        self.assertEqual(st_or, "OK")
        self.assertTrue(seen_or_url[0].endswith("/key"))

        # 6. 401/403 includes explicit env var name + ~/.agent-voice-input/.env
        st_401, _, msg_401 = cli_mod._probe_stage_ping(
            stage="refiner",
            provider="openai",
            cfg=cfg_oa,
            creds=creds_oa,
            env=env_oa,
            base_url=cfg_oa.refiner.effective_base_url("openai"),
            transport=lambda _req: HttpResponse(401, {}, b'{"error":"bad key"}'),
            cache={},
            target_model="gpt-6-luna",
        )
        self.assertEqual(st_401, "FAIL")
        self.assertIn("OPENAI_API_KEY", msg_401)
        self.assertIn("~/.agent-voice-input/.env", msg_401)

    def test_start_cold_preflight_all_scenarios_and_concurrency(self) -> None:
        parser = cli_mod._build_parser()

        class _LiveProc:
            pid = 91919
            returncode = None

            def poll(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rt = root / "rt"
            rt.mkdir()
            (rt / "diag.jsonl").write_text('{"ok":true}\n', encoding="utf-8")
            base_env = {
                "HOME": str(root),
                "PATH": "",
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
                "OPENAI_API_KEY": "sk-oa-preflight-0001",
            }
            cfg_oa = Config(
                asr=AsrConfig(provider="openai", model="gpt-transcribe"),
                refiner=RefinerConfig(provider="openai", model="gpt-6-luna"),
            )

            def _run_cold_start(cfg_in: Config, env_in: dict[str, str], ping_tx) -> tuple[int, str]:
                calls = [0]

                def _ctrl(_rt: Path, _req: dict[str, object], timeout_sec: float = 1.0) -> dict[str, object]:
                    calls[0] += 1
                    if calls[0] == 1:
                        raise ConnectionRefusedError("stopped")
                    return {"ok": True, "pid": 91919, "state": "idle", "shim_ok": True}

                buf = io.StringIO()
                with (
                    mock.patch.object(cli_mod.sys, "platform", "linux"),
                    mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                    mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                    mock.patch("agent_voice_input.control_ipc.send_control_command", side_effect=_ctrl),
                    mock.patch.object(cli_mod.subprocess, "Popen", return_value=_LiveProc()),
                    mock.patch("sys.stdout", buf),
                ):
                    rc = cli_mod._cmd_start(
                        parser.parse_args(["start"]),
                        env_in,
                        cfg=cfg_in,
                        ping_transport=ping_tx,
                    )
                return rc, buf.getvalue()

            # 1. Cold start all OK (shared endpoint sends 1 HTTP call with timeout <= 3.5s)
            seen_reqs: list[HttpRequest] = []

            def _tx_ok(req: HttpRequest) -> HttpResponse:
                seen_reqs.append(req)
                return HttpResponse(200, {}, b'{"data":[{"id":"gpt-transcribe"},{"id":"gpt-6-luna"}]}')

            rc_ok, out_ok = _run_cold_start(cfg_oa, base_env, _tx_ok)
            self.assertEqual(rc_ok, 0)
            self.assertEqual(len(seen_reqs), 1)
            self.assertLessEqual(seen_reqs[0].timeout_sec, 3.5)
            self.assertNotIn("WARN", out_ok)

            # 2. 401 auth failure -> WARN + Fix: with OPENAI_API_KEY, still returns 0
            rc_401, out_401 = _run_cold_start(
                cfg_oa,
                base_env,
                lambda _req: HttpResponse(401, {}, b'{"error":"invalid_api_key"}'),
            )
            self.assertEqual(rc_401, 0)
            self.assertIn("WARN", out_401)
            self.assertIn("Fix:", out_401)
            self.assertIn("OPENAI_API_KEY", out_401)

            # 3. HTTP 200 model missing -> WARN + Fix:, still returns 0
            rc_mod, out_mod = _run_cold_start(
                cfg_oa,
                base_env,
                lambda _req: HttpResponse(200, {}, b'{"data":[{"id":"gpt-transcribe"}]}'),
            )
            self.assertEqual(rc_mod, 0)
            self.assertIn("WARN", out_mod)
            self.assertIn("gpt-6-luna", out_mod)
            self.assertIn("Fix:", out_mod)

            # 4. Network timeout -> WARN + Fix: (doctor --ping), still returns 0
            def _tx_timeout(_req: HttpRequest) -> HttpResponse:
                raise TimeoutError("timed out")

            rc_to, out_to = _run_cold_start(cfg_oa, base_env, _tx_timeout)
            self.assertEqual(rc_to, 0)
            self.assertIn("WARN", out_to)
            self.assertIn("timed out", out_to)
            self.assertIn("Fix:", out_to)
            self.assertIn("doctor --ping", out_to)

            # 5. Already running daemon -> returns 0 immediately with ZERO network calls
            already_called = [0]

            def _tx_forbid(_req: HttpRequest) -> HttpResponse:
                already_called[0] += 1
                raise AssertionError("already running start must not ping")

            buf_alr = io.StringIO()
            with (
                mock.patch(
                    "agent_voice_input.control_ipc.send_control_command",
                    return_value={"ok": True, "pid": 91919, "state": "idle"},
                ),
                mock.patch("sys.stdout", buf_alr),
            ):
                rc_alr = cli_mod._cmd_start(
                    parser.parse_args(["start"]),
                    base_env,
                    cfg=cfg_oa,
                    ping_transport=_tx_forbid,
                )
            self.assertEqual(rc_alr, 0)
            self.assertEqual(already_called[0], 0)

            # 6. Two distinct HTTP providers run concurrently via ThreadPoolExecutor
            cfg_dual = Config(
                asr=AsrConfig(provider="dashscope", model="qwen3-asr-flash"),
                refiner=RefinerConfig(provider="openai", model="gpt-6-luna"),
            )
            env_dual = {
                **base_env,
                "DASHSCOPE_API_KEY": "sk-dash-dual-0001",
                "OPENAI_API_KEY": "sk-oa-dual-0001",
            }
            barrier = threading.Barrier(2, timeout=2.0)

            def _tx_concurrent(req: HttpRequest) -> HttpResponse:
                barrier.wait()  # Would timeout if sequential!
                return HttpResponse(200, {}, b'{"data":[{"id":"gpt-6-luna"}]}')

            rc_dual, out_dual = _run_cold_start(cfg_dual, env_dual, _tx_concurrent)
            self.assertEqual(rc_dual, 0)
            self.assertNotIn("WARN", out_dual)


class RefinerFallbackHudTests(unittest.TestCase):
    def test_format_refiner_fallback_hud_text_all_categories_bilingual(self) -> None:
        self.assertEqual(hud_layer_for_state("refiner_fallback"), "toast")
        self.assertTrue(is_toast_hud_state("refiner_fallback"))
        self.assertEqual(default_hud_duration_ms("refiner_fallback"), 4000)
        self.assertEqual(format_hud_text("refiner_fallback", "zh"), "⚠ 已贴原文(精修失败)")
        self.assertEqual(format_hud_text("refiner_fallback", "en"), "⚠ Raw pasted (refiner failed)")

        cases = [
            ("HTTP 401 unauthorized", "精修鉴权失败", "refiner auth error"),
            ("HTTP 429 rate limit exceeded", "精修额度/限流", "refiner quota/limit"),
            ("HTTP 404 model_not_found", "精修模型不可用", "refiner model error"),
            ("request timed out after 5s", "精修超时", "refiner timeout"),
            ("unexpected 502 bad gateway", "精修失败", "refiner failed"),
            ("", "精修失败", "refiner failed"),
        ]
        for warn, zh_rsn, en_rsn in cases:
            self.assertEqual(
                format_refiner_fallback_hud_text(warn, copied_only=False, lang="zh"),
                f"⚠ 已贴原文({zh_rsn})",
            )
            self.assertEqual(
                format_refiner_fallback_hud_text(warn, copied_only=True, lang="zh"),
                f"⚠ 已复制原文({zh_rsn})",
            )
            self.assertEqual(
                format_refiner_fallback_hud_text(warn, copied_only=False, lang="en"),
                f"⚠ Raw pasted ({en_rsn})",
            )
            self.assertEqual(
                format_refiner_fallback_hud_text(warn, copied_only=True, lang="en"),
                f"⚠ Raw copied ({en_rsn})",
            )

    def test_classify_and_plan_commit_hud_priority_and_daemon_end_to_end(self) -> None:
        pasted_ok = PasteOutcome(True, True, "ok", "Ghostty", False)
        copied_ok = PasteOutcome(False, True, "window_changed", "Ghostty", False)
        failed_out = PasteOutcome(False, False, "paste_failed", "Ghostty", False)

        # Higher-priority errors win over refiner_fallback
        self.assertEqual(
            classify_delivered_commit_hud(
                failed_out, asr_missed_chunks=0, recorder_lost=False, saved_audio="/tmp/a.wav", refiner_fallback="asr_draft"
            )[2],
            "paste_failed_retry",
        )
        self.assertEqual(
            classify_delivered_commit_hud(
                pasted_ok, asr_missed_chunks=1, recorder_lost=False, saved_audio="", refiner_fallback="asr_draft"
            )[2],
            "partial_asr_unsaved",
        )
        # Stage 2 asr_draft fallback -> refiner_fallback (both pasted and copied_only)
        self.assertEqual(
            classify_delivered_commit_hud(
                pasted_ok, asr_missed_chunks=0, recorder_lost=False, saved_audio="", refiner_fallback="asr_draft"
            )[2],
            "refiner_fallback",
        )
        self.assertEqual(
            classify_delivered_commit_hud(
                copied_ok, asr_missed_chunks=0, recorder_lost=False, saved_audio="", refiner_fallback="asr_draft"
            )[2],
            "refiner_fallback",
        )
        # Normal off / single_hop_passthrough stays done / copied
        self.assertEqual(
            classify_delivered_commit_hud(
                pasted_ok, asr_missed_chunks=0, recorder_lost=False, saved_audio="", refiner_fallback="single_hop_passthrough"
            )[2],
            "done",
        )
        self.assertEqual(
            classify_delivered_commit_hud(
                copied_ok, asr_missed_chunks=0, recorder_lost=False, saved_audio="", refiner_fallback=""
            )[2],
            "copied",
        )

        # plan_commit_hud_calls forwards custom text for refiner_fallback across idle, rec_active, next_vis_stage
        for rec_a, next_s in ((False, ""), (True, ""), (False, "recognizing")):
            calls, _ = plan_commit_hud_calls(
                silent_hud=False,
                rec_active=rec_a,
                recording_active=rec_a,
                stopping_capture=False,
                cur_silent_hud=False,
                main_hud_state="refining",
                main_hud_visible=True,
                main_hud_bg_count=0,
                next_vis_stage=next_s,
                vis_inflight=0,
                hud_st="refiner_fallback",
                copied_hud_text="⚠ 已贴原文(精修鉴权失败)",
                hud_dur_ms=4000,
                beep_k="",
            )
            rf_calls = [c for c in calls if c[0] == "refiner_fallback"]
            self.assertEqual(len(rf_calls), 1)
            self.assertEqual(rf_calls[0][1], "⚠ 已贴原文(精修鉴权失败)")
            self.assertEqual(rf_calls[0][2], 4000)

        # End-to-end VoiceInputDaemon test: Stage 2 fallback emits warning HUD, refiner="off" emits "done"
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            rt.mkdir(mode=0o700)
            tracker = SessionTracker(home_dir=Path(td) / "empty_home", runtime_dir=rt, env={"HOME": td})

            # Case A: pasted + 401 refiner fallback (zh) -> 4000ms
            plat_zh = _FakeWavRecorderPlatform(paste_outcome=pasted_ok, lang="zh-CN")
            d_zh = VoiceInputDaemon(
                Config(refiner=RefinerConfig(provider="deepseek"), ui=UiConfig(language="zh")),
                env={"HOME": td, "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)},
                runtime_dir=rt,
                platform=plat_zh,
                asr_provider=_StubAsr("测试原话"),
                refiner=_FailingFallbackRefiner("HTTP 401 unauthorized"),
                tracker=tracker,
            )
            try:
                d_zh._last_trigger_ts = -1e9
                d_zh.handle_trigger("toggle")
                d_zh._last_trigger_ts = -1e9
                rs = d_zh.handle_trigger("toggle")
                dl = time.monotonic() + 3.0
                while time.monotonic() < dl and d_zh.last_result.get("seq_id") != rs["seq_id"]:
                    time.sleep(0.01)
                rf_huds = [c for c in plat_zh.hud_calls if c[0] == "refiner_fallback"]
                self.assertEqual(len(rf_huds), 1, plat_zh.hud_calls)
                self.assertEqual(rf_huds[0][1], "⚠ 已贴原文(精修鉴权失败)")
                self.assertEqual(rf_huds[0][2], 4000)
            finally:
                d_zh.close()

            # Case B: copied_only + 429 refiner fallback (en) -> 4500ms
            plat_en = _FakeWavRecorderPlatform(paste_outcome=copied_ok, lang="en-US")
            d_en = VoiceInputDaemon(
                Config(refiner=RefinerConfig(provider="deepseek"), ui=UiConfig(language="en")),
                env={"HOME": td, "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)},
                runtime_dir=rt,
                platform=plat_en,
                asr_provider=_StubAsr("raw dictation"),
                refiner=_FailingFallbackRefiner("HTTP 429 rate limit"),
                tracker=tracker,
            )
            try:
                d_en._last_trigger_ts = -1e9
                d_en.handle_trigger("toggle")
                d_en._last_trigger_ts = -1e9
                rs = d_en.handle_trigger("toggle")
                dl = time.monotonic() + 3.0
                while time.monotonic() < dl and d_en.last_result.get("seq_id") != rs["seq_id"]:
                    time.sleep(0.01)
                rf_huds = [c for c in plat_en.hud_calls if c[0] == "refiner_fallback"]
                self.assertEqual(len(rf_huds), 1, plat_en.hud_calls)
                self.assertEqual(rf_huds[0][1], "⚠ Raw copied (refiner quota/limit)")
                self.assertEqual(rf_huds[0][2], 4500)
            finally:
                d_en.close()

            # Case C: refiner == "off" -> stays normal "done"
            plat_off = _FakeWavRecorderPlatform(paste_outcome=pasted_ok, lang="zh-CN")
            cfg_off = Config(refiner=RefinerConfig(provider="off"))
            d_off = VoiceInputDaemon(
                cfg_off,
                env={"HOME": td, "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)},
                runtime_dir=rt,
                platform=plat_off,
                asr_provider=_StubAsr("关闭精修"),
                refiner=create_refiner(cfg_off),
                tracker=tracker,
            )
            try:
                d_off._last_trigger_ts = -1e9
                d_off.handle_trigger("toggle")
                d_off._last_trigger_ts = -1e9
                rs = d_off.handle_trigger("toggle")
                dl = time.monotonic() + 3.0
                while time.monotonic() < dl and d_off.last_result.get("seq_id") != rs["seq_id"]:
                    time.sleep(0.01)
                self.assertTrue(any(c[0] == "done" for c in plat_off.hud_calls), plat_off.hud_calls)
                self.assertFalse(any(c[0] == "refiner_fallback" for c in plat_off.hud_calls))
            finally:
                d_off.close()


class StatusContractAndJsonCleanupTests(unittest.TestCase):
    def test_status_toggle_cancel_human_and_json_outputs(self) -> None:
        cfg = Config(hotkey=HotkeyConfig(key="ROption", trigger_mode="ptt", scope="coding"))
        sample_resp = {
            "ok": True,
            "pid": 4321,
            "state": "idle",
            "version": "0.18.0",
            "pending_count": 1,
            "shim_ok": False,
            "shim_error": "no CLT",
            "pipeline": {
                "asr": {"provider": "dashscope", "model": "qwen3-asr-flash", "effort": "none", "single_hop": False},
                "refiner": {"provider": "codex", "model": "gpt-6-luna", "effort": "low", "bypassed": False},
            },
            "hotwords": {
                "total": 205,
                "kept": 200,
                "dropped_count": 5,
                "used_chars": 1800,
                "max_items": 200,
                "max_chars": 3000,
                "warnings_count": 1,
            },
            "last_result": {
                "seq_id": 9,
                "ok": True,
                "delivery_status": "pasted",
                "total_ms": 512,
                "refiner_fallback": "asr_draft",
            },
            "recent_results": [
                {"seq_id": 8, "ok": False, "skipped_reason": "empty_asr", "total_ms": 120},
                {"seq_id": 9, "ok": True, "delivery_status": "pasted", "total_ms": 512},
            ],
            "code_drift": True,
            "drift_warning": "version upgraded (0.17.0 -> 0.18.0); run 'agent-voice-input restart'",
        }
        lines = cli_mod._format_status_human(sample_resp, cfg=cfg, show_history=True)
        txt = "\n".join(lines)
        self.assertIn("Daemon    running (pid=4321, state=idle, v0.18.0, pending=1)", txt)
        self.assertIn("ASR       dashscope / qwen3-asr-flash", txt)
        self.assertIn("Refiner   codex / gpt-6-luna (effort=low)", txt)
        self.assertIn("Hotkey    ROption (ptt, scope=coding)", txt)
        self.assertIn("Bridge    degraded (no CLT)", txt)
        self.assertIn("Hotwords  200/205 active (cap 200), dropped=5, warnings=1", txt)
        self.assertIn("Last      #9 ok pasted 512ms fallback=asr_draft", txt)
        self.assertIn("History   2 record(s) (latest 2):", txt)
        self.assertIn("Notice    version upgraded", txt)
        self.assertIn("Fix:      run 'agent-voice-input restart'", txt)

        # Offline error & empty history branches
        err_lines = cli_mod._format_status_human(
            {"ok": False, "state": "error", "error": "EACCES", "recent_results": []},
            show_history=True,
        )
        err_txt = "\n".join(err_lines)
        self.assertIn("error (EACCES)", err_txt)
        self.assertIn("0 recent records", err_txt)

        # Control action human formatting
        self.assertIn("recording (seq=3)", cli_mod._format_control_action_human("toggle", {"ok": True, "state": "recording", "seq_id": 3}))
        self.assertIn("toggle failed (busy)", cli_mod._format_control_action_human("toggle", {"ok": False, "error": "busy"}))
        self.assertIn("cancelled (seq=2,3, state=idle)", cli_mod._format_control_action_human("cancel", {"ok": True, "cancelled_ids": [2, 3], "state": "idle"}))
        self.assertIn("not cancelled (nothing active, state=idle)", cli_mod._format_control_action_human("cancel", {"ok": True, "cancelled_ids": [], "state": "idle"}))
        self.assertIn("cancel failed (boom)", cli_mod._format_control_action_human("cancel", {"ok": False, "error": "boom"}))

    def test_hotwords_alias_sanitization_and_diagnose_legacy_sqlite_omission(self) -> None:
        # 1. Legacy alias inputs map cleanly onto canonical 7 keys without emitting stage2_kept/stage2_dropped
        sanitized = _sanitize_hotwords_status_counts(
            {
                "total_input": 42,
                "stage2_kept": 40,
                "stage2_dropped": 2,
                "used_chars": 500,
                "max_items": 200,
                "max_chars": 3000,
                "warnings_count": 0,
                "dropped": ["SecretWordMustNotLeak"],
            }
        )
        self.assertEqual(
            sanitized,
            {
                "total": 42,
                "kept": 40,
                "dropped_count": 2,
                "used_chars": 500,
                "max_items": 200,
                "max_chars": 3000,
                "warnings_count": 0,
            },
        )
        assert sanitized is not None
        self.assertNotIn("stage2_kept", sanitized)
        self.assertNotIn("stage2_dropped", sanitized)

        # 2. diagnose storage omits legacy_codex_sqlite when absent, includes when present
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            rt.mkdir(mode=0o700)
            env_d = {"HOME": td, "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)}
            payload_absent = collect_live_diagnostics(Config(), env_d, runtime_dir=rt, last=5)
            self.assertNotIn("legacy_codex_sqlite", payload_absent["storage"])

            ch = rt / "codex-home"
            ch.mkdir(mode=0o700)
            conn = sqlite3.connect(str(ch / "logs_2.sqlite"))
            try:
                conn.execute("CREATE TABLE logs (ts INTEGER, process_uuid TEXT, level TEXT)")
                conn.execute("INSERT INTO logs VALUES (?, 'p1', 'WARN')", (int(time.time()),))
                conn.commit()
            finally:
                conn.close()
            payload_present = collect_live_diagnostics(Config(), env_d, runtime_dir=rt, last=5)
            self.assertIn("legacy_codex_sqlite", payload_present["storage"])
            self.assertEqual(payload_present["storage"]["legacy_codex_sqlite"]["recent_1h_warns"], 1)

    def test_status_human_prefers_live_daemon_hotkey_override(self) -> None:
        static_cfg = Config(hotkey=HotkeyConfig(key="RControl", trigger_mode="tap_toggle", scope="all"))
        live_cfg = Config(hotkey=HotkeyConfig(key="F24", trigger_mode="ptt", scope="terminals"))

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            rt.mkdir(mode=0o700)
            plat = _FakeWavRecorderPlatform()
            tracker = SessionTracker(home_dir=Path(td) / "empty_home", runtime_dir=rt, env={"HOME": td})
            daemon = VoiceInputDaemon(
                live_cfg,
                env={"HOME": td, "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)},
                runtime_dir=rt,
                platform=plat,
                asr_provider=_StubAsr("ok"),
                refiner=create_refiner(Config(refiner=RefinerConfig(provider="off"))),
                tracker=tracker,
            )
            try:
                resp = daemon.handle_control({"cmd": "status"})
            finally:
                daemon.close()

        self.assertEqual(
            resp.get("hotkey"),
            {"key": "F24", "trigger_mode": "ptt", "scope": "terminals"},
        )
        human_live = "\n".join(cli_mod._format_status_human(resp, cfg=static_cfg))
        self.assertIn("Hotkey    F24 (ptt, scope=terminals)", human_live)
        self.assertNotIn("Hotkey    RControl", human_live)

        # String-form hotkey payload also overrides static config key while falling back mode/scope
        human_str = "\n".join(
            cli_mod._format_status_human({"ok": True, "pid": 99, "state": "idle", "hotkey": "F20"}, cfg=static_cfg)
        )
        self.assertIn("Hotkey    F20 (tap_toggle, scope=all)", human_str)

        # Missing live hotkey falls back to static cfg.hotkey
        resp_legacy = dict(resp)
        resp_legacy.pop("hotkey", None)
        human_fallback = "\n".join(cli_mod._format_status_human(resp_legacy, cfg=static_cfg))
        self.assertIn("Hotkey    RControl (tap_toggle, scope=all)", human_fallback)

    def test_pyproject_toml_parses_with_py310_simple_toml_fallback(self) -> None:
        from agent_voice_input import __version__
        from agent_voice_input.config import _parse_simple_toml

        pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
        parsed = _parse_simple_toml(pyproject_path.read_text(encoding="utf-8"))
        proj = parsed["project"]
        self.assertEqual(proj["name"], "agent-voice-input")
        self.assertEqual(proj["version"], __version__)
        self.assertEqual(proj["authors"], [{"name": "xiaohuaxi"}])
        self.assertIn("Homepage", proj["urls"])
        self.assertIn("agent-voice-input", proj["scripts"])


if __name__ == "__main__":
    unittest.main()
