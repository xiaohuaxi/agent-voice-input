"""Single-shot voice pipeline, Stage 1/2 context & ASR helpers, routing probes, and selftest runners."""
from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field, replace
import hashlib
import math
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, Mapping, Sequence

from ._remote_probe import _is_broad_or_unsafe_workspace_dir
from .app_scope import (
    classify_window_category,
    is_antigravity_ide_window,
    is_claude_desktop_window,
    is_codex_desktop_window,
    is_remote_ide_window,
    normalize_app_token,
)
from .asr_providers import (
    AsrError,
    AsrResult,
    BaseAsrProvider,
    CancelToken,
    ChatAudioAsrProvider,
    GeminiAudioProvider,
    create_asr_provider,
)
from .audio_proc import (
    AudioGateResult,
    DEFAULT_MAX_CHUNK_SEC,
    FAILED_AUDIO_MAX_AGE_SEC,
    SINGLE_HOP_MAX_CHUNK_SEC,
    get_failed_audio_dir,
    inspect_wav_chunks,
    process_wav,
    save_failed_audio,
    spawn_recording_archive,
    wait_recording_archives,
)
from .config import (
    AudioConfig,
    Config,
    ProviderCredentials,
    redact_secrets,
    resolve_credentials,
    resolve_saved_recordings_dir,
)
from .control_ipc import (
    _has_window_fields,
    _parse_optional_timeout_sec,
    _resolve_control_origin_window,
)
from .hotwords import (
    format_stage2_vocabulary_hints,
    load_hotwords_table,
    order_stage1_entries,
    order_stage2_entries,
    resolve_personal_hotwords_path,
)
from .hud import is_toast_hud_state, resolve_ui_language
from .platform_common import (
    ForegroundWindow,
    MicrophoneUnavailableError,
    NoMicrophoneError,
    PasteOutcome,
)
from .platform_macos import is_macos_terminal_window
from .platform_wsl import is_windows_terminal_process
from .refiner import (
    BaseRefiner,
    RefinerResult,
    attach_single_hop_context,
    build_refiner_context_chunks,
    create_refiner,
)
from .remote_ssh import RemoteSshManager
from .session_ctx import SessionSnapshot, SessionTracker, parse_window_title_hint
from .workspace_ctx import (
    _AGY_DESKTOP_REJECT_TITLES,
    _dedupe_hotwords_ci,
    _effective_is_wsl,
    _parse_claude_desktop_page_url,
    _resolve_safe_local_dir,
    resolve_antigravity_ide_session,
    resolve_antigravity_ide_session_by_title,
    resolve_claude_desktop_session_by_title,
    resolve_codex_desktop_session_by_title,
    resolve_ide_workspace_cwd,
)

MAX_ASR_CHUNK_CONCURRENCY: int = 5
MAX_PENDING_UTTERANCES: int = 8
MAX_STAGE2_CONCURRENCY: int = 2
DEFAULT_PASTE_SETTLE_SEC: float = 0.15

_SAFE_SESSION_ID_CHARS: frozenset[str] = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)
_KNOWN_MATCHED_BY: frozenset[str] = frozenset(
    {
        "registry",
        "title_uuid",
        "title_prefix",
        "tty_registry",
        "tty_ps",
        "proc_scan",
        "recent_root",
        "tmux",
        "tmux_pane",
        "tmux_registry",
        "tmux_pane_title",
        "tmux_proc_scan",
        "ide_brain",
        "ide_db_title",
        "codex_desktop_db",
        "claude_desktop_code",
        "claude_desktop_cowork",
        "explicit",
        "remote",
        "remote_ssh",
        "remote_cache",
        "remote_whitelist",
        "remote_proc",
        "remote_registry",
        "remote_title_token",
        "remote_cwd",
        "remote_cwd_only",
        "remote_tmux_title",
        "remote_tmux_proc",
    }
)
_HARD_CANCEL_REASONS: tuple[str, ...] = (
    "cancelled",
    "shutdown",
    "client_timeout",
    "client_disconnected",
)


def _resolve_effective_ui_language(
    cfg: Config,
    *,
    env: Mapping[str, str] | None = None,
    platform: Any = None,
) -> str:
    host_culture_fn = getattr(platform, "_host_ui_culture", None)
    win_culture = host_culture_fn() if callable(host_culture_fn) else None
    return resolve_ui_language(cfg.ui.language, env=env, win_ui_culture=win_culture)


def _elapsed_ms(t0: float, clock_fn: Callable[[], float] = time.monotonic) -> int:
    return max(0, int(round((clock_fn() - t0) * 1000)))


@dataclass(frozen=True)
class PipelineResult:
    ok: bool
    seq_id: int
    final_text: str = ""
    skipped_reason: str = ""
    audio_source: str = ""
    switched_fallback: bool = False
    gate: AudioGateResult | None = None
    session: SessionSnapshot | None = None
    asr: AsrResult | None = None
    refined: RefinerResult | None = None
    paste: PasteOutcome | None = None
    error: str = ""
    asr_chunks: int = 1
    asr_chunk_secs: tuple[float, ...] = ()
    asr_missed_chunks: int = 0
    saved_audio: str = ""
    gate_ms: int = 0
    sync_ms: int = 0
    refine_init_ms: int = 0
    deliver_ms: int = 0
    total_ms: int = 0
    queue_wait_ms: int = 0
    ctx_chars: int = 0
    ctx_chunks: int = 0
    mode: str = "agent"
    ctx_fallback: str = ""
    workspace_cwd: str = ""


def _derive_delivery_status(res: PipelineResult, *, dry_run: bool) -> str:
    if res.ok:
        if res.paste is None:
            return "dry_run" if dry_run else "pasted"
        if res.paste.pasted:
            return "pasted"
        if res.paste.clipboard:
            return "copied"
        return "undelivered_recoverable" if res.saved_audio else "undelivered_unrecoverable"
    return "undelivered_recoverable" if res.saved_audio else "undelivered_unrecoverable"


