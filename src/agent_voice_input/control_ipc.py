"""Local IPC (control.sock), daemon lock/PID management, log rotation, and telemetry I/O."""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import select
import socket
import stat
import sys
import time
from typing import Any, Mapping, Sequence

from . import __version__
from .config import ensure_secure_dir
from .platform_common import ForegroundWindow

TELEMETRY_FILENAME: str = "telemetry.jsonl"
TELEMETRY_MAX_BYTES: int = 256 * 1024
RECENT_RESULTS_MAXLEN: int = 20
DAEMON_LOG_FILENAME: str = "daemon.log"
DAEMON_LOG_BACKUP_FILENAME: str = "daemon.log.1"
DEFAULT_DAEMON_LOG_MAX_BYTES: int = 2 * 1024 * 1024

CONTROL_RESPONSE_MAX_BYTES: int = 1_048_576
CONTROL_REQUEST_MAX_BYTES: int = 256 * 1024
CONTROL_REQUEST_READ_TIMEOUT_SEC: float = 2.0
CONTROL_MAX_TIMEOUT_SEC: float = 86_400.0
_BRIDGE_ASSET_NAMES: tuple[str, ...] = ("win_shim.ps1",)
_BRIDGE_ASSET_GLOBS: tuple[tuple[str, str], ...] = (
    ("mac_shim", "*.swift"),
    ("win_shim", "*.cs"),
)

_WINDOW_REQ_KEYS: tuple[str, ...] = (
    "hwnd",
    "process",
    "title",
    "bundle_id",
    "surface_id",
    "tty",
    "cwd",
    "pid",
    "tmux_socket",
    "tmux_pane",
    "document",
    "page_url",
)


class DaemonAlreadyRunningError(RuntimeError):
    """Raised when another daemon instance already holds ``daemon.lock``."""

    def __init__(self, pid: int | None = None) -> None:
        self.pid = pid if (pid is not None and pid > 0) else None
        suffix = f" (pid={self.pid})" if self.pid else ""
        super().__init__(f"agent-voice-input daemon is already running{suffix}")


class _MalformedControlResponseError(ValueError):
    """Raised when control.sock returns EOF, malformed JSON, or an incomplete status payload."""


