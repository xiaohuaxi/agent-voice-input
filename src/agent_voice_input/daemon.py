"""Core serial-pipelined two-stage state machine daemon and single-shot pipeline for agent-voice-input."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import concurrent.futures, fcntl, inspect, json, os, signal, socket, stat, sys, threading, time
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from . import __version__
from .app_scope import (
    classify_target_app_hint,
    classify_window_category,
    is_antigravity_ide_window,
    is_remote_client_window,
    match_app_patterns,
)
from .asr_providers import (
    AsrError,
    AsrResult,
    BaseAsrProvider,
    CancelToken,
    ChatAudioAsrProvider,
    GeminiAudioProvider,
    _sleep_cooperative,
    create_asr_provider,
)
from .audio_proc import (
    AudioGateResult,
    DEFAULT_MAX_CHUNK_SEC,
    SINGLE_HOP_MAX_CHUNK_SEC,
    inspect_wav_chunks,
    process_wav,
    save_failed_audio,
    spawn_recording_archive,
    wait_recording_archives,
)
from .config import (
    Config,
    ProviderCredentials,
    ensure_secure_dir,
    get_runtime_dir,
    redact_secrets,
    resolve_credentials,
    resolve_saved_recordings_dir,
    select_asr_provider,
)
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
from .refiner import BaseRefiner, RefinerResult, attach_single_hop_context, create_refiner
from .session_ctx import (
    SessionSnapshot,
    SessionTracker,
    parse_window_title_hint,
    resolve_antigravity_ide_session,
    resolve_ide_workspace_cwd,
)


TELEMETRY_FILENAME: str = "telemetry.jsonl"
TELEMETRY_MAX_BYTES: int = 256 * 1024
RECENT_RESULTS_MAXLEN: int = 20


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
        "paste_reason": res.paste.reason if res.paste else ("dry_run" if dry_run and res.ok else ""),
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


def append_telemetry_record(
    runtime_dir: Path,
    record: Mapping[str, Any],
    *,
    max_bytes: int = TELEMETRY_MAX_BYTES,
) -> None:
    """Append one pure-numeric JSON record to ``0600`` ``telemetry.jsonl`` with atomic >256KB tail truncation."""
    try:
        rt = ensure_secure_dir(runtime_dir)
        telemetry_path = rt / TELEMETRY_FILENAME
        lock_path = rt / ".telemetry.lock"
        line_bytes = (json.dumps(dict(record), ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        lfd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR | nofollow, 0o600)
        os.fchmod(lfd, 0o600)
        with os.fdopen(lfd, "r+", encoding="utf-8") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    st_pre = os.lstat(telemetry_path)
                    if not stat.S_ISREG(st_pre.st_mode):
                        return
                except FileNotFoundError:
                    pass
                afd = os.open(
                    str(telemetry_path),
                    os.O_CREAT | os.O_WRONLY | os.O_APPEND | nofollow,
                    0o600,
                )
                os.fchmod(afd, 0o600)
                with os.fdopen(afd, "wb") as af:
                    af.write(line_bytes)
                    af.flush()
                st = os.lstat(telemetry_path)
                if not stat.S_ISREG(st.st_mode) or st.st_size <= max_bytes:
                    return
                keep_target = max(1, max_bytes // 2)
                seek_pos = max(0, st.st_size - keep_target)
                with open(telemetry_path, "rb") as rf:
                    if seek_pos > 0:
                        rf.seek(seek_pos)
                    raw_tail = rf.read()
                if seek_pos > 0:
                    nl = raw_tail.find(b"\n")
                    if 0 <= nl < len(raw_tail) - 1:
                        tail = raw_tail[nl + 1 :]
                    else:
                        tail = line_bytes if len(line_bytes) <= max_bytes else b""
                else:
                    tail = raw_tail
                tmp_path = rt / f".{TELEMETRY_FILENAME}.trim.{os.getpid()}.{time.monotonic_ns()}.tmp"
                tfd = os.open(str(tmp_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY | nofollow, 0o600)
                closed_tfd = False
                try:
                    os.fchmod(tfd, 0o600)
                    tf_obj = os.fdopen(tfd, "wb")
                    closed_tfd = True
                    with tf_obj as tf:
                        tf.write(tail)
                        tf.flush()
                    os.replace(tmp_path, telemetry_path)
                except Exception:
                    if not closed_tfd:
                        try:
                            os.close(tfd)
                        except OSError:
                            pass
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except OSError:
                        pass
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass


def read_telemetry_history(
    runtime_dir: Path,
    limit: int = 10,
    *,
    max_scan_bytes: int = TELEMETRY_MAX_BYTES + 65536,
) -> list[dict[str, Any]]:
    """Read up to ``limit`` most recent JSONL telemetry records from ``runtime_dir / telemetry.jsonl``."""
    eff_limit = max(0, int(limit))
    if eff_limit <= 0:
        return []
    telemetry_path = runtime_dir / TELEMETRY_FILENAME
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(telemetry_path), os.O_RDONLY | nofollow)
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_size <= 0:
                return []
            seek_pos = max(0, st.st_size - max(4096, int(max_scan_bytes)))
            if seek_pos > 0:
                f.seek(seek_pos)
            raw = f.read()
        if seek_pos > 0:
            nl = raw.find(b"\n")
            if 0 <= nl < len(raw) - 1:
                raw = raw[nl + 1 :]
        out: list[dict[str, Any]] = []
        for raw_ln in raw.splitlines():
            ln = raw_ln.strip()
            if not ln:
                continue
            try:
                obj = json.loads(ln.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
        return out[-eff_limit:]
    except Exception:
        return []


def merge_recent_results(
    mem_items: Sequence[Mapping[str, Any]],
    disk_items: Sequence[Mapping[str, Any]],
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Merge persisted telemetry records with live in-memory ``recent_results`` without duplicates."""
    eff_limit = max(0, int(limit))
    if eff_limit <= 0:
        return []
    merged: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any]] = set()
    for rec in disk_items:
        d = dict(rec)
        key = (d.get("ts"), d.get("seq_id"))
        if d.get("ts") is not None:
            seen.add(key)
        merged.append(d)
    for rec in mem_items:
        d = dict(rec)
        key = (d.get("ts"), d.get("seq_id"))
        if d.get("ts") is not None and key in seen:
            continue
        if d.get("ts") is not None:
            seen.add(key)
        merged.append(d)
    return merged[-eff_limit:]


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
        document=str(m.get("document") or ""),
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
    before_commit: Callable[[], bool] | None = None,
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
    if before_commit is not None and not _accepts("before_commit"):
        if not before_commit():
            return PasteOutcome(False, False, "cancelled", "")

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
    if before_commit is not None and _accepts("before_commit"):
        kw["before_commit"] = before_commit
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


