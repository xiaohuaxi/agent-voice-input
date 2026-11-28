#!/usr/bin/env python3
"""Read-only personal hotwords corpus extractor for agent-voice-input.

Always imports agent_voice_input from this repository's src/ first. Strictly
read-only toward all user data sources; outputs are written only into fresh,
exclusive 0700 directories / 0600 files outside the repository and data trees.
"""
from __future__ import annotations

import argparse, datetime, difflib, fnmatch, hashlib, json, math, os, re, sqlite3, stat, sys, tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterator, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[4]
_REPO_SRC = _REPO_ROOT / "src"
if not (_REPO_SRC / "agent_voice_input" / "config.py").is_file():
    raise RuntimeError(f"repository source not found under {_REPO_SRC}")
if not sys.path or sys.path[0] != str(_REPO_SRC):
    sys.path.insert(0, str(_REPO_SRC))

from agent_voice_input._remote_probe import (  # noqa: E402
    _clean_agy_record, _clean_claude_record, _extract_codex_item_completed,
    _extract_codex_response_item, _is_claude_sidechain_or_subagent,
)
from agent_voice_input.config import redact_secrets as _repo_redact_secrets, resolve_app_home_dir, resolve_archive_transcripts_dir  # noqa: E402
from agent_voice_input.workspace_ctx import BUILTIN_AGENT_HOTWORDS  # noqa: E402

SCHEMA_VERSION = 2
GENERIC_TEMP_DIR_MARKERS = (
    "-private-tmp-", "-var-folders-", "/private/tmp/", "/var/folders/",
    "/tmp/", "pytest-", "tmp-test", "-smoke-", "smoke-test",
)
AGY_AUTOMATED_WORKSPACE_MARKERS = ("/agy-refiner", "/voice-refiner", "/var/folders/", "/private/tmp/", "/tmp/")
BUCKET_WEIGHTS = {"transcript": 1.3, "eval": 1.2, "recent_30d": 1.0, "older": 0.6, "archive_hist": 0.08}

# Extra token patterns supplementing config.redact_secrets (env vars, sk-, gsk_, AIza, Bearer, JWT, headers, URLs)
_EXTRA_SECRET_PATTERNS = (
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|glpat|xoxb|xoxp|xoxa|xoxr|hf|r8|pat)[-_][A-Za-z0-9_\-]{12,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN (?:[A-Z0-9 ]* )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z0-9 ]* )?PRIVATE KEY-----"),
    re.compile(r"-----BEGIN (?:[A-Z0-9 ]* )?PRIVATE KEY-----[\s\S]*$"),
    re.compile(r"(?:/Users/[^/\s]+|/home/[^/\s]+)/\.ssh/id_[A-Za-z0-9_.\-]+"),
)
_TAG_NAMES = (
    "ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|SYSTEM_MESSAGE|system-reminder|environment_context|"
    "recommended_plugins|skills_instructions|plugins_instructions|apps_instructions|subagent_notification|"
    "collaboration_mode|INSTRUCTIONS|turn_aborted|send_user_message_question_reply|skill|"
    "task-notification|command-message|command-name|command-args|local-command-stdout|local-command-caveat|"
    "user_instructions"
)
_TAG_BLOCK_PATTERNS = (
    re.compile(rf"<({_TAG_NAMES})(?:\s[^>]*)?>[\s\S]*?</\1>", re.I),
    re.compile(r"<permissions[^>]*>[\s\S]*?</permissions[^>]*>", re.I),
    re.compile(r"<image\s+[^>]*>", re.I),
)
_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>\s*([\s\S]*?)\s*</USER_REQUEST>")
_FENCED_CODE_RE = re.compile(r"```[\s\S]*?```")
_URL_RE = re.compile(r"https?://[^\s<>\"')\]】），。；]+")
_ABS_PATH_RE = re.compile(r"(?:(?:/Users|/home|/private/var|/mnt/[a-z]|/tmp|/run|/var|/etc|/opt|/usr)/[^\s`'\"<>()\[\]{}，。；！？、]+)")
_HEX_OR_UUID_RE = re.compile(r"\b(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{24,})\b", re.I)
_PASTED_PLACEHOLDER_RE = re.compile(r"\[(?:Pasted text|Image)[^\]]{0,60}\]", re.I)
_LOG_LINE_RE = re.compile(
    r"^(?:[%$#]\s+|>>>\s+|remote:\s+|Traceback\b|File \"|at\s+\S+\(|"
    r"[d\-l][rwx\-]{9}\s|\+\+\+\s|---\s|@@\s|commit [0-9a-f]{12}|diff --git\b)"
)
_YM_FILE_RE = re.compile(r"^(\d{4}-\d{2})\.jsonl$")
_CODEX_SYS_PREFIXES = (
    "# AGENTS.md", "<environment_context", "<permissions", "<recommended_plugins",
    "<skills_instructions", "<plugins_instructions", "<apps_instructions", "<subagent_notification",
    "<collaboration_mode", "<INSTRUCTIONS>", "<turn_aborted>",
    "<send_user_message_question_reply>", "<skill", "<user_instructions>",
)
_CODEX_AUTO_FIRST_PREFIXES = ("You are running inside a non-interactive codex-run session", "[codex-run]")


def redact_secrets(text: str) -> str:
    if not text:
        return ""
    out = _repo_redact_secrets(text)
    for pat in _EXTRA_SECRET_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


def _env_path(env_key: str, default_rel: str) -> Path:
    val = os.environ.get(env_key, "").strip()
    return Path(val).expanduser() if val else (Path.home() / default_rel)

def resolve_cache_dir() -> Path: return _env_path("XDG_CACHE_HOME", ".cache") / "agent-voice-input"
def resolve_codex_home() -> Path: return _env_path("CODEX_HOME", ".codex")
def resolve_claude_home() -> Path: return _env_path("CLAUDE_CONFIG_DIR", ".claude")

def match_project_patterns(text: str, patterns: tuple[str, ...]) -> bool:
    if not patterns or not text:
        return False
    low = text.lower()
    return any(fnmatch.fnmatch(low, p.lower()) if any(c in p for c in "*?[]") else (p.lower() in low) for p in patterns)

def _is_excluded_project(raw: str, label: str, patterns: tuple[str, ...]) -> bool:
    return any(m in raw.lower() for m in GENERIC_TEMP_DIR_MARKERS) or match_project_patterns(raw, patterns) or match_project_patterns(label, patterns)

def _is_archive_project(raw: str, label: str, patterns: tuple[str, ...]) -> bool:
    return match_project_patterns(raw, patterns) or match_project_patterns(label, patterns)


def normalize_project_name(raw: str) -> str:
    s = (raw or "").strip()
    s = (s[7:] if s.startswith("file://") else s).rstrip("/\\")
    if not s:
        return "unknown"
    m = re.match(r"^-(?:Users|home)-[^-]+-(?:(?:repos|projects|workspace|work|code|dev|src)-)?(.+)$", s)
    if m and "/" not in s and "\\" not in s and (tail := m.group(1).strip("-")):
        return tail
    parts = [p for p in re.split(r"[/\\]", s) if p]
    return parts[-1] if parts else "unknown"


def _strip_system_blocks(text: str) -> str:
    if not text:
        return ""
    for pat in _TAG_BLOCK_PATTERNS:
        text = pat.sub(" ", text)
    if (idx := text.find("<ADDITIONAL_METADATA>")) != -1:
        text = text[:idx]
    return text.strip()


def strip_system_wrappers(text: str) -> str:
    if not text:
        return ""
    text = _strip_system_blocks(text)
    if m := _USER_REQUEST_RE.search(text):
        text = m.group(1)
    return redact_secrets(text).strip()


def clean_for_term_mining(text: str) -> str:
    if not text:
        return ""
    s = _FENCED_CODE_RE.sub(" ", _PASTED_PLACEHOLDER_RE.sub(" ", text))
    kept = [
        ls for line in s.splitlines()
        if (ls := line.strip())
        and not _LOG_LINE_RE.match(ls)
        and not (len(ls) > 600 and ls.count(" ") < 5 and not re.search(r"[\u4e00-\u9fff]", ls))
        and not (ls.startswith("{") and ls.endswith("}") and len(ls) > 120 and '":' in ls)
    ]
    return _HEX_OR_UUID_RE.sub(" ", _ABS_PATH_RE.sub(" ", _URL_RE.sub(" ", "\n".join(kept))))


def is_automated_prompt(text: str) -> bool:
    s = (text or "").strip()
    if not s or any(s.startswith(p) for p in (*_CODEX_SYS_PREFIXES, *_CODEX_AUTO_FIRST_PREFIXES, "[agy-run]")):
        return True
    if "<asr_draft_to_clean>" in s or "<vocabulary_hints" in s or ("plies played" in s and "FEN:" in s):
        return True
    return s in ("Reply OK only.", "ping", "PING") or bool(re.match(r"^/[a-z][a-z0-9\-_]{1,24}$", s))


def parse_ymd_from_ts(ts: Any) -> str:
    if ts is None or isinstance(ts, bool):
        return ""
    if isinstance(ts, (int, float)):
        v = float(ts) / 1000.0 if float(ts) > 1e12 else float(ts)
        if v <= 0:
            return ""
        try:
            return datetime.datetime.fromtimestamp(v).strftime("%Y-%m-%d")
        except Exception:
            return ""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", str(ts).strip())
    return m.group(1) if m else ""


def _file_mtime_ymd(path: Path) -> str:
    try:
        return datetime.datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
    except OSError:
        return ""


def classify_session_bucket(ymd: str, recent_cutoff: str, is_archive_proj: bool) -> str:
    return "archive_hist" if (is_archive_proj and ymd < recent_cutoff) else ("recent_30d" if ymd >= recent_cutoff else "older")


def _iter_jsonl_dicts(path: Path, stats: dict[str, int] | None = None) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            if stats is not None:
                stats["scanned_rows"] += 1
            try:
                obj = json.loads(line)
            except Exception:
                if stats is not None:
                    stats["invalid_json_rows"] += 1
                continue
            if isinstance(obj, dict):
                yield obj
            elif stats is not None:
                stats["invalid_json_rows"] += 1


def _make_utterance(
    host: str, source: str, bucket: str, project: str, is_archive: bool,
    session_id: str, ymd: str, text: str, record_id: str = "",
) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "host": host, "source": source, "bucket": bucket, "project": project,
        "is_archive": is_archive, "session_id": session_id, "ymd": ymd, "text": text[:3000],
    }
    if record_id:
        rec["record_id"] = record_id
    return rec


# Echo / self-correction detection + shared turn assembler
_ASST_ECHO_PATTERNS = (
    re.compile(r"(?:整理后的本意|本意对齐|先对齐一下语音|顺便对齐一下|顺手对齐一个语音|对齐一下语音).{0,80}"),
    re.compile(r"(?:你说的|你指的|你刚说的|你刚才说的|你前面说的).{0,35}(?:应该是指|确认是指|应该是|是指|是不是指)"),
    re.compile(r"(?:语音输入|语音识别|语音里|语音转写).{0,25}(?:识别成了|识别为|听岔|听错|误听|原意)"),
    re.compile(r"(?:确认一下|先确认|只确认一个|跟你确认).{0,30}(?:语音输入|同音字|笔误|转录偏差|拼写|是不是指)"),
    re.compile(r"(?:同音错字|同音字校准|疑似笔误|转录偏差|识别成了|误听成|听岔成).{0,50}"),
)
_USER_CORR_PATTERNS = (
    re.compile(r"(?:打错了|说错了|写错了|听错了|转错了|识别错了|语音识别错|语音输入错|同音错|错别字|更正一下|纠正一下|写反了|说反了)"),
    re.compile(r"(?:刚才那个|上面那个|前面那个|刚才说的).{0,18}(?:应该是|其实是|打错了|说错了|是指|弄错了)"),
    re.compile(
        r"(?:^|[，。！？\s])(?:错了[，,]|不对[，,]?(?:是|应该)|"
        r"(?<!是)不是[「“\"'`]?[A-Za-z0-9\u4e00-\u9fff._\-]{1,14}[」”\"'`]?[，,]\s*"
        r"(?:而是|是)[「“\"'`]?[A-Za-z0-9\u4e00-\u9fff._\-]{1,18})"
    ),
)


