"""Unit tests for Batch 3A: [archive] contract, config finalization, MacBook hotkey default, and unified ISO ts."""
from __future__ import annotations

import contextlib
import datetime
import io
import json
import math
import os
from pathlib import Path
import stat
import subprocess
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

from agent_voice_input import cli as cli_mod  # noqa: E402
from agent_voice_input.asr_providers import AsrContext, AsrError, AsrResult, BaseAsrProvider  # noqa: E402
from agent_voice_input.audio_proc import (  # noqa: E402
    AudioError,
    append_transcript_record,
    archive_recording_audio,
    list_saved_recordings,
    pack_pcm16_mono_wav,
    prune_saved_recordings_dir,
    run_utterance_archive,
)
from agent_voice_input.config import (  # noqa: E402
    UTTERANCE_ID_RE,
    ArchiveConfig,
    AsrConfig,
    AudioConfig,
    Config,
    ConfigError,
    HotkeyConfig,
    RefinerConfig,
    _is_macbook_laptop,
    format_archive_status_dict,
    format_archive_summary,
    format_local_iso_ts,
    generate_utterance_id,
    load_config,
    parse_diag_ts,
    resolve_archive_audio_dir,
    resolve_archive_transcripts_dir,
)
from agent_voice_input.control_ipc import (  # noqa: E402
    append_diag_record,
    build_daemon_status_response,
    merge_recent_results,
    read_diag_history,
)
from agent_voice_input.daemon import VoiceInputDaemon  # noqa: E402
from agent_voice_input.daemon_pipeline import build_pipeline_summary, run_pipeline_once  # noqa: E402
from agent_voice_input.diagnose import (  # noqa: E402
    _fmt_time_short,
    _sanitize_diag_record,
    collect_live_diagnostics,
    format_live_diagnostics_markdown,
)
from agent_voice_input.platform_common import ForegroundWindow, PasteOutcome  # noqa: E402
from agent_voice_input.refiner import BaseRefiner, NoopRefiner, RefinerResult  # noqa: E402
from agent_voice_input.session_ctx import SessionTracker  # noqa: E402


def _voiced_wav(sec: float = 0.6) -> bytes:
    n = int(round(16000 * sec))
    return pack_pcm16_mono_wav([int(6000 * math.sin(2.0 * math.pi * 440.0 * i / 16000.0)) for i in range(n)])


def _silence_wav(sec: float = 0.6) -> bytes:
    return pack_pcm16_mono_wav([0] * int(round(16000 * sec)))


class _StubAsr(BaseAsrProvider):
    def __init__(
        self,
        text: str = "hello draft",
        *,
        provider: str = "dashscope",
        model: str = "qwen3-asr-flash",
        single_hop: bool = False,
        raise_exc: Exception | None = None,
    ) -> None:
        self.provider_name = provider
        self.model = model
        self.single_hop = single_hop
        self._text = text
        self._raise_exc = raise_exc

    def transcribe(
        self,
        wav_bytes: bytes,
        *,
        context: AsrContext | None = None,
        is_cancelled=None,
        cancel_token=None,
    ) -> AsrResult:
        _ = (wav_bytes, context, is_cancelled, cancel_token)
        if self._raise_exc is not None:
            raise self._raise_exc
        return AsrResult(
            text=self._text,
            provider=self.provider_name,
            model=self.model,
            latency_ms=42,
            is_single_hop_refined=self.single_hop,
        )


class _StubRefiner(BaseRefiner):
    def __init__(
        self,
        out_text: str = "Hello refined.",
        *,
        provider: str = "codex",
        model: str = "gpt-5.4-mini",
        used_fallback: str = "",
    ) -> None:
        self.provider_name = provider
        self._out_text = out_text
        self._model = model
        self._used_fallback = used_fallback

    def refine(self, asr_input, **kwargs) -> RefinerResult:
        _ = (asr_input, kwargs)
        return RefinerResult(
            text=self._out_text,
            provider=self.provider_name,
            model=self._model,
            latency_ms=55,
            used_fallback=self._used_fallback,
        )


