"""WSL2 host bridge: WASAPI 16kHz capture via win_shim.ps1 stdio JSONL + PulseAudio RDPSource fallback."""
from __future__ import annotations

import base64, inspect, json, os, queue, signal, subprocess, sys, threading, time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .asr_providers import _sleep_cooperative
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

POWERSHELL_EXE = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
WSLG_PULSE_SOCK = "/mnt/wslg/PulseServer"
SHIM_SCRIPT = Path(__file__).resolve().with_name("win_shim.ps1")
WINDOWS_TERMINALS = frozenset(
    {
        "windowsterminal",
        "openconsole",
        "mintty",
        "wezterm",
        "wezterm-gui",
        "alacritty",
        "ghostty",
        "conhost",
        "putty",
        "tabby",
        "hyper",
        "kitty",
        "rio",
    }
)


def is_windows_terminal_process(process: str) -> bool:
    p = (process or "").strip().lower()
    if p.endswith(".exe"):
        p = p[:-4]
    return bool(p) and p in WINDOWS_TERMINALS


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


def _callable_accepts_kwarg(cb: Callable[..., Any], kw: str) -> bool:
    try:
        sig = inspect.signature(cb)
    except (TypeError, ValueError):
        return False
    return kw in sig.parameters or any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )


def invoke_recorder_lost_callback(cb: Callable[..., Any] | None, seq_id: int | None = None) -> None:
    if cb is None:
        return
    if seq_id is not None and _callable_accepts_kwarg(cb, "seq_id"):
        cb(seq_id=seq_id)
        return
    cb()


def invoke_recorder_recovered_callback(cb: Callable[..., Any] | None, gap_ms: int, seq_id: int | None = None) -> None:
    if cb is None:
        return
    if seq_id is not None and _callable_accepts_kwarg(cb, "seq_id"):
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


def is_wsl(env: Mapping[str, str] | None = None) -> bool:
    src = os.environ if env is None else env
    if src.get("WSL_DISTRO_NAME") or src.get("WSL_INTEROP"):
        return True
    try: return "microsoft" in Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8", errors="ignore").lower()
    except OSError: return False


def _pulse_env(env: Mapping[str, str]) -> dict[str, str]:
    if env.get("PULSE_SERVER"):
        return dict(env)
    if os.path.exists(WSLG_PULSE_SOCK):
        return {**env, "PULSE_SERVER": f"unix:{WSLG_PULSE_SOCK}"}
    xdg = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    native_sock = f"{xdg}/pulse/native"
    return {**env, "PULSE_SERVER": f"unix:{native_sock}"} if os.path.exists(native_sock) else dict(env)


def to_windows_path(path: str | Path) -> str:
    for wb in ("/usr/bin/wslpath", "/bin/wslpath", "wslpath"):
        if wb == "wslpath" or os.path.isfile(wb):
            p = subprocess.run([wb, "-w", str(path)], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5, check=False)
            if p.returncode == 0 and p.stdout.strip():
                return p.stdout.strip()
    raise RuntimeError(f"wslpath failed for {path}")


