"""Offline unit tests for HUD UI language detection, config overrides, WSL2 & macOS HUD RPC, and bilingual copy."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _REPO_ROOT / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from agent_voice_input.config import (  # noqa: E402
    Config,
    ConfigError,
    HotkeyConfig,
    UiConfig,
    VALID_UI_POSITIONS,
    load_config,
    normalize_ui_language,
    normalize_ui_position,
    redact_secrets,
)
from agent_voice_input.hud import (  # noqa: E402
    DEFAULT_ERROR_HUD_DURATION_MS,
    HUD_STATE_DURATIONS_MS,
    default_hud_duration_ms,
    format_countdown_hint,
    format_hotkey_label,
    format_hud_text,
    format_recording_duration,
    is_zh_locale,
    pcm16le_rms_level,
    recording_countdown_warn_sec,
    resolve_ui_language,
)
from agent_voice_input.platform_macos import (  # noqa: E402
    MacOSPlatform,
    probe_macos_host,
)
from agent_voice_input.platform_wsl import (  # noqa: E402
    FfmpegWavRecorder,
    ForegroundWindow,
    POWERSHELL_EXE,
    WINDOWS_TERMINALS,
    WslPlatform,
    probe_wsl_host,
)


_MOCK_HUD_SHIM = (
    "import json, pathlib, sys\n"
    "ui_cult, log_path = sys.argv[1], pathlib.Path(sys.argv[2])\n"
    "ready = {'event': 'ready'} if ui_cult == '__OMIT__' else {'event': 'ready', 'ui_culture': ui_cult}\n"
    "sys.stdout.write(json.dumps(ready) + '\\n'); sys.stdout.flush()\n"
    "for ln in sys.stdin:\n"
    "    req = json.loads(ln)\n"
    "    if req.get('cmd') == 'quit': break\n"
    "    with log_path.open('a', encoding='utf-8') as f:\n"
    "        f.write(json.dumps(req, ensure_ascii=False) + '\\n')\n"
    "    if int(req.get('seq_id') or 0) > 0:\n"
    "        sys.stdout.write(json.dumps({'type': 'reply', 'reply': req['cmd'], 'seq_id': req['seq_id'], 'ok': True}) + '\\n')\n"
    "        sys.stdout.flush()\n"
)


class HudLanguageConfigAndResolutionTests(unittest.TestCase):
    def test_config_and_env_ui_language_normalization_and_validation(self) -> None:
        self.assertEqual(Config().ui.language, "auto")
        self.assertEqual(normalize_ui_language("auto"), "auto")
        self.assertEqual(normalize_ui_language(" AUTO "), "auto")
        for zh_tag in ("zh", "ZH", "zh-CN", "zh_CN", "zh-TW", "zh-HK", "zh-Hans", "zh_CN.UTF-8"):
            self.assertEqual(normalize_ui_language(zh_tag), "zh", msg=zh_tag)
        for en_tag in ("en", "EN", "en-US", "en_US", "en-GB", "en_US.UTF-8", "en_US.UTF-8@euro"):
            self.assertEqual(normalize_ui_language(en_tag), "en", msg=en_tag)

        for bad_val in ("", "   ", "fr", "ja-JP", "C", "C.UTF-8", "POSIX", "enable", "zhx", "zh-", "en_", True, 123):
            with self.subTest(bad_val=bad_val):
                with self.assertRaises(ConfigError):
                    normalize_ui_language(bad_val)

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cfg_dir = home / ".config" / "agent-voice-input"
            cfg_dir.mkdir(parents=True)
            (cfg_dir / "config.toml").write_text('[ui]\nlanguage = "zh-CN"\n', encoding="utf-8")

            # File sets zh-CN -> normalized to 'zh'
            cfg_file, _ = load_config(env={"HOME": str(home)}, home_dir=home)
            self.assertEqual(cfg_file.ui.language, "zh")

            # VOICE_INPUT_UI_LANGUAGE overrides file; AGENT_VOICE_INPUT_UI_LANGUAGE overrides legacy prefix
            cfg_legacy_env, _ = load_config(
                env={"HOME": str(home), "VOICE_INPUT_UI_LANGUAGE": "en_US"},
                home_dir=home,
            )
            self.assertEqual(cfg_legacy_env.ui.language, "en")

            cfg_pref_env, _ = load_config(
                env={
                    "HOME": str(home),
                    "AGENT_VOICE_INPUT_UI_LANGUAGE": "zh_TW",
                    "VOICE_INPUT_UI_LANGUAGE": "en_US",
                },
                home_dir=home,
            )
            self.assertEqual(cfg_pref_env.ui.language, "zh")

            # CLI override wins over env and file
            cfg_cli, _ = load_config(
                env={"HOME": str(home), "AGENT_VOICE_INPUT_UI_LANGUAGE": "zh"},
                home_dir=home,
                cli_overrides={"ui.language": "en-GB"},
            )
            self.assertEqual(cfg_cli.ui.language, "en")

            # Invalid env or unknown [ui] key raises ConfigError
            with self.assertRaises(ConfigError):
                load_config(
                    env={"HOME": str(home), "AGENT_VOICE_INPUT_UI_LANGUAGE": "fr-FR"},
                    home_dir=home,
                )
            (cfg_dir / "config.toml").write_text('[ui]\nunknown_opt = "en"\n', encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(env={"HOME": str(home)}, home_dir=home)

    def test_config_and_env_ui_position_and_non_secret_redaction(self) -> None:
        self.assertEqual(VALID_UI_POSITIONS, frozenset({"bottom", "top"}))
        self.assertEqual(Config().ui.position, "bottom")
        self.assertEqual(normalize_ui_position(), "bottom")
        self.assertEqual(normalize_ui_position("bottom"), "bottom")
        self.assertEqual(normalize_ui_position(" BOTTOM "), "bottom")
        self.assertEqual(normalize_ui_position("top"), "top")
        self.assertEqual(normalize_ui_position(" Top "), "top")

        for bad_pos in ("", "   ", "center", "middle", "left", "right", "auto", True, 123):
            with self.subTest(bad_pos=bad_pos):
                with self.assertRaises(ValueError):
                    normalize_ui_position(bad_pos)

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cfg_dir = home / ".config" / "agent-voice-input"
            cfg_dir.mkdir(parents=True)

            # Default without config file is 'bottom'
            cfg_def, _ = load_config(env={"HOME": str(home)}, home_dir=home)
            self.assertEqual(cfg_def.ui.position, "bottom")

            # TOML sets 'top'
            (cfg_dir / "config.toml").write_text('[ui]\nposition = "TOP"\n', encoding="utf-8")
            cfg_top, _ = load_config(env={"HOME": str(home)}, home_dir=home)
            self.assertEqual(cfg_top.ui.position, "top")

            # Env overrides TOML; AGENT_VOICE_INPUT_UI_POSITION beats VOICE_INPUT_UI_POSITION
            cfg_legacy, _ = load_config(
                env={"HOME": str(home), "VOICE_INPUT_UI_POSITION": "bottom"},
                home_dir=home,
            )
            self.assertEqual(cfg_legacy.ui.position, "bottom")

            cfg_pref, _ = load_config(
                env={
                    "HOME": str(home),
                    "AGENT_VOICE_INPUT_UI_POSITION": "top",
                    "VOICE_INPUT_UI_POSITION": "bottom",
                },
                home_dir=home,
            )
            self.assertEqual(cfg_pref.ui.position, "top")

            # CLI override beats env and TOML
            cfg_cli, _ = load_config(
                env={"HOME": str(home), "AGENT_VOICE_INPUT_UI_POSITION": "top"},
                home_dir=home,
                cli_overrides={"ui.position": "bottom"},
            )
            self.assertEqual(cfg_cli.ui.position, "bottom")

            with self.assertRaises(ConfigError):
                load_config(
                    env={"HOME": str(home), "AGENT_VOICE_INPUT_UI_POSITION": "middle"},
                    home_dir=home,
                )

        # Ensure non-secret env vars never cause redact_secrets to mask 'bottom'/'top'/'true'/'false'
        non_sec_env = {
            "AGENT_VOICE_INPUT_UI_POSITION": "bottom",
            "VOICE_INPUT_UI_POSITION": "top",
            "AGENT_VOICE_INPUT_ONLY_IN_TERMINALS": "true",
            "VOICE_INPUT_ONLY_IN_TERMINALS": "false",
        }
        sample = "hud position=bottom or top, only_in_terminals=true or false"
        self.assertEqual(redact_secrets(sample, non_sec_env), sample)

    def test_wsl2_windows_ui_culture_not_misled_by_posix_lang(self) -> None:
        # Core trap: WSL2 Ubuntu often has LANG=C.UTF-8 or en_US.UTF-8 even on Chinese Windows desktop!
        wsl_c_env = {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
        wsl_en_env = {"LANG": "en_US.UTF-8"}
        self.assertEqual(resolve_ui_language("auto", env=wsl_c_env, win_ui_culture="zh-CN"), "zh")
        self.assertEqual(resolve_ui_language("auto", env=wsl_en_env, win_ui_culture="zh-TW"), "zh")
        self.assertEqual(resolve_ui_language("auto", env=wsl_en_env, win_ui_culture="zh-HK"), "zh")
        self.assertEqual(resolve_ui_language("auto", env=wsl_en_env, win_ui_culture="zh-Hans"), "zh")

        # Conversely, English (or non-zh / empty) Windows desktop stays English even if WSL2 LANG is zh_CN.UTF-8
        wsl_zh_env = {"LANG": "zh_CN.UTF-8", "LC_ALL": "zh_CN.UTF-8"}
        self.assertEqual(resolve_ui_language("auto", env=wsl_zh_env, win_ui_culture="en-US"), "en")
        self.assertEqual(resolve_ui_language("auto", env=wsl_zh_env, win_ui_culture="ja-JP"), "en")
        self.assertEqual(resolve_ui_language("auto", env=wsl_zh_env, win_ui_culture=""), "en")

        # Explicit config or *_UI_LANGUAGE env always wins over Windows UI culture
        self.assertEqual(resolve_ui_language("en", env=wsl_c_env, win_ui_culture="zh-CN"), "en")
        self.assertEqual(resolve_ui_language("zh-CN", env=wsl_en_env, win_ui_culture="en-US"), "zh")
        self.assertEqual(
            resolve_ui_language(
                "auto",
                env={"LANG": "C.UTF-8", "AGENT_VOICE_INPUT_UI_LANGUAGE": "en_US"},
                win_ui_culture="zh-CN",
            ),
            "en",
        )
        self.assertEqual(
            resolve_ui_language(
                "auto",
                env={"LANG": "en_US.UTF-8", "VOICE_INPUT_UI_LANGUAGE": "zh_CN"},
                win_ui_culture="en-US",
            ),
            "zh",
        )
        # AGENT_VOICE_INPUT_UI_LANGUAGE=auto takes precedence over legacy VOICE_INPUT_UI_LANGUAGE=en
        self.assertEqual(
            resolve_ui_language(
                "auto",
                env={
                    "AGENT_VOICE_INPUT_UI_LANGUAGE": "auto",
                    "VOICE_INPUT_UI_LANGUAGE": "en",
                },
                win_ui_culture="zh-CN",
            ),
            "zh",
        )

    def test_posix_locale_precedence(self) -> None:
        self.assertTrue(is_zh_locale("zh"))
        self.assertTrue(is_zh_locale("zh_CN.UTF-8"))
        self.assertTrue(is_zh_locale("zh-Hant-HK"))
        self.assertFalse(is_zh_locale("zhx"))
        self.assertFalse(is_zh_locale("C.UTF-8"))

        # LC_ALL > LC_MESSAGES > LANG when win_ui_culture is None
        self.assertEqual(
            resolve_ui_language(
                "auto",
                env={"LC_ALL": "C.UTF-8", "LC_MESSAGES": "zh_CN.UTF-8", "LANG": "zh_CN.UTF-8"},
            ),
            "en",
        )
        self.assertEqual(
            resolve_ui_language(
                "auto",
                env={"LC_ALL": "", "LC_MESSAGES": "zh_TW.UTF-8", "LANG": "en_US.UTF-8"},
            ),
            "zh",
        )
        self.assertEqual(
            resolve_ui_language(
                "auto",
                env={"LC_ALL": "", "LC_MESSAGES": "en_US.UTF-8", "LANG": "zh_CN.UTF-8"},
            ),
            "en",
        )
        self.assertEqual(resolve_ui_language("auto", env={"LANG": "zh_CN.UTF-8"}), "zh")
        self.assertEqual(resolve_ui_language("auto", env={"LANG": "zh_TW.UTF-8"}), "zh")
        self.assertEqual(resolve_ui_language("auto", env={"LANG": "C.UTF-8"}), "en")
        self.assertEqual(resolve_ui_language("auto", env={"LANG": "POSIX"}), "en")
        self.assertEqual(resolve_ui_language("auto", env={"LANG": "en_US.UTF-8"}), "en")
        self.assertEqual(resolve_ui_language("auto", env={}), "en")

    def test_default_hud_duration_ms_contract(self) -> None:
        for persistent in ("", "   ", "idle", " IDLE ", "hide", " HIDE ", "recording", "recognizing", "processing", "refining"):
            self.assertEqual(default_hud_duration_ms(persistent), 0, msg=persistent)
        self.assertEqual(HUD_STATE_DURATIONS_MS["done"], 900)
        self.assertEqual(HUD_STATE_DURATIONS_MS["copied"], 2000)
        self.assertEqual(HUD_STATE_DURATIONS_MS["empty"], 2200)
        self.assertEqual(HUD_STATE_DURATIONS_MS["error"], 2600)
        self.assertEqual(DEFAULT_ERROR_HUD_DURATION_MS, 2600)
        self.assertEqual(default_hud_duration_ms("done"), 900)
        self.assertEqual(default_hud_duration_ms(" DONE "), 900)
        self.assertEqual(default_hud_duration_ms("copied"), 2000)
        self.assertEqual(default_hud_duration_ms("empty"), 2200)
        self.assertEqual(default_hud_duration_ms("error"), 2600)
        self.assertEqual(default_hud_duration_ms("asr_timeout"), 2600)

    def test_wsl_platform_hud_production_rpc_and_probe_never_launch_real_powershell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)

            def _run_hud_scenario(
                *,
                cfg: Config,
                env: dict[str, str],
                ready_ui_culture: str,
                calls: list[tuple[str, str, int]],
            ) -> list[dict]:
                log_path = tmp_dir / f"rpc-{ready_ui_culture}-{len(list(tmp_dir.iterdir()))}.jsonl"
                plat = WslPlatform(
                    cfg,
                    env=env,
                    shim_cmd_factory=lambda: [
                        sys.executable,
                        "-u",
                        "-c",
                        _MOCK_HUD_SHIM,
                        ready_ui_culture,
                        str(log_path),
                    ],
                )
                try:
                    plat.start_event_listener(lambda _e: None)
                    for state, custom_text, dur_ms in calls:
                        plat.hud(state, text=custom_text, duration_ms=dur_ms)
                finally:
                    plat.close()
                if not log_path.exists():
                    return []
                return [
                    json.loads(ln)
                    for ln in log_path.read_text(encoding="utf-8").splitlines()
                    if ln.strip()
                ]

            # 1. Chinese Windows desktop (ready ui_culture='zh-CN') + distracting Linux LANG=C.UTF-8 / en_US.UTF-8
            zh_cmds = _run_hud_scenario(
                cfg=Config(),
                env={"LANG": "C.UTF-8", "LC_ALL": "en_US.UTF-8"},
                ready_ui_culture="zh-CN",
                calls=[
                    ("recording", "", 0),
                    ("recognizing", "", 0),
                    ("refining", "", 0),
                    ("done", "", 0),
                    ("copied", "", 0),
                    ("empty", "", 0),
                    ("error", "", 0),
                    ("hide", "", 0),
                    ("recording", "Custom HUD", 0),
                    ("copied", "", 1500),
                ],
            )
            self.assertEqual(
                [(c["state"], c.get("text")) for c in zh_cmds],
                [
                    ("recording", "● 录音中… (按右 Ctrl 完成)"),
                    ("recognizing", "◐ 语音识别…"),
                    ("refining", "◈ 整理精修…"),
                    ("done", "✓ 已填入"),
                    ("copied", "✓ 已复制(窗口已切换)"),
                    ("empty", "○ 未检测到语音"),
                    ("error", "✕ 识别失败"),
                    ("hide", None),
                    ("recording", "Custom HUD"),
                    ("copied", "✓ 已复制(窗口已切换)"),
                ],
            )
            self.assertEqual(
                [c.get("duration_ms") for c in zh_cmds],
                [None, None, None, 900, 2000, 2200, 2600, None, None, 1500],
            )
            self.assertEqual(
                [c.get("position") for c in zh_cmds],
                ["bottom", "bottom", "bottom", "bottom", "bottom", "bottom", "bottom", None, "bottom", "bottom"],
            )

            # 2. English Windows desktop (ready ui_culture='en-US') + distracting Linux LANG=zh_CN.UTF-8 + PTT RAlt
            en_cmds = _run_hud_scenario(
                cfg=Config(hotkey=HotkeyConfig(key="RAlt", trigger_mode="ptt")),
                env={"LANG": "zh_CN.UTF-8", "LC_ALL": "zh_CN.UTF-8"},
                ready_ui_culture="en-US",
                calls=[
                    ("recording", "", 0),
                    ("recognizing", "", 0),
                    ("refining", "", 0),
                    ("done", "", 0),
                    ("copied", "", 0),
                    ("empty", "", 0),
                    ("error", "", 0),
                ],
            )
            self.assertEqual(
                [(c["state"], c.get("text")) for c in en_cmds],
                [
                    ("recording", "● Listening… (release Right Alt to finish)"),
                    ("recognizing", "◐ Transcribing…"),
                    ("refining", "◈ Refining…"),
                    ("done", "✓ Inserted"),
                    ("copied", "✓ Copied (window switched)"),
                    ("empty", "○ No speech detected"),
                    ("error", "✕ Transcription failed"),
                ],
            )
            self.assertEqual(
                [c.get("duration_ms") for c in en_cmds],
                [None, None, None, 900, 2000, 2200, 2600],
            )

            # 3. Omitted ui_culture in ready event still passes empty string (never falls back to WSL Linux LANG=zh_CN.UTF-8)
            omit_cmds = _run_hud_scenario(
                cfg=Config(),
                env={"LANG": "zh_CN.UTF-8", "LC_ALL": "zh_CN.UTF-8"},
                ready_ui_culture="__OMIT__",
                calls=[("recording", "", 0)],
            )
            self.assertEqual(omit_cmds[0].get("text"), "● Listening… (press Right Ctrl to finish)")

            # 4. Explicit config / env overrides ready ui_culture in real WslPlatform.hud RPC
            override_en_cmds = _run_hud_scenario(
                cfg=Config(ui=UiConfig(language="en")),
                env={"LANG": "zh_CN.UTF-8"},
                ready_ui_culture="zh-CN",
                calls=[("recording", "", 0)],
            )
            self.assertEqual(override_en_cmds[0].get("text"), "● Listening… (press Right Ctrl to finish)")

            override_zh_cmds = _run_hud_scenario(
                cfg=Config(ui=UiConfig(position="top")),
                env={"LANG": "C.UTF-8", "AGENT_VOICE_INPUT_UI_LANGUAGE": "zh-TW"},
                ready_ui_culture="en-US",
                calls=[("recording", "", 0), ("hide", "", 0)],
            )
            self.assertEqual(override_zh_cmds[0].get("text"), "● 录音中… (按右 Ctrl 完成)")
            self.assertEqual(override_zh_cmds[0].get("position"), "top")
            self.assertIsNone(override_zh_cmds[1].get("position"))

        # 5. probe_wsl_host resolves ui_lang from probe JSON ui_culture without launching real PowerShell
        def _fake_isfile(p: str | os.PathLike[str]) -> bool:
            return str(p) == POWERSHELL_EXE

        def _fake_run(cmd: list[str], **kwargs):
            if cmd[0] == POWERSHELL_EXE:
                out = json.dumps(
                    {
                        "ok": True,
                        "ps_version": "5.1.22621.1",
                        "wave_in_devs": 1,
                        "hwnd": 42,
                        "process": "WindowsTerminal",
                        "title": "codex",
                        "ui_culture": "zh-CN",
                    }
                )
                return subprocess.CompletedProcess(cmd, 0, stdout=out + "\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="RDPSource\n", stderr="")

        with (
            mock.patch("agent_voice_input.platform_wsl.os.path.isfile", side_effect=_fake_isfile),
            mock.patch("agent_voice_input.platform_wsl.to_windows_path", return_value=r"C:\mock\win_shim.ps1"),
            mock.patch("agent_voice_input.platform_wsl.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
            mock.patch("agent_voice_input.platform_wsl.subprocess.run", side_effect=_fake_run),
        ):
            info = probe_wsl_host(Config(), env={"LANG": "C.UTF-8", "LC_ALL": "en_US.UTF-8"})
        self.assertTrue(info["powershell_ok"])
        self.assertEqual(info["ui_culture"], "zh-CN")
        self.assertEqual(info["ui_lang"], "zh")

    def test_macos_platform_hud_bilingual_rpc_and_posix_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            cache_dir = tmp_dir / "cache"

            def _run_mac_hud_scenario(
                *,
                cfg: Config,
                env: dict[str, str],
                ready_ui_culture: str,
                calls: list[tuple[str, str, int]],
            ) -> list[dict]:
                log_path = tmp_dir / f"mac-rpc-{ready_ui_culture}-{len(list(tmp_dir.iterdir()))}.jsonl"
                plat = MacOSPlatform(
                    cfg,
                    env={"HOME": str(tmp_dir), **env},
                    cache_dir=cache_dir,
                    shim_cmd_factory=lambda: [
                        sys.executable,
                        "-u",
                        "-c",
                        _MOCK_HUD_SHIM,
                        ready_ui_culture,
                        str(log_path),
                    ],
                )
                try:
                    plat.start_event_listener(lambda _e: None)
                    for state, custom_text, dur_ms in calls:
                        plat.hud(state, text=custom_text, duration_ms=dur_ms)
                finally:
                    plat.close()
                if not log_path.exists():
                    return []
                return [
                    json.loads(ln)
                    for ln in log_path.read_text(encoding="utf-8").splitlines()
                    if ln.strip()
                ]

            # 1. Chinese macOS desktop (ui_culture='zh-Hans-CN') with ROption on POSIX LANG=C.UTF-8
            mac_zh = _run_mac_hud_scenario(
                cfg=Config(hotkey=HotkeyConfig(key="ROption", trigger_mode="tap_toggle")),
                env={"LANG": "C.UTF-8"},
                ready_ui_culture="zh-Hans-CN",
                calls=[
                    ("recording", "", 0),
                    ("recognizing", "", 0),
                    ("refining", "", 0),
                    ("done", "", 0),
                    ("copied", "", 0),
                    ("empty", "", 0),
                    ("error", "", 0),
                    ("error", "", 1500),
                    ("hide", "", 0),
                ],
            )
            self.assertEqual(
                [(c["state"], c.get("text")) for c in mac_zh],
                [
                    ("recording", "● 录音中… (按右 Option 完成)"),
                    ("recognizing", "◐ 语音识别…"),
                    ("refining", "◈ 整理精修…"),
                    ("done", "✓ 已填入"),
                    ("copied", "✓ 已复制(窗口已切换)"),
                    ("empty", "○ 未检测到语音"),
                    ("error", "✕ 识别失败"),
                    ("error", "✕ 识别失败"),
                    ("hide", None),
                ],
            )
            self.assertEqual(
                [c.get("duration_ms") for c in mac_zh],
                [None, None, None, 900, 2000, 2200, 2600, 1500, None],
            )
            self.assertEqual(
                [c.get("position") for c in mac_zh],
                ["bottom", "bottom", "bottom", "bottom", "bottom", "bottom", "bottom", "bottom", None],
            )

            # 2. English macOS desktop (ui_culture='en-US') with RCommand PTT despite LANG=zh_CN.UTF-8
            mac_en = _run_mac_hud_scenario(
                cfg=Config(hotkey=HotkeyConfig(key="RCommand", trigger_mode="ptt")),
                env={"LANG": "zh_CN.UTF-8"},
                ready_ui_culture="en-US",
                calls=[("recording", "", 0), ("refining", "", 0), ("copied", "", 0)],
            )
            self.assertEqual(
                [(c["state"], c.get("text")) for c in mac_en],
                [
                    ("recording", "● Listening… (release Right Cmd to finish)"),
                    ("refining", "◈ Refining…"),
                    ("copied", "✓ Copied (window switched)"),
                ],
            )
            self.assertEqual([c.get("duration_ms") for c in mac_en], [None, None, 2000])

            # 3. Omitted ui_culture on MacOSPlatform falls back to POSIX LANG (win_ui_culture=None)
            mac_posix_zh = _run_mac_hud_scenario(
                cfg=Config(hotkey=HotkeyConfig(key="Fn", trigger_mode="tap_toggle")),
                env={"LANG": "zh_CN.UTF-8"},
                ready_ui_culture="__OMIT__",
                calls=[("recording", "", 0)],
            )
            self.assertEqual(mac_posix_zh[0].get("text"), "● 录音中… (按 Fn 完成)")

            # 4. Explicit config override wins over mac_shim ui_culture and forwards position='top'
            mac_override = _run_mac_hud_scenario(
                cfg=Config(hotkey=HotkeyConfig(key="ROption"), ui=UiConfig(language="en", position="top")),
                env={"LANG": "zh_CN.UTF-8"},
                ready_ui_culture="zh-Hans-CN",
                calls=[("recording", "", 0), ("hide", "", 0)],
            )
            self.assertEqual(mac_override[0].get("text"), "● Listening… (press Right Option to finish)")
            self.assertEqual(mac_override[0].get("position"), "top")
            self.assertIsNone(mac_override[1].get("position"))

            # 5. probe_macos_host parses self-check ui_culture and resolves ui_lang
            fake_bin = tmp_dir / "fake_mac_shim"
            fake_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_bin.chmod(0o700)

            def _fake_mac_run(cmd: list[str], **kwargs):
                if cmd[-1] == "--self-check":
                    return subprocess.CompletedProcess(
                        cmd,
                        0,
                        stdout=json.dumps({"ok": True, "mode": "self-check", "ui_culture": "zh-Hans-CN"}) + "\n",
                        stderr="",
                    )
                return subprocess.CompletedProcess(cmd, 0, stdout="AVFoundation audio devices:\n[0] Mic\n", stderr="")

            with (
                mock.patch("agent_voice_input.platform_macos.resolve_ffmpeg_bin", return_value="/opt/homebrew/bin/ffmpeg"),
                mock.patch("agent_voice_input.platform_macos.resolve_swiftc_bin", return_value="/usr/bin/swiftc"),
                mock.patch("agent_voice_input.platform_macos.compile_mac_shim", return_value=(fake_bin, "")),
                mock.patch("agent_voice_input.platform_macos.subprocess.run", side_effect=_fake_mac_run),
                mock.patch.object(MacOSPlatform, "get_foreground_window", return_value=ForegroundWindow(0, "Ghostty", "zsh")),
            ):
                minfo = probe_macos_host(Config(), env={"HOME": str(tmp_dir), "PATH": "/usr/bin", "LANG": "C.UTF-8"}, cache_dir=cache_dir)
            self.assertTrue(minfo["mac_shim_ok"])
            self.assertEqual(minfo["ui_culture"], "zh-Hans-CN")
            self.assertEqual(minfo["ui_lang"], "zh")

    def test_bilingual_hud_copy_hotkey_labels_and_trigger_modes(self) -> None:
        expected_labels = [
            ("RControl", "右 Ctrl", "Right Ctrl"),
            ("rctrl", "右 Ctrl", "Right Ctrl"),
            ("rightcontrol", "右 Ctrl", "Right Ctrl"),
            ("LControl", "左 Ctrl", "Left Ctrl"),
            ("RAlt", "右 Alt", "Right Alt"),
            ("rmenu", "右 Alt", "Right Alt"),
            ("LAlt", "左 Alt", "Left Alt"),
            ("lmenu", "左 Alt", "Left Alt"),
            ("ROption", "右 Option", "Right Option"),
            ("rightoption", "右 Option", "Right Option"),
            ("LOption", "左 Option", "Left Option"),
            ("leftoption", "左 Option", "Left Option"),
            ("Option", "Option", "Option"),
            ("RCommand", "右 Cmd", "Right Cmd"),
            ("rcmd", "右 Cmd", "Right Cmd"),
            ("rightcommand", "右 Cmd", "Right Cmd"),
            ("LCommand", "左 Cmd", "Left Cmd"),
            ("lcmd", "左 Cmd", "Left Cmd"),
            ("Command", "Cmd", "Cmd"),
            ("cmd", "Cmd", "Cmd"),
            ("Fn", "Fn", "Fn"),
            ("function", "Fn", "Fn"),
            ("Globe", "Globe", "Globe"),
            ("RShift", "右 Shift", "Right Shift"),
            ("LShift", "左 Shift", "Left Shift"),
            ("CapsLock", "CapsLock", "CapsLock"),
            ("f8", "F8", "F8"),
            ("0xa3", "0xA3", "0xA3"),
        ]
        for raw, zh_lbl, en_lbl in expected_labels:
            self.assertEqual(format_hotkey_label(raw, "zh"), zh_lbl)
            self.assertEqual(format_hotkey_label(raw, "en"), en_lbl)

        for st in ("", "idle", "hide"):
            self.assertEqual(format_hud_text(st, "zh"), "")
            self.assertEqual(format_hud_text(st, "en"), "")

        self.assertEqual(
            format_hud_text("recording", "zh", hotkey="RControl", trigger_mode="tap_toggle"),
            "● 录音中… (按右 Ctrl 完成)",
        )
        self.assertEqual(
            format_hud_text("recording", "zh", hotkey="RControl", trigger_mode="ptt"),
            "● 录音中… (松开右 Ctrl 完成)",
        )
        self.assertEqual(
            format_hud_text("recording", "zh", hotkey="ROption", trigger_mode="tap_toggle"),
            "● 录音中… (按右 Option 完成)",
        )
        self.assertEqual(
            format_hud_text("recording", "zh", hotkey="RCommand", trigger_mode="ptt"),
            "● 录音中… (松开右 Cmd 完成)",
        )
        self.assertEqual(
            format_hud_text("recording", "zh", hotkey="CapsLock", trigger_mode="tap_toggle"),
            "● 录音中… (按 CapsLock 完成)",
        )
        self.assertEqual(
            format_hud_text("recording", "zh", hotkey="F8", trigger_mode="ptt"),
            "● 录音中… (松开 F8 完成)",
        )
        self.assertEqual(
            format_hud_text("recording", "en", hotkey="RControl", trigger_mode="tap_toggle"),
            "● Listening… (press Right Ctrl to finish)",
        )
        self.assertEqual(
            format_hud_text("recording", "en", hotkey="RControl", trigger_mode="ptt"),
            "● Listening… (release Right Ctrl to finish)",
        )
        self.assertEqual(
            format_hud_text("recording", "en", hotkey="ROption", trigger_mode="tap_toggle"),
            "● Listening… (press Right Option to finish)",
        )
        self.assertEqual(
            format_hud_text("recording", "en", hotkey="RCommand", trigger_mode="ptt"),
            "● Listening… (release Right Cmd to finish)",
        )
        self.assertEqual(
            format_hud_text("recording", "en", hotkey="RAlt", trigger_mode="tap_toggle"),
            "● Listening… (press Right Alt to finish)",
        )
        self.assertEqual(
            format_hud_text("recording", "en", hotkey="CapsLock", trigger_mode="ptt"),
            "● Listening… (release CapsLock to finish)",
        )

        pairs = [
            ("recognizing", "◐ 语音识别…", "◐ Transcribing…"),
            ("processing", "◐ 语音识别…", "◐ Transcribing…"),
            ("refining", "◈ 整理精修…", "◈ Refining…"),
            ("done", "✓ 已填入", "✓ Inserted"),
            ("copied", "✓ 已复制(窗口已切换)", "✓ Copied (window switched)"),
            ("empty", "○ 未检测到语音", "○ No speech detected"),
            ("error", "✕ 识别失败", "✕ Transcription failed"),
        ]
        for state, zh_txt, en_txt in pairs:
            self.assertEqual(format_hud_text(state, "zh"), zh_txt)
            self.assertEqual(format_hud_text(state, "en"), en_txt)

    def test_win_shim_ascii_ui_culture_and_layout_contract(self) -> None:
        import re

        shim = (_REPO_ROOT / "src" / "agent_voice_input" / "win_shim.ps1").read_text(encoding="utf-8")
        self.assertTrue(shim.isascii(), "win_shim.ps1 must stay pure 7-bit ASCII using C# \\uXXXX escapes")
        self.assertNotIn("$UiLanguage", shim)
        self.assertIn("GetHostUiCulture()", shim)
        self.assertIn("CultureInfo.CurrentUICulture", shim)
        self.assertIn("CultureInfo.InstalledUICulture", shim)
        self.assertGreaterEqual(shim.count("ui_culture = [VoiceWinShim]::GetHostUiCulture()"), 2)
        self.assertIn("[VoiceWinShim]::EnableDpiAwareness()", shim)
        self.assertIn("SetProcessDpiAwarenessContext", shim)
        self.assertIn("GetDpiForMonitor", shim)
        self.assertIn("GetTargetDpi", shim)
        self.assertIn('"Segoe UI"', shim)
        self.assertIn('"Microsoft YaHei UI"', shim)
        self.assertIn("_ownsFont", shim)
        self.assertIn("GraphicsUnit.Pixel", shim)
        self.assertIn("FontStyle.Regular", shim)
        self.assertIn("TextFormatFlags.SingleLine | TextFormatFlags.NoPrefix", shim)
        self.assertIn("TextFormatFlags.EndEllipsis", shim)
        self.assertIn("SplitHudSegments", shim)
        # Per-Monitor DPI V2 layered anti-aliased capsule + compact 4pt bottom positioning
        self.assertIn("using System.Drawing.Drawing2D;", shim)
        self.assertIn("using System.Drawing.Imaging;", shim)
        self.assertIn("UpdateLayeredWindow(", shim)
        self.assertIn("PixelFormat.Format32bppPArgb", shim)
        self.assertIn("0x00080000", shim)
        self.assertNotIn("CreateRoundRectRgn(", shim)
        self.assertIn("wa.Width - ScalePx(24)", shim)
        self.assertIn("wa.Bottom - h - ScalePx(12)", shim)
        self.assertIn("wa.Top + ScalePx(36)", shim)
        self.assertIn("ScalePx(34)", shim)
        self.assertIn("ScalePx(14)", shim)
        self.assertIn("ScalePx(15)", shim)
        self.assertIn("ScalePx(11)", shim)
        self.assertIn("ScalePx(9)", shim)
        self.assertIn("ScalePx(6)", shim)
        self.assertIn("textDy", shim)
        self.assertIn("StripOuterParens(", shim)
        self.assertIn("DrawStatusIcon(", shim)
        self.assertIn("RecordingCountdownWarnSec(", shim)
        self.assertIn("FormatCountdownHint(", shim)
        self.assertIn("SmoothingMode.AntiAlias", shim)
        self.assertIn("PixelOffsetMode.HighQuality", shim)
        self.assertIn("new GraphicsPath()", shim)
        self.assertIn("path.AddArc(", shim)
        self.assertNotIn("DrawRectangle(", shim)
        self.assertIn("[string]$cmdObj.position", shim)
        for dur_decl in ("ms = 900;", "ms = 2000;", "ms = 2200;", "ms = 2600;"):
            self.assertIn(dur_decl, shim)

        # Single source of truth parity between platform_wsl.WINDOWS_TERMINALS and win_shim.ps1 $AllowedTerminals
        m = re.search(r"\$AllowedTerminals\s*=\s*@\(([^)]+)\)", shim)
        self.assertIsNotNone(m)
        assert m is not None
        shim_terms = {item.strip().strip('"').strip("'") for item in m.group(1).split(",") if item.strip()}
        self.assertEqual(shim_terms, set(WINDOWS_TERMINALS))

        # Dynamic waveform & recording duration contract across win_shim.ps1 and mac_shim.swift
        for sym in (
            "FormatRecordingDuration(",
            "DurationSlotTemplate(",
            "ComputePcm16RmsLevel(",
            "GetLiveAudioLevel(",
            "SetExternalAudioLevel(",
            "BuildVerticalBarPath(",
            "_animTimer",
            '"hud_level"',
        ):
            self.assertIn(sym, shim)

        mac_shim = (_REPO_ROOT / "src" / "agent_voice_input" / "mac_shim.swift").read_text(encoding="utf-8")
        for sym in (
            "func formatRecordingDuration(",
            "func durationSlotTemplate(",
            "func recordingCountdownWarnSec(",
            "func formatCountdownHint(",
            "func stripOuterParens(",
            "final class WaveBarsView: NSView",
            "final class StatusIconView: NSView",
            "monospacedDigitSystemFont",
            "updateAudioLevel(",
            'case "hud_level":',
        ):
            self.assertIn(sym, mac_shim)

    def test_countdown_warn_threshold_hint_copy_and_hud_rpc_fields(self) -> None:
        from agent_voice_input.config import AudioConfig

        self.assertEqual(recording_countdown_warn_sec(0), 0)
        self.assertEqual(recording_countdown_warn_sec(-10), 0)
        self.assertEqual(recording_countdown_warn_sec(1), 1)
        self.assertEqual(recording_countdown_warn_sec(2), 1)
        self.assertEqual(recording_countdown_warn_sec(9), 3)
        self.assertEqual(recording_countdown_warn_sec(20), 6)
        self.assertEqual(recording_countdown_warn_sec(36), 10)
        self.assertEqual(recording_countdown_warn_sec(44.9), 10)
        self.assertEqual(recording_countdown_warn_sec(45), 15)
        self.assertEqual(recording_countdown_warn_sec(600), 15)

        self.assertEqual(format_countdown_hint(12, "zh"), "(12s 后自动完成)")
        self.assertEqual(format_countdown_hint(12, "en"), "(auto-finish in 12s)")
        self.assertEqual(format_countdown_hint(-5, "zh"), "(0s 后自动完成)")
        self.assertEqual(format_countdown_hint(-5, "en"), "(auto-finish in 0s)")

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            rpc_log = tmp / "countdown-rpc.jsonl"
            shim_echo = (
                "import json, pathlib, sys\n"
                "lp = pathlib.Path(sys.argv[1])\n"
                "sys.stdout.write(json.dumps({'event': 'ready', 'ui_culture': 'zh-CN'}) + '\\n'); sys.stdout.flush()\n"
                "for ln in sys.stdin:\n"
                "    req = json.loads(ln)\n"
                "    with lp.open('a', encoding='utf-8') as f: f.write(json.dumps(req, ensure_ascii=False) + '\\n')\n"
                "    if req.get('cmd') == 'quit': break\n"
                "    if 'seq_id' in req:\n"
                "        sys.stdout.write(json.dumps({'type': 'reply', 'reply': req['cmd'], 'seq_id': req['seq_id'], 'ok': True}) + '\\n')\n"
                "        sys.stdout.flush()\n"
            )
            wplat = WslPlatform(
                Config(audio=AudioConfig(max_duration_sec=600.0)),
                env={"LANG": "zh_CN.UTF-8"},
                shim_cmd_factory=lambda: [sys.executable, "-u", "-c", shim_echo, str(rpc_log)],
            )
            try:
                wplat.start_event_listener(lambda _e: None)
                wplat.hud("recording")
                wplat.hud("recognizing")
            finally:
                wplat.close()

            wplat_unlim = WslPlatform(
                Config(audio=AudioConfig(max_duration_sec=0.0)),
                env={"LANG": "zh_CN.UTF-8"},
                shim_cmd_factory=lambda: [sys.executable, "-u", "-c", shim_echo, str(rpc_log)],
            )
            try:
                wplat_unlim.start_event_listener(lambda _e: None)
                wplat_unlim.hud("recording")
            finally:
                wplat_unlim.close()

            hud_reqs = [
                json.loads(ln)
                for ln in rpc_log.read_text(encoding="utf-8").splitlines()
                if ln.strip() and json.loads(ln).get("cmd") == "hud"
            ]
            self.assertEqual(len(hud_reqs), 3)
            self.assertEqual(hud_reqs[0]["state"], "recording")
            self.assertEqual(hud_reqs[0]["max_duration_sec"], 600)
            self.assertEqual(hud_reqs[0]["warn_remain_sec"], 15)
            # Non-recording HUD requests and unlimited max_duration_sec=0 omit countdown fields
            self.assertNotIn("max_duration_sec", hud_reqs[1])
            self.assertNotIn("warn_remain_sec", hud_reqs[1])
            self.assertNotIn("max_duration_sec", hud_reqs[2])
            self.assertNotIn("warn_remain_sec", hud_reqs[2])

    def test_format_recording_duration_and_pcm16le_rms_level_boundaries(self) -> None:
        import array
        import struct

        self.assertEqual(format_recording_duration(-3.8), "0s")
        self.assertEqual(format_recording_duration(0), "0s")
        self.assertEqual(format_recording_duration(9), "9s")
        self.assertEqual(format_recording_duration(12.9), "12s")
        self.assertEqual(format_recording_duration(59), "59s")
        self.assertEqual(format_recording_duration(59.99), "59s")
        self.assertEqual(format_recording_duration(60), "1m 00s")
        self.assertEqual(format_recording_duration(65), "1m 05s")
        self.assertEqual(format_recording_duration(605), "10m 05s")
        self.assertEqual(format_recording_duration(3661), "61m 01s")

        # Empty / single odd byte / silence / low noise floor (-52 dBFS < -48 dBFS) -> 0.0
        self.assertEqual(pcm16le_rms_level(b""), 0.0)
        self.assertEqual(pcm16le_rms_level(b"\x7f"), 0.0)
        self.assertEqual(pcm16le_rms_level(b"\x00\x00" * 320), 0.0)
        quiet_amp = int(32768 * (10.0 ** (-52.0 / 20.0)))  # ~82
        quiet_pcm = struct.pack("<" + "h" * 64, *([quiet_amp, -quiet_amp] * 32))
        self.assertEqual(pcm16le_rms_level(quiet_pcm), 0.0)

        # Mid speech (-31 dBFS -> exact midpoint 0.5 of [-48, -14] dBFS), plus trailing odd byte & memoryview/bytearray
        mid_amp = int(round(32768 * (10.0 ** (-31.0 / 20.0))))  # ~923
        mid_pcm = struct.pack("<" + "h" * 100, *([mid_amp, -mid_amp] * 50))
        lvl_bytes = pcm16le_rms_level(mid_pcm + b"\xff")
        self.assertAlmostEqual(lvl_bytes, 0.5, delta=0.02)
        self.assertEqual(pcm16le_rms_level(bytearray(mid_pcm)), lvl_bytes)
        self.assertEqual(pcm16le_rms_level(memoryview(mid_pcm)), lvl_bytes)

        # Loud speech / full-scale peak (-6 dBFS and 0 dBFS >= -14 dBFS) -> clamped to 1.0
        loud_pcm = struct.pack("<" + "h" * 64, *([16000, -16000] * 32))
        peak_pcm = struct.pack("<" + "h" * 64, *([-32768, 32767] * 32))
        self.assertEqual(pcm16le_rms_level(loud_pcm), 1.0)
        self.assertEqual(pcm16le_rms_level(peak_pcm), 1.0)

        # Ensure big-endian host branch & Python 3.13+ (no audioop) compatibility
        import ast

        be_arr = array.array("h", [mid_amp, -mid_amp] * 20)
        be_arr.byteswap()
        with mock.patch("agent_voice_input.hud.sys.byteorder", "big"), mock.patch.dict(sys.modules, {"audioop": None}):
            self.assertAlmostEqual(pcm16le_rms_level(be_arr.tobytes()), 0.5, delta=0.02)
        for py_file in (_REPO_ROOT / "src" / "agent_voice_input").glob("*.py"):
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertTrue(all(alias.name != "audioop" for alias in node.names), msg=str(py_file))
                elif isinstance(node, ast.ImportFrom):
                    self.assertNotEqual(node.module, "audioop", msg=str(py_file))

    def test_ffmpeg_recorder_live_level_and_one_way_hud_level_rpc_safety(self) -> None:
        import struct
        import threading
        import time
        from agent_voice_input.config import AudioConfig

        # 1. FfmpegWavRecorder updates current_level in real time, expires stale chunks (>0.25s), and resets on stop()
        speech_chunk = struct.pack("<" + "h" * 1024, *([2800, -2800] * 512))  # 2048 bytes (~ -21.4 dBFS -> ~0.78)
        stream_src = (
            "import os, struct, time\n"
            f"os.write(1, {speech_chunk!r})\n"
            "time.sleep(0.45)\n"
        )
        rec = FfmpegWavRecorder(cmd_factory=lambda _f, _s: [sys.executable, "-c", stream_src])
        self.assertEqual(rec.current_level, 0.0)
        with tempfile.TemporaryDirectory() as td:
            rec.start("pulse", "RDPSource", Path(td), {})
            deadline = time.monotonic() + 1.5
            live = 0.0
            while time.monotonic() < deadline:
                live = rec.current_level
                if live > 0.0:
                    break
                time.sleep(0.01)
            self.assertGreater(live, 0.65)
            self.assertLessEqual(live, 1.0)

            # Wait >0.25s with no new audio chunks -> current_level automatically decays to 0.0
            time.sleep(0.28)
            self.assertEqual(rec.current_level, 0.0)

            # Inject fresh timestamp then stop() -> immediately resets to 0.0
            rec._live_level = 0.8
            rec._live_level_ts = time.monotonic()
            self.assertGreater(rec.current_level, 0.7)
            wav = rec.stop(drain_sec=0.0)
            self.assertGreater(len(wav), 44)
            self.assertEqual(rec.current_level, 0.0)

        # 2. JsonlShimMixin.hud_level one-way JSONL + concurrent _shim_rpc interleaving + level streamer lifecycle
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            log_path = tmp_dir / "hud-level-rpc.jsonl"
            plat = MacOSPlatform(
                Config(audio=AudioConfig(tail_drain_sec=0.0)),
                env={"HOME": str(tmp_dir), "LANG": "zh_CN.UTF-8"},
                cache_dir=tmp_dir / "cache",
                shim_cmd_factory=lambda: [
                    sys.executable,
                    "-u",
                    "-c",
                    _MOCK_HUD_SHIM,
                    "zh-Hans-CN",
                    str(log_path),
                ],
            )
            plat._ff_rec = FfmpegWavRecorder(
                cmd_factory=lambda _f, _s: [
                    sys.executable,
                    "-c",
                    f"import os, time\nfor _ in range(6):\n os.write(1, {speech_chunk!r})\n time.sleep(0.05)\n",
                ]
            )
            try:
                plat.start_event_listener(lambda _e: None)
                plat.hud("recording")
                # Direct one-way hud_level clamping & concurrent RPC safety
                plat.hud_level(-0.5)
                plat.hud_level(0.654321)
                plat.hud_level(1.8)

                rpc_errors: list[Exception] = []

                def _hammer_rpc() -> None:
                    for _ in range(8):
                        try:
                            r = plat._shim_rpc({"cmd": "fg_window"}, timeout_sec=2.0)
                            if not r or not r.get("ok"):
                                rpc_errors.append(RuntimeError(f"bad reply: {r}"))
                        except Exception as exc:  # pragma: no cover
                            rpc_errors.append(exc)

                t_rpc = threading.Thread(target=_hammer_rpc)
                t_rpc.start()
                plat.start_recording(tmp_dir)
                time.sleep(0.20)
                t_rpc.join(timeout=3.0)
                self.assertEqual(rpc_errors, [])
                wav_mac, src_mac = plat.stop_recording(drain=False)
                self.assertEqual(src_mac, "avfoundation")
                self.assertGreater(len(wav_mac), 44)
                # Streamer thread must be stopped immediately upon stop_recording()
                self.assertIsNone(plat._level_thread)
                plat.hud("recognizing")
            finally:
                plat.close()

            logged = [json.loads(ln) for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            lvl_msgs = [m for m in logged if m.get("cmd") == "hud_level"]
            self.assertGreaterEqual(len(lvl_msgs), 5)
            for m in lvl_msgs:
                self.assertNotIn("seq_id", m)
                self.assertGreaterEqual(m["level"], 0.0)
                self.assertLessEqual(m["level"], 1.0)
            # First three explicit calls were clamped to 0.0, 0.654, 1.0, and streamer pushed >0.65 live speech levels
            self.assertEqual([m["level"] for m in lvl_msgs[:3]], [0.0, 0.654, 1.0])
            self.assertTrue(any(0.65 < m["level"] < 0.95 for m in lvl_msgs[3:]))

            # 3. BytesIO pipe compatibility in FfmpegWavRecorder._read_pipe & WslPlatform pulse streamer
            import io

            bio = io.BytesIO(speech_chunk)
            sink = bytearray()
            rec_bio = FfmpegWavRecorder()
            rec_bio._buf = sink
            rec_bio._proc = mock.Mock()  # type: ignore[assignment]
            rec_bio._read_pipe(bio, sink, 0)
            self.assertEqual(bytes(sink), speech_chunk)
            self.assertGreater(rec_bio.current_level, 0.65)
            rec_bio._proc = None

            wsl_log = tmp_dir / "wsl-pulse-level.jsonl"
            wplat = WslPlatform(
                Config(audio=AudioConfig(wsl_source="pulse", tail_drain_sec=0.0)),
                env={"LANG": "C.UTF-8"},
                shim_cmd_factory=lambda: [
                    sys.executable,
                    "-u",
                    "-c",
                    _MOCK_HUD_SHIM,
                    "zh-CN",
                    str(wsl_log),
                ],
            )
            wplat._ff_rec = FfmpegWavRecorder(
                cmd_factory=lambda _f, _s: [
                    sys.executable,
                    "-c",
                    f"import os, time\nfor _ in range(5):\n os.write(1, {speech_chunk!r})\n time.sleep(0.05)\n",
                ]
            )
            try:
                wplat.start_event_listener(lambda _e: None)
                wplat.hud("recording")
                wplat.start_recording(tmp_dir)
                time.sleep(0.18)
                wplat.stop_recording(drain=False)
                self.assertIsNone(wplat._level_thread)
            finally:
                wplat.close()
            wsl_msgs = [json.loads(ln) for ln in wsl_log.read_text(encoding="utf-8").splitlines() if ln.strip()]
            self.assertTrue(any(m.get("cmd") == "hud_level" and m.get("level", 0.0) > 0.65 for m in wsl_msgs))


if __name__ == "__main__":
    unittest.main()
