"""Layered plain-text ASR hotwords parser, atomic editor, stat cache, and cross-provider quota budgeter."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
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
    expand_user_path,
    is_dashscope_omni_chat_model,
    is_gemini_native_url,
    resolve_asr_api,
)

DEFAULT_HOTWORDS_FILE: str = "~/.config/agent-voice-input/hotwords.txt"
LEGACY_HOTWORDS_FILE: str = "~/.config/voice-input/hotwords.txt"

VALID_HOTWORD_SCOPES: tuple[str, ...] = ("global", "chat", "coding")
VALID_HOTWORD_LIST_SCOPES: tuple[str, ...] = ("all", "global", "chat", "coding")
VALID_HOTWORD_REMOVE_SCOPES: tuple[str, ...] = ("global", "chat", "coding", "all")

MAX_HOTWORD_ALIASES: int = 5
STAGE2_HOTWORDS_MAX_ITEMS: int = 100
STAGE2_HOTWORDS_MAX_CHARS: int = 1500
SINGLE_HOP_HOTWORDS_MAX_ITEMS: int = STAGE2_HOTWORDS_MAX_ITEMS
SINGLE_HOP_HOTWORDS_MAX_CHARS: int = STAGE2_HOTWORDS_MAX_CHARS
STAGE2_VOCABULARY_HINTS_PREAMBLE: str = (
    "Speaker's personal names/terms. Use ONLY to fix a draft word that sounds like an entry "
    "AND clearly means it in context; otherwise ignore. Never insert unspoken terms, never "
    "translate a correctly recognized native word (豆包 stays 豆包), never mention this list."
)

_ALIAS_SEPARATOR = "<-"
_ALIAS_SPLIT_RE = re.compile(r"[,，]")
_OPENAI_KEYWORD_FORBIDDEN_RE = re.compile(r"[<>]")
_CTRL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
_UNICODE_LINE_SEPS = frozenset({"\x85", "\u2028", "\u2029"})
_INLINE_COMMENT_RE = re.compile(r"\s+#")
_SECTION_HEADER_RE = re.compile(r"^\[([^\[\]]+)\]$")
# Verified OpenRouter /audio/transcriptions models that accept top-level keyterms[]
# (see docs/benchmarks/asr-hotwords-and-protocols.md).
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


@dataclass(frozen=True)
class HotwordEntry:
    term: str
    aliases: tuple[str, ...] = ()

    def format_line(self) -> str:
        if self.aliases:
            return f"{self.term} {_ALIAS_SEPARATOR} {', '.join(self.aliases)}"
        return self.term


def _merge_entry_sequences(entry_groups: Sequence[Sequence[HotwordEntry]]) -> list[HotwordEntry]:
    """Merge HotwordEntry sequences in priority order, CI-deduping canonical terms and merging aliases."""
    order: list[str] = []
    terms_by_low: dict[str, str] = {}
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
                aliases_by_low[low] = []
                alias_lows_by_low[low] = set()
            al_list = aliases_by_low[low]
            al_seen = alias_lows_by_low[low]
            for raw_alias in entry.aliases:
                alias = raw_alias.strip()
                if len(alias) <= 1:
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
        merged.append(HotwordEntry(term=terms_by_low[low], aliases=clean_aliases))
    return merged


@dataclass(frozen=True)
class HotwordsTable:
    global_terms: tuple[str, ...] = ()
    chat_terms: tuple[str, ...] = ()
    coding_terms: tuple[str, ...] = ()
    global_entries: tuple[HotwordEntry, ...] = ()
    chat_entries: tuple[HotwordEntry, ...] = ()
    coding_entries: tuple[HotwordEntry, ...] = ()
    path: Path | None = None
    exists: bool = False
    encoding_error: str = ""

    def terms_for_scope(self, scope: str) -> tuple[str, ...]:
        s = normalize_hotword_scope(scope)
        if s == "global":
            return self.global_terms
        if s == "chat":
            return self.chat_terms
        return self.coding_terms

    def entries_for_scope(self, scope: str) -> tuple[HotwordEntry, ...]:
        s = normalize_hotword_scope(scope)
        if s == "global":
            entries, terms = self.global_entries, self.global_terms
        elif s == "chat":
            entries, terms = self.chat_entries, self.chat_terms
        else:
            entries, terms = self.coding_entries, self.coding_terms
        if entries or not terms:
            return entries
        return tuple(HotwordEntry(t) for t in terms)

    def merged_entries_for_scopes(self, scopes: Sequence[str]) -> list[HotwordEntry]:
        return _merge_entry_sequences([self.entries_for_scope(sc) for sc in scopes])

    def merged_for_scene(self, scene: str) -> list[str]:
        """Return priority-ordered deduplicated canonical manual hotwords for ``scene``.

        Active scene terms (``chat`` or ``coding``) precede ``global`` terms, preserving first-seen casing.
        Never includes mishearing aliases (Stage 1 ASR receives canonical terms only).
        """
        s = (scene or "global").strip().lower()
        if s == "all":
            return dedupe_hotwords_ci([*self.global_terms, *self.chat_terms, *self.coding_terms])
        if s == "chat":
            return dedupe_hotwords_ci([*self.chat_terms, *self.global_terms])
        if s == "coding":
            return dedupe_hotwords_ci([*self.coding_terms, *self.global_terms])
        return dedupe_hotwords_ci(self.global_terms)

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "global": list(self.global_terms),
            "chat": list(self.chat_terms),
            "coding": list(self.coding_terms),
        }

    def as_entries_dict(self) -> dict[str, list[dict[str, Any]]]:
        return {
            sc: [
                {"term": e.term, "aliases": list(e.aliases)}
                for e in self.entries_for_scope(sc)
            ]
            for sc in VALID_HOTWORD_SCOPES
        }


@dataclass(frozen=True)
class HotwordsEditResult:
    path: Path
    scope: str
    added: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    not_found: tuple[str, ...] = ()
    changed: bool = False
    updated: tuple[str, ...] = ()
    aliases_added: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


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
        }


@dataclass(frozen=True)
class Stage2HotwordsBudgetSummary:
    mode: str
    scopes: tuple[str, ...]
    kept_count: int
    dropped_count: int
    total_input: int
    used_chars: int
    max_items: int
    max_chars: int
    dropped: tuple[str, ...] = ()

    @property
    def truncated(self) -> bool:
        return self.dropped_count > 0

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
            "truncated": self.truncated,
        }


# Single source of truth for per-provider ASR hotword quotas (aligned with
# docs/benchmarks/asr-hotwords-and-protocols.md protocol measurements):
# - Doubao file API contract supports up to 5,000 terms; recall is sharpest around 30-50 terms.
# - DashScope qwen3-asr-flash hard cap is 10,000 text_tokens (~14,500 chars); 80 terms / 900 chars leaves wide headroom.
# - OpenAI gpt-transcribe supports native keywords[] (50 terms sweet spot; must strip <, >, \r, \n).
# - Whisper /audio/transcriptions reserves 7 chars for "常用技术术语：" (360 - 7 = 353).
# - Single-hop multimodal models align with Stage 2 wide vocabulary hints (100 items / 1,500 chars).
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
        max_terms=80,
        max_chars=900,
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


def normalize_hotword_scope(scope: str, *, allow_all: bool = False) -> str:
    s = (scope or "").strip().lower()
    valid = VALID_HOTWORD_REMOVE_SCOPES if allow_all else VALID_HOTWORD_SCOPES
    if s not in valid:
        raise ValueError(f"invalid hotwords scope {scope!r} (expected one of {', '.join(valid)})")
    return s


def _has_forbidden_line_chars(raw: str) -> bool:
    return bool(
        _CTRL_CHAR_RE.search(raw)
        or any(ch in _UNICODE_LINE_SEPS for ch in raw)
        or len(raw.splitlines()) > 1
    )


def validate_hotword_term(raw: str) -> str:
    """Validate and normalize a single hotword term so it safely round-trips in plain-text ``hotwords.txt``."""
    if not isinstance(raw, str):
        raise ValueError(f"hotword must be a string, got {type(raw).__name__}")
    if _has_forbidden_line_chars(raw):
        raise ValueError(f"hotword contains newline or control characters: {raw!r}")
    term = raw.strip()
    if not term:
        raise ValueError("hotword must not be empty")
    if term.startswith("[") and term.endswith("]"):
        raise ValueError(f"hotword {term!r} conflicts with section header syntax '[scope]'")
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
    """Validate and normalize a single mishearing alias (rejects empty, control chars, separators, and 1-char aliases)."""
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
    if len(alias) <= 1:
        raise ValueError(
            f"mishearing alias {alias!r} is too short (single-character aliases are rejected to prevent false replacements)"
        )
    return alias


def validate_hotword_aliases(
    term: str,
    aliases: Sequence[str],
    *,
    existing_canonical_lows: set[str] | frozenset[str] = frozenset(),
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Validate, split (by ',' or '，'), deduplicate, and check guardrails for mishearing aliases.

    Returns ``(clean_aliases, warnings)``. Raises ``ValueError`` on empty aliases, single-char aliases,
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
    """
    stripped = raw_line.strip()
    if not stripped or stripped.startswith("#"):
        return ""
    m = _INLINE_COMMENT_RE.search(stripped)
    if m is not None:
        return stripped[: m.start()].strip()
    return stripped


