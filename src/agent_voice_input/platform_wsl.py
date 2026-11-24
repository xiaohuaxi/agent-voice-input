"""WSL2 host bridge: WASAPI 16kHz capture via win_shim.ps1 stdio JSONL + PulseAudio RDPSource fallback."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
from typing import Any, Callable, Mapping

from .app_scope import (
    WINDOWS_TERMINALS,
    is_windows_terminal_process,
    normalize_app_token,
    resolve_windows_paste_chord,
)
from .audio_proc import pack_pcm16_mono_wav, resolve_ffmpeg_bin
from .config import Config, ConfigError, redact_secrets, validate_hotkey_for_platform
from .http_transport import sleep_cooperative
from .hud import resolve_ui_language
from .platform_common import (
    FfmpegWavRecorder,
    ForegroundWindow,
    JsonlShimMixin,
    MicrophoneUnavailableError,
    NoMicrophoneError,
    PasteOutcome,
    _ShimWriteTimeout,
    classify_ffmpeg_startup_error,
    invoke_recorder_lost_callback,
    invoke_recorder_recovered_callback,
    make_once_commit_guard,
    stream_key_test_proc,
)
from .session_ctx import parse_window_title_hint

POWERSHELL_EXE = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
_POWERSHELL_EXE_ROOT_MOUNT = "/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
WSLG_PULSE_SOCK = "/mnt/wslg/PulseServer"
SHIM_SCRIPT = Path(__file__).resolve().with_name("win_shim.ps1")
WIN_SHIM_DIR = Path(__file__).resolve().with_name("win_shim")


def list_win_shim_sources(
    ps_script: Path = SHIM_SCRIPT,
    cs_dir: Path | None = None,
) -> list[Path]:
    """Return ``win_shim.ps1`` followed by sorted ``win_shim/*.cs`` source files."""
    sources: list[Path] = []
    if ps_script.is_file():
        sources.append(ps_script)
    eff_cs_dir = cs_dir if cs_dir is not None else ps_script.resolve().with_name("win_shim")
    if eff_cs_dir.is_dir():
        sources.extend(sorted(p for p in eff_cs_dir.glob("*.cs") if p.is_file()))
    return sources


def read_win_shim_source(
    ps_script: Path = SHIM_SCRIPT,
    cs_dir: Path | None = None,
) -> str:
    """Read and join ``win_shim.ps1`` and all ``win_shim/*.cs`` sources."""
    sources = list_win_shim_sources(ps_script, cs_dir=cs_dir)
    if not sources:
        raise FileNotFoundError(f"win_shim sources not found at {ps_script}")
    return "\n".join(p.read_text(encoding="utf-8") for p in sources)


def resolve_powershell_exe(env: Mapping[str, str] | None = None) -> str | None:
    """Locate Windows ``powershell.exe`` across default mount, PATH, and custom ``automount.root = /``."""
    if os.path.isfile(POWERSHELL_EXE):
        return POWERSHELL_EXE
    raw_path = None if env is None else env.get("PATH")
    found = (
        shutil.which("powershell.exe", path=raw_path)
        if raw_path is not None
        else shutil.which("powershell.exe")
    )
    if found and os.path.isfile(found):
        return found
    if os.path.isfile(_POWERSHELL_EXE_ROOT_MOUNT):
        return _POWERSHELL_EXE_ROOT_MOUNT
    return None



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


def run_win_shim_self_check(
    mode: str = "self-check",
    *,
    env: Mapping[str, str] | None = None,
    cache_dir: Path | str | None = None,
    timeout: float = 15.0,
) -> tuple[bool, dict[str, Any], str]:
    """Run ``win_shim.ps1 -Mode self-check`` (or ``clip-self-check``) headlessly with closed stdin."""
    if mode not in ("self-check", "clip-self-check"):
        raise ValueError(f"unsupported win_shim self-check mode: {mode!r}")
    ps_exe = resolve_powershell_exe(env)
    if not ps_exe:
        return False, {}, f"PowerShell not found at {POWERSHELL_EXE}"
    try:
        cmd = [
            ps_exe,
            "-NoProfile",
            "-NonInteractive",
            "-Sta",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            to_windows_path(SHIM_SCRIPT),
            "-Mode",
            mode,
        ]
        if cache_dir is not None and str(cache_dir).strip():
            raw_cache = str(cache_dir).strip()
            win_cache = (
                raw_cache
                if ((len(raw_cache) >= 2 and raw_cache[1] == ":") or raw_cache.startswith("\\\\"))
                else to_windows_path(raw_cache)
            )
            cmd.extend(["-CacheDir", win_cache])
        p = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=None if env is None else dict(env),
            timeout=timeout,
            check=False,
        )
    except Exception as exc:
        return False, {}, str(exc)
    if p.returncode != 0 or not p.stdout.strip():
        err_txt = (p.stderr or p.stdout or f"rc={p.returncode}").strip()
        return False, {}, err_txt
    try:
        parsed = json.loads(p.stdout.strip().splitlines()[-1])
    except Exception as exc:
        return False, {}, f"invalid self-check JSON: {exc}"
    if not isinstance(parsed, dict) or not parsed.get("ok") or parsed.get("mode") != mode:
        return False, {}, f"unexpected self-check payload: {parsed!r}"
    return True, parsed, ""


def resolve_wsl_effective_audio_source(
    cfg: Config,
    env: Mapping[str, str] | None = None,
    *,
    pulse_source: str = "RDPSource",
    has_powershell: bool | None = None,
) -> tuple[str, str]:
    """Return ``(backend, effective_pulse_source)`` matching ``WslPlatform`` runtime capture selection."""
    raw_env = os.environ if env is None else env
    explicit_pulse = (
        raw_env.get("AGENT_VOICE_INPUT_PULSE_SOURCE")
        or raw_env.get("VOICE_INPUT_PULSE_SOURCE")
        or ""
    ).strip()
    eff_pulse_src = explicit_pulse or pulse_source.strip() or "RDPSource"
    wsl_src = (cfg.audio.wsl_source or "auto").strip().lower()
    if wsl_src in ("winmm", "wasapi"):
        return wsl_src, eff_pulse_src
    if wsl_src == "pulse":
        return "pulse", eff_pulse_src
    has_custom_pulse = bool(explicit_pulse or pulse_source.strip() not in ("", "RDPSource"))
    if has_custom_pulse:
        return "pulse", eff_pulse_src
    ps_avail = (
        resolve_powershell_exe(raw_env) is not None
        if has_powershell is None
        else bool(has_powershell)
    )
    return ("wasapi" if ps_avail else "pulse"), eff_pulse_src


def _extract_pulse_source_names(output: str) -> set[str]:
    """Extract exact PulseAudio source identifiers from ``ffmpeg -sources pulse`` output."""
    names: set[str] = set()
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.lower().startswith("auto-detected sources"):
            continue
        if line.startswith("*"):
            line = line[1:].lstrip()
        if not line:
            continue
        tok = line.split(None, 1)[0]
        if tok:
            names.add(tok)
    return names


def probe_wsl_host(cfg: Config, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    raw_env = os.environ if env is None else env
    eff_env = _pulse_env(raw_env)
    _, eff_pulse_src = resolve_wsl_effective_audio_source(cfg, raw_env)
    info: dict[str, Any] = {
        "powershell_ok": False,
        "pulse_rdp_ok": False,
        "pulse_source": eff_pulse_src,
        "pulse_source_ok": False,
        "winmm_devs": 0,
        "fg_process": "",
        "fg_title": "",
    }
    try:
        p = subprocess.run([resolve_ffmpeg_bin(), "-nostdin", "-hide_banner", "-sources", "pulse"], stdin=subprocess.DEVNULL, capture_output=True, text=True, env=eff_env, timeout=5, check=False)
        if p.returncode == 0:
            pulse_names = _extract_pulse_source_names(p.stdout + "\n" + p.stderr)
            info["pulse_rdp_ok"] = "RDPSource" in pulse_names
            info["pulse_source_ok"] = eff_pulse_src in pulse_names
    except Exception as exc: info["pulse_error"] = str(exc)
    ps_exe = resolve_powershell_exe(raw_env)
    if ps_exe:
        try:
            p = subprocess.run(
                [ps_exe, "-NoProfile", "-NonInteractive", "-Sta", "-ExecutionPolicy", "Bypass", "-File", to_windows_path(SHIM_SCRIPT), "-Mode", "probe"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15, check=False,
            )
            if p.returncode == 0 and p.stdout.strip():
                d = json.loads(p.stdout.strip().splitlines()[-1])
                ui_cult = str(d.get("ui_culture") or "").strip()
                info.update(powershell_ok=bool(d.get("ok")), ps_version=str(d.get("ps_version") or ""), winmm_devs=int(d.get("wave_in_devs") or 0), fg_process=str(d.get("process") or ""), fg_title=str(d.get("title") or ""), fg_hwnd=int(d.get("hwnd") or 0), ui_culture=ui_cult, ui_lang=resolve_ui_language(cfg.ui.language, env=raw_env, win_ui_culture=ui_cult))
                if "wasapi_ok" in d:
                    info["wasapi_ok"] = bool(d.get("wasapi_ok"))
                if "wasapi_state" in d:
                    info["wasapi_state"] = str(d.get("wasapi_state") or "")
                if not d.get("ok") and d.get("error"):
                    info["ps_error"] = redact_secrets(str(d.get("error")), raw_env, cfg=cfg)[:240]
            else:
                raw_lines = [ln.strip() for ln in (p.stderr or p.stdout or "").splitlines() if ln.strip()]
                first_err = raw_lines[0] if raw_lines else f"exit code {p.returncode}"
                info["ps_error"] = redact_secrets(first_err, raw_env, cfg=cfg)[:240]
        except Exception as exc:
            info["ps_error"] = redact_secrets(str(exc), raw_env, cfg=cfg)[:240]
    return info


def run_key_test(cfg: Config, seconds: int = 10) -> int:
    hk = validate_hotkey_for_platform(cfg.hotkey.key, "wsl")
    ps_exe = resolve_powershell_exe()
    if not ps_exe:
        raise RuntimeError(f"PowerShell not found at {POWERSHELL_EXE}")
    cmd = [
        ps_exe, "-NoProfile", "-Sta", "-ExecutionPolicy", "Bypass",
        "-File", to_windows_path(SHIM_SCRIPT),
        "-Mode", "key-test",
        "-Hotkey", hk,
        "-TriggerMode", cfg.hotkey.trigger_mode,
        "-TapMaxMs", str(cfg.hotkey.tap_max_ms),
        "-HoldCancelMs", str(cfg.hotkey.effective_hold_cancel_ms()),
        "-HoldHintMs", str(cfg.hotkey.effective_hold_hint_ms()),
        "-DebounceMs", str(cfg.hotkey.debounce_ms),
        "-Seconds", str(max(1, seconds)),
    ]
    return stream_key_test_proc(cmd)


class WslPlatform(JsonlShimMixin):
    _shim_graceful = False
    _shim_ready_timeout = 15.0
    _shim_label = "win_shim"

    def _host_ui_culture(self) -> str | None:
        # Intentional on WSL2: always pass a string ("" when omitted) so HUD language never falls back to WSL Linux LANG.
        return self._win_ui_culture

    def __init__(self, cfg: Config, *, env: Mapping[str, str] | None = None, pulse_source: str = "RDPSource", shim_cmd_factory: Callable[[], list[str]] | None = None) -> None:
        raw_env = os.environ if env is None else env
        self.cfg, self.env = cfg, _pulse_env(raw_env)
        eff_backend, self.pulse_source = resolve_wsl_effective_audio_source(
            cfg, raw_env, pulse_source=pulse_source
        )
        self._shim_cmd_factory = shim_cmd_factory
        if cfg.audio.wsl_source in ("winmm", "wasapi"):
            self._primary_winmm = True
        elif cfg.audio.wsl_source == "pulse":
            self._primary_winmm = False
        else:
            self._primary_winmm = bool(shim_cmd_factory is None and eff_backend in ("wasapi", "winmm"))
        self._use_winmm = self._primary_winmm
        self.last_audio_error = ""
        self._ff_rec, self._rec_mode = FfmpegWavRecorder(), ""
        self._winmm_state_lock = threading.Lock()
        self._winmm_gen = 0
        self._active_seq_id: int | None = None
        self._winmm_starting = False
        self._winmm_rpc_dispatched = False
        self._rec_writer: Any = None
        self._pending_winmm_lost_reason = ""
        self._pending_winmm_recovered_gaps: list[int] = []
        self._recorder_lost_cb: Callable[..., Any] | None = None
        self._recorder_recovered_cb: Callable[..., Any] | None = None
        self._paste_hold_timeout_sec: float | None = None
        self._rec_stop_timeout_sec: float = 15.0
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
        try:
            hk = validate_hotkey_for_platform(self.cfg.hotkey.key, "wsl")
        except ConfigError as exc:
            self.last_shim_error = redact_secrets(str(exc), self.env, cfg=self.cfg)
            raise
        ps_exe = resolve_powershell_exe(self.env)
        if not ps_exe:
            raise RuntimeError(f"PowerShell not found at {POWERSHELL_EXE}")
        return [
            ps_exe,
            "-NoProfile",
            "-NonInteractive",
            "-Sta",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            to_windows_path(SHIM_SCRIPT),
            "-Mode",
            "bridge",
            "-Hotkey",
            hk,
            "-TriggerMode",
            self.cfg.hotkey.trigger_mode,
            "-TapMaxMs",
            str(self.cfg.hotkey.tap_max_ms),
            "-HoldCancelMs",
            str(self.cfg.hotkey.effective_hold_cancel_ms()),
            "-HoldHintMs",
            str(self.cfg.hotkey.effective_hold_hint_ms()),
            "-DebounceMs",
            str(self.cfg.hotkey.debounce_ms),
            "-Beep",
            "1" if self.cfg.audio.beep else "0",
            "-AllowedTerminals",
            ",".join(sorted(WINDOWS_TERMINALS)),
        ]

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
        if "wasapi_startup_no_frames" in low:
            return MicrophoneUnavailableError(
                f"microphone produced no audio frames ({err})",
                reason_code="mic_no_stream",
                hud_state="mic_no_stream",
            )
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
            with self._winmm_state_lock:
                if self._closed:
                    raise RuntimeError(f"{type(self).__name__} is closed")
                self._winmm_gen += 1
                my_gen = self._winmm_gen
                self._active_seq_id = seq_id
                self._rec_mode = ""
                self._winmm_starting = True
                self._winmm_rpc_dispatched = False
                self._rec_writer = None
                self._pending_winmm_lost_reason = ""
                self._pending_winmm_recovered_gaps.clear()
            req: dict[str, Any] = {"cmd": "rec_start", "rec_gen": my_gen}
            if eff_loopback:
                req["loopback"] = True

            target_writer: Any = None
            try:
                if not self._ensure_shim():
                    raise RuntimeError(self.last_shim_error or f"{self._shim_label} unavailable")
                target_writer = self._current_shim_writer()
                if (
                    target_writer is None
                    and getattr(self._shim_rpc, "__func__", None) is JsonlShimMixin._shim_rpc
                ):
                    if not self._ensure_shim():
                        raise RuntimeError(self.last_shim_error or f"{self._shim_label} unavailable")
                    target_writer = self._current_shim_writer()
                    if target_writer is None:
                        raise BrokenPipeError("shim_not_running")
                with self._winmm_state_lock:
                    if self._closed or self._winmm_gen != my_gen:
                        raise self._classify_wasapi_start_error("mic_busy:wasapi_aborted")
                    self._winmm_rpc_dispatched = True
                    self._rec_writer = target_writer
                res = (
                    self._shim_rpc(
                        req,
                        timeout_sec=4.0,
                        allow_retry=False,
                        target_writer=target_writer,
                    )
                    or {}
                )
            except Exception as shim_exc:
                fb_gen = 0
                should_discard_winmm = False
                with self._winmm_state_lock:
                    if self._winmm_gen == my_gen:
                        # Only send discard when rec_start was fully written and only the reply timed out
                        # (queue.Empty). On write_status=="not_sent" (0 bytes) or "partial" (writer already
                        # closed stdin & reaped shim), never send discard or kill a healthy shim.
                        should_discard_winmm = (
                            isinstance(shim_exc, queue.Empty) and not self._closed
                        )
                        self._winmm_starting = False
                        self._winmm_rpc_dispatched = False
                        self._rec_writer = None
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
                            if isinstance(self._ff_rec, FfmpegWavRecorder):
                                self._ff_rec.reset_abort()
                if should_discard_winmm and target_writer is not None:
                    discard_req = {"cmd": "rec_stop", "rec_gen": my_gen, "discard": True}
                    status = self._write_shim_command(discard_req, writer=target_writer, timeout_sec=0.25)
                    if status != "sent" and not self._closed:
                        self._poison_and_reap_writer(target_writer)
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
                            self._rec_writer = None
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
                    if not self._closed and target_writer is not None:
                        self._write_shim_command(
                            {"cmd": "rec_stop", "rec_gen": my_gen, "discard": True},
                            writer=target_writer,
                            timeout_sec=0.25,
                        )
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
                    self._rec_writer = None
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
            if isinstance(self._ff_rec, FfmpegWavRecorder):
                self._ff_rec.reset_abort()
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

    def stop_recording(self, *, drain: bool = True, _discard: bool = False) -> tuple[bytes, str]:
        self._stop_level_streamer()
        with self._winmm_state_lock:
            stop_gen = self._winmm_gen
            self._winmm_gen += 1
            next_gen = self._winmm_gen
            prev_seq_id = self._active_seq_id
            self._active_seq_id = None
            was_dispatched = self._winmm_rpc_dispatched
            rec_writer, self._rec_writer = self._rec_writer, None
            self._winmm_starting = False
            self._winmm_rpc_dispatched = False
            self._pending_winmm_lost_reason = ""
            self._pending_winmm_recovered_gaps.clear()
            mode, self._rec_mode = self._rec_mode, ""
        drain_sec = self.cfg.audio.effective_tail_drain_sec(macos=False) if (drain and mode) else 0.0
        if mode == "winmm" or was_dispatched:
            if self._closed:
                return b"", "winmm"
            if drain_sec > 0.0:
                time.sleep(drain_sec)
            stop_req: dict[str, Any] = {"cmd": "rec_stop", "rec_gen": stop_gen}
            if _discard:
                stop_req["discard"] = True
            should_release = False
            try:
                # Single generous wait on the serial JSONL pipe: avoids queuing a duplicate 25MB
                # rec_stop that would block the immediately following rec_start.
                res = (
                    self._shim_rpc(
                        stop_req,
                        timeout_sec=self._rec_stop_timeout_sec,
                        allow_retry=False,
                        target_writer=rec_writer,
                    )
                    or {}
                )
                b64 = str(res.get("wav_b64") or "")
                wav = base64.b64decode(b64) if b64 else b""
                if not _discard and stop_gen > 0 and (wav or res.get("rec_gen") == stop_gen or res.get("rec_error")):
                    should_release = True
                rec_err = str(res.get("rec_error") or "").strip()
                self.last_audio_error = rec_err if rec_err else ("" if wav else "wasapi_empty_audio")
                return wav, "winmm"
            except _ShimWriteTimeout as wto:
                # Zero bytes entered stdin -> healthy Windows shim never received rec_stop and is
                # still recording stop_gen. Restore session state so a subsequent cancel_recording()
                # or retry stop_recording() still targets the same writer and rec_gen.
                if wto.write_status == "not_sent":
                    with self._winmm_state_lock:
                        if not self._closed and self._winmm_gen == next_gen:
                            self._winmm_gen = stop_gen
                            self._rec_mode = mode
                            self._rec_writer = rec_writer
                            self._winmm_rpc_dispatched = was_dispatched
                            self._active_seq_id = prev_seq_id
                raise
            finally:
                # Only release the Windows single-slot StoppedRecSlot after receiving and decoding
                # an explicit rec_stop reply. On RPC/pipe timeout or error, never send rec_release
                # blindly (Windows may still be finalizing the WAV, and the single slot is bounded
                # and automatically cleared by the next rec_start / cancel_recording).
                if should_release:
                    self._enqueue_shim_async(
                        {"cmd": "rec_release", "rec_gen": stop_gen},
                        deadline_sec=0.25,
                        writer=rec_writer,
                    )
        wav = self._ff_rec.stop(drain_sec=drain_sec)
        self.last_audio_error = "" if wav else self._ff_rec.diagnostic_summary()
        return wav, "pulse"

    def cancel_recording(self) -> None:
        self._stop_level_streamer()
        with self._winmm_state_lock:
            orig_gen = self._winmm_gen if (self._rec_mode == "winmm" or self._winmm_rpc_dispatched) else 0
            orig_writer = self._rec_writer if orig_gen > 0 else None
        try:
            self.stop_recording(drain=False, _discard=True)
        except _ShimWriteTimeout as wto:
            if wto.write_status != "not_sent":
                return
            reap_writer: Any = None
            with self._winmm_state_lock:
                if self._closed:
                    return
                if orig_gen > 0 and self._winmm_gen == orig_gen and self._rec_writer is orig_writer:
                    self._winmm_gen += 1
                    self._active_seq_id = None
                    self._rec_writer = None
                    self._winmm_starting = False
                    self._winmm_rpc_dispatched = False
                    self._pending_winmm_lost_reason = ""
                    self._pending_winmm_recovered_gaps.clear()
                    self._rec_mode = ""
                    reap_writer = orig_writer
                elif orig_writer is not None and self._rec_writer is not orig_writer:
                    reap_writer = orig_writer
            if reap_writer is not None and not self._closed:
                self._poison_and_reap_writer(reap_writer)
        except Exception:
            pass

    def switch_to_fallback_audio(self) -> bool:
        if self.cfg.audio.wsl_source != "auto":
            return False
        if not self._use_winmm:
            self._use_winmm = True
            return True
        if not self._primary_winmm:
            self._use_winmm = False
        return False

    def _idle_paste_hold_timeout_sec(self) -> float:
        return max(5.0, (self.cfg.hotkey.effective_hold_cancel_ms() / 1000.0) + 2.0)

    def _effective_paste_hold_timeout_sec(self) -> float:
        if self._paste_hold_timeout_sec is not None:
            return max(0.0, float(self._paste_hold_timeout_sec))
        if self.cfg.hotkey.trigger_mode != "ptt":
            return self._idle_paste_hold_timeout_sec()
        return max(15.0, float(self.cfg.audio.max_duration_sec) + 2.0)

    def _dispatch_paste_rpc(self, req: dict[str, Any], timeout_sec: float = 5.0) -> dict[str, Any]:
        return self._shim_rpc(req, timeout_sec=timeout_sec, allow_retry=False) or {}

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
        same_window_verifier: Callable[[ForegroundWindow], bool] | None = None,
    ) -> PasteOutcome:
        eff_hwnd = target_hwnd or (origin_window.hwnd if origin_window is not None else 0)
        origin_proc = normalize_app_token(origin_window.process) if origin_window is not None else ""
        known_origin = (eff_hwnd > 0) or bool(origin_proc)
        expect_terminal = not known_origin
        paste_chord = resolve_windows_paste_chord(origin_proc) if origin_proc else "ctrl_v"
        cancelled_fn = lambda: bool(is_cancelled is not None and is_cancelled())
        _confirm_commit = make_once_commit_guard(cancelled_fn, before_commit)

        def _fallback_clipboard_via_shim() -> PasteOutcome:
            if not _confirm_commit():
                return PasteOutcome(False, False, "cancelled", "")
            fb_req: dict[str, Any] = {
                "cmd": "paste",
                "seq_id": seq_id,
                "text": text,
                "target_hwnd": eff_hwnd,
                "expect_terminal": expect_terminal,
                "expect_process": origin_proc,
                "paste_chord": paste_chord,
                "force_clipboard_only": True,
                "deadline_ms": 3800,
            }
            if fallback_clipboard_text is not None:
                fb_req["fallback_text"] = fallback_clipboard_text
            if bool(suppress_beep() if callable(suppress_beep) else suppress_beep):
                fb_req["suppress_beep"] = True
            try:
                res_fb = self._dispatch_paste_rpc(fb_req, timeout_sec=5.0)
            except Exception:
                return PasteOutcome(False, False, "shim_paste_unconfirmed", origin_proc, used_clipboard_paste=False)
            if not isinstance(res_fb, dict) or res_fb.get("ok") is False or "error" in res_fb or "clipboard" not in res_fb:
                return PasteOutcome(False, False, "shim_paste_unconfirmed", origin_proc, used_clipboard_paste=False)
            clip_ok = bool(res_fb.get("clipboard"))
            fg_proc_fb = str(res_fb.get("fg_process") or "")
            return PasteOutcome(
                False,
                clip_ok,
                "focus_changed" if clip_ok else str(res_fb.get("reason") or "clipboard_failed"),
                fg_proc_fb,
                used_clipboard_paste=False,
            )

        if cancelled_fn():
            return PasteOutcome(False, False, "cancelled", "")
        if before_clipboard_write is not None:
            before_clipboard_write()
        if cancelled_fn():
            return PasteOutcome(False, False, "cancelled", "")
        if origin_window is not None and origin_window.origin_drifted:
            return _fallback_clipboard_via_shim()
        orig_session_tok = ""
        if (
            origin_window is not None
            and is_windows_terminal_process(origin_proc)
            and (orig_hint := parse_window_title_hint(origin_window.title)) is not None
        ):
            orig_session_tok = orig_hint.session_token

        hold_timeout_sec = self._effective_paste_hold_timeout_sec()
        hold_deadline = time.monotonic() + hold_timeout_sec
        waited_for_key = False
        if not self._closed and self._shim is not None and self._shim.poll() is None:
            while True:
                if cancelled_fn():
                    return PasteOutcome(False, False, "cancelled", "")
                if self._closed or self._shim is None or self._shim.poll() is not None:
                    break
                try:
                    ks = self._dispatch_paste_rpc({"cmd": "key_state"}, timeout_sec=0.8)
                except Exception:
                    return PasteOutcome(False, False, "shim_paste_unconfirmed", origin_proc, used_clipboard_paste=False)
                if cancelled_fn():
                    return PasteOutcome(False, False, "cancelled", "")
                if not isinstance(ks, dict) or ks.get("ok") is False or "error" in ks:
                    return PasteOutcome(False, False, "shim_paste_unconfirmed", origin_proc, used_clipboard_paste=False)
                if not ks.get("hotkey_held"):
                    now_flush = time.monotonic()
                    rem_flush = hold_deadline - now_flush
                    if (waited_for_key or hold_timeout_sec > 0.0) and rem_flush <= 0.0:
                        return _fallback_clipboard_via_shim()
                    flush_wait = min(0.05, max(0.005, rem_flush)) if rem_flush > 0.0 else 0.005
                    flushed = self._flush_shim_events(timeout_sec=flush_wait)
                    if cancelled_fn():
                        return PasteOutcome(False, False, "cancelled", "")
                    if not flushed:
                        waited_for_key = True
                        now_after_flush = time.monotonic()
                        rem = hold_deadline - now_after_flush
                        if rem <= 0.0:
                            return _fallback_clipboard_via_shim()
                        if (now_after_flush - now_flush) < 0.005:
                            sleep_cooperative(min(0.05, rem), sleep_fn=time.sleep, is_cancelled=cancelled_fn)
                        continue
                    if (waited_for_key or hold_timeout_sec > 0.0) and (hold_deadline - time.monotonic()) <= 0.0:
                        return _fallback_clipboard_via_shim()
                    break
                rem_ks = hold_deadline - time.monotonic()
                if rem_ks <= 0.0:
                    return _fallback_clipboard_via_shim()
                waited_for_key = True
                sleep_cooperative(min(0.05, rem_ks), sleep_fn=time.sleep, is_cancelled=cancelled_fn)

        if cancelled_fn():
            return PasteOutcome(False, False, "cancelled", "")
        if same_window_verifier is not None:
            try:
                cur_fg = self.get_foreground_window()
                verified = bool(same_window_verifier(cur_fg))
            except Exception:
                verified = False
            if not verified:
                return _fallback_clipboard_via_shim()
        if not _confirm_commit():
            return PasteOutcome(False, False, "cancelled", "")
        req: dict[str, Any] = {
            "cmd": "paste",
            "seq_id": seq_id,
            "text": text,
            "target_hwnd": eff_hwnd,
            "expect_terminal": expect_terminal,
            "expect_process": origin_proc,
            "paste_chord": paste_chord,
            "deadline_ms": 3800,
        }
        if orig_session_tok:
            req["expect_session_token"] = orig_session_tok
        if fallback_clipboard_text is not None and fallback_clipboard_text != text:
            req["fallback_text"] = fallback_clipboard_text
        if bool(suppress_beep() if callable(suppress_beep) else suppress_beep):
            req["suppress_beep"] = True
        try:
            res = self._dispatch_paste_rpc(req, timeout_sec=5.0)
        except Exception:
            # RPC timed out or reply lost: NEVER send a second blind clipboard-overwrite RPC;
            # return unconfirmed so daemon stashes WAV in failed/ for retry.
            return PasteOutcome(False, False, "shim_paste_unconfirmed", origin_proc, used_clipboard_paste=False)
        if not isinstance(res, dict) or res.get("ok") is False or "error" in res or "clipboard" not in res:
            return PasteOutcome(False, False, "shim_paste_unconfirmed", origin_proc, used_clipboard_paste=False)
        pasted = bool(res.get("pasted"))
        clip_ok = bool(res.get("clipboard"))
        reason = str(res.get("reason") or ("ok" if pasted else ("clipboard_only" if clip_ok else "clipboard_failed")))
        fg_proc = str(res.get("fg_process") or "")
        return PasteOutcome(
            pasted,
            clip_ok,
            reason,
            fg_proc,
            used_clipboard_paste=bool(pasted and clip_ok),
        )
