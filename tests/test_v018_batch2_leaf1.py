"""Offline unit tests for v0.18.0 Batch 2 Leaf 1 (D4, D5, M3, M4, B2)."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import signal
import tempfile
import threading
import unittest
from unittest import mock

from agent_voice_input import asr_providers
from agent_voice_input import cli
from agent_voice_input import control_ipc
from agent_voice_input.asr_providers import (
    AsrContext,
    AsrError,
    AudioTranscriptionsAsrProvider,
    ChatAudioAsrProvider,
    DashScopeAsrProvider,
    DoubaoAsrProvider,
    GeminiAudioProvider,
)
from agent_voice_input.audio_proc import pack_pcm16_mono_wav
from agent_voice_input.config import (
    Config,
    ConfigError,
    MAX_EXTRA_INSTRUCTIONS_CHARS,
    MAX_EXTRA_INSTRUCTIONS_ITEMS,
    RefinerConfig,
    _parse_simple_toml,
    get_runtime_dir,
    load_config,
    normalize_extra_instructions,
    parse_toml,
    resolve_credentials,
)
from agent_voice_input.daemon import VoiceInputDaemon, _QueuedUtterance
from agent_voice_input.daemon_pipeline import (
    _Stage1State,
    classify_skipped_commit_hud,
    finalize_commit_slot_outcome,
    run_pipeline_once,
)
from agent_voice_input.http_transport import (
    HttpRequest,
    HttpResponse,
    classify_http_error_category,
    format_http_error_summary,
)
from agent_voice_input.platform_common import ForegroundWindow, MicrophoneUnavailableError, PasteOutcome
from agent_voice_input.platform_macos import classify_macos_mic_permission
from agent_voice_input.prompts import (
    EXTRA_INSTRUCTIONS_HEADER,
    REFINER_GENERAL_SYSTEM_PROMPT,
    REFINER_SYSTEM_PROMPT,
    SINGLE_HOP_GENERAL_SYSTEM_PROMPT,
    SINGLE_HOP_SYSTEM_PROMPT,
    append_extra_instructions,
)
from agent_voice_input.refiner import (
    AgyRefiner,
    CodexRefiner,
    resolve_refiner_prompt,
)
from agent_voice_input.session_ctx import SessionSnapshot, SessionState, SessionTracker


class _DummyPlatform:
    name = "macos"
    last_audio_error = ""

    def __init__(self, *, fail_start: Exception | None = None) -> None:
        self._fail_start = fail_start
        self.hud_calls: list[tuple[str, str]] = []

    def is_allowed_terminal(self, window: ForegroundWindow) -> bool:
        return True

    def get_foreground_window(self) -> ForegroundWindow:
        return ForegroundWindow(1, "Ghostty", "codex")

    def start_recording(self, runtime_dir: Path, *, loopback: bool = False, seq_id: int | None = None) -> None:
        if self._fail_start is not None:
            raise self._fail_start

    def stop_recording(self) -> tuple[bytes, str]:
        return b"", "avfoundation"

    def cancel_recording(self) -> None:
        return None

    def paste_text(self, text: str, origin_window: ForegroundWindow, *, no_paste: bool = False) -> PasteOutcome:
        raise AssertionError("paste_text should not be called")

    def hud(self, state: str, text: str = "", duration_ms: int = 0, **kwargs: object) -> None:
        self.hud_calls.append((state, text))

    def beep(self, kind: str) -> None:
        return None

    def close(self) -> None:
        return None

    def set_recorder_lost_callback(self, cb: object) -> None:
        return None

    def set_recorder_recovered_callback(self, cb: object) -> None:
        return None

    def set_power_resume_callback(self, cb: object) -> None:
        return None


class TestD4ExtraInstructions(unittest.TestCase):
    def test_normalization_limits_and_migration_errors(self) -> None:
        self.assertEqual(normalize_extra_instructions(None), ())
        self.assertEqual(normalize_extra_instructions(""), ())
        self.assertEqual(normalize_extra_instructions("  \n\t "), ())
        self.assertEqual(normalize_extra_instructions([]), ())

        self.assertEqual(
            normalize_extra_instructions("  - Keep acronyms in English  "),
            ("Keep acronyms in English",),
        )
        self.assertEqual(
            normalize_extra_instructions(" - First item\n\n* Second item\n• Third item\n   "),
            ("First item", "Second item", "Third item"),
        )
        self.assertEqual(
            normalize_extra_instructions(["- One", "•  Two ", "  ", "* Three"]),
            ("One", "Two", "Three"),
        )

        exact_20 = [f"rule-{i}" for i in range(MAX_EXTRA_INSTRUCTIONS_ITEMS)]
        self.assertEqual(len(normalize_extra_instructions(exact_20)), MAX_EXTRA_INSTRUCTIONS_ITEMS)
        exact_chars = ["a" * 100 for _ in range(MAX_EXTRA_INSTRUCTIONS_CHARS // 100)]
        self.assertEqual(sum(len(x) for x in normalize_extra_instructions(exact_chars)), MAX_EXTRA_INSTRUCTIONS_CHARS)

        with self.assertRaises(ConfigError):
            normalize_extra_instructions([f"r{i}" for i in range(MAX_EXTRA_INSTRUCTIONS_ITEMS + 1)])
        with self.assertRaises(ConfigError):
            normalize_extra_instructions(["x" * (MAX_EXTRA_INSTRUCTIONS_CHARS + 1)])
        for bad in (123, True, {"a": "b"}, ["ok", 42], ["ok", None]):
            with self.assertRaises(ConfigError):
                normalize_extra_instructions(bad)

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            cfg_path = home / "config.toml"

            toml_arr = '[refiner]\nextra_instructions = [\n  "- Keep API names",\n  "• Output Simplified Chinese",\n]\n'
            self.assertEqual(parse_toml(toml_arr), _parse_simple_toml(toml_arr))
            cfg_path.write_text(toml_arr, encoding="utf-8")
            cfg_a, _ = load_config(config_path=cfg_path, env={"HOME": str(home)}, home_dir=home)
            self.assertEqual(cfg_a.refiner.extra_instructions, ("Keep API names", "Output Simplified Chinese"))

            toml_single = '[refiner]\nextra_instructions = "Keep English identifiers"\n'
            cfg_path.write_text(toml_single, encoding="utf-8")
            cfg_s, _ = load_config(config_path=cfg_path, env={"HOME": str(home)}, home_dir=home)
            self.assertEqual(cfg_s.refiner.extra_instructions, ("Keep English identifiers",))

            cfg_path.write_text('[refiner]\nprompt = "custom"\n', encoding="utf-8")
            with self.assertRaises(ConfigError) as cm_p:
                load_config(config_path=cfg_path, env={"HOME": str(home)}, home_dir=home)
            self.assertIn("refiner.prompt", str(cm_p.exception))
            self.assertIn("refiner.extra_instructions", str(cm_p.exception))

            cfg_path.write_text('[refiner]\nprovider = "off"\n', encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(
                    config_path=cfg_path,
                    env={"HOME": str(home)},
                    home_dir=home,
                    cli_overrides={"refiner.prompt": "legacy"},
                )

    def test_empty_is_byte_identical_and_non_empty_appends_across_two_stage_and_single_hop(self) -> None:
        self.assertEqual(append_extra_instructions(REFINER_SYSTEM_PROMPT, ()), REFINER_SYSTEM_PROMPT)
        self.assertEqual(append_extra_instructions(REFINER_GENERAL_SYSTEM_PROMPT, ()), REFINER_GENERAL_SYSTEM_PROMPT)
        self.assertEqual(append_extra_instructions(SINGLE_HOP_SYSTEM_PROMPT, ()), SINGLE_HOP_SYSTEM_PROMPT)
        self.assertEqual(
            append_extra_instructions(SINGLE_HOP_GENERAL_SYSTEM_PROMPT, ()),
            SINGLE_HOP_GENERAL_SYSTEM_PROMPT,
        )

        extras = ("Keep acronyms uppercase", "Use half-width code punctuation")
        expected_tail = f"\n\n{EXTRA_INSTRUCTIONS_HEADER}\n- {extras[0]}\n- {extras[1]}"
        self.assertEqual(
            resolve_refiner_prompt(Config(refiner=RefinerConfig(extra_instructions=extras)), mode="agent"),
            REFINER_SYSTEM_PROMPT + expected_tail,
        )
        self.assertEqual(
            resolve_refiner_prompt(Config(refiner=RefinerConfig(extra_instructions=extras)), mode="general"),
            REFINER_GENERAL_SYSTEM_PROMPT + expected_tail,
        )

        # Single-hop GeminiAudioProvider: empty vs non-empty (agent & general)
        wav = pack_pcm16_mono_wav([2500, -2500] * 4000)
        gem_env = {"GEMINI_API_KEY": "gem-test-" + ("a" * 16)}
        seen_gem_reqs: list[HttpRequest] = []

        def _fake_gem_tx(req: HttpRequest) -> HttpResponse:
            seen_gem_reqs.append(req)
            return HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps({"candidates": [{"content": {"parts": [{"text": "clean"}]}}]}).encode("utf-8"),
            )

        cfg_empty = Config()
        cfg_empty.asr.provider = "gemini"
        p_gem_empty = GeminiAudioProvider(cfg_empty, resolve_credentials(cfg_empty, gem_env), env=gem_env, transport=_fake_gem_tx)
        p_gem_empty.transcribe(wav, AsrContext(mode="agent"))
        p_gem_empty.transcribe(wav, AsrContext(mode="general"))
        self.assertEqual(
            json.loads(seen_gem_reqs[0].body.decode("utf-8"))["systemInstruction"]["parts"][0]["text"],
            SINGLE_HOP_SYSTEM_PROMPT,
        )
        self.assertEqual(
            json.loads(seen_gem_reqs[1].body.decode("utf-8"))["systemInstruction"]["parts"][0]["text"],
            SINGLE_HOP_GENERAL_SYSTEM_PROMPT,
        )

        cfg_ext = Config(refiner=RefinerConfig(extra_instructions=extras))
        cfg_ext.asr.provider = "gemini"
        p_gem_ext = GeminiAudioProvider(cfg_ext, resolve_credentials(cfg_ext, gem_env), env=gem_env, transport=_fake_gem_tx)
        p_gem_ext.transcribe(wav, AsrContext(mode="agent"))
        p_gem_ext.transcribe(wav, AsrContext(mode="general"))
        self.assertEqual(
            json.loads(seen_gem_reqs[2].body.decode("utf-8"))["systemInstruction"]["parts"][0]["text"],
            SINGLE_HOP_SYSTEM_PROMPT + expected_tail,
        )
        self.assertEqual(
            json.loads(seen_gem_reqs[3].body.decode("utf-8"))["systemInstruction"]["parts"][0]["text"],
            SINGLE_HOP_GENERAL_SYSTEM_PROMPT + expected_tail,
        )

        # Single-hop ChatAudioAsrProvider (OpenRouter audio chat): empty vs non-empty
        or_env = {"OPENROUTER_API_KEY": "sk-or-v1-" + ("b" * 16)}
        seen_chat_reqs: list[HttpRequest] = []

        def _fake_chat_tx(req: HttpRequest) -> HttpResponse:
            seen_chat_reqs.append(req)
            return HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps({"choices": [{"message": {"content": "clean chat"}}]}).encode("utf-8"),
            )

        cfg_chat_empty = Config()
        cfg_chat_empty.asr.provider = "openrouter"
        p_chat_empty = ChatAudioAsrProvider(
            cfg_chat_empty,
            resolve_credentials(cfg_chat_empty, or_env),
            env=or_env,
            transport=_fake_chat_tx,
        )
        p_chat_empty.transcribe(wav, AsrContext(mode="agent"))
        self.assertEqual(
            json.loads(seen_chat_reqs[0].body.decode("utf-8"))["messages"][0]["content"],
            SINGLE_HOP_SYSTEM_PROMPT,
        )

        cfg_chat_ext = Config(refiner=RefinerConfig(extra_instructions=extras))
        cfg_chat_ext.asr.provider = "openrouter"
        p_chat_ext = ChatAudioAsrProvider(
            cfg_chat_ext,
            resolve_credentials(cfg_chat_ext, or_env),
            env=or_env,
            transport=_fake_chat_tx,
        )
        p_chat_ext.transcribe(wav, AsrContext(mode="general"))
        self.assertEqual(
            json.loads(seen_chat_reqs[1].body.decode("utf-8"))["messages"][0]["content"],
            SINGLE_HOP_GENERAL_SYSTEM_PROMPT + expected_tail,
        )

        # AgyRefiner passes extra_instructions in its system prompt
        with tempfile.TemporaryDirectory() as td:
            agy_seen_prompts: list[str] = []
            cfg_agy = Config(refiner=RefinerConfig(provider="agy", extra_instructions=extras))
            agy_ref = AgyRefiner(cfg_agy, env={"HOME": td}, runtime_dir=Path(td))
            try:
                def _fake_run_acp_session(**kw: object):
                    agy_seen_prompts.append(str(kw.get("prompt_text", "")))
                    return "agy cleaned", "conv-1", "flash", (10, 0, 5, 0), 12

                with mock.patch("agent_voice_input.refiner.find_agy_bin", return_value="/usr/bin/agy"):
                    with mock.patch.object(agy_ref, "_run_acp_session", side_effect=_fake_run_acp_session):
                        res_agy = agy_ref.refine("口述草稿", mode="agent")
                self.assertEqual(res_agy.text, "agy cleaned")
                self.assertEqual(len(agy_seen_prompts), 1)
                self.assertIn(REFINER_SYSTEM_PROMPT, agy_seen_prompts[0])
                self.assertIn(expected_tail, agy_seen_prompts[0])
            finally:
                agy_ref.close()

    def test_doctor_refiner_reports_count_never_prints_instruction_text(self) -> None:
        from agent_voice_input.config import explain_pipeline_selection

        secret_rule = "TOP_SECRET_USER_RULE_DO_NOT_PRINT_8842"
        env = {
            "DASHSCOPE_API_KEY": "sk-ds-" + ("a" * 16),
            "GEMINI_API_KEY": "gem-test-" + ("c" * 16),
        }
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            cfg_two = Config(refiner=RefinerConfig(provider="gemini", extra_instructions=(secret_rule, "Second rule")))
            cfg_two.asr.provider = "dashscope"
            creds_two = resolve_credentials(cfg_two, env)
            st_two = cli._DoctorState(
                cfg=cfg_two,
                creds=creds_two,
                env=env,
                rt_dir=rt,
                as_json=True,
                all_secrets=tuple(creds_two.all_secret_values()),
                eff_asr="dashscope",
                asr_is_single_hop=False,
                pipeline=explain_pipeline_selection(cfg_two, creds_two, env),
            )
            cli._doctor_check_refiner(st_two)
            out = "\n".join(st_two.lines_out)
            self.assertIn("extra_instructions=2", out)
            self.assertNotIn(secret_rule, out)
            self.assertNotIn("Second rule", out)

            # Single-hop bypassed line also prints count only and never user text
            cfg_one = Config(refiner=RefinerConfig(provider="gemini", extra_instructions=(secret_rule, "Second rule")))
            cfg_one.asr.provider = "gemini"
            creds_one = resolve_credentials(cfg_one, env)
            st_one = cli._DoctorState(
                cfg=cfg_one,
                creds=creds_one,
                env=env,
                rt_dir=rt,
                as_json=True,
                all_secrets=tuple(creds_one.all_secret_values()),
                eff_asr="gemini",
                asr_is_single_hop=True,
                pipeline=explain_pipeline_selection(cfg_one, creds_one, env),
            )
            cli._doctor_check_refiner(st_one)
            out2 = "\n".join(st_one.lines_out)
            self.assertIn("extra_instructions=2", out2)
            self.assertNotIn(secret_rule, out2)


class TestD5RemovedLegacyShells(unittest.TestCase):
    def test_wsl_source_winmm_rejected_with_migration_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            cfg_path = home / "config.toml"
            cfg_path.write_text('[audio]\nwsl_source = "winmm"\n', encoding="utf-8")
            with self.assertRaises(ConfigError) as cm_toml:
                load_config(config_path=cfg_path, env={"HOME": str(home)}, home_dir=home)
            self.assertIn("winmm", str(cm_toml.exception))
            self.assertIn("wasapi", str(cm_toml.exception))
            self.assertIn("auto", str(cm_toml.exception))

            json_path = home / "config.json"
            json_path.write_text(json.dumps({"audio": {"wsl_source": "winmm"}}), encoding="utf-8")
            with self.assertRaises(ConfigError) as cm_json:
                load_config(config_path=json_path, env={"HOME": str(home)}, home_dir=home)
            self.assertIn("wasapi", str(cm_json.exception))

            cfg_path.write_text('[audio]\nwsl_source = "auto"\n', encoding="utf-8")
            with self.assertRaises(ConfigError) as cm_cli:
                load_config(
                    config_path=cfg_path,
                    env={"HOME": str(home)},
                    home_dir=home,
                    cli_overrides={"audio.wsl_source": "winmm"},
                )
            self.assertIn("wasapi", str(cm_cli.exception))


class TestM3MacOsSilentAllZeroDetection(unittest.TestCase):
    def test_classify_macos_mic_permission_ssh_and_remote_session(self) -> None:
        err_remote = classify_macos_mic_permission(
            {"mic_auth": "not_determined", "has_graphic_session": True, "is_remote_session": True},
            env={},
        )
        self.assertIsInstance(err_remote, MicrophoneUnavailableError)
        self.assertEqual(err_remote.hud_state, "mic_permission")

        for ssh_var in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"):
            err_ssh = classify_macos_mic_permission(
                {"mic_auth": "not_determined", "has_graphic_session": True, "is_remote_session": False},
                env={ssh_var: "1"},
            )
            self.assertIsInstance(err_ssh, MicrophoneUnavailableError, msg=ssh_var)

        self.assertIsNone(
            classify_macos_mic_permission(
                {"mic_auth": "not_determined", "has_graphic_session": True, "is_remote_session": False},
                env={},
            )
        )
        self.assertIsNone(
            classify_macos_mic_permission(
                {"mic_auth": "authorized", "has_graphic_session": True, "is_remote_session": True},
                env={"SSH_CONNECTION": "1.2.3.4 1 5.6.7.8 22"},
            )
        )

    def test_avfoundation_all_zero_over_half_sec_triggers_mic_no_stream(self) -> None:
        cfg = Config(refiner=RefinerConfig(provider="off"))
        zero_long = pack_pcm16_mono_wav([0] * 12800)  # 0.8s at 16kHz
        zero_short = pack_pcm16_mono_wav([0] * 4800)  # 0.3s at 16kHz

        class _NeverCalledAsr(asr_providers.BaseAsrProvider):
            provider_name = "dummy"

            def __init__(self) -> None:
                pass

            def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None, **_kw: object):  # type: ignore[override]
                raise AssertionError("ASR should not be called on silent all-zero gate reject")

        res_long = run_pipeline_once(
            zero_long,
            cfg=cfg,
            asr_provider=_NeverCalledAsr(),
            audio_source="avfoundation",
            dry_run=True,
            env={},
        )
        self.assertFalse(res_long.ok)
        self.assertEqual(res_long.skipped_reason, "mic_no_stream")

        res_short = run_pipeline_once(
            zero_short,
            cfg=cfg,
            asr_provider=_NeverCalledAsr(),
            audio_source="avfoundation",
            dry_run=True,
            env={},
        )
        self.assertFalse(res_short.ok)
        self.assertEqual(res_short.skipped_reason, "silence")

        eff_sr, hud_st, hud_dur, beep_k = classify_skipped_commit_hud(
            "mic_no_stream", recorder_lost=False, saved_audio=""
        )
        self.assertEqual((eff_sr, hud_st, hud_dur, beep_k), ("mic_no_stream", "mic_no_stream", 0, "error"))

        task = _QueuedUtterance(
            seq_id=1,
            raw_wav=zero_long,
            audio_source="avfoundation",
            origin_win=ForegroundWindow(1, "Ghostty", "codex"),
            no_paste=False,
            silent_hud=False,
            recorder_lost=False,
            recovery_count=0,
            recovery_gap_ms=0,
            audio_error="",
            enqueued_ts=0.0,
        )
        st = _Stage1State(t_total0=0.0, queue_wait_ms=0, win=task.origin_win)
        st.skipped_reason = "mic_no_stream"
        captured_res: list[object] = []

        def _summarize(res):
            captured_res.append(res)
            return {"error": res.error, "skipped_reason": res.skipped_reason}

        out = finalize_commit_slot_outcome(
            task=task,
            st=st,
            slot_exc=None,
            stage2_ref_res=None,
            stop_evt=threading.Event(),
            platform=_DummyPlatform(),
            tracker=None,
            env={},
            asr_provider=_NeverCalledAsr(),
            refiner=None,
            redact_fn=lambda e: str(e),
            summarize_fn=_summarize,
            stash_fn=lambda: "",
            deliver_fn=lambda *a, **k: PasteOutcome(False, False, "noop"),
        )
        self.assertIsNotNone(out)
        assert out is not None
        summary, hud_state, _hud_dur, _beep = out
        self.assertEqual(hud_state, "mic_no_stream")
        self.assertIn("macOS", summary["error"])
        self.assertIn("all-zero", summary["error"])


class TestM4DaemonControlAndRuntimeDir(unittest.TestCase):
    def test_get_runtime_dir_rejects_relative_override_and_ignores_relative_xdg(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            with self.assertRaises(ConfigError) as cm_rel:
                get_runtime_dir(env={"HOME": str(home), "AGENT_VOICE_INPUT_RUNTIME_DIR": "rel/runtime"})
            self.assertIn("absolute", str(cm_rel.exception).lower())

            rt_rel_xdg = get_runtime_dir(
                env={"HOME": str(home), "XDG_RUNTIME_DIR": ".", "XDG_CACHE_HOME": "relative/cache"},
            )
            self.assertTrue(rt_rel_xdg.is_absolute())
            self.assertEqual(rt_rel_xdg, home / ".cache" / "agent-voice-input")

            abs_cache = home / "my-cache"
            rt_abs_cache = get_runtime_dir(
                env={"HOME": str(home), "XDG_RUNTIME_DIR": "rel", "XDG_CACHE_HOME": str(abs_cache)},
            )
            self.assertEqual(rt_abs_cache, abs_cache / "agent-voice-input")

    def test_stop_and_status_bypass_broken_config_while_restart_rejects_it(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            rt = home / "run"
            rt.mkdir()
            bad_cfg = home / "broken.toml"
            bad_cfg.write_text("[refiner\nbroken syntax ===", encoding="utf-8")
            env = {
                "HOME": str(home),
                "AGENT_VOICE_INPUT_CONFIG": str(bad_cfg),
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
            }
            with mock.patch.dict(os.environ, env, clear=True):
                buf_stop = io.StringIO()
                with mock.patch("sys.stdout", buf_stop):
                    rc_stop = cli.main(["stop"])
                self.assertEqual(rc_stop, 0)
                self.assertIn("not running", buf_stop.getvalue())

                buf_stat = io.StringIO()
                with mock.patch("sys.stdout", buf_stat):
                    rc_stat = cli.main(["status", "--json"])
                # status exits 1 when daemon is not running (not 2 = config error!)
                self.assertEqual(rc_stat, 1)
                payload = json.loads(buf_stat.getvalue())
                self.assertFalse(payload["ok"])

                buf_stat_h = io.StringIO()
                with mock.patch("sys.stdout", buf_stat_h):
                    rc_stat_h = cli.main(["status"])
                self.assertEqual(rc_stat_h, 1)
                self.assertIn("WARN", buf_stat_h.getvalue())

                buf_err = io.StringIO()
                with mock.patch("sys.stderr", buf_err):
                    rc_restart = cli.main(["restart"])
                self.assertEqual(rc_restart, 2)
                self.assertIn("config error", buf_err.getvalue())

    def test_cmd_stop_sigterm_escalation_pid_guard_and_nonzero_on_lock_held(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            env = {"HOME": td, "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)}
            (rt / "daemon.pid").write_text("424242\n", encoding="utf-8")

            # 1. IPC fails, confirmed daemon PID gets SIGTERM, then lock frees -> exit 0
            lock_fd_r, lock_fd_w = os.pipe()
            os.close(lock_fd_w)
            claim_calls = {"n": 0}

            def _claim_after_sigterm(_rt: Path, _dl: float) -> int | None:
                claim_calls["n"] += 1
                if claim_calls["n"] == 1:
                    return None
                return lock_fd_r

            killed: list[tuple[int, int]] = []
            buf = io.StringIO()
            with (
                mock.patch.object(control_ipc, "send_control_command", side_effect=OSError("refused")),
                mock.patch.object(control_ipc, "_inspect_daemon_lock_holder", return_value=(True, 424242)),
                mock.patch.object(cli, "_try_claim_daemon_lock", side_effect=_claim_after_sigterm),
                mock.patch.object(cli, "_probe_pid_liveness", side_effect=["alive", "dead", "dead", "dead"]),
                mock.patch.object(cli, "_is_avi_daemon_pid", return_value=True),
                mock.patch("os.kill", side_effect=lambda pid, sig: killed.append((pid, sig))),
                mock.patch("fcntl.flock", return_value=None),
                mock.patch("sys.stdout", buf),
            ):
                rc = cli._cmd_stop(env, graceful_timeout_sec=0.05, term_timeout_sec=0.05)
            self.assertEqual(rc, 0)
            self.assertEqual(killed, [(424242, signal.SIGTERM)])
            self.assertIn("stopped", buf.getvalue())

            # 2. Unrelated reused PID must NOT receive SIGTERM, and lock held -> exit 1 & never prints "stopped"
            (rt / "daemon.pid").write_text("515151\n", encoding="utf-8")
            killed_unrelated: list[tuple[int, int]] = []
            buf_out = io.StringIO()
            buf_err = io.StringIO()
            with (
                mock.patch.object(control_ipc, "send_control_command", side_effect=OSError("refused")),
                mock.patch.object(cli, "_try_claim_daemon_lock", return_value=None),
                mock.patch.object(cli, "_probe_pid_liveness", return_value="alive"),
                mock.patch.object(cli, "_is_avi_daemon_pid", return_value=False),
                mock.patch("os.kill", side_effect=lambda pid, sig: killed_unrelated.append((pid, sig))),
                mock.patch("sys.stdout", buf_out),
                mock.patch("sys.stderr", buf_err),
            ):
                rc_stuck = cli._cmd_stop(env, graceful_timeout_sec=0.01, term_timeout_sec=0.01)
            self.assertEqual(rc_stuck, 1)
            self.assertEqual(killed_unrelated, [])
            self.assertNotIn("stopped", buf_out.getvalue())
            self.assertTrue(buf_err.getvalue().strip())

            # 3. Even if shutdown IPC returned ok=True, lock remaining held -> exit 1 & never prints "stopped"
            (rt / "daemon.pid").write_text("616161\n", encoding="utf-8")
            buf_out3 = io.StringIO()
            buf_err3 = io.StringIO()
            with (
                mock.patch.object(control_ipc, "send_control_command", return_value={"ok": True}),
                mock.patch.object(cli, "_try_claim_daemon_lock", return_value=None),
                mock.patch.object(cli, "_probe_pid_liveness", return_value="alive"),
                mock.patch.object(cli, "_is_avi_daemon_pid", return_value=True),
                mock.patch("os.kill", return_value=None),
                mock.patch("sys.stdout", buf_out3),
                mock.patch("sys.stderr", buf_err3),
            ):
                rc_held = cli._cmd_stop(env, graceful_timeout_sec=0.01, term_timeout_sec=0.01)
            self.assertEqual(rc_held, 1)
            self.assertNotIn("stopped", buf_out3.getvalue())


class TestB2SanitizationDeadCodeAndCaptureStatus(unittest.TestCase):
    def test_http_error_category_and_prose_free_asr_and_refiner_errors(self) -> None:
        self.assertEqual(classify_http_error_category(401), "unauthorized")
        self.assertEqual(classify_http_error_category(402), "quota_exceeded")
        self.assertEqual(classify_http_error_category(404, error_code="model_not_found"), "model_not_found")
        self.assertEqual(classify_http_error_category(429), "rate_limited")
        self.assertEqual(classify_http_error_category(503), "server_error")
        self.assertEqual(format_http_error_summary(403, "forbidden"), "HTTP 403 (forbidden)")

        leak_marker = "USER_PRIVATE_DICTATION_ECHO_XYZ999"
        wav = pack_pcm16_mono_wav([2500, -2500] * 4000)

        gem_env = {"GEMINI_API_KEY": "gem-test-" + ("d" * 16)}
        cfg_g = Config()
        cfg_g.asr.provider = "gemini"
        p_g = GeminiAudioProvider(
            cfg_g,
            resolve_credentials(cfg_g, gem_env),
            env=gem_env,
            transport=lambda _r: HttpResponse(400, {}, f'{{"error":{{"message":"bad {leak_marker}"}}}}'.encode()),
        )
        with self.assertRaises(AsrError) as cm_g:
            p_g.transcribe(wav)
        self.assertIn("HTTP 400 (bad_request)", str(cm_g.exception))
        self.assertNotIn(leak_marker, str(cm_g.exception))

        ds_env = {"DASHSCOPE_API_KEY": "sk-ds-" + ("e" * 16)}
        cfg_d = Config()
        cfg_d.asr.provider = "dashscope"
        p_d = DashScopeAsrProvider(
            cfg_d,
            resolve_credentials(cfg_d, ds_env),
            env=ds_env,
            transport=lambda _r: HttpResponse(401, {}, f'{{"message":"{leak_marker}"}}'.encode()),
        )
        with self.assertRaises(AsrError) as cm_d:
            p_d.transcribe(wav)
        self.assertIn("HTTP 401 (unauthorized)", str(cm_d.exception))
        self.assertNotIn(leak_marker, str(cm_d.exception))

        db_env = {"DOUBAO_SPEECH_API_KEY": "db-key-" + ("f" * 16)}
        cfg_b = Config()
        cfg_b.asr.provider = "doubao"
        p_b = DoubaoAsrProvider(
            cfg_b,
            resolve_credentials(cfg_b, db_env),
            env=db_env,
            transport=lambda _r: HttpResponse(403, {"x-api-status-code": "45000010"}, leak_marker.encode()),
        )
        with self.assertRaises(AsrError) as cm_b:
            p_b.transcribe(wav)
        self.assertIn("HTTP 403 (forbidden)", str(cm_b.exception))
        self.assertIn("code=45000010", str(cm_b.exception))
        self.assertNotIn(leak_marker, str(cm_b.exception))

        oai_env = {"OPENAI_API_KEY": "sk-oai-" + ("g" * 16)}
        cfg_o = Config()
        cfg_o.asr.provider = "openai"
        p_o = asr_providers.create_asr_provider(
            cfg_o,
            resolve_credentials(cfg_o, oai_env),
            env=oai_env,
            transport=lambda _r: HttpResponse(404, {}, f'{{"error":{{"code":"model_not_found","message":"{leak_marker}"}}}}'.encode()),
        )
        with self.assertRaises(AsrError) as cm_o:
            p_o.transcribe(wav)
        self.assertIn("HTTP 404 (model_not_found)", str(cm_o.exception))
        self.assertNotIn(leak_marker, str(cm_o.exception))

        # CodexRefiner model-unsupported fallback still triggers on 404 model_not_found while warning stays prose-free
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            auth_dir = tmp / ".codex"
            auth_dir.mkdir()
            (auth_dir / "auth.json").write_text(
                json.dumps({"tokens": {"access_token": "tok-" + ("x" * 24), "account_id": "acct-1"}}),
                encoding="utf-8",
            )
            cfg_cx = Config(refiner=RefinerConfig(provider="codex", model="", timeout_sec=3.0))
            seen_models: list[str] = []

            def _codex_tx(req: HttpRequest) -> HttpResponse:
                body_obj = json.loads(req.body.decode("utf-8"))
                m = str(body_obj.get("model") or "")
                seen_models.append(m)
                if len(seen_models) == 1:
                    return HttpResponse(
                        404,
                        {"content-type": "application/json"},
                        f'{{"detail":"The model {m} does not exist {leak_marker}"}}'.encode("utf-8"),
                    )
                sse = (
                    b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"fallback ok"}\n\n'
                    b'event: response.completed\ndata: {"type":"response.completed","response":{"status":"completed"}}\n\n'
                )
                return HttpResponse(200, {}, sse)

            cx = CodexRefiner(cfg_cx, env={"HOME": str(tmp)}, runtime_dir=tmp, transport=_codex_tx)
            try:
                r_fb = cx.refine("测试模型回退")
                self.assertEqual(r_fb.text, "fallback ok")
                self.assertEqual(len(seen_models), 2)
                self.assertNotEqual(seen_models[0], seen_models[1])
                self.assertNotIn(leak_marker, r_fb.warning)
            finally:
                cx.close()

    def test_dead_code_removed_and_capture_failure_records_delivery_status(self) -> None:
        self.assertFalse(hasattr(asr_providers, "_format_vocabulary_hints"))
        self.assertFalse(hasattr(SessionTracker, "get_luna_delta"))
        self.assertFalse(hasattr(SessionTracker, "mark_luna_synced"))
        self.assertNotIn("luna_synced_idx", SessionState.__dataclass_fields__)
        self.assertNotIn("luna_synced_idx", SessionSnapshot.__dataclass_fields__)

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            plat = _DummyPlatform(fail_start=MicrophoneUnavailableError("mic busy", hud_state="mic_unavailable"))
            cfg = Config(refiner=RefinerConfig(provider="off"))
            daemon = VoiceInputDaemon(cfg, env={"HOME": td}, runtime_dir=rt, platform=plat)
            try:
                resp = daemon.handle_trigger("toggle", ForegroundWindow(1, "Ghostty", "codex"))
                self.assertFalse(resp["ok"])
                diag_path = rt / "diag.jsonl"
                self.assertTrue(diag_path.is_file())
                rows = [json.loads(ln) for ln in diag_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
                self.assertGreaterEqual(len(rows), 1)
                self.assertEqual(rows[-1]["delivery_status"], "undelivered_unrecoverable")
                self.assertEqual(rows[-1]["skipped_reason"], "mic_unavailable")
            finally:
                daemon.stop()


if __name__ == "__main__":
    unittest.main()