MAX_ASR_CHUNK_CONCURRENCY: int = 5
MAX_PENDING_UTTERANCES: int = 8
MAX_STAGE2_CONCURRENCY: int = 2
DEFAULT_PASTE_SETTLE_SEC: float = 0.15


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


def _call_asr_transcribe(
    eff_asr: BaseAsrProvider,
    wav_bytes: bytes,
    *,
    context: Any,
    is_cancelled: Callable[[], bool] | None,
    cancel_token: CancelToken | None = None,
) -> AsrResult:
    try:
        params = inspect.signature(eff_asr.transcribe).parameters
    except (TypeError, ValueError):
        params = {}
    has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    kw: dict[str, Any] = {"context": context}
    if is_cancelled is not None and ("is_cancelled" in params or has_var_kw):
        kw["is_cancelled"] = is_cancelled
    if cancel_token is not None and ("cancel_token" in params or has_var_kw):
        kw["cancel_token"] = cancel_token
    return eff_asr.transcribe(wav_bytes, **kw)


def _call_refiner_refine(
    eff_refiner: BaseRefiner,
    asr_res: AsrResult,
    *,
    session_key: str,
    tracker: SessionTracker,
    fallback_context: str,
    is_cancelled: Callable[[], bool] | None,
    cancel_token: CancelToken | None = None,
    mode: str = "agent",
    workspace_cwd: str = "",
    target_hint: str = "",
) -> RefinerResult:
    try:
        params = inspect.signature(eff_refiner.refine).parameters
    except (TypeError, ValueError):
        params = {}
    has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    kw: dict[str, Any] = {
        "session_key": session_key,
        "tracker": tracker,
        "fallback_context": fallback_context,
    }
    if is_cancelled is not None and ("is_cancelled" in params or has_var_kw):
        kw["is_cancelled"] = is_cancelled
    if cancel_token is not None and ("cancel_token" in params or has_var_kw):
        kw["cancel_token"] = cancel_token
    if "mode" in params or (has_var_kw and mode != "agent"):
        kw["mode"] = mode
    if workspace_cwd and ("workspace_cwd" in params or has_var_kw):
        kw["workspace_cwd"] = workspace_cwd
    if target_hint and ("target_hint" in params or has_var_kw):
        kw["target_hint"] = target_hint
    return eff_refiner.refine(asr_res, **kw)


def _is_single_hop_asr_provider(eff_asr: BaseAsrProvider) -> bool:
    return bool(
        isinstance(eff_asr, (GeminiAudioProvider, ChatAudioAsrProvider))
        or getattr(eff_asr, "single_hop", False)
    )


