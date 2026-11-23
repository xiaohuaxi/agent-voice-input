"""CLI entrypoint for agent-voice-input (init, daemon, start, restart, stop, status, toggle, cancel, once, retry, hotwords, doctor, diagnose, key-test, session-hook)."""
from __future__ import annotations

import argparse, dataclasses, errno, fcntl, json, locale, os, shlex, signal, socket, stat, subprocess, sys, time
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import __version__

if TYPE_CHECKING:
    from .config import Config, ProviderCredentials
    from .http_transport import HttpTransport

_INIT_CONFIG_TOML = """\
# agent-voice-input configuration (chmod 600)
# Priority: CLI flags > config.toml > built-in defaults

[hotkey]
# Default Right Ctrl ("RControl"); also supports "ROption"/"RAlt", "Fn" (macOS), "F13".."F20", etc.
# Note: MacBook built-in keyboards lack a Right Ctrl key — macOS laptop users can switch to "ROption" or "Fn".
key = "RControl"
# "tap_toggle" (tap to start/finish) | "ptt" (hold to speak, release to transcribe)
trigger_mode = "tap_toggle"

[asr]
# "auto" selects the first configured API key: dashscope -> doubao -> openrouter -> gemini -> openai -> groq -> siliconflow
provider = "auto"

[refiner]
# "auto" prefers local `codex` login, then configured API keys (deepseek -> dashscope -> gemini -> openrouter -> ...), or falls back to "off"
provider = "auto"
"""

_INIT_ENV_TEMPLATE = """\
# agent-voice-input API credentials (chmod 600)
# For cloud ASR, uncomment and fill in AT LEAST ONE Stage 1 speech recognition key below
# (local loopback openai-compatible ASR endpoints need no API key):

# === Stage 1: Speech Recognition (REQUIRED for cloud ASR — uncomment at least one; local loopback openai-compatible ASR needs no key) ===
# (DashScope, OpenAI, Groq, OpenRouter, Gemini, and SiliconFlow can also handle Stage 2 refinement)

# Alibaba Cloud Bailian / DashScope (Stage 1 qwen3-asr-flash + Stage 2 qwen3.8-flash, or single-hop qwen3.8-omni-flash)
# DASHSCOPE_API_KEY="sk-..."

# OpenAI (Stage 1 gpt-4o-mini-transcribe + Stage 2 gpt-4o-mini)
# OPENAI_API_KEY="sk-..."

# Groq Cloud (fast Stage 1 whisper-large-v3-turbo + Stage 2 llama-3.3-70b-versatile)
# GROQ_API_KEY="gsk_..."

# OpenRouter (single key for Stage 1 ASR + Stage 2 Refiner)
# OPENROUTER_API_KEY="sk-or-..."

# Google Gemini (single-hop Stage 1 audio + context refinement, or Stage 2 Refiner)
# GEMINI_API_KEY="AIza..."

# Volcengine Doubao Speech (Stage 1 ASR: single-header X-Api-Key, no APP_ID needed)
# DOUBAO_SPEECH_API_KEY="..."
# Legacy dual-header app credentials (optional; ignored when DOUBAO_SPEECH_API_KEY is set):
# DOUBAO_ASR_APP_ID="..."
# DOUBAO_ASR_API_KEY="..."

# SiliconFlow (Stage 1 SenseVoiceSmall + Stage 2 Refiner)
# SILICONFLOW_API_KEY="sk-..."

# === Stage 2 Only: Optional Refiner (pair with a Stage 1 key above) ===
# Note: Setting ONLY a Stage 2 key below cannot transcribe speech on its own.
# Local Codex CLI login (`codex login`, auto-detected) or Antigravity CLI (`refiner.provider = "agy"`)
# can also reuse your existing CLI login for Stage 2 refinement when paired with Stage 1 ASR
# (billing depends on your CLI auth mode, e.g., subscription login vs. API key).

# DeepSeek official API (fast Stage 2 refiner: deepseek-flash)
# DEEPSEEK_API_KEY="sk-..."

# Volcengine Ark (Stage 2 Refiner: doubao-seed-2-0-mini-260428)
# ARK_API_KEY="..."
"""

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
        "--asr",
        "--asr-provider",
        dest="asr",
        help="Override Stage 1 ASR provider (auto|dashscope|doubao|openrouter|gemini|openai|groq|siliconflow|openai-compatible)",
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
        help="Override Stage 1 reasoning/thinking effort (auto|default|none|minimal|low|medium|high|xhigh|max)",
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
        help="Override Stage 2 reasoning/thinking effort (auto|default|none|minimal|low|medium|high|xhigh|max)",
        **kw,
    )


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


