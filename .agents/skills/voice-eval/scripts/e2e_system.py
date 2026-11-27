#!/usr/bin/env python3
"""Zero-disruption end-to-end live system test suite for agent-voice-input.

Exercises the complete real pipeline in strict isolation from the user's desktop,
clipboard, global hotkey, and host daemon:
  Real human-speech WAV (cached system TTS or --wav)
  -> Real Stage 1 ASR provider
  -> Real multi-chunk <session_reference_log> context sync
  -> Real Stage 2 stateless Refiner (prewarm + prefix-cache hit verification)
  -> Real VoiceInputDaemon UNIX socket IPC & back-to-back serial utterance queue
  -> Real isolated `tmux -L avi-e2e-*` pane buffer paste & capture-pane readback
  -> CLI `doctor` & `doctor --ping`, non-Git folder context & shallow files, SSH crosstalk veto, `status --history`, `diag.jsonl` (0600, zero prose), `failed/` staging, and `diagnose_live.py --json`.
"""
from __future__ import annotations

import argparse
import base64
from collections import deque
import contextlib
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Mapping
import uuid

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SRC_ROOT = _REPO_ROOT / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from agent_voice_input import __version__  # noqa: E402
from agent_voice_input import _tmux, platform_macos, platform_wsl  # noqa: E402
from agent_voice_input.asr_providers import BaseAsrProvider, ChatAudioAsrProvider, GeminiAudioProvider, create_asr_provider  # noqa: E402
from agent_voice_input.audio_proc import (  # noqa: E402
    AudioConfig,
    decode_to_16k_mono_pcm,
    ensure_16k_mono_s16_wav,
    list_failed_audios,
    list_saved_recordings,
    pack_pcm16_mono_wav,
    process_wav,
)
from agent_voice_input.config import (  # noqa: E402
    Config,
    ensure_secure_dir,
    expand_user_path,
    get_runtime_dir,
    load_config,
    redact_secrets,
    resolve_archive_audio_dir,
    resolve_credentials,
)
from agent_voice_input.daemon import (  # noqa: E402
    DIAG_FILENAME,
    VoiceInputDaemon,
    read_diag_history,
    send_control_command,
)
from agent_voice_input.hotwords import resolve_personal_hotwords_path  # noqa: E402
from agent_voice_input.platform_wsl import ForegroundWindow, PasteOutcome  # noqa: E402
from agent_voice_input.refiner import create_refiner  # noqa: E402
from agent_voice_input.session_ctx import SessionTracker, write_session_registry_entry  # noqa: E402


DEFAULT_UTTERANCE_SPECS: tuple[dict[str, Any], ...] = (
    {
        "id": "U1",
        "name": "cold_ident",
        "tts_text": "帮我看一下 refiner 点 p y 里面的 Session Tracker 缓存分块逻辑有没有边界遗漏",
        "expect_substrings": ("refiner.py",),
        "is_question": False,
        "is_correction": False,
        "probe_phrases": ("边界遗漏", "缓存分块逻辑"),
    },
    {
        "id": "U2",
        "name": "serial_question",
        "tts_text": "为什么这里的缓存命中率会下降啊，是不是上下文分块被改到了？",
        "expect_substrings": (),
        "is_question": True,
        "is_correction": False,
        "probe_phrases": ("命中率会下降", "分块被改到"),
    },
    {
        "id": "U3",
        "name": "serial_correction",
        "tts_text": "先把超时时间改成三十秒，不对不对改成九十秒，然后跑一遍单测看看结果",
        "expect_substrings": (),
        "is_question": False,
        "is_correction": True,
        "probe_phrases": ("不对不对", "看看结果"),
    },
)

NON_GIT_UTTERANCE_SPEC: dict[str, Any] = {
    "id": "NG1",
    "name": "non_git_shallow_ident",
    "tts_text": "请对照 custom widget spec 点 m d 核对一下第四季度预算方案字段",
    "expect_substrings": ("custom_widget_spec.md",),
    "probe_phrases": ("第四季度预算方案", "核对一下"),
}


def _strip_fast_env(raw_env: Mapping[str, str]) -> dict[str, str]:
    clean = dict(raw_env)
    for k in list(clean.keys()):
        ku = k.upper()
        if "FAST" in ku and ("CODEX" in ku or "OPENAI" in ku or "VOICE" in ku):
            clean.pop(k, None)
    return clean


def _resolve_powershell_bin(env: Mapping[str, str]) -> str:
    resolver = getattr(platform_wsl, "resolve_powershell_exe", None)
    if callable(resolver):
        cand = str(resolver(env) or "").strip()
        if cand and os.path.isfile(cand):
            return cand
    if os.path.isfile(platform_wsl.POWERSHELL_EXE):
        return platform_wsl.POWERSHELL_EXE
    found = shutil.which("powershell.exe", path=env.get("PATH"))
    if found:
        return found
    raise RuntimeError("powershell.exe not found on WSL2 host")


