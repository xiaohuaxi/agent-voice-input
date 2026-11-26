"""Sub-second read-only live diagnostics for agent-voice-input runtime state.

Summarizes daemon health (online / unresponsive / stopped + code/version drift),
configured pipeline selection, recent ``telemetry.jsonl`` latency/fallback/cache-hit
statistics, active CLI session match metadata (zero prose), staged ``failed/`` WAV count,
redacted ``daemon.log`` tail, and optional legacy ``codex-home/logs_2.sqlite`` counters.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from typing import Any
import urllib.parse

from . import __version__
from .audio_proc import list_failed_audios
from .config import (
    Config,
    explain_pipeline_selection,
    get_runtime_dir,
    redact_secrets,
    resolve_credentials,
)
from .control_ipc import (
    TELEMETRY_FILENAME,
    describe_daemon_drift,
    probe_daemon_status,
    read_telemetry_history,
)
from .session_ctx import SessionTracker, format_turns_text


def mask_session_key(skey: str) -> str:
    """Return privacy-preserving ``<cli>#<8-hex>`` session tag (or ``'none'``)."""
    raw = (skey or "").strip()
    if not raw:
        return "none"
    cli = raw.split(":", 1)[0] if ":" in raw else "sess"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{cli}#{digest}"


_DELIVERY_FAIL_REASONS: frozenset[str] = frozenset(
    {"clipboard_failed", "paste_failed", "delivery_failed", "delivery_error"}
)
_VALID_DELIVERY_STATUS_ENUMS: frozenset[str] = frozenset(
    {"", "pasted", "copied", "dry_run", "undelivered_recoverable", "undelivered_unrecoverable"}
)


def _is_record_delivered_ok(rec: Mapping[str, Any]) -> bool:
    """Return True iff the telemetry record represents a genuinely delivered/dry-run success."""
    if not rec.get("ok", False):
        return False
    ds = str(rec.get("delivery_status") or "").strip().lower()
    if ds in ("undelivered_recoverable", "undelivered_unrecoverable"):
        return False
    if rec.get("pasted") is False and not rec.get("dry_run", False):
        if rec.get("clipboard") is False:
            return False
        pr = str(rec.get("paste_reason") or "").strip().lower()
        if pr in _DELIVERY_FAIL_REASONS:
            return False
    return True


def diagnose_record_flags(rec: Mapping[str, Any], *, is_first_for_session: bool) -> list[str]:
    """Return diagnostic warning/anomaly flags for a single telemetry record."""
    flags: list[str] = []
    if not _is_record_delivered_ok(rec):
        reason = str(rec.get("skipped_reason") or rec.get("paste_reason") or "error")
        flags.append(f"SKIP({reason})")
        return flags
    missed_chunks = int(rec.get("asr_missed_chunks") or 0)
    if missed_chunks > 0:
        flags.append(f"PARTIAL_ASR({missed_chunks})")
    fb = str(rec.get("refiner_fallback") or "").strip()
    is_passthrough_sh = fb == "single_hop_passthrough" and int(rec.get("refine_ms") or 0) == 0
    is_single_hop_asr = bool(rec.get("is_single_hop_refined")) or is_passthrough_sh
    rec_mode = str(rec.get("mode") or "agent").strip().lower()
    if fb and fb != "single_hop_passthrough":
        flags.append(f"FALLBACK({fb})")
    if (
        rec_mode != "general"
        and not str(rec.get("session_key") or "").strip()
        and (is_single_hop_asr or str(rec.get("refiner_provider") or "") not in ("", "off"))
    ):
        flags.append("NO_SESSION_CTX")
    asr_slow_thr = 3300 if is_single_hop_asr else 1800
    if int(rec.get("asr_ms") or 0) >= asr_slow_thr:
        flags.append("ASR_SLOW")
    if int(rec.get("refine_init_ms") or 0) >= 250:
        flags.append("COLD_INIT")
    if int(rec.get("refine_ms") or 0) >= 1500:
        flags.append("REFINE_SLOW")
    if int(rec.get("deliver_ms") or 0) >= 350:
        flags.append("DELIVER_STALL")
    in_tok = rec.get("input_tokens")
    cached_tok = rec.get("cached_input_tokens")
    ctx_chars = int(rec.get("ctx_chars") or 0)
    if (
        isinstance(in_tok, int)
        and in_tok >= 2048
        and isinstance(cached_tok, int)
        and ctx_chars >= 4000
        and not is_first_for_session
    ):
        if (100.0 * cached_tok / in_tok) < 30.0:
            flags.append("CACHE_MISS")
    rsn_tok = rec.get("reasoning_tokens")
    thought_chars = rec.get("thought_chars")
    thought_chunks = rec.get("thought_chunks")
    if (isinstance(rsn_tok, int) and rsn_tok >= 256) or (
        isinstance(thought_chars, int) and thought_chars >= 400
    ):
        flags.append("REASONING_BLOAT")
    elif (isinstance(thought_chunks, int) and thought_chunks > 0) or (
        isinstance(thought_chars, int) and thought_chars > 0
    ):
        flags.append(f"THOUGHT({int(thought_chunks or 0)}/{int(thought_chars or 0)}c)")
    return flags


