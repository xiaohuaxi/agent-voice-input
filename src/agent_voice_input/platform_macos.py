"""macOS native bridge: AVFoundation capture, Ghostty split-aware input, and mac_shim.swift hotkey/HUD."""
from __future__ import annotations

import hashlib, os, platform as py_platform, re, shutil, subprocess, threading, time
from pathlib import Path
from typing import Any, Callable, Mapping

from .audio_proc import resolve_ffmpeg_bin
from .config import Config, ensure_secure_dir, expand_user_path, redact_secrets
from .platform_wsl import FfmpegWavRecorder, ForegroundWindow, JsonlShimMixin, PasteOutcome, stream_key_test_proc

GHOSTTY_BUNDLE_ID = "com.mitchellh.ghostty"
MAC_SHIM_SCRIPT = Path(__file__).resolve().with_name("mac_shim.swift")
MACOS_TERMINALS = frozenset({"terminal", "iterm2", "iterm", "wezterm", "wezterm-gui", "alacritty", "ghostty", "kitty", "warp"})
SOUND_MAP = {"start": "Tink.aiff", "stop": "Pop.aiff", "focus": "Glass.aiff", "error": "Basso.aiff"}

_APPLE_SEP = "\x1f"
_SPINNER_PREFIX_RE = re.compile(r"^[\u2800-\u28ff✳✢✶✻✽·•\s]+")
_AVF_AUDIO_DEV_RE = re.compile(r"\[(\d+)\]\s+(.+?)\s*$")
_OSA_CODE_RE = re.compile(r"\((-?\d+)\)")

_FRONTMOST_APP_SCRIPT = """\
tell application "System Events"
    set p to first process whose frontmost is true
    set {appName, bundleId, appPid, winTitle} to {"", "", 0, ""}
    try
        set appName to name of p
    end try
    try
        set bundleId to bundle identifier of p
    end try
    try
        set appPid to unix id of p
    end try
    if bundleId is not "com.mitchellh.ghostty" and appName is not "Ghostty" and appName is not "ghostty" then
        try
            set winTitle to value of attribute "AXTitle" of front window of p
        end try
    end if
    set sep to character id 31
    return appName & sep & bundleId & sep & (appPid as text) & sep & winTitle
end tell"""

_FRONTMOST_AXTITLE_SCRIPT = """\
tell application "System Events"
    set p to first process whose frontmost is true
    try
        return value of attribute "AXTitle" of front window of p
    end try
    return ""
end tell"""

_GHOSTTY_QUERY_SCRIPT = """\
tell application id "com.mitchellh.ghostty"
    if not running or (count of windows) is 0 then return ""
    set t to focused terminal of selected tab of front window
    set tid to id of t
    set {tname, ttty, tcwd, tpid} to {"", "", "", 0}
    try
        set tname to name of t
    end try
    try
        set ttty to tty of t
    end try
    try
        set tcwd to working directory of t
    end try
    try
        set tpid to pid of t
    end try
    set sep to character id 31
    return tid & sep & tname & sep & ttty & sep & tcwd & sep & (tpid as text)
end tell"""

_GHOSTTY_INPUT_SCRIPT = """\
on run argv
    set targetId to item 1 of argv
    set pasteText to item 2 of argv
    tell application id "com.mitchellh.ghostty"
        if not running or not frontmost then return "not_frontmost"
        if (count of windows) is 0 then return "focus_changed"
        try
            set curId to id of (focused terminal of selected tab of front window)
        on error errMsg number errNum
            if errNum is -1743 then error errMsg number errNum
            return "focus_changed"
        end try
        if curId is not targetId then return "focus_changed"
        input text pasteText to terminal id targetId
        return "ok"
    end tell
end run"""

_CMD_V_SCRIPT = 'tell application "System Events" to keystroke "v" using command down'

MACOS_PERMISSIONS_HINT = (
    "Microphone -> terminal/daemon host (ffmpeg capture); "
    "Automation -> Ghostty (split lookup & direct input without Accessibility); "
    "Input Monitoring -> global non-modifier hotkeys / key-test; "
    "Accessibility -> only needed for non-Ghostty System Events window title / Cmd+V fallback"
)


def is_ghostty_app(app_name: str, bundle_id: str = "") -> bool:
    return bundle_id.strip().lower() == GHOSTTY_BUNDLE_ID or app_name.strip().lower() == "ghostty"


