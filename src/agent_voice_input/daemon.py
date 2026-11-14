"""Core anti-reentrant two-stage state machine daemon and single-shot pipeline for agent-voice-input."""
from __future__ import annotations

from dataclasses import dataclass
import inspect, json, os, signal, socket, sys, threading, time
from pathlib import Path
from typing import Any, Callable, Mapping

from .asr_providers import AsrResult, BaseAsrProvider, create_asr_provider
from .audio_proc import AudioGateResult, process_wav
from .config import Config, ProviderCredentials, ensure_secure_dir, get_runtime_dir, redact_secrets, resolve_credentials, select_asr_provider
from .platform_macos import MacOSPlatform, is_macos_terminal_window
from .platform_wsl import ForegroundWindow, PasteOutcome, WslPlatform, is_windows_terminal_process
from .refiner import BaseRefiner, RefinerResult, create_refiner
from .session_ctx import SessionSnapshot, SessionTracker


@dataclass(frozen=True)
class PipelineResult:
    ok: bool; seq_id: int; final_text: str = ""; skipped_reason: str = ""; audio_source: str = ""
    switched_fallback: bool = False; gate: AudioGateResult | None = None; session: SessionSnapshot | None = None
    asr: AsrResult | None = None; refined: RefinerResult | None = None; paste: PasteOutcome | None = None; error: str = ""


def create_platform(cfg: Config, *, env: Mapping[str, str] | None = None) -> WslPlatform | MacOSPlatform:
    return MacOSPlatform(cfg, env=env) if sys.platform == "darwin" else WslPlatform(cfg, env=env)


def _window_from_mapping(m: Mapping[str, Any]) -> ForegroundWindow:
    return ForegroundWindow(
        hwnd=int(m.get("hwnd") or 0),
        process=str(m.get("process") or ""),
        title=str(m.get("title") or ""),
        bundle_id=str(m.get("bundle_id") or ""),
        surface_id=str(m.get("surface_id") or ""),
        tty=str(m.get("tty") or ""),
        cwd=str(m.get("cwd") or ""),
        pid=int(m.get("pid") or 0),
    )


def _deliver_with_origin(platform: Any, text: str, *, seq_id: int, origin_window: ForegroundWindow) -> PasteOutcome:
    try:
        params = inspect.signature(platform.deliver_text).parameters
    except (TypeError, ValueError):
        params = {}
    if "origin_window" in params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return platform.deliver_text(text, seq_id=seq_id, target_hwnd=origin_window.hwnd, origin_window=origin_window)
    return platform.deliver_text(text, seq_id=seq_id, target_hwnd=origin_window.hwnd)


def _will_run_stage2_refiner(cfg: Config, refiner: BaseRefiner, asr_res: AsrResult) -> bool:
    prov = cfg.refiner.provider.strip().lower()
    if prov in ("", "off", "none", "false", "0", "disabled"):
        return False
    if getattr(refiner, "provider_name", "off") in ("", "off", "none"):
        return False
    if asr_res.is_single_hop_refined and not cfg.refiner.force_on_single_hop:
        return False
    return True


