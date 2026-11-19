from __future__ import annotations

import array
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import math
import os
from pathlib import Path
import stat
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
from agent_voice_input.asr_providers import AsrResult, BaseAsrProvider  # noqa: E402
from agent_voice_input.audio_proc import (  # noqa: E402
    TARGET_SAMPLE_RATE,
    pack_pcm16_mono_wav,
    process_wav,
    save_failed_audio,
)
from agent_voice_input.config import AudioConfig, Config, ensure_secure_dir  # noqa: E402
from agent_voice_input.daemon import (  # noqa: E402
    DaemonAlreadyRunningError,
    VoiceInputDaemon,
    send_control_command,
)
from agent_voice_input.platform_wsl import (  # noqa: E402
    ForegroundWindow,
    PasteOutcome,
    SHIM_SCRIPT,
    run_win_shim_self_check,
)
from agent_voice_input.refiner import NoopRefiner  # noqa: E402
from agent_voice_input.session_ctx import (  # noqa: E402
    SESSION_REGISTRY_PRUNE_INTERVAL_SEC,
    SESSION_REGISTRY_TTL_SEC,
    prune_stale_session_registry,
    write_session_registry_entry,
)


def _dbfs_amp(dbfs: float) -> int:
    return max(1, min(32767, int(round((10.0 ** (dbfs / 20.0)) * 32767.0))))


