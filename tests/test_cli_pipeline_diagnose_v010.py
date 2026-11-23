"""Offline unit tests for v0.10.0 batch 1: start/status/drift, explain_pipeline_selection, aliases, Doubao ping, and diagnose."""
from __future__ import annotations

import base64
import fcntl
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _REPO_ROOT / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from agent_voice_input import __version__, cli as cli_mod, http_transport as http_mod  # noqa: E402
from agent_voice_input.asr_providers import DoubaoAsrProvider, HttpRequest, HttpResponse  # noqa: E402
from agent_voice_input.audio_proc import pack_pcm16_mono_wav, save_failed_audio  # noqa: E402
from agent_voice_input.config import (  # noqa: E402
    CODEX_SUBSCRIPTION_BASE_URL,
    OPENAI_DEFAULT_BASE_URL,
    AsrConfig,
    Config,
    RefinerConfig,
    _read_codex_models_cache_slugs,
    detect_codex_local_auth_mode,
    explain_pipeline_selection,
    is_loopback_url,
    load_config,
    normalize_asr_provider,
    resolve_asr_api,
    resolve_codex_default_model,
    resolve_credentials,
    sanitize_base_url,
    select_asr_provider,
    select_refiner_provider,
)
from agent_voice_input.daemon import (  # noqa: E402
    VoiceInputDaemon,
    append_telemetry_record,
    compute_package_build_id,
    describe_daemon_drift,
    probe_daemon_status,
    send_control_command,
)
from agent_voice_input.diagnose import (  # noqa: E402
    collect_live_diagnostics,
    diagnose_record_flags,
    format_live_diagnostics_markdown,
    mask_session_key,
    probe_optional_legacy_codex_sqlite,
    read_redacted_log_tail,
    summarize_telemetry_records,
)
from agent_voice_input.refiner import NoopRefiner  # noqa: E402


def _fake_jwt(exp_offset_sec: int = 7200) -> str:
    hdr = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').decode("ascii").rstrip("=")
    body = (
        base64.urlsafe_b64encode(
            json.dumps(
                {
                    "exp": int(time.time()) + exp_offset_sec,
                    "https://api.openai.com/auth": {"chatgpt_account_id": "acct-test-v010"},
                }
            ).encode("utf-8")
        )
        .decode("ascii")
        .rstrip("=")
    )
    return f"{hdr}.{body}.sig"


