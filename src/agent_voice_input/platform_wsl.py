"""WSL2 host bridge: WASAPI 16kHz capture via win_shim.ps1 stdio JSONL + PulseAudio RDPSource fallback."""
from __future__ import annotations

import base64, json, os, queue, signal, subprocess, sys, threading, time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .audio_proc import pack_pcm16_mono_wav, resolve_ffmpeg_bin
from .config import Config, normalize_ui_position, redact_secrets
from .hud import format_hud_text, resolve_ui_language

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

    @property
    def app_name(self) -> str:
        return self.process


@dataclass(frozen=True)
class PasteOutcome:
    pasted: bool; clipboard: bool; reason: str = "ok"; fg_process: str = ""


class NoMicrophoneError(RuntimeError):
    """Raised when no usable microphone / audio input device can be opened."""


class FfmpegWavRecorder:
    def __init__(self, cmd_factory: Callable[[str, str], list[str]] | None = None) -> None:
        self._cmd_factory = cmd_factory
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._err_reader: threading.Thread | None = None
        self._buf = bytearray()
        self._err_buf = bytearray()
        self._stdout_eof = False
        self._unexpected_exit_cb: Callable[[], None] | None = None
        self.last_stderr: str = ""
        self.last_returncode: int | None = None

    def _build_cmd(self, fmt: str, source: str) -> list[str]:
        if self._cmd_factory is not None:
            return self._cmd_factory(fmt, source)
        in_opts = ["-sample_rate", "16000", "-channels", "1", "-fragment_size", "640"] if fmt == "pulse" else []
        return [resolve_ffmpeg_bin(), "-nostdin", "-hide_banner", "-loglevel", "error", "-probesize", "32", "-analyzeduration", "0", "-fflags", "nobuffer", "-flags", "low_delay", *in_opts, "-f", fmt, "-i", source, "-ac", "1", "-ar", "16000", "-f", "s16le", "-acodec", "pcm_s16le", "-flush_packets", "1", "pipe:1"]

    @staticmethod
    def _read_pipe(pipe: Any, sink: bytearray, limit: int = 0) -> None:
        try:
            while chunk := pipe.read(4096 if limit else 8192):
                sink.extend(chunk)
                if limit and len(sink) > limit:
                    del sink[:-limit]
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
        try:
            while chunk := pipe.read(8192):
                sink.extend(chunk)
                if len(sink) >= 2 and not startup_evt.is_set():
                    startup_evt.set()
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
                still_active = self._proc is proc and self._unexpected_exit_cb is not None
            if still_active:
                try:
                    rc = proc.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    rc = proc.poll()
                err_reader.join(timeout=0.15)
                with self._lock:
                    if self._proc is proc and rc not in (None, 0):
                        self.last_returncode = rc
                        self.last_stderr = bytes(err_buf).decode("utf-8", errors="replace").strip()
                        cb = self._unexpected_exit_cb
                        self._unexpected_exit_cb = None
            if cb is not None:
                threading.Thread(target=cb, daemon=True).start()

    def start(
        self,
        fmt: str,
        source: str,
        runtime_dir: Path | None = None,
        env: Mapping[str, str] | None = None,
        *,
        verify_startup: bool = False,
        startup_timeout_sec: float = 0.35,
        on_unexpected_exit: Callable[[], None] | None = None,
    ) -> None:
        self.stop(drain_sec=0.0)
        buf, err_buf = bytearray(), bytearray()
        startup_evt = threading.Event()
        self.last_stderr, self.last_returncode = "", None
        proc = subprocess.Popen(self._build_cmd(fmt, source), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, start_new_session=True, env=dict(os.environ if env is None else env))
        assert proc.stdout is not None and proc.stderr is not None
        err_reader = threading.Thread(target=self._read_pipe, args=(proc.stderr, err_buf, 512), daemon=True)
        reader = threading.Thread(target=self._read_stdout, args=(proc.stdout, buf, startup_evt, proc, err_reader, err_buf), daemon=True)
        with self._lock:
            self._proc, self._reader, self._err_reader, self._buf, self._err_buf = proc, reader, err_reader, buf, err_buf
            self._stdout_eof = False
            self._unexpected_exit_cb = None
        try:
            err_reader.start(); reader.start()
        except Exception:
            with self._lock:
                self._proc, self._reader, self._err_reader, self._buf, self._err_buf = None, None, None, bytearray(), bytearray()
                self._stdout_eof = False
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
            if len(buf) < 2 and (saw_eof or proc.poll() is not None):
                try:
                    rc = proc.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    rc = proc.poll()
                err_reader.join(timeout=0.15)
                err_txt = bytes(err_buf).decode("utf-8", errors="replace").strip()
                if rc not in (None, 0):
                    self.stop(drain_sec=0.0)
                    self.last_returncode = rc
                    self.last_stderr = err_txt
                    raise NoMicrophoneError(f"no microphone input device available ({self.diagnostic_summary()})")

        if on_unexpected_exit is not None:
            early_eof = False
            with self._lock:
                if self._proc is proc:
                    if self._stdout_eof or proc.poll() is not None:
                        early_eof = True
                    else:
                        self._unexpected_exit_cb = on_unexpected_exit
            if early_eof:
                try:
                    rc = proc.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    rc = proc.poll()
                err_reader.join(timeout=0.15)
                should_fire = False
                with self._lock:
                    if self._proc is proc and rc not in (None, 0):
                        self.last_returncode = rc
                        self.last_stderr = bytes(err_buf).decode("utf-8", errors="replace").strip()
                        should_fire = True
                if should_fire:
                    threading.Thread(target=on_unexpected_exit, daemon=True).start()

    def stop(self, *, drain_sec: float = 0.0) -> bytes:
        with self._lock:
            proc, reader, err_reader, buf, err_buf = self._proc, self._reader, self._err_reader, self._buf, self._err_buf
            self._proc, self._reader, self._err_reader, self._buf, self._err_buf = None, None, None, bytearray(), bytearray()
            self._stdout_eof = False
            self._unexpected_exit_cb = None
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
        self._rpc_seq, self._pending, self._ready = 100, {}, threading.Event()
        self._event_q: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._event_worker: threading.Thread | None = None
        self._on_event: Callable[[dict[str, Any]], None] | None = None
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

    def _has_managed_shim(self) -> bool:
        if self._closed or self._shim_disabled:
            return False
        if self._shim_graceful:
            return self._shim is not None and self._shim.poll() is None
        return self._shim is not None or self._on_event is not None

    def _ensure_event_worker(self) -> None:
        with self._shim_lock:
            if self._event_worker is None or not self._event_worker.is_alive():
                self._event_worker = threading.Thread(target=self._event_loop, daemon=True)
                self._event_worker.start()

    def _event_loop(self) -> None:
        while (msg := self._event_q.get()) is not None:
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
                    self._ready.set()
                elif "event" in msg and self._on_event is not None:
                    self._ensure_event_worker()
                    self._event_q.put(msg)
                elif "reply" in msg or msg.get("type") == "reply":
                    if (q := self._pending.get(int(msg.get("seq_id") or 0))) is not None:
                        try: q.put_nowait(msg)
                        except queue.Full: pass
                elif self._shim_graceful and not got_ready and msg.get("ok") is False:
                    self._disable_shim_once(str(msg.get("error") or f"{self._shim_label} startup failed"))
                    self._ready.set()
        finally:
            need_respawn = False
            with self._shim_lock:
                if self._shim is proc:
                    self._shim = None
                    self._ready.set()
                    if not got_ready and self._shim_graceful and not self._closed:
                        self._disable_shim_once(self.last_shim_error or f"{self._shim_label} exited before ready")
                    need_respawn = got_ready and not self._closed and not self._shim_disabled and self._on_event is not None
            _reap_shim_proc(proc)
            for q in list(self._pending.values()):
                try: q.put_nowait({"error": "shim_eof"})
                except queue.Full: pass
            if need_respawn:
                threading.Thread(target=self._respawn_shim_bg, daemon=True).start()

    def _shim_rpc(self, payload: dict[str, Any], timeout_sec: float = 5.0) -> dict[str, Any] | None:
        for attempt in range(2):
            if not self._ensure_shim():
                if self._shim_graceful:
                    return None
                raise RuntimeError(self.last_shim_error or f"{self._shim_label} unavailable")
            sid = 0
            q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
            broken: subprocess.Popen[str] | None = None
            try:
                with self._shim_lock:
                    proc = self._shim
                    if proc is None or proc.stdin is None or proc.poll() is not None:
                        broken, self._shim = proc, None
                        raise BrokenPipeError("shim_not_running")
                    self._rpc_seq += 1
                    sid = self._rpc_seq
                    self._pending[sid] = q
                    proc.stdin.write(json.dumps({**payload, "seq_id": sid}, ensure_ascii=False) + "\n")
                    proc.stdin.flush()
                res = q.get(timeout=timeout_sec)
                if res.get("error") == "shim_eof" and attempt == 0 and not self._closed and not self._shim_disabled:
                    continue
                if "error" in res:
                    if self._shim_graceful:
                        return None
                    raise RuntimeError(str(res["error"]))
                return res
            except (BrokenPipeError, OSError, queue.Empty):
                if broken is not None:
                    _reap_shim_proc(broken)
                if attempt == 0 and not self._closed and not self._shim_disabled:
                    continue
                if self._shim_graceful:
                    return None
                raise
            finally:
                if sid:
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

    def hud(self, state: str, text: str = "", duration_ms: int = 0) -> None:
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
            if st not in ("", "idle", "hide"):
                req["position"] = normalize_ui_position(self.cfg.ui.position)
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
            if eff_text:
                req["text"] = eff_text
            if duration_ms > 0:
                req["duration_ms"] = duration_ms
            self._shim_rpc(req, timeout_sec=1.5)
        except Exception:
            pass

    def close(self) -> None:
        self._closed = True
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
                if proc.stdin:
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
        self._recorder_lost_cb: Callable[[], None] | None = None
        self._init_shim_bridge()

    def set_recorder_lost_callback(self, cb: Callable[[], None] | None) -> None:
        self._recorder_lost_cb = cb

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

    def start_recording(self, runtime_dir: Path, *, loopback: bool = False) -> None:
        self.last_audio_error = ""
        eff_loopback = bool(loopback or self.env.get("AGENT_VOICE_INPUT_WASAPI_LOOPBACK") == "1" or self.env.get("VOICE_INPUT_WASAPI_LOOPBACK") == "1")
        if self._use_winmm:
            req: dict[str, Any] = {"cmd": "rec_start"}
            if eff_loopback:
                req["loopback"] = True
            res = self._shim_rpc(req, timeout_sec=4.0) or {}
            if res.get("ok"):
                self._rec_mode = "winmm"
                return
            err = str(res.get("rec_error") or "winmm rec_start failed")
            self.last_audio_error = err
            if any(code in err.lower() for code in ("0x80070490", "0x80070005")):
                raise NoMicrophoneError(f"no microphone input device available ({err})")
            if self.cfg.audio.wsl_source == "auto" and not eff_loopback:
                self._rec_mode = "pulse"
                try:
                    self._ff_rec.start(
                        "pulse",
                        self.pulse_source,
                        runtime_dir,
                        self.env,
                        verify_startup=True,
                        on_unexpected_exit=self._recorder_lost_cb,
                    )
                except Exception:
                    self._rec_mode = ""
                    self.last_audio_error = self._ff_rec.diagnostic_summary() or err
                    raise
                return
            raise NoMicrophoneError(f"no microphone input device available ({err})") if "wasapi_" in err.lower() else RuntimeError(err)
        self._rec_mode = "pulse"
        src = "RDPSink.monitor" if (eff_loopback and self.pulse_source == "RDPSource") else self.pulse_source
        try:
            self._ff_rec.start(
                "pulse",
                src,
                runtime_dir,
                self.env,
                verify_startup=True,
                on_unexpected_exit=self._recorder_lost_cb,
            )
        except Exception:
            self._rec_mode = ""
            self.last_audio_error = self._ff_rec.diagnostic_summary()
            raise

    def stop_recording(self, *, drain: bool = True) -> tuple[bytes, str]:
        mode, self._rec_mode = self._rec_mode, ""
        drain_sec = self.cfg.audio.effective_tail_drain_sec(macos=False) if (drain and mode) else 0.0
        if mode == "winmm":
            if drain_sec > 0.0:
                time.sleep(drain_sec)
            res = self._shim_rpc({"cmd": "rec_stop"}, timeout_sec=5.0) or {}
            b64 = str(res.get("wav_b64") or "")
            wav = base64.b64decode(b64) if b64 else b""
            self.last_audio_error = "" if wav else str(res.get("rec_error") or "wasapi_empty_audio")
            return wav, "winmm"
        wav = self._ff_rec.stop(drain_sec=drain_sec)
        self.last_audio_error = "" if wav else self._ff_rec.diagnostic_summary()
        return wav, "pulse"

    def cancel_recording(self) -> None:
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

    def deliver_text(self, text: str, seq_id: int, target_hwnd: int = 0, *, origin_window: ForegroundWindow | None = None) -> PasteOutcome:
        eff_hwnd = target_hwnd or (origin_window.hwnd if origin_window is not None else 0)
        res = self._shim_rpc({"cmd": "paste", "seq_id": seq_id, "text": text, "target_hwnd": eff_hwnd, "expect_terminal": True}, timeout_sec=5.0) or {}
        return PasteOutcome(bool(res.get("pasted")), bool(res.get("clipboard")), str(res.get("reason") or "ok"), str(res.get("fg_process") or ""))
