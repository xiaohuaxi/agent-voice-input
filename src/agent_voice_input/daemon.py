"""Core anti-reentrant two-stage state machine daemon and single-shot pipeline for agent-voice-input."""
from __future__ import annotations

from dataclasses import dataclass
import concurrent.futures, inspect, json, os, signal, socket, sys, threading, time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import __version__
from .asr_providers import AsrError, AsrResult, BaseAsrProvider, create_asr_provider
from .audio_proc import AudioGateResult, DEFAULT_MAX_CHUNK_SEC, inspect_wav_chunks, process_wav, save_failed_audio
from .config import Config, ProviderCredentials, ensure_secure_dir, get_runtime_dir, redact_secrets, resolve_credentials, select_asr_provider
from .hud import is_toast_hud_state
from .platform_macos import MacOSPlatform, is_macos_terminal_window
from .platform_wsl import (
    ForegroundWindow,
    MicrophoneUnavailableError,
    NoMicrophoneError,
    PasteOutcome,
    WslPlatform,
    is_windows_terminal_process,
)
from .refiner import BaseRefiner, RefinerResult, create_refiner
from .session_ctx import SessionSnapshot, SessionTracker


@dataclass(frozen=True)
class PipelineResult:
    ok: bool; seq_id: int; final_text: str = ""; skipped_reason: str = ""; audio_source: str = ""
    switched_fallback: bool = False; gate: AudioGateResult | None = None; session: SessionSnapshot | None = None
    asr: AsrResult | None = None; refined: RefinerResult | None = None; paste: PasteOutcome | None = None; error: str = ""
    asr_chunks: int = 1; asr_chunk_secs: tuple[float, ...] = (); saved_audio: str = ""


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


def _join_chunk_texts(parts: Sequence[str]) -> str:
    out: list[str] = []
    for raw in parts:
        s = raw.strip()
        if not s:
            continue
        if not out:
            out.append(s)
            continue
        prev = out[-1]
        if prev[-1].isascii() and prev[-1].isalnum() and s[0].isascii() and s[0].isalnum():
            out.append(" " + s)
        elif prev[-1] in "，。！？；：,.!?;:\n":
            out.append(s)
        else:
            out.append("，" + s if not prev[-1].isascii() else " " + s)
    return "".join(out).strip()


def _derive_failed_label(snap: SessionSnapshot | None, win: ForegroundWindow) -> str:
    for cwd_cand in ((snap.match.cwd if snap else ""), win.cwd):
        if cwd_cand and cwd_cand.strip():
            name = Path(cwd_cand.strip().rstrip("/\\")).name
            if name:
                return name
    if snap and snap.match.cli and snap.match.session_id:
        return f"{snap.match.cli}-{snap.match.session_id[:8]}"
    return ""


MAX_ASR_CHUNK_CONCURRENCY: int = 5


def _call_asr_transcribe(
    eff_asr: BaseAsrProvider,
    wav_bytes: bytes,
    *,
    context: Any,
    is_cancelled: Callable[[], bool] | None,
) -> AsrResult:
    if is_cancelled is not None:
        try:
            params = inspect.signature(eff_asr.transcribe).parameters
        except (TypeError, ValueError):
            params = {}
        if "is_cancelled" in params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return eff_asr.transcribe(wav_bytes, context=context, is_cancelled=is_cancelled)
    return eff_asr.transcribe(wav_bytes, context=context)


def _call_refiner_refine(
    eff_refiner: BaseRefiner,
    asr_res: AsrResult,
    *,
    session_key: str,
    tracker: SessionTracker,
    fallback_context: str,
    is_cancelled: Callable[[], bool] | None,
) -> RefinerResult:
    if is_cancelled is not None:
        try:
            params = inspect.signature(eff_refiner.refine).parameters
        except (TypeError, ValueError):
            params = {}
        if "is_cancelled" in params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return eff_refiner.refine(
                asr_res,
                session_key=session_key,
                tracker=tracker,
                fallback_context=fallback_context,
                is_cancelled=is_cancelled,
            )
    return eff_refiner.refine(
        asr_res,
        session_key=session_key,
        tracker=tracker,
        fallback_context=fallback_context,
    )


