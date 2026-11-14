"""CLI entrypoint for agent-voice-input (daemon, start, stop, status, toggle, once, doctor, key-test, session-hook)."""
from __future__ import annotations

import argparse, json, os, signal, subprocess, sys, time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import __version__
from .asr_providers import _is_loopback_url
from .audio_proc import resolve_ffmpeg_bin
from .config import (
    Config,
    ConfigError,
    expand_user_path,
    get_runtime_dir,
    load_config,
    load_env_files,
    redact_secrets,
    resolve_asr_api,
    resolve_credentials,
    select_asr_provider,
)
from .daemon import VoiceInputDaemon, create_platform, run_pipeline_once, send_control_command
from .platform_macos import probe_macos_host, run_macos_key_test
from .platform_wsl import ForegroundWindow, is_wsl, probe_wsl_host, run_key_test
from .refiner import find_codex_bin
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
        "--refiner-effort",
        "--effort",
        dest="effort",
        help="Override Stage 2 reasoning/thinking effort (auto|default|none|minimal|low|medium|high|xhigh|max)",
        **kw,
    )


def _cred_label(item: Any) -> str:
    return f"OK (via {item.source_env})" if item is not None else "not set"


def _check_codex_model(model: str, env: dict[str, str]) -> str:
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
        m = probe_macos_host(cfg, env)
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
        mac_ui_info = f" ui_lang={m.get('ui_lang')}" if m.get("ui_lang") else ""
        print(
            f"[mac-shim]  swiftc={'OK (' + str(m.get('swiftc_bin')) + ')' if m.get('swiftc_ok') else 'WARN (not found)'} "
            f"shim={shim_desc}{mac_ui_info}"
        )
        if m.get("fg_surface_id"):
            print(
                f"[macos-fg]  app={m.get('fg_process')!r} surface={m.get('fg_surface_id')} "
                f"tty={m.get('fg_tty') or '-'} cwd={m.get('fg_cwd') or '-'} title={fg_title!r}"
            )
        else:
            print(f"[macos-fg]  app={m.get('fg_process')!r} title={fg_title!r}")
        print(f"[macos-tcc] {m.get('permissions_hint')}")
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
    if ref_prov in ("", "off", "none", "false", "0", "disabled"):
        print("[refiner]   off")
    elif ref_prov == "codex":
        eff_ref_model = cfg.refiner.effective_model("codex")
        if cbin := find_codex_bin(env):
            print(
                f"[refiner]   OK codex ({cbin}, model={eff_ref_model} "
                f"[{_check_codex_model(eff_ref_model, env)}], effort={cfg.refiner.effort})"
            )
        else:
            print("[refiner]   FAIL codex binary not found")
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
            f"(model={eff_ref_model}, effort={cfg.refiner.effort})"
        )
        ok_all = ok_all and ref_ready
    return 0 if ok_all else 1


def _cmd_once(args: argparse.Namespace, cfg: Config, env: dict[str, str]) -> int:
    rt_dir = get_runtime_dir(env, create=True)
    plat = None if (args.wav_file and args.dry_run and args.title is not None) else create_platform(cfg, env=env)
    try:
        win = ForegroundWindow(0, "", args.title or "") if plat is None else plat.get_foreground_window()
        if args.wav_file:
            raw_wav, src = args.wav_file, "file"
        else:
            assert plat is not None
            print(f"Recording {args.duration:.1f}s ...", file=sys.stderr, flush=True)
            plat.start_recording(rt_dir)
            time.sleep(max(0.2, float(args.duration)))
            raw_wav, src = plat.stop_recording()
        res = run_pipeline_once(
            raw_wav, cfg=cfg, platform=plat, origin_window=win, window_title_override=args.title,
            audio_source=src, dry_run=args.dry_run, env=env,
        )
        if not res.ok:
            print(f"[agent-voice-input] skipped: {res.skipped_reason}", file=sys.stderr)
            return 1
        if args.dry_run and res.asr:
            fb = f" fallback={res.refined.used_fallback}" if (res.refined and res.refined.used_fallback) else ""
            r_tag = f"{res.refined.provider}/{res.refined.model} {res.refined.latency_ms}ms{fb}" if res.refined else "off"
            print(
                f"[session] {res.session.session_key if res.session else 'none'}\n"
                f"[stage1]  ({res.asr.provider}/{res.asr.model} {res.asr.latency_ms}ms): {res.asr.text}\n"
                f"[stage2]  ({r_tag}): {res.final_text}",
                file=sys.stderr,
            )
        print(res.final_text)
        return 0
    finally:
        if plat is not None:
            plat.close()


