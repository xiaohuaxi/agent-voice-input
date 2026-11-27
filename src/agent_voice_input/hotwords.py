"""Flat personal ASR hotwords parser, directory-locked atomic editor, stat cache, and Stage 1/2 budgeter."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
import os
from pathlib import Path
import re
import stat
import sys
import threading
import time
from typing import Any, Iterator, Mapping, Sequence

from .config import (
    ASR_PROVIDER_ALIASES,
    DEFAULT_ASR_MODELS,
    ConfigError,
    ensure_app_dir,
    expand_user_path,
    is_dashscope_omni_chat_model,
    is_gemini_native_url,
    resolve_app_home_dir,
    resolve_asr_api,
)

MAX_HOTWORD_ALIASES: int = 5
MAX_HOTWORD_TERM_CHARS: int = 48
MIN_HOTWORD_ALIAS_CHARS: int = 2
MAX_HOTWORD_ALIAS_CHARS: int = 24
HOTWORDS_MAX_BYTES: int = 256 * 1024
HOTWORDS_MAX_ENTRIES: int = 2000

STAGE2_HOTWORDS_MAX_ITEMS: int = 200
STAGE2_HOTWORDS_MAX_CHARS: int = 3000
SINGLE_HOP_HOTWORDS_MAX_ITEMS: int = STAGE2_HOTWORDS_MAX_ITEMS
SINGLE_HOP_HOTWORDS_MAX_CHARS: int = STAGE2_HOTWORDS_MAX_CHARS
STAGE2_VOCABULARY_HINTS_PREAMBLE: str = (
    "Speaker's personal names/terms. Use ONLY to fix a draft word that sounds like an entry "
    "AND clearly means it in context; otherwise ignore. Never insert unspoken terms, never "
    "translate a correctly recognized native word (豆包 stays 豆包), never mention this list."
)

_ALIAS_SEPARATOR = "<-"
_ALIAS_SPLIT_RE = re.compile(r"[,，]")
_XML_BRACKET_RE = re.compile(r"[<>]")
_CJK_SENTENCE_PUNCT_RE = re.compile(r"[。！？；]")
_OPENAI_KEYWORD_FORBIDDEN_RE = _XML_BRACKET_RE
_CTRL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
_UNICODE_LINE_SEPS = frozenset({"\x85", "\u2028", "\u2029"})
_INLINE_COMMENT_RE = re.compile(r"[ \t]+#")
_SECTION_HEADER_RE = re.compile(r"^\[([^\[\]]+)\]$")
_CONFLICT_MARKER_RE = re.compile(r"^(<{7}|\|{7}|={7}|>{7})(?:\s|$)")

# Verified OpenRouter /audio/transcriptions models that accept top-level keyterms[]
# (protocol-verified against upstream endpoints).
# Note: deepgram/nova-3 returns HTTP 400 on OpenRouter top-level keyterms and is intentionally excluded;
# unverified models (e.g. mistralai/voxtral-mini-3b-2507) are excluded until protocol-verified.
_OPENROUTER_KEYTERMS_MODELS: frozenset[str] = frozenset(
    {
        "microsoft/mai-transcribe-2",
        "openai/gpt-transcribe",
        "x-ai/grok-stt-2.0",
        "mistralai/voxtral-small-transcribe-3-0",
        "elevenlabs/scribe-v2",
        "google/gemini-3.5-transcribe",
        "assemblyai/universal-3-6-pro",
    }
)

_HOTWORDS_CACHE_LOCK = threading.Lock()
_HOTWORDS_CACHE: dict[str, tuple[tuple[int, int, int, int, int], HotwordsTable]] = {}
_HOTWORDS_BAD_ENCODING_WARNED: dict[str, tuple[int, int, int, int, int]] = {}
_HOTWORDS_CACHE_MAX = 32


def _format_entry_term(term: str, *, pinned: bool = False) -> str:
    escaped = f"\\{term}" if term.startswith("!") else term
    return f"!{escaped}" if pinned else escaped


@dataclass(frozen=True)
class HotwordEntry:
    term: str
    aliases: tuple[str, ...] = ()
    pinned: bool = False

    def format_line(self) -> str:
        head = _format_entry_term(self.term, pinned=self.pinned)
        if self.aliases:
            return f"{head} {_ALIAS_SEPARATOR} {', '.join(self.aliases)}"
        return head


@dataclass(frozen=True)
class AmbiguousAlias:
    alias: str
    terms: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"alias": self.alias, "terms": list(self.terms)}


@dataclass(frozen=True)
class HotwordsLineIssue:
    line_no: int
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"line_no": self.line_no, "reason": self.reason}


@dataclass(frozen=True)
class HotwordsTable:
    entries: tuple[HotwordEntry, ...] = ()
    path: Path | None = None
    exists: bool = False
    encoding_error: str = ""
    conflict_lines: tuple[int, ...] = ()
    invalid_lines: tuple[HotwordsLineIssue, ...] = ()
    legacy_sections: tuple[str, ...] = ()
    ambiguous_aliases: tuple[AmbiguousAlias, ...] = ()
    alias_warnings: tuple[str, ...] = ()
    file_warnings: tuple[str, ...] = ()
    size_limit_exceeded: bool = False
    entry_limit_exceeded: bool = False
    total_valid_entries: int = 0

    @property
    def terms(self) -> tuple[str, ...]:
        return tuple(e.term for e in self.entries)

    @property
    def stage1_terms(self) -> tuple[str, ...]:
        return tuple(e.term for e in order_stage1_entries(self.entries))

    @property
    def stage2_entries(self) -> tuple[HotwordEntry, ...]:
        return tuple(order_stage2_entries(self.entries))

    @property
    def warnings_count(self) -> int:
        return (
            len(self.conflict_lines)
            + len(self.invalid_lines)
            + len(self.legacy_sections)
            + len(self.ambiguous_aliases)
            + len(self.alias_warnings)
            + len(self.file_warnings)
            + (1 if self.encoding_error else 0)
            + (1 if self.size_limit_exceeded else 0)
            + (1 if self.entry_limit_exceeded else 0)
        )

    def diagnostic_warnings(self) -> list[str]:
        out: list[str] = []
        if self.encoding_error:
            out.append(self.encoding_error)
        out.extend(self.file_warnings)
        if self.size_limit_exceeded:
            out.append(
                f"file exceeded {HOTWORDS_MAX_BYTES // 1024} KiB read limit; trailing content ignored"
            )
        if self.entry_limit_exceeded:
            out.append(
                f"entry count ({self.total_valid_entries}) exceeded {HOTWORDS_MAX_ENTRIES} limit; "
                "trailing entries ignored"
            )
        if self.conflict_lines:
            lines_s = ", ".join(str(ln) for ln in self.conflict_lines)
            out.append(f"unresolved git conflict block(s) skipped at line(s): {lines_s}")
        if self.legacy_sections:
            out.append(
                f"legacy section header(s) ignored (entries merged flat): {', '.join(self.legacy_sections)}"
            )
        for amb in self.ambiguous_aliases:
            out.append(
                f"ambiguous alias {amb.alias!r} hidden across multiple terms: {', '.join(amb.terms)}"
            )
        out.extend(self.alias_warnings)
        for issue in self.invalid_lines:
            out.append(f"invalid line {issue.line_no} skipped: {issue.reason}")
        return out

    def as_entries_list(self) -> list[dict[str, Any]]:
        return [
            {"term": e.term, "aliases": list(e.aliases), "pinned": e.pinned}
            for e in self.entries
        ]


@dataclass(frozen=True)
class HotwordsEditResult:
    path: Path
    added: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    not_found: tuple[str, ...] = ()
    changed: bool = False
    updated: tuple[str, ...] = ()
    aliases_added: tuple[str, ...] = ()
    pinned: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    scope: str = "personal"


@dataclass(frozen=True)
class HotwordsQuotaSpec:
    key: str
    label: str
    max_terms: int
    max_chars: int | None
    separator: str = ""
    max_term_chars: int | None = None
    collapse_file_ext: bool = False
    sanitize_openai_keywords: bool = False
    note: str = ""


@dataclass(frozen=True)
class HotwordsBudgetResult:
    provider: str
    spec_key: str
    label: str
    kept: tuple[str, ...]
    dropped: tuple[str, ...]
    total_input: int
    used_chars: int
    max_terms: int
    max_chars: int | None
    note: str = ""
    personal_kept: int = 0
    dynamic_kept: int = 0
    builtin_kept: int = 0
    personal_dropped: int = 0

    @property
    def kept_count(self) -> int:
        return len(self.kept)

    @property
    def dropped_count(self) -> int:
        return len(self.dropped)

    @property
    def truncated(self) -> bool:
        return bool(self.dropped)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "spec_key": self.spec_key,
            "label": self.label,
            "kept_count": self.kept_count,
            "dropped_count": self.dropped_count,
            "dropped": list(self.dropped),
            "total_input": self.total_input,
            "used_chars": self.used_chars,
            "max_terms": self.max_terms,
            "max_chars": self.max_chars,
            "truncated": self.truncated,
            "note": self.note,
            "personal_kept": self.personal_kept,
            "dynamic_kept": self.dynamic_kept,
            "builtin_kept": self.builtin_kept,
            "personal_dropped": self.personal_dropped,
        }


@dataclass(frozen=True)
class Stage2HotwordsBudgetSummary:
    kept_count: int
    dropped_count: int
    total_input: int
    used_chars: int
    max_items: int
    max_chars: int
    dropped: tuple[str, ...] = ()
    warnings_count: int = 0
    mode: str = "personal"
    scopes: tuple[str, ...] = ()

    @property
    def truncated(self) -> bool:
        return self.dropped_count > 0

    def as_safe_counts_dict(self) -> dict[str, int]:
        """Return privacy-safe integer counters only (never includes term text)."""
        return {
            "total": self.total_input,
            "kept": self.kept_count,
            "dropped_count": self.dropped_count,
            "used_chars": self.used_chars,
            "max_items": self.max_items,
            "max_chars": self.max_chars,
            "warnings_count": self.warnings_count,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "scopes": list(self.scopes),
            "kept_count": self.kept_count,
            "dropped_count": self.dropped_count,
            "dropped": list(self.dropped),
            "total_input": self.total_input,
            "used_chars": self.used_chars,
            "max_items": self.max_items,
            "max_chars": self.max_chars,
            "warnings_count": self.warnings_count,
            "truncated": self.truncated,
        }


# Single source of truth for per-provider ASR hotword quotas (aligned with
# per-provider protocol measurements):
# - Doubao file API contract supports up to 5,000 terms; recall is sharpest around 30-50 terms.
# - DashScope qwen3-asr-flash hard cap is 10,000 text_tokens (~14,500 chars); 150 terms / 1,800 chars leaves wide headroom.
# - OpenAI gpt-transcribe supports native keywords[] (50 terms sweet spot; must strip <, >, \r, \n).
# - Whisper /audio/transcriptions reserves 7 chars for "常用技术术语：" (360 - 7 = 353).
# - Single-hop multimodal models align with Stage 2 wide vocabulary hints (200 items / 3,000 chars).
HOTWORDS_QUOTA_SPECS: dict[str, HotwordsQuotaSpec] = {
    "doubao": HotwordsQuotaSpec(
        key="doubao",
        label="doubao",
        max_terms=50,
        max_chars=500,
        separator="",
        max_term_chars=60,
        collapse_file_ext=True,
    ),
    "openai_keywords": HotwordsQuotaSpec(
        key="openai_keywords",
        label="openai(keywords)",
        max_terms=50,
        max_chars=None,
        separator="",
        sanitize_openai_keywords=True,
    ),
    "whisper": HotwordsQuotaSpec(
        key="whisper",
        label="whisper",
        max_terms=35,
        max_chars=353,
        separator="、",
    ),
    "dashscope": HotwordsQuotaSpec(
        key="dashscope",
        label="dashscope",
        max_terms=150,
        max_chars=1800,
        separator="、",
    ),
    "single_hop": HotwordsQuotaSpec(
        key="single_hop",
        label="single_hop",
        max_terms=SINGLE_HOP_HOTWORDS_MAX_ITEMS,
        max_chars=SINGLE_HOP_HOTWORDS_MAX_CHARS,
        separator=", ",
    ),
    "openrouter_keyterms": HotwordsQuotaSpec(
        key="openrouter_keyterms",
        label="openrouter(keyterms)",
        max_terms=50,
        max_chars=None,
        separator="",
        max_term_chars=100,
    ),
    "openrouter_stt_unsupported": HotwordsQuotaSpec(
        key="openrouter_stt_unsupported",
        label="openrouter(stt)",
        max_terms=0,
        max_chars=0,
        separator="",
        note=(
            "OpenRouter /audio/transcriptions keyterms is only enabled for verified models "
            "(mai-transcribe, gpt-transcribe, grok-stt, voxtral, scribe, gemini-transcribe, assemblyai); "
            "use single-hop or Stage 2 refiner"
        ),
    ),
    "deepseek_asr_unsupported": HotwordsQuotaSpec(
        key="deepseek_asr_unsupported",
        label="deepseek(asr)",
        max_terms=0,
        max_chars=0,
        separator="",
        note="DeepSeek /tools/asr does not accept hotwords/prompt fields; rely on Stage 2 refiner",
    ),
}

MAINSTREAM_QUOTA_PROVIDERS: tuple[str, ...] = ("doubao", "dashscope", "openai", "whisper", "gemini")


def resolve_personal_hotwords_path(
    env: Mapping[str, str] | None = None,
    *,
    home_dir: str | Path | None = None,
) -> Path | None:
    """Return ``<app_home>/hotwords/personal.txt`` (or ``None`` if hermetic env has no home)."""
    app_home = resolve_app_home_dir(env, home_dir=home_dir)
    if app_home is None:
        return None
    return app_home / "hotwords" / "personal.txt"


def dedupe_hotwords_ci(words: Sequence[str]) -> list[str]:
    """Case-insensitive deduplication preserving first-seen original casing."""
    out: list[str] = []
    seen_low: set[str] = set()
    for raw in words:
        w = raw.strip()
        if w and (low := w.lower()) not in seen_low:
            seen_low.add(low)
            out.append(w)
    return out


def _split_physical_lines(text: str, *, keepends: bool = False) -> list[str]:
    """Split ``text`` strictly on ``\\r\\n`` or ``\\n`` (never on Unicode line separators)."""
    if not text:
        return []
    out: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        nl = text.find("\n", start)
        if nl == -1:
            out.append(text[start:])
            break
        end = nl + 1
        if keepends:
            out.append(text[start:end])
        else:
            body_end = nl - 1 if nl > start and text[nl - 1] == "\r" else nl
            out.append(text[start:body_end])
        start = end
    return out


def _has_forbidden_line_chars(raw: str) -> bool:
    return bool(_CTRL_CHAR_RE.search(raw) or any(ch in _UNICODE_LINE_SEPS for ch in raw))


def validate_hotword_term(raw: str) -> str:
    """Validate and normalize a single canonical hotword term for ``personal.txt``."""
    if not isinstance(raw, str):
        raise ValueError(f"hotword must be a string, got {type(raw).__name__}")
    if _has_forbidden_line_chars(raw):
        raise ValueError(f"hotword contains newline or control characters: {raw!r}")
    term = raw.strip()
    if not term:
        raise ValueError("hotword must not be empty")
    if len(term) > MAX_HOTWORD_TERM_CHARS:
        raise ValueError(
            f"hotword {term!r} is too long ({len(term)} chars; maximum is {MAX_HOTWORD_TERM_CHARS})"
        )
    if _XML_BRACKET_RE.search(term):
        raise ValueError(f"hotword {term!r} cannot contain XML angle brackets '<' or '>'")
    if _CJK_SENTENCE_PUNCT_RE.search(term):
        raise ValueError(f"hotword {term!r} cannot contain CJK sentence punctuation ('。', '！', '？', '；')")
    if term.startswith("[") and term.endswith("]"):
        raise ValueError(f"hotword {term!r} conflicts with section header syntax '[section]'")
    if term.startswith("#"):
        raise ValueError(f"hotword {term!r} cannot start with '#' (comment line prefix)")
    if _INLINE_COMMENT_RE.search(term):
        raise ValueError(f"hotword {term!r} cannot contain whitespace followed by '#' (inline comment syntax)")
    if _ALIAS_SEPARATOR in term:
        raise ValueError(
            f"hotword {term!r} cannot contain alias separator '{_ALIAS_SEPARATOR}' (pass aliases separately)"
        )
    return term


def validate_hotword_alias(raw: str) -> str:
    """Validate and normalize a single mishearing alias (2..24 chars, safe morphology)."""
    if not isinstance(raw, str):
        raise ValueError(f"mishearing alias must be a string, got {type(raw).__name__}")
    if _has_forbidden_line_chars(raw):
        raise ValueError(f"mishearing alias contains newline or control characters: {raw!r}")
    alias = raw.strip()
    if not alias:
        raise ValueError("mishearing alias must not be empty")
    if _ALIAS_SEPARATOR in alias or _ALIAS_SPLIT_RE.search(alias):
        raise ValueError(f"mishearing alias {alias!r} cannot contain ',' / '，' or '{_ALIAS_SEPARATOR}'")
    if alias.startswith("#") or _INLINE_COMMENT_RE.search(alias):
        raise ValueError(f"mishearing alias {alias!r} conflicts with '#' comment syntax")
    if _XML_BRACKET_RE.search(alias):
        raise ValueError(f"mishearing alias {alias!r} cannot contain XML angle brackets '<' or '>'")
    if _CJK_SENTENCE_PUNCT_RE.search(alias):
        raise ValueError(
            f"mishearing alias {alias!r} cannot contain CJK sentence punctuation ('。', '！', '？', '；')"
        )
    if len(alias) < MIN_HOTWORD_ALIAS_CHARS:
        raise ValueError(
            f"mishearing alias {alias!r} is too short (single-character aliases are rejected to prevent false replacements)"
        )
    if len(alias) > MAX_HOTWORD_ALIAS_CHARS:
        raise ValueError(
            f"mishearing alias {alias!r} is too long ({len(alias)} chars; maximum is {MAX_HOTWORD_ALIAS_CHARS})"
        )
    return alias


def validate_hotword_aliases(
    term: str,
    aliases: Sequence[str],
    *,
    existing_canonical_lows: set[str] | frozenset[str] = frozenset(),
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Validate, split (by ',' or '，'), deduplicate, and check guardrails for mishearing aliases.

    Returns ``(clean_aliases, warnings)``. Raises ``ValueError`` on empty aliases, <2 or >24 char aliases,
    >5 aliases, self-conflicts, or conflicts with any canonical term in ``existing_canonical_lows``.
    Emits a non-blocking warning for 2-character aliases.
    """
    term_clean = term.strip()
    term_low = term_clean.lower()
    clean: list[str] = []
    seen_low: set[str] = set()
    warnings: list[str] = []

    for raw_item in aliases:
        if not isinstance(raw_item, str):
            raise ValueError(f"mishearing alias must be a string, got {type(raw_item).__name__}")
        if _has_forbidden_line_chars(raw_item):
            raise ValueError(f"mishearing alias contains newline or control characters: {raw_item!r}")
        for piece in _ALIAS_SPLIT_RE.split(raw_item):
            alias = validate_hotword_alias(piece)
            al_low = alias.lower()
            if al_low == term_low:
                raise ValueError(f"mishearing alias {alias!r} cannot match its own canonical term {term_clean!r}")
            if al_low in existing_canonical_lows:
                raise ValueError(f"mishearing alias {alias!r} conflicts with an existing canonical hotword")
            if al_low in seen_low:
                continue
            seen_low.add(al_low)
            clean.append(alias)

    if len(clean) > MAX_HOTWORD_ALIASES:
        raise ValueError(
            f"too many mishearing aliases for {term_clean!r}: {len(clean)} exceeds maximum of {MAX_HOTWORD_ALIASES}"
        )

    for alias in clean:
        if len(alias) == 2:
            warnings.append(
                f"2-character mishearing alias {alias!r} for {term_clean!r} may risk colliding with common words; "
                "keep only if you observe repeated ASR mishearings"
            )

    return tuple(clean), tuple(warnings)