class _StubPlatform:
    def __init__(self, wav_bytes: bytes, *, paste_outcome: PasteOutcome | None = None) -> None:
        self._wav = wav_bytes
        self._paste = paste_outcome or PasteOutcome(True, True, "ok", "Ghostty", False)
        self.deliveries: list[str] = []
        self.huds: list[str] = []

    def get_foreground_window(self) -> ForegroundWindow:
        return ForegroundWindow(hwnd=1, process="Ghostty", title="codex", bundle_id="com.mitchellh.ghostty")

    def start_recording(self, runtime_dir: Path, **kw) -> None:
        _ = (runtime_dir, kw)

    def stop_recording(self, **kw) -> tuple[bytes, str]:
        _ = kw
        return self._wav, "avfoundation"

    def cancel_recording(self) -> None:
        pass

    def deliver_text(self, text: str, **kw) -> PasteOutcome:
        before_commit = kw.get("before_commit")
        if callable(before_commit) and not before_commit():
            return PasteOutcome(False, False, "cancelled", "Ghostty", False)
        self.deliveries.append(text)
        return self._paste

    def hud(self, state: str, **kw) -> None:
        _ = kw
        self.huds.append(state)

    def beep(self, kind: str) -> None:
        _ = kind

    def is_shim_ok(self) -> bool:
        return True

    def close(self) -> None:
        pass