def _match_patterns(text: str, patterns: Sequence[re.Pattern[str]], limit: int) -> list[str]:
    head = (text or "").strip()[:limit]
    return [m.group(0).strip() for pat in patterns if head and (m := pat.search(head))]


def _ingest_session_turns(
    turns: Sequence[tuple[str, str, str]], *, host: str, source: str, proj_label: str,
    is_arch: bool, sid: str, start_ymd: str, end_ymd: str, recent_cutoff: str,
    utterances: list[dict[str, Any]], echoes: list[dict[str, Any]] | None,
    seen_hashes: set[str] | None = None, asst_lookahead: int = 4,
) -> int:
    added, prev_user = 0, ""
    for idx, (role, ymd, text) in enumerate(turns):
        if role != "user":
            continue
        if not ymd or ymd < start_ymd or ymd > end_ymd:
            prev_user = ""
            continue
        bucket = classify_session_bucket(ymd, recent_cutoff, is_arch)
        dup = False
        if seen_hashes is not None:
            h = hashlib.sha1(f"{proj_label}|{text[:300]}".encode("utf-8", "ignore")).hexdigest()[:16]
            dup = h in seen_hashes
            seen_hashes.add(h)
        if not dup:
            utterances.append(_make_utterance(host, source, bucket, proj_label, is_arch, sid, ymd, text))
            added += 1
        if echoes is not None:
            for j in range(idx + 1, min(idx + 1 + asst_lookahead, len(turns))):
                t_role, t_ymd, t_text = turns[j]
                if t_role == "user":
                    break
                if t_role == "assistant":
                    if not t_ymd or t_ymd < start_ymd or t_ymd > end_ymd:
                        continue
                    if eh := _match_patterns(t_text, _ASST_ECHO_PATTERNS, 700):
                        echoes.append({
                            "kind": "assistant_echo", "host": host, "source": source, "project": proj_label,
                            "ymd": ymd, "user_snippet": text[:260], "asst_snippet": t_text[:420], "triggers": eh[:3],
                        })
                    break
            if ch := _match_patterns(text, _USER_CORR_PATTERNS, 400):
                echoes.append({
                    "kind": "user_correction", "host": host, "source": source, "project": proj_label,
                    "ymd": ymd, "prev_user_snippet": prev_user[:220], "user_snippet": text[:320], "triggers": ch[:3],
                })
        prev_user = text
    return added


# Collector 1 & 2: Archived transcripts (strict YYYY-MM.jsonl) & eval-runs
def _opt_str(obj: dict[str, Any], *keys: str) -> str:
    for k in keys:
        val = obj.get(k)
        if isinstance(val, str) and (s := val.strip()):
            return s
    return ""


def _resolve_transcript_session_id(host: str, obj: dict[str, Any], ymd: str, mode: str) -> str:
    agent_raw = _opt_str(obj, "agent").lower()
    sess_raw = _opt_str(obj, "agent_session_id", "session_id", "session_key")
    if sess_raw:
        return f"tr:{host}:{agent_raw}:{sess_raw}" if agent_raw else f"tr:{host}:{sess_raw}"
    return f"tr:{host}:{ymd}:{mode}"


def collect_transcripts(host: str, transcripts_dir: Path, start_ymd: str, end_ymd: str) -> dict[str, Any]:
    stats: dict[str, int] = defaultdict(int)
    utterances, pairs = [], []
    if not transcripts_dir.is_dir():
        return {"exists": False, "stats": {}, "utterances": [], "pairs": []}
    start_ym, end_ym = start_ymd[:7], end_ymd[:7]
    for jf in sorted(transcripts_dir.iterdir()):
        if not jf.is_file() or jf.is_symlink():
            continue
        m_ym = _YM_FILE_RE.fullmatch(jf.name)
        if not m_ym:
            if jf.suffix == ".jsonl":
                stats["skipped_non_monthly_files"] += 1
            continue
        if m_ym.group(1) < start_ym or m_ym.group(1) > end_ym:
            stats["excluded_out_of_window_files"] += 1
            continue
        stats["scanned_files"] += 1
        try:
            for obj in _iter_jsonl_dicts(jf, stats):
                ts_raw = obj.get("ts")
                if not isinstance(ts_raw, (str, int, float)) or isinstance(ts_raw, bool):
                    stats["skipped_invalid_ts_rows"] += 1
                    continue
                ymd = parse_ymd_from_ts(ts_raw)
                if not ymd:
                    stats["skipped_invalid_ts_rows"] += 1
                    continue
                if ymd < start_ymd or ymd > end_ymd:
                    stats["excluded_out_of_window_rows"] += 1
                    continue
                if bool(obj.get("refiner_fallback", False)):
                    stats["excluded_refiner_fallback_rows"] += 1
                    continue
                final_raw = obj.get("final")
                draft_raw = obj.get("draft")
                if (final_raw is not None and not isinstance(final_raw, str)) or (
                    draft_raw is not None and not isinstance(draft_raw, str)
                ):
                    stats["skipped_invalid_field_rows"] += 1
                    continue
                final_txt = redact_secrets(final_raw).strip() if isinstance(final_raw, str) else ""
                draft_txt = redact_secrets(draft_raw).strip() if isinstance(draft_raw, str) else ""
                if not (best_txt := final_txt or draft_txt):
                    stats["skipped_empty_text_rows"] += 1
                    continue
                mode = _opt_str(obj, "mode") or "unknown"
                proj_raw = _opt_str(obj, "project", "workspace", "repo", "cwd", "app")
                proj_label = normalize_project_name(proj_raw) if proj_raw else f"voice:{mode}"
                rec_id = _opt_str(obj, "id", "audio_file")
                utterances.append(_make_utterance(host, "transcript", "transcript", proj_label, False, _resolve_transcript_session_id(host, obj, ymd, mode), ymd, best_txt, rec_id))
                stats["included_utterances"] += 1
                if draft_txt and final_txt and draft_txt != final_txt:
                    pairs.append({
                        "host": host, "id": rec_id or f"{ymd}:{stats['included_utterances']}", "ymd": ymd, "mode": mode,
                        "asr_provider": _opt_str(obj, "asr_provider"), "asr_model": _opt_str(obj, "asr_model"),
                        "draft": draft_txt[:2000], "final": final_txt[:2000],
                    })
                    stats["draft_final_diff_rows"] += 1
        except OSError:
            stats["read_errors"] += 1
    return {"exists": True, "stats": dict(stats), "utterances": utterances, "pairs": pairs}


