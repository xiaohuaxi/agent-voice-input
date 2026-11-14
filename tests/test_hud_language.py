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
    load_config,
    normalize_ui_language,
)
from agent_voice_input.hud import (  # noqa: E402
    format_hotkey_label,
    format_hud_text,
    is_zh_locale,
    resolve_ui_language,
)
from agent_voice_input.platform_macos import (  # noqa: E402
    MacOSPlatform,
    probe_macos_host,
)
from agent_voice_input.platform_wsl import (  # noqa: E402
    ForegroundWindow,
    POWERSHELL_EXE,
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
    "    sys.stdout.write(json.dumps({'type': 'reply', 'reply': req['cmd'], 'seq_id': req['seq_id'], 'ok': True}) + '\\n')\n"
    "    sys.stdout.flush()\n"
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
                    ("done", "", 900),
                    ("copied", "", 0),
                    ("empty", "", 0),
                    ("error", "", 0),
                    ("hide", "", 0),
                    ("recording", "Custom HUD", 0),
                ],
            )
            self.assertEqual(
                [(c["state"], c.get("text")) for c in zh_cmds],
                [
                    ("recording", "● 正在录音… (再按右 Ctrl 完成)"),
                    ("recognizing", "◐ 正在语音识别…"),
                    ("refining", "◈ 正在整理精修…"),
                    ("done", "✓ 已填入"),
                    ("copied", "✓ 已复制(窗口已切换)"),
                    ("empty", "○ 未检测到语音"),
                    ("error", "✕ 识别失败"),
                    ("hide", None),
                    ("recording", "Custom HUD"),
                ],
            )
            self.assertEqual(zh_cmds[3].get("duration_ms"), 900)

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
                cfg=Config(),
                env={"LANG": "C.UTF-8", "AGENT_VOICE_INPUT_UI_LANGUAGE": "zh-TW"},
                ready_ui_culture="en-US",
                calls=[("recording", "", 0)],
            )
            self.assertEqual(override_zh_cmds[0].get("text"), "● 正在录音… (再按右 Ctrl 完成)")

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
                calls=[("recording", "", 0), ("recognizing", "", 0), ("done", "", 900), ("hide", "", 0)],
            )
            self.assertEqual(
                [(c["state"], c.get("text")) for c in mac_zh],
                [
                    ("recording", "● 正在录音… (再按右 Option 完成)"),
                    ("recognizing", "◐ 正在语音识别…"),
                    ("done", "✓ 已填入"),
                    ("hide", None),
                ],
            )
            self.assertEqual(mac_zh[2].get("duration_ms"), 900)

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

            # 3. Omitted ui_culture on MacOSPlatform falls back to POSIX LANG (win_ui_culture=None)
            mac_posix_zh = _run_mac_hud_scenario(
                cfg=Config(hotkey=HotkeyConfig(key="Fn", trigger_mode="tap_toggle")),
                env={"LANG": "zh_CN.UTF-8"},
                ready_ui_culture="__OMIT__",
                calls=[("recording", "", 0)],
            )
            self.assertEqual(mac_posix_zh[0].get("text"), "● 正在录音… (再按 Fn 完成)")

            # 4. Explicit config override wins over mac_shim ui_culture
            mac_override = _run_mac_hud_scenario(
                cfg=Config(hotkey=HotkeyConfig(key="ROption"), ui=UiConfig(language="en")),
                env={"LANG": "zh_CN.UTF-8"},
                ready_ui_culture="zh-Hans-CN",
                calls=[("recording", "", 0)],
            )
            self.assertEqual(mac_override[0].get("text"), "● Listening… (press Right Option to finish)")

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
            "● 正在录音… (再按右 Ctrl 完成)",
        )
        self.assertEqual(
            format_hud_text("recording", "zh", hotkey="RControl", trigger_mode="ptt"),
            "● 正在录音… (松开右 Ctrl 完成)",
        )
        self.assertEqual(
            format_hud_text("recording", "zh", hotkey="ROption", trigger_mode="tap_toggle"),
            "● 正在录音… (再按右 Option 完成)",
        )
        self.assertEqual(
            format_hud_text("recording", "zh", hotkey="RCommand", trigger_mode="ptt"),
            "● 正在录音… (松开右 Cmd 完成)",
        )
        self.assertEqual(
            format_hud_text("recording", "zh", hotkey="CapsLock", trigger_mode="tap_toggle"),
            "● 正在录音… (再按 CapsLock 完成)",
        )
        self.assertEqual(
            format_hud_text("recording", "zh", hotkey="F8", trigger_mode="ptt"),
            "● 正在录音… (松开 F8 完成)",
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
            ("recognizing", "◐ 正在语音识别…", "◐ Transcribing…"),
            ("processing", "◐ 正在语音识别…", "◐ Transcribing…"),
            ("refining", "◈ 正在整理精修…", "◈ Refining…"),
            ("done", "✓ 已填入", "✓ Inserted"),
            ("copied", "✓ 已复制(窗口已切换)", "✓ Copied (window switched)"),
            ("empty", "○ 未检测到语音", "○ No speech detected"),
            ("error", "✕ 识别失败", "✕ Transcription failed"),
        ]
        for state, zh_txt, en_txt in pairs:
            self.assertEqual(format_hud_text(state, "zh"), zh_txt)
            self.assertEqual(format_hud_text(state, "en"), en_txt)

    def test_win_shim_ascii_ui_culture_and_layout_contract(self) -> None:
        shim = (_REPO_ROOT / "src" / "agent_voice_input" / "win_shim.ps1").read_text(encoding="utf-8")
        self.assertTrue(shim.isascii(), "win_shim.ps1 must stay pure 7-bit ASCII using C# \\uXXXX escapes")
        self.assertNotIn("$UiLanguage", shim)
        self.assertIn("GetHostUiCulture()", shim)
        self.assertIn("CultureInfo.CurrentUICulture", shim)
        self.assertIn("CultureInfo.InstalledUICulture", shim)
        self.assertGreaterEqual(shim.count("ui_culture = [VoiceWinShim]::GetHostUiCulture()"), 2)
        self.assertIn('"Segoe UI"', shim)
        self.assertIn('"Microsoft YaHei UI"', shim)
        self.assertIn("_ownsFont", shim)
        self.assertIn("TextFormatFlags.SingleLine | TextFormatFlags.NoPrefix", shim)
        self.assertIn("TextFormatFlags.EndEllipsis", shim)
        self.assertIn("wa.Width - 24", shim)


if __name__ == "__main__":
    unittest.main()
