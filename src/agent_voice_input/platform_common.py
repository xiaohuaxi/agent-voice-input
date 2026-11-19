"""Cross-platform window/paste models, FfmpegWavRecorder, and JsonlShimMixin bridge."""
from __future__ import annotations

from dataclasses import dataclass
import io
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Mapping, Protocol

from .audio_proc import pack_pcm16_mono_wav, resolve_ffmpeg_bin
from .config import Config, normalize_ui_position, redact_secrets
from .hud import (
    default_hud_duration_ms,
    format_hud_text,
    hud_layer_for_state,
    pcm16le_rms_level,
    recording_countdown_warn_sec,
    resolve_ui_language,
)


@dataclass(frozen=True)
class ForegroundWindow:
    hwnd: int = 0
    process: str = ""
    title: str = ""
    bundle_id: str = ""
    surface_id: str = ""
    tty: str = ""
    cwd: str = ""
    pid: int = 0
    ax_trusted: bool | None = None
    tmux_socket: str = ""
    tmux_pane: str = ""
    document: str = ""

    @property
    def app_name(self) -> str:
        return self.process


@dataclass(frozen=True)
class PasteOutcome:
    pasted: bool; clipboard: bool; reason: str = "ok"; fg_process: str = ""; used_clipboard_paste: bool = False


class NoMicrophoneError(RuntimeError):
    """Raised when no usable microphone / audio input device can be opened."""

    reason_code: str = "no_mic"
    hud_state: str = "no_mic"


