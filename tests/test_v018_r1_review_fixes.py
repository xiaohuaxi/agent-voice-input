"""Targeted unit tests for v0.18.0 Astra R1 review fixes (15 items)."""
from __future__ import annotations

import datetime
import fcntl
import http.server
import io
import json
import math
import os
from pathlib import Path
import signal
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_DIR = _REPO_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from agent_voice_input import audio_proc as audio_mod
from agent_voice_input import cli as cli_mod
from agent_voice_input import config as cfg_mod
from agent_voice_input import integrate as integrate_mod
from agent_voice_input import llm_common as llm_mod
from agent_voice_input import refiner as refiner_mod
from agent_voice_input.asr_providers import AsrResult, BaseAsrProvider
from agent_voice_input.audio_proc import (
    append_transcript_record,
    archive_recording_audio,
    list_saved_recordings,
    pack_pcm16_mono_wav,
    run_utterance_archive,
)
from agent_voice_input.config import (
    ArchiveConfig,
    AsrConfig,
    Config,
    ConfigError,
    RefinerConfig,
    load_config,
    load_env_files,
    resolve_archive_audio_dir,
    resolve_archive_transcripts_dir,
    resolve_credentials,
)
from agent_voice_input.control_ipc import (
    DIAG_FILENAME,
    append_diag_record,
    format_local_iso_ts,
    generate_utterance_id,
    read_diag_history,
    send_control_command,
)
from agent_voice_input.daemon import VoiceInputDaemon
from agent_voice_input.daemon_pipeline import build_pipeline_summary, run_pipeline_once
from agent_voice_input.http_transport import HttpRequest, HttpResponse
from agent_voice_input.platform_common import ForegroundWindow, PasteOutcome
from agent_voice_input.refiner import (
    BaseRefiner,
    NoopRefiner,
    OpenAIResponsesRefiner,
    RefinerResult,
    _classify_stream_error_event,
    _parse_responses_sse_lines,
)
from agent_voice_input.session_ctx import SessionTracker


def _voiced_wav(duration_sec: float = 0.6) -> bytes:
    n = int(16000 * duration_sec)
    samples = [int(14000 * math.sin(2.0 * math.pi * 440.0 * i / 16000.0)) for i in range(n)]
    return pack_pcm16_mono_wav(samples, sample_rate=16000)


class _StubAsr(BaseAsrProvider):
    def __init__(
        self,
        text: str = "测试草稿",
        *,
        provider: str = "dashscope",
        model: str = "qwen3-asr-flash",
        missed_chunks: int = 0,
    ) -> None:
        self.provider_name = provider
        self._text = text
        self._model = model
        self._missed_chunks = missed_chunks

    def transcribe(self, wav_bytes: bytes, *, context=None, is_cancelled=None, cancel_token=None) -> AsrResult:
        del wav_bytes, context, is_cancelled, cancel_token
        return AsrResult(
            text=self._text,
            provider=self.provider_name,
            model=self._model,
            latency_ms=10,
            missed_chunks=self._missed_chunks,
        )


class _StubRefiner(BaseRefiner):
    def __init__(
        self,
        text: str,
        *,
        provider: str = "codex",
        model: str = "gpt-6-luna",
        used_fallback: str = "",
        warning: str = "",
    ) -> None:
        self.provider_name = provider
        self._text = text
        self._model = model
        self._used_fallback = used_fallback
        self._warning = warning

    def refine(self, asr_input, **kwargs) -> RefinerResult:
        del asr_input, kwargs
        return RefinerResult(
            text=self._text,
            provider=self.provider_name,
            model=self._model,
            latency_ms=12,
            used_fallback=self._used_fallback,
            warning=self._warning,
        )


class _FakePlatform:
    def __init__(self, wav_bytes: bytes) -> None:
        self._wav = wav_bytes
        self.fg = ForegroundWindow(hwnd=1, process="Ghostty", title="codex")
        self.pasted_texts: list[str] = []
        self.last_audio_error = ""
        self.last_shim_error = ""

    def _host_ui_culture(self) -> str:
        return "zh-CN"

    def is_shim_ok(self) -> bool:
        return True

    def set_recorder_lost_callback(self, cb) -> None:
        self._lost_cb = cb

    def set_recorder_recovered_callback(self, cb) -> None:
        self._rec_cb = cb

    def start_event_listener(self, cb) -> None:
        self._hk_cb = cb

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
        return self._wav, "avfoundation"

    def cancel_recording(self) -> None:
        pass

    def hud(self, *args, **kwargs) -> None:
        del args, kwargs

    def beep(self, kind: str) -> None:
        del kind

    def deliver_text(self, text: str, seq_id: int, target_hwnd: int = 0, **kwargs) -> PasteOutcome:
        del seq_id, target_hwnd, kwargs
        self.pasted_texts.append(text)
        return PasteOutcome(True, True, "ok", "Ghostty", False)

    def close(self) -> None:
        pass


