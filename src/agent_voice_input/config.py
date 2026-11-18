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
VALID_HOTKEY_SCOPES = frozenset({"all", "coding", "terminals"})
VALID_UI_LANGUAGES = frozenset({"auto", "zh", "en"})
VALID_UI_POSITIONS = frozenset({"bottom", "top"})
VALID_WSL_SOURCES = frozenset({"auto", "pulse", "winmm", "wasapi"})
VALID_ASR_PROVIDERS = frozenset(
    {
        "auto",
        "dashscope",
        "doubao",
        "openrouter",
        "gemini",
        "openai",
        "groq",
        "siliconflow",
        "openai-compatible",
    }
)
VALID_ASR_APIS = frozenset({"auto", "audio-transcriptions", "chat-completions"})
VALID_DOUBAO_MODES = frozenset({"auto", "flash", "submit"})
VALID_REFINER_PROVIDERS = frozenset(
    {
        "codex",
        "dashscope",
        "volcengine",
        "openrouter",
        "gemini",
        "openai",
        "groq",
        "siliconflow",
        "openai-compatible",
        "off",
    }
)
VALID_REFINER_APIS = frozenset({"auto", "chat-completions", "responses"})
VALID_EFFORTS = frozenset(
    {"auto", "default", "none", "minimal", "low", "medium", "high", "xhigh", "max"}
)
VALID_REFINER_EFFORTS = VALID_EFFORTS

ASR_AUTO_PRIORITY: tuple[str, ...] = (
    "dashscope",
    "doubao",
    "openrouter",
    "gemini",
    "openai",
    "groq",
    "siliconflow",
)

DEFAULT_ASR_MODELS: dict[str, str] = {
    "dashscope": "qwen3-asr-flash",
    "doubao": "bigmodel",
    "openrouter": "qwen/qwen3-asr-1.7b",
    "gemini": "gemini-2.5-flash",
    "openai": "gpt-4o-mini-transcribe",
    "groq": "whisper-large-v3-turbo",
    "siliconflow": "FunAudioLLM/SenseVoiceSmall",
    "openai-compatible": "gpt-4o-mini-transcribe",
}

DEFAULT_REFINER_MODELS: dict[str, str] = {
    "codex": "gpt-6-luna",
    "dashscope": "qwen-turbo-latest",
    "volcengine": "doubao-seed-1-6-flash",
    "openrouter": "google/gemini-2.5-flash-lite",
    "gemini": "gemini-2.5-flash-lite",
    "openai": "gpt-4o-mini",
    "groq": "llama-3.3-70b-versatile",
    "siliconflow": "Qwen/Qwen2.5-7B-Instruct",
}

DEFAULT_ASR_BASE_URLS: dict[str, str] = {
    "doubao": "https://openspeech.bytedance.com/api/v3/auc/bigmodel",
    "openrouter": "https://openrouter.ai/api/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "siliconflow": "https://api.siliconflow.cn/v1",
}

DEFAULT_REFINER_BASE_URLS: dict[str, str] = {
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "volcengine": "https://ark.cn-beijing.volces.com/api/v3",
    "openrouter": "https://openrouter.ai/api/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "siliconflow": "https://api.siliconflow.cn/v1",
}

# Strict credential lookup priority (canonical vendor env var first, legacy aliases second).
# REDLINE: DOUBAO_ASR_API_ID / API_ID_DOUBAO_ASR typos MUST NEVER be added here.
CREDENTIAL_ENV_ORDER: dict[str, tuple[str, ...]] = {
    "dashscope": ("DASHSCOPE_API_KEY", "ALI_API_KEY", "API_KEY_ALI"),
    "doubao_app_id": ("DOUBAO_ASR_APP_ID", "APP_ID_DOUBAO_ASR"),
    "doubao_key": ("DOUBAO_ASR_API_KEY", "API_KEY_DOUBAO_ASR"),
    "volcengine": ("ARK_API_KEY", "VOLCENGINE_API_KEY", "HUOSHAN_API_KEY", "API_KEY_HUOSHAN"),
    "openrouter": ("OPENROUTER_API_KEY", "API_KEY_OPENROUTER"),
    "gemini": ("GEMINI_API_KEY", "API_KEY_GEMINI"),
    "openai": ("OPENAI_API_KEY", "API_KEY_OPENAI"),
    "groq": ("GROQ_API_KEY", "API_KEY_GROQ"),
    "siliconflow": ("SILICONFLOW_API_KEY", "API_KEY_SILICONFLOW"),
}