def run_pipeline_once(
    raw_wav: bytes | str | Path, *, cfg: Config, creds: ProviderCredentials | None = None,
    tracker: SessionTracker | None = None, asr_provider: BaseAsrProvider | None = None,
    refiner: BaseRefiner | None = None, platform: Any = None, origin_window: ForegroundWindow | None = None,
    window_title_override: str | None = None,
    audio_source: str = "file", seq_id: int = 1, dry_run: bool = False,
    is_current_seq: Callable[[int], bool] | None = None,
    on_stage: Callable[[str], None] | None = None,
    env: Mapping[str, str] | None = None,
) -> PipelineResult:
    eff_env = os.environ if env is None else env
    eff_creds = creds or resolve_credentials(cfg, eff_env)
    eff_tracker = tracker or SessionTracker(env=eff_env)
    eff_asr = asr_provider or create_asr_provider(cfg, eff_creds, env=eff_env)
    owns_refiner = refiner is None
    eff_refiner = refiner or create_refiner(cfg, eff_creds, env=eff_env)
    try:
        if isinstance(raw_wav, (bytes, bytearray)) and len(raw_wav) == 0:
            sw = bool(platform and platform.switch_to_fallback_audio()) if audio_source in ("pulse", "winmm") else False
            return PipelineResult(False, seq_id, skipped_reason="empty_audio", audio_source=audio_source, switched_fallback=sw)
        gate = process_wav(raw_wav, cfg.audio)
        if not gate.passed:
            sw = bool(gate.all_zero and gate.reason == "silence" and audio_source == "pulse" and platform and platform.switch_to_fallback_audio())
            return PipelineResult(False, seq_id, skipped_reason=gate.reason, audio_source=audio_source, switched_fallback=sw, gate=gate)
        win = origin_window or (platform.get_foreground_window() if platform else ForegroundWindow())
        snap = (
            eff_tracker.sync_session(
                window_title=win.title,
                window_tty=win.tty,
                window_cwd=win.cwd,
                window_pid=win.pid,
            )
            if window_title_override is None
            else eff_tracker.sync_session(window_title=window_title_override)
        )
        asr_ctx = eff_tracker.build_asr_context(snap)
        asr_res = eff_asr.transcribe(gate.wav_bytes, context=asr_ctx)
        if not asr_res.text.strip():
            return PipelineResult(False, seq_id, skipped_reason="empty_asr", audio_source=audio_source, gate=gate, session=snap, asr=asr_res)
        if is_current_seq is not None and not is_current_seq(seq_id):
            return PipelineResult(False, seq_id, final_text=asr_res.text.strip(), skipped_reason="superseded", audio_source=audio_source, gate=gate, session=snap, asr=asr_res)
        if _will_run_stage2_refiner(cfg, eff_refiner, asr_res) and on_stage is not None:
            on_stage("refining")
        ref_res = eff_refiner.refine(asr_res, session_key=snap.session_key if snap else "", tracker=eff_tracker, fallback_context=asr_ctx.dialog_text)
        final_text = ref_res.text.strip() or asr_res.text.strip()
        if is_current_seq is not None and not is_current_seq(seq_id):
            return PipelineResult(False, seq_id, final_text=final_text, skipped_reason="superseded", audio_source=audio_source, gate=gate, session=snap, asr=asr_res, refined=ref_res)
        paste_out = None if (dry_run or platform is None) else _deliver_with_origin(platform, final_text, seq_id=seq_id, origin_window=win)
        return PipelineResult(True, seq_id, final_text=final_text, audio_source=audio_source, gate=gate, session=snap, asr=asr_res, refined=ref_res, paste=paste_out)
    finally:
        if owns_refiner:
            eff_refiner.close()