def _strip_line_comment(raw_line: str) -> str:
    """Strip standalone leading '#' comments and whitespace-preceded inline ' #' comments.

    Preserves '#' inside tokens such as ``C#``, ``F#``, or ``Issue#42``.
    Only strips ASCII spaces at edges so control / Unicode line separators remain visible to validation.
    """
    stripped = raw_line.strip(" ")
    if not stripped or stripped.startswith("#"):
        return ""
    m = _INLINE_COMMENT_RE.search(stripped)
    if m is not None:
        return stripped[: m.start()].strip(" ")
    return stripped


def _parse_section_header(cleaned_line: str) -> str | None:
    """Return raw section header text ``[name]`` if ``cleaned_line`` matches ``[name]``, else ``None``."""
    m = _SECTION_HEADER_RE.match(cleaned_line)
    if m is None:
        return None
    inner = m.group(1).strip()
    return f"[{inner}]" if inner else None


def _parse_entry_cleaned_line(cleaned: str) -> tuple[str, tuple[str, ...], bool]:
    """Parse a comment-stripped non-header line into ``(canonical_term, valid_aliases, pinned)``.

    Supports ``Term``, ``!Term``, ``\\!Term``, ``!\\!Term``, and optional ``<- alias1, alias2``.
    Raises ``ValueError`` if canonical term or any non-empty alias violates morphology guardrails.
    """
    if _ALIAS_SEPARATOR in cleaned:
        left_raw, right_raw = cleaned.split(_ALIAS_SEPARATOR, 1)
    else:
        left_raw, right_raw = cleaned, None

    left = left_raw.strip(" ")
    pinned = False
    if left.startswith("!"):
        pinned = True
        left = left[1:].strip(" ")
    if left.startswith(r"\!"):
        left = left[1:]

    term = validate_hotword_term(left)
    term_low = term.lower()

    aliases: list[str] = []
    if right_raw is not None:
        for piece in _ALIAS_SPLIT_RE.split(right_raw):
            if not piece.strip(" "):
                continue
            al = validate_hotword_alias(piece)
            if al.lower() == term_low:
                continue
            aliases.append(al)

    return term, tuple(aliases), pinned


