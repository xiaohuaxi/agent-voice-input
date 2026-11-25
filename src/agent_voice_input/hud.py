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
    "cancelled": ("○ 已取消", "○ Cancelled"),
    "empty": ("○ 未检测到语音", "○ No speech detected"),
    "no_mic": ("✕ 无可用麦克风", "✕ No microphone found"),
    "mic_unavailable": ("! 麦克风暂忙，请再试一次", "! Microphone busy, try again"),
    "mic_no_stream": ("! 麦克风无音频流，请检查耳机/设备", "! No mic audio stream, check headset"),
    "mic_permission": ("✕ 麦克风权限未开启", "✕ Microphone access denied"),
    "mic_disconnected": ("✕ 麦克风已断开", "✕ Microphone disconnected"),
    "audio_recovered": ("! 麦克风短暂中断，已自动恢复", "! Mic briefly interrupted, resumed"),
    "partial_asr": ("! 部分语音未识别，可重试", "! Partial speech missed, retry available"),
    "partial_asr_unsaved": ("! 部分语音未识别", "! Partial speech missed"),
    "partial_asr_copied": ("! 已复制，部分未识别可重试", "! Copied; partial missed, can retry"),
    "partial_asr_copied_unsaved": ("! 已复制，部分语音未识别", "! Copied; partial speech missed"),
    "asr_empty_retry": ("! 识别为空，已存录音可重试", "! Empty transcript, audio saved for retry"),
    "asr_empty": ("✕ 识别为空", "✕ Empty transcript"),
    "paste_failed_retry": ("✕ 填入失败，已存录音可重试", "✕ Insert failed, audio saved for retry"),
    "paste_failed": ("✕ 填入失败", "✕ Failed to insert text"),
}

_CONTROL_HUD_STATES: frozenset[str] = frozenset(
    {"", "idle", "hide", "hide_toast", "hide_all"}
)

_MAIN_HUD_STATES: frozenset[str] = frozenset(
    {"recording", "recognizing", "processing", "refining", "done", "copied"}
)

_PERSISTENT_HUD_STATES: frozenset[str] = frozenset(
    {*_CONTROL_HUD_STATES, "recording", "recognizing", "processing", "refining"}
)

CANCELLABLE_HUD_STATES: frozenset[str] = frozenset(
    {"recording", "recognizing", "processing", "refining"}
)

HOLD_CANCEL_HINT_COPY: tuple[str, str] = ("继续按住取消…", "keep holding to cancel…")

HUD_STATE_DURATIONS_MS: dict[str, int] = {
    "done": 900,
    "copied": 5000,
    "cancelled": 1600,
    "empty": 2200,
    "no_mic": 2200,
    "mic_unavailable": 2600,
    "mic_no_stream": 2600,
    "mic_permission": 3200,
    "mic_disconnected": 2200,
    "audio_recovered": 2400,
    "partial_asr": 3200,
    "partial_asr_unsaved": 3200,
    "partial_asr_copied": 3200,
    "partial_asr_copied_unsaved": 3200,
    "asr_empty_retry": 3200,
    "asr_empty": 2600,
    "paste_failed_retry": 3200,
    "paste_failed": 2600,
    "error": 2600,
}

DEFAULT_ERROR_HUD_DURATION_MS: int = 2600


def hud_layer_for_state(state: str) -> str:
    """Return HUD layer routing ('control' | 'main' | 'toast') for ``state``."""
    st = (state or "").strip().lower()
    if st in _CONTROL_HUD_STATES:
        return "control"
    if st in _MAIN_HUD_STATES:
        return "main"
    return "toast"


def is_toast_hud_state(state: str) -> bool:
    """Return True when ``state`` renders in the upper toast capsule above the main HUD slot."""
    return hud_layer_for_state(state) == "toast"


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
    if st in _CONTROL_HUD_STATES:
        return ""
    zh = (lang or "en").strip().lower() == "zh"
    if st == "recording":
        is_ptt = (trigger_mode or "").strip().lower() in ("ptt", "push_to_talk")
        hk = format_hotkey_label(hotkey, "zh" if zh else "en")
        if zh:
            sep = " " if (hk and ord(hk[0]) <= 0x7F) else ""
            return f"● 录音中… ({'松开' if is_ptt else '按'}{sep}{hk} 结束)"
        return f"● Listening… ({'release' if is_ptt else 'press'} {hk} to end)"
    pair = _STATIC_HUD_COPY.get(st, ("✕ 识别失败", "✕ Transcription failed"))
    return pair[0] if zh else pair[1]


def format_copied_hud_text(reason: str = "", lang: str = "en") -> str:
    """Format bilingual HUD copy for clipboard-only fallback reasons without adding new visual states."""
    low = (reason or "").strip().lower()
    zh = (lang or "en").strip().lower() == "zh"
    if "accessibility_denied" in low:
        return "✓ 已复制(需开启辅助功能)" if zh else "✓ Copied (grant Accessibility)"
    if "automation_denied" in low:
        return "✓ 已复制(需开启自动化权限)" if zh else "✓ Copied (grant Automation)"
    if "hotkey_held" in low or "key_held" in low:
        return "✓ 已复制(检测到按键按住)" if zh else "✓ Copied (modifier held)"
    if "send_input_failed" in low:
        return "✓ 已复制(无法自动粘贴)" if zh else "✓ Copied (manual paste needed)"
    pair = _STATIC_HUD_COPY["copied"]
    return pair[0] if zh else pair[1]


def format_bg_count_badge(bg_count: int) -> str:
    """Return compact background in-flight count badge (e.g. '+1', '+2') or '' when zero."""
    n = int(bg_count)
    return f"+{n}" if n > 0 else ""


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


def format_hold_cancel_hint(lang: str = "en") -> str:
    """Format hold-to-cancel preview hint copy in Chinese ('zh') or English ('en')."""
    return (
        HOLD_CANCEL_HINT_COPY[0]
        if (lang or "en").strip().lower() == "zh"
        else HOLD_CANCEL_HINT_COPY[1]
    )


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
    "CANCELLABLE_HUD_STATES",
    "DEFAULT_ERROR_HUD_DURATION_MS",
    "HOLD_CANCEL_HINT_COPY",
    "HUD_STATE_DURATIONS_MS",
    "POSIX_LOCALE_VARS",
    "VALID_UI_LANGUAGES",
    "VALID_UI_POSITIONS",
    "default_hud_duration_ms",
    "format_bg_count_badge",
    "format_copied_hud_text",
    "format_countdown_hint",
    "format_hold_cancel_hint",
    "format_hotkey_label",
    "format_hud_text",
    "format_recording_duration",
    "hud_layer_for_state",
    "is_toast_hud_state",
    "is_zh_locale",
    "normalize_ui_language",
    "normalize_ui_position",
    "pcm16le_rms_level",
    "recording_countdown_warn_sec",
    "resolve_ui_language",
]
