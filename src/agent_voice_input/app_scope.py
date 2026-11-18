"""Window category classification, remote-desktop yielding, and paste-chord routing."""
from __future__ import annotations

from typing import Callable, Iterable, Literal, Protocol

WindowCategory = Literal["terminal", "coding_gui", "general_gui"]


class WindowLike(Protocol):
    process: str
    bundle_id: str
    title: str


def normalize_app_token(raw: str) -> str:
    """Lowercase and strip trailing ``.exe`` / ``.app`` suffixes."""
    s = (raw or "").strip().lower()
    for suf in (".exe", ".app"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    return s.strip()


def match_app_patterns(win: WindowLike, patterns: Iterable[str]) -> bool:
    """Case-insensitive exact or trailing-``*`` prefix match against process and bundle_id (regex-free)."""
    proc = normalize_app_token(win.process)
    bid = (win.bundle_id or "").strip().lower()
    if not proc and not bid:
        return False
    for raw in patterns:
        raw_low = (raw or "").strip().lower()
        proc_pat = normalize_app_token(raw)
        if not raw_low:
            continue
        if raw_low.endswith("*"):
            raw_prefix = raw_low.rstrip("*")
            proc_prefix = proc_pat.rstrip("*")
            if bid and raw_prefix and bid.startswith(raw_prefix):
                return True
            if proc and proc_prefix and proc.startswith(proc_prefix):
                return True
        else:
            if (bid and raw_low == bid) or (proc and proc_pat and proc_pat == proc):
                return True
    return False


# Built-in remote desktop / streaming / VM client list.
# REDLINE: Bare "msrdc" MUST NEVER be added here because Windows 11 WSLg renders local
# Linux GUI apps under msrdc.exe! ("msrdcw" is only the AVD launcher feed, not the session).
REMOTE_CLIENT_PATTERNS: tuple[str, ...] = (
    # Windows process names (stripped of .exe)
    "mstsc",
    "vmconnect",
    "vmware",
    "vmplayer",
    "virtualboxvm",
    "parsecd",
    "moonlight",
    "rustdesk",
    "anydesk",
    "teamviewer",
    "todesk",
    "sunloginclient",
    "wfica32",
    "cdviewer",
    "vncviewer",
    "tvnviewer",
    # macOS bundle IDs (primary, locale-independent)
    "com.microsoft.rdc.macos",
    "com.microsoft.rdc.osx.beta",
    "com.microsoft.rdc.mac",
    "com.apple.screensharing",
    "com.realvnc.vncviewer",
    "com.edovia.screens.5",
    "com.edovia.screens4.mac",
    "com.nomachine.nxdock",
    "tv.parsec.www",
    "com.moonlight-stream.moonlight",
    "com.carriez.rustdesk",
    "com.philandro.anydesk",
    "com.teamviewer.teamviewer",
    "com.youqu.todesk.mac",
    "com.oray.sunlogin.macclient",
    "com.vmware.fusion",
    "com.parallels.desktop.console",
    "com.parallels.winapp.*",
    "com.utmapp.utm",
    "org.virtualbox.app.virtualboxvm",
    "com.citrix.receiver.icaviewer.mac",
    "com.citrix.xenappviewer",
    "com.vmware.horizon",
    "com.omnissa.horizon.client.mac",
    "com.amazon.workspaces",
    # macOS localized process fallbacks when bundle_id is unavailable
    "windows app",
    "microsoft remote desktop",
    "screen sharing",
    "屏幕共享",
    "parsec",
    "vmware fusion",
    "parallels desktop",
    "citrix viewer",
    "utm",
)

CODING_GUI_PATTERNS: tuple[str, ...] = (
    # Windows IDE / GUI code editor process names
    "code",
    "code - insiders",
    "vscodium",
    "cursor",
    "windsurf",
    "antigravity",
    "zed",
    "trae",
    "kiro",
    "devenv",
    "sublime_text",
    "neovide",
    "emacs",
    "gvim",
    "idea64",
    "idea",
    "pycharm64",
    "pycharm",
    "goland64",
    "webstorm64",
    "clion64",
    "rustrover64",
    "rider64",
    "phpstorm64",
    "datagrip64",
    "rubymine64",
    "studio64",
    # macOS IDE bundle IDs
    "com.microsoft.vscode",
    "com.microsoft.vscodeinsiders",
    "com.vscodium",
    "com.todesktop.230313mzl4w4u92",  # Cursor
    "com.exafunction.windsurf",
    "com.google.antigravity*",
    "dev.zed.zed",
    "dev.zed.zed-preview",
    "com.jetbrains.*",
    "com.google.android.studio",
    "com.apple.dt.xcode",
    "com.sublimetext.*",
    "org.vim.macvim",
    "com.neovide.neovide",
    "org.gnu.emacs",
    # macOS process fallbacks when bundle_id is unavailable
    "visual studio code",
    "xcode",
    "sublime text",
    "macvim",
    "android studio",
)

WINDOWS_SHIFT_INSERT_PROCS: frozenset[str] = frozenset(
    {
        "mintty",   # Git Bash / MSYS2 / Cygwin (ClipShortcuts=yes by default; Ctrl+V sends literal ^V)
        "putty",    # PuTTY (Shift+Insert default)
        "kitty",    # Windows KiTTY (PuTTY fork, NOT Kovid Goyal's kitty; Shift+Insert default)
        "emacs",
    }
)

WINDOWS_CTRL_SHIFT_V_PROCS: frozenset[str] = frozenset(
    {
        "alacritty",
        "wezterm",
        "wezterm-gui",
        "rio",
        "ghostty",
        "tabby",    # Tabby default paste on Windows is Ctrl+Shift+V (Ctrl+V unbound by default)
        "hyper",    # Vercel Hyper win32.json maps editor:paste to ctrl+shift+v
    }
)


def is_remote_client_window(win: WindowLike) -> bool:
    return match_app_patterns(win, REMOTE_CLIENT_PATTERNS)


def classify_window_category(
    win: WindowLike, *, is_terminal: Callable[[WindowLike], bool]
) -> WindowCategory:
    if is_terminal(win):
        return "terminal"
    if match_app_patterns(win, CODING_GUI_PATTERNS):
        return "coding_gui"
    return "general_gui"


def resolve_windows_paste_chord(process: str) -> str:
    p = normalize_app_token(process)
    if p in WINDOWS_SHIFT_INSERT_PROCS:
        return "shift_insert"
    if p in WINDOWS_CTRL_SHIFT_V_PROCS:
        return "ctrl_shift_v"
    return "ctrl_v"
