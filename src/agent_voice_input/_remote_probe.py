"""Self-contained pure-stdlib read-only remote session probe for ``ssh <target> python3 -``.

CRITICAL INVARIANT: This module MUST NOT use any intra-package imports (``from . import ...``)
or third-party libraries because its raw source bytes are piped over ``stdin`` into bare
remote ``python3 -`` processes without ``agent-voice-input`` installed on the remote host.
"""
from __future__ import annotations

import errno
import fcntl
import glob
import gzip
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import stat
import struct
import subprocess
import sys
from typing import Any, BinaryIO, Mapping, Sequence
import urllib.parse

PROBE_FRAME_MAGIC = b"__AVI_GZ__\x00"
MAX_LINE_BYTES = 16 * 1024 * 1024
DEFAULT_REFINER_CONTEXT_MAX_CHARS = 200000
DEFAULT_REFINER_CHUNK_CHARS = DEFAULT_REFINER_CONTEXT_MAX_CHARS // 4
COLD_START_PRIOR_MAX_CHARS = 25000
COLD_START_PRIOR_MAX_TURNS = 500
COLD_START_PRIOR_MAX_HOTWORDS = 48
COLD_START_SWEET_MIN_TURNS = 4
COLD_START_SWEET_MIN_CHARS = 3000
COLD_START_FALLBACK_MIN_CHARS = 1000
COLD_START_TOP_K = 5
COLD_START_CODEX_SCAN_LIMIT = 100
COLD_START_AGY_SCAN_LIMIT = 48
COLD_START_CLAUDE_PROJ_SCAN_LIMIT = 64

PRIOR_SESSION_PREFIX = (
    '<prior_session_reference read_only="true" scope="same_workspace_previous_session">\n'
    "[READ-ONLY PRIOR SESSION REFERENCE — Excerpt from an earlier conversation in the same workspace. "
    "You are NOT a participant; use it ONLY for workspace terminology, identifiers, and spelling. "
    "If <session_reference_log> or <workspace_git_status> shows a different current topic, always follow "
    "the current session and workspace.]\n"
)
PRIOR_SESSION_SUFFIX = "\n</prior_session_reference>"
PRIOR_SESSION_WRAPPER_OVERHEAD_CHARS = len(PRIOR_SESSION_PREFIX) + len(PRIOR_SESSION_SUFFIX)

SESSION_REF_HEADER_BODY = (
    "[READ-ONLY BACKGROUND LOG — Transcript between the speaker and a separate downstream CLI agent. "
    "You are NOT a participant in this dialogue; use it ONLY to spell filenames, identifiers, and "
    "domain terms correctly, and never reply to it or copy its wording into the output.]\n"
)
SESSION_REF_SUFFIX = "\n</session_reference_log>"
SESSION_REF_WRAPPER_OVERHEAD_CHARS = (
    len('<session_reference_log read_only="true">\n')
    + len(SESSION_REF_HEADER_BODY)
    + len(SESSION_REF_SUFFIX)
)

_TECH_EXT = (
    r"(?:jsonl|json|pyi|py|sh|ps1|swift|toml|md|tsx|ts|jsx|mjs|js|vue|svelte|scss|css|html|sql|rs|go"
    r"|cpp|cc|c|hpp|h|java|kt|rb|php|yaml|yml|lock|ini|cfg|zsh|bash|env)"
)
_TECH_IDENT_RE = re.compile(
    rf"(?<![A-Za-z0-9_.\-])(?P<file>[A-Za-z0-9_][A-Za-z0-9_.\-]*\.{_TECH_EXT})(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_.\-])(?P<flag>--[a-z][a-z0-9]*(?:-[a-z0-9]+)*)(?![A-Za-z0-9_\-])"
    r"|(?<![A-Za-z0-9_])(?P<snake>_*[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+)(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_])(?P<camel>[a-z][a-z0-9]*(?:[A-Z][a-z0-9]*)+)(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_])(?P<pascal>[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+|[A-Z]{2,}[a-z0-9]{2,}(?:[A-Z][a-z0-9]*)*)(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_.\-])(?P<kebab>[A-Za-z][A-Za-z0-9]*(?:\.[0-9]+)*(?:-[A-Za-z0-9]+(?:\.[0-9]+)*)+)(?![A-Za-z0-9_\-])"
)
_KEBAB_STOP_SEGS = frozenset(
    "a an and as at by for from in into is it non of off on one or out over per re self step the to up well with".split()
)
_KEBAB_STOP_WORDS = frozenset({
    "read-only", "real-time", "short-term", "long-term", "high-level", "low-level",
    "trade-off", "trade-offs", "code-switching", "false-positive", "false-positives",
    "open-source", "full-text", "plain-text", "single-hop", "two-stage", "multi-turn",
    "utf-8", "utf-16", "sha-256", "covid-19",
})
_KEBAB_NUM_PREFIX_STOP = frozenset({
    "top", "utf", "sha", "iso", "rfc", "http", "line", "step", "part", "item", "page", "phase", "wave", "rule",
})
_IDENT_STOP = frozenset({"__init__", "__main__"})
_VER_ONLY_RE = re.compile(r"^[vV]?[0-9]+(?:\.[0-9]+)*(?:-[A-Za-z0-9.]+)?$")
_HASH_HEX_RE = re.compile(r"^[0-9a-fA-F]{7,}(?:-[0-9a-fA-F]{4,})+$")

_TOKEN_RE = re.compile(r"^[0-9a-f]{6}[0-9a-f-]{0,58}$")
_STRICT_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_PREFIXED_TAG_RE = re.compile(
    r"\[(agy|claude|codex):#?([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}|[0-9a-fA-F]{8,36})\]",
    re.IGNORECASE,
)
_HASH_TAG_RE = re.compile(
    r"\[[#§]([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}|[0-9a-fA-F]{6,36})\](?=$|[^0-9A-Za-z_])"
)
_FULL_UUID_RE = re.compile(
    r"(?:^|[^0-9a-fA-F-])([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?=$|[^0-9a-fA-F-])"
)
_WORD_CLI_RE = re.compile(
    r"(?:^|[\s\[\(\{·|:])(agy|antigravity|claude|codex)(?=$|[\s\]\)\}·|:])",
    re.IGNORECASE,
)
_CLAUDE_VER_COMM_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?$")
_PYTHON_COMM_RE = re.compile(r"^python(?:\d+(?:\.\d+)*)?$")
_INTERP_COMMS = frozenset({"node", "nodejs", "bun", "deno"})
_TMUX_PANE_ID_RE = re.compile(r"^%\d+$")

_SIDECHAIN_TRUE_RE = re.compile(rb'"isSidechain"\s*:\s*true\b')
_SIDECHAIN_FALSE_RE = re.compile(rb'"isSidechain"\s*:\s*false\b')
_CLAUDE_SUBAGENT_HEAD_RE = re.compile(
    rb'"(?:agentId|agent_id)"\s*:\s*"[^"]+"|"entrypoint"\s*:\s*"sdk-cli"'
)
_AGY_WS_CWD_RE = re.compile(
    r"(?:Command Working Directory:\s*|Active Workspaces:\s*\r?\n-\s*)(/[^\r\n<]+)"
)
_AGY_TRAILING_META_RE = re.compile(
    r"\n<(?:ADDITIONAL_METADATA|SKILL|USER_SETTINGS_CHANGE)\b.*", re.DOTALL
)
_AGY_CHECKPOINT_PREAMBLE_RE = re.compile(
    r"^\s*#\s*Resuming from a compaction\b.*?progress summary below:\s*",
    re.DOTALL | re.IGNORECASE,
)
_AGY_CHECKPOINT_TAIL_RE = re.compile(
    r"\n#\s*(?:Subagents|Conversation Logs)\b.*", re.DOTALL
)
_CLAUDE_SYSTEM_REMINDER_RE = re.compile(
    r"(?<!`)<system-reminder>(?!`).*?(?<!`)</system-reminder>(?!`)",
    re.DOTALL | re.IGNORECASE,
)
_CLAUDE_USER_STRIP_PATTERNS = (
    _CLAUDE_SYSTEM_REMINDER_RE,
    re.compile(
        r"(?<!`)<task-notification>(?!`).*?(?<!`)</task-notification>(?!`)",
        re.DOTALL | re.IGNORECASE,
    ),
    re.compile(
        r"(?<!`)<(local-command-[a-zA-Z0-9_-]+)(?:\s[^>]*)?>(?!`).*?(?<!`)</\1>(?!`)",
        re.DOTALL | re.IGNORECASE,
    ),
    re.compile(
        r"(?<!`)<(command-[a-zA-Z0-9_-]+)(?:\s[^>]*)?>(?!`).*?(?<!`)</\1>(?!`)",
        re.DOTALL | re.IGNORECASE,
    ),
    re.compile(
        r"(?<!`)<(bash-[a-zA-Z0-9_-]+)(?:\s[^>]*)?>(?!`).*?(?<!`)</\1>(?!`)",
        re.DOTALL | re.IGNORECASE,
    ),
)
_CLAUDE_USER_ORPHAN_TAG_RE = re.compile(
    r"(?<!`)</?(?:task-notification|system-reminder|local-command-[a-zA-Z0-9_-]+|command-[a-zA-Z0-9_-]+|bash-[a-zA-Z0-9_-]+)(?:\s[^>]*)?>(?!`)",
    re.IGNORECASE,
)
_CODEX_ENV_CTX_RE = re.compile(
    r"<environment_context>.*?</environment_context>", re.DOTALL | re.IGNORECASE
)
_CODEX_PERM_CTX_RE = re.compile(
    r"<permissions instructions>.*?</permissions instructions>", re.DOTALL | re.IGNORECASE
)
_CODEX_REC_PLUGINS_RE = re.compile(
    r"<recommended_plugins>.*?</recommended_plugins>", re.DOTALL | re.IGNORECASE
)

_BROAD_SYSTEM_DIRS_LOW = frozenset(
    {
        "/",
        "/tmp",
        "/private",
        "/private/tmp",
        "/var",
        "/private/var",
        "/users",
        "/home",
        "/root",
        "/private/var/root",
        "/etc",
        "/private/etc",
        "/usr",
        "/system",
        "/library",
        "/applications",
        "/opt",
        "/srv",
        "/mnt",
    }
)
_REPO_SKIP_DIRS = frozenset(
    {
        "node_modules",
        "vendor",
        "dist",
        "build",
        "out",
        "target",
        "third_party",
        "docs",
        "doc",
        "examples",
        "fixtures",
        "testdata",
        "test",
        "tests",
        "__tests__",
        "spec",
        "__pycache__",
        ".github",
        "migrations",
    }
)
_REPO_SKIP_BASES = frozenset(
    {
        "__init__.py",
        "__main__.py",
        "conftest.py",
        "setup.py",
        "index.ts",
        "index.js",
        "index.tsx",
        "mod.rs",
        "main.go",
        "package-lock.json",
    }
)
_REPO_CORE_EXTS = frozenset(
    ".py .ts .tsx .js .vue .svelte .go .rs .swift .sh .ps1 .kt .java .rb .c .cpp .h .toml .sql".split()
)
_REPO_TEST_FILE_RE = re.compile(r"^test_|(?:_test|\.test|\.spec)\.", re.IGNORECASE)
_REPO_PRIORITY_ROOTS = frozenset({"src", "lib", "app", "pkg", "cmd", "internal"})
_SHALLOW_SKIP_DIRS_LOW = frozenset(
    d.lower()
    for d in (
        _REPO_SKIP_DIRS
        | {
            ".git",
            ".hg",
            ".svn",
            ".venv",
            "venv",
            "env",
            ".env",
            ".tox",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            ".next",
            ".nuxt",
            ".turbo",
            ".cache",
            "coverage",
            "htmlcov",
            "Pods",
            "DerivedData",
            "Library",
            "Logs",
            "Temp",
            "tmp",
        }
    )
)
_SHALLOW_DOC_AND_CODE_EXTS = _REPO_CORE_EXTS | frozenset(
    ".md .markdown .mdx .txt .rst .adoc .org .tex .pdf .doc .docx .xls .xlsx .ppt .pptx "
    ".csv .tsv .json .jsonl .yaml .yml .xml .html .css .scss .ipynb .proto .graphql .prisma".split()
)
_SHALLOW_SKIP_BASES_LOW = frozenset(
    b.lower() for b in (_REPO_SKIP_BASES | {".ds_store", "thumbs.db", "desktop.ini"})
)