_TELEMETRY_NUMERIC_FIELDS: frozenset[str] = frozenset(
    {
        "ts",
        "seq_id",
        "raw_audio_sec",
        "voiced_audio_sec",
        "silence_trimmed_sec",
        "silence_trim_cuts",
        "queue_wait_ms",
        "gate_ms",
        "sync_ms",
        "asr_ms",
        "asr_duration_ms",
        "refine_init_ms",
        "refine_ms",
        "deliver_ms",
        "total_ms",
        "asr_chars",
        "final_chars",
        "ctx_chars",
        "ctx_chunks",
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "thought_chunks",
        "thought_chars",
        "cache_hit_pct",
        "audio_tokens",
        "asr_input_tokens",
        "asr_cached_input_tokens",
        "asr_output_tokens",
        "asr_reasoning_tokens",
        "asr_audio_tokens",
        "asr_retries",
        "refiner_retries",
        "asr_chunks",
        "asr_missed_chunks",
        "recovery_count",
        "recovery_gap_ms",
    }
)
_TELEMETRY_BOOL_FIELDS: frozenset[str] = frozenset(
    {
        "ok",
        "switched_fallback",
        "pasted",
        "clipboard",
        "is_single_hop_refined",
        "dry_run",
        "capture_interrupted",
    }
)
_TELEMETRY_ENUM_FIELDS: frozenset[str] = frozenset(
    {
        "skipped_reason",
        "audio_source",
        "mode",
        "asr_provider",
        "asr_model",
        "refiner_provider",
        "refiner_model",
        "refiner_fallback",
        "paste_reason",
        "delivery_status",
        "ctx_fallback",
    }
)
_VALID_CTX_FALLBACK_ENUMS: frozenset[str] = frozenset(
    {"", "prior_hotwords", "prior_session"}
)
_TELEMETRY_DIAG_TEXT_FIELDS: frozenset[str] = frozenset(
    {
        "asr_request_id",
        "refiner_warning",
        "error",
        "audio_error",
    }
)
_ASR_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")


def _clean_single_line(text: str, redact_fn: Any, *, max_chars: int = 200) -> str:
    redacted = redact_fn(text)
    flat = " ".join(str(redacted).splitlines()).strip()
    return flat[:max_chars] if len(flat) > max_chars else flat


