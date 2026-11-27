"""Unit tests for v0.8-3 global voice input scope, remote-desktop yielding, context isolation, and cross-platform paste routing."""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
import re
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

from agent_voice_input.app_scope import (  # noqa: E402
    CODING_GUI_PATTERNS,
    REMOTE_CLIENT_PATTERNS,
    classify_window_category,
    is_remote_client_window,
    match_app_patterns,
    normalize_app_token,
    resolve_windows_paste_chord,
)
from agent_voice_input.asr_providers import (  # noqa: E402
    AsrContext,
    AsrResult,
    BaseAsrProvider,
    _coerce_context,
)
from agent_voice_input.audio_proc import pack_pcm16_mono_wav  # noqa: E402
from agent_voice_input.config import (  # noqa: E402
    Config,
    ConfigError,
    HotkeyConfig,
    RefinerConfig,
    _parse_simple_toml,
    load_config,
    parse_toml,
)
from agent_voice_input.daemon import VoiceInputDaemon, run_pipeline_once  # noqa: E402
from agent_voice_input.platform_macos import (  # noqa: E402
    GHOSTTY_BUNDLE_ID,
    MacOSPlatform,
    _APPLE_SEP,
    is_macos_terminal_window,
)
from agent_voice_input.platform_wsl import (  # noqa: E402
    ForegroundWindow,
    PasteOutcome,
    WslPlatform,
    is_windows_terminal_process,
)
from agent_voice_input.refiner import BaseRefiner, RefinerResult, create_refiner  # noqa: E402
from agent_voice_input.session_ctx import SessionTracker, write_session_registry_entry  # noqa: E402


