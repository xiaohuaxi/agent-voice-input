"""Offline unit tests for Tier-1 Batch A reliability fixes (A1-A5)."""
from __future__ import annotations

import base64
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import math
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_voice_input import cli as cli_mod  # noqa: E402
from agent_voice_input.asr_providers import AsrResult, BaseAsrProvider  # noqa: E402
from agent_voice_input.audio_proc import (  # noqa: E402
    FAILED_AUDIO_MAX_AGE_SEC,
    get_failed_audio_dir,
    list_failed_audios,
    pack_pcm16_mono_wav,
    resolve_failed_audio,
    save_failed_audio,
)
from agent_voice_input.config import (  # noqa: E402
    AsrConfig,
    AudioConfig,
    Config,
    ConfigError,
    HotkeyConfig,
    RefinerConfig,
)
from agent_voice_input.daemon import (  # noqa: E402
    VoiceInputDaemon,
    _QueuedUtterance,
    _transcribe_wav_chunks,
    append_telemetry_record,
    build_pipeline_summary,
    run_pipeline_once,
)
from agent_voice_input.diagnose import (  # noqa: E402
    collect_live_diagnostics,
    diagnose_record_flags,
    format_live_diagnostics_markdown,
    summarize_telemetry_records,
)
from agent_voice_input.hotwords import (  # noqa: E402
    add_hotwords,
    clear_hotwords_cache,
    load_hotwords_file,
    load_hotwords_table,
    remove_hotwords,
)
from agent_voice_input.hud import (  # noqa: E402
    HUD_STATE_DURATIONS_MS,
    default_hud_duration_ms,
    format_hud_text,
    hud_layer_for_state,
    is_toast_hud_state,
)
from agent_voice_input.platform_common import ForegroundWindow, PasteOutcome  # noqa: E402
from agent_voice_input.platform_wsl import WslPlatform  # noqa: E402
from agent_voice_input.refiner import create_refiner  # noqa: E402
from agent_voice_input.session_ctx import SessionTracker  # noqa: E402


def _tone_samples(sec: float, *, amp: int = 8000, freq_hz: float = 440.0, sr: int = 16000) -> list[int]:
    n = int(round(sec * sr))
    return [int(amp * math.sin(2.0 * math.pi * freq_hz * i / sr)) for i in range(n)]


def _silence_samples(sec: float, *, sr: int = 16000) -> list[int]:
    return [0] * int(round(sec * sr))


def _voiced_wav(sec: float = 0.8, amp: int = 8000) -> bytes:
    return pack_pcm16_mono_wav(_tone_samples(sec, amp=amp), sample_rate=16000)


def _isolated_tracker(base: Path) -> SessionTracker:
    return SessionTracker(
        home_dir=base / "home",
        env={"HOME": str(base / "home"), "PATH": ""},
        runtime_dir=base / "rt",
        proc_root=base / "empty_proc",
    )


class _EnergyAwareAsr(BaseAsrProvider):
    """Deterministic concurrent-safe ASR stub keyed by chunk wav bytes or silence check."""

    provider_name = "mock-asr"

    def __init__(self, *, text_by_wav: dict[bytes, str] | None = None, silent_only_empty: bool = False) -> None:
        self._text_by_wav = text_by_wav or {}
        self._silent_only_empty = silent_only_empty
        self._lock = threading.Lock()
        self.empty_calls = 0

    def transcribe(self, wav_bytes: bytes, context: object = None, **kwargs: object) -> AsrResult:
        del context, kwargs
        if wav_bytes in self._text_by_wav:
            txt = self._text_by_wav[wav_bytes]
        elif self._silent_only_empty:
            from agent_voice_input.audio_proc import process_wav

            txt = "有声段" if process_wav(wav_bytes, AudioConfig(), min_duration_sec=0.1).passed else ""
        else:
            txt = ""
        if not txt:
            with self._lock:
                self.empty_calls += 1
        return AsrResult(text=txt, provider=self.provider_name, model="mock-v1", latency_ms=12)


class _SequenceAsr(BaseAsrProvider):
    provider_name = "mock-asr"

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self._lock = threading.Lock()
        self._idx = 0
        self.contexts: list[object] = []

    def transcribe(self, wav_bytes: bytes, context: object = None, **kwargs: object) -> AsrResult:
        del wav_bytes, kwargs
        self.contexts.append(context)
        with self._lock:
            txt = self._responses[self._idx] if self._idx < len(self._responses) else ""
            self._idx += 1
        return AsrResult(text=txt, provider=self.provider_name, model="mock-v1", latency_ms=12)


class _StubPlatform:
    def __init__(
        self,
        wav_bytes: bytes,
        *,
        paste_outcome: PasteOutcome | None = None,
        audio_source: str = "winmm",
    ) -> None:
        self.wav_bytes = wav_bytes
        self.paste_outcome = paste_outcome or PasteOutcome(True, True, "ok", "WindowsTerminal", True)
        self.audio_source = audio_source
        self.fg = ForegroundWindow(
            hwnd=42,
            process="WindowsTerminal",
            title="codex",
            cwd="/tmp/batch-a-proj",
        )
        self.huds: list[str] = []
        self.hud_calls: list[dict[str, object]] = []
        self.beeps: list[str] = []
        self.last_audio_error = ""

    def get_foreground_window(self) -> ForegroundWindow:
        return self.fg

    def start_recording(self, runtime_dir: Path, **kwargs: object) -> None:
        del runtime_dir, kwargs

    def stop_recording(self, **kwargs: object) -> tuple[bytes, str]:
        del kwargs
        return self.wav_bytes, self.audio_source

    def cancel_recording(self) -> None:
        pass

    def switch_to_fallback_audio(self) -> bool:
        return False

    def deliver_text(self, text: str, **kwargs: object) -> PasteOutcome:
        del text, kwargs
        return self.paste_outcome

    def hud(self, state: str, **kwargs: object) -> None:
        self.huds.append(state)
        self.hud_calls.append({"state": state, **kwargs})

    def beep(self, kind: str) -> None:
        self.beeps.append(kind)

    def start_event_listener(self, cb: object) -> None:
        del cb

    def close(self) -> None:
        pass