def _sanitize_daemon_tmux_env(env: Mapping[str, str]) -> dict[str, str]:
    """Strip ``TMUX`` / ``TMUX_PANE`` from global daemon env while keeping socket discovery hint."""
    out = dict(env)
    out.pop("TMUX_PANE", None)
    if raw_tmux := out.pop("TMUX", "").strip():
        out.setdefault("AGENT_VOICE_INPUT_TMUX_HINT", raw_tmux)
    return out


def _cmd_start(args: argparse.Namespace, env: dict[str, str]) -> int:
    rt_dir = get_runtime_dir(env, create=True)
    try:
        if (st := send_control_command(rt_dir, {"cmd": "status"}, timeout_sec=1.0)).get("ok"):
            print(f"already running (pid={st.get('pid')}, state={st.get('state')})")
            return 0
    except Exception:
        pass
    src_root = Path(__file__).resolve().parents[1]
    child_env = _sanitize_daemon_tmux_env(os.environ)
    if (src_root / "agent_voice_input").is_dir():
        prev_pp = child_env.get("PYTHONPATH", "")
        child_env["PYTHONPATH"] = f"{src_root}{os.pathsep}{prev_pp}" if prev_pp else str(src_root)
    cmd = [
        sys.executable,
        "-m",
        "agent_voice_input.cli",
        "daemon",
        *(
            x
            for f, v in (
                ("--config", getattr(args, "config", None)),
                ("--asr-provider", getattr(args, "asr", None)),
                ("--asr-model", getattr(args, "asr_model", None)),
                ("--asr-api", getattr(args, "asr_api", None)),
                ("--asr-effort", getattr(args, "asr_effort", None)),
                ("--refiner-provider", getattr(args, "refiner", None)),
                ("--refiner-model", getattr(args, "refiner_model", None)),
                ("--refiner-effort", getattr(args, "effort", None)),
            )
            if v
            for x in (f, v)
        ),
    ]
    log_path = rt_dir / "daemon.log"
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
            return 1
        try:
            if (st := send_control_command(rt_dir, {"cmd": "status"}, timeout_sec=0.5)).get("ok"):
                print(f"started (pid={st.get('pid')})")
                return 0
        except Exception:
            time.sleep(0.15)
    print(f"started process pid={proc.pid} (socket warming up)")
    return 0


def _cmd_stop(env: dict[str, str]) -> int:
    rt_dir = get_runtime_dir(env, create=False)
    pid_file = rt_dir / "daemon.pid"
    pid = int(pid_file.read_text(encoding="utf-8").strip()) if pid_file.is_file() else 0
    try:
        send_control_command(rt_dir, {"cmd": "shutdown"}, timeout_sec=1.5)
    except Exception:
        if pid > 0:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    for _ in range(20):
        try:
            if pid > 0:
                os.kill(pid, 0)
                time.sleep(0.1)
        except OSError:
            break
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
    for name in ("daemon", "start", "doctor", "stop", "status", "toggle"):
        _add_common_flags(sub.add_parser(name), is_sub=True)
    p_once = sub.add_parser("once")
    _add_common_flags(p_once, is_sub=True)
    p_once.add_argument("--file", "--wav", dest="wav_file", help="Input audio/WAV file instead of live mic")
    p_once.add_argument("--duration", type=float, default=4.0, help="Live capture seconds when --file is omitted")
    p_once.add_argument("--dry-run", action="store_true", help="Print Stage 1 & Stage 2 output without clipboard/paste")
    p_once.add_argument("--title", help="Optional window title override for session matching")
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
            return _cmd_start(args, env)
        if args.subcmd == "stop":
            return _cmd_stop(env)
        if args.subcmd in ("status", "toggle"):
            try:
                resp = send_control_command(get_runtime_dir(env, create=False), {"cmd": args.subcmd})
                print(json.dumps(resp, ensure_ascii=False))
                return 0 if resp.get("ok") else 1
            except Exception:
                if args.subcmd == "status":
                    print(json.dumps({"ok": False, "state": "stopped"}))
                    return 1
                print("agent-voice-input daemon is not running (run 'agent-voice-input start' first)", file=sys.stderr)
                return 1
        if args.subcmd == "once":
            return _cmd_once(args, cfg, env)
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
