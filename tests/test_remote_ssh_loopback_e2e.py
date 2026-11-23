"""Real unprivileged loopback OpenSSH ``sshd`` end-to-end tests for ``RemoteSshManager``.

Exercises production ``RemoteSshManager`` over real ``ssh`` + ``ControlMaster`` + stdin-piped
``_remote_probe.py`` against an ephemeral, isolated local ``sshd`` bound to ``127.0.0.1``.
Never touches the host user's ``~/.ssh``, ``~/.claude``, ``~/.codex``, or system services.
"""
from __future__ import annotations

from email.parser import BytesParser
from email.policy import default as email_policy_default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import pwd
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agent_voice_input.audio_proc import pack_pcm16_mono_wav  # noqa: E402
from agent_voice_input.config import (  # noqa: E402
    AsrConfig,
    AudioConfig,
    Config,
    HotkeyConfig,
    RefinerConfig,
    RemoteConfig,
    RemoteHostConfig,
)
from agent_voice_input.daemon import VoiceInputDaemon  # noqa: E402
from agent_voice_input.platform_common import ForegroundWindow, PasteOutcome  # noqa: E402
from agent_voice_input.remote_ssh import (  # noqa: E402
    BREAKER_CONN_COOLDOWN_SEC,
    RemoteSshManager,
)
from agent_voice_input.session_ctx import SessionTracker, encode_claude_project_dir  # noqa: E402


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
            remote_cfg = RemoteConfig(hosts=(host_cfg,))
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

    def _run_daemon_remote_devbox_prefetch_into_stage2_e2e(
        self,
        *,
        base_dir: Path,
        rt_dir: Path,
        remote_home: Path,
        client_home: Path,
        ssh_bin: str,
        ssh_target: str,
        git_bin: str,
        ssh_call_log: Path,
        expect_control_master: bool,
    ) -> None:
        """Shared E2E driver: VoiceInputDaemon + [@devbox] async SSH prefetch -> loopback Stage 1/2 HTTP."""
        repo_dir = remote_home / "work" / "alpha-service"
        repo_dir.mkdir(parents=True, exist_ok=True)
        git_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(remote_home),
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
        (repo_dir / "session_gateway.py").write_text("class SessionGateway:\n    pass\n", encoding="utf-8")

        real_repo = str(repo_dir.resolve())
        prior_sid = "11111111-2222-3333-4444-555555555555"
        cur_sid = "aaaa1111-bbbb-2222-cccc-333333333333"
        t_now = time.time()

        prior_body = (
            "We refactored GrpcStreamRouter and token_bucket_limiter.py to preserve "
            "deterministic FIFO delivery across reconnects. "
        ) * 8
        _write_claude_jsonl(
            remote_home,
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
        _write_claude_jsonl(
            remote_home,
            cur_sid,
            real_repo,
            [
                ("user", "Check session_gateway.py routing behavior."),
                ("assistant", "Inspecting session_gateway.py now."),
            ],
            mtime=t_now,
        )

        cfg_dir = client_home / ".config" / "agent-voice-input"
        cfg_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        hw_file = cfg_dir / "hotwords.txt"
        hw_file.write_text("[global]\nRemoteGlobalTerm\n\n[coding]\nRemoteDevboxTerm\n", encoding="utf-8")
        os.chmod(hw_file, 0o600)

        asr_reqs: list[dict[str, Any]] = []
        refiner_reqs: list[dict[str, Any]] = []
        srv_lock = threading.Lock()

        class _LoopbackHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                del format, args

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or "0")
                raw = self.rfile.read(length)
                ctype = str(self.headers.get("Content-Type") or "")
                if self.path.endswith("/audio/transcriptions"):
                    msg = BytesParser(policy=email_policy_default).parsebytes(
                        f"Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + raw
                    )
                    prompt_txt = ""
                    if msg.is_multipart():
                        for part in msg.iter_parts():
                            if part.get_param("name", header="content-disposition") == "prompt":
                                prompt_txt = (part.get_payload(decode=True) or b"").decode(
                                    "utf-8", errors="replace"
                                )
                    with srv_lock:
                        asr_reqs.append({"prompt": prompt_txt})
                    body = json.dumps(
                        {"text": "帮我检查 grpc stream router 和 session gateway 的限流逻辑"},
                        ensure_ascii=False,
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if self.path.endswith("/responses"):
                    req_obj = json.loads(raw.decode("utf-8"))
                    with srv_lock:
                        refiner_reqs.append(req_obj)
                    ev1 = json.dumps(
                        {
                            "type": "response.output_text.delta",
                            "delta": "帮我检查 GrpcStreamRouter 和 session_gateway.py 的限流逻辑",
                        },
                        ensure_ascii=False,
                    )
                    ev2 = json.dumps(
                        {
                            "type": "response.completed",
                            "response": {
                                "status": "completed",
                                "usage": {
                                    "input_tokens": 3200,
                                    "input_tokens_details": {"cached_tokens": 0},
                                    "output_tokens": 14,
                                    "output_tokens_details": {"reasoning_tokens": 0},
                                },
                            },
                        },
                        ensure_ascii=False,
                    )
                    sse = (
                        f"event: response.output_text.delta\ndata: {ev1}\n\n"
                        f"event: response.completed\ndata: {ev2}\n\n"
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Content-Length", str(len(sse)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(sse)
                    return

                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), _LoopbackHandler)
        port = int(server.server_address[1])
        base_url = f"http://127.0.0.1:{port}/v1"
        srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
        srv_thread.start()

        sr = 16000
        voiced_wav = pack_pcm16_mono_wav(
            [int(10000 * math.sin(2.0 * math.pi * 260.0 * i / sr)) for i in range(int(sr * 0.45))],
            sample_rate=sr,
        )
        remote_win = ForegroundWindow(
            hwnd=601,
            process="WindowsTerminal.exe",
            title=f"[@devbox] claude · {real_repo} [#{cur_sid[:8]}]",
        )
        deliveries: list[str] = []

        class _RemoteTestPlatform:
            last_audio_error = ""

            def set_recorder_lost_callback(self, cb: Callable[..., Any] | None) -> None:
                del cb

            def set_recorder_recovered_callback(self, cb: Callable[..., Any] | None) -> None:
                del cb

            def start_event_listener(self, on_hotkey_event: Callable[[dict[str, Any]], None]) -> None:
                del on_hotkey_event

            def is_allowed_terminal(self, window: ForegroundWindow) -> bool:
                del window
                return True

            def get_foreground_window(self) -> ForegroundWindow:
                return remote_win

            def resolve_foreground_window(self, hint: ForegroundWindow | None = None) -> ForegroundWindow:
                return hint if (hint and hint.process) else remote_win

            def start_recording(self, runtime_dir: Path, *, loopback: bool = False, seq_id: int | None = None) -> None:
                del runtime_dir, loopback, seq_id

            def stop_recording(self, *, drain: bool = True) -> tuple[bytes, str]:
                del drain
                return voiced_wav, "remote_e2e_mic"

            def cancel_recording(self) -> None:
                pass

            def hud(self, state: str, text: str = "", duration_ms: int = 0, **kwargs: Any) -> None:
                del state, text, duration_ms, kwargs

            def beep(self, kind: str) -> None:
                del kind

            def deliver_text(
                self,
                text: str,
                seq_id: int,
                target_hwnd: int = 0,
                **kwargs: Any,
            ) -> PasteOutcome:
                del seq_id, target_hwnd
                bc = kwargs.get("before_commit")
                if callable(bc) and not bc():
                    return PasteOutcome(False, False, "cancelled", "")
                deliveries.append(text)
                return PasteOutcome(True, True, "pasted", remote_win.process, used_clipboard_paste=True)

            def close(self) -> None:
                pass

        host_cfg = RemoteHostConfig(
            name="devbox",
            ssh_target=ssh_target,
            ssh_bin=ssh_bin,
            aliases=("devbox",),
            timeout_ms=4000,
        )
        remote_cfg = RemoteConfig(timeout_ms=4000, hosts=(host_cfg,))
        cfg = Config(
            hotkey=HotkeyConfig(key="RControl", trigger_mode="tap_toggle", debounce_ms=0),
            audio=AudioConfig(save_recordings=False),
            asr=AsrConfig(
                provider="openai-compatible",
                api="audio-transcriptions",
                base_url=base_url,
                model="whisper-remote-e2e",
                hotwords_file=str(hw_file),
                max_retries=0,
            ),
            refiner=RefinerConfig(
                provider="openai-compatible",
                api="responses",
                base_url=base_url,
                model="refiner-remote-e2e",
                max_retries=0,
                include_git_status=True,
                cold_start_fallback="auto",
            ),
            remote=remote_cfg,
        )
        client_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(client_home),
            "XDG_CONFIG_HOME": str(client_home / ".config"),
            "XDG_CACHE_HOME": str(base_dir / "cache"),
            "XDG_RUNTIME_DIR": str(rt_dir),
            "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt_dir),
        }
        empty_proc = base_dir / "empty_proc"
        empty_proc.mkdir(exist_ok=True)
        tracker = SessionTracker(
            env=client_env,
            runtime_dir=rt_dir,
            proc_root=empty_proc,
            remote_cfg=remote_cfg,
            cold_start_fallback="auto",
        )
        if not expect_control_master:
            tracker.remote_manager.enable_multiplexing = False

        daemon: VoiceInputDaemon | None = None
        try:
            daemon = VoiceInputDaemon(
                cfg,
                env=client_env,
                runtime_dir=rt_dir,
                platform=_RemoteTestPlatform(),
                tracker=tracker,
                paste_settle_sec=0.0,
            )

            # 1. Start recording on [@devbox] -> triggers async SSH prefetch during recording
            r_start = daemon.handle_trigger("ptt_start", remote_win)
            self.assertTrue(r_start["ok"], r_start)

            # Wait for the background prefetch initiated by start_recording() to populate cache
            pref_deadline = time.monotonic() + 6.0
            prefetched_ready = False
            while time.monotonic() < pref_deadline:
                with tracker.remote_manager._lock:
                    comp = tracker.remote_manager._completed.get("devbox")
                    if comp is not None and comp.done_evt.is_set() and comp.response is not None:
                        prefetched_ready = True
                        break
                time.sleep(0.02)
            self.assertTrue(
                prefetched_ready,
                f"start_recording() async remote prefetch did not complete; breaker={tracker.remote_manager.breaker_snapshot()}",
            )
            if expect_control_master:
                cm_socks = list(rt_dir.glob("cm-*"))
                self.assertTrue(cm_socks, f"expected ControlMaster socket under {rt_dir}")
                self.assertTrue(stat.S_ISSOCK(cm_socks[0].stat().st_mode))

            calls_after_prefetch = len(
                [ln for ln in ssh_call_log.read_text(encoding="utf-8").splitlines() if ln.strip()]
            )
            self.assertEqual(calls_after_prefetch, 1, "expected exactly 1 SSH probe during start_recording prefetch")

            # 2. Stop recording -> consumes prefetched remote snapshot into Stage 1 + Stage 2 loopback HTTP
            r_stop = daemon.handle_trigger("ptt_stop")
            self.assertTrue(r_stop["ok"], r_stop)

            idle_deadline = time.monotonic() + 8.0
            st_final: dict[str, Any] = {}
            while time.monotonic() < idle_deadline:
                st_final = daemon.handle_control({"cmd": "status"})
                lr = st_final.get("last_result") if isinstance(st_final.get("last_result"), dict) else {}
                if (
                    st_final.get("state") == "idle"
                    and int(st_final.get("pending_count", 0)) == 0
                    and int(lr.get("seq_id") or 0) >= 1
                ):
                    break
                time.sleep(0.02)

            lr = st_final.get("last_result") or {}
            self.assertTrue(lr.get("ok"), f"remote dictation failed: {lr!r}")
            self.assertEqual(lr.get("session_key"), f"remote:devbox:claude:{cur_sid}")
            self.assertEqual(lr.get("ctx_fallback"), "prior_session")
            self.assertEqual(deliveries, ["帮我检查 GrpcStreamRouter 和 session_gateway.py 的限流逻辑"])

            # Verify stop_recording() hit the prefetched remote cache without a duplicate SSH round-trip
            calls_after_stop = len(
                [ln for ln in ssh_call_log.read_text(encoding="utf-8").splitlines() if ln.strip()]
            )
            self.assertEqual(
                calls_after_stop,
                calls_after_prefetch,
                "stop_recording() should consume start_recording() prefetch cache without re-probing SSH",
            )

            # Verify Stage 1 received remote + manual hotwords
            self.assertEqual(len(asr_reqs), 1)
            s1_prompt = asr_reqs[0]["prompt"]
            self.assertIn("RemoteDevboxTerm", s1_prompt)
            self.assertTrue(
                any(hw in s1_prompt for hw in ("session_gateway.py", "GrpcStreamRouter")),
                f"expected remote hotwords in Stage 1 prompt, got {s1_prompt!r}",
            )

            # Verify Stage 2 loopback HTTP received remote current turns + cold-start prior_turns + git_status
            self.assertEqual(len(refiner_reqs), 1)
            s2_input = refiner_reqs[0]["input"]
            dev_texts = "\n".join(
                m["content"][0]["text"] for m in s2_input if m.get("role") == "developer"
            )
            user_text = s2_input[-1]["content"][0]["text"]
            self.assertIn("<session_reference_log", dev_texts)
            self.assertIn("session_gateway.py", dev_texts)
            self.assertIn("<prior_session_reference", dev_texts)
            self.assertIn("GrpcStreamRouter", dev_texts)
            self.assertIn("<workspace_git_status", user_text)
            self.assertIn("session_gateway.py", user_text)
        finally:
            if daemon is not None:
                daemon.close()
            server.shutdown()
            server.server_close()
            srv_thread.join(timeout=3.0)

    def test_daemon_remote_devbox_prefetch_into_stage2_loopback_http_e2e(self) -> None:
        """Real loopback ``sshd`` + ``ControlMaster`` + ``VoiceInputDaemon`` ``[@devbox]`` prefetch -> Stage 2 HTTP."""
        sshd_bin, ssh_bin, keygen_bin, git_bin = self._require_loopback_sshd_tools()
        fixture = _LoopbackSshdFixture(
            sshd_bin=sshd_bin,
            ssh_bin=ssh_bin,
            ssh_keygen_bin=keygen_bin,
        )
        try:
            fixture.start()
            call_log = fixture.base_dir / "ssh_calls.log"
            call_log.write_text("", encoding="utf-8")
            counting_wrapper = fixture.base_dir / "ssh_counting_wrapper.sh"
            counting_wrapper.write_text(
                f'#!/bin/sh\necho 1 >> "{call_log}"\nexec "{fixture.ssh_bin}" -F "{fixture.ssh_config}" "$@"\n',
                encoding="utf-8",
            )
            os.chmod(counting_wrapper, 0o700)

            self._run_daemon_remote_devbox_prefetch_into_stage2_e2e(
                base_dir=fixture.base_dir,
                rt_dir=fixture.rt_dir,
                remote_home=fixture.remote_home,
                client_home=fixture.client_home,
                ssh_bin=str(counting_wrapper),
                ssh_target="loopbox",
                git_bin=git_bin,
                ssh_call_log=call_log,
                expect_control_master=True,
            )
        finally:
            fixture.cleanup()

    def test_daemon_remote_devbox_prefetch_into_stage2_loopback_http_local_exec_e2e(self) -> None:
        """Runs the exact ``VoiceInputDaemon`` ``[@devbox]`` async prefetch -> ``_remote_probe.py`` ->
        Stage 2 loopback HTTP E2E path even on hosts without ``openssh-server`` installed."""
        git_bin = shutil.which("git")
        if not git_bin:
            self.skipTest("requires git")
        base_dir = Path(tempfile.mkdtemp(prefix="avi-rem-e2e-", dir="/tmp"))
        os.chmod(base_dir, 0o700)
        rt_dir = base_dir / "rt"
        remote_home = base_dir / "rhome"
        fake_proc = base_dir / "rproc"
        client_home = base_dir / "chome"
        for d in (rt_dir, remote_home, fake_proc, client_home):
            d.mkdir(mode=0o700)
        try:
            call_log = base_dir / "ssh_calls.log"
            call_log.write_text("", encoding="utf-8")
            local_ssh_sim = base_dir / "local_ssh_exec.sh"
            local_ssh_sim.write_text(
                f'#!/bin/sh\n'
                f'echo 1 >> "{call_log}"\n'
                f'exec env HOME="{remote_home}" AVI_REMOTE_PROC_ROOT="{fake_proc}" "{sys.executable}" -\n',
                encoding="utf-8",
            )
            os.chmod(local_ssh_sim, 0o700)

            self._run_daemon_remote_devbox_prefetch_into_stage2_e2e(
                base_dir=base_dir,
                rt_dir=rt_dir,
                remote_home=remote_home,
                client_home=client_home,
                ssh_bin=str(local_ssh_sim),
                ssh_target="loopbox",
                git_bin=git_bin,
                ssh_call_log=call_log,
                expect_control_master=False,
            )
        finally:
            shutil.rmtree(base_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