def _atomic_write_0600(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".{target.name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(tmp), flags, 0o600)
    closed = False
    try:
        os.fchmod(fd, 0o600)
        fobj = os.fdopen(fd, "wb")
        closed = True
        with fobj as f:
            f.write(data)
            f.flush()
        os.replace(str(tmp), str(target))
    except Exception:
        if not closed:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _synthesize_speech_wav_bytes(text: str, env: Mapping[str, str], audio_cfg: AudioConfig) -> bytes:
    """Synthesize real spoken audio via built-in OS TTS (WSL2 System.Speech or macOS `say`) -> 16kHz mono PCM WAV."""
    raw_audio: bytes = b""
    if sys.platform == "darwin":
        say_bin = "/usr/bin/say" if os.path.isfile("/usr/bin/say") else (shutil.which("say") or "")
        if not say_bin:
            raise RuntimeError("macOS /usr/bin/say not available for TTS synthesis; pass --wav explicitly")
        with tempfile.TemporaryDirectory(prefix="avi-say-") as td:
            aiff_p = Path(td) / "utterance.aiff"
            voices_p = subprocess.run(
                [say_bin, "-v", "?"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
            chosen_voice = ""
            for v_cand in ("Tingting", "Meijia", "Sinji"):
                if v_cand.lower() in (voices_p.stdout or "").lower():
                    chosen_voice = v_cand
                    break
            if not chosen_voice:
                for ln in (voices_p.stdout or "").splitlines():
                    if "zh_cn" in ln.lower() or "zh-cn" in ln.lower():
                        chosen_voice = ln.split()[0]
                        break
            cmd = [say_bin, *(["-v", chosen_voice] if chosen_voice else []), "-o", str(aiff_p), text]
            proc = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if proc.returncode != 0 or not aiff_p.is_file():
                raise RuntimeError(f"macOS say failed (rc={proc.returncode}): {proc.stderr.strip()}")
            raw_audio = aiff_p.read_bytes()
    elif platform_wsl.is_wsl(env):
        ps_exe = _resolve_powershell_bin(env)
        b64_txt = base64.b64encode(text.encode("utf-8")).decode("ascii")
        ps_cmd = (
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
            "Add-Type -AssemblyName System.Speech\n"
            f'$txt = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("{b64_txt}"))\n'
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
            "foreach ($v in $s.GetInstalledVoices()) {\n"
            '  if ($v.VoiceInfo.Culture.Name -eq "zh-CN" -or $v.VoiceInfo.Name -match "Huihui|Yaoyao|Xiaoxiao|Kangkang") {\n'
            "    $s.SelectVoice($v.VoiceInfo.Name)\n"
            "    break\n"
            "  }\n"
            "}\n"
            "$s.Rate = 0\n"
            "$ms = New-Object System.IO.MemoryStream\n"
            "try {\n"
            "  $s.SetOutputToWaveStream($ms)\n"
            "  $s.Speak($txt)\n"
            "  $s.SetOutputToNull()\n"
            '  [Console]::Out.WriteLine("WAVB64:" + [Convert]::ToBase64String($ms.ToArray()))\n'
            "} finally {\n"
            "  $ms.Dispose()\n"
            "  $s.Dispose()\n"
            "}\n"
        )
        proc = subprocess.run(
            [ps_exe, "-NoProfile", "-NonInteractive", "-Sta", "-Command", ps_cmd],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=25,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"WSL2 System.Speech TTS failed (rc={proc.returncode}): {proc.stderr.strip()[:200]}")
        b64_lines = [ln[7:].strip() for ln in proc.stdout.splitlines() if ln.startswith("WAVB64:")]
        if not b64_lines or not b64_lines[-1]:
            raise RuntimeError("WSL2 System.Speech TTS produced empty WAV stream")
        raw_audio = base64.b64decode(b64_lines[-1])
    else:
        raise RuntimeError("No built-in OS TTS backend on pure non-WSL Linux; pass --wav <path> explicitly")

    pcm16 = decode_to_16k_mono_pcm(raw_audio)
    wav16 = pack_pcm16_mono_wav(pcm16)
    gate = process_wav(wav16, audio_cfg)
    if not gate.passed or gate.all_zero or gate.duration_sec < 0.6:
        raise RuntimeError(
            f"Synthesized TTS WAV failed audio gate (passed={gate.passed}, reason={gate.reason}, dur={gate.duration_sec:.2f}s)"
        )
    return wav16


def _prepare_cached_tts_item(
    spec: Mapping[str, Any],
    *,
    cache_dir: Path,
    refresh_tts: bool,
    env: Mapping[str, str],
    audio_cfg: AudioConfig,
) -> dict[str, Any]:
    tts_txt = str(spec["tts_text"])
    digest = hashlib.sha256(f"v1:{sys.platform}:{tts_txt}".encode("utf-8")).hexdigest()[:16]
    wav_p = cache_dir / f"tts-{str(spec['id']).lower()}-{digest}.wav"
    reused = False
    gate_dur = 0.0
    if not refresh_tts and wav_p.is_file() and not wav_p.is_symlink():
        try:
            os.chmod(wav_p, 0o600)
            gate = process_wav(wav_p, audio_cfg)
            if gate.passed and not gate.all_zero and gate.duration_sec >= 0.6:
                reused = True
                gate_dur = gate.duration_sec
        except Exception:
            reused = False
    if not reused:
        wav_bytes = _synthesize_speech_wav_bytes(tts_txt, env, audio_cfg)
        _atomic_write_0600(wav_p, wav_bytes)
        gate = process_wav(wav_p, audio_cfg)
        gate_dur = gate.duration_sec
    return {
        **spec,
        "wav_path": wav_p,
        "duration_sec": round(gate_dur, 2),
        "cached_wav": reused,
    }


def _prepare_utterance_wavs(
    *,
    wav_arg: str,
    refresh_tts: bool,
    env: Mapping[str, str],
    audio_cfg: AudioConfig,
) -> tuple[list[dict[str, Any]], dict[str, Any], bool]:
    """Return ``(items, non_git_item, used_custom_wav)`` where each item has ``spec`` + ``wav_path`` + ``duration_sec``."""
    if wav_arg.strip():
        raw_paths = [Path(p.strip()).expanduser().resolve() for p in wav_arg.split(",") if p.strip()]
        for p in raw_paths:
            if not p.is_file():
                raise RuntimeError(f"--wav file not found: {p}")
        items: list[dict[str, Any]] = []
        for idx in range(3):
            chosen_p = raw_paths[idx % len(raw_paths)]
            gate = process_wav(chosen_p, audio_cfg)
            if not gate.passed:
                raise RuntimeError(f"--wav failed audio gate ({gate.reason}): {chosen_p.name}")
            items.append(
                {
                    "id": f"U{idx + 1}",
                    "name": f"custom_wav_{idx + 1}",
                    "tts_text": f"<external:{chosen_p.name}>",
                    "wav_path": chosen_p,
                    "duration_sec": round(gate.duration_sec, 2),
                    "cached_wav": True,
                    "expect_substrings": (),
                    "is_question": False,
                    "is_correction": False,
                    "probe_phrases": (),
                }
            )
        ng_item = {
            "id": "NG1",
            "name": "custom_non_git_1",
            "tts_text": f"<external:{items[0]['wav_path'].name}>",
            "wav_path": items[0]["wav_path"],
            "duration_sec": items[0]["duration_sec"],
            "cached_wav": True,
            "expect_substrings": (),
            "probe_phrases": (),
        }
        return items, ng_item, True

    home = expand_user_path("~", env=env)
    cache_dir = ensure_secure_dir(home / ".cache" / "agent-voice-input" / "eval-wavs")
    items = [
        _prepare_cached_tts_item(spec, cache_dir=cache_dir, refresh_tts=refresh_tts, env=env, audio_cfg=audio_cfg)
        for spec in DEFAULT_UTTERANCE_SPECS
    ]
    ng_item = _prepare_cached_tts_item(
        NON_GIT_UTTERANCE_SPEC,
        cache_dir=cache_dir,
        refresh_tts=refresh_tts,
        env=env,
        audio_cfg=audio_cfg,
    )
    return items, ng_item, False


def _create_isolated_session(
    rt_dir: Path,
    *,
    tmux_socket: str,
    tmux_pane: str,
    pane_tty: str,
) -> tuple[str, Path]:
    """Build an isolated ~115K multi-chunk Codex root session registered to the isolated tmux pane."""
    sid = "11111111-2222-4333-8444-555555555555"
    rollout = rt_dir / f"rollout-e2e-{sid}.jsonl"
    lines: list[str] = [
        json.dumps({"type": "session_meta", "payload": {"id": sid, "source": "cli", "cwd": str(_REPO_ROOT)}})
    ]
    turns = [
        (
            "帮我梳理 src/agent_voice_input/refiner.py 和 SessionTracker 的前缀缓存分块机制。",
            "已核对：SessionTracker.get_refiner_context 通过 chunk_turns_for_refiner 按 50000 字符切分 <session_reference_log> 块，保障多轮连续语音输入命中 cached_input_tokens。\n"
            + ("# e2e context chunk padding for deterministic prefix-cache evaluation\n" * 310),
        ),
        (
            "VoiceInputDaemon 的串行队列和 diag.jsonl 脱敏契约是怎么配合的？",
            "VoiceInputDaemon 在后台 worker 线程按序消费 _utterance_queue，diag.jsonl 以 0600 权限仅记录数值分段耗时与 token 计数，绝不落盘用户口述原文。\n"
            + ("# e2e daemon serial pipeline reference log padding line\n" * 310),
        ),
    ]
    for i in range(5):
        u_txt, a_txt = turns[i % len(turns)]
        lines.append(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "item": {"type": "UserMessage", "content": [{"type": "text", "text": f"[Turn {i + 1}] {u_txt}"}]},
                    },
                },
                ensure_ascii=False,
            )
        )
        lines.append(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "item": {"type": "AgentMessage", "content": [{"type": "text", "text": a_txt}]},
                    },
                },
                ensure_ascii=False,
            )
        )
    rollout.write_text("\n".join(lines) + "\n", encoding="utf-8")
    write_session_registry_entry(
        rt_dir,
        cli="codex",
        session_id=sid,
        jsonl_path=str(rollout),
        cwd=str(_REPO_ROOT),
        tmux_pane=tmux_pane,
        tmux_socket=tmux_socket,
        tty=pane_tty,
    )
    return sid, rollout


def _append_codex_turn(rollout: Path, turn_num: int, user_text: str, agent_text: str) -> None:
    new_lines = [
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "UserMessage",
                        "content": [{"type": "text", "text": f"[Turn {turn_num}] {user_text}"}],
                    },
                },
            },
            ensure_ascii=False,
        ),
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "AgentMessage",
                        "content": [{"type": "text", "text": agent_text}],
                    },
                },
            },
            ensure_ascii=False,
        ),
    ]
    with open(rollout, "a", encoding="utf-8") as f:
        f.write("\n".join(new_lines) + "\n")
        f.flush()


def _create_isolated_non_git_workspace_and_session(
    base_dir: Path,
    rt_dir: Path,
    *,
    tmux_socket: str,
    tmux_pane: str,
    pane_tty: str,
) -> tuple[Path, str, Path]:
    """Create a plain folder without `.git`, populate characteristic shallow files, and register a session."""
    ws_dir = base_dir / "non-git-workspace"
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "budget_q4_plan.xlsx").write_bytes(b"PK\x03\x04e2e-placeholder-xlsx")
    (ws_dir / "custom_widget_spec.md").write_text(
        "# Custom Widget Specification\n\nQ4 budget layout fields.\n",
        encoding="utf-8",
    )
    sid = "22222222-3333-4444-8555-666666666666"
    rollout = rt_dir / f"rollout-nongit-{sid}.jsonl"
    rollout.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": sid, "source": "cli", "cwd": str(ws_dir)}}) + "\n",
        encoding="utf-8",
    )
    _append_codex_turn(
        rollout,
        1,
        "我们先在这个普通非 Git 文件夹下整理 custom_widget_spec.md 和 budget_q4_plan.xlsx。",
        "收到，已记录当前非 Git 目录下的 custom_widget_spec.md 与 budget_q4_plan.xlsx 结构说明。\n"
        + ("# non-git turn-1 reference padding\n" * 40),
    )
    write_session_registry_entry(
        rt_dir,
        cli="codex",
        session_id=sid,
        jsonl_path=str(rollout),
        cwd=str(ws_dir),
        tmux_pane=tmux_pane,
        tmux_socket=tmux_socket,
        tty=pane_tty,
    )
    return ws_dir, sid, rollout