def _wait_for_seq(daemon: VoiceInputDaemon, seq_id: int, timeout: float = 3.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if daemon.last_result.get("seq_id") == seq_id:
            return dict(daemon.last_result)
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for seq_id={seq_id}; last_result={daemon.last_result!r}")


class TestA1MultiChunkMissedSpeechProtection(unittest.TestCase):
    def test_multi_chunk_voiced_empty_vs_silent_pause_chunks(self) -> None:
        # 4 voiced segments separated by clean 0.4s silent pauses -> splits cleanly into 4 chunks at max_chunk_sec=1.1s
        voiced_4_samples: list[int] = []
        for idx in range(4):
            voiced_4_samples.extend(_tone_samples(0.65, amp=9000))
            if idx < 3:
                voiced_4_samples.extend(_silence_samples(0.45))
        voiced_4_wav = pack_pcm16_mono_wav(voiced_4_samples, sample_rate=16000)

        # Case 1: middle 2 voiced chunks return empty -> detected as missed_chunks=2 (100% order-deterministic)
        from agent_voice_input.audio_proc import inspect_wav_chunks

        v_chunks = inspect_wav_chunks(voiced_4_wav, max_chunk_sec=1.1, floor_dbfs=-50.0)
        self.assertEqual(len(v_chunks), 4)
        asr_partial = _EnergyAwareAsr(
            text_by_wav={
                v_chunks[0].wav_bytes: "开头",
                v_chunks[1].wav_bytes: "",
                v_chunks[2].wav_bytes: "",
                v_chunks[3].wav_bytes: "结尾",
            }
        )
        merged, n_chunks, _ = _transcribe_wav_chunks(
            voiced_4_wav,
            eff_asr=asr_partial,
            asr_ctx=None,
            floor_dbfs=-50.0,
            audio_cfg=AudioConfig(),
            max_chunk_sec=1.1,
        )
        self.assertEqual(n_chunks, 4)
        self.assertEqual(merged.text, "开头，结尾")
        self.assertEqual(merged.missed_chunks, 2)

        # Case 2: real multi-chunk WAV where middle chunk(s) are PURE silence -> zero false positives!
        silent_mid_samples = (
            _tone_samples(0.8, amp=9000)
            + _silence_samples(2.6)
            + _tone_samples(0.8, amp=9000)
        )
        silent_mid_wav = pack_pcm16_mono_wav(silent_mid_samples, sample_rate=16000)
        asr_silent_mid = _EnergyAwareAsr(silent_only_empty=True)
        merged_sil, n_sil, _ = _transcribe_wav_chunks(
            silent_mid_wav,
            eff_asr=asr_silent_mid,
            asr_ctx=None,
            floor_dbfs=-50.0,
            audio_cfg=AudioConfig(),
            max_chunk_sec=1.1,
        )
        self.assertGreaterEqual(n_sil, 3)
        self.assertGreaterEqual(asr_silent_mid.empty_calls, 1)
        self.assertIn("有声段", merged_sil.text)
        self.assertEqual(merged_sil.missed_chunks, 0)

        # Case 3: end-to-end run_pipeline_once & VoiceInputDaemon stash full WAV + emit partial_asr HUD + diagnose flag
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            rt = base / "rt"
            rt.mkdir(mode=0o700)
            cfg = Config(refiner=RefinerConfig(provider="off"))

            # 3a. Pure silence middle chunks -> no failed WAV, ok=True, asr_missed_chunks=0
            res_sil = run_pipeline_once(
                silent_mid_wav,
                cfg=cfg,
                tracker=_isolated_tracker(base),
                asr_provider=_EnergyAwareAsr(silent_only_empty=True),
                refiner=create_refiner(cfg),
                deliver_fn=lambda txt, win: PasteOutcome(True, True, "ok", "WindowsTerminal", True),
                audio_source="winmm",
                runtime_dir=rt,
                max_chunk_sec=1.1,
                env={"HOME": str(base / "home")},
            )
            self.assertTrue(res_sil.ok)
            self.assertEqual(res_sil.asr_missed_chunks, 0)
            self.assertEqual(res_sil.saved_audio, "")
            self.assertEqual(list_failed_audios(rt), [])

            # 3b. Voiced middle chunks missed -> partial text still delivered, full WAV saved to failed/
            plat = _StubPlatform(voiced_4_wav)
            daemon = VoiceInputDaemon(
                cfg,
                env={"HOME": str(base / "home"), "PATH": ""},
                runtime_dir=rt,
                platform=plat,
                asr_provider=_SequenceAsr(["开头", "", "", "结尾"]),
                refiner=create_refiner(cfg),
                tracker=_isolated_tracker(base),
            )
            try:
                with mock.patch("agent_voice_input.daemon.DEFAULT_MAX_CHUNK_SEC", 1.1):
                    daemon.handle_trigger("toggle", plat.fg)
                    daemon._last_trigger_ts = -1e9
                    r_stop = daemon.handle_trigger("toggle", plat.fg)
                    last = _wait_for_seq(daemon, int(r_stop["seq_id"]))
                self.assertTrue(last["ok"])
                self.assertEqual(last["asr_missed_chunks"], 2)
                self.assertEqual(last["delivery_status"], "pasted")
                self.assertTrue(Path(str(last["saved_audio"])).is_file())
                self.assertEqual(plat.huds[-1], "partial_asr")
            finally:
                daemon.close()

            # Diagnose reports PARTIAL_ASR(2) and keeps raw path out of JSON
            diag = collect_live_diagnostics(
                cfg,
                {"HOME": str(base / "home"), "PATH": ""},
                runtime_dir=rt,
                last=5,
            )
            self.assertEqual(diag["summary"]["ok_count"], 1)
            self.assertEqual(diag["recent_results"][-1]["asr_missed_chunks"], 2)
            self.assertIn("PARTIAL_ASR(2)", diag["recent_results"][-1]["diag_flags"])
            self.assertTrue(diag["recent_results"][-1]["has_saved_audio"])

            # 3c. Partial missed chunks when save_failed_audio fails (ENOSPC) -> partial_asr_unsaved (no retry lie!)
            plat_enospc = _StubPlatform(voiced_4_wav)
            daemon_enospc = VoiceInputDaemon(
                cfg,
                env={"HOME": str(base / "home"), "PATH": ""},
                runtime_dir=rt,
                platform=plat_enospc,
                asr_provider=_SequenceAsr(["开头", "", "", "结尾"]),
                refiner=create_refiner(cfg),
                tracker=_isolated_tracker(base),
            )
            try:
                with (
                    mock.patch("agent_voice_input.daemon.DEFAULT_MAX_CHUNK_SEC", 1.1),
                    mock.patch("agent_voice_input.daemon.save_failed_audio", return_value=None),
                ):
                    daemon_enospc.handle_trigger("toggle", plat_enospc.fg)
                    daemon_enospc._last_trigger_ts = -1e9
                    r_stop2 = daemon_enospc.handle_trigger("toggle", plat_enospc.fg)
                    last2 = _wait_for_seq(daemon_enospc, int(r_stop2["seq_id"]))
                self.assertTrue(last2["ok"])
                self.assertEqual(last2["asr_missed_chunks"], 2)
                self.assertNotIn("saved_audio", last2)
                self.assertEqual(plat_enospc.huds[-1], "partial_asr_unsaved")
            finally:
                daemon_enospc.close()

            # 3d. All voiced chunks return empty -> linked with A5: saved WAV + asr_empty_retry HUD
            plat_all_empty = _StubPlatform(voiced_4_wav)
            daemon_empty = VoiceInputDaemon(
                cfg,
                env={"HOME": str(base / "home"), "PATH": ""},
                runtime_dir=rt,
                platform=plat_all_empty,
                asr_provider=_SequenceAsr(["", "", "", ""]),
                refiner=create_refiner(cfg),
                tracker=_isolated_tracker(base),
            )
            try:
                with mock.patch("agent_voice_input.daemon.DEFAULT_MAX_CHUNK_SEC", 1.1):
                    daemon_empty.handle_trigger("toggle", plat_all_empty.fg)
                    daemon_empty._last_trigger_ts = -1e9
                    r_stop3 = daemon_empty.handle_trigger("toggle", plat_all_empty.fg)
                    last3 = _wait_for_seq(daemon_empty, int(r_stop3["seq_id"]))
                self.assertFalse(last3["ok"])
                self.assertEqual(last3["skipped_reason"], "empty_asr")
                self.assertEqual(last3["asr_missed_chunks"], 4)
                self.assertEqual(last3["delivery_status"], "undelivered_recoverable")
                self.assertTrue(Path(str(last3["saved_audio"])).is_file())
                self.assertEqual(plat_all_empty.huds[-1], "asr_empty_retry")
            finally:
                daemon_empty.close()

    def test_a1_attenuated_quiet_chunk_after_loud_normalization_and_boosted_noise_guard(self) -> None:
        from agent_voice_input.audio_proc import process_wav

        # Loud 1.8s chunk (~ -2 dBFS) + 0.45s silence + quiet spoken 1.3s chunk (amp=220, ~ -43.5 dBFS > -50 dBFS floor).
        # Peak/RMS normalization attenuates whole clip by ~ -13 dB so the quiet chunk drops BELOW raw -50 dBFS!
        loud_and_quiet_wav = pack_pcm16_mono_wav(
            _tone_samples(1.8, amp=26000) + _silence_samples(0.45) + _tone_samples(1.3, amp=220),
            sample_rate=16000,
        )
        gate_check = process_wav(loud_and_quiet_wav, AudioConfig())
        self.assertTrue(gate_check.passed)
        self.assertLess(gate_check.gain_db, -8.0)

        class _DropQuietChunkAsr(BaseAsrProvider):
            provider_name = "mock-asr"

            def __init__(self) -> None:
                pass

            def transcribe(self, wav_bytes: bytes, context: object = None, **kwargs: object) -> AsrResult:
                del context, kwargs
                # Return text only for the loud chunk (passes raw -50 dBFS), return "" for the attenuated quiet chunk!
                loud_chunk = process_wav(wav_bytes, AudioConfig(), floor_dbfs=-50.0, min_duration_sec=0.1).passed
                return AsrResult(
                    text=("强音部分" if loud_chunk else ""),
                    provider=self.provider_name,
                    model="mock-v1",
                    latency_ms=10,
                )

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            rt = base / "rt"
            rt.mkdir(mode=0o700)
            cfg = Config(refiner=RefinerConfig(provider="off"))
            res = run_pipeline_once(
                loud_and_quiet_wav,
                cfg=cfg,
                tracker=_isolated_tracker(base),
                asr_provider=_DropQuietChunkAsr(),
                refiner=create_refiner(cfg),
                deliver_fn=lambda txt, win: PasteOutcome(True, True, "ok", "WindowsTerminal", True),
                audio_source="winmm",
                runtime_dir=rt,
                max_chunk_sec=2.0,
                env={"HOME": str(base / "home")},
            )
            self.assertTrue(res.ok)
            self.assertEqual(res.final_text, "强音部分")
            self.assertGreaterEqual(res.asr_chunks, 2)
            self.assertEqual(res.asr_missed_chunks, 1)
            self.assertTrue(res.saved_audio and Path(res.saved_audio).is_file())

            # Positive gain (+20 dB boost): quiet speech (amp=320, ~ -40 dBFS) + very faint noise floor (amp=40, ~ -58 dBFS < -50 dBFS).
            # After +20 dB boost, faint noise rises to ~ -38 dBFS (> raw -50 dBFS) but stays below shifted floor (-30 dBFS) -> NO false positive!
            boost_wav = pack_pcm16_mono_wav(
                _tone_samples(1.8, amp=320) + _silence_samples(0.45) + _tone_samples(1.3, amp=40),
                sample_rate=16000,
            )
            gate_boost = process_wav(boost_wav, AudioConfig())
            self.assertTrue(gate_boost.passed)
            self.assertGreater(gate_boost.gain_db, 15.0)

            class _DropNoiseChunkAsr(BaseAsrProvider):
                provider_name = "mock-asr"

                def __init__(self) -> None:
                    pass

                def transcribe(self, wav_bytes: bytes, context: object = None, **kwargs: object) -> AsrResult:
                    del context, kwargs
                    strong = process_wav(
                        wav_bytes,
                        AudioConfig(),
                        floor_dbfs=-50.0 + gate_boost.gain_db,
                        min_duration_sec=0.1,
                    ).passed
                    return AsrResult(
                        text=("轻声内容" if strong else ""),
                        provider=self.provider_name,
                        model="mock-v1",
                        latency_ms=10,
                    )

            rt2 = base / "rt2"
            rt2.mkdir(mode=0o700)
            res_boost = run_pipeline_once(
                boost_wav,
                cfg=cfg,
                tracker=_isolated_tracker(base),
                asr_provider=_DropNoiseChunkAsr(),
                refiner=create_refiner(cfg),
                deliver_fn=lambda txt, win: PasteOutcome(True, True, "ok", "WindowsTerminal", True),
                audio_source="winmm",
                runtime_dir=rt2,
                max_chunk_sec=2.0,
                env={"HOME": str(base / "home")},
            )
            self.assertTrue(res_boost.ok)
            self.assertEqual(res_boost.asr_missed_chunks, 0)
            self.assertEqual(res_boost.saved_audio, "")
            self.assertEqual(list_failed_audios(rt2), [])


_MOCK_WSL_REC_SHIM = r"""
import base64, json, sys, time

mode = sys.argv[1]  # "legacy_lose" | "single_slot" | "serial_slow"
wav_b64 = sys.argv[2]
delay_stop = float(sys.argv[3])

recording = False
active_gen = 0
stopped_slot = None  # (rec_gen, wav_b64)
stop_calls = 0
seen_cmds = []

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

emit({"event": "ready", "ui_culture": "en-US"})

for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    msg = json.loads(raw)
    op = msg.get("cmd")
    seq = int(msg.get("seq_id") or 0)
    seen_cmds.append({"cmd": op, "rec_gen": msg.get("rec_gen"), "discard": msg.get("discard")})
    if op == "rec_start":
        recording = True
        active_gen = int(msg.get("rec_gen") or 0)
        stopped_slot = None
        emit({"type": "reply", "reply": "rec_start", "seq_id": seq, "ok": True, "rec_error": ""})
    elif op == "rec_stop":
        stop_calls += 1
        req_gen = int(msg.get("rec_gen") or 0)
        discard = bool(msg.get("discard"))
        if recording:
            recording = False
            if delay_stop > 0 and not discard:
                time.sleep(delay_stop)
            if mode in ("single_slot", "serial_slow") and not discard and active_gen > 0:
                stopped_slot = (active_gen, wav_b64)
            elif discard:
                stopped_slot = None
            if mode in ("legacy_lose", "single_slot") and stop_calls == 1 and delay_stop > 0 and not discard:
                # Simulate first stop reply lost so explicit retry MUST rely on StoppedRecSlot
                continue
            out_b64 = "" if discard else wav_b64
            emit({
                "type": "reply",
                "reply": "rec_stop",
                "seq_id": seq,
                "ok": bool(out_b64),
                "rec_gen": active_gen,
                "pcm_bytes": len(base64.b64decode(out_b64)) - 44 if out_b64 else 0,
                "wav_b64": out_b64,
                "rec_error": "",
            })
        elif mode in ("single_slot", "serial_slow") and not discard and stopped_slot is not None and req_gen > 0 and stopped_slot[0] == req_gen:
            out_b64 = stopped_slot[1]
            emit({
                "type": "reply",
                "reply": "rec_stop",
                "seq_id": seq,
                "ok": True,
                "rec_gen": req_gen,
                "pcm_bytes": len(base64.b64decode(out_b64)) - 44,
                "wav_b64": out_b64,
                "rec_error": "",
            })
        else:
            if discard:
                stopped_slot = None
            emit({
                "type": "reply",
                "reply": "rec_stop",
                "seq_id": seq,
                "ok": False,
                "rec_gen": req_gen,
                "pcm_bytes": 0,
                "wav_b64": "",
                "rec_error": "not_recording",
            })
    elif op == "rec_release":
        req_gen = int(msg.get("rec_gen") or 0)
        if stopped_slot is not None and (req_gen <= 0 or stopped_slot[0] == req_gen):
            stopped_slot = None
        if seq > 0:
            emit({"type": "reply", "reply": "rec_release", "seq_id": seq, "ok": True, "rec_gen": req_gen})
    elif op == "dump":
        emit({
            "type": "reply",
            "reply": "dump",
            "seq_id": seq,
            "ok": True,
            "slot_held": stopped_slot is not None,
            "cmds": seen_cmds,
        })
    elif op == "quit":
        break
    else:
        emit({"type": "reply", "reply": op, "seq_id": seq, "ok": True})
"""


class TestA2WslRecStopTimeoutRetry(unittest.TestCase):
    def test_a2_slow_serial_rec_stop_single_wait_does_not_block_next_rec_start_and_releases_on_timeout(self) -> None:
        sample_wav = _voiced_wav(0.5)
        sample_b64 = base64.b64encode(sample_wav).decode("ascii")
        cfg = Config(audio=AudioConfig(wsl_source="winmm", tail_drain_sec=0.0))

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)

            # 1. Production single-wait budget (default 15.0s, allow_retry=False):
            #    Slow serial rec_stop completes in ONE RPC, releases slot, and never queues a duplicate 25MB
            #    transfer that would block the immediately following rec_start!
            plat_serial = WslPlatform(
                cfg,
                env={},
                shim_cmd_factory=lambda: [
                    sys.executable,
                    "-u",
                    "-c",
                    _MOCK_WSL_REC_SHIM,
                    "serial_slow",
                    sample_b64,
                    "0.08",
                ],
            )
            self.assertEqual(plat_serial._rec_stop_timeout_sec, 15.0)
            plat_serial._rec_stop_timeout_sec = 0.40
            try:
                plat_serial.start_recording(rt)
                got_wav, src_ok = plat_serial.stop_recording(drain=False)
                self.assertEqual(src_ok, "winmm")
                self.assertEqual(got_wav, sample_wav)
                self.assertEqual(plat_serial.last_audio_error, "")

                # Immediately start the next recording: must succeed quickly with zero duplicate rec_stop queued!
                t_next = time.monotonic()
                plat_serial.start_recording(rt)
                self.assertLess(time.monotonic() - t_next, 0.25)

                dump = plat_serial._shim_rpc({"cmd": "dump"}, timeout_sec=2.0) or {}
                self.assertFalse(dump.get("slot_held"))
                rec_stop_cmds = [c for c in dump.get("cmds", []) if c["cmd"] == "rec_stop"]
                rec_rel_cmds = [c for c in dump.get("cmds", []) if c["cmd"] == "rec_release"]
                self.assertEqual(len(rec_stop_cmds), 1)
                self.assertGreater(rec_stop_cmds[0]["rec_gen"], 0)
                self.assertEqual(len(rec_rel_cmds), 1)
                self.assertEqual(rec_rel_cmds[0]["rec_gen"], rec_stop_cmds[0]["rec_gen"])

                # cancel_recording sends discard=True and leaves slot empty
                plat_serial.cancel_recording()
                dump2 = plat_serial._shim_rpc({"cmd": "dump"}, timeout_sec=2.0) or {}
                self.assertFalse(dump2.get("slot_held"))
                last_stop = [c for c in dump2.get("cmds", []) if c["cmd"] == "rec_stop"][-1]
                self.assertTrue(last_stop.get("discard"))
            finally:
                plat_serial.close()

            # 2. Final timeout abandonment STILL sends rec_release(rec_gen=stop_gen) in finally so large WAV never leaks!
            plat_abandon = WslPlatform(
                cfg,
                env={},
                shim_cmd_factory=lambda: [
                    sys.executable,
                    "-u",
                    "-c",
                    _MOCK_WSL_REC_SHIM,
                    "serial_slow",
                    sample_b64,
                    "0.22",
                ],
            )
            plat_abandon._rec_stop_timeout_sec = 0.08
            try:
                plat_abandon.start_recording(rt)
                with self.assertRaises(queue.Empty):
                    plat_abandon.stop_recording(drain=False)
                # FIFO serial pipe guarantees dump executes AFTER the slow rec_stop AND our finally rec_release!
                dump_ab = plat_abandon._shim_rpc({"cmd": "dump"}, timeout_sec=2.0) or {}
                self.assertFalse(dump_ab.get("slot_held"), "StoppedRecSlot must be freed even on rec_stop timeout")
                stop_ab = [c for c in dump_ab.get("cmds", []) if c["cmd"] == "rec_stop"]
                rel_ab = [c for c in dump_ab.get("cmds", []) if c["cmd"] == "rec_release"]
                self.assertEqual(len(stop_ab), 1)
                self.assertEqual(len(rel_ab), 1)
                self.assertEqual(rel_ab[0]["rec_gen"], stop_ab[0]["rec_gen"])
            finally:
                plat_abandon.close()

            # 3. Contrast: explicit same-rec_gen retry returns WAV on single-slot shim vs not_recording on legacy shim
            for mode_name, expect_wav in (("legacy_lose", b""), ("single_slot", sample_wav)):
                plat_cmp = WslPlatform(
                    cfg,
                    env={},
                    shim_cmd_factory=lambda m=mode_name: [
                        sys.executable,
                        "-u",
                        "-c",
                        _MOCK_WSL_REC_SHIM,
                        m,
                        sample_b64,
                        "0.15",
                    ],
                )
                try:
                    plat_cmp.start_recording(rt)
                    gen_id = plat_cmp._winmm_gen
                    res_retry = (
                        plat_cmp._shim_rpc(
                            {"cmd": "rec_stop", "rec_gen": gen_id},
                            timeout_sec=0.10,
                            allow_retry=True,
                        )
                        or {}
                    )
                    got_b = base64.b64decode(res_retry.get("wav_b64") or "") if res_retry.get("wav_b64") else b""
                    self.assertEqual(got_b, expect_wav)
                finally:
                    plat_cmp.close()

    def test_a2_shared_queue_captures_late_first_reply_for_retryable_rpc(self) -> None:
        # When an explicit retryable RPC times out at 0.18s while Attempt 0's reply arrives at 0.28s
        # (and Attempt 1 would take 0.65s), the shared wait queue catches Attempt 0's reply without dropping it.
        sample_wav = _voiced_wav(0.5)
        sample_b64 = base64.b64encode(sample_wav).decode("ascii")
        late_first_shim = r"""
import base64, json, sys, threading, time

wav_b64 = sys.argv[1]
out_lock = threading.Lock()
stop_count = [0]

def emit(obj):
    with out_lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

emit({"event": "ready", "ui_culture": "en-US"})

for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    msg = json.loads(raw)
    op = msg.get("cmd")
    seq = int(msg.get("seq_id") or 0)
    if op == "rec_start":
        emit({"type": "reply", "reply": "rec_start", "seq_id": seq, "ok": True, "rec_error": ""})
    elif op == "rec_stop":
        stop_count[0] += 1
        n = stop_count[0]
        rec_gen = int(msg.get("rec_gen") or 0)
        delay = 0.28 if n == 1 else 0.65
        def _send_later(s=seq, g=rec_gen, d=delay):
            time.sleep(d)
            emit({
                "type": "reply",
                "reply": "rec_stop",
                "seq_id": s,
                "ok": True,
                "rec_gen": g,
                "pcm_bytes": len(base64.b64decode(wav_b64)) - 44,
                "wav_b64": wav_b64,
                "rec_error": "",
            })
        threading.Thread(target=_send_later, daemon=True).start()
    elif op == "quit":
        break
    else:
        emit({"type": "reply", "reply": op, "seq_id": seq, "ok": True})
"""
        cfg = Config(audio=AudioConfig(wsl_source="winmm", tail_drain_sec=0.0))
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            plat = WslPlatform(
                cfg,
                env={},
                shim_cmd_factory=lambda: [sys.executable, "-u", "-c", late_first_shim, sample_b64],
            )
            try:
                plat.start_recording(rt)
                t0 = time.monotonic()
                res = (
                    plat._shim_rpc(
                        {"cmd": "rec_stop", "rec_gen": plat._winmm_gen},
                        timeout_sec=0.18,
                        allow_retry=True,
                    )
                    or {}
                )
                elapsed = time.monotonic() - t0
                self.assertEqual(base64.b64decode(res.get("wav_b64") or ""), sample_wav)
                self.assertLess(elapsed, 0.55)
                with plat._shim_lock:
                    self.assertEqual(plat._pending, {})
            finally:
                plat.close()


