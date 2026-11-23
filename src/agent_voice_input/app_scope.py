"""Window category classification, remote-desktop yielding, and paste-chord routing."""
from __future__ import annotations

from dataclasses import dataclass
import sys
from typing import Callable, Iterable, Literal, Protocol

WindowCategory = Literal["terminal", "coding_gui", "general_gui"]
HotwordsScope = Literal["global", "chat", "coding"]


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

GHOSTTY_BUNDLE_ID = "com.mitchellh.ghostty"
APPLE_TERMINAL_BUNDLE_ID = "com.apple.terminal"
ITERM2_BUNDLE_ID = "com.googlecode.iterm2"

WINDOWS_TERMINALS: frozenset[str] = frozenset(
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
        "warp",
    }
)

MACOS_TERMINALS: frozenset[str] = frozenset(
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

MACOS_TERMINAL_BUNDLE_IDS: frozenset[str] = frozenset(
    {
        GHOSTTY_BUNDLE_ID,
        APPLE_TERMINAL_BUNDLE_ID,
        ITERM2_BUNDLE_ID,
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


def is_windows_terminal_process(process: str) -> bool:
    p = (process or "").strip().lower()
    if p.endswith(".exe"):
        p = p[:-4]
    return bool(p) and p in WINDOWS_TERMINALS


WINDOWS_SHIFT_INSERT_PROCS: frozenset[str] = frozenset(
    {
        "mintty",     # Git Bash / MSYS2 / Cygwin (ClipShortcuts=yes by default; Ctrl+V sends literal ^V)
        "putty",      # PuTTY (Shift+Insert default)
        "kitty",      # Windows KiTTY (PuTTY fork, NOT Kovid Goyal's kitty; Shift+Insert default)
        "mobaxterm",  # MobaXterm SSH terminal (Shift+Insert default)
        "xshell",     # NetSarang Xshell (Shift+Insert default)
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
        "warp",       # Warp on Windows/Linux maps Terminal:Paste to Ctrl+Shift+V by default
        "tabby",      # Tabby default paste on Windows is Ctrl+Shift+V (Ctrl+V unbound by default)
        "hyper",      # Vercel Hyper win32.json maps editor:paste to ctrl+shift+v
        "windterm",   # WindTerm SSH client
        "securecrt",  # VanDyke SecureCRT
        "termius",    # Termius SSH client
        "electerm",   # Electerm SSH client
    }
)

# Dedicated SSH / remote-terminal GUI clients (always remote sessions; never bind local /proc or tmux).
# Note: bare "kitty" on macOS (net.kovidgoyal.kitty) is a local GPU terminal, whereas "kitty.exe" on Windows is KiTTY (PuTTY fork).
DEDICATED_SSH_CLIENT_PATTERNS: tuple[str, ...] = (
    "putty",
    "mobaxterm",
    "windterm",
    "securecrt",
    "xshell",
    "termius",
    "finalshell",
    "electerm",
    "royalts*",
    "com.termius*",
    "com.vandyke.securecrt*",
    "com.lemonmojo.royaltsx*",
    "sh.blink.blinkshell",
)


def is_dedicated_ssh_client_window(win: WindowLike, *, is_macos: bool | None = None) -> bool:
    """Return True when ``win`` is a dedicated SSH client (e.g. PuTTY, KiTTY on Windows, MobaXterm, WindTerm, SecureCRT)."""
    if match_app_patterns(win, DEDICATED_SSH_CLIENT_PATTERNS):
        return True
    raw_proc = (win.process or "").strip().lower()
    if normalize_app_token(raw_proc) == "kitty":
        bid = (win.bundle_id or "").strip().lower()
        if bid == "net.kovidgoyal.kitty":
            return False
        eff_mac = (sys.platform == "darwin") if is_macos is None else bool(is_macos)
        if raw_proc.endswith(".exe") or (not bid and not eff_mac):
            return True
    return False


def is_remote_client_window(win: WindowLike) -> bool:
    return match_app_patterns(win, REMOTE_CLIENT_PATTERNS)


def classify_window_category(
    win: WindowLike, *, is_terminal: Callable[[WindowLike], bool]
) -> WindowCategory:
    if is_terminal(win) or is_dedicated_ssh_client_window(win):
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
    "com.openai.chat", "com.openai.codex", "chatgpt", "com.anthropic.claudefordesktop",
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


_DESKTOP_AI_COLLIDING_PROCS: dict[str, tuple[str, ...]] = {
    "claude": ("claude",),
    "codex": ("codex", "chatgpt"),
}
_DESKTOP_TITLE_DASHES: tuple[str, ...] = ("-", "\u2013", "\u2014")
_HEX_CHARS: frozenset[str] = frozenset("0123456789abcdef")


def _is_hex_session_token(tok: str, *, min_len: int) -> bool:
    if len(tok) < min_len or len(tok) > 36:
        return False
    hex_n = 0
    for ch in tok:
        if ch in _HEX_CHARS:
            hex_n += 1
        elif ch != "-":
            return False
    return hex_n >= min_len


def _has_cli_session_tag(t_low: str) -> bool:
    """Detect real CLI session tokens like ``[#abcd1234]``, ``[§abcdef]``, ``[claude:<hex>]``, ``[codex:<hex>]``, ``[agy:<hex>]``."""
    pos = 0
    n = len(t_low)
    while pos < n:
        lb = t_low.find("[", pos)
        if lb < 0:
            return False
        rb = t_low.find("]", lb + 1)
        if rb < 0:
            return False
        body = t_low[lb + 1 : rb]
        if body.startswith(("#", "\u00a7")) and _is_hex_session_token(body[1:], min_len=6):
            return True
        for pfx in ("claude:", "codex:", "agy:"):
            if body.startswith(pfx):
                rest = body[len(pfx) :]
                if rest.startswith("#"):
                    rest = rest[1:]
                if _is_hex_session_token(rest, min_len=6):
                    return True
        pos = lb + 1
    return False


def _matches_desktop_app_suffix(t_low: str, name: str) -> bool:
    if t_low == name:
        return True
    if not t_low.endswith(name):
        return False
    prefix = t_low[: -len(name)].rstrip()
    if not prefix or not prefix.endswith(_DESKTOP_TITLE_DASHES):
        return False
    return bool(prefix[:-1].rstrip())


def _is_desktop_ai_app_title(title: str, app_names: tuple[str, ...]) -> bool:
    t = (title or "").strip()
    if not t or t[0] == "\u2733" or "\u2800" <= t[0] <= "\u28ff":
        return False
    t_low = t.lower()
    if _has_cli_session_tag(t_low):
        return False
    return any(_matches_desktop_app_suffix(t_low, name) for name in app_names)


def classify_target_app_hint(win: WindowLike) -> str:
    """Return ``'ai_chat' | 'im_chat' | ''`` for a ``general_gui`` foreground window (regex-free)."""
    if match_app_patterns(win, AI_CHAT_PATTERNS):
        return "ai_chat"
    if not (win.bundle_id or "").strip():
        app_names = _DESKTOP_AI_COLLIDING_PROCS.get(normalize_app_token(win.process))
        if app_names is not None:
            return "ai_chat" if _is_desktop_ai_app_title(win.title, app_names) else ""
    return "im_chat" if match_app_patterns(win, IM_CHAT_PATTERNS) else ""


def resolve_hotwords_scope(
    win: WindowLike | None = None,
    *,
    window_category: str = "",
    target_hint: str = "",
    mode: str = "",
    has_agent_session: bool = False,
    is_terminal: Callable[[WindowLike], bool] | None = None,
) -> HotwordsScope:
    """Map foreground window classification, target hint, mode, or Agent session to a hotwords scene."""
    eff_mode = (mode or "").strip().lower()
    eff_hint = (target_hint or "").strip().lower()
    if eff_mode == "general":
        if eff_hint in ("im_chat", "ai_chat"):
            return "chat"
        if win is not None and (win.process or win.bundle_id):
            if classify_target_app_hint(win) in ("im_chat", "ai_chat"):
                return "chat"
        return "global"
    if has_agent_session or eff_mode == "agent":
        return "coding"
    if eff_hint in ("im_chat", "ai_chat"):
        return "chat"
    eff_cat = (window_category or "").strip().lower()
    if eff_cat in ("terminal", "coding_gui"):
        return "coding"
    if win is not None and (win.process or win.bundle_id):
        if not eff_cat and is_terminal is not None:
            eff_cat = classify_window_category(win, is_terminal=is_terminal)
        if eff_cat in ("terminal", "coding_gui"):
            return "coding"
        if not eff_cat and match_app_patterns(win, CODING_GUI_PATTERNS):
            return "coding"
        if classify_target_app_hint(win) in ("im_chat", "ai_chat"):
            return "chat"
    return "global"


def is_antigravity_ide_window(win: WindowLike) -> bool:
    return match_app_patterns(win, _ANTIGRAVITY_IDE_PATTERNS)


def is_terminal_window(win: WindowLike) -> bool:
    """Return True when ``win`` is a known Windows/macOS terminal or dedicated SSH client."""
    bid = (win.bundle_id or "").strip().lower()
    if bid and bid in MACOS_TERMINAL_BUNDLE_IDS:
        return True
    proc = normalize_app_token(win.process)
    if proc and (proc in WINDOWS_TERMINALS or proc in MACOS_TERMINALS):
        return True
    return is_dedicated_ssh_client_window(win)


def is_codex_desktop_window(win: WindowLike) -> bool:
    """Return True for unified ChatGPT / Codex desktop windows (excluding terminal CLI windows)."""
    if is_terminal_window(win):
        return False
    bid = (win.bundle_id or "").strip().lower()
    if bid:
        return bid == "com.openai.codex"
    proc = normalize_app_token(win.process)
    if proc == "chatgpt":
        return True
    if proc == "codex":
        return _is_desktop_ai_app_title(win.title, _DESKTOP_AI_COLLIDING_PROCS["codex"])
    return False


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


def _strip_trailing_app_suffix(
    title: str,
    suffixes: tuple[str, ...],
    *,
    allow_tight_dash: bool = False,
) -> str:
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
        if allow_tight_dash and _matches_desktop_app_suffix(low, app) and low != app:
            prefix = s[: -len(app)].rstrip()
            return prefix[:-1].strip()
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


def is_remote_ide_window(win: WindowLike) -> bool:
    """Return True when an IDE window or document URI is connected to a non-WSL remote host (SSH/Codespaces/Tunnel)."""
    raw_doc = (getattr(win, "document", "") or "").strip().lower()
    if raw_doc.startswith(("ssh://", "sftp://")):
        return True
    if raw_doc.startswith("vscode-remote://"):
        authority = raw_doc[len("vscode-remote://") :].split("/", 1)[0]
        if not authority.startswith("wsl+"):
            return True
    title_low = (win.title or "").lower()
    if "vscode-remote://" in title_low:
        after = title_low.split("vscode-remote://", 1)[1]
        if not after.startswith("wsl+"):
            return True
    if any(
        tok in title_low
        for tok in ("[ssh:", "[ssh ", "[codespaces:", "[tunnel:", "[remote:", "ssh://")
    ):
        return True
    parsed = parse_ide_window_title(win)
    return bool(parsed is not None and parsed.remote_kind == "remote")
