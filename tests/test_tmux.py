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


def _is_ci() -> bool:
    return os.environ.get("CI", "").strip().lower() in {"1", "true", "yes"}

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
    collect_darwin_window_pty_tree,
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
            m_dir_tab2 = tracker.resolve_session("dev@host: ~/worktrees/tmux-support")
            assert m_dir_tab2 is not None
            self.assertEqual((m_dir_tab2.cli, m_dir_tab2.session_id), ("claude", tab2_sid))

            # Rule 2c: Tab 1 outer title explicitly 'tmux' or 'proj-alpha' -> picks Tab 1 tmux active pane!
            m_tmux_word = tracker.resolve_session("dev@host: ~ (tmux)")
            assert m_tmux_word is not None
            self.assertEqual((m_tmux_word.cli, m_tmux_word.session_id), ("agy", agy_cid))

            m_dir_tab1 = tracker.resolve_session("dev@host: ~/repos/proj-alpha")
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
            home_base = root / "users" / "dev"
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

            # tmux client launched from /users/dev (parent of /users/dev/repos/proj-beta!)
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

            # Bug C: Intermediate parent directory '/dev/' in 'dev@host: /users/dev/repos/proj-beta'
            # MUST NOT false-match tmux cwd '/users/dev', AND client_pid cwd is ignored when pane current_path is set!
            pane_state["title"] = "bash"
            pane_state["cwd"] = str(home_base)
            m_midpath = tracker.resolve_session(f"dev@host: {proj_beta}")
            assert m_midpath is not None
            self.assertIn(m_midpath.session_id, (cl_tab2, cx_tab2))

            # Even if client_pid 400 cwd points to proj_beta, non-empty active_pane.current_path (proj_alpha)
            # takes sole precedence so outer title naming proj-alpha resolves tmux!
            (proc / "400" / "cwd").unlink()
            os.symlink(str(proj_beta), proc / "400" / "cwd")
            pane_state["cwd"] = str(proj_alpha)
            m_pane_cwd_wins = tracker.resolve_session(f"dev@host: {proj_alpha}")
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


class TmuxRealIsolatedIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("tmux") is None:
            if _is_ci():
                self.fail("tmux must be installed in CI (silent skip forbidden)")
            self.skipTest("tmux not installed")
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

        self.cache_dir = self.base / "cache"
        self.cache_dir.mkdir(parents=True)
        self.base_env = {
            **os.environ,
            "HOME": str(self.home),
            "XDG_RUNTIME_DIR": str(self.xdg),
            "XDG_CACHE_HOME": str(self.cache_dir),
            "TMUX_TMPDIR": str(self.tmux_tmpdir),
            "TERM": "xterm-256color",
            "PYTHONPATH": str(SRC_ROOT),
        }
        for _pop_key in (
            "TMUX",
            "TMUX_PANE",
            "AGENT_VOICE_INPUT_DISABLE_TMUX",
            "AGENT_VOICE_INPUT_TMUX_HINT",
            "AGENT_VOICE_INPUT_RUNTIME_DIR",
            "VOICE_INPUT_RUNTIME_DIR",
            "SSH_CONNECTION",
            "SSH_CLIENT",
            "SSH_TTY",
            "AGENT_VOICE_INPUT_HOST_TAG",
        ):
            self.base_env.pop(_pop_key, None)

        self._pre_host_pids: set[int] = set()
        if sys.platform == "darwin":
            _ps_pre = subprocess.run(
                ["ps", "-u", str(os.getuid()), "-o", "pid="],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            self._pre_host_pids = {
                int(tok) for tok in _ps_pre.stdout.split() if tok.isdigit()
            }

        self._host_lsof_violations: list[int] = []
        _orig_sc_run = subprocess.run

        def _spy_sc_run(cmd, *args, **kwargs):
            if cmd and os.path.basename(str(cmd[0])) == "lsof" and "-p" in cmd:
                p_arg = str(cmd[cmd.index("-p") + 1])
                for tok in p_arg.split(","):
                    if tok.isdigit() and int(tok) in self._pre_host_pids:
                        self._host_lsof_violations.append(int(tok))
            return _orig_sc_run(cmd, *args, **kwargs)

        _spy_patcher = mock.patch("agent_voice_input.session_ctx.subprocess.run", side_effect=_spy_sc_run)
        _spy_patcher.start()
        self.addCleanup(_spy_patcher.stop)
        self.addCleanup(lambda: self.assertEqual(self._host_lsof_violations, [], "lsof queried host external PIDs"))

        _scan_patcher = mock.patch.object(
            SessionTracker, "_scan_darwin_live_non_tmux_sessions", return_value=[]
        )
        _scan_patcher.start()
        self.addCleanup(_scan_patcher.stop)

        _orig_list_rows = SessionTracker._list_darwin_user_ps_rows
        _iso_markers = (str(self.base), str(self.tmux_tmpdir))

        def _isolated_darwin_ps_rows(tracker_self: SessionTracker):
            rows = _orig_list_rows(tracker_self)
            if rows is None:
                return None
            return [
                row
                for row in rows
                if any(m in " ".join(row[4]) for m in _iso_markers)
            ]

        _rows_patcher = mock.patch.object(
            SessionTracker, "_list_darwin_user_ps_rows", _isolated_darwin_ps_rows
        )
        _rows_patcher.start()
        self.addCleanup(_rows_patcher.stop)

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

    def test_real_tmux_linux_proc_scoping_reopen_stale_uuid_and_empty_pane(self) -> None:
        proj_dir = self.base / "repo"
        proj_dir.mkdir()
        fake_bin = self.base / "fake_bin"
        fake_bin.mkdir()

        # #!{sys.executable} exercises both Linux /proc comm='codex' and macOS ps 'Python .../fake_bin/codex'
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

        # 1b. Zero-hook custom `tmux -L` server auto-discovery (no $TMUX, no registry, no constructor socket arg!)
        daemon_env = {k: v for k, v in self.base_env.items() if k not in ("TMUX", "TMUX_PANE")}
        zero_hook_daemon_tracker = SessionTracker(
            home_dir=self.home,
            runtime_dir=self.rt,
            env=daemon_env,
        )
        m_auto_cx = zero_hook_daemon_tracker.resolve_session("Ubuntu")
        assert m_auto_cx is not None
        self.assertEqual((m_auto_cx.cli, m_auto_cx.session_id, m_auto_cx.matched_by), ("codex", cx_bg, "tmux_proc_scan"))

        # 1c. Ghostty ForegroundWindow (window_tty=client_tty) with stale pre-tmux outer UUID falls through to active pane!
        client_tty, client_pid_s = (
            self._tmux("list-clients", "-F", "#{client_tty}\t#{client_pid}")
            .stdout.splitlines()[0]
            .split("\t")
        )
        stale_pre_tmux_cx = "0196cafe-cafe-7000-8000-00000000cafe"
        _write_codex_rollout(self.home, stale_pre_tmux_cx, str(proj_dir), "进tmux前残留外层标题", mtime=time.time() + 300)
        m_ghostty_tmux = zero_hook_daemon_tracker.resolve_session(
            f"codex · {stale_pre_tmux_cx}",
            window_tty=client_tty,
            window_cwd=str(self.home),
            window_pid=int(client_pid_s),
        )
        assert m_ghostty_tmux is not None
        self.assertEqual((m_ghostty_tmux.cli, m_ghostty_tmux.session_id), ("codex", cx_bg))

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

        # 3. Stale outer Codex UUID (no live codex process holding fd) falls through to active pane on Linux & macOS
        #    First terminate %1's codex_shim so only %0's claude is running on this server, while testing dead_cx_real.
        dead_cx_real = "0196dead-dead-7000-8000-deadbeef9999"
        _write_codex_rollout(self.home, dead_cx_real, str(proj_dir), "已退出无进程Codex", mtime=time.time() + 500)
        m_stale_uuid = tracker.resolve_session(f"⠋ repo · Old dead codex · {dead_cx_real}")
        assert m_stale_uuid is not None
        self.assertEqual((m_stale_uuid.cli, m_stale_uuid.session_id), ("claude", cl_reopen))

        # 3b. Hookless Antigravity (agy) holding presence/<cid>.lock in %1 resolves via tmux_proc_scan
        agy_cid = "3333eeee-4444-4000-8000-000000000077"
        presence_dir = self.home / ".gemini" / "antigravity-cli" / "presence"
        presence_dir.mkdir(parents=True, exist_ok=True)
        agy_lock = presence_dir / f"{agy_cid}.lock"
        agy_lock.write_text("", encoding="utf-8")
        agy_logs = self.home / ".gemini" / "antigravity-cli" / "brain" / agy_cid / ".system_generated" / "logs"
        agy_logs.mkdir(parents=True, exist_ok=True)
        (agy_logs / "transcript.jsonl").write_text(
            json.dumps(
                {
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "content": f"<user_information>\nCommand Working Directory: {proj_dir}\n</user_information>\n<USER_REQUEST>排查WSL与macOS的tmux分屏</USER_REQUEST>",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        agy_shim = fake_bin / "agy"
        agy_shim.write_text(
            f"#!{sys.executable}\n"
            "import fcntl, os, signal, sys, time\n"
            "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
            "fd = os.open(sys.argv[1], os.O_RDWR)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "open(sys.argv[2], 'w').write(str(os.getpid()))\n"
            "while True:\n"
            "    time.sleep(0.2)\n",
            encoding="utf-8",
        )
        os.chmod(agy_shim, 0o755)
        rdy_agy = self.base / "rdy_agy"
        self._tmux(
            "respawn-pane",
            "-k",
            "-t",
            "%1",
            f"{shlex.quote(str(agy_shim))} {shlex.quote(str(agy_lock))} {shlex.quote(str(rdy_agy))}",
        )
        self._tmux("select-pane", "-t", "%1")
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline and not rdy_agy.is_file():
            time.sleep(0.05)
        self.assertTrue(rdy_agy.is_file())
        snap_agy = zero_hook_daemon_tracker.sync_session(window_title="Ubuntu")
        assert snap_agy is not None
        self.assertEqual((snap_agy.match.cli, snap_agy.match.session_id, snap_agy.match.matched_by), ("agy", agy_cid, "tmux_proc_scan"))
        self.assertEqual(list(snap_agy.clean_turns), [("user", "排查WSL与macOS的tmux分屏")])

        # 3c. End-to-end WSL2 WslPlatform (hwnd=98765, process="WindowsTerminal", title="Ubuntu", tty="", cwd="", pid=0)
        #     driving VoiceInputDaemon against the real attached tmux server!
        from agent_voice_input.asr_providers import AsrContext, AsrResult, BaseAsrProvider
        from agent_voice_input.config import Config, RefinerConfig
        from agent_voice_input.daemon import VoiceInputDaemon
        from agent_voice_input.platform_wsl import WslPlatform
        from agent_voice_input.refiner import create_refiner
        import base64
        import io
        import wave

        buf_wav = io.BytesIO()
        with wave.open(buf_wav, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x10\x20" * 6400)
        wav_b64 = base64.b64encode(buf_wav.getvalue()).decode("ascii")

        mock_win_shim = (
            "import json, sys\n"
            f"WAV_B64 = {wav_b64!r}\n"
            "sys.stdout.write(json.dumps({'event': 'ready', 'ui_culture': 'zh-CN'}) + '\\n')\n"
            "sys.stdout.flush()\n"
            "for raw in sys.stdin:\n"
            "    req = json.loads(raw)\n"
            "    cmd = req.get('cmd')\n"
            "    sid = req.get('seq_id', 0)\n"
            "    if cmd == 'quit':\n"
            "        break\n"
            "    if cmd == 'fg_window':\n"
            "        rep = {'reply': 'fg_window', 'seq_id': sid, 'hwnd': 98765, 'process': 'WindowsTerminal', 'title': 'Ubuntu'}\n"
            "    elif cmd == 'rec_start':\n"
            "        rep = {'reply': 'rec_start', 'seq_id': sid, 'ok': True}\n"
            "    elif cmd == 'rec_stop':\n"
            "        rep = {'reply': 'rec_stop', 'seq_id': sid, 'ok': True, 'wav_b64': WAV_B64}\n"
            "    elif cmd == 'paste':\n"
            "        rep = {'reply': 'paste', 'seq_id': sid, 'pasted': req.get('target_hwnd') == 98765, 'clipboard': True, 'reason': 'ok', 'fg_process': 'WindowsTerminal'}\n"
            "    else:\n"
            "        rep = {'reply': cmd, 'seq_id': sid, 'ok': True}\n"
            "    sys.stdout.write(json.dumps(rep) + '\\n')\n"
            "    sys.stdout.flush()\n"
        )

        class _CaptureAsr(BaseAsrProvider):
            provider_name = "dashscope"

            def __init__(self) -> None:
                self.contexts: list[AsrContext | None] = []

            def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None, **_kw: object) -> AsrResult:
                self.contexts.append(context)
                return AsrResult(text="WSL端tmux全链路验证通过。", provider="dashscope", model="qwen3-asr-flash", latency_ms=5)

        wsl_cfg = Config(refiner=RefinerConfig(provider="off"))
        wsl_cfg.audio.wsl_source = "winmm"
        wsl_cfg.audio.tail_drain_sec = 0.0
        wsl_plat = WslPlatform(
            wsl_cfg,
            env=daemon_env,
            shim_cmd_factory=lambda: [sys.executable, "-u", "-c", mock_win_shim],
        )
        cap_asr = _CaptureAsr()
        wsl_daemon = VoiceInputDaemon(
            wsl_cfg,
            env=daemon_env,
            runtime_dir=self.rt / "wsl_daemon",
            platform=wsl_plat,
            asr_provider=cap_asr,
            refiner=create_refiner(wsl_cfg),
            tracker=SessionTracker(home_dir=self.home, runtime_dir=self.rt, env=daemon_env),
        )
        try:
            wsl_plat.start_event_listener(lambda _e: None)
            r_on = wsl_daemon.handle_trigger("toggle")
            self.assertTrue(r_on["ok"])
            self.assertEqual(wsl_daemon._origin_win.hwnd, 98765)
            self.assertEqual(wsl_daemon._origin_win.tty, "")
            self.assertEqual(wsl_daemon._origin_win.pid, 0)
            wsl_daemon._last_trigger_ts = -1e9
            r_off = wsl_daemon.handle_trigger("toggle")
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline and wsl_daemon.last_result.get("seq_id") != r_off["seq_id"]:
                time.sleep(0.03)
            self.assertTrue(wsl_daemon.last_result["ok"], wsl_daemon.last_result)
            self.assertEqual(wsl_daemon.last_result["session_key"], f"agy:{agy_cid}")
            self.assertTrue(wsl_daemon.last_result["pasted"])
            assert cap_asr.contexts and cap_asr.contexts[0] is not None
            self.assertIn("排查WSL与macOS的tmux分屏", cap_asr.contexts[0].dialog_text)
        finally:
            wsl_daemon.close()

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

    def test_interpreter_unwrapping_anti_self_match_wsl_same_cwd_clients_and_daemon_tmux_env(self) -> None:
        from agent_voice_input import cli as cli_mod
        from agent_voice_input.config import Config
        from agent_voice_input.platform_macos import MacOSPlatform, _normalize_title_for_drift
        from agent_voice_input.platform_wsl import ForegroundWindow
        from agent_voice_input.session_ctx import _match_cli_process

        # 1. Interpreter unwrapping (Python/Node/Bun) vs strict anti-self-match on session-hook child args
        self.assertEqual(
            _match_cli_process("Python", ["/opt/homebrew/Resources/Python.app/Contents/MacOS/Python", "/tmp/fake_bin/claude", "--resume"]),
            ("claude", ["/tmp/fake_bin/claude", "--resume"]),
        )
        self.assertEqual(
            _match_cli_process("python3.12", ["python3.12", "-u", "/usr/local/bin/codex"]),
            ("codex", ["/usr/local/bin/codex"]),
        )
        self.assertEqual(
            _match_cli_process("node", ["node", "--no-warnings", "-r", "dotenv/config", "/opt/@openai/codex/bin/codex.js"]),
            ("codex", ["/opt/@openai/codex/bin/codex.js"]),
        )
        for non_cli_argv in (
            ["python3", "/usr/local/bin/agent-voice-input", "session-hook", "--cli", "claude"],
            ["Python", "-m", "agent_voice_input.cli", "session-hook", "--cli", "claude"],
            ["python3", "-c", "import sys; print('claude')", "claude"],
            ["node", "/srv/other-tool.js", "--cli", "claude", "codex", "agy"],
        ):
            self.assertEqual(
                _match_cli_process(os.path.basename(non_cli_argv[0]), non_cli_argv)[0],
                None,
                f"must not misclassify {non_cli_argv!r} as an interactive agent CLI",
            )

        # 2. WSL2 multi-client tmux selection in SAME repo (window_tty='', window_pid=0):
        #    Shared _dir_basename_in_title ('shared-app') MUST NOT drown out exact [#tag] or pane_title on Client 1!
        shared_cwd = "/home/dev/repos/shared-app"
        wsl_multi_snap = TmuxServerSnapshot(
            clients=(
                TmuxClientInfo(
                    session_id="$0",
                    window_id="@0",
                    pane_id="%0",
                    activity=1000,
                    client_tty="/dev/pts/1",
                    client_pid=301,
                    focused=False,
                    socket_path="/tmp/tmux-1000/default",
                ),
                TmuxClientInfo(
                    session_id="$1",
                    window_id="@1",
                    pane_id="%1",
                    activity=2000,
                    client_tty="/dev/pts/2",
                    client_pid=302,
                    focused=True,
                    socket_path="/tmp/tmux-1000/default",
                ),
            ),
            panes=(
                TmuxPaneInfo(
                    session_id="$0",
                    window_id="@0",
                    window_active=True,
                    pane_id="%0",
                    pane_active=True,
                    pane_pid=401,
                    pane_tty="/dev/pts/3",
                    socket_path="/tmp/tmux-1000/default",
                    current_path=shared_cwd,
                    pane_title="claude · ~/repos/shared-app [#aaaa1111]",
                ),
                TmuxPaneInfo(
                    session_id="$1",
                    window_id="@1",
                    window_active=True,
                    pane_id="%1",
                    pane_active=True,
                    pane_pid=402,
                    pane_tty="/dev/pts/4",
                    socket_path="/tmp/tmux-1000/default",
                    current_path=shared_cwd,
                    pane_title="claude · ~/repos/shared-app [#bbbb2222]",
                ),
            ),
        )
        # Even when truncated outer title only carries [#aaaa1111] + shared repo name, Client 1 (%0) wins over higher-activity Client 2!
        picked_tok = SessionTracker._select_active_tmux_target(
            wsl_multi_snap, "0:claude* shared-app [#aaaa1111]", window_tty="", window_pid=0
        )
        assert picked_tok is not None
        self.assertEqual(picked_tok[1].pane_id, "%0")

        # Full pane_title substring in set-titles window title also picks Client 1 (%0) despite shared_cwd on Client 2
        picked_pt = SessionTracker._select_active_tmux_target(
            wsl_multi_snap, '0:0.0 "claude · ~/repos/shared-app [#aaaa1111]"', window_tty="", window_pid=0
        )
        assert picked_pt is not None
        self.assertEqual(picked_pt[1].pane_id, "%0")

        # 3. macOS inline spinner rotation inside tmux set-titles ('0:0.0 claude "⠋ ..."') does NOT trigger focus_changed
        self.assertEqual(
            _normalize_title_for_drift('0:0.0 claude "⠋ shared-app · Fix bug"'),
            _normalize_title_for_drift('0:0.0 claude "⠸ shared-app · Fix bug"'),
        )
        self.assertEqual(
            _normalize_title_for_drift('0:0.0 claude "⠋ shared-app · Fix bug"'),
            _normalize_title_for_drift('0:0.0 claude "shared-app · Fix bug"'),
        )
        self.assertNotEqual(
            _normalize_title_for_drift('0:0.0 claude "⠋ repo-a"'),
            _normalize_title_for_drift('0:0.1 claude "⠋ repo-b"'),
        )

        # 4. cli._cmd_start strips TMUX_PANE and moves TMUX -> AGENT_VOICE_INPUT_TMUX_HINT;
        #    probe_tmux_snapshot still discovers custom -S socket via AGENT_VOICE_INPUT_TMUX_HINT,
        #    while MacOSPlatform in once-mode strips TMUX_PANE before invoking tmux!
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rt = root / "rt"
            rt.mkdir()
            captured_popen_env: dict[str, str] = {}

            class _DummyDaemonProc:
                def poll(self) -> None:
                    return None

            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "HOME": str(root),
                        "XDG_RUNTIME_DIR": str(root / "xdg"),
                        "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
                        "TMUX": "/custom/sock/s1,999,0",
                        "TMUX_PANE": "%7",
                    },
                    clear=False,
                ),
                mock.patch(
                    "agent_voice_input.control_ipc.send_control_command",
                    side_effect=[{"ok": False}, {"ok": True, "pid": 4321}],
                ),
                mock.patch.object(
                    cli_mod.subprocess,
                    "Popen",
                    side_effect=lambda *a, **kw: (captured_popen_env.update(kw.get("env") or {}) or _DummyDaemonProc()),
                ),
                mock.patch("sys.stdout"),
            ):
                ns = cli_mod._build_parser().parse_args(["start"])
                rc_start = cli_mod._cmd_start(ns, dict(os.environ))
            self.assertEqual(rc_start, 0)
            self.assertNotIn("TMUX", captured_popen_env)
            self.assertNotIn("TMUX_PANE", captured_popen_env)
            self.assertEqual(captured_popen_env.get("AGENT_VOICE_INPUT_TMUX_HINT"), "/custom/sock/s1,999,0")

            import socket as sock_mod

            short_dir = Path(tempfile.mkdtemp(prefix="avi_h_", dir="/tmp"))
            srv_sock = sock_mod.socket(sock_mod.AF_UNIX, sock_mod.SOCK_STREAM)
            try:
                custom_sock = short_dir / "ext.sock"
                srv_sock.bind(str(custom_sock))
                probed_cmds: list[list[str]] = []

                def fake_tmux_probe(cmd, **kw):
                    probed_cmds.append(list(cmd))
                    return subprocess.CompletedProcess(cmd, 0, b"", b"")

                with (
                    mock.patch("agent_voice_input._tmux.resolve_tmux_bin", return_value="/usr/bin/tmux"),
                    mock.patch("subprocess.run", side_effect=fake_tmux_probe),
                ):
                    probe_tmux_snapshot(
                        env={
                            "TMUX_TMPDIR": str(root / "empty_t"),
                            "AGENT_VOICE_INPUT_TMUX_HINT": f"{custom_sock},999,0",
                        }
                    )
                self.assertEqual(len(probed_cmds), 1)
                self.assertEqual(probed_cmds[0][:3], ["/usr/bin/tmux", "-S", preferred_tmux_socket_arg(custom_sock)])
            finally:
                srv_sock.close()
                shutil.rmtree(short_dir, ignore_errors=True)

            # Verify MacOSPlatform once-mode strips TMUX_PANE when running display-message / paste-buffer
            seen_tmux_envs: list[dict[str, str]] = []
            cur_osa_title = ['0:0.0 "⠋ repo"']

            def fake_mac_run(cmd, *, env=None, **kw):
                exe = Path(cmd[0]).name
                if exe == "pbcopy":
                    return subprocess.CompletedProcess(cmd, 0, "", "")
                if exe == "tmux":
                    seen_tmux_envs.append(dict(env or {}))
                    return subprocess.CompletedProcess(cmd, 0, "pane-ok\n" if "display-message" in cmd else "", "")
                if exe == "osascript":
                    return subprocess.CompletedProcess(
                        cmd, 0, f"iTerm2\x1fcom.googlecode.iterm2\x1f500\x1f{cur_osa_title[0]}\n", ""
                    )
                raise AssertionError(f"unexpected {cmd}")

            with (
                mock.patch("agent_voice_input.platform_macos.shutil.which", return_value="/usr/bin/mock"),
                mock.patch("agent_voice_input.platform_macos.subprocess.run", side_effect=fake_mac_run),
            ):
                mplat = MacOSPlatform(
                    Config(),
                    env={"HOME": str(root), "TMUX": "/tmp/tmux-501/default,10,0", "TMUX_PANE": "%9"},
                    cache_dir=root / "cache",
                )
                origin_w = mplat.get_foreground_window()
                cur_osa_title[0] = '0:0.0 "⠸ repo"'
                p_out = mplat.deliver_text("测试", seq_id=1, origin_window=origin_w)
            self.assertTrue(p_out.pasted)
            self.assertEqual(p_out.reason, "tmux")
            self.assertTrue(seen_tmux_envs)
            for t_env in seen_tmux_envs:
                self.assertNotIn("TMUX_PANE", t_env)

            # Foreground `agent-voice-input daemon` also sanitizes TMUX / TMUX_PANE via _sanitize_daemon_tmux_env
            captured_fg_daemon_env: dict[str, str] = {}

            class _DummyVoiceDaemon:
                def __init__(self, _cfg: object, *, env: dict[str, str]) -> None:
                    captured_fg_daemon_env.update(env)

                def serve_forever(self) -> None:
                    return None

            cfg_toml = root / "d.toml"
            cfg_toml.write_text('[refiner]\nprovider = "off"\n', encoding="utf-8")
            with (
                mock.patch(
                    "agent_voice_input.config.load_config",
                    return_value=(
                        Config(),
                        {
                            "HOME": str(root),
                            "TMUX": "/custom/sock/fg,888,0",
                            "TMUX_PANE": "%4",
                        },
                    ),
                ),
                mock.patch("agent_voice_input.daemon.VoiceInputDaemon", _DummyVoiceDaemon),
            ):
                rc_fg = cli_mod.main(["daemon", "--config", str(cfg_toml)])
            self.assertEqual(rc_fg, 0)
            self.assertNotIn("TMUX", captured_fg_daemon_env)
            self.assertNotIn("TMUX_PANE", captured_fg_daemon_env)
            self.assertEqual(
                captured_fg_daemon_env.get("AGENT_VOICE_INPUT_TMUX_HINT"),
                "/custom/sock/fg,888,0",
            )

    def test_darwin_lsof_unification_headless_filters_stale_claude_fallthrough_and_socket_quota(self) -> None:
        import socket as sock_mod
        from agent_voice_input._tmux import _candidate_default_tmux_sockets
        from agent_voice_input.session_ctx import (
            _classify_interactive_cli,
            _find_storage_session_for_cli,
            _is_headless_cli_invocation,
            _query_pids_open_files_and_cwds,
        )

        # 1. [P1-4] Desktop background headless CLI invocations MUST be filtered out
        for cli_k, hl_argv in (
            ("codex", ["codex", "-c", 'forced_login_method="chatgpt"', "exec-server", "--concurrent-requests", "32"]),
            ("agy", ["agy", "acp", "--agent", "llm-chat-session"]),
            ("agy", ["agy", "--agent", "llm-chat-session", "acp"]),
            ("claude", ["claude", "mcp", "serve"]),
            ("claude", ["claude", "--settings", "/tmp/s.json", "mcp", "serve"]),
        ):
            self.assertTrue(_is_headless_cli_invocation(cli_k, hl_argv), f"expected headless for {hl_argv!r}")
            self.assertIsNone(_classify_interactive_cli(hl_argv[0], hl_argv))

        # Interactive sessions (including --agent mcp / -c / -i) MUST remain interactive
        for cli_k, ok_argv in (
            ("codex", ["codex", "-c", 'model="gpt-5"']),
            ("agy", ["agy", "--agent", "mcp"]),
            ("agy", ["agy", "-i", "explain"]),
            ("claude", ["claude", "--agent", "mcp"]),
            ("claude", ["claude", "-c"]),
        ):
            self.assertFalse(_is_headless_cli_invocation(cli_k, ok_argv), f"expected interactive for {ok_argv!r}")
            self.assertEqual(_classify_interactive_cli(ok_argv[0], ok_argv), cli_k)

        # 2. [P2-1] _candidate_default_tmux_sockets skips missing default & non-socket dummy files before quota (8)
        short_base = Path(tempfile.mkdtemp(prefix="avi_q_", dir="/tmp"))
        srv_sock = sock_mod.socket(sock_mod.AF_UNIX, sock_mod.SOCK_STREAM)
        try:
            uid_dir = short_base / f"tmux-{os.getuid()}"
            uid_dir.mkdir(mode=0o700)
            for idx in range(10):
                (uid_dir / f"a_dummy_{idx:02d}").write_text("stale", encoding="utf-8")
            live_sock = uid_dir / "z_live_named"
            srv_sock.bind(str(live_sock))
            found_socks = _candidate_default_tmux_sockets({"TMUX_TMPDIR": str(short_base)})
            self.assertEqual(found_socks, [live_sock])
        finally:
            srv_sock.close()
            shutil.rmtree(short_base, ignore_errors=True)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            rt = root / "rt"
            proj_tmux = home / "repos" / "tmux-svc"
            proj_outer_dead = home / "repos" / "outer-dead"
            proj_other_live = home / "repos" / "other-live"
            proj_cd_target = home / "repos" / "cd-target"
            for d in (rt, proj_tmux, proj_outer_dead, proj_other_live, proj_cd_target):
                d.mkdir(parents=True)

            now = time.time()
            cl_tmux_sid = "1111aaaa-2222-4333-8444-555555550001"
            cl_dead_sid = "2222bbbb-3333-4444-8555-666666660002"
            cl_other_sid = "3333cccc-4444-4555-8666-777777770003"
            _write_claude_jsonl(home, cl_tmux_sid, str(proj_tmux), "tmux pane active claude", now - 5)
            _write_claude_jsonl(home, cl_dead_sid, str(proj_outer_dead), "dead outer claude", now)
            _write_claude_jsonl(home, cl_other_sid, str(proj_other_live), "other live non-tmux claude", now - 60)

            cx_cd_sid = "4444dddd-5555-4666-8777-888888880004"
            cx_cd_jf = _write_codex_rollout(home, cx_cd_sid, str(proj_cd_target), "codex -C cd-target", now - 30)

            agy_cid = "5555eeee-6666-4777-8888-999999990005"
            lock_dir = home / ".gemini" / "antigravity-cli" / "presence"
            lock_dir.mkdir(parents=True)
            agy_lock = lock_dir / f"{agy_cid}.lock"
            agy_lock.write_text("", encoding="utf-8")
            os.utime(agy_lock, (now - 45, now - 45))

            snap = TmuxServerSnapshot(
                clients=(
                    TmuxClientInfo(
                        session_id="$0",
                        window_id="@0",
                        pane_id="%0",
                        activity=int(now),
                        client_tty="/dev/ttys002",
                        client_pid=4100,
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
                        current_path=str(proj_tmux),
                        pane_title="zsh",
                    ),
                ),
            )

            # 3. [P1-3a + P2-3] Unified lsof flags (-b -w -d cwd,0-999) + codex -C /other_repo FD priority when cands==0
            seen_lsof_cmds: list[list[str]] = []

            def fake_lsof_and_ps(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[bytes]:
                exe = os.path.basename(cmd[0])
                if exe == "lsof":
                    seen_lsof_cmds.append(list(cmd))
                    pid_arg = cmd[cmd.index("-p") + 1] if "-p" in cmd else ""
                    if pid_arg == "7777":
                        out = f"p7777\nfcwd\nn{proj_tmux}\nf15\nn{cx_cd_jf}\n"
                        return subprocess.CompletedProcess(cmd, 0, out.encode("utf-8"), b"")
                    if pid_arg == "5150":
                        return subprocess.CompletedProcess(cmd, 0, f"p5150\nfcwd\nn{proj_tmux}\n".encode("utf-8"), b"")
                    if "8101" in pid_arg:
                        out = (
                            f"p8101\nfcwd\nn{proj_other_live}\n"
                            f"p8102\nfcwd\nn{proj_cd_target}\nf12\nn{cx_cd_jf}\n"
                            f"p8103\nfcwd\nn{proj_other_live}\nf9\nn{agy_lock}\n"
                        )
                        return subprocess.CompletedProcess(cmd, 0, out.encode("utf-8"), b"")
                    return subprocess.CompletedProcess(cmd, 1, b"", b"")
                if exe == "ps" and "-t" in cmd:
                    t_arg = cmd[cmd.index("-t") + 1]
                    if t_arg == "ttys020":
                        return subprocess.CompletedProcess(
                            cmd, 0, b"5100 4099 Ss ttys020 -zsh\n5150 5100 S+ ttys020 claude\n", b""
                        )
                    return subprocess.CompletedProcess(cmd, 1, b"", b"")
                if exe == "ps" and "-u" in cmd:
                    ps_u = (
                        "5150 5100 S+ ttys020 claude\n"
                        "8099 1 Ss ?? claude\n"
                        "8100 1 S ttys099 codex -c forced_login_method=\"chatgpt\" exec-server\n"
                        "8101 8000 S+ ttys030 claude\n"
                        "8102 8001 S+ ttys031 codex\n"
                        "8103 8002 S+ ttys032 agy\n"
                        "8104 8003 S+ ttys033 agy acp --agent llm-chat-session\n"
                    )
                    return subprocess.CompletedProcess(cmd, 0, ps_u.encode("utf-8"), b"")
                return subprocess.CompletedProcess(cmd, 1, b"", b"")

            with mock.patch("agent_voice_input.session_ctx.subprocess.run", side_effect=fake_lsof_and_ps):
                files_map, cwd_map = _query_pids_open_files_and_cwds([7777], None)
                self.assertEqual(
                    seen_lsof_cmds[-1][:8],
                    ["lsof", "-b", "-n", "-P", "-a", "-w", "-d", "cwd,0-999"],
                )
                self.assertEqual(cwd_map.get(7777), str(proj_tmux))
                self.assertIn(str(cx_cd_jf), files_map.get(7777, []))

                # Codex -C /cd-target running in shell CWD proj_tmux (where _collect_codex_storage_matches == [])
                hit_cd = _find_storage_session_for_cli(
                    "codex",
                    home=home,
                    proc_root=None,
                    eff_cwd=str(proj_tmux),
                    window_title="",
                    agent_pid=7777,
                    matched_by="tmux_proc_scan",
                )
                assert hit_cd is not None
                self.assertEqual(hit_cd.session_id, cx_cd_sid)

                # Offline mock test of _scan_darwin_live_non_tmux_sessions: excludes tmux ttys020 & headless workers
                orig_is_dir = Path.is_dir
                with mock.patch.object(
                    Path,
                    "is_dir",
                    autospec=True,
                    side_effect=lambda p: False if p == Path("/proc") else orig_is_dir(p),
                ):
                    darwin_tracker = SessionTracker(
                        runtime_dir=rt,
                        home_dir=home,
                        proc_root=Path("/proc"),
                        tmux_probe_fn=lambda: snap,
                    )
                    non_tmux_hits = {
                        m.session_key: m
                        for m in darwin_tracker._scan_darwin_live_non_tmux_sessions(snap)
                    }
                    self.assertEqual(
                        set(non_tmux_hits.keys()),
                        {f"claude:{cl_other_sid}", f"codex:{cx_cd_sid}", f"agy:{agy_cid}"},
                    )

                    # 4. [P1-2] Stale outer dead claude UUID (proj_outer_dead) with window_tty="":
                    #    Even though claude runs inside tmux (ttys020 -> proj_tmux) AND outside tmux
                    #    in a DIFFERENT repo (ttys030 -> proj_other_live), neither matches proj_outer_dead,
                    #    so resolve_session MUST NOT resurrect cl_dead_sid — it falls through to active tmux pane!
                    m_fallthrough = darwin_tracker.resolve_session(
                        f"claude · outer-dead · {cl_dead_sid}",
                        window_tty="",
                        window_pid=0,
                    )
                    assert m_fallthrough is not None
                    self.assertEqual(
                        (m_fallthrough.cli, m_fallthrough.session_id, m_fallthrough.matched_by),
                        ("claude", cl_tmux_sid, "tmux_proc_scan"),
                    )

            # 5. [P1-3b] When batched lsof fails/times out on 12 live processes, NO 12x serial lsof cascade occurs!
            timeout_lsof_calls: list[list[str]] = []

            def fake_lsof_timeout(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[bytes]:
                exe = os.path.basename(cmd[0])
                if exe == "ps" and "-u" in cmd:
                    rows_12 = "".join(f"{9100 + i} 9000 S+ ttys0{40 + i} claude\n" for i in range(12))
                    return subprocess.CompletedProcess(cmd, 0, rows_12.encode("utf-8"), b"")
                if exe == "lsof":
                    timeout_lsof_calls.append(list(cmd))
                    raise subprocess.TimeoutExpired(cmd, 1.5)
                return subprocess.CompletedProcess(cmd, 1, b"", b"")

            with mock.patch("agent_voice_input.session_ctx.subprocess.run", side_effect=fake_lsof_timeout):
                t_cascade = SessionTracker(
                    runtime_dir=rt,
                    home_dir=home,
                    proc_root=root / "noproc",
                    tmux_probe_fn=lambda: snap,
                )
                self.assertEqual(t_cascade._scan_darwin_live_non_tmux_sessions(snap), [])
            self.assertEqual(len(timeout_lsof_calls), 1, "batched lsof timeout must not cascade into per-PID lsof")

    def test_darwin_window_pty_tree_single_and_multi_tab_gui_terminal_scoping(self) -> None:
        # 1. collect_darwin_window_pty_tree stops tab-TTY discovery at first controlling TTY per branch
        #    (ignoring nested agent tool sub-PTYs) while still traversing intermediate non-TTY helpers.
        sample_rows = [
            # Alacritty (9000, no tty) -> login (9001, ttys010) -> -zsh (9002, ttys010)
            # -> agy (9003, ttys010) -> nested PTY tool runner (9004, ttys099!)
            (9000, 1, "Ss", "", ["/Applications/Alacritty.app/Contents/MacOS/alacritty"]),
            (9001, 9000, "Ss", "/dev/ttys010", ["/usr/bin/login", "-fp", "dev"]),
            (9002, 9001, "S", "/dev/ttys010", ["-zsh"]),
            (9003, 9002, "S+", "/dev/ttys010", ["agy"]),
            (9004, 9003, "Ss+", "/dev/ttys099", ["/bin/zsh", "-c", "pytest"]),
            # iTerm2 (8000, no tty) -> iTermServer (8001, no tty) -> 2 tabs (ttys004, ttys005)
            (8000, 1, "Ss", "", ["/Applications/iTerm.app/Contents/MacOS/iTerm2"]),
            (8001, 8000, "S", "", ["iTermServer-3.5"]),
            (8010, 8001, "Ss", "/dev/ttys004", ["-zsh"]),
            (8011, 8010, "S+", "/dev/ttys004", ["claude"]),
            (8020, 8001, "Ss+", "/dev/ttys005", ["-zsh"]),
        ]
        alac_ttys, alac_desc = collect_darwin_window_pty_tree(sample_rows, 9000)
        self.assertEqual(alac_ttys, {"/dev/ttys010"})
        self.assertEqual(alac_desc, {9001, 9002, 9003, 9004})

        iterm_ttys, iterm_desc = collect_darwin_window_pty_tree(sample_rows, 8000)
        self.assertEqual(iterm_ttys, {"/dev/ttys004", "/dev/ttys005"})
        self.assertEqual(iterm_desc, {8001, 8010, 8011, 8020})
        self.assertEqual(collect_darwin_window_pty_tree(sample_rows, 1), (set(), set()))

        # 2. SessionTracker.resolve_session on real Darwin (Path('/proc').is_dir() == False)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            rt = root / "rt"
            proj_ghostty_tmux = home / "repos" / "ghostty-tmux"
            proj_ghostty_plain = home / "repos" / "ghostty-plain"
            proj_alac = home / "repos" / "alac-app"
            proj_iterm = home / "repos" / "iterm-svc"
            for d in (rt, proj_ghostty_tmux, proj_ghostty_plain, proj_alac, proj_iterm):
                d.mkdir(parents=True)

            now = time.time()
            sid_g_tmux = "11110000-1111-4111-8111-000000000001"
            sid_g_plain = "22220000-2222-4222-8222-000000000002"
            sid_alac = "33330000-3333-4333-8333-000000000003"
            sid_iterm = "44440000-4444-4444-8444-000000000004"
            sid_agy = "55550000-5555-4555-8555-000000000005"
            _write_claude_jsonl(home, sid_g_tmux, str(proj_ghostty_tmux), "Ghostty tmux 会话", now)
            f_g_plain = _write_codex_rollout(home, sid_g_plain, str(proj_ghostty_plain), "Ghostty 普通会话", now)
            f_alac = _write_codex_rollout(home, sid_alac, str(proj_alac), "Alacritty 单标签会话", now - 30)
            f_iterm = _write_claude_jsonl(home, sid_iterm, str(proj_iterm), "iTerm2 多标签会话", now - 60)
            agy_lock = home / ".gemini" / "antigravity-cli" / "presence" / f"{sid_agy}.lock"
            agy_tx = home / ".gemini" / "antigravity-cli" / "brain" / sid_agy / ".system_generated" / "logs" / "transcript.jsonl"
            agy_lock.parent.mkdir(parents=True, exist_ok=True)
            agy_tx.parent.mkdir(parents=True, exist_ok=True)
            agy_lock.write_text("", encoding="utf-8")
            agy_tx.write_text('{"type":"USER_INPUT","content":"Ghostty agy 会话"}\n', encoding="utf-8")

            snap_box = [
                TmuxServerSnapshot(
                    clients=(
                        TmuxClientInfo(
                            session_id="$0",
                            window_id="@0",
                            pane_id="%0",
                            activity=now,
                            client_tty="/dev/ttys002",
                            client_pid=6101,
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
                            pane_pid=6200,
                            pane_tty="/dev/ttys020",
                            socket_path="/private/tmp/tmux-501/default",
                            current_path=str(proj_ghostty_tmux),
                            pane_title="zsh",
                        ),
                    ),
                )
            ]

            iterm_has_claude = [True]
            ps_calls = [0]

            def fake_darwin_subproc(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[bytes]:
                exe = os.path.basename(cmd[0])
                if exe == "ps":
                    ps_calls[0] += 1
                    if "-p" in cmd:
                        pid_s = cmd[cmd.index("-p") + 1]
                        p_map = {
                            "9000": "9000 1 Ss ?? /Applications/Alacritty.app/Contents/MacOS/alacritty\n",
                            "8000": "8000 1 Ss ?? /Applications/iTerm.app/Contents/MacOS/iTerm2\n",
                        }
                        out = p_map.get(pid_s, "")
                        return subprocess.CompletedProcess(cmd, 0 if out else 1, out.encode("utf-8"), b"")
                    if "-t" in cmd:
                        t_arg = cmd[cmd.index("-t") + 1]
                        if t_arg == "ttys010":
                            out = (
                                "9001 9000 Ss ttys010 /usr/bin/login\n"
                                "9002 9001 S ttys010 -zsh\n"
                                "9003 9002 S+ ttys010 /opt/homebrew/bin/codex\n"
                            )
                            return subprocess.CompletedProcess(cmd, 0, out.encode("utf-8"), b"")
                        if t_arg == "ttys020":
                            out = "6200 6199 Ss ttys020 -zsh\n6201 6200 S+ ttys020 claude\n"
                            return subprocess.CompletedProcess(cmd, 0, out.encode("utf-8"), b"")
                        return subprocess.CompletedProcess(cmd, 1, b"", b"")
                    if "-u" in cmd:
                        lines = [
                            # Ghostty (676) with tmux client (ttys002) + newer plain codex (ttys003)
                            "676 1 Ss ?? /Applications/Ghostty.app/Contents/MacOS/ghostty",
                            "6100 676 Ss ttys002 -zsh",
                            "6101 6100 S+ ttys002 tmux attach",
                            "6200 6199 Ss ttys020 -zsh",
                            "6201 6200 S+ ttys020 claude",
                            "6300 676 Ss ttys003 -zsh",
                            "6301 6300 S+ ttys003 /opt/homebrew/bin/codex",
                            "6400 676 Ss ttys006 -zsh",
                            "6401 6400 S+ ttys006 /opt/homebrew/bin/agy",
                            # Alacritty (9000) single tab ttys010 + nested tool sub-PTY ttys099
                            "9000 1 Ss ?? /Applications/Alacritty.app/Contents/MacOS/alacritty",
                            "9001 9000 Ss ttys010 /usr/bin/login",
                            "9002 9001 S ttys010 -zsh",
                            "9003 9002 S+ ttys010 /opt/homebrew/bin/codex",
                            "9004 9003 Ss+ ttys099 /bin/zsh -c pytest",
                            # iTerm2 (8000) two tabs ttys004 & ttys005
                            "8000 1 Ss ?? /Applications/iTerm.app/Contents/MacOS/iTerm2",
                            "8001 8000 S ?? iTermServer",
                            "8010 8001 Ss ttys004 -zsh",
                            "8020 8001 Ss+ ttys005 -zsh",
                        ]
                        if iterm_has_claude[0]:
                            lines.append("8011 8010 S+ ttys004 claude")
                        return subprocess.CompletedProcess(cmd, 0, ("\n".join(lines) + "\n").encode("utf-8"), b"")
                if exe == "lsof":
                    pid_csv = cmd[cmd.index("-p") + 1] if "-p" in cmd else ""
                    pids = {int(x) for x in pid_csv.split(",") if x.isdigit()}
                    chunks: list[str] = []
                    if 9003 in pids:
                        chunks.append(f"p9003\nfcwd\nn{proj_alac}\nf12\nn{f_alac}\n")
                    if 6301 in pids:
                        chunks.append(f"p6301\nfcwd\nn{proj_ghostty_plain}\nf12\nn{f_g_plain}\n")
                    if 6401 in pids:
                        chunks.append(f"p6401\nfcwd\nn{proj_ghostty_plain}\nf15\nn{agy_lock}\n")
                    if 6201 in pids:
                        chunks.append(f"p6201\nfcwd\nn{proj_ghostty_tmux}\n")
                    if 8011 in pids:
                        chunks.append(f"p8011\nfcwd\nn{proj_iterm}\n")
                    out_s = "".join(chunks)
                    return subprocess.CompletedProcess(cmd, 0 if out_s else 1, out_s.encode("utf-8"), b"")
                return subprocess.CompletedProcess(cmd, 1, b"", b"")

            orig_is_dir = Path.is_dir
            with (
                mock.patch("agent_voice_input.session_ctx.subprocess.run", side_effect=fake_darwin_subproc),
                mock.patch.object(
                    Path,
                    "is_dir",
                    autospec=True,
                    side_effect=lambda p: False if p == Path("/proc") else orig_is_dir(p),
                ),
            ):
                tracker = SessionTracker(
                    runtime_dir=rt,
                    home_dir=home,
                    proc_root=Path("/proc"),
                    tmux_probe_fn=lambda: snap_box[0],
                )

                # A. Single-tab Alacritty (window_pid=9000, window_tty='') resolves exact sid_alac via tty_ps
                #    despite nested tool PTY ttys099 and Ghostty's newer tmux/codex sessions!
                m_alac = tracker.resolve_session("Alacritty", window_tty="", window_pid=9000)
                assert m_alac is not None
                self.assertEqual((m_alac.cli, m_alac.session_id, m_alac.matched_by), ("codex", sid_alac, "tty_ps"))

                # B. Multi-tab iTerm2 (window_pid=8000, 2 tabs) scopes strictly to iTerm2's subtree,
                #    excluding Ghostty's tmux client & Ghostty's codex, resolving iTerm2's claude!
                m_iterm = tracker.resolve_session("zsh", window_tty="", window_pid=8000)
                assert m_iterm is not None
                self.assertEqual((m_iterm.cli, m_iterm.session_id, m_iterm.matched_by), ("claude", sid_iterm, "tty_ps"))

                # C. Multi-tab iTerm2 when BOTH tabs are empty shells -> returns None (no Ghostty or disk leak!)
                iterm_has_claude[0] = False
                self.assertIsNone(tracker.resolve_session("zsh", window_tty="", window_pid=8000))
                _ = f_iterm

                # D. Zero-subprocess O(1) registry fast path preserved when [#tag] is present & no tmux client exists
                orig_tmux_snap = snap_box[0]
                write_session_registry_entry(
                    rt,
                    cli="codex",
                    session_id=sid_alac,
                    jsonl_path=str(f_alac),
                    cwd=str(proj_alac),
                    tty="/dev/ttys010",
                )
                snap_box[0] = TmuxServerSnapshot()
                ps_calls[0] = 0
                m_fast = tracker.resolve_session(
                    f"codex [#{sid_alac[:8]}]", window_tty="", window_pid=9000
                )
                assert m_fast is not None
                self.assertEqual((m_fast.session_id, m_fast.matched_by), (sid_alac, "registry"))
                self.assertEqual(ps_calls[0], 0, "O(1) registry hit without tmux clients must spawn zero ps calls")

                # E. Stale hooked non-tmux [#tag] falls through when window_tty is an attached tmux client!
                snap_box[0] = orig_tmux_snap
                m_stale_reg = tracker.resolve_session(
                    f"codex [#{sid_alac[:8]}]", window_tty="/dev/ttys002", window_pid=676
                )
                assert m_stale_reg is not None
                self.assertEqual((m_stale_reg.cli, m_stale_reg.session_id), ("claude", sid_g_tmux))

                # F. Unhooked agy [#tag] in title scopes strictly to window_pid's open presence/<cid>.lock!
                import fcntl

                with agy_lock.open("r+", encoding="utf-8") as lock_f:
                    fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.assertIsNone(
                        tracker.resolve_session(f"agy [#{sid_agy[:8]}]", window_tty="", window_pid=8000)
                    )
                    m_agy_g = tracker.resolve_session(f"agy [#{sid_agy[:8]}]", window_tty="", window_pid=676)
                    assert m_agy_g is not None
                    self.assertEqual((m_agy_g.cli, m_agy_g.session_id), ("agy", sid_agy))


if __name__ == "__main__":
    unittest.main()
