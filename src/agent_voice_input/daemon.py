"""Core serial-pipelined two-stage state machine daemon and single-shot pipeline for agent-voice-input."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
import concurrent.futures, copy, errno, fcntl, hashlib, inspect, json, math, os, signal, socket, stat, sys, threading, time
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from . import __version__
from ._remote_probe import _is_broad_or_unsafe_workspace_dir
from .app_scope import (
    classify_target_app_hint,
    classify_window_category,
    is_antigravity_ide_window,
    is_remote_client_window,
    is_remote_ide_window,
    match_app_patterns,
    resolve_hotwords_scope,
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
    FAILED_AUDIO_MAX_AGE_SEC,
    SINGLE_HOP_MAX_CHUNK_SEC,
    get_failed_audio_dir,
    inspect_wav_chunks,
    process_wav,
    prune_failed_audio_dir,
    save_failed_audio,
    spawn_recording_archive,
    wait_recording_archives,
)
from .config import (
    AudioConfig,
    Config,
    ProviderCredentials,
    ensure_secure_dir,
    explain_pipeline_selection,
    get_runtime_dir,
    redact_secrets,
    resolve_credentials,
    resolve_saved_recordings_dir,
    select_asr_provider,
    select_refiner_provider,
)
from .hotwords import load_hotwords_file
from .http_transport import evict_all_keepalive_connections
from .hud import format_copied_hud_text, is_toast_hud_state, resolve_ui_language
from .platform_common import (
    ForegroundWindow,
    MicrophoneUnavailableError,
    NoMicrophoneError,
    PasteOutcome,
)
from .platform_macos import MacOSPlatform, is_macos_terminal_window
from .platform_wsl import (
    WslPlatform,
    is_windows_terminal_process,
    is_wsl,
)
from .refiner import BaseRefiner, RefinerResult, attach_single_hop_context, create_refiner
from .session_ctx import (
    SessionSnapshot,
    SessionTracker,
    parse_window_title_hint,
    prune_stale_session_registry,
)
from .workspace_ctx import (
    _AGY_DESKTOP_REJECT_TITLES,
    _dedupe_hotwords_ci,
    _effective_is_wsl,
    _resolve_safe_local_dir,
    resolve_antigravity_ide_session,
    resolve_antigravity_ide_session_by_title,
    resolve_ide_workspace_cwd,
)


TELEMETRY_FILENAME: str = "telemetry.jsonl"
TELEMETRY_MAX_BYTES: int = 256 * 1024
RECENT_RESULTS_MAXLEN: int = 20
DAEMON_LOG_FILENAME: str = "daemon.log"
DAEMON_LOG_BACKUP_FILENAME: str = "daemon.log.1"
DEFAULT_DAEMON_LOG_MAX_BYTES: int = 2 * 1024 * 1024
_LOG_ROTATE_CHECK_INTERVAL_SEC: float = 60.0
_FAILED_PRUNE_CHECK_INTERVAL_SEC: float = 3600.0


def _resolve_effective_ui_language(
    cfg: Config,
    *,
    env: Mapping[str, str] | None = None,
    platform: Any = None,
) -> str:
    host_culture_fn = getattr(platform, "_host_ui_culture", None)
    win_culture = host_culture_fn() if callable(host_culture_fn) else None
    return resolve_ui_language(cfg.ui.language, env=env, win_ui_culture=win_culture)


def rotate_daemon_log_if_needed(
    log_path: Path,
    *,
    max_bytes: int = DEFAULT_DAEMON_LOG_MAX_BYTES,
    rebind_stdio: bool = True,
) -> bool:
    """Safely rotate ``daemon.log`` -> ``daemon.log.1`` (0600) once size reaches ``max_bytes``."""
    if max_bytes <= 0:
        return False
    try:
        st = os.lstat(log_path)
        if not stat.S_ISREG(st.st_mode):
            return False
        backup_path = log_path.with_name(f"{log_path.name}.1")
        bst: os.stat_result | None = None
        try:
            bst = os.lstat(backup_path)
            if not stat.S_ISREG(bst.st_mode):
                return False
        except FileNotFoundError:
            bst = None

        # Idempotent self-heal: if a prior rotation partially failed dup2 and left fd 1/2
        # still pointing at daemon.log.1 while daemon.log exists, rebind those fds back now.
        if rebind_stdio and bst is not None:
            backup_fid = (bst.st_dev, bst.st_ino)
            stale_fds: list[int] = []
            for fd_num in (1, 2):
                try:
                    fst = os.fstat(fd_num)
                    if (fst.st_dev, fst.st_ino) == backup_fid:
                        stale_fds.append(fd_num)
                except OSError:
                    pass
            if stale_fds:
                for fd_num, stream in ((1, sys.stdout), (2, sys.stderr)):
                    if fd_num in stale_fds:
                        try:
                            stream.flush()
                        except Exception:
                            pass
                heal_flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
                heal_fd: int | None = None
                try:
                    heal_fd = os.open(str(log_path), heal_flags, 0o600)
                    for fd_num in stale_fds:
                        try:
                            os.dup2(heal_fd, fd_num)
                        except OSError:
                            pass
                except OSError:
                    pass
                finally:
                    if heal_fd is not None:
                        try:
                            os.close(heal_fd)
                        except OSError:
                            pass

        if st.st_size < max_bytes:
            return False

        rebind_fds: list[int] = []
        if rebind_stdio:
            target_fid = (st.st_dev, st.st_ino)
            for fd_num in (1, 2):
                try:
                    fst = os.fstat(fd_num)
                    if (fst.st_dev, fst.st_ino) == target_fid:
                        rebind_fds.append(fd_num)
                except OSError:
                    pass
            if 1 in rebind_fds:
                try:
                    sys.stdout.flush()
                except Exception:
                    pass
            if 2 in rebind_fds:
                try:
                    sys.stderr.flush()
                except Exception:
                    pass

        tmp_path = log_path.with_name(
            f".{log_path.name}.rotate.{os.getpid()}.{time.monotonic_ns()}.tmp"
        )
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_APPEND
            | getattr(os, "O_NOFOLLOW", 0)
        )
        new_fd: int | None = None
        tmp_exists = False
        try:
            new_fd = os.open(str(tmp_path), flags, 0o600)
            tmp_exists = True
            os.fchmod(new_fd, 0o600)

            os.replace(log_path, backup_path)
            try:
                os.replace(tmp_path, log_path)
                tmp_exists = False
            except BaseException:
                # Best-effort rollback so log_path never stays permanently missing on .1
                try:
                    os.replace(backup_path, log_path)
                except OSError:
                    pass
                raise

            try:
                os.chmod(backup_path, 0o600)
            except OSError:
                pass
            dup_failed = False
            for fd_num in rebind_fds:
                try:
                    os.dup2(new_fd, fd_num)
                except OSError:
                    dup_failed = True
            return not dup_failed
        finally:
            if new_fd is not None:
                try:
                    os.close(new_fd)
                except OSError:
                    pass
            if tmp_exists:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
    except Exception:
        return False


class DaemonAlreadyRunningError(RuntimeError):
    """Raised when another daemon instance already holds ``daemon.lock``."""

    def __init__(self, pid: int | None = None) -> None:
        self.pid = pid if (pid is not None and pid > 0) else None
        suffix = f" (pid={self.pid})" if self.pid else ""
        super().__init__(f"agent-voice-input daemon is already running{suffix}")


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
    if sys.platform == "darwin":
        return MacOSPlatform(cfg, env=env)
    if sys.platform == "linux" and not is_wsl(env):
        raise RuntimeError(
            "Linux desktop host is not yet supported (use macOS or Windows+WSL2 as local voice host)"
        )
    return WslPlatform(cfg, env=env)


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
    asr_missed_chunks: int = 0
    skipped_reason: str = ""
    ctx_fallback: str = ""


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
    origin_drifted = bool(win.origin_drifted and window_title_override is None)
    if should_sync and not origin_drifted:
        sync_fn = tracker.sync_session
        try:
            sync_params = inspect.signature(sync_fn).parameters
            accepts_var_kw = any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sync_params.values()
            )
        except (TypeError, ValueError):
            sync_params = {}
            accepts_var_kw = False
        extra_sync_kw: dict[str, Any] = {}
        if accepts_var_kw or "window_process" in sync_params:
            extra_sync_kw["window_process"] = win.process
        if accepts_var_kw or "bundle_id" in sync_params:
            extra_sync_kw["bundle_id"] = win.bundle_id
        if accepts_var_kw or "document" in sync_params:
            extra_sync_kw["document"] = win.document
        if accepts_var_kw or "tmux_socket" in sync_params:
            extra_sync_kw["tmux_socket"] = win.tmux_socket
        if accepts_var_kw or "tmux_pane" in sync_params:
            extra_sync_kw["tmux_pane"] = win.tmux_pane
        if cancel_check is not None and (accepts_var_kw or "cancel_token" in sync_params):
            extra_sync_kw["cancel_token"] = cancel_check
        if accepts_var_kw or "cold_start_fallback" in sync_params:
            extra_sync_kw["cold_start_fallback"] = cfg.refiner.cold_start_fallback
        st.snap = (
            sync_fn(
                window_title=win.title,
                window_tty=win.tty,
                window_cwd=win.cwd,
                window_pid=win.pid,
                **extra_sync_kw,
            )
            if window_title_override is None
            else sync_fn(
                window_title=window_title_override,
                **{
                    k: v
                    for k, v in extra_sync_kw.items()
                    if k in ("cancel_token", "cold_start_fallback")
                },
            )
        )
        if (
            st.snap is None
            and window_title_override is None
            and win.cwd
            and not getattr(tracker, "last_sync_remote_vetoed", False)
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
        skip_legacy_agy = bool(
            is_agy_ide
            and not (getattr(win, "document", "") or "").strip()
            and (win.title or "").strip().lower() in _AGY_DESKTOP_REJECT_TITLES
        )
        if is_agy_ide and not is_remote_ide_window(win) and not skip_legacy_agy:
            ide_match = resolve_antigravity_ide_session_by_title(
                win.title, home=tracker.home, env=env
            )
            if ide_match is not None:
                if ide_match.cwd:
                    st.workspace_cwd = ide_match.cwd
                st.snap = tracker.sync_ide_session(
                    ide_match, cold_start_fallback=cfg.refiner.cold_start_fallback
                )
        if st.snap is None and not skip_legacy_agy:
            st.workspace_cwd = resolve_ide_workspace_cwd(
                win,
                home=tracker.home,
                known_cwds=tracker.active_session_cwds(),
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
        st.target_hint = classify_target_app_hint(win)

    if (
        eff_mode != "general"
        and st.snap is not None
        and getattr(st.snap, "prior_hotwords_used", False)
        and cfg.refiner.cold_start_fallback != "off"
        and not st.ctx_fallback
    ):
        st.ctx_fallback = "prior_hotwords"

    hw_scope = resolve_hotwords_scope(
        win,
        target_hint=st.target_hint,
        mode=eff_mode,
        has_agent_session=(st.snap is not None),
    )
    has_hw_file = bool(cfg.asr.hotwords_file.strip())
    manual_hw = (
        load_hotwords_file(cfg.asr.hotwords_file, env=env, scene=hw_scope)
        if has_hw_file
        else []
    )
    asr_ctx = tracker.build_asr_context(
        st.snap,
        extra_hotwords=[*manual_hw, *extra_hw],
        mode=eff_mode,
        language=_resolve_effective_ui_language(cfg, env=env, platform=platform),
    )
    asr_ctx.hotwords_scope = hw_scope
    asr_ctx.manual_hotwords_loaded = has_hw_file
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
        if eff_mode != "general" and any(
            b.startswith("<prior_session_reference") for b in asr_ctx.context_blocks
        ):
            st.ctx_fallback = "prior_session"
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
            if c_rsn in ("cancelled", "shutdown") or (
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
                in_delivery = True
                paste_out = deliver_fn(cand_text, st.win)
                in_delivery = False
                deliver_ms = _elapsed_ms(t_deliv0)
            else:
                t_deliv0 = time.monotonic()
                in_delivery = True
                paste_out = platform.deliver_text(
                    cand_text,
                    seq_id=seq_id,
                    target_hwnd=st.win.hwnd,
                    origin_window=st.win,
                    is_cancelled=cancel_check,
                    before_commit=(cancel_token.try_commit if cancel_token is not None else None),
                )
                in_delivery = False
                deliver_ms = _elapsed_ms(t_deliv0)

            if paste_out is not None and paste_out.reason == "cancelled":
                return _cancel_res(_cancel_reason() or "cancelled", draft=cand_text, refined=ref_res, deliver_ms=deliver_ms)
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
            if (c_rsn := _cancel_reason()) in ("cancelled", "shutdown"):
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


CONTROL_RESPONSE_MAX_BYTES: int = 1_048_576
_BRIDGE_ASSET_NAMES: tuple[str, ...] = ("mac_shim.swift", "win_shim.ps1")


class _MalformedControlResponseError(ValueError):
    """Raised when control.sock returns EOF, malformed JSON, or an incomplete status payload."""


def compute_package_build_id(pkg_dir: Path | None = None) -> str:
    """Compute a lightweight deterministic 12-char SHA-256 fingerprint of package ``*.py`` and bridge assets."""
    root = pkg_dir if pkg_dir is not None else Path(__file__).resolve().parent
    h = hashlib.sha256()
    try:
        candidates: list[Path] = sorted(root.glob("*.py"))
        for asset in _BRIDGE_ASSET_NAMES:
            ap = root / asset
            if ap.is_file():
                candidates.append(ap)
        candidates.sort(key=lambda p: p.name)
        for path in candidates:
            if not path.is_file():
                continue
            h.update(path.name.encode("utf-8"))
            h.update(b"\0")
            h.update(path.read_bytes())
            h.update(b"\0")
    except OSError:
        return ""
    return h.hexdigest()[:12]


def describe_daemon_drift(
    daemon_status: Mapping[str, Any],
    *,
    current_version: str = __version__,
    current_build_id: str | None = None,
) -> dict[str, Any]:
    """Compare a running daemon's reported version/build_id against the current CLI process."""
    run_ver = str(daemon_status.get("version") or "").strip()
    run_bid = str(daemon_status.get("build_id") or "").strip()
    cur_bid = current_build_id if current_build_id is not None else compute_package_build_id()
    ver_diff = bool(run_ver and run_ver != current_version)
    bid_diff = bool(run_bid and cur_bid and run_bid != cur_bid)
    drifted = ver_diff or bid_diff
    missing_bid = bool(run_ver and not ver_diff and not run_bid and cur_bid)
    if ver_diff and bid_diff:
        reason = f"version/code mismatch (running={run_ver}/{run_bid}, current={current_version}/{cur_bid})"
    elif ver_diff:
        reason = f"version mismatch (running={run_ver}, current={current_version})"
    elif bid_diff:
        reason = f"code changed (running={run_bid}, current={cur_bid})"
    else:
        reason = ""
    note = (
        f"running daemon (v{run_ver}) omits build_id; run 'agent-voice-input restart' if you updated code"
        if missing_bid
        else ""
    )
    return {
        "drifted": drifted,
        "missing_build_id": missing_bid,
        "running_version": run_ver,
        "running_build_id": run_bid,
        "current_version": current_version,
        "current_build_id": cur_bid,
        "reason": reason,
        "note": note,
    }


