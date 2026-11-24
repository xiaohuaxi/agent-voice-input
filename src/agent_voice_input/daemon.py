"""Core serial-pipelined two-stage state machine daemon for agent-voice-input."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import signal
import socket
import sys
import threading
import time
from typing import Any, Callable, Iterator, Mapping, Sequence

from .app_scope import (
    classify_window_category,
    is_remote_client_window,
    match_app_patterns,
)
from .asr_providers import (
    BaseAsrProvider,
    CancelToken,
    _sleep_cooperative,
    create_asr_provider,
)
from .audio_proc import (
    AudioGateResult,
    get_failed_audio_dir,
    process_wav,
    prune_failed_audio_dir,
    save_failed_audio,
    wait_recording_archives,
)
from .config import (
    Config,
    ensure_secure_dir,
    explain_pipeline_selection,
    get_runtime_dir,
    redact_secrets,
    resolve_credentials,
    select_refiner_provider,
)
from .control_ipc import (
    CONTROL_REQUEST_MAX_BYTES,
    CONTROL_REQUEST_READ_TIMEOUT_SEC,
    DAEMON_LOG_FILENAME,
    DEFAULT_DAEMON_LOG_MAX_BYTES,
    RECENT_RESULTS_MAXLEN,
    TELEMETRY_FILENAME,
    TELEMETRY_MAX_BYTES,
    DaemonAlreadyRunningError,
    _has_window_fields,
    _is_control_peer_disconnected,
    _parse_optional_seq_id,
    _parse_optional_timeout_sec,
    _recv_control_request_line,
    _window_from_mapping,
    acquire_daemon_lock,
    append_telemetry_record,
    build_daemon_status_response,
    compute_package_build_id,
    describe_daemon_drift,
    merge_recent_results,
    probe_daemon_status,
    read_telemetry_history,
    release_daemon_lock,
    rotate_daemon_log_if_needed,
    send_control_command,
    send_control_reply,
    write_daemon_pid_file,
)
from .daemon_pipeline import (
    DEFAULT_PASTE_SETTLE_SEC,
    MAX_ASR_CHUNK_CONCURRENCY,
    MAX_PENDING_UTTERANCES,
    MAX_STAGE2_CONCURRENCY,
    PipelineResult,
    _OriginWindowProbe,
    _Stage1State,
    _derive_failed_label,
    _execute_stage1,
    _existing_recoverable_retry_audio,
    _is_single_hop_asr_provider,
    _make_desktop_same_window_verifier,
    _resolve_effective_ui_language,
    _resolve_pipeline_window_routing,
    _resolve_stage1_context,
    _transcribe_wav_chunks,
    _will_run_stage2_refiner,
    build_pipeline_summary,
    classify_capture_error,
    finalize_commit_slot_outcome,
    make_isolated_probe_tracker,
    plan_commit_hud_calls,
    run_pipeline_once,
    run_probe_route_request,
    run_selftest_wav_request,
    validate_selftest_wav_request,
)
from .http_transport import evict_all_keepalive_connections
from .hud import format_copied_hud_text
from .platform_common import (
    ForegroundWindow,
    MicrophoneUnavailableError,
    NoMicrophoneError,
    PasteOutcome,
)
from .platform_macos import MacOSPlatform, is_macos_terminal_window
from .platform_wsl import WslPlatform, is_windows_terminal_process, is_wsl
from .refiner import BaseRefiner, RefinerResult, create_refiner
from .session_ctx import SessionSnapshot, SessionTracker, prune_stale_session_registry

_LOG_ROTATE_CHECK_INTERVAL_SEC: float = 60.0
_FAILED_PRUNE_CHECK_INTERVAL_SEC: float = 3600.0
_CLOSE_JOIN_TIMEOUT_SEC: float = 3.0


def create_platform(cfg: Config, *, env: Mapping[str, str] | None = None) -> WslPlatform | MacOSPlatform:
    if sys.platform == "darwin":
        return MacOSPlatform(cfg, env=env)
    if sys.platform == "linux" and not is_wsl(env):
        raise RuntimeError(
            "Linux desktop host is not yet supported (use macOS or Windows+WSL2 as local voice host)"
        )
    return WslPlatform(cfg, env=env)


@dataclass(slots=True)
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
    enqueued_ts: float = field(default_factory=time.monotonic)
    cancel_token: CancelToken = field(default_factory=CancelToken)
    stash_lock: threading.Lock = field(default_factory=threading.Lock)
    stashed_done: bool = False
    saved_audio: str = ""


@dataclass(slots=True, eq=False)
class _CommitSlot:
    task: _QueuedUtterance
    st: _Stage1State
    stage: str = "recognizing"
    done_evt: threading.Event = field(default_factory=threading.Event)
    stage2_ref_res: RefinerResult | None = None
    exc: Exception | None = None


class VoiceInputDaemon:
    MAX_PENDING_UTTERANCES: int = MAX_PENDING_UTTERANCES
    MAX_STAGE2_CONCURRENCY: int = MAX_STAGE2_CONCURRENCY
    _origin_resolve_timeout_sec: float = 3.0
    _CTRL_TRIGGER_QUEUE_MAXLEN: int = 32

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
        log_max_bytes: int = DEFAULT_DAEMON_LOG_MAX_BYTES,
    ) -> None:
        self.cfg, self.env = cfg, dict(os.environ if env is None else env)
        self.creds = resolve_credentials(cfg, self.env)
        self.runtime_dir = ensure_secure_dir(runtime_dir or get_runtime_dir(self.env, create=True))
        self.platform = platform or create_platform(cfg, env=self.env)
        if tracker is None:
            self.tracker = SessionTracker(
                env=self.env,
                runtime_dir=self.runtime_dir,
                remote_cfg=cfg.remote,
                context_max_chars=cfg.refiner.context_max_chars,
                cold_start_fallback=cfg.refiner.cold_start_fallback,
            )
        else:
            self.tracker = tracker
            if hasattr(tracker, "cold_start_fallback"):
                tracker.cold_start_fallback = cfg.refiner.cold_start_fallback
            if (cfg_rem := getattr(tracker, "configure_remote", None)) is not None:
                if cfg.remote.enabled:
                    cfg_rem(
                        cfg.remote,
                        runtime_dir=self.runtime_dir,
                        context_max_chars=cfg.refiner.context_max_chars,
                    )
                elif cfg.refiner.context_max_chars != getattr(tracker, "context_max_chars", cfg.refiner.context_max_chars):
                    tracker.context_max_chars = max(1000, int(cfg.refiner.context_max_chars))
        self.asr_provider = asr_provider or create_asr_provider(cfg, self.creds, env=self.env)
        ref_env: Mapping[str, str] | None = self.env if env is not None else None
        self._refiner_env = ref_env
        self.build_id: str = compute_package_build_id()
        self.resolved_refiner_provider = select_refiner_provider(cfg, self.creds, ref_env)
        self.refiner = refiner or create_refiner(cfg, self.creds, env=ref_env, runtime_dir=self.runtime_dir)
        self.active_pipeline: dict[str, Any] = explain_pipeline_selection(cfg, self.creds, ref_env)
        if (actual_asr := str(getattr(self.asr_provider, "provider_name", "") or "").strip()):
            self.active_pipeline["asr"]["provider"] = actual_asr
        self.active_pipeline["refiner"]["provider"] = self.resolved_refiner_provider
        self.clock_fn, self.sleep_fn = clock_fn, sleep_fn
        self._paste_settle_sec = max(0.0, float(paste_settle_sec))
        # Strict global lock hierarchy: _trigger_lock (T) -> _hud_lock (H) -> _lock / _queue_cv (L) -> Leaf locks
        self._trigger_lock = threading.Lock()
        self._hud_lock = threading.Lock()
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._queue_cv = threading.Condition(self._lock)
        self.state, self.seq_id, self._last_trigger_ts = "idle", 0, -1e9
        self._rec_seq_id: int = 0
        self._recording_active: bool = False
        self._recording_seq_id: int = -1
        self._recording_trigger_action: str = ""
        self._recording_shim_id: int = 0
        self._stopping_capture: bool = False
        self._utterance_queue: deque[_QueuedUtterance] = deque()
        self._commit_queue: deque[_CommitSlot] = deque()
        self._active_task: _QueuedUtterance | None = None
        self._worker_thread: threading.Thread | None = None
        self._commit_thread: threading.Thread | None = None
        self._stage2_active_count: int = 0
        self._stage2_slots: set[_CommitSlot] = set()
        self._stage2_threads: set[threading.Thread] = set()
        self._batch_unpasted_clips: list[str] = []
        self._clipboard_busy_until: float = -1e9
        self._main_hud_state: str = ""
        self._main_hud_visible: bool = False
        self._main_hud_bg_count: int = 0
        self._origin_win, self.last_result, self._srv_sock = ForegroundWindow(), {}, None
        self.recent_results: deque[dict[str, Any]] = deque(maxlen=RECENT_RESULTS_MAXLEN)
        self._cur_no_paste, self._cur_silent_hud = False, False
        self._rec_recovery_count, self._rec_recovery_gap_ms = 0, 0
        self._rec_hud_ready_seq, self._pending_recovery_toast_seq = -1, -1
        self._recorder_lost_seq, self._recorder_lost_ts, self._recorder_lost_stop_absorbed = -1, -1e9, False
        self._auto_stop_seq, self._auto_stop_ts, self._auto_stop_absorbed = -1, -1e9, False
        self._max_dur_timer: threading.Timer | None = None
        self._archive_threads: set[threading.Thread] = set()
        self._deliver_lock = threading.Lock()
        self._selftest_lock = threading.Lock()
        self._probe_sem = threading.BoundedSemaphore(4)
        self._ctrl_trigger_cv = threading.Condition()
        self._ctrl_trigger_q: deque[tuple[socket.socket, dict[str, Any]]] = deque()
        self._ctrl_trigger_worker: threading.Thread | None = None
        self._control_threads: set[threading.Thread] = set()
        self._control_cancel_tokens: set[CancelToken] = set()
        self._refiner_users: int = 0
        self._tracker_users: int = 0
        self._deferred_close_refiner: bool = False
        self._deferred_close_tracker: bool = False
        self._refiner_closed: bool = False
        self._tracker_closed: bool = False
        self._lock_fd: int | None = None
        self._owns_runtime_files: bool = False
        self._log_max_bytes: int = max(0, int(log_max_bytes))
        self._log_rotate_lock = threading.Lock()
        self._last_log_rotate_check: float = -1e9
        self._failed_prune_lock = threading.Lock()
        self._last_failed_prune_check: float = -1e9
        if (setter := getattr(self.platform, "set_recorder_lost_callback", None)) is not None:
            setter(self._on_recorder_lost)
        if (rec_setter := getattr(self.platform, "set_recorder_recovered_callback", None)) is not None:
            rec_setter(self._on_recorder_recovered)
        try:
            prune_stale_session_registry(self.runtime_dir, proc_root=self.tracker.proc_root)
        except Exception:
            pass
        self._maybe_prune_failed_audio(force=True)

    @property
    def lock_path(self) -> Path: return self.runtime_dir / "daemon.lock"
    @property
    def pid_path(self) -> Path: return self.runtime_dir / "daemon.pid"
    @property
    def sock_path(self) -> Path: return self.runtime_dir / "control.sock"
    @property
    def telemetry_path(self) -> Path: return self.runtime_dir / TELEMETRY_FILENAME
    @property
    def log_path(self) -> Path: return self.runtime_dir / DAEMON_LOG_FILENAME

    def _maybe_rotate_daemon_log(self, *, force: bool = False) -> bool:
        if self._log_max_bytes <= 0:
            return False
        now = self.clock_fn()
        with self._log_rotate_lock:
            if not force and (now - self._last_log_rotate_check) < _LOG_ROTATE_CHECK_INTERVAL_SEC:
                return False
            self._last_log_rotate_check = now
            return rotate_daemon_log_if_needed(self.log_path, max_bytes=self._log_max_bytes)

    def _maybe_prune_failed_audio(self, *, force: bool = False) -> bool:
        now = self.clock_fn()
        with self._failed_prune_lock:
            if not force and (now - self._last_failed_prune_check) < _FAILED_PRUNE_CHECK_INTERVAL_SEC:
                return False
            self._last_failed_prune_check = now
        try:
            prune_failed_audio_dir(get_failed_audio_dir(self.runtime_dir, create=False))
            return True
        except Exception:
            return False

    def _log_info(self, message: str) -> None:
        self._maybe_rotate_daemon_log(force=True)
        clean = self._redact(message)
        try:
            print(f"[agent-voice-input] INFO: {clean}", file=sys.stderr, flush=True)
        except Exception:
            pass

    def acquire_lock(self) -> None:
        if self._lock_fd is not None:
            return
        self._lock_fd = acquire_daemon_lock(self.runtime_dir, self.lock_path, self.pid_path)
        self._owns_runtime_files = True

    def _record_result_locked(self, summary: dict[str, Any]) -> dict[str, Any]:
        summary.setdefault("ts", round(time.time(), 6))
        self.last_result = summary
        snap = dict(summary)
        self.recent_results.append(snap)
        return snap

    def _append_telemetry(self, summary: Mapping[str, Any]) -> None:
        append_telemetry_record(self.runtime_dir, summary)
        self._maybe_rotate_daemon_log()
        self._maybe_prune_failed_audio()

    def is_current_seq(self, seq_id: int) -> bool:
        with self._lock:
            return not self._stop_evt.is_set() and self.seq_id == seq_id

    def _iter_pipeline_tasks_locked(self) -> Iterator[tuple[_QueuedUtterance, str]]:
        for s in self._commit_queue:
            yield s.task, (s.stage or "refining")
        if self._active_task is not None:
            yield self._active_task, "recognizing"
        for t in self._utterance_queue:
            yield t, "recognizing"

    def _inflight_or_queued_count_locked(self) -> int:
        return sum(1 for t, _ in self._iter_pipeline_tasks_locked() if not t.cancel_token.is_cancelled())

    def _visible_inflight_count_locked(self) -> int:
        return sum(
            1
            for t, _ in self._iter_pipeline_tasks_locked()
            if not t.cancel_token.is_cancelled() and not t.silent_hud
        )

    def _raw_pending_count_locked(self) -> int:
        return len(self._commit_queue) + (1 if self._active_task is not None else 0) + len(self._utterance_queue)

    def _is_seq_inflight_locked(self, seq_id: int) -> bool:
        return any(t.seq_id == seq_id and not t.cancel_token.is_cancelled() for t, _ in self._iter_pipeline_tasks_locked())

    def _any_seq_present_locked(self, seq_ids: set[int]) -> bool:
        return any(t.seq_id in seq_ids for t, _ in self._iter_pipeline_tasks_locked())

    def _has_uncancelled_ahead_locked(self, target_seq_ids: set[int]) -> bool:
        for t, _ in self._iter_pipeline_tasks_locked():
            if t.seq_id in target_seq_ids:
                return False
            if not t.cancel_token.is_cancelled():
                return True
        return False

    def _derive_bg_stage_locked(self, *, visible_only: bool = False) -> str:
        for t, stg in self._iter_pipeline_tasks_locked():
            if not t.cancel_token.is_cancelled() and (not visible_only or not t.silent_hud):
                return stg
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
        if st == "idle" and not self._commit_queue and self._active_task is None and not self._utterance_queue:
            self._batch_unpasted_clips.clear()
        return st

    def _is_recording_or_stopping(self) -> bool:
        with self._lock:
            return self._recording_active or self._stopping_capture

    def _clear_recording_state_locked(self) -> None:
        self._recording_active = False
        self._recording_seq_id = -1
        self._recording_trigger_action = ""
        self._recording_shim_id = 0
        self._cur_no_paste, self._cur_silent_hud = False, False
        self._rec_recovery_count, self._rec_recovery_gap_ms = 0, 0
        self._rec_hud_ready_seq, self._pending_recovery_toast_seq = -1, -1

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
        self._queue_cv.notify_all()

    def _enqueue_commit_slot_locked(self, slot: _CommitSlot) -> None:
        self._commit_queue.append(slot)
        if self._commit_thread is None or not self._commit_thread.is_alive():
            c = threading.Thread(
                target=self._commit_worker_loop,
                name="avi-commit-worker",
                daemon=True,
            )
            self._commit_thread = c
            c.start()
        self._recompute_state_locked()
        self._queue_cv.notify_all()

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
        bg_count: int = 0,
    ) -> None:
        st = state.strip().lower()
        lyr = (layer or "").strip().lower()
        if lyr == "toast":
            if clear_main is True:
                self._main_hud_state = ""
                self._main_hud_visible = False
                self._main_hud_bg_count = 0
            return
        if st in ("recording", "recognizing", "processing", "refining"):
            self._main_hud_state = st
            self._main_hud_visible = True
            self._main_hud_bg_count = max(0, int(bg_count)) if st == "recording" else 0
        elif st in ("hide", "hide_all", "idle", "done", "copied"):
            self._main_hud_state = ""
            self._main_hud_visible = False
            self._main_hud_bg_count = 0
        elif clear_main is True:
            self._main_hud_state = ""
            self._main_hud_visible = False
            self._main_hud_bg_count = 0

    def _invoke_platform_hud_unlocked(
        self,
        state: str,
        *,
        text: str = "",
        duration_ms: int = 0,
        layer: str | None = None,
        clear_main: bool | None = None,
        clear_toast: bool | None = None,
        bg_count: int = 0,
    ) -> None:
        if (fn := getattr(self.platform, "hud", None)) is None:
            return
        kw: dict[str, Any] = {}
        if text:
            kw["text"] = text
        if duration_ms > 0:
            kw["duration_ms"] = duration_ms
        if layer is not None:
            kw["layer"] = layer
        if clear_main is not None:
            kw["clear_main"] = clear_main
        if clear_toast is not None:
            kw["clear_toast"] = clear_toast
        if bg_count > 0:
            kw["bg_count"] = int(bg_count)
        try:
            fn(state, **kw)
        except TypeError:
            raise
        except Exception:
            pass

    def _emit_hud_calls_unlocked(
        self,
        calls: Sequence[tuple[str, str, int, str | None, bool | None, bool | None, int]],
    ) -> None:
        if not calls or getattr(self.platform, "hud", None) is None or self._stop_evt.is_set():
            return
        for st_n, txt_n, dur_n, lyr_n, clr_m_n, clr_t_n, bg_n in calls:
            if self._stop_evt.is_set():
                break
            self._invoke_platform_hud_unlocked(
                st_n,
                text=txt_n,
                duration_ms=dur_n,
                layer=lyr_n,
                clear_main=clr_m_n,
                clear_toast=clr_t_n,
                bg_count=bg_n,
            )

    def _hud(
        self,
        state: str,
        *,
        duration_ms: int = 0,
        layer: str | None = None,
        clear_main: bool | None = None,
        clear_toast: bool | None = None,
        bg_count: int = 0,
    ) -> None:
        with self._hud_lock:
            with self._lock:
                self._update_main_hud_visibility_locked(
                    state, layer=layer, clear_main=clear_main, bg_count=bg_count
                )
            self._emit_hud_calls_unlocked(
                [(state, "", duration_ms, layer, clear_main, clear_toast, bg_count)]
            )

    def _maybe_emit_bg_main_hud(self, desired_stage: str) -> None:
        if self._stop_evt.is_set():
            return
        with self._hud_lock:
            calls: list[tuple[str, str, int, str | None, bool | None, bool | None, int]] = []
            with self._lock:
                if (
                    not self._stop_evt.is_set()
                    and not self._recording_active
                    and not self._stopping_capture
                ):
                    self._recompute_state_locked()
                    vis_st = self._derive_bg_stage_locked(visible_only=True)
                    if vis_st == desired_stage and self._main_hud_state != desired_stage:
                        self._update_main_hud_visibility_locked(desired_stage)
                        calls.append((desired_stage, "", 0, None, None, None, 0))
            self._emit_hud_calls_unlocked(calls)

    def _emit_recording_cancel_hud(self, *, show_cancelled_toast: bool) -> str:
        with self._hud_lock:
            calls: list[tuple[str, str, int, str | None, bool | None, bool | None, int]] = []
            with self._lock:
                cur_state = self._recompute_state_locked()
                vis_bg = self._derive_bg_stage_locked(visible_only=True)
                rec_busy = self._recording_active or self._stopping_capture
                if not self._stop_evt.is_set() and not rec_busy:
                    if show_cancelled_toast:
                        if vis_bg:
                            if self._main_hud_state != vis_bg:
                                calls.append((vis_bg, "", 0, None, None, True, 0))
                            calls.append(("cancelled", "", 0, None, False, None, 0))
                        else:
                            calls.append(("cancelled", "", 0, None, True, None, 0))
                    else:
                        calls.append(((vis_bg if vis_bg else "hide"), "", 0, None, None, True, 0))
                    for st_n, _, _, lyr_n, clr_m_n, _, _ in calls:
                        self._update_main_hud_visibility_locked(st_n, layer=lyr_n, clear_main=clr_m_n)
            self._emit_hud_calls_unlocked(calls)
        return cur_state

    def _emit_capture_error_hud(
        self,
        *,
        cur_seq: int,
        toast_state: str,
        duration_ms: int = 0,
        restore_main_if_needed: bool = True,
        clear_main_if_idle: bool = True,
    ) -> None:
        with self._hud_lock:
            calls: list[tuple[str, str, int, str | None, bool | None, bool | None, int]] = []
            with self._lock:
                if not self._stop_evt.is_set():
                    rec_busy = self._recording_active or self._stopping_capture
                    vis_bg = self._derive_bg_stage_locked(visible_only=True)
                    still_cur = not rec_busy and self._rec_seq_id == cur_seq
                    if rec_busy:
                        pass
                    elif vis_bg:
                        if restore_main_if_needed and self._main_hud_state != vis_bg:
                            calls.append((vis_bg, "", 0, None, None, None, 0))
                        calls.append((toast_state, "", duration_ms, None, False, None, 0))
                    elif still_cur:
                        clr_m = True if clear_main_if_idle else bool(self._main_hud_visible)
                        calls.append((toast_state, "", duration_ms, None, clr_m, None, 0))
                    for st_n, _, _, lyr_n, clr_m_n, _, _ in calls:
                        self._update_main_hud_visibility_locked(st_n, layer=lyr_n, clear_main=clr_m_n)
            self._emit_hud_calls_unlocked(calls)

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
                eff_no_paste = self._cur_no_paste
                eff_silent = self._cur_silent_hud
                rec_cnt, rec_gap = self._rec_recovery_count, self._rec_recovery_gap_ms
                self._clear_recording_state_locked()
                self._stopping_capture = True
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
                is_perm = stop_perm_exc is not None
                toast_st = (
                    str(getattr(stop_perm_exc, "hud_state", "mic_permission") or "mic_permission")
                    if is_perm
                    else "mic_disconnected"
                )
                toast_dur = 0 if is_perm else 2200
                lr_lost: dict[str, Any] = {
                    "seq_id": cur_seq,
                    "ok": False,
                    "skipped_reason": "mic_permission" if is_perm else "mic_disconnected",
                    "audio_source": src_used,
                    "error": self._redact(stop_perm_exc)
                    if stop_perm_exc is not None
                    else self._redact_str(snap_audio_err or "mic_disconnected"),
                }
                if not is_perm:
                    lr_lost["capture_interrupted"] = True
                    if rec_cnt > 0:
                        lr_lost["recovery_count"] = rec_cnt
                        lr_lost["recovery_gap_ms"] = rec_gap
                with self._lock:
                    self._stopping_capture = False
                    self._recompute_state_locked()
                    if not self._recording_active and self._rec_seq_id == cur_seq:
                        self._recorder_lost_seq = cur_seq
                        self._recorder_lost_ts = lost_ts
                        self._recorder_lost_stop_absorbed = False
                    lost_snap = self._record_result_locked(lr_lost)
                self._append_telemetry(lost_snap)
                if not eff_silent:
                    self._emit_capture_error_hud(
                        cur_seq=cur_seq,
                        toast_state=toast_st,
                        duration_ms=toast_dur,
                        restore_main_if_needed=True,
                    )
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
                enqueued_ts=self.clock_fn(),
            )
            with self._queue_cv:
                self._stopping_capture = False
                if self._stop_evt.is_set():
                    self._recompute_state_locked()
                    return
                if self._rec_seq_id == cur_seq:
                    self._recorder_lost_seq = cur_seq
                    self._recorder_lost_ts = lost_ts
                    self._recorder_lost_stop_absorbed = False
                self._enqueue_utterance_locked(task)
                self._recompute_state_locked()
            if not eff_silent:
                self._emit_capture_error_hud(
                    cur_seq=cur_seq,
                    toast_state="mic_disconnected",
                    duration_ms=3200,
                    restore_main_if_needed=True,
                    clear_main_if_idle=False,
                )
                self.platform.beep("error")

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
        eff_audio_err = str(getattr(self.platform, "last_audio_error", "") or "") if audio_error is None else audio_error
        return build_pipeline_summary(
            seq_id,
            res,
            dry_run=dry_run,
            recorder_lost=recorder_lost,
            recovery_count=recovery_count,
            recovery_gap_ms=recovery_gap_ms,
            audio_error=eff_audio_err,
            redact_fn=self._redact_str,
        )

    def _is_terminal_window(self, win: ForegroundWindow) -> bool:
        if (checker := getattr(self.platform, "is_allowed_terminal", None)) is not None:
            return bool(checker(win))
        return is_windows_terminal_process(win.process) or is_macos_terminal_window(
            win.process, win.bundle_id
        )

    def _should_yield_hotkey(self, win: ForegroundWindow) -> str | None:
        hk = self.cfg.hotkey
        if hk.ignore_apps and match_app_patterns(win, hk.ignore_apps):
            return "ignored_app"
        if hk.yield_remote_desktop and is_remote_client_window(win):
            return "remote_client"
        return None

    def _handle_capture_failure(
        self,
        cur_seq: int,
        exc: Exception,
        *,
        eff_silent: bool,
        restore_main_if_needed: bool,
        clear_recording: bool = False,
        clear_stopping: bool = False,
    ) -> dict[str, Any]:
        err, err_reason, err_hud_state, err_hud_dur = classify_capture_error(exc, self._redact)
        with self._lock:
            still_cur = True
            if clear_recording:
                still_cur = self._recording_active and self._recording_seq_id == cur_seq
                if still_cur:
                    self._clear_recording_state_locked()
            if clear_stopping:
                self._stopping_capture = False
            err_state = self._recompute_state_locked()
            lr: dict[str, Any] = {"seq_id": cur_seq, "ok": False, "error": err}
            if err_reason:
                lr["skipped_reason"] = err_reason
            lr_snap = self._record_result_locked(lr)
        self._append_telemetry(lr_snap)
        if still_cur and not eff_silent:
            self._emit_capture_error_hud(
                cur_seq=cur_seq,
                toast_state=err_hud_state,
                duration_ms=err_hud_dur,
                restore_main_if_needed=restore_main_if_needed,
            )
        if not eff_silent:
            self.platform.beep("error")
        out_err: dict[str, Any] = {"ok": False, "error": err, "state": err_state, "seq_id": cur_seq}
        if err_reason:
            out_err["skipped_reason"] = err_reason
        return out_err

    def _start_gate_reason(self, win: ForegroundWindow) -> str | None:
        if (yielded := self._should_yield_hotkey(win)) is not None:
            return yielded
        scope = (self.cfg.hotkey.scope or "all").strip().lower()
        if scope == "all":
            return None
        cat = classify_window_category(win, is_terminal=self._is_terminal_window)
        if cat == "terminal" or (scope == "coding" and cat == "coding_gui"):
            return None
        return "non_terminal" if scope == "terminals" else "out_of_scope"

    def handle_trigger(self, action: str, window: ForegroundWindow | None = None, *, no_paste: bool = False, silent_hud: bool = False, loopback: bool = False, from_hotkey: bool = False, expected_seq_id: int | None = None, shim_instance_id: int = 0) -> dict[str, Any]:
        arrival_ts = self.clock_fn()
        with self._trigger_lock:
            queue_full_toast = False
            with self._lock:
                if self._stop_evt.is_set():
                    return {"ok": False, "ignored": "stopping", "state": self.state, "seq_id": self.seq_id}
                if expected_seq_id is not None and (not self._recording_active or self._recording_seq_id != expected_seq_id):
                    return {"ok": False, "ignored": "stale_seq", "state": self.state, "seq_id": self.seq_id}
                eff_no_paste, eff_silent = bool(no_paste), bool(silent_hud)
                if expected_seq_id is None and action in ("toggle", "ptt_start") and (arrival_ts - self._last_trigger_ts) * 1000.0 < self.cfg.hotkey.debounce_ms:
                    return {"ok": False, "ignored": "debounced", "state": self.state, "seq_id": self.seq_id}
                if (
                    action == "toggle"
                    and not self._recording_active
                    and self._rec_seq_id == self._recorder_lost_seq
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
                    and self._rec_seq_id == self._auto_stop_seq
                    and not self._auto_stop_absorbed
                    and 0.0 <= (arrival_ts - self._auto_stop_ts) < 1.5
                ):
                    self._auto_stop_absorbed = True
                    return {"ok": True, "ignored": "already_stopped", "state": self.state, "seq_id": self.seq_id}
                if (
                    action in ("toggle", "ptt_start")
                    and not self._recording_active
                    and not eff_no_paste
                    and (from_hotkey or window is not None)
                ):
                    cand_win = window if window is not None else ForegroundWindow()
                    if (blocked := self._start_gate_reason(cand_win)) is not None:
                        return {
                            "ok": False,
                            "ignored": blocked,
                            "state": self.state,
                            "seq_id": self.seq_id,
                        }
                now = self.clock_fn()
                resume_state = "idle"
                stop_hud_stage = "recognizing"
                cancelled_rec_seq = -1
                if action == "ptt_cancel":
                    if not self._recording_active:
                        return {"ok": True, "state": self.state, "seq_id": self.seq_id}
                    eff_silent = bool(silent_hud or self._cur_silent_hud)
                    cancelled_rec_seq = self._recording_seq_id
                    self._clear_recording_state_locked()
                    self.seq_id += 1
                    cur_seq = self.seq_id
                    self._rec_seq_id = cur_seq
                    self._last_trigger_ts = now
                    resume_state = self._recompute_state_locked()
                    mode = "cancel"
                elif action in ("toggle", "ptt_start") and not self._recording_active:
                    if (
                        self._inflight_or_queued_count_locked() >= MAX_PENDING_UTTERANCES
                        or self._raw_pending_count_locked() >= MAX_PENDING_UTTERANCES * 2
                    ):
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
                        self._rec_seq_id = cur_seq
                        self._recording_active = True
                        self._recording_seq_id = cur_seq
                        self._recording_trigger_action = action
                        self._recording_shim_id = (
                            int(shim_instance_id or 0)
                            if (from_hotkey and action == "ptt_start")
                            else 0
                        )
                        self._last_trigger_ts = now
                        self._recompute_state_locked()
                        mode = "start"
                elif action in ("toggle", "ptt_stop") and self._recording_active:
                    cur_seq = self._recording_seq_id
                    eff_no_paste = bool(no_paste or self._cur_no_paste)
                    eff_silent = bool(silent_hud or self._cur_silent_hud)
                    rec_cnt, rec_gap = self._rec_recovery_count, self._rec_recovery_gap_ms
                    self._clear_recording_state_locked()
                    self._stopping_capture = True
                    if expected_seq_id is None:
                        self._last_trigger_ts = now
                    win_snap = self._origin_win
                    self._recompute_state_locked()
                    mode = "stop"
                else:
                    return {"ok": True, "state": self.state, "seq_id": self.seq_id}

            if mode == "queue_full":
                if queue_full_toast:
                    self._hud("mic_unavailable", clear_main=False)
                return {"ok": False, "ignored": "queue_full", "state": cur_state, "seq_id": cur_seq}

            if mode == "cancel":
                self._cancel_max_duration_timer()
                self._cancel_remote_prefetch(seq_id=cancelled_rec_seq if cancelled_rec_seq > 0 else None)
                self.platform.cancel_recording()
                if not eff_silent:
                    resume_state = self._emit_recording_cancel_hud(show_cancelled_toast=False)
                    self.platform.beep("error")
                return {"ok": True, "state": resume_state, "seq_id": cur_seq}

            if mode == "start":
                self._cancel_max_duration_timer()
                rec_started = False
                origin_probe = _OriginWindowProbe(self.platform, window, cur_seq)
                try:
                    eff_loopback = bool(
                        loopback
                        or self.env.get("AGENT_VOICE_INPUT_WASAPI_LOOPBACK") == "1"
                        or self.env.get("VOICE_INPUT_WASAPI_LOOPBACK") == "1"
                    )
                    if loopback:
                        self.platform.start_recording(self.runtime_dir, loopback=True, seq_id=cur_seq)
                    else:
                        self.platform.start_recording(self.runtime_dir, seq_id=cur_seq)
                    rec_started = True
                    if self._stop_evt.is_set():
                        self._cancel_max_duration_timer()
                        try:
                            self.platform.cancel_recording()
                        except Exception:
                            pass
                        with self._lock:
                            if self._recording_active and self._recording_seq_id == cur_seq:
                                self._clear_recording_state_locked()
                            stop_st = self._recompute_state_locked()
                        return {"ok": False, "ignored": "stopping", "state": stop_st, "seq_id": cur_seq}
                    if not eff_silent:
                        with self._hud_lock:
                            rec_hud_calls: list[
                                tuple[str, str, int, str | None, bool | None, bool | None, int]
                            ] = []
                            with self._lock:
                                if (
                                    not self._stop_evt.is_set()
                                    and self._recording_active
                                    and self._recording_seq_id == cur_seq
                                ):
                                    bg_cnt = self._visible_inflight_count_locked()
                                    self._update_main_hud_visibility_locked(
                                        "recording", bg_count=bg_cnt
                                    )
                                    rec_hud_calls.append(
                                        ("recording", "", 0, None, None, True, bg_cnt)
                                    )
                            self._emit_hud_calls_unlocked(rec_hud_calls)
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
                    win_now = origin_probe.resolve(self._origin_resolve_timeout_sec)
                    with self._lock:
                        if self._stop_evt.is_set():
                            if self._recording_active and self._recording_seq_id == cur_seq:
                                self._clear_recording_state_locked()
                            stop_st_end = self._recompute_state_locked()
                            stopping_end = True
                        else:
                            stopping_end = False
                            if self._recording_active and self._recording_seq_id == cur_seq:
                                self._origin_win = win_now
                    if stopping_end:
                        self._cancel_max_duration_timer()
                        try:
                            self.platform.cancel_recording()
                        except Exception:
                            pass
                        return {"ok": False, "ignored": "stopping", "state": stop_st_end, "seq_id": cur_seq}
                    if not eff_loopback and not win_now.origin_drifted:
                        self._maybe_prefetch_remote(win_now, seq_id=cur_seq)
                    self._arm_max_duration_timer(cur_seq)
                    return {"ok": True, "state": "recording", "seq_id": cur_seq}
                except Exception as exc:
                    self._cancel_max_duration_timer()
                    if rec_started:
                        try:
                            self.platform.cancel_recording()
                        except Exception:
                            pass
                    return self._handle_capture_failure(
                        cur_seq,
                        exc,
                        eff_silent=eff_silent,
                        restore_main_if_needed=rec_started,
                        clear_recording=True,
                    )

            self._cancel_max_duration_timer()
            stop_rec_lost = False
            try:
                raw_wav, src_used = self.platform.stop_recording()
                snap_audio_err = str(getattr(self.platform, "last_audio_error", "") or "")
                stop_rec_lost = bool(getattr(self.platform, "_last_stop_recorder_lost", False))
                if expected_seq_id is not None:
                    done_ts = self.clock_fn()
                    with self._lock:
                        if self._rec_seq_id == cur_seq:
                            self._auto_stop_seq = cur_seq
                            self._auto_stop_ts = done_ts
                            self._auto_stop_absorbed = False
                if not eff_silent and not self._stop_evt.is_set():
                    with self._hud_lock:
                        stop_hud_calls: list[
                            tuple[str, str, int, str | None, bool | None, bool | None, int]
                        ] = []
                        with self._lock:
                            if not self._stop_evt.is_set():
                                eff_stop_stage = (
                                    self._derive_bg_stage_locked(visible_only=True) or "recognizing"
                                )
                                self._update_main_hud_visibility_locked(eff_stop_stage)
                                stop_hud_calls.append(
                                    (eff_stop_stage, "", 0, None, None, None, 0)
                                )
                        self._emit_hud_calls_unlocked(stop_hud_calls)
                    if not self._stop_evt.is_set():
                        self.platform.beep("stop")
            except Exception as exc:
                return self._handle_capture_failure(
                    cur_seq,
                    exc,
                    eff_silent=eff_silent,
                    restore_main_if_needed=True,
                    clear_stopping=True,
                )

            task = _QueuedUtterance(
                seq_id=cur_seq,
                raw_wav=raw_wav,
                audio_source=src_used,
                origin_win=win_snap,
                no_paste=eff_no_paste,
                silent_hud=eff_silent,
                recorder_lost=stop_rec_lost,
                recovery_count=rec_cnt,
                recovery_gap_ms=rec_gap,
                audio_error=snap_audio_err,
                enqueued_ts=self.clock_fn(),
            )
            with self._queue_cv:
                self._stopping_capture = False
                if self._stop_evt.is_set():
                    stop_out_state = self._recompute_state_locked()
                    return {"ok": False, "ignored": "stopping", "state": stop_out_state, "seq_id": cur_seq}
                self._enqueue_utterance_locked(task)
                out_state = self._recompute_state_locked()
            return {"ok": True, "state": out_state, "seq_id": cur_seq}

    def handle_cancel(
        self,
        *,
        seq_id: int | None = None,
        silent_hud: bool = False,
        only_inflight: bool = False,
        yield_window: ForegroundWindow | None = None,
    ) -> dict[str, Any]:
        with self._trigger_lock:
            cancel_rec_mode = False
            cancelled_rec_seq = -1
            eff_rec_silent = bool(silent_hud)
            resume_state = "idle"
            candidates: list[_QueuedUtterance] = []
            with self._lock:
                if self._stop_evt.is_set():
                    return {"ok": False, "ignored": "stopping", "state": self.state, "seq_id": self.seq_id}
                if (
                    not self._recording_active
                    and yield_window is not None
                    and (yield_window.process or yield_window.bundle_id)
                    and (yield_reason := self._should_yield_hotkey(yield_window)) is not None
                ):
                    return {
                        "ok": False,
                        "ignored": yield_reason,
                        "cancelled_ids": [],
                        "committed_ids": [],
                        "state": self.state,
                        "seq_id": self.seq_id,
                    }
                if only_inflight and (
                    self._recording_active
                    or self._stopping_capture
                    or self._inflight_or_queued_count_locked() == 0
                ):
                    return {
                        "ok": False,
                        "ignored": "not_inflight",
                        "cancelled_ids": [],
                        "committed_ids": [],
                        "state": self.state,
                        "seq_id": self.seq_id,
                    }
                if self._recording_active and (seq_id is None or seq_id == self._recording_seq_id):
                    cancel_rec_mode = True
                    cancelled_rec_seq = self._recording_seq_id
                    eff_rec_silent = bool(silent_hud or self._cur_silent_hud)
                    self._clear_recording_state_locked()
                    self.seq_id += 1
                    self._rec_seq_id = self.seq_id
                    self._last_trigger_ts = self.clock_fn()
                    resume_state = self._recompute_state_locked()
                else:
                    candidates = [
                        t
                        for t, _ in self._iter_pipeline_tasks_locked()
                        if not t.cancel_token.is_cancelled() and (seq_id is None or t.seq_id == seq_id)
                    ]
                    if not candidates:
                        return {
                            "ok": seq_id is None,
                            "ignored": "not_found" if seq_id is not None else "idle",
                            "cancelled_ids": [],
                            "committed_ids": [],
                            "state": self.state,
                            "seq_id": self.seq_id,
                        }

            if cancel_rec_mode:
                self._cancel_max_duration_timer()
                self._cancel_remote_prefetch(seq_id=cancelled_rec_seq)
                self.platform.cancel_recording()
                if not eff_rec_silent:
                    resume_state = self._emit_recording_cancel_hud(show_cancelled_toast=True)
                    self.platform.beep("error")
                return {
                    "ok": True,
                    "cancelled_recording": cancelled_rec_seq,
                    "cancelled_ids": [cancelled_rec_seq],
                    "committed_ids": [],
                    "state": resume_state,
                    "seq_id": self.seq_id,
                }

            tok_to_task = {t.cancel_token: t for t in candidates}
            cancelled_toks, committed_toks = CancelToken.cancel_many(
                [t.cancel_token for t in candidates], "cancelled"
            )
            cancelled_ids = [tok_to_task[tok].seq_id for tok in cancelled_toks]
            committed_ids = [tok_to_task[tok].seq_id for tok in committed_toks]
            for cid in cancelled_ids:
                self._cancel_remote_prefetch(seq_id=cid)
            any_visible_cancelled = any(not tok_to_task[tok].silent_hud for tok in cancelled_toks)
            cancelled_set = set(cancelled_ids)

            with self._queue_cv:
                resume_state = self._recompute_state_locked()
                self._queue_cv.notify_all()
                if cancelled_set and not self._has_uncancelled_ahead_locked(cancelled_set):
                    self._queue_cv.wait_for(
                        lambda: self._stop_evt.is_set() or not self._any_seq_present_locked(cancelled_set),
                        timeout=0.25,
                    )
                    resume_state = self._recompute_state_locked()
                cur_seq_now = self.seq_id

            if not cancelled_ids:
                return {
                    "ok": False,
                    "ignored": "already_committed",
                    "cancelled_ids": [],
                    "committed_ids": committed_ids,
                    "state": resume_state,
                    "seq_id": cur_seq_now,
                }

            if any_visible_cancelled:
                with self._hud_lock:
                    cancel_calls: list[tuple[str, str, int, str | None, bool | None, bool | None, int]] = []
                    with self._lock:
                        cur_rec = self._recording_active and not self._stopping_capture
                        cur_stopping = self._stopping_capture
                        cur_vis_bg = self._derive_bg_stage_locked(visible_only=True)
                        cur_vis_cnt = self._visible_inflight_count_locked()
                        self._recompute_state_locked()
                        if not self._stop_evt.is_set():
                            if cur_rec:
                                if (
                                    not self._cur_silent_hud
                                    and self._main_hud_state == "recording"
                                    and cur_vis_cnt != self._main_hud_bg_count
                                ):
                                    cancel_calls.append(("recording", "", 0, None, None, False, cur_vis_cnt))
                                if not silent_hud:
                                    cancel_calls.append(("cancelled", "", 0, None, False, None, 0))
                            elif not silent_hud:
                                if cur_stopping:
                                    cancel_calls.append(("cancelled", "", 0, None, False, None, 0))
                                elif cur_vis_bg:
                                    if self._main_hud_state != cur_vis_bg:
                                        cancel_calls.append((cur_vis_bg, "", 0, None, None, None, 0))
                                    cancel_calls.append(("cancelled", "", 0, None, False, None, 0))
                                else:
                                    cancel_calls.append(("cancelled", "", 0, None, True, None, 0))
                            for st_n, _, _, lyr_n, clr_m_n, _, bg_n in cancel_calls:
                                self._update_main_hud_visibility_locked(
                                    st_n, layer=lyr_n, clear_main=clr_m_n, bg_count=bg_n
                                )
                    self._emit_hud_calls_unlocked(cancel_calls)

            return {
                "ok": True,
                "cancelled_ids": cancelled_ids,
                "committed_ids": committed_ids,
                "state": resume_state,
                "seq_id": cur_seq_now,
            }

    def _wait_clipboard_settle(self, cancel_token: CancelToken | None = None) -> bool:
        with self._lock:
            busy_until = self._clipboard_busy_until
        rem = busy_until - self.clock_fn()
        if rem > 0.0:
            _sleep_cooperative(
                rem,
                sleep_fn=self.sleep_fn,
                is_cancelled=lambda: self._stop_evt.is_set()
                or (cancel_token is not None and cancel_token.is_cancelled()),
                cancel_token=cancel_token,
            )
            return True
        return False

    def _note_clipboard_paste(self, paste_out: PasteOutcome) -> None:
        if paste_out.used_clipboard_paste:
            now_done = self.clock_fn()
            with self._lock:
                self._clipboard_busy_until = now_done + self._paste_settle_sec

    def _deliver_for_utterance(
        self,
        task: _QueuedUtterance,
        final_text: str,
        origin_win: ForegroundWindow,
        *,
        same_window_verifier: Callable[[ForegroundWindow], bool] | None = None,
    ) -> PasteOutcome:
        with self._deliver_lock:
            if self._stop_evt.is_set() or task.cancel_token.is_cancelled():
                return PasteOutcome(False, False, "cancelled", "")
            with self._lock:
                unpasted_snapshot = list(self._batch_unpasted_clips)
            fallback_txt = "\n\n".join([*unpasted_snapshot, final_text]) if unpasted_snapshot else None
            extra_deliv_kw: dict[str, Any] = {}
            if same_window_verifier is not None:
                extra_deliv_kw["same_window_verifier"] = same_window_verifier
            paste_out = self.platform.deliver_text(
                final_text,
                seq_id=task.seq_id,
                target_hwnd=origin_win.hwnd,
                origin_window=origin_win,
                fallback_clipboard_text=fallback_txt,
                suppress_beep=lambda: self._is_recording_or_stopping() or task.silent_hud,
                is_cancelled=lambda: self._stop_evt.is_set() or task.cancel_token.is_cancelled(),
                before_clipboard_write=lambda: self._wait_clipboard_settle(task.cancel_token),
                before_commit=task.cancel_token.try_commit,
                **extra_deliv_kw,
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

    def _deliver_for_selftest(
        self,
        text: str,
        *,
        seq_id: int,
        target_hwnd: int = 0,
        origin_window: ForegroundWindow | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        before_commit: Callable[[], bool] | None = None,
        same_window_verifier: Callable[[ForegroundWindow], bool] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> PasteOutcome:
        with self._deliver_lock:
            def _aborted() -> bool:
                return bool(
                    self._stop_evt.is_set()
                    or (cancel_token is not None and cancel_token.is_cancelled())
                    or (is_cancelled is not None and is_cancelled())
                )

            if _aborted():
                return PasteOutcome(False, False, "cancelled", "")

            def _guarded_before_commit() -> bool:
                if _aborted():
                    return False
                if before_commit is not None:
                    return bool(before_commit())
                return True

            paste_out = self.platform.deliver_text(
                text,
                seq_id=seq_id,
                target_hwnd=target_hwnd,
                origin_window=origin_window,
                suppress_beep=True,
                is_cancelled=_aborted,
                before_clipboard_write=lambda: self._wait_clipboard_settle(cancel_token),
                before_commit=_guarded_before_commit,
                same_window_verifier=same_window_verifier,
            )
            self._note_clipboard_paste(paste_out)
            return paste_out

    def _stash_task_wav_once(
        self,
        task: _QueuedUtterance,
        *,
        gate: AudioGateResult | None,
        snap: SessionSnapshot | None,
        win: ForegroundWindow,
        workspace_cwd: str = "",
        allow_during_shutdown: bool = False,
    ) -> str:
        with task.stash_lock:
            if task.stashed_done:
                return task.saved_audio
            if self._stop_evt.is_set() and not allow_during_shutdown:
                return ""
            task.stashed_done = True
            if (
                task.cancel_token.reason == "shutdown"
                or task.audio_source in ("file", "selftest_wav")
            ):
                return ""
            if task.audio_source == "retry":
                task.saved_audio = _existing_recoverable_retry_audio(task.saved_audio, self.runtime_dir)
                return task.saved_audio
            eff_gate = gate
            if eff_gate is None and len(task.raw_wav) > 44:
                try:
                    eff_gate = process_wav(task.raw_wav, self.cfg.audio)
                except Exception:
                    eff_gate = None
            if eff_gate is None or not eff_gate.passed or not eff_gate.wav_bytes:
                return ""
            saved = save_failed_audio(
                eff_gate.wav_bytes,
                self.runtime_dir,
                duration_sec=eff_gate.duration_sec,
                label=_derive_failed_label(snap, win, workspace_cwd),
            )
            task.saved_audio = str(saved) if saved is not None else ""
            return task.saved_audio

    def _utterance_worker_loop(self) -> None:
        while True:
            with self._queue_cv:
                while not self._stop_evt.is_set() and (
                    not self._utterance_queue
                    or (
                        self._stage2_active_count >= self.MAX_STAGE2_CONCURRENCY
                        and not self._utterance_queue[0].cancel_token.is_cancelled()
                    )
                ):
                    self._queue_cv.wait()
                if self._stop_evt.is_set():
                    self._active_task = None
                    self._recompute_state_locked()
                    self._queue_cv.notify_all()
                    return
                task = self._utterance_queue.popleft()
                self._active_task = task
                self._recompute_state_locked()
            if not task.silent_hud and not self._stop_evt.is_set() and not task.cancel_token.is_cancelled():
                self._maybe_emit_bg_main_hud("recognizing")
            self._run_stage1_for_utterance(task)

    def _push_completed_stage1_slot(self, slot: _CommitSlot) -> None:
        slot.done_evt.set()
        with self._queue_cv:
            if self._active_task is slot.task:
                self._active_task = None
            if self._stop_evt.is_set():
                self._recompute_state_locked()
                self._queue_cv.notify_all()
                return
            self._enqueue_commit_slot_locked(slot)

    def _run_stage1_for_utterance(self, task: _QueuedUtterance) -> None:
        q_wait_ms = max(0, int(round((self.clock_fn() - task.enqueued_ts) * 1000)))
        st = _Stage1State(t_total0=time.monotonic(), queue_wait_ms=q_wait_ms, win=task.origin_win)
        slot = _CommitSlot(task=task, st=st)
        cancel_fn = lambda: self._stop_evt.is_set() or task.cancel_token.is_cancelled()
        reason_fn = lambda: (
            "shutdown"
            if self._stop_evt.is_set()
            else ((task.cancel_token.reason or "cancelled") if task.cancel_token.is_cancelled() else "")
        )

        if self._stop_evt.is_set():
            with self._queue_cv:
                if self._active_task is task:
                    self._active_task = None
                self._recompute_state_locked()
                self._queue_cv.notify_all()
            return

        try:
            _execute_stage1(
                st,
                task.raw_wav,
                cfg=self.cfg,
                env=self.env,
                tracker=self.tracker,
                eff_asr=self.asr_provider,
                eff_refiner=self.refiner,
                platform=self.platform,
                origin_window=task.origin_win,
                window_title_override=None,
                audio_source=task.audio_source,
                explicit_mode="auto",
                seq_id=task.seq_id,
                max_chunk_sec=None,
                is_current_seq=None,
                is_cancelled=cancel_fn,
                cancel_token=task.cancel_token,
                cancel_check=cancel_fn,
                cancel_reason_fn=reason_fn,
                archive_sink=self._register_archive_thread,
            )
            if st.skipped_reason:
                self._push_completed_stage1_slot(slot)
                return
            assert st.asr_res is not None
            if not _will_run_stage2_refiner(self.cfg, self.refiner, st.asr_res):
                slot.stage2_ref_res = self.refiner.refine(
                    st.asr_res,
                    session_key=st.snap.session_key if st.snap else "",
                    tracker=self.tracker,
                    fallback_context=st.asr_ctx_dialog,
                    is_cancelled=cancel_fn,
                    cancel_token=task.cancel_token,
                    mode=st.eff_mode,
                    workspace_cwd=st.workspace_cwd,
                    target_hint=st.target_hint,
                )
                self._push_completed_stage1_slot(slot)
                return
        except Exception as exc:
            if rsn := reason_fn():
                st.skipped_reason = rsn
            else:
                slot.exc = exc
            self._push_completed_stage1_slot(slot)
            return

        # Hand off to bounded concurrent Stage 2 (outside Stage 1 try/except so slot is never double-enqueued)
        th_s2 = threading.Thread(
            target=self._run_stage2_slot,
            args=(slot,),
            name=f"avi-stage2-{task.seq_id}",
            daemon=True,
        )
        with self._queue_cv:
            while (
                not self._stop_evt.is_set()
                and not task.cancel_token.is_cancelled()
                and self._stage2_active_count >= self.MAX_STAGE2_CONCURRENCY
            ):
                self._queue_cv.wait(timeout=0.05)
            if self._active_task is task:
                self._active_task = None
            if self._stop_evt.is_set():
                self._recompute_state_locked()
                self._queue_cv.notify_all()
                return
            if task.cancel_token.is_cancelled():
                st.skipped_reason = reason_fn() or "cancelled"
                slot.done_evt.set()
                self._enqueue_commit_slot_locked(slot)
                return
            slot.stage = "refining"
            task.cancel_token.bind_event(slot.done_evt)
            self._stage2_active_count += 1
            self._stage2_slots.add(slot)
            self._stage2_threads = {t for t in self._stage2_threads if t.is_alive()}
            self._stage2_threads.add(th_s2)
            self._enqueue_commit_slot_locked(slot)

        if not task.silent_hud and not cancel_fn():
            self._maybe_emit_bg_main_hud("refining")

        try:
            th_s2.start()
        except Exception as start_exc:
            slot.exc = start_exc
            task.cancel_token.unbind_event(slot.done_evt)
            slot.done_evt.set()
            with self._queue_cv:
                self._stage2_active_count = max(0, self._stage2_active_count - 1)
                self._stage2_slots.discard(slot)
                self._stage2_threads.discard(th_s2)
                self._queue_cv.notify_all()
            return

    def _run_stage2_slot(self, slot: _CommitSlot) -> None:
        task = slot.task
        st = slot.st
        cancel_fn = lambda: self._stop_evt.is_set() or task.cancel_token.is_cancelled()
        try:
            assert st.asr_res is not None
            slot.stage2_ref_res = self.refiner.refine(
                st.asr_res,
                session_key=st.snap.session_key if st.snap else "",
                tracker=self.tracker,
                fallback_context=st.asr_ctx_dialog,
                is_cancelled=cancel_fn,
                cancel_token=task.cancel_token,
                mode=st.eff_mode,
                workspace_cwd=st.workspace_cwd,
                target_hint=st.target_hint,
            )
        except Exception as exc:
            if not cancel_fn():
                slot.exc = exc
        finally:
            task.cancel_token.unbind_event(slot.done_evt)
            slot.done_evt.set()
            with self._queue_cv:
                self._stage2_active_count = max(0, self._stage2_active_count - 1)
                self._stage2_slots.discard(slot)
                self._stage2_threads.discard(threading.current_thread())
                self._queue_cv.notify_all()

    def _finalize_commit_slot(self, slot: _CommitSlot) -> tuple[dict[str, Any], str, int, str] | None:
        task, st = slot.task, slot.st
        return finalize_commit_slot_outcome(
            task=task,
            st=st,
            slot_exc=slot.exc,
            stage2_ref_res=slot.stage2_ref_res,
            stop_evt=self._stop_evt,
            platform=self.platform,
            tracker=self.tracker,
            env=self.env,
            asr_provider=self.asr_provider,
            refiner=self.refiner,
            redact_fn=self._redact,
            summarize_fn=lambda res: self._build_summary(
                task.seq_id,
                res,
                dry_run=task.no_paste,
                recorder_lost=task.recorder_lost,
                recovery_count=task.recovery_count,
                recovery_gap_ms=task.recovery_gap_ms,
                audio_error=task.audio_error,
            ),
            stash_fn=lambda: self._stash_task_wav_once(
                task, gate=st.gate, snap=st.snap, win=st.win, workspace_cwd=st.workspace_cwd
            ),
            deliver_fn=lambda t, txt, win, ver: self._deliver_for_utterance(
                t, txt, win, same_window_verifier=ver
            ),
        )

    def _commit_worker_loop(self) -> None:
        while True:
            with self._queue_cv:
                while not self._stop_evt.is_set() and not self._commit_queue:
                    self._queue_cv.wait()
                if self._stop_evt.is_set():
                    self._recompute_state_locked()
                    self._queue_cv.notify_all()
                    return
                slot = self._commit_queue[0]

            while not self._stop_evt.is_set() and not slot.done_evt.wait(timeout=0.05):
                pass

            if self._stop_evt.is_set():
                with self._queue_cv:
                    self._recompute_state_locked()
                    self._queue_cv.notify_all()
                return

            outcome = self._finalize_commit_slot(slot)
            if outcome is None or self._stop_evt.is_set():
                with self._queue_cv:
                    if self._commit_queue and self._commit_queue[0] is slot:
                        self._commit_queue.popleft()
                    self._recompute_state_locked()
                    self._queue_cv.notify_all()
                if self._stop_evt.is_set():
                    return
                continue

            summary, hud_st, hud_dur_ms, beep_k = outcome
            summary.setdefault("ts", round(time.time(), 6))
            self._append_telemetry(summary)
            copied_hud_text = ""
            if hud_st == "copied":
                paste_reason = str(summary.get("paste_reason") or "")
                low_pr = paste_reason.strip().lower()
                if "accessibility_denied" in low_pr or "send_input_failed" in low_pr:
                    copied_hud_text = format_copied_hud_text(
                        paste_reason,
                        _resolve_effective_ui_language(
                            self.cfg, env=self.env, platform=self.platform
                        ),
                    )

            task = slot.task
            play_beep = ""
            with self._hud_lock:
                hud_calls: list[tuple[str, str, int, str | None, bool | None, bool | None, int]] = []
                with self._queue_cv:
                    if self._commit_queue and self._commit_queue[0] is slot:
                        self._commit_queue.popleft()
                    next_vis_stage = self._derive_bg_stage_locked(visible_only=True)
                    vis_inflight = self._visible_inflight_count_locked()
                    rec_active = self._recording_active or self._stopping_capture
                    self._recompute_state_locked()
                    self._record_result_locked(summary)
                    self._queue_cv.notify_all()
                    if not self._stop_evt.is_set():
                        hud_calls, play_beep = plan_commit_hud_calls(
                            silent_hud=task.silent_hud,
                            rec_active=rec_active,
                            recording_active=self._recording_active,
                            stopping_capture=self._stopping_capture,
                            cur_silent_hud=self._cur_silent_hud,
                            main_hud_state=self._main_hud_state,
                            main_hud_visible=self._main_hud_visible,
                            main_hud_bg_count=self._main_hud_bg_count,
                            next_vis_stage=next_vis_stage,
                            vis_inflight=vis_inflight,
                            hud_st=hud_st,
                            copied_hud_text=copied_hud_text,
                            hud_dur_ms=hud_dur_ms,
                            beep_k=beep_k,
                        )
                        for st_n, _, _, lyr_n, clr_m_n, _, bg_n in hud_calls:
                            self._update_main_hud_visibility_locked(
                                st_n, layer=lyr_n, clear_main=clr_m_n, bg_count=bg_n
                            )
                self._emit_hud_calls_unlocked(hud_calls)
            if play_beep:
                self.platform.beep(play_beep)

    def _maybe_prefetch_remote(self, win: ForegroundWindow, *, seq_id: int) -> None:
        if not self.cfg.remote.enabled or win.origin_drifted:
            return
        pref_fn = getattr(self.tracker, "prefetch_remote_session", None)
        if pref_fn is None:
            return
        try:
            should_sync, _ = _resolve_pipeline_window_routing(
                win,
                platform=self.platform,
                origin_window=win,
                window_title_override=None,
                audio_source="live",
                explicit_mode="auto",
            )
            if not should_sync:
                return
            inc_git = bool(self.cfg.refiner.include_git_status)

            def _do_prefetch() -> None:
                with self._lock:
                    if self._stop_evt.is_set() or (
                        self._recording_seq_id != seq_id
                        and not self._is_seq_inflight_locked(seq_id)
                        and self._rec_seq_id != seq_id
                    ):
                        return
                pref_fn(
                    win.title,
                    window_tty=win.tty,
                    window_cwd=win.cwd,
                    window_pid=win.pid,
                    window_process=win.process,
                    bundle_id=win.bundle_id,
                    document=win.document,
                    tmux_socket=win.tmux_socket,
                    tmux_pane=win.tmux_pane,
                    seq_id=seq_id,
                    include_git_info=inc_git,
                )

            rm = getattr(self.tracker, "remote_manager", None)
            if rm is not None and hasattr(rm, "spawn_async_task"):
                rm.spawn_async_task(_do_prefetch, seq_id=seq_id)
            else:
                _do_prefetch()
        except Exception:
            pass

    def _cancel_remote_prefetch(self, *, seq_id: int | None = None) -> None:
        cancel_fn = getattr(self.tracker, "cancel_remote_prefetch", None)
        if cancel_fn is not None:
            try:
                cancel_fn(seq_id=seq_id)
            except Exception:
                pass

    def _initiate_shutdown(self) -> list[threading.Thread]:
        self._cancel_max_duration_timer()
        self._cancel_remote_prefetch(seq_id=None)
        tokens_to_abort: list[CancelToken] = []
        inflight_candidates: list[tuple[_QueuedUtterance, AudioGateResult | None, SessionSnapshot | None, ForegroundWindow, str]] = []
        bg_threads: list[threading.Thread] = []
        seen_ids: set[int] = set()

        def _collect(task: _QueuedUtterance, slot: _CommitSlot | None = None) -> None:
            if task.seq_id in seen_ids:
                return
            seen_ids.add(task.seq_id)
            tokens_to_abort.append(task.cancel_token)
            inflight_candidates.append(
                (
                    task,
                    slot.st.gate if slot is not None else None,
                    slot.st.snap if slot is not None else None,
                    slot.st.win if slot is not None else task.origin_win,
                    slot.st.workspace_cwd if slot is not None else "",
                )
            )

        with self._queue_cv:
            self._stop_evt.set()
            for s in self._commit_queue:
                _collect(s.task, s)
            for s2 in self._stage2_slots:
                _collect(s2.task, s2)
            if self._active_task is not None:
                _collect(self._active_task)
            for t in self._utterance_queue:
                _collect(t)
            self._utterance_queue.clear()
            self._commit_queue.clear()
            self._batch_unpasted_clips.clear()
            self._recompute_state_locked()
            bg_threads.extend(self._control_threads)
            if self._worker_thread is not None:
                bg_threads.append(self._worker_thread)
            if self._commit_thread is not None:
                bg_threads.append(self._commit_thread)
            bg_threads.extend(th for th in self._stage2_threads if th.is_alive())
            tokens_to_abort.extend(self._control_cancel_tokens)
            self._queue_cv.notify_all()

        drained_ctrl_conns: list[socket.socket] = []
        with self._ctrl_trigger_cv:
            while self._ctrl_trigger_q:
                pending_conn, _ = self._ctrl_trigger_q.popleft()
                drained_ctrl_conns.append(pending_conn)
            if self._ctrl_trigger_worker is not None:
                bg_threads.append(self._ctrl_trigger_worker)
            self._ctrl_trigger_cv.notify_all()
        for pconn in drained_ctrl_conns:
            try:
                self._send_control_reply(pconn, {"ok": False, "error": "daemon stopping"})
            finally:
                try:
                    pconn.close()
                except OSError:
                    pass

        CancelToken.cancel_many(tokens_to_abort, "shutdown")
        for task_c, gate_c, snap_c, win_c, ws_cwd_c in inflight_candidates:
            if task_c.cancel_token.is_cancelled() and task_c.cancel_token.reason == "cancelled":
                self._stash_task_wav_once(
                    task_c,
                    gate=gate_c,
                    snap=snap_c,
                    win=win_c,
                    workspace_cwd=ws_cwd_c,
                    allow_during_shutdown=True,
                )
        return bg_threads

    def handle_control(self, req: Mapping[str, Any]) -> dict[str, Any]:
        cmd = str(req.get("cmd") or "status")
        if cmd == "status":
            is_ok_fn = getattr(self.platform, "is_shim_ok", None)
            shim_ok = (
                bool(is_ok_fn())
                if callable(is_ok_fn)
                else not bool(getattr(self.platform, "_shim_disabled", False))
            )
            raw_shim_err = str(getattr(self.platform, "last_shim_error", "") or "")
            shim_err = self._redact_str(raw_shim_err) if raw_shim_err else ""
            raw_hist = req.get("history")
            want_hist = raw_hist is not None and not isinstance(raw_hist, bool)
            with self._lock:
                st_cur = self.state
                seq_cur = self.seq_id
                pending_cur = self._inflight_or_queued_count_locked()
                last_res_snap = dict(self.last_result)
                remote_hosts = (
                    rm.breaker_snapshot()
                    if (
                        self.cfg.remote.enabled
                        and (rm := getattr(self.tracker, "remote_manager", None)) is not None
                    )
                    else None
                )
                mem_snap = [dict(r) for r in self.recent_results] if want_hist else []
            return build_daemon_status_response(
                runtime_dir=self.runtime_dir,
                req=req,
                build_id=self.build_id,
                state=st_cur,
                seq_id=seq_cur,
                pending_count=pending_cur,
                active_pipeline=self.active_pipeline,
                resolved_refiner_provider=self.resolved_refiner_provider,
                shim_ok=shim_ok,
                shim_error=shim_err,
                last_result=last_res_snap,
                remote_hosts=remote_hosts,
                mem_recent_results=mem_snap,
            )
        if cmd in ("toggle", "rec_start", "rec_stop"):
            win = _window_from_mapping(req) if _has_window_fields(req) else None
            return self.handle_trigger({"toggle": "toggle", "rec_start": "ptt_start", "rec_stop": "ptt_stop"}[cmd], win, no_paste=bool(req.get("no_paste")), silent_hud=bool(req.get("silent_hud")), loopback=bool(req.get("loopback")))
        if cmd == "cancel":
            try:
                seq_val = _parse_optional_seq_id(req.get("seq_id"))
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            return self.handle_cancel(
                seq_id=seq_val,
                silent_hud=bool(req.get("silent_hud")),
                only_inflight=bool(req.get("only_inflight")),
            )
        if cmd == "selftest_wav":
            if self._stop_evt.is_set():
                return {"ok": False, "error": "daemon stopping"}
            if not self._selftest_lock.acquire(blocking=False):
                return {"ok": False, "error": "selftest_wav busy"}
            try:
                return self._execute_selftest_wav(req)
            finally:
                self._selftest_lock.release()
        if cmd == "probe_route":
            return self._execute_probe_route(req)
        if cmd == "shutdown":
            self._initiate_shutdown()
            return {"ok": True, "stopping": True}
        return {"ok": False, "error": f"unknown cmd: {cmd}"}

    def _make_probe_tracker(self) -> tuple[Any, bool]:
        return make_isolated_probe_tracker(self.tracker)

    def _safe_close_refiner(self) -> None:
        try:
            self.refiner.close()
        except Exception:
            pass

    def _safe_close_tracker(self) -> None:
        if (tr_close := getattr(self.tracker, "close", None)) is not None:
            try:
                tr_close()
            except Exception:
                pass

    def _acquire_shared_resources(self, *, use_refiner: bool, use_tracker: bool) -> bool:
        with self._lock:
            if (
                self._stop_evt.is_set()
                or (use_refiner and self._refiner_closed)
                or (use_tracker and self._tracker_closed)
            ):
                return False
            if use_refiner:
                self._refiner_users += 1
            if use_tracker:
                self._tracker_users += 1
            return True

    def _release_shared_resources(self, *, use_refiner: bool, use_tracker: bool) -> None:
        do_close_refiner = False
        do_close_tracker = False
        with self._lock:
            if use_refiner:
                self._refiner_users = max(0, self._refiner_users - 1)
                if (
                    self._deferred_close_refiner
                    and self._refiner_users == 0
                    and not self._refiner_closed
                ):
                    self._refiner_closed = True
                    self._deferred_close_refiner = False
                    do_close_refiner = True
            if use_tracker:
                self._tracker_users = max(0, self._tracker_users - 1)
                if (
                    self._deferred_close_tracker
                    and self._tracker_users == 0
                    and not self._tracker_closed
                ):
                    self._tracker_closed = True
                    self._deferred_close_tracker = False
                    do_close_tracker = True
        if do_close_refiner:
            self._safe_close_refiner()
        if do_close_tracker:
            self._safe_close_tracker()

    def _execute_selftest_wav(
        self,
        req: Mapping[str, Any],
        *,
        cancel_token: CancelToken | None = None,
        deadline: float | None = None,
        peer_disconnected: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        if self._stop_evt.is_set() or (cancel_token is not None and cancel_token.is_cancelled()):
            return {"ok": False, "error": "daemon stopping"}
        wav_path, origin_win, deliver, eff_deadline, err_msg = validate_selftest_wav_request(
            req, platform=self.platform, deadline=deadline
        )
        if err_msg or wav_path is None or origin_win is None:
            return {"ok": False, "error": err_msg or "invalid selftest_wav request"}

        known_cwds_snap = tuple(self.tracker.active_session_cwds())
        selftest_tracker, owns_tracker = self._make_probe_tracker()
        acquired_shared = False

        def _alloc_seq(no_telemetry: bool) -> int:
            if no_telemetry:
                return 0
            with self._lock:
                self.seq_id += 1
                return self.seq_id

        def _record_telemetry(summary: dict[str, Any]) -> None:
            with self._lock:
                st_snap = self._record_result_locked(summary)
            self._append_telemetry(st_snap)

        try:
            if not self._acquire_shared_resources(
                use_refiner=True, use_tracker=(not owns_tracker)
            ):
                return {"ok": False, "error": "daemon stopping"}
            acquired_shared = True
            return run_selftest_wav_request(
                req,
                wav_path=wav_path,
                origin_win=origin_win,
                deliver=deliver,
                eff_deadline=eff_deadline,
                stop_evt=self._stop_evt,
                cancel_token=cancel_token,
                peer_disconnected=peer_disconnected,
                cfg=self.cfg,
                creds=self.creds,
                selftest_tracker=selftest_tracker,
                asr_provider=self.asr_provider,
                refiner=self.refiner,
                platform=self.platform,
                runtime_dir=self.runtime_dir,
                env=self.env,
                known_cwds_snap=known_cwds_snap,
                alloc_seq_fn=_alloc_seq,
                deliver_for_selftest_fn=self._deliver_for_selftest,
                build_summary_fn=lambda seq, res, dry: self._build_summary(
                    seq, res, dry_run=dry, audio_error=""
                ),
                record_telemetry_fn=_record_telemetry,
            )
        finally:
            if acquired_shared:
                self._release_shared_resources(use_refiner=True, use_tracker=(not owns_tracker))
            if owns_tracker:
                try:
                    selftest_tracker.close()
                except Exception:
                    pass

    def _execute_probe_route(
        self,
        req: Mapping[str, Any],
        *,
        cancel_token: CancelToken | None = None,
    ) -> dict[str, Any]:
        if self._stop_evt.is_set() or (cancel_token is not None and cancel_token.is_cancelled()):
            return {"ok": False, "error": "daemon stopping"}
        known_cwds_snap = tuple(self.tracker.active_session_cwds())
        probe_tracker, owns_tracker = self._make_probe_tracker()
        acquired_tracker = False
        try:
            if not owns_tracker:
                if not self._acquire_shared_resources(use_refiner=False, use_tracker=True):
                    return {"ok": False, "error": "daemon stopping"}
                acquired_tracker = True
            cancel_fn = (
                (lambda: self._stop_evt.is_set() or cancel_token.is_cancelled())
                if cancel_token is not None
                else self._stop_evt.is_set
            )
            return run_probe_route_request(
                req,
                cfg=self.cfg,
                env=self.env,
                probe_tracker=probe_tracker,
                platform=self.platform,
                known_cwds_snap=known_cwds_snap,
                cancel_fn=cancel_fn,
            )
        finally:
            if acquired_tracker:
                self._release_shared_resources(use_refiner=False, use_tracker=True)
            if owns_tracker:
                try:
                    probe_tracker.close()
                except Exception:
                    pass

    def _send_control_reply(self, conn: socket.socket, resp: Mapping[str, Any]) -> None:
        send_control_reply(conn, resp)

    def _dispatch_control_async(
        self,
        conn: socket.socket,
        req: Mapping[str, Any],
        *,
        cmd: str,
    ) -> bool:
        if self._stop_evt.is_set():
            self._send_control_reply(conn, {"ok": False, "error": "daemon stopping"})
            return False
        is_selftest = cmd == "selftest_wav"
        req_deadline: float | None = None
        if is_selftest:
            try:
                parsed_timeout = _parse_optional_timeout_sec(req.get("timeout_sec"))
            except ValueError as exc:
                self._send_control_reply(conn, {"ok": False, "error": str(exc)})
                return False
            if parsed_timeout is not None:
                req_deadline = time.monotonic() + parsed_timeout
        gate = self._selftest_lock if is_selftest else self._probe_sem
        if not gate.acquire(blocking=False):
            self._send_control_reply(conn, {"ok": False, "error": f"{cmd} busy"})
            return False
        gate_held = True

        def _release_gate() -> None:
            nonlocal gate_held
            if gate_held:
                gate_held = False
                gate.release()

        cancel_tok = CancelToken()
        th: threading.Thread | None = None
        try:
            conn_closed_evt = threading.Event()

            def _peer_disconnected() -> bool:
                return conn_closed_evt.is_set() or _is_control_peer_disconnected(conn)

            def _worker() -> None:
                released = False
                try:
                    try:
                        resp = (
                            self._execute_selftest_wav(
                                req,
                                cancel_token=cancel_tok,
                                deadline=req_deadline,
                                peer_disconnected=_peer_disconnected,
                            )
                            if is_selftest
                            else self._execute_probe_route(req, cancel_token=cancel_tok)
                        )
                    except Exception as exc:
                        resp = {"ok": False, "error": self._redact(exc)}
                    with self._lock:
                        self._control_cancel_tokens.discard(cancel_tok)
                    gate.release()
                    released = True
                    self._send_control_reply(conn, resp)
                finally:
                    conn_closed_evt.set()
                    try:
                        conn.close()
                    except OSError:
                        pass
                    with self._lock:
                        self._control_threads.discard(threading.current_thread())
                        self._control_cancel_tokens.discard(cancel_tok)
                    if not released:
                        gate.release()

            th = threading.Thread(
                target=_worker,
                name="avi-selftest-wav" if is_selftest else "avi-probe-route",
                daemon=True,
            )
            stopping = False
            with self._lock:
                if self._stop_evt.is_set():
                    stopping = True
                else:
                    self._control_threads = {t for t in self._control_threads if t.is_alive()}
                    self._control_threads.add(th)
                    self._control_cancel_tokens.add(cancel_tok)
            if stopping:
                _release_gate()
                self._send_control_reply(conn, {"ok": False, "error": "daemon stopping"})
                return False
            th.start()
            gate_held = False
            return True
        except Exception as exc:
            with self._lock:
                if th is not None:
                    self._control_threads.discard(th)
                self._control_cancel_tokens.discard(cancel_tok)
            _release_gate()
            self._send_control_reply(conn, {"ok": False, "error": self._redact(exc)})
            return False
        finally:
            _release_gate()

    def _handle_power_resume(self, m: Mapping[str, Any] | None = None) -> dict[str, Any]:
        _ = m
        shared_evicted = evict_all_keepalive_connections()
        refiner_evicted = 0
        if callable(evict_fn := getattr(self.refiner, "evict_connections", None)):
            try:
                refiner_evicted = int(evict_fn() or 0)
            except Exception:
                pass
        # Follow strict global lock hierarchy: _trigger_lock (T) -> _lock (L)
        with self._trigger_lock:
            with self._lock:
                self._last_trigger_ts = -1e9
                self._clipboard_busy_until = -1e9
                if not self._recording_active:
                    self._origin_win = ForegroundWindow()
                    self._recorder_lost_seq = -1
                    self._recorder_lost_ts = -1e9
                    self._recorder_lost_stop_absorbed = False
                    self._auto_stop_seq = -1
                    self._auto_stop_ts = -1e9
                    self._auto_stop_absorbed = False
        self._log_info(
            f"power_resume handled (evicted_shared={shared_evicted}, evicted_refiner={refiner_evicted})"
        )
        return {
            "ok": True,
            "evicted_shared": shared_evicted,
            "evicted_refiner": refiner_evicted,
        }

    def _handle_shim_disconnected(self, m: Mapping[str, Any]) -> dict[str, Any]:
        raw_sid = m.get("_shim_instance_id")
        shim_id = (
            int(raw_sid)
            if isinstance(raw_sid, int) and not isinstance(raw_sid, bool) and raw_sid > 0
            else 0
        )
        target_seq: int | None = None
        with self._lock:
            if (
                not self._stop_evt.is_set()
                and self._recording_active
                and self._recording_trigger_action == "ptt_start"
                and self._recording_shim_id > 0
                and shim_id == self._recording_shim_id
            ):
                target_seq = self._recording_seq_id
        if target_seq is None:
            return {"ok": True, "ignored": "not_matching_ptt"}
        self._log_info(
            f"shim disconnected during active PTT (seq={target_seq}, shim_id={shim_id}); auto-stopping PTT recording"
        )
        return self.handle_trigger("ptt_stop", expected_seq_id=target_seq)

    def _on_platform_event(self, m: Mapping[str, Any]) -> None:
        ev = str(m.get("event") or "")
        if ev in ("toggle", "ptt_start", "ptt_stop", "ptt_cancel"):
            raw_sid = m.get("_shim_instance_id")
            shim_id = (
                int(raw_sid)
                if isinstance(raw_sid, int) and not isinstance(raw_sid, bool) and raw_sid > 0
                else 0
            )
            self.handle_trigger(
                ev,
                _window_from_mapping(m),
                from_hotkey=True,
                shim_instance_id=shim_id,
            )
        elif ev == "shim_disconnected":
            self._handle_shim_disconnected(m)
        elif ev == "hold_cancel":
            self.handle_cancel(yield_window=_window_from_mapping(m))
        elif ev == "cancel":
            try:
                seq_val = _parse_optional_seq_id(m.get("seq_id"))
            except ValueError:
                return
            self.handle_cancel(seq_id=seq_val)
        elif ev == "power_resume":
            self._handle_power_resume(m)

    def _ctrl_trigger_worker_loop(self) -> None:
        while True:
            item: tuple[socket.socket, dict[str, Any]] | None = None
            drained: list[socket.socket] = []
            with self._ctrl_trigger_cv:
                while not self._ctrl_trigger_q and not self._stop_evt.is_set():
                    self._ctrl_trigger_cv.wait()
                if self._stop_evt.is_set():
                    while self._ctrl_trigger_q:
                        pconn, _ = self._ctrl_trigger_q.popleft()
                        drained.append(pconn)
                    break
                item = self._ctrl_trigger_q.popleft()
            if item is None:
                continue
            conn, req = item
            try:
                try:
                    resp = self.handle_control(req)
                except Exception as exc:
                    resp = {"ok": False, "error": self._redact(exc)}
                self._send_control_reply(conn, resp)
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
        for pconn in drained:
            try:
                self._send_control_reply(pconn, {"ok": False, "error": "daemon stopping"})
            finally:
                try:
                    pconn.close()
                except OSError:
                    pass

    def _dispatch_control_trigger(self, conn: socket.socket, req: dict[str, Any]) -> bool:
        with self._ctrl_trigger_cv:
            if self._stop_evt.is_set():
                err_msg = "daemon stopping"
            elif len(self._ctrl_trigger_q) >= self._CTRL_TRIGGER_QUEUE_MAXLEN:
                err_msg = "control queue full"
            else:
                self._ctrl_trigger_q.append((conn, req))
                if self._ctrl_trigger_worker is None or not self._ctrl_trigger_worker.is_alive():
                    w = threading.Thread(
                        target=self._ctrl_trigger_worker_loop,
                        name="avi-ctrl-trigger",
                        daemon=True,
                    )
                    try:
                        w.start()
                    except Exception as exc:
                        self._ctrl_trigger_q.pop()
                        err_msg = self._redact(exc)
                    else:
                        self._ctrl_trigger_worker = w
                        self._ctrl_trigger_cv.notify()
                        return True
                else:
                    self._ctrl_trigger_cv.notify()
                    return True
        self._send_control_reply(conn, {"ok": False, "error": err_msg})
        return False

    def _handle_control_client(self, conn: socket.socket) -> None:
        handed_off = False
        try:
            raw = _recv_control_request_line(
                conn,
                max_bytes=CONTROL_REQUEST_MAX_BYTES,
                timeout_sec=CONTROL_REQUEST_READ_TIMEOUT_SEC,
            )
            parsed = json.loads(raw) if raw else {}
            req = parsed if isinstance(parsed, dict) else {}
            cmd = str(req.get("cmd") or "status")
            if cmd in ("selftest_wav", "probe_route"):
                handed_off = self._dispatch_control_async(conn, req, cmd=cmd)
            elif cmd in ("toggle", "rec_start", "rec_stop", "cancel"):
                handed_off = self._dispatch_control_trigger(conn, req)
            else:
                resp = self.handle_control(req)
                self._send_control_reply(conn, resp)
        except Exception as exc:
            self._send_control_reply(conn, {"ok": False, "error": self._redact(exc)})
        finally:
            if not handed_off:
                try:
                    conn.close()
                except OSError:
                    pass

    def serve_forever(self) -> None:
        sig_stop = threading.Event()
        is_main = threading.current_thread() is threading.main_thread()
        prev_int = signal.signal(signal.SIGINT, lambda *_: sig_stop.set()) if is_main else None
        prev_term = signal.signal(signal.SIGTERM, lambda *_: sig_stop.set()) if is_main else None
        try:
            self.acquire_lock()
            self.sock_path.unlink(missing_ok=True)
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._srv_sock = srv
            write_daemon_pid_file(self.pid_path)
            self.platform.start_event_listener(self._on_platform_event)
            srv.bind(str(self.sock_path)); os.chmod(self.sock_path, 0o600); srv.listen(8); srv.settimeout(0.3)
            if (
                self.cfg.refiner.provider.strip().lower() == "auto"
                and self.resolved_refiner_provider == "off"
                and not _is_single_hop_asr_provider(self.asr_provider)
            ):
                print(
                    "[agent-voice-input] INFO: refiner=auto found no Stage 2 credentials; "
                    "delivering Stage 1 ASR transcripts directly.",
                    file=sys.stderr,
                    flush=True,
                )
            while not self._stop_evt.is_set() and not sig_stop.is_set():
                try: conn, _ = srv.accept()
                except socket.timeout:
                    self._maybe_rotate_daemon_log()
                    self._maybe_prune_failed_audio()
                    continue
                except OSError: break
                self._handle_control_client(conn)
        finally:
            if is_main and prev_int is not None: signal.signal(signal.SIGINT, prev_int)
            if is_main and prev_term is not None: signal.signal(signal.SIGTERM, prev_term)
            self.close()

    def _register_archive_thread(self, th: threading.Thread) -> None:
        with self._lock:
            self._archive_threads = {t for t in self._archive_threads if t.is_alive()}
            self._archive_threads.add(th)

    def wait_pending_archives(self, timeout_sec: float = 2.0) -> None:
        with self._lock:
            pending = [t for t in self._archive_threads if t.is_alive()]
        if pending:
            wait_recording_archives(pending, timeout_sec=timeout_sec)
        with self._lock:
            self._archive_threads = {t for t in self._archive_threads if t.is_alive()}

    def close(self) -> None:
        try:
            if self._srv_sock is not None:
                try: self._srv_sock.close()
                except OSError: pass
                self._srv_sock = None
            if self._owns_runtime_files:
                self.sock_path.unlink(missing_ok=True)
                self.pid_path.unlink(missing_ok=True)
            bg_threads = self._initiate_shutdown()
            join_deadline = time.monotonic() + _CLOSE_JOIN_TIMEOUT_SEC
            cur_th = threading.current_thread()
            for th in bg_threads:
                if (rem := join_deadline - time.monotonic()) <= 0:
                    break
                if th.is_alive() and th is not cur_th:
                    th.join(timeout=rem)
            do_close_refiner = False
            do_close_tracker = False
            with self._lock:
                if not self._refiner_closed:
                    if self._refiner_users == 0:
                        self._refiner_closed = True
                        self._deferred_close_refiner = False
                        do_close_refiner = True
                    else:
                        self._deferred_close_refiner = True
                if not self._tracker_closed:
                    if self._tracker_users == 0:
                        self._tracker_closed = True
                        self._deferred_close_tracker = False
                        do_close_tracker = True
                    else:
                        self._deferred_close_tracker = True
            if do_close_refiner:
                self._safe_close_refiner()
            if do_close_tracker:
                self._safe_close_tracker()
            # Deliberate exception: if an active control worker still holds shared refiner/tracker
            # past the bounded join deadline, defer final close() to that worker's finally block
            # so shutdown stays bounded while preventing both mid-use teardown and resource leaks.
            self.wait_pending_archives(timeout_sec=2.0)
            self.platform.close()
        finally:
            # Never unlink daemon.lock: unlinking causes a classic inode race allowing two concurrent daemons.
            lock_fd, self._lock_fd = self._lock_fd, None
            self._owns_runtime_files = False
            release_daemon_lock(lock_fd)

    stop = close
