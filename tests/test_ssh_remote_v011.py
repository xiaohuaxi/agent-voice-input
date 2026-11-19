"""Comprehensive offline hermetic test suite for v0.11.0 SSH Remote Linux routing & Phase 0 crosstalk veto."""
from __future__ import annotations

import ast
import gzip
import io
import json
import os
from pathlib import Path
import pty
import random
import select
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agent_voice_input import _remote_probe  # noqa: E402
from agent_voice_input import cli as cli_mod  # noqa: E402
from agent_voice_input import platform_macos as pmac  # noqa: E402
from agent_voice_input import session_title as VST  # noqa: E402
from agent_voice_input._tmux import TmuxClientInfo, TmuxPaneInfo, TmuxServerSnapshot  # noqa: E402
from agent_voice_input.app_scope import (  # noqa: E402
    classify_window_category,
    is_dedicated_ssh_client_window,
    is_remote_ide_window,
    resolve_windows_paste_chord,
)
from agent_voice_input.asr_providers import AsrContext, AsrResult, BaseAsrProvider  # noqa: E402
from agent_voice_input.audio_proc import pack_pcm16_mono_wav  # noqa: E402
from agent_voice_input.config import (  # noqa: E402
    Config,
    ConfigError,
    RefinerConfig,
    RemoteConfig,
    RemoteHostConfig,
    load_config,
)
from agent_voice_input.daemon import VoiceInputDaemon, create_platform, run_pipeline_once  # noqa: E402
from agent_voice_input.platform_wsl import ForegroundWindow, PasteOutcome  # noqa: E402
from agent_voice_input.refiner import BaseRefiner, RefinerResult  # noqa: E402
from agent_voice_input.remote_ssh import (  # noqa: E402
    RemoteSshManager,
    build_probe_stdin_payload,
    build_safe_ssh_argv,
    decode_probe_stdout,
    resolve_ssh_executable,
)
from agent_voice_input.session_ctx import (  # noqa: E402
    SessionMatch,
    SessionState,
    SessionTracker,
    TitleHint,
    _collect_claude_storage_matches,
    _collect_codex_storage_matches,
    _cwd_match_rank,
    _disambiguate_candidates,
    _find_storage_session_for_cli,
    _is_broad_or_unsafe_workspace_dir,
    _lookup_by_short_prefix,
    _match_cli_process,
    _normalize_cwd,
    _query_pid_codex_rollout_paths,
    _read_trusted_registry_json,
    _resolve_safe_local_dir,
    _scan_nonproc_fallback_sessions,
    _upgrade_agy_transcript_path,
    chunk_turns_for_refiner,
    encode_claude_project_dir,
    is_agy_root_session_valid,
    parse_window_title_hint,
    resolve_ide_workspace_cwd,
    scan_proc_root_sessions,
    trim_turns_chunk_aligned,
    write_session_registry_entry,
)


def _make_voiced_wav(duration_sec: float = 0.6) -> bytes:
    import math

    sr = 16000
    n = int(sr * duration_sec)
    samples = bytearray()
    for i in range(n):
        val = int(9000 * math.sin(2.0 * math.pi * 220.0 * (i / sr)))
        samples.extend(struct.pack("<h", val))
    return pack_pcm16_mono_wav(bytes(samples), sample_rate=sr)


def _write_fake_proc(
    proc_root: Path,
    pid: int,
    *,
    comm: str,
    ppid: int = 1,
    psid: int | None = None,
    tty_nr: int = 0,
    cwd: str = "",
    cmdline: list[str] | tuple[str, ...] = (),
    fds: dict[str, str] | None = None,
) -> Path:
    eff_psid = pid if psid is None else psid
    pdir = proc_root / str(pid)
    (pdir / "fd").mkdir(parents=True, exist_ok=True)
    stat_line = f"{pid} ({comm}) S {ppid} {eff_psid} {eff_psid} {tty_nr} 0 0 0 0 0 0 0 0 0 0 20 0 1 0 100\n"
    (pdir / "stat").write_text(stat_line, encoding="utf-8")
    (pdir / "comm").write_text(f"{comm}\n", encoding="utf-8")
    eff_cmd = list(cmdline) if cmdline else [comm]
    (pdir / "cmdline").write_bytes(b"\x00".join(a.encode("utf-8") for a in eff_cmd) + b"\x00")
    if cwd:
        os.symlink(cwd, pdir / "cwd")
    for fd_k, target in (fds or {}).items():
        os.symlink(target, pdir / "fd" / str(fd_k))
    return pdir


def _write_claude_jsonl(
    home: Path,
    sid: str,
    cwd: str,
    turns: list[tuple[str, str]],
    *,
    mtime: float | None = None,
    is_sidechain: bool = False,
) -> Path:
    encs = encode_claude_project_dir(cwd)
    pdir = home / ".claude" / "projects" / (encs[0] if encs else "-proj")
    pdir.mkdir(parents=True, exist_ok=True)
    jf = pdir / f"{sid}.jsonl"
    lines: list[str] = []
    for role, text in turns:
        rec = {
            "sessionId": sid,
            "cwd": cwd,
            "isSidechain": is_sidechain,
            "type": role,
            "message": {"role": role, "content": [{"type": "text", "text": text}]},
        }
        lines.append(json.dumps(rec, ensure_ascii=False))
    jf.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(jf, (mtime, mtime))
    return jf


def _write_codex_jsonl(
    home: Path,
    sid: str,
    cwd: str,
    turns: list[tuple[str, str]],
    *,
    mtime: float | None = None,
    source: str = "cli",
) -> Path:
    sdir = home / ".codex" / "sessions" / "2026" / "11" / "19"
    sdir.mkdir(parents=True, exist_ok=True)
    jf = sdir / f"rollout-2026-11-19T12-00-00-{sid}.jsonl"
    lines = [
        json.dumps(
            {"type": "session_meta", "payload": {"id": sid, "source": source, "cwd": cwd}},
            ensure_ascii=False,
        )
    ]
    for role, text in turns:
        itype = "UserMessage" if role == "user" else "AgentMessage"
        lines.append(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "item": {"type": itype, "content": [{"type": "text", "text": text}]},
                    },
                },
                ensure_ascii=False,
            )
        )
    jf.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(jf, (mtime, mtime))
    return jf


def _write_agy_jsonl(
    home: Path,
    cid: str,
    cwd: str,
    turns: list[tuple[str, str]],
    *,
    mtime: float | None = None,
    subagent: bool = False,
) -> tuple[Path, Path]:
    pdir = home / ".gemini" / "antigravity-cli" / "presence"
    pdir.mkdir(parents=True, exist_ok=True)
    lock_f = pdir / f"{cid}.lock"
    lock_f.write_text("", encoding="utf-8")
    logs = home / ".gemini" / "antigravity-cli" / "brain" / cid / ".system_generated" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    tf = logs / "transcript_full.jsonl"
    lines: list[str] = []
    if subagent:
        lines.append(
            json.dumps(
                {
                    "source": "SYSTEM",
                    "type": "SYSTEM_MESSAGE",
                    "content": "[Message] timestamp=2026-11-19T00:00:00Z sender=parent <subagent_reminder>",
                }
            )
        )
    for idx, (role, text) in enumerate(turns):
        if role == "user":
            prefix = f"<user_information>\nCommand Working Directory: {cwd}\n</user_information>\n" if idx == 0 else ""
            lines.append(
                json.dumps(
                    {
                        "source": "USER_EXPLICIT",
                        "type": "USER_INPUT",
                        "content": f"{prefix}<USER_REQUEST>{text}</USER_REQUEST>",
                    },
                    ensure_ascii=False,
                )
            )
        else:
            lines.append(
                json.dumps(
                    {"source": "MODEL", "type": "PLANNER_RESPONSE", "content": text},
                    ensure_ascii=False,
                )
            )
    tf.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(tf, (mtime, mtime))
    return tf, lock_f


class _CaptureAsr(BaseAsrProvider):
    provider_name = "fake_asr"

    def __init__(self, draft: str = "请检查 remote_ssh 模块的熔断器状态") -> None:
        self.draft = draft
        self.contexts: list[AsrContext] = []

    def transcribe(
        self,
        wav_bytes: bytes,
        *,
        context: AsrContext | None = None,
        is_cancelled=None,
        cancel_token=None,
    ) -> AsrResult:
        del wav_bytes, is_cancelled, cancel_token
        if context is not None:
            self.contexts.append(context)
        return AsrResult(text=self.draft, provider="fake_asr", model="fake-asr-1", latency_ms=5)


class _CaptureRefiner(BaseRefiner):
    provider_name = "fake_refiner"

    def __init__(self, tracker: SessionTracker) -> None:
        self.tracker = tracker
        self.calls: list[dict[str, object]] = []

    def refine(
        self,
        asr_input: AsrResult | str,
        *,
        session_key: str = "",
        mode: str = "agent",
        workspace_cwd: str = "",
        **kwargs: object,
    ) -> RefinerResult:
        del kwargs
        asr_text = asr_input.text if isinstance(asr_input, AsrResult) else str(asr_input)
        chunks, git_ctx = self.tracker.get_refiner_context(
            session_key, workspace_cwd=workspace_cwd
        )
        self.calls.append(
            {
                "session_key": session_key,
                "mode": mode,
                "chunks": list(chunks),
                "git_ctx": git_ctx,
            }
        )
        ctx_chars = sum(len(c) for c in chunks) + len(git_ctx)
        return RefinerResult(
            text=asr_text,
            provider="fake_refiner",
            model="fake-ref-1",
            latency_ms=4,
            context_chars=ctx_chars,
            context_chunks=len(chunks),
        )


class _FakeVoicePlatform:
    def __init__(self, wav_bytes: bytes, fg_win: ForegroundWindow) -> None:
        self._wav = wav_bytes
        self.fg_win = fg_win
        self.pasted: list[str] = []

    def is_allowed_terminal(self, win: ForegroundWindow) -> bool:
        return classify_window_category(win, is_terminal=lambda w: w.process == "WindowsTerminal.exe") == "terminal"

    def get_foreground_window(self) -> ForegroundWindow:
        return self.fg_win

    def resolve_foreground_window(self, hint: ForegroundWindow | None = None) -> ForegroundWindow:
        return hint if (hint is not None and (hint.title or hint.process)) else self.fg_win

    def start_recording(self, runtime_dir: Path, **kwargs: object) -> None:
        del runtime_dir, kwargs

    def stop_recording(self, **kwargs: object) -> tuple[bytes, str]:
        del kwargs
        return self._wav, "wasapi"

    def cancel_recording(self) -> None:
        pass

    def hud(self, state: str, **kwargs: object) -> None:
        del state, kwargs

    def beep(self, kind: str) -> None:
        del kind

    def deliver_text(self, text: str, **kwargs: object) -> PasteOutcome:
        del kwargs
        self.pasted.append(text)
        return PasteOutcome(True, True, "ok", "WindowsTerminal.exe")

    def close(self) -> None:
        pass