@dataclass
class _PassARecord:
    pinned_spellings: list[str] = field(default_factory=list)
    unpinned_spellings: list[str] = field(default_factory=list)
    raw_aliases_by_low: dict[str, list[str]] = field(default_factory=dict)


def _parse_hotwords_core(
    text: str,
    *,
    path: Path | None = None,
    exists: bool = True,
    size_limit_exceeded: bool = False,
) -> tuple[HotwordsTable, dict[str, tuple[str, ...]]]:
    """Two-pass flat parser returning ``(HotwordsTable, raw_alias_owners_by_low)``."""
    clean_text = text[1:] if text.startswith("\ufeff") else text

    collected_order: list[str] = []
    records: dict[str, _PassARecord] = {}
    overflow_canonical_keys: set[str] = set()
    conflict_lines: list[int] = []
    invalid_lines: list[HotwordsLineIssue] = []
    legacy_sections: list[str] = []
    legacy_seen: set[str] = set()
    conflict_depth = 0

    for line_no, line_body in enumerate(_split_physical_lines(clean_text, keepends=False), start=1):
        # 1. Git conflict block state machine (handles 2-way, diff3 |||||||, nested, and unclosed EOF)
        cm = _CONFLICT_MARKER_RE.match(line_body)
        if cm is not None:
            marker = cm.group(1)
            if marker == "<<<<<<<":
                if conflict_depth == 0:
                    conflict_lines.append(line_no)
                conflict_depth += 1
                continue
            if conflict_depth > 0:
                if marker == ">>>>>>>":
                    conflict_depth -= 1
                continue
        elif conflict_depth > 0:
            continue

        # 2. Reject Unicode line separators anywhere on a physical line before stripping
        if any(ch in _UNICODE_LINE_SEPS for ch in line_body):
            invalid_lines.append(
                HotwordsLineIssue(line_no=line_no, reason="contains Unicode line separator")
            )
            continue

        cleaned = _strip_line_comment(line_body)
        if not cleaned:
            continue

        # 3. Legacy [section] header: skip with warning, merge following entries flat
        sec = _parse_section_header(cleaned)
        if sec is not None:
            sec_low = sec.lower()
            if sec_low not in legacy_seen:
                legacy_seen.add(sec_low)
                legacy_sections.append(sec)
            continue

        # 4. Morphology-validated entry parsing
        try:
            term, line_aliases, line_pinned = _parse_entry_cleaned_line(cleaned)
        except ValueError as exc:
            invalid_lines.append(HotwordsLineIssue(line_no=line_no, reason=str(exc)))
            continue

        low = term.lower()
        rec = records.get(low)
        if rec is None:
            if len(collected_order) >= HOTWORDS_MAX_ENTRIES:
                overflow_canonical_keys.add(low)
                continue
            collected_order.append(low)
            rec = _PassARecord()
            records[low] = rec

        if line_pinned:
            rec.pinned_spellings.append(term)
        else:
            rec.unpinned_spellings.append(term)
        for al in line_aliases:
            rec.raw_aliases_by_low.setdefault(al.lower(), []).append(al)

    # Pass A global indices (NO premature 5-alias truncation, NO premature collision filtering)
    all_canonical_keys: set[str] = set(collected_order)
    winning_terms: dict[str, str] = {}
    winning_pinned: dict[str, bool] = {}
    alias_to_canonicals: dict[str, set[str]] = {}
    alias_spellings_global: dict[str, set[str]] = {}

    for low in collected_order:
        rec = records[low]
        is_pinned = bool(rec.pinned_spellings)
        winning_pinned[low] = is_pinned
        winning_terms[low] = min(rec.pinned_spellings if is_pinned else rec.unpinned_spellings)
        for al_low, spellings in rec.raw_aliases_by_low.items():
            alias_to_canonicals.setdefault(al_low, set()).add(low)
            alias_spellings_global.setdefault(al_low, set()).update(spellings)

    # Full raw alias ownership map for edit-side bidirectional checks (includes ambiguous & >5 aliases)
    raw_alias_owners: dict[str, tuple[str, ...]] = {
        al_low: tuple(
            sorted((winning_terms[owner_low] for owner_low in owner_lows), key=lambda t: (t.lower(), t))
        )
        for al_low, owner_lows in alias_to_canonicals.items()
    }

    # Pass B adjudication: canonical collisions, ambiguous aliases (>=2 owners), then sort & cap at 5
    ambiguous_keys: set[str] = set()
    ambiguous_list: list[AmbiguousAlias] = []
    for al_low, owner_lows in alias_to_canonicals.items():
        if al_low in all_canonical_keys:
            continue
        if len(owner_lows) >= 2:
            ambiguous_keys.add(al_low)
            rep_alias = min(alias_spellings_global[al_low])
            ambiguous_list.append(AmbiguousAlias(alias=rep_alias, terms=raw_alias_owners[al_low]))
    ambiguous_list.sort(key=lambda a: (a.alias.lower(), a.alias))

    entries: list[HotwordEntry] = []
    alias_warnings: list[str] = []
    for low in collected_order:
        term = winning_terms[low]
        rec = records[low]
        eff_aliases: list[str] = []
        for al_low in sorted(rec.raw_aliases_by_low):
            rep_al = min(rec.raw_aliases_by_low[al_low])
            if al_low in all_canonical_keys:
                alias_warnings.append(
                    f"alias {rep_al!r} for {term!r} ignored (conflicts with canonical term {winning_terms[al_low]!r})"
                )
                continue
            if al_low in ambiguous_keys:
                continue
            eff_aliases.append(rep_al)
        eff_aliases.sort(key=lambda a: (a.lower(), a))
        entries.append(
            HotwordEntry(
                term=term,
                aliases=tuple(eff_aliases[:MAX_HOTWORD_ALIASES]),
                pinned=winning_pinned[low],
            )
        )

    total_valid_entries = len(collected_order) + len(overflow_canonical_keys)
    table = HotwordsTable(
        entries=tuple(entries),
        path=path,
        exists=exists,
        conflict_lines=tuple(conflict_lines),
        invalid_lines=tuple(invalid_lines),
        legacy_sections=tuple(legacy_sections),
        ambiguous_aliases=tuple(ambiguous_list),
        alias_warnings=tuple(alias_warnings),
        size_limit_exceeded=size_limit_exceeded,
        entry_limit_exceeded=bool(overflow_canonical_keys),
        total_valid_entries=total_valid_entries,
    )
    return table, raw_alias_owners


def parse_hotwords_text(
    text: str,
    *,
    path: Path | None = None,
    exists: bool = True,
    size_limit_exceeded: bool = False,
) -> HotwordsTable:
    """Parse flat ``personal.txt`` with ``!`` pinning, ``<-`` aliases, conflict skipping, and two-pass adjudication."""
    table, _ = _parse_hotwords_core(
        text,
        path=path,
        exists=exists,
        size_limit_exceeded=size_limit_exceeded,
    )
    return table


def order_stage1_entries(entries: Sequence[HotwordEntry]) -> list[HotwordEntry]:
    """Order entries for Stage 1 ASR: pinned -> effective-aliased -> plain, preserving file order within each tier."""
    return sorted(
        entries,
        key=lambda e: 0 if e.pinned else (1 if e.aliases else 2),
    )


def order_stage2_entries(entries: Sequence[HotwordEntry]) -> list[HotwordEntry]:
    """Order entries deterministically for Stage 2 Block 0 so prefix cache stays byte-identical across edits."""
    return sorted(
        entries,
        key=lambda e: (0 if e.pinned else (1 if e.aliases else 2), e.term.lower(), e.term),
    )


def clear_hotwords_cache() -> None:
    """Clear in-process stat-validated hotwords file cache and encoding-warning state."""
    with _HOTWORDS_CACHE_LOCK:
        _HOTWORDS_CACHE.clear()
        _HOTWORDS_BAD_ENCODING_WARNED.clear()


