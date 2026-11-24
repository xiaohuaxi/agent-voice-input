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
from agent_voice_input.config import DEFAULT_ASR_BASE_URLS, ensure_secure_dir, expand_user_path, get_runtime_dir, load_config, normalize_asr_provider, normalize_refiner_provider, redact_secrets, resolve_credentials, resolve_saved_recordings_dir, select_asr_provider  # noqa: E402
from agent_voice_input.control_ipc import read_telemetry_history  # noqa: E402
from agent_voice_input.daemon_pipeline import _may_run_stage2_refiner, _transcribe_wav_chunks  # noqa: E402
from agent_voice_input.refiner import attach_single_hop_context, create_refiner  # noqa: E402
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
    asr_api: str = "",
    asr_effort: str = "",
    resource_id: str = "",
    doubao_mode: str = "",
    base_url: str = "",
) -> tuple[str, str]:
    raw_ctx = "\n".join(hotwords)
    if git_status or dialog_text:
        raw_ctx = f"{raw_ctx}\0{git_status}\0{dialog_text[-2500:]}"
    hw_sha1 = hashlib.sha1(raw_ctx.encode("utf-8")).hexdigest()
    prov = normalize_asr_provider(asr_provider) if asr_provider.strip() else ""
    norm_api = (asr_api or "auto").strip().lower() or "auto"
    norm_effort = (asr_effort or "auto").strip().lower() or "auto"
    norm_rid = (resource_id or "auto").strip().lower() or "auto"
    norm_dmode = (doubao_mode or "auto").strip().lower() or "auto"
    default_url = DEFAULT_ASR_BASE_URLS.get(prov, "").rstrip("/")
    norm_url = (base_url or "").strip().rstrip("/") or default_url
    slot_canon = f"api={norm_api}\0effort={norm_effort}\0rid={norm_rid}\0dmode={norm_dmode}\0url={norm_url}"
    slot_sig = hashlib.sha1(slot_canon.encode("utf-8")).hexdigest()[:12]
    return f"v2:{rec_sha1}:{prov}:{asr_model}:{hw_sha1}:slot={slot_sig}", hw_sha1


def _contains_tech_term(text: str, term: str) -> bool:
    if not term or term not in text:
        return False
    return bool(re.search(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])", text))


def evaluate_sample_metrics(asr_text: str, final_text: str, *, session_hotwords: Sequence[str] = (), used_fallback: str = "") -> dict[str, Any]:
    draft, out = asr_text.strip(), final_text.strip()
    asr_len, final_len = len(draft), len(out)
    q_hijack = bool(draft) and ("?" in draft or "？" in draft) and not ("?" in out or "？" in out) and final_len > int(asr_len * 1.4) + 20
    term_pool = dict.fromkeys([*_CANONICAL_TECH_TERMS, *(w for w in session_hotwords if len(w) >= 3)])
    char_ret: float | None = (
        round(final_len / asr_len, 4)
        if asr_len > 0
        else (None if used_fallback == "single_hop_passthrough" else 0.0)
    )
    return {
        "asr_chars": asr_len, "final_chars": final_len, "char_retention": char_ret,
        "has_newline": ("\n" in out), "paragraph_count": max(1, sum(1 for p in out.splitlines() if p.strip())) if out else 0,
        "filler_residue_hits": len(_TRAILING_FILLER_RE.findall(out)),
        "canonical_tech_hits": sum(1 for term in term_pool if _contains_tech_term(out, term)),
        "homophone_error_hits": len(_KNOWN_HOMOPHONE_ERROR_RE.findall(out)),
        "backtick_hits": out.count("`"),
        "hard_gate_fail": bool(any(m in out for m in _LEAKED_MARKERS) or q_hijack),
        "is_fallback": bool(used_fallback and used_fallback != "single_hop_passthrough"),
    }


def _sample_e2e_ms(s: Mapping[str, Any]) -> int:
    if isinstance(s.get("e2e_ms"), (int, float)):
        return int(s["e2e_ms"])
    return int(s.get("asr_ms") or 0) + int(s.get("prep_ms") or 0) + int(s.get("refine_ms") or 0)


