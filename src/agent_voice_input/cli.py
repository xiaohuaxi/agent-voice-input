"""CLI entrypoint for agent-voice-input (init, integrate, daemon, start, restart, stop, status, toggle, cancel, once, retry, hotwords, doctor, diagnose, key-test, session-hook)."""
from __future__ import annotations

import argparse, dataclasses, errno, fcntl, json, locale, os, shlex, shutil, signal, socket, stat, subprocess, sys, threading, time, urllib.parse
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import __version__

if TYPE_CHECKING:
    from .config import Config, ProviderCredentials
    from .http_transport import HttpTransport


def _render_init_config_toml(
    *,
    hotkey: str = "RControl",
    asr_provider: str = "auto",
    refiner_provider: str = "auto",
) -> str:
    return (
        "# agent-voice-input configuration (chmod 600)\n"
        "# Priority: CLI flags > config.toml > built-in defaults\n"
        "# For all options & advanced tuning, see config.example.toml\n\n"
        "[hotkey]\n"
        '# Default: "RCommand" on MacBook built-in keyboards, "RControl" on desktop Macs & Windows/WSL2\n'
        '# Also supports "ROption"/"RAlt", "Fn" (macOS), "F13".."F20", etc. (avoid ROption w/ Doubao IME, Fn w/ Globe key, RShift w/ CN IME)\n'
        f'key = "{hotkey}"\n\n'
        '# "tap_toggle" (tap <=0.5s to start/stop, hold 0.8s to cancel) | "ptt" (hold to speak)\n'
        'trigger_mode = "tap_toggle"\n\n'
        "[asr]\n"
        '# "auto" selects the first configured API key: dashscope -> doubao -> deepseek -> openai -> gemini -> groq -> openrouter -> siliconflow\n'
        f'provider = "{asr_provider}"\n\n'
        '# Thinking budget: "" (recommended low-latency default: "none") | "default" | "none" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max"\n'
        '# effort = ""\n\n'
        "[refiner]\n"
        '# "auto" prefers local `codex` login, then configured API keys (deepseek -> dashscope -> gemini -> ...), or falls back to "off"\n'
        f'provider = "{refiner_provider}"\n\n'
        '# Thinking budget: "" (recommended default: "low" for codex/agy/openai, "none" elsewhere) | "default" | "none" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max"\n'
        '# effort = ""\n\n'
        "[archive]\n"
        "# Optional local dictation history under ~/.agent-voice-input/ (0700 dirs, 0600 files; both off by default)\n"
        "# save_transcripts = false    # Monthly 12-field JSONL in transcripts/YYYY-MM.jsonl\n"
        "# save_audio = false          # 16 kHz 64 kbps MP3 in audio/<id>.mp3 (audio_max_count = 1000; 0 = unlimited)\n"
    )


_INIT_CONFIG_TOML = _render_init_config_toml()

_INIT_ENV_TEMPLATE = """\
# agent-voice-input API credentials (chmod 600)
# For cloud ASR, uncomment and fill in 1 Stage 1 speech recognition key below
# (local loopback openai-compatible ASR endpoints need no API key):

# === Stage 1: Speech Recognition (REQUIRED for cloud ASR — uncomment 1 key below; local loopback openai-compatible ASR needs no key) ===
# Simplest 1-Key Quick Start (each key below drives both Stage 1 ASR and Stage 2 Refiner):
#   Mainland China: DASHSCOPE_API_KEY or DEEPSEEK_API_KEY
#   Global:         OPENAI_API_KEY or GEMINI_API_KEY

# Alibaba Cloud Bailian / DashScope [Mainland China] (Stage 1 qwen3-asr-flash + Stage 2 qwen3.8-flash, or single-hop qwen3.8-omni-flash)
# DASHSCOPE_API_KEY="sk-..."

# DeepSeek official API [Mainland China] (single key for Stage 1 deepseek-asr + Stage 2 deepseek-flash)
# DEEPSEEK_API_KEY="sk-..."

# OpenAI [Global] (Stage 1 gpt-transcribe or single-hop gpt-audio-1.5/gpt-audio-mini + Stage 2 gpt-6-luna)
# OPENAI_API_KEY="sk-..."

# Google Gemini [Global] (single-hop Stage 1 audio + context refinement, or Stage 2 Refiner)
# GEMINI_API_KEY="AIza..."

# --- Other Supported Stage 1 Providers & Legacy Credentials ---

# Volcengine Doubao Speech (Stage 1 ASR: single-header X-Api-Key, no APP_ID needed)
# DOUBAO_SPEECH_API_KEY="..."
# Legacy dual-header app credentials (optional; ignored when DOUBAO_SPEECH_API_KEY is set):
# DOUBAO_ASR_APP_ID="..."
# DOUBAO_ASR_API_KEY="..."

# Groq Cloud (fast Stage 1 whisper-large-v3-turbo + Stage 2 llama-3.3-70b-versatile)
# GROQ_API_KEY="gsk_..."

# OpenRouter (aggregator for Stage 1 ASR + Stage 2 Refiner: google/gemini-3.8-flash)
# OPENROUTER_API_KEY="sk-or-..."

# SiliconFlow (Stage 1 SenseVoiceSmall + Stage 2 Refiner)
# SILICONFLOW_API_KEY="sk-..."

# === Stage 2 Only: Optional Refiner (pair with a Stage 1 key above) ===
# Note: Setting ONLY a Stage 2 key below cannot transcribe speech on its own.
# Local Codex CLI login (`codex login`, auto-detected) or Antigravity CLI (`refiner.provider = "agy"`)
# can also reuse your existing CLI login for Stage 2 refinement when paired with Stage 1 ASR
# (billing depends on your CLI auth mode, e.g., subscription login vs. API key).

# Volcengine Ark (Stage 2 Refiner: doubao-seed-2-0-mini-260428)
# ARK_API_KEY="..."

# Proxy (optional): HTTPS_PROXY / HTTP_PROXY / NO_PROXY can be exported in your shell or set here (shell env takes precedence; HTTP/HTTPS proxies only, SOCKS5 not supported).
"""

_INIT_GITIGNORE_CONTENT = "/*\n!/.gitignore\n!/hotwords/\n"

_MACOS_MODIFIER_HOTKEYS = frozenset(
    {
        "rcontrol",
        "rctrl",
        "rightcontrol",
        "rightctrl",
        "lcontrol",
        "lctrl",
        "leftcontrol",
        "leftctrl",
        "control",
        "ctrl",
        "ralt",
        "roption",
        "rightalt",
        "rightoption",
        "rmenu",
        "lalt",
        "loption",
        "leftalt",
        "leftoption",
        "lmenu",
        "alt",
        "option",
        "rcommand",
        "rcmd",
        "rightcommand",
        "rightcmd",
        "lcommand",
        "lcmd",
        "leftcommand",
        "leftcmd",
        "command",
        "cmd",
        "rshift",
        "rightshift",
        "lshift",
        "leftshift",
        "shift",
        "fn",
        "function",
        "globe",
        "capslock",
        "0x3e",
        "0x3b",
        "0x3d",
        "0x3a",
        "0x36",
        "0x37",
        "0x3c",
        "0x38",
        "0x39",
        "0x3f",
        "0xb3",
    }
)


def _add_common_flags(p: argparse.ArgumentParser, *, is_sub: bool = False) -> None:
    kw: dict[str, Any] = {"default": argparse.SUPPRESS} if is_sub else {}
    p.add_argument("--config", help="Path to config.toml or config.json", **kw)
    p.add_argument(
        "--hotkey",
        dest="hotkey",
        help="Override global hotkey (e.g. RCommand, RControl, ROption, F18)",
        **kw,
    )
    p.add_argument(
        "--asr",
        "--asr-provider",
        dest="asr",
        help="Override Stage 1 ASR provider (auto|dashscope|doubao|openrouter|gemini|openai|deepseek|groq|siliconflow|openai-compatible)",
        **kw,
    )
    p.add_argument("--asr-model", dest="asr_model", help="Override Stage 1 ASR model", **kw)
    p.add_argument(
        "--asr-api",
        dest="asr_api",
        help="Override Stage 1 ASR API protocol (auto|audio-transcriptions|chat-completions)",
        **kw,
    )
    p.add_argument(
        "--asr-effort",
        dest="asr_effort",
        help="Override Stage 1 reasoning/thinking effort (default|none|minimal|low|medium|high|xhigh|max; empty '' uses recommended default)",
        **kw,
    )
    p.add_argument(
        "--refiner",
        "--refiner-provider",
        dest="refiner",
        help="Override Stage 2 refiner provider (auto|codex|agy|deepseek|dashscope|volcengine|openrouter|gemini|openai|groq|siliconflow|openai-compatible|off)",
        **kw,
    )
    p.add_argument("--refiner-model", dest="refiner_model", help="Override Stage 2 refiner model", **kw)
    p.add_argument(
        "--refiner-api",
        dest="refiner_api",
        help="Override Stage 2 refiner API protocol (auto|chat-completions|responses)",
        **kw,
    )
    p.add_argument(
        "--refiner-effort",
        "--effort",
        dest="effort",
        help="Override Stage 2 reasoning/thinking effort (default|none|minimal|low|medium|high|xhigh|max; empty '' uses recommended default)",
        **kw,
    )


def _format_effort_display(effective: str, explicit: bool) -> str:
    eff = (effective or "").strip()
    if not eff:
        return "none (recommended)"
    return eff if explicit else f"{eff} (recommended)"


def _cred_label(item: Any) -> str:
    return f"OK (via {item.source_env})" if item is not None else "not set"


def _check_codex_model(model: str, env: dict[str, str]) -> str:
    from .config import resolve_codex_home_dir

    codex_home = resolve_codex_home_dir(env)
    if codex_home is None:
        return "models_cache.json not found"
    cache = codex_home / "models_cache.json"
    if not cache.is_file():
        return "models_cache.json not found"
    try:
        slugs = {m.get("slug") for m in (json.loads(cache.read_text(encoding="utf-8")).get("models") or []) if isinstance(m, dict)}
        return "present in models_cache.json" if model in slugs else f"NOT in models_cache.json ({model})"
    except Exception:
        return "unreadable models_cache.json"


def _is_macos_modifier_hotkey(raw_key: str) -> bool:
    k = (raw_key or "RControl").strip().lower() or "rcontrol"
    return k in _MACOS_MODIFIER_HOTKEYS


def _write_init_file_0600(cfg_dir: Path, target: Path, content: str, *, force: bool) -> str:
    from .config import ConfigError

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        st = None
    except OSError as exc:
        raise ConfigError(f"cannot inspect {target}: {exc}") from None

    if st is not None and stat.S_ISDIR(st.st_mode):
        raise ConfigError(f"cannot overwrite directory: {target}")

    if not force:
        if st is not None:
            return "kept"
        try:
            fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600)
        except FileExistsError:
            return "kept"
        except OSError as exc:
            raise ConfigError(f"cannot create {target}: {exc}") from None
        closed = False
        try:
            os.fchmod(fd, 0o600)
            fobj = os.fdopen(fd, "w", encoding="utf-8")
            closed = True
            with fobj as f:
                f.write(content)
                f.flush()
        except Exception:
            if not closed:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return "created"

    tmp_path = cfg_dir / f".{target.name}.init.{os.getpid()}.{time.monotonic_ns()}.tmp"
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600)
    closed = False
    try:
        os.fchmod(fd, 0o600)
        fobj = os.fdopen(fd, "w", encoding="utf-8")
        closed = True
        with fobj as f:
            f.write(content)
            f.flush()
        os.replace(tmp_path, target)
    except Exception:
        if not closed:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return "overwritten" if st is not None else "created"


def _write_init_gitignore_0644(target: Path, content: str) -> str:
    """Exclusively create 0644 ``.gitignore`` if absent; never overwrite an existing file (even with ``--force``)."""
    from .config import ConfigError

    st = _lstat_or_none(target)
    if st is not None:
        if stat.S_ISDIR(st.st_mode):
            raise ConfigError(f"cannot overwrite directory: {target}")
        return "kept"

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o644)
    except FileExistsError:
        return "kept"
    except OSError as exc:
        raise ConfigError(f"cannot create {target}: {exc}") from None

    closed = False
    try:
        os.fchmod(fd, 0o644)
        fobj = os.fdopen(fd, "w", encoding="utf-8")
        closed = True
        with fobj as f:
            f.write(content)
            f.flush()
    except Exception:
        if not closed:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return "created"


def _lstat_or_none(path: Path) -> os.stat_result | None:
    from .config import ConfigError

    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ConfigError(f"cannot inspect {path}: {exc}") from None


def _ensure_init_parent_dir(cfg_dir: Path, *, preserve_existing_mode: bool) -> None:
    from .config import ConfigError, ensure_secure_dir

    dir_st = _lstat_or_none(cfg_dir)
    if dir_st is not None and preserve_existing_mode:
        if stat.S_ISLNK(dir_st.st_mode) or not stat.S_ISDIR(dir_st.st_mode):
            raise ConfigError(f"unsafe config parent directory (not a real directory): {cfg_dir}")
        return
    ensure_secure_dir(cfg_dir)


def _is_macbook_laptop() -> bool:
    """Return True only on macOS MacBook laptops (desktop Macs like Mac mini/Studio/iMac/Pro return False)."""
    from .config import _is_macbook_laptop as _cfg_is_macbook_laptop

    return _cfg_is_macbook_laptop(platform=sys.platform, run_fn=subprocess.run)


def _which_from_env(env: Mapping[str, str] | None) -> Callable[[str], str | None]:
    if env is None or "PATH" not in env:
        return shutil.which
    raw_path = env.get("PATH") or ""
    if not raw_path:
        return lambda _name: None
    return lambda name: shutil.which(name, path=raw_path)


_KV_LABEL_WIDTH: int = 10
_ZERO_HOOK_MACOS_TERM_PROGRAMS: frozenset[str] = frozenset(
    {"ghostty", "apple_terminal", "iterm.app"}
)


def _kv_line(label: str, value: str) -> str:
    return f"{label:<{_KV_LABEL_WIDTH}}{value}"


def _is_confirmed_zero_hook_env(
    env: Mapping[str, str], *, platform_name: str | None = None
) -> bool:
    """Return True only when the current terminal is confirmed to support zero-hook exact routing."""
    if (env.get("TMUX") or "").strip():
        return True
    plat = platform_name if platform_name is not None else sys.platform
    if plat == "darwin":
        term_prog = (env.get("TERM_PROGRAM") or "").strip().lower()
        if term_prog in _ZERO_HOOK_MACOS_TERM_PROGRAMS:
            return True
    return False


def _format_integrate_tip_lines(
    env: Mapping[str, str],
    *,
    home_dir: str | Path | None = None,
    platform_name: str | None = None,
    which_fn: Callable[[str], str | None] | None = None,
) -> list[str]:
    if _is_confirmed_zero_hook_env(env, platform_name=platform_name):
        return []
    from .integrate import inspect_integrations
    from .platform_wsl import is_wsl

    eff_plat = platform_name if platform_name is not None else sys.platform
    plat_tag = "darwin" if eff_plat == "darwin" else ("wsl" if is_wsl(env) else "linux")
    statuses = inspect_integrations(
        home_dir=home_dir,
        env=env,
        platform=plat_tag,
        which_fn=which_fn if which_fn is not None else _which_from_env(env),
    )
    missing = [s.cli for s in statuses if s.installed and not s.configured]
    if not missing:
        return []
    cli_list = ", ".join(missing)
    pad = " " * _KV_LABEL_WIDTH
    return [
        _kv_line("Tip", f"run 'agent-voice-input integrate' (for {cli_list})"),
        f"{pad}Without it, multi-window sessions may lose context or mix up project terms.",
    ]


def _format_pipeline_rows(
    pipeline: Mapping[str, Any] | None,
    *,
    cfg: Config | None = None,
    creds: ProviderCredentials | None = None,
    include_effort: bool = False,
) -> list[str]:
    if not isinstance(pipeline, Mapping) or not pipeline:
        return []
    asr = pipeline.get("asr")
    ref = pipeline.get("refiner")
    if not isinstance(asr, Mapping) and not isinstance(ref, Mapping):
        summary = str(pipeline.get("summary") or "").strip()
        return [_kv_line("Pipeline", summary)] if summary else []

    rows: list[str] = []
    if isinstance(asr, Mapping):
        a_prov = str(asr.get("provider") or "").strip()
        a_model = str(asr.get("model") or "").strip()
        a_base = f"{a_prov} / {a_model}" if (a_prov and a_model) else (a_prov or a_model)
        a_notes: list[str] = []
        if asr.get("ready") is False:
            a_notes.append("missing API key")
        elif cfg is not None and creds is not None:
            if cfg.asr.api_key_env.strip():
                a_notes.append(
                    creds.asr_custom.source_env
                    if creds.asr_custom is not None
                    else cfg.asr.api_key_env.strip()
                )
            elif a_prov == "openai-compatible":
                a_notes.append(creds.openai.source_env if creds.openai is not None else "loopback")
            else:
                a_item = creds.for_provider(a_prov)
                if a_item is not None:
                    a_notes.append(a_item.source_env)
        if include_effort and (a_eff := str(asr.get("effort") or "").strip()):
            if asr.get("single_hop") or a_eff != "none":
                a_notes.append(f"effort={a_eff}")
        if a_base:
            a_note = ", ".join(a_notes)
            rows.append(_kv_line("ASR", f"{a_base} ({a_note})" if a_note else a_base))

    if isinstance(ref, Mapping):
        raw_prov = str(ref.get("configured_provider") or "").strip()
        r_prov = str(ref.get("provider") or "").strip()
        r_model = str(ref.get("model") or "").strip()
        if ref.get("bypassed"):
            r_txt = "bypassed (single-hop ASR)"
        elif r_prov == "off":
            r_txt = "off" if raw_prov == "off" else "off (no Stage 2 credentials)"
        else:
            r_base = f"{r_prov} / {r_model}" if (r_prov and r_model) else (r_prov or r_model)
            r_notes: list[str] = []
            if ref.get("ready") is False:
                if r_prov == "agy":
                    r_notes.append("agy CLI not found")
                elif r_prov == "codex":
                    r_notes.append("not logged in")
                else:
                    r_notes.append("missing API key")
            elif cfg is not None and creds is not None:
                if r_prov == "codex":
                    auth_mode = str(ref.get("auth_mode") or "")
                    r_notes.append(
                        {
                            "chatgpt": "local Codex subscription",
                            "api_key": "Codex API key",
                            "keyring": "local Codex keyring",
                        }.get(auth_mode, "local Codex login")
                    )
                elif r_prov == "agy":
                    r_notes.append("local agy CLI login")
                elif cfg.refiner.api_key_env.strip():
                    r_notes.append(
                        creds.refiner_custom.source_env
                        if creds.refiner_custom is not None
                        else cfg.refiner.api_key_env.strip()
                    )
                elif r_prov == "openai-compatible":
                    r_notes.append(creds.openai.source_env if creds.openai is not None else "loopback")
                else:
                    r_item = creds.for_provider(r_prov)
                    if r_item is not None:
                        r_notes.append(r_item.source_env)
            if include_effort and (r_eff := str(ref.get("effort") or "").strip()):
                r_notes.append(f"effort={r_eff}")
            r_note = ", ".join(r_notes)
            r_txt = f"{r_base} ({r_note})" if r_note else r_base
        if r_txt:
            rows.append(_kv_line("Refiner", r_txt))

    return rows


def _parse_integrate_cli_targets(raw_cli: list[str] | None) -> str | list[str]:
    if not raw_cli:
        return "all"
    tokens: list[str] = []
    for raw in raw_cli:
        for part in str(raw).split(","):
            tok = part.strip().lower()
            if tok:
                tokens.append(tok)
    if not tokens:
        raise ValueError("--cli must specify 'all' or one or more of: claude, codex, agy")
    return "all" if tokens == ["all"] else tokens