def _parse_section_header(cleaned_line: str) -> str | None:
    """Return lowercase section name if ``cleaned_line`` is ``[name]``, else ``None``."""
    m = _SECTION_HEADER_RE.match(cleaned_line)
    if m is None:
        return None
    return m.group(1).strip().lower()


def _parse_entry_cleaned_line(cleaned: str) -> tuple[str, tuple[str, ...]] | None:
    """Parse a comment-stripped non-header line into ``(canonical_term, valid_aliases)``.

    Supports both legacy ``Term`` lines and optional ``Term <- alias1, alias2`` (',' or '，').
    Returns ``None`` if the canonical term on the left side is empty.
    """
    if _ALIAS_SEPARATOR not in cleaned:
        return cleaned, ()
    left, right = cleaned.split(_ALIAS_SEPARATOR, 1)
    term = left.strip()
    if not term:
        return None
    term_low = term.lower()
    aliases: list[str] = []
    seen_low: set[str] = set()
    for piece in _ALIAS_SPLIT_RE.split(right):
        al = piece.strip()
        if len(al) <= 1 or _ALIAS_SEPARATOR in al:
            continue
        al_low = al.lower()
        if al_low == term_low or al_low in seen_low:
            continue
        if len(aliases) >= MAX_HOTWORD_ALIASES:
            break
        seen_low.add(al_low)
        aliases.append(al)
    return term, tuple(aliases)