class _LoopbackHttpServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: object, client_address: object) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class TestV018R1ReviewFixes(unittest.TestCase):
    def test_p0_1_archive_transcript_redacts_secrets_without_mutating_delivery(self) -> None:
        wav = _voiced_wav(0.6)
        secret_oai = "sk-synthetic-archive-secret-99887766"
        secret_custom = "custom-token-value-1122334455"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            rt = root / "rt"
            home.mkdir()
            rt.mkdir(mode=0o700)
            env = {
                "HOME": str(home),
                "AGENT_VOICE_INPUT_HOME": str(home / ".agent-voice-input"),
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
                "OPENAI_API_KEY": secret_oai,
                "MY_CUSTOM_REF_KEY": secret_custom,
            }
            cfg = Config(
                archive=ArchiveConfig(save_transcripts=True, save_audio=False),
                refiner=RefinerConfig(provider="openai", api_key_env="MY_CUSTOM_REF_KEY"),
            )
            draft_txt = f"请检查 key {secret_oai} 和 {secret_custom}"
            final_txt = f"请检查密钥 {secret_oai} 以及 {secret_custom}。"

            # 1. run_pipeline_once
            delivered: list[str] = []
            res = run_pipeline_once(
                wav,
                cfg=cfg,
                asr_provider=_StubAsr(draft_txt),
                refiner=_StubRefiner(final_txt, provider="openai", model="gpt-6-luna"),
                deliver_fn=lambda txt, _w: (delivered.append(txt) or PasteOutcome(True, True, "ok", "Ghostty", False)),
                audio_source="avfoundation",
                runtime_dir=rt,
                env=env,
            )
            self.assertTrue(res.ok)
            self.assertEqual(res.final_text, final_txt)
            self.assertEqual(delivered, [final_txt])

            # 2. VoiceInputDaemon
            plat = _FakePlatform(wav)
            tracker = SessionTracker(home_dir=home, runtime_dir=rt, proc_root=root / "no_proc", env=env)
            daemon = VoiceInputDaemon(
                cfg,
                env=env,
                runtime_dir=rt,
                platform=plat,
                asr_provider=_StubAsr(draft_txt),
                refiner=_StubRefiner(final_txt, provider="openai", model="gpt-6-luna"),
                tracker=tracker,
                paste_settle_sec=0.0,
            )
            try:
                daemon.handle_trigger("toggle")
                daemon._last_trigger_ts = -1e9
                res_stop = daemon.handle_trigger("toggle")
                dl = time.monotonic() + 3.0
                while time.monotonic() < dl and daemon.last_result.get("seq_id") != res_stop["seq_id"]:
                    time.sleep(0.01)
            finally:
                daemon.close()

            self.assertEqual(plat.pasted_texts, [final_txt])

            tr_dir = resolve_archive_transcripts_dir(env)
            files = list(tr_dir.glob("*.jsonl"))
            self.assertEqual(len(files), 1)
            raw = files[0].read_text(encoding="utf-8")
            self.assertNotIn(secret_oai, raw)
            self.assertNotIn(secret_custom, raw)
            rows = [json.loads(ln) for ln in raw.splitlines() if ln.strip()]
            self.assertEqual(len(rows), 2)
            for row in rows:
                self.assertIn("[REDACTED]", row["draft"])
                self.assertIn("[REDACTED]", row["final"])
                self.assertNotIn(secret_oai, row["draft"])
                self.assertNotIn(secret_custom, row["final"])

    def test_p0_2_responses_incomplete_reason_whitelisted(self) -> None:
        # Unknown/free-text reason normalized to "unknown"
        leaked_phrase = "echoed user utterance SECRET-PHRASE-42"
        err_unknown = _classify_stream_error_event(
            "openai",
            {"type": "response.incomplete", "response": {"incomplete_details": {"reason": leaked_phrase}}},
            "response.incomplete",
            {},
        )
        self.assertEqual(str(err_unknown), "Responses stream incomplete (unknown)")
        self.assertEqual(err_unknown.retry_kind, "transient")
        self.assertNotIn(leaked_phrase, str(err_unknown))

        # Known safe enum reasons preserved with proper retry_kind
        for known, exp_retry in (
            ("max_output_tokens", "transient"),
            ("content_filter", "never"),
            ("safety", "never"),
            ("pause_turn", "transient"),
            ("cancelled", "transient"),
            ("length", "transient"),
        ):
            err_k = _classify_stream_error_event(
                "openai",
                {"type": "response.incomplete", "response": {"incomplete_details": {"reason": known}}},
                "response.incomplete",
                {},
            )
            self.assertEqual(str(err_k), f"Responses stream incomplete ({known})")
            self.assertEqual(err_k.retry_kind, exp_retry)

        # End-to-end refiner -> diag.jsonl -> status history never leaks upstream free-text reason
        sse_body = (
            "event: response.incomplete\n"
            + "data: "
            + json.dumps(
                {
                    "type": "response.incomplete",
                    "response": {"incomplete_details": {"reason": leaked_phrase}},
                }
            )
            + "\n\n"
        ).encode("utf-8")

        cfg = Config(
            refiner=RefinerConfig(
                provider="openai",
                api="responses",
                model="gpt-6-luna",
                max_retries=0,
            )
        )
        env = {"OPENAI_API_KEY": "sk-test-p0-2"}
        ref = OpenAIResponsesRefiner(
            cfg,
            resolve_credentials(cfg, env),
            env=env,
            transport=lambda _req: HttpResponse(200, {}, sse_body),
        )
        try:
            r = ref.refine("原始草稿")
        finally:
            ref.close()
        self.assertEqual(r.used_fallback, "asr_draft")
        self.assertIn("unknown", r.warning)
        self.assertNotIn(leaked_phrase, r.warning)

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            res = run_pipeline_once(
                _voiced_wav(0.6),
                cfg=cfg,
                asr_provider=_StubAsr("原始草稿"),
                refiner=_StubRefiner("原始草稿", provider="openai", used_fallback="asr_draft", warning=r.warning),
                dry_run=True,
                runtime_dir=rt,
                env=env,
            )
            summary = build_pipeline_summary(1, res, dry_run=True)
            append_diag_record(rt, summary)
            diag_txt = (rt / DIAG_FILENAME).read_text(encoding="utf-8")
            self.assertNotIn(leaked_phrase, diag_txt)
            self.assertIn("unknown", diag_txt)

    def test_p0_3_missed_chunks_skip_archive_while_refiner_fallback_still_archives(self) -> None:
        wav = _voiced_wav(0.6)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            rt = root / "rt"
            home.mkdir()
            rt.mkdir(mode=0o700)
            env = {
                "HOME": str(home),
                "AGENT_VOICE_INPUT_HOME": str(home / ".agent-voice-input"),
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
            }
            cfg = Config(
                archive=ArchiveConfig(save_transcripts=True, save_audio=True, audio_max_count=10),
                refiner=RefinerConfig(provider="codex"),
            )
            audio_dir = resolve_archive_audio_dir(env)
            tr_dir = resolve_archive_transcripts_dir(env)

            # 1. Partial chunk drop in run_pipeline_once -> stashes failed/, skips BOTH audio & transcripts
            with mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", side_effect=OSError("no ffmpeg")):
                res_drop = run_pipeline_once(
                    wav,
                    cfg=cfg,
                    asr_provider=_StubAsr("部分识别文本", missed_chunks=1),
                    refiner=_StubRefiner("部分精修文本"),
                    deliver_fn=lambda _t, _w: PasteOutcome(True, True, "ok", "Ghostty", False),
                    audio_source="avfoundation",
                    runtime_dir=rt,
                    env=env,
                )
            self.assertTrue(res_drop.ok)
            self.assertEqual(res_drop.asr_missed_chunks, 1)
            self.assertTrue(res_drop.saved_audio)
            self.assertEqual(list_saved_recordings(audio_dir), [])
            self.assertEqual(list(tr_dir.glob("*.jsonl")) if tr_dir.exists() else [], [])

            # 2. Partial chunk drop in VoiceInputDaemon -> skips BOTH audio & transcripts
            plat = _FakePlatform(wav)
            tracker = SessionTracker(home_dir=home, runtime_dir=rt, proc_root=root / "no_proc", env=env)
            daemon = VoiceInputDaemon(
                cfg,
                env=env,
                runtime_dir=rt,
                platform=plat,
                asr_provider=_StubAsr("守护进程丢块文本", missed_chunks=2),
                refiner=_StubRefiner("守护进程精修"),
                tracker=tracker,
                paste_settle_sec=0.0,
            )
            try:
                daemon.handle_trigger("toggle")
                daemon._last_trigger_ts = -1e9
                rs = daemon.handle_trigger("toggle")
                dl = time.monotonic() + 3.0
                while time.monotonic() < dl and daemon.last_result.get("seq_id") != rs["seq_id"]:
                    time.sleep(0.01)
            finally:
                daemon.close()

            self.assertEqual(list_saved_recordings(audio_dir), [])
            self.assertEqual(list(tr_dir.glob("*.jsonl")) if tr_dir.exists() else [], [])

            # 3. Clean Stage 1 ASR + Stage 2 refiner fallback (asr_draft) -> STILL archives with refiner_fallback=True
            with mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", side_effect=OSError("no ffmpeg")):
                res_fb = run_pipeline_once(
                    wav,
                    cfg=cfg,
                    asr_provider=_StubAsr("完整草稿"),
                    refiner=_StubRefiner("完整草稿", used_fallback="asr_draft", warning="timeout"),
                    deliver_fn=lambda _t, _w: PasteOutcome(True, True, "ok", "Ghostty", False),
                    audio_source="avfoundation",
                    runtime_dir=rt,
                    env=env,
                )
            self.assertTrue(res_fb.ok)
            self.assertEqual(len(list_saved_recordings(audio_dir)), 1)
            tr_files = list(tr_dir.glob("*.jsonl"))
            self.assertEqual(len(tr_files), 1)
            row = json.loads(tr_files[0].read_text(encoding="utf-8").strip())
            self.assertIs(row["refiner_fallback"], True)
            self.assertEqual(row["final"], "完整草稿")

    def test_p0_4_graceful_stop_preserves_archive_during_slow_transcode_and_defers_sigterm_restore(self) -> None:
        wav = _voiced_wav(0.6)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            audio_dir = root / "home" / "audio"
            tr_dir = root / "home" / "transcripts"
            stop_evt = threading.Event()
            uid = generate_utterance_id()
            ts = time.time()

            # Fake slow ffmpeg script sleeping 30s
            slow_ffmpeg = root / "slow_ffmpeg.sh"
            slow_ffmpeg.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
            os.chmod(slow_ffmpeg, 0o700)

            res_holder: list[tuple[Path | None, Path | None]] = []

            def _worker() -> None:
                res_holder.append(
                    run_utterance_archive(
                        utterance_id=uid,
                        record_ts=ts,
                        ts_iso=format_local_iso_ts(ts),
                        mode="agent",
                        duration_sec=0.6,
                        asr_provider="dashscope",
                        asr_model="qwen3-asr-flash",
                        refiner_provider=None,
                        refiner_model=None,
                        refiner_fallback=False,
                        draft="慢转码测试",
                        final="慢转码测试",
                        raw_wav_bytes=wav,
                        save_audio=True,
                        save_transcripts=True,
                        audio_max_count=10,
                        audio_dir=audio_dir,
                        transcripts_dir=tr_dir,
                        ffmpeg_bin=str(slow_ffmpeg),
                        stop_evt=stop_evt,
                        shutdown_grace_sec=0.15,
                    )
                )

            t0 = time.monotonic()
            th = threading.Thread(target=_worker)
            th.start()
            time.sleep(0.15)
            stop_evt.set()
            th.join(timeout=3.0)
            elapsed = time.monotonic() - t0

            self.assertFalse(th.is_alive())
            self.assertLess(elapsed, 2.0)
            self.assertEqual(len(res_holder), 1)
            audio_p, tr_p = res_holder[0]
            assert audio_p is not None and tr_p is not None
            self.assertEqual(audio_p.name, f"{uid}.wav")
            self.assertEqual(audio_p.read_bytes(), wav)
            self.assertEqual([p.name for p in audio_dir.iterdir()], [f"{uid}.wav"])
            saved_row = json.loads(tr_p.read_text(encoding="utf-8").strip())
            self.assertEqual(saved_row["id"], uid)
            self.assertEqual(saved_row["audio_file"], f"{uid}.wav")

            # Verify serve_forever() keeps custom SIGTERM handler active until AFTER close() finishes
            rt = root / "rt"
            rt.mkdir(mode=0o700)
            env = {"HOME": str(root), "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)}
            plat = _FakePlatform(wav)
            tracker = SessionTracker(home_dir=root, runtime_dir=rt, proc_root=root / "no_proc", env=env)
            d = VoiceInputDaemon(
                Config(refiner=RefinerConfig(provider="off")),
                env=env,
                runtime_dir=rt,
                platform=plat,
                asr_provider=_StubAsr("x"),
                refiner=NoopRefiner(),
                tracker=tracker,
            )
            orig_term = signal.getsignal(signal.SIGTERM)
            handler_during_close: list[object] = []
            real_close = d.close

            def _spy_close() -> None:
                handler_during_close.append(signal.getsignal(signal.SIGTERM))
                real_close()

            d._stop_evt.set()  # Exit accept loop immediately
            with mock.patch.object(d, "close", side_effect=_spy_close):
                d.serve_forever()
            self.assertEqual(len(handler_during_close), 1)
            self.assertIsNot(handler_during_close[0], orig_term)
            self.assertIs(signal.getsignal(signal.SIGTERM), orig_term)

            # Verify _cmd_stop default graceful_timeout_sec >= 6.0 (covers daemon close archive wait)
            import inspect

            sig_stop = inspect.signature(cli_mod._cmd_stop)
            self.assertGreaterEqual(float(sig_stop.parameters["graceful_timeout_sec"].default), 6.0)

    def test_p0_5_publish_workflow_guards_publish_and_release_jobs_on_tag_ref(self) -> None:
        wf_text = (_REPO_ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
        lines = wf_text.splitlines()

        def _job_block(job_name: str) -> str:
            start = next(i for i, ln in enumerate(lines) if ln == f"  {job_name}:")
            out = [lines[start]]
            for ln in lines[start + 1 :]:
                if ln.startswith("  ") and not ln.startswith("    ") and ln.strip().endswith(":"):
                    break
                out.append(ln)
            return "\n".join(out)

        pub_block = _job_block("publish")
        rel_block = _job_block("github-release")
        self.assertIn("if: startsWith(github.ref, 'refs/tags/v')", pub_block)
        self.assertIn("if: startsWith(github.ref, 'refs/tags/v')", rel_block)

    def test_p1_1_startup_preflight_enforces_wall_clock_cap_single_shared_and_dual(self) -> None:
        release_evt = threading.Event()

        def _slow_tx(_req: HttpRequest) -> HttpResponse:
            release_evt.wait(timeout=1.5)
            return HttpResponse(200, {}, b'{"data":[{"id":"deepseek-chat"}]}')

        try:
            # 1. Single endpoint
            cfg_single = Config(
                asr=AsrConfig(provider="deepseek", model="deepseek-asr"),
                refiner=RefinerConfig(provider="off"),
            )
            env_single = {"DEEPSEEK_API_KEY": "sk-ds-slow-1"}
            creds_single = resolve_credentials(cfg_single, env_single)
            pipe_single = cfg_mod.explain_pipeline_selection(cfg_single, creds_single, env_single)
            t0 = time.monotonic()
            rows_single = cli_mod._run_startup_preflight_ping(
                cfg=cfg_single,
                creds=creds_single,
                env=env_single,
                pipe_info=pipe_single,
                transport=_slow_tx,
                timeout_sec=0.15,
            )
            dt_single = time.monotonic() - t0
            self.assertLess(dt_single, 0.45)
            self.assertTrue(any(lbl == "WARN" and "timed out" in txt for lbl, txt in rows_single), rows_single)

            # 2. Shared endpoint (ASR + Refiner same provider & key)
            cfg_shared = Config(
                asr=AsrConfig(provider="deepseek", model="deepseek-asr"),
                refiner=RefinerConfig(provider="deepseek", model="deepseek-chat"),
            )
            creds_shared = resolve_credentials(cfg_shared, env_single)
            pipe_shared = cfg_mod.explain_pipeline_selection(cfg_shared, creds_shared, env_single)
            t0 = time.monotonic()
            rows_shared = cli_mod._run_startup_preflight_ping(
                cfg=cfg_shared,
                creds=creds_shared,
                env=env_single,
                pipe_info=pipe_shared,
                transport=_slow_tx,
                timeout_sec=0.15,
            )
            dt_shared = time.monotonic() - t0
            self.assertLess(dt_shared, 0.45)
            warns_shared = [txt for lbl, txt in rows_shared if lbl == "WARN"]
            self.assertEqual(len(warns_shared), 2, rows_shared)
            self.assertTrue(all("timed out" in w for w in warns_shared))

            # 3. Dual endpoints (including slow agy probe)
            cfg_dual = Config(
                asr=AsrConfig(provider="deepseek", model="deepseek-asr"),
                refiner=RefinerConfig(provider="agy", model="gemini-3-flash"),
            )
            env_dual = {"DEEPSEEK_API_KEY": "sk-ds-slow-1", "AGY_BIN": "/fake/agy"}
            creds_dual = resolve_credentials(cfg_dual, env_dual)
            pipe_dual = {
                "asr": {"provider": "deepseek", "model": "deepseek-asr", "ready": True},
                "refiner": {"provider": "agy", "model": "gemini-3-flash", "ready": True, "bypassed": False},
            }

            def _slow_probe_stage(**kwargs):
                release_evt.wait(timeout=1.5)
                return "OK", 1500, "OK"

            t0 = time.monotonic()
            with mock.patch.object(cli_mod, "_probe_stage_ping", side_effect=_slow_probe_stage):
                rows_dual = cli_mod._run_startup_preflight_ping(
                    cfg=cfg_dual,
                    creds=creds_dual,
                    env=env_dual,
                    pipe_info=pipe_dual,
                    timeout_sec=0.15,
                )
            dt_dual = time.monotonic() - t0
            self.assertLess(dt_dual, 0.45)
            warns_dual = [txt for lbl, txt in rows_dual if lbl == "WARN"]
            self.assertEqual(len(warns_dual), 2, rows_dual)
            self.assertTrue(all("timed out" in w for w in warns_dual))
        finally:
            release_evt.set()

    def test_p1_2_stop_does_not_sigterm_new_daemon_when_lock_ownership_changes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            rt.mkdir(mode=0o700)
            (rt / "daemon.pid").write_text("111111\n", encoding="utf-8")
            (rt / "control.sock").write_text("sock", encoding="utf-8")
            env = {"HOME": td, "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)}
            killed: list[tuple[int, int]] = []

            def _fake_kill(pid_arg: int, sig_arg: int) -> None:
                if sig_arg != 0:
                    killed.append((pid_arg, sig_arg))

            # Ownership changes from 111111 (old) to 222222 (new instance) before Phase 2 escalation
            with (
                mock.patch("agent_voice_input.control_ipc.send_control_command", return_value={"ok": True}),
                mock.patch.object(cli_mod, "_resolve_daemon_target_pid", return_value=111111),
                mock.patch.object(cli_mod, "_try_claim_daemon_lock", return_value=None),
                mock.patch(
                    "agent_voice_input.control_ipc._inspect_daemon_lock_holder",
                    return_value=(True, 222222),
                ),
                mock.patch.object(cli_mod, "_probe_pid_liveness", return_value="alive"),
                mock.patch.object(cli_mod, "_is_avi_daemon_pid", return_value=True),
                mock.patch.object(cli_mod.os, "kill", side_effect=_fake_kill),
                mock.patch("sys.stderr", new_callable=io.StringIO),
            ):
                rc = cli_mod._cmd_stop(env, graceful_timeout_sec=0.0, term_timeout_sec=0.0)

            self.assertEqual(rc, 1)
            self.assertEqual(killed, [], "must NOT send SIGTERM to new daemon pid=222222")
            self.assertTrue((rt / "daemon.pid").exists())
            self.assertTrue((rt / "control.sock").exists())

            # R2-P1-2: When initial PID was unknown (0), must NOT adopt or SIGTERM concurrently started instance
            err_unknown = io.StringIO()
            with (
                mock.patch("agent_voice_input.control_ipc.send_control_command", side_effect=OSError("no sock")),
                mock.patch.object(cli_mod, "_resolve_daemon_target_pid", return_value=0),
                mock.patch.object(cli_mod, "_try_claim_daemon_lock", return_value=None),
                mock.patch(
                    "agent_voice_input.control_ipc._inspect_daemon_lock_holder",
                    return_value=(True, 333333),
                ),
                mock.patch.object(cli_mod, "_probe_pid_liveness", return_value="alive"),
                mock.patch.object(cli_mod, "_is_avi_daemon_pid", return_value=True),
                mock.patch.object(cli_mod.os, "kill", side_effect=_fake_kill),
                mock.patch("sys.stderr", err_unknown),
            ):
                rc2 = cli_mod._cmd_stop(env, graceful_timeout_sec=0.0, term_timeout_sec=0.0)
            self.assertEqual(rc2, 1)
            self.assertEqual(killed, [], "must NOT adopt/SIGTERM concurrent instance when initial pid=0")
            self.assertIn("333333", err_unknown.getvalue())
            self.assertIn("refusing to send SIGTERM", err_unknown.getvalue())
            self.assertTrue((rt / "daemon.pid").exists())
            self.assertTrue((rt / "control.sock").exists())

    def test_p1_3_responses_sse_stream_enforces_total_and_line_byte_limits_and_closes_conn(self) -> None:
        # 1. Direct unit tests on _parse_responses_sse_lines (single long line & total bytes)
        long_line = b"data: " + (b"x" * 2048) + b"\n\n"
        with self.assertRaises(refiner_mod._RefinerAttemptError) as ctx_line:
            _parse_responses_sse_lines(
                io.BytesIO(long_line).readline,
                provider_name="openai",
                resp_headers={},
                max_bytes=8192,
                max_line_bytes=512,
            )
        self.assertEqual(ctx_line.exception.retry_kind, "never")
        self.assertIn("line exceeded", str(ctx_line.exception))

        many_events = b"".join(
            f'data: {{"type":"response.output_text.delta","delta":"{"a"*100}"}}\n\n'.encode("utf-8")
            for _ in range(20)
        )
        with self.assertRaises(refiner_mod._RefinerAttemptError) as ctx_tot:
            _parse_responses_sse_lines(
                io.BytesIO(many_events).readline,
                provider_name="openai",
                resp_headers={},
                max_bytes=1024,
                max_line_bytes=512,
            )
        self.assertEqual(ctx_tot.exception.retry_kind, "never")
        self.assertIn("stream exceeded", str(ctx_tot.exception))

        # 2. Real loopback HTTP/1.1 keep-alive server: oversized SSE falls back to asr_draft & drops connection
        ports: list[int] = []
        mode_box = ["over_line"]

        class _SseHandler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if length > 0:
                    self.rfile.read(length)
                ports.append(int(self.client_address[1]))
                self.send_response(200)
                self.send_header("Connection", "keep-alive")
                if mode_box[0] == "over_line":
                    body = b"data: " + (b"a" * 2048) + b"\n\n"
                elif mode_box[0] == "over_total":
                    body = b"".join(
                        f'data: {{"type":"response.output_text.delta","delta":"{"b"*120}"}}\n\n'.encode("utf-8")
                        for _ in range(30)
                    )
                else:
                    body = (
                        b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
                        b'data: {"type":"response.completed","response":{"output":[]}}\n\n'
                    )
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                    self.wfile.flush()
                except OSError:
                    self.close_connection = True

        srv = _LoopbackHttpServer(("127.0.0.1", 0), _SseHandler)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            cfg = Config(
                refiner=RefinerConfig(
                    provider="openai",
                    api="responses",
                    model="gpt-6-luna",
                    base_url=f"http://127.0.0.1:{srv.server_port}/v1",
                    max_retries=0,
                )
            )
            env = {"OPENAI_API_KEY": "sk-p1-3-test"}
            ref = OpenAIResponsesRefiner(cfg, resolve_credentials(cfg, env), env=env)
            try:
                with (
                    mock.patch.object(refiner_mod, "DEFAULT_MAX_RESPONSE_BYTES", 2048),
                    mock.patch.object(refiner_mod, "DEFAULT_MAX_SSE_LINE_BYTES", 512),
                ):
                    mode_box[0] = "over_line"
                    r1 = ref.refine("草稿1")
                    self.assertEqual(r1.used_fallback, "asr_draft")
                    self.assertEqual(r1.text, "草稿1")

                    mode_box[0] = "over_total"
                    r2 = ref.refine("草稿2")
                    self.assertEqual(r2.used_fallback, "asr_draft")
                    self.assertEqual(r2.text, "草稿2")

                    mode_box[0] = "ok"
                    r3 = ref.refine("草稿3")
                    self.assertEqual(r3.used_fallback, "")
                    self.assertEqual(r3.text, "ok")
            finally:
                ref.close()
        finally:
            srv.shutdown()
            srv.server_close()

        # Both oversized attempts must have closed their connections (distinct client ports)
        self.assertEqual(len(ports), 3)
        self.assertNotEqual(ports[0], ports[1])
        self.assertNotEqual(ports[1], ports[2])

    def test_p1_4_openai_effort_mapping_floor_and_ceiling(self) -> None:
        cases = (
            # Non-luna gpt-6 models do not accept none/minimal -> clamp to low
            ("gpt-6-astra", "none", False, "low"),
            ("gpt-6-astra", "minimal", False, "low"),
            ("gpt-6-sol", "none", False, "low"),
            ("gpt-6.2-sol", "none", True, "low"),
            # gpt-6-luna and gpt-5.2/5.4 accept none and xhigh/max
            ("gpt-6-luna", "none", False, "none"),
            ("gpt-6-luna", "max", False, "xhigh"),
            ("gpt-6-luna", "max", True, "max"),
            ("gpt-5.2", "none", False, "none"),
            ("gpt-5.4", "none", False, "none"),
            # R2-P1-3: gpt-5.1 supports none/low/medium/high only (minimal->low, xhigh/max->high)
            ("gpt-5.1", "default", False, None),
            ("gpt-5.1", "none", False, "none"),
            ("gpt-5.1", "minimal", False, "low"),
            ("gpt-5.1", "minimal", True, "low"),
            ("gpt-5.1", "low", False, "low"),
            ("gpt-5.1", "medium", False, "medium"),
            ("gpt-5.1", "high", False, "high"),
            ("gpt-5.1", "xhigh", False, "high"),
            ("gpt-5.1", "xhigh", True, "high"),
            ("gpt-5.1", "max", False, "high"),
            ("gpt-5.1", "max", True, "high"),
            ("openai/gpt-5.1-2025-11-13", "minimal", False, "low"),
            ("openai/gpt-5.1-2025-11-13", "max", True, "high"),
            # R2-P1-3: gpt-5-pro supports high only (every non-default tier clamps to high)
            ("gpt-5-pro", "default", False, None),
            ("gpt-5-pro", "none", False, "high"),
            ("gpt-5-pro", "minimal", False, "high"),
            ("gpt-5-pro", "low", False, "high"),
            ("gpt-5-pro", "medium", False, "high"),
            ("gpt-5-pro", "high", False, "high"),
            ("gpt-5-pro", "xhigh", False, "high"),
            ("gpt-5-pro", "max", False, "high"),
            ("gpt-5-pro", "max", True, "high"),
            ("openai/gpt-5-pro-2025-10-06", "low", False, "high"),
            # Base gpt-5 / mini / nano cap at high (even with allow_max=True)
            ("gpt-5", "max", False, "high"),
            ("gpt-5", "max", True, "high"),
            ("gpt-5", "xhigh", False, "high"),
            ("gpt-5-mini", "max", False, "high"),
            ("gpt-5-mini", "xhigh", True, "high"),
            ("gpt-5-nano", "max", False, "high"),
            ("gpt-5-nano", "xhigh", False, "high"),
            ("gpt-5-mini", "none", False, "minimal"),
        )
        for model, eff, allow_max, expected in cases:
            self.assertEqual(
                llm_mod._resolve_openai_reasoning_effort(model, eff, allow_max=allow_max),
                expected,
                (model, eff, allow_max),
            )

    def test_p1_5_transcript_jsonl_heals_truncated_half_line_and_rolls_back_on_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tr_dir = Path(td) / "home" / "transcripts"
            tr_dir.mkdir(parents=True, mode=0o700)
            ts = 1_764_236_702.0
            month_name = (
                datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
                .astimezone()
                .strftime("%Y-%m.jsonl")
            )
            month_path = tr_dir / month_name

            # 1. Single truncated ASCII JSON half-line -> truncated back to 0 before append
            broken_ascii = '{"id":"broken","final":"truncated-without-newline'
            month_path.write_bytes(broken_ascii.encode("utf-8"))
            os.chmod(month_path, 0o600)

            rec1 = {"id": "20261127-120001-a1b2", "ts": format_local_iso_ts(ts), "final": "第一条合法记录"}
            self.assertEqual(append_transcript_record(tr_dir, rec1, now_ts=ts), month_path)
            lines1 = month_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual([json.loads(ln)["id"] for ln in lines1], [rec1["id"]])

            # 2. Valid line + truncated multi-byte UTF-8 tail -> truncated back to previous newline
            broken_utf8_tail = b'{"id":"broken-utf8","final":"\xe4\xb8'  # mid-character UTF-8
            with open(month_path, "ab") as f:
                f.write(broken_utf8_tail)
            rec2 = {"id": "20261127-120002-c3d4", "ts": format_local_iso_ts(ts + 1), "final": "第二条合法记录"}
            self.assertEqual(append_transcript_record(tr_dir, rec2, now_ts=ts + 1), month_path)
            raw_utf8 = month_path.read_text(encoding="utf-8")
            self.assertEqual(
                [json.loads(ln)["id"] for ln in raw_utf8.splitlines()],
                [rec1["id"], rec2["id"]],
            )

            # 3. Complete valid JSON object missing trailing newline -> preserved with '\n' added
            rec_no_nl = {"id": "20261127-120003-e5f6", "ts": format_local_iso_ts(ts + 2), "final": "完整无换行尾行"}
            with open(month_path, "ab") as f:
                f.write(json.dumps(rec_no_nl, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            rec3 = {"id": "20261127-120004-0123", "ts": format_local_iso_ts(ts + 3), "final": "第四条记录"}
            self.assertEqual(append_transcript_record(tr_dir, rec3, now_ts=ts + 3), month_path)
            rows_after_3 = [json.loads(ln)["id"] for ln in month_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows_after_3, [rec1["id"], rec2["id"], rec_no_nl["id"], rec3["id"]])

            # 4. Partial write failure rollback (also when preceded by truncated tail) -> rolls back to safe_pos
            good_snapshot = month_path.read_bytes()
            with open(month_path, "ab") as f:
                f.write(b'{"id":"half-before-enospc","final":"\xe6\xb5')

            def _partial_then_enospc(af: io.RawIOBase, payload: bytes) -> None:
                af.write(payload[:12])
                raise OSError(28, "No space left on device")

            rec_fail = {"id": "20261127-120005-9999", "ts": format_local_iso_ts(ts + 4), "final": "写失败应回滚"}
            with mock.patch.object(audio_mod, "_write_all_raw", side_effect=_partial_then_enospc):
                out_fail = append_transcript_record(tr_dir, rec_fail, now_ts=ts + 4)
            self.assertIsNone(out_fail)
            self.assertEqual(month_path.read_bytes(), good_snapshot)
            rows_after_rb = [json.loads(ln)["id"] for ln in month_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows_after_rb, [rec1["id"], rec2["id"], rec_no_nl["id"], rec3["id"]])

    def test_p1_6_selftest_wav_accepts_legacy_no_telemetry_and_no_diag(self) -> None:
        wav = _voiced_wav(0.6)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            rt = root / "rt"
            home.mkdir()
            rt.mkdir(mode=0o700)
            wav_path = root / "sample.wav"
            wav_path.write_bytes(wav)
            env = {"HOME": str(home), "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)}
            plat = _FakePlatform(wav)
            tracker = SessionTracker(home_dir=home, runtime_dir=rt, proc_root=root / "no_proc", env=env)
            daemon = VoiceInputDaemon(
                Config(refiner=RefinerConfig(provider="off")),
                env=env,
                runtime_dir=rt,
                platform=plat,
                asr_provider=_StubAsr("旧客户端自检"),
                refiner=NoopRefiner(),
                tracker=tracker,
            )
            try:
                resp = daemon._execute_selftest_wav(
                    {"cmd": "selftest_wav", "wav_path": str(wav_path), "no_telemetry": True}
                )
            finally:
                daemon.close()

            self.assertTrue(resp.get("ok"), resp)
            self.assertEqual(daemon.seq_id, 0)
            self.assertFalse((rt / DIAG_FILENAME).exists())

    def test_p1_7_non_utf8_config_and_env_raise_config_error_and_degrade_status_stop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            avi_home = root / "avi_home"
            rt = root / "rt"
            avi_home.mkdir(mode=0o700)
            rt.mkdir(mode=0o700)

            bad_cfg = avi_home / "config.toml"
            bad_cfg.write_bytes(b"\xff\xfe[asr]\nprovider = 'dashscope'\n")
            env = {
                "HOME": str(root),
                "AGENT_VOICE_INPUT_HOME": str(avi_home),
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
            }
            with self.assertRaises(ConfigError) as ctx_cfg:
                load_config(config_path=bad_cfg, env=env, load_default_env_files=False)
            self.assertIn(str(bad_cfg), str(ctx_cfg.exception))

            # status still probes daemon when config.toml is non-UTF-8
            buf_out = io.StringIO()
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch(
                    "agent_voice_input.control_ipc.probe_daemon_status",
                    return_value={"ok": True, "pid": 43210, "state": "idle", "version": cli_mod.__version__},
                ),
                mock.patch("sys.stdout", buf_out),
            ):
                rc_st = cli_mod.main(["status"])
            self.assertEqual(rc_st, 0)
            self.assertIn("running", buf_out.getvalue())
            self.assertIn("WARN", buf_out.getvalue())

            # Non-UTF-8 .env raises ConfigError in load_env_files, and status/stop still degrade cleanly
            bad_cfg.write_text("[refiner]\nprovider = 'off'\n", encoding="utf-8")
            bad_env = avi_home / ".env"
            bad_env.write_bytes(b"\xff\xfeOPENAI_API_KEY=sk-bad\n")
            with self.assertRaises(ConfigError) as ctx_env:
                load_env_files(env, env_paths=[bad_env])
            self.assertIn(str(bad_env), str(ctx_env.exception))

            buf_stop = io.StringIO()
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("sys.stdout", buf_stop),
            ):
                rc_stop = cli_mod.main(["stop"])
            self.assertEqual(rc_stop, 0)
            self.assertIn("stopped", buf_stop.getvalue())

            buf_st2 = io.StringIO()
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch(
                    "agent_voice_input.control_ipc.probe_daemon_status",
                    return_value={"ok": True, "pid": 43211, "state": "idle", "version": cli_mod.__version__},
                ),
                mock.patch("sys.stdout", buf_st2),
            ):
                rc_st2 = cli_mod.main(["status"])
            self.assertEqual(rc_st2, 0)
            self.assertIn("running", buf_st2.getvalue())

    def test_p2_2_doctor_is_read_only_for_runtime_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            missing_rt = root / "does_not_exist_rt"
            env = {
                "HOME": str(root),
                "AGENT_VOICE_INPUT_HOME": str(root / "avi_home"),
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(missing_rt),
            }
            cfg = Config(refiner=RefinerConfig(provider="off"))
            with (
                mock.patch.object(cli_mod, "_doctor_check_platform", lambda st: None),
                mock.patch("sys.stdout", new_callable=io.StringIO),
            ):
                cli_mod._cmd_doctor(cfg, env)
            self.assertFalse(missing_rt.exists(), "doctor must not create missing runtime_dir")

            existing_rt = root / "existing_rt"
            existing_rt.mkdir(mode=0o755)
            os.chmod(existing_rt, 0o755)
            env2 = {**env, "AGENT_VOICE_INPUT_RUNTIME_DIR": str(existing_rt)}
            with (
                mock.patch.object(cli_mod, "_doctor_check_platform", lambda st: None),
                mock.patch("sys.stdout", new_callable=io.StringIO),
            ):
                cli_mod._cmd_doctor(cfg, env2)
            self.assertEqual(stat.S_IMODE(existing_rt.stat().st_mode), 0o755)

    def test_p2_3_integrate_install_upgrades_legacy_managed_hooks_in_place(self) -> None:
        legacy_claude = next(iter(integrate_mod._LEGACY_CLAUDE_HOOK_COMMANDS))
        legacy_agy = next(iter(integrate_mod._LEGACY_AGY_HOOK_COMMANDS))
        chained_claude = "agent-voice-input session-hook --cli claude && my-custom"
        chained_agy = "agent-voice-input session-hook --cli agy && my-agy-custom"

        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            cache = Path(td) / "cache"
            (home / ".claude").mkdir(parents=True)
            claude_path = home / ".claude" / "settings.json"
            claude_path.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "SessionStart": [
                                {"hooks": [{"type": "command", "command": legacy_claude, "timeout": 7}]},
                                {"hooks": [{"type": "command", "command": chained_claude}]},
                            ],
                            "UserPromptSubmit": [
                                {"hooks": [{"type": "command", "command": legacy_claude}]},
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )

            res_c1 = integrate_mod.apply_integrations("claude", home_dir=home, cache_dir=cache, which_fn=lambda _: None)
            self.assertEqual(res_c1[0].action, "installed")
            after_c = json.loads(claude_path.read_text(encoding="utf-8"))
            self.assertEqual(
                after_c["hooks"]["SessionStart"][0]["hooks"][0],
                {"type": "command", "command": integrate_mod.CLAUDE_HOOK_COMMAND, "timeout": 7},
            )
            self.assertEqual(
                after_c["hooks"]["SessionStart"][1]["hooks"][0]["command"],
                chained_claude,
            )
            self.assertEqual(
                after_c["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"],
                integrate_mod.CLAUDE_HOOK_COMMAND,
            )
            res_c2 = integrate_mod.apply_integrations("claude", home_dir=home, cache_dir=cache, which_fn=lambda _: None)
            self.assertEqual(res_c2[0].action, "already_configured")

            # AGY in-place upgrade inside managed namespace
            agy_dir = home / ".gemini" / "config"
            agy_dir.mkdir(parents=True)
            agy_path = agy_dir / "hooks.json"
            agy_path.write_text(
                json.dumps(
                    {
                        integrate_mod.AGY_NAMESPACE_KEY: {
                            "SessionStart": [
                                {"type": "command", "command": legacy_agy, "timeout": 9},
                                {"type": "command", "command": chained_agy},
                            ],
                            "PreInvocation": [
                                {"type": "command", "command": legacy_agy, "timeout": 5},
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )
            res_a1 = integrate_mod.apply_integrations("agy", home_dir=home, cache_dir=cache, which_fn=lambda _: None)
            self.assertEqual(res_a1[0].action, "installed")
            after_a = json.loads(agy_path.read_text(encoding="utf-8"))
            ns = after_a[integrate_mod.AGY_NAMESPACE_KEY]
            self.assertEqual(
                ns["SessionStart"][0],
                {"type": "command", "command": integrate_mod.AGY_HOOK_COMMAND, "timeout": 9},
            )
            self.assertEqual(ns["SessionStart"][1]["command"], chained_agy)
            self.assertEqual(ns["PreInvocation"][0]["command"], integrate_mod.AGY_HOOK_COMMAND)
            res_a2 = integrate_mod.apply_integrations("agy", home_dir=home, cache_dir=cache, which_fn=lambda _: None)
            self.assertEqual(res_a2[0].action, "already_configured")

    def test_p2_4_toggle_and_cancel_human_output_surfaces_ignored_reason(self) -> None:
        out_qf = cli_mod._format_control_action_human(
            "toggle", {"ok": False, "ignored": "queue_full", "state": "processing", "seq_id": 8}
        )
        self.assertIn("queue_full", out_qf)
        self.assertNotIn("toggle failed (failed)", out_qf)

        out_st = cli_mod._format_control_action_human(
            "toggle", {"ok": False, "ignored": "stopping", "state": "idle", "seq_id": 9}
        )
        self.assertIn("ignored (stopping)", out_st)

        out_err = cli_mod._format_control_action_human(
            "toggle", {"ok": False, "error": "mic busy"}
        )
        self.assertIn("toggle failed (mic busy)", out_err)

        out_cancel_ign = cli_mod._format_control_action_human(
            "cancel", {"ok": False, "ignored": "stopping"}
        )
        self.assertIn("cancel ignored (stopping)", out_cancel_ign)

    def test_r2_p0_1_once_waits_for_slow_archive_past_4s_without_losing_audio_or_transcript(self) -> None:
        wav = _voiced_wav(0.6)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            rt = root / "rt"
            home.mkdir()
            rt.mkdir(mode=0o700)
            env = {
                "HOME": str(home),
                "AGENT_VOICE_INPUT_HOME": str(home / ".agent-voice-input"),
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
            }
            cfg = Config(
                archive=ArchiveConfig(save_audio=True, save_transcripts=True, audio_max_count=10),
                refiner=RefinerConfig(provider="off"),
            )
            # Slow fake ffmpeg taking 4.2s (> legacy 4.0s cap) before emitting a valid output file.
            slow_ffmpeg = root / "slow_ffmpeg_ok.sh"
            slow_ffmpeg.write_text(
                "#!/bin/sh\n"
                "sleep 4.2\n"
                "for last; do :; done\n"
                "printf 'ID3-FAKE-MP3-DATA' > \"$last\"\n"
                "exit 0\n",
                encoding="utf-8",
            )
            os.chmod(slow_ffmpeg, 0o700)

            spawned_threads: list[threading.Thread] = []
            orig_spawn = audio_mod.spawn_utterance_archive

            def _spy_spawn(**kw):
                th = orig_spawn(**kw)
                if th is not None:
                    spawned_threads.append(th)
                return th

            t0 = time.monotonic()
            with (
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value=str(slow_ffmpeg)),
                mock.patch("agent_voice_input.daemon_pipeline.spawn_utterance_archive", side_effect=_spy_spawn),
            ):
                res = run_pipeline_once(
                    wav,
                    cfg=cfg,
                    asr_provider=_StubAsr("慢转码一次归档测试"),
                    refiner=NoopRefiner(),
                    deliver_fn=lambda _t, _w: PasteOutcome(True, True, "ok", "Ghostty", False),
                    audio_source="avfoundation",
                    runtime_dir=rt,
                    env=env,
                )
            elapsed = time.monotonic() - t0
            self.assertTrue(res.ok)
            self.assertGreaterEqual(elapsed, 4.1)
            self.assertEqual(len(spawned_threads), 1)
            self.assertFalse(spawned_threads[0].is_alive(), "once must wait until archive thread finishes")

            audio_dir = resolve_archive_audio_dir(env)
            tr_dir = resolve_archive_transcripts_dir(env)
            audio_files = [p.name for p in audio_dir.iterdir()]
            self.assertEqual(audio_files, [f"{res.utterance_id}.mp3"])
            tr_files = list(tr_dir.glob("*.jsonl"))
            self.assertEqual(len(tr_files), 1)
            row = json.loads(tr_files[0].read_text(encoding="utf-8").strip())
            self.assertEqual(row["id"], res.utterance_id)
            self.assertEqual(row["audio_file"], f"{res.utterance_id}.mp3")
            self.assertEqual(row["final"], "慢转码一次归档测试")

    def test_r2_p1_1_startup_preflight_agy_timeout_reaps_subprocess_group(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pids_file = root / "agy_pids.txt"
            fake_agy = root / "fake_agy.sh"
            # Ignore SIGTERM and spawn a background child in the same process group to prove full PGID reap.
            fake_agy.write_text(
                "#!/bin/sh\n"
                "trap '' TERM\n"
                "sleep 30 &\n"
                "child=$!\n"
                f"printf '%s %s\\n' \"$$\" \"$child\" > '{pids_file}'\n"
                "sleep 30\n",
                encoding="utf-8",
            )
            os.chmod(fake_agy, 0o700)

            cfg = Config(
                asr=AsrConfig(provider="deepseek", model="deepseek-asr"),
                refiner=RefinerConfig(provider="agy", model="gemini-3-flash"),
            )
            env = {
                "HOME": str(root),
                "AGY_BIN": str(fake_agy),
                "DEEPSEEK_API_KEY": "sk-ds-fast-ok",
            }
            creds = resolve_credentials(cfg, env)
            pipe_info = {
                "asr": {"provider": "deepseek", "model": "deepseek-asr", "ready": True},
                "refiner": {"provider": "agy", "model": "gemini-3-flash", "ready": True, "bypassed": False},
            }

            t0 = time.monotonic()
            rows = cli_mod._run_startup_preflight_ping(
                cfg=cfg,
                creds=creds,
                env=env,
                pipe_info=pipe_info,
                transport=lambda _req: HttpResponse(200, {}, b'{"data":[{"id":"deepseek-asr"}]}'),
                timeout_sec=0.8,
            )
            dt = time.monotonic() - t0
            self.assertLess(dt, 1.4, f"preflight exceeded budget: {dt:.2f}s")
            self.assertTrue(
                any(lbl == "WARN" and "refiner(agy)" in txt and "timed out" in txt for lbl, txt in rows),
                rows,
            )
            self.assertTrue(pids_file.is_file(), "fake agy must have actually started")
            raw_pids = [int(tok) for tok in pids_file.read_text(encoding="utf-8").split() if tok.isdigit()]
            self.assertEqual(len(raw_pids), 2)
            # Both main fake_agy process and its child in the same process group must already be gone.
            for p in raw_pids:
                with self.assertRaises(ProcessLookupError, msg=f"orphaned agy pid={p} still alive"):
                    os.kill(p, 0)

    def test_r2_p1_5_regress_matrix_sends_dual_flags_and_detects_legacy_telemetry_growth(self) -> None:
        import importlib.util

        rm_path = _REPO_ROOT / ".agents" / "skills" / "voice-eval" / "scripts" / "regress_matrix.py"
        spec = importlib.util.spec_from_file_location("test_regress_matrix_r2_mod", rm_path)
        assert spec is not None and spec.loader is not None
        rm_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rm_mod)

        wav = _voiced_wav(0.6)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rt = root / "rt"
            rt.mkdir(mode=0o700)
            (rt / "control.sock").write_text("", encoding="utf-8")
            wav_path = root / "probe.wav"
            wav_path.write_bytes(wav)
            cfg = Config(refiner=RefinerConfig(provider="off"))
            env = {"HOME": str(root), "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)}

            captured_reqs: list[dict[str, object]] = []
            force_legacy_leak = [False]

            def _fake_legacy_daemon_ipc(_rt: Path, req: dict[str, object], timeout_sec: float = 5.0) -> dict[str, object]:
                del timeout_sec
                cmd = req.get("cmd")
                if cmd == "status":
                    return {
                        "ok": True,
                        "shim_ok": True,
                        "version": "0.17.1",
                        "state": "idle",
                        "asr_provider": "dashscope",
                        "refiner_provider": "off",
                    }
                if cmd == "probe_route":
                    if req.get("use_live_window"):
                        return {"ok": True, "mode": "agent", "window_category": "terminal"}
                    if "[@remote]" in str(req.get("title") or ""):
                        return {"ok": True, "mode": "agent", "remote_vetoed": True, "session_key_masked": "", "ctx_chars": 0}
                    return {"ok": True, "mode": "general", "session_key_masked": "", "ctx_chars": 0}
                if cmd == "selftest_wav":
                    captured_reqs.append(dict(req))
                    # Simulate legacy daemon that ONLY checks `no_telemetry` (ignoring `no_diag`)
                    if force_legacy_leak[0] or req.get("no_telemetry") is not True:
                        with open(rt / "telemetry.jsonl", "a", encoding="utf-8") as f:
                            f.write('{"seq_id":1,"ok":true}\n')
                    return {
                        "ok": True,
                        "final_text": "hello",
                        "final_chars": 5,
                        "summary": {"asr_ms": 10, "refine_ms": 0, "total_ms": 10, "asr_chars": 5, "final_chars": 5},
                        "routing": {"mode": "agent", "session_key_masked": ""},
                    }
                raise AssertionError(f"unexpected cmd: {cmd}")

            # 1. Legacy daemon honoring `no_telemetry`: new script sends BOTH flags -> zero writes -> PASS
            with (
                mock.patch.object(rm_mod, "send_control_command", side_effect=_fake_legacy_daemon_ipc),
                mock.patch.object(rm_mod, "describe_daemon_drift", return_value={"drifted": False}),
            ):
                checks_ok = rm_mod.run_layer_b_live_daemon(
                    cfg,
                    env,
                    env,
                    lambda s: str(s),
                    runtime_dir_arg=str(rt),
                    wav_arg=str(wav_path),
                    allow_drift=True,
                    skip_selftest_wav=False,
                    deliver_live=False,
                    delay_sec=0.0,
                    show_text=False,
                )
            self.assertEqual(len(captured_reqs), 1)
            self.assertIs(captured_reqs[0].get("no_diag"), True)
            self.assertIs(captured_reqs[0].get("no_telemetry"), True)
            b3_ok = next(c for c in checks_ok if c["id"] == "B3_selftest_wav")
            self.assertEqual(b3_ok["status"], "PASS", b3_ok)
            self.assertEqual(b3_ok["metrics"]["diag_lines_delta"], 0)
            self.assertEqual(b3_ok["metrics"]["legacy_telemetry_lines_delta"], 0)

            # 2. If legacy telemetry.jsonl grows while diag.jsonl stays 0 -> B3 MUST FAIL
            force_legacy_leak[0] = True
            with (
                mock.patch.object(rm_mod, "send_control_command", side_effect=_fake_legacy_daemon_ipc),
                mock.patch.object(rm_mod, "describe_daemon_drift", return_value={"drifted": False}),
            ):
                checks_leak = rm_mod.run_layer_b_live_daemon(
                    cfg,
                    env,
                    env,
                    lambda s: str(s),
                    runtime_dir_arg=str(rt),
                    wav_arg=str(wav_path),
                    allow_drift=True,
                    skip_selftest_wav=False,
                    deliver_live=False,
                    delay_sec=0.0,
                    show_text=False,
                )
            b3_leak = next(c for c in checks_leak if c["id"] == "B3_selftest_wav")
            self.assertEqual(b3_leak["status"], "FAIL", b3_leak)
            self.assertEqual(b3_leak["metrics"]["diag_lines_delta"], 0)
            self.assertEqual(b3_leak["metrics"]["legacy_telemetry_lines_delta"], 1)

    def test_r2_p1_6_shutdown_during_delivered_commit_persists_matching_diag_and_skips_hud(self) -> None:
        wav = _voiced_wav(0.6)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            rt = root / "rt"
            home.mkdir()
            rt.mkdir(mode=0o700)
            env = {
                "HOME": str(home),
                "AGENT_VOICE_INPUT_HOME": str(home / ".agent-voice-input"),
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
            }
            cfg = Config(
                archive=ArchiveConfig(save_audio=True, save_transcripts=True, audio_max_count=10),
                refiner=RefinerConfig(provider="off"),
            )
            hud_events: list[str] = []

            class _SpyPlatform(_FakePlatform):
                def hud(self, state: str, *args, **kwargs) -> None:
                    del args, kwargs
                    hud_events.append(state)

            plat = _SpyPlatform(wav)
            tracker = SessionTracker(home_dir=home, runtime_dir=rt, proc_root=root / "no_proc", env=env)
            daemon = VoiceInputDaemon(
                cfg,
                env=env,
                runtime_dir=rt,
                platform=plat,
                asr_provider=_StubAsr("停机瞬间已交付口述"),
                refiner=NoopRefiner(),
                tracker=tracker,
                paste_settle_sec=0.0,
            )

            archive_registered = threading.Event()
            allow_commit_continue = threading.Event()
            orig_reg = daemon._register_archive_thread

            def _hook_reg(th: threading.Thread) -> None:
                orig_reg(th)
                archive_registered.set()
                allow_commit_continue.wait(timeout=3.0)

            daemon._register_archive_thread = _hook_reg  # type: ignore[method-assign]
            try:
                with mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", side_effect=OSError("no ffmpeg")):
                    daemon.handle_trigger("toggle")
                    daemon._last_trigger_ts = -1e9
                    daemon.handle_trigger("toggle")
                    self.assertTrue(archive_registered.wait(timeout=3.0), "archive should be registered after delivery")
                    # Trigger shutdown right after delivery+archive registration, before _append_diag runs
                    hud_events.clear()
                    close_th = threading.Thread(target=daemon.close)
                    close_th.start()
                    dl = time.monotonic() + 2.0
                    while time.monotonic() < dl and not daemon._stop_evt.is_set():
                        time.sleep(0.005)
                    self.assertTrue(daemon._stop_evt.is_set())
                    allow_commit_continue.set()
                    close_th.join(timeout=5.0)
                    self.assertFalse(close_th.is_alive())
            finally:
                allow_commit_continue.set()
                daemon.close()

            self.assertEqual(plat.pasted_texts, ["停机瞬间已交付口述"])
            self.assertEqual(hud_events, [], "must skip post-delivery HUD when stopping")
            diag_rows = read_diag_history(rt, limit=10)
            self.assertEqual(len(diag_rows), 1)
            diag_id = diag_rows[0]["id"]
            self.assertEqual(daemon.last_result.get("id"), diag_id)

            tr_dir = resolve_archive_transcripts_dir(env)
            tr_files = list(tr_dir.glob("*.jsonl"))
            self.assertEqual(len(tr_files), 1)
            tr_row = json.loads(tr_files[0].read_text(encoding="utf-8").strip())
            self.assertEqual(tr_row["id"], diag_id)

            audio_dir = resolve_archive_audio_dir(env)
            self.assertEqual([p.name for p in audio_dir.iterdir()], [f"{diag_id}.wav"])

    def test_r3_p1_1_startup_preflight_codex_timeout_reaps_subprocess_group(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pids_file = root / "codex_pids.txt"
            fake_codex = root / "fake_codex.sh"
            # Ignore SIGTERM and spawn a background child in the same process group to prove full PGID reap.
            fake_codex.write_text(
                "#!/bin/sh\n"
                "trap '' TERM\n"
                "sleep 30 &\n"
                "child=$!\n"
                f"printf '%s %s\\n' \"$$\" \"$child\" > '{pids_file}'\n"
                "sleep 30\n",
                encoding="utf-8",
            )
            os.chmod(fake_codex, 0o700)

            cfg = Config(
                asr=AsrConfig(provider="deepseek", model="deepseek-asr"),
                refiner=RefinerConfig(provider="codex", model="gpt-5.4-mini"),
            )
            env = {
                "HOME": str(root),
                "CODEX_HOME": str(root / ".codex"),
                "CODEX_BIN": str(fake_codex),
                "DEEPSEEK_API_KEY": "sk-ds-fast-ok",
            }
            creds = resolve_credentials(cfg, env)
            pipe_info = {
                "asr": {"provider": "deepseek", "model": "deepseek-asr", "ready": True},
                "refiner": {"provider": "codex", "model": "gpt-5.4-mini", "ready": True, "bypassed": False},
            }

            t0 = time.monotonic()
            rows = cli_mod._run_startup_preflight_ping(
                cfg=cfg,
                creds=creds,
                env=env,
                pipe_info=pipe_info,
                transport=lambda _req: HttpResponse(200, {}, b'{"data":[{"id":"deepseek-asr"}]}'),
                timeout_sec=1.5,
            )
            dt = time.monotonic() - t0
            self.assertLess(dt, 2.0, f"codex preflight exceeded budget: {dt:.2f}s")
            self.assertTrue(
                any(lbl == "WARN" and "refiner(codex)" in txt and "timed out" in txt for lbl, txt in rows),
                rows,
            )
            self.assertTrue(pids_file.is_file(), "fake codex must have actually started")
            raw_pids = [int(tok) for tok in pids_file.read_text(encoding="utf-8").split() if tok.isdigit()]
            self.assertEqual(len(raw_pids), 2)
            # At the exact instant preflight returns, both fake_codex and its child in the PGID must be gone.
            for p in raw_pids:
                with self.assertRaises(ProcessLookupError, msg=f"orphaned codex pid={p} still alive"):
                    os.kill(p, 0)

    def test_r3_p1_2_stop_expected_pid_prevents_stopping_replaced_daemon_instance(self) -> None:
        wav = _voiced_wav(0.6)
        with tempfile.TemporaryDirectory(prefix="avi-r3-") as td:
            root = Path(td)
            home = root / "home"
            rt = root / "rt"
            home.mkdir()
            rt.mkdir(mode=0o700)
            env = {
                "HOME": str(home),
                "AGENT_VOICE_INPUT_HOME": str(home / ".agent-voice-input"),
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
            }
            cfg = Config(refiner=RefinerConfig(provider="off"))
            plat = _FakePlatform(wav)
            tracker = SessionTracker(home_dir=home, runtime_dir=rt, proc_root=root / "no_proc", env=env)
            new_daemon = VoiceInputDaemon(
                cfg,
                env=env,
                runtime_dir=rt,
                platform=plat,
                asr_provider=_StubAsr("新实例存活"),
                refiner=NoopRefiner(),
                tracker=tracker,
                paste_settle_sec=0.0,
            )
            srv_th = threading.Thread(target=new_daemon.serve_forever, daemon=True)
            srv_th.start()
            try:
                dl = time.monotonic() + 3.0
                while time.monotonic() < dl:
                    if (rt / "control.sock").exists():
                        try:
                            if send_control_command(rt, {"cmd": "status"}, timeout_sec=0.5).get("ok"):
                                break
                        except Exception:
                            pass
                    time.sleep(0.02)
                self.assertTrue(send_control_command(rt, {"cmd": "status"}, timeout_sec=1.0).get("ok"))

                # Invalid expected_pid values are rejected without initiating shutdown
                for bad_pid in (True, 0, -5, "1234"):
                    bad_resp = new_daemon.handle_control({"cmd": "shutdown", "expected_pid": bad_pid})
                    self.assertFalse(bad_resp.get("ok"))
                    self.assertEqual(bad_resp.get("error"), "invalid expected_pid")
                    self.assertEqual(bad_resp.get("pid"), os.getpid())
                    self.assertFalse(new_daemon._stop_evt.is_set())

                # Simulate instance replacement between reading old_pid and connecting to control.sock
                stale_old_pid = os.getpid() + 10000
                err_buf = io.StringIO()
                out_buf = io.StringIO()
                with (
                    mock.patch.object(cli_mod, "_resolve_daemon_target_pid", return_value=stale_old_pid),
                    mock.patch("sys.stderr", err_buf),
                    mock.patch("sys.stdout", out_buf),
                ):
                    rc_mismatch = cli_mod._cmd_stop(env, graceful_timeout_sec=1.0, term_timeout_sec=0.5)
                self.assertEqual(rc_mismatch, 1)
                self.assertIn("changed before shutdown", err_buf.getvalue())
                self.assertIn(f"pid={os.getpid()}", err_buf.getvalue())
                self.assertIn(f"initial pid={stale_old_pid}", err_buf.getvalue())
                self.assertFalse(new_daemon._stop_evt.is_set(), "new daemon instance must NOT be shut down")
                self.assertTrue((rt / "control.sock").exists())
                self.assertTrue((rt / "daemon.pid").exists())
                self.assertTrue(send_control_command(rt, {"cmd": "status"}, timeout_sec=1.0).get("ok"))

                # Normal stop resolves real os.getpid(), sends matching expected_pid, and stops cleanly
                out_ok = io.StringIO()
                err_ok = io.StringIO()
                with (
                    mock.patch.object(cli_mod, "_probe_pid_liveness", return_value="dead"),
                    mock.patch("sys.stdout", out_ok),
                    mock.patch("sys.stderr", err_ok),
                ):
                    rc_ok = cli_mod._cmd_stop(env, graceful_timeout_sec=3.0, term_timeout_sec=0.5)
                self.assertEqual(rc_ok, 0, err_ok.getvalue())
                srv_th.join(timeout=3.0)
                self.assertFalse(srv_th.is_alive())
            finally:
                new_daemon.close()
                srv_th.join(timeout=2.0)

            # Legacy client without expected_pid remains backward-compatible
            legacy_daemon = VoiceInputDaemon(
                cfg,
                env=env,
                runtime_dir=rt,
                platform=_FakePlatform(wav),
                asr_provider=_StubAsr("旧客户端"),
                refiner=NoopRefiner(),
                tracker=SessionTracker(home_dir=home, runtime_dir=rt, proc_root=root / "no_proc", env=env),
                paste_settle_sec=0.0,
            )
            try:
                legacy_resp = legacy_daemon.handle_control({"cmd": "shutdown"})
                self.assertEqual(legacy_resp, {"ok": True, "stopping": True})
                self.assertTrue(legacy_daemon._stop_evt.is_set())
            finally:
                legacy_daemon.close()

            # Initial pid <= 0 with lock held by unknown pid: refuse without sending untargeted shutdown
            rt_unk = root / "rt_unk"
            rt_unk.mkdir(mode=0o700)
            env_unk = {**env, "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt_unk)}
            (rt_unk / "control.sock").write_text("sock", encoding="utf-8")
            held_fd = os.open(str(rt_unk / "daemon.lock"), os.O_CREAT | os.O_RDWR, 0o600)
            os.fchmod(held_fd, 0o600)
            fcntl.flock(held_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                err_unk = io.StringIO()
                with (
                    mock.patch(
                        "agent_voice_input.control_ipc.send_control_command",
                        side_effect=AssertionError("must not send untargeted shutdown when pid is unknown"),
                    ),
                    mock.patch("sys.stderr", err_unk),
                ):
                    rc_unk = cli_mod._cmd_stop(env_unk, graceful_timeout_sec=0.1, term_timeout_sec=0.1)
                self.assertEqual(rc_unk, 1)
                self.assertIn("no initial target pid", err_unk.getvalue())
            finally:
                fcntl.flock(held_fd, fcntl.LOCK_UN)
                os.close(held_fd)

            # Missing daemon.pid recovered via daemon.lock holder sends targeted expected_pid shutdown
            rt_rec = root / "rt_rec"
            rt_rec.mkdir(mode=0o700)
            env_rec = {**env, "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt_rec)}
            captured_payloads: list[dict[str, object]] = []

            def _capture_send(_rt: Path, payload: dict[str, object], timeout_sec: float = 1.5) -> dict[str, object]:
                del timeout_sec
                captured_payloads.append(dict(payload))
                return {"ok": True, "stopping": True}

            with (
                mock.patch("agent_voice_input.control_ipc._inspect_daemon_lock_holder", return_value=(True, 54321)),
                mock.patch("agent_voice_input.control_ipc.send_control_command", side_effect=_capture_send),
                mock.patch.object(cli_mod, "_probe_pid_liveness", return_value="dead"),
                mock.patch("sys.stdout", io.StringIO()),
            ):
                rc_rec = cli_mod._cmd_stop(env_rec, graceful_timeout_sec=0.2, term_timeout_sec=0.1)
            self.assertEqual(rc_rec, 0)
            self.assertEqual(captured_payloads, [{"cmd": "shutdown", "expected_pid": 54321}])


if __name__ == "__main__":
    unittest.main()
