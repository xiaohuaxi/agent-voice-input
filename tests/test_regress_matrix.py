"""Offline unit tests for .agents/skills/voice-eval/scripts/regress_matrix.py."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import struct
import sys
import tempfile
import threading
import unittest
from unittest import mock
import wave

from agent_voice_input.config import Config
from agent_voice_input.daemon import compute_package_build_id


_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT_PATH = _REPO_ROOT / ".agents" / "skills" / "voice-eval" / "scripts" / "regress_matrix.py"


def _load_regress_matrix_module():
    spec = importlib.util.spec_from_file_location("avi_regress_matrix_under_test", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rm = _load_regress_matrix_module()


def _write_voiced_wav(path: Path, duration_sec: float = 0.6, sample_rate: int = 16000) -> Path:
    """Write a non-silent 16 kHz mono PCM WAV that passes audio_proc.process_wav."""
    import math

    n_frames = int(sample_rate * duration_sec)
    frames = bytearray()
    for i in range(n_frames):
        val = int(12000 * math.sin(2.0 * math.pi * 440.0 * i / sample_rate))
        frames.extend(struct.pack("<h", val))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(frames))
    return path


class _StubControlSocketServer:
    """Minimal local AF_UNIX control.sock JSONL stub for Layer B testing."""

    def __init__(self, rt_dir: Path, handler):
        self.rt_dir = rt_dir
        self.sock_path = rt_dir / "control.sock"
        self._handler = handler
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(self.sock_path))
        self._sock.listen(8)
        self._sock.settimeout(0.2)
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                conn.settimeout(2.0)
                buf = b""
                while b"\n" not in buf and len(buf) < 65536:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                if not buf.strip():
                    continue
                req = json.loads(buf.decode("utf-8"))
                resp = self._handler(req)
                conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2.0)
        try:
            self.sock_path.unlink(missing_ok=True)
        except OSError:
            pass


def _create_sqlite(db_path: Path, statements: list[tuple[str, tuple]]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        for sql, params in statements:
            conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


class TestRegressMatrixLayerB(unittest.TestCase):
    def _run_layer_b_with_stub(
        self,
        selftest_resp: dict,
        *,
        deliver_live: bool = True,
        delay_sec: float = 0.0,
        skip_selftest_wav: bool = False,
        events_out: list[str] | None = None,
    ) -> list[dict]:
        build_id = compute_package_build_id()
        with rm._make_short_tempdir(prefix="avi-t-rm-") as raw_td:
            td = Path(os.path.realpath(raw_td))
            rt_dir = td / "rt"
            rt_dir.mkdir(mode=0o700)
            wav_path = _write_voiced_wav(td / "probe.wav")

            def _handler(req: dict) -> dict:
                cmd = req.get("cmd")
                if events_out is not None:
                    if cmd == "probe_route":
                        tag = "probe_live" if req.get("use_live_window") else f"probe_{req.get('process', '')}"
                        events_out.append(tag)
                    else:
                        events_out.append(str(cmd))
                if cmd == "status":
                    return {
                        "ok": True,
                        "version": rm.__version__,
                        "build_id": build_id,
                        "state": "idle",
                        "shim_ok": True,
                        "asr_provider": "dummy",
                        "refiner_provider": "off",
                    }
                if cmd == "probe_route":
                    if req.get("use_live_window"):
                        return {
                            "ok": True,
                            "window_category": "coding_gui",
                            "mode": "agent",
                            "target_app_hint": "",
                            "hotwords_scope": "coding",
                            "session_key_masked": "agy:12345678",
                            "matched_by": "ide_db_title",
                            "remote_vetoed": False,
                            "workspace_name": "demo",
                            "ctx_chars": 100,
                            "ctx_fallback": "",
                            "sync_ms": 1,
                        }
                    if "[@remote]" in str(req.get("title") or ""):
                        return {
                            "ok": True,
                            "window_category": "terminal",
                            "mode": "agent",
                            "remote_vetoed": True,
                            "session_key_masked": "",
                            "ctx_chars": 0,
                        }
                    return {
                        "ok": True,
                        "window_category": "general_gui",
                        "mode": "general",
                        "target_app_hint": "im_chat",
                        "remote_vetoed": False,
                        "session_key_masked": "",
                        "ctx_chars": 0,
                    }
                if cmd == "selftest_wav":
                    return dict(selftest_resp)
                return {"ok": False, "error": f"unexpected cmd: {cmd}"}

            srv = _StubControlSocketServer(rt_dir, _handler)
            srv.start()
            try:
                fake_sleep = (
                    (lambda sec: events_out.append(f"sleep:{sec:.2f}"))
                    if events_out is not None
                    else (lambda sec: None)
                )
                with mock.patch.object(rm.time, "sleep", side_effect=fake_sleep):
                    return rm.run_layer_b_live_daemon(
                        Config(),
                        {},
                        {},
                        lambda s: str(s),
                        runtime_dir_arg=str(rt_dir),
                        wav_arg=str(wav_path),
                        allow_drift=False,
                        skip_selftest_wav=skip_selftest_wav,
                        deliver_live=deliver_live,
                        delay_sec=delay_sec,
                        show_text=False,
                    )
            finally:
                srv.close()

    def test_deliver_live_rejects_clipboard_only_fallback(self) -> None:
        checks = self._run_layer_b_with_stub(
            {
                "ok": True,
                "pasted": False,
                "clipboard": True,
                "paste_reason": "focus_changed",
                "delivery_status": "clipboard",
                "summary": {"asr_ms": 50, "refine_ms": 30, "final_chars": 18},
                "routing": {"mode": "agent", "session_key_masked": "agy:12345678"},
            },
            deliver_live=True,
        )
        b3 = next(c for c in checks if c["id"] == "B3_deliver_live")
        self.assertEqual(b3["status"], "FAIL")
        self.assertIn("focus_changed", b3["detail"])
        self.assertIn("clipboard", b3["detail"])

    def test_deliver_live_rejects_zero_final_chars(self) -> None:
        checks = self._run_layer_b_with_stub(
            {
                "ok": True,
                "pasted": True,
                "clipboard": False,
                "paste_reason": "pasted",
                "delivery_status": "pasted",
                "summary": {"asr_ms": 50, "refine_ms": 0, "final_chars": 0},
                "routing": {"mode": "general"},
            },
            deliver_live=True,
        )
        b3 = next(c for c in checks if c["id"] == "B3_deliver_live")
        self.assertEqual(b3["status"], "FAIL")
        self.assertIn("final_chars=0", b3["detail"])

    def test_deliver_live_passes_only_when_pasted_and_nonempty(self) -> None:
        checks = self._run_layer_b_with_stub(
            {
                "ok": True,
                "pasted": True,
                "clipboard": False,
                "paste_reason": "pasted",
                "delivery_status": "pasted",
                "summary": {"asr_ms": 40, "refine_ms": 20, "final_chars": 14},
                "routing": {"mode": "agent", "session_key_masked": "codex:abcd1234"},
            },
            deliver_live=True,
        )
        b3 = next(c for c in checks if c["id"] == "B3_deliver_live")
        self.assertEqual(b3["status"], "PASS")
        self.assertTrue(b3["metrics"]["pasted"])
        self.assertEqual(b3["metrics"]["final_chars"], 14)

    def test_probe_live_window_runs_after_delay_sec(self) -> None:
        events: list[str] = []
        checks = self._run_layer_b_with_stub(
            {
                "ok": True,
                "pasted": True,
                "clipboard": False,
                "paste_reason": "pasted",
                "delivery_status": "pasted",
                "final_chars": 9,
                "summary": {"asr_ms": 10, "refine_ms": 10, "final_chars": 9},
                "routing": {"mode": "agent"},
            },
            deliver_live=True,
            delay_sec=0.25,
            events_out=events,
        )
        self.assertTrue(all(c["status"] == "PASS" for c in checks))
        sleep_idx = events.index("sleep:0.25")
        probe_live_idx = events.index("probe_live")
        selftest_idx = events.index("selftest_wav")
        self.assertLess(sleep_idx, probe_live_idx)
        self.assertLess(probe_live_idx, selftest_idx)

    def test_resolve_live_wav_respects_xdg_cache_home(self) -> None:
        with tempfile.TemporaryDirectory(prefix="avi-t-xdg-") as td:
            xdg_cache = Path(td) / "custom-xdg-cache"
            fake_wav = Path(td) / "cached.wav"
            fake_wav.write_bytes(b"RIFF")
            captured: dict[str, Path] = {}

            def _fake_prepare(spec, *, cache_dir, refresh_tts, env, audio_cfg):
                del spec, refresh_tts, env, audio_cfg
                captured["cache_dir"] = Path(cache_dir)
                return {"wav_path": str(fake_wav), "duration_sec": 1.25}

            fake_e2e = mock.Mock()
            fake_e2e._prepare_cached_tts_item.side_effect = _fake_prepare
            with mock.patch.object(rm, "_e2e_mod", fake_e2e):
                wav_p, dur, used_tts = rm._resolve_live_wav(
                    "",
                    cfg=Config(),
                    clean_env={"HOME": str(Path(td) / "home"), "XDG_CACHE_HOME": str(xdg_cache)},
                )
            self.assertEqual(wav_p, fake_wav)
            self.assertEqual(dur, 1.25)
            self.assertTrue(used_tts)
            self.assertEqual(
                captured["cache_dir"],
                xdg_cache / "agent-voice-input" / "eval-wavs",
            )


class TestRegressMatrixLayerC(unittest.TestCase):
    def test_top5_prefers_non_empty_root_session_over_newer_small_empty(self) -> None:
        with tempfile.TemporaryDirectory(prefix="avi-t-lc1-") as raw_td:
            home = Path(raw_td)
            sess_dir = home / ".codex" / "sessions" / "2026" / "11" / "23"
            older_rich = rm._write_codex_rollout(
                sess_dir / "rollout-2026-11-23T08-00-00-aaaa0001-0000-4000-8000-000000000001.jsonl",
                "aaaa0001-0000-4000-8000-000000000001",
                str(home / "repo"),
                [("user", "hello world"), ("assistant", "hi there")],
            )
            newer_empty = rm._write_codex_rollout(
                sess_dir / "rollout-2026-11-23T09-00-00-aaaa0002-0000-4000-8000-000000000002.jsonl",
                "aaaa0002-0000-4000-8000-000000000002",
                str(home / "repo"),
                [],
            )
            os.utime(older_rich, (1700000100, 1700000100))
            os.utime(newer_empty, (1700000200, 1700000200))

            checks = rm.run_layer_c_real_clis({"HOME": str(home)}, lambda s: str(s))
            c1 = next(c for c in checks if c["id"] == "C1_real_codex_cli")
            self.assertEqual(c1["status"], "PASS")
            self.assertEqual(c1["metrics"]["turns"], 2)
            self.assertEqual(c1["metrics"]["candidates_checked"], 2)

    def test_large_zero_turn_file_fails_as_schema_drift(self) -> None:
        with tempfile.TemporaryDirectory(prefix="avi-t-lc2-") as raw_td:
            home = Path(raw_td)
            # Codex >= 4096B with valid session_meta + unparsed unknown event schema
            codex_p = (
                home
                / ".codex"
                / "sessions"
                / "2026"
                / "11"
                / "23"
                / "rollout-2026-11-23T09-00-00-bbbb0001-0000-4000-8000-000000000001.jsonl"
            )
            codex_p.parent.mkdir(parents=True, exist_ok=True)
            codex_p.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": "bbbb0001-0000-4000-8000-000000000001", "cwd": str(home), "source": "cli"},
                    }
                )
                + "\n"
                + json.dumps({"type": "future_unknown_v99", "blob": "Z" * 5000})
                + "\n",
                encoding="utf-8",
            )

            # Claude >= 4096B with root meta + unknown schema lines (0 turns)
            claude_p = home / ".claude" / "projects" / "demo-proj" / "cccc0001-0000-4000-8000-000000000001.jsonl"
            claude_p.parent.mkdir(parents=True, exist_ok=True)
            claude_p.write_text(
                json.dumps(
                    {
                        "type": "system",
                        "isSidechain": False,
                        "sessionId": "cccc0001-0000-4000-8000-000000000001",
                        "cwd": str(home),
                        "blob": "Y" * 5000,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            # AGY >= 4096B with SYSTEM_INFO only (0 turns)
            agy_p = (
                home
                / ".gemini"
                / "antigravity-cli"
                / "brain"
                / "dddd0001-0000-4000-8000-000000000001"
                / ".system_generated"
                / "logs"
                / "transcript_full.jsonl"
            )
            agy_p.parent.mkdir(parents=True, exist_ok=True)
            agy_p.write_text(
                json.dumps(
                    {"source": "SYSTEM", "type": "SYSTEM_INFO", "cwd": str(home), "content": "S" * 5000}
                )
                + "\n",
                encoding="utf-8",
            )

            checks = rm.run_layer_c_real_clis({"HOME": str(home)}, lambda s: str(s))
            by_id = {c["id"]: c for c in checks}
            for cid in ("C1_real_codex_cli", "C2_real_claude_cli", "C3_real_agy_cli"):
                self.assertEqual(by_id[cid]["status"], "FAIL", msg=f"{cid}: {by_id[cid]}")
                self.assertIn("schema drift", by_id[cid]["detail"])

    def test_small_empty_sessions_mark_skip_not_pass(self) -> None:
        with tempfile.TemporaryDirectory(prefix="avi-t-lc3-") as raw_td:
            home = Path(raw_td)
            # Codex small (<4096B) 0-turn session
            rm._write_codex_rollout(
                home
                / ".codex"
                / "sessions"
                / "2026"
                / "11"
                / "23"
                / "rollout-2026-11-23T09-00-00-eeee0001-0000-4000-8000-000000000001.jsonl",
                "eeee0001-0000-4000-8000-000000000001",
                str(home / "repo"),
                [],
            )
            # Claude small (<4096B) 0-turn root session
            claude_p = home / ".claude" / "projects" / "demo-proj" / "eeee0002-0000-4000-8000-000000000002.jsonl"
            claude_p.parent.mkdir(parents=True, exist_ok=True)
            claude_p.write_text(
                json.dumps(
                    {
                        "type": "system",
                        "isSidechain": False,
                        "sessionId": "eeee0002-0000-4000-8000-000000000002",
                        "cwd": str(home / "repo"),
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            # AGY small (<4096B) 0-turn root session
            rm._write_agy_transcript(
                home
                / ".gemini"
                / "antigravity-cli"
                / "brain"
                / "eeee0003-0000-4000-8000-000000000003"
                / ".system_generated"
                / "logs"
                / "transcript_full.jsonl",
                str(home / "repo"),
                [],
            )

            checks = rm.run_layer_c_real_clis({"HOME": str(home)}, lambda s: str(s))
            by_id = {c["id"]: c for c in checks}
            for cid in ("C1_real_codex_cli", "C2_real_claude_cli", "C3_real_agy_cli"):
                self.assertEqual(by_id[cid]["status"], "SKIP", msg=f"{cid}: {by_id[cid]}")

    def test_c4_uses_iter_agy_ide_roots_and_c5_falls_back_to_state_5_sqlite(self) -> None:
        with tempfile.TemporaryDirectory(prefix="avi-t-lc45-") as raw_td:
            home = Path(raw_td)
            # Place AGY DB in the second root (`antigravity-ide`) only
            _create_sqlite(
                home / ".gemini" / "antigravity-ide" / "conversation_summaries.db",
                [
                    (
                        "CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT, workspace_uris TEXT)",
                        (),
                    ),
                    (
                        "INSERT INTO conversation_summaries VALUES (?, ?, ?)",
                        ("cid-1", "Test Title", "[]"),
                    ),
                ],
            )
            # Omit codex.db, provide state_5.sqlite fallback
            _create_sqlite(
                home / ".codex" / "state_5.sqlite",
                [
                    (
                        "CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, cwd TEXT, originator TEXT, archived INTEGER)",
                        (),
                    ),
                    (
                        "INSERT INTO threads VALUES (?, ?, ?, ?, ?)",
                        ("tid-1", "Local Task", str(home / "repo"), "Codex Desktop", 0),
                    ),
                    (
                        "INSERT INTO threads VALUES (?, ?, ?, ?, ?)",
                        ("tid-2", "Archived Task", str(home / "repo"), "Codex Desktop", 1),
                    ),
                ],
            )

            checks = rm.run_layer_c_real_clis({"HOME": str(home)}, lambda s: str(s))
            by_id = {c["id"]: c for c in checks}
            self.assertEqual(by_id["C4_real_agy2_db"]["status"], "PASS")
            self.assertEqual(by_id["C4_real_agy2_db"]["metrics"]["db_root"], "antigravity-ide")
            self.assertEqual(by_id["C5_real_codex_desktop_db"]["status"], "PASS")
            self.assertEqual(by_id["C5_real_codex_desktop_db"]["metrics"]["db"], "state_5.sqlite")
            self.assertEqual(by_id["C5_real_codex_desktop_db"]["metrics"]["rows"], 2)
            self.assertEqual(by_id["C5_real_codex_desktop_db"]["metrics"]["local_repo_rows"], 1)

    def test_c5_corrupt_or_incomplete_codex_db_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="avi-t-lc5fc-") as raw_td:
            home = Path(raw_td)
            _create_sqlite(
                home / ".codex" / "sqlite" / "codex.db",
                [("CREATE TABLE local_thread_catalog (thread_id TEXT PRIMARY KEY)", ())],
            )
            _create_sqlite(
                home / ".codex" / "state_5.sqlite",
                [
                    (
                        "CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, cwd TEXT, originator TEXT, archived INTEGER)",
                        (),
                    ),
                ],
            )
            checks = rm.run_layer_c_real_clis({"HOME": str(home)}, lambda s: str(s))
            c5 = next(c for c in checks if c["id"] == "C5_real_codex_desktop_db")
            self.assertEqual(c5["status"], "FAIL")
            self.assertIn("missing required columns", c5["detail"])

    def test_newer_large_zero_turn_drift_not_masked_by_older_valid_session(self) -> None:
        with tempfile.TemporaryDirectory(prefix="avi-t-lc-drift-mask-") as raw_td:
            home = Path(raw_td)
            sess_dir = home / ".codex" / "sessions" / "2026" / "11" / "23"
            older_ok = rm._write_codex_rollout(
                sess_dir / "rollout-2026-11-23T08-00-00-aaaa1001-0000-4000-8000-000000000001.jsonl",
                "aaaa1001-0000-4000-8000-000000000001",
                str(home / "repo"),
                [("user", "old valid turn"), ("assistant", "old valid reply")],
            )
            newer_drift = sess_dir / "rollout-2026-11-23T09-00-00-aaaa1002-0000-4000-8000-000000000002.jsonl"
            newer_drift.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": "aaaa1002-0000-4000-8000-000000000002", "cwd": str(home), "source": "cli"},
                    }
                )
                + "\n"
                + json.dumps({"type": "unknown_v99_event", "blob": "X" * 5000})
                + "\n",
                encoding="utf-8",
            )
            os.utime(older_ok, (1700000100, 1700000100))
            os.utime(newer_drift, (1700000200, 1700000200))

            checks = rm.run_layer_c_real_clis({"HOME": str(home)}, lambda s: str(s))
            c1 = next(c for c in checks if c["id"] == "C1_real_codex_cli")
            self.assertEqual(c1["status"], "FAIL")
            self.assertIn("schema drift", c1["detail"])
            self.assertIn("candidate#1", c1["detail"])
            self.assertIn("older candidate#2", c1["detail"])
            self.assertEqual(c1["metrics"]["drift_candidate"], 1)
            self.assertEqual(c1["metrics"]["older_ok_candidate"], 2)
            self.assertEqual(c1["metrics"]["older_ok_turns"], 2)

            # Even when candidate#1 is a small empty session and candidate#2 is >=4KB 0-turn drift,
            # older candidate#3 with turns>0 MUST NOT turn the check into PASS!
            newest_small_empty = rm._write_codex_rollout(
                sess_dir / "rollout-2026-11-23T10-00-00-aaaa1003-0000-4000-8000-000000000003.jsonl",
                "aaaa1003-0000-4000-8000-000000000003",
                str(home / "repo"),
                [],
            )
            os.utime(newest_small_empty, (1700000300, 1700000300))
            checks2 = rm.run_layer_c_real_clis({"HOME": str(home)}, lambda s: str(s))
            c1_b = next(c for c in checks2 if c["id"] == "C1_real_codex_cli")
            self.assertEqual(c1_b["status"], "FAIL")
            self.assertIn("schema drift", c1_b["detail"])
            self.assertEqual(c1_b["metrics"]["drift_candidate"], 2)
            self.assertEqual(c1_b["metrics"]["older_ok_candidate"], 3)

    def test_newer_parse_exception_not_masked_by_older_valid_session(self) -> None:
        with tempfile.TemporaryDirectory(prefix="avi-t-lc-exc-mask-") as raw_td:
            home = Path(raw_td)
            sess_dir = home / ".codex" / "sessions" / "2026" / "11" / "23"
            older_ok = rm._write_codex_rollout(
                sess_dir / "rollout-2026-11-23T08-00-00-aaaa2001-0000-4000-8000-000000000001.jsonl",
                "aaaa2001-0000-4000-8000-000000000001",
                str(home / "repo"),
                [("user", "old turn"), ("assistant", "old reply")],
            )
            newer_boom = rm._write_codex_rollout(
                sess_dir / "rollout-2026-11-23T09-00-00-aaaa2002-0000-4000-8000-000000000002.jsonl",
                "aaaa2002-0000-4000-8000-000000000002",
                str(home / "repo"),
                [("user", "new turn")],
            )
            os.utime(older_ok, (1700000100, 1700000100))
            os.utime(newer_boom, (1700000200, 1700000200))

            orig_clean = rm._clean_jsonl_file

            def _boom_on_newer(cli: str, path_str: str, **kw):
                if path_str == str(newer_boom):
                    raise ValueError("corrupted upstream frame")
                return orig_clean(cli, path_str, **kw)

            with mock.patch.object(rm, "_clean_jsonl_file", side_effect=_boom_on_newer):
                checks = rm.run_layer_c_real_clis({"HOME": str(home)}, lambda s: str(s))
            c1 = next(c for c in checks if c["id"] == "C1_real_codex_cli")
            self.assertEqual(c1["status"], "FAIL")
            self.assertIn("corrupted upstream frame", c1["detail"])
            self.assertIn("older candidate#2", c1["detail"])
            self.assertEqual(c1["metrics"]["error_candidate"], 1)
            self.assertEqual(c1["metrics"]["older_ok_candidate"], 2)

    def test_c4_validates_all_existing_agy_db_roots(self) -> None:
        with tempfile.TemporaryDirectory(prefix="avi-t-lc4-all-") as raw_td:
            home = Path(raw_td)
            db1 = home / ".gemini" / "antigravity" / "conversation_summaries.db"
            db2 = home / ".gemini" / "antigravity-ide" / "conversation_summaries.db"
            _create_sqlite(
                db1,
                [
                    ("CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT, workspace_uris TEXT)", ()),
                    ("INSERT INTO conversation_summaries VALUES (?, ?, ?)", ("cid-1", "Root 1 Title", "[]")),
                ],
            )
            _create_sqlite(
                db2,
                [
                    ("CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT, workspace_uris TEXT)", ()),
                    ("INSERT INTO conversation_summaries VALUES (?, ?, ?)", ("cid-2", "Root 2 Title", "[]")),
                    ("INSERT INTO conversation_summaries VALUES (?, ?, ?)", ("cid-3", "Root 2 Second", "[]")),
                ],
            )

            # 1) Both roots healthy -> PASS with both roots reported
            checks_ok = rm.run_layer_c_real_clis({"HOME": str(home)}, lambda s: str(s))
            c4_ok = next(c for c in checks_ok if c["id"] == "C4_real_agy2_db")
            self.assertEqual(c4_ok["status"], "PASS")
            self.assertEqual(c4_ok["metrics"]["roots_checked"], 2)
            self.assertEqual(c4_ok["metrics"]["db_roots"], ["antigravity", "antigravity-ide"])
            self.assertEqual(c4_ok["metrics"]["rows"], 3)

            # 2) First root healthy, second root missing required 'title' column -> FAIL!
            db2.unlink()
            _create_sqlite(db2, [("CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY)", ())])
            checks_bad_cols = rm.run_layer_c_real_clis({"HOME": str(home)}, lambda s: str(s))
            c4_bad_cols = next(c for c in checks_bad_cols if c["id"] == "C4_real_agy2_db")
            self.assertEqual(c4_bad_cols["status"], "FAIL")
            self.assertIn("antigravity-ide", c4_bad_cols["detail"])
            self.assertIn("missing required columns", c4_bad_cols["detail"])
            self.assertEqual(c4_bad_cols["metrics"]["roots_checked"], 2)

            # 3) First root healthy, second root corrupt / unreadable -> FAIL!
            db2.write_bytes(b"corrupt secondary sqlite file")
            checks_corrupt = rm.run_layer_c_real_clis({"HOME": str(home)}, lambda s: str(s))
            c4_corrupt = next(c for c in checks_corrupt if c["id"] == "C4_real_agy2_db")
            self.assertEqual(c4_corrupt["status"], "FAIL")
            self.assertIn("antigravity-ide", c4_corrupt["detail"])

            # 4) First root healthy, second root is symlink -> FAIL!
            ext_db = Path(raw_td) / "ext_c4.db"
            _create_sqlite(
                ext_db,
                [("CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT)", ())],
            )
            db2.unlink()
            os.symlink(ext_db, db2)
            checks_sym = rm.run_layer_c_real_clis({"HOME": str(home)}, lambda s: str(s))
            c4_sym = next(c for c in checks_sym if c["id"] == "C4_real_agy2_db")
            self.assertEqual(c4_sym["status"], "FAIL")
            self.assertIn("antigravity-ide", c4_sym["detail"])


class TestRegressMatrixIsolationAndCli(unittest.TestCase):
    def test_layer_a_env_allowlist_strips_host_config_codex_home_and_secrets(self) -> None:
        with tempfile.TemporaryDirectory(prefix="avi-t-env-") as raw_td:
            td = Path(raw_td)
            base_env = {
                "PATH": "/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "HOME": "/host/real-home",
                "XDG_CONFIG_HOME": "/host/real-config",
                "XDG_CACHE_HOME": "/host/real-cache",
                "XDG_RUNTIME_DIR": "/host/real-runtime",
                "AGENT_VOICE_INPUT_CONFIG": "/host/broken/config.toml",
                "CODEX_HOME": "/host/real-codex-home",
                "OPENAI_API_KEY": "sk-hostsecret1234567890",
                "DASHSCOPE_API_KEY": "sk-dashsecret1234567890",
                "GEMINI_API_KEY": "AIzaSyHostSecret1234567890",
                "TMUX": "/tmp/tmux-1000/default,123,0",
                "OPENAI_FAST_MODE": "1",
            }
            iso_home = td / "home"
            iso_config = td / "config"
            iso_cache = td / "cache"
            rt_dir = td / "rt"
            iso_env = rm._build_layer_a_env(
                base_env,
                iso_home=iso_home,
                iso_config=iso_config,
                iso_cache=iso_cache,
                rt_dir=rt_dir,
            )
            self.assertEqual(iso_env["HOME"], str(iso_home))
            self.assertEqual(iso_env["XDG_CONFIG_HOME"], str(iso_config))
            self.assertEqual(iso_env["XDG_CACHE_HOME"], str(iso_cache))
            self.assertEqual(iso_env["XDG_RUNTIME_DIR"], str(rt_dir))
            self.assertEqual(iso_env["AGENT_VOICE_INPUT_RUNTIME_DIR"], str(rt_dir))
            self.assertEqual(iso_env["PATH"], "/usr/bin:/bin")
            for forbidden in (
                "AGENT_VOICE_INPUT_CONFIG",
                "CODEX_HOME",
                "OPENAI_API_KEY",
                "DASHSCOPE_API_KEY",
                "GEMINI_API_KEY",
                "TMUX",
                "OPENAI_FAST_MODE",
            ):
                self.assertNotIn(forbidden, iso_env)

    def test_offline_cli_does_not_load_broken_host_config_toml(self) -> None:
        with tempfile.TemporaryDirectory(prefix="avi-t-badcfg-") as raw_td:
            td = Path(raw_td)
            bad_cfg = td / "broken.toml"
            bad_cfg.write_text("[asr\nnot valid toml = ???\n", encoding="utf-8")
            fake_home = td / "empty_home"
            fake_home.mkdir()
            env_patch = {
                "HOME": str(fake_home),
                "AGENT_VOICE_INPUT_CONFIG": str(bad_cfg),
                "XDG_CONFIG_HOME": str(td / "cfg"),
            }
            buf = io.StringIO()
            with mock.patch.dict(os.environ, env_patch, clear=False), contextlib.redirect_stdout(buf):
                rc = rm.main(["--no-matrix", "--probe-real-clis", "--json"])
            self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual(len(payload["checks"]), 5)

    def test_layer_a_daemon_startup_timeout_recorded_as_redacted_fail_and_valid_json(self) -> None:
        canary = "sk-canarysecret9876543210"
        with mock.patch.object(
            rm,
            "_wait_control_socket",
            side_effect=TimeoutError(f"socket timeout with leaked {canary}"),
        ):
            buf = io.StringIO()
            with mock.patch.dict(os.environ, {"OPENAI_API_KEY": canary}, clear=False), contextlib.redirect_stdout(buf):
                rc = rm.main(["--json"])
        self.assertEqual(rc, 1)
        raw_out = buf.getvalue()
        self.assertNotIn(canary, raw_out)
        payload = json.loads(raw_out)
        self.assertFalse(payload["ok"])
        startup_chk = next(c for c in payload["checks"] if c["id"] == "A0_headless_daemon_startup")
        self.assertEqual(startup_chk["status"], "FAIL")
        self.assertIn("[REDACTED]", startup_chk["detail"])

    def test_make_short_tempdir_prefers_tmp_on_posix(self) -> None:
        if os.name != "posix" or not os.access("/tmp", os.W_OK | os.X_OK):
            self.skipTest("POSIX /tmp not writable")
        with rm._make_short_tempdir(prefix="avi-t-short-") as td:
            self.assertTrue(td.startswith("/tmp/"))
            self.assertLess(len(os.path.join(os.path.realpath(td), "rt", "control.sock")), 96)

    def test_cli_rejects_invalid_option_combinations_and_empty_checks(self) -> None:
        invalid_argvs = [
            ["--live-daemon", "--skip-selftest-wav", "--deliver-live"],
            ["--deliver-live"],
            ["--skip-selftest-wav"],
            ["--allow-drift"],
            ["--wav", "/tmp/sample.wav"],
            ["--show-text"],
        ]
        for argv in invalid_argvs:
            with self.subTest(argv=argv):
                err_buf = io.StringIO()
                with contextlib.redirect_stderr(err_buf):
                    with self.assertRaises(SystemExit) as ctx:
                        rm.main(argv)
                self.assertNotEqual(ctx.exception.code, 0)

        # Empty checks (--no-matrix alone) -> rc=1 and JSON ok=false
        out_buf = io.StringIO()
        with contextlib.redirect_stdout(out_buf):
            rc = rm.main(["--no-matrix", "--json"])
        self.assertEqual(rc, 1)
        payload = json.loads(out_buf.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["checks"], [])


if __name__ == "__main__":
    unittest.main()