def _cmd_integrate(
    *,
    status: bool = False,
    dry_run: bool = False,
    cli_targets: list[str] | None = None,
    uninstall: bool = False,
    as_json: bool = False,
    env: Mapping[str, str] | None = None,
    home_dir: str | Path | None = None,
    cache_dir: str | Path | None = None,
    platform: str | None = None,
    which_fn: Callable[[str], str | None] | None = None,
) -> int:
    from .integrate import (
        _normalize_targets,
        apply_integrations,
        format_actions_json,
        format_actions_text,
        format_status_json,
        format_status_text,
        inspect_integrations,
    )

    try:
        parsed_targets = _parse_integrate_cli_targets(cli_targets)
        is_all, selected = _normalize_targets(parsed_targets)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    eff_which = which_fn if which_fn is not None else _which_from_env(env)
    if status:
        statuses = inspect_integrations(
            home_dir=home_dir, env=env, platform=platform, which_fn=eff_which
        )
        if not is_all:
            sel_set = set(selected)
            statuses = [s for s in statuses if s.cli in sel_set]
        ok = not any(s.error for s in statuses)
        if as_json:
            payload = {
                "ok": ok,
                "mode": "status",
                "items": format_status_json(statuses),
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(format_status_text(statuses))
        return 0 if ok else 1

    actions = apply_integrations(
        parsed_targets,
        dry_run=dry_run,
        uninstall=uninstall,
        home_dir=home_dir,
        cache_dir=cache_dir,
        env=env,
        platform=platform,
        which_fn=eff_which,
    )
    ok = not any(a.action == "error" for a in actions)
    if as_json:
        payload = {
            "ok": ok,
            "mode": "uninstall" if uninstall else "install",
            "dry_run": bool(dry_run),
            "actions": format_actions_json(actions),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_actions_text(actions, dry_run=dry_run))
    return 0 if ok else 1


def _stage2_only_key_labels(cfg: Config, creds: ProviderCredentials) -> list[str]:
    """Return configured Stage 2-only credential env names that cannot drive Stage 1 ASR."""
    labels: list[str] = []
    if creds.volcengine is not None:
        labels.append(creds.volcengine.source_env)
    if cfg.refiner.api_key_env.strip() and creds.refiner_custom is not None:
        if creds.refiner_custom.source_env not in labels:
            labels.append(creds.refiner_custom.source_env)
    return labels


def _cmd_init(
    config_path: str | Path | None = None,
    *,
    force: bool = False,
    asr_provider: str | None = None,
    refiner_provider: str | None = None,
    hotkey: str | None = None,
    env: Mapping[str, str] | None = None,
    home_dir: str | Path | None = None,
) -> int:
    from .audio_proc import resolve_ffmpeg_bin
    from .config import (
        VALID_ASR_PROVIDERS,
        VALID_REFINER_PROVIDERS,
        Config,
        ConfigError,
        detect_codex_local_auth_mode,
        detect_legacy_config_migration_hint,
        ensure_app_dir,
        explain_pipeline_selection,
        load_config,
        load_env_files,
        normalize_asr_provider,
        normalize_refiner_provider,
        resolve_app_home_dir,
        resolve_credentials,
        resolve_target_config_paths,
        validate_hotkey_for_platform,
    )
    from .platform_wsl import is_wsl

    src_env = os.environ if env is None else env
    plat_tag = "darwin" if sys.platform == "darwin" else ("wsl" if is_wsl(src_env) else "linux")

    if asr_provider is not None:
        norm_asr = normalize_asr_provider(asr_provider)
        if norm_asr not in VALID_ASR_PROVIDERS:
            raise ConfigError(
                f"--asr must be one of {sorted(VALID_ASR_PROVIDERS)}, got {asr_provider!r}"
            )
    else:
        norm_asr = "auto"

    if refiner_provider is not None:
        norm_ref = normalize_refiner_provider(refiner_provider)
        if norm_ref not in VALID_REFINER_PROVIDERS:
            raise ConfigError(
                f"--refiner must be one of {sorted(VALID_REFINER_PROVIDERS)}, got {refiner_provider!r}"
            )
    else:
        norm_ref = "auto"

    auto_macbook_hotkey = False
    if hotkey is not None:
        norm_hotkey = validate_hotkey_for_platform(hotkey, plat_tag, "--hotkey")
    elif _is_macbook_laptop():
        norm_hotkey = "RCommand"
        auto_macbook_hotkey = True
    else:
        norm_hotkey = "RControl"

    cli_cfg_str = str(config_path).strip() if config_path is not None else ""
    cfg_dir, toml_path, env_path = resolve_target_config_paths(
        config_path=config_path, env=src_env, home_dir=home_dir
    )
    if toml_path.suffix.lower() == ".json":
        raise ConfigError(
            f"init generates TOML config; target path cannot end with .json: {toml_path}"
        )
    if toml_path.name.lower() in (".env", ".gitignore"):
        raise ConfigError(
            f"init target --config path cannot be {toml_path.name!r}: {toml_path}"
        )

    app_home = resolve_app_home_dir(src_env, home_dir=home_dir)
    in_app_home = (
        app_home is not None
        and os.path.realpath(str(cfg_dir)) == os.path.realpath(str(app_home))
    )

    # Check legacy XDG migration hint BEFORE writing <app_home>/config.toml.
    legacy_hint = detect_legacy_config_migration_hint(src_env, home_dir=home_dir)

    toml_content = _render_init_config_toml(
        hotkey=norm_hotkey,
        asr_provider=norm_asr,
        refiner_provider=norm_ref,
    )

    if in_app_home:
        assert app_home is not None
        gitignore_path = cfg_dir / ".gitignore"
        toml_st = _lstat_or_none(toml_path)
        env_st = _lstat_or_none(env_path)
        gi_st = _lstat_or_none(gitignore_path)
        for p, st in ((toml_path, toml_st), (env_path, env_st), (gitignore_path, gi_st)):
            if st is not None and stat.S_ISDIR(st.st_mode):
                raise ConfigError(f"cannot overwrite directory: {p}")

        will_write_any = force or toml_st is None or env_st is None or gi_st is None
        if will_write_any:
            ensure_app_dir(app_home, create=True)
            if cfg_dir != app_home:
                ensure_app_dir(cfg_dir, create=True)

        state_toml = _write_init_file_0600(cfg_dir, toml_path, toml_content, force=force)
        if state_toml == "kept":
            print(_kv_line("Config", f"kept {toml_path} (pass --force to overwrite config.toml; .env is preserved)"))
        else:
            print(_kv_line("Config", f"{state_toml} {toml_path} (0600)"))
            if auto_macbook_hotkey:
                print(
                    _kv_line(
                        "Hotkey",
                        "RCommand (detected MacBook built-in keyboard; pass --hotkey RControl for external keyboard)",
                    )
                )

        # Never overwrite an existing .env even under --force to protect user API keys.
        state_env = _write_init_file_0600(cfg_dir, env_path, _INIT_ENV_TEMPLATE, force=False)
        if state_env == "kept":
            print(
                _kv_line(
                    "Env",
                    f"kept {env_path} (existing credentials preserved; delete manually to reset template)",
                )
            )
        else:
            print(_kv_line("Env", f"{state_env} {env_path} (0600)"))

        state_gi = _write_init_gitignore_0644(gitignore_path, _INIT_GITIGNORE_CONTENT)
        if state_gi == "kept":
            print(_kv_line("Gitignore", f"kept {gitignore_path}"))
        else:
            print(_kv_line("Gitignore", f"created {gitignore_path} (0644)"))
        cred_target_label = str(env_path)
    else:
        toml_st = _lstat_or_none(toml_path)
        if toml_st is not None and stat.S_ISDIR(toml_st.st_mode):
            raise ConfigError(f"cannot overwrite directory: {toml_path}")
        if force or toml_st is None:
            _ensure_init_parent_dir(cfg_dir, preserve_existing_mode=True)

        state_toml = _write_init_file_0600(cfg_dir, toml_path, toml_content, force=force)
        if state_toml == "kept":
            print(_kv_line("Config", f"kept {toml_path} (pass --force to overwrite config.toml)"))
        else:
            print(_kv_line("Config", f"{state_toml} {toml_path} (0600)"))
            if auto_macbook_hotkey:
                print(
                    _kv_line(
                        "Hotkey",
                        "RCommand (detected MacBook built-in keyboard; pass --hotkey RControl for external keyboard)",
                    )
                )

        if app_home is not None:
            cred_target_label = str(app_home / ".env")
            print(
                _kv_line(
                    "Env",
                    f"skipped in external --config directory (put credentials in {cred_target_label} or process environment)",
                )
            )
        else:
            cred_target_label = "your process environment"
            print(
                _kv_line(
                    "Env",
                    "skipped in external --config directory (provide credentials via process environment variables)",
                )
            )

    if state_toml == "kept":
        ignored_init_flags: list[str] = []
        if asr_provider is not None:
            ignored_init_flags.append(f"--asr {asr_provider}")
        if refiner_provider is not None:
            ignored_init_flags.append(f"--refiner {refiner_provider}")
        if hotkey is not None:
            ignored_init_flags.append(f"--hotkey {hotkey}")
        if ignored_init_flags:
            print(
                _kv_line(
                    "Warning",
                    f"WARN: {', '.join(ignored_init_flags)} not written because {toml_path} already exists; "
                    "edit config.toml manually or re-run with --force to overwrite config.toml (.env is preserved)",
                )
            )

    if legacy_hint:
        print(_kv_line("Migrate", legacy_hint))

    # Zero-network local environment summary
    try:
        eff_cfg, eff_env = load_config(
            config_path=config_path,
            env=src_env,
            home_dir=home_dir,
            is_macbook=_is_macbook_laptop(),
        )
    except ConfigError:
        eff_cfg = Config()
        eff_env = load_env_files(src_env, config_path=config_path, home_dir=home_dir)

    try:
        resolve_ffmpeg_bin(env=eff_env)
    except Exception:
        ff_cmd = "brew install ffmpeg" if sys.platform == "darwin" else "sudo apt install ffmpeg"
        print(_kv_line("Warning", f"ffmpeg not found (install via '{ff_cmd}')"))

    if sys.platform == "darwin":
        from .platform_macos import check_macos_clt_ready

        clt_ok, clt_err = check_macos_clt_ready(eff_env)
        if not clt_ok:
            clt_msg = (
                clt_err
                or "Xcode Command Line Tools not installed; run 'xcode-select --install'"
            )
            print(_kv_line("Warning", clt_msg))

    creds = resolve_credentials(eff_cfg, eff_env)
    pipe = explain_pipeline_selection(eff_cfg, creds, eff_env)
    asr_ready = bool(pipe["asr"]["ready"])
    codex_mode = detect_codex_local_auth_mode(eff_env)

    cfg_flag = f" --config {shlex.quote(cli_cfg_str)}" if cli_cfg_str else ""
    print()
    if asr_ready:
        print(_kv_line("Status", "ready out of the box"))
        for row in _format_pipeline_rows(pipe, cfg=eff_cfg, creds=creds):
            print(row)
        print(
            _kv_line(
                "Note",
                f"No need to edit {cred_target_label} unless you want a different provider.",
            )
        )
        print(
            _kv_line(
                "Next",
                f"agent-voice-input doctor{cfg_flag} --ping && agent-voice-input start{cfg_flag}",
            )
        )
    else:
        if stage2_only := _stage2_only_key_labels(eff_cfg, creds):
            key_names = ", ".join(stage2_only)
            print(
                _kv_line(
                    "Note",
                    f"detected Stage 2 refiner-only key ({key_names}), which cannot transcribe "
                    "Stage 1 speech — still add 1 Stage 1 speech key below.",
                )
            )
        if codex_mode == "chatgpt":
            print(
                _kv_line(
                    "Refiner",
                    "codex (local subscription ready for free — just add 1 Stage 1 speech key)",
                )
            )
        elif codex_mode != "none":
            print(
                _kv_line(
                    "Refiner",
                    "codex (local login ready — just add 1 Stage 1 speech key)",
                )
            )
        pad = " " * _KV_LABEL_WIDTH
        print(
            f"{_kv_line('Next', f'1. Add 1 Stage 1 speech recognition API key in {cred_target_label}:')}\n"
            f"{pad}   - Mainland China: DASHSCOPE_API_KEY (Bailian) or DEEPSEEK_API_KEY\n"
            f"{pad}   - Global:         OPENAI_API_KEY or GEMINI_API_KEY\n"
            f"{pad}2. Verify & start: agent-voice-input doctor{cfg_flag} --ping && agent-voice-input start{cfg_flag}"
        )

    for tip_line in _format_integrate_tip_lines(
        eff_env,
        home_dir=home_dir,
        platform_name=("darwin" if plat_tag == "darwin" else "linux"),
    ):
        print(tip_line)
    return 0


def _build_ping_http_spec(
    provider: str,
    base_url: str,
    key: str,
) -> tuple[str, dict[str, str]]:
    from .config import (
        DASHSCOPE_COMPAT_BASE_URL,
        GEMINI_DEFAULT_BASE_URL,
        OPENAI_DEFAULT_BASE_URL,
        OPENROUTER_DEFAULT_BASE_URL,
        _host_matches_domain,
        is_gemini_native_url,
        resolve_deepseek_endpoint_url,
    )

    prov = provider.strip().lower()
    if prov == "deepseek":
        return (
            resolve_deepseek_endpoint_url(base_url, "/models"),
            ({"Authorization": f"Bearer {key}"} if key else {}),
        )
    b = base_url.strip().rstrip("/")
    for sfx in ("/chat/completions", "/audio/transcriptions", "/responses"):
        if b.endswith(sfx):
            b = b[: -len(sfx)]
            break
    if prov == "gemini" and is_gemini_native_url(b):
        root = b or GEMINI_DEFAULT_BASE_URL.rstrip("/")
        return f"{root}/models?pageSize=1000", {"x-goog-api-key": key}
    if prov == "openrouter":
        if not b or _host_matches_domain(b, "openrouter.ai"):
            root = b or OPENROUTER_DEFAULT_BASE_URL
            return f"{root}/key", {"Authorization": f"Bearer {key}"}
        return f"{b}/models", {"Authorization": f"Bearer {key}"}
    if prov == "dashscope":
        if not b:
            url = f"{DASHSCOPE_COMPAT_BASE_URL}/models"
        elif "/compatible-mode" in b:
            host_root = b.split("/compatible-mode", 1)[0]
            url = f"{host_root}/compatible-mode/v1/models"
        elif b.endswith("/api/v1") or "/multimodal-generation" in b:
            host_root = b.split("/api/v1", 1)[0]
            url = f"{host_root}/compatible-mode/v1/models"
        elif b.endswith("/models"):
            url = b
        else:
            url = f"{b}/models"
        return url, {"Authorization": f"Bearer {key}"}
    root = b or OPENAI_DEFAULT_BASE_URL
    url = root if root.endswith("/models") else f"{root}/models"
    return url, ({"Authorization": f"Bearer {key}"} if key else {})


def _display_env_file_path(
    env: Mapping[str, str] | None = None,
    *,
    config_path: str | Path | None = None,
) -> str:
    from .config import default_env_file_candidates

    try:
        cands = default_env_file_candidates(env, config_path=config_path)
    except Exception:
        cands = []
    return str(cands[0]) if cands else "~/.agent-voice-input/.env"


def _display_config_file_path(
    cfg: Config,
    env: Mapping[str, str] | None = None,
) -> str:
    if cfg.config_path:
        return cfg.config_path
    from .config import default_config_file_candidates, expand_user_path

    src = os.environ if env is None else env
    if explicit_cfg := (src.get("AGENT_VOICE_INPUT_CONFIG") or "").strip():
        return str(expand_user_path(explicit_cfg, env=src))
    try:
        cands = default_config_file_candidates(env)
    except Exception:
        cands = []
    return str(cands[0]) if cands else "~/.agent-voice-input/config.toml"


def _provider_auth_fix_hint(
    provider: str,
    *,
    stage: str,
    cfg: Config,
    env: Mapping[str, str] | None = None,
) -> str:
    from .config import CREDENTIAL_ENV_ORDER

    prov = provider.strip().lower()
    env_path = _display_env_file_path(env, config_path=cfg.config_path or None)
    explicit_env = (cfg.asr.api_key_env if stage == "asr" else cfg.refiner.api_key_env).strip()
    if explicit_env:
        return f"check {explicit_env} in {env_path}"
    if prov == "codex":
        return f"check OPENAI_API_KEY or run 'codex login' (or check {env_path})"
    cred_key = "doubao_key" if prov == "doubao" else prov
    envs = CREDENTIAL_ENV_ORDER.get(cred_key)
    if envs:
        return f"check {envs[0]} in {env_path}"
    return f"check OPENAI_API_KEY (or {stage}.api_key_env) in {env_path}"


def _parse_ping_model_list(
    body: bytes, *, is_gemini_native: bool
) -> tuple[frozenset[str] | None, bool]:
    """Parse GET /models response into ``(model_ids, is_complete)``; returns ``(None, False)`` if empty/unparseable."""
    if not body:
        return None, False
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        return None, False
    if not isinstance(data, Mapping):
        return None, False

    if is_gemini_native:
        items = data.get("models")
        if not isinstance(items, list) or not items:
            return None, False
        has_next = bool(str(data.get("nextPageToken") or data.get("next_page_token") or "").strip())
        ids: set[str] = set()
        for item in items:
            if not isinstance(item, Mapping):
                continue
            raw_name = str(item.get("name") or "").strip()
            if raw_name:
                ids.add(raw_name[7:] if raw_name.startswith("models/") else raw_name)
        return (frozenset(ids), not has_next) if ids else (None, False)

    items = data.get("data")
    if not isinstance(items, list) or not items:
        return None, False
    has_more = data.get("has_more") is True
    next_tok = (
        data.get("next_page")
        or data.get("nextPageToken")
        or data.get("next_page_token")
        or data.get("next_cursor")
        or data.get("next")
    )
    has_next = has_more or (
        bool(next_tok) if isinstance(next_tok, bool) else bool(str(next_tok or "").strip())
    )
    ids = set()
    for item in items:
        if not isinstance(item, Mapping):
            continue
        mid = str(item.get("id") or "").strip()
        if mid:
            ids.add(mid)
    return (frozenset(ids), not has_next) if ids else (None, False)


def _should_verify_ping_model_list(
    *,
    stage: str,
    provider: str,
    url: str,
    cfg: Config,
    target_model: str,
    base_url: str,
) -> bool:
    """Return True only when ``url`` is a /models catalog covering ``stage`` without dedicated-STT false positives."""
    from .config import is_dashscope_omni_chat_model, resolve_asr_api

    path = urllib.parse.urlsplit(url).path.rstrip("/").lower()
    if not path.endswith("/models"):
        return False
    prov = provider.strip().lower()
    if stage == "refiner":
        return True
    if prov in ("openai", "gemini"):
        return True
    # Dedicated STT endpoints (e.g. DeepSeek /tools/asr, DashScope qwen3-asr-flash, Groq/SiliconFlow STT)
    # are often omitted from chat /models catalogs; only verify Stage 1 when using explicit chat-completions single-hop.
    try:
        eff_api = resolve_asr_api(prov, target_model, cfg.asr.api, base_url)
    except Exception:
        return False
    return eff_api == "chat-completions" and (
        prov != "dashscope" or is_dashscope_omni_chat_model(target_model, base_url)
    )


def _verify_ping_model_membership(
    *,
    model_ids: frozenset[str] | None,
    is_complete: bool,
    target_model: str,
    ms: int,
    shared: bool,
    is_gemini_native: bool,
) -> tuple[str, int, str]:
    raw_target = target_model.strip()
    norm_target = raw_target[7:] if (is_gemini_native and raw_target.startswith("models/")) else raw_target
    if norm_target and model_ids and is_complete and norm_target not in model_ids:
        return (
            "FAIL",
            ms,
            f"FAIL (HTTP 200, {ms}ms: model '{raw_target}' not enabled or not found for this account; "
            "enable it in console or update config.toml)",
        )
    sfx = ", shared endpoint" if shared else ""
    return "OK", ms, f"OK ({ms}ms{sfx})"


def _resolve_stage_http_key(
    stage: str, provider: str, cfg: Config, creds: ProviderCredentials
) -> str:
    custom_item = creds.asr_custom if stage == "asr" else creds.refiner_custom
    explicit_env_name = (cfg.asr.api_key_env if stage == "asr" else cfg.refiner.api_key_env).strip()
    if explicit_env_name:
        return custom_item.value if custom_item is not None else ""
    v_item = creds.for_provider(provider)
    return v_item.value if v_item is not None else ""


def _http_ping_cache_key(
    stage: str, provider: str, cfg: Config, creds: ProviderCredentials, base_url: str
) -> tuple[str, str, str] | None:
    prov = provider.strip().lower()
    if prov in ("doubao", "codex", "agy"):
        return None
    try:
        key_val = _resolve_stage_http_key(stage, prov, cfg, creds)
        url, _ = _build_ping_http_spec(prov, base_url, key_val)
        return ("http", url, key_val)
    except Exception:
        return None


def _probe_stage_ping(
    *,
    stage: str,
    provider: str,
    cfg: Config,
    creds: ProviderCredentials,
    env: dict[str, str],
    base_url: str,
    transport: HttpTransport | None,
    cache: dict[tuple[str, str, str], Any],
    timeout_sec: float = 5.0,
    target_model: str = "",
    cancel_token: Any = None,
) -> tuple[str, int, str]:
    from .config import (
        CODEX_SUBSCRIPTION_BASE_URL,
        OPENAI_DEFAULT_BASE_URL,
        is_gemini_native_url,
        redact_secrets,
    )
    from .http_transport import (
        HttpRequest,
        _close_quietly,
        _validate_request_url,
        make_urllib_http_transport,
    )
    from .refiner import CodexRefiner, _build_responses_url

    prov = provider.strip().lower()
    budget = max(0.2, float(timeout_sec))
    eff_model = (target_model or "").strip() or (
        cfg.asr.effective_model(prov) if stage == "asr" else cfg.refiner.effective_model(prov)
    )
    eff_tx = transport or make_urllib_http_transport(env)
    if prov == "doubao":
        from .asr_providers import DoubaoAsrProvider

        db_prov = DoubaoAsrProvider(cfg, creds, env=env, transport=eff_tx)
        return db_prov.probe_ping(timeout_sec=budget)
    if prov == "codex":
        t0 = time.monotonic()
        codex_budget = max(0.05, float(timeout_sec))
        deadline = t0 + codex_budget
        ping_cfg = dataclasses.replace(
            cfg,
            refiner=dataclasses.replace(cfg.refiner, timeout_sec=min(float(cfg.refiner.timeout_sec), codex_budget)),
        )
        ref = CodexRefiner(ping_cfg, creds, env=env, transport=transport)
        try:
            c_item = ref._resolve_codex_credentials(force_refresh=False, cancel_token=cancel_token)
            extra = tuple(s for s in (*creds.all_secret_values(), c_item.token, c_item.account_id) if s)
            if cancel_token is not None and cancel_token.is_cancelled():
                ms = max(0, int(round((time.monotonic() - t0) * 1000)))
                if cancel_token.reason == "timeout":
                    return "FAIL", ms, f"FAIL ({ms}ms: Codex ping timed out after {codex_budget:.1f}s)"
                return "FAIL", ms, f"FAIL ({ms}ms: Codex ping cancelled)"
            rem_sec = deadline - time.monotonic()
            if rem_sec <= 0.05:
                ms = max(0, int(round((time.monotonic() - t0) * 1000)))
                return "FAIL", ms, f"FAIL ({ms}ms: Codex ping exceeded {codex_budget:.1f}s deadline)"
            if c_item.mode == "api_key":
                url, hdrs = _build_ping_http_spec(
                    "openai", cfg.refiner.base_url or OPENAI_DEFAULT_BASE_URL, c_item.token
                )
                resp = eff_tx(
                    HttpRequest(
                        url=url,
                        method="GET",
                        headers=hdrs,
                        body=b"",
                        timeout_sec=rem_sec,
                        connect_timeout_sec=rem_sec,
                        cancel_token=cancel_token,
                    )
                )
                ms = max(0, int(round((time.monotonic() - t0) * 1000)))
                if resp.status == 200:
                    m_ids, m_complete = _parse_ping_model_list(resp.body, is_gemini_native=False)
                    return _verify_ping_model_membership(
                        model_ids=m_ids,
                        is_complete=m_complete,
                        target_model=eff_model,
                        ms=ms,
                        shared=False,
                        is_gemini_native=False,
                    )
                if resp.status in (401, 403):
                    hint = _provider_auth_fix_hint("codex", stage="refiner", cfg=cfg, env=env)
                    return "FAIL", ms, f"FAIL (HTTP {resp.status} unauthorized, {ms}ms; {hint})"
                excerpt = redact_secrets(resp.text().strip(), env, cfg=cfg, extra_secrets=extra)[:140]
                return "FAIL", ms, f"FAIL (HTTP {resp.status}, {ms}ms: {excerpt or 'unauthorized'})"

            if c_item.exp is not None and (c_item.exp - ref.time_fn()) <= 0:
                ms = max(0, int(round((time.monotonic() - t0) * 1000)))
                return "FAIL", ms, "FAIL (Codex JWT expired; Fix: run 'codex login')"
            target_url = _build_responses_url(
                cfg.refiner.base_url, default_base_url=CODEX_SUBSCRIPTION_BASE_URL
            )
            _, origin = _validate_request_url(target_url)
            scheme, host, port, _, _ = origin
            assert port is not None
            if transport is not None:
                resp = transport(
                    HttpRequest(
                        url=target_url,
                        method="GET",
                        headers={"Authorization": f"Bearer {c_item.token}"},
                        body=b"",
                        timeout_sec=rem_sec,
                        connect_timeout_sec=rem_sec,
                        cancel_token=cancel_token,
                    )
                )
                ms = max(0, int(round((time.monotonic() - t0) * 1000)))
                if resp.status in (200, 405):
                    return "OK", ms, f"OK ({ms}ms, TLS+endpoint reachable)"
                excerpt = redact_secrets(resp.text().strip(), env, cfg=cfg, extra_secrets=extra)[:140]
                return "FAIL", ms, f"FAIL (HTTP {resp.status}, {ms}ms: {excerpt or 'unreachable'})"
            conn, _ = ref._pool.acquire(
                scheme, host, port, rem_sec, allow_reuse=False, connect_timeout_sec=rem_sec
            )
            try:
                conn.connect()
            finally:
                _close_quietly(conn)
            ms = max(0, int(round((time.monotonic() - t0) * 1000)))
            return "OK", ms, f"OK ({ms}ms, TLS handshake + JWT valid)"
        except Exception as exc:
            ms = max(0, int(round((time.monotonic() - t0) * 1000)))
            if cancel_token is not None and cancel_token.is_cancelled() and cancel_token.reason == "timeout":
                return "FAIL", ms, f"FAIL ({ms}ms: Codex ping timed out after {codex_budget:.1f}s)"
            safe = redact_secrets(str(exc), env, cfg=cfg, extra_secrets=ref._extra_redaction_secrets(None))[:160]
            return "FAIL", ms, f"FAIL ({ms}ms: {safe})"
        finally:
            ref.close()
    if prov == "agy":
        from .refiner import AgyRefiner

        agy_budget = max(0.05, float(timeout_sec))
        ping_cfg = dataclasses.replace(
            cfg,
            refiner=dataclasses.replace(cfg.refiner, timeout_sec=min(float(cfg.refiner.timeout_sec), agy_budget)),
        )
        ref_agy = AgyRefiner(ping_cfg, creds, env=env)
        try:
            return ref_agy.probe_ping(timeout_sec=agy_budget, cancel_token=cancel_token)
        finally:
            ref_agy.close()

    key_val = _resolve_stage_http_key(stage, prov, cfg, creds)
    extra_sec = tuple(s for s in (*creds.all_secret_values(), key_val) if s)
    t0 = time.monotonic()
    cache_key: tuple[str, str, str] | None = None
    try:
        url, hdrs = _build_ping_http_spec(prov, base_url, key_val)
        is_gemini_native = prov == "gemini" and is_gemini_native_url(base_url)
        verify_models = _should_verify_ping_model_list(
            stage=stage,
            provider=prov,
            url=url,
            cfg=cfg,
            target_model=eff_model,
            base_url=base_url,
        )
        cache_key = ("http", url, key_val)
        if cache_key in cache:
            cached = cache[cache_key]
            if len(cached) >= 5:
                prev_st, prev_ms, prev_msg, cached_ids, cached_complete = cached[:5]
            else:
                prev_st, prev_ms, prev_msg = cached[:3]
                cached_ids, cached_complete = None, False
            if prev_st == "OK":
                if verify_models:
                    return _verify_ping_model_membership(
                        model_ids=cached_ids,
                        is_complete=cached_complete,
                        target_model=eff_model,
                        ms=prev_ms,
                        shared=True,
                        is_gemini_native=is_gemini_native,
                    )
                return "OK", prev_ms, f"OK ({prev_ms}ms, shared endpoint)"
            return prev_st, prev_ms, prev_msg
        resp = eff_tx(
            HttpRequest(
                url=url,
                method="GET",
                headers=hdrs,
                body=b"",
                timeout_sec=budget,
                connect_timeout_sec=budget,
            )
        )
        ms = max(0, int(round((time.monotonic() - t0) * 1000)))
        if resp.status == 200:
            m_ids, m_complete = _parse_ping_model_list(resp.body, is_gemini_native=is_gemini_native)
            cache[cache_key] = ("OK", ms, f"OK ({ms}ms)", m_ids, m_complete)
            if verify_models:
                return _verify_ping_model_membership(
                    model_ids=m_ids,
                    is_complete=m_complete,
                    target_model=eff_model,
                    ms=ms,
                    shared=False,
                    is_gemini_native=is_gemini_native,
                )
            return "OK", ms, f"OK ({ms}ms)"
        if resp.status in (401, 403):
            auth_hint = _provider_auth_fix_hint(prov, stage=stage, cfg=cfg, env=env)
            out = ("FAIL", ms, f"FAIL (HTTP {resp.status} unauthorized, {ms}ms; {auth_hint})")
        else:
            excerpt = redact_secrets(resp.text().strip(), env, cfg=cfg, extra_secrets=extra_sec)[:140]
            out = ("FAIL", ms, f"FAIL (HTTP {resp.status}, {ms}ms: {excerpt or 'unexpected status'})")
    except Exception as exc:
        ms = max(0, int(round((time.monotonic() - t0) * 1000)))
        safe = redact_secrets(str(exc), env, cfg=cfg, extra_secrets=extra_sec)[:160]
        out = ("FAIL", ms, f"FAIL ({ms}ms: {safe})")
    if cache_key is not None:
        cache[cache_key] = (*out, None, False)
    return out


def _run_startup_preflight_ping(
    *,
    cfg: Config,
    creds: ProviderCredentials,
    env: dict[str, str],
    pipe_info: Mapping[str, Any],
    transport: HttpTransport | None = None,
    timeout_sec: float = 3.5,
) -> list[tuple[str, str]]:
    """Run bounded non-blocking cold-start preflight checks and return ``(label, text)`` warning rows."""
    from .config import redact_secrets

    asr_info = pipe_info.get("asr") if isinstance(pipe_info.get("asr"), Mapping) else {}
    ref_info = pipe_info.get("refiner") if isinstance(pipe_info.get("refiner"), Mapping) else {}
    asr_prov = str(asr_info.get("provider") or "").strip().lower()
    asr_model = str(asr_info.get("model") or "").strip()
    asr_ready = bool(asr_info.get("ready"))
    ref_prov = str(ref_info.get("provider") or "").strip().lower()
    ref_model = str(ref_info.get("model") or "").strip()
    ref_ready = (
        bool(ref_info.get("ready"))
        and not bool(ref_info.get("bypassed"))
        and ref_prov not in ("", "off")
    )

    tasks: list[tuple[str, str, str, str]] = []
    if asr_ready and asr_prov:
        tasks.append(("asr", asr_prov, cfg.asr.effective_base_url(asr_prov), asr_model))
    if ref_ready and ref_prov:
        tasks.append(("refiner", ref_prov, cfg.refiner.effective_base_url(ref_prov), ref_model))
    if not tasks:
        return []

    from .asr_providers import CancelToken

    budget = max(0.05, float(timeout_sec))
    t_start = time.monotonic()
    deadline = t_start + budget
    # Reserve explicit teardown/reap headroom inside the overall preflight wall-clock budget
    # for local subprocess probes (agy acp and codex app-server run in their own process groups).
    subproc_provs = ("agy", "codex")
    subproc_reap_reserve = min(1.0, max(0.35, budget * 0.28)) if budget >= 0.5 else (budget * 0.45)
    subproc_soft_deadline = t_start + max(0.05, budget - subproc_reap_reserve)
    shared_http = False
    if len(tasks) == 2:
        k0 = _http_ping_cache_key(tasks[0][0], tasks[0][1], cfg, creds, tasks[0][2])
        k1 = _http_ping_cache_key(tasks[1][0], tasks[1][1], cfg, creds, tasks[1][2])
        shared_http = k0 is not None and k0 == k1

    slots: list[tuple[str, str, str, int, str] | None] = [None] * len(tasks)
    done_evts = [threading.Event() for _ in tasks]
    cancel_tokens = [CancelToken() for _ in tasks]

    def _run_probe_into_slot(
        idx: int,
        task: tuple[str, str, str, str],
        cache_dict: dict[tuple[str, str, str], Any],
    ) -> None:
        st_name, prov, burl, mdl = task
        try:
            if prov in subproc_provs:
                rem_probe = max(0.05, subproc_soft_deadline - time.monotonic())
            else:
                rem_probe = max(0.05, deadline - time.monotonic())
            probe_kw: dict[str, Any] = {
                "stage": st_name,
                "provider": prov,
                "cfg": cfg,
                "creds": creds,
                "env": env,
                "base_url": burl,
                "transport": transport,
                "cache": cache_dict,
                "timeout_sec": rem_probe,
                "target_model": mdl,
            }
            if prov in subproc_provs:
                probe_kw["cancel_token"] = cancel_tokens[idx]
            st, ms, msg = _probe_stage_ping(**probe_kw)
            slots[idx] = (st_name, prov, st, ms, msg)
        except Exception as exc:
            ms = max(0, int(round((time.monotonic() - t_start) * 1000)))
            slots[idx] = (st_name, prov, "FAIL", ms, f"FAIL ({ms}ms: {exc})")
        finally:
            done_evts[idx].set()

    if len(tasks) == 2 and not shared_http:
        for idx, task in enumerate(tasks):
            th = threading.Thread(
                target=_run_probe_into_slot,
                args=(idx, task, {}),
                name=f"avi-preflight-{task[0]}",
                daemon=True,
            )
            th.start()
    else:
        shared_cache: dict[tuple[str, str, str], Any] = {}

        def _run_serial() -> None:
            try:
                for idx, task in enumerate(tasks):
                    if time.monotonic() >= deadline:
                        break
                    _run_probe_into_slot(idx, task, shared_cache)
            finally:
                for evt in done_evts:
                    evt.set()

        th = threading.Thread(
            target=_run_serial,
            name="avi-preflight",
            daemon=True,
        )
        th.start()

    subproc_indices = [idx for idx, (_st, prov, _burl, _mdl) in enumerate(tasks) if prov in subproc_provs]
    for idx in subproc_indices:
        soft_rem = subproc_soft_deadline - time.monotonic()
        if soft_rem > 0.0:
            done_evts[idx].wait(timeout=soft_rem)
        if not done_evts[idx].is_set():
            cancel_tokens[idx].cancel("timeout")

    results: list[tuple[str, str, str, int, str]] = []
    for idx, (st_name, prov, _burl, _mdl) in enumerate(tasks):
        rem = deadline - time.monotonic()
        if rem > 0.0:
            done_evts[idx].wait(timeout=rem)
        res = slots[idx]
        if res is None:
            elapsed_ms = max(0, int(round((time.monotonic() - t_start) * 1000)))
            results.append(
                (
                    st_name,
                    prov,
                    "FAIL",
                    elapsed_ms,
                    f"FAIL ({elapsed_ms}ms: preflight timed out after {budget:.1f}s)",
                )
            )
        else:
            results.append(res)

    all_secrets = tuple(creds.all_secret_values())
    rows: list[tuple[str, str]] = []
    for st_name, prov, st, ms, raw_msg in results:
        if st != "FAIL":
            continue
        clean_msg = redact_secrets(raw_msg, env, cfg=cfg, extra_secrets=all_secrets)
        low = clean_msg.lower()
        if any(tok in low for tok in ("timed out", "timeout", "exceeded", "deadline")):
            rows.append(
                (
                    "WARN",
                    f"{st_name}({prov}): network preflight timed out ({ms}ms); daemon started normally",
                )
            )
            rows.append(
                (
                    "Fix:",
                    "check network/proxy or run 'agent-voice-input doctor --ping' later",
                )
            )
        elif "http 401" in low or "http 403" in low or "unauthorized" in low or "jwt expired" in low:
            rows.append(("WARN", f"{st_name}({prov}): {clean_msg}"))
            if prov == "codex" and "jwt expired" in low:
                rows.append(("Fix:", "run 'codex login' then 'agent-voice-input doctor --ping'"))
            else:
                hint = _provider_auth_fix_hint(prov, stage=st_name, cfg=cfg, env=env)
                rows.append(("Fix:", f"{hint}; then run 'agent-voice-input doctor --ping'"))
        elif "not enabled or not found" in low:
            rows.append(("WARN", f"{st_name}({prov}): {clean_msg}"))
            cfg_disp = _display_config_file_path(cfg, env)
            rows.append(
                (
                    "Fix:",
                    f"enable the model in console or update {cfg_disp}, then run 'agent-voice-input restart'",
                )
            )
        else:
            rows.append(("WARN", f"{st_name}({prov}): {clean_msg} (daemon started normally)"))
            rows.append(("Fix:", "run 'agent-voice-input doctor --ping' to troubleshoot"))
    return rows


def _command_matches_voice_hook(val: Any) -> bool:
    from .integrate import _is_any_voice_hook

    return _is_any_voice_hook(val)


def _event_entries_have_voice_hook(entries: Any) -> bool:
    if not isinstance(entries, list):
        return False
    for item in entries:
        if not isinstance(item, Mapping):
            continue
        if _command_matches_voice_hook(item.get("command")):
            return True
        nested = item.get("hooks")
        if isinstance(nested, list):
            for sub in nested:
                if isinstance(sub, Mapping) and _command_matches_voice_hook(sub.get("command")):
                    return True
    return False


_KNOWN_HOOK_EVENT_NAMES: frozenset[str] = frozenset(
    {
        "SessionStart",
        "SessionEnd",
        "UserPromptSubmit",
        "PreInvocation",
        "PostInvocation",
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "PermissionRequest",
        "Notification",
        "Stop",
        "SubagentStart",
        "SubagentStop",
        "PreCompact",
        "BeforeAgent",
        "AfterAgent",
        "BeforeModel",
        "AfterModel",
        "BeforeTool",
        "AfterTool",
        "BeforeToolSelection",
        "PreCompress",
    }
)


def _hook_map_has_voice_hook(obj: Any) -> bool:
    if not isinstance(obj, Mapping):
        return False
    for key, val in obj.items():
        if key in _KNOWN_HOOK_EVENT_NAMES and _event_entries_have_voice_hook(val):
            return True
        if isinstance(val, Mapping):
            for sub_key, sub_val in val.items():
                if sub_key in _KNOWN_HOOK_EVENT_NAMES and _event_entries_have_voice_hook(sub_val):
                    return True
    return False


def _json_has_voice_hook_command(data: Any, *, is_hooks_file: bool) -> bool:
    if not isinstance(data, Mapping):
        return False
    if "hooks" in data and _hook_map_has_voice_hook(data.get("hooks")):
        return True
    if is_hooks_file:
        return _hook_map_has_voice_hook(data)
    return False


def _toml_has_codex_voice_hook(raw_text: str) -> bool:
    from .config import ConfigError, parse_toml
    from .integrate import has_codex_voice_title

    try:
        parsed = parse_toml(raw_text)
    except ConfigError:
        return False
    return has_codex_voice_title(parsed)


def _count_active_session_registry(rt_dir: Path) -> int:
    sdir = rt_dir / "sessions"
    try:
        if sdir.is_dir() and not sdir.is_symlink():
            return sum(
                1
                for e in sdir.iterdir()
                if e.name.endswith(".json") and not e.name.startswith(".")
            )
    except OSError:
        return 0
    return 0


def _inspect_hook_summary(env: Mapping[str, str], rt_dir: Path) -> str:
    from .config import expand_user_path

    codex_home_raw = (env.get("CODEX_HOME") or "").strip()
    codex_cfg_path = (
        expand_user_path(codex_home_raw, env=env) / "config.toml"
        if codex_home_raw
        else expand_user_path("~/.codex/config.toml", env=env)
    )
    configured: list[str] = []
    for label, paths in (
        ("claude", (expand_user_path("~/.claude/settings.json", env=env),)),
        (
            "gemini/agy",
            (
                expand_user_path("~/.gemini/config/hooks.json", env=env),
                expand_user_path("~/.gemini/hooks.json", env=env),
                expand_user_path("~/.gemini/settings.json", env=env),
            ),
        ),
        ("codex", (codex_cfg_path,)),
    ):
        for p in paths:
            try:
                if p.is_file() and p.stat().st_size <= 524_288:
                    hook_txt = p.read_text(encoding="utf-8", errors="ignore")
                    matched = False
                    if p.suffix.lower() == ".json":
                        try:
                            parsed_json = json.loads(hook_txt)
                        except ValueError:
                            parsed_json = None
                        matched = _json_has_voice_hook_command(
                            parsed_json, is_hooks_file=(p.name.lower() == "hooks.json")
                        )
                    else:
                        matched = _toml_has_codex_voice_hook(hook_txt)
                    if matched:
                        configured.append(label)
                        break
            except OSError:
                continue
    active_reg = _count_active_session_registry(rt_dir)
    hooks_txt = ", ".join(configured) if configured else "none configured (optional)"
    return f"hooks={hooks_txt}, active_registry_entries={active_reg}"


@dataclasses.dataclass
class _DoctorState:
    cfg: Config
    env: dict[str, str]
    creds: ProviderCredentials
    rt_dir: Path
    as_json: bool
    all_secrets: tuple[str, ...]
    ok_all: bool = True
    lines_out: list[str] = dataclasses.field(default_factory=list)
    fixes: list[str] = dataclasses.field(default_factory=list)
    platform_kind: str = "linux"
    ffmpeg_ok: bool = False
    fg_title: str = ""
    fg_tty: str = ""
    fg_cwd: str = ""
    fg_pid: int = 0
    fg_process: str = ""
    fg_bundle_id: str = ""
    fg_document: str = ""
    eff_asr: str = ""
    eff_asr_model: str = ""
    eff_asr_url: str = ""
    eff_asr_api: str = ""
    asr_ready: bool = False
    asr_is_single_hop: bool = False
    daemon_running: bool = False
    daemon_pid: int | None = None
    daemon_state: str = "stopped"
    daemon_version: str = ""
    daemon_build_id: str = ""
    daemon_code_drift: bool = False
    daemon_drift_reason: str = ""
    daemon_error: str = ""
    match: Any = None
    remote_ssh_ok: bool | None = None
    resolved_default_ssh: str = ""
    raw_ref_prov: str = ""
    resolved_ref_prov: str = ""
    eff_ref_model: str = ""
    eff_ref_url: str = ""
    ref_ping_needed: bool = False
    pipeline: dict[str, Any] = dataclasses.field(default_factory=dict)
    ping_results: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)
    hooks_report: dict[str, Any] = dataclasses.field(default_factory=dict)
    hotwords_report: dict[str, Any] = dataclasses.field(default_factory=dict)
    migration_hint: str = ""

    def clean(self, text: str) -> str:
        from .config import redact_secrets

        return redact_secrets(text, self.env, cfg=self.cfg, extra_secrets=self.all_secrets)

    def emit(self, line: str) -> None:
        safe = self.clean(line)
        self.lines_out.append(safe)
        if not self.as_json:
            print(safe)

    def emit_fix(self, fix_cmd: str) -> None:
        safe_fix = self.clean(fix_cmd)
        self.fixes.append(safe_fix)
        self.emit(f"            Fix: {safe_fix}")