class MicrophoneUnavailableError(RuntimeError):
    """Raised when a microphone exists but cannot be opened (busy, switching, or permission denied)."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: str = "mic_unavailable",
        hud_state: str = "mic_unavailable",
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.hud_state = hud_state


class RecorderLostCallback(Protocol):
    def __call__(self, *, seq_id: int | None = None) -> Any: ...


class RecorderRecoveredCallback(Protocol):
    def __call__(self, gap_ms: int, *, seq_id: int | None = None) -> Any: ...


def invoke_recorder_lost_callback(
    cb: RecorderLostCallback | Callable[..., Any] | None, seq_id: int | None = None
) -> None:
    if cb is None:
        return
    if seq_id is not None:
        cb(seq_id=seq_id)
        return
    cb()


def invoke_recorder_recovered_callback(
    cb: RecorderRecoveredCallback | Callable[..., Any] | None,
    gap_ms: int,
    seq_id: int | None = None,
) -> None:
    if cb is None:
        return
    if seq_id is not None:
        cb(gap_ms, seq_id=seq_id)
        return
    cb(gap_ms)


def classify_ffmpeg_startup_error(
    stderr: str,
    rc: int | None,
    *,
    diagnostic: str,
    has_known_input: bool | None = None,
) -> NoMicrophoneError | MicrophoneUnavailableError:
    low = (stderr or "").lower()
    if any(
        tok in low
        for tok in (
            "permission denied",
            "not authorized",
            "access denied",
            "operation not permitted",
            "authorization",
        )
    ):
        return MicrophoneUnavailableError(
            f"microphone access denied ({diagnostic})",
            reason_code="mic_permission",
            hud_state="mic_permission",
        )
    if any(
        tok in low
        for tok in (
            "device or resource busy",
            "resource busy",
            "device in use",
            "is busy",
            "startup_timeout_no_frames",
        )
    ):
        return MicrophoneUnavailableError(
            f"microphone busy ({diagnostic})",
            reason_code="mic_unavailable",
            hud_state="mic_unavailable",
        )
    if any(
        tok in low
        for tok in ("device not found", "no such device", "cannot find audio device", "unknown input")
    ):
        return NoMicrophoneError(f"no microphone input device available ({diagnostic})")
    if has_known_input is True:
        return MicrophoneUnavailableError(
            f"microphone temporarily unavailable ({diagnostic})",
            reason_code="mic_unavailable",
            hud_state="mic_unavailable",
        )
    return NoMicrophoneError(f"no microphone input device available ({diagnostic})")


class FfmpegWavRecorder:
    def __init__(self, cmd_factory: Callable[[str, str], list[str]] | None = None) -> None:
        self._cmd_factory = cmd_factory
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._err_reader: threading.Thread | None = None
        self._wd_stop_evt: threading.Event | None = None
        self._wd_thread: threading.Thread | None = None
        self._buf = bytearray()
        self._err_buf = bytearray()
        self._stdout_eof = False
        self._stopping = False
        self._stall_triggered = False
        self._last_chunk_ts: float = 0.0
        self._unexpected_exit_cb: Callable[[], None] | None = None
        self._level_cb: Callable[[float], None] | None = None
        self._live_level: float = 0.0
        self._live_level_ts: float = 0.0
        self.first_chunk_ts: float = 0.0
        self.last_chunk_ts: float = 0.0
        self.last_stderr: str = ""
        self.last_returncode: int | None = None

    @property
    def is_running(self) -> bool:
        with self._lock:
            return (
                self._proc is not None
                and not self._stopping
                and not self._stdout_eof
                and self._proc.poll() is None
            )

    @property
    def current_level(self) -> float:
        if self._proc is None or (time.monotonic() - self._live_level_ts) > 0.25:
            return 0.0
        return self._live_level

    def _build_cmd(self, fmt: str, source: str) -> list[str]:
        if self._cmd_factory is not None:
            return self._cmd_factory(fmt, source)
        in_opts = ["-sample_rate", "16000", "-channels", "1", "-fragment_size", "640"] if fmt == "pulse" else []
        return [resolve_ffmpeg_bin(), "-nostdin", "-hide_banner", "-loglevel", "error", "-probesize", "32", "-analyzeduration", "0", "-fflags", "nobuffer", "-flags", "low_delay", *in_opts, "-f", fmt, "-i", source, "-ac", "1", "-ar", "16000", "-f", "s16le", "-acodec", "pcm_s16le", "-flush_packets", "1", "pipe:1"]

    def _read_pipe(self, pipe: Any, sink: bytearray, limit: int = 0) -> None:
        read_fn = (getattr(pipe, "read1", None) or pipe.read) if not limit else pipe.read
        chunk_sz = 2048 if not limit else 4096
        try:
            while chunk := read_fn(chunk_sz):
                sink.extend(chunk)
                if limit:
                    if len(sink) > limit:
                        del sink[:-limit]
                elif self._buf is sink:
                    self._live_level = pcm16le_rms_level(chunk)
                    self._live_level_ts = time.monotonic()
        except Exception:
            pass
        finally:
            try: pipe.close()
            except Exception: pass

    def _read_stdout(
        self,
        pipe: Any,
        sink: bytearray,
        startup_evt: threading.Event,
        proc: subprocess.Popen[bytes],
        err_reader: threading.Thread,
        err_buf: bytearray,
    ) -> None:
        read_fn = getattr(pipe, "read1", None) or pipe.read
        try:
            while chunk := read_fn(1600):
                now_ts = time.monotonic()
                sink.extend(chunk)
                self._last_chunk_ts = now_ts
                self.last_chunk_ts = now_ts
                if len(sink) >= 2 and not startup_evt.is_set():
                    self.first_chunk_ts = now_ts
                    startup_evt.set()
                lvl = pcm16le_rms_level(chunk)
                self._live_level = lvl
                self._live_level_ts = now_ts
                if (lvl_cb := self._level_cb) is not None:
                    try:
                        lvl_cb(lvl)
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            try: pipe.close()
            except Exception: pass
            startup_evt.set()
            cb: Callable[[], None] | None = None
            with self._lock:
                if self._proc is proc:
                    self._stdout_eof = True
                still_active = (
                    self._proc is proc
                    and not self._stopping
                    and self._unexpected_exit_cb is not None
                )
            if still_active:
                try:
                    rc = proc.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    rc = proc.poll()
                err_reader.join(timeout=0.15)
                with self._lock:
                    if self._proc is proc and not self._stopping:
                        self.last_returncode = rc
                        raw_err = bytes(err_buf).decode("utf-8", errors="replace").strip()
                        if self._stall_triggered:
                            raw_err = f"{raw_err} [stream_stalled]".strip()
                        elif rc == 0 and not raw_err:
                            raw_err = "unexpected_eof_rc0"
                        self.last_stderr = raw_err
                        cb = self._unexpected_exit_cb
                        self._unexpected_exit_cb = None
                        if (wd_evt := self._wd_stop_evt) is not None:
                            wd_evt.set()
            if cb is not None:
                threading.Thread(target=cb, daemon=True).start()

    def _watch_stall(
        self,
        proc: subprocess.Popen[bytes],
        sink: bytearray,
        wd_stop_evt: threading.Event,
        start_ts: float,
        stall_timeout_sec: float,
    ) -> None:
        initial_grace_sec = max(1.6, stall_timeout_sec * 1.5)
        poll_sec = max(0.04, min(0.12, stall_timeout_sec / 4.0))
        while not wd_stop_evt.wait(poll_sec):
            with self._lock:
                if self._proc is not proc or self._stopping or self._stdout_eof:
                    return
                last_ts = self._last_chunk_ts
                got_audio = len(sink) >= 2
            now = time.monotonic()
            stalled = (
                (now - last_ts) >= stall_timeout_sec
                if (got_audio and last_ts > 0.0)
                else (now - start_ts) >= initial_grace_sec
            )
            if stalled:
                with self._lock:
                    if self._proc is not proc or self._stopping:
                        return
                    self._stall_triggered = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                return

    def start(
        self,
        fmt: str,
        source: str,
        runtime_dir: Path | None = None,
        env: Mapping[str, str] | None = None,
        *,
        verify_startup: bool = False,
        startup_timeout_sec: float = 0.65,
        stall_timeout_sec: float = 1.2,
        has_known_input: bool | None = None,
        on_unexpected_exit: Callable[[], None] | None = None,
        on_level: Callable[[float], None] | None = None,
    ) -> None:
        self.stop(drain_sec=0.0)
        buf, err_buf = bytearray(), bytearray()
        startup_evt = threading.Event()
        start_ts = time.monotonic()
        self.last_stderr, self.last_returncode, self.first_chunk_ts, self.last_chunk_ts = "", None, 0.0, 0.0
        proc = subprocess.Popen(self._build_cmd(fmt, source), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, start_new_session=True, env=dict(os.environ if env is None else env))
        assert proc.stdout is not None and proc.stderr is not None
        err_reader = threading.Thread(target=self._read_pipe, args=(proc.stderr, err_buf, 512), daemon=True)
        reader = threading.Thread(target=self._read_stdout, args=(proc.stdout, buf, startup_evt, proc, err_reader, err_buf), daemon=True)
        with self._lock:
            self._proc, self._reader, self._err_reader, self._buf, self._err_buf = proc, reader, err_reader, buf, err_buf
            self._wd_stop_evt, self._wd_thread = None, None
            self._stdout_eof = False
            self._stopping = False
            self._stall_triggered = False
            self._last_chunk_ts = 0.0
            self._unexpected_exit_cb = None
            self._level_cb = on_level
            self._live_level, self._live_level_ts = 0.0, 0.0
        try:
            err_reader.start(); reader.start()
        except Exception:
            with self._lock:
                if self._proc is proc:
                    self._proc, self._reader, self._err_reader, self._buf, self._err_buf = None, None, None, bytearray(), bytearray()
                    self._stdout_eof = False
                    self._stopping = False
                    self._stall_triggered = False
                    self._last_chunk_ts = 0.0
                    self._unexpected_exit_cb = None
                    self._level_cb = None
                    self._live_level, self._live_level_ts = 0.0, 0.0
            try: proc.kill(); proc.wait(timeout=0.5)
            except Exception: pass
            for pipe in (proc.stdout, proc.stderr):
                try: pipe.close()
                except Exception: pass
            raise

        if verify_startup:
            startup_evt.wait(timeout=max(0.02, float(startup_timeout_sec)))
            with self._lock:
                saw_eof = self._stdout_eof
            if len(buf) < 2:
                exited = saw_eof or proc.poll() is not None
                if exited:
                    try:
                        rc = proc.wait(timeout=0.25)
                    except subprocess.TimeoutExpired:
                        rc = proc.poll()
                    err_reader.join(timeout=0.15)
                else:
                    rc = None
                err_txt = bytes(err_buf).decode("utf-8", errors="replace").strip()
                self.stop(drain_sec=0.0)
                if exited:
                    self.last_returncode = rc
                    self.last_stderr = err_txt or ("startup_eof_rc0" if rc == 0 else "")
                else:
                    self.last_returncode = None
                    self.last_stderr = err_txt or "startup_timeout_no_frames"
                raise classify_ffmpeg_startup_error(
                    self.last_stderr,
                    rc,
                    diagnostic=self.diagnostic_summary(),
                    has_known_input=has_known_input,
                )

        if on_unexpected_exit is not None:
            early_eof = False
            wd_evt: threading.Event | None = None
            wd_thr: threading.Thread | None = None
            with self._lock:
                if self._proc is proc and not self._stopping:
                    if self._stdout_eof or proc.poll() is not None:
                        early_eof = True
                    else:
                        self._unexpected_exit_cb = on_unexpected_exit
                        if stall_timeout_sec > 0.0:
                            wd_evt = threading.Event()
                            wd_thr = threading.Thread(
                                target=self._watch_stall,
                                args=(proc, buf, wd_evt, start_ts, float(stall_timeout_sec)),
                                daemon=True,
                            )
                            self._wd_stop_evt = wd_evt
                            self._wd_thread = wd_thr
            if wd_thr is not None:
                wd_thr.start()
            if early_eof:
                try:
                    rc = proc.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    rc = proc.poll()
                err_reader.join(timeout=0.15)
                should_fire = False
                with self._lock:
                    if self._proc is proc and not self._stopping:
                        self.last_returncode = rc
                        raw_err = bytes(err_buf).decode("utf-8", errors="replace").strip()
                        self.last_stderr = raw_err or ("unexpected_eof_rc0" if rc == 0 else "")
                        should_fire = True
                if should_fire:
                    threading.Thread(target=on_unexpected_exit, daemon=True).start()

    def stop(self, *, drain_sec: float = 0.0) -> bytes:
        with self._lock:
            proc, reader, err_reader, buf, err_buf = self._proc, self._reader, self._err_reader, self._buf, self._err_buf
            wd_evt, wd_thr = self._wd_stop_evt, self._wd_thread
            self._proc, self._reader, self._err_reader, self._buf, self._err_buf = None, None, None, bytearray(), bytearray()
            self._wd_stop_evt, self._wd_thread = None, None
            self._stopping = True
            self._stdout_eof = False
            self._unexpected_exit_cb = None
            self._level_cb = None
            self._live_level, self._live_level_ts = 0.0, 0.0
        if wd_evt is not None:
            wd_evt.set()
        if wd_thr is not None and wd_thr.is_alive() and wd_thr is not threading.current_thread():
            wd_thr.join(timeout=0.5)
        if proc is None:
            return b""
        if drain_sec > 0.0 and proc.poll() is None:
            time.sleep(drain_sec)
        for sig, wait_s in ((signal.SIGTERM, 0.18), (signal.SIGKILL, 1.0)):
            if proc.poll() is not None:
                break
            try: os.killpg(proc.pid, sig)
            except ProcessLookupError: pass
            except OSError:
                try: proc.send_signal(sig)
                except ProcessLookupError: pass
            try: proc.wait(timeout=wait_s); break
            except subprocess.TimeoutExpired: continue
        if reader is not None and reader is not threading.current_thread():
            # Lock-safety invariant: join() must keep a finite timeout so stop_recording() held
            # under Daemon._trigger_lock never deadlocks with an exit callback awaiting _trigger_lock.
            reader.join(timeout=1.0)
        if err_reader is not None and err_reader is not threading.current_thread():
            err_reader.join(timeout=1.0)
        self._live_level, self._live_level_ts = 0.0, 0.0
        self.last_returncode = proc.returncode
        self.last_stderr = bytes(err_buf).decode("utf-8", errors="replace").strip()
        raw = bytes(buf)
        even_n = len(raw) - (len(raw) % 2)
        return pack_pcm16_mono_wav(raw[:even_n]) if even_n >= 2 else b""

    def diagnostic_summary(self) -> str:
        parts = [f"rc={self.last_returncode}"] if self.last_returncode is not None else []
        if self.last_stderr:
            parts.append("stderr=" + " ".join(self.last_stderr.splitlines())[-240:])
        return f"ffmpeg_empty({', '.join(parts)})" if parts else "ffmpeg_empty"


def stream_key_test_proc(cmd: list[str], *, env: Mapping[str, str] | None = None) -> int:
    proc = subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        env=None if env is None else dict(env),
    )
    try:
        assert proc.stdout is not None
        for ln in iter(proc.stdout.readline, ""):
            if ln.strip():
                print(ln.strip(), flush=True)
        return proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.terminate()


class _StdinGate:
    """Mutex for shim stdin writes with indivisible deferred-close handoff on release."""

    def __init__(self) -> None:
        self._io_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._writers = 0
        self._deferred: list[Any] = []

    def acquire(self, blocking: bool = True, timeout: float = -1.0) -> bool:
        ok = (
            self._io_lock.acquire(blocking=blocking)
            if timeout < 0
            else self._io_lock.acquire(blocking=blocking, timeout=timeout)
        )
        if ok:
            with self._state_lock:
                self._writers += 1
        return ok

    def release(self) -> None:
        while True:
            with self._state_lock:
                if not self._deferred:
                    self._writers -= 1
                    self._io_lock.release()
                    return
                items = list(self._deferred)
                self._deferred.clear()
            for pipe in items:
                try:
                    pipe.close()
                except Exception:
                    pass

    def __enter__(self) -> _StdinGate:
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()

    def close_or_defer(self, pipe: Any, *, timeout: float = 0.2) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            rem = deadline - time.monotonic()
            got = (
                self.acquire(timeout=rem)
                if rem > 0.0
                else self.acquire(blocking=False)
            )
            if got:
                try:
                    if pipe is not None:
                        try:
                            pipe.close()
                        except Exception:
                            pass
                finally:
                    self.release()
                return True
            with self._state_lock:
                if pipe is None and not self._deferred:
                    return True
                if self._writers > 0:
                    if pipe is not None:
                        self._deferred.append(pipe)
                    return False
            time.sleep(0)


def _reap_shim_proc(
    proc: subprocess.Popen[str] | None,
    *,
    stdin_lock: _StdinGate | threading.Lock | None = None,
) -> None:
    if proc is None:
        if isinstance(stdin_lock, _StdinGate):
            stdin_lock.close_or_defer(None, timeout=0.2)
        return
    try:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=1.0)
    except Exception:
        pass
    if proc.stdin is not None:
        if stdin_lock is None:
            try:
                proc.stdin.close()
            except Exception:
                pass
        elif isinstance(stdin_lock, _StdinGate):
            if not stdin_lock.close_or_defer(proc.stdin, timeout=0.2):
                setattr(proc, "_deferred_close_stdin", True)
        elif stdin_lock.acquire(timeout=0.2):
            try:
                proc.stdin.close()
            except Exception:
                pass
            finally:
                stdin_lock.release()
        else:
            setattr(proc, "_deferred_close_stdin", True)
    reader_th = getattr(proc, "_shim_reader_th", None)
    if isinstance(reader_th, threading.Thread):
        if reader_th.is_alive() and reader_th is not threading.current_thread():
            reader_th.join(timeout=0.5)
        if not reader_th.is_alive() and proc.stdout is not None:
            try:
                proc.stdout.close()
            except Exception:
                pass
    elif proc.stdout is not None:
        try:
            proc.stdout.close()
        except Exception:
            pass


class JsonlShimMixin:
    _shim_graceful: bool = False
    _shim_ready_timeout: float = 8.0
    _shim_label: str = "shim"
    _shim_backoff_base: float = 0.5
    _shim_backoff_cap: float = 8.0
    _shim_respawn_delays: tuple[float, ...] = (0.3, 1.0, 2.0, 4.0, 8.0)
    cfg: Config
    env: dict[str, str]

    def _init_shim_bridge(self) -> None:
        self._closed = False
        self._close_evt = threading.Event()
        self._shim: subprocess.Popen[str] | None = None
        self._shim_disabled = False
        self._shim_lock = threading.Lock()
        self._shim_cond = threading.Condition(self._shim_lock)
        self._spawning_shim: bool = False
        self._spawned_shim: subprocess.Popen[str] | None = None
        self._spawned_ready: bool = False
        self._shim_caps: frozenset[str] = frozenset()
        self._shim_ever_ready: bool = False
        self._shim_fail_count: int = 0
        self._shim_retry_after: float = 0.0
        self._respawn_scheduled: bool = False
        self._stdin_lock = _StdinGate()
        self._rpc_seq, self._pending, self._ready = 100, {}, threading.Event()
        self._event_q: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._event_worker: threading.Thread | None = None
        self._on_event: Callable[[dict[str, Any]], None] | None = None
        self._level_stop_evt: threading.Event | None = None
        self._level_thread: threading.Thread | None = None
        self._last_level_push_ts: float = 0.0
        self._win_ui_culture = ""
        self.last_shim_error = ""

    def _host_ui_culture(self) -> str | None:
        # Non-WSL platforms fall back to POSIX locale vars when the shim omits ui_culture.
        return self._win_ui_culture or None

    def _disable_shim_once(self, reason: str) -> None:
        self.last_shim_error = redact_secrets(reason, self.env, cfg=self.cfg)
        if not self._shim_disabled:
            self._shim_disabled = True
            if self.last_shim_error:
                print(
                    f"[agent-voice-input] {self._shim_label} unavailable ({self.last_shim_error}); "
                    "global hotkey & HUD disabled, 'toggle'/'once' remain available.",
                    file=sys.stderr, flush=True,
                )

    _INTERNAL_SHIM_EVENTS: frozenset[str] = frozenset(
        {"rec_lost", "rec_recovered", "audio_devices_changed"}
    )

    def _has_managed_shim(self) -> bool:
        if self._closed or self._shim_disabled:
            return False
        if self._shim_graceful:
            return (self._shim is not None and self._shim.poll() is None) or self._on_event is not None
        return self._shim is not None or self._on_event is not None

    def _on_shim_internal_event(self, msg: dict[str, Any]) -> None:
        pass

    def _ensure_event_worker(self) -> None:
        with self._shim_lock:
            if self._event_worker is None or not self._event_worker.is_alive():
                self._event_worker = threading.Thread(target=self._event_loop, daemon=True)
                self._event_worker.start()

    def _flush_shim_events(self, timeout_sec: float = 0.05) -> None:
        if (
            self._event_worker is None
            or not self._event_worker.is_alive()
            or threading.current_thread() is self._event_worker
        ):
            return
        done = threading.Event()
        self._event_q.put({"_sync_evt": done})
        done.wait(timeout=max(0.005, timeout_sec))

    def _event_loop(self) -> None:
        while (msg := self._event_q.get()) is not None:
            if isinstance(sync_evt := msg.get("_sync_evt"), threading.Event):
                sync_evt.set()
                continue
            if str(msg.get("event") or "") in self._INTERNAL_SHIM_EVENTS:
                try:
                    self._on_shim_internal_event(msg)
                except Exception:
                    pass
                continue
            if (cb := self._on_event) is not None:
                try: cb(msg)
                except Exception: pass

    def _build_shim_cmd(self) -> list[str] | None:
        raise NotImplementedError

    def _schedule_respawn_if_needed(self) -> None:
        start_bg = False
        with self._shim_lock:
            if (
                not self._closed
                and not self._shim_disabled
                and self._on_event is not None
                and not self._respawn_scheduled
            ):
                self._respawn_scheduled = True
                start_bg = True
        if start_bg:
            threading.Thread(target=self._respawn_shim_bg, daemon=True).start()

    def _ensure_shim(self, *, ignore_cooldown: bool = False) -> bool:
        if self._closed:
            if self._shim_graceful:
                return False
            raise RuntimeError(f"{type(self).__name__} is closed")
        if self._shim_disabled:
            return False
        dead: subprocess.Popen[str] | None = None
        proc_ready: threading.Event | None = None
        proc: subprocess.Popen[str] | None = None
        spawn_started = False
        try:
            with self._shim_lock:
                wait_deadline = time.monotonic() + max(0.2, float(self._shim_ready_timeout) + 0.5)
                while self._spawning_shim and not self._closed and not self._shim_disabled:
                    rem_w = wait_deadline - time.monotonic()
                    if rem_w <= 0.0:
                        break
                    self._shim_cond.wait(timeout=rem_w)
                if self._closed:
                    if self._shim_graceful:
                        return False
                    raise RuntimeError(f"{type(self).__name__} is closed")
                if self._shim_disabled:
                    return False
                if self._shim is not None and self._shim.poll() is None:
                    if self._shim is not self._spawned_shim or self._spawned_ready:
                        return True
                if self._spawning_shim:
                    return False
                if not ignore_cooldown and time.monotonic() < self._shim_retry_after:
                    return False
                dead, self._shim = self._shim, None
                self._spawned_shim = None
                self._spawned_ready = False
                self._shim_caps = frozenset()
                self._spawning_shim = True
                try:
                    cmd = self._build_shim_cmd()
                    if not cmd:
                        if self._shim_graceful:
                            self._disable_shim_once(self.last_shim_error or f"{self._shim_label} unavailable")
                        else:
                            raise RuntimeError(self.last_shim_error or f"{self._shim_label} unavailable")
                    else:
                        self._ready.clear()
                        proc_ready = threading.Event()
                        try:
                            proc = subprocess.Popen(
                                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, encoding="utf-8", errors="replace", bufsize=1,
                                env=self.env if self._shim_graceful else None,
                            )
                        except OSError as exc:
                            if self._shim_graceful:
                                self._disable_shim_once(f"failed to spawn {self._shim_label}: {exc}")
                            else:
                                raise
                        else:
                            self._shim = proc
                            self._spawned_shim = proc
                            reader_th = threading.Thread(target=self._shim_reader, args=(proc, proc_ready), daemon=True)
                            setattr(proc, "_shim_reader_th", reader_th)
                            reader_th.start()
                            spawn_started = True
                finally:
                    if not spawn_started:
                        self._spawning_shim = False
                        self._shim_cond.notify_all()
        finally:
            if dead is not None:
                self._reap_owned_shim(dead)
        if not spawn_started:
            return False
        assert proc is not None and proc_ready is not None
        proc_ready.wait(timeout=self._shim_ready_timeout)
        reap_failed: subprocess.Popen[str] | None = None
        closed_err = False
        with self._shim_lock:
            self._spawning_shim = False
            self._shim_cond.notify_all()
            if self._closed:
                if self._shim is proc:
                    self._shim = None
                    self._spawned_shim = None
                    self._spawned_ready = False
                    self._shim_caps = frozenset()
                reap_failed = proc
                closed_err = not self._shim_graceful
            else:
                ok = (
                    not self._shim_disabled
                    and self._shim is proc
                    and proc.poll() is None
                    and self._spawned_ready
                )
                if ok:
                    self._shim_ever_ready = True
                    self._shim_fail_count = 0
                    self._shim_retry_after = 0.0
                    self.last_shim_error = ""
                    return True
                if self._shim is proc:
                    self._shim = None
                    self._spawned_shim = None
                    self._spawned_ready = False
                    self._shim_caps = frozenset()
                reap_failed = proc
                self._shim_fail_count += 1
                backoff = min(
                    float(self._shim_backoff_cap),
                    float(self._shim_backoff_base) * (2 ** min(self._shim_fail_count, 4)),
                )
                self._shim_retry_after = time.monotonic() + backoff
                if not self.last_shim_error:
                    self.last_shim_error = f"{self._shim_label} failed to become ready"
                if self._shim_graceful and not self._shim_ever_ready:
                    # Fatal early exit / ok=false disables immediately; pure readiness timeout
                    # (proc still alive, e.g. macOS cold XProtect scan) gets up to 2 bg retries.
                    if proc.poll() is not None or self._shim_fail_count >= 3:
                        self._disable_shim_once(self.last_shim_error)
        self._reap_owned_shim(reap_failed)
        if closed_err:
            raise RuntimeError(f"{type(self).__name__} is closed")
        if not ignore_cooldown:
            self._schedule_respawn_if_needed()
        return False

    def _reap_owned_shim(self, proc: subprocess.Popen[str] | None) -> None:
        _reap_shim_proc(proc, stdin_lock=self._stdin_lock)

    def _respawn_shim_bg(self) -> None:
        succeeded = False
        try:
            for delay in self._shim_respawn_delays:
                if self._close_evt.wait(max(0.0, float(delay))):
                    return
                if self._closed or self._shim_disabled or self._on_event is None:
                    return
                try:
                    if self._ensure_shim(ignore_cooldown=True):
                        succeeded = True
                        return
                except Exception:
                    pass
                if self._closed or self._shim_disabled or self._on_event is None:
                    return
        finally:
            restart_again = False
            with self._shim_lock:
                self._respawn_scheduled = False
                if (
                    succeeded
                    and not self._closed
                    and not self._shim_disabled
                    and self._on_event is not None
                    and (self._shim is None or self._shim.poll() is not None)
                ):
                    self._respawn_scheduled = True
                    restart_again = True
            if restart_again:
                threading.Thread(target=self._respawn_shim_bg, daemon=True).start()

    def _shim_reader(
        self,
        proc: subprocess.Popen[str],
        proc_ready: threading.Event | None = None,
    ) -> None:
        assert proc.stdout is not None
        managed_spawn = proc_ready is not None
        got_ready = False
        try:
            for raw in iter(proc.stdout.readline, ""):
                if not raw.strip():
                    continue
                try: msg = json.loads(raw)
                except ValueError: continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("event") == "ready":
                    is_current = False
                    with self._shim_lock:
                        if self._shim is proc or (not managed_spawn and self._shim is None):
                            is_current = True
                            got_ready = True
                            if self._shim is proc:
                                self._spawned_ready = True
                            raw_caps = msg.get("caps")
                            if isinstance(raw_caps, list):
                                self._shim_caps = frozenset(
                                    str(c).strip()
                                    for c in raw_caps
                                    if isinstance(c, str) and str(c).strip()
                                )
                            else:
                                self._shim_caps = frozenset()
                            self._win_ui_culture = str(msg.get("ui_culture") or "").strip()
                    if is_current:
                        if "audio_status" in msg and isinstance(msg.get("audio_status"), dict):
                            try:
                                self._on_shim_internal_event({"event": "audio_devices_changed", **msg["audio_status"]})
                            except Exception:
                                pass
                        self._ready.set()
                    if proc_ready is not None:
                        proc_ready.set()
                elif "event" in msg and (
                    self._on_event is not None
                    or str(msg.get("event") or "") in self._INTERNAL_SHIM_EVENTS
                ):
                    with self._shim_lock:
                        stale_proc = (
                            (self._shim is not proc)
                            if managed_spawn
                            else (self._shim is not None and self._shim is not proc)
                        )
                    if not stale_proc:
                        self._ensure_event_worker()
                        self._event_q.put(msg)
                elif "reply" in msg or msg.get("type") == "reply":
                    with self._shim_lock:
                        entry = self._pending.get(int(msg.get("seq_id") or 0))
                    if entry is not None:
                        owner_proc, wait_q = entry if isinstance(entry, tuple) else (proc, entry)
                        if owner_proc is proc:
                            try: wait_q.put_nowait(msg)
                            except queue.Full: pass
                elif self._shim_graceful and not got_ready and msg.get("ok") is False:
                    with self._shim_lock:
                        is_cur_err = self._shim is proc or (not managed_spawn and self._shim is None)
                    if is_cur_err:
                        self._disable_shim_once(str(msg.get("error") or f"{self._shim_label} startup failed"))
                        self._ready.set()
                    if proc_ready is not None:
                        proc_ready.set()
        except (OSError, ValueError, RuntimeError):
            pass
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
            if proc_ready is not None:
                proc_ready.set()
            need_respawn = False
            stale_queues: list[queue.Queue[dict[str, Any]]] = []
            with self._shim_lock:
                for k_sid, val in list(self._pending.items()):
                    owner_proc, wait_q = val if isinstance(val, tuple) else (proc, val)
                    if owner_proc is proc:
                        stale_queues.append(wait_q)
                        self._pending.pop(k_sid, None)
                if self._shim is proc:
                    self._shim = None
                    self._spawned_shim = None
                    self._spawned_ready = False
                    self._shim_caps = frozenset()
                    self._ready.set()
                    if not got_ready and self._shim_graceful and not self._closed and not self._shim_ever_ready:
                        self._disable_shim_once(self.last_shim_error or f"{self._shim_label} exited before ready")
                    if (
                        got_ready
                        and not self._closed
                        and not self._shim_disabled
                        and self._on_event is not None
                        and not self._respawn_scheduled
                    ):
                        self._respawn_scheduled = True
                        need_respawn = True
            self._reap_owned_shim(proc)
            for q in stale_queues:
                try: q.put_nowait({"error": "shim_eof"})
                except queue.Full: pass
            if need_respawn:
                threading.Thread(target=self._respawn_shim_bg, daemon=True).start()

    def _shim_rpc(
        self,
        payload: dict[str, Any],
        timeout_sec: float = 5.0,
        *,
        allow_retry: bool = True,
        before_write: Callable[[], None] | None = None,
    ) -> dict[str, Any] | None:
        max_attempts = 2 if allow_retry else 1
        for attempt in range(max_attempts):
            if not self._ensure_shim():
                if self._shim_graceful:
                    return None
                raise RuntimeError(self.last_shim_error or f"{self._shim_label} unavailable")
            sid = 0
            q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
            sent_proc: subprocess.Popen[str] | None = None
            broken: subprocess.Popen[str] | None = None
            try:
                with self._shim_lock:
                    proc = self._shim
                    if proc is None or proc.stdin is None or proc.poll() is not None:
                        broken, self._shim = proc, None
                        self._spawned_shim = None
                        self._spawned_ready = False
                        self._shim_caps = frozenset()
                        raise BrokenPipeError("shim_not_running")
                    sent_proc = proc
                    self._rpc_seq += 1
                    sid = self._rpc_seq
                    self._pending[sid] = (proc, q)
                line = json.dumps({**payload, "seq_id": sid}, ensure_ascii=False) + "\n"
                with self._stdin_lock:
                    if before_write is not None:
                        before_write()
                    if (
                        sent_proc.poll() is not None
                        or getattr(sent_proc, "_deferred_close_stdin", False)
                        or sent_proc.stdin is None
                        or sent_proc.stdin.closed
                    ):
                        raise BrokenPipeError("shim_not_running")
                    try:
                        sent_proc.stdin.write(line)
                        sent_proc.stdin.flush()
                    except ValueError as exc:
                        raise BrokenPipeError("shim_stdin_closed") from exc
                res = q.get(timeout=timeout_sec)
                if res.get("error") == "shim_eof" and (attempt + 1) < max_attempts and not self._closed and not self._shim_disabled:
                    continue
                if "error" in res:
                    if self._shim_graceful:
                        return None
                    raise RuntimeError(str(res["error"]))
                return res
            except (BrokenPipeError, OSError, queue.Empty):
                if broken is not None:
                    self._reap_owned_shim(broken)
                if (attempt + 1) < max_attempts and not self._closed and not self._shim_disabled:
                    continue
                if self._shim_graceful:
                    return None
                raise
            finally:
                if sid:
                    with self._shim_lock:
                        cur_entry = self._pending.get(sid)
                        if cur_entry is not None:
                            owner_p = cur_entry[0] if isinstance(cur_entry, tuple) else sent_proc
                            if owner_p is sent_proc:
                                self._pending.pop(sid, None)
        if self._shim_graceful:
            return None
        raise RuntimeError("shim_rpc exhausted retries")

    def start_event_listener(self, on_hotkey_event: Callable[[dict[str, Any]], None]) -> None:
        self._closed = False
        self._close_evt.clear()
        self._shim_disabled = False
        self._on_event = on_hotkey_event
        self._ensure_event_worker()
        if not self._ensure_shim():
            self._schedule_respawn_if_needed()

    def hud(
        self,
        state: str,
        text: str = "",
        duration_ms: int = 0,
        *,
        layer: str | None = None,
        clear_main: bool | None = None,
        clear_toast: bool | None = None,
    ) -> None:
        if not self._has_managed_shim():
            return
        st = (state or "").strip().lower()
        try:
            if not self._ensure_shim():
                return
            if not self._ready.is_set():
                self._ready.wait(timeout=self._shim_ready_timeout)
            req: dict[str, Any] = {"cmd": "hud", "state": state}
            eff_text = text
            eff_duration_ms = duration_ms if duration_ms > 0 else 0
            base_layer = hud_layer_for_state(st)
            if base_layer != "control":
                exp_layer = (layer or "").strip().lower()
                eff_layer = exp_layer if exp_layer in ("main", "toast") else base_layer
                req["layer"] = eff_layer
                req["position"] = normalize_ui_position(self.cfg.ui.position)
                if st == "recording" and self.cfg.audio.max_duration_sec > 0:
                    req["max_duration_sec"] = max(1, int(round(self.cfg.audio.max_duration_sec)))
                    req["warn_remain_sec"] = recording_countdown_warn_sec(
                        self.cfg.audio.max_duration_sec
                    )
                if not eff_text:
                    lang = resolve_ui_language(
                        self.cfg.ui.language,
                        env=self.env,
                        win_ui_culture=self._host_ui_culture(),
                    )
                    eff_text = format_hud_text(
                        st,
                        lang,
                        hotkey=self.cfg.hotkey.key,
                        trigger_mode=self.cfg.hotkey.trigger_mode,
                    )
                if eff_duration_ms <= 0:
                    eff_duration_ms = default_hud_duration_ms(st)
                if clear_toast is None and st == "recording" and eff_layer == "main":
                    clear_toast = True
            if clear_main is not None:
                req["clear_main"] = bool(clear_main)
            if clear_toast is not None:
                req["clear_toast"] = bool(clear_toast)
            if eff_text:
                req["text"] = eff_text
            if eff_duration_ms > 0:
                req["duration_ms"] = eff_duration_ms
            self._shim_rpc(req, timeout_sec=1.5)
        except Exception:
            pass

    def hud_level(self, level: float) -> None:
        if not self._has_managed_shim() or not self._ready.is_set():
            return
        proc = self._shim
        if proc is None or proc.stdin is None or proc.poll() is not None:
            return
        clamped = round(max(0.0, min(1.0, float(level))), 3)
        line = json.dumps({"cmd": "hud_level", "level": clamped}) + "\n"
        try:
            with self._stdin_lock:
                if (
                    proc.poll() is None
                    and not getattr(proc, "_deferred_close_stdin", False)
                    and proc.stdin is not None
                    and not proc.stdin.closed
                ):
                    proc.stdin.write(line)
                    proc.stdin.flush()
        except Exception:
            pass

    def _on_recorder_level(self, level: float) -> None:
        if self._closed or not self._has_managed_shim():
            return
        now = time.monotonic()
        if (now - self._last_level_push_ts) < 0.045:
            return
        self._last_level_push_ts = now
        self.hud_level(level)

    def _start_level_streamer(self, source_fn: Callable[[], float]) -> None:
        self._stop_level_streamer()
        if not self._has_managed_shim():
            return
        stop_evt = threading.Event()

        def _stream_loop() -> None:
            while not stop_evt.wait(0.075):
                if self._closed or not self._has_managed_shim():
                    break
                if (time.monotonic() - self._last_level_push_ts) < 0.07:
                    continue
                try:
                    lvl = float(source_fn())
                except Exception:
                    lvl = 0.0
                self.hud_level(lvl)

        worker = threading.Thread(target=_stream_loop, daemon=True)
        self._level_stop_evt = stop_evt
        self._level_thread = worker
        worker.start()

    def _stop_level_streamer(self) -> None:
        evt, worker = self._level_stop_evt, self._level_thread
        self._level_stop_evt, self._level_thread = None, None
        self._last_level_push_ts = 0.0
        if evt is not None:
            evt.set()
        if worker is not None and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=0.5)

    def close(self) -> None:
        self._closed = True
        self._close_evt.set()
        self._stop_level_streamer()
        self._on_event = None
        self._event_q.put(None)
        if self._event_worker is not None and self._event_worker.is_alive() and self._event_worker is not threading.current_thread():
            self._event_worker.join(timeout=1.0)
        if (cancel := getattr(self, "cancel_recording", None)) is not None:
            cancel()
        with self._shim_lock:
            proc, self._shim = self._shim, None
            self._spawned_shim = None
            self._spawned_ready = False
            self._shim_caps = frozenset()
            self._shim_cond.notify_all()
        if proc is not None and proc.poll() is None:
            quit_sent = False
            if self._stdin_lock.acquire(timeout=0.2):
                try:
                    if proc.stdin and not proc.stdin.closed:
                        try:
                            fd = proc.stdin.fileno()
                        except (AttributeError, io.UnsupportedOperation, OSError):
                            proc.stdin.write('{"cmd":"quit"}\n')
                            proc.stdin.flush()
                            proc.stdin.close()
                            quit_sent = True
                        else:
                            os.set_blocking(fd, False)
                            os.write(fd, b'{"cmd":"quit"}\n')
                            quit_sent = True
                            try:
                                proc.stdin.close()
                            except Exception:
                                pass
                except Exception:
                    pass
                finally:
                    self._stdin_lock.release()
            if quit_sent:
                try:
                    proc.wait(timeout=2.0)
                except Exception:
                    pass
        self._reap_owned_shim(proc)


def make_once_commit_guard(
    cancelled_fn: Callable[[], bool],
    before_commit: Callable[[], bool] | None,
) -> Callable[[], bool]:
    checked = False
    allowed = True

    def _confirm() -> bool:
        nonlocal checked, allowed
        if not checked:
            checked = True
            allowed = (
                False
                if cancelled_fn()
                else (True if before_commit is None else bool(before_commit()))
            )
        return allowed

    return _confirm
