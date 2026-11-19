"""Active CLI session resolution and incremental JSONL clean context extraction.

Supports ``codex``, ``claude`` (Claude Code), and ``agy`` (Antigravity CLI) across:
1. O(1) window title session tag / UUID lookup (runtime registry + direct path / prefix);
2. ``tmux`` multi-pane active-pane focus convergence and split TTY/CWD resolution (Ghostty / macOS);
3. Linux ``/proc`` foreground root-session discovery (strictly excluding background subagents
   and stale ``agy`` presence locks) with disambiguation fallback, plus macOS ``ps``/``lsof`` fallback;
4. Streaming byte-offset incremental JSONL reading (safe against concurrent half-line writes,
   ``\\r``/``\\x0c`` control bytes, atomic replacement, and equal/longer in-place rewrites)
   with strict per-CLI whitelist filtering and ``git status`` hotword extraction.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field, replace
import fcntl
import fnmatch
import glob
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import stat
import subprocess
import sys
import threading
import time
from typing import Any, BinaryIO, Callable, Collection, Iterable, Iterator, Mapping, Sequence
import urllib.parse
import uuid

from ._remote_probe import (
    DEFAULT_REFINER_CHUNK_CHARS,
    DEFAULT_REFINER_CONTEXT_MAX_CHARS,
    MAX_LINE_BYTES,
    _AGY_WS_CWD_RE,
    _BROAD_SYSTEM_DIRS_LOW,
    _FULL_UUID_RE,
    _HASH_TAG_RE,
    _PREFIXED_TAG_RE,
    _REPO_CORE_EXTS,
    _REPO_PRIORITY_ROOTS,
    _REPO_SKIP_BASES,
    _REPO_SKIP_DIRS,
    _REPO_TEST_FILE_RE,
    _SHALLOW_DOC_AND_CODE_EXTS,
    _SHALLOW_SKIP_BASES_LOW,
    _SHALLOW_SKIP_DIRS_LOW,
    _STRICT_UUID_RE,
    _classify_interactive_cli,
    _classify_proc_cli,
    _clean_agy_record,
    _clean_claude_record,
    _cwd_match_rank,
    _extract_agy_compaction_summary,
    _extract_agy_cwd,
    _extract_claude_cmdline_sid,
    _extract_claude_message_text,
    _extract_codex_compacted_turns,
    _extract_codex_item_completed,
    _extract_codex_response_item,
    _infer_cli_word,
    _is_agy_subagent,
    _is_broad_or_unsafe_workspace_dir,
    _is_claude_sidechain_or_subagent,
    _is_flock_held,
    _is_headless_cli,
    _is_path_under_dir,
    _is_trusted_runtime_dir,
    _is_wsl_slow_mount_str,
    _load_codex_thread_names,
    _match_cli_process,
    _normalize_cwd,
    _ordered_clis,
    _read_bounded_line,
    _read_claude_root_meta,
    _read_codex_root_meta,
    _read_proc_cmdline_args,
    _read_proc_comm,
    _read_proc_ppid,
    _read_trusted_registry_json,
    _readlink_clean,
    _resolve_agy_transcript_path,
    _safe_mtime,
    _safe_realpath,
    encode_claude_project_dir,
    trim_turns_chunk_aligned,
)
from ._tmux import (
    _MAX_TMUX_SOCKET_TARGETS,
    _TMUX_PANE_ID_RE,
    TmuxClientInfo,
    TmuxPaneInfo,
    TmuxServerSnapshot,
    _is_live_owned_socket,
    _norm_same_path,
    _same_tmux_server,
    parse_tmux_clients_output,
    parse_tmux_panes_output,
    parse_tmux_probe_output,
    parse_tmux_socket_from_env,
    preferred_tmux_socket_arg,
    probe_tmux_snapshot,
    resolve_tmux_bin,
)
from .app_scope import is_remote_ide_window, parse_ide_window_title
from .llm_common import AsrContext
from .config import RemoteConfig, RemoteHostConfig, ensure_secure_dir, get_runtime_dir
from .remote_ssh import (
    RemoteForegroundInfo,
    RemoteSshManager,
    collect_local_hostnames,
    detect_remote_foreground,
    extract_local_prompt_cwd,
    extract_ssh_target_from_argv,
    parse_window_host_tag,
    strip_host_tags_from_title,
)
from .workspace_ctx import (
    BUILTIN_AGENT_HOTWORDS,
    REPO_FILES_CACHE_TTL_SEC,
    SessionMatch,
    _AGY_BRAIN_EVIDENCE_CACHE,
    _AGY_BRAIN_EVIDENCE_LOCK,
    _AgyIdeEvidence,
    _dedupe_hotwords_ci,
    _effective_is_wsl,
    _expand_home_prefix,
    _file_stems_for_hotwords,
    _has_git_marker,
    _is_inside_git_worktree,
    _keep_tech_token,
    _resolve_safe_local_dir,
    extract_turn_hotwords,
    is_wsl_slow_mount_path,
    read_git_status_summary,
    read_repo_core_files,
    read_workspace_shallow_files,
    resolve_antigravity_ide_session,
    resolve_ide_workspace_cwd,
)

MAX_META_LINE_BYTES = MAX_LINE_BYTES  # 16 MiB safety cap per metadata/ingest line
MAX_INGEST_LINE_BYTES = MAX_LINE_BYTES
_REG_SID_RE = re.compile(r"^[0-9a-f]{8}[0-9a-f-]{0,56}$")

# ---------------------------------------------------------------------------
# Window title hint parsing
# ---------------------------------------------------------------------------

_WIN_EXPLICIT_NON_WSL_TITLE_RE = re.compile(
    r"^(?:Administrator:\s+|管理员:\s*)?(?:Command Prompt|命令提示符|PS\s+[A-Za-z]:\\|[A-Za-z]:\\)",
    re.IGNORECASE,
)
_GENERIC_LOCAL_TERM_TITLE_RE = re.compile(
    r"^(?:\[\d+/\d+\]\s+|\d+:\d+(?:\.\d+)?\*?\s+|[\u25cf\u2022*]\s+)*"
    r"(?:-?(?:bash|zsh|fish|sh|dash|ash|ksh|tcsh|csh|nu|pwsh|powershell|login)"
    r"|windows\s+powershell|windows\s+terminal|terminal|ghostty|iterm2?|alacritty|kitty"
    r"|wezterm|warp|konsole|gnome[- ]terminal|foot|tilix|terminator"
    r"|ubuntu(?:[- ][0-9.]+)?(?:\s+lts)?|debian(?:[- ][0-9.]+)?|arch(?:\s*linux)?|fedora"
    r"|kali(?:\s*linux)?|alpine|nixos|opensuse|wsl2?|linux)"
    r"(?:\s*(?:[—–-]|\s)\s*\d+\s*[×x]\s*\d+)?\s*$",
    re.IGNORECASE,
)
_WORD_TMUX_RE = re.compile(
    r"(?:^|[\s\[\(\{·|:])tmux(?=$|[\s\]\)\}·|:*])",
    re.IGNORECASE,
)
_TUI_SPINNER_RE = re.compile(r"[\u2733\u2800-\u28ff]")
_CTRL_STRIP_RE = re.compile(r"[\x00-\x1f\x7f]")


def _build_generic_pane_titles() -> frozenset[str]:
    names = {
        "bash",
        "zsh",
        "fish",
        "sh",
        "dash",
        "ksh",
        "tcsh",
        "csh",
        "nu",
        "pwsh",
        "powershell",
        "tmux",
        "ubuntu",
        "debian",
        "linux",
        "localhost",
    }
    if hasattr(os, "uname"):
        try:
            node = os.uname().nodename.strip().lower()
            if node:
                names.add(node)
                names.add(node.split(".", 1)[0])
        except OSError:
            pass
    return frozenset(names)


_GENERIC_SHELL_TITLES = _build_generic_pane_titles()


def _is_meaningful_pane_title(ptitle_low: str) -> bool:
    return len(ptitle_low) >= 4 and ptitle_low not in _GENERIC_SHELL_TITLES


@dataclass(frozen=True)
class TitleHint:
    session_token: str
    is_full_uuid: bool
    cli: str | None = None
    explicit_cli: bool = False
    host_tag: str | None = None


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
    claude_just_compacted: bool = False
    is_remote: bool = False
    host_name: str = ""
    remote_etag: str = ""
    remote_max_chars: int = 0
    remote_evicted_chunks: int = 0
    remote_compaction_count: int = 0
    remote_git_status: str = ""
    remote_git_hotwords: list[str] = field(default_factory=list)
    remote_repo_files: list[str] = field(default_factory=list)
    remote_shallow_files: list[str] = field(default_factory=list)


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


_infer_cli_word_from_title = _infer_cli_word


def parse_window_title_hint(window_title: str) -> TitleHint | None:
    """Extract O(1) session tag or Codex UUID from foreground terminal window title."""
    if not window_title or not window_title.strip():
        return None

    host_tag, _, _ = parse_window_host_tag(window_title)
    clean_title = strip_host_tags_from_title(window_title)

    prefixed_matches = list(_PREFIXED_TAG_RE.finditer(clean_title))
    if prefixed_matches:
        m = prefixed_matches[-1]
        cli = m.group(1).lower()
        tok = m.group(2).lower()
        return TitleHint(
            session_token=tok,
            is_full_uuid=bool(_STRICT_UUID_RE.match(tok)),
            cli=cli,
            explicit_cli=True,
            host_tag=host_tag,
        )

    hash_matches = list(_HASH_TAG_RE.finditer(clean_title))
    if hash_matches:
        tok = hash_matches[-1].group(1).lower()
        return TitleHint(
            session_token=tok,
            is_full_uuid=bool(_STRICT_UUID_RE.match(tok)),
            cli=_infer_cli_word_from_title(clean_title),
            explicit_cli=False,
            host_tag=host_tag,
        )

    uuid_matches = list(_FULL_UUID_RE.finditer(clean_title))
    if uuid_matches:
        tok = uuid_matches[-1].group(1).lower()
        return TitleHint(
            session_token=tok,
            is_full_uuid=True,
            cli=_infer_cli_word_from_title(clean_title) or "codex",
            explicit_cli=False,
            host_tag=host_tag,
        )

    return None


# ---------------------------------------------------------------------------
# Root-session predicates, TTY/CWD normalization & process helpers
# ---------------------------------------------------------------------------


def normalize_tty_path(raw_tty: str) -> str:
    """Normalize a TTY name or path (e.g. ``ttys019`` -> ``/dev/ttys019``), excluding ``?``/``??``."""
    s = (raw_tty or "").strip()
    if not s or s in ("?", "??", "-", "/dev/tty", "/dev/null"):
        return ""
    if s.startswith("/dev/"):
        norm = "/" + "/".join(part for part in s.split("/") if part)
        if norm.startswith("/dev/pts/") and len(norm) > len("/dev/pts/"):
            return norm
        if norm.startswith("/dev/tty") and norm != "/dev/tty":
            return norm
        return ""
    if s.startswith("/") and os.path.basename(s).lower().startswith(("pts", "tty")) and os.path.exists(s):
        return "/" + "/".join(part for part in s.split("/") if part)
    if s.startswith("pts/") and len(s) > 4:
        return f"/dev/{s}"
    if s.startswith("tty") and len(s) > 3:
        return f"/dev/{s}"
    if re.fullmatch(r"s\d+", s):
        return f"/dev/{s}" if os.path.exists(f"/dev/{s}") else f"/dev/tty{s}"
    for cand in (f"/dev/{s}", f"/dev/tty{s}"):
        if os.path.exists(cand):
            return cand
    return ""


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


def _upgrade_agy_transcript_path(path_str: str) -> str:
    if not path_str:
        return path_str
    p = Path(path_str)
    if p.name == "transcript.jsonl":
        full_p = p.with_name("transcript_full.jsonl")
        if _effective_is_wsl():
            safe_full = _safe_realpath(str(full_p), wsl=True)
            if safe_full and os.path.isfile(safe_full):
                return str(full_p)
        elif full_p.is_file():
            return str(full_p)
    return str(p)


_is_agy_transcript_subagent = _is_agy_subagent
_extract_agy_transcript_cwd = _extract_agy_cwd


def _split_cmdline(raw_args: str) -> list[str]:
    return (raw_args or "").split()


def _parse_ps_table(output: str) -> list[tuple[int, int, str, str, list[str]]]:
    rows: list[tuple[int, int, str, str, list[str]]] = []
    for raw_ln in output.splitlines():
        ln = raw_ln.strip()
        if not ln:
            continue
        parts = ln.split(None, 4)
        if len(parts) < 4:
            continue
        try:
            row_pid = int(parts[0])
            row_ppid = int(parts[1])
        except ValueError:
            continue
        stat_str = parts[2]
        tty_path = normalize_tty_path(parts[3])
        argv = _split_cmdline(parts[4] if len(parts) > 4 else "")
        rows.append((row_pid, row_ppid, stat_str, tty_path, argv))
    return rows


def ps_proc_info(pid: int) -> tuple[str, int, str, list[str], bool] | None:
    """Query ``(comm, ppid, tty_path, argv, is_session_leader)`` for ``pid`` via ``ps``."""
    if pid <= 0:
        return None
    try:
        r = subprocess.run(
            ["ps", "-ww", "-o", "pid=,ppid=,stat=,tty=,args=", "-p", str(pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    rows = _parse_ps_table(r.stdout.decode("utf-8", errors="ignore"))
    if not rows:
        return None
    chosen = next((row for row in rows if row[0] == pid), rows[0])
    _, ppid, stat_str, tty_path, argv = chosen
    comm = os.path.basename(argv[0].replace("\\", "/")).lstrip("-") if argv else ""
    return comm, ppid, tty_path, argv, ("s" in stat_str)


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _query_pids_open_files_and_cwds(
    pids: Sequence[int],
    proc_root: Path | None = None,
    *,
    timeout: float = 1.5,
) -> tuple[dict[int, list[str]], dict[int, str]]:
    """Query open file paths and CWDs for ``pids`` via ``/proc/<pid>`` or non-blocking ``lsof``."""
    valid_pids = list(dict.fromkeys(p for p in pids if p > 0))
    if not valid_pids:
        return {}, {}
    files_by_pid: dict[int, list[str]] = {}
    cwd_by_pid: dict[int, str] = {}
    if proc_root is not None and proc_root.is_dir():
        for pid in valid_pids:
            proc_dir = proc_root / str(pid)
            pcwd = _read_proc_cwd(proc_dir)
            if pcwd:
                cwd_by_pid[pid] = pcwd
            fd_dir = proc_dir / "fd"
            try:
                fd_names = os.listdir(fd_dir)
            except OSError:
                continue
            targets = [t for fn in fd_names if (t := _readlink_clean(fd_dir / fn))]
            if targets:
                files_by_pid[pid] = targets
        return files_by_pid, cwd_by_pid

    pid_csv = ",".join(str(p) for p in valid_pids[:32])
    try:
        lr = subprocess.run(
            ["lsof", "-b", "-n", "-P", "-a", "-w", "-d", "cwd,0-999", "-p", pid_csv, "-Fn"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}, {}
    if lr.returncode != 0 and not lr.stdout:
        return {}, {}
    cur_pid = 0
    cur_fd = ""
    for ln in lr.stdout.decode("utf-8", errors="replace").splitlines():
        if ln.startswith("p") and ln[1:].isdigit():
            cur_pid = int(ln[1:])
            cur_fd = ""
        elif ln.startswith("f"):
            cur_fd = ln[1:]
        elif ln.startswith("n") and cur_pid > 0:
            path_val = ln[1:]
            if cur_fd == "cwd":
                if path_val.startswith("/"):
                    cwd_by_pid[cur_pid] = _normalize_cwd(path_val, wsl=_effective_is_wsl())
            elif cur_fd == "":
                files_by_pid.setdefault(cur_pid, []).append(path_val)
                if (
                    path_val.startswith("/")
                    and cur_pid not in cwd_by_pid
                    and not path_val.endswith((".jsonl", ".lock"))
                ):
                    cwd_by_pid[cur_pid] = _normalize_cwd(path_val, wsl=_effective_is_wsl())
            else:
                files_by_pid.setdefault(cur_pid, []).append(path_val)
    return files_by_pid, cwd_by_pid


def _query_pid_cwd_nonproc(pid: int) -> str:
    if pid <= 0:
        return ""
    _, cwd_by_pid = _query_pids_open_files_and_cwds((pid,), None, timeout=1.0)
    return cwd_by_pid.get(pid, "")


def _query_pid_agy_lock_cids(pid: int, proc_root: Path | None = None) -> list[str]:
    """Return open ``presence/<cid>.lock`` stems held by ``pid`` via ``/proc`` or ``lsof``."""
    if pid <= 0:
        return []
    files_by_pid, _ = _query_pids_open_files_and_cwds((pid,), proc_root)
    out: list[str] = []
    for t in files_by_pid.get(pid, ()):
        if t.endswith(".lock") and "/presence/" in t.replace("\\", "/"):
            cid = Path(t).stem.lower()
            if cid and cid not in out:
                out.append(cid)
    return out


def _query_pid_codex_rollout_paths(
    pid: int, home: Path, proc_root: Path | None = None
) -> list[str]:
    """Return open ``rollout-*.jsonl`` realpaths under ``home / .codex`` held by ``pid``."""
    return _query_pids_codex_rollout_paths((pid,), home, proc_root)


def _safe_wsl_dir(dir_path: Path, *, wsl: bool) -> Path | None:
    if not wsl:
        return dir_path if dir_path.is_dir() else None
    real = _resolve_safe_local_dir(str(dir_path), wsl=True)
    return Path(real) if real else None


def _safe_wsl_glob_files(
    root_dir: Path, depth: int, file_glob: str, *, wsl: bool
) -> list[str]:
    """Match ``file_glob`` ``depth`` levels below ``root_dir`` without following WSL ``/mnt/`` symlinks."""
    if not wsl:
        pat = str(root_dir.joinpath(*(["*"] * (depth - 1)), file_glob))
        return [p for p in glob.glob(pat) if os.path.isfile(p)]
    safe_root = _safe_wsl_dir(root_dir, wsl=True)
    if safe_root is None:
        return []
    dirs: list[Path] = [safe_root]
    for _ in range(depth - 1):
        next_dirs: list[Path] = []
        for d in dirs:
            try:
                with os.scandir(d) as it:
                    for entry in it:
                        if entry.name.startswith("."):
                            continue
                        sd = _safe_wsl_dir(Path(entry.path), wsl=True)
                        if sd is not None:
                            next_dirs.append(sd)
            except OSError:
                continue
        dirs = next_dirs
        if not dirs:
            return []
    allow_hidden_file = file_glob.startswith(".")
    matched: list[str] = []
    for d in dirs:
        try:
            with os.scandir(d) as it:
                for entry in it:
                    if not allow_hidden_file and entry.name.startswith("."):
                        continue
                    if not fnmatch.fnmatch(entry.name, file_glob):
                        continue
                    safe_f = _safe_realpath(entry.path, wsl=True)
                    if safe_f and os.path.isfile(safe_f):
                        matched.append(entry.path)
        except OSError:
            continue
    return matched


def _query_pids_codex_rollout_paths(
    pids: Sequence[int], home: Path, proc_root: Path | None = None
) -> list[str]:
    eff_wsl = _effective_is_wsl()
    safe_codex_root = _safe_wsl_dir(home / ".codex", wsl=eff_wsl)
    if safe_codex_root is None:
        return []
    files_by_pid, _ = _query_pids_open_files_and_cwds(pids, proc_root)
    if not files_by_pid:
        return []
    out: list[str] = []
    for targets in files_by_pid.values():
        for t in targets:
            if (
                not t.endswith(".jsonl")
                or not os.path.basename(t).startswith("rollout-")
                or not _is_path_under_dir(t, safe_codex_root, wsl=eff_wsl)
            ):
                continue
            real_t = _safe_realpath(t, wsl=eff_wsl)
            if real_t and os.path.isfile(real_t) and real_t not in out:
                out.append(real_t)
    return out


_is_headless_cli_invocation = _is_headless_cli


def _ps_pid_matches_cli(pid: int, expected_cli: str) -> bool:
    pinfo = ps_proc_info(pid)
    if pinfo is None:
        return False
    return _classify_interactive_cli(pinfo[0], pinfo[3]) == expected_cli


def _collect_open_agy_presence_cids(
    proc_root: Path,
) -> tuple[bool, dict[str, tuple[int, str, float]]]:
    """Scan ``/proc/<pid>/fd`` of live interactive ``agy`` processes for open ``presence/<cid>.lock``."""
    saw_agy = False
    open_locks: dict[str, tuple[int, str, float]] = {}
    if not proc_root.is_dir():
        return False, open_locks

    agy_entries: list[tuple[int, float]] = []
    for entry in _iter_numeric_proc_dirs(proc_root):
        if _classify_proc_cli(entry) != "agy":
            continue
        saw_agy = True
        agy_entries.append((int(entry.name), _read_proc_pts_mtime(entry)))
    if not agy_entries:
        return saw_agy, open_locks

    files_by_pid, cwd_by_pid = _query_pids_open_files_and_cwds(
        [pid for pid, _ in agy_entries], proc_root
    )
    for pid, pts_mtime in agy_entries:
        cwd = cwd_by_pid.get(pid, "")
        for target in files_by_pid.get(pid, ()):
            if not target.endswith(".lock") or "/presence/" not in target.replace("\\", "/"):
                continue
            cid = Path(target).stem.lower()
            if cid:
                prev = open_locks.get(cid)
                if prev is None or pts_mtime >= prev[2]:
                    open_locks[cid] = (pid, cwd, pts_mtime)
    return saw_agy, open_locks


is_agy_presence_lock_held_nonproc = _is_flock_held


def _resolve_valid_agy_root_meta(
    cid: str,
    home: Path,
    proc_root: Path | None = None,
) -> tuple[bool, str, float]:
    """Return ``(is_valid_root, cwd, pts_mtime)`` for an ``agy`` conversation ID."""
    cid_low = cid.strip().lower()
    if not cid_low:
        return False, "", 0.0
    eff_wsl = _effective_is_wsl()
    safe_presence = _safe_wsl_dir(
        home / ".gemini" / "antigravity-cli" / "presence", wsl=eff_wsl
    )
    if safe_presence is None:
        return False, "", 0.0
    lock_candidate = safe_presence / f"{cid_low}.lock"
    if eff_wsl:
        safe_lock = _safe_realpath(str(lock_candidate), wsl=True)
        if not safe_lock or not os.path.isfile(safe_lock):
            return False, "", 0.0
        lock_file = Path(safe_lock)
    else:
        if not lock_candidate.is_file():
            return False, "", 0.0
        lock_file = lock_candidate

    tp = _resolve_agy_transcript_path(home, cid_low, wsl=eff_wsl)
    if tp is not None and _is_agy_transcript_subagent(tp):
        return False, "", 0.0

    if proc_root is not None and proc_root.is_dir():
        _, open_locks = _collect_open_agy_presence_cids(proc_root)
        info = open_locks.get(cid_low)
        if info is None:
            return False, "", 0.0
        return True, info[1], info[2]

    held = is_agy_presence_lock_held_nonproc(lock_file)
    if not held:
        return False, "", 0.0
    agy_cwd = _extract_agy_transcript_cwd(tp, wsl=eff_wsl) if tp is not None else ""
    return True, agy_cwd, 0.0


def is_agy_root_session_valid(
    cid: str,
    home: Path,
    proc_root: Path | None = None,
) -> bool:
    """Strictly verify ``cid`` holds a live ``presence/<cid>.lock`` and is not a subagent."""
    valid, _, _ = _resolve_valid_agy_root_meta(cid, home, proc_root)
    return valid


# ---------------------------------------------------------------------------
# Linux /proc & storage discovery helpers
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


def _read_proc_cwd(proc_pid_dir: Path) -> str:
    return _normalize_cwd(_readlink_clean(proc_pid_dir / "cwd"), wsl=_effective_is_wsl())


def _read_proc_tty_path(proc_pid_dir: Path) -> str:
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
                        pts_path = f"/dev/pts/{(major - 136) * 256 + minor}"
                        if os.path.exists(pts_path):
                            return pts_path
    except (OSError, ValueError):
        pass

    # 2. Fallback: fd/0 when pointing to a real tty/pts device (or test pts_* fixture), never /dev/null
    fd0 = _readlink_clean(proc_pid_dir / "fd" / "0")
    if not fd0 or fd0 == "/dev/null":
        return ""
    try:
        st = os.stat(fd0)
    except OSError:
        return ""
    base = os.path.basename(fd0).lower()
    if (
        stat.S_ISCHR(st.st_mode)
        or fd0.startswith(("/dev/pts/", "/dev/tty"))
        or base.startswith(("pts", "tty"))
    ):
        return fd0
    return ""


def _read_proc_pts_mtime(proc_pid_dir: Path) -> float:
    tty_path = _read_proc_tty_path(proc_pid_dir)
    return _safe_mtime(tty_path) if tty_path else 0.0


def _read_proc_stat_state_pgrp_tpgid(proc_pid_dir: Path) -> tuple[str, int, int]:
    """Return ``(proc_state_char, pgrp, tpgid)`` from ``/proc/<pid>/stat``."""
    try:
        stat_txt = (proc_pid_dir / "stat").read_text(encoding="utf-8", errors="replace")
        rpar = stat_txt.rfind(")")
        if rpar != -1:
            fields_after = stat_txt[rpar + 2 :].split()
            if len(fields_after) >= 6:
                return fields_after[0], int(fields_after[2]), int(fields_after[5])
            if len(fields_after) >= 3:
                return fields_after[0], int(fields_after[2]), 0
    except (OSError, ValueError):
        pass
    return "", 0, 0


def _collect_descendant_pids(proc_root: Path, root_pids: Sequence[int]) -> set[int]:
    seeds = {p for p in root_pids if p > 1}
    if not seeds or not proc_root.is_dir():
        return set()
    children_by_ppid: dict[int, list[int]] = {}
    existing: set[int] = set()
    for pdir in _iter_numeric_proc_dirs(proc_root):
        pid = int(pdir.name)
        existing.add(pid)
        ppid = _read_proc_ppid(pdir)
        if ppid > 1:
            children_by_ppid.setdefault(ppid, []).append(pid)
    visited: set[int] = set()
    stack = [p for p in seeds if p in existing]
    while stack:
        cur = stack.pop()
        if cur in visited:
            continue
        visited.add(cur)
        for ch in children_by_ppid.get(cur, ()):
            if ch not in visited:
                stack.append(ch)
    return visited


def _collect_pane_pids(proc_root: Path, pane_pid: int, pane_tty: str = "") -> set[int]:
    if not proc_root.is_dir():
        return set()
    if pane_pid > 1:
        return _collect_descendant_pids(proc_root, (pane_pid,))
    if not pane_tty:
        return set()
    norm_tty = os.path.realpath(pane_tty) if os.path.exists(pane_tty) else pane_tty
    matched: set[int] = set()
    for pdir in _iter_numeric_proc_dirs(proc_root):
        ptty = _read_proc_tty_path(pdir)
        if not ptty:
            continue
        norm_p = os.path.realpath(ptty) if os.path.exists(ptty) else ptty
        if norm_p == norm_tty:
            matched.add(int(pdir.name))
    return matched


def _collect_live_cli_pids(proc_root: Path, allowed_pids: set[int]) -> dict[str, set[int]]:
    out: dict[str, set[int]] = {}
    if not allowed_pids or not proc_root.is_dir():
        return out
    for pid in allowed_pids:
        pdir = proc_root / str(pid)
        cli = _classify_proc_cli(pdir)
        if cli is not None:
            out.setdefault(cli, set()).add(pid)
    return out


def _dir_basename_in_title(window_title: str, dir_path: str) -> bool:
    if not window_title or not dir_path:
        return False
    base = os.path.basename(dir_path.rstrip("/\\"))
    if not base:
        return False
    tail = r"(?:[/\\]+)?(?=$|[\s:·\]\)\}\"'])"
    if len(base) == 1:
        pat = rf"[/\\]{re.escape(base)}{tail}"
    else:
        pat = rf"(?:^|[\s/\\:·\[\(\{{\"']){re.escape(base)}{tail}"
    return bool(re.search(pat, window_title, re.IGNORECASE))


def _agy_transcript_and_mtime(
    home: Path, cid: str, fallback_path: str | Path | None = None
) -> tuple[str, float]:
    eff_wsl = _effective_is_wsl()
    tp = _resolve_agy_transcript_path(home, cid, wsl=eff_wsl)
    if tp is not None:
        return str(tp), _safe_mtime(tp)
    tp_str = str(_default_agy_expected_transcript_path(home, cid))
    fb_target = str(fallback_path) if fallback_path is not None else tp_str
    if eff_wsl and not _safe_realpath(fb_target, wsl=True):
        return tp_str, 0.0
    return tp_str, _safe_mtime(fb_target)


def _build_agy_match(
    cid: str,
    *,
    home: Path,
    proc_root: Path | None,
    matched_by: str,
    fallback_cwd: str = "",
) -> SessionMatch | None:
    cid_low = cid.strip().lower()
    valid_agy, live_cwd, live_pts = _resolve_valid_agy_root_meta(cid_low, home, proc_root)
    if not valid_agy:
        return None
    eff_wsl = _effective_is_wsl()
    lock_file = home / ".gemini" / "antigravity-cli" / "presence" / f"{cid_low}.lock"
    tp_str, j_mtime = _agy_transcript_and_mtime(home, cid_low, lock_file)
    return SessionMatch(
        cli="agy",
        session_id=cid_low,
        jsonl_path=tp_str,
        cwd=live_cwd or _normalize_cwd(fallback_cwd, wsl=eff_wsl),
        matched_by=matched_by,
        pts_mtime=live_pts,
        jsonl_mtime=j_mtime,
    )


def _collect_codex_storage_matches(
    home: Path,
    *,
    matched_by: str,
    file_glob: str = "rollout-*.jsonl",
    sid_prefix: str = "",
    fallback_sid: str = "",
    eff_cwd: str = "",
    limit: int = 0,
    codex_names: Mapping[str, str] | None = None,
) -> list[SessionMatch]:
    eff_wsl = _effective_is_wsl()
    files = sorted(
        _safe_wsl_glob_files(home / ".codex" / "sessions", 4, file_glob, wsl=eff_wsl),
        key=_safe_mtime,
        reverse=True,
    )
    if limit > 0:
        files = files[:limit]
    names = _load_codex_thread_names(home, wsl=eff_wsl) if codex_names is None else codex_names
    out: list[SessionMatch] = []
    for cf in files:
        is_cli, sid, meta_cwd = _read_codex_root_meta(cf, wsl=eff_wsl)
        eff_sid = sid or fallback_sid
        if not is_cli or not eff_sid or (sid_prefix and not eff_sid.startswith(sid_prefix)):
            continue
        if eff_cwd and _cwd_match_rank(eff_cwd, meta_cwd, home, wsl=eff_wsl) == 0:
            continue
        out.append(
            SessionMatch(
                cli="codex",
                session_id=eff_sid,
                jsonl_path=cf,
                cwd=meta_cwd or eff_cwd,
                matched_by=matched_by,
                jsonl_mtime=_safe_mtime(cf),
                thread_name=names.get(eff_sid, ""),
            )
        )
    return out


def _collect_claude_storage_matches(
    home: Path,
    *,
    matched_by: str,
    file_glob: str = "*.jsonl",
    sid_prefix: str = "",
    fallback_sid: str = "",
    eff_cwd: str = "",
    filter_cwd: bool = False,
    include_global: bool = True,
    allow_empty_global: bool = False,
    pts_mtime: float = 0.0,
    limit: int = 0,
    claimed_claude_sids: Mapping[str, int] | None = None,
    owner_pid: int = 0,
) -> list[SessionMatch]:
    eff_wsl = _effective_is_wsl()
    safe_projects = _safe_wsl_dir(home / ".claude" / "projects", wsl=eff_wsl)
    if safe_projects is None:
        return []
    cands: list[SessionMatch] = []
    seen_paths: set[str] = set()
    if eff_cwd:
        for enc in encode_claude_project_dir(eff_cwd, wsl=eff_wsl):
            proj_files = sorted(
                _safe_wsl_glob_files(safe_projects / enc, 1, file_glob, wsl=eff_wsl),
                key=_safe_mtime,
                reverse=True,
            )
            for jf_str in proj_files:
                seen_paths.add(jf_str)
                jf = Path(jf_str)
                is_root, sid, meta_cwd = _read_claude_root_meta(
                    jf, allow_empty_or_meta_only=True, wsl=eff_wsl
                )
                eff_sid = sid or fallback_sid
                if not is_root or not eff_sid or (sid_prefix and not eff_sid.startswith(sid_prefix)):
                    continue
                if claimed_claude_sids:
                    claimed_owner = claimed_claude_sids.get(eff_sid, 0)
                    if claimed_owner not in (0, owner_pid):
                        continue
                cands.append(
                    SessionMatch(
                        cli="claude",
                        session_id=eff_sid,
                        jsonl_path=jf_str,
                        cwd=meta_cwd or eff_cwd,
                        matched_by=matched_by,
                        pts_mtime=pts_mtime,
                        jsonl_mtime=_safe_mtime(jf),
                    )
                )
    if include_global and (not filter_cwd or not cands):
        claude_files = sorted(
            _safe_wsl_glob_files(safe_projects, 2, file_glob, wsl=eff_wsl),
            key=_safe_mtime,
            reverse=True,
        )
        if limit > 0:
            claude_files = claude_files[:limit]
        for clf in claude_files:
            if clf in seen_paths:
                continue
            is_root, sid, meta_cwd = _read_claude_root_meta(
                clf, allow_empty_or_meta_only=allow_empty_global, wsl=eff_wsl
            )
            eff_sid = sid or fallback_sid
            if not is_root or not eff_sid or (sid_prefix and not eff_sid.startswith(sid_prefix)):
                continue
            if claimed_claude_sids:
                claimed_owner = claimed_claude_sids.get(eff_sid, 0)
                if claimed_owner not in (0, owner_pid):
                    continue
            if filter_cwd and eff_cwd and _cwd_match_rank(eff_cwd, meta_cwd, home, wsl=eff_wsl) == 0:
                continue
            cands.append(
                SessionMatch(
                    cli="claude",
                    session_id=eff_sid,
                    jsonl_path=clf,
                    cwd=meta_cwd or (eff_cwd if filter_cwd else ""),
                    matched_by=matched_by,
                    pts_mtime=pts_mtime,
                    jsonl_mtime=_safe_mtime(clf),
                )
            )
    return cands


def scan_proc_root_sessions(
    home: Path,
    proc_root: Path = Path("/proc"),
    *,
    allowed_pids: set[int] | None = None,
    excluded_pids: set[int] | None = None,
    claimed_claude_sids: Mapping[str, int] | None = None,
) -> list[SessionMatch]:
    """Discover active foreground root sessions for ``codex``, ``agy``, and ``claude``."""
    if not proc_root.is_dir():
        return []
    if allowed_pids is not None:
        if not allowed_pids:
            return []
        proc_dirs = [
            proc_root / str(pid)
            for pid in sorted(allowed_pids)
            if pid > 0 and (proc_root / str(pid)).is_dir()
        ]
    else:
        proc_dirs = _iter_numeric_proc_dirs(proc_root)

    live_proc_entries: list[tuple[int, Path, str, float]] = []
    for proc_dir in proc_dirs:
        pid = int(proc_dir.name)
        if excluded_pids is not None and pid in excluded_pids:
            continue
        cli_kind = _classify_proc_cli(proc_dir)
        if cli_kind is None:
            continue
        live_proc_entries.append((pid, proc_dir, cli_kind, _read_proc_pts_mtime(proc_dir)))
    if not live_proc_entries:
        return []

    eff_wsl = _effective_is_wsl()
    files_by_pid, cwd_by_pid = _query_pids_open_files_and_cwds(
        [pid for pid, _, _, _ in live_proc_entries], proc_root
    )
    codex_root_dir = _safe_wsl_dir(home / ".codex", wsl=eff_wsl)
    agy_root_dir = _safe_wsl_dir(home / ".gemini", wsl=eff_wsl)
    claude_projects_dir = _safe_wsl_dir(home / ".claude" / "projects", wsl=eff_wsl)
    codex_names = _load_codex_thread_names(home, wsl=eff_wsl)
    by_key: dict[str, SessionMatch] = {}

    for pid, proc_dir, cli_kind, pts_mtime in live_proc_entries:
        proc_cwd = cwd_by_pid.get(pid, "")
        open_files = files_by_pid.get(pid, ())

        if cli_kind == "codex":
            if codex_root_dir is None:
                continue
            for target in open_files:
                if not target.endswith(".jsonl"):
                    continue
                base = os.path.basename(target)
                if (
                    not base.startswith("rollout-")
                    or not _is_path_under_dir(target, codex_root_dir, wsl=eff_wsl)
                ):
                    continue
                safe_t = _safe_realpath(target, wsl=eff_wsl)
                if not safe_t or not os.path.isfile(safe_t):
                    continue
                is_cli, sid, meta_cwd = _read_codex_root_meta(safe_t, wsl=eff_wsl)
                if not is_cli or not sid:
                    continue
                j_mtime = _safe_mtime(safe_t)
                cand = SessionMatch(
                    cli="codex",
                    session_id=sid,
                    jsonl_path=safe_t,
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
            if agy_root_dir is None:
                continue
            for target in open_files:
                if not target.endswith(".lock"):
                    continue
                if "/presence/" not in target.replace("\\", "/") or not _is_path_under_dir(
                    target, agy_root_dir, wsl=eff_wsl
                ):
                    continue
                safe_t = _safe_realpath(target, wsl=eff_wsl)
                cid = Path(safe_t).stem.lower() if safe_t else ""
                if not cid or not os.path.isfile(safe_t):
                    continue
                tp = _resolve_agy_transcript_path(home, cid, wsl=eff_wsl)
                if tp is not None and _is_agy_transcript_subagent(tp):
                    continue
                tp_str, j_mtime = _agy_transcript_and_mtime(home, cid, safe_t)
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
            if not proc_cwd or claude_projects_dir is None:
                continue
            pinned_sid = _extract_claude_cmdline_sid(proc_dir)
            best_pinned: SessionMatch | None = None
            best_unclaimed: SessionMatch | None = None
            for enc in encode_claude_project_dir(proc_cwd, wsl=eff_wsl):
                for jf_str in _safe_wsl_glob_files(
                    claude_projects_dir / enc, 1, "*.jsonl", wsl=eff_wsl
                ):
                    jf = Path(jf_str)
                    is_root, sid, meta_cwd = _read_claude_root_meta(
                        jf, allow_empty_or_meta_only=True, wsl=eff_wsl
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
                    if pinned_sid and sid == pinned_sid:
                        if best_pinned is None or cand.jsonl_mtime > best_pinned.jsonl_mtime:
                            best_pinned = cand
                    owner_pid = (claimed_claude_sids or {}).get(sid, 0)
                    if owner_pid in (0, pid):
                        if best_unclaimed is None or cand.jsonl_mtime > best_unclaimed.jsonl_mtime:
                            best_unclaimed = cand
            chosen = best_pinned or best_unclaimed
            if chosen is not None:
                prev = by_key.get(chosen.session_key)
                if prev is None or (chosen.pts_mtime, chosen.jsonl_mtime) > (
                    prev.pts_mtime,
                    prev.jsonl_mtime,
                ):
                    by_key[chosen.session_key] = chosen

    return list(by_key.values())


def _disambiguate_candidates(
    candidates: Sequence[SessionMatch],
    window_title: str,
    window_cwd: str = "",
    home: Path | None = None,
) -> SessionMatch | None:
    """Apply disambiguation: window_cwd -> CLI/basename -> thread_name -> PTS/JSONL mtime."""
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    pool = list(candidates)
    eff_wsl = _effective_is_wsl()
    norm_cwd = "" if is_wsl_slow_mount_path(window_cwd, wsl=eff_wsl) else _normalize_cwd(window_cwd)
    if norm_cwd:
        cwd_ranked = [(_cwd_match_rank(norm_cwd, c.cwd, home, wsl=eff_wsl), c) for c in pool]
        max_cwd_rank = max(r for r, _ in cwd_ranked)
        if max_cwd_rank > 0:
            pool = [c for r, c in cwd_ranked if r == max_cwd_rank]
            if len(pool) == 1:
                return pool[0]

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


def _clean_pos_int(val: Any) -> int:
    return int(val) if isinstance(val, int) and not isinstance(val, bool) and val > 0 else 0


def _extract_record_pid(rec: Mapping[str, Any]) -> int:
    return _clean_pos_int(rec.get("agent_pid")) or _clean_pos_int(rec.get("pid"))


def _iter_trusted_sessions_dirs(runtime_dirs: Sequence[Path]) -> list[tuple[int, Path]]:
    out: list[tuple[int, Path]] = []
    for dir_idx, rt_dir in enumerate(runtime_dirs):
        if _is_trusted_runtime_dir(rt_dir):
            sdir = rt_dir / "sessions"
            if _is_trusted_runtime_dir(sdir):
                out.append((dir_idx, sdir))
    return out


SESSION_REGISTRY_TTL_SEC: float = 7 * 86400.0
SESSION_REGISTRY_PRUNE_INTERVAL_SEC: float = 3600.0
_SESSION_REGISTRY_TMP_STALE_SEC: float = 600.0
_SESSION_REGISTRY_PRUNE_SCAN_LIMIT: int = 512
_REGISTRY_LOCK_FILENAME: str = ".registry.lock"
_REGISTRY_PRUNE_STAMP_FILENAME: str = ".prune.stamp"


def _is_same_user_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _acquire_registry_flock(sessions_dir: Path, *, exclusive: bool, blocking: bool) -> int | None:
    lock_path = sessions_dir / _REGISTRY_LOCK_FILENAME
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        fd = os.open(str(lock_path), flags, 0o600)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            os.close(fd)
            return None
        os.fchmod(fd, 0o600)
        os.set_inheritable(fd, False)
        op = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | (0 if blocking else fcntl.LOCK_NB)
        fcntl.flock(fd, op)
        return fd
    except (BlockingIOError, OSError):
        try:
            os.close(fd)
        except OSError:
            pass
        return None


def _release_registry_flock(fd: int | None) -> None:
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def _touch_prune_stamp_locked(sessions_dir: Path, ts: float) -> None:
    stamp_path = sessions_dir / _REGISTRY_PRUNE_STAMP_FILENAME
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_TRUNC
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        sfd = os.open(str(stamp_path), flags, 0o600)
    except OSError:
        return
    try:
        st = os.fstat(sfd)
        if stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid():
            os.fchmod(sfd, 0o600)
            os.utime(sfd, (ts, ts))
    except OSError:
        pass
    finally:
        try:
            os.close(sfd)
        except OSError:
            pass


def _is_prune_throttled(sessions_dir: Path, now_wall: float) -> bool:
    stamp_path = sessions_dir / _REGISTRY_PRUNE_STAMP_FILENAME
    try:
        st = os.lstat(stamp_path)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
        return False
    elapsed = now_wall - st.st_mtime
    return 0.0 <= elapsed < SESSION_REGISTRY_PRUNE_INTERVAL_SEC


def _try_claim_prune_slot(sessions_dir: Path, now_wall: float) -> bool:
    if _is_prune_throttled(sessions_dir, now_wall):
        return False
    lfd = _acquire_registry_flock(sessions_dir, exclusive=True, blocking=False)
    if lfd is None:
        return False
    try:
        locked_now = time.time()
        if _is_prune_throttled(sessions_dir, locked_now):
            return False
        _touch_prune_stamp_locked(sessions_dir, locked_now)
        return True
    finally:
        _release_registry_flock(lfd)


def _is_nonproc_cli_pid_alive_or_indeterminate(pid: int, expected_cli: str) -> bool:
    if not expected_cli or not _is_same_user_pid_alive(pid):
        return False
    pinfo = ps_proc_info(pid)
    if pinfo is None:
        # `ps` timed out or failed transiently: preserve record if the same-user PID is still alive
        return _is_same_user_pid_alive(pid)
    return _classify_interactive_cli(pinfo[0], pinfo[3]) == expected_cli


def _unlink_if_same_inode(path: str, expected_id: tuple[int, int], cutoff_mtime: float) -> bool:
    try:
        st_cur = os.lstat(path)
        if (
            not stat.S_ISREG(st_cur.st_mode)
            or (st_cur.st_dev, st_cur.st_ino) != expected_id
            or st_cur.st_mtime > cutoff_mtime
        ):
            return False
        os.unlink(path)
        return True
    except OSError:
        return False


def prune_stale_session_registry(
    runtime_dir: str | Path,
    *,
    now_ts: float | None = None,
    max_age_sec: float = SESSION_REGISTRY_TTL_SEC,
    proc_root: Path | None = Path("/proc"),
    _stamp_already_touched: bool = False,
) -> int:
    """Prune expired (>7d) dead-PID session registry JSONs and stale (>10m) ``.tmp`` files."""
    rt_path = Path(runtime_dir)
    sdir = rt_path / "sessions"
    if not _is_trusted_runtime_dir(rt_path) or not _is_trusted_runtime_dir(sdir):
        return 0

    now = float(time.time() if now_ts is None else now_ts)
    eff_max_age = max(0.0, float(max_age_sec))
    my_uid = os.getuid()
    use_proc = proc_root is not None and proc_root.is_dir()
    candidates: list[tuple[str, tuple[int, int], float]] = []
    scanned = 0

    try:
        it = os.scandir(sdir)
    except OSError:
        return 0

    with it:
        for entry in it:
            if scanned >= _SESSION_REGISTRY_PRUNE_SCAN_LIMIT:
                break
            scanned += 1
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_uid != my_uid:
                continue
            name = entry.name
            age = now - st.st_mtime
            inode_id = (st.st_dev, st.st_ino)

            if name.startswith(".") and name.endswith(".tmp"):
                if age > _SESSION_REGISTRY_TMP_STALE_SEC:
                    candidates.append((entry.path, inode_id, now - _SESSION_REGISTRY_TMP_STALE_SEC))
                continue

            if name.startswith(".") or not name.endswith(".json"):
                continue
            if age <= eff_max_age:
                continue

            rec = _read_trusted_registry_json(Path(entry.path))
            if rec is None:
                candidates.append((entry.path, inode_id, now - eff_max_age))
                continue

            agent_pid = _extract_record_pid(rec)
            cli = str(rec.get("cli") or "").strip().lower()
            alive = False
            if agent_pid > 0:
                if use_proc and proc_root is not None:
                    pdir = proc_root / str(agent_pid)
                    try:
                        pst = os.stat(pdir)
                    except OSError:
                        alive = False
                    else:
                        alive = (
                            stat.S_ISDIR(pst.st_mode)
                            and pst.st_uid == my_uid
                            and bool(cli)
                            and _classify_proc_cli(pdir) == cli
                        )
                else:
                    alive = _is_nonproc_cli_pid_alive_or_indeterminate(agent_pid, cli)
            if alive:
                continue

            candidates.append((entry.path, inode_id, now - eff_max_age))

    if not candidates and _stamp_already_touched:
        return 0

    lfd = _acquire_registry_flock(sdir, exclusive=True, blocking=False)
    if lfd is None:
        return 0
    removed = 0
    try:
        if not _stamp_already_touched:
            _touch_prune_stamp_locked(sdir, now)
        for path_str, inode_id, cutoff_mtime in candidates:
            if _unlink_if_same_inode(path_str, inode_id, cutoff_mtime):
                removed += 1
    finally:
        _release_registry_flock(lfd)

    return removed


def write_session_registry_entry(
    runtime_dir: str | Path,
    *,
    cli: str,
    session_id: str,
    jsonl_path: str,
    cwd: str = "",
    tmux_pane: str = "",
    tmux_socket: str = "",
    tty: str = "",
    agent_pid: int = 0,
    pid: int = 0,
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
    tty_clean = _CTRL_STRIP_RE.sub("", (tty or "").strip())
    eff_tty = normalize_tty_path(tty_clean) or tty_clean
    eff_pid = _clean_pos_int(agent_pid) or _clean_pos_int(pid)
    payload: dict[str, Any] = {
        "cli": cli_low,
        "session_id": sid_low,
        "cwd": _normalize_cwd(cwd, wsl=_effective_is_wsl()),
        "tty": eff_tty,
        "jsonl_path": jsonl_path,
        "transcript_path": jsonl_path,
        "updated_at": time.time(),
    }
    pane_clean = _CTRL_STRIP_RE.sub("", (tmux_pane or "").strip())
    if _TMUX_PANE_ID_RE.match(pane_clean):
        payload["tmux_pane"] = pane_clean
        sock_clean = _CTRL_STRIP_RE.sub("", (tmux_socket or "").strip())
        if sock_clean:
            payload["tmux_socket"] = sock_clean
    if eff_pid > 0:
        payload["agent_pid"] = eff_pid
        payload["pid"] = eff_pid
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

    lfd = _acquire_registry_flock(sessions_dir, exclusive=False, blocking=True)
    if lfd is None:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise OSError(f"cannot acquire session registry lock in {sessions_dir}")
    try:
        os.replace(str(tmp), str(target))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    finally:
        _release_registry_flock(lfd)

    try:
        if _try_claim_prune_slot(sessions_dir, time.time()):
            prune_stale_session_registry(rt_path, _stamp_already_touched=True)
    except Exception:
        pass
    return target


def _iter_trusted_registry_records(
    runtime_dirs: Sequence[Path],
    pattern: str = "*.json",
) -> Iterator[tuple[int, Path, dict[str, Any]]]:
    for dir_idx, sdir in _iter_trusted_sessions_dirs(runtime_dirs):
        for reg_file in sdir.glob(pattern):
            rec = _read_trusted_registry_json(reg_file)
            if rec is not None:
                yield dir_idx, reg_file, rec


def _validate_registry_record(
    rec: Mapping[str, Any],
    *,
    expected_cli: str,
    home: Path,
    proc_root: Path | None,
    matched_by: str = "registry",
    fallback_cwd: str = "",
    codex_names: Mapping[str, str] | None = None,
) -> tuple[SessionMatch, float, int] | None:
    sid = str(rec.get("session_id") or "").strip().lower()
    if not sid:
        return None
    agent_pid = _extract_record_pid(rec)
    if agent_pid > 0 and proc_root is not None:
        if proc_root.is_dir():
            if _classify_proc_cli(proc_root / str(agent_pid)) != expected_cli:
                return None
        elif not _is_pid_alive(agent_pid):
            return None

    eff_wsl = _effective_is_wsl()
    raw_path = str(rec.get("jsonl_path") or rec.get("transcript_path") or "").strip()
    if eff_wsl and raw_path:
        safe_raw = _safe_realpath(raw_path, wsl=True)
        if not safe_raw:
            return None
        raw_path = safe_raw
    reg_cwd = _normalize_cwd(str(rec.get("cwd") or ""), wsl=eff_wsl)
    try:
        updated_at = float(rec.get("updated_at") or 0.0)
    except (TypeError, ValueError):
        updated_at = 0.0

    if expected_cli == "agy":
        valid_agy, live_cwd, _ = _resolve_valid_agy_root_meta(sid, home, proc_root)
        if not valid_agy:
            return None
        tp = _resolve_agy_transcript_path(home, sid, wsl=eff_wsl)
        if tp is not None:
            raw_path = str(tp)
        elif raw_path:
            raw_path = _upgrade_agy_transcript_path(raw_path)
        else:
            raw_path = str(_default_agy_expected_transcript_path(home, sid))
        if not reg_cwd and live_cwd:
            reg_cwd = live_cwd

    elif expected_cli == "codex":
        if not raw_path:
            return None
        if os.path.isfile(raw_path) and os.path.getsize(raw_path) > 0:
            is_cli, meta_sid, meta_cwd = _read_codex_root_meta(raw_path, wsl=eff_wsl)
            if not is_cli:
                return None
            sid = sid or meta_sid
            if not reg_cwd and meta_cwd:
                reg_cwd = meta_cwd

    elif expected_cli == "claude":
        if not raw_path:
            return None
        if os.path.isfile(raw_path) and os.path.getsize(raw_path) > 0:
            is_root, meta_sid, meta_cwd = _read_claude_root_meta(
                raw_path, allow_empty_or_meta_only=True, wsl=eff_wsl
            )
            if not is_root:
                return None
            sid = sid or meta_sid
            if not reg_cwd and meta_cwd:
                reg_cwd = meta_cwd
    else:
        return None

    return (
        SessionMatch(
            cli=expected_cli,
            session_id=sid,
            jsonl_path=raw_path,
            cwd=reg_cwd or _normalize_cwd(fallback_cwd, wsl=eff_wsl),
            matched_by=matched_by,
            pts_mtime=updated_at,
            jsonl_mtime=_safe_mtime(raw_path),
            thread_name=(
                codex_names.get(sid, "") if (expected_cli == "codex" and codex_names) else ""
            ),
        ),
        updated_at,
        agent_pid,
    )


@dataclass(frozen=True)
class _RegistryHit:
    match: SessionMatch
    agent_pid: int = 0
    tty: str = ""
    tmux_pane: str = ""
    tmux_socket: str = ""


def _lookup_in_registry_detailed(
    hint: TitleHint,
    runtime_dirs: Sequence[Path],
    home: Path,
    proc_root: Path | None,
    *,
    matched_by: str = "registry",
) -> tuple[_RegistryHit | None, bool]:
    """Return ``(best_registry_hit, saw_stale_exited)``."""
    prefix8 = hint.session_token[:8].lower()
    clis = (hint.cli,) if (hint.explicit_cli and hint.cli) else _ordered_clis(hint.cli)
    hits: list[tuple[int, int, float, _RegistryHit]] = []
    saw_stale_exited = False
    has_proc = proc_root is not None and proc_root.is_dir()

    for dir_idx, sdir in _iter_trusted_sessions_dirs(runtime_dirs):
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
            rec_pid = _extract_record_pid(rec)
            if rec_pid > 0 and proc_root is not None:
                if has_proc:
                    if _classify_proc_cli(proc_root / str(rec_pid)) != cli:
                        saw_stale_exited = True
                        continue
                elif not _is_pid_alive(rec_pid):
                    saw_stale_exited = True
                    continue
            validated = _validate_registry_record(
                rec,
                expected_cli=cli,
                home=home,
                proc_root=proc_root,
                matched_by=matched_by,
            )
            if validated is None:
                continue
            cand, updated_at, agent_pid = validated
            rec_tty = normalize_tty_path(str(rec.get("tty") or ""))
            rec_pane = str(rec.get("tmux_pane") or "").strip()
            rec_sock = str(rec.get("tmux_socket") or "").strip()
            hits.append(
                (
                    -dir_idx,
                    -cli_rank,
                    updated_at,
                    _RegistryHit(
                        match=cand,
                        agent_pid=agent_pid,
                        tty=rec_tty,
                        tmux_pane=rec_pane if _TMUX_PANE_ID_RE.match(rec_pane) else "",
                        tmux_socket=rec_sock,
                    ),
                )
            )

    if not hits:
        return None, saw_stale_exited
    hits.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return hits[0][3], False


def _collect_stale_registry_ttys(
    hint: TitleHint,
    runtime_dirs: Sequence[Path],
) -> set[str]:
    """Collect exact TTY identifiers recorded on dead local registry entries matching ``hint``."""
    prefix8 = hint.session_token[:8].lower()
    clis = (hint.cli,) if (hint.explicit_cli and hint.cli) else _ordered_clis(hint.cli)
    ttys: set[str] = set()
    for _dir_idx, sdir in _iter_trusted_sessions_dirs(runtime_dirs):
        for cli in clis:
            if not cli:
                continue
            rec = _read_trusted_registry_json(sdir / f"{cli}-{prefix8}.json")
            if rec is None:
                continue
            sid = str(rec.get("session_id") or "").strip().lower()
            if not sid or not sid.startswith(hint.session_token):
                continue
            raw_tty = str(rec.get("tty") or "").strip()
            if raw_tty and raw_tty not in ("?", "??", "-", "/dev/tty", "/dev/null"):
                ttys.add(raw_tty)
                norm_t = normalize_tty_path(raw_tty)
                if norm_t:
                    ttys.add(norm_t)
    return ttys


def _collect_live_claimed_claude_sids(
    runtime_dirs: Sequence[Path],
    proc_root: Path | None,
) -> dict[str, int]:
    """Return ``{session_id: agent_pid}`` for Claude sessions actively owned by a live PID."""
    claimed: dict[str, int] = {}
    if proc_root is None:
        return claimed
    has_proc = proc_root.is_dir()
    for _, _, rec in _iter_trusted_registry_records(runtime_dirs, "claude-*.json"):
        sid = str(rec.get("session_id") or "").strip().lower()
        agent_pid = _extract_record_pid(rec)
        if sid and agent_pid > 0 and sid not in claimed:
            is_live = (
                (_classify_proc_cli(proc_root / str(agent_pid)) == "claude")
                if has_proc
                else _is_pid_alive(agent_pid)
            )
            if is_live:
                claimed[sid] = agent_pid
    return claimed


def _collect_registry_tmux_sockets(runtime_dirs: Sequence[Path]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for _, _, rec in _iter_trusted_registry_records(runtime_dirs):
        raw_sock = str(rec.get("tmux_socket") or "").strip()
        if not raw_sock or not _is_live_owned_socket(raw_sock):
            continue
        real_sock = os.path.realpath(raw_sock)
        if real_sock not in seen:
            seen.add(real_sock)
            found.append(preferred_tmux_socket_arg(raw_sock))
            if len(found) >= _MAX_TMUX_SOCKET_TARGETS:
                break
    return found


def _collect_tmux_bound_session_keys(runtime_dirs: Sequence[Path]) -> set[str]:
    bound: set[str] = set()
    for _, _, rec in _iter_trusted_registry_records(runtime_dirs):
        if not str(rec.get("tmux_pane") or "").strip():
            continue
        cli = str(rec.get("cli") or "").strip().lower()
        sid = str(rec.get("session_id") or "").strip().lower()
        if cli in ("codex", "claude", "agy") and sid:
            bound.add(f"{cli}:{sid}")
    return bound


def _lookup_by_full_uuid(
    hint: TitleHint,
    home: Path,
    proc_root: Path | None,
) -> SessionMatch | None:
    uid = hint.session_token.lower()
    clis = (hint.cli,) if (hint.explicit_cli and hint.cli) else _ordered_clis(hint.cli)

    for cli in clis:
        if cli == "codex":
            hits = _collect_codex_storage_matches(
                home,
                matched_by="title_uuid",
                file_glob=f"rollout-*-{uid}.jsonl",
                fallback_sid=uid,
                codex_names={},
            )
            if hits:
                return hits[0]
        elif cli == "agy":
            agy_hit = _build_agy_match(
                uid, home=home, proc_root=proc_root, matched_by="title_uuid"
            )
            if agy_hit is not None:
                return agy_hit
        elif cli == "claude":
            hits = _collect_claude_storage_matches(
                home,
                matched_by="title_uuid",
                file_glob=f"{uid}.jsonl",
                fallback_sid=uid,
                allow_empty_global=True,
            )
            if hits:
                return hits[0]
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
        c for c in proc_candidates if c.session_id.lower().startswith(prefix) and c.cli in clis
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
        return replace(proc_hits[0], matched_by="title_prefix")

    # 2. Prefix match in session storage directories (strictly active/root sessions only)
    dir_hits: list[SessionMatch] = []
    for cli in clis:
        if cli == "codex":
            dir_hits.extend(
                _collect_codex_storage_matches(
                    home,
                    matched_by="title_prefix",
                    file_glob=f"rollout-*-{prefix}*.jsonl",
                    sid_prefix=prefix,
                    codex_names={},
                )
            )
        elif cli == "agy":
            eff_wsl = _effective_is_wsl()
            for lf_str in _safe_wsl_glob_files(
                home / ".gemini" / "antigravity-cli" / "presence",
                1,
                f"{prefix}*.lock",
                wsl=eff_wsl,
            ):
                cid = Path(lf_str).stem.lower()
                if cid.startswith(prefix):
                    agy_hit = _build_agy_match(
                        cid, home=home, proc_root=proc_root, matched_by="title_prefix"
                    )
                    if agy_hit is not None:
                        dir_hits.append(agy_hit)
        elif cli == "claude":
            dir_hits.extend(
                _collect_claude_storage_matches(
                    home,
                    matched_by="title_prefix",
                    file_glob=f"{prefix}*.jsonl",
                    sid_prefix=prefix,
                    allow_empty_global=True,
                )
            )

    if not dir_hits:
        return None
    dir_hits.sort(key=lambda c: (1 if c.cli == hint.cli else 0, c.jsonl_mtime), reverse=True)
    return dir_hits[0]


def _list_tty_processes(
    norm_tty: str,
    proc_root: Path | None = None,
    window_pid: int = 0,
) -> tuple[set[int], list[tuple[int, str]], bool]:
    """Return ``(live_pids, interactive_agents, has_mux)`` on ``norm_tty`` via bounded ``ps``."""
    live_pids: set[int] = set()
    agent_rows: list[tuple[int, int, str]] = []
    has_mux = False
    tty_arg = norm_tty[5:] if norm_tty.startswith("/dev/") else norm_tty
    try:
        r = subprocess.run(
            ["ps", "-ww", "-o", "pid=,ppid=,stat=,tty=,args=", "-t", tty_arg],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=1.0,
            check=False,
        )
        for pid, ppid, _stat, row_tty, argv in _parse_ps_table(
            r.stdout.decode("utf-8", errors="ignore")
        ):
            if row_tty and row_tty != norm_tty:
                continue
            live_pids.add(pid)
            a0 = os.path.basename(argv[0].replace("\\", "/")).lstrip("-").lower() if argv else ""
            if a0 in ("tmux", "screen", "zellij"):
                has_mux = True
            cli_k = _classify_interactive_cli(a0, argv)
            if cli_k is not None:
                agent_rows.append((pid, ppid, cli_k))
    except (OSError, subprocess.SubprocessError):
        pass

    if window_pid > 0 and window_pid not in live_pids:
        pinfo = ps_proc_info(window_pid)
        if pinfo is not None:
            comm, ppid, ptty, argv, _is_sl = pinfo
            is_mux = comm.lower() in ("tmux", "screen", "zellij")
            cli_k = _classify_interactive_cli(comm, argv)
            if ptty == norm_tty or (not ptty and (cli_k is not None or is_mux)):
                live_pids.add(window_pid)
                if is_mux:
                    has_mux = True
                if cli_k is not None:
                    agent_rows.append((window_pid, ppid, cli_k))

    if not live_pids and proc_root is not None and proc_root.is_dir():
        for pdir in _iter_numeric_proc_dirs(proc_root):
            if _readlink_clean(pdir / "fd" / "0") != norm_tty:
                continue
            pid = int(pdir.name)
            live_pids.add(pid)
            if _read_proc_comm(pdir).lower() in ("tmux", "screen", "zellij"):
                has_mux = True
            cli_k = _classify_proc_cli(pdir)
            if cli_k is not None:
                agent_rows.append((pid, 0, cli_k))

    parent_pids = {ppid for _pid, ppid, _cli in agent_rows if ppid > 0}
    agent_rows.sort(key=lambda item: (0 if item[0] in parent_pids else 1, item[0]), reverse=True)
    return live_pids, [(pid, cli_k) for pid, _ppid, cli_k in agent_rows], has_mux


def _lookup_tty_in_registry(
    norm_tty: str,
    live_pids: set[int],
    active_cli_set: set[str],
    runtime_dirs: Sequence[Path],
    home: Path,
    proc_root: Path | None,
    fallback_cwd: str = "",
) -> SessionMatch | None:
    codex_names = _load_codex_thread_names(home, wsl=_effective_is_wsl())
    hits: list[tuple[int, int, float, SessionMatch]] = []
    for dir_idx, _, rec in _iter_trusted_registry_records(runtime_dirs):
        reg_tty = normalize_tty_path(str(rec.get("tty") or ""))
        reg_pid = _extract_record_pid(rec)
        cli = str(rec.get("cli") or "").strip().lower()
        if cli not in ("codex", "claude", "agy"):
            continue

        if reg_pid > 0 and reg_pid in live_pids:
            pid_rank = 2
        elif reg_tty == norm_tty:
            if reg_pid > 0 and not _is_pid_alive(reg_pid):
                continue
            if live_pids and cli not in active_cli_set:
                continue
            pid_rank = 1
        else:
            continue

        validated = _validate_registry_record(
            rec,
            expected_cli=cli,
            home=home,
            proc_root=proc_root,
            matched_by="tty_registry",
            fallback_cwd=fallback_cwd,
            codex_names=codex_names,
        )
        if validated is not None:
            cand, updated_at, _ = validated
            hits.append((pid_rank, -dir_idx, updated_at, cand))
    if not hits:
        return None
    hits.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return hits[0][3]


def _find_storage_session_for_cli(
    cli_kind: str,
    *,
    home: Path,
    proc_root: Path | None,
    eff_cwd: str,
    window_title: str,
    agent_pid: int = 0,
    matched_by: str = "tty_ps",
    claimed_claude_sids: Mapping[str, int] | None = None,
) -> SessionMatch | None:
    eff_wsl = _effective_is_wsl()
    if cli_kind == "codex":
        if agent_pid > 0:
            open_real = _query_pid_codex_rollout_paths(agent_pid, home, proc_root)
            if open_real:
                codex_names = _load_codex_thread_names(home, wsl=eff_wsl)
                pid_cands: list[SessionMatch] = []
                for rp in open_real:
                    is_cli, sid, meta_cwd = _read_codex_root_meta(rp, wsl=eff_wsl)
                    if is_cli and sid:
                        pid_cands.append(
                            SessionMatch(
                                cli="codex",
                                session_id=sid,
                                jsonl_path=rp,
                                cwd=meta_cwd or eff_cwd,
                                matched_by=matched_by,
                                jsonl_mtime=_safe_mtime(rp),
                                thread_name=codex_names.get(sid, ""),
                            )
                        )
                if pid_cands:
                    return _disambiguate_candidates(pid_cands, window_title, eff_cwd, home)
        cands = _collect_codex_storage_matches(
            home, matched_by=matched_by, eff_cwd=eff_cwd, limit=48
        )
        return _disambiguate_candidates(cands, window_title, eff_cwd, home)

    if cli_kind == "claude":
        cands = _collect_claude_storage_matches(
            home,
            matched_by=matched_by,
            eff_cwd=eff_cwd,
            filter_cwd=True,
            allow_empty_global=not bool(eff_cwd),
            limit=48,
            claimed_claude_sids=claimed_claude_sids,
            owner_pid=agent_pid,
        )
        return _disambiguate_candidates(cands, window_title, eff_cwd, home)

    if cli_kind == "agy":
        presence_dir = _safe_wsl_dir(
            home / ".gemini" / "antigravity-cli" / "presence", wsl=eff_wsl
        )
        if presence_dir is None:
            return None
        if agent_pid > 0:
            pid_cands = [
                cand
                for cid in _query_pid_agy_lock_cids(agent_pid, proc_root)
                if (
                    cand := _build_agy_match(
                        cid,
                        home=home,
                        proc_root=proc_root,
                        matched_by=matched_by,
                        fallback_cwd=eff_cwd,
                    )
                )
                is not None
            ]
            if pid_cands:
                return _disambiguate_candidates(pid_cands, window_title, eff_cwd, home)
        matched_cands: list[SessionMatch] = []
        unknown_cwd_cands: list[SessionMatch] = []
        for lf_str in _safe_wsl_glob_files(presence_dir, 1, "*.lock", wsl=eff_wsl):
            cand = _build_agy_match(
                Path(lf_str).stem, home=home, proc_root=proc_root, matched_by=matched_by
            )
            if cand is None:
                continue
            if not eff_cwd:
                matched_cands.append(cand)
            elif cand.cwd:
                if _cwd_match_rank(eff_cwd, cand.cwd, home, wsl=eff_wsl) > 0:
                    matched_cands.append(cand)
            else:
                unknown_cwd_cands.append(replace(cand, cwd=eff_cwd))
        pool = (
            matched_cands
            if matched_cands
            else (unknown_cwd_cands if len(unknown_cwd_cands) == 1 else [])
        )
        return _disambiguate_candidates(pool, window_title, eff_cwd, home)

    return None


def _resolve_by_window_tty(
    norm_tty: str,
    *,
    norm_cwd: str,
    window_pid: int = 0,
    window_title: str,
    runtime_dirs: Sequence[Path],
    home: Path,
    proc_root: Path | None,
    claimed_claude_sids: Mapping[str, int] | None = None,
) -> tuple[SessionMatch | None, bool]:
    """Resolve active session bound to ``norm_tty``; return ``(match, stop_fallback)``."""
    live_pids, interactive_agents, has_mux = _list_tty_processes(
        norm_tty, proc_root, window_pid=window_pid
    )
    active_cli_set = {cli_k for _pid, cli_k in interactive_agents}

    reg_hit = _lookup_tty_in_registry(
        norm_tty,
        live_pids,
        active_cli_set,
        runtime_dirs,
        home,
        proc_root,
        fallback_cwd=norm_cwd,
    )
    if reg_hit is not None:
        return reg_hit, True

    for agent_pid, cli_kind in interactive_agents:
        eff_cwd = norm_cwd or (
            _read_proc_cwd(proc_root / str(agent_pid))
            if (proc_root is not None and proc_root.is_dir())
            else _query_pid_cwd_nonproc(agent_pid)
        )
        hit = _find_storage_session_for_cli(
            cli_kind,
            home=home,
            proc_root=proc_root,
            eff_cwd=eff_cwd,
            window_title=window_title,
            agent_pid=agent_pid,
            matched_by="tty_ps",
            claimed_claude_sids=claimed_claude_sids,
        )
        if hit is not None:
            return hit, True

    if interactive_agents or (live_pids and not has_mux):
        return None, True
    return None, False


def _scan_nonproc_fallback_sessions(
    home: Path,
    runtime_dirs: Sequence[Path],
    window_cwd: str = "",
    *,
    check_pid_alive: bool = True,
) -> list[SessionMatch]:
    """Fallback for macOS (no ``/proc``): active runtime registry + recent root transcripts."""
    codex_names = _load_codex_thread_names(home, wsl=_effective_is_wsl())
    by_key: dict[str, SessionMatch] = {}
    key_tier: dict[str, int] = {}
    eff_proc_for_reg: Path | None = Path("/__nonproc__") if check_pid_alive else None

    for dir_idx, _, rec in _iter_trusted_registry_records(runtime_dirs):
        cli = str(rec.get("cli") or "").strip().lower()
        if cli not in ("codex", "claude", "agy"):
            continue
        validated = _validate_registry_record(
            rec,
            expected_cli=cli,
            home=home,
            proc_root=eff_proc_for_reg,
            matched_by="recent_root",
            codex_names=codex_names,
        )
        if validated is None:
            continue
        cand, updated_at, _ = validated
        prev = by_key.get(cand.session_key)
        prev_tier = key_tier.get(cand.session_key, 999999)
        if prev is None or (-dir_idx, updated_at) >= (-prev_tier, prev.pts_mtime):
            by_key[cand.session_key] = cand
            key_tier[cand.session_key] = dir_idx

    for cand in _collect_codex_storage_matches(
        home, matched_by="recent_root", limit=24, codex_names=codex_names
    ):
        by_key.setdefault(cand.session_key, cand)

    for cand in _collect_claude_storage_matches(
        home,
        matched_by="recent_root",
        eff_cwd=window_cwd,
        allow_empty_global=not bool(window_cwd),
        limit=24,
    ):
        by_key.setdefault(cand.session_key, cand)

    eff_wsl = _effective_is_wsl()
    for lf_str in _safe_wsl_glob_files(
        home / ".gemini" / "antigravity-cli" / "presence", 1, "*.lock", wsl=eff_wsl
    ):
        agy_cand = _build_agy_match(
            Path(lf_str).stem, home=home, proc_root=None, matched_by="recent_root"
        )
        if agy_cand is not None:
            by_key.setdefault(agy_cand.session_key, agy_cand)

    return list(by_key.values())


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


def chunk_turns_for_refiner(
    turns: Sequence[tuple[str, str]],
    *,
    max_chars: int = DEFAULT_REFINER_CONTEXT_MAX_CHARS,
    chunk_chars: int | None = None,
) -> list[str]:
    """Pack turns into deterministic fixed-boundary text chunks with ~1/4-window eviction.

    Chunks are anchored from ``turns[0]`` forward (~1/4 of ``max_chars`` by default, e.g. 50K
    for 200K) and evicted in whole-chunk steps once ``max_chars`` is exceeded, keeping the
    remaining ~3/4 prefix byte-identical across subsequent turns.
    Uses third-party transcript labels (``[Speaker]`` / ``[CLI Agent]``) rather than
    ``[Assistant]`` so Stage 2 refiners do not mistake downstream agent turns for their own.
    """
    if not turns or max_chars <= 0:
        return []
    default_chunk = (
        DEFAULT_REFINER_CHUNK_CHARS
        if max_chars == DEFAULT_REFINER_CONTEXT_MAX_CHARS
        else max_chars // 4
    )
    target_chunk = max(256, default_chunk) if chunk_chars is None else int(chunk_chars)
    eff_chunk = max(256, min(target_chunk, max(256, max_chars // 2)))
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for role, text in turns:
        tag = "Speaker" if role == "user" else "CLI Agent"
        prefix = f"[{tag}]\n"
        entry = f"{prefix}{text}"
        if len(entry) > max_chars:
            rem = max(0, max_chars - len(prefix))
            entry = f"{prefix}{text[-rem:]}" if rem > 0 else prefix.rstrip()
        sep = 2 if cur else 0
        if cur and cur_len + sep + len(entry) > eff_chunk:
            chunks.append("\n\n".join(cur))
            cur = [entry]
            cur_len = len(entry)
        else:
            cur.append(entry)
            cur_len += sep + len(entry)
    if cur:
        chunks.append("\n\n".join(cur))

    kept: list[str] = []
    total = 0
    for ch in reversed(chunks):
        sep = 2 if kept else 0
        if kept and total + sep + len(ch) > max_chars:
            break
        if not kept and len(ch) > max_chars:
            kept.append(ch[-max_chars:])
            break
        kept.append(ch)
        total += sep + len(ch)
    kept.reverse()
    return kept


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


def _tty_matches(a: str, b: str) -> bool:
    if not a or not b:
        return False
    na = normalize_tty_path(a) or a.strip()
    nb = normalize_tty_path(b) or b.strip()
    return na == nb or _norm_same_path(na, nb)


def collect_darwin_window_pty_tree(
    rows: Sequence[tuple[int, int, str, str, list[str]]],
    root_pid: int,
) -> tuple[set[str], set[int]]:
    """Collect ``(tab_ttys, descendant_pids)`` beneath a Darwin GUI terminal ``root_pid``.

    ``tab_ttys`` stops descending along a branch once it enters its first
    controlling TTY so nested tool sub-PTYs (e.g. ``agy`` / ``codex`` PTY
    runners) are not mistaken for separate terminal tabs, while
    ``descendant_pids`` captures the full process subtree under ``root_pid``.
    """
    if root_pid <= 1 or not rows:
        return set(), set()
    tty_by_pid: dict[int, str] = {}
    children: dict[int, list[int]] = {}
    for pid, ppid, _stat, row_tty, _argv in rows:
        tty_by_pid[pid] = row_tty
        if ppid > 0 and pid != ppid:
            children.setdefault(ppid, []).append(pid)

    descendant_pids: set[int] = set()
    tab_ttys: set[str] = set()
    root_tty = tty_by_pid.get(root_pid, "")
    if root_tty:
        tab_ttys.add(root_tty)

    queue: list[tuple[int, bool]] = [(root_pid, bool(root_tty))]
    seen: set[int] = {root_pid}
    while queue:
        cur, has_tab_tty = queue.pop(0)
        for ch in children.get(cur, ()):
            if ch in seen:
                continue
            seen.add(ch)
            descendant_pids.add(ch)
            ch_tty = tty_by_pid.get(ch, "")
            next_has_tty = has_tab_tty or bool(ch_tty)
            if not has_tab_tty and ch_tty:
                tab_ttys.add(ch_tty)
            queue.append((ch, next_has_tty))
    return tab_ttys, descendant_pids


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
        repo_files_cache_ttl_sec: float = REPO_FILES_CACHE_TTL_SEC,
        tmux_socket: str | Path | None = None,
        tmux_socket_name: str | None = None,
        tmux_probe_fn: Callable[[], TmuxServerSnapshot] | None = None,
        remote_cfg: RemoteConfig | None = None,
        local_hostnames: Iterable[str] | None = None,
        remote_manager: RemoteSshManager | None = None,
        context_max_chars: int = DEFAULT_REFINER_CONTEXT_MAX_CHARS,
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
        self.repo_files_cache_ttl_sec = repo_files_cache_ttl_sec
        self.context_max_chars = max(1000, int(context_max_chars))
        self._tmux_socket = str(tmux_socket).strip() if tmux_socket is not None else ""
        self._tmux_socket_name = (tmux_socket_name or "").strip()
        self._tmux_probe_fn = tmux_probe_fn
        self._lock = threading.RLock()
        self._states: dict[str, SessionState] = {}
        self._git_cache: dict[str, tuple[float, str, list[str]]] = {}
        self._repo_cache: dict[str, tuple[float, list[str]]] = {}
        self._shallow_cache: dict[str, tuple[float, list[str]]] = {}
        self._remote_files_cache: dict[tuple[str, str], tuple[float, list[str], list[str]]] = {}
        self._custom_local_hostnames: tuple[str, ...] | None = (
            tuple(local_hostnames) if local_hostnames is not None else None
        )
        self.remote_cfg: RemoteConfig = remote_cfg if remote_cfg is not None else RemoteConfig()
        self.remote_manager: RemoteSshManager = (
            remote_manager
            if remote_manager is not None
            else RemoteSshManager(self.remote_cfg, env=self.env, runtime_dir=self.runtime_dir)
        )
        self._rebuild_local_hostnames()

    def _rebuild_local_hostnames(self) -> None:
        remote_names: list[str] = []
        for h in self.remote_cfg.hosts:
            remote_names.append(h.name)
            remote_names.extend(h.aliases)
            t_host = h.ssh_target.rsplit("@", 1)[-1].lower()
            if t_host:
                remote_names.append(t_host)
        is_synthetic = self.proc_root != Path("/proc")
        self._local_hostnames: frozenset[str] = collect_local_hostnames(
            self.env,
            extra=self._custom_local_hostnames,
            include_synthetic_defaults=(is_synthetic and self._custom_local_hostnames is None),
            include_mac_placeholder=(self.proc_root is None and self._custom_local_hostnames is None),
            configured_remote_names=remote_names,
        )

    def configure_remote(
        self,
        remote_cfg: RemoteConfig,
        *,
        runtime_dir: Path | None = None,
        local_hostnames: Iterable[str] | None = None,
        context_max_chars: int | None = None,
    ) -> None:
        with self._lock:
            self.remote_cfg = remote_cfg
            if runtime_dir is not None:
                self.runtime_dir = Path(runtime_dir)
            if local_hostnames is not None:
                self._custom_local_hostnames = tuple(local_hostnames)
            if context_max_chars is not None:
                self.context_max_chars = max(1000, int(context_max_chars))
            self._rebuild_local_hostnames()
            self.remote_manager.update_config(remote_cfg, runtime_dir=self.runtime_dir)

    def close(self) -> None:
        self.remote_manager.close()

    def _has_explicit_tmux_override(self) -> bool:
        return bool(
            self._tmux_probe_fn is not None
            or self._tmux_socket
            or self._tmux_socket_name
        )

    def _probe_tmux(self, rt_dirs: Sequence[Path] = ()) -> TmuxServerSnapshot:
        if self._tmux_probe_fn is not None:
            try:
                return self._tmux_probe_fn()
            except Exception:
                return TmuxServerSnapshot()
        # Synthetic fake-/proc or proc_root=None tests without an explicit tmux override must never touch host tmux
        if self.proc_root != Path("/proc") and not self._has_explicit_tmux_override():
            return TmuxServerSnapshot()
        extra_socks = _collect_registry_tmux_sockets(rt_dirs) if rt_dirs else ()
        return probe_tmux_snapshot(
            env=self.env,
            tmux_socket=self._tmux_socket or None,
            tmux_socket_name=self._tmux_socket_name or None,
            extra_sockets=extra_socks,
        )

    @staticmethod
    def _select_active_tmux_target(
        snap: TmuxServerSnapshot,
        window_title: str = "",
        *,
        window_tty: str = "",
        window_pid: int = 0,
        allowed_client_ttys: Collection[str] | None = None,
        allowed_client_pids: Collection[int] | None = None,
    ) -> tuple[TmuxClientInfo, TmuxPaneInfo] | None:
        if not snap.clients or not snap.panes:
            return None
        resolved_pairs: list[tuple[TmuxClientInfo, TmuxPaneInfo]] = []
        for client in snap.clients:
            chosen_pane: TmuxPaneInfo | None = None
            if client.pane_id:
                for p in snap.panes:
                    if (
                        p.pane_id == client.pane_id
                        and _same_tmux_server(client.socket_path, p.socket_path)
                        and (not client.session_id or p.session_id == client.session_id)
                        and (not client.window_id or p.window_id == client.window_id)
                    ):
                        chosen_pane = p
                        break
            if chosen_pane is None:
                for p in snap.panes:
                    if (
                        p.session_id == client.session_id
                        and (
                            p.window_id == client.window_id
                            if client.window_id
                            else p.window_active
                        )
                        and p.pane_active
                        and _same_tmux_server(client.socket_path, p.socket_path)
                    ):
                        chosen_pane = p
                        break
            if chosen_pane is not None:
                resolved_pairs.append((client, chosen_pane))

        if allowed_client_ttys is not None or allowed_client_pids is not None:
            pid_set = set(allowed_client_pids) if allowed_client_pids else set()
            tty_list = [t for t in (allowed_client_ttys or ()) if t]
            resolved_pairs = [
                pair
                for pair in resolved_pairs
                if (pair[0].client_pid > 1 and pair[0].client_pid in pid_set)
                or (
                    pair[0].client_tty
                    and any(_tty_matches(pair[0].client_tty, t) for t in tty_list)
                )
            ]

        if not resolved_pairs:
            return None
        if len(resolved_pairs) == 1:
            return resolved_pairs[0]

        if window_tty:
            tty_hits = [
                pair
                for pair in resolved_pairs
                if pair[0].client_tty and _tty_matches(pair[0].client_tty, window_tty)
            ]
            if len(tty_hits) == 1:
                return tty_hits[0]
            if tty_hits:
                resolved_pairs = tty_hits

        if window_pid > 1:
            pid_hits = [pair for pair in resolved_pairs if pair[0].client_pid == window_pid]
            if len(pid_hits) == 1:
                return pid_hits[0]
            if pid_hits:
                resolved_pairs = pid_hits

        wtitle = (window_title or "").strip()
        title_low = wtitle.lower()
        if title_low:
            w_hint = parse_window_title_hint(wtitle)
            if w_hint is not None:
                token_hits = [
                    pair
                    for pair in resolved_pairs
                    if (ph := parse_window_title_hint(pair[1].pane_title)) is not None
                    and ph.session_token == w_hint.session_token
                ]
                if len(token_hits) == 1:
                    return token_hits[0]
                if token_hits:
                    resolved_pairs = token_hits

            pt_hits = [
                pair
                for pair in resolved_pairs
                if _is_meaningful_pane_title((pair[1].pane_title or "").strip().lower())
                and (pair[1].pane_title or "").strip().lower() in title_low
            ]
            if len(pt_hits) == 1:
                return pt_hits[0]
            if pt_hits:
                resolved_pairs = pt_hits

            cwd_hits = [
                pair
                for pair in resolved_pairs
                if _dir_basename_in_title(wtitle, pair[1].current_path)
            ]
            if len(cwd_hits) == 1:
                return cwd_hits[0]
            if cwd_hits:
                resolved_pairs = cwd_hits

        resolved_pairs.sort(
            key=lambda pair: (pair[0].activity, 1 if pair[0].focused else 0),
            reverse=True,
        )
        return resolved_pairs[0]

    @staticmethod
    def _outer_hint_belongs_to_tmux(
        window_title: str,
        hint: TitleHint,
        active_pane: TmuxPaneInfo,
    ) -> bool:
        del window_title
        ptitle = (active_pane.pane_title or "").strip()
        if not ptitle:
            return False
        pane_hint = parse_window_title_hint(ptitle)
        return pane_hint is not None and pane_hint.session_token == hint.session_token

    def _should_prefer_non_tmux(
        self,
        window_title: str,
        active_client: TmuxClientInfo,
        active_pane: TmuxPaneInfo,
        non_tmux_cands: Sequence[SessionMatch],
    ) -> bool:
        """Distinguish foreground non-tmux terminal tabs from an attached ``tmux`` tab."""
        if not non_tmux_cands:
            return False

        wtitle = (window_title or "").strip()
        title_low = wtitle.lower()
        ptitle = (active_pane.pane_title or "").strip()
        ptitle_low = ptitle.lower()

        if wtitle:
            # 1. Quoted tmux set-titles '"<pane_title>"' -> foreground tab IS tmux
            if len(ptitle_low) >= 4 and f'"{ptitle_low}"' in title_low:
                return False

            has_ptitle_in_outer = _is_meaningful_pane_title(ptitle_low) and (
                ptitle_low in title_low
            )

            non_tmux_cwd = any(
                c.cwd and _dir_basename_in_title(wtitle, c.cwd) for c in non_tmux_cands
            )
            tmux_paths: list[str] = []
            if active_pane.current_path:
                tmux_paths.append(active_pane.current_path)
            elif (
                active_client.client_pid > 1
                and self.proc_root is not None
                and self.proc_root.is_dir()
            ):
                tmux_paths.append(
                    _read_proc_cwd(self.proc_root / str(active_client.client_pid))
                )
            tmux_cwd = any(p and _dir_basename_in_title(wtitle, p) for p in tmux_paths)

            cli_word = _infer_cli_word_from_title(wtitle)
            pane_cli_word = _infer_cli_word_from_title(ptitle) if has_ptitle_in_outer else None
            non_tmux_exclusive_cli = bool(
                cli_word
                and any(c.cli == cli_word for c in non_tmux_cands)
                and cli_word != pane_cli_word
            )
            non_tmux_exclusive_topic = any(
                c.thread_name
                and len(c.thread_name) >= 2
                and c.thread_name.lower() in title_low
                and (not has_ptitle_in_outer or c.thread_name.lower() not in ptitle_low)
                for c in non_tmux_cands
            )

            # 2. Unquoted tmux set-titles ('#T' / 'tmux: #T'): active pane_title forwarded to outer window
            if has_ptitle_in_outer:
                if (
                    (non_tmux_cwd and not tmux_cwd)
                    or non_tmux_exclusive_cli
                    or non_tmux_exclusive_topic
                ):
                    return True
                return False

            has_spinner = bool(_TUI_SPINNER_RE.search(wtitle))

            # 3. Explicit word 'tmux' (unless outer title is a non-tmux spinner or thread_name topic)
            if (
                _WORD_TMUX_RE.search(wtitle)
                and not has_spinner
                and not non_tmux_exclusive_topic
            ):
                return False

            # 4. Explicit CLI name, TUI spinner ('✳' / Braille), or Codex thread_name on outer window
            if non_tmux_exclusive_cli:
                return True
            if has_spinner:
                return True
            if non_tmux_exclusive_topic:
                return True

            # 5. Directory basename in outer shell title (e.g. 'user@host: ~/repos/proj')
            if non_tmux_cwd and not tmux_cwd:
                return True
            if tmux_cwd and not non_tmux_cwd:
                return False

        # 5. Generic/empty title tie-breaker: compare non-tmux TTY/JSONL activity vs tmux client_activity
        # (tmux client_activity has 1-second integer precision and ignores background pane stdout)
        if active_client.activity <= 0:
            return False
        best_non_tmux_ts = max(max(c.pts_mtime, c.jsonl_mtime) for c in non_tmux_cands)
        return best_non_tmux_ts >= (active_client.activity + 1.0)

    def _resolve_tmux_active_pane_detailed(
        self,
        active_pane: TmuxPaneInfo,
        rt_dirs: Sequence[Path],
        *,
        window_title: str = "",
        claimed_claude: Mapping[str, int] | None = None,
    ) -> tuple[SessionMatch | None, bool]:
        """Two-stage resolution scoped strictly to ``active_pane``; returns ``(match, authoritative_empty)``."""
        has_proc = self.proc_root is not None and self.proc_root.is_dir()
        use_ps = self.proc_root is not None and not has_proc
        pane_pids: set[int] = set()
        live_pane_clis: dict[str, set[int]] = {}
        pane_proc_cands: list[SessionMatch] = []
        darwin_live_pids: set[int] = set()
        darwin_agents: list[tuple[int, str]] = []

        eff_claimed = (
            claimed_claude
            if claimed_claude is not None
            else _collect_live_claimed_claude_sids(rt_dirs, self.proc_root)
        )

        if has_proc and self.proc_root is not None:
            pane_pids = _collect_pane_pids(
                self.proc_root, active_pane.pane_pid, active_pane.pane_tty
            )
            live_pane_clis = _collect_live_cli_pids(self.proc_root, pane_pids)
            pane_proc_cands = scan_proc_root_sessions(
                self.home,
                self.proc_root,
                allowed_pids=pane_pids,
                claimed_claude_sids=eff_claimed,
            )
        elif use_ps:
            norm_pane_tty = normalize_tty_path(active_pane.pane_tty)
            if norm_pane_tty:
                darwin_live_pids, darwin_agents, _ = _list_tty_processes(
                    norm_pane_tty, None, window_pid=active_pane.pane_pid
                )

        darwin_active_clis = {cli_k for _pid, cli_k in darwin_agents}

        def _is_live_in_pane(cand: SessionMatch, rec_agent_pid: int = 0) -> bool:
            if not has_proc:
                if not use_ps:
                    return True
                if rec_agent_pid > 0 and not _is_pid_alive(rec_agent_pid):
                    return False
                if darwin_live_pids and cand.cli not in darwin_active_clis:
                    if not (rec_agent_pid > 0 and _ps_pid_matches_cli(rec_agent_pid, cand.cli)):
                        return False
                if rec_agent_pid <= 0 and cand.cli == "codex" and cand.jsonl_path:
                    codex_pids = [pid for pid, cli_k in darwin_agents if cli_k == "codex"]
                    open_rollouts = _query_pids_codex_rollout_paths(
                        codex_pids, self.home, None
                    )
                    if open_rollouts:
                        return os.path.realpath(cand.jsonl_path) in open_rollouts
                return True
            cli_pids = live_pane_clis.get(cand.cli)
            if not cli_pids:
                return False
            if rec_agent_pid > 0:
                if rec_agent_pid not in cli_pids:
                    return False
                if cand.cli == "agy":
                    return any(c.session_key == cand.session_key for c in pane_proc_cands)
                if cand.cli == "codex":
                    live_codex = [c for c in pane_proc_cands if c.cli == "codex"]
                    if live_codex:
                        return any(c.session_key == cand.session_key for c in live_codex)
                return True
            return any(c.session_key == cand.session_key for c in pane_proc_cands)

        eff_wsl = _effective_is_wsl(env=self.env)

        def _enrich_from_pane(cand: SessionMatch, matched_by: str) -> SessionMatch:
            proc_hit = next(
                (c for c in pane_proc_cands if c.session_key == cand.session_key), None
            )
            eff_cwd = _normalize_cwd(
                cand.cwd
                or (proc_hit.cwd if proc_hit else "")
                or active_pane.current_path,
                wsl=eff_wsl,
            )
            eff_pts = cand.pts_mtime or (proc_hit.pts_mtime if proc_hit else 0.0)
            eff_tname = cand.thread_name or (proc_hit.thread_name if proc_hit else "")
            return SessionMatch(
                cli=cand.cli,
                session_id=cand.session_id,
                jsonl_path=cand.jsonl_path,
                cwd=eff_cwd,
                matched_by=matched_by,
                pts_mtime=eff_pts,
                jsonl_mtime=cand.jsonl_mtime,
                thread_name=eff_tname,
            )

        # Stage 1a: Active pane's #{pane_title} [#tag] or full UUID
        pane_hint = parse_window_title_hint(active_pane.pane_title)
        if pane_hint is not None:
            reg_hit, saw_stale_exited = _lookup_in_registry_detailed(
                pane_hint,
                rt_dirs,
                self.home,
                self.proc_root,
                matched_by="tmux_registry",
            )
            if reg_hit is not None:
                if _is_live_in_pane(reg_hit.match, reg_hit.agent_pid):
                    return _enrich_from_pane(reg_hit.match, "tmux_registry"), False
                saw_stale_exited = True
            if not saw_stale_exited:
                if pane_hint.is_full_uuid:
                    uuid_hit = _lookup_by_full_uuid(pane_hint, self.home, self.proc_root)
                    if uuid_hit is not None and _is_live_in_pane(uuid_hit):
                        return _enrich_from_pane(uuid_hit, "tmux_pane_title"), False
                else:
                    pfx_hit = _lookup_by_short_prefix(
                        pane_hint, self.home, self.proc_root, pane_proc_cands
                    )
                    if pfx_hit is not None and _is_live_in_pane(pfx_hit):
                        return _enrich_from_pane(pfx_hit, "tmux_pane_title"), False

        # Stage 1b: Runtime session registry entries bound to active_pane.pane_id
        pane_reg_hits: list[tuple[int, float, SessionMatch]] = []
        for dir_idx, _, rec in _iter_trusted_registry_records(rt_dirs):
            if str(rec.get("tmux_pane") or "").strip() != active_pane.pane_id:
                continue
            rec_sock = str(rec.get("tmux_socket") or "").strip()
            if (
                rec_sock
                and active_pane.socket_path
                and not _norm_same_path(rec_sock, active_pane.socket_path)
            ):
                continue
            rec_tty = str(rec.get("tty") or "").strip()
            if (
                rec_tty
                and active_pane.pane_tty
                and not _tty_matches(rec_tty, active_pane.pane_tty)
            ):
                continue
            cli = str(rec.get("cli") or "").strip().lower()
            validated = _validate_registry_record(
                rec,
                expected_cli=cli,
                home=self.home,
                proc_root=self.proc_root,
                matched_by="tmux_registry",
            )
            if validated is None:
                continue
            cand, updated_at, agent_pid = validated
            if not _is_live_in_pane(cand, agent_pid):
                continue
            pane_reg_hits.append((-dir_idx, updated_at, cand))

        if pane_reg_hits:
            pane_reg_hits.sort(key=lambda item: (item[0], item[1]), reverse=True)
            return _enrich_from_pane(pane_reg_hits[0][2], "tmux_registry"), False

        # Stage 2a: Scoped /proc root-session discovery inside active_pane's process tree ONLY
        if pane_proc_cands:
            best = _disambiguate_candidates(
                pane_proc_cands,
                active_pane.pane_title or window_title,
                active_pane.current_path,
                self.home,
            )
            if best is not None:
                return _enrich_from_pane(best, "tmux_proc_scan"), False

        # Stage 2b: Scoped Darwin (non-/proc) active-pane process + storage discovery via ps/lsof
        if use_ps and darwin_agents:
            pane_cwd = _normalize_cwd(active_pane.current_path, wsl=eff_wsl)
            for agent_pid, cli_kind in darwin_agents:
                eff_cwd = pane_cwd or _query_pid_cwd_nonproc(agent_pid)
                hit = _find_storage_session_for_cli(
                    cli_kind,
                    home=self.home,
                    proc_root=self.proc_root,
                    eff_cwd=eff_cwd,
                    window_title=active_pane.pane_title or window_title,
                    agent_pid=agent_pid,
                    matched_by="tmux_proc_scan",
                    claimed_claude_sids=eff_claimed,
                )
                if hit is not None:
                    return _enrich_from_pane(hit, "tmux_proc_scan"), False
            return None, True

        return None, bool(has_proc or (use_ps and darwin_live_pids))

    def _resolve_tmux_active_pane(
        self,
        active_pane: TmuxPaneInfo,
        rt_dirs: Sequence[Path],
        *,
        window_title: str = "",
        claimed_claude: Mapping[str, int] | None = None,
    ) -> SessionMatch | None:
        hit, _ = self._resolve_tmux_active_pane_detailed(
            active_pane,
            rt_dirs,
            window_title=window_title,
            claimed_claude=claimed_claude,
        )
        return hit

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

    @staticmethod
    def _collect_tmux_ttys(tmux_snap: TmuxServerSnapshot) -> set[str]:
        return {
            normalize_tty_path(p.pane_tty) for p in tmux_snap.panes if p.pane_tty
        } | {
            normalize_tty_path(c.client_tty) for c in tmux_snap.clients if c.client_tty
        }

    def _list_darwin_user_ps_rows(self) -> list[tuple[int, int, str, str, list[str]]] | None:
        try:
            r = subprocess.run(
                ["ps", "-ww", "-u", str(os.getuid()), "-o", "pid=,ppid=,stat=,tty=,args="],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=1.5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if r.returncode != 0 and not r.stdout:
            return None
        return _parse_ps_table(r.stdout.decode("utf-8", errors="ignore"))

    def _scan_darwin_live_non_tmux_sessions(
        self,
        tmux_snap: TmuxServerSnapshot,
        *,
        claimed_claude: Mapping[str, int] | None = None,
        allowed_pids: Collection[int] | None = None,
        rows: Sequence[tuple[int, int, str, str, list[str]]] | None = None,
    ) -> list[SessionMatch]:
        """Discover live interactive non-``tmux`` root sessions on Darwin via ``ps`` + ``lsof``."""
        eff_rows = self._list_darwin_user_ps_rows() if rows is None else rows
        if not eff_rows:
            return []
        excluded_ttys = self._collect_tmux_ttys(tmux_snap)
        allowed_pid_set = set(allowed_pids) if allowed_pids is not None else None

        live_rows: list[tuple[int, str, str]] = []
        for pid, _ppid, _stat, row_tty, argv in eff_rows:
            if not row_tty or row_tty in excluded_ttys:
                continue
            if allowed_pid_set is not None and pid not in allowed_pid_set:
                continue
            a0 = os.path.basename(argv[0].replace("\\", "/")).lstrip("-").lower() if argv else ""
            cli_k = _classify_interactive_cli(a0, argv)
            if cli_k is not None:
                live_rows.append((pid, row_tty, cli_k))
        if not live_rows:
            return []

        bounded_rows = live_rows[:32]
        files_by_pid, cwd_by_pid = _query_pids_open_files_and_cwds(
            [p for p, _t, _c in bounded_rows], None
        )

        eff_wsl = _effective_is_wsl(env=self.env)
        codex_root = _safe_wsl_dir(self.home / ".codex", wsl=eff_wsl)
        agy_root = _safe_wsl_dir(self.home / ".gemini", wsl=eff_wsl)
        codex_names = _load_codex_thread_names(self.home, wsl=eff_wsl)
        by_key: dict[str, SessionMatch] = {}

        for pid, row_tty, cli_k in bounded_rows:
            pts_mtime = _safe_mtime(row_tty)
            proc_cwd = cwd_by_pid.get(pid, "")
            open_files = files_by_pid.get(pid, ())

            if cli_k == "codex":
                if codex_root is None:
                    continue
                for target in open_files:
                    if (
                        not target.endswith(".jsonl")
                        or not os.path.basename(target).startswith("rollout-")
                        or not _is_path_under_dir(target, codex_root, wsl=eff_wsl)
                    ):
                        continue
                    safe_t = _safe_realpath(target, wsl=eff_wsl)
                    if not safe_t or not os.path.isfile(safe_t):
                        continue
                    is_cli, sid, meta_cwd = _read_codex_root_meta(safe_t, wsl=eff_wsl)
                    if not is_cli or not sid:
                        continue
                    cand = SessionMatch(
                        cli="codex",
                        session_id=sid,
                        jsonl_path=safe_t,
                        cwd=meta_cwd or proc_cwd,
                        matched_by="tty_ps",
                        pts_mtime=pts_mtime,
                        jsonl_mtime=_safe_mtime(safe_t),
                        thread_name=codex_names.get(sid, ""),
                    )
                    by_key[cand.session_key] = cand

            elif cli_k == "agy":
                if agy_root is None:
                    continue
                for target in open_files:
                    if (
                        not target.endswith(".lock")
                        or "/presence/" not in target.replace("\\", "/")
                        or not _is_path_under_dir(target, agy_root, wsl=eff_wsl)
                    ):
                        continue
                    safe_t = _safe_realpath(target, wsl=eff_wsl)
                    cid = Path(safe_t).stem.lower() if safe_t else ""
                    if not cid or not os.path.isfile(safe_t):
                        continue
                    tp = _resolve_agy_transcript_path(self.home, cid, wsl=eff_wsl)
                    if tp is not None and _is_agy_transcript_subagent(tp):
                        continue
                    tp_str, j_mtime = _agy_transcript_and_mtime(self.home, cid, safe_t)
                    cand = SessionMatch(
                        cli="agy",
                        session_id=cid,
                        jsonl_path=tp_str,
                        cwd=proc_cwd,
                        matched_by="tty_ps",
                        pts_mtime=pts_mtime,
                        jsonl_mtime=j_mtime,
                    )
                    by_key[cand.session_key] = cand

            elif cli_k == "claude" and proc_cwd:
                hit = _find_storage_session_for_cli(
                    "claude",
                    home=self.home,
                    proc_root=None,
                    eff_cwd=proc_cwd,
                    window_title="",
                    agent_pid=pid,
                    matched_by="tty_ps",
                    claimed_claude_sids=claimed_claude,
                )
                if hit is not None:
                    by_key[hit.session_key] = replace(hit, pts_mtime=pts_mtime)

        return list(by_key.values())

    def _darwin_is_unhooked_session_live(
        self,
        cand: SessionMatch,
        tmux_snap: TmuxServerSnapshot,
        *,
        norm_tty: str = "",
        window_pid: int = 0,
        allowed_pids: Collection[int] | None = None,
        rows: Sequence[tuple[int, int, str, str, list[str]]] | None = None,
    ) -> bool:
        if norm_tty:
            live_pids, tty_agents, _ = _list_tty_processes(
                norm_tty, None, window_pid=window_pid
            )
            if live_pids:
                if cand.cli not in {cli_k for _pid, cli_k in tty_agents}:
                    return False
                if cand.cli == "codex" and cand.jsonl_path:
                    eff_wsl = _effective_is_wsl(env=self.env)
                    open_rollouts = _query_pids_codex_rollout_paths(
                        [pid for pid, cli_k in tty_agents if cli_k == "codex"],
                        self.home,
                        None,
                    )
                    safe_cand_p = _safe_realpath(cand.jsonl_path, wsl=eff_wsl)
                    if open_rollouts and (not safe_cand_p or safe_cand_p not in open_rollouts):
                        return False
                elif cand.cli == "agy" and cand.session_id:
                    agy_root = self.home / ".gemini"
                    open_by_pid, _ = _query_pids_open_files_and_cwds(
                        [pid for pid, cli_k in tty_agents if cli_k == "agy"],
                        None,
                    )
                    open_stems = {
                        Path(target).stem.lower()
                        for files in open_by_pid.values()
                        for target in files
                        if target.endswith(".lock")
                        and "/presence/" in target.replace("\\", "/")
                        and _is_path_under_dir(target, agy_root)
                    }
                    if open_stems and cand.session_id.lower() not in open_stems:
                        return False
            return True

        if cand.cli not in ("codex", "claude", "agy"):
            return True
        eff_rows = self._list_darwin_user_ps_rows() if rows is None else rows
        if eff_rows is None:
            return True
        excluded_ttys = self._collect_tmux_ttys(tmux_snap)
        allowed_pid_set = set(allowed_pids) if allowed_pids is not None else None
        matching_pids: list[int] = []
        for pid, _ppid, _stat, rtty, argv in eff_rows:
            if not rtty or rtty in excluded_ttys:
                continue
            if allowed_pid_set is not None and pid not in allowed_pid_set:
                continue
            a0 = os.path.basename(argv[0].replace("\\", "/")).lstrip("-").lower() if argv else ""
            if _classify_interactive_cli(a0, argv) == cand.cli:
                matching_pids.append(pid)
        if not matching_pids:
            return False
        eff_wsl = _effective_is_wsl(env=self.env)
        if cand.cli == "codex" and cand.jsonl_path:
            open_rollouts = _query_pids_codex_rollout_paths(matching_pids, self.home, None)
            safe_cand_p = _safe_realpath(cand.jsonl_path, wsl=eff_wsl)
            return bool(safe_cand_p and safe_cand_p in open_rollouts)
        if cand.cli == "agy" and cand.session_id:
            agy_root = _safe_wsl_dir(self.home / ".gemini", wsl=eff_wsl)
            if agy_root is None:
                return False
            open_by_pid, _ = _query_pids_open_files_and_cwds(matching_pids, None)
            want_stem = cand.session_id.lower()
            return any(
                target.endswith(".lock")
                and "/presence/" in target.replace("\\", "/")
                and _is_path_under_dir(target, agy_root, wsl=eff_wsl)
                and Path(target).stem.lower() == want_stem
                for files in open_by_pid.values()
                for target in files
            )
        if cand.cli == "claude" and cand.cwd:
            eff_wsl = _effective_is_wsl(env=self.env)
            _, cwd_by_pid = _query_pids_open_files_and_cwds(matching_pids, None)
            return any(
                pcwd and _cwd_match_rank(pcwd, cand.cwd, self.home, wsl=eff_wsl) > 0
                for pcwd in cwd_by_pid.values()
            )
        return True

    def _filter_by_title_affinity(
        self,
        candidates: Sequence[SessionMatch],
        eff_title: str,
        *,
        scoped: bool = False,
    ) -> list[SessionMatch]:
        if not candidates or not eff_title:
            return list(candidates)
        s_title = eff_title.strip()
        if not s_title:
            return list(candidates)
        if _WIN_EXPLICIT_NON_WSL_TITLE_RE.match(s_title):
            return []
        local_prompt_cwd = extract_local_prompt_cwd(s_title, self._local_hostnames)
        if scoped and local_prompt_cwd is None:
            return list(candidates)
        if not scoped and _GENERIC_LOCAL_TERM_TITLE_RE.match(s_title):
            return list(candidates)
        cli_word = _infer_cli_word_from_title(s_title)
        if _WORD_TMUX_RE.search(s_title) or _TUI_SPINNER_RE.search(s_title):
            return list(candidates)
        if local_prompt_cwd is not None and cli_word:
            return list(candidates)
        expanded = (
            _expand_home_prefix(local_prompt_cwd, self.home)
            if local_prompt_cwd is not None
            else ""
        )
        eff_wsl = _effective_is_wsl(env=self.env)
        title_low = s_title.lower()
        return [
            c
            for c in candidates
            if (local_prompt_cwd is None and cli_word and c.cli == cli_word)
            or (
                c.cwd
                and (
                    (expanded and _cwd_match_rank(expanded, c.cwd, self.home, wsl=eff_wsl) > 0)
                    or (local_prompt_cwd != "~" and _dir_basename_in_title(s_title, c.cwd))
                )
            )
            or (
                c.thread_name
                and len(c.thread_name) >= 2
                and c.thread_name.lower() in title_low
            )
        ]

    def _tmux_target_matches_unscoped_title(
        self,
        eff_title: str,
        active_pane: TmuxPaneInfo,
        pane_hit: SessionMatch | None,
    ) -> bool:
        s_title = (eff_title or "").strip()
        if not s_title or _GENERIC_LOCAL_TERM_TITLE_RE.match(s_title):
            return True
        if _WIN_EXPLICIT_NON_WSL_TITLE_RE.match(s_title):
            return False
        if (
            _WORD_TMUX_RE.search(s_title)
            or _TUI_SPINNER_RE.search(s_title)
            or _infer_cli_word_from_title(s_title)
        ):
            return True
        title_low = s_title.lower()
        ptitle_low = (active_pane.pane_title or "").strip().lower()
        if len(ptitle_low) >= 2 and ptitle_low in title_low:
            return True
        local_prompt_cwd = extract_local_prompt_cwd(s_title, self._local_hostnames)
        if local_prompt_cwd is not None:
            eff_wsl = _effective_is_wsl(env=self.env)
            expanded = _expand_home_prefix(local_prompt_cwd, self.home)
            for p_cwd in (active_pane.current_path, pane_hit.cwd if pane_hit else ""):
                if p_cwd and (
                    _cwd_match_rank(expanded, p_cwd, self.home, wsl=eff_wsl) > 0
                    or (local_prompt_cwd != "~" and _dir_basename_in_title(s_title, p_cwd))
                ):
                    return True
            return False
        for p_cwd in (active_pane.current_path, pane_hit.cwd if pane_hit else ""):
            if p_cwd and _dir_basename_in_title(s_title, p_cwd):
                return True
        if (
            pane_hit is not None
            and pane_hit.thread_name
            and len(pane_hit.thread_name) >= 2
            and pane_hit.thread_name.lower() in title_low
        ):
            return True
        return False

    def _find_proc_ssh_target_in_pids(self, pids: Iterable[int]) -> str | None:
        if self.proc_root is None or not self.proc_root.is_dir():
            return None
        pid_list = [p for p in pids if p > 0]
        if not pid_list:
            return None
        active_clis: list[tuple[int, int, int]] = []
        active_sshs: list[tuple[int, int, int, str]] = []
        for pid in pid_list:
            pdir = self.proc_root / str(pid)
            st_ch, pgrp, tpgid = _read_proc_stat_state_pgrp_tpgid(pdir)
            if st_ch in ("T", "t", "Z"):
                continue
            if _classify_proc_cli(pdir) is not None:
                active_clis.append((pid, pgrp, tpgid))
                continue
            argv = _read_proc_cmdline_args(pdir)
            if not argv:
                continue
            host_cand, is_inter = extract_ssh_target_from_argv(argv)
            if is_inter:
                active_sshs.append((pid, pgrp, tpgid, host_cand))
        if not active_sshs:
            return None
        # Foreground process group (`pgrp == tpgid`) beats backgrounded CLI on the same TTY
        fg_sshs = [s for s in active_sshs if s[2] > 0 and s[1] == s[2]]
        fg_clis = [c for c in active_clis if c[2] > 0 and c[1] == c[2]]
        if fg_sshs and not fg_clis:
            for _pid, _pgrp, _tpgid, h_cand in fg_sshs:
                if h_cand:
                    return h_cand
            return ""
        if active_clis:
            return None
        for _pid, _pgrp, _tpgid, h_cand in active_sshs:
            if h_cand:
                return h_cand
        return ""

    def _find_tty_ssh_target(
        self,
        norm_tty: str,
        *,
        has_proc: bool,
        darwin_ps_rows: Sequence[tuple[int, int, str, str, list[str]]] | None = None,
    ) -> str | None:
        if not norm_tty:
            return None
        if has_proc and self.proc_root is not None:
            tty_pids: list[int] = []
            for pdir in _iter_numeric_proc_dirs(self.proc_root):
                ptty = _read_proc_tty_path(pdir) or _readlink_clean(pdir / "fd" / "0")
                if ptty and _tty_matches(ptty, norm_tty):
                    tty_pids.append(int(pdir.name))
            return self._find_proc_ssh_target_in_pids(tty_pids)
        if self.proc_root is None:
            return None
        rows: list[tuple[int, int, str, str, list[str]]] = []
        if darwin_ps_rows is not None:
            rows = [r for r in darwin_ps_rows if r[3] and _tty_matches(r[3], norm_tty)]
        else:
            tty_arg = norm_tty[5:] if norm_tty.startswith("/dev/") else norm_tty
            try:
                r = subprocess.run(
                    ["ps", "-ww", "-o", "pid=,ppid=,stat=,tty=,args=", "-t", tty_arg],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=1.0,
                    check=False,
                )
                rows = [
                    row
                    for row in _parse_ps_table(r.stdout.decode("utf-8", errors="ignore"))
                    if not row[3] or _tty_matches(row[3], norm_tty)
                ]
            except (OSError, subprocess.SubprocessError):
                return None
        active_cli_rows: list[str] = []
        active_ssh_rows: list[tuple[bool, str]] = []
        for _pid, _ppid, stat_s, _rtty, argv in rows:
            if "T" in stat_s or "Z" in stat_s:
                continue
            a0 = os.path.basename(argv[0].replace("\\", "/")).lstrip("-").lower() if argv else ""
            if _classify_interactive_cli(a0, argv) is not None:
                active_cli_rows.append(stat_s)
                continue
            host_cand, is_inter = extract_ssh_target_from_argv(argv)
            if is_inter:
                active_ssh_rows.append(("+" in stat_s, host_cand))
        if not active_ssh_rows:
            return None
        fg_ssh = [h for is_fg, h in active_ssh_rows if is_fg]
        has_fg_cli = any("+" in st for st in active_cli_rows)
        if fg_ssh and not has_fg_cli:
            for h in fg_ssh:
                if h:
                    return h
            return ""
        if active_cli_rows:
            return None
        for _is_fg, h in active_ssh_rows:
            if h:
                return h
        return ""

    def _find_unscoped_proc_ssh_target(
        self,
        *,
        window_title: str,
        excluded_pids: set[int],
        local_cands: Sequence[SessionMatch],
    ) -> str | None:
        """When WSL2/Linux lacks window_tty & window_pid, veto local crosstalk if an SSH tab has newer TTY activity."""
        if not local_cands or self.proc_root is None or not self.proc_root.is_dir():
            return None
        wtitle = (window_title or "").strip()
        if wtitle:
            if _WIN_EXPLICIT_NON_WSL_TITLE_RE.match(wtitle):
                return None
            if _infer_cli_word_from_title(wtitle) or _TUI_SPINNER_RE.search(wtitle):
                return None
            title_low = wtitle.lower()
            if any(
                (c.cwd and _dir_basename_in_title(wtitle, c.cwd))
                or (c.thread_name and len(c.thread_name) >= 2 and c.thread_name.lower() in title_low)
                for c in local_cands
            ):
                return None
            if extract_local_prompt_cwd(wtitle, self._local_hostnames) is not None:
                return None

        cli_ttys: set[str] = set()
        ssh_by_tty: dict[str, float] = {}
        for pdir in _iter_numeric_proc_dirs(self.proc_root):
            pid = int(pdir.name)
            if pid in excluded_pids:
                continue
            st_ch, _pgrp, _tpgid = _read_proc_stat_state_pgrp_tpgid(pdir)
            if st_ch in ("T", "t", "Z"):
                continue
            ptty = _read_proc_tty_path(pdir)
            if not ptty:
                continue
            if _classify_proc_cli(pdir) is not None:
                cli_ttys.add(ptty)
                continue
            argv = _read_proc_cmdline_args(pdir)
            if not argv:
                continue
            host_cand, is_inter = extract_ssh_target_from_argv(argv)
            if not is_inter:
                continue
            if host_cand and host_cand in self._local_hostnames:
                continue
            mtime = _safe_mtime(ptty)
            prev = ssh_by_tty.get(ptty)
            if prev is None or mtime >= prev:
                ssh_by_tty[ptty] = mtime

        valid_ssh = [mtime for tty, mtime in ssh_by_tty.items() if tty not in cli_ttys]
        if not valid_ssh:
            return None
        best_ssh_mtime = max(valid_ssh)
        best_local_mtime = max(max(c.pts_mtime, c.jsonl_mtime) for c in local_cands)
        if best_ssh_mtime > 0.0 and best_ssh_mtime >= best_local_mtime:
            # Return neutral "" so unscoped background SSH vetoes local crosstalk
            # without authorizing an outbound SSH connection from an untitled local shell!
            return ""
        return None

    def _find_cached_remote_state(
        self,
        host_name: str,
        *,
        hint: TitleHint | None = None,
        cwd_hint: str = "",
    ) -> SessionState | None:
        with self._lock:
            host_states = [
                st for st in self._states.values() if st.is_remote and st.host_name == host_name
            ]
            if not host_states:
                return None
            if hint is not None and hint.session_token:
                tok = hint.session_token.lower()
                for st in reversed(host_states):
                    if st.session_id.startswith(tok) and (
                        not hint.explicit_cli or not hint.cli or st.cli == hint.cli
                    ):
                        return st
                return None
            if cwd_hint:
                tail = cwd_hint[2:].rstrip("/") if cwd_hint.startswith("~/") else cwd_hint.rstrip("/")
                for st in reversed(host_states):
                    if st.cwd and (
                        st.cwd == cwd_hint
                        or (tail and (st.cwd == tail or st.cwd.endswith("/" + tail)))
                    ):
                        return st
                return None
            return host_states[-1] if len(host_states) == 1 else None

    def _remote_route_match(
        self, host_cfg: RemoteHostConfig, remote_fg: RemoteForegroundInfo
    ) -> SessionMatch:
        r_hint = parse_window_title_hint(remote_fg.effective_title)
        cached_st = self._find_cached_remote_state(
            host_cfg.name, hint=r_hint, cwd_hint=remote_fg.cwd_hint
        )
        if cached_st is not None:
            return SessionMatch(
                cli=cached_st.cli,
                session_id=cached_st.session_id,
                jsonl_path="",
                cwd=remote_fg.cwd_hint,
                matched_by="remote_cache",
                origin="remote",
                host_name=host_cfg.name,
                remote_title=remote_fg.effective_title,
            )
        cli_val = (r_hint.cli or "remote") if r_hint is not None else "remote"
        sid_val = r_hint.session_token if r_hint is not None else f"pending-{host_cfg.name}"
        return SessionMatch(
            cli=cli_val,
            session_id=sid_val,
            jsonl_path="",
            cwd=remote_fg.cwd_hint,
            matched_by="remote_whitelist",
            origin="remote",
            host_name=host_cfg.name,
            remote_title=remote_fg.effective_title,
        )

    def resolve_session(
        self,
        window_title: str = "",
        *,
        window_tty: str = "",
        window_cwd: str = "",
        window_pid: int = 0,
        window_process: str = "",
        bundle_id: str = "",
        document: str = "",
    ) -> SessionMatch | None:
        """Resolve foreground main CLI session via Phase 0 Remote Veto, title O(1) lookup, tmux active pane, TTY/CWD, or fallback."""
        with self._lock:
            # Phase 0 Fast Check: Remote IDE or dedicated SSH client or explicit [@remote_host] / user@remote:path / ssh ...
            early_remote = detect_remote_foreground(
                window_title,
                window_process=window_process,
                bundle_id=bundle_id,
                document=document,
                local_hostnames=self._local_hostnames,
                is_macos=(sys.platform == "darwin" or (self.proc_root == Path("/proc") and not Path("/proc").is_dir())),
            )
            if early_remote.is_remote:
                if (
                    early_remote.ide_remote_only
                    or early_remote.ambiguous_host
                    or not early_remote.host_candidate
                ):
                    return None
                host_cfg = self.remote_cfg.lookup_host(early_remote.host_candidate)
                if host_cfg is None:
                    return None
                return self._remote_route_match(host_cfg, early_remote)

            if _WIN_EXPLICIT_NON_WSL_TITLE_RE.match((window_title or "").strip()):
                return None
            proc_base_low = (
                os.path.basename(window_process.replace("\\", "/")).lower()
                if window_process
                else ""
            )
            if proc_base_low in ("cmd", "cmd.exe") and parse_window_title_hint(window_title) is None:
                return None
            if (
                proc_base_low in ("powershell", "powershell.exe", "pwsh", "pwsh.exe")
                and parse_window_title_hint(window_title) is None
                and re.match(
                    r"^(?:Administrator:\s+|管理员:\s*)?(?:Windows\s+)?PowerShell(?:\s+[\d.]+)?\s*$",
                    (window_title or "").strip(),
                    re.IGNORECASE,
                )
            ):
                return None

            rt_dirs = self._candidate_runtime_dirs()
            eff_wsl = _effective_is_wsl(env=self.env)
            norm_tty = normalize_tty_path(window_tty)
            norm_cwd = (
                ""
                if is_wsl_slow_mount_path(window_cwd, wsl=eff_wsl)
                else _normalize_cwd(window_cwd)
            )
            hint = parse_window_title_hint(window_title)
            tmux_snap = self._probe_tmux(rt_dirs=rt_dirs)
            has_proc = self.proc_root is not None and self.proc_root.is_dir()
            is_real_proc = has_proc and self.proc_root == Path("/proc")
            is_real_darwin = not has_proc and self.proc_root == Path("/proc")

            darwin_ptys_probed = False
            darwin_ps_rows: list[tuple[int, int, str, str, list[str]]] | None = None
            darwin_win_tab_ttys: set[str] = set()
            darwin_win_desc_pids: set[int] = set()

            def _ensure_darwin_window_ptys() -> None:
                nonlocal norm_tty, darwin_ptys_probed, darwin_ps_rows, darwin_win_tab_ttys, darwin_win_desc_pids
                if darwin_ptys_probed:
                    return
                darwin_ptys_probed = True
                if norm_tty or window_pid <= 0 or has_proc or self.proc_root is None:
                    return
                pinfo = ps_proc_info(window_pid)
                if pinfo is not None and pinfo[2] and (
                    _classify_interactive_cli(pinfo[0], pinfo[3])
                    or pinfo[0].lower() in ("tmux", "screen", "zellij")
                ):
                    norm_tty = pinfo[2]
                    return
                if is_real_darwin and window_pid > 1:
                    darwin_ps_rows = self._list_darwin_user_ps_rows()
                    if darwin_ps_rows:
                        darwin_win_tab_ttys, darwin_win_desc_pids = collect_darwin_window_pty_tree(
                            darwin_ps_rows, window_pid
                        )
                        if len(darwin_win_tab_ttys) == 1:
                            norm_tty = next(iter(darwin_win_tab_ttys))

            if not norm_tty and window_pid > 0 and (hint is None or bool(tmux_snap.clients)):
                _ensure_darwin_window_ptys()

            active_target = self._select_active_tmux_target(
                tmux_snap,
                window_title,
                window_tty=norm_tty,
                window_pid=window_pid,
                allowed_client_ttys=(darwin_win_tab_ttys if len(darwin_win_tab_ttys) > 1 else None),
                allowed_client_pids=(darwin_win_desc_pids if len(darwin_win_tab_ttys) > 1 else None),
            )
            claimed_claude = (
                _collect_live_claimed_claude_sids(rt_dirs, self.proc_root)
                if self.proc_root is not None
                else {}
            )

            all_tmux_pids: set[int] = set()
            if has_proc and self.proc_root is not None and (tmux_snap.panes or tmux_snap.clients):
                roots = [p.pane_pid for p in tmux_snap.panes if p.pane_pid > 1] + [
                    c.client_pid for c in tmux_snap.clients if c.client_pid > 1
                ]
                all_tmux_pids = _collect_descendant_pids(self.proc_root, roots)
                for p in tmux_snap.panes:
                    if p.pane_pid <= 1 and p.pane_tty:
                        all_tmux_pids.update(
                            _collect_pane_pids(self.proc_root, 0, p.pane_tty)
                        )

            def _return_remote_fg(rfg: RemoteForegroundInfo) -> SessionMatch | None:
                if rfg.ide_remote_only or rfg.ambiguous_host or not rfg.host_candidate:
                    return None
                hcfg = self.remote_cfg.lookup_host(rfg.host_candidate)
                return self._remote_route_match(hcfg, rfg) if hcfg is not None else None

            def _with_cwd(hit: SessionMatch | None) -> SessionMatch | None:
                return (
                    replace(hit, cwd=norm_cwd)
                    if (hit is not None and norm_cwd and not hit.cwd)
                    else hit
                )

            outer_hint_owned_by_tmux = bool(
                hint is not None
                and active_target is not None
                and self._outer_hint_belongs_to_tmux(window_title, hint, active_target[1])
            )
            stale_hint_fallthrough = False
            stale_hint_ttys: set[str] = set()
            neutralize_outer_title = False

            early_tty_is_tmux_client = bool(
                norm_tty
                and any(
                    c.client_tty and _tty_matches(c.client_tty, norm_tty)
                    for c in tmux_snap.clients
                )
            )
            # Phase 0 Priority: If the known foreground TTY is actively running interactive `ssh`,
            # veto/route BEFORE any 8-hex `[#tag]` collision can return another TTY's local session!
            if norm_tty and not early_tty_is_tmux_client and (has_proc or is_real_darwin):
                ssh_tty_early = self._find_tty_ssh_target(
                    norm_tty, has_proc=has_proc, darwin_ps_rows=darwin_ps_rows
                )
                if ssh_tty_early is not None:
                    rfg_early = detect_remote_foreground(
                        window_title or "",
                        window_process=window_process,
                        bundle_id=bundle_id,
                        document=document,
                        local_hostnames=self._local_hostnames,
                        ssh_proc_host=ssh_tty_early,
                        is_macos=is_real_darwin,
                    )
                    return _return_remote_fg(rfg_early)

            if (
                hint is not None
                and active_target is not None
                and not outer_hint_owned_by_tmux
                and any(
                    p.pane_id != active_target[1].pane_id
                    and _same_tmux_server(p.socket_path, active_target[1].socket_path)
                    and self._outer_hint_belongs_to_tmux(window_title, hint, p)
                    for p in tmux_snap.panes
                )
            ):
                outer_hint_owned_by_tmux = True
                neutralize_outer_title = True

            if hint is not None and not outer_hint_owned_by_tmux:
                reg_hit, saw_stale_exited = _lookup_in_registry_detailed(
                    hint, rt_dirs, self.home, self.proc_root
                )
                if reg_hit is not None:
                    if (
                        active_target is not None
                        and reg_hit.tmux_pane
                        and _same_tmux_server(reg_hit.tmux_socket, active_target[1].socket_path)
                    ):
                        outer_hint_owned_by_tmux = True
                        if reg_hit.tmux_pane != active_target[1].pane_id:
                            neutralize_outer_title = True
                    elif not reg_hit.tmux_pane and (
                        early_tty_is_tmux_client
                        or (norm_tty and reg_hit.tty and not _tty_matches(norm_tty, reg_hit.tty))
                        or (
                            darwin_win_desc_pids
                            and reg_hit.agent_pid > 0
                            and reg_hit.agent_pid not in darwin_win_desc_pids
                        )
                    ):
                        stale_hint_fallthrough = True
                    else:
                        return _with_cwd(reg_hit.match)
                elif saw_stale_exited:
                    stale_hint_fallthrough = True
                    stale_hint_ttys = _collect_stale_registry_ttys(hint, rt_dirs)
                else:
                    all_proc_cands = (
                        scan_proc_root_sessions(
                            self.home,
                            self.proc_root,
                            claimed_claude_sids=claimed_claude,
                        )
                        if (has_proc and self.proc_root is not None)
                        else []
                    )
                    if hint.is_full_uuid:
                        disk_hit = _lookup_by_full_uuid(hint, self.home, self.proc_root)
                    else:
                        disk_hit = _lookup_by_short_prefix(
                            hint, self.home, self.proc_root, all_proc_cands
                        )
                    if disk_hit is None:
                        # Explicit session tag in title that matches no valid root session MUST return None
                        # rather than silently guessing an unrelated window's conversation!
                        return None
                    _ensure_darwin_window_ptys()
                    hint_tty_is_tmux_client = bool(
                        norm_tty
                        and any(
                            c.client_tty and _tty_matches(c.client_tty, norm_tty)
                            for c in tmux_snap.clients
                        )
                    )
                    if hint_tty_is_tmux_client:
                        stale_hint_fallthrough = True
                    elif (
                        is_real_proc or any(c.cli == disk_hit.cli for c in all_proc_cands)
                    ) and not any(
                        c.session_key == disk_hit.session_key for c in all_proc_cands
                    ):
                        # Bug D guard: dead historical transcript on disk MUST NOT return a dead match
                        # NOR fall through to hijack an unrelated background tab when not in tmux/tty!
                        if active_target is None and not norm_tty:
                            return None
                        stale_hint_fallthrough = True
                    elif (
                        is_real_darwin
                        and (
                            active_target is not None
                            or bool(norm_tty)
                            or bool(darwin_win_tab_ttys)
                        )
                        and not self._darwin_is_unhooked_session_live(
                            disk_hit,
                            tmux_snap,
                            norm_tty=norm_tty,
                            window_pid=window_pid,
                            allowed_pids=(darwin_win_desc_pids if darwin_win_desc_pids else None),
                            rows=darwin_ps_rows,
                        )
                    ):
                        if active_target is None and not norm_tty:
                            return None
                        stale_hint_fallthrough = True
                    else:
                        return _with_cwd(disk_hit)

            eff_title = (
                ""
                if (stale_hint_fallthrough or neutralize_outer_title)
                else (window_title or "")
            )

            _ensure_darwin_window_ptys()

            if stale_hint_fallthrough or norm_tty or len(darwin_win_tab_ttys) > 1:
                active_target = self._select_active_tmux_target(
                    tmux_snap,
                    eff_title,
                    window_tty=norm_tty,
                    window_pid=window_pid,
                    allowed_client_ttys=(
                        darwin_win_tab_ttys if len(darwin_win_tab_ttys) > 1 else None
                    ),
                    allowed_client_pids=(
                        darwin_win_desc_pids if len(darwin_win_tab_ttys) > 1 else None
                    ),
                )

            tty_matches_tmux_client = bool(
                norm_tty
                and any(
                    c.client_tty and _tty_matches(c.client_tty, norm_tty)
                    for c in tmux_snap.clients
                )
            )
            if norm_tty and not tty_matches_tmux_client:
                ssh_tty_host = self._find_tty_ssh_target(
                    norm_tty, has_proc=has_proc, darwin_ps_rows=darwin_ps_rows
                )
                if ssh_tty_host is not None:
                    rfg_tty = detect_remote_foreground(
                        eff_title,
                        window_process=window_process,
                        bundle_id=bundle_id,
                        document=document,
                        local_hostnames=self._local_hostnames,
                        ssh_proc_host=ssh_tty_host,
                    )
                    return _return_remote_fg(rfg_tty)
                tty_hit, stop_fallback = _resolve_by_window_tty(
                    norm_tty,
                    norm_cwd=norm_cwd,
                    window_pid=window_pid,
                    window_title=eff_title,
                    runtime_dirs=rt_dirs,
                    home=self.home,
                    proc_root=self.proc_root,
                    claimed_claude_sids=claimed_claude,
                )
                if tty_hit is not None or stop_fallback:
                    return tty_hit

            if not norm_tty and window_pid > 1 and active_target is None:
                if has_proc and self.proc_root is not None:
                    wp_pids = _collect_descendant_pids(self.proc_root, (window_pid,)) - all_tmux_pids
                    ssh_wp_host = self._find_proc_ssh_target_in_pids(wp_pids)
                    if ssh_wp_host is not None:
                        rfg_wp = detect_remote_foreground(
                            eff_title,
                            window_process=window_process,
                            bundle_id=bundle_id,
                            document=document,
                            local_hostnames=self._local_hostnames,
                            ssh_proc_host=ssh_wp_host,
                        )
                        return _return_remote_fg(rfg_wp)
                elif darwin_win_desc_pids and darwin_ps_rows:
                    excluded_ttys_d = self._collect_tmux_ttys(tmux_snap)
                    d_rows = [
                        r
                        for r in darwin_ps_rows
                        if r[0] in darwin_win_desc_pids and (not r[3] or r[3] not in excluded_ttys_d)
                    ]
                    if d_rows and not any(
                        _classify_interactive_cli(
                            os.path.basename(r[4][0].replace("\\", "/")).lstrip("-").lower() if r[4] else "",
                            r[4],
                        )
                        for r in d_rows
                    ):
                        for _pid, _ppid, _st, _rtty, argv in d_rows:
                            h_cand, is_int = extract_ssh_target_from_argv(argv)
                            if is_int:
                                rfg_d = detect_remote_foreground(
                                    eff_title,
                                    window_process=window_process,
                                    bundle_id=bundle_id,
                                    document=document,
                                    local_hostnames=self._local_hostnames,
                                    ssh_proc_host=h_cand,
                                    is_macos=True,
                                )
                                return _return_remote_fg(rfg_d)

            def _check_active_pane_remote(active_pane: TmuxPaneInfo) -> tuple[bool, SessionMatch | None]:
                pane_ssh_host: str | None = None
                if has_proc and self.proc_root is not None:
                    p_pids = _collect_pane_pids(self.proc_root, active_pane.pane_pid, active_pane.pane_tty)
                    pane_ssh_host = self._find_proc_ssh_target_in_pids(p_pids)
                elif self.proc_root is not None and active_pane.pane_tty:
                    pane_ssh_host = self._find_tty_ssh_target(
                        normalize_tty_path(active_pane.pane_tty),
                        has_proc=False,
                        darwin_ps_rows=darwin_ps_rows,
                    )
                rfg_pane = detect_remote_foreground(
                    eff_title,
                    window_process=window_process,
                    bundle_id=bundle_id,
                    document=document,
                    local_hostnames=self._local_hostnames,
                    ssh_proc_host=pane_ssh_host,
                    pane_title_override=active_pane.pane_title,
                    is_macos=is_real_darwin,
                )
                if rfg_pane.is_remote:
                    return True, _return_remote_fg(rfg_pane)
                return False, None

            def _collect_darwin_scoped_non_tmux_cands() -> list[SessionMatch]:
                scoped_cands: dict[str, SessionMatch] = {}
                for cand in self._scan_darwin_live_non_tmux_sessions(
                    tmux_snap,
                    claimed_claude=claimed_claude,
                    allowed_pids=(darwin_win_desc_pids if darwin_win_desc_pids else None),
                    rows=darwin_ps_rows,
                ):
                    scoped_cands[cand.session_key] = cand
                if darwin_win_tab_ttys and darwin_ps_rows:
                    excluded_ttys = self._collect_tmux_ttys(tmux_snap)
                    pids_by_tty: dict[str, set[int]] = {}
                    clis_by_tty: dict[str, set[str]] = {}
                    for r_pid, _ppid, _stat, r_tty, r_argv in darwin_ps_rows:
                        if r_pid in darwin_win_desc_pids and r_tty and r_tty not in excluded_ttys:
                            pids_by_tty.setdefault(r_tty, set()).add(r_pid)
                            a0 = os.path.basename(r_argv[0].replace("\\", "/")).lstrip("-").lower() if r_argv else ""
                            if cli_k := _classify_interactive_cli(a0, r_argv):
                                clis_by_tty.setdefault(r_tty, set()).add(cli_k)
                    for tab_tty in sorted(darwin_win_tab_ttys):
                        if tab_tty in excluded_ttys:
                            continue
                        reg_cand = _lookup_tty_in_registry(
                            tab_tty,
                            pids_by_tty.get(tab_tty, set()),
                            clis_by_tty.get(tab_tty, set()),
                            rt_dirs,
                            self.home,
                            self.proc_root,
                            fallback_cwd=norm_cwd,
                        )
                        if reg_cand is not None:
                            scoped_cands[reg_cand.session_key] = reg_cand
                return list(scoped_cands.values())

            if active_target is not None:
                active_client, active_pane = active_target
                tmux_is_scoped = bool(
                    outer_hint_owned_by_tmux
                    or tty_matches_tmux_client
                    or norm_tty
                    or window_pid > 0
                    or norm_cwd
                )
                if has_proc and self.proc_root is not None:
                    if not outer_hint_owned_by_tmux and not tty_matches_tmux_client:
                        non_tmux_cands = self._filter_by_title_affinity(
                            scan_proc_root_sessions(
                                self.home,
                                self.proc_root,
                                excluded_pids=all_tmux_pids,
                                claimed_claude_sids=claimed_claude,
                            ),
                            eff_title,
                            scoped=bool(norm_tty or norm_cwd),
                        )
                        if non_tmux_cands and self._should_prefer_non_tmux(
                            eff_title, active_client, active_pane, non_tmux_cands
                        ):
                            return _disambiguate_candidates(
                                non_tmux_cands, eff_title, norm_cwd, self.home
                            )
                    # Phase 0 Scope B: Active local tmux pane running interactive `ssh` or [@remote_host]
                    is_pane_rem, pane_rem_match = _check_active_pane_remote(active_pane)
                    if is_pane_rem:
                        if not tmux_is_scoped and not self._tmux_target_matches_unscoped_title(
                            eff_title, active_pane, pane_rem_match
                        ):
                            return None
                        return pane_rem_match
                    pane_res = self._resolve_tmux_active_pane(
                        active_pane,
                        rt_dirs,
                        window_title=eff_title,
                        claimed_claude=claimed_claude,
                    )
                    if not tmux_is_scoped and not self._tmux_target_matches_unscoped_title(
                        eff_title, active_pane, pane_res
                    ):
                        return None
                    return pane_res

                is_pane_rem, pane_rem_match = _check_active_pane_remote(active_pane)
                pane_hit, pane_authoritative_empty = (
                    (None, True)
                    if is_pane_rem
                    else self._resolve_tmux_active_pane_detailed(
                        active_pane,
                        rt_dirs,
                        window_title=eff_title,
                        claimed_claude=claimed_claude,
                    )
                )
                if outer_hint_owned_by_tmux or tty_matches_tmux_client:
                    return pane_rem_match if is_pane_rem else pane_hit
                if is_real_darwin:
                    non_tmux_cands = self._filter_by_title_affinity(
                        _collect_darwin_scoped_non_tmux_cands(),
                        eff_title,
                        scoped=bool(darwin_win_tab_ttys or norm_tty or norm_cwd),
                    )
                    if non_tmux_cands and self._should_prefer_non_tmux(
                        eff_title, active_client, active_pane, non_tmux_cands
                    ):
                        return _disambiguate_candidates(
                            non_tmux_cands, eff_title, norm_cwd, self.home
                        )
                    if not tmux_is_scoped and not self._tmux_target_matches_unscoped_title(
                        eff_title, active_pane, pane_rem_match if is_pane_rem else pane_hit
                    ):
                        return None
                    return pane_rem_match if is_pane_rem else pane_hit
                tmux_bound_keys = _collect_tmux_bound_session_keys(rt_dirs)
                if pane_hit is not None:
                    tmux_bound_keys.add(pane_hit.session_key)
                tmux_pane_uuids = {
                    ph.session_token
                    for p in tmux_snap.panes
                    for ph in (parse_window_title_hint(p.pane_title),)
                    if ph is not None and ph.is_full_uuid
                }
                all_fallback = _scan_nonproc_fallback_sessions(
                    self.home,
                    rt_dirs,
                    window_cwd=norm_cwd,
                    check_pid_alive=(self.proc_root is not None),
                )
                non_tmux_cands = self._filter_by_title_affinity(
                    [
                        c
                        for c in all_fallback
                        if c.session_key not in tmux_bound_keys
                        and c.session_id not in tmux_pane_uuids
                    ],
                    eff_title,
                    scoped=bool(norm_cwd),
                )
                if non_tmux_cands and self._should_prefer_non_tmux(
                    eff_title, active_client, active_pane, non_tmux_cands
                ):
                    return _disambiguate_candidates(
                        non_tmux_cands, eff_title, norm_cwd, self.home
                    )
                if is_pane_rem:
                    if not tmux_is_scoped and not self._tmux_target_matches_unscoped_title(
                        eff_title, active_pane, pane_rem_match
                    ):
                        return None
                    return pane_rem_match
                if (
                    pane_hit is not None
                    or pane_authoritative_empty
                    or (
                        _WORD_TMUX_RE.search(eff_title)
                        and not _TUI_SPINNER_RE.search(eff_title)
                    )
                ):
                    if not tmux_is_scoped and not self._tmux_target_matches_unscoped_title(
                        eff_title, active_pane, pane_hit
                    ):
                        return None
                    return pane_hit
                filtered_fb = self._filter_by_title_affinity(
                    non_tmux_cands or all_fallback, eff_title, scoped=bool(norm_cwd)
                )
                return _disambiguate_candidates(
                    filtered_fb, eff_title, norm_cwd, self.home
                )

            if has_proc and self.proc_root is not None:
                allowed_stale_pids: set[int] | None = None
                if stale_hint_fallthrough:
                    if not stale_hint_ttys:
                        return None
                    allowed_stale_pids = set()
                    for pdir in _iter_numeric_proc_dirs(self.proc_root):
                        ptty = _read_proc_tty_path(pdir) or _readlink_clean(pdir / "fd" / "0")
                        if ptty and any(
                            ptty == st_t or _tty_matches(ptty, st_t) for st_t in stale_hint_ttys
                        ):
                            allowed_stale_pids.add(int(pdir.name))
                    if not allowed_stale_pids:
                        return None
                proc_cands = scan_proc_root_sessions(
                    self.home,
                    self.proc_root,
                    allowed_pids=allowed_stale_pids,
                    excluded_pids=all_tmux_pids or None,
                    claimed_claude_sids=claimed_claude,
                )
                # Phase 0 Scope D: Unscoped /proc interactive SSH tab with newer TTY activity
                if window_pid <= 0 and not norm_tty and not stale_hint_fallthrough and hint is None:
                    ssh_unscoped = self._find_unscoped_proc_ssh_target(
                        window_title=eff_title,
                        excluded_pids=all_tmux_pids,
                        local_cands=proc_cands,
                    )
                    if ssh_unscoped is not None:
                        rfg_u = detect_remote_foreground(
                            eff_title,
                            window_process=window_process,
                            bundle_id=bundle_id,
                            document=document,
                            local_hostnames=self._local_hostnames,
                            ssh_proc_host=ssh_unscoped,
                        )
                        return _return_remote_fg(rfg_u)
                proc_cands = self._filter_by_title_affinity(
                    proc_cands, eff_title, scoped=bool(norm_tty or norm_cwd or stale_hint_fallthrough)
                )
                if proc_cands:
                    return _disambiguate_candidates(proc_cands, eff_title, norm_cwd, self.home)
                if stale_hint_fallthrough or is_real_proc:
                    return None

            if is_real_darwin and len(darwin_win_tab_ttys) > 1:
                scoped_list = self._filter_by_title_affinity(
                    _collect_darwin_scoped_non_tmux_cands(), eff_title, scoped=True
                )
                if scoped_list:
                    return _disambiguate_candidates(scoped_list, eff_title, norm_cwd, self.home)
                return None

            if stale_hint_fallthrough or (is_real_darwin and window_pid > 0):
                return None

            fallback_cands = _scan_nonproc_fallback_sessions(
                self.home,
                rt_dirs,
                window_cwd=norm_cwd,
                check_pid_alive=(self.proc_root is not None),
            )
            fallback_cands = self._filter_by_title_affinity(
                fallback_cands, eff_title, scoped=bool(norm_cwd)
            )
            return _disambiguate_candidates(fallback_cands, eff_title, norm_cwd, self.home)

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
        state.claude_just_compacted = False
        state.generation += 1

    def _consume_parsed_record(self, state: SessionState, obj: Mapping[str, Any]) -> None:
        eff_wsl = False if state.is_remote else _effective_is_wsl(env=self.env)
        if not state.cwd and isinstance(obj.get("cwd"), str) and obj["cwd"].strip():
            state.cwd = _normalize_cwd(obj["cwd"], wsl=eff_wsl)

        if state.cli == "agy":
            cp_summary = _extract_agy_compaction_summary(obj)
            if cp_summary is not None:
                state.clean_turns.clear()
                if cp_summary:
                    state.clean_turns.append(("assistant", cp_summary))
                state.luna_synced_idx = 0
                state.generation += 1
                return
            turn = _clean_agy_record(obj)
            if turn is not None:
                state.clean_turns.append(turn)

        elif state.cli == "claude":
            if not _is_claude_sidechain_or_subagent(obj):
                if obj.get("type") == "system" and obj.get("subtype") == "compact_boundary":
                    state.clean_turns.clear()
                    state.luna_synced_idx = 0
                    state.generation += 1
                    state.claude_just_compacted = True
                    return
                if obj.get("type") == "user" and bool(obj.get("isCompactSummary")):
                    state.clean_turns.clear()
                    state.luna_synced_idx = 0
                    if not state.claude_just_compacted:
                        state.generation += 1
                    state.claude_just_compacted = False
                    summary_txt = _extract_claude_message_text(obj.get("message"), role="user")
                    if summary_txt:
                        state.clean_turns.append(("assistant", summary_txt))
                    return
            turn = _clean_claude_record(obj)
            if turn is not None:
                state.claude_just_compacted = False
                state.clean_turns.append(turn)

        elif state.cli == "codex":
            typ = obj.get("type")
            payload = obj.get("payload")
            if typ == "session_meta" and isinstance(payload, dict) and not state.cwd:
                if isinstance(payload.get("cwd"), str):
                    state.cwd = _normalize_cwd(payload["cwd"], wsl=eff_wsl)
            elif typ == "compacted" and isinstance(payload, dict):
                seed_turns = _extract_codex_compacted_turns(payload)
                state.codex_saw_item_completed = False
                state.codex_item_turns.clear()
                state.codex_resp_turns = list(seed_turns)
                state.clean_turns.clear()
                state.luna_synced_idx = 0
                state.generation += 1
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
        if _effective_is_wsl() and not _safe_realpath(path, wsl=True):
            return

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
        if not cwd or is_wsl_slow_mount_path(cwd, env=self.env):
            return "", []
        now = time.monotonic()
        with self._lock:
            cached = self._git_cache.get(cwd)
            if cached is not None and (now - cached[0]) < self.git_cache_ttl_sec:
                return cached[1], list(cached[2])
        status_txt, hw = read_git_status_summary(cwd, env=self.env)
        with self._lock:
            self._git_cache[cwd] = (time.monotonic(), status_txt, hw)
        return status_txt, list(hw)

    def _get_cached_files(
        self, cache: dict[str, tuple[float, list[str]]], cwd: str, loader: Callable[[str], list[str]]
    ) -> list[str]:
        if not cwd or is_wsl_slow_mount_path(cwd, env=self.env):
            return []
        now = time.monotonic()
        with self._lock:
            cached = cache.get(cwd)
            if cached is not None and (now - cached[0]) < self.repo_files_cache_ttl_sec:
                return list(cached[1])
        files = loader(cwd)
        with self._lock:
            cache[cwd] = (time.monotonic(), files)
        return list(files)

    def _get_repo_files(self, cwd: str) -> list[str]:
        return self._get_cached_files(self._repo_cache, cwd, lambda c: read_repo_core_files(c, env=self.env))

    def _get_shallow_files(self, cwd: str) -> list[str]:
        return self._get_cached_files(
            self._shallow_cache, cwd, lambda c: read_workspace_shallow_files(c, home=self.home, env=self.env)
        )

    def _fallback_workspace_stems(self, cwd: str) -> list[str]:
        files = self._get_repo_files(cwd) or self._get_shallow_files(cwd)
        return _file_stems_for_hotwords(files, limit=12) if files else []

    def active_session_cwds(self) -> list[str]:
        with self._lock:
            return list(
                dict.fromkeys(
                    st.cwd for st in self._states.values() if st.cwd and not st.is_remote
                )
            )

    def build_workspace_hotwords(self, workspace_cwd: str) -> list[str]:
        if not workspace_cwd or is_wsl_slow_mount_path(workspace_cwd, env=self.env):
            return []
        _, git_hw = self._get_git_info(workspace_cwd)
        return list(git_hw[:24]) if git_hw else self._fallback_workspace_stems(workspace_cwd)

    def _build_remote_req_and_key(
        self,
        host_cfg: RemoteHostConfig,
        *,
        effective_title: str,
        cwd_hint: str,
        include_git_info: bool,
        max_chars: int | None = None,
    ) -> tuple[dict[str, Any], tuple[Any, ...]]:
        eff_max_chars = (
            max(1000, int(max_chars)) if max_chars is not None else self.context_max_chars
        )
        clean_title = strip_host_tags_from_title(effective_title or "")
        hint = parse_window_title_hint(clean_title)
        token = hint.session_token if hint is not None else ""
        cli_hint = (hint.cli or "") if hint is not None else (_infer_cli_word_from_title(clean_title) or "")
        explicit_cli = bool(hint.explicit_cli) if hint is not None else False
        spin_free_title = _TUI_SPINNER_RE.sub("", clean_title).strip()

        if token:
            cache_key: tuple[Any, ...] = (host_cfg.name, f"tok:{cli_hint}:{token}", "", "", eff_max_chars)
        elif cwd_hint:
            cache_key = (host_cfg.name, "", cwd_hint, cli_hint, eff_max_chars)
        else:
            cache_key = (host_cfg.name, "", "", spin_free_title, eff_max_chars)

        cached_st = self._find_cached_remote_state(host_cfg.name, hint=hint, cwd_hint=cwd_hint)
        etag = (
            cached_st.remote_etag
            if (cached_st is not None and cached_st.remote_max_chars == eff_max_chars)
            else ""
        )
        skey_hint = f"{cached_st.cli}:{cached_st.session_id}" if cached_st is not None else ""
        include_repo_files = bool(include_git_info)
        if include_repo_files and cached_st is not None and cached_st.cwd:
            with self._lock:
                f_entry = self._remote_files_cache.get((host_cfg.name, cached_st.cwd))
                if f_entry is not None and (time.monotonic() - f_entry[0]) < self.repo_files_cache_ttl_sec:
                    include_repo_files = False

        req_dict: dict[str, Any] = {
            "v": 1,
            "token": token,
            "cli": cli_hint,
            "explicit_cli": explicit_cli,
            "title": spin_free_title[:512],
            "cwd_hint": (cwd_hint or "")[:512],
            "etag": etag,
            "session_key_hint": skey_hint,
            "max_chars": eff_max_chars,
            "include_git": bool(include_git_info),
            "include_repo_files": include_repo_files,
        }
        return req_dict, cache_key

    def _update_remote_files_state_locked(
        self,
        host_name: str,
        state: SessionState,
        *,
        files_included: bool,
        repo_f: list[str],
        shallow_f: list[str],
    ) -> None:
        now = time.monotonic()
        if files_included:
            state.remote_repo_files = repo_f
            state.remote_shallow_files = shallow_f
            if state.cwd:
                self._remote_files_cache[(host_name, state.cwd)] = (now, repo_f, shallow_f)
        elif state.cwd:
            cached_f = self._remote_files_cache.get((host_name, state.cwd))
            if cached_f is not None:
                if (now - cached_f[0]) < self.repo_files_cache_ttl_sec:
                    state.remote_repo_files = list(cached_f[1])
                    state.remote_shallow_files = list(cached_f[2])
                else:
                    self._remote_files_cache.pop((host_name, state.cwd), None)
                    state.remote_repo_files = []
                    state.remote_shallow_files = []

    def _apply_remote_probe_response(
        self,
        host_cfg: RemoteHostConfig,
        resp: Mapping[str, Any],
        *,
        req_max_chars: int | None = None,
    ) -> SessionState | None:
        if not resp.get("ok") or resp.get("pong"):
            return None
        cli = str(resp.get("cli") or "").strip().lower()
        sid = str(resp.get("session_id") or "").strip().lower()
        if not cli or not sid:
            return None
        skey = f"remote:{host_cfg.name}:{cli}:{sid}"
        not_mod = bool(resp.get("not_modified") or resp.get("unchanged"))
        etag = str(resp.get("etag") or "")
        rem_cwd = _normalize_cwd(str(resp.get("cwd") or ""))
        git_status = str(resp.get("git_status") or "")
        git_hw = list(resp.get("git_hotwords") or [])
        repo_f = list(resp.get("repo_files") or [])
        shallow_f = list(resp.get("shallow_files") or [])
        files_inc = bool(resp.get("files_included") or repo_f or shallow_f)
        eff_max_chars = (
            max(1000, int(req_max_chars))
            if req_max_chars is not None
            else self.context_max_chars
        )

        with self._lock:
            state = self._states.get(skey)
            if not_mod:
                if state is None or (etag and state.remote_etag != etag):
                    return None
                if rem_cwd:
                    state.cwd = rem_cwd
                state.remote_git_status = git_status
                state.remote_git_hotwords = git_hw
                self._update_remote_files_state_locked(
                    host_cfg.name,
                    state,
                    files_included=files_inc,
                    repo_f=repo_f,
                    shallow_f=shallow_f,
                )
                return state

            new_turns = [(str(r), str(t)) for r, t in (resp.get("turns") or [])]
            evicted = int(resp.get("evicted_chunks") or 0)
            comp_cnt = int(resp.get("compaction_count") or 0)

            if state is None:
                state = SessionState(
                    session_key=skey,
                    cli=cli,
                    session_id=sid,
                    jsonl_path="",
                    cwd=rem_cwd,
                    clean_turns=new_turns,
                    is_remote=True,
                    host_name=host_cfg.name,
                    remote_etag=etag,
                    remote_max_chars=eff_max_chars,
                    remote_evicted_chunks=evicted,
                    remote_compaction_count=comp_cnt,
                    remote_git_status=git_status,
                    remote_git_hotwords=git_hw,
                )
                self._states[skey] = state
            else:
                old_turns = state.clean_turns
                same_epoch = (
                    comp_cnt == state.remote_compaction_count
                    and evicted == state.remote_evicted_chunks
                    and eff_max_chars == state.remote_max_chars
                )
                if same_epoch and new_turns == old_turns:
                    pass
                elif (
                    same_epoch
                    and len(new_turns) >= len(old_turns)
                    and new_turns[: len(old_turns)] == old_turns
                ):
                    state.clean_turns = new_turns
                else:
                    if (
                        old_turns
                        or comp_cnt != state.remote_compaction_count
                        or evicted != state.remote_evicted_chunks
                        or eff_max_chars != state.remote_max_chars
                    ):
                        state.generation += 1
                    state.luna_synced_idx = 0
                    state.clean_turns = new_turns
                if rem_cwd:
                    state.cwd = rem_cwd
                state.remote_etag = etag
                state.remote_max_chars = eff_max_chars
                state.remote_evicted_chunks = evicted
                state.remote_compaction_count = comp_cnt
                state.remote_git_status = git_status
                state.remote_git_hotwords = git_hw

            self._update_remote_files_state_locked(
                host_cfg.name,
                state,
                files_included=files_inc,
                repo_f=repo_f,
                shallow_f=shallow_f,
            )
            return state

    def _make_remote_result_cb(
        self, req_max_chars: int
    ) -> Callable[[RemoteHostConfig, dict[str, Any]], None]:
        def _cb(hcfg: RemoteHostConfig, r: dict[str, Any]) -> None:
            self._apply_remote_probe_response(hcfg, r, req_max_chars=req_max_chars)

        return _cb

    def prefetch_remote_session(
        self,
        window_title: str = "",
        *,
        window_tty: str = "",
        window_cwd: str = "",
        window_pid: int = 0,
        window_process: str = "",
        bundle_id: str = "",
        document: str = "",
        seq_id: int = 0,
        include_git_info: bool = True,
    ) -> bool:
        """Start non-blocking SSH remote context prefetch during ``start_recording``."""
        if not self.remote_cfg.enabled:
            return False
        match = self.resolve_session(
            window_title,
            window_tty=window_tty,
            window_cwd=window_cwd,
            window_pid=window_pid,
            window_process=window_process,
            bundle_id=bundle_id,
            document=document,
        )
        if match is None or match.origin != "remote" or not match.host_name:
            return False
        host_cfg = self.remote_cfg.lookup_host(match.host_name)
        if host_cfg is None:
            return False
        eff_max = self.context_max_chars
        req_dict, cache_key = self._build_remote_req_and_key(
            host_cfg,
            effective_title=(match.remote_title or window_title),
            cwd_hint=match.cwd,
            include_git_info=include_git_info,
            max_chars=eff_max,
        )
        return self.remote_manager.start_prefetch(
            host_cfg,
            req_dict,
            cache_key=cache_key,
            seq_id=seq_id,
            on_result=self._make_remote_result_cb(eff_max),
        )

    def cancel_remote_prefetch(self, *, seq_id: int | None = None) -> None:
        self.remote_manager.cancel_prefetch(seq_id=seq_id)

    def _sync_remote_match(
        self,
        eff_match: SessionMatch,
        *,
        window_title: str = "",
        include_git_info: bool = True,
        cancel_token: Any = None,
        elapsed_ms: int = 0,
    ) -> SessionSnapshot | None:
        host_cfg = self.remote_cfg.lookup_host(eff_match.host_name)
        if host_cfg is None:
            return None
        synth_title = eff_match.remote_title or window_title
        eff_cwd_hint = eff_match.cwd
        if not eff_match.remote_title and window_title:
            rfg_now = detect_remote_foreground(
                window_title, local_hostnames=self._local_hostnames
            )
            if rfg_now.effective_title:
                synth_title = rfg_now.effective_title
            if rfg_now.cwd_hint:
                eff_cwd_hint = rfg_now.cwd_hint
        elif (
            not synth_title
            and eff_match.cli in ("codex", "claude", "agy")
            and eff_match.session_id
            and not eff_match.session_id.startswith("pending-")
        ):
            synth_title = f"[{eff_match.cli}:#{eff_match.session_id}]"
        eff_max = self.context_max_chars
        req_dict, cache_key = self._build_remote_req_and_key(
            host_cfg,
            effective_title=synth_title,
            cwd_hint=eff_cwd_hint,
            include_git_info=include_git_info,
            max_chars=eff_max,
        )
        cfg_budget_ms = (
            host_cfg.timeout_ms
            if host_cfg.timeout_ms is not None
            else self.remote_cfg.timeout_ms
        )
        rem_budget_ms = max(0, int(cfg_budget_ms) - max(0, int(elapsed_ms)))
        cancel_fn: Callable[[], bool] | None = None
        if cancel_token is not None:
            if hasattr(cancel_token, "is_cancelled"):
                cancel_fn = cancel_token.is_cancelled
            elif hasattr(cancel_token, "is_set"):
                cancel_fn = cancel_token.is_set
            elif callable(cancel_token):
                cancel_fn = cancel_token
        resp = self.remote_manager.fetch_with_budget(
            host_cfg,
            req_dict,
            cache_key=cache_key,
            budget_ms=rem_budget_ms,
            allow_start_if_missing=True,
            allow_half_open=False,
            is_cancelled=cancel_fn,
            on_result=self._make_remote_result_cb(eff_max),
        )
        if resp is None or not resp.get("ok"):
            return None
        state: SessionState | None = None
        if resp.get("_already_applied"):
            skey_done = (
                f"remote:{host_cfg.name}:"
                f"{str(resp.get('cli') or '').strip().lower()}:"
                f"{str(resp.get('session_id') or '').strip().lower()}"
            )
            with self._lock:
                cand_st = self._states.get(skey_done)
                if cand_st is not None and (
                    not resp.get("etag") or cand_st.remote_etag == resp.get("etag")
                ):
                    state = cand_st
        if state is None:
            state = self._apply_remote_probe_response(
                host_cfg, resp, req_max_chars=eff_max
            )
        if state is None:
            return None
        with self._lock:
            turns_tuple = tuple(state.clean_turns)
            luna_idx = state.luna_synced_idx
            gen = state.generation
            git_status = state.remote_git_status if include_git_info else ""
            git_hw = list(state.remote_git_hotwords) if include_git_info else []
            rem_files = list(state.remote_repo_files or state.remote_shallow_files) if include_git_info else []
            resolved_match = SessionMatch(
                cli=state.cli,
                session_id=state.session_id,
                jsonl_path="",
                cwd=state.cwd,
                matched_by=str(resp.get("matched_by") or "remote"),
                origin="remote",
                host_name=host_cfg.name,
            )
        repo_stems = _file_stems_for_hotwords(rem_files, limit=12) if (rem_files and not git_hw) else []
        turn_hw = extract_turn_hotwords(turns_tuple)
        merged_hw = tuple(_dedupe_hotwords_ci([*git_hw[:24], *turn_hw, *repo_stems]))
        return SessionSnapshot(
            match=resolved_match,
            clean_turns=turns_tuple,
            luna_synced_idx=luna_idx,
            generation=gen,
            git_status=git_status,
            hotwords=merged_hw,
            byte_offset=0,
        )

    def _sync_resolved_match(
        self,
        eff_match: SessionMatch,
        *,
        include_git_info: bool = True,
        prefer_match_cwd: bool = False,
    ) -> SessionSnapshot:
        with self._lock:
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
                if eff_match.cwd and (prefer_match_cwd or not state.cwd):
                    state.cwd = eff_match.cwd
                if norm_path != state.jsonl_path:
                    self._reset_state(state, norm_path)

            self._ingest_jsonl_delta(state)

            eff_cwd = (eff_match.cwd if prefer_match_cwd and eff_match.cwd else state.cwd) or eff_match.cwd
            if eff_cwd != eff_match.cwd or state.jsonl_path != eff_match.jsonl_path:
                eff_match = replace(
                    eff_match,
                    jsonl_path=state.jsonl_path,
                    cwd=eff_cwd,
                    jsonl_mtime=_safe_mtime(state.jsonl_path) or eff_match.jsonl_mtime,
                )
            turns_tuple = tuple(state.clean_turns)
            luna_idx = state.luna_synced_idx
            gen = state.generation
            boff = state.byte_offset

        git_status, git_hw = self._get_git_info(eff_cwd) if include_git_info else ("", [])
        repo_stems = self._fallback_workspace_stems(eff_cwd) if (include_git_info and eff_cwd and not git_hw) else []
        turn_hw = extract_turn_hotwords(turns_tuple)
        merged_hw = tuple(_dedupe_hotwords_ci([*git_hw[:24], *turn_hw, *repo_stems]))

        return SessionSnapshot(
            match=eff_match,
            clean_turns=turns_tuple,
            luna_synced_idx=luna_idx,
            generation=gen,
            git_status=git_status,
            hotwords=merged_hw,
            byte_offset=boff,
        )

    def sync_ide_session(
        self,
        match: SessionMatch,
        *,
        include_git_info: bool = True,
    ) -> SessionSnapshot:
        """Incrementally ingest a matched desktop IDE conversation without touching ``sync_session``."""
        return self._sync_resolved_match(match, include_git_info=include_git_info, prefer_match_cwd=True)

    def sync_session(
        self,
        match: SessionMatch | None = None,
        *,
        window_title: str = "",
        window_tty: str = "",
        window_cwd: str = "",
        window_pid: int = 0,
        window_process: str = "",
        bundle_id: str = "",
        document: str = "",
        include_git_info: bool = True,
        cancel_token: Any = None,
    ) -> SessionSnapshot | None:
        """Resolve (if needed) and incrementally ingest new JSONL lines for the active session."""
        t0 = time.monotonic()
        with self._lock:
            eff_match = (
                match
                if match is not None
                else self.resolve_session(
                    window_title,
                    window_tty=window_tty,
                    window_cwd=window_cwd,
                    window_pid=window_pid,
                    window_process=window_process,
                    bundle_id=bundle_id,
                    document=document,
                )
            )
            if eff_match is None:
                return None
        if eff_match.origin == "remote":
            resolve_ms = max(0, int(round((time.monotonic() - t0) * 1000)))
            return self._sync_remote_match(
                eff_match,
                window_title=window_title,
                include_git_info=include_git_info,
                cancel_token=cancel_token,
                elapsed_ms=resolve_ms,
            )
        return self._sync_resolved_match(eff_match, include_git_info=include_git_info)

    def get_refiner_context(
        self,
        session_key: str,
        *,
        max_chars: int = DEFAULT_REFINER_CONTEXT_MAX_CHARS,
        include_git_status: bool = True,
        chunk_chars: int | None = None,
        workspace_cwd: str = "",
    ) -> tuple[list[str], str]:
        """Return ``(dialog_chunks, git_context_text)`` separated for message-boundary prefix caching."""
        with self._lock:
            state = self._states.get(session_key) if session_key else None
            if state is not None and state.is_remote:
                turns_copy = list(state.clean_turns)
                rem_cwd = state.cwd
                rem_git_status = state.remote_git_status if include_git_status else ""
                rem_git_hw = list(state.remote_git_hotwords) if include_git_status else []
                rem_repo_files = list(state.remote_repo_files) if include_git_status else []
                rem_shallow_files = list(state.remote_shallow_files) if include_git_status else []
                dialog_chunks_rem = chunk_turns_for_refiner(
                    turns_copy, max_chars=max_chars, chunk_chars=chunk_chars
                )
                rem_sections: list[str] = []
                if include_git_status:
                    if rem_cwd:
                        rem_sections.append(f"cwd: {rem_cwd}")
                    if rem_git_status:
                        rem_sections.append(f"git_status:\n{rem_git_status}")
                    if rem_git_hw:
                        rem_sections.append("changed_files: " + ", ".join(rem_git_hw[:30]))
                    elif rem_repo_files:
                        rem_sections.append(
                            "repo_files (tracked, unchanged; spelling reference only): "
                            + ", ".join(rem_repo_files[:20])
                        )
                    elif rem_shallow_files:
                        rem_sections.append(
                            "workspace_files (local folder; spelling reference only): "
                            + ", ".join(rem_shallow_files[:20])
                        )
                return dialog_chunks_rem, "\n\n".join(rem_sections)

        eff_ws = (
            ""
            if (not workspace_cwd or is_wsl_slow_mount_path(workspace_cwd, env=self.env))
            else _resolve_safe_local_dir(workspace_cwd, wsl=_effective_is_wsl(None, self.env))
        )
        with self._lock:
            state = self._states.get(session_key) if session_key else None
            if state is None and not eff_ws:
                return [], ""
            turns_copy = list(state.clean_turns) if state is not None else []
            cwd = eff_ws or (state.cwd if state is not None else "")

        git_status, git_hw = (
            self._get_git_info(cwd) if include_git_status else ("", [])
        )
        repo_files = (
            self._get_repo_files(cwd)
            if (include_git_status and cwd and not git_hw)
            else []
        )
        shallow_files = (
            self._get_shallow_files(cwd)
            if (include_git_status and cwd and not git_hw and not repo_files)
            else []
        )
        dialog_chunks = chunk_turns_for_refiner(
            turns_copy, max_chars=max_chars, chunk_chars=chunk_chars
        )
        git_sections: list[str] = []
        if include_git_status:
            if cwd and not is_wsl_slow_mount_path(cwd, env=self.env):
                git_sections.append(f"cwd: {cwd}")
            if git_status:
                git_sections.append(f"git_status:\n{git_status}")
            if git_hw:
                git_sections.append("changed_files: " + ", ".join(git_hw[:30]))
            elif repo_files:
                git_sections.append(
                    "repo_files (tracked, unchanged; spelling reference only): "
                    + ", ".join(repo_files[:20])
                )
            elif shallow_files:
                git_sections.append(
                    "workspace_files (local folder; spelling reference only): "
                    + ", ".join(shallow_files[:20])
                )
        return dialog_chunks, "\n\n".join(git_sections)

    def get_luna_delta(
        self,
        session_key: str,
        *,
        full: bool = False,
        max_chars: int = DEFAULT_REFINER_CONTEXT_MAX_CHARS,
    ) -> tuple[str, int, int]:
        """Return ``(context_delta_text, new_synced_idx, generation)`` for Stage 2 Luna thread."""
        with self._lock:
            state = self._states.get(session_key)
            if state is None:
                return "", 0, 0
            start_idx = 0 if full else min(state.luna_synced_idx, len(state.clean_turns))
            delta_turns = state.clean_turns[start_idx:]
            sections: list[str] = []
            if state.is_remote:
                git_status, git_hw = state.remote_git_status, list(state.remote_git_hotwords)
            else:
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
        mode: str = "agent",
    ) -> AsrContext:
        eff_mode = (mode or "agent").strip().lower() or "agent"
        builtin_tail = () if eff_mode == "general" else BUILTIN_AGENT_HOTWORDS
        if eff_mode == "general" or snapshot is None:
            return AsrContext(
                hotwords=_dedupe_hotwords_ci([*extra_hotwords, *builtin_tail]),
                mode=eff_mode,
            )
        merged_hw = _dedupe_hotwords_ci([*extra_hotwords, *snapshot.hotwords, *builtin_tail])
        return AsrContext(
            hotwords=merged_hw,
            git_status=snapshot.git_status,
            dialog_text=snapshot.format_dialog(max_chars=max_dialog_chars),
            mode=eff_mode,
        )
