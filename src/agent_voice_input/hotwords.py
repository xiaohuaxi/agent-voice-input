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

_CTRL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
_UNICODE_LINE_SEPS = frozenset({"\x85", "\u2028", "\u2029"})
_INLINE_COMMENT_RE = re.compile(r"\s+#")
_SECTION_HEADER_RE = re.compile(r"^\[([^\[\]]+)\]$")
_OPENROUTER_KEYTERMS_PREFIXES: tuple[str, ...] = ("deepgram/", "assemblyai/")

_HOTWORDS_CACHE_LOCK = threading.Lock()
_HOTWORDS_CACHE: dict[str, tuple[tuple[int, int, int, int, int], HotwordsTable]] = {}
_HOTWORDS_BAD_ENCODING_WARNED: dict[str, tuple[int, int, int, int, int]] = {}
_HOTWORDS_CACHE_MAX = 32


@dataclass(frozen=True)
class HotwordsTable:
    global_terms: tuple[str, ...] = ()
    chat_terms: tuple[str, ...] = ()
    coding_terms: tuple[str, ...] = ()
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

    def merged_for_scene(self, scene: str) -> list[str]:
        """Return priority-ordered deduplicated manual hotwords for ``scene``.

        Active scene terms (``chat`` or ``coding``) precede ``global`` terms, preserving first-seen casing.
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


@dataclass(frozen=True)
class HotwordsEditResult:
    path: Path
    scope: str
    added: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    not_found: tuple[str, ...] = ()
    changed: bool = False


@dataclass(frozen=True)
class HotwordsQuotaSpec:
    key: str
    label: str
    max_terms: int
    max_chars: int | None
    separator: str = ""
    max_term_chars: int | None = None
    collapse_file_ext: bool = False
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


# Single source of truth for per-provider ASR hotword quotas.
# DashScope context_text reserves 7 chars for "常用技术术语：" (420 - 7 = 413);
# Whisper /audio/transcriptions reserves 7 chars for "常用技术术语：" (360 - 7 = 353).
HOTWORDS_QUOTA_SPECS: dict[str, HotwordsQuotaSpec] = {
    "doubao": HotwordsQuotaSpec(
        key="doubao",
        label="doubao",
        max_terms=30,
        max_chars=180,
        separator="",
        max_term_chars=60,
        collapse_file_ext=True,
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
        max_terms=45,
        max_chars=413,
        separator="、",
    ),
    "single_hop": HotwordsQuotaSpec(
        key="single_hop",
        label="single_hop",
        max_terms=45,
        max_chars=480,
        separator=", ",
    ),
    "openrouter_keyterms": HotwordsQuotaSpec(
        key="openrouter_keyterms",
        label="openrouter(keyterms)",
        max_terms=50,
        max_chars=None,
        separator="",
    ),
    "openrouter_stt_unsupported": HotwordsQuotaSpec(
        key="openrouter_stt_unsupported",
        label="openrouter(stt)",
        max_terms=0,
        max_chars=0,
        separator="",
        note="OpenRouter /audio/transcriptions only passes keyterms to deepgram/* and assemblyai/*; use single-hop or Stage 2 refiner",
    ),
}

MAINSTREAM_QUOTA_PROVIDERS: tuple[str, ...] = ("doubao", "whisper", "dashscope", "gemini")


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


def validate_hotword_term(raw: str) -> str:
    """Validate and normalize a single hotword term so it safely round-trips in plain-text ``hotwords.txt``."""
    if not isinstance(raw, str):
        raise ValueError(f"hotword must be a string, got {type(raw).__name__}")
    if (
        _CTRL_CHAR_RE.search(raw)
        or any(ch in _UNICODE_LINE_SEPS for ch in raw)
        or len(raw.splitlines()) > 1
    ):
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
    return term


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


def parse_hotwords_text(
    text: str,
    *,
    path: Path | None = None,
    exists: bool = True,
) -> HotwordsTable:
    """Parse layered plain-text ``hotwords.txt`` (100% compatible with legacy flat files)."""
    clean_text = text.lstrip("\ufeff")
    buckets: dict[str, list[str]] = {s: [] for s in VALID_HOTWORD_SCOPES}
    seen_low: dict[str, set[str]] = {s: set() for s in VALID_HOTWORD_SCOPES}
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
        low = cleaned.lower()
        if low not in seen_low[current_section]:
            seen_low[current_section].add(low)
            buckets[current_section].append(cleaned)

    return HotwordsTable(
        global_terms=tuple(buckets["global"]),
        chat_terms=tuple(buckets["chat"]),
        coding_terms=tuple(buckets["coding"]),
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


def add_hotwords(
    path: str | Path,
    terms: Sequence[str],
    scope: str = "global",
) -> HotwordsEditResult:
    """Add one or more hotwords to ``scope`` while preserving comments, BOM, EOL, and other scopes."""
    eff_scope = normalize_hotword_scope(scope)
    clean_terms = dedupe_hotwords_ci([validate_hotword_term(t) for t in terms])
    target = Path(path)
    _inspect_writable_target(target)

    if not clean_terms:
        return HotwordsEditResult(path=target, scope=eff_scope, changed=False)

    _ensure_writable_parent(target.parent, create=True)
    with _hotwords_edit_lock(target):
        st = _inspect_writable_target(target)
        if st is not None:
            existing_text, has_bom, eol = _read_text_preserving_format(target)
        else:
            existing_text, has_bom, eol = "", False, "\n"

        table = parse_hotwords_text(existing_text, path=target, exists=(st is not None))
        existing_low = {w.lower() for w in table.terms_for_scope(eff_scope)}

        added: list[str] = []
        skipped: list[str] = []
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

        raw_lines = existing_text.splitlines(keepends=True) if existing_text else []
        if not raw_lines:
            new_raw_lines = [f"[{eff_scope}]{eol}", *(f"{t}{eol}" for t in added)]
        else:
            plain_lines = [ln.rstrip("\r\n") for ln in raw_lines]
            insert_idx = _find_section_insert_index(plain_lines, eff_scope)
            if insert_idx is not None:
                head = list(raw_lines[:insert_idx])
                if head and not head[-1].endswith(("\n", "\r")):
                    head[-1] = head[-1] + eol
                new_raw_lines = [*head, *(f"{t}{eol}" for t in added), *raw_lines[insert_idx:]]
            else:
                new_raw_lines = list(raw_lines)
                if new_raw_lines and not new_raw_lines[-1].endswith(("\n", "\r")):
                    new_raw_lines[-1] = new_raw_lines[-1] + eol
                if new_raw_lines and new_raw_lines[-1].strip():
                    new_raw_lines.append(eol)
                new_raw_lines.append(f"[{eff_scope}]{eol}")
                new_raw_lines.extend(f"{t}{eol}" for t in added)

        payload = (b"\xef\xbb\xbf" if has_bom else b"") + "".join(new_raw_lines).encode("utf-8")
        _atomic_write_bytes_0600(target, payload)
        return HotwordsEditResult(
            path=target,
            scope=eff_scope,
            added=tuple(added),
            skipped=tuple(skipped),
            changed=True,
        )


def remove_hotwords(
    path: str | Path,
    terms: Sequence[str],
    scope: str = "global",
) -> HotwordsEditResult:
    """Remove hotwords from ``scope`` (``global`` | ``chat`` | ``coding`` | ``all``) preserving layout, BOM, and EOL."""
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
            low = cleaned.lower()
            if in_target_scope and low in target_lows:
                removed_map.setdefault(low, cleaned)
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
        if eff_model.lower().startswith(_OPENROUTER_KEYTERMS_PREFIXES):
            return HOTWORDS_QUOTA_SPECS["openrouter_keyterms"]
        return HOTWORDS_QUOTA_SPECS["openrouter_stt_unsupported"]
    if prov in ("openai", "groq", "siliconflow", "openai-compatible"):
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