class SshRemoteLinuxV011Tests(unittest.TestCase):
    def test_phase0_all_four_crosstalk_veto_paths_and_tightening(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home, rt, proc = root / "home", root / "rt", root / "proc"
            same_repo = home / "repos" / "agent-voice-input"
            (same_repo / ".git").mkdir(parents=True)
            for d in (home, rt, proc):
                d.mkdir(parents=True, exist_ok=True)

            pts_local = root / "pts_local"
            pts_ssh = root / "pts_ssh"
            pts_tmux_0 = root / "pts_tmux0"
            pts_tmux_1 = root / "pts_tmux1"
            for p, mt in ((pts_local, 1500.0), (pts_ssh, 2500.0), (pts_tmux_0, 1800.0), (pts_tmux_1, 2200.0)):
                p.write_text("", encoding="utf-8")
                os.utime(p, (mt, mt))

            # Background live local Claude session in ~/repos/agent-voice-input
            local_sid = "11112222-3333-4444-8555-666677778888"
            local_jf = _write_claude_jsonl(
                home,
                local_sid,
                str(same_repo),
                [("user", "LOCAL_BACKGROUND_SECRET_TURN")],
                mtime=1500.0,
            )
            _write_fake_proc(
                proc,
                501,
                comm="claude",
                cwd=str(same_repo),
                cmdline=["claude"],
                fds={"0": str(pts_local)},
            )
            write_session_registry_entry(
                rt,
                cli="claude",
                session_id=local_sid,
                jsonl_path=str(local_jf),
                cwd=str(same_repo),
                tty=str(pts_local),
                agent_pid=501,
            )

            # Exited historical transcript on local disk (for Bug D 8-hex collision check)
            dead_sid = "deadbeef-9999-4888-a777-000011112222"
            _write_claude_jsonl(
                home,
                dead_sid,
                str(same_repo),
                [("user", "DEAD_HISTORICAL_TURN")],
                mtime=900.0,
            )

            tracker = SessionTracker(
                home_dir=home,
                runtime_dir=rt,
                proc_root=proc,
                env={"HOME": str(home), "HOSTNAME": "my-local-pc"},
                local_hostnames=("my-local-pc",),
            )

            # Sanity check: explicit local session resolves normally
            m_local_ok = tracker.resolve_session(
                "xhx@my-local-pc: ~/repos/agent-voice-input",
                window_process="WindowsTerminal.exe",
            )
            assert m_local_ok is not None
            self.assertEqual(m_local_ok.session_id, local_sid)
            self.assertEqual(m_local_ok.origin, "local")

            # --- Path 1: Dedicated SSH clients (even with empty/generic title & same-name repo) ---
            for ssh_proc, ssh_bundle, ssh_title, is_mac in (
                ("putty.exe", "", "PuTTY", False),
                ("kitty.exe", "", "agent-voice-input", False),  # Windows KiTTY SSH fork
                ("MobaXterm.exe", "", "MobaXterm", False),
                ("WindTerm.exe", "", "agent-voice-input", False),
                ("SecureCRT.exe", "", "SecureCRT", False),
                ("Xshell.exe", "", "Xshell 7", False),
                ("Termius", "com.termius-dmg.mac", "Termius", True),
            ):
                w_ssh = ForegroundWindow(10, ssh_proc, ssh_title, bundle_id=ssh_bundle)
                self.assertTrue(is_dedicated_ssh_client_window(w_ssh, is_macos=is_mac), ssh_proc)
                self.assertEqual(
                    classify_window_category(w_ssh, is_terminal=lambda _w: False),
                    "terminal",
                    ssh_proc,
                )
                self.assertIsNone(
                    tracker.resolve_session(
                        ssh_title, window_process=ssh_proc, bundle_id=ssh_bundle
                    ),
                    f"dedicated SSH client {ssh_proc} leaked local session!",
                )

            # macOS Kitty GPU terminal (`net.kovidgoyal.kitty`) MUST NOT be treated as Windows KiTTY SSH!
            mac_kitty = ForegroundWindow(11, "kitty", "zsh", bundle_id="net.kovidgoyal.kitty")
            self.assertFalse(is_dedicated_ssh_client_window(mac_kitty, is_macos=True))
            self.assertEqual(resolve_windows_paste_chord("MobaXterm.exe"), "shift_insert")
            self.assertEqual(resolve_windows_paste_chord("WindTerm.exe"), "ctrl_shift_v")

            # --- Path 2: Remote SSH window titles (`user@remote:path`, `[@host]`, `ssh ...`) ---
            for rem_title in (
                "xhx@devbox: ~/repos/agent-voice-input",
                "xhx@devbox: ~",
                "[root@gpu-node01 /srv/app]#",
                f"claude · ~/repos/agent-voice-input [#{local_sid[:8]}] [@devbox]",
                "ssh -p 2222 ubuntu@prod.internal",
                "C:\\Windows\\System32\\OpenSSH\\ssh.exe xhx@devbox",
                "ubuntu@devbox - PuTTY",
                f"claude [#{local_sid[:8]}] [@-oProxyCommand=id]",
                f"claude [#{local_sid[:8]}] [@box1] [@box2]",
            ):
                self.assertIsNone(
                    tracker.resolve_session(rem_title, window_process="WindowsTerminal.exe"),
                    f"remote title {rem_title!r} leaked local session!",
                )

            # Tightened local shell prompts (`xhx@my-local-pc: ~`, unrelated folder, CMD / C:\) -> None!
            for non_agent_local_title, wproc in (
                ("xhx@my-local-pc: ~", "WindowsTerminal.exe"),
                ("xhx@my-local-pc: ~/Downloads", "WindowsTerminal.exe"),
                ("C:\\WINDOWS\\system32\\cmd.exe", "cmd.exe"),
                ("Command Prompt", "WindowsTerminal.exe"),
                ("PS C:\\Users\\xhx>", "WindowsTerminal.exe"),
            ):
                self.assertIsNone(
                    tracker.resolve_session(non_agent_local_title, window_process=wproc),
                    f"unrelated local prompt {non_agent_local_title!r} hijacked background session!",
                )

            # --- Path 3: Fake `/proc` local terminal & local `tmux` pane running interactive `ssh` ---
            # 3a. Background `git fetch` (`ssh git@github.com git-upload-pack ...`) on local CLI's TTY MUST NOT veto!
            _write_fake_proc(
                proc,
                502,
                comm="ssh",
                ppid=501,
                cwd=str(same_repo),
                cmdline=["ssh", "git@github.com", "git-upload-pack", "org/repo.git"],
                fds={"0": str(pts_local)},
            )
            m_git_fetch = tracker.resolve_session("Ubuntu", window_tty=str(pts_local))
            assert m_git_fetch is not None
            self.assertEqual(m_git_fetch.session_id, local_sid)

            # 3b. Interactive `ssh devbox` in Tab 2 (`pts_ssh`, mtime=2500 > 1500) vetoes both scoped TTY & unscoped generic title!
            _write_fake_proc(
                proc,
                601,
                comm="ssh",
                ppid=1,
                cwd=str(home),
                cmdline=["ssh", "-p", "2222", "xhx@devbox"],
                fds={"0": str(pts_ssh)},
            )
            self.assertIsNone(tracker.resolve_session("Ubuntu", window_tty=str(pts_ssh)))
            self.assertIsNone(tracker.resolve_session("Ubuntu"))
            # Explicit local CLI title in Tab 1 still resolves Tab 1!
            m_tab1_explicit = tracker.resolve_session("claude · agent-voice-input")
            assert m_tab1_explicit is not None
            self.assertEqual(m_tab1_explicit.session_id, local_sid)

            # 3c. Local `tmux` active pane `%1` running `ssh devbox` while sibling pane `%0` runs local `claude`!
            _write_fake_proc(
                proc,
                700,
                comm="claude",
                ppid=1,
                cwd=str(same_repo),
                cmdline=["claude"],
                fds={"0": str(pts_tmux_0)},
            )
            _write_fake_proc(
                proc,
                710,
                comm="ssh",
                ppid=1,
                cwd=str(home),
                cmdline=["ssh", "devbox"],
                fds={"0": str(pts_tmux_1)},
            )
            active_pane_box = ["%1"]
            tracker_tmux = SessionTracker(
                home_dir=home,
                runtime_dir=rt,
                proc_root=proc,
                env={"HOME": str(home), "HOSTNAME": "my-local-pc"},
                local_hostnames=("my-local-pc",),
                tmux_probe_fn=lambda: TmuxServerSnapshot(
                    clients=(TmuxClientInfo("$0", "@0", active_pane_box[0], 9000.0, "/dev/pts/99", 699, True, "/tmp/t.sock"),),
                    panes=(
                        TmuxPaneInfo("%0", "$0", "@0", True, active_pane_box[0] == "%0", 700, str(pts_tmux_0), "/tmp/t.sock", str(same_repo), "claude"),
                        TmuxPaneInfo("%1", "$0", "@0", True, active_pane_box[0] == "%1", 710, str(pts_tmux_1), "/tmp/t.sock", str(home), "ssh"),
                    ),
                ),
            )
            self.assertIsNone(tracker_tmux.resolve_session("tmux"))
            active_pane_box[0] = "%0"
            m_pane0 = tracker_tmux.resolve_session("tmux")
            assert m_pane0 is not None
            self.assertEqual(m_pane0.cli, "claude")
            import shutil as _shutil
            _shutil.rmtree(proc / "700")
            _shutil.rmtree(proc / "710")

            # --- Path 4: Remote IDE windows (`[SSH: host]` & `vscode-remote://ssh-remote+...`) ---
            for ide_win in (
                ForegroundWindow(20, "Code.exe", "daemon.py - agent-voice-input [SSH: devbox] - Visual Studio Code"),
                ForegroundWindow(
                    21,
                    "Cursor",
                    "daemon.py — agent-voice-input",
                    bundle_id="com.todesktop.230313mzl4w4u92",
                    document="vscode-remote://ssh-remote+devbox/home/xhx/repos/agent-voice-input/daemon.py",
                ),
                ForegroundWindow(22, "Zed", "agent-voice-input — ssh://devbox/home/xhx/repos/agent-voice-input", bundle_id="dev.zed.Zed"),
            ):
                self.assertTrue(is_remote_ide_window(ide_win), ide_win.title)
                self.assertEqual(
                    resolve_ide_workspace_cwd(
                        ide_win,
                        home=home,
                        known_cwds=[str(same_repo)],
                        wsl=False,
                        env={"HOME": str(home)},
                    ),
                    "",
                )
                self.assertIsNone(
                    tracker.resolve_session(
                        ide_win.title,
                        window_process=ide_win.process,
                        bundle_id=ide_win.bundle_id,
                        document=ide_win.document,
                    )
                )

            # --- Bug D: 8-hex collision with dead historical local transcript MUST return None ---
            self.assertIsNone(
                tracker.resolve_session(
                    f"claude · ~/remote-only [#{dead_sid[:8]}]",
                    window_process="WindowsTerminal.exe",
                )
            )

            # --- macOS iTerm2 AppleScript TTY lookup & `-1744` zero-popup preflight ---
            mac_plat = pmac.MacOSPlatform(Config(), env={"HOME": str(home)}, cache_dir=root / "mcache")
            with (
                mock.patch.object(pmac, "_can_automate_bundle_without_prompt", return_value=False),
                mock.patch.object(
                    mac_plat,
                    "_run_osascript",
                    side_effect=AssertionError("osascript must not run when TCC is undetermined!"),
                ),
            ):
                self.assertIsNone(mac_plat._query_iterm2_terminal())

            with (
                mock.patch.object(pmac, "_can_automate_bundle_without_prompt", return_value=True),
                mock.patch.object(
                    mac_plat,
                    "_run_osascript",
                    return_value=subprocess.CompletedProcess(
                        ["osascript"],
                        0,
                        "it1\x1f42\x1f/dev/ttys017\x1fiTerm2\x1fclaude · ~/repo [#12345678]\n",
                        "",
                    ),
                ),
            ):
                it_res = mac_plat._query_iterm2_terminal()
                assert it_res is not None
                it_wid, it_title, it_tty, _ = it_res
                self.assertEqual(it_wid, 42)
                self.assertEqual(it_tty, "/dev/ttys017")
                self.assertIn("claude · ~/repo [#12345678]", it_title)

    def test_osc2_red_team_injection_and_wsl2_ssh_exe_safety(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # 1. Config TOML parser (including Python 3.10 `_parse_simple_toml` `[[remote.hosts]]`) & strict validation
            good_toml = root / "good.toml"
            good_toml.write_text(
                """
[remote]
timeout_ms = 350
ssh_bin = "ssh"

[[remote.hosts]]
name = "devbox"
ssh_target = "ubuntu@10.0.0.42"
aliases = ["gpu01", "prod-linux"]
""".lstrip(),
                encoding="utf-8",
            )
            os.chmod(good_toml, 0o600)
            cfg, _ = load_config(config_path=good_toml, env={"HOME": str(root)})
            self.assertTrue(cfg.remote.enabled)
            self.assertEqual(cfg.remote.timeout_ms, 350)
            h_gpu = cfg.remote.lookup_host("GPU01")
            assert h_gpu is not None
            self.assertEqual(h_gpu.name, "devbox")
            self.assertEqual(h_gpu.ssh_target, "ubuntu@10.0.0.42")

            for bad_body in (
                '[remote]\nhosts = ["-oProxyCommand=id"]\n',
                '[remote]\nhosts = ["evil host"]\n',
                '[remote]\nssh_bin = "-oProxyCommand=id"\n',
                '[[remote.hosts]]\nname = "dev"\nssh_target = "-Jevil"\n',
                '[[remote.hosts]]\nname = "dev"\nssh_target = "ok"\naliases = ["-bad"]\n',
            ):
                bad_p = root / "bad.toml"
                bad_p.write_text(bad_body, encoding="utf-8")
                os.chmod(bad_p, 0o600)
                with self.assertRaises(ConfigError):
                    load_config(config_path=bad_p, env={"HOME": str(root)})

            # 2. Unwhitelisted OSC 2 host -> ZERO subprocess.Popen calls!
            tracker = SessionTracker(
                home_dir=root,
                runtime_dir=root / "rt",
                proc_root=root / "empty_proc",
                env={"HOME": str(root), "HOSTNAME": "localpc"},
                remote_cfg=cfg.remote,
            )
            try:
                with mock.patch("agent_voice_input.remote_ssh.subprocess.Popen", side_effect=AssertionError("unwhitelisted SSH spawned!")):
                    for evil_title in (
                        "claude [#12345678] [@attacker.example.com]",
                        "claude [#12345678] [@-oProxyCommand=id]",
                        "root@evil.host: /root",
                    ):
                        self.assertIsNone(tracker.sync_session(window_title=evil_title))
            finally:
                tracker.close()

            # 3. Malicious OSC 2 `cwd` / `title` injection on whitelisted host: argv is static, stdin JSON is safe!
            pwn_marker = root / "pwned_marker"
            malicious_cwd = f"~/repo'; touch {pwn_marker}; $(touch {pwn_marker}); `touch {pwn_marker}`"
            malicious_title = f"claude · {malicious_cwd} [#12345678] [@gpu01]"

            argv_posix = build_safe_ssh_argv("/usr/bin/ssh", h_gpu, runtime_dir=root / "rt")
            self.assertEqual(argv_posix[-4:], ["--", "ubuntu@10.0.0.42", "python3", "-"])
            self.assertIn("BatchMode=yes", argv_posix)
            self.assertIn("PermitLocalCommand=no", argv_posix)
            self.assertIn("RemoteCommand=none", argv_posix)
            self.assertIn("ClearAllForwardings=yes", argv_posix)
            for arg in argv_posix:
                self.assertNotIn("pwned_marker", arg)
                self.assertNotIn("12345678", arg)

            payload_bytes = build_probe_stdin_payload(
                {"v": 1, "token": "12345678", "title": malicious_title, "cwd_hint": malicious_cwd}
            )
            proc = subprocess.run(
                [sys.executable, "-"],
                input=payload_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={"HOME": str(root), "AVI_REMOTE_PROC_ROOT": str(root / "empty_proc")},
                timeout=5.0,
                check=True,
            )
            self.assertFalse(pwn_marker.exists())
            parsed_resp = decode_probe_stdout(proc.stdout)
            self.assertFalse(parsed_resp["ok"])

            # 4. Noisy `.bashrc` MOTD prefix + Windows `ssh.exe` CRLF suffix + 2 MiB gzip bomb defense!
            framed_ok = (
                b"Welcome to Ubuntu 24.04 LTS!\r\nLast login: Mon Nov 19\r\n"
                + _remote_probe.pack_probe_stdout({"ok": True, "pong": True, "python": "3.12.1", "hostname": "devbox"})
                + b"\r\n"
            )
            self.assertTrue(decode_probe_stdout(framed_ok)["pong"])

            # 6 MiB decompressed payload (~6 KB compressed) MUST be rejected by streaming 2 MiB ceiling!
            bomb_gz = gzip.compress(b'{"ok":true,"pad":"' + (b"A" * (6 * 1024 * 1024)) + b'"}', compresslevel=1)
            bomb_framed = _remote_probe.PROBE_FRAME_MAGIC + struct.pack(">I", len(bomb_gz)) + bomb_gz
            with self.assertRaisesRegex(ValueError, "exceeded 2 MiB decompression cap"):
                decode_probe_stdout(bomb_framed)

            # 5. WSL2 `ssh.exe` resolution when `appendWindowsPath=false` + no POSIX `ControlMaster` on `.exe`
            fake_sys32_ssh = root / "Windows" / "System32" / "OpenSSH" / "ssh.exe"
            fake_sys32_ssh.parent.mkdir(parents=True)
            fake_sys32_ssh.write_text("", encoding="utf-8")
            with mock.patch(
                "agent_voice_input.remote_ssh.WSL_SYSTEM32_OPENSSH_Candidates",
                (str(fake_sys32_ssh),),
            ):
                resolved_exe = resolve_ssh_executable(
                    "ssh.exe",
                    env={"PATH": "/usr/bin:/bin", "WSL_DISTRO_NAME": "Ubuntu"},
                )
                self.assertEqual(resolved_exe, str(fake_sys32_ssh))
            argv_win = build_safe_ssh_argv(str(fake_sys32_ssh), h_gpu, runtime_dir=Path("/mnt/c/Users/xhx/rt"))
            self.assertTrue(all(not a.startswith("ControlMaster=") for a in argv_win))
            argv_wsl_mnt = build_safe_ssh_argv("/usr/bin/ssh", h_gpu, runtime_dir=Path("/mnt/c/Users/xhx/rt"))
            ctl_opts = [a for a in argv_wsl_mnt if a.startswith("ControlPath=")]
            self.assertEqual(len(ctl_opts), 1)
            self.assertFalse(ctl_opts[0].split("=", 1)[1].startswith("/mnt/"))

    def test_remote_probe_tier1_tier2_non_git_privacy_and_etag_short_circuit(self) -> None:
        # Static AST check: `_remote_probe.py` MUST have zero intra-package imports!
        probe_ast = ast.parse((SRC_ROOT / "agent_voice_input" / "_remote_probe.py").read_text(encoding="utf-8"))
        for node in ast.walk(probe_ast):
            if isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, "_remote_probe.py must not use relative imports!")
                self.assertNotEqual(node.module, "agent_voice_input")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(alias.name.startswith("agent_voice_input"))

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rhome = root / "remote_home"
            rproc = root / "remote_proc"
            rrt = root / "remote_rt"
            proj_a = rhome / "repos" / "svc-alpha"
            proj_b = rhome / "repos" / "svc-beta"
            non_git_dir = rhome / "notes" / "arch-folder"
            for d in (rhome, rproc, rrt, proj_a, proj_b, non_git_dir, non_git_dir / "sub"):
                d.mkdir(parents=True, exist_ok=True)
            os.chmod(rrt, 0o700)

            # Populate non-Git folder with code/doc files + ignored noise
            (non_git_dir / "protocol_v2.py").write_text("x = 1\n", encoding="utf-8")
            (non_git_dir / "sub" / "design_notes.md").write_text("# Notes\n", encoding="utf-8")
            (non_git_dir / ".secret_env").write_text("NO\n", encoding="utf-8")

            # Tier 1 sessions + subagent traps
            cl_old_a = "aaaa1111-0000-4000-8000-000000000001"
            cl_new_a = "aaaa2222-0000-4000-8000-000000000002"
            cl_sub_a = "aaaa2222-9999-4000-8000-999999999999"  # Same 8-hex prefix, but sidechain!
            cl_newer_b = "bbbb3333-0000-4000-8000-000000000003"
            cx_sid = "0196cafe-1234-7000-8000-abcdef012345"
            agy_cid = "cccc4444-5555-4666-8777-888899990000"
            agy_sub = "cccc4444-dead-4666-8777-ffffffffffff"

            _write_claude_jsonl(rhome, cl_old_a, str(proj_a), [("user", "alpha old turn")], mtime=1000.0)
            f_new_a = _write_claude_jsonl(
                rhome,
                cl_new_a,
                str(proj_a),
                [("user", "alpha latest turn"), ("assistant", "updated RemoteSshManager")],
                mtime=2000.0,
            )
            _write_claude_jsonl(
                rhome, cl_sub_a, str(proj_a), [("user", "SIDECHAIN_NOISE")], mtime=9999.0, is_sidechain=True
            )
            _write_claude_jsonl(
                rhome, cl_newer_b, str(proj_b), [("user", "BETA_OTHER_REPO_NOISE")], mtime=5000.0
            )
            _write_codex_jsonl(rhome, cx_sid, str(non_git_dir), [("user", "codex in non-git folder")], mtime=3000.0)
            _, agy_lock = _write_agy_jsonl(
                rhome, agy_cid, str(proj_a), [("user", "agy root turn")], mtime=2500.0
            )
            _write_agy_jsonl(rhome, agy_sub, str(proj_a), [("user", "AGY_SUBAGENT_NOISE")], mtime=9999.0, subagent=True)
            _write_fake_proc(rproc, 901, comm="agy", cwd=str(proj_a), cmdline=["agy"], fds={"7": str(agy_lock)})

            probe_env = {
                **os.environ,
                "HOME": str(rhome),
                "AVI_REMOTE_PROC_ROOT": str(rproc),
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rrt),
                "AVI_DISABLE_TMUX_PROBE": "1",
            }

            def _exec_probe_subproc(req: dict[str, object]) -> dict[str, object]:
                p = subprocess.run(
                    [sys.executable, "-"],
                    input=build_probe_stdin_payload(req),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=probe_env,
                    timeout=5.0,
                    check=True,
                )
                return decode_probe_stdout(p.stdout)

            # 1. Tier 1 `[#id8]` & `[§id8]` & UUID hit (filtering out sidechain & subagent!)
            r_t1_cl = _exec_probe_subproc({"v": 1, "title": f"claude [#{cl_new_a[:8]}]"})
            self.assertTrue(r_t1_cl["ok"])
            self.assertEqual(r_t1_cl["session_id"], cl_new_a)
            self.assertEqual(r_t1_cl["turns"], [("user", "alpha latest turn"), ("assistant", "updated RemoteSshManager")])

            r_t1_sec = _exec_probe_subproc({"v": 1, "title": f"agy [§{agy_cid[:8]}]"})
            self.assertTrue(r_t1_sec["ok"])
            self.assertEqual((r_t1_sec["cli"], r_t1_sec["session_id"]), ("agy", agy_cid))

            # 2. Tier 2 same-cwd multiple sessions -> picks latest `mtime` in `svc-alpha` (`cl_new_a` / `agy_cid`), NEVER `svc-beta`!
            # Remove live `agy` proc so Tier 2 storage disambiguation on `svc-alpha` is exercised directly
            for f in (rproc / "901" / "fd").iterdir():
                f.unlink()
            (rproc / "901" / "comm").unlink()
            r_t2_same_cwd = _exec_probe_subproc(
                {"v": 1, "title": "claude", "cwd_hint": "~/repos/svc-alpha"}
            )
            self.assertTrue(r_t2_same_cwd["ok"])
            self.assertEqual(r_t2_same_cwd["session_id"], cl_new_a)
            self.assertNotEqual(r_t2_same_cwd["session_id"], cl_newer_b)

            # 3. Non-Git shallow file extraction (`arch-folder`) + CLI `remote-ctx --raw-json`
            p_cli = subprocess.run(
                [sys.executable, "-m", "agent_voice_input.cli", "remote-ctx", "--raw-json"],
                input=json.dumps({"v": 1, "cwd_hint": "~/notes/arch-folder"}).encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={**probe_env, "PYTHONPATH": str(SRC_ROOT)},
                timeout=5.0,
                check=True,
            )
            r_nongit = json.loads(p_cli.stdout.decode("utf-8"))
            self.assertTrue(r_nongit["ok"])
            self.assertEqual(r_nongit["session_id"], cx_sid)
            self.assertIn("protocol_v2.py", r_nongit["shallow_files"])
            self.assertIn("design_notes.md", r_nongit["shallow_files"])
            self.assertNotIn(".secret_env", r_nongit["shallow_files"])

            # 4. Remote broad privacy roots (`~`, `/`, `/tmp`, `/home`) strictly refused!
            for broad_p in ("~", "/", "/tmp", "/home", str(rhome)):
                r_broad = _exec_probe_subproc({"v": 1, "cwd_hint": broad_p})
                self.assertFalse(r_broad["ok"], broad_p)
                self.assertEqual(r_broad["reason"], "broad_cwd")

            # 5. ETag `not_modified` short-circuit (`0.1ms` zero JSONL re-parse)
            etag_1 = str(r_t2_same_cwd["etag"])
            self.assertTrue(etag_1)
            r_etag_hit = _exec_probe_subproc(
                {
                    "v": 1,
                    "title": f"claude [#{cl_new_a[:8]}]",
                    "etag": etag_1,
                    "session_key_hint": f"claude:{cl_new_a}",
                }
            )
            self.assertTrue(r_etag_hit["ok"])
            self.assertTrue(r_etag_hit["not_modified"])
            self.assertEqual(r_etag_hit["turns"], [])
            # Appending to JSONL invalidates ETag and returns fresh turns!
            with open(f_new_a, "a", encoding="utf-8") as fa:
                fa.write(
                    json.dumps(
                        {
                            "sessionId": cl_new_a,
                            "cwd": str(proj_a),
                            "isSidechain": False,
                            "type": "user",
                            "message": {"role": "user", "content": "third turn appended"},
                        }
                    )
                    + "\n"
                )
            r_etag_miss = _exec_probe_subproc(
                {
                    "v": 1,
                    "title": f"claude [#{cl_new_a[:8]}]",
                    "etag": etag_1,
                    "session_key_hint": f"claude:{cl_new_a}",
                }
            )
            self.assertFalse(r_etag_miss["not_modified"])
            self.assertEqual(len(r_etag_miss["turns"]), 3)

            # 6. `session_title.py` SSH `[@host]` auto-tag & standalone `remote-title-hook.sh` on real PTY
            master_fd, slave_fd = pty.openpty()
            try:
                slave_tty = os.ttyname(slave_fd)
                slave_rdev = os.stat(slave_fd).st_rdev
                _write_fake_proc(
                    rproc,
                    1200,
                    comm="claude",
                    ppid=500,
                    psid=1200,
                    tty_nr=slave_rdev,
                    cwd=str(proj_a),
                    cmdline=["claude"],
                    fds={"0": slave_tty},
                )
                ok_ssh_hook = VST.update_claude_session(
                    {"session_id": cl_new_a, "cwd": str(proj_a), "transcript_path": str(f_new_a)},
                    home=rhome,
                    proc_root=rproc,
                    runtime_dir=rrt,
                    start_pid=1200,
                    env={"HOME": str(rhome), "SSH_CONNECTION": "10.0.0.1 55111 10.0.0.2 22", "HOSTNAME": "gpu-box.corp.internal"},
                )
                self.assertTrue(ok_ssh_hook)
                os.set_blocking(master_fd, False)
                rlist, _, _ = select.select([master_fd], [], [], 0.2)
                self.assertTrue(rlist)
                osc_bytes = os.read(master_fd, 4096).decode("utf-8")
                self.assertEqual(osc_bytes, f"\033]2;claude · ~/repos/svc-alpha [#{cl_new_a[:8]}] [@gpu-box]\007")
            finally:
                os.close(master_fd)
                os.close(slave_fd)

    def test_165_step_long_session_chunk_equivalence_and_namespace_isolation(self) -> None:
        rng = random.Random(20261119)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rhome = root / "rhome"
            proj = rhome / "repos" / "long-bench"
            proj.mkdir(parents=True)
            sid = "99998888-7777-4666-8555-444433332222"
            jf = _write_claude_jsonl(rhome, sid, str(proj), [])

            full_turns: list[tuple[str, str]] = []
            prev_sealed: list[str] = []
            prev_evicted = 0
            saw_eviction = False

            # 165-step continuous growth (0 -> ~380,000 chars across multiple 50K chunk evictions + mid-session compaction)
            for step in range(1, 166):
                if step == 45:
                    # Simulate Claude mid-session compaction boundary + summary reset!
                    summary_txt = "Compacted architecture summary: " + ("S" * 1800)
                    with open(jf, "a", encoding="utf-8") as f:
                        f.write(json.dumps({"type": "system", "subtype": "compact_boundary"}) + "\n")
                        f.write(
                            json.dumps(
                                {
                                    "sessionId": sid,
                                    "cwd": str(proj),
                                    "type": "user",
                                    "isCompactSummary": True,
                                    "message": {"role": "user", "content": summary_txt},
                                }
                            )
                            + "\n"
                        )
                    full_turns = [("assistant", summary_txt)]
                    prev_sealed = []
                    prev_evicted = 0
                else:
                    role = "user" if (step % 2 == 1) else "assistant"
                    body_len = rng.randint(1400, 3800)
                    msg = f"step={step:03d} ident_{step} " + ("x" * body_len)
                    with open(jf, "a", encoding="utf-8") as f:
                        f.write(
                            json.dumps(
                                {
                                    "sessionId": sid,
                                    "cwd": str(proj),
                                    "isSidechain": False,
                                    "type": role,
                                    "message": {"role": role, "content": msg},
                                }
                            )
                            + "\n"
                        )
                    full_turns.append((role, msg))

                probe_resp = _remote_probe.run_probe(
                    {"v": 1, "token": sid[:8], "cli": "claude", "include_git": False},
                    env={"HOME": str(rhome), "AVI_REMOTE_PROC_ROOT": str(root / "no_proc"), "AVI_DISABLE_TMUX_PROBE": "1"},
                    home_dir=rhome,
                    proc_root=root / "no_proc",
                )
                self.assertTrue(probe_resp["ok"])
                probe_turns = [(r, t) for r, t in probe_resp["turns"]]
                evicted_now = int(probe_resp["evicted_chunks"])
                if evicted_now > 0:
                    saw_eviction = True

                expected_chunks = chunk_turns_for_refiner(full_turns)
                actual_chunks = chunk_turns_for_refiner(probe_turns)
                self.assertEqual(
                    actual_chunks,
                    expected_chunks,
                    f"50K chunk mismatch at step {step} (evicted={evicted_now})",
                )

                sealed_now = actual_chunks[:-2] if len(actual_chunks) > 2 else []
                if step != 45 and evicted_now == prev_evicted and prev_sealed:
                    # Without chunk eviction, previously sealed 50K prefix blocks MUST never drift!
                    self.assertEqual(
                        sealed_now[: len(prev_sealed)],
                        prev_sealed,
                        f"sealed prefix block drifted at step {step}!",
                    )
                prev_sealed = sealed_now
                prev_evicted = evicted_now

            self.assertTrue(saw_eviction, "165-step session must exceed 200K chars and trigger whole-chunk eviction")
            # Also verify oversize single-turn truncation alignment
            giant_turn = [("user", "G" * 260000)]
            trimmed_giant, _ = trim_turns_chunk_aligned(giant_turn)
            self.assertEqual(chunk_turns_for_refiner(trimmed_giant), chunk_turns_for_refiner(giant_turn))

            # Cross-machine same-path isolation redline: remote cwd MUST NOT enter active_session_cwds()
            # or trigger ANY local git / scandir / filesystem inspection!
            same_local_dir = root / "shared_path" / "repo"
            (same_local_dir / ".git").mkdir(parents=True)
            (same_local_dir / "LOCAL_ONLY_SECRET.py").write_text("secret = 1\n", encoding="utf-8")

            rem_cfg = RemoteConfig(hosts=(RemoteHostConfig(name="devbox", ssh_target="devbox"),))
            tracker = SessionTracker(
                home_dir=root / "lhome",
                runtime_dir=root / "lrt",
                proc_root=root / "no_proc",
                env={"HOME": str(root / "lhome"), "HOSTNAME": "localpc"},
                remote_cfg=rem_cfg,
            )
            try:
                tracker._apply_remote_probe_response(
                    rem_cfg.hosts[0],
                    {
                        "ok": True,
                        "not_modified": False,
                        "etag": "1:2:3:4",
                        "cli": "claude",
                        "session_id": sid,
                        "matched_by": "remote_registry",
                        "cwd": str(same_local_dir),
                        "git_status": " M src/remote_only_engine.rs",
                        "git_hotwords": ["remote_only_engine.rs", "remote_only_engine"],
                        "repo_files": [],
                        "shallow_files": [],
                        "turns": [("user", "check remote_only_engine.rs")],
                        "evicted_chunks": 0,
                        "compaction_count": 0,
                    },
                )
                self.assertEqual(tracker.active_session_cwds(), [])
                skey = f"remote:devbox:claude:{sid}"
                with (
                    mock.patch("agent_voice_input.session_ctx.read_git_status_summary", side_effect=AssertionError("local git touched!")),
                    mock.patch("agent_voice_input.session_ctx.read_repo_core_files", side_effect=AssertionError("local repo_files touched!")),
                    mock.patch("agent_voice_input.session_ctx.read_workspace_shallow_files", side_effect=AssertionError("local scandir touched!")),
                ):
                    d_chunks, g_ctx = tracker.get_refiner_context(skey)
                    luna_txt, _, _ = tracker.get_luna_delta(skey, full=True)
                self.assertIn("remote_only_engine.rs", g_ctx)
                self.assertNotIn("LOCAL_ONLY_SECRET", g_ctx)
                self.assertIn("remote_only_engine.rs", luna_txt)
                self.assertEqual(len(d_chunks), 1)
            finally:
                tracker.close()

    def test_daemon_async_prefetch_400ms_timeout_breaker_cancel_and_doctor(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lhome, lrt = root / "lhome", root / "lrt"
            rhome, rproc = root / "rhome", root / "rproc"
            rproj = rhome / "repos" / "cloud-engine"
            bin_dir = root / "bin"
            for d in (lhome, lrt, rhome, rproc, rproj, bin_dir):
                d.mkdir(parents=True, exist_ok=True)

            rem_sid = "12345678-aaaa-4bbb-8ccc-ddddeeeeffff"
            _write_claude_jsonl(
                rhome,
                rem_sid,
                str(rproj),
                [
                    ("user", "我们在重构 RemoteSshManager 的预取和熔断器"),
                    ("assistant", "好的，请告诉我需要检查哪个函数。"),
                ],
                mtime=3000.0,
            )

            # Build hermetic executable `fake_ssh` script validating safe ssh flags & recording child PIDs
            ctl_file = root / "fake_ssh_ctl.json"
            invocations_log = root / "fake_ssh_calls.jsonl"
            ctl_file.write_text(json.dumps({"sleep_sec": 0.12, "mode": "ok"}), encoding="utf-8")

            fake_ssh = bin_dir / "fake_ssh"
            fake_ssh.write_text(
                f"""#!{sys.executable}
import json, os, subprocess, sys, time

argv = sys.argv[1:]
assert "BatchMode=yes" in argv, f"missing BatchMode=yes: {{argv}}"
assert "--" in argv, f"missing -- separator: {{argv}}"
sep = argv.index("--")
tail = argv[sep + 1:]
assert len(tail) == 3 and tail[1:] == ["python3", "-"], f"unsafe ssh tail: {{tail}}"

ctl = json.loads(open({str(ctl_file)!r}, encoding="utf-8").read())
with open({str(invocations_log)!r}, "a", encoding="utf-8") as lf:
    lf.write(json.dumps({{"pid": os.getpid(), "target": tail[0], "ts": time.time()}}) + "\\n")

delay = float(ctl.get("sleep_sec") or 0.0)
if delay > 0:
    time.sleep(delay)

mode = ctl.get("mode", "ok")
if mode == "auth_fail":
    sys.stderr.write("ubuntu@devbox: Permission denied (publickey).\\n")
    sys.exit(255)
if mode == "conn_fail":
    sys.stderr.write("ssh: connect to host devbox port 22: Connection timed out\\n")
    sys.exit(255)

env = dict(os.environ)
env["HOME"] = {str(rhome)!r}
env["AVI_REMOTE_PROC_ROOT"] = {str(rproc)!r}
env["AVI_DISABLE_TMUX_PROBE"] = "1"
p = subprocess.run([{sys.executable!r}, "-"], stdin=sys.stdin.buffer, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
sys.stdout.buffer.write(p.stdout)
sys.stderr.buffer.write(p.stderr)
sys.exit(p.returncode)
""",
                encoding="utf-8",
            )
            os.chmod(fake_ssh, 0o755)

            wav_bytes = _make_voiced_wav(0.6)
            fg_win = ForegroundWindow(
                101,
                "WindowsTerminal.exe",
                f"⠋ claude · ~/repos/cloud-engine [#{rem_sid[:8]}] [@devbox]",
            )
            plat = _FakeVoicePlatform(wav_bytes, fg_win)
            cap_asr = _CaptureAsr()

            cfg = Config(
                refiner=RefinerConfig(provider="deepseek"),
                remote=RemoteConfig(
                    hosts=(RemoteHostConfig(name="devbox", ssh_target="ubuntu@devbox"),),
                    timeout_ms=220,
                    ssh_bin=str(fake_ssh),
                ),
            )
            env = {
                "HOME": str(lhome),
                "HOSTNAME": "local-workstation",
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                "DEEPSEEK_API_KEY": "sk-fake-deepseek-test",
                "DASHSCOPE_API_KEY": "sk-fake-dashscope-test",
            }
            fake_clock = [1000.0]
            rem_mgr = RemoteSshManager(
                cfg.remote,
                env=env,
                runtime_dir=lrt,
                clock_fn=lambda: fake_clock[0],
                hard_timeout_sec=1.5,
                enable_multiplexing=False,
            )
            tracker = SessionTracker(
                home_dir=lhome,
                runtime_dir=lrt,
                proc_root=root / "empty_proc",
                env=env,
                remote_cfg=cfg.remote,
                remote_manager=rem_mgr,
            )
            cap_ref = _CaptureRefiner(tracker)
            daemon = VoiceInputDaemon(
                cfg,
                env=env,
                runtime_dir=lrt,
                platform=plat,
                tracker=tracker,
                asr_provider=cap_asr,
                refiner=cap_ref,
                clock_fn=lambda: fake_clock[0],
            )
            try:
                # 1. Start recording -> triggers async prefetch (120ms); spinner changes before stop -> 0ms prefetch hit!
                r_start = daemon.handle_trigger("ptt_start", fg_win)
                self.assertTrue(r_start["ok"])
                pf_deadline = time.monotonic() + 2.5
                while time.monotonic() < pf_deadline and (
                    "devbox" not in rem_mgr.breaker_snapshot() or bool(rem_mgr._inflight)
                ):
                    time.sleep(0.02)
                self.assertIn("devbox", rem_mgr.breaker_snapshot(), "start_recording must trigger async prefetch!")
                fake_clock[0] += 2.0
                plat.fg_win = ForegroundWindow(
                    101,
                    "WindowsTerminal.exe",
                    f"⠸ claude · ~/repos/cloud-engine [#{rem_sid[:8]}] [@devbox]",
                )
                r_stop = daemon.handle_trigger("ptt_stop", plat.fg_win)
                self.assertTrue(r_stop["ok"])

                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline and not daemon.last_result:
                    time.sleep(0.02)
                self.assertEqual(len(plat.pasted), 1, f"last_result={daemon.last_result!r}, r_stop={r_stop!r}")
                self.assertEqual(len(cap_ref.calls), 1)
                self.assertEqual(cap_ref.calls[0]["session_key"], f"remote:devbox:claude:{rem_sid}")
                self.assertIn("RemoteSshManager", str(cap_ref.calls[0]["chunks"]))
                # Because prefetch finished during recording, Stage 1 sync_ms is near-0ms!
                self.assertLess(daemon.last_result["sync_ms"], 50)

                # 2. Cold `<= timeout_ms` (220ms) graceful fallback + background ETag cache warm-up!
                other_sid = "87654321-bbbb-4ccc-8ddd-eeeeffff0000"
                _write_claude_jsonl(
                    rhome,
                    other_sid,
                    str(rproj),
                    [("user", "COLD_WARMUP_TURN_CONTENT")],
                    mtime=4000.0,
                )
                ctl_file.write_text(json.dumps({"sleep_sec": 0.38, "mode": "ok"}), encoding="utf-8")
                t_cold0 = time.monotonic()
                snap_timeout = tracker.sync_session(
                    window_title=f"claude [#{other_sid[:8]}] [@devbox]"
                )
                elapsed_cold_ms = (time.monotonic() - t_cold0) * 1000.0
                self.assertIsNone(snap_timeout)  # Timed out cleanly at ~220ms without blocking!
                self.assertLess(elapsed_cold_ms, 340.0)

                # Wait for the background prefetch worker (380ms sleep + subprocess startup) to finish warming ETag cache
                warm_deadline = time.monotonic() + 2.5
                while time.monotonic() < warm_deadline and (
                    bool(rem_mgr._inflight) or "devbox" not in rem_mgr._completed
                ):
                    time.sleep(0.02)
                ctl_file.write_text(json.dumps({"sleep_sec": 0.01, "mode": "ok"}), encoding="utf-8")
                snap_warmed = tracker.sync_session(
                    window_title=f"claude [#{other_sid[:8]}] [@devbox]"
                )
                assert snap_warmed is not None
                self.assertEqual(snap_warmed.clean_turns, (("user", "COLD_WARMUP_TURN_CONTENT"),))

                # 3. Host Circuit Breaker: 2 connection failures -> 60s open cooldown -> 0ms skip -> half-open recovery on prefetch!
                ctl_file.write_text(json.dumps({"sleep_sec": 0.0, "mode": "conn_fail"}), encoding="utf-8")
                self.assertIsNone(tracker.sync_session(window_title=f"claude [#{rem_sid[:8]}] [@devbox]"))
                self.assertFalse(rem_mgr.breaker_snapshot()["devbox"]["open"])
                self.assertIsNone(tracker.sync_session(window_title=f"claude [#{rem_sid[:8]}] [@devbox]"))
                self.assertTrue(rem_mgr.breaker_snapshot()["devbox"]["open"])

                calls_before_open = len(invocations_log.read_text(encoding="utf-8").splitlines())
                t_skip0 = time.monotonic()
                self.assertIsNone(tracker.sync_session(window_title=f"claude [#{rem_sid[:8]}] [@devbox]"))
                self.assertLess((time.monotonic() - t_skip0) * 1000.0, 15.0)
                calls_after_open = len(invocations_log.read_text(encoding="utf-8").splitlines())
                self.assertEqual(calls_after_open, calls_before_open, "open breaker must spawn 0 ssh processes!")

                # Advance past 60s cooldown: half-open probes ONLY on start_recording prefetch, then closes on success!
                fake_clock[0] += 65.0
                ctl_file.write_text(json.dumps({"sleep_sec": 0.01, "mode": "ok"}), encoding="utf-8")
                self.assertTrue(
                    tracker.prefetch_remote_session(
                        f"claude [#{rem_sid[:8]}] [@devbox]", seq_id=99
                    )
                )
                ho_deadline = time.monotonic() + 2.5
                while time.monotonic() < ho_deadline and (
                    rem_mgr.breaker_snapshot()["devbox"]["tripped"] or bool(rem_mgr._inflight)
                ):
                    time.sleep(0.02)
                self.assertFalse(rem_mgr.breaker_snapshot()["devbox"]["tripped"])
                # Consume the half-open prefetched snapshot (0ms!)
                snap_half_open = tracker.sync_session(window_title=f"claude [#{rem_sid[:8]}] [@devbox]")
                self.assertIsNotNone(snap_half_open)

                # Auth failure immediately trips 300s cooldown on first error!
                ctl_file.write_text(json.dumps({"sleep_sec": 0.0, "mode": "auth_fail"}), encoding="utf-8")
                self.assertIsNone(tracker.sync_session(window_title=f"claude [#{rem_sid[:8]}] [@devbox]"))
                bs_auth = rem_mgr.breaker_snapshot()["devbox"]
                self.assertTrue(bs_auth["open"])
                self.assertGreaterEqual(bs_auth["cooldown_remaining_sec"], 290.0)

                # Reset cooldown and verify mid-recording `handle_cancel` kills slow `fake_ssh` with zero orphans!
                fake_clock[0] += 310.0
                ctl_file.write_text(json.dumps({"sleep_sec": 10.0, "mode": "ok"}), encoding="utf-8")
                prev_lines = len(invocations_log.read_text(encoding="utf-8").splitlines())
                r_st_slow = daemon.handle_trigger("ptt_start", fg_win)
                self.assertTrue(r_st_slow["ok"])
                deadline_sp = time.monotonic() + 2.0
                while time.monotonic() < deadline_sp:
                    cur_lines = invocations_log.read_text(encoding="utf-8").splitlines()
                    if len(cur_lines) > prev_lines:
                        break
                    time.sleep(0.02)
                slow_pid = int(json.loads(invocations_log.read_text(encoding="utf-8").splitlines()[-1])["pid"])
                r_can = daemon.handle_cancel()
                self.assertTrue(r_can["ok"])

                dead_deadline = time.monotonic() + 1.5
                while time.monotonic() < dead_deadline:
                    try:
                        os.kill(slow_pid, 0)
                        time.sleep(0.03)
                    except ProcessLookupError:
                        break
                with self.assertRaises(ProcessLookupError):
                    os.kill(slow_pid, 0)

                # 4. `doctor` offline (zero SSH calls) vs `doctor --remote --json`!
                ctl_file.write_text(json.dumps({"sleep_sec": 0.01, "mode": "ok"}), encoding="utf-8")
                calls_pre_doc = len(invocations_log.read_text(encoding="utf-8").splitlines())
                doc_out_off = io.StringIO()
                with (
                    mock.patch.object(cli_mod.sys, "platform", "linux"),
                    mock.patch.object(cli_mod, "resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                    mock.patch.object(cli_mod, "is_wsl", return_value=True),
                    mock.patch.object(
                        cli_mod,
                        "probe_wsl_host",
                        return_value={
                            "powershell_ok": True,
                            "ps_version": "5.1",
                            "wasapi_ok": True,
                            "pulse_rdp_ok": True,
                            "winmm_devs": 1,
                            "fg_process": "WindowsTerminal.exe",
                            "fg_title": f"claude [#{rem_sid[:8]}] [@devbox]",
                        },
                    ),
                    mock.patch("sys.stdout", doc_out_off),
                ):
                    rc_off = cli_mod._cmd_doctor(cfg, env, ping=False, remote_ping=False, as_json=True)
                self.assertEqual(rc_off, 0)
                self.assertEqual(
                    len(invocations_log.read_text(encoding="utf-8").splitlines()),
                    calls_pre_doc,
                    "offline doctor must never spawn ssh!",
                )
                rep_off = json.loads(doc_out_off.getvalue())
                self.assertTrue(rep_off["remote"]["enabled"])
                self.assertTrue(rep_off["remote"]["ssh_bin_ok"])
                self.assertTrue(rep_off["session"]["session_key"].startswith("remote:devbox:"))

                doc_out_rem = io.StringIO()
                with (
                    mock.patch.object(cli_mod.sys, "platform", "linux"),
                    mock.patch.object(cli_mod, "resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                    mock.patch.object(cli_mod, "is_wsl", return_value=True),
                    mock.patch.object(
                        cli_mod,
                        "probe_wsl_host",
                        return_value={
                            "powershell_ok": True,
                            "ps_version": "5.1",
                            "wasapi_ok": True,
                            "pulse_rdp_ok": True,
                            "winmm_devs": 1,
                            "fg_process": "WindowsTerminal.exe",
                            "fg_title": "Ubuntu",
                        },
                    ),
                    mock.patch("sys.stdout", doc_out_rem),
                ):
                    rc_rem = cli_mod._cmd_doctor(cfg, env, ping=False, remote_ping=True, as_json=True)
                self.assertEqual(rc_rem, 0)
                rep_rem = json.loads(doc_out_rem.getvalue())
                self.assertEqual(rep_rem["ping"]["remote"]["devbox"]["status"], "OK")
            finally:
                daemon.close()

        # 5. Native non-WSL2 Linux desktop clear error boundary on `create_platform()`
        with mock.patch("agent_voice_input.daemon.sys.platform", "linux"), mock.patch(
            "agent_voice_input.daemon.is_wsl", return_value=False
        ):
            with self.assertRaisesRegex(RuntimeError, "Linux desktop host is not yet supported"):
                create_platform(Config(), env={})

    def test_codex_cold_review_regressions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rhome = root / "rhome"
            rproc = root / "rproc"
            lhome = root / "lhome"
            lproc = root / "lproc"
            lrt = root / "lrt"
            proj_a = rhome / "repos" / "svc-alpha"
            proj_b = rhome / "repos" / "svc-beta"
            notes_dir = rhome / "notes" / "arch-folder"
            for d in (rhome, rproc, lhome, lproc, lrt, proj_a, proj_b, notes_dir):
                d.mkdir(parents=True, exist_ok=True)

            # 1. Standalone `integrations/remote/remote-title-hook.sh` reads hook JSON from stdin & writes OSC 2 to PTY!
            hook_sh = REPO_ROOT / "integrations" / "remote" / "remote-title-hook.sh"
            master_fd, slave_fd = pty.openpty()
            try:
                slave_tty = os.ttyname(slave_fd)
                os.set_blocking(master_fd, False)
                cl_sid = "12345678-1111-4222-8333-444455556666"
                p_cl = subprocess.run(
                    ["bash", str(hook_sh), "claude"],
                    input=json.dumps({"session_id": cl_sid, "cwd": str(proj_a)}).encode("utf-8"),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env={
                        **os.environ,
                        "AVI_TEST_TTY_PATH": slave_tty,
                        "AGENT_VOICE_INPUT_HOST_TAG": "gpu-box",
                        "TMUX_PANE": "",
                    },
                    timeout=5.0,
                    check=True,
                )
                self.assertEqual(p_cl.stdout, b"")
                rlist, _, _ = select.select([master_fd], [], [], 0.3)
                self.assertTrue(rlist, "standalone remote-title-hook.sh must write OSC 2 to PTY!")
                osc_cl = os.read(master_fd, 4096).decode("utf-8")
                self.assertEqual(osc_cl, "\033]2;claude · svc-alpha [#12345678] [@gpu-box]\007")

                agy_cid = "abcdef12-1111-4222-8333-444455556666"
                p_agy = subprocess.run(
                    ["bash", str(hook_sh), "agy"],
                    input=json.dumps({"conversationId": agy_cid, "workspacePaths": [str(proj_b)]}).encode("utf-8"),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env={
                        **os.environ,
                        "AVI_TEST_TTY_PATH": slave_tty,
                        "AGENT_VOICE_INPUT_HOST_TAG": "gpu-box",
                        "TMUX_PANE": "",
                    },
                    timeout=5.0,
                    check=True,
                )
                self.assertEqual(json.loads(p_agy.stdout.decode("utf-8")), {})
                rlist2, _, _ = select.select([master_fd], [], [], 0.3)
                self.assertTrue(rlist2)
                osc_agy = os.read(master_fd, 4096).decode("utf-8")
                self.assertEqual(osc_agy, "\033]2;agy · svc-beta [#abcdef12] [@gpu-box]\007")
            finally:
                os.close(master_fd)
                os.close(slave_fd)

            # 2. Truncated gzip stream missing CRC32/ISIZE trailer MUST be rejected!
            valid_gz = gzip.compress(b'{"ok":true,"pong":true}', compresslevel=1)
            trunc_gz = valid_gz[:-4]
            trunc_framed = _remote_probe.PROBE_FRAME_MAGIC + struct.pack(">I", len(trunc_gz)) + trunc_gz
            with self.assertRaisesRegex(ValueError, "truncated gzip"):
                decode_probe_stdout(trunc_framed)

            # 3. Symlink-to-$HOME (`~/sym_home`), `$HOME/.git` dotfiles repo, and symlinked `.jsonl` outside `.claude/projects`!
            sym_home = rhome / "sym_home"
            os.symlink(str(rhome), sym_home)
            (rhome / ".git").mkdir(exist_ok=True)
            (notes_dir / "clean_doc.md").write_text("# Doc\n", encoding="utf-8")
            outside_secret = rhome / "outside_secret.jsonl"
            outside_secret.write_text(
                json.dumps(
                    {
                        "sessionId": "eeeeeeee-0000-4000-8000-000000000000",
                        "cwd": str(proj_a),
                        "isSidechain": False,
                        "type": "user",
                        "message": {"role": "user", "content": "OUTSIDE_SYMLINK_LEAK"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            enc_a = encode_claude_project_dir(str(proj_a))[0]
            cl_pdir = rhome / ".claude" / "projects" / enc_a
            cl_pdir.mkdir(parents=True, exist_ok=True)
            os.symlink(str(outside_secret), cl_pdir / "eeeeeeee-0000-4000-8000-000000000000.jsonl")

            r_sym = _remote_probe.run_probe(
                {"v": 1, "cwd_hint": "~/sym_home"},
                env={"HOME": str(rhome), "AVI_REMOTE_PROC_ROOT": str(rproc), "AVI_DISABLE_TMUX_PROBE": "1"},
                home_dir=rhome,
                proc_root=rproc,
            )
            self.assertFalse(r_sym["ok"])
            self.assertEqual(r_sym["reason"], "broad_cwd")

            r_evil_lnk = _remote_probe.run_probe(
                {"v": 1, "token": "eeeeeeee", "cli": "claude"},
                env={"HOME": str(rhome), "AVI_REMOTE_PROC_ROOT": str(rproc), "AVI_DISABLE_TMUX_PROBE": "1"},
                home_dir=rhome,
                proc_root=rproc,
            )
            self.assertFalse(r_evil_lnk["ok"])

            with mock.patch.object(
                _remote_probe,
                "_read_remote_git_status",
                side_effect=AssertionError("$HOME/.git dotfiles repo must never trigger git status!"),
            ):
                r_dotfiles = _remote_probe.run_probe(
                    {"v": 1, "cwd_hint": "~/notes/arch-folder"},
                    env={"HOME": str(rhome), "AVI_REMOTE_PROC_ROOT": str(rproc), "AVI_DISABLE_TMUX_PROBE": "1"},
                    home_dir=rhome,
                    proc_root=rproc,
                )
            self.assertTrue(r_dotfiles["ok"])
            self.assertIn("clean_doc.md", r_dotfiles["shallow_files"])

            # 4. Unscoped non-generic task title (`ssh.exe` invisible to WSL) blocks BOTH single local Agent & single attached `tmux`!
            #    Plus foreground `ssh` on `window_tty` beats colliding 8-hex `[#tag]` & suspended `T`-state CLI,
            #    while unscoped background `ssh` NEVER triggers outbound SSH from an untitled local shell!
            pts_l1 = root / "pts_l1"
            pts_l2 = root / "pts_l2"
            for p, mt in ((pts_l1, 1000.0), (pts_l2, 3000.0)):
                p.write_text("", encoding="utf-8")
                os.utime(p, (mt, mt))

            l_sid = "aaaa1111-2222-4333-8444-555566667777"
            l_repo = lhome / "repos" / "local-svc"
            l_repo.mkdir(parents=True)
            _write_claude_jsonl(lhome, l_sid, str(l_repo), [("user", "local secret")], mtime=1000.0)
            _write_fake_proc(lproc, 301, comm="claude", cwd=str(l_repo), cmdline=["claude"], fds={"0": str(pts_l1)})

            # Suspended (`T`) claude + foreground `ssh` (`pgrp == tpgid == 402`) on `pts_l2`
            pdir_susp = _write_fake_proc(
                lproc, 401, comm="claude", cwd=str(l_repo), cmdline=["claude"], fds={"0": str(pts_l2)}
            )
            (pdir_susp / "stat").write_text("401 (claude) T 1 401 401 0 402 0 0 0 0 0 0 0 0 0 20 0 1 0 100\n", encoding="utf-8")
            pdir_fg_ssh = _write_fake_proc(
                lproc, 402, comm="ssh", cwd=str(lhome), cmdline=["ssh", "devbox"], fds={"0": str(pts_l2)}
            )
            (pdir_fg_ssh / "stat").write_text("402 (ssh) S 1 402 402 0 402 0 0 0 0 0 0 0 0 0 20 0 1 0 100\n", encoding="utf-8")

            rem_cfg = RemoteConfig(hosts=(RemoteHostConfig(name="devbox", ssh_target="ubuntu@devbox"),))
            tr_local = SessionTracker(
                home_dir=lhome,
                runtime_dir=lrt,
                proc_root=lproc,
                env={"HOME": str(lhome), "HOSTNAME": "mypc"},
                local_hostnames=("mypc",),
                remote_cfg=rem_cfg,
                tmux_probe_fn=lambda: TmuxServerSnapshot(
                    clients=(TmuxClientInfo("$0", "@0", "%0", 2000.0, "/dev/pts/88", 299, True, "/tmp/m.sock"),),
                    panes=(TmuxPaneInfo("%0", "$0", "@0", True, True, 301, str(pts_l1), "/tmp/m.sock", str(l_repo), "zsh"),),
                ),
            )
            try:
                # Non-generic unrelated task title in native `ssh.exe` tab -> returns None despite single attached `tmux`!
                self.assertIsNone(tr_local.resolve_session("Fix remote billing service", window_process="WindowsTerminal.exe"))
                self.assertIsNone(tr_local.resolve_session("Windows PowerShell", window_process="powershell.exe"))

                # Without tmux, non-generic unrelated title STILL returns None despite single background local Agent!
                tr_no_tmux = SessionTracker(
                    home_dir=lhome,
                    runtime_dir=lrt,
                    proc_root=lproc,
                    env={"HOME": str(lhome), "HOSTNAME": "mypc"},
                    local_hostnames=("mypc",),
                    remote_cfg=rem_cfg,
                    tmux_probe_fn=lambda: TmuxServerSnapshot(),
                )
                try:
                    self.assertIsNone(tr_no_tmux.resolve_session("Fix remote billing service", window_process="WindowsTerminal.exe"))
                    # Unscoped `"Ubuntu"` with newer background `ssh` on `pts_l2` vetoes local crosstalk (`None`),
                    # and NEVER authorizes an outbound `remote` match to `devbox`!
                    self.assertIsNone(tr_no_tmux.resolve_session("Ubuntu", window_process="WindowsTerminal.exe"))
                    # Known `window_tty=pts_l2` running foreground `ssh` beats colliding `[#aaaa1111]` AND suspended `claude`!
                    m_tty_ssh = tr_no_tmux.resolve_session(
                        f"claude [#{l_sid[:8]}]", window_tty=str(pts_l2)
                    )
                    assert m_tty_ssh is not None
                    self.assertEqual((m_tty_ssh.origin, m_tty_ssh.host_name), ("remote", "devbox"))
                finally:
                    tr_no_tmux.close()
            finally:
                tr_local.close()

            # 5. Same-host session switch (`A` -> `B`), non-default `context_max_chars = 100_000` chunk parity,
            #    empty `files_included=True` cache eviction, and `run_pipeline_once` (`once`) remote support!
            sid_a = "1111aaaa-0000-4000-8000-000000000001"
            sid_b = "2222bbbb-0000-4000-8000-000000000002"
            turns_b = [
                ("user" if i % 2 == 0 else "assistant", f"turn_b_{i:03d} " + ("z" * 2400))
                for i in range(70)
            ]
            _write_claude_jsonl(rhome, sid_a, str(proj_a), [("user", "SESSION_A_ONLY")], mtime=1000.0)
            jf_b = _write_claude_jsonl(rhome, sid_b, str(proj_b), turns_b, mtime=5000.0)
            _write_fake_proc(rproc, 777, comm="claude", cwd=str(proj_b), cmdline=["claude", "--session-id", sid_b])

            fake_ssh2 = root / "fake_ssh2"
            fake_ssh2.write_text(
                f"""#!{sys.executable}
import os, subprocess, sys
env = dict(os.environ)
env["HOME"] = {str(rhome)!r}
env["AVI_REMOTE_PROC_ROOT"] = {str(rproc)!r}
env["AVI_DISABLE_TMUX_PROBE"] = "1"
p = subprocess.run([{sys.executable!r}, "-"], stdin=sys.stdin.buffer, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
sys.stdout.buffer.write(p.stdout)
sys.exit(p.returncode)
""",
                encoding="utf-8",
            )
            os.chmod(fake_ssh2, 0o755)

            cfg_100k = Config(
                refiner=RefinerConfig(provider="off", context_max_chars=100_000),
                remote=RemoteConfig(
                    hosts=(RemoteHostConfig(name="devbox", ssh_target="ubuntu@devbox"),),
                    timeout_ms=1500,
                    ssh_bin=str(fake_ssh2),
                ),
            )
            tr_sw = SessionTracker(
                home_dir=lhome,
                runtime_dir=lrt,
                proc_root=root / "empty_proc",
                env={"HOME": str(lhome), "HOSTNAME": "mypc"},
                remote_cfg=cfg_100k.remote,
                context_max_chars=100_000,
            )
            try:
                # Cache session A first via its explicit cwd
                snap_a = tr_sw.sync_session(window_title="xhx@devbox: ~/repos/svc-alpha")
                assert snap_a is not None
                self.assertEqual(snap_a.match.session_id, sid_a)

                # Switch to another window on the SAME host with only `[@devbox]` (no cwd_hint):
                # MUST NOT send A's `~/repos/svc-alpha` as `cwd_hint`; MUST resolve live session B!
                snap_b = tr_sw.sync_session(window_title="claude [@devbox]")
                assert snap_b is not None
                self.assertEqual(snap_b.match.session_id, sid_b)

                # Verify 100% byte-identical chunks at non-default `max_chars=100_000`!
                got_chunks, _ = tr_sw.get_refiner_context(snap_b.session_key, max_chars=100_000)
                exp_chunks = chunk_turns_for_refiner(turns_b, max_chars=100_000)
                self.assertEqual(got_chunks, exp_chunks)

                # Verify `files_included=True` with empty lists clears stale cached filenames
                tr_sw._apply_remote_probe_response(
                    cfg_100k.remote.hosts[0],
                    {
                        "ok": True,
                        "not_modified": True,
                        "etag": tr_sw._states[snap_b.session_key].remote_etag,
                        "cli": "claude",
                        "session_id": sid_b,
                        "cwd": str(proj_b),
                        "repo_files": ["old_cached_file.py"],
                        "shallow_files": [],
                        "files_included": True,
                    },
                )
                self.assertEqual(tr_sw._states[snap_b.session_key].remote_repo_files, ["old_cached_file.py"])
                tr_sw._apply_remote_probe_response(
                    cfg_100k.remote.hosts[0],
                    {
                        "ok": True,
                        "not_modified": True,
                        "etag": tr_sw._states[snap_b.session_key].remote_etag,
                        "cli": "claude",
                        "session_id": sid_b,
                        "cwd": str(proj_b),
                        "repo_files": [],
                        "shallow_files": [],
                        "files_included": True,
                    },
                )
                self.assertEqual(tr_sw._states[snap_b.session_key].remote_repo_files, [])
            finally:
                tr_sw.close()

            # Verify `run_pipeline_once` (`once` default tracker=None) inherits `cfg.remote`!
            cap_asr_once = _CaptureAsr("测试 once 远端路由")
            res_once = run_pipeline_once(
                _make_voiced_wav(0.5),
                cfg=cfg_100k,
                asr_provider=cap_asr_once,
                window_title_override=f"claude [#{sid_b[:8]}] [@devbox]",
                dry_run=True,
                runtime_dir=lrt,
                env={"HOME": str(lhome), "HOSTNAME": "mypc", "DASHSCOPE_API_KEY": "sk-fake"},
            )
            self.assertTrue(res_once.ok)
            assert res_once.session is not None
            self.assertEqual(res_once.session.session_key, f"remote:devbox:claude:{sid_b}")
            _ = jf_b

    def test_unified_five_semantic_forks_and_wsl_slow_mount_guard(self) -> None:
        # 1. Fork #1: open-source `gemini` / `@google/gemini-cli` is NOT misclassified as `agy`,
        # while `session-hook --cli gemini` hook mode compatibility remains intact.
        self.assertIs(_match_cli_process, _remote_probe._match_cli_process)
        self.assertEqual(_match_cli_process("gemini", ["gemini"])[0], None)
        self.assertEqual(_match_cli_process("gemini-cli", ["gemini-cli"])[0], None)
        self.assertEqual(
            _match_cli_process(
                "node",
                ["node", "/usr/local/lib/node_modules/@google/gemini-cli/dist/index.js"],
            )[0],
            None,
        )
        self.assertEqual(_match_cli_process("agy", ["agy"])[0], "agy")
        self.assertEqual(_match_cli_process("antigravity", ["antigravity"])[0], "agy")
        self.assertIsNone(parse_window_title_hint("[gemini:01234567]"))
        self.assertEqual(
            _remote_probe._parse_title_hint("[gemini:01234567]"),
            ("", False, None, False),
        )
        h_hash = parse_window_title_hint("Gemini [#abcdef12]")
        assert h_hash is not None
        self.assertIsNone(h_hash.cli)
        self.assertEqual(
            _remote_probe._parse_title_hint("Gemini [#abcdef12]"),
            ("abcdef12", False, None, False),
        )
        buf_hook = io.StringIO()
        with mock.patch("sys.stdout", buf_hook):
            rc_hook = VST.run_session_hook("gemini", stdin_text="{}", env={"HOME": "/nonexistent"})
        self.assertEqual(rc_hook, 0)
        self.assertEqual(buf_hook.getvalue(), "{}\n")

        # 2. Fork #2: `_normalize_cwd` strips `file://` URIs AND blocks raw or percent-encoded NUL bytes.
        self.assertIs(_normalize_cwd, _remote_probe._normalize_cwd)
        self.assertEqual(
            _normalize_cwd("file://macbook.local/Users/xhx/my%20repo/"),
            "/Users/xhx/my repo",
        )
        self.assertEqual(_normalize_cwd("file:///tmp/work/"), "/tmp/work")
        self.assertEqual(_normalize_cwd("/tmp/bad\x00path"), "")
        self.assertEqual(_normalize_cwd("file:///tmp/bad%00path"), "")
        self.assertEqual(_normalize_cwd("file://[broken/path"), "")
        self.assertEqual(
            _remote_probe.run_probe({"v": 1, "cwd_hint": "file://[broken/path"}, env={"HOME": "/tmp"})["ok"],
            False,
        )

        # Exact max_bytes + 1 newline boundary parity in _read_bounded_line
        buf_exact = io.BytesIO(b"a" * 16 + b"\nnext_line\n")
        raw_16, exc_16 = _remote_probe._read_bounded_line(buf_exact, max_bytes=16)
        self.assertTrue(exc_16)
        self.assertEqual(raw_16, b"a" * 16)
        self.assertEqual(_remote_probe._read_bounded_line(buf_exact, max_bytes=16), (b"next_line\n", False))

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            real_home = root / "real_home"
            real_home.mkdir()
            link_home = root / "link_home"
            os.symlink(real_home, link_home)
            real_proj = real_home / "repos" / "alpha"
            (real_proj / "sub").mkdir(parents=True)
            link_to_home = root / "alias_to_home"
            os.symlink(real_home, link_to_home)
            win_link = real_home / "repos" / "win_mount_link"
            os.symlink("/mnt/d/sleeping_hdd/proj", win_link)

            # 3. Fork #3: `_is_broad_or_unsafe_workspace_dir` handles symlinked $HOME, relative '.', and WSL zero-lstat.
            self.assertIs(
                _is_broad_or_unsafe_workspace_dir,
                _remote_probe._is_broad_or_unsafe_workspace_dir,
            )
            for bad_dir in (
                "",
                ".",
                "relative/sub",
                "/",
                "/tmp",
                "/etc",
                "/opt",
                "/mnt",
                "/home/alice",
                "/Users/alice",
                str(real_home),
                str(link_home),
                str(link_to_home),
            ):
                self.assertTrue(
                    _is_broad_or_unsafe_workspace_dir(bad_dir, link_home),
                    f"Expected {bad_dir!r} to be rejected as broad/unsafe",
                )
            self.assertFalse(_is_broad_or_unsafe_workspace_dir(str(real_proj), link_home))

            orig_stat = os.stat
            orig_lstat = os.lstat
            orig_realpath = os.path.realpath

            def _forbid_mnt_stat(p: object, *a: object, **kw: object):
                if isinstance(p, (str, bytes, os.PathLike)):
                    s = os.fsdecode(p)
                    if s == "/mnt" or s.startswith("/mnt/"):
                        raise AssertionError(f"Forbidden stat on WSL /mnt path: {s}")
                    if kw.get("follow_symlinks", True):
                        try:
                            lst = orig_lstat(s)
                            if stat.S_ISLNK(lst.st_mode):
                                target = os.readlink(s)
                                if target == "/mnt" or target.startswith("/mnt/"):
                                    raise AssertionError(
                                        f"Forbidden stat following symlink {s} -> {target}"
                                    )
                        except OSError:
                            pass
                return orig_stat(p, *a, **kw)  # type: ignore[arg-type]

            def _forbid_mnt_lstat(p: object, *a: object, **kw: object):
                s = os.fsdecode(p)  # type: ignore[arg-type]
                if s == "/mnt" or s.startswith("/mnt/"):
                    raise AssertionError(f"Forbidden lstat on WSL /mnt path: {s}")
                return orig_lstat(p, *a, **kw)  # type: ignore[arg-type]

            def _forbid_mnt_realpath(p: object, *a: object, **kw: object):
                s = os.fsdecode(p)  # type: ignore[arg-type]
                if s == "/mnt" or s.startswith("/mnt/"):
                    raise AssertionError(f"Forbidden realpath on WSL /mnt path: {s}")
                return orig_realpath(p, *a, **kw)  # type: ignore[arg-type]

            with mock.patch("os.stat", side_effect=_forbid_mnt_stat), mock.patch(
                "os.lstat", side_effect=_forbid_mnt_lstat
            ), mock.patch(
                "os.path.realpath", side_effect=_forbid_mnt_realpath
            ), mock.patch("agent_voice_input.session_ctx._HOST_IS_WSL", True):
                self.assertTrue(
                    _is_broad_or_unsafe_workspace_dir("/mnt/d/sleeping_hdd/proj", link_home, wsl=True)
                )
                self.assertTrue(
                    _is_broad_or_unsafe_workspace_dir(str(win_link), link_home, wsl=True)
                )
                self.assertFalse(
                    _is_broad_or_unsafe_workspace_dir(str(real_proj), link_home, wsl=True)
                )
                # `missing/../win_link` must NOT collapse onto unchecked `win_link` -> `/mnt/d/...`
                tricky_rel_link = f"{real_home}/repos/missing_dir/../win_mount_link"
                self.assertEqual(_resolve_safe_local_dir(tricky_rel_link, wsl=True), "")
                # `file://server/share/repo` UNC must be rejected in WSL `_cwd_variants` / `_disambiguate_candidates`
                self.assertEqual(_remote_probe._cwd_variants("file://server/share/repo", wsl=True), set())
                c1 = SessionMatch(cli="claude", session_id="s1", jsonl_path="/tmp/a.jsonl", cwd="/share/repo", pts_mtime=10.0)
                c2 = SessionMatch(cli="claude", session_id="s2", jsonl_path="/tmp/b.jsonl", cwd=str(real_proj), pts_mtime=20.0)
                picked_unc = _disambiguate_candidates([c1, c2], "claude", "file://server/share/repo", link_home)
                assert picked_unc is not None
                self.assertEqual(picked_unc.session_id, "s2")
                # Local candidate disambiguation and proc scan on `/mnt/d/...` must never invoke raw `realpath` on `/mnt/`
                c_mnt = SessionMatch(cli="codex", session_id="s3", jsonl_path="/tmp/c.jsonl", cwd="/mnt/d/demo", pts_mtime=5.0)
                picked_mnt = _disambiguate_candidates([c_mnt, c2], "claude", "/mnt/d/demo", link_home)
                assert picked_mnt is not None
                self.assertEqual(encode_claude_project_dir("/mnt/d/demo", wsl=True), [])
                # Create `real_home/.codex/sessions` BEFORE `_query_pid_codex_rollout_paths` so fd scan runs!
                (real_home / ".codex" / "sessions").mkdir(parents=True, exist_ok=True)
                fake_proc = root / "wsl_proc"
                _write_fake_proc(
                    fake_proc,
                    777,
                    comm="codex",
                    cwd=str(real_proj),
                    cmdline=["codex"],
                    fds={"4": "/mnt/d/rollout-test.jsonl"},
                )
                # Local HOME + slow-mount `/mnt/d/rollout-test.jsonl` open fd: zero `/mnt/` realpath or stat!
                self.assertEqual(_query_pid_codex_rollout_paths(777, real_home, fake_proc), [])
                self.assertEqual(scan_proc_root_sessions(real_home, fake_proc), [])

                # Slow-mount HOME (direct `/mnt/c/...` AND symlink `/tmp/.../wsl_home_link -> /mnt/c/...`) + local cwd:
                wsl_home_link = root / "wsl_home_link"
                os.symlink("/mnt/c/Users/demo", wsl_home_link)
                for slow_h in (Path("/mnt/c/Users/demo"), wsl_home_link):
                    self.assertEqual(scan_proc_root_sessions(slow_h, fake_proc), [])
                    self.assertEqual(
                        _lookup_by_short_prefix(
                            TitleHint("abcd1234", False, "agy", True),
                            slow_h,
                            fake_proc,
                            [],
                        ),
                        None,
                    )
                    self.assertEqual(
                        _scan_nonproc_fallback_sessions(slow_h, [], check_pid_alive=False),
                        [],
                    )
                    self.assertEqual(
                        _find_storage_session_for_cli(
                            "agy",
                            home=slow_h,
                            proc_root=fake_proc,
                            eff_cwd=str(real_proj),
                            window_title="agy",
                        ),
                        None,
                    )
                    self.assertEqual(
                        _collect_codex_storage_matches(
                            slow_h, matched_by="test", eff_cwd=str(real_proj)
                        ),
                        [],
                    )
                    self.assertEqual(
                        _collect_claude_storage_matches(
                            slow_h, matched_by="test", eff_cwd=str(real_proj), filter_cwd=True
                        ),
                        [],
                    )

                # Symlinked subdir/file inside local HOME pointing to `/mnt/d/...` must NEVER trigger `/mnt/` I/O
                os.symlink("/mnt/d/sessions_2027", real_home / ".codex" / "sessions" / "2027")
                os.symlink("/mnt/d/idx.jsonl", real_home / ".codex" / "session_index.jsonl")
                self.assertEqual(_remote_probe._load_codex_thread_names(real_home, wsl=True), {})
                claude_proj_dir = real_home / ".claude" / "projects" / "-share-repo"
                claude_proj_dir.mkdir(parents=True, exist_ok=True)
                os.symlink("/mnt/d/bad.jsonl", claude_proj_dir / "bad-session.jsonl")
                self.assertEqual(
                    _collect_claude_storage_matches(
                        real_home, matched_by="test", eff_cwd="/share/repo", filter_cwd=True
                    ),
                    [],
                )

                # Agy symlinked presence `<cid>.lock -> /mnt/d/...` and symlinked `transcript_full.jsonl -> /mnt/d/...`
                agy_cid = "deadbeef-1111-4222-8333-444455556666"
                agy_pres_dir = real_home / ".gemini" / "antigravity-cli" / "presence"
                agy_pres_dir.mkdir(parents=True, exist_ok=True)
                os.symlink("/mnt/d/bad-agy.lock", agy_pres_dir / f"{agy_cid}.lock")
                self.assertFalse(is_agy_root_session_valid(agy_cid, real_home, None))

                agy_cid2 = "feedface-1111-4222-8333-444455556666"
                agy_logs2 = (
                    real_home
                    / ".gemini"
                    / "antigravity-cli"
                    / "brain"
                    / agy_cid2
                    / ".system_generated"
                    / "logs"
                )
                agy_logs2.mkdir(parents=True, exist_ok=True)
                comp_tp2 = agy_logs2 / "transcript.jsonl"
                comp_tp2.write_text(
                    json.dumps(
                        {
                            "source": "USER_EXPLICIT",
                            "type": "USER_INPUT",
                            "content": "<USER_REQUEST>hello local transcript</USER_REQUEST>",
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                os.symlink("/mnt/d/slow_full.jsonl", agy_logs2 / "transcript_full.jsonl")
                self.assertEqual(
                    _remote_probe._resolve_agy_transcript_path(real_home, agy_cid2, wsl=True),
                    comp_tp2,
                )
                self.assertEqual(_upgrade_agy_transcript_path(str(comp_tp2)), str(comp_tp2))
                tracker_agy = SessionTracker(home_dir=real_home)
                snap_agy = tracker_agy.sync_session(
                    SessionMatch(
                        cli="agy",
                        session_id=agy_cid2,
                        jsonl_path=str(comp_tp2),
                        cwd=str(real_proj),
                    )
                )
                self.assertEqual(tracker_agy._states[f"agy:{agy_cid2}"].jsonl_path, str(comp_tp2))
                self.assertEqual(len(snap_agy.clean_turns), 1)

                # Hidden subdir `.backup` under `.codex/sessions` must stay skipped in WSL just like `glob("*/*/*/...")`
                bak_sid = "01967777-8888-7777-8666-555544443333"
                bak_dir = real_home / ".codex" / "sessions" / ".backup" / "11" / "19"
                bak_dir.mkdir(parents=True, exist_ok=True)
                (bak_dir / f"rollout-2026-11-19-{bak_sid}.jsonl").write_text(
                    json.dumps(
                        {
                            "type": "session_meta",
                            "payload": {"id": bak_sid, "source": "cli", "cwd": str(real_proj)},
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                self.assertEqual(
                    _collect_codex_storage_matches(
                        real_home, matched_by="test", eff_cwd=str(real_proj)
                    ),
                    [],
                )

                # Candidate Codex `session_meta` carrying UNC `file://server/share/repo` must NEVER match or strip to `/share/repo` on WSL
                unc_sid = "01969999-8888-7777-8666-555544443333"
                unc_dir = real_home / ".codex" / "sessions" / "2026" / "11" / "19"
                unc_dir.mkdir(parents=True, exist_ok=True)
                unc_file = unc_dir / f"rollout-2026-11-19-{unc_sid}.jsonl"
                unc_file.write_text(
                    json.dumps(
                        {
                            "type": "session_meta",
                            "payload": {
                                "id": unc_sid,
                                "source": "cli",
                                "cwd": "file://server/share/repo",
                            },
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                self.assertEqual(
                    _collect_codex_storage_matches(
                        real_home, matched_by="test", eff_cwd="/share/repo"
                    ),
                    [],
                )
                fake_proc_unc = root / "wsl_proc_unc"
                _write_fake_proc(
                    fake_proc_unc,
                    778,
                    comm="codex",
                    cwd="",
                    cmdline=["codex"],
                    fds={"4": str(unc_file)},
                )
                unc_pid_hit = _find_storage_session_for_cli(
                    "codex",
                    home=real_home,
                    proc_root=fake_proc_unc,
                    eff_cwd="",
                    window_title="codex",
                    agent_pid=778,
                )
                assert unc_pid_hit is not None
                self.assertEqual(unc_pid_hit.cwd, "")

                tracker_wsl = SessionTracker(home_dir=real_home)
                st_wsl = SessionState(
                    session_key=f"codex:{unc_sid}",
                    cli="codex",
                    session_id=unc_sid,
                    jsonl_path=str(unc_file),
                )
                tracker_wsl._consume_parsed_record(
                    st_wsl,
                    {
                        "type": "session_meta",
                        "payload": {"id": unc_sid, "cwd": "file://server/share/repo"},
                    },
                )
                self.assertEqual(st_wsl.cwd, "")

            # 4. Fork #4: `_cwd_match_rank` rejects broad system dirs (`/opt`, `/etc`, symlinked `$HOME`).
            self.assertIs(_cwd_match_rank, _remote_probe._cwd_match_rank)
            self.assertEqual(_cwd_match_rank(str(real_proj), str(real_proj), link_home), 2)
            self.assertEqual(
                _cwd_match_rank(str(real_proj / "sub"), str(real_proj), link_home), 1
            )
            self.assertEqual(_cwd_match_rank("/opt/service-a", "/opt", link_home), 0)
            self.assertEqual(_cwd_match_rank("/etc/nginx", "/etc", link_home), 0)
            self.assertEqual(_cwd_match_rank(str(real_proj), str(real_home), link_home), 0)

            # 5. Fork #5: `_read_trusted_registry_json` enforces parent `0700`/non-symlink + loop read on short reads.
            self.assertIs(_read_trusted_registry_json, _remote_probe._read_trusted_registry_json)
            rt_dir = root / "rt"
            s_dir = rt_dir / "sessions"
            s_dir.mkdir(parents=True)
            os.chmod(rt_dir, 0o700)
            os.chmod(s_dir, 0o700)
            reg_path = s_dir / "claude-12345678.json"
            payload_obj = {
                "cli": "claude",
                "session_id": "12345678-1111-4222-8333-444455556666",
                "cwd": str(real_proj),
                "pad": "x" * 256,
            }
            reg_bytes = json.dumps(payload_obj).encode("utf-8")
            fd_w = os.open(str(reg_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd_w, reg_bytes)
            finally:
                os.close(fd_w)

            orig_os_read = os.read

            def _short_chunk_read(fd: int, n: int) -> bytes:
                return orig_os_read(fd, min(n, 7))

            with mock.patch("os.read", side_effect=_short_chunk_read):
                self.assertEqual(_read_trusted_registry_json(reg_path), payload_obj)

            # Loosening parent runtime dir to 0755 or 0500 must immediately reject direct reads
            os.chmod(rt_dir, 0o755)
            self.assertIsNone(_read_trusted_registry_json(reg_path))
            os.chmod(rt_dir, 0o500)
            try:
                self.assertIsNone(_read_trusted_registry_json(reg_path))
            finally:
                os.chmod(rt_dir, 0o700)
            self.assertEqual(_read_trusted_registry_json(reg_path), payload_obj)

    def test_local_session_tracker_and_subprocess_remote_probe_differential_parity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            proc_root = root / "proc"
            rt_dir = root / "rt"
            real_ws = root / "real_workspaces" / "voice-engine"
            link_ws = home / "repos" / "voice-engine"
            for d in (home, proc_root, rt_dir, real_ws, link_ws.parent):
                d.mkdir(parents=True, exist_ok=True)
            os.chmod(rt_dir, 0o700)
            os.symlink(real_ws, link_ws)

            # 1. Complex Claude Code transcript: pre-compaction, compact_boundary + summary,
            # sidechain/subagent noise, <system-reminder> vs backticked `<system-reminder>`,
            # >2KB progress line, multi-chunk eviction, and incomplete trailing half-line.
            cl_sid = "11112222-3333-4444-8555-666677778888"
            cl_dir = home / ".claude" / "projects" / encode_claude_project_dir(str(link_ws))[0]
            cl_dir.mkdir(parents=True, exist_ok=True)
            cl_file = cl_dir / f"{cl_sid}.jsonl"
            cl_records = [
                {
                    "type": "user",
                    "sessionId": cl_sid,
                    "cwd": link_ws.as_uri(),
                    "isSidechain": False,
                    "message": {"role": "user", "content": "obsolete pre-compaction turn"},
                },
                {"type": "system", "subtype": "compact_boundary"},
                {
                    "type": "user",
                    "isCompactSummary": True,
                    "message": {
                        "role": "user",
                        "content": "Compacted Claude context summary\n<system-reminder>strip me</system-reminder>",
                    },
                },
                {
                    "type": "progress",
                    "data": "P" * 2400,
                },
                {
                    "type": "user",
                    "isSidechain": True,
                    "message": {"role": "user", "content": "SUBAGENT_SIDECHAIN_LEAK"},
                },
                {
                    "type": "assistant",
                    "agentId": "sub-99",
                    "message": {"role": "assistant", "content": "AGENT_ID_LEAK"},
                },
                {
                    "type": "user",
                    "isSidechain": False,
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "Keep literal `<system-reminder>` tag in code span "
                                    "<system-reminder>HIDDEN_REMINDER</system-reminder> "
                                    "<task-notification>HIDDEN_TASK</task-notification> "
                                    + ("alpha " * 320)
                                ),
                            },
                            {"type": "tool_result", "content": "TOOL_RESULT_NOISE"},
                        ],
                    },
                },
                {
                    "type": "assistant",
                    "isSidechain": False,
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "SECRET_THINKING_CHAIN"},
                            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                            {"type": "text", "text": "Refactored _remote_probe " + ("beta " * 380)},
                        ],
                    },
                },
                {
                    "type": "user",
                    "isSidechain": False,
                    "message": {"role": "user", "content": "Final turn after chunk boundary " + ("gamma " * 320)},
                },
                {
                    "type": "assistant",
                    "isSidechain": False,
                    "message": {"role": "assistant", "content": "All 5 forks unified cleanly."},
                },
            ]
            cl_bytes = b"".join((json.dumps(r) + "\n").encode("utf-8") for r in cl_records)
            cl_bytes += b'{"type": "assistant", "message": {"role": "assistant", "content": "UNFINISHED_HALF_LINE'
            cl_file.write_bytes(cl_bytes)

            # 2. Complex Codex rollout: session_meta, response_item boilerplate stripping,
            # compacted (out-of-order retained_context + replacement_history + summary),
            # item_completed dedup transition, >2KB function_call_output noise, multi-chunk eviction.
            cx_sid = "0196abcd-ef01-7234-8567-89abcdef0123"
            cx_dir = home / ".codex" / "sessions" / "2026" / "11" / "19"
            cx_dir.mkdir(parents=True, exist_ok=True)
            cx_file = cx_dir / f"rollout-2026-11-19-{cx_sid}.jsonl"
            cx_records = [
                {
                    "type": "session_meta",
                    "payload": {"id": cx_sid, "source": "cli", "cwd": str(link_ws)},
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "# AGENTS.md instructions for repo\n..."}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    "<environment_context>cwd</environment_context>\n"
                                    "<permissions instructions>perm</permissions instructions>\n"
                                    "<recommended_plugins>plug</recommended_plugins>\n"
                                    "early turn before compaction"
                                ),
                            }
                        ],
                    },
                },
                {
                    "type": "compacted",
                    "payload": {
                        "message": "Codex compaction checkpoint summary",
                        "retained_context": {
                            "assistant_messages": [{"order": 2, "text": "retained assistant #2 " + ("delta " * 320)}],
                            "user_messages": [{"order": 1, "text": "retained user #1 " + ("epsilon " * 320)}],
                        },
                        "replacement_history": [
                            {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": "retained user #1 " + ("epsilon " * 320)}],
                            },
                            {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": "post-compact user prompt " + ("zeta " * 340)}],
                            },
                        ],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "output": "X" * 2600,
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "item": {
                            "type": "UserMessage",
                            "content": [{"type": "text", "text": "post-compact user prompt " + ("zeta " * 340)}],
                        },
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "item": {
                            "type": "AgentMessage",
                            "content": [{"type": "text", "text": "Codex final answer on SessionTracker parity."}],
                        },
                    },
                },
            ]
            cx_file.write_bytes(
                b"".join((json.dumps(r) + "\n").encode("utf-8") for r in cx_records)
            )

            # 3. Complex Antigravity (agy) transcript: USER_INPUT + metadata tail, VIEW_FILE >2KB noise,
            # CHECKPOINT compaction with preamble/tail stripping, and multi-chunk eviction.
            agy_cid = "fedcba98-7654-4321-89ab-cdef01234567"
            agy_pres = home / ".gemini" / "antigravity-cli" / "presence"
            agy_logs = home / ".gemini" / "antigravity-cli" / "brain" / agy_cid / ".system_generated" / "logs"
            agy_pres.mkdir(parents=True, exist_ok=True)
            agy_logs.mkdir(parents=True, exist_ok=True)
            agy_lock = agy_pres / f"{agy_cid}.lock"
            agy_lock.write_text("", encoding="utf-8")
            agy_file = agy_logs / "transcript_full.jsonl"
            agy_records = [
                {
                    "source": "SYSTEM",
                    "type": "SYSTEM_PROMPT",
                    "content": f"Active Workspaces:\n- {link_ws}\nCommand Working Directory: {link_ws}\n",
                },
                {
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "content": "<USER_REQUEST>old agy request</USER_REQUEST>\n<ADDITIONAL_METADATA>meta</ADDITIONAL_METADATA>",
                },
                {
                    "source": "SYSTEM",
                    "type": "CHECKPOINT",
                    "content": (
                        "# Resuming from a compaction\nHere is the progress summary below:\n"
                        "Summary of Step 1 and Step 2 governance work " + ("eta " * 480)
                        + "\n# Subagents\nsubagent log tail\n# Conversation Logs\nraw path"
                    ),
                },
                {
                    "source": "MODEL",
                    "type": "VIEW_FILE",
                    "content": "V" * 2500,
                },
                {
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "content": (
                        "<USER_REQUEST>Verify differential parity "
                        + ("theta " * 460)
                        + "</USER_REQUEST>\n<SKILL>voice-eval</SKILL>"
                    ),
                },
                {
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "content": "Both local SessionTracker and subprocess _remote_probe match byte-for-byte.",
                    "thinking": "INTERNAL_THINKING_IGNORED",
                },
            ]
            agy_file.write_bytes(
                b"".join((json.dumps(r) + "\n").encode("utf-8") for r in agy_records)
            )
            _write_fake_proc(
                proc_root,
                910,
                comm="agy",
                cwd=str(link_ws),
                cmdline=["agy"],
                fds={"8": str(agy_lock)},
            )

            probe_env = {
                **os.environ,
                "HOME": str(home),
                "AVI_REMOTE_PROC_ROOT": str(proc_root),
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt_dir),
                "AVI_DISABLE_TMUX_PROBE": "1",
            }
            max_chars = 4096
            chunk_chars = 1024

            local_tracker = SessionTracker(
                home_dir=home,
                proc_root=proc_root,
                runtime_dir=rt_dir,
                env=probe_env,
            )
            remote_tracker = SessionTracker(
                home_dir=root / "client_home",
                proc_root=root / "empty_proc",
                runtime_dir=root / "client_rt",
                env={"HOME": str(root / "client_home")},
            )
            host_cfg = RemoteHostConfig(name="paritybox", ssh_target="paritybox")
            try:
                cases = [
                    ("claude", cl_sid, cl_file, f"[claude:{cl_sid[:8]}]"),
                    ("codex", cx_sid, cx_file, f"[codex:{cx_sid[:8]}]"),
                    ("agy", agy_cid, agy_file, f"[agy:{agy_cid[:8]}]"),
                ]
                for cli_name, sid, jf_path, title_tag in cases:
                    _ = jf_path
                    local_match = local_tracker.resolve_session(window_title=title_tag)
                    assert local_match is not None, f"local resolve_session failed for {cli_name}"
                    local_snap = local_tracker.sync_session(local_match)
                    assert local_snap is not None, f"local sync failed for {cli_name}"
                    local_chunks, _ = local_tracker.get_refiner_context(
                        local_snap.session_key,
                        max_chars=max_chars,
                        chunk_chars=chunk_chars,
                        include_git_status=False,
                    )
                    self.assertGreaterEqual(
                        len(local_chunks),
                        2,
                        f"Expected multi-chunk context for {cli_name}",
                    )

                    proc = subprocess.run(
                        [sys.executable, "-"],
                        input=build_probe_stdin_payload(
                            {
                                "v": 1,
                                "title": title_tag,
                                "max_chars": max_chars,
                                "chunk_chars": chunk_chars,
                                "include_git": False,
                            }
                        ),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env=probe_env,
                        timeout=5.0,
                        check=True,
                    )
                    probe_resp = decode_probe_stdout(proc.stdout)
                    self.assertTrue(probe_resp["ok"], f"probe failed for {cli_name}: {probe_resp}")
                    self.assertEqual(probe_resp["cli"], cli_name)
                    self.assertEqual(probe_resp["session_id"], sid)

                    probe_turns = [(r, t) for r, t in probe_resp["turns"]]
                    expected_trimmed, exp_evicted = trim_turns_chunk_aligned(
                        local_snap.clean_turns,
                        max_chars=max_chars,
                        chunk_chars=chunk_chars,
                    )
                    self.assertGreater(
                        exp_evicted,
                        0,
                        f"Expected forward-anchored chunk eviction to trigger for {cli_name}",
                    )
                    self.assertEqual(probe_turns, expected_trimmed)
                    self.assertEqual(probe_resp["evicted_chunks"], exp_evicted)
                    self.assertEqual(
                        chunk_turns_for_refiner(
                            probe_turns, max_chars=max_chars, chunk_chars=chunk_chars
                        ),
                        local_chunks,
                    )

                    remote_state = remote_tracker._apply_remote_probe_response(host_cfg, probe_resp)
                    assert remote_state is not None
                    remote_chunks, _ = remote_tracker.get_refiner_context(
                        remote_state.session_key,
                        max_chars=max_chars,
                        chunk_chars=chunk_chars,
                        include_git_status=False,
                    )
                    self.assertEqual(remote_chunks, local_chunks)
                    self.assertEqual(remote_state.cwd, local_snap.match.cwd)
            finally:
                local_tracker.close()
                remote_tracker.close()


if __name__ == "__main__":
    unittest.main()