def collect_eval_runs(host: str, eval_dir: Path, include: bool) -> dict[str, Any]:
    exists = eval_dir.is_dir()
    res: dict[str, Any] = {"exists": exists, "included": bool(include and exists), "file_count": 0, "asr_cache_entries": [], "samples": []}
    if not exists:
        return res
    json_files = [p for p in sorted(eval_dir.iterdir()) if p.is_file() and not p.is_symlink() and p.suffix == ".json"]
    res["file_count"] = len(json_files)
    if not include:
        return res
    ac_path = eval_dir / "asr_cache.json"
    if ac_path.is_file() and not ac_path.is_symlink():
        try:
            for k, v in (json.loads(ac_path.read_text(encoding="utf-8", errors="replace")).get("entries") or {}).items():
                if isinstance(v, dict):
                    res["asr_cache_entries"].append({
                        "key": str(k), "recording_sha1": str(v.get("recording_sha1") or ""),
                        "asr_provider": str(v.get("asr_provider") or ""), "asr_model": str(v.get("asr_model") or ""),
                        "text": redact_secrets(str(v.get("text") or "")), "hotword_count": int(v.get("hotword_count") or 0),
                    })
        except Exception:
            pass
    for jf in json_files:
        if jf.name == "asr_cache.json":
            continue
        try:
            data = json.loads(jf.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        if not isinstance(data, dict) or not isinstance(data.get("samples"), list):
            continue
        tag = jf.stem
        for s in data["samples"]:
            if not isinstance(s, dict):
                continue
            sha = str(s.get("recording_sha1") or s.get("sha1") or s.get("sample_id") or s.get("recording_name") or "")
            draft = redact_secrets(str(s.get("asr_draft") or "")).strip()
            targets: list[tuple[str, str]] = []
            if isinstance(s.get("refined_text"), str) and s["refined_text"].strip():
                targets.append((tag, redact_secrets(s["refined_text"]).strip()))
            for arm_k, arm_v in s.items():
                if isinstance(arm_v, dict) and isinstance(arm_v.get("output_text"), str) and arm_v.get("ok") is not False:
                    if ot := redact_secrets(arm_v["output_text"]).strip():
                        targets.append((f"{tag}:{arm_k}", ot))
            if draft or targets:
                res["samples"].append({
                    "host": host, "tag": tag, "sample_key": sha or f"{tag}:{len(res['samples'])}",
                    "session_tag": str(s.get("session_tag") or s.get("session_short") or "eval"),
                    "asr_draft": draft[:2000], "targets": [(lbl, txt[:2000]) for lbl, txt in targets[:4]],
                })
    return res


# Collector 3: Claude Code, Codex, Antigravity (aligned with repo _remote_probe)
def collect_claude(
    host: str, claude_home: Path, start_ymd: str, end_ymd: str, recent_cutoff: str,
    exclude_patterns: tuple[str, ...], archive_patterns: tuple[str, ...],
) -> dict[str, Any]:
    stats: dict[str, int] = defaultdict(int)
    utterances, echoes, seen_hashes = [], [], set()
    proj_root = claude_home / "projects"
    if proj_root.is_dir():
        for pdir in sorted(proj_root.iterdir()):
            if not pdir.is_dir():
                continue
            dname, proj_label = pdir.name, normalize_project_name(pdir.name)
            if _is_excluded_project(dname, proj_label, exclude_patterns):
                stats["excluded_project_dirs"] += 1
                continue
            is_arch = _is_archive_project(dname, proj_label, archive_patterns)
            for jf in sorted(pdir.rglob("*.jsonl")):
                if "subagents" in jf.parts or jf.name.startswith("agent-") or jf.is_symlink():
                    stats["excluded_subagent_files"] += 1
                    continue
                stats["scanned_project_files"] += 1
                if not (mtime_ymd := _file_mtime_ymd(jf)) or mtime_ymd < start_ymd:
                    if mtime_ymd:
                        stats["excluded_out_of_window_files"] += 1
                    continue
                turns: list[tuple[str, str, str]] = []
                try:
                    for obj in _iter_jsonl_dicts(jf):
                        if obj.get("isCompactSummary") or (cleaned := _clean_claude_record(obj)) is None:
                            continue
                        role, raw_txt = cleaned
                        ymd = parse_ymd_from_ts(obj.get("timestamp")) or mtime_ymd
                        if role == "user":
                            if (txt := strip_system_wrappers(raw_txt)) and not is_automated_prompt(txt):
                                turns.append(("user", ymd, txt))
                        else:
                            turns.append(("assistant", ymd, redact_secrets(raw_txt)[:900]))
                except OSError:
                    continue
                if _ingest_session_turns(
                    turns, host=host, source="claude", proj_label=proj_label, is_arch=is_arch,
                    sid=f"claude:{host}:{jf.stem}", start_ymd=start_ymd, end_ymd=end_ymd,
                    recent_cutoff=recent_cutoff, utterances=utterances, echoes=echoes, seen_hashes=seen_hashes, asst_lookahead=1,
                ):
                    stats["included_project_sessions"] += 1

    hist_path = claude_home / "history.jsonl"
    if hist_path.is_file() and not hist_path.is_symlink():
        hist_sessions: set[str] = set()
        try:
            for obj in _iter_jsonl_dicts(hist_path):
                stats["scanned_history_rows"] += 1
                if _is_claude_sidechain_or_subagent(obj):
                    stats["excluded_history_noise"] += 1
                    continue
                proj_raw = str(obj.get("project", ""))
                proj_label = normalize_project_name(proj_raw)
                if _is_excluded_project(proj_raw, proj_label, exclude_patterns):
                    stats["excluded_history_project"] += 1
                    continue
                ymd = parse_ymd_from_ts(obj.get("timestamp"))
                if not ymd or ymd < start_ymd or ymd > end_ymd:
                    stats["excluded_history_out_of_window"] += 1
                    continue
                if not (text := strip_system_wrappers(str(obj.get("display", "")))) or is_automated_prompt(text):
                    stats["excluded_history_noise"] += 1
                    continue
                is_arch = _is_archive_project(proj_raw, proj_label, archive_patterns)
                sid = f"claude-hist:{host}:{obj.get('sessionId') or (proj_label + ':' + ymd)}"
                emit_echoes = echoes if classify_session_bucket(ymd, recent_cutoff, is_arch) != "archive_hist" else None
                if _ingest_session_turns(
                    [("user", ymd, text)], host=host, source="claude", proj_label=proj_label, is_arch=is_arch,
                    sid=sid, start_ymd=start_ymd, end_ymd=end_ymd, recent_cutoff=recent_cutoff,
                    utterances=utterances, echoes=emit_echoes, seen_hashes=seen_hashes, asst_lookahead=0,
                ):
                    hist_sessions.add(sid)
                else:
                    stats["deduped_history_rows"] += 1
        except OSError:
            pass
        stats["included_history_sessions"] = len(hist_sessions)
    stats["included_utterances"] = len(utterances)
    return {"stats": dict(stats), "utterances": utterances, "echoes": echoes}


def collect_codex(
    host: str, codex_home: Path, start_ymd: str, end_ymd: str, recent_cutoff: str,
    exclude_patterns: tuple[str, ...], archive_patterns: tuple[str, ...],
) -> dict[str, Any]:
    stats: dict[str, int] = defaultdict(int)
    utterances, echoes = [], []
    root = codex_home / "sessions"
    if not root.is_dir():
        return {"stats": {}, "utterances": [], "echoes": []}
    for jf in sorted(root.rglob("*.jsonl")):
        if jf.is_symlink() or not (mtime_ymd := _file_mtime_ymd(jf)):
            continue
        if mtime_ymd < start_ymd:
            stats["excluded_out_of_window"] += 1
            continue
        stats["scanned_files_in_window"] += 1
        try:
            gen = _iter_jsonl_dicts(jf)
            meta = next(gen, None)
            if meta is None or meta.get("type") != "session_meta" or not isinstance(meta.get("payload"), dict):
                stats["excluded_no_meta"] += 1
                continue
            pl = meta["payload"]
            src = pl.get("source")
            if src not in ("cli", "vscode"):
                src_name = src if isinstance(src, str) else (next(iter(src)) if isinstance(src, dict) and src else "other")
                stats[f"excluded_source_{src_name}"] += 1
                continue
            cwd = str(pl.get("cwd", ""))
            proj_label = normalize_project_name(cwd)
            if _is_excluded_project(cwd, proj_label, exclude_patterns):
                stats["excluded_project_cwd"] += 1
                continue
            is_arch = _is_archive_project(cwd, proj_label, archive_patterns)
            sid = f"codex:{host}:{pl.get('id') or jf.stem}"
            last_ymd = parse_ymd_from_ts(meta.get("timestamp")) or parse_ymd_from_ts(pl.get("timestamp")) or mtime_ymd
            saw_item, is_auto, checked_first = False, False, False
            item_turns: list[tuple[str, str, str]] = []
            resp_turns: list[tuple[str, str, str]] = []

            for obj in gen:
                p2 = obj.get("payload")
                if not isinstance(p2, dict):
                    continue
                typ = obj.get("type")
                last_ymd = parse_ymd_from_ts(obj.get("timestamp")) or parse_ymd_from_ts(p2.get("timestamp")) or last_ymd
                turn, is_item = None, False
                if typ == "event_msg" and p2.get("type") == "item_completed":
                    turn, is_item = _extract_codex_item_completed(p2), True
                elif typ == "response_item" and not saw_item:
                    turn = _extract_codex_response_item(p2)
                elif typ == "event_msg" and p2.get("type") == "user_message" and not saw_item:
                    if raw_u := str(p2.get("message") or p2.get("text") or "").strip():
                        turn = ("user", raw_u)
                if turn is None:
                    continue
                role, raw_txt = turn
                if role == "user":
                    s_raw = raw_txt.strip()
                    if not s_raw or s_raw.startswith("# AGENTS.md"):
                        continue
                    outer_txt = _strip_system_blocks(raw_txt)
                    if not outer_txt or any(outer_txt.startswith(p) for p in _CODEX_SYS_PREFIXES):
                        continue
                    clean_txt = redact_secrets(
                        m.group(1) if (m := _USER_REQUEST_RE.search(outer_txt)) else outer_txt
                    ).strip()
                    if not checked_first:
                        if (
                            any(t.startswith(p) for t in (outer_txt, clean_txt) if t for p in _CODEX_AUTO_FIRST_PREFIXES)
                            or "<asr_draft_to_clean>" in outer_txt
                            or "<vocabulary_hints" in outer_txt
                        ):
                            is_auto = True; stats["excluded_codex_run_automated"] += 1; break
                        if "plies played" in outer_txt and "FEN:" in outer_txt:
                            is_auto = True; stats["excluded_game_automated"] += 1; break
                    if not clean_txt or any(clean_txt.startswith(p) for p in _CODEX_SYS_PREFIXES):
                        continue
                    checked_first = True
                    if is_automated_prompt(outer_txt) or is_automated_prompt(clean_txt):
                        continue
                    norm = ("user", last_ymd, clean_txt)
                else:
                    norm = ("assistant", last_ymd, redact_secrets(raw_txt)[:900])

                dest = item_turns if is_item else resp_turns
                if is_item and not saw_item:
                    hist = list(resp_turns)
                    if hist and hist[-1][0] == norm[0] and hist[-1][2][:200] == norm[2][:200]:
                        hist.pop()
                    item_turns, resp_turns, saw_item = hist, [], True
                    dest = item_turns
                if not (dest and dest[-1][0] == norm[0] and dest[-1][2][:200] == norm[2][:200]):
                    dest.append(norm)
        except OSError:
            continue
        if is_auto:
            continue
        turns = item_turns if saw_item else resp_turns
        if not any(t[0] == "user" for t in turns):
            stats["excluded_empty_user"] += 1
            continue
        if _ingest_session_turns(
            turns, host=host, source="codex", proj_label=proj_label, is_arch=is_arch, sid=sid,
            start_ymd=start_ymd, end_ymd=end_ymd, recent_cutoff=recent_cutoff,
            utterances=utterances, echoes=echoes, asst_lookahead=3,
        ):
            stats["included_sessions"] += 1
    stats["included_utterances"] = len(utterances)
    return {"stats": dict(stats), "utterances": utterances, "echoes": echoes}


def _load_full_step_map(full_path: Path, needed_steps: set[int]) -> dict[int, str]:
    out: dict[int, str] = {}
    if not needed_steps or not full_path.is_file() or full_path.is_symlink():
        return out
    try:
        for obj in _iter_jsonl_dicts(full_path):
            si = obj.get("step_index")
            if isinstance(si, int) and not isinstance(si, bool) and si in needed_steps:
                content = obj.get("content")
                if isinstance(content, str) and content.strip():
                    out[si] = content
                    if len(out) == len(needed_steps):
                        break
    except OSError:
        pass
    return out


def collect_agy(
    host: str, agy_home: Path, start_ymd: str, end_ymd: str, recent_cutoff: str,
    exclude_patterns: tuple[str, ...], archive_patterns: tuple[str, ...],
) -> dict[str, Any]:
    stats: dict[str, Any] = defaultdict(int)
    utterances, echoes = [], []
    db_path, brain_dir = agy_home / "conversation_summaries.db", agy_home / "brain"
    if not brain_dir.exists() and not db_path.exists():
        return {"stats": {}, "utterances": [], "echoes": []}
    if not db_path.is_file():
        stats.update({"db_unavailable_skipped": 1, "coverage_gap": "conversation_summaries.db missing; skipped brain/ scan"})
        return {"stats": dict(stats), "utterances": [], "echoes": []}

    candidates: list[tuple[str, str, str]] = []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT conversation_id, nesting_depth, parent_conversation_id, "
                "agent_name, workspace_uris, last_modified_time FROM conversation_summaries"
            ).fetchall()
        finally:
            con.close()
        for cid, depth, parent_id, agent_name, ws, lmt in rows:
            stats["db_total_rows"] += 1
            ymd = parse_ymd_from_ts(lmt)
            if not ymd or ymd < start_ymd:
                stats["excluded_out_of_window"] += 1; continue
            if depth != 0 or (parent_id and str(parent_id).strip()):
                stats["excluded_subagent"] += 1; continue
            if an := (agent_name or "").strip():
                stats["excluded_refiner" if an in ("avi-refiner", "voice-refiner") else "excluded_named_automated_agent"] += 1; continue
            ws_str = str(ws or "")
            if any(m in ws_str for m in AGY_AUTOMATED_WORKSPACE_MARKERS):
                stats["excluded_automated_workspace"] += 1; continue
            candidates.append((str(cid), ws_str, ymd))
    except Exception:
        stats.update({"db_read_error": 1, "db_unavailable_skipped": 1, "coverage_gap": "conversation_summaries.db unreadable; skipped brain/ scan"})
        return {"stats": dict(stats), "utterances": [], "echoes": []}

    for cid, ws_str, fallback_ymd in candidates:
        tp = brain_dir / cid / ".system_generated/logs/transcript.jsonl"
        tfp = brain_dir / cid / ".system_generated/logs/transcript_full.jsonl"
        if not tp.is_file() or tp.is_symlink():
            stats["missing_transcript"] += 1; continue
        proj_raw = ws_str
        if ws_str:
            try:
                ws_list = json.loads(ws_str)
                if isinstance(ws_list, list) and ws_list:
                    proj_raw = str(ws_list[0])
            except Exception:
                pass
        proj_label = normalize_project_name(proj_raw)
        if match_project_patterns(proj_raw, exclude_patterns) or match_project_patterns(proj_label, exclude_patterns):
            stats["excluded_project"] += 1; continue
        is_arch = _is_archive_project(proj_raw, proj_label, archive_patterns)

        raw_events, trunc_steps = [], set()
        try:
            for obj in _iter_jsonl_dicts(tp):
                if (obj.get("source") == "USER_EXPLICIT" and obj.get("type") == "USER_INPUT") or (
                    obj.get("source") == "MODEL" and obj.get("type") == "PLANNER_RESPONSE"
                ):
                    raw_events.append(obj)
                    if "content" in (obj.get("truncated_fields") or []) and obj.get("step_index") is not None:
                        trunc_steps.add(int(obj["step_index"]))
        except OSError:
            continue
        full_map = _load_full_step_map(tfp, trunc_steps)
        if full_map:
            stats["truncated_steps_recovered"] += len(full_map)

        turns, is_auto, checked_first = [], False, False
        for ev in raw_events:
            si = ev.get("step_index")
            recovered = full_map.get(si) if (isinstance(si, int) and not isinstance(si, bool)) else None
            cleaned = _clean_agy_record({**ev, "content": recovered} if recovered else ev)
            if cleaned is None:
                continue
            role, raw_txt = cleaned
            ev_ymd = parse_ymd_from_ts(ev.get("created_at")) or fallback_ymd
            if role == "user":
                if not checked_first:
                    checked_first = True
                    if "<asr_draft_to_clean>" in raw_txt or "<vocabulary_hints" in raw_txt:
                        is_auto = True; stats["excluded_refiner_fallback"] += 1; break
                    if raw_txt.startswith("[agy-run]") or ("plies played" in raw_txt and "FEN:" in raw_txt):
                        is_auto = True; stats["excluded_auto_fallback"] += 1; break
                if (txt := strip_system_wrappers(raw_txt)) and not is_automated_prompt(txt):
                    turns.append(("user", ev_ymd, txt))
            else:
                turns.append(("assistant", ev_ymd, redact_secrets(raw_txt)[:900]))

        if is_auto:
            continue
        if not any(x[0] == "user" for x in turns):
            stats["excluded_zero_user_input"] += 1; continue
        if _ingest_session_turns(
            turns, host=host, source="agy", proj_label=proj_label, is_arch=is_arch, sid=f"agy:{host}:{cid}",
            start_ymd=start_ymd, end_ymd=end_ymd, recent_cutoff=recent_cutoff,
            utterances=utterances, echoes=echoes, asst_lookahead=4,
        ):
            stats["included_sessions"] += 1
    stats["included_utterances"] = len(utterances)
    return {"stats": dict(stats), "utterances": utterances, "echoes": echoes}


