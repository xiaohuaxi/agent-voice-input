"""16 kHz mono WAV decoding, -50 dBFS silence gating, and -20 dBFS RMS normalization.

Pure Python 3.10+ standard library implementation (zero third-party dependencies).
"""
from __future__ import annotations

import array
from dataclasses import dataclass
import io
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Sequence
import wave

from .config import AudioConfig

TARGET_SAMPLE_RATE = 16000
TARGET_CHANNELS = 1
TARGET_SAMPLE_WIDTH = 2  # 16-bit signed little-endian PCM
PEAK_LIMIT_RATIO = 0.99
PEAK_LIMIT_INT16 = int(round(PEAK_LIMIT_RATIO * 32767))  # 32440


class AudioError(RuntimeError):
    """Raised when audio decoding or ffmpeg conversion fails."""


@dataclass(frozen=True)
class AudioGateResult:
    passed: bool
    reason: str  # "ok" | "too_short" | "silence"
    wav_bytes: bytes
    duration_sec: float
    voiced_sec: float
    rms_dbfs: float
    gain_db: float
    all_zero: bool = False


def resolve_ffmpeg_bin(preferred: str = "/usr/bin/ffmpeg") -> str:
    if preferred and os.path.isfile(preferred) and os.access(preferred, os.X_OK):
        return preferred
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise AudioError("ffmpeg binary not found (expected /usr/bin/ffmpeg or in PATH)")


def pack_pcm16_mono_wav(
    pcm_or_samples: bytes | bytearray | memoryview | array.array[int] | Sequence[int],
    *,
    sample_rate: int = TARGET_SAMPLE_RATE,
) -> bytes:
    """Pack little-endian s16le PCM bytes or native int16 samples into a valid RIFF WAV.

    Note: Python's ``wave.Wave_write.writeframes`` expects native-endian sample bytes and
    internally byte-swaps to little-endian on big-endian hosts.
    """
    if isinstance(pcm_or_samples, (bytes, bytearray, memoryview)):
        raw_le = bytes(pcm_or_samples)
        if len(raw_le) % 2 != 0:
            raw_le = raw_le[:-1]
        if sys.byteorder != "little":
            arr = array.array("h")
            arr.frombytes(raw_le)
            arr.byteswap()
            native_pcm = arr.tobytes()
        else:
            native_pcm = raw_le
    else:
        arr = (
            pcm_or_samples
            if isinstance(pcm_or_samples, array.array) and pcm_or_samples.typecode == "h"
            else array.array("h", pcm_or_samples)
        )
        native_pcm = arr.tobytes()

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(TARGET_CHANNELS)
        wf.setsampwidth(TARGET_SAMPLE_WIDTH)
        wf.setframerate(sample_rate)
        wf.writeframes(native_pcm)
    return buf.getvalue()


def _try_extract_fast_16k_mono_pcm(raw_bytes: bytes) -> bytes | None:
    """Directly extract PCM s16le frames if ``raw_bytes`` is already canonical 16k/mono/s16 WAV.

    Note: Python 3.10 ``wave.Wave_read.readframes`` converts 16-bit WAV samples into host
    endianness on big-endian machines, whereas ``ffmpeg -f s16le`` always emits little-endian
    ``s16le``. On big-endian hosts we swap back so ``decode_to_16k_mono_pcm`` always returns
    canonical ``s16le`` bytes.
    """
    if len(raw_bytes) < 44 or raw_bytes[:4] != b"RIFF" or raw_bytes[8:12] != b"WAVE":
        return None
    try:
        with wave.open(io.BytesIO(raw_bytes), "rb") as wf:
            if (
                wf.getnchannels() != TARGET_CHANNELS
                or wf.getsampwidth() != TARGET_SAMPLE_WIDTH
                or wf.getframerate() != TARGET_SAMPLE_RATE
                or wf.getcomptype() != "NONE"
            ):
                return None
            nframes = wf.getnframes()
            pcm = wf.readframes(nframes)
            if len(pcm) == 0 and len(raw_bytes) > 64 and nframes == 0:
                return None
            if len(pcm) % 2 != 0:
                pcm = pcm[:-1]
            if sys.byteorder != "little" and pcm:
                arr = array.array("h")
                arr.frombytes(pcm)
                arr.byteswap()
                pcm = arr.tobytes()
            return pcm
    except (wave.Error, EOFError):
        return None


