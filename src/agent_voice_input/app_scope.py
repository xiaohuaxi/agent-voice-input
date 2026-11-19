"""Window category classification, remote-desktop yielding, and paste-chord routing."""
from __future__ import annotations

from dataclasses import dataclass
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


AI_CHAT_PATTERNS: tuple[str, ...] = (
    "com.openai.chat", "chatgpt", "com.anthropic.claudefordesktop",
    "ai.perplexity.mac", "perplexity",
    "cherry studio", "cherry-studio", "cherrystudio",
    "chatbox", "xyz.chatboxapp.app",
)

IM_CHAT_PATTERNS: tuple[str, ...] = (
    "com.tencent.xinwechat", "com.electron.lark", "com.bytedance.lark",
    "com.alibaba.dingtalkmac", "com.tinyspeck.slackmacgap", "com.hnc.discord",
    "ru.keepcoder.telegram", "com.tdesktop.telegram", "net.whatsapp.whatsapp",
    "com.microsoft.teams2", "com.microsoft.teams", "com.apple.mobilesms", "com.tencent.qq",
    "wechat", "weixin", "微信", "feishu", "lark", "飞书", "dingtalk", "钉钉",
    "slack", "discord", "telegram", "whatsapp", "ms-teams", "teams", "messages", "qq",
)

_ANTIGRAVITY_IDE_PATTERNS: tuple[str, ...] = ("antigravity", "com.google.antigravity*")

_VSCODE_FAMILY_PATTERNS: tuple[str, ...] = (
    "code", "code - insiders", "vscodium", "cursor", "windsurf", "antigravity", "trae", "kiro",
    "visual studio code", "com.microsoft.vscode", "com.microsoft.vscodeinsiders", "com.vscodium",
    "com.todesktop.230313mzl4w4u92", "com.exafunction.windsurf", "com.google.antigravity*",
)
_ZED_PATTERNS: tuple[str, ...] = ("zed", "dev.zed.zed*")
_JETBRAINS_PATTERNS: tuple[str, ...] = (
    "idea*", "pycharm*", "goland*", "webstorm*", "clion*", "rustrover*", "rider*",
    "phpstorm*", "datagrip*", "rubymine*", "studio*", "android studio",
    "com.jetbrains.*", "com.google.android.studio",
)
_XCODE_PATTERNS: tuple[str, ...] = ("xcode", "com.apple.dt.xcode")
_SUBLIME_PATTERNS: tuple[str, ...] = ("sublime_text", "sublime text", "com.sublimetext.*")

_TITLE_SEPS: tuple[str, ...] = (" — ", " – ", " - ")

# Sorted longest-first so "Visual Studio Code - Insiders" strips before "Code".
_VSCODE_APP_SUFFIXES: tuple[str, ...] = (
    "visual studio code - insiders", "code - insiders", "visual studio code",
    "antigravity", "vscodium", "windsurf", "cursor", "trae", "kiro", "code",
)
_VSCODE_REJECT_TITLES: frozenset[str] = frozenset({
    *_VSCODE_APP_SUFFIXES, "welcome", "settings", "preferences", "get started",
    "extensions", "keyboard shortcuts", "release notes", "walkthrough",
    "agent manager", "empty window", "new window", "new tab",
})
_JETBRAINS_SUFFIXES: tuple[str, ...] = (
    "intellij idea", "android studio", "rustrover", "webstorm", "phpstorm",
    "datagrip", "rubymine", "pycharm", "goland", "clion", "rider",
)


def _is_windows_claude_desktop_title(title: str) -> bool:
    t = (title or "").strip()
    if not t or any(ch == "\u2733" or "\u2800" <= ch <= "\u28ff" for ch in t):
        return False
    t_low = t.lower()
    if any(tag in t_low for tag in ("[#", "[claude:", "[codex:", "[agy:")):
        return False
    return t_low == "claude" or t_low.endswith((" - claude", " — claude", " – claude"))


def classify_target_app_hint(win: WindowLike) -> str:
    """Return ``'ai_chat' | 'im_chat' | ''`` for a ``general_gui`` foreground window (regex-free)."""
    if match_app_patterns(win, AI_CHAT_PATTERNS):
        return "ai_chat"
    if normalize_app_token(win.process) == "claude" and not (win.bundle_id or "").strip():
        return "ai_chat" if _is_windows_claude_desktop_title(win.title) else ""
    return "im_chat" if match_app_patterns(win, IM_CHAT_PATTERNS) else ""


def is_antigravity_ide_window(win: WindowLike) -> bool:
    return match_app_patterns(win, _ANTIGRAVITY_IDE_PATTERNS)


@dataclass(frozen=True)
class IdeTitleInfo:
    root_name: str = ""
    explicit_path: str = ""
    remote_kind: str = ""  # "" | "wsl" | "container" | "remote"


def _strip_dirty_prefix(title: str) -> str:
    s = (title or "").strip()
    while len(s) > 1 and (s.startswith(("● ", "• ", "* ")) or s[0] in ("●", "•")):
        s = s[1:].lstrip()
    return s


def _strip_trailing_app_suffix(title: str, suffixes: tuple[str, ...]) -> str:
    s = title.strip()
    low = s.lower()
    for admin_tag in (" [administrator]", " (admin)"):
        if low.endswith(admin_tag):
            s = s[: -len(admin_tag)].rstrip()
            low = s.lower()
    for app in suffixes:
        for sep in _TITLE_SEPS:
            tail = sep + app
            if low.endswith(tail):
                return s[: -len(tail)].strip()
    return s


def _split_ide_segments(text: str) -> list[str]:
    for sep in _TITLE_SEPS:
        if sep in text:
            return [p.strip() for p in text.split(sep) if p.strip()]
    return [text.strip()] if text.strip() else []


