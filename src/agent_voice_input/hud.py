"""Environment-aware HUD language resolution and bilingual status copy (single source of truth)."""
from __future__ import annotations

import os
from typing import Mapping

from .config import (
    VALID_UI_LANGUAGES,
    VALID_UI_POSITIONS,
    normalize_ui_language,
    normalize_ui_position,
)

POSIX_LOCALE_VARS: tuple[str, ...] = ("LC_ALL", "LC_MESSAGES", "LANG")

_HOTKEY_LABELS: dict[str, tuple[str, str]] = {
    "rcontrol": ("右 Ctrl", "Right Ctrl"),
    "rctrl": ("右 Ctrl", "Right Ctrl"),
    "rightcontrol": ("右 Ctrl", "Right Ctrl"),
    "rightctrl": ("右 Ctrl", "Right Ctrl"),
    "lcontrol": ("左 Ctrl", "Left Ctrl"),
    "lctrl": ("左 Ctrl", "Left Ctrl"),
    "leftcontrol": ("左 Ctrl", "Left Ctrl"),
    "leftctrl": ("左 Ctrl", "Left Ctrl"),
    "control": ("Ctrl", "Ctrl"),
    "ctrl": ("Ctrl", "Ctrl"),
    "ralt": ("右 Alt", "Right Alt"),
    "rightalt": ("右 Alt", "Right Alt"),
    "rmenu": ("右 Alt", "Right Alt"),
    "lalt": ("左 Alt", "Left Alt"),
    "leftalt": ("左 Alt", "Left Alt"),
    "lmenu": ("左 Alt", "Left Alt"),
    "alt": ("Alt", "Alt"),
    "roption": ("右 Option", "Right Option"),
    "rightoption": ("右 Option", "Right Option"),
    "loption": ("左 Option", "Left Option"),
    "leftoption": ("左 Option", "Left Option"),
    "option": ("Option", "Option"),
    "rcommand": ("右 Cmd", "Right Cmd"),
    "rcmd": ("右 Cmd", "Right Cmd"),
    "rightcommand": ("右 Cmd", "Right Cmd"),
    "rightcmd": ("右 Cmd", "Right Cmd"),
    "lcommand": ("左 Cmd", "Left Cmd"),
    "lcmd": ("左 Cmd", "Left Cmd"),
    "leftcommand": ("左 Cmd", "Left Cmd"),
    "leftcmd": ("左 Cmd", "Left Cmd"),
    "command": ("Cmd", "Cmd"),
    "cmd": ("Cmd", "Cmd"),
    "rshift": ("右 Shift", "Right Shift"),
    "rightshift": ("右 Shift", "Right Shift"),
    "lshift": ("左 Shift", "Left Shift"),
    "leftshift": ("左 Shift", "Left Shift"),
    "shift": ("Shift", "Shift"),
    "fn": ("Fn", "Fn"),
    "function": ("Fn", "Fn"),
    "globe": ("Globe", "Globe"),
    "capslock": ("CapsLock", "CapsLock"),
    "scrolllock": ("ScrollLock", "ScrollLock"),
    "pause": ("Pause", "Pause"),
    "insert": ("Insert", "Insert"),
    "space": ("Space", "Space"),
}

_STATIC_HUD_COPY: dict[str, tuple[str, str]] = {
    "recognizing": ("◐ 正在语音识别…", "◐ Transcribing…"),
    "processing": ("◐ 正在语音识别…", "◐ Transcribing…"),
    "refining": ("◈ 正在整理精修…", "◈ Refining…"),
    "done": ("✓ 已填入", "✓ Inserted"),
    "copied": ("✓ 已复制(窗口已切换)", "✓ Copied (window switched)"),
    "empty": ("○ 未检测到语音", "○ No speech detected"),
    "no_mic": ("✕ 无可用麦克风", "✕ No microphone found"),
    "mic_disconnected": ("✕ 麦克风已断开", "✕ Microphone disconnected"),
}


def is_zh_locale(tag: str | None) -> bool:
    """Return True when ``tag`` is a Chinese BCP-47 / POSIX locale identifier (e.g. zh-CN, zh_TW.UTF-8)."""
    if not tag:
        return False
    t = tag.strip().lower()
    return t == "zh" or t.startswith(("zh-", "zh_", "zh.", "zh@"))


def resolve_ui_language(
    configured: str | None = "auto",
    *,
    env: Mapping[str, str] | None = None,
    win_ui_culture: str | None = None,
) -> str:
    """Resolve effective HUD language ('zh' | 'en')."""
    src_env = os.environ if env is None else env
    if configured is not None and str(configured).strip() != "":
        norm = normalize_ui_language(configured, "ui.language")
        if norm != "auto":
            return norm
    for prefix in ("AGENT_VOICE_INPUT_", "VOICE_INPUT_"):
        var_name = f"{prefix}UI_LANGUAGE"
        val = src_env.get(var_name)
        if val is not None and val.strip() != "":
            norm_env = normalize_ui_language(val, var_name)
            if norm_env != "auto":
                return norm_env
            break

    if win_ui_culture is not None:
        return "zh" if is_zh_locale(win_ui_culture) else "en"

    for var_name in POSIX_LOCALE_VARS:
        raw_loc = (src_env.get(var_name) or "").strip()
        if raw_loc:
            return "zh" if is_zh_locale(raw_loc) else "en"
    return "en"


def format_hotkey_label(hotkey: str = "RControl", lang: str = "en") -> str:
    """Return human-friendly hotkey label in Chinese ('zh') or English ('en')."""
    k = (hotkey or "RControl").strip() or "RControl"
    zh = (lang or "en").strip().lower() == "zh"
    low = k.lower()
    if pair := _HOTKEY_LABELS.get(low):
        return pair[0] if zh else pair[1]
    if low.startswith("f") and low[1:].isdigit() and 1 <= (fn := int(low[1:])) <= 24:
        return f"F{fn}"
    if low.startswith("0x") and len(k) > 2:
        return "0x" + k[2:].upper()
    return k


def format_hud_text(
    state: str,
    lang: str = "en",
    *,
    hotkey: str = "RControl",
    trigger_mode: str = "tap_toggle",
) -> str:
    """Format HUD status pill copy in Chinese ('zh') or English ('en')."""
    st = (state or "").strip().lower()
    if st in ("", "idle", "hide"):
        return ""
    zh = (lang or "en").strip().lower() == "zh"
    if st == "recording":
        is_ptt = (trigger_mode or "").strip().lower() in ("ptt", "push_to_talk")
        hk = format_hotkey_label(hotkey, "zh" if zh else "en")
        if zh:
            sep = " " if (hk and ord(hk[0]) <= 0x7F) else ""
            return f"● 正在录音… ({'松开' if is_ptt else '再按'}{sep}{hk} 完成)"
        return f"● Listening… ({'release' if is_ptt else 'press'} {hk} to finish)"
    pair = _STATIC_HUD_COPY.get(st, ("✕ 识别失败", "✕ Transcription failed"))
    return pair[0] if zh else pair[1]


__all__ = [
    "POSIX_LOCALE_VARS",
    "VALID_UI_LANGUAGES",
    "VALID_UI_POSITIONS",
    "format_hotkey_label",
    "format_hud_text",
    "is_zh_locale",
    "normalize_ui_language",
    "normalize_ui_position",
    "resolve_ui_language",
]