def _compute_window(days: int) -> tuple[str, str, str]:
    today = datetime.date.today()
    end_ymd = today.strftime("%Y-%m-%d")
    start_ymd = (today - datetime.timedelta(days=max(1, days))).strftime("%Y-%m-%d")
    recent_cutoff = (today - datetime.timedelta(days=min(30, max(1, days)))).strftime("%Y-%m-%d")
    return start_ymd, end_ymd, recent_cutoff


def _normalize_scope_patterns(patterns: Sequence[str]) -> list[str]:
    return sorted({p.strip() for p in patterns if isinstance(p, str) and p.strip()})


def collect_local(
    *, host: str, days: int, transcripts_dir: Path, eval_runs_dir: Path, include_eval_runs: bool,
    no_agent_sessions: bool, exclude_patterns: tuple[str, ...], archive_patterns: tuple[str, ...],
    start_ymd: str | None = None, end_ymd: str | None = None, recent_cutoff: str | None = None,
) -> dict[str, Any]:
    calc_start, calc_end, calc_recent = _compute_window(days)
    start_ymd = start_ymd or calc_start
    end_ymd = end_ymd or calc_end
    recent_cutoff = recent_cutoff or calc_recent
    empty: dict[str, Any] = {"stats": {}, "utterances": [], "echoes": []}
    return {
        "schema_version": SCHEMA_VERSION, "host": host,
        "collected_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "window": {"start_ymd": start_ymd, "end_ymd": end_ymd, "days": days},
        "scope": {
            "include_eval_runs": bool(include_eval_runs),
            "no_agent_sessions": bool(no_agent_sessions),
            "exclude_project": _normalize_scope_patterns(exclude_patterns),
            "archive_project": _normalize_scope_patterns(archive_patterns),
        },
        "transcripts": collect_transcripts(host, transcripts_dir, start_ymd, end_ymd),
        "eval_runs": collect_eval_runs(host, eval_runs_dir, include_eval_runs),
        "claude": empty if no_agent_sessions else collect_claude(host, resolve_claude_home(), start_ymd, end_ymd, recent_cutoff, exclude_patterns, archive_patterns),
        "codex": empty if no_agent_sessions else collect_codex(host, resolve_codex_home(), start_ymd, end_ymd, recent_cutoff, exclude_patterns, archive_patterns),
        "agy": empty if no_agent_sessions else collect_agy(host, Path.home() / ".gemini/antigravity-cli", start_ymd, end_ymd, recent_cutoff, exclude_patterns, archive_patterns),
    }


# Term mining (compact high-signal stop/seed lists + shared stats)
EN_STOPWORDS = frozenset((
    "a an the and or but if then else for of to in on at by with from as is are was were be been being "
    "have has had do does did will would shall should can could may might must not no nor so too very just "
    "only also even still this that these those it its they them their we us our you your he him his she "
    "her who what which when where why how all any both each few more most other some such own same than "
    "into out up down off over under again once here there now about after before between through during "
    "above below because while until without within get got set put run make made take give use used using "
    "need want try keep let see look check add new old good bad ok okay yes yeah please thanks hi hello "
    "sure well right left first last next back one two three four five six seven eight nine ten zero true "
    "false null none nan undefined void return const var def file files line lines text name type value "
    "data list item items info note time date day days week month year case part way thing work done start "
    "stop end open close read write edit save load send show find fix update change remove delete create "
    "build test tests pass fail error warn issue step like know think really actually maybe already always "
    "never http https www com org net io dev cn html utf utf8 redacted image png jpg jpeg gif webp svg pdf "
    "txt log tmp usr bin etc var home users repos path dir src dist ms sec min kb mb gb px id ids todo "
    "fixme plan mode rule user main base core auto full fast slow high low max top big small long short "
    "real local remote global default custom extra final clean clear reset copy paste move sync init help "
    "version status output input result report summary review design spec doc docs task job tool call code "
    "script project repo folder root host port url link key env flag arg param option config state event "
    "field index count size level score rate limit cache queue batch chunk block model message reply "
    "response request context history memory window screen panel button click press focus audio voice sound "
    "record server client service process thread target source format parser schema table query search "
    "filter match group order sort split join strip parse print debug trace verify doesn don isn aren wasn "
    "won wouldn couldn vs eg ie via per pro plus mini lite beta alpha stdout stderr stdin exit pid uid gid "
    "cwd pwd eof import export module package class func function method object string number bool array "
    "dict map tuple enum struct interface async await self super"
).split())
EN_KEEP_WHITELIST = frozenset((
    "agent skill session prompt token daemon worker diff commit branch tag rebase merge stash hook guard "
    "timeout readme swarm mcp acp cli asr llm tts stt oobe tcc osc ssh wsl wsl2 macos linux docker "
    "kubernetes sing-box webrtc gitleaks sandbox symlink hud refiner fallback debounce backoff webhook "
    "websocket grpc quic oauth jwt tls dns vpn cdp gpu cuda mlx vllm gguf lora rag ocr vad wer cer pmi "
    "tui gui pypi ruff mypy vite vitest ffmpeg homebrew nvm fnm sqlite postgres redis nginx caddy "
    "cloudflare vercel supabase quasar nuxt tailwind playwright raycast iterm2 wezterm alacritty neovim "
    "powershell pwsh zed"
).split()) | frozenset(w.lower() for w in BUILTIN_AGENT_HOTWORDS)
EN_EFFECTIVE_STOP = EN_STOPWORDS - EN_KEEP_WHITELIST
_EN_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])(?:[A-Za-z][A-Za-z0-9]*(?:[-_.+/][A-Za-z0-9]+)*)(?![A-Za-z0-9_])")
_EN_BIGRAM_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z][A-Za-z0-9._-]{1,22})[ ]([A-Za-z][A-Za-z0-9._-]{1,22})(?![A-Za-z0-9_])")
_CANONICAL_BIGRAM_KEEP = frozenset({
    "claude code", "vs code", "nuxt ui", "gemini cli", "computer use", "github actions", "github pages",
    "apple silicon", "antigravity cli", "tailwind css", "cloudflare workers", "openai codex", "claude opus",
    "claude sonnet", "claude haiku", "chrome devtools", "prefix cache", "prompt cache", "single hop",
    "pull request", "merge commit", "git worktree", "mcp server", "ssh tunnel",
})

