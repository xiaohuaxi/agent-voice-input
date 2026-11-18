#!/usr/bin/env python3
"""Sub-second read-only live diagnostics for agent-voice-input runtime_dir.

Summarizes daemon health, recent telemetry.jsonl / in-memory segmented latencies &
prefix-cache hit rates, active CLI session match metadata (zero prose), staged
failed/ audio count, and optional legacy codex-home/logs_2.sqlite counters.
Works cleanly with or without codex-home/logs_2.sqlite.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any
import urllib.parse

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SRC_ROOT = _REPO_ROOT / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from agent_voice_input import __version__  # noqa: E402
from agent_voice_input.audio_proc import list_failed_audios  # noqa: E402
from agent_voice_input.cli import _read_redacted_log_tail  # noqa: E402
from agent_voice_input.config import (  # noqa: E402
    get_runtime_dir,
    load_config,
    redact_secrets,
    resolve_credentials,
    select_asr_provider,
)
from agent_voice_input.daemon import (  # noqa: E402
    TELEMETRY_FILENAME,
    read_telemetry_history,
    send_control_command,
)
from agent_voice_input.session_ctx import SessionTracker, format_turns_text  # noqa: E402


def _mask_session_key(skey: str) -> str:
    raw = (skey or "").strip()
    if not raw:
        return "none"
    if ":" in raw:
        cli, sid = raw.split(":", 1)
    else:
        cli, sid = "sess", raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{cli}#{digest}"


def _diagnose_record_flags(rec: dict[str, Any], *, is_first_for_session: bool) -> list[str]:
    flags: list[str] = []
    if not rec.get("ok", False):
        reason = str(rec.get("skipped_reason") or "error")
        flags.append(f"SKIP({reason})")
        return flags
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
    if isinstance(rsn_tok, int) and rsn_tok >= 256:
        flags.append("REASONING_BLOAT")
    return flags


def _probe_optional_legacy_codex_sqlite(rt_dir: Path) -> dict[str, Any] | None:
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


def _fmt_time_short(ts_val: Any) -> str:
    if isinstance(ts_val, (int, float)) and ts_val > 0:
        return time.strftime("%H:%M:%S", time.localtime(float(ts_val)))
    return "-"


def _fmt_opt_int(val: Any) -> str:
    return str(val) if isinstance(val, int) and not isinstance(val, bool) else "-"


def main(argv: list[str] | None = None) -> int:
    t0 = time.monotonic()
    ap = argparse.ArgumentParser(description="Sub-second read-only live voice-input diagnostics.")
    ap.add_argument("--config", help="Optional path to config.toml")
    ap.add_argument("--runtime-dir", default="", help="Override runtime_dir path")
    ap.add_argument("--window-title", default="", help="Optional window title hint for session matching check")
    ap.add_argument("--last", type=int, default=10, help="Number of recent utterances to inspect (default: 10)")
    ap.add_argument("--json", action="store_true", help="Emit machine-readable redacted JSON instead of Markdown")
    args = ap.parse_args(argv)

    cfg, env = load_config(config_path=args.config or None)
    creds = resolve_credentials(cfg, env)
    secrets = creds.all_secret_values()
    redact_fn = lambda s: redact_secrets(str(s), env, cfg=cfg, extra_secrets=secrets)

    rt_dir = Path(args.runtime_dir).expanduser().resolve() if args.runtime_dir else get_runtime_dir(env, create=False)
    last_n = max(1, min(100, int(args.last)))

    # 1. Daemon control status + history (with offline fallback to telemetry.jsonl)
    daemon_online = False
    status_obj: dict[str, Any] = {}
    records: list[dict[str, Any]] = []
    try:
        if rt_dir.is_dir():
            status_obj = send_control_command(rt_dir, {"cmd": "status", "history": last_n}, timeout_sec=0.35)
            daemon_online = bool(status_obj.get("ok"))
            raw_recs = status_obj.get("recent_results")
            if isinstance(raw_recs, list):
                records = [r for r in raw_recs if isinstance(r, dict)]
    except Exception:
        daemon_online = False

    if not records and rt_dir.is_dir():
        records = read_telemetry_history(rt_dir, limit=last_n)

    # 2. Fast local session resolution check (tmux / proc / registry, zero powershell spawn, zero prose output)
    sess_meta: dict[str, Any] = {"matched": False}
    try:
        tracker = SessionTracker(env=env, runtime_dir=rt_dir if rt_dir.is_dir() else None)
        snap = tracker.sync_session(window_title=args.window_title)
        if snap is not None:
            j_size = 0
            try:
                j_size = os.path.getsize(snap.match.jsonl_path)
            except OSError:
                j_size = 0
            sess_meta = {
                "matched": True,
                "session_tag": _mask_session_key(snap.session_key),
                "matched_by": snap.match.matched_by,
                "turns": len(snap.clean_turns),
                "generation": snap.generation,
                "dialog_chars": len(format_turns_text(snap.clean_turns, max_chars=250_000)),
                "jsonl_kb": round(j_size / 1024.0, 1),
                "hotword_count": len(snap.hotwords),
            }
    except Exception as exc:
        sess_meta = {"matched": False, "error": redact_fn(str(exc))}

    # 3. Staged failed/ recordings & redacted daemon.log tail & optional legacy sqlite
    failed_files = list_failed_audios(rt_dir) if rt_dir.is_dir() else []
    newest_failed_ts = ""
    if failed_files:
        try:
            newest_failed_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(failed_files[0].stat().st_mtime))
        except OSError:
            newest_failed_ts = "-"

    telemetry_path = rt_dir / TELEMETRY_FILENAME
    telemetry_kb = 0.0
    if telemetry_path.is_file() and not telemetry_path.is_symlink():
        try:
            telemetry_kb = round(telemetry_path.stat().st_size / 1024.0, 1)
        except OSError:
            telemetry_kb = 0.0

    log_tail = _read_redacted_log_tail(rt_dir / "daemon.log", -1, env, cfg=cfg, max_bytes=1536, max_lines=8) if rt_dir.is_dir() else ""
    legacy_sqlite = _probe_optional_legacy_codex_sqlite(rt_dir) if rt_dir.is_dir() else None

    seen_skeys: set[str] = set()
    annotated_records: list[dict[str, Any]] = []
    for rec in records:
        skey = str(rec.get("session_key") or "").strip()
        is_first = bool(skey and skey not in seen_skeys)
        if skey:
            seen_skeys.add(skey)
        clean_rec: dict[str, Any] = {}
        for k, v in rec.items():
            if k in ("session_key", "saved_audio"):
                continue
            clean_rec[k] = redact_fn(v) if (isinstance(v, str) and v) else v
        flags_input = {**clean_rec, "session_key": skey}
        flags = _diagnose_record_flags(flags_input, is_first_for_session=is_first)
        clean_rec["has_saved_audio"] = bool(str(rec.get("saved_audio") or "").strip())
        clean_rec["session_tag"] = _mask_session_key(skey)
        clean_rec["diag_flags"] = [redact_fn(fl) for fl in flags]
        annotated_records.append(clean_rec)

    elapsed_ms = max(0, int(round((time.monotonic() - t0) * 1000)))
    eff_asr = status_obj.get("asr_provider") or select_asr_provider(cfg, creds)
    eff_ref = status_obj.get("refiner_provider") or cfg.refiner.provider

    payload = {
        "version": __version__,
        "diag_elapsed_ms": elapsed_ms,
        "daemon": {
            "online": daemon_online,
            "state": status_obj.get("state", "stopped"),
            "pid": status_obj.get("pid"),
            "seq_id": status_obj.get("seq_id", 0),
            "pending_count": status_obj.get("pending_count", 0),
            "shim_ok": status_obj.get("shim_ok"),
            "asr_provider": eff_asr,
            "asr_model": cfg.asr.effective_model(str(eff_asr)),
            "refiner_provider": eff_ref,
            "refiner_model": cfg.refiner.effective_model(str(eff_ref)) if str(eff_ref) != "off" else "",
            "refiner_effort": cfg.refiner.effort,
        },
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

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    d_info = payload["daemon"]
    lines = [
        f"# `agent-voice-input` Live Diagnostics (`v{__version__}`, `{elapsed_ms}ms`)",
        "",
        f"- Daemon: `{'ONLINE' if daemon_online else 'STOPPED'}` (state=`{d_info['state']}`, pid=`{d_info['pid'] or '-'}`, pending=`{d_info['pending_count']}`, shim_ok=`{d_info['shim_ok']}`)",
        f"- Configured Pipeline: Stage 1 `{d_info['asr_provider']}/{d_info['asr_model']}` -> Stage 2 `{d_info['refiner_provider']}/{d_info['refiner_model'] or '-'}` (`effort={d_info['refiner_effort']}`)",
    ]
    if sess_meta.get("matched"):
        lines.append(
            f"- Active Session Match: `{sess_meta['session_tag']}` via `{sess_meta['matched_by']}` "
            f"(turns=`{sess_meta['turns']}`, gen=`{sess_meta['generation']}`, dialog_chars=`{sess_meta['dialog_chars']}`, jsonl=`{sess_meta['jsonl_kb']} KB`, hotwords=`{sess_meta['hotword_count']}`)"
        )
    else:
        err_sfx = f" ({sess_meta['error']})" if sess_meta.get("error") else ""
        lines.append(f"- Active Session Match: `none`{err_sfx}")

    failed_note = f"`{len(failed_files)}` (newest: `{newest_failed_ts}`)" if failed_files else "`0`"
    lines.append(f"- Runtime Storage: `telemetry.jsonl`=`{telemetry_kb} KB` | `failed/` WAVs={failed_note}")
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
                flags_str += f" `{redact_fn(warn)[:48]}`"
            lines.append(
                f"| {rec.get('seq_id', '-')} | {_fmt_time_short(rec.get('ts'))} | `{rec.get('session_tag')}` | "
                f"{float(rec.get('voiced_audio_sec') or 0.0):.1f}/{float(rec.get('raw_audio_sec') or 0.0):.1f} | "
                f"{int(rec.get('sync_ms') or 0)} | {int(rec.get('asr_ms') or 0)} | "
                f"{int(rec.get('refine_init_ms') or 0)} | {int(rec.get('refine_ms') or 0)} | "
                f"{int(rec.get('deliver_ms') or 0)} | {int(rec.get('total_ms') or 0)} | "
                f"{int(rec.get('ctx_chunks') or 0)}/{int(rec.get('ctx_chars') or 0)} | "
                f"{tok_col} | {_fmt_opt_int(rec.get('reasoning_tokens'))} | {flags_str} |"
            )

    if log_tail:
        lines.extend(["", "## Recent `daemon.log` Tail (Redacted)", "", "```text", log_tail, "```"])

    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
