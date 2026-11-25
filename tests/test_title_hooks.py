"""Unit tests for window title hooks (codex/claude/agy), integrations, and SessionTracker O(1) linkage."""
from __future__ import annotations

import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import stat
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agent_voice_input import session_title as VST  # noqa: E402
from agent_voice_input.config import parse_toml  # noqa: E402
from agent_voice_input.integrate import AGY_HOOK_COMMAND, CLAUDE_HOOK_COMMAND  # noqa: E402
from agent_voice_input.session_ctx import SessionTracker  # noqa: E402


def _read_pty_nonblocking(master_fd: int, sink: list[bytes]) -> bool:
    """Drain currently buffered PTY bytes into ``sink``; return True on slave EOF/hangup."""
    while True:
        try:
            data = os.read(master_fd, 4096)
        except BlockingIOError:
            return False
        except InterruptedError:
            continue
        except OSError as exc:
            if exc.errno in (errno.EIO, errno.ENXIO):
                return True
            raise
        if not data:
            return True
        sink.append(data)


def _drain_pty_master(master_fd: int) -> bytes:
    os.set_blocking(master_fd, False)
    chunks: list[bytes] = []
    while True:
        r, _, _ = select.select([master_fd], [], [], 0.05)
        if not r or _read_pty_nonblocking(master_fd, chunks):
            break
    return b"".join(chunks)


def _write_fake_proc(
    proc_root: Path,
    pid: int,
    *,
    comm: str,
    ppid: int,
    psid: int,
    tty_nr: int = 0,
    cmdline: list[str] | tuple[str, ...] = (),
    cwd: str = "",
    fds: dict[str, str] | None = None,
) -> Path:
    pdir = proc_root / str(pid)
    (pdir / "fd").mkdir(parents=True, exist_ok=True)
    # Linux /proc/<pid>/stat format: pid (comm) state ppid pgrp session tty_nr ...
    stat_line = f"{pid} ({comm}) S {ppid} {psid} {psid} {tty_nr} 0 0 0 0 0 0 0 0 0 0 20 0 1 0 100\n"
    (pdir / "stat").write_text(stat_line, encoding="utf-8")
    (pdir / "comm").write_text(f"{comm}\n", encoding="utf-8")
    if cmdline:
        (pdir / "cmdline").write_bytes(b"\x00".join(a.encode("utf-8") for a in cmdline) + b"\x00")
    if cwd:
        os.symlink(cwd, pdir / "cwd")
    for fd_num, target in (fds or {}).items():
        os.symlink(target, pdir / "fd" / str(fd_num))
    return pdir