def trim_turns_chunk_aligned(
    turns: Sequence[tuple[str, str]],
    *,
    max_chars: int = DEFAULT_REFINER_CONTEXT_MAX_CHARS,
    chunk_chars: int | None = None,
) -> tuple[list[tuple[str, str]], int]:
    """Evict whole forward-anchored 50K chunks so ``chunk_turns_for_refiner`` stays 100% byte-identical."""
    if not turns or max_chars <= 0:
        return [], 0
    default_chunk = (
        DEFAULT_REFINER_CHUNK_CHARS
        if max_chars == DEFAULT_REFINER_CONTEXT_MAX_CHARS
        else max_chars // 4
    )
    target_chunk = max(256, default_chunk) if chunk_chars is None else int(chunk_chars)
    eff_chunk = max(256, min(target_chunk, max(256, max_chars // 2)))

    norm_turns: list[tuple[str, str]] = []
    chunk_meta: list[tuple[int, int]] = []  # (start_turn_idx, chunk_char_len)
    cur_start = 0
    cur_len = 0
    has_cur = False

    for idx, (role, text) in enumerate(turns):
        norm_role = "user" if role == "user" else "assistant"
        prefix_len = 10 if norm_role == "user" else 12  # "[Speaker]\n" vs "[CLI Agent]\n"
        if prefix_len + len(text) > max_chars:
            rem = max(0, max_chars - prefix_len)
            eff_text = text[-rem:] if rem > 0 else ""
            entry_len = (prefix_len + rem) if rem > 0 else (prefix_len - 1)
        else:
            eff_text = text
            entry_len = prefix_len + len(text)
        norm_turns.append((norm_role, eff_text))

        if has_cur and cur_len + 2 + entry_len > eff_chunk:
            chunk_meta.append((cur_start, cur_len))
            cur_start = idx
            cur_len = entry_len
        else:
            if not has_cur:
                cur_start = idx
                has_cur = True
                cur_len = entry_len
            else:
                cur_len += 2 + entry_len

    if has_cur:
        chunk_meta.append((cur_start, cur_len))

    kept_chunks = 0
    total = 0
    first_kept_idx = len(norm_turns)
    for start_i, ch_len in reversed(chunk_meta):
        sep = 2 if kept_chunks > 0 else 0
        if kept_chunks > 0 and total + sep + ch_len > max_chars:
            break
        if kept_chunks == 0 and ch_len > max_chars:
            first_kept_idx = start_i
            kept_chunks = 1
            break
        first_kept_idx = start_i
        kept_chunks += 1
        total += sep + ch_len

    evicted_chunks = max(0, len(chunk_meta) - kept_chunks)
    return norm_turns[first_kept_idx:], evicted_chunks


def _keep_tech_token(m: re.Match[str]) -> str | None:
    tok = m.group(0)
    if not (4 <= len(tok) <= 48) or tok in _IDENT_STOP:
        return None
    if m.group("file"):
        stem = tok.rsplit(".", 1)[0]
        return tok if len(stem) >= 2 and any(c.isalpha() for c in stem) else None
    if m.group("kebab"):
        low = tok.lower()
        if len(tok) < 5 or low in _KEBAB_STOP_WORDS or _VER_ONLY_RE.match(tok) or _HASH_HEX_RE.match(tok):
            return None
        segs = low.split("-")
        if _KEBAB_STOP_SEGS.intersection(segs):
            return None
        if len(segs) == 2 and segs[1].isdigit() and segs[0] in _KEBAB_NUM_PREFIX_STOP:
            return None
    return tok


def _extract_session_hotword_counts(
    turns: Sequence[tuple[str, str]], *, max_words: int = COLD_START_PRIOR_MAX_HOTWORDS
) -> tuple[tuple[str, int], ...]:
    if not turns or max_words <= 0:
        return ()
    canonical: dict[str, str] = {}
    counts: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    order = 0
    for _, text in reversed(turns[-40:]):
        for m in _TECH_IDENT_RE.finditer(text):
            tok = _keep_tech_token(m)
            if not tok:
                continue
            low = tok.lower()
            if low not in canonical:
                canonical[low] = tok
                first_seen[low] = order
                order += 1
            prev = counts.get(low, 0)
            if prev < 20:
                counts[low] = prev + 1
    ranked = sorted(canonical.keys(), key=lambda low: (-counts[low], first_seen[low]))
    return tuple((canonical[low], counts[low]) for low in ranked[:max_words])


def extract_prior_sessions_hotwords(
    sessions_turns: Sequence[Sequence[tuple[str, str]]],
    *,
    max_words: int = 24,
) -> list[str]:
    """Aggregate high-frequency technical identifiers across recent prior same-cwd sessions."""
    if not sessions_turns or max_words <= 0:
        return []
    per_sess = [
        {"hotword_counts": _extract_session_hotword_counts(turns, max_words=max(max_words, COLD_START_PRIOR_MAX_HOTWORDS))}
        for turns in sessions_turns
    ]
    return aggregate_prior_candidates_hotwords(per_sess, max_words=max_words)


def aggregate_prior_candidates_hotwords(
    candidates: Sequence[Mapping[str, Any]],
    *,
    max_words: int = 24,
) -> list[str]:
    """Aggregate high-frequency technical identifiers from slimmed prior candidate dicts."""
    if not candidates or max_words <= 0:
        return []
    canonical: dict[str, str] = {}
    doc_freq: dict[str, int] = {}
    term_freq: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    order = 0
    for cand in candidates:
        hw_counts = cand.get("hotword_counts")
        if hw_counts is None:
            raw_turns = cand.get("tail_turns") or cand.get("turns") or ()
            hw_counts = _extract_session_hotword_counts(
                raw_turns, max_words=max(max_words, COLD_START_PRIOR_MAX_HOTWORDS)
            )
        seen_in_doc: set[str] = set()
        for tok, cnt in hw_counts:
            low = str(tok).lower()
            if not low or low in seen_in_doc:
                continue
            seen_in_doc.add(low)
            if low not in canonical:
                canonical[low] = str(tok)
                first_seen[low] = order
                order += 1
            doc_freq[low] = doc_freq.get(low, 0) + 1
            term_freq[low] = term_freq.get(low, 0) + max(1, int(cnt))
    ranked = sorted(
        canonical.keys(),
        key=lambda low: (-doc_freq[low], -term_freq[low], first_seen[low]),
    )
    return [canonical[low] for low in ranked[:max_words]]


def prior_chunk_body_budget(
    max_chars: int, *, prior_max_chars: int = COLD_START_PRIOR_MAX_CHARS
) -> int:
    """Return max prior chunk body chars so wrapped ``<prior_session_reference>`` stays <= both caps."""
    if max_chars <= 0:
        return 0
    total_cap = min(max(0, int(prior_max_chars)), max(0, int(max_chars)))
    return max(0, total_cap - PRIOR_SESSION_WRAPPER_OVERHEAD_CHARS)


def slice_prior_turns_tail(
    turns: Sequence[tuple[str, str]],
    *,
    max_chars: int = COLD_START_PRIOR_MAX_CHARS,
    max_turns: int = COLD_START_PRIOR_MAX_TURNS,
) -> list[tuple[str, str]]:
    """Return trailing complete turns fitting within ``max_chars`` formatted length and ``max_turns``."""
    if not turns or max_chars <= 0 or max_turns <= 0:
        return []
    kept: list[tuple[str, str]] = []
    total = 0
    for role, text in reversed(turns):
        if len(kept) >= max_turns:
            break
        norm_role = "user" if role == "user" else "assistant"
        prefix_len = 10 if norm_role == "user" else 12  # "[Speaker]\n" vs "[CLI Agent]\n"
        entry_len = prefix_len + len(text)
        sep = 2 if kept else 0
        if kept and total + sep + entry_len > max_chars:
            break
        if not kept and entry_len > max_chars:
            rem = max(0, max_chars - prefix_len)
            if rem > 0:
                kept.append((norm_role, text[-rem:]))
            break
        kept.append((norm_role, text))
        total += sep + entry_len
    kept.reverse()
    return kept


def format_prior_turns_body(
    turns: Sequence[tuple[str, str]],
    *,
    max_chars: int = COLD_START_PRIOR_MAX_CHARS,
    max_turns: int = COLD_START_PRIOR_MAX_TURNS,
) -> str:
    """Format trailing prior turns as a single frozen reference chunk body (<= ``max_chars``)."""
    sliced = slice_prior_turns_tail(turns, max_chars=max_chars, max_turns=max_turns)
    if not sliced:
        return ""
    return "\n\n".join(
        f"[{'Speaker' if r == 'user' else 'CLI Agent'}]\n{t}" for r, t in sliced
    )


def build_slim_prior_candidate(
    *,
    cli: str,
    session_id: str,
    jsonl_path: str,
    cwd: str,
    jsonl_mtime: float,
    turns: Sequence[tuple[str, str]],
) -> dict[str, Any] | None:
    """Build a memory-bounded prior candidate keeping only counts, bounded hotwords, and <=25K tail turns."""
    total_chars = sum(len(t) for _, t in turns)
    if not turns or total_chars < COLD_START_FALLBACK_MIN_CHARS:
        return None
    tail_budget = prior_chunk_body_budget(COLD_START_PRIOR_MAX_CHARS)
    tail_turns = tuple(
        slice_prior_turns_tail(turns, max_chars=tail_budget, max_turns=COLD_START_PRIOR_MAX_TURNS)
    )
    if not tail_turns:
        return None
    return {
        "cli": cli,
        "session_id": session_id.lower(),
        "jsonl_path": jsonl_path,
        "cwd": cwd,
        "jsonl_mtime": float(jsonl_mtime),
        "turn_count": len(turns),
        "total_chars": total_chars,
        "hotword_counts": _extract_session_hotword_counts(turns, max_words=COLD_START_PRIOR_MAX_HOTWORDS),
        "tail_turns": tail_turns,
    }


def _is_wsl_slow_mount_str(path_str: str) -> bool:
    """Pure-string zero-I/O check for WSL2 ``/mnt/`` DrvFS, Windows drive, and UNC paths."""
    s = (path_str or "").strip()
    if not s:
        return False
    if s.lower().startswith("file://"):
        try:
            parsed = urllib.parse.urlparse(s)
        except ValueError:
            return True
        if parsed.netloc and parsed.netloc.lower() != "localhost":
            return True
        s = urllib.parse.unquote(parsed.path or "")
    if s.startswith(("\\\\", "//")) or (len(s) >= 2 and s[0].isalpha() and s[1] == ":"):
        return True
    if s.startswith(("/", "\\")):
        norm = posixpath.normpath("/" + s.replace("\\", "/").lstrip("/"))
        return norm == "/mnt" or norm.startswith("/mnt/")
    return False


def _normalize_cwd(raw_cwd: str, *, wsl: bool = False) -> str:
    s = (raw_cwd or "").strip()
    if not s or "\x00" in s:
        return ""
    if wsl and _is_wsl_slow_mount_str(s):
        return ""
    if s.startswith("file://") or s.lower().startswith("file://"):
        try:
            parsed = urllib.parse.urlparse(s)
        except ValueError:
            return ""
        s = urllib.parse.unquote(parsed.path or "")
        if not s or "\x00" in s:
            return ""
        if wsl and _is_wsl_slow_mount_str(s):
            return ""
    if len(s) > 1:
        s = s.rstrip("/")
    return s


def _expand_remote_path(raw_path: str, home: Path) -> str:
    s = (raw_path or "").strip()
    if not s or "\x00" in s:
        return ""
    if s == "~":
        return _normalize_cwd(str(home))
    if s.startswith("~/"):
        return _normalize_cwd(str(home / s[2:]))
    return _normalize_cwd(s)


def _safe_realpath(path_str: str, *, wsl: bool = False) -> str:
    """Resolve symlinks without ever calling ``lstat``/``stat`` on WSL2 ``/mnt/`` slow mounts."""
    if not path_str or "\x00" in path_str or (wsl and _is_wsl_slow_mount_str(path_str)):
        return ""
    if not wsl:
        try:
            return _normalize_cwd(os.path.realpath(path_str))
        except OSError:
            return ""
    abs_p = path_str if path_str.startswith("/") else os.path.abspath(path_str)
    if _is_wsl_slow_mount_str(abs_p):
        return ""
    todo = [seg for seg in abs_p.split("/") if seg and seg != "."]
    resolved: list[str] = []
    hops = 0
    while todo:
        seg = todo.pop(0)
        if seg == "..":
            if resolved:
                resolved.pop()
            continue
        cand = "/" + "/".join([*resolved, seg])
        if _is_wsl_slow_mount_str(cand):
            return ""
        try:
            st = os.lstat(cand)
        except OSError as exc:
            if exc.errno != errno.ENOENT or ".." in todo:
                return ""
            tail = posixpath.normpath("/" + "/".join([*resolved, seg, *todo]))
            return "" if _is_wsl_slow_mount_str(tail) else _normalize_cwd(tail)
        if stat.S_ISLNK(st.st_mode):
            hops += 1
            if hops > 40:
                return ""
            try:
                target = os.readlink(cand)
            except OSError:
                return ""
            if _is_wsl_slow_mount_str(target):
                return ""
            if target.startswith("/"):
                resolved = []
            todo = [x for x in target.split("/") if x and x != "."] + todo
        else:
            resolved.append(seg)
    out = "/" + "/".join(resolved)
    return "" if _is_wsl_slow_mount_str(out) else _normalize_cwd(out)


def _cwd_variants(cwd: str, *, wsl: bool = False) -> set[str]:
    if wsl and _is_wsl_slow_mount_str(cwd):
        return set()
    norm = _normalize_cwd(cwd)
    if not norm or (wsl and _is_wsl_slow_mount_str(norm)):
        return set()
    out = {norm}
    real = _safe_realpath(norm, wsl=wsl)
    if wsl and not real:
        return set()
    if real:
        out.add(real)
    return out


def _is_broad_or_unsafe_workspace_dir(
    dir_path: str, home: Path | None = None, *, wsl: bool = False
) -> bool:
    if not dir_path or (wsl and _is_wsl_slow_mount_str(dir_path)):
        return True
    raw_norm = _normalize_cwd(dir_path)
    if not raw_norm or (wsl and _is_wsl_slow_mount_str(raw_norm)):
        return True
    norm = posixpath.normpath(raw_norm)
    if not norm or norm == "." or not norm.startswith("/"):
        return True
    if wsl and _is_wsl_slow_mount_str(norm):
        return True

    cands = {norm}
    real = _safe_realpath(norm, wsl=wsl)
    if wsl and not real:
        return True
    if real:
        real_norm = posixpath.normpath(real)
        if real_norm and real_norm.startswith("/"):
            cands.add(real_norm)

    h_vars: set[str] = set()
    if home is not None:
        h_raw = _normalize_cwd(str(home))
        if h_raw:
            h_norm = posixpath.normpath(h_raw)
            if h_norm and h_norm != ".":
                h_vars.add(h_norm)
            h_real = _safe_realpath(h_raw, wsl=wsl)
            if h_real:
                h_real_norm = posixpath.normpath(h_real)
                if h_real_norm and h_real_norm != ".":
                    h_vars.add(h_real_norm)
    h_vars_low = {p.lower() for p in h_vars}

    for cand in cands:
        cand_low = cand.lower()
        if cand_low in _BROAD_SYSTEM_DIRS_LOW:
            return True
        if posixpath.dirname(cand).lower() in ("/home", "/users"):
            return True
        if cand in h_vars or cand_low in h_vars_low:
            return True
    return False


def _cwd_match_rank(
    target_cwd: str,
    cand_cwd: str,
    home: Path | None = None,
    *,
    wsl: bool = False,
) -> int:
    """Return 2 on exact directory match, 1 on bounded subdirectory match, 0 otherwise."""
    t_vars = _cwd_variants(target_cwd, wsl=wsl)
    c_vars = _cwd_variants(cand_cwd, wsl=wsl)
    if not t_vars or not c_vars:
        return 0
    if t_vars & c_vars:
        return 2
    for tv in t_vars:
        for cv in c_vars:
            if (
                not _is_broad_or_unsafe_workspace_dir(cv, home, wsl=wsl)
                and tv.startswith(cv + "/")
            ) or (
                not _is_broad_or_unsafe_workspace_dir(tv, home, wsl=wsl)
                and cv.startswith(tv + "/")
            ):
                return 1
    return 0


def encode_claude_project_dir(cwd: str, *, wsl: bool = False) -> list[str]:
    """Return deduplicated Claude project directory names for logical and real ``cwd``."""
    if wsl and _is_wsl_slow_mount_str(cwd):
        return []
    norm = _normalize_cwd(cwd)
    if not norm or (wsl and _is_wsl_slow_mount_str(norm)):
        return []
    candidates: list[str] = [norm]
    real = _safe_realpath(norm, wsl=wsl)
    if real:
        candidates.append(real)
        for pfx in ("/private/var/", "/private/tmp/", "/private/etc/"):
            if real.startswith(pfx):
                unprefixed = real[len("/private") :]
                if _safe_realpath(unprefixed, wsl=wsl) == real:
                    candidates.append(unprefixed)
                break
    out: list[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        enc = re.sub(r"[^A-Za-z0-9]", "-", candidate)
        if enc and enc not in out:
            out.append(enc)
    return out


_encode_claude_dirs = encode_claude_project_dir


def _safe_mtime(path: str | Path) -> float:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


def _file_etag(path: str | Path) -> str:
    try:
        st = os.stat(path)
        if not stat.S_ISREG(st.st_mode):
            return ""
        return f"{st.st_dev}:{st.st_ino}:{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        return ""


def _read_bounded_line(f: BinaryIO, max_bytes: int = MAX_LINE_BYTES) -> tuple[bytes, bool]:
    """Read one ``\\n``-terminated line up to ``max_bytes`` total bytes; drain remainder if exceeded."""
    line = f.readline(max_bytes + 1)
    if not line:
        return b"", False
    if len(line) <= max_bytes:
        return line, False
    if not line.endswith(b"\n"):
        while True:
            chunk = f.readline(65536)
            if not chunk or chunk.endswith(b"\n"):
                break
    return line[:max_bytes], True


def _is_path_under_dir(
    target_path: str | Path, root_dir: Path, *, wsl: bool = False
) -> bool:
    if wsl:
        real_t = _safe_realpath(str(target_path), wsl=True)
        real_r = _safe_realpath(str(root_dir), wsl=True)
        if not real_t or not real_r:
            return False
        return real_t == real_r or real_t.startswith(real_r.rstrip("/") + "/")
    try:
        real_t = os.path.realpath(target_path)
        real_r = os.path.realpath(root_dir)
    except OSError:
        return False
    return real_t == real_r or real_t.startswith(real_r.rstrip(os.sep) + os.sep)


def _resolve_safe_transcript_path(
    cli: str, raw_path: str | Path, home: Path, *, wsl: bool = False
) -> str:
    """Validate transcript path is an owned regular ``.jsonl`` file inside the CLI's transcript root."""
    s = str(raw_path or "").strip()
    if not s or "\x00" in s:
        return ""
    if cli == "codex":
        root = home / ".codex" / "sessions"
    elif cli == "claude":
        root = home / ".claude" / "projects"
    elif cli == "agy":
        root = home / ".gemini" / "antigravity-cli" / "brain"
    else:
        return ""
    if not _is_path_under_dir(s, root, wsl=wsl):
        return ""
    try:
        real_p = _safe_realpath(s, wsl=True) if wsl else os.path.realpath(s)
        if not real_p or not real_p.endswith(".jsonl"):
            return ""
        st = os.stat(real_p)
    except OSError:
        return ""
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
        return ""
    return real_p


def _readlink_clean(path: Path) -> str:
    try:
        t = os.readlink(path)
    except OSError:
        return ""
    return "" if t.endswith(" (deleted)") else t


def _is_script_interpreter_token(tok_low: str) -> bool:
    return bool(tok_low and (tok_low in _INTERP_COMMS or _PYTHON_COMM_RE.match(tok_low)))


def _match_cli_process(comm: str, argv: Sequence[str]) -> tuple[str | None, list[str]]:
    args_list = list(argv)
    c_base = os.path.basename((comm or "").replace("\\", "/")).strip()
    c_low = c_base.lower()
    a0_norm = args_list[0].replace("\\", "/") if args_list else ""
    a0_norm_low = a0_norm.lower()
    a0_low = os.path.basename(a0_norm).lstrip("-").lower() if a0_norm else ""

    for tok in (c_low, a0_low):
        if tok == "codex" or tok.startswith(("codex-aarch64-", "codex-x86_64-")):
            return "codex", args_list
        if tok in ("agy", "antigravity", "antigravity-cli"):
            return "agy", args_list
        if tok == "claude":
            return "claude", args_list
    if "/claude/versions/" in a0_norm_low or "/@anthropic-ai/claude-code/" in a0_norm_low:
        return "claude", args_list
    if "/@openai/codex/" in a0_norm_low:
        return "codex", args_list
    if c_base and _CLAUDE_VER_COMM_RE.match(c_base) and "claude" in a0_norm_low:
        return "claude", args_list

    if _is_script_interpreter_token(c_low) or _is_script_interpreter_token(a0_low):
        idx = 1
        limit = min(len(args_list), 12)
        while idx < limit:
            arg = args_list[idx]
            if arg in ("-c", "-m", "-e", "--eval", "-p", "--print", "--"):
                break
            if arg in (
                "-r",
                "--require",
                "--import",
                "--loader",
                "--experimental-loader",
                "-W",
                "-X",
            ):
                idx += 2
                continue
            if arg.startswith("-"):
                idx += 1
                continue
            norm_low = arg.replace("\\", "/").lower()
            base = os.path.basename(norm_low)
            if (
                base in ("claude", "claude.js", "claude.mjs")
                or "/claude/versions/" in norm_low
                or "/@anthropic-ai/claude-code/" in norm_low
            ):
                return "claude", args_list[idx:]
            if base in ("codex", "codex.js", "codex.mjs") or "/@openai/codex/" in norm_low:
                return "codex", args_list[idx:]
            if base in ("agy", "agy.js", "antigravity", "antigravity-cli"):
                return "agy", args_list[idx:]
            break
    return None, args_list


def _is_headless_cli(cli_kind: str, args: Sequence[str]) -> bool:
    if not args:
        return False
    tail = args[1:]
    if cli_kind in ("agy", "claude"):
        value_flags = {
            "-m", "--model", "--agent", "--agents", "--effort", "--mode",
            "--permission-mode", "--project", "--conversation", "--session-id",
            "--resume", "-r", "--add-dir", "--settings", "--setting-sources",
            "--mcp-config", "--json-schema", "--log-file", "--print-timeout",
            "-i", "--prompt-interactive", "--system-prompt", "--append-system-prompt",
            "--fallback-model", "--max-turns", "--max-budget-usd", "--tools",
            "--allowedTools", "--disallowedTools", "-w", "--worktree",
        }
        skip_next = False
        saw_subcmd = False
        for a in tail:
            if a == "--":
                break
            if a in ("-p", "--print", "--prompt", "--input-format", "--output-format"):
                return True
            if a.startswith(("--prompt=", "--input-format=", "--output-format=")):
                return True
            if skip_next:
                skip_next = False
                if not a.startswith("-"):
                    continue
            if a in value_flags:
                skip_next = True
                continue
            if a.startswith("-"):
                continue
            if not saw_subcmd:
                saw_subcmd = True
                if (cli_kind == "agy" and a == "acp") or (cli_kind == "claude" and a == "mcp"):
                    return True
        return False
    if cli_kind == "codex":
        skip_next = False
        value_flags = {
            "-m", "--model", "-c", "--config", "-C", "--cd", "-p", "--profile",
            "-s", "--sandbox", "-a", "--ask-for-approval", "-i", "--image",
            "--disable", "--enable", "--add-dir", "-o", "--output-last-message",
            "--output-schema", "--color", "--local-provider", "-w", "--writable-root",
        }
        for a in tail:
            if skip_next:
                skip_next = False
                continue
            if a == "--":
                break
            if a in value_flags:
                skip_next = True
                continue
            if a.startswith("-"):
                continue
            if a in (
                "exec", "e", "exec-server", "app-server", "mcp-server",
                "completion", "debug", "cloud", "apply",
            ):
                return True
            break
    return False


def _read_proc_comm(proc_pid_dir: Path) -> str:
    try:
        return (proc_pid_dir / "comm").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _read_proc_cmdline_args(proc_pid_dir: Path) -> list[str]:
    try:
        raw_cmd = (proc_pid_dir / "cmdline").read_bytes()
    except OSError:
        return []
    return [a.decode("utf-8", errors="ignore") for a in raw_cmd.split(b"\x00") if a]


def _classify_interactive_cli(comm: str, argv: Sequence[str]) -> str | None:
    cli_kind, eff_args = _match_cli_process(comm, argv)
    if cli_kind is None or _is_headless_cli(cli_kind, eff_args):
        return None
    return cli_kind


def _classify_proc_cli(proc_pid_dir: Path) -> str | None:
    comm = _read_proc_comm(proc_pid_dir)
    argv = _read_proc_cmdline_args(proc_pid_dir)
    if not comm and not argv:
        return None
    return _classify_interactive_cli(comm, argv)


def _read_proc_tty_mtime(pdir: Path) -> float:
    try:
        stat_txt = (pdir / "stat").read_text(encoding="utf-8", errors="replace")
        rpar = stat_txt.rfind(")")
        if rpar != -1:
            fields = stat_txt[rpar + 2 :].split()
            if len(fields) >= 5:
                tty_nr = int(fields[4])
                if tty_nr > 0:
                    major = (tty_nr >> 8) & 0xFFF
                    minor = (tty_nr & 0xFF) | ((tty_nr >> 12) & 0xFFF00)
                    if 136 <= major <= 143:
                        pts = f"/dev/pts/{(major - 136) * 256 + minor}"
                        if os.path.exists(pts):
                            return _safe_mtime(pts)
    except (OSError, ValueError):
        pass
    fd0 = _readlink_clean(pdir / "fd" / "0")
    if fd0 and fd0 != "/dev/null":
        return _safe_mtime(fd0)
    return 0.0


def _read_proc_ppid(proc_pid_dir: Path) -> int:
    try:
        stat_txt = (proc_pid_dir / "stat").read_text(encoding="utf-8", errors="replace")
        rpar = stat_txt.rfind(")")
        if rpar != -1:
            fields = stat_txt[rpar + 2 :].split()
            if len(fields) >= 2:
                return int(fields[1])
    except (OSError, ValueError):
        pass
    return 0


def _read_codex_root_meta(
    jsonl_path: str | Path, *, wsl: bool = False
) -> tuple[bool, str, str]:
    try:
        with open(jsonl_path, "rb") as f:
            first_line, exceeded = _read_bounded_line(f)
    except OSError:
        return False, "", ""
    if not first_line or exceeded:
        return False, "", ""
    try:
        obj = json.loads(first_line.decode("utf-8", errors="replace"))
    except ValueError:
        return False, "", ""
    if not isinstance(obj, dict) or obj.get("type") != "session_meta":
        return False, "", ""
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return False, "", ""
    is_cli = payload.get("source") == "cli"
    sid = str(payload.get("id") or payload.get("session_id") or "").strip().lower()
    cwd = _normalize_cwd(str(payload.get("cwd") or ""), wsl=wsl)
    return is_cli, sid, cwd


def _read_claude_root_meta(
    jsonl_path: str | Path,
    *,
    allow_empty_or_meta_only: bool = False,
    wsl: bool = False,
) -> tuple[bool, str, str]:
    p = Path(jsonl_path)
    if p.name.lower().startswith("agent-"):
        return False, "", ""
    last_sid = p.stem.lower()
    last_cwd = ""
    try:
        with open(p, "rb") as f:
            checked = 0
            while checked < 30:
                raw, exceeded = _read_bounded_line(f)
                if not raw:
                    break
                if not raw.strip():
                    continue
                if exceeded:
                    if _SIDECHAIN_TRUE_RE.search(raw) or _CLAUDE_SUBAGENT_HEAD_RE.search(raw):
                        return False, "", ""
                    if _SIDECHAIN_FALSE_RE.search(raw):
                        return True, last_sid, last_cwd
                    continue
                checked += 1
                try:
                    obj = json.loads(raw.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("agentId") or obj.get("agent_id") or obj.get("entrypoint") == "sdk-cli":
                    return False, "", ""
                if isinstance(obj.get("sessionId"), str) and obj["sessionId"].strip():
                    last_sid = obj["sessionId"].strip().lower()
                if isinstance(obj.get("cwd"), str) and obj["cwd"].strip():
                    last_cwd = _normalize_cwd(obj["cwd"], wsl=wsl)
                if "isSidechain" in obj or obj.get("type") in ("user", "assistant"):
                    if bool(obj.get("isSidechain")):
                        return False, "", ""
                    return True, last_sid, last_cwd
    except OSError:
        return False, "", ""
    if allow_empty_or_meta_only:
        return True, last_sid, last_cwd
    return False, "", ""


def _resolve_agy_transcript_path(
    home: Path, cid: str, *, wsl: bool = False
) -> Path | None:
    logs = home / ".gemini" / "antigravity-cli" / "brain" / cid / ".system_generated" / "logs"
    if wsl and not _safe_realpath(str(logs), wsl=True):
        return None
    full_p = logs / "transcript_full.jsonl"
    if not wsl or _safe_realpath(str(full_p), wsl=True):
        if full_p.is_file():
            return full_p
    comp_p = logs / "transcript.jsonl"
    if not wsl or _safe_realpath(str(comp_p), wsl=True):
        if comp_p.is_file():
            return comp_p
    return None


_AGY_SUBAGENT_PREFIXES = ("[agy-run]", "[avi-refiner]")
_AGY_HEAD_USER_RE = re.compile(r'"source"\s*:\s*"USER_EXPLICIT"')
_AGY_HEAD_SYS_RE = re.compile(r'"source"\s*:\s*"SYSTEM"')
_AGY_HEAD_SYS_TYPE_RE = re.compile(r'"type"\s*:\s*"(?:SYSTEM_MESSAGE|EPHEMERAL_MESSAGE)"')
_AGY_HEAD_CONTENT_RE = re.compile(r'"content"\s*:\s*"((?:\\.|[^"\\]){0,256})')


def _is_agy_user_prefix_subagent(content: str) -> bool:
    c = content.lstrip()
    if c.startswith("<USER_REQUEST>"):
        c = c[len("<USER_REQUEST>") :].lstrip()
    return c.startswith(_AGY_SUBAGENT_PREFIXES)


def _is_agy_sys_subagent(content: str) -> bool:
    return ("[Message] timestamp=" in content and "sender=" in content) or (
        "<subagent_reminder>" in content or "invoked by a caller agent" in content
    )


def _is_agy_subagent(jsonl_path: str | Path) -> bool:
    try:
        with open(jsonl_path, "rb") as f:
            seen_non_empty = 0
            while seen_non_empty < 20:
                line, exceeded = _read_bounded_line(f)
                if not line:
                    return False
                if not line.strip():
                    continue
                seen_non_empty += 1
                if not exceeded:
                    try:
                        obj = json.loads(line.decode("utf-8", errors="replace"))
                    except ValueError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    src, typ = obj.get("source"), obj.get("type")
                    if src == "USER_EXPLICIT":
                        return _is_agy_user_prefix_subagent(str(obj.get("content") or ""))
                    if src == "SYSTEM" and typ in ("SYSTEM_MESSAGE", "EPHEMERAL_MESSAGE"):
                        if _is_agy_sys_subagent(str(obj.get("content") or "")):
                            return True
                    continue

                # Truncated oversized line (> MAX_LINE_BYTES): inspect bounded head.
                head = line[:8192].decode("utf-8", errors="replace")
                if _AGY_HEAD_USER_RE.search(head):
                    m = _AGY_HEAD_CONTENT_RE.search(head)
                    if not m:
                        return False
                    frag = m.group(1)
                    try:
                        decoded = json.loads(f'"{frag}"')
                    except ValueError:
                        decoded = frag.replace("\\n", "\n").replace('\\"', '"')
                    return _is_agy_user_prefix_subagent(str(decoded))
                if _AGY_HEAD_SYS_RE.search(head) and _AGY_HEAD_SYS_TYPE_RE.search(head):
                    if _is_agy_sys_subagent(head):
                        return True
    except OSError:
        return False
    return False


def _extract_agy_cwd(
    jsonl_path: str | Path, *, wsl: bool = False, strict_system_only: bool = False
) -> str:
    try:
        with open(jsonl_path, "rb") as f:
            for _ in range(8):
                raw, exceeded = _read_bounded_line(f)
                if not raw:
                    break
                if exceeded or not raw.strip():
                    continue
                try:
                    obj = json.loads(raw.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if isinstance(obj.get("cwd"), str) and obj["cwd"].strip():
                    return _normalize_cwd(obj["cwd"], wsl=wsl)
                if strict_system_only and obj.get("source") != "SYSTEM":
                    continue
                c = obj.get("content")
                if isinstance(c, str) and (
                    "Working Directory:" in c or "Active Workspaces:" in c
                ):
                    m = _AGY_WS_CWD_RE.search(c)
                    if m:
                        return _normalize_cwd(m.group(1), wsl=wsl)
    except OSError:
        pass
    return ""


def _is_flock_held(lock_path: Path) -> bool:
    if not lock_path.is_file():
        return False
    fd = -1
    try:
        fd = os.open(str(lock_path), os.O_RDONLY)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError as exc:
            return exc.errno in (errno.EAGAIN, errno.EACCES)
        else:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            return False
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def _load_codex_thread_names(home: Path, *, wsl: bool = False) -> dict[str, str]:
    out: dict[str, str] = {}
    idx = home / ".codex" / "session_index.jsonl"
    if wsl:
        safe_idx = _safe_realpath(str(idx), wsl=True)
        if not safe_idx:
            return out
        idx = Path(safe_idx)
    if not idx.is_file():
        return out
    try:
        with open(idx, "r", encoding="utf-8", errors="replace") as f:
            for ln in f:
                s = ln.strip()
                if not s:
                    continue
                try:
                    obj = json.loads(s)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    sid = str(obj.get("id") or "").strip().lower()
                    tn = str(obj.get("thread_name") or "").strip()
                    if sid and tn:
                        out[sid] = tn
    except OSError:
        pass
    return out


def _candidate_runtime_dirs(home: Path, env: Mapping[str, str]) -> list[Path]:
    uid = os.getuid()
    cands: list[Path] = []
    for k in ("AGENT_VOICE_INPUT_RUNTIME_DIR", "VOICE_INPUT_RUNTIME_DIR"):
        v = (env.get(k) or "").strip()
        if v:
            cands.append(Path(_expand_remote_path(v, home)))
    xdg = (env.get("XDG_RUNTIME_DIR") or "").strip()
    if xdg:
        xp = Path(_expand_remote_path(xdg, home))
        cands.extend([xp / "agent-voice-input", xp / "voice-input"])
    cands.append(Path(f"/run/user/{uid}/agent-voice-input"))
    cache_base = Path(_expand_remote_path(env.get("XDG_CACHE_HOME") or "", home) or (home / ".cache"))
    cands.extend([cache_base / "agent-voice-input", cache_base / "voice-input"])
    out: list[Path] = []
    for c in cands:
        if c not in out:
            out.append(c)
    return out


def _is_trusted_runtime_dir(rt_dir: Path) -> bool:
    try:
        st = os.lstat(rt_dir)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return False
    if (st.st_mode & 0o077) != 0 or (st.st_mode & 0o700) != 0o700:
        return False
    return st.st_uid == os.getuid()


def _read_trusted_registry_json(reg_file: Path) -> dict[str, Any] | None:
    """Verify parent ``sessions/`` & runtime dir ``0700``/owner/non-symlink, ``O_NOFOLLOW`` ``0600`` file, and loop-read up to 256 KiB."""
    if not _is_trusted_runtime_dir(reg_file.parent) or not _is_trusted_runtime_dir(
        reg_file.parent.parent
    ):
        return None
    uid = os.getuid()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(reg_file), flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != uid
            or (st.st_mode & 0o077) != 0
            or st.st_size > 262144
        ):
            return None
        chunks: list[bytes] = []
        total = 0
        while True:
            buf = os.read(fd, 16384)
            if not buf:
                break
            total += len(buf)
            if total > 262144:
                return None
            chunks.append(buf)
    except OSError:
        return None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        obj = json.loads(b"".join(chunks).decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _parse_title_hint(title: str) -> tuple[str, bool, str | None, bool]:
    """Return ``(token, is_full_uuid, cli, explicit_cli)``."""
    if not title or not title.strip():
        return "", False, None, False
    pm = list(_PREFIXED_TAG_RE.finditer(title))
    if pm:
        m = pm[-1]
        cli = m.group(1).lower()
        tok = m.group(2).lower()
        return tok, bool(_STRICT_UUID_RE.match(tok)), cli, True
    hm = list(_HASH_TAG_RE.finditer(title))
    if hm:
        tok = hm[-1].group(1).lower()
        return tok, bool(_STRICT_UUID_RE.match(tok)), _infer_cli_word(title), False
    um = list(_FULL_UUID_RE.finditer(title))
    if um:
        tok = um[-1].group(1).lower()
        return tok, True, (_infer_cli_word(title) or "codex"), False
    return "", False, None, False


def _infer_cli_word(title: str) -> str | None:
    m = _WORD_CLI_RE.search(title or "")
    if not m:
        return None
    w = m.group(1).lower()
    return "agy" if w == "antigravity" else w


def _extract_claude_sid_from_argv(args: Sequence[str]) -> str:
    for i, a in enumerate(args):
        if a == "--":
            break
        if a in ("--session-id", "--resume", "-r") and i + 1 < len(args):
            cand = args[i + 1].strip().lower()
            if _STRICT_UUID_RE.match(cand):
                return cand
        for pfx in ("--session-id=", "--resume="):
            if a.startswith(pfx):
                cand = a[len(pfx) :].strip().lower()
                if _STRICT_UUID_RE.match(cand):
                    return cand
    return ""


def _extract_claude_cmdline_sid(proc_pid_dir: Path) -> str:
    return _extract_claude_sid_from_argv(_read_proc_cmdline_args(proc_pid_dir))


def _scan_proc_candidates(
    home: Path,
    proc_root: Path,
    *,
    allowed_pids: set[int] | None = None,
    codex_names: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    if not proc_root.is_dir():
        return []
    try:
        names = (
            [str(p) for p in sorted(allowed_pids) if p > 0]
            if allowed_pids is not None
            else [n for n in os.listdir(proc_root) if n.isdigit()]
        )
    except OSError:
        return []

    cx_names = _load_codex_thread_names(home) if codex_names is None else codex_names
    codex_root = home / ".codex"
    agy_root = home / ".gemini"
    by_key: dict[str, dict[str, Any]] = {}

    for name in names:
        pdir = proc_root / name
        if not pdir.is_dir():
            continue
        cli_k = _classify_proc_cli(pdir)
        if cli_k is None:
            continue
        pts_mtime = _read_proc_tty_mtime(pdir)
        proc_cwd = _normalize_cwd(_readlink_clean(pdir / "cwd"))
        try:
            fds = [_readlink_clean(pdir / "fd" / fn) for fn in os.listdir(pdir / "fd")]
        except OSError:
            fds = []

        if cli_k == "codex":
            for t in fds:
                if (
                    t
                    and t.endswith(".jsonl")
                    and os.path.basename(t).startswith("rollout-")
                    and (safe_t := _resolve_safe_transcript_path("codex", t, home))
                ):
                    ok_c, sid, mcwd = _read_codex_root_meta(safe_t)
                    if ok_c and sid:
                        jm = _safe_mtime(safe_t)
                        cand = {
                            "cli": "codex",
                            "session_id": sid,
                            "jsonl_path": safe_t,
                            "cwd": mcwd or proc_cwd,
                            "matched_by": "remote_proc",
                            "pts_mtime": pts_mtime,
                            "jsonl_mtime": jm,
                            "thread_name": cx_names.get(sid, ""),
                        }
                        key = f"codex:{sid}"
                        prev = by_key.get(key)
                        if prev is None or (pts_mtime, jm) > (prev["pts_mtime"], prev["jsonl_mtime"]):
                            by_key[key] = cand

        elif cli_k == "agy":
            for t in fds:
                if (
                    t
                    and t.endswith(".lock")
                    and "/presence/" in t.replace("\\", "/")
                    and _is_path_under_dir(t, agy_root)
                    and os.path.isfile(t)
                ):
                    cid = Path(t).stem.lower()
                    tp = _resolve_agy_transcript_path(home, cid)
                    safe_tp = _resolve_safe_transcript_path("agy", tp, home) if tp is not None else ""
                    if tp is not None and (not safe_tp or _is_agy_subagent(safe_tp)):
                        continue
                    jm = _safe_mtime(safe_tp) if safe_tp else _safe_mtime(t)
                    cand = {
                        "cli": "agy",
                        "session_id": cid,
                        "jsonl_path": safe_tp,
                        "cwd": proc_cwd or (_extract_agy_cwd(safe_tp) if safe_tp else ""),
                        "matched_by": "remote_proc",
                        "pts_mtime": pts_mtime,
                        "jsonl_mtime": jm,
                        "thread_name": "",
                    }
                    key = f"agy:{cid}"
                    prev = by_key.get(key)
                    if prev is None or (pts_mtime, jm) > (prev["pts_mtime"], prev["jsonl_mtime"]):
                        by_key[key] = cand

        elif cli_k == "claude" and proc_cwd:
            pinned = _extract_claude_cmdline_sid(pdir)
            best_p: dict[str, Any] | None = None
            best_u: dict[str, Any] | None = None
            for enc in _encode_claude_dirs(proc_cwd):
                pproj = home / ".claude" / "projects" / enc
                if not pproj.is_dir():
                    continue
                for jf in pproj.glob("*.jsonl"):
                    safe_jf = _resolve_safe_transcript_path("claude", jf, home)
                    if not safe_jf:
                        continue
                    is_r, sid, mcwd = _read_claude_root_meta(safe_jf, allow_empty_or_meta_only=True)
                    if not is_r or not sid:
                        continue
                    jm = _safe_mtime(safe_jf)
                    cand = {
                        "cli": "claude",
                        "session_id": sid,
                        "jsonl_path": safe_jf,
                        "cwd": mcwd or proc_cwd,
                        "matched_by": "remote_proc",
                        "pts_mtime": pts_mtime,
                        "jsonl_mtime": jm,
                        "thread_name": "",
                    }
                    if pinned and sid == pinned:
                        if best_p is None or jm > best_p["jsonl_mtime"]:
                            best_p = cand
                    if best_u is None or jm > best_u["jsonl_mtime"]:
                        best_u = cand
            chosen = best_p or best_u
            if chosen is not None:
                key = f"claude:{chosen['session_id']}"
                prev = by_key.get(key)
                if prev is None or (chosen["pts_mtime"], chosen["jsonl_mtime"]) > (
                    prev["pts_mtime"],
                    prev["jsonl_mtime"],
                ):
                    by_key[key] = chosen

    return list(by_key.values())


def _collect_descendants(proc_root: Path, root_pid: int) -> set[int]:
    if root_pid <= 1 or not proc_root.is_dir():
        return set()
    children: dict[int, list[int]] = {}
    existing: set[int] = set()
    try:
        for n in os.listdir(proc_root):
            if n.isdigit():
                pid = int(n)
                existing.add(pid)
                ppid = _read_proc_ppid(proc_root / n)
                if ppid > 1:
                    children.setdefault(ppid, []).append(pid)
    except OSError:
        return set()
    visited: set[int] = set()
    stack = [root_pid] if root_pid in existing else []
    while stack:
        cur = stack.pop()
        if cur in visited:
            continue
        visited.add(cur)
        for ch in children.get(cur, ()):
            if ch not in visited:
                stack.append(ch)
    return visited


def _resolve_remote_tool_bin(name: str, env: Mapping[str, str] | None = None) -> str:
    src = os.environ if env is None else env
    raw_path = src.get("PATH")
    found = shutil.which(name, path=raw_path) if raw_path is not None else shutil.which(name)
    if found:
        return found
    raw_home = (src.get("HOME") or os.environ.get("HOME") or "").strip()
    local_bin = os.path.join(os.path.expanduser(raw_home), ".local", "bin", name) if raw_home else ""
    for cand in (
        f"/usr/bin/{name}",
        f"/usr/local/bin/{name}",
        f"/opt/homebrew/bin/{name}",
        f"/opt/local/bin/{name}",
        f"/home/linuxbrew/.linuxbrew/bin/{name}",
        local_bin,
    ):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return name


def _probe_remote_tmux_active_pane(env: Mapping[str, str]) -> dict[str, Any] | None:
    if env.get("AVI_DISABLE_TMUX_PROBE") == "1":
        return None
    if env.get("AVI_REMOTE_PROC_ROOT") and not env.get("AVI_REMOTE_TMUX_SOCK"):
        return None
    sock_arg = (env.get("AVI_REMOTE_TMUX_SOCK") or "").strip()
    tmux_bin = _resolve_remote_tool_bin("tmux", env)
    tmux_cmd = [tmux_bin, "-S", sock_arg] if sock_arg else [tmux_bin]
    sep = "\x1f"
    try:
        rc_c = subprocess.run(
            [
                *tmux_cmd,
                "list-clients",
                "-F",
                f"#{{session_id}}{sep}#{{window_id}}{sep}#{{pane_id}}{sep}#{{client_activity}}{sep}#{{client_flags}}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=0.5,
            check=False,
        )
        if rc_c.returncode != 0 or not rc_c.stdout.strip():
            return None
        clients: list[tuple[float, int, str, str, str]] = []
        for ln in rc_c.stdout.decode("utf-8", errors="replace").splitlines():
            parts = ln.split(sep)
            if len(parts) < 5:
                continue
            flags = set(parts[4].split(","))
            if "control-mode" in flags or ("attached" not in flags and flags != {""}):
                continue
            try:
                act = float(parts[3] or 0.0)
            except ValueError:
                act = 0.0
            clients.append((act, 1 if "focused" in flags else 0, parts[0], parts[1], parts[2]))
        if not clients:
            return None
        clients.sort(reverse=True)
        _act, _foc, c_sid, c_wid, c_pid_tok = clients[0]

        rc_p = subprocess.run(
            [
                *tmux_cmd,
                "list-panes",
                "-a",
                "-F",
                f"#{{session_id}}{sep}#{{window_id}}{sep}#{{window_active}}{sep}#{{pane_id}}{sep}#{{pane_active}}{sep}#{{pane_pid}}{sep}#{{pane_current_path}}{sep}#{{pane_title}}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=0.5,
            check=False,
        )
        if rc_p.returncode != 0:
            return None
        for ln in rc_p.stdout.decode("utf-8", errors="replace").splitlines():
            p = ln.split(sep, 7)
            if len(p) < 8:
                continue
            psid, pwid, wact, pane_id, pact, ppid_s, pcwd, ptitle = p
            if c_pid_tok and pane_id == c_pid_tok and (not c_sid or psid == c_sid):
                return {
                    "pane_id": pane_id,
                    "pane_pid": int(ppid_s) if ppid_s.isdigit() else 0,
                    "cwd": _normalize_cwd(pcwd),
                    "pane_title": ptitle.strip(),
                }
            if (
                not c_pid_tok
                and psid == c_sid
                and (pwid == c_wid if c_wid else wact == "1")
                and pact == "1"
            ):
                return {
                    "pane_id": pane_id,
                    "pane_pid": int(ppid_s) if ppid_s.isdigit() else 0,
                    "cwd": _normalize_cwd(pcwd),
                    "pane_title": ptitle.strip(),
                }
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def _ordered_clis(preferred: str | None) -> tuple[str, ...]:
    all_clis = ("codex", "claude", "agy")
    if preferred in all_clis:
        return (preferred, *[c for c in all_clis if c != preferred])
    return all_clis


def _lookup_by_token(
    token: str,
    cli_hint: str | None,
    explicit_cli: bool,
    home: Path,
    proc_root: Path,
    rt_dirs: Sequence[Path],
    codex_names: Mapping[str, str],
) -> dict[str, Any] | None:
    tok = token.lower()
    clis = (cli_hint,) if (explicit_cli and cli_hint) else _ordered_clis(cli_hint)
    has_proc = proc_root.is_dir()

    # 1. Trusted 0600 runtime session registry
    p8 = tok[:8]
    reg_hits: list[tuple[int, int, float, dict[str, Any]]] = []
    for d_idx, rt in enumerate(rt_dirs):
        sdir = rt / "sessions"
        for c_rank, c in enumerate(clis):
            if not c:
                continue
            reg_files = [sdir / f"{c}-{p8}.json"] if len(tok) >= 8 else sorted(sdir.glob(f"{c}-{tok}*.json"))
            for rf in reg_files:
                rec = _read_trusted_registry_json(rf)
                if rec is None:
                    continue
                sid = str(rec.get("session_id") or "").strip().lower()
                if not sid or not sid.startswith(tok):
                    continue
                rpid = rec.get("agent_pid") or rec.get("pid") or 0
                if isinstance(rpid, int) and not isinstance(rpid, bool) and rpid > 0 and has_proc:
                    if _classify_proc_cli(proc_root / str(rpid)) != c:
                        continue
                raw_p = str(rec.get("jsonl_path") or rec.get("transcript_path") or "").strip()
                rcwd = _normalize_cwd(str(rec.get("cwd") or ""))
                if c == "agy":
                    tp = _resolve_agy_transcript_path(home, sid)
                    if tp is not None:
                        safe_p = _resolve_safe_transcript_path("agy", tp, home)
                        if not safe_p or _is_agy_subagent(safe_p):
                            continue
                        raw_p = safe_p
                    elif raw_p:
                        safe_p = _resolve_safe_transcript_path("agy", raw_p, home)
                        if not safe_p or _is_agy_subagent(safe_p):
                            continue
                        raw_p = safe_p
                    else:
                        continue
                elif c == "codex":
                    safe_p = _resolve_safe_transcript_path("codex", raw_p, home)
                    if not safe_p:
                        continue
                    raw_p = safe_p
                    if os.path.getsize(raw_p) > 0:
                        ok_c, msid, mcwd = _read_codex_root_meta(raw_p)
                        if not ok_c or (msid and msid != sid):
                            continue
                        rcwd = rcwd or mcwd
                elif c == "claude":
                    safe_p = _resolve_safe_transcript_path("claude", raw_p, home)
                    if not safe_p:
                        continue
                    raw_p = safe_p
                    if os.path.getsize(raw_p) > 0:
                        ok_cl, msid, mcwd = _read_claude_root_meta(raw_p, allow_empty_or_meta_only=True)
                        if not ok_cl or (msid and msid != sid):
                            continue
                        rcwd = rcwd or mcwd
                try:
                    upd = float(rec.get("updated_at") or 0.0)
                except (TypeError, ValueError):
                    upd = 0.0
                reg_hits.append(
                    (
                        -d_idx,
                        -c_rank,
                        upd,
                        {
                            "cli": c,
                            "session_id": sid,
                            "jsonl_path": raw_p,
                            "cwd": rcwd,
                            "matched_by": "remote_registry",
                            "pts_mtime": upd,
                            "jsonl_mtime": _safe_mtime(raw_p),
                            "thread_name": codex_names.get(sid, "") if c == "codex" else "",
                        },
                    )
                )
    if reg_hits:
        reg_hits.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
        return reg_hits[0][3]

    # 2. Live /proc scan matching prefix/UUID
    proc_cands = _scan_proc_candidates(home, proc_root, codex_names=codex_names)
    proc_hits = [c for c in proc_cands if c["session_id"].startswith(tok) and c["cli"] in clis]
    if proc_hits:
        proc_hits.sort(
            key=lambda c: (
                1 if c["cli"] == cli_hint else 0,
                max(c["pts_mtime"], c["jsonl_mtime"]),
                c["jsonl_mtime"],
            ),
            reverse=True,
        )
        hit = dict(proc_hits[0])
        hit["matched_by"] = "remote_title_token"
        return hit

    # 3. Direct storage lookup by UUID / prefix
    dir_hits: list[dict[str, Any]] = []
    for c in clis:
        if c == "codex":
            pat = str(home / ".codex" / "sessions" / "*" / "*" / "*" / f"rollout-*-{tok}*.jsonl")
            for cf in sorted(glob.glob(pat), key=_safe_mtime, reverse=True)[:16]:
                safe_cf = _resolve_safe_transcript_path("codex", cf, home)
                if not safe_cf:
                    continue
                ok_c, sid, mcwd = _read_codex_root_meta(safe_cf)
                if ok_c and sid and sid.startswith(tok):
                    dir_hits.append(
                        {
                            "cli": "codex",
                            "session_id": sid,
                            "jsonl_path": safe_cf,
                            "cwd": mcwd,
                            "matched_by": "remote_title_token",
                            "pts_mtime": 0.0,
                            "jsonl_mtime": _safe_mtime(safe_cf),
                            "thread_name": codex_names.get(sid, ""),
                        }
                    )
        elif c == "claude":
            pat = str(home / ".claude" / "projects" / "*" / f"{tok}*.jsonl")
            for clf in sorted(glob.glob(pat), key=_safe_mtime, reverse=True)[:16]:
                safe_clf = _resolve_safe_transcript_path("claude", clf, home)
                if not safe_clf:
                    continue
                ok_cl, sid, mcwd = _read_claude_root_meta(safe_clf, allow_empty_or_meta_only=True)
                if ok_cl and sid and sid.startswith(tok):
                    dir_hits.append(
                        {
                            "cli": "claude",
                            "session_id": sid,
                            "jsonl_path": safe_clf,
                            "cwd": mcwd,
                            "matched_by": "remote_title_token",
                            "pts_mtime": 0.0,
                            "jsonl_mtime": _safe_mtime(safe_clf),
                            "thread_name": "",
                        }
                    )
        elif c == "agy":
            pdir = home / ".gemini" / "antigravity-cli" / "presence"
            if pdir.is_dir():
                for lf in pdir.glob(f"{tok}*.lock"):
                    cid = lf.stem.lower()
                    if not cid.startswith(tok):
                        continue
                    if not _is_path_under_dir(lf, pdir):
                        continue
                    if not has_proc and not _is_flock_held(lf):
                        continue
                    if has_proc:
                        # On /proc hosts without live agy match above, reject stale unlocked lock
                        if not _is_flock_held(lf):
                            continue
                    tp = _resolve_agy_transcript_path(home, cid)
                    safe_tp = _resolve_safe_transcript_path("agy", tp, home) if tp is not None else ""
                    if safe_tp and not _is_agy_subagent(safe_tp):
                        dir_hits.append(
                            {
                                "cli": "agy",
                                "session_id": cid,
                                "jsonl_path": safe_tp,
                                "cwd": _extract_agy_cwd(safe_tp),
                                "matched_by": "remote_title_token",
                                "pts_mtime": 0.0,
                                "jsonl_mtime": _safe_mtime(safe_tp),
                                "thread_name": "",
                            }
                        )
    if not dir_hits:
        return None
    dir_hits.sort(key=lambda c: (1 if c["cli"] == cli_hint else 0, c["jsonl_mtime"]), reverse=True)
    return dir_hits[0]


def _iter_codex_rollout_files_desc(
    home: Path,
    *,
    max_files: int = COLD_START_CODEX_SCAN_LIMIT,
    wsl: bool = False,
) -> list[str]:
    """Walk ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`` in reverse date order with a hard file cap."""
    root = home / ".codex" / "sessions"
    if wsl:
        real_root = _safe_realpath(str(root), wsl=True)
        if not real_root or not os.path.isdir(real_root):
            return []
        root = Path(real_root)
    elif not root.is_dir():
        return []

    def _subdirs_desc(parent: Path) -> list[Path]:
        items: list[tuple[str, Path]] = []
        try:
            with os.scandir(parent) as it:
                for entry in it:
                    if entry.name.startswith("."):
                        continue
                    if wsl and not _safe_realpath(entry.path, wsl=True):
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=True):
                            items.append((entry.name, Path(entry.path)))
                    except OSError:
                        continue
        except OSError:
            return []
        items.sort(key=lambda pair: pair[0], reverse=True)
        return [p for _, p in items]

    collected: list[tuple[float, str]] = []
    for y_dir in _subdirs_desc(root):
        for m_dir in _subdirs_desc(y_dir):
            for d_dir in _subdirs_desc(m_dir):
                day_files: list[tuple[float, str]] = []
                try:
                    with os.scandir(d_dir) as it:
                        for entry in it:
                            name = entry.name
                            if not (name.startswith("rollout-") and name.endswith(".jsonl")):
                                continue
                            safe_p = _resolve_safe_transcript_path("codex", entry.path, home, wsl=wsl)
                            if safe_p:
                                day_files.append((_safe_mtime(safe_p), safe_p))
                except OSError:
                    continue
                day_files.sort(key=lambda item: item[0], reverse=True)
                for item in day_files:
                    collected.append(item)
                    if max_files > 0 and len(collected) >= max_files:
                        collected.sort(key=lambda x: x[0], reverse=True)
                        return [p for _, p in collected]
    collected.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in collected]


def _scan_storage_for_cwd(
    target_cwd: str,
    home: Path,
    codex_names: Mapping[str, str],
    *,
    exact_only: bool = False,
    codex_limit: int = 32,
    agy_limit: int = 24,
    include_claude_global: bool = True,
    claude_proj_limit: int = COLD_START_CLAUDE_PROJ_SCAN_LIMIT,
    wsl: bool = False,
) -> list[dict[str, Any]]:
    """Tier 2 zero-hook storage scan strictly scoped to ``target_cwd`` (never cross-directory)."""
    if _is_broad_or_unsafe_workspace_dir(target_cwd, home, wsl=wsl):
        return []
    min_rank = 2 if exact_only else 1
    cands: list[dict[str, Any]] = []

    def _list_claude_dir_jsonl_desc(pdir: Path) -> list[tuple[float, str]]:
        files: list[tuple[float, str]] = []
        try:
            with os.scandir(pdir) as fit:
                for fentry in fit:
                    fname = fentry.name
                    if not fname.endswith(".jsonl") or fname.lower().startswith("agent-"):
                        continue
                    try:
                        fst = fentry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISLNK(fst.st_mode):
                        if wsl and not _safe_realpath(fentry.path, wsl=True):
                            continue
                        fm = _safe_mtime(fentry.path)
                    elif stat.S_ISREG(fst.st_mode):
                        fm = fst.st_mtime
                    else:
                        continue
                    files.append((fm, fentry.path))
        except OSError:
            return []
        files.sort(key=lambda item: item[0], reverse=True)
        return files

    def _append_claude_file(
        raw_fp: str, *, allow_fallback_cwd: bool, seen_cl: set[str]
    ) -> tuple[bool, bool]:
        """Validate and append one Claude JSONL; returns ``(had_valid_meta_cwd, matched)``."""
        safe_jf = _resolve_safe_transcript_path("claude", raw_fp, home, wsl=wsl)
        if not safe_jf or safe_jf in seen_cl:
            return False, False
        seen_cl.add(safe_jf)
        ok_cl, sid, mcwd = _read_claude_root_meta(
            safe_jf, allow_empty_or_meta_only=False, wsl=wsl
        )
        if not ok_cl or not sid:
            return False, False
        if exact_only or not allow_fallback_cwd:
            if not mcwd:
                return False, False
            eff_mcwd = mcwd
        else:
            eff_mcwd = mcwd or target_cwd
        if _cwd_match_rank(target_cwd, eff_mcwd, home, wsl=wsl) >= min_rank:
            cands.append(
                {
                    "cli": "claude",
                    "session_id": sid,
                    "jsonl_path": safe_jf,
                    "cwd": eff_mcwd,
                    "matched_by": "remote_cwd",
                    "pts_mtime": 0.0,
                    "jsonl_mtime": _safe_mtime(safe_jf),
                    "thread_name": "",
                }
            )
            return bool(mcwd), True
        return bool(mcwd), False

    # 1. Claude projects matching encoded target_cwd (exact + symlink-alias / optional global check)
    seen_cl: set[str] = set()
    seen_pdirs: set[str] = set()
    projects_root = home / ".claude" / "projects"
    real_proot_str = _safe_realpath(str(projects_root), wsl=wsl)
    projects_root_ok = bool(real_proot_str and os.path.isdir(real_proot_str))
    eff_projects_root = Path(real_proot_str) if projects_root_ok else projects_root

    if projects_root_ok:
        for enc in _encode_claude_dirs(target_cwd, wsl=wsl):
            pdir = eff_projects_root / enc
            real_pdir = _safe_realpath(str(pdir), wsl=wsl)
            if (
                not real_pdir
                or real_pdir in seen_pdirs
                or not os.path.isdir(real_pdir)
                or not _is_path_under_dir(real_pdir, eff_projects_root, wsl=wsl)
            ):
                continue
            seen_pdirs.add(real_pdir)
            for _, raw_fp in _list_claude_dir_jsonl_desc(Path(real_pdir))[:24]:
                _append_claude_file(raw_fp, allow_fallback_cwd=True, seen_cl=seen_cl)

    if projects_root_ok and (
        (include_claude_global and not cands)
        or (exact_only and len(cands) < COLD_START_TOP_K + 1)
    ):
        other_pdirs: list[tuple[float, Path]] = []
        try:
            with os.scandir(eff_projects_root) as it:
                for entry in it:
                    if entry.name.startswith("."):
                        continue
                    real_d = _safe_realpath(entry.path, wsl=wsl)
                    if (
                        not real_d
                        or real_d in seen_pdirs
                        or not os.path.isdir(real_d)
                        or not _is_path_under_dir(real_d, eff_projects_root, wsl=wsl)
                    ):
                        continue
                    seen_pdirs.add(real_d)
                    other_pdirs.append((_safe_mtime(real_d), Path(real_d)))
        except OSError:
            other_pdirs = []
        other_pdirs.sort(key=lambda item: item[0], reverse=True)
        for _, pdir_cand in other_pdirs[:claude_proj_limit]:
            dir_files = _list_claude_dir_jsonl_desc(pdir_cand)
            if not dir_files:
                continue
            dir_matched = False
            for _, raw_fp in dir_files[:8]:
                had_mcwd, matched = _append_claude_file(
                    raw_fp, allow_fallback_cwd=False, seen_cl=seen_cl
                )
                if matched:
                    dir_matched = True
                    break
                if had_mcwd:
                    break
            if dir_matched:
                for _, raw_fp in dir_files[:24]:
                    _append_claude_file(raw_fp, allow_fallback_cwd=False, seen_cl=seen_cl)

    # 2. Codex rollouts matching target_cwd (reverse YYYY/MM/DD bounded walk)
    for safe_cf in _iter_codex_rollout_files_desc(home, max_files=codex_limit, wsl=wsl):
        ok_c, sid, mcwd = _read_codex_root_meta(safe_cf, wsl=wsl)
        if ok_c and sid and mcwd and _cwd_match_rank(target_cwd, mcwd, home, wsl=wsl) >= min_rank:
            cands.append(
                {
                    "cli": "codex",
                    "session_id": sid,
                    "jsonl_path": safe_cf,
                    "cwd": mcwd,
                    "matched_by": "remote_cwd",
                    "pts_mtime": 0.0,
                    "jsonl_mtime": _safe_mtime(safe_cf),
                    "thread_name": codex_names.get(sid, ""),
                }
            )

    # 3. Antigravity CLI brain transcripts matching target_cwd (no live presence lock required)
    broot = home / ".gemini" / "antigravity-cli" / "brain"
    broot_ok = (
        bool(_safe_realpath(str(broot), wsl=True)) and broot.is_dir()
        if wsl
        else broot.is_dir()
    )
    if broot_ok:
        agy_items: list[tuple[float, str, str]] = []
        try:
            with os.scandir(broot) as it:
                for entry in it:
                    if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
                        continue
                    tp = _resolve_agy_transcript_path(home, entry.name, wsl=wsl)
                    safe_tp = (
                        _resolve_safe_transcript_path("agy", tp, home, wsl=wsl)
                        if tp is not None
                        else ""
                    )
                    if safe_tp:
                        agy_items.append((_safe_mtime(safe_tp), entry.name.lower(), safe_tp))
        except OSError:
            pass
        agy_items.sort(key=lambda x: x[0], reverse=True)
        for jm, cid, safe_tp in agy_items[:agy_limit]:
            if _is_agy_subagent(safe_tp):
                continue
            acwd = _extract_agy_cwd(safe_tp, wsl=wsl, strict_system_only=exact_only)
            if acwd and _cwd_match_rank(target_cwd, acwd, home, wsl=wsl) >= min_rank:
                cands.append(
                    {
                        "cli": "agy",
                        "session_id": cid,
                        "jsonl_path": safe_tp,
                        "cwd": acwd,
                        "matched_by": "remote_cwd",
                        "pts_mtime": 0.0,
                        "jsonl_mtime": jm,
                        "thread_name": "",
                    }
                )
    return cands


def select_cold_start_prior_candidate(
    candidates: Sequence[Mapping[str, Any]],
    *,
    preferred_cli: str = "",
    exclude_session_id: str = "",
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Select Layer 2 prior session and Top-5 substantial pool from exact-cwd historical candidates."""
    excl = (exclude_session_id or "").strip().lower()
    pref = (preferred_cli or "").strip().lower()
    pool: list[dict[str, Any]] = []
    for item in candidates:
        sid = str(item.get("session_id") or "").strip().lower()
        if excl and sid == excl:
            continue
        raw_turns = item.get("tail_turns") if "tail_turns" in item else (item.get("turns") or ())
        turn_cnt = int(item["turn_count"]) if "turn_count" in item else len(raw_turns)
        total_chars = int(item.get("total_chars") or 0)
        if turn_cnt <= 0 or not raw_turns or total_chars < COLD_START_FALLBACK_MIN_CHARS:
            continue
        pool.append(dict(item))
    pool.sort(key=lambda c: float(c.get("jsonl_mtime") or 0.0), reverse=True)
    top5 = pool[:COLD_START_TOP_K]
    if not top5:
        return None, []
    sweet = [
        c
        for c in top5
        if (
            int(c["turn_count"])
            if "turn_count" in c
            else len(c.get("tail_turns") if "tail_turns" in c else (c.get("turns") or ()))
        )
        >= COLD_START_SWEET_MIN_TURNS
        and int(c.get("total_chars") or 0) >= COLD_START_SWEET_MIN_CHARS
    ]
    if sweet:
        sweet.sort(
            key=lambda c: (
                1 if (pref and str(c.get("cli") or "").strip().lower() == pref) else 0,
                float(c.get("jsonl_mtime") or 0.0),
            ),
            reverse=True,
        )
        return sweet[0], top5
    fallback = sorted(
        top5,
        key=lambda c: (
            int(c.get("total_chars") or 0),
            1 if (pref and str(c.get("cli") or "").strip().lower() == pref) else 0,
            float(c.get("jsonl_mtime") or 0.0),
        ),
        reverse=True,
    )
    return fallback[0], top5


def discover_cli_prior_sessions(
    target_cwd: str,
    *,
    home: Path,
    wsl: bool = False,
    max_keep: int = COLD_START_TOP_K + 2,
) -> list[dict[str, Any]]:
    """Discover substantial exact-cwd historical CLI sessions (Claude / Codex / agy) without age/branch limits."""
    if _is_broad_or_unsafe_workspace_dir(target_cwd, home, wsl=wsl):
        return []
    raw_cands = _scan_storage_for_cwd(
        target_cwd,
        home,
        {},
        exact_only=True,
        codex_limit=COLD_START_CODEX_SCAN_LIMIT,
        agy_limit=COLD_START_AGY_SCAN_LIMIT,
        include_claude_global=False,
        wsl=wsl,
    )
    if not raw_cands:
        return []
    by_id: dict[tuple[str, str], dict[str, Any]] = {}
    for c in raw_cands:
        key = (str(c.get("cli") or "").lower(), str(c.get("session_id") or "").lower())
        prev = by_id.get(key)
        if prev is None or float(c.get("jsonl_mtime") or 0.0) > float(prev.get("jsonl_mtime") or 0.0):
            by_id[key] = c
    ordered = sorted(by_id.values(), key=lambda c: float(c.get("jsonl_mtime") or 0.0), reverse=True)
    parsed: list[dict[str, Any]] = []
    max_inspect = max(20, max_keep * 3)
    for c in ordered[:max_inspect]:
        cli = str(c.get("cli") or "")
        jpath = str(c.get("jsonl_path") or "")
        if not cli or not jpath:
            continue
        turns, disc_cwd, _ = _clean_jsonl_file(cli, jpath, home=home, wsl=wsl)
        eff_cand_cwd = disc_cwd or str(c.get("cwd") or "")
        if not eff_cand_cwd or _cwd_match_rank(target_cwd, eff_cand_cwd, home, wsl=wsl) < 2:
            continue
        slim = build_slim_prior_candidate(
            cli=cli,
            session_id=str(c.get("session_id") or ""),
            jsonl_path=jpath,
            cwd=eff_cand_cwd,
            jsonl_mtime=float(c.get("jsonl_mtime") or 0.0),
            turns=turns,
        )
        if slim is None:
            continue
        parsed.append(slim)
        if len(parsed) >= max_keep:
            break
    return parsed


def _pick_best_for_same_cwd(
    cands: Sequence[dict[str, Any]], target_cwd: str, title: str, home: Path
) -> dict[str, Any] | None:
    if not cands:
        return None
    cli_hint = _infer_cli_word(title)
    ranked: list[tuple[int, int, float, float, dict[str, Any]]] = []
    for c in cands:
        crank = _cwd_match_rank(target_cwd, c["cwd"], home)
        if crank <= 0:
            continue
        cli_score = 1 if (cli_hint and c["cli"] == cli_hint) else 0
        ranked.append((crank, cli_score, float(c["jsonl_mtime"]), float(c["pts_mtime"]), c))
    if not ranked:
        return None
    # Within the confirmed workspace directory, prefer exact dir -> CLI hint -> latest mtime!
    ranked.sort(key=lambda x: (x[0], x[1], x[2], x[3]), reverse=True)
    return ranked[0][4]


def _disambiguate_unscoped_cands(
    cands: Sequence[dict[str, Any]], title: str
) -> tuple[dict[str, Any] | None, str]:
    if not cands:
        return None, "not_found"
    if len(cands) == 1:
        return cands[0], ""
    pool = list(cands)
    title_low = (title or "").lower()
    if title_low:
        cli_hint = _infer_cli_word(title)
        scored: list[tuple[int, dict[str, Any]]] = []
        for c in pool:
            sc = 0
            if cli_hint and c["cli"] == cli_hint:
                sc += 2
            if c["cwd"]:
                base = os.path.basename(c["cwd"].rstrip("/")).lower()
                if len(base) >= 2 and base in title_low:
                    sc += 2
            if c.get("thread_name") and len(c["thread_name"]) >= 2 and c["thread_name"].lower() in title_low:
                sc += 2
            scored.append((sc, c))
        max_sc = max(s for s, _ in scored)
        if max_sc > 0:
            pool = [c for s, c in scored if s == max_sc]
            if len(pool) == 1:
                return pool[0], ""
    # If remaining candidates all share the exact same normalized cwd, pick latest mtime!
    distinct_cwds = {_normalize_cwd(c["cwd"]) for c in pool if c["cwd"]}
    if len(distinct_cwds) == 1 and all(c["cwd"] for c in pool):
        pool.sort(key=lambda c: (c["jsonl_mtime"], c["pts_mtime"]), reverse=True)
        return pool[0], ""
    return None, "ambiguous"


def _resolve_remote_session(
    req: Mapping[str, Any], home: Path, proc_root: Path, env: Mapping[str, str]
) -> tuple[dict[str, Any] | None, str]:
    raw_tok = str(req.get("token") or "").strip().lower()
    raw_cli = str(req.get("cli") or "").strip().lower()
    if raw_cli in ("gemini", "antigravity"):
        raw_cli = "agy"
    explicit_cli = bool(req.get("explicit_cli"))
    title = str(req.get("title") or "").strip()[:512]
    cwd_hint_raw = str(req.get("cwd_hint") or "").strip()[:512]

    if not raw_tok and title:
        t_tok, _, t_cli, t_exp = _parse_title_hint(title)
        if t_tok:
            raw_tok = t_tok
            raw_cli = raw_cli or (t_cli or "")
            explicit_cli = explicit_cli or t_exp

    rt_dirs = _candidate_runtime_dirs(home, env)
    codex_names = _load_codex_thread_names(home)

    # Tier 1: Explicit [#id8] or full UUID
    if raw_tok:
        if not _TOKEN_RE.match(raw_tok):
            return None, "invalid_token"
        hit = _lookup_by_token(
            raw_tok,
            raw_cli or None,
            explicit_cli,
            home,
            proc_root,
            rt_dirs,
            codex_names,
        )
        return (hit, "") if hit is not None else (None, "not_found")

    target_cwd = _expand_remote_path(cwd_hint_raw, home) if cwd_hint_raw else ""
    if target_cwd and not target_cwd.startswith("/"):
        target_cwd = ""

    # Block broad privacy roots immediately when explicitly hinted in prompt (e.g. user@host: ~)
    if target_cwd and _is_broad_or_unsafe_workspace_dir(target_cwd, home):
        return None, "broad_cwd"

    # Check active attached remote tmux pane
    tmux_pane = _probe_remote_tmux_active_pane(env)
    if tmux_pane is not None:
        pane_cwd = tmux_pane["cwd"]
        if not target_cwd or not pane_cwd or _cwd_match_rank(target_cwd, pane_cwd, home) > 0:
            p_tok, _, p_cli, p_exp = _parse_title_hint(tmux_pane["pane_title"])
            if p_tok and _TOKEN_RE.match(p_tok):
                t_hit = _lookup_by_token(
                    p_tok, p_cli, p_exp, home, proc_root, rt_dirs, codex_names
                )
                if t_hit is not None:
                    t_hit = dict(t_hit)
                    t_hit["matched_by"] = "remote_tmux_title"
                    t_hit["cwd"] = t_hit["cwd"] or pane_cwd
                    return t_hit, ""
            pane_pids = _collect_descendants(proc_root, tmux_pane["pane_pid"])
            if pane_pids:
                pane_cands = _scan_proc_candidates(
                    home, proc_root, allowed_pids=pane_pids, codex_names=codex_names
                )
                if pane_cands:
                    best_p, err_p = _disambiguate_unscoped_cands(
                        pane_cands, tmux_pane["pane_title"] or title
                    )
                    if best_p is not None:
                        best_p = dict(best_p)
                        best_p["matched_by"] = "remote_tmux_proc"
                        best_p["cwd"] = best_p["cwd"] or pane_cwd
                        return best_p, ""
                    if err_p:
                        return None, err_p

    # Tier 2a: Explicit safe remote cwd_hint -> live /proc in cwd -> storage in cwd -> workspace-only
    if target_cwd:
        proc_cands = _scan_proc_candidates(home, proc_root, codex_names=codex_names)
        best_proc = _pick_best_for_same_cwd(proc_cands, target_cwd, title, home)
        if best_proc is not None:
            return best_proc, ""
        stor_cands = _scan_storage_for_cwd(target_cwd, home, codex_names)
        best_stor = _pick_best_for_same_cwd(stor_cands, target_cwd, title, home)
        if best_stor is not None:
            return best_stor, ""
        try:
            real_tcwd = _normalize_cwd(os.path.realpath(target_cwd))
        except OSError:
            real_tcwd = ""
        if (
            real_tcwd
            and os.path.isdir(real_tcwd)
            and not _is_broad_or_unsafe_workspace_dir(real_tcwd, home)
        ):
            ws_id = "cwd-" + hashlib.sha1(real_tcwd.encode("utf-8")).hexdigest()[:12]
            return (
                {
                    "cli": "workspace",
                    "session_id": ws_id,
                    "jsonl_path": "",
                    "cwd": real_tcwd,
                    "matched_by": "remote_cwd_only",
                    "pts_mtime": 0.0,
                    "jsonl_mtime": 0.0,
                    "thread_name": "",
                },
                "",
            )
        return None, "not_found"

    # Tier 2b: No cwd_hint -> live /proc interactive root sessions
    proc_cands = _scan_proc_candidates(home, proc_root, codex_names=codex_names)
    return _disambiguate_unscoped_cands(proc_cands, title)


# ---------------------------------------------------------------------------
# Fast byte-prefiltered stateless JSONL cleaner
# ---------------------------------------------------------------------------


def _extract_agy_compaction_summary(obj: Mapping[str, Any]) -> str | None:
    """Return cleaned summary string when ``obj`` is an ``agy`` compaction ``CHECKPOINT`` record."""
    if obj.get("source") != "SYSTEM" or obj.get("type") != "CHECKPOINT":
        return None
    raw = obj.get("content")
    if not isinstance(raw, str):
        return ""
    cleaned = _AGY_CHECKPOINT_PREAMBLE_RE.sub("", raw)
    return _AGY_CHECKPOINT_TAIL_RE.sub("", cleaned).strip()


def _clean_agy_record(obj: Mapping[str, Any]) -> tuple[str, str] | None:
    src = obj.get("source")
    typ = obj.get("type")
    if src == "USER_EXPLICIT" and typ == "USER_INPUT":
        raw = obj.get("content")
        if not isinstance(raw, str) or not raw.strip():
            return None
        trimmed = _AGY_TRAILING_META_RE.sub("", raw).strip()
        open_tag = "<USER_REQUEST>"
        close_tag = "</USER_REQUEST>"
        start = trimmed.find(open_tag)
        end = trimmed.rfind(close_tag)
        text = trimmed[start + len(open_tag) : end].strip() if 0 <= start < end else trimmed
        return ("user", text) if text else None

    if src == "MODEL" and typ == "PLANNER_RESPONSE":
        raw = obj.get("content")
        if not isinstance(raw, str) or not raw.strip():
            return None
        return ("assistant", raw.strip())

    return None


def _clean_claude_text(raw: str, *, role: str) -> str:
    s = raw
    if role == "user":
        for pat in _CLAUDE_USER_STRIP_PATTERNS:
            s = pat.sub("", s)
        s = _CLAUDE_USER_ORPHAN_TAG_RE.sub("", s)
    else:
        s = _CLAUDE_SYSTEM_REMINDER_RE.sub("", s)
    return s.strip()


def _is_claude_sidechain_or_subagent(obj: Mapping[str, Any]) -> bool:
    return bool(
        obj.get("isSidechain")
        or obj.get("agentId")
        or obj.get("agent_id")
        or obj.get("entrypoint") == "sdk-cli"
    )


def _extract_claude_message_text(msg: Any, *, role: str) -> str:
    if not isinstance(msg, Mapping):
        return ""
    content = msg.get("content")
    parts: list[str] = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                t = blk.get("text")
                if isinstance(t, str) and t:
                    parts.append(t)
    return _clean_claude_text("\n".join(parts), role=role) if parts else ""


_extract_claude_msg_text = _extract_claude_message_text


def _clean_claude_record(obj: Mapping[str, Any]) -> tuple[str, str] | None:
    if (
        _is_claude_sidechain_or_subagent(obj)
        or bool(obj.get("isMeta"))
        or bool(obj.get("isApiErrorMessage"))
    ):
        return None
    typ = obj.get("type")
    if typ not in ("user", "assistant"):
        return None
    cleaned = _extract_claude_message_text(obj.get("message"), role=typ)
    return (typ, cleaned) if cleaned else None


def _extract_codex_item_completed(payload: Mapping[str, Any]) -> tuple[str, str] | None:
    item = payload.get("item")
    if not isinstance(item, dict):
        return None
    itype = item.get("type")
    if itype == "UserMessage":
        role = "user"
    elif itype == "AgentMessage":
        role = "assistant"
    else:
        return None

    chunks: list[str] = []
    content = item.get("content")
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and str(c.get("type") or "").lower() == "text":
                t = c.get("text")
                if isinstance(t, str) and t:
                    chunks.append(t)
    elif isinstance(item.get("text"), str):
        chunks.append(item["text"])

    joined = "\n".join(chunks).strip()
    return (role, joined) if joined else None


def _extract_codex_response_item(payload: Mapping[str, Any]) -> tuple[str, str] | None:
    if payload.get("type") != "message":
        return None
    role = payload.get("role")
    if role not in ("user", "assistant"):
        return None
    content = payload.get("content")
    if not isinstance(content, list):
        return None
    chunks: list[str] = []
    for c in content:
        if not isinstance(c, dict):
            continue
        if str(c.get("type") or "").lower() not in ("input_text", "output_text", "text"):
            continue
        raw_t = c.get("text")
        if not isinstance(raw_t, str) or not raw_t.strip():
            continue
        s = raw_t.strip()
        if s.startswith("# AGENTS.md instructions"):
            continue
        s = _CODEX_ENV_CTX_RE.sub("", s)
        s = _CODEX_PERM_CTX_RE.sub("", s)
        s = _CODEX_REC_PLUGINS_RE.sub("", s).strip()
        if not s or s.startswith(("# AGENTS.md instructions", "<recommended_plugins>")):
            continue
        chunks.append(s)
    joined = "\n".join(chunks).strip()
    return (role, joined) if joined else None


_extract_codex_resp_item = _extract_codex_response_item


def _extract_codex_compacted_turns(payload: Mapping[str, Any]) -> list[tuple[str, str]]:
    retained: list[tuple[str, str]] = []
    rc = payload.get("retained_context")
    if isinstance(rc, Mapping):
        ordered: list[tuple[int, int, str, str]] = []
        seq = 0
        for key, role in (("user_messages", "user"), ("assistant_messages", "assistant")):
            items = rc.get(key)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                txt = item.get("text")
                if not isinstance(txt, str) or not txt.strip():
                    continue
                raw_ord = item.get("order")
                ord_val = (
                    int(raw_ord)
                    if isinstance(raw_ord, int) and not isinstance(raw_ord, bool)
                    else (1_000_000 + seq)
                )
                ordered.append((ord_val, seq, role, txt.strip()))
                seq += 1
        ordered.sort(key=lambda t: (t[0], t[1]))
        retained = [(r, txt) for _, _, r, txt in ordered]

    rh_turns: list[tuple[str, str]] = []
    rh = payload.get("replacement_history")
    if isinstance(rh, list):
        for item in rh:
            if isinstance(item, Mapping):
                turn = _extract_codex_response_item(item)
                if turn is not None:
                    rh_turns.append(turn)

    out: list[tuple[str, str]] = []
    raw_msg = payload.get("message")
    summary_txt = raw_msg.strip() if isinstance(raw_msg, str) else ""
    if summary_txt and (not rh_turns or rh_turns[-1][1] != summary_txt):
        out.append(("assistant", summary_txt))
    out.extend(retained)
    seen_ret = set(retained)
    for turn in rh_turns:
        if turn not in seen_ret:
            out.append(turn)
    return out


_extract_codex_compacted = _extract_codex_compacted_turns


_CLAUDE_KEEP_PREFILTER_MARKERS: tuple[bytes, ...] = (
    b'"type":"user"',
    b'"type": "user"',
    b'"type":"assistant"',
    b'"type": "assistant"',
    b'"subtype":"compact_boundary"',
    b'"subtype": "compact_boundary"',
)
_CLAUDE_SKIP_PREFILTER_MARKERS: tuple[bytes, ...] = (
    b'"type":"attachment"',
    b'"type": "attachment"',
    b'"type":"api-request-shape"',
    b'"type": "api-request-shape"',
    b'"type":"api-request-blob"',
    b'"type": "api-request-blob"',
    b'"type":"api-request"',
    b'"type": "api-request"',
    b'"type":"progress"',
    b'"type": "progress"',
    b'"type":"file-history-snapshot"',
    b'"type": "file-history-snapshot"',
    b'"type":"queue-operation"',
    b'"type": "queue-operation"',
)


def _should_prefilter_skip_jsonl_line(
    cli: str, body: bytes, *, allow_agy_early_meta: bool = False
) -> bool:
    """Fast byte pre-filter on large lines (>1 KB) to skip giant non-dialogue JSON before ``json.loads``."""
    if len(body) <= 1024:
        return False
    if cli == "agy":
        if allow_agy_early_meta:
            return False
        return (
            b"USER_INPUT" not in body
            and b"PLANNER_RESPONSE" not in body
            and b"CHECKPOINT" not in body
        )
    if cli == "codex":
        return (
            b"item_completed" not in body
            and b"compacted" not in body
            and b'"message"' not in body
            and b"session_meta" not in body
        )
    if cli == "claude":
        # Check retained Claude record markers first so real user/assistant/compact_boundary
        # lines are never skipped even if they quote or nest non-dialogue type markers.
        if any(tok in body for tok in _CLAUDE_KEEP_PREFILTER_MARKERS):
            return False
        return any(tok in body for tok in _CLAUDE_SKIP_PREFILTER_MARKERS)
    return False


def _clean_jsonl_file(
    cli: str, jsonl_path: str, *, home: Path | None = None, wsl: bool = False
) -> tuple[list[tuple[str, str]], str, int]:
    """Return ``(clean_turns, discovered_cwd, compaction_count)``."""
    turns: list[tuple[str, str]] = []
    discovered_cwd = ""
    compaction_count = 0

    codex_saw_item = False
    codex_item_turns: list[tuple[str, str]] = []
    codex_resp_turns: list[tuple[str, str]] = []
    claude_just_compacted = False

    if not jsonl_path:
        return [], "", 0
    eff_path = (
        _resolve_safe_transcript_path(cli, jsonl_path, home, wsl=wsl)
        if home is not None
        else (jsonl_path if os.path.isfile(jsonl_path) else "")
    )
    if not eff_path:
        return [], "", 0

    try:
        fd = os.open(eff_path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return [], "", 0
    try:
        st_f = os.fstat(fd)
        if not stat.S_ISREG(st_f.st_mode) or st_f.st_uid != os.getuid():
            os.close(fd)
            return [], "", 0
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        return [], "", 0

    try:
        with os.fdopen(fd, "rb") as f:
            line_no = 0
            while True:
                raw_line, exceeded = _read_bounded_line(f)
                if not raw_line:
                    break
                if exceeded or not raw_line.endswith(b"\n"):
                    # Incomplete half-line at EOF or >16MiB attachment record: skip safely
                    continue
                body = raw_line[:-1].strip()
                if not body:
                    continue
                line_no += 1

                # Fast byte pre-filter on large lines (>1 KB) to skip giant non-dialogue JSON
                if _should_prefilter_skip_jsonl_line(
                    cli, body, allow_agy_early_meta=(line_no <= 8)
                ):
                    continue

                try:
                    obj = json.loads(body.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue

                if not discovered_cwd and isinstance(obj.get("cwd"), str) and obj["cwd"].strip():
                    discovered_cwd = _normalize_cwd(obj["cwd"])

                if cli == "agy":
                    cp_txt = _extract_agy_compaction_summary(obj)
                    if cp_txt is not None:
                        turns.clear()
                        if cp_txt:
                            turns.append(("assistant", cp_txt))
                        compaction_count += 1
                        continue
                    turn_agy = _clean_agy_record(obj)
                    if turn_agy is not None:
                        turns.append(turn_agy)

                elif cli == "claude":
                    if not _is_claude_sidechain_or_subagent(obj):
                        if obj.get("type") == "system" and obj.get("subtype") == "compact_boundary":
                            turns.clear()
                            compaction_count += 1
                            claude_just_compacted = True
                            continue
                        if obj.get("type") == "user" and bool(obj.get("isCompactSummary")):
                            turns.clear()
                            if not claude_just_compacted:
                                compaction_count += 1
                            claude_just_compacted = False
                            sum_t = _extract_claude_message_text(obj.get("message"), role="user")
                            if sum_t:
                                turns.append(("assistant", sum_t))
                            continue
                    turn_cl = _clean_claude_record(obj)
                    if turn_cl is not None:
                        claude_just_compacted = False
                        turns.append(turn_cl)

                elif cli == "codex":
                    typ_cx = obj.get("type")
                    pl = obj.get("payload")
                    if typ_cx == "session_meta" and isinstance(pl, dict) and not discovered_cwd:
                        if isinstance(pl.get("cwd"), str):
                            discovered_cwd = _normalize_cwd(pl["cwd"])
                    elif typ_cx == "compacted" and isinstance(pl, dict):
                        seed = _extract_codex_compacted_turns(pl)
                        codex_saw_item = False
                        codex_item_turns.clear()
                        codex_resp_turns = list(seed)
                        compaction_count += 1
                    elif typ_cx == "event_msg" and isinstance(pl, dict):
                        if pl.get("type") == "item_completed":
                            turn_cx = _extract_codex_item_completed(pl)
                            if turn_cx is not None:
                                if not codex_saw_item:
                                    hist = list(codex_resp_turns)
                                    if hist and hist[-1] == turn_cx:
                                        hist.pop()
                                    codex_item_turns = hist
                                    codex_resp_turns.clear()
                                    codex_saw_item = True
                                codex_item_turns.append(turn_cx)
                    elif typ_cx == "response_item" and isinstance(pl, dict) and not codex_saw_item:
                        turn_ri = _extract_codex_response_item(pl)
                        if turn_ri is not None:
                            codex_resp_turns.append(turn_ri)
    except OSError:
        return [], "", 0

    if cli == "codex":
        turns = codex_item_turns if codex_saw_item else codex_resp_turns
    return turns, discovered_cwd, compaction_count


# ---------------------------------------------------------------------------
# Remote workspace git status & non-Git shallow file helpers
# ---------------------------------------------------------------------------


def _has_git_worktree(real_cwd: str, home: Path) -> bool:
    cur = _normalize_cwd(real_cwd)
    for _ in range(13):
        if not cur or _is_broad_or_unsafe_workspace_dir(cur, home):
            return False
        if os.path.exists(os.path.join(cur, ".git")):
            return True
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            return False
        cur = parent
    return False


def _read_remote_git_status(real_cwd: str) -> tuple[str, list[str]]:
    git_bin = _resolve_remote_tool_bin("git")
    cmd = [
        git_bin,
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "-C",
        real_cwd,
        "status",
        "--short",
        "--untracked-files=normal",
    ]
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=0.8,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "", []
    if proc.returncode != 0:
        return "", []
    kept = [
        ln.rstrip()
        for ln in proc.stdout.decode("utf-8", errors="replace").splitlines()
        if ln.strip()
    ][:40]
    hw: list[str] = []
    seen: set[str] = set()
    for ln in kept:
        part = ln[3:].strip() if len(ln) > 3 else ln.strip()
        if " -> " in part:
            part = part.split(" -> ", 1)[1].strip()
        base = os.path.basename(part.strip('"').rstrip("/"))
        if base and base not in seen:
            seen.add(base)
            hw.append(base)
        stem = os.path.splitext(base)[0]
        if stem and len(stem) >= 3 and stem not in seen:
            seen.add(stem)
            hw.append(stem)
    return "\n".join(kept), hw


def _read_remote_repo_files(real_cwd: str, max_files: int = 20) -> list[str]:
    git_bin = _resolve_remote_tool_bin("git")
    base = [git_bin, "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", real_cwd]
    git_env = {**os.environ, "GIT_NO_LAZY_FETCH": "1"}

    def _run_nul(args: list[str]) -> list[str]:
        try:
            p = subprocess.run(
                [*base, *args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=0.6,
                check=False,
                env=git_env,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if p.returncode != 0:
            return []
        return [s.decode("utf-8", errors="replace") for s in p.stdout[:262144].split(b"\0") if s]

    tracked: list[str] = []
    for rel in _run_nul(
        ["ls-files", "-z", "--", ":(glob)*", ":(glob)*/*", ":(glob)*/*/*", ":(glob)*/*/*/*"]
    ):
        r = rel.strip()
        if not r or "\n" in r or "\r" in r:
            continue
        parts = r.split("/")
        bname = parts[-1]
        if not bname or bname in _REPO_SKIP_BASES:
            continue
        _, ext = os.path.splitext(bname)
        if ext.lower() not in _REPO_CORE_EXTS or _REPO_TEST_FILE_RE.search(bname):
            continue
        if not _REPO_SKIP_DIRS.intersection(parts[:-1]):
            tracked.append(r)
    if not tracked:
        return []
    live = set(tracked)
    out: list[str] = []
    seen_low: set[str] = set()
    for p in _run_nul(["log", "-z", "-n", "12", "--no-merges", "--no-renames", "--name-only", "--format="]):
        pc = p.strip()
        if pc in live:
            b = os.path.basename(pc)
            if b.lower() not in seen_low:
                seen_low.add(b.lower())
                out.append(b)
                if len(out) >= max_files:
                    return out
    tracked.sort(
        key=lambda p: p.count("/") - (1 if p.split("/", 1)[0] in _REPO_PRIORITY_ROOTS else 0)
    )
    for p in tracked:
        b = os.path.basename(p)
        if b.lower() not in seen_low:
            seen_low.add(b.lower())
            out.append(b)
            if len(out) >= max_files:
                break
    return out


def _read_remote_shallow_files(
    real_cwd: str, home: Path, *, max_files: int = 20, max_entries: int = 200
) -> list[str]:
    if _is_broad_or_unsafe_workspace_dir(real_cwd, home) or _has_git_worktree(real_cwd, home):
        return []
    scanned = 0

    def _accept(name: str) -> bool:
        if not name or name.startswith((".", "~$")) or name.lower() in _SHALLOW_SKIP_BASES_LOW:
            return False
        stem, ext = os.path.splitext(name)
        return ext.lower() in _SHALLOW_DOC_AND_CODE_EXTS and 2 <= len(stem.strip()) <= 64

    def _scan_one(dpath: str, *, include_dirs: bool) -> tuple[list[str], list[str]]:
        nonlocal scanned
        files: list[str] = []
        dirs: list[str] = []
        try:
            with os.scandir(dpath) as it:
                for entry in it:
                    if scanned >= max_entries:
                        break
                    scanned += 1
                    ename = entry.name
                    if not ename or ename.startswith((".", "~$")):
                        continue
                    try:
                        if include_dirs and entry.is_dir(follow_symlinks=False):
                            if ename.lower() not in _SHALLOW_SKIP_DIRS_LOW:
                                dirs.append(entry.path)
                        elif entry.is_file(follow_symlinks=False) and _accept(ename):
                            files.append(ename)
                    except OSError:
                        continue
        except OSError:
            return [], []
        files.sort(key=str.lower)
        dirs.sort(key=lambda p: os.path.basename(p).lower())
        return files, dirs

    root_f, sub_d = _scan_one(real_cwd, include_dirs=True)
    sub_f: list[str] = []
    for sd in sub_d:
        if scanned >= max_entries or len(root_f) + len(sub_f) >= max_files * 2:
            break
        lf, _ = _scan_one(sd, include_dirs=False)
        sub_f.extend(lf)

    out: list[str] = []
    seen_low: set[str] = set()
    for fn in (*root_f, *sub_f):
        low = fn.lower()
        if low not in seen_low:
            seen_low.add(low)
            out.append(fn)
            if len(out) >= max_files:
                break
    return out


def run_probe(
    req: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
    home_dir: str | Path | None = None,
    proc_root: str | Path | None = None,
) -> dict[str, Any]:
    """Execute stateless read-only probe and return JSON-serializable response dict."""
    if not isinstance(req, Mapping):
        return {"ok": False, "reason": "bad_request"}
    if req.get("ping_only"):
        return {
            "ok": True,
            "pong": True,
            "python": sys.version.split()[0],
            "hostname": os.uname().nodename if hasattr(os, "uname") else "",
        }

    eff_env = os.environ if env is None else env
    home = (
        Path(home_dir)
        if home_dir is not None
        else Path((eff_env.get("HOME") or "").strip() or os.path.expanduser("~"))
    )
    eff_proc = (
        Path(proc_root)
        if proc_root is not None
        else Path((eff_env.get("AVI_REMOTE_PROC_ROOT") or "/proc").strip())
    )

    hit, err_reason = _resolve_remote_session(req, home, eff_proc, eff_env)
    if hit is None:
        return {"ok": False, "reason": err_reason or "not_found"}

    cli = str(hit["cli"])
    sid = str(hit["session_id"])
    jpath = str(hit.get("jsonl_path") or "")
    if cli == "agy" and jpath:
        p_j = Path(jpath)
        if p_j.name == "transcript.jsonl":
            full_p = p_j.with_name("transcript_full.jsonl")
            safe_full = _resolve_safe_transcript_path("agy", full_p, home)
            if safe_full:
                jpath = safe_full
    if jpath and cli in ("codex", "claude", "agy"):
        jpath = _resolve_safe_transcript_path(cli, jpath, home)

    cur_etag = _file_etag(jpath) if jpath else ""
    req_etag = str(req.get("etag") or "").strip()
    req_skey = str(req.get("session_key_hint") or "").strip()
    include_git = bool(req.get("include_git", True))
    include_repo_files = bool(req.get("include_repo_files", True))
    raw_max = req.get("max_chars", DEFAULT_REFINER_CONTEXT_MAX_CHARS)
    max_chars = (
        max(0, min(1_000_000, int(raw_max)))
        if isinstance(raw_max, int) and not isinstance(raw_max, bool)
        else DEFAULT_REFINER_CONTEXT_MAX_CHARS
    )
    raw_chunk = req.get("chunk_chars")
    chunk_chars = (
        int(raw_chunk)
        if isinstance(raw_chunk, int) and not isinstance(raw_chunk, bool) and raw_chunk > 0
        else None
    )

    is_not_modified = bool(
        cur_etag
        and req_etag
        and cur_etag == req_etag
        and (not req_skey or req_skey == f"{cli}:{sid}")
    )

    if is_not_modified:
        eff_cwd = _normalize_cwd(str(hit.get("cwd") or ""))
        kept_turns: list[tuple[str, str]] = []
        evicted_chunks = 0
        compaction_count = 0
    else:
        raw_turns, disc_cwd, compaction_count = _clean_jsonl_file(cli, jpath, home=home)
        eff_cwd = _normalize_cwd(str(hit.get("cwd") or "") or disc_cwd)
        kept_turns, evicted_chunks = trim_turns_chunk_aligned(
            raw_turns, max_chars=max_chars, chunk_chars=chunk_chars
        )

    git_status = ""
    git_hotwords: list[str] = []
    repo_files: list[str] = []
    shallow_files: list[str] = []

    if include_git and eff_cwd and not _is_broad_or_unsafe_workspace_dir(eff_cwd, home):
        try:
            real_cwd = _normalize_cwd(os.path.realpath(eff_cwd))
        except OSError:
            real_cwd = ""
        if (
            real_cwd
            and os.path.isdir(real_cwd)
            and not _is_broad_or_unsafe_workspace_dir(real_cwd, home)
        ):
            if _has_git_worktree(real_cwd, home):
                git_status, git_hotwords = _read_remote_git_status(real_cwd)
                if include_repo_files and not git_hotwords:
                    repo_files = _read_remote_repo_files(real_cwd)
            elif include_repo_files:
                shallow_files = _read_remote_shallow_files(real_cwd, home)

    resp: dict[str, Any] = {
        "ok": True,
        "not_modified": is_not_modified,
        "unchanged": is_not_modified,
        "etag": cur_etag,
        "cli": cli,
        "session_id": sid,
        "matched_by": str(hit.get("matched_by") or "remote"),
        "cwd": eff_cwd,
        "git_status": git_status,
        "git_hotwords": git_hotwords,
        "repo_files": repo_files,
        "shallow_files": shallow_files,
        "files_included": bool(include_git and include_repo_files),
    }
    if not is_not_modified:
        resp["turns"] = [[r, t] for r, t in kept_turns]
        resp["evicted_chunks"] = evicted_chunks
        resp["compaction_count"] = compaction_count
        cold_start_mode = str(req.get("cold_start_fallback") or "auto").strip().lower()
        prior_checked_sid = str(req.get("prior_checked_session_id") or "").strip().lower()
        prior_already_checked = bool(
            prior_checked_sid and prior_checked_sid in (sid, f"{cli}:{sid}")
        )
        if (
            cold_start_mode in ("auto", "hotwords")
            and not prior_already_checked
            and len(kept_turns) < COLD_START_SWEET_MIN_TURNS
            and evicted_chunks == 0
            and compaction_count == 0
            and eff_cwd
            and not _is_broad_or_unsafe_workspace_dir(eff_cwd, home)
        ):
            prior_cands = discover_cli_prior_sessions(eff_cwd, home=home, wsl=False)
            best_prior, top5_prior = select_cold_start_prior_candidate(
                prior_cands,
                preferred_cli=("" if cli == "workspace" else cli),
                exclude_session_id=sid,
            )
            if top5_prior:
                prior_hw = aggregate_prior_candidates_hotwords(
                    top5_prior, max_words=COLD_START_PRIOR_MAX_HOTWORDS
                )
                if prior_hw:
                    resp["prior_hotwords"] = prior_hw
            if (
                cold_start_mode == "auto"
                and cli != "workspace"
                and best_prior is not None
                and max_chars > 0
            ):
                body_cap = prior_chunk_body_budget(max_chars)
                prior_src_turns = best_prior.get("tail_turns") or best_prior.get("turns") or ()
                if body_cap > 0 and prior_src_turns:
                    sliced_prior = slice_prior_turns_tail(
                        prior_src_turns,
                        max_chars=body_cap,
                        max_turns=COLD_START_PRIOR_MAX_TURNS,
                    )
                    if sliced_prior:
                        resp["prior_turns"] = [[r, t] for r, t in sliced_prior]
    return resp


def pack_probe_stdout(payload: Mapping[str, Any]) -> bytes:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    gz = gzip.compress(raw, compresslevel=1)
    return PROBE_FRAME_MAGIC + struct.pack(">I", len(gz)) + gz


def _cli_main() -> None:
    req_obj = globals().get("REQ")
    if req_obj is None:
        raw_in = sys.stdin.buffer.read(131072)
        req_obj = json.loads(raw_in.decode("utf-8")) if raw_in.strip() else {}
    resp = run_probe(req_obj if isinstance(req_obj, Mapping) else {})
    if isinstance(req_obj, Mapping) and req_obj.get("raw_json"):
        sys.stdout.buffer.write(
            json.dumps(resp, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        )
    else:
        sys.stdout.buffer.write(pack_probe_stdout(resp))
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    _cli_main()
