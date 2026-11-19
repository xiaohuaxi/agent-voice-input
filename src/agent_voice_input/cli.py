"""CLI entrypoint for agent-voice-input (init, daemon, start, restart, stop, status, toggle, cancel, once, retry, doctor, key-test, session-hook)."""
from __future__ import annotations

import argparse, dataclasses, json, os, shlex, signal, socket, stat, subprocess, sys, time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import __version__
from .asr_providers import (
    GEMINI_DEFAULT_BASE_URL,
    HttpRequest,
    HttpTransport,
    _close_quietly,
    _is_loopback_url,
    _missing_key_hint,
    _validate_request_url,
    urllib_http_transport,
)
from .audio_proc import get_failed_audio_dir, list_failed_audios, resolve_failed_audio, resolve_ffmpeg_bin
from .config import (
    Config,
    ConfigError,
    ProviderCredentials,
    _host_matches_domain,
    default_config_file_candidates,
    default_env_file_candidates,
    ensure_secure_dir,
    expand_user_path,
    get_runtime_dir,
    is_dashscope_omni_chat_model,
    is_gemini_native_url,
    load_config,
    load_env_files,
    redact_secrets,
    resolve_asr_api,
    resolve_codex_home_dir,
    resolve_credentials,
    resolve_target_config_paths,
    select_asr_provider,
    select_refiner_provider,
)
from .daemon import (
    VoiceInputDaemon,
    append_telemetry_record,
    build_pipeline_summary,
    create_platform,
    read_telemetry_history,
    run_pipeline_once,
    send_control_command,
)
from .platform_macos import (
    classify_macos_mic_permission,
    compile_mac_shim,
    probe_macos_host,
    run_mac_shim_self_check,
    run_macos_key_test,
)
from .platform_wsl import ForegroundWindow, is_wsl, probe_wsl_host, run_key_test
from .refiner import (
    CODEX_SUBSCRIPTION_BASE_URL,
    CodexRefiner,
    _build_responses_url,
    find_codex_bin,
    inspect_codex_auth_status,
    resolve_codex_default_model,
)
from .session_ctx import SessionTracker
from .session_title import run_session_hook

