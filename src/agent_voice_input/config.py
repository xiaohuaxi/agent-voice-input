"""Configuration loading, credential resolution, and secret redaction for agent-voice-input.

Pure Python 3.10+ standard library implementation (no third-party dependencies).
Uses stdlib ``tomllib`` on Python 3.11+ and falls back to ``_parse_simple_toml`` on 3.10.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any, Iterable, Mapping, Sequence
import urllib.parse

try:
    import tomllib as _stdlib_tomllib  # type: ignore[import-not-found]
except ImportError:  # Python 3.10
    _stdlib_tomllib = None


class ConfigError(ValueError):
    """Raised when configuration syntax or values are invalid."""


VALID_TRIGGER_MODES = frozenset({"tap_toggle", "ptt"})
VALID_UI_LANGUAGES = frozenset({"auto", "zh", "en"})
VALID_WSL_SOURCES = frozenset({"auto", "pulse", "winmm", "wasapi"})
VALID_ASR_PROVIDERS = frozenset(
    {"auto", "ali", "qwen", "doubao", "gemini", "gemini-flash-audio", "openai-compatible"}
)
VALID_DOUBAO_MODES = frozenset({"auto", "flash", "submit"})
VALID_REFINER_PROVIDERS = frozenset({"codex-luna", "openai-compatible", "off"})
VALID_REFINER_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})

DEFAULT_ASR_MODELS: dict[str, str] = {
    "ali": "qwen3-asr-flash",
    "qwen": "qwen3-asr-flash",
    "doubao": "bigmodel",
    "gemini": "gemini-2.5-flash",
    "gemini-flash-audio": "gemini-2.5-flash",
    "openai-compatible": "gpt-4o-transcribe",
}

# Strict credential lookup priority (suffix style first, prefix style second).
# REDLINE: DOUBAO_ASR_API_ID / API_ID_DOUBAO_ASR typos MUST NEVER be added here.
CREDENTIAL_ENV_ORDER: dict[str, tuple[str, ...]] = {
    "ali": ("ALI_API_KEY", "API_KEY_ALI", "DASHSCOPE_API_KEY"),
    "huoshan": ("HUOSHAN_API_KEY", "API_KEY_HUOSHAN", "ARK_API_KEY"),
    "doubao_app_id": ("DOUBAO_ASR_APP_ID", "APP_ID_DOUBAO_ASR"),
    "doubao_key": ("DOUBAO_ASR_API_KEY", "API_KEY_DOUBAO_ASR"),
    "gemini": ("GEMINI_API_KEY", "API_KEY_GEMINI"),
    "openai": ("OPENAI_API_KEY", "API_KEY_OPENAI"),
}

_SENSITIVE_ENV_NAME_RE = re.compile(
    r"(?:^|_)(?:API_KEY|ACCESS_KEY|APP_ID|API_ID|APP_KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|PRIVATE_KEY)(?:$|_)",
    re.IGNORECASE,
)
_NON_SECRET_ENV_NAMES = frozenset(
    {
        "AGENT_VOICE_INPUT_HOTKEY",
        "AGENT_VOICE_INPUT_UI_LANGUAGE",
        "AGENT_VOICE_INPUT_ASR_API_KEY_ENV",
        "AGENT_VOICE_INPUT_REFINER_API_KEY_ENV",
        "VOICE_INPUT_HOTKEY",
        "VOICE_INPUT_UI_LANGUAGE",
        "VOICE_INPUT_ASR_API_KEY_ENV",
        "VOICE_INPUT_REFINER_API_KEY_ENV",
        "SSH_AUTH_SOCK",
    }
)
_NON_SECRET_ENV_SUFFIXES = (
    "_DIR",
    "_DIRECTORY",
    "_PATH",
    "_FILE",
    "_URL",
    "_ENV",
    "_LIMIT",
    "_COUNT",
    "_MAX",
    "_MIN",
    "_SIZE",
    "_MS",
    "_SEC",
)


@dataclass
class HotkeyConfig:
    key: str = "RControl"
    trigger_mode: str = "tap_toggle"
    tap_max_ms: int = 1200
    debounce_ms: int = 180


@dataclass
class UiConfig:
    language: str = "auto"


DEFAULT_WSL_TAIL_DRAIN_SEC = 0.65
DEFAULT_MACOS_TAIL_DRAIN_SEC = 0.20


@dataclass
class AudioConfig:
    floor_dbfs: float = -50.0
    target_dbfs: float = -20.0
    max_gain_db: float = 40.0
    min_duration_sec: float = 0.3
    min_voiced_sec: float = 0.1
    wsl_source: str = "auto"
    macos_device: str = "default"
    beep: bool = False
    tail_drain_sec: float | None = None

    def effective_tail_drain_sec(self, *, macos: bool = False) -> float:
        if self.tail_drain_sec is not None:
            return self.tail_drain_sec
        return DEFAULT_MACOS_TAIL_DRAIN_SEC if macos else DEFAULT_WSL_TAIL_DRAIN_SEC


@dataclass
class AsrConfig:
    provider: str = "auto"
    model: str = ""
    resource_id: str = "auto"
    doubao_mode: str = "auto"
    base_url: str = ""
    api_key_env: str = ""
    hotwords_file: str = "~/.config/agent-voice-input/hotwords.txt"
    timeout_sec: float = 15.0

    def canonical_provider(self) -> str:
        p = self.provider.strip().lower()
        if p == "qwen":
            return "ali"
        if p == "gemini-flash-audio":
            return "gemini"
        return p

    def effective_model(self, resolved_provider: str | None = None) -> str:
        if self.model and self.model.strip():
            return self.model.strip()
        prov = (resolved_provider or self.provider).strip().lower()
        return DEFAULT_ASR_MODELS.get(prov, DEFAULT_ASR_MODELS["ali"])


@dataclass
class RefinerConfig:
    provider: str = "codex-luna"
    model: str = "gpt-6-luna"
    effort: str = "low"
    base_url: str = ""
    api_key_env: str = ""
    max_turns_per_thread: int = 20
    timeout_sec: float = 12.0
    force_on_single_hop: bool = False


@dataclass
class Config:
    hotkey: HotkeyConfig = field(default_factory=HotkeyConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    refiner: RefinerConfig = field(default_factory=RefinerConfig)
    config_path: str | None = None


@dataclass(frozen=True)
class CredentialItem:
    """Holds a resolved secret value and the environment variable name it came from.

    ``__repr__`` and ``__str__`` intentionally redact ``value`` so credentials never leak
    into tracebacks, test diffs, or diagnostic logs.
    """

    value: str
    source_env: str

    def __repr__(self) -> str:
        return f"CredentialItem(value='[REDACTED]', source_env={self.source_env!r})"

    def __str__(self) -> str:
        return f"configured (via {self.source_env})"


@dataclass(frozen=True)
class ProviderCredentials:
    ali: CredentialItem | None = None
    huoshan: CredentialItem | None = None
    doubao_app_id: CredentialItem | None = None
    doubao_key: CredentialItem | None = None
    gemini: CredentialItem | None = None
    openai: CredentialItem | None = None
    asr_custom: CredentialItem | None = None
    refiner_custom: CredentialItem | None = None

    def all_secret_values(self) -> list[str]:
        vals: list[str] = []
        for f in dataclasses.fields(self):
            item: CredentialItem | None = getattr(self, f.name)
            if item is not None and item.value:
                vals.append(item.value)
        return vals


def expand_user_path(
    raw_path: str | Path,
    *,
    env: Mapping[str, str] | None = None,
    home_dir: str | Path | None = None,
) -> Path:
    """Expand leading ``~`` honoring explicit ``home_dir`` or ``env['HOME']`` first."""
    s = str(raw_path).strip()
    if s == "~" or s.startswith("~/") or s.startswith("~\\"):
        home: str | None = None
        if home_dir is not None:
            home = str(home_dir)
        elif env is not None and env.get("HOME", "").strip():
            home = env["HOME"].strip()
        if home:
            return Path(home) if len(s) == 1 else Path(home) / s[2:]
    return Path(os.path.expanduser(s))


def ensure_secure_dir(path: str | Path, *, expected_uid: int | None = None) -> Path:
    """Create ``path`` with ``0700`` mode and verify it is a non-symlink directory owned by ``expected_uid``."""
    target = Path(path)
    owner_uid = os.getuid() if expected_uid is None else expected_uid

    try:
        pre_st = os.lstat(target)
    except FileNotFoundError:
        pre_st = None
    except OSError as exc:
        raise ConfigError(f"cannot inspect runtime directory {target}: {exc}") from None

    if pre_st is not None and (stat.S_ISLNK(pre_st.st_mode) or not stat.S_ISDIR(pre_st.st_mode)):
        raise ConfigError(f"unsafe runtime directory (not a real directory): {target}")

    try:
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise ConfigError(f"cannot create runtime directory {target}: {exc}") from None

    open_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(str(target), open_flags)
    except OSError as exc:
        raise ConfigError(f"unsafe runtime directory (symlink or non-directory): {target}: {exc}") from None

    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode):
            raise ConfigError(f"unsafe runtime directory (not a directory): {target}")
        if st.st_uid != owner_uid:
            raise ConfigError(
                f"unsafe runtime directory owner uid={st.st_uid} (expected {owner_uid}): {target}"
            )
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)
    return target


def get_runtime_dir(
    env: Mapping[str, str] | None = None,
    *,
    uid: int | None = None,
    create: bool = False,
) -> Path:
    """Return ``${XDG_RUNTIME_DIR}/agent-voice-input`` (or ``~/.cache/agent-voice-input``)."""
    src_env = os.environ if env is None else env
    explicit_rt = (
        src_env.get("AGENT_VOICE_INPUT_RUNTIME_DIR")
        or src_env.get("VOICE_INPUT_RUNTIME_DIR")
        or ""
    ).strip()
    if explicit_rt:
        rt_dir = expand_user_path(explicit_rt, env=src_env)
    else:
        xdg = (src_env.get("XDG_RUNTIME_DIR") or "").strip()
        if xdg:
            rt_dir = Path(xdg) / "agent-voice-input"
        else:
            cache_home = (src_env.get("XDG_CACHE_HOME") or "").strip()
            base_cache = (
                expand_user_path(cache_home, env=src_env)
                if cache_home
                else (expand_user_path("~", env=src_env) / ".cache")
            )
            rt_dir = base_cache / "agent-voice-input"
    if create:
        ensure_secure_dir(rt_dir, expected_uid=uid)
    return rt_dir


# ---------------------------------------------------------------------------
# Environment (.env) file parser
# ---------------------------------------------------------------------------

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _parse_env_value(raw_val: str) -> str:
    s = raw_val.strip()
    if not s:
        return ""
    if s[0] == "'":
        end = s.find("'", 1)
        if end != -1:
            return s[1:end]
        return s[1:]
    if s[0] == '"':
        out: list[str] = []
        i = 1
        while i < len(s):
            ch = s[i]
            if ch == "\\" and i + 1 < len(s):
                nxt = s[i + 1]
                escapes = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\", "$": "$"}
                out.append(escapes.get(nxt, nxt))
                i += 2
                continue
            if ch == '"':
                break
            out.append(ch)
            i += 1
        return "".join(out)
    # Empty unquoted value followed directly by `# comment` (e.g. `KEY= # optional`)
    if s.startswith("#"):
        return ""
    # Unquoted value with trailing `# comment` preceded by whitespace
    m = re.search(r"\s+#", s)
    if m:
        s = s[: m.start()]
    return s.strip()


def parse_env_text(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    if text.startswith("\ufeff"):
        text = text[1:]
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export ") or line.startswith("export\t"):
            line = line[6:].strip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if not _ENV_KEY_RE.match(k):
            continue
        result[k] = _parse_env_value(v)
    return result


def load_env_files(
    base_env: Mapping[str, str] | None = None,
    *,
    env_paths: Sequence[str | Path] | None = None,
    home_dir: str | Path | None = None,
) -> dict[str, str]:
    """Merge optional .env files behind existing environment variables (never overwrites)."""
    merged: dict[str, str] = dict(os.environ if base_env is None else base_env)
    home = (
        Path(home_dir)
        if home_dir is not None
        else Path((merged.get("HOME") or "").strip() or Path.home())
    )
    candidates: Sequence[str | Path] = (
        env_paths
        if env_paths is not None
        else (
            home / ".config" / "agent-voice-input" / "agent-voice-input.env",
            home / ".config" / "agent-voice-input" / ".env",
            home / ".config" / "voice-input" / "voice-input.env",
            home / ".config" / "voice-input" / ".env",
        )
    )
    for p in candidates:
        path = expand_user_path(p, env=merged, home_dir=home)
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        for k, v in parse_env_text(content).items():
            if k not in merged:
                merged[k] = v
    return merged


# ---------------------------------------------------------------------------
# Pure-stdlib simple TOML parser (Python 3.10 fallback & directly testable)
# ---------------------------------------------------------------------------

_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_INT_RE = re.compile(r"^[+-]?(?:0|[1-9](?:_?[0-9])*)$")
_HEX_INT_RE = re.compile(r"^0x[0-9a-fA-F](?:_?[0-9a-fA-F])*$")
_FLOAT_RE = re.compile(
    r"^[+-]?(?:0|[1-9](?:_?[0-9])*)(?:\.[0-9](?:_?[0-9])*(?:[eE][+-]?[0-9](?:_?[0-9])*)?|[eE][+-]?[0-9](?:_?[0-9])*)$"
)


def _is_disallowed_toml_ctrl(ch: str) -> bool:
    code = ord(ch)
    return (code < 0x20 and ch != "\t") or code == 0x7F


def _decode_unicode_scalar(hex_part: str, expected_len: int, lineno: int) -> str:
    if len(hex_part) != expected_len or not all(c in "0123456789abcdefABCDEF" for c in hex_part):
        raise ConfigError(f"line {lineno}: invalid Unicode escape")
    cp = int(hex_part, 16)
    if cp > 0x10FFFF or 0xD800 <= cp <= 0xDFFF:
        raise ConfigError(f"line {lineno}: invalid Unicode scalar U+{cp:04X}")
    return chr(cp)


def _strip_toml_comment(line: str) -> str:
    """Strip unquoted `# ...` comment from a single TOML line."""
    in_single = False
    in_double = False
    escaped = False
    for idx, ch in enumerate(line):
        if in_double:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_double = False
        elif in_single:
            if ch == "'":
                in_single = False
        else:
            if ch == '"':
                in_double = True
            elif ch == "'":
                in_single = True
            elif ch == "#":
                return line[:idx]
    return line


def _parse_toml_basic_string(src: str, pos: int, lineno: int) -> tuple[str, int]:
    """Parse double-quoted string starting at `pos` (pointing to opening `"`)."""
    i = pos + 1
    out: list[str] = []
    escapes = {
        "b": "\b",
        "t": "\t",
        "n": "\n",
        "f": "\f",
        "r": "\r",
        '"': '"',
        "\\": "\\",
    }
    while i < len(src):
        ch = src[i]
        if ch == '"':
            return "".join(out), i + 1
        if ch == "\\":
            i += 1
            if i >= len(src):
                raise ConfigError(f"line {lineno}: unterminated escape sequence")
            esc = src[i]
            if esc in escapes:
                out.append(escapes[esc])
                i += 1
            elif esc == "u":
                out.append(_decode_unicode_scalar(src[i + 1 : i + 5], 4, lineno))
                i += 5
            elif esc == "U":
                out.append(_decode_unicode_scalar(src[i + 1 : i + 9], 8, lineno))
                i += 9
            else:
                raise ConfigError(f"line {lineno}: unsupported escape \\{esc}")
            continue
        if _is_disallowed_toml_ctrl(ch):
            raise ConfigError(f"line {lineno}: unescaped control character in string")
        out.append(ch)
        i += 1
    raise ConfigError(f"line {lineno}: unterminated double-quoted string")


def _parse_toml_literal_string(src: str, pos: int, lineno: int) -> tuple[str, int]:
    """Parse single-quoted literal string starting at `pos` (pointing to `'`)."""
    end = src.find("'", pos + 1)
    if end == -1:
        raise ConfigError(f"line {lineno}: unterminated single-quoted string")
    val = src[pos + 1 : end]
    for ch in val:
        if _is_disallowed_toml_ctrl(ch):
            raise ConfigError(f"line {lineno}: control character in literal string")
    return val, end + 1


def _parse_toml_key_path(raw_key: str, lineno: int) -> list[str]:
    s = raw_key.strip()
    if not s:
        raise ConfigError(f"line {lineno}: empty key")
    parts: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        while i < n and s[i] in " \t":
            i += 1
        if i >= n:
            raise ConfigError(f"line {lineno}: trailing dot in key {raw_key!r}")
        if s[i] == '"':
            seg, i = _parse_toml_basic_string(s, i, lineno)
            parts.append(seg)
        elif s[i] == "'":
            seg, i = _parse_toml_literal_string(s, i, lineno)
            parts.append(seg)
        else:
            start = i
            while i < n and s[i] not in ". \t":
                i += 1
            seg = s[start:i]
            if not _BARE_KEY_RE.match(seg):
                raise ConfigError(f"line {lineno}: invalid key segment {seg!r}")
            parts.append(seg)
        while i < n and s[i] in " \t":
            i += 1
        if i < n:
            if s[i] != ".":
                raise ConfigError(f"line {lineno}: unexpected character {s[i]!r} in key")
            i += 1
            if i >= n:
                raise ConfigError(f"line {lineno}: trailing dot in key {raw_key!r}")
    return parts


def _toml_type_tag(val: Any) -> str:
    if isinstance(val, bool):
        return "bool"
    if isinstance(val, int):
        return "int"
    if isinstance(val, float):
        return "float"
    if isinstance(val, str):
        return "str"
    if isinstance(val, list):
        return "array"
    return type(val).__name__


def _parse_toml_value(src: str, pos: int, lineno: int) -> tuple[Any, int]:
    n = len(src)
    while pos < n and src[pos] in " \t\r\n":
        pos += 1
    if pos >= n:
        raise ConfigError(f"line {lineno}: missing value")
    ch = src[pos]
    if ch == '"':
        if src[pos : pos + 3] == '"""':
            raise ConfigError(f"line {lineno}: multi-line strings are not supported in simple TOML")
        return _parse_toml_basic_string(src, pos, lineno)
    if ch == "'":
        if src[pos : pos + 3] == "'''":
            raise ConfigError(f"line {lineno}: multi-line strings are not supported in simple TOML")
        return _parse_toml_literal_string(src, pos, lineno)
    if ch == "[":
        i = pos + 1
        items: list[Any] = []
        elem_tag: str | None = None
        while True:
            while i < n and src[i] in " \t\r\n":
                i += 1
            if i >= n:
                raise ConfigError(f"line {lineno}: unterminated array")
            if src[i] == "]":
                return items, i + 1
            val, i = _parse_toml_value(src, i, lineno)
            vtag = _toml_type_tag(val)
            if elem_tag is None:
                elem_tag = vtag
            elif elem_tag != vtag:
                raise ConfigError(
                    f"line {lineno}: heterogeneous array types ({elem_tag} vs {vtag})"
                )
            items.append(val)
            while i < n and src[i] in " \t\r\n":
                i += 1
            if i >= n:
                raise ConfigError(f"line {lineno}: unterminated array")
            if src[i] == ",":
                i += 1
                continue
            if src[i] == "]":
                return items, i + 1
            raise ConfigError(f"line {lineno}: expected ',' or ']' in array")
    start = pos
    while pos < n and src[pos] not in " \t\r\n,]":
        pos += 1
    tok = src[start:pos]
    if tok == "true":
        return True, pos
    if tok == "false":
        return False, pos
    if _INT_RE.match(tok):
        return int(tok.replace("_", ""), 10), pos
    if _HEX_INT_RE.match(tok):
        return int(tok.replace("_", ""), 16), pos
    if _FLOAT_RE.match(tok):
        fval = float(tok.replace("_", ""))
        if not math.isfinite(fval):
            raise ConfigError(f"line {lineno}: non-finite float value {tok!r}")
        return fval, pos
    raise ConfigError(f"line {lineno}: invalid TOML value {tok!r}")