def _sanitize_telemetry_record(rec: Mapping[str, Any], redact_fn: Any) -> dict[str, Any]:
    """Filter a telemetry record through a strict field allowlist with secret redaction."""
    clean: dict[str, Any] = {}
    for k in _TELEMETRY_NUMERIC_FIELDS:
        if k in rec:
            v = rec[k]
            if v is None or (isinstance(v, (int, float)) and not isinstance(v, bool)):
                clean[k] = v
    for k in _TELEMETRY_BOOL_FIELDS:
        if k in rec and isinstance(rec[k], bool):
            clean[k] = rec[k]
    for k in _TELEMETRY_ENUM_FIELDS:
        if k in rec and isinstance(rec[k], str):
            val = _clean_single_line(rec[k], redact_fn, max_chars=120) if rec[k] else ""
            if k == "ctx_fallback" and val not in _VALID_CTX_FALLBACK_ENUMS:
                val = ""
            elif k == "delivery_status" and val not in _VALID_DELIVERY_STATUS_ENUMS:
                val = ""
            clean[k] = val
    for k in _TELEMETRY_DIAG_TEXT_FIELDS:
        if k in rec and isinstance(rec[k], str) and rec[k].strip():
            if k == "asr_request_id":
                parts: list[str] = []
                total_len = 0
                for raw_part in rec[k].split(","):
                    part = raw_part.strip()
                    if part and _ASR_REQUEST_ID_RE.fullmatch(part):
                        clean_part = _clean_single_line(part, redact_fn, max_chars=256)
                        if not clean_part:
                            continue
                        added_len = len(clean_part) + (1 if parts else 0)
                        if total_len + added_len > 256:
                            break
                        parts.append(clean_part)
                        total_len += added_len
                if not parts:
                    continue
                clean[k] = ",".join(parts)
            else:
                clean[k] = _clean_single_line(rec[k], redact_fn, max_chars=200)
    if "asr_chunk_secs" in rec and isinstance(rec["asr_chunk_secs"], list):
        clean["asr_chunk_secs"] = [
            float(x) for x in rec["asr_chunk_secs"] if isinstance(x, (int, float)) and not isinstance(x, bool)
        ]
    return clean


def read_redacted_log_tail(
    log_path: Path,
    start_offset: int,
    env: Mapping[str, str],
    *,
    cfg: Config | None = None,
    max_bytes: int = 2048,
    max_lines: int = 12,
    max_line_chars: int = 240,
) -> str:
    """Read newly appended regular-file log lines since ``start_offset`` with secret redaction."""
    try:
        target_path = log_path
        st: os.stat_result | None = None
        try:
            cand_st = os.lstat(target_path)
            if stat.S_ISREG(cand_st.st_mode):
                st = cand_st
        except OSError:
            st = None
        if (st is None or st.st_size <= 0) and start_offset < 0:
            backup_path = log_path.with_name(f"{log_path.name}.1")
            try:
                bst = os.lstat(backup_path)
                if stat.S_ISREG(bst.st_mode) and bst.st_size > 0:
                    target_path = backup_path
                    st = bst
            except OSError:
                pass
        if st is None or st.st_size <= 0:
            return ""
        base = start_offset if 0 <= start_offset < st.st_size else max(0, st.st_size - max_bytes)
        seek_pos = max(base, st.st_size - max(256, int(max_bytes)))
        with open(target_path, "rb") as f:
            if seek_pos > 0:
                f.seek(seek_pos)
            raw = f.read()
        lines = [ln.rstrip() for ln in raw.decode("utf-8", errors="replace").splitlines() if ln.strip()]
        if not lines:
            return ""
        creds = resolve_credentials(cfg, env) if cfg is not None else None
        extra = creds.all_secret_values() if creds is not None else ()
        redacted = redact_secrets("\n".join(lines[-max_lines:]), env, cfg=cfg, extra_secrets=extra)
        clipped = [
            (ln[:max_line_chars] + "…") if len(ln) > max_line_chars else ln
            for ln in redacted.splitlines()
        ]
        return "\n".join(clipped)
    except Exception:
        return ""