def _cmd_init(
    config_path: str | Path | None = None,
    *,
    force: bool = False,
    env: Mapping[str, str] | None = None,
    home_dir: str | Path | None = None,
) -> int:
    from .config import (
        ConfigError,
        default_config_file_candidates,
        resolve_target_config_paths,
    )

    src_env = os.environ if env is None else env
    cli_cfg_str = str(config_path).strip() if config_path is not None else ""
    has_explicit_cfg = bool(
        cli_cfg_str
        or src_env.get("AGENT_VOICE_INPUT_CONFIG", "").strip()
        or src_env.get("VOICE_INPUT_CONFIG", "").strip()
    )
    cfg_dir, toml_path, env_path = resolve_target_config_paths(
        config_path=config_path, env=src_env, home_dir=home_dir
    )
    if toml_path.suffix.lower() == ".json":
        raise ConfigError(
            f"init generates TOML config; target path cannot end with .json: {toml_path}"
        )

    toml_st = _lstat_or_none(toml_path)
    env_st = _lstat_or_none(env_path)
    for p, st in ((toml_path, toml_st), (env_path, env_st)):
        if st is not None and stat.S_ISDIR(st.st_mode):
            raise ConfigError(f"cannot overwrite directory: {p}")

    shadowed_cfg: Path | None = None
    if not has_explicit_cfg and not force and toml_st is None:
        for cand in default_config_file_candidates(src_env, home_dir=home_dir):
            if cand != toml_path and cand.is_file():
                shadowed_cfg = cand
                break

    will_write_toml = (force or toml_st is None) and shadowed_cfg is None
    will_write_env = force or env_st is None
    if will_write_toml or will_write_env:
        _ensure_init_parent_dir(cfg_dir, preserve_existing_mode=has_explicit_cfg)

    if shadowed_cfg is not None:
        print(
            f"[init] kept existing {shadowed_cfg} "
            f"(pass --force to create {toml_path} which takes precedence)"
        )
    else:
        state_toml = _write_init_file_0600(cfg_dir, toml_path, _INIT_CONFIG_TOML, force=force)
        if state_toml == "kept":
            print(f"[init] kept existing {toml_path} (pass --force to overwrite)")
        else:
            print(f"[init] {state_toml} {toml_path} (0600)")

    state_env = _write_init_file_0600(cfg_dir, env_path, _INIT_ENV_TEMPLATE, force=force)
    if state_env == "kept":
        print(f"[init] kept existing {env_path} (pass --force to overwrite)")
    else:
        print(f"[init] {state_env} {env_path} (0600)")

    cfg_flag = f" --config {shlex.quote(cli_cfg_str)}" if cli_cfg_str else ""
    print(
        "\nNext steps:\n"
        f"  1. For cloud ASR, add at least one Stage 1 speech recognition API key in {env_path}\n"
        "     (e.g. DASHSCOPE_API_KEY / OPENAI_API_KEY / GROQ_API_KEY / OPENROUTER_API_KEY / GEMINI_API_KEY;\n"
        "      local loopback openai-compatible ASR endpoints need no key)\n"
        f"  2. Verify setup:  agent-voice-input doctor{cfg_flag} --ping\n"
        f"  3. Start daemon:  agent-voice-input start{cfg_flag}"
    )
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
    )

    prov = provider.strip().lower()
    b = base_url.strip().rstrip("/")
    for sfx in ("/chat/completions", "/audio/transcriptions", "/responses"):
        if b.endswith(sfx):
            b = b[: -len(sfx)]
            break
    if prov == "gemini" and is_gemini_native_url(b):
        root = b or GEMINI_DEFAULT_BASE_URL.rstrip("/")
        return f"{root}/models?pageSize=1", {"x-goog-api-key": key}
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


def _probe_stage_ping(
    *,
    stage: str,
    provider: str,
    cfg: Config,
    creds: ProviderCredentials,
    env: dict[str, str],
    base_url: str,
    transport: HttpTransport | None,
    cache: dict[tuple[str, str, str], tuple[str, int, str]],
) -> tuple[str, int, str]:
    from .config import CODEX_SUBSCRIPTION_BASE_URL, OPENAI_DEFAULT_BASE_URL, redact_secrets
    from .http_transport import (
        HttpRequest,
        _close_quietly,
        _validate_request_url,
        urllib_http_transport,
    )
    from .refiner import CodexRefiner, _build_responses_url

    prov = provider.strip().lower()
    eff_tx = transport or urllib_http_transport
    if prov == "doubao":
        from .asr_providers import DoubaoAsrProvider

        db_prov = DoubaoAsrProvider(cfg, creds, env=env, transport=eff_tx)
        return db_prov.probe_ping(timeout_sec=5.0)
    if prov == "codex":
        t0 = time.monotonic()
        deadline = t0 + 5.0
        ping_cfg = dataclasses.replace(
            cfg,
            refiner=dataclasses.replace(cfg.refiner, timeout_sec=min(float(cfg.refiner.timeout_sec), 5.0)),
        )
        ref = CodexRefiner(ping_cfg, creds, env=env, transport=transport)
        try:
            c_item = ref._resolve_codex_credentials(force_refresh=False)
            extra = tuple(s for s in (*creds.all_secret_values(), c_item.token, c_item.account_id) if s)
            rem_sec = deadline - time.monotonic()
            if rem_sec <= 0.05:
                ms = max(0, int(round((time.monotonic() - t0) * 1000)))
                return "FAIL", ms, f"FAIL ({ms}ms: Codex ping exceeded 5.0s deadline)"
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
                    )
                )
                ms = max(0, int(round((time.monotonic() - t0) * 1000)))
                if resp.status == 200:
                    return "OK", ms, f"OK ({ms}ms)"
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
            safe = redact_secrets(str(exc), env, cfg=cfg, extra_secrets=ref._extra_redaction_secrets(None))[:160]
            return "FAIL", ms, f"FAIL ({ms}ms: {safe})"
        finally:
            ref.close()
    if prov == "agy":
        from .refiner import AgyRefiner

        ping_cfg = dataclasses.replace(
            cfg,
            refiner=dataclasses.replace(cfg.refiner, timeout_sec=min(float(cfg.refiner.timeout_sec), 5.0)),
        )
        ref_agy = AgyRefiner(ping_cfg, creds, env=env)
        try:
            return ref_agy.probe_ping(timeout_sec=5.0)
        finally:
            ref_agy.close()

    custom_item = creds.asr_custom if stage == "asr" else creds.refiner_custom
    explicit_env_name = (cfg.asr.api_key_env if stage == "asr" else cfg.refiner.api_key_env).strip()
    if explicit_env_name:
        key_val = custom_item.value if custom_item is not None else ""
    else:
        v_item = creds.for_provider(prov)
        key_val = v_item.value if v_item is not None else ""

    url, hdrs = _build_ping_http_spec(prov, base_url, key_val)
    cache_key = ("http", url, key_val)
    if cache_key in cache:
        prev_st, prev_ms, prev_msg = cache[cache_key]
        if prev_st == "OK":
            return "OK", prev_ms, f"OK ({prev_ms}ms, shared endpoint)"
        return prev_st, prev_ms, prev_msg

    extra_sec = tuple(s for s in (*creds.all_secret_values(), key_val) if s)
    t0 = time.monotonic()
    try:
        resp = eff_tx(
            HttpRequest(
                url=url,
                method="GET",
                headers=hdrs,
                body=b"",
                timeout_sec=5.0,
                connect_timeout_sec=5.0,
            )
        )
        ms = max(0, int(round((time.monotonic() - t0) * 1000)))
        if resp.status == 200:
            out = ("OK", ms, f"OK ({ms}ms)")
        elif resp.status in (401, 403):
            out = (
                "FAIL",
                ms,
                f"FAIL (HTTP {resp.status} unauthorized, {ms}ms; check API key in agent-voice-input.env)",
            )
        else:
            excerpt = redact_secrets(resp.text().strip(), env, cfg=cfg, extra_secrets=extra_sec)[:140]
            out = ("FAIL", ms, f"FAIL (HTTP {resp.status}, {ms}ms: {excerpt or 'unexpected status'})")
    except Exception as exc:
        ms = max(0, int(round((time.monotonic() - t0) * 1000)))
        safe = redact_secrets(str(exc), env, cfg=cfg, extra_secrets=extra_sec)[:160]
        out = ("FAIL", ms, f"FAIL ({ms}ms: {safe})")
    cache[cache_key] = out
    return out