_SENSITIVE_ENV_NAME_RE = re.compile(
    r"(?:^|_)(?:API_KEY|ACCESS_KEY|APP_ID|API_ID|APP_KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|PRIVATE_KEY)(?:$|_)",
    re.IGNORECASE,
)
_NON_SECRET_ENV_NAMES = frozenset(
    {
        "AGENT_VOICE_INPUT_TMUX_HINT",
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


_UNSET: Any = object()


@dataclass
class HotkeyConfig:
    key: str = "RControl"
    trigger_mode: str = "tap_toggle"
    tap_max_ms: int = 1200
    debounce_ms: int = 180
    scope: str = "all"
    yield_remote_desktop: bool = True
    ignore_apps: tuple[str, ...] = ()

    def __init__(
        self,
        key: str = "RControl",
        trigger_mode: str = "tap_toggle",
        tap_max_ms: int = 1200,
        debounce_ms: int = 180,
        only_in_terminals: Any = _UNSET,
        *,
        scope: Any = _UNSET,
        yield_remote_desktop: bool = True,
        ignore_apps: tuple[str, ...] = (),
    ) -> None:
        self.key = key
        self.trigger_mode = trigger_mode
        self.tap_max_ms = tap_max_ms
        self.debounce_ms = debounce_ms
        self.yield_remote_desktop = yield_remote_desktop
        self.ignore_apps = (
            tuple(ignore_apps) if isinstance(ignore_apps, (list, tuple)) else ignore_apps
        )
        if scope is not _UNSET and scope is not None:
            self.scope = scope.strip().lower() if isinstance(scope, str) else scope
        elif only_in_terminals is not _UNSET and only_in_terminals is not None:
            if not isinstance(only_in_terminals, bool):
                raise ConfigError(
                    f"hotkey.only_in_terminals: invalid boolean value {only_in_terminals!r}"
                )
            self.scope = "terminals" if only_in_terminals else "all"
        else:
            self.scope = "all"

    @property
    def only_in_terminals(self) -> bool:
        """Deprecated compatibility alias synced with ``scope == 'terminals'``."""
        return self.scope == "terminals"

    @only_in_terminals.setter
    def only_in_terminals(self, val: bool) -> None:
        if not isinstance(val, bool):
            raise ConfigError(f"hotkey.only_in_terminals: invalid boolean value {val!r}")
        self.scope = "terminals" if val else "all"


@dataclass
class UiConfig:
    language: str = "auto"
    position: str = "bottom"


DEFAULT_WSL_TAIL_DRAIN_SEC = 0.65
DEFAULT_MACOS_TAIL_DRAIN_SEC = 0.20


@dataclass
class AudioConfig:
    floor_dbfs: float = -50.0
    target_dbfs: float = -20.0
    max_gain_db: float = 40.0
    min_duration_sec: float = 0.3
    min_voiced_sec: float = 0.1
    max_duration_sec: float = 600.0
    wsl_source: str = "auto"
    macos_device: str = "default"
    beep: bool = False
    tail_drain_sec: float | None = None
    save_recordings: bool = False
    save_recordings_max_count: int = 300

    def effective_tail_drain_sec(self, *, macos: bool = False) -> float:
        if self.tail_drain_sec is not None:
            return self.tail_drain_sec
        return DEFAULT_MACOS_TAIL_DRAIN_SEC if macos else DEFAULT_WSL_TAIL_DRAIN_SEC


@dataclass
class AsrConfig:
    provider: str = "auto"
    model: str = ""
    api: str = "auto"
    effort: str = "auto"
    resource_id: str = "auto"
    doubao_mode: str = "auto"
    base_url: str = ""
    api_key_env: str = ""
    hotwords_file: str = "~/.config/agent-voice-input/hotwords.txt"
    timeout_sec: float = 60.0
    max_retries: int = 2

    def canonical_provider(self) -> str:
        return self.provider.strip().lower()

    def effective_model(self, resolved_provider: str | None = None) -> str:
        if self.model and self.model.strip():
            return self.model.strip()
        prov = (resolved_provider or self.provider).strip().lower()
        return DEFAULT_ASR_MODELS.get(prov, DEFAULT_ASR_MODELS["dashscope"])

    def effective_base_url(self, resolved_provider: str | None = None) -> str:
        if self.base_url and self.base_url.strip():
            return self.base_url.strip()
        prov = (resolved_provider or self.provider).strip().lower()
        return DEFAULT_ASR_BASE_URLS.get(prov, "")


@dataclass
class RefinerConfig:
    provider: str = "codex"
    model: str = ""
    api: str = "auto"
    effort: str = "low"
    prompt: str = ""
    include_git_status: bool = True
    context_max_chars: int = 200000
    base_url: str = ""
    api_key_env: str = ""
    max_turns_per_thread: int = 20
    timeout_sec: float = 90.0
    max_retries: int = 2
    force_on_single_hop: bool = False

    def __post_init__(self) -> None:
        if not self.model or not self.model.strip():
            prov = self.provider.strip().lower()
            if prov in ("none", "false", "0", "disabled"):
                prov = "off"
            self.model = DEFAULT_REFINER_MODELS.get(prov, "")

    def effective_model(self, resolved_provider: str | None = None) -> str:
        if self.model and self.model.strip():
            return self.model.strip()
        prov = (resolved_provider or self.provider).strip().lower()
        return DEFAULT_REFINER_MODELS.get(prov, "")

    def effective_base_url(self, resolved_provider: str | None = None) -> str:
        if self.base_url and self.base_url.strip():
            return self.base_url.strip()
        prov = (resolved_provider or self.provider).strip().lower()
        return DEFAULT_REFINER_BASE_URLS.get(prov, "")

    def effective_api(self, resolved_provider: str | None = None) -> str:
        if not isinstance(self.api, str):
            raise ConfigError(f"refiner.api: expected string, got {type(self.api).__name__}")
        req_api = (self.api or "auto").strip().lower()
        if req_api not in VALID_REFINER_APIS:
            raise ConfigError(
                f"refiner.api must be one of {sorted(VALID_REFINER_APIS)}, got {self.api!r}"
            )
        prov = (resolved_provider or self.provider).strip().lower()
        if prov in ("", "none", "false", "0", "disabled"):
            prov = "off"
        if prov == "off":
            return "off"
        if prov == "gemini":
            if req_api != "auto":
                raise ConfigError(
                    f"refiner.api={req_api!r} is incompatible with provider 'gemini' "
                    "(uses native Gemini :generateContent; keep refiner.api='auto')"
                )
            return "gemini" if is_gemini_native_url(self.base_url) else "chat-completions"
        if prov == "codex":
            if req_api == "chat-completions":
                raise ConfigError(
                    "refiner.api='chat-completions' is incompatible with provider 'codex' "
                    "(uses stateless 'responses' API; keep refiner.api='auto' or 'responses')"
                )
            return "responses"
        if req_api == "auto":
            return "chat-completions"
        return req_api


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
    dashscope: CredentialItem | None = None
    volcengine: CredentialItem | None = None
    doubao_app_id: CredentialItem | None = None
    doubao_key: CredentialItem | None = None
    openrouter: CredentialItem | None = None
    gemini: CredentialItem | None = None
    openai: CredentialItem | None = None
    groq: CredentialItem | None = None
    siliconflow: CredentialItem | None = None
    asr_custom: CredentialItem | None = None
    refiner_custom: CredentialItem | None = None

    def for_provider(self, provider: str) -> CredentialItem | None:
        prov = provider.strip().lower()
        mapping: dict[str, CredentialItem | None] = {
            "dashscope": self.dashscope,
            "volcengine": self.volcengine,
            "doubao": self.doubao_key,
            "openrouter": self.openrouter,
            "gemini": self.gemini,
            "openai": self.openai,
            "groq": self.groq,
            "siliconflow": self.siliconflow,
            "openai-compatible": self.openai,
        }
        return mapping.get(prov)

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


def resolve_saved_recordings_dir(
    env: Mapping[str, str] | None = None,
    *,
    uid: int | None = None,
    create: bool = False,
) -> Path:
    """Return ``${XDG_CACHE_HOME}/agent-voice-input/recordings`` (or ``~/.cache/agent-voice-input/recordings``)."""
    src_env = os.environ if env is None else env
    cache_home = (src_env.get("XDG_CACHE_HOME") or "").strip()
    if cache_home:
        rec_dir = expand_user_path(cache_home, env=src_env) / "agent-voice-input" / "recordings"
    else:
        raw_home = (src_env.get("HOME") or "").strip()
        base_home = expand_user_path(raw_home, env=src_env) if raw_home else Path.home()
        rec_dir = base_home / ".cache" / "agent-voice-input" / "recordings"
    if create:
        ensure_secure_dir(rec_dir, expected_uid=uid)
    return rec_dir


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


def _toml_lineno(src: str, pos: int) -> int:
    return src.count("\n", 0, max(0, min(pos, len(src)))) + 1


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


_TOML_BASIC_ESCAPES: dict[str, str] = {
    "b": "\b",
    "t": "\t",
    "n": "\n",
    "f": "\f",
    "r": "\r",
    '"': '"',
    "\\": "\\",
}


def _parse_toml_basic_string(src: str, pos: int, lineno: int) -> tuple[str, int]:
    """Parse single-line double-quoted string starting at ``pos`` (pointing to opening ``"``)."""
    i = pos + 1
    out: list[str] = []
    while i < len(src):
        ch = src[i]
        if ch == '"':
            return "".join(out), i + 1
        if ch in "\r\n":
            raise ConfigError(f"line {lineno}: unterminated string literal")
        if ch == "\\":
            esc_lineno = _toml_lineno(src, i)
            i += 1
            if i >= len(src) or src[i] in "\r\n":
                raise ConfigError(f"line {esc_lineno}: unterminated escape sequence")
            esc = src[i]
            if esc in _TOML_BASIC_ESCAPES:
                out.append(_TOML_BASIC_ESCAPES[esc])
                i += 1
            elif esc == "u":
                out.append(_decode_unicode_scalar(src[i + 1 : i + 5], 4, esc_lineno))
                i += 5
            elif esc == "U":
                out.append(_decode_unicode_scalar(src[i + 1 : i + 9], 8, esc_lineno))
                i += 9
            else:
                raise ConfigError(f"line {esc_lineno}: unsupported escape \\{esc}")
            continue
        if _is_disallowed_toml_ctrl(ch):
            raise ConfigError(f"line {_toml_lineno(src, i)}: unescaped control character in string")
        out.append(ch)
        i += 1
    raise ConfigError(f"line {lineno}: unterminated double-quoted string")


def _parse_toml_multiline_basic_string(src: str, pos: int, lineno: int) -> tuple[str, int]:
    """Parse TOML v1.0 multiline basic string ``\"\"\"...\"\"\"`` starting at ``pos``."""
    i = pos + 3
    n = len(src)
    if src.startswith("\r\n", i):
        i += 2
    elif i < n and src[i] == "\n":
        i += 1
    out: list[str] = []
    while i < n:
        if src.startswith('"""', i):
            qcount = 3
            while i + qcount < n and src[i + qcount] == '"':
                qcount += 1
            if qcount >= 6:
                raise ConfigError(
                    f"line {_toml_lineno(src, i)}: too many consecutive quotes in multiline string"
                )
            out.append('"' * (qcount - 3))
            return "".join(out), i + qcount
        ch = src[i]
        if ch == "\\":
            # TOML line-ending backslash trims trailing whitespace + newline(s)
            j = i + 1
            while j < n and src[j] in " \t":
                j += 1
            if j < n and (src[j] == "\n" or src.startswith("\r\n", j)):
                i = j
                while i < n:
                    if src.startswith("\r\n", i):
                        i += 2
                    elif src[i] in " \t\n":
                        i += 1
                    else:
                        break
                continue
            esc_lineno = _toml_lineno(src, i)
            i += 1
            if i >= n:
                raise ConfigError(f"line {esc_lineno}: unterminated escape sequence")
            esc = src[i]
            if esc in _TOML_BASIC_ESCAPES:
                out.append(_TOML_BASIC_ESCAPES[esc])
                i += 1
            elif esc == "u":
                out.append(_decode_unicode_scalar(src[i + 1 : i + 5], 4, esc_lineno))
                i += 5
            elif esc == "U":
                out.append(_decode_unicode_scalar(src[i + 1 : i + 9], 8, esc_lineno))
                i += 9
            else:
                raise ConfigError(f"line {esc_lineno}: unsupported escape \\{esc}")
            continue
        if src.startswith("\r\n", i):
            out.append("\n")
            i += 2
            continue
        if ch == "\n":
            out.append("\n")
            i += 1
            continue
        if _is_disallowed_toml_ctrl(ch):
            raise ConfigError(f"line {_toml_lineno(src, i)}: unescaped control character in string")
        out.append(ch)
        i += 1
    raise ConfigError(f"line {lineno}: unterminated multiline double-quoted string")


def _parse_toml_literal_string(src: str, pos: int, lineno: int) -> tuple[str, int]:
    """Parse single-line literal string ``'...'`` starting at ``pos``."""
    i = pos + 1
    while i < len(src):
        ch = src[i]
        if ch == "'":
            return src[pos + 1 : i], i + 1
        if ch in "\r\n":
            raise ConfigError(f"line {lineno}: unterminated single-quoted string")
        if _is_disallowed_toml_ctrl(ch):
            raise ConfigError(f"line {_toml_lineno(src, i)}: control character in literal string")
        i += 1
    raise ConfigError(f"line {lineno}: unterminated single-quoted string")


def _parse_toml_multiline_literal_string(src: str, pos: int, lineno: int) -> tuple[str, int]:
    """Parse TOML v1.0 multiline literal string ``'''...'''`` starting at ``pos``."""
    i = pos + 3
    n = len(src)
    if src.startswith("\r\n", i):
        i += 2
    elif i < n and src[i] == "\n":
        i += 1
    out: list[str] = []
    while i < n:
        if src.startswith("'''", i):
            qcount = 3
            while i + qcount < n and src[i + qcount] == "'":
                qcount += 1
            if qcount >= 6:
                raise ConfigError(
                    f"line {_toml_lineno(src, i)}: too many consecutive quotes in multiline literal string"
                )
            out.append("'" * (qcount - 3))
            return "".join(out), i + qcount
        ch = src[i]
        if src.startswith("\r\n", i):
            out.append("\n")
            i += 2
            continue
        if ch == "\n":
            out.append("\n")
            i += 1
            continue
        if _is_disallowed_toml_ctrl(ch):
            raise ConfigError(f"line {_toml_lineno(src, i)}: control character in literal string")
        out.append(ch)
        i += 1
    raise ConfigError(f"line {lineno}: unterminated multiline single-quoted string")


def _skip_toml_inline_ws_and_comment(src: str, pos: int) -> int:
    n = len(src)
    while pos < n and src[pos] in " \t":
        pos += 1
    if pos < n and src[pos] == "#":
        pos += 1
        while pos < n:
            if src.startswith("\r\n", pos) or src[pos] == "\n":
                break
            if _is_disallowed_toml_ctrl(src[pos]):
                raise ConfigError(f"line {_toml_lineno(src, pos)}: disallowed control character")
            pos += 1
    return pos


def _skip_toml_ws_and_comments(src: str, pos: int) -> int:
    n = len(src)
    while pos < n:
        ch = src[pos]
        if ch in " \t\n":
            pos += 1
            continue
        if ch == "\r":
            if src.startswith("\r\n", pos):
                pos += 2
                continue
            raise ConfigError(f"line {_toml_lineno(src, pos)}: disallowed control character")
        if ch == "#":
            pos = _skip_toml_inline_ws_and_comment(src, pos)
            continue
        break
    return pos


def _expect_toml_eol_or_eof(src: str, pos: int) -> int:
    pos = _skip_toml_inline_ws_and_comment(src, pos)
    n = len(src)
    if pos >= n:
        return pos
    if src.startswith("\r\n", pos):
        return pos + 2
    if src[pos] == "\n":
        return pos + 1
    line_end = src.find("\n", pos)
    if line_end == -1:
        raw_rem = src[pos:]
    elif line_end > pos and src[line_end - 1] == "\r":
        raw_rem = src[pos : line_end - 1]
    else:
        raw_rem = src[pos:line_end]
    if any(_is_disallowed_toml_ctrl(c) for c in raw_rem):
        raise ConfigError(f"line {_toml_lineno(src, pos)}: disallowed control character")
    rem = raw_rem.strip()
    raise ConfigError(f"line {_toml_lineno(src, pos)}: unexpected trailing tokens {rem!r}")


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
            if s.startswith('"""', i):
                raise ConfigError(f"line {lineno}: multiline string not allowed in key")
            seg, i = _parse_toml_basic_string(s, i, lineno)
            parts.append(seg)
        elif s[i] == "'":
            if s.startswith("'''", i):
                raise ConfigError(f"line {lineno}: multiline string not allowed in key")
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


def _parse_toml_value(src: str, pos: int) -> tuple[Any, int]:
    pos = _skip_toml_ws_and_comments(src, pos)
    n = len(src)
    lineno = _toml_lineno(src, pos)
    if pos >= n:
        raise ConfigError(f"line {lineno}: missing value")
    ch = src[pos]
    if ch == '"':
        if src.startswith('"""', pos):
            return _parse_toml_multiline_basic_string(src, pos, lineno)
        return _parse_toml_basic_string(src, pos, lineno)
    if ch == "'":
        if src.startswith("'''", pos):
            return _parse_toml_multiline_literal_string(src, pos, lineno)
        return _parse_toml_literal_string(src, pos, lineno)
    if ch == "[":
        arr_lineno = lineno
        i = pos + 1
        items: list[Any] = []
        elem_tag: str | None = None
        while True:
            i = _skip_toml_ws_and_comments(src, i)
            if i >= n:
                raise ConfigError(f"line {arr_lineno}: unterminated array")
            if src[i] == "]":
                return items, i + 1
            elem_lineno = _toml_lineno(src, i)
            val, i = _parse_toml_value(src, i)
            vtag = _toml_type_tag(val)
            if elem_tag is None:
                elem_tag = vtag
            elif elem_tag != vtag:
                raise ConfigError(
                    f"line {elem_lineno}: heterogeneous array types ({elem_tag} vs {vtag})"
                )
            items.append(val)
            i = _skip_toml_ws_and_comments(src, i)
            if i >= n:
                raise ConfigError(f"line {arr_lineno}: unterminated array")
            if src[i] == ",":
                i += 1
                continue
            if src[i] == "]":
                return items, i + 1
            raise ConfigError(f"line {_toml_lineno(src, i)}: expected ',' or ']' in array")
    start = pos
    while pos < n and src[pos] not in " \t\r\n,]#":
        if _is_disallowed_toml_ctrl(src[pos]):
            raise ConfigError(f"line {_toml_lineno(src, pos)}: disallowed control character")
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


def _scan_toml_single_line_delim(src: str, pos: int, target: str) -> int:
    """Find unquoted single-line ``target`` char before EOL/comment, validating control chars."""
    n = len(src)
    in_single = False
    in_double = False
    escaped = False
    i = pos
    while i < n:
        ch = src[i]
        if ch == "\n" or src.startswith("\r\n", i):
            return -1
        if _is_disallowed_toml_ctrl(ch):
            raise ConfigError(f"line {_toml_lineno(src, i)}: disallowed control character")
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
            if ch == "#":
                return -1
            if ch == target:
                return i
            if ch == '"':
                in_double = True
            elif ch == "'":
                in_single = True
        i += 1
    return -1


def _parse_simple_toml(text: str) -> dict[str, Any]:
    """Pure-stdlib TOML parser for Python 3.10 fallback (supports multiline strings & arrays)."""
    if text.startswith("\ufeff"):
        text = text[1:]
    root: dict[str, Any] = {}
    current_table: dict[str, Any] = root
    explicit_tables: set[tuple[str, ...]] = set()

    pos = 0
    n = len(text)
    while True:
        pos = _skip_toml_ws_and_comments(text, pos)
        if pos >= n:
            break
        lineno = _toml_lineno(text, pos)

        if text[pos] == "[":
            if text.startswith("[[", pos):
                raise ConfigError(f"line {lineno}: array of tables [[...]] is not supported")
            close_idx = _scan_toml_single_line_delim(text, pos + 1, "]")
            if close_idx == -1:
                line_end = text.find("\n", pos)
                snippet = (text[pos:] if line_end == -1 else text[pos:line_end]).rstrip("\r").strip()
                raise ConfigError(f"line {lineno}: invalid table header {snippet!r}")
            header_body = text[pos + 1 : close_idx].strip()
            path_parts = _parse_toml_key_path(header_body, lineno)
            pos = _expect_toml_eol_or_eof(text, close_idx + 1)
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

        eq_pos = _scan_toml_single_line_delim(text, pos, "=")
        if eq_pos == -1:
            # Ensure control chars on the rest of the malformed line are still caught
            line_end = text.find("\n", pos)
            rest = text[pos:] if line_end == -1 else text[pos:line_end]
            if rest.endswith("\r"):
                rest = rest[:-1]
            for off, ch in enumerate(rest):
                if ch == "#":
                    _skip_toml_inline_ws_and_comment(text, pos + off)
                    break
                if _is_disallowed_toml_ctrl(ch):
                    raise ConfigError(f"line {_toml_lineno(text, pos + off)}: disallowed control character")
            raise ConfigError(f"line {lineno}: expected 'key = value'")

        key_parts = _parse_toml_key_path(text[pos:eq_pos], lineno)
        vpos = eq_pos + 1
        while vpos < n and text[vpos] in " \t":
            vpos += 1
        if vpos >= n or text[vpos] in "\r\n#":
            if vpos < n and text[vpos] == "#":
                _skip_toml_inline_ws_and_comment(text, vpos)
            elif vpos < n and text[vpos] == "\r" and not text.startswith("\r\n", vpos):
                raise ConfigError(f"line {lineno}: disallowed control character")
            raise ConfigError(f"line {lineno}: missing value after '='")

        val, end_pos = _parse_toml_value(text, vpos)
        pos = _expect_toml_eol_or_eof(text, end_pos)

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


def _parse_max_duration_sec(val: Any, name: str = "audio.max_duration_sec") -> float:
    if isinstance(val, bool):
        raise ConfigError(f"{name}: expected number, got boolean")
    try:
        fval = float(val)
    except (TypeError, ValueError):
        raise ConfigError(f"{name}: invalid float value {val!r}") from None
    if not math.isfinite(fval) or fval < 0.0:
        raise ConfigError("audio.max_duration_sec must be >= 0")
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


def normalize_ui_position(raw: Any = "bottom", field_name: str = "ui.position") -> str:
    s = _as_str(raw, field_name).strip().lower()
    if s in VALID_UI_POSITIONS:
        return s
    raise ConfigError(
        f"{field_name} must be one of {sorted(VALID_UI_POSITIONS)}, got {raw!r}"
    )


def _normalize_refiner_provider(raw: Any) -> str:
    if raw is False:
        return "off"
    s = _as_str(raw, "refiner.provider").strip().lower()
    if s in ("none", "false", "0", "disabled"):
        return "off"
    return s


def _as_app_patterns(val: Any, name: str = "hotkey.ignore_apps") -> tuple[str, ...]:
    if not isinstance(val, (list, tuple)):
        raise ConfigError(f"{name}: expected array of strings, got {type(val).__name__}")
    out: list[str] = []
    for item in val:
        if not isinstance(item, str):
            raise ConfigError(f"{name}: every item must be a string, got {item!r}")
        s = item.strip()
        if not s:
            continue
        if s.rstrip("*").strip() == "":
            raise ConfigError(f"{name}: bare wildcard {item!r} is not allowed")
        out.append(s)
    return tuple(dict.fromkeys(out))


def _apply_section_dict(cfg: Config, data: Mapping[str, Any]) -> None:
    allowed_top = {"hotkey", "ui", "audio", "asr", "refiner"}
    for k in data:
        if k not in allowed_top:
            raise ConfigError(f"unknown top-level config section {k!r}")

    hk = data.get("hotkey")
    if hk is not None:
        if not isinstance(hk, Mapping):
            raise ConfigError("[hotkey] must be a table/object")
        has_explicit_scope = hk.get("scope") is not None
        oit_val: bool | None = None
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
            elif k == "scope":
                cfg.hotkey.scope = _as_str(v, "hotkey.scope").strip().lower()
            elif k == "yield_remote_desktop":
                cfg.hotkey.yield_remote_desktop = _as_bool(v, "hotkey.yield_remote_desktop")
            elif k == "ignore_apps":
                cfg.hotkey.ignore_apps = _as_app_patterns(v, "hotkey.ignore_apps")
            elif k == "only_in_terminals":
                oit_val = _as_bool(v, "hotkey.only_in_terminals")
            else:
                raise ConfigError(f"unknown hotkey option {k!r}")
        if oit_val is not None and not has_explicit_scope:
            cfg.hotkey.scope = "terminals" if oit_val else "all"

    ui = data.get("ui")
    if ui is not None:
        if not isinstance(ui, Mapping):
            raise ConfigError("[ui] must be a table/object")
        for k, v in ui.items():
            if v is None:
                continue
            if k == "language":
                cfg.ui.language = normalize_ui_language(v, "ui.language")
            elif k == "position":
                cfg.ui.position = normalize_ui_position(v, "ui.position")
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
            elif k == "max_duration_sec":
                cfg.audio.max_duration_sec = _parse_max_duration_sec(v, "audio.max_duration_sec")
            elif k == "wsl_source":
                cfg.audio.wsl_source = _as_str(v, "audio.wsl_source").strip().lower()
            elif k == "macos_device":
                cfg.audio.macos_device = _as_str(v, "audio.macos_device").strip()
            elif k == "beep":
                cfg.audio.beep = _as_bool(v, "audio.beep")
            elif k == "tail_drain_sec":
                cfg.audio.tail_drain_sec = _as_float(v, "audio.tail_drain_sec")
            elif k == "save_recordings":
                cfg.audio.save_recordings = _as_bool(v, "audio.save_recordings")
            elif k == "save_recordings_max_count":
                cfg.audio.save_recordings_max_count = _as_int(v, "audio.save_recordings_max_count")
            else:
                raise ConfigError(f"unknown audio option {k!r}")

    asr = data.get("asr")
    if asr is not None:
        if not isinstance(asr, Mapping):
            raise ConfigError("[asr] must be a table/object")
        prev_asr_provider = cfg.asr.provider
        has_explicit_asr_provider = asr.get("provider") is not None
        has_explicit_asr_model = (
            asr.get("model") is not None and str(asr.get("model")).strip() != ""
        )
        has_explicit_asr_api = (
            asr.get("api") is not None and str(asr.get("api")).strip() != ""
        )
        has_explicit_asr_base_url = (
            asr.get("base_url") is not None and str(asr.get("base_url")).strip() != ""
        )
        has_explicit_asr_key_env = (
            asr.get("api_key_env") is not None and str(asr.get("api_key_env")).strip() != ""
        )
        for k, v in asr.items():
            if v is None:
                continue
            if k == "provider":
                cfg.asr.provider = _as_str(v, "asr.provider").strip().lower()
            elif k == "model":
                cfg.asr.model = _as_str(v, "asr.model").strip()
            elif k == "api":
                cfg.asr.api = _as_str(v, "asr.api").strip().lower()
            elif k == "effort":
                cfg.asr.effort = _as_str(v, "asr.effort").strip().lower()
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
            elif k == "max_retries":
                cfg.asr.max_retries = _as_int(v, "asr.max_retries")
            else:
                raise ConfigError(f"unknown asr option {k!r}")
        if has_explicit_asr_provider and cfg.asr.provider != prev_asr_provider:
            if not has_explicit_asr_model:
                cfg.asr.model = ""
            if not has_explicit_asr_api:
                cfg.asr.api = "auto"
            if not has_explicit_asr_base_url:
                cfg.asr.base_url = ""
            if not has_explicit_asr_key_env:
                cfg.asr.api_key_env = ""

    ref = data.get("refiner")
    if ref is not None:
        if not isinstance(ref, Mapping):
            raise ConfigError("[refiner] must be a table/object")
        prev_ref_provider = cfg.refiner.provider
        has_explicit_ref_provider = ref.get("provider") is not None
        has_explicit_ref_model = (
            ref.get("model") is not None and str(ref.get("model")).strip() != ""
        )
        has_explicit_ref_api = (
            ref.get("api") is not None and str(ref.get("api")).strip() != ""
        )
        has_explicit_ref_base_url = (
            ref.get("base_url") is not None and str(ref.get("base_url")).strip() != ""
        )
        has_explicit_ref_key_env = (
            ref.get("api_key_env") is not None and str(ref.get("api_key_env")).strip() != ""
        )
        for k, v in ref.items():
            if v is None:
                continue
            if k == "provider":
                cfg.refiner.provider = _normalize_refiner_provider(v)
            elif k == "model":
                cfg.refiner.model = _as_str(v, "refiner.model").strip()
            elif k == "api":
                cfg.refiner.api = _as_str(v, "refiner.api").strip().lower()
            elif k == "effort":
                cfg.refiner.effort = _as_str(v, "refiner.effort").strip().lower()
            elif k == "prompt":
                cfg.refiner.prompt = _as_str(v, "refiner.prompt")
            elif k == "include_git_status":
                cfg.refiner.include_git_status = _as_bool(v, "refiner.include_git_status")
            elif k == "context_max_chars":
                cfg.refiner.context_max_chars = _as_int(v, "refiner.context_max_chars")
            elif k == "base_url":
                cfg.refiner.base_url = _as_str(v, "refiner.base_url").strip()
            elif k == "api_key_env":
                cfg.refiner.api_key_env = _as_str(v, "refiner.api_key_env").strip()
            elif k == "max_turns_per_thread":
                cfg.refiner.max_turns_per_thread = _as_int(v, "refiner.max_turns_per_thread")
            elif k == "timeout_sec":
                cfg.refiner.timeout_sec = _as_float(v, "refiner.timeout_sec")
            elif k == "max_retries":
                cfg.refiner.max_retries = _as_int(v, "refiner.max_retries")
            elif k == "force_on_single_hop":
                cfg.refiner.force_on_single_hop = _as_bool(v, "refiner.force_on_single_hop")
            else:
                raise ConfigError(f"unknown refiner option {k!r}")
        if has_explicit_ref_provider and cfg.refiner.provider != prev_ref_provider:
            if not has_explicit_ref_model:
                cfg.refiner.model = DEFAULT_REFINER_MODELS.get(cfg.refiner.provider, "")
            if not has_explicit_ref_api:
                cfg.refiner.api = "auto"
            if not has_explicit_ref_base_url:
                cfg.refiner.base_url = ""
            if not has_explicit_ref_key_env:
                cfg.refiner.api_key_env = ""


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


def _host_matches_domain(url: str, *domains: str) -> bool:
    try:
        host = (urllib.parse.urlsplit(url.strip()).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if not host:
        return False
    return any(host == d or host.endswith(f".{d}") for d in domains)


def is_gemini_native_url(base_url: str) -> bool:
    """Return True when ``base_url`` targets Google's native ``:generateContent`` endpoint."""
    b = base_url.strip().rstrip("/")
    if not b:
        return True
    if _host_matches_domain(b, "googleapis.com"):
        parsed = urllib.parse.urlsplit(b)
        return "/openai" not in (parsed.path or "").lower()
    return False


def is_dashscope_native_multimodal_model(model: str, base_url: str = "") -> bool:
    """Return True when DashScope model/URL uses the native multimodal-generation route."""
    b = base_url.strip().lower().rstrip("/")
    if b.endswith("/generation") or "/multimodal-generation" in b:
        return True
    m = model.strip().lower()
    return (
        m.startswith("qwen-audio-")
        or m.startswith("fun-asr")
        or m.startswith("paraformer")
        or m.startswith("sensevoice")
    )


def classify_asr_model_api(model: str) -> str | None:
    """Classify known dedicated STT vs multimodal chat-audio models (case-insensitive).

    Returns ``'audio-transcriptions'``, ``'chat-completions'``, or ``None`` if unknown.
    """
    m = model.strip().lower()
    if not m:
        return None
    slug = m.split("/", 1)[-1]
    base_slug = slug.split(":", 1)[0]

    # 1. Dedicated STT signatures (transcribe BEFORE gemini-, -stt/voxtral-mini-3b-2507 BEFORE voxtral-small*)
    if (
        m.startswith(("deepgram/", "assemblyai/", "elevenlabs/", "fish-audio/"))
        or "whisper" in m
        or "transcribe" in m
        or "-stt" in m
        or "_stt" in m
        or base_slug.startswith("stt")
        or base_slug.endswith("stt")
        or "-asr" in m
        or "_asr" in m
        or base_slug.startswith("asr")
        or base_slug.endswith("asr")
        or "parakeet" in m
        or "chirp-" in m
        or "scribe-" in m
        or "sensevoice" in m
        or "telespeech" in m
        or "voxtral-mini-3b-2507" in m
    ):
        return "audio-transcriptions"

    # 2. Multimodal audio-understanding chat models
    if (
        base_slug.startswith("gpt-audio")
        or "-gpt-audio" in m
        or ("gpt-4o" in base_slug and "audio" in base_slug)
        or "-omni" in m
        or "_omni" in m
        or base_slug.endswith("omni")
        or base_slug.startswith("gemini-")
        or base_slug.startswith("muse-")
        or base_slug.startswith("mimo-")
        or base_slug.startswith("voxtral-small")
    ):
        return "chat-completions"

    return None


def resolve_asr_api(
    provider: str,
    model: str,
    api: str = "auto",
    base_url: str = "",
) -> str:
    """Resolve effective Stage 1 ASR wire protocol and reject incompatible combinations."""
    prov = provider.strip().lower()
    req_api = (api or "auto").strip().lower()
    if req_api not in VALID_ASR_APIS:
        raise ConfigError(
            f"asr.api must be one of {sorted(VALID_ASR_APIS)}, got {api!r}"
        )
    eff_model = model.strip() or DEFAULT_ASR_MODELS.get(prov, "")

    if prov == "doubao":
        if req_api != "auto":
            raise ConfigError(
                f"asr.api={req_api!r} is incompatible with provider 'doubao' "
                "(uses native Doubao ASR protocol; keep asr.api='auto')"
            )
        return "doubao-native"

    if prov == "gemini" and is_gemini_native_url(base_url):
        if req_api != "auto":
            raise ConfigError(
                f"asr.api={req_api!r} is incompatible with native 'gemini' :generateContent "
                "(keep asr.api='auto', or use provider='openrouter' / an OpenAI-compatible base_url)"
            )
        return "gemini-native"

    if prov == "dashscope":
        if is_dashscope_native_multimodal_model(eff_model, base_url):
            if req_api != "auto":
                raise ConfigError(
                    f"asr.api={req_api!r} is incompatible with DashScope native multimodal model "
                    f"{eff_model!r} (keep asr.api='auto')"
                )
            return "dashscope-native"
        if req_api == "audio-transcriptions":
            raise ConfigError(
                "asr.api='audio-transcriptions' is not supported by provider 'dashscope'"
            )
        return "chat-completions"

    if req_api != "auto":
        return req_api

    classified = classify_asr_model_api(eff_model)
    if classified is not None:
        return classified

    if prov == "openrouter":
        raise ConfigError(
            f"cannot infer asr.api for unknown OpenRouter model {eff_model!r} with asr.api='auto'; "
            "please explicitly set asr.api = 'audio-transcriptions' or 'chat-completions'"
        )
    return "audio-transcriptions"


def _validate_config(cfg: Config, *, env: Mapping[str, str] | None = None) -> None:
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
    if not isinstance(cfg.hotkey.scope, str) or cfg.hotkey.scope not in VALID_HOTKEY_SCOPES:
        raise ConfigError(
            f"hotkey.scope must be one of {sorted(VALID_HOTKEY_SCOPES)}, "
            f"got {cfg.hotkey.scope!r}"
        )
    if not isinstance(cfg.hotkey.yield_remote_desktop, bool):
        raise ConfigError(
            f"hotkey.yield_remote_desktop: invalid boolean value {cfg.hotkey.yield_remote_desktop!r}"
        )
    cfg.hotkey.ignore_apps = _as_app_patterns(cfg.hotkey.ignore_apps, "hotkey.ignore_apps")

    cfg.ui.language = normalize_ui_language(cfg.ui.language, "ui.language")
    cfg.ui.position = normalize_ui_position(cfg.ui.position, "ui.position")

    if cfg.audio.wsl_source not in VALID_WSL_SOURCES:
        raise ConfigError(
            f"audio.wsl_source must be one of {sorted(VALID_WSL_SOURCES)}, "
            f"got {cfg.audio.wsl_source!r}"
        )
    if not cfg.audio.macos_device or not cfg.audio.macos_device.strip():
        raise ConfigError("audio.macos_device must not be empty")
    if cfg.audio.min_duration_sec < 0 or cfg.audio.min_voiced_sec < 0:
        raise ConfigError("audio min_duration_sec and min_voiced_sec must be >= 0")
    if (
        isinstance(cfg.audio.max_duration_sec, bool)
        or not isinstance(cfg.audio.max_duration_sec, (int, float))
        or not math.isfinite(cfg.audio.max_duration_sec)
        or cfg.audio.max_duration_sec < 0.0
    ):
        raise ConfigError("audio.max_duration_sec must be >= 0")
    if not (-120.0 <= cfg.audio.floor_dbfs < cfg.audio.target_dbfs <= 0.0):
        raise ConfigError(
            "audio dBFS bounds must satisfy -120.0 <= floor_dbfs < target_dbfs <= 0.0"
        )
    if not (0.0 <= cfg.audio.max_gain_db <= 120.0):
        raise ConfigError("audio.max_gain_db must satisfy 0.0 <= max_gain_db <= 120.0")
    if cfg.audio.tail_drain_sec is not None and not (0.0 <= cfg.audio.tail_drain_sec <= 5.0):
        raise ConfigError("audio.tail_drain_sec must satisfy 0.0 <= tail_drain_sec <= 5.0")
    if not isinstance(cfg.audio.save_recordings, bool):
        raise ConfigError(
            f"audio.save_recordings: invalid boolean value {cfg.audio.save_recordings!r}"
        )
    if (
        isinstance(cfg.audio.save_recordings_max_count, bool)
        or not isinstance(cfg.audio.save_recordings_max_count, int)
        or not (1 <= cfg.audio.save_recordings_max_count <= 5000)
    ):
        raise ConfigError(
            "audio.save_recordings_max_count must satisfy 1 <= save_recordings_max_count <= 5000"
        )

    if cfg.asr.provider not in VALID_ASR_PROVIDERS:
        raise ConfigError(
            f"asr.provider must be one of {sorted(VALID_ASR_PROVIDERS)}, got {cfg.asr.provider!r}"
        )
    if cfg.asr.api not in VALID_ASR_APIS:
        raise ConfigError(
            f"asr.api must be one of {sorted(VALID_ASR_APIS)}, got {cfg.asr.api!r}"
        )
    if cfg.asr.effort not in VALID_EFFORTS:
        raise ConfigError(
            f"asr.effort must be one of {sorted(VALID_EFFORTS)}, got {cfg.asr.effort!r}"
        )
    if cfg.asr.doubao_mode not in VALID_DOUBAO_MODES:
        raise ConfigError(
            f"asr.doubao_mode must be one of {sorted(VALID_DOUBAO_MODES)}, "
            f"got {cfg.asr.doubao_mode!r}"
        )
    if cfg.asr.timeout_sec <= 0:
        raise ConfigError("asr.timeout_sec must be > 0")
    if (
        isinstance(cfg.asr.max_retries, bool)
        or not isinstance(cfg.asr.max_retries, int)
        or not (0 <= cfg.asr.max_retries <= 10)
    ):
        raise ConfigError("asr.max_retries must satisfy 0 <= max_retries <= 10")
    if cfg.asr.provider == "openai-compatible" and not cfg.asr.base_url.strip():
        raise ConfigError(
            "asr.base_url must be explicitly set when asr.provider is 'openai-compatible' "
            "(use asr.provider='openai' for the official OpenAI API)"
        )

    if cfg.asr.provider != "auto":
        resolve_asr_api(
            cfg.asr.provider,
            cfg.asr.effective_model(cfg.asr.provider),
            cfg.asr.api,
            cfg.asr.base_url,
        )
    else:
        auto_prov = _infer_provider_from_explicit_asr_hints(cfg.asr)
        if auto_prov is None and env is not None:
            creds = resolve_credentials(cfg, env)
            if any(
                getattr(creds, k) is not None
                for k in (
                    "asr_custom",
                    "dashscope",
                    "doubao_key",
                    "openrouter",
                    "gemini",
                    "openai",
                    "groq",
                    "siliconflow",
                )
            ) or cfg.asr.base_url.strip():
                auto_prov = select_asr_provider(cfg, creds)
        if auto_prov is not None:
            if auto_prov == "openai-compatible" and not cfg.asr.base_url.strip():
                raise ConfigError(
                    "asr.base_url must be explicitly set when asr.provider resolves to "
                    "'openai-compatible' (set asr.base_url or choose a built-in asr.provider)"
                )
            resolve_asr_api(
                auto_prov,
                cfg.asr.effective_model(auto_prov),
                cfg.asr.api,
                cfg.asr.base_url,
            )

    if cfg.refiner.provider not in VALID_REFINER_PROVIDERS:
        raise ConfigError(
            f"refiner.provider must be one of {sorted(VALID_REFINER_PROVIDERS)}, "
            f"got {cfg.refiner.provider!r}"
        )
    cfg.refiner.effective_api(cfg.refiner.provider)
    if cfg.refiner.provider != "off" and cfg.refiner.effort not in VALID_EFFORTS:
        raise ConfigError(
            f"refiner.effort must be one of {sorted(VALID_EFFORTS)}, "
            f"got {cfg.refiner.effort!r}"
        )
    if not isinstance(cfg.refiner.prompt, str):
        raise ConfigError(
            f"refiner.prompt: expected string, got {type(cfg.refiner.prompt).__name__}"
        )
    if not isinstance(cfg.refiner.include_git_status, bool):
        raise ConfigError(
            f"refiner.include_git_status: invalid boolean value {cfg.refiner.include_git_status!r}"
        )
    if (
        isinstance(cfg.refiner.context_max_chars, bool)
        or not isinstance(cfg.refiner.context_max_chars, int)
        or cfg.refiner.context_max_chars < 0
    ):
        raise ConfigError("refiner.context_max_chars must be an integer >= 0")
    if cfg.refiner.provider == "openai-compatible":
        if not cfg.refiner.base_url.strip():
            raise ConfigError(
                "refiner.base_url must be explicitly set when refiner.provider is 'openai-compatible'"
            )
        if not cfg.refiner.model.strip():
            raise ConfigError(
                "refiner.model must be explicitly set when refiner.provider is 'openai-compatible'"
            )
    if cfg.refiner.max_turns_per_thread <= 0:
        raise ConfigError("refiner.max_turns_per_thread must be > 0")
    if cfg.refiner.timeout_sec <= 0:
        raise ConfigError("refiner.timeout_sec must be > 0")
    if (
        isinstance(cfg.refiner.max_retries, bool)
        or not isinstance(cfg.refiner.max_retries, int)
        or not (0 <= cfg.refiner.max_retries <= 10)
    ):
        raise ConfigError("refiner.max_retries must satisfy 0 <= max_retries <= 10")


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
    """Load merged ``(Config, merged_env)`` following CLI > config file > defaults."""
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

    if cli_overrides:
        _apply_section_dict(cfg, _expand_cli_overrides(cli_overrides))

    if not cfg.refiner.model.strip():
        cfg.refiner.model = DEFAULT_REFINER_MODELS.get(cfg.refiner.provider, "")

    _validate_config(cfg, env=effective_env)
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
    """Resolve provider credentials strictly honoring canonical vendor env priority."""
    src_env = os.environ if env is None else env
    asr_custom: CredentialItem | None = None
    refiner_custom: CredentialItem | None = None
    if cfg is not None:
        if cfg.asr.api_key_env.strip():
            asr_custom = _first_present_credential(src_env, (cfg.asr.api_key_env.strip(),))
        if cfg.refiner.api_key_env.strip():
            refiner_custom = _first_present_credential(src_env, (cfg.refiner.api_key_env.strip(),))

    return ProviderCredentials(
        dashscope=_first_present_credential(src_env, CREDENTIAL_ENV_ORDER["dashscope"]),
        volcengine=_first_present_credential(src_env, CREDENTIAL_ENV_ORDER["volcengine"]),
        doubao_app_id=_first_present_credential(src_env, CREDENTIAL_ENV_ORDER["doubao_app_id"]),
        doubao_key=_first_present_credential(src_env, CREDENTIAL_ENV_ORDER["doubao_key"]),
        openrouter=_first_present_credential(src_env, CREDENTIAL_ENV_ORDER["openrouter"]),
        gemini=_first_present_credential(src_env, CREDENTIAL_ENV_ORDER["gemini"]),
        openai=_first_present_credential(src_env, CREDENTIAL_ENV_ORDER["openai"]),
        groq=_first_present_credential(src_env, CREDENTIAL_ENV_ORDER["groq"]),
        siliconflow=_first_present_credential(src_env, CREDENTIAL_ENV_ORDER["siliconflow"]),
        asr_custom=asr_custom,
        refiner_custom=refiner_custom,
    )


def _infer_provider_from_explicit_asr_hints(asr_cfg: AsrConfig) -> str | None:
    """Infer target ASR provider when ``provider='auto'`` has explicit base_url, Doubao slot, or api_key_env."""
    url = asr_cfg.base_url.strip()
    rid = asr_cfg.resource_id.strip().lower()
    dmode = asr_cfg.doubao_mode.strip().lower()
    key_env = asr_cfg.api_key_env.strip().upper()

    if url:
        if _host_matches_domain(url, "aliyuncs.com", "dashscope.aliyuncs.com"):
            return "dashscope"
        if _host_matches_domain(url, "bytedance.com"):
            return "doubao"
        if _host_matches_domain(url, "openrouter.ai"):
            return "openrouter"
        if _host_matches_domain(url, "googleapis.com"):
            return "gemini"
        if _host_matches_domain(url, "openai.com"):
            return "openai"
        if _host_matches_domain(url, "groq.com"):
            return "groq"
        if _host_matches_domain(url, "siliconflow.cn"):
            return "siliconflow"
        return "openai-compatible"

    if (rid and rid != "auto") or (dmode and dmode != "auto"):
        return "doubao"

    if key_env:
        for group_key, names in CREDENTIAL_ENV_ORDER.items():
            if key_env in names:
                if group_key in ("doubao_app_id", "doubao_key"):
                    return "doubao"
                if group_key in VALID_ASR_PROVIDERS:
                    return group_key
        return "openai-compatible"

    return None


def select_asr_provider(cfg: Config, creds: ProviderCredentials) -> str:
    """Resolve effective canonical ASR provider name without cross-vendor key leakage."""
    prov = cfg.asr.canonical_provider()
    if prov not in VALID_ASR_PROVIDERS:
        raise ConfigError(
            f"asr.provider must be one of {sorted(VALID_ASR_PROVIDERS)}, got {cfg.asr.provider!r}"
        )
    if prov != "auto":
        return prov

    inferred = _infer_provider_from_explicit_asr_hints(cfg.asr)
    if inferred is not None:
        return inferred

    for candidate in ASR_AUTO_PRIORITY:
        if creds.for_provider(candidate) is not None:
            return candidate
    return "dashscope"


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------

_BEARER_RE = re.compile(r"(\bBearer\s+)[A-Za-z0-9._~+/=-]{4,}", re.IGNORECASE)
_SK_TOKEN_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{6,}")
_GSK_TOKEN_RE = re.compile(r"\bgsk_[A-Za-z0-9_-]{12,}")
_AIZA_TOKEN_RE = re.compile(r"\bAIza[0-9A-Za-z_-]{12,}")
_JWT_TOKEN_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{4,}\b")
_URL_USERINFO_SECRET_RE = re.compile(r"(https?://[^/\s:@]+:)[^/\s@]+(@)", re.IGNORECASE)
_SECRET_KEY_ALT = (
    r"x-api-key|x-api-access-key|x-api-app-key|x-goog-api-key|chatgpt[-_]account[-_]id|authorization|"
    r"api_?key|access_?key|access_?token|refresh_?token|id_?token|auth_?token|account_?id|"
    r"app_?id|api_?id|app_?key|secret|token|password"
)
_QUOTED_KEYVAL_SECRET_RE = re.compile(
    rf"""((['"])(?:{_SECRET_KEY_ALT})\2\s*:\s*)(['"])(?:\\.|(?!\3)[^\\])*?\3""",
    re.IGNORECASE | re.DOTALL,
)
_ESCAPED_QUOTED_KEYVAL_SECRET_RE = re.compile(
    rf"""(\\(['"])(?:{_SECRET_KEY_ALT})\\\2\s*:\s*)(\\(['"]))"""
    r"""(?:(?!\4)[^\\]|\\\\\\\\|\\\\\\\4|\\\\\\(?!\\|\4)[^\\]|\\\\(?!\\|\4)[^\\]|\\(?!\\|\4)[^\\])*(\\\4)""",
    re.IGNORECASE | re.DOTALL,
)
_AUTH_HEADER_LINE_RE = re.compile(
    r"(\bauthorization\s*[:=]\s*)(?:(['\"])(?:\\[^\r\n]|(?!\2)[^\\\r\n])*\2|"
    r"(?:(?:Bearer|Basic|Token|ApiKey|Digest)\s+)?[^\s\"',;}\]\r\n]+)",
    re.IGNORECASE,
)
_UNQUOTED_HEADER_SECRET_RE = re.compile(
    r"(\b(?:x-api-key|x-api-access-key|x-api-app-key|x-goog-api-key|chatgpt[-_]account[-_]id|"
    r"access_?token|refresh_?token|id_?token|auth_?token|account_?id)\s*[:=]\s*)"
    r"(?:(['\"])(?:\\[^\r\n]|(?!\2)[^\\\r\n])*\2|[^\s\"',;}\]\r\n]+)",
    re.IGNORECASE,
)
_QUERY_SECRET_RE = re.compile(
    r"([?&](?:key|api_key|access_key|access_token|refresh_token|id_token|auth_token|account_id|token)=)[^&\s\"']+",
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

    # Follow configured custom API key env var pointers from Config
    ptr_names: list[str] = []
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
    out = _GSK_TOKEN_RE.sub("[REDACTED]", out)
    out = _AIZA_TOKEN_RE.sub("[REDACTED]", out)
    out = _JWT_TOKEN_RE.sub("[REDACTED]", out)
    return out