def rotate_daemon_log_if_needed(
    log_path: Path,
    *,
    max_bytes: int = DEFAULT_DAEMON_LOG_MAX_BYTES,
    rebind_stdio: bool = True,
) -> bool:
    """Safely rotate ``daemon.log`` -> ``daemon.log.1`` (0600) once size reaches ``max_bytes``."""
    if max_bytes <= 0:
        return False
    try:
        st = os.lstat(log_path)
        if not stat.S_ISREG(st.st_mode):
            return False
        backup_path = log_path.with_name(f"{log_path.name}.1")
        bst: os.stat_result | None = None
        try:
            bst = os.lstat(backup_path)
            if not stat.S_ISREG(bst.st_mode):
                return False
        except FileNotFoundError:
            bst = None

        # Idempotent self-heal: if a prior rotation partially failed dup2 and left fd 1/2
        # still pointing at daemon.log.1 while daemon.log exists, rebind those fds back now.
        if rebind_stdio and bst is not None:
            backup_fid = (bst.st_dev, bst.st_ino)
            stale_fds: list[int] = []
            for fd_num in (1, 2):
                try:
                    fst = os.fstat(fd_num)
                    if (fst.st_dev, fst.st_ino) == backup_fid:
                        stale_fds.append(fd_num)
                except OSError:
                    pass
            if stale_fds:
                for fd_num, stream in ((1, sys.stdout), (2, sys.stderr)):
                    if fd_num in stale_fds:
                        try:
                            stream.flush()
                        except Exception:
                            pass
                heal_flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
                heal_fd: int | None = None
                try:
                    heal_fd = os.open(str(log_path), heal_flags, 0o600)
                    for fd_num in stale_fds:
                        try:
                            os.dup2(heal_fd, fd_num)
                        except OSError:
                            pass
                except OSError:
                    pass
                finally:
                    if heal_fd is not None:
                        try:
                            os.close(heal_fd)
                        except OSError:
                            pass

        if st.st_size < max_bytes:
            return False

        rebind_fds: list[int] = []
        if rebind_stdio:
            target_fid = (st.st_dev, st.st_ino)
            for fd_num in (1, 2):
                try:
                    fst = os.fstat(fd_num)
                    if (fst.st_dev, fst.st_ino) == target_fid:
                        rebind_fds.append(fd_num)
                except OSError:
                    pass
            if 1 in rebind_fds:
                try:
                    sys.stdout.flush()
                except Exception:
                    pass
            if 2 in rebind_fds:
                try:
                    sys.stderr.flush()
                except Exception:
                    pass

        tmp_path = log_path.with_name(
            f".{log_path.name}.rotate.{os.getpid()}.{time.monotonic_ns()}.tmp"
        )
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_APPEND
            | getattr(os, "O_NOFOLLOW", 0)
        )
        new_fd: int | None = None
        tmp_exists = False
        try:
            new_fd = os.open(str(tmp_path), flags, 0o600)
            tmp_exists = True
            os.fchmod(new_fd, 0o600)

            os.replace(log_path, backup_path)
            try:
                os.replace(tmp_path, log_path)
                tmp_exists = False
            except BaseException:
                # Best-effort rollback so log_path never stays permanently missing on .1
                try:
                    os.replace(backup_path, log_path)
                except OSError:
                    pass
                raise

            try:
                os.chmod(backup_path, 0o600)
            except OSError:
                pass
            dup_failed = False
            for fd_num in rebind_fds:
                try:
                    os.dup2(new_fd, fd_num)
                except OSError:
                    dup_failed = True
            return not dup_failed
        finally:
            if new_fd is not None:
                try:
                    os.close(new_fd)
                except OSError:
                    pass
            if tmp_exists:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
    except Exception:
        return False