def probe_optional_legacy_codex_sqlite(rt_dir: Path) -> dict[str, Any] | None:
    """Optionally inspect legacy ``codex-home/logs_2.sqlite`` read-only if present."""
    db_path = rt_dir / "codex-home" / "logs_2.sqlite"
    if not db_path.is_file() or db_path.is_symlink():
        return None
    uri = f"file:{urllib.parse.quote(str(db_path))}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=0.2)
    except Exception:
        return {"present": True, "readable": False}
    try:
        cur = conn.cursor()
        tables = {
            str(r[0])
            for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "logs" not in tables:
            return {"present": True, "readable": True, "has_logs_table": False}
        cols = {str(r[1]) for r in cur.execute("PRAGMA table_info(logs)").fetchall()}
        cutoff_ts = int(time.time()) - 3600
        where_ts = f"WHERE ts >= {cutoff_ts}" if "ts" in cols else ""
        proc_cnt = 0
        if "process_uuid" in cols:
            row = cur.execute(
                f"SELECT COUNT(DISTINCT process_uuid) FROM logs {where_ts}"
            ).fetchone()
            proc_cnt = int(row[0] or 0) if row else 0
        warn_cnt = 0
        err_cnt = 0
        if "level" in cols:
            for lvl, cnt in cur.execute(
                f"SELECT UPPER(level), COUNT(*) FROM logs {where_ts} GROUP BY UPPER(level)"
            ).fetchall():
                if lvl == "WARN":
                    warn_cnt = int(cnt or 0)
                elif lvl == "ERROR":
                    err_cnt = int(cnt or 0)
        return {
            "present": True,
            "readable": True,
            "recent_1h_processes": proc_cnt,
            "recent_1h_warns": warn_cnt,
            "recent_1h_errors": err_cnt,
        }
    except Exception:
        return {"present": True, "readable": False}
    finally:
        conn.close()


def _percentile(vals: Sequence[float], pct: float) -> float | None:
    if not vals:
        return None
    s = sorted(float(v) for v in vals)
    k = (len(s) - 1) * (pct / 100.0)
    f, c = math.floor(k), math.ceil(k)
    return s[f] + (k - f) * (s[c] - s[f])


def summarize_telemetry_records(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Compute latency, fallback, failure, and prefix-cache hit summary over annotated records."""
    ok_recs = [r for r in records if _is_record_delivered_ok(r)]
    asr_ms = [int(r["asr_ms"]) for r in ok_recs if isinstance(r.get("asr_ms"), (int, float))]
    ref_ms = [int(r["refine_ms"]) for r in ok_recs if isinstance(r.get("refine_ms"), (int, float))]
    tot_ms = [int(r["total_ms"]) for r in ok_recs if isinstance(r.get("total_ms"), (int, float))]
    hit_rates: list[float] = []
    for r in ok_recs:
        in_t = r.get("input_tokens")
        c_t = r.get("cached_input_tokens")
        if isinstance(in_t, int) and in_t > 0 and isinstance(c_t, int):
            hit_rates.append(100.0 * c_t / in_t)
    fallback_count = sum(
        1
        for r in ok_recs
        if str(r.get("refiner_fallback") or "").strip() not in ("", "single_hop_passthrough")
    )
    flag_counts: dict[str, int] = {}
    for r in records:
        for fl in r.get("diag_flags") or ():
            key = str(fl).split("(", 1)[0]
            flag_counts[key] = flag_counts.get(key, 0) + 1
    r1 = lambda v: round(v, 1) if v is not None else None
    return {
        "total": len(records),
        "ok_count": len(ok_recs),
        "failed_or_skipped": len(records) - len(ok_recs),
        "fallback_count": fallback_count,
        "asr_ms_p50": r1(_percentile(asr_ms, 50)),
        "refine_ms_p50": r1(_percentile(ref_ms, 50)),
        "total_ms_p50": r1(_percentile(tot_ms, 50)),
        "cache_hit_pct_avg": round(sum(hit_rates) / len(hit_rates), 1) if hit_rates else None,
        "flag_counts": flag_counts,
    }


def _read_optional_status_snapshot(rt_dir: Path) -> dict[str, Any]:
    snap_path = rt_dir / "status.json"
    if not snap_path.is_file() or snap_path.is_symlink():
        return {}
    try:
        obj = json.loads(snap_path.read_text(encoding="utf-8"))
        return dict(obj) if isinstance(obj, Mapping) else {}
    except (OSError, ValueError):
        return {}


def _redact_obj(val: Any, redact_fn: Any) -> Any:
    if isinstance(val, str):
        return redact_fn(val)
    if isinstance(val, dict):
        return {str(k): _redact_obj(v, redact_fn) for k, v in val.items()}
    if isinstance(val, (list, tuple)):
        return [_redact_obj(v, redact_fn) for v in val]
    return val


def collect_live_diagnostics(
    cfg: Config,
    env: Mapping[str, str],
    *,
    runtime_dir: Path | None = None,
    window_title: str = "",
    last: int = 10,
) -> dict[str, Any]:
    """Collect a redacted, read-only diagnostic snapshot of the local voice-input runtime."""
    t0 = time.monotonic()
    creds = resolve_credentials(cfg, env)
    secrets = creds.all_secret_values()
    redact_fn = lambda s: redact_secrets(str(s), env, cfg=cfg, extra_secrets=secrets)

    rt_dir = runtime_dir if runtime_dir is not None else get_runtime_dir(env, create=False)
    last_n = max(1, min(100, int(last)))

    status_obj: dict[str, Any] = {}
    records: list[dict[str, Any]] = []
    if rt_dir.is_dir():
        status_obj = probe_daemon_status(rt_dir, timeout_sec=0.35, history=last_n)
        raw_recs = status_obj.get("recent_results")
        if isinstance(raw_recs, list):
            records = [r for r in raw_recs if isinstance(r, dict)]
    daemon_online = bool(status_obj.get("ok"))
    daemon_state = str(status_obj.get("state") or ("idle" if daemon_online else "stopped"))
    status_snapshot = _read_optional_status_snapshot(rt_dir) if (not daemon_online and rt_dir.is_dir()) else {}

    if not records and rt_dir.is_dir():
        records = read_telemetry_history(rt_dir, limit=last_n)

    drift = describe_daemon_drift(status_obj) if daemon_online else None
    pipeline = (
        status_obj.get("pipeline")
        if isinstance(status_obj.get("pipeline"), Mapping)
        else explain_pipeline_selection(cfg, creds, env)
    )

    sess_meta: dict[str, Any] = {"matched": False}
    try:
        tracker = SessionTracker(env=env, runtime_dir=rt_dir if rt_dir.is_dir() else None)
        snap = tracker.sync_session(window_title=window_title)
        if snap is not None:
            j_size = 0
            try:
                j_size = os.path.getsize(snap.match.jsonl_path)
            except OSError:
                j_size = 0
            sess_meta = {
                "matched": True,
                "session_tag": mask_session_key(snap.session_key),
                "matched_by": snap.match.matched_by,
                "turns": len(snap.clean_turns),
                "generation": snap.generation,
                "dialog_chars": len(format_turns_text(snap.clean_turns, max_chars=250_000)),
                "jsonl_kb": round(j_size / 1024.0, 1),
                "hotword_count": len(snap.hotwords),
            }
    except Exception as exc:
        sess_meta = {"matched": False, "error": redact_fn(str(exc))}

    failed_files = list_failed_audios(rt_dir) if rt_dir.is_dir() else []
    newest_failed_ts = ""
    if failed_files:
        try:
            newest_failed_ts = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(failed_files[0].stat().st_mtime)
            )
        except OSError:
            newest_failed_ts = "-"

    telemetry_path = rt_dir / TELEMETRY_FILENAME
    telemetry_kb = 0.0
    if telemetry_path.is_file() and not telemetry_path.is_symlink():
        try:
            telemetry_kb = round(telemetry_path.stat().st_size / 1024.0, 1)
        except OSError:
            telemetry_kb = 0.0

    log_tail = (
        read_redacted_log_tail(rt_dir / "daemon.log", -1, env, cfg=cfg, max_bytes=1536, max_lines=8)
        if rt_dir.is_dir()
        else ""
    )
    legacy_sqlite = probe_optional_legacy_codex_sqlite(rt_dir) if rt_dir.is_dir() else None

    seen_skeys: set[str] = set()
    annotated_records: list[dict[str, Any]] = []
    for rec in records:
        skey = str(rec.get("session_key") or "").strip()
        is_first = bool(skey and skey not in seen_skeys)
        if skey:
            seen_skeys.add(skey)
        clean_rec = _sanitize_telemetry_record(rec, redact_fn)
        flags_input = {**clean_rec, "session_key": skey}
        flags = diagnose_record_flags(flags_input, is_first_for_session=is_first)
        clean_rec["has_saved_audio"] = bool(str(rec.get("saved_audio") or "").strip())
        clean_rec["session_tag"] = mask_session_key(skey)
        clean_rec["diag_flags"] = [redact_fn(fl) for fl in flags]
        annotated_records.append(clean_rec)

    summary_stats = summarize_telemetry_records(annotated_records)
    elapsed_ms = max(0, int(round((time.monotonic() - t0) * 1000)))
    eff_asr = str(status_obj.get("asr_provider") or pipeline["asr"]["provider"])
    eff_ref = str(status_obj.get("refiner_provider") or pipeline["refiner"]["provider"])
    daemon_info: dict[str, Any] = {
        "online": daemon_online,
        "state": daemon_state,
        "pid": status_obj.get("pid") or status_snapshot.get("pid"),
        "version": str(status_obj.get("version") or status_snapshot.get("version") or ""),
        "build_id": str(status_obj.get("build_id") or status_snapshot.get("build_id") or ""),
        "code_drift": bool(drift and drift["drifted"]),
        "drift_reason": str(drift["reason"]) if (drift and drift["drifted"]) else "",
        "seq_id": status_obj.get("seq_id", 0),
        "pending_count": status_obj.get("pending_count", 0),
        "shim_ok": status_obj.get("shim_ok"),
        "asr_provider": eff_asr,
        "asr_model": pipeline["asr"]["model"],
        "refiner_provider": eff_ref,
        "refiner_model": pipeline["refiner"]["model"],
        "refiner_effort": pipeline["refiner"]["effort"],
    }
    if daemon_err := str(status_obj.get("error") or "").strip():
        daemon_info["error"] = redact_fn(daemon_err)

    payload: dict[str, Any] = {
        "version": __version__,
        "diag_elapsed_ms": elapsed_ms,
        "requested_last": last_n,
        "daemon": daemon_info,
        "pipeline": dict(pipeline),
        "summary": summary_stats,
        "session_match": sess_meta,
        "storage": {
            "runtime_dir_exists": rt_dir.is_dir(),
            "telemetry_kb": telemetry_kb,
            "failed_audio_count": len(failed_files),
            "newest_failed_at": newest_failed_ts,
            "legacy_codex_sqlite": legacy_sqlite,
        },
        "recent_results": annotated_records,
        "daemon_log_tail": log_tail,
    }
    return _redact_obj(payload, redact_fn)


def _fmt_time_short(ts_val: Any) -> str:
    if isinstance(ts_val, (int, float)) and ts_val > 0:
        return time.strftime("%H:%M:%S", time.localtime(float(ts_val)))
    return "-"


def _fmt_opt_int(val: Any) -> str:
    return str(val) if isinstance(val, int) and not isinstance(val, bool) else "-"


def format_live_diagnostics_markdown(payload: Mapping[str, Any]) -> str:
    """Render a redacted live diagnostics payload as readable Markdown."""
    d_info = payload["daemon"]
    sess_meta = payload["session_match"]
    storage = payload["storage"]
    stats = payload["summary"]
    annotated_records = payload["recent_results"]
    last_n = int(payload.get("requested_last") or len(annotated_records) or 10)

    if d_info.get("online"):
        status_label = "ONLINE"
    elif d_info.get("state") == "unresponsive":
        status_label = "UNRESPONSIVE"
    elif d_info.get("state") == "error":
        status_label = "ERROR"
    else:
        status_label = "STOPPED"

    lines = [
        f"# `agent-voice-input` Live Diagnostics (`v{payload['version']}`, `{payload['diag_elapsed_ms']}ms`)",
        "",
        f"- Daemon: `{status_label}` (state=`{d_info['state']}`, pid=`{d_info['pid'] or '-'}`, pending=`{d_info['pending_count']}`, shim_ok=`{d_info['shim_ok']}`)",
    ]
    if d_info.get("error"):
        lines.append(f"- Daemon Warning: `{d_info['error']}` (try `agent-voice-input restart`)")
    if d_info.get("code_drift"):
        lines.append(
            f"- Version/Code Drift: `{d_info.get('drift_reason') or 'mismatch'}` — run `agent-voice-input restart`"
        )
    pipeline_summary = str((payload.get("pipeline") or {}).get("summary") or "").strip()
    if pipeline_summary:
        lines.append(f"- Pipeline: {pipeline_summary}")
    else:
        lines.append(
            f"- Configured Pipeline: Stage 1 `{d_info['asr_provider']}/{d_info['asr_model']}` -> "
            f"Stage 2 `{d_info['refiner_provider']}/{d_info['refiner_model'] or '-'}` (`effort={d_info['refiner_effort']}`)"
        )

    if stats.get("total", 0) > 0:
        f_ms = lambda v: f"{v}ms" if v is not None else "-"
        hit_s = f"{stats['cache_hit_pct_avg']}%" if stats.get("cache_hit_pct_avg") is not None else "-"
        lines.append(
            f"- Recent Stats (`{stats['ok_count']}/{stats['total']}` ok, fallbacks=`{stats['fallback_count']}`, "
            f"failed/skipped=`{stats['failed_or_skipped']}`): "
            f"p50 total=`{f_ms(stats.get('total_ms_p50'))}` (ASR=`{f_ms(stats.get('asr_ms_p50'))}`, "
            f"Refine=`{f_ms(stats.get('refine_ms_p50'))}`), avg cache_hit=`{hit_s}`"
        )

    if sess_meta.get("matched"):
        lines.append(
            f"- Active Session Match: `{sess_meta['session_tag']}` via `{sess_meta['matched_by']}` "
            f"(turns=`{sess_meta['turns']}`, gen=`{sess_meta['generation']}`, dialog_chars=`{sess_meta['dialog_chars']}`, "
            f"jsonl=`{sess_meta['jsonl_kb']} KB`, hotwords=`{sess_meta['hotword_count']}`)"
        )
    else:
        err_sfx = f" ({sess_meta['error']})" if sess_meta.get("error") else ""
        lines.append(f"- Active Session Match: `none`{err_sfx}")

    failed_cnt = int(storage.get("failed_audio_count") or 0)
    newest_failed_ts = str(storage.get("newest_failed_at") or "")
    failed_note = f"`{failed_cnt}` (newest: `{newest_failed_ts}`)" if failed_cnt else "`0`"
    lines.append(
        f"- Runtime Storage: `telemetry.jsonl`=`{storage.get('telemetry_kb', 0.0)} KB` | `failed/` WAVs={failed_note}"
    )

    legacy_sqlite = storage.get("legacy_codex_sqlite")
    if legacy_sqlite is not None:
        if legacy_sqlite.get("readable") and legacy_sqlite.get("has_logs_table", True):
            lines.append(
                f"- Legacy `codex-home/logs_2.sqlite` (1h): processes=`{legacy_sqlite.get('recent_1h_processes', 0)}`, "
                f"WARN=`{legacy_sqlite.get('recent_1h_warns', 0)}`, ERROR=`{legacy_sqlite.get('recent_1h_errors', 0)}`"
            )
        else:
            lines.append("- Legacy `codex-home/logs_2.sqlite`: present (unreadable or empty schema)")

    lines.append("")
    lines.append(f"## Recent Utterances (last `{len(annotated_records)}` of requested `{last_n}`)")
    lines.append("")
    if not annotated_records:
        lines.append("_No utterance telemetry recorded yet._")
    else:
        lines.append(
            "| Seq | Time | Session | Audio(s) | Sync | ASR | Init | Refine | Deliv | Total(ms) | Ctx(ch/chr) | In / Cached (Hit%) | Rsn | Flags |"
        )
        lines.append(
            "| ---: | :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :--- |"
        )
        for rec in annotated_records:
            in_t = rec.get("input_tokens")
            c_t = rec.get("cached_input_tokens")
            if isinstance(in_t, int) and in_t > 0 and isinstance(c_t, int):
                tok_col = f"{in_t} / {c_t} ({100.0 * c_t / in_t:.1f}%)"
            elif isinstance(in_t, int):
                tok_col = f"{in_t} / -"
            else:
                tok_col = "-"
            flags_str = ", ".join(rec.get("diag_flags") or []) or "OK"
            if warn := str(rec.get("refiner_warning") or rec.get("error") or "").strip():
                flags_str += f" `{warn[:48]}`"
            raw_sec = float(rec.get("raw_audio_sec") or 0.0)
            voiced_sec = float(rec.get("voiced_audio_sec") or 0.0)
            trimmed_sec = float(rec.get("silence_trimmed_sec") or 0.0)
            audio_col = (
                f"{voiced_sec:.1f}/{raw_sec:.1f}→{max(0.0, raw_sec - trimmed_sec):.1f}"
                if trimmed_sec > 0.0
                else f"{voiced_sec:.1f}/{raw_sec:.1f}"
            )
            lines.append(
                f"| {rec.get('seq_id', '-')} | {_fmt_time_short(rec.get('ts'))} | `{rec.get('session_tag')}` | "
                f"{audio_col} | "
                f"{int(rec.get('sync_ms') or 0)} | {int(rec.get('asr_ms') or 0)} | "
                f"{int(rec.get('refine_init_ms') or 0)} | {int(rec.get('refine_ms') or 0)} | "
                f"{int(rec.get('deliver_ms') or 0)} | {int(rec.get('total_ms') or 0)} | "
                f"{int(rec.get('ctx_chunks') or 0)}/{int(rec.get('ctx_chars') or 0)} | "
                f"{tok_col} | {_fmt_opt_int(rec.get('reasoning_tokens'))} | {flags_str} |"
            )

    if log_tail := str(payload.get("daemon_log_tail") or "").strip():
        lines.extend(["", "## Recent `daemon.log` Tail (Redacted)", "", "```text", log_tail, "```"])
    return "\n".join(lines)


def run_diagnose_command(
    cfg: Config,
    env: Mapping[str, str],
    *,
    runtime_dir: str = "",
    window_title: str = "",
    last: int = 10,
    as_json: bool = False,
) -> int:
    """Execute read-only live diagnostics and print Markdown or JSON to stdout."""
    rt_path = Path(runtime_dir).expanduser().resolve() if runtime_dir.strip() else None
    payload = collect_live_diagnostics(
        cfg,
        env,
        runtime_dir=rt_path,
        window_title=window_title,
        last=last,
    )
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_live_diagnostics_markdown(payload))
    return 0
