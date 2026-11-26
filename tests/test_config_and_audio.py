"""Unit tests for ``agent_voice_input.config`` and ``agent_voice_input.audio_proc``."""
from __future__ import annotations

import array
import io
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock
import urllib.parse
import wave

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _REPO_ROOT / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))


def _is_ci() -> bool:
    return os.environ.get("CI", "").strip().lower() in {"1", "true", "yes"}

from agent_voice_input.audio_proc import (  # noqa: E402
    AudioError,
    DEFAULT_MAX_CHUNK_SEC,
    PEAK_LIMIT_INT16,
    SINGLE_HOP_MAX_CHUNK_SEC,
    SINGLE_HOP_MP3_MIN_WAV_BYTES,
    _TRIM_PRESETS,
    _plan_silence_trim,
    _splice_with_crossfade,
    decode_to_16k_mono_pcm,
    encode_wav_to_mp3_bytes,
    inspect_wav_chunks,
    list_failed_audios,
    pack_pcm16_mono_wav,
    process_wav,
    resolve_failed_audio,
    resolve_ffmpeg_bin,
    save_failed_audio,
    split_wav_chunks,
)
from agent_voice_input.config import (  # noqa: E402
    ASR_AUTO_PRIORITY,
    AudioConfig,
    CREDENTIAL_ENV_ORDER,
    Config,
    ConfigError,
    DEFAULT_ASR_BASE_URLS,
    DEFAULT_ASR_MODELS,
    DEFAULT_REFINER_BASE_URLS,
    DEFAULT_REFINER_EFFORTS,
    DEFAULT_REFINER_MODELS,
    RefinerConfig,
    VALID_ASR_APIS,
    VALID_ASR_PROVIDERS,
    VALID_EFFORTS,
    VALID_REFINER_PROVIDERS,
    VALID_SILENCE_TRIM,
    _parse_simple_toml,
    classify_asr_model_api,
    ensure_secure_dir,
    get_runtime_dir,
    is_dashscope_omni_chat_model,
    load_config,
    load_env_files,
    parse_env_text,
    parse_toml,
    redact_secrets,
    resolve_asr_api,
    resolve_credentials,
    select_asr_provider,
)


def _unpack_wav_samples(wav_bytes: bytes) -> tuple[int, int, int, array.array[int]]:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        ch = wf.getnchannels()
        sw = wf.getsampwidth()
        sr = wf.getframerate()
        # wave.Wave_read.readframes() already returns host-endian sample bytes
        pcm = wf.readframes(wf.getnframes())
    arr = array.array("h")
    arr.frombytes(pcm)
    return ch, sw, sr, arr


class SimpleTomlParserTests(unittest.TestCase):
    def test_parses_sections_scalars_escapes_and_arrays(self) -> None:
        toml_doc = """\ufeff
        # Top-level comment
        [hotkey]
        key = "RControl" # inline comment with # hash
        trigger_mode = 'tap_toggle'
        tap_max_ms = 1_200
        debounce_ms = 180

        [audio]
        floor_dbfs = -50.0
        target_dbfs = -20.0
        beep = true

        [asr.extra]
        tags = ["ali#1", "doubao", "gemini"]
        multiline = [
            10,
            20,
            30,
        ]
        escaped = "hello\\n\\u4e2d\\u6587\\""
        """
        parsed = _parse_simple_toml(toml_doc)
        self.assertEqual(parsed["hotkey"]["key"], "RControl")
        self.assertEqual(parsed["hotkey"]["trigger_mode"], "tap_toggle")
        self.assertEqual(parsed["hotkey"]["tap_max_ms"], 1200)
        self.assertEqual(parsed["audio"]["floor_dbfs"], -50.0)
        self.assertIs(parsed["audio"]["beep"], True)
        self.assertEqual(parsed["asr"]["extra"]["tags"], ["ali#1", "doubao", "gemini"])
        self.assertEqual(parsed["asr"]["extra"]["multiline"], [10, 20, 30])
        self.assertEqual(parsed["asr"]["extra"]["escaped"], 'hello\n中文"')

        # parse_toml entry point also handles UTF-8 BOM cleanly across 3.10 and 3.11+
        parsed_entry = parse_toml(toml_doc)
        self.assertEqual(parsed_entry["hotkey"]["key"], "RControl")

    def test_parses_multiline_basic_and_literal_toml_strings_identically_across_runtimes(self) -> None:
        toml_doc = (
            "[refiner]\n"
            "prompt = '''\n"
            "# Heading inside literal multiline (not a comment!)\n"
            "Line 2 with raw backslash C:\\temp\\file and 'single' ''two'' quotes.\n"
            "\n"
            "  Indented line 4 after blank line''''\n"
            "alt_basic = \"\"\"\n"
            "# Basic multiline with escape \\u4e2d\\u6587 and folded \\\n"
            "   \t  continuation plus \"one\" \"\"two\"\" quotes\"\"\"\"\"\n"
            "inline_ml = '''kept first line\nsecond line''' # trailing comment\n"
            "arr_ml = [\n"
            "  # comment inside multiline array\n"
            "  '''first\nitem''',\n"
            "  \"\"\"second\\nitem\"\"\",\n"
            "]\n"
        )
        parsed_simple = _parse_simple_toml(toml_doc)
        parsed_entry = parse_toml(toml_doc)
        self.assertEqual(parsed_simple, parsed_entry)
        self.assertEqual(
            parsed_simple["refiner"]["prompt"],
            "# Heading inside literal multiline (not a comment!)\n"
            "Line 2 with raw backslash C:\\temp\\file and 'single' ''two'' quotes.\n"
            "\n"
            "  Indented line 4 after blank line'",
        )
        self.assertEqual(
            parsed_simple["refiner"]["alt_basic"],
            '# Basic multiline with escape 中文 and folded continuation plus "one" ""two"" quotes""',
        )
        self.assertEqual(parsed_simple["refiner"]["inline_ml"], "kept first line\nsecond line")
        self.assertEqual(parsed_simple["refiner"]["arr_ml"], ["first\nitem", "second\nitem"])
        # CRLF leading newline trimming and normalization inside multiline strings
        crlf_doc = "[refiner]\r\nprompt = '''\r\nfirst\r\nsecond'''\r\n"
        self.assertEqual(_parse_simple_toml(crlf_doc)["refiner"]["prompt"], "first\nsecond")
        self.assertEqual(parse_toml(crlf_doc)["refiner"]["prompt"], "first\nsecond")

    def test_rejects_invalid_toml_syntax_surrogates_and_control_chars_with_line_number(self) -> None:
        bad_cases = [
            ('[hotkey\nkey = "F8"', "line 1"),
            ('[hotkey]\nkey = "unterminated', "line 2"),
            ('[hotkey]\narr = ["a", 1]', "line 2"),
            ('[hotkey]\nflag = True', "line 2"),
            ('[hotkey]\nkey = "F8"\nkey = "F9"', "line 3"),
            ('[hotkey]\n[hotkey]', "line 2"),
            ('[hotkey]\nmissing_equals', "line 2"),
            ('[hotkey]\nsurrogate = "\\uD800"', "line 2"),
            ('[hotkey]\nout_of_range = "\\U00110000"', "line 2"),
            ('[hotkey]\nmax_overflow = "\\UFFFFFFFF"', "line 2"),
            ('[hotkey]\nraw_nul = "abc\x00def"', "line 2"),
            ('[hotkey]\narr = [\n  "unclosed_across_newline,\n  b"\n]', "line 3"),
            ("[refiner]\nprompt = '''\nunterminated multiline", "line 2"),
            ('[refiner]\nprompt = """\nunterminated basic', "line 2"),
            ("[refiner]\nprompt = '''too many ''''''", "line 2"),
            ('[refiner]\nprompt = """too many """"""', "line 2"),
            ("[refiner]\nprompt = '''\nline3\nbad\x00ctrl'''", "line 4"),
            ('["""badkey"""]\nx = 1', "line 1"),
        ]
        for doc, expected_line in bad_cases:
            with self.subTest(doc=repr(doc)):
                with self.assertRaises(ConfigError) as ctx:
                    _parse_simple_toml(doc)
                self.assertIn(expected_line, str(ctx.exception))