def _voiced_wav(duration_ms: int = 450, amp: int = 6000) -> bytes:
    n_samples = int(16000 * duration_ms / 1000)
    pcm = bytearray()
    for i in range(n_samples):
        val = amp if (i // 32) % 2 == 0 else -amp
        pcm.extend(int(val).to_bytes(2, "little", signed=True))
    return pack_pcm16_mono_wav(bytes(pcm), sample_rate=16000)


class _SpyAsr(BaseAsrProvider):
    provider_name = "dashscope"

    def __init__(
        self,
        text: str = "测试语音输入",
        *,
        env: dict[str, str] | None = None,
    ) -> None:
        self.text = text
        self.env = env
        self.seen_contexts: list[AsrContext] = []

    def transcribe(
        self,
        wav_bytes: bytes,
        context: AsrContext | None = None,
        *,
        is_cancelled: object = None,
        **_kw: object,
    ) -> AsrResult:
        del wav_bytes, is_cancelled
        eff_ctx = _coerce_context(context, env=self.env)
        self.seen_contexts.append(eff_ctx)
        return AsrResult(text=self.text, provider="dashscope", model="qwen3-asr-flash", latency_ms=15)


class _SpyRefiner(BaseRefiner):
    provider_name = "dashscope"

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def refine(
        self,
        asr_text: str,
        *,
        session_key: str = "",
        tracker: SessionTracker | None = None,
        fallback_context: str = "",
        is_cancelled: object = None,
        mode: str = "agent",
        **_kw: object,
    ) -> RefinerResult:
        del is_cancelled
        ctx_blocks: tuple[str, ...] = ()
        git_st = ""
        if mode != "general" and session_key and tracker is not None:
            raw_chunks, git_st = tracker.get_refiner_context(
                session_key, max_chars=200000, include_git_status=True
            )
            ctx_blocks = tuple(raw_chunks)
        ctx_chars = sum(len(b) for b in ctx_blocks)
        self.calls.append(
            {
                "asr_text": asr_text,
                "session_key": session_key,
                "fallback_context": fallback_context,
                "mode": mode,
                "ctx_blocks": ctx_blocks,
                "git_status": git_st,
            }
        )
        return RefinerResult(
            text=f"[{mode}]{asr_text}",
            provider="dashscope",
            model="qwen-turbo-latest",
            latency_ms=10,
            context_chars=ctx_chars,
            context_chunks=len(ctx_blocks),
        )


class _DummyPlatform:
    def __init__(self, fg: ForegroundWindow | None = None) -> None:
        self.fg = fg or ForegroundWindow(100, "WindowsTerminal.exe", "zsh")
        self.huds: list[str] = []
        self.beeps: list[str] = []
        self.starts = 0
        self.cancels = 0
        self.paste_outcome = PasteOutcome(True, True, "ok", "WindowsTerminal")

    def is_allowed_terminal(self, win: ForegroundWindow) -> bool:
        return is_windows_terminal_process(win.process) or is_macos_terminal_window(
            win.process, win.bundle_id
        )

    def get_foreground_window(self) -> ForegroundWindow:
        return self.fg

    def start_recording(self, runtime_dir: Path, **kw: object) -> None:
        del runtime_dir, kw
        self.starts += 1

    def stop_recording(self) -> tuple[bytes, str]:
        return _voiced_wav(), "pulse"

    def cancel_recording(self) -> None:
        self.cancels += 1

    def beep(self, kind: str) -> None:
        self.beeps.append(kind)

    def hud(self, state: str, **kw: object) -> None:
        del kw
        self.huds.append(state)

    def deliver_text(self, text: str, seq_id: int, target_hwnd: int = 0, **kw: object) -> PasteOutcome:
        del text, seq_id, target_hwnd, kw
        return self.paste_outcome

    def switch_to_fallback_audio(self) -> bool:
        return False

    def close(self) -> None:
        return None


class AppScopeAndGlobalDeliveryTests(unittest.TestCase):
    def test_hotkey_scope_migration_and_ignore_apps_validation(self) -> None:
        # 1. Defaults & constructor validation
        dflt = Config()
        self.assertEqual(dflt.hotkey.scope, "all")
        self.assertFalse(hasattr(dflt.hotkey, "only_in_terminals"))
        self.assertIs(dflt.hotkey.yield_remote_desktop, True)
        self.assertEqual(dflt.hotkey.ignore_apps, ())

        hk_coding = HotkeyConfig(scope="CODING", ignore_apps=["msrdc"])
        self.assertEqual(hk_coding.scope, "coding")
        self.assertEqual(hk_coding.ignore_apps, ("msrdc",))

        with self.assertRaises(TypeError):
            HotkeyConfig(only_in_terminals=True)  # type: ignore[call-arg]

        # dataclasses.replace / asdict round-trip
        hk_mut = HotkeyConfig(scope="coding")
        hk_replaced = dataclasses.replace(hk_mut, key="F8")
        self.assertEqual(hk_replaced.key, "F8")
        self.assertEqual(hk_replaced.scope, "coding")
        as_dict = dataclasses.asdict(Config(hotkey=hk_replaced))
        self.assertEqual(as_dict["hotkey"]["scope"], "coding")

        # 2. TOML (stdlib + Python 3.10 _parse_simple_toml) & JSON + cli_overrides precedence
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            cfg_dir = home / ".agent-voice-input"
            cfg_dir.mkdir(parents=True)
            cfg_file = cfg_dir / "config.toml"

            # Legacy only_in_terminals raises a clear migration ConfigError
            cfg_file.write_text("[hotkey]\nonly_in_terminals = true\n", encoding="utf-8")
            with self.assertRaises(ConfigError) as cm_leg:
                load_config(env={"HOME": str(home)}, home_dir=home)
            self.assertIn("only_in_terminals", str(cm_leg.exception))
            self.assertIn("hotkey.scope", str(cm_leg.exception))

            # Explicit scope + yield_remote_desktop + ignore_apps
            toml_same = (
                '[hotkey]\n'
                'scope = "coding"\n'
                'yield_remote_desktop = false\n'
                'ignore_apps = [\n  "msrdc.exe",\n  "com.foo.*",\n  "msrdc.exe",\n]\n'
            )
            self.assertEqual(parse_toml(toml_same), _parse_simple_toml(toml_same))
            cfg_file.write_text(toml_same, encoding="utf-8")
            c2, _ = load_config(env={"HOME": str(home)}, home_dir=home)
            self.assertEqual(c2.hotkey.scope, "coding")
            self.assertIs(c2.hotkey.yield_remote_desktop, False)
            self.assertEqual(c2.hotkey.ignore_apps, ("msrdc.exe", "com.foo.*"))

            # CLI scope override works; legacy CLI override hotkey.only_in_terminals fails with migration error
            c2_cli, _ = load_config(
                env={"HOME": str(home)},
                home_dir=home,
                cli_overrides={"hotkey.scope": "terminals"},
            )
            self.assertEqual(c2_cli.hotkey.scope, "terminals")

            with self.assertRaises(ConfigError) as cm_cli_leg:
                load_config(
                    env={"HOME": str(home)},
                    home_dir=home,
                    cli_overrides={"hotkey.only_in_terminals": True},
                )
            self.assertIn("hotkey.scope", str(cm_cli_leg.exception))

            # Legacy key alongside scope is still rejected
            cfg_file.write_text(
                '[hotkey]\nscope = "all"\nonly_in_terminals = false\n', encoding="utf-8"
            )
            with self.assertRaises(ConfigError):
                load_config(env={"HOME": str(home)}, home_dir=home)

            # JSON config & validation error table
            json_path = home / "custom.json"
            for bad_hk in (
                {"scope": "everywhere"},
                {"yield_remote_desktop": "maybe"},
                {"yield_remote_desktop": 123},
                {"ignore_apps": "mstsc"},
                {"ignore_apps": [123]},
                {"ignore_apps": ["*"]},
                {"ignore_apps": ["   ***   "]},
            ):
                json_path.write_text(json.dumps({"hotkey": bad_hk}), encoding="utf-8")
                with self.assertRaises(ConfigError, msg=f"expected ConfigError for {bad_hk}"):
                    load_config(config_path=json_path, env={"HOME": str(home)}, home_dir=home)

    def test_app_scope_classification_wslg_exemption_and_paste_chord_table(self) -> None:
        self.assertEqual(normalize_app_token("  WindowsTerminal.EXE "), "windowsterminal")
        self.assertEqual(normalize_app_token("Visual Studio Code.app"), "visual studio code")

        def _is_term(w: ForegroundWindow) -> bool:
            return is_windows_terminal_process(w.process) or is_macos_terminal_window(
                w.process, w.bundle_id
            )

        # 1. Three-category window classification (D3: ChatGPT / Claude Desktop -> general_gui)
        cases: list[tuple[ForegroundWindow, str]] = [
            (ForegroundWindow(1, "WindowsTerminal.exe", "codex"), "terminal"),
            (ForegroundWindow(2, "mintty.exe", "MINGW64"), "terminal"),
            (ForegroundWindow(3, "kitty.exe", "PuTTY-KiTTY"), "terminal"),
            (ForegroundWindow(4, "Ghostty", "zsh", bundle_id=GHOSTTY_BUNDLE_ID), "terminal"),
            (ForegroundWindow(5, "终端", "zsh", bundle_id="com.apple.Terminal"), "terminal"),
            (ForegroundWindow(6, "Code.exe", "daemon.py - VS Code"), "coding_gui"),
            (ForegroundWindow(7, "Code - Insiders.exe", "app.ts"), "coding_gui"),
            (ForegroundWindow(8, "Cursor.exe", "project"), "coding_gui"),
            (ForegroundWindow(9, "Antigravity.exe", "workspace"), "coding_gui"),
            (ForegroundWindow(10, "idea64.exe", "IntelliJ"), "coding_gui"),
            (ForegroundWindow(11, "Electron", "Cursor", bundle_id="com.todesktop.230313mzl4w4u92"), "coding_gui"),
            (ForegroundWindow(12, "Antigravity", "agy", bundle_id="com.google.antigravity.insiders"), "coding_gui"),
            (ForegroundWindow(13, "PyCharm", "proj", bundle_id="com.jetbrains.pycharm"), "coding_gui"),
            (ForegroundWindow(14, "Visual Studio Code", "file.py"), "coding_gui"),
            (ForegroundWindow(15, "chrome.exe", "GitHub"), "general_gui"),
            (ForegroundWindow(16, "Safari", "Docs", bundle_id="com.apple.Safari"), "general_gui"),
            (ForegroundWindow(17, "ChatGPT", "New chat", bundle_id="com.openai.chat"), "general_gui"),
            (ForegroundWindow(171, "ChatGPT", "New chat", bundle_id="com.openai.codex"), "general_gui"),
            (ForegroundWindow(172, "Codex", "Codex", bundle_id="com.openai.codex"), "general_gui"),
            (ForegroundWindow(173, "Codex.exe", "Refactor - Codex"), "general_gui"),
            (ForegroundWindow(174, "WindowsTerminal.exe", "[codex:550e8400-e29b-41d4-a716-446655440000]"), "terminal"),
            (ForegroundWindow(175, "Ghostty", "codex [#abcd1234]", bundle_id=GHOSTTY_BUNDLE_ID), "terminal"),
            (ForegroundWindow(18, "Claude", "Chat", bundle_id="com.anthropic.claudefordesktop"), "general_gui"),
            (ForegroundWindow(19, "WeChat", "微信", bundle_id="com.tencent.xinWeChat"), "general_gui"),
        ]
        for win, expected_cat in cases:
            self.assertEqual(classify_window_category(win, is_terminal=_is_term), expected_cat, msg=win)

        # 2. Remote desktop / VM client detection & strict WSLg msrdc / msrdcw exemption
        self.assertNotIn("msrdc", REMOTE_CLIENT_PATTERNS)
        self.assertNotIn("msrdcw", REMOTE_CLIENT_PATTERNS)
        self.assertNotIn("chatgpt", CODING_GUI_PATTERNS)
        for rwin in (
            ForegroundWindow(1, "mstsc.exe", "Remote Desktop"),
            ForegroundWindow(2, "vmconnect.exe", "Hyper-V"),
            ForegroundWindow(3, "parsecd.exe", "Parsec"),
            ForegroundWindow(4, "rustdesk.exe", "RustDesk"),
            ForegroundWindow(5, "Windows App", "Win11", bundle_id="com.microsoft.rdc.macos"),
            ForegroundWindow(6, "屏幕共享", "Mac mini", bundle_id="com.apple.screensharing"),
            ForegroundWindow(7, "Notepad", "Coherence", bundle_id="com.parallels.winapp.notepad"),
        ):
            self.assertTrue(is_remote_client_window(rwin), msg=rwin)

        for local_wslg_or_app in (
            ForegroundWindow(10, "msrdc.exe", "Google Chrome (Ubuntu)"),
            ForegroundWindow(11, "msrdc", "kitty (Ubuntu)"),
            ForegroundWindow(12, "msrdcw.exe", "Remote Desktop Feeds"),
            ForegroundWindow(13, "WindowsTerminal.exe", "WSL"),
            ForegroundWindow(14, "Code.exe", "VS Code"),
        ):
            self.assertFalse(is_remote_client_window(local_wslg_or_app), msg=local_wslg_or_app)

        # Optional user opt-in via ignore_apps = ("msrdc",) matches msrdc.exe cleanly;
        # bundle IDs ending with literal ".app" TLD (e.g. io.rancherdesktop.app) also match cleanly!
        self.assertTrue(
            match_app_patterns(ForegroundWindow(10, "msrdc.exe", "AVD"), ("msrdc",))
        )
        rancher_win = ForegroundWindow(15, "Rancher Desktop", "K8s", bundle_id="io.rancherdesktop.app")
        self.assertTrue(match_app_patterns(rancher_win, ("io.rancherdesktop.app",)))
        self.assertTrue(match_app_patterns(rancher_win, ("IO.RANCHERDESKTOP.*",)))
        self.assertFalse(match_app_patterns(rancher_win, ("org.example.app",)))

        # 3. Windows per-process paste chord routing (including KiTTY Shift+Insert & Tabby/Hyper Ctrl+Shift+V)
        chord_cases = [
            ("mintty.exe", "shift_insert"),
            ("putty", "shift_insert"),
            ("kitty.exe", "shift_insert"),
            ("emacs.exe", "shift_insert"),
            ("alacritty.exe", "ctrl_shift_v"),
            ("wezterm", "ctrl_shift_v"),
            ("wezterm-gui.exe", "ctrl_shift_v"),
            ("rio.exe", "ctrl_shift_v"),
            ("ghostty.exe", "ctrl_shift_v"),
            ("tabby.exe", "ctrl_shift_v"),
            ("hyper.exe", "ctrl_shift_v"),
            ("WindowsTerminal.exe", "ctrl_v"),
            ("OpenConsole.exe", "ctrl_v"),
            ("Code.exe", "ctrl_v"),
            ("chrome.exe", "ctrl_v"),
            ("", "ctrl_v"),
        ]
        for proc_name, exp_chord in chord_cases:
            self.assertEqual(resolve_windows_paste_chord(proc_name), exp_chord, msg=proc_name)

    def test_daemon_gate_ordering_remote_yield_and_already_stopped_race(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            now = [200.0]
            plat = _DummyPlatform()
            cfg = Config(
                hotkey=HotkeyConfig(
                    scope="coding",
                    yield_remote_desktop=True,
                    ignore_apps=("Cursor", "com.custom.ignored*"),
                ),
                refiner=RefinerConfig(provider="off"),
            )
            cfg.audio.beep = True
            daemon = VoiceInputDaemon(
                cfg,
                env={},
                runtime_dir=rt,
                platform=plat,
                asr_provider=_SpyAsr("门禁与竞态验证"),
                refiner=create_refiner(cfg),
                tracker=SessionTracker(home_dir=rt, env={}, runtime_dir=rt, proc_root=rt / "noproc"),
                clock_fn=lambda: now[0],
            )
            try:
                # 1. Gate precedence: ignore_apps > yield_remote_desktop > scope ("coding" blocks general_gui as "out_of_scope")
                self.assertEqual(
                    daemon.handle_trigger(
                        "toggle", ForegroundWindow(1, "Cursor.exe", "proj"), from_hotkey=True
                    )["ignored"],
                    "ignored_app",
                )
                self.assertEqual(
                    daemon.handle_trigger(
                        "ptt_start",
                        ForegroundWindow(2, "App", "t", bundle_id="com.custom.ignored.helper"),
                        from_hotkey=True,
                    )["ignored"],
                    "ignored_app",
                )
                self.assertEqual(
                    daemon.handle_trigger(
                        "toggle", ForegroundWindow(3, "mstsc.exe", "RDP"), from_hotkey=True
                    )["ignored"],
                    "remote_client",
                )
                self.assertEqual(
                    daemon.handle_trigger(
                        "toggle", ForegroundWindow(4, "chrome.exe", "Web"), from_hotkey=True
                    )["ignored"],
                    "out_of_scope",
                )
                # Zero side effects & _last_trigger_ts untouched
                self.assertEqual(plat.starts, 0)
                self.assertEqual(plat.huds, [])
                self.assertEqual(plat.beeps, [])
                self.assertEqual(daemon._last_trigger_ts, -1e9)

                # WSLg msrdc.exe in scope="all" is NOT treated as remote_client!
                daemon.cfg.hotkey.scope = "all"
                wslg_win = ForegroundWindow(5, "msrdc.exe", "Linux GUI App (Ubuntu)")
                r_wslg = daemon.handle_trigger("toggle", wslg_win, from_hotkey=True)
                self.assertTrue(r_wslg["ok"])
                self.assertEqual(r_wslg["state"], "recording")
                now[0] += 0.3
                # Cancel while focused on remote desktop window works cleanly
                r_cancel = daemon.handle_trigger(
                    "ptt_cancel", ForegroundWindow(3, "mstsc.exe", "RDP"), from_hotkey=True
                )
                self.assertTrue(r_cancel["ok"])
                self.assertEqual(r_cancel["state"], "idle")

                # Coding GUI (VS Code) starts in scope="coding"
                daemon.cfg.hotkey.scope = "coding"
                now[0] += 0.5
                vscode_win = ForegroundWindow(6, "Code.exe", "daemon.py - Visual Studio Code")
                r_code = daemon.handle_trigger("toggle", vscode_win, from_hotkey=True)
                self.assertTrue(r_code["ok"])
                self.assertEqual(r_code["state"], "recording")
                cur_seq = r_code["seq_id"]

                # 2. D2 Race Condition: auto-stop happens at t=now; within 1.5s user taps hotkey inside
                # a remote desktop window (mstsc.exe). The already_stopped absorber MUST consume it BEFORE
                # remote_client gate so switching back to VS Code within 1.5s starts a fresh recording!
                now[0] += 1.0
                r_auto = daemon.handle_trigger("toggle", expected_seq_id=cur_seq)
                self.assertTrue(r_auto["ok"])
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and daemon.state != "idle":
                    time.sleep(0.02)
                self.assertEqual(daemon.state, "idle")
                daemon._auto_stop_seq = cur_seq
                daemon._auto_stop_ts = now[0]
                daemon._auto_stop_absorbed = False

                now[0] += 0.4  # still inside 1.5s absorption window!
                r_absorb_remote = daemon.handle_trigger(
                    "toggle", ForegroundWindow(3, "mstsc.exe", "RDP"), from_hotkey=True
                )
                self.assertEqual(r_absorb_remote["ignored"], "already_stopped")
                self.assertTrue(daemon._auto_stop_absorbed)

                now[0] += 0.3  # still inside 1.5s of auto_stop_ts, now back in local VS Code!
                r_fresh = daemon.handle_trigger("toggle", vscode_win, from_hotkey=True)
                self.assertTrue(r_fresh["ok"])
                self.assertEqual(r_fresh["state"], "recording")
                now[0] += 0.3
                daemon.handle_trigger("ptt_cancel")

                # Same D2 absorption check for _recorder_lost_seq
                daemon._recorder_lost_seq = daemon.seq_id
                daemon._recorder_lost_ts = now[0]
                daemon._recorder_lost_stop_absorbed = False
                now[0] += 0.4
                r_lost_absorb = daemon.handle_trigger(
                    "toggle", ForegroundWindow(3, "mstsc.exe", "RDP"), from_hotkey=True
                )
                self.assertEqual(r_lost_absorb["ignored"], "already_stopped")
                self.assertTrue(daemon._recorder_lost_stop_absorbed)
                now[0] += 0.3
                r_after_lost = daemon.handle_trigger("toggle", vscode_win, from_hotkey=True)
                self.assertTrue(r_after_lost["ok"])
                self.assertEqual(r_after_lost["state"], "recording")
                now[0] += 0.3
                daemon.handle_trigger("ptt_cancel")
            finally:
                daemon.close()

    def test_window_category_context_hard_isolation_and_mode_routing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            rt = tmp / "rt"
            rt.mkdir(parents=True, mode=0o700)
            hw_file = home / ".agent-voice-input" / "hotwords" / "personal.txt"
            hw_file.parent.mkdir(parents=True)
            hw_file.write_text("MyCustomBrand\n", encoding="utf-8")

            # Seed an active CLI session containing a distinctive repo identifier
            sid = "abcd1234-1111-2222-3333-444444444444"
            sdir = home / ".codex" / "sessions" / "2026" / "11" / "18"
            sdir.mkdir(parents=True)
            rollout = sdir / f"rollout-2026-{sid}.jsonl"
            rollout.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": sid, "source": "cli", "cwd": str(tmp)},
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "item_completed",
                            "item": {
                                "type": "UserMessage",
                                "content": [
                                    {"type": "text", "text": "请检查 secret_bg_cli_symbol.py 的实现"}
                                ],
                            },
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            write_session_registry_entry(
                rt, cli="codex", session_id=sid, jsonl_path=str(rollout), cwd=str(tmp)
            )
            tracker = SessionTracker(home_dir=home, env={"HOME": str(home)}, runtime_dir=rt, proc_root=tmp / "noproc")
            sync_calls = [0]
            orig_sync = tracker.sync_session

            def _counting_sync(*args: object, **kwargs: object):
                sync_calls[0] += 1
                return orig_sync(*args, **kwargs)  # type: ignore[arg-type]

            tracker.sync_session = _counting_sync  # type: ignore[method-assign]

            cfg = Config()
            cfg.refiner.provider = "dashscope"
            plat = _DummyPlatform()

            # 1. Terminal window -> runs sync_session, mode="agent", injects session + built-in AI/CLI hotwords
            env_t = {"HOME": str(home), "DASHSCOPE_API_KEY": "sk-test"}
            asr_term = _SpyAsr("检查 secret bg cli symbol", env=env_t)
            ref_term = _SpyRefiner()
            res_term = run_pipeline_once(
                _voiced_wav(),
                cfg=cfg,
                tracker=tracker,
                asr_provider=asr_term,
                refiner=ref_term,
                platform=plat,
                origin_window=ForegroundWindow(101, "WindowsTerminal.exe", "codex [#abcd1234]"),
                audio_source="winmm",
                dry_run=True,
                env=env_t,
            )
            self.assertTrue(res_term.ok)
            self.assertEqual(sync_calls[0], 1)
            self.assertIsNotNone(res_term.session)
            self.assertEqual(ref_term.calls[-1]["mode"], "agent")
            self.assertGreater(len(ref_term.calls[-1]["ctx_blocks"]), 0)  # type: ignore[arg-type]
            term_hw = tuple(asr_term.seen_contexts[-1].hotwords)
            self.assertEqual(term_hw[0], "MyCustomBrand")
            self.assertIn("secret_bg_cli_symbol.py", term_hw)
            self.assertIn("Codex", term_hw)

            # 2. Coding GUI (VS Code / Cursor / Antigravity) -> MUST skip sync_session (0 calls!),
            # yet still use mode="agent" with user hotwords + built-in AI/CLI hotwords and zero CLI session leak
            sync_calls[0] = 0
            for ide_win in (
                ForegroundWindow(202, "Code.exe", "main.py - Visual Studio Code"),
                ForegroundWindow(203, "Antigravity", "workspace", bundle_id="com.google.antigravity"),
            ):
                asr_ide = _SpyAsr("把 fast API 路由改一下", env=env_t)
                ref_ide = _SpyRefiner()
                res_ide = run_pipeline_once(
                    _voiced_wav(),
                    cfg=cfg,
                    tracker=tracker,
                    asr_provider=asr_ide,
                    refiner=ref_ide,
                    platform=plat,
                    origin_window=ide_win,
                    audio_source="winmm",
                    dry_run=True,
                    env=env_t,
                )
                self.assertTrue(res_ide.ok)
                self.assertEqual(sync_calls[0], 0, msg=f"sync_session leaked for {ide_win}")
                self.assertIsNone(res_ide.session)
                self.assertEqual(ref_ide.calls[-1]["mode"], "agent")
                self.assertEqual(ref_ide.calls[-1]["session_key"], "")
                self.assertEqual(ref_ide.calls[-1]["ctx_blocks"], ())
                ide_hw = tuple(asr_ide.seen_contexts[-1].hotwords)
                self.assertEqual(ide_hw[0], "MyCustomBrand")
                self.assertIn("Codex", ide_hw)
                self.assertNotIn("secret_bg_cli_symbol.py", ide_hw)

            # 3. General GUI (Browser / ChatGPT Desktop / WeChat) -> skips sync_session AND uses mode="general"
            # with zero CLI session leak and zero built-in coding/AI hotwords
            sync_calls[0] = 0
            for gen_win in (
                ForegroundWindow(301, "Safari", "Mail", bundle_id="com.apple.Safari"),
                ForegroundWindow(302, "ChatGPT", "New chat", bundle_id="com.openai.chat"),
                ForegroundWindow(303, "chrome.exe", "Notion"),
            ):
                asr_gen = _SpyAsr("明天下午三点开会", env=env_t)
                ref_gen = _SpyRefiner()
                res_gen = run_pipeline_once(
                    _voiced_wav(),
                    cfg=cfg,
                    tracker=tracker,
                    asr_provider=asr_gen,
                    refiner=ref_gen,
                    platform=plat,
                    origin_window=gen_win,
                    audio_source="winmm",
                    dry_run=True,
                    env=env_t,
                )
                self.assertTrue(res_gen.ok)
                self.assertEqual(sync_calls[0], 0, msg=f"sync_session leaked for {gen_win}")
                self.assertIsNone(res_gen.session)
                self.assertEqual(ref_gen.calls[-1]["mode"], "general")
                self.assertEqual(ref_gen.calls[-1]["session_key"], "")
                gen_hw = tuple(asr_gen.seen_contexts[-1].hotwords)
                self.assertEqual(gen_hw, ("MyCustomBrand",))

            # 4. retry / file audio_source with explicit GUI foreground window MUST NOT leak background CLI session;
            # headless file diagnostics (empty ForegroundWindow) or explicit --title still sync session!
            for retry_src in ("retry", "file"):
                sync_calls[0] = 0
                ref_retry_gui = _SpyRefiner()
                res_retry_gui = run_pipeline_once(
                    _voiced_wav(),
                    cfg=cfg,
                    tracker=tracker,
                    asr_provider=_SpyAsr("浏览器恢复录音", env=env_t),
                    refiner=ref_retry_gui,
                    platform=plat,
                    origin_window=ForegroundWindow(301, "Safari", "Mail", bundle_id="com.apple.Safari"),
                    audio_source=retry_src,
                    dry_run=True,
                    env=env_t,
                )
                self.assertTrue(res_retry_gui.ok)
                self.assertEqual(sync_calls[0], 0, msg=f"sync_session leaked on {retry_src} for Safari")
                self.assertEqual(ref_retry_gui.calls[-1]["mode"], "general")

            sync_calls[0] = 0
            res_headless_file = run_pipeline_once(
                _voiced_wav(),
                cfg=cfg,
                tracker=tracker,
                asr_provider=_SpyAsr("无窗口离线诊断", env=env_t),
                refiner=_SpyRefiner(),
                platform=None,
                origin_window=ForegroundWindow(0, "", ""),
                audio_source="file",
                dry_run=True,
                env=env_t,
            )
            self.assertTrue(res_headless_file.ok)
            self.assertEqual(sync_calls[0], 1)

    def test_wsl_non_terminal_delivery_paste_chords_and_wt_tab_token_conflict(self) -> None:
        from agent_voice_input.session_ctx import parse_window_title_hint

        plat = WslPlatform(Config(), env={})
        sent_rpcs: list[dict[str, object]] = []
        fg_reply: list[dict[str, object]] = [
            {"ok": True, "hwnd": 700, "process": "WindowsTerminal.exe", "title": "✳ claude [#abcd1234] — Running tests"}
        ]
        hold_steps = [0]
        on_hotkey_held: list[object] = []

        def fake_shim_rpc(
            payload: dict[str, object], timeout_sec: float = 3.0, *, allow_retry: bool = True
        ) -> dict[str, object]:
            del timeout_sec, allow_retry
            sent_rpcs.append(dict(payload))
            cur_proc = str(fg_reply[0].get("process") or "WindowsTerminal.exe")
            cur_title = str(fg_reply[0].get("title") or "")
            if payload.get("cmd") == "key_state":
                if hold_steps[0] > 0:
                    hold_steps[0] -= 1
                    if on_hotkey_held:
                        fn = on_hotkey_held[0]
                        if callable(fn):
                            fn()
                    return {"ok": True, "hotkey_held": True}
                return {"ok": True, "hotkey_held": False}
            if payload.get("force_clipboard_only"):
                return {
                    "ok": True,
                    "pasted": False,
                    "clipboard": True,
                    "reason": "focus_changed",
                    "fg_process": cur_proc,
                }
            exp_tok = str(payload.get("expect_session_token") or "")
            cur_hint = parse_window_title_hint(cur_title)
            if exp_tok and (cur_hint is None or cur_hint.session_token != exp_tok):
                return {
                    "ok": True,
                    "pasted": False,
                    "clipboard": True,
                    "reason": "focus_changed",
                    "fg_process": cur_proc,
                }
            return {
                "ok": True,
                "pasted": True,
                "clipboard": True,
                "reason": "ok",
                "fg_process": str(payload.get("expect_process") or "WindowsTerminal"),
            }

        plat._shim_rpc = fake_shim_rpc  # type: ignore[method-assign]

        # 1. Known non-terminal GUI & IDE origins set expect_terminal=False + expect_process + paste_chord="ctrl_v"
        for origin, exp_proc, exp_chord in (
            (ForegroundWindow(401, "Code.exe", "daemon.py - VS Code"), "code", "ctrl_v"),
            (ForegroundWindow(402, "chrome.exe", "ChatGPT"), "chrome", "ctrl_v"),
            (ForegroundWindow(403, "mintty.exe", "MINGW64"), "mintty", "shift_insert"),
            (ForegroundWindow(404, "kitty.exe", "KiTTY"), "kitty", "shift_insert"),
            (ForegroundWindow(405, "wezterm-gui.exe", "zsh"), "wezterm-gui", "ctrl_shift_v"),
            (ForegroundWindow(406, "tabby.exe", "Tabby"), "tabby", "ctrl_shift_v"),
            (ForegroundWindow(407, "hyper.exe", "Hyper"), "hyper", "ctrl_shift_v"),
        ):
            sent_rpcs.clear()
            out = plat.deliver_text("跨应用粘贴测试", seq_id=1, origin_window=origin)
            self.assertTrue(out.pasted, msg=origin)
            self.assertEqual(len(sent_rpcs), 1)
            req = sent_rpcs[0]
            self.assertIs(req["expect_terminal"], False)
            self.assertEqual(req["expect_process"], exp_proc)
            self.assertEqual(req["paste_chord"], exp_chord)
            self.assertEqual(req["target_hwnd"], origin.hwnd)

        # Unknown empty origin falls back conservatively to expect_terminal=True
        sent_rpcs.clear()
        out_unknown = plat.deliver_text("未知起点保守门禁", seq_id=2, origin_window=None, target_hwnd=0)
        self.assertTrue(out_unknown.pasted)
        self.assertIs(sent_rpcs[0]["expect_terminal"], True)

        # 2. Windows Terminal dynamic title with SAME [#id8] -> allowed via single paste RPC;
        # switching to a tab without the session token (e.g. plain shell) -> blocked & copied!
        wt_origin = ForegroundWindow(700, "WindowsTerminal.exe", "claude [#abcd1234] — Initial prompt")
        sent_rpcs.clear()
        fg_reply[0] = {"ok": True, "hwnd": 700, "process": "WindowsTerminal.exe", "title": "✳ claude [#abcd1234] — Editing 3 files"}
        out_wt_ok = plat.deliver_text("动态标题不误判", seq_id=3, origin_window=wt_origin)
        self.assertTrue(out_wt_ok.pasted)
        self.assertEqual([r["cmd"] for r in sent_rpcs], ["paste"])
        self.assertEqual(sent_rpcs[0].get("expect_session_token"), "abcd1234")

        sent_rpcs.clear()
        fg_reply[0] = {"ok": True, "hwnd": 700, "process": "WindowsTerminal.exe", "title": "Ubuntu"}
        out_wt_plain = plat.deliver_text("切普通shell拦截", seq_id=31, origin_window=wt_origin)
        self.assertFalse(out_wt_plain.pasted)
        self.assertTrue(out_wt_plain.clipboard)
        self.assertEqual(out_wt_plain.reason, "focus_changed")
        self.assertEqual([r["cmd"] for r in sent_rpcs], ["paste"])
        self.assertEqual(sent_rpcs[0].get("expect_session_token"), "abcd1234")

        # 3. Windows Terminal same hwnd switched to a DIFFERENT [#id8] tab -> blocked & copied to clipboard!
        sent_rpcs.clear()
        fg_reply[0] = {"ok": True, "hwnd": 700, "process": "WindowsTerminal.exe", "title": "codex [#99998888] — Other repo"}
        out_wt_conflict = plat.deliver_text(
            "同窗切Tab拦截",
            seq_id=4,
            origin_window=wt_origin,
            fallback_clipboard_text="前段\n\n同窗切Tab拦截",
        )
        self.assertFalse(out_wt_conflict.pasted)
        self.assertTrue(out_wt_conflict.clipboard)
        self.assertEqual(out_wt_conflict.reason, "focus_changed")
        self.assertEqual([r["cmd"] for r in sent_rpcs], ["paste"])
        self.assertEqual(sent_rpcs[0].get("expect_session_token"), "abcd1234")
        self.assertEqual(sent_rpcs[0].get("fallback_text"), "前段\n\n同窗切Tab拦截")

        # 4. P0 Guard: Switching to a DIFFERENT [#id8] tab WHILE waiting for hotkey release (key_state) is also blocked
        #    with a single paste RPC (no duplicate paste / no second force_clipboard_only RPC)!
        class _AliveShim:
            def poll(self) -> None:
                return None

        plat._shim = _AliveShim()  # type: ignore[assignment]
        plat._flush_shim_events = lambda timeout_sec=0.05: True  # type: ignore[method-assign]
        sent_rpcs.clear()
        fg_reply[0] = {"ok": True, "hwnd": 700, "process": "WindowsTerminal.exe", "title": "claude [#abcd1234] — Tab A"}
        hold_steps[0] = 1
        on_hotkey_held[:] = [
            lambda: fg_reply.__setitem__(
                0, {"ok": True, "hwnd": 700, "process": "WindowsTerminal.exe", "title": "codex [#99998888] — Tab B"}
            )
        ]
        out_wt_hold_switch = plat.deliver_text(
            "等待松键期间切Tab拦截",
            seq_id=5,
            origin_window=wt_origin,
            fallback_clipboard_text="前段\n\n等待松键期间切Tab拦截",
        )
        self.assertFalse(out_wt_hold_switch.pasted)
        self.assertTrue(out_wt_hold_switch.clipboard)
        self.assertEqual(out_wt_hold_switch.reason, "focus_changed")
        paste_only_rpcs = [r for r in sent_rpcs if r.get("cmd") == "paste"]
        self.assertEqual(len(paste_only_rpcs), 1)
        self.assertEqual([r.get("cmd") for r in sent_rpcs], ["key_state", "key_state", "paste"])

    def test_wsl_deliver_text_flush_barrier_and_tap_toggle_hold_timeout(self) -> None:
        """P1-C7 + P2-bridge-misc (b): WSL consumes _flush_shim_events (including waited_for_key=False) and caps tap_toggle timeout."""
        from agent_voice_input.http_transport import CancelToken

        # 1. _effective_paste_hold_timeout_sec aligns with macOS: tap_toggle capped at max(5.0, hold_cancel+2.0)
        cfg_tap = Config()
        cfg_tap.hotkey.trigger_mode = "tap_toggle"
        cfg_tap.audio.max_duration_sec = 600
        plat_tap = WslPlatform(cfg_tap, env={})
        self.assertEqual(plat_tap._effective_paste_hold_timeout_sec(), 5.0)
        plat_tap._paste_hold_timeout_sec = 0.25
        self.assertEqual(plat_tap._effective_paste_hold_timeout_sec(), 0.25)

        cfg_tap_long = Config()
        cfg_tap_long.hotkey.trigger_mode = "tap_toggle"
        cfg_tap_long.hotkey.hold_cancel_ms = 4000
        self.assertEqual(WslPlatform(cfg_tap_long, env={})._effective_paste_hold_timeout_sec(), 6.0)

        cfg_ptt = Config()
        cfg_ptt.hotkey.trigger_mode = "ptt"
        cfg_ptt.audio.max_duration_sec = 600
        self.assertEqual(WslPlatform(cfg_ptt, env={})._effective_paste_hold_timeout_sec(), 602.0)

        class _AliveShim:
            def poll(self) -> None:
                return None

        # 2. Barrier timeout (waited_for_key=False, _flush_shim_events -> False) MUST NOT commit normal paste; degrades to clipboard
        plat_to = WslPlatform(cfg_tap, env={})
        plat_to._shim = _AliveShim()  # type: ignore[assignment]
        plat_to._paste_hold_timeout_sec = 0.05
        rpcs_to: list[dict[str, object]] = []
        flush_calls_to = [0]

        def _fake_flush_false(timeout_sec: float = 0.05) -> bool:
            del timeout_sec
            flush_calls_to[0] += 1
            return False

        def _fake_rpc_to(payload: dict[str, object], timeout_sec: float = 5.0) -> dict[str, object]:
            del timeout_sec
            rpcs_to.append(dict(payload))
            if payload.get("cmd") == "key_state":
                return {"ok": True, "hotkey_held": False}
            if payload.get("force_clipboard_only"):
                return {"ok": True, "pasted": False, "clipboard": True, "reason": "focus_changed", "fg_process": "Code.exe"}
            return {"ok": True, "pasted": True, "clipboard": True, "reason": "ok", "fg_process": "Code.exe"}

        plat_to._flush_shim_events = _fake_flush_false  # type: ignore[method-assign]
        plat_to._dispatch_paste_rpc = _fake_rpc_to  # type: ignore[method-assign]
        tok_to = CancelToken()
        out_to = plat_to.deliver_text(
            "屏障超时不提交粘贴",
            seq_id=101,
            origin_window=ForegroundWindow(401, "Code.exe", "main.py - VS Code"),
            fallback_clipboard_text="安全兜底文本",
            is_cancelled=tok_to.is_cancelled,
            before_commit=tok_to.try_commit,
        )
        self.assertGreaterEqual(flush_calls_to[0], 1, "must invoke _flush_shim_events even when waited_for_key=False")
        self.assertFalse(out_to.pasted)
        self.assertTrue(out_to.clipboard)
        self.assertEqual(out_to.reason, "hotkey_held")
        paste_rpcs_to = [r for r in rpcs_to if r.get("cmd") == "paste"]
        self.assertEqual(len(paste_rpcs_to), 1)
        self.assertIs(paste_rpcs_to[0].get("force_clipboard_only"), True)
        self.assertEqual(paste_rpcs_to[0].get("fallback_text"), "安全兜底文本")

        # 3. Real _event_worker blocked past 50ms slice with hold_cancel behind it (both waited_for_key=False and True):
        #    deliver_text retries flush until hold_cancel drains, then cancels with ZERO paste RPCs and uncommitted token!
        for case_idx, ks_values in enumerate(([False], [True, False])):
            plat_fl = WslPlatform(cfg_tap, env={})
            plat_fl._shim = _AliveShim()  # type: ignore[assignment]
            plat_fl._paste_hold_timeout_sec = 2.0
            blocker = threading.Event()
            blocker_entered = threading.Event()
            tok_fl = CancelToken()
            flush_returns: list[bool] = []
            paste_rpcs_fl: list[dict[str, object]] = []

            def _on_evt(msg: dict[str, object], _blk=blocker, _ent=blocker_entered, _tok=tok_fl) -> None:
                if msg.get("event") == "slow_prev":
                    _ent.set()
                    _blk.wait(timeout=2.0)
                elif msg.get("event") == "hold_cancel":
                    _tok.cancel("hold_cancel")

            plat_fl._on_event = _on_evt
            plat_fl._ensure_event_worker()
            orig_flush = plat_fl._flush_shim_events

            def _spy_flush(timeout_sec: float = 0.05, _orig=orig_flush, _rec=flush_returns, _blk=blocker) -> bool:
                ok = _orig(timeout_sec=timeout_sec)
                _rec.append(ok)
                if not ok:
                    _blk.set()
                return ok

            plat_fl._flush_shim_events = _spy_flush  # type: ignore[method-assign]
            ks_iter = iter(ks_values)

            def _rpc_fl(payload: dict[str, object], timeout_sec: float = 5.0, _it=ks_iter, _rec=paste_rpcs_fl) -> dict[str, object]:
                del timeout_sec
                if payload.get("cmd") == "key_state":
                    return {"ok": True, "hotkey_held": next(_it, False)}
                _rec.append(dict(payload))
                return {"ok": True, "pasted": True, "clipboard": True, "reason": "ok", "fg_process": "Code.exe"}

            plat_fl._dispatch_paste_rpc = _rpc_fl  # type: ignore[method-assign]
            try:
                plat_fl._event_q.put({"event": "slow_prev"})
                plat_fl._event_q.put({"event": "hold_cancel"})
                self.assertTrue(blocker_entered.wait(timeout=1.0))
                try:
                    out_fl = plat_fl.deliver_text(
                        f"排空前禁止提交_{case_idx}",
                        seq_id=200 + case_idx,
                        origin_window=ForegroundWindow(401, "Code.exe", "main.py - VS Code"),
                        is_cancelled=tok_fl.is_cancelled,
                        before_commit=tok_fl.try_commit,
                    )
                finally:
                    blocker.set()
                self.assertFalse(out_fl.pasted)
                self.assertFalse(out_fl.clipboard)
                self.assertEqual(out_fl.reason, "cancelled")
                self.assertTrue(tok_fl.is_cancelled())
                self.assertFalse(tok_fl.is_committed())
                self.assertEqual(paste_rpcs_fl, [])
                self.assertIn(False, flush_returns)
            finally:
                plat_fl._shim = None
                plat_fl.close()

    def test_macos_gui_delivery_tmux_env_isolation_dynamic_title_and_shim_cmd_v(self) -> None:
        pbcopy_writes: list[str] = []
        tmux_calls: list[list[str]] = []
        osa_cmd_v_calls = [0]
        beeps: list[str] = []
        cur_fg = [
            ForegroundWindow(
                hwnd=42,
                process="ChatGPT",
                title="Python Dictation Streaming Title...",
                bundle_id="com.openai.chat",
                pid=920,
                ax_trusted=True,
            )
        ]

        def fake_run(
            cmd: list[str],
            *,
            stdin: object = None,
            input: object = None,
            capture_output: bool = False,
            text: bool = False,
            env: object = None,
            timeout: object = None,
            check: bool = False,
            **kw: object,
        ) -> subprocess.CompletedProcess[object]:
            del stdin, capture_output, env, timeout, check
            exe = Path(cmd[0]).name
            empty: object = "" if (text or kw.get("encoding")) else b""
            if exe == "pbcopy":
                raw = input.decode("utf-8") if isinstance(input, (bytes, bytearray)) else str(input or "")
                pbcopy_writes.append(raw)
                return subprocess.CompletedProcess(cmd, 0, empty, empty)
            if exe == "tmux":
                tmux_calls.append(list(cmd))
                return subprocess.CompletedProcess(cmd, 0, empty, empty)
            if exe == "osascript":
                script = cmd[2] if len(cmd) > 2 else ""
                if "on run argv" in script:
                    return subprocess.CompletedProcess(cmd, 0, "ok\n", "")
                if 'keystroke "v"' in script:
                    osa_cmd_v_calls[0] += 1
                    return subprocess.CompletedProcess(cmd, 0, "", "")
                w = cur_fg[0]
                return subprocess.CompletedProcess(
                    cmd, 0, _APPLE_SEP.join([w.process, w.bundle_id, str(w.pid), w.title]) + "\n", ""
                )
            if exe in ("ps", "lsof", "pgrep", "git"):
                return subprocess.CompletedProcess(cmd, 1, empty, empty)
            raise AssertionError(f"unexpected subprocess cmd: {cmd}")

        with tempfile.TemporaryDirectory() as td, mock.patch(
            "agent_voice_input.platform_macos.shutil.which", return_value="/usr/bin/mock"
        ), mock.patch("agent_voice_input.platform_macos.subprocess.run", side_effect=fake_run):
            # Daemon started from inside a tmux session ($TMUX is set in env!)
            plat = MacOSPlatform(
                Config(),
                env={
                    "HOME": td,
                    "TMUX": "/tmp/tmux-501/default,123,0",
                    "AGENT_VOICE_INPUT_SWIFTC": "none",
                },
                cache_dir=Path(td) / "cache",
            )
            plat.beep = lambda kind: beeps.append(kind)  # type: ignore[method-assign]
            plat._has_managed_shim = lambda: True  # type: ignore[method-assign]

            paste_rpc_behavior: list[object] = [
                {"ok": True, "posted": True, "reason": "ok"}
            ]
            paste_rpc_calls: list[tuple[dict[str, object], bool]] = []

            def fake_mac_shim_rpc(
                payload: dict[str, object], timeout_sec: float = 0.8, *, allow_retry: bool = True
            ) -> dict[str, object]:
                del timeout_sec
                op = payload.get("cmd")
                if op == "fg_window":
                    w = cur_fg[0]
                    return {
                        "ok": True,
                        "hwnd": w.hwnd,
                        "process": w.process,
                        "bundle_id": w.bundle_id,
                        "pid": w.pid,
                        "title": w.title,
                        "ax_trusted": True,
                    }
                if op == "key_state":
                    return {"ok": True, "hotkey_held": False}
                if op == "paste_cmd_v":
                    paste_rpc_calls.append((dict(payload), allow_retry))
                    beh = paste_rpc_behavior[0]
                    if beh is None:
                        return None  # type: ignore[return-value]
                    if isinstance(beh, Exception):
                        raise beh
                    return dict(beh)  # type: ignore[arg-type]
                return {"ok": True}

            plat._shim_rpc = fake_mac_shim_rpc  # type: ignore[method-assign]

            # 1. P0 Guard: Dictating in ChatGPT/Safari while daemon has $TMUX MUST NEVER invoke tmux paste-buffer!
            # Also verifies GUI dynamic title exemption when hwnd=42 (>0) & bundle_id match ("New chat" -> streaming title)
            origin_chatgpt = ForegroundWindow(
                hwnd=42,
                process="ChatGPT",
                title="New chat",
                bundle_id="com.openai.chat",
                pid=920,
            )
            out_gui = plat.deliver_text("发给桌面版ChatGPT的内容", seq_id=1, origin_window=origin_chatgpt)
            self.assertTrue(out_gui.pasted)
            self.assertTrue(out_gui.clipboard)
            self.assertTrue(out_gui.used_clipboard_paste)
            self.assertEqual(out_gui.reason, "ok")
            self.assertEqual(tmux_calls, [], "GUI delivery leaked into background $TMUX!")
            self.assertEqual(osa_cmd_v_calls[0], 0, "native shim paste_cmd_v should bypass osascript")
            self.assertEqual(len(paste_rpc_calls), 1)
            self.assertFalse(paste_rpc_calls[0][1], "paste_cmd_v RPC must set allow_retry=False")
            self.assertEqual(paste_rpc_calls[0][0].get("expect_bundle_id"), "com.openai.chat")
            self.assertEqual(paste_rpc_calls[0][0].get("expect_process"), "ChatGPT")
            self.assertEqual(paste_rpc_calls[0][0].get("expect_hwnd"), 42)
            self.assertEqual(paste_rpc_calls[0][0].get("deadline_ms"), 750)
            self.assertEqual(pbcopy_writes, ["发给桌面版ChatGPT的内容"])

            # Empty ForegroundWindow() (origin capture failure) MUST NOT paste into non-terminal GUI!
            pbcopy_writes.clear()
            paste_rpc_calls.clear()
            out_empty_origin = plat.deliver_text(
                "未知起点不盲投GUI",
                seq_id=10,
                origin_window=ForegroundWindow(),
                fallback_clipboard_text="历史\n\n未知起点不盲投GUI",
            )
            self.assertFalse(out_empty_origin.pasted)
            self.assertTrue(out_empty_origin.clipboard)
            self.assertEqual(out_empty_origin.reason, "focus_changed")
            self.assertEqual(paste_rpc_calls, [])
            self.assertEqual(pbcopy_writes, ["历史\n\n未知起点不盲投GUI"])

            # Different GUI window ID (hwnd 42 -> 43) still triggers focus_changed
            cur_fg[0] = ForegroundWindow(
                hwnd=43, process="ChatGPT", title="Another window", bundle_id="com.openai.chat", pid=920
            )
            out_diff_hwnd = plat.deliver_text("切了另一个窗口", seq_id=2, origin_window=origin_chatgpt)
            self.assertFalse(out_diff_hwnd.pasted)
            self.assertTrue(out_diff_hwnd.clipboard)
            self.assertEqual(out_diff_hwnd.reason, "focus_changed")

            # Without shim (hwnd=0), GUI title change conservatively drifts
            origin_no_hwnd = ForegroundWindow(
                hwnd=0, process="ChatGPT", title="Chat A", bundle_id="com.openai.chat", pid=920
            )
            cur_fg[0] = ForegroundWindow(
                hwnd=0, process="ChatGPT", title="Chat B", bundle_id="com.openai.chat", pid=920
            )
            self.assertTrue(plat._is_focus_drifted(origin_no_hwnd, cur_fg[0]))

            # TTY-less third-party terminal (iTerm2) [#id8] session token comparison
            iterm_orig = ForegroundWindow(100, "iTerm2", "claude [#11223344] — Step 1", "com.googlecode.iterm2")
            iterm_same_tok = ForegroundWindow(100, "iTerm2", "✳ claude [#11223344] — Step 2", "com.googlecode.iterm2")
            iterm_diff_tok = ForegroundWindow(100, "iTerm2", "claude [#55667788] — Step 1", "com.googlecode.iterm2")
            iterm_plain_zsh = ForegroundWindow(100, "iTerm2", "zsh", "com.googlecode.iterm2")
            self.assertFalse(plat._is_focus_drifted(iterm_orig, iterm_same_tok))
            self.assertTrue(plat._is_focus_drifted(iterm_orig, iterm_diff_tok))
            self.assertTrue(plat._is_focus_drifted(iterm_orig, iterm_plain_zsh))

            # 2. Accessibility denied via native shim -> returns actionable reason, restores fallback_clipboard_text, zero osascript
            cur_fg[0] = ForegroundWindow(
                hwnd=42, process="ChatGPT", title="New chat", bundle_id="com.openai.chat", pid=920
            )
            beeps.clear()
            pbcopy_writes.clear()
            paste_rpc_behavior[0] = {"ok": True, "posted": False, "reason": "accessibility_denied"}
            out_ax_denied = plat.deliver_text(
                "无辅助功能权限",
                seq_id=3,
                origin_window=origin_chatgpt,
                fallback_clipboard_text="上一段\n\n无辅助功能权限",
            )
            self.assertFalse(out_ax_denied.pasted)
            self.assertTrue(out_ax_denied.clipboard)
            self.assertFalse(out_ax_denied.used_clipboard_paste)
            self.assertIn("accessibility_denied", out_ax_denied.reason)
            self.assertEqual(osa_cmd_v_calls[0], 0)
            self.assertEqual(beeps, ["focus"])
            self.assertEqual(pbcopy_writes, ["无辅助功能权限", "上一段\n\n无辅助功能权限"])

            # 3. In-flight RPC timeout/EOF (both None from _shim_graceful AND Exception) -> NEVER falls back to osascript
            #    and NEVER performs a second pbcopy overwrite (A+B); returns clipboard=False so daemon stashes in failed/.
            for idx_case, failure_beh in enumerate((None, TimeoutError("shim paste_cmd_v timed out")), start=1):
                beeps.clear()
                pbcopy_writes.clear()
                paste_rpc_behavior[0] = failure_beh
                out_rpc_err = plat.deliver_text(
                    "超时防双贴",
                    seq_id=40 + idx_case,
                    origin_window=origin_chatgpt,
                    fallback_clipboard_text="前文\n\n超时防双贴",
                )
                self.assertFalse(out_rpc_err.pasted, msg=failure_beh)
                self.assertFalse(out_rpc_err.clipboard, msg=failure_beh)
                self.assertEqual(out_rpc_err.reason, "shim_paste_unconfirmed", msg=failure_beh)
                self.assertEqual(osa_cmd_v_calls[0], 0, msg=failure_beh)
                self.assertEqual(beeps, [], msg=failure_beh)
                self.assertEqual(pbcopy_writes, ["超时防双贴"], msg=failure_beh)

            # 3b. Focus drift caught inside mac_shim right before CGEvent post (during pbcopy race window)
            beeps.clear()
            pbcopy_writes.clear()
            paste_rpc_behavior[0] = {
                "ok": True,
                "posted": False,
                "reason": "focus_changed",
                "fg_process": "WeChat",
            }
            out_shim_drift = plat.deliver_text(
                "pbcopy期间切窗拦截",
                seq_id=49,
                origin_window=origin_chatgpt,
                fallback_clipboard_text="前文\n\npbcopy期间切窗拦截",
            )
            self.assertFalse(out_shim_drift.pasted)
            self.assertTrue(out_shim_drift.clipboard)
            self.assertEqual(out_shim_drift.reason, "focus_changed")
            self.assertEqual(out_shim_drift.fg_process, "WeChat")
            self.assertEqual(osa_cmd_v_calls[0], 0)
            self.assertEqual(beeps, ["focus"])
            self.assertEqual(pbcopy_writes, ["pbcopy期间切窗拦截", "前文\n\npbcopy期间切窗拦截"])

            # 4. No managed shim -> falls back to osascript _CMD_V_SCRIPT cleanly
            plat._has_managed_shim = lambda: False  # type: ignore[method-assign]
            pbcopy_writes.clear()
            cur_fg[0] = origin_no_hwnd
            out_osa_fb = plat.deliver_text("无shim回退osascript", seq_id=6, origin_window=origin_no_hwnd)
            self.assertTrue(out_osa_fb.pasted)
            self.assertEqual(osa_cmd_v_calls[0], 1)

            # 5. Direct Ghostty & tmux channels NEVER touch system clipboard
            pbcopy_writes.clear()
            out_ghostty = plat.deliver_text(
                "Ghostty直投零剪贴板",
                seq_id=7,
                origin_window=ForegroundWindow(0, "Ghostty", "zsh", GHOSTTY_BUNDLE_ID, surface_id="surf-9"),
            )
            self.assertTrue(out_ghostty.pasted)
            self.assertFalse(out_ghostty.clipboard)
            self.assertEqual(pbcopy_writes, [])

            tmux_calls.clear()
            iterm_tmux_fg = ForegroundWindow(0, "iTerm2", "tmux", "com.googlecode.iterm2", pid=512)
            cur_fg[0] = iterm_tmux_fg
            out_tmux_direct = plat.deliver_text(
                "tmux直投零剪贴板",
                seq_id=8,
                origin_window=iterm_tmux_fg,
            )
            self.assertTrue(out_tmux_direct.pasted)
            self.assertFalse(out_tmux_direct.clipboard)
            self.assertEqual(out_tmux_direct.reason, "tmux")
            self.assertGreaterEqual(len(tmux_calls), 2)
            self.assertEqual(pbcopy_writes, [])

    def test_general_mode_asr_context_has_no_builtin_ai_chat_tail(self) -> None:
        import agent_voice_input.app_scope as app_scope_mod

        for removed_attr in (
            "HotwordsScope",
            "AI_CHAT_PATTERNS",
            "IM_CHAT_PATTERNS",
            "BUILTIN_AI_CHAT_HOTWORDS",
            "get_builtin_ai_chat_hotwords",
            "classify_target_app_hint",
            "resolve_hotwords_scope",
        ):
            self.assertFalse(hasattr(app_scope_mod, removed_attr), msg=f"still exported: {removed_attr}")

        ctx_zh = SessionTracker.build_asr_context(
            None,
            extra_hotwords=["MyCustomBrand"],
            mode="general",
            language="zh-CN",
        )
        self.assertEqual(ctx_zh.hotwords, ["MyCustomBrand"])

        ctx_en = SessionTracker.build_asr_context(
            None,
            extra_hotwords=["MyCustomBrand"],
            mode="general",
            language="en",
        )
        self.assertEqual(ctx_en.hotwords, ["MyCustomBrand"])

    def test_no_auto_enter_static_guard(self) -> None:
        forbidden_patterns = (
            re.compile(r"\bVK_RETURN\b"),
            re.compile(r"\bauto_enter\b", re.IGNORECASE),
            re.compile(r"\bkeystroke\s+return\b", re.IGNORECASE),
            re.compile(r"\bkey\s+code\s+(?:36|76)\b", re.IGNORECASE),
            re.compile(r"send-keys[^\n]*\bEnter\b"),
        )
        for path in sorted(_SRC_ROOT.rglob("*")):
            if not path.is_file() or path.suffix not in (".py", ".ps1", ".swift"):
                continue
            src = path.read_text(encoding="utf-8")
            for pat in forbidden_patterns:
                self.assertIsNone(
                    pat.search(src),
                    msg=f"Forbidden auto-Enter pattern {pat.pattern!r} found in {path.name}",
                )


if __name__ == "__main__":
    unittest.main()
