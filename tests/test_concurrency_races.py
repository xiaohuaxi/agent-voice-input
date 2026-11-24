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
    _StdinGate,
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
                    # and verify handle_trigger("ptt_start") invokes hud("recording") -> _ensure_shim()
                    # while holding _trigger_lock and fast-returns False
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
                    hud_cooldown_calls: list[tuple[bool, bool]] = []
                    in_hud_recording = [False]
                    orig_hud = plat.hud
                    orig_ensure = plat._ensure_shim

                    def _spy_hud(state: str, *args: Any, **kwargs: Any) -> None:
                        if state == "recording":
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
                            hud_cooldown_calls.append((locked_now, ret))
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
                    self.assertEqual(
                        hud_cooldown_calls,
                        [(True, False)],
                        f"Expected hud('recording')->_ensure_shim()->False under _trigger_lock, got {hud_cooldown_calls}",
                    )
                    self.assertEqual(spawn_gen[0], spawns_before_cd)
                    daemon.handle_trigger(
                        "ptt_cancel",
                        ForegroundWindow(hwnd=301, process="WindowsTerminal.exe", title="codex"),
                    )

                    # Regression check A (enqueue before writer release): _reap_shim_proc neither hangs on
                    # blocked stdout nor leaks deferred stdin when writer holds _StdinGate
                    r_fd, w_fd = os.pipe()
                    r_file = os.fdopen(r_fd, "r", encoding="utf-8")
                    in_r_fd, in_w_fd = os.pipe()
                    os.close(in_r_fd)
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
                    fake_proc._deferred_close_stdin = False
                    gate_a = _StdinGate()
                    self.assertTrue(gate_a.acquire())
                    try:
                        t_reap0 = time.monotonic()
                        _reap_shim_proc(fake_proc, stdin_lock=gate_a)
                        reap_dt = time.monotonic() - t_reap0
                        self.assertLess(reap_dt, 1.5)
                        self.assertTrue(fake_proc._deferred_close_stdin)
                        self.assertFalse(in_w_file.closed)
                        self.assertFalse(r_file.closed)
                        # Writer releases _StdinGate -> closes deferred stdin automatically!
                        gate_a.release()
                        self.assertTrue(in_w_file.closed)
                        self.assertEqual(gate_a._deferred, [])
                    finally:
                        if gate_a._writers > 0:
                            gate_a.release()
                        if not in_w_file.closed:
                            in_w_file.close()
                        os.close(w_fd)
                        self.assertTrue(blocked_reader_done.wait(timeout=1.0))
                        self.assertTrue(r_file.closed)

                    # Regression check B (writer finishes release right after initial acquire timeout,
                    # before reaper inspects _state_lock): reaper observes _writers == 0 and closes pipe
                    in_r2, in_w2 = os.pipe()
                    os.close(in_r2)
                    in_w2_file = os.fdopen(in_w2, "w", encoding="utf-8")
                    gate_b = _StdinGate()
                    self.assertTrue(gate_b.acquire())
                    real_io_lock_b = gate_b._io_lock
                    calls_b = [0]

                    class _ProxyIoLockB:
                        def acquire(self, blocking: bool = True, timeout: float = -1.0) -> bool:
                            calls_b[0] += 1
                            if calls_b[0] == 1:
                                # Simulate writer finishing release() right as timeout expires!
                                gate_b.release()
                                return False
                            return (
                                real_io_lock_b.acquire(blocking=blocking)
                                if timeout < 0
                                else real_io_lock_b.acquire(blocking=blocking, timeout=timeout)
                            )

                        def release(self) -> None:
                            real_io_lock_b.release()

                    gate_b._io_lock = _ProxyIoLockB()  # type: ignore[assignment]
                    try:
                        self.assertTrue(gate_b.close_or_defer(in_w2_file, timeout=0.01))
                        self.assertTrue(in_w2_file.closed)
                        self.assertEqual(gate_b._deferred, [])
                    finally:
                        if not in_w2_file.closed:
                            in_w2_file.close()

                    # Regression check C (writer already drained first batch inside release() while still
                    # holding _io_lock -> reaper enqueues 2nd pipe before final _state_lock check):
                    # writer's release() loop catches and closes the 2nd pipe before releasing _io_lock!
                    p1_r, p1_w = os.pipe()
                    p2_r, p2_w = os.pipe()
                    os.close(p1_r)
                    os.close(p2_r)
                    p1_file = os.fdopen(p1_w, "w", encoding="utf-8")
                    p2_file = os.fdopen(p2_w, "w", encoding="utf-8")
                    gate_c = _StdinGate()
                    self.assertTrue(gate_c.acquire())
                    self.assertFalse(gate_c.close_or_defer(p1_file, timeout=0.0))

                    p1_closing_evt = threading.Event()
                    allow_p1_finish_evt = threading.Event()
                    orig_p1_close = p1_file.close

                    def _hooked_p1_close() -> None:
                        p1_closing_evt.set()
                        allow_p1_finish_evt.wait(timeout=2.0)
                        orig_p1_close()

                    p1_file.close = _hooked_p1_close  # type: ignore[method-assign]
                    writer_rel_th = threading.Thread(target=gate_c.release, daemon=True)
                    writer_rel_th.start()
                    try:
                        # Writer ALREADY drained _deferred once and is mid-release still holding _io_lock!
                        self.assertTrue(p1_closing_evt.wait(timeout=2.0))
                        self.assertFalse(gate_c.close_or_defer(p2_file, timeout=0.0))
                        allow_p1_finish_evt.set()
                        writer_rel_th.join(timeout=2.0)
                        self.assertFalse(writer_rel_th.is_alive())
                        self.assertTrue(p1_file.closed)
                        self.assertTrue(p2_file.closed)
                        self.assertEqual(gate_c._deferred, [])
                        self.assertEqual(gate_c._writers, 0)
                    finally:
                        allow_p1_finish_evt.set()
                        writer_rel_th.join(timeout=1.0)
                        for pf in (p1_file, p2_file):
                            if not pf.closed:
                                orig_p1_close() if pf is p1_file else pf.close()

                    # Regression check D (two concurrent reapers: Reaper 1 acquires gate and blocks inside
                    # pipe1.close(); Reaper 2 times out promptly, enqueues pipe2, and Reaper 1 closes both!)
                    d1_r, d1_w = os.pipe()
                    d2_r, d2_w = os.pipe()
                    os.close(d1_r)
                    os.close(d2_r)
                    d1_file = os.fdopen(d1_w, "w", encoding="utf-8")
                    d2_file = os.fdopen(d2_w, "w", encoding="utf-8")
                    gate_d = _StdinGate()
                    d1_in_close = threading.Event()
                    d1_unblock = threading.Event()
                    orig_d1_close = d1_file.close

                    def _slow_d1_close() -> None:
                        d1_in_close.set()
                        d1_unblock.wait(timeout=2.0)
                        orig_d1_close()

                    d1_file.close = _slow_d1_close  # type: ignore[method-assign]
                    reaper1_th = threading.Thread(
                        target=lambda: gate_d.close_or_defer(d1_file, timeout=0.2), daemon=True
                    )
                    reaper1_th.start()
                    try:
                        self.assertTrue(d1_in_close.wait(timeout=2.0))
                        t_d2 = time.monotonic()
                        self.assertFalse(gate_d.close_or_defer(d2_file, timeout=0.02))
                        self.assertLess(time.monotonic() - t_d2, 0.25)
                        d1_unblock.set()
                        reaper1_th.join(timeout=2.0)
                        self.assertFalse(reaper1_th.is_alive())
                        self.assertTrue(d1_file.closed)
                        self.assertTrue(d2_file.closed)
                        self.assertEqual(gate_d._deferred, [])
                        self.assertEqual(gate_d._writers, 0)
                    finally:
                        d1_unblock.set()
                        reaper1_th.join(timeout=1.0)
                        for df in (d1_file, d2_file):
                            if not df.closed:
                                orig_d1_close() if df is d1_file else df.close()
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
        """1) Normal ptt_stop/toggle stops recording BEFORE HUD RPC; slow HUD never blocks _hud_lock/_trigger_lock."""
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
            hud_entered = threading.Event()
            release_hud = threading.Event()
            block_next_hud = [False]

            class _SlowHudPlatform(_TorturePlatform):
                def stop_recording(self, *, drain: bool = True) -> tuple[bytes, str]:
                    call_order.append("stop_recording")
                    return super().stop_recording(drain=drain)

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
                    if block_next_hud[0] and state == "recognizing":
                        block_next_hud[0] = False
                        hud_entered.set()
                        release_hud.wait(timeout=3.0)
                    super().hud(
                        state,
                        text=text,
                        duration_ms=duration_ms,
                        layer=layer,
                        clear_main=clear_main,
                        clear_toast=clear_toast,
                        bg_count=bg_count,
                    )

            plat = _SlowHudPlatform(self.speech_wav)
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
                # 1a. Normal ptt_stop: stop_recording MUST precede hud:recognizing even when HUD blocks!
                self.assertTrue(daemon.handle_trigger("ptt_start", win)["ok"])
                call_order.clear()
                block_next_hud[0] = True
                t_stop0 = time.monotonic()
                res_stop = daemon.handle_trigger("ptt_stop", win)
                stop_elapsed = time.monotonic() - t_stop0
                self.assertTrue(res_stop["ok"])
                self.assertTrue(hud_entered.wait(timeout=1.0))
                self.assertLess(
                    stop_elapsed,
                    0.4,
                    f"ptt_stop must not wait for 3s blocked HUD RPC (took {stop_elapsed:.3f}s)",
                )
                self.assertIn("stop_recording", call_order)
                self.assertIn("hud:recognizing", call_order)
                self.assertLess(
                    call_order.index("stop_recording"),
                    call_order.index("hud:recognizing"),
                    f"stop_recording must run before HUD RPC, got {call_order}",
                )

                # 1b. While HUD worker is still blocked on the slow RPC, _hud_lock & _trigger_lock stay responsive!
                t_next0 = time.monotonic()
                res_next = daemon.handle_trigger("ptt_start", win)
                self.assertTrue(res_next["ok"])
                self.assertLess(time.monotonic() - t_next0, 0.3)
                res_cancel = daemon.handle_cancel()
                self.assertTrue(res_cancel["ok"])
            finally:
                release_hud.set()
                daemon.close()
                tracker.close()

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
                hud_w = daemon._hud_worker
                shut_resp = send_control_command(rt, {"cmd": "shutdown"}, timeout_sec=2.0)
                self.assertTrue(shut_resp["ok"])
                srv_th.join(timeout=3.0)
                self.assertFalse(srv_th.is_alive())
                if ctrl_w is not None:
                    self.assertFalse(ctrl_w.is_alive(), "control trigger worker leaked after shutdown")
                if hud_w is not None:
                    self.assertFalse(hud_w.is_alive(), "HUD worker leaked after shutdown")
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
                self.assertFalse(plat._rec_active, "closed=True must never coexist with rec_active=True")
                self.assertFalse(plat._recording)
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

            # 4a. _shim_rpc(..., timeout_sec=0.05) MUST time out promptly despite full pipe!
            t0 = time.monotonic()
            with self.assertRaises(TimeoutError):
                bridge._shim_rpc({"cmd": "ping"}, timeout_sec=0.05, allow_retry=True)
            dt_rpc = time.monotonic() - t0
            self.assertGreaterEqual(dt_rpc, 0.04)
            self.assertLess(dt_rpc, 0.35, f"_shim_rpc exceeded deadline on full pipe ({dt_rpc:.3f}s)")

            # 4b. hud_level() on full pipe MUST drop frame immediately without blocking audio reader!
            t_lvl0 = time.monotonic()
            for idx in range(50):
                bridge.hud_level(0.5 + (idx % 5) * 0.1)
            dt_lvl = time.monotonic() - t_lvl0
            self.assertLess(dt_lvl, 0.15, f"hud_level blocked on full pipe ({dt_lvl:.3f}s)")

            # 4c. hud_level() while _stdin_lock is held MUST drop frame immediately!
            self.assertTrue(bridge._stdin_lock.acquire())
            try:
                t_lock0 = time.monotonic()
                for _ in range(20):
                    bridge.hud_level(0.42)
                self.assertLess(time.monotonic() - t_lock0, 0.05)

                # And _shim_rpc waiting on held _stdin_lock honors the same deadline!
                t_lk_rpc = time.monotonic()
                with self.assertRaises(TimeoutError):
                    bridge._shim_rpc({"cmd": "ping"}, timeout_sec=0.05, allow_retry=False)
                self.assertLess(time.monotonic() - t_lk_rpc, 0.25)
            finally:
                bridge._stdin_lock.release()
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
                    plat._rec_active = True
                    plat._recording = True
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
                self.assertFalse(plat._rec_active)
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

            # Hook _reap_owned_shim to pause AFTER _stdin_lock is released and BEFORE reap completes;
            # a concurrent hud_level holding the stale proc reference MUST see _stdin_poisoned and refuse to write!
            reap_hook_hit = threading.Event()
            lvl_attempted = threading.Event()
            orig_reap = bridge._reap_owned_shim

            def _hooked_reap(p: Any) -> None:
                reap_hook_hit.set()
                lvl_attempted.wait(timeout=2.0)
                orig_reap(p)

            bridge._reap_owned_shim = _hooked_reap  # type: ignore[method-assign]

            stale_sent: list[bool] = []
            pre_drained: list[bytes] = []

            def _stale_writer() -> None:
                reap_hook_hit.wait(timeout=2.0)
                # Drain kernel pipe space BEFORE the stale writer attempts to write so the pipe
                # IS writable again; rejection MUST come from _stdin_poisoned rather than a full pipe.
                try:
                    pre_drained.append(os.read(r_fd, 16384))
                except BlockingIOError:
                    pass
                stale_sent.append(
                    bridge._send_shim_fire_and_forget_to_proc(
                        proc,  # type: ignore[arg-type]
                        {"cmd": "hud_level", "level": 0.9},
                        drop_if_busy=True,
                    )
                )
                lvl_attempted.set()

            stale_th = threading.Thread(target=_stale_writer, daemon=True)
            stale_th.start()
            with self.assertRaises(TimeoutError):
                bridge._shim_rpc({"cmd": "paste", "text": "Z" * 131072}, timeout_sec=0.04, allow_retry=False)
            stale_th.join(timeout=2.0)
            self.assertEqual(stale_sent, [False])
            self.assertGreater(
                sum(len(c) for c in pre_drained),
                0,
                "pipe must have writable space before stale writer runs",
            )
            self.assertTrue(getattr(proc, "_stdin_poisoned", False))
            self.assertIsNone(bridge._shim)
            self.assertEqual(respawn_calls[0], 1, "poisoned current shim must schedule respawn")

            # Drain remaining kernel pipe bytes and confirm hud_level was NEVER appended after partial JSON prefix.
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
        """P0-2: Guarded FfmpegWavRecorder.start prevents Popen after MacOSPlatform.close() returns."""
        from agent_voice_input.platform_common import FfmpegWavRecorder
        from agent_voice_input.platform_macos import MacOSPlatform

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            rt.mkdir(parents=True, exist_ok=True)
            build_entered = threading.Event()
            release_build = threading.Event()
            popen_called = [0]

            rec = FfmpegWavRecorder(
                cmd_factory=lambda _f, _s: [sys.executable, "-c", "import time; time.sleep(5)"]
            )
            orig_build = rec._build_cmd

            def _paused_build(fmt: str, src: str) -> list[str]:
                cmd = orig_build(fmt, src)
                build_entered.set()
                release_build.wait(timeout=3.0)
                return cmd

            rec._build_cmd = _paused_build  # type: ignore[method-assign]
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
            plat._ff_rec = rec
            errs: list[BaseException] = []
            orig_popen = subprocess.Popen

            def _counting_popen(*args: Any, **kwargs: Any) -> Any:
                popen_called[0] += 1
                return orig_popen(*args, **kwargs)

            def _run_start() -> None:
                try:
                    plat.start_recording(rt, seq_id=1)
                except BaseException as exc:
                    errs.append(exc)

            with mock.patch("agent_voice_input.platform_common.subprocess.Popen", side_effect=_counting_popen):
                th = threading.Thread(target=_run_start, daemon=True)
                th.start()
                try:
                    self.assertTrue(build_entered.wait(timeout=2.0))
                    # close() returns while start_recording is parked right before Popen!
                    plat.close()
                    self.assertTrue(plat._closed)
                    self.assertIsNone(plat._cur_session)
                    release_build.set()
                    th.join(timeout=2.0)
                    self.assertFalse(th.is_alive())
                    self.assertEqual(errs, [])
                    self.assertEqual(popen_called[0], 0, "Popen must NOT run after close() returned")
                    self.assertIsNone(rec._proc)
                    self.assertFalse(plat._rec_active)
                    self.assertIsNone(plat._cur_session)
                finally:
                    release_build.set()
                    th.join(timeout=1.0)
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

            # 1) On a full pipe, cleanup command times out cleanly without leaving anything in Python buffer.
            sent = bridge._send_shim_fire_and_forget(
                {"cmd": "rec_stop", "rec_gen": 1, "discard": True},
                timeout_sec=0.02,
            )
            self.assertFalse(sent)

            # Drain filler bytes completely.
            drained = 0
            while drained < filled:
                drained += len(os.read(r_fd, 65536))

            # 2) Subsequent cleanup + RPC arrive in strict FIFO order on the pipe.
            self.assertTrue(
                bridge._send_shim_fire_and_forget({"cmd": "rec_release", "rec_gen": 1}, timeout_sec=0.1)
            )
            # Send an RPC line (will time out waiting for reply since no reader replies, which is expected).
            with self.assertRaises(queue.Empty):
                bridge._shim_rpc({"cmd": "rec_start", "rec_gen": 2}, timeout_sec=0.02, allow_retry=False)

            raw_out = os.read(r_fd, 65536).decode("utf-8")
            cmds = [json.loads(ln)["cmd"] for ln in raw_out.splitlines() if ln.strip()]
            self.assertEqual(cmds, ["rec_release", "rec_start"])
        finally:
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
        """P2-1: Full HUD queue evicts intermediate stage updates before critical result toasts (copied) & hide."""
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
            first_hud_entered = threading.Event()
            release_hud = threading.Event()
            seen_states: list[str] = []
            hide_seen = threading.Event()

            class _BlockingHudPlat(_TorturePlatform):
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
                    if not first_hud_entered.is_set():
                        first_hud_entered.set()
                        release_hud.wait(timeout=3.0)
                    seen_states.append(state)
                    if state == "hide":
                        hide_seen.set()
                    super().hud(
                        state,
                        text=text,
                        duration_ms=duration_ms,
                        layer=layer,
                        clear_main=clear_main,
                        clear_toast=clear_toast,
                        bg_count=bg_count,
                    )

            plat = _BlockingHudPlat(self.speech_wav)
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
            daemon._hud_wait_timeout_sec = 0.001
            try:
                # 1st batch blocks the HUD worker
                daemon._hud("recording")
                self.assertTrue(first_hud_entered.wait(timeout=1.0))
                # Enqueue critical result toast 'copied', then flood >64 intermediate stage updates, then 'hide'
                daemon._hud("copied", layer="toast", clear_main=False)
                for idx in range(70):
                    daemon._hud("recognizing" if idx % 2 == 0 else "refining")
                daemon._hud("hide")
                release_hud.set()
                self.assertTrue(hide_seen.wait(timeout=2.0))
                self.assertIn("copied", seen_states, "copied toast must not be evicted by stage updates")
                self.assertIn("hide", seen_states, "hide clear must be preserved")
            finally:
                release_hud.set()
                daemon.close()
                tracker.close()

    def test_r3_p1_1_wsl_start_timeout_discard_write_failure_reaps_wedged_shim(self) -> None:
        """R2 P1-1: WSL rec_start timeout whose discard rec_stop cannot be written must reap the wedged shim."""
        import io
        from agent_voice_input.platform_wsl import WslPlatform

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            rt.mkdir(parents=True, exist_ok=True)
            cfg = Config()
            cfg.audio.wsl_source = "wasapi"
            plat = WslPlatform(cfg, env={"PATH": "/usr/bin"})
            respawn_calls = [0]

            class _FakeWedgedProc:
                def __init__(self) -> None:
                    self.stdin = io.StringIO()
                    self.stdout = None
                    self.killed = False

                def poll(self) -> int | None:
                    return 1 if self.killed else None

                def kill(self) -> None:
                    self.killed = True

                def wait(self, timeout: float | None = None) -> int:
                    del timeout
                    return 1

            proc = _FakeWedgedProc()
            plat._shim = proc  # type: ignore[assignment]
            plat._spawned_shim = proc  # type: ignore[assignment]
            plat._spawned_ready = True
            plat._on_event = lambda _e: None
            plat._ready.set()
            plat._schedule_respawn_if_needed = (  # type: ignore[method-assign]
                lambda: respawn_calls.__setitem__(0, respawn_calls[0] + 1)
            )

            lock_acquired = threading.Event()
            release_lock = threading.Event()

            def _hold_stdin_lock() -> None:
                with plat._stdin_lock:
                    lock_acquired.set()
                    release_lock.wait(timeout=3.0)

            orig_reap = plat._reap_owned_shim

            def _reap_and_release(p: Any) -> None:
                release_lock.set()
                orig_reap(p)

            plat._reap_owned_shim = _reap_and_release  # type: ignore[method-assign]

            holder_th = threading.Thread(target=_hold_stdin_lock, daemon=True)

            def _timeout_rpc(
                payload: dict[str, Any],
                timeout_sec: float = 5.0,
                *,
                allow_retry: bool = True,
                before_write: Any = None,
            ) -> dict[str, Any] | None:
                del payload, timeout_sec, allow_retry
                if before_write is not None:
                    before_write()
                # Wedge _stdin_lock right after rec_start was dispatched so the subsequent
                # rec_stop(discard=True) cleanup exceeds its 0.25s write deadline and returns False.
                holder_th.start()
                self.assertTrue(lock_acquired.wait(timeout=2.0))
                raise queue.Empty()

            plat._shim_rpc = _timeout_rpc  # type: ignore[method-assign]
            try:
                with self.assertRaises(queue.Empty):
                    plat.start_recording(rt)
                holder_th.join(timeout=2.0)
                self.assertTrue(getattr(proc, "_stdin_poisoned", False))
                self.assertTrue(proc.killed, "wedged shim must be killed so host cannot keep recording")
                self.assertIsNone(plat._shim)
                self.assertEqual(respawn_calls[0], 1, "reaping wedged current shim must schedule respawn")
            finally:
                release_lock.set()
                plat.close()

    def test_r3_p1_2_macos_handoff_rejects_spawn_when_selected_uid_disconnects(self) -> None:
        """R2 P1-2: MacOSPlatform handoff before Popen must reject start if selected mic UID disconnected."""
        from agent_voice_input.platform_common import (
            FfmpegWavRecorder,
            MicrophoneUnavailableError,
            NoMicrophoneError,
        )
        from agent_voice_input.platform_macos import MacOSPlatform

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
                    popen_called = [0]

                    rec = FfmpegWavRecorder(
                        cmd_factory=lambda _f, _s: [sys.executable, "-c", "import time; time.sleep(5)"]
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
                    plat._ff_rec = rec
                    exc_box: list[BaseException] = []

                    def _run_start() -> None:
                        try:
                            plat.start_recording(rt)
                        except BaseException as exc:
                            exc_box.append(exc)

                    def _counting_popen(*args: Any, **kwargs: Any) -> Any:
                        popen_called[0] += 1
                        raise AssertionError(f"Popen must not be called after {label}: {args} {kwargs}")

                    try:
                        with mock.patch("agent_voice_input.platform_common.subprocess.Popen", side_effect=_counting_popen):
                            th = threading.Thread(target=_run_start, daemon=True)
                            th.start()
                            self.assertTrue(build_entered.wait(timeout=2.0))
                            plat._on_shim_internal_event(disconnect_event)
                            release_build.set()
                            th.join(timeout=2.0)
                        self.assertEqual(popen_called[0], 0, "FFmpeg Popen must be rejected at handoff")
                        self.assertEqual(len(exc_box), 1)
                        self.assertIsInstance(exc_box[0], expected_exc)
                        self.assertEqual(plat.last_audio_error, "coreaudio_device_disconnected")
                        self.assertFalse(plat._rec_active)
                        self.assertIsNone(plat._cur_session)
                    finally:
                        release_build.set()
                        plat.close()

    def test_r4_1_wsl_rec_start_lock_timeout_unwritten_does_not_kill_healthy_shim(self) -> None:
        """R3-1: WSL rec_start timing out on _stdin_lock (0 bytes written) must NOT send discard or kill shim."""
        import io
        from agent_voice_input.platform_common import _ShimWriteTimeout
        from agent_voice_input.platform_wsl import WslPlatform

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            rt.mkdir(parents=True, exist_ok=True)
            cfg = Config()
            cfg.audio.wsl_source = "wasapi"
            plat = WslPlatform(cfg, env={"PATH": "/usr/bin"})

            class _HealthyPasteProc:
                def __init__(self) -> None:
                    self.stdin = io.StringIO()
                    self.stdout = None
                    self.killed = False

                def poll(self) -> int | None:
                    return 1 if self.killed else None

                def kill(self) -> None:
                    self.killed = True

                def wait(self, timeout: float | None = None) -> int:
                    del timeout
                    return 0

            proc = _HealthyPasteProc()
            plat._shim = proc  # type: ignore[assignment]
            plat._spawned_shim = proc  # type: ignore[assignment]
            plat._spawned_ready = True
            plat._on_event = lambda _e: None
            plat._ready.set()
            plat._ensure_shim = lambda *, ignore_cooldown=False: True  # type: ignore[method-assign]

            lock_acquired = threading.Event()
            release_lock = threading.Event()

            def _long_paste_holder() -> None:
                with plat._stdin_lock:
                    lock_acquired.set()
                    release_lock.wait(timeout=3.0)

            orig_rpc = plat._shim_rpc

            def _short_timeout_rpc(
                payload: dict[str, Any],
                timeout_sec: float = 5.0,
                *,
                allow_retry: bool = True,
                before_write: Any = None,
            ) -> dict[str, Any] | None:
                return orig_rpc(
                    payload,
                    timeout_sec=0.04,
                    allow_retry=allow_retry,
                    before_write=before_write,
                )

            plat._shim_rpc = _short_timeout_rpc  # type: ignore[method-assign]
            reap_calls = [0]
            orig_detach = plat._detach_and_reap_poisoned_shim

            def _counting_detach(p: Any) -> None:
                reap_calls[0] += 1
                orig_detach(p)

            plat._detach_and_reap_poisoned_shim = _counting_detach  # type: ignore[method-assign]
            holder_th = threading.Thread(target=_long_paste_holder, daemon=True)
            holder_th.start()
            self.assertTrue(lock_acquired.wait(timeout=2.0))
            try:
                with self.assertRaises(_ShimWriteTimeout) as ctx:
                    plat.start_recording(rt)
                self.assertFalse(ctx.exception.partial)
                self.assertFalse(ctx.exception.written)
                self.assertEqual(reap_calls[0], 0, "unwritten rec_start must NOT reap shim serving concurrent paste")
                self.assertFalse(proc.killed, "healthy shim must remain alive")
                self.assertFalse(getattr(proc, "_stdin_poisoned", False))
                self.assertIs(plat._shim, proc)
                self.assertEqual(proc.stdin.getvalue(), "", "no discard rec_stop should be written when 0 bytes sent")
            finally:
                release_lock.set()
                holder_th.join(timeout=2.0)
                plat.close()

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
                self.assertFalse(plat._rec_active)
                self.assertIsNone(plat._cur_session)
            finally:
                release_probe.set()
                plat.close()


if __name__ == "__main__":
    unittest.main()
