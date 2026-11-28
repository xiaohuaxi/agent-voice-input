"""Deterministic offline regression tests for v1.0.0 architecture & concurrency audit fixes."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import queue
import struct
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
import unittest
from unittest import mock

from agent_voice_input import daemon as daemon_mod
from agent_voice_input._tmux import TmuxClientInfo, TmuxPaneInfo, TmuxServerSnapshot
from agent_voice_input.asr_providers import AsrResult, BaseAsrProvider
from agent_voice_input.audio_proc import AudioGateResult
from agent_voice_input.config import Config, RemoteConfig, RemoteHostConfig
from agent_voice_input.daemon import VoiceInputDaemon, _CommitSlot, _QueuedUtterance, _Stage1State
from agent_voice_input.platform_common import ForegroundWindow, PasteOutcome
from agent_voice_input.platform_macos import GHOSTTY_BUNDLE_ID, MacOSPlatform
from agent_voice_input.platform_wsl import SHIM_SCRIPT, WslPlatform, list_win_shim_sources, read_win_shim_source, resolve_powershell_exe, to_windows_path
from agent_voice_input.refiner import BaseRefiner, RefinerResult
from agent_voice_input.remote_ssh import RemoteSshManager
from agent_voice_input.session_ctx import (
    SessionTracker,
    parse_window_title_hint,
    write_session_registry_entry,
)


def _make_speech_wav(duration_sec: float = 0.7, sample_rate: int = 16000) -> bytes:
    n = int(duration_sec * sample_rate)
    samples = [int(12000 * math.sin(2.0 * math.pi * 440.0 * i / sample_rate)) for i in range(n)]
    pcm = struct.pack(f"<{n}h", *samples)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(pcm),
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        sample_rate,
        sample_rate * 2,
        2,
        16,
        b"data",
        len(pcm),
    )
    return header + pcm


class _StaticAsr(BaseAsrProvider):
    provider_name = "static_test"

    def __init__(self) -> None:
        pass

    def transcribe(self, wav_bytes: bytes, **kwargs: Any) -> AsrResult:
        del wav_bytes, kwargs
        return AsrResult(text="测试语音输入", provider="static_test", model="m1", latency_ms=5)


class _NoopRefiner(BaseRefiner):
    provider_name = "off"

    def refine(self, asr_res: AsrResult, **kwargs: Any) -> RefinerResult:
        del kwargs
        return RefinerResult(text=asr_res.text, provider="off", model="off", latency_ms=0)


class _DummyPlatform:
    def __init__(self, wav: bytes | None = None) -> None:
        self._wav = wav if wav is not None else _make_speech_wav()
        self.start_calls = 0
        self.stop_calls = 0
        self.cancel_calls = 0
        self.delivered: list[str] = []

    def start_recording(self, runtime_dir: Path, *, loopback: bool = False, seq_id: int | None = None) -> None:
        del runtime_dir, loopback, seq_id
        self.start_calls += 1

    def stop_recording(self, *, drain: bool = True) -> tuple[bytes, str]:
        del drain
        self.stop_calls += 1
        return self._wav, "dummy"

    def cancel_recording(self) -> None:
        self.cancel_calls += 1

    def get_foreground_window(self) -> ForegroundWindow:
        return ForegroundWindow(1, "Ghostty", "Ghostty", GHOSTTY_BUNDLE_ID)

    def is_allowed_terminal(self, window: ForegroundWindow) -> bool:
        del window
        return True

    def deliver_text(self, text: str, seq_id: int, target_hwnd: int = 0, **kwargs: Any) -> PasteOutcome:
        del seq_id, target_hwnd
        before_commit = kwargs.get("before_commit")
        if callable(before_commit) and not before_commit():
            return PasteOutcome(False, False, "cancelled", "Ghostty")
        self.delivered.append(text)
        return PasteOutcome(True, False, "ok", "Ghostty")

    def hud(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs

    def beep(self, kind: str) -> None:
        del kind

    def close(self) -> None:
        pass


class TestV100AuditFixes(unittest.TestCase):
    # ------------------------------------------------------------------
    # Fix 1: Ghostty same-surface tmux pane switch isolation & plain Ghostty direct delivery
    # ------------------------------------------------------------------
    def test_01_ghostty_same_surface_tmux_pane_switch_isolation_and_plain_direct(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            rt_dir = tmp / "rt"
            rt_dir.mkdir(parents=True)
            sock_path = str(tmp / "tmux-501" / "default")

            # Prepare two real JSONL sessions (A for pane %1, B for pane %2)
            sess_a_id = "aaaaaaaa-1111-2222-3333-444444444444"
            sess_b_id = "bbbbbbbb-1111-2222-3333-444444444444"
            repo_a = tmp / "repo-a"
            repo_b = tmp / "repo-b"
            repo_a.mkdir()
            repo_b.mkdir()
            jsonl_a = tmp / f"{sess_a_id}.jsonl"
            jsonl_b = tmp / f"{sess_b_id}.jsonl"
            jsonl_a.write_text(
                json.dumps({"type": "user", "message": {"role": "user", "content": "context from pane A"}}) + "\n",
                encoding="utf-8",
            )
            jsonl_b.write_text(
                json.dumps({"type": "user", "message": {"role": "user", "content": "context from pane B"}}) + "\n",
                encoding="utf-8",
            )
            write_session_registry_entry(
                rt_dir,
                cli="claude",
                session_id=sess_a_id,
                jsonl_path=str(jsonl_a),
                cwd=str(repo_a),
                tmux_pane="%1",
                tmux_socket=sock_path,
                tty="/dev/ttys011",
            )
            write_session_registry_entry(
                rt_dir,
                cli="claude",
                session_id=sess_b_id,
                jsonl_path=str(jsonl_b),
                cwd=str(repo_b),
                tmux_pane="%2",
                tmux_socket=sock_path,
                tty="/dev/ttys012",
            )

            def _make_snap(active_pane_id: str) -> TmuxServerSnapshot:
                return TmuxServerSnapshot(
                    clients=(
                        TmuxClientInfo(
                            session_id="$0",
                            window_id="@0",
                            pane_id=active_pane_id,
                            client_tty="/dev/ttys005",
                            client_pid=5000,
                            socket_path=sock_path,
                        ),
                    ),
                    panes=(
                        TmuxPaneInfo(
                            session_id="$0",
                            window_id="@0",
                            pane_id="%1",
                            pane_pid=5001,
                            pane_tty="/dev/ttys011",
                            current_path=str(repo_a),
                            pane_title="claude [#aaaaaaaa]",
                            pane_active=(active_pane_id == "%1"),
                            window_active=True,
                            socket_path=sock_path,
                        ),
                        TmuxPaneInfo(
                            session_id="$0",
                            window_id="@0",
                            pane_id="%2",
                            pane_pid=5002,
                            pane_tty="/dev/ttys012",
                            current_path=str(repo_b),
                            pane_title="claude [#bbbbbbbb]",
                            pane_active=(active_pane_id == "%2"),
                            window_active=True,
                            socket_path=sock_path,
                        ),
                    ),
                )

            snap_box = [_make_snap("%1")]
            ghostty_query_box = [("surf-1", "Ghostty", "/dev/ttys005", str(tmp), 4200)]

            plat = MacOSPlatform(
                Config(),
                env={"HOME": str(home), "PATH": "/usr/bin:/opt/homebrew/bin"},
                cache_dir=tmp / "cache",
                tmux_probe_fn=lambda: snap_box[0],
            )
            tracker = SessionTracker(
                home_dir=home,
                runtime_dir=rt_dir,
                proc_root=None,
                env={"HOME": str(home)},
                tmux_probe_fn=lambda: snap_box[0],
            )
            copied_texts: list[str] = []
            tmux_deliveries: list[tuple[str, str]] = []
            ghostty_direct_calls: list[tuple[str, str]] = []

            def _fake_tmux_deliver(
                cmd_prefix: list[str],
                text: str,
                *,
                seq_id: int,
                target_pane: str = "",
                tmux_env: Any = None,
            ) -> bool:
                del cmd_prefix, seq_id, tmux_env
                tmux_deliveries.append((target_pane, text))
                return True

            def _fake_osa(args: list[str], *, timeout: float = 2.5) -> subprocess.CompletedProcess[str]:
                del timeout
                if len(args) >= 5 and args[2] == "--":
                    ghostty_direct_calls.append((args[3], args[4]))
                    return subprocess.CompletedProcess(["osascript"], 0, "ok\n", "")
                return subprocess.CompletedProcess(["osascript"], 0, "", "")

            try:
                with mock.patch.object(
                    plat,
                    "_query_frontmost_process_and_title",
                    return_value=("Ghostty", GHOSTTY_BUNDLE_ID, "Ghostty", 4100),
                ), mock.patch.object(
                    plat,
                    "_query_ghostty_terminal",
                    side_effect=lambda: ghostty_query_box[0],
                ), mock.patch.object(
                    plat, "_resolve_tmux_exe", return_value="/opt/homebrew/bin/tmux"
                ), mock.patch.object(
                    plat, "_deliver_via_tmux_buffer", side_effect=_fake_tmux_deliver
                ), mock.patch.object(
                    plat, "_copy_to_clipboard", side_effect=lambda t: (copied_texts.append(t), True)[1]
                ), mock.patch.object(
                    plat, "_run_osascript", side_effect=_fake_osa
                ), mock.patch.object(
                    plat, "beep"
                ):
                    # 1) Origin capture in Ghostty surface running tmux pane %1 retains tmux_socket & tmux_pane
                    origin_a = plat.get_foreground_window()
                    self.assertEqual(origin_a.surface_id, "surf-1")
                    self.assertEqual(origin_a.tmux_pane, "%1")
                    self.assertEqual(origin_a.tmux_socket, sock_path)

                    # 2) Same pane %1: Stage 1 resolves Session A and delivery pastes into tmux pane %1
                    st_same = _Stage1State(t_total0=time.monotonic())
                    daemon_mod._execute_stage1(
                        st_same,
                        _make_speech_wav(),
                        cfg=Config(),
                        env={"HOME": str(home)},
                        tracker=tracker,
                        eff_asr=_StaticAsr(),
                        eff_refiner=_NoopRefiner(),
                        platform=plat,
                        origin_window=origin_a,
                        window_title_override=None,
                        audio_source="live",
                        explicit_mode="auto",
                        seq_id=1,
                        max_chunk_sec=None,
                        is_current_seq=None,
                        is_cancelled=lambda: False,
                        cancel_token=None,
                        cancel_check=lambda: False,
                        cancel_reason_fn=lambda: "",
                        archive_sink=None,
                    )
                    self.assertIsNotNone(st_same.snap)
                    assert st_same.snap is not None
                    self.assertEqual(st_same.snap.match.session_id, sess_a_id)

                    out_same = plat.deliver_text("给窗格A的口述", seq_id=1, origin_window=origin_a)
                    self.assertTrue(out_same.pasted)
                    self.assertEqual(out_same.reason, "tmux")
                    self.assertEqual(tmux_deliveries, [("%1", "给窗格A的口述")])
                    self.assertEqual(ghostty_direct_calls, [])

                    # 3) Switch tmux pane inside the SAME Ghostty surface surf-1: %1 -> %2
                    snap_box[0] = _make_snap("%2")
                    tmux_deliveries.clear()
                    copied_texts.clear()
                    ghostty_direct_calls.clear()

                    # Stage 1 MUST NOT borrow pane B's session context
                    st_switched = _Stage1State(t_total0=time.monotonic())
                    daemon_mod._execute_stage1(
                        st_switched,
                        _make_speech_wav(),
                        cfg=Config(),
                        env={"HOME": str(home)},
                        tracker=tracker,
                        eff_asr=_StaticAsr(),
                        eff_refiner=_NoopRefiner(),
                        platform=plat,
                        origin_window=origin_a,
                        window_title_override=None,
                        audio_source="live",
                        explicit_mode="auto",
                        seq_id=2,
                        max_chunk_sec=None,
                        is_current_seq=None,
                        is_cancelled=lambda: False,
                        cancel_token=None,
                        cancel_check=lambda: False,
                        cancel_reason_fn=lambda: "",
                        archive_sink=None,
                    )
                    self.assertIsNone(st_switched.snap, "Switched tmux pane must not borrow pane B context")

                    # Delivery MUST NOT paste A's dictation into pane B (falls back to clipboard only)
                    out_switched = plat.deliver_text("不应贴进窗格B", seq_id=2, origin_window=origin_a)
                    self.assertFalse(out_switched.pasted)
                    self.assertTrue(out_switched.clipboard)
                    self.assertEqual(out_switched.reason, "focus_changed")
                    self.assertEqual(tmux_deliveries, [])
                    self.assertEqual(ghostty_direct_calls, [])
                    self.assertEqual(copied_texts, ["不应贴进窗格B"])

                    # 4) Plain Ghostty surface (different tty not attached to tmux) keeps direct surface delivery
                    ghostty_query_box[0] = ("surf-plain", "zsh", "/dev/ttys009", str(tmp), 4300)
                    plain_origin = plat.get_foreground_window()
                    self.assertEqual(plain_origin.surface_id, "surf-plain")
                    self.assertEqual(plain_origin.tmux_pane, "")
                    out_plain = plat.deliver_text("普通Ghostty直投", seq_id=3, origin_window=plain_origin)
                    self.assertTrue(out_plain.pasted)
                    self.assertEqual(out_plain.reason, "ok")
                    self.assertEqual(ghostty_direct_calls, [("surf-plain", "普通Ghostty直投")])
            finally:
                plat.close()
                tracker.close()

    # ------------------------------------------------------------------
    # Fix 2: Windows Terminal token -> plain shell blocked + cross-language title parser parity
    # ------------------------------------------------------------------
    def test_02_windows_terminal_token_plain_shell_block_and_title_parser_parity(self) -> None:
        for src_path in list_win_shim_sources():
            raw_src = src_path.read_bytes()
            self.assertFalse(raw_src.startswith(b"\xef\xbb\xbf"), f"{src_path.name} must not have UTF-8 BOM")
            non_ascii = [(i, b) for i, b in enumerate(raw_src) if b >= 128]
            self.assertEqual(non_ascii, [], f"{src_path.name} must stay strictly 7-bit ASCII")
        ps1_text = SHIM_SCRIPT.read_text(encoding="ascii")

        # Verify both paste focus checks require curTok == expectTok (missing token is treated as conflict)
        self.assertIn('$tokConflict = ($expectTok -ne "") -and ($curTok -ne $expectTok)', ps1_text)
        self.assertIn('$tokConflict2 = ($expectTok -ne "") -and ($curTok2 -ne $expectTok)', ps1_text)
        self.assertNotIn('($curTok -ne "") -and ($curTok -ne $expectTok)', ps1_text)
        self.assertNotIn('($curTok2 -ne "") -and ($curTok2 -ne $expectTok)', ps1_text)

        # Shared cross-language title token contract corpus
        cases: list[tuple[str, str]] = [
            ("", ""),
            ("Ubuntu", ""),
            ("Windows PowerShell", ""),
            ("feat/codex-config", ""),
            ("agy \u00b7 ~/repo [#a1b2c3d4]", "a1b2c3d4"),
            ("claude [\u00a7AbCdEf] [@devbox]", "abcdef"),
            ("codex [#1a2b3c][@devbox]", "1a2b3c"),
            ("claude [claude:#DEADBEEF]", "deadbeef"),
            ("old [#11112222] new [\u00a7334455]", "334455"),
            ("codex 12345678-1234-1234-1234-1234567890ab", "12345678-1234-1234-1234-1234567890ab"),
            ("term [#12345]", ""),
            ("term [\u00a7a1b2c3]tail", ""),
            ("term [@12345678-1234-1234-1234-1234567890ab]", ""),
        ]
        for title, expected in cases:
            hint = parse_window_title_hint(title)
            got = hint.session_token if hint is not None else ""
            self.assertEqual(got, expected, msg=f"Python parse_window_title_hint mismatch for {title!r}")

        # Verify WslPlatform.deliver_text passes short [§abcdef] token and blocks switch to plain shell
        plat = WslPlatform(Config(), env={"PATH": "/usr/bin"})
        try:
            sent_reqs: list[dict[str, Any]] = []
            cur_title_box = ["claude [\u00a7AbCdEf] — busy"]

            def _sim_bridge_paste(
                payload: dict[str, Any], timeout_sec: float = 5.0, *, allow_retry: bool = False
            ) -> dict[str, Any]:
                del timeout_sec, allow_retry
                sent_reqs.append(dict(payload))
                exp_tok = str(payload.get("expect_session_token") or "").lower()
                cur_hint = parse_window_title_hint(cur_title_box[0])
                cur_tok = cur_hint.session_token if cur_hint is not None else ""
                tok_conflict = bool(exp_tok) and (cur_tok != exp_tok)
                if tok_conflict:
                    return {
                        "ok": True,
                        "pasted": False,
                        "clipboard": True,
                        "reason": "focus_changed",
                        "fg_process": "WindowsTerminal.exe",
                    }
                return {
                    "ok": True,
                    "pasted": True,
                    "clipboard": True,
                    "reason": "ok",
                    "fg_process": "WindowsTerminal.exe",
                }

            plat._shim_rpc = _sim_bridge_paste  # type: ignore[method-assign]
            origin = ForegroundWindow(700, "WindowsTerminal.exe", "claude [\u00a7AbCdEf] [@devbox]")
            out_same = plat.deliver_text("同标签继续贴", seq_id=1, origin_window=origin)
            self.assertTrue(out_same.pasted)
            self.assertEqual(sent_reqs[-1].get("expect_session_token"), "abcdef")

            # Switch to plain shell tab (no session token): must refuse auto-paste & copy only
            cur_title_box[0] = "Ubuntu"
            out_plain = plat.deliver_text("切普通shell只复制", seq_id=2, origin_window=origin)
            self.assertFalse(out_plain.pasted)
            self.assertTrue(out_plain.clipboard)
            self.assertEqual(out_plain.reason, "focus_changed")
            self.assertEqual(sent_reqs[-1].get("expect_session_token"), "abcdef")
        finally:
            plat.close()

        # When powershell.exe is present (WSL2 host), also execute the real PowerShell Get-TitleSessionToken
        ps_exe = resolve_powershell_exe(os.environ)
        if ps_exe:
            start_idx = ps1_text.index("function Get-TitleSessionToken([string]$title) {")
            end_idx = ps1_text.index("\ntry {", start_idx)
            fn_src = ps1_text[start_idx:end_idx]
            with tempfile.TemporaryDirectory() as td:
                harness = Path(td) / "check_tokens.ps1"
                harness.write_text(
                    fn_src
                    + "\n$raw = [Console]::In.ReadToEnd()\n"
                    + "$items = ConvertFrom-Json $raw\n"
                    + "$out = @()\n"
                    + "foreach ($it in $items) { $out += (Get-TitleSessionToken ([string]$it)) }\n"
                    + "ConvertTo-Json -InputObject $out -Compress\n",
                    encoding="ascii",
                )
                payload_json = json.dumps([c[0] for c in cases], ensure_ascii=True)
                cp = subprocess.run(
                    [ps_exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", to_windows_path(harness)],
                    input=payload_json,
                    text=True,
                    encoding="utf-8",
                    capture_output=True,
                    timeout=15.0,
                    check=False,
                )
                self.assertEqual(cp.returncode, 0, msg=cp.stderr)
                ps_results = json.loads(cp.stdout.strip())
                self.assertEqual(
                    ps_results,
                    [c[1] for c in cases],
                    msg="PowerShell Get-TitleSessionToken diverged from Python parse_window_title_hint",
                )

    # ------------------------------------------------------------------
    # Fix 3: WSL rec_start timeout sends discard-stop with original rec_gen + C# rec_gen guard
    # ------------------------------------------------------------------
    def test_03_wsl_rec_start_timeout_sends_discard_stop_and_rec_gen_guard(self) -> None:
        # 1) Verify C# StopRecording checks rec_gen BEFORE clearing _recSess and CancelRecording preserves recGen
        ps1_text = read_win_shim_source()
        self.assertIn(
            "if (sess != null && recGen > 0L && sess.RecGen > 0L && sess.RecGen != recGen)",
            ps1_text,
        )
        self.assertIn("ReleaseStoppedRecording(recGen);", ps1_text)

        # 2) Verify Python WSLPlatform sends fire-and-forget discard-stop with original rec_gen on rec_start timeout
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cmd_log = tmp / "shim_cmds.jsonl"
            fake_shim_code = (
                "import json, sys\n"
                f"log_path = {str(cmd_log)!r}\n"
                "print(json.dumps({'event': 'ready'}), flush=True)\n"
                "for line in sys.stdin:\n"
                "    s = line.strip()\n"
                "    if not s:\n"
                "        continue\n"
                "    with open(log_path, 'a', encoding='utf-8') as f:\n"
                "        f.write(s + '\\n')\n"
                "    msg = json.loads(s)\n"
                "    if msg.get('cmd') == 'quit':\n"
                "        break\n"
                "    # Intentionally do NOT reply to rec_start so start_recording hits timeout\n"
            )
            cfg = Config()
            cfg.audio.wsl_source = "wasapi"
            plat = WslPlatform(
                cfg,
                env={"PATH": "/usr/bin"},
                shim_cmd_factory=lambda: [sys.executable, "-u", "-c", fake_shim_code],
            )
            try:
                orig_rpc = plat._shim_rpc

                def _fast_timeout_rpc(
                    payload: dict[str, Any],
                    timeout_sec: float = 5.0,
                    *,
                    allow_retry: bool = True,
                    target_writer: Any = None,
                ) -> dict[str, Any] | None:
                    eff_to = 0.08 if payload.get("cmd") == "rec_start" else timeout_sec
                    return orig_rpc(
                        payload,
                        timeout_sec=eff_to,
                        allow_retry=allow_retry,
                        target_writer=target_writer,
                    )

                plat._shim_rpc = _fast_timeout_rpc  # type: ignore[method-assign]
                with self.assertRaises(queue.Empty):
                    plat.start_recording(tmp / "rt", seq_id=7)

                deadline = time.monotonic() + 2.0
                cmds: list[dict[str, Any]] = []
                while time.monotonic() < deadline:
                    if cmd_log.is_file():
                        lines = [ln for ln in cmd_log.read_text(encoding="utf-8").splitlines() if ln.strip()]
                        cmds = [json.loads(ln) for ln in lines]
                        if len(cmds) >= 2:
                            break
                    time.sleep(0.02)

                self.assertGreaterEqual(len(cmds), 2, msg=f"Expected rec_start + discard rec_stop, got {cmds}")
                rec_start_cmd = cmds[0]
                rec_stop_cmd = cmds[1]
                self.assertEqual(rec_start_cmd.get("cmd"), "rec_start")
                self.assertEqual(rec_stop_cmd.get("cmd"), "rec_stop")
                self.assertIs(rec_stop_cmd.get("discard"), True)
                self.assertEqual(rec_stop_cmd.get("rec_gen"), rec_start_cmd.get("rec_gen"))
                self.assertGreater(int(rec_start_cmd.get("rec_gen") or 0), 0)
            finally:
                plat.close()

            # 3) Also verify wsl_source="auto" fallback to PulseAudio still sends discard-stop with original rec_gen first
            cmd_log_auto = tmp / "shim_cmds_auto.jsonl"
            fake_shim_auto = fake_shim_code.replace(repr(str(cmd_log)), repr(str(cmd_log_auto)))
            cfg_auto = Config()
            cfg_auto.audio.wsl_source = "auto"
            plat_auto = WslPlatform(
                cfg_auto,
                env={"PATH": "/usr/bin"},
                shim_cmd_factory=lambda: [sys.executable, "-u", "-c", fake_shim_auto],
            )
            plat_auto._use_winmm = True
            try:
                orig_rpc_auto = plat_auto._shim_rpc

                def _fast_timeout_rpc_auto(
                    payload: dict[str, Any],
                    timeout_sec: float = 5.0,
                    *,
                    allow_retry: bool = True,
                    target_writer: Any = None,
                ) -> dict[str, Any] | None:
                    eff_to = 0.08 if payload.get("cmd") == "rec_start" else timeout_sec
                    return orig_rpc_auto(
                        payload,
                        timeout_sec=eff_to,
                        allow_retry=allow_retry,
                        target_writer=target_writer,
                    )

                plat_auto._shim_rpc = _fast_timeout_rpc_auto  # type: ignore[method-assign]
                with mock.patch.object(plat_auto._ff_rec, "start") as m_ff_start:
                    plat_auto.start_recording(tmp / "rt_auto", seq_id=8)
                self.assertEqual(m_ff_start.call_count, 1)
                self.assertEqual(plat_auto._rec_mode, "pulse")

                deadline_a = time.monotonic() + 2.0
                cmds_auto: list[dict[str, Any]] = []
                while time.monotonic() < deadline_a:
                    if cmd_log_auto.is_file():
                        lines_a = [ln for ln in cmd_log_auto.read_text(encoding="utf-8").splitlines() if ln.strip()]
                        cmds_auto = [json.loads(ln) for ln in lines_a]
                        if len(cmds_auto) >= 2:
                            break
                    time.sleep(0.02)
                self.assertGreaterEqual(len(cmds_auto), 2, msg=f"Expected rec_start + discard rec_stop, got {cmds_auto}")
                self.assertEqual(cmds_auto[0].get("cmd"), "rec_start")
                self.assertEqual(cmds_auto[1].get("cmd"), "rec_stop")
                self.assertIs(cmds_auto[1].get("discard"), True)
                self.assertEqual(cmds_auto[1].get("rec_gen"), cmds_auto[0].get("rec_gen"))
            finally:
                plat_auto.close()

    # ------------------------------------------------------------------
    # Fix 4: macOS PTT shim crash EOF auto-stops matching PTT without harming tap_toggle or other shims
    # ------------------------------------------------------------------
    def test_04_macos_ptt_shim_eof_auto_stops_only_matching_ptt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            # Fake bridge script: emits ready, emits one event specified by argv[1], then exits on stdin 'crash'
            fake_bridge_code = (
                "import json, sys\n"
                "ev = sys.argv[1]\n"
                "print(json.dumps({'event': 'ready'}), flush=True)\n"
                "print(json.dumps({'event': ev, 'hwnd': 10, 'process': 'Ghostty', 'bundle_id': 'com.mitchellh.ghostty', 'title': 'Ghostty'}), flush=True)\n"
                "for line in sys.stdin:\n"
                "    if 'crash' in line:\n"
                "        sys.exit(1)\n"
            )
            cfg = Config()
            cfg.hotkey.trigger_mode = "push_to_talk"
            cfg.hotkey.debounce_ms = 0
            cfg.audio.beep = False

            # Case A: PTT active on shim #1 -> shim #1 crashes (EOF) -> daemon auto-stops PTT cleanly
            ev_arg = ["ptt_start"]
            spawn_count = [0]

            def _factory() -> list[str]:
                spawn_count[0] += 1
                if spawn_count[0] == 1:
                    return [sys.executable, "-u", "-c", fake_bridge_code, ev_arg[0]]
                # Subsequent respawned shim replies to RPCs like real mac_shim without emitting ptt_stop
                return [
                    sys.executable,
                    "-u",
                    "-c",
                    (
                        "import json, sys\n"
                        "print(json.dumps({'event': 'ready'}), flush=True)\n"
                        "for ln in sys.stdin:\n"
                        "    if not ln.strip(): continue\n"
                        "    req = json.loads(ln)\n"
                        "    if req.get('cmd') == 'quit': break\n"
                        "    print(json.dumps({'type': 'reply', 'reply': req.get('cmd'), 'seq_id': req.get('seq_id', 0), 'ok': True}), flush=True)\n"
                    ),
                ]

            plat = MacOSPlatform(
                cfg,
                env={"HOME": str(tmp), "PATH": "/usr/bin"},
                cache_dir=tmp / "cache",
                shim_cmd_factory=_factory,
            )
            plat._shim_respawn_delays = (0.05,)
            start_evt = threading.Event()
            stop_evt = threading.Event()
            wav_bytes = _make_speech_wav()
            rec_calls = {"start": 0, "stop": 0}

            def _fake_start(rt: Path, *, loopback: bool = False, seq_id: int | None = None) -> None:
                del rt, loopback, seq_id
                rec_calls["start"] += 1
                start_evt.set()

            def _fake_stop(*, drain: bool = True) -> tuple[bytes, str]:
                del drain
                rec_calls["stop"] += 1
                stop_evt.set()
                return wav_bytes, "avfoundation"

            plat.start_recording = _fake_start  # type: ignore[method-assign]
            plat.stop_recording = _fake_stop  # type: ignore[method-assign]
            plat.resolve_foreground_window = lambda w=None: w or ForegroundWindow(10, "Ghostty", "Ghostty", GHOSTTY_BUNDLE_ID)  # type: ignore[method-assign]
            plat.deliver_text = lambda text, seq_id, target_hwnd=0, **kw: PasteOutcome(True, False, "ok", "Ghostty")  # type: ignore[method-assign]
            plat.beep = lambda kind: None  # type: ignore[method-assign]

            tracker = SessionTracker(home_dir=tmp, runtime_dir=tmp / "rt", proc_root=None, env={"HOME": str(tmp)})
            daemon = VoiceInputDaemon(
                cfg,
                env={"HOME": str(tmp)},
                runtime_dir=tmp / "rt",
                platform=plat,
                asr_provider=_StaticAsr(),
                refiner=_NoopRefiner(),
                tracker=tracker,
            )
            try:
                plat.start_event_listener(daemon._on_platform_event)
                self.assertTrue(start_evt.wait(timeout=3.0), "PTT recording did not start from shim #1")
                self.assertTrue(daemon._recording_active, "PTT recording did not enter active state")
                self.assertEqual(rec_calls["start"], 1)

                # Crash shim #1 mid-PTT (no ptt_stop emitted!)
                proc1 = plat._shim
                self.assertIsNotNone(proc1)
                assert proc1 is not None and proc1.stdin is not None
                proc1.stdin.write("crash\n")
                proc1.stdin.flush()

                self.assertTrue(stop_evt.wait(timeout=3.0), "Daemon failed to auto-stop PTT after shim EOF")
                deadline2 = time.monotonic() + 3.0
                while time.monotonic() < deadline2 and daemon._recording_active:
                    time.sleep(0.02)
                self.assertFalse(daemon._recording_active)
                self.assertEqual(rec_calls["stop"], 1)

                # Case B: Non-matching shim_instance_id disconnect does NOT stop active PTT #2
                daemon._on_platform_event(
                    {
                        "event": "ptt_start",
                        "hwnd": 10,
                        "process": "Ghostty",
                        "bundle_id": GHOSTTY_BUNDLE_ID,
                        "title": "Ghostty",
                        "_shim_instance_id": 99,
                    }
                )
                self.assertTrue(daemon._recording_active)
                res_stale = daemon._handle_shim_disconnected({"event": "shim_disconnected", "_shim_instance_id": 98})
                self.assertEqual(res_stale.get("ignored"), "not_matching_ptt")
                self.assertTrue(daemon._recording_active, "Stale shim disconnect must not stop newer PTT")
                daemon.handle_trigger("ptt_cancel")
                self.assertFalse(daemon._recording_active)

                # Case C: tap_toggle recording is NOT interrupted by shim_disconnected
                daemon._on_platform_event(
                    {
                        "event": "toggle",
                        "hwnd": 10,
                        "process": "Ghostty",
                        "bundle_id": GHOSTTY_BUNDLE_ID,
                        "title": "Ghostty",
                        "_shim_instance_id": 100,
                    }
                )
                self.assertTrue(daemon._recording_active)
                res_toggle = daemon._handle_shim_disconnected({"event": "shim_disconnected", "_shim_instance_id": 100})
                self.assertEqual(res_toggle.get("ignored"), "not_matching_ptt")
                self.assertTrue(daemon._recording_active, "tap_toggle recording must not be stopped on shim EOF")
                daemon.handle_trigger("ptt_cancel")
            finally:
                daemon.close()

    # ------------------------------------------------------------------
    # Fix 5: Cancelled valid recording racing with stop/restart saves exactly once
    # ------------------------------------------------------------------
    def test_05_cancelled_recording_stash_vs_shutdown_saves_exactly_once(self) -> None:
        wav = _make_speech_wav()
        gate_ok = AudioGateResult(
            passed=True,
            reason="",
            wav_bytes=wav,
            duration_sec=0.7,
            voiced_sec=0.5,
            rms_dbfs=-20.0,
            gain_db=0.0,
        )

        # Interleaving A: shutdown sets _stop_evt, normal path acquires stash_lock FIRST and yields,
        # then shutdown path acquires stash_lock and MUST complete the sole save (never 0 calls).
        with tempfile.TemporaryDirectory() as td_a:
            rt_a = Path(td_a) / "rt"
            plat_a = _DummyPlatform(wav)
            tracker_a = SessionTracker(home_dir=Path(td_a), runtime_dir=rt_a, proc_root=None, env={"HOME": td_a})
            daemon_a = VoiceInputDaemon(
                Config(),
                env={"HOME": td_a},
                runtime_dir=rt_a,
                platform=plat_a,
                asr_provider=_StaticAsr(),
                refiner=_NoopRefiner(),
                tracker=tracker_a,
            )
            try:
                task_a = _QueuedUtterance(
                    seq_id=1,
                    raw_wav=wav,
                    audio_source="live",
                    origin_win=ForegroundWindow(1, "Ghostty", "Ghostty", GHOSTTY_BUNDLE_ID),
                )
                task_a.cancel_token.cancel("cancelled")
                st_a = _Stage1State(t_total0=time.monotonic(), gate=gate_ok, win=task_a.origin_win)
                slot_a = _CommitSlot(task=task_a, st=st_a)
                slot_a.done_evt.set()
                with daemon_a._queue_cv:
                    daemon_a._commit_queue.append(slot_a)

                save_calls_a: list[bytes] = []
                real_save = daemon_mod.save_failed_audio

                def _spy_save_a(wav_b: bytes, *args: Any, **kwargs: Any) -> Path | None:
                    save_calls_a.append(wav_b)
                    return real_save(wav_b, *args, **kwargs)

                normal_done = threading.Event()
                allow_normal = threading.Event()
                orig_stash = daemon_a._stash_task_wav_once

                def _normal_worker() -> None:
                    allow_normal.wait(timeout=2.0)
                    orig_stash(task_a, gate=gate_ok, snap=None, win=task_a.origin_win, allow_during_shutdown=False)
                    normal_done.set()

                th_norm = threading.Thread(target=_normal_worker, daemon=True)
                th_norm.start()

                # Force exact bug interleaving: _stop_evt becomes set inside _initiate_shutdown,
                # then normal path runs _stash_task_wav_once(allow_during_shutdown=False) BEFORE shutdown calls it!
                def _ordered_stash(
                    t: _QueuedUtterance,
                    *,
                    gate: AudioGateResult | None,
                    snap: Any,
                    win: ForegroundWindow,
                    workspace_cwd: str = "",
                    allow_during_shutdown: bool = False,
                ) -> str:
                    if allow_during_shutdown:
                        allow_normal.set()
                        self.assertTrue(normal_done.wait(timeout=2.0))
                    return orig_stash(
                        t,
                        gate=gate,
                        snap=snap,
                        win=win,
                        workspace_cwd=workspace_cwd,
                        allow_during_shutdown=allow_during_shutdown,
                    )

                with mock.patch.object(daemon_mod, "save_failed_audio", side_effect=_spy_save_a), mock.patch.object(
                    daemon_a, "_stash_task_wav_once", side_effect=_ordered_stash
                ):
                    daemon_a._initiate_shutdown()
                th_norm.join(timeout=2.0)

                self.assertEqual(len(save_calls_a), 1, "Cancelled recording must be stashed exactly once")
                self.assertTrue(task_a.saved_audio and Path(task_a.saved_audio).is_file())
            finally:
                daemon_a.close()

        # Interleaving B: normal path enters save_failed_audio BEFORE shutdown sets _stop_evt;
        # shutdown runs concurrently and MUST NOT save a second copy (still exactly 1 call).
        with tempfile.TemporaryDirectory() as td_b:
            rt_b = Path(td_b) / "rt"
            plat_b = _DummyPlatform(wav)
            tracker_b = SessionTracker(home_dir=Path(td_b), runtime_dir=rt_b, proc_root=None, env={"HOME": td_b})
            daemon_b = VoiceInputDaemon(
                Config(),
                env={"HOME": td_b},
                runtime_dir=rt_b,
                platform=plat_b,
                asr_provider=_StaticAsr(),
                refiner=_NoopRefiner(),
                tracker=tracker_b,
            )
            try:
                task_b = _QueuedUtterance(
                    seq_id=2,
                    raw_wav=wav,
                    audio_source="live",
                    origin_win=ForegroundWindow(1, "Ghostty", "Ghostty", GHOSTTY_BUNDLE_ID),
                )
                task_b.cancel_token.cancel("cancelled")
                st_b = _Stage1State(t_total0=time.monotonic(), gate=gate_ok, win=task_b.origin_win)
                slot_b = _CommitSlot(task=task_b, st=st_b)
                slot_b.done_evt.set()
                with daemon_b._queue_cv:
                    daemon_b._commit_queue.append(slot_b)

                in_save = threading.Event()
                release_save = threading.Event()
                save_calls_b: list[bytes] = []
                real_save = daemon_mod.save_failed_audio

                def _blocking_save_b(wav_b: bytes, *args: Any, **kwargs: Any) -> Path | None:
                    save_calls_b.append(wav_b)
                    in_save.set()
                    release_save.wait(timeout=2.0)
                    return real_save(wav_b, *args, **kwargs)

                with mock.patch.object(daemon_mod, "save_failed_audio", side_effect=_blocking_save_b):
                    th_norm_b = threading.Thread(
                        target=lambda: daemon_b._stash_task_wav_once(
                            task_b, gate=gate_ok, snap=None, win=task_b.origin_win, allow_during_shutdown=False
                        ),
                        daemon=True,
                    )
                    th_norm_b.start()
                    self.assertTrue(in_save.wait(timeout=2.0))
                    th_shut_b = threading.Thread(target=daemon_b._initiate_shutdown, daemon=True)
                    th_shut_b.start()
                    time.sleep(0.05)
                    release_save.set()
                    th_norm_b.join(timeout=2.0)
                    th_shut_b.join(timeout=2.0)

                self.assertEqual(len(save_calls_b), 1, "Concurrent normal + shutdown stash must never double-save")
                self.assertTrue(task_b.saved_audio and Path(task_b.saved_audio).is_file())
            finally:
                daemon_b.close()

    # ------------------------------------------------------------------
    # Fix 6: SSH stale prefetch callback must not overwrite newer clean_turns / ETag
    # ------------------------------------------------------------------
    def test_06_ssh_stale_prefetch_callback_does_not_overwrite_newer_turns_or_etag(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            host = RemoteHostConfig(name="devbox", ssh_target="devbox")
            rcfg = RemoteConfig(hosts=(host,), timeout_ms=400)
            mgr = RemoteSshManager(rcfg, env={"HOME": str(tmp)}, runtime_dir=tmp / "rt")
            tracker = SessionTracker(
                home_dir=tmp,
                runtime_dir=tmp / "rt",
                proc_root=None,
                env={"HOME": str(tmp)},
                remote_cfg=rcfg,
                remote_manager=mgr,
            )
            try:
                sid = "11223344-5566-7788-99aa-bbccddeeff00"
                skey = f"remote:devbox:claude:{sid}"
                old_cb_entered = threading.Event()
                release_old_cb = threading.Event()
                new_cb_done = threading.Event()
                old_cb_done = threading.Event()

                def _fake_probe(
                    hcfg: RemoteHostConfig,
                    req_dict: dict[str, Any],
                    *,
                    abort_evt: threading.Event | None = None,
                    timeout_sec: float = 5.0,
                ) -> tuple[dict[str, Any] | None, str, bool, bool]:
                    del hcfg, abort_evt, timeout_sec
                    if req_dict.get("token"):
                        return (
                            {
                                "ok": True,
                                "cli": "claude",
                                "session_id": sid,
                                "cwd": "/srv/app",
                                "etag": "etag-new",
                                "turns": [("user", "turn 1"), ("assistant", "reply 1"), ("user", "turn 2")],
                                "matched_by": "title_tag",
                            },
                            "",
                            False,
                            False,
                        )
                    return (
                        {
                            "ok": True,
                            "cli": "claude",
                            "session_id": sid,
                            "cwd": "/srv/app",
                            "etag": "etag-old",
                            "turns": [("user", "turn 1")],
                            "matched_by": "cwd",
                        },
                        "",
                        False,
                        False,
                    )

                mgr._run_ssh_probe_once = _fake_probe  # type: ignore[method-assign]
                base_cb = tracker._make_remote_result_cb(tracker.context_max_chars)

                def _old_cb(hcfg: RemoteHostConfig, resp: dict[str, Any]) -> None:
                    old_cb_entered.set()
                    release_old_cb.wait(timeout=3.0)
                    base_cb(hcfg, resp)
                    old_cb_done.set()

                def _new_cb(hcfg: RemoteHostConfig, resp: dict[str, Any]) -> None:
                    base_cb(hcfg, resp)
                    new_cb_done.set()

                # 1) Old probe (keyed by cwd) finishes SSH probe, releases manager lock, pauses right before callback
                self.assertTrue(
                    mgr.start_prefetch(
                        host,
                        {"v": 1, "token": "", "cwd_hint": "/srv/app"},
                        cache_key=("devbox", "", "/srv/app", "claude", tracker.context_max_chars),
                        seq_id=1,
                        on_result=_old_cb,
                    )
                )
                self.assertTrue(old_cb_entered.wait(timeout=3.0))

                # 2) New probe (keyed by title token, different cache_key for same remote session) finishes & applies first
                self.assertTrue(
                    mgr.start_prefetch(
                        host,
                        {"v": 1, "token": sid[:8], "cwd_hint": ""},
                        cache_key=("devbox", f"tok:claude:{sid[:8]}", "", "", tracker.context_max_chars),
                        seq_id=2,
                        on_result=_new_cb,
                    )
                )
                self.assertTrue(new_cb_done.wait(timeout=3.0))

                with tracker._lock:
                    st_mid = tracker._states[skey]
                    self.assertEqual(st_mid.remote_etag, "etag-new")
                    self.assertEqual(len(st_mid.clean_turns), 3)
                    self.assertEqual(st_mid.remote_applied_gen, 2)

                # 3) Release paused old callback (gen=1) -> must be dropped without regressing ETag or clean_turns!
                release_old_cb.set()
                self.assertTrue(old_cb_done.wait(timeout=3.0))

                with tracker._lock:
                    st_final = tracker._states[skey]
                    self.assertEqual(st_final.remote_etag, "etag-new")
                    self.assertEqual(
                        st_final.clean_turns,
                        [("user", "turn 1"), ("assistant", "reply 1"), ("user", "turn 2")],
                    )
                    self.assertEqual(st_final.remote_applied_gen, 2)

                # 4) Synchronous _apply_remote_probe_response path with stale _probe_gen=1 also preserves "etag-new"
                stale_res = tracker._apply_remote_probe_response(
                    host,
                    {
                        "ok": True,
                        "cli": "claude",
                        "session_id": sid,
                        "cwd": "/srv/app",
                        "etag": "etag-old",
                        "turns": [("user", "turn 1")],
                        "_probe_gen": 1,
                    },
                )
                self.assertIsNotNone(stale_res)
                assert stale_res is not None
                self.assertEqual(stale_res.remote_etag, "etag-new")
                self.assertEqual(len(stale_res.clean_turns), 3)
            finally:
                tracker.close()

    # ------------------------------------------------------------------
    # Follow-Up 1 [P1]: Ghostty pane switch during start_recording marks origin drifted
    # ------------------------------------------------------------------
    def test_07_ghostty_pane_switch_during_start_recording_marks_origin_drifted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            rt_dir = tmp / "rt"
            rt_dir.mkdir(parents=True)
            sock_path = str(tmp / "tmux-501" / "default")
            repo_a = tmp / "repo-a"
            repo_b = tmp / "repo-b"
            repo_a.mkdir()
            repo_b.mkdir()

            sess_a_id = "aaaaaaaa-1111-2222-3333-444444444444"
            sess_b_id = "bbbbbbbb-1111-2222-3333-444444444444"
            jsonl_a = tmp / f"{sess_a_id}.jsonl"
            jsonl_b = tmp / f"{sess_b_id}.jsonl"
            jsonl_a.write_text(
                json.dumps({"type": "user", "message": {"role": "user", "content": "pane A ctx"}}) + "\n",
                encoding="utf-8",
            )
            jsonl_b.write_text(
                json.dumps({"type": "user", "message": {"role": "user", "content": "pane B ctx"}}) + "\n",
                encoding="utf-8",
            )
            write_session_registry_entry(
                rt_dir,
                cli="claude",
                session_id=sess_a_id,
                jsonl_path=str(jsonl_a),
                cwd=str(repo_a),
                tmux_pane="%1",
                tmux_socket=sock_path,
                tty="/dev/ttys011",
            )
            write_session_registry_entry(
                rt_dir,
                cli="codex",
                session_id=sess_b_id,
                jsonl_path=str(jsonl_b),
                cwd=str(repo_b),
                tmux_pane="%2",
                tmux_socket=sock_path,
                tty="/dev/ttys012",
            )

            def _make_snap(active_pane_id: str) -> TmuxServerSnapshot:
                return TmuxServerSnapshot(
                    clients=(
                        TmuxClientInfo(
                            session_id="$0",
                            window_id="@0",
                            pane_id=active_pane_id,
                            client_tty="/dev/ttys005",
                            client_pid=5000,
                            socket_path=sock_path,
                        ),
                    ),
                    panes=(
                        TmuxPaneInfo(
                            session_id="$0",
                            window_id="@0",
                            pane_id="%1",
                            pane_pid=5001,
                            pane_tty="/dev/ttys011",
                            current_path=str(repo_a),
                            pane_title="claude · repo-a [#aaaaaaaa]",
                            pane_active=(active_pane_id == "%1"),
                            window_active=True,
                            socket_path=sock_path,
                        ),
                        TmuxPaneInfo(
                            session_id="$0",
                            window_id="@0",
                            pane_id="%2",
                            pane_pid=5002,
                            pane_tty="/dev/ttys012",
                            current_path=str(repo_b),
                            pane_title="codex · repo-b [#bbbbbbbb]",
                            pane_active=(active_pane_id == "%2"),
                            window_active=True,
                            socket_path=sock_path,
                        ),
                    ),
                )

            snap_box = [_make_snap("%1")]
            ghostty_box = [("surf-1", "claude · repo-a [#aaaaaaaa]", "/dev/ttys005", str(repo_a), 4200)]

            cfg = Config()
            cfg.audio.beep = False
            cfg.hotkey.debounce_ms = 0
            plat = MacOSPlatform(
                cfg,
                env={"HOME": str(home), "PATH": "/usr/bin:/opt/homebrew/bin"},
                cache_dir=tmp / "cache",
                tmux_probe_fn=lambda: snap_box[0],
            )
            tracker = SessionTracker(
                home_dir=home,
                runtime_dir=rt_dir,
                proc_root=None,
                env={"HOME": str(home)},
                tmux_probe_fn=lambda: snap_box[0],
            )
            wav_bytes = _make_speech_wav()
            copied_texts: list[str] = []
            tmux_deliveries: list[tuple[str, str]] = []
            ghostty_direct_calls: list[tuple[str, str]] = []

            start_switched = threading.Event()

            def _start_rec_with_mid_switch(rt: Path, *, loopback: bool = False, seq_id: int | None = None) -> None:
                del rt, loopback, seq_id
                # User switches tmux pane %1 -> %2 in the SAME Ghostty surface before start_recording returns!
                snap_box[0] = _make_snap("%2")
                ghostty_box[0] = ("surf-1", "codex · repo-b [#bbbbbbbb]", "/dev/ttys005", str(repo_b), 4200)
                start_switched.set()

            plat.start_recording = _start_rec_with_mid_switch  # type: ignore[method-assign]
            plat.stop_recording = lambda *, drain=True: (wav_bytes, "avfoundation")  # type: ignore[method-assign]
            plat.hud = lambda *a, **kw: None  # type: ignore[method-assign]
            plat.beep = lambda kind: None  # type: ignore[method-assign]

            daemon = VoiceInputDaemon(
                cfg,
                env={"HOME": str(home)},
                runtime_dir=rt_dir,
                platform=plat,
                asr_provider=_StaticAsr(),
                refiner=_NoopRefiner(),
                tracker=tracker,
            )
            try:
                def _query_ghostty_after_switch() -> tuple[str, str, str, str, int]:
                    # Wait until mid-start switch happened to verify title conflict catches B even when concurrent
                    start_switched.wait(timeout=2.0)
                    return ghostty_box[0]

                with mock.patch.object(
                    plat, "_which", side_effect=lambda name: f"/usr/bin/{name}" if name in ("osascript", "pbcopy") else None
                ), mock.patch.object(
                    plat, "_query_ghostty_terminal", side_effect=_query_ghostty_after_switch
                ), mock.patch.object(
                    plat, "_resolve_tmux_exe", return_value="/opt/homebrew/bin/tmux"
                ), mock.patch.object(
                    plat,
                    "_deliver_via_tmux_buffer",
                    side_effect=lambda pfx, txt, *, seq_id, target_pane="", tmux_env=None: (
                        tmux_deliveries.append((target_pane, txt)),
                        True,
                    )[1],
                ), mock.patch.object(
                    plat, "_copy_to_clipboard", side_effect=lambda t: (copied_texts.append(t), True)[1]
                ), mock.patch.object(
                    plat,
                    "_run_osascript",
                    side_effect=lambda args, *, timeout=2.5: (
                        ghostty_direct_calls.append((args[3], args[4])) if len(args) >= 5 else None,
                        subprocess.CompletedProcess(["osascript"], 0, "ok\n", ""),
                    )[1],
                ), mock.patch.object(
                    tracker, "sync_session", wraps=tracker.sync_session
                ) as spy_sync, mock.patch.object(
                    tracker, "build_workspace_hotwords", wraps=tracker.build_workspace_hotwords
                ) as spy_ws_hw:
                    # Enter from hotkey path with pane A trigger window
                    daemon._on_platform_event(
                        {
                            "event": "ptt_start",
                            "hwnd": 100,
                            "process": "Ghostty",
                            "bundle_id": GHOSTTY_BUNDLE_ID,
                            "title": "claude · repo-a [#aaaaaaaa]",
                            "pid": 4100,
                            "ax_trusted": True,
                            "_shim_instance_id": 1,
                        }
                    )
                    self.assertTrue(daemon._recording_active)
                    # Must NOT adopt B (%2 / codex title) as origin; must mark origin_drifted!
                    self.assertTrue(daemon._origin_win.origin_drifted)
                    self.assertNotEqual(daemon._origin_win.tmux_pane, "%2")
                    self.assertNotIn("bbbbbbbb", daemon._origin_win.title)

                    # Finish recording -> Stage 1 must not borrow B context, delivery must copy only (never paste into B)
                    daemon._on_platform_event(
                        {
                            "event": "ptt_stop",
                            "hwnd": 100,
                            "process": "Ghostty",
                            "bundle_id": GHOSTTY_BUNDLE_ID,
                            "title": "codex · repo-b [#bbbbbbbb]",
                            "pid": 4100,
                            "_shim_instance_id": 1,
                        }
                    )
                    deadline = time.monotonic() + 3.0
                    while time.monotonic() < deadline and not daemon.last_result:
                        time.sleep(0.02)
                    self.assertTrue(daemon.last_result)
                    self.assertEqual(daemon.last_result.get("session_key"), "")
                    self.assertEqual(spy_sync.call_count, 0)
                    self.assertEqual(spy_ws_hw.call_count, 0)
                    self.assertFalse(daemon.last_result.get("pasted"))
                    self.assertTrue(daemon.last_result.get("clipboard"))
                    self.assertEqual(daemon.last_result.get("paste_reason"), "focus_changed")
                    self.assertEqual(tmux_deliveries, [])
                    self.assertEqual(ghostty_direct_calls, [])
                    self.assertEqual(copied_texts, ["测试语音输入"])
            finally:
                daemon.close()

    # ------------------------------------------------------------------
    # Follow-Up 2 [P1]: Known pane drift blocks remote [@host] AND local cwd workspace/cold-start fallback
    # ------------------------------------------------------------------
    def test_08_known_pane_drift_blocks_remote_host_and_local_cwd_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            rt_dir = tmp / "rt"
            rt_dir.mkdir(parents=True)
            repo_dir = tmp / "local-repo"
            repo_dir.mkdir()
            (repo_dir / "main.py").write_text("print('hi')\n", encoding="utf-8")
            sock_path = str(tmp / "tmux-501" / "default")

            # Live tmux snapshot shows client on /dev/ttys005 is now on pane %2 (drifted from %1)
            drifted_snap = TmuxServerSnapshot(
                clients=(
                    TmuxClientInfo(
                        session_id="$0",
                        window_id="@0",
                        pane_id="%2",
                        client_tty="/dev/ttys005",
                        client_pid=5000,
                        socket_path=sock_path,
                    ),
                ),
                panes=(
                    TmuxPaneInfo(
                        session_id="$0",
                        window_id="@0",
                        pane_id="%1",
                        pane_pid=5001,
                        pane_tty="/dev/ttys011",
                        current_path=str(repo_dir),
                        pane_title="claude [@devbox]",
                        pane_active=False,
                        window_active=True,
                        socket_path=sock_path,
                    ),
                    TmuxPaneInfo(
                        session_id="$0",
                        window_id="@0",
                        pane_id="%2",
                        pane_pid=5002,
                        pane_tty="/dev/ttys012",
                        current_path=str(repo_dir),
                        pane_title="zsh",
                        pane_active=True,
                        window_active=True,
                        socket_path=sock_path,
                    ),
                ),
            )
            host = RemoteHostConfig(name="devbox", ssh_target="devbox")
            rcfg = RemoteConfig(hosts=(host,), timeout_ms=400)
            mgr = RemoteSshManager(rcfg, env={"HOME": str(home)}, runtime_dir=rt_dir)
            tracker = SessionTracker(
                home_dir=home,
                runtime_dir=rt_dir,
                proc_root=None,
                env={"HOME": str(home)},
                tmux_probe_fn=lambda: drifted_snap,
                remote_cfg=rcfg,
                remote_manager=mgr,
            )
            cfg = Config(remote=rcfg)
            plat = _DummyPlatform()
            daemon = VoiceInputDaemon(
                cfg,
                env={"HOME": str(home)},
                runtime_dir=rt_dir,
                platform=plat,
                asr_provider=_StaticAsr(),
                refiner=_NoopRefiner(),
                tracker=tracker,
            )
            try:
                with mock.patch.object(
                    mgr, "start_prefetch", wraps=mgr.start_prefetch
                ) as spy_prefetch, mock.patch.object(
                    mgr, "fetch_with_budget", wraps=mgr.fetch_with_budget
                ) as spy_fetch, mock.patch.object(
                    mgr, "_run_ssh_probe_once"
                ) as spy_ssh, mock.patch.object(
                    tracker, "build_workspace_hotwords", wraps=tracker.build_workspace_hotwords
                ) as spy_ws_hw, mock.patch.object(
                    tracker, "get_workspace_prior_hotwords", wraps=tracker.get_workspace_prior_hotwords
                ) as spy_prior_hw:
                    # Path A: whitelisted [@devbox] title with drifted origin tmux_pane="%1"
                    win_remote = ForegroundWindow(
                        hwnd=100,
                        process="Ghostty",
                        title="claude · app [@devbox]",
                        bundle_id=GHOSTTY_BUNDLE_ID,
                        surface_id="surf-1",
                        tty="/dev/ttys005",
                        cwd=str(repo_dir),
                        pid=4200,
                        tmux_socket=sock_path,
                        tmux_pane="%1",
                    )
                    self.assertFalse(
                        tracker.prefetch_remote_session(
                            win_remote.title,
                            window_tty=win_remote.tty,
                            window_cwd=win_remote.cwd,
                            window_pid=win_remote.pid,
                            window_process=win_remote.process,
                            bundle_id=win_remote.bundle_id,
                            tmux_socket=win_remote.tmux_socket,
                            tmux_pane=win_remote.tmux_pane,
                            seq_id=1,
                        )
                    )
                    st_rem = _Stage1State(t_total0=time.monotonic())
                    daemon_mod._execute_stage1(
                        st_rem,
                        _make_speech_wav(),
                        cfg=cfg,
                        env={"HOME": str(home)},
                        tracker=tracker,
                        eff_asr=_StaticAsr(),
                        eff_refiner=_NoopRefiner(),
                        platform=plat,
                        origin_window=win_remote,
                        window_title_override=None,
                        audio_source="live",
                        explicit_mode="auto",
                        seq_id=1,
                        max_chunk_sec=None,
                        is_current_seq=None,
                        is_cancelled=lambda: False,
                        cancel_token=None,
                        cancel_check=lambda: False,
                        cancel_reason_fn=lambda: "",
                        archive_sink=None,
                    )
                    self.assertIsNone(st_rem.snap)
                    self.assertTrue(tracker.last_sync_target_drifted)
                    self.assertEqual(st_rem.workspace_cwd, "")
                    self.assertEqual(spy_prefetch.call_count, 0)
                    self.assertEqual(spy_fetch.call_count, 0)
                    self.assertEqual(spy_ssh.call_count, 0)
                    self.assertEqual(spy_ws_hw.call_count, 0)
                    self.assertEqual(spy_prior_hw.call_count, 0)

                    # Path B: ordinary local title with drifted origin tmux_pane="%1" & valid local cwd
                    win_local = ForegroundWindow(
                        hwnd=100,
                        process="Ghostty",
                        title="Ghostty",
                        bundle_id=GHOSTTY_BUNDLE_ID,
                        surface_id="surf-1",
                        tty="/dev/ttys005",
                        cwd=str(repo_dir),
                        pid=4200,
                        tmux_socket=sock_path,
                        tmux_pane="%1",
                    )
                    st_loc = _Stage1State(t_total0=time.monotonic())
                    daemon_mod._execute_stage1(
                        st_loc,
                        _make_speech_wav(),
                        cfg=cfg,
                        env={"HOME": str(home)},
                        tracker=tracker,
                        eff_asr=_StaticAsr(),
                        eff_refiner=_NoopRefiner(),
                        platform=plat,
                        origin_window=win_local,
                        window_title_override=None,
                        audio_source="live",
                        explicit_mode="auto",
                        seq_id=2,
                        max_chunk_sec=None,
                        is_current_seq=None,
                        is_cancelled=lambda: False,
                        cancel_token=None,
                        cancel_check=lambda: False,
                        cancel_reason_fn=lambda: "",
                        archive_sink=None,
                    )
                    self.assertIsNone(st_loc.snap)
                    self.assertTrue(tracker.last_sync_target_drifted)
                    self.assertEqual(st_loc.workspace_cwd, "")
                    self.assertEqual(spy_ws_hw.call_count, 0)
                    self.assertEqual(spy_prior_hw.call_count, 0)
            finally:
                daemon.close()

    # ------------------------------------------------------------------
    # Follow-Up 3 [P1]: Hotkey cancel wins token while shutdown clears queue -> still stashes exactly once
    # ------------------------------------------------------------------
    def test_09_hotkey_cancel_wins_token_while_shutdown_clears_queue_still_stashes_once(self) -> None:
        wav = _make_speech_wav()
        gate_ok = AudioGateResult(
            passed=True,
            reason="",
            wav_bytes=wav,
            duration_sec=0.7,
            voiced_sec=0.5,
            rms_dbfs=-20.0,
            gain_db=0.0,
        )
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            plat = _DummyPlatform(wav)
            tracker = SessionTracker(home_dir=Path(td), runtime_dir=rt, proc_root=None, env={"HOME": td})
            daemon = VoiceInputDaemon(
                Config(),
                env={"HOME": td},
                runtime_dir=rt,
                platform=plat,
                asr_provider=_StaticAsr(),
                refiner=_NoopRefiner(),
                tracker=tracker,
            )
            try:
                task = _QueuedUtterance(
                    seq_id=1,
                    raw_wav=wav,
                    audio_source="live",
                    origin_win=ForegroundWindow(1, "Ghostty", "Ghostty", GHOSTTY_BUNDLE_ID),
                )
                st = _Stage1State(t_total0=time.monotonic(), gate=gate_ok, win=task.origin_win)
                slot = _CommitSlot(task=task, st=st)
                slot.done_evt.set()
                with daemon._queue_cv:
                    daemon._commit_queue.append(slot)

                cancel_selected = threading.Event()
                shutdown_cleared_queue = threading.Event()
                cancel_won_token = threading.Event()

                orig_cancel_many = daemon_mod.CancelToken.cancel_many
                save_calls: list[bytes] = []
                real_save = daemon_mod.save_failed_audio

                def _coordinated_cancel_many(tokens: Any, reason: str = "cancelled") -> Any:
                    if reason == "cancelled":
                        # Step 1: handle_cancel selected active candidate and exited _queue_cv; wait for shutdown to clear queue
                        cancel_selected.set()
                        self.assertTrue(shutdown_cleared_queue.wait(timeout=3.0))
                        res = orig_cancel_many(tokens, reason)
                        # Step 3: handle_cancel wins the token before shutdown runs cancel_many("shutdown")
                        cancel_won_token.set()
                        return res
                    if reason == "shutdown":
                        # Step 2: shutdown saw active token and cleared queues; let handle_cancel win token first
                        shutdown_cleared_queue.set()
                        self.assertTrue(cancel_won_token.wait(timeout=3.0))
                        return orig_cancel_many(tokens, reason)
                    return orig_cancel_many(tokens, reason)

                cancel_res_box: list[dict[str, Any]] = []
                with mock.patch.object(
                    daemon_mod.CancelToken, "cancel_many", side_effect=_coordinated_cancel_many
                ), mock.patch.object(
                    daemon_mod,
                    "save_failed_audio",
                    side_effect=lambda b, *a, **kw: (save_calls.append(b), real_save(b, *a, **kw))[1],
                ):
                    th_cancel = threading.Thread(
                        target=lambda: cancel_res_box.append(daemon.handle_cancel(seq_id=1)),
                        daemon=True,
                    )
                    th_cancel.start()
                    self.assertTrue(cancel_selected.wait(timeout=3.0))
                    daemon._initiate_shutdown()
                    th_cancel.join(timeout=3.0)

                self.assertEqual(len(cancel_res_box), 1)
                self.assertTrue(cancel_res_box[0].get("ok"))
                self.assertEqual(cancel_res_box[0].get("cancelled_ids"), [1])
                self.assertEqual(len(save_calls), 1, "Won user cancel racing with shutdown must stash exactly once")
                self.assertTrue(task.saved_audio and Path(task.saved_audio).is_file())
            finally:
                daemon.close()

    # ------------------------------------------------------------------
    # Follow-Up 4 [P1]: macOS PTT EOF stops recording BEFORE refreshing HUD
    # ------------------------------------------------------------------
    def test_10_macos_ptt_shim_eof_stops_recording_before_hud(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = Config()
            cfg.hotkey.trigger_mode = "push_to_talk"
            cfg.hotkey.debounce_ms = 0
            cfg.audio.beep = False
            plat = MacOSPlatform(cfg, env={"HOME": str(tmp), "PATH": "/usr/bin"}, cache_dir=tmp / "cache")
            tracker = SessionTracker(home_dir=tmp, runtime_dir=tmp / "rt", proc_root=None, env={"HOME": str(tmp)})
            wav_bytes = _make_speech_wav()
            call_order: list[str] = []
            hud_saw_stopped = []
            rec_running = [False]

            def _fake_start(rt: Path, *, loopback: bool = False, seq_id: int | None = None) -> None:
                del rt, loopback, seq_id
                rec_running[0] = True
                call_order.append("start_recording")

            def _fake_stop(*, drain: bool = True) -> tuple[bytes, str]:
                del drain
                rec_running[0] = False
                call_order.append("stop_recording")
                return wav_bytes, "avfoundation"

            def _spy_hud(state: str, *args: Any, **kwargs: Any) -> None:
                del args, kwargs
                call_order.append(f"hud:{state}")
                if state == "recognizing":
                    hud_saw_stopped.append(not rec_running[0])

            plat.start_recording = _fake_start  # type: ignore[method-assign]
            plat.stop_recording = _fake_stop  # type: ignore[method-assign]
            plat.hud = _spy_hud  # type: ignore[method-assign]
            plat.beep = lambda kind: None  # type: ignore[method-assign]
            plat.resolve_foreground_window = lambda w=None: w or ForegroundWindow(10, "Ghostty", "Ghostty", GHOSTTY_BUNDLE_ID)  # type: ignore[method-assign]
            plat.deliver_text = lambda text, seq_id, target_hwnd=0, **kw: PasteOutcome(True, False, "ok", "Ghostty")  # type: ignore[method-assign]

            daemon = VoiceInputDaemon(
                cfg,
                env={"HOME": str(tmp)},
                runtime_dir=tmp / "rt",
                platform=plat,
                asr_provider=_StaticAsr(),
                refiner=_NoopRefiner(),
                tracker=tracker,
            )
            try:
                daemon._on_platform_event(
                    {
                        "event": "ptt_start",
                        "hwnd": 10,
                        "process": "Ghostty",
                        "bundle_id": GHOSTTY_BUNDLE_ID,
                        "title": "Ghostty",
                        "_shim_instance_id": 7,
                    }
                )
                self.assertTrue(daemon._recording_active)
                call_order.clear()
                res = daemon._handle_shim_disconnected({"event": "shim_disconnected", "_shim_instance_id": 7})
                self.assertTrue(res.get("ok"))
                self.assertIn("stop_recording", call_order)
                self.assertIn("hud:recognizing", call_order)
                self.assertLess(
                    call_order.index("hud:recognizing"),
                    call_order.index("stop_recording"),
                    f"hud:recognizing must precede stop_recording on shim EOF, got {call_order}",
                )
                self.assertEqual(hud_saw_stopped, [False])
            finally:
                daemon.close()

    # ------------------------------------------------------------------
    # Follow-Up 5 [P2]: EOF racing with close() must not recreate/leak event worker thread
    # ------------------------------------------------------------------
    def test_11_shim_eof_racing_with_close_leaks_no_event_worker_thread(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            fake_bridge_code = (
                "import json, sys\n"
                "print(json.dumps({'event': 'ready'}), flush=True)\n"
                "print(json.dumps({'event': 'ptt_start', 'hwnd': 10, 'process': 'Ghostty', 'bundle_id': 'com.mitchellh.ghostty', 'title': 'Ghostty'}), flush=True)\n"
                "sys.stdin.read()\n"
            )
            plat = MacOSPlatform(
                Config(),
                env={"HOME": str(tmp), "PATH": "/usr/bin"},
                cache_dir=tmp / "cache",
                shim_cmd_factory=lambda: [sys.executable, "-u", "-c", fake_bridge_code],
            )
            got_ptt = threading.Event()
            in_reap = threading.Event()
            release_reap = threading.Event()
            orig_reap = plat._reap_owned_shim
            reap_blocked_once = [False]

            def _barrier_reap(proc: Any) -> None:
                if proc is not None and not reap_blocked_once[0]:
                    reap_blocked_once[0] = True
                    in_reap.set()
                    release_reap.wait(timeout=3.0)
                orig_reap(proc)

            plat._reap_owned_shim = _barrier_reap  # type: ignore[method-assign]
            try:
                plat.start_event_listener(lambda m: got_ptt.set() if m.get("event") == "ptt_start" else None)
                self.assertTrue(got_ptt.wait(timeout=3.0))
                proc = plat._shim
                self.assertIsNotNone(proc)
                assert proc is not None and proc.stdin is not None
                reader_th = getattr(proc, "_shim_reader_th", None)
                # Trigger EOF on fake bridge while reader pauses inside _reap_owned_shim
                proc.stdin.close()
                self.assertTrue(in_reap.wait(timeout=3.0))

                # Concurrently call close() while _shim_reader is in its finally block
                close_done = threading.Event()
                th_close = threading.Thread(target=lambda: (plat.close(), close_done.set()), daemon=True)
                th_close.start()
                time.sleep(0.05)
                release_reap.set()
                self.assertTrue(close_done.wait(timeout=3.0))
                if isinstance(reader_th, threading.Thread):
                    reader_th.join(timeout=2.0)

                ev_worker = plat._event_worker
                self.assertTrue(
                    ev_worker is None or not ev_worker.is_alive(),
                    "close() returned with a leaked live event worker thread",
                )
            finally:
                release_reap.set()
                plat.close()

    # ------------------------------------------------------------------
    # Follow-Up 6 [P2]: Shared SSH (host, cwd) file cache guarded by _probe_gen
    # ------------------------------------------------------------------
    def test_12_ssh_shared_host_cwd_files_cache_guarded_by_probe_gen(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            host = RemoteHostConfig(name="devbox", ssh_target="devbox")
            rcfg = RemoteConfig(hosts=(host,), timeout_ms=400)
            tracker = SessionTracker(
                home_dir=tmp,
                runtime_dir=tmp / "rt",
                proc_root=None,
                env={"HOME": str(tmp)},
                remote_cfg=rcfg,
            )
            try:
                sid_a = "aaaa1111-2222-3333-4444-555566667777"
                sid_b = "bbbb1111-2222-3333-4444-555566667777"
                cwd = "/srv/shared-app"

                # 1) Session B (gen=2) writes newer repo_files=["new.py"] to shared (host, cwd) cache
                st_b1 = tracker._apply_remote_probe_response(
                    host,
                    {
                        "ok": True,
                        "cli": "claude",
                        "session_id": sid_b,
                        "cwd": cwd,
                        "etag": "etag-b1",
                        "turns": [("user", "b1")],
                        "files_included": True,
                        "repo_files": ["new.py"],
                        "_probe_gen": 2,
                    },
                )
                self.assertIsNotNone(st_b1)
                assert st_b1 is not None
                self.assertEqual(st_b1.remote_repo_files, ["new.py"])

                # 2) Stale Session A (gen=1) arrives late with repo_files=["old.py"] -> MUST NOT overwrite shared cache!
                st_a = tracker._apply_remote_probe_response(
                    host,
                    {
                        "ok": True,
                        "cli": "claude",
                        "session_id": sid_a,
                        "cwd": cwd,
                        "etag": "etag-a1",
                        "turns": [("user", "a1")],
                        "files_included": True,
                        "repo_files": ["old.py"],
                        "_probe_gen": 1,
                    },
                )
                self.assertIsNotNone(st_a)

                # 3) Session B (gen=3) omits file list -> reads shared cache and STILL gets ["new.py"]!
                st_b2 = tracker._apply_remote_probe_response(
                    host,
                    {
                        "ok": True,
                        "cli": "claude",
                        "session_id": sid_b,
                        "cwd": cwd,
                        "etag": "etag-b2",
                        "turns": [("user", "b1"), ("user", "b2")],
                        "files_included": False,
                        "_probe_gen": 3,
                    },
                )
                self.assertIsNotNone(st_b2)
                assert st_b2 is not None
                self.assertEqual(st_b2.remote_repo_files, ["new.py"])
            finally:
                tracker.close()

    # ------------------------------------------------------------------
    # Follow-Up 7 [P2]: Concurrent t=0ms origin resolve never blocks start_recording & keeps normal Ghostty+tmux direct paste
    # ------------------------------------------------------------------
    def test_13_concurrent_origin_resolve_does_not_block_mic_open_and_preserves_multitab_ghostty_tmux(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            rt_dir = tmp / "rt"
            rt_dir.mkdir(parents=True)
            sock_path = str(tmp / "tmux-501" / "default")
            repo_a = tmp / "repo-a"
            repo_a.mkdir()

            # Two Ghostty tabs running tmux (same generic title "Ghostty", different TTYs): normal user with NO switch
            snap = TmuxServerSnapshot(
                clients=(
                    TmuxClientInfo(
                        session_id="$0",
                        window_id="@0",
                        pane_id="%1",
                        client_tty="/dev/ttys005",
                        client_pid=5000,
                        socket_path=sock_path,
                    ),
                    TmuxClientInfo(
                        session_id="$1",
                        window_id="@1",
                        pane_id="%9",
                        client_tty="/dev/ttys006",
                        client_pid=5009,
                        socket_path=sock_path,
                    ),
                ),
                panes=(
                    TmuxPaneInfo(
                        session_id="$0",
                        window_id="@0",
                        pane_id="%1",
                        pane_pid=5001,
                        pane_tty="/dev/ttys011",
                        current_path=str(repo_a),
                        pane_title="claude",
                        pane_active=True,
                        window_active=True,
                        socket_path=sock_path,
                    ),
                    TmuxPaneInfo(
                        session_id="$1",
                        window_id="@1",
                        pane_id="%9",
                        pane_pid=5010,
                        pane_tty="/dev/ttys019",
                        current_path=str(repo_a),
                        pane_title="zsh",
                        pane_active=True,
                        window_active=True,
                        socket_path=sock_path,
                    ),
                ),
            )

            cfg = Config()
            cfg.audio.beep = False
            cfg.hotkey.debounce_ms = 0
            plat = MacOSPlatform(
                cfg,
                env={"HOME": str(home), "PATH": "/usr/bin:/opt/homebrew/bin"},
                cache_dir=tmp / "cache",
                tmux_probe_fn=lambda: snap,
            )
            self.assertFalse(hasattr(plat, "capture_trigger_origin"), "Sync pre-start tmux/ps probe must be removed")

            tracker = SessionTracker(
                home_dir=home,
                runtime_dir=rt_dir,
                proc_root=None,
                env={"HOME": str(home)},
                tmux_probe_fn=lambda: snap,
            )
            wav_bytes = _make_speech_wav()
            fg_probe_entered = threading.Event()
            release_fg_probe = threading.Event()
            start_rec_entered = threading.Event()
            tmux_deliveries: list[tuple[str, str]] = []

            def _slow_ghostty_query() -> tuple[str, str, str, str, int]:
                fg_probe_entered.set()
                release_fg_probe.wait(timeout=3.0)
                return ("surf-1", "Ghostty", "/dev/ttys005", str(repo_a), 4200)

            def _fast_start_rec(rt: Path, *, loopback: bool = False, seq_id: int | None = None) -> None:
                del rt, loopback, seq_id
                # Prove true concurrent overlap: origin probe is ALREADY inside _slow_ghostty_query (blocked on
                # release_fg_probe) BEFORE start_recording returns! Serial order either way would fail/timeout here.
                self.assertTrue(
                    fg_probe_entered.wait(timeout=2.0),
                    "origin probe must overlap concurrently with start_recording",
                )
                self.assertFalse(release_fg_probe.is_set())
                start_rec_entered.set()

            plat.start_recording = _fast_start_rec  # type: ignore[method-assign]
            plat.stop_recording = lambda *, drain=True: (wav_bytes, "avfoundation")  # type: ignore[method-assign]
            plat.hud = lambda *a, **kw: None  # type: ignore[method-assign]
            plat.beep = lambda kind: None  # type: ignore[method-assign]

            daemon = VoiceInputDaemon(
                cfg,
                env={"HOME": str(home)},
                runtime_dir=rt_dir,
                platform=plat,
                asr_provider=_StaticAsr(),
                refiner=_NoopRefiner(),
                tracker=tracker,
            )
            try:
                with mock.patch.object(
                    plat, "_which", side_effect=lambda name: f"/usr/bin/{name}" if name in ("osascript", "pbcopy") else None
                ), mock.patch.object(
                    plat, "_query_frontmost_process_and_title", return_value=("Ghostty", GHOSTTY_BUNDLE_ID, "Ghostty", 4100)
                ), mock.patch.object(
                    plat, "_query_ghostty_terminal", side_effect=_slow_ghostty_query
                ), mock.patch.object(
                    plat, "_resolve_tmux_exe", return_value="/opt/homebrew/bin/tmux"
                ), mock.patch.object(
                    plat,
                    "_deliver_via_tmux_buffer",
                    side_effect=lambda pfx, txt, *, seq_id, target_pane="", tmux_env=None: (
                        tmux_deliveries.append((target_pane, txt)),
                        True,
                    )[1],
                ):
                    trigger_res: list[dict[str, Any]] = []
                    th = threading.Thread(
                        target=lambda: trigger_res.append(
                            daemon.handle_trigger(
                                "ptt_start",
                                ForegroundWindow(
                                    hwnd=100,
                                    process="Ghostty",
                                    title="Ghostty",
                                    bundle_id=GHOSTTY_BUNDLE_ID,
                                    pid=4100,
                                    ax_trusted=True,
                                ),
                                from_hotkey=True,
                            )
                        ),
                        daemon=True,
                    )
                    th.start()
                    # Verify BOTH started concurrently and start_recording did NOT block on slow Ghostty query!
                    self.assertTrue(start_rec_entered.wait(timeout=1.5), "start_recording must not wait for slow origin probe")
                    self.assertTrue(fg_probe_entered.wait(timeout=1.5), "origin probe should launch concurrently at t=0")
                    self.assertFalse(release_fg_probe.is_set())

                    release_fg_probe.set()
                    th.join(timeout=3.0)
                    self.assertTrue(trigger_res and trigger_res[0].get("ok"))
                    # Normal multi-tab Ghostty+tmux user (no switch): origin locks onto %1 without false drift!
                    self.assertFalse(daemon._origin_win.origin_drifted)
                    self.assertEqual(daemon._origin_win.surface_id, "surf-1")
                    self.assertEqual(daemon._origin_win.tmux_pane, "%1")
                    self.assertEqual(daemon._origin_win.tmux_socket, sock_path)

                    # Direct tmux delivery to %1 succeeds!
                    daemon.handle_trigger("ptt_stop")
                    deadline = time.monotonic() + 3.0
                    while time.monotonic() < deadline and not daemon.last_result:
                        time.sleep(0.02)
                    self.assertTrue(daemon.last_result.get("pasted"))
                    self.assertEqual(daemon.last_result.get("paste_reason"), "tmux")
                    self.assertEqual(tmux_deliveries, [("%1", "测试语音输入")])
            finally:
                release_fg_probe.set()
                daemon.close()

    # ------------------------------------------------------------------
    # Follow-Up 8 [P1+P2]: Concurrent origin timeout/no-result and Thread.start() failure degrade safely to origin_drifted=True
    # ------------------------------------------------------------------
    def test_14_concurrent_origin_timeout_and_thread_spawn_failure_degrade_safely(self) -> None:
        # Sub-case A: CLI toggle (window=None) concurrent origin probe times out -> origin_drifted=True,
        # switching to Ghostty window B must NOT direct-paste into B (clipboard-only safe fallback).
        with tempfile.TemporaryDirectory() as td_a:
            tmp_a = Path(td_a)
            cfg_a = Config()
            cfg_a.audio.beep = False
            cfg_a.hotkey.debounce_ms = 0
            plat_a = MacOSPlatform(
                cfg_a,
                env={"HOME": str(tmp_a), "PATH": "/usr/bin:/opt/homebrew/bin"},
                cache_dir=tmp_a / "cache",
            )
            tracker_a = SessionTracker(
                home_dir=tmp_a, runtime_dir=tmp_a / "rt", proc_root=None, env={"HOME": str(tmp_a)}
            )
            wav_bytes = _make_speech_wav()
            unblock_hung_probe = threading.Event()
            copied_a: list[str] = []
            ghostty_paste_calls_a: list[tuple[str, str]] = []
            rec_starts_a = [0]

            plat_a.start_recording = lambda rt, *, loopback=False, seq_id=None: rec_starts_a.__setitem__(0, rec_starts_a[0] + 1)  # type: ignore[method-assign]
            plat_a.stop_recording = lambda *, drain=True: (wav_bytes, "avfoundation")  # type: ignore[method-assign]
            plat_a.hud = lambda *a, **kw: None  # type: ignore[method-assign]
            plat_a.beep = lambda kind: None  # type: ignore[method-assign]

            daemon_a = VoiceInputDaemon(
                cfg_a,
                env={"HOME": str(tmp_a)},
                runtime_dir=tmp_a / "rt",
                platform=plat_a,
                asr_provider=_StaticAsr(),
                refiner=_NoopRefiner(),
                tracker=tracker_a,
            )
            daemon_a._origin_resolve_timeout_sec = 0.05
            try:
                def _hung_resolve(win: ForegroundWindow | None = None) -> ForegroundWindow:
                    del win
                    unblock_hung_probe.wait(timeout=3.0)
                    return ForegroundWindow(200, "Ghostty", "Window B", GHOSTTY_BUNDLE_ID, surface_id="surf-B")

                with mock.patch.object(
                    plat_a, "resolve_foreground_window", side_effect=_hung_resolve
                ), mock.patch.object(
                    plat_a,
                    "get_foreground_window",
                    return_value=ForegroundWindow(200, "Ghostty", "Window B", GHOSTTY_BUNDLE_ID, surface_id="surf-B"),
                ), mock.patch.object(
                    plat_a, "_copy_to_clipboard", side_effect=lambda t: (copied_a.append(t), True)[1]
                ), mock.patch.object(
                    plat_a,
                    "_run_osascript",
                    side_effect=lambda args, *, timeout=2.5: (
                        ghostty_paste_calls_a.append((args[3], args[4])) if len(args) >= 5 else None,
                        subprocess.CompletedProcess(["osascript"], 0, "ok\n", ""),
                    )[1],
                ):
                    r_start = daemon_a.handle_trigger("toggle", None)
                    self.assertTrue(r_start.get("ok"))
                    self.assertEqual(rec_starts_a[0], 1)
                    self.assertTrue(daemon_a._origin_win.origin_drifted)

                    # Stop recording while focused on Window B -> MUST NOT direct-paste into B!
                    daemon_a.handle_trigger("toggle", None)
                    deadline = time.monotonic() + 3.0
                    while time.monotonic() < deadline and not daemon_a.last_result:
                        time.sleep(0.02)
                    self.assertTrue(daemon_a.last_result)
                    self.assertFalse(daemon_a.last_result.get("pasted"))
                    self.assertTrue(daemon_a.last_result.get("clipboard"))
                    self.assertEqual(daemon_a.last_result.get("paste_reason"), "focus_changed")
                    self.assertEqual(ghostty_paste_calls_a, [])
                    self.assertEqual(copied_a, ["测试语音输入"])
            finally:
                unblock_hung_probe.set()
                daemon_a.close()

        # Sub-case B: Thread.start() raises RuntimeError when spawning avi-origin-* thread ->
        # start_recording() still runs cleanly (no fake recording state), origin_drifted=True degrades to copy-only.
        with tempfile.TemporaryDirectory() as td_b:
            tmp_b = Path(td_b)
            cfg_b = Config()
            cfg_b.audio.beep = False
            cfg_b.hotkey.debounce_ms = 0
            plat_b = MacOSPlatform(
                cfg_b,
                env={"HOME": str(tmp_b), "PATH": "/usr/bin:/opt/homebrew/bin"},
                cache_dir=tmp_b / "cache",
            )
            tracker_b = SessionTracker(
                home_dir=tmp_b, runtime_dir=tmp_b / "rt", proc_root=None, env={"HOME": str(tmp_b)}
            )
            wav_bytes = _make_speech_wav()
            copied_b: list[str] = []
            rec_starts_b = [0]
            rec_stops_b = [0]

            plat_b.start_recording = lambda rt, *, loopback=False, seq_id=None: rec_starts_b.__setitem__(0, rec_starts_b[0] + 1)  # type: ignore[method-assign]
            plat_b.stop_recording = lambda *, drain=True: (rec_stops_b.__setitem__(0, rec_stops_b[0] + 1), (wav_bytes, "avfoundation"))[1]  # type: ignore[method-assign]
            plat_b.hud = lambda *a, **kw: None  # type: ignore[method-assign]
            plat_b.beep = lambda kind: None  # type: ignore[method-assign]

            daemon_b = VoiceInputDaemon(
                cfg_b,
                env={"HOME": str(tmp_b)},
                runtime_dir=tmp_b / "rt",
                platform=plat_b,
                asr_provider=_StaticAsr(),
                refiner=_NoopRefiner(),
                tracker=tracker_b,
            )
            orig_thread_start = threading.Thread.start

            def _fail_origin_thread_start(self_th: threading.Thread) -> None:
                if self_th.name.startswith("avi-origin-"):
                    raise RuntimeError("can't start new thread")
                orig_thread_start(self_th)

            try:
                with mock.patch.object(
                    threading.Thread, "start", autospec=True, side_effect=_fail_origin_thread_start
                ), mock.patch.object(
                    plat_b, "_copy_to_clipboard", side_effect=lambda t: (copied_b.append(t), True)[1]
                ):
                    r_start_b = daemon_b.handle_trigger(
                        "ptt_start",
                        ForegroundWindow(100, "Ghostty", "claude", GHOSTTY_BUNDLE_ID, pid=4100),
                        from_hotkey=True,
                    )
                    self.assertTrue(r_start_b.get("ok"))
                    self.assertEqual(rec_starts_b[0], 1, "start_recording must still run on origin thread spawn failure")
                    self.assertTrue(daemon_b._recording_active)
                    self.assertTrue(daemon_b._origin_win.origin_drifted)

                    r_stop_b = daemon_b.handle_trigger("ptt_stop")
                    self.assertTrue(r_stop_b.get("ok"))
                    self.assertEqual(rec_stops_b[0], 1)
                    deadline_b = time.monotonic() + 3.0
                    while time.monotonic() < deadline_b and not daemon_b.last_result:
                        time.sleep(0.02)
                    self.assertTrue(daemon_b.last_result)
                    self.assertFalse(daemon_b.last_result.get("pasted"))
                    self.assertTrue(daemon_b.last_result.get("clipboard"))
                    self.assertEqual(daemon_b.last_result.get("paste_reason"), "focus_changed")
                    self.assertEqual(copied_b, ["测试语音输入"])
            finally:
                daemon_b.close()

        # Sub-case C: CLI toggle (window=None) concurrent origin probe returns an empty ForegroundWindow() on time
        # (macOS shim could not identify frontmost app) -> origin_drifted=True, switching to Ghostty B copies only!
        with tempfile.TemporaryDirectory() as td_c:
            tmp_c = Path(td_c)
            cfg_c = Config()
            cfg_c.audio.beep = False
            cfg_c.hotkey.debounce_ms = 0
            plat_c = MacOSPlatform(
                cfg_c,
                env={"HOME": str(tmp_c), "PATH": "/usr/bin:/opt/homebrew/bin"},
                cache_dir=tmp_c / "cache",
            )
            tracker_c = SessionTracker(
                home_dir=tmp_c, runtime_dir=tmp_c / "rt", proc_root=None, env={"HOME": str(tmp_c)}
            )
            wav_bytes = _make_speech_wav()
            copied_c: list[str] = []
            ghostty_paste_calls_c: list[tuple[str, str]] = []

            plat_c.start_recording = lambda rt, *, loopback=False, seq_id=None: None  # type: ignore[method-assign]
            plat_c.stop_recording = lambda *, drain=True: (wav_bytes, "avfoundation")  # type: ignore[method-assign]
            plat_c.hud = lambda *a, **kw: None  # type: ignore[method-assign]
            plat_c.beep = lambda kind: None  # type: ignore[method-assign]

            daemon_c = VoiceInputDaemon(
                cfg_c,
                env={"HOME": str(tmp_c)},
                runtime_dir=tmp_c / "rt",
                platform=plat_c,
                asr_provider=_StaticAsr(),
                refiner=_NoopRefiner(),
                tracker=tracker_c,
            )
            try:
                with mock.patch.object(
                    plat_c,
                    "resolve_foreground_window",
                    return_value=ForegroundWindow(hwnd=0, process="", title=""),
                ), mock.patch.object(
                    plat_c,
                    "get_foreground_window",
                    return_value=ForegroundWindow(200, "Ghostty", "Window B", GHOSTTY_BUNDLE_ID, surface_id="surf-B"),
                ), mock.patch.object(
                    plat_c, "_copy_to_clipboard", side_effect=lambda t: (copied_c.append(t), True)[1]
                ), mock.patch.object(
                    plat_c,
                    "_run_osascript",
                    side_effect=lambda args, *, timeout=2.5: (
                        ghostty_paste_calls_c.append((args[3], args[4])) if len(args) >= 5 else None,
                        subprocess.CompletedProcess(["osascript"], 0, "ok\n", ""),
                    )[1],
                ):
                    r_start_c = daemon_c.handle_trigger("toggle", None)
                    self.assertTrue(r_start_c.get("ok"))
                    self.assertTrue(daemon_c._origin_win.origin_drifted)

                    daemon_c.handle_trigger("toggle", None)
                    deadline_c = time.monotonic() + 3.0
                    while time.monotonic() < deadline_c and not daemon_c.last_result:
                        time.sleep(0.02)
                    self.assertTrue(daemon_c.last_result)
                    self.assertFalse(daemon_c.last_result.get("pasted"))
                    self.assertTrue(daemon_c.last_result.get("clipboard"))
                    self.assertIn("focus_changed", str(daemon_c.last_result.get("paste_reason") or ""))
                    self.assertEqual(ghostty_paste_calls_c, [])
                    self.assertEqual(copied_c, ["测试语音输入"])
            finally:
                daemon_c.close()


if __name__ == "__main__":
    unittest.main()