def _inspect_hotwords_path_info(hw_path: Path) -> dict[str, Any]:
    """Inspect ``personal.txt`` and its ``hotwords/`` parent directory symlink/realpath metadata."""
    hw_dir = hw_path.parent
    dir_is_symlink = False
    dir_symlink_target = ""
    try:
        st_dir = os.lstat(hw_dir)
        dir_is_symlink = stat.S_ISLNK(st_dir.st_mode)
        if dir_is_symlink:
            dir_symlink_target = os.readlink(hw_dir)
    except OSError:
        pass
    try:
        dir_realpath = os.path.realpath(str(hw_dir))
    except OSError:
        dir_realpath = str(hw_dir)
    try:
        file_realpath = os.path.realpath(str(hw_path))
    except OSError:
        file_realpath = str(hw_path)
    file_exists = False
    file_is_symlink = False
    try:
        st_file = os.lstat(hw_path)
        file_is_symlink = stat.S_ISLNK(st_file.st_mode)
        file_exists = stat.S_ISREG(st_file.st_mode)
    except OSError:
        pass
    return {
        "path": _recover_utf8_surrogates(str(hw_path)),
        "realpath": _recover_utf8_surrogates(file_realpath),
        "exists": file_exists,
        "file_is_symlink": file_is_symlink,
        "dir": _recover_utf8_surrogates(str(hw_dir)),
        "dir_is_symlink": dir_is_symlink,
        "dir_symlink_target": _recover_utf8_surrogates(dir_symlink_target),
        "dir_realpath": _recover_utf8_surrogates(dir_realpath),
    }


def _doctor_check_env_and_ffmpeg(st: _DoctorState) -> None:
    from .audio_proc import resolve_ffmpeg_bin
    from .config import default_env_file_candidates, detect_legacy_config_migration_hint

    st.emit(f"[python]    OK ({sys.version.split()[0]}, runtime={st.rt_dir})")
    st.emit(f"[config]    {st.cfg.config_path or 'defaults (no config.toml)'}")
    if mig_hint := detect_legacy_config_migration_hint(st.env):
        st.migration_hint = mig_hint
        st.emit(f"[migrate]   INFO {mig_hint}")
    seen_env_inodes: set[tuple[int, int]] = set()
    for cand_env in default_env_file_candidates(st.env, config_path=st.cfg.config_path or None):
        try:
            st_env = os.stat(cand_env)
        except OSError:
            continue
        inode_key = (st_env.st_dev, st_env.st_ino)
        if inode_key in seen_env_inodes:
            continue
        seen_env_inodes.add(inode_key)
        if stat.S_ISREG(st_env.st_mode) and (st_env.st_mode & 0o077) != 0:
            mode_oct = oct(stat.S_IMODE(st_env.st_mode))
            real_target = cand_env.resolve() if cand_env.is_symlink() else cand_env
            st.emit(
                f"[env-perm]  WARN {cand_env} has open permissions ({mode_oct}; recommend: chmod 600 {real_target})"
            )

    try:
        ff_bin = resolve_ffmpeg_bin(env=st.env)
        st.ffmpeg_ok = True
        st.emit(f"[ffmpeg]    OK ({ff_bin})")
    except Exception as exc:
        st.emit(f"[ffmpeg]    FAIL ({exc})")
        st.emit_fix("brew install ffmpeg" if sys.platform == "darwin" else "sudo apt install ffmpeg")
        st.ok_all = False

    from .http_transport import inspect_proxy_config_warning

    if proxy_warn := inspect_proxy_config_warning(st.env):
        warn_detail, fix_hint = proxy_warn
        st.emit(f"[proxy]     WARN {warn_detail}")
        st.emit_fix(fix_hint)