_INIT_CONFIG_TOML = """\
# agent-voice-input configuration (chmod 600)
# Priority: CLI flags > config.toml > built-in defaults

[hotkey]
# Default Right Ctrl ("RControl"); also supports "RAlt", "Fn" (macOS), "F13".."F20", etc.
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
# Uncomment and fill in AT LEAST ONE provider key below:

# Alibaba Cloud Bailian / DashScope (Stage 1 qwen3-asr-flash + Stage 2 qwen3.8-flash, or single-hop qwen3.8-omni-flash)
# DASHSCOPE_API_KEY="sk-..."

# DeepSeek official API (fast Stage 2 refiner: deepseek-flash)
# DEEPSEEK_API_KEY="sk-..."

# OpenRouter (single key for Stage 1 ASR + Stage 2 Refiner)
# OPENROUTER_API_KEY="sk-or-..."

# Google Gemini (single-hop Stage 1 audio + context refinement, or Stage 2 Refiner)
# GEMINI_API_KEY="AIza..."

# Volcengine Doubao Speech (Stage 1 ASR) & Volcengine Ark (Stage 2 Refiner)
# DOUBAO_ASR_APP_ID="..."
# DOUBAO_ASR_API_KEY="..."
# ARK_API_KEY="..."

# OpenAI / Groq / SiliconFlow
# OPENAI_API_KEY="sk-..."
# GROQ_API_KEY="gsk_..."
# SILICONFLOW_API_KEY="sk-..."
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
        help="Override Stage 2 refiner provider (auto|codex|deepseek|dashscope|volcengine|openrouter|gemini|openai|groq|siliconflow|openai-compatible|off)",
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
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ConfigError(f"cannot inspect {path}: {exc}") from None


def _ensure_init_parent_dir(cfg_dir: Path, *, preserve_existing_mode: bool) -> None:
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
        f"  1. Add at least one API key in {env_path} (e.g. DASHSCOPE_API_KEY or GEMINI_API_KEY)\n"
        f"  2. Verify setup:  agent-voice-input doctor{cfg_flag} --ping\n"
        f"  3. Start daemon:  agent-voice-input start{cfg_flag}"
    )
    return 0


def _build_ping_http_spec(
    provider: str,
    base_url: str,
    key: str,
) -> tuple[str, dict[str, str]]:
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
            root = b or "https://openrouter.ai/api/v1"
            return f"{root}/key", {"Authorization": f"Bearer {key}"}
        return f"{b}/models", {"Authorization": f"Bearer {key}"}
    if prov == "dashscope":
        if not b:
            url = "https://dashscope.aliyuncs.com/compatible-mode/v1/models"
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
    root = b or "https://api.openai.com/v1"
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
    prov = provider.strip().lower()
    if prov == "doubao":
        return (
            "SKIP",
            0,
            "SKIP (verify audio with 'agent-voice-input once --file <wav> --dry-run')",
        )

    eff_tx = transport or urllib_http_transport
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
                    "openai", cfg.refiner.base_url or "https://api.openai.com/v1", c_item.token
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


def _inspect_hook_summary(env: Mapping[str, str], rt_dir: Path) -> str:
    configured: list[str] = []
    for label, rel in (
        ("claude", "~/.claude/settings.json"),
        ("gemini/agy", "~/.gemini/settings.json"),
    ):
        p = expand_user_path(rel, env=env)
        try:
            if p.is_file() and not p.is_symlink() and p.stat().st_size <= 524_288:
                if "session-hook" in p.read_text(encoding="utf-8", errors="ignore"):
                    configured.append(label)
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


def _cmd_doctor(
    cfg: Config,
    env: dict[str, str],
    *,
    ping: bool = False,
    as_json: bool = False,
    transport: HttpTransport | None = None,
) -> int:
    creds = resolve_credentials(cfg, env)
    rt_dir = get_runtime_dir(env, create=True)
    ok_all = True
    all_secrets = creds.all_secret_values()
    lines_out: list[str] = []
    fixes: list[str] = []

    def _clean(text: str) -> str:
        return redact_secrets(text, env, cfg=cfg, extra_secrets=all_secrets)

    def _emit(line: str) -> None:
        safe = _clean(line)
        lines_out.append(safe)
        if not as_json:
            print(safe)

    def _emit_fix(fix_cmd: str) -> None:
        safe_fix = _clean(fix_cmd)
        fixes.append(safe_fix)
        _emit(f"            Fix: {safe_fix}")

    fg_title, fg_tty, fg_cwd, fg_pid = "", "", "", 0
    _emit(f"[python]    OK ({sys.version.split()[0]}, runtime={rt_dir})")
    _emit(f"[config]    {cfg.config_path or 'defaults (no config.toml)'}")
    seen_env_inodes: set[tuple[int, int]] = set()
    for cand_env in default_env_file_candidates(env, config_path=cfg.config_path or None):
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
            _emit(
                f"[env-perm]  WARN {cand_env} has open permissions ({mode_oct}; recommend: chmod 600 {real_target})"
            )

    ffmpeg_ok = False
    try:
        ff_bin = resolve_ffmpeg_bin()
        ffmpeg_ok = True
        _emit(f"[ffmpeg]    OK ({ff_bin})")
    except Exception as exc:
        _emit(f"[ffmpeg]    FAIL ({exc})")
        _emit_fix("brew install ffmpeg" if sys.platform == "darwin" else "sudo apt install ffmpeg")
        ok_all = False

    platform_kind = "linux"
    if sys.platform == "darwin":
        platform_kind = "darwin"
        m = probe_macos_host(cfg, env, announce_compile=(not as_json))
        fg_title = str(m.get("fg_title") or "")
        fg_tty = str(m.get("fg_tty") or "")
        fg_cwd = str(m.get("fg_cwd") or "")
        fg_pid = int(m.get("fg_pid") or 0)
        devs = m.get("audio_devices") or []
        devs_str = ", ".join(devs) if devs else "none listed (check mic device or grant Microphone permission)"
        is_ssh_env = any(str(env.get(k) or "").strip() for k in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"))
        is_local_gui = (
            m.get("caller_has_graphic_session") is not False
            and not bool(m.get("caller_is_remote_session"))
            and not is_ssh_env
        )
        avf_fail = is_local_gui and (m.get("avfoundation_ok") is False)
        avf_label = "OK" if m.get("avfoundation_ok") else ("FAIL" if avf_fail else "WARN")
        _emit(
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
        _emit(f"[mac-shim]  swiftc={swiftc_desc} shim={shim_desc}{mac_ui_info}")
        if shim_fail:
            ok_all = False
            _emit_fix("xcode-select --install")
        if m.get("fg_surface_id"):
            _emit(
                f"[macos-fg]  app={m.get('fg_process')!r} surface={m.get('fg_surface_id')} "
                f"tty={m.get('fg_tty') or '-'} cwd={m.get('fg_cwd') or '-'} title={fg_title!r}"
            )
        elif m.get("fg_tty") or m.get("fg_cwd"):
            _emit(
                f"[macos-fg]  app={m.get('fg_process')!r} "
                f"tty={m.get('fg_tty') or '-'} cwd={m.get('fg_cwd') or '-'} title={fg_title!r}"
            )
        else:
            _emit(f"[macos-fg]  app={m.get('fg_process')!r} title={fg_title!r}")
        caller_mic = str(m.get("caller_mic_auth") or "").strip().lower()
        tcc_prefix = f"caller_mic_auth={caller_mic} | " if caller_mic else ""
        _emit(f"[macos-tcc] {tcc_prefix}{m.get('permissions_hint')}")
        if is_local_gui and caller_mic in ("denied", "restricted"):
            ok_all = False
            _emit(f"[macos-tcc] FAIL Microphone permission is {caller_mic}")
            _emit_fix('open "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"')
        elif caller_warn := str(m.get("caller_mic_warning") or "").strip():
            _emit(
                f"[macos-tcc] WARN (current shell context: {caller_warn}; "
                "note: reflects this shell, not an already-running GUI daemon)"
            )
        if avf_fail:
            ok_all = False
            _emit_fix("connect/enable an input microphone in System Settings -> Sound -> Input")
        if m.get("ax_trusted") is False:
            _emit(
                '[macos-ax]  WARN Accessibility not granted (Ghostty/tmux direct delivery works; '
                'for Cmd+V in other apps: open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")'
            )
        if (
            not _is_macos_modifier_hotkey(cfg.hotkey.key)
            and m.get("listen_event_access") is False
            and m.get("ax_trusted") is False
        ):
            _emit(
                '[macos-im]  WARN Input Monitoring not granted for non-modifier hotkey '
                '(open "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent")'
            )
        if m.get("pbcopy_ok") is False:
            ok_all = False
    elif is_wsl(env):
        platform_kind = "wsl2"
        w = probe_wsl_host(cfg, env)
        fg_title = str(w.get("fg_title") or "")
        ps_ok = bool(w.get("powershell_ok"))
        pulse_ok = bool(w.get("pulse_rdp_ok"))
        winmm_n = int(w.get("winmm_devs") or 0)
        has_wasapi_key = "wasapi_ok" in w
        wasapi_ok = bool(w.get("wasapi_ok")) if has_wasapi_key else False
        has_wsl_audio = (wasapi_ok or pulse_ok or winmm_n > 0) if has_wasapi_key else (pulse_ok or winmm_n > 0)
        ui_info = f" ui_lang={w.get('ui_lang')}" if w.get("ui_lang") else ""
        wasapi_prefix = f"WASAPI={'OK' if wasapi_ok else 'WARN'}, " if has_wasapi_key else ""
        _emit(
            f"[wsl-audio] {wasapi_prefix}PulseAudio RDPSource={'OK' if pulse_ok else 'WARN'}, winmm waveIn devs={winmm_n}"
        )
        _emit(
            f"[win-shim]  PowerShell={'OK (' + str(w.get('ps_version')) + ')' if ps_ok else 'FAIL'} fg={w.get('fg_process')!r}{ui_info}"
        )
        if not ps_ok:
            ok_all = False
            _emit_fix("check WSL interop or /mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
        if not has_wsl_audio:
            ok_all = False
            _emit_fix("cmd.exe /c start ms-settings:privacy-microphone")
    else:
        _emit(
            "[platform]  INFO native Linux (native global hotkey/HUD require macOS or WSL2; CLI 'once --file' ready)"
        )

    eff_asr = select_asr_provider(cfg, creds)
    eff_asr_model = cfg.asr.effective_model(eff_asr)
    eff_asr_url = cfg.asr.effective_base_url(eff_asr)
    eff_asr_api = resolve_asr_api(eff_asr, eff_asr_model, cfg.asr.api, cfg.asr.base_url)
    if cfg.asr.api_key_env.strip():
        asr_ready = bool(creds.asr_custom and creds.asr_custom.value)
    elif eff_asr == "openai-compatible":
        asr_ready = bool(creds.openai or _is_loopback_url(eff_asr_url))
    else:
        asr_ready = bool(creds.for_provider(eff_asr))
    asr_creds_tail = f", asr_custom={_cred_label(creds.asr_custom)}" if cfg.asr.api_key_env.strip() else ""
    _emit(
        f"[asr]       selected={eff_asr} (model={eff_asr_model}, api={eff_asr_api}, effort={cfg.asr.effort}) -> "
        f"{'OK' if asr_ready else 'MISSING KEY'} | "
        f"dashscope={_cred_label(creds.dashscope)}, "
        f"deepseek={_cred_label(creds.deepseek)}, "
        f"doubao={_cred_label(creds.doubao_key)} (app_id={_cred_label(creds.doubao_app_id)}), "
        f"volcengine={_cred_label(creds.volcengine)}, "
        f"openrouter={_cred_label(creds.openrouter)}, "
        f"gemini={_cred_label(creds.gemini)}, "
        f"openai={_cred_label(creds.openai)}, "
        f"groq={_cred_label(creds.groq)}, "
        f"siliconflow={_cred_label(creds.siliconflow)}{asr_creds_tail}"
    )
    if not asr_ready:
        ok_all = False
        _emit_fix(
            "run 'agent-voice-input init' then set DASHSCOPE_API_KEY (or OPENROUTER_API_KEY / GEMINI_API_KEY) "
            "in ~/.config/agent-voice-input/agent-voice-input.env"
        )

    daemon_running = False
    daemon_pid: int | None = None
    daemon_state = "stopped"
    if (rt_dir / "control.sock").exists():
        try:
            dst = send_control_command(rt_dir, {"cmd": "status"}, timeout_sec=0.5)
            if dst.get("ok"):
                daemon_running = True
                daemon_pid = int(dst.get("pid") or 0) or None
                daemon_state = str(dst.get("state") or "idle")
        except Exception:
            pass
    if daemon_running:
        _emit(f"[daemon]    running (pid={daemon_pid}, state={daemon_state})")
    else:
        _emit("[daemon]    stopped (start via 'agent-voice-input start')")

    match = SessionTracker(env=env, runtime_dir=rt_dir).resolve_session(
        fg_title,
        window_tty=fg_tty,
        window_cwd=fg_cwd,
        window_pid=fg_pid,
    )
    _emit(
        f"[session]   {match.session_key + ' (' + match.matched_by + ')' if match else 'none matched (will use empty context)'}"
    )
    _emit(f"[hooks]     {_inspect_hook_summary(env, rt_dir)}")

    raw_ref_prov = cfg.refiner.provider.strip().lower()
    if raw_ref_prov in ("none", "false", "0", "disabled"):
        raw_ref_prov = "off"
    resolved_ref_prov = select_refiner_provider(cfg, creds, env)
    prompt_tag = ", prompt=custom" if cfg.refiner.prompt.strip() else ""
    asr_is_single_hop = eff_asr_api == "gemini-native" or (
        eff_asr_api == "chat-completions"
        and (eff_asr != "dashscope" or is_dashscope_omni_chat_model(eff_asr_model, cfg.asr.base_url))
    )

    ref_ping_needed = False
    eff_ref_model = ""
    eff_ref_url = ""
    if raw_ref_prov == "off":
        _emit("[refiner]   off")
    elif asr_is_single_hop and not cfg.refiner.force_on_single_hop:
        target_lbl = resolved_ref_prov if raw_ref_prov != "auto" else "Stage 2"
        _emit(
            f"[refiner]   bypassed (single-hop ASR passthrough; set refiner.force_on_single_hop=true to run {target_lbl})"
        )
    elif raw_ref_prov == "auto" and resolved_ref_prov == "off":
        _emit(
            "[refiner]   WARN auto -> off (no Stage 2 credentials set; raw Stage 1 ASR draft will be delivered. "
            "Optional fix: set DEEPSEEK_API_KEY / DASHSCOPE_API_KEY or run 'codex login')"
        )
    elif resolved_ref_prov == "codex":
        eff_ref_api = cfg.refiner.effective_api("codex")
        eff_ref_effort = cfg.refiner.effective_effort("codex")
        eff_ref_model = resolve_codex_default_model(cfg, env)
        prov_disp = "auto -> codex" if raw_ref_prov == "auto" else "codex"
        auth_state, auth_note = inspect_codex_auth_status(cfg, env)
        if auth_state in ("OK", "WARN"):
            ref_ping_needed = True
            cbin = find_codex_bin(env)
            bin_tag = f", refresh_bin={cbin}" if cbin else ""
            _emit(
                f"[refiner]   {auth_state} {prov_disp} (model={eff_ref_model} "
                f"[{_check_codex_model(eff_ref_model, env)}], api={eff_ref_api}, "
                f"effort={eff_ref_effort}, {auth_note}{bin_tag}{prompt_tag})"
            )
        else:
            _emit(
                f"[refiner]   FAIL {prov_disp} (model={eff_ref_model}, api={eff_ref_api}, "
                f"effort={eff_ref_effort}{prompt_tag}) -> {auth_note}"
            )
            _emit_fix("run 'codex login' or set DEEPSEEK_API_KEY / DASHSCOPE_API_KEY in agent-voice-input.env")
            ok_all = False
    else:
        eff_ref_api = cfg.refiner.effective_api(resolved_ref_prov)
        eff_ref_effort = cfg.refiner.effective_effort(resolved_ref_prov)
        eff_ref_model = cfg.refiner.effective_model(resolved_ref_prov)
        eff_ref_url = cfg.refiner.effective_base_url(resolved_ref_prov)
        prov_disp = f"auto -> {resolved_ref_prov}" if raw_ref_prov == "auto" else resolved_ref_prov
        if cfg.refiner.api_key_env.strip():
            ref_ready = bool(creds.refiner_custom and creds.refiner_custom.value)
        elif resolved_ref_prov == "openai-compatible":
            ref_ready = bool(creds.openai or _is_loopback_url(eff_ref_url))
        else:
            ref_ready = bool(creds.for_provider(resolved_ref_prov))
        _emit(
            f"[refiner]   {'OK' if ref_ready else 'MISSING KEY'} {prov_disp} "
            f"(model={eff_ref_model}, api={eff_ref_api}, effort={eff_ref_effort}{prompt_tag})"
        )
        if ref_ready:
            ref_ping_needed = True
        else:
            ok_all = False
            if cfg.refiner.api_key_env.strip():
                _emit_fix(f"set {cfg.refiner.api_key_env.strip()} in agent-voice-input.env")
            else:
                _emit_fix(_missing_key_hint(resolved_ref_prov, stage="refiner"))

    ping_results: dict[str, dict[str, Any]] = {}
    if ping:
        ping_cache: dict[tuple[str, str, str], tuple[str, int, str]] = {}
        if asr_ready:
            st_asr, ms_asr, msg_asr = _probe_stage_ping(
                stage="asr",
                provider=eff_asr,
                cfg=cfg,
                creds=creds,
                env=env,
                base_url=eff_asr_url,
                transport=transport,
                cache=ping_cache,
            )
            ping_results["asr"] = {"provider": eff_asr, "status": st_asr, "latency_ms": ms_asr, "detail": _clean(msg_asr)}
            _emit(f"[ping]      asr({eff_asr}): {msg_asr}")
            if st_asr == "FAIL":
                ok_all = False
        if ref_ping_needed:
            st_ref, ms_ref, msg_ref = _probe_stage_ping(
                stage="refiner",
                provider=resolved_ref_prov,
                cfg=cfg,
                creds=creds,
                env=env,
                base_url=eff_ref_url,
                transport=transport,
                cache=ping_cache,
            )
            ping_results["refiner"] = {
                "provider": resolved_ref_prov,
                "status": st_ref,
                "latency_ms": ms_ref,
                "detail": _clean(msg_ref),
            }
            _emit(f"[ping]      refiner({resolved_ref_prov}): {msg_ref}")
            if st_ref == "FAIL":
                ok_all = False

    if as_json:
        def _redact_json_obj(val: Any) -> Any:
            if isinstance(val, str):
                return _clean(val)
            if isinstance(val, dict):
                return {str(k): _redact_json_obj(v) for k, v in val.items()}
            if isinstance(val, (list, tuple)):
                return [_redact_json_obj(v) for v in val]
            return val

        report: dict[str, Any] = {
            "ok": ok_all,
            "version": __version__,
            "platform": platform_kind,
            "config_path": cfg.config_path or "",
            "ffmpeg_ok": ffmpeg_ok,
            "daemon": {"running": daemon_running, "pid": daemon_pid, "state": daemon_state},
            "asr": {
                "provider": eff_asr,
                "model": eff_asr_model,
                "api": eff_asr_api,
                "ready": asr_ready,
                "single_hop": asr_is_single_hop,
            },
            "refiner": {
                "configured_provider": raw_ref_prov,
                "resolved_provider": resolved_ref_prov,
                "model": eff_ref_model,
                "bypassed": bool(asr_is_single_hop and not cfg.refiner.force_on_single_hop and raw_ref_prov != "off"),
            },
            "session": (
                {"session_key": match.session_key, "matched_by": match.matched_by}
                if match is not None
                else None
            ),
            "fixes": fixes,
            "lines": lines_out,
        }
        if ping:
            report["ping"] = ping_results
        print(json.dumps(_redact_json_obj(report), ensure_ascii=False, indent=2))

    return 0 if ok_all else 1


def _run_wav_or_live(
    *,
    wav_file: str | Path | None,
    audio_source: str,
    duration: float,
    dry_run: bool,
    title: str | None,
    cfg: Config,
    env: dict[str, str],
) -> int:
    rt_dir = get_runtime_dir(env, create=True)
    plat = None if (wav_file and dry_run and title is not None) else create_platform(cfg, env=env)
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
            err_rec: dict[str, Any] = {
                "ts": round(time.time(), 6),
                "seq_id": 1,
                "ok": False,
                "audio_source": src,
                "error": redact_secrets(str(exc), env, cfg=cfg, extra_secrets=creds_err.all_secret_values()),
            }
            if saved_p := str(getattr(exc, "_saved_audio_path", "") or ""):
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
            return 1
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


def _read_redacted_log_tail(
    log_path: Path,
    start_offset: int,
    env: Mapping[str, str],
    *,
    cfg: Config | None = None,
    max_bytes: int = 2048,
    max_lines: int = 12,
) -> str:
    """Read newly appended regular-file log lines since ``start_offset`` with secret redaction."""
    try:
        st = os.lstat(log_path)
        if not stat.S_ISREG(st.st_mode) or st.st_size <= 0:
            return ""
        base = start_offset if 0 <= start_offset < st.st_size else max(0, st.st_size - max_bytes)
        seek_pos = max(base, st.st_size - max(256, int(max_bytes)))
        with open(log_path, "rb") as f:
            if seek_pos > 0:
                f.seek(seek_pos)
            raw = f.read()
        lines = [ln.rstrip() for ln in raw.decode("utf-8", errors="replace").splitlines() if ln.strip()]
        if not lines:
            return ""
        creds = resolve_credentials(cfg, env) if cfg is not None else None
        extra = creds.all_secret_values() if creds is not None else ()
        return redact_secrets("\n".join(lines[-max_lines:]), env, cfg=cfg, extra_secrets=extra)
    except Exception:
        return ""


def _warn_degraded_shim(st: Mapping[str, Any]) -> None:
    if st.get("shim_ok") is False and (se := str(st.get("shim_error") or "").strip()):
        print(
            f"[agent-voice-input] WARN: native hotkey/HUD helper unavailable ({se}); "
            "CLI 'toggle'/'once' remain available.",
            file=sys.stderr,
            flush=True,
        )


def _cmd_start(args: argparse.Namespace, env: dict[str, str], *, cfg: Config | None = None) -> int:
    rt_dir = get_runtime_dir(env, create=True)
    has_overrides = any(getattr(args, attr, None) for _, attr in _CLI_OVERRIDE_FLAGS)
    try:
        if (st := send_control_command(rt_dir, {"cmd": "status"}, timeout_sec=1.0)).get("ok"):
            _warn_degraded_shim(st)
            if has_overrides:
                print(
                    f"already running (pid={st.get('pid')}, state={st.get('state')}); "
                    "run 'agent-voice-input restart' to apply new flags/config"
                )
            else:
                print(
                    f"already running (pid={st.get('pid')}, state={st.get('state')}) "
                    "(use 'restart' to reload config)"
                )
            return 0
    except Exception:
        pass
    src_root = Path(__file__).resolve().parents[1]
    child_env = _sanitize_daemon_tmux_env(os.environ)
    if (src_root / "agent_voice_input").is_dir():
        prev_pp = child_env.get("PYTHONPATH", "")
        child_env["PYTHONPATH"] = f"{src_root}{os.pathsep}{prev_pp}" if prev_pp else str(src_root)
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
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            print(f"daemon exited early (rc={proc.returncode}); check {log_path}", file=sys.stderr)
            if diag := _read_redacted_log_tail(log_path, start_offset, env, cfg=cfg):
                print(diag, file=sys.stderr)
            return 1
        try:
            if (st := send_control_command(rt_dir, {"cmd": "status"}, timeout_sec=0.5)).get("ok"):
                if not warned_shim:
                    _warn_degraded_shim(st)
                print(f"started (pid={st.get('pid')})")
                return 0
        except Exception:
            time.sleep(0.15)
    print(f"started process pid={proc.pid} (socket warming up)")
    return 0


def _cmd_stop(env: dict[str, str]) -> int:
    rt_dir = get_runtime_dir(env, create=False)
    pid_file = rt_dir / "daemon.pid"
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip()) if pid_file.is_file() else 0
    except ValueError:
        pid = 0
    try:
        send_control_command(rt_dir, {"cmd": "shutdown"}, timeout_sec=1.5)
    except Exception:
        if pid > 0:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    if pid > 0:
        for _ in range(20):
            try:
                os.kill(pid, 0)
                time.sleep(0.1)
            except OSError:
                break
    dead = pid <= 0
    if not dead:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            dead = True
        except PermissionError:
            dead = False
        except OSError:
            dead = True
    if dead:
        for stale in (pid_file, rt_dir / "control.sock"):
            try:
                stale.unlink(missing_ok=True)
            except OSError:
                pass
    print("stopped")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-voice-input",
        description="Two-stage context-aware voice input for CLI coding agents.",
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
    for name in ("daemon", "start", "restart", "stop", "toggle"):
        _add_common_flags(sub.add_parser(name), is_sub=True)
    p_doc = sub.add_parser("doctor", help="Check audio bridge, native shim, credentials, and session binding")
    _add_common_flags(p_doc, is_sub=True)
    p_doc.add_argument(
        "--ping",
        action="store_true",
        help="Run lightweight 5s online connectivity & auth checks for active endpoints",
    )
    p_doc.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit redacted diagnostic report as JSON",
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
    p_status = sub.add_parser("status")
    _add_common_flags(p_status, is_sub=True)
    p_status.add_argument(
        "--history",
        nargs="?",
        const=10,
        type=int,
        default=None,
        help="Include last N redacted numeric records (default: 10)",
    )
    p_once = sub.add_parser("once")
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
    p_retry.add_argument("--title", help="Optional window title override for session matching")
    p_kt = sub.add_parser("key-test")
    _add_common_flags(p_kt, is_sub=True)
    p_kt.add_argument("--seconds", type=int, default=10, help="Probe duration in seconds (Esc exits early)")
    p_hook = sub.add_parser("session-hook", help="Update terminal OSC 2 title and session registry from Agent hook stdin")
    p_hook.add_argument("--cli", choices=("auto", "claude", "agy", "gemini"), default="auto", help="Source Agent CLI type")
    p_hook.add_argument("--quiet", action="store_true", help="Suppress JSON '{}' stdout (e.g. when called from statusline)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.subcmd == "session-hook":
        return run_session_hook(cli_mode=args.cli, quiet_stdout=args.quiet)
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
            VoiceInputDaemon(cfg, env=_sanitize_daemon_tmux_env(env)).serve_forever()
            return 0
        if args.subcmd == "start":
            return _cmd_start(args, env, cfg=cfg)
        if args.subcmd == "restart":
            _cmd_stop(env)
            return _cmd_start(args, env, cfg=cfg)
        if args.subcmd == "stop":
            return _cmd_stop(env)
        if args.subcmd in ("status", "toggle", "cancel"):
            ctrl_timeout = 20.0 if args.subcmd == "toggle" else 8.0
            rt_dir_st = get_runtime_dir(env, create=False)
            raw_hist = getattr(args, "history", None) if args.subcmd == "status" else None
            hist_limit = max(0, min(500, int(raw_hist))) if raw_hist is not None else None
            ctrl_req: dict[str, Any] = {"cmd": args.subcmd}
            if hist_limit is not None:
                ctrl_req["history"] = hist_limit
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
                if args.subcmd == "status":
                    out_st: dict[str, Any] = {"ok": False, "state": "stopped"}
                    if hist_limit is not None:
                        disk_hist = read_telemetry_history(rt_dir_st, limit=hist_limit)
                        out_st["last_result"] = dict(disk_hist[-1]) if disk_hist else {}
                        out_st["recent_results"] = disk_hist
                    print(json.dumps(out_st, ensure_ascii=False))
                    return 1
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
        if args.subcmd == "doctor":
            return _cmd_doctor(
                cfg,
                env,
                ping=bool(getattr(args, "ping", False)),
                as_json=bool(getattr(args, "as_json", False)),
            )
        if args.subcmd == "key-test":
            if sys.platform == "darwin":
                return run_macos_key_test(cfg, seconds=args.seconds, env=env)
            return run_key_test(cfg, seconds=args.seconds)
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