def _transcribe_wav_chunks(
    wav_bytes: bytes,
    *,
    eff_asr: BaseAsrProvider,
    asr_ctx: Any,
    floor_dbfs: float,
    max_chunk_sec: float = DEFAULT_MAX_CHUNK_SEC,
    seq_id: int = 1,
    is_current_seq: Callable[[int], bool] | None = None,
) -> tuple[AsrResult, int, tuple[float, ...]]:
    cancel_check: Callable[[], bool] | None = (
        (lambda: not is_current_seq(seq_id)) if is_current_seq is not None else None
    )
    infos = inspect_wav_chunks(wav_bytes, max_chunk_sec=max_chunk_sec, floor_dbfs=floor_dbfs)
    if len(infos) <= 1:
        if cancel_check is not None and cancel_check():
            return AsrResult(text="", provider=getattr(eff_asr, "provider_name", ""), model="", latency_ms=0), 1, ()
        return _call_asr_transcribe(eff_asr, wav_bytes, context=asr_ctx, is_cancelled=cancel_check), 1, ()

    chunk_secs = tuple(round(c.duration_sec, 2) for c in infos)
    t0 = time.monotonic()
    results: list[AsrResult | None] = [None] * len(infos)
    abort_evt = threading.Event()
    first_exc_lock = threading.Lock()
    first_exc: list[Exception] = []

    def _chunk_cancelled() -> bool:
        return abort_evt.is_set() or (cancel_check is not None and cancel_check())

    def _work(chunk_wav: bytes) -> AsrResult | None:
        if cancel_check is not None and cancel_check():
            return None
        if abort_evt.is_set():
            return None
        try:
            return _call_asr_transcribe(eff_asr, chunk_wav, context=asr_ctx, is_cancelled=_chunk_cancelled)
        except Exception as exc:
            if cancel_check is not None and cancel_check() and isinstance(exc, AsrError) and "cancelled" in str(exc).lower():
                return None
            with first_exc_lock:
                if not abort_evt.is_set():
                    first_exc.append(exc)
                    abort_evt.set()
            raise

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX_ASR_CHUNK_CONCURRENCY, len(infos))) as pool:
        futs = [pool.submit(_work, c.wav_bytes) for c in infos]
        try:
            for idx, fut in enumerate(futs):
                results[idx] = fut.result()
        except Exception:
            abort_evt.set()
            for f in futs:
                f.cancel()
            if first_exc:
                raise first_exc[0]
            raise

    non_null = [r for r in results if r is not None]
    merged_text = _join_chunk_texts([r.text for r in non_null if r.text.strip()])
    elapsed_ms = max(0, int(round((time.monotonic() - t0) * 1000)))
    first = non_null[0] if non_null else None
    prov = first.provider if first else getattr(eff_asr, "provider_name", "")
    model = first.model if first else ""
    single_hop = bool(non_null) and all(r.is_single_hop_refined for r in non_null)
    total_retries = sum(r.retries for r in non_null)
    return (
        AsrResult(
            text=merged_text,
            provider=prov,
            model=model,
            latency_ms=elapsed_ms,
            is_single_hop_refined=single_hop,
            retries=total_retries,
        ),
        len(infos),
        chunk_secs,
    )