def _doctor_check_macos_host(st: _DoctorState) -> None:
    from .config import ConfigError, validate_hotkey_for_platform
    from .platform_macos import probe_macos_host

    st.platform_kind = "darwin"
    canon_hk = ""
    try:
        canon_hk = validate_hotkey_for_platform(st.cfg.hotkey.key, "darwin")
    except ConfigError as exc:
        st.ok_all = False
        st.emit(f"[hotkey]    FAIL {exc}")
    if canon_hk == "RControl" and _is_macbook_laptop():
        st.emit(
            "[hotkey]    WARN hotkey is set to RControl on a MacBook "
            '(ignore if using an external full-size keyboard with Right Ctrl; '
            'for built-in keyboard, set key = "RCommand" (recommended) or "ROption"/"Fn" in config.toml)'
        )
    m = probe_macos_host(st.cfg, st.env, announce_compile=(not st.as_json))
    st.fg_title = str(m.get("fg_title") or "")
    st.fg_tty = str(m.get("fg_tty") or "")
    st.fg_cwd = str(m.get("fg_cwd") or "")
    st.fg_pid = int(m.get("fg_pid") or 0)
    st.fg_process = str(m.get("fg_process") or "")
    st.fg_bundle_id = str(m.get("fg_bundle_id") or "")
    st.fg_document = str(m.get("fg_document") or "")
    devs = m.get("audio_devices") or []
    devs_str = ", ".join(devs) if devs else "none listed (check mic device or grant Microphone permission)"
    is_ssh_env = any(str(st.env.get(k) or "").strip() for k in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"))
    is_local_gui = (
        m.get("caller_has_graphic_session") is not False
        and not bool(m.get("caller_is_remote_session"))
        and not is_ssh_env
    )
    avf_fail = is_local_gui and (m.get("avfoundation_ok") is False)
    avf_label = "OK" if m.get("avfoundation_ok") else ("FAIL" if avf_fail else "WARN")
    st.emit(
        f"[macos-aud] avfoundation={avf_label} "
        f"device={m.get('configured_audio_device')} inputs={devs_str} | "
        f"pbcopy={'OK' if m.get('pbcopy_ok') else 'FAIL'} tmux={m.get('tmux_active')}"
    )
    if avf_fail:
        st.ok_all = False
        st.emit_fix("connect/enable an input microphone in System Settings -> Sound -> Input")
    shim_fail = (m.get("swiftc_ok") is False) or (m.get("mac_shim_ok") is False)
    shim_desc = (
        f"OK ({m.get('mac_shim_bin')})"
        if m.get("mac_shim_ok")
        else f"FAIL ({m.get('mac_shim_error') or 'unavailable'})"
    )
    if m.get("swiftc_ok"):
        swiftc_desc = f"OK ({m.get('swiftc_bin')})"
    elif m.get("clt_ok") is False:
        swiftc_desc = "MISSING CLT (run 'xcode-select --install')"
    else:
        swiftc_desc = "FAIL (not found)" if m.get("swiftc_ok") is False else "WARN (not found)"
    mac_ui_info = f" ui_lang={m.get('ui_lang')}" if m.get("ui_lang") else ""
    st.emit(f"[mac-shim]  swiftc={swiftc_desc} shim={shim_desc}{mac_ui_info}")
    if shim_fail:
        st.ok_all = False
        shim_err_low = str(m.get("mac_shim_error") or "").lower()
        if m.get("swiftc_ok") and "xcodebuild -license" in shim_err_low:
            st.emit_fix("sudo xcodebuild -license accept")
        else:
            st.emit_fix("xcode-select --install")
    if m.get("fg_surface_id"):
        st.emit(
            f"[macos-fg]  app={m.get('fg_process')!r} surface={m.get('fg_surface_id')} "
            f"tty={m.get('fg_tty') or '-'} cwd={m.get('fg_cwd') or '-'} title={st.fg_title!r}"
        )
    elif m.get("fg_tty") or m.get("fg_cwd"):
        st.emit(
            f"[macos-fg]  app={m.get('fg_process')!r} "
            f"tty={m.get('fg_tty') or '-'} cwd={m.get('fg_cwd') or '-'} title={st.fg_title!r}"
        )
    else:
        st.emit(f"[macos-fg]  app={m.get('fg_process')!r} title={st.fg_title!r}")
    caller_mic = str(m.get("caller_mic_auth") or "").strip().lower()
    tcc_prefix = f"caller_mic_auth={caller_mic} | " if caller_mic else ""
    st.emit(f"[macos-tcc] {tcc_prefix}{m.get('permissions_hint')}")
    if is_local_gui and caller_mic in ("denied", "restricted"):
        st.ok_all = False
        st.emit(f"[macos-tcc] FAIL Microphone permission is {caller_mic}")
        st.emit_fix('open "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"')
    elif caller_warn := str(m.get("caller_mic_warning") or "").strip():
        st.emit(
            f"[macos-tcc] WARN (current shell context: {caller_warn}; "
            "note: reflects this shell, not an already-running GUI daemon)"
        )
    elif is_local_gui and caller_mic == "not_determined":
        st.emit(
            "[macos-tcc] INFO Microphone permission not yet requested; "
            "macOS will prompt for authorization on your first recording."
        )
    if m.get("ax_trusted") is False:
        st.emit(
            '[macos-ax]  WARN Accessibility not granted (Ghostty/tmux direct delivery works; '
            'without it, Cmd+V in other apps and window-title/session binding in third-party terminals/desktop apps fail: '
            'open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")'
        )
    if (
        not _is_macos_modifier_hotkey(st.cfg.hotkey.key)
        and m.get("listen_event_access") is False
        and m.get("ax_trusted") is False
    ):
        st.emit(
            '[macos-im]  WARN Input Monitoring not granted for non-modifier hotkey '
            '(open "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent")'
        )
    if m.get("pbcopy_ok") is False:
        st.ok_all = False


def _doctor_check_wsl_host(st: _DoctorState) -> None:
    from .config import ConfigError, validate_hotkey_for_platform
    from .platform_wsl import probe_wsl_host, resolve_wsl_effective_audio_source

    st.platform_kind = "wsl2"
    try:
        validate_hotkey_for_platform(st.cfg.hotkey.key, "wsl")
    except ConfigError as exc:
        st.ok_all = False
        st.emit(f"[hotkey]    FAIL {exc}")
    w = probe_wsl_host(st.cfg, st.env, announce_slow=(not st.as_json))
    st.fg_title = str(w.get("fg_title") or "")
    st.fg_process = str(w.get("fg_process") or "")
    ps_ok = bool(w.get("powershell_ok"))
    eff_backend, eff_pulse_src = resolve_wsl_effective_audio_source(
        st.cfg, st.env, has_powershell=ps_ok
    )
    pulse_ok = (
        bool(w.get("pulse_source_ok"))
        if (eff_pulse_src != "RDPSource" and "pulse_source_ok" in w)
        else bool(w.get("pulse_rdp_ok"))
    )
    winmm_n = int(w.get("winmm_devs") or 0)
    has_wasapi_key = "wasapi_ok" in w
    wasapi_ok = bool(w.get("wasapi_ok")) if has_wasapi_key else False
    wasapi_state = str(w.get("wasapi_state") or "").strip()
    if eff_backend == "pulse":
        has_wsl_audio = pulse_ok
    elif has_wasapi_key:
        # Default auto/wasapi uses Windows WASAPI capture inside win_shim.ps1;
        # do not let WSLg's virtual silent RDPSource or waveIn device count mask wasapi_ok=False.
        has_wsl_audio = wasapi_ok
    else:
        has_wsl_audio = pulse_ok or winmm_n > 0
    ui_info = f" ui_lang={w.get('ui_lang')}" if w.get("ui_lang") else ""
    wasapi_lbl = "OK" if wasapi_ok else ("WARN" if eff_backend == "pulse" else "FAIL")
    wasapi_detail = f" ({wasapi_state})" if (not wasapi_ok and wasapi_state) else ""
    wasapi_prefix = f"WASAPI={wasapi_lbl}{wasapi_detail}, " if has_wasapi_key else ""
    pulse_lbl = f"PulseAudio ({eff_pulse_src})" if eff_pulse_src != "RDPSource" else "PulseAudio RDPSource"
    st.emit(
        f"[wsl-audio] {wasapi_prefix}{pulse_lbl}={'OK' if pulse_ok else 'WARN'}, winmm waveIn devs={winmm_n}"
    )
    st.emit(
        f"[win-shim]  PowerShell={'OK (' + str(w.get('ps_version')) + ')' if ps_ok else 'FAIL'} fg={w.get('fg_process')!r}{ui_info}"
    )
    if not ps_ok:
        st.ok_all = False
        if ps_err := str(w.get("ps_error") or "").strip()[:180]:
            st.emit(f"[win-shim]  FAIL error={ps_err}")
            low_pe = ps_err.lower()
            if any(
                tok in low_pe
                for tok in (
                    "executionpolicy",
                    "execution policy",
                    "constrainedlanguage",
                    "constrained language",
                )
            ):
                st.emit(
                    "[win-shim]  HINT Windows PowerShell ExecutionPolicy/ConstrainedLanguage blocked win_shim.ps1; "
                    "check Windows Group Policy / AppLocker or allow ExecutionPolicy Bypass"
                )
        st.emit_fix("check WSL interop or /mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    if not has_wsl_audio:
        st.ok_all = False
        st.emit_fix("cmd.exe /c start ms-settings:privacy-microphone")


def _doctor_check_platform(st: _DoctorState) -> None:
    from .platform_wsl import is_wsl

    if sys.platform == "darwin":
        _doctor_check_macos_host(st)
    elif is_wsl(st.env):
        _doctor_check_wsl_host(st)
    else:
        st.emit(
            "[platform]  INFO native Linux (native global hotkey/HUD require macOS or WSL2; CLI 'once --file' ready)"
        )


def _doctor_check_asr(st: _DoctorState) -> None:
    cfg, creds = st.cfg, st.creds
    asr_info = st.pipeline["asr"]
    st.eff_asr = str(asr_info["provider"])
    st.eff_asr_model = str(asr_info["model"])
    st.eff_asr_url = cfg.asr.effective_base_url(st.eff_asr)
    st.eff_asr_api = str(asr_info["api"])
    st.asr_ready = bool(asr_info["ready"])
    st.asr_is_single_hop = bool(asr_info["single_hop"])

    configured_keys: list[str] = []
    for name, item in (
        ("dashscope", creds.dashscope),
        ("deepseek", creds.deepseek),
    ):
        if item is not None:
            configured_keys.append(f"{name}={_cred_label(item)}")

    doubao_single_key = (
        creds.doubao_key is not None
        and creds.doubao_key.source_env.strip().upper() == "DOUBAO_SPEECH_API_KEY"
    )
    if doubao_single_key:
        doubao_label = f"doubao={_cred_label(creds.doubao_key)}"
        if creds.doubao_app_id is not None:
            doubao_label += f" (legacy app_id via {creds.doubao_app_id.source_env} ignored)"
        configured_keys.append(doubao_label)
    elif creds.doubao_key is not None or creds.doubao_app_id is not None:
        if creds.doubao_app_id is not None:
            configured_keys.append(
                f"doubao={_cred_label(creds.doubao_key)} (app_id={_cred_label(creds.doubao_app_id)})"
            )
        else:
            configured_keys.append(f"doubao={_cred_label(creds.doubao_key)}")

    for name, item in (
        ("volcengine", creds.volcengine),
        ("openrouter", creds.openrouter),
        ("gemini", creds.gemini),
        ("openai", creds.openai),
        ("groq", creds.groq),
        ("siliconflow", creds.siliconflow),
    ):
        if item is not None:
            configured_keys.append(f"{name}={_cred_label(item)}")
    if cfg.asr.api_key_env.strip():
        configured_keys.append(f"asr_custom={_cred_label(creds.asr_custom)}")

    auto_unconfigured = (
        str(asr_info.get("configured_provider") or "") == "auto"
        and not st.asr_ready
        and str(asr_info.get("reason") or "").startswith("auto default fallback")
    )
    if auto_unconfigured:
        st.emit(
            "[asr]       selected=auto (none configured; no Stage 1 ASR key found) -> MISSING KEY"
        )
    else:
        eff_asr_effort = _format_effort_display(
            str(asr_info.get("effort") or cfg.asr.effective_effort(st.eff_asr)),
            cfg.asr.has_explicit_effort(st.eff_asr),
        )
        st.emit(
            f"[asr]       selected={st.eff_asr} (model={st.eff_asr_model}, api={st.eff_asr_api}, effort={eff_asr_effort}) -> "
            f"{'OK' if st.asr_ready else 'MISSING KEY'}"
        )
    st.emit(f"[keys]      {', '.join(configured_keys) if configured_keys else 'none configured'}")
    if not st.asr_ready:
        st.ok_all = False
        if stage2_only := _stage2_only_key_labels(cfg, creds):
            key_names = ", ".join(stage2_only)
            st.emit(
                f"[asr]       NOTE detected Stage 2 refiner-only key ({key_names}), "
                "which cannot be used for Stage 1 speech recognition; still add 1 Stage 1 ASR key"
            )
        from .config import default_env_file_candidates

        has_cfg_or_env = bool(cfg.config_path) or any(
            p.is_file()
            for p in default_env_file_candidates(st.env, config_path=cfg.config_path or None)
        )
        init_prefix = "" if has_cfg_or_env else "run 'agent-voice-input init' then "
        env_path = _display_env_file_path(st.env, config_path=cfg.config_path or None)
        st.emit_fix(
            f"{init_prefix}set DASHSCOPE_API_KEY / DEEPSEEK_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY "
            "(or DOUBAO_SPEECH_API_KEY / OPENROUTER_API_KEY) "
            f"in {env_path}"
        )


def _doctor_check_hotwords(st: _DoctorState) -> None:
    from .hotwords import (
        HOTWORDS_MAX_BYTES,
        HOTWORDS_MAX_ENTRIES,
        STAGE2_HOTWORDS_MAX_CHARS,
        STAGE2_HOTWORDS_MAX_ITEMS,
        load_hotwords_table,
        resolve_hotwords_quota_spec,
        resolve_personal_hotwords_path,
        summarize_stage2_hotwords_budget,
    )

    s1_spec = (
        resolve_hotwords_quota_spec(
            st.eff_asr,
            model=st.eff_asr_model,
            api=st.eff_asr_api,
            base_url=st.eff_asr_url,
        )
        if st.eff_asr
        else None
    )
    s1_unsupported = s1_spec is not None and s1_spec.max_terms == 0
    s1_note = " | stage1_hotwords=unsupported (stage2 only)" if s1_unsupported else ""

    hw_path = resolve_personal_hotwords_path(env=st.env)
    if hw_path is None:
        st.hotwords_report = {
            "path": "",
            "exists": False,
            "dir_is_symlink": False,
            "dir_realpath": "",
            "counts": {
                "total": 0,
                "kept": 0,
                "dropped_count": 0,
                "used_chars": 0,
                "max_items": STAGE2_HOTWORDS_MAX_ITEMS,
                "max_chars": STAGE2_HOTWORDS_MAX_CHARS,
                "warnings_count": 0,
            },
            "issues": {},
        }
        st.emit("[hotwords]  none (HOME and AGENT_VOICE_INPUT_HOME unset)")
        return

    path_info = _inspect_hotwords_path_info(hw_path)
    table = load_hotwords_table(hw_path, env=st.env, use_cache=False, strict_encoding=False)
    s2 = summarize_stage2_hotwords_budget(table)
    safe_counts = s2.as_safe_counts_dict()
    file_symlink_rejected = any("symlink" in w.lower() for w in table.file_warnings)
    # Privacy guardrail: only integer counters, line numbers, and category flags enter doctor JSON/lines
    # (never emit hotword terms, aliases, or dropped vocabulary text).
    issues: dict[str, Any] = {
        "encoding_error": bool(table.encoding_error),
        "file_warnings_count": len(table.file_warnings),
        "file_symlink_rejected": file_symlink_rejected,
        "size_limit_exceeded": bool(table.size_limit_exceeded),
        "entry_limit_exceeded": bool(table.entry_limit_exceeded),
        "conflict_lines": list(table.conflict_lines),
        "legacy_sections_count": len(table.legacy_sections),
        "ambiguous_aliases_count": len(table.ambiguous_aliases),
        "alias_warnings_count": len(table.alias_warnings),
        "invalid_lines_count": len(table.invalid_lines),
        "stage2_truncated": bool(s2.truncated),
    }
    st.hotwords_report = {
        "path": path_info["path"],
        "exists": bool(table.exists),
        "dir_is_symlink": bool(path_info["dir_is_symlink"]),
        "dir_realpath": path_info["dir_realpath"] if path_info["dir_is_symlink"] else "",
        "counts": safe_counts,
        "issues": issues,
        "stage1_supported": not s1_unsupported,
    }

    sym_note = f" (symlink -> {path_info['dir_realpath']})" if path_info["dir_is_symlink"] else ""
    warn_parts: list[str] = []
    if table.encoding_error:
        warn_parts.append("non-UTF-8 encoding")
    if file_symlink_rejected:
        warn_parts.append("personal.txt is a symlink (symlink hotwords/ dir instead)")
    elif table.file_warnings:
        warn_parts.append(f"{len(table.file_warnings)} file warning(s)")
    if table.size_limit_exceeded:
        warn_parts.append(f"exceeded {HOTWORDS_MAX_BYTES // 1024} KiB limit")
    if table.entry_limit_exceeded:
        warn_parts.append(
            f"exceeded {HOTWORDS_MAX_ENTRIES} entries limit ({table.total_valid_entries})"
        )
    if table.conflict_lines:
        ln_csv = ",".join(str(ln) for ln in table.conflict_lines[:5])
        warn_parts.append(f"{len(table.conflict_lines)} git conflict block(s) at line(s) {ln_csv}")
    if table.legacy_sections:
        warn_parts.append(f"{len(table.legacy_sections)} legacy [section] header(s)")
    if table.ambiguous_aliases:
        warn_parts.append(f"{len(table.ambiguous_aliases)} ambiguous alias(es)")
    if table.alias_warnings:
        warn_parts.append(f"{len(table.alias_warnings)} alias conflict(s)")
    if table.invalid_lines:
        warn_parts.append(f"{len(table.invalid_lines)} invalid line(s)")
    if s2.dropped_count > 0:
        warn_parts.append(f"stage2 overflow ({s2.dropped_count} dropped)")

    if warn_parts:
        st.emit(
            f"[hotwords]  WARN {hw_path}{sym_note} "
            f"(total={safe_counts['total']}, stage2={safe_counts['kept']}/{safe_counts['max_items']}, "
            f"dropped={safe_counts['dropped_count']}; {'; '.join(warn_parts)}){s1_note}"
        )
        if file_symlink_rejected:
            st.emit_fix(
                "replace the personal.txt symlink with a regular file or symlink the hotwords/ directory instead, "
                "then run 'agent-voice-input hotwords list'"
            )
        elif table.encoding_error:
            st.emit_fix(f"re-save {hw_path} as UTF-8, then run 'agent-voice-input hotwords list'")
        else:
            st.emit_fix(
                "run 'agent-voice-input hotwords list' to inspect and resolve hotwords warnings or overflow"
            )
    elif table.exists:
        st.emit(
            f"[hotwords]  OK {hw_path}{sym_note} "
            f"(total={safe_counts['total']}, stage2={safe_counts['kept']}/{safe_counts['max_items']}, "
            f"{safe_counts['used_chars']}/{safe_counts['max_chars']} chars){s1_note}"
        )
    else:
        st.emit(f"[hotwords]  OK {hw_path}{sym_note} (not created yet; optional){s1_note}")


def _doctor_check_daemon_and_session(st: _DoctorState) -> None:
    from .control_ipc import describe_daemon_drift, probe_daemon_status
    from .session_ctx import SessionTracker

    dst = probe_daemon_status(st.rt_dir, timeout_sec=0.5)
    if dst.get("ok"):
        st.daemon_running = True
        st.daemon_pid = int(dst.get("pid") or 0) or None
        st.daemon_state = str(dst.get("state") or "idle")
        drift = describe_daemon_drift(dst)
        st.daemon_version = drift["running_version"]
        st.daemon_build_id = drift["running_build_id"]
        st.daemon_code_drift = bool(drift["drifted"])
        st.daemon_drift_reason = drift["reason"]
        ver_tag = f", version={st.daemon_version}" if st.daemon_version else ""
        st.emit(f"[daemon]    running (pid={st.daemon_pid}, state={st.daemon_state}{ver_tag})")
        if st.daemon_code_drift:
            st.emit(
                f"[daemon]    WARN {st.daemon_drift_reason}; run 'agent-voice-input restart' to update"
            )
    elif dst.get("state") == "unresponsive":
        st.daemon_running = False
        st.daemon_pid = int(dst.get("pid") or 0) or None
        st.daemon_state = "unresponsive"
        st.daemon_error = str(dst.get("error") or "control socket unresponsive")
        st.emit(
            f"[daemon]    WARN unresponsive (pid={st.daemon_pid or '-'}, {st.daemon_error}; "
            "try 'agent-voice-input restart')"
        )
    elif dst.get("state") == "error":
        st.daemon_running = False
        st.daemon_state = "error"
        st.daemon_error = str(dst.get("error") or "control socket error")
        st.ok_all = False
        st.emit(f"[daemon]    FAIL {st.daemon_error}")
    else:
        st.daemon_state = "stopped"
        st.emit("[daemon]    stopped (start via 'agent-voice-input start')")

    st.match = SessionTracker(env=st.env, runtime_dir=st.rt_dir, remote_cfg=st.cfg.remote).resolve_session(
        st.fg_title,
        window_tty=st.fg_tty,
        window_cwd=st.fg_cwd,
        window_pid=st.fg_pid,
        window_process=st.fg_process,
        bundle_id=st.fg_bundle_id,
        document=st.fg_document,
    )
    st.emit(
        f"[session]   {st.match.session_key + ' (' + st.match.matched_by + ')' if st.match else 'none matched (will use empty context)'}"
    )
    st.emit(f"[hooks]     {_inspect_hook_summary(st.env, st.rt_dir)}")
    from .integrate import inspect_integrations

    hook_statuses = inspect_integrations(
        env=st.env,
        platform=st.platform_kind,
        which_fn=_which_from_env(st.env),
    )
    st.hooks_report = {
        "necessity": hook_statuses[0].necessity if hook_statuses else "optional",
        "note": hook_statuses[0].note if hook_statuses else "",
        "active_registry_entries": _count_active_session_registry(st.rt_dir),
        "clis": {
            s.cli: {
                "installed": s.installed,
                "configured": s.configured,
                "config_path": s.config_path,
                "resolved_path": s.resolved_path,
                "is_symlink": s.is_symlink,
                "necessity": s.necessity,
                "recommended": s.necessity == "recommended",
                "detected_via": s.detected_via,
                "error": s.error,
            }
            for s in hook_statuses
        },
    }


def _doctor_check_remote_ssh(st: _DoctorState) -> None:
    from .remote_ssh import resolve_ssh_executable

    cfg = st.cfg
    if not cfg.remote.hosts:
        st.emit('[remote]    off (optional: set [remote] hosts = ["<host>"] in config.toml for SSH Linux sessions)')
        return
    missing_bins: list[str] = []
    resolved_bins: list[str] = []
    for h_cfg in cfg.remote.hosts:
        raw_sb = h_cfg.ssh_bin or cfg.remote.ssh_bin or "ssh"
        exe_sb = resolve_ssh_executable(raw_sb, env=st.env)
        if exe_sb:
            if exe_sb not in resolved_bins:
                resolved_bins.append(exe_sb)
        elif raw_sb not in missing_bins:
            missing_bins.append(raw_sb)
    st.resolved_default_ssh = resolved_bins[0] if resolved_bins else ""
    host_names_csv = ", ".join(h.name for h in cfg.remote.hosts)
    if missing_bins:
        st.remote_ssh_ok = False
        st.ok_all = False
        st.emit(f"[remote]    FAIL hosts=[{host_names_csv}] missing ssh_bin={', '.join(missing_bins)}")
        st.emit_fix('install openssh-client or set remote.ssh_bin = "ssh.exe" on WSL2')
    else:
        st.remote_ssh_ok = True
        st.emit(
            f"[remote]    OK hosts=[{host_names_csv}] (timeout_ms={cfg.remote.timeout_ms}, ssh={st.resolved_default_ssh})"
        )


def _doctor_check_refiner(st: _DoctorState) -> None:
    from .config import find_agy_bin
    from .llm_common import _missing_key_hint
    from .refiner import (
        find_codex_bin,
        inspect_codex_auth_status,
    )

    cfg = st.cfg
    ref_info = st.pipeline["refiner"]
    raw_ref_prov = str(ref_info["configured_provider"])
    st.raw_ref_prov = raw_ref_prov
    st.resolved_ref_prov = str(ref_info["provider"])
    extra_n = len(cfg.refiner.extra_instructions)
    extra_tag = f", extra_instructions={extra_n}" if extra_n else ""

    if raw_ref_prov == "off":
        st.emit("[refiner]   off")
        st.emit(f"[pipeline]  {st.pipeline['summary']}")
        return
    if ref_info["bypassed"]:
        target_lbl = st.resolved_ref_prov if raw_ref_prov != "auto" else "Stage 2"
        st.emit(
            f"[refiner]   bypassed (single-hop ASR passthrough{extra_tag}; set refiner.force_on_single_hop=true to run {target_lbl})"
        )
        st.emit(f"[pipeline]  {st.pipeline['summary']}")
        return
    if raw_ref_prov == "auto" and st.resolved_ref_prov == "off":
        st.emit(
            "[refiner]   WARN auto -> off (no Stage 2 credentials set; raw Stage 1 ASR draft will be delivered. "
            "Optional fix: set DEEPSEEK_API_KEY / DASHSCOPE_API_KEY or run 'codex login')"
        )
        st.emit(f"[pipeline]  {st.pipeline['summary']}")
        return
    if st.resolved_ref_prov == "codex":
        eff_ref_api = str(ref_info["api"])
        eff_ref_effort = _format_effort_display(
            str(ref_info["effort"]), cfg.refiner.has_explicit_effort("codex")
        )
        st.eff_ref_model = str(ref_info["model"])
        prov_disp = "auto -> codex" if raw_ref_prov == "auto" else "codex"
        auth_state, auth_note = inspect_codex_auth_status(cfg, st.env)
        if auth_state in ("OK", "WARN"):
            st.ref_ping_needed = True
            cbin = find_codex_bin(st.env)
            bin_tag = f", refresh_bin={cbin}" if cbin else ""
            st.emit(
                f"[refiner]   {auth_state} {prov_disp} (model={st.eff_ref_model} "
                f"[{_check_codex_model(st.eff_ref_model, st.env)}], api={eff_ref_api}, "
                f"effort={eff_ref_effort}, {auth_note}{bin_tag}{extra_tag})"
            )
        else:
            st.emit(
                f"[refiner]   FAIL {prov_disp} (model={st.eff_ref_model}, api={eff_ref_api}, "
                f"effort={eff_ref_effort}{extra_tag}) -> {auth_note}"
            )
            env_path = _display_env_file_path(st.env, config_path=cfg.config_path or None)
            st.emit_fix(f"run 'codex login' or set DEEPSEEK_API_KEY / DASHSCOPE_API_KEY in {env_path}")
            st.ok_all = False
        st.emit(f"[pipeline]  {st.pipeline['summary']}")
        return
    if st.resolved_ref_prov == "agy":
        eff_ref_api = str(ref_info["api"])
        eff_ref_effort = _format_effort_display(
            str(ref_info["effort"]), cfg.refiner.has_explicit_effort("agy")
        )
        st.eff_ref_model = str(ref_info["model"])
        agy_bin = find_agy_bin(st.env)
        if agy_bin:
            st.ref_ping_needed = True
            st.emit(
                f"[refiner]   OK agy (model={st.eff_ref_model}, api={eff_ref_api}, "
                f"effort={eff_ref_effort}, bin={agy_bin}{extra_tag})"
            )
        else:
            st.emit(
                f"[refiner]   FAIL agy (model={st.eff_ref_model}, api={eff_ref_api}, "
                f"effort={eff_ref_effort}{extra_tag}) -> agy CLI not found in PATH"
            )
            st.emit_fix("install Antigravity CLI ('agy'), set AGY_BIN, or switch refiner.provider")
            st.ok_all = False
        st.emit(f"[pipeline]  {st.pipeline['summary']}")
        return

    eff_ref_api = str(ref_info["api"])
    eff_ref_effort = _format_effort_display(
        str(ref_info["effort"]), cfg.refiner.has_explicit_effort(st.resolved_ref_prov)
    )
    st.eff_ref_model = str(ref_info["model"])
    st.eff_ref_url = cfg.refiner.effective_base_url(st.resolved_ref_prov)
    prov_disp = f"auto -> {st.resolved_ref_prov}" if raw_ref_prov == "auto" else st.resolved_ref_prov
    ref_ready = bool(ref_info["ready"])
    st.emit(
        f"[refiner]   {'OK' if ref_ready else 'MISSING KEY'} {prov_disp} "
        f"(model={st.eff_ref_model}, api={eff_ref_api}, effort={eff_ref_effort}{extra_tag})"
    )
    if ref_ready:
        st.ref_ping_needed = True
    else:
        st.ok_all = False
        if cfg.refiner.api_key_env.strip():
            env_path = _display_env_file_path(st.env, config_path=cfg.config_path or None)
            st.emit_fix(f"set {cfg.refiner.api_key_env.strip()} in {env_path}")
        else:
            st.emit_fix(_missing_key_hint(st.resolved_ref_prov, stage="refiner"))
    st.emit(f"[pipeline]  {st.pipeline['summary']}")


def _doctor_run_pings(
    st: _DoctorState,
    *,
    ping: bool,
    remote_ping: bool,
    transport: HttpTransport | None,
) -> None:
    from .remote_ssh import RemoteSshManager

    if ping:
        ping_cache: dict[tuple[str, str, str], Any] = {}
        if st.asr_ready:
            st_asr, ms_asr, msg_asr = _probe_stage_ping(
                stage="asr",
                provider=st.eff_asr,
                cfg=st.cfg,
                creds=st.creds,
                env=st.env,
                base_url=st.eff_asr_url,
                transport=transport,
                cache=ping_cache,
                target_model=st.eff_asr_model,
            )
            st.ping_results["asr"] = {
                "provider": st.eff_asr,
                "status": st_asr,
                "latency_ms": ms_asr,
                "detail": st.clean(msg_asr),
            }
            st.emit(f"[ping]      asr({st.eff_asr}): {msg_asr}")
            if st_asr == "FAIL":
                st.ok_all = False
        if st.ref_ping_needed:
            st_ref, ms_ref, msg_ref = _probe_stage_ping(
                stage="refiner",
                provider=st.resolved_ref_prov,
                cfg=st.cfg,
                creds=st.creds,
                env=st.env,
                base_url=st.eff_ref_url,
                transport=transport,
                cache=ping_cache,
                target_model=st.eff_ref_model,
            )
            st.ping_results["refiner"] = {
                "provider": st.resolved_ref_prov,
                "status": st_ref,
                "latency_ms": ms_ref,
                "detail": st.clean(msg_ref),
            }
            st.emit(f"[ping]      refiner({st.resolved_ref_prov}): {msg_ref}")
            if st_ref == "FAIL":
                st.ok_all = False

    if ping or remote_ping:
        if not st.cfg.remote.hosts:
            if remote_ping:
                st.emit("[ping]      remote: skipped (no [remote] hosts configured in config.toml)")
                st.ping_results["remote"] = {}
        elif st.remote_ssh_ok:
            rem_ping_map: dict[str, dict[str, Any]] = {}
            rm_doc = RemoteSshManager(st.cfg.remote, env=st.env, runtime_dir=st.rt_dir)
            try:
                for h_cfg in st.cfg.remote.hosts:
                    st_rm, ms_rm, msg_rm = rm_doc.probe_host_ping(h_cfg, timeout_sec=3.0)
                    rem_ping_map[h_cfg.name] = {
                        "ssh_target": h_cfg.ssh_target,
                        "status": st_rm,
                        "latency_ms": ms_rm,
                        "detail": st.clean(msg_rm),
                    }
                    st.emit(f"[ping]      remote({h_cfg.name}): {msg_rm}")
                    if st_rm == "FAIL":
                        st.ok_all = False
            finally:
                rm_doc.close()
            st.ping_results["remote"] = rem_ping_map


def _doctor_emit_json_report(st: _DoctorState, *, include_ping: bool) -> None:
    def _redact_json_obj(val: Any) -> Any:
        if isinstance(val, str):
            return st.clean(val)
        if isinstance(val, dict):
            return {str(k): _redact_json_obj(v) for k, v in val.items()}
        if isinstance(val, (list, tuple)):
            return [_redact_json_obj(v) for v in val]
        return val

    from .config import format_archive_status_dict

    cfg = st.cfg
    daemon_report: dict[str, Any] = {
        "running": st.daemon_running,
        "pid": st.daemon_pid,
        "state": st.daemon_state,
        "version": st.daemon_version,
        "build_id": st.daemon_build_id,
        "code_drift": st.daemon_code_drift,
    }
    if st.daemon_drift_reason:
        daemon_report["drift_reason"] = st.daemon_drift_reason
    if st.daemon_error:
        daemon_report["error"] = st.daemon_error
    report: dict[str, Any] = {
        "ok": st.ok_all,
        "version": __version__,
        "platform": st.platform_kind,
        "config_path": cfg.config_path or "",
        "ffmpeg_ok": st.ffmpeg_ok,
        "daemon": daemon_report,
        "pipeline": st.pipeline,
        "asr": {
            "provider": st.eff_asr,
            "model": st.eff_asr_model,
            "api": st.eff_asr_api,
            "ready": st.asr_ready,
            "single_hop": st.asr_is_single_hop,
        },
        "refiner": {
            "configured_provider": st.raw_ref_prov,
            "resolved_provider": st.resolved_ref_prov,
            "model": st.eff_ref_model,
            "bypassed": bool(
                st.asr_is_single_hop and not cfg.refiner.force_on_single_hop and st.raw_ref_prov != "off"
            ),
        },
        "remote": {
            "enabled": cfg.remote.enabled,
            "hosts": [h.name for h in cfg.remote.hosts],
            "timeout_ms": cfg.remote.timeout_ms,
            "ssh_bin": st.resolved_default_ssh or cfg.remote.ssh_bin,
            "ssh_bin_ok": st.remote_ssh_ok,
        },
        "session": (
            {"session_key": st.match.session_key, "matched_by": st.match.matched_by}
            if st.match is not None
            else None
        ),
        "hooks": st.hooks_report,
        "hotwords": st.hotwords_report,
        "archive": format_archive_status_dict(cfg.archive),
        "migration_hint": st.migration_hint,
        "fixes": st.fixes,
        "lines": st.lines_out,
    }
    if include_ping:
        report["ping"] = st.ping_results
    print(_json_dumps_stdout(_redact_json_obj(report), indent=2))


def _format_doctor_next_line(st: _DoctorState) -> str:
    if not st.ok_all:
        return "Next: fix the FAIL item(s) above (see Fix:), then re-run 'agent-voice-input doctor --ping'"
    if st.daemon_running and st.daemon_code_drift:
        reason = st.daemon_drift_reason
        suffix = f" ({reason})" if reason else ""
        return f"Next: run 'agent-voice-input restart' to apply changes{suffix}"
    if st.daemon_state == "unresponsive":
        return "Next: daemon is unresponsive; run 'agent-voice-input diagnose' or 'agent-voice-input restart'"
    if st.daemon_running:
        hk = (st.cfg.hotkey.key or "RControl").strip() or "RControl"
        verb = (
            "hold"
            if (st.cfg.hotkey.trigger_mode or "tap_toggle").strip().lower() == "ptt"
            else "tap"
        )
        return f"Next: Ready — daemon is running; {verb} {hk} to dictate"
    return "Next: run 'agent-voice-input start' to start the background daemon"


def _cmd_doctor(
    cfg: Config,
    env: dict[str, str],
    *,
    ping: bool = False,
    remote_ping: bool = False,
    as_json: bool = False,
    transport: HttpTransport | None = None,
) -> int:
    from .config import (
        explain_pipeline_selection,
        format_archive_summary,
        get_runtime_dir,
        resolve_credentials,
    )

    creds = resolve_credentials(cfg, env)
    pipeline = explain_pipeline_selection(cfg, creds, env)
    st = _DoctorState(
        cfg=cfg,
        env=env,
        creds=creds,
        rt_dir=get_runtime_dir(env, create=False),
        as_json=as_json,
        all_secrets=tuple(creds.all_secret_values()),
        pipeline=pipeline,
    )
    _doctor_check_env_and_ffmpeg(st)
    _doctor_check_platform(st)
    _doctor_check_asr(st)
    _doctor_check_hotwords(st)
    _doctor_check_daemon_and_session(st)
    _doctor_check_remote_ssh(st)
    _doctor_check_refiner(st)
    st.emit(f"Archive: {format_archive_summary(st.cfg.archive)}")
    _doctor_run_pings(st, ping=ping, remote_ping=remote_ping, transport=transport)
    st.emit(_format_doctor_next_line(st))
    if as_json:
        _doctor_emit_json_report(st, include_ping=bool(ping or remote_ping))
    return 0 if st.ok_all else 1


def _run_wav_or_live(
    *,
    wav_file: str | Path | None,
    audio_source: str,
    duration: float,
    dry_run: bool,
    title: str | None,
    cfg: Config,
    env: dict[str, str],
    suppress_stdout_on_paste: bool = False,
    force_print: bool = False,
) -> int:
    from .config import get_runtime_dir, redact_secrets, resolve_credentials
    from .control_ipc import append_diag_record, format_local_iso_ts, generate_utterance_id
    from .daemon import create_platform
    from .daemon_pipeline import build_pipeline_summary, run_pipeline_once
    from .platform_common import ForegroundWindow
    from .platform_wsl import is_wsl

    rt_dir = get_runtime_dir(env, create=True)
    is_native_linux = sys.platform == "linux" and not is_wsl(env)
    plat = (
        None
        if (wav_file and dry_run and (title is not None or is_native_linux))
        else create_platform(cfg, env=env)
    )
    try:
        win = ForegroundWindow(0, "", title or "") if plat is None else plat.get_foreground_window()
        if wav_file:
            raw_wav: bytes | str | Path = wav_file
            src = audio_source
        else:
            assert plat is not None
            plat.start_recording(rt_dir)
            print(f"Recording {duration:.1f}s ...", file=sys.stderr, flush=True)
            time.sleep(max(0.2, float(duration)))
            raw_wav, src = plat.stop_recording()
        try:
            res = run_pipeline_once(
                raw_wav, cfg=cfg, platform=plat, origin_window=win, window_title_override=title,
                audio_source=src, dry_run=dry_run, runtime_dir=rt_dir, env=env,
            )
        except Exception as exc:
            creds_err = resolve_credentials(cfg, env)
            saved_p = str(getattr(exc, "_saved_audio_path", "") or "")
            ds_err = str(
                getattr(exc, "_delivery_status", "")
                or ("undelivered_recoverable" if saved_p else "undelivered_unrecoverable")
            )
            err_rec: dict[str, Any] = {
                "id": generate_utterance_id(),
                "ts": format_local_iso_ts(),
                "seq_id": 1,
                "ok": False,
                "audio_source": src,
                "delivery_status": ds_err,
                "error": redact_secrets(str(exc), env, cfg=cfg, extra_secrets=creds_err.all_secret_values()),
            }
            if sk_err := str(getattr(exc, "_skipped_reason", "") or ""):
                err_rec["skipped_reason"] = sk_err
            if pr_err := str(getattr(exc, "_paste_reason", "") or ""):
                err_rec["paste_reason"] = pr_err
            if saved_p:
                err_rec["saved_audio"] = saved_p
                print(
                    f"[agent-voice-input] saved failed audio: {saved_p} (recover via 'agent-voice-input retry')",
                    file=sys.stderr,
                )
            append_diag_record(rt_dir, err_rec)
            raise
        creds_ok = resolve_credentials(cfg, env)
        summary = build_pipeline_summary(
            res.seq_id,
            res,
            dry_run=dry_run,
            redact_fn=lambda s: redact_secrets(s, env, cfg=cfg, extra_secrets=creds_ok.all_secret_values()),
        )
        append_diag_record(rt_dir, summary)
        if not res.ok:
            msg = f"[agent-voice-input] skipped: {res.skipped_reason}"
            if res.saved_audio:
                msg += f" (saved: {res.saved_audio}; recover via 'agent-voice-input retry')"
            print(msg, file=sys.stderr)
            if res.final_text and not dry_run:
                print(res.final_text)
            return 1
        if res.asr_missed_chunks > 0:
            miss_msg = (
                f"[agent-voice-input] WARN: {res.asr_missed_chunks} voiced audio chunk(s) returned empty ASR"
            )
            if res.saved_audio:
                miss_msg += f" (saved: {res.saved_audio}; recover via 'agent-voice-input retry')"
            print(miss_msg, file=sys.stderr)
        if dry_run and res.asr:
            fb = f" fallback={res.refined.used_fallback}" if (res.refined and res.refined.used_fallback) else ""
            tok_tag = ""
            if res.refined and res.refined.input_tokens is not None:
                c_tok = res.refined.cached_input_tokens
                if c_tok is not None and res.refined.input_tokens > 0:
                    pct = 100.0 * c_tok / res.refined.input_tokens
                    tok_tag = f" tokens={res.refined.input_tokens}(cached={c_tok},{pct:.0f}%)"
                else:
                    tok_tag = f" tokens={res.refined.input_tokens}"
            r_tag = f"{res.refined.provider}/{res.refined.model} {res.refined.latency_ms}ms{fb}{tok_tag}" if res.refined else "off"
            ch_tag = f" chunks={res.asr_chunks}" if res.asr_chunks > 1 else ""
            print(
                f"[session] {res.session.session_key if res.session else 'none'}\n"
                f"[stage1]  ({res.asr.provider}/{res.asr.model} {res.asr.latency_ms}ms{ch_tag}): {res.asr.text}\n"
                f"[stage2]  ({r_tag}): {res.final_text}",
                file=sys.stderr,
            )
        if res.paste is not None and res.paste.pasted:
            if suppress_stdout_on_paste:
                print(f"[agent-voice-input] pasted ({len(res.final_text)} chars)", file=sys.stderr)
                if force_print:
                    print(res.final_text)
            else:
                print(res.final_text)
        else:
            if suppress_stdout_on_paste and res.paste is not None and res.paste.clipboard:
                print(
                    f"[agent-voice-input] copied to clipboard ({len(res.final_text)} chars; {res.paste.reason})",
                    file=sys.stderr,
                )
            print(res.final_text)
        return 0
    finally:
        if plat is not None:
            plat.close()


def _cmd_once(args: argparse.Namespace, cfg: Config, env: dict[str, str]) -> int:
    return _run_wav_or_live(
        wav_file=args.wav_file,
        audio_source="file",
        duration=args.duration,
        dry_run=args.dry_run,
        title=args.title,
        cfg=cfg,
        env=env,
    )


def _cmd_retry(args: argparse.Namespace, cfg: Config, env: dict[str, str]) -> int:
    from .audio_proc import get_failed_audio_dir, list_failed_audios, resolve_failed_audio
    from .config import get_runtime_dir

    rt_dir = get_runtime_dir(env, create=False)
    if args.list_only:
        items = list_failed_audios(rt_dir)
        if not items:
            print(f"no saved failed recordings in {get_failed_audio_dir(rt_dir)}")
            return 0
        for idx, p in enumerate(items, 1):
            try:
                st = p.stat()
                ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))
                kb = max(1, int(round(st.st_size / 1024.0)))
            except OSError:
                ts_str, kb = "-", 0
            print(f"{idx}. [{ts_str}] {p.name} ({kb} KB) -> {p}")
        return 0
    wav_path = resolve_failed_audio(rt_dir, args.target)
    print(f"[agent-voice-input] retrying failed audio: {wav_path}", file=sys.stderr, flush=True)
    return _run_wav_or_live(
        wav_file=wav_path,
        audio_source="retry",
        duration=0.0,
        dry_run=args.dry_run,
        title=args.title,
        cfg=cfg,
        env=env,
        suppress_stdout_on_paste=True,
        force_print=bool(getattr(args, "print_stdout", False)),
    )


