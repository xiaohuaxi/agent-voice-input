"""Core serial-pipelined two-stage state machine daemon and single-shot pipeline for agent-voice-input."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import concurrent.futures, inspect, json, os, signal, socket, sys, threading, time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import __version__
from .asr_providers import (
    AsrError,
    AsrResult,
    BaseAsrProvider,
    ChatAudioAsrProvider,
    GeminiAudioProvider,
    _sleep_cooperative,
    create_asr_provider,
)
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
    raw_ax = m.get("ax_trusted")
    return ForegroundWindow(
        hwnd=int(m.get("hwnd") or 0),
        process=str(m.get("process") or ""),
        title=str(m.get("title") or ""),
        bundle_id=str(m.get("bundle_id") or ""),
        surface_id=str(m.get("surface_id") or ""),
        tty=str(m.get("tty") or ""),
        cwd=str(m.get("cwd") or ""),
        pid=int(m.get("pid") or 0),
        ax_trusted=raw_ax if isinstance(raw_ax, bool) else None,
        tmux_socket=str(m.get("tmux_socket") or ""),
        tmux_pane=str(m.get("tmux_pane") or ""),
    )


def _deliver_with_origin(
    platform: Any,
    text: str,
    *,
    seq_id: int,
    origin_window: ForegroundWindow,
    fallback_clipboard_text: str | None = None,
    preserve_clipboard: bool = False,
    suppress_beep: bool | Callable[[], bool] = False,
    is_cancelled: Callable[[], bool] | None = None,
    before_clipboard_write: Callable[[], None] | None = None,
) -> PasteOutcome:
    try:
        params = inspect.signature(platform.deliver_text).parameters
    except (TypeError, ValueError):
        params = {}
    has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())

    def _accepts(name: str) -> bool:
        return name in params or has_var_kw

    if before_clipboard_write is not None and not _accepts("before_clipboard_write"):
        before_clipboard_write()

    kw: dict[str, Any] = {"seq_id": seq_id, "target_hwnd": origin_window.hwnd}
    if _accepts("origin_window"):
        kw["origin_window"] = origin_window
    if fallback_clipboard_text is not None and _accepts("fallback_clipboard_text"):
        kw["fallback_clipboard_text"] = fallback_clipboard_text
    if preserve_clipboard and _accepts("preserve_clipboard"):
        kw["preserve_clipboard"] = True
    if suppress_beep and _accepts("suppress_beep"):
        kw["suppress_beep"] = suppress_beep
    if is_cancelled is not None and _accepts("is_cancelled"):
        kw["is_cancelled"] = is_cancelled
    if before_clipboard_write is not None and _accepts("before_clipboard_write"):
        kw["before_clipboard_write"] = before_clipboard_write
    return platform.deliver_text(text, **kw)


def _may_run_stage2_refiner(
    cfg: Config, refiner: BaseRefiner, asr_provider: BaseAsrProvider
) -> bool:
    prov = cfg.refiner.provider.strip().lower()
    if prov in ("", "off", "none", "false", "0", "disabled"):
        return False
    if getattr(refiner, "provider_name", "off") in ("", "off", "none"):
        return False
    if not cfg.refiner.force_on_single_hop and (
        isinstance(asr_provider, (GeminiAudioProvider, ChatAudioAsrProvider))
        or bool(getattr(asr_provider, "single_hop", False))
    ):
        return False
    return True


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
MAX_PENDING_UTTERANCES: int = 8
DEFAULT_PASTE_SETTLE_SEC: float = 0.15


def _make_cancel_check(
    seq_id: int,
    is_current_seq: Callable[[int], bool] | None,
    is_cancelled: Callable[[], bool] | None,
) -> Callable[[], bool] | None:
    if is_current_seq is None and is_cancelled is None:
        return None

    def _check() -> bool:
        if is_cancelled is not None and is_cancelled():
            return True
        if is_current_seq is not None and not is_current_seq(seq_id):
            return True
        return False

    return _check


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
    is_cancelled: Callable[[], bool] | None = None,
) -> tuple[AsrResult, int, tuple[float, ...]]:
    cancel_check = _make_cancel_check(seq_id, is_current_seq, is_cancelled)
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
    is_cancelled: Callable[[], bool] | None = None,
    deliver_fn: Callable[[str, ForegroundWindow], PasteOutcome] | None = None,
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
    eff_refiner = refiner or create_refiner(
        cfg, eff_creds, env=eff_env, runtime_dir=runtime_dir
    )
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
            cancel_check = _make_cancel_check(seq_id, is_current_seq, is_cancelled)
            if (
                not (cancel_check is not None and cancel_check())
                and _may_run_stage2_refiner(cfg, eff_refiner, eff_asr)
                and (prep_fn := getattr(eff_refiner, "prepare_session", None)) is not None
            ):
                try:
                    prep_fn(snap.session_key if snap else "")
                except Exception:
                    pass
            try:
                asr_res, chunk_count, chunk_secs = _transcribe_wav_chunks(
                    gate.wav_bytes,
                    eff_asr=eff_asr,
                    asr_ctx=asr_ctx,
                    floor_dbfs=cfg.audio.floor_dbfs,
                    max_chunk_sec=max_chunk_sec,
                    seq_id=seq_id,
                    is_current_seq=is_current_seq,
                    is_cancelled=is_cancelled,
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
                if cancel_check is not None and cancel_check():
                    saved_p = _stash_failed(gate, snap, win) if gate.duration_sec >= 15.0 else ""
                    return PipelineResult(False, seq_id, skipped_reason="superseded", audio_source=audio_source, gate=gate, session=snap, asr=asr_res, asr_chunks=chunk_count, asr_chunk_secs=chunk_secs, saved_audio=saved_p)
                saved_p = _stash_failed(gate, snap, win)
                return PipelineResult(False, seq_id, skipped_reason="empty_asr", audio_source=audio_source, gate=gate, session=snap, asr=asr_res, asr_chunks=chunk_count, asr_chunk_secs=chunk_secs, saved_audio=saved_p)
            if cancel_check is not None and cancel_check():
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
            if cancel_check is not None and cancel_check():
                saved_p = _stash_failed(gate, snap, win) if gate.duration_sec >= 15.0 else ""
                return PipelineResult(False, seq_id, final_text=final_text, skipped_reason="superseded", audio_source=audio_source, gate=gate, session=snap, asr=asr_res, refined=ref_res, asr_chunks=chunk_count, asr_chunk_secs=chunk_secs, saved_audio=saved_p)
            if dry_run or (platform is None and deliver_fn is None):
                paste_out = None
            elif deliver_fn is not None:
                paste_out = deliver_fn(final_text, win)
            else:
                paste_out = _deliver_with_origin(
                    platform,
                    final_text,
                    seq_id=seq_id,
                    origin_window=win,
                    is_cancelled=cancel_check,
                )
            if paste_out is not None and paste_out.reason == "cancelled" and cancel_check is not None and cancel_check():
                saved_p = _stash_failed(gate, snap, win) if gate.duration_sec >= 15.0 else ""
                return PipelineResult(False, seq_id, final_text=final_text, skipped_reason="superseded", audio_source=audio_source, gate=gate, session=snap, asr=asr_res, refined=ref_res, asr_chunks=chunk_count, asr_chunk_secs=chunk_secs, saved_audio=saved_p)
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


@dataclass(frozen=True)
class _QueuedUtterance:
    seq_id: int
    raw_wav: bytes
    audio_source: str
    origin_win: ForegroundWindow
    no_paste: bool = False
    silent_hud: bool = False
    recorder_lost: bool = False
    recovery_count: int = 0
    recovery_gap_ms: int = 0
    audio_error: str = ""


class VoiceInputDaemon:
    MAX_PENDING_UTTERANCES: int = MAX_PENDING_UTTERANCES

    def __init__(
        self,
        cfg: Config,
        *,
        env: Mapping[str, str] | None = None,
        runtime_dir: Path | None = None,
        platform: Any = None,
        asr_provider: BaseAsrProvider | None = None,
        refiner: BaseRefiner | None = None,
        tracker: SessionTracker | None = None,
        clock_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
        paste_settle_sec: float = DEFAULT_PASTE_SETTLE_SEC,
    ) -> None:
        self.cfg, self.env = cfg, dict(os.environ if env is None else env)
        self.creds = resolve_credentials(cfg, self.env)
        self.runtime_dir = ensure_secure_dir(runtime_dir or get_runtime_dir(self.env, create=True))
        self.platform = platform or create_platform(cfg, env=self.env)
        self.tracker = tracker or SessionTracker(env=self.env, runtime_dir=self.runtime_dir)
        self.asr_provider = asr_provider or create_asr_provider(cfg, self.creds, env=self.env)
        self.refiner = refiner or create_refiner(cfg, self.creds, env=self.env, runtime_dir=self.runtime_dir)
        self.clock_fn, self.sleep_fn = clock_fn, sleep_fn
        self._paste_settle_sec = max(0.0, float(paste_settle_sec))
        self._trigger_lock, self._lock, self._stop_evt = threading.Lock(), threading.Lock(), threading.Event()
        self._queue_cv = threading.Condition(self._lock)
        self.state, self.seq_id, self._last_trigger_ts = "idle", 0, -1e9
        self._recording_active: bool = False
        self._recording_seq_id: int = -1
        self._stopping_capture: bool = False
        self._utterance_queue: deque[_QueuedUtterance] = deque()
        self._active_task: _QueuedUtterance | None = None
        self._active_bg_stage: str = ""
        self._worker_thread: threading.Thread | None = None
        self._batch_unpasted_clips: list[str] = []
        self._clipboard_busy_until: float = -1e9
        self._main_hud_state: str = ""
        self._main_hud_visible: bool = False
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

    def _inflight_or_queued_count_locked(self) -> int:
        return (1 if self._active_task is not None else 0) + len(self._utterance_queue)

    def _is_seq_inflight_locked(self, seq_id: int) -> bool:
        if self._active_task is not None and self._active_task.seq_id == seq_id:
            return True
        return any(t.seq_id == seq_id for t in self._utterance_queue)

    def _derive_bg_stage_locked(self) -> str:
        if self._active_task is not None:
            return self._active_bg_stage or "recognizing"
        if self._utterance_queue:
            return "recognizing"
        return ""

    def _recompute_state_locked(self) -> str:
        if self._recording_active:
            st = "recording"
        elif bg := self._derive_bg_stage_locked():
            st = bg
        elif self._stopping_capture:
            st = "recognizing"
        else:
            st = "idle"
        self.state = st
        if st == "idle":
            self._batch_unpasted_clips.clear()
        return st

    def _is_recording_or_stopping(self) -> bool:
        with self._lock:
            return self._recording_active or self._stopping_capture

    def _enqueue_utterance_locked(self, task: _QueuedUtterance) -> None:
        self._utterance_queue.append(task)
        if self._worker_thread is None or not self._worker_thread.is_alive():
            w = threading.Thread(
                target=self._utterance_worker_loop,
                name="avi-utterance-worker",
                daemon=True,
            )
            self._worker_thread = w
            w.start()
        self._queue_cv.notify()

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
            if not self._stop_evt.is_set() and self._recording_active and self._recording_seq_id == seq_id:
                self._max_dur_timer = t
                start_it = True
        if old is not None:
            old.cancel()
        if start_it:
            t.start()

    def _on_max_duration_timeout(self, seq_id: int) -> None:
        if self._stop_evt.is_set():
            return
        with self._lock:
            if self._stop_evt.is_set() or not self._recording_active or self._recording_seq_id != seq_id:
                return
        self.handle_trigger("toggle", expected_seq_id=seq_id)

    def _redact_str(self, text: str) -> str:
        return redact_secrets(text, self.env, cfg=self.cfg, extra_secrets=self.creds.all_secret_values())

    def _redact(self, exc: Exception) -> str:
        return self._redact_str(str(exc))

    def _update_main_hud_visibility_locked(
        self,
        state: str,
        *,
        layer: str | None = None,
        clear_main: bool | None = None,
    ) -> None:
        st = state.strip().lower()
        lyr = (layer or "").strip().lower()
        if lyr == "toast":
            if clear_main is True:
                self._main_hud_state = ""
                self._main_hud_visible = False
            return
        if st in ("recording", "recognizing", "processing", "refining"):
            self._main_hud_state = st
            self._main_hud_visible = True
        elif st in ("hide", "hide_all", "idle", "done", "copied"):
            self._main_hud_state = ""
            self._main_hud_visible = False
        elif clear_main is True:
            self._main_hud_state = ""
            self._main_hud_visible = False

    def _hud(
        self,
        state: str,
        *,
        duration_ms: int = 0,
        layer: str | None = None,
        clear_main: bool | None = None,
        clear_toast: bool | None = None,
    ) -> None:
        with self._lock:
            self._update_main_hud_visibility_locked(state, layer=layer, clear_main=clear_main)
        if (fn := getattr(self.platform, "hud", None)) is None:
            return
        kw: dict[str, Any] = {}
        if duration_ms > 0:
            kw["duration_ms"] = duration_ms
        if layer is not None:
            kw["layer"] = layer
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
            if self._stop_evt.is_set() or not self._recording_active:
                return
            if seq_id is not None and self._recording_seq_id != seq_id:
                return
            rec_seq = self._recording_seq_id
            self._rec_recovery_count += 1
            self._rec_recovery_gap_ms += norm_gap
            if not self._cur_silent_hud and norm_gap >= 250:
                if self._rec_hud_ready_seq == rec_seq:
                    emit_toast = True
                else:
                    self._pending_recovery_toast_seq = rec_seq
        if emit_toast:
            self._hud("audio_recovered", clear_main=False)

    def _on_recorder_lost(self, *, seq_id: int | None = None) -> None:
        with self._trigger_lock:
            with self._lock:
                if self._stop_evt.is_set() or not self._recording_active:
                    return
                if seq_id is not None and self._recording_seq_id != seq_id:
                    return
                cur_seq = self._recording_seq_id
                self._recording_active = False
                self._recording_seq_id = -1
                self._stopping_capture = True
                eff_no_paste = self._cur_no_paste
                eff_silent = self._cur_silent_hud
                rec_cnt, rec_gap = self._rec_recovery_count, self._rec_recovery_gap_ms
                self._cur_no_paste, self._cur_silent_hud = False, False
                self._rec_recovery_count, self._rec_recovery_gap_ms = 0, 0
                self._rec_hud_ready_seq, self._pending_recovery_toast_seq = -1, -1
                win_snap = self._origin_win
                self._recompute_state_locked()
            self._cancel_max_duration_timer()
            stop_perm_exc: MicrophoneUnavailableError | None = None
            try:
                try:
                    raw_wav, src_used = self.platform.stop_recording(drain=False)
                except TypeError:
                    raw_wav, src_used = self.platform.stop_recording()
            except MicrophoneUnavailableError as exc:
                raw_wav = bytes(getattr(exc, "_raw_wav", b"") or b"")
                src_used = str(getattr(exc, "_audio_source", "") or "")
                if str(getattr(exc, "reason_code", "")) == "mic_permission":
                    stop_perm_exc = exc
            except Exception:
                raw_wav, src_used = b"", ""
            snap_audio_err = str(getattr(self.platform, "last_audio_error", "") or "")
            gate = None
            if len(raw_wav) > 44:
                try:
                    gate = process_wav(raw_wav, self.cfg.audio)
                except Exception:
                    gate = None
            lost_ts = self.clock_fn()
            if gate is None or not gate.passed:
                if stop_perm_exc is not None:
                    perm_hud = str(getattr(stop_perm_exc, "hud_state", "mic_permission") or "mic_permission")
                    perm_err = self._redact(stop_perm_exc)
                    with self._lock:
                        self._stopping_capture = False
                        resume_state = self._recompute_state_locked()
                        still_cur = not self._recording_active and self.seq_id == cur_seq
                        if still_cur:
                            self._recorder_lost_seq = cur_seq
                            self._recorder_lost_ts = lost_ts
                            self._recorder_lost_stop_absorbed = False
                        self.last_result = {
                            "seq_id": cur_seq,
                            "ok": False,
                            "skipped_reason": "mic_permission",
                            "audio_source": src_used,
                            "error": perm_err,
                        }
                    if not eff_silent:
                        if resume_state != "idle":
                            self._hud(resume_state)
                            self._hud(perm_hud, duration_ms=0, clear_main=False)
                        elif still_cur:
                            self._hud(perm_hud, duration_ms=0, clear_main=True)
                    if not eff_silent:
                        self.platform.beep("error")
                    return
                audio_err = self._redact_str(snap_audio_err or "mic_disconnected")
                with self._lock:
                    self._stopping_capture = False
                    resume_state = self._recompute_state_locked()
                    still_cur = not self._recording_active and self.seq_id == cur_seq
                    if still_cur:
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
                if not eff_silent:
                    if resume_state != "idle":
                        self._hud(resume_state)
                        self._hud("mic_disconnected", duration_ms=2200, clear_main=False)
                    elif still_cur:
                        self._hud("mic_disconnected", duration_ms=2200, clear_main=True)
                if not eff_silent:
                    self.platform.beep("error")
                return
            task = _QueuedUtterance(
                seq_id=cur_seq,
                raw_wav=raw_wav,
                audio_source=src_used,
                origin_win=win_snap,
                no_paste=eff_no_paste,
                silent_hud=eff_silent,
                recorder_lost=True,
                recovery_count=rec_cnt,
                recovery_gap_ms=rec_gap,
                audio_error=snap_audio_err,
            )
            with self._queue_cv:
                self._stopping_capture = False
                if self.seq_id == cur_seq:
                    self._recorder_lost_seq = cur_seq
                    self._recorder_lost_ts = lost_ts
                    self._recorder_lost_stop_absorbed = False
                self._enqueue_utterance_locked(task)
                main_stage = self._recompute_state_locked()
            if not eff_silent:
                self._hud(main_stage)
                self._hud("mic_disconnected", duration_ms=3200, clear_main=False)
                self.platform.beep("error")

    def _on_pipeline_stage(self, seq_id: int, stage: str, silent_hud: bool = False) -> None:
        if self._stop_evt.is_set():
            return
        with self._trigger_lock:
            emit = False
            with self._lock:
                if self._stop_evt.is_set() or self._active_task is None or self._active_task.seq_id != seq_id:
                    return
                self._active_bg_stage = stage
                if not self._recording_active and not self._stopping_capture:
                    new_st = self._recompute_state_locked()
                    emit = not silent_hud and new_st == stage and self._main_hud_state != stage
                    if emit:
                        self._update_main_hud_visibility_locked(stage)
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
        audio_error: str | None = None,
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
        eff_audio_err = str(getattr(self.platform, "last_audio_error", "") or "") if audio_error is None else audio_error
        if (not res.ok or recorder_lost) and eff_audio_err:
            summary["audio_error"] = self._redact_str(eff_audio_err)
        return summary

    def handle_trigger(self, action: str, window: ForegroundWindow | None = None, *, no_paste: bool = False, silent_hud: bool = False, loopback: bool = False, from_hotkey: bool = False, expected_seq_id: int | None = None) -> dict[str, Any]:
        arrival_ts = self.clock_fn()
        with self._trigger_lock:
            queue_full_toast = False
            with self._lock:
                if self._stop_evt.is_set():
                    return {"ok": False, "ignored": "stopping", "state": self.state, "seq_id": self.seq_id}
                if expected_seq_id is not None and (not self._recording_active or self._recording_seq_id != expected_seq_id):
                    return {"ok": False, "ignored": "stale_seq", "state": self.state, "seq_id": self.seq_id}
                eff_no_paste, eff_silent = bool(no_paste), bool(silent_hud)
                if (
                    action in ("toggle", "ptt_start")
                    and not self._recording_active
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
                    and not self._recording_active
                    and self.seq_id == self._recorder_lost_seq
                    and not self._recorder_lost_stop_absorbed
                    and (
                        self._is_seq_inflight_locked(self._recorder_lost_seq)
                        or 0.0 <= (arrival_ts - self._recorder_lost_ts) < 1.5
                    )
                ):
                    self._recorder_lost_stop_absorbed = True
                    return {"ok": True, "ignored": "already_stopped", "state": self.state, "seq_id": self.seq_id}
                if (
                    expected_seq_id is None
                    and action == "toggle"
                    and not self._recording_active
                    and self.seq_id == self._auto_stop_seq
                    and not self._auto_stop_absorbed
                    and 0.0 <= (arrival_ts - self._auto_stop_ts) < 1.5
                ):
                    self._auto_stop_absorbed = True
                    return {"ok": True, "ignored": "already_stopped", "state": self.state, "seq_id": self.seq_id}
                now = self.clock_fn()
                resume_state = "idle"
                stop_hud_stage = "recognizing"
                if action == "ptt_cancel":
                    if not self._recording_active:
                        return {"ok": True, "state": self.state, "seq_id": self.seq_id}
                    eff_silent = bool(silent_hud or self._cur_silent_hud)
                    self._recording_active = False
                    self._recording_seq_id = -1
                    self._cur_no_paste, self._cur_silent_hud = False, False
                    self._rec_recovery_count, self._rec_recovery_gap_ms = 0, 0
                    self._rec_hud_ready_seq, self._pending_recovery_toast_seq = -1, -1
                    self.seq_id += 1
                    cur_seq = self.seq_id
                    self._last_trigger_ts = now
                    resume_state = self._recompute_state_locked()
                    mode = "cancel"
                elif action in ("toggle", "ptt_start") and not self._recording_active:
                    if self._inflight_or_queued_count_locked() >= MAX_PENDING_UTTERANCES:
                        cur_state, cur_seq = self.state, self.seq_id
                        queue_full_toast = not eff_silent
                        mode = "queue_full"
                    else:
                        self._cur_no_paste, self._cur_silent_hud = bool(no_paste), bool(silent_hud)
                        self._rec_recovery_count, self._rec_recovery_gap_ms = 0, 0
                        self._rec_hud_ready_seq, self._pending_recovery_toast_seq = -1, -1
                        self._recorder_lost_seq, self._recorder_lost_ts, self._recorder_lost_stop_absorbed = -1, -1e9, False
                        self._auto_stop_seq, self._auto_stop_ts, self._auto_stop_absorbed = -1, -1e9, False
                        self._origin_win = window if window is not None else ForegroundWindow()
                        self.seq_id += 1
                        cur_seq = self.seq_id
                        self._recording_active = True
                        self._recording_seq_id = cur_seq
                        self._last_trigger_ts = now
                        self._recompute_state_locked()
                        mode = "start"
                elif action in ("toggle", "ptt_stop") and self._recording_active:
                    cur_seq = self._recording_seq_id
                    self._recording_active = False
                    self._recording_seq_id = -1
                    self._stopping_capture = True
                    eff_no_paste = bool(no_paste or self._cur_no_paste)
                    eff_silent = bool(silent_hud or self._cur_silent_hud)
                    rec_cnt, rec_gap = self._rec_recovery_count, self._rec_recovery_gap_ms
                    self._cur_no_paste, self._cur_silent_hud = False, False
                    self._rec_recovery_count, self._rec_recovery_gap_ms = 0, 0
                    self._rec_hud_ready_seq, self._pending_recovery_toast_seq = -1, -1
                    if expected_seq_id is None:
                        self._last_trigger_ts = now
                    win_snap = self._origin_win
                    stop_hud_stage = self._recompute_state_locked()
                    mode = "stop"
                else:
                    return {"ok": True, "state": self.state, "seq_id": self.seq_id}

            if mode == "queue_full":
                if queue_full_toast:
                    self._hud("mic_unavailable", clear_main=False)
                return {"ok": False, "ignored": "queue_full", "state": cur_state, "seq_id": cur_seq}

            if mode == "cancel":
                self._cancel_max_duration_timer()
                if not eff_silent:
                    if resume_state == "idle":
                        self._hud("hide", clear_toast=True)
                    else:
                        self._hud(resume_state, clear_toast=True)
                self.platform.cancel_recording()
                if not eff_silent:
                    self.platform.beep("error")
                return {"ok": True, "state": resume_state, "seq_id": cur_seq}

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
                        if self._recording_active and self._recording_seq_id == cur_seq:
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
                    with self._lock:
                        if self._recording_active and self._recording_seq_id == cur_seq:
                            self._origin_win = win_now
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
                        still_cur = self._recording_active and self._recording_seq_id == cur_seq
                        if still_cur:
                            self._recording_active = False
                            self._recording_seq_id = -1
                            self._cur_no_paste, self._cur_silent_hud = False, False
                            self._rec_recovery_count, self._rec_recovery_gap_ms = 0, 0
                            self._rec_hud_ready_seq, self._pending_recovery_toast_seq = -1, -1
                        err_state = self._recompute_state_locked()
                        lr: dict[str, Any] = {"seq_id": cur_seq, "ok": False, "error": err}
                        if err_reason:
                            lr["skipped_reason"] = err_reason
                        self.last_result = lr
                    if still_cur and not eff_silent:
                        if err_state == "idle":
                            self._hud(err_hud_state, duration_ms=err_hud_dur, clear_main=True)
                        else:
                            if rec_started:
                                self._hud(err_state)
                            self._hud(err_hud_state, duration_ms=err_hud_dur, clear_main=False)
                    if not eff_silent:
                        self.platform.beep("error")
                    out_err: dict[str, Any] = {"ok": False, "error": err, "state": err_state, "seq_id": cur_seq}
                    if err_reason:
                        out_err["skipped_reason"] = err_reason
                    return out_err

            self._cancel_max_duration_timer()
            if not eff_silent:
                self._hud(stop_hud_stage)
            try:
                raw_wav, src_used = self.platform.stop_recording()
                snap_audio_err = str(getattr(self.platform, "last_audio_error", "") or "")
                if expected_seq_id is not None:
                    done_ts = self.clock_fn()
                    with self._lock:
                        if self.seq_id == cur_seq:
                            self._auto_stop_seq = cur_seq
                            self._auto_stop_ts = done_ts
                            self._auto_stop_absorbed = False
                if not eff_silent:
                    self.platform.beep("stop")
            except Exception as exc:
                err = self._redact(exc)
                is_mic_err = isinstance(exc, (NoMicrophoneError, MicrophoneUnavailableError))
                err_reason = str(getattr(exc, "reason_code", "no_mic")) if is_mic_err else ""
                err_hud_state = str(getattr(exc, "hud_state", "no_mic")) if is_mic_err else "error"
                err_hud_dur = 2200 if err_hud_state == "no_mic" else 0
                with self._lock:
                    self._stopping_capture = False
                    err_stop_state = self._recompute_state_locked()
                    still_cur = not self._recording_active and self.seq_id == cur_seq
                    lr_stop: dict[str, Any] = {"seq_id": cur_seq, "ok": False, "error": err}
                    if err_reason:
                        lr_stop["skipped_reason"] = err_reason
                    self.last_result = lr_stop
                if not eff_silent:
                    if err_stop_state == "idle":
                        if still_cur:
                            self._hud(err_hud_state, duration_ms=err_hud_dur, clear_main=True)
                    else:
                        if err_stop_state != stop_hud_stage:
                            self._hud(err_stop_state)
                        self._hud(err_hud_state, duration_ms=err_hud_dur, clear_main=False)
                if not eff_silent:
                    self.platform.beep("error")
                out_stop_err: dict[str, Any] = {"ok": False, "error": err, "state": err_stop_state, "seq_id": cur_seq}
                if err_reason:
                    out_stop_err["skipped_reason"] = err_reason
                return out_stop_err

            task = _QueuedUtterance(
                seq_id=cur_seq,
                raw_wav=raw_wav,
                audio_source=src_used,
                origin_win=win_snap,
                no_paste=eff_no_paste,
                silent_hud=eff_silent,
                recorder_lost=False,
                recovery_count=rec_cnt,
                recovery_gap_ms=rec_gap,
                audio_error=snap_audio_err,
            )
            with self._queue_cv:
                self._stopping_capture = False
                self._enqueue_utterance_locked(task)
                out_state = self._recompute_state_locked()
            return {"ok": True, "state": out_state, "seq_id": cur_seq}

    def _wait_clipboard_settle(self) -> bool:
        with self._lock:
            busy_until = self._clipboard_busy_until
        rem = busy_until - self.clock_fn()
        if rem > 0.0:
            _sleep_cooperative(rem, sleep_fn=self.sleep_fn, is_cancelled=self._stop_evt.is_set)
            return True
        return False

    def _deliver_for_utterance(
        self,
        task: _QueuedUtterance,
        final_text: str,
        origin_win: ForegroundWindow,
    ) -> PasteOutcome:
        with self._lock:
            unpasted_snapshot = list(self._batch_unpasted_clips)
        fallback_txt = "\n\n".join([*unpasted_snapshot, final_text]) if unpasted_snapshot else None
        preserve_clip = bool(unpasted_snapshot)
        paste_out = _deliver_with_origin(
            self.platform,
            final_text,
            seq_id=task.seq_id,
            origin_window=origin_win,
            fallback_clipboard_text=fallback_txt,
            preserve_clipboard=preserve_clip,
            suppress_beep=lambda: self._is_recording_or_stopping() or task.silent_hud,
            is_cancelled=self._stop_evt.is_set,
            before_clipboard_write=self._wait_clipboard_settle,
        )
        now_done = self.clock_fn()
        with self._lock:
            if paste_out.used_clipboard_paste:
                self._clipboard_busy_until = now_done + self._paste_settle_sec
            if paste_out.pasted:
                if paste_out.used_clipboard_paste or paste_out.clipboard:
                    self._batch_unpasted_clips.clear()
            elif paste_out.clipboard:
                self._batch_unpasted_clips.append(final_text)
        return paste_out

    def _utterance_worker_loop(self) -> None:
        while True:
            with self._queue_cv:
                while not self._stop_evt.is_set() and not self._utterance_queue:
                    self._queue_cv.wait()
                if self._stop_evt.is_set():
                    self._utterance_queue.clear()
                    self._active_task = None
                    self._active_bg_stage = ""
                    self._recompute_state_locked()
                    return
                task = self._utterance_queue.popleft()
                self._active_task = task
                self._active_bg_stage = "recognizing"
                self._recompute_state_locked()
            if not task.silent_hud and not self._stop_evt.is_set():
                with self._trigger_lock:
                    emit_recog = False
                    with self._lock:
                        if (
                            not self._stop_evt.is_set()
                            and self._active_task is task
                            and not self._recording_active
                            and not self._stopping_capture
                            and self.state == "recognizing"
                            and self._main_hud_state != "recognizing"
                        ):
                            emit_recog = True
                            self._update_main_hud_visibility_locked("recognizing")
                    if emit_recog:
                        self._hud("recognizing")
            self._execute_queued_utterance(task)

    def _execute_queued_utterance(self, task: _QueuedUtterance) -> None:
        hud_st, hud_dur_ms, beep_k = "", 0, ""
        try:
            res = run_pipeline_once(
                task.raw_wav,
                cfg=self.cfg,
                creds=self.creds,
                tracker=self.tracker,
                asr_provider=self.asr_provider,
                refiner=self.refiner,
                platform=self.platform,
                origin_window=task.origin_win,
                audio_source=task.audio_source,
                seq_id=task.seq_id,
                dry_run=task.no_paste,
                is_cancelled=self._stop_evt.is_set,
                deliver_fn=(
                    None
                    if (task.no_paste or self.platform is None)
                    else (lambda txt, w: self._deliver_for_utterance(task, txt, w))
                ),
                on_stage=lambda st: self._on_pipeline_stage(task.seq_id, st, task.silent_hud),
                runtime_dir=self.runtime_dir,
                env=self.env,
            )
            summary = self._build_summary(
                task.seq_id,
                res,
                dry_run=task.no_paste,
                recorder_lost=task.recorder_lost,
                recovery_count=task.recovery_count,
                recovery_gap_ms=task.recovery_gap_ms,
                audio_error=task.audio_error,
            )
            if res.ok:
                if res.paste and not res.paste.pasted and not res.paste.clipboard:
                    hud_st, beep_k = "error", "error"
                elif res.paste and not res.paste.pasted and res.paste.clipboard:
                    hud_st = "copied"
                else:
                    hud_st = "done"
            elif res.skipped_reason in ("too_short", "silence", "empty_audio", "empty_asr"):
                if task.recorder_lost:
                    hud_st, hud_dur_ms, beep_k = "mic_disconnected", 2200, ""
                    summary["skipped_reason"] = "mic_disconnected"
                else:
                    hud_st, beep_k = "empty", "error"
            elif res.skipped_reason != "superseded":
                hud_st, beep_k = "error", "error"
        except Exception as exc:
            hud_st, beep_k = "error", "error"
            summary = {"seq_id": task.seq_id, "ok": False, "error": self._redact(exc)}
            if task.recorder_lost:
                summary["capture_interrupted"] = True
            if task.recovery_count > 0:
                summary["recovery_count"] = task.recovery_count
                summary["recovery_gap_ms"] = task.recovery_gap_ms
            if task.audio_error:
                summary["audio_error"] = self._redact_str(task.audio_error)
            if saved_p := str(getattr(exc, "_saved_audio_path", "") or ""):
                summary["saved_audio"] = saved_p

        if task.recorder_lost and beep_k == "error":
            beep_k = ""

        if self._stop_evt.is_set():
            with self._lock:
                if self._active_task is task:
                    self._active_task = None
                    self._active_bg_stage = ""
                self._recompute_state_locked()
            return

        with self._trigger_lock:
            hud_calls: list[tuple[str, int, str | None, bool | None]] = []
            play_beep = ""
            with self._lock:
                self.last_result = summary
                prev_state = self.state
                if self._active_task is task:
                    self._active_task = None
                    self._active_bg_stage = ""
                has_more = bool(self._utterance_queue)
                rec_active = self._recording_active or self._stopping_capture
                new_state = self._recompute_state_locked()
                if not self._stop_evt.is_set():
                    if task.silent_hud:
                        if not rec_active and not has_more and self._main_hud_visible:
                            hud_calls.append(("hide", 0, None, None))
                    elif rec_active:
                        if hud_st == "copied":
                            hud_calls.append(("copied", hud_dur_ms, "toast", False))
                        elif hud_st and is_toast_hud_state(hud_st):
                            hud_calls.append((hud_st, hud_dur_ms, None, False))
                    elif has_more and not self._utterance_queue[0].silent_hud:
                        if new_state == "recognizing" and self._main_hud_state != "recognizing":
                            hud_calls.append(("recognizing", 0, None, None))
                        if hud_st == "copied":
                            hud_calls.append(("copied", hud_dur_ms, "toast", False))
                        elif hud_st and is_toast_hud_state(hud_st):
                            hud_calls.append((hud_st, hud_dur_ms, None, False))
                        play_beep = beep_k
                    else:
                        if hud_st:
                            hud_calls.append(
                                (
                                    hud_st,
                                    hud_dur_ms,
                                    None,
                                    True if is_toast_hud_state(hud_st) else None,
                                )
                            )
                        elif self._main_hud_visible:
                            hud_calls.append(("hide", 0, None, None))
                        play_beep = beep_k
                    for st_n, _, lyr_n, clr_m_n in hud_calls:
                        self._update_main_hud_visibility_locked(st_n, layer=lyr_n, clear_main=clr_m_n)
            for st_name, dur_val, lyr_val, clr_main_val in hud_calls:
                self._hud(
                    st_name,
                    duration_ms=dur_val,
                    layer=lyr_val,
                    clear_main=clr_main_val,
                )
            if play_beep:
                self.platform.beep(play_beep)

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
                    "pending_count": self._inflight_or_queued_count_locked(),
                    "asr_provider": select_asr_provider(self.cfg, self.creds),
                    "refiner_provider": self.cfg.refiner.provider,
                    "shim_ok": shim_ok,
                    "last_result": dict(self.last_result),
                }
                if shim_err:
                    resp["shim_error"] = shim_err
                return resp
        if cmd in ("toggle", "rec_start", "rec_stop"):
            win = (
                _window_from_mapping(req)
                if any(
                    req.get(k)
                    for k in (
                        "hwnd",
                        "process",
                        "title",
                        "bundle_id",
                        "surface_id",
                        "tty",
                        "cwd",
                        "pid",
                        "tmux_socket",
                        "tmux_pane",
                    )
                )
                else None
            )
            return self.handle_trigger({"toggle": "toggle", "rec_start": "ptt_start", "rec_stop": "ptt_stop"}[cmd], win, no_paste=bool(req.get("no_paste")), silent_hud=bool(req.get("silent_hud")), loopback=bool(req.get("loopback")))
        if cmd == "selftest_wav":
            wav_path = Path(str(req.get("wav_path") or "")).expanduser()
            if not wav_path.is_file():
                return {"ok": False, "error": f"wav_path not found: {wav_path}"}
            with self._lock:
                self.seq_id += 1
                cur_seq = self.seq_id
            win = _window_from_mapping(req)
            res = run_pipeline_once(
                wav_path,
                cfg=self.cfg,
                creds=self.creds,
                tracker=self.tracker,
                asr_provider=self.asr_provider,
                refiner=self.refiner,
                platform=self.platform,
                origin_window=win,
                audio_source="selftest_wav",
                seq_id=cur_seq,
                dry_run=True,
                runtime_dir=self.runtime_dir,
                env=self.env,
            )
            summary = self._build_summary(cur_seq, res, dry_run=True, audio_error="")
            with self._lock:
                self.last_result = summary
            return {"ok": res.ok, "seq_id": cur_seq, "skipped_reason": res.skipped_reason, "asr_text": res.asr.text if res.asr else "", "final_text": res.final_text, "summary": summary}
        if cmd == "shutdown":
            self._stop_evt.set()
            self._cancel_max_duration_timer()
            with self._queue_cv:
                self._utterance_queue.clear()
                self._queue_cv.notify_all()
            return {"ok": True, "stopping": True}
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
        worker: threading.Thread | None = None
        with self._queue_cv:
            self._utterance_queue.clear()
            self._batch_unpasted_clips.clear()
            worker = self._worker_thread
            self._queue_cv.notify_all()
        if self._srv_sock is not None:
            try: self._srv_sock.close()
            except OSError: pass
            self._srv_sock = None
        self.sock_path.unlink(missing_ok=True); self.pid_path.unlink(missing_ok=True)
        self.refiner.close()
        if worker is not None and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=1.5)
        self.platform.close()