class Batch3AConfigContractTests(unittest.TestCase):
    def test_archive_defaults_parsing_unlimited_and_validation(self) -> None:
        cfg_def = Config()
        self.assertIs(cfg_def.archive.save_transcripts, False)
        self.assertIs(cfg_def.archive.save_audio, False)
        self.assertEqual(cfg_def.archive.audio_max_count, 1000)
        self.assertFalse(hasattr(cfg_def.audio, "save_recordings"))
        self.assertFalse(hasattr(cfg_def.audio, "save_recordings_max_count"))
        self.assertEqual(cfg_def.asr.doubao_resource_id, "auto")
        self.assertFalse(hasattr(cfg_def.asr, "resource_id"))
        self.assertEqual(cfg_def.refiner.session_max_turns, 0)
        self.assertFalse(hasattr(cfg_def.refiner, "max_turns_per_thread"))

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            toml_p = root / "config.toml"
            toml_p.write_text(
                "\n".join(
                    [
                        "[archive]",
                        "save_transcripts = true",
                        "save_audio = true",
                        "audio_max_count = 0",
                        "[asr]",
                        'doubao_resource_id = "volc.seedasr.auc"',
                        "[refiner]",
                        "session_max_turns = 12",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            cfg, _ = load_config(config_path=toml_p, env={"HOME": str(root)}, is_macbook=False)
            self.assertIs(cfg.archive.save_transcripts, True)
            self.assertIs(cfg.archive.save_audio, True)
            self.assertEqual(cfg.archive.audio_max_count, 0)
            self.assertEqual(cfg.asr.doubao_resource_id, "volc.seedasr.auc")
            self.assertEqual(cfg.refiner.session_max_turns, 12)

            for bad_toml, pat in (
                ("[archive]\naudio_max_count = -1\n", "audio_max_count"),
                ('[archive]\nsave_transcripts = "maybe"\n', "save_transcripts"),
                ('[archive]\nsave_audio = "nope"\n', "save_audio"),
                ("[archive]\nunknown_key = 1\n", "unknown archive option"),
                ("[refiner]\nsession_max_turns = -5\n", "session_max_turns"),
            ):
                bad_p = root / "bad.toml"
                bad_p.write_text(bad_toml, encoding="utf-8")
                with self.assertRaisesRegex(ConfigError, pat):
                    load_config(config_path=bad_p, env={"HOME": str(root)}, is_macbook=False)

    def test_removed_and_renamed_keys_raise_clear_migration_errors(self) -> None:
        cases = [
            ("[audio]\nsave_recordings = true\n", r"archive\.save_audio"),
            ("[audio]\nsave_recordings_max_count = 300\n", r"archive\.audio_max_count"),
            ('[asr]\nresource_id = "volc.bigasr.auc"\n', r"asr\.doubao_resource_id"),
            ("[refiner]\nmax_turns_per_thread = 10\n", r"refiner\.session_max_turns"),
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for idx, (doc, expected_pat) in enumerate(cases):
                p = root / f"mig_{idx}.toml"
                p.write_text(doc, encoding="utf-8")
                with self.subTest(case=idx):
                    with self.assertRaisesRegex(ConfigError, expected_pat):
                        load_config(config_path=p, env={"HOME": str(root)}, is_macbook=False)

    def test_config_example_toml_has_new_contract_and_bcp47_comment(self) -> None:
        raw = (_REPO_ROOT / "config.example.toml").read_text(encoding="utf-8")
        self.assertIn("[archive]", raw)
        self.assertIn("save_transcripts = false", raw)
        self.assertIn("save_audio = false", raw)
        self.assertIn("audio_max_count = 1000", raw)
        self.assertIn("doubao_resource_id", raw)
        self.assertIn("session_max_turns", raw)
        self.assertIn("zh-CN", raw)
        self.assertIn("en-US", raw)
        self.assertNotIn("save_recordings", raw)
        self.assertNotIn("max_turns_per_thread", raw)
        cfg, _ = load_config(
            config_path=_REPO_ROOT / "config.example.toml",
            env={"HOME": "/tmp/nonexistent-avi-home"},
            is_macbook=False,
        )
        self.assertIs(cfg.archive.save_transcripts, False)
        self.assertIs(cfg.archive.save_audio, False)
        self.assertEqual(cfg.archive.audio_max_count, 1000)

    def test_macbook_default_hotkey_isolation_and_zero_init_never_writes_config(self) -> None:
        # 1. Deterministic _is_macbook_laptop injection matrix
        self.assertFalse(_is_macbook_laptop(platform="linux"))
        self.assertTrue(
            _is_macbook_laptop(
                platform="darwin",
                run_fn=lambda cmd, **_kw: subprocess.CompletedProcess(cmd, 0, stdout="MacBookPro18,1\n", stderr=""),
            )
        )
        self.assertFalse(
            _is_macbook_laptop(
                platform="darwin",
                run_fn=lambda cmd, **_kw: subprocess.CompletedProcess(cmd, 0, stdout="Macmini9,1\n", stderr=""),
            )
        )

        def _pmset_fallback(cmd, **_kw):
            if cmd[0] == "sysctl":
                return subprocess.CompletedProcess(cmd, 0, stdout="Mac16,1\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout=" -InternalBattery-0 100%\n", stderr="")

        self.assertTrue(_is_macbook_laptop(platform="darwin", run_fn=_pmset_fallback))

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            env = {"HOME": str(home), "AGENT_VOICE_INPUT_HOME": str(home / ".agent-voice-input")}

            # 2. Unconfigured MacBook -> RCommand; non-MacBook -> RControl; config.toml NOT created
            cfg_mb, _ = load_config(env=env, load_default_env_files=False, is_macbook=True)
            self.assertEqual(cfg_mb.hotkey.key, "RCommand")
            self.assertFalse((home / ".agent-voice-input" / "config.toml").exists())

            cfg_non, _ = load_config(env=env, load_default_env_files=False, is_macbook=False)
            self.assertEqual(cfg_non.hotkey.key, "RControl")

            # 3. Explicit key = "RControl" in config.toml on MacBook -> strictly respected
            app_home = home / ".agent-voice-input"
            app_home.mkdir(mode=0o700)
            cfg_file = app_home / "config.toml"
            cfg_file.write_text('[hotkey]\nkey = "RControl"\n', encoding="utf-8")
            cfg_exp, _ = load_config(env=env, load_default_env_files=False, is_macbook=True)
            self.assertEqual(cfg_exp.hotkey.key, "RControl")

            # 4. CLI --hotkey override overrides MacBook auto-default
            cfg_cli, _ = load_config(
                cli_overrides={"hotkey.key": "RControl"},
                env={"HOME": str(home / "empty_home")},
                load_default_env_files=False,
                is_macbook=True,
            )
            self.assertEqual(cfg_cli.hotkey.key, "RControl")

            # 5. CLI start on MacBook with NO config.toml uses RCommand and NEVER creates config.toml
            empty_home = home / "zero_init_home"
            empty_home.mkdir()
            rt_dir = home / "rt"
            rt_dir.mkdir(mode=0o700)
            captured_cfgs: list[Config] = []

            def _fake_cmd_start(args, start_env, *, cfg=None, **_kw):
                assert cfg is not None
                captured_cfgs.append(cfg)
                return 0

            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "HOME": str(empty_home),
                        "AGENT_VOICE_INPUT_HOME": str(empty_home / ".agent-voice-input"),
                        "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt_dir),
                    },
                    clear=False,
                ),
                mock.patch.object(cli_mod, "_is_macbook_laptop", return_value=True),
                mock.patch.object(cli_mod, "_cmd_start", side_effect=_fake_cmd_start),
            ):
                rc1 = cli_mod.main(["start"])
                rc2 = cli_mod.main(["start", "--hotkey", "RControl"])
            self.assertEqual((rc1, rc2), (0, 0))
            self.assertEqual([c.hotkey.key for c in captured_cfgs], ["RCommand", "RControl"])
            self.assertFalse((empty_home / ".agent-voice-input" / "config.toml").exists())


class Batch3AArchiveEngineTests(unittest.TestCase):
    def test_short_id_貫通_12_fields_modes_and_ordered_archive(self) -> None:
        wav = _voiced_wav(0.6)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            rt = root / "rt"
            home.mkdir()
            rt.mkdir(mode=0o700)
            env = {
                "HOME": str(home),
                "AGENT_VOICE_INPUT_HOME": str(home / ".agent-voice-input"),
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
                "OPENAI_API_KEY": "sk-" + ("7" * 24),
            }
            audio_dir = resolve_archive_audio_dir(env)
            tr_dir = resolve_archive_transcripts_dir(env)

            # Force deterministic WAV fallback so test never depends on ffmpeg availability
            with mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", side_effect=AudioError("no ffmpeg")):
                # Case A: 2-stage normal (agent mode) with both save_audio + save_transcripts
                cfg_both = Config(
                    archive=ArchiveConfig(save_transcripts=True, save_audio=True, audio_max_count=10),
                    refiner=RefinerConfig(provider="codex"),
                )
                res_a = run_pipeline_once(
                    wav,
                    cfg=cfg_both,
                    asr_provider=_StubAsr("原始草稿A", provider="dashscope", model="qwen3-asr-flash"),
                    refiner=_StubRefiner("精修文本A", provider="codex", model="gpt-5.4-mini"),
                    audio_source="avfoundation",
                    dry_run=True,
                    runtime_dir=rt,
                    env=env,
                    mode="agent",
                )
                self.assertTrue(res_a.ok)
                self.assertRegex(res_a.utterance_id, UTTERANCE_ID_RE)
                sum_a = build_pipeline_summary(1, res_a, dry_run=True)
                append_diag_record(rt, sum_a)

                # Case B: 2-stage refiner fallback (general mode), transcripts only (save_audio=False)
                cfg_tr_only = Config(
                    archive=ArchiveConfig(save_transcripts=True, save_audio=False),
                    refiner=RefinerConfig(provider="codex"),
                )
                res_b = run_pipeline_once(
                    wav,
                    cfg=cfg_tr_only,
                    asr_provider=_StubAsr("原始草稿B", provider="doubao", model="bigmodel"),
                    refiner=_StubRefiner("原始草稿B", provider="codex", model="gpt-5.4-mini", used_fallback="asr_draft"),
                    audio_source="avfoundation",
                    dry_run=True,
                    runtime_dir=rt,
                    env=env,
                    mode="general",
                )
                self.assertTrue(res_b.ok)

                # Case C: refiner off
                cfg_off = Config(
                    archive=ArchiveConfig(save_transcripts=True, save_audio=False),
                    refiner=RefinerConfig(provider="off"),
                )
                res_c = run_pipeline_once(
                    wav,
                    cfg=cfg_off,
                    asr_provider=_StubAsr("无精修文本C", provider="dashscope", model="qwen3-asr-flash"),
                    refiner=NoopRefiner(),
                    audio_source="avfoundation",
                    dry_run=True,
                    runtime_dir=rt,
                    env=env,
                    mode="agent",
                )
                self.assertTrue(res_c.ok)

                # Case D: single-hop passthrough (draft=None, refiner_provider=None, refiner_model=None)
                res_d = run_pipeline_once(
                    wav,
                    cfg=cfg_off,
                    asr_provider=_StubAsr(
                        "单跳直接精修D",
                        provider="gemini",
                        model="gemini-2.5-flash",
                        single_hop=True,
                    ),
                    refiner=NoopRefiner(),
                    audio_source="avfoundation",
                    dry_run=True,
                    runtime_dir=rt,
                    env=env,
                    mode="agent",
                )
                self.assertTrue(res_d.ok)

            # Verify audio dir & 1:1 ID link
            saved_audio = list_saved_recordings(audio_dir)
            self.assertEqual(len(saved_audio), 1)
            self.assertEqual(saved_audio[0].name, f"{res_a.utterance_id}.wav")
            self.assertEqual(stat.S_IMODE(os.lstat(audio_dir).st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(os.lstat(saved_audio[0]).st_mode), 0o600)

            # Verify transcripts monthly JSONL permissions & exact 12 flat fields
            tr_files = sorted(tr_dir.glob("*.jsonl"))
            self.assertEqual(len(tr_files), 1)
            self.assertRegex(tr_files[0].name, r"^\d{4}-\d{2}\.jsonl$")
            self.assertEqual(stat.S_IMODE(os.lstat(tr_dir).st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(os.lstat(tr_files[0]).st_mode), 0o600)
            raw_jsonl = tr_files[0].read_text(encoding="utf-8")
            self.assertNotIn(env["OPENAI_API_KEY"], raw_jsonl)
            self.assertIn("精修文本A", raw_jsonl)  # ensure_ascii=False

            rows = [json.loads(line) for line in raw_jsonl.splitlines() if line.strip()]
            self.assertEqual(len(rows), 4)
            expected_keys = [
                "id",
                "ts",
                "mode",
                "duration_sec",
                "asr_provider",
                "asr_model",
                "refiner_provider",
                "refiner_model",
                "refiner_fallback",
                "draft",
                "final",
                "audio_file",
            ]
            for row in rows:
                self.assertEqual(list(row.keys()), expected_keys)
                self.assertRegex(row["id"], UTTERANCE_ID_RE)
                self.assertIsNotNone(parse_diag_ts(row["ts"]))
                self.assertIn("T", row["ts"])

            # Verify 1:1 ID across diag.jsonl, transcripts, and audio
            diag_rows = read_diag_history(rt, limit=10)
            self.assertEqual(diag_rows[-1]["id"], res_a.utterance_id)
            self.assertEqual(rows[0]["id"], res_a.utterance_id)
            self.assertEqual(rows[0]["ts"], diag_rows[-1]["ts"])
            self.assertEqual(rows[0]["audio_file"], f"{res_a.utterance_id}.wav")
            self.assertEqual(rows[0]["mode"], "agent")
            self.assertAlmostEqual(rows[0]["duration_sec"], 0.6, delta=0.05)
            self.assertEqual(rows[0]["refiner_provider"], "codex")
            self.assertEqual(rows[0]["refiner_model"], "gpt-5.4-mini")
            self.assertIs(rows[0]["refiner_fallback"], False)
            self.assertEqual(rows[0]["draft"], "原始草稿A")
            self.assertEqual(rows[0]["final"], "精修文本A")

            # Case B: fallback=True, general mode, audio_file=None
            self.assertEqual(rows[1]["mode"], "general")
            self.assertIs(rows[1]["refiner_fallback"], True)
            self.assertIsNone(rows[1]["audio_file"])

            # Case C: refiner off -> null provider/model, draft present
            self.assertIsNone(rows[2]["refiner_provider"])
            self.assertIsNone(rows[2]["refiner_model"])
            self.assertIs(rows[2]["refiner_fallback"], False)
            self.assertEqual(rows[2]["draft"], "无精修文本C")
            self.assertEqual(rows[2]["final"], "无精修文本C")

            # Case D: single-hop -> draft=None, refiner_provider=None, refiner_model=None
            self.assertIsNone(rows[3]["refiner_provider"])
            self.assertIsNone(rows[3]["refiner_model"])
            self.assertIs(rows[3]["refiner_fallback"], False)
            self.assertIsNone(rows[3]["draft"])
            self.assertEqual(rows[3]["final"], "单跳直接精修D")

    def test_audio_failure_still_writes_transcript_with_null_audio_file_and_symlink_refused(self) -> None:
        wav = _voiced_wav(0.5)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            audio_dir = root / "audio"
            tr_dir = root / "transcripts"
            ts = 1_764_236_702.0
            uid = generate_utterance_id(ts)
            rec = {
                "id": uid,
                "ts": format_local_iso_ts(ts),
                "mode": "agent",
                "duration_sec": 0.5,
                "asr_provider": "dashscope",
                "asr_model": "qwen3-asr-flash",
                "refiner_provider": None,
                "refiner_model": None,
                "refiner_fallback": False,
                "draft": "测试",
                "final": "测试",
                "audio_file": None,
            }
            with mock.patch("agent_voice_input.audio_proc.archive_recording_audio", side_effect=OSError("disk full")):
                audio_p, tr_p = run_utterance_archive(
                    utterance_id=uid,
                    record_ts=ts,
                    ts_iso=format_local_iso_ts(ts),
                    mode="agent",
                    duration_sec=0.5,
                    asr_provider="dashscope",
                    asr_model="qwen3-asr-flash",
                    refiner_provider=None,
                    refiner_model=None,
                    refiner_fallback=False,
                    draft="测试",
                    final="测试",
                    raw_wav_bytes=wav,
                    audio_dir=audio_dir,
                    save_audio=True,
                    audio_max_count=10,
                    transcripts_dir=tr_dir,
                    save_transcripts=True,
                )
            self.assertIsNone(audio_p)
            assert tr_p is not None and tr_p.is_file()
            saved = json.loads(tr_p.read_text(encoding="utf-8").strip())
            self.assertEqual(saved["id"], uid)
            self.assertIsNone(saved["audio_file"])

            # Symlinked monthly JSONL file must be refused
            sym_tr_dir = root / "sym_transcripts"
            sym_tr_dir.mkdir(mode=0o700)
            target_f = root / "outside.jsonl"
            target_f.write_text("", encoding="utf-8")
            month_name = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).astimezone().strftime("%Y-%m.jsonl")
            (sym_tr_dir / month_name).symlink_to(target_f)
            self.assertIsNone(append_transcript_record(sym_tr_dir, rec, now_ts=ts))
            self.assertEqual(target_f.read_text(encoding="utf-8"), "")

    def test_audio_prune_handles_legacy_rec_files_and_zero_means_unlimited(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            audio_dir = Path(td) / "audio"
            audio_dir.mkdir(mode=0o700)
            legacy = audio_dir / "rec-20260101-100000-111111.mp3"
            new1 = audio_dir / "20261127-100001-a1b2.mp3"
            new2 = audio_dir / "20261127-100002-c3d4.mp3"
            for idx, p in enumerate((legacy, new1, new2)):
                p.write_bytes(b"abc")
                os.chmod(p, 0o600)
                os.utime(p, (1_700_000_000 + idx * 10, 1_700_000_000 + idx * 10))

            # max_count=0 -> unlimited, nothing pruned
            prune_saved_recordings_dir(audio_dir, max_count=0)
            self.assertEqual(len(list_saved_recordings(audio_dir)), 3)

            # max_count=2 -> removes oldest (legacy rec-*) and keeps newest 2
            prune_saved_recordings_dir(audio_dir, max_count=2)
            self.assertFalse(legacy.exists())
            self.assertEqual([p.name for p in list_saved_recordings(audio_dir)], [new2.name, new1.name])

    def test_invalid_utterances_never_archive_and_daemon_close_waits_safely(self) -> None:
        wav = _voiced_wav(0.6)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            rt = root / "rt"
            home.mkdir()
            rt.mkdir(mode=0o700)
            env = {
                "HOME": str(home),
                "AGENT_VOICE_INPUT_HOME": str(home / ".agent-voice-input"),
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
            }
            cfg = Config(
                archive=ArchiveConfig(save_transcripts=True, save_audio=True, audio_max_count=5),
                refiner=RefinerConfig(provider="off"),
            )
            audio_dir = resolve_archive_audio_dir(env)
            tr_dir = resolve_archive_transcripts_dir(env)

            # 1. Silence, empty ASR, ASR error, delivery failed, cancelled, retry -> 0 archived
            run_pipeline_once(_silence_wav(), cfg=cfg, asr_provider=_StubAsr("x"), dry_run=True, env=env)
            run_pipeline_once(wav, cfg=cfg, asr_provider=_StubAsr("   "), dry_run=True, env=env)
            with self.assertRaises(AsrError):
                run_pipeline_once(wav, cfg=cfg, asr_provider=_StubAsr(raise_exc=AsrError("boom")), dry_run=True, env=env)
            run_pipeline_once(
                wav,
                cfg=cfg,
                asr_provider=_StubAsr("failed deliver"),
                deliver_fn=lambda _t, _w: PasteOutcome(False, False, "paste_failed", "Ghostty", False),
                env=env,
            )
            run_pipeline_once(wav, cfg=cfg, asr_provider=_StubAsr("cancelled"), is_cancelled=lambda: True, dry_run=True, env=env)
            run_pipeline_once(wav, cfg=cfg, asr_provider=_StubAsr("retry"), audio_source="retry", dry_run=True, env=env)
            run_pipeline_once(wav, cfg=cfg, asr_provider=_StubAsr("selftest"), audio_source="selftest_wav", dry_run=True, env=env)

            self.assertEqual(list_saved_recordings(audio_dir), [])
            self.assertEqual(list(tr_dir.glob("*.jsonl")) if tr_dir.exists() else [], [])

            # 2. Daemon close() waits for slow in-flight archive thread and links 1:1 ID across all 3 places
            archive_finished = threading.Event()
            real_run = run_utterance_archive

            def _slow_archive(**kw):
                time.sleep(0.15)
                try:
                    return real_run(**kw)
                finally:
                    archive_finished.set()

            plat = _StubPlatform(wav)
            tracker = SessionTracker(home_dir=home, runtime_dir=rt, proc_root=root / "no_proc", env=env)
            daemon = VoiceInputDaemon(
                cfg,
                env=env,
                runtime_dir=rt,
                platform=plat,
                asr_provider=_StubAsr("守护进程三处贯通测试"),
                refiner=NoopRefiner(),
                tracker=tracker,
                paste_settle_sec=0.0,
            )
            with (
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", side_effect=AudioError("no ffmpeg")),
                mock.patch("agent_voice_input.audio_proc.run_utterance_archive", side_effect=_slow_archive),
            ):
                daemon.handle_trigger("toggle")
                daemon._last_trigger_ts = -1e9
                res_stop = daemon.handle_trigger("toggle")
                dl = time.monotonic() + 3.0
                while time.monotonic() < dl and daemon.last_result.get("seq_id") != res_stop["seq_id"]:
                    time.sleep(0.01)
                daemon.close()

            self.assertTrue(archive_finished.is_set())
            diag_items = read_diag_history(rt, limit=5)
            self.assertEqual(len(diag_items), 1)
            uid = diag_items[0]["id"]
            self.assertRegex(uid, UTTERANCE_ID_RE)
            self.assertEqual([p.name for p in list_saved_recordings(audio_dir)], [f"{uid}.wav"])
            tr_files = list(tr_dir.glob("*.jsonl"))
            self.assertEqual(len(tr_files), 1)
            tr_row = json.loads(tr_files[0].read_text(encoding="utf-8").strip())
            self.assertEqual(tr_row["id"], uid)
            self.assertEqual(tr_row["audio_file"], f"{uid}.wav")
            self.assertEqual(tr_row["ts"], diag_items[0]["ts"])


class Batch3ADiagAndStatusTests(unittest.TestCase):
    def test_diag_iso_and_legacy_ts_compatibility_and_id_sanitization(self) -> None:
        iso_ts = format_local_iso_ts(1_764_236_702.0)
        self.assertIsNotNone(parse_diag_ts(iso_ts))
        self.assertAlmostEqual(parse_diag_ts(1_764_236_702.5) or 0.0, 1_764_236_702.5, places=3)
        self.assertIsNone(parse_diag_ts("not-a-timestamp"))
        self.assertIsNone(parse_diag_ts(True))
        self.assertRegex(_fmt_time_short(iso_ts), r"^\d{2}:\d{2}:\d{2}$")
        self.assertRegex(_fmt_time_short(1_764_236_702.0), r"^\d{2}:\d{2}:\d{2}$")
        self.assertEqual(_fmt_time_short("bad"), "-")

        clean_new = _sanitize_diag_record(
            {"id": "20261127-174502-a1b2", "ts": iso_ts, "seq_id": 1, "ok": True, "final_text": "SECRET_PROSE"},
            lambda s: s,
        )
        self.assertEqual(clean_new["id"], "20261127-174502-a1b2")
        self.assertEqual(clean_new["ts"], iso_ts)
        self.assertNotIn("final_text", clean_new)

        clean_bad_id = _sanitize_diag_record(
            {"id": "not valid prose!!", "ts": 1_764_236_702.0, "seq_id": 2, "ok": True},
            lambda s: s,
        )
        self.assertNotIn("id", clean_bad_id)
        self.assertEqual(clean_bad_id["ts"], 1_764_236_702.0)

        # Dedupe by id in merge_recent_results
        merged = merge_recent_results(
            [{"id": "20261127-174502-a1b2", "ts": iso_ts, "seq_id": 1}],
            [{"id": "20261127-174502-a1b2", "ts": iso_ts, "seq_id": 1}],
            limit=5,
        )
        self.assertEqual(len(merged), 1)

        # collect_live_diagnostics & markdown handle mixed legacy numeric + new ISO diag.jsonl rows
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rt = root / "rt"
            rt.mkdir(mode=0o700)
            append_diag_record(rt, {"ts": 1_764_236_600.0, "seq_id": 1, "ok": True, "mode": "agent", "total_ms": 200})
            append_diag_record(
                rt,
                {"id": "20261127-174502-a1b2", "ts": iso_ts, "seq_id": 2, "ok": True, "mode": "agent", "total_ms": 180},
            )
            payload = collect_live_diagnostics(
                Config(),
                {"HOME": str(root), "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)},
                runtime_dir=rt,
                last=5,
            )
            self.assertEqual(len(payload["recent_results"]), 2)
            md = format_live_diagnostics_markdown(payload)
            self.assertIn("Live Diagnostics", md)
            self.assertNotIn("|-|1|", md)

    def test_archive_summary_formatting_and_cli_status_doctor_start_outputs(self) -> None:
        self.assertEqual(format_archive_summary(ArchiveConfig()), "off")
        self.assertEqual(
            format_archive_summary(ArchiveConfig(save_transcripts=True, save_audio=False)),
            "transcripts=on, audio=off",
        )
        self.assertEqual(
            format_archive_summary(ArchiveConfig(save_transcripts=False, save_audio=True, audio_max_count=500)),
            "transcripts=off, audio=on (max 500)",
        )
        self.assertEqual(
            format_archive_summary(ArchiveConfig(save_transcripts=True, save_audio=True, audio_max_count=1000)),
            "transcripts=on, audio=on (max 1000)",
        )
        self.assertEqual(
            format_archive_summary(ArchiveConfig(save_transcripts=True, save_audio=True, audio_max_count=0)),
            "transcripts=on, audio=on (unlimited)",
        )

        # status response & human formatting include Archive:
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            rt.mkdir(mode=0o700)
            arch_cfg = ArchiveConfig(save_transcripts=True, save_audio=True, audio_max_count=0)
            cfg = Config(archive=arch_cfg)
            resp = build_daemon_status_response(
                runtime_dir=rt,
                req={"cmd": "status"},
                build_id="abc123def456",
                state="idle",
                seq_id=0,
                pending_count=0,
                active_pipeline={"asr": {"provider": "dashscope", "model": "qwen3-asr-flash"}, "refiner": {"provider": "off", "model": ""}},
                resolved_refiner_provider="off",
                shim_ok=True,
                shim_error="",
                last_result={},
                remote_hosts=None,
                mem_recent_results=[],
                archive=format_archive_status_dict(arch_cfg),
            )
            self.assertEqual(
                resp["archive"],
                {"save_transcripts": True, "save_audio": True, "audio_max_count": 0},
            )
            human_on = "\n".join(cli_mod._format_status_human(resp, cfg=cfg))
            self.assertIn("Archive", human_on)
            self.assertIn("transcripts=on, audio=on (unlimited)", human_on)

            human_off = "\n".join(cli_mod._format_status_human({"ok": False, "state": "stopped"}, cfg=Config()))
            self.assertIn("Archive", human_off)
            self.assertIn("off", human_off)

            # doctor text & --json include Archive: / archive dict
            buf_txt = io.StringIO()
            with (
                mock.patch.object(cli_mod, "_doctor_check_env_and_ffmpeg"),
                mock.patch.object(cli_mod, "_doctor_check_platform"),
                mock.patch.object(cli_mod, "_doctor_check_asr"),
                mock.patch.object(cli_mod, "_doctor_check_hotwords"),
                mock.patch.object(cli_mod, "_doctor_check_daemon_and_session"),
                mock.patch.object(cli_mod, "_doctor_check_remote_ssh"),
                mock.patch.object(cli_mod, "_doctor_check_refiner"),
                contextlib.redirect_stdout(buf_txt),
            ):
                cli_mod._cmd_doctor(cfg, {"HOME": td, "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)}, as_json=False)
            self.assertIn("Archive: transcripts=on, audio=on (unlimited)", buf_txt.getvalue())

            buf_json = io.StringIO()
            with (
                mock.patch.object(cli_mod, "_doctor_check_env_and_ffmpeg"),
                mock.patch.object(cli_mod, "_doctor_check_platform"),
                mock.patch.object(cli_mod, "_doctor_check_asr"),
                mock.patch.object(cli_mod, "_doctor_check_hotwords"),
                mock.patch.object(cli_mod, "_doctor_check_daemon_and_session"),
                mock.patch.object(cli_mod, "_doctor_check_remote_ssh"),
                mock.patch.object(cli_mod, "_doctor_check_refiner"),
                contextlib.redirect_stdout(buf_json),
            ):
                cli_mod._cmd_doctor(cfg, {"HOME": td, "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)}, as_json=True)
            doc_json = json.loads(buf_json.getvalue())
            self.assertEqual(
                doc_json["archive"],
                {"save_transcripts": True, "save_audio": True, "audio_max_count": 0},
            )

            # _cmd_start (already running path) prints Archive line
            buf_start = io.StringIO()
            fake_args = cli_mod._build_parser().parse_args(["start"])
            with (
                mock.patch("agent_voice_input.control_ipc.probe_daemon_status", return_value=resp),
                contextlib.redirect_stdout(buf_start),
            ):
                rc_s = cli_mod._cmd_start(
                    fake_args,
                    {"HOME": td, "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)},
                    cfg=cfg,
                )
            self.assertEqual(rc_s, 0)
            self.assertIn("Archive", buf_start.getvalue())
            self.assertIn("transcripts=on, audio=on (unlimited)", buf_start.getvalue())


if __name__ == "__main__":
    unittest.main()
