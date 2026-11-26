"""Full-pipeline offline E2E tests: real ffmpeg + SessionTracker + hotwords.txt +
two-stage loopback HTTP (Stage 1 ASR + Stage 2 SSE Refiner) + failed/ -> cli retry recovery.

Never accesses the internet or host user config/cache/runtime directories.
"""
from __future__ import annotations

from email.parser import BytesParser
from email.policy import default as email_policy_default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable
import unittest
from unittest import mock
import wave

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agent_voice_input import cli as cli_mod  # noqa: E402
from agent_voice_input.audio_proc import (  # noqa: E402
    AudioError,
    list_failed_audios,
    process_wav,
    resolve_ffmpeg_bin,
)
from agent_voice_input.config import (  # noqa: E402
    AsrConfig,
    AudioConfig,
    Config,
    HotkeyConfig,
    RefinerConfig,
)
from agent_voice_input.daemon import (  # noqa: E402
    VoiceInputDaemon,
    read_telemetry_history,
)
from agent_voice_input.platform_common import ForegroundWindow, PasteOutcome  # noqa: E402
from agent_voice_input.session_ctx import SessionTracker, encode_claude_project_dir  # noqa: E402


def _make_noncanonical_48k_stereo_wav() -> bytes:
    """Build a 48 kHz 2-channel 16-bit WAV with leading silence + speech tone + trailing silence.

    Forces ``decode_to_16k_mono_pcm`` through real ``ffmpeg`` resampling/downmixing
    (cannot hit the 16 kHz mono fast path).
    """
    sr = 48000
    lead_n = int(sr * 0.25)
    voice_n = int(sr * 0.65)
    tail_n = int(sr * 0.25)
    frames = bytearray()
    frames.extend(b"\x00\x00\x00\x00" * lead_n)
    for i in range(voice_n):
        left = int(3800 * math.sin(2.0 * math.pi * 310.0 * i / sr))
        right = int(3800 * math.sin(2.0 * math.pi * 465.0 * i / sr))
        frames.extend(left.to_bytes(2, "little", signed=True))
        frames.extend(right.to_bytes(2, "little", signed=True))
    frames.extend(b"\x00\x00\x00\x00" * tail_n)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(bytes(frames))
    return buf.getvalue()


def _parse_multipart_fields(content_type: str, body: bytes) -> dict[str, tuple[bytes, str]]:
    """Parse ``multipart/form-data`` into ``{name: (raw_bytes, filename)}`` using stdlib email."""
    msg = BytesParser(policy=email_policy_default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + body
    )
    out: dict[str, tuple[bytes, str]] = {}
    if msg.is_multipart():
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if isinstance(name, str) and name:
                fn = part.get_filename() or ""
                payload = part.get_payload(decode=True) or b""
                out[name] = (payload, fn)
    return out


def _write_large_claude_jsonl(home: Path, sid: str, cwd: str) -> Path:
    """Create a real Claude JSONL session >55,000 chars so SessionTracker produces >=2 50K chunks."""
    encs = encode_claude_project_dir(cwd)
    pdir = home / ".claude" / "projects" / (encs[0] if encs else "-proj")
    pdir.mkdir(parents=True, exist_ok=True)
    jf = pdir / f"{sid}.jsonl"

    lines: list[str] = []
    # 16 turns x ~3,800 chars ≈ 60,800 chars -> guarantees >= 2 50K refiner chunks (sealed chunk 0 + growing chunk 1)
    for idx in range(8):
        u_txt = (
            f"Turn {idx}: investigate KVCacheScheduler eviction policy in kv_cache_scheduler.py "
            f"and verify OpenTelemetry span propagation across worker pool #{idx}. "
            + (f"detail_block_{idx}_kv_cache_scheduler_opentelemetry_trace_context " * 55)
        )
        a_txt = (
            f"Reply {idx}: verified KVCacheScheduler ring buffer watermarks and OpenTelemetry "
            f"exporter batch timeout in kv_cache_scheduler.py for batch #{idx}. "
            + (f"analysis_block_{idx}_ring_buffer_watermark_batch_timeout_verified " * 55)
        )
        for role, text in (("user", u_txt), ("assistant", a_txt)):
            lines.append(
                json.dumps(
                    {
                        "sessionId": sid,
                        "cwd": cwd,
                        "isSidechain": False,
                        "type": role,
                        "message": {"role": role, "content": [{"type": "text", "text": text}]},
                    },
                    ensure_ascii=False,
                )
            )
    jf.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jf