class _BadHotwordsEncodingError(ValueError):
    """Internal marker for non-UTF-8 or NUL-containing hotwords files."""


def _describe_encoding_error(
    target: Path,
    raw_bytes: bytes,
    exc: UnicodeDecodeError | None = None,
) -> str:
    if raw_bytes.startswith(b"\xff\xfe\x00\x00"):
        enc_hint = "UTF-32 LE BOM detected"
    elif raw_bytes.startswith(b"\x00\x00\xfe\xff"):
        enc_hint = "UTF-32 BE BOM detected"
    elif raw_bytes.startswith(b"\xff\xfe"):
        enc_hint = "UTF-16 LE BOM detected"
    elif raw_bytes.startswith(b"\xfe\xff"):
        enc_hint = "UTF-16 BE BOM detected"
    elif b"\x00" in raw_bytes:
        enc_hint = "NUL byte detected (likely UTF-16/UTF-32 without BOM or binary file)"
    elif exc is not None:
        enc_hint = f"invalid UTF-8 byte at offset {exc.start}"
    else:  # pragma: no cover - defensive fallback
        enc_hint = "invalid UTF-8"
    return f"hotwords file {target} is not valid UTF-8 ({enc_hint}); please re-save it as UTF-8"


def _decode_hotwords_bytes(target: Path, raw_bytes: bytes) -> tuple[str, bool]:
    """Decode UTF-8 (with optional UTF-8 BOM), rejecting NUL bytes and non-UTF-8 encodings."""
    if b"\x00" in raw_bytes:
        raise _BadHotwordsEncodingError(_describe_encoding_error(target, raw_bytes))
    has_bom = raw_bytes.startswith(b"\xef\xbb\xbf")
    try:
        text = raw_bytes[3:].decode("utf-8") if has_bom else raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _BadHotwordsEncodingError(_describe_encoding_error(target, raw_bytes, exc)) from None
    return text, has_bom


def _warn_bad_encoding_once(
    cache_key: str,
    sig: tuple[int, int, int, int, int],
    message: str,
) -> None:
    emit = False
    with _HOTWORDS_CACHE_LOCK:
        if _HOTWORDS_BAD_ENCODING_WARNED.get(cache_key) != sig:
            if (
                len(_HOTWORDS_BAD_ENCODING_WARNED) >= _HOTWORDS_CACHE_MAX
                and cache_key not in _HOTWORDS_BAD_ENCODING_WARNED
            ):
                _HOTWORDS_BAD_ENCODING_WARNED.pop(next(iter(_HOTWORDS_BAD_ENCODING_WARNED)), None)
            _HOTWORDS_BAD_ENCODING_WARNED[cache_key] = sig
            emit = True
    if emit:
        try:
            print(
                f"[agent-voice-input] WARN: {message}; skipping manual hotwords until fixed",
                file=sys.stderr,
                flush=True,
            )
        except Exception:  # pragma: no cover - stderr write failure during diagnostic warning
            pass


def _ensure_hotwords_parent_dirs(
    target: Path,
    *,
    env: Mapping[str, str] | None = None,
    create: bool,
) -> None:
    """Validate/create logical ``app_home`` (when applicable) and ``target.parent`` via ``ensure_app_dir``."""
    app_home: Path | None = None
    try:
        app_home = resolve_app_home_dir(env)
    except ConfigError:
        app_home = None

    parent = target.parent
    if app_home is not None and (parent == app_home / "hotwords" or parent == app_home):
        ensure_app_dir(app_home, create=create)
    elif parent.name == "hotwords" and parent.parent.name == ".agent-voice-input":
        ensure_app_dir(parent.parent, create=create)
    ensure_app_dir(parent, create=create)