class StartStatusDriftAndHelpTests(unittest.TestCase):
    def test_build_id_and_drift_detection_cover_version_and_code_changes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pkg = Path(td)
            (pkg / "a.py").write_text("X = 1\n", encoding="utf-8")
            (pkg / "ignore.txt").write_text("ignored\n", encoding="utf-8")
            bid1 = compute_package_build_id(pkg)
            self.assertEqual(len(bid1), 12)
            # Editing non-.py does not change fingerprint
            (pkg / "ignore.txt").write_text("changed\n", encoding="utf-8")
            self.assertEqual(compute_package_build_id(pkg), bid1)
            # Editing .py changes fingerprint deterministically
            (pkg / "a.py").write_text("X = 2\n", encoding="utf-8")
            bid2 = compute_package_build_id(pkg)
            self.assertNotEqual(bid1, bid2)
            # Editing bridge assets (mac_shim.swift / win_shim.ps1) also changes fingerprint
            (pkg / "mac_shim.swift").write_text("let v = 1\n", encoding="utf-8")
            bid3 = compute_package_build_id(pkg)
            self.assertNotEqual(bid2, bid3)
            (pkg / "win_shim.ps1").write_text("$v = 1\n", encoding="utf-8")
            bid4 = compute_package_build_id(pkg)
            self.assertNotEqual(bid3, bid4)

            # Drift matrix
            same = describe_daemon_drift(
                {"version": __version__, "build_id": bid1},
                current_version=__version__,
                current_build_id=bid1,
            )
            self.assertFalse(same["drifted"])
            self.assertFalse(same["missing_build_id"])
            self.assertEqual(same["reason"], "")

            # Same version omitting build_id -> not drifted, emits gentle note
            same_no_bid = describe_daemon_drift(
                {"version": __version__},
                current_version=__version__,
                current_build_id=bid1,
            )
            self.assertFalse(same_no_bid["drifted"])
            self.assertTrue(same_no_bid["missing_build_id"])
            self.assertIn("omits build_id", same_no_bid["note"])

            code_only = describe_daemon_drift(
                {"version": __version__, "build_id": bid1},
                current_version=__version__,
                current_build_id=bid2,
            )
            self.assertTrue(code_only["drifted"])
            self.assertIn("code changed", code_only["reason"])

            ver_only = describe_daemon_drift(
                {"version": "0.8.0"},
                current_version=__version__,
                current_build_id=bid2,
            )
            self.assertTrue(ver_only["drifted"])
            self.assertIn("version mismatch", ver_only["reason"])

            both = describe_daemon_drift(
                {"version": "0.8.0", "build_id": bid1},
                current_version=__version__,
                current_build_id=bid2,
            )
            self.assertTrue(both["drifted"])
            self.assertIn("version/code mismatch", both["reason"])

            legacy_mock = describe_daemon_drift(
                {"ok": True, "pid": 123, "state": "idle"},
                current_version=__version__,
                current_build_id=bid2,
            )
            self.assertFalse(legacy_mock["drifted"])

    def test_start_idempotent_drift_hint_status_unresponsive_and_doctor_drift(self) -> None:
        parser = cli_mod._build_parser()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rt = root / "rt"
            rt.mkdir(mode=0o700)
            env = {
                "HOME": str(root / "home"),
                "PATH": "",
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
                "DASHSCOPE_API_KEY": "sk-dash-unit-0001",
            }
            cfg = Config()

            # 1. `start` when already running with drifted code/version: returns 0, no Popen, warns restart + prints pipeline
            out_start = io.StringIO()
            with (
                mock.patch(
                    "agent_voice_input.daemon.send_control_command",
                    return_value={
                        "ok": True,
                        "pid": 5678,
                        "state": "idle",
                        "version": "0.8.0",
                        "build_id": "000000000000",
                    },
                ),
                mock.patch.object(cli_mod.subprocess, "Popen", side_effect=AssertionError("must not spawn")),
                mock.patch("sys.stdout", out_start),
            ):
                rc_start = cli_mod._cmd_start(parser.parse_args(["start"]), env, cfg=cfg)
            self.assertEqual(rc_start, 0)
            txt_start = out_start.getvalue()
            self.assertIn("already running (pid=5678, state=idle", txt_start)
            self.assertIn("agent-voice-input restart", txt_start)
            self.assertIn("[pipeline]", txt_start)
            self.assertIn("dashscope/qwen3-asr-flash", txt_start)

            # 1b. `start` with CLI overrides prefers running daemon's pipeline (does NOT show unapplied override!)
            out_ov = io.StringIO()
            with (
                mock.patch(
                    "agent_voice_input.daemon.send_control_command",
                    return_value={
                        "ok": True,
                        "pid": 5678,
                        "state": "idle",
                        "version": __version__,
                        "pipeline": {"summary": "ASR dashscope/qwen3-asr-flash (running)"},
                    },
                ),
                mock.patch("sys.stdout", out_ov),
            ):
                rc_ov = cli_mod._cmd_start(parser.parse_args(["start", "--asr", "groq"]), env, cfg=cfg)
            self.assertEqual(rc_ov, 0)
            self.assertIn("ASR dashscope/qwen3-asr-flash (running)", out_ov.getvalue())
            self.assertNotIn("groq", out_ov.getvalue())

            # 1c. `start` when daemon is running same version without build_id prints gentle INFO note
            out_nobid = io.StringIO()
            with (
                mock.patch(
                    "agent_voice_input.daemon.send_control_command",
                    return_value={"ok": True, "pid": 5678, "state": "idle", "version": __version__},
                ),
                mock.patch("sys.stdout", out_nobid),
            ):
                rc_nb = cli_mod._cmd_start(parser.parse_args(["start"]), env, cfg=cfg)
            self.assertEqual(rc_nb, 0)
            self.assertIn("omits build_id", out_nobid.getvalue())

            # 1d. Legacy daemon omitting `pipeline` but reporting `asr_provider`/`refiner_provider` MUST NOT synthesize contradicting local pipeline
            legacy_daem = {
                "ok": True,
                "pid": 5679,
                "state": "idle",
                "version": __version__,
                "asr_provider": "doubao",
                "refiner_provider": "off",
            }
            out_leg_start = io.StringIO()
            with (
                mock.patch("agent_voice_input.daemon.send_control_command", return_value=dict(legacy_daem)),
                mock.patch("sys.stdout", out_leg_start),
            ):
                rc_leg_start = cli_mod._cmd_start(parser.parse_args(["start"]), env, cfg=cfg)
            self.assertEqual(rc_leg_start, 0)
            self.assertIn("ASR doubao -> Refiner off (reported by running daemon)", out_leg_start.getvalue())
            self.assertNotIn("dashscope", out_leg_start.getvalue())

            out_leg_st = io.StringIO()
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("agent_voice_input.daemon.send_control_command", return_value=dict(legacy_daem)),
                mock.patch("sys.stdout", out_leg_st),
            ):
                rc_leg_st = cli_mod.main(["status"])
            self.assertEqual(rc_leg_st, 0)
            leg_st_obj = json.loads(out_leg_st.getvalue())
            self.assertEqual(leg_st_obj["refiner_provider"], "off")
            self.assertEqual(leg_st_obj["pipeline"]["refiner"]["provider"], "off")
            self.assertEqual(leg_st_obj["pipeline"]["asr"]["provider"], "doubao")

            # 1e. Unlocked control socket PermissionError -> state="error", `start` returns 1 (NOT 0 / already running!)
            out_perm, err_perm = io.StringIO(), io.StringIO()
            with (
                mock.patch(
                    "agent_voice_input.daemon.send_control_command",
                    side_effect=PermissionError("EACCES sk-dash-unit-0001"),
                ),
                mock.patch.object(cli_mod.subprocess, "Popen", side_effect=AssertionError("must not spawn")),
                mock.patch("sys.stdout", out_perm),
                mock.patch("sys.stderr", err_perm),
            ):
                rc_perm = cli_mod._cmd_start(parser.parse_args(["start"]), env, cfg=cfg)
            self.assertEqual(rc_perm, 1)
            self.assertEqual(out_perm.getvalue(), "")
            self.assertIn("cannot verify daemon state", err_perm.getvalue())
            self.assertNotIn("sk-dash-unit-0001", err_perm.getvalue())

            out_doc_perm = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch(
                    "agent_voice_input.daemon.send_control_command",
                    side_effect=PermissionError("EACCES"),
                ),
                mock.patch("sys.stdout", out_doc_perm),
            ):
                rc_doc_perm = cli_mod._cmd_doctor(cfg, env, as_json=True)
            self.assertEqual(rc_doc_perm, 1)
            self.assertEqual(json.loads(out_doc_perm.getvalue())["daemon"]["state"], "error")

            # 2. `status` when daemon.lock is actively held but control.sock is missing -> reports "unresponsive" (NOT "stopped"!)
            lock_path = rt / "daemon.lock"
            lfd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
            os.fchmod(lfd, 0o600)
            os.write(lfd, f"{os.getpid()}\n".encode("ascii"))
            fcntl.flock(lfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                # Idempotent `start` when daemon holds lock / socket unresponsive -> returns 0, NEVER spawns Popen!
                out_start_unresp = io.StringIO()
                with (
                    mock.patch.object(cli_mod.subprocess, "Popen", side_effect=AssertionError("must not spawn")),
                    mock.patch("sys.stdout", out_start_unresp),
                ):
                    rc_su = cli_mod._cmd_start(parser.parse_args(["start"]), env, cfg=cfg)
                self.assertEqual(rc_su, 0)
                self.assertIn("state=unresponsive", out_start_unresp.getvalue())
                self.assertIn("diagnose", out_start_unresp.getvalue())

                out_unresp = io.StringIO()
                with (
                    mock.patch.dict(os.environ, env, clear=True),
                    mock.patch("sys.stdout", out_unresp),
                ):
                    rc_unresp = cli_mod.main(["status"])
                self.assertEqual(rc_unresp, 1)
                st_unresp = json.loads(out_unresp.getvalue())
                self.assertFalse(st_unresp["ok"])
                self.assertEqual(st_unresp["state"], "unresponsive")
                self.assertEqual(st_unresp["pid"], os.getpid())
                self.assertIn("daemon.lock", st_unresp["error"])

                # `doctor` also reports WARN unresponsive (not stopped!)
                out_doc_unresp = io.StringIO()
                with (
                    mock.patch.object(cli_mod.sys, "platform", "linux"),
                    mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                    mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                    mock.patch("sys.stdout", out_doc_unresp),
                ):
                    cli_mod._cmd_doctor(cfg, env, as_json=True)
                doc_u = json.loads(out_doc_unresp.getvalue())
                self.assertEqual(doc_u["daemon"]["state"], "unresponsive")
                self.assertEqual(doc_u["daemon"]["pid"], os.getpid())
                self.assertTrue(any("unresponsive" in ln for ln in doc_u["lines"]))
            finally:
                fcntl.flock(lfd, fcntl.LOCK_UN)
                os.close(lfd)

            # 3. `status` & `doctor` when daemon is online with drifted build_id -> surfaces code_drift + pipeline
            drifted_resp = {
                "ok": True,
                "pid": 7777,
                "state": "idle",
                "version": __version__,
                "build_id": "deadbeef0000",
                "asr_provider": "dashscope",
                "refiner_provider": "dashscope",
            }
            out_st_drift = io.StringIO()
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("agent_voice_input.daemon.send_control_command", return_value=dict(drifted_resp)),
                mock.patch("sys.stdout", out_st_drift),
            ):
                rc_st_drift = cli_mod.main(["status"])
            self.assertEqual(rc_st_drift, 0)
            obj_drift = json.loads(out_st_drift.getvalue())
            self.assertTrue(obj_drift["code_drift"])
            self.assertIn("restart", obj_drift["drift_warning"])
            self.assertIn("pipeline", obj_drift)

            out_doc_drift = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("agent_voice_input.daemon.send_control_command", return_value=dict(drifted_resp)),
                mock.patch("sys.stdout", out_doc_drift),
            ):
                rc_doc_drift = cli_mod._cmd_doctor(cfg, env, as_json=True)
            self.assertEqual(rc_doc_drift, 0)
            doc_d = json.loads(out_doc_drift.getvalue())
            self.assertTrue(doc_d["daemon"]["code_drift"])
            self.assertEqual(doc_d["daemon"]["build_id"], "deadbeef0000")
            self.assertIn("pipeline", doc_d)
            self.assertTrue(any("agent-voice-input restart" in ln for ln in doc_d["lines"]))

            # 4. `probe_daemon_status` rejects EOF/malformed/incomplete responses as `unresponsive` (never `stopped`),
            #    and `send_control_command` handles >64 KiB responses cleanly (up to 1 MiB).
            sock_p = rt / "control.sock"
            sock_p.write_text("placeholder", encoding="utf-8")
            try:
                for bad_resp in ({}, {"ok": False, "error": "busy"}, {"ok": True}):
                    with mock.patch("agent_voice_input.daemon.send_control_command", return_value=bad_resp):
                        pr_bad = probe_daemon_status(rt, timeout_sec=0.5)
                    self.assertEqual(pr_bad["state"], "unresponsive")
                    self.assertFalse(pr_bad["ok"])
            finally:
                sock_p.unlink(missing_ok=True)

            # Real AF_UNIX socket: (a) abrupt EOF -> unresponsive; (b) 100 KiB valid JSON -> succeeds!
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(str(sock_p))
            srv.listen(4)
            srv_mode = ["eof"]

            def _serve_two() -> None:
                for _ in range(2):
                    conn, _ = srv.accept()
                    with conn:
                        conn.recv(4096)
                        if srv_mode[0] == "eof":
                            pass  # close immediately -> EOF
                        else:
                            big_payload = {"ok": True, "state": "idle", "pad": "x" * 90000}
                            conn.sendall((json.dumps(big_payload) + "\n").encode("utf-8"))

            th = threading.Thread(target=_serve_two, daemon=True)
            th.start()
            try:
                pr_eof = probe_daemon_status(rt, timeout_sec=1.0)
                self.assertEqual(pr_eof["state"], "unresponsive")
                srv_mode[0] = "big"
                pr_big = probe_daemon_status(rt, timeout_sec=2.0)
                self.assertTrue(pr_big["ok"])
                self.assertEqual(len(pr_big["pad"]), 90000)
            finally:
                th.join(timeout=2.0)
                srv.close()
                sock_p.unlink(missing_ok=True)

            # 5. Daemon freezes `active_pipeline` at startup even if auth.json disappears later
            iso_h = root / "iso_daemon_home"
            c_dir = iso_h / ".codex"
            c_dir.mkdir(parents=True, mode=0o700)
            auth_f = c_dir / "auth.json"
            auth_f.write_text(json.dumps({"tokens": {"access_token": _fake_jwt()}}), encoding="utf-8")
            env_d = {
                "HOME": str(iso_h),
                "PATH": "",
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
                "DASHSCOPE_API_KEY": "sk-dash-freeze-1111",
            }
            class _MinPlat:
                _shim_disabled = False
                last_shim_error = ""
                def close(self) -> None:
                    pass
            d_freeze = VoiceInputDaemon(
                Config(), env=env_d, runtime_dir=rt, platform=_MinPlat(), refiner=NoopRefiner()
            )
            try:
                auth_f.unlink()  # Delete auth.json after startup
                st_fr = d_freeze.handle_control({"cmd": "status"})
                self.assertEqual(st_fr["refiner_provider"], "codex")
                self.assertEqual(st_fr["pipeline"]["refiner"]["provider"], "codex")
            finally:
                d_freeze.close()

    def test_all_cli_subcommands_expose_help_summaries(self) -> None:
        parser = cli_mod._build_parser()
        top_help = parser.format_help()
        expected_cmds = (
            "init",
            "daemon",
            "start",
            "restart",
            "stop",
            "toggle",
            "doctor",
            "diagnose",
            "cancel",
            "status",
            "once",
            "retry",
            "key-test",
            "session-hook",
            "remote-ctx",
        )
        for cmd in expected_cmds:
            self.assertIn(cmd, top_help)
        # Inspect subparsers action directly to confirm every subcommand has non-empty help
        sub_actions = [a for a in parser._actions if hasattr(a, "_choices_actions")]
        self.assertEqual(len(sub_actions), 1)
        help_map = {ca.dest: (ca.help or "").strip() for ca in sub_actions[0]._choices_actions}
        for cmd in expected_cmds:
            self.assertTrue(help_map.get(cmd), f"subcommand {cmd!r} is missing help= text")


