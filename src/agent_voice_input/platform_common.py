"""Cross-platform window/paste models, FfmpegWavRecorder, and JsonlShimMixin bridge."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import io
import json
import os
from pathlib import Path
import queue
import select
import signal
import stat
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Mapping, Protocol

from .audio_proc import pack_pcm16_mono_wav, resolve_ffmpeg_bin
from .config import Config, ConfigError, normalize_ui_position, redact_secrets
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
    page_url: str = ""
    origin_drifted: bool = False

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
        self._startup_evt: threading.Event | None = None
        self._aborted = threading.Event()
        self._force_kill = False
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

    def reset_abort(self) -> None:
        """Clear abort latch before reusing a recorder instance sequentially."""
        self._force_kill = False
        self._aborted.clear()

    def abort(self) -> None:
        """Wake any startup or tail-drain wait and force-kill an active subprocess."""
        self._force_kill = True
        self._aborted.set()
        with self._lock:
            startup_evt = self._startup_evt
            proc = self._proc
        if startup_evt is not None:
            startup_evt.set()
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass

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
        if self._aborted.is_set():
            raise MicrophoneUnavailableError(
                "microphone recording aborted",
                reason_code="mic_unavailable",
                hud_state="mic_unavailable",
            )
        if self._proc is not None:
            self._stop_impl(drain_sec=0.0)
        cmd = self._build_cmd(fmt, source)
        if self._aborted.is_set():
            raise MicrophoneUnavailableError(
                "microphone recording aborted",
                reason_code="mic_unavailable",
                hud_state="mic_unavailable",
            )
        buf, err_buf = bytearray(), bytearray()
        startup_evt = threading.Event()
        start_ts = time.monotonic()
        self.last_stderr, self.last_returncode, self.first_chunk_ts, self.last_chunk_ts = "", None, 0.0, 0.0

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            start_new_session=True,
            env=dict(os.environ if env is None else env),
        )
        err_reader: threading.Thread | None = None
        reader: threading.Thread | None = None
        wd_evt: threading.Event | None = None
        wd_thr: threading.Thread | None = None
        try:
            assert proc.stdout is not None and proc.stderr is not None
            err_reader = threading.Thread(target=self._read_pipe, args=(proc.stderr, err_buf, 512), daemon=True)
            reader = threading.Thread(
                target=self._read_stdout,
                args=(proc.stdout, buf, startup_evt, proc, err_reader, err_buf),
                daemon=True,
            )
            with self._lock:
                if self._aborted.is_set():
                    raise MicrophoneUnavailableError(
                        "microphone recording aborted",
                        reason_code="mic_unavailable",
                        hud_state="mic_unavailable",
                    )
                self._proc, self._reader, self._err_reader, self._buf, self._err_buf = (
                    proc,
                    reader,
                    err_reader,
                    buf,
                    err_buf,
                )
                self._wd_stop_evt, self._wd_thread = None, None
                self._startup_evt = startup_evt
                self._stdout_eof = False
                self._stopping = False
                self._stall_triggered = False
                self._last_chunk_ts = 0.0
                self._unexpected_exit_cb = None
                self._level_cb = on_level
                self._live_level, self._live_level_ts = 0.0, 0.0
            err_reader.start()
            reader.start()

            if verify_startup:
                deadline = time.monotonic() + max(0.02, float(startup_timeout_sec))
                while not startup_evt.is_set():
                    if self._aborted.is_set():
                        raise MicrophoneUnavailableError(
                            "microphone recording aborted",
                            reason_code="mic_unavailable",
                            hud_state="mic_unavailable",
                        )
                    rem = deadline - time.monotonic()
                    if rem <= 0.0:
                        break
                    startup_evt.wait(timeout=min(0.03, rem))
                if self._aborted.is_set():
                    raise MicrophoneUnavailableError(
                        "microphone recording aborted",
                        reason_code="mic_unavailable",
                        hud_state="mic_unavailable",
                    )
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
                    self._stop_impl(drain_sec=0.0)
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
                with self._lock:
                    if self._aborted.is_set():
                        raise MicrophoneUnavailableError(
                            "microphone recording aborted",
                            reason_code="mic_unavailable",
                            hud_state="mic_unavailable",
                        )
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

            with self._lock:
                if self._startup_evt is startup_evt:
                    self._startup_evt = None
                if self._aborted.is_set():
                    raise MicrophoneUnavailableError(
                        "microphone recording aborted",
                        reason_code="mic_unavailable",
                        hud_state="mic_unavailable",
                    )
        except BaseException:
            with self._lock:
                if self._startup_evt is startup_evt:
                    self._startup_evt = None
                if self._proc is proc:
                    self._proc, self._reader, self._err_reader, self._buf, self._err_buf = (
                        None,
                        None,
                        None,
                        bytearray(),
                        bytearray(),
                    )
                    self._wd_stop_evt, self._wd_thread = None, None
                    self._stdout_eof = False
                    self._stopping = True
                    self._stall_triggered = False
                    self._last_chunk_ts = 0.0
                    self._unexpected_exit_cb = None
                    self._level_cb = None
                    self._live_level, self._live_level_ts = 0.0, 0.0
            if wd_evt is not None:
                wd_evt.set()
            if wd_thr is not None and wd_thr.ident is not None and wd_thr is not threading.current_thread():
                wd_thr.join(timeout=0.5)
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                try:
                    proc.wait(timeout=1.0)
                except Exception:
                    pass
            if reader is not None and reader.ident is not None and reader is not threading.current_thread():
                reader.join(timeout=1.0)
            if err_reader is not None and err_reader.ident is not None and err_reader is not threading.current_thread():
                err_reader.join(timeout=1.0)
            for pipe, thr in ((proc.stdout, reader), (proc.stderr, err_reader)):
                if pipe is not None and (thr is None or not thr.is_alive()):
                    try:
                        pipe.close()
                    except Exception:
                        pass
            raise

    def stop(self, *, drain_sec: float = 0.0) -> bytes:
        if drain_sec <= 0.0:
            self._aborted.set()
        return self._stop_impl(drain_sec=drain_sec)

    def _stop_impl(self, *, drain_sec: float = 0.0) -> bytes:
        with self._lock:
            proc, reader, err_reader, buf, err_buf = self._proc, self._reader, self._err_reader, self._buf, self._err_buf
            wd_evt, wd_thr = self._wd_stop_evt, self._wd_thread
            startup_evt = self._startup_evt
            self._proc, self._reader, self._err_reader, self._buf, self._err_buf = None, None, None, bytearray(), bytearray()
            self._wd_stop_evt, self._wd_thread = None, None
            self._startup_evt = None
            self._stopping = True
            self._stdout_eof = False
            self._unexpected_exit_cb = None
            self._level_cb = None
            self._live_level, self._live_level_ts = 0.0, 0.0
        if startup_evt is not None:
            startup_evt.set()
        if wd_evt is not None:
            wd_evt.set()
        if wd_thr is not None and wd_thr.ident is not None and wd_thr is not threading.current_thread():
            wd_thr.join(timeout=0.5)
        if proc is None:
            return b""
        aborted_during_drain = False
        if drain_sec > 0.0 and proc.poll() is None:
            aborted_during_drain = self._aborted.wait(timeout=drain_sec)
        sig_plan = (
            ((signal.SIGKILL, 1.0),)
            if (self._force_kill or aborted_during_drain)
            else ((signal.SIGTERM, 0.18), (signal.SIGKILL, 1.0))
        )
        for sig, wait_s in sig_plan:
            if proc.poll() is not None:
                break
            try: os.killpg(proc.pid, sig)
            except ProcessLookupError: pass
            except OSError:
                try: proc.send_signal(sig)
                except ProcessLookupError: pass
            try: proc.wait(timeout=wait_s); break
            except subprocess.TimeoutExpired: continue
        if reader is not None and reader.ident is not None and reader is not threading.current_thread():
            # Lock-safety invariant: join() must keep a finite timeout so stop_recording() held
            # under Daemon._trigger_lock never deadlocks with an exit callback awaiting _trigger_lock.
            reader.join(timeout=1.0)
        if err_reader is not None and err_reader.ident is not None and err_reader is not threading.current_thread():
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


class _ShimWriteTimeout(TimeoutError):
    """Raised when writing a JSONL command to shim stdin exceeds the RPC deadline.

    write_status is either:
    - "not_sent": zero bytes entered stdin (healthy shim kept alive);
    - "partial": a non-empty prefix entered the pipe before timeout (stdin closed & shim poisoned).
    """

    def __init__(self, message: str, *, write_status: str = "not_sent") -> None:
        super().__init__(message)
        self.write_status = write_status
        self.partial = write_status == "partial"
        self.written = self.partial


def _stream_pipe_fd(pipe: Any) -> int | None:
    """Return POSIX fd only for real pipes/sockets (leaving StringIO/files/mocks on .write())."""
    if os.name != "posix" or pipe is None:
        return None
    try:
        fd = pipe.fileno()
    except (AttributeError, io.UnsupportedOperation, OSError, ValueError):
        return None
    if not isinstance(fd, int) or fd < 0:
        return None
    try:
        mode = os.fstat(fd).st_mode
    except OSError:
        return None
    return fd if (stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)) else None


def _dump_jsonl(payload: Mapping[str, Any]) -> str:
    """Serialize one JSONL frame and replace lone UTF-16 surrogates so UTF-8 encoding never raises."""
    return (
        json.dumps(dict(payload), ensure_ascii=False)
        .encode("utf-8", errors="replace")
        .decode("utf-8")
        + "\n"
    )


def _write_line_to_pipe(
    pipe: Any,
    line: str,
    deadline: float,
    abort_evt: threading.Event,
    *,
    nonblocking_start: bool = False,
) -> tuple[str, str]:
    """Write one JSONL line to a shim stdin stream with abortable poll deadline.

    Returns ``(status, reason)`` where ``status`` is strictly ``"sent"``, ``"not_sent"``,
    or ``"partial"``. Compatible with both real POSIX pipe fds and in-memory streams.
    """
    if pipe is None or getattr(pipe, "closed", False):
        return "not_sent", "closed"
    raw = line.encode("utf-8", errors="replace")
    fd = _stream_pipe_fd(pipe)
    if fd is None:
        if abort_evt.is_set():
            return "not_sent", "closed"
        if not nonblocking_start and time.monotonic() >= deadline:
            return "not_sent", "timeout"
        try:
            pipe.write(raw.decode("utf-8"))
            pipe.flush()
        except (BrokenPipeError, OSError, ValueError):
            return "not_sent", "broken"
        return "sent", ""

    view = memoryview(raw)
    written = 0
    eff_deadline = deadline
    try:
        os.set_blocking(fd, False)
    except OSError:
        return "not_sent", "broken"

    while written < len(view):
        if abort_evt.is_set():
            return ("partial", "aborted") if written > 0 else ("not_sent", "closed")
        if written == 0 and nonblocking_start:
            wait_s = 0.0
        else:
            rem = eff_deadline - time.monotonic()
            if rem <= 0.0:
                return ("partial", "timeout") if written > 0 else ("not_sent", "timeout")
            wait_s = min(rem, 0.02)
        try:
            _, ready_w, _ = select.select([], [fd], [], wait_s)
        except (OSError, ValueError):
            return ("partial", "broken") if written > 0 else ("not_sent", "broken")
        if not ready_w:
            if written == 0 and nonblocking_start:
                return "not_sent", "busy"
            continue
        try:
            n = os.write(fd, view[written:])
        except BlockingIOError:
            if written == 0 and nonblocking_start:
                return "not_sent", "busy"
            continue
        except OSError:
            return ("partial", "broken") if written > 0 else ("not_sent", "broken")
        if n <= 0:
            return ("partial", "broken") if written > 0 else ("not_sent", "broken")
        if written == 0 and nonblocking_start and n < len(view):
            eff_deadline = time.monotonic() + 0.005
        written += n
    return "sent", ""


@dataclass(slots=True)
class _WriterItem:
    line: str
    deadline: float | None
    is_rpc: bool = False
    is_hud: bool = False
    mergeable_hud: bool = False
    is_quit: bool = False
    state: str = "queued"
    cancelled: bool = False
    status: str = "not_sent"
    reason: str = ""
    done: threading.Event | None = None


class _ShimWriter:
    """Single-owner writer thread for one shim subprocess stdin pipe.

    Owns all writes and the sole close() of ``proc.stdin``:
    - Ordered FIFO queue for RPC frames, HUD state frames, cleanup commands, and quit sentinel.
    - Single overwrite slot for ``hud_level`` (never blocks, never accumulates backlog).
    - Bounded HUD queue (evicts oldest mergeable intermediate stage updates before toasts/hide).
    """

    _HUD_QUEUE_MAXLEN: int = 64
    _TERMINAL_HUD_WRITE_TIMEOUT_SEC: float = 5.0

    def __init__(
        self,
        proc: subprocess.Popen[str],
        *,
        on_poison: Callable[[subprocess.Popen[str], _ShimWriter], None] | None = None,
    ) -> None:
        self.proc = proc
        self._pipe = proc.stdin
        self._on_poison = on_poison
        self._cond = threading.Condition()
        self._fifo: deque[_WriterItem] = deque()
        self._hud_queued_count: int = 0
        self._pending_level: str | None = None
        self._closing: bool = False
        self._closed: bool = False
        self._poisoned: bool = False
        self._quit_sent: bool = False
        self._pipe_closed: bool = False
        self._poison_notified: bool = False
        self._abort_evt = threading.Event()
        try:
            setattr(proc, "_shim_writer_obj", self)
        except Exception:
            pass
        self._thread = threading.Thread(
            target=self._run, name="avi-shim-writer", daemon=True
        )
        self._thread.start()

    @property
    def is_open(self) -> bool:
        with self._cond:
            return not self._closed and not self._closing and not self._poisoned

    @property
    def poisoned(self) -> bool:
        with self._cond:
            return self._poisoned

    def _close_pipe_once(self) -> None:
        if self._pipe_closed:
            return
        self._pipe_closed = True
        if self._pipe is not None:
            try:
                self._pipe.close()
            except Exception:
                pass

    def _finish_item_locked(self, item: _WriterItem, status: str, reason: str) -> None:
        item.status = status
        item.reason = reason
        item.state = "done"
        if item.done is not None:
            item.done.set()

    def _drain_pending_locked(self, reason: str) -> None:
        while self._fifo:
            it = self._fifo.popleft()
            if it.is_hud:
                self._hud_queued_count = max(0, self._hud_queued_count - 1)
            if not it.cancelled and it.state != "done":
                self._finish_item_locked(it, "not_sent", reason)
        self._pending_level = None

    def _notify_poison_once(self) -> None:
        if self._poison_notified:
            return
        self._poison_notified = True
        cb = self._on_poison
        if cb is not None:
            try:
                cb(self.proc, self)
            except Exception:
                pass

    def write_sync(self, line: str, deadline: float) -> tuple[str, str]:
        """Enqueue a synchronous frame and wait for its 3-state receipt (sent/not_sent/partial)."""
        done_evt = threading.Event()
        item = _WriterItem(line=line, deadline=deadline, is_rpc=True, done=done_evt)
        with self._cond:
            if self._closed or self._closing or self._poisoned:
                return "not_sent", "closed"
            if time.monotonic() >= deadline:
                return "not_sent", "timeout"
            self._fifo.append(item)
            self._cond.notify()

        rem = max(0.0, deadline - time.monotonic())
        if done_evt.wait(timeout=rem):
            return item.status, item.reason

        with self._cond:
            if item.state == "done":
                return item.status, item.reason
            if item.state == "queued":
                item.cancelled = True
                self._finish_item_locked(item, "not_sent", "timeout")
                return "not_sent", "timeout"
        # Writer already picked up this item and checks item.deadline every <=20ms.
        done_evt.wait()
        return item.status, item.reason

    def enqueue_async(
        self,
        line: str,
        *,
        deadline_sec: float | None = 0.5,
        is_hud: bool = False,
        mergeable_hud: bool = False,
    ) -> bool:
        """Non-blocking FIFO enqueue for HUD state updates and fire-and-forget cleanup."""
        eff_deadline = (
            None
            if deadline_sec is None
            else (time.monotonic() + max(0.01, float(deadline_sec)))
        )
        item = _WriterItem(
            line=line,
            deadline=eff_deadline,
            is_hud=is_hud,
            mergeable_hud=mergeable_hud,
        )
        with self._cond:
            if self._closed or self._closing or self._poisoned:
                return False
            if is_hud:
                while self._hud_queued_count >= self._HUD_QUEUE_MAXLEN:
                    evict_idx: int | None = None
                    fallback_idx: int | None = None
                    for idx, cand in enumerate(self._fifo):
                        if cand.is_hud and not cand.cancelled:
                            if fallback_idx is None:
                                fallback_idx = idx
                            if cand.mergeable_hud:
                                evict_idx = idx
                                break
                    drop_idx = evict_idx if evict_idx is not None else fallback_idx
                    if drop_idx is None:
                        break
                    del self._fifo[drop_idx]
                    self._hud_queued_count = max(0, self._hud_queued_count - 1)
                self._hud_queued_count += 1
            self._fifo.append(item)
            self._cond.notify()
            return True

    def submit_level(self, line: str) -> None:
        """Overwrite the single pending hud_level slot without ever blocking."""
        with self._cond:
            if self._closed or self._closing or self._poisoned:
                return
            self._pending_level = line
            self._cond.notify()

    def close(self, *, send_quit: bool = False, timeout_sec: float = 0.25) -> bool:
        """Stop the writer thread, optionally flushing queued frames + quit first.

        Returns True iff ``send_quit=True`` and the quit line was written completely.
        """
        eff_to = max(0.02, float(timeout_sec))
        with self._cond:
            already_closed = self._closed
            if not already_closed:
                if send_quit and not self._closing and not self._poisoned:
                    self._closing = True
                    self._pending_level = None
                    self._fifo.append(
                        _WriterItem(
                            line='{"cmd":"quit"}\n',
                            deadline=time.monotonic() + eff_to,
                            is_quit=True,
                        )
                    )
                else:
                    self._closing = True
                    self._pending_level = None
                    self._abort_evt.set()
                self._cond.notify_all()
        cur = threading.current_thread()
        if self._thread.is_alive() and self._thread is not cur:
            self._thread.join(timeout=eff_to if send_quit else 0.5)
            if self._thread.is_alive():
                self._abort_evt.set()
                with self._cond:
                    self._cond.notify_all()
                self._thread.join(timeout=0.5)
        return self._quit_sent

    def _run(self) -> None:
        active_item: _WriterItem | None = None
        try:
            while True:
                item: _WriterItem | None = None
                level_line: str | None = None
                with self._cond:
                    while (
                        not self._fifo
                        and self._pending_level is None
                        and not self._closing
                        and not self._abort_evt.is_set()
                    ):
                        self._cond.wait()
                    if self._abort_evt.is_set():
                        self._drain_pending_locked("closed")
                        self._closed = True
                        break
                    while self._fifo:
                        cand = self._fifo.popleft()
                        if cand.is_hud:
                            self._hud_queued_count = max(0, self._hud_queued_count - 1)
                        if cand.cancelled:
                            continue
                        if self._closing and cand.is_rpc:
                            self._finish_item_locked(cand, "not_sent", "closed")
                            continue
                        cand.state = "writing"
                        item = cand
                        active_item = cand
                        break
                    if item is None:
                        if self._closing:
                            self._pending_level = None
                            self._closed = True
                            break
                        if self._pending_level is not None:
                            level_line = self._pending_level
                            self._pending_level = None

                if item is not None:
                    eff_deadline = (
                        item.deadline
                        if item.deadline is not None
                        else (time.monotonic() + self._TERMINAL_HUD_WRITE_TIMEOUT_SEC)
                    )
                    status, reason = _write_line_to_pipe(
                        self._pipe, item.line, eff_deadline, self._abort_evt
                    )
                    if item.is_quit:
                        with self._cond:
                            self._quit_sent = status == "sent"
                            self._finish_item_locked(item, status, reason)
                            active_item = None
                            self._drain_pending_locked("closed")
                            self._closed = True
                        break
                    if status == "partial":
                        self._close_pipe_once()
                        with self._cond:
                            self._poisoned = True
                            self._closed = True
                            self._drain_pending_locked("closed")
                        if not self._abort_evt.is_set():
                            self._notify_poison_once()
                        with self._cond:
                            self._finish_item_locked(item, "partial", reason)
                            active_item = None
                        break
                    if reason == "broken":
                        self._close_pipe_once()
                        with self._cond:
                            self._closed = True
                            self._finish_item_locked(item, "not_sent", "broken")
                            active_item = None
                            self._drain_pending_locked("closed")
                        break
                    with self._cond:
                        self._finish_item_locked(item, status, reason)
                        active_item = None
                elif level_line is not None:
                    status, reason = _write_line_to_pipe(
                        self._pipe,
                        level_line,
                        time.monotonic() + 0.005,
                        self._abort_evt,
                        nonblocking_start=True,
                    )
                    if status == "partial":
                        self._close_pipe_once()
                        with self._cond:
                            self._poisoned = True
                            self._closed = True
                            self._drain_pending_locked("closed")
                        if not self._abort_evt.is_set():
                            self._notify_poison_once()
                        break
                    if reason == "broken":
                        self._close_pipe_once()
                        with self._cond:
                            self._closed = True
                            self._drain_pending_locked("closed")
                        break
        finally:
            self._close_pipe_once()
            with self._cond:
                self._closed = True
                if active_item is not None and active_item.state != "done":
                    self._finish_item_locked(active_item, "not_sent", "broken")
                self._drain_pending_locked("closed")
                self._cond.notify_all()


def _reap_shim_proc(
    proc: subprocess.Popen[str] | None,
    *,
    writer: _ShimWriter | None = None,
) -> None:
    if writer is None and proc is not None:
        attached = getattr(proc, "_shim_writer_obj", None)
        if isinstance(attached, _ShimWriter) and attached.proc is proc:
            writer = attached
    if writer is not None:
        writer.close(send_quit=False)
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=1.0)
    except Exception:
        pass
    if writer is None and proc.stdin is not None and not getattr(proc.stdin, "closed", False):
        try:
            proc.stdin.close()
        except Exception:
            pass
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
        self._shim_writer: _ShimWriter | None = None
        self._live_writers: dict[int, _ShimWriter] = {}
        self._shim_disabled = False
        self._shim_lock = threading.Lock()
        self._shim_cond = threading.Condition(self._shim_lock)
        self._shim_build_lock = threading.Lock()
        self._spawning_shim: bool = False
        self._spawned_shim: subprocess.Popen[str] | None = None
        self._spawned_ready: bool = False
        self._shim_caps: frozenset[str] = frozenset()
        self._shim_instance_id: int = 0
        self._shim_ever_ready: bool = False
        self._shim_fail_count: int = 0
        self._shim_retry_after: float = 0.0
        self._respawn_scheduled: bool = False
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

    def is_shim_ok(self) -> bool:
        """Return True when the native shim bridge is healthy (ready when listening, no startup failure)."""
        if self._closed or self._shim_disabled:
            return False
        if self._on_event is not None:
            proc = self._shim
            writer = self._shim_writer
            return bool(
                proc is not None
                and proc.poll() is None
                and (proc is not self._spawned_shim or self._spawned_ready)
                and (writer is None or writer.proc is not proc or writer.is_open)
            )
        return self._shim_fail_count == 0 and not self.last_shim_error

    def _on_shim_internal_event(self, msg: dict[str, Any]) -> None:
        pass

    def _ensure_event_worker_locked(self) -> None:
        if self._closed:
            return
        if self._event_worker is None or not self._event_worker.is_alive():
            self._event_worker = threading.Thread(target=self._event_loop, daemon=True)
            self._event_worker.start()

    def _ensure_event_worker(self) -> None:
        with self._shim_lock:
            self._ensure_event_worker_locked()

    def _flush_shim_events(self, timeout_sec: float = 0.05) -> bool:
        if (
            self._event_worker is None
            or not self._event_worker.is_alive()
            or threading.current_thread() is self._event_worker
        ):
            return True
        done = threading.Event()
        self._event_q.put({"_sync_evt": done})
        return bool(done.wait(timeout=max(0.005, timeout_sec)))

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
        claimed_spawn = False
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
                    cur_w = self._shim_writer
                    writer_ok = cur_w is None or cur_w.proc is not self._shim or cur_w.is_open
                    if writer_ok and (self._shim is not self._spawned_shim or self._spawned_ready):
                        return True
                if self._spawning_shim:
                    return False
                if not ignore_cooldown and time.monotonic() < self._shim_retry_after:
                    return False
                dead, self._shim = self._shim, None
                if self._shim_writer is not None and self._shim_writer.proc is dead:
                    self._shim_writer = None
                self._spawned_shim = None
                self._spawned_ready = False
                self._shim_caps = frozenset()
                self._spawning_shim = True
                claimed_spawn = True

            if dead is not None:
                self._reap_owned_shim(dead)
                dead = None

            cmd: list[str] | None = None
            build_config_err: str | None = None
            with self._shim_build_lock:
                if not self._closed and not self._shim_disabled:
                    try:
                        cmd = self._build_shim_cmd()
                    except ConfigError as exc:
                        build_config_err = str(exc)

            with self._shim_lock:
                if self._closed:
                    if self._shim_graceful:
                        return False
                    raise RuntimeError(f"{type(self).__name__} is closed")
                if self._shim_disabled:
                    return False
                if build_config_err is not None:
                    self._disable_shim_once(build_config_err)
                    return False
                if not cmd:
                    if self._shim_graceful or self._shim_disabled:
                        self._disable_shim_once(self.last_shim_error or f"{self._shim_label} unavailable")
                        return False
                    raise RuntimeError(self.last_shim_error or f"{self._shim_label} unavailable")
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
                        return False
                    raise
                writer = _ShimWriter(proc, on_poison=self._on_writer_poison)
                self._live_writers[id(proc)] = writer
                self._spawned_shim = proc
                self._shim_writer = writer
                self._shim = proc
                self._shim_instance_id += 1
                setattr(proc, "_shim_instance_id", self._shim_instance_id)
                reader_th = threading.Thread(
                    target=self._shim_reader, args=(proc, proc_ready), daemon=True
                )
                setattr(proc, "_shim_reader_th", reader_th)
                reader_th.start()
                spawn_started = True
        finally:
            if claimed_spawn and not spawn_started:
                with self._shim_lock:
                    self._spawning_shim = False
                    self._shim_cond.notify_all()
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
                    self._shim_writer = None
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
                    self._shim_writer = None
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

    def _get_writer_locked(self, proc: subprocess.Popen[str] | None) -> _ShimWriter | None:
        if proc is None or self._closed or proc.stdin is None or proc.poll() is not None:
            return None
        w = self._shim_writer
        if w is not None and w.proc is proc:
            return w
        w = self._live_writers.get(id(proc))
        if w is not None and w.proc is proc:
            if self._shim is proc:
                self._shim_writer = w
            return w
        existing = getattr(proc, "_shim_writer_obj", None)
        if isinstance(existing, _ShimWriter) and existing.proc is proc:
            return existing
        if getattr(proc.stdin, "closed", False):
            return None
        w = _ShimWriter(proc, on_poison=self._on_writer_poison)
        self._live_writers[id(proc)] = w
        if self._shim is proc:
            self._shim_writer = w
        return w

    def _current_shim_writer(self) -> _ShimWriter | None:
        with self._shim_lock:
            return self._get_writer_locked(self._shim)

    def _ready_shim_writer(self) -> _ShimWriter | None:
        """Return the current shim writer iff already alive & ready (never blocks or spawns)."""
        proc = self._shim
        if (
            self._closed
            or self._shim_disabled
            or self._spawning_shim
            or proc is None
            or proc.poll() is not None
            or (proc is self._spawned_shim and not self._spawned_ready)
            or not self._ready.is_set()
        ):
            return None
        writer = self._shim_writer
        if writer is None or writer.proc is not proc:
            attached = getattr(proc, "_shim_writer_obj", None)
            writer = attached if (isinstance(attached, _ShimWriter) and attached.proc is proc) else None
        if writer is not None:
            return writer if writer.is_open else None
        # Fallback for tests injecting a bare fake self._shim without a pre-attached writer;
        # short timeout prevents blocking on slow Popen() while avoiding first-init race drops.
        if not self._shim_lock.acquire(timeout=0.01):
            return None
        try:
            if (
                self._closed
                or self._shim_disabled
                or self._spawning_shim
                or self._shim is not proc
                or proc.poll() is not None
                or (proc is self._spawned_shim and not self._spawned_ready)
                or not self._ready.is_set()
            ):
                return None
            writer = self._get_writer_locked(proc)
            return writer if (writer is not None and writer.is_open) else None
        finally:
            self._shim_lock.release()

    def _on_writer_poison(self, proc: subprocess.Popen[str], writer: _ShimWriter) -> None:
        was_current = False
        with self._shim_lock:
            if self._shim is proc:
                was_current = True
                self._shim = None
                self._shim_writer = None
                self._spawned_shim = None
                self._spawned_ready = False
                self._shim_caps = frozenset()
                self._shim_cond.notify_all()
            elif self._shim_writer is writer:
                self._shim_writer = None
        try:
            if proc.poll() is None:
                proc.kill()
            reader_th = getattr(proc, "_shim_reader_th", None)
            if not isinstance(reader_th, threading.Thread) or not reader_th.is_alive():
                proc.wait(timeout=0.5)
        except Exception:
            pass
        if was_current and not self._closed and not self._shim_disabled:
            self._schedule_respawn_if_needed()

    def _poison_and_reap_writer(self, writer: _ShimWriter | None) -> None:
        if writer is None:
            return
        proc = writer.proc
        was_current = False
        with self._shim_lock:
            if self._shim is proc:
                was_current = True
                self._shim = None
                self._shim_writer = None
                self._spawned_shim = None
                self._spawned_ready = False
                self._shim_caps = frozenset()
                self._shim_cond.notify_all()
            elif self._shim_writer is writer:
                self._shim_writer = None
            self._live_writers[id(proc)] = writer
        self._reap_owned_shim(proc)
        if was_current and not self._closed and not self._shim_disabled:
            self._schedule_respawn_if_needed()

    def _reap_owned_shim(self, proc: subprocess.Popen[str] | None) -> None:
        eff_writer: _ShimWriter | None = None
        if proc is not None:
            with self._shim_lock:
                mapped = self._live_writers.get(id(proc))
                if mapped is not None and mapped.proc is proc:
                    eff_writer = mapped
                if self._shim_writer is not None and (
                    self._shim_writer is eff_writer or self._shim_writer.proc is proc
                ):
                    if eff_writer is None:
                        eff_writer = self._shim_writer
                    self._shim_writer = None
            if eff_writer is None:
                attached = getattr(proc, "_shim_writer_obj", None)
                if isinstance(attached, _ShimWriter) and attached.proc is proc:
                    eff_writer = attached
        _reap_shim_proc(proc, writer=eff_writer)
        if proc is not None and eff_writer is not None:
            with self._shim_lock:
                if self._live_writers.get(id(proc)) is eff_writer:
                    self._live_writers.pop(id(proc), None)

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
        ptt_active_in_proc = False
        shim_id = int(getattr(proc, "_shim_instance_id", 0) or 0)
        if shim_id <= 0:
            with self._shim_lock:
                self._shim_instance_id += 1
                shim_id = self._shim_instance_id
                try:
                    setattr(proc, "_shim_instance_id", shim_id)
                except Exception:
                    pass
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
                        if not stale_proc and not self._closed:
                            ev_name = str(msg.get("event") or "")
                            if ev_name == "ptt_start":
                                ptt_active_in_proc = True
                            elif ev_name in ("ptt_stop", "ptt_cancel"):
                                ptt_active_in_proc = False
                            if ev_name not in self._INTERNAL_SHIM_EVENTS:
                                msg = {**msg, "_shim_instance_id": shim_id}
                            self._ensure_event_worker_locked()
                            self._event_q.put(msg)
                elif "reply" in msg or msg.get("type") == "reply":
                    with self._shim_lock:
                        entry = self._pending.get(int(msg.get("seq_id") or 0))
                    if entry is not None:
                        owner_proc, wait_q = entry if isinstance(entry, tuple) else (proc, entry)
                        if owner_proc is proc:
                            try: wait_q.put_nowait(msg)
                            except queue.Full: pass
                elif not got_ready and msg.get("ok") is False:
                    err_msg = str(msg.get("error") or f"{self._shim_label} startup failed")
                    with self._shim_lock:
                        is_cur_err = self._shim is proc or (not managed_spawn and self._shim is None)
                    if is_cur_err:
                        if self._shim_graceful:
                            self._disable_shim_once(err_msg)
                        else:
                            self.last_shim_error = redact_secrets(err_msg, self.env, cfg=self.cfg)
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
            seen_q_ids: set[int] = set()
            with self._shim_lock:
                for k_sid, val in list(self._pending.items()):
                    owner_proc, wait_q = val if isinstance(val, tuple) else (proc, val)
                    if owner_proc is proc:
                        if id(wait_q) not in seen_q_ids:
                            seen_q_ids.add(id(wait_q))
                            stale_queues.append(wait_q)
                        self._pending.pop(k_sid, None)
                if ptt_active_in_proc and not self._closed and self._on_event is not None:
                    # Enqueue under _shim_lock without recreating an event worker (ptt_start already started one);
                    # close() serializes _closed + None sentinel under the same lock so no worker can leak.
                    if self._event_worker is not None and self._event_worker.is_alive():
                        self._event_q.put({"event": "shim_disconnected", "_shim_instance_id": shim_id})
                if self._shim is proc:
                    self._shim = None
                    self._shim_writer = None
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
        target_writer: _ShimWriter | None = None,
    ) -> dict[str, Any] | None:
        max_attempts = 1 if target_writer is not None else (2 if allow_retry else 1)
        shared_q: queue.Queue[dict[str, Any]] | None = None
        shared_proc: subprocess.Popen[str] | None = None
        registered: list[tuple[subprocess.Popen[str], int]] = []

        def _unregister_registered_locked() -> None:
            for reg_proc, reg_sid in registered:
                cur_entry = self._pending.get(reg_sid)
                if cur_entry is not None:
                    owner_p = cur_entry[0] if isinstance(cur_entry, tuple) else reg_proc
                    if owner_p is reg_proc:
                        self._pending.pop(reg_sid, None)
            registered.clear()

        def _unregister_all() -> None:
            if not registered:
                return
            with self._shim_lock:
                _unregister_registered_locked()

        try:
            for attempt in range(max_attempts):
                if target_writer is None:
                    if not self._ensure_shim():
                        if self._shim_graceful:
                            return None
                        raise RuntimeError(self.last_shim_error or f"{self._shim_label} unavailable")
                sent_proc: subprocess.Popen[str] | None = None
                sent_writer: _ShimWriter | None = None
                broken: subprocess.Popen[str] | None = None
                try:
                    with self._shim_lock:
                        if target_writer is not None:
                            proc = target_writer.proc
                            writer = target_writer
                            if self._closed or proc is None or proc.poll() is not None or not writer.is_open:
                                if proc is not None and self._shim is proc:
                                    broken, self._shim = proc, None
                                    if self._shim_writer is not None and self._shim_writer.proc is proc:
                                        self._shim_writer = None
                                    self._spawned_shim = None
                                    self._spawned_ready = False
                                    self._shim_caps = frozenset()
                                raise BrokenPipeError("shim_not_running")
                        else:
                            proc = self._shim
                            writer = self._get_writer_locked(proc)
                            if proc is None or writer is None or not writer.is_open:
                                broken, self._shim = proc, None
                                if self._shim_writer is not None and self._shim_writer.proc is proc:
                                    self._shim_writer = None
                                self._spawned_shim = None
                                self._spawned_ready = False
                                self._shim_caps = frozenset()
                                raise BrokenPipeError("shim_not_running")
                        if shared_q is None or shared_proc is not proc:
                            _unregister_registered_locked()
                            shared_q = queue.Queue()
                            shared_proc = proc
                        else:
                            # If a prior timed-out attempt's reply arrived just before retrying, consume it now.
                            try:
                                early_res = shared_q.get_nowait()
                            except queue.Empty:
                                early_res = None
                            if early_res is not None and early_res.get("error") != "shim_eof":
                                if "error" in early_res:
                                    if self._shim_graceful:
                                        return None
                                    raise RuntimeError(str(early_res["error"]))
                                return early_res
                        sent_proc = proc
                        sent_writer = writer
                        self._rpc_seq += 1
                        sid = self._rpc_seq
                        self._pending[sid] = (proc, shared_q)
                        registered.append((proc, sid))
                    line = _dump_jsonl({**payload, "seq_id": sid})
                    attempt_deadline = time.monotonic() + max(0.0, float(timeout_sec))
                    write_status, write_reason = sent_writer.write_sync(line, attempt_deadline)
                    if write_status == "partial":
                        self._reap_owned_shim(sent_proc)
                        raise _ShimWriteTimeout(
                            "shim_stdin_partial_write_timeout", write_status="partial"
                        )
                    if write_status == "not_sent":
                        if write_reason == "timeout":
                            raise _ShimWriteTimeout(
                                "shim_stdin_write_timeout", write_status="not_sent"
                            )
                        raise BrokenPipeError(f"shim_stdin_{write_reason or 'closed'}")
                    rem_reply = attempt_deadline - time.monotonic()
                    if rem_reply <= 0.0:
                        raise queue.Empty()
                    res = shared_q.get(timeout=rem_reply)
                    if res.get("error") == "shim_eof" and (attempt + 1) < max_attempts and not self._closed and not self._shim_disabled:
                        _unregister_all()
                        shared_q = None
                        shared_proc = None
                        continue
                    if "error" in res:
                        if self._shim_graceful:
                            return None
                        raise RuntimeError(str(res["error"]))
                    return res
                except _ShimWriteTimeout:
                    _unregister_all()
                    shared_q = None
                    shared_proc = None
                    if self._shim_graceful:
                        return None
                    raise
                except (BrokenPipeError, OSError, queue.Empty) as exc:
                    if broken is not None:
                        self._reap_owned_shim(broken)
                        if not self._closed and not self._shim_disabled:
                            self._schedule_respawn_if_needed()
                    if not isinstance(exc, queue.Empty):
                        _unregister_all()
                        shared_q = None
                        shared_proc = None
                    if (attempt + 1) < max_attempts and not self._closed and not self._shim_disabled:
                        continue
                    if self._shim_graceful:
                        return None
                    raise
            if self._shim_graceful:
                return None
            raise RuntimeError("shim_rpc exhausted retries")
        finally:
            _unregister_all()

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
        bg_count: int = 0,
    ) -> None:
        if not self._has_managed_shim():
            return
        writer = self._ready_shim_writer()
        if writer is None:
            return
        st = (state or "").strip().lower()
        try:
            req: dict[str, Any] = {"cmd": "hud", "state": state, "seq_id": 0}
            eff_text = text
            eff_duration_ms = duration_ms if duration_ms > 0 else 0
            base_layer = hud_layer_for_state(st)
            eff_layer = ""
            if base_layer != "control":
                exp_layer = (layer or "").strip().lower()
                eff_layer = exp_layer if exp_layer in ("main", "toast") else base_layer
                req["layer"] = eff_layer
                req["position"] = normalize_ui_position(self.cfg.ui.position)
                if st == "recording" and eff_layer == "main":
                    if bg_count > 0:
                        req["bg_count"] = int(bg_count)
                    if self.cfg.audio.max_duration_sec > 0:
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
            mergeable = (
                st in ("recording", "recognizing", "processing", "refining")
                and eff_layer != "toast"
            )
            self._enqueue_shim_async(
                req,
                deadline_sec=0.5 if mergeable else None,
                is_hud=True,
                mergeable_hud=mergeable,
                writer=writer,
            )
        except Exception:
            pass

    def _write_shim_command(
        self,
        payload: Mapping[str, Any],
        *,
        writer: _ShimWriter | None = None,
        timeout_sec: float = 0.25,
    ) -> str:
        """Write one fire-and-forget JSONL command synchronously via the shim's single writer.

        Returns ``"sent"``, ``"not_sent"``, or ``"partial"``.
        """
        if self._closed:
            return "not_sent"
        target_writer = writer
        if target_writer is None:
            if not self._has_managed_shim():
                return "not_sent"
            target_writer = self._ready_shim_writer()
        if target_writer is None or not target_writer.is_open:
            return "not_sent"
        line = _dump_jsonl(payload)
        status, _ = target_writer.write_sync(line, time.monotonic() + max(0.0, float(timeout_sec)))
        return status

    def _enqueue_shim_async(
        self,
        payload: Mapping[str, Any],
        *,
        deadline_sec: float | None = 0.5,
        is_hud: bool = False,
        mergeable_hud: bool = False,
        writer: _ShimWriter | None = None,
    ) -> bool:
        if self._closed:
            return False
        target_writer = writer
        if target_writer is None:
            if not self._has_managed_shim():
                return False
            target_writer = self._ready_shim_writer()
        if target_writer is None or not target_writer.is_open:
            return False
        line = _dump_jsonl(payload)
        return target_writer.enqueue_async(
            line,
            deadline_sec=deadline_sec,
            is_hud=is_hud,
            mergeable_hud=mergeable_hud,
        )

    def hud_level(self, level: float) -> None:
        if not self._has_managed_shim():
            return
        writer = self._ready_shim_writer()
        if writer is None:
            return
        clamped = round(max(0.0, min(1.0, float(level))), 3)
        line = _dump_jsonl({"cmd": "hud_level", "level": clamped})
        writer.submit_level(line)

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
        with self._shim_lock:
            self._closed = True
            self._close_evt.set()
            self._on_event = None
            ev_worker = self._event_worker
            self._event_q.put(None)
        self._stop_level_streamer()
        if ev_worker is not None and ev_worker.is_alive() and ev_worker is not threading.current_thread():
            ev_worker.join(timeout=1.0)
        if (cancel := getattr(self, "cancel_recording", None)) is not None:
            cancel()
        with self._shim_lock:
            proc, self._shim = self._shim, None
            writer, self._shim_writer = self._shim_writer, None
            if proc is not None:
                mapped_w = self._live_writers.get(id(proc))
                if writer is None or writer.proc is not proc:
                    writer = mapped_w
                if writer is not None:
                    self._live_writers[id(proc)] = writer
            stragglers = [
                sw for pid_k, sw in list(self._live_writers.items()) if proc is None or pid_k != id(proc)
            ]
            self._spawned_shim = None
            self._spawned_ready = False
            self._shim_caps = frozenset()
            self._shim_cond.notify_all()
        if proc is not None and proc.poll() is None and writer is not None:
            if writer.close(send_quit=True, timeout_sec=0.25):
                try:
                    proc.wait(timeout=2.0)
                except Exception:
                    pass
        self._reap_owned_shim(proc)
        for sw in stragglers:
            if sw is not writer:
                sw.close(send_quit=False)
                with self._shim_lock:
                    if self._live_writers.get(id(sw.proc)) is sw:
                        self._live_writers.pop(id(sw.proc), None)


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