class TestA3FailedAudioTtlAndActivePruning(unittest.TestCase):
    def test_failed_audio_24h_ttl_filter_and_daemon_startup_runtime_pruning(self) -> None:
        wav = _voiced_wav(0.4)
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            rt = base / "rt"
            rt.mkdir(mode=0o700)
            now = time.time()

            # Stage exact 24h boundary files directly via save_failed_audio (with custom now_ts)
            f_expired = save_failed_audio(
                wav, rt, duration_sec=4.0, label="expired-24h", now_ts=now - FAILED_AUDIO_MAX_AGE_SEC - 5.0, max_age_sec=0
            )
            f_border_ok = save_failed_audio(
                wav, rt, duration_sec=5.0, label="within-24h", now_ts=now - FAILED_AUDIO_MAX_AGE_SEC + 60.0, max_age_sec=0
            )
            assert f_expired is not None and f_border_ok is not None

            # 1. list_failed_audios / resolve_failed_audio filter out >24h entries without mutating disk
            listed = list_failed_audios(rt, now_ts=now)
            self.assertEqual(listed, [f_border_ok])
            self.assertTrue(f_expired.is_file(), "list_failed_audios must stay read-only")
            self.assertEqual(resolve_failed_audio(rt, now_ts=now), f_border_ok)
            with self.assertRaises(FileNotFoundError):
                resolve_failed_audio(rt, "expired-24h", now_ts=now)

            # 2. CLI `retry --list` only shows unexpired entries
            cli_env = {"HOME": str(base / "home"), "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)}
            out_buf = io.StringIO()
            with mock.patch.dict(os.environ, cli_env, clear=True), redirect_stdout(out_buf):
                rc = cli_mod.main(["retry", "--list"])
            self.assertEqual(rc, 0)
            self.assertIn("within-24h", out_buf.getvalue())
            self.assertNotIn("expired-24h", out_buf.getvalue())

            # 3. Daemon startup prunes >24h WAVs while keeping fresh ones, and throttles runtime pruning
            fake_now = [1000.0]
            cfg = Config(refiner=RefinerConfig(provider="off"))
            plat = _StubPlatform(wav)
            daemon = VoiceInputDaemon(
                cfg,
                env={"HOME": str(base / "home"), "PATH": ""},
                runtime_dir=rt,
                platform=plat,
                asr_provider=_SequenceAsr(["ok"]),
                refiner=create_refiner(cfg),
                tracker=_isolated_tracker(base),
                clock_fn=lambda: fake_now[0],
            )
            try:
                self.assertFalse(f_expired.exists(), "daemon startup must prune expired failed WAV")
                self.assertTrue(f_border_ok.exists())

                # Stage another expired WAV manually and verify throttling (< 3600s does not prune)
                f_old2 = get_failed_audio_dir(rt) / "20200101-000000_3s_old2.wav"
                f_old2.write_bytes(wav)
                old_ts = time.time() - FAILED_AUDIO_MAX_AGE_SEC - 30.0
                os.utime(f_old2, (old_ts, old_ts))

                fake_now[0] += 120.0
                self.assertFalse(daemon._maybe_prune_failed_audio())
                self.assertTrue(f_old2.exists())

                # Advance clock past 1h throttle -> _append_telemetry triggers active pruning
                fake_now[0] += 3600.0
                daemon._append_telemetry({"seq_id": 1, "ok": True})
                self.assertFalse(f_old2.exists())

                # Pruning error never bubbles up to disrupt main flow
                fake_now[0] += 3601.0
                with mock.patch("agent_voice_input.daemon.prune_failed_audio_dir", side_effect=OSError("EIO")):
                    self.assertFalse(daemon._maybe_prune_failed_audio())
            finally:
                daemon.close()

    def test_a3_explicit_path_inside_failed_dir_enforces_24h_ttl_but_external_wav_allowed(self) -> None:
        wav = _voiced_wav(0.3)
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            rt = base / "rt"
            rt.mkdir(mode=0o700)
            now = time.time()
            old_ts = now - FAILED_AUDIO_MAX_AGE_SEC - 120.0

            f_expired_in_failed = save_failed_audio(
                wav, rt, duration_sec=3.0, label="expired-explicit", now_ts=old_ts, max_age_sec=0
            )
            assert f_expired_in_failed is not None
            # Explicit full path inside failed/ must STILL enforce 24h TTL
            with self.assertRaisesRegex(FileNotFoundError, "expired"):
                resolve_failed_audio(rt, str(f_expired_in_failed), now_ts=now)

            # External WAV outside failed/ remains allowed regardless of mtime
            ext_wav = base / "external_old.wav"
            ext_wav.write_bytes(wav)
            os.utime(ext_wav, (old_ts, old_ts))
            self.assertEqual(resolve_failed_audio(rt, str(ext_wav), now_ts=now), ext_wav)


class TestA4BadEncodingHotwordsDegradation(unittest.TestCase):
    def setUp(self) -> None:
        clear_hotwords_cache()

    def tearDown(self) -> None:
        clear_hotwords_cache()

    def test_utf16_and_invalid_utf8_hotwords_degrade_in_pipeline_and_fail_cleanly_in_cli(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            hw_utf16 = base / "hotwords_utf16.txt"
            utf16_bytes = "[global]\r\nAntigravity\r\n".encode("utf-16")  # UTF-16 LE BOM
            hw_utf16.write_bytes(utf16_bytes)

            hw_utf16be = base / "hotwords_utf16be.txt"
            hw_utf16be.write_bytes(b"\xfe\xff" + "TestWord\n".encode("utf-16-be"))

            hw_bad = base / "hotwords_bad.txt"
            bad_bytes = b"GoodWord\n\xff\xfe\x80\x81bad\n"
            hw_bad.write_bytes(bad_bytes)

            # 1. Live pipeline read degrades gracefully and warns ONCE per file stat version
            err1 = io.StringIO()
            with redirect_stderr(err1):
                t1 = load_hotwords_table(hw_utf16, use_cache=True)
                t2 = load_hotwords_table(hw_utf16, use_cache=True)
                t3 = load_hotwords_table(hw_utf16, use_cache=False)
            self.assertEqual(t1.merged_for_scene("coding"), [])
            self.assertEqual(t2.merged_for_scene("coding"), [])
            self.assertEqual(t3.merged_for_scene("coding"), [])
            self.assertTrue(t1.exists)
            self.assertIn("UTF-16 LE", t1.encoding_error)
            self.assertEqual(err1.getvalue().count("WARN:"), 1)

            # Updating file (new stat version, still bad encoding) emits 1 fresh warning
            err2 = io.StringIO()
            hw_utf16.write_bytes("[global]\r\nAntigravity2\r\n".encode("utf-16"))
            with redirect_stderr(err2):
                load_hotwords_file(hw_utf16, scene="coding")
                load_hotwords_file(hw_utf16, scene="coding")
            self.assertEqual(err2.getvalue().count("WARN:"), 1)

            # Fixing file to valid UTF-8 restores hotwords immediately
            hw_utf16.write_text("[coding]\nRecoveredTerm\n", encoding="utf-8")
            self.assertEqual(load_hotwords_file(hw_utf16, scene="coding"), ["RecoveredTerm"])

            # UTF-16 BE & raw invalid UTF-8 bytes
            err3 = io.StringIO()
            with redirect_stderr(err3):
                self.assertEqual(load_hotwords_file(hw_utf16be, scene="global"), [])
                self.assertEqual(load_hotwords_file(hw_bad, scene="global"), [])
            self.assertIn("UTF-16 BE", err3.getvalue())
            self.assertIn("invalid UTF-8", err3.getvalue())

            # 2. strict_encoding / add_hotwords / remove_hotwords raise ConfigError & never clobber file!
            hw_utf16.write_bytes(utf16_bytes)
            with self.assertRaises(ConfigError) as ctx_list:
                load_hotwords_table(hw_utf16, use_cache=False, strict_encoding=True)
            self.assertIn("UTF-8", str(ctx_list.exception))
            with self.assertRaises(ConfigError) as ctx_add:
                add_hotwords(hw_utf16, ["NewTerm"], scope="global")
            self.assertIn("UTF-8", str(ctx_add.exception))
            with self.assertRaises(ConfigError) as ctx_rm:
                remove_hotwords(hw_utf16, ["Antigravity"], scope="global")
            self.assertIn("UTF-8", str(ctx_rm.exception))
            self.assertEqual(hw_utf16.read_bytes(), utf16_bytes, "add/remove must never corrupt non-UTF-8 file")

            # 3. CLI `hotwords list|add|remove` report actionable UTF-8 error and return exit code 2
            cfg_path = base / "config.toml"
            cfg_path.write_text(f'[asr]\nhotwords_file = "{hw_utf16}"\n', encoding="utf-8")
            cli_env = {"HOME": str(base / "home"), "PATH": ""}
            for sub_argv in (
                ["--config", str(cfg_path), "hotwords", "list"],
                ["--config", str(cfg_path), "hotwords", "add", "Foo"],
                ["--config", str(cfg_path), "hotwords", "remove", "Foo"],
            ):
                err_cli = io.StringIO()
                with mock.patch.dict(os.environ, cli_env, clear=True), redirect_stderr(err_cli):
                    rc = cli_mod.main(sub_argv)
                self.assertEqual(rc, 2, msg=sub_argv)
                self.assertIn("UTF-8", err_cli.getvalue())
            self.assertEqual(hw_utf16.read_bytes(), utf16_bytes)

            # 4. Live dictation pipeline does NOT break ASR when hotwords.txt is UTF-16 LE; keeps built-in/workspace hotwords
            clear_hotwords_cache()
            rt = base / "rt"
            rt.mkdir(mode=0o700)
            proj_dir = base / "my-repo"
            proj_dir.mkdir()
            (proj_dir / "README.md").write_text("# KestrelEngine\n", encoding="utf-8")
            cfg = Config(
                asr=AsrConfig(hotwords_file=str(hw_utf16)),
                refiner=RefinerConfig(provider="off"),
            )
            asr = _SequenceAsr(["第一次正常转写", "第二次正常转写"])
            err_pipe = io.StringIO()
            with redirect_stderr(err_pipe):
                for seq in (1, 2):
                    res = run_pipeline_once(
                        _voiced_wav(0.6),
                        cfg=cfg,
                        tracker=_isolated_tracker(base),
                        asr_provider=asr,
                        refiner=create_refiner(cfg),
                        origin_window=ForegroundWindow(1, "WindowsTerminal", "bash", cwd=str(proj_dir)),
                        deliver_fn=lambda txt, win: PasteOutcome(True, True, "ok", "WindowsTerminal", True),
                        audio_source="winmm",
                        seq_id=seq,
                        runtime_dir=rt,
                        env={"HOME": str(base / "home"), "PATH": ""},
                    )
                    self.assertTrue(res.ok)
            self.assertEqual(len(asr.contexts), 2)
            # Built-in/workspace hotwords preserved despite bad manual hotwords.txt
            hw_used = getattr(asr.contexts[0], "hotwords", [])
            self.assertTrue(len(hw_used) > 0)
            self.assertEqual(err_pipe.getvalue().count("WARN:"), 1)

    def test_a4_bomless_utf16le_with_nul_bytes_rejected_without_corrupting_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            hw_bomless = base / "hotwords_utf16le_nobom.txt"
            raw_utf16le = "[global]\nCodex\nAntigravity\n".encode("utf-16-le")
            self.assertIn(b"\x00", raw_utf16le)
            hw_bomless.write_bytes(raw_utf16le)

            # Live read rejects NUL-containing file gracefully and deduplicates warning per stat version
            err_live = io.StringIO()
            with redirect_stderr(err_live):
                t1 = load_hotwords_table(hw_bomless, use_cache=True)
                t2 = load_hotwords_table(hw_bomless, use_cache=False)
            self.assertEqual(t1.merged_for_scene("coding"), [])
            self.assertEqual(t2.merged_for_scene("coding"), [])
            self.assertIn("NUL byte", t1.encoding_error)
            self.assertEqual(err_live.getvalue().count("WARN:"), 1)

            # Strict load, add, remove, and CLI all refuse cleanly and keep original bytes untouched
            with self.assertRaises(ConfigError) as ctx_strict:
                load_hotwords_table(hw_bomless, use_cache=False, strict_encoding=True)
            self.assertIn("UTF-8", str(ctx_strict.exception))
            with self.assertRaises(ConfigError):
                add_hotwords(hw_bomless, ["SafeTerm"], scope="global")
            with self.assertRaises(ConfigError):
                remove_hotwords(hw_bomless, ["Codex"], scope="global")
            self.assertEqual(hw_bomless.read_bytes(), raw_utf16le)

            cfg_path = base / "cfg.toml"
            cfg_path.write_text(f'[asr]\nhotwords_file = "{hw_bomless}"\n', encoding="utf-8")
            cli_env = {"HOME": str(base / "home"), "PATH": ""}
            for argv in (
                ["--config", str(cfg_path), "hotwords", "list"],
                ["--config", str(cfg_path), "hotwords", "add", "SafeTerm"],
                ["--config", str(cfg_path), "hotwords", "remove", "Codex"],
            ):
                err_cli = io.StringIO()
                with mock.patch.dict(os.environ, cli_env, clear=True), redirect_stderr(err_cli):
                    rc = cli_mod.main(argv)
                self.assertEqual(rc, 2, msg=argv)
                self.assertIn("UTF-8", err_cli.getvalue())
            self.assertEqual(hw_bomless.read_bytes(), raw_utf16le)


class TestA5UnifiedDeliveryHudAndDiagnoseContract(unittest.TestCase):
    def test_hud_copy_layers_and_durations_for_new_reliability_states(self) -> None:
        expected = [
            ("partial_asr", "! 部分语音未识别，可重试", "! Partial speech missed, retry available", 3200),
            ("partial_asr_unsaved", "! 部分语音未识别", "! Partial speech missed", 3200),
            ("partial_asr_copied", "! 已复制，部分未识别可重试", "! Copied; partial missed, can retry", 3200),
            ("partial_asr_copied_unsaved", "! 已复制，部分语音未识别", "! Copied; partial speech missed", 3200),
            ("asr_empty_retry", "! 识别为空，已存录音可重试", "! Empty transcript, audio saved for retry", 3200),
            ("asr_empty", "✕ 识别为空", "✕ Empty transcript", 2600),
            ("paste_failed_retry", "✕ 填入失败，已存录音可重试", "✕ Insert failed, audio saved for retry", 3200),
            ("paste_failed", "✕ 填入失败", "✕ Failed to insert text", 2600),
        ]
        for state, zh_txt, en_txt, dur in expected:
            self.assertEqual(format_hud_text(state, "zh"), zh_txt)
            self.assertEqual(format_hud_text(state, "en"), en_txt)
            self.assertEqual(HUD_STATE_DURATIONS_MS[state], dur)
            self.assertEqual(default_hud_duration_ms(state), dur)
            self.assertEqual(hud_layer_for_state(state), "toast")
            self.assertTrue(is_toast_hud_state(state))

    def test_four_delivery_outcomes_unified_across_daemon_hud_telemetry_and_diagnose(self) -> None:
        wav = _voiced_wav(0.6)
        cfg = Config(refiner=RefinerConfig(provider="off"))

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            rt = base / "rt"
            rt.mkdir(mode=0o700)
            env = {"HOME": str(base / "home"), "PATH": ""}

            def _run_case(
                *,
                asr_text: str,
                paste_outcome: PasteOutcome,
                fail_save: bool = False,
            ) -> tuple[dict[str, object], list[str]]:
                plat = _StubPlatform(wav, paste_outcome=paste_outcome)
                d = VoiceInputDaemon(
                    cfg,
                    env=env,
                    runtime_dir=rt,
                    platform=plat,
                    asr_provider=_SequenceAsr([asr_text]),
                    refiner=create_refiner(cfg),
                    tracker=_isolated_tracker(base),
                )
                try:
                    ctx_mgr = (
                        mock.patch("agent_voice_input.daemon.save_failed_audio", return_value=None)
                        if fail_save
                        else mock.patch("agent_voice_input.daemon._LOG_ROTATE_CHECK_INTERVAL_SEC", 60.0)
                    )
                    with ctx_mgr:
                        d.handle_trigger("toggle", plat.fg)
                        d._last_trigger_ts = -1e9
                        r = d.handle_trigger("toggle", plat.fg)
                        last = _wait_for_seq(d, int(r["seq_id"]))
                    return last, list(plat.huds)
                finally:
                    d.close()

            # 1. Pasted
            r1, h1 = _run_case(
                asr_text="已填入文本",
                paste_outcome=PasteOutcome(True, True, "ok", "WindowsTerminal", True),
            )
            self.assertTrue(r1["ok"])
            self.assertTrue(r1["pasted"])
            self.assertEqual(r1["delivery_status"], "pasted")
            self.assertEqual(h1[-1], "done")

            # 2. Copied (window switched -> clipboard ready)
            r2, h2 = _run_case(
                asr_text="已复制文本",
                paste_outcome=PasteOutcome(False, True, "focus_changed", "chrome", False),
            )
            self.assertTrue(r2["ok"])
            self.assertFalse(r2["pasted"])
            self.assertTrue(r2["clipboard"])
            self.assertEqual(r2["delivery_status"], "copied")
            self.assertEqual(h2[-1], "copied")

            # 3a. Undelivered-recoverable: paste + clipboard both fail, WAV saved
            r3a, h3a = _run_case(
                asr_text="粘贴剪贴板全败",
                paste_outcome=PasteOutcome(False, False, "clipboard_failed", "WindowsTerminal", False),
            )
            self.assertFalse(r3a["ok"])
            self.assertEqual(r3a["skipped_reason"], "clipboard_failed")
            self.assertEqual(r3a["delivery_status"], "undelivered_recoverable")
            self.assertTrue(Path(str(r3a["saved_audio"])).is_file())
            self.assertEqual(h3a[-1], "paste_failed_retry")

            # 3b. Undelivered-recoverable: gated speech -> empty ASR, WAV saved (NOT 'empty'!)
            r3b, h3b = _run_case(
                asr_text="",
                paste_outcome=PasteOutcome(True, True, "ok", "WindowsTerminal", True),
            )
            self.assertFalse(r3b["ok"])
            self.assertEqual(r3b["skipped_reason"], "empty_asr")
            self.assertEqual(r3b["delivery_status"], "undelivered_recoverable")
            self.assertTrue(Path(str(r3b["saved_audio"])).is_file())
            self.assertEqual(h3b[-1], "asr_empty_retry")

            # 4a. Undelivered-unrecoverable: paste + clipboard fail AND ENOSPC prevents saving WAV
            r4a, h4a = _run_case(
                asr_text="无盘空间粘贴失败",
                paste_outcome=PasteOutcome(False, False, "clipboard_failed", "WindowsTerminal", False),
                fail_save=True,
            )
            self.assertFalse(r4a["ok"])
            self.assertEqual(r4a["delivery_status"], "undelivered_unrecoverable")
            self.assertNotIn("saved_audio", r4a)
            self.assertEqual(h4a[-1], "paste_failed")

            # 4b. Undelivered-unrecoverable: gated speech -> empty ASR AND ENOSPC prevents saving WAV
            r4b, h4b = _run_case(
                asr_text="",
                paste_outcome=PasteOutcome(True, True, "ok", "WindowsTerminal", True),
                fail_save=True,
            )
            self.assertFalse(r4b["ok"])
            self.assertEqual(r4b["skipped_reason"], "empty_asr")
            self.assertEqual(r4b["delivery_status"], "undelivered_unrecoverable")
            self.assertNotIn("saved_audio", r4b)
            self.assertEqual(h4b[-1], "asr_empty")

            # Legacy buggy record compatibility + unknown delivery_status sanitization
            append_telemetry_record(
                rt,
                {
                    "ts": round(time.time(), 6),
                    "seq_id": 99,
                    "ok": True,
                    "pasted": False,
                    "paste_reason": "clipboard_failed",
                    "delivery_status": "UNTRUSTED_INJECTED_VALUE",
                    "secret_prose": "MUST_BE_STRIPPED",
                },
            )

            diag = collect_live_diagnostics(cfg, env, runtime_dir=rt, last=10)
            # Only r1 (pasted) + r2 (copied) count as ok_count=2; r3a, r3b, r4a, r4b, legacy(99) -> 5 failed_or_skipped!
            self.assertEqual(diag["summary"]["total"], 7)
            self.assertEqual(diag["summary"]["ok_count"], 2)
            self.assertEqual(diag["summary"]["failed_or_skipped"], 5)

            raw_json = json.dumps(diag, ensure_ascii=False)
            self.assertNotIn("MUST_BE_STRIPPED", raw_json)
            self.assertNotIn("UNTRUSTED_INJECTED_VALUE", raw_json)
            legacy_clean = diag["recent_results"][-1]
            self.assertEqual(legacy_clean["delivery_status"], "")
            self.assertEqual(legacy_clean["diag_flags"], ["SKIP(clipboard_failed)"])

            # Markdown renders cleanly without leaking prose
            md = format_live_diagnostics_markdown(diag)
            self.assertIn("`2/7` ok", md)
            self.assertIn("failed/skipped=`5`", md)

            # Direct unit check on diagnose_record_flags / summarize_telemetry_records
            self.assertEqual(
                diagnose_record_flags({"ok": True, "pasted": False, "clipboard": False, "paste_reason": "paste_failed"}, is_first_for_session=True),
                ["SKIP(paste_failed)"],
            )
            self.assertEqual(
                summarize_telemetry_records([{"ok": True, "pasted": False, "clipboard": False, "paste_reason": "clipboard_failed"}])["ok_count"],
                0,
            )

    def test_a5_partial_asr_copied_delivery_exception_and_retry_re_failure_recoverability(self) -> None:
        cfg = Config(refiner=RefinerConfig(provider="off"))
        voiced_4_samples: list[int] = []
        for idx in range(4):
            voiced_4_samples.extend(_tone_samples(0.65, amp=9000))
            if idx < 3:
                voiced_4_samples.extend(_silence_samples(0.45))
        voiced_4_wav = pack_pcm16_mono_wav(voiced_4_samples, sample_rate=16000)

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            rt = base / "rt"
            rt.mkdir(mode=0o700)
            env = {"HOME": str(base / "home"), "PATH": ""}

            # 1. Partial ASR + copied (window switched): MUST show combined copied+partial HUD (not hide copied!)
            for fail_save, expected_hud in ((False, "partial_asr_copied"), (True, "partial_asr_copied_unsaved")):
                plat_pc = _StubPlatform(
                    voiced_4_wav,
                    paste_outcome=PasteOutcome(False, True, "focus_changed", "chrome", False),
                )
                d_pc = VoiceInputDaemon(
                    cfg,
                    env=env,
                    runtime_dir=rt,
                    platform=plat_pc,
                    asr_provider=_SequenceAsr(["开头", "", "", "结尾"]),
                    refiner=create_refiner(cfg),
                    tracker=_isolated_tracker(base),
                )
                try:
                    save_cm = (
                        mock.patch("agent_voice_input.daemon.save_failed_audio", return_value=None)
                        if fail_save
                        else mock.patch("agent_voice_input.daemon._LOG_ROTATE_CHECK_INTERVAL_SEC", 60.0)
                    )
                    with mock.patch("agent_voice_input.daemon.DEFAULT_MAX_CHUNK_SEC", 1.1), save_cm:
                        d_pc.handle_trigger("toggle", plat_pc.fg)
                        d_pc._last_trigger_ts = -1e9
                        r = d_pc.handle_trigger("toggle", plat_pc.fg)
                        last = _wait_for_seq(d_pc, int(r["seq_id"]))
                    self.assertTrue(last["ok"])
                    self.assertTrue(last["clipboard"])
                    self.assertFalse(last["pasted"])
                    self.assertEqual(last["delivery_status"], "copied")
                    self.assertEqual(last["asr_missed_chunks"], 2)
                    self.assertEqual(plat_pc.huds[-1], expected_hud)
                finally:
                    d_pc.close()

            # 2. Delivery exception (e.g. WSL paste RPC timeout): HUD must be paste_failed_retry / paste_failed (NOT 'error'!)
            class _RaisingDeliverPlatform(_StubPlatform):
                def deliver_text(self, text: str, **kwargs: object) -> PasteOutcome:
                    del text, kwargs
                    raise RuntimeError("wsl_paste_rpc_timeout")

            for fail_save, exp_hud, exp_ds in (
                (False, "paste_failed_retry", "undelivered_recoverable"),
                (True, "paste_failed", "undelivered_unrecoverable"),
            ):
                plat_exc = _RaisingDeliverPlatform(_voiced_wav(0.6))
                d_exc = VoiceInputDaemon(
                    cfg,
                    env=env,
                    runtime_dir=rt,
                    platform=plat_exc,
                    asr_provider=_SequenceAsr(["转写成功但粘贴抛异常"]),
                    refiner=create_refiner(cfg),
                    tracker=_isolated_tracker(base),
                )
                try:
                    save_cm = (
                        mock.patch("agent_voice_input.daemon.save_failed_audio", return_value=None)
                        if fail_save
                        else mock.patch("agent_voice_input.daemon._LOG_ROTATE_CHECK_INTERVAL_SEC", 60.0)
                    )
                    with save_cm:
                        d_exc.handle_trigger("toggle", plat_exc.fg)
                        d_exc._last_trigger_ts = -1e9
                        r = d_exc.handle_trigger("toggle", plat_exc.fg)
                        last = _wait_for_seq(d_exc, int(r["seq_id"]))
                    self.assertFalse(last["ok"])
                    self.assertEqual(last["skipped_reason"], "delivery_error")
                    self.assertEqual(last["paste_reason"], "delivery_error")
                    self.assertEqual(last["delivery_status"], exp_ds)
                    self.assertIn("wsl_paste_rpc_timeout", str(last.get("error") or ""))
                    self.assertEqual(plat_exc.huds[-1], exp_hud)
                finally:
                    d_exc.close()

            # 3. `retry` failing delivery again stays undelivered_recoverable while original failed/ WAV is valid & unexpired
            rt_retry = base / "rt_retry"
            rt_retry.mkdir(mode=0o700)
            orig_wav = save_failed_audio(_voiced_wav(0.6), rt_retry, duration_sec=2.0, label="retry-orig")
            assert orig_wav is not None

            res_retry_fail = run_pipeline_once(
                orig_wav,
                cfg=cfg,
                tracker=_isolated_tracker(base),
                asr_provider=_SequenceAsr(["重试再次粘贴失败"]),
                refiner=create_refiner(cfg),
                deliver_fn=lambda txt, win: PasteOutcome(False, False, "clipboard_failed", "WindowsTerminal", False),
                audio_source="retry",
                runtime_dir=rt_retry,
                env=env,
            )
            self.assertFalse(res_retry_fail.ok)
            self.assertEqual(res_retry_fail.saved_audio, str(orig_wav))
            self.assertEqual(list_failed_audios(rt_retry), [orig_wav], "retry must not duplicate failed/ WAV")
            sum_rf = build_pipeline_summary(1, res_retry_fail)
            self.assertEqual(sum_rf["delivery_status"], "undelivered_recoverable")

            # Daemon _stash_task_wav_once also preserves recoverability for retry tasks without duplicating
            plat_rt = _StubPlatform(
                _voiced_wav(0.6),
                paste_outcome=PasteOutcome(False, False, "clipboard_failed", "WindowsTerminal", False),
            )
            d_rt = VoiceInputDaemon(
                cfg,
                env=env,
                runtime_dir=rt_retry,
                platform=plat_rt,
                asr_provider=_SequenceAsr(["重试再次失败"]),
                refiner=create_refiner(cfg),
                tracker=_isolated_tracker(base),
            )
            try:
                t_retry = _QueuedUtterance(
                    seq_id=7,
                    raw_wav=orig_wav.read_bytes(),
                    audio_source="retry",
                    origin_win=plat_rt.fg,
                    saved_audio=str(orig_wav),
                )
                with d_rt._queue_cv:
                    d_rt._enqueue_utterance_locked(t_retry)
                last_rt = _wait_for_seq(d_rt, 7)
                self.assertFalse(last_rt["ok"])
                self.assertEqual(last_rt["delivery_status"], "undelivered_recoverable")
                self.assertEqual(last_rt["saved_audio"], str(orig_wav))
                self.assertEqual(plat_rt.huds[-1], "paste_failed_retry")
                self.assertEqual(list_failed_audios(rt_retry), [orig_wav])
            finally:
                d_rt.close()

            # Once original failed/ WAV expires (>24h), retry failure accurately becomes undelivered_unrecoverable
            old_ts = time.time() - FAILED_AUDIO_MAX_AGE_SEC - 60.0
            os.utime(orig_wav, (old_ts, old_ts))
            res_retry_exp = run_pipeline_once(
                orig_wav,
                cfg=cfg,
                tracker=_isolated_tracker(base),
                asr_provider=_SequenceAsr(["已过期重试失败"]),
                refiner=create_refiner(cfg),
                deliver_fn=lambda txt, win: PasteOutcome(False, False, "clipboard_failed", "WindowsTerminal", False),
                audio_source="retry",
                runtime_dir=rt_retry,
                env=env,
            )
            self.assertFalse(res_retry_exp.ok)
            self.assertEqual(res_retry_exp.saved_audio, "")
            self.assertEqual(build_pipeline_summary(2, res_retry_exp)["delivery_status"], "undelivered_unrecoverable")

    def test_a5_cli_retry_and_run_pipeline_once_delivery_exception_telemetry(self) -> None:
        cfg = Config(refiner=RefinerConfig(provider="off"))

        class _BoomPlat(_StubPlatform):
            def deliver_text(self, text: str, **kwargs: object) -> PasteOutcome:
                del text, kwargs
                raise RuntimeError("cli_paste_rpc_boom")

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            rt = base / "rt"
            rt.mkdir(mode=0o700)
            home = base / "home"
            home.mkdir(mode=0o700)
            env = {"HOME": str(home), "PATH": "", "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)}

            orig_wav = save_failed_audio(_voiced_wav(0.6), rt, duration_sec=2.0, label="retry-boom")
            assert orig_wav is not None

            # 1. Direct run_pipeline_once delivery exception annotates _delivery_status & _saved_audio_path
            with self.assertRaises(RuntimeError) as ctx_rt:
                run_pipeline_once(
                    orig_wav,
                    cfg=cfg,
                    tracker=_isolated_tracker(base),
                    asr_provider=_SequenceAsr(["投递抛异常文本"]),
                    refiner=create_refiner(cfg),
                    deliver_fn=lambda txt, win: (_ for _ in ()).throw(RuntimeError("deliver_boom")),
                    audio_source="retry",
                    runtime_dir=rt,
                    env=env,
                )
            self.assertEqual(getattr(ctx_rt.exception, "_delivery_status", ""), "undelivered_recoverable")
            self.assertEqual(getattr(ctx_rt.exception, "_saved_audio_path", ""), str(orig_wav))
            self.assertEqual(getattr(ctx_rt.exception, "_skipped_reason", ""), "delivery_error")
            self.assertEqual(getattr(ctx_rt.exception, "_paste_reason", ""), "delivery_error")

            # Non-saveable file source delivery exception -> undelivered_unrecoverable
            ext_wav = base / "ext_boom.wav"
            ext_wav.write_bytes(_voiced_wav(0.6))
            with self.assertRaises(RuntimeError) as ctx_file:
                run_pipeline_once(
                    ext_wav,
                    cfg=cfg,
                    tracker=_isolated_tracker(base),
                    asr_provider=_SequenceAsr(["单次文件抛异常"]),
                    refiner=create_refiner(cfg),
                    deliver_fn=lambda txt, win: (_ for _ in ()).throw(RuntimeError("deliver_file_boom")),
                    audio_source="file",
                    runtime_dir=rt,
                    env=env,
                )
            self.assertEqual(getattr(ctx_file.exception, "_delivery_status", ""), "undelivered_unrecoverable")
            self.assertFalse(getattr(ctx_file.exception, "_saved_audio_path", ""))

            # 2. CLI `retry` delivery exception writes unified telemetry (delivery_status=undelivered_recoverable)
            err_buf = io.StringIO()
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("agent_voice_input.daemon.create_platform", return_value=_BoomPlat(_voiced_wav(0.6))),
                mock.patch("agent_voice_input.daemon.create_asr_provider", return_value=_SequenceAsr(["CLI重试投递抛错"])),
                redirect_stderr(err_buf),
            ):
                rc_retry = cli_mod.main(["retry", "--refiner", "off"])
            self.assertEqual(rc_retry, 1)
            self.assertIn("saved failed audio", err_buf.getvalue())

            # 3. CLI `once --file` delivery exception writes delivery_status=undelivered_unrecoverable (no retry claim)
            err_once = io.StringIO()
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("agent_voice_input.daemon.create_platform", return_value=_BoomPlat(_voiced_wav(0.6))),
                mock.patch("agent_voice_input.daemon.create_asr_provider", return_value=_SequenceAsr(["CLI文件投递抛错"])),
                redirect_stderr(err_once),
            ):
                rc_once = cli_mod.main(["once", "--file", str(ext_wav), "--refiner", "off"])
            self.assertEqual(rc_once, 1)
            self.assertNotIn("saved failed audio", err_once.getvalue())

            diag = collect_live_diagnostics(cfg, env, runtime_dir=rt, last=5)
            self.assertEqual(diag["summary"]["total"], 2)
            self.assertEqual(diag["summary"]["ok_count"], 0)
            self.assertEqual(diag["summary"]["failed_or_skipped"], 2)
            rec_retry, rec_once = diag["recent_results"][-2], diag["recent_results"][-1]
            self.assertEqual(rec_retry["delivery_status"], "undelivered_recoverable")
            self.assertEqual(rec_retry["skipped_reason"], "delivery_error")
            self.assertEqual(rec_retry["paste_reason"], "delivery_error")
            self.assertTrue(rec_retry["has_saved_audio"])
            self.assertEqual(rec_retry["diag_flags"], ["SKIP(delivery_error)"])
            self.assertEqual(rec_once["delivery_status"], "undelivered_unrecoverable")
            self.assertEqual(rec_once["skipped_reason"], "delivery_error")
            self.assertFalse(rec_once["has_saved_audio"])


if __name__ == "__main__":
    unittest.main()
