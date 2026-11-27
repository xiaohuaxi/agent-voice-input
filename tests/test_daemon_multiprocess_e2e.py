"""Real multi-process end-to-end tests for ``VoiceInputDaemon`` over Unix ``control.sock`` & ``daemon.lock``.

Spawns independent child Python processes running production ``VoiceInputDaemon.serve_forever()``
against an isolated temporary runtime directory with lightweight deterministic offline fakes for
audio capture, ASR, and Stage 2 refinement. Verifies:
1. Cross-process ``status`` health, ``pid``, and ``build_id`` reporting;
2. Concurrent back-to-back utterances with out-of-order Stage 2 completion preserving FIFO delivery;
3. Cross-process ``cancel`` interrupting in-flight Stage 2 in milliseconds, atomically stashing the
   original WAV with mode ``0600`` under ``failed/``, and leaving subsequent dictations unaffected;
4. Single-instance ``daemon.lock`` mutual exclusion, graceful ``stop``/``shutdown`` lock handoff,
   and clean second daemon startup.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
from pathlib import Path
import shutil
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

from agent_voice_input.asr_providers import BaseAsrProvider  # noqa: E402
from agent_voice_input.audio_proc import list_failed_audios, pack_pcm16_mono_wav  # noqa: E402
from agent_voice_input.config import (  # noqa: E402
    AsrConfig,
    AudioConfig,
    Config,
    HotkeyConfig,
    RefinerConfig,
)
from agent_voice_input.daemon import (  # noqa: E402
    DaemonAlreadyRunningError,
    VoiceInputDaemon,
    compute_package_build_id,
    probe_daemon_status,
    send_control_command,
)
from agent_voice_input.http_transport import CancelToken, HttpTransportError  # noqa: E402
from agent_voice_input.llm_common import AsrContext, AsrResult  # noqa: E402
from agent_voice_input.platform_common import ForegroundWindow, PasteOutcome  # noqa: E402
from agent_voice_input.refiner import BaseRefiner, RefinerResult  # noqa: E402
from agent_voice_input.session_ctx import SessionTracker  # noqa: E402


def _make_voiced_wav(duration_sec: float = 0.36, freq_hz: float = 240.0) -> bytes:
    sr = 16000
    n = int(sr * duration_sec)
    samples = [int(11000 * math.sin(2.0 * math.pi * freq_hz * i / sr)) for i in range(n)]
    return pack_pcm16_mono_wav(samples, sample_rate=sr)


def _touch_marker(sync_dir: Path, name: str, text: str = "1") -> None:
    tmp = sync_dir / f".{name}.tmp.{os.getpid()}"
    dst = sync_dir / name
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, dst)


def _wait_file(path: Path, *, timeout: float = 6.0, poll_sec: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(poll_sec)
    return path.exists()


class _ScriptedPlatform:
    """Offline in-process platform double recording deliveries to a shared JSONL file."""

    def __init__(self, speech_wav: bytes, sync_dir: Path) -> None:
        self._speech_wav = speech_wav
        self._sync_dir = sync_dir
        self._deliveries_path = sync_dir / "deliveries.jsonl"
        self._lock = threading.Lock()
        self._rec_active = False
        self.last_audio_error = ""
        self._lost_cb: Callable[..., Any] | None = None
        self._recovered_cb: Callable[..., Any] | None = None

    def set_recorder_lost_callback(self, cb: Callable[..., Any] | None) -> None:
        self._lost_cb = cb

    def set_recorder_recovered_callback(self, cb: Callable[..., Any] | None) -> None:
        self._recovered_cb = cb

    def start_event_listener(self, on_hotkey_event: Callable[[dict[str, Any]], None]) -> None:
        self._on_event = on_hotkey_event

    def is_allowed_terminal(self, window: ForegroundWindow) -> bool:
        del window
        return True

    def get_foreground_window(self) -> ForegroundWindow:
        return ForegroundWindow(hwnd=101, process="WindowsTerminal.exe", title="codex")

    def resolve_foreground_window(self, hint: ForegroundWindow | None = None) -> ForegroundWindow:
        return hint if (hint and hint.process) else self.get_foreground_window()

    def start_recording(
        self, runtime_dir: Path, *, loopback: bool = False, seq_id: int | None = None
    ) -> None:
        del runtime_dir, loopback, seq_id
        with self._lock:
            self._rec_active = True
            self.last_audio_error = ""

    def stop_recording(self, *, drain: bool = True) -> tuple[bytes, str]:
        del drain
        with self._lock:
            self._rec_active = False
            return self._speech_wav, "scripted_mic"

    def cancel_recording(self) -> None:
        with self._lock:
            self._rec_active = False

    def hud(
        self,
        state: str,
        text: str = "",
        duration_ms: int = 0,
        *,
        layer: str | None = None,
        clear_main: bool | None = None,
        clear_toast: bool | None = None,
        bg_count: int = 0,
    ) -> None:
        del state, text, duration_ms, layer, clear_main, clear_toast, bg_count

    def beep(self, kind: str) -> None:
        del kind

    def deliver_text(
        self,
        text: str,
        seq_id: int,
        target_hwnd: int = 0,
        *,
        origin_window: ForegroundWindow | None = None,
        fallback_clipboard_text: str | None = None,
        suppress_beep: bool | Callable[[], bool] = False,
        is_cancelled: Callable[[], bool] | None = None,
        before_clipboard_write: Callable[[], None] | None = None,
        before_commit: Callable[[], bool] | None = None,
        same_window_verifier: object = None,
    ) -> PasteOutcome:
        del target_hwnd, origin_window, fallback_clipboard_text, suppress_beep
        if is_cancelled is not None and is_cancelled():
            return PasteOutcome(False, False, "cancelled", "")
        if before_clipboard_write is not None:
            before_clipboard_write()
        if is_cancelled is not None and is_cancelled():
            return PasteOutcome(False, False, "cancelled", "")
        if before_commit is not None and not before_commit():
            return PasteOutcome(False, False, "cancelled", "")
        rec = json.dumps({"seq_id": seq_id, "text": text}, ensure_ascii=False) + "\n"
        with self._lock:
            with self._deliveries_path.open("a", encoding="utf-8") as fh:
                fh.write(rec)
                fh.flush()
                os.fsync(fh.fileno())
        return PasteOutcome(True, True, "pasted", "WindowsTerminal.exe", used_clipboard_paste=True)

    def close(self) -> None:
        pass


class _ScriptedAsr(BaseAsrProvider):
    provider_name = "dashscope"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counter = 0

    def transcribe(
        self,
        wav_bytes: bytes,
        context: AsrContext | None = None,
        *,
        is_cancelled: Callable[[], bool] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> AsrResult:
        del wav_bytes, context
        if (cancel_token is not None and cancel_token.is_cancelled()) or (
            is_cancelled is not None and is_cancelled()
        ):
            raise HttpTransportError("ASR cancelled", retry_kind="never")
        with self._lock:
            self._counter += 1
            idx = self._counter
        return AsrResult(text=f"draft-{idx}", provider="dashscope", model="scripted-asr", latency_ms=1)


class _ScriptedRefiner(BaseRefiner):
    """Cross-process barrier-controlled Stage 2 refiner synchronized via files in ``sync_dir``.

    When ``hold_<idx>`` exists in ``sync_dir`` upon entering ``refine()``, writes
    ``stage2_entered_<idx>`` and blocks cooperatively on ``cancel_token`` until either
    ``release_<idx>`` appears or ``cancel_token`` fires.
    """

    provider_name = "deepseek"

    def __init__(self, sync_dir: Path) -> None:
        self._sync_dir = sync_dir
        self._lock = threading.Lock()
        self._call_idx = 0

    def refine(
        self,
        asr: AsrResult,
        *,
        session_key: str = "",
        tracker: Any = None,
        fallback_context: str = "",
        is_cancelled: Callable[[], bool] | None = None,
        cancel_token: CancelToken | None = None,
        mode: str = "agent",
        workspace_cwd: str = "",
    ) -> RefinerResult:
        del session_key, tracker, fallback_context, mode, workspace_cwd
        ev = threading.Event()
        if cancel_token is not None:
            cancel_token.bind_event(ev)
        with self._lock:
            self._call_idx += 1
            my_idx = self._call_idx
        try:
            _touch_marker(self._sync_dir, f"stage2_entered_{my_idx}", asr.text)
            hold_file = self._sync_dir / f"hold_{my_idx}"
            release_file = self._sync_dir / f"release_{my_idx}"
            if hold_file.exists():
                deadline = time.monotonic() + 8.0
                while time.monotonic() < deadline:
                    if ev.is_set() or (is_cancelled is not None and is_cancelled()):
                        break
                    if release_file.exists():
                        break
                    ev.wait(timeout=0.01)

            if (cancel_token is not None and cancel_token.is_cancelled()) or (
                is_cancelled is not None and is_cancelled()
            ):
                _touch_marker(self._sync_dir, f"stage2_cancelled_{my_idx}", asr.text)
                return RefinerResult(asr.text, "deepseek", "scripted-refiner", 1, "cancelled")

            out_text = f"refined-{my_idx}:{asr.text}"
            _touch_marker(self._sync_dir, f"stage2_done_{my_idx}", out_text)
            return RefinerResult(out_text, "deepseek", "scripted-refiner", 2, "")
        finally:
            if cancel_token is not None:
                cancel_token.unbind_event(ev)

    def close(self) -> None:
        pass


def _run_daemon_child(runtime_dir: Path, sync_dir: Path) -> int:
    home_dir = sync_dir / "home"
    home_dir.mkdir(parents=True, exist_ok=True)
    child_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home_dir),
        "XDG_CONFIG_HOME": str(home_dir / ".config"),
        "XDG_CACHE_HOME": str(sync_dir / "cache"),
        "XDG_RUNTIME_DIR": str(runtime_dir),
    }
    cfg = Config(
        hotkey=HotkeyConfig(key="RControl", trigger_mode="tap_toggle", debounce_ms=0),
        audio=AudioConfig(),
        asr=AsrConfig(provider="dashscope"),
        refiner=RefinerConfig(provider="deepseek"),
    )
    tracker = SessionTracker(env=child_env, runtime_dir=runtime_dir, proc_root=sync_dir / "empty_proc")
    daemon = VoiceInputDaemon(
        cfg,
        env=child_env,
        runtime_dir=runtime_dir,
        platform=_ScriptedPlatform(_make_voiced_wav(), sync_dir),
        asr_provider=_ScriptedAsr(),
        refiner=_ScriptedRefiner(sync_dir),
        tracker=tracker,
        paste_settle_sec=0.0,
    )
    try:
        daemon.serve_forever()
        return 0
    except DaemonAlreadyRunningError as exc:
        print(
            json.dumps({"error": "already_running", "pid": exc.pid}),
            file=sys.stderr,
            flush=True,
        )
        return 11
    finally:
        daemon.close()


class DaemonMultiprocessE2ETests(unittest.TestCase):
    def _spawn_daemon(self, runtime_dir: Path, sync_dir: Path) -> subprocess.Popen[str]:
        child_env = {
            **os.environ,
            "PYTHONPATH": str(SRC_ROOT),
            "PYTHONUNBUFFERED": "1",
        }
        return subprocess.Popen(
            [
                sys.executable,
                "-W",
                "error::ResourceWarning",
                str(Path(__file__).resolve()),
                "--child",
                str(runtime_dir),
                str(sync_dir),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=child_env,
        )

    def _wait_daemon_ready(
        self,
        runtime_dir: Path,
        proc: subprocess.Popen[str],
        *,
        timeout: float = 6.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last_resp: dict[str, Any] = {}
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                err = proc.stderr.read() if proc.stderr is not None else ""
                self.fail(f"daemon child exited prematurely rc={proc.returncode}, stderr={err!r}")
            if (runtime_dir / "control.sock").exists():
                resp = probe_daemon_status(runtime_dir, timeout_sec=1.0)
                last_resp = resp
                if resp.get("ok") and resp.get("pid") == proc.pid:
                    return resp
            time.sleep(0.02)
        self.fail(f"daemon did not become ready within {timeout}s; last_resp={last_resp!r}")

    def _wait_daemon_idle(self, runtime_dir: Path, *, timeout: float = 6.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = probe_daemon_status(runtime_dir, timeout_sec=2.0)
            if last.get("ok") and last.get("state") == "idle" and int(last.get("pending_count", 0)) == 0:
                return last
            time.sleep(0.015)
        self.fail(f"daemon did not return to idle within {timeout}s; last={last!r}")

    def _read_deliveries(self, sync_dir: Path) -> list[dict[str, Any]]:
        path = sync_dir / "deliveries.jsonl"
        if not path.is_file():
            return []
        out: list[dict[str, Any]] = []
        for ln in path.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                out.append(json.loads(ln))
        return out

    def _reap_proc(self, proc: subprocess.Popen[str] | None) -> tuple[int | None, str]:
        if proc is None:
            return None, ""
        stderr_txt = ""
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2.0)
            if proc.stderr is not None:
                stderr_txt = proc.stderr.read()
        finally:
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except Exception:
                        pass
        return proc.returncode, stderr_txt

    def test_cross_process_status_fifo_cancel_lock_handoff_and_restart(self) -> None:
        # Use short /tmp path so Unix domain socket sun_path stays well below macOS 104-byte cap.
        base_dir = Path(tempfile.mkdtemp(prefix="avi-mp-", dir="/tmp"))
        os.chmod(base_dir, 0o700)
        rt_dir = base_dir / "rt"
        sync_dir = base_dir / "sync"
        rt_dir.mkdir(mode=0o700)
        sync_dir.mkdir(mode=0o700)

        proc1: subprocess.Popen[str] | None = None
        proc_dup: subprocess.Popen[str] | None = None
        proc2: subprocess.Popen[str] | None = None
        try:
            # --- 1. Spawn daemon #1 and verify cross-process status / pid / build_id ---
            proc1 = self._spawn_daemon(rt_dir, sync_dir)
            st1 = self._wait_daemon_ready(rt_dir, proc1)
            self.assertTrue(st1["ok"])
            self.assertEqual(st1["pid"], proc1.pid)
            self.assertEqual(st1["build_id"], compute_package_build_id())
            self.assertEqual(st1["state"], "idle")
            self.assertEqual(st1["pending_count"], 0)

            # --- 2. Back-to-back concurrent utterances: #1 slow Stage 2, #2 fast Stage 2 -> FIFO ---
            _touch_marker(sync_dir, "hold_1")

            r1_start = send_control_command(rt_dir, {"cmd": "rec_start"}, timeout_sec=4.0)
            self.assertTrue(r1_start.get("ok"), r1_start)
            r1_stop = send_control_command(rt_dir, {"cmd": "rec_stop"}, timeout_sec=4.0)
            self.assertTrue(r1_stop.get("ok"), r1_stop)
            self.assertTrue(_wait_file(sync_dir / "stage2_entered_1"), "utterance #1 never entered Stage 2")

            r2_start = send_control_command(rt_dir, {"cmd": "rec_start"}, timeout_sec=4.0)
            self.assertTrue(r2_start.get("ok"), r2_start)
            r2_stop = send_control_command(rt_dir, {"cmd": "rec_stop"}, timeout_sec=4.0)
            self.assertTrue(r2_stop.get("ok"), r2_stop)
            self.assertTrue(_wait_file(sync_dir / "stage2_done_2"), "utterance #2 never finished Stage 2")

            # #2 finished Stage 2 before #1, yet FIFO commit queue must hold #2 until #1 commits
            self.assertEqual(self._read_deliveries(sync_dir), [])

            _touch_marker(sync_dir, "release_1")
            self._wait_daemon_idle(rt_dir)

            deliv_after_fifo = self._read_deliveries(sync_dir)
            self.assertEqual(
                [d["text"] for d in deliv_after_fifo],
                ["refined-1:draft-1", "refined-2:draft-2"],
            )
            self.assertLess(deliv_after_fifo[0]["seq_id"], deliv_after_fifo[1]["seq_id"])

            # --- 3. Cross-process cancel during in-flight Stage 2 -> fast abort + 0600 failed WAV ---
            _touch_marker(sync_dir, "hold_3")

            r3_start = send_control_command(rt_dir, {"cmd": "rec_start"}, timeout_sec=4.0)
            self.assertTrue(r3_start.get("ok"), r3_start)
            r3_stop = send_control_command(rt_dir, {"cmd": "rec_stop"}, timeout_sec=4.0)
            self.assertTrue(r3_stop.get("ok"), r3_stop)
            self.assertTrue(_wait_file(sync_dir / "stage2_entered_3"), "utterance #3 never entered Stage 2")

            t0 = time.monotonic()
            cancel_resp = send_control_command(rt_dir, {"cmd": "cancel"}, timeout_sec=4.0)
            cancel_ms = (time.monotonic() - t0) * 1000.0
            self.assertTrue(cancel_resp.get("ok"), cancel_resp)
            self.assertEqual(cancel_resp.get("cancelled_ids"), [3], cancel_resp)
            self.assertLess(cancel_ms, 1000.0, f"cancel control RPC unexpectedly slow: {cancel_ms:.1f}ms")
            self.assertTrue(_wait_file(sync_dir / "stage2_cancelled_3"), "utterance #3 Stage 2 did not observe cancel")
            self._wait_daemon_idle(rt_dir)

            # Wait until cancellation result for seq #3 is published and stashed WAV is atomically renamed into failed/.
            stash_deadline = time.monotonic() + 4.0
            failed_wavs: list[Path] = []
            cancel_summary: dict[str, Any] = {}
            while time.monotonic() < stash_deadline:
                st_cancel = probe_daemon_status(rt_dir, timeout_sec=2.0)
                last_res = st_cancel.get("last_result") if isinstance(st_cancel.get("last_result"), dict) else {}
                failed_wavs = list_failed_audios(rt_dir)
                if last_res.get("seq_id") == 3 and failed_wavs:
                    cancel_summary = last_res
                    break
                time.sleep(0.015)

            self.assertEqual(cancel_summary.get("seq_id"), 3, f"seq #3 cancel summary not published: {cancel_summary!r}")
            self.assertEqual(cancel_summary.get("skipped_reason"), "cancelled", cancel_summary)

            # Original WAV atomically stashed under failed/ with strict 0600 permissions
            self.assertEqual(len(failed_wavs), 1, f"expected 1 stashed WAV, got {failed_wavs}")
            wav_mode = stat.S_IMODE(failed_wavs[0].stat().st_mode)
            self.assertEqual(wav_mode, 0o600, f"expected 0600 mode on stashed WAV, got {oct(wav_mode)}")
            self.assertGreater(failed_wavs[0].stat().st_size, 44)

            # Cancelled utterance must NOT have been delivered
            self.assertEqual(len(self._read_deliveries(sync_dir)), 2)

            # Subsequent dictation #4 proceeds normally with zero lingering cancel corruption
            r4_start = send_control_command(rt_dir, {"cmd": "rec_start"}, timeout_sec=4.0)
            self.assertTrue(r4_start.get("ok"), r4_start)
            r4_stop = send_control_command(rt_dir, {"cmd": "rec_stop"}, timeout_sec=4.0)
            self.assertTrue(r4_stop.get("ok"), r4_stop)
            self._wait_daemon_idle(rt_dir)

            deliv_after_cancel = self._read_deliveries(sync_dir)
            self.assertEqual(
                [d["text"] for d in deliv_after_cancel],
                ["refined-1:draft-1", "refined-2:draft-2", "refined-4:draft-4"],
            )

            # --- 4. Single-instance daemon.lock mutex, graceful shutdown handoff, and relaunch ---
            proc_dup = self._spawn_daemon(rt_dir, sync_dir)
            dup_rc = proc_dup.wait(timeout=6.0)
            dup_rc, dup_err = self._reap_proc(proc_dup)
            proc_dup = None
            self.assertEqual(dup_rc, 11, f"expected duplicate daemon rejection rc=11, got {dup_rc}, stderr={dup_err!r}")
            dup_payload = json.loads(dup_err.strip().splitlines()[-1])
            self.assertEqual(dup_payload.get("error"), "already_running")
            self.assertEqual(dup_payload.get("pid"), proc1.pid)

            # Live daemon #1 remains healthy after duplicate attempt
            st1_still = probe_daemon_status(rt_dir, timeout_sec=2.0)
            self.assertTrue(st1_still.get("ok"))
            self.assertEqual(st1_still.get("pid"), proc1.pid)

            # Graceful cross-process shutdown releases daemon.lock (without unlinking it) and cleans socket/pid
            shut_resp = send_control_command(rt_dir, {"cmd": "shutdown"}, timeout_sec=4.0)
            self.assertTrue(shut_resp.get("ok"), shut_resp)
            rc1 = proc1.wait(timeout=6.0)
            rc1, err1 = self._reap_proc(proc1)
            old_pid = proc1.pid
            proc1 = None
            self.assertEqual(rc1, 0, f"daemon #1 shutdown rc={rc1}, stderr={err1!r}")
            self.assertFalse((rt_dir / "control.sock").exists())
            self.assertFalse((rt_dir / "daemon.pid").exists())
            lock_file = rt_dir / "daemon.lock"
            self.assertTrue(lock_file.is_file(), "daemon.lock must remain on disk to prevent inode race")

            # Verify lock is genuinely released via non-blocking flock probe
            lfd = os.open(str(lock_file), os.O_RDWR)
            try:
                fcntl.flock(lfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lfd, fcntl.LOCK_UN)
            finally:
                os.close(lfd)

            # Second daemon cleanly acquires lock handoff and serves control.sock
            proc2 = self._spawn_daemon(rt_dir, sync_dir)
            st2 = self._wait_daemon_ready(rt_dir, proc2)
            self.assertTrue(st2["ok"])
            self.assertEqual(st2["pid"], proc2.pid)
            self.assertNotEqual(st2["pid"], old_pid)
            self.assertEqual(st2["state"], "idle")

            shut2 = send_control_command(rt_dir, {"cmd": "shutdown"}, timeout_sec=4.0)
            self.assertTrue(shut2.get("ok"), shut2)
            rc2 = proc2.wait(timeout=6.0)
            rc2, err2 = self._reap_proc(proc2)
            proc2 = None
            self.assertEqual(rc2, 0, f"daemon #2 shutdown rc={rc2}, stderr={err2!r}")
        finally:
            self._reap_proc(proc_dup)
            self._reap_proc(proc1)
            self._reap_proc(proc2)
            shutil.rmtree(base_dir, ignore_errors=True)

    def test_real_cli_subprocess_operations_lifecycle_e2e(self) -> None:
        """Real ``python3 -m agent_voice_input.cli`` subprocess lifecycle:
        start (detached start_new_session + 0600 daemon.log) -> status -> toggle -> cancel
        -> restart with CLI override & TMUX sanitization -> diagnose --json -> stop.
        """
        base_dir = Path(tempfile.mkdtemp(prefix="avi-cli-", dir="/tmp"))
        os.chmod(base_dir, 0o700)
        home_dir = base_dir / "home"
        cfg_home = home_dir / ".config"
        cache_home = base_dir / "cache"
        rt_dir = base_dir / "rt"
        sync_dir = base_dir / "sync"
        harness_dir = base_dir / "harness"
        for d in (home_dir, cfg_home, cache_home, rt_dir, sync_dir, harness_dir):
            d.mkdir(parents=True, mode=0o700, exist_ok=True)

        cfg_dir = cfg_home / "agent-voice-input"
        cfg_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        cfg_file = cfg_dir / "config.toml"
        cfg_file.write_text(
            "\n".join(
                [
                    "[hotkey]",
                    'key = "RControl"',
                    'trigger_mode = "tap_toggle"',
                    "debounce_ms = 0",
                    "",
                    "[asr]",
                    'provider = "openai-compatible"',
                    'base_url = "http://127.0.0.1:19999/v1"',
                    'model = "whisper-base-cfg"',
                    "",
                    "[refiner]",
                    'provider = "off"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        os.chmod(cfg_file, 0o600)

        # Harness injected strictly into test child processes via PYTHONPATH sitecustomize.py
        # so production cli._cmd_start spawns a real detached `cli daemon` process without
        # touching host PowerShell / mac_shim bridges or host directories.
        sitecustomize_src = '''\
import json, os, sys, warnings
from pathlib import Path

_SYNC = os.environ.get("_AVI_CLI_E2E_SYNC_DIR", "").strip()
if _SYNC:
    if "daemon" in sys.argv:
        warnings.simplefilter("error", ResourceWarning)
    from agent_voice_input import daemon as _daemon_mod
    from agent_voice_input.platform_common import ForegroundWindow, PasteOutcome

    class _CliE2EPlatform:
        def __init__(self, env):
            self.last_audio_error = ""
            self.last_shim_error = ""
            self._rec_active = False
            self._env = dict(env) if env is not None else dict(os.environ)

        def is_shim_ok(self):
            return True

        def set_recorder_lost_callback(self, cb):
            self._lost_cb = cb

        def set_recorder_recovered_callback(self, cb):
            self._recovered_cb = cb

        def start_event_listener(self, on_hotkey_event):
            self._on_event = on_hotkey_event

        def is_allowed_terminal(self, window):
            del window
            return True

        def get_foreground_window(self):
            return ForegroundWindow(hwnd=301, process="WindowsTerminal.exe", title="codex")

        def resolve_foreground_window(self, hint=None):
            return hint if (hint and hint.process) else self.get_foreground_window()

        def start_recording(self, runtime_dir, *, loopback=False, seq_id=None):
            del runtime_dir, loopback, seq_id
            self._rec_active = True

        def stop_recording(self, *, drain=True):
            del drain
            self._rec_active = False
            return b"", "cli_e2e_mic"

        def cancel_recording(self):
            self._rec_active = False

        def hud(self, state, text="", duration_ms=0, *, layer=None, clear_main=None, clear_toast=None, bg_count=0):
            del state, text, duration_ms, layer, clear_main, clear_toast, bg_count

        def beep(self, kind):
            del kind

        def deliver_text(self, text, seq_id, target_hwnd=0, **kwargs):
            del text, seq_id, target_hwnd, kwargs
            return PasteOutcome(True, True, "pasted", "WindowsTerminal.exe", used_clipboard_paste=True)

        def close(self):
            pass

    def _fake_create_platform(cfg, *, env=None):
        del cfg
        eff = dict(env) if env is not None else dict(os.environ)
        rec = {
            "pid": os.getpid(),
            "sid": os.getsid(0),
            "env_TMUX": eff.get("TMUX"),
            "env_TMUX_PANE": eff.get("TMUX_PANE"),
            "env_TMUX_HINT": eff.get("AGENT_VOICE_INPUT_TMUX_HINT"),
            "os_TMUX": os.environ.get("TMUX"),
            "os_TMUX_PANE": os.environ.get("TMUX_PANE"),
            "os_TMUX_HINT": os.environ.get("AGENT_VOICE_INPUT_TMUX_HINT"),
        }
        out_path = Path(_SYNC) / "daemon_starts.jsonl"
        with out_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\\n")
            fh.flush()
            os.fsync(fh.fileno())
        return _CliE2EPlatform(eff)

    _daemon_mod.create_platform = _fake_create_platform

    from agent_voice_input import cli as _cli_mod

    _cli_mod._run_startup_preflight_ping = lambda **_kwargs: []

    if sys.platform == "darwin":
        from agent_voice_input import platform_macos as _mac_mod

        _mac_mod.compile_mac_shim = lambda **kwargs: (None, "")
'''
        (harness_dir / "sitecustomize.py").write_text(sitecustomize_src, encoding="utf-8")

        base_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(home_dir),
            "XDG_CONFIG_HOME": str(cfg_home),
            "XDG_CACHE_HOME": str(cache_home),
            "XDG_RUNTIME_DIR": str(rt_dir),
            "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt_dir),
            "PYTHONPATH": f"{SRC_ROOT}{os.pathsep}{harness_dir}",
            "PYTHONUNBUFFERED": "1",
            "PYTHONWARNINGS": "error::ResourceWarning",
            "_AVI_CLI_E2E_SYNC_DIR": str(sync_dir),
        }

        def _run_cli(
            *cli_args: str,
            extra_env: dict[str, str] | None = None,
            timeout: float = 15.0,
        ) -> subprocess.CompletedProcess[str]:
            run_env = {**base_env, **(extra_env or {})}
            warn_flags = ["-W", "error::ResourceWarning"]
            # `cli._cmd_start` intentionally detaches a `start_new_session=True` daemon child;
            # CPython's `Popen.__del__` emits `ResourceWarning: subprocess <pid> is still running`
            # in the launcher process only. Filter only that launcher warning (removing PYTHONWARNINGS
            # from launcher env so `-W` takes precedence; sitecustomize enforces strict ResourceWarning
            # errors inside the detached `daemon` child) while keeping all other CLI calls strict.
            if cli_args and cli_args[0] in ("start", "restart"):
                run_env.pop("PYTHONWARNINGS", None)
                warn_flags.extend(["-W", "ignore:subprocess :ResourceWarning"])
            return subprocess.run(
                [sys.executable, *warn_flags, "-m", "agent_voice_input.cli", *cli_args],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=run_env,
                timeout=timeout,
                check=False,
            )

        def _read_starts() -> list[dict[str, Any]]:
            p = sync_dir / "daemon_starts.jsonl"
            if not p.is_file():
                return []
            return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]

        def _pid_alive(pid: int) -> bool:
            if pid <= 0:
                return False
            try:
                os.kill(pid, 0)
                return True
            except ProcessLookupError:
                return False
            except OSError:
                return False

        spawned_pids: set[int] = set()
        try:
            # 1. `start`: real start_new_session=True background daemon + 0600 daemon.log + TMUX sanitization
            tmux_start = "/tmp/tmux-e2e-start/default,4242,0"
            p_start = _run_cli(
                "start",
                "--config",
                str(cfg_file),
                extra_env={"TMUX": tmux_start, "TMUX_PANE": "%7"},
            )
            self.assertEqual(
                p_start.returncode,
                0,
                f"cli start failed rc={p_start.returncode} stdout={p_start.stdout!r} stderr={p_start.stderr!r}",
            )
            self.assertIn("Daemon    started (pid=", p_start.stdout)
            self.assertIn("ASR       ", p_start.stdout)
            self.assertIn("Refiner   ", p_start.stdout)
            self.assertNotIn("ResourceWarning", p_start.stderr)

            log_path = rt_dir / "daemon.log"
            self.assertTrue(log_path.is_file(), "daemon.log was not created by cli start")
            self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600)

            starts1 = _read_starts()
            self.assertEqual(len(starts1), 1, f"expected 1 daemon start record, got {starts1!r}")
            pid1 = int(starts1[0]["pid"])
            spawned_pids.add(pid1)
            self.assertEqual(starts1[0]["sid"], pid1, "daemon must run in its own session (start_new_session=True)")
            self.assertIsNone(starts1[0]["os_TMUX"])
            self.assertIsNone(starts1[0]["os_TMUX_PANE"])
            self.assertEqual(starts1[0]["os_TMUX_HINT"], tmux_start)
            self.assertIsNone(starts1[0]["env_TMUX"])
            self.assertIsNone(starts1[0]["env_TMUX_PANE"])
            self.assertEqual(starts1[0]["env_TMUX_HINT"], tmux_start)

            # 2. `status --json` & plain human-readable `status` match pid1 and configured model
            p_st1 = _run_cli("status", "--json", "--config", str(cfg_file))
            self.assertEqual(p_st1.returncode, 0, p_st1.stderr)
            st1 = json.loads(p_st1.stdout)
            self.assertTrue(st1["ok"])
            self.assertEqual(st1["pid"], pid1)
            self.assertEqual(st1["state"], "idle")
            self.assertEqual(st1["pipeline"]["asr"]["model"], "whisper-base-cfg")
            self.assertFalse(st1["code_drift"])

            p_st1_human = _run_cli("status", "--config", str(cfg_file))
            self.assertEqual(p_st1_human.returncode, 0, p_st1_human.stderr)
            self.assertIn(f"pid={pid1}", p_st1_human.stdout)
            self.assertIn("whisper-base-cfg", p_st1_human.stdout)

            # 3. `toggle` (start recording) -> `status` (recording) -> `cancel` (abort recording cleanly)
            p_tog = _run_cli("toggle", "--json", "--config", str(cfg_file))
            self.assertEqual(p_tog.returncode, 0, f"toggle stderr={p_tog.stderr!r}")
            tog_obj = json.loads(p_tog.stdout)
            self.assertTrue(tog_obj["ok"])
            self.assertEqual(tog_obj["state"], "recording")
            self.assertEqual(tog_obj["seq_id"], 1)

            p_st_rec = _run_cli("status", "--json", "--config", str(cfg_file))
            self.assertEqual(p_st_rec.returncode, 0, p_st_rec.stderr)
            self.assertEqual(json.loads(p_st_rec.stdout)["state"], "recording")

            p_can = _run_cli("cancel", "--json", "--config", str(cfg_file))
            self.assertEqual(p_can.returncode, 0, f"cancel stderr={p_can.stderr!r}")
            can_obj = json.loads(p_can.stdout)
            self.assertTrue(can_obj["ok"])
            self.assertEqual(can_obj.get("cancelled_recording"), 1)
            self.assertEqual(can_obj.get("cancelled_ids"), [1])
            self.assertEqual(can_obj["state"], "idle")

            # 4. `restart` with CLI override + new TMUX env: stops pid1, waits lock handoff, spawns pid2
            tmux_restart = "/tmp/tmux-e2e-restart/default,9999,1"
            p_rst = _run_cli(
                "restart",
                "--config",
                str(cfg_file),
                "--asr-model",
                "whisper-override-v2",
                extra_env={"TMUX": tmux_restart, "TMUX_PANE": "%12"},
            )
            self.assertEqual(
                p_rst.returncode,
                0,
                f"cli restart failed rc={p_rst.returncode} stdout={p_rst.stdout!r} stderr={p_rst.stderr!r}",
            )
            self.assertIn("Daemon    restarted (pid=", p_rst.stdout)
            self.assertIn(f"pid={pid1} -> ", p_rst.stdout)
            self.assertIn("ASR       ", p_rst.stdout)
            self.assertNotIn("ResourceWarning", p_rst.stderr)

            starts2 = _read_starts()
            self.assertEqual(len(starts2), 2, f"expected 2 daemon starts after restart, got {starts2!r}")
            pid2 = int(starts2[1]["pid"])
            spawned_pids.add(pid2)
            self.assertNotEqual(pid2, pid1)
            self.assertEqual(starts2[1]["sid"], pid2)
            self.assertIsNone(starts2[1]["os_TMUX"])
            self.assertIsNone(starts2[1]["os_TMUX_PANE"])
            self.assertEqual(starts2[1]["os_TMUX_HINT"], tmux_restart)
            self.assertFalse(_pid_alive(pid1), f"previous daemon pid1={pid1} still alive after restart")

            p_st2 = _run_cli("status", "--json", "--config", str(cfg_file))
            self.assertEqual(p_st2.returncode, 0, p_st2.stderr)
            st2 = json.loads(p_st2.stdout)
            self.assertTrue(st2["ok"])
            self.assertEqual(st2["pid"], pid2)
            self.assertEqual(st2["state"], "idle")
            self.assertEqual(st2["pipeline"]["asr"]["model"], "whisper-override-v2")

            # 5. `diagnose --json` against the live restarted daemon
            p_diag = _run_cli("diagnose", "--json", "--config", str(cfg_file))
            self.assertEqual(p_diag.returncode, 0, f"diagnose stderr={p_diag.stderr!r}")
            diag_obj = json.loads(p_diag.stdout)
            self.assertTrue(diag_obj["daemon"]["online"])
            self.assertEqual(diag_obj["daemon"]["pid"], pid2)
            self.assertEqual(diag_obj["daemon"]["state"], "idle")
            self.assertFalse(diag_obj["daemon"]["code_drift"])
            self.assertEqual(diag_obj["pipeline"]["asr"]["model"], "whisper-override-v2")
            self.assertEqual(diag_obj["storage"]["failed_audio_count"], 0)

            # 6. `stop`: clean daemon exit, socket/pid removed, daemon.lock unlocked, 0600 log, no ResourceWarning
            p_stop = _run_cli("stop", "--config", str(cfg_file))
            self.assertEqual(p_stop.returncode, 0, f"stop stderr={p_stop.stderr!r}")
            self.assertIn("stopped", p_stop.stdout)
            self.assertNotIn("ResourceWarning", p_stop.stderr)

            wait_gone = time.monotonic() + 4.0
            while time.monotonic() < wait_gone and _pid_alive(pid2):
                time.sleep(0.02)
            self.assertFalse(_pid_alive(pid2), f"daemon pid2={pid2} remained alive after cli stop")
            self.assertFalse((rt_dir / "control.sock").exists())
            self.assertFalse((rt_dir / "daemon.pid").exists())

            lock_file = rt_dir / "daemon.lock"
            self.assertTrue(lock_file.is_file())
            lfd = os.open(str(lock_file), os.O_RDWR)
            try:
                fcntl.flock(lfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lfd, fcntl.LOCK_UN)
            finally:
                os.close(lfd)

            self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600)
            log_txt = log_path.read_text(encoding="utf-8", errors="replace")
            self.assertNotIn("ResourceWarning", log_txt)
            self.assertNotIn("Traceback", log_txt)
        finally:
            # Best-effort guarantee that no detached daemon child survives even on test failure.
            pid_file = rt_dir / "daemon.pid"
            if pid_file.is_file():
                try:
                    spawned_pids.add(int(pid_file.read_text(encoding="utf-8").strip()))
                except (OSError, ValueError):
                    pass
            for rec in _read_starts():
                try:
                    spawned_pids.add(int(rec.get("pid") or 0))
                except (TypeError, ValueError):
                    pass
            for pid in sorted(p for p in spawned_pids if p > 0):
                if _pid_alive(pid):
                    try:
                        os.kill(pid, 15)
                    except OSError:
                        pass
            dl = time.monotonic() + 2.0
            while time.monotonic() < dl and any(_pid_alive(p) for p in spawned_pids if p > 0):
                time.sleep(0.02)
            for pid in sorted(p for p in spawned_pids if p > 0):
                if _pid_alive(pid):
                    try:
                        os.kill(pid, 9)
                    except OSError:
                        pass
            shutil.rmtree(base_dir, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--child":
        raise SystemExit(_run_daemon_child(Path(sys.argv[2]), Path(sys.argv[3])))
    unittest.main()