_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
_CN_FUNC_EDGE_CHARS = frozenset(
    "的了着过吗呢吧啊呀哦嗯哈啦嘛哇呗兮我你他她它咱您谁哪这那其每各某在是从到把被给让叫向往朝离按照"
    "和与及或但而却虽若如则因由以便就都也还又再才只刚已曾将要会能可该应须得很太最更极挺蛮颇极其较稍略"
    "不没无非未勿别莫一个些种次条项份块段句篇张件套批什么怎么怎样为什么多少几"
)
_CN_HARD_STOPWORDS = frozenset((
    "这个 那个 一个 这些 那些 这样 那样 这里 那里 这种 那种 可以 应该 需要 已经 现在 目前 之前 之后 然后 但是 不过 "
    "因为 所以 如果 虽然 还是 或者 以及 而且 并且 另外 同时 就是 不是 没有 其实 可能 大概 基本 主要 一般 通常 自己 "
    "我们 你们 他们 大家 一下 一点 一些 一起 一直 一样 非常 比较 特别 完全 直接 自动 手动 具体 实际 整体 整个 问题 "
    "情况 方式 方法 结果 效果 内容 信息 东西 地方 时候 时间 开始 结束 完成 继续 处理 解决 确认 检查 查看 看看 觉得 "
    "感觉 认为 希望 建议 知道 明白 了解 发现 看到 找到 使用 通过 进行 提供 支持 实现 增加 添加 删除 修改 更新 调整 "
    "优化 改进 修复 保持 保证 确保 避免 防止 限制 允许 默认 当前 对应 相关 所有 全部 部分 其他 其它 其中 包括 比如 "
    "例如 关于 对于 针对 根据 按照 基于 由于 为了 除了 不会 不能 不要 不用 不行 不对 不够 不好 不错 正常 正确 明显 "
    "严重 简单 复杂 重要 必要 合适 合理 清楚 明确 准确 刚才 刚刚 接下来 下一步 顺便 帮我 请你 麻烦 谢谢 好的 可以吗 "
    "对不对 是不是 有没有 能不能 怎么做 为什么 怎么样 是什么 什么是 还有 还要 第一 第二 第三 首先 其次 最后 总之 "
    "今天 明天 昨天 最近 近期 以前 以后 后面 前面 上面 下面 里面 外面 很多 不少 少量 大量 几个 多个 各种 不同 相同 "
    "类似 一致 统一 单独 分别 各自 之间 之内 之外 以上 以下 左右 大约 几乎 差不多 至少 最多 尽量 最好 最大 最小 最新 "
    "确实 真的 显然 当然 必须 务必 一定 肯定 绝对 或许 也许 似乎 好像 意思 原因 目的 目标 需求 要求 条件 前提 结论 "
    "判断 决定 选择 方案 计划 步骤 流程 过程 阶段 环节 范围 边界 标准 原则 机制 逻辑 结构 架构 设计 功能 能力 特性 "
    "参数 选项 状态 类型 格式 模式 场景 环境 系统 平台 工具 项目 仓库 目录 文件 代码 脚本 程序 页面 界面 窗口 位置 "
    "运行 执行 启动 停止 关闭 打开 切换 返回 退出 进入 读取 写入 保存 加载 导出 导入 发送 接收 调用 触发 响应 监听 "
    "测试 调试 排查 定位 复现 验证 验收 评估 对比 参考 说明 解释 描述 定义 区分 识别 提取 过滤 筛选 排序 合并 拆分 "
    "替换 覆盖 保留 忽略 跳过 取消 撤销 恢复 重置 清理 刷新 同步 拉取 推送 提交 回退 回滚 重试 重启 连接 绑定 配置 "
    "设置 开启 启用 禁用 指定 输入 输出 显示 隐藏 提示 提醒 通知 警告 报错 失败 成功 存在 缺失 丢失 重复 冲突 异常 "
    "错误 故障 影响 导致 出现 发生 遇到 涉及 包含 属于 依赖 关联 结合 配合 协作 沟通 讨论 思考 考虑 注意 关注 优先 "
    "核心 关键 基础 通用 独立 临时 长期 实时 离线 在线 本地 远端 远程 云端 桌面 终端 前端 后端 底层 外部 内部 公开 "
    "私有 个人 用户 开发 维护 产品 技术 工作 任务 操作 事件 数据 文本 文字 字符 语言 中文 英文 数字 符号 标点 标题 "
    "正文 列表 表格 图片 视频 音频 语音 录音 路径 链接 地址 名字 版本 日期 历史 日志 规范 约定 匹配 对齐 升级 降级"
).split())
# All seeds are 2-4 CJK chars and checked before edge-char filtering so none are unreachable.
_CN_DOMAIN_KEEP = frozenset((
    "豆包 千问 通义 通义千问 火山引擎 硅基流动 月之暗面 智谱 百炼 魔搭 混元 文心 讯飞 科大讯飞 反重力 "
    "热词 精修 精修器 冷审 拍板 软链 软链接 串台 防串台 守护进程 剪贴板 热键 落字 兜底 叠甲 门禁 单测 "
    "覆盖率 真身 沙箱 宿主机 发版 润笔 口癖 同音错字 静音帧 转录 归档 上下文 提示词 工作流 工作树 "
    "前缀缓存 缓存命中 单跳 两阶段 流水线 编排 委派 派单 竞态 退避 指数退避 熔断 幂等 原子写 回放 "
    "回归矩阵 修饰键 变基 暂存区 热重载 端点检测 采样率 多模态 微调 蒸馏 量化 思维链"
).split())
_CN_EFFECTIVE_STOP = _CN_HARD_STOPWORDS - _CN_DOMAIN_KEEP
_CN_STOP_PREFIXES = tuple("可以 应该 需要 已经 没有 不是 就是 还是 或者 因为 所以 如果 虽然 但是 不过 然后 而且 并且 另外 同时 我们 你们 他们 自己 这个 那个 这些 那些 这样 那样 这里 那里 一个 一下 一点 一些 怎么 什么 是否 能否 帮我 请你 麻烦 觉得 直接 完全 非常 比较 特别 基本 可能 一定 必须 不要 不能 不会 不用 刚才 现在 目前 之前 之后 还有 只要 只有 无论".split())
_CN_STOP_SUFFIXES = tuple("一下 一点 一些 一样 一个 的话 时候 地方 东西 问题 情况 方式 方法 结果 内容 可以 应该 需要 已经 没有 不是 就是 还是 或者 起来 下来 出来 进去 过去 回来 之后 之前 以后 以前 里面 外面 上面 下面 左右 之间 以上 以下 即可 就行".split())


def _record_term_hit(stats: dict[str, dict[str, Any]], key: str, rec: dict[str, Any], raw_form: str | None = None) -> None:
    st = stats.setdefault(key, {"variants": Counter(), "total": 0, "buckets": Counter(), "weighted": 0.0, "sessions": set(), "recent_sessions": set(), "projects": Counter(), "sources": Counter()})
    b = rec["bucket"]
    if raw_form is not None:
        st["variants"][raw_form] += 1
    st["total"] += 1
    st["buckets"][b] += 1
    st["weighted"] += BUCKET_WEIGHTS.get(b, 0.5)
    st["sessions"].add(rec["session_id"])
    if b in ("transcript", "eval", "recent_30d"):
        st["recent_sessions"].add(rec["session_id"])
    st["projects"][rec["project"]] += 1
    st["sources"][rec["source"]] += 1


def _build_term_entry(term: str, st: dict[str, Any], builtin_lows: set[str], mult: float = 1.0, **extra: Any) -> dict[str, Any]:
    sess_n, rec_sess_n = len(st["sessions"]), len(st["recent_sessions"])
    score = round(st["weighted"] * (1.0 + 0.45 * math.log1p(rec_sess_n) + 0.15 * math.log1p(sess_n)) * mult, 2)
    return {
        "term": term, "rank_score": score, "weighted_count": round(st["weighted"], 2), "total_count": st["total"],
        "buckets": dict(st["buckets"]), "session_count": sess_n, "recent_session_count": rec_sess_n,
        "project_count": len(st["projects"]), "projects": [p for p, _ in st["projects"].most_common(6)],
        "sources": dict(st["sources"]), "in_builtin": term.lower() in builtin_lows, **extra,
    }


def pick_canonical_spelling(variant_counts: Counter[str]) -> str:
    if not variant_counts:
        return ""
    total, best_form, best_score = sum(variant_counts.values()), "", -1.0
    for form, cnt in variant_counts.items():
        has_u, has_l = any(c.isupper() for c in form), any(c.islower() for c in form)
        bonus = 1.6 if (has_u and has_l) else (1.4 if (has_u and len(form) <= 6) else (1.05 if has_u else 1.0))
        sc = cnt * 0.8 if (cnt < max(2, total * 0.12) and has_u) else (cnt * bonus)
        if sc > best_score:
            best_score, best_form = sc, form
    return best_form


def is_valid_en_token(tok: str) -> bool:
    if not (2 <= len(tok) <= 40) or (low := tok.lower()) in EN_EFFECTIVE_STOP or "redacted" in low:
        return False
    if re.fullmatch(r"[0-9._\-+/]+|v?\d+(?:\.\d+)+[a-z]?|[0-9a-f]{7,}", low):
        return False
    if re.fullmatch(r"[a-z]\d{1,2}", low) and low not in ("v2", "v3", "v4"):
        return False
    if tok[0] in "-._+/" or tok[-1] in "-._+/" or tok.count("/") >= 2 or tok.count(".") >= 3 or re.search(r"-[0-9a-f]{6,}$", low):
        return False
    if low.endswith("s") and low[:-1] in EN_EFFECTIVE_STOP and low not in EN_KEEP_WHITELIST:
        return False
    return not ((low.endswith("ed") and low[:-2] in EN_EFFECTIVE_STOP) or (low.endswith("ing") and low[:-3] in EN_EFFECTIVE_STOP))