def _find_unquoted_equals(line: str) -> int:
    in_single = False
    in_double = False
    escaped = False
    for idx, ch in enumerate(line):
        if in_double:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_double = False
        elif in_single:
            if ch == "'":
                in_single = False
        else:
            if ch == '"':
                in_double = True
            elif ch == "'":
                in_single = True
            elif ch == "=":
                return idx
    return -1


def _has_unclosed_single_line_quote(text: str) -> bool:
    if '"""' in text or "'''" in text:
        return False
    in_single = False
    in_double = False
    escaped = False
    for ch in text:
        if in_double:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_double = False
        elif in_single:
            if ch == "'":
                in_single = False
        else:
            if ch == '"':
                in_double = True
            elif ch == "'":
                in_single = True
    return in_single or in_double


def _bracket_balance(text: str) -> int:
    depth = 0
    in_single = False
    in_double = False
    escaped = False
    for ch in text:
        if in_double:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_double = False
        elif in_single:
            if ch == "'":
                in_single = False
        else:
            if ch == '"':
                in_double = True
            elif ch == "'":
                in_single = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
    return depth


def _parse_simple_toml(text: str) -> dict[str, Any]:
    """Pure-stdlib lightweight TOML parser for Python 3.10 fallback."""
    if text.startswith("\ufeff"):
        text = text[1:]
    root: dict[str, Any] = {}
    current_table: dict[str, Any] = root
    explicit_tables: set[tuple[str, ...]] = set()

    raw_lines = text.splitlines()
    idx = 0
    total = len(raw_lines)
    while idx < total:
        lineno = idx + 1
        raw_ln = raw_lines[idx]
        for ch in _strip_toml_comment(raw_ln):
            if _is_disallowed_toml_ctrl(ch):
                raise ConfigError(f"line {lineno}: disallowed control character")
        cleaned = _strip_toml_comment(raw_ln).strip()
        idx += 1
        if not cleaned:
            continue

        if cleaned.startswith("["):
            if cleaned.startswith("[["):
                raise ConfigError(f"line {lineno}: array of tables [[...]] is not supported")
            if not cleaned.endswith("]"):
                raise ConfigError(f"line {lineno}: invalid table header {cleaned!r}")
            header_body = cleaned[1:-1].strip()
            path_parts = _parse_toml_key_path(header_body, lineno)
            path_tuple = tuple(path_parts)
            if path_tuple in explicit_tables:
                raise ConfigError(f"line {lineno}: duplicate table [{header_body}]")
            explicit_tables.add(path_tuple)
            node = root
            for part in path_parts:
                existing = node.get(part)
                if existing is None:
                    nxt: dict[str, Any] = {}
                    node[part] = nxt
                    node = nxt
                elif isinstance(existing, dict):
                    node = existing
                else:
                    raise ConfigError(
                        f"line {lineno}: cannot redefine scalar key {part!r} as table"
                    )
            current_table = node
            continue

        eq_pos = _find_unquoted_equals(cleaned)
        if eq_pos == -1:
            raise ConfigError(f"line {lineno}: expected 'key = value'")
        key_part = cleaned[:eq_pos]
        val_part = cleaned[eq_pos + 1 :].strip()
        if not val_part:
            raise ConfigError(f"line {lineno}: missing value after '='")

        if _has_unclosed_single_line_quote(val_part):
            raise ConfigError(f"line {lineno}: unterminated string literal")

        while _bracket_balance(val_part) > 0:
            if idx >= total:
                raise ConfigError(f"line {lineno}: unterminated array")
            cont_raw = raw_lines[idx]
            cont_lineno = idx + 1
            for ch in _strip_toml_comment(cont_raw):
                if _is_disallowed_toml_ctrl(ch):
                    raise ConfigError(f"line {cont_lineno}: disallowed control character")
            cont = _strip_toml_comment(cont_raw).strip()
            if _has_unclosed_single_line_quote(cont):
                raise ConfigError(f"line {cont_lineno}: unterminated string literal")
            idx += 1
            if cont:
                val_part = f"{val_part}\n{cont}"

        key_parts = _parse_toml_key_path(key_part, lineno)
        val, end_pos = _parse_toml_value(val_part, 0, lineno)
        rem = val_part[end_pos:].strip()
        if rem:
            raise ConfigError(f"line {lineno}: unexpected trailing tokens {rem!r}")

        target = current_table
        for part in key_parts[:-1]:
            existing = target.get(part)
            if existing is None:
                sub: dict[str, Any] = {}
                target[part] = sub
                target = sub
            elif isinstance(existing, dict):
                target = existing
            else:
                raise ConfigError(f"line {lineno}: key conflict at {part!r}")
        leaf = key_parts[-1]
        if leaf in target:
            raise ConfigError(f"line {lineno}: duplicate key {leaf!r}")
        target[leaf] = val

    return root