def _command_matches_voice_hook(val: Any) -> bool:
    return isinstance(val, str) and ("session-hook" in val or "voice-title.sh" in val)


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

    try:
        parsed = parse_toml(raw_text)
    except ConfigError:
        return False
    notify = parsed.get("notify")
    if isinstance(notify, str):
        return _command_matches_voice_hook(notify)
    if isinstance(notify, list):
        return any(_command_matches_voice_hook(tok) for tok in notify)
    return False


def _inspect_hook_summary(env: Mapping[str, str], rt_dir: Path) -> str:
    from .config import expand_user_path

    configured: list[str] = []
    for label, rels in (
        ("claude", ("~/.claude/settings.json",)),
        ("gemini/agy", ("~/.gemini/hooks.json", "~/.gemini/settings.json")),
        ("codex", ("~/.codex/config.toml",)),
    ):
        for rel in rels:
            p = expand_user_path(rel, env=env)
            try:
                if p.is_file() and not p.is_symlink() and p.stat().st_size <= 524_288:
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
    active_reg = 0
    sdir = rt_dir / "sessions"
    try:
        if sdir.is_dir() and not sdir.is_symlink():
            active_reg = sum(
                1
                for e in sdir.iterdir()
                if e.name.endswith(".json") and not e.name.startswith(".")
            )
    except OSError:
        active_reg = 0
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
    daemon_build_id_note: str = ""
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


def _doctor_check_env_and_ffmpeg(st: _DoctorState) -> None:
    from .audio_proc import resolve_ffmpeg_bin
    from .config import default_env_file_candidates

    st.emit(f"[python]    OK ({sys.version.split()[0]}, runtime={st.rt_dir})")
    st.emit(f"[config]    {st.cfg.config_path or 'defaults (no config.toml)'}")
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
        ff_bin = resolve_ffmpeg_bin()
        st.ffmpeg_ok = True
        st.emit(f"[ffmpeg]    OK ({ff_bin})")
    except Exception as exc:
        st.emit(f"[ffmpeg]    FAIL ({exc})")
        st.emit_fix("brew install ffmpeg" if sys.platform == "darwin" else "sudo apt install ffmpeg")
        st.ok_all = False


