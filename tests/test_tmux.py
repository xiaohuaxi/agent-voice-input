"""Offline mock unit tests and isolated ``tmux -L`` integration tests for multi-pane session tracking."""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import pty
import select
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Mapping
from unittest import mock
import unittest
import uuid

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agent_voice_input import session_title as VST  # noqa: E402
from agent_voice_input._tmux import (  # noqa: E402
    parse_tmux_clients_output,
    parse_tmux_panes_output,
    preferred_tmux_socket_arg,
)
from agent_voice_input.session_ctx import (  # noqa: E402
    SessionTracker,
    TmuxClientInfo,
    TmuxPaneInfo,
    TmuxServerSnapshot,
    parse_tmux_probe_output,
    parse_tmux_socket_from_env,
    probe_tmux_snapshot,
    write_session_registry_entry,
)


def _write_fake_proc(
    proc_root: Path,
    pid: int,
    *,
    comm: str,
    ppid: int = 1,
    psid: int | None = None,
    tty_nr: int = 0,
    cmdline: list[str] | tuple[str, ...] = (),
    cwd: str = "",
    fds: Mapping[str, str] | None = None,
) -> Path:
    eff_psid = pid if psid is None else psid
    pdir = proc_root / str(pid)
    (pdir / "fd").mkdir(parents=True, exist_ok=True)
    stat_line = (
        f"{pid} ({comm}) S {ppid} {eff_psid} {eff_psid} {tty_nr} "
        "0 0 0 0 0 0 0 0 0 0 20 0 1 0 100\n"
    )
    (pdir / "stat").write_text(stat_line, encoding="utf-8")
    (pdir / "comm").write_text(f"{comm}\n", encoding="utf-8")
    if cmdline:
        (pdir / "cmdline").write_bytes(b"\x00".join(a.encode("utf-8") for a in cmdline) + b"\x00")
    if cwd:
        cur_cwd = pdir / "cwd"
        if cur_cwd.is_symlink() or cur_cwd.exists():
            cur_cwd.unlink()
        os.symlink(cwd, cur_cwd)
    for fd_num, target in (fds or {}).items():
        fd_link = pdir / "fd" / str(fd_num)
        if fd_link.is_symlink() or fd_link.exists():
            fd_link.unlink()
        os.symlink(target, fd_link)
    return pdir


def _write_codex_rollout(home: Path, sid: str, cwd: str, user_text: str, mtime: float) -> Path:
    rdir = home / ".codex" / "sessions" / "2026" / "11" / "14"
    rdir.mkdir(parents=True, exist_ok=True)
    rfile = rdir / f"rollout-2026-11-14T16-00-00-{sid}.jsonl"
    lines = [
        json.dumps({"type": "session_meta", "payload": {"id": sid, "source": "cli", "cwd": cwd}}),
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {"type": "UserMessage", "content": [{"type": "text", "text": user_text}]},
                },
            },
            ensure_ascii=False,
        ),
    ]
    rfile.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.utime(rfile, (mtime, mtime))
    return rfile


def _write_claude_jsonl(home: Path, sid: str, cwd: str, user_text: str, mtime: float) -> Path:
    enc = "".join(ch if ch.isalnum() else "-" for ch in cwd)
    pdir = home / ".claude" / "projects" / enc
    pdir.mkdir(parents=True, exist_ok=True)
    jfile = pdir / f"{sid}.jsonl"
    rec = {
        "type": "user",
        "isSidechain": False,
        "sessionId": sid,
        "cwd": cwd,
        "message": {"role": "user", "content": user_text},
    }
    jfile.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
    os.utime(jfile, (mtime, mtime))
    return jfile


def _write_agy_session(home: Path, cid: str, user_text: str, mtime: float) -> tuple[Path, Path]:
    pdir = home / ".gemini" / "antigravity-cli" / "presence"
    pdir.mkdir(parents=True, exist_ok=True)
    lock_f = pdir / f"{cid}.lock"
    lock_f.write_text("", encoding="utf-8")
    ldir = home / ".gemini" / "antigravity-cli" / "brain" / cid / ".system_generated" / "logs"
    ldir.mkdir(parents=True, exist_ok=True)
    tf = ldir / "transcript_full.jsonl"
    rec = {
        "source": "USER_EXPLICIT",
        "type": "USER_INPUT",
        "content": f"<USER_REQUEST>{user_text}</USER_REQUEST>",
    }
    tf.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
    os.utime(tf, (mtime, mtime))
    return lock_f, tf