def parse_toml(text: str) -> dict[str, Any]:
    """Parse TOML text using stdlib ``tomllib`` when available, else ``_parse_simple_toml``."""
    if text.startswith("\ufeff"):
        text = text[1:]
    if _stdlib_tomllib is not None:
        try:
            data = _stdlib_tomllib.loads(text)
        except Exception as exc:
            raise ConfigError(str(exc)) from None
        if not isinstance(data, dict):
            raise ConfigError("TOML root must be a table")
        return data
    return _parse_simple_toml(text)


# ---------------------------------------------------------------------------
# Config merging, coercion, and validation
# ---------------------------------------------------------------------------


def _as_bool(val: Any, name: str) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        low = val.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
    raise ConfigError(f"{name}: invalid boolean value {val!r}")


def _as_int(val: Any, name: str) -> int:
    if isinstance(val, bool):
        raise ConfigError(f"{name}: expected integer, got boolean")
    if isinstance(val, float):
        raise ConfigError(f"{name}: expected integer, got float {val!r}")
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        s = val.strip()
        if _INT_RE.match(s):
            return int(s.replace("_", ""), 10)
    raise ConfigError(f"{name}: invalid integer value {val!r}")


def _as_float(val: Any, name: str) -> float:
    if isinstance(val, bool):
        raise ConfigError(f"{name}: expected number, got boolean")
    try:
        fval = float(val)
    except (TypeError, ValueError):
        raise ConfigError(f"{name}: invalid float value {val!r}") from None
    if not math.isfinite(fval):
        raise ConfigError(f"{name}: float value must be finite, got {val!r}")
    return fval