def probe_wsl_host(cfg: Config, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    raw_env = os.environ if env is None else env
    eff_env = _pulse_env(raw_env)
    info: dict[str, Any] = {"powershell_ok": False, "pulse_rdp_ok": False, "winmm_devs": 0, "fg_process": "", "fg_title": ""}
    try:
        p = subprocess.run([resolve_ffmpeg_bin(), "-nostdin", "-hide_banner", "-sources", "pulse"], stdin=subprocess.DEVNULL, capture_output=True, text=True, env=eff_env, timeout=5, check=False)
        info["pulse_rdp_ok"] = "RDPSource" in (p.stdout + p.stderr)
    except Exception as exc: info["pulse_error"] = str(exc)
    if os.path.isfile(POWERSHELL_EXE):
        try:
            p = subprocess.run([POWERSHELL_EXE, "-NoProfile", "-NonInteractive", "-Sta", "-ExecutionPolicy", "Bypass", "-File", to_windows_path(SHIM_SCRIPT), "-Mode", "probe"], stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8, check=False)
            if p.returncode == 0 and p.stdout.strip():
                d = json.loads(p.stdout.strip().splitlines()[-1])
                ui_cult = str(d.get("ui_culture") or "").strip()
                info.update(powershell_ok=bool(d.get("ok")), ps_version=str(d.get("ps_version") or ""), winmm_devs=int(d.get("wave_in_devs") or 0), fg_process=str(d.get("process") or ""), fg_title=str(d.get("title") or ""), fg_hwnd=int(d.get("hwnd") or 0), ui_culture=ui_cult, ui_lang=resolve_ui_language(cfg.ui.language, env=raw_env, win_ui_culture=ui_cult))
        except Exception as exc: info["ps_error"] = str(exc)
    return info


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


def run_key_test(cfg: Config, seconds: int = 10) -> int:
    if not os.path.isfile(POWERSHELL_EXE):
        raise RuntimeError(f"PowerShell not found at {POWERSHELL_EXE}")
    cmd = [POWERSHELL_EXE, "-NoProfile", "-Sta", "-ExecutionPolicy", "Bypass", "-File", to_windows_path(SHIM_SCRIPT), "-Mode", "key-test", "-Hotkey", cfg.hotkey.key, "-TriggerMode", cfg.hotkey.trigger_mode, "-TapMaxMs", str(cfg.hotkey.tap_max_ms), "-DebounceMs", str(cfg.hotkey.debounce_ms), "-Seconds", str(max(1, seconds))]
    return stream_key_test_proc(cmd)


def _reap_shim_proc(proc: subprocess.Popen[str] | None) -> None:
    if proc is None:
        return
    for pipe in (proc.stdin, proc.stdout):
        if pipe is not None:
            try: pipe.close()
            except Exception: pass
    try:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=1.0)
    except Exception:
        pass


