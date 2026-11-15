"""macOS native bridge: AVFoundation capture, Ghostty split-aware input, and mac_shim.swift hotkey/HUD."""
from __future__ import annotations

import hashlib, json, os, platform as py_platform, re, shutil, subprocess, sys, threading, time
from pathlib import Path
from typing import Any, Callable, Mapping

from ._tmux import (
    TmuxServerSnapshot,
    preferred_tmux_socket_arg,
    probe_tmux_snapshot,
    resolve_tmux_bin,
)
from .audio_proc import pack_pcm16_mono_wav, resolve_ffmpeg_bin
from .config import Config, ensure_secure_dir, expand_user_path, get_runtime_dir, redact_secrets
from .hud import resolve_ui_language
from .platform_wsl import (
    FfmpegWavRecorder,
    ForegroundWindow,
    JsonlShimMixin,
    MicrophoneUnavailableError,
    NoMicrophoneError,
    PasteOutcome,
    invoke_recorder_lost_callback,
    invoke_recorder_recovered_callback,
    stream_key_test_proc,
)
from .session_ctx import (
    SessionTracker,
    _collect_registry_tmux_sockets,
    _tty_matches,
    collect_darwin_window_pty_tree,
    normalize_tty_path,
    parse_window_title_hint,
)

GHOSTTY_BUNDLE_ID = "com.mitchellh.ghostty"
APPLE_TERMINAL_BUNDLE_ID = "com.apple.terminal"
MAC_SHIM_SCRIPT = Path(__file__).resolve().with_name("mac_shim.swift")
SHIM_SCHEMA_VERSION = 2
MACOS_TERMINALS = frozenset(
    {
        "terminal",
        "终端",
        "iterm2",
        "iterm",
        "wezterm",
        "wezterm-gui",
        "alacritty",
        "ghostty",
        "kitty",
        "warp",
        "hyper",
        "rio",
        "tabby",
    }
)
MACOS_TERMINAL_BUNDLE_IDS = frozenset(
    {
        GHOSTTY_BUNDLE_ID,
        "com.apple.terminal",
        "com.googlecode.iterm2",
        "com.github.wez.wezterm",
        "org.alacritty",
        "io.alacritty",
        "net.kovidgoyal.kitty",
        "dev.warp.warp-stable",
        "dev.warp.warp",
        "co.zeit.hyper",
        "com.raphaelamorim.rio",
        "org.tabby",
    }
)
SOUND_MAP = {"start": "Tink.aiff", "stop": "Pop.aiff", "focus": "Glass.aiff", "error": "Basso.aiff"}

_APPLE_SEP = "\x1f"
_SPINNER_PREFIX_RE = re.compile(r"^[\u2800-\u28ff✳✢✶✻✽·•\s]+")
_SPINNER_INLINE_RE = re.compile(r"[\u2800-\u28ff✳✢✶✻✽]+\s*")
_MULTI_WS_RE = re.compile(r"\s+")
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

_TERMINAL_QUERY_SCRIPT = """\
tell application id "com.apple.Terminal"
    if not running or (count of windows) is 0 then return ""
    set w to front window
    try
        if (miniaturized of w) or not (visible of w) then return ""
    end try
    set wid to 0
    set {wname, ttty, tcustom} to {"", "", ""}
    try
        set wid to id of w
    end try
    try
        set wname to name of w
    end try
    try
        set t to selected tab of w
        try
            set ttty to tty of t
        end try
        try
            set tcustom to custom title of t
        end try
    end try
    set sep to character id 31
    return "at1" & sep & (wid as text) & sep & ttty & sep & wname & sep & tcustom
end tell"""

_CMD_V_SCRIPT = 'tell application "System Events" to keystroke "v" using command down'

MACOS_PERMISSIONS_HINT = (
    "Microphone -> terminal/daemon host (ffmpeg capture); "
    "Automation -> Ghostty (split lookup & direct input) or Terminal.app (active tab TTY lookup); "
    "Input Monitoring -> global non-modifier hotkeys / key-test; "
    "Accessibility -> Cmd+V paste for non-Ghostty terminals outside tmux"
)


def is_ghostty_app(app_name: str, bundle_id: str = "") -> bool:
    return bundle_id.strip().lower() == GHOSTTY_BUNDLE_ID or app_name.strip().lower() == "ghostty"


def is_apple_terminal_app(app_name: str, bundle_id: str = "") -> bool:
    bid = (bundle_id or "").strip().lower()
    if bid:
        return bid == APPLE_TERMINAL_BUNDLE_ID
    proc = (app_name or "").strip().lower()
    if proc.endswith(".app"):
        proc = proc[:-4]
    return proc in ("terminal", "终端")


def is_macos_terminal_window(process: str, bundle_id: str = "") -> bool:
    bid = (bundle_id or "").strip().lower()
    if bid and bid in MACOS_TERMINAL_BUNDLE_IDS:
        return True
    proc = (process or "").strip().lower()
    if proc.endswith(".app"):
        proc = proc[:-4]
    return bool(proc) and proc in MACOS_TERMINALS


def resolve_macos_audio_device(cfg: Config, env: Mapping[str, str] | None = None) -> str:
    del env
    raw = (cfg.audio.macos_device or "default").strip()
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