def _doctor_check_macos_host(st: _DoctorState) -> None:
    from .config import ConfigError, validate_hotkey_for_platform
    from .platform_macos import probe_macos_host

    st.platform_kind = "darwin"
    try:
        validate_hotkey_for_platform(st.cfg.hotkey.key, "darwin")
    except ConfigError as exc:
        st.ok_all = False
        st.emit(f"[hotkey]    FAIL {exc}")
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
    if avf_fail:
        st.ok_all = False
        st.emit_fix("connect/enable an input microphone in System Settings -> Sound -> Input")
    if m.get("ax_trusted") is False:
        st.emit(
            '[macos-ax]  WARN Accessibility not granted (Ghostty/tmux direct delivery works; '
            'for Cmd+V in other apps: open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")'
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
    w = probe_wsl_host(st.cfg, st.env)
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
        # Both default auto/wasapi and legacy winmm use Windows WASAPI capture inside win_shim.ps1;
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
    asr_creds_tail = f", asr_custom={_cred_label(creds.asr_custom)}" if cfg.asr.api_key_env.strip() else ""
    doubao_single_key = (
        creds.doubao_key is not None
        and creds.doubao_key.source_env.strip().upper() == "DOUBAO_SPEECH_API_KEY"
    )
    if doubao_single_key:
        doubao_label = f"doubao={_cred_label(creds.doubao_key)}"
        if creds.doubao_app_id is not None:
            doubao_label += f" (legacy app_id via {creds.doubao_app_id.source_env} ignored)"
    elif creds.doubao_app_id is not None:
        doubao_label = (
            f"doubao={_cred_label(creds.doubao_key)} (app_id={_cred_label(creds.doubao_app_id)})"
        )
    else:
        doubao_label = f"doubao={_cred_label(creds.doubao_key)}"
    st.emit(
        f"[asr]       selected={st.eff_asr} (model={st.eff_asr_model}, api={st.eff_asr_api}, effort={cfg.asr.effort}) -> "
        f"{'OK' if st.asr_ready else 'MISSING KEY'} | "
        f"dashscope={_cred_label(creds.dashscope)}, "
        f"deepseek={_cred_label(creds.deepseek)}, "
        f"{doubao_label}, "
        f"volcengine={_cred_label(creds.volcengine)}, "
        f"openrouter={_cred_label(creds.openrouter)}, "
        f"gemini={_cred_label(creds.gemini)}, "
        f"openai={_cred_label(creds.openai)}, "
        f"groq={_cred_label(creds.groq)}, "
        f"siliconflow={_cred_label(creds.siliconflow)}{asr_creds_tail}"
    )
    if not st.asr_ready:
        st.ok_all = False
        st.emit_fix(
            "run 'agent-voice-input init' then set DASHSCOPE_API_KEY (or DOUBAO_SPEECH_API_KEY / OPENROUTER_API_KEY / GEMINI_API_KEY) "
            "in ~/.config/agent-voice-input/agent-voice-input.env"
        )


def _doctor_check_daemon_and_session(st: _DoctorState) -> None:
    from .daemon import describe_daemon_drift, probe_daemon_status
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
        st.daemon_build_id_note = drift.get("note", "")
        ver_tag = f", version={st.daemon_version}" if st.daemon_version else ""
        st.emit(f"[daemon]    running (pid={st.daemon_pid}, state={st.daemon_state}{ver_tag})")
        if st.daemon_code_drift:
            st.emit(
                f"[daemon]    WARN {st.daemon_drift_reason}; run 'agent-voice-input restart' to update"
            )
        elif st.daemon_build_id_note:
            st.emit(f"[daemon]    INFO {st.daemon_build_id_note}")
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
    prompt_tag = ", prompt=custom" if cfg.refiner.prompt.strip() else ""

    if raw_ref_prov == "off":
        st.emit("[refiner]   off")
        st.emit(f"[pipeline]  {st.pipeline['summary']}")
        return
    if ref_info["bypassed"]:
        target_lbl = st.resolved_ref_prov if raw_ref_prov != "auto" else "Stage 2"
        st.emit(
            f"[refiner]   bypassed (single-hop ASR passthrough; set refiner.force_on_single_hop=true to run {target_lbl})"
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
        eff_ref_effort = str(ref_info["effort"])
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
                f"effort={eff_ref_effort}, {auth_note}{bin_tag}{prompt_tag})"
            )
        else:
            st.emit(
                f"[refiner]   FAIL {prov_disp} (model={st.eff_ref_model}, api={eff_ref_api}, "
                f"effort={eff_ref_effort}{prompt_tag}) -> {auth_note}"
            )
            st.emit_fix("run 'codex login' or set DEEPSEEK_API_KEY / DASHSCOPE_API_KEY in agent-voice-input.env")
            st.ok_all = False
        st.emit(f"[pipeline]  {st.pipeline['summary']}")
        return
    if st.resolved_ref_prov == "agy":
        eff_ref_api = str(ref_info["api"])
        eff_ref_effort = str(ref_info["effort"])
        st.eff_ref_model = str(ref_info["model"])
        agy_bin = find_agy_bin(st.env)
        if agy_bin:
            st.ref_ping_needed = True
            st.emit(
                f"[refiner]   OK agy (model={st.eff_ref_model}, api={eff_ref_api}, "
                f"effort={eff_ref_effort}, bin={agy_bin}{prompt_tag})"
            )
        else:
            st.emit(
                f"[refiner]   FAIL agy (model={st.eff_ref_model}, api={eff_ref_api}, "
                f"effort={eff_ref_effort}{prompt_tag}) -> agy CLI not found in PATH"
            )
            st.emit_fix("install Antigravity CLI ('agy'), set AGY_BIN, or switch refiner.provider")
            st.ok_all = False
        st.emit(f"[pipeline]  {st.pipeline['summary']}")
        return

    eff_ref_api = str(ref_info["api"])
    eff_ref_effort = str(ref_info["effort"])
    st.eff_ref_model = str(ref_info["model"])
    st.eff_ref_url = cfg.refiner.effective_base_url(st.resolved_ref_prov)
    prov_disp = f"auto -> {st.resolved_ref_prov}" if raw_ref_prov == "auto" else st.resolved_ref_prov
    ref_ready = bool(ref_info["ready"])
    st.emit(
        f"[refiner]   {'OK' if ref_ready else 'MISSING KEY'} {prov_disp} "
        f"(model={st.eff_ref_model}, api={eff_ref_api}, effort={eff_ref_effort}{prompt_tag})"
    )
    if ref_ready:
        st.ref_ping_needed = True
    else:
        st.ok_all = False
        if cfg.refiner.api_key_env.strip():
            st.emit_fix(f"set {cfg.refiner.api_key_env.strip()} in agent-voice-input.env")
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
        ping_cache: dict[tuple[str, str, str], tuple[str, int, str]] = {}
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
    if st.daemon_build_id_note:
        daemon_report["build_id_note"] = st.daemon_build_id_note
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
        "fixes": st.fixes,
        "lines": st.lines_out,
    }
    if include_ping:
        report["ping"] = st.ping_results
    print(json.dumps(_redact_json_obj(report), ensure_ascii=False, indent=2))


