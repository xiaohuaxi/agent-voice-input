#!/usr/bin/env python3
"""Point-in-time historical recording replay & A/B evaluation for agent-voice-input."""
from __future__ import annotations

import argparse, datetime, hashlib, json, math, os, re, statistics, sys, tempfile, time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

_SRC_ROOT = Path(__file__).resolve().parents[4] / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from agent_voice_input import __version__  # noqa: E402
from agent_voice_input.asr_providers import AsrResult, create_asr_provider, load_hotwords_file  # noqa: E402
from agent_voice_input.audio_proc import _write_temp_0600, list_saved_recordings, process_wav  # noqa: E402
from agent_voice_input.config import ensure_secure_dir, expand_user_path, get_runtime_dir, load_config, redact_secrets, resolve_credentials, resolve_saved_recordings_dir, select_asr_provider  # noqa: E402
from agent_voice_input.daemon import _call_refiner_refine, _may_run_stage2_refiner, _transcribe_wav_chunks, read_telemetry_history  # noqa: E402
from agent_voice_input.refiner import create_refiner  # noqa: E402
from agent_voice_input.session_ctx import SessionMatch, SessionTracker, _clean_agy_record, _clean_claude_record, _collect_claude_storage_matches, _collect_codex_storage_matches, _extract_agy_transcript_cwd, _extract_codex_item_completed, _extract_codex_response_item, _is_agy_transcript_subagent, _resolve_agy_transcript_path  # noqa: E402

_REPO_ROOT = _SRC_ROOT.parent
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_REC_STAMP_RE = re.compile(r"^rec-(\d{8}-\d{6})-")
_LEAKED_MARKERS = ("<asr_draft_to_clean>", "</asr_draft_to_clean>", "<session_reference_log", "<task_reminder>", "[Speaker]", "[CLI Agent]", "```", "】【")
_TRAILING_FILLER_RE = re.compile(
    r"(?:^|[。！？；.!?\n])[ \t]*(?:嗯+|呃+|啊+|哦+|噢+|唉+)[ \t]*(?:[，,。！？；.!?\n]|$)"
    r"|(?:对[，,]?\s*(?:主要|基本|大概)?就是(?:这样|这些)|我的意思就是这样|(?:主要|大概|基本)就是这些|就先这样吧?|就说这么多|(?:就这一点)?我再补充一下(?:[。.]?\s*嗯)?|不对不对|等一下等一下|算了算了|(?:呃|哦)[，,]?不对)"
)
_KNOWN_HOMOPHONE_ERROR_RE = re.compile(
    r"(?:(?<![A-Za-z0-9])(?:To\s*B\s*X|to\s*bx|Coke\s*X|Code\s+X)(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])Lula(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])Flash\s*(?:Net|Light|Night)(?![A-Za-z0-9])"
    r"|Kimi\s*的\s*Flash|离的意思"
    r"|点\s*(?:py|jsonl|json|toml|md|sh|ps1|swift|ts|tsx|js)(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])json\s+l(?![A-Za-z0-9]))",
    re.IGNORECASE,
)
_CANONICAL_TECH_TERMS = (
    "Codex", "Claude Code", "Claude", "Gemini", "DashScope", "OpenRouter", "Luna", "Flash-Lite",
    "Antigravity", "agy", "Ghostty", "Opus", "Sonnet", "Haiku", "Qwen", "DeepSeek", "Doubao", "豆包", "Groq", "Whisper",
    "Astra", "Sol", "CLI", "竞态",
    "subagent", "worktree", "rebase", "cherry-pick", "JSONL", "TOML", "YAML", "WSL2", "tmux", "ffmpeg", "RControl",
    "SessionTracker", "CodexRefiner", "VoiceInputDaemon", "refiner.py", "session_ctx.py", "daemon.py",
    "asr_providers.py", "config.toml", "AGENTS.md", "SKILL.md", "TODO.md", "ROADMAP.md",
    "win_shim.ps1", "mac_shim.swift", "gpt-6-luna", "qwen3-asr-flash", "prompt_cache_key", "telemetry.jsonl",
)


def _validate_tag(tag: str) -> str:
    if not _TAG_RE.match(tag.strip()):
        raise ValueError(f"invalid tag {tag!r} (allowed: [A-Za-z0-9_.-], max 64 chars)")
    return tag.strip()

def _atomic_write_0600(path: Path, content: str | bytes) -> None:
    parent = ensure_secure_dir(path.parent)
    if path.is_symlink():
        raise RuntimeError(f"refusing to overwrite symlink: {path.name}")
    tmp = parent / f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    try:
        _write_temp_0600(tmp, content.encode("utf-8") if isinstance(content, str) else bytes(content))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)