def check_macos_clt_ready(env: Mapping[str, str] | None = None) -> tuple[bool, str]:
    """Verify Xcode Command Line Tools are installed without triggering Apple's GUI stub modal."""
    src_env = os.environ if env is None else env
    if dev_dir := (src_env.get("DEVELOPER_DIR") or "").strip():
        if os.path.isdir(dev_dir):
            return True, ""
        return (
            False,
            f"DEVELOPER_DIR={dev_dir!r} does not exist (unset DEVELOPER_DIR or run 'xcode-select --install')",
        )
    xs_bin = (
        "/usr/bin/xcode-select"
        if (os.path.isfile("/usr/bin/xcode-select") and os.access("/usr/bin/xcode-select", os.X_OK))
        else shutil.which("xcode-select", path=src_env.get("PATH"))
    )
    if not xs_bin:
        return True, ""
    try:
        proc = subprocess.run(
            [xs_bin, "-p"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=dict(src_env),
            timeout=3,
            check=False,
        )
    except Exception as exc:
        return False, f"xcode-select check failed ({exc}); install Xcode Command Line Tools via 'xcode-select --install'"
    dev_path = (proc.stdout or "").strip()
    if proc.returncode != 0 or not dev_path or not os.path.isdir(dev_path):
        return (
            False,
            "Xcode Command Line Tools not installed; run 'xcode-select --install', complete the macOS installer, "
            "then run 'agent-voice-input restart'",
        )
    return True, ""


def compile_mac_shim(
    *,
    env: Mapping[str, str] | None = None,
    cache_dir: Path | None = None,
    swift_source: Path = MAC_SHIM_SCRIPT,
    swiftc_bin: str | None = None,
    cfg: Config | None = None,
    announce: bool = False,
) -> tuple[Path | None, str]:
    src_env = os.environ if env is None else env
    if not swift_source.is_file():
        return None, f"mac_shim.swift not found at {swift_source}"
    try:
        src_bytes = swift_source.read_bytes()
    except OSError as exc:
        return None, redact_secrets(f"cannot read {swift_source}: {exc}", src_env, cfg=cfg)

    arch = (py_platform.machine() or "unknown").lower()
    digest = hashlib.sha256(f"schema={SHIM_SCHEMA_VERSION}\n".encode("utf-8") + src_bytes).hexdigest()[:16]
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

    # On bare macOS, /usr/bin/swiftc is an xcrun stub that pops up a GUI install modal if CLT is missing.
    if eff_swiftc == "/usr/bin/swiftc":
        clt_ok, clt_err = check_macos_clt_ready(src_env)
        if not clt_ok:
            return None, redact_secrets(clt_err, src_env, cfg=cfg)

    tmp_path = target_dir / f".{bin_name}.{os.getpid()}.{threading.get_ident()}.{time.monotonic_ns()}.tmp"
    mod_cache = ensure_secure_dir(target_dir / "swift-modcache")
    cmd = [
        eff_swiftc, "-O", "-parse-as-library", "-module-cache-path", str(mod_cache), str(swift_source),
        "-framework", "AppKit", "-framework", "ApplicationServices", "-framework", "CoreAudio", "-o", str(tmp_path),
    ]
    t0 = time.monotonic()
    if announce:
        print(
            "[agent-voice-input] Compiling native macOS helper (first run, may take 5-15s)...",
            file=sys.stderr,
            flush=True,
        )
    try:
        proc = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, env=dict(src_env), timeout=60, check=False
        )
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        return None, redact_secrets(f"swiftc invocation failed: {exc}", src_env, cfg=cfg)

    if proc.returncode != 0 or not tmp_path.is_file():
        tmp_path.unlink(missing_ok=True)
        raw_out = (proc.stderr or proc.stdout or "").strip()
        err_lines = " ".join(raw_out.splitlines())[-280:]
        low = raw_out.lower()
        hint = ""
        if "license" in low and ("xcodebuild" in low or "agree" in low):
            hint = " — run 'sudo xcodebuild -license accept'"
        elif "xcode-select" in low or "no developer tools" in low or "unable to find utility" in low:
            hint = " — run 'xcode-select --install'"
        return None, redact_secrets(
            f"swiftc compile failed (rc={proc.returncode}): {err_lines or 'no output'}{hint}",
            src_env,
            cfg=cfg,
        )

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
    if announce:
        print(
            f"[agent-voice-input] Native macOS helper ready ({time.monotonic() - t0:.1f}s).",
            file=sys.stderr,
            flush=True,
        )
    return bin_path, ""


def run_macos_key_test(
    cfg: Config, seconds: int = 10, *, env: Mapping[str, str] | None = None, cache_dir: Path | None = None
) -> int:
    bin_path, err = compile_mac_shim(env=env, cache_dir=cache_dir, cfg=cfg, announce=True)
    if bin_path is None:
        raise RuntimeError(f"macOS key-test unavailable: {err}")
    cmd = [
        str(bin_path), "--mode", "key-test", "--hotkey", cfg.hotkey.key,
        "--trigger-mode", cfg.hotkey.trigger_mode, "--tap-max-ms", str(cfg.hotkey.tap_max_ms),
        "--debounce-ms", str(cfg.hotkey.debounce_ms), "--seconds", str(max(1, seconds)),
    ]
    return stream_key_test_proc(cmd, env=os.environ if env is None else env)


def _normalize_title_for_drift(title: str) -> str:
    cleaned = _SPINNER_PREFIX_RE.sub("", title.strip())
    cleaned = _SPINNER_INLINE_RE.sub("", cleaned)
    return _MULTI_WS_RE.sub(" ", cleaned).strip()


def _summarize_osascript_error(stderr: str, *, kind: str) -> str:
    # Redline: NEVER echo raw stderr text (AppleScript errors can echo pasteText user speech!)
    low = stderr.lower()
    if any(tok in low for tok in ("not allowed assistive access", "not allowed to send keystrokes", "-1719", "-25211", "(1002)")):
        return "accessibility_denied (grant Accessibility permission for System Events Cmd+V, or use Ghostty)"
    if "-1743" in low or "not authorized to send apple events" in low:
        target = {"ghostty": "Ghostty", "terminal": "Terminal"}.get(kind, "System Events")
        return f"automation_denied (grant Automation permission for {target})"
    m = _OSA_CODE_RE.search(stderr)
    return f"{kind}_osascript_error({m.group(1)})" if m else f"{kind}_osascript_failed"


