"""High-intensity offline concurrency & race-condition torture suite (Zones 1-4).

Forces microsecond CPython thread preemption (``sys.setswitchinterval(1e-6)``) paired with
a stack-dumping deadlock watchdog and ``threading.excepthook`` trap.
"""
from __future__ import annotations

from collections import deque
import contextlib
import faulthandler
import http.client
import http.server
import json
import math
import os
from pathlib import Path
import queue
import random
import socket
import socketserver
import stat
import subprocess
import sys
import tempfile
import _thread
import threading
import time
import traceback
from typing import Any, Callable, Iterator
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_voice_input.asr_providers import BaseAsrProvider  # noqa: E402
from agent_voice_input.audio_proc import (  # noqa: E402
    list_failed_audios,
    pack_pcm16_mono_wav,
)
from agent_voice_input.config import (  # noqa: E402
    AsrConfig,
    AudioConfig,
    Config,
    HotkeyConfig,
    RefinerConfig,
    RemoteConfig,
    RemoteHostConfig,
)
from agent_voice_input.daemon import VoiceInputDaemon, read_telemetry_history  # noqa: E402
from agent_voice_input.http_transport import (  # noqa: E402
    CancelToken,
    HttpRequest,
    HttpTransportError,
    _KeepAlivePool,
    _SplitTimeoutHTTPConnection,
    _register_conn_abort,
    _unregister_conn_abort,
    urllib_http_transport,
)
from agent_voice_input.llm_common import AsrContext, AsrResult  # noqa: E402
from agent_voice_input.platform_common import (  # noqa: E402
    ForegroundWindow,
    JsonlShimMixin,
    PasteOutcome,
    _ShimWriteTimeout,
    _ShimWriter,
    _reap_shim_proc,
)
from agent_voice_input.platform_wsl import WslPlatform  # noqa: E402
from agent_voice_input.refiner import BaseRefiner, RefinerResult  # noqa: E402
from agent_voice_input.session_ctx import (  # noqa: E402
    SessionTracker,
    _iter_trusted_registry_records,
    _read_trusted_registry_json,
    prune_stale_session_registry,
    write_session_registry_entry,
)


def _build_voiced_wav(duration_sec: float = 0.36, freq_hz: float = 240.0) -> bytes:
    """Create canonical 16 kHz mono s16le speech-level WAV that passes ``process_wav`` in <0.2ms."""
    sample_rate = 16000
    n_samples = int(sample_rate * duration_sec)
    samples = [
        int(11000 * math.sin(2.0 * math.pi * freq_hz * i / sample_rate))
        for i in range(n_samples)
    ]
    return pack_pcm16_mono_wav(samples, sample_rate=sample_rate)


def _dump_all_thread_stacks() -> str:
    frames = sys._current_frames()
    by_ident = {th.ident: th for th in threading.enumerate()}
    blocks: list[str] = []
    for tid, frame in frames.items():
        th = by_ident.get(tid)
        tname = th.name if th is not None else f"tid-{tid}"
        daemon_flag = th.daemon if th is not None else "?"
        stack_txt = "".join(traceback.format_stack(frame))
        blocks.append(f"--- Thread {tname!r} (ident={tid}, daemon={daemon_flag}) ---\n{stack_txt}")
    return "\n".join(blocks)


@contextlib.contextmanager
def preemption_watchdog(
    *,
    timeout_sec: float = 15.0,
    switch_interval: float = 1e-6,
    hard_exit_grace_sec: float = 3.0,
) -> Iterator[list[threading.ExceptHookArgs]]:
    """Force 1us bytecode GIL switching, trap background thread exceptions, and guard against deadlocks.

    On timeout: immediately flushes full C/Python stack traces of all threads to ``sys.__stderr__``,
    signals the main thread via ``_thread.interrupt_main()``, and if the main thread remains wedged
    in an uninterruptible C-level lock past ``hard_exit_grace_sec``, terminates via ``os._exit(124)``
    so a true C-level deadlock can never hang a CI runner indefinitely.
    """
    orig_interval = sys.getswitchinterval()
    orig_excepthook = threading.excepthook
    done_evt = threading.Event()
    watchdog_dump: list[str] = []
    uncaught_thread_errors: list[threading.ExceptHookArgs] = []

    def _excepthook(args: threading.ExceptHookArgs) -> None:
        uncaught_thread_errors.append(args)

    def _watchdog() -> None:
        if not done_evt.wait(timeout_sec):
            msg = f"\n[DEADLOCK WATCHDOG] Expired after {timeout_sec:.2f}s!\n{_dump_all_thread_stacks()}\n"
            watchdog_dump.append(msg)
            err_stream = sys.__stderr__ or sys.stderr
            try:
                err_stream.write(msg)
                err_stream.flush()
                faulthandler.dump_traceback(file=err_stream, all_threads=True)
                err_stream.flush()
            except Exception:
                pass
            _thread.interrupt_main()
            if hard_exit_grace_sec > 0.0 and not done_evt.wait(hard_exit_grace_sec):
                os._exit(124)

    wd_thread = threading.Thread(target=_watchdog, name="test-deadlock-watchdog", daemon=True)
    wd_thread.start()
    threading.excepthook = _excepthook
    sys.setswitchinterval(switch_interval)
    try:
        try:
            yield uncaught_thread_errors
        except KeyboardInterrupt as exc:
            if watchdog_dump:
                raise AssertionError(watchdog_dump[0]) from exc
            raise
    finally:
        done_evt.set()
        sys.setswitchinterval(orig_interval)
        threading.excepthook = orig_excepthook

    if watchdog_dump:
        raise AssertionError(watchdog_dump[0])
    if uncaught_thread_errors:
        formatted = "\n".join(
            "".join(
                traceback.format_exception(
                    err.exc_type, err.exc_value, err.exc_traceback
                )
            )
            for err in uncaught_thread_errors
        )
        raise AssertionError(
            f"Uncaught background thread exception(s) ({len(uncaught_thread_errors)}):\n{formatted}"
        )


# ---------------------------------------------------------------------------
# Zone 1 Fakes: Thread-safe Platform, Fast ASR, and Out-of-Order Stage 2 Refiner
# ---------------------------------------------------------------------------


class _TorturePlatform:
    def __init__(self, speech_wav: bytes) -> None:
        self._speech_wav = speech_wav
        self._lock = threading.Lock()
        self._rec_active = False
        self._next_empty = False
        self._lost_cb: Callable[..., Any] | None = None
        self._recovered_cb: Callable[..., Any] | None = None
        self.last_audio_error = ""
        self.delivered: list[tuple[int, str]] = []
        self.hud_events: deque[tuple[str, dict[str, Any]]] = deque(maxlen=500)
        self.closed = False

    def set_recorder_lost_callback(self, cb: Callable[..., Any] | None) -> None:
        self._lost_cb = cb

    def set_recorder_recovered_callback(self, cb: Callable[..., Any] | None) -> None:
        self._recovered_cb = cb

    def start_event_listener(self, on_hotkey_event: Callable[[dict[str, Any]], None]) -> None:
        self._on_event = on_hotkey_event

    def is_allowed_terminal(self, window: ForegroundWindow) -> bool:
        return True

    def get_foreground_window(self) -> ForegroundWindow:
        return ForegroundWindow(hwnd=101, process="WindowsTerminal.exe", title="codex")

    def resolve_foreground_window(self, hint: ForegroundWindow | None = None) -> ForegroundWindow:
        return hint if (hint and hint.process) else self.get_foreground_window()

    def set_next_stop_empty(self, empty: bool = True) -> None:
        with self._lock:
            self._next_empty = empty

    def start_recording(
        self, runtime_dir: Path, *, loopback: bool = False, seq_id: int | None = None
    ) -> None:
        del runtime_dir, loopback, seq_id
        with self._lock:
            self._rec_active = True
            self.last_audio_error = ""

    def stop_recording(self, *, drain: bool = True) -> tuple[bytes, str]:
        del drain
        with self._lock:
            self._rec_active = False
            use_empty = self._next_empty
            self._next_empty = False
            if use_empty:
                self.last_audio_error = "simulated_drop_empty"
                return b"", "mock_mic"
            return self._speech_wav, "mock_mic"

    def cancel_recording(self) -> None:
        with self._lock:
            self._rec_active = False
            self._next_empty = False

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
        self.hud_events.append(
            (
                state,
                {
                    "text": text,
                    "duration_ms": duration_ms,
                    "layer": layer,
                    "clear_main": clear_main,
                    "clear_toast": clear_toast,
                    "bg_count": bg_count,
                },
            )
        )

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
    ) -> PasteOutcome:
        del target_hwnd, origin_window, fallback_clipboard_text, suppress_beep
        if is_cancelled is not None and is_cancelled():
            return PasteOutcome(False, False, "cancelled", "")
        if before_clipboard_write is not None:
            before_clipboard_write()
        if is_cancelled is not None and is_cancelled():
            return PasteOutcome(False, False, "cancelled", "")
        if before_commit is not None and not before_commit():
            return PasteOutcome(False, False, "cancelled", "")
        with self._lock:
            self.delivered.append((seq_id, text))
        return PasteOutcome(True, True, "pasted", "WindowsTerminal.exe", used_clipboard_paste=True)

    def close(self) -> None:
        self.closed = True