def _cmd_doctor(
    cfg: Config,
    env: dict[str, str],
    *,
    ping: bool = False,
    remote_ping: bool = False,
    as_json: bool = False,
    transport: HttpTransport | None = None,
) -> int:
    from .config import explain_pipeline_selection, get_runtime_dir, resolve_credentials

    creds = resolve_credentials(cfg, env)
    pipeline = explain_pipeline_selection(cfg, creds, env)
    st = _DoctorState(
        cfg=cfg,
        env=env,
        creds=creds,
        rt_dir=get_runtime_dir(env, create=True),
        as_json=as_json,
        all_secrets=tuple(creds.all_secret_values()),
        pipeline=pipeline,
    )
    _doctor_check_env_and_ffmpeg(st)
    _doctor_check_platform(st)
    _doctor_check_asr(st)
    _doctor_check_daemon_and_session(st)
    _doctor_check_remote_ssh(st)
    _doctor_check_refiner(st)
    _doctor_run_pings(st, ping=ping, remote_ping=remote_ping, transport=transport)
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
    from .daemon import (
        append_telemetry_record,
        build_pipeline_summary,
        create_platform,
        run_pipeline_once,
    )
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
                "ts": round(time.time(), 6),
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
            append_telemetry_record(rt_dir, err_rec)
            raise
        creds_ok = resolve_credentials(cfg, env)
        summary = build_pipeline_summary(
            res.seq_id,
            res,
            dry_run=dry_run,
            redact_fn=lambda s: redact_secrets(s, env, cfg=cfg, extra_secrets=creds_ok.all_secret_values()),
        )
        append_telemetry_record(rt_dir, summary)
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


def _pipeline_from_legacy_daemon_status(st: Mapping[str, Any]) -> dict[str, Any] | None:
    """Build a non-contradictory pipeline summary from a legacy daemon's reported provider fields."""
    asr_p = str(st.get("asr_provider") or "").strip()
    ref_p = str(st.get("refiner_provider") or "").strip()
    if not asr_p and not ref_p:
        return None
    asr_lbl = asr_p or "unknown"
    ref_lbl = ref_p or "unknown"
    return {
        "asr": {"provider": asr_lbl, "model": ""},
        "refiner": {"provider": ref_lbl, "model": ""},
        "summary": f"ASR {asr_lbl} -> Refiner {ref_lbl} (reported by running daemon)",
        "source": "legacy_daemon_status",
    }


def _format_start_readiness_line(
    cfg: Config,
    *,
    asr_ready: bool,
    hotkey_ready: bool = True,
) -> str | None:
    if not asr_ready:
        return "WARN: no Stage 1 ASR key configured; run 'agent-voice-input doctor' before dictating"
    if not hotkey_ready:
        return None
    hotkey = (cfg.hotkey.key or "RControl").strip() or "RControl"
    mode = (cfg.hotkey.trigger_mode or "tap_toggle").strip().lower()
    if mode == "ptt":
        return f"Ready: hold {hotkey} to speak, release to transcribe (press another key to cancel)"
    hc_ms = cfg.hotkey.effective_hold_cancel_ms()
    if hc_ms > 0:
        sec_str = f"{round(hc_ms / 1000.0, 2):g}s"
        return f"Ready: tap {hotkey} to start/stop recording (hold {sec_str} to cancel)"
    return f"Ready: tap {hotkey} to start/stop recording"


def _cmd_start(args: argparse.Namespace, env: dict[str, str], *, cfg: Config | None = None) -> int:
    from .config import (
        explain_pipeline_selection,
        get_runtime_dir,
        redact_secrets,
        resolve_credentials,
    )
    from .daemon import describe_daemon_drift, probe_daemon_status, send_control_command
    from .diagnose import read_redacted_log_tail
    from .platform_macos import (
        classify_macos_mic_permission,
        compile_mac_shim,
        run_mac_shim_self_check,
    )

    rt_dir = get_runtime_dir(env, create=True)
    has_overrides = any(getattr(args, attr, None) for _, attr in _CLI_OVERRIDE_FLAGS)
    pipeline_summary = ""
    asr_ready: bool | None = None
    if cfg is not None:
        pipe_info = explain_pipeline_selection(cfg, resolve_credentials(cfg, env), env)
        pipeline_summary = str(pipe_info["summary"])
        asr_ready = bool(pipe_info["asr"]["ready"])
    st = probe_daemon_status(rt_dir, timeout_sec=1.0)
    if st.get("ok"):
        _warn_degraded_shim(st)
        drift = describe_daemon_drift(st)
        if drift["drifted"]:
            print(
                f"already running (pid={st.get('pid')}, state={st.get('state')}, {drift['reason']}); "
                "run 'agent-voice-input restart' to update"
            )
        elif has_overrides:
            print(
                f"already running (pid={st.get('pid')}, state={st.get('state')}); "
                "run 'agent-voice-input restart' to apply new flags/config"
            )
        else:
            print(
                f"already running (pid={st.get('pid')}, state={st.get('state')}) "
                "(use 'restart' to reload config)"
            )
            if drift.get("note"):
                print(f"[daemon]    INFO {drift['note']}")
        running_pipe = st.get("pipeline")
        running_summary = (
            str(running_pipe.get("summary") or "").strip()
            if isinstance(running_pipe, Mapping)
            else ""
        )
        if running_summary:
            print(f"[pipeline]  {running_summary}")
        elif legacy_pipe := _pipeline_from_legacy_daemon_status(st):
            print(f"[pipeline]  {legacy_pipe['summary']}")
        elif cfg is not None and not has_overrides and pipeline_summary:
            print(f"[pipeline]  {pipeline_summary}")
        return 0
    if st.get("state") == "unresponsive":
        err_note = redact_secrets(
            str(st.get("error") or "control socket temporarily unresponsive"),
            env,
            cfg=cfg,
        )
        print(
            f"already running (pid={st.get('pid') or '-'}, state=unresponsive: {err_note}); "
            "run 'agent-voice-input diagnose' or 'agent-voice-input restart'"
        )
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
    from .platform_wsl import is_wsl

    start_wait_sec = 18.0 if (sys.platform != "darwin" and is_wsl(env)) else 10.0
    deadline = time.monotonic() + start_wait_sec
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            print(f"daemon exited early (rc={proc.returncode}); check {log_path}", file=sys.stderr)
            if diag := read_redacted_log_tail(log_path, start_offset, env, cfg=cfg):
                print(diag, file=sys.stderr)
            return 1
        try:
            if (st := send_control_command(rt_dir, {"cmd": "status"}, timeout_sec=0.5)).get("ok"):
                if not warned_shim:
                    _warn_degraded_shim(st)
                print(f"started (pid={st.get('pid')})")
                if pipeline_summary:
                    print(f"[pipeline]  {pipeline_summary}")
                if cfg is not None and asr_ready is not None:
                    if readiness_line := _format_start_readiness_line(
                        cfg,
                        asr_ready=asr_ready,
                        hotkey_ready=st.get("shim_ok") is True,
                    ):
                        print(readiness_line)
                return 0
        except Exception:
            time.sleep(0.15)
    print(f"started process pid={proc.pid} (socket warming up)")
    if pipeline_summary:
        print(f"[pipeline]  {pipeline_summary}")
    if cfg is not None and asr_ready is not None:
        if readiness_line := _format_start_readiness_line(
            cfg,
            asr_ready=asr_ready,
            hotkey_ready=False,
        ):
            print(readiness_line)
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