def probe_macos_host(
    cfg: Config,
    env: Mapping[str, str] | None = None,
    *,
    cache_dir: Path | None = None,
    announce_compile: bool = False,
) -> dict[str, Any]:
    eff_env = dict(os.environ if env is None else env)
    audio_dev = resolve_macos_audio_device(cfg, eff_env)
    info: dict[str, Any] = {
        "avfoundation_ok": False, "audio_devices": [], "configured_audio_device": f":{audio_dev}",
        "pbcopy_ok": bool(shutil.which("pbcopy", path=eff_env.get("PATH"))),
        "tmux_active": bool(eff_env.get("TMUX")),
        "clt_ok": True, "clt_error": "",
        "swiftc_ok": False, "swiftc_bin": "", "mac_shim_ok": False, "mac_shim_bin": "", "mac_shim_error": "",
        "fg_hwnd": 0, "fg_process": "", "fg_bundle_id": "", "fg_title": "", "fg_surface_id": "", "fg_tty": "", "fg_cwd": "", "fg_pid": 0,
        "permissions_hint": MACOS_PERMISSIONS_HINT,
    }
    try:
        p = subprocess.run(
            [resolve_ffmpeg_bin(), "-nostdin", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, env=eff_env, timeout=4, check=False,
        )
        combined = f"{p.stdout or ''}\n{p.stderr or ''}"
        devs = parse_avfoundation_audio_devices(combined)
        info["audio_devices"] = devs
        info["avfoundation_ok"] = ("AVFoundation audio devices" in combined) and bool(devs)
    except Exception as exc:
        info["av_error"] = redact_secrets(str(exc), eff_env, cfg=cfg)

    swiftc = resolve_swiftc_bin(eff_env)
    if swiftc == "/usr/bin/swiftc":
        clt_ok, clt_err = check_macos_clt_ready(eff_env)
        info["clt_ok"] = clt_ok
        if clt_err:
            info["clt_error"] = redact_secrets(clt_err, eff_env, cfg=cfg)
    else:
        clt_ok = True
    if swiftc:
        info["swiftc_bin"] = swiftc
        info["swiftc_ok"] = bool(clt_ok)
    shim_bin, shim_err = compile_mac_shim(
        env=eff_env, cache_dir=cache_dir, swiftc_bin=swiftc, cfg=cfg, announce=announce_compile
    )
    ui_cult = ""
    if shim_bin is not None:
        info["mac_shim_bin"] = str(shim_bin)
        try:
            sc = subprocess.run(
                [str(shim_bin), "--self-check"], stdin=subprocess.DEVNULL,
                capture_output=True, text=True, env=eff_env, timeout=5, check=False,
            )
            info["mac_shim_ok"] = sc.returncode == 0
            if sc.returncode == 0 and sc.stdout.strip():
                try:
                    sc_json = json.loads(sc.stdout.strip().splitlines()[-1])
                    if isinstance(sc_json, dict):
                        ui_cult = str(sc_json.get("ui_culture") or "").strip()
                except ValueError:
                    pass
            elif sc.returncode != 0:
                raw_err = (sc.stderr or sc.stdout or f"self-check rc={sc.returncode}").strip()
                info["mac_shim_error"] = redact_secrets(raw_err, eff_env, cfg=cfg)
        except Exception as exc:
            info["mac_shim_error"] = redact_secrets(str(exc), eff_env, cfg=cfg)
    else:
        info["mac_shim_error"] = shim_err
    info["ui_culture"] = ui_cult
    info["ui_lang"] = resolve_ui_language(cfg.ui.language, env=eff_env, win_ui_culture=ui_cult or None)

    try:
        fg = MacOSPlatform(cfg, env=eff_env, cache_dir=cache_dir).get_foreground_window()
        info.update(
            fg_hwnd=fg.hwnd, fg_process=fg.process, fg_bundle_id=fg.bundle_id, fg_title=fg.title,
            fg_surface_id=fg.surface_id, fg_tty=fg.tty, fg_cwd=fg.cwd, fg_pid=fg.pid,
        )
    except Exception as exc:
        info["fg_error"] = redact_secrets(str(exc), eff_env, cfg=cfg)
    return info


class _MacRecSession:
    def __init__(
        self,
        gen: int,
        rec: FfmpegWavRecorder,
        runtime_dir: Path,
        *,
        active_uid: str = "",
        active_name: str = "",
        active_name_unique: bool = False,
        daemon_seq_id: int | None = None,
    ) -> None:
        self.gen = gen
        self.rec = rec
        self.runtime_dir = runtime_dir
        self.active_uid = active_uid
        self.active_name = active_name
        self.active_name_unique = active_name_unique
        self.daemon_seq_id = daemon_seq_id
        self.pcm = bytearray()
        self.active: bool = True
        self.aborted: bool = False
        self.busy: bool = False
        self.pending_exit: bool = False
        self.force_lost_reason: str = ""
        self.respawn_used: bool = False
        self.start_ts: float = time.monotonic()


def _is_unique_audio_device_name(st: Mapping[str, Any], uid: str, name: str) -> bool:
    if not uid or not name or ":" in name:
        return False
    raw_uid_names = st.get("uid_names")
    if isinstance(raw_uid_names, Mapping):
        if str(raw_uid_names.get(uid) or "").strip() != name:
            return False
        for other_uid, raw_other in raw_uid_names.items():
            if str(other_uid).strip() == uid:
                continue
            other_name = str(raw_other or "").strip()
            if not other_name or other_name == name or other_name.startswith(name) or name.startswith(other_name):
                return False
        return True
    if "default_name_unique" in st and str(st.get("default_uid") or "").strip() == uid and str(st.get("default_name") or "").strip() == name:
        return bool(st["default_name_unique"])
    raw_alive = st.get("alive_uids")
    alive_cnt = len([u for u in raw_alive if str(u).strip()]) if isinstance(raw_alive, (list, tuple, set, frozenset)) else 0
    try:
        cnt = int(st.get("input_count", alive_cnt))
    except (TypeError, ValueError):
        cnt = alive_cnt
    return (
        cnt == 1
        and str(st.get("default_uid") or "").strip() == uid
        and str(st.get("default_name") or "").strip() == name
    )


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
        tmux_probe_fn: Callable[[], TmuxServerSnapshot] | None = None,
    ) -> None:
        self.cfg = cfg
        self.env = dict(os.environ if env is None else env)
        self.audio_dev = resolve_macos_audio_device(self.cfg, self.env)
        self._cache_dir = cache_dir
        self._shim_cmd_factory = shim_cmd_factory
        self._tmux_probe_fn = tmux_probe_fn
        self._fg_tls = threading.local()
        self._ff_rec = FfmpegWavRecorder()
        self._recording = False
        self._recorder_lost_cb: Callable[..., Any] | None = None
        self._recorder_recovered_cb: Callable[..., Any] | None = None
        self._audio_cache_lock = threading.Lock()
        self._audio_status_known: bool = False
        self._has_audio_input: bool = True
        self._default_input_uid: str = ""
        self._default_input_name: str = ""
        self._default_input_name_unique: bool = False
        self._alive_input_uids: set[str] = set()
        self._rec_cond = threading.Condition()
        self._rec_active: bool = False
        self._rec_gen: int = 0
        self._cur_session: _MacRecSession | None = None
        self._busy_wait_timeout_sec: float = 4.0
        self.last_audio_error = ""
        self._init_shim_bridge()

    def _wait_session_idle_locked(self, session: _MacRecSession, timeout_sec: float) -> bool:
        deadline = time.monotonic() + timeout_sec
        while session.busy and (rem := deadline - time.monotonic()) > 0.0:
            self._rec_cond.wait(timeout=min(0.05, rem))
        return not session.busy

    def _detach_busy_recorder_locked(self, session: _MacRecSession) -> None:
        if self._ff_rec is session.rec:
            self._ff_rec = FfmpegWavRecorder(cmd_factory=getattr(session.rec, "_cmd_factory", None))

    @property
    def _active_recording_uid(self) -> str:
        with self._rec_cond:
            s = self._cur_session
            return s.active_uid if (s is not None and s.active and not s.aborted) else ""

    def set_recorder_lost_callback(self, cb: Callable[..., Any] | None) -> None:
        self._recorder_lost_cb = cb

    def set_recorder_recovered_callback(self, cb: Callable[..., Any] | None) -> None:
        self._recorder_recovered_cb = cb

    def _update_audio_status_cache(self, st: Mapping[str, Any]) -> tuple[bool, str, set[str]]:
        raw_alive = st.get("alive_uids")
        alive_uids = (
            {str(u).strip() for u in raw_alive if str(u).strip()}
            if isinstance(raw_alive, (list, tuple, set, frozenset))
            else set()
        )
        has_input = bool(st["has_input"]) if "has_input" in st else bool(alive_uids)
        default_uid = str(st.get("default_uid") or "").strip()
        default_name = str(st.get("default_name") or "").strip()
        default_unique = _is_unique_audio_device_name(st, default_uid, default_name)
        with self._audio_cache_lock:
            self._audio_status_known = True
            self._has_audio_input = has_input
            self._default_input_uid = default_uid
            self._default_input_name = default_name
            self._default_input_name_unique = default_unique
            self._alive_input_uids = alive_uids
        return has_input, default_uid, alive_uids

    def _query_live_audio_status(self, timeout_sec: float = 0.8) -> dict[str, Any] | None:
        if not self._has_managed_shim():
            return None
        try:
            res = self._shim_rpc({"cmd": "audio_status"}, timeout_sec=timeout_sec, allow_retry=False)
        except TypeError:
            res = self._shim_rpc({"cmd": "audio_status"}, timeout_sec=timeout_sec)
        except Exception:
            return None
        if not isinstance(res, dict):
            return None
        st = (
            res.get("audio_status")
            if isinstance(res.get("audio_status"), dict)
            else (res if ("has_input" in res or "alive_uids" in res) else None)
        )
        if not isinstance(st, dict):
            return None
        has_input, default_uid, alive_uids = self._update_audio_status_cache(st)
        default_name = str(st.get("default_name") or "").strip()
        return {
            **st,
            "has_input": has_input,
            "default_uid": default_uid,
            "default_name": default_name,
            "default_name_unique": _is_unique_audio_device_name(st, default_uid, default_name),
            "alive_uids": sorted(alive_uids),
        }

    def _on_shim_internal_event(self, msg: dict[str, Any]) -> None:
        if str(msg.get("event") or "") != "audio_devices_changed":
            return
        has_input, _default_uid, alive_uids = self._update_audio_status_cache(msg)
        claimed_session: _MacRecSession | None = None
        owns_busy = False
        with self._rec_cond:
            session = self._cur_session
            if (
                session is not None
                and session.active
                and not session.aborted
                and (not has_input or (session.active_uid and session.active_uid not in alive_uids))
            ):
                if session.busy:
                    session.force_lost_reason = "coreaudio_device_disconnected"
                    self._rec_cond.notify_all()
                    return
                session.active = False
                self._rec_active = False
                self._recording = False
                session.busy = True
                owns_busy = True
                claimed_session = session
        if claimed_session is None:
            return
        try:
            self._stop_level_streamer()
            seg_wav = claimed_session.rec.stop(drain_sec=0.0)
            with self._rec_cond:
                if not claimed_session.aborted and len(seg_wav) > 44:
                    claimed_session.pcm.extend(seg_wav[44:])
        finally:
            if owns_busy:
                with self._rec_cond:
                    claimed_session.busy = False
                    owns_busy = False
                    self._rec_cond.notify_all()
        with self._rec_cond:
            deliver_lost = not claimed_session.aborted and self._cur_session is claimed_session
        if not deliver_lost:
            return
        self.last_audio_error = "coreaudio_device_disconnected"
        if (lost_cb := self._recorder_lost_cb) is not None:
            if not self._ready.is_set():
                def _deliver_async_lost(s: _MacRecSession = claimed_session) -> None:
                    with self._rec_cond:
                        if s.aborted or self._cur_session is not s:
                            return
                    invoke_recorder_lost_callback(self._recorder_lost_cb, s.daemon_seq_id)

                threading.Thread(target=_deliver_async_lost, daemon=True).start()
            else:
                invoke_recorder_lost_callback(lost_cb, claimed_session.daemon_seq_id)

    def _which(self, name: str) -> str | None:
        return shutil.which(name, path=self.env.get("PATH")) if "PATH" in self.env else shutil.which(name)

    def _resolve_tmux_exe(self) -> str:
        if found := self._which("tmux"):
            return found if os.path.sep in found and Path(found).name == "tmux" else "tmux"
        return resolve_tmux_bin(self.env) if "PATH" in self.env else ""

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
        bin_path, err = compile_mac_shim(env=self.env, cache_dir=self._cache_dir, cfg=self.cfg, announce=True)
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
        self._fg_tls.hwnd = 0
        self._fg_tls.from_shim = False
        self._fg_tls.ax_trusted = None
        if self._has_managed_shim():
            r = self._shim_rpc({"cmd": "fg_window"}, timeout_sec=1.5)
            if r is not None:
                app_name = str(r.get("process") or "").strip()
                bundle_id = str(r.get("bundle_id") or "").strip()
                app_pid = int(r.get("pid") or 0)
                self._fg_tls.hwnd = int(r.get("hwnd") or 0)
                self._fg_tls.from_shim = True
                raw_ax = r.get("ax_trusted")
                self._fg_tls.ax_trusted = raw_ax if isinstance(raw_ax, bool) else None
                shim_title = str(r.get("title") or "").strip()
                if is_ghostty_app(app_name, bundle_id):
                    return app_name or "Ghostty", bundle_id or GHOSTTY_BUNDLE_ID, shim_title, app_pid
                if is_apple_terminal_app(app_name, bundle_id):
                    return app_name or "Terminal", bundle_id or APPLE_TERMINAL_BUNDLE_ID, shim_title, app_pid
                return app_name, bundle_id, shim_title, app_pid

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

    def _query_apple_terminal(self) -> tuple[int, str, str, str] | None:
        p = self._run_osascript(["-e", _TERMINAL_QUERY_SCRIPT], timeout=2.5)
        if p is None or p.returncode != 0 or not p.stdout.strip():
            return None
        parts = p.stdout.rstrip("\r\n").split(_APPLE_SEP)
        if len(parts) < 4 or parts[0] != "at1":
            return None
        try:
            win_id = max(0, int(parts[1].strip())) if parts[1].strip() else 0
        except ValueError:
            win_id = 0
        tab_tty = normalize_tty_path(parts[2].strip())
        if not tab_tty and win_id <= 0:
            return None
        wname = parts[3].strip()
        tcustom = parts[4].strip() if len(parts) > 4 else ""
        eff_title = f"{wname} {tcustom}".strip() if (tcustom and tcustom not in wname) else (wname or tcustom)
        return win_id, eff_title, tab_tty, wname

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

    def _append_tmux_pane_title(self, win_title: str, app_name: str, bundle_id: str = "") -> str:
        tmux_exe = self._resolve_tmux_exe() if self.env.get("TMUX") else ""
        if tmux_exe and (
            not (app_name or bundle_id) or is_macos_terminal_window(app_name, bundle_id)
        ):
            tmux_env = {k: v for k, v in self.env.items() if k != "TMUX_PANE"}
            try:
                tp = subprocess.run(
                    [tmux_exe, "display-message", "-p", "#{pane_title}"],
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, env=tmux_env, timeout=1.5, check=False,
                )
                if tp.returncode == 0 and tp.stdout.strip():
                    return f"{win_title} {tp.stdout.strip()}".strip()
            except (OSError, subprocess.SubprocessError):
                pass
        return win_title

    def is_allowed_terminal(self, window: ForegroundWindow) -> bool:
        return is_macos_terminal_window(window.process, window.bundle_id)

    def get_foreground_window(self, *, hint: ForegroundWindow | None = None) -> ForegroundWindow:
        if hint is not None and (hint.process or hint.bundle_id):
            app_name, bundle_id, win_title, app_pid, app_hwnd = (
                hint.process,
                hint.bundle_id,
                hint.title,
                hint.pid,
                hint.hwnd,
            )
            from_shim = hint.ax_trusted is not None
            ax_trusted = hint.ax_trusted
            if (
                not is_ghostty_app(app_name, bundle_id)
                and not is_apple_terminal_app(app_name, bundle_id)
                and not win_title
                and not from_shim
                and ax_trusted is not False
            ):
                win_title = self._query_ax_title()
        else:
            self._fg_tls.hwnd = 0
            self._fg_tls.from_shim = False
            self._fg_tls.ax_trusted = None
            app_name, bundle_id, win_title, app_pid = self._query_frontmost_process_and_title()
            app_hwnd = int(getattr(self._fg_tls, "hwnd", 0) or 0)
            from_shim = bool(getattr(self._fg_tls, "from_shim", False))
            ax_trusted = getattr(self._fg_tls, "ax_trusted", None)

        if is_ghostty_app(app_name, bundle_id):
            eff_name, eff_bundle = app_name or "Ghostty", bundle_id or GHOSTTY_BUNDLE_ID
            if not (from_shim and app_hwnd <= 0):
                if (g_info := self._query_ghostty_terminal()) is not None:
                    surface_id, g_title, tty, cwd, t_pid = g_info
                    return ForegroundWindow(
                        app_hwnd, eff_name, g_title, eff_bundle, surface_id, tty, cwd, t_pid, ax_trusted=ax_trusted
                    )
            if not win_title and not from_shim and ax_trusted is not False:
                win_title = self._query_ax_title()
            return ForegroundWindow(
                app_hwnd,
                eff_name,
                self._append_tmux_pane_title(win_title, eff_name, eff_bundle),
                eff_bundle,
                pid=app_pid,
                ax_trusted=ax_trusted,
            )

        if is_apple_terminal_app(app_name, bundle_id):
            eff_name = app_name or "Terminal"
            eff_bundle = bundle_id or APPLE_TERMINAL_BUNDLE_ID
            if not (from_shim and app_hwnd <= 0):
                if (t_info := self._query_apple_terminal()) is not None:
                    t_wid, t_title, t_tty, t_wname = t_info
                    is_other_panel = bool(
                        win_title
                        and t_wname
                        and _normalize_title_for_drift(win_title) != _normalize_title_for_drift(t_wname)
                        and (app_hwnd <= 0 or t_wid <= 0 or app_hwnd != t_wid)
                    )
                    if not is_other_panel:
                        eff_hwnd = t_wid if t_wid > 0 else app_hwnd
                        merged_title = t_title or win_title
                        return ForegroundWindow(
                            eff_hwnd,
                            eff_name,
                            self._append_tmux_pane_title(merged_title, eff_name, eff_bundle),
                            eff_bundle,
                            tty=t_tty,
                            pid=app_pid,
                            ax_trusted=ax_trusted,
                        )
            if not win_title and not from_shim and ax_trusted is not False:
                win_title = self._query_ax_title()
            return ForegroundWindow(
                app_hwnd,
                eff_name,
                self._append_tmux_pane_title(win_title, eff_name, eff_bundle),
                eff_bundle,
                pid=app_pid,
                ax_trusted=ax_trusted,
            )

        return ForegroundWindow(
            app_hwnd,
            app_name,
            self._append_tmux_pane_title(win_title, app_name, bundle_id),
            bundle_id,
            pid=app_pid,
            ax_trusted=ax_trusted,
        )

    def resolve_foreground_window(self, window: ForegroundWindow | None = None) -> ForegroundWindow:
        if window is None:
            return self.get_foreground_window()
        if is_ghostty_app(window.process, window.bundle_id):
            if window.surface_id or (window.title and not self._which("osascript")):
                return window
            return self.get_foreground_window(hint=window)
        if is_apple_terminal_app(window.process, window.bundle_id):
            if window.tty or (window.title and not self._which("osascript")):
                return window
            return self.get_foreground_window(hint=window)
        if (
            window.surface_id
            or window.title
            or (window.ax_trusted is not None and not self.env.get("TMUX"))
            or (window.hwnd and not self._which("osascript"))
        ):
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

    def start_recording(
        self,
        runtime_dir: Path,
        *,
        loopback: bool = False,
        seq_id: int | None = None,
    ) -> None:
        self._stop_level_streamer()
        old_session: _MacRecSession | None = None
        old_idle = True
        with self._rec_cond:
            self._rec_gen += 1
            self._rec_active = False
            self._recording = False
            old_session = self._cur_session
            self._cur_session = None
            if old_session is not None:
                old_session.active = False
                old_session.aborted = True
                self._rec_cond.notify_all()
                old_idle = self._wait_session_idle_locked(old_session, self._busy_wait_timeout_sec)
                if not old_idle:
                    self._detach_busy_recorder_locked(old_session)
        if old_session is not None and old_idle:
            old_session.rec.stop(drain_sec=0.0)

        self.last_audio_error = ""
        has_known_input: bool | None = None
        locked_uid = ""
        locked_name = ""
        locked_name_unique = False
        is_default_dev = self.audio_dev.lstrip(":").strip().lower() in ("", "default")
        if self._has_managed_shim() or (
            self._audio_status_known and not self._shim_disabled and not self._closed
        ):
            live_st = self._query_live_audio_status(timeout_sec=0.8)
            if live_st is not None:
                known = True
                has_in = bool(live_st.get("has_input", True))
                def_uid = str(live_st.get("default_uid") or "").strip()
                def_name = str(live_st.get("default_name") or "").strip()
                def_uniq = bool(live_st.get("default_name_unique", False))
            else:
                with self._audio_cache_lock:
                    known = self._audio_status_known
                    has_in = self._has_audio_input
                    def_uid = self._default_input_uid
                    def_name = self._default_input_name
                    def_uniq = self._default_input_name_unique
            if known:
                if not has_in:
                    self.last_audio_error = "coreaudio_no_input_device"
                    raise NoMicrophoneError("no microphone input device available on macOS")
                has_known_input = True
                if is_default_dev:
                    locked_uid = def_uid
                    locked_name = def_name
                    locked_name_unique = def_uniq

        with self._rec_cond:
            self._rec_gen += 1
            session = _MacRecSession(
                self._rec_gen,
                self._ff_rec,
                runtime_dir,
                active_uid=locked_uid,
                active_name=locked_name,
                active_name_unique=locked_name_unique,
                daemon_seq_id=seq_id,
            )
            self._cur_session = session
            self._rec_active = True
            self._recording = True

        try:
            session.rec.start(
                "avfoundation",
                f":{self.audio_dev}",
                runtime_dir,
                self.env,
                verify_startup=True,
                startup_timeout_sec=0.85,
                stall_timeout_sec=1.2,
                has_known_input=has_known_input,
                on_unexpected_exit=lambda s=session: self._on_ffmpeg_unexpected_exit(s),
                on_level=self._on_recorder_level,
            )
        except Exception:
            with self._rec_cond:
                session.active = False
                session.aborted = True
                if self._cur_session is session:
                    self._cur_session = None
                    self._rec_active = False
                    self._recording = False
            self.last_audio_error = session.rec.diagnostic_summary()
            raise

        cleanup_early_stop = False
        owns_early_busy = False
        with self._rec_cond:
            still_active = session.active and not session.aborted and self._cur_session is session
            if not still_active and not session.busy:
                session.busy = True
                owns_early_busy = True
                cleanup_early_stop = True
        if still_active:
            self._start_level_streamer(lambda s=session: s.rec.current_level)
        elif cleanup_early_stop:
            try:
                seg_wav = session.rec.stop(drain_sec=0.0)
                with self._rec_cond:
                    if not session.aborted and len(seg_wav) > 44:
                        session.pcm.extend(seg_wav[44:])
            finally:
                if owns_early_busy:
                    with self._rec_cond:
                        session.busy = False
                        owns_early_busy = False
                        self._rec_cond.notify_all()

    def _on_ffmpeg_unexpected_exit(self, session: _MacRecSession) -> None:
        owns_busy = False
        with self._rec_cond:
            if session.aborted or not session.active:
                return
            if session.busy:
                session.pending_exit = True
                self._rec_cond.notify_all()
                return
            session.pending_exit = False
            session.busy = True
            owns_busy = True

        recovered_gap_ms: int | None = None
        should_notify_lost = False
        diag = "ffmpeg_unexpected_exit"
        try:
            last_audio_ts = session.rec.last_chunk_ts or session.start_ts or time.monotonic()
            self._stop_level_streamer()
            seg_wav = session.rec.stop(drain_sec=0.0)
            diag = session.rec.diagnostic_summary() or "ffmpeg_unexpected_exit"
            with self._rec_cond:
                if not session.aborted and len(seg_wav) > 44:
                    session.pcm.extend(seg_wav[44:])
                has_captured_pcm = not session.aborted and len(session.pcm) >= 2
                still_active = session.active and not session.aborted
                forced_reason = session.force_lost_reason

            if not still_active:
                return
            if forced_reason:
                diag = forced_reason
            else:
                can_respawn = False
                if (
                    not session.respawn_used
                    and session.active_uid
                    and session.active_name
                    and session.active_name_unique
                    and has_captured_pcm
                ):
                    live_st = self._query_live_audio_status(timeout_sec=0.5)
                    if live_st is not None:
                        raw_alive = live_st.get("alive_uids") or []
                        alive_set = {str(u).strip() for u in raw_alive if str(u).strip()}
                        cur_def_uid = str(live_st.get("default_uid") or "").strip()
                        if (
                            bool(live_st.get("has_input", bool(alive_set)))
                            and session.active_uid in alive_set
                            and cur_def_uid == session.active_uid
                            and _is_unique_audio_device_name(live_st, session.active_uid, session.active_name)
                        ):
                            can_respawn = True

                if can_respawn:
                    with self._rec_cond:
                        if not session.active or session.aborted or session.force_lost_reason:
                            can_respawn = False
                            if session.force_lost_reason:
                                diag = session.force_lost_reason
                        else:
                            session.respawn_used = True
                            session.pending_exit = False
                    if can_respawn:
                        try:
                            session.rec.start(
                                "avfoundation",
                                f":{session.active_name}",
                                session.runtime_dir,
                                self.env,
                                verify_startup=True,
                                startup_timeout_sec=0.85,
                                stall_timeout_sec=1.2,
                                has_known_input=True,
                                on_unexpected_exit=lambda s=session: self._on_ffmpeg_unexpected_exit(s),
                                on_level=self._on_recorder_level,
                            )
                        except Exception:
                            diag = session.rec.diagnostic_summary() or diag
                        else:
                            with self._rec_cond:
                                self._rec_cond.wait_for(
                                    lambda: (
                                        not session.active
                                        or session.aborted
                                        or session.pending_exit
                                        or bool(session.force_lost_reason)
                                    ),
                                    timeout=0.08,
                                )
                            post_st = self._query_live_audio_status(timeout_sec=0.5)
                            post_alive = (
                                {str(u).strip() for u in (post_st.get("alive_uids") or []) if str(u).strip()}
                                if isinstance(post_st, dict)
                                else set()
                            )
                            drifted = not (
                                isinstance(post_st, dict)
                                and bool(post_st.get("has_input", bool(post_alive)))
                                and session.active_uid in post_alive
                                and str(post_st.get("default_uid") or "").strip() == session.active_uid
                                and _is_unique_audio_device_name(post_st, session.active_uid, session.active_name)
                            )
                            child_dead = not session.rec.is_running
                            resume_ts = session.rec.first_chunk_ts or session.rec.last_chunk_ts or time.monotonic()
                            raw_gap_ms = max(0, int(round((resume_ts - last_audio_ts) * 1000.0)))
                            pad_bytes = (min(30000, raw_gap_ms) * 32) & ~1

                            aborted_mid = False
                            forced_after = ""
                            with self._rec_cond:
                                aborted_mid = not session.active or session.aborted
                                forced_after = session.force_lost_reason
                                immediate_eof = session.pending_exit or child_dead
                                if not aborted_mid and not forced_after and not immediate_eof and not drifted:
                                    if pad_bytes > 0:
                                        session.pcm.extend(b"\x00" * pad_bytes)
                                    recovered_gap_ms = raw_gap_ms
                                    session.busy = False
                                    owns_busy = False
                                    self._rec_cond.notify_all()

                            if recovered_gap_ms is None:
                                seg_extra = session.rec.stop(drain_sec=0.0)
                                if not session.aborted and not drifted and len(seg_extra) > 44:
                                    with self._rec_cond:
                                        if not session.aborted:
                                            if pad_bytes > 0:
                                                session.pcm.extend(b"\x00" * pad_bytes)
                                            session.pcm.extend(seg_extra[44:])
                                if aborted_mid:
                                    return
                                if forced_after:
                                    diag = forced_after
                                elif drifted:
                                    diag = "coreaudio_default_input_switched"
                                else:
                                    diag = session.rec.diagnostic_summary() or diag

            if recovered_gap_ms is None:
                with self._rec_cond:
                    if session.active and not session.aborted:
                        session.active = False
                        if self._cur_session is session:
                            self._rec_active = False
                            self._recording = False
                        should_notify_lost = True
                    session.pending_exit = False
                    session.busy = False
                    owns_busy = False
                    self._rec_cond.notify_all()
        finally:
            if owns_busy:
                with self._rec_cond:
                    session.pending_exit = False
                    session.busy = False
                    owns_busy = False
                    self._rec_cond.notify_all()

        if recovered_gap_ms is not None:
            with self._rec_cond:
                still_cur = session.active and not session.aborted and self._cur_session is session
            if not still_cur:
                return
            self._start_level_streamer(lambda s=session: s.rec.current_level)
            if (rec_cb := self._recorder_recovered_cb) is not None:
                try:
                    invoke_recorder_recovered_callback(rec_cb, recovered_gap_ms, session.daemon_seq_id)
                except Exception:
                    pass
            return

        if should_notify_lost:
            with self._rec_cond:
                still_cur_lost = not session.aborted and self._cur_session is session
            if not still_cur_lost:
                return
            self.last_audio_error = diag
            if (lost_cb := self._recorder_lost_cb) is not None:
                invoke_recorder_lost_callback(lost_cb, session.daemon_seq_id)

    def stop_recording(self, *, drain: bool = True) -> tuple[bytes, str]:
        self._stop_level_streamer()
        with self._rec_cond:
            session = self._cur_session
            was_rec = (session is not None and session.active) or self._rec_active or self._recording
            self._cur_session = None
            self._rec_active = False
            self._recording = False
            idle = True
            if session is not None:
                session.active = False
                self._rec_cond.notify_all()
                idle = self._wait_session_idle_locked(session, self._busy_wait_timeout_sec)
                if not idle:
                    session.aborted = True
                    self._detach_busy_recorder_locked(session)
                    self._rec_cond.notify_all()

        if session is None:
            drain_sec = self.cfg.audio.effective_tail_drain_sec(macos=True) if (drain and was_rec) else 0.0
            wav = self._ff_rec.stop(drain_sec=drain_sec)
            if not self.last_audio_error:
                self.last_audio_error = "" if wav else self._ff_rec.diagnostic_summary()
            return wav, "avfoundation"

        if not idle:
            if not self.last_audio_error:
                self.last_audio_error = "recorder_busy_timeout"
            return b"", "avfoundation"

        drain_sec = self.cfg.audio.effective_tail_drain_sec(macos=True) if (drain and was_rec) else 0.0
        seg_wav = session.rec.stop(drain_sec=drain_sec)
        if session.pcm:
            pcm = bytearray(session.pcm)
            session.pcm.clear()
            if len(seg_wav) > 44:
                pcm.extend(seg_wav[44:])
            wav = pack_pcm16_mono_wav(bytes(pcm)) if pcm else b""
        else:
            wav = seg_wav
        if not self.last_audio_error:
            self.last_audio_error = "" if wav else session.rec.diagnostic_summary()
        return wav, "avfoundation"

    def cancel_recording(self) -> None:
        self._stop_level_streamer()
        with self._rec_cond:
            self._rec_gen += 1
            session = self._cur_session
            self._cur_session = None
            self._rec_active = False
            self._recording = False
            idle = True
            if session is not None:
                session.active = False
                session.aborted = True
                self._rec_cond.notify_all()
                idle = self._wait_session_idle_locked(session, 1.5)
                if not idle:
                    self._detach_busy_recorder_locked(session)
        if session is None:
            self._ff_rec.stop(drain_sec=0.0)
        elif idle:
            session.rec.stop(drain_sec=0.0)
            session.pcm.clear()

    def switch_to_fallback_audio(self) -> bool:
        return False

    def _copy_to_clipboard(self, text: str) -> bool:
        if not self._which("pbcopy"):
            return False
        try:
            p = subprocess.run(
                ["pbcopy"], input=text, text=True, encoding="utf-8", env=self.env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2.0, check=False,
            )
            return p.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _is_focus_drifted(self, origin: ForegroundWindow | None, current: ForegroundWindow) -> bool:
        if origin is None:
            return False
        if origin.bundle_id and current.bundle_id:
            if origin.bundle_id.lower() != current.bundle_id.lower():
                return True
        elif origin.process and current.process and origin.process.lower() != current.process.lower():
            return True
        if (origin.process or origin.bundle_id) and not (current.process or current.bundle_id):
            return True
        if origin.hwnd > 0 and current.hwnd > 0 and origin.hwnd != current.hwnd:
            return True
        orig_tty = normalize_tty_path(origin.tty)
        cur_tty = normalize_tty_path(current.tty)
        if orig_tty and not cur_tty:
            return True
        if origin.hwnd > 0 and current.hwnd <= 0 and not cur_tty and (not current.title or current.ax_trusted is not None):
            return True
        if orig_tty and cur_tty:
            if orig_tty != cur_tty:
                return True
            o_hint = parse_window_title_hint(origin.title)
            c_hint = parse_window_title_hint(current.title)
            if o_hint is not None and c_hint is not None and o_hint.session_token != c_hint.session_token:
                return True
            return False
        if (origin.process or origin.bundle_id) and origin.title and not current.title and current.hwnd <= 0 and not cur_tty:
            return True
        if (origin.process or origin.bundle_id) and origin.title and current.title:
            if _normalize_title_for_drift(origin.title) != _normalize_title_for_drift(current.title):
                return True
        return False

    def _deliver_via_tmux_buffer(
        self,
        tmux_cmd_prefix: list[str],
        text: str,
        *,
        seq_id: int,
        target_pane: str = "",
        tmux_env: Mapping[str, str],
    ) -> bool:
        buf_name = f"avi-{os.getpid()}-{seq_id}-{time.monotonic_ns() & 0xFFFF:04x}"
        env_dict = dict(tmux_env)
        loaded = False
        pasted = False
        try:
            lb = subprocess.run(
                [*tmux_cmd_prefix, "load-buffer", "-b", buf_name, "-"],
                input=text,
                text=True,
                encoding="utf-8",
                env=env_dict,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2.0,
                check=False,
            )
            if lb.returncode != 0:
                return False
            loaded = True
            pb_cmd = [*tmux_cmd_prefix, "paste-buffer", "-d", "-p", "-b", buf_name]
            if target_pane:
                pb_cmd.extend(["-t", target_pane])
            pb = subprocess.run(
                pb_cmd,
                stdin=subprocess.DEVNULL,
                env=env_dict,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2.0,
                check=False,
            )
            if pb.returncode == 0:
                pasted = True
                return True
        except (OSError, subprocess.SubprocessError):
            pass
        finally:
            if loaded and not pasted:
                try:
                    subprocess.run(
                        [*tmux_cmd_prefix, "delete-buffer", "-b", buf_name],
                        stdin=subprocess.DEVNULL,
                        env=env_dict,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=1.0,
                        check=False,
                    )
                except (OSError, subprocess.SubprocessError):
                    pass
        return False

    def _probe_tmux_for_paste(self) -> TmuxServerSnapshot:
        if self._tmux_probe_fn is not None:
            try:
                return self._tmux_probe_fn()
            except Exception:
                return TmuxServerSnapshot()
        try:
            rt_dir = get_runtime_dir(self.env, create=False)
            extra_socks = _collect_registry_tmux_sockets([rt_dir])
        except Exception:
            extra_socks = []
        if (
            self._cache_dir is not None
            and not extra_socks
            and not any(self.env.get(k) for k in ("TMUX", "AGENT_VOICE_INPUT_TMUX_HINT", "TMUX_TMPDIR"))
        ):
            return TmuxServerSnapshot()
        return probe_tmux_snapshot(env=self.env, extra_sockets=extra_socks)

    def _try_deliver_attached_tmux(
        self,
        text: str,
        *,
        seq_id: int,
        eff_tty: str,
        fg: ForegroundWindow,
        allow_pid_single_tty: bool = False,
    ) -> bool:
        tmux_bin = self._resolve_tmux_exe()
        if not tmux_bin:
            return False
        snap = self._probe_tmux_for_paste()
        if not snap.clients or not snap.panes:
            return False
        target_tty = normalize_tty_path(eff_tty)
        if not target_tty and allow_pid_single_tty and fg.pid > 1:
            rows = SessionTracker(env=self.env)._list_darwin_user_ps_rows()
            if rows:
                tab_ttys, desc_pids = collect_darwin_window_pty_tree(rows, fg.pid)
                if len(tab_ttys) == 1:
                    target_tty = next(iter(tab_ttys))
                elif not tab_ttys and desc_pids:
                    desc_clients = [
                        c for c in snap.clients if c.client_pid > 1 and c.client_pid in desc_pids and c.client_tty
                    ]
                    if len(desc_clients) == 1:
                        target_tty = normalize_tty_path(desc_clients[0].client_tty)
        if not target_tty:
            return False
        selected = SessionTracker._select_active_tmux_target(
            snap,
            fg.title,
            window_tty=target_tty,
            window_pid=fg.pid,
            allowed_client_ttys=(target_tty,),
        )
        if selected is None:
            return False
        client, pane = selected
        if not _tty_matches(client.client_tty, target_tty) or not pane.pane_id:
            return False
        sock_path = pane.socket_path or client.socket_path
        cmd_prefix = [tmux_bin]
        if sock_path:
            cmd_prefix.extend(["-S", preferred_tmux_socket_arg(sock_path)])
        tmux_env = {k: v for k, v in self.env.items() if k not in ("TMUX", "TMUX_PANE")}
        return self._deliver_via_tmux_buffer(
            cmd_prefix,
            text,
            seq_id=seq_id,
            target_pane=pane.pane_id,
            tmux_env=tmux_env,
        )

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
        if self._is_focus_drifted(origin_window, fg) or ((fg.process or fg.bundle_id) and not self.is_allowed_terminal(fg)):
            if clip_ok:
                self.beep("focus")
            return PasteOutcome(False, clip_ok, "focus_changed" if clip_ok else "clipboard_failed", fg.process)
        if self.env.get("TMUX") and (tmux_exe := self._resolve_tmux_exe()):
            tmux_env = {k: v for k, v in self.env.items() if k != "TMUX_PANE"}
            if self._deliver_via_tmux_buffer([tmux_exe], text, seq_id=seq_id, tmux_env=tmux_env):
                return PasteOutcome(True, clip_ok, "tmux", fg.process)
        eff_tty = normalize_tty_path(fg.tty)
        if (eff_tty or fg.pid > 1) and self._try_deliver_attached_tmux(
            text, seq_id=seq_id, eff_tty=eff_tty, fg=fg, allow_pid_single_tty=not bool(eff_tty)
        ):
            return PasteOutcome(True, clip_ok, "tmux", fg.process)
        if not clip_ok:
            return PasteOutcome(False, False, "clipboard_failed", fg.process)
        p = self._run_osascript(["-e", _CMD_V_SCRIPT], timeout=2.5)
        if p is not None and p.returncode == 0:
            return PasteOutcome(True, True, "ok", fg.process)
        self.beep("focus")
        reason = "osascript_unavailable" if p is None else _summarize_osascript_error(p.stderr or "", kind="system_events")
        return PasteOutcome(False, True, reason, fg.process)