class TmuxMockUnitTests(unittest.TestCase):
    def test_tmux_env_and_probe_output_parsing_and_multi_client_selection(self) -> None:
        self.assertEqual(
            parse_tmux_socket_from_env("/tmp/tmux-1000/default,54321,0"),
            "/tmp/tmux-1000/default",
        )
        # Socket path itself containing commas must still parse cleanly via rsplit(",", 2)
        self.assertEqual(
            parse_tmux_socket_from_env("/tmp/dir,with,comma/my.sock,54321,2"),
            "/tmp/dir,with,comma/my.sock",
        )
        self.assertEqual(parse_tmux_socket_from_env(""), "")
        self.assertEqual(
            VST.parse_tmux_env({"TMUX": "/tmp/tmux-1000/dev,99,0", "TMUX_PANE": "%12"}),
            ("/tmp/tmux-1000/dev", "%12"),
        )
        self.assertEqual(
            VST.parse_tmux_env({"TMUX": "/tmp/tmux-1000/dev,99,0", "TMUX_PANE": "bad"}),
            ("", ""),
        )

        raw_out = "\n".join(
            [
                # Unattached control client should be ignored
                "CLIENT\x1f$0\x1f@0\x1f%0\x1f9999999999\x1f/dev/pts/1\x1f100\x1fcontrol-mode",
                # Older attached client on session $0
                "CLIENT\x1f$0\x1f@0\x1f%0\x1f1700000100\x1f/dev/pts/2\x1f101\x1fattached,UTF-8",
                # Newer attached & focused client on session $1, active window @2, active pane %3
                "CLIENT\x1f$1\x1f@2\x1f%3\x1f1700000250\x1f/dev/pts/3\x1f102\x1fattached,focused,UTF-8",
                "MALFORMED_LINE_WITHOUT_SEPARATOR",
                "PANE\x1f$0\x1f@0\x1f1\x1f%0\x1f1\x1f201\x1f/dev/pts/10\x1f/tmp/t.sock\x1f/repo/a\x1fold-pane",
                "PANE\x1f$1\x1f@1\x1f0\x1f%1\x1f1\x1f202\x1f/dev/pts/11\x1f/tmp/t.sock\x1f/repo/b\x1finactive-win",
                "PANE\x1f$1\x1f@2\x1f1\x1f%2\x1f0\x1f203\x1f/dev/pts/12\x1f/tmp/t.sock\x1f/repo/b\x1fbg-teammate",
                "PANE\x1f$1\x1f@2\x1f1\x1f%3\x1f1\x1f204\x1f/dev/pts/13\x1f/tmp/t.sock\x1f/repo/c\x1fclaude · ~/c [#aabbccdd]",
            ]
        )
        snap = parse_tmux_probe_output(raw_out)
        self.assertEqual(len(snap.clients), 2)
        self.assertEqual(len(snap.panes), 4)
        self.assertEqual(snap.clients[0].socket_path, "/tmp/t.sock")

        picked = SessionTracker._select_active_tmux_target(snap)
        self.assertIsNotNone(picked)
        assert picked is not None
        client, pane = picked
        self.assertEqual(client.client_pid, 102)
        self.assertEqual(pane.pane_id, "%3")
        self.assertEqual(pane.pane_pid, 204)

        # If outer window title explicitly names repo 'a' while two clients are attached, prefer client on 'a'
        picked_a = SessionTracker._select_active_tmux_target(snap, "user@host: /repo/a")
        assert picked_a is not None
        self.assertEqual(picked_a[1].pane_id, "%0")

    def test_probe_graceful_degradation_missing_bin_no_server_timeout_and_disable_flag(self) -> None:
        # 0. Dedicated client/pane parsers and short symlink socket arg selection
        self.assertEqual(len(parse_tmux_clients_output("CLIENT\x1f$0\x1f@0\x1f%0\x1f10\x1f/dev/pts/1\x1f9\x1fattached")), 1)
        self.assertEqual(
            len(parse_tmux_panes_output("PANE\x1f$0\x1f@0\x1f1\x1f%0\x1f1\x1f50\x1f/dev/pts/2\x1f/tmp/s\x1f/a\x1fbash")),
            1,
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            long_dir = root / ("very_long_private_folder_segment_" * 2)
            long_dir.mkdir(parents=True)
            real_file = long_dir / "sock"
            real_file.write_text("", encoding="utf-8")
            short_link_dir = root / "s"
            os.symlink(long_dir, short_link_dir)
            short_sock = short_link_dir / "sock"
            self.assertEqual(preferred_tmux_socket_arg(short_sock), str(short_sock))
            self.assertLess(len(str(short_sock).encode("utf-8")), len(os.path.realpath(short_sock).encode("utf-8")))

            # 1. Explicit disable env flag skips immediately without spawning subprocess
            with mock.patch("subprocess.run", side_effect=AssertionError("must not spawn")):
                snap = probe_tmux_snapshot(env={"AGENT_VOICE_INPUT_DISABLE_TMUX": "1"})
                self.assertEqual(snap, TmuxServerSnapshot())

            # 2. Non-tmux user (no live default socket, no $TMUX, no extra_sockets) does pure stat -> zero subprocess!
            empty_tmpdir = root / "empty_tmux_tmp"
            empty_tmpdir.mkdir()
            with (
                mock.patch("agent_voice_input._tmux.resolve_tmux_bin", return_value="/usr/bin/tmux"),
                mock.patch("subprocess.run", side_effect=AssertionError("must not spawn when no live socket")),
            ):
                self.assertEqual(
                    probe_tmux_snapshot(env={"TMUX_TMPDIR": str(empty_tmpdir)}),
                    TmuxServerSnapshot(),
                )

        # 3. Missing tmux binary returns empty snapshot cleanly
        with mock.patch("agent_voice_input._tmux.resolve_tmux_bin", return_value=""):
            self.assertEqual(
                probe_tmux_snapshot(env={}, tmux_socket_name="mock_sock"),
                TmuxServerSnapshot(),
            )

        # 4. Server not running (returncode=1) via explicit constructor/arg socket_name returns empty snapshot
        with (
            mock.patch("agent_voice_input._tmux.resolve_tmux_bin", return_value="/usr/bin/tmux"),
            mock.patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess(["tmux"], 1, b"", b"no server running"),
            ) as m_run,
        ):
            snap_no_srv = probe_tmux_snapshot(env={}, tmux_socket_name="mock_sock")
            self.assertEqual(snap_no_srv, TmuxServerSnapshot())
            called_cmd = m_run.call_args[0][0]
            self.assertEqual(called_cmd[:3], ["/usr/bin/tmux", "-L", "mock_sock"])
            self.assertEqual(m_run.call_args[1]["stdin"], subprocess.DEVNULL)

        # 5. TimeoutExpired via explicit tmux_socket returns empty snapshot without raising
        with (
            mock.patch("agent_voice_input._tmux.resolve_tmux_bin", return_value="/usr/bin/tmux"),
            mock.patch(
                "subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="tmux", timeout=0.4),
            ),
        ):
            self.assertEqual(
                probe_tmux_snapshot(env={}, tmux_socket="/tmp/t.sock"),
                TmuxServerSnapshot(),
            )

    def test_multi_pane_proc_scoping_teammate_noise_empty_shell_and_headless_filter(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home, rt, proc = root / "home", root / "rt", root / "proc"
            proj = root / "workspace"
            for d in (home, rt, proc, proj):
                d.mkdir(parents=True)

            # Three panes inside the SAME tmux window and SAME repo directory:
            # %0 (active, older mtime=1000): interactive codex session A + refiner headless codex app-server
            # %1 (background teammate, much newer mtime=9000!): interactive codex session B
            # %2 (empty shell, no agent at all!)
            sid_a = "aaaa1111-1111-4111-8111-111111111111"
            sid_b = "bbbb2222-2222-4222-8222-222222222222"
            sid_refiner = "cccc3333-3333-4333-8333-333333333333"
            f_a = _write_codex_rollout(home, sid_a, str(proj), "主窗格任务A", mtime=1000.0)
            f_b = _write_codex_rollout(home, sid_b, str(proj), "后台队友任务B狂刷日志", mtime=9000.0)
            f_ref = _write_codex_rollout(home, sid_refiner, str(proj), "精修器无头子进程", mtime=9999.0)

            pts0, pts1, pts2 = root / "pts_0", root / "pts_1", root / "pts_2"
            for p, mt in ((pts0, 1000.0), (pts1, 9000.0), (pts2, 9500.0)):
                p.write_text("", encoding="utf-8")
                os.utime(p, (mt, mt))

            # tmux client (PID 400) + pane shells (500, 600, 700)
            _write_fake_proc(proc, 400, comm="tmux", ppid=300, cwd=str(home))
            _write_fake_proc(proc, 500, comm="bash", ppid=1, cwd=str(proj), fds={"0": str(pts0)})
            _write_fake_proc(
                proc,
                501,
                comm="codex",
                ppid=500,
                cwd=str(proj),
                cmdline=["codex"],
                fds={"0": str(pts0), "3": str(f_a)},
            )
            _write_fake_proc(
                proc,
                502,
                comm="codex",
                ppid=500,
                cwd=str(proj),
                cmdline=["codex", "app-server", "--stdio"],
                fds={"0": "/dev/null", "3": str(f_ref)},
            )

            _write_fake_proc(proc, 600, comm="bash", ppid=1, cwd=str(proj), fds={"0": str(pts1)})
            _write_fake_proc(
                proc,
                601,
                comm="codex",
                ppid=600,
                cwd=str(proj),
                cmdline=["codex"],
                fds={"0": str(pts1), "3": str(f_b)},
            )

            _write_fake_proc(proc, 700, comm="bash", ppid=1, cwd=str(proj), fds={"0": str(pts2)})

            active_id = ["%0"]

            def fake_probe() -> TmuxServerSnapshot:
                cur = active_id[0]
                return TmuxServerSnapshot(
                    clients=(
                        TmuxClientInfo(
                            session_id="$0",
                            window_id="@0",
                            pane_id=cur,
                            activity=9500.0,
                            client_tty="/dev/pts/99",
                            client_pid=400,
                            focused=True,
                            socket_path="/tmp/mock.sock",
                        ),
                    ),
                    panes=(
                        TmuxPaneInfo("%0", "$0", "@0", True, cur == "%0", 500, str(pts0), "/tmp/mock.sock", str(proj), "bash"),
                        TmuxPaneInfo("%1", "$0", "@0", True, cur == "%1", 600, str(pts1), "/tmp/mock.sock", str(proj), "bash"),
                        TmuxPaneInfo("%2", "$0", "@0", True, cur == "%2", 700, str(pts2), "/tmp/mock.sock", str(proj), "bash"),
                    ),
                )

            tracker = SessionTracker(
                home_dir=home, runtime_dir=rt, proc_root=proc, tmux_probe_fn=fake_probe
            )

            # 1. Focus on %0 -> strictly locks sid_a despite %1 having mtime=9000 and headless refiner mtime=9999
            s0 = tracker.sync_session(window_title="Ubuntu")
            self.assertIsNotNone(s0)
            assert s0 is not None
            self.assertEqual(s0.match.session_id, sid_a)
            self.assertEqual(s0.match.matched_by, "tmux_proc_scan")
            self.assertEqual(list(s0.clean_turns), [("user", "主窗格任务A")])

            # 2. Switch focus to %1 -> follows focus to sid_b
            active_id[0] = "%1"
            s1 = tracker.sync_session(window_title="Ubuntu")
            self.assertIsNotNone(s1)
            assert s1 is not None
            self.assertEqual(s1.match.session_id, sid_b)
            self.assertEqual(list(s1.clean_turns), [("user", "后台队友任务B狂刷日志")])

            # 3. Switch focus to empty shell %2 -> MUST return None (never cross-attaches %0 or %1!)
            active_id[0] = "%2"
            self.assertIsNone(tracker.resolve_session("Ubuntu"))
            self.assertIsNone(tracker.sync_session(window_title="tmux"))

    def test_hook_registry_and_stale_exit_anti_dirty_read(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home, rt, proc = root / "home", root / "rt", root / "proc"
            proj = root / "svc"
            for d in (home, rt, proc, proj):
                d.mkdir(parents=True)

            pts0, pts1 = root / "pts_0", root / "pts_1"
            pts0.write_text("", encoding="utf-8")
            pts1.write_text("", encoding="utf-8")

            sid_old = "1111aaaa-2222-4333-8444-555566667777"
            sid_neighbor = "9999bbbb-2222-4333-8444-555566667777"
            f_old = _write_claude_jsonl(home, sid_old, str(proj), "旧会话内容", mtime=1000.0)
            f_nb = _write_claude_jsonl(home, sid_neighbor, str(proj), "隔壁窗格会话", mtime=5000.0)

            # Pane %0 shell (PID 500) -> claude (PID 510) -> hook (PID 511)
            _write_fake_proc(proc, 500, comm="bash", ppid=1, cwd=str(proj), fds={"0": str(pts0)})
            _write_fake_proc(
                proc,
                510,
                comm="claude",
                ppid=500,
                cwd=str(proj),
                cmdline=["claude"],
                fds={"0": str(pts0)},
            )
            _write_fake_proc(proc, 511, comm="python3", ppid=510, cwd=str(proj))

            # Pane %1 shell (PID 600) -> neighbor claude (PID 610)
            _write_fake_proc(proc, 600, comm="bash", ppid=1, cwd=str(proj), fds={"0": str(pts1)})
            _write_fake_proc(
                proc,
                610,
                comm="claude",
                ppid=600,
                cwd=str(proj),
                cmdline=["claude"],
                fds={"0": str(pts1)},
            )

            mock_sock = str(root / "nonexistent_tmux.sock")
            ok = VST.update_claude_session(
                {"session_id": sid_old, "transcript_path": str(f_old), "cwd": str(proj)},
                home=home,
                proc_root=proc,
                runtime_dir=rt,
                start_pid=511,
                env={
                    "HOME": str(home),
                    "TMUX": f"{mock_sock},999,0",
                    "TMUX_PANE": "%0",
                },
            )
            self.assertTrue(ok)
            reg_data = json.loads((rt / "sessions" / "claude-1111aaaa.json").read_text(encoding="utf-8"))
            self.assertEqual(reg_data["tmux_pane"], "%0")
            self.assertEqual(reg_data["tmux_socket"], mock_sock)
            self.assertEqual(reg_data["agent_pid"], 510)

            pane_title_box = [f"claude · ~/svc [#{sid_old[:8]}]"]

            def fake_probe() -> TmuxServerSnapshot:
                return TmuxServerSnapshot(
                    clients=(TmuxClientInfo("$0", "@0", "%0", 6000.0, "/dev/pts/9", 400, True, mock_sock),),
                    panes=(
                        TmuxPaneInfo("%0", "$0", "@0", True, True, 500, str(pts0), mock_sock, str(proj), pane_title_box[0]),
                        TmuxPaneInfo("%1", "$0", "@0", True, False, 600, str(pts1), mock_sock, str(proj), "claude"),
                    ),
                )

            tracker = SessionTracker(
                home_dir=home, runtime_dir=rt, proc_root=proc, tmux_probe_fn=fake_probe
            )

            # A. While PID 510 is alive, %0 resolves sid_old (both via pane_title AND via tmux_pane registry if title cleared)
            m_live = tracker.resolve_session("Ubuntu")
            assert m_live is not None
            self.assertEqual((m_live.session_id, m_live.matched_by), (sid_old, "tmux_registry"))

            pane_title_box[0] = "custom-pane-name-without-tag"
            m_reg_only = tracker.resolve_session("Ubuntu")
            assert m_reg_only is not None
            self.assertEqual((m_reg_only.session_id, m_reg_only.matched_by), (sid_old, "tmux_registry"))

            # B. Restore sticky [#1111aaaa] pane_title, then simulate Claude PID 510 exiting back to empty bash!
            pane_title_box[0] = f"claude · ~/svc [#{sid_old[:8]}]"
            shutil.rmtree(proc / "510")
            shutil.rmtree(proc / "511")
            # Must NOT return stale sid_old AND must NOT leak neighbor %1's sid_neighbor!
            self.assertIsNone(tracker.resolve_session("Ubuntu"))

            # C. User starts a NEW hookless Claude session (PID 520, sid_new) in %0 while sticky title still says [#1111aaaa]!
            sid_new = "2222cccc-3333-4444-8555-666677778888"
            _write_claude_jsonl(home, sid_new, str(proj), "重开的新无钩子会话", mtime=7000.0)
            _write_fake_proc(
                proc,
                520,
                comm="claude",
                ppid=500,
                cwd=str(proj),
                cmdline=["claude"],
                fds={"0": str(pts0)},
            )
            m_reopened = tracker.resolve_session("Ubuntu")
            assert m_reopened is not None
            self.assertEqual(m_reopened.session_id, sid_new)
            self.assertEqual(m_reopened.matched_by, "tmux_proc_scan")
            _ = f_nb  # ensure neighbor fixture remained on disk throughout

    def test_windows_terminal_multi_tab_non_tmux_anti_hijack(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home, rt, proc = root / "home", root / "rt", root / "proc"
            proj_tmux = root / "proj-alpha"
            proj_tab2 = root / "tmux-support"  # Intentionally contains 'tmux-' to test word boundary!
            for d in (home, rt, proc, proj_tmux, proj_tab2):
                d.mkdir(parents=True)

            pts_tmux = root / "pts_tmux"
            pts_tab2 = root / "pts_tab2"
            pts_tmux.write_text("", encoding="utf-8")
            pts_tab2.write_text("", encoding="utf-8")
            os.utime(pts_tmux, (5000.0, 5000.0))
            os.utime(pts_tab2, (2000.0, 2000.0))

            # Tab 1: attached tmux (client_activity=5000) running agy in proj-alpha
            agy_cid = "aaaa0000-1111-4222-8333-444455556666"
            agy_lock, _ = _write_agy_session(home, agy_cid, "tmux 内 agy 会话", mtime=5000.0)
            _write_fake_proc(proc, 400, comm="tmux", ppid=1, cwd=str(proj_tmux))
            _write_fake_proc(proc, 500, comm="bash", ppid=1, cwd=str(proj_tmux), fds={"0": str(pts_tmux)})
            _write_fake_proc(
                proc,
                501,
                comm="agy",
                ppid=500,
                cwd=str(proj_tmux),
                cmdline=["agy"],
                fds={"0": str(pts_tmux), "5": str(agy_lock)},
            )

            # Tab 2: non-tmux terminal tab running Claude Code (mtime=2000, older than tmux client_activity=5000!)
            tab2_sid = "bbbb9999-1111-4222-8333-444455556666"
            tab2_f = _write_claude_jsonl(home, tab2_sid, str(proj_tab2), "Tab2 普通标签页会话", mtime=2000.0)
            _write_fake_proc(
                proc,
                800,
                comm="claude",
                ppid=1,
                cwd=str(proj_tab2),
                cmdline=["claude"],
                fds={"0": str(pts_tab2)},
            )

            client_act = [5000.0]

            def fake_probe() -> TmuxServerSnapshot:
                return TmuxServerSnapshot(
                    clients=(TmuxClientInfo("$0", "@0", "%0", client_act[0], "/dev/pts/1", 400, True, "/tmp/m.sock"),),
                    panes=(
                        TmuxPaneInfo(
                            "%0", "$0", "@0", True, True, 500, str(pts_tmux), "/tmp/m.sock", str(proj_tmux), f"agy · ~/proj-alpha [#{agy_cid[:8]}]"
                        ),
                    ),
                )

            tracker = SessionTracker(
                home_dir=home, runtime_dir=rt, proc_root=proc, tmux_probe_fn=fake_probe
            )

            # Rule 1: Explicit outer [#tag] in Tab 2 wins immediately
            write_session_registry_entry(
                rt, cli="claude", session_id=tab2_sid, jsonl_path=str(tab2_f), cwd=str(proj_tab2), agent_pid=800
            )
            m_tag = tracker.resolve_session(f"claude · ~/tmux-support [#{tab2_sid[:8]}]")
            assert m_tag is not None
            self.assertEqual((m_tag.cli, m_tag.session_id, m_tag.matched_by), ("claude", tab2_sid, "registry"))

            # Rule 2a: Tab 2 without hook emitting Claude spinner '✳' or CLI word 'claude' -> picks Tab 2!
            m_spin = tracker.resolve_session("✳ Fix race condition in parser")
            assert m_spin is not None
            self.assertEqual((m_spin.cli, m_spin.session_id), ("claude", tab2_sid))

            m_word = tracker.resolve_session("claude")
            assert m_word is not None
            self.assertEqual((m_word.cli, m_word.session_id), ("claude", tab2_sid))

            # Rule 2b: Tab 2 shell title containing repo path 'tmux-support' MUST NOT false-match 'tmux' word!
            m_dir_tab2 = tracker.resolve_session("xhx@host: ~/worktrees/tmux-support")
            assert m_dir_tab2 is not None
            self.assertEqual((m_dir_tab2.cli, m_dir_tab2.session_id), ("claude", tab2_sid))

            # Rule 2c: Tab 1 outer title explicitly 'tmux' or 'proj-alpha' -> picks Tab 1 tmux active pane!
            m_tmux_word = tracker.resolve_session("xhx@host: ~ (tmux)")
            assert m_tmux_word is not None
            self.assertEqual((m_tmux_word.cli, m_tmux_word.session_id), ("agy", agy_cid))

            m_dir_tab1 = tracker.resolve_session("xhx@host: ~/repos/proj-alpha")
            assert m_dir_tab1 is not None
            self.assertEqual((m_dir_tab1.cli, m_dir_tab1.session_id), ("agy", agy_cid))

            # Rule 2d: Generic title 'Ubuntu' -> compares client_activity vs non-tmux activity!
            # When client_activity=5000 >= tab2=2000 -> picks Tab 1 tmux
            m_gen_tmux = tracker.resolve_session("Ubuntu")
            assert m_gen_tmux is not None
            self.assertEqual((m_gen_tmux.cli, m_gen_tmux.session_id), ("agy", agy_cid))

            # When user chats in Tab 2 at t=6000 while tmux client stays untouched at 5000 -> picks Tab 2!
            os.utime(pts_tab2, (6000.0, 6000.0))
            m_gen_tab2 = tracker.resolve_session("Ubuntu")
            assert m_gen_tab2 is not None
            self.assertEqual((m_gen_tab2.cli, m_gen_tab2.session_id), ("claude", tab2_sid))

            # Rule 2e: Detached tmux server (0 attached clients, even with newer pane mtime=9999)
            # MUST have its pane PIDs excluded so Tab 2 wins unconditionally!
            os.utime(pts_tmux, (9999.0, 9999.0))
            os.utime(pts_tab2, (2000.0, 2000.0))

            def detached_probe() -> TmuxServerSnapshot:
                return TmuxServerSnapshot(
                    clients=(),
                    panes=(
                        TmuxPaneInfo(
                            "%0",
                            "$0",
                            "@0",
                            True,
                            True,
                            500,
                            str(pts_tmux),
                            "/tmp/m.sock",
                            str(proj_tmux),
                            f"agy · ~/proj-alpha [#{agy_cid[:8]}]",
                        ),
                    ),
                )

            tracker_detached = SessionTracker(
                home_dir=home, runtime_dir=rt, proc_root=proc, tmux_probe_fn=detached_probe
            )
            m_detached = tracker_detached.resolve_session("Ubuntu")
            assert m_detached is not None
            self.assertEqual((m_detached.cli, m_detached.session_id), ("claude", tab2_sid))

            # Also verify update_agy_session inside tmux records tmux_pane, tmux_socket, and agent_pid
            _write_fake_proc(proc, 502, comm="python3", ppid=501, cwd=str(proj_tmux))
            ok_agy = VST.update_agy_session(
                {"conversationId": agy_cid, "workspacePaths": [str(proj_tmux)]},
                home=home,
                proc_root=proc,
                runtime_dir=rt,
                start_pid=502,
                env={
                    "HOME": str(home),
                    "TMUX": f"{root / 'nonexistent_agy.sock'},888,0",
                    "TMUX_PANE": "%0",
                },
            )
            self.assertTrue(ok_agy)
            agy_reg = json.loads(
                (rt / "sessions" / f"agy-{agy_cid[:8]}.json").read_text(encoding="utf-8")
            )
            self.assertEqual(agy_reg["tmux_pane"], "%0")
            self.assertEqual(agy_reg["agent_pid"], 501)

    def test_outer_hint_set_titles_passthrough_and_stale_exit_no_resurrection(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home, rt, proc = root / "home", root / "rt", root / "proc"
            proj = root / "svc"
            for d in (home, rt, proc, proj):
                d.mkdir(parents=True)

            pts0, pts1 = root / "pts_0", root / "pts_1"
            pts0.write_text("", encoding="utf-8")
            pts1.write_text("", encoding="utf-8")

            sid_old = "1111aaaa-2222-4333-8444-555566667777"
            sid_nb = "9999bbbb-2222-4333-8444-555566667777"
            dead_cx = "0196dead-1111-7000-8000-deadbeef0001"
            f_old = _write_claude_jsonl(home, sid_old, str(proj), "已退出的旧Claude会话", mtime=1000.0)
            _write_claude_jsonl(home, sid_nb, str(proj), "隔壁窗格Claude会话", mtime=5000.0)
            _write_codex_rollout(home, dead_cx, str(proj), "已退出的旧Codex会话", mtime=3000.0)

            _write_fake_proc(proc, 400, comm="tmux", ppid=1, cwd=str(proj))
            _write_fake_proc(proc, 500, comm="bash", ppid=1, cwd=str(proj), fds={"0": str(pts0)})
            _write_fake_proc(proc, 600, comm="bash", ppid=1, cwd=str(proj), fds={"0": str(pts1)})
            _write_fake_proc(
                proc, 610, comm="claude", ppid=600, cwd=str(proj), cmdline=["claude"], fds={"0": str(pts1)}
            )

            # Register sid_old with dead agent_pid=510 (not present in proc_root -> exited!)
            write_session_registry_entry(
                rt,
                cli="claude",
                session_id=sid_old,
                jsonl_path=str(f_old),
                cwd=str(proj),
                tmux_pane="%0",
                tmux_socket="/tmp/mock.sock",
                tty=str(pts0),
                agent_pid=510,
            )

            pane_title_box = [f"claude · ~/svc [#{sid_old[:8]}]"]

            def fake_probe() -> TmuxServerSnapshot:
                return TmuxServerSnapshot(
                    clients=(TmuxClientInfo("$0", "@0", "%0", 6000.0, "/dev/pts/9", 400, True, "/tmp/mock.sock"),),
                    panes=(
                        TmuxPaneInfo("%0", "$0", "@0", True, True, 500, str(pts0), "/tmp/mock.sock", str(proj), pane_title_box[0]),
                        TmuxPaneInfo("%1", "$0", "@0", True, False, 600, str(pts1), "/tmp/mock.sock", str(proj), "claude"),
                    ),
                )

            tracker = SessionTracker(
                home_dir=home, runtime_dir=rt, proc_root=proc, tmux_probe_fn=fake_probe
            )

            # 1. tmux `set-titles on` forwards sticky [#1111aaaa] (both quoted default and unquoted '#T' / 'tmux ...')
            # to outer window_title after PID 510 exited -> MUST return None (never resurrect f_old from disk!)
            for outer_t in (
                f'0:0.0 "{pane_title_box[0]}" ',
                pane_title_box[0],
                f"tmux · [#{sid_old[:8]}]",
            ):
                self.assertIsNone(tracker.resolve_session(outer_t), outer_t)

            # Same when pane_title + outer window_title hold a stale exited Codex full UUID!
            pane_title_box[0] = f"⠋ svc · Old task · {dead_cx}"
            self.assertIsNone(tracker.resolve_session(pane_title_box[0]))

            # 2. User launches a NEW hookless Codex session in %0 while set-titles still forwards old sticky [#1111aaaa]
            pane_title_box[0] = f"claude · ~/svc [#{sid_old[:8]}]"
            live_cx = "0196cafe-2222-7000-8000-cafebabe0002"
            f_live_cx = _write_codex_rollout(home, live_cx, str(proj), "同窗格重开的新Codex会话", mtime=7000.0)
            _write_fake_proc(
                proc,
                530,
                comm="codex",
                ppid=500,
                cwd=str(proj),
                cmdline=["codex"],
                fds={"0": str(pts0), "3": str(f_live_cx)},
            )
            for outer_t in (f'0:0.0 "{pane_title_box[0]}" ', pane_title_box[0]):
                m_new = tracker.resolve_session(outer_t)
                assert m_new is not None
                self.assertEqual((m_new.cli, m_new.session_id, m_new.matched_by), ("codex", live_cx, "tmux_proc_scan"))

            # 3. Non-tmux normal terminal with sticky [#1111aaaa] whose registered agent_pid=510 has exited:
            shutil.rmtree(proc / "530")
            shutil.rmtree(proc / "610")
            tracker_no_tmux = SessionTracker(
                home_dir=home,
                runtime_dir=rt,
                proc_root=proc,
                tmux_probe_fn=lambda: TmuxServerSnapshot(),
            )
            # Empty shell -> MUST return None (strictly blocked from disk resurrection & nonproc fallback!)
            self.assertIsNone(tracker_no_tmux.resolve_session(f"claude · ~/svc [#{sid_old[:8]}]"))

            # Reopen hookless Codex in that normal terminal while sticky [#1111aaaa] remains on outer window
            _write_fake_proc(
                proc,
                820,
                comm="codex",
                ppid=1,
                cwd=str(proj),
                cmdline=["codex"],
                fds={"0": str(pts0), "3": str(f_live_cx)},
            )
            m_plain = tracker_no_tmux.resolve_session(f"claude · ~/svc [#{sid_old[:8]}]")
            assert m_plain is not None
            self.assertEqual((m_plain.cli, m_plain.session_id), ("codex", live_cx))

    def test_should_prefer_non_tmux_unquoted_set_titles_short_pane_and_midpath_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home_base = root / "users" / "xhx"
            rt, proc = root / "rt", root / "proc"
            proj_alpha = home_base / "repos" / "proj-alpha"
            proj_beta = home_base / "repos" / "proj-beta"
            for d in (home_base, rt, proc, proj_alpha, proj_beta):
                d.mkdir(parents=True)

            pts_tmux, pts_tab2 = root / "pts_tmux", root / "pts_tab2"
            pts_tmux.write_text("", encoding="utf-8")
            pts_tab2.write_text("", encoding="utf-8")
            os.utime(pts_tmux, (2000.0, 2000.0))
            os.utime(pts_tab2, (6000.0, 6000.0))

            cl_tmux = "aaaa1111-1111-4111-8111-000000000001"
            cl_tab2 = "bbbb2222-2222-4222-8222-000000000002"
            cx_tab2 = "cccc3333-3333-4333-8333-000000000003"
            _write_claude_jsonl(home_base, cl_tmux, str(proj_alpha), "tmux前台会话", mtime=2000.0)
            _write_claude_jsonl(home_base, cl_tab2, str(proj_beta), "Tab2后台空闲会话", mtime=5000.0)
            f_cx_tab2 = _write_codex_rollout(home_base, cx_tab2, str(proj_beta), "Tab2 Codex会话", mtime=6000.0)
            (home_base / ".codex" / "session_index.jsonl").write_text(
                json.dumps({"id": cx_tab2, "thread_name": "hi-perf-topic"}) + "\n",
                encoding="utf-8",
            )

            # tmux client launched from /users/xhx (parent of /users/xhx/repos/proj-beta!)
            _write_fake_proc(proc, 400, comm="tmux", ppid=1, cwd=str(home_base))
            _write_fake_proc(proc, 500, comm="bash", ppid=1, cwd=str(proj_alpha), fds={"0": str(pts_tmux)})
            _write_fake_proc(
                proc, 501, comm="claude", ppid=500, cwd=str(proj_alpha), cmdline=["claude"], fds={"0": str(pts_tmux)}
            )
            _write_fake_proc(
                proc, 801, comm="claude", ppid=1, cwd=str(proj_beta), cmdline=["claude"], fds={"0": str(pts_tab2)}
            )
            _write_fake_proc(
                proc,
                802,
                comm="codex",
                ppid=1,
                cwd=str(proj_beta),
                cmdline=["codex"],
                fds={"0": str(pts_tab2), "3": str(f_cx_tab2)},
            )

            pane_state = {"title": "claude · ~/repos/proj-alpha", "cwd": str(proj_alpha), "activity": 2000.0}

            def fake_probe() -> TmuxServerSnapshot:
                return TmuxServerSnapshot(
                    clients=(
                        TmuxClientInfo(
                            "$0", "@0", "%0", float(pane_state["activity"]), "/dev/pts/1", 400, True, "/tmp/m.sock"
                        ),
                    ),
                    panes=(
                        TmuxPaneInfo(
                            "%0",
                            "$0",
                            "@0",
                            True,
                            True,
                            500,
                            str(pts_tmux),
                            "/tmp/m.sock",
                            str(pane_state["cwd"]),
                            str(pane_state["title"]),
                        ),
                    ),
                )

            tracker = SessionTracker(
                home_dir=home_base, runtime_dir=rt, proc_root=proc, tmux_probe_fn=fake_probe
            )

            # Bug A: Unquoted set-titles-string '#T' / '#S: #T' containing 'claude' or spinner '✳ Fixing bug'
            # MUST stay on tmux %0 even though Tab 2 has newer pts_mtime=6000 and cli=='claude'!
            for unquoted_outer in ("claude · ~/repos/proj-alpha", "dev: claude · ~/repos/proj-alpha"):
                m_unq = tracker.resolve_session(unquoted_outer)
                assert m_unq is not None
                self.assertEqual((m_unq.cli, m_unq.session_id), ("claude", cl_tmux))

            pane_state["title"] = "✳ Fixing bug"
            m_spin_tmux = tracker.resolve_session("✳ Fixing bug")
            assert m_spin_tmux is not None
            self.assertEqual((m_spin_tmux.cli, m_spin_tmux.session_id), ("claude", cl_tmux))

            # Now set tmux client_activity=9000 > Tab 2 timestamps (1500/1600) so Bug B & Bug C
            # CANNOT pass by timestamp tie-breaker and must strictly pass via spinner/topic/cwd rules!
            pane_state["activity"] = 9000.0
            os.utime(pts_tab2, (1500.0, 1500.0))
            os.utime(f_cx_tab2, (1500.0, 1500.0))

            # Bug B: Short <4 char pane_title ('✳' or 'hi') MUST NOT block Tab 2 spinner or thread_name!
            pane_state["title"] = "✳"
            m_short_spin = tracker.resolve_session("✳ Investigating Tab2 issue")
            assert m_short_spin is not None
            self.assertEqual(m_short_spin.session_id, cl_tab2)

            pane_state["title"] = "hi"
            m_short_topic = tracker.resolve_session("Terminal - hi-perf-topic")
            assert m_short_topic is not None
            self.assertEqual(m_short_topic.session_id, cx_tab2)

            # Bug C: Intermediate parent directory '/xhx/' in 'xhx@host: /users/xhx/repos/proj-beta'
            # MUST NOT false-match tmux cwd '/users/xhx', AND client_pid cwd is ignored when pane current_path is set!
            pane_state["title"] = "bash"
            pane_state["cwd"] = str(home_base)
            m_midpath = tracker.resolve_session(f"xhx@host: {proj_beta}")
            assert m_midpath is not None
            self.assertIn(m_midpath.session_id, (cl_tab2, cx_tab2))

            # Even if client_pid 400 cwd points to proj_beta, non-empty active_pane.current_path (proj_alpha)
            # takes sole precedence so outer title naming proj-alpha resolves tmux!
            (proc / "400" / "cwd").unlink()
            os.symlink(str(proj_beta), proc / "400" / "cwd")
            pane_state["cwd"] = str(proj_alpha)
            m_pane_cwd_wins = tracker.resolve_session(f"xhx@host: {proj_alpha}")
            assert m_pane_cwd_wins is not None
            self.assertEqual(m_pane_cwd_wins.session_id, cl_tmux)

    def test_multi_server_socket_isolation_proc_none_guard_and_macos_nonproc_alignment(self) -> None:
        # 1. Multi-server ($0, @0, %0) socket isolation: Server A and Server B both number from $0/@0/%0!
        multi_snap = TmuxServerSnapshot(
            clients=(
                TmuxClientInfo("$0", "@0", "%0", 100.0, "/dev/pts/1", 101, False, "/tmp/tmux-1000/srv_a"),
                # Newer focused client on srv_b with empty pane_id to also exercise session/window fallback
                TmuxClientInfo("$0", "@0", "", 900.0, "/dev/pts/2", 202, True, "/tmp/tmux-1000/srv_b"),
            ),
            panes=(
                # srv_a's %0 appears FIRST in panes tuple — must NOT be matched to srv_b's client!
                TmuxPaneInfo("%0", "$0", "@0", True, True, 501, "/dev/pts/10", "/tmp/tmux-1000/srv_a", "/repo/a", "bash"),
                TmuxPaneInfo("%0", "$0", "@0", True, True, 601, "/dev/pts/20", "/tmp/tmux-1000/srv_b", "/repo/b", "bash"),
            ),
        )
        picked = SessionTracker._select_active_tmux_target(multi_snap)
        assert picked is not None
        self.assertEqual(picked[0].socket_path, "/tmp/tmux-1000/srv_b")
        self.assertEqual(picked[1].socket_path, "/tmp/tmux-1000/srv_b")
        self.assertEqual(picked[1].pane_pid, 601)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home, rt, proc = root / "home", root / "rt", root / "proc"
            proj_tmux = root / "proj-tmux"
            proj_mac = root / "proj-mac"
            for d in (home, rt, proc, proj_tmux, proj_mac):
                d.mkdir(parents=True)

            # 2. proc_root=None without explicit tmux override MUST NEVER touch host tmux!
            sid_tmux = "aaaa1111-0000-4000-8000-000000000011"
            sid_mac = "bbbb2222-0000-4000-8000-000000000022"
            f_tmux = _write_claude_jsonl(home, sid_tmux, str(proj_tmux), "macOS后台tmux会话", mtime=1000.0)
            f_mac = _write_claude_jsonl(home, sid_mac, str(proj_mac), "macOS前台普通终端会话", mtime=5000.0)

            reg_tmux = write_session_registry_entry(
                rt,
                cli="claude",
                session_id=sid_tmux,
                jsonl_path=str(f_tmux),
                cwd=str(proj_tmux),
                tmux_pane="%0",
                tmux_socket="/tmp/mac_tmux.sock",
            )
            # Corrupt updated_at to non-numeric string to verify _scan_nonproc_fallback_sessions tolerance (P2.3)
            raw_rec = json.loads(reg_tmux.read_text(encoding="utf-8"))
            raw_rec["updated_at"] = "invalid-timestamp"
            reg_tmux.write_text(json.dumps(raw_rec), encoding="utf-8")
            os.chmod(reg_tmux, 0o600)

            with mock.patch(
                "agent_voice_input.session_ctx.probe_tmux_snapshot",
                side_effect=AssertionError("proc_root=None must not probe host tmux"),
            ):
                tracker_isolated = SessionTracker(home_dir=home, runtime_dir=rt, proc_root=None, env={})
                m_iso = tracker_isolated.resolve_session("zsh")
                assert m_iso is not None
                self.assertEqual(m_iso.session_id, sid_mac)

            # 3. macOS (proc_root=None) with explicit tmux snapshot: background tmux MUST NOT unconditionally
            # hijack foreground normal terminal when non-tmux session is newer or matches outer title!
            act_box = [1000.0]
            tracker_mac_tmux = SessionTracker(
                home_dir=home,
                runtime_dir=rt,
                proc_root=None,
                tmux_probe_fn=lambda: TmuxServerSnapshot(
                    clients=(TmuxClientInfo("$0", "@0", "%0", act_box[0], "/dev/ttys001", 0, True, "/tmp/mac_tmux.sock"),),
                    panes=(
                        TmuxPaneInfo(
                            "%0",
                            "$0",
                            "@0",
                            True,
                            True,
                            0,
                            "/dev/ttys002",
                            "/tmp/mac_tmux.sock",
                            str(proj_tmux),
                            f"claude · ~/proj-tmux [#{sid_tmux[:8]}]",
                        ),
                    ),
                ),
            )
            m_mac_fg = tracker_mac_tmux.resolve_session("user@mac: ~/proj-mac")
            assert m_mac_fg is not None
            self.assertEqual(m_mac_fg.session_id, sid_mac)

            m_mac_ts = tracker_mac_tmux.resolve_session("zsh")
            assert m_mac_ts is not None
            self.assertEqual(m_mac_ts.session_id, sid_mac)

            # When tmux client_activity is newer (9000 > 5000) or outer title says 'tmux', tmux wins on macOS
            act_box[0] = 9000.0
            m_mac_tmux_win = tracker_mac_tmux.resolve_session("zsh")
            assert m_mac_tmux_win is not None
            self.assertEqual(m_mac_tmux_win.session_id, sid_tmux)

            # 4. Dead socket on real /proc returns ('', '') + cross-PTY ancestor ignored in session_title
            self.assertEqual(
                VST._resolve_and_sync_tmux_pane(
                    {"TMUX": f"{root / 'dead.sock'},123,0", "TMUX_PANE": "%0"},
                    tty_path="/dev/null",
                    title="claude [#11223344]",
                    proc_root=Path("/proc"),
                    explicit_env=True,
                ),
                ("", ""),
            )
            pts_outer, pts_child = root / "pts_outer", root / "pts_child"
            pts_outer.write_text("", encoding="utf-8")
            pts_child.write_text("", encoding="utf-8")
            _write_fake_proc(
                proc, 900, comm="claude", ppid=1, psid=900, cwd=str(proj_tmux), cmdline=["claude"], fds={"0": str(pts_outer)}
            )
            # Child 901 ran setsid (psid=901) and attached to a fresh PTY pts_child != pts_outer
            _write_fake_proc(
                proc, 901, comm="python3", ppid=900, psid=901, cwd=str(proj_mac), fds={"0": str(pts_child)}
            )
            sid_xpty = "dddd4444-0000-4000-8000-000000000044"
            self.assertTrue(
                VST.update_claude_session(
                    {"session_id": sid_xpty, "cwd": str(proj_mac)},
                    home=home,
                    proc_root=proc,
                    runtime_dir=rt,
                    start_pid=901,
                    env={"HOME": str(home)},
                )
            )
            xpty_reg = json.loads((rt / "sessions" / f"claude-{sid_xpty[:8]}.json").read_text(encoding="utf-8"))
            self.assertEqual(xpty_reg.get("agent_pid", 0), 0)
            self.assertEqual(xpty_reg["tty"], str(pts_child))

    def test_non_tmux_title_mentioning_word_tmux_and_stale_hint_eff_title_neutralization(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home, rt, proc = root / "home", root / "rt", root / "proc"
            proj_tab1 = root / "proj-tab1"
            proj_tab2 = root / "proj-tab2"
            for d in (home, rt, proc, proj_tab1, proj_tab2):
                d.mkdir(parents=True)

            pts1, pts2 = root / "pts_1", root / "pts_2"
            pts1.write_text("", encoding="utf-8")
            pts2.write_text("", encoding="utf-8")
            os.utime(pts1, (7000.0, 7000.0))
            os.utime(pts2, (2000.0, 2000.0))

            # Tab 1: attached tmux running interactive Codex (cx_tmux) in proj_tab1
            cx_tmux = "0196aaaa-0000-7000-8000-000000000101"
            f_cx_tmux = _write_codex_rollout(home, cx_tmux, str(proj_tab1), "Tab1 tmux 内 Codex", mtime=7000.0)
            _write_fake_proc(proc, 400, comm="tmux", ppid=1, cwd=str(proj_tab1))
            _write_fake_proc(proc, 500, comm="bash", ppid=1, cwd=str(proj_tab1), fds={"0": str(pts1)})
            _write_fake_proc(
                proc,
                501,
                comm="codex",
                ppid=500,
                cwd=str(proj_tab1),
                cmdline=["codex"],
                fds={"0": str(pts1), "3": str(f_cx_tmux)},
            )

            # Tab 2: non-tmux terminal running Claude (cl_tab2) + Codex (cx_tab2, topic='Fix tmux bug')
            cl_tab2 = "bbbb2222-0000-4000-8000-000000000202"
            cx_tab2 = "0196bbbb-0000-7000-8000-000000000203"
            f_cl_tab2 = _write_claude_jsonl(home, cl_tab2, str(proj_tab2), "Tab2 讨论 tmux 任务", mtime=2500.0)
            f_cx_tab2 = _write_codex_rollout(home, cx_tab2, str(proj_tab2), "Tab2 Codex 讨论 tmux", mtime=2000.0)
            (home / ".codex" / "session_index.jsonl").write_text(
                json.dumps({"id": cx_tab2, "thread_name": "Fix tmux bug"}) + "\n",
                encoding="utf-8",
            )
            _write_fake_proc(
                proc, 801, comm="claude", ppid=1, cwd=str(proj_tab2), cmdline=["claude"], fds={"0": str(pts2)}
            )
            _write_fake_proc(
                proc,
                802,
                comm="codex",
                ppid=1,
                cwd=str(proj_tab2),
                cmdline=["codex"],
                fds={"0": str(pts2), "3": str(f_cx_tab2)},
            )
            write_session_registry_entry(
                rt, cli="claude", session_id=cl_tab2, jsonl_path=str(f_cl_tab2), cwd=str(proj_tab2), agent_pid=801
            )

            pane_title_box = ["bash"]

            def fake_probe() -> TmuxServerSnapshot:
                return TmuxServerSnapshot(
                    clients=(TmuxClientInfo("$0", "@0", "%0", 7000.0, "/dev/pts/1", 400, True, "/tmp/t.sock"),),
                    panes=(
                        TmuxPaneInfo(
                            "%0", "$0", "@0", True, True, 500, str(pts1), "/tmp/t.sock", str(proj_tab1), pane_title_box[0]
                        ),
                    ),
                )

            tracker = SessionTracker(
                home_dir=home, runtime_dir=rt, proc_root=proc, tmux_probe_fn=fake_probe
            )

            # 1a. Non-tmux Tab 2 title mentioning word 'tmux' WITH explicit UUID or [#tag] -> MUST resolve Tab 2!
            # Even if tmux active_pane.pane_title happens to be a bare repo name substring ('agent-voice-input').
            pane_title_box[0] = "agent-voice-input"
            m_uuid = tracker.resolve_session(f"⠋ agent-voice-input · Fix tmux pane focus · {cx_tab2}")
            assert m_uuid is not None
            self.assertEqual((m_uuid.cli, m_uuid.session_id, m_uuid.matched_by), ("codex", cx_tab2, "title_uuid"))
            pane_title_box[0] = "bash"

            m_tag = tracker.resolve_session(f"claude · Fix tmux bug [#{cl_tab2[:8]}]")
            assert m_tag is not None
            self.assertEqual((m_tag.cli, m_tag.session_id, m_tag.matched_by), ("claude", cl_tab2, "registry"))

            # Meanwhile, tmux window title like 'tmux · claude' / '[tmux] 0:claude*' stays on Tab 1 tmux!
            m_tmux_win = tracker.resolve_session("tmux · claude")
            assert m_tmux_win is not None
            self.assertEqual((m_tmux_win.cli, m_tmux_win.session_id), ("codex", cx_tmux))

            # 1b. Non-tmux Tab 2 title mentioning word 'tmux' with spinner ('✳ Fix tmux focus bug') or thread_name
            m_spin = tracker.resolve_session("✳ Fix tmux focus bug")
            assert m_spin is not None
            self.assertEqual((m_spin.cli, m_spin.session_id), ("claude", cl_tab2))

            m_topic = tracker.resolve_session("Terminal - Fix tmux bug")
            assert m_topic is not None
            self.assertEqual((m_topic.cli, m_topic.session_id), ("codex", cx_tab2))

            # 1c. Meanwhile, tmux `set-titles-string "tmux: #T"` forwarding 'tmux: ✳ Fixing bug' stays on tmux!
            pane_title_box[0] = "✳ Fixing bug"
            m_fwd = tracker.resolve_session("tmux: ✳ Fixing bug")
            assert m_fwd is not None
            self.assertEqual((m_fwd.cli, m_fwd.session_id), ("codex", cx_tmux))
            pane_title_box[0] = "bash"

            # 1d. Non-/proc (macOS) alignment when foreground title is '✳ Fix tmux focus bug'
            rt_mac = root / "rt_mac"
            rt_mac.mkdir()
            write_session_registry_entry(
                rt_mac,
                cli="codex",
                session_id=cx_tmux,
                jsonl_path=str(f_cx_tmux),
                cwd=str(proj_tab1),
                tmux_pane="%0",
                tmux_socket="/tmp/t.sock",
            )
            write_session_registry_entry(
                rt_mac, cli="claude", session_id=cl_tab2, jsonl_path=str(f_cl_tab2), cwd=str(proj_tab2)
            )
            pane_title_box[0] = f"codex [#{cx_tmux[:8]}]"
            tracker_mac = SessionTracker(
                home_dir=home, runtime_dir=rt_mac, proc_root=None, tmux_probe_fn=fake_probe
            )
            m_mac_spin = tracker_mac.resolve_session("✳ Fix tmux focus bug")
            assert m_mac_spin is not None
            self.assertEqual((m_mac_spin.cli, m_mac_spin.session_id), ("claude", cl_tab2))
            pane_title_box[0] = "bash"

            # 2. Stale hint eff_title='' neutralization:
            # Tab 1 previously ran Claude [#dead1111] (PID 599 exited), leaving sticky outer title
            # 'claude · ~/proj-tab2 [#dead1111]' (or dead spinner UUID), then user started tmux in Tab 1
            # (set-titles off, client_activity=7000 > Tab 2 idle claude pts_mtime=2000).
            # Stale words ('claude', 'proj-tab2', '⠋') MUST be neutralized so Tab 2 idle claude cannot hijack Tab 1!
            dead_cl = "dead1111-0000-4000-8000-000000000999"
            f_dead_cl = _write_claude_jsonl(home, dead_cl, str(proj_tab2), "早已退出的旧会话", mtime=900.0)
            write_session_registry_entry(
                rt, cli="claude", session_id=dead_cl, jsonl_path=str(f_dead_cl), cwd=str(proj_tab2), agent_pid=599
            )
            m_neutral = tracker.resolve_session(f"claude · ~/proj-tab2 [#{dead_cl[:8]}]")
            assert m_neutral is not None
            self.assertEqual((m_neutral.cli, m_neutral.session_id, m_neutral.matched_by), ("codex", cx_tmux, "tmux_proc_scan"))

            dead_cx = "0196dead-0000-7000-8000-000000000998"
            f_dead_cx = _write_codex_rollout(home, dead_cx, str(proj_tab2), "早已退出的旧Codex", mtime=900.0)
            write_session_registry_entry(
                rt, cli="codex", session_id=dead_cx, jsonl_path=str(f_dead_cx), cwd=str(proj_tab2), agent_pid=598
            )
            m_neutral_spin = tracker.resolve_session(f"⠋ proj-tab2 · Old task · {dead_cx}")
            assert m_neutral_spin is not None
            self.assertEqual((m_neutral_spin.cli, m_neutral_spin.session_id, m_neutral_spin.matched_by), ("codex", cx_tmux, "tmux_proc_scan"))

    def test_unclaimed_zero_msg_claude_and_same_server_bg_pane_sticky_outer_tag(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home, rt, proc = root / "home", root / "rt", root / "proc"
            proj = root / "svc"
            for d in (home, rt, proc, proj):
                d.mkdir(parents=True)

            pts0, pts1, pts_tab2 = root / "pts_0", root / "pts_1", root / "pts_tab2"
            for p, mt in ((pts0, 3000.0), (pts1, 4000.0), (pts_tab2, 1000.0)):
                p.write_text("", encoding="utf-8")
                os.utime(p, (mt, mt))

            # Pane %0: live hooked Claude (PID 510) claiming sid_0 (the ONLY .jsonl in proj!)
            sid_0 = "aaaa1111-2222-4333-8444-555566660000"
            f_0 = _write_claude_jsonl(home, sid_0, str(proj), "Pane0存活Claude独占会话", mtime=3000.0)
            _write_fake_proc(proc, 400, comm="tmux", ppid=1, cwd=str(proj))
            _write_fake_proc(proc, 500, comm="bash", ppid=1, cwd=str(proj), fds={"0": str(pts0)})
            _write_fake_proc(
                proc, 510, comm="claude", ppid=500, cwd=str(proj), cmdline=["claude"], fds={"0": str(pts0)}
            )
            write_session_registry_entry(
                rt,
                cli="claude",
                session_id=sid_0,
                jsonl_path=str(f_0),
                cwd=str(proj),
                tmux_pane="%0",
                tmux_socket="/tmp/same.sock",
                tty=str(pts0),
                agent_pid=510,
            )

            # Pane %1 (active!): newly opened 0-message hookless Claude (PID 610, no .jsonl yet!)
            _write_fake_proc(proc, 600, comm="bash", ppid=1, cwd=str(proj), fds={"0": str(pts1)})
            _write_fake_proc(
                proc, 610, comm="claude", ppid=600, cwd=str(proj), cmdline=["claude"], fds={"0": str(pts1)}
            )

            # Background non-tmux Tab 2: idle hookless Claude in another project (pts_mtime=1000 < client_activity=5000)
            proj_other = root / "other"
            proj_other.mkdir()
            sid_tab2 = "bbbb2222-2222-4333-8444-555566669999"
            _write_claude_jsonl(home, sid_tab2, str(proj_other), "Tab2后台会话", mtime=1000.0)
            _write_fake_proc(
                proc, 810, comm="claude", ppid=1, cwd=str(proj_other), cmdline=["claude"], fds={"0": str(pts_tab2)}
            )

            active_pane_box = ["%1"]
            pane0_title_box = ["custom-renamed-pane"]  # Even if %0's pane_title no longer carries [#aaaa1111]!

            def fake_probe() -> TmuxServerSnapshot:
                cur = active_pane_box[0]
                return TmuxServerSnapshot(
                    clients=(TmuxClientInfo("$0", "@0", cur, 5000.0, "/dev/pts/9", 400, True, "/tmp/same.sock"),),
                    panes=(
                        TmuxPaneInfo("%0", "$0", "@0", True, cur == "%0", 500, str(pts0), "/tmp/same.sock", str(proj), pane0_title_box[0]),
                        TmuxPaneInfo("%1", "$0", "@0", True, cur == "%1", 600, str(pts1), "/tmp/same.sock", str(proj), "bash"),
                    ),
                )

            tracker = SessionTracker(
                home_dir=home, runtime_dir=rt, proc_root=proc, tmux_probe_fn=fake_probe
            )

            # Defect 1 check: 0-message hookless Claude in %1 MUST NOT steal sid_0 claimed by live PID 510 in %0!
            self.assertIsNone(tracker.resolve_session("Ubuntu"))

            # Defect 2 check (part A): Outer window title retains sticky [#aaaa1111] from background pane %0
            # on the SAME tmux server while focus is on %1 -> MUST NOT return %0's sid_0 AND MUST neutralize
            # outer title words ('claude') so idle Tab 2 cannot hijack either!
            sticky_bg_outer = f"claude · ~/svc [#{sid_0[:8]}]"
            self.assertIsNone(tracker.resolve_session(sticky_bg_outer))

            # Defect 2 check (part B): Launch interactive Codex cx_1 in focused pane %1 while outer window title
            # still carries background pane %0's sticky [#aaaa1111] -> resolves focused %1's cx_1!
            cx_1 = "0196cafe-1111-7000-8000-000000000111"
            f_cx_1 = _write_codex_rollout(home, cx_1, str(proj), "焦点Pane1新开Codex", mtime=4500.0)
            _write_fake_proc(
                proc,
                620,
                comm="codex",
                ppid=600,
                cwd=str(proj),
                cmdline=["codex"],
                fds={"0": str(pts1), "3": str(f_cx_1)},
            )
            m_fg = tracker.resolve_session(sticky_bg_outer)
            assert m_fg is not None
            self.assertEqual((m_fg.cli, m_fg.session_id, m_fg.matched_by), ("codex", cx_1, "tmux_proc_scan"))

            # Switching focus back to %0 resolves %0's sid_0 cleanly
            active_pane_box[0] = "%0"
            m_p0 = tracker.resolve_session(sticky_bg_outer)
            assert m_p0 is not None
            self.assertEqual((m_p0.cli, m_p0.session_id, m_p0.matched_by), ("claude", sid_0, "tmux_registry"))


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class TmuxRealIsolatedIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.home = self.base / "home"
        self.xdg = self.base / "xdg"
        self.rt = self.xdg / "agent-voice-input"
        self.home.mkdir(parents=True)
        self.xdg.mkdir(parents=True)

        # Short /tmp directory prevents Darwin's ~68-char /private/var/folders/... TMPDIR
        # from overflowing the 104-byte Unix domain socket SUN_PATH limit.
        self._tmux_tmp = tempfile.TemporaryDirectory(dir="/tmp", prefix="avi_")
        self.addCleanup(self._tmux_tmp.cleanup)
        self.tmux_tmpdir = Path(self._tmux_tmp.name)
        os.chmod(self.tmux_tmpdir, 0o700)
        self.sock_name = f"t{os.getpid()}_{uuid.uuid4().hex[:6]}"

        self.base_env = {
            **os.environ,
            "HOME": str(self.home),
            "XDG_RUNTIME_DIR": str(self.xdg),
            "TMUX_TMPDIR": str(self.tmux_tmpdir),
            "TERM": "xterm-256color",
            "PYTHONPATH": str(SRC_ROOT),
        }
        self.base_env.pop("TMUX", None)
        self.base_env.pop("TMUX_PANE", None)
        self.base_env.pop("AGENT_VOICE_INPUT_DISABLE_TMUX", None)

        self._client_proc: subprocess.Popen[bytes] | None = None
        self._master_fd: int | None = None
        self._slave_fd: int | None = None
        # Registered AFTER self._tmux_tmp.cleanup so LIFO runs _cleanup_tmux BEFORE directory removal.
        self.addCleanup(self._cleanup_tmux)

    def _tmux(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["tmux", "-L", self.sock_name, "-f", "/dev/null", *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=self.base_env,
            timeout=5.0,
            check=check,
        )

    def _cleanup_tmux(self) -> None:
        # 1. Terminate isolated tmux server first so attached client exits cleanly.
        try:
            self._tmux("kill-server", check=False)
        except (OSError, subprocess.SubprocessError):
            pass

        # 2. Close PTY master_fd (and any leftover slave_fd) BEFORE reaping _client_proc
        # to unblock Darwin XNU kernel ttywait().
        for fd_attr in ("_master_fd", "_slave_fd"):
            fd = getattr(self, fd_attr)
            if fd is not None:
                setattr(self, fd_attr, None)
                try:
                    os.close(fd)
                except OSError:
                    pass

        # 3. Reap _client_proc after server shutdown and master PTY closure.
        proc = self._client_proc
        self._client_proc = None
        if proc is not None:
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                try:
                    proc.terminate()
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    try:
                        proc.wait(timeout=1.0)
                    except subprocess.SubprocessError:
                        pass
                except OSError:
                    pass

    def _drain_master(self, wait_sec: float = 0.05) -> bytes:
        assert self._master_fd is not None
        chunks: list[bytes] = []
        while select.select([self._master_fd], [], [], wait_sec)[0]:
            try:
                data = os.read(self._master_fd, 8192)
            except BlockingIOError:
                break
            except InterruptedError:
                continue
            except OSError as exc:
                if exc.errno in (errno.EIO, errno.ENXIO):
                    break
                raise
            if not data:
                break
            chunks.append(data)
        return b"".join(chunks)

    def _attach_real_client(self, session_name: str = "s0") -> None:
        self._master_fd, self._slave_fd = pty.openpty()
        os.set_blocking(self._master_fd, False)
        self._client_proc = subprocess.Popen(
            ["tmux", "-L", self.sock_name, "-f", "/dev/null", "attach-session", "-t", session_name],
            stdin=self._slave_fd,
            stdout=self._slave_fd,
            stderr=self._slave_fd,
            env=self.base_env,
            start_new_session=True,
            close_fds=True,
        )
        os.close(self._slave_fd)
        self._slave_fd = None

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            r = self._tmux("list-clients", "-F", "#{client_session}", check=False)
            if session_name in r.stdout.splitlines():
                break
            time.sleep(0.05)
        self._drain_master(0.05)

    def _write_claude_shim(self, fake_bin: Path) -> Path:
        claude_shim = fake_bin / "claude"
        claude_shim.write_text(
            f"#!{sys.executable}\n"
            "import json, os, signal, subprocess, sys, time\n"
            "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
            "sid, tp, cwd, cli_bin, rdy = sys.argv[1:6]\n"
            "if sid != 'none':\n"
            "    payload = json.dumps({'session_id': sid, 'transcript_path': tp, 'cwd': cwd})\n"
            "    subprocess.run([cli_bin, 'session-hook', '--cli', 'claude'], input=payload, text=True, check=True)\n"
            "open(rdy, 'w').write(str(os.getpid()))\n"
            "while True:\n"
            "    time.sleep(0.2)\n",
            encoding="utf-8",
        )
        os.chmod(claude_shim, 0o755)
        return claude_shim

    def test_real_tmux_cross_platform_hooks_focus_discovery_escape_and_paste(self) -> None:
        proj_dir = self.base / "repo"
        proj_dir.mkdir()
        fake_bin = self.base / "fake_bin"
        fake_bin.mkdir()
        claude_shim = self._write_claude_shim(fake_bin)
        cli_entry = str(REPO_ROOT / "bin" / "agent-voice-input")

        # 1. Start isolated server & verify short socket path (< 55 UTF-8 bytes for Darwin SUN_PATH safety)
        self._tmux("new-session", "-d", "-s", "s0", "-x", "120", "-y", "30", "-c", str(proj_dir))
        sock_file = self.tmux_tmpdir / f"tmux-{os.getuid()}" / self.sock_name
        self.assertTrue(stat.S_ISSOCK(os.stat(sock_file).st_mode))
        self.assertLess(len(str(sock_file).encode("utf-8")), 55)

        self._attach_real_client("s0")
        self._tmux("split-window", "-h", "-t", "s0", "-c", str(proj_dir))
        panes_raw = self._tmux("list-panes", "-t", "s0", "-F", "#{pane_id}").stdout.splitlines()
        self.assertEqual(panes_raw, ["%0", "%1"])

        # 2. Dual-pane session_title hook registration & verify background hook NEVER steals focus
        cl_0 = "aaaa1111-0000-4000-8000-000000000010"
        cl_1 = "bbbb2222-0000-4000-8000-000000000020"
        f_cl_0 = _write_claude_jsonl(self.home, cl_0, str(proj_dir), "左窗格Claude对话", mtime=time.time() + 120)
        f_cl_1 = _write_claude_jsonl(self.home, cl_1, str(proj_dir), "右窗格Claude对话", mtime=time.time() - 120)
        rdy_cl0, rdy_cl1 = self.base / "rdy_cl0", self.base / "rdy_cl1"

        self._tmux("select-pane", "-t", "%1")
        self._tmux(
            "respawn-pane",
            "-k",
            "-t",
            "%1",
            f"env HOME={shlex.quote(str(self.home))} XDG_RUNTIME_DIR={shlex.quote(str(self.xdg))} "
            f"PYTHONPATH={shlex.quote(str(SRC_ROOT))} "
            f"{shlex.quote(str(claude_shim))} {cl_1} {shlex.quote(str(f_cl_1))} "
            f"{shlex.quote(str(proj_dir))} {shlex.quote(cli_entry)} {shlex.quote(str(rdy_cl1))}",
        )
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline and not rdy_cl1.is_file():
            time.sleep(0.05)
        self.assertTrue(rdy_cl1.is_file())

        # Trigger %0's hook in the background while %1 retains focus
        self._tmux(
            "respawn-pane",
            "-k",
            "-t",
            "%0",
            f"env HOME={shlex.quote(str(self.home))} XDG_RUNTIME_DIR={shlex.quote(str(self.xdg))} "
            f"PYTHONPATH={shlex.quote(str(SRC_ROOT))} "
            f"{shlex.quote(str(claude_shim))} {cl_0} {shlex.quote(str(f_cl_0))} "
            f"{shlex.quote(str(proj_dir))} {shlex.quote(cli_entry)} {shlex.quote(str(rdy_cl0))}",
        )
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline and not rdy_cl0.is_file():
            time.sleep(0.05)
        self.assertTrue(rdy_cl0.is_file())

        active_check = self._tmux("list-panes", "-t", "s0", "-F", "#{pane_id}:#{pane_active}:#{pane_title}").stdout.splitlines()
        self.assertTrue(any(ln.startswith("%1:1:") and f"[#{cl_1[:8]}]" in ln for ln in active_check), active_check)
        self.assertTrue(any(ln.startswith("%0:0:") and f"[#{cl_0[:8]}]" in ln for ln in active_check), active_check)

        tracker = SessionTracker(
            home_dir=self.home,
            runtime_dir=self.rt,
            env=self.base_env,
            tmux_socket_name=self.sock_name,
        )
        snap_cl1 = tracker.sync_session(window_title="zsh")
        assert snap_cl1 is not None
        self.assertEqual((snap_cl1.match.cli, snap_cl1.match.session_id, snap_cl1.match.matched_by), ("claude", cl_1, "tmux_registry"))
        self.assertEqual(list(snap_cl1.clean_turns), [("user", "右窗格Claude对话")])

        # Switch focus to %0 -> follows focus to cl_0
        self._tmux("select-pane", "-t", "%0")
        snap_cl0 = tracker.sync_session(window_title="zsh")
        assert snap_cl0 is not None
        self.assertEqual((snap_cl0.match.cli, snap_cl0.match.session_id, snap_cl0.match.matched_by), ("claude", cl_0, "tmux_registry"))
        self.assertEqual(list(snap_cl0.clean_turns), [("user", "左窗格Claude对话")])

        # 3. Daemon auto-discovery of custom `tmux -L` server solely via registry `tmux_socket`
        # (no $TMUX env and no constructor tmux_socket / tmux_socket_name overrides!)
        daemon_env = {k: v for k, v in self.base_env.items() if k not in ("TMUX", "TMUX_PANE")}
        daemon_tracker = SessionTracker(home_dir=self.home, runtime_dir=self.rt, env=daemon_env)
        m_auto_0 = daemon_tracker.resolve_session("zsh")
        assert m_auto_0 is not None
        self.assertEqual((m_auto_0.cli, m_auto_0.session_id, m_auto_0.matched_by), ("claude", cl_0, "tmux_registry"))

        self._tmux("select-pane", "-t", "%1")
        m_auto_1 = daemon_tracker.resolve_session("zsh")
        assert m_auto_1 is not None
        self.assertEqual((m_auto_1.cli, m_auto_1.session_id, m_auto_1.matched_by), ("claude", cl_1, "tmux_registry"))

        # 4. Verify select-pane -T escaping preserves literal '#T', '#{pane_id}', and trailing ';' on real tmux
        pane0_tty = self._tmux("display-message", "-p", "-t", "%0", "#{pane_tty}").stdout.strip()
        tricky_title = "claude · ~/dir-#T-#{pane_id}; [#11223344];"
        sync_sock, sync_pane = VST._resolve_and_sync_tmux_pane(
            {**daemon_env, "TMUX": f"{sock_file},999,0", "TMUX_PANE": "%0"},
            tty_path=pane0_tty,
            title=tricky_title,
            proc_root=Path("/proc"),
            explicit_env=True,
        )
        self.assertEqual(sync_pane, "%0")
        self.assertEqual(sync_sock, preferred_tmux_socket_arg(sock_file))
        self.assertEqual(os.path.realpath(sync_sock), os.path.realpath(sock_file))
        actual_pane_title = self._tmux("display-message", "-p", "-t", "%0", "#{pane_title}").stdout.rstrip("\n")
        self.assertEqual(actual_pane_title, tricky_title)

        # 5. Bracketed Paste multiline Chinese + code passthrough into active pane via outer client PTY
        paste_out = self.base / "paste_captured.bin"
        paste_rdy = self.base / "paste_rdy"
        paste_reader = self.base / "paste_reader.py"
        paste_reader.write_text(
            "import os, select, sys, termios, tty\n"
            "fd = sys.stdin.fileno()\n"
            "old = termios.tcgetattr(fd)\n"
            "try:\n"
            "    tty.setraw(fd)\n"
            "    os.write(sys.stdout.fileno(), b'\\x1b[?2004h')\n"
            "    open(sys.argv[2], 'w').write('ok')\n"
            "    buf = b''\n"
            "    for _ in range(40):\n"
            "        if select.select([fd], [], [], 0.05)[0]:\n"
            "            buf += os.read(fd, 4096)\n"
            "            if b'\\x1b[201~' in buf:\n"
            "                if select.select([fd], [], [], 0.08)[0]:\n"
            "                    buf += os.read(fd, 4096)\n"
            "                break\n"
            "    open(sys.argv[1], 'wb').write(buf)\n"
            "finally:\n"
            "    os.write(sys.stdout.fileno(), b'\\x1b[?2004l')\n"
            "    termios.tcsetattr(fd, termios.TCSADRAIN, old)\n",
            encoding="utf-8",
        )
        self._tmux(
            "split-window",
            "-v",
            "-t",
            "s0",
            f"{shlex.quote(sys.executable)} {shlex.quote(str(paste_reader))} "
            f"{shlex.quote(str(paste_out))} {shlex.quote(str(paste_rdy))}",
        )
        self._tmux("select-pane", "-t", "%2")
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline and not paste_rdy.is_file():
            time.sleep(0.05)
        self.assertTrue(paste_rdy.is_file())
        self._drain_master(0.05)

        assert self._master_fd is not None
        expected_paste = "\x1b[200~第一行中文：检查 session_ctx.py 分屏焦点\n第二行代码：return None\x1b[201~".encode("utf-8")
        os.write(self._master_fd, expected_paste)

        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline and not (paste_out.is_file() and paste_out.stat().st_size > 0):
            time.sleep(0.05)
        self.assertTrue(paste_out.is_file())
        received_bytes = paste_out.read_bytes()
        self.assertEqual(received_bytes, expected_paste)
        self.assertTrue(received_bytes.endswith(b"\x1b[201~"))

        # Verify cleanup completely terminates the isolated tmux server
        self._cleanup_tmux()
        after = self._tmux("list-sessions", check=False)
        self.assertNotEqual(after.returncode, 0)

    @unittest.skipUnless(Path("/proc").is_dir(), "Linux /proc required")
    def test_real_tmux_linux_proc_scoping_reopen_stale_uuid_and_empty_pane(self) -> None:
        proj_dir = self.base / "repo"
        proj_dir.mkdir()
        fake_bin = self.base / "fake_bin"
        fake_bin.mkdir()

        # Direct #!{sys.executable} shebang ensures Linux kernel sets /proc/<pid>/comm to 'codex'/'claude'
        codex_shim = fake_bin / "codex"
        codex_shim.write_text(
            f"#!{sys.executable}\n"
            "import os, signal, sys, time\n"
            "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
            "f = open(sys.argv[1], 'r+', encoding='utf-8')\n"
            "open(sys.argv[2], 'w').write(str(os.getpid()))\n"
            "while True:\n"
            "    time.sleep(0.2)\n",
            encoding="utf-8",
        )
        os.chmod(codex_shim, 0o755)
        claude_shim = self._write_claude_shim(fake_bin)
        cli_entry = str(REPO_ROOT / "bin" / "agent-voice-input")

        self._tmux("new-session", "-d", "-s", "s0", "-x", "120", "-y", "30", "-c", str(proj_dir))
        self._attach_real_client("s0")
        self._tmux("split-window", "-h", "-t", "s0", "-c", str(proj_dir))
        panes_raw = self._tmux("list-panes", "-t", "s0", "-F", "#{pane_id}").stdout.splitlines()
        self.assertEqual(panes_raw, ["%0", "%1"])

        # 1. Hookless Codex in %0 (background newer mtime) vs %1 (active older mtime)
        cx_bg = "0196aaaa-1111-7000-8000-000000000001"
        cx_fg = "0196bbbb-2222-7000-8000-000000000002"
        f_cx_bg = _write_codex_rollout(self.home, cx_bg, str(proj_dir), "后台队友Pane0", mtime=time.time() + 60)
        f_cx_fg = _write_codex_rollout(self.home, cx_fg, str(proj_dir), "当前焦点Pane1", mtime=time.time() - 60)
        rdy0, rdy1 = self.base / "rdy0", self.base / "rdy1"

        self._tmux(
            "respawn-pane",
            "-k",
            "-t",
            "%0",
            f"{shlex.quote(str(codex_shim))} {shlex.quote(str(f_cx_bg))} {shlex.quote(str(rdy0))}",
        )
        self._tmux(
            "respawn-pane",
            "-k",
            "-t",
            "%1",
            f"{shlex.quote(str(codex_shim))} {shlex.quote(str(f_cx_fg))} {shlex.quote(str(rdy1))}",
        )
        self._tmux("select-pane", "-t", "%1")

        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline and not (rdy0.is_file() and rdy1.is_file()):
            time.sleep(0.05)
        self.assertTrue(rdy0.is_file() and rdy1.is_file())
        os.utime(f_cx_bg, None)

        tracker = SessionTracker(
            home_dir=self.home,
            runtime_dir=self.rt,
            env=self.base_env,
            tmux_socket_name=self.sock_name,
        )
        snap_fg = tracker.sync_session(window_title="Ubuntu")
        self.assertIsNotNone(snap_fg)
        assert snap_fg is not None
        self.assertEqual(snap_fg.match.session_id, cx_fg)
        self.assertEqual(snap_fg.match.matched_by, "tmux_proc_scan")
        self.assertEqual(list(snap_fg.clean_turns), [("user", "当前焦点Pane1")])

        self._tmux("select-pane", "-t", "%0")
        snap_sw = tracker.sync_session(window_title="Ubuntu")
        assert snap_sw is not None
        self.assertEqual(snap_sw.match.session_id, cx_bg)

        # 2. Hooked Claude exits in %0, then hookless Claude reopens over sticky [#aaaa1111] #{pane_title}
        cl_0 = "aaaa1111-0000-4000-8000-000000000010"
        f_cl_0 = _write_claude_jsonl(self.home, cl_0, str(proj_dir), "左窗格旧Claude对话", mtime=time.time() + 120)
        rdy_cl0 = self.base / "rdy_cl0"
        self._tmux(
            "respawn-pane",
            "-k",
            "-t",
            "%0",
            f"env HOME={shlex.quote(str(self.home))} XDG_RUNTIME_DIR={shlex.quote(str(self.xdg))} "
            f"PYTHONPATH={shlex.quote(str(SRC_ROOT))} "
            f"{shlex.quote(str(claude_shim))} {cl_0} {shlex.quote(str(f_cl_0))} "
            f"{shlex.quote(str(proj_dir))} {shlex.quote(cli_entry)} {shlex.quote(str(rdy_cl0))}",
        )
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline and not rdy_cl0.is_file():
            time.sleep(0.05)
        self.assertTrue(rdy_cl0.is_file())
        m_cl0 = tracker.resolve_session("Ubuntu")
        assert m_cl0 is not None
        self.assertEqual((m_cl0.cli, m_cl0.session_id, m_cl0.matched_by), ("claude", cl_0, "tmux_registry"))

        cl_reopen = "cccc3333-0000-4000-8000-000000000030"
        _write_claude_jsonl(self.home, cl_reopen, str(proj_dir), "重开的无钩子Claude会话", mtime=time.time() + 240)
        rdy_reopen = self.base / "rdy_reopen"
        self._tmux(
            "respawn-pane",
            "-k",
            "-t",
            "%0",
            f"env HOME={shlex.quote(str(self.home))} XDG_RUNTIME_DIR={shlex.quote(str(self.xdg))} "
            f"PYTHONPATH={shlex.quote(str(SRC_ROOT))} "
            f"{shlex.quote(str(claude_shim))} none - "
            f"{shlex.quote(str(proj_dir))} {shlex.quote(cli_entry)} {shlex.quote(str(rdy_reopen))}",
        )
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline and not rdy_reopen.is_file():
            time.sleep(0.05)
        self.assertTrue(rdy_reopen.is_file())
        sticky_title = self._tmux("display-message", "-p", "-t", "%0", "#{pane_title}").stdout.strip()
        self.assertIn(f"[#{cl_0[:8]}]", sticky_title)
        m_reopen = tracker.resolve_session("Ubuntu")
        assert m_reopen is not None
        self.assertEqual((m_reopen.cli, m_reopen.session_id, m_reopen.matched_by), ("claude", cl_reopen, "tmux_proc_scan"))

        # 3. Real /proc stale outer Codex UUID (no live codex process holding fd) falls through to active pane
        dead_cx_real = "0196dead-dead-7000-8000-deadbeef9999"
        _write_codex_rollout(self.home, dead_cx_real, str(proj_dir), "已退出无进程Codex", mtime=time.time() + 500)
        m_stale_uuid = tracker.resolve_session(f"⠋ repo · Old dead codex · {dead_cx_real}")
        assert m_stale_uuid is not None
        self.assertEqual((m_stale_uuid.cli, m_stale_uuid.session_id), ("claude", cl_reopen))

        # 4. Empty shell pane %2 while unbound disk sessions & neighbor pane agents exist -> MUST return None
        self._tmux("split-window", "-v", "-t", "s0", "bash --norc --noprofile")
        self._tmux("select-pane", "-t", "%2")
        self.assertIsNone(tracker.resolve_session("Ubuntu"))


class DarwinTmuxGhosttyUnitTests(unittest.TestCase):
    def test_darwin_ghostty_window_tty_and_tmux_multi_pane_convergence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            rt = root / "runtime"
            no_proc = root / "no_proc"  # Simulates macOS (no /proc filesystem)
            proj_a = root / "proj_a"
            proj_b = root / "proj_b"
            proj_plain = root / "proj_plain"
            for d in (home, rt, proj_a, proj_b, proj_plain):
                d.mkdir(parents=True, exist_ok=True)

            cl_pane0 = "1111aaaa-2222-4000-8000-000000000001"
            cl_pane1 = "2222bbbb-3333-4000-8000-000000000002"
            cx_plain = "0196aaaa-bbbb-7000-8000-000000000099"
            now = time.time()
            _write_claude_jsonl(home, cl_pane0, str(proj_a), "Ghostty左tmux客户端焦点窗格", mtime=now - 10)
            _write_claude_jsonl(home, cl_pane1, str(proj_b), "Ghostty右tmux后台相邻窗格", mtime=now)
            plain_rollout = _write_codex_rollout(home, cx_plain, str(proj_plain), "Ghostty普通非tmux分屏", mtime=now - 5)

            snap = TmuxServerSnapshot(
                clients=(
                    # Client 1 on /dev/ttys002 (older activity, focused when Ghostty split is on ttys002)
                    TmuxClientInfo(
                        session_id="$0",
                        window_id="@0",
                        pane_id="%0",
                        activity=1000,
                        client_tty="/dev/ttys002",
                        client_pid=4100,
                        focused=False,
                        socket_path="/private/tmp/tmux-501/default",
                    ),
                    # Client 2 on /dev/ttys009 (newer activity, focused on %1)
                    TmuxClientInfo(
                        session_id="$1",
                        window_id="@1",
                        pane_id="%1",
                        activity=2000,
                        client_tty="/dev/ttys009",
                        client_pid=4200,
                        focused=True,
                        socket_path="/private/tmp/tmux-501/default",
                    ),
                ),
                panes=(
                    TmuxPaneInfo(
                        session_id="$0",
                        window_id="@0",
                        window_active=True,
                        pane_id="%0",
                        pane_active=True,
                        pane_pid=5100,
                        pane_tty="/dev/ttys020",
                        socket_path="/private/tmp/tmux-501/default",
                        current_path=str(proj_a),
                        pane_title="zsh",
                    ),
                    TmuxPaneInfo(
                        session_id="$1",
                        window_id="@1",
                        window_active=True,
                        pane_id="%1",
                        pane_active=True,
                        pane_pid=5200,
                        pane_tty="/dev/ttys021",
                        socket_path="/private/tmp/tmux-501/default",
                        current_path=str(proj_b),
                        pane_title="zsh",
                    ),
                    TmuxPaneInfo(
                        session_id="$0",
                        window_id="@0",
                        window_active=False,
                        pane_id="%2",
                        pane_active=True,
                        pane_pid=5300,
                        pane_tty="/dev/ttys022",
                        socket_path="/private/tmp/tmux-501/default",
                        current_path=str(proj_a),
                        pane_title="zsh",
                    ),
                ),
            )

            tty_ps_map = {
                "ttys020": (
                    "  PID  PPID STAT TTY      COMMAND\n"
                    " 5100  4099 Ss   ttys020  -zsh\n"
                    " 5150  5100 S+   ttys020  claude\n"
                ),
                "ttys021": (
                    "  PID  PPID STAT TTY      COMMAND\n"
                    " 5200  4099 Ss   ttys021  -zsh\n"
                    " 5250  5200 S+   ttys021  claude\n"
                ),
                "ttys022": (
                    "  PID  PPID STAT TTY      COMMAND\n"
                    " 5300  4099 Ss+  ttys022  -zsh\n"
                ),
                "ttys011": (
                    "  PID  PPID STAT TTY      COMMAND\n"
                    " 6100  4000 Ss   ttys011  -zsh\n"
                    " 6150  6100 S+   ttys011  /opt/homebrew/bin/codex\n"
                ),
            }

            def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
                if cmd and cmd[0] == "ps" and "-t" in cmd:
                    tty_short = cmd[cmd.index("-t") + 1]
                    out = tty_ps_map.get(tty_short, "")
                    return subprocess.CompletedProcess(cmd, 0 if out else 1, stdout=out.encode("utf-8"), stderr=b"")
                if cmd and cmd[0] == "lsof":
                    pid_arg = cmd[cmd.index("-p") + 1] if "-p" in cmd else ""
                    if pid_arg == "5150":
                        out = f"p5150\nfcwd\nn{proj_a}\n"
                    elif pid_arg == "5250":
                        out = f"p5250\nfcwd\nn{proj_b}\n"
                    elif pid_arg == "6150":
                        out = f"p6150\nfcwd\nn{proj_plain}\nf12\nn{plain_rollout}\n"
                    else:
                        out = ""
                    return subprocess.CompletedProcess(cmd, 0 if out else 1, stdout=out.encode("utf-8"), stderr=b"")
                return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"")

            tracker = SessionTracker(
                runtime_dir=rt,
                home_dir=home,
                proc_root=no_proc,
                tmux_probe_fn=lambda: snap,
            )

            with mock.patch("agent_voice_input.session_ctx.subprocess.run", side_effect=fake_run):
                # 1. Ghostty split on /dev/ttys002 locks onto Client 1 (%0 -> proj_a -> cl_pane0)
                #    even though Client 2 (%1 -> cl_pane1) has higher activity & newer mtime!
                m_c1 = tracker.resolve_session(
                    "Ghostty",
                    window_tty="/dev/ttys002",
                    window_cwd=str(home),
                    window_pid=4000,
                )
                assert m_c1 is not None
                self.assertEqual((m_c1.cli, m_c1.session_id, m_c1.matched_by), ("claude", cl_pane0, "tmux_proc_scan"))

                # 2. Plain non-tmux Ghostty split on /dev/ttys011 resolves its own Codex session
                #    via tty_ps without being hijacked by attached tmux clients!
                m_plain = tracker.resolve_session(
                    "Ghostty",
                    window_tty="/dev/ttys011",
                    window_cwd=str(proj_plain),
                    window_pid=4000,
                )
                assert m_plain is not None
                self.assertEqual((m_plain.cli, m_plain.session_id, m_plain.matched_by), ("codex", cx_plain, "tty_ps"))

                # 3. Empty shell pane inside tmux on Darwin returns None (never cross-attaches to sibling pane)
                snap_empty = TmuxServerSnapshot(
                    clients=(
                        TmuxClientInfo(
                            session_id="$0",
                            window_id="@0",
                            pane_id="%2",
                            activity=3000,
                            client_tty="/dev/ttys002",
                            client_pid=4100,
                            focused=True,
                            socket_path="/private/tmp/tmux-501/default",
                        ),
                    ),
                    panes=(
                        snap.panes[0],
                        snap.panes[1],
                        TmuxPaneInfo(
                            session_id="$0",
                            window_id="@0",
                            window_active=True,
                            pane_id="%2",
                            pane_active=True,
                            pane_pid=5300,
                            pane_tty="/dev/ttys022",
                            socket_path="/private/tmp/tmux-501/default",
                            current_path=str(proj_a),
                            pane_title="zsh",
                        ),
                    ),
                )
                tracker_empty = SessionTracker(
                    runtime_dir=rt,
                    home_dir=home,
                    proc_root=no_proc,
                    tmux_probe_fn=lambda: snap_empty,
                )
                self.assertIsNone(
                    tracker_empty.resolve_session(
                        "Ghostty",
                        window_tty="/dev/ttys002",
                        window_cwd=str(home),
                        window_pid=4000,
                    )
                )


if __name__ == "__main__":
    unittest.main()