def _sanitize_daemon_tmux_env(env: Mapping[str, str]) -> dict[str, str]:
    """Strip ``TMUX`` / ``TMUX_PANE`` from global daemon env while keeping socket discovery hint."""
    out = dict(env)
    out.pop("TMUX_PANE", None)
    if raw_tmux := out.pop("TMUX", "").strip():
        out.setdefault("AGENT_VOICE_INPUT_TMUX_HINT", raw_tmux)
    return out


_CLI_OVERRIDE_FLAGS: tuple[tuple[str, str], ...] = (
    ("--config", "config"),
    ("--hotkey", "hotkey"),
    ("--asr-provider", "asr"),
    ("--asr-model", "asr_model"),
    ("--asr-api", "asr_api"),
    ("--asr-effort", "asr_effort"),
    ("--refiner-provider", "refiner"),
    ("--refiner-model", "refiner_model"),
    ("--refiner-api", "refiner_api"),
    ("--refiner-effort", "effort"),
)


def _trim_log_file_if_needed(
    log_path: Path, max_bytes: int = 1_048_576, keep_bytes: int = 262_144
) -> None:
    """Atomically retain only the tail of a regular log file once it exceeds ``max_bytes``."""
    try:
        st = os.lstat(log_path)
        if not stat.S_ISREG(st.st_mode) or st.st_size <= max_bytes:
            return
        keep = max(0, int(keep_bytes))
        if keep <= 0:
            tail = b""
        else:
            with open(log_path, "rb") as f:
                if st.st_size > keep:
                    f.seek(st.st_size - keep)
                tail = f.read()
            if st.st_size > keep:
                nl = tail.find(b"\n")
                if 0 <= nl < len(tail) - 1:
                    tail = tail[nl + 1 :]
        tmp_path = log_path.parent / f".{log_path.name}.trim.{os.getpid()}.{time.monotonic_ns()}.tmp"
        fd = os.open(str(tmp_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        closed_by_file = False
        try:
            os.fchmod(fd, 0o600)
            fobj = os.fdopen(fd, "wb")
            closed_by_file = True
            with fobj as tf:
                tf.write(tail)
                tf.flush()
            os.replace(tmp_path, log_path)
        except Exception:
            if not closed_by_file:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
    except Exception:
        pass


def _warn_degraded_shim(st: Mapping[str, Any]) -> None:
    if st.get("shim_ok") is False and (se := str(st.get("shim_error") or "").strip()):
        print(
            f"[agent-voice-input] WARN: native hotkey/HUD helper unavailable ({se}); "
            "CLI 'toggle'/'once' remain available.",
            file=sys.stderr,
            flush=True,
        )


def _format_start_readiness_row(
    cfg: Config,
    *,
    asr_ready: bool,
    hotkey_ready: bool = True,
    warming_up: bool = False,
) -> tuple[str, str] | None:
    if not asr_ready:
        return (
            "Warning",
            "no Stage 1 ASR key configured; run 'agent-voice-input doctor' before dictating",
        )
    if not hotkey_ready:
        if warming_up:
            return None
        return (
            "Hotkey",
            "unavailable (fix warning above, then run 'agent-voice-input restart')",
        )
    hotkey = (cfg.hotkey.key or "RControl").strip() or "RControl"
    mode = (cfg.hotkey.trigger_mode or "tap_toggle").strip().lower()
    if mode == "ptt":
        return (
            "Hotkey",
            f"hold {hotkey} to speak, release to transcribe (press another key to cancel)",
        )
    hc_ms = cfg.hotkey.effective_hold_cancel_ms()
    if hc_ms > 0:
        sec_str = f"{round(hc_ms / 1000.0, 2):g}s"
        return ("Hotkey", f"tap {hotkey} to start/stop (hold {sec_str} to cancel)")
    return ("Hotkey", f"tap {hotkey} to start/stop")


def _cmd_start(
    args: argparse.Namespace,
    env: dict[str, str],
    *,
    cfg: Config | None = None,
    restarted_from_pid: int | None = None,
    ping_transport: HttpTransport | None = None,
) -> int:
    from .config import (
        explain_pipeline_selection,
        format_archive_summary,
        get_runtime_dir,
        redact_secrets,
        resolve_credentials,
    )
    from .control_ipc import (
        DIAG_FILENAME,
        _LEGACY_DIAG_FILENAME,
        describe_daemon_drift,
        probe_daemon_status,
        send_control_command,
    )
    from .diagnose import read_redacted_log_tail
    from .platform_macos import (
        classify_macos_mic_permission,
        compile_mac_shim,
        run_mac_shim_self_check,
    )

    rt_dir = get_runtime_dir(env, create=True)
    passed_flags = [f for f, attr in _CLI_OVERRIDE_FLAGS if getattr(args, attr, None)]
    has_overrides = bool(passed_flags)
    creds: ProviderCredentials | None = None
    pipe_info: dict[str, Any] | None = None
    asr_ready: bool | None = None
    if cfg is not None:
        creds = resolve_credentials(cfg, env)
        pipe_info = explain_pipeline_selection(cfg, creds, env)
        asr_ready = bool(pipe_info["asr"]["ready"])
    st = probe_daemon_status(rt_dir, timeout_sec=1.0)
    if st.get("ok"):
        _warn_degraded_shim(st)
        drift = describe_daemon_drift(st)
        reasons: list[str] = [drift["reason"]] if drift["drifted"] and drift["reason"] else []
        if passed_flags:
            if len(passed_flags) == 1:
                reasons.append(f"new CLI flag passed ({passed_flags[0]})")
            elif len(passed_flags) == 2:
                reasons.append(f"new CLI flags passed ({', '.join(passed_flags)})")
            else:
                reasons.append(f"new CLI flags passed ({len(passed_flags)} overrides)")
        print(_kv_line("Daemon", f"already running (pid={st.get('pid')}, {st.get('state')})"))
        if reasons:
            print(_kv_line("Notice", "; ".join(reasons)))
        running_pipe = st.get("pipeline")
        if isinstance(running_pipe, Mapping) and running_pipe:
            for row in _format_pipeline_rows(running_pipe):
                print(row)
        elif pipe_info is not None and not has_overrides:
            for row in _format_pipeline_rows(pipe_info, cfg=cfg, creds=creds):
                print(row)
        running_arch = st.get("archive")
        if isinstance(running_arch, Mapping):
            print(_kv_line("Archive", format_archive_summary(running_arch)))
        elif cfg is not None:
            print(_kv_line("Archive", format_archive_summary(cfg.archive)))
        next_msg = (
            "run 'agent-voice-input restart' to apply changes"
            if reasons
            else "run 'agent-voice-input restart' to reload config"
        )
        print(_kv_line("Next", next_msg))
        return 0
    if st.get("state") == "unresponsive":
        err_note = redact_secrets(
            str(st.get("error") or "control socket temporarily unresponsive"),
            env,
            cfg=cfg,
        )
        print(_kv_line("Daemon", f"already running (pid={st.get('pid') or '-'}, unresponsive)"))
        print(_kv_line("Detail", err_note))
        print(_kv_line("Next", "run 'agent-voice-input diagnose' or 'agent-voice-input restart'"))
        return 0
    if st.get("state") == "error":
        err_note = redact_secrets(
            str(st.get("error") or "control socket error"),
            env,
            cfg=cfg,
        )
        print(
            f"error: cannot verify daemon state in {rt_dir}: {err_note}",
            file=sys.stderr,
        )
        return 1
    src_root = Path(__file__).resolve().parents[1]
    child_env = _sanitize_daemon_tmux_env(os.environ)
    if (src_root / "agent_voice_input").is_dir():
        prev_pp = child_env.get("PYTHONPATH", "")
        child_env["PYTHONPATH"] = f"{src_root}{os.pathsep}{prev_pp}" if prev_pp else str(src_root)
    from .audio_proc import resolve_ffmpeg_bin

    try:
        ff_found = resolve_ffmpeg_bin(env=env)
    except Exception:
        ff_found = ""
    if not ff_found:
        ff_install = "brew install ffmpeg" if sys.platform == "darwin" else "sudo apt install ffmpeg"
        print(
            f"[agent-voice-input] WARN: ffmpeg not found (required for audio capture/conversion); install via: {ff_install}",
            file=sys.stderr,
            flush=True,
        )
    warned_shim = False
    if sys.platform == "darwin" and cfg is not None:
        shim_bin, pre_err = compile_mac_shim(env=env, cfg=cfg, announce=True)
        if shim_bin is None:
            if pre_err:
                warned_shim = True
                print(
                    f"[agent-voice-input] WARN: native hotkey/HUD helper unavailable ({pre_err}); "
                    "daemon will start in CLI-only mode ('toggle'/'once').",
                    file=sys.stderr,
                    flush=True,
                )
            ssh_env = (
                {**child_env, **{k: env[k] for k in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY") if k in env}}
            )
            if (ssh_warn := classify_macos_mic_permission({"mic_auth": "not_determined"}, ssh_env)) is not None:
                print(
                    f"[agent-voice-input] WARN: {redact_secrets(str(ssh_warn), env, cfg=cfg)}",
                    file=sys.stderr,
                    flush=True,
                )
        else:
            sc_ok, sc_json, _sc_err = run_mac_shim_self_check(shim_bin, child_env, cfg=cfg)
            if sc_ok:
                aud_st = sc_json.get("audio_status")
                perm_env = (
                    {**child_env, **{k: env[k] for k in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY") if k in env}}
                )
                if (
                    isinstance(aud_st, Mapping)
                    and (perm_err := classify_macos_mic_permission(aud_st, perm_env)) is not None
                ):
                    print(
                        f"[agent-voice-input] WARN: {redact_secrets(str(perm_err), env, cfg=cfg)}",
                        file=sys.stderr,
                        flush=True,
                    )
                if (
                    sc_json.get("ax_trusted") is False
                    and cfg.hotkey.scope != "terminals"
                ):
                    print(
                        "[agent-voice-input] Tip: macOS Accessibility is not granted for the terminal starting "
                        "the daemon (Ghostty/tmux direct delivery works; grant Accessibility to this terminal "
                        'for auto-paste in other apps: open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")',
                        file=sys.stderr,
                        flush=True,
                    )
    cmd = [
        sys.executable,
        "-m",
        "agent_voice_input.cli",
        "daemon",
        *(
            x
            for f, attr in _CLI_OVERRIDE_FLAGS
            if (v := getattr(args, attr, None))
            for x in (f, v)
        ),
    ]
    log_path = rt_dir / "daemon.log"
    _trim_log_file_if_needed(log_path)
    try:
        st_before = os.lstat(log_path)
        start_offset = st_before.st_size if stat.S_ISREG(st_before.st_mode) else 0
    except OSError:
        start_offset = 0
    fd = os.open(str(log_path), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as log_f:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=log_f,
            env=child_env,
            start_new_session=True,
        )
    from .platform_wsl import (
        WSL_COLD_START_PROGRESS_MSG,
        WSL_SHIM_COLD_TIMEOUT_SEC,
        _WSL_SLOW_START_NOTICE_SEC,
        is_wsl,
    )

    is_wsl_host = sys.platform != "darwin" and is_wsl(env)
    start_wait_sec = WSL_SHIM_COLD_TIMEOUT_SEC if is_wsl_host else 10.0
    start_t0 = time.monotonic()
    deadline = start_t0 + start_wait_sec
    wsl_progress_announced = False
    while (now := time.monotonic()) < deadline:
        if proc.poll() is not None:
            print(f"daemon exited early (rc={proc.returncode}); check {log_path}", file=sys.stderr)
            if diag := read_redacted_log_tail(log_path, start_offset, env, cfg=cfg):
                print(diag, file=sys.stderr)
            return 1
        if (
            is_wsl_host
            and not wsl_progress_announced
            and (now - start_t0) >= _WSL_SLOW_START_NOTICE_SEC
        ):
            wsl_progress_announced = True
            print(WSL_COLD_START_PROGRESS_MSG, file=sys.stderr, flush=True)
        try:
            if (st := send_control_command(rt_dir, {"cmd": "status"}, timeout_sec=0.5)).get("ok"):
                if not warned_shim:
                    _warn_degraded_shim(st)
                new_pid = st.get("pid")
                if restarted_from_pid is not None and restarted_from_pid > 0:
                    print(_kv_line("Daemon", f"restarted (pid={restarted_from_pid} -> {new_pid})"))
                else:
                    print(_kv_line("Daemon", f"started (pid={new_pid})"))
                for row in _format_pipeline_rows(pipe_info, cfg=cfg, creds=creds):
                    print(row)
                started_arch = st.get("archive")
                if isinstance(started_arch, Mapping):
                    print(_kv_line("Archive", format_archive_summary(started_arch)))
                elif cfg is not None:
                    print(_kv_line("Archive", format_archive_summary(cfg.archive)))
                if cfg is not None and asr_ready is not None:
                    hotkey_ok = st.get("shim_ok") is True
                    if readiness := _format_start_readiness_row(
                        cfg,
                        asr_ready=asr_ready,
                        hotkey_ready=hotkey_ok,
                    ):
                        print(_kv_line(readiness[0], readiness[1]))
                        if not asr_ready and not hotkey_ok:
                            print(
                                _kv_line(
                                    "Hotkey",
                                    "unavailable (fix warning above, then run 'agent-voice-input restart')",
                                )
                            )
                        if (
                            asr_ready
                            and hotkey_ok
                            and not (rt_dir / DIAG_FILENAME).exists()
                            and not (rt_dir / _LEGACY_DIAG_FILENAME).exists()
                        ):
                            hk = (cfg.hotkey.key or "RControl").strip() or "RControl"
                            verb = (
                                "hold"
                                if (cfg.hotkey.trigger_mode or "tap_toggle").strip().lower() == "ptt"
                                else "tap"
                            )
                            print(
                                _kv_line(
                                    "Tip",
                                    f"{verb} {hk} to test live, or run 'agent-voice-input once --duration 4 --dry-run'",
                                )
                            )
                for tip_line in _format_integrate_tip_lines(env):
                    print(tip_line)
                if cfg is not None and creds is not None and pipe_info is not None:
                    try:
                        for lbl, val in _run_startup_preflight_ping(
                            cfg=cfg,
                            creds=creds,
                            env=env,
                            pipe_info=pipe_info,
                            transport=ping_transport,
                            timeout_sec=3.5,
                        ):
                            print(_kv_line(lbl, val))
                    except Exception:
                        pass
                return 0
        except Exception:
            time.sleep(0.15)
    if restarted_from_pid is not None and restarted_from_pid > 0:
        print(
            _kv_line(
                "Daemon",
                f"restarting (pid={restarted_from_pid} -> {proc.pid}, socket warming up)",
            )
        )
    else:
        print(_kv_line("Daemon", f"starting (pid={proc.pid}, socket warming up)"))
    for row in _format_pipeline_rows(pipe_info, cfg=cfg, creds=creds):
        print(row)
    if cfg is not None:
        print(_kv_line("Archive", format_archive_summary(cfg.archive)))
    if cfg is not None and asr_ready is not None:
        if readiness := _format_start_readiness_row(
            cfg,
            asr_ready=asr_ready,
            hotkey_ready=False,
            warming_up=True,
        ):
            print(_kv_line(readiness[0], readiness[1]))
    return 0


def _try_open_daemon_lock_fd(rt_dir: Path, *, create: bool = False) -> int | None:
    lock_file = rt_dir / "daemon.lock"
    flags = (
        os.O_RDWR
        | (os.O_CREAT if create else 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        fd = os.open(str(lock_file), flags, 0o600)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            os.close(fd)
            return None
        if create:
            os.fchmod(fd, 0o600)
        os.set_inheritable(fd, False)
        return fd
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        return None


def _wait_daemon_lock_free(rt_dir: Path, timeout_sec: float = 6.0) -> bool:
    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    while True:
        if not (rt_dir / "daemon.lock").exists():
            return True
        fd = _try_open_daemon_lock_fd(rt_dir, create=False)
        if fd is None:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            if isinstance(exc, OSError) and exc.errno not in (
                errno.EACCES,
                errno.EAGAIN,
                errno.EWOULDBLOCK,
            ):
                return False
        else:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            return True
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _read_pid_file(pid_file: Path) -> int:
    try:
        val = int(pid_file.read_text(encoding="utf-8").strip()) if pid_file.is_file() else 0
        return val if val > 0 else 0
    except (OSError, ValueError):
        return 0


def _probe_pid_liveness(pid: int) -> str:
    """Return ``'gone'``, ``'alive'``, or ``'perm'`` via zero-signal ``os.kill(pid, 0)``."""
    if pid <= 0:
        return "gone"
    try:
        os.kill(pid, 0)
        return "alive"
    except ProcessLookupError:
        return "gone"
    except PermissionError:
        return "perm"
    except OSError:
        return "gone"


def _read_process_cmdline(pid: int) -> list[str] | None:
    """Best-effort read of argv tokens for ``pid`` (Linux ``/proc`` or POSIX ``ps``)."""
    if pid <= 0:
        return None
    proc_cmdline = Path(f"/proc/{pid}/cmdline")
    try:
        if proc_cmdline.is_file():
            raw = proc_cmdline.read_bytes()
            if raw:
                return [tok.decode("utf-8", errors="replace") for tok in raw.split(b"\x00") if tok]
    except OSError:
        pass
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    line = (proc.stdout or "").strip()
    if not line:
        return None
    try:
        return shlex.split(line)
    except ValueError:
        return line.split()


def _is_avi_daemon_pid(pid: int) -> bool | None:
    """Return True if ``pid`` is confirmed to be an ``agent-voice-input daemon`` process."""
    if pid <= 0 or pid == os.getpid():
        return False
    tokens = _read_process_cmdline(pid)
    if not tokens:
        return None
    has_avi = any("agent-voice-input" in t or "agent_voice_input" in t for t in tokens)
    has_daemon = any(t == "daemon" for t in tokens)
    return bool(has_avi and has_daemon)


def _resolve_daemon_target_pid(rt_dir: Path) -> int:
    from .control_ipc import _inspect_daemon_lock_holder

    if rt_dir.is_dir():
        locked, holder_pid = _inspect_daemon_lock_holder(rt_dir)
        if locked and holder_pid is not None and holder_pid > 0:
            return holder_pid
    return _read_pid_file(rt_dir / "daemon.pid")


def _try_claim_daemon_lock(rt_dir: Path, deadline: float) -> int | None:
    while True:
        lfd = _try_open_daemon_lock_fd(rt_dir, create=True)
        if lfd is None:
            return None
        try:
            fcntl.flock(lfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lfd
        except (BlockingIOError, OSError) as exc:
            try:
                os.close(lfd)
            except OSError:
                pass
            if isinstance(exc, OSError) and exc.errno not in (
                errno.EACCES,
                errno.EAGAIN,
                errno.EWOULDBLOCK,
            ):
                return None
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.05)


def _cmd_stop(
    env: dict[str, str],
    *,
    quiet: bool = False,
    graceful_timeout_sec: float = 6.0,
    term_timeout_sec: float = 3.0,
) -> int:
    from .config import get_runtime_dir
    from .control_ipc import _inspect_daemon_lock_holder, send_control_command

    rt_dir = get_runtime_dir(env, create=False)
    pid_file = rt_dir / "daemon.pid"
    # Resolves unique lock-holder PID from daemon.lock first, falling back to daemon.pid.
    pid = _resolve_daemon_target_pid(rt_dir)

    shutdown_sent = False
    pid_mismatch_actual: int | None = None
    if pid > 0:
        try:
            resp = send_control_command(
                rt_dir, {"cmd": "shutdown", "expected_pid": pid}, timeout_sec=1.5
            )
            if isinstance(resp, Mapping) and resp.get("ok") is False:
                if str(resp.get("error") or "") == "pid_mismatch":
                    raw_actual = resp.get("pid")
                    pid_mismatch_actual = (
                        raw_actual
                        if (
                            isinstance(raw_actual, int)
                            and not isinstance(raw_actual, bool)
                            and raw_actual > 0
                        )
                        else 0
                    )
            else:
                shutdown_sent = True
        except Exception:
            pass

    if pid_mismatch_actual is not None:
        actual_note = f"pid={pid_mismatch_actual}" if pid_mismatch_actual > 0 else "unknown pid"
        print(
            f"error: daemon instance in {rt_dir} changed before shutdown ({actual_note}, initial pid={pid}); "
            "refusing to stop the new daemon instance",
            file=sys.stderr,
        )
        return 1

    lock_confirmed_free = not rt_dir.is_dir()
    sigterm_sent = False
    lock_owner_changed = False
    lock_holder_for_err: int | None = None
    lfd: int | None = None

    if rt_dir.is_dir():
        # Phase 1: wait for graceful shutdown (or probe lock immediately if IPC failed)
        graceful_dl = time.monotonic() + (max(0.0, float(graceful_timeout_sec)) if shutdown_sent else 0.0)
        lfd = _try_claim_daemon_lock(rt_dir, graceful_dl)
        if lfd is not None:
            lock_confirmed_free = True
        else:
            # Phase 2: escalate to SIGTERM only if initial target pid > 0 was known and
            # daemon.lock is still confirmed held by that exact same pid.
            locked_now, holder_pid = _inspect_daemon_lock_holder(rt_dir)
            if pid > 0 and locked_now and holder_pid == pid:
                if _probe_pid_liveness(pid) == "alive" and _is_avi_daemon_pid(pid) is True:
                    try:
                        os.kill(pid, signal.SIGTERM)
                        sigterm_sent = True
                    except ProcessLookupError:
                        pass
                    except OSError:
                        pass
                term_dl = time.monotonic() + max(0.0, float(term_timeout_sec))
                lfd = _try_claim_daemon_lock(rt_dir, term_dl)
                if lfd is not None:
                    lock_confirmed_free = True
            elif not locked_now:
                lfd = _try_claim_daemon_lock(rt_dir, time.monotonic())
                if lfd is not None:
                    lock_confirmed_free = True
            else:
                lock_owner_changed = True
                lock_holder_for_err = holder_pid

        if lfd is not None:
            try:
                cur_pid = _read_pid_file(pid_file)
                cur_liveness = _probe_pid_liveness(cur_pid)
                init_liveness = _probe_pid_liveness(pid)
                if init_liveness != "perm" and cur_liveness != "perm":
                    for stale in (pid_file, rt_dir / "control.sock"):
                        try:
                            stale.unlink(missing_ok=True)
                        except OSError:
                            pass
            finally:
                try:
                    fcntl.flock(lfd, fcntl.LOCK_UN)
                except OSError:
                    pass
                try:
                    os.close(lfd)
                except OSError:
                    pass

    pid_liveness = _probe_pid_liveness(pid)
    still_avi_alive = pid_liveness == "alive" and _is_avi_daemon_pid(pid) is True
    if not lock_confirmed_free or still_avi_alive or (not shutdown_sent and pid_liveness == "perm"):
        if lock_owner_changed:
            holder_note = (
                f"pid={lock_holder_for_err}"
                if (lock_holder_for_err is not None and lock_holder_for_err > 0)
                else "unknown pid"
            )
            if pid > 0:
                print(
                    f"error: daemon.lock in {rt_dir} changed hands ({holder_note}, initial pid={pid}); "
                    "refusing to send SIGTERM to the new daemon instance",
                    file=sys.stderr,
                )
            else:
                print(
                    f"error: daemon.lock in {rt_dir} is held by a concurrently started instance ({holder_note}) "
                    "with no initial target pid; refusing to send SIGTERM",
                    file=sys.stderr,
                )
        else:
            pid_note = f" (pid={pid})" if pid > 0 else ""
            print(
                f"error: could not confirm daemon stop in {rt_dir}{pid_note}; "
                "daemon.lock is still held or daemon process is still running",
                file=sys.stderr,
            )
        return 1
    if not quiet:
        if (shutdown_sent or sigterm_sent) and pid > 0:
            via = ", via SIGTERM" if sigterm_sent else ""
            print(_kv_line("Daemon", f"stopped (pid={pid}{via})"))
        elif shutdown_sent or sigterm_sent:
            print(_kv_line("Daemon", "stopped"))
        else:
            print(_kv_line("Daemon", "stopped (not running)"))
    return 0


def _cmd_hotwords(args: argparse.Namespace, cfg: Config, env: dict[str, str]) -> int:
    from .config import ConfigError, explain_pipeline_selection, resolve_credentials
    from .hotwords import (
        MAINSTREAM_QUOTA_PROVIDERS,
        add_hotwords,
        budget_hotwords_for_provider,
        format_budget_line,
        format_stage2_budget_line,
        load_hotwords_table,
        order_stage2_entries,
        project_stage1_hotwords,
        remove_hotwords,
        resolve_hotwords_quota_spec,
        resolve_personal_hotwords_path,
        summarize_stage2_hotwords_budget,
    )
    from .llm_common import AsrContext

    action = str(getattr(args, "hotwords_action", "") or "").strip().lower()
    as_json = bool(getattr(args, "as_json", False))
    hw_path = resolve_personal_hotwords_path(env=env)
    if hw_path is None:
        raise ConfigError(
            "cannot resolve personal hotwords path: HOME and AGENT_VOICE_INPUT_HOME are unset"
        )

    if action == "path":
        path_info = _inspect_hotwords_path_info(hw_path)
        if as_json:
            print(_json_dumps_stdout(path_info, indent=2))
            return 0
        print(path_info["path"])
        if path_info["dir_is_symlink"]:
            print(f"hotwords/ symlink -> {path_info['dir_realpath']}")
        elif path_info["realpath"] != path_info["path"]:
            print(f"realpath -> {path_info['realpath']}")
        return 0

    creds = resolve_credentials(cfg, env)
    asr_info = explain_pipeline_selection(cfg, creds, env)["asr"]
    eff_asr = str(asr_info["provider"])
    eff_asr_model = str(asr_info["model"])
    eff_asr_api = str(asr_info["api"])
    eff_asr_url = cfg.asr.effective_base_url(eff_asr)

    if action == "list":
        path_info = _inspect_hotwords_path_info(hw_path)
        table = load_hotwords_table(hw_path, env=env, use_cache=False, strict_encoding=True)
        ordered_entries = order_stage2_entries(table.entries)
        stage1_terms = table.stage1_terms
        pinned_count = sum(1 for e in ordered_entries if e.pinned)
        aliased_count = sum(1 for e in ordered_entries if e.aliases)

        ctx_general = AsrContext(
            mode="general",
            personal_hotwords=stage1_terms,
            hotwords_bucketed=True,
        )
        active_general = project_stage1_hotwords(
            ctx_general,
            eff_asr,
            model=eff_asr_model,
            api=eff_asr_api,
            base_url=eff_asr_url,
        )
        spec = resolve_hotwords_quota_spec(
            eff_asr,
            model=eff_asr_model,
            api=eff_asr_api,
            base_url=eff_asr_url,
        )
        dyn_reserve = 0 if spec.max_terms < 3 else (spec.max_terms * 3 + 9) // 10
        p_term_cap = max(0, spec.max_terms - dyn_reserve)
        p_char_cap = int(spec.max_chars * 0.70) if spec.max_chars is not None else None
        active_agent_sim = budget_hotwords_for_provider(
            stage1_terms,
            eff_asr,
            model=eff_asr_model,
            api=eff_asr_api,
            base_url=eff_asr_url,
            max_terms=p_term_cap,
            max_chars=p_char_cap,
        )
        provider_budgets = {
            prov: budget_hotwords_for_provider(stage1_terms, prov)
            for prov in MAINSTREAM_QUOTA_PROVIDERS
        }
        stage2_summary = summarize_stage2_hotwords_budget(table)
        diag_warnings = table.diagnostic_warnings()

        if as_json:
            payload: dict[str, Any] = {
                "path": path_info["path"],
                "realpath": path_info["realpath"],
                "exists": table.exists,
                "dir_is_symlink": path_info["dir_is_symlink"],
                "dir_realpath": path_info["dir_realpath"],
                "active_asr": {
                    "provider": eff_asr,
                    "model": eff_asr_model,
                    "api": eff_asr_api,
                },
                "counts": {
                    "total": len(ordered_entries),
                    "pinned": pinned_count,
                    "with_aliases": aliased_count,
                    "warnings": table.warnings_count,
                },
                "terms": [e.term for e in ordered_entries],
                "stage1_terms": list(stage1_terms),
                "entries": [
                    {"term": e.term, "aliases": list(e.aliases), "pinned": e.pinned}
                    for e in ordered_entries
                ],
                "stage1_budgets": {
                    "active_general": active_general.as_dict(),
                    "active_agent_70_30": {
                        **active_agent_sim.as_dict(),
                        "dynamic_reserve_terms": dyn_reserve,
                        "personal_term_cap": p_term_cap,
                        "personal_char_cap": p_char_cap,
                    },
                    "providers": {prov: res.as_dict() for prov, res in provider_budgets.items()},
                },
                "stage2_budget": stage2_summary.as_dict(),
                "diagnostics": {
                    "warnings_count": table.warnings_count,
                    "warnings": diag_warnings,
                    "conflict_lines": list(table.conflict_lines),
                    "legacy_sections": list(table.legacy_sections),
                    "ambiguous_aliases": [a.as_dict() for a in table.ambiguous_aliases],
                    "invalid_lines": [i.as_dict() for i in table.invalid_lines],
                    "alias_warnings": list(table.alias_warnings),
                    "file_warnings": list(table.file_warnings),
                    "size_limit_exceeded": table.size_limit_exceeded,
                    "entry_limit_exceeded": table.entry_limit_exceeded,
                    "total_valid_entries": table.total_valid_entries,
                },
            }
            print(_json_dumps_stdout(payload, indent=2))
            return 0

        status_lbl = "exists" if table.exists else "not created yet"
        print(f"Hotwords file: {path_info['path']} ({status_lbl})")
        if path_info["dir_is_symlink"]:
            print(f"Hotwords dir:  {path_info['dir']} -> {path_info['dir_realpath']}")
        elif path_info["realpath"] != path_info["path"]:
            print(f"Resolved path: {path_info['realpath']}")
        print(f"Active ASR:    {eff_asr}/{eff_asr_model} (api={eff_asr_api})")
        print(
            f"\nPersonal hotwords ({len(ordered_entries)} total, "
            f"{pinned_count} pinned, {aliased_count} with aliases):"
        )
        if ordered_entries:
            for entry in ordered_entries:
                print(f"  {entry.format_line()}")
        else:
            print("  (empty)")

        print(f"\nStage 1 ASR quotas (active route: {eff_asr}/{eff_asr_model}, api={eff_asr_api}):")
        print(f"  [general 100% personal] {format_budget_line(active_general, name=eff_asr)}")
        print(
            f"  [agent 70%/30% sim]     {format_budget_line(active_agent_sim, name=eff_asr)} "
            f"(dynamic reserve={dyn_reserve} terms; unused slots backfill personal)"
        )
        print(f"  Mainstream reference ({len(stage1_terms)} personal terms):")
        for prov in MAINSTREAM_QUOTA_PROVIDERS:
            print(f"    {format_budget_line(provider_budgets[prov], name=prov)}")

        print("\nStage 2 / Single-hop wide vocabulary budget (includes '<-' aliases):")
        print(f"  {format_stage2_budget_line(stage2_summary)}")

        if diag_warnings:
            print(f"\n[Diagnostics] ({len(diag_warnings)} warning(s)):")
            for warn_msg in diag_warnings:
                print(f"  WARN {warn_msg}")

        print()
        if not table.exists:
            print(
                "Tip: run 'agent-voice-input hotwords add <term> [--pin] [--alias a1,a2]' to create it."
            )
        if not ordered_entries:
            print(
                "Agent tip: paste into Claude Code / Codex / AGY -> "
                '"Read my recent projects & README/specs, draft a clean hotwords list for '
                f'{path_info["path"]}, show the diff first, and write after I confirm."'
            )
        else:
            print(
                "Tip: ask your coding agent to bootstrap, pin (!), add aliases (<-), or tidy your hotwords (see README -> Hotwords)."
            )
        return 0

    raw_terms = list(getattr(args, "terms", ()) or ())
    raw_aliases = list(getattr(args, "alias", ()) or ())
    pin = bool(getattr(args, "pin", False))
    if action == "add" and raw_aliases and len(raw_terms) != 1:
        raise ConfigError(
            "--alias can only be used when adding a single canonical hotword term "
            f"(got {len(raw_terms)} terms)"
        )
    try:
        if action == "add":
            edit_res = add_hotwords(hw_path, raw_terms, aliases=raw_aliases, pin=pin, env=env)
        elif action == "remove":
            edit_res = remove_hotwords(hw_path, raw_terms, env=env)
        else:  # pragma: no cover - guarded by argparse required subparser
            raise ConfigError(f"unsupported hotwords action: {action!r}")
    except ValueError as exc:
        raise ConfigError(str(exc)) from None

    table_after = load_hotwords_table(hw_path, env=env, use_cache=False)
    active_budget = budget_hotwords_for_provider(
        table_after.stage1_terms,
        eff_asr,
        model=eff_asr_model,
        api=eff_asr_api,
        base_url=eff_asr_url,
    )
    stage2_after = summarize_stage2_hotwords_budget(table_after)

    display_edit_path = _recover_utf8_surrogates(str(edit_res.path))
    if as_json:
        out_obj: dict[str, Any] = {
            "action": action,
            "path": display_edit_path,
            "changed": edit_res.changed,
            "added": list(edit_res.added),
            "updated": list(edit_res.updated),
            "pinned": list(edit_res.pinned),
            "aliases_added": list(edit_res.aliases_added),
            "skipped": list(edit_res.skipped),
            "removed": list(edit_res.removed),
            "not_found": list(edit_res.not_found),
            "warnings": list(edit_res.warnings),
            "active_asr": {
                "provider": eff_asr,
                "model": eff_asr_model,
                "api": eff_asr_api,
                "budget": active_budget.as_dict(),
            },
            "stage2_budget": stage2_after.as_dict(),
        }
        print(_json_dumps_stdout(out_obj, indent=2))
        return 0

    print(f"Hotwords file: {display_edit_path}")
    if action == "add":
        if edit_res.added:
            pin_tag = " (pinned)" if pin else ""
            if edit_res.aliases_added:
                print(
                    f"Added ({len(edit_res.added)}):   {', '.join(edit_res.added)} "
                    f"<- {', '.join(edit_res.aliases_added)}{pin_tag}"
                )
            else:
                print(f"Added ({len(edit_res.added)}):   {', '.join(edit_res.added)}{pin_tag}")
        if edit_res.updated:
            upd_parts: list[str] = []
            if edit_res.pinned:
                upd_parts.append("+pinned")
            if edit_res.aliases_added:
                upd_parts.append(f"+aliases: {', '.join(edit_res.aliases_added)}")
            upd_note = f" ({', '.join(upd_parts)})" if upd_parts else ""
            print(f"Updated ({len(edit_res.updated)}): {', '.join(edit_res.updated)}{upd_note}")
        if edit_res.skipped:
            print(f"Skipped ({len(edit_res.skipped)}): {', '.join(edit_res.skipped)} (already present)")
        for warn in edit_res.warnings:
            print(f"Warning: {warn}")
    else:
        if edit_res.removed:
            print(f"Removed ({len(edit_res.removed)}):   {', '.join(edit_res.removed)}")
        if edit_res.not_found:
            print(f"Not found ({len(edit_res.not_found)}): {', '.join(edit_res.not_found)}")
    print(f"Active ASR quota (general 100%): {format_budget_line(active_budget, name=eff_asr)}")
    print(f"Stage 2 budget: {format_stage2_budget_line(stage2_after)}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-voice-input",
        description="Context-aware voice input for CLI coding agents and desktop apps.",
    )
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    _add_common_flags(parser)
    sub = parser.add_subparsers(dest="subcmd", required=True)
    p_init = sub.add_parser(
        "init",
        help="Initialize app home (default: ~/.agent-voice-input/, override via AGENT_VOICE_INPUT_HOME) with 0600 config.toml, commented .env, and whitelist .gitignore",
    )
    p_init.add_argument(
        "--config",
        default=argparse.SUPPRESS,
        help="Target config.toml path (default: ~/.agent-voice-input/config.toml; override via AGENT_VOICE_INPUT_CONFIG / AGENT_VOICE_INPUT_HOME)",
    )
    p_init.add_argument(
        "--asr",
        "--asr-provider",
        dest="asr",
        default=argparse.SUPPRESS,
        help="Initial Stage 1 ASR provider to write into config.toml (default: auto)",
    )
    p_init.add_argument(
        "--refiner",
        "--refiner-provider",
        dest="refiner",
        default=argparse.SUPPRESS,
        help="Initial Stage 2 refiner provider to write into config.toml (default: auto)",
    )
    p_init.add_argument(
        "--hotkey",
        dest="hotkey",
        default=argparse.SUPPRESS,
        help="Initial global hotkey to write into config.toml (auto-selects RCommand on MacBook, else RControl)",
    )
    p_init.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Atomically overwrite existing config.toml (preserves existing .env and .gitignore)",
    )
    p_int = sub.add_parser(
        "integrate",
        help="Safely inspect, install, or uninstall optional Claude Code / Codex / AGY session hooks",
    )
    p_int.add_argument(
        "--status",
        action="store_true",
        help="Read-only check of detected CLIs, current hook state, and platform necessity",
    )
    p_int.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview exact changes without modifying any files",
    )
    p_int.add_argument(
        "--cli",
        action="append",
        default=None,
        help="Target CLI(s): all, or claude/codex/agy (comma-separated or repeatable; default: all)",
    )
    p_int.add_argument(
        "--uninstall",
        action="store_true",
        help="Idempotently remove only agent-voice-input managed hooks",
    )
    p_int.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit machine-readable JSON output",
    )
    for name, help_text in (
        ("daemon", "Run the voice input daemon in the foreground"),
        ("start", "Start the background daemon (idempotent if already running)"),
        ("restart", "Stop and restart the background daemon"),
        ("stop", "Stop the background daemon"),
    ):
        _add_common_flags(sub.add_parser(name, help=help_text), is_sub=True)
    p_toggle = sub.add_parser("toggle", help="Toggle recording start/stop on the running daemon")
    _add_common_flags(p_toggle, is_sub=True)
    p_toggle.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit machine-readable JSON output",
    )
    p_doc = sub.add_parser("doctor", help="Check audio bridge, native shim, credentials, and session binding")
    _add_common_flags(p_doc, is_sub=True)
    p_doc.add_argument(
        "--ping",
        action="store_true",
        help="Run lightweight 5s online connectivity & auth checks for active endpoints",
    )
    p_doc.add_argument(
        "--remote",
        action="store_true",
        dest="remote_ping",
        help="Probe SSH connectivity and python3 readiness for configured [remote] hosts",
    )
    p_doc.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit redacted diagnostic report as JSON",
    )
    p_diag = sub.add_parser(
        "diagnose",
        help="Run read-only live diagnostics on daemon health, latency, cache hits, and logs",
    )
    _add_common_flags(p_diag, is_sub=True)
    p_diag.add_argument(
        "--last",
        type=int,
        default=10,
        help="Number of recent utterances to inspect (default: 10)",
    )
    p_diag.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit machine-readable redacted JSON instead of Markdown",
    )
    p_diag.add_argument(
        "--runtime-dir",
        default="",
        help="Override runtime directory path",
    )
    p_diag.add_argument(
        "--window-title",
        default="",
        help="Optional window title hint for session matching check",
    )
    p_cancel = sub.add_parser("cancel", help="Cancel active recording or in-flight voice tasks")
    _add_common_flags(p_cancel, is_sub=True)
    p_cancel.add_argument(
        "--seq",
        dest="seq_id",
        type=int,
        default=None,
        help="Cancel only the task matching seq_id",
    )
    p_cancel.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit machine-readable JSON output",
    )
    p_status = sub.add_parser(
        "status",
        help="Show running daemon status, active pipeline, and optional recent history",
    )
    _add_common_flags(p_status, is_sub=True)
    p_status.add_argument(
        "--history",
        nargs="?",
        const=10,
        type=int,
        default=None,
        help="Include last N redacted numeric records (default: 10)",
    )
    p_status.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit machine-readable JSON output",
    )
    p_once = sub.add_parser(
        "once",
        help="Run a single recording or audio file through the transcription pipeline",
    )
    _add_common_flags(p_once, is_sub=True)
    p_once.add_argument("--file", "--wav", dest="wav_file", help="Input audio/WAV file instead of live mic")
    p_once.add_argument("--duration", type=float, default=4.0, help="Live capture seconds when --file is omitted")
    p_once.add_argument("--dry-run", action="store_true", help="Print Stage 1 & Stage 2 output without clipboard/paste")
    p_once.add_argument("--title", help="Optional window title override for session matching")
    p_retry = sub.add_parser("retry", help="List or re-run saved failed voice recordings")
    _add_common_flags(p_retry, is_sub=True)
    p_retry.add_argument("target", nargs="?", default="", help="Optional 1-based index (1=newest), filename, substring, or path")
    p_retry.add_argument("-l", "--list", action="store_true", dest="list_only", help="List saved failed recordings without running")
    p_retry.add_argument("--dry-run", action="store_true", help="Print Stage 1 & Stage 2 output without clipboard/paste")
    p_retry.add_argument(
        "--print",
        action="store_true",
        dest="print_stdout",
        help="Print recovered text to stdout even when automatically pasted",
    )
    p_retry.add_argument("--title", help="Optional window title override for session matching")
    p_hw = sub.add_parser(
        "hotwords",
        help="Manage personal hotwords & '<-' mishearing aliases (path, list, add [--pin] [--alias], remove) and inspect Stage 1/2 budgets",
        description=(
            "Manage flat personal hotwords (default: ~/.agent-voice-input/hotwords/personal.txt; "
            "override via AGENT_VOICE_INPUT_HOME) with optional "
            "'!' pinning and 'CanonicalTerm <- mishearing1, mishearing2' aliases (add --pin --alias), "
            "and inspect Stage 1/2 budgets."
        ),
    )
    _add_common_flags(p_hw, is_sub=True)
    hw_sub = p_hw.add_subparsers(dest="hotwords_action", required=True)
    p_hw_path = hw_sub.add_parser(
        "path",
        help="Print personal.txt absolute path and hotwords/ symlink realpath info",
    )
    _add_common_flags(p_hw_path, is_sub=True)
    p_hw_path.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit structured JSON output",
    )
    p_hw_list = hw_sub.add_parser(
        "list",
        help="Show personal hotwords (with '!' pins and '<-' aliases), Stage 1/2 budgets, and diagnostics",
    )
    _add_common_flags(p_hw_list, is_sub=True)
    p_hw_list.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit structured JSON output",
    )
    p_hw_add = hw_sub.add_parser(
        "add",
        help="Add or update personal hotword(s), optionally pinned (--pin) or with '<-' mishearing aliases (--alias)",
    )
    _add_common_flags(p_hw_add, is_sub=True)
    p_hw_add.add_argument(
        "terms",
        nargs="+",
        help="One or more canonical hotwords or phrases to add (pass a single term when using --alias)",
    )
    p_hw_add.add_argument(
        "--pin",
        action="store_true",
        default=False,
        help="Pin hotword(s) with '!' prefix for top Stage 1/2 priority (upgrades existing entries in place)",
    )
    p_hw_add.add_argument(
        "--alias",
        action="append",
        default=[],
        help="Comma-separated ASR mishearing alias(es) for a single canonical term (repeatable, max 5 total; e.g. --alias '哈尼斯,哈尼思')",
    )
    p_hw_add.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit structured JSON output",
    )
    p_hw_rm = hw_sub.add_parser(
        "remove",
        help="Remove one or more personal hotwords (removes all duplicate lines matching case-insensitively)",
    )
    _add_common_flags(p_hw_rm, is_sub=True)
    p_hw_rm.add_argument("terms", nargs="+", help="One or more hotwords or phrases to remove")
    p_hw_rm.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit structured JSON output",
    )
    p_kt = sub.add_parser(
        "key-test",
        help="Test global hotkey capture and display raw key/modifier events",
    )
    _add_common_flags(p_kt, is_sub=True)
    p_kt.add_argument("--seconds", type=int, default=10, help="Probe duration in seconds (Esc exits early)")
    p_hook = sub.add_parser("session-hook", help="Update terminal OSC 2 title and session registry from Agent hook stdin")
    p_hook.add_argument("--cli", choices=("auto", "claude", "agy", "gemini"), default="auto", help="Source Agent CLI type")
    p_hook.add_argument("--quiet", action="store_true", help="Suppress JSON '{}' stdout (e.g. when called from statusline)")
    p_rctx = sub.add_parser("remote-ctx", help="Execute local read-only remote session probe from stdin JSON")
    p_rctx.add_argument("--raw-json", action="store_true", help="Emit plain UTF-8 JSON instead of framed gzip")
    return parser