def _as_str(val: Any, name: str) -> str:
    if isinstance(val, str):
        return val
    raise ConfigError(f"{name}: expected string, got {type(val).__name__}")


_ZH_EXPLICIT_RE = re.compile(r"^zh(?:[-_][a-z0-9]+)*$", re.IGNORECASE)
_EN_EXPLICIT_RE = re.compile(r"^en(?:[-_][a-z0-9]+)*$", re.IGNORECASE)


def normalize_ui_language(raw: Any, name: str = "ui.language") -> str:
    s = _as_str(raw, name).strip()
    if not s:
        raise ConfigError(
            f"{name} must be one of {sorted(VALID_UI_LANGUAGES)}, got {raw!r}"
        )
    low = s.lower()
    if low == "auto":
        return "auto"
    core = low.split("@", 1)[0].split(".", 1)[0]
    if _ZH_EXPLICIT_RE.match(core):
        return "zh"
    if _EN_EXPLICIT_RE.match(core):
        return "en"
    raise ConfigError(
        f"{name} must be one of {sorted(VALID_UI_LANGUAGES)}, got {raw!r}"
    )


def _normalize_refiner_provider(raw: Any) -> str:
    if raw is False:
        return "off"
    s = _as_str(raw, "refiner.provider").strip().lower()
    if s in ("none", "false", "0", "disabled"):
        return "off"
    return s