def parse_hotwords_text(
    text: str,
    *,
    path: Path | None = None,
    exists: bool = True,
) -> HotwordsTable:
    """Parse layered plain-text ``hotwords.txt`` (100% compatible with legacy flat files and ``<-`` aliases)."""
    clean_text = text.lstrip("\ufeff")
    order_by_sec: dict[str, list[str]] = {s: [] for s in VALID_HOTWORD_SCOPES}
    terms_by_sec: dict[str, dict[str, str]] = {s: {} for s in VALID_HOTWORD_SCOPES}
    aliases_by_sec: dict[str, dict[str, list[str]]] = {s: {} for s in VALID_HOTWORD_SCOPES}
    alias_lows_by_sec: dict[str, dict[str, set[str]]] = {s: {} for s in VALID_HOTWORD_SCOPES}
    current_section: str | None = "global"

    for raw_line in clean_text.splitlines():
        cleaned = _strip_line_comment(raw_line)
        if not cleaned:
            continue
        sec = _parse_section_header(cleaned)
        if sec is not None:
            current_section = sec if sec in VALID_HOTWORD_SCOPES else None
            continue
        if current_section is None:
            continue
        parsed = _parse_entry_cleaned_line(cleaned)
        if parsed is None:
            continue
        term, line_aliases = parsed
        low = term.lower()
        if low not in terms_by_sec[current_section]:
            order_by_sec[current_section].append(low)
            terms_by_sec[current_section][low] = term
            aliases_by_sec[current_section][low] = []
            alias_lows_by_sec[current_section][low] = set()
        al_list = aliases_by_sec[current_section][low]
        al_seen = alias_lows_by_sec[current_section][low]
        for al in line_aliases:
            al_low = al.lower()
            if al_low not in al_seen and len(al_list) < MAX_HOTWORD_ALIASES:
                al_seen.add(al_low)
                al_list.append(al)

    # Guardrail: drop any alias that collides (CI) with any canonical term across all sections.
    all_canonical_lows: set[str] = set()
    for sc in VALID_HOTWORD_SCOPES:
        all_canonical_lows.update(terms_by_sec[sc])

    entries_by_sec: dict[str, tuple[HotwordEntry, ...]] = {}
    canon_by_sec: dict[str, tuple[str, ...]] = {}
    for sc in VALID_HOTWORD_SCOPES:
        sc_entries: list[HotwordEntry] = []
        sc_terms: list[str] = []
        for low in order_by_sec[sc]:
            term = terms_by_sec[sc][low]
            clean_aliases = tuple(
                al for al in aliases_by_sec[sc][low] if al.lower() not in all_canonical_lows
            )
            sc_entries.append(HotwordEntry(term=term, aliases=clean_aliases))
            sc_terms.append(term)
        entries_by_sec[sc] = tuple(sc_entries)
        canon_by_sec[sc] = tuple(sc_terms)

    return HotwordsTable(
        global_terms=canon_by_sec["global"],
        chat_terms=canon_by_sec["chat"],
        coding_terms=canon_by_sec["coding"],
        global_entries=entries_by_sec["global"],
        chat_entries=entries_by_sec["chat"],
        coding_entries=entries_by_sec["coding"],
        path=path,
        exists=exists,
    )


def resolve_hotwords_path(
    path_str: str,
    *,
    env: Mapping[str, str] | None = None,
) -> Path | None:
    """Resolve ``hotwords_file`` path with legacy ``~/.config/voice-input/hotwords.txt`` fallback."""
    if not path_str or not path_str.strip():
        return None
    raw_spec = path_str.strip()
    primary = expand_user_path(raw_spec, env=env)
    if not primary.exists() and not primary.is_symlink() and raw_spec == DEFAULT_HOTWORDS_FILE:
        legacy = expand_user_path(LEGACY_HOTWORDS_FILE, env=env)
        if legacy.is_file():
            return legacy
    return primary


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