def send_control_command(runtime_dir: Path, payload: Mapping[str, Any], timeout_sec: float = 8.0) -> dict[str, Any]:
    sock_path = runtime_dir / "control.sock"
    if not sock_path.exists():
        raise RuntimeError(f"daemon socket not found at {sock_path}")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout_sec); s.connect(str(sock_path))
        s.sendall((json.dumps(dict(payload), ensure_ascii=False) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf and len(buf) < 65536 and (chunk := s.recv(16384)):
            buf += chunk
    return json.loads(buf.decode("utf-8", errors="replace").strip() or "{}")


class VoiceInputDaemon:
    def __init__(self, cfg: Config, *, env: Mapping[str, str] | None = None, runtime_dir: Path | None = None, platform: Any = None, asr_provider: BaseAsrProvider | None = None, refiner: BaseRefiner | None = None, tracker: SessionTracker | None = None, clock_fn: Callable[[], float] = time.monotonic) -> None:
        self.cfg, self.env = cfg, dict(os.environ if env is None else env)
        self.creds = resolve_credentials(cfg, self.env)
        self.runtime_dir = ensure_secure_dir(runtime_dir or get_runtime_dir(self.env, create=True))
        self.platform = platform or create_platform(cfg, env=self.env)
        self.tracker = tracker or SessionTracker(env=self.env, runtime_dir=self.runtime_dir)
        self.asr_provider = asr_provider or create_asr_provider(cfg, self.creds, env=self.env)
        self.refiner = refiner or create_refiner(cfg, self.creds, env=self.env, runtime_dir=self.runtime_dir)
        self.clock_fn, self._trigger_lock, self._lock, self._stop_evt = clock_fn, threading.Lock(), threading.Lock(), threading.Event()
        self.state, self.seq_id, self._last_trigger_ts = "idle", 0, -1e9
        self._origin_win, self.last_result, self._srv_sock = ForegroundWindow(), {}, None
        self._cur_no_paste, self._cur_silent_hud = False, False

    @property
    def pid_path(self) -> Path: return self.runtime_dir / "daemon.pid"
    @property
    def sock_path(self) -> Path: return self.runtime_dir / "control.sock"

    def is_current_seq(self, seq_id: int) -> bool:
        with self._lock:
            return not self._stop_evt.is_set() and self.seq_id == seq_id

    def _redact_str(self, text: str) -> str:
        return redact_secrets(text, self.env, cfg=self.cfg, extra_secrets=self.creds.all_secret_values())

    def _redact(self, exc: Exception) -> str:
        return self._redact_str(str(exc))

    def _hud(self, state: str) -> None:
        if (fn := getattr(self.platform, "hud", None)) is not None:
            try: fn(state)
            except Exception: pass

    def _on_pipeline_stage(self, seq_id: int, stage: str, silent_hud: bool = False) -> None:
        if not self.is_current_seq(seq_id):
            return
        with self._trigger_lock:
            emit = False
            with self._lock:
                if not self._stop_evt.is_set() and self.seq_id == seq_id and self.state in ("recognizing", "processing"):
                    self.state = stage
                    emit = not silent_hud
            if emit:
                self._hud(stage)

    def _build_summary(self, seq_id: int, res: PipelineResult, *, dry_run: bool = False) -> dict[str, Any]:
        summary: dict[str, Any] = {"seq_id": seq_id, "ok": res.ok, "skipped_reason": res.skipped_reason, "audio_source": res.audio_source, "switched_fallback": res.switched_fallback, "session_key": res.session.session_key if res.session else "", "asr_provider": res.asr.provider if res.asr else "", "refiner_fallback": res.refined.used_fallback if res.refined else "", "pasted": res.paste.pasted if res.paste else False, "paste_reason": res.paste.reason if res.paste else ("dry_run" if dry_run and res.ok else ""), "raw_audio_sec": round(res.gate.duration_sec, 3) if res.gate else 0.0, "voiced_audio_sec": round(res.gate.voiced_sec, 3) if res.gate else 0.0, "asr_ms": res.asr.latency_ms if res.asr else 0, "refine_ms": res.refined.latency_ms if res.refined else 0, "asr_chars": len(res.asr.text.strip()) if res.asr else 0, "final_chars": len(res.final_text)}
        if dry_run:
            summary["dry_run"] = True
        if not res.ok and (audio_err := str(getattr(self.platform, "last_audio_error", "") or "")):
            summary["audio_error"] = self._redact_str(audio_err)
        return summary

    def handle_trigger(self, action: str, window: ForegroundWindow | None = None, *, no_paste: bool = False, silent_hud: bool = False, loopback: bool = False, from_hotkey: bool = False) -> dict[str, Any]:
        arrival_ts = self.clock_fn()
        with self._trigger_lock:
            with self._lock:
                if self._stop_evt.is_set():
                    return {"ok": False, "ignored": "stopping", "state": self.state, "seq_id": self.seq_id}
                eff_no_paste, eff_silent = bool(no_paste), bool(silent_hud)
                if (
                    action in ("toggle", "ptt_start")
                    and self.state != "recording"
                    and self.cfg.hotkey.only_in_terminals
                    and not eff_no_paste
                    and (from_hotkey or window is not None)
                ):
                    cand_win = window if window is not None else ForegroundWindow()
                    if (checker := getattr(self.platform, "is_allowed_terminal", None)) is not None:
                        allowed = bool(checker(cand_win))
                    else:
                        allowed = is_windows_terminal_process(cand_win.process) or is_macos_terminal_window(cand_win.process, cand_win.bundle_id)
                    if not allowed:
                        return {"ok": False, "ignored": "non_terminal", "state": self.state, "seq_id": self.seq_id}
                if action in ("toggle", "ptt_start") and (arrival_ts - self._last_trigger_ts) * 1000.0 < self.cfg.hotkey.debounce_ms:
                    return {"ok": False, "ignored": "debounced", "state": self.state, "seq_id": self.seq_id}
                now = self.clock_fn()
                if action == "ptt_cancel":
                    if self.state != "recording":
                        return {"ok": True, "state": self.state, "seq_id": self.seq_id}
                    eff_silent = bool(silent_hud or self._cur_silent_hud)
                    self._cur_no_paste, self._cur_silent_hud = False, False
                    self.seq_id += 1; self.state, self._last_trigger_ts, cur_seq, mode = "idle", now, self.seq_id, "cancel"
                elif action in ("toggle", "ptt_start") and self.state != "recording":
                    self._cur_no_paste, self._cur_silent_hud = bool(no_paste), bool(silent_hud)
                    self.seq_id += 1; self.state, self._last_trigger_ts, cur_seq, mode = "recording", now, self.seq_id, "start"
                elif action in ("toggle", "ptt_stop") and self.state == "recording":
                    # Stage 1 ('recognizing') starts immediately on the very first line of stopping recording
                    self.state = "recognizing"
                    eff_no_paste = bool(no_paste or self._cur_no_paste)
                    eff_silent = bool(silent_hud or self._cur_silent_hud)
                    self._cur_no_paste, self._cur_silent_hud = False, False
                    self._last_trigger_ts, cur_seq, win_snap, mode = now, self.seq_id, self._origin_win, "stop"
                else:
                    return {"ok": True, "state": self.state, "seq_id": self.seq_id}

            if mode == "cancel":
                if not eff_silent: self._hud("hide")
                self.platform.cancel_recording()
                if not eff_silent: self.platform.beep("error")
                return {"ok": True, "state": "idle", "seq_id": cur_seq}
            if mode == "start":
                if not eff_silent: self._hud("recording")
                try:
                    if (resolver := getattr(self.platform, "resolve_foreground_window", None)) is not None:
                        win_now = resolver(window)
                    else:
                        win_now = window if (window and (window.title or window.process or window.hwnd)) else self.platform.get_foreground_window()
                    with self._lock: self._origin_win = win_now
                    if loopback:
                        self.platform.start_recording(self.runtime_dir, loopback=True)
                    else:
                        self.platform.start_recording(self.runtime_dir)
                    if not eff_silent: self.platform.beep("start")
                    return {"ok": True, "state": "recording", "seq_id": cur_seq}
                except Exception as exc:
                    err = self._redact(exc)
                    with self._lock:
                        still_cur = self.seq_id == cur_seq
                        if still_cur:
                            self.state = "idle"
                            self._cur_no_paste, self._cur_silent_hud = False, False
                        self.last_result = {"seq_id": cur_seq, "ok": False, "error": err}
                    if still_cur and not eff_silent: self._hud("error")
                    if not eff_silent: self.platform.beep("error")
                    return {"ok": False, "error": err, "state": "idle", "seq_id": cur_seq}

            if not eff_silent: self._hud("recognizing")
            try:
                raw_wav, src_used = self.platform.stop_recording()
                if not eff_silent: self.platform.beep("stop")
            except Exception as exc:
                err = self._redact(exc)
                with self._lock:
                    still_cur = self.seq_id == cur_seq
                    if still_cur: self.state = "idle"
                    self.last_result = {"seq_id": cur_seq, "ok": False, "error": err}
                if still_cur and not eff_silent: self._hud("error")
                if not eff_silent: self.platform.beep("error")
                return {"ok": False, "error": err, "state": "idle", "seq_id": cur_seq}
            threading.Thread(target=self._run_bg_pipeline, args=(cur_seq, raw_wav, src_used, win_snap, eff_no_paste, eff_silent), daemon=True).start()
            return {"ok": True, "state": "recognizing", "seq_id": cur_seq}

    def _run_bg_pipeline(self, seq_id: int, raw_wav: bytes, src_used: str, origin_win: ForegroundWindow, no_paste: bool = False, silent_hud: bool = False) -> None:
        hud_st, beep_k = "", ""
        try:
            res = run_pipeline_once(
                raw_wav, cfg=self.cfg, creds=self.creds, tracker=self.tracker,
                asr_provider=self.asr_provider, refiner=self.refiner, platform=self.platform,
                origin_window=origin_win, audio_source=src_used, seq_id=seq_id, dry_run=no_paste,
                is_current_seq=self.is_current_seq,
                on_stage=lambda st: self._on_pipeline_stage(seq_id, st, silent_hud),
                env=self.env,
            )
            if res.ok:
                hud_st = "copied" if (res.paste and not res.paste.pasted and res.paste.clipboard) else "done"
            elif res.skipped_reason in ("too_short", "silence", "empty_audio", "empty_asr"):
                hud_st, beep_k = "empty", "error"
            elif res.skipped_reason != "superseded":
                hud_st, beep_k = "error", "error"
            summary = self._build_summary(seq_id, res, dry_run=no_paste)
        except Exception as exc:
            hud_st, beep_k = "error", "error"
            summary = {"seq_id": seq_id, "ok": False, "error": self._redact(exc)}
        if not self.is_current_seq(seq_id):
            with self._lock:
                if seq_id >= int(self.last_result.get("seq_id") or 0):
                    self.last_result = summary
            return
        with self._trigger_lock:
            emit = False
            with self._lock:
                if seq_id >= int(self.last_result.get("seq_id") or 0):
                    self.last_result = summary
                if not self._stop_evt.is_set() and self.seq_id == seq_id:
                    if self.state in ("recognizing", "refining", "processing"):
                        self.state = "idle"
                    emit = not silent_hud
            if emit:
                if hud_st:
                    self._hud(hud_st)
                if beep_k:
                    self.platform.beep(beep_k)

    def handle_control(self, req: Mapping[str, Any]) -> dict[str, Any]:
        cmd = str(req.get("cmd") or "status")
        if cmd == "status":
            with self._lock:
                return {"ok": True, "pid": os.getpid(), "state": self.state, "seq_id": self.seq_id, "asr_provider": select_asr_provider(self.cfg, self.creds), "refiner_provider": self.cfg.refiner.provider, "last_result": dict(self.last_result)}
        if cmd in ("toggle", "rec_start", "rec_stop"):
            win = _window_from_mapping(req) if any(req.get(k) for k in ("hwnd", "process", "title", "bundle_id", "surface_id", "tty", "cwd", "pid")) else None
            return self.handle_trigger({"toggle": "toggle", "rec_start": "ptt_start", "rec_stop": "ptt_stop"}[cmd], win, no_paste=bool(req.get("no_paste")), silent_hud=bool(req.get("silent_hud")), loopback=bool(req.get("loopback")))
        if cmd == "selftest_wav":
            wav_path = Path(str(req.get("wav_path") or "")).expanduser()
            if not wav_path.is_file():
                return {"ok": False, "error": f"wav_path not found: {wav_path}"}
            with self._lock:
                self.seq_id += 1
                cur_seq = self.seq_id
            win = _window_from_mapping(req)
            res = run_pipeline_once(wav_path, cfg=self.cfg, creds=self.creds, tracker=self.tracker, asr_provider=self.asr_provider, refiner=self.refiner, platform=self.platform, origin_window=win, audio_source="selftest_wav", seq_id=cur_seq, dry_run=True, env=self.env)
            summary = self._build_summary(cur_seq, res, dry_run=True)
            with self._lock:
                self.last_result = summary
            return {"ok": res.ok, "seq_id": cur_seq, "skipped_reason": res.skipped_reason, "asr_text": res.asr.text if res.asr else "", "final_text": res.final_text, "summary": summary}
        if cmd == "shutdown":
            self._stop_evt.set(); return {"ok": True, "stopping": True}
        return {"ok": False, "error": f"unknown cmd: {cmd}"}

    def serve_forever(self) -> None:
        self.sock_path.unlink(missing_ok=True)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv_sock = srv
        is_main = threading.current_thread() is threading.main_thread()
        prev_int = signal.signal(signal.SIGINT, lambda *_: self._stop_evt.set()) if is_main else None
        prev_term = signal.signal(signal.SIGTERM, lambda *_: self._stop_evt.set()) if is_main else None
        try:
            self.pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8"); os.chmod(self.pid_path, 0o600)
            self.platform.start_event_listener(lambda m: self.handle_trigger(str(m.get("event")), _window_from_mapping(m), from_hotkey=True) if m.get("event") in ("toggle", "ptt_start", "ptt_stop", "ptt_cancel") else None)
            srv.bind(str(self.sock_path)); os.chmod(self.sock_path, 0o600); srv.listen(8); srv.settimeout(0.3)
            while not self._stop_evt.is_set():
                try: conn, _ = srv.accept()
                except socket.timeout: continue
                except OSError: break
                with conn:
                    try:
                        conn.settimeout(2.0)
                        raw = conn.recv(16384).decode("utf-8", errors="replace").strip()
                        resp = self.handle_control(req if isinstance(req := (json.loads(raw) if raw else {}), dict) else {})
                    except Exception as exc:
                        resp = {"ok": False, "error": self._redact(exc)}
                    try: conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                    except OSError: pass
        finally:
            if is_main and prev_int is not None: signal.signal(signal.SIGINT, prev_int)
            if is_main and prev_term is not None: signal.signal(signal.SIGTERM, prev_term)
            self.close()

    def close(self) -> None:
        self._stop_evt.set()
        if self._srv_sock is not None:
            try: self._srv_sock.close()
            except OSError: pass
            self._srv_sock = None
        self.sock_path.unlink(missing_ok=True); self.pid_path.unlink(missing_ok=True)
        self.platform.close(); self.refiner.close()