def _apply_section_dict(cfg: Config, data: Mapping[str, Any]) -> None:
    allowed_top = {"hotkey", "ui", "audio", "asr", "refiner"}
    for k in data:
        if k not in allowed_top:
            raise ConfigError(f"unknown top-level config section {k!r}")

    hk = data.get("hotkey")
    if hk is not None:
        if not isinstance(hk, Mapping):
            raise ConfigError("[hotkey] must be a table/object")
        for k, v in hk.items():
            if v is None:
                continue
            if k == "key":
                cfg.hotkey.key = _as_str(v, "hotkey.key").strip()
            elif k == "trigger_mode":
                cfg.hotkey.trigger_mode = _as_str(v, "hotkey.trigger_mode").strip().lower()
            elif k == "tap_max_ms":
                cfg.hotkey.tap_max_ms = _as_int(v, "hotkey.tap_max_ms")
            elif k == "debounce_ms":
                cfg.hotkey.debounce_ms = _as_int(v, "hotkey.debounce_ms")
            else:
                raise ConfigError(f"unknown hotkey option {k!r}")

    ui = data.get("ui")
    if ui is not None:
        if not isinstance(ui, Mapping):
            raise ConfigError("[ui] must be a table/object")
        for k, v in ui.items():
            if v is None:
                continue
            if k == "language":
                cfg.ui.language = normalize_ui_language(v, "ui.language")
            else:
                raise ConfigError(f"unknown ui option {k!r}")

    au = data.get("audio")
    if au is not None:
        if not isinstance(au, Mapping):
            raise ConfigError("[audio] must be a table/object")
        for k, v in au.items():
            if v is None:
                continue
            if k == "floor_dbfs":
                cfg.audio.floor_dbfs = _as_float(v, "audio.floor_dbfs")
            elif k == "target_dbfs":
                cfg.audio.target_dbfs = _as_float(v, "audio.target_dbfs")
            elif k == "max_gain_db":
                cfg.audio.max_gain_db = _as_float(v, "audio.max_gain_db")
            elif k == "min_duration_sec":
                cfg.audio.min_duration_sec = _as_float(v, "audio.min_duration_sec")
            elif k == "min_voiced_sec":
                cfg.audio.min_voiced_sec = _as_float(v, "audio.min_voiced_sec")
            elif k == "wsl_source":
                cfg.audio.wsl_source = _as_str(v, "audio.wsl_source").strip().lower()
            elif k == "macos_device":
                cfg.audio.macos_device = _as_str(v, "audio.macos_device").strip()
            elif k == "beep":
                cfg.audio.beep = _as_bool(v, "audio.beep")
            elif k == "tail_drain_sec":
                cfg.audio.tail_drain_sec = _as_float(v, "audio.tail_drain_sec")
            else:
                raise ConfigError(f"unknown audio option {k!r}")

    asr = data.get("asr")
    if asr is not None:
        if not isinstance(asr, Mapping):
            raise ConfigError("[asr] must be a table/object")
        for k, v in asr.items():
            if v is None:
                continue
            if k == "provider":
                cfg.asr.provider = _as_str(v, "asr.provider").strip().lower()
            elif k == "model":
                cfg.asr.model = _as_str(v, "asr.model").strip()
            elif k == "resource_id":
                cfg.asr.resource_id = _as_str(v, "asr.resource_id").strip()
            elif k == "doubao_mode":
                cfg.asr.doubao_mode = _as_str(v, "asr.doubao_mode").strip().lower()
            elif k == "base_url":
                cfg.asr.base_url = _as_str(v, "asr.base_url").strip()
            elif k == "api_key_env":
                cfg.asr.api_key_env = _as_str(v, "asr.api_key_env").strip()
            elif k == "hotwords_file":
                cfg.asr.hotwords_file = _as_str(v, "asr.hotwords_file").strip()
            elif k == "timeout_sec":
                cfg.asr.timeout_sec = _as_float(v, "asr.timeout_sec")
            else:
                raise ConfigError(f"unknown asr option {k!r}")

    ref = data.get("refiner")
    if ref is not None:
        if not isinstance(ref, Mapping):
            raise ConfigError("[refiner] must be a table/object")
        for k, v in ref.items():
            if v is None:
                continue
            if k == "provider":
                cfg.refiner.provider = _normalize_refiner_provider(v)
            elif k == "model":
                cfg.refiner.model = _as_str(v, "refiner.model").strip()
            elif k == "effort":
                cfg.refiner.effort = _as_str(v, "refiner.effort").strip().lower()
            elif k == "base_url":
                cfg.refiner.base_url = _as_str(v, "refiner.base_url").strip()
            elif k == "api_key_env":
                cfg.refiner.api_key_env = _as_str(v, "refiner.api_key_env").strip()
            elif k == "max_turns_per_thread":
                cfg.refiner.max_turns_per_thread = _as_int(v, "refiner.max_turns_per_thread")
            elif k == "timeout_sec":
                cfg.refiner.timeout_sec = _as_float(v, "refiner.timeout_sec")
            elif k == "force_on_single_hop":
                cfg.refiner.force_on_single_hop = _as_bool(v, "refiner.force_on_single_hop")
            else:
                raise ConfigError(f"unknown refiner option {k!r}")