class PipelineExplainAliasesAndDoubaoPingTests(unittest.TestCase):
    def test_auto_prefers_zero_billing_codex_and_explain_covers_all_modes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            codex_dir = home / ".codex"
            codex_dir.mkdir(mode=0o700)
            (codex_dir / "auth.json").write_text(
                json.dumps({"tokens": {"access_token": _fake_jwt()}}),
                encoding="utf-8",
            )
            env_multi = {
                "HOME": str(home),
                "PATH": "",
                "DASHSCOPE_API_KEY": "sk-dash-1111",
                "DOUBAO_ASR_API_KEY": "db-key-2222",
                "DEEPSEEK_API_KEY": "sk-ds-3333",
                "OPENROUTER_API_KEY": "sk-or-4444",
            }
            cfg_auto = Config()
            sel = explain_pipeline_selection(cfg_auto, env=env_multi)
            self.assertEqual(sel["asr"]["provider"], "dashscope")
            self.assertTrue(sel["asr"]["ready"])
            self.assertEqual(sel["asr"]["alternatives"], ["doubao", "openrouter", "deepseek"])
            # Codex subscription MUST win over paid DEEPSEEK/OPENROUTER/DASHSCOPE keys
            self.assertEqual(detect_codex_local_auth_mode(env_multi), "chatgpt")
            self.assertEqual(sel["refiner"]["provider"], "codex")
            self.assertEqual(sel["refiner"]["auth_mode"], "chatgpt")
            self.assertEqual(sel["refiner"]["base_url"], CODEX_SUBSCRIPTION_BASE_URL)
            self.assertTrue(sel["refiner"]["ready"])
            self.assertIn("zero extra API billing", sel["refiner"]["reason"])
            self.assertEqual(sel["refiner"]["alternatives"], ["deepseek", "dashscope", "openrouter"])
            self.assertIn("alternatives:", sel["summary"])

            # Codex `auth_mode="api_key"` (paid OpenAI API) MUST NOT preempt auto priority or claim zero billing!
            (codex_dir / "auth.json").write_text(
                json.dumps(
                    {
                        "auth_mode": "api_key",
                        "OPENAI_API_KEY": "sk-codex-paid-key-1234",
                        "tokens": {"access_token": _fake_jwt()},  # stale tokens must not override auth_mode=api_key
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(detect_codex_local_auth_mode(env_multi), "api_key")
            sel_apikey_multi = explain_pipeline_selection(cfg_auto, env=env_multi)
            self.assertEqual(sel_apikey_multi["refiner"]["provider"], "deepseek")
            self.assertIn("codex", sel_apikey_multi["refiner"]["alternatives"])

            # When ONLY Codex auth.json API key is available -> auto falls back to codex with honest OpenAI API URL & billing
            env_codex_apikey_only = {"HOME": str(home), "PATH": "", "DOUBAO_ASR_API_KEY": "db-key-2222"}
            sel_apikey_only = explain_pipeline_selection(cfg_auto, env=env_codex_apikey_only)
            self.assertEqual(sel_apikey_only["refiner"]["provider"], "codex")
            self.assertEqual(sel_apikey_only["refiner"]["auth_mode"], "api_key")
            self.assertEqual(sel_apikey_only["refiner"]["base_url"], OPENAI_DEFAULT_BASE_URL)
            self.assertIn("OpenAI API billing", sel_apikey_only["refiner"]["reason"])
            self.assertNotIn("zero extra API billing", sel_apikey_only["refiner"]["reason"])

            sel_apikey_explicit = explain_pipeline_selection(
                Config(refiner=RefinerConfig(provider="codex")), env=env_codex_apikey_only
            )
            self.assertIn("API key billing", sel_apikey_explicit["refiner"]["reason"])

            # Empty api_key mode in auth.json -> returns "none" (does not fall through to stale tokens)
            (codex_dir / "auth.json").write_text(
                json.dumps({"auth_mode": "api_key", "tokens": {"access_token": _fake_jwt()}}),
                encoding="utf-8",
            )
            self.assertEqual(detect_codex_local_auth_mode({"HOME": str(home), "PATH": ""}), "none")

            # Refresh-token-only auth.json without auth_mode + OPENAI_API_KEY in env falls back to api_key in refiner.py,
            # so config.py MUST classify it as "api_key" (NOT "chatgpt") and NOT preempt DEEPSEEK_API_KEY!
            (codex_dir / "auth.json").write_text(
                json.dumps({"tokens": {"refresh_token": "rt-only-token"}}),
                encoding="utf-8",
            )
            env_rt_oai = {**env_multi, "OPENAI_API_KEY": "sk-oai-env-fallback-999"}
            self.assertEqual(detect_codex_local_auth_mode(env_rt_oai), "api_key")
            self.assertEqual(
                select_refiner_provider(cfg_auto, resolve_credentials(cfg_auto, env_rt_oai), env_rt_oai),
                "deepseek",
            )
            # Explicit auth_mode="chatgpt" + refresh_token keeps chatgpt subscription priority even with OPENAI_API_KEY in env
            (codex_dir / "auth.json").write_text(
                json.dumps({"auth_mode": "chatgpt", "tokens": {"refresh_token": "rt-only-token"}}),
                encoding="utf-8",
            )
            self.assertEqual(detect_codex_local_auth_mode(env_rt_oai), "chatgpt")
            self.assertEqual(
                select_refiner_provider(cfg_auto, resolve_credentials(cfg_auto, env_rt_oai), env_rt_oai),
                "codex",
            )

            # Keyring mode (unverified offline): MUST NOT preempt paid API keys, falls back to codex only when no API keys set
            (codex_dir / "auth.json").unlink()
            fake_cbin = home / "codex"
            fake_cbin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_cbin.chmod(0o755)
            (codex_dir / "config.toml").write_text('cli_auth_credentials_store = "keyring"\n', encoding="utf-8")
            env_keyring_multi = {**env_multi, "CODEX_BIN": str(fake_cbin)}
            self.assertEqual(detect_codex_local_auth_mode(env_keyring_multi), "keyring")
            sel_kr_multi = explain_pipeline_selection(cfg_auto, env=env_keyring_multi)
            self.assertEqual(sel_kr_multi["refiner"]["provider"], "deepseek")
            self.assertIn("codex", sel_kr_multi["refiner"]["alternatives"])

            env_keyring_only = {"HOME": str(home), "PATH": "", "CODEX_BIN": str(fake_cbin), "DOUBAO_ASR_API_KEY": "db-key-2222"}
            sel_kr = explain_pipeline_selection(cfg_auto, env=env_keyring_only)
            self.assertEqual(sel_kr["refiner"]["provider"], "codex")
            self.assertEqual(sel_kr["refiner"]["auth_mode"], "keyring")
            self.assertIn("unverified offline", sel_kr["refiner"]["reason"])
            self.assertNotIn("zero extra API billing", sel_kr["refiner"]["reason"])
            (codex_dir / "config.toml").unlink()
            # Restore chatgpt auth.json for subsequent assertions
            (codex_dir / "auth.json").write_text(
                json.dumps({"tokens": {"access_token": _fake_jwt()}}),
                encoding="utf-8",
            )

            # Single-hop Gemini bypasses Stage 2 and lists ready refiner alternatives
            cfg_gem = Config(asr=AsrConfig(provider="gemini"))
            sel_gem = explain_pipeline_selection(cfg_gem, env={**env_multi, "GEMINI_API_KEY": "dummy-gemini-key"})
            self.assertTrue(sel_gem["asr"]["single_hop"])
            self.assertTrue(sel_gem["refiner"]["bypassed"])
            self.assertEqual(sel_gem["refiner"]["api"], "off")
            self.assertIn("codex", sel_gem["refiner"]["alternatives"])
            self.assertIn("bypassed", sel_gem["summary"])

            # Explicit refiner.provider="off" & empty credentials auto fallback
            cfg_off = Config(refiner=RefinerConfig(provider="off"))
            sel_off = explain_pipeline_selection(cfg_off, env={"HOME": str(home / "empty"), "PATH": ""})
            self.assertFalse(sel_off["asr"]["ready"])
            self.assertIn("no ASR credentials", sel_off["asr"]["reason"])
            self.assertEqual(sel_off["refiner"]["reason"], "explicit refiner.provider=off")
            self.assertIn("Refiner off", sel_off["summary"])

            # Auto refiner degrades to off when no Stage 2 credentials exist
            sel_no_ref = explain_pipeline_selection(
                Config(),
                env={"HOME": str(home / "empty"), "PATH": "", "DOUBAO_ASR_API_KEY": "db-only-key"},
            )
            self.assertEqual(sel_no_ref["asr"]["provider"], "doubao")
            self.assertEqual(sel_no_ref["refiner"]["provider"], "off")
            self.assertIn("degraded to off", sel_no_ref["refiner"]["reason"])

            # Explicit hints (loopback openai-compatible & custom api_key_env)
            cfg_hints = Config(
                asr=AsrConfig(provider="auto", base_url="http://127.0.0.1:8000/v1"),
                refiner=RefinerConfig(
                    provider="auto",
                    base_url="http://localhost:8001/v1",
                    model="local-llm",
                ),
            )
            sel_hints = explain_pipeline_selection(cfg_hints, env={"HOME": str(home / "empty"), "PATH": ""})
            self.assertEqual(sel_hints["asr"]["provider"], "openai-compatible")
            self.assertTrue(sel_hints["asr"]["ready"])
            self.assertIn("inferred from explicit asr config", sel_hints["asr"]["reason"])
            self.assertEqual(sel_hints["refiner"]["provider"], "openai-compatible")
            self.assertTrue(sel_hints["refiner"]["ready"])
            self.assertIn("inferred from explicit refiner config", sel_hints["refiner"]["reason"])

            cfg_custom_env = Config(
                asr=AsrConfig(provider="dashscope", api_key_env="MY_ASR_KEY"),
                refiner=RefinerConfig(provider="deepseek", api_key_env="MY_REF_KEY"),
            )
            sel_cust = explain_pipeline_selection(
                cfg_custom_env,
                env={"HOME": str(home / "empty"), "PATH": "", "MY_ASR_KEY": "k1", "MY_REF_KEY": "k2"},
            )
            self.assertTrue(sel_cust["asr"]["ready"])
            self.assertTrue(sel_cust["refiner"]["ready"])
            self.assertEqual(sel_cust["asr"]["reason"], "explicit asr.provider=dashscope")
            self.assertEqual(sel_cust["refiner"]["reason"], "explicit refiner.provider=deepseek")

            # Auto priority paid refiner (no codex auth)
            sel_paid = explain_pipeline_selection(
                Config(),
                env={"HOME": str(home / "empty"), "PATH": "", "DEEPSEEK_API_KEY": "sk-ds-paid"},
            )
            self.assertEqual(sel_paid["refiner"]["provider"], "deepseek")
            self.assertIn("DEEPSEEK_API_KEY", sel_paid["refiner"]["reason"])

            # Unified `is_loopback_url` (shared with http_transport) and `sanitize_base_url` (strips userinfo & query)
            self.assertIs(http_mod._is_loopback_url, is_loopback_url)
            self.assertFalse(is_loopback_url(""))
            self.assertTrue(is_loopback_url("http://127.0.0.1:9000/v1"))
            self.assertTrue(is_loopback_url("http://[::1]:8000/v1"))
            self.assertFalse(is_loopback_url("http://127.0.0.2:9000/v1"))
            self.assertFalse(is_loopback_url("https://api.openai.com/v1"))

            self.assertEqual(
                sanitize_base_url("https://alice:topsecret@gw.example.com:8443/v1?api_key=leak#frag"),
                "https://gw.example.com:8443/v1",
            )
            self.assertEqual(sanitize_base_url("http://[::1]:8000/v1?tok=abc"), "http://[::1]:8000/v1")
            self.assertEqual(sanitize_base_url(""), "")
            self.assertEqual(sanitize_base_url("not-a-url"), "")
            cfg_secret_url = Config(
                asr=AsrConfig(
                    provider="openai-compatible",
                    base_url="https://u1:gwpass123@gw.example.test:8443/v1?token=qsleak",
                ),
                refiner=RefinerConfig(
                    provider="openai-compatible",
                    base_url="https://u2:refpass456@gw.example.test/v1?key=refqsleak",
                    model="m1",
                ),
            )
            sel_sec_url = explain_pipeline_selection(cfg_secret_url, env={"HOME": str(home / "empty"), "PATH": ""})
            self.assertEqual(sel_sec_url["asr"]["base_url"], "https://gw.example.test:8443/v1")
            self.assertEqual(sel_sec_url["refiner"]["base_url"], "https://gw.example.test/v1")
            dumped_sel = json.dumps(sel_sec_url)
            for leaked in ("gwpass123", "qsleak", "refpass456", "refqsleak"):
                self.assertNotIn(leaked, dumped_sel)
            self.assertEqual(_read_codex_models_cache_slugs({"CODEX_HOME": "~/.codex"}), [])
            (codex_dir / "models_cache.json").write_text("not-json", encoding="utf-8")
            self.assertEqual(_read_codex_models_cache_slugs({"HOME": str(home), "PATH": ""}), [])
            (codex_dir / "models_cache.json").write_text("[]", encoding="utf-8")
            self.assertEqual(_read_codex_models_cache_slugs({"HOME": str(home), "PATH": ""}), [])
            (codex_dir / "models_cache.json").write_text('{"models": [{"slug": "custom-mini-v1"}]}', encoding="utf-8")
            self.assertEqual(resolve_codex_default_model(Config(), {"HOME": str(home), "PATH": ""}), "custom-mini-v1")
            (codex_dir / "models_cache.json").write_text('{"models": [{"slug": "other-large-model"}]}', encoding="utf-8")
            self.assertEqual(resolve_codex_default_model(Config(), {"HOME": str(home), "PATH": ""}), "gpt-5.4-mini")
            (codex_dir / "models_cache.json").write_text("", encoding="utf-8")
            self.assertEqual(_read_codex_models_cache_slugs({"HOME": str(home), "PATH": ""}), [])

    def test_cross_stage_provider_aliases_normalize_cleanly(self) -> None:
        self.assertEqual(normalize_asr_provider("volcengine"), "doubao")
        self.assertEqual(normalize_asr_provider("volc"), "doubao")
        self.assertEqual(normalize_asr_provider("bailian"), "dashscope")
        self.assertEqual(AsrConfig(provider="volcengine").provider, "doubao")
        self.assertEqual(AsrConfig(provider="volc").effective_model(), "bigmodel")
        self.assertIn("bytedance.com", AsrConfig(provider="volcengine").effective_base_url())
        self.assertEqual(resolve_asr_api("volcengine", "bigmodel"), "doubao-native")

        ref_db = RefinerConfig(provider="doubao")
        self.assertEqual(ref_db.provider, "volcengine")
        self.assertEqual(ref_db.effective_api("ark"), "chat-completions")
        self.assertEqual(ref_db.effective_effort("doubao"), "auto")
        self.assertIn("ark.cn-beijing.volces.com", ref_db.effective_base_url("doubao"))
        self.assertEqual(ref_db.effective_model("ark"), ref_db.model)

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            cfg, env = load_config(
                env={
                    "HOME": str(home),
                    "DOUBAO_ASR_API_KEY": "db-asr-secret",
                    "ARK_API_KEY": "ark-ref-secret",
                },
                home_dir=home,
                cli_overrides={"asr.provider": "volcengine", "refiner.provider": "doubao"},
                load_default_env_files=False,
            )
            creds = resolve_credentials(cfg, env)
            self.assertEqual(select_asr_provider(cfg, creds), "doubao")
            self.assertEqual(select_refiner_provider(cfg, creds, env), "volcengine")
            self.assertEqual(creds.for_provider("doubao").value, "db-asr-secret")
            self.assertEqual(creds.for_provider("volcengine").value, "ark-ref-secret")

    def test_doubao_1_0_probe_ping_slot_fallback_auth_fail_and_redaction(self) -> None:
        secret_tok = "db_secret_ping_99999"
        env = {
            "HOME": "/tmp/iso-nonexistent",
            "DOUBAO_ASR_APP_ID": "db_app_88888",
            "DOUBAO_ASR_API_KEY": secret_tok,
        }
        cfg = Config(asr=AsrConfig(provider="doubao"))
        creds = resolve_credentials(cfg, env)

        # 1. 1.0 Turbo flash unavailable (45000030) -> SeedASR 2.0 unavailable (45000000) -> 1.0 standard submit OK (20000000)
        seen_slots: list[tuple[str, str]] = []

        def _fallback_tx(req: HttpRequest) -> HttpResponse:
            rid = req.headers.get("X-Api-Resource-Id", "")
            seen_slots.append((req.url.rsplit("/", 1)[-1], rid))
            if rid == "volc.bigasr.auc_turbo":
                return HttpResponse(200, {"X-Api-Status-Code": "45000030", "X-Api-Message": "resource not granted"}, b"{}")
            if rid == "volc.seedasr.auc":
                return HttpResponse(200, {"X-Api-Status-Code": "45000000", "X-Api-Message": "resource not allowed"}, b"{}")
            return HttpResponse(200, {"X-Api-Status-Code": "20000000", "X-Api-Message": "OK"}, b"{}")

        st_ok, _, msg_ok = DoubaoAsrProvider(cfg, creds, env=env, transport=_fallback_tx).probe_ping(timeout_sec=5.0)
        self.assertEqual(st_ok, "OK")
        self.assertIn("volc.bigasr.auc/submit", msg_ok)
        self.assertEqual(
            seen_slots,
            [
                ("flash", "volc.bigasr.auc_turbo"),
                ("submit", "volc.seedasr.auc"),
                ("submit", "volc.bigasr.auc"),
            ],
        )

        # 1b. doubao_mode="submit" with resource_id="auto" skips /recognize/flash even when both submit slots fail
        seen_sub_only: list[tuple[str, str]] = []
        cfg_auto_sub = Config(asr=AsrConfig(provider="doubao", resource_id="auto", doubao_mode="submit"))
        st_sub_auto, _, msg_sub_auto = DoubaoAsrProvider(
            cfg_auto_sub,
            creds,
            env=env,
            transport=lambda r: (
                seen_sub_only.append((r.url.rsplit("/", 1)[-1], r.headers.get("X-Api-Resource-Id", ""))),
                HttpResponse(200, {"X-Api-Status-Code": "45000030", "X-Api-Message": "not granted"}, b"{}"),
            )[1],
        ).probe_ping(timeout_sec=5.0)
        self.assertEqual(st_sub_auto, "FAIL")
        self.assertIn("no granted Doubao ASR slot", msg_sub_auto)
        self.assertEqual(
            seen_sub_only,
            [
                ("submit", "volc.seedasr.auc"),
                ("submit", "volc.bigasr.auc"),
            ],
        )

        # 2. HTTP 401 unauthorized fails immediately without cascading
        def _unauth_tx(_req: HttpRequest) -> HttpResponse:
            return HttpResponse(401, {}, f'{{"message":"bad key {secret_tok}"}}'.encode("utf-8"))

        st_401, _, msg_401 = DoubaoAsrProvider(cfg, creds, env=env, transport=_unauth_tx).probe_ping(timeout_sec=5.0)
        self.assertEqual(st_401, "FAIL")
        self.assertIn("HTTP 401", msg_401)
        self.assertNotIn(secret_tok, msg_401)

        # 3. All slots ungranted -> FAIL listing tried slots
        def _all_denied_tx(_req: HttpRequest) -> HttpResponse:
            return HttpResponse(200, {"X-Api-Status-Code": "45000030", "X-Api-Message": "not granted"}, b"{}")

        st_none, _, msg_none = DoubaoAsrProvider(cfg, creds, env=env, transport=_all_denied_tx).probe_ping(timeout_sec=5.0)
        self.assertEqual(st_none, "FAIL")
        self.assertIn("no granted Doubao ASR slot", msg_none)

        # 4. HTTP 503 MUST fail even if header/body carries 20000000; submit mode requires 20000000/20000003
        def _http_503_tx(_req: HttpRequest) -> HttpResponse:
            return HttpResponse(503, {"X-Api-Status-Code": "20000000"}, b'{"result":{"text":""}}')

        st_503, _, msg_503 = DoubaoAsrProvider(cfg, creds, env=env, transport=_http_503_tx).probe_ping(timeout_sec=5.0)
        self.assertEqual(st_503, "FAIL")
        self.assertIn("HTTP 503", msg_503)

        cfg_sub = Config(asr=AsrConfig(provider="doubao", resource_id="volc.bigasr.auc", doubao_mode="submit"))
        st_sub_bad, _, _ = DoubaoAsrProvider(
            cfg_sub,
            creds,
            env=env,
            transport=lambda _r: HttpResponse(200, {}, b'{"result":{}}'),
        ).probe_ping(timeout_sec=5.0)
        self.assertEqual(st_sub_bad, "FAIL")

        # 5. Network exception with secret is redacted
        def _err_tx(_req: HttpRequest) -> HttpResponse:
            raise RuntimeError(f"dial failed with {secret_tok}")

        st_err, _, msg_err = DoubaoAsrProvider(cfg, creds, env=env, transport=_err_tx).probe_ping(timeout_sec=5.0)
        self.assertEqual(st_err, "FAIL")
        self.assertNotIn(secret_tok, msg_err)
        self.assertIn("[REDACTED]", msg_err)


class OfficialDiagnoseSubcommandTests(unittest.TestCase):
    def test_cli_diagnose_markdown_json_stats_redaction_and_sqlite(self) -> None:
        secret_key = "sk-diag-super-secret-98765"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            rt = root / "rt"
            rt.mkdir(parents=True, mode=0o700)
            home.mkdir(parents=True, mode=0o700)
            cfg_path = root / "cfg.toml"
            cfg_path.write_text('[asr]\nprovider = "dashscope"\n[refiner]\nprovider = "deepseek"\n', encoding="utf-8")

            # Seed telemetry records covering normal, warm cache hit, cache miss, slow stages, fallback, and skipped/failed
            append_telemetry_record(
                rt,
                {
                    "ts": 1700001000.0,
                    "seq_id": 1,
                    "ok": True,
                    "session_key": "agy:private-session-1111",
                    "asr_provider": "dashscope",
                    "refiner_provider": "deepseek",
                    "sync_ms": 10,
                    "asr_ms": 400,
                    "refine_init_ms": 300,
                    "refine_ms": 600,
                    "deliver_ms": 20,
                    "total_ms": 1330,
                    "ctx_chunks": 2,
                    "ctx_chars": 12000,
                    "input_tokens": 5000,
                    "cached_input_tokens": 0,
                    "reasoning_tokens": 0,
                    "asr_chunk_secs": [1.5, 2.5],
                    "asr_duration_ms": 2340,
                    "asr_request_id": "req-ds-001",
                    "text": "PRIVATE_DICTATION_SHOULD_BE_DROPPED",
                    "transcript": "SECRET_SYNTHETIC_FIELD",
                },
            )
            append_telemetry_record(
                rt,
                {
                    "ts": 1700001020.0,
                    "seq_id": 2,
                    "ok": True,
                    "session_key": "agy:private-session-1111",
                    "asr_provider": "dashscope",
                    "refiner_provider": "deepseek",
                    "asr_duration_ms": 1850,
                    "asr_request_id": f"req-{secret_key}",
                    "refiner_fallback": "asr_draft",
                    "refiner_warning": f"upstream failed token={secret_key}",
                    "sync_ms": 5,
                    "asr_ms": 2000,
                    "refine_init_ms": 0,
                    "refine_ms": 1700,
                    "deliver_ms": 400,
                    "total_ms": 4105,
                    "ctx_chunks": 2,
                    "ctx_chars": 12000,
                    "input_tokens": 5000,
                    "cached_input_tokens": 500,
                    "reasoning_tokens": 300,
                },
            )
            append_telemetry_record(
                rt,
                {
                    "ts": 1700001040.0,
                    "seq_id": 3,
                    "ok": False,
                    "skipped_reason": "asr_error",
                    "asr_request_id": "free text prose leaked from legacy telemetry",
                    "error": f"bad auth {secret_key}",
                    "saved_audio": str(rt / "failed" / "secret-user-path.wav"),
                },
            )
            # Stage a failed audio WAV + daemon.log with secret & oversized line + optional status.json + optional legacy sqlite
            save_failed_audio(pack_pcm16_mono_wav([100] * 1600, sample_rate=16000), rt, label="diag-unit")
            (rt / "daemon.log").write_text(
                f"startup ok\nwarn leaked={secret_key} {'A' * 320}\n", encoding="utf-8"
            )
            (rt / "status.json").write_text(json.dumps({"pid": 9999, "version": "0.9.1"}), encoding="utf-8")

            legacy_dir = rt / "codex-home"
            legacy_dir.mkdir(mode=0o700)
            db_path = legacy_dir / "logs_2.sqlite"
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute("CREATE TABLE logs (ts INTEGER, process_uuid TEXT, level TEXT)")
                now_ts = int(time.time())
                conn.executemany(
                    "INSERT INTO logs VALUES (?, ?, ?)",
                    [
                        (now_ts - 10, "p1", "WARN"),
                        (now_ts - 5, "p2", "ERROR"),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            env = {
                "HOME": str(home),
                "PATH": "",
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
                "DASHSCOPE_API_KEY": secret_key,
                "DEEPSEEK_API_KEY": "sk-ds-diag-key-1111",
            }

            # 1. JSON mode via official `agent-voice-input diagnose --last 5 --json`
            out_json = io.StringIO()
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("sys.stdout", out_json),
            ):
                rc_j = cli_mod.main(["--config", str(cfg_path), "diagnose", "--last", "5", "--json"])
            self.assertEqual(rc_j, 0)
            raw_j = out_json.getvalue()
            self.assertNotIn(secret_key, raw_j)
            self.assertNotIn("private-session-1111", raw_j)
            self.assertNotIn("secret-user-path.wav", raw_j)
            self.assertNotIn("PRIVATE_DICTATION_SHOULD_BE_DROPPED", raw_j)
            self.assertNotIn("SECRET_SYNTHETIC_FIELD", raw_j)
            data = json.loads(raw_j)
            self.assertEqual(data["recent_results"][0]["asr_chunk_secs"], [1.5, 2.5])
            self.assertEqual(data["recent_results"][0]["asr_duration_ms"], 2340)
            self.assertEqual(data["recent_results"][0]["asr_request_id"], "req-ds-001")
            self.assertEqual(data["recent_results"][1]["asr_duration_ms"], 1850)
            self.assertIn("[REDACTED]", data["recent_results"][1]["asr_request_id"])
            self.assertNotIn(secret_key, data["recent_results"][1]["asr_request_id"])
            self.assertNotIn("asr_request_id", data["recent_results"][2])
            self.assertNotIn("free text prose leaked", raw_j)
            from agent_voice_input.diagnose import _sanitize_telemetry_record
            san_valid = _sanitize_telemetry_record(
                {"asr_request_id": "req_123:abc/def.456-789"}, lambda s: s
            )
            self.assertEqual(san_valid["asr_request_id"], "req_123:abc/def.456-789")
            for bad_req_id in (
                "contains space",
                "line1\nline2",
                "用户口述隐私文本",
                "a" * 129,
                "req?token=1",
            ):
                with self.subTest(bad_asr_request_id=bad_req_id[:24]):
                    san_bad = _sanitize_telemetry_record({"asr_request_id": bad_req_id}, lambda s: s)
                    self.assertNotIn("asr_request_id", san_bad)
            self.assertTrue(all(len(ln) <= 241 for ln in data["daemon_log_tail"].splitlines()))
            self.assertEqual(data["summary"]["total"], 3)
            self.assertEqual(data["summary"]["ok_count"], 2)
            self.assertEqual(data["summary"]["failed_or_skipped"], 1)
            self.assertEqual(data["summary"]["fallback_count"], 1)
            self.assertGreater(data["summary"]["asr_ms_p50"], 0)
            self.assertEqual(data["storage"]["failed_audio_count"], 1)
            self.assertEqual(data["storage"]["legacy_codex_sqlite"]["recent_1h_processes"], 2)
            self.assertEqual(data["storage"]["legacy_codex_sqlite"]["recent_1h_warns"], 1)
            self.assertEqual(data["storage"]["legacy_codex_sqlite"]["recent_1h_errors"], 1)
            rec2_flags = data["recent_results"][1]["diag_flags"]
            for expected_flag in ("FALLBACK(asr_draft)", "ASR_SLOW", "REFINE_SLOW", "DELIVER_STALL", "CACHE_MISS", "REASONING_BLOAT"):
                self.assertIn(expected_flag, rec2_flags)
            self.assertTrue(data["recent_results"][2]["has_saved_audio"])
            self.assertIn("pipeline", data)

            # 2. Markdown mode via official `agent-voice-input diagnose --last 5`
            out_md = io.StringIO()
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("sys.stdout", out_md),
            ):
                rc_m = cli_mod.main(["--config", str(cfg_path), "diagnose", "--last", "5"])
            self.assertEqual(rc_m, 0)
            md_txt = out_md.getvalue()
            self.assertIn("Live Diagnostics", md_txt)
            self.assertIn("Recent Stats", md_txt)
            self.assertIn("Pipeline:", md_txt)
            self.assertIn("[REDACTED]", md_txt)
            self.assertNotIn(secret_key, md_txt)
            self.assertNotIn("private-session-1111", md_txt)

            # 3. Helper edge cases: mask_session_key, empty log tail, unresponsive/drifted markdown formatting
            self.assertEqual(mask_session_key(""), "none")
            self.assertTrue(mask_session_key("plain-id").startswith("sess#"))
            self.assertEqual(read_redacted_log_tail(rt / "missing.log", 0, env), "")
            self.assertEqual(summarize_telemetry_records([])["total"], 0)

            md_unresp = format_live_diagnostics_markdown(
                {
                    **data,
                    "daemon": {
                        **data["daemon"],
                        "online": False,
                        "state": "unresponsive",
                        "error": "control socket timed out",
                    },
                    "recent_results": [],
                }
            )
            self.assertIn("UNRESPONSIVE", md_unresp)
            self.assertIn("No utterance telemetry recorded yet", md_unresp)

            md_drift = format_live_diagnostics_markdown(
                {
                    **data,
                    "daemon": {
                        **data["daemon"],
                        "online": True,
                        "state": "idle",
                        "code_drift": True,
                        "drift_reason": "code changed (running=aaa, current=bbb)",
                    },
                }
            )
            self.assertIn("ONLINE", md_drift)
            self.assertIn("Version/Code Drift", md_drift)

            # Unreadable / empty legacy sqlite handling
            db_path.write_text("corrupt", encoding="utf-8")
            self.assertFalse(probe_optional_legacy_codex_sqlite(rt)["readable"])
            db_path.unlink()
            conn2 = sqlite3.connect(str(db_path))
            try:
                conn2.execute("CREATE TABLE other (id INTEGER)")
                conn2.commit()
            finally:
                conn2.close()
            self.assertFalse(probe_optional_legacy_codex_sqlite(rt)["has_logs_table"])

    def test_probe_stage_ping_invalid_ipv6_url_returns_fail_and_remote_hook_path_fallback(self) -> None:
        import pty
        import select
        import subprocess
        from agent_voice_input.config import Config, resolve_credentials

        # 1. P2-ipv6-url: _probe_stage_ping catches ValueError from malformed IPv6 base_url cleanly
        secret = "ds_test_bbbb2222"
        cfg = Config()
        cfg.asr.provider = "deepseek"
        cfg.asr.base_url = f"https://[invalid-ipv6/v1?k={secret}"
        env = {"DEEPSEEK_API_KEY": secret}
        creds = resolve_credentials(cfg, env)
        st, _ms, msg = cli_mod._probe_stage_ping(
            stage="asr",
            provider="deepseek",
            cfg=cfg,
            creds=creds,
            env=env,
            base_url=cfg.asr.base_url,
            transport=lambda _r: self.fail("transport must not be called on invalid URL"),
            cache={},
        )
        self.assertEqual(st, "FAIL")
        self.assertTrue(msg.startswith("FAIL ("))
        self.assertNotIn(secret, msg)

        # 2. P2-remote-hook: direct execution (shebang #!/bin/sh, not via /bin/bash) works under stripped PATH=""
        hook_sh = Path(__file__).resolve().parents[1] / "integrations" / "remote" / "remote-title-hook.sh"
        self.assertTrue(hook_sh.read_text(encoding="utf-8").startswith("#!/bin/sh\n"))
        p_direct_bare = subprocess.run(
            [str(hook_sh), "agy"],
            input=b"{}",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": ""},
            timeout=5.0,
            check=False,
        )
        self.assertEqual(p_direct_bare.returncode, 0, msg=p_direct_bare.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(json.loads(p_direct_bare.stdout.decode("utf-8")), {})

        with tempfile.TemporaryDirectory() as td:
            proj = Path(td) / "demo-repo"
            proj.mkdir()
            master_fd, slave_fd = pty.openpty()
            try:
                slave_tty = os.ttyname(slave_fd)
                os.set_blocking(master_fd, False)
                p_agy = subprocess.run(
                    [str(hook_sh), "agy"],
                    input=json.dumps(
                        {"conversationId": "abcdef12-1111-4222-8333-444455556666", "workspacePaths": [str(proj)]}
                    ).encode("utf-8"),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env={
                        "HOME": td,
                        "PATH": "",
                        "AVI_TEST_TTY_PATH": slave_tty,
                        "AGENT_VOICE_INPUT_HOST_TAG": "srv1",
                        "TMUX_PANE": "",
                    },
                    timeout=5.0,
                    check=False,
                )
                self.assertEqual(p_agy.returncode, 0, msg=p_agy.stderr.decode("utf-8", errors="replace"))
                self.assertEqual(json.loads(p_agy.stdout.decode("utf-8")), {})
                rlist, _, _ = select.select([master_fd], [], [], 0.3)
                self.assertTrue(rlist, "remote-title-hook.sh must still find python3 and write OSC 2 under PATH=''")
                osc = os.read(master_fd, 4096).decode("utf-8")
                self.assertEqual(osc, "\033]2;agy · demo-repo [#abcdef12] [@srv1]\007")
            finally:
                os.close(master_fd)
                os.close(slave_fd)

        # Missing python3 fallback under POSIX /bin/sh & bash: agy/gemini emit {} and exit 0; claude/auto stay silent and exit 0
        bash_bin = "/bin/bash" if os.path.isfile("/bin/bash") else "/usr/bin/bash"
        wrapper = (
            'command() { if [ "${1:-}" = "-v" ] && [ "${2:-}" = "python3" ]; then return 1; fi; '
            'builtin command "$@"; }; '
            'source "$1" "$2"'
        )
        for mode, want_stdout in (("agy", "{}\n"), ("gemini", "{}\n"), ("claude", ""), ("auto", "")):
            with self.subTest(missing_python3_mode=mode):
                p_miss = subprocess.run(
                    [bash_bin, "-c", wrapper, "_", str(hook_sh), mode],
                    input=b"{}",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env={"HOME": "/nonexistent", "PATH": ""},
                    timeout=5.0,
                    check=False,
                )
                self.assertEqual(p_miss.returncode, 0, msg=p_miss.stderr.decode("utf-8", errors="replace"))
                self.assertEqual(p_miss.stdout.decode("utf-8"), want_stdout)


if __name__ == "__main__":
    unittest.main()