def _resolve_eval_dir(env: Mapping[str, str], explicit: str = "", *, repo_root: Path = _REPO_ROOT) -> Path:
    if explicit.strip():
        target = expand_user_path(explicit.strip(), env=env)
    else:
        cache_home = (env.get("XDG_CACHE_HOME") or "").strip()
        base = expand_user_path(cache_home, env=env) if cache_home else expand_user_path("~/.cache", env=env)
        target = base / "agent-voice-input" / "eval-runs"
    resolved_target = target.resolve(strict=False)
    resolved_repo = repo_root.resolve(strict=False)
    if resolved_target == resolved_repo or resolved_repo in resolved_target.parents:
        raise ValueError(f"refusing to place eval output inside repository: {target}")
    return ensure_secure_dir(target)

def _mask_session_key(skey: str) -> str:
    raw = (skey or "").strip()
    return f"{raw.split(':', 1)[0] if ':' in raw else 'sess'}#{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:8]}" if raw else "none"

def _display_path(path: Path, home: Path) -> str:
    s, h = str(path), str(home).rstrip("/\\")
    return ("~" + s[len(h) :]) if (h and s.startswith(h + os.sep)) else s

def _load_json_dict(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        return obj if isinstance(obj := json.loads(path.read_text(encoding="utf-8")), dict) else {}
    except (OSError, ValueError):
        return {}

def _percentile(vals: Sequence[float], pct: float) -> float | None:
    if not vals:
        return None
    s = sorted(float(v) for v in vals)
    k = (len(s) - 1) * (pct / 100.0)
    f, c = math.floor(k), math.ceil(k)
    return s[f] + (k - f) * (s[c] - s[f])


def extract_jsonl_record_ts(obj: Mapping[str, Any]) -> float | None:
    cand = next((obj.get(k) for k in ("created_at", "timestamp", "createdAt", "ts") if obj.get(k) is not None), None)
    if cand is None and isinstance(pl := obj.get("payload"), Mapping):
        cand = next((pl.get(k) for k in ("timestamp", "created_at", "ts") if pl.get(k) is not None), None)
    if isinstance(cand, (int, float)) and not isinstance(cand, bool):
        return float(cand) / 1000.0 if cand > 1e11 else float(cand)
    if isinstance(cand, str) and cand.strip():
        s = cand.strip()
        try:
            dt = datetime.datetime.fromisoformat((s[:-1] + "+00:00") if s.endswith(("Z", "z")) else s)
            return (dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)).timestamp()
        except ValueError:
            return None
    return None

def _extract_clean_turn(cli: str, obj: Mapping[str, Any]) -> tuple[str, str] | None:
    if cli == "agy":
        return _clean_agy_record(obj)
    if cli == "claude":
        return _clean_claude_record(obj)
    if cli == "codex" and isinstance(pl := obj.get("payload"), Mapping):
        if obj.get("type") == "event_msg" and pl.get("type") == "item_completed":
            return _extract_codex_item_completed(pl)
        if obj.get("type") == "response_item":
            return _extract_codex_response_item(pl)
    return None


