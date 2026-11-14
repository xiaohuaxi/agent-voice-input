"""macOS terminal audio capture (avfoundation) and tmux / pbcopy + osascript paste."""
from __future__ import annotations

import os, shutil, subprocess
from pathlib import Path
from typing import Any, Callable, Mapping

from .audio_proc import resolve_ffmpeg_bin
from .config import Config
from .platform_wsl import FfmpegWavRecorder, ForegroundWindow, PasteOutcome

MACOS_TERMINALS = frozenset({"terminal", "iterm2", "iterm", "wezterm", "wezterm-gui", "alacritty", "ghostty", "kitty", "warp"})
SOUND_MAP = {"start": "Tink.aiff", "stop": "Pop.aiff", "focus": "Glass.aiff", "error": "Basso.aiff"}


def probe_macos_host(cfg: Config, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    eff_env = os.environ if env is None else env
    info: dict[str, Any] = {"avfoundation_ok": False, "pbcopy_ok": bool(shutil.which("pbcopy")), "tmux_active": bool(eff_env.get("TMUX"))}
    try:
        p = subprocess.run([resolve_ffmpeg_bin(), "-nostdin", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5, check=False)
        info["avfoundation_ok"] = "AVFoundation audio devices" in (p.stdout + p.stderr)
    except Exception as exc:
        info["av_error"] = str(exc)
    return info


class MacOSPlatform:
    def __init__(self, cfg: Config, *, env: Mapping[str, str] | None = None) -> None:
        self.cfg = cfg
        self.env = dict(os.environ if env is None else env)
        self.audio_dev = (
            self.env.get("AGENT_VOICE_INPUT_MACOS_AUDIO_DEVICE")
            or self.env.get("VOICE_INPUT_MACOS_AUDIO_DEVICE")
            or "0"
        ).strip() or "0"
        self._ff_rec = FfmpegWavRecorder()
        self._recording = False
        self.last_audio_error = ""

    def start_event_listener(self, on_hotkey_event: Callable[[dict[str, Any]], None]) -> None:
        return None

    def get_foreground_window(self) -> ForegroundWindow:
        app_name, win_title = "", ""
        if shutil.which("osascript"):
            script = 'tell application "System Events" to set p to first process whose frontmost is true\nreturn (name of p) & "\\n" & (value of attribute "AXTitle" of front window of p)'
            p = subprocess.run(["osascript", "-e", script], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=2.5, check=False)
            if p.returncode == 0 and p.stdout.strip():
                parts = p.stdout.splitlines()
                app_name, win_title = parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")
        if shutil.which("tmux") and (self.env.get("TMUX") or app_name.lower() in MACOS_TERMINALS):
            tp = subprocess.run(["tmux", "display-message", "-p", "#{pane_title}"], stdin=subprocess.DEVNULL, capture_output=True, text=True, env=self.env, timeout=1.5, check=False)
            if tp.returncode == 0 and tp.stdout.strip():
                win_title = f"{win_title} {tp.stdout.strip()}".strip()
        return ForegroundWindow(0, app_name, win_title)

    def beep(self, kind: str) -> None:
        snd = f"/System/Library/Sounds/{SOUND_MAP.get(kind, 'Basso.aiff')}"
        if self.cfg.audio.beep and shutil.which("afplay") and os.path.isfile(snd):
            subprocess.Popen(["afplay", snd], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def hud(self, state: str, text: str = "", duration_ms: int = 0) -> None:
        return None

    def start_recording(self, runtime_dir: Path, *, loopback: bool = False) -> None:
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

    def deliver_text(self, text: str, seq_id: int, target_hwnd: int = 0) -> PasteOutcome:
        clip_ok = bool(shutil.which("pbcopy")) and subprocess.run(["pbcopy"], input=text, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2.0, check=False).returncode == 0
        fg = self.get_foreground_window()
        is_term = not fg.process or fg.process.lower() in MACOS_TERMINALS
        if is_term and shutil.which("tmux") and self.env.get("TMUX"):
            lb = subprocess.run(["tmux", "load-buffer", "-"], input=text, text=True, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2.0, check=False)
            pb = subprocess.run(["tmux", "paste-buffer", "-p"], stdin=subprocess.DEVNULL, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2.0, check=False)
            if lb.returncode == 0 and pb.returncode == 0:
                return PasteOutcome(True, clip_ok or True, "tmux", fg.process)
        if is_term and clip_ok and shutil.which("osascript"):
            if subprocess.run(["osascript", "-e", 'tell application "System Events" to keystroke "v" using command down'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2.5, check=False).returncode == 0:
                return PasteOutcome(True, True, "ok", fg.process)
        if clip_ok:
            self.beep("focus")
        return PasteOutcome(False, clip_ok, "focus_changed" if clip_ok else "clipboard_failed", fg.process)

    def close(self) -> None:
        self.cancel_recording()