def _apply_env_overrides(cfg: Config, env: Mapping[str, str]) -> None:
    def _get(*suffixes: str) -> tuple[str, str] | None:
        for prefix in ("AGENT_VOICE_INPUT_", "VOICE_INPUT_"):
            for sfx in suffixes:
                name = f"{prefix}{sfx}"
                val = env.get(name)
                if val is not None and val.strip() != "":
                    return name, val.strip()
        return None

    if (kv := _get("HOTKEY")) is not None:
        cfg.hotkey.key = kv[1]
    if (kv := _get("TRIGGER_MODE")) is not None:
        cfg.hotkey.trigger_mode = kv[1].lower()
    if (kv := _get("TAP_MAX_MS")) is not None:
        cfg.hotkey.tap_max_ms = _as_int(kv[1], kv[0])
    if (kv := _get("DEBOUNCE_MS")) is not None:
        cfg.hotkey.debounce_ms = _as_int(kv[1], kv[0])

    if (kv := _get("UI_LANGUAGE")) is not None:
        cfg.ui.language = normalize_ui_language(kv[1], kv[0])

    if (kv := _get("AUDIO_FLOOR_DBFS")) is not None:
        cfg.audio.floor_dbfs = _as_float(kv[1], kv[0])
    if (kv := _get("AUDIO_TARGET_DBFS")) is not None:
        cfg.audio.target_dbfs = _as_float(kv[1], kv[0])
    if (kv := _get("AUDIO_MAX_GAIN_DB")) is not None:
        cfg.audio.max_gain_db = _as_float(kv[1], kv[0])
    if (kv := _get("AUDIO_MIN_DURATION_SEC")) is not None:
        cfg.audio.min_duration_sec = _as_float(kv[1], kv[0])
    if (kv := _get("AUDIO_MIN_VOICED_SEC")) is not None:
        cfg.audio.min_voiced_sec = _as_float(kv[1], kv[0])
    if (kv := _get("WSL_SOURCE")) is not None:
        cfg.audio.wsl_source = kv[1].lower()
    if (kv := _get("MACOS_AUDIO_DEVICE", "MACOS_DEVICE")) is not None:
        cfg.audio.macos_device = kv[1]
    if (kv := _get("BEEP")) is not None:
        cfg.audio.beep = _as_bool(kv[1], kv[0])
    if (kv := _get("TAIL_DRAIN_SEC", "AUDIO_TAIL_DRAIN_SEC")) is not None:
        cfg.audio.tail_drain_sec = _as_float(kv[1], kv[0])

    if (kv := _get("ASR_PROVIDER")) is not None:
        cfg.asr.provider = kv[1].lower()
    if (kv := _get("ASR_MODEL")) is not None:
        cfg.asr.model = kv[1]
    if (kv := _get("ASR_RESOURCE_ID")) is not None:
        cfg.asr.resource_id = kv[1]
    if (kv := _get("ASR_DOUBAO_MODE")) is not None:
        cfg.asr.doubao_mode = kv[1].lower()
    if (kv := _get("ASR_BASE_URL")) is not None:
        cfg.asr.base_url = kv[1]
    if (kv := _get("ASR_API_KEY_ENV")) is not None:
        cfg.asr.api_key_env = kv[1]
    if (kv := _get("HOTWORDS_FILE")) is not None:
        cfg.asr.hotwords_file = kv[1]
    if (kv := _get("ASR_TIMEOUT_SEC")) is not None:
        cfg.asr.timeout_sec = _as_float(kv[1], kv[0])

    if (kv := _get("REFINER")) is not None:
        cfg.refiner.provider = _normalize_refiner_provider(kv[1])
    if (kv := _get("REFINER_MODEL")) is not None:
        cfg.refiner.model = kv[1]
    if (kv := _get("REFINER_EFFORT")) is not None:
        cfg.refiner.effort = kv[1].lower()
    if (kv := _get("REFINER_BASE_URL")) is not None:
        cfg.refiner.base_url = kv[1]
    if (kv := _get("REFINER_API_KEY_ENV")) is not None:
        cfg.refiner.api_key_env = kv[1]
    if (kv := _get("REFINER_MAX_TURNS")) is not None:
        cfg.refiner.max_turns_per_thread = _as_int(kv[1], kv[0])
    if (kv := _get("REFINER_TIMEOUT_SEC")) is not None:
        cfg.refiner.timeout_sec = _as_float(kv[1], kv[0])
    if (kv := _get("FORCE_REFINE")) is not None:
        cfg.refiner.force_on_single_hop = _as_bool(kv[1], kv[0])


def _expand_cli_overrides(cli_overrides: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    nested: dict[str, dict[str, Any]] = {}
    for k, v in cli_overrides.items():
        if v is None:
            continue
        if isinstance(v, Mapping):
            sec = nested.setdefault(k, {})
            for sk, sv in v.items():
                if sv is not None:
                    sec[sk] = sv
        elif "." in k:
            sec_name, opt_name = k.split(".", 1)
            nested.setdefault(sec_name, {})[opt_name] = v
        else:
            raise ConfigError(f"invalid CLI override key {k!r}")
    return nested


def _validate_config(cfg: Config) -> None:
    if not cfg.hotkey.key:
        raise ConfigError("hotkey.key must not be empty")
    if cfg.hotkey.trigger_mode not in VALID_TRIGGER_MODES:
        raise ConfigError(
            f"hotkey.trigger_mode must be one of {sorted(VALID_TRIGGER_MODES)}, "
            f"got {cfg.hotkey.trigger_mode!r}"
        )
    if cfg.hotkey.tap_max_ms <= 0:
        raise ConfigError("hotkey.tap_max_ms must be > 0")
    if cfg.hotkey.debounce_ms < 0:
        raise ConfigError("hotkey.debounce_ms must be >= 0")

    cfg.ui.language = normalize_ui_language(cfg.ui.language, "ui.language")

    if cfg.audio.wsl_source not in VALID_WSL_SOURCES:
        raise ConfigError(
            f"audio.wsl_source must be one of {sorted(VALID_WSL_SOURCES)}, "
            f"got {cfg.audio.wsl_source!r}"
        )
    if not cfg.audio.macos_device or not cfg.audio.macos_device.strip():
        raise ConfigError("audio.macos_device must not be empty")
    if cfg.audio.min_duration_sec < 0 or cfg.audio.min_voiced_sec < 0:
        raise ConfigError("audio min_duration_sec and min_voiced_sec must be >= 0")
    if not (-120.0 <= cfg.audio.floor_dbfs < cfg.audio.target_dbfs <= 0.0):
        raise ConfigError(
            "audio dBFS bounds must satisfy -120.0 <= floor_dbfs < target_dbfs <= 0.0"
        )
    if not (0.0 <= cfg.audio.max_gain_db <= 120.0):
        raise ConfigError("audio.max_gain_db must satisfy 0.0 <= max_gain_db <= 120.0")
    if cfg.audio.tail_drain_sec is not None and not (0.0 <= cfg.audio.tail_drain_sec <= 5.0):
        raise ConfigError("audio.tail_drain_sec must satisfy 0.0 <= tail_drain_sec <= 5.0")

    if cfg.asr.provider not in VALID_ASR_PROVIDERS:
        raise ConfigError(
            f"asr.provider must be one of {sorted(VALID_ASR_PROVIDERS)}, got {cfg.asr.provider!r}"
        )
    if cfg.asr.doubao_mode not in VALID_DOUBAO_MODES:
        raise ConfigError(
            f"asr.doubao_mode must be one of {sorted(VALID_DOUBAO_MODES)}, "
            f"got {cfg.asr.doubao_mode!r}"
        )
    if cfg.asr.timeout_sec <= 0:
        raise ConfigError("asr.timeout_sec must be > 0")

    if cfg.refiner.provider not in VALID_REFINER_PROVIDERS:
        raise ConfigError(
            f"refiner.provider must be one of {sorted(VALID_REFINER_PROVIDERS)}, "
            f"got {cfg.refiner.provider!r}"
        )
    if cfg.refiner.provider == "codex-luna" and cfg.refiner.effort not in VALID_REFINER_EFFORTS:
        raise ConfigError(
            f"refiner.effort for codex-luna must be one of {sorted(VALID_REFINER_EFFORTS)}, "
            f"got {cfg.refiner.effort!r}"
        )
    if cfg.refiner.max_turns_per_thread <= 0:
        raise ConfigError("refiner.max_turns_per_thread must be > 0")
    if cfg.refiner.timeout_sec <= 0:
        raise ConfigError("refiner.timeout_sec must be > 0")


def _load_config_file(path: Path) -> dict[str, Any]:
    try:
        raw_text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from None
    if path.suffix.lower() == ".json":
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path}: invalid JSON at line {exc.lineno}: {exc.msg}") from None
        if not isinstance(parsed, dict):
            raise ConfigError(f"{path}: JSON config root must be an object")
        return parsed
    return parse_toml(raw_text)