def resolve_macos_audio_device(cfg: Config, env: Mapping[str, str] | None = None) -> str:
    src_env = os.environ if env is None else env
    raw = (
        src_env.get("AGENT_VOICE_INPUT_MACOS_AUDIO_DEVICE")
        or src_env.get("VOICE_INPUT_MACOS_AUDIO_DEVICE")
        or cfg.audio.macos_device
        or "default"
    ).strip()
    if raw.startswith(":"):
        raw = raw[1:].strip()
    return raw or "default"


def parse_avfoundation_audio_devices(output: str) -> list[str]:
    devices: list[str] = []
    in_audio = False
    for raw_ln in output.splitlines():
        ln = raw_ln.strip()
        if "AVFoundation audio devices" in ln:
            in_audio = True
            continue
        if not in_audio:
            continue
        if "AVFoundation video devices" in ln or ln.startswith("Error opening"):
            break
        if m := _AVF_AUDIO_DEV_RE.search(ln):
            devices.append(f"[{m.group(1)}] {m.group(2)}")
    return devices


def get_macos_cache_dir(env: Mapping[str, str] | None = None, *, create: bool = False) -> Path:
    src_env = os.environ if env is None else env
    explicit = (src_env.get("AGENT_VOICE_INPUT_CACHE_DIR") or src_env.get("VOICE_INPUT_CACHE_DIR") or "").strip()
    if explicit:
        cdir = expand_user_path(explicit, env=src_env)
    elif xdg_cache := (src_env.get("XDG_CACHE_HOME") or "").strip():
        cdir = expand_user_path(xdg_cache, env=src_env) / "agent-voice-input"
    else:
        home = expand_user_path("~", env=src_env)
        lib_caches = home / "Library" / "Caches"
        cdir = (lib_caches if lib_caches.is_dir() else (home / ".cache")) / "agent-voice-input"
    if create:
        ensure_secure_dir(cdir)
    return cdir


def resolve_swiftc_bin(env: Mapping[str, str] | None = None) -> str | None:
    src_env = os.environ if env is None else env
    if (override := src_env.get("AGENT_VOICE_INPUT_SWIFTC")) is not None:
        ov = override.strip()
        if ov.lower() in ("", "none", "off", "0", "false"):
            return None
        return ov if (os.path.isfile(ov) and os.access(ov, os.X_OK)) else shutil.which(ov, path=src_env.get("PATH"))
    if os.path.isfile("/usr/bin/swiftc") and os.access("/usr/bin/swiftc", os.X_OK):
        return "/usr/bin/swiftc"
    return shutil.which("swiftc", path=src_env.get("PATH"))


def compile_mac_shim(
    *,
    env: Mapping[str, str] | None = None,
    cache_dir: Path | None = None,
    swift_source: Path = MAC_SHIM_SCRIPT,
    swiftc_bin: str | None = None,
    cfg: Config | None = None,
) -> tuple[Path | None, str]:
    src_env = os.environ if env is None else env
    if not swift_source.is_file():
        return None, f"mac_shim.swift not found at {swift_source}"
    try:
        src_bytes = swift_source.read_bytes()
    except OSError as exc:
        return None, redact_secrets(f"cannot read {swift_source}: {exc}", src_env, cfg=cfg)

    arch = (py_platform.machine() or "unknown").lower()
    digest = hashlib.sha256(src_bytes).hexdigest()[:16]
    try:
        target_dir = ensure_secure_dir(cache_dir if cache_dir is not None else get_macos_cache_dir(src_env, create=False))
    except Exception as exc:
        return None, redact_secrets(f"cannot prepare mac_shim cache dir: {exc}", src_env, cfg=cfg)

    bin_name = f"mac_shim-{arch}-{digest}"
    bin_path = target_dir / bin_name
    if bin_path.is_file() and not bin_path.is_symlink() and os.access(bin_path, os.X_OK):
        return bin_path, ""

    eff_swiftc = swiftc_bin if swiftc_bin is not None else resolve_swiftc_bin(src_env)
    if not eff_swiftc:
        return None, "swiftc not found (install Xcode Command Line Tools: xcode-select --install)"

    tmp_path = target_dir / f".{bin_name}.{os.getpid()}.{threading.get_ident()}.{time.monotonic_ns()}.tmp"
    mod_cache = ensure_secure_dir(target_dir / "swift-modcache")
    cmd = [
        eff_swiftc, "-O", "-parse-as-library", "-module-cache-path", str(mod_cache), str(swift_source),
        "-framework", "AppKit", "-framework", "ApplicationServices", "-o", str(tmp_path),
    ]
    try:
        proc = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, env=dict(src_env), timeout=60, check=False
        )
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        return None, redact_secrets(f"swiftc invocation failed: {exc}", src_env, cfg=cfg)

    if proc.returncode != 0 or not tmp_path.is_file():
        tmp_path.unlink(missing_ok=True)
        err_lines = " ".join((proc.stderr or proc.stdout or "").strip().splitlines())[-280:]
        return None, redact_secrets(f"swiftc compile failed (rc={proc.returncode}): {err_lines or 'no output'}", src_env, cfg=cfg)

    try:
        os.chmod(tmp_path, 0o700)
        os.replace(tmp_path, bin_path)
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        return None, redact_secrets(f"atomic replace of mac_shim failed: {exc}", src_env, cfg=cfg)

    prefix = f"mac_shim-{arch}-"
    for item in target_dir.iterdir():
        if item.name.startswith(prefix) and item.name != bin_name:
            try:
                item.unlink()
            except OSError:
                pass
    return bin_path, ""