class _LoopbackTwoStageServer:
    """Local loopback HTTP server serving Stage 1 ``/v1/audio/transcriptions`` and Stage 2 SSE ``/v1/responses``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.asr_requests: list[dict[str, Any]] = []
        self.refiner_requests: list[dict[str, Any]] = []
        self.fail_stage2 = False
        self.asr_text = "帮我看下 kv cache scheduler 和 open telemetry 的超时配置"
        self.refined_deltas = [
            "帮我看下 KVCacheScheduler 和 ",
            "OpenTelemetry 的超时配置",
        ]

        owner = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                del format, args

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or "0")
                raw = self.rfile.read(length)
                ctype = str(self.headers.get("Content-Type") or "")
                if self.path.endswith("/audio/transcriptions"):
                    fields = _parse_multipart_fields(ctype, raw)
                    wav_bytes = fields.get("file", (b"", ""))[0]
                    prompt_txt = fields.get("prompt", (b"", ""))[0].decode("utf-8", errors="replace")
                    model_txt = fields.get("model", (b"", ""))[0].decode("utf-8", errors="replace")
                    with owner._lock:
                        owner.asr_requests.append(
                            {
                                "path": self.path,
                                "model": model_txt,
                                "prompt": prompt_txt,
                                "wav_bytes": wav_bytes,
                            }
                        )
                        out_text = owner.asr_text
                    body = json.dumps({"text": out_text}, ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if self.path.endswith("/responses"):
                    req_obj = json.loads(raw.decode("utf-8"))
                    with owner._lock:
                        owner.refiner_requests.append(req_obj)
                        should_fail = owner.fail_stage2
                        deltas = list(owner.refined_deltas)
                    if should_fail:
                        err_body = json.dumps(
                            {"error": {"message": "injected loopback stage2 outage", "type": "server_error"}}
                        ).encode("utf-8")
                        self.send_response(500)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(err_body)))
                        self.send_header("Connection", "close")
                        self.end_headers()
                        self.wfile.write(err_body)
                        return

                    sse_chunks: list[bytes] = []
                    for d in deltas:
                        ev = json.dumps({"type": "response.output_text.delta", "delta": d}, ensure_ascii=False)
                        sse_chunks.append(f"event: response.output_text.delta\ndata: {ev}\n\n".encode("utf-8"))
                    done_ev = json.dumps(
                        {
                            "type": "response.completed",
                            "response": {
                                "status": "completed",
                                "usage": {
                                    "input_tokens": 14000,
                                    "input_tokens_details": {"cached_tokens": 12000},
                                    "output_tokens": 18,
                                    "output_tokens_details": {"reasoning_tokens": 0},
                                },
                            },
                        },
                        ensure_ascii=False,
                    )
                    sse_chunks.append(f"event: response.completed\ndata: {done_ev}\n\n".encode("utf-8"))
                    sse_body = b"".join(sse_chunks)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Content-Length", str(len(sse_body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(sse_body)
                    return

                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = int(self._server.server_address[1])
        self.base_url = f"http://127.0.0.1:{self.port}/v1"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=3.0)


class _RecordingDeliveryPlatform:
    """Offline platform double returning non-canonical 48 kHz stereo WAV and recording deliveries."""

    def __init__(self, raw_wav: bytes, window: ForegroundWindow) -> None:
        self._raw_wav = raw_wav
        self._window = window
        self.fail_delivery = False
        self.deliveries: list[str] = []
        self.last_audio_error = ""
        self._lock = threading.Lock()

    def set_recorder_lost_callback(self, cb: Callable[..., Any] | None) -> None:
        del cb

    def set_recorder_recovered_callback(self, cb: Callable[..., Any] | None) -> None:
        del cb

    def start_event_listener(self, on_hotkey_event: Callable[[dict[str, Any]], None]) -> None:
        del on_hotkey_event

    def is_allowed_terminal(self, window: ForegroundWindow) -> bool:
        del window
        return True

    def get_foreground_window(self) -> ForegroundWindow:
        return self._window

    def resolve_foreground_window(self, hint: ForegroundWindow | None = None) -> ForegroundWindow:
        return hint if (hint and hint.process) else self._window

    def start_recording(self, runtime_dir: Path, *, loopback: bool = False, seq_id: int | None = None) -> None:
        del runtime_dir, loopback, seq_id

    def stop_recording(self, *, drain: bool = True) -> tuple[bytes, str]:
        del drain
        return self._raw_wav, "loopback_e2e_mic"

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
        del state, text, duration_ms, layer, clear_main, clear_toast, bg_count

    def beep(self, kind: str) -> None:
        del kind

    def deliver_text(
        self,
        text: str,
        seq_id: int,
        target_hwnd: int = 0,
        *,
        origin_window: ForegroundWindow | None = None,
        fallback_clipboard_text: str | None = None,
        suppress_beep: bool | Callable[[], bool] = False,
        is_cancelled: Callable[[], bool] | None = None,
        before_clipboard_write: Callable[[], None] | None = None,
        before_commit: Callable[[], bool] | None = None,
        same_window_verifier: object = None,
    ) -> PasteOutcome:
        del seq_id, target_hwnd, origin_window, fallback_clipboard_text, suppress_beep
        if is_cancelled is not None and is_cancelled():
            return PasteOutcome(False, False, "cancelled", "")
        if before_clipboard_write is not None:
            before_clipboard_write()
        if before_commit is not None and not before_commit():
            return PasteOutcome(False, False, "cancelled", "")
        if self.fail_delivery:
            return PasteOutcome(False, False, "paste_failed", self._window.process, used_clipboard_paste=False)
        with self._lock:
            self.deliveries.append(text)
        return PasteOutcome(True, True, "pasted", self._window.process, used_clipboard_paste=True)

    def close(self) -> None:
        pass


class FullPipelineLoopbackE2ETests(unittest.TestCase):
    def _require_ffmpeg_and_git(self) -> tuple[str, str]:
        try:
            ff_bin = resolve_ffmpeg_bin()
        except AudioError:
            ff_bin = ""
        git_bin = shutil.which("git") or ""
        missing = [n for n, p in (("ffmpeg", ff_bin), ("git", git_bin)) if not p]
        if missing:
            msg = f"full pipeline e2e requires {', '.join(missing)}"
            if os.environ.get("AGENT_VOICE_INPUT_CI_NO_SKIP") == "1":
                self.fail(f"AGENT_VOICE_INPUT_CI_NO_SKIP=1: {msg}")
            self.skipTest(msg)
        return ff_bin, git_bin

    def _wait_daemon_idle(self, daemon: VoiceInputDaemon, *, min_seq: int, timeout: float = 8.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            st = daemon.handle_control({"cmd": "status"})
            lr = st.get("last_result") if isinstance(st.get("last_result"), dict) else {}
            if (
                st.get("state") == "idle"
                and int(st.get("pending_count", 0)) == 0
                and int(lr.get("seq_id") or 0) >= min_seq
            ):
                return st
            time.sleep(0.02)
        self.fail(f"daemon did not finish seq>={min_seq} within {timeout}s; status={daemon.handle_control({'cmd': 'status'})!r}")

    def test_real_ffmpeg_session_hotwords_two_stage_loopback_and_failed_retry_e2e(self) -> None:
        _ff_bin, git_bin = self._require_ffmpeg_and_git()

        base_dir = Path(tempfile.mkdtemp(prefix="avi-pipe-", dir="/tmp"))
        os.chmod(base_dir, 0o700)
        home_dir = base_dir / "home"
        cfg_dir = home_dir / ".config" / "agent-voice-input"
        cache_dir = base_dir / "cache"
        rt_dir = base_dir / "rt"
        repo_dir = home_dir / "work" / "infer-engine"
        empty_proc = base_dir / "empty_proc"
        for d in (home_dir, cfg_dir, cache_dir, rt_dir, repo_dir, empty_proc):
            d.mkdir(parents=True, mode=0o700, exist_ok=True)

        server = _LoopbackTwoStageServer()
        daemon: VoiceInputDaemon | None = None
        try:
            # 1. Build a real dirty Git workspace
            git_env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(home_dir),
                "GIT_CONFIG_NOSYSTEM": "1",
            }
            (repo_dir / "engine_core.py").write_text("def step():\n    return 1\n", encoding="utf-8")
            for gcmd in (
                [git_bin, "init", "-q"],
                [git_bin, "config", "user.email", "ci@example.com"],
                [git_bin, "config", "user.name", "CI Runner"],
                [git_bin, "add", "engine_core.py"],
                [git_bin, "commit", "-q", "-m", "init"],
            ):
                gp = subprocess.run(
                    gcmd,
                    cwd=str(repo_dir),
                    env=git_env,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(gp.returncode, 0, f"{gcmd} failed: {gp.stderr!r}")
            # Dirty untracked file to surface in git status
            (repo_dir / "kv_cache_scheduler.py").write_text("class KVCacheScheduler:\n    pass\n", encoding="utf-8")

            # 2. Build a real >55K Claude JSONL session & segmented hotwords.txt
            real_repo = str(repo_dir.resolve())
            sid = "22222222-3333-4444-5555-666666666666"
            _write_large_claude_jsonl(home_dir, sid, real_repo)

            hw_file = home_dir / ".agent-voice-input" / "hotwords" / "personal.txt"
            hw_file.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            hw_file.write_text(
                "\n".join(
                    [
                        "!KVCacheScheduler",
                        "OpenTelemetry",
                        "vLLM",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            os.chmod(hw_file, 0o600)

            # 3. Sanity-verify real ffmpeg decodes our 48 kHz stereo WAV to canonical 16 kHz mono WAV
            raw_48k_stereo = _make_noncanonical_48k_stereo_wav()
            gate_check = process_wav(raw_48k_stereo, AudioConfig())
            self.assertTrue(gate_check.passed, f"audio gate rejected 48k stereo sample: {gate_check}")
            with wave.open(io.BytesIO(gate_check.wav_bytes), "rb") as wf:
                self.assertEqual(wf.getnchannels(), 1)
                self.assertEqual(wf.getsampwidth(), 2)
                self.assertEqual(wf.getframerate(), 16000)

            # 4. Write isolated config.toml pointing both stages at local loopback HTTP
            cfg_path = cfg_dir / "config.toml"
            cfg_path.write_text(
                "\n".join(
                    [
                        "[hotkey]",
                        'key = "RControl"',
                        'trigger_mode = "tap_toggle"',
                        "debounce_ms = 0",
                        "",
                        "[audio]",
                        "save_recordings = false",
                        "",
                        "[asr]",
                        'provider = "openai-compatible"',
                        'api = "audio-transcriptions"',
                        f'base_url = "{server.base_url}"',
                        'model = "whisper-loopback"',
                        "max_retries = 0",
                        "",
                        "[refiner]",
                        'provider = "openai-compatible"',
                        'api = "responses"',
                        f'base_url = "{server.base_url}"',
                        'model = "refiner-loopback-sse"',
                        "max_retries = 0",
                        "include_git_status = true",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            os.chmod(cfg_path, 0o600)

            iso_env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(home_dir),
                "XDG_CONFIG_HOME": str(home_dir / ".config"),
                "XDG_CACHE_HOME": str(cache_dir),
                "XDG_RUNTIME_DIR": str(rt_dir),
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt_dir),
            }
            cfg = Config(
                config_path=str(cfg_path),
                hotkey=HotkeyConfig(key="RControl", trigger_mode="tap_toggle", debounce_ms=0),
                audio=AudioConfig(save_recordings=False),
                asr=AsrConfig(
                    provider="openai-compatible",
                    api="audio-transcriptions",
                    base_url=server.base_url,
                    model="whisper-loopback",
                    max_retries=0,
                ),
                refiner=RefinerConfig(
                    provider="openai-compatible",
                    api="responses",
                    base_url=server.base_url,
                    model="refiner-loopback-sse",
                    max_retries=0,
                    include_git_status=True,
                ),
            )
            win_title = f"claude · {repo_dir.name} [#{sid[:8]}]"
            win = ForegroundWindow(
                hwnd=501,
                process="WindowsTerminal.exe",
                title=win_title,
                cwd=real_repo,
            )
            plat = _RecordingDeliveryPlatform(raw_48k_stereo, win)
            tracker = SessionTracker(env=iso_env, runtime_dir=rt_dir, proc_root=empty_proc)

            daemon = VoiceInputDaemon(
                cfg,
                env=iso_env,
                runtime_dir=rt_dir,
                platform=plat,
                tracker=tracker,
                paste_settle_sec=0.0,
            )

            # --- Scenario A: Happy-path full pipeline over real ffmpeg + SessionTracker + loopback HTTP ---
            self.assertTrue(daemon.handle_trigger("ptt_start", win)["ok"])
            self.assertTrue(daemon.handle_trigger("ptt_stop")["ok"])
            st_ok = self._wait_daemon_idle(daemon, min_seq=1)
            lr1 = st_ok["last_result"]
            self.assertTrue(lr1["ok"], lr1)
            self.assertEqual(lr1["delivery_status"], "pasted")
            self.assertGreaterEqual(int(lr1["ctx_chunks"]), 2, f"expected >=2 50K chunks, got {lr1}")
            self.assertEqual(lr1["input_tokens"], 14000)
            self.assertEqual(lr1["cached_input_tokens"], 12000)
            self.assertEqual(plat.deliveries, ["帮我看下 KVCacheScheduler 和 OpenTelemetry 的超时配置"])

            # Verify Stage 1 received ffmpeg-normalized 16kHz mono WAV + scoped [coding]+[global] hotwords
            self.assertEqual(len(server.asr_requests), 1)
            s1_req = server.asr_requests[0]
            with wave.open(io.BytesIO(s1_req["wav_bytes"]), "rb") as wf1:
                self.assertEqual(wf1.getnchannels(), 1)
                self.assertEqual(wf1.getsampwidth(), 2)
                self.assertEqual(wf1.getframerate(), 16000)
            self.assertIn("KVCacheScheduler", s1_req["prompt"])
            self.assertIn("OpenTelemetry", s1_req["prompt"])

            # Verify Stage 2 SSE request received >=2 50K developer chunks + real git status + draft, no Fast flag
            self.assertEqual(len(server.refiner_requests), 1)
            s2_req = server.refiner_requests[0]
            self.assertTrue(s2_req["stream"])
            self.assertFalse(s2_req["store"])
            self.assertEqual(s2_req["tools"], [])
            self.assertNotIn("service_tier", s2_req)
            dev_blocks = [
                m["content"][0]["text"]
                for m in s2_req["input"]
                if m.get("role") == "developer"
            ]
            self.assertGreaterEqual(
                len(dev_blocks),
                3,
                f"expected leading <vocabulary_hints> + >=2 50K developer context blocks, got {len(dev_blocks)}",
            )
            self.assertIn("<vocabulary_hints", dev_blocks[0])
            self.assertIn("KVCacheScheduler", dev_blocks[0])
            self.assertIn("OpenTelemetry", dev_blocks[0])
            self.assertTrue(all("<session_reference_log" in b for b in dev_blocks[1:]))
            user_block = s2_req["input"][-1]["content"][0]["text"]
            self.assertIn("<workspace_git_status", user_block)
            self.assertIn("kv_cache_scheduler.py", user_block)
            self.assertIn("<asr_draft_to_clean>", user_block)

            # --- Scenario B: Stage 2 HTTP 500 + undeliverable paste -> 0600 failed/*.wav -> cli.main(['retry']) ---
            server.fail_stage2 = True
            plat.fail_delivery = True
            self.assertTrue(daemon.handle_trigger("ptt_start", win)["ok"])
            self.assertTrue(daemon.handle_trigger("ptt_stop")["ok"])
            st_fail = self._wait_daemon_idle(daemon, min_seq=2)
            lr2 = st_fail["last_result"]
            self.assertFalse(lr2["ok"], lr2)
            self.assertEqual(lr2["delivery_status"], "undelivered_recoverable")
            self.assertTrue(lr2.get("refiner_fallback"), f"expected Stage 2 fallback recorded, got {lr2}")
            self.assertTrue(lr2.get("saved_audio"), f"expected failed audio stashed, got {lr2}")

            failed_wavs = list_failed_audios(rt_dir)
            self.assertEqual(len(failed_wavs), 1, f"expected 1 failed WAV, got {failed_wavs}")
            self.assertEqual(stat.S_IMODE(failed_wavs[0].stat().st_mode), 0o600)
            self.assertGreater(failed_wavs[0].stat().st_size, 44)

            # Recover Stage 2 & platform delivery, then run real `cli.main(["retry", ...])`
            server.fail_stage2 = False
            plat.fail_delivery = False
            plat_retry = _RecordingDeliveryPlatform(b"", win)

            with (
                mock.patch.dict(os.environ, iso_env, clear=True),
                mock.patch("agent_voice_input.daemon.create_platform", return_value=plat_retry),
            ):
                list_buf = io.StringIO()
                with mock.patch("sys.stdout", list_buf):
                    rc_list = cli_mod.main(["retry", "--list", "--config", str(cfg_path)])
                self.assertEqual(rc_list, 0)
                self.assertIn(failed_wavs[0].name, list_buf.getvalue())

                retry_out = io.StringIO()
                retry_err = io.StringIO()
                with mock.patch("sys.stdout", retry_out), mock.patch("sys.stderr", retry_err):
                    rc_retry = cli_mod.main(
                        ["retry", "--config", str(cfg_path), "--title", win_title]
                    )
                self.assertEqual(rc_retry, 0, f"cli retry failed stderr={retry_err.getvalue()!r}")

            self.assertEqual(plat_retry.deliveries, ["帮我看下 KVCacheScheduler 和 OpenTelemetry 的超时配置"])
            self.assertIn("pasted", retry_err.getvalue())

            # Verify Stage 1 & Stage 2 loopback HTTP both executed on retry and telemetry logged recovery
            self.assertEqual(len(server.asr_requests), 3)
            self.assertEqual(len(server.refiner_requests), 3)
            hist = read_telemetry_history(rt_dir, limit=10)
            self.assertTrue(hist, "telemetry.jsonl missing records")
            last_tel = hist[-1]
            self.assertEqual(last_tel["audio_source"], "retry")
            self.assertTrue(last_tel["ok"])
            self.assertEqual(last_tel["delivery_status"], "pasted")
            self.assertEqual(last_tel["input_tokens"], 14000)
        finally:
            if daemon is not None:
                daemon.close()
            server.close()
            shutil.rmtree(base_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