_ASCII_STREAM_ENCODINGS = frozenset({"ascii", "ansi-x3.4-1968", "us-ascii", "646", "c", "posix"})


def _normalize_enc_name(raw: str | None) -> str:
    return str(raw or "").strip().lower().replace("_", "-")


def _recover_utf8_surrogates(token: str) -> str:
    """Decode POSIX ``LC_ALL=C`` surrogate-escaped UTF-8 CLI text tokens back to Unicode."""
    if not any(0xDC80 <= ord(ch) <= 0xDCFF for ch in token):
        return token
    try:
        return token.encode("utf-8", errors="surrogateescape").decode("utf-8")
    except UnicodeDecodeError:
        return token


def _json_dumps_stdout(obj: Any, *, indent: int | None = None) -> str:
    """Serialize JSON for stdout without corrupting non-ASCII text under non-UTF-8 stream encodings."""
    out_enc = _normalize_enc_name(getattr(sys.stdout, "encoding", ""))
    use_ascii = bool(out_enc) and out_enc not in ("utf-8", "utf8")
    return json.dumps(obj, ensure_ascii=use_ascii, indent=indent)


def _load_env_for_redaction(
    env: Mapping[str, str] | None,
    *,
    load_env_files_fn: Any,
    config_error_cls: type[Exception],
) -> Mapping[str, str]:
    """Return an env mapping for error redaction without re-raising ConfigError/OSError."""
    if env is not None:
        return env
    try:
        return load_env_files_fn()
    except (OSError, config_error_cls):
        return dict(os.environ)