def aggregate_run_metrics(
    samples: Sequence[Mapping[str, Any]],
    *,
    manifest_expected: int | None = None,
) -> dict[str, Any]:
    valid = [s for s in samples if s.get("ok")]
    long_s = [s for s in valid if float(s.get("duration_sec") or 0.0) >= 10.0]
    e2e_ms = [_sample_e2e_ms(s) for s in valid]
    long_e2e_ms = [_sample_e2e_ms(s) for s in long_s]
    asr_ms = [int(s["asr_ms"]) for s in valid if isinstance(s.get("asr_ms"), (int, float))]
    ref_ms = [int(s["refine_ms"]) for s in valid if isinstance(s.get("refine_ms"), (int, float))]
    long_ref_ms = [int(s["refine_ms"]) for s in long_s if isinstance(s.get("refine_ms"), (int, float))]
    warm_hits = [float(s["cache_hit_pct"]) for s in valid if s.get("is_warm") and isinstance(s.get("cache_hit_pct"), (int, float))]
    ret = [float(s["char_retention"]) for s in valid if int(s.get("asr_chars") or 0) > 0 and isinstance(s.get("char_retention"), (int, float))]
    long_ret = [float(s["char_retention"]) for s in long_s if int(s.get("asr_chars") or 0) > 0 and isinstance(s.get("char_retention"), (int, float))]
    r1 = lambda v: round(v, 1) if v is not None else None
    out: dict[str, Any] = {
        "total_recordings": len(samples), "valid_samples": len(valid), "long_samples": len(long_s),
        "skipped_samples": len(samples) - len(valid), "asr_cache_hits": sum(1 for s in samples if s.get("asr_cached")),
        "e2e_ms_p50": r1(_percentile(e2e_ms, 50)), "e2e_ms_p90": r1(_percentile(e2e_ms, 90)),
        "long_e2e_ms_p50": r1(_percentile(long_e2e_ms, 50)),
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
        "backtick_hits": sum(int(s.get("backtick_hits") or 0) for s in valid),
        "backtick_samples": sum(1 for s in valid if int(s.get("backtick_hits") or 0) > 0),
        "fallback_count": sum(1 for s in valid if s.get("is_fallback")),
        "hard_gate_fails": sum(1 for s in valid if s.get("hard_gate_fail")),
    }
    if manifest_expected is not None:
        out["manifest_expected"] = int(manifest_expected)
        out["manifest_missing"] = max(0, int(manifest_expected) - len(samples))
    return out

def _summary_rows(a: Mapping[str, Any]) -> list[tuple[str, str]]:
    f = lambda v, s="": "-" if v is None else f"{v}{s}"
    rows: list[tuple[str, str]] = [
        ("Valid / Total / Long (`>=10s`)", f"{a['valid_samples']} / {a['total_recordings']} / {a['long_samples']}"),
    ]
    if a.get("manifest_expected") is not None:
        rows.append(
            (
                "Manifest aligned (matched / expected / missing)",
                f"{a['total_recordings']} / {a['manifest_expected']} / {a.get('manifest_missing', 0)}",
            )
        )
    rows.extend([
        ("Stage 1 ASR cache hits", f"{a['asr_cache_hits']} / {a['total_recordings']}"),
        ("End-to-end `e2e_ms` (p50 / p90 / `>=10s` p50)", f"{f(a.get('e2e_ms_p50'))} / {f(a.get('e2e_ms_p90'))} / {f(a.get('long_e2e_ms_p50'))} ms"),
        ("Stage 1 `asr_ms` (p50 / p90)", f"{f(a['asr_ms_p50'])} / {f(a['asr_ms_p90'])} ms"),
        ("Stage 2 `refine_ms` (p50 / p90 / `>=10s` p50)", f"{f(a['refine_ms_p50'])} / {f(a['refine_ms_p90'])} / {f(a['long_refine_ms_p50'])} ms"),
        ("Warm `cache_hit_rate` p50", f(a["warm_cache_hit_pct_p50"], "%")),
        ("Char retention `final/asr` p50 (all / `>=10s`)", f"{f(a['char_retention_p50'], '%')} / {f(a['long_char_retention_p50'], '%')}"),
        ("Paragraph split rate (all / `>=10s`)", f"{f(a['multiline_rate_pct'], '%')} / {f(a['long_multiline_rate_pct'], '%')}"),
        ("Trailing filler residue (hits / samples)", f"{a['filler_residue_hits']} / {a['filler_residue_samples']}"),
        ("Canonical tech-term hits / Homophone errors", f"{a['canonical_tech_hits']} / {a['homophone_error_hits']}"),
        ("Inline backtick violations (hits / samples)", f"{a.get('backtick_hits', 0)} / {a.get('backtick_samples', 0)}"),
        ("Fallbacks / Hard-gate fails", f"{a['fallback_count']} / {a['hard_gate_fails']}"),
    ])
    return rows