def extract_english_mixed_terms(records: list[dict[str, Any]], builtin_lows: set[str]) -> list[dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    bg_cnt: Counter[str] = Counter()
    bg_sess: dict[str, set[str]] = defaultdict(set)
    cleaned_texts: list[tuple[dict[str, Any], str]] = []
    for rec in records:
        if not (ct := clean_for_term_mining(rec["text"])):
            continue
        cleaned_texts.append((rec, ct))
        for m in _EN_BIGRAM_RE.finditer(ct):
            w1, w2 = m.group(1), m.group(2)
            bg = f"{w1.lower()} {w2.lower()}"
            if bg not in _CANONICAL_BIGRAM_KEEP and (
                w1.lower() in EN_STOPWORDS or w2.lower() in EN_STOPWORDS
                or not (w1[0].isupper() and w2[0].isupper() and len(w1) >= 2 and len(w2) >= 2 and not w1.isdigit() and not w2.isdigit())
            ):
                continue
            bg_cnt[bg] += 1
            bg_sess[bg].add(rec["session_id"])
    valid_bgs = {bg for bg, c in bg_cnt.items() if (bg in _CANONICAL_BIGRAM_KEEP and c >= 2) or (c >= 4 and len(bg_sess[bg]) >= 2)}
    for rec, ct in cleaned_texts:
        for m in _EN_TOKEN_RE.finditer(ct):
            if is_valid_en_token(tok := m.group(0)):
                _record_term_hit(stats, tok.lower(), rec, tok)
        for m in _EN_BIGRAM_RE.finditer(ct):
            if (bg := f"{m.group(1).lower()} {m.group(2).lower()}") in valid_bgs:
                _record_term_hit(stats, bg, rec, f"{m.group(1)} {m.group(2)}")
    results = [
        _build_term_entry(pick_canonical_spelling(st["variants"]), st, builtin_lows, key=k, variants=[v for v, _ in st["variants"].most_common(4)])
        for k, st in stats.items()
        if not ((st["total"] < 2 and st["buckets"]["transcript"] == 0 and st["buckets"]["eval"] == 0) or (st["weighted"] < 0.8 and st["total"] < 5))
    ]
    results.sort(key=lambda x: (-x["rank_score"], -x["recent_session_count"], -x["total_count"], x["key"]))
    return results


def _shannon_entropy(counter: Counter[str]) -> float:
    total = sum(counter.values())
    return 0.0 if total <= 0 else -sum((c / total) * math.log(c / total) for c in counter.values())


def extract_chinese_domain_terms(records: list[dict[str, Any]], builtin_lows: set[str]) -> list[dict[str, Any]]:
    char_counts: Counter[str] = Counter()
    total_chars, rec_runs = 0, []
    for rec in records:
        if runs := (_CJK_RUN_RE.findall(ct) if (ct := clean_for_term_mining(rec["text"])) else []):
            rec_runs.append((rec, runs))
            for r in runs:
                total_chars += len(r); char_counts.update(r)
    if total_chars == 0:
        return []
    raw_ng: Counter[str] = Counter()
    for _rec, runs in rec_runs:
        for r in runs:
            for n in (2, 3, 4):
                for i in range(max(0, len(r) - n + 1)):
                    w = r[i : i + n]
                    if w in _CN_DOMAIN_KEEP or (w[0] not in _CN_FUNC_EDGE_CHARS and w[-1] not in _CN_FUNC_EDGE_CHARS):
                        raw_ng[w] += 1
    cand_set = {w for w, c in raw_ng.items() if c >= 3 or (c >= 2 and w in _CN_DOMAIN_KEEP)}
    ng_left, ng_right, ng_meta = defaultdict(Counter), defaultdict(Counter), {}
    for rec, runs in rec_runs:
        for r in runs:
            lr = len(r)
            for n in (2, 3, 4):
                for i in range(max(0, lr - n + 1)):
                    if (w := r[i : i + n]) in cand_set:
                        ng_left[w][r[i - 1] if i > 0 else "^"] += 1
                        ng_right[w][r[i + n] if i + n < lr else "$"] += 1
                        _record_term_hit(ng_meta, w, rec)
    scored: dict[str, dict[str, Any]] = {}
    for w, m in ng_meta.items():
        if w in _CN_EFFECTIVE_STOP or (len(w) == 2 and w[0] == w[1]):
            continue
        is_seed = w in _CN_DOMAIN_KEEP
        if not is_seed and (
            (len(w) >= 3 and (w.startswith(_CN_STOP_PREFIXES) or w.endswith(_CN_STOP_SUFFIXES)))
            or m["weighted"] < 1.2
            or (len(m["sessions"]) < 2 and m["buckets"]["transcript"] == 0 and m["buckets"]["eval"] == 0)
        ):
            continue
        p_w, min_pmi = m["total"] / total_chars, 99.0
        for split in range(1, len(w)):
            ls, rs = w[:split], w[split:]
            c_l = char_counts[ls] if len(ls) == 1 else raw_ng.get(ls, 0)
            c_r = char_counts[rs] if len(rs) == 1 else raw_ng.get(rs, 0)
            min_pmi = min(min_pmi, 0.0 if (c_l <= 0 or c_r <= 0) else math.log(p_w / ((c_l / total_chars) * (c_r / total_chars)) + 1e-12))
        min_ent = min(_shannon_entropy(ng_left[w]), _shannon_entropy(ng_right[w]))
        if not is_seed:
            pmi_th, ent_th = (2.2, 0.85) if len(w) == 2 else ((2.0, 0.70) if len(w) == 3 else (1.8, 0.55))
            if min_pmi < pmi_th or min_ent < ent_th:
                continue
        cohesion = min(2.0, 0.7 + 0.15 * max(0.0, min_pmi) + 0.2 * min(2.5, min_ent))
        scored[w] = _build_term_entry(w, m, builtin_lows, mult=cohesion * (1.45 if is_seed else 1.0), pmi=round(min_pmi, 2), min_entropy=round(min_ent, 2), is_domain_seed=is_seed)

    to_drop: set[str] = set()
    for w in list(scored):
        if len(w) <= 2:
            continue
        cnt_w = scored[w]["total_count"]
        for sub_len in range(2, len(w)):
            for i in range(len(w) - sub_len + 1):
                if (sub := w[i : i + sub_len]) in scored:
                    cnt_sub = scored[sub]["total_count"]
                    if cnt_w >= 0.72 * cnt_sub and sub not in _CN_DOMAIN_KEEP:
                        to_drop.add(sub)
                    if w not in _CN_DOMAIN_KEEP and cnt_w < 0.12 * cnt_sub and scored[w]["min_entropy"] < 1.1:
                        to_drop.add(w)
    final_list = [item for w, item in scored.items() if w not in to_drop]
    final_list.sort(key=lambda x: (-x["rank_score"], -x["recent_session_count"], -x["total_count"], x["term"]))
    return final_list


# Diff alignment & clustering
_FILLER_ONLY = frozenset("那个 就是 然后 就是说 嗯 啊 呢 吧 哦 呃 哈 这个 其实 但是 不过 所以 因为 那么 那 就 也 都 还 再 说 看 对 好 行 嘛 呀 啦 哇".split())


def extract_alignment_diffs(draft: str, target: str) -> list[tuple[str, str, str]]:
    if not draft or not target or draft == target:
        return []
    t_a = re.findall(r"[A-Za-z0-9_.\-/+]+|[\u4e00-\u9fff]", draft)
    t_b = re.findall(r"[A-Za-z0-9_.\-/+]+|[\u4e00-\u9fff]", target)
    if not t_a or not t_b:
        return []
    out: list[tuple[str, str, str]] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, t_a, t_b, autojunk=False).get_opcodes():
        if tag != "replace" or (i2 - i1) > 8 or (j2 - j1) > 8:
            continue
        w_t, r_t = t_a[i1:i2], t_b[j1:j2]
        wrong = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", (" " if any(re.search(r"[A-Za-z]", x) for x in w_t) else "").join(w_t)).strip()
        right = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", (" " if any(re.search(r"[A-Za-z]", x) for x in r_t) else "").join(r_t)).strip()
        if not wrong or not right or wrong.lower() == right.lower() or wrong in _FILLER_ONLY or right in _FILLER_ONLY:
            continue
        if len(wrong) == 1 and len(right) == 1 and not re.search(r"[A-Za-z]", right):
            continue
        out.append((wrong, right, f"…{''.join(t_b[max(0, j1 - 6) : j1])}[{wrong} -> {right}]{''.join(t_b[j2 : min(len(t_b), j2 + 6)])}…"))
    return out


def _cluster_diffs(items: list[tuple[str, str, str, str, str]]) -> list[dict[str, Any]]:
    clusters: dict[tuple[str, str], dict[str, Any]] = {}
    for wrong, right, ctx, src_tag, sample_id in items:
        if not (re.search(r"[A-Za-z]", right) or (len(wrong) >= 2 and len(right) >= 2)):
            continue
        c = clusters.setdefault((right.lower(), wrong.lower()), {"right": right, "wrong": wrong, "count": 0, "sources": Counter(), "examples": []})
        c["count"] += 1; c["sources"][src_tag] += 1
        if len(c["examples"]) < 3:
            c["examples"].append(f"[{sample_id}] {ctx}")
    out = sorted(clusters.values(), key=lambda c: (-(2 if re.search(r"[A-Za-z]", c["right"]) else 1) * c["count"], -c["count"], c["right"].lower()))
    for c in out:
        c["sources"] = dict(c["sources"])
    return out


# Strict outside-repo exclusive 0700 / 0600 filesystem guards & merge checks
def _find_git_root(start: Path) -> Path | None:
    cur = Path(os.path.realpath(str(start)))
    return next((c for c in (cur, *cur.parents) if (c / ".git").exists()), None)


def _build_forbidden_roots(transcripts_dir: Path, eval_runs_dir: Path) -> list[Path]:
    raw = [_REPO_ROOT, transcripts_dir, eval_runs_dir, resolve_claude_home(), resolve_codex_home(), Path.home() / ".gemini/antigravity-cli"]
    if (app_home := resolve_app_home_dir()) is not None:
        raw.append(app_home)
    if (cwd_git := _find_git_root(Path.cwd())) is not None:
        raw.append(cwd_git)
    return list({Path(os.path.realpath(str(p))) for p in raw})


def _assert_outside_forbidden(resolved: Path, forbidden_roots: Sequence[Path]) -> None:
    if resolved == Path(os.path.realpath(str(Path.home()))):
        raise ValueError(f"refusing to write directly into home directory: {resolved}")
    if (git_root := _find_git_root(resolved)) is not None:
        raise ValueError(f"refusing to write inside git repository ({git_root}): {resolved}")
    for root in forbidden_roots:
        if resolved == root or root in resolved.parents:
            raise ValueError(f"refusing to write inside repository or data-source directory ({root}): {resolved}")


def _prepare_exclusive_output_dir(explicit: Path | None, forbidden_roots: Sequence[Path]) -> Path:
    if explicit is None:
        tmp_base = Path(os.path.realpath(tempfile.gettempdir()))
        _assert_outside_forbidden(tmp_base / "avi-hotwords-corpus-probe", forbidden_roots)
        d = Path(tempfile.mkdtemp(prefix=f"avi-hotwords-corpus-{os.getuid()}-"))
        try:
            _assert_outside_forbidden(Path(os.path.realpath(str(d))), forbidden_roots)
            os.chmod(d, 0o700)
        except Exception:
            try:
                d.rmdir()
            except OSError:
                pass
            raise
        return d
    target = explicit.expanduser()
    if target.is_symlink():
        raise ValueError(f"refusing symlink output directory: {target}")
    _assert_outside_forbidden(Path(os.path.realpath(str(target))), forbidden_roots)
    if target.exists():
        if not target.is_dir():
            raise ValueError(f"output path exists and is not a directory: {target}")
        if any(target.iterdir()):
            raise ValueError(f"refusing to write into existing non-empty directory: {target}")
        sub = Path(tempfile.mkdtemp(prefix="run-", dir=str(target)))
        os.chmod(sub, 0o700)
        return sub
    target.parent.mkdir(parents=True, exist_ok=True)
    os.mkdir(target, 0o700)
    os.chmod(target, 0o700)
    return target


def _write_private_exclusive(path: Path, text: str) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    closed = False
    try:
        os.fchmod(fd, 0o600)
        fobj = os.fdopen(fd, "w", encoding="utf-8")
        closed = True
        with fobj as f:
            f.write(text)
    except Exception:
        if not closed:
            try: os.close(fd)
            except OSError: pass
        path.unlink(missing_ok=True)
        raise

def _prepare_out_file(out_file: Path, forbidden_roots: Sequence[Path]) -> Path:
    target = out_file.expanduser()
    if target.is_symlink() or target.exists():
        raise ValueError(f"refusing symlink or existing output file: {target}")
    parent = target.parent
    if parent.is_symlink():
        raise ValueError(f"refusing symlink parent directory for --out-file: {parent}")
    parent_real = Path(os.path.realpath(str(parent)))
    _assert_outside_forbidden(parent_real, forbidden_roots)
    _assert_outside_forbidden(parent_real / target.name, forbidden_roots)
    if parent.exists():
        st = os.stat(parent_real)
        if not stat.S_ISDIR(st.st_mode):
            raise ValueError(f"output parent exists and is not a directory: {parent}")
        if st.st_uid != os.getuid() or (st.st_mode & 0o077) != 0:
            raise ValueError(
                f"refusing shared or non-exclusive parent directory for --out-file (expected owner-only 0700): {parent}"
            )
    else:
        missing: list[Path] = []
        cur = parent
        while not cur.exists():
            if cur.is_symlink():
                raise ValueError(f"refusing symlink parent directory for --out-file: {cur}")
            missing.append(cur)
            if cur.parent == cur:
                break
            cur = cur.parent
        for d in reversed(missing):
            os.mkdir(d, 0o700)
            os.chmod(d, 0o700)
    return target


def _normalize_scope_keyset(raw: Any, field_name: str, path: Path) -> list[str]:
    if not isinstance(raw, list) or any(not isinstance(x, str) for x in raw):
        raise ValueError(f"merge file missing or invalid 'scope.{field_name}' list: {path}")
    return sorted({x.strip().lower() for x in raw if x.strip()})