def _build_test_wav(duration_sec: float, bursts: list[tuple[float, float, float]]) -> bytes:
    """Synthesize a 16 kHz mono WAV with sine bursts ``(start_sec, dur_sec, dbfs)``."""
    total = int(round(duration_sec * TARGET_SAMPLE_RATE))
    samples = array.array("h", [0]) * total
    for start_s, len_s, dbfs in bursts:
        amp = _dbfs_amp(dbfs)
        s_idx = max(0, min(total, int(round(start_s * TARGET_SAMPLE_RATE))))
        e_idx = max(s_idx, min(total, int(round((start_s + len_s) * TARGET_SAMPLE_RATE))))
        for i in range(s_idx, e_idx):
            val = int(round(amp * math.sin(2.0 * math.pi * 220.0 * (i - s_idx) / TARGET_SAMPLE_RATE)))
            # Ensure non-zero samples at sine zero-crossings do not skew exact voiced duration too low
            if val == 0:
                val = max(1, amp // 2)
            samples[i] = val
    return pack_pcm16_mono_wav(samples, sample_rate=TARGET_SAMPLE_RATE)


class _DummyAsr(BaseAsrProvider):
    provider_name = "dashscope"
    model = "qwen3-asr-flash"

    def __init__(self, text: str = "recovered text") -> None:
        self._text = text

    def transcribe(
        self, wav_bytes: bytes, *, context: object = None, cancel_token: object = None, **kwargs: object
    ) -> AsrResult:
        del wav_bytes, context, cancel_token, kwargs
        return AsrResult(text=self._text, provider=self.provider_name, model=self.model, latency_ms=12)


class _DummyPlatform:
    def __init__(self, outcome: PasteOutcome | None = None) -> None:
        self.outcome = outcome or PasteOutcome(True, True, "ok", "ghostty")
        self.closed = False
        self.deliver_calls: list[str] = []

    def start_event_listener(self, cb: object) -> None:
        del cb

    def get_foreground_window(self) -> ForegroundWindow:
        return ForegroundWindow(hwnd=101, process="ghostty", title="agy")

    def deliver_text(self, text: str, seq_id: int, target_hwnd: int = 0, **kwargs: object) -> PasteOutcome:
        del seq_id, target_hwnd, kwargs
        self.deliver_calls.append(text)
        return self.outcome

    def hud(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    def beep(self, kind: str) -> None:
        del kind

    def close(self) -> None:
        self.closed = True


class RuntimeHygieneAndGateV0102Tests(unittest.TestCase):
    def test_daemon_flock_single_instance_cloexec_no_unlink_and_cli_exit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rt = ensure_secure_dir(Path(td) / "rt")
            iso_home = ensure_secure_dir(Path(td) / "home")
            env = {
                "HOME": str(iso_home),
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
                "DASHSCOPE_API_KEY": "sk-unit-dummy-000000",
            }
            cfg = Config()

            d1 = VoiceInputDaemon(
                cfg,
                env=env,
                runtime_dir=rt,
                platform=_DummyPlatform(),
                asr_provider=_DummyAsr(),
                refiner=NoopRefiner(),
            )
            t1 = threading.Thread(target=d1.serve_forever, daemon=True)
            t1.start()
            child: subprocess.Popen[bytes] | None = None
            try:
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    if d1.sock_path.exists():
                        try:
                            st_resp = send_control_command(rt, {"cmd": "status"}, timeout_sec=0.5)
                            if st_resp.get("ok"):
                                break
                        except Exception:
                            pass
                    time.sleep(0.02)
                else:
                    self.fail("daemon 1 did not become ready in time")

                self.assertTrue(d1.lock_path.is_file())
                self.assertEqual(stat.S_IMODE(d1.lock_path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(d1.pid_path.stat().st_mode), 0o600)
                self.assertIsNotNone(d1._lock_fd)
                assert d1._lock_fd is not None
                self.assertFalse(os.get_inheritable(d1._lock_fd))

                # Spawn a long-lived child process while d1 holds daemon.lock to prove O_CLOEXEC prevents inheritance
                child = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

                # Second daemon in the same runtime_dir fails fast without clobbering d1's control.sock or daemon.pid
                d2 = VoiceInputDaemon(
                    cfg,
                    env=env,
                    runtime_dir=rt,
                    platform=_DummyPlatform(),
                    asr_provider=_DummyAsr(),
                    refiner=NoopRefiner(),
                )
                with self.assertRaises(DaemonAlreadyRunningError) as ctx_err:
                    d2.serve_forever()
                self.assertEqual(ctx_err.exception.pid, os.getpid())
                self.assertTrue(d1.sock_path.exists())
                self.assertTrue(d1.pid_path.exists())
                st_after = send_control_command(rt, {"cmd": "status"}, timeout_sec=0.5)
                self.assertTrue(st_after.get("ok"))

                # Unstarted offline instance close() also leaves active runtime files untouched
                d_unused = VoiceInputDaemon(
                    cfg,
                    env=env,
                    runtime_dir=rt,
                    platform=_DummyPlatform(),
                    asr_provider=_DummyAsr(),
                    refiner=NoopRefiner(),
                )
                d_unused.close()
                self.assertTrue(d1.sock_path.exists())
                self.assertTrue(d1.pid_path.exists())

                # CLI `daemon` subcommand prints friendly message with holder PID and returns exit code 1
                err_buf = io.StringIO()
                with (
                    mock.patch.dict(os.environ, env, clear=True),
                    mock.patch("agent_voice_input.daemon.create_platform", return_value=_DummyPlatform()),
                    redirect_stderr(err_buf),
                ):
                    rc_cli = cli_mod.main(["daemon", "--refiner", "off"])
                self.assertEqual(rc_cli, 1)
                self.assertIn(f"already running (pid={os.getpid()})", err_buf.getvalue())

                # Shut down d1 while `child` is STILL alive: lock must release cleanly and daemon.lock is NOT unlinked
                lock_ino_before = d1.lock_path.stat().st_ino
                send_control_command(rt, {"cmd": "shutdown"}, timeout_sec=1.0)
                t1.join(timeout=3.0)
                self.assertFalse(t1.is_alive())
                self.assertTrue(d1.lock_path.is_file())
                self.assertEqual(d1.lock_path.stat().st_ino, lock_ino_before)
                self.assertEqual(d1.lock_path.read_text(encoding="utf-8"), "")
                self.assertFalse(d1.sock_path.exists())
                self.assertFalse(d1.pid_path.exists())
                self.assertIsNone(child.poll(), "child process should still be running")

                # Instance 3 can immediately acquire the lock even while `child` is still alive
                d3 = VoiceInputDaemon(
                    cfg,
                    env=env,
                    runtime_dir=rt,
                    platform=_DummyPlatform(),
                    asr_provider=_DummyAsr(),
                    refiner=NoopRefiner(),
                )
                try:
                    d3.acquire_lock()
                    self.assertIsNotNone(d3._lock_fd)
                finally:
                    d3.close()
            finally:
                if child is not None and child.poll() is None:
                    child.terminate()
                    child.wait(timeout=3.0)
                d1.close()
                if t1.is_alive():
                    t1.join(timeout=2.0)

            # Symlink guard on daemon.lock
            rt_sym = ensure_secure_dir(Path(td) / "rt_sym")
            victim = Path(td) / "victim.txt"
            victim.write_text("keep", encoding="utf-8")
            (rt_sym / "daemon.lock").symlink_to(victim)
            d_sym = VoiceInputDaemon(
                cfg,
                env=env,
                runtime_dir=rt_sym,
                platform=_DummyPlatform(),
                asr_provider=_DummyAsr(),
                refiner=NoopRefiner(),
            )
            try:
                with self.assertRaises(OSError):
                    d_sym.acquire_lock()
                self.assertEqual(victim.read_text(encoding="utf-8"), "keep")
            finally:
                d_sym.close()

            # Legacy online daemon (no daemon.lock held, active control.sock responding to `status`):
            # new daemon detects it via status handshake, releases lock, and preserves legacy control.sock & daemon.pid!
            import fcntl
            import socket

            rt_leg = ensure_secure_dir(Path(td) / "rt_leg")
            leg_sock_path = rt_leg / "control.sock"
            leg_pid_path = rt_leg / "daemon.pid"
            leg_pid_path.write_text("424242\n", encoding="utf-8")
            leg_srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            leg_srv.bind(str(leg_sock_path))
            leg_srv.listen(2)
            leg_stop = threading.Event()

            def _serve_legacy() -> None:
                leg_srv.settimeout(0.1)
                while not leg_stop.is_set():
                    try:
                        c, _ = leg_srv.accept()
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    with c:
                        c.recv(4096)
                        c.sendall(b'{"ok":true,"pid":424242,"state":"idle"}\n')

            leg_th = threading.Thread(target=_serve_legacy, daemon=True)
            leg_th.start()
            try:
                d_up = VoiceInputDaemon(
                    cfg,
                    env=env,
                    runtime_dir=rt_leg,
                    platform=_DummyPlatform(),
                    asr_provider=_DummyAsr(),
                    refiner=NoopRefiner(),
                )
                with self.assertRaises(DaemonAlreadyRunningError) as leg_err:
                    d_up.serve_forever()
                self.assertEqual(leg_err.exception.pid, 424242)
                self.assertTrue(leg_sock_path.exists())
                self.assertTrue(leg_pid_path.is_file())
            finally:
                leg_stop.set()
                leg_srv.close()
                leg_th.join(timeout=1.0)

            # Busy/slow legacy online daemon (accepts AF_UNIX connection, stalls before replying):
            # still detected as live listener via connect() and falls back to daemon.pid without clobbering!
            rt_leg_slow = ensure_secure_dir(Path(td) / "rt_leg_slow")
            slow_sock_path = rt_leg_slow / "control.sock"
            slow_pid_path = rt_leg_slow / "daemon.pid"
            slow_pid_path.write_text("434343\n", encoding="utf-8")
            slow_srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            slow_srv.bind(str(slow_sock_path))
            slow_srv.listen(2)
            try:
                d_up_slow = VoiceInputDaemon(
                    cfg,
                    env=env,
                    runtime_dir=rt_leg_slow,
                    platform=_DummyPlatform(),
                    asr_provider=_DummyAsr(),
                    refiner=NoopRefiner(),
                )
                with self.assertRaises(DaemonAlreadyRunningError) as slow_err:
                    d_up_slow.serve_forever()
                self.assertEqual(slow_err.exception.pid, 434343)
                self.assertTrue(slow_sock_path.exists())
                self.assertTrue(slow_pid_path.is_file())
            finally:
                slow_srv.close()

            # `_cmd_stop` NEVER sends SIGTERM to an old PID left in an unlocked `daemon.lock` OR a stale `daemon.pid`
            # whose PID has been recycled by a non-daemon same-user process (here: `os.getpid()` itself)!
            rt_stale_lock = ensure_secure_dir(Path(td) / "rt_stale_lock")
            (rt_stale_lock / "daemon.lock").write_text("765432\n", encoding="utf-8")
            os.chmod(rt_stale_lock / "daemon.lock", 0o600)
            (rt_stale_lock / "daemon.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
            (rt_stale_lock / "control.sock").write_text("stale", encoding="utf-8")
            sigterm_targets: list[int] = []
            real_os_kill = os.kill

            def _spy_kill(pid_arg: int, sig_arg: int) -> None:
                if sig_arg != 0:
                    sigterm_targets.append(pid_arg)
                    return  # Never forward non-zero signals to test runner!
                real_os_kill(pid_arg, sig_arg)

            with (
                mock.patch.object(cli_mod.os, "kill", side_effect=_spy_kill),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(cli_mod._cmd_stop({**env, "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt_stale_lock)}), 0)
            self.assertEqual(sigterm_targets, [])
            self.assertFalse((rt_stale_lock / "daemon.pid").exists())
            self.assertFalse((rt_stale_lock / "control.sock").exists())

            # When control socket times out on a live legacy daemon (no daemon.lock held), `_cmd_stop` preserves control files & returns 1
            rt_indet = ensure_secure_dir(Path(td) / "rt_indet")
            (rt_indet / "daemon.pid").write_text("565656\n", encoding="utf-8")
            (rt_indet / "control.sock").write_text("live-legacy", encoding="utf-8")
            err_indet = io.StringIO()
            with (
                mock.patch.object(cli_mod, "send_control_command", side_effect=TimeoutError("busy")),
                redirect_stdout(io.StringIO()),
                redirect_stderr(err_indet),
            ):
                rc_indet = cli_mod._cmd_stop({**env, "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt_indet)})
            self.assertEqual(rc_indet, 1)
            self.assertTrue((rt_indet / "daemon.pid").is_file())
            self.assertTrue((rt_indet / "control.sock").exists())

            # Full listen backlog on legacy control.sock (`connect()` raises EAGAIN): preserves legacy files & raises!
            import errno

            rt_leg_eagain = ensure_secure_dir(Path(td) / "rt_leg_eagain")
            (rt_leg_eagain / "daemon.pid").write_text("444444\n", encoding="utf-8")
            (rt_leg_eagain / "control.sock").write_text("sock-placeholder", encoding="utf-8")
            d_eagain = VoiceInputDaemon(
                cfg,
                env=env,
                runtime_dir=rt_leg_eagain,
                platform=_DummyPlatform(),
                asr_provider=_DummyAsr(),
                refiner=NoopRefiner(),
            )
            with mock.patch("socket.socket.connect", side_effect=OSError(errno.EAGAIN, "Resource temporarily unavailable")):
                with self.assertRaises(DaemonAlreadyRunningError) as eag_err:
                    d_eagain.serve_forever()
            self.assertEqual(eag_err.exception.pid, 444444)
            self.assertTrue((rt_leg_eagain / "control.sock").exists())
            self.assertTrue((rt_leg_eagain / "daemon.pid").is_file())

            # `_cmd_stop` & `restart` lock-coordination: if another live daemon holds daemon.lock,
            # `_cmd_stop` does not unlink its files and `restart` aborts cleanly instead of colliding.
            rt_race = ensure_secure_dir(Path(td) / "rt_race")
            env_race = {**env, "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt_race)}
            (rt_race / "daemon.pid").write_text("999998\n", encoding="utf-8")
            (rt_race / "control.sock").write_text("live-sock", encoding="utf-8")
            held_fd = os.open(str(rt_race / "daemon.lock"), os.O_CREAT | os.O_RDWR, 0o600)
            os.fchmod(held_fd, 0o600)
            fcntl.flock(held_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                err_race = io.StringIO()
                with (
                    mock.patch.object(cli_mod, "send_control_command", side_effect=ConnectionRefusedError("starting")),
                    mock.patch.object(cli_mod.os, "kill", side_effect=ProcessLookupError(3, "gone")),
                    mock.patch.object(cli_mod.time, "sleep", lambda _s: None),
                    mock.patch.object(cli_mod.time, "monotonic", side_effect=[0.0, 10.0, 20.0, 30.0]),
                    redirect_stdout(io.StringIO()),
                    redirect_stderr(err_race),
                ):
                    self.assertEqual(cli_mod._cmd_stop(env_race), 1)
                self.assertTrue((rt_race / "control.sock").exists())
                self.assertTrue((rt_race / "daemon.pid").exists())

                err_rst = io.StringIO()
                with (
                    mock.patch.dict(os.environ, env_race, clear=True),
                    mock.patch.object(cli_mod, "_cmd_stop", return_value=0),
                    mock.patch.object(cli_mod, "_wait_daemon_lock_free", return_value=False),
                    redirect_stderr(err_rst),
                ):
                    rc_rst = cli_mod.main(["restart"])
                self.assertEqual(rc_rst, 1)
                self.assertIn("still holds daemon.lock", err_rst.getvalue())
            finally:
                fcntl.flock(held_fd, fcntl.LOCK_UN)
                os.close(held_fd)

    def test_retry_suppresses_duplicate_stdout_on_paste_and_honors_print_flag(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rt = ensure_secure_dir(Path(td) / "rt")
            iso_home = ensure_secure_dir(Path(td) / "home")
            wav_bytes = _build_test_wav(1.2, [(0.1, 0.8, -18.0)])
            saved = save_failed_audio(wav_bytes, rt, label="unit-retry")
            env = {
                "HOME": str(iso_home),
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
                "DASHSCOPE_API_KEY": "sk-unit-dummy-000000",
            }
            expected_text = "修复后的终端语音文本"

            # 1. `retry` when pasted=True: stdout is empty (no duplicate terminal echo), stderr confirms paste
            plat_pasted = _DummyPlatform(PasteOutcome(True, True, "ok", "ghostty"))
            out1, err1 = io.StringIO(), io.StringIO()
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("agent_voice_input.cli.create_platform", return_value=plat_pasted),
                mock.patch("agent_voice_input.daemon.create_asr_provider", return_value=_DummyAsr(expected_text)),
                redirect_stdout(out1),
                redirect_stderr(err1),
            ):
                rc1 = cli_mod.main(["retry", "unit-retry", "--refiner", "off"])
            self.assertEqual(rc1, 0)
            self.assertEqual(plat_pasted.deliver_calls, [expected_text])
            self.assertEqual(out1.getvalue(), "")
            self.assertIn(f"[agent-voice-input] pasted ({len(expected_text)} chars)", err1.getvalue())
            self.assertTrue(saved.is_file())

            # 2. `retry --print` when pasted=True: prints recovered text to stdout as well
            plat_print = _DummyPlatform(PasteOutcome(True, True, "ok", "ghostty"))
            out2, err2 = io.StringIO(), io.StringIO()
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("agent_voice_input.cli.create_platform", return_value=plat_print),
                mock.patch("agent_voice_input.daemon.create_asr_provider", return_value=_DummyAsr(expected_text)),
                redirect_stdout(out2),
                redirect_stderr(err2),
            ):
                rc2 = cli_mod.main(["retry", "unit-retry", "--refiner", "off", "--print"])
            self.assertEqual(rc2, 0)
            self.assertEqual(out2.getvalue().strip(), expected_text)
            self.assertIn("[agent-voice-input] pasted", err2.getvalue())

            # 3. `retry` when focus changed (pasted=False, clipboard=True): still prints to stdout so text is visible
            plat_clip = _DummyPlatform(PasteOutcome(False, True, "focus_changed", "chrome"))
            out3, err3 = io.StringIO(), io.StringIO()
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("agent_voice_input.cli.create_platform", return_value=plat_clip),
                mock.patch("agent_voice_input.daemon.create_asr_provider", return_value=_DummyAsr(expected_text)),
                redirect_stdout(out3),
                redirect_stderr(err3),
            ):
                rc3 = cli_mod.main(["retry", "unit-retry", "--refiner", "off"])
            self.assertEqual(rc3, 0)
            self.assertEqual(out3.getvalue().strip(), expected_text)
            self.assertIn("copied to clipboard", err3.getvalue())

            # 4. `once --file` preserves existing stdout printing contract even when pasted=True
            plat_once = _DummyPlatform(PasteOutcome(True, True, "ok", "ghostty"))
            out4, err4 = io.StringIO(), io.StringIO()
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("agent_voice_input.cli.create_platform", return_value=plat_once),
                mock.patch("agent_voice_input.daemon.create_asr_provider", return_value=_DummyAsr(expected_text)),
                redirect_stdout(out4),
                redirect_stderr(err4),
            ):
                rc4 = cli_mod.main(["once", "--file", str(saved), "--refiner", "off"])
            self.assertEqual(rc4, 0)
            self.assertEqual(out4.getvalue().strip(), expected_text)

    def test_prune_stale_session_registry_ttl_live_pid_and_atomic_guards(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rt = ensure_secure_dir(Path(td) / "rt")
            fake_proc = Path(td) / "proc"
            fake_proc.mkdir(parents=True)

            # Live interactive `claude` PID 41001 vs reused non-agent PID 41002 (`bash`) vs headless `claude -p` 41003
            live_dir = fake_proc / "41001"
            live_dir.mkdir()
            (live_dir / "comm").write_text("claude\n", encoding="utf-8")
            (live_dir / "cmdline").write_bytes(b"claude\x00--resume\x00aaaaaaaa-1111-2222-3333-444444444444\x00")

            reused_dir = fake_proc / "41002"
            reused_dir.mkdir()
            (reused_dir / "comm").write_text("bash\n", encoding="utf-8")
            (reused_dir / "cmdline").write_bytes(b"-bash\x00")

            headless_dir = fake_proc / "41003"
            headless_dir.mkdir()
            (headless_dir / "comm").write_text("claude\n", encoding="utf-8")
            (headless_dir / "cmdline").write_bytes(b"claude\x00-p\x00hello\x00")

            now = 1_800_000_000.0
            old_mtime = now - (SESSION_REGISTRY_TTL_SEC + 3600.0)
            fresh_mtime = now - (2 * 86400.0)

            p_fresh_dead = write_session_registry_entry(
                rt, cli="codex", session_id="11111111-aaaa-bbbb-cccc-000000000001", jsonl_path="/tmp/a.jsonl", agent_pid=99991
            )
            os.utime(p_fresh_dead, (fresh_mtime, fresh_mtime))

            p_old_dead = write_session_registry_entry(
                rt, cli="codex", session_id="22222222-aaaa-bbbb-cccc-000000000002", jsonl_path="/tmp/b.jsonl", agent_pid=99992
            )
            os.utime(p_old_dead, (old_mtime, old_mtime))

            p_old_live = write_session_registry_entry(
                rt, cli="claude", session_id="aaaaaaaa-1111-2222-3333-444444444444", jsonl_path="/tmp/c.jsonl", agent_pid=41001
            )
            os.utime(p_old_live, (old_mtime, old_mtime))

            p_old_reused = write_session_registry_entry(
                rt, cli="claude", session_id="33333333-aaaa-bbbb-cccc-000000000003", jsonl_path="/tmp/d.jsonl", agent_pid=41002
            )
            os.utime(p_old_reused, (old_mtime, old_mtime))

            p_old_headless = write_session_registry_entry(
                rt, cli="claude", session_id="44444444-aaaa-bbbb-cccc-000000000004", jsonl_path="/tmp/e.jsonl", agent_pid=41003
            )
            os.utime(p_old_headless, (old_mtime, old_mtime))

            sdir = rt / "sessions"
            p_old_corrupt = sdir / "agy-55555555.json"
            fd_c = os.open(str(p_old_corrupt), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.write(fd_c, b"{broken json")
            os.close(fd_c)
            os.utime(p_old_corrupt, (old_mtime, old_mtime))

            p_old_tmp = sdir / ".codex-deadbeef.123.abc.tmp"
            p_old_tmp.write_text("partial", encoding="utf-8")
            os.chmod(p_old_tmp, 0o600)
            os.utime(p_old_tmp, (now - 900.0, now - 900.0))

            p_fresh_tmp = sdir / ".codex-cafebabe.123.def.tmp"
            p_fresh_tmp.write_text("inflight", encoding="utf-8")
            os.chmod(p_fresh_tmp, 0o600)
            os.utime(p_fresh_tmp, (now - 30.0, now - 30.0))

            outside_target = Path(td) / "outside.json"
            outside_target.write_text("{}", encoding="utf-8")
            p_symlink = sdir / "codex-symlink0.json"
            p_symlink.symlink_to(outside_target)

            removed = prune_stale_session_registry(rt, now_ts=now, proc_root=fake_proc)
            # Removed: p_old_dead, p_old_reused, p_old_headless, p_old_corrupt, p_old_tmp (5 items)
            self.assertEqual(removed, 5)
            self.assertTrue(p_fresh_dead.is_file())
            self.assertTrue(p_old_live.is_file())
            self.assertTrue(p_fresh_tmp.is_file())
            self.assertTrue(outside_target.is_file())
            self.assertFalse(p_old_dead.exists())
            self.assertFalse(p_old_reused.exists())
            self.assertFalse(p_old_headless.exists())
            self.assertFalse(p_old_corrupt.exists())
            self.assertFalse(p_old_tmp.exists())

            # Non-/proc mode (Darwin): live same-user matching CLI is preserved; reused non-CLI PID & cross-user PID are pruned
            rt_mac = ensure_secure_dir(Path(td) / "rt_mac")
            p_mac_live = write_session_registry_entry(
                rt_mac, cli="codex", session_id="66666666-aaaa-bbbb-cccc-000000000006", jsonl_path="/tmp/f.jsonl", agent_pid=55001
            )
            os.utime(p_mac_live, (old_mtime, old_mtime))
            p_mac_reused = write_session_registry_entry(
                rt_mac, cli="codex", session_id="67676767-aaaa-bbbb-cccc-000000000067", jsonl_path="/tmp/f2.jsonl", agent_pid=55002
            )
            os.utime(p_mac_reused, (old_mtime, old_mtime))
            p_mac_ps_timeout = write_session_registry_entry(
                rt_mac, cli="codex", session_id="68686868-aaaa-bbbb-cccc-000000000068", jsonl_path="/tmp/f3.jsonl", agent_pid=55003
            )
            os.utime(p_mac_ps_timeout, (old_mtime, old_mtime))
            p_mac_other_uid = write_session_registry_entry(
                rt_mac, cli="codex", session_id="77777777-aaaa-bbbb-cccc-000000000007", jsonl_path="/tmp/g.jsonl", agent_pid=77777
            )
            os.utime(p_mac_other_uid, (old_mtime, old_mtime))

            def _fake_kill(pid: int, sig: int) -> None:
                del sig
                if pid == 77777:
                    raise PermissionError(1, "Operation not permitted")
                if pid in (55001, 55002, 55003):
                    return
                raise ProcessLookupError(3, "No such process")

            def _fake_ps_info(pid: int) -> tuple[str, int, str, list[str], bool] | None:
                if pid == 55001:
                    return ("codex", 1, "/dev/ttys001", ["codex"], True)
                if pid == 55002:
                    return ("zsh", 1, "/dev/ttys002", ["-zsh"], True)
                return None  # 55003 simulates `ps` transient timeout -> preserved while PID is alive

            with (
                mock.patch("agent_voice_input.session_ctx.os.kill", side_effect=_fake_kill),
                mock.patch("agent_voice_input.session_ctx.ps_proc_info", side_effect=_fake_ps_info),
            ):
                rem_mac = prune_stale_session_registry(rt_mac, now_ts=now, proc_root=None)
            self.assertEqual(rem_mac, 2)
            self.assertTrue(p_mac_live.is_file())
            self.assertTrue(p_mac_ps_timeout.is_file())
            self.assertFalse(p_mac_reused.exists())
            self.assertFalse(p_mac_other_uid.exists())

            # `write_session_registry_entry` refuses to os.replace without `.registry.lock` shared lock and cleans `.tmp`
            rt_lockfail = ensure_secure_dir(Path(td) / "rt_lockfail")
            with mock.patch("agent_voice_input.session_ctx._acquire_registry_flock", return_value=None):
                with self.assertRaises(OSError):
                    write_session_registry_entry(
                        rt_lockfail,
                        cli="claude",
                        session_id="99990000-0000-0000-0000-000000000099",
                        jsonl_path="/tmp/z.jsonl",
                    )
            self.assertEqual(list((rt_lockfail / "sessions").glob("*.tmp")), [])
            self.assertEqual(list((rt_lockfail / "sessions").glob("*.json")), [])

            # `_try_claim_prune_slot` re-reads clock under LOCK_EX so a delayed caller cannot roll back a newer stamp
            from agent_voice_input.session_ctx import _touch_prune_stamp_locked, _try_claim_prune_slot

            rt_clock = ensure_secure_dir(Path(td) / "rt_clock")
            sdir_clock = ensure_secure_dir(rt_clock / "sessions")
            _touch_prune_stamp_locked(sdir_clock, 5000.0)
            # Caller A observed stale `now_wall = 1000.0` before pausing, while B already wrote `5000.0`;
            # inside lock, `time.time()` is `5005.0` -> throttled (`False`) and stamp stays `5000.0`!
            with mock.patch("agent_voice_input.session_ctx.time.time", return_value=5005.0):
                self.assertFalse(_try_claim_prune_slot(sdir_clock, 1000.0))
            self.assertAlmostEqual((sdir_clock / ".prune.stamp").stat().st_mtime, 5000.0, places=0)

            # Cross-process 1-hour throttle via `.prune.stamp` & `.registry.lock` flock exclusion:
            rt_sub = ensure_secure_dir(Path(td) / "rt_sub")
            sub_code = (
                "import sys; sys.path.insert(0, %r); "
                "from agent_voice_input.session_ctx import write_session_registry_entry; "
                "write_session_registry_entry(%r, cli='claude', session_id=sys.argv[1], jsonl_path='/tmp/x.jsonl', agent_pid=99999)"
            ) % (str(_SRC_ROOT), str(rt_sub))
            r_s1 = subprocess.run(
                [sys.executable, "-c", sub_code, "aaaa1111-0000-0000-0000-000000000001"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(r_s1.returncode, 0)
            stale_p = rt_sub / "sessions" / "claude-aaaa1111.json"
            past_wall = time.time() - (SESSION_REGISTRY_TTL_SEC + 7200.0)
            os.utime(stale_p, (past_wall, past_wall))
            # Second separate process within 1 hour must NOT scan/delete `stale_p` because `.prune.stamp` is fresh!
            r_s2 = subprocess.run(
                [sys.executable, "-c", sub_code, "bbbb2222-0000-0000-0000-000000000002"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(r_s2.returncode, 0)
            self.assertTrue(stale_p.is_file(), "cross-process .prune.stamp should throttle prune within 1h")
            # Age `.prune.stamp` past 1h -> next separate process prunes `stale_p`!
            stamp_p = rt_sub / "sessions" / ".prune.stamp"
            old_stamp = time.time() - (SESSION_REGISTRY_PRUNE_INTERVAL_SEC + 60.0)
            os.utime(stamp_p, (old_stamp, old_stamp))
            r_s3 = subprocess.run(
                [sys.executable, "-c", sub_code, "cccc3333-0000-0000-0000-000000000003"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(r_s3.returncode, 0)
            self.assertFalse(stale_p.exists(), "expired .prune.stamp should trigger prune in fresh process")

            # Holding LOCK_SH on `.registry.lock` (simulating concurrent os.replace in hook) prevents prune deletion race
            import fcntl

            p_locked = write_session_registry_entry(
                rt_sub, cli="codex", session_id="dddd4444-0000-0000-0000-000000000004", jsonl_path="/tmp/y.jsonl", agent_pid=99997
            )
            os.utime(p_locked, (past_wall, past_wall))
            sh_fd = os.open(str(rt_sub / "sessions" / ".registry.lock"), os.O_RDWR)
            fcntl.flock(sh_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            try:
                self.assertEqual(prune_stale_session_registry(rt_sub, proc_root=fake_proc), 0)
                self.assertTrue(p_locked.is_file())
            finally:
                fcntl.flock(sh_fd, fcntl.LOCK_UN)
                os.close(sh_fd)
            self.assertEqual(prune_stale_session_registry(rt_sub, proc_root=fake_proc), 1)
            self.assertFalse(p_locked.exists())

            # VoiceInputDaemon.__init__ triggers initial prune using tracker.proc_root
            p_daemon_boot = write_session_registry_entry(
                rt, cli="agy", session_id="88888888-aaaa-bbbb-cccc-000000000008", jsonl_path="/tmp/h.jsonl", agent_pid=88888
            )
            past = time.time() - (SESSION_REGISTRY_TTL_SEC + 7200.0)
            os.utime(p_daemon_boot, (past, past))
            from agent_voice_input.session_ctx import SessionTracker

            iso_home = ensure_secure_dir(Path(td) / "home")
            env = {"HOME": str(iso_home), "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt), "DASHSCOPE_API_KEY": "sk-unit-dummy"}
            d_boot = VoiceInputDaemon(
                Config(),
                env=env,
                runtime_dir=rt,
                platform=_DummyPlatform(),
                asr_provider=_DummyAsr(),
                refiner=NoopRefiner(),
                tracker=SessionTracker(env=env, runtime_dir=rt, proc_root=fake_proc),
            )
            try:
                self.assertFalse(p_daemon_boot.exists())
            finally:
                d_boot.close()

    def test_audio_proc_sparse_sigh_filter_vs_short_speech_and_whisper(self) -> None:
        acfg = AudioConfig()

        # 1. Scattered ambient clicks / sighs across 28.8s (~0.86s total voiced = 3.0%, max 1.5s window < 0.36s, conc < 0.42) -> blocked!
        scattered_bursts = [(1.0 + idx * 1.5, 0.048, -22.0) for idx in range(18)]
        wav_scattered = _build_test_wav(28.8, scattered_bursts)
        res_scattered = process_wav(wav_scattered, acfg)
        self.assertFalse(res_scattered.passed)
        self.assertEqual(res_scattered.reason, "silence")
        self.assertFalse(res_scattered.all_zero)
        self.assertGreater(res_scattered.voiced_sec, 0.7)

        # 2. Single faint breath barely crossing -50 dBFS (-46 dBFS < -40 dBFS clear-peak threshold) in 20s silence -> blocked!
        wav_faint_sigh = _build_test_wav(20.0, [(10.0, 0.22, -46.0)])
        res_faint = process_wav(wav_faint_sigh, acfg)
        self.assertFalse(res_faint.passed)
        self.assertEqual(res_faint.reason, "silence")
        self.assertFalse(res_faint.all_zero)

        # 3. Short clip (< 8.0s) quick command ("OK" / "好", 0.5s total with 0.18s voiced, and 4.0s with 0.16s voiced) -> 100% passed!
        for dur_s, burst_s in ((0.5, 0.18), (4.0, 0.16)):
            wav_short = _build_test_wav(dur_s, [(0.1, burst_s, -24.0)])
            res_short = process_wav(wav_short, acfg)
            self.assertTrue(res_short.passed, f"short clip {dur_s}s should pass")
            self.assertEqual(res_short.reason, "ok")

        # 4. 28s silent thinking + 0.70s short phrase at end (ratio ~2.5% < 5%, max_win >= 0.45s) -> 100% passed!
        wav_think_phrase = _build_test_wav(28.0, [(26.5, 0.70, -24.0)])
        res_phrase = process_wav(wav_think_phrase, acfg)
        self.assertTrue(res_phrase.passed)
        self.assertEqual(res_phrase.reason, "ok")

        # 5. 28s silent thinking + crisp single word ("好"/"行", 0.25s voiced, plus tiny earlier click: conc > 0.80 >= 0.65 & clear peak) -> 100% passed!
        wav_think_word = _build_test_wav(28.0, [(5.0, 0.03, -30.0), (27.1, 0.25, -18.0)])
        res_word = process_wav(wav_think_word, acfg)
        self.assertTrue(res_word.passed)
        self.assertEqual(res_word.reason, "ok")

        # 6. Continuous quiet whisper (12.0s, -46 dBFS, 25% voiced >= 5%) -> bypasses sparse gate & preserves high whisper gain!
        whisper_bursts = [(idx * 0.5, 0.125, -46.0) for idx in range(24)]
        wav_whisper = _build_test_wav(12.0, whisper_bursts)
        res_whisper = process_wav(wav_whisper, acfg)
        self.assertTrue(res_whisper.passed)
        self.assertEqual(res_whisper.reason, "ok")
        self.assertGreater(res_whisper.gain_db, 20.0)

        # 7. Exact 5% integer boundary (128,020 total samples with 6,401 voiced: 6401*20 = 128020 >= total) -> bypasses sparse gate!
        exact_samples = array.array("h", [0]) * 128_020
        for idx in range(6_401):
            exact_samples[(idx * 20) % 128_020] = 200  # ~ -44.3 dBFS (> -50 dBFS, < -40 dBFS)
        wav_exact5 = pack_pcm16_mono_wav(exact_samples, sample_rate=TARGET_SAMPLE_RATE)
        res_exact5 = process_wav(wav_exact5, acfg)
        self.assertTrue(res_exact5.passed)
        self.assertEqual(res_exact5.reason, "ok")

        # 8. High floor_dbfs (-6 dBFS, target -3 dBFS) where floor+10dB exceeds 0 dBFS: clear focused phrase still passes!
        high_floor_cfg = AudioConfig(floor_dbfs=-6.0, target_dbfs=-3.0)
        wav_high_floor = _build_test_wav(12.0, [(10.5, 0.50, -3.5)])
        res_high_floor = process_wav(wav_high_floor, high_floor_cfg)
        self.assertTrue(res_high_floor.passed)
        self.assertEqual(res_high_floor.reason, "ok")

        # 8b. Near-0dBFS reachable clear-peak boundary: floor_dbfs=-10.05 (floor+10dB = -0.05dBFS < 0dBFS),
        # 10s with 0.2s burst at amplitude 15000 (~ -6.8dBFS < -0.05dBFS) must still be gated by clear-peak check!
        sub_zero_samples = array.array("h", [0]) * (10 * TARGET_SAMPLE_RATE)
        for i in range(int(0.2 * TARGET_SAMPLE_RATE)):
            sub_zero_samples[16000 + i] = 15000 if (i % 2 == 0) else -15000
        wav_sub_zero = pack_pcm16_mono_wav(sub_zero_samples, sample_rate=TARGET_SAMPLE_RATE)
        res_sub_zero = process_wav(wav_sub_zero, AudioConfig(floor_dbfs=-10.05, target_dbfs=-3.0))
        self.assertFalse(res_sub_zero.passed)
        self.assertEqual(res_sub_zero.reason, "silence")

    def test_win_shim_ascii_and_ci_packaging_contracts(self) -> None:
        raw_shim = SHIM_SCRIPT.read_bytes()
        non_ascii = [b for b in raw_shim if b > 0x7F]
        self.assertEqual(non_ascii, [], "win_shim.ps1 must remain strictly 7-bit ASCII")
        shim_txt = raw_shim.decode("ascii")
        self.assertIn("BuildPasteChordInputs", shim_txt)
        self.assertIn("RunHeadlessSelfCheck", shim_txt)
        self.assertIn('$Mode -eq "self-check"', shim_txt)

        with self.assertRaises(ValueError):
            run_win_shim_self_check("bridge")
        with mock.patch("agent_voice_input.platform_wsl.os.path.isfile", return_value=False):
            ok_ps, payload_ps, err_ps = run_win_shim_self_check("self-check")
        self.assertFalse(ok_ps)
        self.assertEqual(payload_ps, {})
        self.assertIn("PowerShell not found", err_ps)

        ci_txt = (_REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        pub_txt = (_REPO_ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
        self.assertIn("win-shim:", ci_txt)
        self.assertIn("windows-latest", ci_txt)
        self.assertIn("-Mode self-check", ci_txt)
        for wf_name, wf_txt in (("ci.yml", ci_txt), ("publish.yml", pub_txt)):
            self.assertIn("agent_voice_input/mac_shim.swift", wf_txt, wf_name)
            self.assertIn("/src/agent_voice_input/mac_shim.swift", wf_txt, wf_name)
            self.assertIn("/CHANGELOG.md", wf_txt, wf_name)
            self.assertIn("/ROADMAP.md", wf_txt, wf_name)


if __name__ == "__main__":
    unittest.main()