def _read_positive_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
        val = int(raw)
        return val if val > 0 else None
    except (OSError, ValueError):
        return None


def _inspect_daemon_lock_holder(runtime_dir: Path) -> tuple[bool, int | None]:
    """Return ``(lock_held, holder_pid)`` without creating or modifying ``daemon.lock``."""
    lock_path = runtime_dir / "daemon.lock"
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(str(lock_path), flags)
    except OSError:
        return False, None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            return False, None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            if isinstance(exc, OSError) and exc.errno not in (
                errno.EACCES,
                errno.EAGAIN,
                errno.EWOULDBLOCK,
            ):
                return False, None
            os.lseek(fd, 0, os.SEEK_SET)
            raw_pid = os.read(fd, 64).decode("utf-8", errors="ignore").strip()
            holder_pid = (
                int(raw_pid)
                if (raw_pid.isdigit() and int(raw_pid) > 0)
                else _read_positive_pid(runtime_dir / "daemon.pid")
            )
            return True, holder_pid
        else:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            return False, None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def send_control_command(runtime_dir: Path, payload: Mapping[str, Any], timeout_sec: float = 8.0) -> dict[str, Any]:
    sock_path = runtime_dir / "control.sock"
    if not sock_path.exists():
        raise RuntimeError(f"daemon socket not found at {sock_path}")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout_sec); s.connect(str(sock_path))
        s.sendall((json.dumps(dict(payload), ensure_ascii=False) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf and len(buf) < CONTROL_RESPONSE_MAX_BYTES and (chunk := s.recv(16384)):
            buf += chunk
    if not buf.strip():
        raise _MalformedControlResponseError("empty control response (EOF)")
    return json.loads(buf.decode("utf-8", errors="replace").strip())


def probe_daemon_status(
    runtime_dir: Path,
    *,
    timeout_sec: float = 8.0,
    history: int | None = None,
) -> dict[str, Any]:
    """Query daemon status and distinguish genuinely ``stopped`` from live-but-``unresponsive``."""
    req: dict[str, Any] = {"cmd": "status"}
    if history is not None:
        req["history"] = history
    try:
        resp = send_control_command(runtime_dir, req, timeout_sec=timeout_sec)
        if (
            isinstance(resp, Mapping)
            and resp.get("ok") is True
            and isinstance(resp.get("state"), str)
            and resp["state"].strip()
        ):
            return dict(resp)
        err_detail = str(resp.get("error") or "") if isinstance(resp, Mapping) else ""
        raise _MalformedControlResponseError(
            err_detail or "malformed or error status response from daemon"
        )
    except Exception as exc:
        lock_held, holder_pid = _inspect_daemon_lock_holder(runtime_dir)
        sock_exists = (runtime_dir / "control.sock").exists()
        is_timeout = isinstance(exc, (TimeoutError, socket.timeout))
        is_backlog_busy = isinstance(exc, OSError) and exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK)
        is_malformed = (
            isinstance(exc, (_MalformedControlResponseError, ValueError)) and sock_exists
        )
        if lock_held or is_timeout or is_backlog_busy or is_malformed:
            if holder_pid is None:
                cand_pid = _read_positive_pid(runtime_dir / "daemon.pid")
                if cand_pid is not None:
                    try:
                        os.kill(cand_pid, 0)
                        holder_pid = cand_pid
                    except PermissionError:
                        holder_pid = cand_pid
                    except OSError:
                        holder_pid = None
            if is_timeout:
                err_msg = f"control socket timed out after {timeout_sec:.1f}s (daemon busy or hung)"
            elif lock_held:
                err_msg = f"daemon.lock is held but control socket is unreachable ({exc})"
            else:
                err_msg = f"control socket error ({exc})"
            out: dict[str, Any] = {
                "ok": False,
                "state": "unresponsive",
                "lock_held": lock_held,
                "error": err_msg,
            }
            if holder_pid is not None:
                out["pid"] = holder_pid
            return out
        is_offline_err = (
            isinstance(exc, (RuntimeError, FileNotFoundError, ConnectionRefusedError))
            or (
                isinstance(exc, OSError)
                and exc.errno in (errno.ENOENT, errno.ECONNREFUSED, errno.ENOTSOCK, errno.EPROTOTYPE)
            )
            or (isinstance(exc, (_MalformedControlResponseError, ValueError)) and not sock_exists)
        )
        if not is_offline_err:
            return {
                "ok": False,
                "state": "error",
                "lock_held": False,
                "error": f"control socket error ({exc})",
            }
        return {"ok": False, "state": "stopped"}


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

    def _read_pid_file_fallback(self) -> int | None:
        try:
            raw = self.pid_path.read_text(encoding="utf-8").strip()
            val = int(raw)
            return val if val > 0 else None
        except (OSError, ValueError):
            return None

    def acquire_lock(self) -> None:
        if self._lock_fd is not None:
            return
        ensure_secure_dir(self.runtime_dir)
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        fd = os.open(str(self.lock_path), flags, 0o600)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                raise RuntimeError(f"unsafe daemon lock file: {self.lock_path}")
            os.fchmod(fd, 0o600)
            os.set_inheritable(fd, False)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError) as exc:
                if isinstance(exc, OSError) and exc.errno not in (
                    errno.EACCES,
                    errno.EAGAIN,
                    errno.EWOULDBLOCK,
                ):
                    raise
                os.lseek(fd, 0, os.SEEK_SET)
                raw_pid = os.read(fd, 64).decode("utf-8", errors="ignore").strip()
                holder_pid = (
                    int(raw_pid)
                    if (raw_pid.isdigit() and int(raw_pid) > 0)
                    else self._read_pid_file_fallback()
                )
                raise DaemonAlreadyRunningError(holder_pid) from None
            os.ftruncate(fd, 0)
            if self.sock_path.exists():
                legacy_pid: int | None = None
                legacy_alive = False
                try:
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe_sock:
                        probe_sock.settimeout(0.6)
                        probe_sock.connect(str(self.sock_path))
                        # A successful AF_UNIX connect proves a live listener holds control.sock!
                        legacy_alive = True
                        try:
                            probe_sock.sendall(b'{"cmd":"status"}\n')
                            buf = b""
                            while b"\n" not in buf and len(buf) < 65536 and (chunk := probe_sock.recv(16384)):
                                buf += chunk
                            st_resp = json.loads(buf.decode("utf-8", errors="replace").strip() or "{}")
                            if isinstance(st_resp, Mapping):
                                raw_lpid = st_resp.get("pid")
                                if isinstance(raw_lpid, int) and not isinstance(raw_lpid, bool) and raw_lpid > 0:
                                    legacy_pid = raw_lpid
                        except Exception:
                            pass
                except (TimeoutError, socket.timeout, PermissionError):
                    legacy_alive = True
                except OSError as sock_exc:
                    # Only treat explicit dead/non-socket errors as offline; EAGAIN/EWOULDBLOCK means full backlog on live listener!
                    if sock_exc.errno in (
                        errno.ENOENT,
                        errno.ECONNREFUSED,
                        errno.ENOTSOCK,
                        errno.EPROTOTYPE,
                    ):
                        legacy_alive = False
                    else:
                        legacy_alive = True
                if legacy_alive:
                    if legacy_pid is None:
                        legacy_pid = self._read_pid_file_fallback()
                    raise DaemonAlreadyRunningError(legacy_pid)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        except BaseException:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)
            raise
        self._lock_fd = fd
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
            self._invoke_platform_hud_unlocked(
                state,
                duration_ms=duration_ms,
                layer=layer,
                clear_main=clear_main,
                clear_toast=clear_toast,
                bg_count=bg_count,
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
                    self._recompute_state_locked()
                    vis_st = self._derive_bg_stage_locked(visible_only=True)
                    if vis_st == desired_stage and self._main_hud_state != desired_stage:
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
                        if vis_bg:
                            if self._main_hud_state != vis_bg:
                                calls.append((vis_bg, None, None, True))
                            calls.append(("cancelled", None, False, None))
                        else:
                            calls.append(("cancelled", None, True, None))
                    else:
                        calls.append(((vis_bg if vis_bg else "hide"), None, None, True))
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
            calls: list[tuple[str, int, str | None, bool | None, bool | None]] = []
            with self._lock:
                if not self._stop_evt.is_set():
                    rec_busy = self._recording_active or self._stopping_capture
                    vis_bg = self._derive_bg_stage_locked(visible_only=True)
                    still_cur = not rec_busy and self.seq_id == cur_seq
                    if rec_busy:
                        pass
                    elif vis_bg:
                        if restore_main_if_needed and self._main_hud_state != vis_bg:
                            calls.append((vis_bg, 0, None, None, None))
                        calls.append((toast_state, duration_ms, None, False, None))
                    elif still_cur:
                        clr_m = True if clear_main_if_idle else bool(self._main_hud_visible)
                        calls.append((toast_state, duration_ms, None, clr_m, None))
                    for st_n, _, lyr_n, clr_m_n, _ in calls:
                        self._update_main_hud_visibility_locked(st_n, layer=lyr_n, clear_main=clr_m_n)
            for st_n, dur_n, lyr_n, clr_m_n, clr_t_n in calls:
                self._invoke_platform_hud_unlocked(
                    st_n,
                    duration_ms=dur_n,
                    layer=lyr_n,
                    clear_main=clr_m_n,
                    clear_toast=clr_t_n,
                )

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
                self._recording_trigger_action = ""
                self._recording_shim_id = 0
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
                        self._recompute_state_locked()
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
                        self._emit_capture_error_hud(
                            cur_seq=cur_seq,
                            toast_state=perm_hud,
                            duration_ms=0,
                            restore_main_if_needed=True,
                        )
                        self.platform.beep("error")
                    return
                audio_err = self._redact_str(snap_audio_err or "mic_disconnected")
                with self._lock:
                    self._stopping_capture = False
                    self._recompute_state_locked()
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
                    self._emit_capture_error_hud(
                        cur_seq=cur_seq,
                        toast_state="mic_disconnected",
                        duration_ms=2200,
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
                if self.seq_id == cur_seq:
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

    def handle_trigger(self, action: str, window: ForegroundWindow | None = None, *, no_paste: bool = False, silent_hud: bool = False, loopback: bool = False, from_hotkey: bool = False, expected_seq_id: int | None = None, shim_instance_id: int = 0, stop_before_hud: bool = False) -> dict[str, Any]:
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
                cancelled_rec_seq = -1
                if action == "ptt_cancel":
                    if not self._recording_active:
                        return {"ok": True, "state": self.state, "seq_id": self.seq_id}
                    eff_silent = bool(silent_hud or self._cur_silent_hud)
                    cancelled_rec_seq = self._recording_seq_id
                    self._recording_active = False
                    self._recording_seq_id = -1
                    self._recording_trigger_action = ""
                    self._recording_shim_id = 0
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
                    self._recording_active = False
                    self._recording_seq_id = -1
                    self._recording_trigger_action = ""
                    self._recording_shim_id = 0
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
                    self._recompute_state_locked()
                    stop_hud_stage = self._derive_bg_stage_locked(visible_only=True) or "recognizing"
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
                if not eff_silent:
                    resume_state = self._emit_recording_cancel_hud(show_cancelled_toast=False)
                self.platform.cancel_recording()
                if not eff_silent:
                    self.platform.beep("error")
                return {"ok": True, "state": resume_state, "seq_id": cur_seq}

            if mode == "start":
                self._cancel_max_duration_timer()
                rec_started = False
                trigger_win = window
                resolver = getattr(self.platform, "resolve_foreground_window", None)
                fg_box: list[ForegroundWindow | None] = [None]
                fg_err: list[BaseException | None] = [None]
                fg_done: threading.Event | None = None
                fg_spawn_failed = False
                if callable(resolver) and bool(getattr(self.platform, "_concurrent_origin_resolve", False)):
                    fg_done = threading.Event()
                    _res_fn = resolver
                    _tw = trigger_win

                    def _resolve_origin_bg() -> None:
                        try:
                            fg_box[0] = _res_fn(_tw)
                        except BaseException as exc:
                            fg_err[0] = exc
                        finally:
                            assert fg_done is not None
                            fg_done.set()

                    try:
                        threading.Thread(
                            target=_resolve_origin_bg,
                            name=f"avi-origin-{cur_seq}",
                            daemon=True,
                        ).start()
                    except Exception:
                        fg_done = None
                        fg_spawn_failed = True
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
                        with self._hud_lock:
                            bg_cnt = 0
                            emit_rec = False
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
                                    emit_rec = True
                            if emit_rec:
                                self._invoke_platform_hud_unlocked(
                                    "recording", clear_toast=True, bg_count=bg_cnt
                                )
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
                    if fg_spawn_failed:
                        win_now = (
                            replace(trigger_win, origin_drifted=True)
                            if trigger_win is not None
                            else ForegroundWindow(origin_drifted=True)
                        )
                    elif fg_done is not None:
                        finished = fg_done.wait(timeout=self._origin_resolve_timeout_sec)
                        sampled = fg_box[0] if finished and fg_err[0] is None else None
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
                            win_now = (
                                replace(trigger_win, origin_drifted=True)
                                if trigger_win is not None
                                else ForegroundWindow(origin_drifted=True)
                            )
                        else:
                            assert sampled is not None
                            win_now = sampled
                    elif callable(resolver):
                        win_now = resolver(trigger_win)
                    else:
                        win_now = trigger_win if (trigger_win and (trigger_win.title or trigger_win.process or trigger_win.hwnd)) else self.platform.get_foreground_window()
                    if trigger_win is not None and not win_now.origin_drifted:
                        conflict_fn = getattr(self.platform, "origin_resolution_conflicted", None)
                        if callable(conflict_fn) and conflict_fn(trigger_win, win_now):
                            win_now = replace(trigger_win, origin_drifted=True)
                    with self._lock:
                        if self._recording_active and self._recording_seq_id == cur_seq:
                            self._origin_win = win_now
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
                            self._recording_trigger_action = ""
                            self._recording_shim_id = 0
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
                        self._emit_capture_error_hud(
                            cur_seq=cur_seq,
                            toast_state=err_hud_state,
                            duration_ms=err_hud_dur,
                            restore_main_if_needed=rec_started,
                        )
                    if not eff_silent:
                        self.platform.beep("error")
                    out_err: dict[str, Any] = {"ok": False, "error": err, "state": err_state, "seq_id": cur_seq}
                    if err_reason:
                        out_err["skipped_reason"] = err_reason
                    return out_err

            self._cancel_max_duration_timer()
            if not eff_silent and not stop_before_hud:
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
                if not eff_silent and stop_before_hud:
                    self._hud(stop_hud_stage)
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
                    lr_stop: dict[str, Any] = {"seq_id": cur_seq, "ok": False, "error": err}
                    if err_reason:
                        lr_stop["skipped_reason"] = err_reason
                    lr_stop_snap = self._record_result_locked(lr_stop)
                self._append_telemetry(lr_stop_snap)
                if not eff_silent:
                    self._emit_capture_error_hud(
                        cur_seq=cur_seq,
                        toast_state=err_hud_state,
                        duration_ms=err_hud_dur,
                        restore_main_if_needed=True,
                    )
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
                    self._recording_active = False
                    self._recording_seq_id = -1
                    self._recording_trigger_action = ""
                    self._recording_shim_id = 0
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
                self._cancel_remote_prefetch(seq_id=cancelled_rec_seq)
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
                    cancel_calls: list[tuple[str, str | None, bool | None, bool | None, int]] = []
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
                                    cancel_calls.append(("recording", None, None, False, cur_vis_cnt))
                                if not silent_hud:
                                    cancel_calls.append(("cancelled", None, False, None, 0))
                            elif not silent_hud:
                                if cur_stopping:
                                    cancel_calls.append(("cancelled", None, False, None, 0))
                                elif cur_vis_bg:
                                    if self._main_hud_state != cur_vis_bg:
                                        cancel_calls.append((cur_vis_bg, None, None, None, 0))
                                    cancel_calls.append(("cancelled", None, False, None, 0))
                                else:
                                    cancel_calls.append(("cancelled", None, True, None, 0))
                            for st_n, lyr_n, clr_m_n, _, bg_n in cancel_calls:
                                self._update_main_hud_visibility_locked(
                                    st_n, layer=lyr_n, clear_main=clr_m_n, bg_count=bg_n
                                )
                    for st_n, lyr_n, clr_m_n, clr_t_n, bg_n in cancel_calls:
                        self._invoke_platform_hud_unlocked(
                            st_n,
                            layer=lyr_n,
                            clear_main=clr_m_n,
                            clear_toast=clr_t_n,
                            bg_count=bg_n,
                        )

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
        paste_out = self.platform.deliver_text(
            final_text,
            seq_id=task.seq_id,
            target_hwnd=origin_win.hwnd,
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
            if sr in ("too_short", "silence", "empty_audio"):
                if task.recorder_lost:
                    hud_st, hud_dur_ms, beep_k = "mic_disconnected", 2200, ""
                    sum_skip["skipped_reason"] = "mic_disconnected"
                else:
                    hud_st, beep_k = "empty", "error"
            elif sr == "empty_asr":
                if task.recorder_lost:
                    hud_st, hud_dur_ms, beep_k = "mic_disconnected", 2200, ""
                    sum_skip["skipped_reason"] = "mic_disconnected"
                else:
                    hud_st = "asr_empty_retry" if saved_p else "asr_empty"
                    beep_k = "error"
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
                if not task.cancel_token.try_commit():
                    return _cancelled_outcome()
                deliver_ms = _elapsed_ms(t_deliv0)
                saved_p = _stash_slot_wav()
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
                    error=self._redact(deliv_exc),
                )
                hud_st = "paste_failed_retry" if saved_p else "paste_failed"
                beep_k = "" if task.recorder_lost else "error"
                return _summarize(res_deliv_err), hud_st, hud_dur_ms, beep_k
            if paste_out.reason == "cancelled" or task.cancel_token.is_cancelled():
                return _cancelled_outcome()
            task.cancel_token.try_commit()

        delivery_failed = paste_out is not None and not paste_out.pasted and not paste_out.clipboard
        copied_only = bool(paste_out and not paste_out.pasted and paste_out.clipboard)
        saved_p = _stash_slot_wav() if (delivery_failed or st.asr_missed_chunks > 0) else ""
        eff_skip = (
            (
                paste_out.reason
                if (paste_out is not None and paste_out.reason and paste_out.reason != "ok")
                else "paste_failed"
            )
            if delivery_failed
            else ""
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
        if delivery_failed:
            hud_st = "paste_failed_retry" if saved_p else "paste_failed"
            beep_k = "" if task.recorder_lost else "error"
        elif st.asr_missed_chunks > 0:
            if copied_only:
                hud_st = "partial_asr_copied" if saved_p else "partial_asr_copied_unsaved"
            else:
                hud_st = "partial_asr" if saved_p else "partial_asr_unsaved"
        elif copied_only:
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
                        if task.silent_hud:
                            if not rec_active and not next_vis_stage and self._main_hud_visible:
                                hud_calls.append(("hide", "", 0, None, None, None, 0))
                        elif rec_active:
                            if (
                                self._recording_active
                                and not self._stopping_capture
                                and not self._cur_silent_hud
                                and self._main_hud_state == "recording"
                                and vis_inflight != self._main_hud_bg_count
                            ):
                                hud_calls.append(("recording", "", 0, None, None, False, vis_inflight))
                            if hud_st == "copied":
                                hud_calls.append(("copied", copied_hud_text, hud_dur_ms, "toast", False, None, 0))
                            elif hud_st and is_toast_hud_state(hud_st):
                                hud_calls.append((hud_st, "", hud_dur_ms, None, False, None, 0))
                        elif next_vis_stage:
                            if self._main_hud_state != next_vis_stage:
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
                            elif self._main_hud_visible:
                                hud_calls.append(("hide", "", 0, None, None, None, 0))
                            play_beep = beep_k
                        for st_n, _, _, lyr_n, clr_m_n, _, bg_n in hud_calls:
                            self._update_main_hud_visibility_locked(
                                st_n, layer=lyr_n, clear_main=clr_m_n, bg_count=bg_n
                            )
                for st_name, txt_val, dur_val, lyr_val, clr_main_val, clr_toast_val, bg_val in hud_calls:
                    self._invoke_platform_hud_unlocked(
                        st_name,
                        text=txt_val,
                        duration_ms=dur_val,
                        layer=lyr_val,
                        clear_main=clr_main_val,
                        clear_toast=clr_toast_val,
                        bg_count=bg_val,
                    )
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
                        and self.seq_id != seq_id
                    ):
                        return
                pref_extra: dict[str, Any] = {}
                try:
                    pref_params = inspect.signature(pref_fn).parameters
                    pref_var_kw = any(
                        p.kind == inspect.Parameter.VAR_KEYWORD for p in pref_params.values()
                    )
                except (TypeError, ValueError):
                    pref_params = {}
                    pref_var_kw = False
                if pref_var_kw or "tmux_socket" in pref_params:
                    pref_extra["tmux_socket"] = win.tmux_socket
                if pref_var_kw or "tmux_pane" in pref_params:
                    pref_extra["tmux_pane"] = win.tmux_pane
                pref_fn(
                    win.title,
                    window_tty=win.tty,
                    window_cwd=win.cwd,
                    window_pid=win.pid,
                    window_process=win.process,
                    bundle_id=win.bundle_id,
                    document=win.document,
                    seq_id=seq_id,
                    include_git_info=inc_git,
                    **pref_extra,
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
            if self._worker_thread is not None:
                bg_threads.append(self._worker_thread)
            if self._commit_thread is not None:
                bg_threads.append(self._commit_thread)
            bg_threads.extend(th for th in self._stage2_threads if th.is_alive())
            self._queue_cv.notify_all()

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
            hist_limit = max(0, min(500, int(raw_hist))) if want_hist else 0
            with self._lock:
                resp: dict[str, Any] = {
                    "ok": True,
                    "version": __version__,
                    "build_id": self.build_id,
                    "pid": os.getpid(),
                    "state": self.state,
                    "seq_id": self.seq_id,
                    "pending_count": self._inflight_or_queued_count_locked(),
                    "asr_provider": self.active_pipeline["asr"]["provider"],
                    "refiner_provider": self.resolved_refiner_provider,
                    "pipeline": copy.deepcopy(self.active_pipeline),
                    "shim_ok": shim_ok,
                    "last_result": dict(self.last_result),
                }
                if shim_err:
                    resp["shim_error"] = shim_err
                if self.cfg.remote.enabled and (rm := getattr(self.tracker, "remote_manager", None)) is not None:
                    bs = rm.breaker_snapshot()
                    if bs:
                        resp["remote_hosts"] = bs
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
        return self.handle_trigger("ptt_stop", expected_seq_id=target_seq, stop_before_hud=True)

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
            pid_fd = os.open(
                str(self.pid_path),
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                os.fchmod(pid_fd, 0o600)
                os.write(pid_fd, f"{os.getpid()}\n".encode("ascii"))
            finally:
                os.close(pid_fd)
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
        try:
            bg_threads = self._initiate_shutdown()
            if self._srv_sock is not None:
                try: self._srv_sock.close()
                except OSError: pass
                self._srv_sock = None
            if self._owns_runtime_files:
                self.sock_path.unlink(missing_ok=True)
                self.pid_path.unlink(missing_ok=True)
            self.refiner.close()
            if (tr_close := getattr(self.tracker, "close", None)) is not None:
                try:
                    tr_close()
                except Exception:
                    pass
            join_deadline = time.monotonic() + 3.0
            cur_th = threading.current_thread()
            for th in bg_threads:
                if (rem := join_deadline - time.monotonic()) <= 0:
                    break
                if th.is_alive() and th is not cur_th:
                    th.join(timeout=rem)
            self.wait_pending_archives(timeout_sec=2.0)
            self.platform.close()
        finally:
            # Never unlink daemon.lock: unlinking causes a classic inode race allowing two concurrent daemons.
            lock_fd, self._lock_fd = self._lock_fd, None
            self._owns_runtime_files = False
            if lock_fd is not None:
                try:
                    os.ftruncate(lock_fd, 0)
                except OSError:
                    pass
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                try:
                    os.close(lock_fd)
                except OSError:
                    pass

    stop = close
