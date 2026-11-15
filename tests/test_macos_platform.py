"""Offline isolated unit tests (plus Swift --self-check compile guard) for macOS + Ghostty support."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
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
from agent_voice_input.asr_providers import AsrContext, AsrResult, BaseAsrProvider  # noqa: E402
from agent_voice_input.audio_proc import pack_pcm16_mono_wav  # noqa: E402
from agent_voice_input.config import AudioConfig, Config, ConfigError, HotkeyConfig, RefinerConfig, load_config  # noqa: E402
from agent_voice_input.daemon import VoiceInputDaemon, run_pipeline_once  # noqa: E402
from agent_voice_input.platform_macos import (  # noqa: E402
    GHOSTTY_BUNDLE_ID,
    MACOS_TERMINALS,
    MACOS_TERMINAL_BUNDLE_IDS,
    MAC_SHIM_SCRIPT,
    MacOSPlatform,
    SHIM_SCHEMA_VERSION,
    _APPLE_SEP,
    _GHOSTTY_INPUT_SCRIPT,
    check_macos_clt_ready,
    compile_mac_shim,
    get_macos_cache_dir,
    is_macos_terminal_window,
    parse_avfoundation_audio_devices,
    probe_macos_host,
    resolve_macos_audio_device,
    run_macos_key_test,
)
from agent_voice_input.platform_wsl import (  # noqa: E402
    FfmpegWavRecorder,
    ForegroundWindow,
    MicrophoneUnavailableError,
    NoMicrophoneError,
)
from agent_voice_input.refiner import create_refiner  # noqa: E402
from agent_voice_input.session_ctx import SessionTracker, write_session_registry_entry  # noqa: E402


def _voiced_wav() -> bytes:
    return pack_pcm16_mono_wav([3200, -3200] * 4000)


class _StubAsr(BaseAsrProvider):
    provider_name = "dashscope"

    def __init__(self, text: str) -> None:
        self.text = text
        self.seen_contexts: list[AsrContext | None] = []

    def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None) -> AsrResult:
        self.seen_contexts.append(context)
        return AsrResult(text=self.text, provider="dashscope", model="qwen3-asr-flash", latency_ms=10)


class MacOSPlatformAndGhosttyTests(unittest.TestCase):
    def test_macos_audio_device_config_and_env_precedence(self) -> None:
        # Default is 'default' -> ':default'
        default_cfg = Config()
        self.assertEqual(default_cfg.audio.macos_device, "default")
        self.assertEqual(resolve_macos_audio_device(default_cfg, {}), "default")

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            cfg_file = home / "config.toml"
            cfg_file.write_text('[audio]\nmacos_device = "1"\n', encoding="utf-8")
            loaded_cfg, _ = load_config(config_path=cfg_file, env={"HOME": str(home)}, home_dir=home)
            self.assertEqual(loaded_cfg.audio.macos_device, "1")
            self.assertEqual(resolve_macos_audio_device(loaded_cfg, {}), "1")

            # Leading colon is stripped cleanly; ordinary *_MACOS_AUDIO_DEVICE env vars do not override config
            self.assertEqual(
                resolve_macos_audio_device(loaded_cfg, {"AGENT_VOICE_INPUT_MACOS_AUDIO_DEVICE": "2"}),
                "1",
            )
            self.assertEqual(
                resolve_macos_audio_device(Config(audio=AudioConfig(macos_device=":MacBook Pro Mic")), {}),
                "MacBook Pro Mic",
            )

            # Empty macos_device is rejected by config validation
            bad_cfg = home / "bad.toml"
            bad_cfg.write_text('[audio]\nmacos_device = "   "\n', encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(config_path=bad_cfg, env={"HOME": str(home)}, home_dir=home)

            # Verify MacOSPlatform.start_recording passes ':default' / ':<configured>' to FfmpegWavRecorder
            seen_fmt_src: list[tuple[str, str]] = []
            ok_pcm_cmd = [
                sys.executable,
                "-c",
                "import os, time; os.write(1, b'\\x00\\x00' * 160); time.sleep(0.5)",
            ]
            plat_default = MacOSPlatform(Config(), env={"HOME": str(home)}, cache_dir=home / "cache")
            plat_default._ff_rec = FfmpegWavRecorder(
                cmd_factory=lambda fmt, src: (seen_fmt_src.append((fmt, src)) or list(ok_pcm_cmd))
            )
            plat_default.start_recording(home)
            plat_default.cancel_recording()

            plat_cfg = MacOSPlatform(
                Config(audio=AudioConfig(macos_device="3")),
                env={"HOME": str(home)},
                cache_dir=home / "cache",
            )
            plat_cfg._ff_rec = FfmpegWavRecorder(
                cmd_factory=lambda fmt, src: (seen_fmt_src.append((fmt, src)) or list(ok_pcm_cmd))
            )
            plat_cfg.start_recording(home)
            plat_cfg.cancel_recording()

            self.assertEqual(seen_fmt_src, [("avfoundation", ":default"), ("avfoundation", ":3")])

    def test_ghostty_foreground_capture_and_daemon_origin_metadata_preservation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sid = "12345678-1111-2222-3333-444444444444"
            rollout = tmp / f"rollout-{sid}.jsonl"
            rollout.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": sid, "source": "cli", "cwd": str(tmp)}})
                + "\n"
                + json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "item_completed",
                            "item": {"type": "UserMessage", "content": [{"type": "text", "text": "Ghostty 分屏上下文"}]},
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            write_session_registry_entry(tmp, cli="codex", session_id=sid, jsonl_path=str(rollout), cwd=str(tmp))

            osa_calls: list[tuple[list[str], Any, float | None]] = []

            def fake_run(cmd, *, stdin=None, input=None, capture_output=False, text=False, env=None, timeout=None, check=False, **kw):
                exe = Path(cmd[0]).name
                if exe == "pbcopy":
                    return subprocess.CompletedProcess(cmd, 0, "", "")
                if exe == "osascript":
                    osa_calls.append((list(cmd), stdin, timeout))
                    script = cmd[2] if len(cmd) > 2 and cmd[1] == "-e" else ""
                    if 'tell application "System Events"' in script:
                        out = _APPLE_SEP.join(["Ghostty", GHOSTTY_BUNDLE_ID, "901", ""])
                        return subprocess.CompletedProcess(cmd, 0, out + "\n", "")
                    if "focused terminal of selected tab of front window" in script and "on run argv" not in script:
                        out = _APPLE_SEP.join(
                            ["surface-split-A", "codex [#12345678]", "/dev/ttys016", "/Users/test/proj", "955"]
                        )
                        return subprocess.CompletedProcess(cmd, 0, out + "\n", "")
                    if "on run argv" in script:
                        target_id = cmd[4]
                        self.assertEqual(target_id, "surface-split-A")
                        return subprocess.CompletedProcess(cmd, 0, "ok\n", "")
                if exe == "git":
                    return subprocess.CompletedProcess(cmd, 1, "", "")
                raise AssertionError(f"unexpected subprocess call: {cmd}")

            with mock.patch("agent_voice_input.platform_macos.shutil.which", return_value="/usr/bin/mock"), mock.patch(
                "agent_voice_input.platform_macos.subprocess.run", side_effect=fake_run
            ):
                plat = MacOSPlatform(
                    Config(refiner=RefinerConfig(provider="off")),
                    env={"HOME": str(tmp), "AGENT_VOICE_INPUT_SWIFTC": "none"},
                    cache_dir=tmp / "cache",
                )
                # 1. Direct get_foreground_window() captures full Ghostty split metadata
                fg = plat.get_foreground_window()
                self.assertEqual(fg.process, "Ghostty")
                self.assertEqual(fg.app_name, "Ghostty")
                self.assertEqual(fg.bundle_id, GHOSTTY_BUNDLE_ID)
                self.assertEqual(fg.surface_id, "surface-split-A")
                self.assertEqual(fg.title, "codex [#12345678]")
                self.assertEqual(fg.tty, "/dev/ttys016")
                self.assertEqual(fg.cwd, "/Users/test/proj")
                self.assertEqual(fg.pid, 955)
                for _, used_stdin, used_timeout in osa_calls:
                    self.assertEqual(used_stdin, subprocess.DEVNULL)
                    self.assertIsNotNone(used_timeout)

                # 2. Daemon start triggered from mac_shim event (process+bundle_id hint without title/surface_id)
                #    enriches Ghostty split metadata WITHOUT needing System Events, matches session, and injects!
                osa_calls.clear()
                plat.start_recording = lambda _rt, *, loopback=False: None  # type: ignore[method-assign]
                plat.stop_recording = lambda *, drain=True: (_voiced_wav(), "avfoundation")  # type: ignore[method-assign]
                asr = _StubAsr("安全直投到分屏A")
                tracker = SessionTracker(home_dir=tmp, env={"HOME": str(tmp)}, runtime_dir=tmp, proc_root=tmp / "noproc")
                daemon = VoiceInputDaemon(
                    Config(refiner=RefinerConfig(provider="off")),
                    env={"HOME": str(tmp)},
                    runtime_dir=tmp,
                    platform=plat,
                    asr_provider=asr,
                    refiner=create_refiner(Config(refiner=RefinerConfig(provider="off"))),
                    tracker=tracker,
                )
                try:
                    shim_hint = ForegroundWindow(hwnd=0, process="Ghostty", title="", bundle_id=GHOSTTY_BUNDLE_ID, pid=901)
                    r_start = daemon.handle_trigger("toggle", shim_hint)
                    self.assertTrue(r_start["ok"])
                    self.assertEqual(daemon._origin_win.surface_id, "surface-split-A")
                    self.assertEqual(daemon._origin_win.tty, "/dev/ttys016")
                    self.assertEqual(daemon._origin_win.cwd, "/Users/test/proj")
                    # With shim hint, System Events was skipped completely (only 1 Ghostty query ran)!
                    self.assertEqual(len(osa_calls), 1)

                    daemon._last_trigger_ts = -1e9
                    r_stop = daemon.handle_trigger("toggle")
                    seq = r_stop["seq_id"]
                    deadline = time.monotonic() + 2.0
                    while time.monotonic() < deadline and daemon.last_result.get("seq_id") != seq:
                        time.sleep(0.02)

                    self.assertTrue(daemon.last_result["ok"])
                    self.assertTrue(daemon.last_result["pasted"])
                    self.assertEqual(daemon.last_result["paste_reason"], "ok")
                    self.assertEqual(daemon.last_result["session_key"], f"codex:{sid}")
                    self.assertIn("Ghostty 分屏上下文", asr.seen_contexts[0].dialog_text if asr.seen_contexts[0] else "")
                finally:
                    daemon.close()

    def test_ghostty_deliver_text_argv_injection_safety_and_split_focus_drift(self) -> None:
        tricky_text = '-e "hello\\nworld" \\ \' & do shell script "touch /tmp/pwn" & \'\n第二行'
        clipboard_writes: list[str] = []
        osa_invocations: list[list[str]] = []
        beeps: list[str] = []
        verdict_box = ["ok"]
        frontmost_app_box = ["Safari"]

        def fake_run(cmd, *, stdin=None, input=None, capture_output=False, text=False, env=None, timeout=None, check=False, **kw):
            exe = Path(cmd[0]).name
            if exe == "pbcopy":
                clipboard_writes.append(input or "")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if exe == "osascript":
                self.assertEqual(stdin, subprocess.DEVNULL)
                osa_invocations.append(list(cmd))
                script = cmd[2] if len(cmd) > 2 and cmd[1] == "-e" else ""
                if "on run argv" in script:
                    # CRITICAL REDLINE: Speech text MUST NEVER be interpolated into AppleScript source!
                    self.assertNotIn("do shell script", script)
                    self.assertNotIn("第二行", script)
                    self.assertEqual(cmd[3], "--")
                    self.assertEqual(cmd[4], "surf-origin-1")
                    if verdict_box[0] == "denied":
                        return subprocess.CompletedProcess(
                            cmd, 1, "", "execution error: Not authorized to send Apple events to Ghostty. (-1743)"
                        )
                    return subprocess.CompletedProcess(cmd, 0, verdict_box[0] + "\n", "")
                if 'tell application "System Events"' in script:
                    out = _APPLE_SEP.join([frontmost_app_box[0], "com.apple.Safari", "400", "Apple"])
                    return subprocess.CompletedProcess(cmd, 0, out + "\n", "")
            raise AssertionError(f"unexpected cmd: {cmd}")

        cfg = Config(audio=AudioConfig(beep=True))
        origin = ForegroundWindow(
            hwnd=0,
            process="Ghostty",
            title="claude [#aaaaaaaa]",
            bundle_id=GHOSTTY_BUNDLE_ID,
            surface_id="surf-origin-1",
            tty="/dev/ttys004",
            cwd="/tmp/work",
        )
        with tempfile.TemporaryDirectory() as td, mock.patch(
            "agent_voice_input.platform_macos.shutil.which", return_value="/usr/bin/mock"
        ), mock.patch("agent_voice_input.platform_macos.subprocess.run", side_effect=fake_run):
            plat = MacOSPlatform(cfg, env={"HOME": td}, cache_dir=Path(td) / "cache")
            plat.beep = lambda kind: beeps.append(kind)  # type: ignore[method-assign]

            # Case 1: Same Ghostty surface -> direct input text succeeds via argv
            out_ok = plat.deliver_text(tricky_text, seq_id=1, origin_window=origin)
            self.assertTrue(out_ok.pasted)
            self.assertTrue(out_ok.clipboard)
            self.assertEqual(out_ok.reason, "ok")
            self.assertEqual(out_ok.fg_process, "Ghostty")
            self.assertEqual(osa_invocations[-1][5], tricky_text)
            self.assertEqual(clipboard_writes[-1], tricky_text)
            self.assertEqual(beeps, [])

            # Case 2: User switched split or tab inside Ghostty -> focus_changed, keeps clipboard, plays focus cue
            verdict_box[0] = "focus_changed"
            out_split_drift = plat.deliver_text("切分屏后的文本", seq_id=2, origin_window=origin)
            self.assertFalse(out_split_drift.pasted)
            self.assertTrue(out_split_drift.clipboard)
            self.assertEqual(out_split_drift.reason, "focus_changed")
            self.assertEqual(out_split_drift.fg_process, "Ghostty")
            self.assertEqual(clipboard_writes[-1], "切分屏后的文本")
            self.assertEqual(beeps, ["focus"])

            # Case 3: User switched away to Safari -> focus_changed with Safari fg_process
            beeps.clear()
            verdict_box[0] = "not_frontmost"
            out_app_drift = plat.deliver_text("切到浏览器后的文本", seq_id=3, origin_window=origin)
            self.assertFalse(out_app_drift.pasted)
            self.assertTrue(out_app_drift.clipboard)
            self.assertEqual(out_app_drift.reason, "focus_changed")
            self.assertEqual(out_app_drift.fg_process, "Safari")
            self.assertEqual(beeps, ["focus"])

            # Case 4: Ghostty Automation permission denied -> clear actionable reason, no crash
            verdict_box[0] = "denied"
            out_denied = plat.deliver_text("未授权文本", seq_id=4, origin_window=origin)
            self.assertFalse(out_denied.pasted)
            self.assertTrue(out_denied.clipboard)
            self.assertIn("automation_denied", out_denied.reason)

    def test_non_ghostty_fallback_tmux_cmd_v_accessibility_denied_and_window_drift(self) -> None:
        cur_app = ["iTerm2", "com.googlecode.iterm2", "512", "⠙ claude [#abcd1234]"]
        cmd_v_rc = [0]
        cmd_v_err = [""]
        tmux_calls: list[list[str]] = []

        def fake_run(cmd, *, stdin=None, input=None, capture_output=False, text=False, env=None, timeout=None, check=False, **kw):
            exe = Path(cmd[0]).name
            if exe == "pbcopy":
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if exe == "tmux":
                tmux_calls.append(list(cmd))
                if "display-message" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, "pane-ctx\n", "")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if exe == "osascript":
                self.assertEqual(stdin, subprocess.DEVNULL)
                script = cmd[2] if len(cmd) > 2 else ""
                if 'keystroke "v"' in script:
                    return subprocess.CompletedProcess(cmd, cmd_v_rc[0], "", cmd_v_err[0])
                return subprocess.CompletedProcess(cmd, 0, _APPLE_SEP.join(cur_app) + "\n", "")
            raise AssertionError(f"unexpected cmd: {cmd}")

        with tempfile.TemporaryDirectory() as td, mock.patch(
            "agent_voice_input.platform_macos.shutil.which", return_value="/usr/bin/mock"
        ), mock.patch("agent_voice_input.platform_macos.subprocess.run", side_effect=fake_run):
            # 1. Missing Accessibility when reading AXTitle -> gracefully returns empty title without raising
            cur_app[:] = ["iTerm2", "com.googlecode.iterm2", "512", ""]
            plat = MacOSPlatform(Config(), env={"HOME": td}, cache_dir=Path(td) / "cache")
            fg_no_ax = plat.get_foreground_window()
            self.assertEqual(fg_no_ax.process, "iTerm2")
            self.assertEqual(fg_no_ax.title, "")

            # 2. Braille spinner rotation on same window is NOT treated as focus drift; Cmd+V succeeds
            origin_iterm = ForegroundWindow(0, "iTerm2", "⠋ claude [#abcd1234]", bundle_id="com.googlecode.iterm2")
            cur_app[:] = ["iTerm2", "com.googlecode.iterm2", "512", "⠸ claude [#abcd1234]"]
            out_cmdv = plat.deliver_text("终端粘贴", seq_id=1, origin_window=origin_iterm)
            self.assertTrue(out_cmdv.pasted)
            self.assertEqual(out_cmdv.reason, "ok")

            # 3. Switching to a different iTerm2 window title blocks paste with focus_changed
            cur_app[:] = ["iTerm2", "com.googlecode.iterm2", "512", "zsh — ~/other-repo"]
            out_win_drift = plat.deliver_text("误切窗口", seq_id=2, origin_window=origin_iterm)
            self.assertFalse(out_win_drift.pasted)
            self.assertEqual(out_win_drift.reason, "focus_changed")

            # 4. Accessibility permission denied on System Events Cmd+V -> clear error summary, keeps clipboard
            cur_app[:] = ["iTerm2", "com.googlecode.iterm2", "512", "⠋ claude [#abcd1234]"]
            cmd_v_rc[0] = 1
            cmd_v_err[0] = "System Events got an error: osascript is not allowed to send keystrokes. (1002)"
            out_ax_err = plat.deliver_text("缺辅助功能权限", seq_id=3, origin_window=origin_iterm)
            self.assertFalse(out_ax_err.pasted)
            self.assertTrue(out_ax_err.clipboard)
            self.assertIn("accessibility_denied", out_ax_err.reason)

            # 5. $TMUX active uses tmux load-buffer + paste-buffer before System Events Cmd+V
            plat_tmux = MacOSPlatform(Config(), env={"HOME": td, "TMUX": "/tmp/tmux-501/default,123,0"}, cache_dir=Path(td) / "cache")
            origin_tmux = plat_tmux.get_foreground_window()
            out_tmux = plat_tmux.deliver_text("tmux 粘贴文本", seq_id=4, origin_window=origin_tmux)
            self.assertTrue(out_tmux.pasted)
            self.assertEqual(out_tmux.reason, "tmux")
            self.assertTrue(any("load-buffer" in c for c in tmux_calls))
            self.assertTrue(any("paste-buffer" in c for c in tmux_calls))

    def test_mac_shim_cache_hit_atomic_replace_and_missing_swiftc_graceful_degradation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cache_dir = tmp / "isolated_cache"
            env_no_swift = {"HOME": str(tmp), "XDG_CACHE_HOME": str(tmp / "xdg"), "AGENT_VOICE_INPUT_SWIFTC": "none"}

            # 1. Cache dir honors XDG_CACHE_HOME and never touches real ~/Library/Caches
            resolved_cdir = get_macos_cache_dir(env_no_swift, create=True)
            self.assertEqual(resolved_cdir, tmp / "xdg" / "agent-voice-input")
            self.assertEqual(resolved_cdir.stat().st_mode & 0o777, 0o700)

            # 2. Missing swiftc degrades cleanly: compile returns None, listener & HUD are no-ops, key-test raises clear error
            bin_none, err_none = compile_mac_shim(env=env_no_swift, cache_dir=cache_dir)
            self.assertIsNone(bin_none)
            self.assertIn("swiftc", err_none)

            stderr_buf = io.StringIO()
            plat_deg = MacOSPlatform(Config(), env=env_no_swift, cache_dir=cache_dir)
            with mock.patch("sys.stderr", stderr_buf):
                self.assertIsNone(plat_deg.start_event_listener(lambda _e: None))
                self.assertIsNone(plat_deg.hud("recording"))
            plat_deg.close()
            self.assertIn("mac_shim unavailable", stderr_buf.getvalue())

            with self.assertRaises(RuntimeError) as kt_ctx:
                run_macos_key_test(Config(), seconds=1, env=env_no_swift, cache_dir=cache_dir)
            self.assertIn("swiftc", str(kt_ctx.exception))

            # 3. Mock swiftc verifies SHA256 cache hit, atomic temp output -> replace, and old hash cleanup
            fake_swift = tmp / "fake_shim.swift"
            fake_swift.write_text("// v1\n", encoding="utf-8")
            compile_log = tmp / "swiftc_calls.log"
            fake_swiftc = tmp / "mock_swiftc"
            fake_swiftc.write_text(
                f"#!{sys.executable}\n"
                "import os, pathlib, sys\n"
                f"log = pathlib.Path({str(compile_log)!r})\n"
                "out_idx = sys.argv.index('-o') + 1\n"
                "out_path = pathlib.Path(sys.argv[out_idx])\n"
                "with log.open('a', encoding='utf-8') as f:\n"
                "    f.write(out_path.name + '\\n')\n"
                "if 'FAIL_BUILD' in os.environ:\n"
                "    out_path.write_text('partial', encoding='utf-8')\n"
                "    sys.stderr.write('syntax error near line 1\\n')\n"
                "    sys.exit(1)\n"
                "out_path.write_text('#!/bin/sh\\necho \"{\\\"ok\\\":true}\"\\n', encoding='utf-8')\n",
                encoding="utf-8",
            )
            fake_swiftc.chmod(0o755)

            # Failed build cleans up temp file and leaves no broken binary
            b_fail, e_fail = compile_mac_shim(
                env={"HOME": str(tmp), "FAIL_BUILD": "1"},
                cache_dir=cache_dir,
                swift_source=fake_swift,
                swiftc_bin=str(fake_swiftc),
            )
            self.assertIsNone(b_fail)
            self.assertIn("syntax error", e_fail)
            self.assertEqual([p for p in cache_dir.iterdir() if p.is_file()], [])

            # Successful build outputs to hidden .tmp first, then atomically replaces final binary
            b1, e1 = compile_mac_shim(
                env={"HOME": str(tmp)},
                cache_dir=cache_dir,
                swift_source=fake_swift,
                swiftc_bin=str(fake_swiftc),
            )
            self.assertEqual(e1, "")
            assert b1 is not None
            self.assertTrue(b1.is_file())
            self.assertEqual(b1.stat().st_mode & 0o777, 0o700)
            calls_after_first = compile_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(calls_after_first), 2)
            self.assertTrue(calls_after_first[-1].startswith(".mac_shim-"))
            self.assertTrue(calls_after_first[-1].endswith(".tmp"))

            # Second call with identical source hits cache without invoking swiftc again
            b2, e2 = compile_mac_shim(
                env={"HOME": str(tmp)},
                cache_dir=cache_dir,
                swift_source=fake_swift,
                swiftc_bin=str(fake_swiftc),
            )
            self.assertEqual(b2, b1)
            self.assertEqual(e2, "")
            self.assertEqual(len(compile_log.read_text(encoding="utf-8").splitlines()), 2)

            # Modifying source compiles new SHA256 binary and prunes stale hash binary
            fake_swift.write_text("// v2 updated\n", encoding="utf-8")
            b3, _ = compile_mac_shim(
                env={"HOME": str(tmp)},
                cache_dir=cache_dir,
                swift_source=fake_swift,
                swiftc_bin=str(fake_swiftc),
            )
            assert b3 is not None
            self.assertNotEqual(b3, b1)
            self.assertTrue(b3.is_file())
            self.assertFalse(b1.exists())

            # 4. Managed bridge lifecycle: hotkey events, HUD RPC, crash self-healing, and clean shutdown
            mock_bridge = (
                "import json, sys\n"
                "gen = int(sys.argv[1])\n"
                "sys.stdout.write(json.dumps({'event': 'ready', 'gen': gen}) + '\\n'); sys.stdout.flush()\n"
                "if gen == 2:\n"
                "    sys.stdout.write(json.dumps({'event': 'toggle', 'process': 'Ghostty', 'bundle_id': 'com.mitchellh.ghostty', 'pid': 77}) + '\\n')\n"
                "    sys.stdout.write(json.dumps({'type': 'reply', 'reply': 'paste', 'seq_id': 999999, 'ok': False, 'error': 'late_timeout'}) + '\\n')\n"
                "    sys.stdout.flush()\n"
                "for ln in sys.stdin:\n"
                "    req = json.loads(ln)\n"
                "    if req.get('cmd') == 'quit': break\n"
                "    if gen == 1 and req.get('cmd') == 'hud': sys.exit(2)\n"
                "    sys.stdout.write(json.dumps({'type': 'reply', 'reply': req['cmd'], 'seq_id': req['seq_id'], 'ok': True}) + '\\n')\n"
                "    sys.stdout.flush()\n"
            )
            spawns = [0]
            received: list[dict] = []
            got_evt = threading.Event()

            def bridge_factory() -> list[str]:
                spawns[0] += 1
                return [sys.executable, "-u", "-c", mock_bridge, str(spawns[0])]

            plat_live = MacOSPlatform(Config(), env={"HOME": str(tmp)}, cache_dir=cache_dir, shim_cmd_factory=bridge_factory)
            try:
                plat_live.start_event_listener(lambda m: (received.append(m), got_evt.set()))
                self.assertEqual(spawns[0], 1)
                # First HUD crashes gen=1; _shim_rpc retries & respawns gen=2 transparently
                plat_live.hud("recording")
                self.assertGreaterEqual(spawns[0], 2)
                self.assertTrue(got_evt.wait(timeout=3.0))
                self.assertEqual(received[0]["bundle_id"], GHOSTTY_BUNDLE_ID)
                self.assertFalse(plat_live._shim_disabled)
                ping_after_late = plat_live._shim_rpc({"cmd": "ping"}, timeout_sec=2.0)
                self.assertTrue(ping_after_late.get("ok"))
                self.assertFalse(plat_live._shim_disabled)
            finally:
                plat_live.close()

            # 5. Pre-ready ok=False startup failure on graceful shim disables cleanly; Ghostty surface_id keeps t_pid (0) instead of GUI app_pid
            plat_init_fail = MacOSPlatform(
                Config(),
                env={"HOME": str(tmp)},
                cache_dir=cache_dir,
                shim_cmd_factory=lambda: [
                    sys.executable,
                    "-u",
                    "-c",
                    "import json, sys; sys.stdout.write(json.dumps({'ok': False, 'error': 'init_denied'}) + '\\n'); sys.stdout.flush()",
                ],
            )
            try:
                with mock.patch("sys.stderr", io.StringIO()):
                    plat_init_fail.start_event_listener(lambda _e: None)
                    deadline = time.monotonic() + 2.0
                    while time.monotonic() < deadline and not plat_init_fail._shim_disabled:
                        time.sleep(0.02)
                self.assertTrue(plat_init_fail._shim_disabled)
            finally:
                plat_init_fail.close()

            with mock.patch.object(
                plat_init_fail,
                "_query_frontmost_process_and_title",
                return_value=("Ghostty", GHOSTTY_BUNDLE_ID, "Ghostty", 4242),
            ), mock.patch.object(
                plat_init_fail, "_query_ghostty_terminal", return_value=("surf-split-1", "zsh", "/dev/ttys019", "/tmp", 0)
            ):
                fg_split = plat_init_fail.get_foreground_window()
            self.assertEqual(fg_split.surface_id, "surf-split-1")
            self.assertEqual(fg_split.pid, 0)

    def test_doctor_and_cli_key_test_on_macos(self) -> None:
        sample_ff = (
            "[AVFoundation indev @ 0x123] AVFoundation video devices:\n"
            "[AVFoundation indev @ 0x123] [0] FaceTime HD Camera\n"
            "[AVFoundation indev @ 0x123] AVFoundation audio devices:\n"
            "[AVFoundation indev @ 0x123] [0] MacBook Pro Microphone\n"
            "[AVFoundation indev @ 0x123] [1] AirPods Pro\n"
            "[in#0 @ 0x123] Error opening input: Input/output error\n"
        )
        self.assertEqual(
            parse_avfoundation_audio_devices(sample_ff),
            ["[0] MacBook Pro Microphone", "[1] AirPods Pro"],
        )
        self.assertEqual(
            parse_avfoundation_audio_devices("[AVFoundation indev @ 0x1] AVFoundation audio devices:\nError opening input\n"),
            [],
        )

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sid = "fedcba98-0000-1111-2222-333333333333"
            rollout = tmp / f"rollout-{sid}.jsonl"
            rollout.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": sid, "source": "cli", "cwd": str(tmp)}}) + "\n",
                encoding="utf-8",
            )
            rt_dir = tmp / "rt"
            write_session_registry_entry(rt_dir, cli="codex", session_id=sid, jsonl_path=str(rollout), cwd=str(tmp))

            fake_probe = {
                "avfoundation_ok": True,
                "audio_devices": ["[0] MacBook Pro Microphone"],
                "configured_audio_device": ":default",
                "pbcopy_ok": True,
                "tmux_active": False,
                "swiftc_ok": True,
                "swiftc_bin": "/usr/bin/swiftc",
                "mac_shim_ok": True,
                "mac_shim_bin": str(tmp / "mac_shim-arm64-deadbeef"),
                "mac_shim_error": "",
                "fg_process": "Ghostty",
                "fg_bundle_id": GHOSTTY_BUNDLE_ID,
                "fg_title": "codex [#fedcba98]",
                "fg_surface_id": "surf-doc-1",
                "fg_tty": "/dev/ttys009",
                "fg_cwd": str(tmp),
                "permissions_hint": "Microphone -> terminal; Automation -> Ghostty",
            }
            env = {
                "HOME": str(tmp),
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt_dir),
                "DASHSCOPE_API_KEY": "dummy_dashscope_key_doctor",
            }
            cfg = Config(refiner=RefinerConfig(provider="off"))
            out_buf = io.StringIO()
            with mock.patch.object(cli_mod.sys, "platform", "darwin"), mock.patch.object(
                cli_mod, "probe_macos_host", return_value=fake_probe
            ), mock.patch.object(cli_mod, "resolve_ffmpeg_bin", return_value="/opt/homebrew/bin/ffmpeg"), mock.patch(
                "sys.stdout", out_buf
            ):
                rc = cli_mod._cmd_doctor(cfg, env)
            self.assertEqual(rc, 0)
            doc_out = out_buf.getvalue()
            self.assertIn("device=:default", doc_out)
            self.assertIn("[0] MacBook Pro Microphone", doc_out)
            self.assertIn("surface=surf-doc-1", doc_out)
            self.assertIn(f"codex:{sid}", doc_out)
            self.assertIn("[macos-tcc]", doc_out)

            # Verify CLI key-test routes to run_macos_key_test on darwin (never PowerShell)
            cfg_toml = tmp / "k.toml"
            cfg_toml.write_text('[refiner]\nprovider = "off"\n', encoding="utf-8")
            with mock.patch.object(cli_mod.sys, "platform", "darwin"), mock.patch.object(
                cli_mod, "run_macos_key_test", return_value=0
            ) as mock_mkt, mock.patch.dict(os.environ, {"HOME": str(tmp)}, clear=False):
                rc_kt = cli_mod.main(["key-test", "--config", str(cfg_toml), "--seconds", "3"])
            self.assertEqual(rc_kt, 0)
            mock_mkt.assert_called_once()
            self.assertEqual(mock_mkt.call_args.kwargs["seconds"], 3)

    def test_privacy_redaction_ghostty_fallback_early_exit_and_once_title_override(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            secret_token = "sk-very-secret-dashscope-token-987654"
            sensitive_speech = "这是绝对不能落入状态快照的用户私密语音原文"
            env = {"HOME": str(tmp), "DASHSCOPE_API_KEY": secret_token, "AGENT_VOICE_INPUT_SWIFTC": "none"}

            # 1. Privacy redline: osascript stderr echoing pasteText & secret NEVER leaks into PasteOutcome.reason or daemon status
            def fake_osa_leak(cmd, *, stdin=None, input=None, capture_output=False, text=False, env=None, timeout=None, check=False, **kw):
                exe = Path(cmd[0]).name
                if exe == "pbcopy":
                    return subprocess.CompletedProcess(cmd, 0, "", "")
                if exe == "osascript":
                    err_echo = f'execution error: Ghostty failed on "{sensitive_speech}" key={secret_token} (-1708)'
                    return subprocess.CompletedProcess(cmd, 1, "", err_echo)
                if exe == "git":
                    return subprocess.CompletedProcess(cmd, 1, "", "")
                raise AssertionError(f"unexpected cmd: {cmd}")

            origin_g = ForegroundWindow(0, "Ghostty", "codex", GHOSTTY_BUNDLE_ID, surface_id="surf-priv-1")
            with mock.patch("agent_voice_input.platform_macos.shutil.which", return_value="/usr/bin/mock"), mock.patch(
                "agent_voice_input.platform_macos.subprocess.run", side_effect=fake_osa_leak
            ):
                plat_priv = MacOSPlatform(Config(refiner=RefinerConfig(provider="off")), env=env, cache_dir=tmp / "cache")
                out_priv = plat_priv.deliver_text(sensitive_speech, seq_id=1, origin_window=origin_g)
                self.assertFalse(out_priv.pasted)
                self.assertEqual(out_priv.reason, "ghostty_osascript_error(-1708)")
                self.assertNotIn(sensitive_speech, out_priv.reason)
                self.assertNotIn(secret_token, out_priv.reason)

                plat_priv.start_recording = lambda _rt, *, loopback=False: None  # type: ignore[method-assign]
                plat_priv.stop_recording = lambda *, drain=True: (_voiced_wav(), "avfoundation")  # type: ignore[method-assign]
                daemon = VoiceInputDaemon(
                    Config(refiner=RefinerConfig(provider="off")),
                    env=env,
                    runtime_dir=tmp / "rt_priv",
                    platform=plat_priv,
                    asr_provider=_StubAsr(sensitive_speech),
                    refiner=create_refiner(Config(refiner=RefinerConfig(provider="off"))),
                    tracker=SessionTracker(home_dir=tmp, env=env, runtime_dir=tmp / "rt_priv", proc_root=tmp / "noproc"),
                )
                try:
                    daemon.handle_trigger("toggle", origin_g)
                    daemon._last_trigger_ts = -1e9
                    r_stop = daemon.handle_trigger("toggle")
                    deadline = time.monotonic() + 2.0
                    while time.monotonic() < deadline and daemon.last_result.get("seq_id") != r_stop["seq_id"]:
                        time.sleep(0.02)
                    st_dump = json.dumps(daemon.handle_control({"cmd": "status"}), ensure_ascii=False)
                    self.assertNotIn(sensitive_speech, st_dump)
                    self.assertNotIn(secret_token, st_dump)
                finally:
                    daemon.close()

            # 2. Ghostty fallback when _query_ghostty_terminal returns None: fetches AXTitle on demand & NEVER blindly injects fg.surface_id
            ghostty_query_enabled = [False]
            ghostty_inject_calls: list[list[str]] = []

            def fake_ghostty_fallback(cmd, *, stdin=None, input=None, capture_output=False, text=False, env=None, timeout=None, check=False, **kw):
                exe = Path(cmd[0]).name
                if exe in ("pbcopy", "ffmpeg"):
                    return subprocess.CompletedProcess(cmd, 0, "", "")
                if exe == "osascript":
                    script = cmd[2] if len(cmd) > 2 else ""
                    if "on run argv" in script:
                        ghostty_inject_calls.append(list(cmd))
                        return subprocess.CompletedProcess(cmd, 0, "ok\n", "")
                    if 'keystroke "v"' in script:
                        return subprocess.CompletedProcess(cmd, 0, "", "")
                    if "focused terminal of selected tab of front window" in script:
                        if not ghostty_query_enabled[0]:
                            return subprocess.CompletedProcess(cmd, 1, "", "appleevents disabled (-1743)")
                        out = _APPLE_SEP.join(["surf-other-split", "claude [#11111111]", "/dev/ttys020", str(tmp), "789"])
                        return subprocess.CompletedProcess(cmd, 0, out + "\n", "")
                    if "first process whose frontmost is true" in script and "character id 31" in script:
                        return subprocess.CompletedProcess(cmd, 0, _APPLE_SEP.join(["Ghostty", GHOSTTY_BUNDLE_ID, "700", ""]) + "\n", "")
                    if 'attribute "AXTitle"' in script:
                        return subprocess.CompletedProcess(cmd, 0, "claude [#11111111]\n", "")
                raise AssertionError(f"unexpected cmd: {cmd}")

            with mock.patch("agent_voice_input.platform_macos.shutil.which", return_value="/usr/bin/mock"), mock.patch(
                "agent_voice_input.platform_macos.subprocess.run", side_effect=fake_ghostty_fallback
            ):
                plat_fb = MacOSPlatform(Config(), env=env, cache_dir=tmp / "cache")
                fg_fb = plat_fb.get_foreground_window()
                self.assertEqual(fg_fb.surface_id, "")
                self.assertEqual(fg_fb.title, "claude [#11111111]")
                self.assertEqual(fg_fb.pid, 700)

                # Even if Ghostty AppleScript later returns surf-other-split, origin without surface_id MUST NOT inject via fg.surface_id
                ghostty_query_enabled[0] = True
                out_fb = plat_fb.deliver_text("回退粘贴", seq_id=2, origin_window=fg_fb)
                self.assertEqual(ghostty_inject_calls, [])
                self.assertTrue(out_fb.pasted)
                self.assertEqual(out_fb.reason, "ok")

                # Also verify probe_macos_host includes fg_pid
                probe_info = probe_macos_host(Config(), env, cache_dir=tmp / "cache")
                self.assertEqual(probe_info["fg_pid"], 789)

            # Verify _GHOSTTY_INPUT_SCRIPT wraps focused terminal lookup in try ... on error ... return "focus_changed"
            self.assertIn("try\n", _GHOSTTY_INPUT_SCRIPT)
            self.assertIn('return "focus_changed"', _GHOSTTY_INPUT_SCRIPT)

            # 3. mac_shim ok:false / early exit returns promptly (<1s, no 5s hang), redacts secret, and does NOT respawn infinitely
            ok_false_spawns = [0]
            stderr_capture = io.StringIO()
            ok_false_script = (
                "import json, sys\n"
                f"sys.stdout.write(json.dumps({{'ok': False, 'error': 'BadKey token={secret_token}'}}) + '\\n')\n"
                "sys.stdout.flush()\n"
                "sys.exit(2)\n"
            )
            plat_bad = MacOSPlatform(
                Config(),
                env=env,
                cache_dir=tmp / "cache",
                shim_cmd_factory=lambda: (ok_false_spawns.__setitem__(0, ok_false_spawns[0] + 1) or [sys.executable, "-u", "-c", ok_false_script]),
            )
            t0 = time.monotonic()
            with mock.patch("sys.stderr", stderr_capture):
                plat_bad.start_event_listener(lambda _m: None)
                elapsed = time.monotonic() - t0
                time.sleep(0.32)
            plat_bad.close()
            self.assertLess(elapsed, 1.0, f"start_event_listener hung for {elapsed:.2f}s on ok:false")
            self.assertEqual(ok_false_spawns[0], 1, "ok:false shim must not respawn infinitely")
            self.assertTrue(plat_bad._shim_disabled)
            self.assertNotIn(secret_token, plat_bad.last_shim_error)
            self.assertNotIn(secret_token, stderr_capture.getvalue())

            early_spawns = [0]
            plat_early = MacOSPlatform(
                Config(),
                env=env,
                cache_dir=tmp / "cache",
                shim_cmd_factory=lambda: (early_spawns.__setitem__(0, early_spawns[0] + 1) or [sys.executable, "-c", "import sys; sys.exit(1)"]),
            )
            t1 = time.monotonic()
            with mock.patch("sys.stderr", io.StringIO()):
                plat_early.start_event_listener(lambda _m: None)
                elapsed_early = time.monotonic() - t1
                time.sleep(0.32)
            plat_early.close()
            self.assertLess(elapsed_early, 1.0, f"start_event_listener hung for {elapsed_early:.2f}s on early exit")
            self.assertEqual(early_spawns[0], 1)

            # 4. once --title override on non-Ghostty terminal matches session via override while checking focus against real window title
            sid = "87654321-1111-2222-3333-444444444444"
            rollout = tmp / f"rollout-{sid}.jsonl"
            rollout.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": sid, "source": "cli", "cwd": str(tmp)}}) + "\n",
                encoding="utf-8",
            )
            write_session_registry_entry(tmp / "rt_once", cli="codex", session_id=sid, jsonl_path=str(rollout), cwd=str(tmp))

            def fake_iterm_once(cmd, *, stdin=None, input=None, capture_output=False, text=False, env=None, timeout=None, check=False, **kw):
                exe = Path(cmd[0]).name
                if exe == "pbcopy":
                    return subprocess.CompletedProcess(cmd, 0, "", "")
                if exe == "osascript":
                    script = cmd[2] if len(cmd) > 2 else ""
                    if 'keystroke "v"' in script:
                        return subprocess.CompletedProcess(cmd, 0, "", "")
                    return subprocess.CompletedProcess(cmd, 0, _APPLE_SEP.join(["iTerm2", "com.googlecode.iterm2", "512", "zsh — ~/proj"]) + "\n", "")
                if exe == "git":
                    return subprocess.CompletedProcess(cmd, 1, "", "")
                raise AssertionError(f"unexpected cmd: {cmd}")

            with mock.patch("agent_voice_input.platform_macos.shutil.which", return_value="/usr/bin/mock"), mock.patch(
                "agent_voice_input.platform_macos.subprocess.run", side_effect=fake_iterm_once
            ):
                plat_once = MacOSPlatform(Config(refiner=RefinerConfig(provider="off")), env=env, cache_dir=tmp / "cache")
                real_win = plat_once.get_foreground_window()
                self.assertEqual(real_win.title, "zsh — ~/proj")
                res_once = run_pipeline_once(
                    _voiced_wav(),
                    cfg=Config(refiner=RefinerConfig(provider="off")),
                    tracker=SessionTracker(home_dir=tmp, env=env, runtime_dir=tmp / "rt_once", proc_root=tmp / "noproc"),
                    asr_provider=_StubAsr("once 标题覆盖不漂移"),
                    refiner=create_refiner(Config(refiner=RefinerConfig(provider="off"))),
                    platform=plat_once,
                    origin_window=real_win,
                    window_title_override="codex [#87654321]",
                    dry_run=False,
                    env=env,
                )
                self.assertTrue(res_once.ok)
                assert res_once.session is not None and res_once.paste is not None
                self.assertEqual(res_once.session.session_key, f"codex:{sid}")
                self.assertTrue(res_once.paste.pasted)
                self.assertEqual(res_once.paste.reason, "ok")

    def test_daemon_and_doctor_forward_ghostty_split_tty_cwd_pid_to_tracker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            rt = tmp / "rt"
            proj_a = home / "repos" / "split-alpha"
            proj_b = home / "repos" / "split-beta"
            proj_a.mkdir(parents=True)
            proj_b.mkdir(parents=True)
            rt.mkdir(parents=True)

            cx_dir = home / ".codex" / "sessions" / "2026" / "11" / "14"
            cx_dir.mkdir(parents=True)
            sid_a = "aaaa9999-1111-4222-8333-000000000001"
            sid_b = "bbbb9999-1111-4222-8333-000000000002"
            f_a = cx_dir / f"rollout-2026-11-14-{sid_a}.jsonl"
            f_b = cx_dir / f"rollout-2026-11-14-{sid_b}.jsonl"
            f_a.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": sid_a, "source": "cli", "cwd": str(proj_a)}})
                + "\n"
                + json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "item_completed",
                            "item": {"type": "UserMessage", "content": [{"type": "text", "text": "正在修改 split_alpha_router.py"}]},
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            f_b.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": sid_b, "source": "cli", "cwd": str(proj_b)}})
                + "\n"
                + json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "item_completed",
                            "item": {"type": "UserMessage", "content": [{"type": "text", "text": "隔壁分屏 split_beta_worker.py"}]},
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            now = time.time()
            os.utime(f_a, (now - 120, now - 120))
            os.utime(f_b, (now - 2, now - 2))

            env = {
                "HOME": str(home),
                "XDG_RUNTIME_DIR": str(tmp / "xdg"),
                "DASHSCOPE_API_KEY": "sk-test-dummy-key-value",
            }

            def fake_subproc(cmd, *, stdin=None, input=None, capture_output=False, text=False, env=None, timeout=None, check=False, **kw):
                exe = Path(cmd[0]).name
                if exe == "pbcopy":
                    return subprocess.CompletedProcess(cmd, 0, "", "")
                if exe == "osascript":
                    script = cmd[2] if len(cmd) > 2 else ""
                    if script == _GHOSTTY_INPUT_SCRIPT:
                        return subprocess.CompletedProcess(cmd, 0, "ok\n", "")
                    if "focused terminal" in script:
                        out = _APPLE_SEP.join(["surf-alpha", "zsh", "/dev/ttys021", str(proj_a), "4101"])
                        return subprocess.CompletedProcess(cmd, 0, out + "\n", "")
                    out = _APPLE_SEP.join(["Ghostty", GHOSTTY_BUNDLE_ID, "676", "zsh"])
                    return subprocess.CompletedProcess(cmd, 0, out + "\n", "")
                if exe == "ps" and "-t" in cmd:
                    self.assertIs(stdin, subprocess.DEVNULL)
                    t_arg = cmd[cmd.index("-t") + 1]
                    if t_arg == "ttys021":
                        return subprocess.CompletedProcess(
                            cmd, 0, b"4100 600 Ss ttys021 -zsh\n4101 4100 S+ ttys021 /Users/dev/.local/bin/codex\n", b""
                        )
                    if t_arg == "ttys022":
                        return subprocess.CompletedProcess(
                            cmd, 0, b"4200 600 Ss ttys022 -zsh\n4202 4200 S+ ttys022 /Users/dev/.local/bin/codex\n", b""
                        )
                    return subprocess.CompletedProcess(cmd, 1, b"", b"")
                if exe == "lsof" and "-p" in cmd:
                    self.assertIs(stdin, subprocess.DEVNULL)
                    pid_arg = cmd[cmd.index("-p") + 1]
                    if pid_arg == "4101":
                        return subprocess.CompletedProcess(
                            cmd, 0, f"p4101\nfcwd\nn{proj_a}\nf12\nn{f_a}\n".encode("utf-8"), b""
                        )
                    return subprocess.CompletedProcess(cmd, 1, b"", b"")
                if exe == "git":
                    return subprocess.CompletedProcess(cmd, 1, b"", b"")
                raise AssertionError(f"unexpected cmd: {cmd}")

            # 1. run_pipeline_once wires win.tty / win.cwd / win.pid into SessionTracker.sync_session
            stub_asr = _StubAsr("检查 split_alpha_router.py")
            with mock.patch("agent_voice_input.platform_macos.shutil.which", return_value="/usr/bin/mock"), mock.patch(
                "subprocess.run", side_effect=fake_subproc
            ):
                plat = MacOSPlatform(Config(refiner=RefinerConfig(provider="off")), env=env, cache_dir=tmp / "cache")
                tracker = SessionTracker(home_dir=home, env=env, runtime_dir=rt, proc_root=tmp / "noproc")
                res = run_pipeline_once(
                    _voiced_wav(),
                    cfg=Config(refiner=RefinerConfig(provider="off")),
                    tracker=tracker,
                    asr_provider=stub_asr,
                    refiner=create_refiner(Config(refiner=RefinerConfig(provider="off"))),
                    platform=plat,
                    dry_run=False,
                    env=env,
                )
            self.assertTrue(res.ok)
            assert res.session is not None and res.paste is not None
            self.assertEqual(res.session.match.session_id, sid_a)
            self.assertEqual(res.session.match.matched_by, "tty_ps")
            self.assertIn("split_alpha_router.py", res.session.hotwords)
            self.assertNotIn("split_beta_worker.py", res.session.hotwords)
            assert stub_asr.seen_contexts and stub_asr.seen_contexts[0] is not None
            self.assertIn("split_alpha_router.py", stub_asr.seen_contexts[0].dialog_text)
            self.assertTrue(res.paste.pasted)
            self.assertEqual(res.paste.reason, "ok")

            # 2. _cmd_doctor on Darwin forwards fg_tty / fg_cwd / fg_pid into SessionTracker.resolve_session
            fake_probe = {
                "avfoundation_ok": True,
                "configured_audio_device": "default",
                "audio_devices": ["[0] Built-in Mic"],
                "pbcopy_ok": True,
                "tmux_active": False,
                "swiftc_ok": True,
                "swiftc_bin": "/usr/bin/swiftc",
                "mac_shim_ok": True,
                "mac_shim_bin": "/tmp/mac_shim",
                "mac_shim_error": "",
                "fg_process": "Ghostty",
                "fg_surface_id": "surf-alpha",
                "fg_tty": "/dev/ttys021",
                "fg_cwd": str(proj_a),
                "fg_pid": 4101,
                "fg_title": "zsh",
                "permissions_hint": "Microphone=ok",
            }
            orig_st_init = cli_mod.SessionTracker
            buf = io.StringIO()
            with mock.patch.object(cli_mod.sys, "platform", "darwin"), mock.patch.object(
                cli_mod, "resolve_ffmpeg_bin", return_value="/opt/homebrew/bin/ffmpeg"
            ), mock.patch.object(
                cli_mod, "probe_macos_host", return_value=fake_probe
            ), mock.patch.object(
                cli_mod,
                "SessionTracker",
                side_effect=lambda **kw: orig_st_init(home_dir=home, proc_root=tmp / "noproc", **kw),
            ), mock.patch(
                "subprocess.run", side_effect=fake_subproc
            ), mock.patch("sys.stdout", buf):
                rc = cli_mod._cmd_doctor(Config(refiner=RefinerConfig(provider="off")), env)
            self.assertEqual(rc, 0)
            doctor_out = buf.getvalue()
            self.assertIn(f"[session]   codex:{sid_a} (tty_ps)", doctor_out)
            self.assertIn("tty=/dev/ttys021", doctor_out)

    def test_macos_terminal_whitelist_chinese_terminal_paste_and_hotkey_gate(self) -> None:
        expected_procs = {
            "terminal",
            "终端",
            "iterm2",
            "iterm",
            "wezterm",
            "wezterm-gui",
            "alacritty",
            "ghostty",
            "kitty",
            "warp",
            "hyper",
            "rio",
            "tabby",
        }
        expected_bundles = {
            GHOSTTY_BUNDLE_ID,
            "com.apple.terminal",
            "com.googlecode.iterm2",
            "com.github.wez.wezterm",
            "org.alacritty",
            "io.alacritty",
            "net.kovidgoyal.kitty",
            "dev.warp.warp-stable",
            "dev.warp.warp",
            "co.zeit.hyper",
            "com.raphaelamorim.rio",
            "org.tabby",
        }
        self.assertEqual(MACOS_TERMINALS, expected_procs)
        self.assertEqual(MACOS_TERMINAL_BUNDLE_IDS, expected_bundles)

        self.assertTrue(is_macos_terminal_window("Ghostty", GHOSTTY_BUNDLE_ID))
        self.assertTrue(is_macos_terminal_window("终端", "com.apple.Terminal"))
        self.assertTrue(is_macos_terminal_window("终端.app", ""))
        self.assertTrue(is_macos_terminal_window("LocalizedTerm", "com.apple.Terminal"))
        self.assertTrue(is_macos_terminal_window("Terminal.app", ""))
        self.assertTrue(is_macos_terminal_window("Warp.app", "dev.warp.warp-stable"))
        self.assertFalse(is_macos_terminal_window("Windows App", "com.microsoft.rdc.macos"))
        self.assertFalse(is_macos_terminal_window("Safari", "com.apple.Safari"))
        self.assertFalse(is_macos_terminal_window("", ""))

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            osa_calls: list[list[str]] = []
            hud_calls: list[str] = []
            beep_calls: list[str] = []
            rec_starts: list[bool] = []
            cancel_calls: list[int] = []
            cur_app = ["终端", "com.apple.Terminal", "601", "codex — zsh"]

            def fake_run(cmd, *, stdin=None, input=None, capture_output=False, text=False, env=None, timeout=None, check=False, **kw):
                exe = Path(cmd[0]).name
                if exe == "pbcopy":
                    return subprocess.CompletedProcess(cmd, 0, "", "")
                if exe == "osascript":
                    osa_calls.append(list(cmd))
                    script = cmd[2] if len(cmd) > 2 else ""
                    if 'keystroke "v"' in script:
                        return subprocess.CompletedProcess(cmd, 0, "", "")
                    if "focused terminal of selected tab of front window" in script and "on run argv" not in script:
                        out = _APPLE_SEP.join(["surf-g1", "codex", "/dev/ttys011", str(tmp), "701"])
                        return subprocess.CompletedProcess(cmd, 0, out + "\n", "")
                    if "on run argv" in script:
                        return subprocess.CompletedProcess(cmd, 0, "ok\n", "")
                    return subprocess.CompletedProcess(cmd, 0, _APPLE_SEP.join(cur_app) + "\n", "")
                if exe in ("git", "tmux", "ps"):
                    empty = "" if text else b""
                    return subprocess.CompletedProcess(cmd, 1, empty, empty)
                raise AssertionError(f"unexpected subprocess call: {cmd}")

            with mock.patch("agent_voice_input.platform_macos.shutil.which", return_value="/usr/bin/mock"), mock.patch(
                "agent_voice_input.platform_macos.subprocess.run", side_effect=fake_run
            ):
                plat = MacOSPlatform(
                    Config(audio=AudioConfig(beep=True), refiner=RefinerConfig(provider="off")),
                    env={"HOME": str(tmp), "AGENT_VOICE_INPUT_SWIFTC": "none"},
                    cache_dir=tmp / "cache",
                )
                plat.hud = lambda state, text="", *, duration_ms=0: hud_calls.append(state)  # type: ignore[method-assign]
                plat.beep = lambda kind: beep_calls.append(kind)  # type: ignore[method-assign]
                plat.start_recording = lambda _rt, *, loopback=False: rec_starts.append(loopback)  # type: ignore[method-assign]
                plat.stop_recording = lambda *, drain=True: (_voiced_wav(), "avfoundation")  # type: ignore[method-assign]
                plat.cancel_recording = lambda: cancel_calls.append(1)  # type: ignore[method-assign]

                # 1. Chinese macOS built-in Terminal (`终端` / `com.apple.Terminal`) is allowed and pastes via Cmd+V
                zh_term_win = ForegroundWindow(0, "终端", "codex — zsh", bundle_id="com.apple.Terminal", pid=601)
                self.assertTrue(plat.is_allowed_terminal(zh_term_win))
                paste_zh = plat.deliver_text("中文系统终端粘贴", seq_id=1, origin_window=zh_term_win)
                self.assertTrue(paste_zh.pasted)
                self.assertEqual(paste_zh.reason, "ok")

                osa_calls.clear()
                hud_calls.clear()
                beep_calls.clear()
                rec_starts.clear()

                daemon = VoiceInputDaemon(
                    Config(audio=AudioConfig(beep=True), refiner=RefinerConfig(provider="off")),
                    env={"HOME": str(tmp)},
                    runtime_dir=tmp / "rt",
                    platform=plat,
                    asr_provider=_StubAsr("终端语音输入"),
                    refiner=create_refiner(Config(refiner=RefinerConfig(provider="off"))),
                    tracker=SessionTracker(home_dir=tmp, env={"HOME": str(tmp)}, runtime_dir=tmp / "rt", proc_root=tmp / "noproc"),
                )
                try:
                    # 2. Frontmost Windows App (RDP) or Safari or empty window -> ignored="non_terminal" with zero side effects
                    for non_term in (
                        ForegroundWindow(0, "Windows App", "", bundle_id="com.microsoft.rdc.macos", pid=810),
                        ForegroundWindow(0, "Safari", "", bundle_id="com.apple.Safari", pid=811),
                        ForegroundWindow(),
                    ):
                        prev_ts = daemon._last_trigger_ts
                        res_ig = daemon.handle_trigger("toggle", non_term, from_hotkey=True)
                        self.assertEqual(
                            res_ig,
                            {"ok": False, "ignored": "non_terminal", "state": "idle", "seq_id": 0},
                        )
                        self.assertEqual(daemon.state, "idle")
                        self.assertEqual(daemon.seq_id, 0)
                        self.assertEqual(daemon._last_trigger_ts, prev_ts)
                        self.assertEqual(osa_calls, [])
                        self.assertEqual(hud_calls, [])
                        self.assertEqual(beep_calls, [])
                        self.assertEqual(rec_starts, [])

                    # 3. Immediately switching to Chinese `终端` (`com.apple.Terminal`) starts recording without debounce block
                    r_zh_start = daemon.handle_trigger(
                        "toggle",
                        ForegroundWindow(0, "终端", "", bundle_id="com.apple.Terminal", pid=601),
                        from_hotkey=True,
                    )
                    self.assertTrue(r_zh_start["ok"])
                    self.assertEqual(daemon.state, "recording")
                    self.assertEqual(rec_starts, [False])
                    self.assertEqual(hud_calls, ["recording"])
                    self.assertEqual(beep_calls, ["start"])

                    # 4. Active recording started in terminal, then focus switched to Windows App -> toggle stop & ptt_cancel NEVER blocked!
                    cur_app[:] = ["Windows App", "com.microsoft.rdc.macos", "810", "Win11 Remote"]
                    win_app_fg = ForegroundWindow(0, "Windows App", "Win11 Remote", bundle_id="com.microsoft.rdc.macos", pid=810)
                    daemon._last_trigger_ts = -1e9
                    r_stop = daemon.handle_trigger("toggle", win_app_fg, from_hotkey=True)
                    self.assertTrue(r_stop["ok"])
                    self.assertEqual(r_stop["state"], "recognizing")
                    deadline = time.monotonic() + 2.0
                    while time.monotonic() < deadline and daemon.last_result.get("seq_id") != r_stop["seq_id"]:
                        time.sleep(0.02)
                    self.assertTrue(daemon.last_result["ok"])
                    self.assertFalse(daemon.last_result["pasted"])
                    self.assertEqual(daemon.last_result["paste_reason"], "focus_changed")

                    # Start in Ghostty via ptt_start, switch to Windows App, ptt_cancel cleanly cancels
                    daemon._last_trigger_ts = -1e9
                    r_ptt = daemon.handle_trigger(
                        "ptt_start",
                        ForegroundWindow(0, "Ghostty", "", bundle_id=GHOSTTY_BUNDLE_ID, pid=700),
                        from_hotkey=True,
                    )
                    self.assertTrue(r_ptt["ok"])
                    self.assertEqual(daemon.state, "recording")
                    r_cancel = daemon.handle_trigger("ptt_cancel", win_app_fg, from_hotkey=True)
                    self.assertEqual(r_cancel, {"ok": True, "state": "idle", "seq_id": r_ptt["seq_id"] + 1})
                    self.assertEqual(cancel_calls, [1])
                    self.assertEqual(daemon.state, "idle")
                finally:
                    daemon.close()

                # 5. only_in_terminals = False allows starting recording even in Windows App
                daemon_all = VoiceInputDaemon(
                    Config(hotkey=HotkeyConfig(only_in_terminals=False), refiner=RefinerConfig(provider="off")),
                    env={"HOME": str(tmp)},
                    runtime_dir=tmp / "rt_all",
                    platform=plat,
                    asr_provider=_StubAsr("允许任意窗口"),
                    refiner=create_refiner(Config(refiner=RefinerConfig(provider="off"))),
                    tracker=SessionTracker(home_dir=tmp, env={"HOME": str(tmp)}, runtime_dir=tmp / "rt_all", proc_root=tmp / "noproc"),
                )
                try:
                    cur_app[:] = ["Windows App", "com.microsoft.rdc.macos", "810", "Win11 Remote"]
                    r_any = daemon_all.handle_trigger(
                        "toggle",
                        ForegroundWindow(0, "Windows App", "", bundle_id="com.microsoft.rdc.macos", pid=810),
                        from_hotkey=True,
                    )
                    self.assertTrue(r_any["ok"])
                    self.assertEqual(daemon_all.state, "recording")
                finally:
                    daemon_all.close()

    def test_mac_shim_static_invariants(self) -> None:
        self.assertEqual(SHIM_SCHEMA_VERSION, 2)
        self.assertTrue(MAC_SHIM_SCRIPT.is_file())
        src = MAC_SHIM_SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("import Carbon", src)
        self.assertIn("import CoreAudio", src)
        # Right Control dual compatibility: keyCode 0x3E (62) + NX_DEVICERCTLKEYMASK (0x00002000)
        self.assertIn("0x00002000", src)
        self.assertIn("0x3E", src)
        # Zero Input Monitoring TCC prompt for default modifier hotkeys + physical keyState recovery
        self.assertIn("CGPreflightListenEventAccess() || AXIsProcessTrusted()", src)
        self.assertIn(": [.flagsChanged]", src)
        self.assertIn("CGEventSource.keyState", src)
        # Non-activating capsule HUD + independent Doubao-style upper toast panel invariants
        self.assertIn(".accessory", src)
        self.assertIn(".nonactivatingPanel", src)
        self.assertIn("private var toastPanel: NSPanel?", src)
        self.assertIn("private var toastHideWorkItem: DispatchWorkItem?", src)
        self.assertIn("ignoresMouseEvents = true", src)
        self.assertIn("hidesOnDeactivate = false", src)
        self.assertIn("orderFrontRegardless()", src)
        self.assertNotIn("makeKeyAndOrderFront", src)
        # Chord tainting, HUD bottom/top positioning, & two-layer HUD states
        self.assertIn("chordTainted", src)
        self.assertIn("ptt_cancel", src)
        self.assertIn("counterForEventType", src)
        self.assertIn('case "recognizing", "processing":', src)
        self.assertIn('case "refining":', src)
        self.assertIn('case "no_mic":', src)
        self.assertIn('case "mic_unavailable":', src)
        self.assertIn('case "mic_permission":', src)
        self.assertIn('case "mic_disconnected":', src)
        self.assertIn('case "audio_recovered":', src)
        self.assertIn('case "paste_failed":', src)
        self.assertIn("✕ 无可用麦克风", src)
        self.assertIn("! 麦克风暂忙，请再试一次", src)
        self.assertIn("! 麦克风短暂中断，已自动恢复", src)
        self.assertIn("✕ 麦克风已断开", src)
        self.assertIn("func computeHudPillY(", src)
        self.assertIn("func computeHudToastY(", src)
        self.assertIn("round(visibleMinY + 8.0)", src)
        self.assertIn("round(visibleMaxY - pillH - 14.0)", src)
        self.assertIn("round(visibleMinY + 8.0 + effMainH + 8.0)", src)
        self.assertIn("round(visibleMaxY - 14.0 - effMainH - 8.0 - toastH)", src)
        self.assertIn("func splitHudSegments(", src)
        self.assertIn("func stripOuterParens(", src)
        self.assertIn("func stripLeadingHudSymbol(", src)
        self.assertIn("func resolveHudVisualStyle(", src)
        self.assertIn("rgba(64, 18, 26, 0.95)", src)
        self.assertIn("rgba(72, 38, 12, 0.95)", src)
        self.assertIn("hudRgba(16, 40, 72, 0.95)", src)
        self.assertIn("hudRgba(46, 22, 76, 0.95)", src)
        self.assertIn("hudRgba(14, 54, 36, 0.95)", src)
        self.assertIn("hudRgba(28, 32, 82, 0.95)", src)
        self.assertIn("HudRgba(r: 30.0 / 255.0, g: 31.0 / 255.0, b: 36.0 / 255.0, a: 0.96)", src)
        self.assertIn("HudRgba(r: 251.0 / 255.0, g: 191.0 / 255.0, b: 36.0 / 255.0, a: 0.78)", src)
        self.assertIn("HudRgba(r: 248.0 / 255.0, g: 113.0 / 255.0, b: 113.0 / 255.0, a: 0.78)", src)
        self.assertIn("final class StatusIconView: NSView", src)
        self.assertIn("func recordingCountdownWarnSec(", src)
        self.assertIn("func formatCountdownHint(", src)
        # 42pt Retina-scaled main capsule & 36pt toast capsule + CoreAudio live monitor
        self.assertIn("max(42.0, ceil(textH) + 18.0)", src)
        self.assertIn("max(36.0, ceil(textH) + 14.0)", src)
        self.assertIn("ofSize: 15.0, weight: .medium", src)
        self.assertIn("ofSize: 14.0, weight: .medium", src)
        self.assertIn("barWidth: CGFloat = 3.0", src)
        self.assertIn("barGap: CGFloat = 3.0", src)
        self.assertIn("totalWidth: CGFloat = 51.0", src)
        self.assertIn("contentsScale = scale", src)
        self.assertIn('obj["position"] as? String', src)
        self.assertIn("func queryAudioInputStatus()", src)
        self.assertIn("kAudioHardwarePropertyDevices", src)
        self.assertIn("kAudioHardwarePropertyDefaultInputDevice", src)
        self.assertIn("kAudioDevicePropertyDeviceIsAlive", src)
        self.assertIn('"audio_devices_changed"', src)

    def test_macos_coreaudio_fast_fail_busy_disconnect_and_seamless_respawn(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            rt = tmp / "rt"
            rt.mkdir()
            env = {"HOME": str(tmp), "AGENT_VOICE_INPUT_SWIFTC": "none"}

            # 1. audio_status.has_input == False -> zero ffmpeg subprocesses spawned, fast-fails with NoMicrophoneError
            ff_spawns_1: list[tuple[str, str]] = []
            plat_no_in = MacOSPlatform(Config(), env=env, cache_dir=tmp / "c1")
            plat_no_in._ff_rec = FfmpegWavRecorder(
                cmd_factory=lambda fmt, src: (ff_spawns_1.append((fmt, src)) or [sys.executable, "-c", "pass"])
            )
            plat_no_in._has_managed_shim = lambda: True  # type: ignore[method-assign]
            plat_no_in._shim_rpc = lambda payload, timeout_sec=0.8, **kw: (  # type: ignore[method-assign]
                {
                    "ok": True,
                    "reply": True,
                    "audio_status": {
                        "has_input": False,
                        "input_count": 0,
                        "default_uid": "",
                        "default_name": "",
                        "alive_uids": [],
                    },
                }
                if payload.get("cmd") == "audio_status"
                else {"ok": True}
            )
            with self.assertRaises(NoMicrophoneError):
                plat_no_in.start_recording(rt)
            self.assertEqual(ff_spawns_1, [])
            self.assertEqual(plat_no_in.last_audio_error, "coreaudio_no_input_device")

            # 2. has_input == True + ffmpeg fails with Input/output error (e.g. BT A2DP->HFP switch)
            # -> raises MicrophoneUnavailableError(reason_code="mic_unavailable", hud_state="mic_unavailable")
            io_err_src = "import sys; sys.stderr.write('[in#0] Error opening input: Input/output error\\n'); sys.exit(251)"
            plat_busy = MacOSPlatform(Config(), env=env, cache_dir=tmp / "c2")
            plat_busy._ff_rec = FfmpegWavRecorder(cmd_factory=lambda _f, _s: [sys.executable, "-c", io_err_src])
            plat_busy._has_managed_shim = lambda: True  # type: ignore[method-assign]
            plat_busy._shim_rpc = lambda payload, timeout_sec=0.8, **kw: (  # type: ignore[method-assign]
                {
                    "ok": True,
                    "reply": True,
                    "audio_status": {
                        "has_input": True,
                        "input_count": 1,
                        "default_uid": "bt-earbuds:input",
                        "default_name": "FreeClip",
                        "alive_uids": ["bt-earbuds:input"],
                    },
                }
                if payload.get("cmd") == "audio_status"
                else {"ok": True}
            )
            with self.assertRaises(MicrophoneUnavailableError) as busy_ctx:
                plat_busy.start_recording(rt)
            self.assertEqual(busy_ctx.exception.reason_code, "mic_unavailable")
            self.assertEqual(busy_ctx.exception.hud_state, "mic_unavailable")

            # 3. Mid-recording audio_devices_changed (active_uid disappears) -> immediately aborts recording,
            # preserves first-half PCM, sets last_audio_error="coreaudio_device_disconnected", fires _recorder_lost_cb
            hold_src = (
                "import os, sys, time\n"
                "os.write(1, (b'\\xe8\\x0e\\x18\\xf1' * 4000))\n"  # 0.5s @ 16kHz voiced audio
                "sys.stdout.flush()\n"
                "time.sleep(10.0)\n"
            )
            lost_evt = threading.Event()
            rescued_box: list[tuple[bytes, str]] = []
            plat_drop = MacOSPlatform(Config(), env=env, cache_dir=tmp / "c3")
            plat_drop._ff_rec = FfmpegWavRecorder(cmd_factory=lambda _f, _s: [sys.executable, "-c", hold_src])
            plat_drop._has_managed_shim = lambda: True  # type: ignore[method-assign]
            plat_drop._shim_rpc = lambda payload, timeout_sec=0.8, **kw: (  # type: ignore[method-assign]
                {
                    "ok": True,
                    "reply": True,
                    "audio_status": {
                        "has_input": True,
                        "input_count": 1,
                        "default_uid": "bt-earbuds:input",
                        "default_name": "FreeClip",
                        "alive_uids": ["bt-earbuds:input"],
                    },
                }
                if payload.get("cmd") == "audio_status"
                else {"ok": True}
            )

            def _on_lost_drop() -> None:
                rescued_box.append(plat_drop.stop_recording(drain=False))
                lost_evt.set()

            plat_drop.set_recorder_lost_callback(_on_lost_drop)
            plat_drop.start_recording(rt)
            self.assertEqual(plat_drop._active_recording_uid, "bt-earbuds:input")
            t_drop = time.monotonic()
            plat_drop._on_shim_internal_event(
                {
                    "event": "audio_devices_changed",
                    "has_input": False,
                    "input_count": 0,
                    "default_uid": "",
                    "default_name": "",
                    "alive_uids": [],
                }
            )
            self.assertTrue(lost_evt.wait(timeout=1.0))
            self.assertLess(time.monotonic() - t_drop, 0.5)
            self.assertEqual(plat_drop.last_audio_error, "coreaudio_device_disconnected")
            self.assertEqual(len(rescued_box), 1)
            rescued_wav, rescued_backend = rescued_box[0]
            self.assertEqual(rescued_backend, "avfoundation")
            self.assertEqual(len(rescued_wav), 44 + 16000)
            self.assertEqual(rescued_wav[44:52], b"\xe8\x0e\x18\xf1" * 2)

            # 4. Mid-recording transient ffmpeg exit while active_uid remains in alive_uids ->
            # seamlessly respawns ffmpeg once, concatenates both PCM segments + silence padding, fires _recorder_recovered_cb
            gen_counter = [0]
            seg1_src = (
                "import os, sys, time\n"
                "os.write(1, b'\\x11\\x22' * 3200)\n"  # 6400 bytes PCM (0.2s)
                "sys.stdout.flush()\n"
                "time.sleep(0.05)\n"
                "sys.stderr.write('transient HFP glitch\\n')\n"
                "sys.exit(251)\n"
            )
            seg2_src = (
                "import os, sys, time\n"
                "os.write(1, b'\\x33\\x44' * 4800)\n"  # 9600 bytes PCM (0.3s)
                "sys.stdout.flush()\n"
                "time.sleep(5.0)\n"
            )

            seen_respawn_srcs: list[str] = []

            def _respawn_cmd_factory(_fmt: str, src: str) -> list[str]:
                seen_respawn_srcs.append(src)
                gen_counter[0] += 1
                return [sys.executable, "-c", seg1_src if gen_counter[0] == 1 else seg2_src]

            recovered_gaps: list[int] = []
            rec_evt = threading.Event()
            bogus_lost: list[int] = []
            plat_rec = MacOSPlatform(
                Config(audio=AudioConfig(tail_drain_sec=0.0)),
                env=env,
                cache_dir=tmp / "c4",
            )
            plat_rec._ff_rec = FfmpegWavRecorder(cmd_factory=_respawn_cmd_factory)
            plat_rec._has_managed_shim = lambda: True  # type: ignore[method-assign]
            plat_rec._shim_rpc = lambda payload, timeout_sec=0.8, **kw: (  # type: ignore[method-assign]
                {
                    "ok": True,
                    "reply": True,
                    "audio_status": {
                        "has_input": True,
                        "input_count": 1,
                        "default_uid": "bt-earbuds:input",
                        "default_name": "FreeClip",
                        "alive_uids": ["bt-earbuds:input"],
                    },
                }
                if payload.get("cmd") == "audio_status"
                else {"ok": True}
            )
            plat_rec.set_recorder_lost_callback(lambda: bogus_lost.append(1))
            plat_rec.set_recorder_recovered_callback(lambda gap: (recovered_gaps.append(gap), rec_evt.set()))

            plat_rec.start_recording(rt)
            self.assertTrue(rec_evt.wait(timeout=2.0))
            self.assertEqual(gen_counter[0], 2)
            self.assertEqual(seen_respawn_srcs, [":default", ":FreeClip"])
            self.assertEqual(bogus_lost, [])
            self.assertEqual(len(recovered_gaps), 1)
            self.assertGreaterEqual(recovered_gaps[0], 0)

            merged_wav, merged_backend = plat_rec.stop_recording(drain=False)
            self.assertEqual(merged_backend, "avfoundation")
            self.assertEqual(plat_rec.last_audio_error, "")
            expected_pad = (min(30000, recovered_gaps[0]) * 32) & ~1
            merged_pcm = merged_wav[44:]
            self.assertEqual(len(merged_pcm), 6400 + expected_pad + 9600)
            self.assertEqual(merged_pcm[:6400], b"\x11\x22" * 3200)
            if expected_pad > 0:
                self.assertEqual(merged_pcm[6400 : 6400 + expected_pad], b"\x00" * expected_pad)
            self.assertEqual(merged_pcm[-9600:], b"\x33\x44" * 4800)

            # 5. No silent drift to another mic B when default_uid changed mid-recording, AND immediate-EOF on
            # respawned child reports lost instead of false recovered!
            drift_gen = [0]
            plat_drift = MacOSPlatform(Config(audio=AudioConfig(tail_drain_sec=0.0)), env=env, cache_dir=tmp / "c5")
            plat_drift._ff_rec = FfmpegWavRecorder(
                cmd_factory=lambda _f, _s: (
                    drift_gen.__setitem__(0, drift_gen[0] + 1)
                    or [sys.executable, "-c", seg1_src if drift_gen[0] == 1 else seg2_src]
                )
            )
            plat_drift._has_managed_shim = lambda: True  # type: ignore[method-assign]
            cur_def = ["mic-a:input"]
            plat_drift._shim_rpc = lambda payload, timeout_sec=0.8, **kw: (  # type: ignore[method-assign]
                {
                    "ok": True,
                    "reply": True,
                    "audio_status": {
                        "has_input": True,
                        "input_count": 2,
                        "default_uid": cur_def[0],
                        "default_name": "Mic",
                        "alive_uids": ["mic-a:input", "mic-b:input"],
                    },
                }
                if payload.get("cmd") == "audio_status"
                else {"ok": True}
            )
            drift_lost = threading.Event()
            plat_drift.set_recorder_lost_callback(drift_lost.set)
            plat_drift.start_recording(rt)
            cur_def[0] = "mic-b:input"  # Default switched to B while A is still in alive_uids
            self.assertTrue(drift_lost.wait(timeout=2.0))
            self.assertEqual(drift_gen[0], 1)  # Must NOT respawn onto mic-b!
            wav_drift, _ = plat_drift.stop_recording(drain=False)
            self.assertEqual(wav_drift[44:], b"\x11\x22" * 3200)

            imm_eof_gen = [0]
            imm_eof_seg2 = (
                "import os, sys\n"
                "os.write(1, b'\\x55\\x66' * 100)\n"
                "sys.stdout.flush()\n"
                "sys.exit(1)\n"
            )
            plat_imm = MacOSPlatform(Config(audio=AudioConfig(tail_drain_sec=0.0)), env=env, cache_dir=tmp / "c6")
            plat_imm._ff_rec = FfmpegWavRecorder(
                cmd_factory=lambda _f, _s: (
                    imm_eof_gen.__setitem__(0, imm_eof_gen[0] + 1)
                    or [sys.executable, "-c", seg1_src if imm_eof_gen[0] == 1 else imm_eof_seg2]
                )
            )
            plat_imm._has_managed_shim = lambda: True  # type: ignore[method-assign]
            plat_imm._shim_rpc = plat_rec._shim_rpc  # type: ignore[method-assign]
            imm_lost = threading.Event()
            imm_false_rec: list[int] = []
            plat_imm.set_recorder_lost_callback(imm_lost.set)
            plat_imm.set_recorder_recovered_callback(imm_false_rec.append)
            plat_imm.start_recording(rt)
            self.assertTrue(imm_lost.wait(timeout=2.0))
            self.assertEqual(imm_false_rec, [])
            wav_imm, _ = plat_imm.stop_recording(drain=False)
            self.assertGreaterEqual(len(wav_imm), 44 + 6400)

            # 6. Post-start live CoreAudio verification catches default switch during start() even before
            # audio_devices_changed event is consumed, AND late EOF during post_st RPC is never swallowed.
            switch_rpc_calls = [0]
            plat_post_sw = MacOSPlatform(Config(audio=AudioConfig(tail_drain_sec=0.0)), env=env, cache_dir=tmp / "c7")
            plat_post_sw._ff_rec = FfmpegWavRecorder(
                cmd_factory=lambda _f, _s: [
                    sys.executable,
                    "-c",
                    seg1_src if switch_rpc_calls[0] <= 1 else seg2_src,
                ]
            )
            plat_post_sw._has_managed_shim = lambda: True  # type: ignore[method-assign]

            def _post_switch_rpc(payload: dict[str, object], timeout_sec: float = 0.8, **kw: object) -> dict[str, object]:
                if payload.get("cmd") != "audio_status":
                    return {"ok": True}
                switch_rpc_calls[0] += 1
                # Call 1 (start_recording) & Call 2 (pre-respawn): mic-a ("MicA"); Call 3 (post-respawn): switched to mic-b!
                is_after_respawn = switch_rpc_calls[0] >= 3
                return {
                    "ok": True,
                    "reply": True,
                    "audio_status": {
                        "has_input": True,
                        "input_count": 2,
                        "default_uid": "mic-b:input" if is_after_respawn else "mic-a:input",
                        "default_name": "MicB" if is_after_respawn else "MicA",
                        "default_name_unique": True,
                        "alive_uids": ["mic-a:input", "mic-b:input"],
                        "uid_names": {"mic-a:input": "MicA", "mic-b:input": "MicB"},
                    },
                }

            plat_post_sw._shim_rpc = _post_switch_rpc  # type: ignore[method-assign]
            post_sw_lost = threading.Event()
            post_sw_false_rec: list[int] = []
            plat_post_sw.set_recorder_lost_callback(post_sw_lost.set)
            plat_post_sw.set_recorder_recovered_callback(post_sw_false_rec.append)
            plat_post_sw.start_recording(rt)
            self.assertTrue(post_sw_lost.wait(timeout=2.5))
            self.assertEqual(post_sw_false_rec, [])
            self.assertEqual(plat_post_sw.last_audio_error, "coreaudio_default_input_switched")
            wav_post_sw, _ = plat_post_sw.stop_recording(drain=False)
            self.assertEqual(wav_post_sw[44:], b"\x11\x22" * 3200)

            # Late EOF arriving AFTER 0.08s wait while post_st RPC is in flight must not be swallowed
            late_eof_seg2 = (
                "import os, sys, time\n"
                "os.write(1, b'\\x77\\x88' * 200)\n"
                "sys.stdout.flush()\n"
                "time.sleep(0.11)\n"
                "sys.exit(1)\n"
            )
            late_gen = [0]
            late_rpc_calls = [0]
            plat_late = MacOSPlatform(Config(audio=AudioConfig(tail_drain_sec=0.0)), env=env, cache_dir=tmp / "c8")
            plat_late._ff_rec = FfmpegWavRecorder(
                cmd_factory=lambda _f, _s: (
                    late_gen.__setitem__(0, late_gen[0] + 1)
                    or [sys.executable, "-c", seg1_src if late_gen[0] == 1 else late_eof_seg2]
                )
            )
            plat_late._has_managed_shim = lambda: True  # type: ignore[method-assign]

            def _slow_post_rpc(payload: dict[str, object], timeout_sec: float = 0.8, **kw: object) -> dict[str, object]:
                if payload.get("cmd") == "audio_status":
                    late_rpc_calls[0] += 1
                    if late_rpc_calls[0] >= 3:
                        time.sleep(0.12)
                return plat_rec._shim_rpc(payload, timeout_sec=timeout_sec)

            plat_late._shim_rpc = _slow_post_rpc  # type: ignore[method-assign]
            late_lost = threading.Event()
            late_false_rec: list[int] = []
            plat_late.set_recorder_lost_callback(late_lost.set)
            plat_late.set_recorder_recovered_callback(late_false_rec.append)
            plat_late.start_recording(rt)
            self.assertTrue(late_lost.wait(timeout=2.5))
            self.assertEqual(late_false_rec, [])
            _, _ = plat_late.stop_recording(drain=False)

            # 7. >1.5s recovery gap pads full actual silence without 1500ms truncation, AND busy timeout isolates sessions
            long_gap_gen = [0]
            plat_lgap = MacOSPlatform(Config(audio=AudioConfig(tail_drain_sec=0.0)), env=env, cache_dir=tmp / "c9")
            plat_lgap._ff_rec = FfmpegWavRecorder(
                cmd_factory=lambda _f, _s: (
                    long_gap_gen.__setitem__(0, long_gap_gen[0] + 1)
                    or [sys.executable, "-c", seg1_src if long_gap_gen[0] == 1 else seg2_src]
                )
            )
            plat_lgap._has_managed_shim = lambda: True  # type: ignore[method-assign]
            lgap_calls = [0]

            def _delayed_pre_rpc(payload: dict[str, object], timeout_sec: float = 0.8, **kw: object) -> dict[str, object]:
                if payload.get("cmd") == "audio_status":
                    lgap_calls[0] += 1
                    if lgap_calls[0] == 2:
                        time.sleep(1.55)
                return plat_rec._shim_rpc(payload, timeout_sec=timeout_sec)

            plat_lgap._shim_rpc = _delayed_pre_rpc  # type: ignore[method-assign]
            lgap_vals: list[int] = []
            lgap_evt = threading.Event()
            plat_lgap.set_recorder_recovered_callback(lambda g: (lgap_vals.append(g), lgap_evt.set()))
            plat_lgap.start_recording(rt)
            self.assertTrue(lgap_evt.wait(timeout=4.0))
            self.assertGreater(lgap_vals[0], 1520)
            wav_lgap, _ = plat_lgap.stop_recording(drain=False)
            self.assertEqual(len(wav_lgap[44:]), 6400 + ((lgap_vals[0] * 32) & ~1) + 9600)

            # Session isolation under busy timeout: stuck old recovery cannot leak PCM or stop a newly started session
            block_enter = threading.Event()
            block_release = threading.Event()
            iso_gen = [0]
            plat_iso = MacOSPlatform(Config(audio=AudioConfig(tail_drain_sec=0.0)), env=env, cache_dir=tmp / "c10")
            plat_iso._busy_wait_timeout_sec = 0.08
            plat_iso._ff_rec = FfmpegWavRecorder(
                cmd_factory=lambda _f, _s: (
                    iso_gen.__setitem__(0, iso_gen[0] + 1)
                    or [sys.executable, "-c", seg1_src if iso_gen[0] == 1 else seg2_src]
                )
            )
            plat_iso._has_managed_shim = lambda: True  # type: ignore[method-assign]
            iso_rpc_calls = [0]

            def _blocking_rpc(payload: dict[str, object], timeout_sec: float = 0.8, **kw: object) -> dict[str, object]:
                if payload.get("cmd") == "audio_status":
                    iso_rpc_calls[0] += 1
                    if iso_rpc_calls[0] == 2:
                        block_enter.set()
                        block_release.wait(timeout=2.0)
                return plat_rec._shim_rpc(payload, timeout_sec=timeout_sec)

            plat_iso._shim_rpc = _blocking_rpc  # type: ignore[method-assign]
            plat_iso.start_recording(rt)
            self.assertTrue(block_enter.wait(timeout=1.5))
            # stop_recording times out explicitly instead of touching busy recorder or leaving orphaned cache
            wav_to, _ = plat_iso.stop_recording(drain=False)
            self.assertEqual(wav_to, b"")
            self.assertEqual(plat_iso.last_audio_error, "recorder_busy_timeout")
            self.assertIsNone(plat_iso._cur_session)
            # Starting a new session while old session is still blocked allocates an isolated recorder
            plat_iso.start_recording(rt)
            new_sess = plat_iso._cur_session
            self.assertIsNotNone(new_sess)
            assert new_sess is not None
            self.assertTrue(new_sess.rec.is_running)
            block_release.set()
            time.sleep(0.12)
            # Unblocked old task must NOT have stopped new_sess.rec!
            self.assertTrue(new_sess.rec.is_running)
            wav_new, _ = plat_iso.stop_recording(drain=False)
            self.assertEqual(wav_new[44:], b"\x33\x44" * 4800)

            # 8. Slow post-respawn RPC while seg2 keeps streaming uses first_chunk_ts (no double-counted gap)
            streaming_seg2 = (
                "import os, sys, time\n"
                "for _ in range(12):\n"
                "    os.write(1, b'\\x55\\x66' * 400)\n"
                "    sys.stdout.flush()\n"
                "    time.sleep(0.03)\n"
                "time.sleep(2.0)\n"
            )
            fc_gen = [0]
            fc_rpc = [0]
            plat_fc = MacOSPlatform(Config(audio=AudioConfig(tail_drain_sec=0.0)), env=env, cache_dir=tmp / "c11")
            plat_fc._ff_rec = FfmpegWavRecorder(
                cmd_factory=lambda _f, _s: (
                    fc_gen.__setitem__(0, fc_gen[0] + 1)
                    or [sys.executable, "-c", seg1_src if fc_gen[0] == 1 else streaming_seg2]
                )
            )
            plat_fc._has_managed_shim = lambda: True  # type: ignore[method-assign]

            def _slow_post_for_fc(payload: dict[str, object], timeout_sec: float = 0.8, **kw: object) -> dict[str, object]:
                if payload.get("cmd") == "audio_status":
                    fc_rpc[0] += 1
                    if fc_rpc[0] >= 3:
                        time.sleep(0.32)
                return plat_rec._shim_rpc(payload, timeout_sec=timeout_sec)

            plat_fc._shim_rpc = _slow_post_for_fc  # type: ignore[method-assign]
            fc_gaps: list[int] = []
            fc_evt = threading.Event()
            plat_fc.set_recorder_recovered_callback(lambda g: (fc_gaps.append(g), fc_evt.set()))
            plat_fc.start_recording(rt)
            self.assertTrue(fc_evt.wait(timeout=2.5))
            # True startup gap is <240ms; must NOT inflate by the 320ms post_st RPC!
            self.assertLess(fc_gaps[0], 245)
            _, _ = plat_fc.stop_recording(drain=False)

    def test_macos_clt_check_first_run_progress_and_xcode_license_hint(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cache_dir = tmp / "clt_cache"

            # 1. DEVELOPER_DIR validation without invoking xcode-select
            dev_dir = tmp / "FakeXcode.app" / "Contents" / "Developer"
            dev_dir.mkdir(parents=True)
            self.assertEqual(check_macos_clt_ready({"DEVELOPER_DIR": str(dev_dir)}), (True, ""))
            ok_bad_dev, err_bad_dev = check_macos_clt_ready({"DEVELOPER_DIR": str(tmp / "missing_dev")})
            self.assertFalse(ok_bad_dev)
            self.assertIn("xcode-select --install", err_bad_dev)

            # 2. Missing CLT when using /usr/bin/swiftc stub: NEVER invokes /usr/bin/swiftc (no GUI modal!)
            fake_xs = tmp / "xcode-select"
            fake_xs.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(2)\n", encoding="utf-8")
            fake_xs.chmod(0o755)
            subproc_calls: list[list[str]] = []

            def spy_run(cmd, *a, **kw):
                subproc_calls.append(list(cmd))
                if Path(cmd[0]).name == "xcode-select":
                    return subprocess.CompletedProcess(cmd, 2, "", "xcode-select: error: unable to get active developer directory\n")
                raise AssertionError(f"/usr/bin/swiftc stub MUST NOT be invoked when CLT is missing: {cmd}")

            err_buf = io.StringIO()
            orig_isfile = os.path.isfile
            with (
                mock.patch(
                    "agent_voice_input.platform_macos.os.path.isfile",
                    side_effect=lambda p: str(p) == str(fake_xs) or (str(p) != "/usr/bin/xcode-select" and orig_isfile(p)),
                ),
                mock.patch("agent_voice_input.platform_macos.os.access", return_value=True),
                mock.patch("agent_voice_input.platform_macos.shutil.which", side_effect=lambda n, path=None: str(fake_xs) if n == "xcode-select" else None),
                mock.patch("agent_voice_input.platform_macos.subprocess.run", side_effect=spy_run),
                mock.patch("sys.stderr", err_buf),
            ):
                b_clt, e_clt = compile_mac_shim(
                    env={"HOME": str(tmp), "PATH": str(tmp)},
                    cache_dir=cache_dir,
                    swiftc_bin="/usr/bin/swiftc",
                    announce=True,
                )
            self.assertIsNone(b_clt)
            self.assertIn("xcode-select --install", e_clt)
            self.assertIn("agent-voice-input restart", e_clt)
            self.assertEqual(len(subproc_calls), 1)
            self.assertEqual(Path(subproc_calls[0][0]).name, "xcode-select")
            # No misleading "Compiling..." progress when CLT is missing
            self.assertEqual(err_buf.getvalue(), "")

            # 3. Cold compile with announce=True emits clear stderr progress once; warm cache hit stays silent
            fake_swift = tmp / "prog_shim.swift"
            fake_swift.write_text("// cold-compile-test\n", encoding="utf-8")
            fake_swiftc = tmp / "prog_swiftc"
            fake_swiftc.write_text(
                f"#!{sys.executable}\n"
                "import os, pathlib, sys\n"
                "if 'UNACCEPTED_LICENSE' in os.environ:\n"
                "    sys.stderr.write('Agreeing to the Xcode/iOS license requires admin privileges, please run \"sudo xcodebuild -license\"\\n')\n"
                "    sys.exit(69)\n"
                "out = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
                "out.write_text('#!/bin/sh\\necho ok\\n', encoding='utf-8')\n",
                encoding="utf-8",
            )
            fake_swiftc.chmod(0o755)

            # Unaccepted Xcode license appends non-interactive `sudo xcodebuild -license accept` hint
            b_lic, e_lic = compile_mac_shim(
                env={"HOME": str(tmp), "UNACCEPTED_LICENSE": "1"},
                cache_dir=cache_dir,
                swift_source=fake_swift,
                swiftc_bin=str(fake_swiftc),
            )
            self.assertIsNone(b_lic)
            self.assertIn("sudo xcodebuild -license accept", e_lic)

            cold_out, cold_err = io.StringIO(), io.StringIO()
            with mock.patch("sys.stdout", cold_out), mock.patch("sys.stderr", cold_err):
                b_cold, e_cold = compile_mac_shim(
                    env={"HOME": str(tmp)},
                    cache_dir=cache_dir,
                    swift_source=fake_swift,
                    swiftc_bin=str(fake_swiftc),
                    announce=True,
                )
            self.assertIsNotNone(b_cold)
            self.assertEqual(e_cold, "")
            self.assertEqual(cold_out.getvalue(), "")
            self.assertIn("Compiling native macOS helper (first run, may take 5-15s)", cold_err.getvalue())
            self.assertIn("Native macOS helper ready", cold_err.getvalue())

            warm_err = io.StringIO()
            with mock.patch("sys.stderr", warm_err):
                b_warm, e_warm = compile_mac_shim(
                    env={"HOME": str(tmp)},
                    cache_dir=cache_dir,
                    swift_source=fake_swift,
                    swiftc_bin=str(fake_swiftc),
                    announce=True,
                )
            self.assertEqual(b_warm, b_cold)
            self.assertEqual(e_warm, "")
            self.assertEqual(warm_err.getvalue(), "")

            # 4. _cmd_doctor clearly reports MISSING CLT when clt_ok is False
            fake_probe_noclts = {
                "avfoundation_ok": True,
                "audio_devices": ["[0] Built-in Mic"],
                "configured_audio_device": ":default",
                "pbcopy_ok": True,
                "tmux_active": False,
                "clt_ok": False,
                "clt_error": "Xcode Command Line Tools not installed",
                "swiftc_ok": False,
                "swiftc_bin": "/usr/bin/swiftc",
                "mac_shim_ok": False,
                "mac_shim_bin": "",
                "mac_shim_error": "Xcode Command Line Tools not installed; run 'xcode-select --install'",
                "fg_process": "Ghostty",
                "fg_title": "zsh",
                "permissions_hint": "Microphone -> terminal",
            }
            doc_buf = io.StringIO()
            with (
                mock.patch.object(cli_mod.sys, "platform", "darwin"),
                mock.patch.object(cli_mod, "probe_macos_host", return_value=fake_probe_noclts),
                mock.patch.object(cli_mod, "resolve_ffmpeg_bin", return_value="/opt/homebrew/bin/ffmpeg"),
                mock.patch("sys.stdout", doc_buf),
            ):
                cli_mod._cmd_doctor(Config(refiner=RefinerConfig(provider="off")), {"HOME": str(tmp), "DASHSCOPE_API_KEY": "sk-doc"})
            self.assertIn("MISSING CLT (run 'xcode-select --install')", doc_buf.getvalue())

    @unittest.skipUnless(
        sys.platform == "darwin"
        and bool(os.path.isfile("/usr/bin/swiftc") or shutil.which("swiftc"))
        and check_macos_clt_ready()[0],
        "macOS swiftc + Xcode Command Line Tools required for native shim compile check",
    )
    def test_real_swiftc_compiles_mac_shim_and_passes_self_check(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            isolated_env = {
                **os.environ,
                "HOME": str(tmp),
                "XDG_CACHE_HOME": str(tmp / "cache"),
                "AGENT_VOICE_INPUT_CACHE_DIR": str(tmp / "shim_cache"),
            }
            bin_path, err = compile_mac_shim(env=isolated_env, cache_dir=tmp / "shim_cache")
            self.assertEqual(err, "", f"mac_shim.swift failed to compile: {err}")
            self.assertIsNotNone(bin_path)
            assert bin_path is not None
            self.assertTrue(bin_path.is_file())

            proc = subprocess.run(
                [str(bin_path), "--self-check"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                env=isolated_env,
                timeout=10,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, f"--self-check failed: stderr={proc.stderr!r} stdout={proc.stdout!r}")
            payload = json.loads(proc.stdout.strip())
            self.assertTrue(payload.get("ok"))
            self.assertEqual(payload.get("mode"), "self-check")
            self.assertEqual(payload.get("rcontrol_vk"), "0x3E")
            self.assertEqual(payload.get("rcontrol_mask"), "0x2000")


if __name__ == "__main__":
    unittest.main()
