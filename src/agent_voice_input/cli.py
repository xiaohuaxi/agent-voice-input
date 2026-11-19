"""CLI entrypoint for agent-voice-input (daemon, start, restart, stop, status, toggle, cancel, once, retry, doctor, key-test, session-hook)."""
from __future__ import annotations

import argparse, json, os, signal, socket, stat, subprocess, sys, time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import __version__
from .asr_providers import _is_loopback_url
from .audio_proc import get_failed_audio_dir, list_failed_audios, resolve_failed_audio, resolve_ffmpeg_bin
from .config import (
    Config,
    ConfigError,
    expand_user_path,
    get_runtime_dir,
    is_dashscope_omni_chat_model,
    load_config,
    load_env_files,
    redact_secrets,
    resolve_asr_api,
    resolve_credentials,
    select_asr_provider,
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
from .refiner import find_codex_bin, inspect_codex_auth_status
from .session_ctx import SessionTracker
from .session_title import run_session_hook


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
        help="Override Stage 2 refiner provider (codex|dashscope|volcengine|openrouter|gemini|openai|groq|siliconflow|openai-compatible|off)",
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
    if env is not os.environ and not (env.get("CODEX_HOME") or "").strip() and not (env.get("HOME") or "").strip():
        return "models_cache.json not found"
    cache = Path(env.get("CODEX_HOME") or (expand_user_path("~", env=env) / ".codex")) / "models_cache.json"
    if not cache.is_file():
        return "models_cache.json not found"
    try:
        slugs = {m.get("slug") for m in (json.loads(cache.read_text(encoding="utf-8")).get("models") or []) if isinstance(m, dict)}
        return "present in models_cache.json" if model in slugs else f"NOT in models_cache.json ({model})"
    except Exception:
        return "unreadable models_cache.json"


def _cmd_doctor(cfg: Config, env: dict[str, str]) -> int:
    creds, rt_dir, ok_all = resolve_credentials(cfg, env), get_runtime_dir(env, create=True), True
    fg_title, fg_tty, fg_cwd, fg_pid = "", "", "", 0
    print(f"[python]    OK ({sys.version.split()[0]}, runtime={rt_dir})")
    try:
        print(f"[ffmpeg]    OK ({resolve_ffmpeg_bin()})")
    except Exception as exc:
        print(f"[ffmpeg]    FAIL ({exc})")
        ok_all = False
    if sys.platform == "darwin":
        m = probe_macos_host(cfg, env, announce_compile=True)
        fg_title = str(m.get("fg_title") or "")
        fg_tty = str(m.get("fg_tty") or "")
        fg_cwd = str(m.get("fg_cwd") or "")
        fg_pid = int(m.get("fg_pid") or 0)
        devs = m.get("audio_devices") or []
        devs_str = ", ".join(devs) if devs else "none listed (check mic device or grant Microphone permission)"
        print(
            f"[macos-aud] avfoundation={'OK' if m.get('avfoundation_ok') else 'WARN'} "
            f"device={m.get('configured_audio_device')} inputs={devs_str} | "
            f"pbcopy={'OK' if m.get('pbcopy_ok') else 'FAIL'} tmux={m.get('tmux_active')}"
        )
        shim_desc = (
            f"OK ({m.get('mac_shim_bin')})"
            if m.get("mac_shim_ok")
            else f"WARN ({m.get('mac_shim_error') or 'unavailable'})"
        )
        if m.get("swiftc_ok"):
            swiftc_desc = f"OK ({m.get('swiftc_bin')})"
        elif m.get("clt_ok") is False:
            swiftc_desc = "MISSING CLT (run 'xcode-select --install')"
        else:
            swiftc_desc = "WARN (not found)"
        mac_ui_info = f" ui_lang={m.get('ui_lang')}" if m.get("ui_lang") else ""
        print(f"[mac-shim]  swiftc={swiftc_desc} shim={shim_desc}{mac_ui_info}")
        if m.get("fg_surface_id"):
            print(
                f"[macos-fg]  app={m.get('fg_process')!r} surface={m.get('fg_surface_id')} "
                f"tty={m.get('fg_tty') or '-'} cwd={m.get('fg_cwd') or '-'} title={fg_title!r}"
            )
        elif m.get("fg_tty") or m.get("fg_cwd"):
            print(
                f"[macos-fg]  app={m.get('fg_process')!r} "
                f"tty={m.get('fg_tty') or '-'} cwd={m.get('fg_cwd') or '-'} title={fg_title!r}"
            )
        else:
            print(f"[macos-fg]  app={m.get('fg_process')!r} title={fg_title!r}")
        caller_mic = str(m.get("caller_mic_auth") or "").strip()
        tcc_prefix = f"caller_mic_auth={caller_mic} | " if caller_mic else ""
        print(f"[macos-tcc] {tcc_prefix}{m.get('permissions_hint')}")
        if caller_warn := str(m.get("caller_mic_warning") or "").strip():
            print(
                f"[macos-tcc] WARN (current shell context: {caller_warn}; "
                "note: reflects this shell, not an already-running GUI daemon)"
            )
        ok_all = ok_all and bool(m.get("pbcopy_ok"))
    elif is_wsl(env):
        w = probe_wsl_host(cfg, env)
        fg_title = str(w.get("fg_title") or "")
        ps_ok, pulse_ok, winmm_n = bool(w.get("powershell_ok")), bool(w.get("pulse_rdp_ok")), int(w.get("winmm_devs") or 0)
        ui_info = f" ui_lang={w.get('ui_lang')}" if w.get("ui_lang") else ""
        print(f"[wsl-audio] PulseAudio RDPSource={'OK' if pulse_ok else 'WARN'}, winmm waveIn devs={winmm_n}")
        print(f"[win-shim]  PowerShell={'OK (' + str(w.get('ps_version')) + ')' if ps_ok else 'FAIL'} fg={w.get('fg_process')!r}{ui_info}")
        ok_all = ok_all and ps_ok and (pulse_ok or winmm_n > 0)
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
    print(
        f"[asr]       selected={eff_asr} (model={eff_asr_model}, api={eff_asr_api}, effort={cfg.asr.effort}) -> "
        f"{'OK' if asr_ready else 'MISSING KEY'} | "
        f"dashscope={_cred_label(creds.dashscope)}, "
        f"doubao={_cred_label(creds.doubao_key)} (app_id={_cred_label(creds.doubao_app_id)}), "
        f"volcengine={_cred_label(creds.volcengine)}, "
        f"openrouter={_cred_label(creds.openrouter)}, "
        f"gemini={_cred_label(creds.gemini)}, "
        f"openai={_cred_label(creds.openai)}, "
        f"groq={_cred_label(creds.groq)}, "
        f"siliconflow={_cred_label(creds.siliconflow)}{asr_creds_tail}"
    )
    ok_all = ok_all and asr_ready
    match = SessionTracker(env=env, runtime_dir=rt_dir).resolve_session(
        fg_title,
        window_tty=fg_tty,
        window_cwd=fg_cwd,
        window_pid=fg_pid,
    )
    print(f"[session]   {match.session_key + ' (' + match.matched_by + ')' if match else 'none matched (will use empty context)'}")
    ref_prov = cfg.refiner.provider.strip().lower()
    eff_ref_api = cfg.refiner.effective_api(ref_prov)
    prompt_tag = ", prompt=custom" if cfg.refiner.prompt.strip() else ""
    asr_is_single_hop = eff_asr_api == "gemini-native" or (
        eff_asr_api == "chat-completions"
        and (eff_asr != "dashscope" or is_dashscope_omni_chat_model(eff_asr_model, cfg.asr.base_url))
    )
    if eff_ref_api == "off":
        print("[refiner]   off")
    elif asr_is_single_hop and not cfg.refiner.force_on_single_hop:
        print(
            f"[refiner]   bypassed (single-hop ASR passthrough; set refiner.force_on_single_hop=true to run {ref_prov})"
        )
    elif ref_prov == "codex":
        eff_ref_model = cfg.refiner.effective_model("codex")
        auth_ok, auth_note = inspect_codex_auth_status(cfg, env)
        if auth_ok:
            cbin = find_codex_bin(env)
            bin_tag = f", refresh_bin={cbin}" if cbin else ""
            print(
                f"[refiner]   OK codex (model={eff_ref_model} "
                f"[{_check_codex_model(eff_ref_model, env)}], api={eff_ref_api}, "
                f"effort={cfg.refiner.effort}, {auth_note}{bin_tag}{prompt_tag})"
            )
        else:
            print(
                f"[refiner]   FAIL codex (model={eff_ref_model}, api={eff_ref_api}, "
                f"effort={cfg.refiner.effort}{prompt_tag}) -> {auth_note}"
            )
            ok_all = False
    else:
        eff_ref_model = cfg.refiner.effective_model(ref_prov)
        eff_ref_url = cfg.refiner.effective_base_url(ref_prov)
        if cfg.refiner.api_key_env.strip():
            ref_ready = bool(creds.refiner_custom and creds.refiner_custom.value)
        elif ref_prov == "openai-compatible":
            ref_ready = bool(creds.openai or _is_loopback_url(eff_ref_url))
        else:
            ref_ready = bool(creds.for_provider(ref_prov))
        print(
            f"[refiner]   {'OK' if ref_ready else 'MISSING KEY'} {ref_prov} "
            f"(model={eff_ref_model}, api={eff_ref_api}, effort={cfg.refiner.effort}{prompt_tag})"
        )
        ok_all = ok_all and ref_ready
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
    for name in ("daemon", "start", "restart", "doctor", "stop", "toggle"):
        _add_common_flags(sub.add_parser(name), is_sub=True)
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
            return _cmd_doctor(cfg, env)
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