def _decode_pcm16_via_ffmpeg(
    source_path: Path | None,
    raw_bytes: bytes | None,
    *,
    ffmpeg_bin: str = "/usr/bin/ffmpeg",
    timeout_sec: float = 15.0,
) -> bytes:
    exe = resolve_ffmpeg_bin(ffmpeg_bin)
    input_spec = str(source_path.resolve()) if source_path is not None else "pipe:0"
    cmd = [
        exe,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        input_spec,
        "-ac",
        str(TARGET_CHANNELS),
        "-ar",
        str(TARGET_SAMPLE_RATE),
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "pipe:1",
    ]
    try:
        proc = subprocess.run(
            cmd,
            input=None if source_path is not None else raw_bytes,
            stdin=subprocess.DEVNULL if source_path is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise AudioError(f"ffmpeg decode timed out after {timeout_sec}s") from None
    except OSError as exc:
        raise AudioError(f"failed to execute ffmpeg: {exc}") from None

    if proc.returncode != 0:
        err_msg = proc.stderr.decode("utf-8", errors="replace").strip()
        raise AudioError(f"ffmpeg decode failed (rc={proc.returncode}): {err_msg}")

    pcm = proc.stdout
    if len(pcm) % 2 != 0:
        pcm = pcm[:-1]
    return pcm


def decode_to_16k_mono_pcm(
    wav_input: bytes | bytearray | memoryview | str | Path,
    *,
    ffmpeg_bin: str = "/usr/bin/ffmpeg",
    timeout_sec: float = 15.0,
) -> bytes:
    """Decode WAV bytes or an audio file path into raw 16 kHz mono s16le PCM bytes."""
    source_path: Path | None = None
    if isinstance(wav_input, (str, Path)):
        source_path = Path(wav_input)
        try:
            raw_bytes = source_path.read_bytes()
        except OSError as exc:
            raise AudioError(f"cannot read audio file {source_path}: {exc}") from None
    elif isinstance(wav_input, (bytes, bytearray, memoryview)):
        raw_bytes = bytes(wav_input)
    else:
        raise TypeError(f"unsupported wav_input type: {type(wav_input).__name__}")

    if not raw_bytes:
        return b""

    fast_pcm = _try_extract_fast_16k_mono_pcm(raw_bytes)
    if fast_pcm is not None:
        return fast_pcm

    return _decode_pcm16_via_ffmpeg(
        source_path,
        raw_bytes if source_path is None else None,
        ffmpeg_bin=ffmpeg_bin,
        timeout_sec=timeout_sec,
    )


def ensure_16k_mono_s16_wav(
    wav_input: bytes | bytearray | memoryview | str | Path,
    *,
    ffmpeg_bin: str = "/usr/bin/ffmpeg",
    timeout_sec: float = 15.0,
) -> bytes:
    """Return canonical 16 kHz 16-bit mono PCM WAV bytes without level scaling."""
    pcm = decode_to_16k_mono_pcm(wav_input, ffmpeg_bin=ffmpeg_bin, timeout_sec=timeout_sec)
    return pack_pcm16_mono_wav(pcm, sample_rate=TARGET_SAMPLE_RATE)


def process_wav(
    wav_input: bytes | bytearray | memoryview | str | Path,
    audio_cfg: AudioConfig | None = None,
    *,
    floor_dbfs: float | None = None,
    target_dbfs: float | None = None,
    max_gain_db: float | None = None,
    min_duration_sec: float | None = None,
    min_voiced_sec: float | None = None,
    ffmpeg_bin: str = "/usr/bin/ffmpeg",
    timeout_sec: float = 15.0,
) -> AudioGateResult:
    """Gate silence/short clips and normalize voiced RMS to ``target_dbfs`` (-20 dBFS)."""
    cfg = audio_cfg or AudioConfig()
    eff_floor = cfg.floor_dbfs if floor_dbfs is None else floor_dbfs
    eff_target = cfg.target_dbfs if target_dbfs is None else target_dbfs
    eff_max_gain = cfg.max_gain_db if max_gain_db is None else max_gain_db
    eff_min_dur = cfg.min_duration_sec if min_duration_sec is None else min_duration_sec
    eff_min_voiced = cfg.min_voiced_sec if min_voiced_sec is None else min_voiced_sec

    pcm_bytes = decode_to_16k_mono_pcm(
        wav_input, ffmpeg_bin=ffmpeg_bin, timeout_sec=timeout_sec
    )
    samples = array.array("h")
    samples.frombytes(pcm_bytes)
    if sys.byteorder != "little":
        samples.byteswap()

    total_samples = len(samples)
    duration_sec = total_samples / float(TARGET_SAMPLE_RATE)
    all_zero = total_samples > 0 and all(s == 0 for s in samples)

    thr = 10.0 ** (eff_floor / 20.0)
    thr_int = thr * 32768.0
    voiced = [s for s in samples if abs(s) > thr_int]
    voiced_count = len(voiced)
    voiced_sec = voiced_count / float(TARGET_SAMPLE_RATE)

    if duration_sec < eff_min_dur:
        return AudioGateResult(
            passed=False,
            reason="too_short",
            wav_bytes=b"",
            duration_sec=duration_sec,
            voiced_sec=voiced_sec,
            rms_dbfs=-200.0,
            gain_db=0.0,
            all_zero=all_zero,
        )

    if voiced_sec < eff_min_voiced or voiced_count == 0:
        return AudioGateResult(
            passed=False,
            reason="silence",
            wav_bytes=b"",
            duration_sec=duration_sec,
            voiced_sec=voiced_sec,
            rms_dbfs=-200.0,
            gain_db=0.0,
            all_zero=all_zero,
        )

    mean_sq = sum(s * s for s in voiced) / (voiced_count * (32768.0 * 32768.0))
    rms = math.sqrt(mean_sq)
    rms_dbfs = 20.0 * math.log10(max(rms, 1e-10))
    gain_db = min(eff_target - rms_dbfs, eff_max_gain)
    scale = 10.0 ** (gain_db / 20.0)

    hi = PEAK_LIMIT_INT16
    lo = -PEAK_LIMIT_INT16
    out_samples = array.array(
        "h",
        (
            hi
            if (scaled := int(round(s * scale))) > hi
            else (lo if scaled < lo else scaled)
            for s in samples
        ),
    )

    out_wav = pack_pcm16_mono_wav(out_samples, sample_rate=TARGET_SAMPLE_RATE)
    return AudioGateResult(
        passed=True,
        reason="ok",
        wav_bytes=out_wav,
        duration_sec=duration_sec,
        voiced_sec=voiced_sec,
        rms_dbfs=rms_dbfs,
        gain_db=gain_db,
        all_zero=False,
    )