def load_hotwords_table(
    path_str: str | Path,
    *,
    env: Mapping[str, str] | None = None,
    use_cache: bool = True,
    strict_encoding: bool = False,
) -> HotwordsTable:
    """Load and parse ``hotwords.txt`` with ``(mtime_ns, ctime_ns, size, ino, dev)`` hot-reload caching."""
    target = path_str if isinstance(path_str, Path) else resolve_hotwords_path(path_str, env=env)
    if target is None:
        return HotwordsTable(path=None, exists=False)

    cache_key = os.path.abspath(str(target))
    try:
        st = os.stat(target)
    except OSError:
        with _HOTWORDS_CACHE_LOCK:
            if use_cache:
                _HOTWORDS_CACHE.pop(cache_key, None)
            _HOTWORDS_BAD_ENCODING_WARNED.pop(cache_key, None)
        return HotwordsTable(path=target, exists=False)

    if not stat.S_ISREG(st.st_mode):
        with _HOTWORDS_CACHE_LOCK:
            if use_cache:
                _HOTWORDS_CACHE.pop(cache_key, None)
            _HOTWORDS_BAD_ENCODING_WARNED.pop(cache_key, None)
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
        raw_bytes = target.read_bytes()
    except OSError:
        return HotwordsTable(path=target, exists=False)

    try:
        raw, _ = _decode_hotwords_bytes(target, raw_bytes)
    except _BadHotwordsEncodingError as exc:
        err_msg = str(exc)
        if strict_encoding:
            raise ConfigError(err_msg) from None
        _warn_bad_encoding_once(cache_key, sig, err_msg)
        bad_table = HotwordsTable(path=target, exists=True, encoding_error=err_msg)
        if use_cache:
            with _HOTWORDS_CACHE_LOCK:
                if len(_HOTWORDS_CACHE) >= _HOTWORDS_CACHE_MAX and cache_key not in _HOTWORDS_CACHE:
                    _HOTWORDS_CACHE.pop(next(iter(_HOTWORDS_CACHE)), None)
                _HOTWORDS_CACHE[cache_key] = (sig, bad_table)
        return bad_table

    table = parse_hotwords_text(raw, path=target, exists=True)
    with _HOTWORDS_CACHE_LOCK:
        _HOTWORDS_BAD_ENCODING_WARNED.pop(cache_key, None)
        if use_cache:
            if len(_HOTWORDS_CACHE) >= _HOTWORDS_CACHE_MAX and cache_key not in _HOTWORDS_CACHE:
                _HOTWORDS_CACHE.pop(next(iter(_HOTWORDS_CACHE)), None)
            _HOTWORDS_CACHE[cache_key] = (sig, table)
    return table


def load_hotwords_file(
    path_str: str | Path,
    *,
    env: Mapping[str, str] | None = None,
    scene: str = "global",
    use_cache: bool = True,
) -> list[str]:
    """Load deduplicated manual hotwords for ``scene`` (``global`` | ``chat`` | ``coding`` | ``all``)."""
    table = load_hotwords_table(path_str, env=env, use_cache=use_cache)
    return table.merged_for_scene(scene)