def _cmd_stop(env: dict[str, str]) -> int:
    from .config import get_runtime_dir
    from .daemon import send_control_command

    rt_dir = get_runtime_dir(env, create=False)
    pid_file = rt_dir / "daemon.pid"
    pid = _read_pid_file(pid_file)
    stop_deadline = time.monotonic() + 6.0
    shutdown_sent = False
    socket_still_live = False
    try:
        send_control_command(rt_dir, {"cmd": "shutdown"}, timeout_sec=1.5)
        shutdown_sent = True
    except (TimeoutError, socket.timeout, PermissionError):
        socket_still_live = True
    except OSError as sock_exc:
        if sock_exc.errno not in (
            None,
            errno.ENOENT,
            errno.ECONNREFUSED,
            errno.ENOTSOCK,
            errno.EPROTOTYPE,
        ):
            socket_still_live = True
    except Exception:
        pass

    if shutdown_sent and pid > 0:
        while time.monotonic() < stop_deadline:
            try:
                os.kill(pid, 0)
                time.sleep(0.05)
            except OSError:
                break

    pid_liveness = _probe_pid_liveness(pid)
    lock_confirmed_free = not rt_dir.is_dir()

    if rt_dir.is_dir():
        lfd: int | None = None
        while True:
            lfd = _try_open_daemon_lock_fd(rt_dir, create=True)
            if lfd is None:
                break
            try:
                fcntl.flock(lfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError) as exc:
                try:
                    os.close(lfd)
                except OSError:
                    pass
                lfd = None
                if isinstance(exc, OSError) and exc.errno not in (
                    errno.EACCES,
                    errno.EAGAIN,
                    errno.EWOULDBLOCK,
                ):
                    break
                if time.monotonic() >= stop_deadline:
                    break
                time.sleep(0.05)
        if lfd is not None:
            lock_confirmed_free = True
            try:
                cur_pid = _read_pid_file(pid_file)
                cur_liveness = _probe_pid_liveness(cur_pid)
                # Under LOCK_EX with no live socket:
                # - if shutdown was sent, only unlink once `pid` has actually exited (`gone`), preserving legacy live instances;
                # - if shutdown failed with offline socket (e.g. SIGKILL remnant whose PID may even be recycled), safe to unlink unless `perm`.
                can_unlink = (
                    not socket_still_live
                    and pid_liveness != "perm"
                    and cur_liveness != "perm"
                    and (not shutdown_sent or (pid_liveness == "gone" and cur_liveness == "gone"))
                )
                if can_unlink:
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

    if not shutdown_sent and (socket_still_live or not lock_confirmed_free or pid_liveness == "perm"):
        print(
            f"error: could not confirm daemon stop in {rt_dir}; check running processes",
            file=sys.stderr,
        )
        return 1
    print("stopped")
    return 0


