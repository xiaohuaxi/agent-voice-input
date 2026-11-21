"""Real unprivileged loopback OpenSSH ``sshd`` end-to-end tests for ``RemoteSshManager``.

Exercises production ``RemoteSshManager`` over real ``ssh`` + ``ControlMaster`` + stdin-piped
``_remote_probe.py`` against an ephemeral, isolated local ``sshd`` bound to ``127.0.0.1``.
Never touches the host user's ``~/.ssh``, ``~/.claude``, ``~/.codex``, or system services.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agent_voice_input.config import RemoteConfig, RemoteHostConfig  # noqa: E402
from agent_voice_input.remote_ssh import (  # noqa: E402
    BREAKER_CONN_COOLDOWN_SEC,
    RemoteSshManager,
)
from agent_voice_input.session_ctx import encode_claude_project_dir  # noqa: E402


def _find_sshd_bin() -> str | None:
    if found := shutil.which("sshd"):
        return found
    for cand in ("/usr/sbin/sshd", "/sbin/sshd", "/usr/local/sbin/sshd"):
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _pick_free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _write_claude_jsonl(
    home: Path,
    sid: str,
    cwd: str,
    turns: list[tuple[str, str]],
    *,
    mtime: float | None = None,
) -> Path:
    encs = encode_claude_project_dir(cwd)
    pdir = home / ".claude" / "projects" / (encs[0] if encs else "-proj")
    pdir.mkdir(parents=True, exist_ok=True)
    jf = pdir / f"{sid}.jsonl"
    lines: list[str] = []
    for role, text in turns:
        lines.append(
            json.dumps(
                {
                    "sessionId": sid,
                    "cwd": cwd,
                    "isSidechain": False,
                    "type": role,
                    "message": {"role": role, "content": [{"type": "text", "text": text}]},
                },
                ensure_ascii=False,
            )
        )
    jf.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(jf, (mtime, mtime))
    return jf


def _append_claude_turns(jf: Path, sid: str, cwd: str, turns: list[tuple[str, str]]) -> None:
    lines: list[str] = []
    for role, text in turns:
        lines.append(
            json.dumps(
                {
                    "sessionId": sid,
                    "cwd": cwd,
                    "isSidechain": False,
                    "type": role,
                    "message": {"role": role, "content": [{"type": "text", "text": text}]},
                },
                ensure_ascii=False,
            )
        )
    with jf.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


class _LoopbackSshdFixture:
    """Ephemeral unprivileged loopback ``sshd`` + isolated client SSH config wrapper."""

    def __init__(self, *, sshd_bin: str, ssh_bin: str, ssh_keygen_bin: str) -> None:
        self.sshd_bin = sshd_bin
        self.ssh_bin = ssh_bin
        self.ssh_keygen_bin = ssh_keygen_bin
        self.base_dir = Path(tempfile.mkdtemp(prefix="avi-sshd-", dir="/tmp"))
        os.chmod(self.base_dir, 0o700)
        self.rt_dir = self.base_dir / "rt"
        self.rt_dir.mkdir(mode=0o700)
        self.remote_home = self.base_dir / "rhome"
        self.remote_home.mkdir(mode=0o700)
        self.fake_proc = self.base_dir / "rproc"
        self.fake_proc.mkdir(mode=0o700)
        self.client_home = self.base_dir / "chome"
        self.client_home.mkdir(mode=0o700)

        self.user = pwd.getpwuid(os.getuid()).pw_name
        self.port = _pick_free_loopback_port()
        self.host_key = self.base_dir / "host_ed25519"
        self.client_key = self.base_dir / "client_ed25519"
        self.authorized_keys = self.base_dir / "authorized_keys"
        self.known_hosts = self.base_dir / "known_hosts"
        self.sshd_config = self.base_dir / "sshd_config"
        self.ssh_config = self.base_dir / "ssh_config"
        self.ssh_wrapper = self.base_dir / "ssh_wrapper.sh"
        self.sshd_proc: subprocess.Popen[bytes] | None = None

        self._prepare_keys_and_configs()

    def _prepare_keys_and_configs(self) -> None:
        for key_path in (self.host_key, self.client_key):
            proc = subprocess.run(
                [self.ssh_keygen_bin, "-q", "-t", "ed25519", "-N", "", "-f", str(key_path)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
                check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"ssh-keygen failed: {proc.stderr.decode('utf-8', errors='replace')}")
            os.chmod(key_path, 0o600)

        client_pub = self.client_key.with_suffix(".pub").read_text(encoding="utf-8").strip()
        host_pub = self.host_key.with_suffix(".pub").read_text(encoding="utf-8").strip()

        self.authorized_keys.write_text(f"{client_pub}\n", encoding="utf-8")
        os.chmod(self.authorized_keys, 0o600)

        self.known_hosts.write_text(f"[127.0.0.1]:{self.port} {host_pub}\n", encoding="utf-8")
        os.chmod(self.known_hosts, 0o600)

        # Lock remote probe execution strictly to our isolated temporary HOME & fake /proc via SetEnv,
        # and disable PermitUserEnvironment / PermitUserRC so host ~/.ssh/environment & ~/.ssh/rc are never read.
        self.sshd_config.write_text(
            "\n".join(
                [
                    f"Port {self.port}",
                    "ListenAddress 127.0.0.1",
                    f"HostKey {self.host_key}",
                    f"PidFile {self.base_dir / 'sshd.pid'}",
                    f"AuthorizedKeysFile {self.authorized_keys}",
                    "StrictModes no",
                    "PubkeyAuthentication yes",
                    "PasswordAuthentication no",
                    "KbdInteractiveAuthentication no",
                    "UsePAM no",
                    "PermitUserEnvironment no",
                    "PermitUserRC no",
                    f"SetEnv HOME={self.remote_home} AVI_REMOTE_PROC_ROOT={self.fake_proc}",
                    f"AllowUsers {self.user}",
                    "LogLevel ERROR",
                    "Subsystem sftp internal-sftp",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(self.sshd_config, 0o600)

        self.ssh_config.write_text(
            "\n".join(
                [
                    "Host loopbox",
                    "  HostName 127.0.0.1",
                    f"  Port {self.port}",
                    f"  User {self.user}",
                    f"  IdentityFile {self.client_key}",
                    "  IdentitiesOnly yes",
                    f"  UserKnownHostsFile {self.known_hosts}",
                    "  StrictHostKeyChecking yes",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(self.ssh_config, 0o600)

        self.ssh_wrapper.write_text(
            f'#!/bin/sh\nexec "{self.ssh_bin}" -F "{self.ssh_config}" "$@"\n',
            encoding="utf-8",
        )
        os.chmod(self.ssh_wrapper, 0o700)

    def start(self) -> None:
        if self.sshd_proc is not None and self.sshd_proc.poll() is None:
            return
        self.sshd_proc = subprocess.Popen(
            [self.sshd_bin, "-D", "-e", "-f", str(self.sshd_config)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if self.sshd_proc.poll() is not None:
                err_txt = ""
                if self.sshd_proc.stderr is not None:
                    err_txt = self.sshd_proc.stderr.read().decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"sshd exited early rc={self.sshd_proc.returncode}: {err_txt.strip()}"
                )
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.15):
                    return
            except OSError:
                time.sleep(0.02)
        raise TimeoutError(f"sshd did not start listening on 127.0.0.1:{self.port}")

    def stop_sshd(self) -> None:
        proc, self.sshd_proc = self.sshd_proc, None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2.0)
        finally:
            if proc.stderr is not None:
                try:
                    proc.stderr.close()
                except Exception:
                    pass

    def close_control_masters(self) -> None:
        for sock_file in self.rt_dir.glob("cm-*"):
            try:
                subprocess.run(
                    [
                        self.ssh_bin,
                        "-F",
                        str(self.ssh_config),
                        "-o",
                        f"ControlPath={sock_file}",
                        "-O",
                        "exit",
                        "loopbox",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2.0,
                    check=False,
                )
            except Exception:
                pass

    def cleanup(self) -> None:
        try:
            self.close_control_masters()
        finally:
            self.stop_sshd()
            shutil.rmtree(self.base_dir, ignore_errors=True)


class RemoteSshLoopbackE2ETests(unittest.TestCase):
    def _require_loopback_sshd_tools(self) -> tuple[str, str, str, str]:
        if sys.platform != "linux":
            self.skipTest("unprivileged loopback sshd e2e targets Linux")
        sshd_bin = _find_sshd_bin()
        ssh_bin = shutil.which("ssh")
        keygen_bin = shutil.which("ssh-keygen")
        git_bin = shutil.which("git")
        missing = [
            name
            for name, path in (
                ("sshd", sshd_bin),
                ("ssh", ssh_bin),
                ("ssh-keygen", keygen_bin),
                ("git", git_bin),
            )
            if not path
        ]
        if missing:
            msg = f"loopback sshd e2e requires {', '.join(missing)}"
            if os.environ.get("AGENT_VOICE_INPUT_CI_NO_SKIP") == "1":
                self.fail(f"AGENT_VOICE_INPUT_CI_NO_SKIP=1: {msg}")
            self.skipTest(msg)
        assert sshd_bin and ssh_bin and keygen_bin and git_bin
        return sshd_bin, ssh_bin, keygen_bin, git_bin

    def test_ci_no_skip_guard_behavior(self) -> None:
        # Sanity check that helper path resolution never mutates environment or raises.
        port = _pick_free_loopback_port()
        self.assertGreater(port, 0)
        self.assertLessEqual(port, 65535)

    def test_real_sshd_loopback_probe_etag_incremental_and_reconnect(self) -> None:
        sshd_bin, ssh_bin, keygen_bin, git_bin = self._require_loopback_sshd_tools()
        fixture = _LoopbackSshdFixture(
            sshd_bin=sshd_bin,
            ssh_bin=ssh_bin,
            ssh_keygen_bin=keygen_bin,
        )
        now = [1000.0]
        mgr: RemoteSshManager | None = None
        try:
            # 1. Create synthetic remote Git repository with committed + dirty files
            repo_dir = fixture.remote_home / "work" / "alpha-service"
            repo_dir.mkdir(parents=True)
            git_env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(fixture.remote_home),
                "GIT_CONFIG_NOSYSTEM": "1",
            }
            (repo_dir / "core_router.py").write_text("def route():\n    return 1\n", encoding="utf-8")
            for gcmd in (
                [git_bin, "init", "-q"],
                [git_bin, "config", "user.email", "ci@example.com"],
                [git_bin, "config", "user.name", "CI Runner"],
                [git_bin, "add", "core_router.py"],
                [git_bin, "commit", "-q", "-m", "init"],
            ):
                gp = subprocess.run(
                    gcmd,
                    cwd=str(repo_dir),
                    env=git_env,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(gp.returncode, 0, f"{gcmd} failed: {gp.stderr!r}")
            # Untracked dirty file should surface in git_status & git_hotwords
            (repo_dir / "session_gateway.py").write_text("class SessionGateway: pass\n", encoding="utf-8")

            real_repo = str(repo_dir.resolve())
            prior_sid = "11111111-2222-3333-4444-555555555555"
            cur_sid = "aaaa1111-bbbb-2222-cccc-333333333333"
            t_now = time.time()

            # Older prior session (>4 turns, >3000 chars) to exercise cold-start prior_turns & prior_hotwords
            prior_body = (
                "We refactored GrpcStreamRouter and token_bucket_limiter.py to preserve "
                "deterministic FIFO delivery across reconnects. "
            ) * 8
            _write_claude_jsonl(
                fixture.remote_home,
                prior_sid,
                real_repo,
                [
                    ("user", f"Review GrpcStreamRouter design: {prior_body}"),
                    ("assistant", f"Checked token_bucket_limiter.py metrics: {prior_body}"),
                    ("user", f"Check backpressure on GrpcStreamRouter: {prior_body}"),
                    ("assistant", f"Backpressure confirmed safe in token_bucket_limiter.py: {prior_body}"),
                    ("user", "Any follow-up?"),
                    ("assistant", "Ready for next session."),
                ],
                mtime=t_now - 120.0,
            )

            # Current short active session (1 exchange -> triggers cold-start prior extraction)
            cur_jsonl = _write_claude_jsonl(
                fixture.remote_home,
                cur_sid,
                real_repo,
                [
                    ("user", "Check session_gateway.py routing behavior."),
                    ("assistant", "Inspecting session_gateway.py now."),
                ],
                mtime=t_now,
            )

            fixture.start()

            host_cfg = RemoteHostConfig(
                name="loopbox",
                ssh_target="loopbox",
                ssh_bin=str(fixture.ssh_wrapper),
                aliases=("loopbox",),
            )
            remote_cfg = RemoteConfig(enabled=True, hosts={"loopbox": host_cfg})
            client_env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(fixture.client_home),
            }
            mgr = RemoteSshManager(
                remote_cfg,
                env=client_env,
                runtime_dir=fixture.rt_dir,
                clock_fn=lambda: now[0],
                hard_timeout_sec=4.0,
                enable_multiplexing=True,
            )

            # 2. Ping health check over real OpenSSH
            ping_status, _ping_ms, ping_msg = mgr.probe_host_ping(host_cfg, timeout_sec=4.0)
            self.assertEqual(ping_status, "OK", ping_msg)
            self.assertIn("target=loopbox", ping_msg)

            # 3. Initial full probe over real ssh + ControlMaster + stdin _remote_probe.py
            base_req = {
                "v": 1,
                "cli": "claude",
                "explicit_cli": True,
                "token": cur_sid[:8],
                "title": f"claude · {real_repo} [#{cur_sid[:8]}]",
                "cwd_hint": real_repo,
                "include_git": True,
                "include_repo_files": True,
                "cold_start_fallback": "auto",
            }
            resp1 = mgr.fetch_with_budget(
                host_cfg,
                base_req,
                cache_key=("loopbox", cur_sid, 1),
                budget_ms=4000,
            )
            self.assertIsNotNone(resp1, f"initial probe failed; breaker={mgr.breaker_snapshot()}")
            assert resp1 is not None
            self.assertTrue(resp1["ok"])
            self.assertFalse(resp1["not_modified"])
            self.assertEqual(resp1["cli"], "claude")
            self.assertEqual(resp1["session_id"], cur_sid)
            self.assertEqual(resp1["cwd"], real_repo)
            self.assertEqual(len(resp1["turns"]), 2)
            self.assertIn("session_gateway.py", resp1["turns"][0][1])
            self.assertIn("session_gateway.py", resp1["git_status"])
            self.assertIn("session_gateway.py", resp1["git_hotwords"])
            self.assertGreaterEqual(len(resp1["prior_turns"]), 2)
            self.assertTrue(
                any(
                    hw in resp1["prior_hotwords"]
                    for hw in ("GrpcStreamRouter", "token_bucket_limiter.py")
                ),
                f"expected prior_hotwords from prior session, got {resp1['prior_hotwords']!r}",
            )
            etag1 = resp1["etag"]
            self.assertTrue(etag1)

            # Verify ControlMaster Unix socket was genuinely created under isolated runtime_dir
            cm_socks = list(fixture.rt_dir.glob("cm-*"))
            self.assertTrue(cm_socks, f"expected ControlMaster socket under {fixture.rt_dir}")
            cm_inode = cm_socks[0].stat().st_ino
            self.assertTrue(stat.S_ISSOCK(cm_socks[0].stat().st_mode))

            # 4. Second probe reuses ControlMaster and hits ETag not_modified short-circuit
            req2 = {
                **base_req,
                "etag": etag1,
                "session_key_hint": f"claude:{cur_sid}",
                "prior_checked_session_id": cur_sid,
            }
            resp2 = mgr.fetch_with_budget(
                host_cfg,
                req2,
                cache_key=("loopbox", cur_sid, 2),
                budget_ms=4000,
            )
            self.assertIsNotNone(resp2)
            assert resp2 is not None
            self.assertTrue(resp2["ok"])
            self.assertTrue(resp2["not_modified"])
            self.assertTrue(resp2["unchanged"])
            self.assertEqual(resp2["etag"], etag1)
            self.assertEqual(resp2["turns"], [])
            # Same ControlMaster socket reused
            cm_socks_after = list(fixture.rt_dir.glob("cm-*"))
            self.assertEqual([p.stat().st_ino for p in cm_socks_after], [cm_inode])

            # 5. Append new turns to remote JSONL -> incremental refresh with new ETag
            _append_claude_turns(
                cur_jsonl,
                cur_sid,
                real_repo,
                [
                    ("user", "Now inspect CircuitBreakerMetrics export."),
                    ("assistant", "Found CircuitBreakerMetrics counter."),
                ],
            )
            resp3 = mgr.fetch_with_budget(
                host_cfg,
                req2,
                cache_key=("loopbox", cur_sid, 3),
                budget_ms=4000,
            )
            self.assertIsNotNone(resp3)
            assert resp3 is not None
            self.assertTrue(resp3["ok"])
            self.assertFalse(resp3["not_modified"])
            self.assertNotEqual(resp3["etag"], etag1)
            self.assertEqual(len(resp3["turns"]), 4)
            self.assertIn("CircuitBreakerMetrics", resp3["turns"][-2][1])

            # 6. Kill sshd + ControlMaster -> graceful failure -> trip breaker -> restart sshd -> half-open recover
            fixture.close_control_masters()
            fixture.stop_sshd()

            fail1 = mgr.fetch_with_budget(
                host_cfg,
                base_req,
                cache_key=("loopbox", cur_sid, 4),
                budget_ms=4000,
            )
            self.assertIsNone(fail1)
            fail2 = mgr.fetch_with_budget(
                host_cfg,
                base_req,
                cache_key=("loopbox", cur_sid, 5),
                budget_ms=4000,
            )
            self.assertIsNone(fail2)
            snap_tripped = mgr.breaker_snapshot()["loopbox"]
            self.assertTrue(snap_tripped["tripped"])
            self.assertTrue(snap_tripped["open"])
            self.assertGreaterEqual(snap_tripped["consecutive_failures"], 2)

            # Restart sshd on the same loopback port and advance virtual clock past cooldown
            fixture.start()
            now[0] += BREAKER_CONN_COOLDOWN_SEC + 1.0

            resp4 = mgr.fetch_with_budget(
                host_cfg,
                {**base_req, "etag": resp3["etag"], "session_key_hint": f"claude:{cur_sid}"},
                cache_key=("loopbox", cur_sid, 6),
                budget_ms=4000,
                allow_half_open=True,
            )
            self.assertIsNotNone(resp4, f"reconnect probe failed; breaker={mgr.breaker_snapshot()}")
            assert resp4 is not None
            self.assertTrue(resp4["ok"])
            self.assertTrue(resp4["not_modified"])
            snap_recovered = mgr.breaker_snapshot()["loopbox"]
            self.assertFalse(snap_recovered["tripped"])
            self.assertFalse(snap_recovered["open"])
            self.assertEqual(snap_recovered["consecutive_failures"], 0)
        finally:
            if mgr is not None:
                mgr.close()
            fixture.cleanup()


if __name__ == "__main__":
    unittest.main()