def _inspect_writable_target(target: Path) -> os.stat_result | None:
    """Ensure ``target`` is absent or a regular non-symlink file."""
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ConfigError(f"cannot inspect hotwords file {target}: {exc}") from None
    if stat.S_ISLNK(st.st_mode):
        raise ConfigError(f"refusing to write hotwords through symlink: {target}")
    if stat.S_ISDIR(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise ConfigError(f"unsafe hotwords target (not a regular file): {target}")
    return st


def _ensure_writable_parent(parent: Path, *, create: bool) -> bool:
    """Verify ``parent`` is a directory (creating it with ``0700`` when ``create=True``)."""
    try:
        os.lstat(parent)
    except FileNotFoundError:
        if not create:
            return False
        try:
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise ConfigError(f"cannot create hotwords parent directory {parent}: {exc}") from None
        return True
    except OSError as exc:
        raise ConfigError(f"cannot inspect hotwords parent directory {parent}: {exc}") from None
    if not parent.is_dir():
        raise ConfigError(f"hotwords parent path is not a directory: {parent}")
    return True


@contextmanager
def _hotwords_edit_lock(target: Path) -> Iterator[None]:
    """Serialize read-modify-write edits via a sibling non-symlink ``flock(LOCK_EX)`` file."""
    lock_path = target.parent / f".{target.name}.lock"
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        lfd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR | nofollow, 0o600)
    except OSError as exc:
        raise ConfigError(f"unsafe or unwritable hotwords lock file {lock_path}: {exc}") from None
    try:
        st = os.fstat(lfd)
        if not stat.S_ISREG(st.st_mode):  # pragma: no cover - open(O_RDWR) already rejects directories
            raise ConfigError(f"unsafe hotwords lock file (not a regular file): {lock_path}")
        os.fchmod(lfd, 0o600)
        fcntl.flock(lfd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            try:
                fcntl.flock(lfd, fcntl.LOCK_UN)
            except OSError:  # pragma: no cover - defensive unlock cleanup
                pass
    finally:
        try:
            os.close(lfd)
        except OSError:  # pragma: no cover - defensive close cleanup
            pass


def _detect_eol(raw_lines: Sequence[str]) -> str:
    crlf = sum(1 for ln in raw_lines if ln.endswith("\r\n"))
    lf = sum(1 for ln in raw_lines if ln.endswith("\n") and not ln.endswith("\r\n"))
    return "\r\n" if crlf > lf else "\n"


def _read_text_preserving_format(target: Path) -> tuple[str, bool, str]:
    raw_bytes = target.read_bytes()
    try:
        text, has_bom = _decode_hotwords_bytes(target, raw_bytes)
    except _BadHotwordsEncodingError as exc:
        raise ConfigError(str(exc)) from None
    eol = _detect_eol(text.splitlines(keepends=True))
    return text, has_bom, eol


def _atomic_write_bytes_0600(target: Path, payload: bytes) -> None:
    """Atomically write raw ``payload`` bytes to ``target`` via same-dir temp file + ``0600`` + ``os.replace``."""
    _inspect_writable_target(target)
    _ensure_writable_parent(target.parent, create=True)

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

    cache_key = os.path.abspath(str(target))
    with _HOTWORDS_CACHE_LOCK:
        _HOTWORDS_CACHE.pop(cache_key, None)


def _find_section_insert_index(lines: list[str], target_scope: str) -> int | None:
    """Find insertion line index right after the last non-blank line in ``target_scope``.

    Returns ``None`` if a new ``[target_scope]`` section header should be appended.
    """
    header_spans: list[tuple[str | None, int, int]] = []
    cur_sec: str | None = "global"
    cur_start = 0
    has_explicit_header = False

    for idx, raw_line in enumerate(lines):
        cleaned = _strip_line_comment(raw_line)
        sec = _parse_section_header(cleaned) if cleaned else None
        if sec is not None:
            header_spans.append((cur_sec, cur_start, idx))
            cur_sec = sec if sec in VALID_HOTWORD_SCOPES else None
            cur_start = idx
            has_explicit_header = True
    header_spans.append((cur_sec, cur_start, len(lines)))

    # Prefer the last explicit [target_scope] block if present.
    explicit_matches = [
        (start, end)
        for sec, start, end in header_spans
        if sec == target_scope and (start > 0 or (lines and _parse_section_header(_strip_line_comment(lines[0])) == target_scope))
    ]
    if explicit_matches:
        start, end = explicit_matches[-1]
        insert_at = start + 1
        for i in range(start + 1, end):
            if lines[i].strip():
                insert_at = i + 1
        return insert_at

    # For implicit top-level global region (legacy flat files or top terms before first header):
    if target_scope == "global":
        _, top_start, top_end = header_spans[0]
        top_has_term = any(bool(_strip_line_comment(lines[i])) for i in range(top_start, top_end))
        if not has_explicit_header or top_has_term:
            insert_at = top_start
            for i in range(top_start, top_end):
                if lines[i].strip():
                    insert_at = i + 1
            return insert_at
    return None


def _rewrite_raw_line_with_entry(raw_line: str, entry: HotwordEntry) -> str:
    """Replace the entry portion of ``raw_line`` while preserving leading whitespace, inline comment, and EOL."""
    body = raw_line.rstrip("\r\n")
    line_eol = raw_line[len(body) :]
    leading_len = len(body) - len(body.lstrip(" \t"))
    leading_ws = body[:leading_len]
    content = body[leading_len:]
    m = _INLINE_COMMENT_RE.search(content)
    comment_suffix = content[m.start() :] if m is not None else ""
    return f"{leading_ws}{entry.format_line()}{comment_suffix}{line_eol}"


def _update_entry_line_in_scope(
    raw_lines: list[str],
    target_scope: str,
    entry: HotwordEntry,
) -> list[str]:
    """Update the first matching canonical entry line in ``target_scope`` in place."""
    target_low = entry.term.lower()
    cur_sec: str | None = "global"
    out: list[str] = []
    updated = False
    for raw_line in raw_lines:
        cleaned = _strip_line_comment(raw_line)
        if not cleaned:
            out.append(raw_line)
            continue
        sec = _parse_section_header(cleaned)
        if sec is not None:
            cur_sec = sec if sec in VALID_HOTWORD_SCOPES else None
            out.append(raw_line)
            continue
        if not updated and cur_sec == target_scope:
            parsed = _parse_entry_cleaned_line(cleaned)
            if parsed is not None and parsed[0].lower() == target_low:
                out.append(_rewrite_raw_line_with_entry(raw_line, entry))
                updated = True
                continue
        out.append(raw_line)
    return out


def add_hotwords(
    path: str | Path,
    terms: Sequence[str],
    scope: str = "global",
    *,
    aliases: Sequence[str] = (),
) -> HotwordsEditResult:
    """Add one or more hotwords (with optional mishearing ``aliases`` for a single term) to ``scope``.

    Preserves comments, BOM, EOL, and other scopes; uses ``flock`` + ``0600`` atomic replacement.
    If ``aliases`` is provided and the canonical term already exists in ``scope``, merges deduplicated
    aliases into the existing line in place (enforcing the 5-alias cap and conflict guardrails).
    """
    eff_scope = normalize_hotword_scope(scope)
    clean_terms = dedupe_hotwords_ci([validate_hotword_term(t) for t in terms])
    has_aliases = bool(aliases)
    if has_aliases and len(clean_terms) != 1:
        raise ValueError("mishearing aliases can only be specified when adding a single hotword term")

    target = Path(path)
    _inspect_writable_target(target)

    if not clean_terms:
        return HotwordsEditResult(path=target, scope=eff_scope, changed=False)

    # Fail fast on alias syntax / single-char / >5 / self-conflict before touching disk or lock.
    pre_aliases: tuple[str, ...] = ()
    pre_warnings: tuple[str, ...] = ()
    if has_aliases:
        pre_aliases, pre_warnings = validate_hotword_aliases(clean_terms[0], aliases)

    _ensure_writable_parent(target.parent, create=True)
    with _hotwords_edit_lock(target):
        st = _inspect_writable_target(target)
        if st is not None:
            existing_text, has_bom, eol = _read_text_preserving_format(target)
        else:
            existing_text, has_bom, eol = "", False, "\n"

        table = parse_hotwords_text(existing_text, path=target, exists=(st is not None))
        all_canonical_lows = {
            e.term.lower()
            for sc in VALID_HOTWORD_SCOPES
            for e in table.entries_for_scope(sc)
        }
        all_alias_owners: dict[str, str] = {}
        for sc in VALID_HOTWORD_SCOPES:
            for e in table.entries_for_scope(sc):
                for al in e.aliases:
                    all_alias_owners.setdefault(al.lower(), e.term)

        # Reject adding a canonical term that collides with an existing mishearing alias.
        for term in clean_terms:
            if (owner := all_alias_owners.get(term.lower())) is not None:
                raise ValueError(
                    f"hotword {term!r} conflicts with existing mishearing alias of {owner!r}"
                )

        existing_entries = {e.term.lower(): e for e in table.entries_for_scope(eff_scope)}

        if has_aliases:
            term = clean_terms[0]
            other_canonical_lows = all_canonical_lows - {term.lower()}
            clean_aliases, warnings = validate_hotword_aliases(
                term,
                pre_aliases,
                existing_canonical_lows=other_canonical_lows,
            )
            existing_entry = existing_entries.get(term.lower())
            if existing_entry is not None:
                existing_al_lows = {a.lower() for a in existing_entry.aliases}
                new_aliases = tuple(a for a in clean_aliases if a.lower() not in existing_al_lows)
                if not new_aliases:
                    return HotwordsEditResult(
                        path=target,
                        scope=eff_scope,
                        skipped=(term,),
                        changed=False,
                        warnings=warnings,
                    )
                combined_aliases = (*existing_entry.aliases, *new_aliases)
                if len(combined_aliases) > MAX_HOTWORD_ALIASES:
                    raise ValueError(
                        f"too many mishearing aliases for {existing_entry.term!r}: "
                        f"{len(combined_aliases)} exceeds maximum of {MAX_HOTWORD_ALIASES}"
                    )
                merged_entry = HotwordEntry(term=existing_entry.term, aliases=combined_aliases)
                raw_lines = existing_text.splitlines(keepends=True)
                new_raw_lines = _update_entry_line_in_scope(raw_lines, eff_scope, merged_entry)
                payload = (b"\xef\xbb\xbf" if has_bom else b"") + "".join(new_raw_lines).encode("utf-8")
                _atomic_write_bytes_0600(target, payload)
                return HotwordsEditResult(
                    path=target,
                    scope=eff_scope,
                    updated=(existing_entry.term,),
                    aliases_added=new_aliases,
                    changed=True,
                    warnings=warnings,
                )

            new_entry = HotwordEntry(term=term, aliases=clean_aliases)
            added_lines = [new_entry.format_line()]
            added = [term]
            skipped: list[str] = []
            aliases_added = clean_aliases
        else:
            warnings = pre_warnings
            aliases_added = ()
            added = []
            skipped = []
            existing_low = set(existing_entries)
            for term in clean_terms:
                if term.lower() in existing_low:
                    skipped.append(term)
                else:
                    existing_low.add(term.lower())
                    added.append(term)
            if not added:
                return HotwordsEditResult(
                    path=target,
                    scope=eff_scope,
                    added=(),
                    skipped=tuple(skipped),
                    changed=False,
                )
            added_lines = list(added)

        raw_lines = existing_text.splitlines(keepends=True) if existing_text else []
        if not raw_lines:
            new_raw_lines = [f"[{eff_scope}]{eol}", *(f"{ln}{eol}" for ln in added_lines)]
        else:
            plain_lines = [ln.rstrip("\r\n") for ln in raw_lines]
            insert_idx = _find_section_insert_index(plain_lines, eff_scope)
            if insert_idx is not None:
                head = list(raw_lines[:insert_idx])
                if head and not head[-1].endswith(("\n", "\r")):
                    head[-1] = head[-1] + eol
                new_raw_lines = [*head, *(f"{ln}{eol}" for ln in added_lines), *raw_lines[insert_idx:]]
            else:
                new_raw_lines = list(raw_lines)
                if new_raw_lines and not new_raw_lines[-1].endswith(("\n", "\r")):
                    new_raw_lines[-1] = new_raw_lines[-1] + eol
                if new_raw_lines and new_raw_lines[-1].strip():
                    new_raw_lines.append(eol)
                new_raw_lines.append(f"[{eff_scope}]{eol}")
                new_raw_lines.extend(f"{ln}{eol}" for ln in added_lines)

        payload = (b"\xef\xbb\xbf" if has_bom else b"") + "".join(new_raw_lines).encode("utf-8")
        _atomic_write_bytes_0600(target, payload)
        return HotwordsEditResult(
            path=target,
            scope=eff_scope,
            added=tuple(added),
            skipped=tuple(skipped),
            changed=True,
            aliases_added=aliases_added,
            warnings=warnings,
        )


def add_hotword(
    path: str | Path,
    term: str,
    scope: str = "global",
    *,
    aliases: Sequence[str] = (),
) -> HotwordsEditResult:
    """Add a single hotword (with optional mishearing ``aliases``) to ``scope``."""
    return add_hotwords(path, [term], scope=scope, aliases=aliases)


def remove_hotwords(
    path: str | Path,
    terms: Sequence[str],
    scope: str = "global",
) -> HotwordsEditResult:
    """Remove hotwords by canonical term from ``scope`` (``global`` | ``chat`` | ``coding`` | ``all``)."""
    eff_scope = normalize_hotword_scope(scope, allow_all=True)
    clean_terms = dedupe_hotwords_ci([validate_hotword_term(t) for t in terms])
    target = Path(path)
    st_pre = _inspect_writable_target(target)

    if not clean_terms or st_pre is None or not _ensure_writable_parent(target.parent, create=False):
        return HotwordsEditResult(
            path=target,
            scope=eff_scope,
            removed=(),
            not_found=tuple(clean_terms),
            changed=False,
        )

    with _hotwords_edit_lock(target):
        st = _inspect_writable_target(target)
        if st is None:
            return HotwordsEditResult(
                path=target,
                scope=eff_scope,
                removed=(),
                not_found=tuple(clean_terms),
                changed=False,
            )
        existing_text, has_bom, _ = _read_text_preserving_format(target)
        target_lows = {t.lower() for t in clean_terms}
        removed_map: dict[str, str] = {}

        cur_sec: str | None = "global"
        kept_raw_lines: list[str] = []
        changed = False

        for raw_line in existing_text.splitlines(keepends=True):
            cleaned = _strip_line_comment(raw_line)
            if not cleaned:
                kept_raw_lines.append(raw_line)
                continue
            sec = _parse_section_header(cleaned)
            if sec is not None:
                cur_sec = sec if sec in VALID_HOTWORD_SCOPES else None
                kept_raw_lines.append(raw_line)
                continue
            in_target_scope = (
                cur_sec in VALID_HOTWORD_SCOPES if eff_scope == "all" else cur_sec == eff_scope
            )
            parsed = _parse_entry_cleaned_line(cleaned)
            if in_target_scope and parsed is not None:
                canon_term = parsed[0]
                low = canon_term.lower()
                if low in target_lows:
                    removed_map.setdefault(low, canon_term)
                    changed = True
                    continue
            kept_raw_lines.append(raw_line)

        removed = tuple(removed_map[t.lower()] for t in clean_terms if t.lower() in removed_map)
        not_found = tuple(t for t in clean_terms if t.lower() not in removed_map)

        if changed:
            payload = (b"\xef\xbb\xbf" if has_bom else b"") + "".join(kept_raw_lines).encode("utf-8")
            _atomic_write_bytes_0600(target, payload)

        return HotwordsEditResult(
            path=target,
            scope=eff_scope,
            removed=removed,
            not_found=not_found,
            changed=changed,
        )


def remove_hotword(
    path: str | Path,
    term: str,
    scope: str = "global",
) -> HotwordsEditResult:
    """Remove a single hotword (and any attached aliases) by canonical term from ``scope``."""
    return remove_hotwords(path, [term], scope=scope)


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
        if base_url.strip() and not is_gemini_native_url(base_url) and (api or "").strip().lower() == "audio-transcriptions":
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
        w = raw_w
        if spec.sanitize_openai_keywords:
            w = sanitize_openai_keyword(w)
            if not w:
                dropped.append(raw_w)
                continue
        if spec.collapse_file_ext and "." in w:
            stem, ext = w.rsplit(".", 1)
            if len(stem) >= 3 and ext.isalnum() and stem.lower() in canonical_by_low:
                w = canonical_by_low[stem.lower()]
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


def select_stage2_manual_entries(
    doc_or_path: HotwordsTable | str | Path | None,
    *,
    mode: str = "general",
    env: Mapping[str, str] | None = None,
    use_cache: bool = True,
) -> list[HotwordEntry]:
    """Select static manual hotword entries for Stage 2 / single-hop ``<vocabulary_hints>``.

    - ``mode == "agent"``: merges ``[coding]`` then ``[global]``.
    - ``mode == "general"``: merges ``[global]`` then ``[chat]``.
    """
    m = (mode or "general").strip().lower()
    if m == "agent":
        scopes: tuple[str, ...] = ("coding", "global")
    elif m == "general":
        scopes = ("global", "chat")
    else:
        raise ValueError(f"invalid stage2 hotwords mode {mode!r} (expected 'agent' or 'general')")

    if doc_or_path is None:
        return []
    table = (
        doc_or_path
        if isinstance(doc_or_path, HotwordsTable)
        else load_hotwords_table(doc_or_path, env=env, use_cache=use_cache)
    )
    return table.merged_entries_for_scopes(scopes)


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
        if len(al) <= 1:
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
    """Truncate Stage 2 hotword entries strictly in line order within item and rendered-char budgets.

    Returns ``(kept_entries, dropped_entries, used_chars)``.
    """
    eff_max_items = max(0, int(max_items))
    eff_max_chars = max(0, int(max_chars))
    normalized = _coerce_stage2_entries(entries)

    kept: list[HotwordEntry] = []
    dropped: list[HotwordEntry] = []
    used_chars = 0
    truncated = False

    for entry in normalized:
        rendered = format_stage2_entry_line(entry)
        if truncated or eff_max_items <= 0 or eff_max_chars <= 0 or len(kept) >= eff_max_items:
            dropped.append(entry)
            truncated = True
            continue
        add_len = len(rendered) + (1 if kept else 0)
        if used_chars + add_len > eff_max_chars:
            dropped.append(entry)
            truncated = True
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


def summarize_stage2_hotwords_budgets(
    doc_or_path: HotwordsTable | str | Path | None,
    *,
    env: Mapping[str, str] | None = None,
    use_cache: bool = True,
    max_items: int = STAGE2_HOTWORDS_MAX_ITEMS,
    max_chars: int = STAGE2_HOTWORDS_MAX_CHARS,
) -> dict[str, Stage2HotwordsBudgetSummary]:
    """Summarize Stage 2 manual vocabulary budget usage for both ``agent`` and ``general`` modes."""
    table: HotwordsTable | None
    if doc_or_path is None:
        table = None
    elif isinstance(doc_or_path, HotwordsTable):
        table = doc_or_path
    else:
        table = load_hotwords_table(doc_or_path, env=env, use_cache=use_cache)

    eff_max_items = max(0, int(max_items))
    eff_max_chars = max(0, int(max_chars))
    out: dict[str, Stage2HotwordsBudgetSummary] = {}
    for mode, scopes in (("agent", ("coding", "global")), ("general", ("global", "chat"))):
        selected = select_stage2_manual_entries(table, mode=mode)
        kept, dropped, used_chars = budget_stage2_hotwords(
            selected,
            max_items=eff_max_items,
            max_chars=eff_max_chars,
        )
        out[mode] = Stage2HotwordsBudgetSummary(
            mode=mode,
            scopes=scopes,
            kept_count=len(kept),
            dropped_count=len(dropped),
            total_input=len(kept) + len(dropped),
            used_chars=used_chars,
            max_items=eff_max_items,
            max_chars=eff_max_chars,
            dropped=tuple(e.term for e in dropped),
        )
    return out


def format_stage2_budget_line(summary: Stage2HotwordsBudgetSummary, *, name: str = "") -> str:
    """Format a compact Stage 2 budget line such as ``stage2 (agent: coding+global): 12/100 items, 240/1500 chars [OK]``."""
    label = name or f"stage2 ({summary.mode}: {'+'.join(summary.scopes)})"
    base = f"{label}: {summary.kept_count}/{summary.max_items} items, {summary.used_chars}/{summary.max_chars} chars"
    if summary.truncated:
        preview = ", ".join(summary.dropped[:5])
        more = f", +{summary.dropped_count - 5} more" if summary.dropped_count > 5 else ""
        return f"{base} [TRUNCATED: {summary.dropped_count} dropped: {preview}{more}]"
    return f"{base} [OK]"


def summarize_hotwords_budgets(
    global_terms: Sequence[str],
    scoped_terms: Mapping[str, Sequence[str]] | Sequence[str] | None = None,
    *,
    active_provider: str = "",
    active_model: str = "",
    active_api: str = "",
    active_base_url: str = "",
) -> dict[str, Any]:
    """Summarize active and mainstream ASR provider quota usage for ``hotwords list``."""
    g_clean = dedupe_hotwords_ci(global_terms)
    mounted_sets: dict[str, list[str]] = {"global": g_clean}

    if isinstance(scoped_terms, Mapping):
        for sc in ("chat", "coding"):
            sc_list = scoped_terms.get(sc, ())
            mounted_sets[sc] = dedupe_hotwords_ci([*sc_list, *g_clean])
        reference_scene = max(
            ("coding", "chat", "global"),
            key=lambda s: (len(mounted_sets[s]), sum(len(w) for w in mounted_sets[s])),
        )
        reference_terms = mounted_sets[reference_scene]
    elif scoped_terms is not None and not isinstance(scoped_terms, (str, bytes)):
        reference_scene = "scoped"
        reference_terms = dedupe_hotwords_ci([*scoped_terms, *g_clean])
        mounted_sets["scoped"] = reference_terms
    else:
        reference_scene = "global"
        reference_terms = g_clean

    active_summary: dict[str, Any] | None = None
    if active_provider.strip():
        per_scene: dict[str, dict[str, Any]] = {}
        for sc_name, sc_terms in mounted_sets.items():
            res = budget_hotwords_for_provider(
                sc_terms,
                active_provider,
                model=active_model,
                api=active_api,
                base_url=active_base_url,
            )
            per_scene[sc_name] = res.as_dict()
        active_summary = {
            "provider": ASR_PROVIDER_ALIASES.get(active_provider.strip().lower(), active_provider.strip().lower()),
            "model": active_model.strip(),
            "api": active_api.strip(),
            "reference_scene": reference_scene,
            "reference": per_scene[reference_scene],
            "by_scene": per_scene,
        }

    providers_summary: dict[str, dict[str, Any]] = {}
    for prov in MAINSTREAM_QUOTA_PROVIDERS:
        res = budget_hotwords_for_provider(reference_terms, prov)
        providers_summary[prov] = res.as_dict()

    return {
        "reference_scene": reference_scene,
        "reference_count": len(reference_terms),
        "active": active_summary,
        "providers": providers_summary,
    }


def format_budget_line(res: HotwordsBudgetResult, *, name: str = "") -> str:
    """Format a compact human-readable quota line such as ``doubao: 12/30 terms, 68/180 chars``."""
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
