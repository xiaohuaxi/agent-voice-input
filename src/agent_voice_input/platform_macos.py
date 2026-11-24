"""macOS native bridge: AVFoundation capture, Ghostty split-aware input, and mac_shim.swift hotkey/HUD."""
from __future__ import annotations

from dataclasses import replace
import hashlib, json, os, platform as py_platform, re, shutil, subprocess, sys, threading, time
from pathlib import Path
from typing import Any, Callable, Mapping

from ._tmux import (
    TmuxServerSnapshot,
    _same_tmux_server,
    parse_tmux_socket_from_env,
    preferred_tmux_socket_arg,
    probe_tmux_snapshot,
    resolve_tmux_bin,
)
from .http_transport import sleep_cooperative
from .app_scope import (
    APPLE_TERMINAL_BUNDLE_ID,
    GHOSTTY_BUNDLE_ID,
    ITERM2_BUNDLE_ID,
    MACOS_TERMINAL_BUNDLE_IDS,
    MACOS_TERMINALS,
)
from .audio_proc import pack_pcm16_mono_wav, resolve_ffmpeg_bin
from .config import (
    Config,
    ConfigError,
    ensure_secure_dir,
    expand_user_path,
    get_runtime_dir,
    is_hermetic_env,
    redact_secrets,
    validate_hotkey_for_platform,
)
from .hud import resolve_ui_language
from .platform_common import (
    FfmpegWavRecorder,
    ForegroundWindow,
    JsonlShimMixin,
    MicrophoneUnavailableError,
    NoMicrophoneError,
    PasteOutcome,
    invoke_recorder_lost_callback,
    invoke_recorder_recovered_callback,
    make_once_commit_guard,
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

MAC_SHIM_SCRIPT = Path(__file__).resolve().with_name("mac_shim.swift")
SHIM_SCHEMA_VERSION = 5
SOUND_MAP = {"start": "Tink.aiff", "stop": "Pop.aiff", "focus": "Glass.aiff", "error": "Basso.aiff"}

_APPLE_SEP = "\x1f"
_SPINNER_PREFIX_RE = re.compile(r"^[\u2800-\u28ff✳✢✶✻✽·•\s]+")
_SPINNER_INLINE_RE = re.compile(r"[\u2800-\u28ff✳✢✶✻✽]+\s*")
_TERM_DIM_RE = re.compile(r"\s*(?:[—–-]|\s)\s*\d+\s*[×x]\s*\d+(?=\s|$)")
_MULTI_WS_RE = re.compile(r"\s+")
_AVF_AUDIO_DEV_RE = re.compile(r"\[(\d+)\]\s+(.+?)\s*$")
_OSA_CODE_RE = re.compile(r"\((-?\d+)\)")

_FRONTMOST_APP_SCRIPT = """\
tell application "System Events"
    set p to first process whose frontmost is true
    set {appName, bundleId, appPid, winTitle, winDoc} to {"", "", 0, "", ""}
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
        try
            set winDoc to value of attribute "AXDocument" of front window of p
        end try
    end if
    set sep to character id 31
    return appName & sep & bundleId & sep & (appPid as text) & sep & winTitle & sep & (winDoc as text)
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

_ITERM2_QUERY_SCRIPT = """\
tell application id "com.googlecode.iterm2"
    if not running or (count of windows) is 0 then return ""
    set w to current window
    try
        if (miniaturized of w) or not (visible of w) then return ""
    end try
    set wid to 0
    set {wname, stty, sname} to {"", "", ""}
    try
        set wid to id of w
    end try
    try
        set wname to name of w
    end try
    try
        set s to current session of current tab of w
        try
            set stty to tty of s
        end try
        try
            set sname to name of s
        end try
    end try
    set sep to character id 31
    return "it1" & sep & (wid as text) & sep & stty & sep & wname & sep & sname
end tell"""

_CMD_V_SCRIPT = 'tell application "System Events" to keystroke "v" using command down'

MACOS_PERMISSIONS_HINT = (
    "Microphone -> terminal/daemon host (ffmpeg capture); "
    "Automation -> Ghostty (split lookup & direct input) or Terminal.app (active tab TTY lookup); "
    "Input Monitoring -> global non-modifier hotkeys / key-test; "
    "Accessibility -> Cmd+V paste for GUI apps and non-Ghostty terminals outside tmux"
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


def is_iterm2_app(app_name: str, bundle_id: str = "") -> bool:
    bid = (bundle_id or "").strip().lower()
    if bid:
        return bid == ITERM2_BUNDLE_ID
    proc = (app_name or "").strip().lower()
    if proc.endswith(".app"):
        proc = proc[:-4]
    return proc in ("iterm2", "iterm")


def _can_automate_bundle_without_prompt(bundle_id: str) -> bool:
    """Non-interactive TCC preflight via ``AEDeterminePermissionToAutomateTarget(..., askUserIfNeeded=False)``.

    Returns True only when Automation is already granted (status == 0), and False when sending
    AppleEvents would trigger a modal consent dialog (-1744), is denied (-1743), or is unavailable.
    """
    if sys.platform != "darwin" or not bundle_id:
        return False
    try:
        import ctypes

        ae_lib = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")

        class _AEDesc(ctypes.Structure):
            _fields_ = [("descriptorType", ctypes.c_uint32), ("dataHandle", ctypes.c_void_p)]

        create_fn = ae_lib.AECreateDesc
        create_fn.argtypes = [ctypes.c_uint32, ctypes.c_char_p, ctypes.c_long, ctypes.POINTER(_AEDesc)]
        create_fn.restype = ctypes.c_int16

        det_fn = getattr(ae_lib, "AEDeterminePermissionToAutomateTarget", None)
        if det_fn is None:
            return False
        det_fn.argtypes = [ctypes.POINTER(_AEDesc), ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint8]
        det_fn.restype = ctypes.c_int32

        dispose_fn = ae_lib.AEDisposeDesc
        dispose_fn.argtypes = [ctypes.POINTER(_AEDesc)]
        dispose_fn.restype = ctypes.c_int16

        raw = bundle_id.encode("utf-8")
        desc = _AEDesc()
        type_bundle = int.from_bytes(b"bund", "big")
        type_wildcard = int.from_bytes(b"****", "big")
        if create_fn(type_bundle, raw, len(raw), ctypes.byref(desc)) != 0:
            return False
        try:
            status = int(det_fn(ctypes.byref(desc), type_wildcard, type_wildcard, 0))
            return status == 0
        finally:
            dispose_fn(ctypes.byref(desc))
    except Exception:
        return False


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


_MAC_SHIM_COMPILE_LOCK = threading.Lock()


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

    with _MAC_SHIM_COMPILE_LOCK:
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
            "-framework", "AppKit", "-framework", "ApplicationServices", "-framework", "CoreAudio",
            "-framework", "AVFoundation", "-framework", "Security",
            "-o", str(tmp_path),
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
    hk = validate_hotkey_for_platform(cfg.hotkey.key, "darwin")
    bin_path, err = compile_mac_shim(env=env, cache_dir=cache_dir, cfg=cfg, announce=True)
    if bin_path is None:
        raise RuntimeError(f"macOS key-test unavailable: {err}")
    cmd = [
        str(bin_path), "--mode", "key-test", "--hotkey", hk,
        "--trigger-mode", cfg.hotkey.trigger_mode, "--tap-max-ms", str(cfg.hotkey.tap_max_ms),
        "--hold-cancel-ms", str(cfg.hotkey.effective_hold_cancel_ms()),
        "--hold-hint-ms", str(cfg.hotkey.effective_hold_hint_ms()),
        "--debounce-ms", str(cfg.hotkey.debounce_ms), "--seconds", str(max(1, seconds)),
    ]
    return stream_key_test_proc(cmd, env=os.environ if env is None else env)


def _normalize_title_for_drift(title: str) -> str:
    cleaned = _SPINNER_PREFIX_RE.sub("", title.strip())
    cleaned = _SPINNER_INLINE_RE.sub("", cleaned)
    return _MULTI_WS_RE.sub(" ", cleaned).strip()


def _normalize_tmux_title_for_drift(title: str) -> str:
    return _normalize_title_for_drift(_TERM_DIM_RE.sub("", title))


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


def classify_macos_mic_permission(
    st: Mapping[str, Any] | None,
    env: Mapping[str, str] | None = None,
) -> MicrophoneUnavailableError | None:
    mic_auth = str((st or {}).get("mic_auth") or "").strip().lower()
    if mic_auth == "authorized":
        return None
    if mic_auth == "denied":
        return MicrophoneUnavailableError(
            "macOS Microphone access is denied; enable your terminal app in System Settings -> Privacy & Security -> Microphone",
            reason_code="mic_permission",
            hud_state="mic_permission",
        )
    if mic_auth == "restricted":
        return MicrophoneUnavailableError(
            "macOS Microphone access is restricted by system or management policy",
            reason_code="mic_permission",
            hud_state="mic_permission",
        )
    if mic_auth == "not_determined":
        non_gui_msg = (
            "macOS daemon is running in a non-GUI/SSH session without Microphone TCC access; "
            "run 'agent-voice-input restart' inside a local macOS terminal (outside any SSH-started tmux server)"
        )
        has_graphic = (st or {}).get("has_graphic_session")
        if isinstance(has_graphic, bool):
            if has_graphic is False:
                return MicrophoneUnavailableError(
                    non_gui_msg,
                    reason_code="mic_permission",
                    hud_state="mic_permission",
                )
            return None
        eff_env = env if env is not None else {}
        if any(str(eff_env.get(k) or "").strip() for k in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")):
            return MicrophoneUnavailableError(
                non_gui_msg,
                reason_code="mic_permission",
                hud_state="mic_permission",
            )
        return None
    return None


def _run_mac_shim_json_mode(
    shim_bin: Path,
    flag: str,
    label: str,
    env: Mapping[str, str],
    *,
    cfg: Config | None = None,
    timeout: float = 5.0,
) -> tuple[bool, dict[str, Any], str]:
    try:
        sc = subprocess.run(
            [str(shim_bin), flag],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=dict(env),
            timeout=timeout,
            check=False,
        )
    except Exception as exc:
        return False, {}, redact_secrets(str(exc), env, cfg=cfg)
    if sc.returncode != 0:
        raw_err = (sc.stderr or sc.stdout or f"{label} rc={sc.returncode}").strip()
        return False, {}, redact_secrets(raw_err, env, cfg=cfg)
    out = (sc.stdout or "").strip()
    if not out:
        return False, {}, f"empty {label} output"
    try:
        parsed = json.loads(out.splitlines()[-1])
    except ValueError as exc:
        return False, {}, f"invalid {label} JSON: {redact_secrets(str(exc), env, cfg=cfg)}"
    if not isinstance(parsed, dict) or parsed.get("ok") is not True:
        return False, {}, f"unexpected {label} payload: {redact_secrets(repr(parsed), env, cfg=cfg)}"
    return True, parsed, ""


def run_mac_shim_self_check(
    shim_bin: Path,
    env: Mapping[str, str],
    *,
    cfg: Config | None = None,
    timeout: float = 5.0,
) -> tuple[bool, dict[str, Any], str]:
    return _run_mac_shim_json_mode(shim_bin, "--self-check", "self-check", env, cfg=cfg, timeout=timeout)


def run_mac_shim_audio_status(
    shim_bin: Path,
    env: Mapping[str, str],
    *,
    cfg: Config | None = None,
    timeout: float = 2.0,
) -> tuple[bool, dict[str, Any], str]:
    return _run_mac_shim_json_mode(shim_bin, "--audio-status", "audio-status", env, cfg=cfg, timeout=timeout)


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
        "pbcopy_ok": bool(
            shutil.which("pbcopy", path=eff_env.get("PATH"))
            or (
                not is_hermetic_env(env)
                and os.path.isfile("/usr/bin/pbcopy")
                and os.access("/usr/bin/pbcopy", os.X_OK)
            )
        ),
        "tmux_active": bool(eff_env.get("TMUX")),
        "clt_ok": True, "clt_error": "",
        "swiftc_ok": False, "swiftc_bin": "", "mac_shim_ok": False, "mac_shim_bin": "", "mac_shim_error": "",
        "caller_mic_auth": "", "caller_has_graphic_session": None, "caller_is_remote_session": None, "caller_mic_warning": "",
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
        sc_ok, sc_json, sc_err = run_mac_shim_self_check(shim_bin, eff_env, cfg=cfg, timeout=5.0)
        info["mac_shim_ok"] = sc_ok
        if sc_ok:
            ui_cult = str(sc_json.get("ui_culture") or "").strip()
            if isinstance(sc_json.get("ax_trusted"), bool):
                info["ax_trusted"] = sc_json["ax_trusted"]
            if isinstance(sc_json.get("listen_event_access"), bool):
                info["listen_event_access"] = sc_json["listen_event_access"]
            aud_st = sc_json.get("audio_status")
            if isinstance(aud_st, Mapping):
                info["caller_mic_auth"] = str(aud_st.get("mic_auth") or "").strip().lower()
                if isinstance(aud_st.get("has_graphic_session"), bool):
                    info["caller_has_graphic_session"] = aud_st["has_graphic_session"]
                if isinstance(aud_st.get("is_remote_session"), bool):
                    info["caller_is_remote_session"] = aud_st["is_remote_session"]
                if (perm_err := classify_macos_mic_permission(aud_st, eff_env)) is not None:
                    info["caller_mic_warning"] = redact_secrets(str(perm_err), eff_env, cfg=cfg)
        elif sc_err:
            info["mac_shim_error"] = sc_err
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
        initial_mic_auth: str = "",
    ) -> None:
        self.gen = gen
        self.rec = rec
        self.runtime_dir = runtime_dir
        self.active_uid = active_uid
        self.active_name = active_name
        self.active_name_unique = active_name_unique
        self.daemon_seq_id = daemon_seq_id
        self.initial_mic_auth = initial_mic_auth
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
        self_check_fn: Callable[[], Mapping[str, Any] | None] | None = None,
    ) -> None:
        self.cfg = cfg
        self._hermetic_env = is_hermetic_env(env)
        self.env = dict(os.environ if env is None else env)
        self.audio_dev = resolve_macos_audio_device(self.cfg, self.env)
        self._cache_dir = cache_dir
        self._shim_cmd_factory = shim_cmd_factory
        self._tmux_probe_fn = tmux_probe_fn
        self._self_check_fn = self_check_fn
        self._fg_tls = threading.local()
        self._ff_rec = FfmpegWavRecorder()
        self._recording = False
        self._recorder_lost_cb: Callable[..., Any] | None = None
        self._recorder_recovered_cb: Callable[..., Any] | None = None
        self._audio_cache_lock = threading.Lock()
        self._audio_event_ver: int = 0
        self._audio_status_known: bool = False
        self._hal_ok: bool = True
        self._has_audio_input: bool = True
        self._default_input_uid: str = ""
        self._default_input_name: str = ""
        self._default_input_name_unique: bool = False
        self._alive_input_uids: set[str] = set()
        self._mic_auth: str = ""
        self._has_graphic_session: bool | None = None
        self._is_remote_session: bool | None = None
        self._rec_cond = threading.Condition()
        self._rec_active: bool = False
        self._rec_gen: int = 0
        self._cur_session: _MacRecSession | None = None
        self._rec_cleanup_threads: set[threading.Thread] = set()
        self._busy_wait_timeout_sec: float = 4.0
        self._paste_hold_timeout_sec: float | None = None
        self._iterm2_osa_retry_after: float = 0.0
        self.last_audio_error = ""
        self._last_stop_recorder_lost: bool = False
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

    def _update_audio_status_cache(
        self,
        st: Mapping[str, Any],
        *,
        from_event: bool = False,
        expected_event_ver: int | None = None,
    ) -> tuple[bool, str, set[str]]:
        raw_alive = st.get("alive_uids")
        alive_uids = (
            {str(u).strip() for u in raw_alive if str(u).strip()}
            if isinstance(raw_alive, (list, tuple, set, frozenset))
            else set()
        )
        hal_ok = bool(st["hal_ok"]) if ("hal_ok" in st and st["hal_ok"] is not None) else True
        has_input = bool(st["has_input"]) if "has_input" in st else bool(alive_uids)
        default_uid = str(st.get("default_uid") or "").strip()
        default_name = str(st.get("default_name") or "").strip()
        default_unique = _is_unique_audio_device_name(st, default_uid, default_name)
        has_perm_fields = any(k in st for k in ("mic_auth", "has_graphic_session", "is_remote_session"))
        mic_auth = str(st.get("mic_auth") or "").strip().lower()
        raw_graphic = st.get("has_graphic_session")
        has_graphic = raw_graphic if isinstance(raw_graphic, bool) else None
        raw_remote = st.get("is_remote_session")
        is_remote = raw_remote if isinstance(raw_remote, bool) else None
        with self._audio_cache_lock:
            if from_event:
                self._audio_event_ver += 1
            elif expected_event_ver is not None and self._audio_event_ver != expected_event_ver:
                # A newer device change event landed while this probe was in flight; never clobber it.
                return has_input, default_uid, alive_uids
            self._audio_status_known = True
            self._hal_ok = hal_ok
            self._has_audio_input = has_input
            self._default_input_uid = default_uid
            self._default_input_name = default_name
            self._default_input_name_unique = default_unique
            self._alive_input_uids = alive_uids
            if has_perm_fields:
                self._mic_auth = mic_auth
                self._has_graphic_session = has_graphic
                self._is_remote_session = is_remote
        return has_input, default_uid, alive_uids

    def _normalize_and_cache_audio_status(
        self,
        st: Mapping[str, Any],
        *,
        expected_event_ver: int | None = None,
    ) -> dict[str, Any]:
        has_input, default_uid, alive_uids = self._update_audio_status_cache(
            st, expected_event_ver=expected_event_ver
        )
        default_name = str(st.get("default_name") or "").strip()
        hal_ok = bool(st["hal_ok"]) if ("hal_ok" in st and st["hal_ok"] is not None) else True
        mic_auth = str(st.get("mic_auth") or "").strip().lower()
        raw_graphic = st.get("has_graphic_session")
        has_graphic = raw_graphic if isinstance(raw_graphic, bool) else None
        raw_remote = st.get("is_remote_session")
        is_remote = raw_remote if isinstance(raw_remote, bool) else None
        return {
            **st,
            "hal_ok": hal_ok,
            "has_input": has_input,
            "default_uid": default_uid,
            "default_name": default_name,
            "default_name_unique": _is_unique_audio_device_name(st, default_uid, default_name),
            "alive_uids": sorted(alive_uids),
            "mic_auth": mic_auth,
            "has_graphic_session": has_graphic,
            "is_remote_session": is_remote,
        }

    def _probe_unmanaged_audio_status(self, timeout_sec: float = 1.5) -> dict[str, Any] | None:
        if self._closed or self._shim_disabled:
            return None
        with self._audio_cache_lock:
            ver_before = self._audio_event_ver
        raw_st: Any = None
        if self._self_check_fn is not None:
            try:
                raw_st = self._self_check_fn()
            except Exception:
                return None
            if (
                isinstance(raw_st, Mapping)
                and isinstance(raw_st.get("audio_status"), Mapping)
                and "has_input" not in raw_st
                and "mic_auth" not in raw_st
            ):
                raw_st = raw_st["audio_status"]
        elif (
            sys.platform == "darwin"
            and self._shim_cmd_factory is None
            and getattr(self._ff_rec, "_cmd_factory", None) is None
        ):
            bin_path, _err = compile_mac_shim(
                env=self.env, cache_dir=self._cache_dir, cfg=self.cfg, announce=False
            )
            if bin_path is None:
                return None
            sc_ok, sc_json, _sc_err = run_mac_shim_audio_status(
                bin_path, self.env, cfg=self.cfg, timeout=max(1.5, timeout_sec)
            )
            if not sc_ok:
                return None
            raw_st = sc_json.get("audio_status")
        if not isinstance(raw_st, Mapping):
            return None
        return self._normalize_and_cache_audio_status(raw_st, expected_event_ver=ver_before)

    def _query_live_audio_status(self, timeout_sec: float = 0.8) -> dict[str, Any] | None:
        if not self._has_managed_shim():
            return self._probe_unmanaged_audio_status(timeout_sec=timeout_sec)
        with self._audio_cache_lock:
            ver_before = self._audio_event_ver
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
            else (res if ("has_input" in res or "alive_uids" in res or "mic_auth" in res) else None)
        )
        if not isinstance(st, dict):
            return None
        return self._normalize_and_cache_audio_status(st, expected_event_ver=ver_before)

    def _on_shim_internal_event(self, msg: dict[str, Any]) -> None:
        if str(msg.get("event") or "") != "audio_devices_changed":
            return
        has_input, _default_uid, alive_uids = self._update_audio_status_cache(msg, from_event=True)
        claimed_session: _MacRecSession | None = None
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
                session.force_lost_reason = "coreaudio_device_disconnected"
                self._rec_active = False
                self._recording = False
                session.busy = True
                claimed_session = session
        if claimed_session is None:
            return

        def _finish_disconnected_session(s: _MacRecSession = claimed_session) -> None:
            try:
                try:
                    self._stop_level_streamer()
                    seg_wav = s.rec.stop(drain_sec=0.0)
                    with self._rec_cond:
                        if not s.aborted and len(seg_wav) > 44:
                            s.pcm.extend(seg_wav[44:])
                finally:
                    with self._rec_cond:
                        s.busy = False
                        self._rec_cond.notify_all()
                with self._rec_cond:
                    deliver_lost = not self._closed and not s.aborted and self._cur_session is s
                if not deliver_lost:
                    return
                self.last_audio_error = "coreaudio_device_disconnected"
                if (lost_cb := self._recorder_lost_cb) is not None:
                    try:
                        invoke_recorder_lost_callback(lost_cb, s.daemon_seq_id)
                    except Exception:
                        pass
            finally:
                with self._rec_cond:
                    self._rec_cleanup_threads.discard(threading.current_thread())

        th = threading.Thread(
            target=_finish_disconnected_session,
            name="avi-mac-rec-disconnect",
            daemon=True,
        )
        with self._rec_cond:
            self._rec_cleanup_threads = {t for t in self._rec_cleanup_threads if t.is_alive()}
            self._rec_cleanup_threads.add(th)
        try:
            th.start()
        except Exception:
            with self._rec_cond:
                self._rec_cleanup_threads.discard(th)
                claimed_session.busy = False
                self._rec_cond.notify_all()

    def _which(self, name: str) -> str | None:
        if "PATH" in self.env:
            if found := shutil.which(name, path=self.env.get("PATH")):
                return found
            if not self._hermetic_env and name in ("osascript", "pbcopy", "afplay", "ps"):
                sys_p = "/bin/ps" if name == "ps" else f"/usr/bin/{name}"
                if os.path.isfile(sys_p) and os.access(sys_p, os.X_OK):
                    return sys_p
            return None
        return shutil.which(name)

    def _resolve_tmux_exe(self) -> str:
        if found := self._which("tmux"):
            return found if os.path.sep in found and Path(found).name == "tmux" else "tmux"
        return resolve_tmux_bin(self.env) if ("PATH" in self.env or not self._hermetic_env) else ""

    def _run_osascript(self, args: list[str], *, timeout: float = 2.5) -> subprocess.CompletedProcess[str] | None:
        osa_bin = self._which("osascript")
        if not osa_bin:
            return None
        exe = osa_bin if os.path.sep in osa_bin and Path(osa_bin).name == "osascript" else "osascript"
        try:
            return subprocess.run(
                [exe, *args], stdin=subprocess.DEVNULL,
                capture_output=True, text=True, env=self.env, timeout=timeout, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    def _build_shim_cmd(self) -> list[str] | None:
        if self._shim_cmd_factory is not None:
            return self._shim_cmd_factory()
        try:
            hk = validate_hotkey_for_platform(self.cfg.hotkey.key, "darwin")
        except ConfigError as exc:
            self.last_shim_error = redact_secrets(str(exc), self.env, cfg=self.cfg)
            return None
        bin_path, err = compile_mac_shim(env=self.env, cache_dir=self._cache_dir, cfg=self.cfg, announce=True)
        if bin_path is None:
            self.last_shim_error = redact_secrets(err, self.env, cfg=self.cfg)
            return None
        self.last_shim_error = ""
        return [
            str(bin_path), "--mode", "bridge", "--hotkey", hk,
            "--trigger-mode", self.cfg.hotkey.trigger_mode, "--tap-max-ms", str(self.cfg.hotkey.tap_max_ms),
            "--hold-cancel-ms", str(self.cfg.hotkey.effective_hold_cancel_ms()),
            "--hold-hint-ms", str(self.cfg.hotkey.effective_hold_hint_ms()),
            "--debounce-ms", str(self.cfg.hotkey.debounce_ms),
        ]

    def _query_ax_title(self) -> str:
        ax = self._run_osascript(["-e", _FRONTMOST_AXTITLE_SCRIPT], timeout=2.0)
        return ax.stdout.strip() if (ax is not None and ax.returncode == 0) else ""

    def _query_frontmost_process_and_title(self) -> tuple[str, str, str, int]:
        self._fg_tls.hwnd = 0
        self._fg_tls.from_shim = False
        self._fg_tls.ax_trusted = None
        self._fg_tls.document = ""
        self._fg_tls.page_url = ""
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
                raw_doc = str(r.get("document") or "").strip()
                self._fg_tls.document = raw_doc if raw_doc.lower().startswith("file://") else ""
                self._fg_tls.page_url = str(r.get("page_url") or "").strip()
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
        raw_osa_doc = parts[4].strip() if len(parts) > 4 else ""
        self._fg_tls.document = raw_osa_doc if raw_osa_doc.lower().startswith("file://") else ""
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

    def _query_iterm2_terminal(self) -> tuple[int, str, str, str] | None:
        if time.monotonic() < self._iterm2_osa_retry_after or not _can_automate_bundle_without_prompt(ITERM2_BUNDLE_ID):
            return None
        p = self._run_osascript(["-e", _ITERM2_QUERY_SCRIPT], timeout=1.5)
        if p is None:
            return None
        if p.returncode != 0:
            err_low = (p.stderr or "").lower()
            if "-1743" in err_low or "(1743)" in err_low or "-1744" in err_low or "not authorized" in err_low:
                self._iterm2_osa_retry_after = time.monotonic() + 60.0
            return None
        if not p.stdout.strip():
            return None
        parts = p.stdout.rstrip("\r\n").split(_APPLE_SEP)
        if len(parts) < 4 or parts[0] != "it1":
            return None
        try:
            win_id = max(0, int(parts[1].strip())) if parts[1].strip() else 0
        except ValueError:
            win_id = 0
        tab_tty = normalize_tty_path(parts[2].strip())
        if not tab_tty:
            return None
        wname = parts[3].strip()
        sname = parts[4].strip() if len(parts) > 4 else ""
        eff_title = f"{wname} {sname}".strip() if (sname and sname not in wname) else (wname or sname)
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
            raw_hint_doc = (hint.document or "").strip()
            win_doc = raw_hint_doc if raw_hint_doc.lower().startswith("file://") else ""
            win_page_url = (hint.page_url or "").strip()
            from_shim = hint.ax_trusted is not None
            ax_trusted = hint.ax_trusted
            if (
                not is_ghostty_app(app_name, bundle_id)
                and not is_apple_terminal_app(app_name, bundle_id)
                and not is_iterm2_app(app_name, bundle_id)
                and not win_title
                and not from_shim
                and ax_trusted is not False
            ):
                win_title = self._query_ax_title()
        else:
            self._fg_tls.hwnd = 0
            self._fg_tls.from_shim = False
            self._fg_tls.ax_trusted = None
            self._fg_tls.document = ""
            self._fg_tls.page_url = ""
            app_name, bundle_id, win_title, app_pid = self._query_frontmost_process_and_title()
            app_hwnd = int(getattr(self._fg_tls, "hwnd", 0) or 0)
            from_shim = bool(getattr(self._fg_tls, "from_shim", False))
            ax_trusted = getattr(self._fg_tls, "ax_trusted", None)
            win_doc = str(getattr(self._fg_tls, "document", "") or "")
            win_page_url = str(getattr(self._fg_tls, "page_url", "") or "")

        if is_ghostty_app(app_name, bundle_id):
            eff_name, eff_bundle = app_name or "Ghostty", bundle_id or GHOSTTY_BUNDLE_ID
            if not (from_shim and app_hwnd <= 0):
                if (g_info := self._query_ghostty_terminal()) is not None:
                    surface_id, g_title, tty, cwd, t_pid = g_info
                    g_win = self._enrich_window_tmux_target(
                        ForegroundWindow(
                            app_hwnd, eff_name, g_title, eff_bundle, surface_id, tty, cwd, t_pid, ax_trusted=ax_trusted
                        )
                    )
                    if hint is not None and self.origin_resolution_conflicted(hint, g_win):
                        return replace(hint, origin_drifted=True)
                    return g_win
            if not win_title and not from_shim and ax_trusted is not False:
                win_title = self._query_ax_title()
            g_win = ForegroundWindow(
                app_hwnd,
                eff_name,
                self._append_tmux_pane_title(win_title, eff_name, eff_bundle),
                eff_bundle,
                pid=app_pid,
                ax_trusted=ax_trusted,
            )
            return self._enrich_window_tmux_target(g_win) if not (from_shim and app_hwnd <= 0) else g_win

        if is_apple_terminal_app(app_name, bundle_id):
            eff_name = app_name or "Terminal"
            eff_bundle = bundle_id or APPLE_TERMINAL_BUNDLE_ID
            saw_other_panel = False
            if not (from_shim and app_hwnd <= 0):
                if (t_info := self._query_apple_terminal()) is not None:
                    t_wid, t_title, t_tty, t_wname = t_info
                    is_other_panel = bool(
                        (
                            win_title
                            and t_wname
                            and _normalize_title_for_drift(win_title) != _normalize_title_for_drift(t_wname)
                            and (app_hwnd <= 0 or t_wid <= 0 or app_hwnd != t_wid)
                        )
                        or (
                            not win_title
                            and ax_trusted is False
                            and app_hwnd > 0
                            and t_wid > 0
                            and app_hwnd != t_wid
                        )
                    )
                    if not is_other_panel:
                        eff_hwnd = t_wid if t_wid > 0 else app_hwnd
                        merged_title = t_title or win_title
                        return self._enrich_window_tmux_target(
                            ForegroundWindow(
                                eff_hwnd,
                                eff_name,
                                self._append_tmux_pane_title(merged_title, eff_name, eff_bundle),
                                eff_bundle,
                                tty=t_tty,
                                pid=app_pid,
                                ax_trusted=ax_trusted,
                            )
                        )
                    saw_other_panel = True
            if not win_title and not from_shim and ax_trusted is not False:
                win_title = self._query_ax_title()
            term_win = ForegroundWindow(
                app_hwnd,
                eff_name,
                self._append_tmux_pane_title(win_title, eff_name, eff_bundle),
                eff_bundle,
                pid=app_pid,
                ax_trusted=ax_trusted,
            )
            if not (from_shim and app_hwnd <= 0) and not saw_other_panel:
                return self._enrich_window_tmux_target(term_win)
            return term_win

        if is_iterm2_app(app_name, bundle_id):
            eff_name = app_name or "iTerm2"
            eff_bundle = bundle_id or ITERM2_BUNDLE_ID
            saw_other_iterm_panel = False
            if not (from_shim and app_hwnd <= 0):
                if (it_info := self._query_iterm2_terminal()) is not None:
                    it_wid, it_title, it_tty, it_wname = it_info
                    is_other_iterm_panel = bool(
                        win_title
                        and it_wname
                        and _normalize_title_for_drift(win_title) != _normalize_title_for_drift(it_wname)
                        and (it_wname not in win_title and win_title not in it_wname)
                    )
                    if not is_other_iterm_panel:
                        eff_hwnd = app_hwnd if app_hwnd > 0 else it_wid
                        merged_it_title = win_title or it_title
                        if it_title and win_title and it_title not in win_title:
                            merged_it_title = f"{win_title} {it_title}".strip()
                        return self._enrich_window_tmux_target(
                            ForegroundWindow(
                                eff_hwnd,
                                eff_name,
                                self._append_tmux_pane_title(merged_it_title, eff_name, eff_bundle),
                                eff_bundle,
                                tty=it_tty,
                                pid=app_pid,
                                ax_trusted=ax_trusted,
                            )
                        )
                    saw_other_iterm_panel = True
            if hint is not None and not win_title and not from_shim and ax_trusted is not False:
                win_title = self._query_ax_title()
            iterm_win = ForegroundWindow(
                app_hwnd,
                eff_name,
                self._append_tmux_pane_title(win_title, eff_name, eff_bundle),
                eff_bundle,
                pid=app_pid,
                ax_trusted=ax_trusted,
                document=win_doc,
            )
            if not (from_shim and app_hwnd <= 0) and not saw_other_iterm_panel:
                return self._enrich_window_tmux_target(iterm_win)
            return iterm_win

        other_win = ForegroundWindow(
            app_hwnd,
            app_name,
            self._append_tmux_pane_title(win_title, app_name, bundle_id),
            bundle_id,
            pid=app_pid,
            ax_trusted=ax_trusted,
            document=win_doc,
            page_url=win_page_url,
        )
        return self._enrich_window_tmux_target(other_win) if not (from_shim and app_hwnd <= 0) else other_win

    _concurrent_origin_resolve: bool = True
    _GENERIC_TERMINAL_TITLES: frozenset[str] = frozenset(
        {"ghostty", "terminal", "终端", "iterm2", "iterm", "alacritty", "wezterm", "kitty"}
    )

    def origin_resolution_conflicted(
        self, trigger_win: ForegroundWindow | None, resolved: ForegroundWindow
    ) -> bool:
        if trigger_win is None:
            return False
        if trigger_win.origin_drifted or resolved.origin_drifted:
            return True
        if trigger_win.bundle_id and resolved.bundle_id:
            if trigger_win.bundle_id.lower() != resolved.bundle_id.lower():
                return True
        elif trigger_win.process and resolved.process:
            if trigger_win.process.lower() != resolved.process.lower():
                return True
        if trigger_win.hwnd > 0 and resolved.hwnd > 0 and trigger_win.hwnd != resolved.hwnd:
            return True
        if trigger_win.surface_id and resolved.surface_id and trigger_win.surface_id != resolved.surface_id:
            return True
        if trigger_win.tmux_pane:
            if not resolved.tmux_pane or trigger_win.tmux_pane != resolved.tmux_pane:
                return True
            if (
                trigger_win.tmux_socket
                and resolved.tmux_socket
                and not _same_tmux_server(trigger_win.tmux_socket, resolved.tmux_socket)
            ):
                return True
        t_tty = normalize_tty_path(trigger_win.tty)
        r_tty = normalize_tty_path(resolved.tty)
        if t_tty and r_tty and t_tty != r_tty:
            return True
        t_title = (trigger_win.title or "").strip()
        r_title = (resolved.title or "").strip()
        if t_title and r_title and t_title.lower() not in self._GENERIC_TERMINAL_TITLES:
            t_hint = parse_window_title_hint(t_title)
            r_hint = parse_window_title_hint(r_title)
            if t_hint is not None or r_hint is not None:
                if t_hint is None or r_hint is None or t_hint.session_token != r_hint.session_token:
                    return True
            else:
                norm_fn = (
                    _normalize_tmux_title_for_drift
                    if (trigger_win.tmux_pane or resolved.tmux_pane)
                    else _normalize_title_for_drift
                )
                t_norm = norm_fn(t_title)
                r_norm = norm_fn(r_title)
                if t_norm and r_norm and t_norm != r_norm:
                    if not (r_norm.startswith(t_norm + " ") or t_norm.startswith(r_norm + " ")):
                        return True
        return False

    def resolve_foreground_window(self, window: ForegroundWindow | None = None) -> ForegroundWindow:
        if window is None:
            return self.get_foreground_window()
        if is_ghostty_app(window.process, window.bundle_id):
            if window.surface_id or (window.title and not self._which("osascript")):
                return self._enrich_window_tmux_target(window)
            return self.get_foreground_window(hint=window)
        if is_apple_terminal_app(window.process, window.bundle_id):
            if window.tty or (window.title and not self._which("osascript")):
                return self._enrich_window_tmux_target(window)
            return self.get_foreground_window(hint=window)
        if is_iterm2_app(window.process, window.bundle_id):
            if window.tty or (window.title and not self._which("osascript")):
                return self._enrich_window_tmux_target(window)
            return self.get_foreground_window(hint=window)
        if (
            window.surface_id
            or window.title
            or (window.ax_trusted is not None and not self.env.get("TMUX"))
            or (window.hwnd and not self._which("osascript"))
        ):
            return self._enrich_window_tmux_target(window)
        if window.title and not window.process and not window.bundle_id:
            return self._enrich_window_tmux_target(window)
        return self.get_foreground_window(hint=window)

    def beep(self, kind: str) -> None:
        snd = f"/System/Library/Sounds/{SOUND_MAP.get(kind, 'Basso.aiff')}"
        af_bin = self._which("afplay") if self.cfg.audio.beep else None
        if af_bin and os.path.isfile(snd):
            exe = af_bin if os.path.sep in af_bin and Path(af_bin).name == "afplay" else "afplay"
            try:
                subprocess.Popen([exe, snd], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        aborted_before_probe = False
        with self._rec_cond:
            self._rec_gen += 1
            start_gen = self._rec_gen
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
            if self._closed or self._rec_gen != start_gen:
                aborted_before_probe = True
        if old_session is not None and old_idle:
            old_session.rec.stop(drain_sec=0.0)
        if aborted_before_probe:
            return

        self.last_audio_error = ""
        self._last_stop_recorder_lost = False
        has_known_input: bool | None = None
        locked_uid = ""
        locked_name = ""
        locked_name_unique = False
        had_locked_uid_in_alive = False
        initial_mic_auth = ""
        is_default_dev = self.audio_dev.lstrip(":").strip().lower() in ("", "default")
        if not self._shim_disabled and not self._closed:
            live_st = self._query_live_audio_status(timeout_sec=0.8)
            with self._rec_cond:
                if self._closed or self._rec_gen != start_gen:
                    return
            if live_st is not None:
                eff_st: dict[str, Any] = live_st
                known = True
                hal_ok = bool(live_st.get("hal_ok", True))
                has_in = bool(live_st.get("has_input", True))
                def_uid = str(live_st.get("default_uid") or "").strip()
                def_name = str(live_st.get("default_name") or "").strip()
                def_uniq = bool(live_st.get("default_name_unique", False))
                init_alive = {str(u).strip() for u in (live_st.get("alive_uids") or []) if str(u).strip()}
            else:
                with self._audio_cache_lock:
                    known = self._audio_status_known
                    hal_ok = self._hal_ok
                    has_in = self._has_audio_input
                    def_uid = self._default_input_uid
                    def_name = self._default_input_name
                    def_uniq = self._default_input_name_unique
                    init_alive = set(self._alive_input_uids)
                    eff_st = (
                        {
                            "hal_ok": hal_ok,
                            "has_input": has_in,
                            "mic_auth": self._mic_auth,
                            "has_graphic_session": self._has_graphic_session,
                            "is_remote_session": self._is_remote_session,
                        }
                        if known
                        else {}
                    )
            if known and hal_ok and not has_in:
                self.last_audio_error = "coreaudio_no_input_device"
                raise NoMicrophoneError("no microphone input device available on macOS")
            if (perm_err := classify_macos_mic_permission(eff_st, self.env)) is not None:
                self.last_audio_error = "macos_mic_permission"
                raise perm_err
            if known and not has_in:
                self.last_audio_error = "coreaudio_no_input_device"
                raise NoMicrophoneError("no microphone input device available on macOS")
            if known:
                has_known_input = True
                initial_mic_auth = str(eff_st.get("mic_auth") or "").strip().lower()
                if is_default_dev:
                    locked_uid = def_uid
                    locked_name = def_name
                    locked_name_unique = def_uniq
                    had_locked_uid_in_alive = bool(locked_uid and locked_uid in init_alive)

        handoff_disconnected = [False]
        handoff_has_input = [True]

        def _selected_input_disconnected() -> bool:
            if not (has_known_input or locked_uid):
                return False
            with self._audio_cache_lock:
                if not self._audio_status_known:
                    return False
                if not self._has_audio_input or (
                    locked_uid
                    and (had_locked_uid_in_alive or bool(self._alive_input_uids))
                    and locked_uid not in self._alive_input_uids
                ):
                    handoff_disconnected[0] = True
                    handoff_has_input[0] = bool(self._has_audio_input)
                    return True
            return False

        pre_spawn_aborted = False
        with self._rec_cond:
            if self._closed or self._rec_gen != start_gen:
                return
            rec_obj = self._ff_rec
            session = _MacRecSession(
                start_gen,
                rec_obj,
                runtime_dir,
                active_uid=locked_uid,
                active_name=locked_name,
                active_name_unique=locked_name_unique,
                daemon_seq_id=seq_id,
                initial_mic_auth=initial_mic_auth,
            )
            use_guarded_spawn = isinstance(rec_obj, FfmpegWavRecorder)
            if not use_guarded_spawn:
                if _selected_input_disconnected():
                    pre_spawn_aborted = True
                else:
                    self._cur_session = session
                    self._rec_active = True
                    self._recording = True

        if pre_spawn_aborted:
            self.last_audio_error = "coreaudio_device_disconnected"
            if not handoff_has_input[0]:
                raise NoMicrophoneError("no microphone input device available on macOS")
            raise MicrophoneUnavailableError(
                "selected microphone disconnected before capture start",
                reason_code="mic_unavailable",
                hud_state="mic_unavailable",
            )

        spawned_ok = [not use_guarded_spawn]

        def _can_spawn_session() -> bool:
            if self._closed or self._rec_gen != start_gen or self._ff_rec is not rec_obj:
                return False
            return not _selected_input_disconnected()

        def _on_session_spawned() -> None:
            spawned_ok[0] = True
            self._cur_session = session
            self._rec_active = True
            self._recording = True

        startup_timeout = 15.0 if initial_mic_auth == "not_determined" else 0.85
        start_kwargs: dict[str, Any] = {
            "verify_startup": True,
            "startup_timeout_sec": startup_timeout,
            "stall_timeout_sec": 1.2,
            "has_known_input": has_known_input,
            "on_unexpected_exit": lambda s=session: self._on_ffmpeg_unexpected_exit(s),
            "on_level": self._on_recorder_level,
        }
        if use_guarded_spawn:
            start_kwargs["spawn_lock"] = self._rec_cond
            start_kwargs["can_spawn"] = _can_spawn_session
            start_kwargs["on_spawned"] = _on_session_spawned
        try:
            session.rec.start(
                "avfoundation",
                f":{self.audio_dev}",
                runtime_dir,
                self.env,
                **start_kwargs,
            )
        except Exception:
            with self._rec_cond:
                session.active = False
                session.aborted = True
                if self._cur_session is session:
                    self._cur_session = None
                    self._rec_active = False
                    self._recording = False
                self._rec_cond.notify_all()
            if session.initial_mic_auth == "not_determined":
                post_st = self._query_live_audio_status(timeout_sec=0.6)
                if (perm_err := classify_macos_mic_permission(post_st, self.env)) is not None:
                    self.last_audio_error = "macos_mic_permission"
                    raise perm_err from None
            self.last_audio_error = session.rec.diagnostic_summary()
            raise

        if not spawned_ok[0]:
            if handoff_disconnected[0]:
                self.last_audio_error = "coreaudio_device_disconnected"
                if not handoff_has_input[0]:
                    raise NoMicrophoneError("no microphone input device available on macOS")
                raise MicrophoneUnavailableError(
                    "selected microphone disconnected before capture start",
                    reason_code="mic_unavailable",
                    hud_state="mic_unavailable",
                )
            return

        cleanup_early_stop = False
        owns_early_busy = False
        with self._rec_cond:
            if self._closed and self._cur_session is session:
                session.active = False
                session.aborted = True
                self._cur_session = None
                self._rec_active = False
                self._recording = False
                self._rec_cond.notify_all()
            still_active = (
                not self._closed
                and session.active
                and not session.aborted
                and self._cur_session is session
            )
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
                            classify_macos_mic_permission(live_st, self.env) is None
                            and bool(live_st.get("has_input", bool(alive_set)))
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
                        respawn_spawned = [not isinstance(session.rec, FfmpegWavRecorder)]
                        respawn_kw: dict[str, Any] = {
                            "verify_startup": True,
                            "startup_timeout_sec": 0.85,
                            "stall_timeout_sec": 1.2,
                            "has_known_input": True,
                            "on_unexpected_exit": lambda s=session: self._on_ffmpeg_unexpected_exit(s),
                            "on_level": self._on_recorder_level,
                        }
                        if isinstance(session.rec, FfmpegWavRecorder):
                            respawn_kw["spawn_lock"] = self._rec_cond
                            respawn_kw["can_spawn"] = (
                                lambda s=session: not self._closed
                                and s.active
                                and not s.aborted
                                and not s.force_lost_reason
                            )
                            respawn_kw["on_spawned"] = lambda: respawn_spawned.__setitem__(0, True)
                        try:
                            session.rec.start(
                                "avfoundation",
                                f":{session.active_name}",
                                session.runtime_dir,
                                self.env,
                                **respawn_kw,
                            )
                        except Exception:
                            diag = session.rec.diagnostic_summary() or diag
                        else:
                            if not respawn_spawned[0]:
                                with self._rec_cond:
                                    if session.force_lost_reason:
                                        diag = session.force_lost_reason
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
                    if not session.force_lost_reason:
                        session.force_lost_reason = diag
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
        self._last_stop_recorder_lost = False
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
            lost_reason = (session.force_lost_reason if session is not None else "")

        if session is None:
            drain_sec = self.cfg.audio.effective_tail_drain_sec(macos=True) if (drain and was_rec) else 0.0
            wav = self._ff_rec.stop(drain_sec=drain_sec)
            if not self.last_audio_error:
                self.last_audio_error = "" if wav else self._ff_rec.diagnostic_summary()
            return wav, "avfoundation"

        if lost_reason:
            self.last_audio_error = lost_reason
            self._last_stop_recorder_lost = True

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
        if (
            session.initial_mic_auth == "not_determined"
            and self.last_audio_error != "coreaudio_device_disconnected"
        ):
            post_st = self._query_live_audio_status(timeout_sec=0.6)
            hw_gone = (
                isinstance(post_st, Mapping)
                and bool(post_st.get("hal_ok", True))
                and not bool(post_st.get("has_input", True))
            )
            if not hw_gone and (perm_err := classify_macos_mic_permission(post_st, self.env)) is not None:
                self.last_audio_error = "macos_mic_permission"
                perm_err._raw_wav = wav  # type: ignore[attr-defined]
                perm_err._audio_source = "avfoundation"  # type: ignore[attr-defined]
                raise perm_err
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

    def close(self) -> None:
        super().close()
        with self._rec_cond:
            pending = [t for t in self._rec_cleanup_threads if t.is_alive()]
        deadline = time.monotonic() + 1.0
        cur = threading.current_thread()
        for th in pending:
            rem = deadline - time.monotonic()
            if rem <= 0.0:
                break
            if th is not cur and th.is_alive():
                th.join(timeout=rem)
        with self._rec_cond:
            self._rec_cleanup_threads = {t for t in self._rec_cleanup_threads if t.is_alive()}

    def switch_to_fallback_audio(self) -> bool:
        return False

    def _copy_to_clipboard(self, text: str) -> bool:
        pb_bin = self._which("pbcopy")
        if not pb_bin:
            return False
        exe = pb_bin if os.path.sep in pb_bin and Path(pb_bin).name == "pbcopy" else "pbcopy"
        try:
            p = subprocess.run(
                [exe], input=text, text=True, encoding="utf-8", env=self.env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2.0, check=False,
            )
            return p.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _is_focus_drifted(self, origin: ForegroundWindow | None, current: ForegroundWindow) -> bool:
        if origin is None:
            return False
        if origin.origin_drifted:
            return True
        if origin.bundle_id and current.bundle_id:
            if origin.bundle_id.lower() != current.bundle_id.lower():
                return True
        elif origin.process and current.process and origin.process.lower() != current.process.lower():
            return True
        if (origin.process or origin.bundle_id) and not (current.process or current.bundle_id):
            return True
        if origin.hwnd > 0 and current.hwnd > 0 and origin.hwnd != current.hwnd:
            return True
        if origin.surface_id and current.surface_id and origin.surface_id != current.surface_id:
            return True
        if (
            not self.is_allowed_terminal(origin)
            and (origin.process or origin.bundle_id)
            and origin.hwnd > 0
            and current.hwnd > 0
        ):
            return False
        if origin.tmux_pane:
            if not current.tmux_pane or origin.tmux_pane != current.tmux_pane:
                return True
            if (
                origin.tmux_socket
                and current.tmux_socket
                and not _same_tmux_server(origin.tmux_socket, current.tmux_socket)
            ):
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
            if not origin.tmux_pane and current.tmux_pane and origin.title and current.title:
                if _normalize_tmux_title_for_drift(origin.title) != _normalize_tmux_title_for_drift(current.title):
                    return True
            return False
        has_term_identity = bool(
            (origin.surface_id and current.surface_id)
            or (origin.tmux_pane and current.tmux_pane)
            or (orig_tty and cur_tty)
        )
        if (
            not has_term_identity
            and (origin.process or origin.bundle_id)
            and (
                origin.ax_trusted is not None
                or not ((origin.title or "").strip() and (current.title or "").strip())
            )
            and origin.hwnd <= 0
            and current.hwnd > 0
        ):
            return True
        if (origin.process or origin.bundle_id) and origin.title and not current.title and current.hwnd <= 0 and not cur_tty:
            return True
        if (origin.process or origin.bundle_id) and origin.title and current.title:
            o_hint = parse_window_title_hint(origin.title)
            c_hint = parse_window_title_hint(current.title)
            if o_hint is not None and c_hint is not None:
                return o_hint.session_token != c_hint.session_token
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
        probe_env = (
            {**self.env, "TMUX_TMPDIR": str(self._cache_dir)}
            if (self._cache_dir is not None and "TMUX_TMPDIR" not in self.env)
            else self.env
        )
        return probe_tmux_snapshot(env=probe_env, extra_sockets=extra_socks)

    def _resolve_window_tmux_target(
        self,
        win: ForegroundWindow,
        *,
        allow_pid_single_tty: bool = True,
        strict_tty: bool = False,
    ) -> tuple[str, str]:
        if (win.process or win.bundle_id) and not is_macos_terminal_window(win.process, win.bundle_id):
            return "", ""
        if self._tmux_probe_fn is None and not self._resolve_tmux_exe():
            return "", ""
        snap = self._probe_tmux_for_paste()
        if not snap.clients or not snap.panes:
            return "", ""
        target_tty = normalize_tty_path(win.tty)
        if not target_tty and allow_pid_single_tty and win.pid > 1:
            try:
                rows = SessionTracker(env=self.env)._list_darwin_user_ps_rows()
            except Exception:
                rows = None
            if rows:
                tab_ttys, desc_pids = collect_darwin_window_pty_tree(rows, win.pid)
                if len(tab_ttys) == 1:
                    target_tty = next(iter(tab_ttys))
                elif not tab_ttys and desc_pids:
                    desc_clients = [
                        c for c in snap.clients if c.client_pid > 1 and c.client_pid in desc_pids and c.client_tty
                    ]
                    if len(desc_clients) == 1:
                        target_tty = normalize_tty_path(desc_clients[0].client_tty)
        if target_tty:
            selected = SessionTracker._select_active_tmux_target(
                snap,
                win.title,
                window_tty=target_tty,
                window_pid=win.pid,
                allowed_client_ttys=(target_tty,),
            )
            if selected is not None:
                client, pane = selected
                if _tty_matches(client.client_tty, target_tty) and pane.pane_id:
                    return (pane.socket_path or client.socket_path), pane.pane_id
            return "", ""
        if not strict_tty and not win.surface_id and (raw_tmux := (self.env.get("TMUX") or "").strip()):
            env_sock = parse_tmux_socket_from_env(raw_tmux)
            scoped_snap = snap
            if env_sock:
                c_filt = tuple(
                    c for c in snap.clients if not c.socket_path or _same_tmux_server(c.socket_path, env_sock)
                )
                p_filt = tuple(
                    p for p in snap.panes if not p.socket_path or _same_tmux_server(p.socket_path, env_sock)
                )
                scoped_snap = TmuxServerSnapshot(clients=c_filt, panes=p_filt)
            selected = SessionTracker._select_active_tmux_target(
                scoped_snap,
                win.title,
                window_tty=target_tty,
                window_pid=win.pid,
            )
            if selected is not None:
                client, pane = selected
                if pane.pane_id:
                    return (pane.socket_path or client.socket_path or env_sock), pane.pane_id
        return "", ""

    def _enrich_window_tmux_target(self, win: ForegroundWindow) -> ForegroundWindow:
        if win.tmux_pane or (win.ax_trusted is not None and win.hwnd <= 0 and not win.tty):
            return win
        sock_path, pane_id = self._resolve_window_tmux_target(win)
        if not pane_id:
            return win
        return replace(win, tmux_socket=sock_path, tmux_pane=pane_id)

    def _try_deliver_attached_tmux(
        self,
        text: str,
        *,
        seq_id: int,
        eff_tty: str,
        fg: ForegroundWindow,
        allow_pid_single_tty: bool = False,
        origin_window: ForegroundWindow | None = None,
        before_commit: Callable[[], bool] | None = None,
    ) -> str:
        orig_pane = (origin_window.tmux_pane or "").strip() if origin_window is not None else ""
        orig_sock = (origin_window.tmux_socket or "").strip() if origin_window is not None else ""
        tmux_bin = self._resolve_tmux_exe()
        if not tmux_bin:
            return "focus_changed" if orig_pane else ""
        probe_win = replace(fg, tty=eff_tty) if eff_tty != fg.tty else fg
        sock_path, pane_id = self._resolve_window_tmux_target(
            probe_win,
            allow_pid_single_tty=allow_pid_single_tty,
            strict_tty=True,
        )
        if not pane_id:
            return "focus_changed" if orig_pane else ""
        if orig_pane:
            if pane_id != orig_pane or (
                orig_sock and sock_path and not _same_tmux_server(orig_sock, sock_path)
            ):
                return "focus_changed"
        if before_commit is not None and not before_commit():
            return "cancelled"
        cmd_prefix = [tmux_bin]
        if sock_path:
            cmd_prefix.extend(["-S", preferred_tmux_socket_arg(sock_path)])
        tmux_env = {k: v for k, v in self.env.items() if k not in ("TMUX", "TMUX_PANE")}
        pasted = self._deliver_via_tmux_buffer(
            cmd_prefix,
            text,
            seq_id=seq_id,
            target_pane=pane_id,
            tmux_env=tmux_env,
        )
        if pasted:
            return "tmux"
        return "tmux_paste_failed" if orig_pane else ""

    _PASTE_KEY_STATE_UNAVAIL_TIMEOUT_SEC: float = 4.0

    def _idle_paste_hold_timeout_sec(self) -> float:
        return max(5.0, (self.cfg.hotkey.effective_hold_cancel_ms() / 1000.0) + 2.0)

    def _effective_paste_hold_timeout_sec(self) -> float:
        if self._paste_hold_timeout_sec is not None:
            return max(0.0, float(self._paste_hold_timeout_sec))
        if self.cfg.hotkey.trigger_mode != "ptt":
            return self._idle_paste_hold_timeout_sec()
        return max(15.0, float(self.cfg.audio.max_duration_sec) + 2.0)

    def _query_shim_key_state(self, *, immediate: bool = False) -> bool | None:
        if not self._has_managed_shim():
            return None
        req: dict[str, Any] = {"cmd": "key_state"}
        if immediate:
            req["immediate"] = True
        try:
            res = self._shim_rpc(req, timeout_sec=0.8, allow_retry=False)
        except Exception:
            return None
        if not isinstance(res, dict) or res.get("ok") is not True:
            return None
        return bool(res.get("hotkey_held"))

    def _poll_shim_key_state(self, *, immediate: bool = False) -> bool | None:
        if immediate:
            try:
                return self._query_shim_key_state(immediate=True)
            except TypeError:
                return self._query_shim_key_state()
        return self._query_shim_key_state()

    def _query_paste_foreground_window(self) -> ForegroundWindow:
        fg = self.get_foreground_window()
        if (
            "get_foreground_window" in self.__dict__
            or type(self).get_foreground_window is not MacOSPlatform.get_foreground_window
        ) and not fg.tmux_pane:
            fg = self._enrich_window_tmux_target(fg)
        return fg

    def _deliver_to_ghostty_surface(
        self,
        text: str,
        surface_id: str,
        *,
        on_direct_success: Callable[[str, str], PasteOutcome],
        on_fallback: Callable[[str, str], PasteOutcome],
    ) -> PasteOutcome:
        p = self._run_osascript(["-e", _GHOSTTY_INPUT_SCRIPT, "--", surface_id, text], timeout=2.5)
        if p is None:
            return on_fallback("osascript_unavailable", "Ghostty")
        if p.returncode != 0:
            err_reason = _summarize_osascript_error(p.stderr or "", kind="ghostty")
            return on_fallback(err_reason, "Ghostty")
        verdict = p.stdout.strip()
        if verdict == "ok":
            return on_direct_success("ok", "Ghostty")
        cur_proc = self._query_frontmost_process_and_title()[0] if verdict == "not_frontmost" else "Ghostty"
        return on_fallback("focus_changed", cur_proc)

    def _shim_supports_paste_clip(self) -> bool:
        return self._has_managed_shim() and ("paste_clip" in self._shim_caps)

    def _shim_supports_key_state_immediate(self) -> bool:
        return self._has_managed_shim() and ("key_state_immediate" in self._shim_caps)

    def _populate_paste_expectations(
        self,
        req: dict[str, Any],
        *,
        origin_window: ForegroundWindow | None,
        expected_fg: ForegroundWindow | None,
    ) -> None:
        exp_bundle = (
            (origin_window.bundle_id if origin_window and origin_window.bundle_id else "")
            or (expected_fg.bundle_id if expected_fg and expected_fg.bundle_id else "")
        ).strip()
        exp_proc = (
            (origin_window.process if origin_window and origin_window.process else "")
            or (expected_fg.process if expected_fg and expected_fg.process else "")
        ).strip()
        exp_hwnd = (
            origin_window.hwnd
            if origin_window is not None and origin_window.hwnd > 0
            else (expected_fg.hwnd if expected_fg is not None and expected_fg.hwnd > 0 else 0)
        )
        if exp_bundle:
            req["expect_bundle_id"] = exp_bundle
        if exp_proc:
            req["expect_process"] = exp_proc
        if exp_hwnd > 0:
            req["expect_hwnd"] = exp_hwnd

    def _post_cmd_v(
        self,
        *,
        origin_window: ForegroundWindow | None = None,
        expected_fg: ForegroundWindow | None = None,
    ) -> tuple[bool, str, str]:
        cur_proc = expected_fg.process if expected_fg is not None else ""
        if self._has_managed_shim():
            req: dict[str, Any] = {"cmd": "paste_cmd_v"}
            self._populate_paste_expectations(req, origin_window=origin_window, expected_fg=expected_fg)
            try:
                res = self._shim_rpc(req, timeout_sec=1.0, allow_retry=False)
            except Exception:
                # Never fall back to osascript after an in-flight RPC error/timeout to prevent double paste.
                return False, "shim_paste_unconfirmed", cur_proc
            # _shim_graceful returns None on timeout/EOF/error; NEVER fall through to osascript!
            if isinstance(res, dict) and res.get("reason") == "key_held" and not res.get("posted"):
                res_proc = str(res.get("fg_process")) if "fg_process" in res else cur_proc
                return False, "key_held", res_proc
            if not isinstance(res, dict) or res.get("ok") is not True:
                return False, "shim_paste_unconfirmed", cur_proc
            if "posted" in res:
                res_proc = str(res.get("fg_process")) if "fg_process" in res else cur_proc
                if res.get("posted") is True:
                    return True, "ok", res_proc
                raw_reason = str(res.get("reason") or "cgevent_failed")
                if raw_reason == "accessibility_denied":
                    return (
                        False,
                        "accessibility_denied (grant Accessibility permission to your terminal app)",
                        res_proc,
                    )
                return False, raw_reason, res_proc

        p = self._run_osascript(["-e", _CMD_V_SCRIPT], timeout=2.5)
        if p is not None and p.returncode == 0:
            return True, "ok", cur_proc
        reason = (
            "osascript_unavailable"
            if p is None
            else _summarize_osascript_error(p.stderr or "", kind="system_events")
        )
        return False, reason, cur_proc

    def _deliver_cmd_v_via_shim_clip(
        self,
        text: str,
        *,
        origin_window: ForegroundWindow | None,
        expected_fg: ForegroundWindow,
        fallback_clipboard_text: str | None,
        is_beep_suppressed: Callable[[], bool],
    ) -> PasteOutcome:
        cur_proc = expected_fg.process
        req: dict[str, Any] = {"cmd": "paste_cmd_v", "text": text}
        if fallback_clipboard_text is not None and fallback_clipboard_text != text:
            req["fallback_text"] = fallback_clipboard_text
        self._populate_paste_expectations(req, origin_window=origin_window, expected_fg=expected_fg)
        res: dict[str, Any] | None = None
        try:
            res = self._shim_rpc(req, timeout_sec=1.5, allow_retry=False)
        except Exception:
            res = None
        if isinstance(res, dict) and res.get("reason") == "key_held" and not res.get("posted"):
            eff_fg_proc = str(res.get("fg_process")) if "fg_process" in res else cur_proc
            fb_txt = fallback_clipboard_text if fallback_clipboard_text is not None else text
            clip_ok = self._copy_to_clipboard(fb_txt)
            if clip_ok and not is_beep_suppressed():
                self.beep("focus")
            return PasteOutcome(
                False,
                clip_ok,
                "key_held" if clip_ok else "clipboard_failed",
                eff_fg_proc,
                used_clipboard_paste=False,
            )
        if (
            isinstance(res, dict)
            and res.get("ok") is True
            and "clipboard" in res
            and res.get("reason") != "shim_paste_unconfirmed"
        ):
            posted_ok = bool(res.get("posted"))
            clip_ok = bool(res.get("clipboard"))
            eff_fg_proc = str(res.get("fg_process")) if "fg_process" in res else cur_proc
            if posted_ok:
                return PasteOutcome(
                    True,
                    clip_ok,
                    "ok",
                    eff_fg_proc,
                    used_clipboard_paste=bool(posted_ok and clip_ok),
                )
            if not clip_ok:
                raw_reason = str(res.get("reason") or "clipboard_failed")
                return PasteOutcome(False, False, raw_reason, eff_fg_proc, used_clipboard_paste=False)
            raw_reason = str(res.get("reason") or "cgevent_failed")
            post_reason = (
                "accessibility_denied (grant Accessibility permission to your terminal app)"
                if raw_reason == "accessibility_denied"
                else raw_reason
            )
            if not is_beep_suppressed():
                self.beep("focus")
            return PasteOutcome(False, True, post_reason, eff_fg_proc, used_clipboard_paste=False)
        # RPC timed out or failed: NEVER send osascript Cmd+V (avoid double paste); ensure fallback text is on clipboard.
        fb_txt = fallback_clipboard_text if fallback_clipboard_text is not None else text
        clip_ok = self._copy_to_clipboard(fb_txt)
        if clip_ok and not is_beep_suppressed():
            self.beep("focus")
        return PasteOutcome(
            False,
            clip_ok,
            "shim_paste_unconfirmed" if clip_ok else "clipboard_failed",
            cur_proc,
            used_clipboard_paste=False,
        )

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
        before_commit: Callable[[], bool] | None = None,
        same_window_verifier: Callable[[ForegroundWindow], bool] | None = None,
    ) -> PasteOutcome:
        del target_hwnd, preserve_clipboard
        cancelled_fn = lambda: bool(is_cancelled is not None and is_cancelled())
        if cancelled_fn():
            return PasteOutcome(False, False, "cancelled", "")
        _confirm_commit = make_once_commit_guard(cancelled_fn, before_commit)

        def _verify_same_window(cur_fg: ForegroundWindow) -> bool:
            if same_window_verifier is None:
                return True
            try:
                return bool(same_window_verifier(cur_fg))
            except Exception:
                return False

        def _is_beep_suppressed() -> bool:
            return bool(suppress_beep() if callable(suppress_beep) else suppress_beep)

        def _fallback_to_clipboard(reason_if_ok: str, fg_proc: str, *, skip_settle: bool = False) -> PasteOutcome:
            if before_clipboard_write is not None and not skip_settle:
                before_clipboard_write()
            if not _confirm_commit():
                return PasteOutcome(False, False, "cancelled", fg_proc)
            fb_txt = fallback_clipboard_text if fallback_clipboard_text is not None else text
            clip_ok = self._copy_to_clipboard(fb_txt)
            if clip_ok and not _is_beep_suppressed():
                self.beep("focus")
            return PasteOutcome(
                False,
                clip_ok,
                reason_if_ok if clip_ok else "clipboard_failed",
                fg_proc,
                used_clipboard_paste=False,
            )

        def _direct_success(reason_ok: str, fg_proc: str) -> PasteOutcome:
            return PasteOutcome(True, False, reason_ok, fg_proc, used_clipboard_paste=False)

        if origin_window is not None and origin_window.origin_drifted:
            return _fallback_to_clipboard("focus_changed", origin_window.process or "")
        if origin_window is not None and origin_window.surface_id and not origin_window.tmux_pane:
            if not _confirm_commit():
                return PasteOutcome(False, False, "cancelled", "Ghostty")
            return self._deliver_to_ghostty_surface(
                text,
                origin_window.surface_id,
                on_direct_success=_direct_success,
                on_fallback=_fallback_to_clipboard,
            )
        unknown_origin = origin_window is None or not (
            origin_window.process
            or origin_window.bundle_id
            or origin_window.hwnd > 0
            or origin_window.surface_id
            or origin_window.tty
            or origin_window.tmux_pane
        )
        try:
            fg = self._query_paste_foreground_window()
        except Exception:
            if same_window_verifier is not None:
                return _fallback_to_clipboard("focus_changed", origin_window.process if origin_window else "")
            raise
        if (
            origin_window is None
            and fg.surface_id
            and not fg.tmux_pane
            and is_ghostty_app(fg.process, fg.bundle_id)
        ):
            if not _confirm_commit():
                return PasteOutcome(False, False, "cancelled", "Ghostty")
            return self._deliver_to_ghostty_surface(
                text,
                fg.surface_id,
                on_direct_success=_direct_success,
                on_fallback=_fallback_to_clipboard,
            )
        if (
            self._is_focus_drifted(origin_window, fg)
            or (
                unknown_origin
                and (fg.process or fg.bundle_id)
                and not self.is_allowed_terminal(fg)
            )
            or not _verify_same_window(fg)
        ):
            return _fallback_to_clipboard("focus_changed", fg.process)
        fg_is_term = self.is_allowed_terminal(fg)
        if fg_is_term and self.env.get("TMUX") and (tmux_exe := self._resolve_tmux_exe()):
            eff_sock = ""
            target_p = ""
            if origin_window is not None and origin_window.tmux_pane:
                cur_sock, cur_pane = self._resolve_window_tmux_target(fg)
                if (
                    not cur_pane
                    or cur_pane != origin_window.tmux_pane
                    or (
                        origin_window.tmux_socket
                        and cur_sock
                        and not _same_tmux_server(origin_window.tmux_socket, cur_sock)
                    )
                ):
                    return _fallback_to_clipboard("focus_changed", fg.process)
                eff_sock = cur_sock or origin_window.tmux_socket
                target_p = origin_window.tmux_pane
            if not _confirm_commit():
                return PasteOutcome(False, False, "cancelled", fg.process)
            cmd_prefix = [tmux_exe]
            if eff_sock:
                cmd_prefix.extend(["-S", preferred_tmux_socket_arg(eff_sock)])
            tmux_env = (
                {k: v for k, v in self.env.items() if k not in ("TMUX", "TMUX_PANE")}
                if eff_sock
                else {k: v for k, v in self.env.items() if k != "TMUX_PANE"}
            )
            if self._deliver_via_tmux_buffer(cmd_prefix, text, seq_id=seq_id, target_pane=target_p, tmux_env=tmux_env):
                return _direct_success("tmux", fg.process)
            if target_p:
                return _fallback_to_clipboard("tmux_paste_failed", fg.process)
        eff_tty = normalize_tty_path(fg.tty)
        if fg_is_term and (eff_tty or fg.pid > 1 or (origin_window is not None and origin_window.tmux_pane)):
            tmux_out = self._try_deliver_attached_tmux(
                text,
                seq_id=seq_id,
                eff_tty=eff_tty,
                fg=fg,
                allow_pid_single_tty=not bool(eff_tty),
                origin_window=origin_window,
                before_commit=_confirm_commit,
            )
            if tmux_out == "cancelled":
                return PasteOutcome(False, False, "cancelled", fg.process)
            if tmux_out == "tmux":
                return _direct_success("tmux", fg.process)
            if tmux_out in ("focus_changed", "tmux_paste_failed"):
                return _fallback_to_clipboard(tmux_out, fg.process)

        waited_for_settle = False
        if before_clipboard_write is not None:
            t_settle0 = time.monotonic()
            settle_ret = before_clipboard_write()
            waited_for_settle = bool(settle_ret) or (time.monotonic() - t_settle0) >= 0.001
        if cancelled_fn():
            return PasteOutcome(False, False, "cancelled", fg.process)

        waited_for_key = False
        effective_deadline_fn: Callable[[float], float] | None = None
        hold_timeout_sec = 0.0
        if self._has_managed_shim():
            hold_timeout_sec = self._effective_paste_hold_timeout_sec()
            hold_deadline = time.monotonic() + hold_timeout_sec
            ptt_dynamic_idle = (
                self._paste_hold_timeout_sec is None
                and self.cfg.hotkey.trigger_mode == "ptt"
            )
            idle_timeout = self._idle_paste_hold_timeout_sec() if ptt_dynamic_idle else 0.0
            idle_since: float | None = None
            unavail_since: float | None = None

            def _effective_deadline(now_ts: float) -> float:
                nonlocal idle_since
                eff = hold_deadline
                if ptt_dynamic_idle:
                    if self._rec_active:
                        idle_since = None
                    else:
                        if idle_since is None:
                            idle_since = now_ts
                        eff = min(eff, idle_since + idle_timeout)
                if unavail_since is not None:
                    eff = min(eff, unavail_since + self._PASTE_KEY_STATE_UNAVAIL_TIMEOUT_SEC)
                return eff

            effective_deadline_fn = _effective_deadline

            while True:
                if cancelled_fn():
                    return PasteOutcome(False, False, "cancelled", fg.process)
                if not self._has_managed_shim():
                    return _fallback_to_clipboard("focus_changed", fg.process, skip_settle=True)
                poll_started = time.monotonic()
                if waited_for_key and (_effective_deadline(poll_started) - poll_started) <= 0.0:
                    return _fallback_to_clipboard("focus_changed", fg.process, skip_settle=True)
                key_state = self._poll_shim_key_state(immediate=False)
                if cancelled_fn():
                    return PasteOutcome(False, False, "cancelled", fg.process)
                if not self._has_managed_shim():
                    return _fallback_to_clipboard("focus_changed", fg.process, skip_settle=True)
                if key_state is False:
                    needs_immediate = self._shim_supports_key_state_immediate()
                    if not needs_immediate:
                        unavail_since = None
                    now_flush = time.monotonic()
                    eff_deadline = _effective_deadline(now_flush)
                    rem_flush = eff_deadline - now_flush
                    if (waited_for_key or hold_timeout_sec > 0.0) and rem_flush <= 0.0:
                        return _fallback_to_clipboard("focus_changed", fg.process, skip_settle=True)
                    flush_wait = min(0.05, max(0.005, rem_flush)) if rem_flush > 0.0 else 0.005
                    flushed = self._flush_shim_events(timeout_sec=flush_wait)
                    if cancelled_fn():
                        return PasteOutcome(False, False, "cancelled", fg.process)
                    if not flushed:
                        waited_for_key = True
                        now_after_flush = time.monotonic()
                        rem = _effective_deadline(now_after_flush) - now_after_flush
                        if rem <= 0.0:
                            return _fallback_to_clipboard("focus_changed", fg.process, skip_settle=True)
                        if (now_after_flush - now_flush) < 0.005:
                            sleep_cooperative(min(0.05, rem), sleep_fn=time.sleep, is_cancelled=cancelled_fn)
                        continue
                    now_post_flush = time.monotonic()
                    if (waited_for_key or hold_timeout_sec > 0.0) and (_effective_deadline(now_post_flush) - now_post_flush) <= 0.0:
                        return _fallback_to_clipboard("focus_changed", fg.process, skip_settle=True)
                    if needs_immediate:
                        imm_started = now_post_flush
                        imm_state = self._poll_shim_key_state(immediate=True)
                        if cancelled_fn():
                            return PasteOutcome(False, False, "cancelled", fg.process)
                        if not self._has_managed_shim():
                            return _fallback_to_clipboard("focus_changed", fg.process, skip_settle=True)
                        if imm_state is False:
                            unavail_since = None
                            now_after_imm = time.monotonic()
                            if (waited_for_key or hold_timeout_sec > 0.0) and (_effective_deadline(now_after_imm) - now_after_imm) <= 0.0:
                                return _fallback_to_clipboard("focus_changed", fg.process, skip_settle=True)
                            break
                        now_imm_fail = time.monotonic()
                        if imm_state is None:
                            if unavail_since is None:
                                unavail_since = imm_started
                        else:
                            unavail_since = None
                        rem = _effective_deadline(now_imm_fail) - now_imm_fail
                        if rem <= 0.0:
                            return _fallback_to_clipboard("focus_changed", fg.process, skip_settle=True)
                        waited_for_key = True
                        sleep_cooperative(min(0.05, rem), sleep_fn=time.sleep, is_cancelled=cancelled_fn)
                        continue
                    break
                now_poll = time.monotonic()
                if key_state is None:
                    if unavail_since is None:
                        unavail_since = poll_started
                else:
                    unavail_since = None
                rem = _effective_deadline(now_poll) - now_poll
                if rem <= 0.0:
                    return _fallback_to_clipboard("focus_changed", fg.process, skip_settle=True)
                waited_for_key = True
                sleep_cooperative(min(0.05, rem), sleep_fn=time.sleep, is_cancelled=cancelled_fn)

        if cancelled_fn():
            return PasteOutcome(False, False, "cancelled", fg.process)
        if waited_for_settle or waited_for_key or same_window_verifier is not None:
            try:
                fg = self._query_paste_foreground_window()
            except Exception:
                if same_window_verifier is not None:
                    return _fallback_to_clipboard("focus_changed", fg.process, skip_settle=True)
                raise
            if (
                self._is_focus_drifted(origin_window, fg)
                or (
                    unknown_origin
                    and (fg.process or fg.bundle_id)
                    and not self.is_allowed_terminal(fg)
                )
                or not _verify_same_window(fg)
            ):
                return _fallback_to_clipboard("focus_changed", fg.process, skip_settle=True)

        if effective_deadline_fn is not None and (waited_for_key or hold_timeout_sec > 0.0):
            now_pre_commit = time.monotonic()
            if now_pre_commit >= effective_deadline_fn(now_pre_commit):
                return _fallback_to_clipboard("focus_changed", fg.process, skip_settle=True)

        if not _confirm_commit():
            return PasteOutcome(False, False, "cancelled", fg.process)
        if self._shim_supports_paste_clip():
            return self._deliver_cmd_v_via_shim_clip(
                text,
                origin_window=origin_window,
                expected_fg=fg,
                fallback_clipboard_text=fallback_clipboard_text,
                is_beep_suppressed=_is_beep_suppressed,
            )
        clip_ok = self._copy_to_clipboard(text)
        if not clip_ok:
            return PasteOutcome(False, False, "clipboard_failed", fg.process)
        posted_ok, post_reason, eff_fg_proc = self._post_cmd_v(
            origin_window=origin_window,
            expected_fg=fg,
        )
        if posted_ok:
            return PasteOutcome(True, True, "ok", eff_fg_proc, used_clipboard_paste=True)
        if fallback_clipboard_text is not None and fallback_clipboard_text != text:
            clip_ok = self._copy_to_clipboard(fallback_clipboard_text)
        if not clip_ok:
            return PasteOutcome(False, False, "clipboard_failed", eff_fg_proc, used_clipboard_paste=False)
        if not _is_beep_suppressed():
            self.beep("focus")
        return PasteOutcome(False, True, post_reason, eff_fg_proc, used_clipboard_paste=False)