def _cmd_hotwords(args: argparse.Namespace, cfg: Config, env: dict[str, str]) -> int:
    from .config import ConfigError, explain_pipeline_selection, resolve_credentials
    from .hotwords import (
        MAINSTREAM_QUOTA_PROVIDERS,
        VALID_HOTWORD_SCOPES,
        add_hotwords,
        budget_hotwords_for_provider,
        format_budget_line,
        load_hotwords_table,
        remove_hotwords,
        resolve_hotwords_path,
        summarize_hotwords_budgets,
    )

    action = str(getattr(args, "hotwords_action", "") or "").strip().lower()
    as_json = bool(getattr(args, "as_json", False))
    hw_path = resolve_hotwords_path(cfg.asr.hotwords_file, env=env)
    if hw_path is None:
        raise ConfigError(
            "asr.hotwords_file is empty; set a file path in config.toml before managing hotwords"
        )

    creds = resolve_credentials(cfg, env)
    asr_info = explain_pipeline_selection(cfg, creds, env)["asr"]
    eff_asr = str(asr_info["provider"])
    eff_asr_model = str(asr_info["model"])
    eff_asr_api = str(asr_info["api"])
    eff_asr_url = cfg.asr.effective_base_url(eff_asr)

    def _scene_label(sc: str) -> str:
        return "global" if sc == "global" else f"{sc}+global"

    if action == "list":
        scope_filter = str(getattr(args, "scope", "all") or "all").strip().lower()
        table = load_hotwords_table(hw_path, env=env, use_cache=False, strict_encoding=True)
        if scope_filter == "all":
            budgets = summarize_hotwords_budgets(
                table.global_terms,
                {"chat": table.chat_terms, "coding": table.coding_terms},
                active_provider=eff_asr,
                active_model=eff_asr_model,
                active_api=eff_asr_api,
                active_base_url=eff_asr_url,
            )
            display_scopes: tuple[str, ...] = VALID_HOTWORD_SCOPES
            ref_scene = str(budgets["reference_scene"])
            ref_terms = table.merged_for_scene(ref_scene)
        elif scope_filter == "global":
            budgets = summarize_hotwords_budgets(
                table.global_terms,
                None,
                active_provider=eff_asr,
                active_model=eff_asr_model,
                active_api=eff_asr_api,
                active_base_url=eff_asr_url,
            )
            display_scopes = ("global",)
            ref_scene = "global"
            ref_terms = table.merged_for_scene("global")
        else:
            budgets = summarize_hotwords_budgets(
                table.global_terms,
                table.terms_for_scope(scope_filter),
                active_provider=eff_asr,
                active_model=eff_asr_model,
                active_api=eff_asr_api,
                active_base_url=eff_asr_url,
            )
            display_scopes = (scope_filter,)
            ref_scene = scope_filter
            ref_terms = table.merged_for_scene(scope_filter)

        if as_json:
            payload: dict[str, Any] = {
                "path": str(hw_path),
                "exists": table.exists,
                "scope": scope_filter,
                "active_asr": {
                    "provider": eff_asr,
                    "model": eff_asr_model,
                    "api": eff_asr_api,
                },
                "scopes": {sc: list(table.terms_for_scope(sc)) for sc in display_scopes},
                "mounted": {sc: table.merged_for_scene(sc) for sc in display_scopes},
                "budgets": budgets,
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        status_lbl = "exists" if table.exists else "not created yet"
        print(f"Hotwords file: {hw_path} ({status_lbl})")
        print(f"Active ASR:    {eff_asr}/{eff_asr_model} (api={eff_asr_api})")
        for sc in display_scopes:
            sc_terms = table.terms_for_scope(sc)
            mount_note = (
                f"; mounts {sc}+global={len(table.merged_for_scene(sc))} terms"
                if sc != "global"
                else ""
            )
            print(f"\n[{sc}] ({len(sc_terms)} terms{mount_note})")
            if sc_terms:
                for term in sc_terms:
                    print(f"  {term}")
            else:
                print("  (empty)")

        print("\nProvider quotas (manual hotwords before dynamic session/workspace terms):")
        for sc in display_scopes:
            active_res = budget_hotwords_for_provider(
                table.merged_for_scene(sc),
                eff_asr,
                model=eff_asr_model,
                api=eff_asr_api,
                base_url=eff_asr_url,
            )
            print(f"  [active] {format_budget_line(active_res, name=f'{eff_asr} ({_scene_label(sc)})')}")
        print(f"  Mainstream reference ({_scene_label(ref_scene)}, {len(ref_terms)} manual terms):")
        for prov in MAINSTREAM_QUOTA_PROVIDERS:
            ref_res = budget_hotwords_for_provider(ref_terms, prov)
            print(f"    {format_budget_line(ref_res, name=prov)}")
        if not table.exists:
            print(
                "\nTip: run 'agent-voice-input hotwords add <term...> [--scope global|chat|coding]' to create it."
            )
        return 0

    raw_terms = list(getattr(args, "terms", ()) or ())
    scope = str(getattr(args, "scope", "global") or "global").strip().lower()
    try:
        if action == "add":
            edit_res = add_hotwords(hw_path, raw_terms, scope=scope)
        elif action == "remove":
            edit_res = remove_hotwords(hw_path, raw_terms, scope=scope)
        else:  # pragma: no cover - guarded by argparse required subparser
            raise ConfigError(f"unsupported hotwords action: {action!r}")
    except ValueError as exc:
        raise ConfigError(str(exc)) from None

    table_after = load_hotwords_table(hw_path, env=env, use_cache=False)
    eval_scene = (
        max(
            ("coding", "chat", "global"),
            key=lambda s: (len(table_after.merged_for_scene(s)), sum(len(w) for w in table_after.merged_for_scene(s))),
        )
        if scope == "all"
        else scope
    )
    active_budget = budget_hotwords_for_provider(
        table_after.merged_for_scene(eval_scene),
        eff_asr,
        model=eff_asr_model,
        api=eff_asr_api,
        base_url=eff_asr_url,
    )

    if as_json:
        out_obj: dict[str, Any] = {
            "action": action,
            "path": str(edit_res.path),
            "scope": edit_res.scope,
            "changed": edit_res.changed,
            "added": list(edit_res.added),
            "skipped": list(edit_res.skipped),
            "removed": list(edit_res.removed),
            "not_found": list(edit_res.not_found),
            "active_asr": {
                "provider": eff_asr,
                "model": eff_asr_model,
                "api": eff_asr_api,
                "scene": _scene_label(eval_scene),
                "budget": active_budget.as_dict(),
            },
        }
        print(json.dumps(out_obj, ensure_ascii=False, indent=2))
        return 0

    print(f"Hotwords file: {edit_res.path} (scope={edit_res.scope})")
    if action == "add":
        if edit_res.added:
            print(f"Added ({len(edit_res.added)}):   {', '.join(edit_res.added)}")
        if edit_res.skipped:
            print(f"Skipped ({len(edit_res.skipped)}): {', '.join(edit_res.skipped)} (already present)")
    else:
        if edit_res.removed:
            print(f"Removed ({len(edit_res.removed)}):   {', '.join(edit_res.removed)}")
        if edit_res.not_found:
            print(f"Not found ({len(edit_res.not_found)}): {', '.join(edit_res.not_found)}")
    print(f"Active ASR quota ({_scene_label(eval_scene)}): {format_budget_line(active_budget, name=eff_asr)}")
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
        help="Initialize 0600 config.toml and commented agent-voice-input.env",
    )
    p_init.add_argument(
        "--config",
        default=argparse.SUPPRESS,
        help="Target config.toml path (default: ~/.config/agent-voice-input/config.toml)",
    )
    p_init.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Atomically overwrite existing files",
    )
    for name, help_text in (
        ("daemon", "Run the voice input daemon in the foreground"),
        ("start", "Start the background daemon (idempotent if already running)"),
        ("restart", "Stop and restart the background daemon"),
        ("stop", "Stop the background daemon"),
        ("toggle", "Toggle recording start/stop on the running daemon"),
    ):
        _add_common_flags(sub.add_parser(name, help=help_text), is_sub=True)
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
        help="Manage layered plain-text ASR hotwords (list, add, remove) and inspect provider quotas",
    )
    _add_common_flags(p_hw, is_sub=True)
    hw_sub = p_hw.add_subparsers(dest="hotwords_action", required=True)
    p_hw_list = hw_sub.add_parser(
        "list",
        help="Show hotwords file path, scoped terms, and active/mainstream ASR provider quota usage",
    )
    _add_common_flags(p_hw_list, is_sub=True)
    p_hw_list.add_argument(
        "--scope",
        choices=("all", "global", "chat", "coding"),
        default="all",
        help="Filter displayed scope (default: all)",
    )
    p_hw_list.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit structured JSON output",
    )
    p_hw_add = hw_sub.add_parser(
        "add",
        help="Add one or more hotwords to a scope (default: global)",
    )
    _add_common_flags(p_hw_add, is_sub=True)
    p_hw_add.add_argument("terms", nargs="+", help="One or more hotwords or phrases to add")
    p_hw_add.add_argument(
        "--scope",
        choices=("global", "chat", "coding"),
        default="global",
        help="Target scope section (default: global)",
    )
    p_hw_add.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit structured JSON output",
    )
    p_hw_rm = hw_sub.add_parser(
        "remove",
        help="Remove one or more hotwords from a scope (default: global; use 'all' for all scopes)",
    )
    _add_common_flags(p_hw_rm, is_sub=True)
    p_hw_rm.add_argument("terms", nargs="+", help="One or more hotwords or phrases to remove")
    p_hw_rm.add_argument(
        "--scope",
        choices=("global", "chat", "coding", "all"),
        default="global",
        help="Target scope section to remove from (default: global)",
    )
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
    if getattr(args, "subcmd", None) == "hotwords" and isinstance(getattr(args, "terms", None), list):
        args.terms = [_recover_utf8_surrogates(str(tok)) for tok in args.terms]
    for attr in ("window_title", "title"):
        val = getattr(args, attr, None)
        if isinstance(val, str) and val:
            setattr(args, attr, _recover_utf8_surrogates(val))


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
    from .daemon import DaemonAlreadyRunningError

    if args.subcmd == "init":
        try:
            return _cmd_init(config_path=getattr(args, "config", None), force=bool(args.force))
        except ConfigError as exc:
            eff_env = load_env_files()
            print(f"config error: {redact_secrets(str(exc), eff_env)}", file=sys.stderr)
            return 2
        except Exception as exc:
            eff_env = load_env_files()
            print(f"error: {redact_secrets(str(exc), eff_env)}", file=sys.stderr)
            return 1
    ov = {
        k: getattr(args, a)
        for a, k in (
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
        cfg, env = load_config(config_path=getattr(args, "config", None), cli_overrides=ov)
        if args.subcmd == "daemon":
            from .daemon import VoiceInputDaemon

            VoiceInputDaemon(cfg, env=_sanitize_daemon_tmux_env(env)).serve_forever()
            return 0
        if args.subcmd == "start":
            return _cmd_start(args, env, cfg=cfg)
        if args.subcmd == "restart":
            rc_stop = _cmd_stop(env)
            if rc_stop != 0:
                return rc_stop
            if not _wait_daemon_lock_free(get_runtime_dir(env, create=False), timeout_sec=2.0):
                print(
                    "error: previous daemon still holds daemon.lock; run 'agent-voice-input stop' and retry",
                    file=sys.stderr,
                )
                return 1
            return _cmd_start(args, env, cfg=cfg)
        if args.subcmd == "stop":
            return _cmd_stop(env)
        if args.subcmd == "status":
            from .daemon import describe_daemon_drift, probe_daemon_status, read_telemetry_history

            rt_dir_st = get_runtime_dir(env, create=False)
            raw_hist = getattr(args, "history", None)
            hist_limit = max(0, min(500, int(raw_hist))) if raw_hist is not None else None
            resp = probe_daemon_status(rt_dir_st, timeout_sec=8.0, history=hist_limit)
            if resp.get("ok"):
                drift = describe_daemon_drift(resp)
                resp["code_drift"] = bool(drift["drifted"])
                if drift["drifted"]:
                    resp["current_version"] = drift["current_version"]
                    resp["current_build_id"] = drift["current_build_id"]
                    resp["drift_warning"] = f"{drift['reason']}; run 'agent-voice-input restart'"
                elif drift.get("note"):
                    resp["build_id_note"] = drift["note"]
                if not isinstance(resp.get("pipeline"), Mapping):
                    if legacy_pipe := _pipeline_from_legacy_daemon_status(resp):
                        resp["pipeline"] = legacy_pipe
                print(json.dumps(resp, ensure_ascii=False))
                return 0
            if resp.get("error"):
                resp["error"] = redact_secrets(str(resp["error"]), env, cfg=cfg)
            if hist_limit is not None:
                disk_hist = read_telemetry_history(rt_dir_st, limit=hist_limit)
                resp["last_result"] = dict(disk_hist[-1]) if disk_hist else {}
                resp["recent_results"] = disk_hist
            print(json.dumps(resp, ensure_ascii=False))
            return 1
        if args.subcmd in ("toggle", "cancel"):
            from .daemon import send_control_command

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
                print(json.dumps(resp, ensure_ascii=False))
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
        eff_env = env if env is not None else load_env_files()
        print(f"config error: {redact_secrets(str(exc), eff_env, cfg=cfg)}", file=sys.stderr)
        return 2
    except Exception as exc:
        eff_env = env if env is not None else load_env_files()
        print(f"error: {redact_secrets(str(exc), eff_env, cfg=cfg)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
