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
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Iterable, Sequence
import wave

from .config import AudioConfig, ensure_secure_dir

TARGET_SAMPLE_RATE = 16000
TARGET_CHANNELS = 1
TARGET_SAMPLE_WIDTH = 2  # 16-bit signed little-endian PCM
PEAK_LIMIT_RATIO = 0.99
PEAK_LIMIT_INT16 = int(round(PEAK_LIMIT_RATIO * 32767))  # 32440

DEFAULT_MAX_CHUNK_SEC = 180.0
DEFAULT_SEARCH_WINDOW_SEC = 45.0
_SPLIT_FRAME_SAMPLES = 320  # 20 ms @ 16 kHz (50 fps)

FAILED_AUDIO_MAX_FILES = 10
FAILED_AUDIO_MAX_AGE_SEC = 24 * 3600.0
_FAILED_LABEL_CLEAN_RE = re.compile(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+")


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
    timeout_sec: float = 30.0,
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
    timeout_sec: float = 30.0,
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
    timeout_sec: float = 30.0,
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
    timeout_sec: float = 30.0,
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


@dataclass(frozen=True)
class WavChunkInfo:
    index: int
    start_sec: float
    end_sec: float
    duration_sec: float
    cut_reason: str  # "single" | "pause_300ms" | "pause_100ms" | "valley" | "tail"
    cut_dbfs: float
    silence_dbfs: float
    wav_bytes: bytes


def _pick_cut_frame(
    frame_db: Sequence[float],
    win_start: int,
    win_end: int,
    silence_db: float,
) -> tuple[int, str, float]:
    """Pick the cleanest pause frame inside ``[win_start, win_end)`` (300ms -> 100ms -> energy valley)."""
    silent_runs: list[tuple[int, int]] = []
    run_s = -1
    for i in range(win_start, win_end):
        if frame_db[i] <= silence_db:
            if run_s < 0:
                run_s = i
        elif run_s >= 0:
            silent_runs.append((run_s, i))
            run_s = -1
    if run_s >= 0:
        silent_runs.append((run_s, win_end))

    for min_run, reason in ((15, "pause_300ms"), (5, "pause_100ms")):
        cands = [seg for seg in silent_runs if (seg[1] - seg[0]) >= min_run]
        if cands:
            # Once a pause is >=700ms (35 frames, >=350ms silence on each side), prefer later sentence stops near win_end
            best_s, best_e = max(cands, key=lambda seg: (min(seg[1] - seg[0], 35), seg[1]))
            mid = max(win_start, min(win_end, (best_s + best_e) // 2))
            avg_db = sum(frame_db[best_s:best_e]) / max(1, best_e - best_s)
            return mid, reason, avg_db

    # Continuous rapid speech fallback: 160 ms (8-frame) minimum-energy valley across the full search window
    span = win_end - win_start
    if span <= 0:
        return win_end, "valley", frame_db[min(win_end, len(frame_db) - 1)]
    w = min(8, span)
    cur_sum = sum(frame_db[win_start : win_start + w])
    best_sum, best_idx = cur_sum, win_start
    for idx in range(win_start + 1, win_end - w + 1):
        cur_sum += frame_db[idx + w - 1] - frame_db[idx - 1]
        if cur_sum <= best_sum:
            best_sum, best_idx = cur_sum, idx
    mid = max(win_start, min(win_end, best_idx + w // 2))
    return mid, "valley", best_sum / float(w)


def inspect_wav_chunks(
    wav_input: bytes | bytearray | memoryview | str | Path,
    *,
    max_chunk_sec: float = DEFAULT_MAX_CHUNK_SEC,
    search_window_sec: float = DEFAULT_SEARCH_WINDOW_SEC,
    floor_dbfs: float = -50.0,
) -> list[WavChunkInfo]:
    """Split long 16 kHz mono WAV audio at natural pauses and return full diagnostic metadata + WAV bytes."""
    raw_bytes = bytes(wav_input) if isinstance(wav_input, (bytes, bytearray, memoryview)) else None
    fast_pcm = _try_extract_fast_16k_mono_pcm(raw_bytes) if raw_bytes is not None else None
    pcm_bytes = fast_pcm if fast_pcm is not None else decode_to_16k_mono_pcm(wav_input)
    if not pcm_bytes:
        return []

    samples = array.array("h")
    samples.frombytes(pcm_bytes)
    if sys.byteorder != "little":
        samples.byteswap()

    total_samples = len(samples)
    total_sec = total_samples / float(TARGET_SAMPLE_RATE)
    max_samples = max(_SPLIT_FRAME_SAMPLES * 5, int(round(max_chunk_sec * TARGET_SAMPLE_RATE)))
    if total_samples <= max_samples:
        single_wav = (
            raw_bytes
            if (raw_bytes is not None and fast_pcm is not None)
            else pack_pcm16_mono_wav(samples, sample_rate=TARGET_SAMPLE_RATE)
        )
        return [
            WavChunkInfo(
                index=0,
                start_sec=0.0,
                end_sec=total_sec,
                duration_sec=total_sec,
                cut_reason="single",
                cut_dbfs=floor_dbfs,
                silence_dbfs=floor_dbfs,
                wav_bytes=single_wav,
            )
        ]

    fs = _SPLIT_FRAME_SAMPLES
    full_scale_sq = 32768.0 * 32768.0
    frame_db: list[float] = []
    for off in range(0, total_samples, fs):
        blk = samples[off : off + fs]
        if not blk:
            break
        msq = sum(s * s for s in blk) / (len(blk) * full_scale_sq)
        frame_db.append(10.0 * math.log10(msq) if msq > 1e-12 else -120.0)

    n_frames = len(frame_db)
    sorted_db = sorted(frame_db)
    p08_db = sorted_db[int(0.08 * (n_frames - 1))]
    p85_db = sorted_db[int(0.85 * (n_frames - 1))]
    upper_cap = min(-26.0, max(floor_dbfs, p85_db - 8.0))
    silence_db = min(upper_cap, max(floor_dbfs, p08_db + 6.0))

    max_frames = max(5, max_samples // fs)
    lookback_frames = min(max(1, int(round(search_window_sec * 50.0))), max(1, int(max_frames * 0.35)))
    min_tail_frames = min(250, max(5, max_frames // 6))

    ranges: list[tuple[int, int, str, float]] = []
    cur = 0
    while cur < n_frames:
        rem = n_frames - cur
        if rem <= max_frames:
            tail_db = frame_db[-1] if frame_db else silence_db
            ranges.append((cur, n_frames, "tail", tail_db))
            break
        eff_min_tail = min(min_tail_frames, rem // 2)
        win_end = min(cur + max_frames, n_frames - eff_min_tail)
        win_start = max(cur + 1, win_end - lookback_frames)
        cut, reason, cut_db = _pick_cut_frame(frame_db, win_start, win_end, silence_db)
        if cut <= cur:
            cut = win_end
        ranges.append((cur, cut, reason, cut_db))
        cur = cut

    out_infos: list[WavChunkInfo] = []
    for idx, (f_start, f_end, reason, cut_db) in enumerate(ranges):
        s_idx = f_start * fs
        e_idx = total_samples if idx == len(ranges) - 1 else min(total_samples, f_end * fs)
        if e_idx > s_idx:
            s_sec = s_idx / float(TARGET_SAMPLE_RATE)
            e_sec = e_idx / float(TARGET_SAMPLE_RATE)
            out_infos.append(
                WavChunkInfo(
                    index=len(out_infos),
                    start_sec=s_sec,
                    end_sec=e_sec,
                    duration_sec=e_sec - s_sec,
                    cut_reason=reason,
                    cut_dbfs=cut_db,
                    silence_dbfs=silence_db,
                    wav_bytes=pack_pcm16_mono_wav(samples[s_idx:e_idx], sample_rate=TARGET_SAMPLE_RATE),
                )
            )
    return out_infos


def split_wav_chunks(
    wav_input: bytes | bytearray | memoryview | str | Path,
    *,
    max_chunk_sec: float = DEFAULT_MAX_CHUNK_SEC,
    search_window_sec: float = DEFAULT_SEARCH_WINDOW_SEC,
    floor_dbfs: float = -50.0,
) -> list[bytes]:
    """Split long 16 kHz mono WAV audio at natural silent pauses so every chunk is <= ``max_chunk_sec``."""
    return [
        c.wav_bytes
        for c in inspect_wav_chunks(
            wav_input,
            max_chunk_sec=max_chunk_sec,
            search_window_sec=search_window_sec,
            floor_dbfs=floor_dbfs,
        )
    ]


def get_failed_audio_dir(runtime_dir: Path, *, create: bool = False) -> Path:
    if create:
        base = ensure_secure_dir(Path(runtime_dir))
        return ensure_secure_dir(base / "failed")
    return Path(runtime_dir) / "failed"


def _sanitize_failed_label(raw: str) -> str:
    cleaned = _FAILED_LABEL_CLEAN_RE.sub("-", (raw or "").strip()).strip("-_")
    return cleaned[:32]


def prune_failed_audio_dir(
    failed_dir: Path,
    *,
    now_ts: float | None = None,
    max_files: int = FAILED_AUDIO_MAX_FILES,
    max_age_sec: float = FAILED_AUDIO_MAX_AGE_SEC,
) -> None:
    if not failed_dir.is_dir() or failed_dir.is_symlink():
        return
    now = time.time() if now_ts is None else now_ts
    alive: list[tuple[float, str, Path]] = []
    try:
        entries = list(failed_dir.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_symlink():
                continue
            st = entry.stat()
        except OSError:
            continue
        if entry.name.startswith("."):
            if (entry.name.endswith(".tmp") or entry.name.startswith(".rec-")) and (now - st.st_mtime) > 600.0:
                try:
                    entry.unlink(missing_ok=True)
                except OSError:
                    pass
            continue
        if not entry.name.lower().endswith(".wav"):
            continue
        if max_age_sec > 0 and (now - st.st_mtime) > max_age_sec:
            try:
                entry.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        alive.append((st.st_mtime, entry.name, entry))

    if max_files >= 0 and len(alive) > max_files:
        alive.sort(key=lambda item: (item[0], item[1]))
        for _, _, old_path in alive[: len(alive) - max_files]:
            try:
                old_path.unlink(missing_ok=True)
            except OSError:
                pass


def save_failed_audio(
    wav_bytes: bytes,
    runtime_dir: Path,
    *,
    duration_sec: float = 0.0,
    label: str = "",
    now_ts: float | None = None,
    max_files: int = FAILED_AUDIO_MAX_FILES,
    max_age_sec: float = FAILED_AUDIO_MAX_AGE_SEC,
) -> Path | None:
    """Atomically persist a failed normalized WAV under ``<runtime_dir>/failed/`` (0600, ring-pruned)."""
    if not wav_bytes:
        return None
    try:
        fdir = get_failed_audio_dir(runtime_dir, create=True)
        ts = time.time() if now_ts is None else now_ts
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(ts))
        dur_tag = f"{max(1, int(round(duration_sec)))}s" if duration_sec > 0 else "wav"
        clean_lbl = _sanitize_failed_label(label)
        stem = f"{stamp}_{dur_tag}_{clean_lbl}" if clean_lbl else f"{stamp}_{dur_tag}"
        tmp = fdir / f".{stem}.{os.getpid()}.{threading.get_ident()}.{time.monotonic_ns()}.tmp"
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(wav_bytes)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            if now_ts is not None:
                os.utime(tmp, (ts, ts))
            target: Path | None = None
            for suffix_idx in range(1, 100):
                cand = fdir / (f"{stem}.wav" if suffix_idx == 1 else f"{stem}_{suffix_idx}.wav")
                try:
                    os.link(tmp, cand)
                    target = cand
                    break
                except FileExistsError:
                    continue
                except OSError:
                    if not cand.exists():
                        os.replace(tmp, cand)
                        target = cand
                        break
            if target is None:
                return None
        finally:
            tmp.unlink(missing_ok=True)
        prune_failed_audio_dir(fdir, now_ts=ts, max_files=max_files, max_age_sec=max_age_sec)
        return target
    except Exception:
        return None


def list_failed_audios(runtime_dir: Path) -> list[Path]:
    """Return saved failed WAV paths ordered newest-first (read-only; does not mutate disk)."""
    fdir = get_failed_audio_dir(runtime_dir, create=False)
    if not fdir.is_dir() or fdir.is_symlink():
        return []
    items: list[tuple[float, str, Path]] = []
    try:
        for entry in fdir.iterdir():
            if entry.name.startswith(".") or not entry.name.lower().endswith(".wav") or entry.is_symlink():
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            if st.st_size > 0:
                items.append((st.st_mtime, entry.name, entry))
    except OSError:
        return []
    items.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [p for _, _, p in items]


def resolve_failed_audio(runtime_dir: Path, selector: str | None = None) -> Path:
    """Resolve a failed audio file by path, 1-based index (1=newest), filename, or unique substring."""
    sel = (selector or "").strip()
    if sel and (os.sep in sel or (os.altsep and os.altsep in sel) or sel.startswith("~") or sel.lower().endswith(".wav")):
        cand = Path(sel).expanduser()
        if cand.is_file():
            return cand
    items = list_failed_audios(runtime_dir)
    if not items:
        raise FileNotFoundError(f"no saved failed audio found in {get_failed_audio_dir(runtime_dir)}")
    if not sel:
        return items[0]
    if sel.isascii() and sel.isdigit():
        idx = int(sel)
        if idx < 1 or idx > len(items):
            raise FileNotFoundError(f"failed audio index {idx} out of range (available: 1..{len(items)})")
        return items[idx - 1]
    exact = [p for p in items if p.name == sel or p.stem == sel]
    if len(exact) == 1:
        return exact[0]
    substr = [p for p in items if sel.lower() in p.name.lower()]
    if len(substr) == 1:
        return substr[0]
    if len(substr) > 1:
        names = ", ".join(p.name for p in substr[:5])
        raise FileNotFoundError(f"ambiguous failed audio selector {sel!r} matched {len(substr)} files: {names}")
    raise FileNotFoundError(f"no failed audio matching {sel!r} (run 'agent-voice-input retry --list')")


SAVED_RECORDING_EXTS: tuple[str, ...] = (".mp3", ".m4a", ".wav")


def _is_saved_recording_entry(entry: Path) -> bool:
    name = entry.name
    return (
        name.startswith("rec-")
        and not name.startswith(".")
        and name.lower().endswith(SAVED_RECORDING_EXTS)
    )


def prune_saved_recordings_dir(
    recordings_dir: Path,
    *,
    max_count: int = 100,
    now_ts: float | None = None,
) -> None:
    """Retain at most ``max_count`` newest ``rec-*`` files ordered by mtime, pruning older entries."""
    if not recordings_dir.is_dir() or recordings_dir.is_symlink():
        return
    now = time.time() if now_ts is None else now_ts
    eff_max = max(1, int(max_count))
    alive: list[tuple[int, str, Path]] = []
    try:
        entries = list(recordings_dir.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_symlink():
                continue
            st = entry.stat()
        except OSError:
            continue
        if entry.name.startswith("."):
            if (entry.name.endswith(".tmp") or entry.name.startswith(".rec-")) and (now - st.st_mtime) > 600.0:
                try:
                    entry.unlink(missing_ok=True)
                except OSError:
                    pass
            continue
        if _is_saved_recording_entry(entry):
            alive.append((st.st_mtime_ns, entry.name, entry))

    if len(alive) > eff_max:
        alive.sort(key=lambda item: (item[0], item[1]))
        for _, _, old_path in alive[: len(alive) - eff_max]:
            try:
                old_path.unlink(missing_ok=True)
            except OSError:
                pass


def list_saved_recordings(recordings_dir: Path) -> list[Path]:
    """Return archived ``rec-*`` files ordered newest-first (read-only; never mutates disk)."""
    if not recordings_dir.is_dir() or recordings_dir.is_symlink():
        return []
    items: list[tuple[int, str, Path]] = []
    try:
        for entry in recordings_dir.iterdir():
            if not _is_saved_recording_entry(entry) or entry.is_symlink():
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            if st.st_size > 0:
                items.append((st.st_mtime_ns, entry.name, entry))
    except OSError:
        return []
    items.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [p for _, _, p in items]


def _write_temp_0600(path: Path, data: bytes) -> None:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600)
    closed = False
    try:
        os.fchmod(fd, 0o600)
        fobj = os.fdopen(fd, "wb")
        closed = True
        with fobj as f:
            f.write(data)
            f.flush()
    except Exception:
        if not closed:
            try:
                os.close(fd)
            except OSError:
                pass
        raise


def archive_recording_audio(
    raw_wav_bytes: bytes,
    recordings_dir: Path,
    *,
    max_count: int = 100,
    ffmpeg_bin: str = "/usr/bin/ffmpeg",
    now_ts: float | None = None,
    timeout_sec: float = 15.0,
) -> Path | None:
    """Compress voiced WAV to 16 kHz mono 32 kbps MP3 (fallback AAC .m4a / .wav) with 0600 mode & ring pruning."""
    if not raw_wav_bytes:
        return None
    tmp_in: Path | None = None
    tmp_candidates: list[Path] = []
    try:
        rec_dir = ensure_secure_dir(recordings_dir)
        ts = time.time() if now_ts is None else now_ts
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(ts))
        rand_hex = os.urandom(3).hex()
        uniq = f"{os.getpid()}.{threading.get_ident()}.{time.monotonic_ns()}"
        tmp_in = rec_dir / f".rec-in-{uniq}.wav.tmp"
        _write_temp_0600(tmp_in, raw_wav_bytes)

        chosen_tmp: Path | None = None
        chosen_ext = ".wav"
        exe: str | None = None
        try:
            exe = resolve_ffmpeg_bin(ffmpeg_bin)
        except Exception:
            exe = None

        if exe is not None:
            for ext, fmt_args in (
                (".mp3", ("-f", "mp3", "-c:a", "libmp3lame", "-b:a", "32k")),
                (".m4a", ("-f", "ipod", "-c:a", "aac", "-b:a", "32k")),
            ):
                tmp_out = rec_dir / f".rec-out-{uniq}{ext}"
                tmp_candidates.append(tmp_out)
                # Pre-create 0600 target before ffmpeg writes into it so encoded bytes are never world-readable
                _write_temp_0600(tmp_out, b"")
                proc = subprocess.run(
                    [
                        exe,
                        "-nostdin",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-i",
                        str(tmp_in),
                        "-vn",
                        "-ac",
                        "1",
                        "-ar",
                        "16000",
                        *fmt_args,
                        str(tmp_out),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=timeout_sec,
                    check=False,
                )
                if proc.returncode == 0 and tmp_out.is_file() and tmp_out.stat().st_size > 0:
                    chosen_tmp = tmp_out
                    chosen_ext = ext
                    break

        if chosen_tmp is None:
            tmp_wav = rec_dir / f".rec-out-{uniq}.wav.tmp"
            tmp_candidates.append(tmp_wav)
            _write_temp_0600(tmp_wav, raw_wav_bytes)
            chosen_tmp = tmp_wav
            chosen_ext = ".wav"

        os.chmod(chosen_tmp, 0o600)
        if now_ts is not None:
            os.utime(chosen_tmp, (ts, ts))
        final_path = rec_dir / f"rec-{stamp}-{rand_hex}{chosen_ext}"
        os.replace(chosen_tmp, final_path)
        prune_saved_recordings_dir(rec_dir, max_count=max_count, now_ts=ts)
        return final_path
    except Exception:
        return None
    finally:
        if tmp_in is not None:
            try:
                tmp_in.unlink(missing_ok=True)
            except OSError:
                pass
        for cand in tmp_candidates:
            try:
                cand.unlink(missing_ok=True)
            except OSError:
                pass


def spawn_recording_archive(
    raw_wav_bytes: bytes,
    recordings_dir: Path,
    *,
    max_count: int = 100,
    ffmpeg_bin: str = "/usr/bin/ffmpeg",
    now_ts: float | None = None,
) -> threading.Thread | None:
    """Spawn a non-blocking daemon thread to archive ``raw_wav_bytes``."""
    if not raw_wav_bytes:
        return None
    try:
        th = threading.Thread(
            target=archive_recording_audio,
            args=(raw_wav_bytes, recordings_dir),
            kwargs={"max_count": max_count, "ffmpeg_bin": ffmpeg_bin, "now_ts": now_ts},
            name="avi-rec-archive",
            daemon=True,
        )
        th.start()
        return th
    except Exception:
        return None


def wait_recording_archives(
    threads: Iterable[threading.Thread],
    *,
    timeout_sec: float = 2.0,
) -> None:
    """Best-effort wait up to ``timeout_sec`` total for pending background archive threads."""
    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    for th in list(threads):
        if th is threading.current_thread() or not th.is_alive():
            continue
        rem = deadline - time.monotonic()
        if rem <= 0:
            break
        try:
            th.join(timeout=rem)
        except Exception:
            pass
