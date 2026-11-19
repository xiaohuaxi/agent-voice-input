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

from agent_voice_input.audio_proc import (  # noqa: E402
    DEFAULT_MAX_CHUNK_SEC,
    PEAK_LIMIT_INT16,
    SINGLE_HOP_MAX_CHUNK_SEC,
    SINGLE_HOP_MP3_MIN_WAV_BYTES,
    decode_to_16k_mono_pcm,
    encode_wav_to_mp3_bytes,
    inspect_wav_chunks,
    list_failed_audios,
    pack_pcm16_mono_wav,
    process_wav,
    resolve_failed_audio,
    save_failed_audio,
    split_wav_chunks,
)
from agent_voice_input.config import (  # noqa: E402
    ASR_AUTO_PRIORITY,
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
            self.assertEqual(cfg.asr.effective_model(), "gemini-3.5-flash-lite")
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
            (creds.doubao_app_id.value, creds.doubao_app_id.source_env),
            ("db_appid_suffix", "DOUBAO_ASR_APP_ID"),
        )
        self.assertEqual(
            (creds.doubao_key.value, creds.doubao_key.source_env),
            ("db_key_suffix", "DOUBAO_ASR_API_KEY"),
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
            ("dashscope", "doubao", "openrouter", "gemini", "openai", "groq", "siliconflow"),
        )
        # Full ordered priority chain check
        full_env = {
            "DASHSCOPE_API_KEY": "k_dash",
            "DOUBAO_ASR_API_KEY": "k_db",
            "OPENROUTER_API_KEY": "k_or",
            "GEMINI_API_KEY": "k_gem",
            "OPENAI_API_KEY": "k_oai",
            "GROQ_API_KEY": "k_groq",
            "SILICONFLOW_API_KEY": "k_sf",
        }
        expected_chain = [
            ("DASHSCOPE_API_KEY", "dashscope"),
            ("DOUBAO_ASR_API_KEY", "doubao"),
            ("OPENROUTER_API_KEY", "openrouter"),
            ("GEMINI_API_KEY", "gemini"),
            ("OPENAI_API_KEY", "openai"),
            ("GROQ_API_KEY", "groq"),
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
            f"appid=appid_val_98765; toml_custom=toml_custom_provider_secret; "
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
            frozenset({"auto", "dashscope", "doubao", "openrouter", "gemini", "openai", "groq", "siliconflow", "openai-compatible"}),
        )
        self.assertEqual(
            VALID_REFINER_PROVIDERS,
            frozenset({"auto", "codex", "deepseek", "dashscope", "volcengine", "openrouter", "gemini", "openai", "groq", "siliconflow", "openai-compatible", "off"}),
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
        self.assertEqual(DEFAULT_ASR_MODELS["gemini"], "gemini-3.5-flash-lite")
        self.assertEqual(DEFAULT_ASR_MODELS["openai"], "gpt-4o-mini-transcribe")
        self.assertEqual(DEFAULT_ASR_MODELS["groq"], "whisper-large-v3-turbo")
        self.assertEqual(DEFAULT_ASR_MODELS["siliconflow"], "FunAudioLLM/SenseVoiceSmall")
        self.assertEqual(DEFAULT_ASR_MODELS["openai-compatible"], "gpt-4o-mini-transcribe")

        self.assertEqual(DEFAULT_REFINER_MODELS["codex"], "gpt-6-luna")
        self.assertEqual(DEFAULT_REFINER_MODELS["deepseek"], "deepseek-flash")
        self.assertEqual(DEFAULT_REFINER_MODELS["dashscope"], "qwen3.8-flash")
        self.assertEqual(DEFAULT_REFINER_MODELS["volcengine"], "doubao-seed-2-0-mini-260428")
        self.assertEqual(DEFAULT_REFINER_MODELS["openrouter"], "google/gemini-3.5-flash-lite")
        self.assertEqual(DEFAULT_REFINER_MODELS["gemini"], "gemini-3.5-flash-lite")
        self.assertEqual(DEFAULT_REFINER_MODELS["openai"], "gpt-4o-mini")
        self.assertEqual(DEFAULT_REFINER_MODELS["groq"], "llama-3.3-70b-versatile")
        self.assertEqual(DEFAULT_REFINER_MODELS["siliconflow"], "Qwen/Qwen2.5-7B-Instruct")

        self.assertEqual(DEFAULT_REFINER_EFFORTS, {"codex": "low", "openai": "low"})
        self.assertEqual(RefinerConfig(provider="codex").effort, "low")
        self.assertEqual(RefinerConfig(provider="openai").effort, "low")
        for non_codex in ("deepseek", "openrouter", "dashscope", "volcengine", "gemini", "openai-compatible"):
            self.assertEqual(RefinerConfig(provider=non_codex).effort, "auto")

        self.assertEqual(DEFAULT_ASR_BASE_URLS["openrouter"], "https://openrouter.ai/api/v1")
        self.assertEqual(DEFAULT_ASR_BASE_URLS["groq"], "https://api.groq.com/openai/v1")
        self.assertEqual(DEFAULT_ASR_BASE_URLS["siliconflow"], "https://api.siliconflow.cn/v1")
        self.assertEqual(DEFAULT_REFINER_BASE_URLS["deepseek"], "https://api.deepseek.com/v1")
        self.assertEqual(DEFAULT_REFINER_BASE_URLS["volcengine"], "https://ark.cn-beijing.volces.com/api/v3")
        self.assertEqual(DEFAULT_REFINER_BASE_URLS["dashscope"], "https://dashscope.aliyuncs.com/compatible-mode/v1")

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            # Legacy v0.1.0 aliases and stage-mismatched providers MUST raise ConfigError
            for bad_asr in ("ali", "qwen", "gemini-flash-audio", "codex-luna", "volcengine", "codex", "deepseek", "off"):
                with self.subTest(bad_asr=bad_asr):
                    with self.assertRaises(ConfigError):
                        load_config(env={"HOME": str(home)}, home_dir=home, cli_overrides={"asr.provider": bad_asr})
            for bad_refiner in ("codex-luna", "ali", "qwen", "gemini-flash-audio", "doubao"):
                with self.subTest(bad_refiner=bad_refiner):
                    with self.assertRaises(ConfigError):
                        load_config(env={"HOME": str(home)}, home_dir=home, cli_overrides={"refiner.provider": bad_refiner})

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
            self.assertEqual(cfg_sw.refiner.model, "google/gemini-3.5-flash-lite")
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
        if shutil.which("ffmpeg") is None:
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
        if shutil.which("ffmpeg") is None:
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
        from agent_voice_input.platform_macos import MacOSPlatform
        from agent_voice_input.platform_wsl import WslPlatform

        seen_drains: list[float] = []
        wplat = WslPlatform(Config(), env={})
        wplat._rec_mode = "pulse"
        wplat._ff_rec.stop = lambda *, drain_sec=0.0: (seen_drains.append(drain_sec) or b"")  # type: ignore[method-assign]
        wplat.stop_recording()
        wplat._rec_mode = "pulse"
        wplat.cancel_recording()

        mplat = MacOSPlatform(Config(), env={})
        mplat._recording = True
        mplat._ff_rec.stop = lambda *, drain_sec=0.0: (seen_drains.append(drain_sec) or b"")  # type: ignore[method-assign]
        mplat.stop_recording()
        mplat._recording = True
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
            kept = list_failed_audios(rt)
            self.assertEqual(len(kept), 10)
            self.assertIn("proj-10", kept[0].name)
            self.assertIn("proj-1", kept[-1].name)

            self.assertEqual(resolve_failed_audio(rt), kept[0])
            self.assertEqual(resolve_failed_audio(rt, "1"), kept[0])
            self.assertEqual(resolve_failed_audio(rt, "10"), kept[-1])
            self.assertEqual(resolve_failed_audio(rt, "proj-7"), [p for p in kept if "proj-7" in p.name][0])
            with self.assertRaises(FileNotFoundError):
                resolve_failed_audio(rt, "proj-")
            with self.assertRaises(FileNotFoundError):
                resolve_failed_audio(rt, "99")

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
        ff_bin = shutil.which("ffmpeg")
        if ff_bin is not None:
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


if __name__ == "__main__":
    unittest.main()