def _rescore_paired_sample(s: Mapping[str, Any], shared_hw: Sequence[str], *, fallback_asr_draft: str = "") -> tuple[dict[str, Any], bool]:
    d = dict(s)
    used_paired_draft = False
    if d.get("ok") and "refined_text" in d:
        is_sh_pass = str(d.get("used_fallback") or "") == "single_hop_passthrough"
        own_draft = "" if is_sh_pass else str(d.get("asr_draft") or "").strip()
        eff_draft = own_draft
        if not eff_draft and fallback_asr_draft.strip():
            eff_draft = fallback_asr_draft.strip()
            used_paired_draft = True
        if "asr_draft" in d or is_sh_pass or used_paired_draft:
            d.update(
                evaluate_sample_metrics(
                    eff_draft,
                    str(d.get("refined_text") or ""),
                    session_hotwords=shared_hw,
                    used_fallback=str(d.get("used_fallback") or ""),
                )
            )
    return d, used_paired_draft


def compare_runs(
    base_data: Mapping[str, Any], cand_data: Mapping[str, Any], *,
    diff_out_path: Path | None = None, redact_fn: Callable[[str], str] = str, diff_disp: str = "",
) -> str:
    b_tag, c_tag = str(base_data.get("tag") or "base"), str(cand_data.get("tag") or "cand")
    b_map = {str(s["recording_sha1"]): s for s in base_data.get("samples", ()) if isinstance(s, Mapping) and s.get("recording_sha1")}
    c_map = {str(s["recording_sha1"]): s for s in cand_data.get("samples", ()) if isinstance(s, Mapping) and s.get("recording_sha1")}
    keys = [k for k in b_map if k in c_map]
    do_rescore = (
        "meta" in base_data
        or "meta" in cand_data
        or any(s.get("used_fallback") == "single_hop_passthrough" for s in (*b_map.values(), *c_map.values()))
    )
    b_scored, c_scored = [], []
    used_paired_ret = False
    for k in keys:
        bs, cs = b_map[k], c_map[k]
        if do_rescore:
            hw = tuple(cs.get("session_hotwords") or bs.get("session_hotwords") or ())
            b_draft, c_draft = str(bs.get("asr_draft") or ""), str(cs.get("asr_draft") or "")
            bs, b_used = _rescore_paired_sample(bs, hw, fallback_asr_draft=c_draft)
            cs, c_used = _rescore_paired_sample(cs, hw, fallback_asr_draft=b_draft)
            used_paired_ret = used_paired_ret or b_used or c_used
        b_scored.append(bs)
        c_scored.append(cs)
    b_rows, c_rows = _summary_rows(aggregate_run_metrics(b_scored)), _summary_rows(aggregate_run_metrics(c_scored))
    if diff_out_path is not None:
        md = [f"# Detailed Diff: `{b_tag}` vs `{c_tag}` (paired={len(keys)})", ""]
        for idx, k in enumerate(keys, 1):
            bs, cs = b_scored[idx - 1], c_scored[idx - 1]
            state = "CHANGED" if bs.get("refined_text", "") != cs.get("refined_text", "") else "IDENTICAL"
            md.extend([
                f"## #{idx:02d} `{bs.get('recording_name')}` ({bs.get('duration_sec')}s, `{bs.get('session_tag')}`, {state})",
                f"- Base (`e2e={_sample_e2e_ms(bs)}ms`, `ref={bs.get('refine_ms')}ms`, ret=`{bs.get('char_retention')}`): {redact_fn(str(bs.get('refined_text') or ''))}",
                f"- Cand (`e2e={_sample_e2e_ms(cs)}ms`, `ref={cs.get('refine_ms')}ms`, ret=`{cs.get('char_retention')}`): {redact_fn(str(cs.get('refined_text') or ''))}",
                f"- ASR Draft: {redact_fn(str(bs.get('asr_draft') or cs.get('asr_draft') or ''))}",
                f"- Submitted User Ref: {redact_fn(str(bs.get('submitted_user_ref') or cs.get('submitted_user_ref') or '-'))}", "",
            ])
        _atomic_write_0600(diff_out_path, "\n".join(md))
    pair_note = (
        f"paired=`{len(keys)}`, base=`{len(b_map)}`, cand=`{len(c_map)}`"
        if (len(keys) != len(b_map) or len(keys) != len(c_map))
        else f"paired=`{len(keys)}`"
    )
    out = [f"# Replay A/B Comparison: `{b_tag}` vs `{c_tag}` ({pair_note})", ""]
    if diff_disp:
        out.append(f"- Detailed Diff (`0600`): `{diff_disp}`")
    if used_paired_ret:
        out.append("- Note: Single-hop `char_retention` is computed relative to the paired Stage 1 `asr_draft`.")
    if diff_disp or used_paired_ret:
        out.append("")
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
        ("--asr-api", {"default": ""}), ("--asr-effort", {"default": ""}),
        ("--asr-resource-id", {"default": ""}), ("--asr-doubao-mode", {"default": ""}),
        ("--asr-base-url", {"default": ""}),
        ("--refiner-provider", {"default": ""}), ("--refiner-model", {"default": ""}),
        ("--refiner-api", {"default": ""}), ("--refiner-effort", {"default": ""}),
        ("--refiner-base-url", {"default": ""}), ("--refiner-api-key-env", {"default": ""}),
        ("--force-single-hop-refine", {"action": "store_true"}),
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
        ap_val = normalize_asr_provider(args.asr_provider)
        ov["asr.provider"] = ap_val
        if ap_val != base_cfg.asr.canonical_provider():
            ov.update({
                "asr.model": "", "asr.api": "auto", "asr.base_url": "", "asr.api_key_env": "",
                "asr.resource_id": "auto", "asr.doubao_mode": "auto",
            })
    for attr, key in (
        ("asr_model", "asr.model"),
        ("asr_api", "asr.api"),
        ("asr_effort", "asr.effort"),
        ("asr_resource_id", "asr.resource_id"),
        ("asr_doubao_mode", "asr.doubao_mode"),
        ("asr_base_url", "asr.base_url"),
    ):
        if val := getattr(args, attr).strip():
            ov[key] = val
    if args.refiner_provider.strip():
        rp = normalize_refiner_provider(args.refiner_provider)
        ov["refiner.provider"] = rp
        if rp != normalize_refiner_provider(base_cfg.refiner.provider):
            ov.update({"refiner.model": "", "refiner.api": "auto", "refiner.effort": "", "refiner.base_url": "", "refiner.api_key_env": ""})
    for attr, key in (
        ("refiner_model", "refiner.model"),
        ("refiner_api", "refiner.api"),
        ("refiner_effort", "refiner.effort"),
        ("refiner_base_url", "refiner.base_url"),
        ("refiner_api_key_env", "refiner.api_key_env"),
    ):
        if val := getattr(args, attr).strip():
            ov[key] = val
    if args.force_single_hop_refine:
        ov["refiner.force_on_single_hop"] = True
    if args.max_chars > 0:
        ov["refiner.context_max_chars"] = args.max_chars
    if args.no_git_status:
        ov["refiner.include_git_status"] = False
    cfg, _ = load_config(config_path=args.config or None, cli_overrides=ov, env=env, home_dir=home, load_default_env_files=False)
    creds = resolve_credentials(cfg, env)
    secrets = list(dict.fromkeys([*secrets, *creds.all_secret_values()]))
    redact_fn = lambda s: redact_secrets(str(s), env, cfg=cfg, extra_secrets=secrets)

    rec_dir = expand_user_path(args.recordings_dir, env=env) if args.recordings_dir else resolve_saved_recordings_dir(env)
    raw_recs = sorted(list_saved_recordings(rec_dir), key=lambda p: (p.stat().st_mtime, p.name))
    allowed_sha1: set[str] | None = None
    manifest_tag = ""
    manifest_total: int | None = None
    manifest_expected: int | None = None
    if args.manifest_from.strip():
        manifest_tag = _validate_tag(args.manifest_from)
        mf_doc = _load_json_dict(eval_dir / f"{manifest_tag}.json")
        if not mf_doc or not isinstance(mf_doc.get("samples"), list) or not mf_doc["samples"]:
            raise SystemExit(f"missing or empty --manifest-from file in {_display_path(eval_dir, home)} for {manifest_tag!r}")
        allowed_sha1 = {
            str(s["recording_sha1"])
            for s in mf_doc["samples"]
            if isinstance(s, Mapping) and s.get("recording_sha1")
        }
        if not allowed_sha1:
            raise SystemExit(f"--manifest-from file {manifest_tag!r} contains no valid recording_sha1 entries")
        manifest_total = len(allowed_sha1)
        manifest_expected = min(manifest_total, args.limit) if args.limit > 0 else manifest_total
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
    seen_sha1: set[str] = set()
    manifest_seen_on_disk: set[str] = set()
    for p in raw_recs:
        sha1 = hashlib.sha1(p.read_bytes()).hexdigest()
        if allowed_sha1 is not None:
            if sha1 not in allowed_sha1:
                continue
            manifest_seen_on_disk.add(sha1)
            if sha1 in seen_sha1:
                continue
        gate = process_wav(p, cfg.audio)
        if gate.passed and gate.duration_sec >= args.min_duration:
            seen_sha1.add(sha1)
            prepared.append((p, sha1, p.stat().st_mtime, gate))
            if args.limit > 0 and len(prepared) >= args.limit:
                break
    manifest_missing_on_disk = 0
    manifest_filtered = 0
    if allowed_sha1 is not None:
        if not prepared:
            raise SystemExit(
                f"--manifest-from {manifest_tag!r} matched 0/{manifest_expected} recordings in {_display_path(rec_dir, home)}"
            )
        assert manifest_expected is not None
        missing_n = max(0, manifest_expected - len(prepared))
        if missing_n > 0:
            # Reaching missing_n > 0 means the loop scanned all of raw_recs without hitting --limit.
            manifest_missing_on_disk = len(allowed_sha1 - manifest_seen_on_disk)
            manifest_filtered = len(manifest_seen_on_disk - seen_sha1)
            sys.stderr.write(
                f"[eval] WARN: --manifest-from {manifest_tag!r} matched {len(prepared)}/{manifest_expected} "
                f"(missing_on_disk={manifest_missing_on_disk}, filtered_by_gate_or_duration={manifest_filtered})\n"
            )

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
                rec_mode = "general" if str(tel.get("mode") or "").strip().lower() == "general" else "agent"
                skey = "" if rec_mode == "general" else str(tel.get("session_key") or "").strip()
                if rec_mode != "general" and (not skey or skey not in session_idx):
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
                asr_ctx = tracker.build_asr_context(
                    snap, extra_hotwords=file_hotwords, mode=rec_mode, language=cfg.ui.language
                )
                is_single_hop_asr = bool(getattr(eff_asr, "single_hop", False))
                if is_single_hop_asr and asr_from_map is None:
                    asr_ctx = attach_single_hop_context(
                        cfg,
                        asr_ctx,
                        tracker=tracker if snap is not None else None,
                        session_key=eff_skey,
                        mode=rec_mode,
                    )
                if asr_from_map is not None:
                    src_s = asr_from_map.get(rec_sha1)
                    if src_s is None:
                        raise SystemExit(f"recording {p.name} ({rec_sha1}) missing in --asr-from {args.asr_from!r}")
                    is_empty_skip = not src_s.get("ok", True) and src_s.get("skipped_reason") == "empty_asr"
                    src_is_sh_pass = str(src_s.get("used_fallback") or "") == "single_hop_passthrough"
                    if is_empty_skip:
                        src_txt = ""
                    else:
                        src_txt = str(src_s.get("asr_draft") or src_s.get("asr_text") or "")
                        if not src_txt and src_is_sh_pass:
                            src_txt = str(src_s.get("refined_text") or "")
                    src_in_tok = src_s.get("asr_input_tokens") if "asr_input_tokens" in src_s else (src_s.get("input_tokens") if src_is_sh_pass else None)
                    src_c_tok = src_s.get("asr_cached_input_tokens") if "asr_cached_input_tokens" in src_s else (src_s.get("cached_input_tokens") if src_is_sh_pass else None)
                    src_o_tok = src_s.get("asr_output_tokens") if "asr_output_tokens" in src_s else (src_s.get("output_tokens") if src_is_sh_pass else None)
                    src_r_tok = src_s.get("asr_reasoning_tokens") if "asr_reasoning_tokens" in src_s else (src_s.get("reasoning_tokens") if src_is_sh_pass else None)
                    src_aud_tok = src_s.get("asr_audio_tokens") if "asr_audio_tokens" in src_s else src_s.get("audio_tokens")
                    asr_res = AsrResult(
                        src_txt,
                        asr_prov,
                        asr_mod,
                        int(src_s.get("asr_ms") or 0),
                        bool(src_s.get("is_single_hop_refined", False) or src_is_sh_pass),
                        input_tokens=src_in_tok if isinstance(src_in_tok, int) else None,
                        cached_input_tokens=src_c_tok if isinstance(src_c_tok, int) else None,
                        output_tokens=src_o_tok if isinstance(src_o_tok, int) else None,
                        reasoning_tokens=src_r_tok if isinstance(src_r_tok, int) else None,
                        context_chars=int(src_s.get("ctx_chars") or 0) if src_is_sh_pass else 0,
                        context_chunks=int(src_s.get("ctx_chunks") or 0) if src_is_sh_pass else 0,
                        audio_tokens=src_aud_tok if isinstance(src_aud_tok, int) else None,
                    )
                    asr_cached = True
                elif is_single_hop_asr:
                    asr_res, _, _ = _transcribe_wav_chunks(gate.wav_bytes, eff_asr=eff_asr, asr_ctx=asr_ctx, floor_dbfs=cfg.audio.floor_dbfs)
                    asr_cached = False
                else:
                    ckey, hw_sha1 = make_asr_cache_key(
                        rec_sha1,
                        asr_prov,
                        asr_mod,
                        asr_ctx.hotwords,
                        git_status=asr_ctx.git_status,
                        dialog_text=asr_ctx.dialog_text,
                        asr_api=cfg.asr.api,
                        asr_effort=cfg.asr.effort,
                        resource_id=cfg.asr.resource_id,
                        doubao_mode=cfg.asr.doubao_mode,
                        base_url=cfg.asr.effective_base_url(asr_prov),
                    )
                    hit = asr_entries.get(ckey) if (args.use_asr_cache and not args.refresh_asr) else None
                    if isinstance(hit, Mapping) and "text" in hit:
                        asr_res, asr_cached = AsrResult(str(hit.get("text") or ""), asr_prov, asr_mod, int(hit.get("latency_ms") or 0), bool(hit.get("is_single_hop_refined", False))), True
                    else:
                        asr_res, chunks_n, _ = _transcribe_wav_chunks(gate.wav_bytes, eff_asr=eff_asr, asr_ctx=asr_ctx, floor_dbfs=cfg.audio.floor_dbfs)
                        asr_cached = False
                        asr_entries[ckey] = {
                            "recording_sha1": rec_sha1, "asr_provider": asr_prov, "asr_model": asr_mod,
                            "asr_api": cfg.asr.api, "asr_effort": cfg.asr.effort,
                            "asr_resource_id": cfg.asr.resource_id, "asr_doubao_mode": cfg.asr.doubao_mode,
                            "hotwords_sha1": hw_sha1, "hotword_count": len(asr_ctx.hotwords),
                            "text": redact_fn(asr_res.text), "latency_ms": asr_res.latency_ms,
                            "is_single_hop_refined": asr_res.is_single_hop_refined, "chunks": chunks_n,
                            "cached_at": int(time.time()),
                        }
                        _atomic_write_0600(asr_cache_path, json.dumps({"version": 1, "entries": asr_entries}, ensure_ascii=False, indent=2) + "\n")
                if not asr_res.text.strip():
                    if is_single_hop_asr and eff_skey and asr_res.context_chunks > 0:
                        seen_warm.add(eff_skey)
                    skip_rec: dict[str, Any] = {
                        "recording_name": p.name, "recording_sha1": rec_sha1, "ok": False, "skipped_reason": "empty_asr",
                        "duration_sec": round(gate.duration_sec, 2), "mode": rec_mode, "session_tag": _mask_session_key(eff_skey),
                        "asr_cached": asr_cached, "is_single_hop_refined": asr_res.is_single_hop_refined,
                        "asr_ms": asr_res.latency_ms, "e2e_ms": asr_res.latency_ms, "refined_text": "",
                    }
                    if asr_res.input_tokens is not None:
                        skip_rec.update({
                            "input_tokens": asr_res.input_tokens, "cached_input_tokens": asr_res.cached_input_tokens,
                            "output_tokens": asr_res.output_tokens, "reasoning_tokens": asr_res.reasoning_tokens,
                        })
                    if asr_res.audio_tokens is not None:
                        skip_rec["audio_tokens"] = asr_res.audio_tokens
                    samples_out.append(skip_rec)
                    sys.stderr.write(f"[{idx_n:02d}/{total_n:02d}] dur={gate.duration_sec:5.1f}s SKIP(empty_asr) cached={asr_cached}\n")
                    continue
                is_warm = bool(eff_skey and eff_skey in seen_warm)
                prep_ms = 0
                if eff_skey and not is_warm and _may_run_stage2_refiner(cfg, eff_ref, eff_asr) and callable(getattr(eff_ref, "prepare_session", None)):
                    t_prep0 = time.monotonic()
                    eff_ref.prepare_session(eff_skey)
                    prep_ms = max(0, int(round((time.monotonic() - t_prep0) * 1000)))
                ref_res = eff_ref.refine(asr_res, session_key=eff_skey, tracker=tracker, fallback_context=asr_ctx.dialog_text, is_cancelled=None, mode=rec_mode)
                if eff_skey:
                    seen_warm.add(eff_skey)
                final_txt = ref_res.text.strip() or asr_res.text.strip()
                is_passthrough = ref_res.used_fallback == "single_hop_passthrough"
                metric_draft = "" if is_passthrough else asr_res.text.strip()
                e2e_val = int(asr_res.latency_ms) + int(prep_ms) + int(ref_res.latency_ms)
                refine_init_total = int(prep_ms) + int(ref_res.init_ms)
                in_t, c_t = ref_res.input_tokens, ref_res.cached_input_tokens
                aud_t = ref_res.audio_tokens if ref_res.audio_tokens is not None else asr_res.audio_tokens
                if not is_passthrough and asr_res.input_tokens is not None and in_t is not None:
                    tot_in = asr_res.input_tokens + in_t
                    tot_cached = (asr_res.cached_input_tokens or 0) + (c_t or 0)
                    hit_pct = round(100.0 * tot_cached / tot_in, 1) if tot_in > 0 else None
                elif not is_passthrough and in_t is None and asr_res.input_tokens is not None:
                    hit_pct = round(100.0 * asr_res.cached_input_tokens / asr_res.input_tokens, 1) if (asr_res.cached_input_tokens is not None and asr_res.input_tokens > 0) else None
                else:
                    hit_pct = round(100.0 * c_t / in_t, 1) if (in_t and c_t is not None and in_t > 0) else None
                sys.stderr.write(f"[{idx_n:02d}/{total_n:02d}] dur={gate.duration_sec:5.1f}s e2e={e2e_val}ms asr={asr_res.latency_ms}ms(cached={asr_cached}) ref={ref_res.latency_ms}ms hit={hit_pct if hit_pct is not None else '-'}%\n")
                rec_out: dict[str, Any] = {
                    "recording_name": p.name, "recording_sha1": rec_sha1, "ok": True, "duration_sec": round(gate.duration_sec, 2),
                    "mode": rec_mode, "session_tag": _mask_session_key(eff_skey), "ctx_chunks": ref_res.context_chunks, "ctx_chars": ref_res.context_chars,
                    "telemetry_ctx_chars": tel.get("ctx_chars"), "hotword_count": len(asr_ctx.hotwords), "asr_cached": asr_cached,
                    "is_single_hop_refined": asr_res.is_single_hop_refined,
                    "e2e_ms": e2e_val, "asr_ms": asr_res.latency_ms, "prep_ms": prep_ms, "refine_init_ms": refine_init_total, "refine_ms": ref_res.latency_ms, "is_warm": is_warm,
                    "input_tokens": in_t, "cached_input_tokens": c_t, "output_tokens": ref_res.output_tokens, "reasoning_tokens": ref_res.reasoning_tokens,
                    "audio_tokens": aud_t,
                    "cache_hit_pct": hit_pct, "used_fallback": redact_fn(ref_res.used_fallback),
                    "session_hotwords": [redact_fn(w) for w in (snap.hotwords[:40] if snap else ())],
                    **evaluate_sample_metrics(metric_draft, final_txt, session_hotwords=snap.hotwords if snap else (), used_fallback=ref_res.used_fallback),
                    "asr_draft": redact_fn(metric_draft), "refined_text": redact_fn(final_txt), "submitted_user_ref": redact_fn(submitted_ref),
                }
                if not is_passthrough and asr_res.input_tokens is not None:
                    rec_out.update({
                        "asr_input_tokens": asr_res.input_tokens,
                        "asr_cached_input_tokens": asr_res.cached_input_tokens,
                        "asr_output_tokens": asr_res.output_tokens,
                        "asr_reasoning_tokens": asr_res.reasoning_tokens,
                    })
                samples_out.append(rec_out)
        finally:
            eff_ref.close()

    meta: dict[str, Any] = {
        "version": __version__,
        "created_at": int(time.time()),
        "asr_provider": asr_prov,
        "asr_model": asr_mod,
        "asr_api": cfg.asr.api,
        "asr_effort": cfg.asr.effort,
        "asr_resource_id": cfg.asr.resource_id,
        "asr_doubao_mode": cfg.asr.doubao_mode,
        "refiner_provider": cfg.refiner.provider,
        "refiner_model": cfg.refiner.effective_model(cfg.refiner.provider),
        "refiner_effort": cfg.refiner.effort,
        "context_max_chars": cfg.refiner.context_max_chars,
    }
    if manifest_tag:
        meta.update({
            "manifest_from": manifest_tag,
            "manifest_total": manifest_total,
            "manifest_expected": manifest_expected,
            "manifest_matched": len(samples_out),
            "manifest_missing": max(0, (manifest_expected or 0) - len(samples_out)),
            "manifest_missing_on_disk": manifest_missing_on_disk,
            "manifest_filtered": manifest_filtered,
        })
    agg = aggregate_run_metrics(samples_out, manifest_expected=manifest_expected)
    out_path = eval_dir / f"{tag}.json"
    _atomic_write_0600(out_path, json.dumps({"tag": tag, "meta": meta, "aggregate": agg, "samples": samples_out}, ensure_ascii=False, indent=2) + "\n")
    rows_md = "\n".join(f"| {lbl} | `{val}` |" for lbl, val in _summary_rows(agg))
    print(f"# Recording Replay Summary (`{tag}`)\n\n- Pipeline: `{asr_prov}/{asr_mod}` -> `{meta['refiner_provider']}/{meta['refiner_model']}` (`effort={meta['refiner_effort']}`)\n- Saved JSON (`0600`): `{_display_path(out_path, home)}`\n\n| Metric | Value |\n| :--- | ---: |\n{rows_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