class TitleHooksAndLinkageTests(unittest.TestCase):
    def test_codex_integration_config_and_uuid_resolution(self) -> None:
        cfg_text = (REPO_ROOT / "integrations" / "codex" / "config.toml").read_text(encoding="utf-8")
        self.assertIn(
            'terminal_title = ["spinner", "project-name", "thread-title", "thread-id"]',
            cfg_text,
        )
        parsed = parse_toml(cfg_text)
        self.assertEqual(
            parsed["tui"]["terminal_title"],
            ["spinner", "project-name", "thread-title", "thread-id"],
        )

        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            rt = Path(td) / "rt"
            uid = "0196c8e2-1234-7abc-9def-0123456789ab"
            roll_dir = home / ".codex" / "sessions" / "2026" / "11" / "13"
            roll_dir.mkdir(parents=True)
            roll_file = roll_dir / f"rollout-2026-11-13T22-00-00-{uid}.jsonl"
            roll_file.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": uid, "source": "cli", "cwd": "/tmp/proj"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            tracker = SessionTracker(home_dir=home, runtime_dir=rt, proc_root=Path(td) / "no_proc")
            win_title = f"⠋ agent-voice-input · Wire session titles · {uid}"
            match = tracker.resolve_session(win_title)
            self.assertIsNotNone(match)
            assert match is not None
            self.assertEqual(match.cli, "codex")
            self.assertEqual(match.session_id, uid)
            self.assertEqual(match.matched_by, "title_uuid")
            self.assertEqual(match.jsonl_path, str(roll_file))

    def test_integration_manifests_valid_and_executable(self) -> None:
        claude_cfg = json.loads(
            (REPO_ROOT / "integrations" / "claude" / "settings.hooks.json").read_text(encoding="utf-8")
        )
        hooks = claude_cfg["hooks"]
        for ev in ("SessionStart", "UserPromptSubmit"):
            self.assertIn(ev, hooks)
            self.assertEqual(
                hooks[ev][0]["hooks"][0]["command"],
                CLAUDE_HOOK_COMMAND,
            )

        agy_hooks = json.loads(
            (REPO_ROOT / "integrations" / "gemini" / "hooks.json").read_text(encoding="utf-8")
        )
        vst_hook = agy_hooks["voice-session-title"]
        for ev in ("SessionStart", "PreInvocation"):
            self.assertIsInstance(vst_hook[ev], list)
            self.assertNotIn("matcher", vst_hook[ev][0])
            self.assertEqual(
                vst_hook[ev][0]["command"],
                AGY_HOOK_COMMAND,
            )

        voice_title_sh = REPO_ROOT / "integrations" / "claude" / "voice-title.sh"
        self.assertTrue(voice_title_sh.is_file())
        self.assertTrue(os.access(voice_title_sh, os.X_OK))

    def test_agy_double_presence_lock_gate_pty_bypass_and_tracker_linkage(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            home = base / "home"
            rt = base / "rt"
            proc_root = base / "proc"
            presence_dir = home / ".gemini" / "antigravity-cli" / "presence"
            presence_dir.mkdir(parents=True)
            agy_stdout_log = base / "cli-redirected.log"
            agy_stdout_log.write_bytes(b"")

            root_cid = "0a23bc89-8e44-4dda-ac58-d11aba8434e6"
            sub_cid = "07e22ada-1111-4222-8333-444455556666"
            stale_cid = "deadbeef-9999-4888-a777-666655554444"

            root_lock = presence_dir / f"{root_cid}.lock"
            root_lock.write_text("", encoding="utf-8")
            stale_lock = presence_dir / f"{stale_cid}.lock"
            stale_lock.write_text("", encoding="utf-8")
            # sub_cid intentionally has NO presence lock file, matching real agy subagents.

            master_fd, slave_fd = pty.openpty()
            try:
                slave_name = os.ttyname(slave_fd)
                slave_rdev = os.stat(slave_fd).st_rdev
                proj_dir = home / "repos" / "demo-app"
                proj_dir.mkdir(parents=True)

                # Ancestor agy (PID 1000) holds root_lock on fd 7, fd 1 redirected to cli-redirected.log!
                _write_fake_proc(
                    proc_root,
                    1000,
                    comm="agy",
                    ppid=500,
                    psid=1000,
                    tty_nr=slave_rdev,
                    cmdline=["/usr/local/bin/agy"],
                    cwd=str(proj_dir),
                    fds={"0": slave_name, "1": str(agy_stdout_log), "7": str(root_lock)},
                )
                # Hook child (PID 1002 -> sh 1001 -> agy 1000)
                _write_fake_proc(proc_root, 1001, comm="sh", ppid=1000, psid=1000)
                _write_fake_proc(proc_root, 1002, comm="python3", ppid=1001, psid=1000)

                # 1. Background subagent inside the SAME agy process MUST be blocked
                ok_sub = VST.update_agy_session(
                    {"conversationId": sub_cid, "cwd": str(proj_dir)},
                    home=home,
                    proc_root=proc_root,
                    runtime_dir=rt,
                    start_pid=1002,
                )
                self.assertFalse(ok_sub)
                self.assertEqual(_drain_pty_master(master_fd), b"")

                # 2. Stale lock file on disk NOT held by agy fd MUST be blocked
                ok_stale = VST.update_agy_session(
                    {"conversationId": stale_cid, "cwd": str(proj_dir)},
                    home=home,
                    proc_root=proc_root,
                    runtime_dir=rt,
                    start_pid=1002,
                )
                self.assertFalse(ok_stale)
                self.assertEqual(_drain_pty_master(master_fd), b"")

                # 2b. Subagent transcript content (e.g. [agy-run]) MUST be blocked even if lock existed
                delegated_cid = "feedface-1234-4567-89ab-cdef01234567"
                delegated_lock = presence_dir / f"{delegated_cid}.lock"
                delegated_lock.write_text("", encoding="utf-8")
                delegated_log = (
                    home
                    / ".gemini"
                    / "antigravity-cli"
                    / "brain"
                    / delegated_cid
                    / ".system_generated"
                    / "logs"
                    / "transcript.jsonl"
                )
                delegated_log.parent.mkdir(parents=True)
                delegated_log.write_text(
                    json.dumps(
                        {
                            "source": "USER_EXPLICIT",
                            "type": "USER_INPUT",
                            "content": "<USER_REQUEST>[agy-run] bounded task</USER_REQUEST>",
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                _write_fake_proc(
                    proc_root,
                    2001,
                    comm="agy",
                    ppid=500,
                    psid=2001,
                    tty_nr=slave_rdev,
                    cmdline=["/usr/local/bin/agy"],
                    cwd=str(proj_dir),
                    fds={"7": str(delegated_lock)},
                )
                self.assertFalse(
                    VST.update_agy_session(
                        {"conversationId": delegated_cid, "cwd": str(proj_dir)},
                        home=home,
                        proc_root=proc_root,
                        runtime_dir=rt,
                        start_pid=2001,
                    )
                )

                # 3. Headless agy -p MUST be blocked
                _write_fake_proc(
                    proc_root,
                    2000,
                    comm="agy",
                    ppid=500,
                    psid=2000,
                    tty_nr=slave_rdev,
                    cmdline=["agy", "-p", "headless task"],
                    cwd=str(proj_dir),
                    fds={"7": str(root_lock)},
                )
                self.assertFalse(
                    VST.update_agy_session(
                        {"conversationId": root_cid, "cwd": str(proj_dir)},
                        home=home,
                        proc_root=proc_root,
                        runtime_dir=rt,
                        start_pid=2000,
                    )
                )
                self.assertEqual(_drain_pty_master(master_fd), b"")

                # 4. Active foreground root session succeeds, writes OSC 2 to pty, leaves fd/1 log untouched!
                ok_root = VST.update_agy_session(
                    {"conversationId": root_cid, "cwd": str(proj_dir)},
                    home=home,
                    proc_root=proc_root,
                    runtime_dir=rt,
                    start_pid=1002,
                )
                self.assertTrue(ok_root)
                pty_out = _drain_pty_master(master_fd).decode("utf-8")
                self.assertEqual(pty_out, "\033]2;agy · ~/repos/demo-app [#0a23bc89]\007")
                self.assertEqual(agy_stdout_log.read_bytes(), b"")

                reg_file = rt / "sessions" / "agy-0a23bc89.json"
                self.assertTrue(reg_file.is_file())
                self.assertEqual(stat.S_IMODE(os.stat(reg_file).st_mode), 0o600)

                # Verify SessionTracker O(1) resolution against the exact emitted window title
                tracker = SessionTracker(home_dir=home, runtime_dir=rt, proc_root=proc_root)
                match = tracker.resolve_session("agy · ~/repos/demo-app [#0a23bc89]")
                self.assertIsNotNone(match)
                assert match is not None
                self.assertEqual(match.cli, "agy")
                self.assertEqual(match.session_id, root_cid)
                self.assertEqual(match.matched_by, "registry")
            finally:
                os.close(master_fd)
                os.close(slave_fd)

    def test_agy_nonproc_flock_gate_and_cli_hook_stdout_json_contract(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            home = base / "home"
            rt = base / "rt"
            no_proc = base / "nonexistent_proc"
            presence_dir = home / ".gemini" / "antigravity-cli" / "presence"
            presence_dir.mkdir(parents=True)

            cid = "11223344-5566-4777-8899-aabbccddeeff"
            lock_file = presence_dir / f"{cid}.lock"
            lock_file.write_text("", encoding="utf-8")

            # Unlocked stale lock in non-/proc mode -> rejected
            self.assertFalse(
                VST.update_agy_session(
                    {"conversationId": cid, "cwd": str(home)},
                    home=home,
                    proc_root=no_proc,
                    runtime_dir=rt,
                    start_pid=1,
                )
            )

            # Actively LOCK_EX held in non-/proc mode -> accepted & registered
            hold_fd = os.open(str(lock_file), os.O_RDWR)
            try:
                fcntl.flock(hold_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertTrue(
                    VST.update_agy_session(
                        {"conversationId": cid, "cwd": str(home)},
                        home=home,
                        proc_root=no_proc,
                        runtime_dir=rt,
                        start_pid=1,
                    )
                )
                self.assertTrue((rt / "sessions" / "agy-11223344.json").is_file())
            finally:
                os.close(hold_fd)

            # CLI subprocess execution: malformed JSON, empty input, or subagent payload ALWAYS emits '{}'
            bin_cli = str(REPO_ROOT / "bin" / "agent-voice-input")
            env = {
                **os.environ,
                "HOME": str(home),
                "XDG_RUNTIME_DIR": str(base),
                "PYTHONPATH": str(SRC_ROOT),
            }
            for cmd in (
                [bin_cli, "session-hook", "--cli", "agy"],
                [sys.executable, "-m", "agent_voice_input.session_title"],
            ):
                for raw_in in (
                    "",
                    "{bad json",
                    json.dumps({"conversationId": "99999999-0000-4000-8000-000000000000"}),
                ):
                    proc = subprocess.run(
                        cmd,
                        input=raw_in.encode("utf-8"),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env=env,
                        timeout=5,
                        check=False,
                    )
                    self.assertEqual(proc.returncode, 0)
                    self.assertEqual(json.loads(proc.stdout.decode("utf-8")), {})
                    self.assertNotIn(b"\x1b]2;", proc.stdout)

            # write_osc2 strictly refuses non-character files (e.g. redirected log files)
            reg_log = base / "regular.log"
            reg_log.write_bytes(b"")
            self.assertFalse(VST.write_osc2(str(reg_log), "agy · ~ [#11223344]"))
            self.assertEqual(reg_log.read_bytes(), b"")

    def test_claude_hook_wrapper_and_session_hook_e2e_with_pty_and_non_tty_safety(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            home = base / "home"
            xdg = base / "xdg"
            proj = home / "work" / "my-svc"
            proj.mkdir(parents=True)
            xdg.mkdir(parents=True)
            bin_dir = str(REPO_ROOT / "bin")
            env = {
                k: v
                for k, v in os.environ.items()
                if k
                not in (
                    "AGENT_VOICE_INPUT_RUNTIME_DIR",
                    "VOICE_INPUT_RUNTIME_DIR",
                    "SSH_CONNECTION",
                    "SSH_CLIENT",
                    "SSH_TTY",
                    "AGENT_VOICE_INPUT_HOST_TAG",
                )
            }
            env.update(
                {
                    "HOME": str(home),
                    "XDG_RUNTIME_DIR": str(xdg),
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                    "PYTHONPATH": str(SRC_ROOT),
                }
            )

            sid = "a1b2c3d4-5678-4abc-9def-0123456789ab"
            sub_sid = "f0e1d2c3-5678-4abc-9def-0123456789ab"
            hook_sh = str(REPO_ROOT / "integrations" / "claude" / "voice-title.sh")
            bin_cli = str(REPO_ROOT / "bin" / "agent-voice-input")

            def _run_with_fresh_pty(
                cmd: list[str], raw_payload: bytes, timeout_sec: float = 5.0
            ) -> tuple[subprocess.CompletedProcess[bytes], bytes]:
                fds = list(pty.openpty())  # [master_fd, slave_fd]
                proc: subprocess.Popen[bytes] | None = None
                pty_chunks: list[bytes] = []

                def _close_pty(idx: int) -> None:
                    if fds[idx] >= 0:
                        fd, fds[idx] = fds[idx], -1
                        os.close(fd)

                def _kill_and_reap() -> tuple[bytes, bytes]:
                    # Close master_fd first to unblock Darwin XNU ttywait() before SIGKILL.
                    _close_pty(0)
                    assert proc is not None
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except OSError:
                        pass
                    proc.kill()
                    return proc.communicate()

                try:
                    os.set_blocking(fds[0], False)
                    child_slave_fd = fds[1]

                    def _attach_ctty() -> None:
                        os.setsid()
                        fcntl.ioctl(child_slave_fd, termios.TIOCSCTTY, 0)

                    proc = subprocess.Popen(
                        cmd,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env=env,
                        preexec_fn=_attach_ctty,
                        pass_fds=(child_slave_fd,),
                    )
                    _close_pty(1)
                    assert proc.stdin is not None
                    try:
                        proc.stdin.write(raw_payload)
                    except BrokenPipeError:
                        pass
                    finally:
                        try:
                            proc.stdin.close()
                        except BrokenPipeError:
                            pass
                        proc.stdin = None

                    deadline = time.monotonic() + timeout_sec
                    pty_eof = False
                    while proc.poll() is None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            out_b, err_b = _kill_and_reap()
                            raise subprocess.TimeoutExpired(
                                cmd, timeout_sec, output=out_b, stderr=err_b
                            )
                        if not pty_eof:
                            ready, _, _ = select.select([fds[0]], [], [], min(0.05, remaining))
                            if ready:
                                pty_eof = _read_pty_nonblocking(fds[0], pty_chunks)
                        else:
                            time.sleep(min(0.005, remaining))

                    while not pty_eof and fds[0] >= 0:
                        ready, _, _ = select.select([fds[0]], [], [], 0.05)
                        if not ready:
                            break
                        pty_eof = _read_pty_nonblocking(fds[0], pty_chunks)

                    out_b, err_b = proc.communicate(timeout=2)
                    return (
                        subprocess.CompletedProcess(cmd, proc.returncode, out_b, err_b),
                        b"".join(pty_chunks),
                    )
                finally:
                    _close_pty(1)
                    if proc is not None:
                        if proc.poll() is None:
                            _kill_and_reap()
                        for pipe in (proc.stdin, proc.stdout, proc.stderr):
                            if pipe is not None and not pipe.closed:
                                try:
                                    pipe.close()
                                except BrokenPipeError:
                                    pass
                    _close_pty(0)

            # 1. Run integrations/claude/voice-title.sh in an isolated session attached to a dedicated pty
            payload = json.dumps({"session_id": sid, "cwd": str(proj)}).encode("utf-8")
            proc, pty_bytes = _run_with_fresh_pty(["bash", hook_sh], payload)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout, b"")
            self.assertEqual(pty_bytes.decode("utf-8"), "\033]2;claude · ~/work/my-svc [#a1b2c3d4]\007")

            # 2. Subagent payload (agent_id set) MUST NOT emit OSC 2 or create registry
            sub_payload = json.dumps(
                {"session_id": sub_sid, "agent_id": "agent-xyz", "cwd": str(proj)}
            ).encode("utf-8")
            proc_sub, sub_pty_bytes = _run_with_fresh_pty(["bash", hook_sh], sub_payload)
            self.assertEqual(proc_sub.returncode, 0, proc_sub.stderr)
            self.assertEqual(sub_pty_bytes, b"")

            # 3. `agent-voice-input session-hook --cli auto` detects session_id and updates pty cleanly
            auto_in = json.dumps({"session_id": sid, "cwd": str(proj)}).encode("utf-8")
            proc_auto, auto_pty_bytes = _run_with_fresh_pty([bin_cli, "session-hook", "--cli", "auto"], auto_in)
            self.assertEqual(proc_auto.returncode, 0, proc_auto.stderr)
            self.assertEqual(proc_auto.stdout, b"")
            self.assertEqual(
                auto_pty_bytes.decode("utf-8"),
                "\033]2;claude · ~/work/my-svc [#a1b2c3d4]\007",
            )

            # 4. Resolve the registered 0-message Claude session via SessionTracker
            rt_dir = xdg / "agent-voice-input"
            tracker = SessionTracker(home_dir=home, runtime_dir=rt_dir, proc_root=base / "no_proc")
            match = tracker.resolve_session("claude · ~/work/my-svc [#a1b2c3d4]")
            self.assertIsNotNone(match)
            assert match is not None
            self.assertEqual(match.cli, "claude")
            self.assertEqual(match.session_id, sid)
            self.assertEqual(match.matched_by, "registry")

            # 5. Detached non-TTY fd/1 redirected to regular file -> safe no-op on OSC 2, zero escape leak
            out_file = base / "redirected_stdout.txt"
            for cmd, payload_obj in (
                (["bash", hook_sh], {"session_id": sid, "cwd": str(proj)}),
                ([bin_cli, "session-hook", "--cli", "claude"], {"session_id": sid, "cwd": str(proj)}),
                ([bin_cli, "session-hook", "--cli", "agy"], {"conversation_id": sid, "cwd": str(proj)}),
            ):
                r_p, w_p = os.pipe()
                os.write(w_p, json.dumps(payload_obj).encode("utf-8"))
                os.close(w_p)
                try:
                    with open(out_file, "wb") as outf:
                        rc = subprocess.run(
                            cmd,
                            stdin=r_p,
                            stdout=outf,
                            stderr=subprocess.PIPE,
                            env=env,
                            start_new_session=True,
                            timeout=5,
                            check=False,
                        ).returncode
                finally:
                    os.close(r_p)
                self.assertEqual(rc, 0)
                self.assertNotIn(b"\x1b]2;", out_file.read_bytes())

    def test_nonproc_ps_parent_chain_tty_discovery_and_headless_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            home = base / "home"
            rt = base / "rt"
            no_proc = base / "nonexistent_proc"
            proj = home / "repos" / "voice-app"
            presence_dir = home / ".gemini" / "antigravity-cli" / "presence"
            proj.mkdir(parents=True)
            presence_dir.mkdir(parents=True)

            # 1. Real PTY OSC 2 emission across a multi-hop detached ps ancestor chain (non-/proc)
            master_fd, slave_fd = pty.openpty()
            try:
                os.set_blocking(master_fd, False)
                slave_name = os.ttyname(slave_fd)
                slave_short = slave_name[5:] if slave_name.startswith("/dev/") else slave_name

                ps_rows_pty: dict[int, str] = {
                    9010: f"9010 676 Ss {slave_short} login -fp dev\n",
                    9011: f"9011 9010 S {slave_short} -zsh\n",
                    9050: "9050 9011 S+ ?? node /opt/claude/versions/2.1.12 --resume\n",
                    9080: "9080 9050 Ss ?? /bin/sh -c agent-voice-input session-hook --cli claude\n",
                    9081: "9081 9080 S ?? python3 agent-voice-input session-hook --cli claude\n",
                }

                def fake_subproc_pty(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
                    self.assertIs(kwargs.get("stdin"), subprocess.DEVNULL)
                    exe = os.path.basename(cmd[0])
                    if exe == "ps" and "-p" in cmd:
                        pid_q = int(cmd[cmd.index("-p") + 1])
                        out = ps_rows_pty.get(pid_q, "")
                        return subprocess.CompletedProcess(cmd, 0 if out else 1, out.encode("utf-8"), b"")
                    if exe == "lsof":
                        return subprocess.CompletedProcess(cmd, 0, f"p9050\nn{proj}\n".encode("utf-8"), b"")
                    raise AssertionError(f"unexpected subprocess cmd: {cmd}")

                cl_sid = "12345678-aaaa-4bbb-8ccc-ddddeeeeffff"
                with mock.patch("agent_voice_input.session_ctx.subprocess.run", side_effect=fake_subproc_pty):
                    ok_cl = VST.update_claude_session(
                        {"session_id": cl_sid},
                        home=home,
                        proc_root=no_proc,
                        runtime_dir=rt,
                        start_pid=9081,
                    )
                self.assertTrue(ok_cl)
                pty_buf: list[bytes] = []
                _read_pty_nonblocking(master_fd, pty_buf)
                self.assertEqual(
                    b"".join(pty_buf).decode("utf-8"),
                    "\033]2;claude · ~/repos/voice-app [#12345678]\007",
                )
                cl_reg = json.loads((rt / "sessions" / "claude-12345678.json").read_text(encoding="utf-8"))
                self.assertEqual(cl_reg["tty"], slave_name)
                self.assertEqual(cl_reg["pid"], 9050)
                self.assertEqual(cl_reg["cwd"], str(proj))
            finally:
                os.close(master_fd)
                os.close(slave_fd)

            # 2. Deterministic /dev/ttys019 normalization + agy multi-hop walk + headless -p / --print blocking
            cid_ok = "abcdef12-3333-4444-8555-666677778888"
            cid_hl = "fedcba98-3333-4444-8555-666677778888"
            lock_ok = presence_dir / f"{cid_ok}.lock"
            lock_hl = presence_dir / f"{cid_hl}.lock"
            lock_ok.write_text("", encoding="utf-8")
            lock_hl.write_text("", encoding="utf-8")

            ps_rows_ttys: dict[int, str] = {
                7001: "7001 600 Ss ttys019 -zsh\n",
                # Interactive agy spawning hook in detached child chain without direct tty
                7100: "7100 7001 S+ ?? /Users/dev/.local/bin/agy\n",
                7101: "7101 7100 Ss ?? /bin/sh -c agent-voice-input session-hook --cli agy\n",
                7102: "7102 7101 S ?? python3 agent-voice-input session-hook --cli agy\n",
                # Headless agy -p invocation on same shell
                7200: "7200 7001 S+ ttys019 /Users/dev/.local/bin/agy -p summarize diff\n",
                7201: "7201 7200 S ?? python3 agent-voice-input session-hook --cli agy\n",
                # Headless claude -p and --print invocations (native & node wrapper)
                7300: "7300 7001 S+ ttys019 claude -p review changes\n",
                7301: "7301 7300 S ?? python3 agent-voice-input session-hook --cli claude\n",
                7400: "7400 7001 S+ ttys019 node /opt/claude/versions/2.1.9 --print check\n",
                7401: "7401 7400 S ?? python3 agent-voice-input session-hook --cli claude\n",
                # Detached non-agent session leader without tty (must stop at session boundary!)
                7500: "7500 7001 Ss ?? /bin/bash -c hook\n",
                7501: "7501 7500 S ?? python3 agent-voice-input\n",
                # Session-leader boundary guards against climbing into an outer host claude terminal:
                7600: "7600 600 S+ ttys019 claude\n",
                # (a) New interactive login/pty session leader WITH tty (Ss ttys025) stops immediately
                7601: "7601 7600 Ss ttys025 -zsh\n",
                7602: "7602 7601 S ttys025 python3 -m unittest\n",
                # (b) setsid() test runner WITHOUT tty (Ss ??) allows only 1 hop (7610), never reaching 7600
                7610: "7610 7600 S ?? python3 -m unittest\n",
                7611: "7611 7610 Ss ?? python3 hook_child\n",
            }
            osc2_calls: list[tuple[str, str]] = []

            def fake_subproc_ttys(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
                self.assertIs(kwargs.get("stdin"), subprocess.DEVNULL)
                exe = os.path.basename(cmd[0])
                if exe == "ps" and "-p" in cmd:
                    pid_q = int(cmd[cmd.index("-p") + 1])
                    out = ps_rows_ttys.get(pid_q, "")
                    return subprocess.CompletedProcess(cmd, 0 if out else 1, out.encode("utf-8"), b"")
                if exe == "lsof":
                    return subprocess.CompletedProcess(cmd, 0, f"p7100\nn{proj}\n".encode("utf-8"), b"")
                raise AssertionError(f"unexpected subprocess cmd: {cmd}")

            fd_ok = os.open(str(lock_ok), os.O_RDWR)
            fd_hl = os.open(str(lock_hl), os.O_RDWR)
            try:
                fcntl.flock(fd_ok, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd_hl, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with mock.patch(
                    "agent_voice_input.session_ctx.subprocess.run", side_effect=fake_subproc_ttys
                ), mock.patch(
                    "agent_voice_input.session_title.write_osc2",
                    side_effect=lambda t, s: osc2_calls.append((t, s)) or bool(t),
                ):
                    # Interactive agy succeeds, normalizes ttys019 -> /dev/ttys019
                    self.assertTrue(
                        VST.update_agy_session(
                            {"conversationId": cid_ok},
                            home=home,
                            proc_root=no_proc,
                            runtime_dir=rt,
                            start_pid=7102,
                        )
                    )
                    agy_reg = json.loads((rt / "sessions" / "agy-abcdef12.json").read_text(encoding="utf-8"))
                    self.assertEqual(agy_reg["tty"], "/dev/ttys019")
                    self.assertEqual(agy_reg["pid"], 7100)
                    self.assertEqual(agy_reg["cwd"], str(proj))
                    self.assertEqual(osc2_calls[-1], ("/dev/ttys019", "agy · ~/repos/voice-app [#abcdef12]"))

                    # Headless agy -p blocked even when presence lock is held!
                    before_n = len(osc2_calls)
                    self.assertFalse(
                        VST.update_agy_session(
                            {"conversationId": cid_hl, "cwd": str(proj)},
                            home=home,
                            proc_root=no_proc,
                            runtime_dir=rt,
                            start_pid=7201,
                        )
                    )
                    self.assertFalse((rt / "sessions" / "agy-fedcba98.json").exists())
                    self.assertEqual(len(osc2_calls), before_n)

                    # Headless claude -p & node claude --print blocked!
                    for bad_sid, bad_pid in (
                        ("88880001-1111-4222-8333-444455556666", 7301),
                        ("88880002-1111-4222-8333-444455556666", 7401),
                    ):
                        self.assertFalse(
                            VST.update_claude_session(
                                {"session_id": bad_sid, "cwd": str(proj)},
                                home=home,
                                proc_root=no_proc,
                                runtime_dir=rt,
                                start_pid=bad_pid,
                            )
                        )
                        self.assertFalse((rt / "sessions" / f"claude-{bad_sid[:8]}.json").exists())
                    self.assertEqual(len(osc2_calls), before_n)

                    # Detached session leader (7500 Ss ??) does not leak across session boundary to 7001 ttys019
                    self.assertEqual(VST._find_tty(7501, no_proc), "")

                    # Session-leader boundary in _walk_ancestor (non-/proc):
                    # (a) Ss ttys025 stops immediately; (b) Ss ?? stops after 1 hop without hitting outer claude (7600)
                    for child_pid in (7602, 7611):
                        self.assertEqual(VST._find_ancestor_cli(child_pid, no_proc, "claude"), (0, []))

            finally:
                os.close(fd_ok)
                os.close(fd_hl)

            # 3. Mock /proc _walk_ancestor session-leader boundary check (has_tty stops immediately; no_tty stops after 1 hop)
            proc_sl = base / "proc_sl"
            _write_fake_proc(proc_sl, 8000, ppid=1, psid=8000, tty_nr=34816, comm="claude", cmdline=["claude"])
            # (a) New session leader WITH tty (8010 == psid 8010, tty_nr > 0) -> stops at 8010
            _write_fake_proc(proc_sl, 8010, ppid=8000, psid=8010, tty_nr=34817, comm="zsh", cmdline=["-zsh"])
            _write_fake_proc(proc_sl, 8011, ppid=8010, psid=8010, tty_nr=34817, comm="python3", cmdline=["python3", "-m", "unittest"])
            # (b) setsid() session leader WITHOUT tty (8021 == psid 8021, tty_nr == 0) -> 1 hop checks 8020, stops before 8000
            _write_fake_proc(proc_sl, 8020, ppid=8000, psid=8000, tty_nr=0, comm="python3", cmdline=["python3", "-m", "unittest"])
            _write_fake_proc(proc_sl, 8021, ppid=8020, psid=8021, tty_nr=0, comm="python3", cmdline=["python3", "hook"])
            # (c) Direct setsid() hook spawned by claude (8030 == psid 8030, tty_nr == 0, ppid == 8000) -> 1 hop hits 8000!
            _write_fake_proc(proc_sl, 8030, ppid=8000, psid=8030, tty_nr=0, comm="sh", cmdline=["/bin/sh", "-c", "hook"])
            self.assertEqual(VST._find_ancestor_cli(8011, proc_sl, "claude"), (0, []))
            self.assertEqual(VST._find_ancestor_cli(8021, proc_sl, "claude"), (0, []))
            self.assertEqual(VST._find_ancestor_cli(8030, proc_sl, "claude")[0], 8000)


if __name__ == "__main__":
    unittest.main()