def _transcribe_wav_chunks(
    wav_bytes: bytes,
    *,
    eff_asr: BaseAsrProvider,
    asr_ctx: Any,
    floor_dbfs: float,
    max_chunk_sec: float | None = None,
    seq_id: int = 1,
    is_current_seq: Callable[[int], bool] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    cancel_token: CancelToken | None = None,
) -> tuple[AsrResult, int, tuple[float, ...]]:
    cancel_check = _make_cancel_check(seq_id, is_current_seq, is_cancelled, cancel_token)
    eff_max_chunk_sec = (
        (SINGLE_HOP_MAX_CHUNK_SEC if _is_single_hop_asr_provider(eff_asr) else DEFAULT_MAX_CHUNK_SEC)
        if max_chunk_sec is None
        else float(max_chunk_sec)
    )
    infos = inspect_wav_chunks(wav_bytes, max_chunk_sec=eff_max_chunk_sec, floor_dbfs=floor_dbfs)
    if len(infos) <= 1:
        if cancel_check is not None and cancel_check():
            return AsrResult(text="", provider=getattr(eff_asr, "provider_name", ""), model="", latency_ms=0), 1, ()
        return (
            _call_asr_transcribe(
                eff_asr, wav_bytes, context=asr_ctx, is_cancelled=cancel_check, cancel_token=cancel_token
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
            return _call_asr_transcribe(
                eff_asr,
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
        ),
        len(infos),
        chunk_secs,
    )


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
    checker = getattr(platform, "is_allowed_terminal", None)
    is_term = (
        (lambda w: bool(checker(w)))
        if checker is not None
        else (
            lambda w: is_windows_terminal_process(w.process)
            or is_macos_terminal_window(w.process, w.bundle_id)
        )
    )
    cat = classify_window_category(win, is_terminal=is_term)
    if cat == "terminal":
        return True, "agent"
    if cat == "coding_gui":
        # v0.8-3: MUST skip sync_session so WSL2 /proc or macOS fallback never injects an
        # unrelated background CLI repo's 200K session into VS Code / Cursor / Antigravity IDE!
        return False, "agent"
    return False, "general"


def _parse_optional_seq_id(raw: Any) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise ValueError(f"seq_id must be a positive integer, got {raw!r}")
    return raw


@dataclass(slots=True)
class _Stage1State:
    t_total0: float
    queue_wait_ms: int = 0
    gate: AudioGateResult | None = None
    gate_ms: int = 0
    switched_fallback: bool = False
    win: ForegroundWindow = field(default_factory=ForegroundWindow)
    eff_mode: str = "agent"
    snap: SessionSnapshot | None = None
    workspace_cwd: str = ""
    target_hint: str = ""
    asr_ctx_dialog: str = ""
    sync_ms: int = 0
    prep_ms: int = 0
    asr_res: AsrResult | None = None
    asr_chunks: int = 1
    asr_chunk_secs: tuple[float, ...] = ()
    skipped_reason: str = ""


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
    )


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

    t_gate0 = time.monotonic()
    gate = process_wav(raw_wav, cfg.audio)
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

    if cfg.audio.save_recordings and audio_source not in ("retry", "selftest_wav") and archive_sink is not None:
        try:
            arch_bytes = (
                bytes(raw_wav)
                if isinstance(raw_wav, (bytes, bytearray, memoryview))
                else (Path(raw_wav).read_bytes() or gate.wav_bytes)
            )
            if (
                arch_th := spawn_recording_archive(
                    arch_bytes,
                    resolve_saved_recordings_dir(env),
                    max_count=cfg.audio.save_recordings_max_count,
                )
            ) is not None:
                archive_sink(arch_th)
        except Exception:
            pass

    t_sync0 = time.monotonic()
    win = origin_window or (platform.get_foreground_window() if platform else ForegroundWindow())
    st.win = win
    should_sync, eff_mode = _resolve_pipeline_window_routing(
        win,
        platform=platform,
        origin_window=origin_window,
        window_title_override=window_title_override,
        audio_source=audio_source,
        explicit_mode=explicit_mode,
    )
    st.eff_mode = eff_mode
    extra_hw: list[str] = []
    if should_sync:
        st.snap = (
            tracker.sync_session(
                window_title=win.title,
                window_tty=win.tty,
                window_cwd=win.cwd,
                window_pid=win.pid,
            )
            if window_title_override is None
            else tracker.sync_session(window_title=window_title_override)
        )
    elif eff_mode == "agent" and (win.process or win.bundle_id):
        st.workspace_cwd = resolve_ide_workspace_cwd(
            win,
            home=tracker.home,
            known_cwds=tracker.active_session_cwds(),
            env=env,
        )
        if st.workspace_cwd and is_antigravity_ide_window(win):
            ide_match = resolve_antigravity_ide_session(
                st.workspace_cwd, home=tracker.home, env=env
            )
            if ide_match is not None:
                st.snap = tracker.sync_ide_session(ide_match)
        if st.snap is None and st.workspace_cwd:
            extra_hw = tracker.build_workspace_hotwords(st.workspace_cwd)
    elif eff_mode == "general" and (win.process or win.bundle_id):
        st.target_hint = classify_target_app_hint(win)

    asr_ctx = tracker.build_asr_context(st.snap, extra_hotwords=extra_hw, mode=eff_mode)
    if st.target_hint:
        asr_ctx.target_hint = st.target_hint
    st.asr_ctx_dialog = asr_ctx.dialog_text
    if _is_single_hop_asr_provider(eff_asr):
        asr_ctx = attach_single_hop_context(
            cfg,
            asr_ctx,
            tracker=tracker if (st.snap is not None or st.workspace_cwd) else None,
            session_key=st.snap.session_key if st.snap else "",
            mode=eff_mode,
            workspace_cwd=st.workspace_cwd,
            target_hint=st.target_hint,
        )
    st.sync_ms = _elapsed_ms(t_sync0)

    if (
        not (cancel_check is not None and cancel_check())
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
            max_chunk_sec=max_chunk_sec,
            seq_id=seq_id,
            is_current_seq=is_current_seq,
            is_cancelled=is_cancelled,
            cancel_token=cancel_token,
        )
    except Exception as asr_exc:
        if c_rsn := cancel_reason_fn():
            if c_rsn in ("cancelled", "shutdown") or (
                isinstance(asr_exc, AsrError) and "cancelled" in str(asr_exc).lower()
            ):
                st.skipped_reason = c_rsn
                return
        raise
    st.asr_res = asr_res
    st.asr_chunks = chunk_count
    st.asr_chunk_secs = chunk_secs
    if not asr_res.text.strip():
        st.skipped_reason = cancel_reason_fn() or "empty_asr"
    elif c_rsn := cancel_reason_fn():
        st.skipped_reason = c_rsn


