"""Unit tests for ``agent_voice_input.config`` and ``agent_voice_input.audio_proc``."""
from __future__ import annotations

import array
import io
import json
import math
import os
from pathlib import Path
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
    PEAK_LIMIT_INT16,
    decode_to_16k_mono_pcm,
    pack_pcm16_mono_wav,
    process_wav,
)
from agent_voice_input.config import (  # noqa: E402
    Config,
    ConfigError,
    _parse_simple_toml,
    ensure_secure_dir,
    get_runtime_dir,
    is_volces_host,
    load_config,
    load_env_files,
    parse_env_text,
    parse_toml,
    redact_secrets,
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
        ]
        for doc, expected_line in bad_cases:
            with self.subTest(doc=repr(doc)):
                with self.assertRaises(ConfigError) as ctx:
                    _parse_simple_toml(doc)
                self.assertIn(expected_line, str(ctx.exception))


class ConfigAndCredentialsTests(unittest.TestCase):
    def test_config_precedence_cli_over_env_over_file_over_default(self) -> None:
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
                provider = "qwen"
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

            env = {
                "HOME": str(home),
                "VOICE_INPUT_HOTKEY": "RAlt",
                "VOICE_INPUT_ASR_PROVIDER": "doubao",
            }
            cfg, _ = load_config(
                env=env,
                home_dir=home,
                cli_overrides={"asr.provider": "gemini", "hotkey.debounce_ms": None},
            )
            self.assertEqual(cfg.hotkey.key, "RAlt")
            self.assertEqual(cfg.hotkey.trigger_mode, "ptt")
            self.assertEqual(cfg.hotkey.tap_max_ms, 900)
            self.assertEqual(cfg.hotkey.debounce_ms, 180)
            self.assertEqual(cfg.asr.provider, "gemini")
            self.assertEqual(cfg.asr.effective_model(), "gemini-2.5-flash")
            self.assertEqual(cfg.refiner.provider, "off")
            self.assertEqual(cfg.refiner.effort, "medium")

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
                "VOICE_INPUT_HOTKEY": "F8",
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
            # AGENT_VOICE_INPUT_* takes precedence over VOICE_INPUT_*, while VOICE_INPUT_* still works as fallback
            self.assertEqual(cfg.hotkey.key, "F9")
            self.assertEqual(cfg.hotkey.trigger_mode, "ptt")

    def test_rejects_float_for_int_nonfinite_float_and_invalid_luna_effort(self) -> None:
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
            with self.assertRaises(ConfigError):
                load_config(
                    env={"HOME": str(home), "VOICE_INPUT_REFINER_EFFORT": "none"},
                    home_dir=home,
                )
            with self.assertRaises(ConfigError):
                load_config(
                    env={"HOME": str(home), "VOICE_INPUT_REFINER_EFFORT": "ultra"},
                    home_dir=home,
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
                    env={"HOME": str(home), "VOICE_INPUT_TAIL_DRAIN_SEC": "650"},
                    home_dir=home,
                )

    def test_beep_default_false_and_platform_adaptive_tail_drain_sec(self) -> None:
        default_cfg = Config()
        self.assertIs(default_cfg.audio.beep, False)
        self.assertIsNone(default_cfg.audio.tail_drain_sec)
        self.assertAlmostEqual(default_cfg.audio.effective_tail_drain_sec(macos=False), 0.65)
        self.assertAlmostEqual(default_cfg.audio.effective_tail_drain_sec(macos=True), 0.20)

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cfg_zero, _ = load_config(
                env={"HOME": str(home), "VOICE_INPUT_TAIL_DRAIN_SEC": "0.0"},
                home_dir=home,
            )
            self.assertEqual(cfg_zero.audio.tail_drain_sec, 0.0)
            self.assertEqual(cfg_zero.audio.effective_tail_drain_sec(macos=False), 0.0)
            self.assertEqual(cfg_zero.audio.effective_tail_drain_sec(macos=True), 0.0)

    def test_credential_suffix_first_priority_and_strict_no_api_id_typo(self) -> None:
        cfg = Config()
        creds = resolve_credentials(
            cfg,
            {
                "ALI_API_KEY": "ali_suffix_v",
                "API_KEY_ALI": "ali_prefix_v",
                "DASHSCOPE_API_KEY": "ali_dash_v",
                "HUOSHAN_API_KEY": "hs_suffix_v",
                "API_KEY_HUOSHAN": "hs_prefix_v",
                "ARK_API_KEY": "hs_ark_v",
                "DOUBAO_ASR_APP_ID": "db_appid_suffix",
                "APP_ID_DOUBAO_ASR": "db_appid_prefix",
                "DOUBAO_ASR_API_KEY": "db_key_suffix",
                "API_KEY_DOUBAO_ASR": "db_key_prefix",
                "GEMINI_API_KEY": "gem_suffix_v",
                "API_KEY_GEMINI": "gem_prefix_v",
                "OPENAI_API_KEY": "oai_suffix_v",
                "API_KEY_OPENAI": "oai_prefix_v",
            },
        )
        assert creds.ali is not None
        assert creds.huoshan is not None
        assert creds.doubao_app_id is not None
        assert creds.doubao_key is not None
        assert creds.gemini is not None
        assert creds.openai is not None
        self.assertEqual((creds.ali.value, creds.ali.source_env), ("ali_suffix_v", "ALI_API_KEY"))
        self.assertEqual(
            (creds.huoshan.value, creds.huoshan.source_env), ("hs_suffix_v", "HUOSHAN_API_KEY")
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
            (creds.gemini.value, creds.gemini.source_env), ("gem_suffix_v", "GEMINI_API_KEY")
        )
        self.assertEqual(
            (creds.openai.value, creds.openai.source_env), ("oai_suffix_v", "OPENAI_API_KEY")
        )

        self.assertNotIn("ali_suffix_v", repr(creds))
        self.assertNotIn("ali_suffix_v", str(creds.ali))
        self.assertIn("ALI_API_KEY", str(creds.ali))

        # REDLINE REGRESSION: typo API_ID_DOUBAO_ASR / DOUBAO_ASR_API_ID MUST return None!
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
            select_asr_provider(cfg, resolve_credentials(cfg, {"API_KEY_DOUBAO_ASR": "k"})),
            "doubao",
        )
        self.assertEqual(
            select_asr_provider(
                cfg,
                resolve_credentials(cfg, {"API_KEY_ALI": "a", "API_KEY_DOUBAO_ASR": "k"}),
            ),
            "ali",
        )
        self.assertEqual(
            select_asr_provider(cfg, resolve_credentials(cfg, {"API_KEY_GEMINI": "g"})),
            "gemini",
        )
        self.assertEqual(
            select_asr_provider(cfg, resolve_credentials(cfg, {"API_KEY_OPENAI": "o"})),
            "openai-compatible",
        )
        self.assertEqual(select_asr_provider(cfg, resolve_credentials(cfg, {})), "ali")

        # Custom third-party api_key_env / base_url with provider='auto' MUST NOT jump to doubao!
        cfg_groq = Config()
        cfg_groq.asr.api_key_env = "GROQ_CUSTOM_KEY"
        env_mixed = {"GROQ_CUSTOM_KEY": "groq_val_123", "DOUBAO_ASR_API_KEY": "db_val_456"}
        self.assertEqual(
            select_asr_provider(cfg_groq, resolve_credentials(cfg_groq, env_mixed)),
            "openai-compatible",
        )

        # Explicit Doubao resource_id under provider='auto' MUST route to doubao even if ALI key exists
        cfg_db_rid = Config()
        cfg_db_rid.asr.resource_id = "volc.seedasr.auc"
        self.assertEqual(
            select_asr_provider(
                cfg_db_rid,
                resolve_credentials(cfg_db_rid, {"ALI_API_KEY": "a", "DOUBAO_ASR_API_KEY": "d"}),
            ),
            "doubao",
        )

        # Strict hostname check for volces.com (prevent substring spoofing)
        self.assertTrue(is_volces_host("https://ark.cn-beijing.volces.com/api/v3"))
        self.assertFalse(is_volces_host("https://evil-notvolces.com/v1"))

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
        if not Path("/usr/bin/ffmpeg").is_file():
            self.skipTest("/usr/bin/ffmpeg not installed")
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

        # 2. Real /usr/bin/ffmpeg streaming s16le pipe with low-latency flags + tail_drain_sec
        if not Path("/usr/bin/ffmpeg").is_file():
            self.skipTest("/usr/bin/ffmpeg not installed")
        rec_live = FfmpegWavRecorder()
        with tempfile.TemporaryDirectory() as td:
            rec_live.start("lavfi", "sine=frequency=440:sample_rate=16000", Path(td), os.environ)
            time.sleep(0.22)
            live_wav = rec_live.stop(drain_sec=0.15)
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


if __name__ == "__main__":
    unittest.main()