def _looks_like_single_filename(seg: str) -> bool:
    if "." not in seg or seg.startswith("."):
        return False
    stem, ext = seg.rsplit(".", 1)
    return bool(stem and 1 <= len(ext) <= 7 and ext[0].isalpha() and ext.isalnum())


def _classify_remote_label(label: str) -> str:
    low = label.strip().lower()
    if low == "wsl" or low.startswith(("wsl:", "wsl ")):
        return "wsl"
    if low.startswith(("dev container", "devcontainer", "container")):
        return "container"
    if low.startswith(("ssh", "codespaces", "tunnel", "remote")):
        return "remote"
    return ""


def _extract_trailing_remote_tag(cand: str) -> tuple[str, str]:
    if cand.endswith("]") and " [" in cand:
        base, bracket = cand.rsplit(" [", 1)
        if rkind := _classify_remote_label(bracket[:-1]):
            return base.strip(), rkind
    return cand, ""


def _looks_like_explicit_path(s: str) -> bool:
    return s.startswith(("~/", "/", "\\\\", "//")) or (
        len(s) >= 3 and s[0].isalpha() and s[1] == ":" and s[2] in ("/", "\\")
    )


def _parse_leading_root_title(
    raw: str, suffixes: tuple[str, ...], rejects: frozenset[str]
) -> IdeTitleInfo | None:
    segs = _split_ide_segments(_strip_trailing_app_suffix(raw, suffixes))
    if not segs:
        return None
    cand, remote_kind = _extract_trailing_remote_tag(segs[0])
    cand_low = cand.lower()
    if not cand or cand_low in rejects or cand_low.startswith("untitled"):
        return None
    if len(segs) == 1 and not remote_kind and _looks_like_single_filename(cand):
        return None
    return IdeTitleInfo(root_name=cand, remote_kind=remote_kind)


def parse_ide_window_title(win: WindowLike) -> IdeTitleInfo | None:
    """Extract workspace folder name / explicit path from an IDE window title (zero disk I/O)."""
    raw = _strip_dirty_prefix(win.title or "")
    if not raw:
        return None

    if match_app_patterns(win, _VSCODE_FAMILY_PATTERNS):
        segs = _split_ide_segments(_strip_trailing_app_suffix(raw, _VSCODE_APP_SUFFIXES))
        if not segs:
            return None
        remote_kind = ""
        if len(segs) >= 2 and (tail_rkind := _classify_remote_label(segs[-1].strip("[]"))):
            remote_kind = tail_rkind
            segs = segs[:-1]
        cand, tag_rkind = _extract_trailing_remote_tag(segs[-1])
        remote_kind = tag_rkind or remote_kind
        cand_low = cand.lower()
        if (
            not cand
            or cand_low.endswith(" (workspace)")
            or cand_low in _VSCODE_REJECT_TITLES
            or cand_low.startswith("untitled")
            or (len(segs) == 1 and not remote_kind and _looks_like_single_filename(cand))
        ):
            return None
        return IdeTitleInfo(root_name=cand, remote_kind=remote_kind)

    if match_app_patterns(win, _ZED_PATTERNS):
        return _parse_leading_root_title(
            raw, ("zed preview", "zed"), frozenset({"zed", "zed preview", "empty project", "welcome", "settings"})
        )

    if match_app_patterns(win, _JETBRAINS_PATTERNS):
        segs = _split_ide_segments(_strip_trailing_app_suffix(raw, _JETBRAINS_SUFFIXES))
        if not segs:
            return None
        explicit_path = ""
        cleaned_segs: list[str] = []
        for seg in segs:
            if seg.endswith("]") and ("[" in seg):
                head, br = ("", seg[1:-1]) if seg.startswith("[") else seg.rsplit(" [", 1) if " [" in seg else (seg, "")
                inner = br.rstrip("]").strip()
                if inner and _looks_like_explicit_path(inner):
                    explicit_path = explicit_path or inner
                    seg = head.strip()
            if seg:
                cleaned_segs.append(seg)
        root_name = cleaned_segs[0] if cleaned_segs else ""
        r_low = root_name.lower()
        if r_low in (*_JETBRAINS_SUFFIXES, "settings", "preferences", "tip of the day") or r_low.startswith(
            ("welcome to ", "untitled")
        ):
            root_name = ""
        return IdeTitleInfo(root_name=root_name, explicit_path=explicit_path) if (root_name or explicit_path) else None

    if match_app_patterns(win, _XCODE_PATTERNS):
        return _parse_leading_root_title(
            raw,
            ("xcode",),
            frozenset({"xcode", "welcome to xcode", "settings", "preferences", "organizer", "devices and simulators"}),
        )

    if match_app_patterns(win, _SUBLIME_PATTERNS):
        body = raw[: -len(" (UNREGISTERED)")].rstrip() if raw.upper().endswith(" (UNREGISTERED)") else raw
        segs = _split_ide_segments(_strip_trailing_app_suffix(body, ("sublime text",)))
        if not segs:
            return None
        first, root_name = segs[0], ""
        if first.endswith(")") and " (" in first:
            head, paren = first.rsplit(" (", 1)
            proj = paren[:-1].strip()
            if proj and not proj.lower().startswith("unregistered"):
                first, root_name = head.strip(), proj
        if _looks_like_explicit_path(first):
            return IdeTitleInfo(root_name=root_name, explicit_path=first)
        if not root_name and len(segs) >= 2:
            cand = segs[-1]
            if cand.lower() not in ("sublime text", "untitled") and not _looks_like_single_filename(cand):
                root_name = cand
        return IdeTitleInfo(root_name=root_name) if root_name else None

    return None
