"""Active CLI session resolution and incremental JSONL clean context extraction.

Supports ``codex``, ``claude`` (Claude Code), and ``agy`` (Antigravity CLI) across:
1. O(1) window title session tag / UUID lookup (runtime registry + direct path / prefix);
2. Linux ``/proc`` foreground root-session discovery (strictly excluding background subagents
   and stale ``agy`` presence locks) with 4-level disambiguation fallback, plus macOS fallback;
3. Streaming byte-offset incremental JSONL reading (safe against concurrent half-line writes,
   ``\\r``/``\\x0c`` control bytes, atomic replacement, and equal/longer in-place rewrites)
   with strict per-CLI whitelist filtering and ``git status`` hotword extraction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import errno
import fcntl
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import threading
import time
from typing import Any, BinaryIO, Mapping, Sequence
import uuid

from .asr_providers import AsrContext
from .config import ensure_secure_dir, get_runtime_dir

MAX_META_LINE_BYTES = 16 * 1024 * 1024  # 16 MiB safety cap per metadata/ingest line
MAX_INGEST_LINE_BYTES = 16 * 1024 * 1024
_REG_SID_RE = re.compile(r"^[0-9a-f]{8}[0-9a-f-]{0,56}$")

# ---------------------------------------------------------------------------
# Window title hint parsing
# ---------------------------------------------------------------------------

_PREFIXED_TAG_RE = re.compile(
    r"\[(agy|claude|codex):#?([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}|[0-9a-fA-F]{8,36})\]",
    re.IGNORECASE,
)
_HASH_TAG_RE = re.compile(
    r"\[#([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}|[0-9a-fA-F]{8,36})\](?=$|[^0-9A-Za-z_])"
)
_FULL_UUID_RE = re.compile(
    r"(?:^|[^0-9a-fA-F-])([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?=$|[^0-9a-fA-F-])"
)
_STRICT_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
# Strict word boundaries excluding '/' and '-' so paths/branches like feat/codex-config do not match
_WORD_CLI_RE = re.compile(
    r"(?:^|[\s\[\(\{·|:])(agy|antigravity|claude|codex)(?=$|[\s\]\)\}·|:])",
    re.IGNORECASE,
)
_CLAUDE_VER_COMM_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?$")


@dataclass(frozen=True)
class TitleHint:
    session_token: str
    is_full_uuid: bool
    cli: str | None = None
    explicit_cli: bool = False


@dataclass(frozen=True)
class SessionMatch:
    cli: str  # "codex" | "claude" | "agy"
    session_id: str
    jsonl_path: str
    cwd: str = ""
    matched_by: str = ""  # "registry" | "title_uuid" | "title_prefix" | "proc_scan" | "recent_root"
    pts_mtime: float = 0.0
    jsonl_mtime: float = 0.0
    thread_name: str = ""

    @property
    def session_key(self) -> str:
        return f"{self.cli}:{self.session_id}"


@dataclass
class SessionState:
    session_key: str
    cli: str
    session_id: str
    jsonl_path: str
    cwd: str = ""
    byte_offset: int = 0
    file_id: tuple[int, int] | None = None
    last_mtime_ns: int = 0
    prefix_hasher: Any = field(
        default_factory=lambda: hashlib.blake2b(digest_size=16), repr=False
    )
    clean_turns: list[tuple[str, str]] = field(default_factory=list)
    luna_synced_idx: int = 0
    generation: int = 0
    codex_saw_item_completed: bool = False
    codex_item_turns: list[tuple[str, str]] = field(default_factory=list)
    codex_resp_turns: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class SessionSnapshot:
    match: SessionMatch
    clean_turns: tuple[tuple[str, str], ...]
    luna_synced_idx: int
    generation: int
    git_status: str
    hotwords: tuple[str, ...]
    byte_offset: int

    @property
    def session_key(self) -> str:
        return self.match.session_key

    def format_dialog(self, max_chars: int = 16000) -> str:
        return format_turns_text(self.clean_turns, max_chars=max_chars)


def _infer_cli_word_from_title(window_title: str) -> str | None:
    m = _WORD_CLI_RE.search(window_title)
    if not m:
        return None
    w = m.group(1).lower()
    if w == "antigravity":
        return "agy"
    return w


def parse_window_title_hint(window_title: str) -> TitleHint | None:
    """Extract O(1) session tag or Codex UUID from foreground terminal window title."""
    if not window_title or not window_title.strip():
        return None

    prefixed_matches = list(_PREFIXED_TAG_RE.finditer(window_title))
    if prefixed_matches:
        m = prefixed_matches[-1]
        cli = m.group(1).lower()
        tok = m.group(2).lower()
        return TitleHint(
            session_token=tok,
            is_full_uuid=bool(_STRICT_UUID_RE.match(tok)),
            cli=cli,
            explicit_cli=True,
        )

    hash_matches = list(_HASH_TAG_RE.finditer(window_title))
    if hash_matches:
        tok = hash_matches[-1].group(1).lower()
        return TitleHint(
            session_token=tok,
            is_full_uuid=bool(_STRICT_UUID_RE.match(tok)),
            cli=_infer_cli_word_from_title(window_title),
            explicit_cli=False,
        )

    uuid_matches = list(_FULL_UUID_RE.finditer(window_title))
    if uuid_matches:
        tok = uuid_matches[-1].group(1).lower()
        return TitleHint(
            session_token=tok,
            is_full_uuid=True,
            cli=_infer_cli_word_from_title(window_title) or "codex",
            explicit_cli=False,
        )

    return None


# ---------------------------------------------------------------------------
# Root-session predicates & filesystem helpers
# ---------------------------------------------------------------------------


def encode_claude_project_dir(cwd: str) -> list[str]:
    """Return deduplicated Claude project directory names for logical and real ``cwd``."""
    if not cwd:
        return []
    out: list[str] = []
    for candidate in (cwd, os.path.realpath(cwd)):
        enc = re.sub(r"[^A-Za-z0-9]", "-", candidate)
        if enc and enc not in out:
            out.append(enc)
    return out


def _safe_mtime(path: str | Path) -> float:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


def _read_bounded_line(f: BinaryIO, max_bytes: int = MAX_META_LINE_BYTES) -> tuple[bytes, bool]:
    """Read one ``\\n``-terminated line up to ``max_bytes``; drain remainder if exceeded."""
    line = f.readline(max_bytes + 1)
    if not line:
        return b"", False
    if len(line) <= max_bytes or line.endswith(b"\n"):
        return line, False
    # Drain rest of oversized single line until '\n' or EOF
    while True:
        chunk = f.readline(65536)
        if not chunk or chunk.endswith(b"\n"):
            break
    return line[:max_bytes], True


def _read_codex_root_meta(jsonl_path: str | Path) -> tuple[bool, str, str]:
    """Return ``(is_root_cli, session_id, cwd)`` from line 1 of a Codex rollout JSONL."""
    try:
        with open(jsonl_path, "rb") as f:
            first_line, exceeded = _read_bounded_line(f)
    except OSError:
        return False, "", ""
    if not first_line or exceeded:
        return False, "", ""
    try:
        obj = json.loads(first_line.decode("utf-8", errors="replace"))
    except ValueError:
        return False, "", ""
    if not isinstance(obj, dict) or obj.get("type") != "session_meta":
        return False, "", ""
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return False, "", ""
    is_cli = payload.get("source") == "cli"
    sid = str(payload.get("id") or payload.get("session_id") or "").strip().lower()
    cwd = str(payload.get("cwd") or "").strip()
    return is_cli, sid, cwd


_SIDECHAIN_TRUE_RE = re.compile(rb'"isSidechain"\s*:\s*true\b')
_SIDECHAIN_FALSE_RE = re.compile(rb'"isSidechain"\s*:\s*false\b')
_CLAUDE_SUBAGENT_HEAD_RE = re.compile(
    rb'"(?:agentId|agent_id)"\s*:\s*"[^"]+"|"entrypoint"\s*:\s*"sdk-cli"'
)


def _read_claude_root_meta(
    jsonl_path: str | Path,
    *,
    allow_empty_or_meta_only: bool = False,
) -> tuple[bool, str, str]:
    """Return ``(is_root_not_sidechain, session_id, cwd)`` for a Claude Code JSONL."""
    p = Path(jsonl_path)
    if p.name.lower().startswith("agent-"):
        return False, "", ""
    fallback_sid = p.stem.lower()
    last_cwd = ""
    last_sid = fallback_sid
    try:
        with open(p, "rb") as f:
            checked_records = 0
            while checked_records < 30:
                raw, exceeded = _read_bounded_line(f)
                if not raw:
                    break
                if not raw.strip():
                    continue
                if exceeded:
                    # Oversized attachment/image line: inspect head bytes without burning quota
                    if _SIDECHAIN_TRUE_RE.search(raw) or _CLAUDE_SUBAGENT_HEAD_RE.search(raw):
                        return False, "", ""
                    if _SIDECHAIN_FALSE_RE.search(raw):
                        return True, last_sid, last_cwd
                    continue
                checked_records += 1
                try:
                    obj = json.loads(raw.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("agentId") or obj.get("agent_id") or obj.get("entrypoint") == "sdk-cli":
                    return False, "", ""
                if isinstance(obj.get("sessionId"), str) and obj["sessionId"].strip():
                    last_sid = obj["sessionId"].strip().lower()
                if isinstance(obj.get("cwd"), str) and obj["cwd"].strip():
                    last_cwd = obj["cwd"].strip()
                if "isSidechain" in obj or obj.get("type") in ("user", "assistant"):
                    if bool(obj.get("isSidechain")):
                        return False, "", ""
                    return True, last_sid, last_cwd
    except OSError:
        return False, "", ""

    if allow_empty_or_meta_only:
        return True, last_sid, last_cwd
    return False, "", ""


def _default_agy_expected_transcript_path(home: Path, cid: str) -> Path:
    return (
        home
        / ".gemini"
        / "antigravity-cli"
        / "brain"
        / cid
        / ".system_generated"
        / "logs"
        / "transcript_full.jsonl"
    )


def _resolve_agy_transcript_path(home: Path, cid: str) -> Path | None:
    logs_dir = home / ".gemini" / "antigravity-cli" / "brain" / cid / ".system_generated" / "logs"
    full_p = logs_dir / "transcript_full.jsonl"
    if full_p.is_file():
        return full_p
    compact_p = logs_dir / "transcript.jsonl"
    if compact_p.is_file():
        return compact_p
    return None


def _upgrade_agy_transcript_path(path_str: str) -> str:
    if not path_str:
        return path_str
    p = Path(path_str)
    if p.name == "transcript.jsonl":
        full_p = p.with_name("transcript_full.jsonl")
        if full_p.is_file():
            return str(full_p)
    return str(p)


def _is_agy_transcript_subagent(jsonl_path: str | Path) -> bool:
    """Inspect ONLY the first JSONL record to distinguish agy subagents from main sessions.

    Main sessions begin with ``USER_EXPLICIT`` (or ``SYSTEM``/``CHECKPOINT`` / ``CONVERSATION_HISTORY``).
    Subagents begin with a ``SYSTEM``/``SYSTEM_MESSAGE`` (or ``EPHEMERAL_MESSAGE``) carrying a
    routed ``[Message] timestamp=... sender=...`` payload or ``<subagent_reminder>``.
    Later records are never scanned so reading code/specs mentioning subagents cannot false-fire.
    """
    try:
        with open(jsonl_path, "rb") as f:
            while True:
                line, exceeded = _read_bounded_line(f)
                if not line:
                    return False
                if line.strip():
                    break
    except OSError:
        return False

    if exceeded:
        return False

    try:
        obj = json.loads(line.decode("utf-8", errors="replace"))
    except ValueError:
        return False
    if not isinstance(obj, dict):
        return False

    src = obj.get("source")
    typ = obj.get("type")
    if src == "USER_EXPLICIT":
        content = str(obj.get("content") or "").lstrip()
        if content.startswith("<USER_REQUEST>"):
            content = content[len("<USER_REQUEST>") :].lstrip()
        # Exclude headless delegated agy-run worker sessions
        if content.startswith("[agy-run]"):
            return True
        return False
    if src == "SYSTEM" and typ in ("CHECKPOINT", "CONVERSATION_HISTORY"):
        return False
    if src == "SYSTEM" and typ in ("SYSTEM_MESSAGE", "EPHEMERAL_MESSAGE"):
        content = str(obj.get("content") or "")
        if ("[Message] timestamp=" in content and "sender=" in content) or (
            "<subagent_reminder>" in content or "invoked by a caller agent" in content
        ):
            return True
    return False


def _read_proc_cmdline_args(proc_pid_dir: Path) -> list[str]:
    try:
        raw_cmd = (proc_pid_dir / "cmdline").read_bytes()
    except OSError:
        return []
    return [a.decode("utf-8", errors="ignore") for a in raw_cmd.split(b"\x00") if a]


def _is_headless_cli_invocation(cli_kind: str, args: Sequence[str]) -> bool:
    """Return True when ``cmdline`` belongs to a headless/delegated CLI worker (e.g. -p, exec, app-server)."""
    if not args:
        return False
    tail = args[1:]
    if cli_kind in ("agy", "claude"):
        for a in tail:
            if a == "--":
                break
            if a in ("-p", "--print", "--input-format", "--output-format"):
                return True
            if a.startswith(("--input-format=", "--output-format=")):
                return True
        return False
    if cli_kind == "codex":
        # Find first positional subcommand (skip flags and common flag values)
        skip_next = False
        value_flags = {
            "-m",
            "--model",
            "-c",
            "--config",
            "-C",
            "--cd",
            "-p",
            "--profile",
            "-s",
            "--sandbox",
            "-a",
            "--ask-for-approval",
            "-i",
            "--image",
            "--disable",
            "--enable",
            "--add-dir",
            "-o",
            "--output-last-message",
            "--output-schema",
            "--color",
            "--local-provider",
            "-w",
            "--writable-root",
        }
        for a in tail:
            if skip_next:
                skip_next = False
                continue
            if a == "--":
                break
            if a in value_flags:
                skip_next = True
                continue
            if a.startswith("-"):
                continue
            if a in ("exec", "e", "app-server", "mcp-server", "completion", "debug", "cloud", "apply"):
                return True
            break
    return False


def _collect_open_agy_presence_cids(
    proc_root: Path,
) -> tuple[bool, dict[str, tuple[int, str, float]]]:
    """Scan ``/proc/<pid>/fd`` of live interactive ``agy`` processes for open ``presence/<cid>.lock``."""
    saw_agy = False
    open_locks: dict[str, tuple[int, str, float]] = {}
    if not proc_root.is_dir():
        return False, open_locks

    for entry in _iter_numeric_proc_dirs(proc_root):
        comm = _read_proc_comm(entry)
        if comm != "agy":
            continue
        cmd_args = _read_proc_cmdline_args(entry)
        if _is_headless_cli_invocation("agy", cmd_args):
            continue
        saw_agy = True
        pid = int(entry.name)
        cwd = _read_proc_cwd(entry)
        pts_mtime = _read_proc_pts_mtime(entry)
        fd_dir = entry / "fd"
        try:
            fd_names = os.listdir(fd_dir)
        except OSError:
            continue
        for fd_name in fd_names:
            target = _readlink_clean(fd_dir / fd_name)
            if not target or not target.endswith(".lock"):
                continue
            if "/presence/" not in target.replace("\\", "/"):
                continue
            cid = Path(target).stem.lower()
            if cid:
                prev = open_locks.get(cid)
                if prev is None or pts_mtime >= prev[2]:
                    open_locks[cid] = (pid, cwd, pts_mtime)
    return saw_agy, open_locks


def is_agy_presence_lock_held_nonproc(lock_path: Path) -> bool:
    """On platforms without ``/proc`` (macOS), verify ``presence/<cid>.lock`` is actively flocked.

    Uses non-blocking shared lock ``LOCK_SH | LOCK_NB`` on ``O_RDONLY`` so read-only probes
    still conflict with ``agy``'s exclusive ``LOCK_EX`` while never blocking each other or
    contending for write ownership on stale lock files.
    """
    if not lock_path.is_file():
        return False
    fd = -1
    try:
        fd = os.open(str(lock_path), os.O_RDONLY)
    except OSError:
        return False

    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError as exc:
            return exc.errno in (errno.EAGAIN, errno.EACCES)
        else:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            return False
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def _resolve_valid_agy_root_meta(
    cid: str,
    home: Path,
    proc_root: Path | None = None,
) -> tuple[bool, str, float]:
    """Return ``(is_valid_root, cwd, pts_mtime)`` for an ``agy`` conversation ID."""
    cid_low = cid.strip().lower()
    if not cid_low:
        return False, "", 0.0
    lock_file = home / ".gemini" / "antigravity-cli" / "presence" / f"{cid_low}.lock"
    if not lock_file.is_file():
        return False, "", 0.0

    tp = _resolve_agy_transcript_path(home, cid_low)
    if tp is not None and _is_agy_transcript_subagent(tp):
        return False, "", 0.0

    if proc_root is not None and proc_root.is_dir():
        _, open_locks = _collect_open_agy_presence_cids(proc_root)
        info = open_locks.get(cid_low)
        if info is None:
            return False, "", 0.0
        return True, info[1], info[2]

    held = is_agy_presence_lock_held_nonproc(lock_file)
    return held, "", 0.0


def is_agy_root_session_valid(
    cid: str,
    home: Path,
    proc_root: Path | None = None,
) -> bool:
    """Strictly verify ``cid`` holds a live ``presence/<cid>.lock`` and is not a subagent."""
    valid, _, _ = _resolve_valid_agy_root_meta(cid, home, proc_root)
    return valid


# ---------------------------------------------------------------------------
# Linux /proc helpers
# ---------------------------------------------------------------------------


def _iter_numeric_proc_dirs(proc_root: Path) -> list[Path]:
    out: list[Path] = []
    try:
        names = os.listdir(proc_root)
    except OSError:
        return out
    for name in names:
        if name.isdigit():
            out.append(proc_root / name)
    return out


def _read_proc_comm(proc_pid_dir: Path) -> str:
    try:
        return (proc_pid_dir / "comm").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _classify_proc_cli(proc_pid_dir: Path) -> str | None:
    comm = _read_proc_comm(proc_pid_dir)
    args: list[str] | None = None
    cli_kind: str | None = None

    if comm in ("codex", "agy", "claude"):
        cli_kind = comm
    elif comm == "node" or _CLAUDE_VER_COMM_RE.match(comm):
        args = _read_proc_cmdline_args(proc_pid_dir)
        for idx, arg in enumerate(args[:8]):
            norm = arg.replace("\\", "/")
            if os.path.basename(norm) == "claude" or "/claude/versions/" in norm:
                cli_kind = "claude"
                # Slice args so args[0] is the claude script entrypoint
                args = args[idx:]
                break

    if cli_kind is None:
        return None
    if args is None:
        args = _read_proc_cmdline_args(proc_pid_dir)
    if _is_headless_cli_invocation(cli_kind, args):
        return None
    return cli_kind


def _readlink_clean(path: Path) -> str:
    try:
        target = os.readlink(path)
    except OSError:
        return ""
    if target.endswith(" (deleted)"):
        return ""
    return target


def _read_proc_cwd(proc_pid_dir: Path) -> str:
    return _readlink_clean(proc_pid_dir / "cwd")


def _read_proc_pts_mtime(proc_pid_dir: Path) -> float:
    # 1. Primary: /proc/<pid>/stat tty_nr -> /dev/pts/<minor>
    try:
        stat_txt = (proc_pid_dir / "stat").read_text(encoding="utf-8", errors="replace")
        rpar = stat_txt.rfind(")")
        if rpar != -1:
            fields_after = stat_txt[rpar + 2 :].split()
            if len(fields_after) >= 5:
                tty_nr = int(fields_after[4])
                if tty_nr > 0:
                    major = (tty_nr >> 8) & 0xFFF
                    minor = (tty_nr & 0xFF) | ((tty_nr >> 12) & 0xFFF00)
                    if 136 <= major <= 143:
                        pts_idx = (major - 136) * 256 + minor
                        mt = _safe_mtime(f"/dev/pts/{pts_idx}")
                        if mt > 0:
                            return mt
    except (OSError, ValueError):
        pass

    # 2. Fallback: fd/0 when pointing to a real tty/pts device (or test pts_* fixture), never /dev/null
    fd0 = _readlink_clean(proc_pid_dir / "fd" / "0")
    if not fd0 or fd0 == "/dev/null":
        return 0.0
    try:
        st = os.stat(fd0)
    except OSError:
        return 0.0
    base = os.path.basename(fd0).lower()
    if (
        stat.S_ISCHR(st.st_mode)
        or fd0.startswith(("/dev/pts/", "/dev/tty"))
        or base.startswith(("pts", "tty"))
    ):
        return st.st_mtime
    return 0.0


def _load_codex_thread_names(home: Path) -> dict[str, str]:
    idx_file = home / ".codex" / "session_index.jsonl"
    mapping: dict[str, str] = {}
    if not idx_file.is_file():
        return mapping
    try:
        with open(idx_file, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                try:
                    obj = json.loads(s)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    sid = str(obj.get("id") or "").strip().lower()
                    tname = str(obj.get("thread_name") or "").strip()
                    if sid and tname:
                        mapping[sid] = tname
    except OSError:
        pass
    return mapping


def scan_proc_root_sessions(
    home: Path,
    proc_root: Path = Path("/proc"),
) -> list[SessionMatch]:
    """Discover active foreground root sessions for ``codex``, ``agy``, and ``claude``."""
    if not proc_root.is_dir():
        return []

    codex_names = _load_codex_thread_names(home)
    by_key: dict[str, SessionMatch] = {}

    for proc_dir in _iter_numeric_proc_dirs(proc_root):
        cli_kind = _classify_proc_cli(proc_dir)
        if cli_kind is None:
            continue
        pts_mtime = _read_proc_pts_mtime(proc_dir)
        proc_cwd = _read_proc_cwd(proc_dir)
        fd_dir = proc_dir / "fd"

        if cli_kind == "codex":
            try:
                fd_names = os.listdir(fd_dir)
            except OSError:
                continue
            for fd_name in fd_names:
                target = _readlink_clean(fd_dir / fd_name)
                if not target or not target.endswith(".jsonl"):
                    continue
                base = os.path.basename(target)
                if not base.startswith("rollout-") or not os.path.isfile(target):
                    continue
                is_cli, sid, meta_cwd = _read_codex_root_meta(target)
                if not is_cli or not sid:
                    continue
                j_mtime = _safe_mtime(target)
                cand = SessionMatch(
                    cli="codex",
                    session_id=sid,
                    jsonl_path=target,
                    cwd=meta_cwd or proc_cwd,
                    matched_by="proc_scan",
                    pts_mtime=pts_mtime,
                    jsonl_mtime=j_mtime,
                    thread_name=codex_names.get(sid, ""),
                )
                prev = by_key.get(cand.session_key)
                if prev is None or (cand.pts_mtime, cand.jsonl_mtime) > (
                    prev.pts_mtime,
                    prev.jsonl_mtime,
                ):
                    by_key[cand.session_key] = cand

        elif cli_kind == "agy":
            try:
                fd_names = os.listdir(fd_dir)
            except OSError:
                continue
            for fd_name in fd_names:
                target = _readlink_clean(fd_dir / fd_name)
                if not target or not target.endswith(".lock"):
                    continue
                if "/presence/" not in target.replace("\\", "/"):
                    continue
                cid = Path(target).stem.lower()
                if not cid or not os.path.isfile(target):
                    continue
                tp = _resolve_agy_transcript_path(home, cid)
                if tp is not None:
                    if _is_agy_transcript_subagent(tp):
                        continue
                    tp_str = str(tp)
                    j_mtime = _safe_mtime(tp)
                else:
                    # 0-message newly opened agy window holding an active presence lock
                    tp_str = str(_default_agy_expected_transcript_path(home, cid))
                    j_mtime = _safe_mtime(target)
                cand = SessionMatch(
                    cli="agy",
                    session_id=cid,
                    jsonl_path=tp_str,
                    cwd=proc_cwd,
                    matched_by="proc_scan",
                    pts_mtime=pts_mtime,
                    jsonl_mtime=j_mtime,
                )
                prev = by_key.get(cand.session_key)
                if prev is None or (cand.pts_mtime, cand.jsonl_mtime) > (
                    prev.pts_mtime,
                    prev.jsonl_mtime,
                ):
                    by_key[cand.session_key] = cand

        elif cli_kind == "claude":
            if not proc_cwd:
                continue
            best_claude: SessionMatch | None = None
            for enc in encode_claude_project_dir(proc_cwd):
                proj_dir = home / ".claude" / "projects" / enc
                if not proj_dir.is_dir():
                    continue
                for jf in proj_dir.glob("*.jsonl"):
                    if not jf.is_file():
                        continue
                    is_root, sid, meta_cwd = _read_claude_root_meta(
                        jf, allow_empty_or_meta_only=True
                    )
                    if not is_root or not sid:
                        continue
                    j_mtime = _safe_mtime(jf)
                    cand = SessionMatch(
                        cli="claude",
                        session_id=sid,
                        jsonl_path=str(jf),
                        cwd=meta_cwd or proc_cwd,
                        matched_by="proc_scan",
                        pts_mtime=pts_mtime,
                        jsonl_mtime=j_mtime,
                    )
                    if best_claude is None or cand.jsonl_mtime > best_claude.jsonl_mtime:
                        best_claude = cand
            if best_claude is not None:
                prev = by_key.get(best_claude.session_key)
                if prev is None or (best_claude.pts_mtime, best_claude.jsonl_mtime) > (
                    prev.pts_mtime,
                    prev.jsonl_mtime,
                ):
                    by_key[best_claude.session_key] = best_claude

    return list(by_key.values())


def _disambiguate_candidates(
    candidates: Sequence[SessionMatch], window_title: str
) -> SessionMatch | None:
    """Apply 4-level disambiguation: CLI/basename -> thread_name -> PTS/JSONL mtime."""
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    pool = list(candidates)
    title_low = (window_title or "").lower()

    if title_low:
        # Level 1: strict word-bounded CLI name & cwd basename match in window title
        cli_hint = _infer_cli_word_from_title(window_title)
        scored_l1: list[tuple[int, SessionMatch]] = []
        for c in pool:
            score = 0
            if cli_hint and c.cli == cli_hint:
                score += 2
            if c.cwd:
                base = os.path.basename(c.cwd.rstrip("/")).lower()
                if len(base) >= 2 and base in title_low:
                    score += 2
            scored_l1.append((score, c))
        max_l1 = max(s for s, _ in scored_l1)
        if max_l1 > 0:
            pool = [c for s, c in scored_l1 if s == max_l1]
            if len(pool) == 1:
                return pool[0]

        # Level 2: Codex session_index thread_name match in window title
        tname_matched = [
            c
            for c in pool
            if c.thread_name and len(c.thread_name) >= 2 and c.thread_name.lower() in title_low
        ]
        if tname_matched:
            pool = tname_matched
            if len(pool) == 1:
                return pool[0]

    # Prefer interactive sessions attached to a controlling terminal over headless processes
    with_tty = [c for c in pool if c.pts_mtime > 0]
    if with_tty:
        pool = with_tty

    # Level 3 & 4: Controlling terminal /dev/pts/N mtime & root JSONL mtime latest
    pool.sort(
        key=lambda c: (max(c.pts_mtime, c.jsonl_mtime), c.jsonl_mtime, c.pts_mtime),
        reverse=True,
    )
    return pool[0]


# ---------------------------------------------------------------------------
# Runtime registry & directory lookup helpers
# ---------------------------------------------------------------------------


def _is_trusted_runtime_dir(rt_dir: Path) -> bool:
    try:
        st = os.lstat(rt_dir)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return False
    if (st.st_mode & 0o077) != 0:
        return False
    return st.st_uid == os.getuid()


def _read_trusted_registry_json(reg_file: Path) -> dict[str, Any] | None:
    """Open registry file with ``O_NOFOLLOW``, verify regular file/owner/0600, and parse JSON."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(reg_file), flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.getuid()
            or (st.st_mode & 0o077) != 0
            or st.st_size > 262144
        ):
            return None
        chunks: list[bytes] = []
        while True:
            buf = os.read(fd, 16384)
            if not buf:
                break
            chunks.append(buf)
    except OSError:
        return None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        rec = json.loads(b"".join(chunks).decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def write_session_registry_entry(
    runtime_dir: str | Path,
    *,
    cli: str,
    session_id: str,
    jsonl_path: str,
    cwd: str = "",
) -> Path:
    """Atomically write ``${runtime_dir}/sessions/<cli>-<sid[:8]>.json`` (0700 dirs, 0600 file)."""
    cli_low = cli.strip().lower()
    if cli_low not in ("codex", "claude", "agy"):
        raise ValueError(f"unsupported cli for session registry: {cli!r}")
    sid_low = session_id.strip().lower()
    if not _REG_SID_RE.match(sid_low):
        raise ValueError(f"invalid session_id for session registry: {session_id!r}")

    rt_path = ensure_secure_dir(runtime_dir)
    sessions_dir = ensure_secure_dir(rt_path / "sessions")

    prefix8 = sid_low[:8]
    target = sessions_dir / f"{cli_low}-{prefix8}.json"
    nonce = uuid.uuid4().hex[:12]
    tmp = sessions_dir / f".{cli_low}-{prefix8}.{os.getpid()}.{nonce}.tmp"
    payload = {
        "cli": cli_low,
        "session_id": sid_low,
        "cwd": cwd,
        "jsonl_path": jsonl_path,
        "transcript_path": jsonl_path,
        "updated_at": time.time(),
    }
    open_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(tmp), open_flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    else:
        os.close(fd)

    try:
        os.replace(str(tmp), str(target))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def _ordered_clis(preferred: str | None) -> tuple[str, ...]:
    all_clis = ("codex", "claude", "agy")
    if preferred in all_clis:
        return (preferred, *[c for c in all_clis if c != preferred])
    return all_clis


def _lookup_in_registry(
    hint: TitleHint,
    runtime_dirs: Sequence[Path],
    home: Path,
    proc_root: Path | None,
) -> SessionMatch | None:
    prefix8 = hint.session_token[:8].lower()
    clis = (hint.cli,) if (hint.explicit_cli and hint.cli) else _ordered_clis(hint.cli)
    hits: list[tuple[int, int, float, SessionMatch]] = []

    for dir_idx, rt_dir in enumerate(runtime_dirs):
        if not _is_trusted_runtime_dir(rt_dir):
            continue
        sdir = rt_dir / "sessions"
        if not _is_trusted_runtime_dir(sdir):
            continue
        for cli_rank, cli in enumerate(clis):
            if not cli:
                continue
            reg_file = sdir / f"{cli}-{prefix8}.json"
            rec = _read_trusted_registry_json(reg_file)
            if rec is None:
                continue
            sid = str(rec.get("session_id") or "").strip().lower()
            if not sid or not sid.startswith(hint.session_token):
                continue
            raw_path = str(rec.get("jsonl_path") or rec.get("transcript_path") or "").strip()
            reg_cwd = str(rec.get("cwd") or "").strip()
            updated_at = 0.0
            try:
                updated_at = float(rec.get("updated_at") or 0.0)
            except (TypeError, ValueError):
                updated_at = 0.0

            if cli == "agy":
                valid_agy, live_cwd, _ = _resolve_valid_agy_root_meta(sid, home, proc_root)
                if not valid_agy:
                    continue
                tp = _resolve_agy_transcript_path(home, sid)
                if tp is not None:
                    raw_path = str(tp)
                elif raw_path:
                    raw_path = _upgrade_agy_transcript_path(raw_path)
                else:
                    raw_path = str(_default_agy_expected_transcript_path(home, sid))
                if not reg_cwd and live_cwd:
                    reg_cwd = live_cwd

            elif cli == "codex":
                if not raw_path:
                    continue
                if os.path.isfile(raw_path) and os.path.getsize(raw_path) > 0:
                    is_cli, meta_sid, meta_cwd = _read_codex_root_meta(raw_path)
                    if not is_cli:
                        continue
                    sid = sid or meta_sid
                    if not reg_cwd and meta_cwd:
                        reg_cwd = meta_cwd

            elif cli == "claude":
                if not raw_path:
                    continue
                if os.path.isfile(raw_path) and os.path.getsize(raw_path) > 0:
                    is_root, meta_sid, meta_cwd = _read_claude_root_meta(
                        raw_path, allow_empty_or_meta_only=True
                    )
                    if not is_root:
                        continue
                    sid = sid or meta_sid
                    if not reg_cwd and meta_cwd:
                        reg_cwd = meta_cwd

            hits.append(
                (
                    -dir_idx,
                    -cli_rank,
                    updated_at,
                    SessionMatch(
                        cli=cli,
                        session_id=sid,
                        jsonl_path=raw_path,
                        cwd=reg_cwd,
                        matched_by="registry",
                        pts_mtime=updated_at,
                        jsonl_mtime=_safe_mtime(raw_path),
                    ),
                )
            )

    if not hits:
        return None
    hits.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return hits[0][3]


def _lookup_by_full_uuid(
    hint: TitleHint,
    home: Path,
    proc_root: Path | None,
) -> SessionMatch | None:
    uid = hint.session_token.lower()
    clis = (hint.cli,) if (hint.explicit_cli and hint.cli) else _ordered_clis(hint.cli)

    for cli in clis:
        if cli == "codex":
            pattern = str(home / ".codex" / "sessions" / "*" / "*" / "*" / f"rollout-*-{uid}.jsonl")
            matches = sorted(glob.glob(pattern), key=_safe_mtime, reverse=True)
            for mp in matches:
                if not os.path.isfile(mp):
                    continue
                is_cli, sid, cwd = _read_codex_root_meta(mp)
                if not is_cli:
                    continue
                return SessionMatch(
                    cli="codex",
                    session_id=sid or uid,
                    jsonl_path=mp,
                    cwd=cwd,
                    matched_by="title_uuid",
                    jsonl_mtime=_safe_mtime(mp),
                )
        elif cli == "agy":
            valid_agy, live_cwd, live_pts = _resolve_valid_agy_root_meta(uid, home, proc_root)
            if not valid_agy:
                continue
            tp = _resolve_agy_transcript_path(home, uid)
            tp_str = str(tp) if tp is not None else str(_default_agy_expected_transcript_path(home, uid))
            return SessionMatch(
                cli="agy",
                session_id=uid,
                jsonl_path=tp_str,
                cwd=live_cwd,
                matched_by="title_uuid",
                pts_mtime=live_pts,
                jsonl_mtime=_safe_mtime(tp_str),
            )
        elif cli == "claude":
            pattern = str(home / ".claude" / "projects" / "*" / f"{uid}.jsonl")
            matches = sorted(glob.glob(pattern), key=_safe_mtime, reverse=True)
            for mp in matches:
                if not os.path.isfile(mp):
                    continue
                is_root, sid, cwd = _read_claude_root_meta(mp, allow_empty_or_meta_only=True)
                if not is_root:
                    continue
                return SessionMatch(
                    cli="claude",
                    session_id=sid or uid,
                    jsonl_path=mp,
                    cwd=cwd,
                    matched_by="title_uuid",
                    jsonl_mtime=_safe_mtime(mp),
                )
    return None


def _lookup_by_short_prefix(
    hint: TitleHint,
    home: Path,
    proc_root: Path | None,
    proc_candidates: Sequence[SessionMatch],
) -> SessionMatch | None:
    prefix = hint.session_token.lower()
    clis = (hint.cli,) if (hint.explicit_cli and hint.cli) else _ordered_clis(hint.cli)

    # 1. Prefer live /proc foreground root sessions matching the prefix
    proc_hits = [
        c
        for c in proc_candidates
        if c.session_id.lower().startswith(prefix) and c.cli in clis
    ]
    if proc_hits:
        proc_hits.sort(
            key=lambda c: (
                1 if c.cli == hint.cli else 0,
                max(c.pts_mtime, c.jsonl_mtime),
                c.jsonl_mtime,
            ),
            reverse=True,
        )
        top = proc_hits[0]
        return SessionMatch(
            cli=top.cli,
            session_id=top.session_id,
            jsonl_path=top.jsonl_path,
            cwd=top.cwd,
            matched_by="title_prefix",
            pts_mtime=top.pts_mtime,
            jsonl_mtime=top.jsonl_mtime,
            thread_name=top.thread_name,
        )

    # 2. Prefix match in session storage directories (strictly active/root sessions only)
    dir_hits: list[SessionMatch] = []
    for cli in clis:
        if cli == "codex":
            pattern = str(
                home / ".codex" / "sessions" / "*" / "*" / "*" / f"rollout-*-{prefix}*.jsonl"
            )
            for mp in glob.glob(pattern):
                if not os.path.isfile(mp):
                    continue
                is_cli, sid, cwd = _read_codex_root_meta(mp)
                if is_cli and sid.startswith(prefix):
                    dir_hits.append(
                        SessionMatch(
                            cli="codex",
                            session_id=sid,
                            jsonl_path=mp,
                            cwd=cwd,
                            matched_by="title_prefix",
                            jsonl_mtime=_safe_mtime(mp),
                        )
                    )
        elif cli == "agy":
            presence_dir = home / ".gemini" / "antigravity-cli" / "presence"
            if presence_dir.is_dir():
                for lf in presence_dir.glob(f"{prefix}*.lock"):
                    cid = lf.stem.lower()
                    if not cid.startswith(prefix):
                        continue
                    valid_agy, live_cwd, live_pts = _resolve_valid_agy_root_meta(
                        cid, home, proc_root
                    )
                    if not valid_agy:
                        continue
                    tp = _resolve_agy_transcript_path(home, cid)
                    tp_str = (
                        str(tp)
                        if tp is not None
                        else str(_default_agy_expected_transcript_path(home, cid))
                    )
                    dir_hits.append(
                        SessionMatch(
                            cli="agy",
                            session_id=cid,
                            jsonl_path=tp_str,
                            cwd=live_cwd,
                            matched_by="title_prefix",
                            pts_mtime=live_pts,
                            jsonl_mtime=_safe_mtime(tp_str),
                        )
                    )
        elif cli == "claude":
            pattern = str(home / ".claude" / "projects" / "*" / f"{prefix}*.jsonl")
            for mp in glob.glob(pattern):
                if not os.path.isfile(mp):
                    continue
                is_root, sid, cwd = _read_claude_root_meta(mp, allow_empty_or_meta_only=True)
                if is_root and sid.startswith(prefix):
                    dir_hits.append(
                        SessionMatch(
                            cli="claude",
                            session_id=sid,
                            jsonl_path=mp,
                            cwd=cwd,
                            matched_by="title_prefix",
                            jsonl_mtime=_safe_mtime(mp),
                        )
                    )

    if not dir_hits:
        return None
    dir_hits.sort(
        key=lambda c: (1 if c.cli == hint.cli else 0, c.jsonl_mtime),
        reverse=True,
    )
    return dir_hits[0]


def _scan_nonproc_fallback_sessions(
    home: Path,
    runtime_dirs: Sequence[Path],
) -> list[SessionMatch]:
    """Fallback for macOS (no ``/proc``): active runtime registry + recent root transcripts."""
    codex_names = _load_codex_thread_names(home)
    by_key: dict[str, SessionMatch] = {}
    key_tier: dict[str, int] = {}

    for dir_idx, rt_dir in enumerate(runtime_dirs):
        if not _is_trusted_runtime_dir(rt_dir):
            continue
        sdir = rt_dir / "sessions"
        if not _is_trusted_runtime_dir(sdir):
            continue
        for reg_file in sdir.glob("*.json"):
            rec = _read_trusted_registry_json(reg_file)
            if rec is None:
                continue
            cli = str(rec.get("cli") or "").strip().lower()
            sid = str(rec.get("session_id") or "").strip().lower()
            raw_path = str(rec.get("jsonl_path") or rec.get("transcript_path") or "").strip()
            if cli not in ("codex", "claude", "agy") or not sid:
                continue
            if cli == "agy":
                if not is_agy_root_session_valid(sid, home, None):
                    continue
                tp = _resolve_agy_transcript_path(home, sid)
                raw_path = (
                    str(tp)
                    if tp is not None
                    else (
                        _upgrade_agy_transcript_path(raw_path)
                        if raw_path
                        else str(_default_agy_expected_transcript_path(home, sid))
                    )
                )
            else:
                if not raw_path:
                    continue
                if cli == "codex" and os.path.isfile(raw_path) and os.path.getsize(raw_path) > 0:
                    if not _read_codex_root_meta(raw_path)[0]:
                        continue
                if cli == "claude" and os.path.isfile(raw_path) and os.path.getsize(raw_path) > 0:
                    if not _read_claude_root_meta(raw_path, allow_empty_or_meta_only=True)[0]:
                        continue
            cand = SessionMatch(
                cli=cli,
                session_id=sid,
                jsonl_path=raw_path,
                cwd=str(rec.get("cwd") or ""),
                matched_by="recent_root",
                pts_mtime=float(rec.get("updated_at") or 0.0),
                jsonl_mtime=_safe_mtime(raw_path),
                thread_name=codex_names.get(sid, "") if cli == "codex" else "",
            )
            prev = by_key.get(cand.session_key)
            prev_tier = key_tier.get(cand.session_key, 999999)
            if prev is None or (-dir_idx, cand.pts_mtime) >= (-prev_tier, prev.pts_mtime):
                by_key[cand.session_key] = cand
                key_tier[cand.session_key] = dir_idx

    codex_files = sorted(
        glob.glob(str(home / ".codex" / "sessions" / "*" / "*" / "*" / "rollout-*.jsonl")),
        key=_safe_mtime,
        reverse=True,
    )[:12]
    for cf in codex_files:
        is_cli, sid, cwd = _read_codex_root_meta(cf)
        if is_cli and sid:
            key = f"codex:{sid}"
            if key not in by_key:
                by_key[key] = SessionMatch(
                    cli="codex",
                    session_id=sid,
                    jsonl_path=cf,
                    cwd=cwd,
                    matched_by="recent_root",
                    jsonl_mtime=_safe_mtime(cf),
                    thread_name=codex_names.get(sid, ""),
                )

    claude_files = sorted(
        glob.glob(str(home / ".claude" / "projects" / "*" / "*.jsonl")),
        key=_safe_mtime,
        reverse=True,
    )[:12]
    for clf in claude_files:
        if not os.path.isfile(clf):
            continue
        is_root, sid, cwd = _read_claude_root_meta(clf)
        if is_root and sid:
            key = f"claude:{sid}"
            if key not in by_key:
                by_key[key] = SessionMatch(
                    cli="claude",
                    session_id=sid,
                    jsonl_path=clf,
                    cwd=cwd,
                    matched_by="recent_root",
                    jsonl_mtime=_safe_mtime(clf),
                )

    presence_dir = home / ".gemini" / "antigravity-cli" / "presence"
    if presence_dir.is_dir():
        for lf in presence_dir.glob("*.lock"):
            cid = lf.stem.lower()
            if not is_agy_root_session_valid(cid, home, None):
                continue
            tp = _resolve_agy_transcript_path(home, cid)
            tp_str = (
                str(tp)
                if tp is not None
                else str(_default_agy_expected_transcript_path(home, cid))
            )
            key = f"agy:{cid}"
            if key not in by_key:
                by_key[key] = SessionMatch(
                    cli="agy",
                    session_id=cid,
                    jsonl_path=tp_str,
                    cwd="",
                    matched_by="recent_root",
                    jsonl_mtime=_safe_mtime(tp_str),
                )

    return list(by_key.values())


# ---------------------------------------------------------------------------
# Per-CLI JSONL strict whitelist cleaners
# ---------------------------------------------------------------------------

_AGY_TRAILING_META_RE = re.compile(
    r"\n<(?:ADDITIONAL_METADATA|SKILL|USER_SETTINGS_CHANGE)\b.*",
    re.DOTALL,
)

# Negative backtick lookarounds prevent inline-code mentions like `<system-reminder>`
# from wiping prose between two code spans.
_CLAUDE_SYSTEM_REMINDER_RE = re.compile(
    r"(?<!`)<system-reminder>(?!`).*?(?<!`)</system-reminder>(?!`)",
    re.DOTALL | re.IGNORECASE,
)
_CLAUDE_USER_STRIP_PATTERNS: tuple[re.Pattern[str], ...] = (
    _CLAUDE_SYSTEM_REMINDER_RE,
    re.compile(
        r"(?<!`)<task-notification>(?!`).*?(?<!`)</task-notification>(?!`)",
        re.DOTALL | re.IGNORECASE,
    ),
    re.compile(
        r"(?<!`)<(local-command-[a-zA-Z0-9_-]+)(?:\s[^>]*)?>(?!`).*?(?<!`)</\1>(?!`)",
        re.DOTALL | re.IGNORECASE,
    ),
    re.compile(
        r"(?<!`)<(command-[a-zA-Z0-9_-]+)(?:\s[^>]*)?>(?!`).*?(?<!`)</\1>(?!`)",
        re.DOTALL | re.IGNORECASE,
    ),
    re.compile(
        r"(?<!`)<(bash-[a-zA-Z0-9_-]+)(?:\s[^>]*)?>(?!`).*?(?<!`)</\1>(?!`)",
        re.DOTALL | re.IGNORECASE,
    ),
)
_CLAUDE_USER_ORPHAN_TAG_RE = re.compile(
    r"(?<!`)</?(?:task-notification|system-reminder|local-command-[a-zA-Z0-9_-]+|command-[a-zA-Z0-9_-]+|bash-[a-zA-Z0-9_-]+)(?:\s[^>]*)?>(?!`)",
    re.IGNORECASE,
)

_CODEX_ENV_CTX_RE = re.compile(
    r"<environment_context>.*?</environment_context>", re.DOTALL | re.IGNORECASE
)
_CODEX_PERM_CTX_RE = re.compile(
    r"<permissions instructions>.*?</permissions instructions>", re.DOTALL | re.IGNORECASE
)


def _clean_agy_record(obj: Mapping[str, Any]) -> tuple[str, str] | None:
    src = obj.get("source")
    typ = obj.get("type")
    if src == "USER_EXPLICIT" and typ == "USER_INPUT":
        raw = obj.get("content")
        if not isinstance(raw, str) or not raw.strip():
            return None
        trimmed = _AGY_TRAILING_META_RE.sub("", raw).strip()
        open_tag = "<USER_REQUEST>"
        close_tag = "</USER_REQUEST>"
        start = trimmed.find(open_tag)
        end = trimmed.rfind(close_tag)
        if 0 <= start < end:
            text = trimmed[start + len(open_tag) : end].strip()
        else:
            text = trimmed
        return ("user", text) if text else None

    if src == "MODEL" and typ == "PLANNER_RESPONSE":
        # Strictly ignore MODEL:GENERIC, VIEW_FILE, CODE_ACTION, thinking, and tool_calls.
        raw = obj.get("content")
        if not isinstance(raw, str) or not raw.strip():
            return None
        return ("assistant", raw.strip())

    return None


def _clean_claude_text(raw: str, *, role: str) -> str:
    s = raw
    if role == "user":
        for pat in _CLAUDE_USER_STRIP_PATTERNS:
            s = pat.sub("", s)
        s = _CLAUDE_USER_ORPHAN_TAG_RE.sub("", s)
    else:
        s = _CLAUDE_SYSTEM_REMINDER_RE.sub("", s)
    return s.strip()


def _clean_claude_record(obj: Mapping[str, Any]) -> tuple[str, str] | None:
    if (
        bool(obj.get("isSidechain"))
        or bool(obj.get("isMeta"))
        or bool(obj.get("isApiErrorMessage"))
        or bool(obj.get("agentId"))
        or bool(obj.get("agent_id"))
        or obj.get("entrypoint") == "sdk-cli"
    ):
        return None
    typ = obj.get("type")
    if typ not in ("user", "assistant"):
        return None
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    raw_parts: list[str] = []
    if isinstance(content, str):
        raw_parts.append(content)
    elif isinstance(content, list):
        for block in content:
            # Exclude tool_use, tool_result, thinking, redacted_thinking, image, etc.
            if isinstance(block, dict) and block.get("type") == "text":
                txt = block.get("text")
                if isinstance(txt, str) and txt:
                    raw_parts.append(txt)
    if not raw_parts:
        return None
    cleaned = _clean_claude_text("\n".join(raw_parts), role=typ)
    if not cleaned:
        return None
    return (typ, cleaned)


def _extract_codex_item_completed(payload: Mapping[str, Any]) -> tuple[str, str] | None:
    item = payload.get("item")
    if not isinstance(item, dict):
        return None
    itype = item.get("type")
    if itype == "UserMessage":
        role = "user"
    elif itype == "AgentMessage":
        role = "assistant"
    else:
        return None

    chunks: list[str] = []
    content = item.get("content")
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and str(c.get("type") or "").lower() == "text":
                t = c.get("text")
                if isinstance(t, str) and t:
                    chunks.append(t)
    elif isinstance(item.get("text"), str):
        chunks.append(item["text"])

    joined = "\n".join(chunks).strip()
    return (role, joined) if joined else None


def _extract_codex_response_item(payload: Mapping[str, Any]) -> tuple[str, str] | None:
    if payload.get("type") != "message":
        return None
    role = payload.get("role")
    if role not in ("user", "assistant"):
        return None
    content = payload.get("content")
    if not isinstance(content, list):
        return None
    chunks: list[str] = []
    for c in content:
        if not isinstance(c, dict):
            continue
        ctype = str(c.get("type") or "").lower()
        if ctype not in ("input_text", "output_text", "text"):
            continue
        raw_txt = c.get("text")
        if not isinstance(raw_txt, str) or not raw_txt.strip():
            continue
        s = raw_txt.strip()
        if s.startswith("# AGENTS.md instructions"):
            continue
        s = _CODEX_ENV_CTX_RE.sub("", s)
        s = _CODEX_PERM_CTX_RE.sub("", s).strip()
        if not s or s.startswith("# AGENTS.md instructions"):
            continue
        chunks.append(s)
    joined = "\n".join(chunks).strip()
    return (role, joined) if joined else None


# ---------------------------------------------------------------------------
# Git status & technical identifier hotword extraction
# ---------------------------------------------------------------------------

_TECH_IDENT_RE = re.compile(
    r"\b(?:[A-Za-z0-9_-]+\.(?:py|sh|ps1|toml|json|md|ts|tsx|js|rs|go|yaml|yml)|[A-Za-z_][A-Za-z0-9_]*_[A-Za-z0-9_]+|[a-z]+(?:[A-Z][a-z0-9]+)+)\b"
)


def read_git_status_summary(
    cwd: str, *, timeout_sec: float = 1.5, max_lines: int = 40
) -> tuple[str, list[str]]:
    """Run read-only ``git status --short`` (<= 1.5s timeout) and extract changed filenames."""
    if not cwd or not os.path.isdir(cwd):
        return "", []
    cmd = [
        "git",
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "-C",
        cwd,
        "status",
        "--short",
        "--untracked-files=normal",
    ]
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout_sec,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "", []

    if proc.returncode != 0:
        return "", []

    raw_lines = proc.stdout.decode("utf-8", errors="replace").splitlines()
    kept_lines = [ln.rstrip() for ln in raw_lines if ln.strip()][:max_lines]
    hotwords: list[str] = []
    seen: set[str] = set()
    for ln in kept_lines:
        path_part = ln[3:].strip() if len(ln) > 3 else ln.strip()
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[1].strip()
        path_part = path_part.strip('"')
        base = os.path.basename(path_part.rstrip("/"))
        if base and base not in seen:
            seen.add(base)
            hotwords.append(base)
        stem = os.path.splitext(base)[0]
        if stem and len(stem) >= 3 and stem not in seen:
            seen.add(stem)
            hotwords.append(stem)

    return "\n".join(kept_lines), hotwords


def extract_turn_hotwords(
    turns: Sequence[tuple[str, str]], *, max_words: int = 40
) -> list[str]:
    """Extract recent snake_case / camelCase / filename tokens from recent clean turns."""
    found: list[str] = []
    seen: set[str] = set()
    for _, text in reversed(turns[-12:]):
        for m in _TECH_IDENT_RE.finditer(text):
            tok = m.group(0)
            if 3 <= len(tok) <= 48 and tok not in seen:
                seen.add(tok)
                found.append(tok)
                if len(found) >= max_words:
                    return found
    return found


def format_turns_text(turns: Sequence[tuple[str, str]], max_chars: int = 16000) -> str:
    if not turns or max_chars <= 0:
        return ""
    blocks: list[str] = []
    total = 0
    for role, text in reversed(turns):
        tag = "User" if role == "user" else "Assistant"
        entry = f"[{tag}]\n{text}"
        if total + len(entry) + 2 > max_chars:
            rem = max_chars - total - len(f"[{tag}]\n") - 2
            if rem > 80:
                blocks.append(f"[{tag}]\n{text[-rem:]}")
            break
        blocks.append(entry)
        total += len(entry) + 2
    blocks.reverse()
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# SessionTracker (stateful byte_offset & Luna delta tracker)
# ---------------------------------------------------------------------------


def _hash_file_prefix(f: BinaryIO, length: int) -> tuple[int, bytes]:
    """Compute ``blake2b(digest_size=16)`` of the first ``length`` bytes starting at offset 0."""
    f.seek(0)
    h = hashlib.blake2b(digest_size=16)
    rem = length
    read_total = 0
    while rem > 0:
        chunk = f.read(min(65536, rem))
        if not chunk:
            break
        h.update(chunk)
        read_total += len(chunk)
        rem -= len(chunk)
    return read_total, h.digest()


class SessionTracker:
    """Thread-safe stateful session resolver and incremental JSONL clean-turn extractor."""

    def __init__(
        self,
        *,
        home_dir: str | Path | None = None,
        runtime_dir: str | Path | None = None,
        proc_root: str | Path | None = "/proc",
        env: Mapping[str, str] | None = None,
        git_cache_ttl_sec: float = 2.0,
    ) -> None:
        self.env = os.environ if env is None else env
        self.home = (
            Path(home_dir)
            if home_dir is not None
            else Path((self.env.get("HOME") or "").strip() or Path.home())
        )
        self._explicit_runtime_dir = runtime_dir is not None
        self.runtime_dir = (
            Path(runtime_dir)
            if runtime_dir is not None
            else get_runtime_dir(self.env, create=False)
        )
        self.proc_root = Path(proc_root) if proc_root is not None else None
        self.git_cache_ttl_sec = git_cache_ttl_sec
        self._lock = threading.RLock()
        self._states: dict[str, SessionState] = {}
        self._git_cache: dict[str, tuple[float, str, list[str]]] = {}

    def _candidate_runtime_dirs(self) -> list[Path]:
        dirs: list[Path] = [self.runtime_dir]
        if not self._explicit_runtime_dir or self.runtime_dir.name == "agent-voice-input":
            uid = os.getuid()
            parent = self.runtime_dir.parent
            fallbacks: list[Path] = [
                parent / "voice-input",
                parent / f"voice-input-{uid}",
            ]
            if not self._explicit_runtime_dir:
                xdg = (self.env.get("XDG_RUNTIME_DIR") or "").strip()
                if xdg:
                    xp = Path(xdg)
                    fallbacks.extend(
                        [xp / "agent-voice-input", xp / "voice-input", xp / f"voice-input-{uid}"]
                    )
                cache_base = Path(
                    (self.env.get("XDG_CACHE_HOME") or "").strip() or (self.home / ".cache")
                )
                fallbacks.extend(
                    [
                        cache_base / "agent-voice-input",
                        cache_base / "voice-input",
                        cache_base / f"voice-input-{uid}",
                    ]
                )
            for cand in fallbacks:
                if cand not in dirs:
                    dirs.append(cand)
        return dirs

    def get_state(self, session_key: str) -> SessionState | None:
        with self._lock:
            return self._states.get(session_key)

    def resolve_session(self, window_title: str = "") -> SessionMatch | None:
        """Resolve foreground main CLI session via title O(1) lookup or ``/proc`` fallback."""
        with self._lock:
            rt_dirs = self._candidate_runtime_dirs()
            hint = parse_window_title_hint(window_title)

            if hint is not None:
                reg_hit = _lookup_in_registry(hint, rt_dirs, self.home, self.proc_root)
                if reg_hit is not None:
                    return reg_hit
                if hint.is_full_uuid:
                    return _lookup_by_full_uuid(hint, self.home, self.proc_root)

                proc_cands = (
                    scan_proc_root_sessions(self.home, self.proc_root)
                    if (self.proc_root is not None and self.proc_root.is_dir())
                    else []
                )
                # Explicit session tag in title that matches no valid root session MUST return None
                # rather than silently guessing an unrelated window's conversation!
                return _lookup_by_short_prefix(hint, self.home, self.proc_root, proc_cands)

            if self.proc_root is not None and self.proc_root.is_dir():
                proc_cands = scan_proc_root_sessions(self.home, self.proc_root)
                if proc_cands:
                    return _disambiguate_candidates(proc_cands, window_title)

            fallback_cands = _scan_nonproc_fallback_sessions(self.home, rt_dirs)
            return _disambiguate_candidates(fallback_cands, window_title)

    def _reset_state(self, state: SessionState, new_path: str) -> None:
        state.jsonl_path = new_path
        state.byte_offset = 0
        state.file_id = None
        state.last_mtime_ns = 0
        state.prefix_hasher = hashlib.blake2b(digest_size=16)
        state.clean_turns.clear()
        state.luna_synced_idx = 0
        state.codex_saw_item_completed = False
        state.codex_item_turns.clear()
        state.codex_resp_turns.clear()
        state.generation += 1

    def _consume_parsed_record(self, state: SessionState, obj: Mapping[str, Any]) -> None:
        if not state.cwd and isinstance(obj.get("cwd"), str) and obj["cwd"].strip():
            state.cwd = obj["cwd"].strip()

        if state.cli == "agy":
            turn = _clean_agy_record(obj)
            if turn is not None:
                state.clean_turns.append(turn)

        elif state.cli == "claude":
            turn = _clean_claude_record(obj)
            if turn is not None:
                state.clean_turns.append(turn)

        elif state.cli == "codex":
            typ = obj.get("type")
            payload = obj.get("payload")
            if typ == "session_meta" and isinstance(payload, dict) and not state.cwd:
                if isinstance(payload.get("cwd"), str):
                    state.cwd = payload["cwd"].strip()
            elif typ == "event_msg" and isinstance(payload, dict):
                if payload.get("type") == "item_completed":
                    turn = _extract_codex_item_completed(payload)
                    if turn is not None:
                        if not state.codex_saw_item_completed:
                            # Preserve legacy response_item history (e.g. codex resume of older rollout)
                            # while deduplicating the immediately preceding matching user message.
                            hist = list(state.codex_resp_turns)
                            if hist and hist[-1] == turn:
                                hist.pop()
                            state.codex_item_turns = hist
                            state.codex_resp_turns.clear()
                            state.codex_saw_item_completed = True
                        state.codex_item_turns.append(turn)
            elif typ == "response_item" and isinstance(payload, dict):
                if not state.codex_saw_item_completed:
                    turn = _extract_codex_response_item(payload)
                    if turn is not None:
                        state.codex_resp_turns.append(turn)

    def _verify_consumed_prefix(self, f: BinaryIO, state: SessionState) -> bool:
        """Verify ``[0 : state.byte_offset]`` has not been rewritten in-place."""
        if state.byte_offset <= 0:
            return True
        nread, digest = _hash_file_prefix(f, state.byte_offset)
        return nread == state.byte_offset and digest == state.prefix_hasher.digest()

    def _ingest_jsonl_delta(self, state: SessionState) -> None:
        path = state.jsonl_path
        if state.cli == "agy":
            upgraded = _upgrade_agy_transcript_path(path)
            if upgraded != path:
                self._reset_state(state, upgraded)
                path = upgraded

        try:
            with open(path, "rb") as f:
                st = os.fstat(f.fileno())
                current_fid = (st.st_dev, st.st_ino)

                if state.file_id is not None and state.file_id != current_fid:
                    # Atomic replacement (new inode)
                    self._reset_state(state, path)
                elif st.st_size < state.byte_offset:
                    # Truncated / shorter rewrite
                    self._reset_state(state, path)
                elif state.byte_offset > 0:
                    if not self._verify_consumed_prefix(f, state):
                        # Caught in-place overwrite anywhere in [0 : byte_offset] regardless of file size
                        self._reset_state(state, path)
                    elif st.st_size == state.byte_offset:
                        state.file_id = current_fid
                        state.last_mtime_ns = st.st_mtime_ns
                        return

                if st.st_size == 0 and state.byte_offset == 0:
                    state.file_id = current_fid
                    state.last_mtime_ns = st.st_mtime_ns
                    return

                f.seek(state.byte_offset)
                prev_saw_item = state.codex_saw_item_completed
                prev_resp_count = len(state.codex_resp_turns)
                ingested_any = False

                while True:
                    # Bounded binary readline splitting strictly on b'\n' (safe on \r / U+2028)
                    first_chunk = f.readline(65536)
                    if not first_chunk:
                        break
                    if first_chunk.endswith(b"\n"):
                        raw_line: bytes | None = first_chunk
                        line_len = len(first_chunk)
                        state.prefix_hasher.update(first_chunk)
                    else:
                        line_hasher = state.prefix_hasher.copy()
                        line_hasher.update(first_chunk)
                        chunks: list[bytes] | None = [first_chunk]
                        line_len = len(first_chunk)
                        saw_nl = False
                        while True:
                            piece = f.readline(65536)
                            if not piece:
                                break
                            line_hasher.update(piece)
                            line_len += len(piece)
                            if chunks is not None:
                                if line_len <= MAX_INGEST_LINE_BYTES:
                                    chunks.append(piece)
                                else:
                                    chunks = None
                            if piece.endswith(b"\n"):
                                saw_nl = True
                                break
                        if not saw_nl:
                            # Incomplete half-line at EOF: leave byte_offset and hasher untouched
                            break
                        state.prefix_hasher = line_hasher
                        raw_line = b"".join(chunks) if chunks is not None else None

                    ingested_any = True
                    state.byte_offset += line_len

                    if raw_line is None:
                        # Oversized single line (> 16 MiB): skip JSON parse to bound daemon memory
                        continue

                    body = raw_line[:-1].strip()
                    if not body:
                        continue
                    try:
                        obj = json.loads(body.decode("utf-8", errors="replace"))
                    except ValueError:
                        continue
                    if isinstance(obj, dict):
                        self._consume_parsed_record(state, obj)

                state.file_id = current_fid
                state.last_mtime_ns = st.st_mtime_ns
        except OSError:
            if state.byte_offset > 0 and not os.path.exists(path):
                self._reset_state(state, path)
            return

        if ingested_any and state.cli == "codex":
            if state.codex_saw_item_completed:
                if not prev_saw_item and prev_resp_count > 0:
                    # Switched from fallback response_item stream to item_completed stream
                    state.luna_synced_idx = 0
                    state.generation += 1
                state.clean_turns = list(state.codex_item_turns)
            else:
                state.clean_turns = list(state.codex_resp_turns)

    def _get_git_info(self, cwd: str) -> tuple[str, list[str]]:
        if not cwd:
            return "", []
        now = time.monotonic()
        cached = self._git_cache.get(cwd)
        if cached is not None and (now - cached[0]) < self.git_cache_ttl_sec:
            return cached[1], list(cached[2])
        status_txt, hw = read_git_status_summary(cwd)
        self._git_cache[cwd] = (now, status_txt, hw)
        return status_txt, list(hw)

    def sync_session(
        self,
        match: SessionMatch | None = None,
        *,
        window_title: str = "",
    ) -> SessionSnapshot | None:
        """Resolve (if needed) and incrementally ingest new JSONL lines for the active session."""
        with self._lock:
            eff_match = match if match is not None else self.resolve_session(window_title)
            if eff_match is None:
                return None

            norm_path = (
                _upgrade_agy_transcript_path(eff_match.jsonl_path)
                if eff_match.cli == "agy"
                else eff_match.jsonl_path
            )

            state = self._states.get(eff_match.session_key)
            if state is None:
                state = SessionState(
                    session_key=eff_match.session_key,
                    cli=eff_match.cli,
                    session_id=eff_match.session_id,
                    jsonl_path=norm_path,
                    cwd=eff_match.cwd,
                )
                self._states[eff_match.session_key] = state
            else:
                if eff_match.cwd and not state.cwd:
                    state.cwd = eff_match.cwd
                if norm_path != state.jsonl_path:
                    self._reset_state(state, norm_path)

            self._ingest_jsonl_delta(state)

            eff_cwd = state.cwd or eff_match.cwd
            if eff_cwd != eff_match.cwd or state.jsonl_path != eff_match.jsonl_path:
                eff_match = SessionMatch(
                    cli=eff_match.cli,
                    session_id=eff_match.session_id,
                    jsonl_path=state.jsonl_path,
                    cwd=eff_cwd,
                    matched_by=eff_match.matched_by,
                    pts_mtime=eff_match.pts_mtime,
                    jsonl_mtime=_safe_mtime(state.jsonl_path) or eff_match.jsonl_mtime,
                    thread_name=eff_match.thread_name,
                )

            git_status, git_hw = self._get_git_info(eff_cwd)
            turn_hw = extract_turn_hotwords(state.clean_turns)
            merged_hw = tuple(dict.fromkeys([*git_hw, *turn_hw]))

            return SessionSnapshot(
                match=eff_match,
                clean_turns=tuple(state.clean_turns),
                luna_synced_idx=state.luna_synced_idx,
                generation=state.generation,
                git_status=git_status,
                hotwords=merged_hw,
                byte_offset=state.byte_offset,
            )

    def get_luna_delta(
        self,
        session_key: str,
        *,
        full: bool = False,
        max_chars: int = 12000,
    ) -> tuple[str, int, int]:
        """Return ``(context_delta_text, new_synced_idx, generation)`` for Stage 2 Luna thread."""
        with self._lock:
            state = self._states.get(session_key)
            if state is None:
                return "", 0, 0
            start_idx = 0 if full else min(state.luna_synced_idx, len(state.clean_turns))
            delta_turns = state.clean_turns[start_idx:]
            sections: list[str] = []
            git_status, git_hw = self._get_git_info(state.cwd)
            if full and state.cwd:
                sections.append(f"cwd: {state.cwd}")
            if git_status:
                sections.append(f"git_status:\n{git_status}")
            if git_hw:
                sections.append("changed_files: " + ", ".join(git_hw[:30]))
            dialog_str = format_turns_text(delta_turns, max_chars=max_chars)
            if dialog_str:
                sections.append(f"dialog_delta:\n{dialog_str}")
            return "\n\n".join(sections), len(state.clean_turns), state.generation

    def mark_luna_synced(
        self, session_key: str, synced_idx: int, generation: int | None = None
    ) -> None:
        with self._lock:
            state = self._states.get(session_key)
            if state is None:
                return
            if generation is not None and state.generation != generation:
                return
            state.luna_synced_idx = max(0, min(synced_idx, len(state.clean_turns)))

    @staticmethod
    def build_asr_context(
        snapshot: SessionSnapshot | None,
        *,
        extra_hotwords: Sequence[str] = (),
        max_dialog_chars: int = 16000,
    ) -> AsrContext:
        if snapshot is None:
            return AsrContext(hotwords=list(dict.fromkeys(extra_hotwords)))
        merged_hw = list(dict.fromkeys([*extra_hotwords, *snapshot.hotwords]))
        return AsrContext(
            hotwords=merged_hw,
            git_status=snapshot.git_status,
            dialog_text=snapshot.format_dialog(max_chars=max_dialog_chars),
        )