class HeadlessTmuxPlatform:
    """Headless test PlatformBackend: zero host hotkey/HUD/clipboard touch, real isolated tmux paste."""

    def __init__(
        self,
        *,
        tmux_bin: str,
        tmux_sock_name: str,
        tmux_sock_path: str,
        tmux_pane: str,
        pane_tty: str,
        session_id: str,
        tmux_env: Mapping[str, str],
    ) -> None:
        self._tmux_bin = tmux_bin
        self._sock_name = tmux_sock_name
        self._tmux_env = {k: v for k, v in tmux_env.items() if k not in ("TMUX", "TMUX_PANE")}
        self._pane = tmux_pane
        self._fg_win = ForegroundWindow(
            hwnd=4242,
            process="WindowsTerminal",
            title=f"codex · avi [codex:#{session_id[:8]}]",
            tty=pane_tty,
            cwd=str(_REPO_ROOT),
        )
        self._wav_queue: deque[bytes] = deque()
        self._cur_wav: bytes = b""
        self._lock = threading.Lock()
        self._shim_disabled = False
        self.last_shim_error = ""
        self.last_audio_error = ""
        self.hud_events: list[str] = []
        self.delivered_by_seq: dict[int, str] = {}

    def enqueue_wav(self, wav_bytes: bytes) -> None:
        with self._lock:
            self._wav_queue.append(wav_bytes)

    def set_recorder_lost_callback(self, cb: Callable[..., Any] | None) -> None:
        del cb

    def set_recorder_recovered_callback(self, cb: Callable[..., Any] | None) -> None:
        del cb

    def start_event_listener(self, on_hotkey_event: Callable[[dict[str, Any]], None]) -> None:
        del on_hotkey_event

    def is_allowed_terminal(self, window: ForegroundWindow) -> bool:
        del window
        return True

    def set_foreground_window(self, win: ForegroundWindow) -> None:
        with self._lock:
            self._fg_win = win

    def get_foreground_window(self) -> ForegroundWindow:
        with self._lock:
            return self._fg_win

    def start_recording(self, runtime_dir: Path, *, loopback: bool = False, seq_id: int | None = None) -> None:
        del runtime_dir, loopback, seq_id
        with self._lock:
            if not self._wav_queue:
                raise RuntimeError("headless wav queue empty")
            self._cur_wav = self._wav_queue.popleft()
        self.last_audio_error = ""

    def stop_recording(self, *, drain: bool = True) -> tuple[bytes, str]:
        del drain
        with self._lock:
            wav, self._cur_wav = self._cur_wav, b""
        return wav, "e2e_mic"

    def cancel_recording(self) -> None:
        with self._lock:
            self._cur_wav = b""

    def switch_to_fallback_audio(self) -> bool:
        return False

    def hud(self, state: str, **kwargs: Any) -> None:
        del kwargs
        with self._lock:
            self.hud_events.append(state)

    def beep(self, kind: str) -> None:
        del kind

    def deliver_text(self, text: str, seq_id: int, target_hwnd: int = 0, **kwargs: Any) -> PasteOutcome:
        del target_hwnd
        if callable(is_canc := kwargs.get("is_cancelled")) and is_canc():
            return PasteOutcome(False, False, "cancelled", "tmux")
        if callable(bcw := kwargs.get("before_clipboard_write")):
            bcw()
        if callable(bc := kwargs.get("before_commit")) and not bc():
            return PasteOutcome(False, False, "cancelled", "tmux")
        buf_name = f"avi-e2e-{os.getpid()}-{seq_id}"
        lb = subprocess.run(
            [self._tmux_bin, "-L", self._sock_name, "load-buffer", "-b", buf_name, "-"],
            input=text,
            text=True,
            encoding="utf-8",
            env=self._tmux_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=3.0,
            check=False,
        )
        if lb.returncode != 0:
            return PasteOutcome(False, False, "tmux_load_failed", "tmux")
        pb = subprocess.run(
            [self._tmux_bin, "-L", self._sock_name, "paste-buffer", "-d", "-p", "-b", buf_name, "-t", self._pane],
            stdin=subprocess.DEVNULL,
            env=self._tmux_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=3.0,
            check=False,
        )
        if pb.returncode != 0:
            return PasteOutcome(False, False, "tmux_paste_failed", "tmux")
        subprocess.run(
            [self._tmux_bin, "-L", self._sock_name, "send-keys", "-t", self._pane, "Enter"],
            stdin=subprocess.DEVNULL,
            env=self._tmux_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
            check=False,
        )
        with self._lock:
            self.delivered_by_seq[seq_id] = text
        return PasteOutcome(True, True, "ok", "tmux", used_clipboard_paste=False)

    def close(self) -> None:
        pass


class _ObservingAsrProxy(BaseAsrProvider):
    """Thin pass-through around the real Stage 1 ASR provider to record draft text & inject fault check."""

    def __init__(self, inner: BaseAsrProvider) -> None:
        self._inner = inner
        self.provider_name = getattr(inner, "provider_name", "")
        self.single_hop = bool(
            isinstance(inner, (GeminiAudioProvider, ChatAudioAsrProvider))
            or getattr(inner, "single_hop", False)
        )
        self.asr_texts: list[str] = []
        self.fail_error_msg: str = ""
        self._lock = threading.Lock()

    def transcribe(
        self,
        wav_bytes: bytes,
        context: Any = None,
        *,
        is_cancelled: Any = None,
        cancel_token: Any = None,
    ) -> Any:
        with self._lock:
            err_msg = self.fail_error_msg
        if err_msg:
            raise RuntimeError(err_msg)
        res = self._inner.transcribe(
            wav_bytes,
            context=context,
            is_cancelled=is_cancelled,
            cancel_token=cancel_token,
        )
        with self._lock:
            self.asr_texts.append(res.text.strip())
        return res