def run_pipeline_once(
    raw_wav: bytes | str | Path, *, cfg: Config, creds: ProviderCredentials | None = None,
    tracker: SessionTracker | None = None, asr_provider: BaseAsrProvider | None = None,
    refiner: BaseRefiner | None = None, platform: Any = None, origin_window: ForegroundWindow | None = None,
    window_title_override: str | None = None,
    audio_source: str = "file", seq_id: int = 1, dry_run: bool = False,
    is_current_seq: Callable[[int], bool] | None = None,
    on_stage: Callable[[str], None] | None = None,
    runtime_dir: Path | None = None,
    max_chunk_sec: float = DEFAULT_MAX_CHUNK_SEC,
    env: Mapping[str, str] | None = None,
) -> PipelineResult:
    eff_env = os.environ if env is None else env
    eff_creds = creds or resolve_credentials(cfg, eff_env)
    eff_tracker = tracker or SessionTracker(env=eff_env)
    eff_asr = asr_provider or create_asr_provider(cfg, eff_creds, env=eff_env)
    owns_refiner = refiner is None
    eff_refiner = refiner or create_refiner(cfg, eff_creds, env=eff_env)
    can_save_failed = runtime_dir is not None and audio_source not in ("file", "selftest_wav", "retry")

    def _stash_failed(gate_res: AudioGateResult, snap_res: SessionSnapshot | None, win_res: ForegroundWindow) -> str:
        if not can_save_failed or runtime_dir is None or not gate_res.wav_bytes:
            return ""
        saved = save_failed_audio(
            gate_res.wav_bytes,
            runtime_dir,
            duration_sec=gate_res.duration_sec,
            label=_derive_failed_label(snap_res, win_res),
        )
        return str(saved) if saved is not None else ""

    try:
        if isinstance(raw_wav, (bytes, bytearray)) and len(raw_wav) == 0:
            sw = bool(platform and platform.switch_to_fallback_audio()) if audio_source in ("pulse", "winmm") else False
            return PipelineResult(False, seq_id, skipped_reason="empty_audio", audio_source=audio_source, switched_fallback=sw)
        gate = process_wav(raw_wav, cfg.audio)
        if not gate.passed:
            sw = bool(gate.all_zero and gate.reason == "silence" and audio_source == "pulse" and platform and platform.switch_to_fallback_audio())
            return PipelineResult(False, seq_id, skipped_reason=gate.reason, audio_source=audio_source, switched_fallback=sw, gate=gate)
        win = origin_window or (platform.get_foreground_window() if platform else ForegroundWindow())
        snap: SessionSnapshot | None = None
        try:
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
            cancel_check: Callable[[], bool] | None = (
                (lambda: not is_current_seq(seq_id)) if is_current_seq is not None else None
            )
            try:
                asr_res, chunk_count, chunk_secs = _transcribe_wav_chunks(
                    gate.wav_bytes,
                    eff_asr=eff_asr,
                    asr_ctx=asr_ctx,
                    floor_dbfs=cfg.audio.floor_dbfs,
                    max_chunk_sec=max_chunk_sec,
                    seq_id=seq_id,
                    is_current_seq=is_current_seq,
                )
            except AsrError as asr_exc:
                if cancel_check is not None and cancel_check() and "cancelled" in str(asr_exc).lower():
                    saved_p = _stash_failed(gate, snap, win) if gate.duration_sec >= 15.0 else ""
                    return PipelineResult(
                        False,
                        seq_id,
                        skipped_reason="superseded",
                        audio_source=audio_source,
                        gate=gate,
                        session=snap,
                        saved_audio=saved_p,
                    )
                raise
            if not asr_res.text.strip():
                if is_current_seq is not None and not is_current_seq(seq_id):
                    saved_p = _stash_failed(gate, snap, win) if gate.duration_sec >= 15.0 else ""
                    return PipelineResult(False, seq_id, skipped_reason="superseded", audio_source=audio_source, gate=gate, session=snap, asr=asr_res, asr_chunks=chunk_count, asr_chunk_secs=chunk_secs, saved_audio=saved_p)
                saved_p = _stash_failed(gate, snap, win)
                return PipelineResult(False, seq_id, skipped_reason="empty_asr", audio_source=audio_source, gate=gate, session=snap, asr=asr_res, asr_chunks=chunk_count, asr_chunk_secs=chunk_secs, saved_audio=saved_p)
            if is_current_seq is not None and not is_current_seq(seq_id):
                saved_p = _stash_failed(gate, snap, win) if gate.duration_sec >= 15.0 else ""
                return PipelineResult(False, seq_id, final_text=asr_res.text.strip(), skipped_reason="superseded", audio_source=audio_source, gate=gate, session=snap, asr=asr_res, asr_chunks=chunk_count, asr_chunk_secs=chunk_secs, saved_audio=saved_p)
            if _will_run_stage2_refiner(cfg, eff_refiner, asr_res) and on_stage is not None:
                on_stage("refining")
            ref_res = _call_refiner_refine(
                eff_refiner,
                asr_res,
                session_key=snap.session_key if snap else "",
                tracker=eff_tracker,
                fallback_context=asr_ctx.dialog_text,
                is_cancelled=cancel_check,
            )
            final_text = ref_res.text.strip() or asr_res.text.strip()
            if is_current_seq is not None and not is_current_seq(seq_id):
                saved_p = _stash_failed(gate, snap, win) if gate.duration_sec >= 15.0 else ""
                return PipelineResult(False, seq_id, final_text=final_text, skipped_reason="superseded", audio_source=audio_source, gate=gate, session=snap, asr=asr_res, refined=ref_res, asr_chunks=chunk_count, asr_chunk_secs=chunk_secs, saved_audio=saved_p)
            paste_out = None if (dry_run or platform is None) else _deliver_with_origin(platform, final_text, seq_id=seq_id, origin_window=win)
            saved_p = _stash_failed(gate, snap, win) if (paste_out is not None and not paste_out.pasted and not paste_out.clipboard) else ""
            return PipelineResult(True, seq_id, final_text=final_text, audio_source=audio_source, gate=gate, session=snap, asr=asr_res, refined=ref_res, paste=paste_out, asr_chunks=chunk_count, asr_chunk_secs=chunk_secs, saved_audio=saved_p)
        except Exception as exc:
            if saved_p := _stash_failed(gate, snap, win):
                try:
                    setattr(exc, "_saved_audio_path", saved_p)
                except Exception:
                    pass
            raise
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
        self._rec_recovery_count, self._rec_recovery_gap_ms = 0, 0
        self._rec_hud_ready_seq, self._pending_recovery_toast_seq = -1, -1
        self._recorder_lost_seq, self._recorder_lost_ts, self._recorder_lost_stop_absorbed = -1, -1e9, False
        self._auto_stop_seq, self._auto_stop_ts, self._auto_stop_absorbed = -1, -1e9, False
        self._max_dur_timer: threading.Timer | None = None
        if (setter := getattr(self.platform, "set_recorder_lost_callback", None)) is not None:
            setter(self._on_recorder_lost)
        if (rec_setter := getattr(self.platform, "set_recorder_recovered_callback", None)) is not None:
            rec_setter(self._on_recorder_recovered)

    @property
    def pid_path(self) -> Path: return self.runtime_dir / "daemon.pid"
    @property
    def sock_path(self) -> Path: return self.runtime_dir / "control.sock"

    def is_current_seq(self, seq_id: int) -> bool:
        with self._lock:
            return not self._stop_evt.is_set() and self.seq_id == seq_id

    def _cancel_max_duration_timer(self) -> None:
        with self._lock:
            t, self._max_dur_timer = self._max_dur_timer, None
        if t is not None:
            t.cancel()

    def _arm_max_duration_timer(self, seq_id: int) -> None:
        max_sec = float(self.cfg.audio.max_duration_sec)
        if max_sec <= 0.0:
            return
        t = threading.Timer(max_sec, self._on_max_duration_timeout, args=(seq_id,))
        t.daemon = True
        start_it = False
        with self._lock:
            old, self._max_dur_timer = self._max_dur_timer, None
            if not self._stop_evt.is_set() and self.seq_id == seq_id and self.state == "recording":
                self._max_dur_timer = t
                start_it = True
        if old is not None:
            old.cancel()
        if start_it:
            t.start()

    def _on_max_duration_timeout(self, seq_id: int) -> None:
        if self._stop_evt.is_set() or not self.is_current_seq(seq_id):
            return
        with self._lock:
            if self._stop_evt.is_set() or self.seq_id != seq_id or self.state != "recording":
                return
        self.handle_trigger("toggle", expected_seq_id=seq_id)

    def _redact_str(self, text: str) -> str:
        return redact_secrets(text, self.env, cfg=self.cfg, extra_secrets=self.creds.all_secret_values())

    def _redact(self, exc: Exception) -> str:
        return self._redact_str(str(exc))

    def _hud(
        self,
        state: str,
        *,
        duration_ms: int = 0,
        clear_main: bool | None = None,
        clear_toast: bool | None = None,
    ) -> None:
        if (fn := getattr(self.platform, "hud", None)) is None:
            return
        kw: dict[str, Any] = {}
        if duration_ms > 0:
            kw["duration_ms"] = duration_ms
        if clear_main is not None:
            kw["clear_main"] = clear_main
        if clear_toast is not None:
            kw["clear_toast"] = clear_toast
        try:
            fn(state, **kw)
            return
        except TypeError:
            pass
        except Exception:
            return
        try:
            if duration_ms > 0:
                fn(state, duration_ms=duration_ms)
            else:
                fn(state)
        except TypeError:
            try:
                fn(state)
            except Exception:
                pass
        except Exception:
            pass

    def _on_recorder_recovered(self, gap_ms: int = 0, *, seq_id: int | None = None) -> None:
        norm_gap = max(0, int(gap_ms or 0))
        emit_toast = False
        with self._lock:
            if self._stop_evt.is_set() or self.state != "recording":
                return
            if seq_id is not None and self.seq_id != seq_id:
                return
            self._rec_recovery_count += 1
            self._rec_recovery_gap_ms += norm_gap
            if not self._cur_silent_hud and norm_gap >= 250:
                if self._rec_hud_ready_seq == self.seq_id:
                    emit_toast = True
                else:
                    self._pending_recovery_toast_seq = self.seq_id
        if emit_toast:
            self._hud("audio_recovered", clear_main=False)

    def _on_recorder_lost(self, *, seq_id: int | None = None) -> None:
        with self._trigger_lock:
            with self._lock:
                if self._stop_evt.is_set() or self.state != "recording":
                    return
                if seq_id is not None and self.seq_id != seq_id:
                    return
                cur_seq = self.seq_id
                eff_no_paste = self._cur_no_paste
                eff_silent = self._cur_silent_hud
                rec_cnt, rec_gap = self._rec_recovery_count, self._rec_recovery_gap_ms
                self._cur_no_paste, self._cur_silent_hud = False, False
                self._rec_recovery_count, self._rec_recovery_gap_ms = 0, 0
                self._rec_hud_ready_seq, self._pending_recovery_toast_seq = -1, -1
                win_snap = self._origin_win
                self.state = "recognizing"
            self._cancel_max_duration_timer()
            try:
                try:
                    raw_wav, src_used = self.platform.stop_recording(drain=False)
                except TypeError:
                    raw_wav, src_used = self.platform.stop_recording()
            except Exception:
                raw_wav, src_used = b"", ""
            gate = None
            if len(raw_wav) > 44:
                try:
                    gate = process_wav(raw_wav, self.cfg.audio)
                except Exception:
                    gate = None
            lost_ts = self.clock_fn()
            if gate is None or not gate.passed:
                audio_err = self._redact_str(str(getattr(self.platform, "last_audio_error", "") or "mic_disconnected"))
                with self._lock:
                    still_cur = self.seq_id == cur_seq
                    if still_cur:
                        self.state = "idle"
                        self._recorder_lost_seq = cur_seq
                        self._recorder_lost_ts = lost_ts
                        self._recorder_lost_stop_absorbed = False
                    lr_lost: dict[str, Any] = {
                        "seq_id": cur_seq,
                        "ok": False,
                        "skipped_reason": "mic_disconnected",
                        "audio_source": src_used,
                        "error": audio_err,
                        "capture_interrupted": True,
                    }
                    if rec_cnt > 0:
                        lr_lost["recovery_count"] = rec_cnt
                        lr_lost["recovery_gap_ms"] = rec_gap
                    self.last_result = lr_lost
                if still_cur and not eff_silent:
                    self._hud("mic_disconnected", duration_ms=2200, clear_main=True)
                if not eff_silent:
                    self.platform.beep("error")
                return
            with self._lock:
                if self.seq_id == cur_seq:
                    self._recorder_lost_seq = cur_seq
                    self._recorder_lost_ts = lost_ts
                    self._recorder_lost_stop_absorbed = False
            if not eff_silent:
                self._hud("recognizing")
                self._hud("mic_disconnected", duration_ms=3200, clear_main=False)
                self.platform.beep("error")
            threading.Thread(
                target=self._run_bg_pipeline,
                args=(cur_seq, raw_wav, src_used, win_snap, eff_no_paste, eff_silent, True, rec_cnt, rec_gap),
                daemon=True,
            ).start()

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

    def _build_summary(
        self,
        seq_id: int,
        res: PipelineResult,
        *,
        dry_run: bool = False,
        recorder_lost: bool = False,
        recovery_count: int = 0,
        recovery_gap_ms: int = 0,
    ) -> dict[str, Any]:
        summary: dict[str, Any] = {"seq_id": seq_id, "ok": res.ok, "skipped_reason": res.skipped_reason, "audio_source": res.audio_source, "switched_fallback": res.switched_fallback, "session_key": res.session.session_key if res.session else "", "asr_provider": res.asr.provider if res.asr else "", "refiner_fallback": res.refined.used_fallback if res.refined else "", "pasted": res.paste.pasted if res.paste else False, "paste_reason": res.paste.reason if res.paste else ("dry_run" if dry_run and res.ok else ""), "raw_audio_sec": round(res.gate.duration_sec, 3) if res.gate else 0.0, "voiced_audio_sec": round(res.gate.voiced_sec, 3) if res.gate else 0.0, "asr_ms": res.asr.latency_ms if res.asr else 0, "refine_ms": res.refined.latency_ms if res.refined else 0, "asr_chars": len(res.asr.text.strip()) if res.asr else 0, "final_chars": len(res.final_text)}
        if res.asr and res.asr.retries > 0:
            summary["asr_retries"] = res.asr.retries
        if res.refined and res.refined.retries > 0:
            summary["refiner_retries"] = res.refined.retries
        if res.refined and res.refined.warning:
            summary["refiner_warning"] = self._redact_str(res.refined.warning)
        if res.asr_chunks > 1:
            summary["asr_chunks"] = res.asr_chunks
            if res.asr_chunk_secs:
                summary["asr_chunk_secs"] = list(res.asr_chunk_secs)
        if res.saved_audio:
            summary["saved_audio"] = res.saved_audio
        if dry_run:
            summary["dry_run"] = True
        if recorder_lost:
            summary["capture_interrupted"] = True
        if recovery_count > 0:
            summary["recovery_count"] = recovery_count
            summary["recovery_gap_ms"] = recovery_gap_ms
        if (not res.ok or recorder_lost) and (audio_err := str(getattr(self.platform, "last_audio_error", "") or "")):
            summary["audio_error"] = self._redact_str(audio_err)
        return summary

    def handle_trigger(self, action: str, window: ForegroundWindow | None = None, *, no_paste: bool = False, silent_hud: bool = False, loopback: bool = False, from_hotkey: bool = False, expected_seq_id: int | None = None) -> dict[str, Any]:
        arrival_ts = self.clock_fn()
        with self._trigger_lock:
            with self._lock:
                if self._stop_evt.is_set():
                    return {"ok": False, "ignored": "stopping", "state": self.state, "seq_id": self.seq_id}
                if expected_seq_id is not None and (self.seq_id != expected_seq_id or self.state != "recording"):
                    return {"ok": False, "ignored": "stale_seq", "state": self.state, "seq_id": self.seq_id}
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
                if expected_seq_id is None and action in ("toggle", "ptt_start") and (arrival_ts - self._last_trigger_ts) * 1000.0 < self.cfg.hotkey.debounce_ms:
                    return {"ok": False, "ignored": "debounced", "state": self.state, "seq_id": self.seq_id}
                if (
                    action == "toggle"
                    and self.state != "recording"
                    and self.seq_id == self._recorder_lost_seq
                    and not self._recorder_lost_stop_absorbed
                    and (
                        self.state in ("recognizing", "refining", "processing")
                        or 0.0 <= (arrival_ts - self._recorder_lost_ts) < 1.5
                    )
                ):
                    self._recorder_lost_stop_absorbed = True
                    return {"ok": True, "ignored": "already_stopped", "state": self.state, "seq_id": self.seq_id}
                if (
                    expected_seq_id is None
                    and action == "toggle"
                    and self.state != "recording"
                    and self.seq_id == self._auto_stop_seq
                    and not self._auto_stop_absorbed
                    and 0.0 <= (arrival_ts - self._auto_stop_ts) < 1.5
                ):
                    self._auto_stop_absorbed = True
                    return {"ok": True, "ignored": "already_stopped", "state": self.state, "seq_id": self.seq_id}
                now = self.clock_fn()
                if action == "ptt_cancel":
                    if self.state != "recording":
                        return {"ok": True, "state": self.state, "seq_id": self.seq_id}
                    eff_silent = bool(silent_hud or self._cur_silent_hud)
                    self._cur_no_paste, self._cur_silent_hud = False, False
                    self._rec_recovery_count, self._rec_recovery_gap_ms = 0, 0
                    self._rec_hud_ready_seq, self._pending_recovery_toast_seq = -1, -1
                    self.seq_id += 1; self.state, self._last_trigger_ts, cur_seq, mode = "idle", now, self.seq_id, "cancel"
                elif action in ("toggle", "ptt_start") and self.state != "recording":
                    self._cur_no_paste, self._cur_silent_hud = bool(no_paste), bool(silent_hud)
                    self._rec_recovery_count, self._rec_recovery_gap_ms = 0, 0
                    self._rec_hud_ready_seq, self._pending_recovery_toast_seq = -1, -1
                    self._recorder_lost_seq, self._recorder_lost_ts, self._recorder_lost_stop_absorbed = -1, -1e9, False
                    self._auto_stop_seq, self._auto_stop_ts, self._auto_stop_absorbed = -1, -1e9, False
                    self._origin_win = window if window is not None else ForegroundWindow()
                    self.seq_id += 1; self.state, self._last_trigger_ts, cur_seq, mode = "recording", now, self.seq_id, "start"
                elif action in ("toggle", "ptt_stop") and self.state == "recording":
                    # Stage 1 ('recognizing') starts immediately on the very first line of stopping recording
                    self.state = "recognizing"
                    eff_no_paste = bool(no_paste or self._cur_no_paste)
                    eff_silent = bool(silent_hud or self._cur_silent_hud)
                    rec_cnt, rec_gap = self._rec_recovery_count, self._rec_recovery_gap_ms
                    self._cur_no_paste, self._cur_silent_hud = False, False
                    self._rec_recovery_count, self._rec_recovery_gap_ms = 0, 0
                    self._rec_hud_ready_seq, self._pending_recovery_toast_seq = -1, -1
                    if expected_seq_id is None:
                        self._last_trigger_ts = now
                    cur_seq, win_snap, mode = self.seq_id, self._origin_win, "stop"
                else:
                    return {"ok": True, "state": self.state, "seq_id": self.seq_id}

            if mode == "cancel":
                self._cancel_max_duration_timer()
                if not eff_silent: self._hud("hide", clear_toast=True)
                self.platform.cancel_recording()
                if not eff_silent: self.platform.beep("error")
                return {"ok": True, "state": "idle", "seq_id": cur_seq}
            if mode == "start":
                self._cancel_max_duration_timer()
                rec_started = False
                try:
                    eff_loopback = bool(
                        loopback
                        or self.env.get("AGENT_VOICE_INPUT_WASAPI_LOOPBACK") == "1"
                        or self.env.get("VOICE_INPUT_WASAPI_LOOPBACK") == "1"
                    )
                    start_fn = self.platform.start_recording
                    try:
                        start_params = inspect.signature(start_fn).parameters
                        pass_seq = "seq_id" in start_params or any(
                            p.kind == inspect.Parameter.VAR_KEYWORD for p in start_params.values()
                        )
                    except (TypeError, ValueError):
                        pass_seq = False
                    start_kw: dict[str, Any] = {"seq_id": cur_seq} if pass_seq else {}
                    if loopback:
                        start_fn(self.runtime_dir, loopback=True, **start_kw)
                    else:
                        start_fn(self.runtime_dir, **start_kw)
                    rec_started = True
                    if not eff_silent:
                        self._hud("recording", clear_toast=True)
                    flush_rec_toast = False
                    with self._lock:
                        if self.seq_id == cur_seq and self.state == "recording":
                            self._rec_hud_ready_seq = cur_seq
                            flush_rec_toast = (
                                self._pending_recovery_toast_seq == cur_seq and not eff_silent
                            )
                            self._pending_recovery_toast_seq = -1
                    if flush_rec_toast:
                        self._hud("audio_recovered", clear_main=False)
                    if not eff_silent and not eff_loopback:
                        self.platform.beep("start")
                    if (resolver := getattr(self.platform, "resolve_foreground_window", None)) is not None:
                        win_now = resolver(window)
                    else:
                        win_now = window if (window and (window.title or window.process or window.hwnd)) else self.platform.get_foreground_window()
                    with self._lock: self._origin_win = win_now
                    self._arm_max_duration_timer(cur_seq)
                    return {"ok": True, "state": "recording", "seq_id": cur_seq}
                except Exception as exc:
                    self._cancel_max_duration_timer()
                    if rec_started:
                        try:
                            self.platform.cancel_recording()
                        except Exception:
                            pass
                    err = self._redact(exc)
                    is_mic_err = isinstance(exc, (NoMicrophoneError, MicrophoneUnavailableError))
                    err_reason = str(getattr(exc, "reason_code", "no_mic")) if is_mic_err else ""
                    err_hud_state = str(getattr(exc, "hud_state", "no_mic")) if is_mic_err else "error"
                    err_hud_dur = 2200 if err_hud_state == "no_mic" else 0
                    with self._lock:
                        still_cur = self.seq_id == cur_seq
                        if still_cur:
                            self.state = "idle"
                            self._cur_no_paste, self._cur_silent_hud = False, False
                            self._rec_recovery_count, self._rec_recovery_gap_ms = 0, 0
                            self._rec_hud_ready_seq, self._pending_recovery_toast_seq = -1, -1
                        lr: dict[str, Any] = {"seq_id": cur_seq, "ok": False, "error": err}
                        if err_reason:
                            lr["skipped_reason"] = err_reason
                        self.last_result = lr
                    if still_cur and not eff_silent:
                        self._hud(err_hud_state, duration_ms=err_hud_dur, clear_main=True)
                    if not eff_silent: self.platform.beep("error")
                    out_err: dict[str, Any] = {"ok": False, "error": err, "state": "idle", "seq_id": cur_seq}
                    if err_reason:
                        out_err["skipped_reason"] = err_reason
                    return out_err

            self._cancel_max_duration_timer()
            if not eff_silent: self._hud("recognizing")
            try:
                raw_wav, src_used = self.platform.stop_recording()
                if expected_seq_id is not None:
                    done_ts = self.clock_fn()
                    with self._lock:
                        if self.seq_id == cur_seq:
                            self._auto_stop_seq = cur_seq
                            self._auto_stop_ts = done_ts
                            self._auto_stop_absorbed = False
                if not eff_silent: self.platform.beep("stop")
            except Exception as exc:
                err = self._redact(exc)
                with self._lock:
                    still_cur = self.seq_id == cur_seq
                    if still_cur: self.state = "idle"
                    self.last_result = {"seq_id": cur_seq, "ok": False, "error": err}
                if still_cur and not eff_silent: self._hud("error", clear_main=True)
                if not eff_silent: self.platform.beep("error")
                return {"ok": False, "error": err, "state": "idle", "seq_id": cur_seq}
            threading.Thread(
                target=self._run_bg_pipeline,
                args=(cur_seq, raw_wav, src_used, win_snap, eff_no_paste, eff_silent, False, rec_cnt, rec_gap),
                daemon=True,
            ).start()
            return {"ok": True, "state": "recognizing", "seq_id": cur_seq}

    def _run_bg_pipeline(
        self,
        seq_id: int,
        raw_wav: bytes,
        src_used: str,
        origin_win: ForegroundWindow,
        no_paste: bool = False,
        silent_hud: bool = False,
        recorder_lost: bool = False,
        recovery_count: int = 0,
        recovery_gap_ms: int = 0,
    ) -> None:
        hud_st, hud_dur_ms, beep_k = "", 0, ""
        try:
            res = run_pipeline_once(
                raw_wav, cfg=self.cfg, creds=self.creds, tracker=self.tracker,
                asr_provider=self.asr_provider, refiner=self.refiner, platform=self.platform,
                origin_window=origin_win, audio_source=src_used, seq_id=seq_id, dry_run=no_paste,
                is_current_seq=self.is_current_seq,
                on_stage=lambda st: self._on_pipeline_stage(seq_id, st, silent_hud),
                runtime_dir=self.runtime_dir,
                env=self.env,
            )
            summary = self._build_summary(
                seq_id,
                res,
                dry_run=no_paste,
                recorder_lost=recorder_lost,
                recovery_count=recovery_count,
                recovery_gap_ms=recovery_gap_ms,
            )
            if res.ok:
                if res.paste and not res.paste.pasted and not res.paste.clipboard:
                    hud_st, beep_k = "error", "error"
                elif res.paste and not res.paste.pasted and res.paste.clipboard:
                    hud_st = "copied"
                else:
                    hud_st = "done"
            elif res.skipped_reason in ("too_short", "silence", "empty_audio", "empty_asr"):
                if recorder_lost:
                    hud_st, hud_dur_ms, beep_k = "mic_disconnected", 2200, ""
                    summary["skipped_reason"] = "mic_disconnected"
                else:
                    hud_st, beep_k = "empty", "error"
            elif res.skipped_reason != "superseded":
                hud_st, beep_k = "error", "error"
        except Exception as exc:
            hud_st, beep_k = "error", "error"
            summary = {"seq_id": seq_id, "ok": False, "error": self._redact(exc)}
            if recorder_lost:
                summary["capture_interrupted"] = True
            if recovery_count > 0:
                summary["recovery_count"] = recovery_count
                summary["recovery_gap_ms"] = recovery_gap_ms
            if saved_p := str(getattr(exc, "_saved_audio_path", "") or ""):
                summary["saved_audio"] = saved_p
        if recorder_lost and beep_k == "error":
            beep_k = ""
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
                    self._hud(
                        hud_st,
                        duration_ms=hud_dur_ms,
                        clear_main=True if is_toast_hud_state(hud_st) else None,
                    )
                if beep_k:
                    self.platform.beep(beep_k)

    def handle_control(self, req: Mapping[str, Any]) -> dict[str, Any]:
        cmd = str(req.get("cmd") or "status")
        if cmd == "status":
            shim_ok = not bool(getattr(self.platform, "_shim_disabled", False))
            raw_shim_err = str(getattr(self.platform, "last_shim_error", "") or "")
            shim_err = self._redact_str(raw_shim_err) if raw_shim_err else ""
            with self._lock:
                resp: dict[str, Any] = {
                    "ok": True,
                    "version": __version__,
                    "pid": os.getpid(),
                    "state": self.state,
                    "seq_id": self.seq_id,
                    "asr_provider": select_asr_provider(self.cfg, self.creds),
                    "refiner_provider": self.cfg.refiner.provider,
                    "shim_ok": shim_ok,
                    "last_result": dict(self.last_result),
                }
                if shim_err:
                    resp["shim_error"] = shim_err
                return resp
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
            self._stop_evt.set(); self._cancel_max_duration_timer(); return {"ok": True, "stopping": True}
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
        self._cancel_max_duration_timer()
        if self._srv_sock is not None:
            try: self._srv_sock.close()
            except OSError: pass
            self._srv_sock = None
        self.sock_path.unlink(missing_ok=True); self.pid_path.unlink(missing_ok=True)
        self.platform.close(); self.refiner.close()