def load_config(
    *,
    config_path: str | Path | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    env_paths: Sequence[str | Path] | None = None,
    home_dir: str | Path | None = None,
    load_default_env_files: bool = True,
) -> tuple[Config, dict[str, str]]:
    """Load merged ``(Config, merged_env)`` following CLI > AGENT_VOICE_INPUT_* > VOICE_INPUT_* > file > defaults."""
    effective_env = (
        load_env_files(env, env_paths=env_paths, home_dir=home_dir)
        if load_default_env_files
        else dict(os.environ if env is None else env)
    )
    home = (
        Path(home_dir)
        if home_dir is not None
        else Path((effective_env.get("HOME") or "").strip() or Path.home())
    )

    chosen_path: Path | None = None
    if config_path is not None and str(config_path).strip():
        chosen_path = expand_user_path(config_path, env=effective_env, home_dir=home)
        if not chosen_path.is_file():
            raise ConfigError(f"config file does not exist: {chosen_path}")
    elif effective_env.get("AGENT_VOICE_INPUT_CONFIG", "").strip():
        chosen_path = expand_user_path(
            effective_env["AGENT_VOICE_INPUT_CONFIG"], env=effective_env, home_dir=home
        )
        if not chosen_path.is_file():
            raise ConfigError(f"AGENT_VOICE_INPUT_CONFIG file does not exist: {chosen_path}")
    elif effective_env.get("VOICE_INPUT_CONFIG", "").strip():
        chosen_path = expand_user_path(
            effective_env["VOICE_INPUT_CONFIG"], env=effective_env, home_dir=home
        )
        if not chosen_path.is_file():
            raise ConfigError(f"VOICE_INPUT_CONFIG file does not exist: {chosen_path}")
    else:
        for candidate in (
            home / ".config" / "agent-voice-input" / "config.toml",
            home / ".config" / "agent-voice-input" / "config.json",
            home / ".config" / "voice-input" / "config.toml",
            home / ".config" / "voice-input" / "config.json",
        ):
            if candidate.is_file():
                chosen_path = candidate
                break

    cfg = Config()
    if chosen_path is not None:
        file_data = _load_config_file(chosen_path)
        _apply_section_dict(cfg, file_data)
        cfg.config_path = str(chosen_path)

    _apply_env_overrides(cfg, effective_env)

    if cli_overrides:
        _apply_section_dict(cfg, _expand_cli_overrides(cli_overrides))

    _validate_config(cfg)
    return cfg, effective_env


# ---------------------------------------------------------------------------
# Credential resolution & provider auto-selection
# ---------------------------------------------------------------------------


def _first_present_credential(
    env: Mapping[str, str], candidates: Sequence[str]
) -> CredentialItem | None:
    for var_name in candidates:
        val = env.get(var_name)
        if val is not None and val.strip():
            return CredentialItem(value=val.strip(), source_env=var_name)
    return None


def resolve_credentials(
    cfg: Config | None, env: Mapping[str, str] | None = None
) -> ProviderCredentials:
    """Resolve provider credentials strictly honoring suffix-first priority."""
    src_env = os.environ if env is None else env
    asr_custom: CredentialItem | None = None
    refiner_custom: CredentialItem | None = None
    if cfg is not None:
        if cfg.asr.api_key_env.strip():
            asr_custom = _first_present_credential(src_env, (cfg.asr.api_key_env.strip(),))
        if cfg.refiner.api_key_env.strip():
            refiner_custom = _first_present_credential(src_env, (cfg.refiner.api_key_env.strip(),))

    return ProviderCredentials(
        ali=_first_present_credential(src_env, CREDENTIAL_ENV_ORDER["ali"]),
        huoshan=_first_present_credential(src_env, CREDENTIAL_ENV_ORDER["huoshan"]),
        doubao_app_id=_first_present_credential(src_env, CREDENTIAL_ENV_ORDER["doubao_app_id"]),
        doubao_key=_first_present_credential(src_env, CREDENTIAL_ENV_ORDER["doubao_key"]),
        gemini=_first_present_credential(src_env, CREDENTIAL_ENV_ORDER["gemini"]),
        openai=_first_present_credential(src_env, CREDENTIAL_ENV_ORDER["openai"]),
        asr_custom=asr_custom,
        refiner_custom=refiner_custom,
    )