def run_macos_key_test(
    cfg: Config, seconds: int = 10, *, env: Mapping[str, str] | None = None, cache_dir: Path | None = None
) -> int:
    bin_path, err = compile_mac_shim(env=env, cache_dir=cache_dir, cfg=cfg)
    if bin_path is None:
        raise RuntimeError(f"macOS key-test unavailable: {err}")
    cmd = [
        str(bin_path), "--mode", "key-test", "--hotkey", cfg.hotkey.key,
        "--trigger-mode", cfg.hotkey.trigger_mode, "--tap-max-ms", str(cfg.hotkey.tap_max_ms),
        "--debounce-ms", str(cfg.hotkey.debounce_ms), "--seconds", str(max(1, seconds)),
    ]
    return stream_key_test_proc(cmd, env=os.environ if env is None else env)


def _normalize_title_for_drift(title: str) -> str:
    return _SPINNER_PREFIX_RE.sub("", title.strip())


def _summarize_osascript_error(stderr: str, *, kind: str) -> str:
    # Redline: NEVER echo raw stderr text (AppleScript errors can echo pasteText user speech!)
    low = stderr.lower()
    if any(tok in low for tok in ("not allowed assistive access", "not allowed to send keystrokes", "-1719", "-25211", "(1002)")):
        return "accessibility_denied (grant Accessibility permission for System Events Cmd+V, or use Ghostty)"
    if "-1743" in low or "not authorized to send apple events" in low:
        target = "Ghostty" if kind == "ghostty" else "System Events"
        return f"automation_denied (grant Automation permission for {target})"
    m = _OSA_CODE_RE.search(stderr)
    return f"{kind}_osascript_error({m.group(1)})" if m else f"{kind}_osascript_failed"