def _wait_daemon_idle(rt_dir: Path, expected_results: int, *, timeout_sec: float = 120.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_sec
    last_st: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last_st = send_control_command(rt_dir, {"cmd": "status", "history": 50}, timeout_sec=3.0)
        recs = last_st.get("recent_results") or []
        if (
            last_st.get("ok")
            and last_st.get("state") == "idle"
            and int(last_st.get("pending_count") or 0) == 0
            and len(recs) >= expected_results
        ):
            return last_st
        time.sleep(0.12)
    raise TimeoutError(
        f"Daemon did not settle to idle with {expected_results} results within {timeout_sec}s (last={last_st})"
    )


@contextlib.contextmanager
def _install_term_signal_handlers() -> Any:
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    sigs: tuple[int, ...] = (
        (signal.SIGTERM, signal.SIGHUP)
        if hasattr(signal, "SIGHUP")
        else (signal.SIGTERM,)
    )
    prev_handlers: dict[int, Any] = {}

    def _handle_term(_signum: int, _frame: Any) -> None:
        for s in sigs:
            try:
                signal.signal(s, signal.SIG_DFL)
            except (OSError, ValueError):
                pass
        raise SystemExit(143)

    for s in sigs:
        prev_handlers[s] = signal.signal(s, _handle_term)
    try:
        yield
    finally:
        for s, prev_h in prev_handlers.items():
            try:
                signal.signal(s, prev_h)
            except (OSError, ValueError):
                pass


def _fmt_cell(text: str, max_chars: int = 160) -> str:
    s = text.replace("|", "\\|").replace("\r", "").replace("\n", " ⏎ ").strip()
    return s if len(s) <= max_chars else (s[:max_chars].rstrip() + " …[truncated]")


def _pane_contains_delivered(fin_txt: str, pane_full: str) -> bool:
    """Verify delivered text landed in tmux pane (handling multiline paragraphs & 2-col CJK wrap padding spaces)."""
    if not fin_txt:
        return False
    if fin_txt in pane_full:
        return True
    fin_norm = "\n".join(ln.strip() for ln in fin_txt.splitlines() if ln.strip())
    if fin_norm and fin_norm in pane_full:
        return True
    fin_compact = "".join(fin_txt.split())
    return bool(fin_compact and fin_compact in "".join(pane_full.split()))


def main(argv: list[str] | None = None) -> int:
    t_suite0 = time.monotonic()
    ap = argparse.ArgumentParser(
        description="Zero-disruption end-to-end live system test suite for agent-voice-input."
    )
    ap.add_argument("--config", help="Optional path to config.toml")
    ap.add_argument("--wav", default="", help="Optional WAV path(s), comma-separated (default: auto OS TTS cache)")
    ap.add_argument("--refresh-tts", action="store_true", help="Regenerate cached TTS WAV samples")
    ap.add_argument("--saved-limit", type=int, default=2, help="Max saved real recordings from host cache to replay (default: 2; 0 skips)")
    ap.add_argument("--skip-fault-check", action="store_true", help="Skip lightweight fault-injection & failed/ staging check")
    ap.add_argument("--json", action="store_true", help="Emit redacted JSON summary instead of Markdown")
    ap.add_argument("--out", default="", help="Optional 0600 file path to write report")
    args = ap.parse_args(argv)

    cfg, raw_env = load_config(config_path=args.config or None)
    clean_env = _strip_fast_env(raw_env)
    creds = resolve_credentials(cfg, clean_env)
    real_secrets = [s for s in creds.all_secret_values() if s and len(s) >= 4]
    canary_secret = f"canary-{uuid.uuid4().hex}"
    clean_env["AVI_E2E_CANARY_TOKEN"] = canary_secret
    all_secrets = [*real_secrets, canary_secret]
    redact_fn = lambda s: redact_secrets(str(s), clean_env, cfg=cfg, extra_secrets=all_secrets)

    tmux_bin = _tmux.resolve_tmux_bin(clean_env)
    if not tmux_bin:
        raise RuntimeError("tmux binary not found; required for isolated pane delivery verification")

    checks: list[dict[str, Any]] = []
    utterance_reports: list[dict[str, Any]] = []
    saved_replay_reports: list[dict[str, Any]] = []
    private_replay_prose: list[str] = []
    host_daemon_info: dict[str, Any] = {"online": False, "shim_ok": None}
    saved_limit = max(0, int(args.saved_limit))
    host_rec_dir = resolve_archive_audio_dir(raw_env, create=False)
    host_saved_all = list_saved_recordings(host_rec_dir) if saved_limit > 0 else []
    host_saved_selected = host_saved_all[:saved_limit]
    host_replay_status = ""

    # Read-only check of optional host main daemon (never mutates it)
    host_rt = get_runtime_dir(raw_env, create=False)
    if (host_rt / "control.sock").exists():
        try:
            hst = send_control_command(host_rt, {"cmd": "status"}, timeout_sec=0.5)
            if hst.get("ok"):
                host_daemon_info = {
                    "online": True,
                    "state": hst.get("state"),
                    "shim_ok": hst.get("shim_ok"),
                }
        except Exception:
            pass

    utterance_items, non_git_item, used_custom_wav = _prepare_utterance_wavs(
        wav_arg=args.wav,
        refresh_tts=args.refresh_tts,
        env=clean_env,
        audio_cfg=cfg.audio,
    )

    sock_name = f"avi-e2e-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    tmux_clean_env = {k: v for k, v in clean_env.items() if k not in ("TMUX", "TMUX_PANE")}

    with _install_term_signal_handlers(), tempfile.TemporaryDirectory(prefix="avi-e2e-") as td:
        rt_dir = ensure_secure_dir(Path(td) / "rt")
        iso_cache_dir = ensure_secure_dir(Path(td) / "cache")
        iso_home = ensure_secure_dir(Path(td) / "app-home")
        e2e_env = dict(tmux_clean_env)
        e2e_env["AGENT_VOICE_INPUT_HOME"] = str(iso_home)
        if cfg.config_path:
            e2e_env["AGENT_VOICE_INPUT_CONFIG"] = str(cfg.config_path)
        e2e_env["AGENT_VOICE_INPUT_RUNTIME_DIR"] = str(rt_dir)
        e2e_env["XDG_CACHE_HOME"] = str(iso_cache_dir)
        prev_pp = e2e_env.get("PYTHONPATH", "")
        e2e_env["PYTHONPATH"] = f"{_SRC_ROOT}{os.pathsep}{prev_pp}" if prev_pp else str(_SRC_ROOT)

        # Mount host personal hotwords read-only (symlink dir or copy personal.txt) into isolated app-home
        host_hw_path = resolve_personal_hotwords_path(raw_env)
        if host_hw_path is not None:
            host_hw_dir = host_hw_path.parent
            iso_hw_dir = iso_home / "hotwords"
            if host_hw_dir.is_dir():
                try:
                    os.symlink(host_hw_dir.resolve(), iso_hw_dir)
                except OSError:
                    ensure_secure_dir(iso_hw_dir)
                    if host_hw_path.is_file() and not host_hw_path.is_symlink():
                        shutil.copy2(host_hw_path, iso_hw_dir / "personal.txt")
                        os.chmod(iso_hw_dir / "personal.txt", 0o600)

        # Subcheck 1: CLI `doctor` (offline) + `doctor --ping` (live endpoint connectivity & auth)
        t_doc0 = time.monotonic()
        doc_proc = subprocess.run(
            [sys.executable, str(_REPO_ROOT / "bin" / "agent-voice-input"), "doctor"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=e2e_env,
            timeout=25.0,
            check=False,
        )
        ping_proc = subprocess.run(
            [sys.executable, str(_REPO_ROOT / "bin" / "agent-voice-input"), "doctor", "--ping"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=e2e_env,
            timeout=30.0,
            check=False,
        )
        doc_ms = max(0, int(round((time.monotonic() - t_doc0) * 1000)))
        ping_out = (ping_proc.stdout or "") + "\n" + (ping_proc.stderr or "")
        doc_combined = (doc_proc.stdout or "") + "\n" + (doc_proc.stderr or "") + "\n" + ping_out
        doc_leaked = any(sec in doc_combined for sec in all_secrets)
        ping_lines = [ln.strip() for ln in (ping_proc.stdout or "").splitlines() if ln.strip().startswith("[ping]")]
        ping_verified = (
            ping_proc.returncode == 0
            and len(ping_lines) >= 1
            and all("FAIL" not in ln for ln in ping_lines)
        )
        doc_ok = (doc_proc.returncode == 0) and ping_verified and (not doc_leaked)
        host_note = (
            f"host_daemon=ONLINE(shim_ok={host_daemon_info['shim_ok']})"
            if host_daemon_info["online"]
            else "host_daemon=OFFLINE(untouched)"
        )
        checks.append(
            {
                "id": "C1_doctor",
                "ok": doc_ok,
                "elapsed_ms": doc_ms,
                "detail": (
                    f"offline_rc={doc_proc.returncode}, ping_rc={ping_proc.returncode}"
                    f"(probes={len(ping_lines)},ok={ping_verified}), "
                    f"zero_secret_leak={not doc_leaked}, {host_note}"
                ),
            }
        )

        # Start isolated tmux server + daemon
        daemon: VoiceInputDaemon | None = None
        daemon_thr: threading.Thread | None = None
        tmux_sock_path = ""
        try:
            ns = subprocess.run(
                [tmux_bin, "-L", sock_name, "-f", "/dev/null", "new-session", "-d", "-s", "e2e", "-x", "200", "-y", "50", "sh", "-c", "cat >/dev/null"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                env=tmux_clean_env,
                timeout=5.0,
                check=False,
            )
            if ns.returncode != 0:
                raise RuntimeError(f"Failed to start isolated tmux server: {ns.stderr.strip()}")

            dm = subprocess.run(
                [tmux_bin, "-L", sock_name, "display-message", "-p", "-t", "e2e:0.0", "#{socket_path}\x1f#{pane_id}\x1f#{pane_tty}"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                env=tmux_clean_env,
                timeout=3.0,
                check=True,
            )
            tmux_sock_path, pane_id, pane_tty = dm.stdout.strip().split("\x1f", 2)

            sid, _rollout = _create_isolated_session(
                rt_dir,
                tmux_socket=tmux_sock_path,
                tmux_pane=pane_id,
                pane_tty=pane_tty,
            )
            subprocess.run(
                [tmux_bin, "-L", sock_name, "select-pane", "-t", pane_id, "-T", f"codex · avi [#{sid[:8]}]"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=tmux_clean_env,
                timeout=2.0,
                check=False,
            )

            platform = HeadlessTmuxPlatform(
                tmux_bin=tmux_bin,
                tmux_sock_name=sock_name,
                tmux_sock_path=tmux_sock_path,
                tmux_pane=pane_id,
                pane_tty=pane_tty,
                session_id=sid,
                tmux_env=tmux_clean_env,
            )
            for item in utterance_items:
                platform.enqueue_wav(Path(item["wav_path"]).read_bytes())

            home = expand_user_path("~", env=e2e_env)
            tracker = SessionTracker(
                home_dir=home,
                env=e2e_env,
                runtime_dir=rt_dir,
                proc_root=rt_dir / "no_proc",
                tmux_socket=tmux_sock_path,
            )
            real_asr = create_asr_provider(cfg, creds, env=e2e_env)
            asr_proxy = _ObservingAsrProxy(real_asr)
            refiner = create_refiner(cfg, creds, env=e2e_env, runtime_dir=rt_dir)
            expects_stage2 = (
                getattr(refiner, "provider_name", "off") not in ("", "off", "none")
                and (not asr_proxy.single_hop or cfg.refiner.force_on_single_hop)
            )

            daemon_cfg = dataclasses.replace(
                cfg,
                archive=dataclasses.replace(
                    cfg.archive,
                    save_audio=True,
                    audio_max_count=max(10, int(cfg.archive.audio_max_count)),
                ),
            )
            daemon = VoiceInputDaemon(
                daemon_cfg,
                env=e2e_env,
                runtime_dir=rt_dir,
                platform=platform,
                asr_provider=asr_proxy,
                refiner=refiner,
                tracker=tracker,
                paste_settle_sec=0.02,
            )
            daemon_thr = threading.Thread(target=daemon.serve_forever, name="avi-e2e-daemon", daemon=True)
            daemon_thr.start()

            sock_deadline = time.monotonic() + 5.0
            while time.monotonic() < sock_deadline:
                if (rt_dir / "control.sock").exists():
                    try:
                        if send_control_command(rt_dir, {"cmd": "status"}, timeout_sec=0.5).get("ok"):
                            break
                    except Exception:
                        pass
                time.sleep(0.05)
            else:
                raise RuntimeError("Isolated VoiceInputDaemon socket did not become ready")

            # Subcheck 2: Real E2E pipeline (U1 cold+prewarm, then U2+U3 back-to-back serial queue)
            t_e2e0 = time.monotonic()
            debounce_wait = max(0.22, (cfg.hotkey.debounce_ms + 40) / 1000.0)

            # Utterance 1: Cold start + session prewarm + full pipeline
            r1_start = send_control_command(rt_dir, {"cmd": "rec_start"})
            if not r1_start.get("ok"):
                raise RuntimeError(f"U1 rec_start rejected: {r1_start}")
            time.sleep(0.05)
            r1_stop = send_control_command(rt_dir, {"cmd": "rec_stop"})
            if not r1_stop.get("ok"):
                raise RuntimeError(f"U1 rec_stop rejected: {r1_stop}")
            _wait_daemon_idle(rt_dir, expected_results=1)

            # Utterance 2 & 3: Back-to-back serial pipeline queueing!
            time.sleep(debounce_wait)
            r2_start = send_control_command(rt_dir, {"cmd": "rec_start"})
            if not r2_start.get("ok"):
                raise RuntimeError(f"U2 rec_start rejected: {r2_start}")
            time.sleep(0.05)
            r2_stop = send_control_command(rt_dir, {"cmd": "rec_stop"})
            if not r2_stop.get("ok"):
                raise RuntimeError(f"U2 rec_stop rejected: {r2_stop}")

            # Immediately start & stop U3 while U2 is still running in the background worker!
            time.sleep(debounce_wait)
            st_mid = send_control_command(rt_dir, {"cmd": "status"}, timeout_sec=2.0)
            r3_start = send_control_command(rt_dir, {"cmd": "rec_start"})
            if not r3_start.get("ok"):
                raise RuntimeError(f"U3 rec_start rejected: {r3_start}")
            time.sleep(0.05)
            r3_stop = send_control_command(rt_dir, {"cmd": "rec_stop"})
            if not r3_stop.get("ok"):
                raise RuntimeError(f"U3 rec_stop rejected: {r3_stop}")
            st_queued = send_control_command(rt_dir, {"cmd": "status"}, timeout_sec=2.0)
            peak_pending = max(
                int(st_mid.get("pending_count") or 0),
                int(st_queued.get("pending_count") or 0),
            )

            st_after_3 = _wait_daemon_idle(rt_dir, expected_results=3)
            e2e_ms = max(0, int(round((time.monotonic() - t_e2e0) * 1000)))

            # Read back real terminal text from isolated tmux pane
            time.sleep(0.08)
            cap = subprocess.run(
                [tmux_bin, "-L", sock_name, "capture-pane", "-p", "-J", "-S", "-", "-t", pane_id],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                env=tmux_clean_env,
                timeout=3.0,
                check=True,
            )
            pane_lines = [ln.strip() for ln in cap.stdout.splitlines() if ln.strip()]
            pane_full = "\n".join(pane_lines)
            pane_compact = "".join(pane_full.split())

            recs_3 = [r for r in (st_after_3.get("recent_results") or []) if int(r.get("seq_id") or 0) in (1, 2, 3)]
            recs_by_seq = {int(r["seq_id"]): r for r in recs_3}

            e2e_issues: list[str] = []
            warm_cache_hits: list[float] = []
            last_compact_pos = -1

            for idx, item in enumerate(utterance_items, start=1):
                rec = recs_by_seq.get(idx, {})
                asr_txt = asr_proxy.asr_texts[idx - 1] if (idx - 1) < len(asr_proxy.asr_texts) else ""
                fin_txt = platform.delivered_by_seq.get(idx, "")
                fin_norm = "\n".join(ln.strip() for ln in fin_txt.splitlines() if ln.strip())
                fin_compact = "".join(fin_txt.split())
                matched_pane_line = next((ln for ln in pane_lines if fin_txt and fin_txt in ln), "")
                if not matched_pane_line and fin_norm and fin_norm in pane_full:
                    matched_pane_line = fin_norm
                elif not matched_pane_line and fin_compact and fin_compact in pane_compact:
                    matched_pane_line = next((ln for ln in pane_lines if fin_compact in "".join(ln.split())), fin_norm)
                row_issues: list[str] = []

                if not rec.get("ok"):
                    row_issues.append(f"ok=False({rec.get('skipped_reason') or rec.get('error')})")
                if not rec.get("pasted"):
                    row_issues.append("pasted=False")
                if int(rec.get("asr_ms") or 0) <= 0:
                    row_issues.append("asr_ms<=0")
                if int(rec.get("ctx_chunks") or 0) < 2 or int(rec.get("ctx_chars") or 0) < 50_000:
                    row_issues.append(f"ctx_low({rec.get('ctx_chunks')}/{rec.get('ctx_chars')})")

                in_tok = rec.get("input_tokens")
                c_tok = rec.get("cached_input_tokens")
                hit_pct = (100.0 * c_tok / in_tok) if (isinstance(in_tok, int) and in_tok > 0 and isinstance(c_tok, int)) else None
                if expects_stage2:
                    if int(rec.get("refine_ms") or 0) <= 0:
                        row_issues.append("refine_ms<=0")
                    if not isinstance(in_tok, int) or in_tok <= 0:
                        row_issues.append("input_tokens<=0")
                    if rec.get("refiner_fallback"):
                        row_issues.append(f"fallback={rec.get('refiner_fallback')}")
                    if idx >= 2 and hit_pct is not None:
                        warm_cache_hits.append(hit_pct)

                if not _pane_contains_delivered(fin_txt, pane_full):
                    row_issues.append("missing_in_tmux_pane")
                elif fin_compact:
                    pos = pane_compact.find(fin_compact, max(0, last_compact_pos))
                    if pos < last_compact_pos:
                        row_issues.append("tmux_order_mismatch")
                    else:
                        last_compact_pos = pos + len(fin_compact)

                if used_custom_wav:
                    for priv_s in (asr_txt, fin_txt):
                        if len(priv_s) >= 6:
                            private_replay_prose.append(priv_s)
                else:
                    for sub in item.get("expect_substrings", ()):
                        if sub.lower() not in fin_txt.lower():
                            row_issues.append(f"missing({sub})")
                    if item.get("is_question"):
                        if ("?" not in fin_txt and "？" not in fin_txt) or len(fin_txt) > len(asr_txt) * 2 + 25:
                            row_issues.append("question_answered")
                    if item.get("is_correction"):
                        if "三十" in fin_txt or "30" in fin_txt or "不对不对" in fin_txt or ("90" not in fin_txt and "九十" not in fin_txt):
                            row_issues.append("correction_unresolved")

                if row_issues:
                    e2e_issues.append(f"{item['id']}:{','.join(row_issues)}")

                utterance_reports.append(
                    {
                        "id": item["id"],
                        "name": item["name"],
                        "seq_id": idx,
                        "ok": not row_issues,
                        "issues": row_issues,
                        "audio_sec": rec.get("raw_audio_sec", item["duration_sec"]),
                        "sync_ms": int(rec.get("sync_ms") or 0),
                        "asr_ms": int(rec.get("asr_ms") or 0),
                        "refine_init_ms": int(rec.get("refine_init_ms") or 0),
                        "refine_ms": int(rec.get("refine_ms") or 0),
                        "deliver_ms": int(rec.get("deliver_ms") or 0),
                        "total_ms": int(rec.get("total_ms") or 0),
                        "ctx_chunks": int(rec.get("ctx_chunks") or 0),
                        "ctx_chars": int(rec.get("ctx_chars") or 0),
                        "input_tokens": in_tok,
                        "cached_input_tokens": c_tok,
                        "cache_hit_pct": round(hit_pct, 1) if hit_pct is not None else None,
                        "asr_text": (
                            f"<redacted external wav: {len(asr_txt)} chars>"
                            if used_custom_wav
                            else redact_fn(asr_txt)
                        ),
                        "refined_text": (
                            f"<redacted external wav: {len(fin_txt)} chars>"
                            if used_custom_wav
                            else redact_fn(fin_txt)
                        ),
                        "tmux_line": (
                            f"<redacted external wav: {len(matched_pane_line)} chars>"
                            if used_custom_wav
                            else redact_fn(matched_pane_line)
                        ),
                    }
                )

            if expects_stage2 and not any(
                isinstance(recs_by_seq.get(i, {}).get("cached_input_tokens"), int)
                and int(recs_by_seq[i]["cached_input_tokens"]) > 0
                for i in (2, 3)
            ):
                e2e_issues.append("warm_cached_input_tokens==0")

            warm_hit_note = (
                f"warm_hit_avg={sum(warm_cache_hits) / len(warm_cache_hits):.1f}%"
                if warm_cache_hits
                else "warm_hit=n/a"
            )
            checks.append(
                {
                    "id": "C2_serial_e2e_tmux",
                    "ok": not e2e_issues,
                    "elapsed_ms": e2e_ms,
                    "detail": (
                        f"utterances=3/3, peak_queue_pending={peak_pending}, {warm_hit_note}, "
                        f"tmux_lines={len(pane_lines)}"
                        + (f", issues={'; '.join(e2e_issues)}" if e2e_issues else "")
                    ),
                }
            )

            # Subcheck 5: Isolated MP3 archive roundtrip + optional real saved recordings replay (strict zero-prose privacy!)
            t_mp30 = time.monotonic()
            daemon.wait_pending_archives(timeout_sec=4.0)
            iso_rec_dir = resolve_archive_audio_dir(e2e_env, create=False)
            iso_archived = list_saved_recordings(iso_rec_dir)
            iso_mp3_ok = (
                len(iso_archived) == 3
                and all(p.suffix.lower() == ".mp3" for p in iso_archived)
                and all((os.lstat(p).st_mode & 0o777) == 0o600 for p in iso_archived)
            )
            restored_iso_wav = b""
            if iso_archived:
                try:
                    restored_iso_wav = ensure_16k_mono_s16_wav(iso_archived[0].read_bytes())
                except Exception:
                    restored_iso_wav = b""
            restored_iso_gate = process_wav(restored_iso_wav, cfg.audio) if restored_iso_wav else None
            iso_restore_ok = bool(restored_iso_gate is not None and restored_iso_gate.passed and not restored_iso_gate.all_zero)

            replay_targets: list[tuple[str, str, bytes]] = []
            replay_issues: list[str] = []
            if saved_limit <= 0:
                host_replay_status = "SKIPPED (--saved-limit=0)"
            elif host_saved_selected:
                host_replay_status = f"REPLAYED ({len(host_saved_selected)}/{len(host_saved_all)} host files)"
                for s_idx, sp in enumerate(host_saved_selected, start=1):
                    rep_id = f"S{s_idx}"
                    rep_src = f"host{sp.suffix.lower()}"
                    try:
                        raw_bytes = sp.read_bytes()
                        decoded_wav = ensure_16k_mono_s16_wav(raw_bytes)
                        if len(decoded_wav) <= 44:
                            raise ValueError("empty_wav")
                    except Exception:
                        replay_issues.append(f"{rep_id}:decode_failed")
                        saved_replay_reports.append(
                            {
                                "id": rep_id,
                                "source": rep_src,
                                "seq_id": 0,
                                "ok": False,
                                "issues": ["decode_failed"],
                                "audio_sec": 0.0,
                                "sync_ms": 0,
                                "asr_ms": 0,
                                "refine_init_ms": 0,
                                "refine_ms": 0,
                                "deliver_ms": 0,
                                "total_ms": 0,
                                "ctx_chunks": 0,
                                "ctx_chars": 0,
                                "input_tokens": None,
                                "cached_input_tokens": None,
                                "cache_hit_pct": None,
                                "asr_chars": 0,
                                "final_chars": 0,
                                "tmux_verified": False,
                            }
                        )
                        continue
                    replay_targets.append((rep_id, rep_src, decoded_wav))
            else:
                host_replay_status = "SKIPPED (0 saved recordings yet)"
                if restored_iso_wav:
                    replay_targets.append(("M1", "iso_mp3_roundtrip", restored_iso_wav))

            expected_ok_records = 3
            for rep_id, rep_src, rep_wav_bytes in replay_targets:
                expected_ok_records += 1
                seq_num = expected_ok_records
                platform.enqueue_wav(rep_wav_bytes)
                time.sleep(debounce_wait)
                rs_start = send_control_command(rt_dir, {"cmd": "rec_start", "silent_hud": True})
                time.sleep(0.04)
                rs_stop = send_control_command(rt_dir, {"cmd": "rec_stop", "silent_hud": True})
                st_rep = _wait_daemon_idle(rt_dir, expected_results=seq_num)
                daemon.wait_pending_archives(timeout_sec=2.0)

                time.sleep(0.06)
                cap_rep = subprocess.run(
                    [tmux_bin, "-L", sock_name, "capture-pane", "-p", "-J", "-S", "-", "-t", pane_id],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    env=tmux_clean_env,
                    timeout=3.0,
                    check=True,
                )
                rep_pane_full = "\n".join(ln.strip() for ln in cap_rep.stdout.splitlines() if ln.strip())
                rec_rep = next(
                    (r for r in (st_rep.get("recent_results") or []) if int(r.get("seq_id") or 0) == seq_num),
                    {},
                )
                rep_asr_txt = asr_proxy.asr_texts[seq_num - 1] if (seq_num - 1) < len(asr_proxy.asr_texts) else ""
                rep_fin_txt = platform.delivered_by_seq.get(seq_num, "")
                for priv_s in (rep_asr_txt, rep_fin_txt):
                    if len(priv_s) >= 6:
                        private_replay_prose.append(priv_s)

                tmux_hit = _pane_contains_delivered(rep_fin_txt, rep_pane_full)
                r_issues: list[str] = []
                if not rs_start.get("ok") or not rs_stop.get("ok"):
                    r_issues.append("trigger_failed")
                if not rec_rep.get("ok"):
                    r_issues.append(f"ok=False({rec_rep.get('skipped_reason') or rec_rep.get('error')})")
                if not rec_rep.get("pasted"):
                    r_issues.append("pasted=False")
                if not tmux_hit:
                    r_issues.append("missing_in_tmux_pane")
                if r_issues:
                    replay_issues.append(f"{rep_id}:{','.join(r_issues)}")

                in_tok_r = rec_rep.get("input_tokens")
                c_tok_r = rec_rep.get("cached_input_tokens")
                hit_pct_r = (
                    round(100.0 * c_tok_r / in_tok_r, 1)
                    if (isinstance(in_tok_r, int) and in_tok_r > 0 and isinstance(c_tok_r, int))
                    else None
                )
                # Strictly numeric-only report entry: NEVER store or print real spoken prose!
                saved_replay_reports.append(
                    {
                        "id": rep_id,
                        "source": rep_src,
                        "seq_id": seq_num,
                        "ok": not r_issues,
                        "issues": r_issues,
                        "audio_sec": float(rec_rep.get("raw_audio_sec") or 0.0),
                        "sync_ms": int(rec_rep.get("sync_ms") or 0),
                        "asr_ms": int(rec_rep.get("asr_ms") or 0),
                        "refine_init_ms": int(rec_rep.get("refine_init_ms") or 0),
                        "refine_ms": int(rec_rep.get("refine_ms") or 0),
                        "deliver_ms": int(rec_rep.get("deliver_ms") or 0),
                        "total_ms": int(rec_rep.get("total_ms") or 0),
                        "ctx_chunks": int(rec_rep.get("ctx_chunks") or 0),
                        "ctx_chars": int(rec_rep.get("ctx_chars") or 0),
                        "input_tokens": in_tok_r,
                        "cached_input_tokens": c_tok_r,
                        "cache_hit_pct": hit_pct_r,
                        "asr_chars": int(rec_rep.get("asr_chars") or len(rep_asr_txt)),
                        "final_chars": int(rec_rep.get("final_chars") or len(rep_fin_txt)),
                        "tmux_verified": tmux_hit,
                    }
                )

            mp3_ms = max(0, int(round((time.monotonic() - t_mp30) * 1000)))
            mp3_all_ok = bool(iso_mp3_ok and iso_restore_ok and not replay_issues)
            checks.append(
                {
                    "id": "C5_mp3_archive_and_saved_replay",
                    "ok": mp3_all_ok,
                    "elapsed_ms": mp3_ms,
                    "detail": (
                        f"iso_mp3_0600={iso_mp3_ok}(count={len(iso_archived)}), "
                        f"ffmpeg_restore_16k={iso_restore_ok}, host_saved={host_replay_status}"
                        + (f", issues={'; '.join(replay_issues)}" if replay_issues else "")
                    ),
                }
            )

            # Subcheck 6: Non-Git plain folder 2-turn context + SSH foreground crosstalk veto
            t_ctx0 = time.monotonic()
            orig_fg_win = platform.get_foreground_window()
            ctx_iso_issues: list[str] = []
            non_git_dir, ng_sid, ng_rollout = _create_isolated_non_git_workspace_and_session(
                Path(td),
                rt_dir,
                tmux_socket=tmux_sock_path,
                tmux_pane=pane_id,
                pane_tty=pane_tty,
            )
            if (non_git_dir / ".git").exists():
                ctx_iso_issues.append("unexpected_dot_git")

            def _run_daemon_probe_utterance(wav_bytes: bytes) -> tuple[int, dict[str, Any], str, str]:
                nonlocal expected_ok_records
                expected_ok_records += 1
                seq_n = expected_ok_records
                platform.enqueue_wav(wav_bytes)
                time.sleep(debounce_wait)
                p_start = send_control_command(rt_dir, {"cmd": "rec_start", "silent_hud": True})
                time.sleep(0.04)
                p_stop = send_control_command(rt_dir, {"cmd": "rec_stop", "silent_hud": True})
                st_done = _wait_daemon_idle(rt_dir, expected_results=seq_n)
                daemon.wait_pending_archives(timeout_sec=2.0)
                rec_n = next(
                    (r for r in (st_done.get("recent_results") or []) if int(r.get("seq_id") or 0) == seq_n),
                    {},
                )
                if not p_start.get("ok") or not p_stop.get("ok"):
                    rec_n = {**rec_n, "ok": False, "error": "trigger_failed"}
                asr_n = asr_proxy.asr_texts[seq_n - 1] if (seq_n - 1) < len(asr_proxy.asr_texts) else ""
                fin_n = platform.delivered_by_seq.get(seq_n, "")
                return seq_n, rec_n, asr_n, fin_n

            ng1_chars = 0
            ng2_chars = 0
            shallow_extracted = False
            ssh_veto_ok = False
            try:
                # 6a. Non-Git Turn 1 (NG1): bind plain folder session, extract shallow filenames, refine & paste
                subprocess.run(
                    [tmux_bin, "-L", sock_name, "select-pane", "-t", pane_id, "-T", f"codex · non-git-workspace [#{ng_sid[:8]}]"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=tmux_clean_env,
                    timeout=2.0,
                    check=False,
                )
                platform.set_foreground_window(
                    ForegroundWindow(
                        hwnd=5151,
                        process="WindowsTerminal",
                        title=f"codex · non-git-workspace [codex:#{ng_sid[:8]}]",
                        tty=pane_tty,
                        cwd=str(non_git_dir),
                    )
                )
                ng1_wav = Path(non_git_item["wav_path"]).read_bytes()
                _ng1_seq, rec_ng1, asr_ng1, fin_ng1 = _run_daemon_probe_utterance(ng1_wav)
                for priv_s in (asr_ng1, fin_ng1):
                    if len(priv_s) >= 6:
                        private_replay_prose.append(priv_s)

                # Append a second conversation turn to non-Git rollout and run Non-Git Turn 2 (NG2)
                _append_codex_turn(
                    ng_rollout,
                    2,
                    "请把 custom_widget_spec.md 里的字段跟 budget_q4_plan.xlsx 对齐。",
                    "已完成字段映射核对，等待下一条语音指令。\n"
                    + ("# non-git turn-2 incremental context growth line\n" * 60),
                )
                ng2_wav = Path(utterance_items[1]["wav_path"]).read_bytes()
                _ng2_seq, rec_ng2, asr_ng2, fin_ng2 = _run_daemon_probe_utterance(ng2_wav)
                for priv_s in (asr_ng2, fin_ng2):
                    if len(priv_s) >= 6:
                        private_replay_prose.append(priv_s)

                ng_session_key = f"codex:{ng_sid}"
                ng_chunks, ng_git_ctx = tracker.get_refiner_context(ng_session_key)
                ng_ws_hotwords = tracker.build_workspace_hotwords(str(non_git_dir))
                shallow_extracted = (
                    "workspace_files (local folder; spelling reference only):" in ng_git_ctx
                    and "budget_q4_plan.xlsx" in ng_git_ctx
                    and "custom_widget_spec.md" in ng_git_ctx
                    and "git_status:" not in ng_git_ctx
                    and "repo_files" not in ng_git_ctx
                    and "budget_q4_plan" in ng_ws_hotwords
                    and "custom_widget_spec" in ng_ws_hotwords
                )
                if not rec_ng1.get("ok") or not rec_ng1.get("pasted"):
                    ctx_iso_issues.append("NG1:failed_or_unpasted")
                if not rec_ng2.get("ok") or not rec_ng2.get("pasted"):
                    ctx_iso_issues.append("NG2:failed_or_unpasted")
                if rec_ng1.get("session_key") != ng_session_key or rec_ng2.get("session_key") != ng_session_key:
                    ctx_iso_issues.append(
                        f"non_git_session_mismatch({rec_ng1.get('session_key')},{rec_ng2.get('session_key')})"
                    )
                if not shallow_extracted:
                    ctx_iso_issues.append("non_git_shallow_files_missing")
                ng1_chars = int(rec_ng1.get("ctx_chars") or 0)
                ng2_chars = int(rec_ng2.get("ctx_chars") or 0)
                if expects_stage2 and not (ng2_chars > ng1_chars > 0):
                    ctx_iso_issues.append(f"non_git_multiturn_ctx_not_grown({ng1_chars}->{ng2_chars})")
                elif not ng_chunks or sum(len(c) for c in ng_chunks) <= 0:
                    ctx_iso_issues.append("non_git_chunks_empty")
                if not used_custom_wav:
                    for sub in non_git_item.get("expect_substrings", ()):
                        if sub.lower() not in fin_ng1.lower():
                            ctx_iso_issues.append(f"NG1:missing({sub})")

                time.sleep(0.06)
                cap_ng = subprocess.run(
                    [tmux_bin, "-L", sock_name, "capture-pane", "-p", "-J", "-S", "-", "-t", pane_id],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    env=tmux_clean_env,
                    timeout=3.0,
                    check=True,
                )
                ng_pane_full = "\n".join(ln.strip() for ln in cap_ng.stdout.splitlines() if ln.strip())
                if not _pane_contains_delivered(fin_ng1, ng_pane_full) or not _pane_contains_delivered(fin_ng2, ng_pane_full):
                    ctx_iso_issues.append("non_git_missing_in_tmux")

                # 6b. SSH foreground crosstalk veto while both local Git & non-Git sessions remain live!
                # Case 1: Terminal SSH prompt title (`ubuntu@prod-bastion.invalid:~`)
                subprocess.run(
                    [tmux_bin, "-L", sock_name, "select-pane", "-t", pane_id, "-T", "ubuntu@prod-bastion.invalid:~"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=tmux_clean_env,
                    timeout=2.0,
                    check=False,
                )
                platform.set_foreground_window(
                    ForegroundWindow(
                        hwnd=6161,
                        process="WindowsTerminal",
                        title="ubuntu@prod-bastion.invalid:~",
                        tty="",
                        cwd="",
                    )
                )
                _ssh1_seq, rec_ssh1, asr_ssh1, fin_ssh1 = _run_daemon_probe_utterance(ng2_wav)
                for priv_s in (asr_ssh1, fin_ssh1):
                    if len(priv_s) >= 6:
                        private_replay_prose.append(priv_s)

                # Case 2: Dedicated SSH GUI client (`putty.exe`)
                platform.set_foreground_window(
                    ForegroundWindow(
                        hwnd=6262,
                        process="putty.exe",
                        title="ubuntu@prod-bastion.invalid - PuTTY",
                    )
                )
                _ssh2_seq, rec_ssh2, asr_ssh2, fin_ssh2 = _run_daemon_probe_utterance(ng2_wav)
                for priv_s in (asr_ssh2, fin_ssh2):
                    if len(priv_s) >= 6:
                        private_replay_prose.append(priv_s)

                ssh_veto_ok = (
                    bool(rec_ssh1.get("ok"))
                    and bool(rec_ssh2.get("ok"))
                    and rec_ssh1.get("session_key") == ""
                    and rec_ssh2.get("session_key") == ""
                    and int(rec_ssh1.get("ctx_chars") or 0) == 0
                    and int(rec_ssh2.get("ctx_chars") or 0) == 0
                    and int(rec_ssh1.get("ctx_chunks") or 0) == 0
                    and int(rec_ssh2.get("ctx_chunks") or 0) == 0
                )
                if not ssh_veto_ok:
                    ctx_iso_issues.append(
                        f"ssh_crosstalk_leak(term={rec_ssh1.get('session_key')!r}/{rec_ssh1.get('ctx_chars')},"
                        f"putty={rec_ssh2.get('session_key')!r}/{rec_ssh2.get('ctx_chars')})"
                    )
            finally:
                platform.set_foreground_window(orig_fg_win)
                subprocess.run(
                    [tmux_bin, "-L", sock_name, "select-pane", "-t", pane_id, "-T", f"codex · avi [#{sid[:8]}]"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=tmux_clean_env,
                    timeout=2.0,
                    check=False,
                )

            ctx_iso_ms = max(0, int(round((time.monotonic() - t_ctx0) * 1000)))
            checks.append(
                {
                    "id": "C6_non_git_and_ssh_isolation",
                    "ok": not ctx_iso_issues,
                    "elapsed_ms": ctx_iso_ms,
                    "detail": (
                        f"non_git_2turn=({ng1_chars}->{ng2_chars} chars, shallow_files={shallow_extracted}), "
                        f"ssh_veto_terminal_and_putty={ssh_veto_ok}"
                        + (f", issues={'; '.join(ctx_iso_issues)}" if ctx_iso_issues else "")
                    ),
                }
            )

            # Subcheck 4 (run before final diag audit so diag.jsonl + diagnose_live cover both normal & fault records)
            expected_total_records = expected_ok_records
            if not args.skip_fault_check:
                t_flt0 = time.monotonic()
                fault_seq = expected_ok_records + 1
                expected_total_records = fault_seq
                platform.enqueue_wav(Path(utterance_items[0]["wav_path"]).read_bytes())
                asr_proxy.fail_error_msg = f"simulated_upstream_failure Bearer {canary_secret}"
                time.sleep(debounce_wait)
                f_start = send_control_command(rt_dir, {"cmd": "rec_start", "silent_hud": True})
                time.sleep(0.04)
                f_stop = send_control_command(rt_dir, {"cmd": "rec_stop", "silent_hud": True})
                st_after_4 = _wait_daemon_idle(rt_dir, expected_results=fault_seq)
                asr_proxy.fail_error_msg = ""

                failed_wavs = list_failed_audios(rt_dir)
                failed_mode_ok = bool(
                    failed_wavs
                    and all((os.lstat(p).st_mode & 0o777) == 0o600 for p in failed_wavs)
                )
                rec4 = next(
                    (r for r in (st_after_4.get("recent_results") or []) if int(r.get("seq_id") or 0) == fault_seq),
                    {},
                )
                err_txt = str(rec4.get("error") or "")
                redacted_ok = (canary_secret not in err_txt) and ("[REDACTED]" in err_txt)

                # Also verify diagnose_live.py --json against this live isolated runtime_dir
                diag_proc = subprocess.run(
                    [
                        sys.executable,
                        str(_REPO_ROOT / ".agents" / "skills" / "voice-eval" / "scripts" / "diagnose_live.py"),
                        "--runtime-dir",
                        str(rt_dir),
                        "--json",
                    ],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    env=e2e_env,
                    timeout=10.0,
                    check=False,
                )
                diag_raw = diag_proc.stdout or ""
                diag_obj = json.loads(diag_raw) if diag_proc.returncode == 0 and diag_raw.strip() else {}
                diag_recs = diag_obj.get("recent_results") or []
                diag_rec4 = next((r for r in diag_recs if int(r.get("seq_id") or 0) == fault_seq), {})
                diag_clean = (
                    diag_proc.returncode == 0
                    and canary_secret not in diag_raw
                    and str(rt_dir) not in diag_raw
                    and "saved_audio" not in diag_rec4
                    and diag_rec4.get("has_saved_audio") is True
                )
                flt_ok = bool(
                    f_start.get("ok")
                    and f_stop.get("ok")
                    and rec4.get("ok") is False
                    and failed_mode_ok
                    and redacted_ok
                    and diag_clean
                )
                flt_ms = max(0, int(round((time.monotonic() - t_flt0) * 1000)))
                checks.append(
                    {
                        "id": "C4_fault_recovery_redaction",
                        "ok": flt_ok,
                        "elapsed_ms": flt_ms,
                        "detail": (
                            f"failed_wav_0600={failed_mode_ok}(count={len(failed_wavs)}), "
                            f"secret_redacted={redacted_ok}, diagnose_json_clean={diag_clean}"
                        ),
                    }
                )

            # Subcheck 3: CLI `status --history` & `diag.jsonl` (0600, complete metrics, zero spoken prose)
            t_tel0 = time.monotonic()
            tel_path = rt_dir / DIAG_FILENAME
            tel_st = os.lstat(tel_path) if tel_path.exists() else None
            tel_mode_0600 = bool(
                tel_st is not None
                and stat.S_ISREG(tel_st.st_mode)
                and (tel_st.st_mode & 0o777) == 0o600
            )
            tel_raw_text = tel_path.read_text(encoding="utf-8") if tel_mode_0600 else ""
            disk_records = read_diag_history(rt_dir, limit=50)

            st_cli_proc = subprocess.run(
                [
                    sys.executable,
                    str(_REPO_ROOT / "bin" / "agent-voice-input"),
                    "status",
                    "--history",
                    "50",
                    "--json",
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                env=e2e_env,
                timeout=8.0,
                check=False,
            )
            st_cli_raw = st_cli_proc.stdout or ""
            st_cli_obj = json.loads(st_cli_raw) if st_cli_proc.returncode == 0 and st_cli_raw.strip() else {}
            cli_recs = st_cli_obj.get("recent_results") or []

            required_latency_keys = (
                "gate_ms",
                "sync_ms",
                "asr_ms",
                "refine_init_ms",
                "refine_ms",
                "deliver_ms",
                "total_ms",
            )
            ok_recs = [r for r in disk_records if r.get("ok") is True]
            lat_keys_present = len(ok_recs) == expected_ok_records and all(
                all(k in r for k in required_latency_keys) for r in ok_recs
            )

            # Verify zero spoken prose & zero secrets in diag.jsonl and `status --history`
            forbidden_prose: list[str] = list(private_replay_prose)
            for item in (*utterance_items, non_git_item):
                forbidden_prose.extend(item.get("probe_phrases", ()))
            if not used_custom_wav:
                for u_rep in utterance_reports:
                    for full_s in (u_rep["asr_text"], u_rep["refined_text"]):
                        if len(full_s) >= 6:
                            forbidden_prose.append(full_s)

            combined_audit_blob = tel_raw_text + "\n" + st_cli_raw
            prose_leaked = any(p and p in combined_audit_blob for p in forbidden_prose)
            secret_leaked = any(sec in combined_audit_blob for sec in all_secrets)
            counts_match = (len(disk_records) == expected_total_records) and (len(cli_recs) == expected_total_records)
            tel_ok = bool(
                tel_mode_0600
                and st_cli_proc.returncode == 0
                and counts_match
                and lat_keys_present
                and not prose_leaked
                and not secret_leaked
            )
            tel_ms = max(0, int(round((time.monotonic() - t_tel0) * 1000)))
            checks.append(
                {
                    "id": "C3_diag_privacy_contract",
                    "ok": tel_ok,
                    "elapsed_ms": tel_ms,
                    "detail": (
                        f"mode_0600={tel_mode_0600}, records={len(disk_records)}/{expected_total_records}, "
                        f"latency_fields={lat_keys_present}, zero_prose={not prose_leaked}, zero_secrets={not secret_leaked}"
                    ),
                }
            )

            # Subcheck 7: Pure offscreen native HUD & shim geometry self-check (zero clipboard / desktop disruption)
            t_hud_sc0 = time.monotonic()

            def _validate_hud_self_check(
                ok_flag: bool, payload: Mapping[str, Any], err_msg: str, label: str
            ) -> tuple[bool, str]:
                if not ok_flag:
                    return False, f"{label} failed: {err_msg}"
                if payload.get("ok") is not True or payload.get("mode") != "self-check":
                    return (
                        False,
                        f"{label} unexpected payload: ok={payload.get('ok')!r}, mode={payload.get('mode')!r}",
                    )
                raw_cases = payload.get("hud_layout_cases")
                if isinstance(raw_cases, bool) or not isinstance(raw_cases, int) or raw_cases <= 0:
                    return False, f"{label} missing/invalid hud_layout_cases={raw_cases!r}"
                extra = f", cache_hit={payload.get('cache_hit')}" if "cache_hit" in payload else ""
                return True, f"{label} ok=True, hud_layout_cases={raw_cases}{extra}"

            if sys.platform == "darwin":
                shim_bin, shim_err = platform_macos.compile_mac_shim(
                    env=e2e_env, cache_dir=iso_cache_dir, cfg=cfg
                )
                if shim_bin is not None:
                    sc_ok_raw, sc_json, sc_err = platform_macos.run_mac_shim_self_check(
                        shim_bin, e2e_env, cfg=cfg, timeout=15.0
                    )
                    sc_ok, hud_sc_detail = _validate_hud_self_check(
                        sc_ok_raw, sc_json, sc_err, "mac_shim --self-check"
                    )
                else:
                    sc_ok = False
                    hud_sc_detail = f"mac_shim compile failed: {shim_err}"
            elif platform_wsl.is_wsl(clean_env):
                if not os.path.isfile(platform_wsl.POWERSHELL_EXE):
                    sc_ok = False
                    hud_sc_detail = (
                        f"WSL detected but PowerShell not found at {platform_wsl.POWERSHELL_EXE}"
                    )
                else:
                    win_cache_dir = iso_cache_dir / "win_shim_cache"
                    win_cache_dir.mkdir(parents=True, exist_ok=True)
                    sc_ok_raw, sc_json, sc_err = platform_wsl.run_win_shim_self_check(
                        "self-check", env=clean_env, cache_dir=win_cache_dir, timeout=25.0
                    )
                    sc_ok, hud_sc_detail = _validate_hud_self_check(
                        sc_ok_raw, sc_json, sc_err, "win_shim -Mode self-check"
                    )
            else:
                sc_ok = True
                hud_sc_detail = "skipped (no native Windows/macOS shim on pure Linux)"
            hud_sc_ms = max(0, int(round((time.monotonic() - t_hud_sc0) * 1000)))
            checks.append(
                {
                    "id": "C7_native_hud_self_check",
                    "ok": sc_ok,
                    "elapsed_ms": hud_sc_ms,
                    "detail": hud_sc_detail,
                }
            )
        finally:
            if daemon is not None:
                try:
                    send_control_command(rt_dir, {"cmd": "shutdown"}, timeout_sec=1.5)
                except Exception:
                    pass
                daemon.close()
            if daemon_thr is not None and daemon_thr.is_alive():
                daemon_thr.join(timeout=4.0)
            subprocess.run(
                [tmux_bin, "-L", sock_name, "kill-server"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=tmux_clean_env,
                timeout=3.0,
                check=False,
            )
            if tmux_sock_path:
                try:
                    Path(tmux_sock_path).unlink(missing_ok=True)
                except OSError:
                    pass

    # Keep checks ordered C1, C2, C3, C4
    checks.sort(key=lambda c: c["id"])
    suite_ok = all(c["ok"] for c in checks)
    suite_ms = max(0, int(round((time.monotonic() - t_suite0) * 1000)))

    summary_payload = {
        "ok": suite_ok,
        "version": __version__,
        "elapsed_ms": suite_ms,
        "host_daemon": host_daemon_info,
        "host_saved_replay_status": host_replay_status,
        "checks": checks,
        "utterances": utterance_reports,
        "saved_replays": saved_replay_reports,
    }

    if args.json:
        rendered = json.dumps(summary_payload, ensure_ascii=False, indent=2) + "\n"
    else:
        ts_str = time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime())
        md_lines = [
            f"# `agent-voice-input` End-to-End Live System Report (`v{__version__}`)",
            "",
            f"- Result: **`{'PASS' if suite_ok else 'FAIL'}`** (`{sum(1 for c in checks if c['ok'])}/{len(checks)}` checks passed in `{suite_ms}ms`)",
            f"- Timestamp: `{ts_str}` | Isolation: `<temp>/avi-e2e-*` + `tmux -L {sock_name}` (cleaned)",
            "",
            "## System Check Summary",
            "",
            "| Check | Status | Elapsed (ms) | Verification Detail |",
            "| :--- | :--- | ---: | :--- |",
        ]
        for c in checks:
            md_lines.append(
                f"| `{c['id']}` | `{'PASS' if c['ok'] else 'FAIL'}` | {c['elapsed_ms']} | {redact_fn(c['detail'])} |"
            )
        md_lines.extend(
            [
                "",
                "## Serial Utterance Latencies & Prefix Cache",
                "",
                "| ID | Audio(s) | Sync | ASR | Init | Refine | Deliv | Total(ms) | Ctx(ch/chr) | In / Cached (Hit%) | Status |",
                "| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :--- | :--- |",
            ]
        )
        for u in utterance_reports:
            in_t = u["input_tokens"]
            c_t = u["cached_input_tokens"]
            tok_str = (
                f"{in_t} / {c_t} ({u['cache_hit_pct']:.1f}%)"
                if (isinstance(in_t, int) and isinstance(c_t, int) and u["cache_hit_pct"] is not None)
                else (f"{in_t} / -" if isinstance(in_t, int) else "-")
            )
            st_label = "PASS" if u["ok"] else f"FAIL({','.join(u['issues'])})"
            md_lines.append(
                f"| {u['id']} (`{u['name']}`) | {u['audio_sec']:.2f} | {u['sync_ms']} | {u['asr_ms']} | "
                f"{u['refine_init_ms']} | {u['refine_ms']} | {u['deliver_ms']} | {u['total_ms']} | "
                f"{u['ctx_chunks']}/{u['ctx_chars']} | {tok_str} | {st_label} |"
            )
        md_lines.extend(
            [
                "",
                "## Stage 1 ASR -> Stage 2 Refiner -> Isolated `tmux` Readback",
                "",
                "| ID | ASR Draft | Refined Output | `tmux capture-pane` Readback |",
                "| :--- | :--- | :--- | :--- |",
            ]
        )
        for u in utterance_reports:
            md_lines.append(
                f"| {u['id']} | {_fmt_cell(u['asr_text'])} | {_fmt_cell(u['refined_text'])} | {_fmt_cell(u['tmux_line'])} |"
            )
        md_lines.extend(
            [
                "",
                f"## MP3 Archive & Saved Recording Replay (`{host_replay_status}` — Privacy-Safe Metrics Only)",
                "",
                "| ID | Source | Audio(s) | ASR | Refine | Deliv | Total(ms) | In / Cached (Hit%) | Chars (ASR->Final) | `tmux` Verified | Status |",
                "| :--- | :--- | ---: | ---: | ---: | ---: | ---: | :--- | :--- | :--- | :--- |",
            ]
        )
        for sr in saved_replay_reports:
            in_t = sr["input_tokens"]
            c_t = sr["cached_input_tokens"]
            tok_str = (
                f"{in_t} / {c_t} ({sr['cache_hit_pct']:.1f}%)"
                if (isinstance(in_t, int) and isinstance(c_t, int) and sr["cache_hit_pct"] is not None)
                else (f"{in_t} / -" if isinstance(in_t, int) else "-")
            )
            st_lbl = "PASS" if sr["ok"] else f"FAIL({','.join(sr['issues'])})"
            md_lines.append(
                f"| {sr['id']} | `{sr['source']}` | {sr['audio_sec']:.2f} | {sr['asr_ms']} | "
                f"{sr['refine_ms']} | {sr['deliver_ms']} | {sr['total_ms']} | {tok_str} | "
                f"{sr['asr_chars']} -> {sr['final_chars']} | `{sr['tmux_verified']}` | {st_lbl} |"
            )
        rendered = "\n".join(md_lines) + "\n"

    if args.out:
        out_p = Path(args.out).expanduser().resolve()
        _atomic_write_0600(out_p, rendered.encode("utf-8"))
    print(rendered, end="")
    return 0 if suite_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