def build_pipeline_summary(
    seq_id: int,
    res: PipelineResult,
    *,
    dry_run: bool = False,
    recorder_lost: bool = False,
    recovery_count: int = 0,
    recovery_gap_ms: int = 0,
    audio_error: str = "",
    queue_wait_ms: int | None = None,
    redact_fn: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Build a strictly text-free, secret-redacted numeric summary for status & telemetry.jsonl."""
    clean_str = redact_fn if redact_fn is not None else (lambda s: redact_secrets(s))
    in_tok = res.refined.input_tokens if res.refined else None
    cached_tok = res.refined.cached_input_tokens if res.refined else None
    out_tok = res.refined.output_tokens if res.refined else None
    rsn_tok = res.refined.reasoning_tokens if res.refined else None
    eff_q_wait = max(0, int(res.queue_wait_ms if queue_wait_ms is None else queue_wait_ms))
    summary: dict[str, Any] = {
        "ts": round(time.time(), 6),
        "seq_id": seq_id,
        "ok": res.ok,
        "skipped_reason": res.skipped_reason,
        "audio_source": res.audio_source,
        "mode": res.mode,
        "switched_fallback": res.switched_fallback,
        "session_key": res.session.session_key if res.session else "",
        "asr_provider": res.asr.provider if res.asr else "",
        "asr_model": res.asr.model if res.asr else "",
        "refiner_provider": res.refined.provider if res.refined else "",
        "refiner_model": res.refined.model if res.refined else "",
        "refiner_fallback": res.refined.used_fallback if res.refined else "",
        "pasted": res.paste.pasted if res.paste else False,
        "clipboard": res.paste.clipboard if res.paste else False,
        "paste_reason": res.paste.reason if res.paste else ("dry_run" if dry_run and res.ok else ""),
        "delivery_status": _derive_delivery_status(res, dry_run=dry_run),
        "raw_audio_sec": round(res.gate.duration_sec, 3) if res.gate else 0.0,
        "voiced_audio_sec": round(res.gate.voiced_sec, 3) if res.gate else 0.0,
        "queue_wait_ms": eff_q_wait,
        "gate_ms": res.gate_ms,
        "sync_ms": res.sync_ms,
        "asr_ms": res.asr.latency_ms if res.asr else 0,
        "refine_init_ms": res.refine_init_ms,
        "refine_ms": res.refined.latency_ms if res.refined else 0,
        "deliver_ms": res.deliver_ms,
        "total_ms": res.total_ms,
        "asr_chars": len(res.asr.text.strip()) if res.asr else 0,
        "final_chars": len(res.final_text),
        "ctx_chars": res.ctx_chars,
        "ctx_chunks": res.ctx_chunks,
        "ctx_fallback": res.ctx_fallback,
        "input_tokens": in_tok,
        "cached_input_tokens": cached_tok,
        "output_tokens": out_tok,
        "reasoning_tokens": rsn_tok,
    }
    if in_tok is not None and in_tok > 0 and cached_tok is not None:
        summary["cache_hit_pct"] = round(100.0 * cached_tok / in_tok, 1)
    if res.asr and res.asr.is_single_hop_refined:
        summary["is_single_hop_refined"] = True
    if res.refined and res.refined.audio_tokens is not None:
        summary["audio_tokens"] = res.refined.audio_tokens
    if res.refined and (res.refined.thought_chunks > 0 or res.refined.thought_chars > 0):
        summary["thought_chunks"] = res.refined.thought_chunks
        summary["thought_chars"] = res.refined.thought_chars
    if (
        res.asr
        and (res.asr.input_tokens is not None or res.asr.audio_tokens is not None)
        and (res.refined is None or res.refined.used_fallback != "single_hop_passthrough")
    ):
        if res.asr.input_tokens is not None:
            summary["asr_input_tokens"] = res.asr.input_tokens
            summary["asr_cached_input_tokens"] = res.asr.cached_input_tokens
            summary["asr_output_tokens"] = res.asr.output_tokens
            summary["asr_reasoning_tokens"] = res.asr.reasoning_tokens
        if res.asr.audio_tokens is not None:
            summary["asr_audio_tokens"] = res.asr.audio_tokens
    if res.asr and res.asr.retries > 0:
        summary["asr_retries"] = res.asr.retries
    if res.asr and res.asr.duration_ms is not None:
        summary["asr_duration_ms"] = res.asr.duration_ms
    if res.asr and res.asr.request_id:
        summary["asr_request_id"] = clean_str(res.asr.request_id)
    if res.refined and res.refined.retries > 0:
        summary["refiner_retries"] = res.refined.retries
    if res.refined and res.refined.warning:
        summary["refiner_warning"] = clean_str(res.refined.warning)
    if res.error:
        summary["error"] = clean_str(res.error)
    if res.asr_chunks > 1:
        summary["asr_chunks"] = res.asr_chunks
        if res.asr_chunk_secs:
            summary["asr_chunk_secs"] = list(res.asr_chunk_secs)
    if res.asr_missed_chunks > 0:
        summary["asr_missed_chunks"] = res.asr_missed_chunks
    if res.gate and res.gate.trimmed_sec > 0.0:
        summary["silence_trimmed_sec"] = round(res.gate.trimmed_sec, 3)
        summary["silence_trim_cuts"] = res.gate.trim_cuts
    if res.saved_audio:
        summary["saved_audio"] = res.saved_audio
    if dry_run:
        summary["dry_run"] = True
    if recorder_lost:
        summary["capture_interrupted"] = True
    if recovery_count > 0:
        summary["recovery_count"] = recovery_count
        summary["recovery_gap_ms"] = recovery_gap_ms
    if (not res.ok or recorder_lost) and audio_error:
        summary["audio_error"] = clean_str(audio_error)
    return summary


def _is_safe_session_token(val: str) -> bool:
    return bool(val) and all(ch in _SAFE_SESSION_ID_CHARS for ch in val)


def _mask_session_token(raw_id: str) -> str:
    if _is_safe_session_token(raw_id):
        return raw_id[:8]
    return hashlib.sha256(raw_id.encode("utf-8", errors="replace")).hexdigest()[:8]


def _mask_session_key(snap: SessionSnapshot | None) -> str:
    if snap is None:
        return ""
    m = getattr(snap, "match", None)
    cli = str(getattr(m, "cli", "") or "").strip()
    sid = str(getattr(m, "session_id", "") or "").strip()
    if cli and sid:
        safe_cli = cli if _is_safe_session_token(cli) else "other"
        return f"{safe_cli}:{_mask_session_token(sid)}"
    raw = str(getattr(snap, "session_key", "") or "").strip()
    if not raw:
        return ""
    prefix, sep, tail = raw.rpartition(":")
    if sep:
        safe_cli = prefix if _is_safe_session_token(prefix) else "other"
        return f"{safe_cli}:{_mask_session_token(tail)}"
    return _mask_session_token(raw)


def _sanitize_matched_by(snap: SessionSnapshot | None) -> str:
    if snap is None:
        return ""
    m = getattr(snap, "match", None)
    raw = str(getattr(m, "matched_by", "") or "").strip()
    if not raw:
        return ""
    if raw in _KNOWN_MATCHED_BY:
        return raw
    is_remote = str(getattr(m, "origin", "") or "").strip().lower() == "remote" or bool(
        str(getattr(m, "host_name", "") or "").strip()
    )
    return "remote" if is_remote else "other"


def _workspace_basename(cwd: str) -> str:
    cleaned = (cwd or "").replace("\\", "/").strip().rstrip("/")
    if not cleaned:
        return ""
    name = Path(cleaned).name
    return "" if (not name or name.endswith(":")) else name


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


def _derive_failed_label(
    snap: SessionSnapshot | None, win: ForegroundWindow, workspace_cwd: str = ""
) -> str:
    for cwd_cand in ((snap.match.cwd if snap else ""), win.cwd, workspace_cwd):
        if cwd_cand and cwd_cand.strip():
            name = Path(cwd_cand.strip().rstrip("/\\")).name
            if name:
                return name
    if snap and snap.match.cli and snap.match.session_id:
        return f"{snap.match.cli}-{snap.match.session_id[:8]}"
    return ""


def _make_cancel_check(
    seq_id: int,
    is_current_seq: Callable[[int], bool] | None,
    is_cancelled: Callable[[], bool] | None,
    cancel_token: CancelToken | None = None,
) -> Callable[[], bool] | None:
    if is_current_seq is None and is_cancelled is None and cancel_token is None:
        return None

    def _check() -> bool:
        if cancel_token is not None and cancel_token.is_cancelled():
            return True
        if is_cancelled is not None and is_cancelled():
            return True
        if is_current_seq is not None and not is_current_seq(seq_id):
            return True
        return False

    return _check


class _IsolatedSessionTrackerView:
    """Read-only tracker view mapping empty ``session_key`` lookups to a fixed snapshot key."""

    def __init__(self, inner: SessionTracker, bound_session_key: str) -> None:
        self._inner = inner
        self._bound_session_key = bound_session_key

    @property
    def has_bound_session(self) -> bool:
        return bool(self._bound_session_key)

    def _eff_key(self, session_key: str) -> str:
        return session_key or self._bound_session_key

    def get_refiner_context_with_prior(self, session_key: str, *args: Any, **kwargs: Any) -> Any:
        fn = getattr(self._inner, "get_refiner_context_with_prior", None)
        if callable(fn):
            return fn(self._eff_key(session_key), *args, **kwargs)
        raw_chunks, git_ctx = self._inner.get_refiner_context(
            self._eff_key(session_key),
            **{k: v for k, v in kwargs.items() if k != "allow_prior"},
        )
        return "", raw_chunks, git_ctx

    def get_refiner_context(self, session_key: str, *args: Any, **kwargs: Any) -> Any:
        return self._inner.get_refiner_context(self._eff_key(session_key), *args, **kwargs)

    def get_state(self, session_key: str) -> Any:
        return self._inner.get_state(self._eff_key(session_key))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _is_single_hop_asr_provider(eff_asr: BaseAsrProvider) -> bool:
    return bool(
        isinstance(eff_asr, (GeminiAudioProvider, ChatAudioAsrProvider))
        or getattr(eff_asr, "single_hop", False)
    )


def _existing_recoverable_retry_audio(
    raw_wav: bytes | str | Path,
    runtime_dir: Path | None,
) -> str:
    """Return the existing unexpired ``failed/`` WAV path for ``audio_source == 'retry'`` without duplicating it."""
    if runtime_dir is None or not isinstance(raw_wav, (str, Path)):
        return ""
    raw_str = str(raw_wav).strip()
    if not raw_str:
        return ""
    cand = Path(raw_str).expanduser()
    fdir = get_failed_audio_dir(runtime_dir, create=False)
    try:
        if not cand.is_file() or cand.is_symlink():
            return ""
        cand.resolve().relative_to(fdir.resolve())
        st_f = cand.stat()
        if st_f.st_size <= 0:
            return ""
        if FAILED_AUDIO_MAX_AGE_SEC > 0 and (time.time() - st_f.st_mtime) > FAILED_AUDIO_MAX_AGE_SEC:
            return ""
        return str(cand)
    except (OSError, ValueError):
        return ""


def _normalized_chunk_floor_dbfs(floor_dbfs: float, gain_db: float) -> float:
    """Map raw ``floor_dbfs`` into post-normalization scale using ``gate.gain_db``."""
    base = float(floor_dbfs)
    if not math.isfinite(gain_db) or gain_db == 0.0:
        return base
    shifted = base + float(gain_db)
    return min(-3.0, shifted) if gain_db > 0.0 else shifted


def _transcribe_wav_chunks(
    wav_bytes: bytes,
    *,
    eff_asr: BaseAsrProvider,
    asr_ctx: Any,
    floor_dbfs: float,
    audio_cfg: AudioConfig | None = None,
    gain_db: float = 0.0,
    max_chunk_sec: float | None = None,
    seq_id: int = 1,
    is_current_seq: Callable[[int], bool] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    cancel_token: CancelToken | None = None,
) -> tuple[AsrResult, int, tuple[float, ...]]:
    cancel_check = _make_cancel_check(seq_id, is_current_seq, is_cancelled, cancel_token)
    base_chunk_sec = (
        (SINGLE_HOP_MAX_CHUNK_SEC if _is_single_hop_asr_provider(eff_asr) else DEFAULT_MAX_CHUNK_SEC)
        if max_chunk_sec is None
        else float(max_chunk_sec)
    )
    prov_chunk_sec = getattr(eff_asr, "max_chunk_sec", None)
    eff_max_chunk_sec = (
        min(base_chunk_sec, float(prov_chunk_sec))
        if prov_chunk_sec is not None and float(prov_chunk_sec) > 0
        else base_chunk_sec
    )
    norm_floor_dbfs = _normalized_chunk_floor_dbfs(floor_dbfs, gain_db)
    infos = inspect_wav_chunks(wav_bytes, max_chunk_sec=eff_max_chunk_sec, floor_dbfs=norm_floor_dbfs)
    if len(infos) <= 1:
        if cancel_check is not None and cancel_check():
            return AsrResult(text="", provider=getattr(eff_asr, "provider_name", ""), model="", latency_ms=0), 1, ()
        return (
            eff_asr.transcribe(
                wav_bytes, context=asr_ctx, is_cancelled=cancel_check, cancel_token=cancel_token
            ),
            1,
            (),
        )

    chunk_secs = tuple(round(c.duration_sec, 2) for c in infos)
    t0 = time.monotonic()
    results: list[AsrResult | None] = [None] * len(infos)
    abort_evt = threading.Event()
    if cancel_token is not None:
        cancel_token.bind_event(abort_evt)
    first_exc_lock = threading.Lock()
    first_exc: list[Exception] = []

    def _chunk_cancelled() -> bool:
        return abort_evt.is_set() or (cancel_check is not None and cancel_check())

    def _work(chunk_wav: bytes) -> AsrResult | None:
        if _chunk_cancelled():
            return None
        try:
            return eff_asr.transcribe(
                chunk_wav,
                context=asr_ctx,
                is_cancelled=_chunk_cancelled,
                cancel_token=cancel_token,
            )
        except Exception as exc:
            if (cancel_check is not None and cancel_check()) and isinstance(exc, AsrError) and "cancelled" in str(exc).lower():
                return None
            with first_exc_lock:
                if not abort_evt.is_set():
                    first_exc.append(exc)
                    abort_evt.set()
            raise

    try:
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
    finally:
        if cancel_token is not None:
            cancel_token.unbind_event(abort_evt)

    def _sum_opt(vals: Sequence[int | None]) -> int | None:
        present = [v for v in vals if v is not None]
        return sum(present) if present else None

    non_null = [r for r in results if r is not None]
    merged_text = _join_chunk_texts([r.text for r in non_null if r.text.strip()])
    missed_chunks = 0
    if not (cancel_check is not None and cancel_check()):
        eff_audio_cfg = audio_cfg if audio_cfg is not None else AudioConfig(floor_dbfs=floor_dbfs)
        chunk_min_dur = min(eff_audio_cfg.min_duration_sec, eff_audio_cfg.min_voiced_sec)
        for idx, res_i in enumerate(results):
            if res_i is None or res_i.text.strip():
                continue
            try:
                if process_wav(
                    infos[idx].wav_bytes,
                    eff_audio_cfg,
                    floor_dbfs=norm_floor_dbfs,
                    min_duration_sec=chunk_min_dur,
                    silence_trim="off",
                ).passed:
                    missed_chunks += 1
            except Exception:
                pass
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
            input_tokens=_sum_opt([r.input_tokens for r in non_null]),
            cached_input_tokens=_sum_opt([r.cached_input_tokens for r in non_null]),
            output_tokens=_sum_opt([r.output_tokens for r in non_null]),
            reasoning_tokens=_sum_opt([r.reasoning_tokens for r in non_null]),
            context_chars=max((r.context_chars for r in non_null), default=0),
            context_chunks=max((r.context_chunks for r in non_null), default=0),
            audio_tokens=_sum_opt([r.audio_tokens for r in non_null]),
            missed_chunks=missed_chunks,
            request_id=",".join(r.request_id for r in non_null if r.request_id),
            duration_ms=_sum_opt([r.duration_ms for r in non_null]),
        ),
        len(infos),
        chunk_secs,
    )


def _classify_window_for_platform(win: ForegroundWindow, platform: Any) -> str:
    if not (win.process or win.bundle_id):
        return "general_gui"
    checker = getattr(platform, "is_allowed_terminal", None)
    is_term = (
        (lambda w: bool(checker(w)))
        if checker is not None
        else (
            lambda w: is_windows_terminal_process(w.process)
            or is_macos_terminal_window(w.process, w.bundle_id)
        )
    )
    return classify_window_category(win, is_terminal=is_term)


def _resolve_pipeline_window_routing(
    win: ForegroundWindow,
    *,
    platform: Any,
    origin_window: ForegroundWindow | None,
    window_title_override: str | None,
    audio_source: str,
    explicit_mode: str,
) -> tuple[bool, str]:
    """Return ``(should_sync_session, refiner_mode)`` for ``run_pipeline_once``."""
    m = (explicit_mode or "auto").strip().lower()
    if m == "general":
        return False, "general"
    if m == "agent":
        return True, "agent"
    if window_title_override is not None or bool(
        win.tty or win.cwd or win.tmux_pane or win.surface_id
    ):
        return True, "agent"
    if not (win.process or win.bundle_id):
        if (
            parse_window_title_hint(win.title) is not None
            or audio_source in ("file", "selftest_wav", "retry")
            or (platform is None and origin_window is None)
        ):
            return True, "agent"
        return False, "general"
    cat = _classify_window_for_platform(win, platform)
    if cat == "terminal":
        return True, "agent"
    if cat == "coding_gui":
        # v0.8-3: MUST skip sync_session so WSL2 /proc or macOS fallback never injects an
        # unrelated background CLI repo's 200K session into VS Code / Cursor / Antigravity IDE!
        return False, "agent"
    return False, "general"


@dataclass(slots=True)
class _Stage1State:
    t_total0: float
    queue_wait_ms: int = 0
    gate: AudioGateResult | None = None
    gate_ms: int = 0
    switched_fallback: bool = False
    win: ForegroundWindow = field(default_factory=ForegroundWindow)
    window_category: str = "general_gui"
    should_sync: bool = False
    eff_mode: str = "agent"
    snap: SessionSnapshot | None = None
    workspace_cwd: str = ""
    vocabulary_hints: str = ""
    remote_vetoed: bool = False
    asr_ctx_dialog: str = ""
    sync_ms: int = 0
    prep_ms: int = 0
    asr_res: AsrResult | None = None
    asr_chunks: int = 1
    asr_chunk_secs: tuple[float, ...] = ()
    asr_missed_chunks: int = 0
    skipped_reason: str = ""
    ctx_fallback: str = ""
    codex_promoted_sid: str = ""
    claude_promoted_sid: str = ""
    claude_promoted_route_id: str = ""
    agy_bound_sid: str = ""


def _make_desktop_same_window_verifier(
    st: _Stage1State,
    *,
    tracker: SessionTracker,
    env: Mapping[str, str],
) -> Callable[[ForegroundWindow], bool] | None:
    """Return a same-window conversation verifier for Codex desktop, Claude Desktop, or AGY 2.0 deliveries."""
    orig_win = st.win
    codex_sid = st.codex_promoted_sid
    claude_sid = st.claude_promoted_sid
    claude_route_id = st.claude_promoted_route_id
    agy_sid = st.agy_bound_sid
    if (not codex_sid and not claude_sid and not agy_sid) or orig_win.origin_drifted:
        return None
    if codex_sid:
        kind = "codex"
        bound_sid = codex_sid
    elif claude_sid:
        kind = "claude"
        bound_sid = claude_sid
    else:
        kind = "agy"
        bound_sid = agy_sid
    orig_title = (orig_win.title or "").strip()
    orig_bundle = (orig_win.bundle_id or "").strip().lower()

    def _verify(cur_win: ForegroundWindow) -> bool:
        if cur_win is None or (
            not cur_win.hwnd
            and not (cur_win.process or "").strip()
            and not (cur_win.bundle_id or "").strip()
        ):
            return False
        same_window = (
            (orig_win.hwnd > 0 and cur_win.hwnd == orig_win.hwnd)
            or (orig_win.hwnd <= 0 and cur_win.hwnd <= 0)
        )
        same_desktop_proc = bool(
            orig_win.hwnd > 0
            and normalize_app_token(cur_win.process)
            and normalize_app_token(cur_win.process) == normalize_app_token(orig_win.process)
        )
        same_desktop_bundle = bool(
            orig_win.hwnd > 0
            and orig_bundle
            and (cur_win.bundle_id or "").strip().lower() == orig_bundle
        )
        if kind == "codex":
            is_target_app = is_codex_desktop_window(cur_win)
        elif kind == "claude":
            is_target_app = is_claude_desktop_window(cur_win)
        else:
            is_target_app = is_antigravity_ide_window(cur_win)
        if not same_window or not (is_target_app or same_desktop_proc or same_desktop_bundle):
            return True
        cur_title = (cur_win.title or "").strip()
        if kind == "claude":
            cur_page_url = (getattr(cur_win, "page_url", "") or "").strip()
            if claude_route_id:
                cur_rk, cur_rsid = _parse_claude_desktop_page_url(cur_page_url)
                if cur_rk not in ("code", "cowork") or not cur_rsid:
                    return False
                if f"{cur_rk}:{cur_rsid.lower()}" != claude_route_id:
                    return False
            try:
                re_match = resolve_claude_desktop_session_by_title(
                    cur_title, page_url=cur_page_url, home=tracker.home, env=env
                )
            except Exception:
                return False
            return bool(re_match is not None and re_match.session_id == bound_sid)
        if cur_title == orig_title:
            return True
        try:
            if kind == "codex":
                re_match = resolve_codex_desktop_session_by_title(cur_title, home=tracker.home, env=env)
            else:
                re_match = resolve_antigravity_ide_session_by_title(cur_title, home=tracker.home, env=env)
        except Exception:
            return False
        return bool(re_match is not None and re_match.session_id == bound_sid)

    return _verify


_make_codex_desktop_same_window_verifier = _make_desktop_same_window_verifier


def _apply_fallback_same_window_verifier(
    orig_win: ForegroundWindow,
    *,
    platform: Any,
    verifier: Callable[[ForegroundWindow], bool],
) -> ForegroundWindow:
    get_fg = getattr(platform, "get_foreground_window", None)
    if not callable(get_fg):
        return replace(orig_win, origin_drifted=True)
    try:
        cur_win = get_fg()
        ok = bool(verifier(cur_win))
    except Exception:
        ok = False
    return orig_win if ok else replace(orig_win, origin_drifted=True)


def _make_pipeline_result(
    seq_id: int,
    audio_source: str,
    st: _Stage1State,
    *,
    ok: bool = False,
    final_text: str = "",
    skipped_reason: str = "",
    refined: RefinerResult | None = None,
    paste: PasteOutcome | None = None,
    deliver_ms: int = 0,
    saved_audio: str = "",
    error: str = "",
) -> PipelineResult:
    eff_ctx_fb = ""
    if st.eff_mode != "general":
        if (
            (refined is not None and getattr(refined, "used_prior_session", False))
            or st.ctx_fallback == "prior_session"
        ):
            eff_ctx_fb = "prior_session"
        elif st.ctx_fallback == "prior_hotwords":
            eff_ctx_fb = "prior_hotwords"
    return PipelineResult(
        ok,
        seq_id,
        final_text=final_text,
        skipped_reason=skipped_reason,
        audio_source=audio_source,
        switched_fallback=st.switched_fallback,
        gate=st.gate,
        session=st.snap,
        asr=st.asr_res,
        refined=refined,
        paste=paste,
        error=error,
        asr_chunks=st.asr_chunks,
        asr_chunk_secs=st.asr_chunk_secs,
        asr_missed_chunks=st.asr_missed_chunks,
        saved_audio=saved_audio,
        gate_ms=st.gate_ms,
        sync_ms=st.sync_ms,
        refine_init_ms=st.prep_ms + (refined.init_ms if refined else 0),
        deliver_ms=deliver_ms,
        total_ms=_elapsed_ms(st.t_total0),
        queue_wait_ms=st.queue_wait_ms,
        ctx_chars=refined.context_chars if refined else 0,
        ctx_chunks=refined.context_chunks if refined else 0,
        mode=st.eff_mode,
        ctx_fallback=eff_ctx_fb,
        workspace_cwd=st.workspace_cwd,
    )


def _resolve_stage1_context(
    st: _Stage1State,
    *,
    cfg: Config,
    env: Mapping[str, str],
    tracker: SessionTracker,
    platform: Any,
    origin_window: ForegroundWindow | None,
    window_title_override: str | None,
    audio_source: str,
    explicit_mode: str,
    cancel_check: Callable[[], bool] | None,
    attach_single_hop: bool = False,
    known_cwds_override: Sequence[str] | None = None,
) -> Any:
    """Shared real window routing, session/IDE/desktop context resolution, and hotwords builder."""
    t_sync0 = time.monotonic()
    win = origin_window or (platform.get_foreground_window() if platform else ForegroundWindow())
    st.win = win
    st.window_category = _classify_window_for_platform(win, platform)
    should_sync, eff_mode = _resolve_pipeline_window_routing(
        win,
        platform=platform,
        origin_window=origin_window,
        window_title_override=window_title_override,
        audio_source=audio_source,
        explicit_mode=explicit_mode,
    )
    st.should_sync = should_sync
    st.eff_mode = eff_mode
    st.remote_vetoed = False
    extra_hw: list[str] = []
    origin_drifted = bool(win.origin_drifted and window_title_override is None)
    if should_sync and not origin_drifted:
        if window_title_override is None:
            st.snap = tracker.sync_session(
                window_title=win.title,
                window_tty=win.tty,
                window_cwd=win.cwd,
                window_pid=win.pid,
                window_process=win.process,
                bundle_id=win.bundle_id,
                document=win.document,
                tmux_socket=win.tmux_socket,
                tmux_pane=win.tmux_pane,
                cancel_token=cancel_check,
                cold_start_fallback=cfg.refiner.cold_start_fallback,
            )
        else:
            st.snap = tracker.sync_session(
                window_title=window_title_override,
                cancel_token=cancel_check,
                cold_start_fallback=cfg.refiner.cold_start_fallback,
            )
        st.remote_vetoed = bool(getattr(tracker, "last_sync_remote_vetoed", False))
        if (
            st.snap is None
            and window_title_override is None
            and win.cwd
            and not st.remote_vetoed
            and not getattr(tracker, "last_sync_target_drifted", False)
        ):
            eff_wsl = _effective_is_wsl(env=env)
            safe_cwd = _resolve_safe_local_dir(win.cwd, wsl=eff_wsl)
            if safe_cwd and not _is_broad_or_unsafe_workspace_dir(
                safe_cwd, getattr(tracker, "home", None), wsl=eff_wsl
            ):
                st.workspace_cwd = safe_cwd
                extra_hw = tracker.build_workspace_hotwords(st.workspace_cwd)
                if cfg.refiner.cold_start_fallback != "off" and (
                    prior_hw_fn := getattr(tracker, "get_workspace_prior_hotwords", None)
                ) is not None:
                    if prior_hw := prior_hw_fn(
                        st.workspace_cwd,
                        scope="cli",
                        cold_start_fallback=cfg.refiner.cold_start_fallback,
                    ):
                        extra_hw = _dedupe_hotwords_ci([*extra_hw, *prior_hw])
                        st.ctx_fallback = "prior_hotwords"
    elif not origin_drifted and eff_mode == "agent" and (win.process or win.bundle_id):
        is_agy_ide = is_antigravity_ide_window(win)
        is_rem_ide = is_remote_ide_window(win)
        if is_rem_ide:
            st.remote_vetoed = True
        skip_legacy_agy = bool(
            is_agy_ide
            and not (getattr(win, "document", "") or "").strip()
            and (win.title or "").strip().lower() in _AGY_DESKTOP_REJECT_TITLES
        )
        agy_db_veto: list[str] = []
        if is_agy_ide and not is_rem_ide and not skip_legacy_agy:
            ide_match = resolve_antigravity_ide_session_by_title(
                win.title, home=tracker.home, env=env, veto_out=agy_db_veto
            )
            if ide_match is not None:
                if ide_match.cwd:
                    st.workspace_cwd = ide_match.cwd
                st.snap = tracker.sync_ide_session(
                    ide_match, cold_start_fallback=cfg.refiner.cold_start_fallback
                )
                if st.snap is not None:
                    st.agy_bound_sid = st.snap.match.session_id or ide_match.session_id
        if st.snap is None and not skip_legacy_agy and not agy_db_veto:
            st.workspace_cwd = resolve_ide_workspace_cwd(
                win,
                home=tracker.home,
                known_cwds=(
                    known_cwds_override
                    if known_cwds_override is not None
                    else tracker.active_session_cwds()
                ),
                env=env,
            )
            if st.workspace_cwd and is_agy_ide:
                ide_match = resolve_antigravity_ide_session(
                    st.workspace_cwd, home=tracker.home, env=env
                )
                if ide_match is not None:
                    st.snap = tracker.sync_ide_session(
                        ide_match, cold_start_fallback=cfg.refiner.cold_start_fallback
                    )
        if st.snap is None and st.workspace_cwd:
            extra_hw = tracker.build_workspace_hotwords(st.workspace_cwd)
            if cfg.refiner.cold_start_fallback != "off" and (
                prior_hw_fn := getattr(tracker, "get_workspace_prior_hotwords", None)
            ) is not None:
                ide_scope = "agy_ide" if is_agy_ide else "cli"
                if prior_hw := prior_hw_fn(
                    st.workspace_cwd,
                    scope=ide_scope,
                    cold_start_fallback=cfg.refiner.cold_start_fallback,
                ):
                    extra_hw = _dedupe_hotwords_ci([*extra_hw, *prior_hw])
                    st.ctx_fallback = "prior_hotwords"
    elif eff_mode == "general" and (win.process or win.bundle_id):
        codex_match = None
        claude_match = None
        if not origin_drifted and (explicit_mode or "auto").strip().lower() != "general":
            if is_codex_desktop_window(win):
                codex_match = resolve_codex_desktop_session_by_title(
                    win.title, home=tracker.home, env=env
                )
            elif is_claude_desktop_window(win):
                claude_match = resolve_claude_desktop_session_by_title(
                    win.title,
                    page_url=getattr(win, "page_url", ""),
                    home=tracker.home,
                    env=env,
                )
        if codex_match is not None:
            eff_mode = "agent"
            st.eff_mode = "agent"
            st.workspace_cwd = codex_match.cwd
            st.codex_promoted_sid = codex_match.session_id
            st.snap = tracker.sync_ide_session(
                codex_match, cold_start_fallback=cfg.refiner.cold_start_fallback
            )
        elif claude_match is not None:
            eff_mode = "agent"
            st.eff_mode = "agent"
            st.workspace_cwd = claude_match.cwd
            st.claude_promoted_sid = claude_match.session_id
            rk, rsid = _parse_claude_desktop_page_url(getattr(win, "page_url", ""))
            st.claude_promoted_route_id = (
                f"{rk}:{rsid.lower()}" if (rk in ("code", "cowork") and rsid) else ""
            )
            st.snap = tracker.sync_ide_session(
                claude_match, cold_start_fallback=cfg.refiner.cold_start_fallback
            )

    if (
        eff_mode != "general"
        and st.snap is not None
        and getattr(st.snap, "prior_hotwords_used", False)
        and cfg.refiner.cold_start_fallback != "off"
        and not st.ctx_fallback
    ):
        st.ctx_fallback = "prior_hotwords"

    hw_path = resolve_personal_hotwords_path(env)
    if hw_path is not None:
        hw_table = load_hotwords_table(hw_path, env=env)
        personal_hw = tuple(e.term for e in order_stage1_entries(hw_table.entries))
        stage2_entries = order_stage2_entries(hw_table.entries)
        st.vocabulary_hints = format_stage2_vocabulary_hints(stage2_entries)
    else:
        personal_hw = ()
        st.vocabulary_hints = ""
    asr_ctx = tracker.build_asr_context(
        st.snap,
        personal_hotwords=personal_hw,
        workspace_dynamic_hotwords=extra_hw,
        mode=eff_mode,
        language=_resolve_effective_ui_language(cfg, env=env, platform=platform),
    )
    asr_ctx.manual_hotwords_loaded = True
    st.asr_ctx_dialog = asr_ctx.dialog_text
    if attach_single_hop:
        asr_ctx = attach_single_hop_context(
            cfg,
            asr_ctx,
            tracker=tracker if (st.snap is not None or st.workspace_cwd) else None,
            session_key=st.snap.session_key if st.snap else "",
            mode=eff_mode,
            workspace_cwd=st.workspace_cwd,
            vocabulary_hints=st.vocabulary_hints,
        )
        if eff_mode != "general" and any(
            b.startswith("<prior_session_reference") for b in asr_ctx.context_blocks
        ):
            st.ctx_fallback = "prior_session"
    st.sync_ms = _elapsed_ms(t_sync0)
    return asr_ctx


def _execute_stage1(
    st: _Stage1State,
    raw_wav: bytes | str | Path,
    *,
    cfg: Config,
    env: Mapping[str, str],
    tracker: SessionTracker,
    eff_asr: BaseAsrProvider,
    eff_refiner: BaseRefiner,
    platform: Any,
    origin_window: ForegroundWindow | None,
    window_title_override: str | None,
    audio_source: str,
    explicit_mode: str,
    seq_id: int,
    max_chunk_sec: float | None,
    is_current_seq: Callable[[int], bool] | None,
    is_cancelled: Callable[[], bool] | None,
    cancel_token: CancelToken | None,
    cancel_check: Callable[[], bool] | None,
    cancel_reason_fn: Callable[[], str],
    archive_sink: Callable[[threading.Thread], None] | None,
    known_cwds_override: Sequence[str] | None = None,
) -> None:
    if isinstance(raw_wav, (bytes, bytearray)) and len(raw_wav) == 0:
        if c_rsn := cancel_reason_fn():
            st.skipped_reason = c_rsn
        else:
            st.switched_fallback = (
                bool(platform and platform.switch_to_fallback_audio())
                if audio_source in ("pulse", "winmm")
                else False
            )
            st.skipped_reason = "empty_audio"
        return

    if c_rsn := cancel_reason_fn():
        st.skipped_reason = c_rsn
        return

    t_gate0 = time.monotonic()
    # Retry replays audio that already passed capture gating (and may be shorter or have fewer voiced
    # floor-exceeding noise samples after prior trimming); cap duration/voiced gates to built-in defaults
    # and skip re-trimming so elevated user thresholds do not reject valid failed/ clips.
    is_retry = audio_source == "retry"
    gate = process_wav(
        raw_wav,
        cfg.audio,
        min_duration_sec=min(cfg.audio.min_duration_sec, AudioConfig.min_duration_sec) if is_retry else None,
        min_voiced_sec=min(cfg.audio.min_voiced_sec, AudioConfig.min_voiced_sec) if is_retry else None,
        silence_trim="off" if is_retry else None,
    )
    st.gate = gate
    st.gate_ms = _elapsed_ms(t_gate0)
    if not gate.passed:
        if c_rsn := cancel_reason_fn():
            st.skipped_reason = c_rsn
        else:
            st.switched_fallback = bool(
                gate.all_zero
                and gate.reason == "silence"
                and audio_source == "pulse"
                and platform
                and platform.switch_to_fallback_audio()
            )
            st.skipped_reason = gate.reason
        return

    if c_rsn := cancel_reason_fn():
        st.skipped_reason = c_rsn
        return

    if (
        cfg.audio.save_recordings
        and audio_source not in ("retry", "selftest_wav")
        and archive_sink is not None
    ):
        try:
            arch_bytes = (
                bytes(raw_wav)
                if isinstance(raw_wav, (bytes, bytearray, memoryview))
                else (Path(raw_wav).read_bytes() or gate.wav_bytes)
            )
            if (
                arch_th := spawn_recording_archive(
                    arch_bytes,
                    resolve_saved_recordings_dir(env, create=True),
                    max_count=cfg.audio.save_recordings_max_count,
                )
            ) is not None:
                archive_sink(arch_th)
        except Exception:
            pass

    asr_ctx = _resolve_stage1_context(
        st,
        cfg=cfg,
        env=env,
        tracker=tracker,
        platform=platform,
        origin_window=origin_window,
        window_title_override=window_title_override,
        audio_source=audio_source,
        explicit_mode=explicit_mode,
        cancel_check=cancel_check,
        attach_single_hop=_is_single_hop_asr_provider(eff_asr),
        known_cwds_override=known_cwds_override,
    )

    if (
        audio_source != "selftest_wav"
        and not (cancel_check is not None and cancel_check())
        and _may_run_stage2_refiner(cfg, eff_refiner, eff_asr)
        and (prep_fn := getattr(eff_refiner, "prepare_session", None)) is not None
    ):
        t_prep0 = time.monotonic()
        try:
            prep_fn(st.snap.session_key if st.snap else "")
        except Exception:
            pass
        st.prep_ms = _elapsed_ms(t_prep0)

    try:
        asr_res, chunk_count, chunk_secs = _transcribe_wav_chunks(
            gate.wav_bytes,
            eff_asr=eff_asr,
            asr_ctx=asr_ctx,
            floor_dbfs=cfg.audio.floor_dbfs,
            audio_cfg=cfg.audio,
            gain_db=gate.gain_db,
            max_chunk_sec=max_chunk_sec,
            seq_id=seq_id,
            is_current_seq=is_current_seq,
            is_cancelled=is_cancelled,
            cancel_token=cancel_token,
        )
    except Exception as asr_exc:
        if c_rsn := cancel_reason_fn():
            if c_rsn in _HARD_CANCEL_REASONS or (
                isinstance(asr_exc, AsrError) and "cancelled" in str(asr_exc).lower()
            ):
                st.skipped_reason = c_rsn
                return
        raise
    st.asr_res = asr_res
    st.asr_chunks = chunk_count
    st.asr_chunk_secs = chunk_secs
    st.asr_missed_chunks = max(0, int(getattr(asr_res, "missed_chunks", 0) or 0))
    if not asr_res.text.strip():
        st.skipped_reason = cancel_reason_fn() or "empty_asr"
    elif c_rsn := cancel_reason_fn():
        st.skipped_reason = c_rsn


def run_pipeline_once(
    raw_wav: bytes | str | Path,
    *,
    cfg: Config,
    creds: ProviderCredentials | None = None,
    tracker: SessionTracker | None = None,
    asr_provider: BaseAsrProvider | None = None,
    refiner: BaseRefiner | None = None,
    platform: Any = None,
    origin_window: ForegroundWindow | None = None,
    window_title_override: str | None = None,
    audio_source: str = "file",
    seq_id: int = 1,
    dry_run: bool = False,
    is_current_seq: Callable[[int], bool] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    cancel_token: CancelToken | None = None,
    deliver_fn: Callable[[str, ForegroundWindow], PasteOutcome] | None = None,
    deliver_text_fn: Callable[..., PasteOutcome] | None = None,
    on_stage: Callable[[str], None] | None = None,
    runtime_dir: Path | None = None,
    max_chunk_sec: float | None = None,
    env: Mapping[str, str] | None = None,
    archive_sink: Callable[[threading.Thread], None] | None = None,
    mode: str = "auto",
    queue_wait_ms: int = 0,
    known_cwds_override: Sequence[str] | None = None,
    isolate_refiner_session: bool = False,
) -> PipelineResult:
    eff_env = os.environ if env is None else env
    eff_creds = creds or resolve_credentials(cfg, eff_env)
    owns_tracker = tracker is None
    eff_tracker = tracker or SessionTracker(
        env=eff_env,
        runtime_dir=runtime_dir,
        remote_cfg=cfg.remote,
        context_max_chars=cfg.refiner.context_max_chars,
        cold_start_fallback=cfg.refiner.cold_start_fallback,
    )
    eff_asr = asr_provider or create_asr_provider(cfg, eff_creds, env=eff_env)
    owns_refiner = refiner is None
    eff_refiner = refiner or create_refiner(cfg, eff_creds, env=env, runtime_dir=runtime_dir)
    can_save_failed = runtime_dir is not None and audio_source not in ("file", "selftest_wav", "retry")
    local_archive_threads: list[threading.Thread] = []
    stashed_once = False
    stashed_path = ""
    cancel_check = _make_cancel_check(seq_id, is_current_seq, is_cancelled, cancel_token)
    st = _Stage1State(
        t_total0=time.monotonic(),
        queue_wait_ms=queue_wait_ms,
        win=origin_window or ForegroundWindow(),
    )

    def _cancel_reason() -> str:
        if cancel_token is not None and cancel_token.is_cancelled():
            return cancel_token.reason or "cancelled"
        if cancel_check is not None and cancel_check():
            return "superseded"
        return ""

    def _stash_failed() -> str:
        nonlocal stashed_once, stashed_path
        if stashed_once:
            return stashed_path
        stashed_once = True
        if audio_source == "retry":
            stashed_path = _existing_recoverable_retry_audio(raw_wav, runtime_dir)
            return stashed_path
        if not can_save_failed or runtime_dir is None or st.gate is None or not st.gate.passed or not st.gate.wav_bytes:
            return ""
        saved = save_failed_audio(
            st.gate.wav_bytes,
            runtime_dir,
            duration_sec=st.gate.duration_sec,
            label=_derive_failed_label(st.snap, st.win, st.workspace_cwd),
        )
        stashed_path = str(saved) if saved is not None else ""
        return stashed_path

    def _stash_for_cancel(reason: str) -> str:
        if reason == "shutdown" or st.gate is None or not st.gate.passed:
            return ""
        if reason == "cancelled" or st.gate.duration_sec >= 15.0:
            return _stash_failed()
        return ""

    def _cancel_res(
        reason: str,
        *,
        draft: str = "",
        refined: RefinerResult | None = None,
        deliver_ms: int = 0,
    ) -> PipelineResult:
        out_txt = "" if reason in _HARD_CANCEL_REASONS else draft
        return _make_pipeline_result(
            seq_id,
            audio_source,
            st,
            final_text=out_txt,
            skipped_reason=reason,
            refined=refined,
            deliver_ms=deliver_ms,
            saved_audio=_stash_for_cancel(reason),
        )

    eff_sink = archive_sink if archive_sink is not None else local_archive_threads.append
    in_delivery = False
    try:
        try:
            _execute_stage1(
                st,
                raw_wav,
                cfg=cfg,
                env=eff_env,
                tracker=eff_tracker,
                eff_asr=eff_asr,
                eff_refiner=eff_refiner,
                platform=platform,
                origin_window=origin_window,
                window_title_override=window_title_override,
                audio_source=audio_source,
                explicit_mode=mode,
                seq_id=seq_id,
                max_chunk_sec=max_chunk_sec,
                is_current_seq=is_current_seq,
                is_cancelled=is_cancelled,
                cancel_token=cancel_token,
                cancel_check=cancel_check,
                cancel_reason_fn=_cancel_reason,
                archive_sink=eff_sink,
                known_cwds_override=known_cwds_override,
            )
            if st.skipped_reason:
                sr = st.skipped_reason
                if sr in (*_HARD_CANCEL_REASONS, "superseded"):
                    return _cancel_res(sr, draft=(st.asr_res.text.strip() if st.asr_res else ""))
                return _make_pipeline_result(
                    seq_id,
                    audio_source,
                    st,
                    skipped_reason=sr,
                    saved_audio=(_stash_failed() if sr == "empty_asr" else ""),
                )

            assert st.asr_res is not None
            if _will_run_stage2_refiner(cfg, eff_refiner, st.asr_res) and on_stage is not None:
                on_stage("refining")
            real_skey = st.snap.session_key if st.snap else ""
            ref_session_key = "" if isolate_refiner_session else real_skey
            ref_tracker: Any = eff_tracker
            ref_workspace_cwd = st.workspace_cwd
            ref_fallback_ctx = st.asr_ctx_dialog
            if isolate_refiner_session and real_skey:
                # Deliberate exception: pass empty session_key so stateful refiners (e.g. AgyRefiner)
                # run statelessly without touching/evicting live session slots while keeping real st.snap context.
                ref_tracker = _IsolatedSessionTrackerView(eff_tracker, real_skey)
                if not ref_workspace_cwd and st.snap is not None and st.snap.match.cwd:
                    ref_workspace_cwd = st.snap.match.cwd
            refine_kw: dict[str, Any] = {
                "session_key": ref_session_key,
                "tracker": ref_tracker,
                "fallback_context": ref_fallback_ctx,
                "is_cancelled": cancel_check,
                "cancel_token": cancel_token,
                "mode": st.eff_mode,
                "workspace_cwd": ref_workspace_cwd,
            }
            if st.vocabulary_hints:
                refine_kw["vocabulary_hints"] = st.vocabulary_hints
            ref_res = eff_refiner.refine(st.asr_res, **refine_kw)
            cand_text = (ref_res.text.strip() if ref_res else "") or st.asr_res.text.strip()
            if (c_rsn := _cancel_reason()) or (
                ref_res and ref_res.used_fallback in (*_HARD_CANCEL_REASONS, "superseded")
            ):
                return _cancel_res(
                    c_rsn or (ref_res.used_fallback if ref_res else "cancelled"),
                    draft=cand_text,
                    refined=ref_res,
                )

            deliver_ms = 0
            if dry_run or (platform is None and deliver_fn is None):
                if cancel_token is not None and not cancel_token.try_commit():
                    return _cancel_res(cancel_token.reason or "cancelled", draft=cand_text, refined=ref_res)
                paste_out = None
            elif deliver_fn is not None:
                if cancel_token is not None and not cancel_token.try_commit():
                    return _cancel_res(cancel_token.reason or "cancelled", draft=cand_text, refined=ref_res)
                verifier = _make_desktop_same_window_verifier(
                    st, tracker=eff_tracker, env=eff_env
                )
                deliver_win = (
                    _apply_fallback_same_window_verifier(
                        st.win, platform=platform, verifier=verifier
                    )
                    if (verifier is not None and platform is not None)
                    else st.win
                )
                t_deliv0 = time.monotonic()
                in_delivery = True
                paste_out = deliver_fn(cand_text, deliver_win)
                in_delivery = False
                deliver_ms = _elapsed_ms(t_deliv0)
            else:
                verifier = _make_desktop_same_window_verifier(
                    st, tracker=eff_tracker, env=eff_env
                )
                deliver_win = st.win
                target_deliv_fn = deliver_text_fn or platform.deliver_text
                t_deliv0 = time.monotonic()
                in_delivery = True
                paste_out = target_deliv_fn(
                    cand_text,
                    seq_id=seq_id,
                    target_hwnd=deliver_win.hwnd,
                    origin_window=deliver_win,
                    is_cancelled=cancel_check,
                    before_commit=(cancel_token.try_commit if cancel_token is not None else None),
                    same_window_verifier=verifier,
                )
                in_delivery = False
                deliver_ms = _elapsed_ms(t_deliv0)

            if paste_out is not None and paste_out.reason == "cancelled":
                return _cancel_res(
                    _cancel_reason() or "cancelled",
                    draft=cand_text,
                    refined=ref_res,
                    deliver_ms=deliver_ms,
                )
            if cancel_token is not None:
                cancel_token.try_commit()
            delivery_failed = paste_out is not None and not paste_out.pasted and not paste_out.clipboard
            saved_p = _stash_failed() if (delivery_failed or st.asr_missed_chunks > 0) else ""
            eff_skip = (
                (
                    paste_out.reason
                    if (paste_out is not None and paste_out.reason and paste_out.reason != "ok")
                    else "paste_failed"
                )
                if delivery_failed
                else ""
            )
            return _make_pipeline_result(
                seq_id,
                audio_source,
                st,
                ok=(not delivery_failed),
                final_text=cand_text,
                skipped_reason=eff_skip,
                refined=ref_res,
                paste=paste_out,
                deliver_ms=deliver_ms,
                saved_audio=saved_p,
            )
        except Exception as exc:
            if (c_rsn := _cancel_reason()) in _HARD_CANCEL_REASONS:
                return _cancel_res(c_rsn)
            saved_p = _stash_failed()
            ds_err = "undelivered_recoverable" if saved_p else "undelivered_unrecoverable"
            try:
                setattr(exc, "_delivery_status", ds_err)
                if saved_p:
                    setattr(exc, "_saved_audio_path", saved_p)
                if in_delivery:
                    setattr(exc, "_skipped_reason", "delivery_error")
                    setattr(exc, "_paste_reason", "delivery_error")
            except Exception:
                pass
            raise
    finally:
        if owns_refiner:
            eff_refiner.close()
        if owns_tracker and (tr_cl := getattr(eff_tracker, "close", None)) is not None:
            tr_cl()
        if local_archive_threads:
            wait_recording_archives(local_archive_threads, timeout_sec=2.0)


def make_isolated_probe_tracker(tracker: Any) -> tuple[Any, bool]:
    """Clone a ``SessionTracker`` with non-multiplexed SSH probing so selftests stay isolated."""
    if not isinstance(tracker, SessionTracker):
        return tracker, False
    probe_rm = RemoteSshManager(
        tracker.remote_cfg,
        env=tracker.env,
        runtime_dir=tracker.runtime_dir,
        enable_multiplexing=False,
    )
    probe_tr = SessionTracker(
        home_dir=tracker.home,
        runtime_dir=tracker.runtime_dir,
        proc_root=tracker.proc_root,
        env=tracker.env,
        tmux_socket=tracker._tmux_socket or None,
        tmux_socket_name=tracker._tmux_socket_name or None,
        tmux_probe_fn=tracker._tmux_probe_fn,
        remote_cfg=tracker.remote_cfg,
        local_hostnames=tracker._custom_local_hostnames,
        remote_manager=probe_rm,
        context_max_chars=tracker.context_max_chars,
        cold_start_fallback=tracker.cold_start_fallback,
    )
    return probe_tr, True


def run_probe_route_request(
    req: Mapping[str, Any],
    *,
    cfg: Config,
    env: Mapping[str, str],
    probe_tracker: Any,
    platform: Any,
    known_cwds_snap: Sequence[str],
    cancel_fn: Callable[[], bool],
) -> dict[str, Any]:
    """Execute ``probe_route`` routing & context inspection without mutating live daemon state."""
    origin_win = _resolve_control_origin_window(req)
    t0 = time.monotonic()
    st = _Stage1State(t_total0=t0, win=origin_win or ForegroundWindow())
    asr_ctx = _resolve_stage1_context(
        st,
        cfg=cfg,
        env=env,
        tracker=probe_tracker,
        platform=platform,
        origin_window=origin_win,
        window_title_override=None,
        audio_source="probe_route",
        explicit_mode=str(req.get("mode") or "auto"),
        cancel_check=cancel_fn,
        attach_single_hop=False,
        known_cwds_override=known_cwds_snap,
    )
    if st.eff_mode == "general":
        ctx_blocks: list[str] = []
    else:
        ctx_blocks, _ = build_refiner_context_chunks(
            cfg,
            session_key=st.snap.session_key if st.snap else "",
            tracker=probe_tracker if (st.snap is not None or st.workspace_cwd) else None,
            fallback_context=st.asr_ctx_dialog,
            workspace_cwd=st.workspace_cwd,
        )
        if any(b.startswith("<prior_session_reference") for b in ctx_blocks):
            st.ctx_fallback = "prior_session"
    eff_cwd = (
        st.snap.match.cwd
        if (st.snap is not None and st.snap.match.cwd)
        else ""
    ) or st.workspace_cwd
    return {
        "ok": True,
        "window_category": st.window_category,
        "mode": st.eff_mode,
        "should_sync": bool(st.should_sync),
        "hotword_count": len(asr_ctx.hotwords),
        "personal_hotword_count": len(asr_ctx.personal_hotwords),
        "session_key_masked": _mask_session_key(st.snap),
        "matched_by": _sanitize_matched_by(st.snap),
        "remote_vetoed": bool(st.remote_vetoed),
        "workspace_name": _workspace_basename(eff_cwd),
        "ctx_chars": sum(len(b) for b in ctx_blocks),
        "ctx_chunks": len(ctx_blocks),
        "ctx_fallback": st.ctx_fallback,
        "sync_ms": _elapsed_ms(t0),
    }


def validate_selftest_wav_request(
    req: Mapping[str, Any],
    *,
    platform: Any,
    deadline: float | None = None,
) -> tuple[Path | None, ForegroundWindow | None, bool, float | None, str]:
    """Validate ``selftest_wav`` parameters and pin live foreground window when requested."""
    eff_deadline = deadline
    if eff_deadline is None:
        try:
            parsed_timeout = _parse_optional_timeout_sec(req.get("timeout_sec"))
        except ValueError as exc:
            return None, None, False, None, str(exc)
        if parsed_timeout is not None:
            eff_deadline = time.monotonic() + parsed_timeout
    raw_deliver = req.get("deliver", False)
    if not isinstance(raw_deliver, bool):
        return None, None, False, None, "deliver must be a boolean"
    deliver = raw_deliver
    if deliver and (req.get("use_live_window") is not True or _has_window_fields(req)):
        return (
            None,
            None,
            False,
            None,
            "deliver requires use_live_window=true without window overrides",
        )
    raw_wav_path = str(req.get("wav_path") or "").strip()
    wav_path = Path(raw_wav_path).expanduser() if raw_wav_path else None
    if wav_path is None or not wav_path.is_file():
        return None, None, False, None, f"wav_path not found: {raw_wav_path}"
    origin_win = _resolve_control_origin_window(req)
    if origin_win is None:
        origin_win = (
            platform.get_foreground_window() if platform is not None else ForegroundWindow()
        )
        if deliver and (
            not origin_win.hwnd
            and not (origin_win.process or "").strip()
            and not (origin_win.bundle_id or "").strip()
        ):
            return None, None, False, None, "no live foreground window"
    return wav_path, origin_win, deliver, eff_deadline, ""


def format_selftest_wav_response(
    cur_seq: int, res: PipelineResult, summary: Mapping[str, Any]
) -> dict[str, Any]:
    """Build the control response dictionary for a completed ``selftest_wav`` run."""
    eff_cwd = (
        res.session.match.cwd
        if (res.session is not None and res.session.match.cwd)
        else ""
    ) or res.workspace_cwd
    routing = {
        "mode": res.mode,
        "session_key_masked": _mask_session_key(res.session),
        "ctx_chars": int(res.ctx_chars),
        "ctx_chunks": int(res.ctx_chunks),
        "ctx_fallback": res.ctx_fallback,
        "workspace_name": _workspace_basename(eff_cwd),
    }
    return {
        "ok": res.ok,
        "seq_id": cur_seq,
        "skipped_reason": res.skipped_reason,
        "asr_text": res.asr.text if res.asr else "",
        "final_text": res.final_text,
        "pasted": bool(summary["pasted"]),
        "clipboard": bool(summary["clipboard"]),
        "paste_reason": str(summary["paste_reason"]),
        "delivery_status": str(summary["delivery_status"]),
        "summary": dict(summary),
        "routing": routing,
    }


def classify_skipped_commit_hud(
    skipped_reason: str,
    *,
    recorder_lost: bool,
    saved_audio: str,
) -> tuple[str, int, str, str]:
    """Return ``(effective_skipped_reason, hud_state, hud_duration_ms, beep_kind)``."""
    hud_st, hud_dur_ms, beep_k = "", 0, ""
    eff_sr = skipped_reason
    if skipped_reason in ("too_short", "silence", "empty_audio"):
        if recorder_lost:
            hud_st, hud_dur_ms, beep_k = "mic_disconnected", 2200, ""
            eff_sr = "mic_disconnected"
        else:
            hud_st, beep_k = "empty", "error"
    elif skipped_reason == "empty_asr":
        if recorder_lost:
            hud_st, hud_dur_ms, beep_k = "mic_disconnected", 2200, ""
            eff_sr = "mic_disconnected"
        else:
            hud_st = "asr_empty_retry" if saved_audio else "asr_empty"
            beep_k = "error"
    elif skipped_reason != "superseded":
        hud_st, beep_k = "error", "error"
    if recorder_lost and beep_k == "error":
        beep_k = ""
    return eff_sr, hud_st, hud_dur_ms, beep_k


def classify_delivered_commit_hud(
    paste_out: PasteOutcome | None,
    *,
    asr_missed_chunks: int,
    recorder_lost: bool,
    saved_audio: str,
) -> tuple[bool, str, str, str]:
    """Return ``(delivery_failed, eff_skipped_reason, hud_state, beep_kind)``."""
    delivery_failed = paste_out is not None and not paste_out.pasted and not paste_out.clipboard
    copied_only = bool(paste_out and not paste_out.pasted and paste_out.clipboard)
    eff_skip = (
        (
            paste_out.reason
            if (paste_out is not None and paste_out.reason and paste_out.reason != "ok")
            else "paste_failed"
        )
        if delivery_failed
        else ""
    )
    beep_k = ""
    if delivery_failed:
        hud_st = "paste_failed_retry" if saved_audio else "paste_failed"
        beep_k = "" if recorder_lost else "error"
    elif asr_missed_chunks > 0:
        if copied_only:
            hud_st = "partial_asr_copied" if saved_audio else "partial_asr_copied_unsaved"
        else:
            hud_st = "partial_asr" if saved_audio else "partial_asr_unsaved"
    elif copied_only:
        hud_st = "copied"
    else:
        hud_st = "done"
    return delivery_failed, eff_skip, hud_st, beep_k


class _OriginWindowProbe:
    """Optional background foreground-window resolver started alongside recording start."""

    __slots__ = ("_platform", "_trigger_win", "_resolver", "_box", "_err", "_done", "_spawn_failed")

    def __init__(self, platform: Any, trigger_win: ForegroundWindow | None, seq_id: int) -> None:
        self._platform = platform
        self._trigger_win = trigger_win
        self._resolver = getattr(platform, "resolve_foreground_window", None)
        self._box: list[ForegroundWindow | None] = [None]
        self._err: list[BaseException | None] = [None]
        self._done: threading.Event | None = None
        self._spawn_failed = False
        if callable(self._resolver) and bool(getattr(platform, "_concurrent_origin_resolve", False)):
            done_evt = threading.Event()
            self._done = done_evt
            res_fn = self._resolver

            def _bg() -> None:
                try:
                    self._box[0] = res_fn(trigger_win)
                except BaseException as exc:
                    self._err[0] = exc
                finally:
                    done_evt.set()

            try:
                threading.Thread(target=_bg, name=f"avi-origin-{seq_id}", daemon=True).start()
            except Exception:
                self._done = None
                self._spawn_failed = True

    def resolve(self, timeout_sec: float) -> ForegroundWindow:
        tw = self._trigger_win
        if self._spawn_failed:
            win_now = replace(tw, origin_drifted=True) if tw is not None else ForegroundWindow(origin_drifted=True)
        elif self._done is not None:
            finished = self._done.wait(timeout=timeout_sec)
            sampled = self._box[0] if finished and self._err[0] is None else None
            has_identity = sampled is not None and bool(
                sampled.hwnd > 0
                or sampled.process
                or sampled.bundle_id
                or sampled.title
                or sampled.surface_id
                or sampled.tty
                or sampled.tmux_pane
            )
            if not has_identity:
                win_now = replace(tw, origin_drifted=True) if tw is not None else ForegroundWindow(origin_drifted=True)
            else:
                assert sampled is not None
                win_now = sampled
        elif callable(self._resolver):
            win_now = self._resolver(tw)
        else:
            win_now = (
                tw
                if (tw and (tw.title or tw.process or tw.hwnd))
                else self._platform.get_foreground_window()
            )
        if tw is not None and not win_now.origin_drifted:
            conflict_fn = getattr(self._platform, "origin_resolution_conflicted", None)
            if callable(conflict_fn) and conflict_fn(tw, win_now):
                win_now = replace(tw, origin_drifted=True)
        return win_now


def classify_capture_error(
    exc: Exception, redact_fn: Callable[[Exception], str]
) -> tuple[str, str, str, int]:
    """Return ``(redacted_error, skipped_reason, hud_state, hud_duration_ms)`` for capture failures."""
    err = redact_fn(exc)
    is_mic_err = isinstance(exc, (NoMicrophoneError, MicrophoneUnavailableError))
    err_reason = str(getattr(exc, "reason_code", "no_mic")) if is_mic_err else ""
    err_hud_state = str(getattr(exc, "hud_state", "no_mic")) if is_mic_err else "error"
    err_hud_dur = 2200 if err_hud_state == "no_mic" else 0
    return err, err_reason, err_hud_state, err_hud_dur


def finalize_commit_slot_outcome(
    *,
    task: Any,
    st: _Stage1State,
    slot_exc: Exception | None,
    stage2_ref_res: RefinerResult | None,
    stop_evt: threading.Event,
    platform: Any,
    tracker: Any,
    env: Mapping[str, str],
    asr_provider: Any,
    refiner: Any,
    redact_fn: Callable[[Exception], str],
    summarize_fn: Callable[[PipelineResult], dict[str, Any]],
    stash_fn: Callable[[], str],
    deliver_fn: Callable[[Any, str, ForegroundWindow, Callable[[ForegroundWindow], bool] | None], PasteOutcome],
) -> tuple[dict[str, Any], str, int, str] | None:
    """Finalize one completed commit slot into ``(summary, hud_state, hud_duration_ms, beep_kind)``."""
    hud_st, hud_dur_ms, beep_k = "", 0, ""

    def _cancelled_outcome() -> tuple[dict[str, Any], str, int, str] | None:
        if stop_evt.is_set() or task.cancel_token.reason == "shutdown" or st.skipped_reason == "shutdown":
            return None
        saved_p = stash_fn()
        res_c = _make_pipeline_result(
            task.seq_id,
            task.audio_source,
            st,
            skipped_reason="cancelled",
            saved_audio=saved_p,
        )
        return summarize_fn(res_c), "", 0, ""

    if (
        stop_evt.is_set()
        or task.cancel_token.is_cancelled()
        or st.skipped_reason in ("cancelled", "shutdown")
    ):
        return _cancelled_outcome()

    if slot_exc is not None:
        if not task.cancel_token.try_commit():
            return _cancelled_outcome()
        saved_p = stash_fn()
        res_err = _make_pipeline_result(
            task.seq_id,
            task.audio_source,
            st,
            refined=stage2_ref_res,
            saved_audio=saved_p,
            error=redact_fn(slot_exc),
        )
        sum_err = summarize_fn(res_err)
        if not sum_err["asr_provider"]:
            sum_err["asr_provider"] = getattr(asr_provider, "provider_name", "")
        if not sum_err["refiner_provider"]:
            sum_err["refiner_provider"] = getattr(refiner, "provider_name", "")
        return sum_err, "error", 0, ("" if task.recorder_lost else "error")

    if st.skipped_reason:
        if not task.cancel_token.try_commit():
            return _cancelled_outcome()
        sr = st.skipped_reason
        saved_p = stash_fn() if sr == "empty_asr" else ""
        res_skip = _make_pipeline_result(
            task.seq_id,
            task.audio_source,
            st,
            skipped_reason=sr,
            saved_audio=saved_p,
        )
        sum_skip = summarize_fn(res_skip)
        eff_sr, hud_st, hud_dur_ms, beep_k = classify_skipped_commit_hud(
            sr, recorder_lost=task.recorder_lost, saved_audio=saved_p
        )
        sum_skip["skipped_reason"] = eff_sr
        return sum_skip, hud_st, hud_dur_ms, beep_k

    ref_res = stage2_ref_res
    if (
        stop_evt.is_set()
        or task.cancel_token.is_cancelled()
        or (ref_res is not None and ref_res.used_fallback in ("cancelled", "shutdown"))
    ):
        return _cancelled_outcome()

    assert st.asr_res is not None
    final_text = (ref_res.text.strip() if ref_res else "") or st.asr_res.text.strip()

    deliver_ms = 0
    paste_out: PasteOutcome | None = None
    if task.no_paste or platform is None:
        if not task.cancel_token.try_commit():
            return _cancelled_outcome()
    else:
        try:
            verifier = _make_desktop_same_window_verifier(st, tracker=tracker, env=env)
            t_deliv0 = time.monotonic()
            paste_out = deliver_fn(task, final_text, st.win, verifier)
            deliver_ms = _elapsed_ms(t_deliv0)
        except Exception as deliv_exc:
            if stop_evt.is_set() or task.cancel_token.is_cancelled():
                return _cancelled_outcome()
            if not task.cancel_token.try_commit():
                return _cancelled_outcome()
            deliver_ms = _elapsed_ms(t_deliv0)
            saved_p = stash_fn()
            res_deliv_err = _make_pipeline_result(
                task.seq_id,
                task.audio_source,
                st,
                ok=False,
                final_text=final_text,
                skipped_reason="delivery_error",
                refined=ref_res,
                paste=PasteOutcome(False, False, "delivery_error", st.win.process, False),
                deliver_ms=deliver_ms,
                saved_audio=saved_p,
                error=redact_fn(deliv_exc),
            )
            hud_st = "paste_failed_retry" if saved_p else "paste_failed"
            beep_k = "" if task.recorder_lost else "error"
            return summarize_fn(res_deliv_err), hud_st, hud_dur_ms, beep_k
        if paste_out.reason == "cancelled" or task.cancel_token.is_cancelled():
            return _cancelled_outcome()
        task.cancel_token.try_commit()

    delivery_failed = paste_out is not None and not paste_out.pasted and not paste_out.clipboard
    saved_p = stash_fn() if (delivery_failed or st.asr_missed_chunks > 0) else ""
    delivery_failed, eff_skip, hud_st, beep_k = classify_delivered_commit_hud(
        paste_out,
        asr_missed_chunks=st.asr_missed_chunks,
        recorder_lost=task.recorder_lost,
        saved_audio=saved_p,
    )
    res_ok = _make_pipeline_result(
        task.seq_id,
        task.audio_source,
        st,
        ok=(not delivery_failed),
        final_text=final_text,
        skipped_reason=eff_skip,
        refined=ref_res,
        paste=paste_out,
        deliver_ms=deliver_ms,
        saved_audio=saved_p,
    )
    return summarize_fn(res_ok), hud_st, hud_dur_ms, beep_k


def run_selftest_wav_request(
    req: Mapping[str, Any],
    *,
    wav_path: Path,
    origin_win: ForegroundWindow,
    deliver: bool,
    eff_deadline: float | None,
    stop_evt: threading.Event,
    cancel_token: CancelToken | None,
    peer_disconnected: Callable[[], bool] | None,
    cfg: Config,
    creds: ProviderCredentials,
    selftest_tracker: Any,
    asr_provider: BaseAsrProvider,
    refiner: BaseRefiner,
    platform: Any,
    runtime_dir: Path,
    env: Mapping[str, str],
    known_cwds_snap: Sequence[str],
    alloc_seq_fn: Callable[[bool], int],
    deliver_for_selftest_fn: Callable[..., PasteOutcome],
    build_summary_fn: Callable[[int, PipelineResult, bool], dict[str, Any]],
    record_telemetry_fn: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    """Execute ``selftest_wav`` with bounded cancellation/deadline wiring."""
    req_cancel = CancelToken()
    parent_hook_id = 0
    deadline_timer: threading.Timer | None = None
    if cancel_token is not None:
        parent_hook_id = cancel_token.register_abort(
            lambda: req_cancel.cancel(cancel_token.reason or "shutdown")
        )
    if eff_deadline is not None:
        rem_sec = eff_deadline - time.monotonic()
        if rem_sec <= 0.0:
            req_cancel.cancel("client_timeout")
        else:
            deadline_timer = threading.Timer(rem_sec, lambda: req_cancel.cancel("client_timeout"))
            deadline_timer.daemon = True
            deadline_timer.start()

    def _abort_reason() -> str:
        if stop_evt.is_set():
            req_cancel.cancel("shutdown")
            return "shutdown"
        if cancel_token is not None and cancel_token.is_cancelled():
            rsn = cancel_token.reason or "shutdown"
            req_cancel.cancel(rsn)
            return rsn
        if req_cancel.is_cancelled():
            return req_cancel.reason or "cancelled"
        if eff_deadline is not None and time.monotonic() >= eff_deadline:
            req_cancel.cancel("client_timeout")
            return req_cancel.reason or "client_timeout"
        if peer_disconnected is not None and peer_disconnected():
            req_cancel.cancel("client_disconnected")
            return req_cancel.reason or "client_disconnected"
        return ""

    try:
        if init_abort := _abort_reason():
            if init_abort == "shutdown":
                return {"ok": False, "error": "daemon stopping"}
            return {
                "ok": False,
                "skipped_reason": init_abort,
                "error": init_abort,
                "pasted": False,
                "clipboard": False,
            }
        no_telemetry = req.get("no_telemetry") is True
        cur_seq = alloc_seq_fn(no_telemetry)
        deliver_hook = (
            (lambda text, **kw: deliver_for_selftest_fn(text, cancel_token=req_cancel, **kw))
            if deliver
            else None
        )
        res = run_pipeline_once(
            wav_path,
            cfg=cfg,
            creds=creds,
            tracker=selftest_tracker,
            asr_provider=asr_provider,
            refiner=refiner,
            platform=platform,
            origin_window=origin_win,
            audio_source="selftest_wav",
            seq_id=cur_seq,
            dry_run=(not deliver),
            is_cancelled=lambda: bool(_abort_reason()),
            cancel_token=req_cancel,
            deliver_text_fn=deliver_hook,
            runtime_dir=runtime_dir,
            env=env,
            mode=str(req.get("mode") or "auto"),
            known_cwds_override=known_cwds_snap,
            isolate_refiner_session=True,
        )
        summary = build_summary_fn(cur_seq, res, not deliver)
        if not no_telemetry and not stop_evt.is_set():
            record_telemetry_fn(summary)
        return format_selftest_wav_response(cur_seq, res, summary)
    finally:
        if deadline_timer is not None:
            deadline_timer.cancel()
        if cancel_token is not None and parent_hook_id:
            cancel_token.unregister_abort(parent_hook_id)


def plan_commit_hud_calls(
    *,
    silent_hud: bool,
    rec_active: bool,
    recording_active: bool,
    stopping_capture: bool,
    cur_silent_hud: bool,
    main_hud_state: str,
    main_hud_visible: bool,
    main_hud_bg_count: int,
    next_vis_stage: str,
    vis_inflight: int,
    hud_st: str,
    copied_hud_text: str,
    hud_dur_ms: int,
    beep_k: str,
) -> tuple[list[tuple[str, str, int, str | None, bool | None, bool | None, int]], str]:
    """Compute HUD call tuples and beep kind for a finalized commit slot."""
    hud_calls: list[tuple[str, str, int, str | None, bool | None, bool | None, int]] = []
    play_beep = ""
    if silent_hud:
        if not rec_active and not next_vis_stage and main_hud_visible:
            hud_calls.append(("hide", "", 0, None, None, None, 0))
    elif rec_active:
        if (
            recording_active
            and not stopping_capture
            and not cur_silent_hud
            and main_hud_state == "recording"
            and vis_inflight != main_hud_bg_count
        ):
            hud_calls.append(("recording", "", 0, None, None, False, vis_inflight))
        if hud_st == "copied":
            hud_calls.append(("copied", copied_hud_text, hud_dur_ms, "toast", False, None, 0))
        elif hud_st and is_toast_hud_state(hud_st):
            hud_calls.append((hud_st, "", hud_dur_ms, None, False, None, 0))
    elif next_vis_stage:
        if main_hud_state != next_vis_stage:
            hud_calls.append((next_vis_stage, "", 0, None, None, None, 0))
        if hud_st == "copied":
            hud_calls.append(("copied", copied_hud_text, hud_dur_ms, "toast", False, None, 0))
        elif hud_st and is_toast_hud_state(hud_st):
            hud_calls.append((hud_st, "", hud_dur_ms, None, False, None, 0))
        play_beep = beep_k
    else:
        if hud_st:
            hud_calls.append(
                (
                    hud_st,
                    copied_hud_text if hud_st == "copied" else "",
                    hud_dur_ms,
                    None,
                    True if is_toast_hud_state(hud_st) else None,
                    None,
                    0,
                )
            )
        elif main_hud_visible:
            hud_calls.append(("hide", "", 0, None, None, None, 0))
        play_beep = beep_k
    return hud_calls, play_beep