def _configure_posix_ascii_stdio(env: Mapping[str, str] | None = None) -> None:
    """Upgrade stdio to UTF-8 only under POSIX ASCII locales when ``PYTHONIOENCODING`` is unset."""
    eff_env = os.environ if env is None else env
    if (eff_env.get("PYTHONIOENCODING") or "").strip():
        return
    if _normalize_enc_name(locale.getpreferredencoding(False)) not in _ASCII_STREAM_ENCODINGS:
        return
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if _normalize_enc_name(getattr(stream, "encoding", "")) in _ASCII_STREAM_ENCODINGS and callable(reconf):
            try:
                reconf(encoding="utf-8", errors="replace")
            except (AttributeError, OSError, ValueError):
                pass


def _recover_parsed_text_args(args: argparse.Namespace) -> None:
    """Recover UTF-8 text business arguments without mutating OS filesystem path arguments."""
    if getattr(args, "subcmd", None) == "hotwords":
        if isinstance(getattr(args, "terms", None), list):
            args.terms = [_recover_utf8_surrogates(str(tok)) for tok in args.terms]
        if isinstance(getattr(args, "alias", None), list):
            args.alias = [_recover_utf8_surrogates(str(tok)) for tok in args.alias]
    for attr in ("window_title", "title"):
        val = getattr(args, attr, None)
        if isinstance(val, str) and val:
            setattr(args, attr, _recover_utf8_surrogates(val))