class ConfigAndCredentialsTests(unittest.TestCase):
    def test_config_precedence_cli_over_file_over_default_and_ignores_ordinary_env_knobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cfg_dir = home / ".config" / "voice-input"
            cfg_dir.mkdir(parents=True)
            (cfg_dir / "config.toml").write_text(
                """
                [hotkey]
                key = "F8"
                trigger_mode = "ptt"
                tap_max_ms = 900

                [asr]
                provider = "dashscope"
                timeout_sec = 20.0

                [refiner]
                provider = "none"
                effort = "medium"
                """,
                encoding="utf-8",
            )
            (cfg_dir / "config.json").write_text(
                json.dumps({"hotkey": {"key": "F9"}}),
                encoding="utf-8",
            )

            # Ordinary config env vars are ignored; config.toml and CLI overrides govern
            env = {
                "HOME": str(home),
                "AGENT_VOICE_INPUT_HOTKEY": "RAlt",
                "VOICE_INPUT_HOTKEY": "RAlt",
                "VOICE_INPUT_ASR_PROVIDER": "doubao",
            }
            cfg, _ = load_config(
                env=env,
                home_dir=home,
                cli_overrides={"asr.provider": "gemini", "hotkey.debounce_ms": None},
            )
            self.assertEqual(cfg.hotkey.key, "F8")
            self.assertEqual(cfg.hotkey.trigger_mode, "ptt")
            self.assertEqual(cfg.hotkey.tap_max_ms, 900)
            self.assertEqual(cfg.hotkey.debounce_ms, 180)
            self.assertEqual(cfg.asr.provider, "gemini")
            self.assertEqual(cfg.asr.effective_model(), "gemini-3.7-flash")
            self.assertEqual(cfg.refiner.provider, "off")
            self.assertEqual(cfg.refiner.effort, "medium")

            # AGENT_VOICE_INPUT_CONFIG path bootstrap env var still selects config file
            alt_cfg = home / "custom.toml"
            alt_cfg.write_text('[hotkey]\nkey = "F10"\n', encoding="utf-8")
            cfg_alt, _ = load_config(
                env={"HOME": str(home), "AGENT_VOICE_INPUT_CONFIG": str(alt_cfg)},
                home_dir=home,
            )
            self.assertEqual(cfg_alt.hotkey.key, "F10")

    def test_falls_back_to_utf8_bom_json_and_parses_empty_env_inline_comments(self) -> None:
        env_parsed = parse_env_text(
            "DOUBAO_ASR_APP_ID= # leave blank for single-key mode\n"
            "API_KEY_ALI=val_abc # trailing comment\n"
        )
        self.assertEqual(env_parsed["DOUBAO_ASR_APP_ID"], "")
        self.assertEqual(env_parsed["API_KEY_ALI"], "val_abc")

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            new_dir = home / ".config" / "agent-voice-input"
            legacy_dir = home / ".config" / "voice-input"
            legacy_dir.mkdir(parents=True)

            # 1. Legacy fallback when ~/.config/agent-voice-input does not exist yet
            #    (JSON with UTF-8 BOM to simulate Windows Notepad / PowerShell 5.1 save)
            (legacy_dir / "config.json").write_bytes(
                b"\xef\xbb\xbf"
                + json.dumps({"audio": {"wsl_source": "winmm", "beep": False}}).encode("utf-8")
            )
            (legacy_dir / "voice-input.env").write_text(
                'API_KEY_ALI="from_legacy_env"\n'
                'GEMINI_API_KEY="from_legacy_gemini"\n',
                encoding="utf-8",
            )
            legacy_cfg, legacy_meta = load_config(env={"HOME": str(home)}, home_dir=home)
            self.assertEqual(legacy_cfg.audio.wsl_source, "winmm")
            self.assertIs(legacy_cfg.audio.beep, False)
            self.assertTrue(str(legacy_meta).endswith("config.json") or legacy_cfg.config_path.endswith("config.json"))

            # 2. New ~/.config/agent-voice-input takes precedence over legacy ~/.config/voice-input,
            #    while missing keys in new env file still smoothly fall back to legacy env file!
            new_dir.mkdir(parents=True)
            (new_dir / "config.toml").write_text(
                '[audio]\nwsl_source = "wasapi"\nbeep = true\n',
                encoding="utf-8",
            )
            (new_dir / "agent-voice-input.env").write_text(
                'export API_KEY_ALI="from_new_env" # comment\n'
                "DOUBAO_ASR_APP_ID= # empty with comment\n"
                "DOUBAO_ASR_API_KEY='from_new_single'\n",
                encoding="utf-8",
            )

            caller_env = {
                "HOME": str(home),
                "DOUBAO_ASR_API_KEY": "caller_wins_val",
                "AGENT_VOICE_INPUT_HOTKEY": "F9",
                "VOICE_INPUT_TRIGGER_MODE": "ptt",
            }
            merged = load_env_files(caller_env, home_dir=home)
            self.assertEqual(merged["DOUBAO_ASR_API_KEY"], "caller_wins_val")
            self.assertEqual(merged["DOUBAO_ASR_APP_ID"], "")
            self.assertEqual(merged["API_KEY_ALI"], "from_new_env")
            self.assertEqual(merged["GEMINI_API_KEY"], "from_legacy_gemini")

            cfg, _ = load_config(env=caller_env, home_dir=home)
            self.assertEqual(cfg.audio.wsl_source, "wasapi")
            self.assertIs(cfg.audio.beep, True)
            # Ordinary config env vars do not override defaults/config.toml
            self.assertEqual(cfg.hotkey.key, "RControl")
            self.assertEqual(cfg.hotkey.trigger_mode, "tap_toggle")

    def test_rejects_float_for_int_nonfinite_float_and_invalid_effort_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with self.assertRaises(ConfigError):
                load_config(
                    env={"HOME": str(home)},
                    home_dir=home,
                    cli_overrides={"hotkey.tap_max_ms": 1.2},
                )
            with self.assertRaises(ConfigError):
                load_config(
                    env={"HOME": str(home)},
                    home_dir=home,
                    cli_overrides={"hotkey.hold_cancel_ms": 800.5},
                )

    def test_hotkey_hold_cancel_defaults_legacy_alignment_and_validation(self) -> None:
        from agent_voice_input.config import HotkeyConfig, _validate_hotkey_config

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cfg_def, _ = load_config(env={"HOME": str(home)}, home_dir=home)
            self.assertEqual(cfg_def.hotkey.tap_max_ms, 500)
            self.assertEqual(cfg_def.hotkey.hold_cancel_ms, 800)
            self.assertEqual(cfg_def.hotkey.effective_hold_cancel_ms(), 800)
            self.assertEqual(cfg_def.hotkey.effective_hold_hint_ms(), 250)

            # Legacy configs with only a custom tap_max_ms auto-align hold_cancel_ms = max(800, tap_max_ms + 300)
            legacy_p = home / "legacy.toml"
            legacy_p.write_text("[hotkey]\ntap_max_ms = 1200\n", encoding="utf-8")
            cfg_leg, _ = load_config(config_path=legacy_p, env={"HOME": str(home)}, home_dir=home)
            self.assertEqual(cfg_leg.hotkey.tap_max_ms, 1200)
            self.assertEqual(cfg_leg.hotkey.hold_cancel_ms, 1500)
            self.assertEqual(cfg_leg.hotkey.effective_hold_cancel_ms(), 1500)
            self.assertEqual(cfg_leg.hotkey.effective_hold_hint_ms(), 250)

            legacy_mid = home / "legacy_mid.toml"
            legacy_mid.write_text("[hotkey]\ntap_max_ms = 700\n", encoding="utf-8")
            cfg_mid, _ = load_config(config_path=legacy_mid, env={"HOME": str(home)}, home_dir=home)
            self.assertEqual(cfg_mid.hotkey.tap_max_ms, 700)
            self.assertEqual(cfg_mid.hotkey.hold_cancel_ms, 1000)
            self.assertEqual(cfg_mid.hotkey.effective_hold_cancel_ms(), 1000)
            self.assertEqual(cfg_mid.hotkey.effective_hold_hint_ms(), 250)

            legacy_short = home / "legacy_short.toml"
            legacy_short.write_text("[hotkey]\ntap_max_ms = 300\n", encoding="utf-8")
            cfg_short, _ = load_config(config_path=legacy_short, env={"HOME": str(home)}, home_dir=home)
            self.assertEqual(cfg_short.hotkey.tap_max_ms, 300)
            self.assertEqual(cfg_short.hotkey.hold_cancel_ms, 800)

            # Direct Python construction with custom tap_max_ms also stays aligned via effective_hold_cancel_ms()
            self.assertEqual(HotkeyConfig(tap_max_ms=1200).effective_hold_cancel_ms(), 1500)
            self.assertEqual(HotkeyConfig(tap_max_ms=700).effective_hold_cancel_ms(), 1000)
            self.assertEqual(HotkeyConfig(tap_max_ms=50, hold_cancel_ms=60).effective_hold_hint_ms(), 30)
            self.assertEqual(HotkeyConfig(tap_max_ms=100, hold_cancel_ms=80).effective_hold_cancel_ms(), 400)

            # Explicit hold_cancel_ms = 0 disables hold-to-cancel & preview hint
            cfg_off, _ = load_config(
                env={"HOME": str(home)},
                home_dir=home,
                cli_overrides={"hotkey.hold_cancel_ms": 0},
            )
            self.assertEqual(cfg_off.hotkey.hold_cancel_ms, 0)
            self.assertEqual(cfg_off.hotkey.effective_hold_cancel_ms(), 0)
            self.assertEqual(cfg_off.hotkey.effective_hold_hint_ms(), 0)

            # Tight custom thresholds stay aligned with shim lower bound (>=50) and hold_hint_ms < hold_cancel_ms
            cfg_tight, _ = load_config(
                env={"HOME": str(home)},
                home_dir=home,
                cli_overrides={"hotkey.tap_max_ms": 50, "hotkey.hold_cancel_ms": 80},
            )
            self.assertEqual(cfg_tight.hotkey.tap_max_ms, 50)
            self.assertEqual(cfg_tight.hotkey.effective_hold_cancel_ms(), 80)
            self.assertEqual(cfg_tight.hotkey.effective_hold_hint_ms(), 40)
            self.assertLess(cfg_tight.hotkey.effective_hold_hint_ms(), cfg_tight.hotkey.effective_hold_cancel_ms())

            # tap_max_ms < 50 (e.g. 20 / 49) rejected to match native shim lower bound
            for bad_tap in (20, 49):
                with self.assertRaisesRegex(ConfigError, "hotkey.tap_max_ms must be >= 50"):
                    load_config(
                        env={"HOME": str(home)},
                        home_dir=home,
                        cli_overrides={"hotkey.tap_max_ms": bad_tap},
                    )

            # Explicit conflicting hold_cancel_ms <= tap_max_ms or negative / bool raises ConfigError
            with self.assertRaisesRegex(ConfigError, "hotkey.hold_cancel_ms must be 0"):
                load_config(
                    env={"HOME": str(home)},
                    home_dir=home,
                    cli_overrides={"hotkey.tap_max_ms": 900, "hotkey.hold_cancel_ms": 600},
                )
            with self.assertRaisesRegex(ConfigError, "hotkey.hold_cancel_ms must be 0"):
                load_config(
                    env={"HOME": str(home)},
                    home_dir=home,
                    cli_overrides={"hotkey.tap_max_ms": 500, "hotkey.hold_cancel_ms": 500},
                )
            with self.assertRaisesRegex(ConfigError, "hotkey.hold_cancel_ms must be >= 0"):
                load_config(
                    env={"HOME": str(home)},
                    home_dir=home,
                    cli_overrides={"hotkey.hold_cancel_ms": -1},
                )
            with self.assertRaisesRegex(ConfigError, "expected integer, got boolean"):
                load_config(
                    env={"HOME": str(home)},
                    home_dir=home,
                    cli_overrides={"hotkey.hold_cancel_ms": True},
                )
            with self.assertRaisesRegex(ConfigError, "hotkey.hold_cancel_ms must be >= 0"):
                bad_cfg = Config(hotkey=HotkeyConfig(hold_cancel_ms=True))
                _validate_hotkey_config(bad_cfg)

            # Verify both platform shim builders forward tap_max_ms / effective hold_cancel_ms / hold_hint_ms
            from unittest.mock import patch
            from agent_voice_input.platform_macos import MacOSPlatform
            from agent_voice_input.platform_wsl import WslPlatform

            for cfg_case, exp_tap, exp_hc, exp_hint in (
                (cfg_def, "500", "800", "250"),
                (cfg_leg, "1200", "1500", "250"),
                (cfg_off, "500", "0", "0"),
                (cfg_tight, "50", "80", "40"),
            ):
                wsl_p = WslPlatform(cfg_case, env={"HOME": str(home)})
                with (
                    patch("agent_voice_input.platform_wsl.os.path.isfile", return_value=True),
                    patch("agent_voice_input.platform_wsl.to_windows_path", return_value=r"C:\shim.ps1"),
                ):
                    wcmd = wsl_p._build_shim_cmd()
                self.assertEqual(wcmd[wcmd.index("-TapMaxMs") + 1], exp_tap)
                self.assertEqual(wcmd[wcmd.index("-HoldCancelMs") + 1], exp_hc)
                self.assertEqual(wcmd[wcmd.index("-HoldHintMs") + 1], exp_hint)

                mac_p = MacOSPlatform(cfg_case, env={"HOME": str(home)})
                with patch(
                    "agent_voice_input.platform_macos.compile_mac_shim",
                    return_value=(home / "mac_shim", ""),
                ):
                    mcmd = mac_p._build_shim_cmd()
                self.assertIsNotNone(mcmd)
                assert mcmd is not None
                self.assertEqual(mcmd[mcmd.index("--tap-max-ms") + 1], exp_tap)
                self.assertEqual(mcmd[mcmd.index("--hold-cancel-ms") + 1], exp_hc)
                self.assertEqual(mcmd[mcmd.index("--hold-hint-ms") + 1], exp_hint)

    def test_rejects_nonfinite_float_and_invalid_effort_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with self.assertRaises(ConfigError):
                load_config(
                    env={"HOME": str(home)},
                    home_dir=home,
                    cli_overrides={"audio.floor_dbfs": float("nan")},
                )
            # All 9 canonical effort levels are accepted on both stages; bogus values raise ConfigError
            for valid_eff in ("auto", "default", "none", "minimal", "low", "medium", "high", "xhigh", "max"):
                ok_cfg, _ = load_config(
                    env={"HOME": str(home)},
                    home_dir=home,
                    cli_overrides={"asr.effort": valid_eff, "refiner.effort": valid_eff},
                )
                self.assertEqual(ok_cfg.asr.effort, valid_eff)
                self.assertEqual(ok_cfg.refiner.effort, valid_eff)
            with self.assertRaises(ConfigError):
                load_config(
                    env={"HOME": str(home)},
                    home_dir=home,
                    cli_overrides={"refiner.effort": "ultra"},
                )
            with self.assertRaises(ConfigError):
                load_config(
                    env={"HOME": str(home)},
                    home_dir=home,
                    cli_overrides={"asr.effort": "turbo"},
                )
            with self.assertRaises(ConfigError):
                load_config(
                    env={"HOME": str(home)},
                    home_dir=home,
                    cli_overrides={"audio.max_gain_db": 9999.0},
                )
            with self.assertRaises(ConfigError):
                load_config(
                    env={"HOME": str(home)},
                    home_dir=home,
                    cli_overrides={"audio.floor_dbfs": 5.0},
                )
            with self.assertRaises(ConfigError):
                load_config(
                    env={"HOME": str(home)},
                    home_dir=home,
                    cli_overrides={"audio.tail_drain_sec": -0.1},
                )
            with self.assertRaises(ConfigError):
                load_config(
                    env={"HOME": str(home)},
                    home_dir=home,
                    cli_overrides={"audio.tail_drain_sec": 650.0},
                )

    def test_beep_default_false_and_platform_adaptive_tail_drain_sec(self) -> None:
        default_cfg = Config()
        self.assertIs(default_cfg.audio.beep, False)
        self.assertIsNone(default_cfg.audio.tail_drain_sec)
        self.assertEqual(default_cfg.audio.max_duration_sec, 600.0)
        self.assertAlmostEqual(default_cfg.audio.effective_tail_drain_sec(macos=False), 0.65)
        self.assertAlmostEqual(default_cfg.audio.effective_tail_drain_sec(macos=True), 0.20)

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cfg_path = home / "config.toml"
            cfg_path.write_text("[audio]\ntail_drain_sec = 0.0\n", encoding="utf-8")
            cfg_zero, _ = load_config(
                config_path=cfg_path,
                env={"HOME": str(home)},
                home_dir=home,
            )
            self.assertEqual(cfg_zero.audio.tail_drain_sec, 0.0)
            self.assertEqual(cfg_zero.audio.effective_tail_drain_sec(macos=False), 0.0)
            self.assertEqual(cfg_zero.audio.effective_tail_drain_sec(macos=True), 0.0)

    def test_max_duration_sec_config_and_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cfg_dir = home / ".config" / "agent-voice-input"
            cfg_dir.mkdir(parents=True)
            (cfg_dir / "config.toml").write_text(
                "[audio]\nmax_duration_sec = 300\n",
                encoding="utf-8",
            )
            cfg_toml, _ = load_config(env={"HOME": str(home)}, home_dir=home)
            self.assertEqual(cfg_toml.audio.max_duration_sec, 300.0)

            # 0 disables max duration limit via config.toml or cli_overrides
            (cfg_dir / "config.toml").write_text(
                "[audio]\nmax_duration_sec = 0\n",
                encoding="utf-8",
            )
            cfg_zero, _ = load_config(env={"HOME": str(home)}, home_dir=home)
            self.assertEqual(cfg_zero.audio.max_duration_sec, 0.0)

            cfg_cli, _ = load_config(
                env={"HOME": str(home), "AGENT_VOICE_INPUT_MAX_DURATION_SEC": "120"},
                home_dir=home,
                cli_overrides={"audio.max_duration_sec": 480.5},
            )
            self.assertEqual(cfg_cli.audio.max_duration_sec, 480.5)

            for bad_override in (-0.1, -60, float("nan"), float("inf"), float("-inf")):
                with self.subTest(bad_override=bad_override):
                    with self.assertRaises(ConfigError) as ctx:
                        load_config(
                            env={"HOME": str(home)},
                            home_dir=home,
                            cli_overrides={"audio.max_duration_sec": bad_override},
                        )
                    self.assertIn("audio.max_duration_sec must be >= 0", str(ctx.exception))

            for bad_toml in ("-1", "-0.01", '"abc"', "true"):
                with self.subTest(bad_toml=bad_toml):
                    (cfg_dir / "config.toml").write_text(
                        f"[audio]\nmax_duration_sec = {bad_toml}\n",
                        encoding="utf-8",
                    )
                    with self.assertRaises(ConfigError):
                        load_config(env={"HOME": str(home)}, home_dir=home)

    def test_credential_suffix_first_priority_and_strict_no_api_id_typo(self) -> None:
        cfg = Config()
        creds = resolve_credentials(
            cfg,
            {
                "ALI_API_KEY": "ali_suffix_v",
                "API_KEY_ALI": "ali_prefix_v",
                "DASHSCOPE_API_KEY": "dash_canonical_v",
                "HUOSHAN_API_KEY": "hs_suffix_v",
                "API_KEY_HUOSHAN": "hs_prefix_v",
                "VOLCENGINE_API_KEY": "volc_suffix_v",
                "ARK_API_KEY": "ark_canonical_v",
                "DOUBAO_ASR_APP_ID": "db_appid_suffix",
                "APP_ID_DOUBAO_ASR": "db_appid_prefix",
                "DOUBAO_SPEECH_API_KEY": "db_speech_canonical",
                "DOUBAO_ASR_API_KEY": "db_key_suffix",
                "API_KEY_DOUBAO_ASR": "db_key_prefix",
                "OPENROUTER_API_KEY": "or_suffix_v",
                "API_KEY_OPENROUTER": "or_prefix_v",
                "GEMINI_API_KEY": "gem_suffix_v",
                "API_KEY_GEMINI": "gem_prefix_v",
                "OPENAI_API_KEY": "oai_suffix_v",
                "API_KEY_OPENAI": "oai_prefix_v",
                "GROQ_API_KEY": "groq_suffix_v",
                "API_KEY_GROQ": "groq_prefix_v",
                "SILICONFLOW_API_KEY": "sf_suffix_v",
                "API_KEY_SILICONFLOW": "sf_prefix_v",
            },
        )
        assert creds.dashscope is not None
        assert creds.volcengine is not None
        assert creds.doubao_app_id is not None
        assert creds.doubao_key is not None
        assert creds.openrouter is not None
        assert creds.gemini is not None
        assert creds.openai is not None
        assert creds.groq is not None
        assert creds.siliconflow is not None
        self.assertEqual(
            (creds.dashscope.value, creds.dashscope.source_env),
            ("dash_canonical_v", "DASHSCOPE_API_KEY"),
        )
        self.assertEqual(
            (creds.volcengine.value, creds.volcengine.source_env),
            ("ark_canonical_v", "ARK_API_KEY"),
        )
        self.assertEqual(
            CREDENTIAL_ENV_ORDER["doubao_key"],
            ("DOUBAO_SPEECH_API_KEY", "DOUBAO_ASR_API_KEY", "API_KEY_DOUBAO_ASR"),
        )
        self.assertEqual(
            (creds.doubao_app_id.value, creds.doubao_app_id.source_env),
            ("db_appid_suffix", "DOUBAO_ASR_APP_ID"),
        )
        self.assertEqual(
            (creds.doubao_key.value, creds.doubao_key.source_env),
            ("db_speech_canonical", "DOUBAO_SPEECH_API_KEY"),
        )
        # Doubao key priority fallback chain: DOUBAO_ASR_API_KEY > API_KEY_DOUBAO_ASR
        creds_db_legacy1 = resolve_credentials(
            cfg,
            {
                "DOUBAO_ASR_API_KEY": "db_key_suffix",
                "API_KEY_DOUBAO_ASR": "db_key_prefix",
            },
        )
        assert creds_db_legacy1.doubao_key is not None
        self.assertEqual(
            (creds_db_legacy1.doubao_key.value, creds_db_legacy1.doubao_key.source_env),
            ("db_key_suffix", "DOUBAO_ASR_API_KEY"),
        )
        creds_db_legacy2 = resolve_credentials(cfg, {"API_KEY_DOUBAO_ASR": "db_key_prefix"})
        assert creds_db_legacy2.doubao_key is not None
        self.assertEqual(
            (creds_db_legacy2.doubao_key.value, creds_db_legacy2.doubao_key.source_env),
            ("db_key_prefix", "API_KEY_DOUBAO_ASR"),
        )
        self.assertEqual(
            (creds.openrouter.value, creds.openrouter.source_env),
            ("or_suffix_v", "OPENROUTER_API_KEY"),
        )
        self.assertEqual(
            (creds.gemini.value, creds.gemini.source_env), ("gem_suffix_v", "GEMINI_API_KEY")
        )
        self.assertEqual(
            (creds.openai.value, creds.openai.source_env), ("oai_suffix_v", "OPENAI_API_KEY")
        )
        self.assertEqual(
            (creds.groq.value, creds.groq.source_env), ("groq_suffix_v", "GROQ_API_KEY")
        )
        self.assertEqual(
            (creds.siliconflow.value, creds.siliconflow.source_env),
            ("sf_suffix_v", "SILICONFLOW_API_KEY"),
        )

        # Fallback aliases work cleanly when canonical key is absent
        creds_alias = resolve_credentials(
            cfg,
            {
                "ALI_API_KEY": "ali_alias_v",
                "VOLCENGINE_API_KEY": "volc_alias_v",
                "API_KEY_OPENROUTER": "or_alias_v",
                "API_KEY_GROQ": "groq_alias_v",
                "API_KEY_SILICONFLOW": "sf_alias_v",
            },
        )
        assert creds_alias.dashscope is not None
        assert creds_alias.volcengine is not None
        assert creds_alias.openrouter is not None
        assert creds_alias.groq is not None
        assert creds_alias.siliconflow is not None
        self.assertEqual(creds_alias.dashscope.source_env, "ALI_API_KEY")
        self.assertEqual(creds_alias.volcengine.source_env, "VOLCENGINE_API_KEY")
        self.assertEqual(creds_alias.openrouter.source_env, "API_KEY_OPENROUTER")
        self.assertEqual(creds_alias.groq.source_env, "API_KEY_GROQ")
        self.assertEqual(creds_alias.siliconflow.source_env, "API_KEY_SILICONFLOW")

        creds_hs = resolve_credentials(cfg, {"HUOSHAN_API_KEY": "hs_alias_v"})
        assert creds_hs.volcengine is not None
        self.assertEqual(creds_hs.volcengine.source_env, "HUOSHAN_API_KEY")

        self.assertNotIn("dash_canonical_v", repr(creds))
        self.assertNotIn("dash_canonical_v", str(creds.dashscope))
        self.assertIn("DASHSCOPE_API_KEY", str(creds.dashscope))

        # REDLINE REGRESSION: typo API_ID_DOUBAO_ASR / DOUBAO_ASR_API_ID MUST return None!
        for group_names in CREDENTIAL_ENV_ORDER.values():
            for name in group_names:
                self.assertNotIn("API_ID", name)
        creds_typo = resolve_credentials(
            cfg,
            {
                "API_ID_DOUBAO_ASR": "999888777",
                "DOUBAO_ASR_API_ID": "666555444",
                "DOUBAO_ASR_API_KEY": "valid_db_key",
            },
        )
        self.assertIsNone(creds_typo.doubao_app_id)
        self.assertIsNotNone(creds_typo.doubao_key)

    def test_auto_provider_selection_and_custom_api_key_env_isolation(self) -> None:
        cfg = Config()
        self.assertEqual(
            ASR_AUTO_PRIORITY,
            ("dashscope", "doubao", "deepseek", "openai", "gemini", "groq", "openrouter", "siliconflow"),
        )
        # Full ordered priority chain check (canonical DOUBAO_SPEECH_API_KEY)
        full_env = {
            "DASHSCOPE_API_KEY": "k_dash",
            "DOUBAO_SPEECH_API_KEY": "k_db",
            "DEEPSEEK_API_KEY": "k_ds",
            "OPENAI_API_KEY": "k_oai",
            "GEMINI_API_KEY": "k_gem",
            "GROQ_API_KEY": "k_groq",
            "OPENROUTER_API_KEY": "k_or",
            "SILICONFLOW_API_KEY": "k_sf",
        }
        expected_chain = [
            ("DASHSCOPE_API_KEY", "dashscope"),
            ("DOUBAO_SPEECH_API_KEY", "doubao"),
            ("DEEPSEEK_API_KEY", "deepseek"),
            ("OPENAI_API_KEY", "openai"),
            ("GEMINI_API_KEY", "gemini"),
            ("GROQ_API_KEY", "groq"),
            ("OPENROUTER_API_KEY", "openrouter"),
            ("SILICONFLOW_API_KEY", "siliconflow"),
        ]
        cur_env = dict(full_env)
        for env_key, expected_prov in expected_chain:
            self.assertEqual(
                select_asr_provider(cfg, resolve_credentials(cfg, cur_env)),
                expected_prov,
            )
            cur_env.pop(env_key)
        # Empty env defaults to dashscope
        self.assertEqual(select_asr_provider(cfg, resolve_credentials(cfg, {})), "dashscope")

        # Custom third-party api_key_env with provider='auto' MUST NOT jump to doubao!
        cfg_groq = Config()
        cfg_groq.asr.api_key_env = "GROQ_CUSTOM_KEY"
        env_mixed = {"GROQ_CUSTOM_KEY": "groq_val_123", "DOUBAO_ASR_API_KEY": "db_val_456"}
        self.assertEqual(
            select_asr_provider(cfg_groq, resolve_credentials(cfg_groq, env_mixed)),
            "openai-compatible",
        )

        # Explicit Doubao resource_id under provider='auto' MUST route to doubao even if DashScope key exists
        cfg_db_rid = Config()
        cfg_db_rid.asr.resource_id = "volc.seedasr.auc"
        self.assertEqual(
            select_asr_provider(
                cfg_db_rid,
                resolve_credentials(cfg_db_rid, {"DASHSCOPE_API_KEY": "a", "DOUBAO_ASR_API_KEY": "d"}),
            ),
            "doubao",
        )

    def test_redact_secrets_dict_repr_custom_env_and_no_false_positive_on_tokens_count(self) -> None:
        dyn_sk = "sk-" + ("z" * 20)
        dyn_aiza = "AIza" + ("Q" * 24)
        url_raw_secret = "sec+val/with=chars"
        url_pct_secret = urllib.parse.quote(url_raw_secret, safe="")
        cfg_toml_custom = Config()
        cfg_toml_custom.asr.api_key_env = "CUSTOM_ONLY_IN_TOML"
        env = {
            "API_KEY_ALI": "supersecret_short",
            "ALI_API_KEY": "supersecret_short_extended_tail",
            "DOUBAO_SPEECH_API_KEY": "db_speech_secret_xyz",
            "APP_ID_DOUBAO_ASR": "appid_val_98765",
            "GEMINI_API_KEY": url_raw_secret,
            "VOICE_INPUT_HOTKEY": "RControl",
            "CUSTOM_ONLY_IN_TOML": "toml_custom_provider_secret",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "16000",
            "TIKTOKEN_CACHE_DIR": "/tmp/tiktoken_cache",
        }
        py_dict_repr = repr(
            {
                "X-Api-Key": "untracked_hdr_secret",
                "X-Goog-Api-Key": "untracked_goog_secret",
                "x-api-app-key": "untracked_appid_999",
            }
        )
        escaped_json_frag = r'{\"api_key\": \"esc_part1\\\"esc_part2\"}'
        raw = (
            f"err with supersecret_short_extended_tail and supersecret_short; "
            f"db_speech=db_speech_secret_xyz; appid=appid_val_98765; toml_custom=toml_custom_provider_secret; "
            f"Authorization: Bearer tok_xyz_123456; "
            f"Authorization: Basic dXNlcjpwYXNzX3NlY3JldA==; "
            f"Authorization: Token raw_tok_987654; "
            f"url=https://svcuser:url_inline_pwd@api.example.com/v1?k={url_pct_secret}; "
            f"x-goog-api-key: {dyn_aiza}; raw={dyn_sk}; "
            f"headers={py_dict_repr}; escaped={escaped_json_frag}; "
            f"sr=16000; cache=/tmp/tiktoken_cache; hotkey=RControl; "
            f'{{"access_key": "inline_json_secret", "x-api-key": "json_hdr_secret"}}'
        )
        cleaned = redact_secrets(raw, env, cfg=cfg_toml_custom)
        for forbidden in (
            "supersecret",
            "_extended_tail",
            "db_speech_secret_xyz",
            "appid_val_98765",
            "toml_custom_provider_secret",
            "tok_xyz_123456",
            "dXNlcjpwYXNzX3NlY3JldA==",
            "raw_tok_987654",
            "url_inline_pwd",
            url_pct_secret,
            "esc_part1",
            "esc_part2",
            dyn_aiza,
            dyn_sk,
            "untracked_hdr_secret",
            "untracked_goog_secret",
            "untracked_appid_999",
            "inline_json_secret",
            "json_hdr_secret",
        ):
            self.assertNotIn(forbidden, cleaned)
        # Non-secret numeric / path env vars MUST NOT corrupt normal log text!
        self.assertIn("sr=16000", cleaned)
        self.assertIn("cache=/tmp/tiktoken_cache", cleaned)
        self.assertIn("hotkey=RControl", cleaned)
        self.assertIn("[REDACTED]", cleaned)

        # Nested json.dumps preserves valid outer & inner JSON (including trailing backslash)
        nested = json.dumps(
            {
                "err": json.dumps(
                    {"api_key": "nested_secret_bs\\", "token": 'nested_quote_"_val'}
                )
            }
        )
        redacted_nested = redact_secrets(nested, {})
        self.assertNotIn("nested_secret_bs", redacted_nested)
        self.assertNotIn("nested_quote_", redacted_nested)
        outer_obj = json.loads(redacted_nested)
        inner_obj = json.loads(outer_obj["err"])
        self.assertEqual(inner_obj, {"api_key": "[REDACTED]", "token": "[REDACTED]"})

        # Pathological unclosed backslash sequences MUST run in strict O(N) without ReDoS
        evil = (
            'Authorization: "' + ("\\" * 4000) + "\n"
            + "x-api-key: '" + ("\\" * 4000) + "\n"
            + r'{\"api_key\": \"' + ("\\" * 4000)
        )
        t0 = time.monotonic()
        _ = redact_secrets(evil, {})
        self.assertLess(time.monotonic() - t0, 0.25)

    def test_runtime_dir_creation_mode_0700_symlink_and_owner_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rt_xdg = get_runtime_dir({"XDG_RUNTIME_DIR": tmp}, create=True)
            self.assertEqual(rt_xdg, Path(tmp) / "agent-voice-input")
            self.assertEqual(rt_xdg.stat().st_mode & 0o777, 0o700)

            rt = get_runtime_dir(
                {"XDG_RUNTIME_DIR": "   ", "HOME": tmp}, create=True
            )
            self.assertEqual(rt, Path(tmp) / ".cache" / "agent-voice-input")
            self.assertEqual(rt.stat().st_mode & 0o777, 0o700)

            rt_override = get_runtime_dir(
                {
                    "AGENT_VOICE_INPUT_RUNTIME_DIR": str(Path(tmp) / "custom-rt"),
                    "VOICE_INPUT_RUNTIME_DIR": str(Path(tmp) / "legacy-rt"),
                },
                create=True,
            )
            self.assertEqual(rt_override, Path(tmp) / "custom-rt")

            # Reject wrong owner UID strictly (no silent bypass when expected_uid is specified)
            with self.assertRaises(ConfigError):
                ensure_secure_dir(rt, expected_uid=os.getuid() + 1)

            # Reject pre-existing symlink or dangling symlink
            real_sub = Path(tmp) / "real_sub"
            real_sub.mkdir()
            sym_dir = Path(tmp) / "sym_dir"
            os.symlink(real_sub, sym_dir)
            with self.assertRaises(ConfigError):
                ensure_secure_dir(sym_dir)
            dangling = Path(tmp) / "dangling_sym"
            os.symlink(Path(tmp) / "missing_target", dangling)
            with self.assertRaises(ConfigError):
                ensure_secure_dir(dangling)

    def test_max_retries_defaults_overrides_validation_and_redaction(self) -> None:
        dcfg = Config()
        self.assertEqual(dcfg.asr.max_retries, 2)
        self.assertEqual(dcfg.refiner.max_retries, 2)

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            cfg_path = home / "config.toml"
            cfg_path.write_text(
                "[asr]\nmax_retries = 0\n\n[refiner]\nmax_retries = 4\n",
                encoding="utf-8",
            )
            c_toml, _ = load_config(config_path=cfg_path, env={"HOME": td}, home_dir=home)
            self.assertEqual(c_toml.asr.max_retries, 0)
            self.assertEqual(c_toml.refiner.max_retries, 4)

            # *_MAX_RETRIES env vars are ignored (ordinary config lives in config.toml / cli_overrides)
            env_ov = {
                "HOME": td,
                "AGENT_VOICE_INPUT_ASR_MAX_RETRIES": "5",
                "VOICE_INPUT_REFINER_MAX_RETRIES": "1",
            }
            c_env, _ = load_config(config_path=cfg_path, env=env_ov, home_dir=home)
            self.assertEqual(c_env.asr.max_retries, 0)
            self.assertEqual(c_env.refiner.max_retries, 4)

            c_cli, _ = load_config(
                config_path=cfg_path,
                env=env_ov,
                home_dir=home,
                cli_overrides={"asr.max_retries": 5, "refiner.max_retries": 1},
            )
            self.assertEqual(c_cli.asr.max_retries, 5)
            self.assertEqual(c_cli.refiner.max_retries, 1)

            msg = redact_secrets("HTTP 502 after 5 attempts (code=1)", env_ov, cfg=c_cli)
            self.assertEqual(msg, "HTTP 502 after 5 attempts (code=1)")

            for bad_toml in (
                "[asr]\nmax_retries = -1\n",
                "[asr]\nmax_retries = 11\n",
                "[asr]\nmax_retries = true\n",
                '[asr]\nmax_retries = "abc"\n',
                "[refiner]\nmax_retries = 1.5\n",
                "[refiner]\nmax_retries = -2\n",
            ):
                cfg_path.write_text(bad_toml, encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load_config(config_path=cfg_path, env={"HOME": td}, home_dir=home)

    def test_v020_legacy_provider_rejection_and_stage_validation_matrix(self) -> None:
        self.assertEqual(
            VALID_ASR_PROVIDERS,
            frozenset({"auto", "dashscope", "doubao", "openrouter", "gemini", "openai", "deepseek", "groq", "siliconflow", "openai-compatible"}),
        )
        self.assertEqual(
            VALID_REFINER_PROVIDERS,
            frozenset({"auto", "codex", "agy", "deepseek", "dashscope", "volcengine", "openrouter", "gemini", "openai", "groq", "siliconflow", "openai-compatible", "off"}),
        )
        self.assertEqual(VALID_ASR_APIS, frozenset({"auto", "audio-transcriptions", "chat-completions"}))
        self.assertEqual(
            VALID_EFFORTS,
            frozenset({"auto", "default", "none", "minimal", "low", "medium", "high", "xhigh", "max"}),
        )

        # Default Config uses canonical v0.10-1 auto-routing values without polluting deferred model/effort
        dcfg = Config()
        self.assertEqual(dcfg.asr.provider, "auto")
        self.assertEqual(dcfg.asr.api, "auto")
        self.assertEqual(dcfg.asr.effort, "auto")
        self.assertEqual(dcfg.refiner.provider, "auto")
        self.assertEqual(dcfg.refiner.model, "")
        self.assertEqual(dcfg.refiner.effort, "")
        self.assertEqual(dcfg.refiner.effective_model("codex"), "gpt-6-luna")
        self.assertEqual(dcfg.refiner.effective_effort("codex"), "low")
        self.assertEqual(dcfg.refiner.effective_model("deepseek"), "deepseek-flash")
        self.assertEqual(dcfg.refiner.effective_effort("deepseek"), "auto")

        # Built-in default models, efforts & base_urls strictly match v0.9-1 / v0.10-1 table
        self.assertEqual(DEFAULT_ASR_MODELS["dashscope"], "qwen3-asr-flash")
        self.assertEqual(DEFAULT_ASR_MODELS["doubao"], "bigmodel")
        self.assertEqual(DEFAULT_ASR_MODELS["openrouter"], "qwen/qwen3-asr-1.7b")
        self.assertEqual(DEFAULT_ASR_MODELS["gemini"], "gemini-3.7-flash")
        self.assertEqual(DEFAULT_ASR_MODELS["openai"], "gpt-transcribe")
        self.assertEqual(DEFAULT_ASR_MODELS["deepseek"], "deepseek-asr")
        self.assertEqual(DEFAULT_ASR_MODELS["groq"], "whisper-large-v3-turbo")
        self.assertEqual(DEFAULT_ASR_MODELS["siliconflow"], "FunAudioLLM/SenseVoiceSmall")
        self.assertEqual(DEFAULT_ASR_MODELS["openai-compatible"], "gpt-4o-mini-transcribe")

        self.assertEqual(DEFAULT_REFINER_MODELS["codex"], "gpt-6-luna")
        self.assertEqual(DEFAULT_REFINER_MODELS["agy"], "gemini-3.8-flash")
        self.assertEqual(DEFAULT_REFINER_MODELS["deepseek"], "deepseek-flash")
        self.assertEqual(DEFAULT_REFINER_MODELS["dashscope"], "qwen3.8-flash")
        self.assertEqual(DEFAULT_REFINER_MODELS["volcengine"], "doubao-seed-2-0-mini-260428")
        self.assertEqual(DEFAULT_REFINER_MODELS["openrouter"], "google/gemini-3.8-flash")
        self.assertEqual(DEFAULT_REFINER_MODELS["gemini"], "gemini-3.7-flash")
        self.assertEqual(DEFAULT_REFINER_MODELS["openai"], "gpt-6-luna")
        self.assertEqual(DEFAULT_REFINER_MODELS["groq"], "llama-3.3-70b-versatile")
        self.assertEqual(DEFAULT_REFINER_MODELS["siliconflow"], "Qwen/Qwen2.5-7B-Instruct")

        self.assertEqual(DEFAULT_REFINER_EFFORTS, {"codex": "low", "agy": "low", "openai": "low"})
        self.assertEqual(RefinerConfig(provider="codex").effort, "low")
        self.assertEqual(RefinerConfig(provider="openai").effort, "low")
        for non_codex in ("deepseek", "openrouter", "dashscope", "volcengine", "gemini", "openai-compatible"):
            self.assertEqual(RefinerConfig(provider=non_codex).effort, "auto")

        self.assertEqual(DEFAULT_ASR_BASE_URLS["openrouter"], "https://openrouter.ai/api/v1")
        self.assertEqual(DEFAULT_ASR_BASE_URLS["deepseek"], "https://api.deepseek.com/v1")
        self.assertEqual(DEFAULT_ASR_BASE_URLS["groq"], "https://api.groq.com/openai/v1")
        self.assertEqual(DEFAULT_ASR_BASE_URLS["siliconflow"], "https://api.siliconflow.cn/v1")
        self.assertEqual(DEFAULT_REFINER_BASE_URLS["deepseek"], "https://api.deepseek.com/v1")
        self.assertEqual(DEFAULT_REFINER_BASE_URLS["volcengine"], "https://ark.cn-beijing.volces.com/api/v3")
        self.assertEqual(DEFAULT_REFINER_BASE_URLS["dashscope"], "https://dashscope.aliyuncs.com/compatible-mode/v1")

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            # Legacy v0.1.0 aliases and genuinely stage-incompatible providers MUST raise ConfigError
            for bad_asr in ("ali", "qwen", "gemini-flash-audio", "codex-luna", "codex", "off"):
                with self.subTest(bad_asr=bad_asr):
                    with self.assertRaises(ConfigError):
                        load_config(env={"HOME": str(home)}, home_dir=home, cli_overrides={"asr.provider": bad_asr})
            # DeepSeek ASR defaults, single-hop=False, base_url auto-inference, and asr.api="auto" enforcement
            cfg_ds_asr, _ = load_config(
                env={"HOME": str(home)},
                home_dir=home,
                cli_overrides={"asr.provider": "deepseek"},
            )
            self.assertEqual(cfg_ds_asr.asr.effective_model("deepseek"), "deepseek-asr")
            self.assertEqual(cfg_ds_asr.asr.effective_base_url("deepseek"), "https://api.deepseek.com/v1")
            self.assertEqual(
                resolve_asr_api("deepseek", "", "auto"),
                "deepseek-native",
            )
            self.assertEqual(classify_asr_model_api("deepseek-asr"), "audio-transcriptions")
            for bad_ds_api in ("audio-transcriptions", "chat-completions"):
                with self.assertRaises(ConfigError):
                    load_config(
                        env={"HOME": str(home)},
                        home_dir=home,
                        cli_overrides={"asr.provider": "deepseek", "asr.api": bad_ds_api},
                    )
                with self.assertRaises(ConfigError):
                    load_config(
                        env={"HOME": str(home), "DEEPSEEK_API_KEY": "sk-ds-auto-validate"},
                        home_dir=home,
                        cli_overrides={"asr.api": bad_ds_api},
                        load_default_env_files=False,
                    )
            cfg_ds_url, _ = load_config(
                env={"HOME": str(home)},
                home_dir=home,
                cli_overrides={"asr.base_url": "https://api.deepseek.com/v1/tools/asr"},
            )
            self.assertEqual(select_asr_provider(cfg_ds_url, resolve_credentials(cfg_ds_url, {})), "deepseek")
            for bad_refiner in ("codex-luna", "ali", "qwen", "gemini-flash-audio"):
                with self.subTest(bad_refiner=bad_refiner):
                    with self.assertRaises(ConfigError):
                        load_config(env={"HOME": str(home)}, home_dir=home, cli_overrides={"refiner.provider": bad_refiner})
            # Cross-stage Volcengine/Doubao aliases normalize cleanly to each stage's canonical provider
            cfg_asr_volc, _ = load_config(
                env={"HOME": str(home)}, home_dir=home, cli_overrides={"asr.provider": "volcengine"}
            )
            self.assertEqual(cfg_asr_volc.asr.provider, "doubao")
            cfg_ref_db, _ = load_config(
                env={"HOME": str(home)}, home_dir=home, cli_overrides={"refiner.provider": "doubao"}
            )
            self.assertEqual(cfg_ref_db.refiner.provider, "volcengine")
            self.assertEqual(cfg_ref_db.refiner.model, DEFAULT_REFINER_MODELS["volcengine"])

            # openai-compatible requires explicit base_url (and explicit model on Refiner)
            with self.assertRaises(ConfigError):
                load_config(env={"HOME": str(home)}, home_dir=home, cli_overrides={"asr.provider": "openai-compatible"})
            ok_oai_asr, _ = load_config(
                env={"HOME": str(home)},
                home_dir=home,
                cli_overrides={"asr.provider": "openai-compatible", "asr.base_url": "http://127.0.0.1:8000/v1"},
            )
            self.assertEqual(ok_oai_asr.asr.effective_model(), "gpt-4o-mini-transcribe")

            with self.assertRaises(ConfigError):
                load_config(
                    env={"HOME": str(home)},
                    home_dir=home,
                    cli_overrides={"refiner.provider": "openai-compatible", "refiner.base_url": "http://127.0.0.1:8000/v1"},
                )
            with self.assertRaises(ConfigError):
                load_config(
                    env={"HOME": str(home)},
                    home_dir=home,
                    cli_overrides={"refiner.provider": "openai-compatible", "refiner.model": "local-qwen"},
                )
            ok_oai_ref, _ = load_config(
                env={"HOME": str(home)},
                home_dir=home,
                cli_overrides={
                    "refiner.provider": "openai-compatible",
                    "refiner.base_url": "http://127.0.0.1:8000/v1",
                    "refiner.model": "local-qwen",
                },
            )
            self.assertEqual(ok_oai_ref.refiner.model, "local-qwen")

    def test_cross_layer_provider_override_resets_inherited_model_and_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cfg_dir = home / ".config" / "agent-voice-input"
            cfg_dir.mkdir(parents=True)
            (cfg_dir / "config.toml").write_text(
                """
                [asr]
                provider = "dashscope"
                model = "fun-asr-realtime"
                api = "auto"

                [refiner]
                provider = "codex"
                model = "gpt-6-luna"
                """,
                encoding="utf-8",
            )
            # 1. CLI overrides provider without model -> resets config.toml model/api to new provider default!
            (cfg_dir / "config.toml").write_text(
                """
                [asr]
                provider = "openrouter"
                model = "google/gemini-2.5-flash"
                api = "chat-completions"

                [refiner]
                provider = "codex"
                model = "gpt-6-luna"
                """,
                encoding="utf-8",
            )
            cfg_cli, _ = load_config(
                env={"HOME": str(home)},
                home_dir=home,
                cli_overrides={
                    "asr.provider": "groq",
                    "refiner.provider": "volcengine",
                },
            )
            self.assertEqual(cfg_cli.asr.provider, "groq")
            self.assertEqual(cfg_cli.asr.model, "")
            self.assertEqual(cfg_cli.asr.api, "auto")
            self.assertEqual(cfg_cli.asr.effective_model(), "whisper-large-v3-turbo")
            self.assertEqual(cfg_cli.refiner.provider, "volcengine")
            self.assertEqual(cfg_cli.refiner.model, "doubao-seed-2-0-mini-260428")
            self.assertEqual(cfg_cli.refiner.effort, "auto")

            # 2. Same-layer provider + explicit model preserves explicit model
            cfg_both, _ = load_config(
                env={"HOME": str(home)},
                home_dir=home,
                cli_overrides={
                    "asr.provider": "openrouter",
                    "asr.model": "qwen/qwen3.8-omni-flash",
                    "refiner.provider": "openrouter",
                    "refiner.model": "anthropic/claude-haiku-4.5",
                },
            )
            self.assertEqual(cfg_both.asr.model, "qwen/qwen3.8-omni-flash")
            self.assertEqual(cfg_both.refiner.model, "anthropic/claude-haiku-4.5")

            # 4. Switching away from openai-compatible in CLI clears inherited local base_url / api_key_env
            (cfg_dir / "config.toml").write_text(
                """
                [asr]
                provider = "openai-compatible"
                base_url = "http://127.0.0.1:8000/v1"
                model = "faster-whisper"
                api_key_env = "LOCAL_ASR_KEY"

                [refiner]
                provider = "openai-compatible"
                base_url = "http://127.0.0.1:8001/v1"
                model = "local-qwen"
                api_key_env = "LOCAL_REF_KEY"
                """,
                encoding="utf-8",
            )
            cfg_sw, _ = load_config(
                env={"HOME": str(home)},
                home_dir=home,
                cli_overrides={"asr.provider": "openrouter", "refiner.provider": "openrouter"},
            )
            self.assertEqual(cfg_sw.asr.effective_base_url(), "https://openrouter.ai/api/v1")
            self.assertEqual(cfg_sw.asr.effective_model(), "qwen/qwen3-asr-1.7b")
            self.assertEqual(cfg_sw.asr.api_key_env, "")
            self.assertEqual(cfg_sw.refiner.effective_base_url(), "https://openrouter.ai/api/v1")
            self.assertEqual(cfg_sw.refiner.model, "google/gemini-3.8-flash")
            self.assertEqual(cfg_sw.refiner.effort, "auto")
            self.assertEqual(cfg_sw.refiner.api_key_env, "")

            # 5. Switching provider resets inherited effort unless same-layer effort is explicitly given;
            #    config.example.toml loads cleanly with auto -> deferred effort="" (codex -> 'low', dashscope -> 'auto')
            cfg_ex, _ = load_config(
                config_path=_REPO_ROOT / "config.example.toml",
                env={"HOME": str(home)},
                home_dir=home,
                load_default_env_files=False,
            )
            self.assertEqual(cfg_ex.refiner.provider, "auto")
            self.assertEqual(cfg_ex.refiner.effort, "")
            self.assertEqual(cfg_ex.refiner.effective_effort("codex"), "low")
            cfg_ex_or, _ = load_config(
                config_path=_REPO_ROOT / "config.example.toml",
                env={"HOME": str(home)},
                home_dir=home,
                cli_overrides={"refiner.provider": "dashscope"},
                load_default_env_files=False,
            )
            self.assertEqual(cfg_ex_or.refiner.effort, "auto")
            cfg_ex_keep, _ = load_config(
                config_path=_REPO_ROOT / "config.example.toml",
                env={"HOME": str(home)},
                home_dir=home,
                cli_overrides={"refiner.provider": "dashscope", "refiner.effort": "medium"},
                load_default_env_files=False,
            )
            self.assertEqual(cfg_ex_keep.refiner.effort, "medium")

            # 6. DashScope Qwen-Omni multimodal chat model detection (rejects stream-only legacy/realtime Omni)
            self.assertTrue(is_dashscope_omni_chat_model("qwen3.8-omni-flash"))
            self.assertTrue(is_dashscope_omni_chat_model("qwen3-omni-flash"))
            self.assertFalse(is_dashscope_omni_chat_model("qwen3.5-omni-plus"))
            self.assertFalse(is_dashscope_omni_chat_model("qwen-omni-turbo"))
            self.assertFalse(is_dashscope_omni_chat_model("qwen3-omni-flash-realtime"))
            self.assertFalse(is_dashscope_omni_chat_model("qwen3-asr-flash"))
            self.assertFalse(is_dashscope_omni_chat_model("qwen-audio-turbo-latest"))
            with self.assertRaises(ConfigError):
                resolve_asr_api("dashscope", "qwen3.5-omni-plus", "auto")
            with self.assertRaises(ConfigError):
                resolve_asr_api("dashscope", "qwen3-omni-flash-realtime", "auto")

    def test_asr_api_auto_classification_and_native_incompatibility_checks(self) -> None:
        # Dedicated STT models -> audio-transcriptions (including gemini-*-transcribe & voxtral-mini-3b-2507 / -stt)
        for stt_model in (
            "qwen/qwen3-asr-1.7b",
            "openai/whisper-large-v3-turbo",
            "openai/gpt-transcribe",
            "openai/gpt-4o-mini-transcribe",
            "google/gemini-3.5-transcribe",
            "deepgram/nova-3",
            "assemblyai/universal-3-6-pro",
            "elevenlabs/scribe-v1",
            "fish-audio/fish-speech-1.5",
            "nvidia/parakeet-tdt-0.6b-v2",
            "google/chirp-2",
            "FunAudioLLM/SenseVoiceSmall",
            "TeleAI/TeleSpeechASR",
            "mistralai/voxtral-mini-3b-2507",
            "mistralai/voxtral-small-24b-2507-stt",
        ):
            with self.subTest(stt_model=stt_model):
                self.assertEqual(classify_asr_model_api(stt_model), "audio-transcriptions")
                self.assertEqual(resolve_asr_api("openrouter", stt_model, "auto"), "audio-transcriptions")

        # Multimodal audio chat models -> chat-completions
        for chat_model in (
            "google/gemini-2.5-flash",
            "google/gemini-3-flash-preview",
            "openai/gpt-audio-mini",
            "openai/gpt-4o-audio-preview",
            "qwen/qwen3.8-omni-flash",
            "mistralai/voxtral-small-24b-2507",
            "xiaomi/mimo-audio-7b",
            "muse-audio-1",
        ):
            with self.subTest(chat_model=chat_model):
                self.assertEqual(classify_asr_model_api(chat_model), "chat-completions")
                self.assertEqual(resolve_asr_api("openrouter", chat_model, "auto"), "chat-completions")

        # Unknown model: OpenRouter + api="auto" raises ConfigError; explicit api succeeds; other 4 default to audio-transcriptions
        self.assertIsNone(classify_asr_model_api("acme/custom-voice-v1"))
        with self.assertRaises(ConfigError):
            resolve_asr_api("openrouter", "acme/custom-voice-v1", "auto")
        self.assertEqual(
            resolve_asr_api("openrouter", "acme/custom-voice-v1", "audio-transcriptions"),
            "audio-transcriptions",
        )
        self.assertEqual(
            resolve_asr_api("openrouter", "acme/custom-voice-v1", "chat-completions"),
            "chat-completions",
        )
        for other_prov in ("openai", "groq", "siliconflow", "openai-compatible"):
            self.assertEqual(
                resolve_asr_api(other_prov, "acme/custom-voice-v1", "auto"),
                "audio-transcriptions",
            )

        # Native/dedicated providers reject incompatible explicit asr.api at config validation time
        with self.assertRaises(ConfigError):
            resolve_asr_api("doubao", "bigmodel", "audio-transcriptions")
        with self.assertRaises(ConfigError):
            resolve_asr_api("doubao", "bigmodel", "chat-completions")
        with self.assertRaises(ConfigError):
            resolve_asr_api("dashscope", "qwen3-asr-flash", "audio-transcriptions")
        with self.assertRaises(ConfigError):
            resolve_asr_api("dashscope", "fun-asr-realtime", "chat-completions")
        with self.assertRaises(ConfigError):
            resolve_asr_api("gemini", "gemini-2.5-flash", "audio-transcriptions")
        self.assertEqual(resolve_asr_api("dashscope", "qwen3-asr-flash", "auto"), "chat-completions")
        self.assertEqual(resolve_asr_api("dashscope", "fun-asr-realtime", "auto"), "dashscope-native")
        self.assertEqual(resolve_asr_api("gemini", "gemini-2.5-flash", "auto"), "gemini-native")

        # Regression: asr.provider="auto" with explicit hints validates openai-compatible base_url
        # AND OpenRouter unknown-model api="auto" immediately in load_config (even without env vars set!)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cfg_dir = home / ".config" / "agent-voice-input"
            cfg_dir.mkdir(parents=True)

            # 1) Custom api_key_env inferring openai-compatible without base_url -> ConfigError
            (cfg_dir / "config.toml").write_text(
                '[asr]\nprovider = "auto"\napi_key_env = "CUSTOM_THIRD_PARTY_STT_KEY"\n',
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                load_config(env={"HOME": str(home)}, home_dir=home)

            # Adding base_url makes it valid
            (cfg_dir / "config.toml").write_text(
                '[asr]\nprovider = "auto"\napi_key_env = "CUSTOM_THIRD_PARTY_STT_KEY"\nbase_url = "http://127.0.0.1:8000/v1"\n',
                encoding="utf-8",
            )
            ok_hint_cfg, _ = load_config(env={"HOME": str(home)}, home_dir=home)
            self.assertEqual(ok_hint_cfg.asr.base_url, "http://127.0.0.1:8000/v1")

            # 2) api_key_env = "OPENROUTER_API_KEY" + unknown model + api="auto" -> ConfigError even with empty env
            (cfg_dir / "config.toml").write_text(
                '[asr]\nprovider = "auto"\napi_key_env = "OPENROUTER_API_KEY"\nmodel = "acme/unknown-voice-model"\napi = "auto"\n',
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                load_config(env={"HOME": str(home)}, home_dir=home)

    def test_redact_secrets_covers_gsk_openrouter_and_all_v020_credential_envs(self) -> None:
        gsk_tok = "gsk_" + ("q" * 20)
        or_tok = "sk-or-v1-" + ("r" * 20)
        env = {
            "DASHSCOPE_API_KEY": "dash_" + ("a" * 12),
            "ARK_API_KEY": "ark_" + ("b" * 12),
            "VOLCENGINE_API_KEY": "volc_" + ("c" * 12),
            "OPENROUTER_API_KEY": "or_" + ("d" * 12),
            "GROQ_API_KEY": "groq_" + ("e" * 12),
            "SILICONFLOW_API_KEY": "sf_" + ("f" * 12),
        }
        raw = " ".join([*env.values(), gsk_tok, or_tok])
        redacted = redact_secrets(raw, env)
        for secret in (*env.values(), gsk_tok, or_tok):
            self.assertNotIn(secret, redacted)

    def test_audio_silence_trim_config_parsing_and_validation(self) -> None:
        self.assertEqual(VALID_SILENCE_TRIM, frozenset({"off", "conservative", "balanced"}))
        self.assertEqual(AudioConfig().silence_trim, "balanced")

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cfg_dir = home / ".config" / "agent-voice-input"
            cfg_dir.mkdir(parents=True)

            # Default when omitted is "balanced"
            (cfg_dir / "config.toml").write_text("[audio]\nbeep = false\n", encoding="utf-8")
            default_cfg, _ = load_config(env={"HOME": str(home)}, home_dir=home)
            self.assertEqual(default_cfg.audio.silence_trim, "balanced")

            # Valid values + case/whitespace normalization
            for raw_val, expected in (
                ("off", "off"),
                ("conservative", "conservative"),
                ("balanced", "balanced"),
                ("  Conservative  ", "conservative"),
                (" OFF ", "off"),
                ("BALANCED", "balanced"),
            ):
                (cfg_dir / "config.toml").write_text(
                    f'[audio]\nsilence_trim = "{raw_val}"\n',
                    encoding="utf-8",
                )
                cfg, _ = load_config(env={"HOME": str(home)}, home_dir=home)
                self.assertEqual(cfg.audio.silence_trim, expected)

            # Invalid strings & non-strings (bool false must NOT be coerced to "off")
            for bad_toml in (
                '[audio]\nsilence_trim = "aggressive"\n',
                '[audio]\nsilence_trim = ""\n',
                '[audio]\nsilence_trim = "false"\n',
                "[audio]\nsilence_trim = false\n",
                "[audio]\nsilence_trim = true\n",
                "[audio]\nsilence_trim = 0\n",
            ):
                (cfg_dir / "config.toml").write_text(bad_toml, encoding="utf-8")
                with self.assertRaises(ConfigError) as ctx:
                    load_config(env={"HOME": str(home)}, home_dir=home)
                self.assertIn("audio.silence_trim", str(ctx.exception))


class AudioProcTests(unittest.TestCase):
    def test_rejects_clip_shorter_than_min_duration(self) -> None:
        samples = [10000] * int(0.20 * 16000)
        wav = pack_pcm16_mono_wav(samples)
        res = process_wav(wav)
        self.assertFalse(res.passed)
        self.assertEqual(res.reason, "too_short")
        self.assertEqual(res.wav_bytes, b"")
        self.assertEqual(res.rms_dbfs, -200.0)
        self.assertEqual(res.gain_db, 0.0)
        self.assertAlmostEqual(res.duration_sec, 0.20, places=3)

    def test_silence_gate_boundary_and_all_zero_detection(self) -> None:
        zero_wav = pack_pcm16_mono_wav([0] * 16000)
        zero_res = process_wav(zero_wav)
        self.assertFalse(zero_res.passed)
        self.assertEqual(zero_res.reason, "silence")
        self.assertTrue(zero_res.all_zero)

        sub_floor_wav = pack_pcm16_mono_wav([103, -103] * 4000)
        sub_res = process_wav(sub_floor_wav)
        self.assertFalse(sub_res.passed)
        self.assertEqual(sub_res.reason, "silence")
        self.assertEqual(sub_res.voiced_sec, 0.0)
        self.assertFalse(sub_res.all_zero)

        short_voiced = [2000] * int(0.08 * 16000) + [0] * int(0.42 * 16000)
        sv_res = process_wav(pack_pcm16_mono_wav(short_voiced))
        self.assertFalse(sv_res.passed)
        self.assertEqual(sv_res.reason, "silence")
        self.assertAlmostEqual(sv_res.voiced_sec, 0.08, places=3)

        edge_voiced = [104, -104] * int(0.15 * 8000) + [0] * int(0.35 * 16000)
        edge_res = process_wav(pack_pcm16_mono_wav(edge_voiced))
        self.assertTrue(edge_res.passed)
        self.assertEqual(edge_res.reason, "ok")
        self.assertAlmostEqual(edge_res.voiced_sec, 0.15, places=3)
        self.assertAlmostEqual(edge_res.gain_db, -20.0 - edge_res.rms_dbfs, places=2)

    def test_normalizes_quiet_and_loud_audio_to_minus_20_dbfs_with_peak_clip(self) -> None:
        sr = 16000
        target_in_rms = 10.0 ** (-38.0 / 20.0)
        amp = target_in_rms * math.sqrt(2.0) * 32768.0
        voiced_n = int(0.4 * sr)
        sine_samples = [
            int(round(amp * math.sin(2.0 * math.pi * 440.0 * i / sr)))
            for i in range(voiced_n)
        ]
        sine_samples[10] = 30000
        sine_samples[11] = -30000
        padded = sine_samples + [0] * int(0.2 * sr)

        res = process_wav(pack_pcm16_mono_wav(padded))
        self.assertTrue(res.passed)
        self.assertTrue(res.gain_db > 10.0)
        ch, sw, out_sr, out_arr = _unpack_wav_samples(res.wav_bytes)
        self.assertEqual((ch, sw, out_sr), (1, 2, 16000))
        self.assertEqual(len(out_arr), len(padded))
        self.assertEqual(max(out_arr), PEAK_LIMIT_INT16)
        self.assertEqual(min(out_arr), -PEAK_LIMIT_INT16)

        loud_amp = int(round((10.0 ** (-10.0 / 20.0)) * 32768.0))
        loud_samples = [loud_amp, -loud_amp] * int(0.25 * sr)
        loud_res = process_wav(pack_pcm16_mono_wav(loud_samples))
        self.assertTrue(loud_res.passed)
        self.assertAlmostEqual(loud_res.rms_dbfs, -10.0, delta=0.05)
        self.assertAlmostEqual(loud_res.gain_db, -10.0, delta=0.05)
        _, _, _, loud_out = _unpack_wav_samples(loud_res.wav_bytes)
        out_rms = math.sqrt(sum((s / 32768.0) ** 2 for s in loud_out) / len(loud_out))
        out_dbfs = 20.0 * math.log10(out_rms)
        self.assertAlmostEqual(out_dbfs, -20.0, delta=0.05)

    def test_ffmpeg_converts_48k_stereo_wav_to_16k_mono(self) -> None:
        try:
            resolve_ffmpeg_bin()
        except AudioError:
            if _is_ci():
                self.fail("ffmpeg must be installed in CI (silent skip forbidden)")
            self.skipTest("ffmpeg not installed")
        sr_in = 48000
        n_frames = int(0.5 * sr_in)
        stereo = array.array("h")
        for i in range(n_frames):
            v = int(round(4000.0 * math.sin(2.0 * math.pi * 440.0 * i / sr_in)))
            stereo.append(v)
            stereo.append(v)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(sr_in)
            wf.writeframes(stereo.tobytes())

        res = process_wav(memoryview(buf.getvalue()))
        self.assertTrue(res.passed)
        ch, sw, out_sr, out_arr = _unpack_wav_samples(res.wav_bytes)
        self.assertEqual((ch, sw, out_sr), (1, 2, 16000))
        self.assertAlmostEqual(len(out_arr) / 16000.0, 0.5, delta=0.03)

    def test_big_endian_fast_path_returns_canonical_s16le_bytes(self) -> None:
        #Sample 0x1234 in s16le is b'\x34\x12'; on big-endian hosts wave.readframes() swaps to b'\x12\x34'
        wav_le = pack_pcm16_mono_wav(b"\x34\x12\xcd\xab")
        with mock.patch("wave.sys.byteorder", "big"), mock.patch("agent_voice_input.audio_proc.sys.byteorder", "big"):
            pcm_le = decode_to_16k_mono_pcm(wav_le)
        self.assertEqual(pcm_le, b"\x34\x12\xcd\xab")

    def test_streaming_pipe_recorder_zero_loss_on_sigkill_and_live_ffmpeg_drain(self) -> None:
        from agent_voice_input.platform_wsl import FfmpegWavRecorder

        # 1. Blocked source ignoring SIGTERM (simulates RDPSource blocked in pa_simple_read):
        #    must finish via SIGKILL in <0.4s (no 2.5s SIGINT hang) and preserve every flushed PCM byte.
        hang_src = (
            "import os, signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "os.write(1, b'\\x34\\x12' * 9001 + b'\\x7f')\n"  # 18003 bytes (odd trailing byte)
            "while True: time.sleep(10)\n"
        )
        rec_hang = FfmpegWavRecorder(cmd_factory=lambda _f, _s: [sys.executable, "-c", hang_src])
        with tempfile.TemporaryDirectory() as td:
            rec_hang.start("pulse", "RDPSource", Path(td), {})
            time.sleep(0.12)
            t0 = time.monotonic()
            wav_out = rec_hang.stop(drain_sec=0.0)
            elapsed = time.monotonic() - t0
            self.assertEqual(list(Path(td).iterdir()), [])  # zero temp .wav files on disk
        self.assertLess(elapsed, 0.45)
        ch, sw, sr, arr = _unpack_wav_samples(wav_out)
        self.assertEqual((ch, sw, sr, len(arr)), (1, 2, 16000, 9001))
        self.assertTrue(all(v == 0x1234 for v in arr))

        # 2. Real ffmpeg streaming s16le pipe with low-latency flags + tail_drain_sec
        try:
            resolve_ffmpeg_bin()
        except AudioError:
            if _is_ci():
                self.fail("ffmpeg must be installed in CI (silent skip forbidden)")
            self.skipTest("ffmpeg not installed")
        rec_live = FfmpegWavRecorder()
        with tempfile.TemporaryDirectory() as td:
            try:
                rec_live.start(
                    "lavfi",
                    "sine=frequency=440:sample_rate=16000:samples_per_frame=320:duration=5,arealtime",
                    Path(td),
                    os.environ,
                )
                deadline = time.monotonic() + 2.0
                while not rec_live._buf and time.monotonic() < deadline:
                    time.sleep(0.01)
                time.sleep(0.22)
                self.assertIsNotNone(rec_live._proc)
                assert rec_live._proc is not None
                self.assertIsNone(rec_live._proc.poll())
                live_wav = rec_live.stop(drain_sec=0.15)
            finally:
                rec_live.stop(drain_sec=0.0)
            self.assertEqual(list(Path(td).iterdir()), [])
        gate = process_wav(live_wav)
        self.assertTrue(gate.passed)
        self.assertGreaterEqual(gate.duration_sec, 0.30)

        # 3. WslPlatform (0.65s) & MacOSPlatform (0.20s) pass platform drain on stop, 0.0s on cancel
        from agent_voice_input.platform_macos import MacOSPlatform, _MacRecSession
        from agent_voice_input.platform_wsl import WslPlatform

        seen_drains: list[float] = []
        wplat = WslPlatform(Config(), env={})
        wplat._rec_mode = "pulse"
        wplat._ff_rec.stop = lambda *, drain_sec=0.0: (seen_drains.append(drain_sec) or b"")  # type: ignore[method-assign]
        wplat.stop_recording()
        wplat._rec_mode = "pulse"
        wplat.cancel_recording()

        mplat = MacOSPlatform(Config(), env={})
        mplat._ff_rec.stop = lambda *, drain_sec=0.0: (seen_drains.append(drain_sec) or b"")  # type: ignore[method-assign]
        with mplat._rec_cond:
            mplat._cur_session = _MacRecSession(1, mplat._ff_rec, Path("."))
        mplat.stop_recording()
        with mplat._rec_cond:
            mplat._cur_session = _MacRecSession(2, mplat._ff_rec, Path("."))
        mplat.cancel_recording()
        self.assertEqual(seen_drains, [0.65, 0.0, 0.20, 0.0])

    def test_ffmpeg_recorder_stderr_observability_and_pulse_env_fallback(self) -> None:
        from agent_voice_input.platform_wsl import FfmpegWavRecorder, _pulse_env

        err_src = (
            "import sys\n"
            "sys.stderr.write('X' * 700 + ' pulse connection refused\\n')\n"
            "sys.stderr.flush()\n"
            "sys.exit(1)\n"
        )
        rec_err = FfmpegWavRecorder(cmd_factory=lambda _f, _s: [sys.executable, "-c", err_src])
        with tempfile.TemporaryDirectory() as td:
            rec_err.start("pulse", "RDPSource", Path(td), {})
            time.sleep(0.08)
            wav_empty = rec_err.stop(drain_sec=0.0)
        self.assertEqual(wav_empty, b"")
        self.assertEqual(rec_err.last_returncode, 1)
        self.assertLessEqual(len(rec_err.last_stderr.encode("utf-8")), 512)
        self.assertIn("pulse connection refused", rec_err.diagnostic_summary())
        self.assertIn("rc=1", rec_err.diagnostic_summary())

        # Explicit PULSE_SERVER is always preserved
        explicit = _pulse_env({"PULSE_SERVER": "unix:/custom/pulse.sock"})
        self.assertEqual(explicit["PULSE_SERVER"], "unix:/custom/pulse.sock")

        # Falls back to XDG_RUNTIME_DIR/pulse/native when WSLg socket is absent
        with tempfile.TemporaryDirectory() as td:
            pdir = Path(td) / "pulse"
            pdir.mkdir()
            (pdir / "native").write_text("", encoding="utf-8")
            with mock.patch("agent_voice_input.platform_wsl.WSLG_PULSE_SOCK", str(Path(td) / "no_wslg_sock")):
                derived = _pulse_env({"XDG_RUNTIME_DIR": td})
            self.assertEqual(derived["PULSE_SERVER"], f"unix:{pdir / 'native'}")

    def test_default_timeouts_pause_splitting_and_failed_audio_ring_buffer(self) -> None:
        default_cfg = Config()
        self.assertEqual(default_cfg.asr.timeout_sec, 60.0)
        self.assertEqual(default_cfg.refiner.timeout_sec, 90.0)
        self.assertEqual(DEFAULT_MAX_CHUNK_SEC, 180.0)

        # 1. Exact <= 180.0s (including exact 180.0s boundary) returns single original WAV without splitting
        short_wav = pack_pcm16_mono_wav([3500, -3500] * 8000)  # 1.0s
        single_chunks = split_wav_chunks(short_wav, max_chunk_sec=5.0)
        self.assertEqual(len(single_chunks), 1)
        self.assertEqual(single_chunks[0], short_wav)
        exact_180_wav = pack_pcm16_mono_wav(b"\x10\x0e" * (180 * 16000))  # exact 180.000s (2,880,000 samples)
        self.assertEqual(split_wav_chunks(exact_180_wav), [exact_180_wav])

        # 2. Long audio with elevated ambient floor (-38 dBFS > -50 dBFS) splits cleanly at natural pauses
        sr = 16000
        speech_amp = 4200
        ambient_amp = int(round((10.0 ** (-38.0 / 20.0)) * 32768.0))  # ~412 (> -50 dBFS floor!)

        def _seg(amp: int, sec: float) -> list[int]:
            half = int(round(sec * sr / 2.0))
            return [amp, -amp] * half

        long_samples = (
            _seg(speech_amp, 3.5)
            + _seg(ambient_amp, 0.5)  # pause 1 centered at ~3.75s
            + _seg(speech_amp, 3.5)
            + _seg(ambient_amp, 0.4)  # pause 2 centered at ~7.70s
            + _seg(speech_amp, 2.6)
        )
        long_wav = pack_pcm16_mono_wav(long_samples)
        chunks = split_wav_chunks(long_wav, max_chunk_sec=5.0, search_window_sec=2.0)
        self.assertEqual(len(chunks), 3)
        reconstructed: list[int] = []
        chunk_secs: list[float] = []
        for ch_bytes in chunks:
            _, _, out_sr, arr = _unpack_wav_samples(ch_bytes)
            self.assertEqual(out_sr, sr)
            dur = len(arr) / float(sr)
            self.assertLessEqual(dur, 5.0 + 1e-6)
            chunk_secs.append(dur)
            reconstructed.extend(arr)
        self.assertGreaterEqual(chunk_secs[0], 3.55)
        self.assertLessEqual(chunk_secs[0], 3.95)
        self.assertEqual(reconstructed, long_samples)  # strictly lossless sample preservation!

        # 3. Continuous speech without any silent frames falls back to minimum-energy valley
        cont_samples = _seg(6000, 4.0) + _seg(1500, 0.2) + _seg(6000, 1.0)
        cont_chunks = split_wav_chunks(pack_pcm16_mono_wav(cont_samples), max_chunk_sec=4.5, search_window_sec=1.5)
        self.assertEqual(len(cont_chunks), 2)
        _, _, _, c0_arr = _unpack_wav_samples(cont_chunks[0])
        _, _, _, c1_arr = _unpack_wav_samples(cont_chunks[1])
        self.assertAlmostEqual(len(c0_arr) / float(sr), 4.1, delta=0.12)
        self.assertEqual(list(c0_arr) + list(c1_arr), cont_samples)

        # 3b. Barely-over-threshold audio with trailing silence never emits a <0.5s micro-sliver chunk
        barely_over = [4000, -4000] * 39000 + [0] * 2002  # 80,002 samples (5.000125s)
        bo_chunks = split_wav_chunks(pack_pcm16_mono_wav(barely_over), max_chunk_sec=5.0, search_window_sec=2.0)
        self.assertEqual(len(bo_chunks), 2)
        bo_recon: list[int] = []
        for bo_b in bo_chunks:
            _, _, _, bo_arr = _unpack_wav_samples(bo_b)
            self.assertGreaterEqual(len(bo_arr) / float(sr), 0.8)
            self.assertLessEqual(len(bo_arr) / float(sr), 5.0 + 1e-6)
            bo_recon.extend(bo_arr)
        self.assertEqual(bo_recon, barely_over)

        # 3c. Soft speaker + room fan noise boosted by process_wav (+18 dB -> pauses at -30 dBFS) & rapid talker (<9% pauses)
        soft_speech = 460   # ~ -37 dBFS raw speech -> boosted by +17 dB to -20 dBFS
        fan_noise = 130     # ~ -48 dBFS raw fan noise -> boosted to ~ -31 dBFS (above -32 dBFS!)
        boosted_raw = (
            _seg(soft_speech, 3.2)
            + _seg(fan_noise, 0.85)  # earlier stop at 3.2..4.05s
            + _seg(soft_speech, 0.5)
            + _seg(fan_noise, 0.75)  # later full sentence stop at 4.55..5.30s -> midpoint ~4.92s!
            + _seg(soft_speech, 3.4)
        )
        gate_boosted = process_wav(pack_pcm16_mono_wav(boosted_raw))
        self.assertTrue(gate_boosted.passed)
        self.assertGreater(gate_boosted.gain_db, 14.0)
        b_infos = inspect_wav_chunks(gate_boosted.wav_bytes, max_chunk_sec=5.5, search_window_sec=2.5)
        self.assertEqual(len(b_infos), 2)
        self.assertEqual(b_infos[0].cut_reason, "pause_300ms")
        # Prefers the later >=700ms sentence stop (~4.92s) over the earlier one (~3.62s) to minimize total chunks!
        self.assertGreaterEqual(b_infos[0].end_sec, 4.70)
        self.assertLessEqual(b_infos[0].end_sec, 5.15)

        # 4. Failed audio ring buffer: 0700 dir, 0600 files, collision suffix, 24h TTL + max 10 retention, selector lookup
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "uncreated_rt"
            base_ts = 1_760_000_000.0
            expired = save_failed_audio(short_wav, rt, duration_sec=248.4, label="old-repo", now_ts=base_ts - 90_000.0)
            self.assertIsNotNone(expired)
            assert expired is not None
            self.assertTrue(expired.is_file())
            self.assertEqual(expired.stat().st_mode & 0o777, 0o600)
            self.assertEqual(expired.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(rt.stat().st_mode & 0o777, 0o700)

            s1 = save_failed_audio(short_wav, rt, duration_sec=210.0, label="post-studio", now_ts=base_ts)
            s1_dup = save_failed_audio(short_wav, rt, duration_sec=210.0, label="post-studio", now_ts=base_ts)
            s2 = save_failed_audio(short_wav, rt, duration_sec=5.0, label="post-studio", now_ts=base_ts)
            assert s1 is not None and s1_dup is not None and s2 is not None
            self.assertNotEqual(s1, s1_dup)
            self.assertTrue(s1_dup.name.endswith("_2.wav"))
            self.assertNotEqual(s1, s2)
            self.assertIn("210s_post-studio", s1.name)
            self.assertFalse(expired.exists())  # >24h entry pruned on write!

            for idx in range(11):
                save_failed_audio(
                    short_wav,
                    rt,
                    duration_sec=10 + idx,
                    label=f"proj-{idx}",
                    now_ts=base_ts + 10 + idx,
                )
            kept = list_failed_audios(rt, now_ts=base_ts + 30.0)
            self.assertEqual(len(kept), 10)
            self.assertIn("proj-10", kept[0].name)
            self.assertIn("proj-1", kept[-1].name)

            self.assertEqual(resolve_failed_audio(rt, now_ts=base_ts + 30.0), kept[0])
            self.assertEqual(resolve_failed_audio(rt, "1", now_ts=base_ts + 30.0), kept[0])
            self.assertEqual(resolve_failed_audio(rt, "10", now_ts=base_ts + 30.0), kept[-1])
            self.assertEqual(resolve_failed_audio(rt, "proj-7", now_ts=base_ts + 30.0), [p for p in kept if "proj-7" in p.name][0])
            with self.assertRaises(FileNotFoundError):
                resolve_failed_audio(rt, "proj-", now_ts=base_ts + 30.0)
            with self.assertRaises(FileNotFoundError):
                resolve_failed_audio(rt, "99", now_ts=base_ts + 30.0)

    def test_in_memory_mp3_encoding_and_single_hop_constants(self) -> None:
        import subprocess
        self.assertEqual(SINGLE_HOP_MAX_CHUNK_SEC, 610.0)
        self.assertEqual(SINGLE_HOP_MP3_MIN_WAV_BYTES, 96_000)
        self.assertIsNone(encode_wav_to_mp3_bytes(b""))
        self.assertIsNone(encode_wav_to_mp3_bytes(b"not-a-valid-wav"))
        valid_1s_wav = pack_pcm16_mono_wav([1000] * 16000)
        with mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", side_effect=FileNotFoundError("no ffmpeg")):
            self.assertIsNone(encode_wav_to_mp3_bytes(valid_1s_wav, ffmpeg_bin="/nonexistent/ffmpeg"))
        with mock.patch(
            "agent_voice_input.audio_proc.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["ffmpeg"], timeout=0.01),
        ):
            self.assertIsNone(encode_wav_to_mp3_bytes(valid_1s_wav, ffmpeg_bin="/usr/bin/ffmpeg", timeout_sec=0.01))
        try:
            ff_bin: str | None = resolve_ffmpeg_bin()
        except AudioError:
            ff_bin = None
        if ff_bin is None:
            if _is_ci():
                self.fail("ffmpeg must be installed in CI for MP3 encoding test (silent skip forbidden)")
        else:
            enc_probe = subprocess.run(
                [ff_bin, "-hide_banner", "-encoders"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=5.0,
                check=False,
            )
            has_lame = b"libmp3lame" in enc_probe.stdout
            samples_4s = array.array(
                "h",
                (int(5000 * math.sin(2.0 * math.pi * 220.0 * i / 16000.0)) for i in range(64000)),
            )
            wav_4s = pack_pcm16_mono_wav(samples_4s)
            self.assertGreater(len(wav_4s), SINGLE_HOP_MP3_MIN_WAV_BYTES)
            mp3_bytes = encode_wav_to_mp3_bytes(wav_4s, ffmpeg_bin=ff_bin)
            if has_lame:
                self.assertIsNotNone(mp3_bytes)
            if mp3_bytes is not None:
                self.assertLess(len(mp3_bytes), len(wav_4s) // 2)
                pcm_back = decode_to_16k_mono_pcm(mp3_bytes)
                self.assertGreater(len(pcm_back), 60000 * 2)

    def test_nvm_codex_shebang_path_auto_keyring_ffmpeg_fallback_and_recordings_parent_perms(self) -> None:
        import json
        import stat
        from agent_voice_input._remote_probe import _is_trusted_runtime_dir
        from agent_voice_input.audio_proc import resolve_ffmpeg_bin
        from agent_voice_input.config import (
            Config,
            find_codex_bin,
            get_runtime_dir,
            has_codex_local_auth,
            resolve_saved_recordings_dir,
        )
        from agent_voice_input.refiner import CodexRefiner

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            nvm_v18 = home / ".nvm" / "versions" / "node" / "v18.20.0" / "bin"
            nvm_v22 = home / ".nvm" / "versions" / "node" / "v22.14.0" / "bin"
            nvm_v18.mkdir(parents=True)
            nvm_v22.mkdir(parents=True)

            # Fake `node` exists ONLY inside nvm v22 bin, NOT in minimal `/usr/bin:/bin` PATH!
            fake_node = nvm_v22 / "node"
            fake_node.write_text(f"#!{sys.executable}\nimport runpy, sys\nrunpy.run_path(sys.argv[1], run_name='__main__')\n", encoding="utf-8")
            fake_node.chmod(0o755)

            old_codex = nvm_v18 / "codex"
            old_codex.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            old_codex.chmod(0o755)

            codex_script = nvm_v22 / "codex"
            codex_script.write_text(
                "#!/usr/bin/env node\n"
                "import json, sys\n"
                "for line in sys.stdin:\n"
                "    msg = json.loads(line)\n"
                "    mid = msg.get('id')\n"
                "    method = msg.get('method', '')\n"
                "    if method == 'initialize':\n"
                "        sys.stdout.write(json.dumps({'id': mid, 'result': {'userAgent': 'test'}}) + '\\n')\n"
                "        sys.stdout.flush()\n"
                "    elif method == 'getAuthStatus':\n"
                "        sys.stdout.write(json.dumps({'id': mid, 'result': {'authMethod': 'apikey', 'authToken': 'sk-nvm-ok'}}) + '\\n')\n"
                "        sys.stdout.flush()\n"
                "        break\n",
                encoding="utf-8",
            )
            codex_script.chmod(0o755)

            codex_home = home / ".codex"
            codex_home.mkdir(parents=True)
            (codex_home / "config.toml").write_text('cli_auth_credentials_store = "auto"\n', encoding="utf-8")

            min_env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
            self.assertEqual(find_codex_bin(min_env), str(codex_script))
            self.assertTrue(has_codex_local_auth(min_env))

            ref = CodexRefiner(Config(), env=min_env)
            try:
                method, tok = ref._call_codex_get_auth_status(timeout_sec=3.0)
                self.assertEqual((method, tok), ("apikey", "sk-nvm-ok"))
            finally:
                ref.close()

            # resolve_ffmpeg_bin falls back to ~/.local/bin/ffmpeg when preferred & PATH miss
            local_ff = home / ".local" / "bin" / "ffmpeg"
            local_ff.parent.mkdir(parents=True, exist_ok=True)
            local_ff.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            local_ff.chmod(0o755)
            with mock.patch("agent_voice_input.audio_proc.os.path.isfile", side_effect=lambda p: str(p) == str(local_ff)), mock.patch(
                "agent_voice_input.audio_proc.os.access", side_effect=lambda p, _m: str(p) == str(local_ff)
            ):
                self.assertEqual(
                    resolve_ffmpeg_bin("/usr/bin/ffmpeg", env={"HOME": str(home), "PATH": "/nonexistent"}),
                    str(local_ff),
                )

            # resolve_saved_recordings_dir(create=True) secures ~/.cache/agent-voice-input (0700) when XDG_RUNTIME_DIR is unset
            no_xdg_env = {"HOME": str(home)}
            rec_dir = resolve_saved_recordings_dir(no_xdg_env, create=True)
            rt_dir = get_runtime_dir(no_xdg_env, create=False)
            self.assertEqual(rec_dir.parent, rt_dir)
            self.assertEqual(stat.S_IMODE(os.lstat(rt_dir).st_mode), 0o700)
            self.assertTrue(_is_trusted_runtime_dir(rt_dir))

    def test_refiner_max_turns_per_thread_defaults_zero_and_rejects_negative(self) -> None:
        cfg_default, _ = load_config(env={"HOME": "/tmp/nonexistent-avi-home"}, load_default_env_files=False)
        self.assertEqual(cfg_default.refiner.max_turns_per_thread, 0)

        cfg_zero, _ = load_config(
            cli_overrides={"refiner.max_turns_per_thread": 0},
            env={"HOME": "/tmp/nonexistent-avi-home"},
            load_default_env_files=False,
        )
        self.assertEqual(cfg_zero.refiner.max_turns_per_thread, 0)

        cfg_pos, _ = load_config(
            cli_overrides={"refiner.max_turns_per_thread": 15},
            env={"HOME": "/tmp/nonexistent-avi-home"},
            load_default_env_files=False,
        )
        self.assertEqual(cfg_pos.refiner.max_turns_per_thread, 15)

        with self.assertRaisesRegex(ConfigError, "refiner.max_turns_per_thread must be >= 0"):
            load_config(
                cli_overrides={"refiner.max_turns_per_thread": -1},
                env={"HOME": "/tmp/nonexistent-avi-home"},
                load_default_env_files=False,
            )

    def _synth_sine(self, sec: float, dbfs: float, *, freq_hz: float = 440.0) -> list[int]:
        sr = 16000
        amp = (10.0 ** (dbfs / 20.0)) * math.sqrt(2.0) * 32768.0
        n = int(round(sec * sr))
        return [
            max(-32767, min(32767, int(round(amp * math.sin(2.0 * math.pi * freq_hz * i / sr)))))
            for i in range(n)
        ]

    def _synth_noise(self, sec: float, dbfs: float = -75.0, *, seed: int = 42) -> list[int]:
        import random

        sr = 16000
        rng = random.Random(seed)
        # Uniform[-bound, bound] has RMS = bound / sqrt(3)
        bound = (10.0 ** (dbfs / 20.0)) * 32768.0 * math.sqrt(3.0)
        n = int(round(sec * sr))
        return [int(round(rng.uniform(-bound, bound))) for _ in range(n)]

    def test_silence_trim_long_pause_boundaries_and_edge_margins(self) -> None:
        sr = 16000

        # 1. 20s internal pause between two 4s speech segments (total 28s; speech > 15% so p85 is speech)
        long_pause_samples = (
            self._synth_sine(4.0, -20.0)
            + self._synth_noise(20.0, -75.0, seed=1)
            + self._synth_sine(4.0, -20.0)
        )
        long_pause_wav = pack_pcm16_mono_wav(long_pause_samples)

        bal = process_wav(long_pause_wav, AudioConfig(silence_trim="balanced"))
        self.assertTrue(bal.passed)
        self.assertAlmostEqual(bal.duration_sec, 28.0, places=3)
        self.assertEqual(bal.trim_cuts, 1)
        # 20.0s pause -> ~0.60s kept minus 0.01s crossfade overlap => ~19.41s trimmed, ~8.59s output
        self.assertAlmostEqual(bal.trimmed_sec, 19.41, delta=0.05)
        _, _, _, bal_out = _unpack_wav_samples(bal.wav_bytes)
        self.assertAlmostEqual(len(bal_out) / float(sr), bal.duration_sec - bal.trimmed_sec, places=3)
        self.assertAlmostEqual(len(bal_out) / float(sr), 8.59, delta=0.05)

        cons = process_wav(long_pause_wav, AudioConfig(silence_trim="conservative"))
        self.assertTrue(cons.passed)
        self.assertEqual(cons.trim_cuts, 1)
        # 20.0s pause -> ~0.80s kept minus 0.01s crossfade => ~19.21s trimmed, ~8.79s output
        self.assertAlmostEqual(cons.trimmed_sec, 19.21, delta=0.05)
        _, _, _, cons_out = _unpack_wav_samples(cons.wav_bytes)
        self.assertAlmostEqual(len(cons_out) / float(sr), 8.79, delta=0.05)

        # 2. Pause duration boundaries: two 1.1s pauses untouched in balanced; two 1.5s pauses trimmed in balanced but untouched in conservative
        p11 = (
            self._synth_sine(1.5, -20.0)
            + self._synth_noise(1.1, -75.0, seed=2)
            + self._synth_sine(1.5, -20.0)
            + self._synth_noise(1.1, -75.0, seed=3)
            + self._synth_sine(1.5, -20.0)
        )
        p11_wav = pack_pcm16_mono_wav(p11)
        p11_bal = process_wav(p11_wav, AudioConfig(silence_trim="balanced"))
        p11_off = process_wav(p11_wav, AudioConfig(silence_trim="off"))
        self.assertEqual(p11_bal.trim_cuts, 0)
        self.assertEqual(p11_bal.trimmed_sec, 0.0)
        self.assertEqual(p11_bal.wav_bytes, p11_off.wav_bytes)

        p15 = (
            self._synth_sine(1.5, -20.0)
            + self._synth_noise(1.5, -75.0, seed=4)
            + self._synth_sine(1.5, -20.0)
            + self._synth_noise(1.5, -75.0, seed=5)
            + self._synth_sine(1.5, -20.0)
        )
        p15_wav = pack_pcm16_mono_wav(p15)
        p15_cons = process_wav(p15_wav, AudioConfig(silence_trim="conservative"))
        p15_bal = process_wav(p15_wav, AudioConfig(silence_trim="balanced"))
        self.assertEqual(p15_cons.trim_cuts, 0)
        self.assertEqual(p15_cons.trimmed_sec, 0.0)
        self.assertEqual(p15_bal.trim_cuts, 2)
        self.assertAlmostEqual(p15_bal.trimmed_sec, 1.82, delta=0.08)

        # 3. Leading & trailing margins per preset + 0.2s hysteresis
        edges = (
            self._synth_noise(2.0, -75.0, seed=6)
            + self._synth_sine(3.0, -20.0)
            + self._synth_noise(2.0, -75.0, seed=7)
        )
        edges_wav = pack_pcm16_mono_wav(edges)
        e_bal = process_wav(edges_wav, AudioConfig(silence_trim="balanced"))
        self.assertEqual(e_bal.trim_cuts, 2)
        # 2.0->0.30 (save 1.70) + 2.0->0.45 (save 1.55) = 3.25s trimmed
        self.assertAlmostEqual(e_bal.trimmed_sec, 3.25, delta=0.05)

        e_cons = process_wav(edges_wav, AudioConfig(silence_trim="conservative"))
        self.assertEqual(e_cons.trim_cuts, 2)
        # 2.0->0.40 (save 1.60) + 2.0->0.55 (save 1.45) = 3.05s trimmed
        self.assertAlmostEqual(e_cons.trimmed_sec, 3.05, delta=0.05)

        # Hysteresis: leading 0.48s (< 0.30+0.20) & trailing 0.62s (< 0.45+0.20) must not trim
        small_edges = (
            self._synth_noise(0.48, -75.0, seed=8)
            + self._synth_sine(3.2, -20.0)
            + self._synth_noise(0.62, -75.0, seed=9)
        )
        se_bal = process_wav(pack_pcm16_mono_wav(small_edges), AudioConfig(silence_trim="balanced"))
        self.assertEqual(se_bal.trim_cuts, 0)
        self.assertEqual(se_bal.trimmed_sec, 0.0)

    def test_silence_trim_impulse_bridging_weak_syllable_and_decay_tail(self) -> None:
        sr = 16000

        # 1. Faint 40ms click (-48 dBFS <= thresh+8dB) inside a 3.0s pause is bridged in both balanced and conservative
        click_40 = (
            self._synth_sine(2.0, -20.0)
            + self._synth_noise(1.5, -75.0, seed=10)
            + self._synth_sine(0.04, -48.0, freq_hz=1200.0)
            + self._synth_noise(1.5, -75.0, seed=11)
            + self._synth_sine(2.0, -20.0)
        )
        c40_wav = pack_pcm16_mono_wav(click_40)
        c40_bal = process_wav(c40_wav, AudioConfig(silence_trim="balanced"))
        c40_cons = process_wav(c40_wav, AudioConfig(silence_trim="conservative"))
        self.assertEqual(c40_bal.trim_cuts, 1)
        self.assertAlmostEqual(c40_bal.trimmed_sec, 2.45, delta=0.08)
        self.assertEqual(c40_cons.trim_cuts, 1)
        self.assertAlmostEqual(c40_cons.trimmed_sec, 2.25, delta=0.08)

        # Faint 60ms click: bridged in balanced (<=3 frames), NOT bridged in conservative (<=2 frames -> two 1.5s < 1.6s gaps)
        click_60 = (
            self._synth_sine(2.0, -20.0)
            + self._synth_noise(1.5, -75.0, seed=12)
            + self._synth_sine(0.06, -48.0, freq_hz=1200.0)
            + self._synth_noise(1.5, -75.0, seed=13)
            + self._synth_sine(2.0, -20.0)
        )
        c60_wav = pack_pcm16_mono_wav(click_60)
        self.assertEqual(process_wav(c60_wav, AudioConfig(silence_trim="balanced")).trim_cuts, 1)
        self.assertEqual(process_wav(c60_wav, AudioConfig(silence_trim="conservative")).trim_cuts, 0)

        # Loud 40ms short burst (-20 dBFS > thresh+8dB) is protected by peak guard and NOT bridged
        loud_burst = (
            self._synth_sine(2.0, -20.0)
            + self._synth_noise(1.5, -75.0, seed=18)
            + self._synth_sine(0.04, -20.0, freq_hz=900.0)
            + self._synth_noise(1.5, -75.0, seed=19)
            + self._synth_sine(2.0, -20.0)
        )
        lb_res = process_wav(pack_pcm16_mono_wav(loud_burst), AudioConfig(silence_trim="balanced"))
        self.assertEqual(lb_res.trim_cuts, 2)
        self.assertAlmostEqual(lb_res.trimmed_sec, 1.82, delta=0.08)

        # 2. 150ms weak syllable (-42 dBFS, realistic soft voice) inside a 4s pause MUST be preserved (2 cuts around it)
        weak_syl = (
            self._synth_sine(2.0, -20.0)
            + self._synth_noise(2.0, -75.0, seed=14)
            + self._synth_sine(0.15, -42.0, freq_hz=660.0)
            + self._synth_noise(2.0, -75.0, seed=15)
            + self._synth_sine(2.0, -20.0)
        )
        ws_res = process_wav(pack_pcm16_mono_wav(weak_syl), AudioConfig(silence_trim="balanced"))
        self.assertEqual(ws_res.trim_cuts, 2)
        _, _, _, ws_out = _unpack_wav_samples(ws_res.wav_bytes)
        # Expected output: 2.0 + 0.59 + 0.15 + 0.59 + 2.0 = ~5.33s; weak syllable lives around 2.59..2.74s
        self.assertAlmostEqual(len(ws_out) / float(sr), 5.33, delta=0.08)
        mid_seg = ws_out[int(2.45 * sr) : int(2.90 * sr)]
        mid_peak = max(abs(int(x)) for x in mid_seg)
        # -42 dBFS sine peak ~ 368; -75 dBFS noise peak ~ 10 -> preserved weak syllable clearly stands above noise
        self.assertGreater(mid_peak, 200)

        # 3. Decaying -60 dBFS tail inside post-speech 0.35s keep zone MUST be preserved (anti-word-clipping guard)
        tail_samples = (
            self._synth_sine(3.0, -20.0)
            + self._synth_sine(0.30, -60.0, freq_hz=330.0)
            + self._synth_noise(3.0, -75.0, seed=16)
            + self._synth_sine(2.0, -20.0)
        )
        tail_res = process_wav(pack_pcm16_mono_wav(tail_samples), AudioConfig(silence_trim="balanced"))
        self.assertEqual(tail_res.trim_cuts, 1)
        _, _, _, tail_out = _unpack_wav_samples(tail_res.wav_bytes)
        tail_region = tail_out[int(3.02 * sr) : int(3.28 * sr)]
        tail_rms = math.sqrt(sum(s * s for s in tail_region) / (len(tail_region) * (32768.0 * 32768.0)))
        tail_dbfs = 20.0 * math.log10(max(tail_rms, 1e-10))
        self.assertAlmostEqual(tail_dbfs, -60.0 + tail_res.gain_db, delta=1.5)

    def test_silence_trim_preserves_quiet_followup_clause_under_elevated_noise_floor(self) -> None:
        sr = 16000
        # Main clauses (-16 dBFS) flanking 2.0s pauses and a quiet modulated -47 dBFS follow-up clause (~1.9s)
        # with realistic short inter-word gaps, overlaid with -62 dBFS fan/laptop noise floor.
        quiet_clause = (
            self._synth_sine(0.55, -47.0, freq_hz=520.0)
            + [0] * int(0.12 * sr)
            + self._synth_sine(0.55, -46.0, freq_hz=580.0)
            + [0] * int(0.13 * sr)
            + self._synth_sine(0.55, -48.0, freq_hz=500.0)
        )  # 1.90s
        clean = (
            self._synth_sine(2.5, -16.0, freq_hz=440.0)
            + [0] * int(2.0 * sr)
            + quiet_clause
            + [0] * int(2.0 * sr)
            + self._synth_sine(2.5, -16.0, freq_hz=440.0)
        )  # total 10.90s
        noise = self._synth_noise(len(clean) / float(sr), -62.0, seed=404)
        mixed = [max(-32767, min(32767, a + b)) for a, b in zip(clean, noise)]

        res = process_wav(pack_pcm16_mono_wav(mixed), AudioConfig(silence_trim="balanced"))
        self.assertTrue(res.passed)
        self.assertAlmostEqual(res.duration_sec, 10.90, places=2)
        # Both 2.0s pauses trimmed to ~0.6s (~1.4s saved each -> ~2.82s total), NEVER deleting the 1.9s quiet clause!
        self.assertEqual(res.trim_cuts, 2)
        self.assertGreater(res.trimmed_sec, 2.4)
        self.assertLess(res.trimmed_sec, 3.3)
        _, _, _, out_arr = _unpack_wav_samples(res.wav_bytes)
        self.assertAlmostEqual(len(out_arr) / float(sr), 10.90 - res.trimmed_sec, places=3)
        # Verify the quiet follow-up clause (~3.1s..5.0s in output) retains full -47 dBFS + gain_db energy
        mid_clause = out_arr[int(3.2 * sr) : int(4.8 * sr)]
        mid_rms = math.sqrt(sum(s * s for s in mid_clause) / (len(mid_clause) * (32768.0 * 32768.0)))
        mid_dbfs = 20.0 * math.log10(max(mid_rms, 1e-10))
        self.assertGreater(mid_dbfs, -52.0 + res.gain_db)

    def test_silence_trim_extreme_low_speech_ratio_thinking_pause(self) -> None:
        sr = 16000
        # 3s speech + 45s noise + 3s speech = 51s total (~11.8% speech < 15%, where p85 falls into noise floor)
        sparse_samples = (
            self._synth_sine(3.0, -20.0)
            + self._synth_noise(45.0, -75.0, seed=17)
            + self._synth_sine(3.0, -20.0)
        )
        res = process_wav(pack_pcm16_mono_wav(sparse_samples), AudioConfig(silence_trim="balanced"))
        self.assertTrue(res.passed)
        self.assertAlmostEqual(res.duration_sec, 51.0, places=3)
        self.assertGreaterEqual(res.trim_cuts, 1)
        self.assertEqual(res.trim_cuts, 1)
        self.assertGreater(res.trimmed_sec, 40.0)
        self.assertAlmostEqual(res.trimmed_sec, 44.41, delta=0.08)
        _, _, _, out_arr = _unpack_wav_samples(res.wav_bytes)
        self.assertAlmostEqual(len(out_arr) / float(sr), 6.59, delta=0.08)

    def test_silence_trim_bypass_gain_invariance_fixed_noise_idempotency_and_crossfade(self) -> None:
        sr = 16000

        # 1. Fast bypass: < 4s clip produces bit-for-bit identical bytes to "off"
        short_clip = self._synth_sine(1.0, -20.0) + self._synth_noise(2.5, -75.0, seed=20)
        short_wav = pack_pcm16_mono_wav(short_clip)
        short_off = process_wav(short_wav, AudioConfig(silence_trim="off"))
        short_bal = process_wav(short_wav, AudioConfig(silence_trim="balanced"))
        self.assertEqual(short_bal.trimmed_sec, 0.0)
        self.assertEqual(short_bal.trim_cuts, 0)
        self.assertEqual(short_bal.wav_bytes, short_off.wav_bytes)

        # Fast bypass: >= 4s clip with < 1.0s savable silence (1.4s pause -> 0.8s savable < 1.0s)
        small_save = (
            self._synth_sine(2.0, -20.0)
            + self._synth_noise(1.4, -75.0, seed=21)
            + self._synth_sine(2.0, -20.0)
        )
        ss_wav = pack_pcm16_mono_wav(small_save)
        ss_off = process_wav(ss_wav, AudioConfig(silence_trim="off"))
        ss_bal = process_wav(ss_wav, AudioConfig(silence_trim="balanced"))
        self.assertEqual(ss_bal.trimmed_sec, 0.0)
        self.assertEqual(ss_bal.trim_cuts, 0)
        self.assertEqual(ss_bal.wav_bytes, ss_off.wav_bytes)

        # Direct all-silent guard on _plan_silence_trim
        self.assertEqual(_plan_silence_trim(array.array("h", [0] * (5 * sr)), _TRIM_PRESETS["balanced"]), [])

        # 2. Gain invariance: off vs balanced produce identical gain_db, rms_dbfs, duration_sec, voiced_sec
        quiet_long = (
            self._synth_noise(2.0, -75.0, seed=22)
            + self._synth_sine(3.0, -34.0)
            + self._synth_noise(6.0, -75.0, seed=23)
            + self._synth_sine(3.0, -34.0)
            + self._synth_noise(2.0, -75.0, seed=24)
        )
        ql_wav = pack_pcm16_mono_wav(quiet_long)
        ql_off = process_wav(ql_wav, AudioConfig(silence_trim="off"))
        ql_bal = process_wav(ql_wav, AudioConfig(silence_trim="balanced"))
        ql_override = process_wav(ql_wav, AudioConfig(silence_trim="balanced"), silence_trim="off")
        self.assertGreater(ql_bal.trimmed_sec, 8.0)
        self.assertEqual(ql_bal.gain_db, ql_off.gain_db)
        self.assertEqual(ql_bal.rms_dbfs, ql_off.rms_dbfs)
        self.assertEqual(ql_bal.duration_sec, ql_off.duration_sec)
        self.assertEqual(ql_bal.voiced_sec, ql_off.voiced_sec)
        self.assertEqual(ql_override.wav_bytes, ql_off.wav_bytes)
        self.assertEqual(ql_override.trimmed_sec, 0.0)

        # 3. Fixed-noise-floor second-pass idempotency: re-planning on its own spliced output returns []
        raw_arr = array.array("h", quiet_long)
        for mode in ("balanced", "conservative"):
            preset = _TRIM_PRESETS[mode]
            ranges = _plan_silence_trim(raw_arr, preset)
            self.assertTrue(ranges)
            spliced = _splice_with_crossfade(raw_arr, ranges)
            self.assertEqual(_plan_silence_trim(spliced, preset), [])

        # 4. Crossfade continuity: opposite DC steps (+4000 -> -4000) interpolate smoothly with no click
        step_src = array.array("h", [4000] * 1000 + [0] * 1000 + [-4000] * 1000)
        spliced_step = _splice_with_crossfade(step_src, [(0, 1000), (2000, 3000)])
        self.assertEqual(len(spliced_step), 2000 - 160)
        max_jump = max(
            abs(int(spliced_step[i + 1]) - int(spliced_step[i]))
            for i in range(len(spliced_step) - 1)
        )
        self.assertLessEqual(max_jump, 60)
        # Packed WAV output is valid canonical 16kHz mono 16-bit PCM
        ch, sw, out_sr, out_arr = _unpack_wav_samples(ql_bal.wav_bytes)
        self.assertEqual((ch, sw, out_sr), (1, 2, 16000))
        self.assertAlmostEqual(len(out_arr) / float(sr), ql_bal.duration_sec - ql_bal.trimmed_sec, places=3)

    def test_silence_trim_pipeline_seams_retry_archive_telemetry_and_single_hop(self) -> None:
        from agent_voice_input.asr_providers import AsrContext, AsrResult, BaseAsrProvider
        from agent_voice_input.audio_proc import list_saved_recordings
        from agent_voice_input.config import RefinerConfig, resolve_saved_recordings_dir
        from agent_voice_input.daemon_pipeline import build_pipeline_summary, run_pipeline_once
        from agent_voice_input.diagnose import (
            _sanitize_telemetry_record,
            format_live_diagnostics_markdown,
        )
        from agent_voice_input.refiner import NoopRefiner

        class _RecordingAsr(BaseAsrProvider):
            provider_name = "fake"

            def __init__(self, *, single_hop: bool = False) -> None:
                self.single_hop = single_hop
                self.seen_wav_lens: list[int] = []

            def transcribe(
                self,
                wav_bytes: bytes,
                *,
                context: AsrContext | None = None,
                is_cancelled: object = None,
                cancel_token: object = None,
            ) -> AsrResult:
                self.seen_wav_lens.append(len(wav_bytes))
                return AsrResult(
                    text="测试裁剪接缝",
                    provider="fake",
                    model="fake-model",
                    latency_ms=12,
                    is_single_hop_refined=self.single_hop,
                )

        sr = 16000
        long_samples = (
            self._synth_sine(2.0, -20.0)
            + self._synth_noise(5.0, -75.0, seed=30)
            + self._synth_sine(2.0, -20.0)
        )
        raw_wav = pack_pcm16_mono_wav(long_samples)  # 9.0s

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            rt = root / "rt"
            home.mkdir()
            rt.mkdir(mode=0o700)
            env = {"HOME": str(home), "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)}

            # 1. Normal recording trims before ASR, archives RAW original audio to recordings/, and populates telemetry
            asr_norm = _RecordingAsr(single_hop=False)
            cfg_arch = Config(
                audio=AudioConfig(silence_trim="balanced", save_recordings=True, save_recordings_max_count=5),
                refiner=RefinerConfig(provider="off"),
            )
            # Force WAV archive fallback so we can compare exact archived sample count without ffmpeg lossy framing
            with mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", side_effect=AudioError("no ffmpeg")):
                res_norm = run_pipeline_once(
                    raw_wav,
                    cfg=cfg_arch,
                    asr_provider=asr_norm,
                    refiner=NoopRefiner(),
                    audio_source="avfoundation",
                    dry_run=True,
                    runtime_dir=rt,
                    env=env,
                )
            self.assertTrue(res_norm.ok)
            assert res_norm.gate is not None
            self.assertAlmostEqual(res_norm.gate.duration_sec, 9.0, places=3)
            self.assertAlmostEqual(res_norm.gate.trimmed_sec, 4.41, delta=0.06)
            self.assertEqual(res_norm.gate.trim_cuts, 1)
            self.assertEqual(len(asr_norm.seen_wav_lens), 1)
            self.assertLess(asr_norm.seen_wav_lens[0], len(raw_wav) - int(4.0 * sr * 2))

            archived = list_saved_recordings(resolve_saved_recordings_dir(env))
            self.assertEqual(len(archived), 1)
            self.assertEqual(archived[0].read_bytes(), raw_wav)

            summary = build_pipeline_summary(1, res_norm, dry_run=True)
            self.assertEqual(summary["raw_audio_sec"], 9.0)
            self.assertEqual(summary["silence_trimmed_sec"], round(res_norm.gate.trimmed_sec, 3))
            self.assertEqual(summary["silence_trim_cuts"], 1)

            sanitized = _sanitize_telemetry_record(summary, lambda s: s)
            self.assertEqual(sanitized["silence_trimmed_sec"], summary["silence_trimmed_sec"])
            self.assertEqual(sanitized["silence_trim_cuts"], 1)

            # Diagnose markdown renders sent duration with arrow when trimmed and remains compatible for legacy records
            legacy_rec = {k: v for k, v in sanitized.items() if k not in ("silence_trimmed_sec", "silence_trim_cuts")}
            md = format_live_diagnostics_markdown(
                {
                    "version": "0.16.0",
                    "diag_elapsed_ms": 5,
                    "requested_last": 2,
                    "daemon": {
                        "online": False,
                        "state": "stopped",
                        "pid": None,
                        "pending_count": 0,
                        "shim_ok": None,
                        "asr_provider": "fake",
                        "asr_model": "fake-model",
                        "refiner_provider": "off",
                        "refiner_model": "",
                        "refiner_effort": "auto",
                    },
                    "pipeline": {},
                    "summary": {"total": 2, "ok_count": 2, "fallback_count": 0, "failed_or_skipped": 0},
                    "session_match": {"matched": False},
                    "storage": {"telemetry_kb": 0.1, "failed_audio_count": 0, "newest_failed_at": ""},
                    "recent_results": [
                        {**sanitized, "session_tag": "none", "diag_flags": []},
                        {**legacy_rec, "seq_id": 2, "session_tag": "none", "diag_flags": []},
                    ],
                    "daemon_log_tail": "",
                }
            )
            self.assertIn("→4.6", md)

            # 2. retry bypasses silence trimming and omits silence_trimmed_* telemetry keys
            asr_retry = _RecordingAsr(single_hop=False)
            res_retry = run_pipeline_once(
                raw_wav,
                cfg=Config(audio=AudioConfig(silence_trim="balanced"), refiner=RefinerConfig(provider="off")),
                asr_provider=asr_retry,
                refiner=NoopRefiner(),
                audio_source="retry",
                dry_run=True,
                runtime_dir=rt,
                env=env,
            )
            self.assertTrue(res_retry.ok)
            assert res_retry.gate is not None
            self.assertEqual(res_retry.gate.trimmed_sec, 0.0)
            self.assertEqual(res_retry.gate.trim_cuts, 0)
            self.assertEqual(asr_retry.seen_wav_lens, [len(raw_wav)])
            retry_summary = build_pipeline_summary(2, res_retry, dry_run=True)
            self.assertNotIn("silence_trimmed_sec", retry_summary)
            self.assertNotIn("silence_trim_cuts", retry_summary)

            # 3. Single-hop provider also receives the trimmed WAV bytes
            asr_sh = _RecordingAsr(single_hop=True)
            res_sh = run_pipeline_once(
                raw_wav,
                cfg=Config(audio=AudioConfig(silence_trim="balanced"), refiner=RefinerConfig(provider="off")),
                asr_provider=asr_sh,
                refiner=NoopRefiner(),
                audio_source="file",
                dry_run=True,
                runtime_dir=rt,
                env=env,
            )
            self.assertTrue(res_sh.ok)
            self.assertEqual(asr_sh.seen_wav_lens, asr_norm.seen_wav_lens)

            # 4. P1-1 regression: recording passing min_duration_sec=4.0 trims to <4s in failed/, then retry succeeds
            from agent_voice_input.asr_providers import AsrError

            class _FailOnceAsr(BaseAsrProvider):
                provider_name = "fake"

                def __init__(self) -> None:
                    pass

                def transcribe(self, wav_bytes: bytes, **_kw: object) -> AsrResult:
                    raise AsrError("simulated_asr_outage")

            short_after_trim_raw = pack_pcm16_mono_wav(
                self._synth_sine(1.2, -20.0)
                + self._synth_noise(6.4, -75.0, seed=31)
                + self._synth_sine(1.2, -20.0)
            )  # 8.8s raw -> ~3.0s after balanced trim (< 4.0s min_duration_sec!)
            cfg_min4 = Config(
                audio=AudioConfig(min_duration_sec=4.0, silence_trim="balanced"),
                refiner=RefinerConfig(provider="off"),
            )
            with self.assertRaises(AsrError) as fail_ctx:
                run_pipeline_once(
                    short_after_trim_raw,
                    cfg=cfg_min4,
                    asr_provider=_FailOnceAsr(),
                    refiner=NoopRefiner(),
                    audio_source="avfoundation",
                    dry_run=True,
                    runtime_dir=rt,
                    env=env,
                )
            stashed_path = Path(str(getattr(fail_ctx.exception, "_saved_audio_path", "")))
            self.assertTrue(stashed_path.is_file())
            stashed_bytes = stashed_path.read_bytes()
            _, _, _, stashed_arr = _unpack_wav_samples(stashed_bytes)
            self.assertLess(len(stashed_arr) / float(sr), 4.0)

            asr_recover = _RecordingAsr(single_hop=False)
            res_recover = run_pipeline_once(
                stashed_bytes,
                cfg=cfg_min4,
                asr_provider=asr_recover,
                refiner=NoopRefiner(),
                audio_source="retry",
                dry_run=True,
                runtime_dir=rt,
                env=env,
            )
            self.assertTrue(res_recover.ok, f"retry failed: {res_recover.skipped_reason} {res_recover.error}")
            self.assertEqual(asr_recover.seen_wav_lens, [len(stashed_bytes)])

            # 5. Retry regression for elevated min_voiced_sec=3.0:
            #    two 0.8s -13 dBFS speech clips + 7.0s -46 dBFS noise (> -50 dBFS floor -> voiced_sec > 3.0 raw),
            #    trimmed down to voiced_sec < 3.0 in failed/, MUST still recover via retry without 'silence' rejection.
            voiced_trim_raw = pack_pcm16_mono_wav(
                self._synth_sine(0.8, -13.0)
                + self._synth_noise(7.0, -46.0, seed=32)
                + self._synth_sine(0.8, -13.0)
            )
            cfg_mv3 = Config(
                audio=AudioConfig(floor_dbfs=-50.0, min_voiced_sec=3.0, silence_trim="balanced"),
                refiner=RefinerConfig(provider="off"),
            )
            with self.assertRaises(AsrError) as mv3_ctx:
                run_pipeline_once(
                    voiced_trim_raw,
                    cfg=cfg_mv3,
                    asr_provider=_FailOnceAsr(),
                    refiner=NoopRefiner(),
                    audio_source="avfoundation",
                    dry_run=True,
                    runtime_dir=rt,
                    env=env,
                )
            mv3_stashed_path = Path(str(getattr(mv3_ctx.exception, "_saved_audio_path", "")))
            self.assertTrue(mv3_stashed_path.is_file())
            mv3_stashed_bytes = mv3_stashed_path.read_bytes()
            # Confirm the stashed audio indeed fails raw min_voiced_sec=3.0 without retry cap
            raw_recheck = process_wav(mv3_stashed_bytes, cfg_mv3.audio, silence_trim="off")
            self.assertFalse(raw_recheck.passed)
            self.assertEqual(raw_recheck.reason, "silence")

            asr_mv3_recover = _RecordingAsr(single_hop=False)
            res_mv3_recover = run_pipeline_once(
                mv3_stashed_bytes,
                cfg=cfg_mv3,
                asr_provider=asr_mv3_recover,
                refiner=NoopRefiner(),
                audio_source="retry",
                dry_run=True,
                runtime_dir=rt,
                env=env,
            )
            self.assertTrue(
                res_mv3_recover.ok,
                f"retry failed under min_voiced_sec=3.0: {res_mv3_recover.skipped_reason} {res_mv3_recover.error}",
            )
            self.assertEqual(asr_mv3_recover.seen_wav_lens, [len(mv3_stashed_bytes)])


if __name__ == "__main__":
    unittest.main()
