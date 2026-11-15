"""Environment-aware HUD language resolution and bilingual status copy (single source of truth)."""
from __future__ import annotations

import array
import math
import os
import sys
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
    "recognizing": ("◐ 语音识别…", "◐ Transcribing…"),
    "processing": ("◐ 语音识别…", "◐ Transcribing…"),
    "refining": ("◈ 整理精修…", "◈ Refining…"),
    "done": ("✓ 已填入", "✓ Inserted"),
    "copied": ("✓ 已复制(窗口已切换)", "✓ Copied (window switched)"),
    "empty": ("○ 未检测到语音", "○ No speech detected"),
    "no_mic": ("✕ 无可用麦克风", "✕ No microphone found"),
    "mic_disconnected": ("✕ 麦克风已断开", "✕ Microphone disconnected"),
}

_PERSISTENT_HUD_STATES: frozenset[str] = frozenset(
    {"", "idle", "hide", "recording", "recognizing", "processing", "refining"}
)

HUD_STATE_DURATIONS_MS: dict[str, int] = {
    "done": 900,
    "copied": 2000,
    "empty": 2200,
    "no_mic": 2200,
    "mic_disconnected": 2200,
    "error": 2600,
}

DEFAULT_ERROR_HUD_DURATION_MS: int = 2600


def default_hud_duration_ms(state: str) -> int:
    """Return default auto-hide duration in ms for ``state`` (0 = stay visible until next state)."""
    st = (state or "").strip().lower()
    if st in _PERSISTENT_HUD_STATES:
        return 0
    return HUD_STATE_DURATIONS_MS.get(st, DEFAULT_ERROR_HUD_DURATION_MS)


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
            return f"● 录音中… ({'松开' if is_ptt else '按'}{sep}{hk} 完成)"
        return f"● Listening… ({'release' if is_ptt else 'press'} {hk} to finish)"
    pair = _STATIC_HUD_COPY.get(st, ("✕ 识别失败", "✕ Transcription failed"))
    return pair[0] if zh else pair[1]


def format_recording_duration(elapsed_sec: int | float) -> str:
    """Format recording elapsed seconds as '0s'..'59s' (< 60s) or '1m 00s' (>= 60s)."""
    sec = max(0, int(elapsed_sec))
    if sec < 60:
        return f"{sec}s"
    m, s = divmod(sec, 60)
    return f"{m}m {s:02d}s"


def recording_countdown_warn_sec(max_duration_sec: float) -> int:
    """Return countdown warning threshold in seconds before ``max_duration_sec`` (`0` when disabled)."""
    if not math.isfinite(max_duration_sec) or max_duration_sec <= 0:
        return 0
    if max_duration_sec >= 45:
        return 15
    return max(1, min(10, int(max_duration_sec // 3) or 1))


def format_countdown_hint(remain_sec: int, lang: str = "en") -> str:
    """Format countdown bracket hint near maximum recording duration."""
    r = max(0, int(remain_sec))
    if (lang or "en").strip().lower() == "zh":
        return f"({r}s 后自动完成)"
    return f"(auto-finish in {r}s)"


def pcm16le_rms_level(pcm_bytes: bytes | bytearray | memoryview) -> float:
    """Compute normalized visual audio level in [0.0, 1.0] from 16-bit signed LE PCM bytes."""
    even_n = len(pcm_bytes) - (len(pcm_bytes) % 2)
    if even_n < 2:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm_bytes[:even_n])
    if sys.byteorder != "little":
        samples.byteswap()
    rms = math.sqrt(sum(x * x for x in samples) / len(samples)) / 32768.0
    if rms <= 1e-5:
        return 0.0
    db = 20.0 * math.log10(rms)
    if db <= -48.0:
        return 0.0
    if db >= -14.0:
        return 1.0
    return (db + 48.0) / 34.0


__all__ = [
    "DEFAULT_ERROR_HUD_DURATION_MS",
    "HUD_STATE_DURATIONS_MS",
    "POSIX_LOCALE_VARS",
    "VALID_UI_LANGUAGES",
    "VALID_UI_POSITIONS",
    "default_hud_duration_ms",
    "format_countdown_hint",
    "format_hotkey_label",
    "format_hud_text",
    "format_recording_duration",
    "is_zh_locale",
    "normalize_ui_language",
    "normalize_ui_position",
    "pcm16le_rms_level",
    "recording_countdown_warn_sec",
    "resolve_ui_language",
]