def probe_macos_host(cfg: Config, env: Mapping[str, str] | None = None, *, cache_dir: Path | None = None) -> dict[str, Any]:
    eff_env = dict(os.environ if env is None else env)
    audio_dev = resolve_macos_audio_device(cfg, eff_env)
    info: dict[str, Any] = {
        "avfoundation_ok": False, "audio_devices": [], "configured_audio_device": f":{audio_dev}",
        "pbcopy_ok": bool(shutil.which("pbcopy", path=eff_env.get("PATH"))),
        "tmux_active": bool(eff_env.get("TMUX")),
        "swiftc_ok": False, "swiftc_bin": "", "mac_shim_ok": False, "mac_shim_bin": "", "mac_shim_error": "",
        "fg_process": "", "fg_bundle_id": "", "fg_title": "", "fg_surface_id": "", "fg_tty": "", "fg_cwd": "", "fg_pid": 0,
        "permissions_hint": MACOS_PERMISSIONS_HINT,
    }
    try:
        p = subprocess.run(
            [resolve_ffmpeg_bin(), "-nostdin", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, env=eff_env, timeout=4, check=False,
        )
        combined = f"{p.stdout or ''}\n{p.stderr or ''}"
        info["avfoundation_ok"] = "AVFoundation audio devices" in combined
        info["audio_devices"] = parse_avfoundation_audio_devices(combined)
    except Exception as exc:
        info["av_error"] = redact_secrets(str(exc), eff_env, cfg=cfg)

    if swiftc := resolve_swiftc_bin(eff_env):
        info["swiftc_ok"], info["swiftc_bin"] = True, swiftc
    shim_bin, shim_err = compile_mac_shim(env=eff_env, cache_dir=cache_dir, swiftc_bin=swiftc, cfg=cfg)
    if shim_bin is not None:
        info["mac_shim_bin"] = str(shim_bin)
        try:
            sc = subprocess.run(
                [str(shim_bin), "--self-check"], stdin=subprocess.DEVNULL,
                capture_output=True, text=True, env=eff_env, timeout=5, check=False,
            )
            info["mac_shim_ok"] = sc.returncode == 0
            if sc.returncode != 0:
                raw_err = (sc.stderr or sc.stdout or f"self-check rc={sc.returncode}").strip()
                info["mac_shim_error"] = redact_secrets(raw_err, eff_env, cfg=cfg)
        except Exception as exc:
            info["mac_shim_error"] = redact_secrets(str(exc), eff_env, cfg=cfg)
    else:
        info["mac_shim_error"] = shim_err

    try:
        fg = MacOSPlatform(cfg, env=eff_env, cache_dir=cache_dir).get_foreground_window()
        info.update(
            fg_process=fg.process, fg_bundle_id=fg.bundle_id, fg_title=fg.title,
            fg_surface_id=fg.surface_id, fg_tty=fg.tty, fg_cwd=fg.cwd, fg_pid=fg.pid,
        )
    except Exception as exc:
        info["fg_error"] = redact_secrets(str(exc), eff_env, cfg=cfg)
    return info


class MacOSPlatform(JsonlShimMixin):
    _shim_graceful = True
    _shim_ready_timeout = 5.0
    _shim_label = "mac_shim"

    def __init__(
        self,
        cfg: Config,
        *,
        env: Mapping[str, str] | None = None,
        cache_dir: Path | None = None,
        shim_cmd_factory: Callable[[], list[str] | None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.env = dict(os.environ if env is None else env)
        self.audio_dev = resolve_macos_audio_device(self.cfg, self.env)
        self._cache_dir = cache_dir
        self._shim_cmd_factory = shim_cmd_factory
        self._ff_rec = FfmpegWavRecorder()
        self._recording = False
        self.last_audio_error = ""
        self._init_shim_bridge()

    def _which(self, name: str) -> str | None:
        return shutil.which(name, path=self.env.get("PATH")) if "PATH" in self.env else shutil.which(name)

    def _run_osascript(self, args: list[str], *, timeout: float = 2.5) -> subprocess.CompletedProcess[str] | None:
        if not self._which("osascript"):
            return None
        try:
            return subprocess.run(
                ["osascript", *args], stdin=subprocess.DEVNULL,
                capture_output=True, text=True, env=self.env, timeout=timeout, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    def _build_shim_cmd(self) -> list[str] | None:
        if self._shim_cmd_factory is not None:
            return self._shim_cmd_factory()
        bin_path, err = compile_mac_shim(env=self.env, cache_dir=self._cache_dir, cfg=self.cfg)
        if bin_path is None:
            self.last_shim_error = redact_secrets(err, self.env, cfg=self.cfg)
            return None
        self.last_shim_error = ""
        return [
            str(bin_path), "--mode", "bridge", "--hotkey", self.cfg.hotkey.key,
            "--trigger-mode", self.cfg.hotkey.trigger_mode, "--tap-max-ms", str(self.cfg.hotkey.tap_max_ms),
            "--debounce-ms", str(self.cfg.hotkey.debounce_ms),
        ]

    def _query_ax_title(self) -> str:
        ax = self._run_osascript(["-e", _FRONTMOST_AXTITLE_SCRIPT], timeout=2.0)
        return ax.stdout.strip() if (ax is not None and ax.returncode == 0) else ""

    def _query_frontmost_process_and_title(self) -> tuple[str, str, str, int]:
        if self._has_managed_shim():
            r = self._shim_rpc({"cmd": "fg_window"}, timeout_sec=1.5)
            if r is not None:
                app_name = str(r.get("process") or "").strip()
                bundle_id = str(r.get("bundle_id") or "").strip()
                app_pid = int(r.get("pid") or 0)
                if is_ghostty_app(app_name, bundle_id):
                    return app_name or "Ghostty", bundle_id or GHOSTTY_BUNDLE_ID, "", app_pid
                return app_name, bundle_id, self._query_ax_title(), app_pid

        p = self._run_osascript(["-e", _FRONTMOST_APP_SCRIPT], timeout=2.5)
        if p is None or p.returncode != 0 or _APPLE_SEP not in p.stdout:
            return "", "", "", 0
        parts = p.stdout.rstrip("\r\n").split(_APPLE_SEP)
        try:
            app_pid = int(parts[2].strip()) if len(parts) > 2 and parts[2].strip() else 0
        except ValueError:
            app_pid = 0
        return (
            parts[0].strip() if len(parts) > 0 else "",
            parts[1].strip() if len(parts) > 1 else "",
            parts[3].strip() if len(parts) > 3 else "",
            app_pid,
        )

    def _query_ghostty_terminal(self) -> tuple[str, str, str, str, int] | None:
        p = self._run_osascript(["-e", _GHOSTTY_QUERY_SCRIPT], timeout=2.5)
        if p is None or p.returncode != 0 or not p.stdout.strip():
            return None
        parts = p.stdout.rstrip("\r\n").split(_APPLE_SEP)
        surface_id = parts[0].strip() if len(parts) > 0 else ""
        if not surface_id:
            return None
        try:
            t_pid = int(parts[4].strip()) if len(parts) > 4 and parts[4].strip() else 0
        except ValueError:
            t_pid = 0
        return (
            surface_id,
            parts[1].strip() if len(parts) > 1 else "",
            parts[2].strip() if len(parts) > 2 else "",
            parts[3].strip() if len(parts) > 3 else "",
            t_pid,
        )

    def _append_tmux_pane_title(self, win_title: str, app_name: str) -> str:
        if self.env.get("TMUX") and self._which("tmux") and (not app_name or app_name.lower() in MACOS_TERMINALS):
            try:
                tp = subprocess.run(
                    ["tmux", "display-message", "-p", "#{pane_title}"],
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, env=self.env, timeout=1.5, check=False,
                )
                if tp.returncode == 0 and tp.stdout.strip():
                    return f"{win_title} {tp.stdout.strip()}".strip()
            except (OSError, subprocess.SubprocessError):
                pass
        return win_title

    def get_foreground_window(self, *, hint: ForegroundWindow | None = None) -> ForegroundWindow:
        if hint is not None and (hint.process or hint.bundle_id):
            app_name, bundle_id, win_title, app_pid = hint.process, hint.bundle_id, hint.title, hint.pid
            if not is_ghostty_app(app_name, bundle_id) and not win_title:
                win_title = self._query_ax_title()
        else:
            app_name, bundle_id, win_title, app_pid = self._query_frontmost_process_and_title()

        if is_ghostty_app(app_name, bundle_id):
            eff_name, eff_bundle = app_name or "Ghostty", bundle_id or GHOSTTY_BUNDLE_ID
            if (g_info := self._query_ghostty_terminal()) is not None:
                surface_id, g_title, tty, cwd, t_pid = g_info
                return ForegroundWindow(0, eff_name, g_title, eff_bundle, surface_id, tty, cwd, t_pid)
            if not win_title:
                win_title = self._query_ax_title()
            return ForegroundWindow(0, eff_name, self._append_tmux_pane_title(win_title, eff_name), eff_bundle, pid=app_pid)

        return ForegroundWindow(0, app_name, self._append_tmux_pane_title(win_title, app_name), bundle_id, pid=app_pid)

    def resolve_foreground_window(self, window: ForegroundWindow | None = None) -> ForegroundWindow:
        if window is None:
            return self.get_foreground_window()
        if window.surface_id or window.hwnd or (window.title and not is_ghostty_app(window.process, window.bundle_id)):
            return window
        if window.title and is_ghostty_app(window.process, window.bundle_id) and not self._which("osascript"):
            return window
        if window.title and not window.process and not window.bundle_id:
            return window
        return self.get_foreground_window(hint=window)

    def beep(self, kind: str) -> None:
        snd = f"/System/Library/Sounds/{SOUND_MAP.get(kind, 'Basso.aiff')}"
        if self.cfg.audio.beep and self._which("afplay") and os.path.isfile(snd):
            try:
                subprocess.Popen(["afplay", snd], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                pass

    def start_recording(self, runtime_dir: Path, *, loopback: bool = False) -> None:
        self.last_audio_error = ""
        self._ff_rec.start("avfoundation", f":{self.audio_dev}", runtime_dir, self.env)
        self._recording = True

    def stop_recording(self, *, drain: bool = True) -> tuple[bytes, str]:
        was_rec, self._recording = self._recording, False
        drain_sec = self.cfg.audio.effective_tail_drain_sec(macos=True) if (drain and was_rec) else 0.0
        wav = self._ff_rec.stop(drain_sec=drain_sec)
        self.last_audio_error = "" if wav else self._ff_rec.diagnostic_summary()
        return wav, "avfoundation"

    def cancel_recording(self) -> None:
        self._recording = False
        self._ff_rec.stop(drain_sec=0.0)

    def switch_to_fallback_audio(self) -> bool:
        return False

    def _copy_to_clipboard(self, text: str) -> bool:
        if not self._which("pbcopy"):
            return False
        try:
            p = subprocess.run(
                ["pbcopy"], input=text, text=True, env=self.env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2.0, check=False,
            )
            return p.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _is_focus_drifted(self, origin: ForegroundWindow | None, current: ForegroundWindow) -> bool:
        if origin is None:
            return False
        if origin.bundle_id and current.bundle_id and origin.bundle_id.lower() != current.bundle_id.lower():
            return True
        if origin.process and current.process and origin.process.lower() != current.process.lower():
            return True
        if (origin.process or origin.bundle_id) and not (current.process or current.bundle_id):
            return True
        if origin.hwnd > 0 and current.hwnd > 0 and origin.hwnd != current.hwnd:
            return True
        if (origin.process or origin.bundle_id) and origin.title and current.title:
            if _normalize_title_for_drift(origin.title) != _normalize_title_for_drift(current.title):
                return True
        return False

    def _deliver_to_ghostty_surface(self, text: str, surface_id: str, clip_ok: bool) -> PasteOutcome:
        p = self._run_osascript(["-e", _GHOSTTY_INPUT_SCRIPT, "--", surface_id, text], timeout=2.5)
        if p is None:
            if clip_ok:
                self.beep("focus")
            return PasteOutcome(False, clip_ok, "osascript_unavailable" if clip_ok else "clipboard_failed", "Ghostty")
        if p.returncode != 0:
            if clip_ok:
                self.beep("focus")
            reason = _summarize_osascript_error(p.stderr or "", kind="ghostty") if clip_ok else "clipboard_failed"
            return PasteOutcome(False, clip_ok, reason, "Ghostty")
        verdict = p.stdout.strip()
        if verdict == "ok":
            return PasteOutcome(True, clip_ok, "ok", "Ghostty")
        cur_proc = self._query_frontmost_process_and_title()[0] if verdict == "not_frontmost" else "Ghostty"
        if clip_ok:
            self.beep("focus")
        return PasteOutcome(False, clip_ok, "focus_changed" if clip_ok else "clipboard_failed", cur_proc)

    def deliver_text(
        self, text: str, seq_id: int, target_hwnd: int = 0, *, origin_window: ForegroundWindow | None = None
    ) -> PasteOutcome:
        clip_ok = self._copy_to_clipboard(text)
        if origin_window is not None and origin_window.surface_id:
            return self._deliver_to_ghostty_surface(text, origin_window.surface_id, clip_ok)
        fg = self.get_foreground_window()
        if origin_window is None and fg.surface_id and is_ghostty_app(fg.process, fg.bundle_id):
            return self._deliver_to_ghostty_surface(text, fg.surface_id, clip_ok)
        if self._is_focus_drifted(origin_window, fg) or (fg.process and fg.process.lower() not in MACOS_TERMINALS):
            if clip_ok:
                self.beep("focus")
            return PasteOutcome(False, clip_ok, "focus_changed" if clip_ok else "clipboard_failed", fg.process)
        if self._which("tmux") and self.env.get("TMUX"):
            try:
                lb = subprocess.run(
                    ["tmux", "load-buffer", "-"], input=text, text=True, env=self.env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2.0, check=False,
                )
                pb = subprocess.run(
                    ["tmux", "paste-buffer", "-p"], stdin=subprocess.DEVNULL, env=self.env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2.0, check=False,
                )
                if lb.returncode == 0 and pb.returncode == 0:
                    return PasteOutcome(True, True, "tmux", fg.process)
            except (OSError, subprocess.SubprocessError):
                pass
        if not clip_ok:
            return PasteOutcome(False, False, "clipboard_failed", fg.process)
        p = self._run_osascript(["-e", _CMD_V_SCRIPT], timeout=2.5)
        if p is not None and p.returncode == 0:
            return PasteOutcome(True, True, "ok", fg.process)
        self.beep("focus")
        reason = "osascript_unavailable" if p is None else _summarize_osascript_error(p.stderr or "", kind="system_events")
        return PasteOutcome(False, True, reason, fg.process)
