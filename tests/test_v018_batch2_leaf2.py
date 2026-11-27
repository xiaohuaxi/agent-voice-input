"""Offline unit tests for v0.18.0 Batch 2 Leaf 2 changes:
- diag.jsonl rename + legacy telemetry.jsonl read-only fallback
- M1: init --force preserves .env + WARN when flags ignored on existing config.toml
- B1: macOS Accessibility startup tip, 3 HUD states (hotkey_held / error_retry / queue_full),
  doctor [macos-aud] Fix placement + [macos-ax] + Stage 1 unsupported hotwords hint,
  diagnose dynamic ASR_SLOW / 8000ms REFINE_SLOW + agent/general cache split.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

import agent_voice_input.cli as cli_mod
from agent_voice_input.config import AsrConfig, Config, HotkeyConfig, RefinerConfig
from agent_voice_input.control_ipc import (
    DIAG_FILENAME,
    _LEGACY_DIAG_FILENAME,
    append_diag_record,
    read_diag_history,
)
from agent_voice_input.daemon_pipeline import (
    CancelToken,
    _Stage1State,
    build_pipeline_summary,
    finalize_commit_slot_outcome,
)
from agent_voice_input.diagnose import (
    collect_live_diagnostics,
    diagnose_record_flags,
    format_live_diagnostics_markdown,
    summarize_diag_records,
)
from agent_voice_input.hud import (
    HUD_STATE_DURATIONS_MS,
    format_copied_hud_text,
    format_hud_text,
)
from agent_voice_input.platform_common import ForegroundWindow


class TestDiagRenameAndFallback(unittest.TestCase):
    def test_append_writes_diag_jsonl_and_reads_with_legacy_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            legacy_p = rt / _LEGACY_DIAG_FILENAME
            diag_p = rt / DIAG_FILENAME

            # 1. Legacy telemetry.jsonl present, diag.jsonl absent -> read-only fallback works
            legacy_rec = {"ts": 1700000100.0, "seq_id": 1, "ok": True, "asr_ms": 900, "refine_ms": 400, "total_ms": 1300}
            legacy_p.write_text(json.dumps(legacy_rec) + "\n", encoding="utf-8")
            self.assertFalse(diag_p.exists())
            hist_legacy = read_diag_history(rt, limit=10)
            self.assertEqual(len(hist_legacy), 1)
            self.assertEqual(hist_legacy[0]["seq_id"], 1)
            self.assertFalse(diag_p.exists(), "read fallback must stay strictly read-only")

            # collect_live_diagnostics also reads legacy fallback and exposes diag_kb + diag.jsonl
            env = {"HOME": td, "XDG_RUNTIME_DIR": td}
            snap_legacy = collect_live_diagnostics(Config(), env, runtime_dir=rt, last=5)
            self.assertEqual(snap_legacy["summary"]["total"], 1)
            self.assertGreater(snap_legacy["storage"]["diag_kb"], 0.0)
            md_legacy = format_live_diagnostics_markdown(snap_legacy)
            self.assertIn("`diag.jsonl`", md_legacy)

            # 2. append_diag_record writes ONLY diag.jsonl, and diag.jsonl wins over legacy
            append_diag_record(
                rt,
                {"ts": 1700000200.0, "seq_id": 2, "ok": True, "asr_ms": 1000, "refine_ms": 500, "total_ms": 1500},
            )
            self.assertTrue(diag_p.is_file())
            hist_new = read_diag_history(rt, limit=10)
            self.assertEqual([r["seq_id"] for r in hist_new], [2])


class TestM1InitEnvProtectionAndIgnoredFlags(unittest.TestCase):
    def test_init_force_preserves_existing_env_and_warns_on_ignored_flags(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            env = {"HOME": str(home), "PATH": ""}
            cfg_dir = home / ".agent-voice-input"
            env_path = cfg_dir / ".env"
            toml_path = cfg_dir / "config.toml"

            # First init creates both files
            out1 = io.StringIO()
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", out1),
            ):
                self.assertEqual(cli_mod.main(["init"]), 0, out1.getvalue())
            self.assertTrue(toml_path.is_file())
            self.assertTrue(env_path.is_file())

            # Populate real secret into .env
            secret_content = "DASHSCOPE_API_KEY=sk-user-real-secret-key-999\n"
            env_path.write_text(secret_content, encoding="utf-8")

            # Re-run init WITHOUT --force with explicit flags -> keeps config.toml + prints WARN
            out_warn = io.StringIO()
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", out_warn),
            ):
                self.assertEqual(
                    cli_mod.main(["init", "--hotkey", "RAlt", "--asr", "bailian", "--refiner", "deepseek"]),
                    0,
                    out_warn.getvalue(),
                )
            warn_txt = out_warn.getvalue()
            self.assertIn("WARN", warn_txt)
            self.assertIn("--hotkey", warn_txt)
            self.assertIn("--asr", warn_txt)
            self.assertIn("--refiner", warn_txt)
            self.assertIn("--force", warn_txt)
            self.assertEqual(env_path.read_text(encoding="utf-8"), secret_content)

            # Re-run init WITH --force -> overwrites config.toml, MUST preserve .env!
            out_force = io.StringIO()
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", out_force),
            ):
                self.assertEqual(
                    cli_mod.main(["init", "--force", "--hotkey", "RAlt", "--asr", "bailian", "--refiner", "off"]),
                    0,
                    out_force.getvalue(),
                )
            force_txt = out_force.getvalue()
            self.assertIn('key = "RAlt"', toml_path.read_text(encoding="utf-8"))
            self.assertEqual(
                env_path.read_text(encoding="utf-8"),
                secret_content,
                "init --force must NEVER overwrite existing .env credentials",
            )
            self.assertIn("existing credentials preserved", force_txt)


class TestB1StartAxTipHudAndDoctor(unittest.TestCase):
    def test_macos_start_prints_accessibility_tip_when_not_trusted_and_non_terminals_scope(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            rt.mkdir()
            env = {"HOME": td, "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)}
            args = cli_mod._build_parser().parse_args(["start"])

            class _DummyProc:
                pid = 43210

                def poll(self) -> None:
                    return None

            for scope, ax_trusted, expect_tip in [
                ("all", False, True),
                ("all", True, False),
                ("terminals", False, False),
            ]:
                cfg = Config(hotkey=HotkeyConfig(key="RCommand", scope=scope))
                out = io.StringIO()
                err = io.StringIO()
                probe_calls = [0]

                def _fake_probe(_rt: Path, timeout_sec: float = 1.0) -> dict[str, object]:
                    del timeout_sec
                    probe_calls[0] += 1
                    if probe_calls[0] == 1:
                        return {"ok": False, "state": "stopped"}
                    return {"ok": True, "pid": 43210, "state": "idle", "hotkey_ready": True}

                with (
                    mock.patch.object(cli_mod.sys, "platform", "darwin"),
                    mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/opt/homebrew/bin/ffmpeg"),
                    mock.patch("agent_voice_input.control_ipc.probe_daemon_status", return_value={"ok": False, "state": "stopped"}),
                    mock.patch(
                        "agent_voice_input.control_ipc.send_control_command",
                        return_value={"ok": True, "pid": 43210, "state": "idle", "shim_ok": True},
                    ),
                    mock.patch("agent_voice_input.platform_macos.compile_mac_shim", return_value=("/tmp/mac_shim", "")),
                    mock.patch(
                        "agent_voice_input.platform_macos.run_mac_shim_self_check",
                        return_value=(True, {"ax_trusted": ax_trusted, "audio_status": {"mic_auth": "authorized"}}, ""),
                    ),
                    mock.patch("agent_voice_input.cli.subprocess.Popen", return_value=_DummyProc()),
                    mock.patch("sys.stdout", out),
                    mock.patch("sys.stderr", err),
                ):
                    rc = cli_mod._cmd_start(args, env, cfg=cfg)
                self.assertEqual(rc, 0, out.getvalue() + err.getvalue())
                if expect_tip:
                    self.assertIn("Privacy_Accessibility", err.getvalue())
                    self.assertIn("terminal starting the daemon", err.getvalue())
                else:
                    self.assertNotIn("Privacy_Accessibility", err.getvalue())

    def test_hud_copy_and_stage1_error_retry_state(self) -> None:
        # 1. HUD copy & durations for queue_full, error_retry, hotkey_held
        self.assertIn("队列已满", format_hud_text("queue_full", "zh"))
        self.assertIn("queue full", format_hud_text("queue_full", "en").lower())
        self.assertIn("可重试", format_hud_text("error_retry", "zh"))
        self.assertIn("retry", format_hud_text("error_retry", "en").lower())
        self.assertIn("按键按住", format_copied_hud_text("hotkey_held", "zh"))
        self.assertIn("held", format_copied_hud_text("hotkey_held", "en").lower())
        self.assertEqual(HUD_STATE_DURATIONS_MS["queue_full"], 2600)
        self.assertEqual(HUD_STATE_DURATIONS_MS["error_retry"], 3200)

        # 2. Stage 1 failure with stashed audio -> HUD state "error_retry"; without stashed audio -> "error"
        for saved_path, expected_hud in [("/tmp/failed/seq1.wav", "error_retry"), ("", "error")]:
            task = SimpleNamespace(
                seq_id=1,
                audio_source="test",
                cancel_token=CancelToken(),
                recorder_lost=False,
                no_paste=False,
            )
            st = _Stage1State(t_total0=0.0, win=ForegroundWindow())
            outcome = finalize_commit_slot_outcome(
                task=task,
                st=st,
                slot_exc=RuntimeError("asr_upstream_503"),
                stage2_ref_res=None,
                stop_evt=threading.Event(),
                platform=None,
                tracker=None,
                env={},
                asr_provider=SimpleNamespace(provider_name="dashscope"),
                refiner=SimpleNamespace(provider_name="off"),
                redact_fn=lambda exc: str(exc),
                summarize_fn=lambda res: build_pipeline_summary(Config(), res),
                stash_fn=lambda p=saved_path: p,
                deliver_fn=lambda *_a, **_kw: None,  # type: ignore[return-value]
            )
            self.assertIsNotNone(outcome)
            assert outcome is not None
            summary, hud_st, _dur, beep_k = outcome
            self.assertFalse(summary["ok"])
            self.assertEqual(hud_st, expected_hud)
            self.assertEqual(beep_k, "error")

    def test_doctor_macos_avfoundation_fix_placement_ax_copy_and_stage1_hotwords_note(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env = {
                "HOME": td,
                "XDG_RUNTIME_DIR": td,
                "OPENROUTER_API_KEY": "sk-or-test-key",
            }
            cfg = Config(
                asr=AsrConfig(provider="openrouter", model="google/gemini-2.5-flash"),
                refiner=RefinerConfig(provider="off"),
            )
            probe_fail_aud = {
                "avfoundation_ok": False,
                "avfoundation_error": "no input device",
                "pbcopy_ok": True,
                "osascript_ok": True,
                "mac_shim_ok": True,
                "mac_shim_bin": "/tmp/mac_shim",
                "caller_mic_auth": "authorized",
                "caller_has_graphic_session": True,
                "caller_is_remote_session": False,
                "ax_trusted": False,
                "fg_process": "Ghostty",
                "fg_title": "zsh",
                "permissions_hint": "Microphone -> Ghostty",
            }
            out = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "darwin"),
                mock.patch.object(cli_mod, "_is_macbook_laptop", return_value=False),
                mock.patch("agent_voice_input.platform_macos.probe_macos_host", return_value=probe_fail_aud),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/opt/homebrew/bin/ffmpeg"),
                mock.patch("sys.stdout", out),
            ):
                rc = cli_mod._cmd_doctor(cfg, env)
            self.assertEqual(rc, 1)
            lines = out.getvalue().splitlines()
            aud_idx = next(i for i, ln in enumerate(lines) if ln.startswith("[macos-aud]"))
            shim_idx = next(i for i, ln in enumerate(lines) if ln.startswith("[mac-shim]"))
            self.assertEqual(
                aud_idx + 1,
                next(i for i, ln in enumerate(lines) if "Sound -> Input" in ln),
                f"avfoundation Fix line must immediately follow [macos-aud]:\n{out.getvalue()}",
            )
            self.assertLess(aud_idx + 1, shim_idx)
            ax_line = next(ln for ln in lines if ln.startswith("[macos-ax]"))
            self.assertIn("window-title/session binding", ax_line)

            # Stage 1 unsupported hotwords hint (OpenRouter non-keyterms STT route has max_terms=0)
            cfg_no_s1_hw = Config(
                asr=AsrConfig(provider="openrouter", model="openai/whisper-large-v3", api="audio-transcriptions"),
                refiner=RefinerConfig(provider="off"),
            )
            out_hw = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.stdout", out_hw),
            ):
                cli_mod._cmd_doctor(cfg_no_s1_hw, env)
            self.assertIn("stage1_hotwords=unsupported (stage2 only)", out_hw.getvalue())


class TestB1DiagnoseThresholdsAndCacheSplit(unittest.TestCase):
    def test_dynamic_asr_slow_and_8000ms_refine_slow_thresholds(self) -> None:
        # 1. Two-stage: 10s audio -> threshold = max(4500, 1000) = 4500ms; 60s audio -> threshold = 6000ms
        self.assertNotIn(
            "ASR_SLOW",
            diagnose_record_flags(
                {"ok": True, "session_key": "claude:1", "raw_audio_sec": 10.0, "asr_ms": 4400, "refine_ms": 7900},
                is_first_for_session=True,
            ),
        )
        flags_4500 = diagnose_record_flags(
            {"ok": True, "session_key": "claude:1", "raw_audio_sec": 10.0, "asr_ms": 4500, "refine_ms": 8000},
            is_first_for_session=True,
        )
        self.assertIn("ASR_SLOW", flags_4500)
        self.assertIn("REFINE_SLOW", flags_4500)

        # Long 60s audio: 5500ms ASR is NOT slow (< 6000ms), 6000ms IS slow
        self.assertNotIn(
            "ASR_SLOW",
            diagnose_record_flags(
                {"ok": True, "session_key": "claude:1", "raw_audio_sec": 60.0, "asr_ms": 5500, "refine_ms": 1000},
                is_first_for_session=True,
            ),
        )
        self.assertIn(
            "ASR_SLOW",
            diagnose_record_flags(
                {"ok": True, "session_key": "claude:1", "raw_audio_sec": 60.0, "asr_ms": 6000, "refine_ms": 1000},
                is_first_for_session=True,
            ),
        )

        # 2. Single-hop: 60s audio -> threshold = max(6000, 7200) = 7200ms
        self.assertNotIn(
            "ASR_SLOW",
            diagnose_record_flags(
                {
                    "ok": True,
                    "session_key": "claude:1",
                    "is_single_hop_refined": True,
                    "raw_audio_sec": 60.0,
                    "asr_ms": 7100,
                    "refine_ms": 0,
                },
                is_first_for_session=True,
            ),
        )
        self.assertIn(
            "ASR_SLOW",
            diagnose_record_flags(
                {
                    "ok": True,
                    "session_key": "claude:1",
                    "is_single_hop_refined": True,
                    "raw_audio_sec": 60.0,
                    "asr_ms": 7200,
                    "refine_ms": 0,
                },
                is_first_for_session=True,
            ),
        )

    def test_summarize_and_markdown_split_agent_and_general_cache_hit_rates(self) -> None:
        records = [
            {
                "ok": True,
                "mode": "agent",
                "asr_ms": 1000,
                "refine_ms": 1200,
                "total_ms": 2200,
                "input_tokens": 10000,
                "cached_input_tokens": 9000,
                "diag_flags": [],
            },
            {
                "ok": True,
                "mode": "agent",
                "asr_ms": 1100,
                "refine_ms": 1300,
                "total_ms": 2400,
                "input_tokens": 10000,
                "cached_input_tokens": 7000,
                "diag_flags": [],
            },
            {
                "ok": True,
                "mode": "general",
                "asr_ms": 900,
                "refine_ms": 800,
                "total_ms": 1700,
                "input_tokens": 500,
                "cached_input_tokens": 0,
                "diag_flags": [],
            },
        ]
        stats = summarize_diag_records(records)
        self.assertEqual(stats["agent_cache_hit_pct_avg"], 80.0)
        self.assertEqual(stats["agent_cache_samples"], 2)
        self.assertEqual(stats["general_cache_hit_pct_avg"], 0.0)
        self.assertEqual(stats["general_cache_samples"], 1)

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            for idx, r in enumerate(records, start=1):
                append_diag_record(rt, {**r, "ts": 1700000000.0 + idx, "seq_id": idx})
            snap = collect_live_diagnostics(Config(), {"HOME": td, "XDG_RUNTIME_DIR": td}, runtime_dir=rt, last=10)
            self.assertEqual(snap["summary"]["agent_cache_hit_pct_avg"], 80.0)
            self.assertEqual(snap["summary"]["general_cache_hit_pct_avg"], 0.0)
            md = format_live_diagnostics_markdown(snap)
            self.assertIn("agent=`80.0%` (n=2)", md)
            self.assertIn("general=`0.0%` (n=1)", md)


if __name__ == "__main__":
    unittest.main()