def _load_and_validate_merge_file(
    raw_path: str,
    *,
    expected_days: int,
    expected_start_ymd: str,
    expected_end_ymd: str,
    expected_include_eval_runs: bool,
    expected_no_agent: bool,
    expected_exclude_projects: Sequence[str],
    expected_archive_projects: Sequence[str],
    seen_hosts: set[str],
) -> dict[str, Any]:
    p = Path(raw_path).expanduser()
    if p.is_symlink():
        raise ValueError(f"refusing symlink merge file: {p}")
    if not p.is_file():
        raise ValueError(f"merge file not found: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"failed to parse merge file {p}: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError(f"invalid merge payload (expected JSON object): {p}")
    host = data.get("host")
    if not isinstance(host, str) or not host.strip():
        raise ValueError(f"merge file missing non-empty 'host': {p}")
    if host in seen_hosts:
        raise ValueError(f"duplicate host label {host!r} in {p}; pass distinct --host-label values across machines")
    win = data.get("window")
    if not isinstance(win, dict):
        raise ValueError(f"merge file missing valid 'window' object: {p}")
    win_days = win.get("days")
    if not isinstance(win_days, int) or isinstance(win_days, bool):
        raise ValueError(f"merge file missing valid 'window.days': {p}")
    if win_days != expected_days:
        raise ValueError(f"merge window mismatch in {p}: file has days={win_days}, local has --days={expected_days}")
    win_start, win_end = win.get("start_ymd"), win.get("end_ymd")
    if not isinstance(win_start, str) or not isinstance(win_end, str) or not win_start.strip() or not win_end.strip():
        raise ValueError(f"merge file missing valid 'window.start_ymd'/'window.end_ymd': {p}")
    if win_start != expected_start_ymd or win_end != expected_end_ymd:
        raise ValueError(
            f"merge window date mismatch in {p}: file has {win_start}..{win_end}, local has {expected_start_ymd}..{expected_end_ymd}"
        )
    scope = data.get("scope")
    if not isinstance(scope, dict):
        raise ValueError(f"merge file missing required 'scope' object: {p}")
    for key, expected_val in (("no_agent_sessions", expected_no_agent), ("include_eval_runs", expected_include_eval_runs)):
        val = scope.get(key)
        if not isinstance(val, bool):
            raise ValueError(f"merge file missing boolean 'scope.{key}': {p}")
        if val != expected_val:
            raise ValueError(f"merge scope mismatch in {p}: {key}={val} vs local {expected_val}")
    for key, expected_list in (("exclude_project", expected_exclude_projects), ("archive_project", expected_archive_projects)):
        got_norm = _normalize_scope_keyset(scope.get(key), key, p)
        exp_norm = sorted({x.strip().lower() for x in expected_list if x.strip()})
        if got_norm != exp_norm:
            raise ValueError(f"merge scope mismatch in {p}: {key}={got_norm} vs local {exp_norm}")
    if not isinstance(data.get("transcripts"), dict) or any(data.get(k) is not None and not isinstance(data.get(k), dict) for k in ("claude", "codex", "agy")):
        raise ValueError(f"merge file missing or invalid source sections: {p}")
    seen_hosts.add(host)
    return data


# Report rendering
def _md_table(headers: Sequence[str], align: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    return [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(align) + "|",
        *("| " + " | ".join(str(c).replace("|", "\\|").replace("\n", " ") for c in r) + " |" for r in rows),
    ]


def _render_cluster_table(clusters: Sequence[dict[str, Any]], limit: int) -> list[str]:
    rows = [
        (f"`{c['right']}`", f"`{c['wrong']}`", c["count"],
         ",".join(f"{k}:{v}" for k, v in sorted(c["sources"].items(), key=lambda kv: -kv[1])),
         " ; ".join(c["examples"][:2])[:140])
        for c in clusters[:limit]
    ]
    return _md_table(("目标词", "观测误听", "次数", "来源", "对照语境"), (":---", ":---", "---:", ":---", ":---"), rows)


def _write_term_report(out_dir: Path, stem: str, title: str, total: int, exported: list[dict[str, Any]], headers: Sequence[str], align: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    _write_private_exclusive(out_dir / f"{stem}.json", json.dumps({"total_candidates": total, "exported": len(exported), "terms": exported}, ensure_ascii=False, indent=2))
    _write_private_exclusive(out_dir / f"{stem}.md", "\n".join([title, "", *_md_table(headers, align, rows)]) + "\n")


def write_outputs(out_dir: Path, hosts_data: list[dict[str, Any]], top_en: int, top_cn: int) -> dict[str, Any]:
    builtin_lows = {w.lower() for w in BUILTIN_AGENT_HOTWORDS}
    all_records, seen_utt, dedup_count = [], set(), 0
    tr_pairs, seen_tr_ids = [], set()
    eval_pairs, eval_cache, seen_eval_spoken, seen_eval_pair = [], {}, set(), set()
    all_echoes, seen_echo = [], set()

    for hd in hosts_data:
        tr = hd.get("transcripts") or {}
        for u in tr.get("utterances") or []:
            sig = f"tr:{u['record_id']}" if u.get("record_id") else hashlib.sha1(f"tr|{u['ymd']}|{u['text'][:300]}".encode("utf-8", "ignore")).hexdigest()[:16]
            if sig in seen_utt: dedup_count += 1
            else: seen_utt.add(sig); all_records.append(u)
        for p in tr.get("pairs") or []:
            pid, pkey = p.get("id") or "", f"{p.get('id') or ''}|{p['draft'][:100]}"
            if pkey not in seen_tr_ids:
                seen_tr_ids.add(pkey)
                src_tag = f"{p.get('asr_provider') or 'asr'}:{p.get('mode') or 'any'}"
                tr_pairs.extend((w, r, ctx, src_tag, pid or p["ymd"]) for w, r, ctx in extract_alignment_diffs(p["draft"], p["final"]))

        ev = hd.get("eval_runs") or {}
        for e in ev.get("asr_cache_entries") or []:
            if k := e.get("key", ""): eval_cache.setdefault(k, e)
        for s in ev.get("samples") or []:
            skey, targets = s.get("sample_key", ""), s.get("targets") or []
            if skey and skey not in seen_eval_spoken:
                seen_eval_spoken.add(skey)
                if best_txt := (targets[0][1] if targets else s.get("asr_draft", "")):
                    all_records.append(_make_utterance(hd["host"], "eval", "eval", f"eval:{s.get('tag', 'run')}", False, f"eval:{s.get('session_tag', 's')}", hd.get("window", {}).get("end_ymd", ""), best_txt))
            draft = s.get("asr_draft", "")
            for lbl, tgt in targets:
                if draft and tgt:
                    for w, r, ctx in extract_alignment_diffs(draft, tgt):
                        if (pk := (skey, r.lower(), w.lower())) not in seen_eval_pair:
                            seen_eval_pair.add(pk); eval_pairs.append((w, r, ctx, lbl, skey[:12]))

        for src_key in ("claude", "codex", "agy"):
            block = hd.get(src_key) or {}
            for u in block.get("utterances") or []:
                sig = hashlib.sha1(f"{u['source']}|{u['project']}|{u['ymd']}|{u['text'][:300]}".encode("utf-8", "ignore")).hexdigest()[:16]
                if sig in seen_utt: dedup_count += 1
                else: seen_utt.add(sig); all_records.append(u)
            for e in block.get("echoes") or []:
                esig = hashlib.sha1(f"{e['kind']}|{e['user_snippet'][:120]}".encode("utf-8", "ignore")).hexdigest()[:16]
                if esig not in seen_echo: seen_echo.add(esig); all_echoes.append(e)

    by_sha1: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in eval_cache.values():
        if e.get("recording_sha1") and e.get("text"):
            by_sha1[e["recording_sha1"]].append(e)
    cross_asr_diffs: list[dict[str, Any]] = []
    for sha, entries in by_sha1.items():
        uniq = {f"{e['asr_provider']}:{e['asr_model']}(hw={e.get('hotword_count', 0)})": e for e in entries}
        if len(uniq) >= 2:
            pms = sorted(uniq)
            for other_pm in pms[1:]:
                if meaningful := [d for d in extract_alignment_diffs(uniq[pms[0]]["text"], uniq[other_pm]["text"]) if re.search(r"[A-Za-z]", d[0] + d[1]) or (len(d[0]) >= 2 and len(d[1]) >= 2)]:
                    cross_asr_diffs.append({"sha1": sha[:10], "p1": pms[0], "p2": other_pm, "diffs": [[w, r] for w, r, _ in meaningful[:6]]})

    en_terms, cn_terms = extract_english_mixed_terms(all_records, builtin_lows), extract_chinese_domain_terms(all_records, builtin_lows)
    en_top, cn_top = en_terms[:top_en], cn_terms[:top_cn]
    _write_term_report(
        out_dir, "terms_english_mixed", f"# 英文与中英混排候选词表（Top {len(en_top)} / 共 {len(en_terms)}）", len(en_terms), en_top,
        ("#", "候选词", "加权分", "总频次", "归档转录", "近30天", "跨会话(近/总)", "内置", "主要项目", "来源"),
        ("---:", ":---", "---:", "---:", "---:", "---:", "---:", ":---:", ":---", ":---"),
        [(i, f"`{t['term']}`", t["rank_score"], t["total_count"], t["buckets"].get("transcript", 0), t["buckets"].get("recent_30d", 0), f"{t['recent_session_count']}/{t['session_count']}", "yes" if t["in_builtin"] else "-", ", ".join(t["projects"][:4]), "/".join(f"{k}:{v}" for k, v in sorted(t["sources"].items()))) for i, t in enumerate(en_top, 1)],
    )
    _write_term_report(
        out_dir, "terms_chinese_domain", f"# 中文领域候选词表（Top {len(cn_top)} / 共 {len(cn_terms)}）", len(cn_terms), cn_top,
        ("#", "候选词", "加权分", "总频次", "归档转录", "近30天", "跨会话(近/总)", "PMI", "邻接熵", "内置", "主要项目"),
        ("---:", ":---", "---:", "---:", "---:", "---:", "---:", "---:", "---:", ":---:", ":---"),
        [(i, t["term"], t["rank_score"], t["total_count"], t["buckets"].get("transcript", 0), t["buckets"].get("recent_30d", 0), f"{t['recent_session_count']}/{t['session_count']}", t["pmi"], t["min_entropy"], "yes" if t["in_builtin"] else "-", ", ".join(t["projects"][:4])) for i, t in enumerate(cn_top, 1)],
    )

    tr_clusters, ev_clusters = _cluster_diffs(tr_pairs), _cluster_diffs(eval_pairs)
    asst_echoes = sorted((e for e in all_echoes if e["kind"] == "assistant_echo"), key=lambda x: x.get("ymd", ""), reverse=True)
    user_corrs = sorted((e for e in all_echoes if e["kind"] == "user_correction"), key=lambda x: x.get("ymd", ""), reverse=True)
    _write_private_exclusive(out_dir / "mishearing_candidates.json", json.dumps({
        "transcript_clusters": tr_clusters[:200], "eval_clusters": ev_clusters[:200],
        "cross_asr_diffs": cross_asr_diffs[:80], "assistant_echoes": asst_echoes[:80], "user_corrections": user_corrs[:80],
    }, ensure_ascii=False, indent=2))

    mh_md = [
        "# 误听别名候选证据汇总", "",
        f"- 归档转录 `draft` -> `final` 聚类：{len(tr_clusters)} 组；Eval-runs 对齐：{len(ev_clusters)} 组；跨 ASR 差异：{len(cross_asr_diffs)} 组",
        f"- 助手本意对齐回显：{len(asst_echoes)} 条；用户自纠线索：{len(user_corrs)} 条", "",
        "## 一、归档转录 draft -> final 误听聚类", "", *_render_cluster_table(tr_clusters, 120),
    ]
    if ev_clusters or cross_asr_diffs:
        mh_md += ["", "## 二、Eval-Runs 回放误听聚类与跨 ASR 差异", "", *_render_cluster_table(ev_clusters, 100)]
        mh_md.extend(f"- `{x['sha1']}` ({x['p1']} vs {x['p2']}): " + " ; ".join(f"`{w}` vs `{r}`" for w, r in x["diffs"][:4]) for x in cross_asr_diffs[:30])
    mh_md += ["", f"## 三、助手本意对齐回显（前 50 / 共 {len(asst_echoes)} 条）", ""]
    for i, e in enumerate(asst_echoes[:50], 1):
        mh_md += [f"- E-{i:02d} [{e['ymd']}] `{e['host']}/{e['source']}` (`{e['project']}`)", f"  - 用户：{e['user_snippet'].replace(chr(10), ' ')[:200]}", f"  - 助手：{e['asst_snippet'].replace(chr(10), ' ')[:280]}"]
    mh_md += ["", f"## 四、用户自纠线索（前 50 / 共 {len(user_corrs)} 条）", ""]
    for i, e in enumerate(user_corrs[:50], 1):
        mh_md.append(f"- C-{i:02d} [{e['ymd']}] `{e['host']}/{e['source']}` (`{e['project']}`)")
        if p_s := e.get("prev_user_snippet", "").replace("\n", " "):
            mh_md.append(f"  - 上一句：{p_s[:160]}")
        mh_md.append(f"  - 纠错句：{e['user_snippet'].replace(chr(10), ' ')[:240]}")
    _write_private_exclusive(out_dir / "mishearing_candidates.md", "\n".join(mh_md) + "\n")

    arch_records = [r for r in all_records if r.get("is_archive")]
    if arch_records:
        arch_recent = sum(1 for r in arch_records if r["bucket"] == "recent_30d")
        arch_hist = sum(1 for r in arch_records if r["bucket"] == "archive_hist")
        arch_en, arch_cn = extract_english_mixed_terms(arch_records, builtin_lows)[:80], extract_chinese_domain_terms(arch_records, builtin_lows)[:80]
        _write_private_exclusive(out_dir / "archive_project_terms.md", "\n".join([
            f"# 归档项目专项候选词（共 {len(arch_records)} 条发言：近30天 {arch_recent} 条正常权重、历史 {arch_hist} 条按 0.08 降权）", "",
            "## 英文与技术词（Top 80）", "",
            *_md_table(("#", "词条", "频次", "跨会话", "内置", "项目"), ("---:", ":---", "---:", "---:", ":---:", ":---"), [(i, f"`{t['term']}`", t["total_count"], t["session_count"], "yes" if t["in_builtin"] else "-", ", ".join(t["projects"][:3])) for i, t in enumerate(arch_en, 1)]),
            "", "## 中文领域词（Top 80）", "",
            *_md_table(("#", "词条", "频次", "跨会话", "PMI", "项目"), ("---:", ":---", "---:", "---:", "---:", ":---"), [(i, t["term"], t["total_count"], t["session_count"], t["pmi"], ", ".join(t["projects"][:3])) for i, t in enumerate(arch_cn, 1)]),
        ]) + "\n")

    by_src: dict[tuple[str, str, str], dict[str, Any]] = {}
    proj_stats: dict[str, dict[str, Any]] = {}
    for r in all_records:
        d = by_src.setdefault((r["host"], r["source"], r["bucket"]), {"sessions": set(), "utts": 0, "chars": 0})
        d["sessions"].add(r["session_id"]); d["utts"] += 1; d["chars"] += len(r["text"])
        ps = proj_stats.setdefault(r["project"], {"sessions": set(), "utts": 0, "chars": 0, "buckets": Counter()})
        ps["sessions"].add(r["session_id"]); ps["utts"] += 1; ps["chars"] += len(r["text"]); ps["buckets"][r["bucket"]] += 1

    total_utts, total_chars, total_sessions = len(all_records), sum(len(r["text"]) for r in all_records), len({r["session_id"] for r in all_records})
    sum_lines = [
        "# 个人热词语料抽取统计摘要", "",
        f"- 采集时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 合计去重：{total_sessions} 个会话 / {total_utts} 条有效发言 / {total_chars:,} 字符（跨源去重 {dedup_count} 条）", "",
        "## 一、各来源纳入统计", "",
        *_md_table(("主机", "来源", "分桶", "会话数", "发言条数", "字符量"), (":---", ":---", ":---", "---:", "---:", "---:"),
                   [(h, s, f"`{b}`", len(d["sessions"]), d["utts"], f"{d['chars']:,}") for (h, s, b), d in sorted(by_src.items())]),
        "", "## 二、各来源过滤明细", "",
    ]
    for hd in hosts_data:
        tr, ev = hd.get("transcripts") or {}, hd.get("eval_runs") or {}
        sum_lines += [
            f"### 主机：{hd['host']}",
            f"- transcripts: exists={tr.get('exists')}, stats={json.dumps(tr.get('stats') or {}, ensure_ascii=False)}",
            f"- eval_runs: exists={ev.get('exists')}, included={ev.get('included')}, files={ev.get('file_count', 0)}, samples={len(ev.get('samples') or [])}",
            *(f"- {k}: {json.dumps((hd.get(k) or {}).get('stats') or {}, ensure_ascii=False)}" for k in ("agy", "codex", "claude")), "",
        ]
    sum_lines += [
        "## 三、项目分布（Top 30）", "",
        *_md_table(("项目", "会话数", "发言条数", "字符量", "分桶分布"), (":---", "---:", "---:", "---:", ":---"),
                   [(f"`{p}`", len(ps["sessions"]), ps["utts"], f"{ps['chars']:,}", ", ".join(f"{k}:{v}" for k, v in ps["buckets"].most_common()))
                    for p, ps in sorted(proj_stats.items(), key=lambda kv: -kv[1]["utts"])[:30]]),
    ]
    _write_private_exclusive(out_dir / "corpus_summary.md", "\n".join(sum_lines) + "\n")

    return {
        "output_dir": str(out_dir), "total_sessions": total_sessions, "total_utterances": total_utts,
        "total_chars": total_chars, "deduped_utterances": dedup_count,
        "english_candidates": len(en_terms), "english_exported": len(en_top),
        "chinese_candidates": len(cn_terms), "chinese_exported": len(cn_top),
        "transcript_mishearing_clusters": len(tr_clusters), "eval_mishearing_clusters": len(ev_clusters),
        "cross_asr_diffs": len(cross_asr_diffs), "assistant_echoes": len(asst_echoes),
        "user_corrections": len(user_corrs), "archive_project_utterances": len(arch_records),
        "eval_runs_detected": any((hd.get("eval_runs") or {}).get("exists") for hd in hosts_data),
        "eval_runs_included": any((hd.get("eval_runs") or {}).get("included") for hd in hosts_data),
        "transcripts_detected": any((hd.get("transcripts") or {}).get("exists") for hd in hosts_data),
    }


def _collect_local_summary(payload: dict[str, Any], out_file: Path) -> dict[str, Any]:
    tr, ev = payload.get("transcripts") or {}, payload.get("eval_runs") or {}
    return {
        "out_file": str(out_file),
        "counts": {
            "transcript_utterances": len(tr.get("utterances") or []), "transcript_pairs": len(tr.get("pairs") or []),
            "eval_samples": len(ev.get("samples") or []),
            "claude_utterances": len((payload.get("claude") or {}).get("utterances") or []),
            "codex_utterances": len((payload.get("codex") or {}).get("utterances") or []),
            "agy_utterances": len((payload.get("agy") or {}).get("utterances") or []),
            "echoes": sum(len((payload.get(k) or {}).get("echoes") or []) for k in ("claude", "codex", "agy")),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only corpus extractor for agent-voice-input personal hotwords curation.")
    parser.add_argument("--days", type=int, default=30, help="Lookback window in days (default: 30)")
    parser.add_argument("--transcripts-dir", type=Path, default=None, help="Override archived transcripts directory")
    parser.add_argument("--eval-runs-dir", type=Path, default=None, help="Override eval-runs directory")
    parser.add_argument("--include-eval-runs", action="store_true", help="Include eval-runs replay files and asr_cache.json")
    parser.add_argument("--no-agent-sessions", action="store_true", help="Skip scanning local Claude/Codex/Antigravity sessions")
    parser.add_argument("--exclude-project", action="append", default=[], help="Exclude projects matching substring or glob")
    parser.add_argument("--archive-project", action="append", default=[], help="Down-weight historical (>30d) utterances (0.08) and emit archive_project_terms.md")
    parser.add_argument("--output-dir", type=Path, default=None, help="New or empty directory outside repo/sources for exclusive 0700 output")
    parser.add_argument("--top-en", type=int, default=500, help="Max English/mixed candidates to export (default: 500)")
    parser.add_argument("--top-cn", type=int, default=400, help="Max Chinese candidates to export (default: 400)")
    parser.add_argument("--collect-local", action="store_true", help="Write private 0600 local corpus JSON outside repo (stdout prints path & counts only)")
    parser.add_argument("--out-file", type=Path, default=None, help="Exclusive 0600 target JSON path for --collect-local")
    parser.add_argument("--host-label", default="local", help="Host label for reports or --collect-local")
    parser.add_argument("--merge-json", action="append", default=[], help="Path(s) to JSON files produced by --collect-local on other machines")
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be >= 1")
    if args.out_file and not args.collect_local:
        parser.error("--out-file can only be used with --collect-local")

    transcripts_dir = args.transcripts_dir.expanduser() if args.transcripts_dir else resolve_archive_transcripts_dir(create=False)
    eval_runs_dir = args.eval_runs_dir.expanduser() if args.eval_runs_dir else (resolve_cache_dir() / "eval-runs")
    forbidden_roots = _build_forbidden_roots(transcripts_dir, eval_runs_dir)
    exclude_patterns = tuple(p.strip() for p in args.exclude_project if p and p.strip())
    archive_patterns = tuple(p.strip() for p in args.archive_project if p and p.strip())
    start_ymd, end_ymd, recent_cutoff = _compute_window(args.days)

    try:
        if args.collect_local:
            out_file_path = _prepare_out_file(args.out_file, forbidden_roots) if args.out_file else (_prepare_exclusive_output_dir(args.output_dir, forbidden_roots) / "local_corpus.json")
            out_dir_path, merged_extras = None, []
        else:
            seen_hosts = {args.host_label}
            merged_extras = [
                _load_and_validate_merge_file(
                    mp,
                    expected_days=args.days,
                    expected_start_ymd=start_ymd,
                    expected_end_ymd=end_ymd,
                    expected_include_eval_runs=args.include_eval_runs,
                    expected_no_agent=args.no_agent_sessions,
                    expected_exclude_projects=exclude_patterns,
                    expected_archive_projects=archive_patterns,
                    seen_hosts=seen_hosts,
                )
                for mp in args.merge_json
            ]
            out_dir_path, out_file_path = _prepare_exclusive_output_dir(args.output_dir, forbidden_roots), None
    except (ValueError, OSError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    local_payload = collect_local(
        host=args.host_label, days=args.days, transcripts_dir=transcripts_dir, eval_runs_dir=eval_runs_dir,
        include_eval_runs=args.include_eval_runs, no_agent_sessions=args.no_agent_sessions,
        exclude_patterns=exclude_patterns, archive_patterns=archive_patterns,
        start_ymd=start_ymd, end_ymd=end_ymd, recent_cutoff=recent_cutoff,
    )
    if args.collect_local:
        assert out_file_path is not None
        _write_private_exclusive(out_file_path, json.dumps(local_payload, ensure_ascii=False))
        print(json.dumps(_collect_local_summary(local_payload, out_file_path), ensure_ascii=False, indent=2))
        return 0
    assert out_dir_path is not None
    summary = write_outputs(out_dir_path, [local_payload, *merged_extras], top_en=max(1, args.top_en), top_cn=max(1, args.top_cn))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