def _format_last_result_brief(rec: Mapping[str, Any]) -> str:
    """Render a strictly text-free, single-line numeric/status summary for a local diagnostic record."""
    seq = rec.get("seq_id")
    prefix = f"#{seq}" if seq is not None else "#-"
    ok_lbl = (
        "ok"
        if rec.get("ok")
        else (str(rec.get("skipped_reason") or rec.get("paste_reason") or "failed").strip() or "failed")
    )
    deliv = str(rec.get("delivery_status") or "").strip()
    if not deliv and rec.get("ok"):
        if rec.get("pasted"):
            deliv = "pasted"
        elif rec.get("clipboard"):
            deliv = "copied"
    parts: list[str] = [prefix, ok_lbl]
    if deliv:
        parts.append(deliv)
    tot_ms = rec.get("total_ms")
    if isinstance(tot_ms, (int, float)) and not isinstance(tot_ms, bool):
        parts.append(f"{int(round(tot_ms))}ms")
    fb = str(rec.get("refiner_fallback") or "").strip()
    if fb and fb != "single_hop_passthrough":
        parts.append(f"fallback={fb}")
    return " ".join(parts)


def _format_status_human(
    resp: Mapping[str, Any],
    *,
    cfg: Config | None = None,
    creds: ProviderCredentials | None = None,
    show_history: bool = False,
) -> list[str]:
    """Render ``status`` response as aligned human-readable rows (never emits spoken prose)."""
    from .config import format_archive_summary

    lines: list[str] = []
    if resp.get("ok"):
        pid = resp.get("pid") or "-"
        state = str(resp.get("state") or "idle")
        ver = str(resp.get("version") or "").strip()
        pending = int(resp.get("pending_count") or 0)
        bits = [f"pid={pid}", f"state={state}"]
        if ver:
            bits.append(f"v{ver}")
        if pending > 0:
            bits.append(f"pending={pending}")
        lines.append(_kv_line("Daemon", f"running ({', '.join(bits)})"))

        pipe = resp.get("pipeline")
        if isinstance(pipe, Mapping) and pipe:
            lines.extend(_format_pipeline_rows(pipe, cfg=cfg, creds=creds, include_effort=True))

        live_hk = resp.get("hotkey")
        if isinstance(live_hk, Mapping) and live_hk:
            hk = (
                str(live_hk.get("key") or "").strip()
                or ((cfg.hotkey.key or "").strip() if cfg is not None else "")
                or "RControl"
            )
            mode = (
                str(live_hk.get("trigger_mode") or "").strip().lower()
                or ((cfg.hotkey.trigger_mode or "").strip().lower() if cfg is not None else "")
                or "tap_toggle"
            )
            scope = (
                str(live_hk.get("scope") or "").strip().lower()
                or ((cfg.hotkey.scope or "").strip().lower() if cfg is not None else "")
                or "all"
            )
            lines.append(_kv_line("Hotkey", f"{hk} ({mode}, scope={scope})"))
        elif isinstance(live_hk, str) and live_hk.strip():
            hk = live_hk.strip()
            mode = ((cfg.hotkey.trigger_mode or "").strip().lower() if cfg is not None else "") or "tap_toggle"
            scope = ((cfg.hotkey.scope or "").strip().lower() if cfg is not None else "") or "all"
            lines.append(_kv_line("Hotkey", f"{hk} ({mode}, scope={scope})"))
        elif cfg is not None:
            hk = (cfg.hotkey.key or "RControl").strip() or "RControl"
            mode = (cfg.hotkey.trigger_mode or "tap_toggle").strip().lower()
            scope = (cfg.hotkey.scope or "all").strip().lower()
            lines.append(_kv_line("Hotkey", f"{hk} ({mode}, scope={scope})"))

        shim_ok = resp.get("shim_ok")
        if shim_ok is True:
            lines.append(_kv_line("Bridge", "OK"))
        elif shim_ok is False:
            se = str(resp.get("shim_error") or "unavailable").strip()
            lines.append(_kv_line("Bridge", f"degraded ({se})"))

        hw = resp.get("hotwords")
        if isinstance(hw, Mapping) and hw:
            total = hw.get("total", 0)
            kept = hw.get("kept", 0)
            dropped = hw.get("dropped_count", 0)
            warns = hw.get("warnings_count", 0)
            max_items = hw.get("max_items")
            hw_bits = [f"{kept}/{total} active"]
            if max_items is not None:
                hw_bits[0] += f" (cap {max_items})"
            if dropped:
                hw_bits.append(f"dropped={dropped}")
            if warns:
                hw_bits.append(f"warnings={warns}")
            lines.append(_kv_line("Hotwords", ", ".join(hw_bits)))

        arch = resp.get("archive")
        if isinstance(arch, Mapping):
            lines.append(_kv_line("Archive", format_archive_summary(arch)))
        elif cfg is not None:
            lines.append(_kv_line("Archive", format_archive_summary(cfg.archive)))

        last_res = resp.get("last_result")
        if isinstance(last_res, Mapping) and last_res:
            lines.append(_kv_line("Last", _format_last_result_brief(last_res)))

        if show_history and isinstance(recs := resp.get("recent_results"), list):
            if not recs:
                lines.append(_kv_line("History", "0 recent records"))
            else:
                shown = recs[-5:]
                lines.append(
                    _kv_line("History", f"{len(recs)} record(s) (latest {len(shown)}):")
                )
                pad = " " * _KV_LABEL_WIDTH
                for rec in shown:
                    if isinstance(rec, Mapping):
                        lines.append(f"{pad}{_format_last_result_brief(rec)}")

        if resp.get("code_drift"):
            warn_msg = str(resp.get("drift_warning") or "code or version drifted").strip()
            lines.append(_kv_line("Notice", warn_msg))
            lines.append(_kv_line("Fix:", "run 'agent-voice-input restart'"))
        return lines

    state = str(resp.get("state") or "stopped").strip() or "stopped"
    pid = resp.get("pid")
    err = str(resp.get("error") or "").strip()
    if state == "stopped":
        lines.append(_kv_line("Daemon", "not running (stopped)"))
        lines.append(_kv_line("Fix:", "run 'agent-voice-input start'"))
    elif state == "unresponsive":
        lines.append(_kv_line("Daemon", f"unresponsive (pid={pid or '-'})"))
        if err:
            lines.append(_kv_line("Detail", err))
        lines.append(_kv_line("Fix:", "run 'agent-voice-input diagnose' or 'agent-voice-input restart'"))
    else:
        lines.append(_kv_line("Daemon", f"error ({err or 'control socket error'})"))
        lines.append(_kv_line("Fix:", "run 'agent-voice-input doctor' or 'agent-voice-input restart'"))

    arch_off = resp.get("archive")
    if isinstance(arch_off, Mapping):
        lines.append(_kv_line("Archive", format_archive_summary(arch_off)))
    elif cfg is not None:
        lines.append(_kv_line("Archive", format_archive_summary(cfg.archive)))

    if show_history and isinstance(recs := resp.get("recent_results"), list):
        if not recs:
            lines.append(_kv_line("History", "0 recent records"))
        else:
            shown = recs[-5:]
            lines.append(_kv_line("History", f"{len(recs)} record(s) (latest {len(shown)}):"))
            pad = " " * _KV_LABEL_WIDTH
            for rec in shown:
                if isinstance(rec, Mapping):
                    lines.append(f"{pad}{_format_last_result_brief(rec)}")
    return lines


def _format_control_action_human(cmd: str, resp: Mapping[str, Any]) -> str:
    """Render ``toggle`` or ``cancel`` control response as a concise single-line summary."""
    state = str(resp.get("state") or "").strip()
    if cmd == "toggle":
        if resp.get("ok"):
            seq = resp.get("seq_id")
            st_lbl = state or "ok"
            return _kv_line("Daemon", f"{st_lbl} (seq={seq})" if seq is not None else st_lbl)
        ignored = str(resp.get("ignored") or "").strip()
        if ignored == "queue_full" and not resp.get("error"):
            return _kv_line("Daemon", "ignored (queue_full; wait for pending utterances to finish)")
        if ignored and not resp.get("error"):
            return _kv_line("Daemon", f"ignored ({ignored})")
        err = str(resp.get("error") or resp.get("skipped_reason") or "failed").strip()
        return _kv_line("Daemon", f"toggle failed ({err})")

    if resp.get("ok"):
        ids = resp.get("cancelled_ids")
        if isinstance(ids, list) and ids:
            id_csv = ",".join(str(x) for x in ids)
            st_sfx = f", state={state}" if state else ""
            return _kv_line("Daemon", f"cancelled (seq={id_csv}{st_sfx})")
        reason = str(resp.get("reason") or "nothing active").strip()
        st_sfx = f", state={state}" if state else ""
        return _kv_line("Daemon", f"not cancelled ({reason}{st_sfx})")
    ignored = str(resp.get("ignored") or "").strip()
    if ignored and not resp.get("error"):
        return _kv_line("Daemon", f"cancel ignored ({ignored})")
    err = str(resp.get("error") or resp.get("skipped_reason") or "failed").strip()
    return _kv_line("Daemon", f"cancel failed ({err})")


def main(argv: list[str] | None = None) -> int:
    _configure_posix_ascii_stdio()
    args = _build_parser().parse_args(argv)
    _recover_parsed_text_args(args)
    if args.subcmd == "session-hook":
        from .session_title import run_session_hook

        return run_session_hook(cli_mode=args.cli, quiet_stdout=args.quiet)
    if args.subcmd == "remote-ctx":
        from ._remote_probe import pack_probe_stdout, run_probe

        raw_in = sys.stdin.buffer.read(131072)
        req_obj = json.loads(raw_in.decode("utf-8")) if raw_in.strip() else {}
        resp_obj = run_probe(req_obj if isinstance(req_obj, Mapping) else {})
        if args.raw_json or (isinstance(req_obj, Mapping) and req_obj.get("raw_json")):
            sys.stdout.buffer.write(
                json.dumps(resp_obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
            )
        else:
            sys.stdout.buffer.write(pack_probe_stdout(resp_obj))
        sys.stdout.buffer.flush()
        return 0

    from .config import ConfigError, get_runtime_dir, load_config, load_env_files, redact_secrets
    from .control_ipc import DaemonAlreadyRunningError

    if args.subcmd == "integrate":
        return _cmd_integrate(
            status=bool(getattr(args, "status", False)),
            dry_run=bool(getattr(args, "dry_run", False)),
            cli_targets=getattr(args, "cli", None),
            uninstall=bool(getattr(args, "uninstall", False)),
            as_json=bool(getattr(args, "as_json", False)),
            env=dict(os.environ),
        )
    if args.subcmd == "init":
        try:
            return _cmd_init(
                config_path=getattr(args, "config", None),
                force=bool(args.force),
                asr_provider=getattr(args, "asr", None),
                refiner_provider=getattr(args, "refiner", None),
                hotkey=getattr(args, "hotkey", None),
            )
        except ConfigError as exc:
            eff_env = _load_env_for_redaction(None, load_env_files_fn=load_env_files, config_error_cls=ConfigError)
            print(f"config error: {redact_secrets(str(exc), eff_env)}", file=sys.stderr)
            return 2
        except Exception as exc:
            eff_env = _load_env_for_redaction(None, load_env_files_fn=load_env_files, config_error_cls=ConfigError)
            print(f"error: {redact_secrets(str(exc), eff_env)}", file=sys.stderr)
            return 1
    ov = {
        k: getattr(args, a)
        for a, k in (
            ("hotkey", "hotkey.key"),
            ("asr", "asr.provider"),
            ("asr_model", "asr.model"),
            ("asr_api", "asr.api"),
            ("asr_effort", "asr.effort"),
            ("refiner", "refiner.provider"),
            ("refiner_model", "refiner.model"),
            ("refiner_api", "refiner.api"),
            ("effort", "refiner.effort"),
        )
        if getattr(args, a, None)
    }
    cfg: Config | None = None
    env: dict[str, str] | None = None
    try:
        if args.subcmd == "stop":
            try:
                env = load_env_files(None)
            except (OSError, ConfigError):
                env = dict(os.environ)
            return _cmd_stop(env)
        if args.subcmd == "status":
            from .config import format_archive_status_dict, resolve_credentials
            from .control_ipc import (
                describe_daemon_drift,
                probe_daemon_status,
                read_diag_history,
            )

            try:
                env = load_env_files(None, config_path=getattr(args, "config", None))
            except (OSError, ConfigError):
                env = dict(os.environ)
            rt_dir_st = get_runtime_dir(env, create=False)
            cfg_warn = ""
            try:
                cfg, env = load_config(
                    config_path=getattr(args, "config", None),
                    cli_overrides=ov,
                    is_macbook=_is_macbook_laptop(),
                )
            except ConfigError as exc:
                cfg = None
                cfg_warn = str(exc)

            as_json = bool(getattr(args, "as_json", False))
            raw_hist = getattr(args, "history", None)
            hist_limit = max(0, min(500, int(raw_hist))) if raw_hist is not None else None
            resp = probe_daemon_status(rt_dir_st, timeout_sec=8.0, history=hist_limit)
            if resp.get("ok"):
                if "archive" not in resp and cfg is not None:
                    resp["archive"] = format_archive_status_dict(cfg.archive)
                drift = describe_daemon_drift(resp)
                resp["code_drift"] = bool(drift["drifted"])
                if drift["drifted"]:
                    resp["current_version"] = drift["current_version"]
                    resp["current_build_id"] = drift["current_build_id"]
                    resp["drift_warning"] = f"{drift['reason']}; run 'agent-voice-input restart'"
                if as_json:
                    print(_json_dumps_stdout(resp, indent=2))
                else:
                    creds_st = resolve_credentials(cfg, env) if cfg is not None else None
                    for line in _format_status_human(
                        resp, cfg=cfg, creds=creds_st, show_history=(hist_limit is not None)
                    ):
                        print(line)
                    if cfg_warn:
                        print(_kv_line("Config", f"WARN ({redact_secrets(cfg_warn, env)})"))
                return 0
            if resp.get("error"):
                resp["error"] = redact_secrets(str(resp["error"]), env, cfg=cfg)
            if "archive" not in resp and cfg is not None:
                resp["archive"] = format_archive_status_dict(cfg.archive)
            if hist_limit is not None:
                disk_hist = read_diag_history(rt_dir_st, limit=hist_limit)
                resp["last_result"] = dict(disk_hist[-1]) if disk_hist else {}
                resp["recent_results"] = disk_hist
            if as_json:
                print(_json_dumps_stdout(resp, indent=2))
            else:
                for line in _format_status_human(
                    resp, cfg=cfg, creds=None, show_history=(hist_limit is not None)
                ):
                    print(line)
                if cfg_warn:
                    print(_kv_line("Config", f"WARN ({redact_secrets(cfg_warn, env)})"))
            return 1

        cfg, env = load_config(
            config_path=getattr(args, "config", None),
            cli_overrides=ov,
            is_macbook=_is_macbook_laptop(),
        )
        if args.subcmd == "daemon":
            from .daemon import VoiceInputDaemon

            VoiceInputDaemon(cfg, env=_sanitize_daemon_tmux_env(env)).serve_forever()
            return 0
        if args.subcmd == "start":
            return _cmd_start(args, env, cfg=cfg)
        if args.subcmd == "restart":
            rt_dir_rst = get_runtime_dir(env, create=False)
            prev_pid: int | None = None
            if rt_dir_rst.is_dir():
                from .control_ipc import _inspect_daemon_lock_holder

                locked, holder_pid = _inspect_daemon_lock_holder(rt_dir_rst)
                if locked and holder_pid is not None and holder_pid > 0:
                    prev_pid = holder_pid
                else:
                    cand_pid = _read_pid_file(rt_dir_rst / "daemon.pid")
                    if cand_pid > 0 and _probe_pid_liveness(cand_pid) == "alive":
                        prev_pid = cand_pid
            rc_stop = _cmd_stop(env, quiet=True)
            if rc_stop != 0:
                return rc_stop
            if not _wait_daemon_lock_free(rt_dir_rst, timeout_sec=2.0):
                print(
                    "error: previous daemon still holds daemon.lock; run 'agent-voice-input stop' and retry",
                    file=sys.stderr,
                )
                return 1
            return _cmd_start(args, env, cfg=cfg, restarted_from_pid=prev_pid)
        if args.subcmd in ("toggle", "cancel"):
            from .control_ipc import send_control_command

            as_json = bool(getattr(args, "as_json", False))
            ctrl_timeout = 20.0 if args.subcmd == "toggle" else 8.0
            rt_dir_st = get_runtime_dir(env, create=False)
            ctrl_req: dict[str, Any] = {"cmd": args.subcmd}
            if args.subcmd == "cancel" and getattr(args, "seq_id", None) is not None:
                ctrl_req["seq_id"] = int(args.seq_id)
            try:
                resp = send_control_command(
                    rt_dir_st,
                    ctrl_req,
                    timeout_sec=ctrl_timeout,
                )
                if as_json:
                    print(_json_dumps_stdout(resp, indent=2))
                else:
                    print(_format_control_action_human(args.subcmd, resp))
                return 0 if resp.get("ok") else 1
            except Exception as exc:
                msg = (
                    "agent-voice-input daemon control request timed out"
                    if isinstance(exc, (TimeoutError, socket.timeout))
                    else "agent-voice-input daemon is not running (run 'agent-voice-input start' first)"
                )
                print(msg, file=sys.stderr)
                return 1
        if args.subcmd == "once":
            return _cmd_once(args, cfg, env)
        if args.subcmd == "retry":
            return _cmd_retry(args, cfg, env)
        if args.subcmd == "hotwords":
            return _cmd_hotwords(args, cfg, env)
        if args.subcmd == "doctor":
            return _cmd_doctor(
                cfg,
                env,
                ping=bool(getattr(args, "ping", False)),
                remote_ping=bool(getattr(args, "remote_ping", False)),
                as_json=bool(getattr(args, "as_json", False)),
            )
        if args.subcmd == "diagnose":
            from .diagnose import run_diagnose_command

            return run_diagnose_command(
                cfg,
                env,
                runtime_dir=str(getattr(args, "runtime_dir", "") or ""),
                window_title=str(getattr(args, "window_title", "") or ""),
                last=int(getattr(args, "last", 10)),
                as_json=bool(getattr(args, "as_json", False)),
            )
        if args.subcmd == "key-test":
            if sys.platform == "darwin":
                from .platform_macos import run_macos_key_test

                return run_macos_key_test(cfg, seconds=args.seconds, env=env)
            from .platform_wsl import run_key_test

            return run_key_test(cfg, seconds=args.seconds)
    except DaemonAlreadyRunningError as exc:
        pid_tag = f" (pid={exc.pid})" if exc.pid else ""
        print(
            f"agent-voice-input daemon is already running{pid_tag}; "
            "run 'agent-voice-input restart' or 'agent-voice-input stop'",
            file=sys.stderr,
        )
        return 1
    except ConfigError as exc:
        eff_env = _load_env_for_redaction(env, load_env_files_fn=load_env_files, config_error_cls=ConfigError)
        print(f"config error: {redact_secrets(str(exc), eff_env, cfg=cfg)}", file=sys.stderr)
        return 2
    except Exception as exc:
        eff_env = _load_env_for_redaction(env, load_env_files_fn=load_env_files, config_error_cls=ConfigError)
        print(f"error: {redact_secrets(str(exc), eff_env, cfg=cfg)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
