"""Unit tests for window title hooks (codex/claude/agy), integrations, and SessionTracker O(1) linkage."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import pty
import select
import stat
import subprocess
import sys
import tempfile
import termios
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agent_voice_input import session_title as VST  # noqa: E402
from agent_voice_input.config import parse_toml  # noqa: E402
from agent_voice_input.session_ctx import SessionTracker  # noqa: E402


def _drain_pty_master(master_fd: int) -> bytes:
    os.set_blocking(master_fd, False)
    chunks: list[bytes] = []
    while True:
        r, _, _ = select.select([master_fd], [], [], 0.05)
        if not r:
            break
        try:
            data = os.read(master_fd, 4096)
        except OSError:
            break
        if not data:
            break
        chunks.append(data)
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
                "agent-voice-input session-hook --cli claude",
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
                "agent-voice-input session-hook --cli agy",
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
                **os.environ,
                "HOME": str(home),
                "XDG_RUNTIME_DIR": str(xdg),
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                "PYTHONPATH": str(SRC_ROOT),
            }

            sid = "a1b2c3d4-5678-4abc-9def-0123456789ab"
            sub_sid = "f0e1d2c3-5678-4abc-9def-0123456789ab"
            hook_sh = str(REPO_ROOT / "integrations" / "claude" / "voice-title.sh")
            bin_cli = str(REPO_ROOT / "bin" / "agent-voice-input")

            def _run_with_fresh_pty(cmd: list[str], raw_payload: bytes) -> tuple[subprocess.CompletedProcess[bytes], bytes]:
                master_fd, slave_fd = pty.openpty()
                os.set_blocking(master_fd, False)
                r_pipe, w_pipe = os.pipe()
                os.write(w_pipe, raw_payload)
                os.close(w_pipe)

                def _attach_ctty() -> None:
                    os.setsid()
                    fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

                try:
                    proc = subprocess.run(
                        cmd,
                        stdin=r_pipe,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env=env,
                        preexec_fn=_attach_ctty,
                        pass_fds=(slave_fd,),
                        timeout=5,
                        check=False,
                    )
                finally:
                    os.close(r_pipe)
                    os.close(slave_fd)
                try:
                    return proc, _drain_pty_master(master_fd)
                finally:
                    os.close(master_fd)

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


if __name__ == "__main__":
    unittest.main()