def is_volces_host(url: str) -> bool:
    """Strictly check whether ``url``'s hostname belongs to ``volces.com``."""
    try:
        host = (urllib.parse.urlsplit(url.strip()).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return host == "volces.com" or host.endswith(".volces.com")


def _host_matches_domain(url: str, *domains: str) -> bool:
    try:
        host = (urllib.parse.urlsplit(url.strip()).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if not host:
        return False
    return any(host == d or host.endswith(f".{d}") for d in domains)


def _infer_provider_from_explicit_asr_hints(asr_cfg: AsrConfig) -> str | None:
    """Infer target ASR provider when ``provider='auto'`` has explicit url/model/api_key_env."""
    url = asr_cfg.base_url.strip()
    model = asr_cfg.model.strip().lower()
    key_env = asr_cfg.api_key_env.strip().upper()
    rid = asr_cfg.resource_id.strip().lower()
    dmode = asr_cfg.doubao_mode.strip().lower()

    if url:
        if _host_matches_domain(url, "aliyuncs.com", "dashscope.aliyuncs.com"):
            return "ali"
        if _host_matches_domain(url, "bytedance.com"):
            return "doubao"
        if _host_matches_domain(url, "googleapis.com"):
            return "gemini"
        return "openai-compatible"

    if model:
        if model.startswith(("qwen", "fun-asr", "paraformer", "sensevoice")):
            return "ali"
        if model == "bigmodel" or "seedasr" in model or "bigasr" in model:
            return "doubao"
        if model.startswith("gemini"):
            return "gemini"
        if "whisper" in model or "transcribe" in model:
            return "openai-compatible"

    if (rid and rid != "auto") or (dmode and dmode != "auto"):
        return "doubao"

    if key_env:
        if any(tok in key_env for tok in ("ALI", "DASHSCOPE", "QWEN")):
            return "ali"
        if "DOUBAO" in key_env:
            return "doubao"
        if "GEMINI" in key_env or "GOOGLE" in key_env:
            return "gemini"
        # Third-party custom key env (e.g. GROQ_API_KEY / OPENAI_* / HUOSHAN_*) -> openai-compatible
        return "openai-compatible"

    return None


def select_asr_provider(cfg: Config, creds: ProviderCredentials) -> str:
    """Resolve effective canonical ASR provider name without cross-vendor key leakage."""
    prov = cfg.asr.canonical_provider()
    if prov != "auto":
        return prov

    inferred = _infer_provider_from_explicit_asr_hints(cfg.asr)
    if inferred is not None:
        return inferred

    if creds.ali is not None:
        return "ali"
    if creds.doubao_key is not None:
        return "doubao"
    if creds.gemini is not None:
        return "gemini"
    if creds.openai is not None:
        return "openai-compatible"
    return "ali"


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------

_BEARER_RE = re.compile(r"(\bBearer\s+)[A-Za-z0-9._~+/=-]{4,}", re.IGNORECASE)
_SK_TOKEN_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{6,}")
_AIZA_TOKEN_RE = re.compile(r"\bAIza[0-9A-Za-z_-]{12,}")
_URL_USERINFO_SECRET_RE = re.compile(r"(https?://[^/\s:@]+:)[^/\s@]+(@)", re.IGNORECASE)
_QUOTED_KEYVAL_SECRET_RE = re.compile(
    r"""((['"])(?:x-api-key|x-api-access-key|x-api-app-key|x-goog-api-key|authorization|"""
    r"""api_?key|access_?key|app_?id|api_?id|app_?key|secret|token|password)\2\s*:\s*)(['"])"""
    r"""(?:\\.|(?!\3)[^\\])*?\3""",
    re.IGNORECASE | re.DOTALL,
)
_ESCAPED_QUOTED_KEYVAL_SECRET_RE = re.compile(
    r"""(\\(['"])(?:x-api-key|x-api-access-key|x-api-app-key|x-goog-api-key|authorization|"""
    r"""api_?key|access_?key|app_?id|api_?id|app_?key|secret|token|password)\\\2\s*:\s*)(\\(['"]))"""
    r"""(?:(?!\4)[^\\]|\\\\\\\\|\\\\\\\4|\\\\\\(?!\\|\4)[^\\]|\\\\(?!\\|\4)[^\\]|\\(?!\\|\4)[^\\])*(\\\4)""",
    re.IGNORECASE | re.DOTALL,
)
_AUTH_HEADER_LINE_RE = re.compile(
    r"(\bauthorization\s*[:=]\s*)(?:(['\"])(?:\\[^\r\n]|(?!\2)[^\\\r\n])*\2|"
    r"(?:(?:Bearer|Basic|Token|ApiKey|Digest)\s+)?[^\s\"',;}\]\r\n]+)",
    re.IGNORECASE,
)
_UNQUOTED_HEADER_SECRET_RE = re.compile(
    r"((?:x-api-key|x-api-access-key|x-api-app-key|x-goog-api-key)\s*[:=]\s*)"
    r"(?:(['\"])(?:\\[^\r\n]|(?!\2)[^\\\r\n])*\2|[^\s\"',;}\]\r\n]+)",
    re.IGNORECASE,
)
_QUERY_SECRET_RE = re.compile(
    r"([?&](?:key|api_key|access_key|access_token|token)=)[^&\s\"']+",
    re.IGNORECASE,
)


def _is_secret_env_var_name(name: str) -> bool:
    upper = name.upper()
    if upper in _NON_SECRET_ENV_NAMES:
        return False
    if upper.endswith(_NON_SECRET_ENV_SUFFIXES):
        return False
    return bool(_SENSITIVE_ENV_NAME_RE.search(upper))


def redact_secrets(
    text: str,
    env: Mapping[str, str] | None = None,
    *,
    cfg: Config | None = None,
    extra_secrets: Iterable[str] = (),
) -> str:
    """Replace all sensitive env values (len >= 4) and token patterns with ``[REDACTED]``."""
    if not text:
        return text

    src_env = os.environ if env is None else env
    raw_secrets: set[str] = set()

    for group in CREDENTIAL_ENV_ORDER.values():
        for name in group:
            val = src_env.get(name)
            if val and len(val.strip()) >= 4:
                raw_secrets.add(val.strip())

    # Follow configured custom API key env var pointers from env and Config
    ptr_names: list[str] = [
        (src_env.get("AGENT_VOICE_INPUT_ASR_API_KEY_ENV") or "").strip(),
        (src_env.get("VOICE_INPUT_ASR_API_KEY_ENV") or "").strip(),
        (src_env.get("AGENT_VOICE_INPUT_REFINER_API_KEY_ENV") or "").strip(),
        (src_env.get("VOICE_INPUT_REFINER_API_KEY_ENV") or "").strip(),
    ]
    if cfg is not None:
        ptr_names.append(cfg.asr.api_key_env.strip())
        ptr_names.append(cfg.refiner.api_key_env.strip())

    for custom_name in ptr_names:
        if custom_name:
            cval = src_env.get(custom_name)
            if cval and len(cval.strip()) >= 4:
                raw_secrets.add(cval.strip())

    for name, val in src_env.items():
        if _is_secret_env_var_name(name) and val and len(val.strip()) >= 4:
            raw_secrets.add(val.strip())

    for s in extra_secrets:
        if s and len(s.strip()) >= 4:
            raw_secrets.add(s.strip())

    candidates: set[str] = set()
    for secret in raw_secrets:
        candidates.add(secret)
        pct = urllib.parse.quote(secret, safe="")
        if len(pct) >= 4:
            candidates.add(pct)
        json_esc = json.dumps(secret)[1:-1]
        if len(json_esc) >= 4:
            candidates.add(json_esc)

    out = text
    for secret in sorted(candidates, key=len, reverse=True):
        out = out.replace(secret, "[REDACTED]")

    out = _URL_USERINFO_SECRET_RE.sub(r"\1[REDACTED]\2", out)
    out = _ESCAPED_QUOTED_KEYVAL_SECRET_RE.sub(r"\1\3[REDACTED]\5", out)
    out = _QUOTED_KEYVAL_SECRET_RE.sub(r"\1\3[REDACTED]\3", out)
    out = _AUTH_HEADER_LINE_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]{m.group(2)}"
        if m.group(2)
        else f"{m.group(1)}[REDACTED]",
        out,
    )
    out = _UNQUOTED_HEADER_SECRET_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]{m.group(2)}"
        if m.group(2)
        else f"{m.group(1)}[REDACTED]",
        out,
    )
    out = _BEARER_RE.sub(r"\1[REDACTED]", out)
    out = _QUERY_SECRET_RE.sub(r"\1[REDACTED]", out)
    out = _SK_TOKEN_RE.sub("[REDACTED]", out)
    out = _AIZA_TOKEN_RE.sub("[REDACTED]", out)
    return out