def run_pipeline_once(
    raw_wav: bytes | str | Path, *, cfg: Config, creds: ProviderCredentials | None = None,
    tracker: SessionTracker | None = None, asr_provider: BaseAsrProvider | None = None,
    refiner: BaseRefiner | None = None, platform: Any = None, origin_window: ForegroundWindow | None = None,
    window_title_override: str | None = None,
    audio_source: str = "file", seq_id: int = 1, dry_run: bool = False,
    is_current_seq: Callable[[int], bool] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    cancel_token: CancelToken | None = None,
    deliver_fn: Callable[[str, ForegroundWindow], PasteOutcome] | None = None,
    on_stage: Callable[[str], None] | None = None,
    runtime_dir: Path | None = None,
    max_chunk_sec: float | None = None,
    env: Mapping[str, str] | None = None,
    archive_sink: Callable[[threading.Thread], None] | None = None,
    mode: str = "auto",
    queue_wait_ms: int = 0,
) -> PipelineResult:
    eff_env = os.environ if env is None else env
    eff_creds = creds or resolve_credentials(cfg, eff_env)
    eff_tracker = tracker or SessionTracker(env=eff_env)
    eff_asr = asr_provider or create_asr_provider(cfg, eff_creds, env=eff_env)
    owns_refiner = refiner is None
    eff_refiner = refiner or create_refiner(cfg, eff_creds, env=eff_env, runtime_dir=runtime_dir)
    can_save_failed = runtime_dir is not None and audio_source not in ("file", "selftest_wav", "retry")
    local_archive_threads: list[threading.Thread] = []
    stashed_once = False
    stashed_path = ""
    cancel_check = _make_cancel_check(seq_id, is_current_seq, is_cancelled, cancel_token)
    st = _Stage1State(t_total0=time.monotonic(), queue_wait_ms=queue_wait_ms, win=origin_window or ForegroundWindow())

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
        out_txt = "" if reason in ("cancelled", "shutdown") else draft
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
            )
            if st.skipped_reason:
                sr = st.skipped_reason
                if sr in ("cancelled", "shutdown", "superseded"):
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
            ref_res = _call_refiner_refine(
                eff_refiner,
                st.asr_res,
                session_key=st.snap.session_key if st.snap else "",
                tracker=eff_tracker,
                fallback_context=st.asr_ctx_dialog,
                is_cancelled=cancel_check,
                cancel_token=cancel_token,
                mode=st.eff_mode,
                workspace_cwd=st.workspace_cwd,
                target_hint=st.target_hint,
            )
            cand_text = (ref_res.text.strip() if ref_res else "") or st.asr_res.text.strip()
            if (c_rsn := _cancel_reason()) or (ref_res and ref_res.used_fallback in ("cancelled", "superseded", "shutdown")):
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
                t_deliv0 = time.monotonic()
                paste_out = deliver_fn(cand_text, st.win)
                deliver_ms = _elapsed_ms(t_deliv0)
            else:
                t_deliv0 = time.monotonic()
                paste_out = _deliver_with_origin(
                    platform,
                    cand_text,
                    seq_id=seq_id,
                    origin_window=st.win,
                    is_cancelled=cancel_check,
                    before_commit=(cancel_token.try_commit if cancel_token is not None else None),
                )
                deliver_ms = _elapsed_ms(t_deliv0)

            if paste_out is not None and paste_out.reason == "cancelled":
                return _cancel_res(_cancel_reason() or "cancelled", draft=cand_text, refined=ref_res, deliver_ms=deliver_ms)
            if cancel_token is not None:
                cancel_token.try_commit()
            saved_p = _stash_failed() if (paste_out is not None and not paste_out.pasted and not paste_out.clipboard) else ""
            return _make_pipeline_result(
                seq_id,
                audio_source,
                st,
                ok=True,
                final_text=cand_text,
                refined=ref_res,
                paste=paste_out,
                deliver_ms=deliver_ms,
                saved_audio=saved_p,
            )
        except Exception as exc:
            if (c_rsn := _cancel_reason()) in ("cancelled", "shutdown"):
                return _cancel_res(c_rsn)
            if saved_p := _stash_failed():
                try:
                    setattr(exc, "_saved_audio_path", saved_p)
                except Exception:
                    pass
            raise
    finally:
        if owns_refiner:
            eff_refiner.close()
        if local_archive_threads:
            wait_recording_archives(local_archive_threads, timeout_sec=2.0)


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
        # Strict global lock hierarchy: _trigger_lock (T) -> _hud_lock (H) -> _lock / _queue_cv (L) -> Leaf locks
        self._trigger_lock = threading.Lock()
        self._hud_lock = threading.Lock()
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._queue_cv = threading.Condition(self._lock)
        self.state, self.seq_id, self._last_trigger_ts = "idle", 0, -1e9
        self._recording_active: bool = False
        self._recording_seq_id: int = -1
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
        self._origin_win, self.last_result, self._srv_sock = ForegroundWindow(), {}, None
        self.recent_results: deque[dict[str, Any]] = deque(maxlen=RECENT_RESULTS_MAXLEN)
        self._cur_no_paste, self._cur_silent_hud = False, False
        self._rec_recovery_count, self._rec_recovery_gap_ms = 0, 0
        self._rec_hud_ready_seq, self._pending_recovery_toast_seq = -1, -1
        self._recorder_lost_seq, self._recorder_lost_ts, self._recorder_lost_stop_absorbed = -1, -1e9, False
        self._auto_stop_seq, self._auto_stop_ts, self._auto_stop_absorbed = -1, -1e9, False
        self._max_dur_timer: threading.Timer | None = None
        self._archive_threads: set[threading.Thread] = set()
        if (setter := getattr(self.platform, "set_recorder_lost_callback", None)) is not None:
            setter(self._on_recorder_lost)
        if (rec_setter := getattr(self.platform, "set_recorder_recovered_callback", None)) is not None:
            rec_setter(self._on_recorder_recovered)

    @property
    def pid_path(self) -> Path: return self.runtime_dir / "daemon.pid"
    @property
    def sock_path(self) -> Path: return self.runtime_dir / "control.sock"
    @property
    def telemetry_path(self) -> Path: return self.runtime_dir / TELEMETRY_FILENAME

    def _record_result_locked(self, summary: dict[str, Any]) -> dict[str, Any]:
        summary.setdefault("ts", round(time.time(), 6))
        self.last_result = summary
        snap = dict(summary)
        self.recent_results.append(snap)
        return snap

    def _append_telemetry(self, summary: Mapping[str, Any]) -> None:
        append_telemetry_record(self.runtime_dir, summary)

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

    def _invoke_platform_hud_unlocked(
        self,
        state: str,
        *,
        duration_ms: int = 0,
        layer: str | None = None,
        clear_main: bool | None = None,
        clear_toast: bool | None = None,
    ) -> None:
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

    def _hud(
        self,
        state: str,
        *,
        duration_ms: int = 0,
        layer: str | None = None,
        clear_main: bool | None = None,
        clear_toast: bool | None = None,
    ) -> None:
        with self._hud_lock:
            with self._lock:
                self._update_main_hud_visibility_locked(state, layer=layer, clear_main=clear_main)
            self._invoke_platform_hud_unlocked(
                state,
                duration_ms=duration_ms,
                layer=layer,
                clear_main=clear_main,
                clear_toast=clear_toast,
            )

    def _maybe_emit_bg_main_hud(self, desired_stage: str) -> None:
        if self._stop_evt.is_set():
            return
        with self._hud_lock:
            emit = False
            with self._lock:
                if (
                    not self._stop_evt.is_set()
                    and not self._recording_active
                    and not self._stopping_capture
                ):
                    cur_st = self._recompute_state_locked()
                    vis_st = self._derive_bg_stage_locked(visible_only=True)
                    if cur_st == desired_stage and vis_st == desired_stage and self._main_hud_state != desired_stage:
                        self._update_main_hud_visibility_locked(desired_stage)
                        emit = True
            if emit:
                self._invoke_platform_hud_unlocked(desired_stage)

    def _emit_recording_cancel_hud(self, *, show_cancelled_toast: bool) -> str:
        with self._hud_lock:
            calls: list[tuple[str, str | None, bool | None, bool | None]] = []
            with self._lock:
                cur_state = self._recompute_state_locked()
                vis_bg = self._derive_bg_stage_locked(visible_only=True)
                rec_busy = self._recording_active or self._stopping_capture
                if not self._stop_evt.is_set() and not rec_busy:
                    if show_cancelled_toast:
                        if vis_bg and cur_state == vis_bg:
                            if self._main_hud_state != vis_bg:
                                calls.append((vis_bg, None, None, True))
                            calls.append(("cancelled", None, False, None))
                        else:
                            calls.append(("cancelled", None, True, None))
                    else:
                        calls.append(((vis_bg if (vis_bg and cur_state == vis_bg) else "hide"), None, None, True))
                    for st_n, lyr_n, clr_m_n, _ in calls:
                        self._update_main_hud_visibility_locked(st_n, layer=lyr_n, clear_main=clr_m_n)
            for st_n, lyr_n, clr_m_n, clr_t_n in calls:
                self._invoke_platform_hud_unlocked(
                    st_n,
                    layer=lyr_n,
                    clear_main=clr_m_n,
                    clear_toast=clr_t_n,
                )
            return cur_state

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
                        perm_snap = self._record_result_locked(
                            {
                                "seq_id": cur_seq,
                                "ok": False,
                                "skipped_reason": "mic_permission",
                                "audio_source": src_used,
                                "error": perm_err,
                            }
                        )
                    self._append_telemetry(perm_snap)
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
                    lost_snap = self._record_result_locked(lr_lost)
                self._append_telemetry(lost_snap)
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
                enqueued_ts=self.clock_fn(),
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
                    resume_state = self._emit_recording_cancel_hud(show_cancelled_toast=False)
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
                        lr_snap = self._record_result_locked(lr)
                    self._append_telemetry(lr_snap)
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
                    lr_stop_snap = self._record_result_locked(lr_stop)
                self._append_telemetry(lr_stop_snap)
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
                enqueued_ts=self.clock_fn(),
            )
            with self._queue_cv:
                self._stopping_capture = False
                self._enqueue_utterance_locked(task)
                out_state = self._recompute_state_locked()
            return {"ok": True, "state": out_state, "seq_id": cur_seq}

    def handle_cancel(
        self,
        *,
        seq_id: int | None = None,
        silent_hud: bool = False,
        only_inflight: bool = False,
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
                    self._recording_active = False
                    self._recording_seq_id = -1
                    self._cur_no_paste, self._cur_silent_hud = False, False
                    self._rec_recovery_count, self._rec_recovery_gap_ms = 0, 0
                    self._rec_hud_ready_seq, self._pending_recovery_toast_seq = -1, -1
                    self.seq_id += 1
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
                if not eff_rec_silent:
                    resume_state = self._emit_recording_cancel_hud(show_cancelled_toast=True)
                self.platform.cancel_recording()
                if not eff_rec_silent:
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
                rec_now = self._recording_active or self._stopping_capture
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

            if not silent_hud and any_visible_cancelled:
                if not rec_now and resume_state == "idle":
                    self._hud("cancelled", clear_main=True)
                else:
                    if not rec_now and resume_state in ("recognizing", "refining"):
                        self._maybe_emit_bg_main_hud(resume_state)
                    self._hud("cancelled", clear_main=False)

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
            is_cancelled=lambda: self._stop_evt.is_set() or task.cancel_token.is_cancelled(),
            before_clipboard_write=lambda: self._wait_clipboard_settle(task.cancel_token),
            before_commit=task.cancel_token.try_commit,
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
            task.stashed_done = True
            if (
                (self._stop_evt.is_set() and not allow_during_shutdown)
                or task.cancel_token.reason == "shutdown"
                or task.audio_source in ("file", "selftest_wav", "retry")
            ):
                return ""
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
                slot.stage2_ref_res = _call_refiner_refine(
                    self.refiner,
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
            slot.stage2_ref_res = _call_refiner_refine(
                self.refiner,
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
        task = slot.task
        st = slot.st
        hud_st, hud_dur_ms, beep_k = "", 0, ""

        def _summarize(res: PipelineResult) -> dict[str, Any]:
            return self._build_summary(
                task.seq_id,
                res,
                dry_run=task.no_paste,
                recorder_lost=task.recorder_lost,
                recovery_count=task.recovery_count,
                recovery_gap_ms=task.recovery_gap_ms,
                audio_error=task.audio_error,
            )

        def _stash_slot_wav() -> str:
            return self._stash_task_wav_once(
                task, gate=st.gate, snap=st.snap, win=st.win, workspace_cwd=st.workspace_cwd
            )

        def _cancelled_outcome() -> tuple[dict[str, Any], str, int, str] | None:
            if self._stop_evt.is_set() or task.cancel_token.reason == "shutdown" or st.skipped_reason == "shutdown":
                return None
            saved_p = _stash_slot_wav()
            res_c = _make_pipeline_result(
                task.seq_id,
                task.audio_source,
                st,
                skipped_reason="cancelled",
                saved_audio=saved_p,
            )
            return _summarize(res_c), "", 0, ""

        if (
            self._stop_evt.is_set()
            or task.cancel_token.is_cancelled()
            or st.skipped_reason in ("cancelled", "shutdown")
        ):
            return _cancelled_outcome()

        if slot.exc is not None:
            if not task.cancel_token.try_commit():
                return _cancelled_outcome()
            saved_p = _stash_slot_wav()
            res_err = _make_pipeline_result(
                task.seq_id,
                task.audio_source,
                st,
                refined=slot.stage2_ref_res,
                saved_audio=saved_p,
                error=self._redact(slot.exc),
            )
            sum_err = _summarize(res_err)
            if not sum_err["asr_provider"]:
                sum_err["asr_provider"] = getattr(self.asr_provider, "provider_name", "")
            if not sum_err["refiner_provider"]:
                sum_err["refiner_provider"] = getattr(self.refiner, "provider_name", "")
            return sum_err, "error", 0, ("" if task.recorder_lost else "error")

        if st.skipped_reason:
            if not task.cancel_token.try_commit():
                return _cancelled_outcome()
            sr = st.skipped_reason
            saved_p = _stash_slot_wav() if sr == "empty_asr" else ""
            res_skip = _make_pipeline_result(
                task.seq_id,
                task.audio_source,
                st,
                skipped_reason=sr,
                saved_audio=saved_p,
            )
            sum_skip = _summarize(res_skip)
            if sr in ("too_short", "silence", "empty_audio", "empty_asr"):
                if task.recorder_lost:
                    hud_st, hud_dur_ms, beep_k = "mic_disconnected", 2200, ""
                    sum_skip["skipped_reason"] = "mic_disconnected"
                else:
                    hud_st, beep_k = "empty", "error"
            elif sr != "superseded":
                hud_st, beep_k = "error", "error"
            if task.recorder_lost and beep_k == "error":
                beep_k = ""
            return sum_skip, hud_st, hud_dur_ms, beep_k

        ref_res = slot.stage2_ref_res
        if (
            self._stop_evt.is_set()
            or task.cancel_token.is_cancelled()
            or (ref_res is not None and ref_res.used_fallback in ("cancelled", "shutdown"))
        ):
            return _cancelled_outcome()

        assert st.asr_res is not None
        final_text = (ref_res.text.strip() if ref_res else "") or st.asr_res.text.strip()

        deliver_ms = 0
        paste_out: PasteOutcome | None = None
        if task.no_paste or self.platform is None:
            if not task.cancel_token.try_commit():
                return _cancelled_outcome()
        else:
            try:
                t_deliv0 = time.monotonic()
                paste_out = self._deliver_for_utterance(task, final_text, st.win)
                deliver_ms = _elapsed_ms(t_deliv0)
            except Exception as deliv_exc:
                if self._stop_evt.is_set() or task.cancel_token.is_cancelled():
                    return _cancelled_outcome()
                slot.exc = deliv_exc
                return self._finalize_commit_slot(slot)
            if paste_out.reason == "cancelled" or task.cancel_token.is_cancelled():
                return _cancelled_outcome()
            task.cancel_token.try_commit()

        saved_p = (
            _stash_slot_wav()
            if (paste_out is not None and not paste_out.pasted and not paste_out.clipboard)
            else ""
        )
        res_ok = _make_pipeline_result(
            task.seq_id,
            task.audio_source,
            st,
            ok=True,
            final_text=final_text,
            refined=ref_res,
            paste=paste_out,
            deliver_ms=deliver_ms,
            saved_audio=saved_p,
        )
        if paste_out and not paste_out.pasted and not paste_out.clipboard:
            hud_st, beep_k = "error", ("" if task.recorder_lost else "error")
        elif paste_out and not paste_out.pasted and paste_out.clipboard:
            hud_st = "copied"
        else:
            hud_st = "done"
        return _summarize(res_ok), hud_st, hud_dur_ms, beep_k

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

            task = slot.task
            play_beep = ""
            with self._hud_lock:
                hud_calls: list[tuple[str, int, str | None, bool | None]] = []
                with self._queue_cv:
                    if self._commit_queue and self._commit_queue[0] is slot:
                        self._commit_queue.popleft()
                    has_more_active = self._inflight_or_queued_count_locked() > 0
                    next_vis_stage = self._derive_bg_stage_locked(visible_only=True)
                    rec_active = self._recording_active or self._stopping_capture
                    new_state = self._recompute_state_locked()
                    self._record_result_locked(summary)
                    self._queue_cv.notify_all()
                    if not self._stop_evt.is_set():
                        if task.silent_hud:
                            if not rec_active and not has_more_active and self._main_hud_visible:
                                hud_calls.append(("hide", 0, None, None))
                        elif rec_active:
                            if hud_st == "copied":
                                hud_calls.append(("copied", hud_dur_ms, "toast", False))
                            elif hud_st and is_toast_hud_state(hud_st):
                                hud_calls.append((hud_st, hud_dur_ms, None, False))
                        elif next_vis_stage:
                            if new_state == next_vis_stage and self._main_hud_state != next_vis_stage:
                                hud_calls.append((next_vis_stage, 0, None, None))
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
                            elif not has_more_active and self._main_hud_visible:
                                hud_calls.append(("hide", 0, None, None))
                            play_beep = beep_k
                        for st_n, _, lyr_n, clr_m_n in hud_calls:
                            self._update_main_hud_visibility_locked(st_n, layer=lyr_n, clear_main=clr_m_n)
                for st_name, dur_val, lyr_val, clr_main_val in hud_calls:
                    self._invoke_platform_hud_unlocked(
                        st_name,
                        duration_ms=dur_val,
                        layer=lyr_val,
                        clear_main=clr_main_val,
                    )
            if play_beep:
                self.platform.beep(play_beep)

    def _initiate_shutdown(self) -> list[threading.Thread]:
        self._cancel_max_duration_timer()
        tokens_to_abort: list[CancelToken] = []
        cancelled_to_stash: list[tuple[_QueuedUtterance, AudioGateResult | None, SessionSnapshot | None, ForegroundWindow]] = []
        bg_threads: list[threading.Thread] = []
        seen_ids: set[int] = set()

        def _collect(task: _QueuedUtterance, slot: _CommitSlot | None = None) -> None:
            if task.seq_id in seen_ids:
                return
            seen_ids.add(task.seq_id)
            tokens_to_abort.append(task.cancel_token)
            if (
                task.cancel_token.is_cancelled()
                and task.cancel_token.reason == "cancelled"
                and not task.stashed_done
            ):
                cancelled_to_stash.append(
                    (
                        task,
                        slot.st.gate if slot is not None else None,
                        slot.st.snap if slot is not None else None,
                        slot.st.win if slot is not None else task.origin_win,
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
            if self._worker_thread is not None:
                bg_threads.append(self._worker_thread)
            if self._commit_thread is not None:
                bg_threads.append(self._commit_thread)
            bg_threads.extend(th for th in self._stage2_threads if th.is_alive())
            self._queue_cv.notify_all()

        CancelToken.cancel_many(tokens_to_abort, "shutdown")
        for task_c, gate_c, snap_c, win_c in cancelled_to_stash:
            self._stash_task_wav_once(
                task_c,
                gate=gate_c,
                snap=snap_c,
                win=win_c,
                allow_during_shutdown=True,
            )
        return bg_threads

    def handle_control(self, req: Mapping[str, Any]) -> dict[str, Any]:
        cmd = str(req.get("cmd") or "status")
        if cmd == "status":
            shim_ok = not bool(getattr(self.platform, "_shim_disabled", False))
            raw_shim_err = str(getattr(self.platform, "last_shim_error", "") or "")
            shim_err = self._redact_str(raw_shim_err) if raw_shim_err else ""
            raw_hist = req.get("history")
            want_hist = raw_hist is not None and not isinstance(raw_hist, bool)
            hist_limit = max(0, min(500, int(raw_hist))) if want_hist else 0
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
                mem_snap = [dict(r) for r in self.recent_results] if want_hist else []
            if want_hist:
                disk_items = read_telemetry_history(
                    self.runtime_dir, limit=max(hist_limit, RECENT_RESULTS_MAXLEN)
                )
                merged = merge_recent_results(mem_snap, disk_items, limit=hist_limit)
                resp["recent_results"] = merged
                if not resp["last_result"] and merged:
                    resp["last_result"] = dict(merged[-1])
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
                        "document",
                    )
                )
                else None
            )
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
                st_snap = self._record_result_locked(summary)
            self._append_telemetry(st_snap)
            return {"ok": res.ok, "seq_id": cur_seq, "skipped_reason": res.skipped_reason, "asr_text": res.asr.text if res.asr else "", "final_text": res.final_text, "summary": summary}
        if cmd == "shutdown":
            self._initiate_shutdown()
            return {"ok": True, "stopping": True}
        return {"ok": False, "error": f"unknown cmd: {cmd}"}

    def _on_platform_event(self, m: Mapping[str, Any]) -> None:
        ev = str(m.get("event") or "")
        if ev in ("toggle", "ptt_start", "ptt_stop", "ptt_cancel"):
            self.handle_trigger(ev, _window_from_mapping(m), from_hotkey=True)
        elif ev == "hold_cancel":
            win = _window_from_mapping(m)
            if (win.process or win.bundle_id) and self._should_yield_hotkey(win) is not None:
                return
            self.handle_cancel(only_inflight=True)
        elif ev == "cancel":
            try:
                seq_val = _parse_optional_seq_id(m.get("seq_id"))
            except ValueError:
                return
            self.handle_cancel(seq_id=seq_val)

    def serve_forever(self) -> None:
        self.sock_path.unlink(missing_ok=True)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv_sock = srv
        sig_stop = threading.Event()
        is_main = threading.current_thread() is threading.main_thread()
        prev_int = signal.signal(signal.SIGINT, lambda *_: sig_stop.set()) if is_main else None
        prev_term = signal.signal(signal.SIGTERM, lambda *_: sig_stop.set()) if is_main else None
        try:
            self.pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8"); os.chmod(self.pid_path, 0o600)
            self.platform.start_event_listener(self._on_platform_event)
            srv.bind(str(self.sock_path)); os.chmod(self.sock_path, 0o600); srv.listen(8); srv.settimeout(0.3)
            while not self._stop_evt.is_set() and not sig_stop.is_set():
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
        bg_threads = self._initiate_shutdown()
        if self._srv_sock is not None:
            try: self._srv_sock.close()
            except OSError: pass
            self._srv_sock = None
        self.sock_path.unlink(missing_ok=True); self.pid_path.unlink(missing_ok=True)
        self.refiner.close()
        join_deadline = time.monotonic() + 3.0
        cur_th = threading.current_thread()
        for th in bg_threads:
            if (rem := join_deadline - time.monotonic()) <= 0:
                break
            if th.is_alive() and th is not cur_th:
                th.join(timeout=rem)
        self.wait_pending_archives(timeout_sec=2.0)
        self.platform.close()

    stop = close