class JsonlShimMixin:
    _shim_graceful: bool = False
    _shim_ready_timeout: float = 8.0
    _shim_label: str = "shim"
    cfg: Config
    env: dict[str, str]

    def _init_shim_bridge(self) -> None:
        self._closed = False
        self._shim: subprocess.Popen[str] | None = None
        self._shim_disabled = False
        self._shim_lock = threading.Lock()
        self._stdin_lock = threading.Lock()
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

    def _event_loop(self) -> None:
        while (msg := self._event_q.get()) is not None:
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

    def _ensure_shim(self) -> bool:
        if self._closed:
            if self._shim_graceful:
                return False
            raise RuntimeError(f"{type(self).__name__} is closed")
        if self._shim_disabled:
            return False
        dead: subprocess.Popen[str] | None = None
        with self._shim_lock:
            if self._closed:
                if self._shim_graceful:
                    return False
                raise RuntimeError(f"{type(self).__name__} is closed")
            if self._shim_disabled:
                return False
            if self._shim is not None and self._shim.poll() is None:
                return True
            dead, self._shim = self._shim, None
            cmd = self._build_shim_cmd()
            if not cmd:
                if self._shim_graceful:
                    self._disable_shim_once(self.last_shim_error or f"{self._shim_label} unavailable")
                    return False
                raise RuntimeError(self.last_shim_error or f"{self._shim_label} unavailable")
            self._ready.clear()
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
            self._shim = proc
            threading.Thread(target=self._shim_reader, args=(proc,), daemon=True).start()
        _reap_shim_proc(dead)
        self._ready.wait(timeout=self._shim_ready_timeout)
        if self._closed:
            if self._shim_graceful:
                return False
            raise RuntimeError(f"{type(self).__name__} is closed")
        if self._shim_disabled:
            return False
        if self._shim_graceful:
            with self._shim_lock:
                alive = self._shim is not None and self._shim.poll() is None
            if not alive:
                self._disable_shim_once(self.last_shim_error or f"{self._shim_label} exited before ready")
                return False
        return True

    def _respawn_shim_bg(self) -> None:
        time.sleep(0.2)
        if not self._closed and not self._shim_disabled and self._on_event is not None:
            try: self._ensure_shim()
            except Exception: pass

    def _shim_reader(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stdout is not None
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
                    got_ready = True
                    self._win_ui_culture = str(msg.get("ui_culture") or "").strip()
                    if "audio_status" in msg and isinstance(msg.get("audio_status"), dict):
                        try:
                            self._on_shim_internal_event({"event": "audio_devices_changed", **msg["audio_status"]})
                        except Exception:
                            pass
                    self._ready.set()
                elif "event" in msg and (
                    self._on_event is not None
                    or str(msg.get("event") or "") in self._INTERNAL_SHIM_EVENTS
                ):
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
                    self._disable_shim_once(str(msg.get("error") or f"{self._shim_label} startup failed"))
                    self._ready.set()
        finally:
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
                    self._ready.set()
                    if not got_ready and self._shim_graceful and not self._closed:
                        self._disable_shim_once(self.last_shim_error or f"{self._shim_label} exited before ready")
                    need_respawn = got_ready and not self._closed and not self._shim_disabled and self._on_event is not None
            _reap_shim_proc(proc)
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
                        raise BrokenPipeError("shim_not_running")
                    if before_write is not None:
                        before_write()
                    sent_proc = proc
                    self._rpc_seq += 1
                    sid = self._rpc_seq
                    self._pending[sid] = (proc, q)
                    with self._stdin_lock:
                        proc.stdin.write(json.dumps({**payload, "seq_id": sid}, ensure_ascii=False) + "\n")
                        proc.stdin.flush()
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
                    _reap_shim_proc(broken)
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
        self._shim_disabled = False
        self._on_event = on_hotkey_event
        self._ensure_event_worker()
        self._ensure_shim()

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
                if proc.poll() is None and proc.stdin is not None and not proc.stdin.closed:
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
        self._stop_level_streamer()
        self._on_event = None
        self._event_q.put(None)
        if self._event_worker is not None and self._event_worker.is_alive() and self._event_worker is not threading.current_thread():
            self._event_worker.join(timeout=1.0)
        if (cancel := getattr(self, "cancel_recording", None)) is not None:
            cancel()
        with self._shim_lock:
            proc, self._shim = self._shim, None
        if proc is not None and proc.poll() is None:
            try:
                with self._stdin_lock:
                    if proc.stdin and not proc.stdin.closed:
                        proc.stdin.write('{"cmd":"quit"}\n')
                        proc.stdin.flush()
                        proc.stdin.close()
                proc.wait(timeout=2.0)
            except Exception:
                pass
        _reap_shim_proc(proc)


class WslPlatform(JsonlShimMixin):
    _shim_graceful = False
    _shim_ready_timeout = 8.0
    _shim_label = "win_shim"

    def _host_ui_culture(self) -> str | None:
        # Intentional on WSL2: always pass a string ("" when omitted) so HUD language never falls back to WSL Linux LANG.
        return self._win_ui_culture

    def __init__(self, cfg: Config, *, env: Mapping[str, str] | None = None, pulse_source: str = "RDPSource", shim_cmd_factory: Callable[[], list[str]] | None = None) -> None:
        raw_env = os.environ if env is None else env
        self.cfg, self.env = cfg, _pulse_env(raw_env)
        explicit_pulse = (raw_env.get("AGENT_VOICE_INPUT_PULSE_SOURCE") or raw_env.get("VOICE_INPUT_PULSE_SOURCE") or "").strip()
        self.pulse_source = explicit_pulse or pulse_source.strip() or "RDPSource"
        self._shim_cmd_factory = shim_cmd_factory
        if cfg.audio.wsl_source in ("winmm", "wasapi"):
            self._primary_winmm = True
        elif cfg.audio.wsl_source == "pulse":
            self._primary_winmm = False
        else:
            has_custom_pulse = bool(explicit_pulse or pulse_source.strip() not in ("", "RDPSource"))
            self._primary_winmm = bool(not has_custom_pulse and shim_cmd_factory is None and os.path.isfile(POWERSHELL_EXE))
        self._use_winmm = self._primary_winmm
        self.last_audio_error = ""
        self._ff_rec, self._rec_mode = FfmpegWavRecorder(), ""
        self._winmm_state_lock = threading.Lock()
        self._winmm_gen = 0
        self._active_seq_id: int | None = None
        self._winmm_starting = False
        self._winmm_rpc_dispatched = False
        self._pending_winmm_lost_reason = ""
        self._pending_winmm_recovered_gaps: list[int] = []
        self._recorder_lost_cb: Callable[..., Any] | None = None
        self._recorder_recovered_cb: Callable[..., Any] | None = None
        self._paste_hold_timeout_sec: float | None = None
        self._init_shim_bridge()

    def set_recorder_lost_callback(self, cb: Callable[..., Any] | None) -> None:
        self._recorder_lost_cb = cb

    def set_recorder_recovered_callback(self, cb: Callable[..., Any] | None) -> None:
        self._recorder_recovered_cb = cb

    @staticmethod
    def _parse_event_rec_gen(msg: Mapping[str, Any]) -> int:
        raw = msg.get("rec_gen")
        if raw is None or isinstance(raw, bool):
            return 0
        try:
            val = int(raw)
        except (TypeError, ValueError):
            return 0
        return val if val > 0 else 0

    def _on_shim_internal_event(self, msg: dict[str, Any]) -> None:
        ev = str(msg.get("event") or "")
        if ev == "rec_lost":
            ev_gen = self._parse_event_rec_gen(msg)
            if ev_gen <= 0:
                return
            reason = str(msg.get("reason") or "wasapi_device_lost").strip() or "wasapi_device_lost"
            should_fire = False
            ev_seq: int | None = None
            with self._winmm_state_lock:
                if ev_gen != self._winmm_gen:
                    return
                if self._rec_mode == "winmm":
                    self.last_audio_error = reason
                    should_fire = True
                    ev_seq = self._active_seq_id
                elif self._winmm_starting:
                    self._pending_winmm_lost_reason = reason
            if should_fire:
                invoke_recorder_lost_callback(self._recorder_lost_cb, ev_seq)
        elif ev == "rec_recovered":
            ev_gen = self._parse_event_rec_gen(msg)
            if ev_gen <= 0:
                return
            try:
                gap_ms = int(msg.get("gap_ms") or 0)
            except (TypeError, ValueError):
                gap_ms = 0
            should_fire_rec = False
            rec_seq: int | None = None
            with self._winmm_state_lock:
                if ev_gen != self._winmm_gen:
                    return
                if self._rec_mode == "winmm":
                    should_fire_rec = True
                    rec_seq = self._active_seq_id
                elif self._winmm_starting:
                    self._pending_winmm_recovered_gaps.append(gap_ms)
            if should_fire_rec:
                invoke_recorder_recovered_callback(self._recorder_recovered_cb, gap_ms, rec_seq)

    def _build_shim_cmd(self) -> list[str]:
        if self._shim_cmd_factory is not None:
            return self._shim_cmd_factory()
        if not os.path.isfile(POWERSHELL_EXE):
            raise RuntimeError(f"PowerShell not found at {POWERSHELL_EXE}")
        return [POWERSHELL_EXE, "-NoProfile", "-NonInteractive", "-Sta", "-ExecutionPolicy", "Bypass", "-File", to_windows_path(SHIM_SCRIPT), "-Mode", "bridge", "-Hotkey", self.cfg.hotkey.key, "-TriggerMode", self.cfg.hotkey.trigger_mode, "-TapMaxMs", str(self.cfg.hotkey.tap_max_ms), "-DebounceMs", str(self.cfg.hotkey.debounce_ms), "-Beep", "1" if self.cfg.audio.beep else "0"]

    def is_allowed_terminal(self, window: ForegroundWindow) -> bool:
        return is_windows_terminal_process(window.process)

    def get_foreground_window(self) -> ForegroundWindow:
        if self._has_managed_shim():
            try:
                r = self._shim_rpc({"cmd": "fg_window"}, timeout_sec=3.0) or {}
                return ForegroundWindow(int(r.get("hwnd") or 0), str(r.get("process") or ""), str(r.get("title") or ""))
            except Exception:
                pass
        info = probe_wsl_host(self.cfg, self.env)
        return ForegroundWindow(int(info.get("fg_hwnd") or 0), str(info.get("fg_process") or ""), str(info.get("fg_title") or ""))

    def beep(self, kind: str) -> None:
        if self.cfg.audio.beep and self._has_managed_shim():
            try: self._shim_rpc({"cmd": "beep", "kind": kind}, timeout_sec=1.5)
            except Exception: pass

    @staticmethod
    def _classify_wasapi_start_error(err: str) -> NoMicrophoneError | MicrophoneUnavailableError:
        low = (err or "").strip().lower()
        if low.startswith("mic_permission") or "0x80070005" in low or "access_denied" in low:
            return MicrophoneUnavailableError(
                f"microphone access denied ({err})",
                reason_code="mic_permission",
                hud_state="mic_permission",
            )
        if low.startswith("no_mic") or "0x80070490" in low:
            return NoMicrophoneError(f"no microphone input device available ({err})")
        return MicrophoneUnavailableError(
            f"microphone temporarily unavailable ({err})",
            reason_code="mic_unavailable",
            hud_state="mic_unavailable",
        )

    def _fire_pulse_lost(self, expected_gen: int, expected_seq: int | None) -> None:
        with self._winmm_state_lock:
            if self._winmm_gen != expected_gen or self._rec_mode != "pulse":
                return
        invoke_recorder_lost_callback(self._recorder_lost_cb, expected_seq)

    def start_recording(self, runtime_dir: Path, *, loopback: bool = False, seq_id: int | None = None) -> None:
        self._stop_level_streamer()
        self.last_audio_error = ""
        eff_loopback = bool(loopback or self.env.get("AGENT_VOICE_INPUT_WASAPI_LOOPBACK") == "1" or self.env.get("VOICE_INPUT_WASAPI_LOOPBACK") == "1")
        if self._use_winmm:
            has_before_write = _callable_accepts_kwarg(self._shim_rpc, "before_write")
            with self._winmm_state_lock:
                if self._closed:
                    raise RuntimeError(f"{type(self).__name__} is closed")
                self._winmm_gen += 1
                my_gen = self._winmm_gen
                self._active_seq_id = seq_id
                self._rec_mode = ""
                self._winmm_starting = True
                self._winmm_rpc_dispatched = not has_before_write
                self._pending_winmm_lost_reason = ""
                self._pending_winmm_recovered_gaps.clear()
            req: dict[str, Any] = {"cmd": "rec_start", "rec_gen": my_gen}
            if eff_loopback:
                req["loopback"] = True

            def _on_before_write() -> None:
                with self._winmm_state_lock:
                    if self._closed or self._winmm_gen != my_gen:
                        raise self._classify_wasapi_start_error("mic_busy:wasapi_aborted")
                    self._winmm_rpc_dispatched = True

            rpc_kw: dict[str, Any] = {}
            if _callable_accepts_kwarg(self._shim_rpc, "allow_retry"):
                rpc_kw["allow_retry"] = False
            if has_before_write:
                rpc_kw["before_write"] = _on_before_write
            try:
                res = self._shim_rpc(req, timeout_sec=4.0, **rpc_kw) or {}
            except Exception as shim_exc:
                fb_gen = 0
                with self._winmm_state_lock:
                    if self._winmm_gen == my_gen:
                        self._winmm_starting = False
                        self._winmm_rpc_dispatched = False
                        self._active_seq_id = None
                        self._pending_winmm_lost_reason = ""
                        self._pending_winmm_recovered_gaps.clear()
                        if (
                            not self._closed
                            and not isinstance(shim_exc, (NoMicrophoneError, MicrophoneUnavailableError))
                            and self.cfg.audio.wsl_source == "auto"
                            and not eff_loopback
                        ):
                            self._winmm_gen += 1
                            fb_gen = self._winmm_gen
                            self._active_seq_id = seq_id
                            self._rec_mode = "pulse"
                self.last_audio_error = str(shim_exc)
                if fb_gen:
                    try:
                        self._ff_rec.start(
                            "pulse",
                            self.pulse_source,
                            runtime_dir,
                            self.env,
                            verify_startup=True,
                            on_unexpected_exit=lambda g=fb_gen, s=seq_id: self._fire_pulse_lost(g, s),
                            on_level=self._on_recorder_level,
                        )
                    except Exception:
                        with self._winmm_state_lock:
                            if self._winmm_gen == fb_gen:
                                self._rec_mode = ""
                                self._active_seq_id = None
                        self.last_audio_error = self._ff_rec.diagnostic_summary() or str(shim_exc)
                        raise
                    stale_fb = False
                    with self._winmm_state_lock:
                        if self._closed or self._winmm_gen != fb_gen:
                            stale_fb = True
                            if self._winmm_gen == fb_gen:
                                self._rec_mode = ""
                                self._active_seq_id = None
                    if stale_fb:
                        self._ff_rec.stop(drain_sec=0.0)
                        raise self._classify_wasapi_start_error("mic_busy:wasapi_aborted")
                    self._start_level_streamer(lambda: self._ff_rec.current_level)
                    return
                raise
            if res.get("ok"):
                early_lost = ""
                early_gaps: list[int] = []
                stale_ok = False
                with self._winmm_state_lock:
                    if self._closed or self._winmm_gen != my_gen:
                        stale_ok = True
                        if self._winmm_gen == my_gen:
                            self._winmm_starting = False
                            self._winmm_rpc_dispatched = False
                            self._active_seq_id = None
                            self._pending_winmm_lost_reason = ""
                            self._pending_winmm_recovered_gaps.clear()
                    else:
                        self._winmm_starting = False
                        self._winmm_rpc_dispatched = False
                        self._rec_mode = "winmm"
                        early_lost = self._pending_winmm_lost_reason
                        self._pending_winmm_lost_reason = ""
                        early_gaps = list(self._pending_winmm_recovered_gaps)
                        self._pending_winmm_recovered_gaps.clear()
                if stale_ok:
                    if not has_before_write and not self._closed:
                        try:
                            self._shim_rpc({"cmd": "rec_stop"}, timeout_sec=2.0)
                        except Exception:
                            pass
                    self.last_audio_error = "mic_busy:wasapi_aborted"
                    raise self._classify_wasapi_start_error("mic_busy:wasapi_aborted")
                if early_gaps:
                    for g in early_gaps:
                        invoke_recorder_recovered_callback(self._recorder_recovered_cb, g, seq_id)
                if early_lost:
                    self.last_audio_error = early_lost
                    if self._recorder_lost_cb is not None:
                        def _deliver_early_lost(expected_gen: int = my_gen, expected_seq: int | None = seq_id) -> None:
                            with self._winmm_state_lock:
                                if self._winmm_gen != expected_gen or self._rec_mode != "winmm":
                                    return
                            invoke_recorder_lost_callback(self._recorder_lost_cb, expected_seq)

                        threading.Thread(target=_deliver_early_lost, daemon=True).start()
                return
            with self._winmm_state_lock:
                if self._winmm_gen == my_gen:
                    self._winmm_starting = False
                    self._winmm_rpc_dispatched = False
                    self._active_seq_id = None
                    self._pending_winmm_lost_reason = ""
                    self._pending_winmm_recovered_gaps.clear()
            err = str(res.get("rec_error") or "winmm rec_start failed")
            self.last_audio_error = err
            # Never fall back to WSLg's dummy RDPSource (which streams silent zeros) when Windows
            # itself confirmed the microphone is missing, denied, or temporarily busy.
            raise self._classify_wasapi_start_error(err)
        with self._winmm_state_lock:
            if self._closed:
                raise RuntimeError(f"{type(self).__name__} is closed")
            self._winmm_gen += 1
            p_gen = self._winmm_gen
            self._active_seq_id = seq_id
            self._rec_mode = "pulse"
        src = "RDPSink.monitor" if (eff_loopback and self.pulse_source == "RDPSource") else self.pulse_source
        try:
            self._ff_rec.start(
                "pulse",
                src,
                runtime_dir,
                self.env,
                verify_startup=True,
                on_unexpected_exit=lambda g=p_gen, s=seq_id: self._fire_pulse_lost(g, s),
                on_level=self._on_recorder_level,
            )
        except Exception:
            with self._winmm_state_lock:
                if self._winmm_gen == p_gen:
                    self._rec_mode = ""
                    self._active_seq_id = None
            self.last_audio_error = self._ff_rec.diagnostic_summary()
            raise
        stale_pulse = False
        with self._winmm_state_lock:
            if self._closed or self._winmm_gen != p_gen:
                stale_pulse = True
                if self._winmm_gen == p_gen:
                    self._rec_mode = ""
                    self._active_seq_id = None
        if stale_pulse:
            self._ff_rec.stop(drain_sec=0.0)
            raise self._classify_wasapi_start_error("mic_busy:wasapi_aborted")
        self._start_level_streamer(lambda: self._ff_rec.current_level)

    def stop_recording(self, *, drain: bool = True) -> tuple[bytes, str]:
        self._stop_level_streamer()
        with self._winmm_state_lock:
            self._winmm_gen += 1
            self._active_seq_id = None
            was_dispatched = self._winmm_rpc_dispatched
            self._winmm_starting = False
            self._winmm_rpc_dispatched = False
            self._pending_winmm_lost_reason = ""
            self._pending_winmm_recovered_gaps.clear()
            mode, self._rec_mode = self._rec_mode, ""
        drain_sec = self.cfg.audio.effective_tail_drain_sec(macos=False) if (drain and mode) else 0.0
        if mode == "winmm" or was_dispatched:
            if drain_sec > 0.0:
                time.sleep(drain_sec)
            res = self._shim_rpc({"cmd": "rec_stop"}, timeout_sec=5.0) or {}
            b64 = str(res.get("wav_b64") or "")
            wav = base64.b64decode(b64) if b64 else b""
            rec_err = str(res.get("rec_error") or "").strip()
            self.last_audio_error = rec_err if rec_err else ("" if wav else "wasapi_empty_audio")
            return wav, "winmm"
        wav = self._ff_rec.stop(drain_sec=drain_sec)
        self.last_audio_error = "" if wav else self._ff_rec.diagnostic_summary()
        return wav, "pulse"

    def cancel_recording(self) -> None:
        self._stop_level_streamer()
        try: self.stop_recording(drain=False)
        except Exception: pass

    def switch_to_fallback_audio(self) -> bool:
        if self.cfg.audio.wsl_source != "auto":
            return False
        if not self._use_winmm:
            self._use_winmm = True
            return True
        if not self._primary_winmm:
            self._use_winmm = False
        return False

    def _effective_paste_hold_timeout_sec(self) -> float:
        if self._paste_hold_timeout_sec is not None:
            return max(0.0, float(self._paste_hold_timeout_sec))
        return max(15.0, float(self.cfg.audio.max_duration_sec) + 2.0)

    def _dispatch_paste_rpc(self, req: dict[str, Any], timeout_sec: float = 5.0) -> dict[str, Any]:
        if _callable_accepts_kwarg(self._shim_rpc, "allow_retry"):
            return self._shim_rpc(req, timeout_sec=timeout_sec, allow_retry=False) or {}
        return self._shim_rpc(req, timeout_sec=timeout_sec) or {}

    def deliver_text(
        self,
        text: str,
        seq_id: int,
        target_hwnd: int = 0,
        *,
        origin_window: ForegroundWindow | None = None,
        fallback_clipboard_text: str | None = None,
        preserve_clipboard: bool = False,
        suppress_beep: bool | Callable[[], bool] = False,
        is_cancelled: Callable[[], bool] | None = None,
        before_clipboard_write: Callable[[], None] | None = None,
    ) -> PasteOutcome:
        del preserve_clipboard
        eff_hwnd = target_hwnd or (origin_window.hwnd if origin_window is not None else 0)
        cancelled_fn = lambda: bool(is_cancelled is not None and is_cancelled())
        if cancelled_fn():
            return PasteOutcome(False, False, "cancelled", "")
        if before_clipboard_write is not None:
            before_clipboard_write()
        hold_deadline = time.monotonic() + self._effective_paste_hold_timeout_sec()
        while True:
            if cancelled_fn():
                return PasteOutcome(False, False, "cancelled", "")
            req: dict[str, Any] = {
                "cmd": "paste",
                "seq_id": seq_id,
                "text": text,
                "target_hwnd": eff_hwnd,
                "expect_terminal": True,
            }
            if fallback_clipboard_text is not None and fallback_clipboard_text != text:
                req["fallback_text"] = fallback_clipboard_text
            if bool(suppress_beep() if callable(suppress_beep) else suppress_beep):
                req["suppress_beep"] = True
            res = self._dispatch_paste_rpc(req, timeout_sec=5.0)
            reason = str(res.get("reason") or "ok")
            fg_proc = str(res.get("fg_process") or "")
            if reason != "hotkey_held":
                pasted = bool(res.get("pasted"))
                clip_ok = bool(res.get("clipboard"))
                return PasteOutcome(
                    pasted,
                    clip_ok,
                    reason,
                    fg_proc,
                    used_clipboard_paste=bool(pasted and clip_ok),
                )
            rem = hold_deadline - time.monotonic()
            if rem <= 0.0:
                break
            _sleep_cooperative(min(0.05, rem), sleep_fn=time.sleep, is_cancelled=cancelled_fn)

        if cancelled_fn():
            return PasteOutcome(False, False, "cancelled", "")
        fb_req: dict[str, Any] = {
            "cmd": "paste",
            "seq_id": seq_id,
            "text": text,
            "target_hwnd": eff_hwnd,
            "expect_terminal": True,
            "force_clipboard_only": True,
        }
        if fallback_clipboard_text is not None:
            fb_req["fallback_text"] = fallback_clipboard_text
        if bool(suppress_beep() if callable(suppress_beep) else suppress_beep):
            fb_req["suppress_beep"] = True
        res_fb = self._dispatch_paste_rpc(fb_req, timeout_sec=5.0)
        clip_ok = bool(res_fb.get("clipboard"))
        fg_proc = str(res_fb.get("fg_process") or "")
        return PasteOutcome(
            False,
            clip_ok,
            "focus_changed" if clip_ok else str(res_fb.get("reason") or "clipboard_failed"),
            fg_proc,
            used_clipboard_paste=False,
        )