def _read_bounded_fd(fd: int, limit_bytes: int) -> bytes:
    """Read up to ``limit_bytes`` from an open file descriptor."""
    chunks: list[bytes] = []
    remaining = limit_bytes
    while remaining > 0:
        chunk = os.read(fd, min(65536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _evict_cache_key(cache_key: str, *, use_cache: bool) -> None:
    with _HOTWORDS_CACHE_LOCK:
        if use_cache:
            _HOTWORDS_CACHE.pop(cache_key, None)
        _HOTWORDS_BAD_ENCODING_WARNED.pop(cache_key, None)


def _store_cache_entry(cache_key: str, sig: tuple[int, int, int, int, int], table: HotwordsTable) -> None:
    with _HOTWORDS_CACHE_LOCK:
        if len(_HOTWORDS_CACHE) >= _HOTWORDS_CACHE_MAX and cache_key not in _HOTWORDS_CACHE:
            _HOTWORDS_CACHE.pop(next(iter(_HOTWORDS_CACHE)), None)
        _HOTWORDS_CACHE[cache_key] = (sig, table)


def load_hotwords_table(
    path_str: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    use_cache: bool = True,
    strict_encoding: bool = False,
) -> HotwordsTable:
    """Load and parse ``personal.txt`` with ``O_NOFOLLOW``, 256 KiB bounded read, and realpath stat caching."""
    if path_str is None:
        target = resolve_personal_hotwords_path(env=env)
    elif isinstance(path_str, Path):
        target = path_str
    else:
        raw_spec = path_str.strip()
        target = expand_user_path(raw_spec, env=env) if raw_spec else None

    if target is None:
        return HotwordsTable(path=None, exists=False)

    cache_key = os.path.realpath(str(target))
    try:
        _ensure_hotwords_parent_dirs(target, env=env, create=False)
    except ConfigError as exc:
        _evict_cache_key(cache_key, use_cache=use_cache)
        return HotwordsTable(path=target, exists=False, file_warnings=(str(exc),))

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(target), os.O_RDONLY | nofollow)
    except FileNotFoundError:
        _evict_cache_key(cache_key, use_cache=use_cache)
        return HotwordsTable(path=target, exists=False)
    except OSError as exc:
        is_symlink = False
        try:
            is_symlink = stat.S_ISLNK(os.lstat(target).st_mode)
        except OSError:
            pass
        _evict_cache_key(cache_key, use_cache=use_cache)
        if is_symlink:
            warn = (
                f"refusing to read hotwords through symlink {target}; "
                "symlink the hotwords/ directory instead"
            )
            return HotwordsTable(path=target, exists=False, file_warnings=(warn,))
        return HotwordsTable(
            path=target,
            exists=False,
            file_warnings=(f"cannot open hotwords file {target}: {exc}",),
        )

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            _evict_cache_key(cache_key, use_cache=use_cache)
            return HotwordsTable(path=target, exists=False)

        sig = (st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino, st.st_dev)
        if use_cache:
            with _HOTWORDS_CACHE_LOCK:
                cached = _HOTWORDS_CACHE.get(cache_key)
                if cached is not None and cached[0] == sig:
                    if strict_encoding and cached[1].encoding_error:
                        raise ConfigError(cached[1].encoding_error)
                    return cached[1]

        try:
            raw_bytes = _read_bounded_fd(fd, HOTWORDS_MAX_BYTES + 1)
        except OSError as exc:
            return HotwordsTable(
                path=target,
                exists=False,
                file_warnings=(f"cannot read hotwords file {target}: {exc}",),
            )
    finally:
        try:
            os.close(fd)
        except OSError:  # pragma: no cover - defensive close cleanup
            pass

    size_limit_exceeded = len(raw_bytes) > HOTWORDS_MAX_BYTES
    decode_bytes = raw_bytes
    if size_limit_exceeded:
        prefix = raw_bytes[:HOTWORDS_MAX_BYTES]
        if b"\x00" in prefix:
            decode_bytes = prefix
        else:
            last_nl = prefix.rfind(b"\n")
            decode_bytes = prefix[: last_nl + 1] if last_nl >= 0 else b""

    try:
        raw, _ = _decode_hotwords_bytes(target, decode_bytes)
    except _BadHotwordsEncodingError as exc:
        err_msg = str(exc)
        if strict_encoding:
            raise ConfigError(err_msg) from None
        _warn_bad_encoding_once(cache_key, sig, err_msg)
        bad_table = HotwordsTable(
            path=target,
            exists=True,
            encoding_error=err_msg,
            size_limit_exceeded=size_limit_exceeded,
        )
        if use_cache:
            _store_cache_entry(cache_key, sig, bad_table)
        return bad_table

    table = parse_hotwords_text(
        raw,
        path=target,
        exists=True,
        size_limit_exceeded=size_limit_exceeded,
    )
    with _HOTWORDS_CACHE_LOCK:
        _HOTWORDS_BAD_ENCODING_WARNED.pop(cache_key, None)
    if use_cache:
        _store_cache_entry(cache_key, sig, table)
    return table


def _inspect_writable_target(target: Path) -> os.stat_result | None:
    """Ensure ``target`` is absent or a regular non-symlink file."""
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ConfigError(f"cannot inspect hotwords file {target}: {exc}") from None
    if stat.S_ISLNK(st.st_mode):
        raise ConfigError(
            f"refusing to write hotwords through symlink {target}; symlink the hotwords/ directory instead"
        )
    if stat.S_ISDIR(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise ConfigError(f"unsafe hotwords target (not a regular file): {target}")
    return st


@contextmanager
def _hotwords_edit_lock(
    target: Path,
    *,
    env: Mapping[str, str] | None = None,
) -> Iterator[None]:
    """Serialize read-modify-write edits via ``flock(LOCK_EX)`` on the resolved parent directory fd."""
    _ensure_hotwords_parent_dirs(target, env=env, create=True)
    real_parent = os.path.realpath(str(target.parent))
    o_dir = getattr(os, "O_DIRECTORY", 0)
    try:
        dfd = os.open(real_parent, os.O_RDONLY | o_dir)
    except OSError as exc:
        raise ConfigError(f"cannot open hotwords directory for locking {real_parent}: {exc}") from None
    try:
        fcntl.flock(dfd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            try:
                fcntl.flock(dfd, fcntl.LOCK_UN)
            except OSError:  # pragma: no cover - defensive unlock cleanup
                pass
    finally:
        try:
            os.close(dfd)
        except OSError:  # pragma: no cover - defensive close cleanup
            pass


def _detect_eol(raw_lines: Sequence[str]) -> str:
    crlf = sum(1 for ln in raw_lines if ln.endswith("\r\n"))
    lf = sum(1 for ln in raw_lines if ln.endswith("\n") and not ln.endswith("\r\n"))
    return "\r\n" if crlf > lf else "\n"


def _read_full_for_edit(
    target: Path,
) -> tuple[str, bool, str, HotwordsTable, dict[str, tuple[str, ...]]]:
    """Read and validate the full ``target`` under edit lock; refuse oversized, non-UTF-8, or conflicted files."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(target), os.O_RDONLY | nofollow)
    except OSError as exc:
        raise ConfigError(f"cannot open hotwords file {target} for editing: {exc}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):  # pragma: no cover - pre-checked by _inspect_writable_target
            raise ConfigError(f"unsafe hotwords target (not a regular file): {target}")
        raw_bytes = _read_bounded_fd(fd, HOTWORDS_MAX_BYTES + 1)
    finally:
        try:
            os.close(fd)
        except OSError:  # pragma: no cover - defensive close cleanup
            pass

    if len(raw_bytes) > HOTWORDS_MAX_BYTES:
        raise ConfigError(
            f"hotwords file {target} exceeds the {HOTWORDS_MAX_BYTES // 1024} KiB edit limit; "
            "trim the file manually before using CLI edits"
        )
    try:
        text, has_bom = _decode_hotwords_bytes(target, raw_bytes)
    except _BadHotwordsEncodingError as exc:
        raise ConfigError(str(exc)) from None

    raw_lines = _split_physical_lines(text, keepends=True)
    eol = _detect_eol(raw_lines)
    table, raw_alias_owners = _parse_hotwords_core(text, path=target, exists=True)
    if table.conflict_lines:
        raise ConfigError(
            f"hotwords file {target} contains unresolved git conflict block at line "
            f"{table.conflict_lines[0]}; resolve conflicts manually before editing"
        )
    if table.entry_limit_exceeded:
        raise ConfigError(
            f"hotwords file {target} exceeds the {HOTWORDS_MAX_ENTRIES} entry edit limit "
            f"({table.total_valid_entries} entries); trim the file manually before editing"
        )
    return text, has_bom, eol, table, raw_alias_owners


def _atomic_write_bytes_0600(
    target: Path,
    payload: bytes,
    *,
    env: Mapping[str, str] | None = None,
) -> None:
    """Atomically write raw ``payload`` bytes to ``target`` via same-dir temp file + ``0600`` + ``os.replace``."""
    if len(payload) > HOTWORDS_MAX_BYTES:
        raise ConfigError(
            f"edited hotwords file {target} would exceed the {HOTWORDS_MAX_BYTES // 1024} KiB limit "
            f"({len(payload)} bytes)"
        )
    _inspect_writable_target(target)
    _ensure_hotwords_parent_dirs(target, env=env, create=True)

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    tmp_path = target.parent / f".{target.name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600)
    closed = False
    try:
        os.fchmod(fd, 0o600)
        fobj = os.fdopen(fd, "wb")
        closed = True
        with fobj as f:
            f.write(payload)
            f.flush()
        os.replace(tmp_path, target)
    except Exception:
        if not closed:
            try:
                os.close(fd)
            except OSError:  # pragma: no cover - defensive fd cleanup on write error
                pass
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - defensive temp cleanup on write error
            pass
        raise

    cache_key = os.path.realpath(str(target))
    with _HOTWORDS_CACHE_LOCK:
        _HOTWORDS_CACHE.pop(cache_key, None)


def _split_line_body_and_eol(raw_line: str) -> tuple[str, str]:
    if raw_line.endswith("\r\n"):
        return raw_line[:-2], "\r\n"
    if raw_line.endswith("\n"):
        return raw_line[:-1], "\n"
    return raw_line, ""


def _rewrite_raw_line_with_entry(raw_line: str, entry: HotwordEntry) -> str:
    """Replace the entry portion of ``raw_line`` while preserving leading whitespace, inline comment, and EOL."""
    body, line_eol = _split_line_body_and_eol(raw_line)
    leading_len = len(body) - len(body.lstrip(" "))
    leading_ws = body[:leading_len]
    content = body[leading_len:]
    m = _INLINE_COMMENT_RE.search(content)
    comment_suffix = content[m.start() :] if m is not None else ""
    return f"{leading_ws}{entry.format_line()}{comment_suffix}{line_eol}"


def _pin_raw_line_in_place(raw_line: str) -> str:
    """Add a leading ``!`` pin prefix while preserving the rest of the raw line verbatim."""
    body, line_eol = _split_line_body_and_eol(raw_line)
    leading_len = len(body) - len(body.lstrip(" "))
    leading_ws = body[:leading_len]
    content = body[leading_len:]
    if content.startswith("!"):
        return raw_line
    return f"{leading_ws}!{content}{line_eol}"


def _parse_valid_edit_line(raw_line: str) -> tuple[str, tuple[str, ...], bool] | None:
    """Parse a physical line using the same validity rules as the reader (returns None if non-entry/invalid)."""
    body, _ = _split_line_body_and_eol(raw_line)
    if any(ch in _UNICODE_LINE_SEPS for ch in body):
        return None
    cleaned = _strip_line_comment(body)
    if not cleaned or _parse_section_header(cleaned) is not None:
        return None
    try:
        return _parse_entry_cleaned_line(cleaned)
    except ValueError:
        return None


def _collect_raw_aliases_for_term(raw_lines: Sequence[str], term_low: str) -> set[str]:
    """Collect all lowercase raw aliases declared across valid lines for ``term_low`` (incl. hidden/ambiguous/>5)."""
    found: set[str] = set()
    for raw_line in raw_lines:
        parsed = _parse_valid_edit_line(raw_line)
        if parsed is not None and parsed[0].lower() == term_low:
            for al in parsed[1]:
                found.add(al.lower())
    return found


def _update_entry_lines_flat(
    raw_lines: list[str],
    *,
    pin_only_lows: set[str] | None = None,
    alias_append_by_low: Mapping[str, tuple[tuple[str, ...], bool]] | None = None,
) -> list[str]:
    """Update the first matching valid canonical line in place without dropping hidden/ambiguous/>5 raw aliases."""
    rem_pin = set(pin_only_lows or ())
    rem_alias = dict(alias_append_by_low or {})
    out: list[str] = []
    for raw_line in raw_lines:
        if rem_pin or rem_alias:
            parsed = _parse_valid_edit_line(raw_line)
            if parsed is not None:
                canon_term, line_raw_aliases, line_pinned = parsed
                low = canon_term.lower()
                if low in rem_alias:
                    new_aliases, add_pin = rem_alias.pop(low)
                    merged_aliases: list[str] = []
                    seen_al: set[str] = set()
                    for al in (*line_raw_aliases, *new_aliases):
                        al_low = al.lower()
                        if al_low not in seen_al:
                            seen_al.add(al_low)
                            merged_aliases.append(al)
                    merged_entry = HotwordEntry(
                        term=canon_term,
                        aliases=tuple(merged_aliases),
                        pinned=(line_pinned or add_pin),
                    )
                    out.append(_rewrite_raw_line_with_entry(raw_line, merged_entry))
                    continue
                if low in rem_pin:
                    rem_pin.remove(low)
                    out.append(_pin_raw_line_in_place(raw_line))
                    continue
        out.append(raw_line)
    return out


def add_hotwords(
    path: str | Path,
    terms: Sequence[str],
    *,
    aliases: Sequence[str] = (),
    pin: bool = False,
    env: Mapping[str, str] | None = None,
) -> HotwordsEditResult:
    """Add or update hotwords in flat ``personal.txt`` with directory-fd ``flock`` & bidirectional checks."""
    clean_terms = dedupe_hotwords_ci([validate_hotword_term(t) for t in terms])
    has_aliases = bool(aliases)
    if has_aliases and len(clean_terms) != 1:
        raise ValueError("mishearing aliases can only be specified when adding a single hotword term")

    target = Path(path)
    _inspect_writable_target(target)

    if not clean_terms:
        return HotwordsEditResult(path=target, changed=False)

    # Fail fast on alias syntax / length / >5 / self-conflict before acquiring lock.
    pre_aliases: tuple[str, ...] = ()
    pre_warnings: tuple[str, ...] = ()
    if has_aliases:
        pre_aliases, pre_warnings = validate_hotword_aliases(clean_terms[0], aliases)

    with _hotwords_edit_lock(target, env=env):
        st = _inspect_writable_target(target)
        if st is not None:
            existing_text, has_bom, eol, table, raw_alias_owners = _read_full_for_edit(target)
        else:
            existing_text, has_bom, eol = "", False, "\n"
            table = HotwordsTable(path=target, exists=False)
            raw_alias_owners = {}

        existing_entries = {e.term.lower(): e for e in table.entries}
        all_canonical_lows = set(existing_entries)

        # Bidirectional check 1: new canonical term must not collide with any existing alias (incl. hidden ambiguous)
        for term in clean_terms:
            owners = raw_alias_owners.get(term.lower())
            if owners:
                other_owners = [o for o in owners if o.lower() != term.lower()]
                if other_owners:
                    raise ValueError(
                        f"hotword {term!r} conflicts with existing mishearing alias of {other_owners[0]!r}"
                    )

        raw_lines = _split_physical_lines(existing_text, keepends=True) if existing_text else []
        pin_only_lows: set[str] = set()

        if has_aliases:
            term = clean_terms[0]
            term_low = term.lower()
            other_canonical_lows = all_canonical_lows - {term_low}
            clean_aliases, warnings = validate_hotword_aliases(
                term,
                pre_aliases,
                existing_canonical_lows=other_canonical_lows,
            )
            # Bidirectional check 2: new aliases must not collide with any other entry's aliases (incl. hidden)
            for al in clean_aliases:
                owners = raw_alias_owners.get(al.lower())
                if owners:
                    other_owners = [o for o in owners if o.lower() != term_low]
                    if other_owners:
                        raise ValueError(
                            f"mishearing alias {al!r} conflicts with existing mishearing alias of {other_owners[0]!r}"
                        )

            existing_entry = existing_entries.get(term_low)
            if existing_entry is not None:
                existing_raw_al_lows = _collect_raw_aliases_for_term(raw_lines, term_low)
                new_aliases = tuple(a for a in clean_aliases if a.lower() not in existing_raw_al_lows)
                pin_upgrade = bool(pin and not existing_entry.pinned)
                if not new_aliases and not pin_upgrade:
                    return HotwordsEditResult(
                        path=target,
                        skipped=(term,),
                        changed=False,
                        warnings=warnings,
                    )
                if new_aliases:
                    total_alias_count = len(existing_raw_al_lows) + len(new_aliases)
                    if total_alias_count > MAX_HOTWORD_ALIASES:
                        raise ValueError(
                            f"too many mishearing aliases for {existing_entry.term!r}: "
                            f"{total_alias_count} exceeds maximum of {MAX_HOTWORD_ALIASES}"
                        )
                    new_raw_lines = _update_entry_lines_flat(
                        raw_lines,
                        alias_append_by_low={term_low: (new_aliases, pin)},
                    )
                else:
                    new_raw_lines = _update_entry_lines_flat(raw_lines, pin_only_lows={term_low})
                payload = (b"\xef\xbb\xbf" if has_bom else b"") + "".join(new_raw_lines).encode("utf-8")
                _atomic_write_bytes_0600(target, payload, env=env)
                return HotwordsEditResult(
                    path=target,
                    updated=(existing_entry.term,),
                    aliases_added=new_aliases,
                    pinned=(existing_entry.term,) if pin_upgrade else (),
                    changed=True,
                    warnings=warnings,
                )

            if len(existing_entries) + 1 > HOTWORDS_MAX_ENTRIES:
                raise ConfigError(
                    f"adding hotwords would exceed the {HOTWORDS_MAX_ENTRIES} entry limit"
                )
            new_entry = HotwordEntry(term=term, aliases=clean_aliases, pinned=pin)
            added_lines = [new_entry.format_line()]
            added = [term]
            updated_terms: list[str] = []
            skipped: list[str] = []
            aliases_added = clean_aliases
            pinned_terms = [term] if pin else []
        else:
            warnings = pre_warnings
            aliases_added = ()
            added = []
            updated_terms = []
            skipped = []
            pinned_terms = []
            for term in clean_terms:
                term_low = term.lower()
                existing_entry = existing_entries.get(term_low)
                if existing_entry is not None:
                    if pin and not existing_entry.pinned:
                        pin_only_lows.add(term_low)
                        updated_terms.append(existing_entry.term)
                        pinned_terms.append(existing_entry.term)
                    else:
                        skipped.append(term)
                else:
                    added.append(term)
                    if pin:
                        pinned_terms.append(term)
            if not added and not updated_terms:
                return HotwordsEditResult(
                    path=target,
                    skipped=tuple(skipped),
                    changed=False,
                )
            if len(existing_entries) + len(added) > HOTWORDS_MAX_ENTRIES:
                raise ConfigError(
                    f"adding {len(added)} hotword(s) would exceed the {HOTWORDS_MAX_ENTRIES} entry limit"
                )
            added_lines = [HotwordEntry(term=t, pinned=pin).format_line() for t in added]

        new_raw_lines = (
            _update_entry_lines_flat(raw_lines, pin_only_lows=pin_only_lows)
            if pin_only_lows
            else list(raw_lines)
        )
        if added_lines:
            if new_raw_lines and not new_raw_lines[-1].endswith("\n"):
                new_raw_lines[-1] = new_raw_lines[-1] + eol
            new_raw_lines.extend(f"{ln}{eol}" for ln in added_lines)

        payload = (b"\xef\xbb\xbf" if has_bom else b"") + "".join(new_raw_lines).encode("utf-8")
        _atomic_write_bytes_0600(target, payload, env=env)
        return HotwordsEditResult(
            path=target,
            added=tuple(added),
            updated=tuple(updated_terms),
            skipped=tuple(skipped),
            changed=True,
            aliases_added=aliases_added,
            pinned=tuple(pinned_terms),
            warnings=warnings,
        )


def add_hotword(
    path: str | Path,
    term: str,
    *,
    aliases: Sequence[str] = (),
    pin: bool = False,
    env: Mapping[str, str] | None = None,
) -> HotwordsEditResult:
    """Add or update a single hotword in flat ``personal.txt``."""
    return add_hotwords(path, [term], aliases=aliases, pin=pin, env=env)


def remove_hotwords(
    path: str | Path,
    terms: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
) -> HotwordsEditResult:
    """Remove all matching canonical hotword lines (by ``.lower()`` key) from flat ``personal.txt``."""
    clean_terms = dedupe_hotwords_ci([validate_hotword_term(t) for t in terms])
    target = Path(path)
    st_pre = _inspect_writable_target(target)

    if not clean_terms or st_pre is None:
        _ensure_hotwords_parent_dirs(target, env=env, create=False)
        return HotwordsEditResult(
            path=target,
            removed=(),
            not_found=tuple(clean_terms),
            changed=False,
        )

    with _hotwords_edit_lock(target, env=env):
        st = _inspect_writable_target(target)
        if st is None:
            return HotwordsEditResult(
                path=target,
                removed=(),
                not_found=tuple(clean_terms),
                changed=False,
            )
        existing_text, has_bom, _, _, _ = _read_full_for_edit(target)
        target_lows = {t.lower() for t in clean_terms}
        removed_map: dict[str, str] = {}

        kept_raw_lines: list[str] = []
        changed = False

        for raw_line in _split_physical_lines(existing_text, keepends=True):
            parsed = _parse_valid_edit_line(raw_line)
            if parsed is not None:
                canon_term = parsed[0]
                if (low := canon_term.lower()) in target_lows:
                    removed_map.setdefault(low, canon_term)
                    changed = True
                    continue
            kept_raw_lines.append(raw_line)

        removed = tuple(removed_map[t.lower()] for t in clean_terms if t.lower() in removed_map)
        not_found = tuple(t for t in clean_terms if t.lower() not in removed_map)

        if changed:
            payload = (b"\xef\xbb\xbf" if has_bom else b"") + "".join(kept_raw_lines).encode("utf-8")
            _atomic_write_bytes_0600(target, payload, env=env)

        return HotwordsEditResult(
            path=target,
            removed=removed,
            not_found=not_found,
            changed=changed,
        )


def remove_hotword(
    path: str | Path,
    term: str,
    *,
    env: Mapping[str, str] | None = None,
) -> HotwordsEditResult:
    """Remove a single hotword (and all duplicate lines matching its ``.lower()`` key)."""
    return remove_hotwords(path, [term], env=env)


def sanitize_openai_keyword(word: str) -> str:
    """Strip characters rejected by OpenAI ``keywords[]`` (``<``, ``>``, ``\\r``, ``\\n``, control chars)."""
    if not isinstance(word, str):
        return ""
    s = _OPENAI_KEYWORD_FORBIDDEN_RE.sub("", word)
    s = _CTRL_CHAR_RE.sub(" ", s)
    for ch in _UNICODE_LINE_SEPS:
        if ch in s:
            s = s.replace(ch, " ")
    return " ".join(s.split())


def resolve_hotwords_quota_spec(
    provider: str,
    *,
    model: str = "",
    api: str = "",
    base_url: str = "",
) -> HotwordsQuotaSpec:
    """Resolve the exact hotwords quota specification for an ASR provider/model/protocol route."""
    raw = (provider or "").strip().lower()
    prov = ASR_PROVIDER_ALIASES.get(raw, raw)

    if prov == "doubao":
        return HOTWORDS_QUOTA_SPECS["doubao"]
    if prov == "deepseek":
        return HOTWORDS_QUOTA_SPECS["deepseek_asr_unsupported"]
    if prov in ("whisper", "stt", "audio-transcriptions"):
        return HOTWORDS_QUOTA_SPECS["whisper"]
    if prov in ("single_hop", "single-hop", "chat-completions"):
        return HOTWORDS_QUOTA_SPECS["single_hop"]
    if prov == "gemini":
        if (
            base_url.strip()
            and not is_gemini_native_url(base_url)
            and (api or "").strip().lower() == "audio-transcriptions"
        ):
            return HOTWORDS_QUOTA_SPECS["whisper"]
        return HOTWORDS_QUOTA_SPECS["single_hop"]
    if prov == "dashscope":
        eff_model = model.strip() or DEFAULT_ASR_MODELS["dashscope"]
        if is_dashscope_omni_chat_model(eff_model, base_url):
            return HOTWORDS_QUOTA_SPECS["single_hop"]
        return HOTWORDS_QUOTA_SPECS["dashscope"]
    if prov == "openrouter":
        eff_model = model.strip() or DEFAULT_ASR_MODELS["openrouter"]
        eff_api = resolve_asr_api("openrouter", eff_model, api or "auto", base_url)
        if eff_api == "chat-completions":
            return HOTWORDS_QUOTA_SPECS["single_hop"]
        if eff_model.lower().split(":", 1)[0] in _OPENROUTER_KEYTERMS_MODELS:
            return HOTWORDS_QUOTA_SPECS["openrouter_keyterms"]
        return HOTWORDS_QUOTA_SPECS["openrouter_stt_unsupported"]
    if prov == "openai":
        eff_model = model.strip() or DEFAULT_ASR_MODELS["openai"]
        eff_api = resolve_asr_api("openai", eff_model, api or "auto", base_url)
        if eff_api == "chat-completions":
            return HOTWORDS_QUOTA_SPECS["single_hop"]
        low_model = eff_model.lower()
        if "whisper" in low_model or low_model.startswith("gpt-4o-"):
            return HOTWORDS_QUOTA_SPECS["whisper"]
        return HOTWORDS_QUOTA_SPECS["openai_keywords"]
    if prov in ("groq", "siliconflow", "openai-compatible"):
        eff_model = model.strip() or DEFAULT_ASR_MODELS.get(prov, "")
        eff_api = resolve_asr_api(prov, eff_model, api or "auto", base_url)
        if eff_api == "chat-completions":
            return HOTWORDS_QUOTA_SPECS["single_hop"]
        return HOTWORDS_QUOTA_SPECS["whisper"]
    return HOTWORDS_QUOTA_SPECS["single_hop"]


def _normalize_stage1_candidate(
    raw_w: str,
    spec: HotwordsQuotaSpec,
    canonical_by_low: Mapping[str, str],
) -> str | None:
    """Normalize a single Stage 1 candidate (OpenAI sanitization + Doubao file-extension stem collapse)."""
    w = raw_w
    if spec.sanitize_openai_keywords:
        w = sanitize_openai_keyword(w)
        if not w:
            return None
    if spec.collapse_file_ext and "." in w:
        stem, ext = w.rsplit(".", 1)
        if len(stem) >= 3 and ext.isalnum() and stem.lower() in canonical_by_low:
            w = canonical_by_low[stem.lower()]
    return w


def budget_hotwords_for_provider(
    terms: Sequence[str],
    provider: str,
    *,
    model: str = "",
    api: str = "",
    base_url: str = "",
    max_terms: int | None = None,
    max_chars: int | None = None,
) -> HotwordsBudgetResult:
    """Select priority-ordered (head-first) whole hotwords within ``provider``'s exact term & char quota."""
    spec = resolve_hotwords_quota_spec(provider, model=model, api=api, base_url=base_url)
    eff_max_terms = spec.max_terms if max_terms is None else max(0, int(max_terms))
    eff_max_chars = spec.max_chars if max_chars is None else max(0, int(max_chars))
    cleaned_words = dedupe_hotwords_ci(terms)

    canonical_by_low: dict[str, str] = {}
    if spec.collapse_file_ext:
        for w in cleaned_words:
            canonical_by_low.setdefault(w.lower(), w)

    kept: list[str] = []
    dropped: list[str] = []
    used_chars = 0
    seen_out_low: set[str] = set()
    sep_len = len(spec.separator)

    for raw_w in cleaned_words:
        w = _normalize_stage1_candidate(raw_w, spec, canonical_by_low)
        if w is None:
            dropped.append(raw_w)
            continue
        w_low = w.lower()
        if w_low in seen_out_low:
            continue
        seen_out_low.add(w_low)

        if eff_max_terms <= 0 or len(kept) >= eff_max_terms:
            dropped.append(w)
            continue

        if spec.max_term_chars is not None and len(w) > spec.max_term_chars:
            dropped.append(w)
            continue

        add_len = len(w) + (sep_len if kept else 0)
        if eff_max_chars is not None and (eff_max_chars <= 0 or used_chars + add_len > eff_max_chars):
            dropped.append(w)
            continue

        kept.append(w)
        used_chars += add_len

    disp_provider = (provider or spec.label).strip().lower() or spec.label
    return HotwordsBudgetResult(
        provider=disp_provider,
        spec_key=spec.key,
        label=spec.label,
        kept=tuple(kept),
        dropped=tuple(dropped),
        total_input=len(kept) + len(dropped),
        used_chars=used_chars,
        max_terms=eff_max_terms,
        max_chars=eff_max_chars,
        note=spec.note,
    )


def project_stage1_hotwords(
    ctx: Any,
    provider: str,
    *,
    model: str = "",
    api: str = "",
    base_url: str = "",
    max_terms: int | None = None,
    max_chars: int | None = None,
) -> HotwordsBudgetResult:
    """Project three-bucket ``AsrContext`` hotwords for Stage 1 ASR with dynamic reserve & overflow backfill.

    When ``ctx.hotwords_bucketed`` is False, falls back to flat ``budget_hotwords_for_provider(ctx.hotwords, ...)``.
    When True:
    - ``mode == "general"``: 100% of quota goes to ``personal_hotwords`` (``dynamic`` and ``builtin`` excluded).
    - ``mode == "agent"``: reserves ``ceil(30%)`` terms for ``dynamic_hotwords`` (0 if ``eff_max_terms < 3``)
      and caps Pass 1 personal chars at 70%, then backfills remaining slots/chars via
      ``personal_overflow -> dynamic_overflow -> builtin``.
    All three passes share one sanitizer, one Doubao stem index, one global ``seen`` set, and one budget counter.
    """
    if not bool(getattr(ctx, "hotwords_bucketed", False)):
        return budget_hotwords_for_provider(
            getattr(ctx, "hotwords", ()),
            provider,
            model=model,
            api=api,
            base_url=base_url,
            max_terms=max_terms,
            max_chars=max_chars,
        )

    spec = resolve_hotwords_quota_spec(provider, model=model, api=api, base_url=base_url)
    eff_max_terms = spec.max_terms if max_terms is None else max(0, int(max_terms))
    eff_max_chars = spec.max_chars if max_chars is None else max(0, int(max_chars))
    disp_provider = (provider or spec.label).strip().lower() or spec.label
    mode = (str(getattr(ctx, "mode", "agent") or "agent")).strip().lower()

    p_clean = dedupe_hotwords_ci(getattr(ctx, "personal_hotwords", ()) or ())
    if mode == "general":
        flat = budget_hotwords_for_provider(
            p_clean,
            provider,
            model=model,
            api=api,
            base_url=base_url,
            max_terms=max_terms,
            max_chars=max_chars,
        )
        return HotwordsBudgetResult(
            provider=flat.provider,
            spec_key=flat.spec_key,
            label=flat.label,
            kept=flat.kept,
            dropped=flat.dropped,
            total_input=flat.total_input,
            used_chars=flat.used_chars,
            max_terms=flat.max_terms,
            max_chars=flat.max_chars,
            note=flat.note,
            personal_kept=flat.kept_count,
            dynamic_kept=0,
            builtin_kept=0,
            personal_dropped=flat.dropped_count,
        )

    d_clean = dedupe_hotwords_ci(getattr(ctx, "dynamic_hotwords", ()) or ())
    b_clean = dedupe_hotwords_ci(getattr(ctx, "builtin_hotwords", ()) or ())

    # Single shared Doubao stem-collapse table across all active buckets in priority order
    canonical_by_low: dict[str, str] = {}
    if spec.collapse_file_ext:
        for w in (*p_clean, *d_clean, *b_clean):
            canonical_by_low.setdefault(w.lower(), w)

    dyn_reserve = 0 if eff_max_terms < 3 else (eff_max_terms * 3 + 9) // 10
    p_term_cap = max(0, eff_max_terms - dyn_reserve)
    p_char_cap = int(eff_max_chars * 0.70) if eff_max_chars is not None else None

    kept: list[str] = []
    dropped: list[str] = []
    used_chars = 0
    seen_out_low: set[str] = set()
    sep_len = len(spec.separator)
    personal_kept = 0
    dynamic_kept = 0
    builtin_kept = 0
    personal_dropped = 0

    def _hard_reject(w: str) -> bool:
        if eff_max_terms <= 0 or (eff_max_chars is not None and eff_max_chars <= 0):
            return True
        if spec.max_term_chars is not None and len(w) > spec.max_term_chars:
            return True
        return False

    # Pass 1: personal primary quota (<= eff_max_terms - dyn_reserve, <= 70% chars)
    personal_overflow: list[str] = []
    p_overflow_low: set[str] = set()
    for raw_w in p_clean:
        w = _normalize_stage1_candidate(raw_w, spec, canonical_by_low)
        if w is None:
            dropped.append(raw_w)
            personal_dropped += 1
            continue
        w_low = w.lower()
        if w_low in seen_out_low or w_low in p_overflow_low:
            continue
        if _hard_reject(w):
            seen_out_low.add(w_low)
            dropped.append(w)
            personal_dropped += 1
            continue
        add_len = len(w) + (sep_len if kept else 0)
        fits_terms = personal_kept < p_term_cap and len(kept) < eff_max_terms
        fits_chars = p_char_cap is None or used_chars + add_len <= p_char_cap
        if fits_terms and fits_chars:
            kept.append(w)
            used_chars += add_len
            seen_out_low.add(w_low)
            personal_kept += 1
        else:
            p_overflow_low.add(w_low)
            personal_overflow.append(w)

    # Pass 2: dynamic reserve (<= dyn_reserve terms, bounded by ALL remaining chars)
    dynamic_overflow: list[str] = []
    d_overflow_low: set[str] = set()
    for raw_w in d_clean:
        w = _normalize_stage1_candidate(raw_w, spec, canonical_by_low)
        if w is None:
            dropped.append(raw_w)
            continue
        w_low = w.lower()
        if w_low in seen_out_low or w_low in d_overflow_low:
            continue
        if _hard_reject(w):
            seen_out_low.add(w_low)
            dropped.append(w)
            continue
        add_len = len(w) + (sep_len if kept else 0)
        fits_terms = dynamic_kept < dyn_reserve and len(kept) < eff_max_terms
        fits_chars = eff_max_chars is None or used_chars + add_len <= eff_max_chars
        if fits_terms and fits_chars:
            kept.append(w)
            used_chars += add_len
            seen_out_low.add(w_low)
            dynamic_kept += 1
        else:
            d_overflow_low.add(w_low)
            dynamic_overflow.append(w)

    # Pass 3: backfill remaining slots/chars via personal_overflow -> dynamic_overflow -> builtin
    backfill_groups: tuple[tuple[str, Sequence[str], bool], ...] = (
        ("personal", personal_overflow, True),
        ("dynamic", dynamic_overflow, True),
        ("builtin", b_clean, False),
    )
    for bucket_name, items, pre_normalized in backfill_groups:
        for raw_w in items:
            if pre_normalized:
                w: str | None = raw_w
            else:
                w = _normalize_stage1_candidate(raw_w, spec, canonical_by_low)
                if w is None:
                    dropped.append(raw_w)
                    continue
            w_low = w.lower()
            if w_low in seen_out_low:
                continue
            seen_out_low.add(w_low)

            add_len = len(w) + (sep_len if kept else 0)
            if (
                _hard_reject(w)
                or len(kept) >= eff_max_terms
                or (eff_max_chars is not None and used_chars + add_len > eff_max_chars)
            ):
                dropped.append(w)
                if bucket_name == "personal":
                    personal_dropped += 1
                continue

            kept.append(w)
            used_chars += add_len
            if bucket_name == "personal":
                personal_kept += 1
            elif bucket_name == "dynamic":
                dynamic_kept += 1
            else:
                builtin_kept += 1

    return HotwordsBudgetResult(
        provider=disp_provider,
        spec_key=spec.key,
        label=spec.label,
        kept=tuple(kept),
        dropped=tuple(dropped),
        total_input=len(kept) + len(dropped),
        used_chars=used_chars,
        max_terms=eff_max_terms,
        max_chars=eff_max_chars,
        note=spec.note,
        personal_kept=personal_kept,
        dynamic_kept=dynamic_kept,
        builtin_kept=builtin_kept,
        personal_dropped=personal_dropped,
    )


def budget_openai_keywords(
    terms: Sequence[str],
    *,
    max_terms: int | None = None,
) -> HotwordsBudgetResult:
    """Sanitize and budget canonical hotwords for OpenAI ``gpt-transcribe`` native ``keywords[]``."""
    return budget_hotwords_for_provider(
        terms,
        "openai",
        model="gpt-transcribe",
        max_terms=max_terms,
    )


def _merge_entry_sequences(entry_groups: Sequence[Sequence[HotwordEntry]]) -> list[HotwordEntry]:
    """Merge HotwordEntry sequences in priority order, CI-deduping canonical terms and merging aliases."""
    order: list[str] = []
    terms_by_low: dict[str, str] = {}
    pinned_by_low: dict[str, bool] = {}
    aliases_by_low: dict[str, list[str]] = {}
    alias_lows_by_low: dict[str, set[str]] = {}

    for group in entry_groups:
        for entry in group:
            term = entry.term.strip()
            if not term:
                continue
            low = term.lower()
            if low not in terms_by_low:
                order.append(low)
                terms_by_low[low] = term
                pinned_by_low[low] = entry.pinned
                aliases_by_low[low] = []
                alias_lows_by_low[low] = set()
            elif entry.pinned:
                pinned_by_low[low] = True
            al_list = aliases_by_low[low]
            al_seen = alias_lows_by_low[low]
            for raw_alias in entry.aliases:
                alias = raw_alias.strip()
                if len(alias) < MIN_HOTWORD_ALIAS_CHARS:
                    continue
                al_low = alias.lower()
                if al_low == low or al_low in al_seen:
                    continue
                if len(al_list) >= MAX_HOTWORD_ALIASES:
                    break
                al_seen.add(al_low)
                al_list.append(alias)

    all_canonical_lows = set(terms_by_low)
    merged: list[HotwordEntry] = []
    for low in order:
        clean_aliases = tuple(
            a for a in aliases_by_low[low] if a.lower() not in all_canonical_lows
        )
        merged.append(
            HotwordEntry(
                term=terms_by_low[low],
                aliases=clean_aliases,
                pinned=pinned_by_low[low],
            )
        )
    return merged


def _sanitize_xml_hint_text(text: str) -> str:
    """Strip control/line-break characters and escape XML special characters for ``<vocabulary_hints>``."""
    s = _CTRL_CHAR_RE.sub(" ", text)
    for ch in _UNICODE_LINE_SEPS:
        if ch in s:
            s = s.replace(ch, " ")
    s = " ".join(s.split())
    if not s:
        return ""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _coerce_stage2_entries(entries: Sequence[HotwordEntry | str]) -> list[HotwordEntry]:
    raw_entries: list[HotwordEntry] = []
    for item in entries:
        entry = item if isinstance(item, HotwordEntry) else HotwordEntry(term=str(item))
        if _sanitize_xml_hint_text(entry.term):
            raw_entries.append(entry)
    return _merge_entry_sequences([raw_entries])


def format_stage2_entry_line(entry: HotwordEntry | str) -> str:
    """Format a single Stage 2 vocabulary hint line with XML escaping and descriptive alias text."""
    if isinstance(entry, HotwordEntry):
        term = _sanitize_xml_hint_text(entry.term)
        raw_aliases = entry.aliases
    else:
        term = _sanitize_xml_hint_text(str(entry))
        raw_aliases = ()
    if not term:
        return ""
    term_low = term.lower()
    clean_aliases: list[str] = []
    seen_low: set[str] = set()
    for raw_al in raw_aliases:
        al = _sanitize_xml_hint_text(raw_al)
        if len(al) < MIN_HOTWORD_ALIAS_CHARS:
            continue
        al_low = al.lower()
        if al_low == term_low or al_low in seen_low:
            continue
        seen_low.add(al_low)
        clean_aliases.append(al)
        if len(clean_aliases) >= MAX_HOTWORD_ALIASES:
            break
    if clean_aliases:
        return f"{term} (ASR has misheard as: {', '.join(clean_aliases)})"
    return term


def budget_stage2_hotwords(
    entries: Sequence[HotwordEntry | str],
    *,
    max_items: int = STAGE2_HOTWORDS_MAX_ITEMS,
    max_chars: int = STAGE2_HOTWORDS_MAX_CHARS,
) -> tuple[list[HotwordEntry], list[HotwordEntry], int]:
    """Select Stage 2 hotword entries within item and rendered-body char budgets.

    Skips individual oversized entries without halting evaluation of subsequent shorter entries.
    Returns ``(kept_entries, dropped_entries, used_chars)``.
    """
    eff_max_items = max(0, int(max_items))
    eff_max_chars = max(0, int(max_chars))
    normalized = _coerce_stage2_entries(entries)

    kept: list[HotwordEntry] = []
    dropped: list[HotwordEntry] = []
    used_chars = 0

    for entry in normalized:
        if eff_max_items <= 0 or eff_max_chars <= 0 or len(kept) >= eff_max_items:
            dropped.append(entry)
            continue
        rendered = format_stage2_entry_line(entry)
        add_len = len(rendered) + (1 if kept else 0)
        if used_chars + add_len > eff_max_chars:
            dropped.append(entry)
            continue
        kept.append(entry)
        used_chars += add_len

    return kept, dropped, used_chars


def format_stage2_vocabulary_hints(
    entries: Sequence[HotwordEntry | str],
    *,
    max_items: int = STAGE2_HOTWORDS_MAX_ITEMS,
    max_chars: int = STAGE2_HOTWORDS_MAX_CHARS,
) -> str:
    """Render static manual hotwords into a read-only ``<vocabulary_hints>`` block for Stage 2 / single-hop."""
    kept, _, _ = budget_stage2_hotwords(entries, max_items=max_items, max_chars=max_chars)
    if not kept:
        return ""
    body = "\n".join(format_stage2_entry_line(e) for e in kept)
    return (
        '<vocabulary_hints read_only="true">\n'
        f"{STAGE2_VOCABULARY_HINTS_PREAMBLE}\n"
        f"{body}\n"
        "</vocabulary_hints>"
    )


def summarize_stage2_hotwords_budget(
    doc_or_path: HotwordsTable | str | Path | None,
    *,
    env: Mapping[str, str] | None = None,
    use_cache: bool = True,
    max_items: int = STAGE2_HOTWORDS_MAX_ITEMS,
    max_chars: int = STAGE2_HOTWORDS_MAX_CHARS,
) -> Stage2HotwordsBudgetSummary:
    """Summarize Stage 2 Block 0 vocabulary budget usage for the unified personal hotwords table."""
    if doc_or_path is None:
        table = HotwordsTable()
    elif isinstance(doc_or_path, HotwordsTable):
        table = doc_or_path
    else:
        table = load_hotwords_table(doc_or_path, env=env, use_cache=use_cache)

    eff_max_items = max(0, int(max_items))
    eff_max_chars = max(0, int(max_chars))
    ordered = order_stage2_entries(table.entries)
    kept, dropped, used_chars = budget_stage2_hotwords(
        ordered,
        max_items=eff_max_items,
        max_chars=eff_max_chars,
    )
    return Stage2HotwordsBudgetSummary(
        kept_count=len(kept),
        dropped_count=len(dropped),
        total_input=len(kept) + len(dropped),
        used_chars=used_chars,
        max_items=eff_max_items,
        max_chars=eff_max_chars,
        dropped=tuple(e.term for e in dropped),
        warnings_count=table.warnings_count,
    )


def format_stage2_budget_line(summary: Stage2HotwordsBudgetSummary, *, name: str = "") -> str:
    """Format a compact Stage 2 budget line such as ``stage2: 12/200 items, 240/3000 chars [OK]``."""
    if name:
        label = name
    elif summary.scopes:
        label = f"stage2 ({summary.mode}: {'+'.join(summary.scopes)})"
    else:
        label = "stage2"
    base = f"{label}: {summary.kept_count}/{summary.max_items} items, {summary.used_chars}/{summary.max_chars} chars"
    if summary.truncated:
        preview = ", ".join(summary.dropped[:5])
        more = f", +{summary.dropped_count - 5} more" if summary.dropped_count > 5 else ""
        return f"{base} [TRUNCATED: {summary.dropped_count} dropped: {preview}{more}]"
    return f"{base} [OK]"


def format_budget_line(res: HotwordsBudgetResult, *, name: str = "") -> str:
    """Format a compact human-readable quota line such as ``doubao: 12/50 terms, 68/500 chars [OK]``."""
    label = name or res.provider
    if res.max_terms <= 0:
        tail = f" ({res.note})" if res.note else ""
        if res.dropped_count > 0:
            return f"{label}: 0/0 terms (not passed by this route; {res.dropped_count} manual terms unused){tail}"
        return f"{label}: 0/0 terms (not passed by this route){tail}"
    char_part = f", {res.used_chars}/{res.max_chars} chars" if res.max_chars is not None else ""
    base = f"{label}: {res.kept_count}/{res.max_terms} terms{char_part}"
    if res.truncated:
        preview = ", ".join(res.dropped[:5])
        more = f", +{res.dropped_count - 5} more" if res.dropped_count > 5 else ""
        return f"{base} [TRUNCATED: {res.dropped_count} dropped: {preview}{more}]"
    return f"{base} [OK]"