def truncate_session_jsonl_point_in_time(
    src_jsonl: Path, *, cli: str, cutoff_ts: float, stop_ts: float, dst_jsonl: Path, submit_window_sec: float = 1800.0
) -> tuple[int, str, float | None]:
    """Write strictly pre-cutoff (< cutoff_ts) JSONL history to dst_jsonl (0600) and return post-stop user ref."""
    kept, seen_post_cutoff, submitted_user, submitted_ts = [], False, "", None
    min_submit_ts = max(cutoff_ts, stop_ts - 2.0)
    with open(src_jsonl, "r", encoding="utf-8", errors="replace") as f:
        for raw_line in f:
            if not raw_line.strip():
                continue
            try:
                obj = json.loads(raw_line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            rts = extract_jsonl_record_ts(obj)
            if (rts is None or rts < cutoff_ts) and not seen_post_cutoff:
                kept.append(raw_line if raw_line.endswith("\n") else raw_line + "\n")
                continue
            if rts is None:
                continue
            seen_post_cutoff = True
            if submitted_ts is None and min_submit_ts <= rts <= (stop_ts + submit_window_sec):
                turn = _extract_clean_turn(cli, obj)
                if turn is not None and turn[0] == "user" and turn[1].strip():
                    submitted_user, submitted_ts = turn[1].strip(), rts
    _atomic_write_0600(dst_jsonl, "".join(kept))
    return len(kept), submitted_user, submitted_ts


def _index_root_sessions(home: Path) -> dict[str, tuple[str, str, Path, str]]:
    out: dict[str, tuple[str, str, Path, str]] = {}
    brain = home / ".gemini" / "antigravity-cli" / "brain"
    if brain.is_dir():
        for entry in brain.iterdir():
            tp = _resolve_agy_transcript_path(home, entry.name) if entry.is_dir() else None
            if tp is not None and tp.is_file() and not _is_agy_transcript_subagent(tp):
                sid = entry.name.lower()
                out[f"agy:{sid}"] = ("agy", sid, tp, _extract_agy_transcript_cwd(tp))
    for m in (*_collect_codex_storage_matches(home, matched_by="replay"), *_collect_claude_storage_matches(home, matched_by="replay", allow_empty_global=True)):
        if m.jsonl_path and os.path.isfile(m.jsonl_path):
            out[m.session_key] = (m.cli, m.session_id, Path(m.jsonl_path), m.cwd)
    return out

def _find_fallback_session_by_ts(session_idx: Mapping[str, tuple[str, str, Path, str]], cutoff_ts: float, stop_ts: float) -> str:
    best_key, best_delta, min_ts = "", 1e9, max(cutoff_ts, stop_ts - 2.0)
    for skey, (cli_k, _, path, _) in session_idx.items():
        try:
            if path.stat().st_mtime < cutoff_ts - 5.0:
                continue
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for raw_line in f:
                    if raw_line.strip() and isinstance(obj := json.loads(raw_line), dict):
                        rts = extract_jsonl_record_ts(obj)
                        if rts is not None and min_ts <= rts <= stop_ts + 900.0:
                            turn = _extract_clean_turn(cli_k, obj)
                            if turn and turn[0] == "user" and (rts - min_ts) < best_delta:
                                best_delta, best_key = (rts - min_ts), skey
                                break
        except (OSError, ValueError):
            continue
    return best_key

def _estimate_rec_stop_ts(rec_path: Path, mtime: float) -> float:
    if m := _REC_STAMP_RE.match(rec_path.name):
        try:
            stamp_ts = time.mktime(time.strptime(m.group(1), "%Y%m%d-%H%M%S"))
            if 0.0 <= (mtime - stamp_ts) <= 30.0:
                return min(mtime, stamp_ts + 0.999)
        except ValueError:
            pass
    return mtime

def _match_telemetry(rec_items: Sequence[tuple[Path, float, float]], telemetry: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    used_idx, matched = set(), {}
    for rec_path, mtime, dur_sec in rec_items:
        best_i, best_score = -1, 1e9
        for idx, tel in enumerate(telemetry):
            t_ts, t_dur = tel.get("ts"), tel.get("raw_audio_sec")
            if idx in used_idx or not isinstance(t_ts, (int, float)) or not isinstance(t_dur, (int, float)):
                continue
            dur_diff, time_diff = abs(float(t_dur) - dur_sec), abs((float(t_ts) - float(tel.get("total_ms") or 0) / 1000.0) - mtime)
            if dur_diff <= 0.5 and time_diff <= 15.0 and (dur_diff * 10.0 + time_diff) < best_score:
                best_score, best_i = dur_diff * 10.0 + time_diff, idx
        if best_i >= 0:
            used_idx.add(best_i)
            matched[rec_path.name] = dict(telemetry[best_i])
    return matched


def make_asr_cache_key(
    rec_sha1: str,
    asr_provider: str,
    asr_model: str,
    hotwords: Sequence[str],
    *,
    git_status: str = "",
    dialog_text: str = "",
) -> tuple[str, str]:
    raw_ctx = "\n".join(hotwords)
    if git_status or dialog_text:
        raw_ctx = f"{raw_ctx}\0{git_status}\0{dialog_text[-2500:]}"
    hw_sha1 = hashlib.sha1(raw_ctx.encode("utf-8")).hexdigest()
    return f"{rec_sha1}:{asr_provider}:{asr_model}:{hw_sha1}", hw_sha1


def _contains_tech_term(text: str, term: str) -> bool:
    if not term or term not in text:
        return False
    return bool(re.search(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])", text))


def evaluate_sample_metrics(asr_text: str, final_text: str, *, session_hotwords: Sequence[str] = (), used_fallback: str = "") -> dict[str, Any]:
    draft, out = asr_text.strip(), final_text.strip()
    asr_len, final_len = len(draft), len(out)
    q_hijack = ("?" in draft or "？" in draft) and not ("?" in out or "？" in out) and final_len > int(asr_len * 1.4) + 20
    term_pool = dict.fromkeys([*_CANONICAL_TECH_TERMS, *(w for w in session_hotwords if len(w) >= 3)])
    return {
        "asr_chars": asr_len, "final_chars": final_len, "char_retention": round(final_len / asr_len, 4) if asr_len > 0 else 0.0,
        "has_newline": ("\n" in out), "paragraph_count": max(1, sum(1 for p in out.splitlines() if p.strip())) if out else 0,
        "filler_residue_hits": len(_TRAILING_FILLER_RE.findall(out)),
        "canonical_tech_hits": sum(1 for term in term_pool if _contains_tech_term(out, term)),
        "homophone_error_hits": len(_KNOWN_HOMOPHONE_ERROR_RE.findall(out)),
        "hard_gate_fail": bool(any(m in out for m in _LEAKED_MARKERS) or q_hijack),
        "is_fallback": bool(used_fallback and used_fallback != "single_hop_passthrough"),
    }


def aggregate_run_metrics(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    valid = [s for s in samples if s.get("ok")]
    long_s = [s for s in valid if float(s.get("duration_sec") or 0.0) >= 10.0]
    asr_ms = [int(s["asr_ms"]) for s in valid if isinstance(s.get("asr_ms"), (int, float))]
    ref_ms = [int(s["refine_ms"]) for s in valid if isinstance(s.get("refine_ms"), (int, float))]
    long_ref_ms = [int(s["refine_ms"]) for s in long_s if isinstance(s.get("refine_ms"), (int, float))]
    warm_hits = [float(s["cache_hit_pct"]) for s in valid if s.get("is_warm") and isinstance(s.get("cache_hit_pct"), (int, float))]
    ret = [float(s["char_retention"]) for s in valid if int(s.get("asr_chars") or 0) > 0]
    long_ret = [float(s["char_retention"]) for s in long_s if int(s.get("asr_chars") or 0) > 0]
    r1 = lambda v: round(v, 1) if v is not None else None
    return {
        "total_recordings": len(samples), "valid_samples": len(valid), "long_samples": len(long_s),
        "skipped_samples": len(samples) - len(valid), "asr_cache_hits": sum(1 for s in samples if s.get("asr_cached")),
        "asr_ms_p50": r1(_percentile(asr_ms, 50)), "asr_ms_p90": r1(_percentile(asr_ms, 90)),
        "refine_ms_p50": r1(_percentile(ref_ms, 50)), "refine_ms_p90": r1(_percentile(ref_ms, 90)),
        "long_refine_ms_p50": r1(_percentile(long_ref_ms, 50)), "warm_cache_hit_pct_p50": r1(_percentile(warm_hits, 50)),
        "char_retention_p50": round(_percentile(ret, 50) * 100.0, 1) if ret else None,
        "long_char_retention_p50": round(_percentile(long_ret, 50) * 100.0, 1) if long_ret else None,
        "multiline_rate_pct": round(100.0 * statistics.mean(1.0 if s.get("has_newline") else 0.0 for s in valid), 1) if valid else 0.0,
        "long_multiline_rate_pct": round(100.0 * statistics.mean(1.0 if s.get("has_newline") else 0.0 for s in long_s), 1) if long_s else 0.0,
        "filler_residue_hits": sum(int(s.get("filler_residue_hits") or 0) for s in valid),
        "filler_residue_samples": sum(1 for s in valid if int(s.get("filler_residue_hits") or 0) > 0),
        "canonical_tech_hits": sum(int(s.get("canonical_tech_hits") or 0) for s in valid),
        "homophone_error_hits": sum(int(s.get("homophone_error_hits") or 0) for s in valid),
        "fallback_count": sum(1 for s in valid if s.get("is_fallback")),
        "hard_gate_fails": sum(1 for s in valid if s.get("hard_gate_fail")),
    }

def _summary_rows(a: Mapping[str, Any]) -> list[tuple[str, str]]:
    f = lambda v, s="": "-" if v is None else f"{v}{s}"
    return [
        ("Valid / Total / Long (`>=10s`)", f"{a['valid_samples']} / {a['total_recordings']} / {a['long_samples']}"),
        ("Stage 1 ASR cache hits", f"{a['asr_cache_hits']} / {a['total_recordings']}"),
        ("Stage 1 `asr_ms` (p50 / p90)", f"{f(a['asr_ms_p50'])} / {f(a['asr_ms_p90'])} ms"),
        ("Stage 2 `refine_ms` (p50 / p90 / `>=10s` p50)", f"{f(a['refine_ms_p50'])} / {f(a['refine_ms_p90'])} / {f(a['long_refine_ms_p50'])} ms"),
        ("Warm `cache_hit_rate` p50", f(a["warm_cache_hit_pct_p50"], "%")),
        ("Char retention `final/asr` p50 (all / `>=10s`)", f"{f(a['char_retention_p50'], '%')} / {f(a['long_char_retention_p50'], '%')}"),
        ("Paragraph split rate (all / `>=10s`)", f"{f(a['multiline_rate_pct'], '%')} / {f(a['long_multiline_rate_pct'], '%')}"),
        ("Trailing filler residue (hits / samples)", f"{a['filler_residue_hits']} / {a['filler_residue_samples']}"),
        ("Canonical tech-term hits / Homophone errors", f"{a['canonical_tech_hits']} / {a['homophone_error_hits']}"),
        ("Fallbacks / Hard-gate fails", f"{a['fallback_count']} / {a['hard_gate_fails']}"),
    ]


def _rescore_paired_sample(s: Mapping[str, Any], shared_hw: Sequence[str]) -> dict[str, Any]:
    d = dict(s)
    if d.get("ok") and "asr_draft" in d and "refined_text" in d:
        d.update(
            evaluate_sample_metrics(
                str(d.get("asr_draft") or ""),
                str(d.get("refined_text") or ""),
                session_hotwords=shared_hw,
                used_fallback=str(d.get("used_fallback") or ""),
            )
        )
    return d


def compare_runs(
    base_data: Mapping[str, Any], cand_data: Mapping[str, Any], *,
    diff_out_path: Path | None = None, redact_fn: Callable[[str], str] = str, diff_disp: str = "",
) -> str:
    b_tag, c_tag = str(base_data.get("tag") or "base"), str(cand_data.get("tag") or "cand")
    b_map = {str(s["recording_sha1"]): s for s in base_data.get("samples", ()) if isinstance(s, Mapping) and s.get("recording_sha1")}
    c_map = {str(s["recording_sha1"]): s for s in cand_data.get("samples", ()) if isinstance(s, Mapping) and s.get("recording_sha1")}
    keys = [k for k in b_map if k in c_map]
    do_rescore = "meta" in base_data or "meta" in cand_data
    b_scored, c_scored = [], []
    for k in keys:
        bs, cs = b_map[k], c_map[k]
        if do_rescore:
            hw = tuple(cs.get("session_hotwords") or bs.get("session_hotwords") or ())
            bs, cs = _rescore_paired_sample(bs, hw), _rescore_paired_sample(cs, hw)
        b_scored.append(bs)
        c_scored.append(cs)
    b_rows, c_rows = _summary_rows(aggregate_run_metrics(b_scored)), _summary_rows(aggregate_run_metrics(c_scored))
    if diff_out_path is not None:
        md = [f"# Detailed Diff: `{b_tag}` vs `{c_tag}` (paired={len(keys)})", ""]
        for idx, k in enumerate(keys, 1):
            bs, cs = b_map[k], c_map[k]
            state = "CHANGED" if bs.get("refined_text", "") != cs.get("refined_text", "") else "IDENTICAL"
            md.extend([
                f"## #{idx:02d} `{bs.get('recording_name')}` ({bs.get('duration_sec')}s, `{bs.get('session_tag')}`, {state})",
                f"- Base (`{bs.get('refine_ms')}ms`, ret=`{bs.get('char_retention')}`): {redact_fn(str(bs.get('refined_text') or ''))}",
                f"- Cand (`{cs.get('refine_ms')}ms`, ret=`{cs.get('char_retention')}`): {redact_fn(str(cs.get('refined_text') or ''))}",
                f"- ASR Draft: {redact_fn(str(bs.get('asr_draft') or cs.get('asr_draft') or ''))}",
                f"- Submitted User Ref: {redact_fn(str(bs.get('submitted_user_ref') or cs.get('submitted_user_ref') or '-'))}", "",
            ])
        _atomic_write_0600(diff_out_path, "\n".join(md))
    out = [f"# Replay A/B Comparison: `{b_tag}` vs `{c_tag}` (paired=`{len(keys)}`)", ""]
    if diff_disp:
        out.extend([f"- Detailed Diff (`0600`): `{diff_disp}`", ""])
    out.extend([f"| Metric | `{b_tag}` | `{c_tag}` |", "| :--- | ---: | ---: |"])
    out.extend(f"| {lbl} | `{bv}` | `{cv}` |" for (lbl, bv), (_, cv) in zip(b_rows, c_rows))
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Point-in-time historical recording replay & A/B evaluator.")
    for flag, kw in (
        ("--config", {"default": ""}), ("--tag", {"default": "run_latest"}),
        ("--compare", {"nargs": 2, "metavar": ("BASE_TAG", "CAND_TAG")}),
        ("--manifest-from", {"default": ""}), ("--asr-from", {"default": ""}),
        ("--use-asr-cache", {"dest": "use_asr_cache", "action": "store_true", "default": True}),
        ("--no-asr-cache", {"dest": "use_asr_cache", "action": "store_false"}), ("--refresh-asr", {"action": "store_true"}),
        ("--min-duration", {"type": float, "default": 0.0}), ("--limit", {"type": int, "default": 0}),
        ("--asr-provider", {"default": ""}), ("--asr-model", {"default": ""}),
        ("--refiner-provider", {"default": ""}), ("--refiner-model", {"default": ""}),
        ("--refiner-api", {"default": ""}), ("--refiner-effort", {"default": ""}),
        ("--max-chars", {"type": int, "default": 0}), ("--no-git-status", {"action": "store_true"}),
        ("--recordings-dir", {"default": ""}), ("--eval-dir", {"default": ""}),
        ("--host-runtime-dir", {"default": ""}), ("--home-dir", {"default": ""}),
    ):
        ap.add_argument(flag, **kw)
    args = ap.parse_args(argv)

    base_cfg, raw_env = load_config(config_path=args.config or None, home_dir=args.home_dir or None)
    env = {k: v for k, v in raw_env.items() if not ("FAST" in k.upper() and "CODEX" in k.upper())}
    home = Path(args.home_dir).expanduser().resolve() if args.home_dir else expand_user_path("~", env=env)
    eval_dir = _resolve_eval_dir(env, args.eval_dir)
    secrets = resolve_credentials(base_cfg, env).all_secret_values()
    redact_fn = lambda s: redact_secrets(str(s), env, cfg=base_cfg, extra_secrets=secrets)

    if args.compare:
        b_tag, c_tag = _validate_tag(args.compare[0]), _validate_tag(args.compare[1])
        b_data, c_data = _load_json_dict(eval_dir / f"{b_tag}.json"), _load_json_dict(eval_dir / f"{c_tag}.json")
        if not b_data or not c_data:
            raise SystemExit(f"missing run file(s) in {_display_path(eval_dir, home)} for {b_tag!r} / {c_tag!r}")
        diff_p = eval_dir / f"diff_{b_tag}_vs_{c_tag}.md"
        print(compare_runs(b_data, c_data, diff_out_path=diff_p, redact_fn=redact_fn, diff_disp=_display_path(diff_p, home)))
        return 0

    tag = _validate_tag(args.tag)
    ov: dict[str, Any] = {"audio.save_recordings": False}
    if args.asr_provider.strip():
        ov.update({"asr.provider": args.asr_provider.strip().lower(), "asr.model": args.asr_model.strip()})
    elif args.asr_model.strip():
        ov["asr.model"] = args.asr_model.strip()
    if rp := args.refiner_provider.strip().lower():
        ov["refiner.provider"] = rp
        if rp != base_cfg.refiner.provider:
            ov.update({"refiner.model": "", "refiner.api": "auto", "refiner.base_url": "", "refiner.api_key_env": ""})
    for attr, key in (("refiner_model", "refiner.model"), ("refiner_api", "refiner.api"), ("refiner_effort", "refiner.effort")):
        if val := getattr(args, attr).strip():
            ov[key] = val
    if args.max_chars > 0:
        ov["refiner.context_max_chars"] = args.max_chars
    if args.no_git_status:
        ov["refiner.include_git_status"] = False
    cfg, _ = load_config(config_path=args.config or None, cli_overrides=ov, env=env, home_dir=home, load_default_env_files=False)
    creds = resolve_credentials(cfg, env)

    rec_dir = expand_user_path(args.recordings_dir, env=env) if args.recordings_dir else resolve_saved_recordings_dir(env)
    raw_recs = sorted(list_saved_recordings(rec_dir), key=lambda p: (p.stat().st_mtime, p.name))
    allowed_sha1: set[str] | None = None
    if args.manifest_from.strip():
        mf_tag = _validate_tag(args.manifest_from)
        mf_doc = _load_json_dict(eval_dir / f"{mf_tag}.json")
        if not mf_doc or not isinstance(mf_doc.get("samples"), list) or not mf_doc["samples"]:
            raise SystemExit(f"missing or empty --manifest-from file in {_display_path(eval_dir, home)} for {mf_tag!r}")
        allowed_sha1 = {
            str(s["recording_sha1"])
            for s in mf_doc["samples"]
            if isinstance(s, Mapping) and s.get("recording_sha1")
        }
    asr_from_doc: dict[str, Any] | None = None
    asr_from_map: dict[str, Mapping[str, Any]] | None = None
    if args.asr_from.strip():
        af_tag = _validate_tag(args.asr_from)
        asr_from_doc = _load_json_dict(eval_dir / f"{af_tag}.json")
        if not asr_from_doc:
            raise SystemExit(f"missing --asr-from file in {_display_path(eval_dir, home)} for {af_tag!r}")
        asr_from_map = {
            str(s["recording_sha1"]): s
            for s in asr_from_doc.get("samples", ())
            if isinstance(s, Mapping) and s.get("recording_sha1")
        }
    prepared = []
    for p in raw_recs:
        sha1 = hashlib.sha1(p.read_bytes()).hexdigest()
        if allowed_sha1 is not None and sha1 not in allowed_sha1:
            continue
        gate = process_wav(p, cfg.audio)
        if gate.passed and gate.duration_sec >= args.min_duration:
            prepared.append((p, sha1, p.stat().st_mtime, gate))
            if args.limit > 0 and len(prepared) >= args.limit:
                break

    host_rt = expand_user_path(args.host_runtime_dir, env=env) if args.host_runtime_dir else get_runtime_dir(env, create=False)
    tel_map = _match_telemetry([(p, mt, g.duration_sec) for p, _, mt, g in prepared], read_telemetry_history(host_rt, limit=1000) if host_rt.is_dir() else [])
    session_idx, asr_cache_path = _index_root_sessions(home), eval_dir / "asr_cache.json"
    raw_entries = _load_json_dict(asr_cache_path).get("entries")
    asr_entries: dict[str, Any] = dict(raw_entries) if isinstance(raw_entries, Mapping) else {}
    file_hotwords = load_hotwords_file(cfg.asr.hotwords_file, env=env)

    with tempfile.TemporaryDirectory(prefix="avi-eval-rec-") as td:
        iso_rt, trunc_dir = ensure_secure_dir(Path(td) / "rt"), ensure_secure_dir(Path(td) / "rt" / "trunc")
        iso_env = {**env, "AGENT_VOICE_INPUT_RUNTIME_DIR": str(iso_rt)}
        tracker = SessionTracker(home_dir=home, runtime_dir=iso_rt, proc_root=iso_rt / "no_proc", env=iso_env)
        eff_ref = create_refiner(cfg, creds, env=iso_env, runtime_dir=iso_rt)
        if asr_from_map is not None and asr_from_doc is not None:
            af_meta = asr_from_doc.get("meta") if isinstance(asr_from_doc.get("meta"), Mapping) else {}
            asr_prov = str(af_meta.get("asr_provider") or "frozen")
            asr_mod = str(af_meta.get("asr_model") or "")
            eff_asr: Any = type("_FrozenAsrStub", (), {"provider_name": asr_prov, "single_hop": False})()
        else:
            eff_asr = create_asr_provider(cfg, creds, env=iso_env)
            asr_prov = select_asr_provider(cfg, creds)
            asr_mod = cfg.asr.effective_model(asr_prov)
        seen_warm, samples_out = set(), []
        try:
            total_n = len(prepared)
            for idx_n, (p, rec_sha1, mtime, gate) in enumerate(prepared, 1):
                stop_ts = _estimate_rec_stop_ts(p, mtime)
                cutoff_ts, tel = stop_ts - gate.duration_sec, tel_map.get(p.name, {})
                skey = str(tel.get("session_key") or "").strip()
                if not skey or skey not in session_idx:
                    skey = _find_fallback_session_by_ts(session_idx, cutoff_ts, stop_ts)
                snap, submitted_ref = None, ""
                if skey and skey in session_idx:
                    cli_k, sid, src_j, cwd = session_idx[skey]
                    dst_j = trunc_dir / f"{cli_k}_{hashlib.sha1(skey.encode('utf-8')).hexdigest()[:12]}.jsonl"
                    _, submitted_ref, _ = truncate_session_jsonl_point_in_time(src_j, cli=cli_k, cutoff_ts=cutoff_ts, stop_ts=stop_ts, dst_jsonl=dst_j)
                    snap = tracker.sync_session(
                        match=SessionMatch(cli=cli_k, session_id=sid, jsonl_path=str(dst_j), cwd=cwd if cfg.refiner.include_git_status else "", matched_by="replay"),
                        include_git_info=cfg.refiner.include_git_status,
                    )
                eff_skey = snap.session_key if snap else ""
                asr_ctx = tracker.build_asr_context(snap, extra_hotwords=file_hotwords)
                if asr_from_map is not None:
                    src_s = asr_from_map.get(rec_sha1)
                    if src_s is None:
                        raise SystemExit(f"recording {p.name} ({rec_sha1}) missing in --asr-from {args.asr_from!r}")
                    src_txt = "" if (not src_s.get("ok", True) and src_s.get("skipped_reason") == "empty_asr") else str(src_s.get("asr_draft") or src_s.get("asr_text") or "")
                    asr_res = AsrResult(src_txt, asr_prov, asr_mod, int(src_s.get("asr_ms") or 0), bool(src_s.get("is_single_hop_refined", False)))
                    asr_cached = True
                else:
                    ckey, hw_sha1 = make_asr_cache_key(
                        rec_sha1, asr_prov, asr_mod, asr_ctx.hotwords, git_status=asr_ctx.git_status, dialog_text=asr_ctx.dialog_text
                    )
                    hit = asr_entries.get(ckey) if (args.use_asr_cache and not args.refresh_asr) else None
                    if isinstance(hit, Mapping) and "text" in hit:
                        asr_res, asr_cached = AsrResult(str(hit.get("text") or ""), asr_prov, asr_mod, int(hit.get("latency_ms") or 0), bool(hit.get("is_single_hop_refined", False))), True
                    else:
                        asr_res, chunks_n, _ = _transcribe_wav_chunks(gate.wav_bytes, eff_asr=eff_asr, asr_ctx=asr_ctx, floor_dbfs=cfg.audio.floor_dbfs)
                        asr_cached = False
                        asr_entries[ckey] = {"recording_sha1": rec_sha1, "asr_provider": asr_prov, "asr_model": asr_mod, "hotwords_sha1": hw_sha1, "hotword_count": len(asr_ctx.hotwords), "text": redact_fn(asr_res.text), "latency_ms": asr_res.latency_ms, "is_single_hop_refined": asr_res.is_single_hop_refined, "chunks": chunks_n, "cached_at": int(time.time())}
                        _atomic_write_0600(asr_cache_path, json.dumps({"version": 1, "entries": asr_entries}, ensure_ascii=False, indent=2) + "\n")
                if not asr_res.text.strip():
                    samples_out.append({"recording_name": p.name, "recording_sha1": rec_sha1, "ok": False, "skipped_reason": "empty_asr", "duration_sec": round(gate.duration_sec, 2), "session_tag": _mask_session_key(eff_skey), "asr_cached": asr_cached, "asr_ms": asr_res.latency_ms})
                    sys.stderr.write(f"[{idx_n:02d}/{total_n:02d}] dur={gate.duration_sec:5.1f}s SKIP(empty_asr) cached={asr_cached}\n")
                    continue
                is_warm = bool(eff_skey and eff_skey in seen_warm)
                if eff_skey and not is_warm and _may_run_stage2_refiner(cfg, eff_ref, eff_asr) and callable(getattr(eff_ref, "prepare_session", None)):
                    eff_ref.prepare_session(eff_skey)
                ref_res = _call_refiner_refine(eff_ref, asr_res, session_key=eff_skey, tracker=tracker, fallback_context=asr_ctx.dialog_text, is_cancelled=None)
                if eff_skey:
                    seen_warm.add(eff_skey)
                final_txt = ref_res.text.strip() or asr_res.text.strip()
                in_t, c_t = ref_res.input_tokens, ref_res.cached_input_tokens
                hit_pct = round(100.0 * c_t / in_t, 1) if (in_t and c_t is not None and in_t > 0) else None
                sys.stderr.write(f"[{idx_n:02d}/{total_n:02d}] dur={gate.duration_sec:5.1f}s asr={asr_res.latency_ms}ms(cached={asr_cached}) ref={ref_res.latency_ms}ms hit={hit_pct if hit_pct is not None else '-'}%\n")
                samples_out.append({
                    "recording_name": p.name, "recording_sha1": rec_sha1, "ok": True, "duration_sec": round(gate.duration_sec, 2),
                    "session_tag": _mask_session_key(eff_skey), "ctx_chunks": ref_res.context_chunks, "ctx_chars": ref_res.context_chars,
                    "telemetry_ctx_chars": tel.get("ctx_chars"), "hotword_count": len(asr_ctx.hotwords), "asr_cached": asr_cached,
                    "is_single_hop_refined": asr_res.is_single_hop_refined,
                    "asr_ms": asr_res.latency_ms, "refine_init_ms": ref_res.init_ms, "refine_ms": ref_res.latency_ms, "is_warm": is_warm,
                    "input_tokens": in_t, "cached_input_tokens": c_t, "output_tokens": ref_res.output_tokens, "reasoning_tokens": ref_res.reasoning_tokens,
                    "cache_hit_pct": hit_pct, "used_fallback": redact_fn(ref_res.used_fallback),
                    "session_hotwords": [redact_fn(w) for w in (snap.hotwords[:40] if snap else ())],
                    **evaluate_sample_metrics(asr_res.text, final_txt, session_hotwords=snap.hotwords if snap else (), used_fallback=ref_res.used_fallback),
                    "asr_draft": redact_fn(asr_res.text.strip()), "refined_text": redact_fn(final_txt), "submitted_user_ref": redact_fn(submitted_ref),
                })
        finally:
            eff_ref.close()

    meta = {"version": __version__, "created_at": int(time.time()), "asr_provider": asr_prov, "asr_model": asr_mod, "refiner_provider": cfg.refiner.provider, "refiner_model": cfg.refiner.effective_model(cfg.refiner.provider), "refiner_effort": cfg.refiner.effort, "context_max_chars": cfg.refiner.context_max_chars}
    agg = aggregate_run_metrics(samples_out)
    out_path = eval_dir / f"{tag}.json"
    _atomic_write_0600(out_path, json.dumps({"tag": tag, "meta": meta, "aggregate": agg, "samples": samples_out}, ensure_ascii=False, indent=2) + "\n")
    rows_md = "\n".join(f"| {lbl} | `{val}` |" for lbl, val in _summary_rows(agg))
    print(f"# Recording Replay Summary (`{tag}`)\n\n- Pipeline: `{asr_prov}/{asr_mod}` -> `{meta['refiner_provider']}/{meta['refiner_model']}` (`effort={meta['refiner_effort']}`)\n- Saved JSON (`0600`): `{_display_path(out_path, home)}`\n\n| Metric | Value |\n| :--- | ---: |\n{rows_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