def append_telemetry_record(
    runtime_dir: Path,
    record: Mapping[str, Any],
    *,
    max_bytes: int = TELEMETRY_MAX_BYTES,
) -> None:
    """Append one pure-numeric JSON record to ``0600`` ``telemetry.jsonl`` with atomic >256KB tail truncation."""
    try:
        rt = ensure_secure_dir(runtime_dir)
        telemetry_path = rt / TELEMETRY_FILENAME
        lock_path = rt / ".telemetry.lock"
        line_bytes = (
            json.dumps(dict(record), ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        lfd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR | nofollow, 0o600)
        os.fchmod(lfd, 0o600)
        with os.fdopen(lfd, "r+", encoding="utf-8") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    st_pre = os.lstat(telemetry_path)
                    if not stat.S_ISREG(st_pre.st_mode):
                        return
                except FileNotFoundError:
                    pass
                afd = os.open(
                    str(telemetry_path),
                    os.O_CREAT | os.O_WRONLY | os.O_APPEND | nofollow,
                    0o600,
                )
                os.fchmod(afd, 0o600)
                with os.fdopen(afd, "wb") as af:
                    af.write(line_bytes)
                    af.flush()
                st = os.lstat(telemetry_path)
                if not stat.S_ISREG(st.st_mode) or st.st_size <= max_bytes:
                    return
                keep_target = max(1, max_bytes // 2)
                seek_pos = max(0, st.st_size - keep_target)
                with open(telemetry_path, "rb") as rf:
                    if seek_pos > 0:
                        rf.seek(seek_pos)
                    raw_tail = rf.read()
                if seek_pos > 0:
                    nl = raw_tail.find(b"\n")
                    if 0 <= nl < len(raw_tail) - 1:
                        tail = raw_tail[nl + 1 :]
                    else:
                        tail = line_bytes if len(line_bytes) <= max_bytes else b""
                else:
                    tail = raw_tail
                tmp_path = rt / f".{TELEMETRY_FILENAME}.trim.{os.getpid()}.{time.monotonic_ns()}.tmp"
                tfd = os.open(
                    str(tmp_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY | nofollow, 0o600
                )
                closed_tfd = False
                try:
                    os.fchmod(tfd, 0o600)
                    tf_obj = os.fdopen(tfd, "wb")
                    closed_tfd = True
                    with tf_obj as tf:
                        tf.write(tail)
                        tf.flush()
                    os.replace(tmp_path, telemetry_path)
                except Exception:
                    if not closed_tfd:
                        try:
                            os.close(tfd)
                        except OSError:
                            pass
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except OSError:
                        pass
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass


def read_telemetry_history(
    runtime_dir: Path,
    limit: int = 10,
    *,
    max_scan_bytes: int = TELEMETRY_MAX_BYTES + 65536,
) -> list[dict[str, Any]]:
    """Read up to ``limit`` most recent JSONL telemetry records from ``runtime_dir / telemetry.jsonl``."""
    eff_limit = max(0, int(limit))
    if eff_limit <= 0:
        return []
    telemetry_path = runtime_dir / TELEMETRY_FILENAME
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(telemetry_path), os.O_RDONLY | nofollow)
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_size <= 0:
                return []
            seek_pos = max(0, st.st_size - max(4096, int(max_scan_bytes)))
            if seek_pos > 0:
                f.seek(seek_pos)
            raw = f.read()
        if seek_pos > 0:
            nl = raw.find(b"\n")
            if 0 <= nl < len(raw) - 1:
                raw = raw[nl + 1 :]
        out: list[dict[str, Any]] = []
        for raw_ln in raw.splitlines():
            ln = raw_ln.strip()
            if not ln:
                continue
            try:
                obj = json.loads(ln.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
        return out[-eff_limit:]
    except Exception:
        return []


def merge_recent_results(
    mem_items: Sequence[Mapping[str, Any]],
    disk_items: Sequence[Mapping[str, Any]],
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Merge persisted telemetry records with live in-memory ``recent_results`` without duplicates."""
    eff_limit = max(0, int(limit))
    if eff_limit <= 0:
        return []
    merged: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any]] = set()
    for rec in disk_items:
        d = dict(rec)
        key = (d.get("ts"), d.get("seq_id"))
        if d.get("ts") is not None:
            seen.add(key)
        merged.append(d)
    for rec in mem_items:
        d = dict(rec)
        key = (d.get("ts"), d.get("seq_id"))
        if d.get("ts") is not None and key in seen:
            continue
        if d.get("ts") is not None:
            seen.add(key)
        merged.append(d)
    return merged[-eff_limit:]


def _has_window_fields(req: Mapping[str, Any]) -> bool:
    for k in _WINDOW_REQ_KEYS:
        val = req.get(k)
        if isinstance(val, str):
            if val.strip():
                return True
        elif val:
            return True
    return False


def _window_from_mapping(m: Mapping[str, Any]) -> ForegroundWindow:
    raw_ax = m.get("ax_trusted")
    return ForegroundWindow(
        hwnd=int(m.get("hwnd") or 0),
        process=str(m.get("process") or ""),
        title=str(m.get("title") or ""),
        bundle_id=str(m.get("bundle_id") or ""),
        surface_id=str(m.get("surface_id") or ""),
        tty=str(m.get("tty") or ""),
        cwd=str(m.get("cwd") or ""),
        pid=int(m.get("pid") or 0),
        ax_trusted=raw_ax if isinstance(raw_ax, bool) else None,
        tmux_socket=str(m.get("tmux_socket") or ""),
        tmux_pane=str(m.get("tmux_pane") or ""),
        document=str(m.get("document") or ""),
        page_url=str(m.get("page_url") or ""),
    )


def _resolve_control_origin_window(req: Mapping[str, Any]) -> ForegroundWindow | None:
    if _has_window_fields(req):
        return _window_from_mapping(req)
    if req.get("use_live_window") is True:
        return None
    return ForegroundWindow()


def _parse_optional_seq_id(raw: Any) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise ValueError(f"seq_id must be a positive integer, got {raw!r}")
    return raw


def _parse_optional_timeout_sec(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError("timeout_sec must be a finite non-negative number")
    try:
        val = float(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("timeout_sec out of range") from exc
    if not math.isfinite(val) or val < 0.0 or val > CONTROL_MAX_TIMEOUT_SEC:
        raise ValueError(f"timeout_sec must be between 0 and {CONTROL_MAX_TIMEOUT_SEC:g}")
    return val


def _is_control_peer_disconnected(conn: socket.socket | None) -> bool:
    if conn is None:
        return False
    kqueue_factory = getattr(select, "kqueue", None)
    kevent_factory = getattr(select, "kevent", None)
    use_kqueue = callable(kqueue_factory) and callable(kevent_factory)
    poll_factory = None if use_kqueue else getattr(select, "poll", None)
    if not use_kqueue and not callable(poll_factory):
        return False
    try:
        fd = conn.fileno()
        if fd < 0:
            return True
        if use_kqueue:
            # BSD/macOS poll() sets POLLHUP on peer SHUT_WR; KQ_FILTER_WRITE
            # only raises KQ_EV_EOF once the peer closes its read side too.
            kq = kqueue_factory()
            try:
                ev = kevent_factory(
                    fd,
                    filter=select.KQ_FILTER_WRITE,
                    flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                )
                eof_mask = select.KQ_EV_EOF | select.KQ_EV_ERROR
                return any(bool(r.flags & eof_mask) for r in kq.control([ev], 1, 0))
            finally:
                kq.close()
        hangup_mask = (
            getattr(select, "POLLHUP", 0)
            | getattr(select, "POLLERR", 0)
            | getattr(select, "POLLNVAL", 0)
        )
        poller = poll_factory()
        poller.register(fd, hangup_mask)
        return any(bool(revents & hangup_mask) for _, revents in poller.poll(0))
    except (InterruptedError, BlockingIOError, TimeoutError, socket.timeout):
        return False
    except (OSError, ValueError):
        return True


def _recv_control_request_line(
    conn: socket.socket,
    *,
    max_bytes: int = CONTROL_REQUEST_MAX_BYTES,
    timeout_sec: float = CONTROL_REQUEST_READ_TIMEOUT_SEC,
) -> str:
    buf = bytearray()
    deadline = time.monotonic() + max(0.05, float(timeout_sec))
    while True:
        nl = buf.find(b"\n")
        if nl >= 0:
            if nl + 1 > max_bytes:
                raise ValueError(f"control request exceeds {max_bytes} bytes")
            return bytes(buf[:nl]).decode("utf-8", errors="replace").strip()
        if len(buf) > max_bytes:
            raise ValueError(f"control request exceeds {max_bytes} bytes")
        rem = deadline - time.monotonic()
        if rem <= 0.0:
            raise socket.timeout("timed out reading control request")
        conn.settimeout(rem)
        chunk = conn.recv(min(16384, max_bytes - len(buf) + 1))
        if not chunk:
            break
        buf.extend(chunk)
    if len(buf) > max_bytes:
        raise ValueError(f"control request exceeds {max_bytes} bytes")
    return bytes(buf).decode("utf-8", errors="replace").strip()


def send_control_reply(conn: socket.socket, resp: Mapping[str, Any]) -> None:
    try:
        conn.settimeout(2.0)
        conn.sendall((json.dumps(dict(resp), ensure_ascii=False) + "\n").encode("utf-8"))
    except OSError:
        pass


_SAFE_HOTWORDS_STATUS_KEYS: tuple[str, ...] = (
    "total",
    "kept",
    "dropped_count",
    "used_chars",
    "max_items",
    "max_chars",
    "warnings_count",
    "stage2_kept",
    "stage2_dropped",
)


def _sanitize_hotwords_status_counts(raw: Mapping[str, Any] | None) -> dict[str, int] | None:
    """Whitelist integer-only hotwords counters for daemon ``status`` (never leaks term text)."""
    if not isinstance(raw, Mapping):
        return None
    out: dict[str, int] = {}
    for key in _SAFE_HOTWORDS_STATUS_KEYS:
        val = raw.get(key)
        if isinstance(val, int) and not isinstance(val, bool):
            out[key] = val
    # Normalize fallback aliases (e.g., callers passing summary.as_dict()) onto canonical contract keys
    for dst_key, src_keys in (
        ("total", ("total_input",)),
        ("stage2_kept", ("kept", "kept_count")),
        ("stage2_dropped", ("dropped_count",)),
    ):
        if dst_key not in out:
            for src_key in src_keys:
                val = raw.get(src_key)
                if isinstance(val, int) and not isinstance(val, bool):
                    out[dst_key] = val
                    break
    return out if out else None


def build_daemon_status_response(
    *,
    runtime_dir: Path,
    req: Mapping[str, Any],
    build_id: str,
    state: str,
    seq_id: int,
    pending_count: int,
    active_pipeline: Mapping[str, Any],
    resolved_refiner_provider: str,
    shim_ok: bool,
    shim_error: str,
    last_result: Mapping[str, Any],
    remote_hosts: Mapping[str, Any] | None,
    mem_recent_results: Sequence[Mapping[str, Any]],
    hotwords: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the JSON-serializable response payload for a ``status`` control command."""
    import copy

    raw_hist = req.get("history")
    want_hist = raw_hist is not None and not isinstance(raw_hist, bool)
    hist_limit = max(0, min(500, int(raw_hist))) if want_hist else 0
    resp: dict[str, Any] = {
        "ok": True,
        "version": __version__,
        "build_id": build_id,
        "pid": os.getpid(),
        "state": state,
        "seq_id": seq_id,
        "pending_count": pending_count,
        "asr_provider": active_pipeline["asr"]["provider"],
        "refiner_provider": resolved_refiner_provider,
        "pipeline": copy.deepcopy(dict(active_pipeline)),
        "shim_ok": shim_ok,
        "last_result": dict(last_result),
    }
    if shim_error:
        resp["shim_error"] = shim_error
    if remote_hosts:
        resp["remote_hosts"] = dict(remote_hosts)
    safe_hw = _sanitize_hotwords_status_counts(hotwords)
    if safe_hw is not None:
        resp["hotwords"] = safe_hw
    if want_hist:
        disk_items = read_telemetry_history(
            runtime_dir, limit=max(hist_limit, RECENT_RESULTS_MAXLEN)
        )
        merged = merge_recent_results(mem_recent_results, disk_items, limit=hist_limit)
        resp["recent_results"] = merged
        if not resp["last_result"] and merged:
            resp["last_result"] = dict(merged[-1])
    return resp


def compute_package_build_id(pkg_dir: Path | None = None) -> str:
    """Compute a lightweight deterministic 12-char SHA-256 fingerprint of package ``*.py`` and bridge assets."""
    root = pkg_dir if pkg_dir is not None else Path(__file__).resolve().parent
    h = hashlib.sha256()
    try:
        candidates: list[tuple[str, Path]] = [(p.name, p) for p in root.glob("*.py") if p.is_file()]
        for asset in _BRIDGE_ASSET_NAMES:
            ap = root / asset
            if ap.is_file():
                candidates.append((asset, ap))
        for subdir, pattern in _BRIDGE_ASSET_GLOBS:
            ad = root / subdir
            if ad.is_dir():
                for ap in ad.glob(pattern):
                    if ap.is_file():
                        candidates.append((ap.relative_to(root).as_posix(), ap))
        candidates.sort(key=lambda item: item[0])
        for rel_name, path in candidates:
            if not path.is_file():
                continue
            h.update(rel_name.encode("utf-8"))
            h.update(b"\0")
            h.update(path.read_bytes())
            h.update(b"\0")
    except OSError:
        return ""
    return h.hexdigest()[:12]


def _parse_version_tuple(ver: str) -> tuple[int, ...] | None:
    core = ver.strip().split("-", 1)[0].split("+", 1)[0]
    parts = core.split(".")
    if not parts or any(not p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def describe_daemon_drift(
    daemon_status: Mapping[str, Any],
    *,
    current_version: str = __version__,
    current_build_id: str | None = None,
) -> dict[str, Any]:
    """Compare a running daemon's reported version/build_id against the current CLI process."""
    run_ver = str(daemon_status.get("version") or "").strip()
    run_bid = str(daemon_status.get("build_id") or "").strip()
    cur_bid = current_build_id if current_build_id is not None else compute_package_build_id()
    ver_diff = bool(run_ver and run_ver != current_version)
    bid_diff = bool(run_bid and cur_bid and run_bid != cur_bid)
    drifted = ver_diff or bid_diff
    if ver_diff:
        rv = _parse_version_tuple(run_ver)
        cv = _parse_version_tuple(current_version)
        verb = "upgraded" if (rv is not None and cv is not None and cv > rv) else "changed"
        reason = f"version {verb} ({run_ver} -> {current_version})"
    elif bid_diff:
        reason = f"code updated on disk ({run_bid} -> {cur_bid})"
    else:
        reason = ""
    return {
        "drifted": drifted,
        "running_version": run_ver,
        "running_build_id": run_bid,
        "current_version": current_version,
        "current_build_id": cur_bid,
        "reason": reason,
    }


def _read_positive_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
        val = int(raw)
        return val if val > 0 else None
    except (OSError, ValueError):
        return None


def _inspect_daemon_lock_holder(runtime_dir: Path) -> tuple[bool, int | None]:
    """Return ``(lock_held, holder_pid)`` without creating or modifying ``daemon.lock``."""
    lock_path = runtime_dir / "daemon.lock"
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(str(lock_path), flags)
    except OSError:
        return False, None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            return False, None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            if isinstance(exc, OSError) and exc.errno not in (
                errno.EACCES,
                errno.EAGAIN,
                errno.EWOULDBLOCK,
            ):
                return False, None
            os.lseek(fd, 0, os.SEEK_SET)
            raw_pid = os.read(fd, 64).decode("utf-8", errors="ignore").strip()
            holder_pid = (
                int(raw_pid)
                if (raw_pid.isdigit() and int(raw_pid) > 0)
                else _read_positive_pid(runtime_dir / "daemon.pid")
            )
            return True, holder_pid
        else:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            return False, None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def acquire_daemon_lock(runtime_dir: Path, lock_path: Path, pid_path: Path) -> int:
    """Acquire exclusive non-blocking ``daemon.lock`` (0600, O_NOFOLLOW, O_CLOEXEC) and write PID."""
    ensure_secure_dir(runtime_dir)
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    fd = os.open(str(lock_path), flags, 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            raise RuntimeError(f"unsafe daemon lock file: {lock_path}")
        os.fchmod(fd, 0o600)
        os.set_inheritable(fd, False)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            if isinstance(exc, OSError) and exc.errno not in (
                errno.EACCES,
                errno.EAGAIN,
                errno.EWOULDBLOCK,
            ):
                raise
            os.lseek(fd, 0, os.SEEK_SET)
            raw_pid = os.read(fd, 64).decode("utf-8", errors="ignore").strip()
            holder_pid = (
                int(raw_pid)
                if (raw_pid.isdigit() and int(raw_pid) > 0)
                else _read_positive_pid(pid_path)
            )
            raise DaemonAlreadyRunningError(holder_pid) from None
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
    except BaseException:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
        raise
    return fd


def release_daemon_lock(lock_fd: int | None) -> None:
    """Truncate, unlock, and close ``daemon.lock`` without unlinking it (avoids inode races)."""
    if lock_fd is None:
        return
    try:
        os.ftruncate(lock_fd, 0)
    except OSError:
        pass
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(lock_fd)
    except OSError:
        pass


def write_daemon_pid_file(pid_path: Path) -> None:
    """Write the current process PID into ``daemon.pid`` with 0600 permissions and O_NOFOLLOW."""
    pid_fd = os.open(
        str(pid_path),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(pid_fd, 0o600)
        os.write(pid_fd, f"{os.getpid()}\n".encode("ascii"))
    finally:
        os.close(pid_fd)


def send_control_command(
    runtime_dir: Path, payload: Mapping[str, Any], timeout_sec: float = 8.0
) -> dict[str, Any]:
    sock_path = runtime_dir / "control.sock"
    if not sock_path.exists():
        raise RuntimeError(f"daemon socket not found at {sock_path}")
    t0 = time.monotonic()
    req_payload = dict(payload)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout_sec)
        s.connect(str(sock_path))
        if (
            str(req_payload.get("cmd") or "") == "selftest_wav"
            and "timeout_sec" not in req_payload
            and isinstance(timeout_sec, (int, float))
            and not isinstance(timeout_sec, bool)
            and math.isfinite(timeout_sec)
            and timeout_sec > 0
        ):
            rem_budget = max(0.01, float(timeout_sec) - (time.monotonic() - t0))
            req_payload["timeout_sec"] = round(rem_budget, 4)
        s.sendall((json.dumps(req_payload, ensure_ascii=False) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf and len(buf) < CONTROL_RESPONSE_MAX_BYTES and (chunk := s.recv(16384)):
            buf += chunk
    if not buf.strip():
        raise _MalformedControlResponseError("empty control response (EOF)")
    return json.loads(buf.decode("utf-8", errors="replace").strip())


def probe_daemon_status(
    runtime_dir: Path,
    *,
    timeout_sec: float = 8.0,
    history: int | None = None,
) -> dict[str, Any]:
    """Query daemon status and distinguish genuinely ``stopped`` from live-but-``unresponsive``."""
    req: dict[str, Any] = {"cmd": "status"}
    if history is not None:
        req["history"] = history
    try:
        resp = send_control_command(runtime_dir, req, timeout_sec=timeout_sec)
        if (
            isinstance(resp, Mapping)
            and resp.get("ok") is True
            and isinstance(resp.get("state"), str)
            and resp["state"].strip()
        ):
            return dict(resp)
        err_detail = str(resp.get("error") or "") if isinstance(resp, Mapping) else ""
        raise _MalformedControlResponseError(
            err_detail or "malformed or error status response from daemon"
        )
    except Exception as exc:
        lock_held, holder_pid = _inspect_daemon_lock_holder(runtime_dir)
        sock_exists = (runtime_dir / "control.sock").exists()
        is_timeout = isinstance(exc, (TimeoutError, socket.timeout))
        is_backlog_busy = isinstance(exc, OSError) and exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK)
        is_malformed = (
            isinstance(exc, (_MalformedControlResponseError, ValueError)) and sock_exists
        )
        if lock_held or is_timeout or is_backlog_busy or is_malformed:
            if holder_pid is None:
                cand_pid = _read_positive_pid(runtime_dir / "daemon.pid")
                if cand_pid is not None:
                    try:
                        os.kill(cand_pid, 0)
                        holder_pid = cand_pid
                    except PermissionError:
                        holder_pid = cand_pid
                    except OSError:
                        holder_pid = None
            if is_timeout:
                err_msg = f"control socket timed out after {timeout_sec:.1f}s (daemon busy or hung)"
            elif lock_held:
                err_msg = f"daemon.lock is held but control socket is unreachable ({exc})"
            else:
                err_msg = f"control socket error ({exc})"
            out: dict[str, Any] = {
                "ok": False,
                "state": "unresponsive",
                "lock_held": lock_held,
                "error": err_msg,
            }
            if holder_pid is not None:
                out["pid"] = holder_pid
            return out
        is_offline_err = (
            isinstance(exc, (RuntimeError, FileNotFoundError, ConnectionRefusedError))
            or (
                isinstance(exc, OSError)
                and exc.errno in (errno.ENOENT, errno.ECONNREFUSED, errno.ENOTSOCK, errno.EPROTOTYPE)
            )
            or (isinstance(exc, (_MalformedControlResponseError, ValueError)) and not sock_exists)
        )
        if not is_offline_err:
            return {
                "ok": False,
                "state": "error",
                "lock_held": False,
                "error": f"control socket error ({exc})",
            }
        return {"ok": False, "state": "stopped"}