class _TaggingAsrProvider(BaseAsrProvider):
    provider_name = "dashscope"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counter = 0

    def transcribe(
        self,
        wav_bytes: bytes,
        context: AsrContext | None = None,
        *,
        is_cancelled: Callable[[], bool] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> AsrResult:
        del wav_bytes, context
        ev = threading.Event()
        if cancel_token is not None:
            cancel_token.bind_event(ev)
        try:
            ev.wait(timeout=0.002)
            if (cancel_token is not None and cancel_token.is_cancelled()) or (
                is_cancelled is not None and is_cancelled()
            ):
                raise HttpTransportError("ASR cancelled", retry_kind="never")
            with self._lock:
                self._counter += 1
                idx = self._counter
            return AsrResult(text=f"draft-{idx}", provider="dashscope", model="qwen-asr", latency_ms=2)
        finally:
            if cancel_token is not None:
                cancel_token.unbind_event(ev)


class _OutOfOrderRefiner(BaseRefiner):
    """Odd utterances take 45ms while even utterances take 2ms, forcing Stage 2 out-of-order completion."""

    provider_name = "deepseek"

    def __init__(self, *, slow_delay_sec: float = 0.045) -> None:
        self._lock = threading.Lock()
        self._call_idx = 0
        self._slow_delay_sec = slow_delay_sec
        self.max_concurrent_seen = 0
        self._in_flight = 0
        self.completed_call_indices: list[int] = []
        self.first_entered_evt = threading.Event()
        self._hold_next = False
        self.held_entered_evt = threading.Event()
        self.held_release_evt = threading.Event()
        self.closed = False

    def arm_hold_next(self) -> None:
        with self._lock:
            self.held_entered_evt.clear()
            self.held_release_evt.clear()
            self._hold_next = True

    def refine(
        self,
        asr: AsrResult,
        *,
        session_key: str = "",
        tracker: Any = None,
        fallback_context: str = "",
        is_cancelled: Callable[[], bool] | None = None,
        cancel_token: CancelToken | None = None,
        mode: str = "agent",
        workspace_cwd: str = "",
        target_hint: str = "",
    ) -> RefinerResult:
        del session_key, tracker, fallback_context, mode, workspace_cwd, target_hint
        ev = threading.Event()
        if cancel_token is not None:
            cancel_token.bind_event(ev)
        with self._lock:
            self._call_idx += 1
            my_idx = self._call_idx
            should_hold = self._hold_next
            self._hold_next = False
            self._in_flight += 1
            if self._in_flight > self.max_concurrent_seen:
                self.max_concurrent_seen = self._in_flight
        self.first_entered_evt.set()
        is_odd = (my_idx % 2 == 1)
        delay = self._slow_delay_sec if is_odd else 0.002
        try:
            if should_hold:
                self.held_entered_evt.set()
                # Deterministic latch: remains in-flight until cancel_token fires or held_release_evt is set
                deadline_h = time.monotonic() + 3.0
                while time.monotonic() < deadline_h:
                    if ev.is_set() or self.held_release_evt.is_set():
                        break
                    ev.wait(timeout=0.005)
            else:
                ev.wait(timeout=delay)
            if (cancel_token is not None and cancel_token.is_cancelled()) or (
                is_cancelled is not None and is_cancelled()
            ):
                return RefinerResult(asr.text, "deepseek", "deepseek-flash", 1, "cancelled")
            with self._lock:
                self.completed_call_indices.append(my_idx)
            return RefinerResult(
                f"refined[{asr.text}]",
                "deepseek",
                "deepseek-flash",
                int(delay * 1000),
                "",
            )
        finally:
            with self._lock:
                self._in_flight -= 1
            if cancel_token is not None:
                cancel_token.unbind_event(ev)

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Zone 2 Local Loopback HTTP Server (Keep-Alive + Slow Stall Endpoint)
# ---------------------------------------------------------------------------


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _KeepAliveAndStallHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        del format, args

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/slow"):
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", "64")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                self.wfile.write(b"partial-")
                self.wfile.flush()
                self.connection.settimeout(0.35)
                try:
                    while self.connection.recv(16):
                        pass
                except OSError:
                    pass
            except OSError:
                pass
            return

        payload = b'{"ok":true}'
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            if self.path.startswith("/close"):
                self.send_header("Connection", "close")
            else:
                self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
        except OSError:
            pass

    do_POST = do_GET


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------


class TestConcurrencyRaces(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.speech_wav = _build_voiced_wav(0.36)

    def test_preemption_context_restores_interval_and_watchdog_dumps_stuck_child(self) -> None:
        before = sys.getswitchinterval()
        with preemption_watchdog(timeout_sec=5.0, switch_interval=1e-6):
            self.assertAlmostEqual(sys.getswitchinterval(), 1e-6, places=9)
        self.assertEqual(sys.getswitchinterval(), before)

        # Verify watchdog dumps thread stacks to stderr and terminates even a C-level Lock.acquire() deadlock
        stuck_code = (
            "import sys, threading\n"
            f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
            f"sys.path.insert(0, {str(SRC_DIR)!r})\n"
            "from tests.test_concurrency_races import preemption_watchdog\n"
            "lk = threading.Lock(); lk.acquire()\n"
            "def _wedged_lock_caller():\n"
            "    lk.acquire()\n"
            "with preemption_watchdog(timeout_sec=0.05, hard_exit_grace_sec=0.08):\n"
            "    _wedged_lock_caller()\n"
        )
        p = subprocess.run(
            [sys.executable, "-c", stuck_code],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        self.assertEqual(p.returncode, 124)
        self.assertIn("[DEADLOCK WATCHDOG]", p.stderr)
        self.assertIn("_wedged_lock_caller", p.stderr)

    def test_zone1_daemon_hotkey_stage2_out_of_order_cancel_and_mic_drop_storm(self) -> None:
        """Zone 1: 30 rapid start/stop rounds, 45ms/2ms Stage 2 inversion, cancel, and mic drops."""
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            empty_proc = Path(td) / "empty_proc"
            empty_proc.mkdir(parents=True, exist_ok=True)
            cfg = Config(
                asr=AsrConfig(provider="dashscope"),
                refiner=RefinerConfig(provider="deepseek", include_git_status=False),
                audio=AudioConfig(beep=False, save_recordings=False, tail_drain_sec=0.0),
                hotkey=HotkeyConfig(debounce_ms=0, scope="all"),
            )
            plat = _TorturePlatform(self.speech_wav)
            asr = _TaggingAsrProvider()
            refiner = _OutOfOrderRefiner(slow_delay_sec=0.045)
            tracker = SessionTracker(
                env={"HOME": td},
                runtime_dir=rt,
                proc_root=empty_proc,
            )
            daemon = VoiceInputDaemon(
                cfg,
                env={"HOME": td, "DEEPSEEK_API_KEY": "sk-test-d", "DASHSCOPE_API_KEY": "sk-test-q"},
                runtime_dir=rt,
                platform=plat,
                asr_provider=asr,
                refiner=refiner,
                tracker=tracker,
                paste_settle_sec=0.0,
            )

            confirmed_cancelled_pipeline_seqs: set[int] = set()
            confirmed_cancelled_all_seqs: set[int] = set()
            only_inflight_cancelled_ids: list[int] = []
            stop_status_poller = threading.Event()
            win = ForegroundWindow(hwnd=101, process="WindowsTerminal.exe", title="codex")

            def _status_poller() -> None:
                while not stop_status_poller.is_set():
                    st = daemon.handle_control({"cmd": "status", "history": 25})
                    assert st["ok"] is True
                    time.sleep(0.004)

            try:
                with preemption_watchdog(timeout_sec=15.0, switch_interval=1e-6):
                    poller_th = threading.Thread(target=_status_poller, name="status-poller", daemon=True)
                    poller_th.start()
                    try:
                        total_rounds = 30
                        for i in range(1, total_rounds + 1):
                            deadline_q = time.monotonic() + 3.0
                            while True:
                                r_start = daemon.handle_trigger("ptt_start", win)
                                if r_start.get("ignored") != "queue_full" or time.monotonic() >= deadline_q:
                                    break
                                time.sleep(0.008)
                            self.assertTrue(r_start.get("ok"), f"start failed on round {i}: {r_start}")
                            cur_seq = int(r_start["seq_id"])

                            if i in (5, 19):
                                daemon._on_recorder_recovered(320, seq_id=cur_seq)
                            if i == 8:
                                daemon._on_recorder_lost(seq_id=cur_seq)
                                r_abs = daemon.handle_trigger("toggle", win)
                                self.assertTrue(r_abs.get("ok"))
                                self.assertEqual(r_abs.get("ignored"), "already_stopped")
                                continue
                            if i == 16:
                                plat.set_next_stop_empty(True)
                                daemon._on_recorder_lost(seq_id=cur_seq)
                                continue
                            if i == 22:
                                r_crec = daemon.handle_cancel(seq_id=cur_seq)
                                if r_crec.get("ok"):
                                    confirmed_cancelled_all_seqs.update(r_crec.get("cancelled_ids") or [])
                                continue

                            if i == 15:
                                # Drain prior utterances first so cur_seq enters Stage 2 under deterministic hold latch
                                wait_idle0 = time.monotonic() + 3.0
                                while time.monotonic() < wait_idle0:
                                    if daemon.handle_control({"cmd": "status"})["pending_count"] == 0:
                                        break
                                    time.sleep(0.005)
                                self.assertEqual(daemon.handle_control({"cmd": "status"})["pending_count"], 0)
                                refiner.arm_hold_next()

                            r_stop = daemon.handle_trigger("ptt_stop", win)
                            self.assertTrue(r_stop.get("ok"), f"stop failed on round {i}: {r_stop}")

                            if i in (3, 11, 20, 26):
                                r_cseq = daemon.handle_cancel(seq_id=cur_seq)
                                if r_cseq.get("ok"):
                                    cids = [int(x) for x in (r_cseq.get("cancelled_ids") or [])]
                                    confirmed_cancelled_pipeline_seqs.update(cids)
                                    confirmed_cancelled_all_seqs.update(cids)
                            elif i == 15:
                                try:
                                    self.assertTrue(refiner.held_entered_evt.wait(timeout=2.0))
                                    r_cinf = daemon.handle_cancel(only_inflight=True)
                                    self.assertTrue(r_cinf.get("ok"), f"only_inflight cancel missed: {r_cinf}")
                                    cids = [int(x) for x in (r_cinf.get("cancelled_ids") or [])]
                                    self.assertIn(cur_seq, cids)
                                    only_inflight_cancelled_ids.extend(cids)
                                    confirmed_cancelled_pipeline_seqs.update(cids)
                                    confirmed_cancelled_all_seqs.update(cids)
                                finally:
                                    refiner.held_release_evt.set()
                            elif i % 2 == 0:
                                # Allow odd (45ms) + even (2ms) Stage 2 slots to overlap and invert completion order
                                time.sleep(0.048)

                        drain_deadline = time.monotonic() + 5.0
                        while time.monotonic() < drain_deadline:
                            snap = daemon.handle_control({"cmd": "status"})
                            if snap["state"] == "idle" and snap["pending_count"] == 0:
                                break
                            time.sleep(0.01)
                        self.assertEqual(daemon.state, "idle")
                    finally:
                        stop_status_poller.set()
                        poller_th.join(timeout=1.0)
                        w_th = daemon._worker_thread
                        c_th = daemon._commit_thread
                        s2_ths = list(daemon._stage2_threads)
                        daemon.close()

                    for th in [w_th, c_th, *s2_ths]:
                        if th is not None:
                            self.assertFalse(th.is_alive(), f"Worker thread {th.name} still alive after close()")

                delivered_seqs = [seq for seq, _ in plat.delivered]
                self.assertGreaterEqual(len(delivered_seqs), 15)
                self.assertGreaterEqual(refiner.max_concurrent_seen, 2)
                # Prove at least one faster later Stage 2 call completed BEFORE an earlier slow call
                self.assertNotEqual(
                    refiner.completed_call_indices,
                    sorted(refiner.completed_call_indices),
                    f"Expected Stage 2 out-of-order completion, got {refiner.completed_call_indices}",
                )
                # (1) Delivered seq_ids remain strictly monotonic & duplicate-free despite Stage 2 inversion
                self.assertEqual(delivered_seqs, sorted(set(delivered_seqs)))
                for idx in range(1, len(delivered_seqs)):
                    self.assertGreater(delivered_seqs[idx], delivered_seqs[idx - 1])

                # (2) Both only_inflight=True and --seq cancellations succeeded and never reached screen
                self.assertGreaterEqual(len(only_inflight_cancelled_ids), 1)
                self.assertGreaterEqual(len(confirmed_cancelled_pipeline_seqs), 4)
                self.assertTrue(confirmed_cancelled_all_seqs.isdisjoint(set(delivered_seqs)))

                history = read_telemetry_history(rt, limit=100)
                by_seq = {int(rec["seq_id"]): rec for rec in history if "seq_id" in rec}
                stashed_files = {str(p): p for p in list_failed_audios(rt)}
                self.assertLessEqual(len(confirmed_cancelled_pipeline_seqs), 10)
                for cseq in sorted(confirmed_cancelled_pipeline_seqs):
                    self.assertIn(cseq, by_seq, f"Cancelled seq {cseq} missing from telemetry")
                    rec = by_seq[cseq]
                    self.assertFalse(rec.get("ok", True))
                    self.assertEqual(rec.get("skipped_reason"), "cancelled")
                    saved_path_str = str(rec.get("saved_audio") or "")
                    self.assertTrue(saved_path_str, f"Cancelled seq {cseq} did not stash failed WAV")
                    self.assertIn(saved_path_str, stashed_files)
                    st_mode = stat.S_IMODE(Path(saved_path_str).stat().st_mode)
                    self.assertEqual(st_mode, 0o600)

                # Sub-check: mid-flight daemon.close() while Stage 2 is verified active aborts all threads
                plat2 = _TorturePlatform(self.speech_wav)
                refiner2 = _OutOfOrderRefiner(slow_delay_sec=0.30)
                daemon2 = VoiceInputDaemon(
                    cfg,
                    env={"HOME": td, "DEEPSEEK_API_KEY": "sk-test-d", "DASHSCOPE_API_KEY": "sk-test-q"},
                    runtime_dir=Path(td) / "rt2",
                    platform=plat2,
                    asr_provider=_TaggingAsrProvider(),
                    refiner=refiner2,
                    tracker=tracker,
                    paste_settle_sec=0.0,
                )
                try:
                    with preemption_watchdog(timeout_sec=5.0, switch_interval=1e-6):
                        for _ in range(3):
                            daemon2.handle_trigger("ptt_start", win)
                            daemon2.handle_trigger("ptt_stop", win)
                        self.assertTrue(refiner2.first_entered_evt.wait(timeout=2.0))
                        with daemon2._lock:
                            w2 = daemon2._worker_thread
                            c2 = daemon2._commit_thread
                            s2_ths2 = list(daemon2._stage2_threads)
                        self.assertGreaterEqual(len(s2_ths2), 1)
                        daemon2.close()
                        for th in [w2, c2, *s2_ths2]:
                            if th is not None:
                                self.assertFalse(th.is_alive(), f"Thread {th.name} leaked after close()")
                finally:
                    daemon2.close()
            finally:
                stop_status_poller.set()
                daemon.close()
                tracker.close()

    def test_zone2_keepalive_pool_and_cancel_token_concurrent_torture(self) -> None:
        """Zone 2: Loopback + socketpair KeepAlivePool borrow/return/evict vs CancelToken shutdown."""
        srv = _ThreadedHTTPServer(("127.0.0.1", 0), _KeepAliveAndStallHandler)
        port = int(srv.server_address[1])
        srv_th = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        srv_th.start()

        pool = _KeepAlivePool(max_per_origin=2, idle_timeout_sec=0.05)
        active_tokens_lock = threading.Lock()
        active_tokens: list[CancelToken] = []
        stop_bg = threading.Event()
        stats_lock = threading.Lock()
        ok_count = 0
        reused_count = 0
        cancelled_count = 0
        workers: list[threading.Thread] = []
        canceller_threads: list[threading.Thread] = []

        def _http_worker(worker_id: int) -> None:
            nonlocal ok_count, reused_count, cancelled_count
            rng = random.Random(1000 + worker_id)
            for step in range(24):
                tok = CancelToken()
                use_slow = (step % 4 == 0)
                if use_slow or step % 5 == 1:
                    with active_tokens_lock:
                        active_tokens.append(tok)
                path = "/slow" if use_slow else ("/close" if step % 9 == 0 else "/fast")
                if step % 2 == 0:
                    req = HttpRequest(
                        url=f"http://127.0.0.1:{port}{path}",
                        method="GET",
                        headers={},
                        body=b"",
                        timeout_sec=0.5,
                        connect_timeout_sec=0.5,
                        cancel_token=tok,
                    )
                    try:
                        resp = urllib_http_transport(req)
                        if resp.status == 200:
                            with stats_lock:
                                ok_count += 1
                    except (HttpTransportError, OSError, http.client.HTTPException, TimeoutError):
                        if tok.is_cancelled():
                            with stats_lock:
                                cancelled_count += 1
                else:
                    conn, reused = pool.acquire(
                        "http",
                        "127.0.0.1",
                        port,
                        0.5,
                        allow_reuse=rng.choice((True, True, True, False)),
                        connect_timeout_sec=0.5,
                    )
                    if reused:
                        with stats_lock:
                            reused_count += 1
                    hid = _register_conn_abort(tok, conn)
                    try:
                        if not reused:
                            conn.connect()
                        conn.request("GET", path, headers={"Connection": "keep-alive"})
                        resp_raw = conn.getresponse()
                        _ = resp_raw.read()
                        resp_raw.close()
                        _unregister_conn_abort(tok, hid)
                        hid = 0
                        if tok.is_cancelled() or getattr(conn, "_aborted", False) or resp_raw.will_close:
                            conn.close()
                            if tok.is_cancelled():
                                with stats_lock:
                                    cancelled_count += 1
                        else:
                            pool.release("http", "127.0.0.1", port, conn)
                            with stats_lock:
                                ok_count += 1
                    except (OSError, http.client.HTTPException, TimeoutError):
                        conn.close()
                        if tok.is_cancelled():
                            with stats_lock:
                                cancelled_count += 1
                    finally:
                        if hid:
                            _unregister_conn_abort(tok, hid)

        def _canceller_and_flusher() -> None:
            idx = 0
            while not stop_bg.is_set():
                batch: list[CancelToken] = []
                with active_tokens_lock:
                    if active_tokens:
                        batch = active_tokens[-3:]
                        del active_tokens[:-6]
                if batch:
                    if idx % 2 == 0:
                        CancelToken.cancel_many(batch[:2], "torture_cancel")
                    else:
                        for t in batch[:2]:
                            t.cancel("torture_single")
                if idx % 8 == 7:
                    pool.close_all()
                idx += 1
                time.sleep(0.004)

        try:
            with mock.patch(
                "agent_voice_input.http_transport._should_use_system_proxy", return_value=False
            ), preemption_watchdog(timeout_sec=15.0, switch_interval=1e-6):
                # 1. Deterministic regression test for deferred CancelToken callback vs pool reuse (_borrow_gen)
                gate_entered = threading.Event()
                gate_release = threading.Event()
                stale_tok = CancelToken()
                stale_tok.register_abort(lambda: (gate_entered.set(), gate_release.wait(timeout=2.0)))
                c_orig, _ = pool.acquire("http", "127.0.0.1", port, 0.5, allow_reuse=False)
                c_orig.connect()
                hid_orig = _register_conn_abort(stale_tok, c_orig)
                c_orig.request("GET", "/fast", headers={"Connection": "keep-alive"})
                r_orig = c_orig.getresponse()
                self.assertEqual(r_orig.read(), b'{"ok":true}')
                r_orig.close()

                cancel_th = threading.Thread(target=stale_tok.cancel, args=("late_stale",), daemon=True)
                cancel_th.start()
                self.assertTrue(gate_entered.wait(timeout=2.0))
                # stale_tok already popped c_orig's abort callback; now c_orig is released & re-borrowed!
                _unregister_conn_abort(stale_tok, hid_orig)
                pool.release("http", "127.0.0.1", port, c_orig)
                c_reused, was_reused = pool.acquire("http", "127.0.0.1", port, 0.5, allow_reuse=True)
                self.assertTrue(was_reused)
                gate_release.set()
                cancel_th.join(timeout=2.0)
                self.assertFalse(getattr(c_reused, "_aborted", False))
                c_reused.request("GET", "/fast", headers={"Connection": "keep-alive"})
                r_reused = c_reused.getresponse()
                self.assertEqual(r_reused.read(), b'{"ok":true}')
                r_reused.close()
                pool.release("http", "127.0.0.1", port, c_reused)

                # 2. Multi-thread HTTP Keep-Alive borrow/return/eviction vs CancelToken storm
                workers = [
                    threading.Thread(target=_http_worker, args=(w,), name=f"ka-worker-{w}", daemon=True)
                    for w in range(6)
                ]
                canceller_threads = [
                    threading.Thread(target=_canceller_and_flusher, name=f"ka-canceller-{c}", daemon=True)
                    for c in range(2)
                ]
                for th in [*workers, *canceller_threads]:
                    th.start()
                for th in workers:
                    th.join(timeout=10.0)
                    self.assertFalse(th.is_alive())
                stop_bg.set()
                for th in canceller_threads:
                    th.join(timeout=2.0)
                    self.assertFalse(th.is_alive())

                self.assertGreaterEqual(ok_count, 25)
                self.assertGreaterEqual(reused_count, 3)
                self.assertGreaterEqual(cancelled_count, 5)

                # 3. Real socketpair HTTP request/read/shutdown wakeup + try_commit() vs cancel() mutex
                commit_vs_cancel_violations = 0
                ok_pair_reads = 0
                aborted_pair_wakeups = 0
                pair_lock = threading.Lock()

                def _socketpair_hammer(tid: int) -> None:
                    nonlocal commit_vs_cancel_violations, ok_pair_reads, aborted_pair_wakeups
                    for k in range(36):
                        s_cli, s_srv = socket.socketpair()
                        tok = CancelToken()
                        conn = _SplitTimeoutHTTPConnection(
                            "127.0.0.1", port, timeout=2.0, connect_timeout=0.5, cancel_token=tok
                        )
                        try:
                            s_cli.settimeout(2.0)
                            conn.sock = s_cli
                            with conn._abort_lock:
                                conn._cur_sock = s_cli
                            # Transition HTTPConnection into valid _CS_REQ_SENT state
                            conn.request("GET", "/pair")
                            _ = s_srv.recv(4096)

                            if (tid + k) % 2 == 0:
                                # Server writes valid response; race read() vs pool release
                                s_srv.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
                                resp = conn.getresponse()
                                body = resp.read()
                                resp.close()
                                if body == b"OK":
                                    with pair_lock:
                                        ok_pair_reads += 1
                            else:
                                # Server sends headers + 2/10 body bytes then stalls; reader hooks raw SocketIO.readinto
                                # so entered_body_read fires strictly inside the blocking remainder read path!
                                s_srv.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n12")
                                r_stalled = conn.getresponse()
                                head2 = r_stalled.read(2)
                                self.assertEqual(head2, b"12")
                                entered_body_read = threading.Event()
                                raw_sio = r_stalled.fp.raw  # type: ignore[attr-defined]
                                orig_readinto = raw_sio.readinto

                                def _hooked_readinto(buf: Any) -> int | None:
                                    entered_body_read.set()
                                    return orig_readinto(buf)

                                raw_sio.readinto = _hooked_readinto

                                def _delayed_cancel() -> None:
                                    if entered_body_read.wait(timeout=1.5):
                                        tok.cancel("pair_shutdown_wakeup")

                                c_th = threading.Thread(target=_delayed_cancel, daemon=True)
                                c_th.start()
                                t_w0 = time.monotonic()
                                try:
                                    _ = r_stalled.read()
                                except http.client.IncompleteRead:
                                    if tok.is_cancelled() and (time.monotonic() - t_w0) < 1.5:
                                        with pair_lock:
                                            aborted_pair_wakeups += 1
                                finally:
                                    r_stalled.close()
                                    c_th.join(timeout=1.0)
                        finally:
                            conn.close()
                            s_cli.close()
                            s_srv.close()

                        # Verify strict mutual exclusion between try_commit() and cancel()
                        race_tok = CancelToken()
                        res_box = [False, False]

                        def _do_commit() -> None:
                            res_box[0] = race_tok.try_commit()

                        def _do_cancel() -> None:
                            res_box[1] = race_tok.cancel("race")

                        t1 = threading.Thread(target=_do_commit, daemon=True)
                        t2 = threading.Thread(target=_do_cancel, daemon=True)
                        t1.start()
                        t2.start()
                        t1.join(timeout=1.0)
                        t2.join(timeout=1.0)
                        if res_box[0] and res_box[1]:
                            with pair_lock:
                                commit_vs_cancel_violations += 1

                sp_threads = [
                    threading.Thread(target=_socketpair_hammer, args=(i,), name=f"sp-hammer-{i}", daemon=True)
                    for i in range(4)
                ]
                for th in sp_threads:
                    th.start()
                for th in sp_threads:
                    th.join(timeout=8.0)
                    self.assertFalse(th.is_alive())

                self.assertEqual(commit_vs_cancel_violations, 0)
                self.assertGreaterEqual(ok_pair_reads, 20)
                self.assertGreaterEqual(aborted_pair_wakeups, 20)
        finally:
            stop_bg.set()
            for th in [*workers, *canceller_threads]:
                if th.is_alive():
                    th.join(timeout=2.0)
            pool.close_all()
            from agent_voice_input.http_transport import _KEEPALIVE_POOL

            _KEEPALIVE_POOL.close_all()
            srv.shutdown()
            srv.server_close()
            srv_th.join(timeout=2.0)

    def test_zone3_session_tracker_remote_ssh_and_registry_flock_torture(self) -> None:
        """Zone 3: Remote prefetch/cancel/ETag/half-open breaker + cross-process/thread flock registry."""
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            empty_proc = Path(td) / "empty_proc"
            empty_proc.mkdir(parents=True, exist_ok=True)
            clock_lock = threading.Lock()
            fake_now = [1000.0]

            def _clock() -> float:
                with clock_lock:
                    return fake_now[0]

            def _advance_clock(sec: float) -> None:
                with clock_lock:
                    fake_now[0] += sec

            rem_cfg = RemoteConfig(
                timeout_ms=250,
                hosts=(RemoteHostConfig(name="devbox", ssh_target="dev@127.0.0.1"),),
            )
            tracker = SessionTracker(
                env={"HOME": td},
                runtime_dir=rt,
                proc_root=empty_proc,
                remote_cfg=rem_cfg,
                context_max_chars=80000,
            )
            rm = tracker.remote_manager
            rm.clock_fn = _clock

            probe_seq = [0]
            force_fail = [False]
            etag_304_count = [0]
            valid_snaps = [0]
            half_open_claimed_count = [0]
            probe_lock = threading.Lock()
            sid_hex = "a1b2c3d4-1111-2222-3333-444455556666"

            orig_can_attempt = rm._can_attempt_locked

            def _wrapped_can_attempt(host_name: str, *, allow_half_open: bool) -> tuple[bool, bool]:
                can_r, claimed_ho = orig_can_attempt(host_name, allow_half_open=allow_half_open)
                if claimed_ho:
                    with probe_lock:
                        half_open_claimed_count[0] += 1
                return can_r, claimed_ho

            rm._can_attempt_locked = _wrapped_can_attempt  # type: ignore[method-assign]

            def _fake_ssh_probe(
                host_cfg: RemoteHostConfig,
                req_dict: Any,
                *,
                abort_evt: threading.Event | None = None,
                timeout_sec: float | None = None,
            ) -> tuple[dict[str, Any] | None, str, bool, bool]:
                del host_cfg, timeout_sec
                if abort_evt is not None and abort_evt.wait(timeout=0.003):
                    return None, "aborted", False, True
                with probe_lock:
                    probe_seq[0] += 1
                    n = probe_seq[0]
                    do_fail = force_fail[0] or (n % 9 in (5, 6))
                if abort_evt is not None and abort_evt.is_set():
                    return None, "aborted", False, True
                if do_fail:
                    return None, "ssh connect timeout", False, False
                sent_etag = str(req_dict.get("etag") or "")
                cur_etag = f"etag-v{max(1, n // 4)}"
                if sent_etag and sent_etag == cur_etag:
                    with probe_lock:
                        etag_304_count[0] += 1
                    return (
                        {
                            "ok": True,
                            "not_modified": True,
                            "unchanged": True,
                            "etag": cur_etag,
                            "cli": "codex",
                            "session_id": sid_hex,
                            "matched_by": "tag_id",
                            "cwd": "/srv/repo",
                            "git_status": f"M file_{n % 5}.py",
                            "git_hotwords": [f"file_{n % 5}.py"],
                            "repo_files": ["core_engine.py"],
                            "shallow_files": [],
                            "files_included": True,
                            "turns": [],
                            "evicted_chunks": 0,
                            "compaction_count": 0,
                        },
                        "",
                        False,
                        False,
                    )
                turns = [
                    ("user", f"check round {k} in core_engine.py")
                    if k % 2 == 0
                    else ("assistant", f"verified round {k}")
                    for k in range(1, (n % 8) + 3)
                ]
                return (
                    {
                        "ok": True,
                        "not_modified": False,
                        "unchanged": False,
                        "etag": cur_etag,
                        "cli": "codex",
                        "session_id": sid_hex,
                        "matched_by": "tag_id",
                        "cwd": "/srv/repo",
                        "git_status": "M core_engine.py",
                        "git_hotwords": ["core_engine.py"],
                        "repo_files": ["core_engine.py", "remote_ssh.py"],
                        "shallow_files": [],
                        "files_included": True,
                        "turns": turns,
                        "evicted_chunks": 0,
                        "compaction_count": 0,
                    },
                    "",
                    False,
                    False,
                )

            rm._run_ssh_probe_once = _fake_ssh_probe  # type: ignore[method-assign]
            skey = f"remote:devbox:codex:{sid_hex}"
            title = "codex · ~/repo [#a1b2c3d4] [@devbox]"
            corrupt_reads: list[str] = []

            def _prefetch_and_sync_worker(wid: int) -> None:
                for step in range(18):
                    seq_val = wid * 100 + step + 1
                    tracker.prefetch_remote_session(
                        title, window_process="WindowsTerminal.exe", seq_id=seq_val
                    )
                    if step % 3 == 0:
                        tracker.cancel_remote_prefetch(seq_id=seq_val)
                    elif step % 4 == 0:
                        _advance_clock(65.0)
                    snap = tracker.sync_session(
                        window_title=title, window_process="WindowsTerminal.exe"
                    )
                    if snap is not None:
                        with probe_lock:
                            valid_snaps[0] += 1
                        for role, txt in snap.clean_turns:
                            if role not in ("user", "assistant") or not txt:
                                corrupt_reads.append(f"bad turn: {(role, txt)!r}")
                    chunks, git_txt = tracker.get_refiner_context(skey, max_chars=20000)
                    for ch in chunks:
                        if not isinstance(ch, str) or not ch.strip():
                            corrupt_reads.append("empty chunk")
                    if not isinstance(git_txt, str):
                        corrupt_reads.append("non-str git_txt")

            child_script = (
                "import os, sys, time\n"
                f"sys.path.insert(0, {str(SRC_DIR)!r})\n"
                "from pathlib import Path\n"
                "from agent_voice_input.session_ctx import (\n"
                "    prune_stale_session_registry,\n"
                "    write_session_registry_entry,\n"
                ")\n"
                "rt = Path(sys.argv[1])\n"
                "wid = int(sys.argv[2])\n"
                "for i in range(18):\n"
                "    sid = f'{(wid * 100 + (i % 6)):08x}-aaaa-bbbb-cccc-ddddeeeeffff'\n"
                "    write_session_registry_entry(\n"
                "        rt, cli='claude', session_id=sid,\n"
                "        jsonl_path=f'/tmp/s-{wid}-{i}.jsonl', cwd='/tmp/proj',\n"
                "        tty='/dev/pts/3', agent_pid=999999,\n"
                "    )\n"
                "    if i % 4 == 0:\n"
                "        prune_stale_session_registry(\n"
                "            rt, now_ts=time.time() + 900000.0, max_age_sec=10.0,\n"
                "            proc_root=rt / 'empty_proc',\n"
                "        )\n"
            )

            def _registry_thread_worker(tid: int) -> None:
                for i in range(22):
                    sid = f"{(tid * 50 + (i % 6)):08x}-1111-2222-3333-444455556666"
                    p = write_session_registry_entry(
                        rt,
                        cli="codex",
                        session_id=sid,
                        jsonl_path=f"/tmp/th-{tid}-{i}.jsonl",
                        cwd="/tmp/repo",
                        tty="/dev/pts/1",
                        agent_pid=999998,
                    )
                    for _, _, rec in _iter_trusted_registry_records([rt]):
                        if "cli" not in rec or "session_id" not in rec:
                            corrupt_reads.append(f"corrupt record: {rec!r}")
                    raw_txt = ""
                    try:
                        raw_txt = p.read_text(encoding="utf-8")
                    except FileNotFoundError:
                        raw_txt = ""
                    if raw_txt:
                        try:
                            parsed = json.loads(raw_txt)
                            if not isinstance(parsed, dict) or not parsed.get("session_id"):
                                corrupt_reads.append("torn json dict")
                        except ValueError as exc:
                            corrupt_reads.append(f"half-written JSON observed: {exc}")
                    if i % 5 == 0:
                        prune_stale_session_registry(
                            rt,
                            now_ts=time.time() + 900000.0,
                            max_age_sec=10.0,
                            proc_root=rt / "empty_proc",
                        )

            (rt / "empty_proc").mkdir(parents=True, exist_ok=True)
            procs: list[subprocess.Popen[str]] = []
            rem_threads: list[threading.Thread] = []
            reg_threads: list[threading.Thread] = []
            try:
                with preemption_watchdog(timeout_sec=15.0, switch_interval=1e-6):
                    # Seed initial full sync + ETag 304 hit + tripped -> half-open claim before storm
                    s1 = tracker.sync_session(window_title=title, window_process="WindowsTerminal.exe")
                    self.assertIsNotNone(s1)
                    s2 = tracker.sync_session(window_title=title, window_process="WindowsTerminal.exe")
                    self.assertIsNotNone(s2)
                    force_fail[0] = True
                    for _ in range(2):
                        _ = tracker.sync_session(
                            window_title="codex · ~/other [#b2c3d4e5] [@devbox]",
                            window_process="WindowsTerminal.exe",
                        )
                    force_fail[0] = False
                    _advance_clock(65.0)
                    tracker.prefetch_remote_session(
                        title, window_process="WindowsTerminal.exe", seq_id=9001
                    )

                    for pidx in range(3):
                        procs.append(
                            subprocess.Popen(
                                [sys.executable, "-c", child_script, str(rt), str(pidx + 1)],
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                text=True,
                            )
                        )
                    rem_threads = [
                        threading.Thread(target=_prefetch_and_sync_worker, args=(w,), name=f"rem-{w}", daemon=True)
                        for w in range(5)
                    ]
                    reg_threads = [
                        threading.Thread(target=_registry_thread_worker, args=(t,), name=f"reg-{t}", daemon=True)
                        for t in range(4)
                    ]
                    for th in [*rem_threads, *reg_threads]:
                        th.start()
                    for th in [*rem_threads, *reg_threads]:
                        th.join(timeout=10.0)
                        self.assertFalse(th.is_alive())
                    for proc in procs:
                        out_s, err_s = proc.communicate(timeout=10.0)
                        self.assertEqual(proc.returncode, 0, err_s or out_s)

                    rm.cancel_prefetch(seq_id=None)
                    deadline_bg = time.monotonic() + 2.0
                    alive_bg: list[threading.Thread] = []
                    while time.monotonic() < deadline_bg:
                        with rm._lock:
                            alive_bg = [t for t in rm._bg_threads if t.is_alive()]
                        if not alive_bg:
                            break
                        time.sleep(0.01)
                    self.assertEqual(alive_bg, [])

                    with rm._lock:
                        self.assertGreaterEqual(len(rm._breakers), 1)
                        for hname, bst in rm._breakers.items():
                            self.assertFalse(
                                bst.half_open_in_flight,
                                f"Circuit breaker half_open_in_flight leaked on {hname}",
                            )
                        self.assertEqual(dict(rm._inflight), {})

                self.assertGreaterEqual(valid_snaps[0], 3)
                self.assertGreaterEqual(etag_304_count[0], 1)
                self.assertGreaterEqual(half_open_claimed_count[0], 1)
                self.assertEqual(corrupt_reads, [])
                sdir = rt / "sessions"
                leftover_tmps = [p.name for p in sdir.iterdir() if p.name.endswith(".tmp")]
                self.assertEqual(leftover_tmps, [])
                for jf in sdir.glob("*.json"):
                    self.assertEqual(stat.S_IMODE(jf.stat().st_mode), 0o600)
                    self.assertIsNotNone(_read_trusted_registry_json(jf))
            finally:
                for th in [*rem_threads, *reg_threads]:
                    if th.is_alive():
                        th.join(timeout=1.0)
                for proc in procs:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait(timeout=1.0)
                    for pipe in (proc.stdout, proc.stderr):
                        if pipe is not None:
                            pipe.close()
                tracker.close()

    def test_zone4_jsonl_shim_crash_respawn_and_concurrent_hud_events(self) -> None:
        """Zone 4: Mock shim abrupt EOF crashes, auto-backoff respawn, concurrent HUD/events & pipe ownership."""
        crashing_shim_code = (
            "import json, sys\n"
            "gen = int(sys.argv[1])\n"
            "sys.stdout.write(json.dumps({'event': 'ready', 'ui_culture': 'zh-CN', 'caps': ['hud_layers']}) + '\\n')\n"
            "sys.stdout.write(json.dumps({'event': 'toggle', 'hwnd': 300 + gen, 'process': 'WindowsTerminal.exe', 'title': 'codex'}) + '\\n')\n"
            "sys.stdout.flush()\n"
            "handled = 0\n"
            "for raw in sys.stdin:\n"
            "    raw = raw.strip()\n"
            "    if not raw:\n"
            "        continue\n"
            "    req = json.loads(raw)\n"
            "    cmd = req.get('cmd')\n"
            "    if cmd == 'quit':\n"
            "        break\n"
            "    if cmd == 'hud_level':\n"
            "        continue\n"
            "    handled += 1\n"
            "    if gen <= 3 and handled >= 3:\n"
            "        sys.exit(13)\n"
            "    sys.stdout.write(json.dumps({'type': 'reply', 'reply': cmd, 'seq_id': req.get('seq_id'), 'ok': True, 'hwnd': 301, 'process': 'WindowsTerminal.exe', 'title': 'codex'}) + '\\n')\n"
            "    sys.stdout.flush()\n"
        )

        class _FastHealShimPlatform(WslPlatform):
            _shim_ready_timeout = 1.5
            _shim_backoff_base = 0.03
            _shim_backoff_cap = 0.12
            _shim_respawn_delays = (0.02, 0.04, 0.06, 0.08)

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            empty_proc4 = Path(td) / "empty_proc"
            empty_proc4.mkdir(parents=True, exist_ok=True)
            gen_lock = threading.Lock()
            spawn_gen = [0]
            spawned_procs: list[subprocess.Popen[str]] = []
            orig_popen = subprocess.Popen

            def _tracking_popen(*args: Any, **kwargs: Any) -> Any:
                p = orig_popen(*args, **kwargs)
                if args and isinstance(args[0], list) and len(args[0]) >= 4 and args[0][2] == "-c":
                    spawned_procs.append(p)
                return p

            def _shim_factory() -> list[str]:
                with gen_lock:
                    spawn_gen[0] += 1
                    g = spawn_gen[0]
                return [sys.executable, "-u", "-c", crashing_shim_code, str(g)]

            cfg = Config(
                asr=AsrConfig(provider="dashscope"),
                refiner=RefinerConfig(provider="off", include_git_status=False),
                audio=AudioConfig(wsl_source="pulse", beep=True, save_recordings=False, tail_drain_sec=0.0),
                hotkey=HotkeyConfig(debounce_ms=0, scope="all"),
            )

            plat = _FastHealShimPlatform(cfg, env={"LANG": "C.UTF-8"}, shim_cmd_factory=_shim_factory)
            plat.start_recording = lambda runtime_dir, **kw: None  # type: ignore[method-assign]
            plat.stop_recording = lambda **kw: (self.speech_wav, "pulse")  # type: ignore[method-assign]
            plat.cancel_recording = lambda: None  # type: ignore[method-assign]
            plat.deliver_text = lambda text, seq_id, *a, **kw: (  # type: ignore[method-assign]
                kw["before_commit"]() if kw.get("before_commit") else True,
                PasteOutcome(True, True, "pasted", "WindowsTerminal.exe"),
            )[1]

            tracker = SessionTracker(
                env={"HOME": td},
                runtime_dir=rt,
                proc_root=empty_proc4,
            )
            daemon = VoiceInputDaemon(
                cfg,
                env={"HOME": td, "DASHSCOPE_API_KEY": "sk-test-q"},
                runtime_dir=rt,
                platform=plat,
                asr_provider=_TaggingAsrProvider(),
                tracker=tracker,
                paste_settle_sec=0.0,
            )

            hammer_threads: list[threading.Thread] = []

            def _hud_and_trigger_hammer(tid: int) -> None:
                states = ("recording", "recognizing", "refining", "done", "cancelled", "hide")
                for step in range(18):
                    st = states[(tid + step) % len(states)]
                    plat.hud(st)
                    plat.hud_level(((tid * 7 + step) % 10) / 10.0)
                    if step % 4 == 0:
                        daemon.handle_trigger(
                            "toggle",
                            ForegroundWindow(hwnd=301, process="WindowsTerminal.exe", title="codex"),
                        )
                    time.sleep(0.002)

            try:
                with mock.patch("subprocess.Popen", side_effect=_tracking_popen), preemption_watchdog(
                    timeout_sec=15.0, switch_interval=1e-6
                ):
                    plat.start_event_listener(daemon._on_platform_event)
                    hammer_threads = [
                        threading.Thread(
                            target=_hud_and_trigger_hammer, args=(i,), name=f"shim-hammer-{i}", daemon=True
                        )
                        for i in range(6)
                    ]
                    for th in hammer_threads:
                        th.start()
                    for th in hammer_threads:
                        th.join(timeout=10.0)
                        self.assertFalse(th.is_alive())

                    # Wait up to 2.0s for background backoff respawn to reach stable generation >= 4
                    heal_deadline = time.monotonic() + 2.0
                    while time.monotonic() < heal_deadline and spawn_gen[0] < 4:
                        time.sleep(0.02)
                    self.assertGreaterEqual(spawn_gen[0], 4)
                    ping_ok = plat._shim_rpc({"cmd": "ping"}, timeout_sec=1.5)
                    self.assertIsNotNone(ping_ok)
                    assert ping_ok is not None
                    self.assertTrue(ping_ok.get("ok"))

                    # Explicit cooldown fast-skip check under _trigger_lock (scheduler-independent):
                    # Ensure daemon starts from idle (drain hammer utterances first), enter backoff window,
                    # and verify handle_trigger("ptt_start") invokes hud("recording") while holding
                    # _trigger_lock and immediately drops the frame without calling _ensure_shim().
                    assert isinstance(plat, JsonlShimMixin)
                    if daemon._recording_active:
                        daemon.handle_trigger("ptt_cancel", ForegroundWindow(hwnd=301, process="WindowsTerminal.exe", title="codex"))
                    idle_deadline = time.monotonic() + 5.0
                    while time.monotonic() < idle_deadline:
                        with daemon._lock:
                            if (
                                not daemon._recording_active
                                and daemon._inflight_or_queued_count_locked() == 0
                                and daemon._raw_pending_count_locked() == 0
                            ):
                                break
                        time.sleep(0.01)
                    with daemon._lock:
                        self.assertEqual(daemon._inflight_or_queued_count_locked(), 0)
                        self.assertEqual(daemon._raw_pending_count_locked(), 0)
                    with plat._shim_lock:
                        live_p, plat._shim = plat._shim, None
                        plat._spawned_shim = None
                        plat._spawned_ready = False
                        plat._shim_retry_after = time.monotonic() + 30.0
                    plat._reap_owned_shim(live_p)
                    spawns_before_cd = spawn_gen[0]
                    hud_recording_locked: list[bool] = []
                    hud_ensure_calls: list[tuple[bool, bool]] = []
                    in_hud_recording = [False]
                    orig_hud = plat.hud
                    orig_ensure = plat._ensure_shim

                    def _spy_hud(state: str, *args: Any, **kwargs: Any) -> None:
                        if state == "recording":
                            hud_recording_locked.append(daemon._trigger_lock.locked())
                            in_hud_recording[0] = True
                            try:
                                orig_hud(state, *args, **kwargs)
                            finally:
                                in_hud_recording[0] = False
                        else:
                            orig_hud(state, *args, **kwargs)

                    def _spy_ensure_shim(*, ignore_cooldown: bool = False) -> bool:
                        locked_now = daemon._trigger_lock.locked()
                        ret = orig_ensure(ignore_cooldown=ignore_cooldown)
                        if not ignore_cooldown and in_hud_recording[0]:
                            hud_ensure_calls.append((locked_now, ret))
                        return ret

                    with mock.patch.object(plat, "hud", side_effect=_spy_hud), mock.patch.object(
                        plat, "_ensure_shim", side_effect=_spy_ensure_shim
                    ):
                        r_cd = daemon.handle_trigger(
                            "ptt_start",
                            ForegroundWindow(hwnd=301, process="WindowsTerminal.exe", title="codex"),
                        )
                    self.assertTrue(r_cd.get("ok"), f"ptt_start unexpectedly skipped: {r_cd}")
                    self.assertEqual(r_cd.get("state"), "recording")
                    self.assertEqual(hud_recording_locked, [True])
                    self.assertEqual(
                        hud_ensure_calls,
                        [],
                        f"hud('recording') must not call _ensure_shim() under _trigger_lock, got {hud_ensure_calls}",
                    )
                    self.assertEqual(spawn_gen[0], spawns_before_cd)
                    daemon.handle_trigger(
                        "ptt_cancel",
                        ForegroundWindow(hwnd=301, process="WindowsTerminal.exe", title="codex"),
                    )

                    # Regression check A: _reap_shim_proc with single-owner _ShimWriter neither hangs on
                    # blocked stdout nor leaks stdin; writer thread is the sole closer and exits promptly.
                    r_fd, w_fd = os.pipe()
                    r_file = os.fdopen(r_fd, "r", encoding="utf-8")
                    in_r_fd, in_w_fd = os.pipe()
                    in_w_file = os.fdopen(in_w_fd, "w", encoding="utf-8")
                    blocked_reader_done = threading.Event()

                    def _blocked_reader() -> None:
                        try:
                            _ = r_file.readline()
                        except Exception:
                            pass
                        finally:
                            r_file.close()
                            blocked_reader_done.set()

                    br_th = threading.Thread(target=_blocked_reader, daemon=True)
                    br_th.start()
                    fake_proc = mock.MagicMock()
                    fake_proc.poll.return_value = 0
                    fake_proc.stdin = in_w_file
                    fake_proc.stdout = r_file
                    fake_proc._shim_reader_th = br_th
                    writer_a = _ShimWriter(fake_proc)
                    try:
                        t_reap0 = time.monotonic()
                        _reap_shim_proc(fake_proc, writer=writer_a)
                        reap_dt = time.monotonic() - t_reap0
                        self.assertLess(reap_dt, 1.5)
                        self.assertTrue(in_w_file.closed)
                        self.assertFalse(writer_a._thread.is_alive())
                        self.assertFalse(r_file.closed)
                    finally:
                        writer_a.close(send_quit=False)
                        if not in_w_file.closed:
                            in_w_file.close()
                        os.close(in_r_fd)
                        os.close(w_fd)
                        self.assertTrue(blocked_reader_done.wait(timeout=1.0))
                        self.assertTrue(r_file.closed)

                    # Regression check B: _ShimWriter close() aborts an in-flight full-pipe write promptly
                    # without leaking writer threads or stdin fds.
                    fp_r, fp_w = os.pipe()
                    os.set_blocking(fp_w, False)
                    while True:
                        try:
                            os.write(fp_w, b"B" * 4096)
                        except BlockingIOError:
                            break
                    os.set_blocking(fp_w, True)
                    fp_w_file = os.fdopen(fp_w, "w", encoding="utf-8", buffering=1)
                    fake_full = mock.MagicMock()
                    fake_full.poll.return_value = None
                    fake_full.stdin = fp_w_file
                    fake_full.stdout = None
                    writer_b = _ShimWriter(fake_full)
                    write_res: list[tuple[str, str]] = []
                    write_th = threading.Thread(
                        target=lambda: write_res.append(
                            writer_b.write_sync('{"cmd":"ping"}\n', time.monotonic() + 5.0)
                        ),
                        daemon=True,
                    )
                    write_th.start()
                    try:
                        time.sleep(0.03)
                        t_cl0 = time.monotonic()
                        quit_sent = writer_b.close(send_quit=False)
                        cl_dt = time.monotonic() - t_cl0
                        self.assertFalse(quit_sent)
                        self.assertLess(cl_dt, 0.35)
                        write_th.join(timeout=1.0)
                        self.assertFalse(write_th.is_alive())
                        self.assertFalse(writer_b._thread.is_alive())
                        self.assertTrue(fp_w_file.closed)
                        self.assertEqual(len(write_res), 1)
                        self.assertEqual(write_res[0][0], "not_sent")
                    finally:
                        writer_b.close(send_quit=False)
                        write_th.join(timeout=1.0)
                        if not fp_w_file.closed:
                            fp_w_file.close()
                        os.close(fp_r)
            finally:
                for th in hammer_threads:
                    if th.is_alive():
                        th.join(timeout=1.0)
                daemon.close()
                plat.close()
                tracker.close()
                for sp in spawned_procs:
                    if sp.poll() is None:
                        sp.kill()
                    try:
                        sp.wait(timeout=1.0)
                    except Exception:
                        pass
                    for pipe in (sp.stdin, sp.stdout, sp.stderr):
                        if pipe is not None:
                            try:
                                pipe.close()
                            except Exception:
                                pass


class TestBatch1BConcurrencyFixes(unittest.TestCase):
    """Batch 1B targeted regression tests for daemon, platform_common, and platform_macos races."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.speech_wav = _build_voiced_wav(0.36)

    def test_normal_stop_calls_stop_recording_before_hud_and_slow_hud_does_not_block_locks(self) -> None:
        """1) Normal ptt_stop/cancel stops capture BEFORE HUD; non-blocking bridge HUD never stalls _trigger_lock."""
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            empty_proc = Path(td) / "empty_proc"
            empty_proc.mkdir(parents=True, exist_ok=True)
            cfg = Config(
                asr=AsrConfig(provider="dashscope"),
                refiner=RefinerConfig(provider="off", include_git_status=False),
                audio=AudioConfig(beep=False, save_recordings=False, tail_drain_sec=0.0),
                hotkey=HotkeyConfig(debounce_ms=0, scope="all"),
            )
            call_order: list[str] = []

            # Real full pipe behind a JsonlShimMixin bridge simulates a slow/wedged native shim.
            r_fd, w_fd = os.pipe()
            os.set_blocking(w_fd, False)
            while True:
                try:
                    os.write(w_fd, b"H" * 4096)
                except BlockingIOError:
                    break
            os.set_blocking(w_fd, True)
            w_file = os.fdopen(w_fd, "w", encoding="utf-8", buffering=1)

            class _SlowBridgeProc:
                def __init__(self, stdin_obj: Any) -> None:
                    self.stdin = stdin_obj
                    self.stdout = None
                    self.stderr = None

                def poll(self) -> int | None:
                    return None

                def kill(self) -> None:
                    pass

                def wait(self, timeout: float | None = None) -> int:
                    del timeout
                    return 0

            class _OrderAndNonBlockingHudPlatform(_TorturePlatform, JsonlShimMixin):
                _shim_graceful = False
                _shim_label = "hud_order_test"

                def __init__(self, wav: bytes) -> None:
                    super().__init__(wav)
                    self.cfg = cfg
                    self.env = {}
                    self._init_shim_bridge()

                def _build_shim_cmd(self) -> list[str] | None:
                    return None

                def stop_recording(self, *, drain: bool = True) -> tuple[bytes, str]:
                    call_order.append("stop_recording")
                    return super().stop_recording(drain=drain)

                def cancel_recording(self) -> None:
                    call_order.append("cancel_recording")
                    super().cancel_recording()

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
                    call_order.append(f"hud:{state}")
                    JsonlShimMixin.hud(
                        self,
                        state,
                        text=text,
                        duration_ms=duration_ms,
                        layer=layer,
                        clear_main=clear_main,
                        clear_toast=clear_toast,
                        bg_count=bg_count,
                    )

                def close(self) -> None:
                    JsonlShimMixin.close(self)

            plat = _OrderAndNonBlockingHudPlatform(self.speech_wav)
            proc = _SlowBridgeProc(w_file)
            plat._shim = proc  # type: ignore[assignment]
            plat._spawned_shim = proc  # type: ignore[assignment]
            plat._spawned_ready = True
            plat._ready.set()
            plat._ensure_shim = lambda *, ignore_cooldown=False: True  # type: ignore[method-assign]
            tracker = SessionTracker(env={"HOME": td}, runtime_dir=rt, proc_root=empty_proc)
            daemon = VoiceInputDaemon(
                cfg,
                env={"HOME": td, "DASHSCOPE_API_KEY": "sk-test-q"},
                runtime_dir=rt,
                platform=plat,
                asr_provider=_TaggingAsrProvider(),
                tracker=tracker,
                paste_settle_sec=0.0,
            )
            win = ForegroundWindow(hwnd=101, process="WindowsTerminal.exe", title="codex")
            try:
                # 1a. Normal ptt_stop: stop_recording precedes hud:recognizing and never blocks on full HUD pipe.
                self.assertTrue(daemon.handle_trigger("ptt_start", win)["ok"])
                call_order.clear()
                t_stop0 = time.monotonic()
                res_stop = daemon.handle_trigger("ptt_stop", win)
                stop_elapsed = time.monotonic() - t_stop0
                self.assertTrue(res_stop["ok"])
                self.assertLess(
                    stop_elapsed,
                    0.25,
                    f"ptt_stop must not wait on full shim HUD pipe (took {stop_elapsed:.3f}s)",
                )
                self.assertIn("stop_recording", call_order)
                self.assertIn("hud:recognizing", call_order)
                self.assertLess(
                    call_order.index("stop_recording"),
                    call_order.index("hud:recognizing"),
                    f"stop_recording must run before HUD update, got {call_order}",
                )

                # 1b. Subsequent ptt_start + handle_cancel + ptt_cancel stay responsive and cancel_recording precedes HUD.
                t_next0 = time.monotonic()
                self.assertTrue(daemon.handle_trigger("ptt_start", win)["ok"])
                self.assertLess(time.monotonic() - t_next0, 0.25)
                call_order.clear()
                self.assertTrue(daemon.handle_cancel()["ok"])
                self.assertIn("cancel_recording", call_order)
                self.assertIn("hud:cancelled", call_order)
                self.assertLess(
                    call_order.index("cancel_recording"),
                    call_order.index("hud:cancelled"),
                    f"cancel_recording must precede HUD in handle_cancel, got {call_order}",
                )

                self.assertTrue(daemon.handle_trigger("ptt_start", win)["ok"])
                call_order.clear()
                self.assertTrue(daemon.handle_trigger("ptt_cancel", win)["ok"])
                self.assertIn("cancel_recording", call_order)
                self.assertTrue(any(c.startswith("hud:") for c in call_order))
                first_hud_idx = next(i for i, c in enumerate(call_order) if c.startswith("hud:"))
                self.assertLess(
                    call_order.index("cancel_recording"),
                    first_hud_idx,
                    f"cancel_recording must precede HUD in ptt_cancel, got {call_order}",
                )
            finally:
                daemon.close()
                tracker.close()
                if not w_file.closed:
                    w_file.close()
                os.close(r_fd)

    def test_control_sock_status_stays_fast_during_slow_start_and_stop(self) -> None:
        """2) control.sock offloads slow rec_start/rec_stop/toggle so status responds within tens of ms."""
        from agent_voice_input.daemon import send_control_command

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            empty_proc = Path(td) / "empty_proc"
            empty_proc.mkdir(parents=True, exist_ok=True)
            cfg = Config(
                asr=AsrConfig(provider="dashscope"),
                refiner=RefinerConfig(provider="off", include_git_status=False),
                audio=AudioConfig(beep=False, save_recordings=False, tail_drain_sec=0.0),
                hotkey=HotkeyConfig(debounce_ms=0, scope="all"),
            )
            start_entered = threading.Event()
            release_start = threading.Event()
            stop_entered = threading.Event()
            release_stop = threading.Event()
            exec_order: list[str] = []

            class _SlowCapturePlatform(_TorturePlatform):
                def start_recording(
                    self, runtime_dir: Path, *, loopback: bool = False, seq_id: int | None = None
                ) -> None:
                    exec_order.append("start_begin")
                    start_entered.set()
                    release_start.wait(timeout=3.0)
                    super().start_recording(runtime_dir, loopback=loopback, seq_id=seq_id)
                    exec_order.append("start_end")

                def stop_recording(self, *, drain: bool = True) -> tuple[bytes, str]:
                    exec_order.append("stop_begin")
                    stop_entered.set()
                    release_stop.wait(timeout=3.0)
                    res = super().stop_recording(drain=drain)
                    exec_order.append("stop_end")
                    return res

            plat = _SlowCapturePlatform(self.speech_wav)
            tracker = SessionTracker(env={"HOME": td}, runtime_dir=rt, proc_root=empty_proc)
            daemon = VoiceInputDaemon(
                cfg,
                env={"HOME": td, "DASHSCOPE_API_KEY": "sk-test-q"},
                runtime_dir=rt,
                platform=plat,
                asr_provider=_TaggingAsrProvider(),
                tracker=tracker,
                paste_settle_sec=0.0,
            )
            srv_th = threading.Thread(target=daemon.serve_forever, name="test-ctrl-srv", daemon=True)
            srv_th.start()
            try:
                dl = time.monotonic() + 3.0
                while time.monotonic() < dl and not daemon.sock_path.exists():
                    time.sleep(0.01)
                self.assertTrue(daemon.sock_path.exists())

                start_box: list[dict[str, Any]] = []
                stop_box: list[dict[str, Any]] = []
                t_start = threading.Thread(
                    target=lambda: start_box.append(
                        send_control_command(rt, {"cmd": "rec_start"}, timeout_sec=5.0)
                    ),
                    daemon=True,
                )
                t_start.start()
                self.assertTrue(start_entered.wait(timeout=2.0))

                # While rec_start is blocked in start_recording(), status MUST return in < 150ms!
                t0 = time.monotonic()
                st_during_start = send_control_command(rt, {"cmd": "status"}, timeout_sec=2.0)
                st_dt1 = time.monotonic() - t0
                self.assertTrue(st_during_start["ok"])
                self.assertLess(st_dt1, 0.25, f"status blocked during slow rec_start ({st_dt1:.3f}s)")

                # Queue rec_stop while rec_start is still running -> ordered worker runs stop AFTER start!
                t_stop = threading.Thread(
                    target=lambda: stop_box.append(
                        send_control_command(rt, {"cmd": "rec_stop"}, timeout_sec=5.0)
                    ),
                    daemon=True,
                )
                t_stop.start()
                time.sleep(0.03)
                release_start.set()
                self.assertTrue(stop_entered.wait(timeout=2.0))

                # While rec_stop is blocked in stop_recording(), status MUST also stay fast!
                t1 = time.monotonic()
                st_during_stop = send_control_command(rt, {"cmd": "status"}, timeout_sec=2.0)
                st_dt2 = time.monotonic() - t1
                self.assertTrue(st_during_stop["ok"])
                self.assertLess(st_dt2, 0.25, f"status blocked during slow rec_stop ({st_dt2:.3f}s)")

                release_stop.set()
                t_start.join(timeout=3.0)
                t_stop.join(timeout=3.0)
                self.assertEqual(len(start_box), 1)
                self.assertEqual(len(stop_box), 1)
                self.assertTrue(start_box[0]["ok"])
                self.assertTrue(stop_box[0]["ok"])
                self.assertEqual(exec_order, ["start_begin", "start_end", "stop_begin", "stop_end"])

                ctrl_w = daemon._ctrl_trigger_worker
                shut_resp = send_control_command(rt, {"cmd": "shutdown"}, timeout_sec=2.0)
                self.assertTrue(shut_resp["ok"])
                srv_th.join(timeout=3.0)
                self.assertFalse(srv_th.is_alive())
                if ctrl_w is not None:
                    self.assertFalse(ctrl_w.is_alive(), "control trigger worker leaked after shutdown")
            finally:
                release_start.set()
                release_stop.set()
                daemon.close()
                srv_th.join(timeout=2.0)
                tracker.close()

    def test_macos_start_recording_probe_racing_with_close_never_leaves_closed_and_active(self) -> None:
        """3) Concurrent MacOSPlatform.start_recording (slow audio probe) + close() never leaves closed=True & rec_active=True."""
        from agent_voice_input.platform_macos import MacOSPlatform

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            rt.mkdir(parents=True, exist_ok=True)
            probe_entered = threading.Event()
            release_probe = threading.Event()
            rec_starts = [0]
            rec_stops = [0]

            class _FakeRec:
                def __init__(self) -> None:
                    self._cmd_factory = None
                    self.current_level = 0.0
                    self.is_running = False

                def start(self, *args: Any, **kwargs: Any) -> None:
                    del args, kwargs
                    rec_starts[0] += 1
                    self.is_running = True

                def stop(self, *, drain_sec: float = 0.0) -> bytes:
                    del drain_sec
                    rec_stops[0] += 1
                    self.is_running = False
                    return b""

                def diagnostic_summary(self) -> str:
                    return "fake"

            def _slow_probe() -> dict[str, Any]:
                probe_entered.set()
                release_probe.wait(timeout=3.0)
                return {
                    "hal_ok": True,
                    "has_input": True,
                    "default_uid": "mic-1",
                    "default_name": "Built-in Mic",
                    "alive_uids": ["mic-1"],
                    "mic_auth": "authorized",
                }

            plat = MacOSPlatform(
                Config(),
                env={"HOME": td},
                cache_dir=Path(td) / "cache",
                self_check_fn=_slow_probe,
            )
            plat._ff_rec = _FakeRec()  # type: ignore[assignment]
            start_errs: list[BaseException] = []

            def _run_start() -> None:
                try:
                    plat.start_recording(rt, seq_id=1)
                except BaseException as exc:
                    start_errs.append(exc)

            th = threading.Thread(target=_run_start, daemon=True)
            th.start()
            try:
                self.assertTrue(probe_entered.wait(timeout=2.0))
                plat.close()
                release_probe.set()
                th.join(timeout=2.0)
                self.assertFalse(th.is_alive())
                self.assertEqual(start_errs, [])
                self.assertTrue(plat._closed)
                self.assertFalse(plat.is_recording, "closed=True must never coexist with is_recording=True")
                self.assertFalse(hasattr(plat, "_rec_active"))
                self.assertFalse(hasattr(plat, "_recording"))
                self.assertIsNone(plat._cur_session)
                self.assertEqual(rec_starts[0], 0, "stale start_recording after close() must never spawn ffmpeg")
            finally:
                release_probe.set()
                th.join(timeout=1.0)
                plat.close()

    def test_shim_rpc_full_pipe_times_out_on_deadline_and_hud_level_drops_when_busy(self) -> None:
        """4) Real full os.pipe() honors _shim_rpc timeout_sec=0.05 and hud_level drops frames without blocking."""
        class _PipeBridge(JsonlShimMixin):
            _shim_graceful = False
            _shim_label = "pipe_test"

            def __init__(self) -> None:
                self.cfg = Config()
                self.env = {}
                self._init_shim_bridge()

            def _build_shim_cmd(self) -> list[str] | None:
                return None

        r_fd, w_fd = os.pipe()
        w_file = None
        try:
            # Fill kernel anonymous pipe buffer completely using non-blocking writes.
            os.set_blocking(w_fd, False)
            chunk = b"A" * 4096
            while True:
                try:
                    os.write(w_fd, chunk)
                except BlockingIOError:
                    break
            os.set_blocking(w_fd, True)
            w_file = os.fdopen(w_fd, "w", encoding="utf-8", buffering=1)
            w_fd = -1  # ownership transferred to w_file

            class _FullPipeProc:
                def __init__(self, stdin_obj: Any) -> None:
                    self.stdin = stdin_obj
                    self.stdout = None
                    self.stderr = None

                def poll(self) -> int | None:
                    return None

                def kill(self) -> None:
                    pass

                def wait(self, timeout: float | None = None) -> int:
                    del timeout
                    return 0

            bridge = _PipeBridge()
            proc = _FullPipeProc(w_file)
            bridge._shim = proc  # type: ignore[assignment]
            bridge._spawned_shim = proc  # type: ignore[assignment]
            bridge._spawned_ready = True
            bridge._ready.set()
            bridge._ensure_shim = lambda *, ignore_cooldown=False: True  # type: ignore[method-assign]

            # 4a. _shim_rpc(..., timeout_sec=0.05) MUST time out promptly with write_status='not_sent' (0 bytes)
            # and keep the healthy shim alive!
            t0 = time.monotonic()
            with self.assertRaises(_ShimWriteTimeout) as ctx_4a:
                bridge._shim_rpc({"cmd": "ping"}, timeout_sec=0.05, allow_retry=True)
            dt_rpc = time.monotonic() - t0
            self.assertEqual(ctx_4a.exception.write_status, "not_sent")
            self.assertFalse(ctx_4a.exception.partial)
            self.assertGreaterEqual(dt_rpc, 0.04)
            self.assertLess(dt_rpc, 0.35, f"_shim_rpc exceeded deadline on full pipe ({dt_rpc:.3f}s)")
            self.assertIs(bridge._shim, proc)
            writer = bridge._current_shim_writer()
            self.assertIsNotNone(writer)
            assert writer is not None
            self.assertFalse(writer.poisoned)

            # 4b. hud_level() on full pipe overwrites single slot without ever blocking audio reader!
            t_lvl0 = time.monotonic()
            for idx in range(50):
                bridge.hud_level(0.5 + (idx % 5) * 0.1)
            dt_lvl = time.monotonic() - t_lvl0
            self.assertLess(dt_lvl, 0.05, f"hud_level blocked on full pipe ({dt_lvl:.3f}s)")

            # 4c. While a slower RPC (0.35s) occupies the single writer on a full pipe, a queued shorter-deadline
            # RPC (0.05s) still honors its own deadline with write_status='not_sent' and hud_level stays instant!
            slow_done = threading.Event()
            slow_errs: list[BaseException] = []

            def _slow_rpc() -> None:
                try:
                    bridge._shim_rpc({"cmd": "ping"}, timeout_sec=0.35, allow_retry=False)
                except BaseException as exc:
                    slow_errs.append(exc)
                finally:
                    slow_done.set()

            slow_th = threading.Thread(target=_slow_rpc, daemon=True)
            slow_th.start()
            time.sleep(0.02)
            t_lock0 = time.monotonic()
            for _ in range(20):
                bridge.hud_level(0.42)
            self.assertLess(time.monotonic() - t_lock0, 0.05)

            t_lk_rpc = time.monotonic()
            with self.assertRaises(_ShimWriteTimeout) as ctx_4c:
                bridge._shim_rpc({"cmd": "ping"}, timeout_sec=0.05, allow_retry=False)
            self.assertEqual(ctx_4c.exception.write_status, "not_sent")
            self.assertLess(time.monotonic() - t_lk_rpc, 0.25)
            self.assertIs(bridge._shim, proc)
            self.assertTrue(slow_done.wait(timeout=1.0))
            self.assertEqual(len(slow_errs), 1)
            self.assertIsInstance(slow_errs[0], _ShimWriteTimeout)
            bridge.close()
        finally:
            if w_file is not None:
                try:
                    w_file.close()
                except Exception:
                    pass
            if w_fd >= 0:
                os.close(w_fd)
            os.close(r_fd)

    def test_ensure_shim_slow_build_does_not_block_is_shim_ok_or_close(self) -> None:
        """5) _build_shim_cmd() runs outside _shim_lock so is_shim_ok() and close() never block on slow compile."""
        from agent_voice_input.platform_macos import MacOSPlatform

        build_entered = threading.Event()
        release_build = threading.Event()

        def _slow_build() -> list[str] | None:
            build_entered.set()
            release_build.wait(timeout=3.0)
            return [sys.executable, "-c", "import time; time.sleep(5)"]

        plat = MacOSPlatform(Config(), env={}, shim_cmd_factory=_slow_build)
        plat._on_event = lambda _e: None
        ensure_box: list[bool] = []
        th = threading.Thread(target=lambda: ensure_box.append(plat._ensure_shim()), daemon=True)
        th.start()
        try:
            self.assertTrue(build_entered.wait(timeout=2.0))
            t0 = time.monotonic()
            self.assertFalse(plat.is_shim_ok())
            self.assertLess(time.monotonic() - t0, 0.05)

            t_close0 = time.monotonic()
            plat.close()
            self.assertLess(time.monotonic() - t_close0, 0.2)
            release_build.set()
            th.join(timeout=2.0)
            self.assertFalse(th.is_alive())
            self.assertEqual(ensure_box, [False])
            self.assertIsNone(plat._shim)
        finally:
            release_build.set()
            th.join(timeout=1.0)
            plat.close()

    def test_macos_internal_audio_disconnect_does_not_block_shim_reader_or_event_worker(self) -> None:
        """6) Slow rec.stop() on audio_devices_changed runs off _shim_reader/_event_worker threads."""
        from agent_voice_input.platform_macos import MacOSPlatform, _MacRecSession

        stop_entered = threading.Event()
        release_stop = threading.Event()
        lost_fired = threading.Event()

        class _SlowStopRec:
            def stop(self, *, drain_sec: float = 0.0) -> bytes:
                del drain_sec
                stop_entered.set()
                release_stop.wait(timeout=3.0)
                return b""

        with tempfile.TemporaryDirectory() as td:
            plat = MacOSPlatform(Config(), env={"HOME": td}, cache_dir=Path(td) / "cache")
            try:
                sess = _MacRecSession(
                    1,
                    _SlowStopRec(),  # type: ignore[arg-type]
                    Path(td),
                    active_uid="bt-mic",
                    active_name="BT Mic",
                    daemon_seq_id=7,
                )
                with plat._rec_cond:
                    plat._cur_session = sess
                plat.set_recorder_lost_callback(lambda *, seq_id=None: lost_fired.set())

                t0 = time.monotonic()
                plat._on_shim_internal_event(
                    {
                        "event": "audio_devices_changed",
                        "hal_ok": True,
                        "has_input": False,
                        "alive_uids": [],
                    }
                )
                dt = time.monotonic() - t0
                self.assertLess(dt, 0.1, f"_on_shim_internal_event blocked caller ({dt:.3f}s)")
                self.assertTrue(stop_entered.wait(timeout=1.0))
                self.assertFalse(plat.is_recording)
                release_stop.set()
                self.assertTrue(lost_fired.wait(timeout=2.0))
            finally:
                release_stop.set()
                plat.close()

    def test_r2_p0_1_partial_write_timeout_poisons_before_unlock_and_schedules_respawn(self) -> None:
        """P0-1: Partial JSONL write timeout poisons proc before releasing _stdin_lock and triggers respawn."""
        class _PartialPipeBridge(JsonlShimMixin):
            _shim_graceful = False
            _shim_label = "partial_pipe_test"

            def __init__(self) -> None:
                self.cfg = Config()
                self.env = {}
                self._init_shim_bridge()

            def _build_shim_cmd(self) -> list[str] | None:
                return None

        r_fd, w_fd = os.pipe()
        w_file = None
        respawn_calls = [0]
        try:
            # Fill pipe completely, then drain 8192 bytes (> PIPE_BUF) so a 128KB RPC writes a prefix then hits partial timeout.
            os.set_blocking(w_fd, False)
            os.set_blocking(r_fd, False)
            total_filled = 0
            while True:
                try:
                    total_filled += os.write(w_fd, b"X" * 4096)
                except BlockingIOError:
                    break
            drained_head = os.read(r_fd, 8192)
            self.assertEqual(len(drained_head), 8192)
            os.set_blocking(w_fd, True)
            w_file = os.fdopen(w_fd, "w", encoding="utf-8", buffering=1)
            w_fd = -1

            class _PartialProc:
                def __init__(self, stdin_obj: Any) -> None:
                    self.stdin = stdin_obj
                    self.stdout = None
                    self.stderr = None
                    self.killed = False

                def poll(self) -> int | None:
                    return 1 if self.killed else None

                def kill(self) -> None:
                    self.killed = True

                def wait(self, timeout: float | None = None) -> int:
                    del timeout
                    return 1

            bridge = _PartialPipeBridge()
            proc = _PartialProc(w_file)
            bridge._shim = proc  # type: ignore[assignment]
            bridge._spawned_shim = proc  # type: ignore[assignment]
            bridge._spawned_ready = True
            bridge._on_event = lambda _e: None
            bridge._ready.set()
            bridge._ensure_shim = lambda *, ignore_cooldown=False: True  # type: ignore[method-assign]
            bridge._schedule_respawn_if_needed = lambda: respawn_calls.__setitem__(0, respawn_calls[0] + 1)  # type: ignore[method-assign]

            writer = bridge._current_shim_writer()
            self.assertIsNotNone(writer)
            assert writer is not None

            with self.assertRaises(_ShimWriteTimeout) as ctx_partial:
                bridge._shim_rpc({"cmd": "paste", "text": "Z" * 131072}, timeout_sec=0.04, allow_retry=False)
            self.assertEqual(ctx_partial.exception.write_status, "partial")
            self.assertTrue(ctx_partial.exception.partial)
            self.assertTrue(writer.poisoned)
            self.assertTrue(w_file.closed, "single writer must close stdin immediately on partial write")
            self.assertTrue(proc.killed)
            self.assertIsNone(bridge._shim)
            self.assertEqual(respawn_calls[0], 1, "poisoned current shim must schedule respawn")

            # Drain kernel pipe space so pipe WOULD be writable again, then attempt stale writes on old writer:
            # single writer already closed stdin & marked poisoned, so nothing can ever append to the half-line!
            pre_drained: list[bytes] = []
            try:
                pre_drained.append(os.read(r_fd, 16384))
            except BlockingIOError:
                pass
            self.assertGreater(sum(len(c) for c in pre_drained), 0)
            self.assertEqual(
                bridge._write_shim_command({"cmd": "hud_level", "level": 0.9}, writer=writer),
                "not_sent",
            )
            self.assertFalse(writer.enqueue_async('{"cmd":"after_poison"}\n'))
            writer.submit_level('{"cmd":"hud_level","level":0.9}\n')

            # Drain remaining kernel pipe bytes and confirm nothing was ever appended after partial JSON prefix.
            chunks: list[bytes] = list(pre_drained)
            while True:
                try:
                    data = os.read(r_fd, 65536)
                    if not data:
                        break
                    chunks.append(data)
                except BlockingIOError:
                    break
            tail = b"".join(chunks)[total_filled - 8192 :]
            self.assertGreater(len(tail), 0, "expected partial JSON prefix written before timeout")
            self.assertNotIn(b"hud_level", tail)
            self.assertNotIn(b"after_poison", tail)
            bridge.close()
        finally:
            if w_file is not None:
                try:
                    w_file.close()
                except Exception:
                    pass
            if w_fd >= 0:
                os.close(w_fd)
            os.close(r_fd)

    def test_r2_p0_2_macos_close_never_allows_ffmpeg_popen_after_close_returns(self) -> None:
        """P0-2: Lock-free spawn + close() convergence ensures zero live orphan FFmpeg when close() returns."""
        from agent_voice_input.platform_common import FfmpegWavRecorder
        from agent_voice_input.platform_macos import MacOSPlatform

        pcm_src = (
            "import os, sys, time\n"
            "os.write(1, b'\\x01\\x02' * 800)\n"
            "sys.stdout.flush()\n"
            "time.sleep(5.0)\n"
        )
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            rt.mkdir(parents=True, exist_ok=True)
            popen_entered = threading.Event()
            release_popen = threading.Event()
            spawned_procs: list[subprocess.Popen[bytes]] = []
            lock_free_during_popen: list[bool] = []

            plat = MacOSPlatform(
                Config(),
                env={"HOME": td},
                cache_dir=Path(td) / "cache",
                self_check_fn=lambda: {
                    "hal_ok": True,
                    "has_input": True,
                    "default_uid": "mic-1",
                    "default_name": "Built-in Mic",
                    "alive_uids": ["mic-1"],
                    "mic_auth": "authorized",
                },
            )
            plat._ff_rec = FfmpegWavRecorder(
                cmd_factory=lambda _f, _s: [sys.executable, "-c", pcm_src]
            )
            errs: list[BaseException] = []
            orig_popen = subprocess.Popen

            def _probe_rec_cond_unlocked_from_other_thread() -> bool:
                res_box: list[bool] = []

                def _probe() -> None:
                    acquired = plat._rec_cond.acquire(blocking=False)
                    if acquired:
                        plat._rec_cond.release()
                    res_box.append(acquired)

                pt = threading.Thread(target=_probe, daemon=True)
                pt.start()
                pt.join(timeout=1.0)
                return bool(res_box and res_box[0])

            def _counting_popen(*args: Any, **kwargs: Any) -> Any:
                # Verify _rec_cond is NEVER held across subprocess.Popen from a separate thread
                # (same-thread acquire on Condition's default RLock would re-enter and mask a held lock).
                lock_free_during_popen.append(_probe_rec_cond_unlocked_from_other_thread())
                p = orig_popen(*args, **kwargs)
                spawned_procs.append(p)
                popen_entered.set()
                release_popen.wait(timeout=3.0)
                return p

            def _run_start() -> None:
                try:
                    plat.start_recording(rt, seq_id=1)
                except BaseException as exc:
                    errs.append(exc)

            close_done = threading.Event()

            def _run_close() -> None:
                plat.close()
                close_done.set()

            with mock.patch("agent_voice_input.platform_common.subprocess.Popen", side_effect=_counting_popen):
                th = threading.Thread(target=_run_start, daemon=True)
                th.start()
                th_close = threading.Thread(target=_run_close, daemon=True)
                try:
                    self.assertTrue(popen_entered.wait(timeout=2.0))
                    # Launch close() while start_recording is parked inside Popen:
                    # close() must wait for the in-flight spawn to converge and kill any FFmpeg it creates!
                    th_close.start()
                    time.sleep(0.05)
                    self.assertFalse(close_done.is_set(), "close() must wait for in-flight spawn to converge")
                    release_popen.set()
                    self.assertTrue(close_done.wait(timeout=3.0), "close() must finish once spawn aborts")
                    # Assert BEFORE joining th: the instant close() returns, _spawning_recs is already empty
                    # and every spawned subprocess is already dead (no orphan window after close() returns).
                    with plat._rec_cond:
                        self.assertEqual(plat._spawning_recs, {}, "_spawning_recs must be empty when close() returns")
                    self.assertEqual(len(spawned_procs), 1)
                    for p in spawned_procs:
                        self.assertIsNotNone(p.poll(), "no orphan FFmpeg subprocess may survive when close() returns")
                    th.join(timeout=2.0)
                    self.assertFalse(th.is_alive())
                    self.assertEqual(errs, [])
                    self.assertTrue(plat._closed)
                    self.assertIsNone(plat._cur_session)
                    self.assertFalse(plat.is_recording)
                    self.assertEqual(lock_free_during_popen, [True], "_rec_cond must not be held during Popen")
                finally:
                    release_popen.set()
                    th_close.join(timeout=2.0)
                    th.join(timeout=2.0)
                    plat.close()

    def test_r2_p0_3_agy_worker_readers_close_pipes_after_stop_join_timeout(self) -> None:
        """P0-3: _AgyWorker stdout/stderr readers close streams in finally even if they exit after stop() join timeout."""
        from agent_voice_input.refiner import _AgyWorker

        in_r, in_w = os.pipe()
        out_r, out_w = os.pipe()
        err_r, err_w = os.pipe()
        stdin_f = os.fdopen(in_w, "w", encoding="utf-8", buffering=1)
        stdout_f = os.fdopen(out_r, "r", encoding="utf-8", buffering=1)
        stderr_f = os.fdopen(err_r, "r", encoding="utf-8", buffering=1)
        os.close(in_r)

        class _FakeDeadProc:
            def __init__(self) -> None:
                self.pid = 999999
                self.stdin = stdin_f
                self.stdout = stdout_f
                self.stderr = stderr_f

            def poll(self) -> int:
                return 0

            def wait(self, timeout: float | None = None) -> int:
                del timeout
                return 0

        proc = _FakeDeadProc()
        stopped_cb_seen: list[Any] = []
        worker = _AgyWorker(
            agy_bin="agy",
            workdir=Path("."),
            proc=proc,  # type: ignore[arg-type]
            on_stopped=lambda p: stopped_cb_seen.append(p),
        )
        try:
            # Keep out_w/err_w open so reader threads outlive stop()'s join timeout.
            with mock.patch("agent_voice_input.refiner._agy_stop_process_group", lambda _p, fast=False: None):
                worker.stop(fast=True)
            self.assertEqual(len(stopped_cb_seen), 1)
            self.assertTrue(stdin_f.closed)
            self.assertTrue(worker.t_out.is_alive())
            self.assertTrue(worker.t_err.is_alive())
            # Release writer ends after stop() returned -> readers exit and close their own streams!
            os.close(out_w)
            out_w = -1
            os.close(err_w)
            err_w = -1
            worker.t_out.join(timeout=2.0)
            worker.t_err.join(timeout=2.0)
            self.assertFalse(worker.t_out.is_alive())
            self.assertFalse(worker.t_err.is_alive())
            self.assertTrue(stdout_f.closed, "stdout must be closed by reader finally after late exit")
            self.assertTrue(stderr_f.closed, "stderr must be closed by reader finally after late exit")
        finally:
            if out_w >= 0:
                os.close(out_w)
            if err_w >= 0:
                os.close(err_w)
            worker.t_out.join(timeout=1.0)
            worker.t_err.join(timeout=1.0)
            for fh in (stdin_f, stdout_f, stderr_f):
                if not fh.closed:
                    fh.close()

    def test_r2_p1_1_unified_raw_fd_write_never_buffers_or_reorders_cleanup_vs_rpc(self) -> None:
        """P1-1: Fire-and-forget cleanup uses raw fd (never Python TextIOWrapper buffer) so RPC never overtakes it."""
        class _OrderBridge(JsonlShimMixin):
            _shim_graceful = False
            _shim_label = "order_test"

            def __init__(self) -> None:
                self.cfg = Config()
                self.env = {}
                self._init_shim_bridge()

            def _build_shim_cmd(self) -> list[str] | None:
                return None

        r_fd, w_fd = os.pipe()
        w_file = None
        try:
            os.set_blocking(w_fd, False)
            os.set_blocking(r_fd, False)
            filled = 0
            while True:
                try:
                    filled += os.write(w_fd, b"F" * 4096)
                except BlockingIOError:
                    break
            os.set_blocking(w_fd, True)
            w_file = os.fdopen(w_fd, "w", encoding="utf-8", buffering=1)
            w_fd = -1

            class _PipeProc:
                def __init__(self, stdin_obj: Any) -> None:
                    self.stdin = stdin_obj
                    self.stdout = None
                    self.stderr = None

                def poll(self) -> int | None:
                    return None

                def kill(self) -> None:
                    pass

                def wait(self, timeout: float | None = None) -> int:
                    del timeout
                    return 0

            bridge = _OrderBridge()
            proc = _PipeProc(w_file)
            bridge._shim = proc  # type: ignore[assignment]
            bridge._spawned_shim = proc  # type: ignore[assignment]
            bridge._spawned_ready = True
            bridge._ready.set()
            bridge._ensure_shim = lambda *, ignore_cooldown=False: True  # type: ignore[method-assign]

            # 1) On a full pipe, cleanup command times out cleanly with 'not_sent' (0 bytes written).
            status_full = bridge._write_shim_command(
                {"cmd": "rec_stop", "rec_gen": 1, "discard": True},
                timeout_sec=0.02,
            )
            self.assertEqual(status_full, "not_sent")

            # Drain filler bytes completely.
            drained = 0
            while drained < filled:
                drained += len(os.read(r_fd, 65536))

            # 2) Subsequent cleanup + RPC arrive in strict FIFO order on the pipe; full-sent RPC whose
            # reply is lost raises queue.Empty while keeping the shim healthy!
            self.assertEqual(
                bridge._write_shim_command({"cmd": "rec_release", "rec_gen": 1}, timeout_sec=0.1),
                "sent",
            )
            with self.assertRaises(queue.Empty):
                bridge._shim_rpc({"cmd": "rec_start", "rec_gen": 2}, timeout_sec=0.02, allow_retry=False)

            raw_out = os.read(r_fd, 65536).decode("utf-8")
            cmds = [json.loads(ln)["cmd"] for ln in raw_out.splitlines() if ln.strip()]
            self.assertEqual(cmds, ["rec_release", "rec_start"])

            # 3) Close & restart isolation: detaching old shim and attaching new shim ensures
            # late commands targeting the old writer NEVER land on the new shim's pipe!
            old_writer = bridge._current_shim_writer()
            self.assertIsNotNone(old_writer)
            assert old_writer is not None
            r2_fd, w2_fd = os.pipe()
            w2_file = os.fdopen(w2_fd, "w", encoding="utf-8", buffering=1)
            try:
                proc2 = _PipeProc(w2_file)
                with bridge._shim_lock:
                    bridge._shim = proc2  # type: ignore[assignment]
                    bridge._shim_writer = None
                    bridge._spawned_shim = proc2  # type: ignore[assignment]
                    bridge._spawned_ready = True
                old_writer.close(send_quit=False)
                self.assertEqual(
                    bridge._write_shim_command({"cmd": "stale_old"}, writer=old_writer, timeout_sec=0.05),
                    "not_sent",
                )
                self.assertEqual(
                    bridge._write_shim_command({"cmd": "fresh_new"}, timeout_sec=0.1),
                    "sent",
                )
                bridge.close()
                os.set_blocking(r2_fd, False)
                new_raw = os.read(r2_fd, 65536).decode("utf-8")
                new_cmds = [json.loads(ln)["cmd"] for ln in new_raw.splitlines() if ln.strip()]
                self.assertIn("fresh_new", new_cmds)
                self.assertNotIn("stale_old", new_cmds)
            finally:
                if not w2_file.closed:
                    w2_file.close()
                os.close(r2_fd)
        finally:
            bridge.close()
            if w_file is not None:
                try:
                    w_file.close()
                except Exception:
                    pass
            if w_fd >= 0:
                os.close(w_fd)
            os.close(r_fd)

    def test_r2_p1_2_control_cancel_stays_in_same_fifo_behind_rec_stop(self) -> None:
        """P1-2: control.sock cancel queued behind slow rec_start -> rec_stop never overtakes rec_stop."""
        from agent_voice_input.daemon import send_control_command

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            empty_proc = Path(td) / "empty_proc"
            empty_proc.mkdir(parents=True, exist_ok=True)
            cfg = Config(
                asr=AsrConfig(provider="dashscope"),
                refiner=RefinerConfig(provider="off", include_git_status=False),
                audio=AudioConfig(beep=False, save_recordings=False, tail_drain_sec=0.0),
                hotkey=HotkeyConfig(debounce_ms=0, scope="all"),
            )
            start_entered = threading.Event()
            release_start = threading.Event()
            asr_entered = threading.Event()
            release_asr = threading.Event()

            class _SlowStartPlat(_TorturePlatform):
                def start_recording(
                    self, runtime_dir: Path, *, loopback: bool = False, seq_id: int | None = None
                ) -> None:
                    start_entered.set()
                    release_start.wait(timeout=3.0)
                    super().start_recording(runtime_dir, loopback=loopback, seq_id=seq_id)

            class _HeldAsr(BaseAsrProvider):
                provider_name = "dashscope"

                def __init__(self) -> None:
                    pass

                def transcribe(
                    self,
                    wav_bytes: bytes,
                    context: AsrContext | None = None,
                    *,
                    is_cancelled: Callable[[], bool] | None = None,
                    cancel_token: CancelToken | None = None,
                ) -> AsrResult:
                    del wav_bytes, context
                    ev = threading.Event()
                    if cancel_token is not None:
                        cancel_token.bind_event(ev)
                    try:
                        asr_entered.set()
                        dl = time.monotonic() + 2.0
                        while time.monotonic() < dl and not release_asr.is_set() and not ev.is_set():
                            ev.wait(timeout=0.01)
                        if (cancel_token is not None and cancel_token.is_cancelled()) or (
                            is_cancelled is not None and is_cancelled()
                        ):
                            raise HttpTransportError("cancelled", retry_kind="never")
                        return AsrResult(text="hello", provider="dashscope", model="qwen", latency_ms=2)
                    finally:
                        if cancel_token is not None:
                            cancel_token.unbind_event(ev)

            plat = _SlowStartPlat(self.speech_wav)
            tracker = SessionTracker(env={"HOME": td}, runtime_dir=rt, proc_root=empty_proc)
            daemon = VoiceInputDaemon(
                cfg,
                env={"HOME": td, "DASHSCOPE_API_KEY": "sk-test-q"},
                runtime_dir=rt,
                platform=plat,
                asr_provider=_HeldAsr(),
                tracker=tracker,
                paste_settle_sec=0.0,
            )
            srv_th = threading.Thread(target=daemon.serve_forever, daemon=True)
            srv_th.start()
            try:
                dl = time.monotonic() + 3.0
                while time.monotonic() < dl and not daemon.sock_path.exists():
                    time.sleep(0.01)
                res_start: list[dict[str, Any]] = []
                res_stop: list[dict[str, Any]] = []
                res_cancel: list[dict[str, Any]] = []

                t1 = threading.Thread(
                    target=lambda: res_start.append(send_control_command(rt, {"cmd": "rec_start"}, timeout_sec=5.0)),
                    daemon=True,
                )
                t1.start()
                self.assertTrue(start_entered.wait(timeout=2.0))

                # Enqueue rec_stop first, then cancel behind it while rec_start is still executing!
                t2 = threading.Thread(
                    target=lambda: res_stop.append(send_control_command(rt, {"cmd": "rec_stop"}, timeout_sec=5.0)),
                    daemon=True,
                )
                t2.start()
                dl_q1 = time.monotonic() + 2.0
                while time.monotonic() < dl_q1:
                    with daemon._ctrl_trigger_cv:
                        if len(daemon._ctrl_trigger_q) >= 1:
                            break
                    time.sleep(0.005)
                t3 = threading.Thread(
                    target=lambda: res_cancel.append(send_control_command(rt, {"cmd": "cancel"}, timeout_sec=5.0)),
                    daemon=True,
                )
                t3.start()
                dl_q2 = time.monotonic() + 2.0
                while time.monotonic() < dl_q2:
                    with daemon._ctrl_trigger_cv:
                        if len(daemon._ctrl_trigger_q) >= 2:
                            break
                    time.sleep(0.005)

                release_start.set()
                for t in (t1, t2, t3):
                    t.join(timeout=3.0)
                self.assertTrue(res_start[0]["ok"])
                self.assertTrue(res_stop[0]["ok"])
                self.assertEqual(res_stop[0]["state"], "recognizing")
                self.assertTrue(res_cancel[0]["ok"])
                self.assertNotIn(
                    "cancelled_recording",
                    res_cancel[0],
                    f"cancel must not overtake rec_stop and drop active capture: {res_cancel[0]}",
                )
                self.assertIn(res_stop[0]["seq_id"], res_cancel[0].get("cancelled_ids") or [])
            finally:
                release_start.set()
                release_asr.set()
                daemon.close()
                srv_th.join(timeout=2.0)
                tracker.close()

    def test_r2_p1_3_shutdown_during_slow_rec_stop_and_rec_start_rejects_enqueue_and_reports_stopping(self) -> None:
        """P1-3: Shutdown racing with slow rec_stop or rec_start never enqueues into stopped pipeline or reports ok=True."""
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            empty_proc = Path(td) / "empty_proc"
            empty_proc.mkdir(parents=True, exist_ok=True)
            cfg = Config(
                asr=AsrConfig(provider="dashscope"),
                refiner=RefinerConfig(provider="off", include_git_status=False),
                audio=AudioConfig(beep=False, save_recordings=False, tail_drain_sec=0.0),
                hotkey=HotkeyConfig(debounce_ms=0, scope="all"),
            )
            stop_entered = threading.Event()
            release_stop = threading.Event()

            class _SlowStopPlat(_TorturePlatform):
                def stop_recording(self, *, drain: bool = True) -> tuple[bytes, str]:
                    stop_entered.set()
                    release_stop.wait(timeout=3.0)
                    return super().stop_recording(drain=drain)

            plat1 = _SlowStopPlat(self.speech_wav)
            tracker = SessionTracker(env={"HOME": td}, runtime_dir=rt, proc_root=empty_proc)
            d1 = VoiceInputDaemon(
                cfg,
                env={"HOME": td, "DASHSCOPE_API_KEY": "sk-test-q"},
                runtime_dir=rt,
                platform=plat1,
                asr_provider=_TaggingAsrProvider(),
                tracker=tracker,
                paste_settle_sec=0.0,
            )
            win = ForegroundWindow(hwnd=101, process="WindowsTerminal.exe", title="codex")
            try:
                self.assertTrue(d1.handle_trigger("ptt_start", win)["ok"])
                stop_out: list[dict[str, Any]] = []
                t_stop = threading.Thread(
                    target=lambda: stop_out.append(d1.handle_trigger("ptt_stop", win)),
                    daemon=True,
                )
                t_stop.start()
                self.assertTrue(stop_entered.wait(timeout=2.0))
                self.assertTrue(d1.handle_control({"cmd": "shutdown"})["ok"])
                release_stop.set()
                t_stop.join(timeout=2.0)
                self.assertEqual(len(stop_out), 1)
                self.assertFalse(stop_out[0]["ok"])
                self.assertEqual(stop_out[0].get("ignored"), "stopping")
                with d1._lock:
                    self.assertEqual(len(d1._utterance_queue), 0)
            finally:
                release_stop.set()
                d1.close()

            # Slow rec_start racing with shutdown
            start_entered = threading.Event()
            release_start = threading.Event()

            class _SlowStartPlat2(_TorturePlatform):
                def start_recording(
                    self, runtime_dir: Path, *, loopback: bool = False, seq_id: int | None = None
                ) -> None:
                    start_entered.set()
                    release_start.wait(timeout=3.0)
                    super().start_recording(runtime_dir, loopback=loopback, seq_id=seq_id)

            plat2 = _SlowStartPlat2(self.speech_wav)
            d2 = VoiceInputDaemon(
                cfg,
                env={"HOME": td, "DASHSCOPE_API_KEY": "sk-test-q"},
                runtime_dir=Path(td) / "rt2",
                platform=plat2,
                asr_provider=_TaggingAsrProvider(),
                tracker=tracker,
                paste_settle_sec=0.0,
            )
            try:
                start_out: list[dict[str, Any]] = []
                t_start = threading.Thread(
                    target=lambda: start_out.append(d2.handle_trigger("ptt_start", win)),
                    daemon=True,
                )
                t_start.start()
                self.assertTrue(start_entered.wait(timeout=2.0))
                self.assertTrue(d2.handle_control({"cmd": "shutdown"})["ok"])
                release_start.set()
                t_start.join(timeout=2.0)
                self.assertEqual(len(start_out), 1)
                self.assertFalse(start_out[0]["ok"])
                self.assertEqual(start_out[0].get("ignored"), "stopping")
                self.assertFalse(d2._recording_active)
            finally:
                release_start.set()
                d2.close()
                tracker.close()

    def test_r2_p1_4_mic_disconnect_then_immediate_stop_preserves_disconnect_reason_once(self) -> None:
        """P1-4: Immediate user ptt_stop during async disconnect cleanup preserves disconnect error & runs once."""
        from agent_voice_input.platform_macos import MacOSPlatform, _MacRecSession

        stop_entered = threading.Event()
        release_stop = threading.Event()
        lost_cb_count = [0]

        class _SlowDisconnectRec:
            def __init__(self, wav: bytes) -> None:
                self._wav = wav
                self.current_level = 0.0
                self.stop_calls = 0

            def start(self, *args: Any, **kwargs: Any) -> None:
                del args, kwargs

            def stop(self, *, drain_sec: float = 0.0) -> bytes:
                del drain_sec
                self.stop_calls += 1
                if self.stop_calls == 1:
                    stop_entered.set()
                    release_stop.wait(timeout=3.0)
                    return self._wav
                return b""

            def diagnostic_summary(self) -> str:
                return "ok"

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            empty_proc = Path(td) / "empty_proc"
            empty_proc.mkdir(parents=True, exist_ok=True)
            cfg = Config(
                asr=AsrConfig(provider="dashscope"),
                refiner=RefinerConfig(provider="off", include_git_status=False),
                audio=AudioConfig(beep=False, save_recordings=False, tail_drain_sec=0.0),
                hotkey=HotkeyConfig(debounce_ms=0, scope="all"),
            )
            plat = MacOSPlatform(
                cfg,
                env={"HOME": td},
                cache_dir=Path(td) / "cache",
                self_check_fn=lambda: {
                    "hal_ok": True,
                    "has_input": True,
                    "default_uid": "bt-mic",
                    "default_name": "BT Mic",
                    "alive_uids": ["bt-mic"],
                    "mic_auth": "authorized",
                },
            )
            fake_rec = _SlowDisconnectRec(self.speech_wav)
            plat._ff_rec = fake_rec  # type: ignore[assignment]
            plat.deliver_text = lambda text, seq_id, target_hwnd=0, **kw: (  # type: ignore[method-assign]
                kw["before_commit"]() if kw.get("before_commit") else True,
                PasteOutcome(True, False, "pasted", "Ghostty"),
            )[1]
            tracker = SessionTracker(env={"HOME": td}, runtime_dir=rt, proc_root=empty_proc)
            daemon = VoiceInputDaemon(
                cfg,
                env={"HOME": td, "DASHSCOPE_API_KEY": "sk-test-q"},
                runtime_dir=rt,
                platform=plat,
                asr_provider=_TaggingAsrProvider(),
                tracker=tracker,
                paste_settle_sec=0.0,
            )
            orig_lost = daemon._on_recorder_lost

            def _counting_lost(*, seq_id: int | None = None) -> None:
                lost_cb_count[0] += 1
                orig_lost(seq_id=seq_id)

            plat.set_recorder_lost_callback(_counting_lost)
            win = ForegroundWindow(hwnd=10, process="Ghostty", title="codex")
            # Pin foreground window resolution so macOS tests never spawn real osascript.
            plat.resolve_foreground_window = lambda w=None: w or win  # type: ignore[method-assign]
            plat.active_window = lambda: win  # type: ignore[method-assign]
            plat._run_osascript = (  # type: ignore[method-assign]
                lambda *a, **kw: (_ for _ in ()).throw(AssertionError("unexpected real osascript call"))
            )
            stop_waiting_busy = threading.Event()
            orig_wait_idle = plat._wait_session_idle_locked

            def _wrapped_wait_idle(session: Any, timeout_sec: float) -> bool:
                stop_waiting_busy.set()
                return orig_wait_idle(session, timeout_sec)

            plat._wait_session_idle_locked = _wrapped_wait_idle  # type: ignore[method-assign]
            try:
                self.assertTrue(daemon.handle_trigger("ptt_start", win)["ok"])
                # CoreAudio disconnects while recording is active
                plat._on_shim_internal_event(
                    {
                        "event": "audio_devices_changed",
                        "hal_ok": True,
                        "has_input": False,
                        "alive_uids": [],
                    }
                )
                self.assertTrue(stop_entered.wait(timeout=2.0))

                # User releases PTT immediately while async disconnect cleanup is inside rec.stop()
                stop_box: list[dict[str, Any]] = []
                t_stop = threading.Thread(
                    target=lambda: stop_box.append(daemon.handle_trigger("ptt_stop", win)),
                    daemon=True,
                )
                t_stop.start()
                self.assertTrue(
                    stop_waiting_busy.wait(timeout=2.0),
                    "ptt_stop must take over the session before releasing disconnect cleanup",
                )
                release_stop.set()
                t_stop.join(timeout=2.0)

                self.assertEqual(len(stop_box), 1)
                self.assertTrue(stop_box[0]["ok"])
                self.assertEqual(plat.last_audio_error, "coreaudio_device_disconnected")
                self.assertEqual(lost_cb_count[0], 0, "disconnect callback must not double-finalize")

                dl = time.monotonic() + 2.0
                while time.monotonic() < dl and daemon.state != "idle":
                    time.sleep(0.01)
                lr = daemon.last_result
                self.assertTrue(lr.get("capture_interrupted"))
                self.assertEqual(lr.get("audio_error"), "coreaudio_device_disconnected")
            finally:
                release_stop.set()
                daemon.close()
                tracker.close()

    def test_r2_p2_hud_full_queue_preserves_copied_toast_and_hide_clear(self) -> None:
        """P2-1 / ⑤: Single-writer HUD queue drops excess levels & stage updates while delivering copied toast & hide in order."""
        class _HudPipeBridge(JsonlShimMixin):
            _shim_graceful = False
            _shim_label = "hud_pipe_test"

            def __init__(self) -> None:
                self.cfg = Config()
                self.env = {}
                self._init_shim_bridge()

            def _build_shim_cmd(self) -> list[str] | None:
                return None

        r_fd, w_fd = os.pipe()
        w_file = None
        bridge = _HudPipeBridge()
        try:
            # Fill pipe so writer pauses on the first HUD item while callers flood levels & HUD states.
            os.set_blocking(w_fd, False)
            os.set_blocking(r_fd, False)
            filled = 0
            while True:
                try:
                    filled += os.write(w_fd, b"Q" * 4096)
                except BlockingIOError:
                    break
            os.set_blocking(w_fd, True)
            w_file = os.fdopen(w_fd, "w", encoding="utf-8", buffering=1)
            w_fd = -1

            class _HudPipeProc:
                def __init__(self, stdin_obj: Any) -> None:
                    self.stdin = stdin_obj
                    self.stdout = None
                    self.stderr = None

                def poll(self) -> int | None:
                    return None

                def kill(self) -> None:
                    pass

                def wait(self, timeout: float | None = None) -> int:
                    del timeout
                    return 0

            proc = _HudPipeProc(w_file)
            bridge._shim = proc  # type: ignore[assignment]
            bridge._spawned_shim = proc  # type: ignore[assignment]
            bridge._spawned_ready = True
            bridge._ready.set()
            bridge._ensure_shim = lambda *, ignore_cooldown=False: True  # type: ignore[method-assign]

            t0 = time.monotonic()
            # 1st HUD enters the writer and waits on the full pipe; enqueue critical copied toast,
            # flood 70 intermediate stage updates + 200 high-frequency hud_level pushes, then final hide.
            bridge.hud("recording")
            bridge.hud("copied", layer="toast", clear_main=False)
            for idx in range(70):
                bridge.hud("recognizing" if idx % 2 == 0 else "refining")
                bridge.hud_level((idx % 10) / 10.0)
                bridge.hud_level(((idx + 3) % 10) / 10.0)
                bridge.hud_level(((idx + 6) % 10) / 10.0)
            bridge.hud("hide")
            enqueue_dt = time.monotonic() - t0
            self.assertLess(enqueue_dt, 0.15, f"HUD + hud_level burst must never block callers ({enqueue_dt:.3f}s)")

            # Now drain the filler bytes so the shim writer flushes its FIFO cleanly before quit.
            drained = 0
            while drained < filled:
                drained += len(os.read(r_fd, 65536))
            bridge.close()

            raw_chunks: list[bytes] = []
            while True:
                try:
                    data = os.read(r_fd, 65536)
                    if not data:
                        break
                    raw_chunks.append(data)
                except BlockingIOError:
                    break
            msgs = [json.loads(ln) for ln in b"".join(raw_chunks).decode("utf-8").splitlines() if ln.strip()]
            hud_states = [m["state"] for m in msgs if m.get("cmd") == "hud"]
            lvl_cmds = [m for m in msgs if m.get("cmd") == "hud_level"]
            self.assertIn("copied", hud_states, "copied toast must not be evicted by stage updates")
            self.assertIn("hide", hud_states, "hide clear must be preserved")
            self.assertLess(hud_states.index("copied"), hud_states.index("hide"))
            self.assertLessEqual(len(hud_states), 66, "intermediate stage updates must stay bounded")
            self.assertLessEqual(len(lvl_cmds), 1, "high-frequency hud_level must coalesce into single slot")
        finally:
            bridge.close()
            if w_file is not None:
                try:
                    w_file.close()
                except Exception:
                    pass
            if w_fd >= 0:
                os.close(w_fd)
            os.close(r_fd)

    def test_r3_p1_1_wsl_start_timeout_discard_write_failure_reaps_wedged_shim(self) -> None:
        """R2 P1-1: WSL rec_start reply timeout whose discard rec_stop cannot be written must reap the wedged shim."""
        from agent_voice_input.platform_wsl import WslPlatform

        r_fd, w_fd = os.pipe()
        w_file = None
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            rt.mkdir(parents=True, exist_ok=True)
            cfg = Config()
            cfg.audio.wsl_source = "wasapi"
            plat = WslPlatform(cfg, env={"PATH": "/usr/bin"})
            respawn_calls = [0]
            try:
                os.set_blocking(w_fd, False)
                while True:
                    try:
                        os.write(w_fd, b"W" * 4096)
                    except BlockingIOError:
                        break
                os.set_blocking(w_fd, True)
                w_file = os.fdopen(w_fd, "w", encoding="utf-8", buffering=1)
                w_fd = -1

                class _FakeWedgedProc:
                    def __init__(self, stdin_obj: Any) -> None:
                        self.stdin = stdin_obj
                        self.stdout = None
                        self.killed = False

                    def poll(self) -> int | None:
                        return 1 if self.killed else None

                    def kill(self) -> None:
                        self.killed = True

                    def wait(self, timeout: float | None = None) -> int:
                        del timeout
                        return 1

                proc = _FakeWedgedProc(w_file)
                plat._shim = proc  # type: ignore[assignment]
                plat._spawned_shim = proc  # type: ignore[assignment]
                plat._spawned_ready = True
                plat._on_event = lambda _e: None
                plat._ready.set()
                plat._ensure_shim = lambda *, ignore_cooldown=False: True  # type: ignore[method-assign]
                plat._schedule_respawn_if_needed = (  # type: ignore[method-assign]
                    lambda: respawn_calls.__setitem__(0, respawn_calls[0] + 1)
                )

                def _reply_timeout_rpc(
                    payload: dict[str, Any],
                    timeout_sec: float = 5.0,
                    *,
                    allow_retry: bool = True,
                    target_writer: Any = None,
                ) -> dict[str, Any] | None:
                    del payload, timeout_sec, allow_retry, target_writer
                    # Simulate rec_start fully written, reply timed out (queue.Empty), while stdin pipe is now full
                    # so subsequent rec_stop(discard=True) times out with 'not_sent'.
                    raise queue.Empty()

                plat._shim_rpc = _reply_timeout_rpc  # type: ignore[method-assign]
                with self.assertRaises(queue.Empty):
                    plat.start_recording(rt)
                self.assertTrue(proc.killed, "wedged shim must be killed so host cannot keep recording")
                self.assertTrue(w_file.closed)
                self.assertIsNone(plat._shim)
                self.assertEqual(respawn_calls[0], 1, "reaping wedged current shim must schedule respawn")
            finally:
                plat.close()
                if w_file is not None and not w_file.closed:
                    w_file.close()
                if w_fd >= 0:
                    os.close(w_fd)
                os.close(r_fd)

    def test_r3_p1_2_macos_handoff_rejects_spawn_when_selected_uid_disconnects(self) -> None:
        """R2 P1-2: MacOSPlatform phase-2 verification rejects commit & kills FFmpeg if selected mic UID disconnects during spawn."""
        from agent_voice_input.platform_common import (
            FfmpegWavRecorder,
            MicrophoneUnavailableError,
            NoMicrophoneError,
        )
        from agent_voice_input.platform_macos import MacOSPlatform

        pcm_src = (
            "import os, sys, time\n"
            "os.write(1, b'\\x01\\x02' * 800)\n"
            "sys.stdout.flush()\n"
            "time.sleep(5.0)\n"
        )
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            rt.mkdir(parents=True, exist_ok=True)
            cfg = Config(
                audio=AudioConfig(beep=False, save_recordings=False, tail_drain_sec=0.0),
                hotkey=HotkeyConfig(debounce_ms=0, scope="all"),
            )

            for label, disconnect_event, expected_exc in (
                (
                    "selected_uid_vanished_other_mic_left",
                    {
                        "event": "audio_devices_changed",
                        "hal_ok": True,
                        "has_input": True,
                        "default_uid": "builtin-mic",
                        "alive_uids": ["builtin-mic"],
                    },
                    MicrophoneUnavailableError,
                ),
                (
                    "all_mics_disconnected",
                    {
                        "event": "audio_devices_changed",
                        "hal_ok": True,
                        "has_input": False,
                        "default_uid": "",
                        "alive_uids": [],
                    },
                    NoMicrophoneError,
                ),
            ):
                with self.subTest(case=label):
                    build_entered = threading.Event()
                    release_build = threading.Event()
                    spawned_procs: list[subprocess.Popen[bytes]] = []
                    lock_free_during_popen: list[bool] = []

                    rec = FfmpegWavRecorder(
                        cmd_factory=lambda _f, _s: [sys.executable, "-c", pcm_src]
                    )
                    orig_build = rec._build_cmd

                    def _paused_build(fmt: str, src: str) -> list[str]:
                        cmd = orig_build(fmt, src)
                        build_entered.set()
                        release_build.wait(timeout=3.0)
                        return cmd

                    rec._build_cmd = _paused_build  # type: ignore[method-assign]
                    plat = MacOSPlatform(
                        cfg,
                        env={"HOME": td},
                        cache_dir=Path(td) / f"cache-{label}",
                        self_check_fn=lambda: {
                            "hal_ok": True,
                            "has_input": True,
                            "default_uid": "bt-mic",
                            "default_name": "BT Mic",
                            "alive_uids": ["bt-mic", "builtin-mic"],
                            "mic_auth": "authorized",
                        },
                    )
                    plat._new_recorder = lambda r=rec: r  # type: ignore[method-assign]
                    exc_box: list[BaseException] = []
                    orig_popen = subprocess.Popen

                    def _run_start() -> None:
                        try:
                            plat.start_recording(rt)
                        except BaseException as exc:
                            exc_box.append(exc)

                    def _tracking_popen(*args: Any, **kwargs: Any) -> Any:
                        probe_ok: list[bool] = []

                        def _probe_lock() -> None:
                            acquired = plat._rec_cond.acquire(blocking=False)
                            if acquired:
                                plat._rec_cond.release()
                            probe_ok.append(acquired)

                        pt = threading.Thread(target=_probe_lock, daemon=True)
                        pt.start()
                        pt.join(timeout=1.0)
                        lock_free_during_popen.append(bool(probe_ok and probe_ok[0]))
                        p = orig_popen(*args, **kwargs)
                        spawned_procs.append(p)
                        return p

                    try:
                        with mock.patch("agent_voice_input.platform_common.subprocess.Popen", side_effect=_tracking_popen):
                            th = threading.Thread(target=_run_start, daemon=True)
                            th.start()
                            self.assertTrue(build_entered.wait(timeout=2.0))
                            plat._on_shim_internal_event(disconnect_event)
                            release_build.set()
                            th.join(timeout=3.0)
                        self.assertEqual(lock_free_during_popen, [True], "_rec_cond must not be held during Popen")
                        self.assertEqual(len(exc_box), 1)
                        self.assertIsInstance(exc_box[0], expected_exc)
                        self.assertEqual(plat.last_audio_error, "coreaudio_device_disconnected")
                        self.assertFalse(plat.is_recording)
                        self.assertIsNone(plat._cur_session)
                        self.assertIsNone(rec._proc)
                        for p in spawned_procs:
                            self.assertIsNotNone(p.poll(), "spawned FFmpeg must be terminated on disconnect abort")
                    finally:
                        release_build.set()
                        plat.close()

    def test_r4_1_wsl_rec_start_lock_timeout_unwritten_does_not_kill_healthy_shim(self) -> None:
        """R3-1 / ①: WSL rec_start timing out with 0 bytes written ('not_sent') must NOT send discard or kill healthy shim."""
        from agent_voice_input.platform_wsl import WslPlatform

        r_fd, w_fd = os.pipe()
        w_file = None
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            rt.mkdir(parents=True, exist_ok=True)
            cfg = Config()
            cfg.audio.wsl_source = "wasapi"
            plat = WslPlatform(cfg, env={"PATH": "/usr/bin"})
            try:
                os.set_blocking(w_fd, False)
                os.set_blocking(r_fd, False)
                filled = 0
                while True:
                    try:
                        filled += os.write(w_fd, b"P" * 4096)
                    except BlockingIOError:
                        break
                os.set_blocking(w_fd, True)
                w_file = os.fdopen(w_fd, "w", encoding="utf-8", buffering=1)
                w_fd = -1

                class _HealthyPasteProc:
                    def __init__(self, stdin_obj: Any) -> None:
                        self.stdin = stdin_obj
                        self.stdout = None
                        self.killed = False

                    def poll(self) -> int | None:
                        return 1 if self.killed else None

                    def kill(self) -> None:
                        self.killed = True

                    def wait(self, timeout: float | None = None) -> int:
                        del timeout
                        return 0

                proc = _HealthyPasteProc(w_file)
                plat._shim = proc  # type: ignore[assignment]
                plat._spawned_shim = proc  # type: ignore[assignment]
                plat._spawned_ready = True
                plat._on_event = lambda _e: None
                plat._ready.set()
                plat._ensure_shim = lambda *, ignore_cooldown=False: True  # type: ignore[method-assign]

                orig_rpc = plat._shim_rpc

                def _short_timeout_rpc(
                    payload: dict[str, Any],
                    timeout_sec: float = 5.0,
                    *,
                    allow_retry: bool = True,
                    target_writer: Any = None,
                ) -> dict[str, Any] | None:
                    return orig_rpc(
                        payload,
                        timeout_sec=0.04,
                        allow_retry=allow_retry,
                        target_writer=target_writer,
                    )

                plat._shim_rpc = _short_timeout_rpc  # type: ignore[method-assign]
                reap_calls = [0]
                orig_poison = plat._poison_and_reap_writer

                def _counting_poison(w: Any) -> None:
                    reap_calls[0] += 1
                    orig_poison(w)

                plat._poison_and_reap_writer = _counting_poison  # type: ignore[method-assign]
                with self.assertRaises(_ShimWriteTimeout) as ctx:
                    plat.start_recording(rt)
                self.assertEqual(ctx.exception.write_status, "not_sent")
                self.assertFalse(ctx.exception.partial)
                self.assertFalse(ctx.exception.written)
                self.assertEqual(reap_calls[0], 0, "unwritten rec_start must NOT reap healthy shim")
                self.assertFalse(proc.killed, "healthy shim must remain alive")
                writer = plat._current_shim_writer()
                self.assertIsNotNone(writer)
                assert writer is not None
                self.assertFalse(writer.poisoned)
                self.assertIs(plat._shim, proc)

                # Drain exact filler bytes and confirm zero rec_start/rec_stop bytes entered the pipe.
                drained = 0
                while drained < filled:
                    drained += len(os.read(r_fd, 65536))
                try:
                    extra = os.read(r_fd, 4096)
                except BlockingIOError:
                    extra = b""
                self.assertEqual(extra, b"", "no rec_start or discard rec_stop should be written on 0-byte timeout")
            finally:
                plat.close()
                if w_file is not None and not w_file.closed:
                    w_file.close()
                if w_fd >= 0:
                    os.close(w_fd)
                os.close(r_fd)

    def test_r4_2_macos_stale_probe_cannot_clobber_newer_disconnect_event_cache(self) -> None:
        """R3-2: Stale macOS audio probe returning after a newer disconnect event must not clobber cache or spawn."""
        from agent_voice_input.platform_common import FfmpegWavRecorder, MicrophoneUnavailableError
        from agent_voice_input.platform_macos import MacOSPlatform

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            rt.mkdir(parents=True, exist_ok=True)
            cfg = Config(
                audio=AudioConfig(beep=False, save_recordings=False, tail_drain_sec=0.0),
                hotkey=HotkeyConfig(debounce_ms=0, scope="all"),
            )
            probe_entered = threading.Event()
            release_probe = threading.Event()
            popen_called = [0]

            def _slow_stale_probe() -> dict[str, Any]:
                probe_entered.set()
                release_probe.wait(timeout=3.0)
                # Stale snapshot taken BEFORE bt-mic disconnected
                return {
                    "hal_ok": True,
                    "has_input": True,
                    "default_uid": "bt-mic",
                    "default_name": "BT Mic",
                    "alive_uids": ["bt-mic", "builtin-mic"],
                    "mic_auth": "authorized",
                }

            plat = MacOSPlatform(
                cfg,
                env={"HOME": td},
                cache_dir=Path(td) / "cache",
                self_check_fn=_slow_stale_probe,
            )
            plat._ff_rec = FfmpegWavRecorder(
                cmd_factory=lambda _f, _s: [sys.executable, "-c", "import time; time.sleep(5)"]
            )
            exc_box: list[BaseException] = []

            def _run_start() -> None:
                try:
                    plat.start_recording(rt)
                except BaseException as exc:
                    exc_box.append(exc)

            def _counting_popen(*args: Any, **kwargs: Any) -> Any:
                popen_called[0] += 1
                raise AssertionError(f"Popen must not be called when newer cache removed bt-mic: {args} {kwargs}")

            try:
                with mock.patch("agent_voice_input.platform_common.subprocess.Popen", side_effect=_counting_popen):
                    th = threading.Thread(target=_run_start, daemon=True)
                    th.start()
                    self.assertTrue(probe_entered.wait(timeout=2.0))
                    # Newer disconnect event arrives WHILE slow probe is still in flight
                    plat._on_shim_internal_event(
                        {
                            "event": "audio_devices_changed",
                            "hal_ok": True,
                            "has_input": True,
                            "default_uid": "builtin-mic",
                            "alive_uids": ["builtin-mic"],
                        }
                    )
                    ver_after_ev = plat._audio_event_ver
                    release_probe.set()
                    th.join(timeout=2.0)

                # Cache MUST retain the newer disconnect state, NOT be clobbered by the stale probe!
                with plat._audio_cache_lock:
                    self.assertEqual(plat._audio_event_ver, ver_after_ev)
                    self.assertEqual(plat._alive_input_uids, {"builtin-mic"})
                    self.assertEqual(plat._default_input_uid, "builtin-mic")
                self.assertEqual(popen_called[0], 0, "FFmpeg Popen must be rejected using uncorrupted newer cache")
                self.assertEqual(len(exc_box), 1)
                self.assertIsInstance(exc_box[0], MicrophoneUnavailableError)
                self.assertEqual(plat.last_audio_error, "coreaudio_device_disconnected")
                self.assertFalse(plat.is_recording)
                self.assertIsNone(plat._cur_session)
            finally:
                release_probe.set()
                plat.close()

    def test_r5_p1_1_unpaired_surrogate_and_writer_exception_never_hang_write_sync(self) -> None:
        """P1 #1: Lone UTF-16 surrogates serialize safely and unexpected writer exceptions always wake write_sync()."""
        r_fd, w_fd = os.pipe()
        w_file = None
        try:
            os.set_blocking(r_fd, False)
            w_file = os.fdopen(w_fd, "w", encoding="utf-8", buffering=1)
            w_fd = -1

            class _Proc:
                def __init__(self, stdin_obj: Any) -> None:
                    self.stdin = stdin_obj
                    self.stdout = None

                def poll(self) -> int | None:
                    return None

                def kill(self) -> None:
                    pass

                def wait(self, timeout: float | None = None) -> int:
                    del timeout
                    return 0

            # 1a. Real POSIX pipe + lone surrogate in both _dump_jsonl/direct write_sync must succeed without hanging
            writer = _ShimWriter(_Proc(w_file))  # type: ignore[arg-type]
            try:
                bad_line = json.dumps({"cmd": "paste", "text": "hello\ud800world"}, ensure_ascii=False) + "\n"
                st, rsn = writer.write_sync(bad_line, time.monotonic() + 1.0)
                self.assertEqual((st, rsn), ("sent", ""))
                raw = os.read(r_fd, 4096).decode("utf-8")
                parsed = json.loads(raw.strip())
                self.assertEqual(parsed["cmd"], "paste")
                self.assertEqual(parsed["text"], "hello?world")
            finally:
                writer.close(send_quit=False)

            # 1b. Unexpected exception inside writer thread MUST still finish active item and wake write_sync()
            class _BoomPipe:
                closed = False

                def write(self, _s: str) -> int:
                    raise RuntimeError("boom_inside_writer")

                def flush(self) -> None:
                    pass

                def close(self) -> None:
                    self.closed = True

            boom_pipe = _BoomPipe()
            writer2 = _ShimWriter(_Proc(boom_pipe))  # type: ignore[arg-type]
            try:
                res_box: list[tuple[str, str]] = []

                def _call_sync() -> None:
                    res_box.append(writer2.write_sync('{"cmd":"paste"}\n', time.monotonic() + 5.0))

                t = threading.Thread(target=_call_sync, daemon=True)
                t.start()
                t.join(timeout=1.0)
                self.assertFalse(t.is_alive(), "write_sync() hung after unexpected writer exception")
                self.assertEqual(res_box, [("not_sent", "broken")])
                self.assertTrue(boom_pipe.closed)
            finally:
                writer2.close(send_quit=False)
        finally:
            if w_file is not None and not w_file.closed:
                w_file.close()
            if w_fd >= 0:
                os.close(w_fd)
            os.close(r_fd)

    def test_r5_p1_2_concurrent_reap_closes_stdin_strictly_once_via_writer(self) -> None:
        """P1 #2: Concurrent _reap_owned_shim callers never bypass _ShimWriter to close proc.stdin directly."""
        class _ReapBridge(JsonlShimMixin):
            _shim_graceful = False
            _shim_label = "reap_race_test"

            def __init__(self) -> None:
                self.cfg = Config()
                self.env = {}
                self._init_shim_bridge()

            def _build_shim_cmd(self) -> list[str] | None:
                return None

        r_fd, w_fd = os.pipe()
        w_file = None
        bridge = _ReapBridge()
        try:
            # Fill pipe so writer thread stays active inside _write_line_to_pipe when two reapers arrive.
            os.set_blocking(w_fd, False)
            while True:
                try:
                    os.write(w_fd, b"X" * 4096)
                except BlockingIOError:
                    break
            os.set_blocking(w_fd, True)
            w_file = os.fdopen(w_fd, "w", encoding="utf-8", buffering=1)
            w_fd = -1

            close_events: list[str] = []

            class _TrackedStdin:
                def __init__(self, inner: Any) -> None:
                    self._inner = inner

                @property
                def closed(self) -> bool:
                    return bool(self._inner.closed)

                def fileno(self) -> int:
                    return int(self._inner.fileno())

                def write(self, s: str) -> int:
                    return int(self._inner.write(s))

                def flush(self) -> None:
                    self._inner.flush()

                def close(self) -> None:
                    close_events.append(threading.current_thread().name)
                    self._inner.close()

            class _Proc:
                def __init__(self, stdin_obj: Any) -> None:
                    self.stdin = stdin_obj
                    self.stdout = None

                def poll(self) -> int | None:
                    return 0

                def kill(self) -> None:
                    pass

                def wait(self, timeout: float | None = None) -> int:
                    del timeout
                    return 0

            tracked = _TrackedStdin(w_file)
            proc = _Proc(tracked)
            writer = _ShimWriter(proc)  # type: ignore[arg-type]
            with bridge._shim_lock:
                bridge._shim = proc  # type: ignore[assignment]
                bridge._shim_writer = writer
                bridge._live_writers[id(proc)] = writer

            # Keep writer busy polling full pipe when concurrent reaps fire
            writer.enqueue_async('{"cmd":"hud","state":"recording"}\n', deadline_sec=1.0, is_hud=True)
            time.sleep(0.02)

            barrier = threading.Barrier(2)

            def _reap_worker() -> None:
                barrier.wait(timeout=2.0)
                bridge._reap_owned_shim(proc)  # type: ignore[arg-type]

            t1 = threading.Thread(target=_reap_worker, name="reap-reader-eof", daemon=True)
            t2 = threading.Thread(target=_reap_worker, name="reap-ensure-shim", daemon=True)
            t1.start()
            t2.start()
            t1.join(timeout=2.0)
            t2.join(timeout=2.0)
            self.assertFalse(t1.is_alive())
            self.assertFalse(t2.is_alive())
            self.assertFalse(writer._thread.is_alive())
            self.assertEqual(
                close_events,
                ["avi-shim-writer"],
                f"proc.stdin.close() must happen exactly once inside writer thread, got {close_events}",
            )
            self.assertNotIn(id(proc), bridge._live_writers)
        finally:
            bridge.close()
            if w_file is not None and not w_file.closed:
                w_file.close()
            if w_fd >= 0:
                os.close(w_fd)
            os.close(r_fd)

    def test_r5_p1_3_wsl_rec_start_discard_and_release_pinned_to_same_shim_writer(self) -> None:
        """P1 #3: Dual-pipe shim swap between _ensure_shim and _shim_rpc never sends rec_start to B and discard/release to A."""
        from agent_voice_input.platform_wsl import WslPlatform

        def _make_pipe_proc() -> tuple[int, Any, Any]:
            r_fd, w_fd = os.pipe()
            os.set_blocking(r_fd, False)
            w_file = os.fdopen(w_fd, "w", encoding="utf-8", buffering=1)

            class _PipeShimProc:
                def __init__(self, stdin_obj: Any) -> None:
                    self.stdin = stdin_obj
                    self.stdout = None
                    self.killed = False

                def poll(self) -> int | None:
                    return 1 if self.killed else None

                def kill(self) -> None:
                    self.killed = True

                def wait(self, timeout: float | None = None) -> int:
                    del timeout
                    return 1

            return r_fd, w_file, _PipeShimProc(w_file)

        def _read_all_cmds(r_fd: int) -> list[dict[str, Any]]:
            chunks: list[bytes] = []
            while True:
                try:
                    data = os.read(r_fd, 65536)
                    if not data:
                        break
                    chunks.append(data)
                except BlockingIOError:
                    break
            return [json.loads(ln) for ln in b"".join(chunks).decode("utf-8").splitlines() if ln.strip()]

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            rt.mkdir(parents=True, exist_ok=True)
            cfg = Config()
            cfg.audio.wsl_source = "wasapi"
            cfg.audio.tail_drain_sec = 0.0
            plat = WslPlatform(cfg, env={"PATH": "/usr/bin"})
            ra_fd, wa_file, proc_a = _make_pipe_proc()
            rb_fd, wb_file, proc_b = _make_pipe_proc()
            try:
                plat._shim = proc_a  # type: ignore[assignment]
                plat._spawned_shim = proc_a  # type: ignore[assignment]
                plat._spawned_ready = True
                plat._ready.set()
                plat._ensure_shim = lambda *, ignore_cooldown=False: True  # type: ignore[method-assign]

                # Swap plat._shim -> proc_b right after start_recording pins writer_a!
                orig_cur_writer = plat._current_shim_writer
                swapped = [False]

                def _swap_after_pick() -> Any:
                    w = orig_cur_writer()
                    if not swapped[0]:
                        swapped[0] = True
                        with plat._shim_lock:
                            plat._shim = proc_b  # type: ignore[assignment]
                            plat._shim_writer = None
                            plat._spawned_shim = proc_b  # type: ignore[assignment]
                            plat._spawned_ready = True
                    return w

                orig_rpc = plat._shim_rpc

                def _fast_rpc(
                    payload: dict[str, Any],
                    timeout_sec: float = 5.0,
                    *,
                    allow_retry: bool = True,
                    target_writer: Any = None,
                ) -> dict[str, Any] | None:
                    return orig_rpc(
                        payload,
                        timeout_sec=0.05,
                        allow_retry=allow_retry,
                        target_writer=target_writer,
                    )

                plat._current_shim_writer = _swap_after_pick  # type: ignore[method-assign]
                plat._shim_rpc = _fast_rpc  # type: ignore[method-assign]
                with self.assertRaises(queue.Empty):
                    plat.start_recording(rt)

                # Wait briefly for discard flush on writer A, then inspect both pipes.
                time.sleep(0.05)
                cmds_a = _read_all_cmds(ra_fd)
                cmds_b = _read_all_cmds(rb_fd)
                self.assertEqual([c["cmd"] for c in cmds_a], ["rec_start", "rec_stop"])
                self.assertIs(cmds_a[1].get("discard"), True)
                self.assertEqual(cmds_a[0].get("rec_gen"), cmds_a[1].get("rec_gen"))
                self.assertEqual(cmds_b, [], f"New shim B must receive zero commands when A was pinned, got {cmds_b}")
                self.assertFalse(proc_b.killed)

                # 3b. stop_recording + rec_release must also stay pinned to the original recording writer A!
                plat._current_shim_writer = orig_cur_writer  # type: ignore[method-assign]
                writer_a = getattr(proc_a, "_shim_writer_obj")
                with plat._winmm_state_lock:
                    plat._winmm_gen = 9
                    plat._rec_mode = "winmm"
                    plat._rec_writer = writer_a

                def _reply_on_a(
                    payload: dict[str, Any],
                    timeout_sec: float = 5.0,
                    *,
                    allow_retry: bool = True,
                    target_writer: Any = None,
                ) -> dict[str, Any] | None:
                    self.assertIs(target_writer, writer_a)
                    return {"ok": True, "rec_gen": payload.get("rec_gen"), "wav_b64": ""}

                plat._shim_rpc = _reply_on_a  # type: ignore[method-assign]
                plat.stop_recording(drain=False)
                time.sleep(0.05)
                cmds_a2 = _read_all_cmds(ra_fd)
                cmds_b2 = _read_all_cmds(rb_fd)
                self.assertEqual([c["cmd"] for c in cmds_a2], ["rec_release"])
                self.assertEqual(cmds_a2[0].get("rec_gen"), 9)
                self.assertEqual(cmds_b2, [], f"rec_release must not leak to new shim B, got {cmds_b2}")
            finally:
                plat.close()
                for wf in (wa_file, wb_file):
                    if not wf.closed:
                        wf.close()
                os.close(ra_fd)
                os.close(rb_fd)

    def test_r5_p1_4_hud_never_blocks_during_slow_shim_respawn(self) -> None:
        """P1 #4 + R2 P1 #2: hud('hide') and hud_level() return immediately (<50ms) even while Popen() holds _shim_lock for 350ms+."""
        class _SlowRespawnBridge(JsonlShimMixin):
            _shim_graceful = False
            _shim_ready_timeout = 5.0
            _shim_label = "slow_respawn_test"

            def __init__(self) -> None:
                self.cfg = Config()
                self.env = {}
                self._init_shim_bridge()

            def _build_shim_cmd(self) -> list[str] | None:
                return ["/fake/slow-shim"]

        bridge = _SlowRespawnBridge()
        bridge._on_event = lambda _e: None
        popen_entered = threading.Event()
        allow_popen_finish = threading.Event()

        def _slow_popen(*_args: Any, **_kwargs: Any) -> Any:
            popen_entered.set()
            allow_popen_finish.wait(timeout=2.0)
            raise OSError("simulated slow popen abort")

        def _run_respawn() -> None:
            try:
                bridge._ensure_shim(ignore_cooldown=True)
            except OSError:
                pass

        respawn_th = threading.Thread(target=_run_respawn, daemon=True)
        try:
            with mock.patch("agent_voice_input.platform_common.subprocess.Popen", side_effect=_slow_popen):
                respawn_th.start()
                self.assertTrue(popen_entered.wait(timeout=1.0), "Popen() inside _shim_lock did not start")
                acquired = bridge._shim_lock.acquire(blocking=False)
                if acquired:
                    bridge._shim_lock.release()
                self.assertFalse(acquired, "_shim_lock must actually be held during Popen()")
                t0 = time.monotonic()
                self.assertIsNone(bridge._ready_shim_writer())
                bridge.hud("hide")
                bridge.hud("recording")
                bridge.hud_level(0.75)
                dt = time.monotonic() - t0
                self.assertLess(dt, 0.05, f"hud/hud_level blocked while Popen() held _shim_lock ({dt:.3f}s)")
                time.sleep(0.35)
                self.assertTrue(
                    respawn_th.is_alive(),
                    "Popen() should still be holding _shim_lock after 350ms while hud already returned",
                )
        finally:
            allow_popen_finish.set()
            respawn_th.join(timeout=2.0)
            bridge.close()

    def test_r6_p1_1_wsl_rec_stop_detaching_exited_shim_schedules_respawn(self) -> None:
        """R2 P1 #1: When WSL rec_stop detaches an exited shim before reader EOF, respawn still runs and restores hotkeys."""
        from agent_voice_input.platform_wsl import WslPlatform

        respawned_shim_code = (
            "import json, sys\n"
            "print(json.dumps({'event': 'ready'}), flush=True)\n"
            "print(json.dumps({'event': 'toggle', 'hwnd': 1, 'process': 'WindowsTerminal.exe', 'title': 'codex'}), flush=True)\n"
            "for ln in sys.stdin:\n"
            "    if not ln.strip(): continue\n"
            "    req = json.loads(ln)\n"
            "    if req.get('cmd') == 'quit': break\n"
            "    print(json.dumps({'type': 'reply', 'reply': req.get('cmd'), 'seq_id': req.get('seq_id', 0), 'ok': True}), flush=True)\n"
        )

        in_r, in_w = os.pipe()
        out_r, out_w = os.pipe()
        in_w_file = os.fdopen(in_w, "w", encoding="utf-8", buffering=1)
        out_r_file = os.fdopen(out_r, "r", encoding="utf-8")
        out_w_file = os.fdopen(out_w, "w", encoding="utf-8", buffering=1)

        class _ExitingShimProc:
            def __init__(self, stdin_obj: Any, stdout_obj: Any) -> None:
                self.stdin = stdin_obj
                self.stdout = stdout_obj
                self.exited = False

            def poll(self) -> int | None:
                return 1 if self.exited else None

            def kill(self) -> None:
                self.exited = True

            def wait(self, timeout: float | None = None) -> int:
                del timeout
                self.exited = True
                return 1

        cfg = Config()
        cfg.audio.wsl_source = "wasapi"
        cfg.audio.tail_drain_sec = 0.0
        spawn_count = [0]

        def _factory() -> list[str]:
            spawn_count[0] += 1
            return [sys.executable, "-u", "-c", respawned_shim_code]

        plat = WslPlatform(cfg, env={"PATH": "/usr/bin"}, shim_cmd_factory=_factory)
        plat._shim_respawn_delays = (0.01,)
        hotkey_events: list[dict[str, Any]] = []
        got_toggle_evt = threading.Event()

        def _on_ev(msg: dict[str, Any]) -> None:
            hotkey_events.append(msg)
            if msg.get("event") == "toggle":
                got_toggle_evt.set()

        reader_th: threading.Thread | None = None
        try:
            proc1 = _ExitingShimProc(in_w_file, out_r_file)
            writer1 = _ShimWriter(proc1, on_poison=plat._on_writer_poison)  # type: ignore[arg-type]
            plat._on_event = _on_ev
            plat._ensure_event_worker()
            with plat._shim_lock:
                plat._shim = proc1  # type: ignore[assignment]
                plat._shim_writer = writer1
                plat._live_writers[id(proc1)] = writer1
                plat._spawned_shim = proc1  # type: ignore[assignment]
                plat._spawned_ready = False
                plat._shim_ever_ready = True
                plat._ready.clear()
            reader_th = threading.Thread(target=plat._shim_reader, args=(proc1, None), daemon=True)
            setattr(proc1, "_shim_reader_th", reader_th)
            reader_th.start()

            out_w_file.write(json.dumps({"event": "ready"}) + "\n")
            out_w_file.flush()
            self.assertTrue(plat._ready.wait(timeout=2.0), "shim #1 reader did not process ready")

            with plat._winmm_state_lock:
                plat._winmm_gen = 3
                plat._rec_mode = "winmm"
                plat._rec_writer = writer1

            # Shim #1 exits BEFORE stdout reaches EOF in _shim_reader.
            proc1.exited = True
            with self.assertRaises(BrokenPipeError):
                plat.stop_recording(drain=False)

            # Confirm rec_stop won the race and detached self._shim before reader EOF ran.
            self.assertIsNot(plat._shim, proc1)
            self.assertTrue(reader_th.is_alive(), "reader should still be waiting on stdout before EOF")

            # Now deliver EOF to reader #1; reader sees self._shim is already detached and skips respawn.
            out_w_file.close()
            reader_th.join(timeout=2.0)
            self.assertFalse(reader_th.is_alive())

            # Respawn scheduled by _shim_rpc must launch shim #2 and restore global hotkey delivery.
            self.assertTrue(
                got_toggle_evt.wait(timeout=3.0),
                "respawn did not launch shim #2 and deliver hotkey event after rec_stop detached exited shim",
            )
            self.assertGreaterEqual(spawn_count[0], 1)
            self.assertTrue(plat.is_shim_ok())
        finally:
            if not out_w_file.closed:
                out_w_file.close()
            plat.close()
            if reader_th is not None and reader_th.is_alive():
                reader_th.join(timeout=1.0)
            for fobj in (in_w_file, out_r_file):
                if not fobj.closed:
                    fobj.close()
            os.close(in_r)

    def test_r5_p1_5_terminal_hud_frames_survive_650ms_pipe_backpressure(self) -> None:
        """P1 #5: Terminal HUD frames (copied, hide) survive 650ms pipe backpressure after recording and arrive intact."""
        class _BackpressureHudBridge(JsonlShimMixin):
            _shim_graceful = False
            _shim_label = "hud_bp_test"

            def __init__(self) -> None:
                self.cfg = Config()
                self.env = {}
                self._init_shim_bridge()

            def _build_shim_cmd(self) -> list[str] | None:
                return None

        r_fd, w_fd = os.pipe()
        w_file = None
        bridge = _BackpressureHudBridge()
        try:
            os.set_blocking(r_fd, False)
            w_file = os.fdopen(w_fd, "w", encoding="utf-8", buffering=1)
            w_fd = -1

            class _BpProc:
                def __init__(self, stdin_obj: Any) -> None:
                    self.stdin = stdin_obj
                    self.stdout = None

                def poll(self) -> int | None:
                    return None

                def kill(self) -> None:
                    pass

                def wait(self, timeout: float | None = None) -> int:
                    del timeout
                    return 0

            proc = _BpProc(w_file)
            bridge._shim = proc  # type: ignore[assignment]
            bridge._spawned_shim = proc  # type: ignore[assignment]
            bridge._spawned_ready = True
            bridge._ready.set()

            # 1) Deliver 'recording' first onto the empty pipe and confirm it arrived.
            bridge.hud("recording")
            rec_raw = b""
            dl_rec = time.monotonic() + 1.0
            while time.monotonic() < dl_rec and b"\n" not in rec_raw:
                try:
                    rec_raw += os.read(r_fd, 4096)
                except BlockingIOError:
                    time.sleep(0.005)
            first_msg = json.loads(rec_raw.decode("utf-8").strip())
            self.assertEqual(first_msg.get("state"), "recording")

            # 2) Fill pipe to create sustained 650ms backpressure, then enqueue terminal copied + hide frames.
            pipe_fd = w_file.fileno()
            os.set_blocking(pipe_fd, False)
            filled = 0
            while True:
                try:
                    filled += os.write(pipe_fd, b"B" * 4096)
                except BlockingIOError:
                    break
            os.set_blocking(pipe_fd, True)

            bridge.hud("copied", layer="toast", clear_main=False)
            bridge.hud("hide")
            time.sleep(0.65)

            # 3) Drain filler bytes and verify both copied and hide arrive cleanly before quit.
            buf = bytearray()
            while len(buf) < filled:
                try:
                    chunk = os.read(r_fd, 65536)
                    if not chunk:
                        break
                    buf.extend(chunk)
                except BlockingIOError:
                    time.sleep(0.005)
            bridge.close()

            while True:
                try:
                    data = os.read(r_fd, 65536)
                    if not data:
                        break
                    buf.extend(data)
                except BlockingIOError:
                    break
            tail_bytes = bytes(buf[filled:])
            msgs = [json.loads(ln) for ln in tail_bytes.decode("utf-8").splitlines() if ln.strip()]
            hud_states = [m["state"] for m in msgs if m.get("cmd") == "hud"]
            self.assertEqual(
                hud_states,
                ["copied", "hide"],
                f"copied and hide must survive 650ms backpressure in order, got {hud_states}",
            )
        finally:
            bridge.close()
            if w_file is not None and not w_file.closed:
                w_file.close()
            if w_fd >= 0:
                os.close(w_fd)
            os.close(r_fd)

    def test_r7_lockfree_ready_writer_nonblocking_popen_and_zero_terminal_hud_drops(self) -> None:
        """R3 fix: _ready_shim_writer() is lock-free on healthy shims—(1) hud('hide') returns <50ms during 350ms Popen() holding _shim_lock, and (2) terminal hud('copied')/hud('hide') never drop under concurrent hud_level() & _shim_lock contention."""
        class _R7Bridge(JsonlShimMixin):
            _shim_graceful = False
            _shim_ready_timeout = 5.0
            _shim_label = "r7_hud_lockfree_test"

            def __init__(self) -> None:
                self.cfg = Config()
                self.env = {}
                self._init_shim_bridge()

            def _build_shim_cmd(self) -> list[str] | None:
                return ["/fake/r7-shim"]

        # Part 1: _ensure_shim() holding _shim_lock inside Popen() for 350ms -> hud('hide') returns <50ms.
        slow_bridge = _R7Bridge()
        slow_bridge._on_event = lambda _e: None
        popen_entered = threading.Event()
        allow_popen_exit = threading.Event()

        def _slow_popen(*_args: Any, **_kwargs: Any) -> Any:
            popen_entered.set()
            allow_popen_exit.wait(timeout=2.0)
            raise OSError("simulated slow popen")

        def _spawn_worker() -> None:
            try:
                slow_bridge._ensure_shim(ignore_cooldown=True)
            except OSError:
                pass

        spawn_th = threading.Thread(target=_spawn_worker, daemon=True)
        try:
            with mock.patch("agent_voice_input.platform_common.subprocess.Popen", side_effect=_slow_popen):
                spawn_th.start()
                self.assertTrue(popen_entered.wait(timeout=1.0), "Popen() did not enter under _shim_lock")
                t0 = time.monotonic()
                slow_bridge.hud("hide")
                slow_bridge.hud_level(0.4)
                dt = time.monotonic() - t0
                self.assertLess(dt, 0.05, f"hud('hide') blocked during slow Popen() ({dt:.3f}s)")
                time.sleep(0.35)
                self.assertTrue(spawn_th.is_alive(), "Popen() should still hold _shim_lock after 350ms")
        finally:
            allow_popen_exit.set()
            spawn_th.join(timeout=2.0)
            slow_bridge.close()

        # Part 2: Healthy shim under heavy concurrent hud_level() + _shim_lock contention ->
        # 100% of terminal hud('copied') & hud('hide') frames are enqueued and delivered without drops.
        r_fd, w_fd = os.pipe()
        w_file = None
        r_file = None
        bridge = _R7Bridge()
        received: list[dict[str, Any]] = []
        reader_done = threading.Event()

        def _drain_pipe() -> None:
            assert r_file is not None
            try:
                for raw in r_file:
                    if raw.strip():
                        received.append(json.loads(raw))
            finally:
                reader_done.set()

        stop_storm = threading.Event()
        storm_threads: list[threading.Thread] = []
        try:
            r_file = os.fdopen(r_fd, "r", encoding="utf-8")
            r_fd = -1
            w_file = os.fdopen(w_fd, "w", encoding="utf-8", buffering=1)
            w_fd = -1
            drain_th = threading.Thread(target=_drain_pipe, daemon=True)
            drain_th.start()

            class _HealthyProc:
                def __init__(self, stdin_obj: Any) -> None:
                    self.stdin = stdin_obj
                    self.stdout = None

                def poll(self) -> int | None:
                    return None

                def kill(self) -> None:
                    pass

                def wait(self, timeout: float | None = None) -> int:
                    del timeout
                    return 0

            proc = _HealthyProc(w_file)
            writer = _ShimWriter(proc, on_poison=bridge._on_writer_poison)  # type: ignore[arg-type]
            with bridge._shim_lock:
                bridge._live_writers[id(proc)] = writer
                bridge._spawned_shim = proc  # type: ignore[assignment]
                bridge._spawned_ready = True
                bridge._shim_writer = writer
                bridge._shim = proc  # type: ignore[assignment]
            bridge._ready.set()

            def _level_storm() -> None:
                i = 0
                while not stop_storm.is_set():
                    bridge.hud_level((i % 10) / 10.0)
                    i += 1

            def _lock_hog() -> None:
                while not stop_storm.is_set():
                    with bridge._shim_lock:
                        time.sleep(0.002)

            with preemption_watchdog(timeout_sec=10.0, switch_interval=1e-6):
                for fn in (_level_storm, _level_storm, _lock_hog):
                    th = threading.Thread(target=fn, daemon=True)
                    th.start()
                    storm_threads.append(th)

                rounds = 30
                for idx in range(rounds):
                    bridge.hud("copied", text=f"copied-{idx}", layer="toast", clear_main=False)
                    bridge.hud("hide")
                    time.sleep(0.002)

                stop_storm.set()
                for th in storm_threads:
                    th.join(timeout=2.0)
                bridge.close()
                self.assertTrue(reader_done.wait(timeout=3.0), "pipe reader did not reach EOF after close()")
                drain_th.join(timeout=1.0)

            hud_msgs = [m for m in received if m.get("cmd") == "hud"]
            copied_texts = [m.get("text") for m in hud_msgs if m.get("state") == "copied"]
            hide_count = sum(1 for m in hud_msgs if m.get("state") == "hide")
            self.assertEqual(
                copied_texts,
                [f"copied-{i}" for i in range(rounds)],
                f"Expected all {rounds} copied frames in order without drops, got {len(copied_texts)}",
            )
            self.assertEqual(
                hide_count,
                rounds,
                f"Expected all {rounds} hide frames without drops, got {hide_count}",
            )
        finally:
            stop_storm.set()
            for th in storm_threads:
                if th.is_alive():
                    th.join(timeout=1.0)
            bridge.close()
            if w_file is not None and not w_file.closed:
                w_file.close()
            if r_file is not None and not r_file.closed:
                r_file.close()
            if w_fd >= 0:
                os.close(w_fd)
            if r_fd >= 0:
                os.close(r_fd)

    def test_stage3a_r2_p0_macos_close_aborts_pre_popen_and_15s_first_frame_wait(self) -> None:
        """Stage 3A P0: close() aborts both pre-Popen spawn and 15s first-frame wait with zero live orphans on return."""
        from agent_voice_input.platform_common import FfmpegWavRecorder
        from agent_voice_input.platform_macos import MacOSPlatform

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            rt.mkdir(parents=True, exist_ok=True)

            # Part 1: close() called while start_recording() is parked before Popen -> Popen is NEVER executed.
            build_entered = threading.Event()
            release_build = threading.Event()
            popen_calls_pre: list[int] = []
            orig_build = FfmpegWavRecorder._build_cmd
            orig_popen = subprocess.Popen

            def _paused_build(rec_self: FfmpegWavRecorder, fmt: str, src: str) -> list[str]:
                cmd = orig_build(rec_self, fmt, src)
                build_entered.set()
                release_build.wait(timeout=3.0)
                return cmd

            def _forbid_popen(*args: Any, **kwargs: Any) -> Any:
                popen_calls_pre.append(1)
                return orig_popen(*args, **kwargs)

            plat_pre = MacOSPlatform(
                Config(),
                env={"HOME": td},
                cache_dir=Path(td) / "cache_pre",
                self_check_fn=lambda: {
                    "hal_ok": True,
                    "has_input": True,
                    "default_uid": "mic-1",
                    "default_name": "Built-in Mic",
                    "alive_uids": ["mic-1"],
                    "mic_auth": "not_determined",
                    "has_graphic_session": True,
                },
            )
            plat_pre._ff_rec = FfmpegWavRecorder(
                cmd_factory=lambda _f, _s: [sys.executable, "-c", "import time; time.sleep(20.0)"]
            )
            pre_errs: list[BaseException] = []
            close_pre_done = threading.Event()

            def _start_pre() -> None:
                try:
                    plat_pre.start_recording(rt, seq_id=1)
                except BaseException as exc:
                    pre_errs.append(exc)

            def _close_pre() -> None:
                plat_pre.close()
                close_pre_done.set()

            with (
                mock.patch.object(FfmpegWavRecorder, "_build_cmd", _paused_build),
                mock.patch("agent_voice_input.platform_common.subprocess.Popen", side_effect=_forbid_popen),
            ):
                th_pre = threading.Thread(target=_start_pre, daemon=True)
                th_close_pre = threading.Thread(target=_close_pre, daemon=True)
                th_pre.start()
                try:
                    self.assertTrue(build_entered.wait(timeout=2.0))
                    th_close_pre.start()
                    time.sleep(0.05)
                    self.assertFalse(close_pre_done.is_set())
                    t_rel = time.monotonic()
                    release_build.set()
                    self.assertTrue(close_pre_done.wait(timeout=1.0), "close() must converge immediately after pre-Popen abort")
                    self.assertLess(time.monotonic() - t_rel, 1.0)
                    # Assert BEFORE joining th_pre: _spawning_recs is already empty and Popen was never called.
                    with plat_pre._rec_cond:
                        self.assertEqual(plat_pre._spawning_recs, {})
                    self.assertEqual(popen_calls_pre, [], "aborted recorder must never invoke Popen")
                    th_pre.join(timeout=2.0)
                    self.assertEqual(pre_errs, [])
                finally:
                    release_build.set()
                    th_close_pre.join(timeout=2.0)
                    th_pre.join(timeout=2.0)
                    plat_pre.close()

            # Part 2: 15s first-frame wait (not_determined mic_auth, child emits 0 frames) ->
            # close() aborts in <1s (never waits 2s or 15s) and child is ALREADY dead when close() returns!
            child_spawned = threading.Event()
            spawned_15s: list[subprocess.Popen[bytes]] = []

            def _capture_popen(*args: Any, **kwargs: Any) -> Any:
                p = orig_popen(*args, **kwargs)
                spawned_15s.append(p)
                child_spawned.set()
                return p

            plat_15s = MacOSPlatform(
                Config(),
                env={"HOME": td},
                cache_dir=Path(td) / "cache_15s",
                self_check_fn=lambda: {
                    "hal_ok": True,
                    "has_input": True,
                    "default_uid": "mic-1",
                    "default_name": "Built-in Mic",
                    "alive_uids": ["mic-1"],
                    "mic_auth": "not_determined",
                    "has_graphic_session": True,
                },
            )
            plat_15s._ff_rec = FfmpegWavRecorder(
                cmd_factory=lambda _f, _s: [sys.executable, "-c", "import time; time.sleep(20.0)"]
            )
            errs_15s: list[BaseException] = []

            def _start_15s() -> None:
                try:
                    plat_15s.start_recording(rt, seq_id=2)
                except BaseException as exc:
                    errs_15s.append(exc)

            with mock.patch("agent_voice_input.platform_common.subprocess.Popen", side_effect=_capture_popen):
                th_15s = threading.Thread(target=_start_15s, daemon=True)
                th_15s.start()
                try:
                    self.assertTrue(child_spawned.wait(timeout=2.0))
                    t0 = time.monotonic()
                    plat_15s.close()
                    dt = time.monotonic() - t0
                    self.assertLess(dt, 1.0, f"close() should abort 15s first-frame wait quickly, took {dt:.3f}s")
                    # Assert BEFORE joining th_15s: zero live orphans and empty _spawning_recs right as close() returns!
                    with plat_15s._rec_cond:
                        self.assertEqual(plat_15s._spawning_recs, {})
                    self.assertEqual(len(spawned_15s), 1)
                    self.assertIsNotNone(spawned_15s[0].poll(), "child must already be dead when close() returns")
                    th_15s.join(timeout=2.0)
                    self.assertFalse(th_15s.is_alive())
                    self.assertEqual(errs_15s, [])
                finally:
                    plat_15s.close()
                    th_15s.join(timeout=2.0)

    def test_stage3a_r2_p1_1_macos_concurrent_stop_and_new_start_zero_crosstalk(self) -> None:
        """Stage 3A P1 #1: Exclusive FfmpegWavRecorder per session eliminates concurrent stop/start crosstalk."""
        from agent_voice_input.platform_common import FfmpegWavRecorder
        from agent_voice_input.platform_macos import MacOSPlatform

        seg1_src = (
            "import os, sys, time\n"
            "os.write(1, b'\\x01\\x00' * 800)\n"
            "sys.stdout.flush()\n"
            "time.sleep(10.0)\n"
        )
        seg2_src = (
            "import os, sys, time\n"
            "os.write(1, b'\\x02\\x00' * 800)\n"
            "sys.stdout.flush()\n"
            "time.sleep(10.0)\n"
        )
        call_n = [0]
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            rt.mkdir(parents=True, exist_ok=True)
            plat = MacOSPlatform(
                Config(audio=AudioConfig(tail_drain_sec=0.0)),
                env={"HOME": td},
                cache_dir=Path(td) / "cache",
                self_check_fn=lambda: {
                    "hal_ok": True,
                    "has_input": True,
                    "default_uid": "mic-1",
                    "default_name": "Built-in Mic",
                    "alive_uids": ["mic-1"],
                    "mic_auth": "authorized",
                },
            )
            plat._ff_rec = FfmpegWavRecorder(
                cmd_factory=lambda _f, _s: (
                    call_n.__setitem__(0, call_n[0] + 1)
                    or [sys.executable, "-c", seg1_src if call_n[0] == 1 else seg2_src]
                )
            )
            stop_entered = threading.Event()
            release_stop = threading.Event()
            stop1_res: list[tuple[bytes, str]] = []
            try:
                plat.start_recording(rt, seq_id=1)
                sess1 = plat._cur_session
                self.assertIsNotNone(sess1)
                assert sess1 is not None
                rec1 = sess1.rec
                orig_rec1_stop = rec1.stop

                def _paused_stop1(*, drain_sec: float = 0.0) -> bytes:
                    stop_entered.set()
                    release_stop.wait(timeout=3.0)
                    return orig_rec1_stop(drain_sec=drain_sec)

                rec1.stop = _paused_stop1  # type: ignore[method-assign]

                def _run_stop1() -> None:
                    stop1_res.append(plat.stop_recording(drain=False))

                th_stop1 = threading.Thread(target=_run_stop1, daemon=True)
                th_stop1.start()
                self.assertTrue(stop_entered.wait(timeout=2.0))

                # Start session 2 while session 1's stop_recording() is still executing rec1.stop() outside _rec_cond.
                plat.start_recording(rt, seq_id=2)
                sess2 = plat._cur_session
                self.assertIsNotNone(sess2)
                assert sess2 is not None
                self.assertIsNot(sess2.rec, rec1, "new session must use a fresh FfmpegWavRecorder instance")
                proc2 = sess2.rec._proc
                self.assertIsNotNone(proc2)
                assert proc2 is not None

                release_stop.set()
                th_stop1.join(timeout=2.0)
                self.assertFalse(th_stop1.is_alive())
                self.assertEqual(len(stop1_res), 1)
                wav1, _ = stop1_res[0]
                self.assertEqual(wav1[44:], b"\x01\x00" * 800, "old stop must return only session 1 PCM")
                self.assertTrue(plat.is_recording, "old stop must never terminate session 2")
                self.assertIsNone(proc2.poll(), "session 2 subprocess must remain alive")

                wav2, _ = plat.stop_recording(drain=False)
                self.assertEqual(wav2[44:], b"\x02\x00" * 800, "new stop must return only session 2 PCM")
                self.assertIsNotNone(proc2.poll())
            finally:
                release_stop.set()
                plat.close()

    def test_stage3a_r2_p1_2_macos_normal_stop_during_mic_recovery_preserves_tail_audio(self) -> None:
        """Stage 3A P1 #2: Normal stop_recording() during microphone recovery preserves both pre-drop and recovered tail PCM."""
        from agent_voice_input.platform_common import FfmpegWavRecorder
        from agent_voice_input.platform_macos import MacOSPlatform

        seg1_src = (
            "import os, sys, time\n"
            "os.write(1, b'\\x11\\x22' * 800)\n"
            "sys.stdout.flush()\n"
            "time.sleep(0.04)\n"
            "sys.stderr.write('transient glitch\\n')\n"
            "sys.exit(251)\n"
        )
        seg2_src = (
            "import os, sys, time\n"
            "os.write(1, b'\\x33\\x44' * 800)\n"
            "sys.stdout.flush()\n"
            "time.sleep(10.0)\n"
        )
        call_n = [0]
        respawn_started = threading.Event()
        allow_respawn_return = threading.Event()
        orig_rec_start = FfmpegWavRecorder.start

        def _sync_start(rec_self: FfmpegWavRecorder, *a: Any, **kw: Any) -> None:
            orig_rec_start(rec_self, *a, **kw)
            if call_n[0] == 2:
                # Respawn FFmpeg has already delivered its first PCM frames and is about to return to platform_macos.
                respawn_started.set()
                allow_respawn_return.wait(timeout=3.0)

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            rt.mkdir(parents=True, exist_ok=True)
            plat = MacOSPlatform(
                Config(audio=AudioConfig(tail_drain_sec=0.0)),
                env={"HOME": td},
                cache_dir=Path(td) / "cache",
            )
            plat._ff_rec = FfmpegWavRecorder(
                cmd_factory=lambda _f, _s: (
                    call_n.__setitem__(0, call_n[0] + 1)
                    or [sys.executable, "-c", seg1_src if call_n[0] == 1 else seg2_src]
                )
            )
            plat._has_managed_shim = lambda: True  # type: ignore[method-assign]
            plat._shim_rpc = lambda payload, timeout_sec=0.8, **kw: (  # type: ignore[method-assign]
                {
                    "ok": True,
                    "reply": True,
                    "audio_status": {
                        "has_input": True,
                        "input_count": 1,
                        "default_uid": "bt-earbuds:input",
                        "default_name": "FreeClip",
                        "alive_uids": ["bt-earbuds:input"],
                    },
                }
                if payload.get("cmd") == "audio_status"
                else {"ok": True}
            )
            stop_box: list[tuple[bytes, str]] = []
            try:
                with mock.patch.object(FfmpegWavRecorder, "start", autospec=True, side_effect=_sync_start):
                    plat.start_recording(rt, seq_id=1)
                    self.assertTrue(respawn_started.wait(timeout=2.5), "respawn did not reach first-frame readiness")

                    def _do_stop() -> None:
                        stop_box.append(plat.stop_recording(drain=False))

                    th_stop = threading.Thread(target=_do_stop, daemon=True)
                    th_stop.start()
                    # Wait until stop_recording() has cleared _cur_session & set session.active=False, waiting on session.busy.
                    dl = time.monotonic() + 2.0
                    while time.monotonic() < dl:
                        with plat._rec_cond:
                            if plat._cur_session is None:
                                break
                        time.sleep(0.005)
                    allow_respawn_return.set()
                    th_stop.join(timeout=3.0)

                self.assertFalse(th_stop.is_alive())
                self.assertEqual(len(stop_box), 1)
                wav_out, backend = stop_box[0]
                self.assertEqual(backend, "avfoundation")
                self.assertEqual(plat.last_audio_error, "")
                self.assertFalse(plat._last_stop_recorder_lost)
                pcm_out = wav_out[44:]
                self.assertTrue(pcm_out.startswith(b"\x11\x22" * 800), "missing pre-drop head PCM")
                self.assertTrue(pcm_out.endswith(b"\x33\x44" * 800), "recovered tail PCM was dropped during normal stop")
                self.assertGreaterEqual(len(pcm_out), 3200)
                with plat._rec_cond:
                    self.assertEqual(plat._spawning_recs, {})
            finally:
                allow_respawn_return.set()
                plat.close()

    def test_stage3a_r2_p1_3_ffmpeg_start_reaps_child_when_thread_start_fails(self) -> None:
        """Stage 3A P1 #3: Any failure after Popen (e.g. watchdog Thread.start() error) immediately reaps the child."""
        from agent_voice_input.platform_common import FfmpegWavRecorder
        from agent_voice_input.platform_macos import MacOSPlatform

        pcm_src = (
            "import os, sys, time\n"
            "os.write(1, b'\\x01\\x02' * 800)\n"
            "sys.stdout.flush()\n"
            "time.sleep(20.0)\n"
        )
        orig_popen = subprocess.Popen
        orig_thread_start = threading.Thread.start

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            rt.mkdir(parents=True, exist_ok=True)
            spawned: list[subprocess.Popen[bytes]] = []

            def _track_popen(*args: Any, **kwargs: Any) -> Any:
                p = orig_popen(*args, **kwargs)
                spawned.append(p)
                return p

            # Subcase A: Watchdog Thread.start() fails after first frame arrives.
            def _fail_watchdog_start(thr_self: threading.Thread) -> None:
                target = getattr(thr_self, "_target", None)
                if getattr(target, "__name__", "") == "_watch_stall":
                    raise RuntimeError("can't start new thread (injected watchdog failure)")
                orig_thread_start(thr_self)

            plat = MacOSPlatform(
                Config(),
                env={"HOME": td},
                cache_dir=Path(td) / "cache",
                self_check_fn=lambda: {
                    "hal_ok": True,
                    "has_input": True,
                    "default_uid": "mic-1",
                    "default_name": "Built-in Mic",
                    "alive_uids": ["mic-1"],
                    "mic_auth": "authorized",
                },
            )
            plat._ff_rec = FfmpegWavRecorder(
                cmd_factory=lambda _f, _s: [sys.executable, "-c", pcm_src]
            )
            try:
                with (
                    mock.patch("agent_voice_input.platform_common.subprocess.Popen", side_effect=_track_popen),
                    mock.patch.object(threading.Thread, "start", _fail_watchdog_start),
                ):
                    with self.assertRaises(RuntimeError):
                        plat.start_recording(rt, seq_id=1)
                self.assertEqual(len(spawned), 1)
                self.assertIsNotNone(spawned[0].poll(), "child must be reaped immediately when watchdog Thread.start fails")
                self.assertFalse(plat.is_recording)
                with plat._rec_cond:
                    self.assertEqual(plat._spawning_recs, {})
            finally:
                plat.close()

            # Subcase B: Reader Thread.start() fails right after Popen -> child reaped & pipes closed cleanly.
            spawned_b: list[subprocess.Popen[bytes]] = []

            def _track_popen_b(*args: Any, **kwargs: Any) -> Any:
                p = orig_popen(*args, **kwargs)
                spawned_b.append(p)
                return p

            def _fail_reader_start(thr_self: threading.Thread) -> None:
                target = getattr(thr_self, "_target", None)
                if getattr(target, "__name__", "") == "_read_stdout":
                    raise RuntimeError("can't start new thread (injected reader failure)")
                orig_thread_start(thr_self)

            rec_b = FfmpegWavRecorder(cmd_factory=lambda _f, _s: [sys.executable, "-c", pcm_src])
            with (
                mock.patch("agent_voice_input.platform_common.subprocess.Popen", side_effect=_track_popen_b),
                mock.patch.object(threading.Thread, "start", _fail_reader_start),
            ):
                with self.assertRaises(RuntimeError):
                    rec_b.start("avfoundation", ":default", rt, {"HOME": td}, verify_startup=True)
            self.assertEqual(len(spawned_b), 1)
            self.assertIsNotNone(spawned_b[0].poll(), "child must be reaped immediately when reader Thread.start fails")
            self.assertIsNone(rec_b._proc)

    def test_stage3a_r2_p1_4_wsl_rec_stop_not_sent_preserves_session_for_cancel(self) -> None:
        """Stage 3A P1 #4: Zero-byte write timeout (not_sent) on WSL rec_stop preserves session so cancel_recording stops shim."""
        from agent_voice_input.platform_wsl import WslPlatform

        mock_shim_src = (
            "import json, sys\n"
            "log_path = sys.argv[1]\n"
            "print(json.dumps({'event': 'ready', 'ok': True, 'caps': ['wasapi_v2']}), flush=True)\n"
            "for raw in sys.stdin:\n"
            "    raw = raw.strip()\n"
            "    if not raw:\n"
            "        continue\n"
            "    msg = json.loads(raw)\n"
            "    with open(log_path, 'a', encoding='utf-8') as f:\n"
            "        f.write(json.dumps(msg) + '\\n')\n"
            "    cmd = msg.get('cmd')\n"
            "    sid = msg.get('seq_id', 0)\n"
            "    if cmd == 'quit':\n"
            "        break\n"
            "    if cmd == 'rec_start':\n"
            "        print(json.dumps({'reply': True, 'seq_id': sid, 'ok': True}), flush=True)\n"
            "    elif cmd == 'rec_stop':\n"
            "        print(json.dumps({'reply': True, 'seq_id': sid, 'ok': True, 'rec_gen': msg.get('rec_gen', 0), 'wav_b64': ''}), flush=True)\n"
        )
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cmd_log = tmp / "wsl_cmds.jsonl"
            rt = tmp / "rt"
            rt.mkdir(parents=True, exist_ok=True)
            wplat = WslPlatform(
                Config(audio=AudioConfig(wsl_source="wasapi", tail_drain_sec=0.0)),
                env={"HOME": td},
                shim_cmd_factory=lambda: [sys.executable, "-u", "-c", mock_shim_src, str(cmd_log)],
            )
            try:
                wplat.start_event_listener(lambda _e: None)
                wplat.start_recording(rt, seq_id=7)
                with wplat._winmm_state_lock:
                    orig_gen = wplat._winmm_gen
                    orig_writer = wplat._rec_writer
                    self.assertEqual(wplat._rec_mode, "winmm")
                self.assertIsNotNone(orig_writer)

                # Force zero-byte write timeout (not_sent) on real _ShimWriter by setting deadline to 0.0s.
                wplat._rec_stop_timeout_sec = 0.0
                with self.assertRaises(_ShimWriteTimeout) as ctx:
                    wplat.stop_recording(drain=False)
                self.assertEqual(ctx.exception.write_status, "not_sent")

                # Session state MUST remain intact pointing at the original writer & rec_gen!
                with wplat._winmm_state_lock:
                    self.assertEqual(wplat._rec_mode, "winmm")
                    self.assertEqual(wplat._winmm_gen, orig_gen)
                    self.assertIs(wplat._rec_writer, orig_writer)

                # Shim has NOT received any rec_stop yet (still recording).
                cmds_before = [
                    json.loads(ln)
                    for ln in cmd_log.read_text(encoding="utf-8").splitlines()
                    if ln.strip()
                ]
                self.assertEqual([c.get("cmd") for c in cmds_before], ["rec_start"])

                # Restore normal timeout and call cancel_recording() -> MUST send rec_stop(discard=True, rec_gen=orig_gen) to orig_writer!
                wplat._rec_stop_timeout_sec = 5.0
                wplat.cancel_recording()

                deadline = time.monotonic() + 2.0
                cmds_after: list[dict[str, Any]] = []
                while time.monotonic() < deadline:
                    cmds_after = [
                        json.loads(ln)
                        for ln in cmd_log.read_text(encoding="utf-8").splitlines()
                        if ln.strip()
                    ]
                    if any(c.get("cmd") == "rec_stop" for c in cmds_after):
                        break
                    time.sleep(0.02)
                stop_cmds = [c for c in cmds_after if c.get("cmd") == "rec_stop"]
                self.assertEqual(len(stop_cmds), 1, f"expected cancel_recording() to send rec_stop, got {cmds_after}")
                self.assertEqual(stop_cmds[0].get("rec_gen"), orig_gen)
                self.assertIs(stop_cmds[0].get("discard"), True)
            finally:
                wplat.close()

    def test_stage3a_r3_p0_macos_close_wakes_tail_drain_and_reaps_child(self) -> None:
        """Stage 3A R3 P0: close() immediately wakes in-flight tail_drain (<0.3s) with child dead on return, while normal stop keeps tail audio."""
        from agent_voice_input.platform_common import FfmpegWavRecorder
        from agent_voice_input.platform_macos import MacOSPlatform

        tail_src = (
            "import os, sys, time\n"
            "os.write(1, b'\\x01\\x00' * 400)\n"
            "time.sleep(0.10)\n"
            "os.write(1, b'\\x02\\x00' * 400)\n"
            "time.sleep(10.0)\n"
        )
        ignore_term_src = (
            "import os, signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "os.write(1, b'\\x03\\x00' * 400)\n"
            "time.sleep(20.0)\n"
        )
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            rt.mkdir(parents=True, exist_ok=True)
            mic_ok = lambda: {
                "hal_ok": True,
                "has_input": True,
                "default_uid": "mic-1",
                "default_name": "Built-in Mic",
                "alive_uids": ["mic-1"],
                "mic_auth": "authorized",
            }

            # Subcase A: normal stop(drain=True) must NOT skip legitimate tail drain.
            plat_norm = MacOSPlatform(
                Config(audio=AudioConfig(tail_drain_sec=0.22)),
                env={"HOME": td},
                cache_dir=Path(td) / "cache_norm",
                self_check_fn=mic_ok,
            )
            plat_norm._ff_rec = FfmpegWavRecorder(
                cmd_factory=lambda _f, _s: [sys.executable, "-c", tail_src]
            )
            try:
                plat_norm.start_recording(rt, seq_id=1)
                t_norm = time.monotonic()
                wav_norm, _ = plat_norm.stop_recording(drain=True)
                dt_norm = time.monotonic() - t_norm
                self.assertGreaterEqual(dt_norm, 0.16, f"normal stop skipped tail drain ({dt_norm:.3f}s)")
                pcm_norm = wav_norm[44:]
                self.assertIn(b"\x01\x00" * 400, pcm_norm)
                self.assertIn(b"\x02\x00" * 400, pcm_norm)
            finally:
                plat_norm.close()

            # Subcase B: concurrent close() during 0.45s tail_drain wakes immediately (<0.3s) and child is already dead.
            plat = MacOSPlatform(
                Config(audio=AudioConfig(tail_drain_sec=0.45)),
                env={"HOME": td},
                cache_dir=Path(td) / "cache_close",
                self_check_fn=mic_ok,
            )
            plat._ff_rec = FfmpegWavRecorder(
                cmd_factory=lambda _f, _s: [sys.executable, "-c", ignore_term_src]
            )
            stop_out: list[tuple[bytes, str]] = []
            try:
                plat.start_recording(rt, seq_id=2)
                sess = plat._cur_session
                self.assertIsNotNone(sess)
                assert sess is not None
                child_proc = sess.rec._proc
                self.assertIsNotNone(child_proc)
                assert child_proc is not None
                self.assertIsNone(child_proc.poll())

                def _run_stop() -> None:
                    stop_out.append(plat.stop_recording(drain=True))

                th_stop = threading.Thread(target=_run_stop, daemon=True)
                th_stop.start()
                dl_wait = time.monotonic() + 2.0
                while time.monotonic() < dl_wait:
                    with plat._rec_cond:
                        if plat._cur_session is None and plat._stopping_recs:
                            break
                    time.sleep(0.005)
                with plat._rec_cond:
                    self.assertTrue(plat._stopping_recs, "stop_recording did not enter tracked tail_drain")

                t0 = time.monotonic()
                plat.close()
                dt_close = time.monotonic() - t0

                # Assert immediately on close() return (before joining th_stop): <0.3s and child already dead.
                self.assertLess(dt_close, 0.3, f"close() blocked on 0.45s tail_drain ({dt_close:.3f}s)")
                self.assertIsNotNone(child_proc.poll(), "FFmpeg child must already be dead when close() returns")
                with plat._rec_cond:
                    self.assertEqual(plat._stopping_recs, {})
                    self.assertEqual(plat._spawning_recs, {})

                th_stop.join(timeout=2.0)
                self.assertFalse(th_stop.is_alive())
                self.assertEqual(len(stop_out), 1)
            finally:
                plat.close()

    def test_stage3a_r3_p1_2_macos_stop_before_commit_preserves_initial_pcm(self) -> None:
        """Stage 3A R3 P1 #2: Normal stop_recording() after first 8192B PCM arrives but before _cur_session commits preserves audio."""
        from agent_voice_input.platform_common import FfmpegWavRecorder
        from agent_voice_input.platform_macos import MacOSPlatform

        pcm_payload = b"\x5a\xa5" * 4096
        assert len(pcm_payload) == 8192
        pcm_src = (
            "import os, sys, time\n"
            "os.write(1, b'\\x5a\\xa5' * 4096)\n"
            "sys.stdout.flush()\n"
            "time.sleep(20.0)\n"
        )
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            rt.mkdir(parents=True, exist_ok=True)
            plat = MacOSPlatform(
                Config(audio=AudioConfig(tail_drain_sec=0.0)),
                env={"HOME": td},
                cache_dir=Path(td) / "cache",
                self_check_fn=lambda: {
                    "hal_ok": True,
                    "has_input": True,
                    "default_uid": "mic-1",
                    "default_name": "Built-in Mic",
                    "alive_uids": ["mic-1"],
                    "mic_auth": "authorized",
                },
            )
            plat._ff_rec = FfmpegWavRecorder(
                cmd_factory=lambda _f, _s: [sys.executable, "-c", pcm_src]
            )
            orig_check = plat._check_selected_input_disconnected
            check_calls = [0]
            phase2_entered = threading.Event()
            release_phase2 = threading.Event()
            spawned_procs: list[subprocess.Popen[bytes]] = []
            orig_popen = subprocess.Popen

            def _track_popen(*args: Any, **kwargs: Any) -> Any:
                p = orig_popen(*args, **kwargs)
                spawned_procs.append(p)
                return p

            def _paused_check(**kw: Any) -> tuple[bool, bool, int]:
                check_calls[0] += 1
                res = orig_check(**kw)
                if check_calls[0] == 2:
                    # First 8192B PCM has already been read by FfmpegWavRecorder.start(); pause right before _cur_session commit.
                    phase2_entered.set()
                    release_phase2.wait(timeout=3.0)
                return res

            start_errs: list[BaseException] = []
            stop_box: list[tuple[bytes, str]] = []

            def _run_start() -> None:
                try:
                    plat.start_recording(rt, seq_id=1)
                except BaseException as exc:
                    start_errs.append(exc)

            def _run_stop() -> None:
                stop_box.append(plat.stop_recording(drain=False))

            try:
                plat._check_selected_input_disconnected = _paused_check  # type: ignore[method-assign]
                with mock.patch("agent_voice_input.platform_common.subprocess.Popen", side_effect=_track_popen):
                    th_start = threading.Thread(target=_run_start, daemon=True)
                    th_start.start()
                    self.assertTrue(phase2_entered.wait(timeout=2.0), "start_recording did not reach pre-commit Phase 2")
                    with plat._rec_cond:
                        self.assertIsNone(plat._cur_session)
                        orig_gen = plat._rec_gen
                        self.assertIn(orig_gen, plat._spawning_recs)

                    th_stop = threading.Thread(target=_run_stop, daemon=True)
                    th_stop.start()
                    dl = time.monotonic() + 2.0
                    while time.monotonic() < dl:
                        with plat._rec_cond:
                            if plat._pending_stop_gen == orig_gen:
                                break
                        time.sleep(0.005)
                    with plat._rec_cond:
                        self.assertEqual(plat._pending_stop_gen, orig_gen)
                        self.assertEqual(plat._rec_gen, orig_gen, "normal stop must wait instead of bumping _rec_gen")

                    release_phase2.set()
                    th_start.join(timeout=3.0)
                    th_stop.join(timeout=3.0)

                self.assertFalse(th_start.is_alive())
                self.assertFalse(th_stop.is_alive())
                self.assertEqual(start_errs, [])
                self.assertEqual(len(stop_box), 1)
                wav_out, backend = stop_box[0]
                self.assertEqual(backend, "avfoundation")
                self.assertEqual(plat.last_audio_error, "")
                self.assertGreaterEqual(len(wav_out), 44 + 8192)
                self.assertEqual(wav_out[44 : 44 + 8192], pcm_payload, "initial 8192B PCM was lost before commit")
                self.assertEqual(len(spawned_procs), 1)
                self.assertIsNotNone(spawned_procs[0].poll())
                with plat._rec_cond:
                    self.assertIsNone(plat._cur_session)
                    self.assertEqual(plat._spawning_recs, {})
                    self.assertEqual(plat._stopping_recs, {})
            finally:
                release_phase2.set()
                plat.close()

    def test_stage3a_r3_p1_3_wsl_cancel_not_sent_backpressure_delivers_rec_stop(self) -> None:
        """Stage 3A R4: WSL cancel_recording() under sustained not_sent backpressure directly poisons & reaps orig_writer and respawns a clean shim."""
        from agent_voice_input.platform_wsl import WslPlatform

        class _FastRespawnWslPlatform(WslPlatform):
            _shim_respawn_delays = (0.02, 0.05)

        mock_shim_src = (
            "import json, os, sys, time\n"
            "log_path, gate_path = sys.argv[1], sys.argv[2]\n"
            "print(json.dumps({'event': 'ready', 'ok': True, 'caps': ['wasapi_v2']}), flush=True)\n"
            "for raw in sys.stdin:\n"
            "    raw = raw.strip()\n"
            "    if not raw:\n"
            "        continue\n"
            "    msg = json.loads(raw)\n"
            "    with open(log_path, 'a', encoding='utf-8') as f:\n"
            "        f.write(json.dumps(msg) + '\\n')\n"
            "    cmd = msg.get('cmd')\n"
            "    sid = msg.get('seq_id', 0)\n"
            "    if cmd == 'quit':\n"
            "        break\n"
            "    if cmd == 'ping':\n"
            "        print(json.dumps({'reply': True, 'seq_id': sid, 'ok': True}), flush=True)\n"
            "    elif cmd == 'rec_start':\n"
            "        print(json.dumps({'reply': True, 'seq_id': sid, 'ok': True}), flush=True)\n"
            "        while not os.path.exists(gate_path):\n"
            "            time.sleep(0.01)\n"
            "    elif cmd == 'rec_stop':\n"
            "        print(json.dumps({'reply': True, 'seq_id': sid, 'ok': True, 'rec_gen': msg.get('rec_gen', 0), 'wav_b64': ''}), flush=True)\n"
        )
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cmd_log = tmp / "wsl_bp_cmds.jsonl"
            gate_file = tmp / "drain.gate"
            rt = tmp / "rt"
            rt.mkdir(parents=True, exist_ok=True)
            wplat = _FastRespawnWslPlatform(
                Config(audio=AudioConfig(wsl_source="wasapi", tail_drain_sec=0.0)),
                env={"HOME": td},
                shim_cmd_factory=lambda: [sys.executable, "-u", "-c", mock_shim_src, str(cmd_log), str(gate_file)],
            )
            try:
                wplat.start_event_listener(lambda _e: None)
                wplat.start_recording(rt, seq_id=9)
                with wplat._winmm_state_lock:
                    orig_gen = wplat._winmm_gen
                    orig_writer = wplat._rec_writer
                    self.assertEqual(wplat._rec_mode, "winmm")
                self.assertIsNotNone(orig_writer)
                assert orig_writer is not None
                orig_proc = orig_writer.proc
                self.assertIsNotNone(orig_proc.stdin)
                assert orig_proc.stdin is not None

                # Create real OS pipe backpressure while the child is wedged on gate_file.
                pipe_fd = orig_proc.stdin.fileno()
                os.set_blocking(pipe_fd, False)
                filled = 0
                while True:
                    try:
                        filled += os.write(pipe_fd, b"\n" * 4096)
                    except BlockingIOError:
                        break
                self.assertGreater(filled, 0, "failed to fill real stdin pipe for backpressure test")

                # Short timeout forces real _ShimWriteTimeout(write_status='not_sent') inside cancel_recording().
                wplat._rec_stop_timeout_sec = 0.08
                with mock.patch.object(wplat, "_enqueue_shim_async", wraps=wplat._enqueue_shim_async) as spy_async:
                    wplat.cancel_recording()
                    self.assertEqual(
                        spy_async.call_count,
                        0,
                        "cancel_recording() must not enqueue async rec_stop into a wedged pipe",
                    )

                with wplat._winmm_state_lock:
                    self.assertEqual(wplat._rec_mode, "")
                    self.assertIsNone(wplat._rec_writer)
                    self.assertGreater(wplat._winmm_gen, orig_gen)
                self.assertIsNotNone(
                    orig_proc.poll(),
                    "wedged shim must be immediately reaped on not_sent backpressure to guarantee Windows recording stops",
                )
                self.assertIsNot(wplat._shim, orig_proc)

                # Background respawn replaces the reaped shim with a fresh responsive process.
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    if wplat._shim is not None and wplat._shim is not orig_proc and wplat._shim.poll() is None:
                        break
                    time.sleep(0.02)
                self.assertIsNotNone(wplat._shim)
                self.assertIsNot(wplat._shim, orig_proc)
                ping_res = wplat._shim_rpc({"cmd": "ping"}, timeout_sec=1.5)
                self.assertIsNotNone(ping_res)
                assert ping_res is not None
                self.assertTrue(ping_res.get("ok"))
            finally:
                gate_file.write_text("go", encoding="utf-8")
                wplat.close()

    def test_stage3a_r4_wsl_cancel_not_sent_does_not_clobber_gen2_on_new_shim(self) -> None:
        """Stage 3A R4: Gen 1 cancel_recording() hitting not_sent must never clear or kill gen 2 started on a new shim."""
        from agent_voice_input.platform_wsl import WslPlatform

        mock_shim_src = (
            "import json, os, sys, time\n"
            "log_path, gate_path, tag = sys.argv[1], sys.argv[2], sys.argv[3]\n"
            "print(json.dumps({'event': 'ready', 'ok': True, 'caps': ['wasapi_v2']}), flush=True)\n"
            "for raw in sys.stdin:\n"
            "    raw = raw.strip()\n"
            "    if not raw:\n"
            "        continue\n"
            "    msg = json.loads(raw)\n"
            "    msg['_tag'] = tag\n"
            "    with open(log_path, 'a', encoding='utf-8') as f:\n"
            "        f.write(json.dumps(msg) + '\\n')\n"
            "    cmd = msg.get('cmd')\n"
            "    sid = msg.get('seq_id', 0)\n"
            "    if cmd == 'quit':\n"
            "        break\n"
            "    if cmd == 'rec_start':\n"
            "        print(json.dumps({'reply': True, 'seq_id': sid, 'ok': True}), flush=True)\n"
            "        if tag == 'shim1':\n"
            "            while not os.path.exists(gate_path):\n"
            "                time.sleep(0.01)\n"
            "    elif cmd == 'rec_stop':\n"
            "        print(json.dumps({'reply': True, 'seq_id': sid, 'ok': True, 'rec_gen': msg.get('rec_gen', 0), 'wav_b64': ''}), flush=True)\n"
        )
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cmd_log = tmp / "wsl_gen2_cmds.jsonl"
            gate_file = tmp / "shim1.gate"
            rt = tmp / "rt"
            rt.mkdir(parents=True, exist_ok=True)
            spawn_n = [0]

            def _factory() -> list[str]:
                spawn_n[0] += 1
                tag = f"shim{spawn_n[0]}"
                return [sys.executable, "-u", "-c", mock_shim_src, str(cmd_log), str(gate_file), tag]

            wplat = WslPlatform(
                Config(audio=AudioConfig(wsl_source="wasapi", tail_drain_sec=0.0)),
                env={"HOME": td},
                shim_cmd_factory=_factory,
            )
            gen1_wto_ready = threading.Event()
            release_gen1_wto = threading.Event()
            try:
                wplat.start_event_listener(lambda _e: None)
                wplat.start_recording(rt, seq_id=1)
                with wplat._winmm_state_lock:
                    gen1 = wplat._winmm_gen
                    writer1 = wplat._rec_writer
                    self.assertEqual(wplat._rec_mode, "winmm")
                self.assertIsNotNone(writer1)
                assert writer1 is not None
                proc1 = writer1.proc
                self.assertIsNotNone(proc1.stdin)
                assert proc1.stdin is not None

                # Wedge shim1's real stdin pipe so gen1 rec_stop hits genuine _ShimWriteTimeout('not_sent').
                pipe_fd = proc1.stdin.fileno()
                os.set_blocking(pipe_fd, False)
                while True:
                    try:
                        os.write(pipe_fd, b"\n" * 4096)
                    except BlockingIOError:
                        break

                orig_shim_rpc = wplat._shim_rpc

                def _paused_rpc(req: dict[str, Any], **kw: Any) -> dict[str, Any] | None:
                    if req.get("cmd") == "rec_stop" and kw.get("target_writer") is writer1:
                        kw["timeout_sec"] = 0.06
                        try:
                            return orig_shim_rpc(req, **kw)
                        except _ShimWriteTimeout:
                            gen1_wto_ready.set()
                            release_gen1_wto.wait(timeout=3.0)
                            raise
                    return orig_shim_rpc(req, **kw)

                wplat._shim_rpc = _paused_rpc  # type: ignore[method-assign]
                th_cancel1 = threading.Thread(target=wplat.cancel_recording, daemon=True)
                th_cancel1.start()
                self.assertTrue(
                    gen1_wto_ready.wait(timeout=2.0),
                    "gen1 cancel_recording() did not hit _ShimWriteTimeout(not_sent)",
                )

                # Detach wedged shim1 slot (without killing proc1 yet) and start gen2 on a brand-new shim2.
                with wplat._shim_lock:
                    wplat._shim = None
                    wplat._shim_writer = None
                    wplat._spawned_shim = None
                    wplat._spawned_ready = False
                wplat.start_recording(rt, seq_id=2)
                with wplat._winmm_state_lock:
                    gen2 = wplat._winmm_gen
                    writer2 = wplat._rec_writer
                    self.assertEqual(wplat._rec_mode, "winmm")
                    self.assertEqual(wplat._active_seq_id, 2)
                self.assertGreater(gen2, gen1)
                self.assertIsNotNone(writer2)
                assert writer2 is not None
                self.assertIsNot(writer2, writer1)
                proc2 = writer2.proc
                self.assertIsNot(proc2, proc1)
                self.assertIsNone(proc2.poll())

                # Unblock gen1's _ShimWriteTimeout propagation into cancel_recording().
                release_gen1_wto.set()
                th_cancel1.join(timeout=3.0)
                self.assertFalse(th_cancel1.is_alive())

                # Gen2 recording state & shim2 must remain completely intact; orphaned wedged proc1 is reaped.
                with wplat._winmm_state_lock:
                    self.assertEqual(wplat._rec_mode, "winmm")
                    self.assertEqual(wplat._winmm_gen, gen2)
                    self.assertIs(wplat._rec_writer, writer2)
                    self.assertEqual(wplat._active_seq_id, 2)
                self.assertIsNone(proc2.poll(), "gen1 cancel must never kill gen2's new shim")
                self.assertIs(wplat._shim, proc2)
                self.assertIsNotNone(proc1.poll(), "wedged gen1 shim1 should still be reaped")

                # Gen2 can stop cleanly on shim2 afterwards.
                wplat._rec_stop_timeout_sec = 5.0
                wplat.stop_recording(drain=False)
                cmds = [
                    json.loads(ln)
                    for ln in cmd_log.read_text(encoding="utf-8").splitlines()
                    if ln.strip()
                ]
                shim2_stops = [c for c in cmds if c.get("_tag") == "shim2" and c.get("cmd") == "rec_stop"]
                self.assertEqual(len(shim2_stops), 1, f"expected clean gen2 rec_stop on shim2, got {cmds}")
                self.assertEqual(shim2_stops[0].get("rec_gen"), gen2)
                self.assertIsNone(proc2.poll())
            finally:
                release_gen1_wto.set()
                gate_file.write_text("go", encoding="utf-8")
                wplat.close()


if __name__ == "__main__":
    unittest.main()
