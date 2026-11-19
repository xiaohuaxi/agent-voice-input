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
import errno
import fcntl
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
from .app_scope import parse_ide_window_title
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
_WORD_TMUX_RE = re.compile(
    r"(?:^|[\s\[\(\{·|:])tmux(?=$|[\s\]\)\}·|:*])",
    re.IGNORECASE,
)
_TUI_SPINNER_RE = re.compile(r"[\u2733\u2800-\u28ff]")
_CTRL_STRIP_RE = re.compile(r"[\x00-\x1f\x7f]")
_CLAUDE_VER_COMM_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?$")
_PYTHON_COMM_RE = re.compile(r"^python(?:\d+(?:\.\d+)*)?$")
_INTERP_COMMS = frozenset({"node", "nodejs", "bun", "deno"})


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


@dataclass(frozen=True)
class SessionMatch:
    cli: str  # "codex" | "claude" | "agy"
    session_id: str
    jsonl_path: str
    cwd: str = ""
    matched_by: str = ""  # "registry" | "title_uuid" | "title_prefix" | "tmux_registry" | "tmux_pane_title" | "tmux_proc_scan" | "tty_registry" | "tty_ps" | "proc_scan" | "recent_root"
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
    claude_just_compacted: bool = False


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
# Root-session predicates, TTY/CWD normalization & process helpers
# ---------------------------------------------------------------------------

_AGY_WS_CWD_RE = re.compile(
    r"(?:Command Working Directory:\s*|Active Workspaces:\s*\r?\n-\s*)(/[^\r\n<]+)"
)


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


def _normalize_cwd(raw_cwd: str) -> str:
    s = (raw_cwd or "").strip()
    if not s:
        return ""
    if s.startswith("file://"):
        parsed = urllib.parse.urlparse(s)
        s = urllib.parse.unquote(parsed.path or "")
    if len(s) > 1:
        s = s.rstrip("/")
    return s


def _cwd_variants(cwd: str) -> set[str]:
    norm = _normalize_cwd(cwd)
    if not norm:
        return set()
    out = {norm}
    try:
        real = os.path.realpath(norm)
        if len(real) > 1:
            real = real.rstrip("/")
        if real:
            out.add(real)
    except OSError:
        pass
    return out


def _cwd_match_rank(target_cwd: str, cand_cwd: str, home: Path | None = None) -> int:
    """Return 2 on exact directory match, 1 on bounded subdirectory match, 0 otherwise."""
    t_vars = _cwd_variants(target_cwd)
    c_vars = _cwd_variants(cand_cwd)
    if not t_vars or not c_vars:
        return 0
    if t_vars & c_vars:
        return 2
    broad = {"/", "/tmp", "/private/tmp", "/var", "/private/var", "/Users", "/home"}
    if home is not None:
        broad |= _cwd_variants(str(home))
    for tv in t_vars:
        for cv in c_vars:
            if (cv not in broad and tv.startswith(cv + "/")) or (
                tv not in broad and cv.startswith(tv + "/")
            ):
                return 1
    return 0


def encode_claude_project_dir(cwd: str) -> list[str]:
    """Return deduplicated Claude project directory names for logical and real ``cwd``."""
    norm = _normalize_cwd(cwd)
    if not norm:
        return []
    out: list[str] = []
    for candidate in (norm, _normalize_cwd(os.path.realpath(norm))):
        if not candidate:
            continue
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
    cwd = _normalize_cwd(str(payload.get("cwd") or ""))
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
                    last_cwd = _normalize_cwd(obj["cwd"])
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


def _extract_agy_transcript_cwd(jsonl_path: str | Path) -> str:
    try:
        with open(jsonl_path, "rb") as f:
            for _ in range(8):
                raw, exceeded = _read_bounded_line(f)
                if not raw:
                    break
                if exceeded or not raw.strip():
                    continue
                try:
                    obj = json.loads(raw.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if isinstance(obj.get("cwd"), str) and obj["cwd"].strip():
                    return _normalize_cwd(obj["cwd"])
                content = obj.get("content")
                if isinstance(content, str) and (
                    "Working Directory:" in content or "Active Workspaces:" in content
                ):
                    m = _AGY_WS_CWD_RE.search(content)
                    if m:
                        return _normalize_cwd(m.group(1))
    except OSError:
        pass
    return ""


def _split_cmdline(raw_args: str) -> list[str]:
    return (raw_args or "").split()


def _is_script_interpreter_token(tok_low: str) -> bool:
    return bool(tok_low and (tok_low in _INTERP_COMMS or _PYTHON_COMM_RE.match(tok_low)))


def _match_cli_process(comm: str, argv: Sequence[str]) -> tuple[str | None, list[str]]:
    """Identify ``("codex" | "claude" | "agy", effective_argv)`` before headless filtering."""
    args_list = list(argv)
    c_base = os.path.basename((comm or "").replace("\\", "/")).strip()
    c_low = c_base.lower()
    a0_norm = args_list[0].replace("\\", "/") if args_list else ""
    a0_norm_low = a0_norm.lower()
    a0_low = os.path.basename(a0_norm).lstrip("-").lower() if a0_norm else ""

    for tok in (c_low, a0_low):
        if tok == "codex" or tok.startswith(("codex-aarch64-", "codex-x86_64-")):
            return "codex", args_list
        if tok in ("agy", "antigravity", "antigravity-cli"):
            return "agy", args_list
        if tok == "claude":
            return "claude", args_list
    if "/claude/versions/" in a0_norm_low or "/@anthropic-ai/claude-code/" in a0_norm_low:
        return "claude", args_list
    if "/@openai/codex/" in a0_norm_low:
        return "codex", args_list
    if c_base and _CLAUDE_VER_COMM_RE.match(c_base) and "claude" in a0_norm_low:
        return "claude", args_list

    if _is_script_interpreter_token(c_low) or _is_script_interpreter_token(a0_low):
        idx = 1
        limit = min(len(args_list), 12)
        while idx < limit:
            arg = args_list[idx]
            if arg in ("-c", "-m", "-e", "--eval", "-p", "--print", "--"):
                break
            if arg in (
                "-r",
                "--require",
                "--import",
                "--loader",
                "--experimental-loader",
                "-W",
                "-X",
            ):
                idx += 2
                continue
            if arg.startswith("-"):
                idx += 1
                continue
            norm_low = arg.replace("\\", "/").lower()
            base = os.path.basename(norm_low)
            if (
                base in ("claude", "claude.js", "claude.mjs")
                or "/claude/versions/" in norm_low
                or "/@anthropic-ai/claude-code/" in norm_low
            ):
                return "claude", args_list[idx:]
            if base in ("codex", "codex.js", "codex.mjs") or "/@openai/codex/" in norm_low:
                return "codex", args_list[idx:]
            if base in ("agy", "agy.js", "antigravity", "antigravity-cli"):
                return "agy", args_list[idx:]
            # Stop at the first non-option script path so arguments to other programs
            # (e.g. `python3 agent-voice-input session-hook --cli claude`) never false-match.
            break
    return None, args_list


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
                    cwd_by_pid[cur_pid] = _normalize_cwd(path_val)
            elif cur_fd == "":
                files_by_pid.setdefault(cur_pid, []).append(path_val)
                if (
                    path_val.startswith("/")
                    and cur_pid not in cwd_by_pid
                    and not path_val.endswith((".jsonl", ".lock"))
                ):
                    cwd_by_pid[cur_pid] = _normalize_cwd(path_val)
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


def _query_pids_codex_rollout_paths(
    pids: Sequence[int], home: Path, proc_root: Path | None = None
) -> list[str]:
    files_by_pid, _ = _query_pids_open_files_and_cwds(pids, proc_root)
    if not files_by_pid:
        return []
    codex_root_dir = home / ".codex"
    out: list[str] = []
    for targets in files_by_pid.values():
        for t in targets:
            if (
                t.endswith(".jsonl")
                and os.path.basename(t).startswith("rollout-")
                and _is_path_under_dir(t, codex_root_dir)
                and os.path.isfile(t)
            ):
                real_t = os.path.realpath(t)
                if real_t not in out:
                    out.append(real_t)
    return out


def _read_proc_cmdline_args(proc_pid_dir: Path) -> list[str]:
    try:
        raw_cmd = (proc_pid_dir / "cmdline").read_bytes()
    except OSError:
        return []
    return [a.decode("utf-8", errors="ignore") for a in raw_cmd.split(b"\x00") if a]


def _is_headless_cli_invocation(cli_kind: str, args: Sequence[str]) -> bool:
    """Return True when ``cmdline`` belongs to a headless/delegated CLI worker (e.g. -p, exec-server, acp, mcp)."""
    if not args:
        return False
    tail = args[1:]
    if cli_kind in ("agy", "claude"):
        value_flags = {
            "-m", "--model", "--agent", "--agents", "--effort", "--mode",
            "--permission-mode", "--project", "--conversation", "--session-id",
            "--resume", "-r", "--add-dir", "--settings", "--setting-sources",
            "--mcp-config", "--json-schema", "--log-file", "--print-timeout",
            "-i", "--prompt-interactive", "--system-prompt", "--append-system-prompt",
            "--fallback-model", "--max-turns", "--max-budget-usd", "--tools",
            "--allowedTools", "--disallowedTools", "-w", "--worktree",
        }
        skip_next = False
        saw_subcmd = False
        for a in tail:
            if a == "--":
                break
            if a in ("-p", "--print", "--prompt", "--input-format", "--output-format"):
                return True
            if a.startswith(("--prompt=", "--input-format=", "--output-format=")):
                return True
            if skip_next:
                skip_next = False
                if not a.startswith("-"):
                    continue
            if a in value_flags:
                skip_next = True
                continue
            if a.startswith("-"):
                continue
            if not saw_subcmd:
                saw_subcmd = True
                if cli_kind == "agy" and a == "acp":
                    return True
                if cli_kind == "claude" and a == "mcp":
                    return True
        return False
    if cli_kind == "codex":
        skip_next = False
        value_flags = {
            "-m", "--model", "-c", "--config", "-C", "--cd", "-p", "--profile",
            "-s", "--sandbox", "-a", "--ask-for-approval", "-i", "--image",
            "--disable", "--enable", "--add-dir", "-o", "--output-last-message",
            "--output-schema", "--color", "--local-provider", "-w", "--writable-root",
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
            if a in (
                "exec", "e", "exec-server", "app-server", "mcp-server",
                "completion", "debug", "cloud", "apply",
            ):
                return True
            break
    return False


def _classify_interactive_cli(comm: str, argv: Sequence[str]) -> str | None:
    cli_kind, eff_args = _match_cli_process(comm, argv)
    if cli_kind is None or _is_headless_cli_invocation(cli_kind, eff_args):
        return None
    return cli_kind


def _classify_proc_cli(proc_pid_dir: Path) -> str | None:
    return _classify_interactive_cli(
        _read_proc_comm(proc_pid_dir), _read_proc_cmdline_args(proc_pid_dir)
    )


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
    if not held:
        return False, "", 0.0
    agy_cwd = _extract_agy_transcript_cwd(tp) if tp is not None else ""
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


def _read_proc_comm(proc_pid_dir: Path) -> str:
    try:
        return (proc_pid_dir / "comm").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _readlink_clean(path: Path) -> str:
    try:
        target = os.readlink(path)
    except OSError:
        return ""
    if target.endswith(" (deleted)"):
        return ""
    return target


def _read_proc_cwd(proc_pid_dir: Path) -> str:
    return _normalize_cwd(_readlink_clean(proc_pid_dir / "cwd"))


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


def _read_proc_ppid(proc_pid_dir: Path) -> int:
    try:
        stat_txt = (proc_pid_dir / "stat").read_text(encoding="utf-8", errors="replace")
        rpar = stat_txt.rfind(")")
        if rpar != -1:
            fields_after = stat_txt[rpar + 2 :].split()
            if len(fields_after) >= 2:
                return int(fields_after[1])
    except (OSError, ValueError):
        pass
    return 0


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


def _extract_claude_cmdline_sid(proc_pid_dir: Path) -> str:
    args = _read_proc_cmdline_args(proc_pid_dir)
    return _extract_claude_sid_from_argv(args)


def _extract_claude_sid_from_argv(args: Sequence[str]) -> str:
    for i, a in enumerate(args):
        if a == "--":
            break
        if a in ("--session-id", "--resume", "-r") and i + 1 < len(args):
            cand = args[i + 1].strip().lower()
            if _STRICT_UUID_RE.match(cand):
                return cand
        for pfx in ("--session-id=", "--resume="):
            if a.startswith(pfx):
                cand = a[len(pfx) :].strip().lower()
                if _STRICT_UUID_RE.match(cand):
                    return cand
    return ""


def _is_path_under_dir(target_path: str | Path, root_dir: Path) -> bool:
    try:
        real_target = os.path.realpath(target_path)
        real_root = os.path.realpath(root_dir)
    except OSError:
        return False
    return real_target == real_root or real_target.startswith(real_root.rstrip(os.sep) + os.sep)


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
    tp = _resolve_agy_transcript_path(home, cid)
    if tp is not None:
        return str(tp), _safe_mtime(tp)
    tp_str = str(_default_agy_expected_transcript_path(home, cid))
    return tp_str, (
        _safe_mtime(fallback_path) if fallback_path is not None else _safe_mtime(tp_str)
    )


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
    lock_file = home / ".gemini" / "antigravity-cli" / "presence" / f"{cid_low}.lock"
    tp_str, j_mtime = _agy_transcript_and_mtime(home, cid_low, lock_file)
    return SessionMatch(
        cli="agy",
        session_id=cid_low,
        jsonl_path=tp_str,
        cwd=live_cwd or fallback_cwd,
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
    pattern = str(home / ".codex" / "sessions" / "*" / "*" / "*" / file_glob)
    files = sorted(glob.glob(pattern), key=_safe_mtime, reverse=True)
    if limit > 0:
        files = files[:limit]
    names = _load_codex_thread_names(home) if codex_names is None else codex_names
    out: list[SessionMatch] = []
    for cf in files:
        if not os.path.isfile(cf):
            continue
        is_cli, sid, meta_cwd = _read_codex_root_meta(cf)
        eff_sid = sid or fallback_sid
        if not is_cli or not eff_sid or (sid_prefix and not eff_sid.startswith(sid_prefix)):
            continue
        if eff_cwd and _cwd_match_rank(eff_cwd, meta_cwd, home) == 0:
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
    cands: list[SessionMatch] = []
    seen_paths: set[str] = set()
    if eff_cwd:
        for enc in encode_claude_project_dir(eff_cwd):
            proj_dir = home / ".claude" / "projects" / enc
            if not proj_dir.is_dir():
                continue
            for jf in sorted(proj_dir.glob(file_glob), key=_safe_mtime, reverse=True):
                if not jf.is_file():
                    continue
                jf_str = str(jf)
                seen_paths.add(jf_str)
                is_root, sid, meta_cwd = _read_claude_root_meta(
                    jf, allow_empty_or_meta_only=True
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
            glob.glob(str(home / ".claude" / "projects" / "*" / file_glob)),
            key=_safe_mtime,
            reverse=True,
        )
        if limit > 0:
            claude_files = claude_files[:limit]
        for clf in claude_files:
            if clf in seen_paths or not os.path.isfile(clf):
                continue
            is_root, sid, meta_cwd = _read_claude_root_meta(
                clf, allow_empty_or_meta_only=allow_empty_global
            )
            eff_sid = sid or fallback_sid
            if not is_root or not eff_sid or (sid_prefix and not eff_sid.startswith(sid_prefix)):
                continue
            if claimed_claude_sids:
                claimed_owner = claimed_claude_sids.get(eff_sid, 0)
                if claimed_owner not in (0, owner_pid):
                    continue
            if filter_cwd and eff_cwd and _cwd_match_rank(eff_cwd, meta_cwd, home) == 0:
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

    files_by_pid, cwd_by_pid = _query_pids_open_files_and_cwds(
        [pid for pid, _, _, _ in live_proc_entries], proc_root
    )
    codex_root_dir = home / ".codex"
    agy_root_dir = home / ".gemini"
    codex_names = _load_codex_thread_names(home)
    by_key: dict[str, SessionMatch] = {}

    for pid, proc_dir, cli_kind, pts_mtime in live_proc_entries:
        proc_cwd = cwd_by_pid.get(pid, "")
        open_files = files_by_pid.get(pid, ())

        if cli_kind == "codex":
            for target in open_files:
                if not target.endswith(".jsonl"):
                    continue
                base = os.path.basename(target)
                if (
                    not base.startswith("rollout-")
                    or not _is_path_under_dir(target, codex_root_dir)
                    or not os.path.isfile(target)
                ):
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
            for target in open_files:
                if not target.endswith(".lock"):
                    continue
                if "/presence/" not in target.replace("\\", "/") or not _is_path_under_dir(
                    target, agy_root_dir
                ):
                    continue
                cid = Path(target).stem.lower()
                if not cid or not os.path.isfile(target):
                    continue
                tp = _resolve_agy_transcript_path(home, cid)
                if tp is not None and _is_agy_transcript_subagent(tp):
                    continue
                tp_str, j_mtime = _agy_transcript_and_mtime(home, cid, target)
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
            pinned_sid = _extract_claude_cmdline_sid(proc_dir)
            best_pinned: SessionMatch | None = None
            best_unclaimed: SessionMatch | None = None
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
    norm_cwd = _normalize_cwd(window_cwd)
    if norm_cwd:
        cwd_ranked = [(_cwd_match_rank(norm_cwd, c.cwd, home), c) for c in pool]
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


def _iter_trusted_sessions_dirs(runtime_dirs: Sequence[Path]) -> list[tuple[int, Path]]:
    out: list[tuple[int, Path]] = []
    for dir_idx, rt_dir in enumerate(runtime_dirs):
        if _is_trusted_runtime_dir(rt_dir):
            sdir = rt_dir / "sessions"
            if _is_trusted_runtime_dir(sdir):
                out.append((dir_idx, sdir))
    return out


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
        "cwd": _normalize_cwd(cwd),
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

    raw_path = str(rec.get("jsonl_path") or rec.get("transcript_path") or "").strip()
    reg_cwd = _normalize_cwd(str(rec.get("cwd") or ""))
    try:
        updated_at = float(rec.get("updated_at") or 0.0)
    except (TypeError, ValueError):
        updated_at = 0.0

    if expected_cli == "agy":
        valid_agy, live_cwd, _ = _resolve_valid_agy_root_meta(sid, home, proc_root)
        if not valid_agy:
            return None
        tp = _resolve_agy_transcript_path(home, sid)
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
            is_cli, meta_sid, meta_cwd = _read_codex_root_meta(raw_path)
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
                raw_path, allow_empty_or_meta_only=True
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
            cwd=reg_cwd or _normalize_cwd(fallback_cwd),
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
            presence_dir = home / ".gemini" / "antigravity-cli" / "presence"
            if presence_dir.is_dir():
                for lf in presence_dir.glob(f"{prefix}*.lock"):
                    cid = lf.stem.lower()
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
    codex_names = _load_codex_thread_names(home)
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
    if cli_kind == "codex":
        if agent_pid > 0:
            open_real = _query_pid_codex_rollout_paths(agent_pid, home, proc_root)
            if open_real:
                codex_names = _load_codex_thread_names(home)
                pid_cands: list[SessionMatch] = []
                for rp in open_real:
                    is_cli, sid, meta_cwd = _read_codex_root_meta(rp)
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
        presence_dir = home / ".gemini" / "antigravity-cli" / "presence"
        if not presence_dir.is_dir():
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
        for lf in presence_dir.glob("*.lock"):
            cand = _build_agy_match(
                lf.stem, home=home, proc_root=proc_root, matched_by=matched_by
            )
            if cand is None:
                continue
            if not eff_cwd:
                matched_cands.append(cand)
            elif cand.cwd:
                if _cwd_match_rank(eff_cwd, cand.cwd, home) > 0:
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
    codex_names = _load_codex_thread_names(home)
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

    presence_dir = home / ".gemini" / "antigravity-cli" / "presence"
    if presence_dir.is_dir():
        for lf in presence_dir.glob("*.lock"):
            agy_cand = _build_agy_match(
                lf.stem, home=home, proc_root=None, matched_by="recent_root"
            )
            if agy_cand is not None:
                by_key.setdefault(agy_cand.session_key, agy_cand)

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
_CODEX_REC_PLUGINS_RE = re.compile(
    r"<recommended_plugins>.*?</recommended_plugins>", re.DOTALL | re.IGNORECASE
)
_AGY_CHECKPOINT_PREAMBLE_RE = re.compile(
    r"^\s*#\s*Resuming from a compaction\b.*?progress summary below:\s*",
    re.DOTALL | re.IGNORECASE,
)
_AGY_CHECKPOINT_TAIL_RE = re.compile(
    r"\n#\s*(?:Subagents|Conversation Logs)\b.*",
    re.DOTALL,
)


def _extract_agy_compaction_summary(obj: Mapping[str, Any]) -> str | None:
    """Return cleaned summary string when ``obj`` is an ``agy`` compaction ``CHECKPOINT`` record."""
    if obj.get("source") != "SYSTEM" or obj.get("type") != "CHECKPOINT":
        return None
    raw = obj.get("content")
    if not isinstance(raw, str):
        return ""
    cleaned = _AGY_CHECKPOINT_PREAMBLE_RE.sub("", raw)
    cleaned = _AGY_CHECKPOINT_TAIL_RE.sub("", cleaned).strip()
    return cleaned


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


def _is_claude_sidechain_or_subagent(obj: Mapping[str, Any]) -> bool:
    return bool(
        obj.get("isSidechain")
        or obj.get("agentId")
        or obj.get("agent_id")
        or obj.get("entrypoint") == "sdk-cli"
    )


def _extract_claude_message_text(msg: Any, *, role: str) -> str:
    if not isinstance(msg, Mapping):
        return ""
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
        return ""
    return _clean_claude_text("\n".join(raw_parts), role=role)


def _clean_claude_record(obj: Mapping[str, Any]) -> tuple[str, str] | None:
    if (
        _is_claude_sidechain_or_subagent(obj)
        or bool(obj.get("isMeta"))
        or bool(obj.get("isApiErrorMessage"))
    ):
        return None
    typ = obj.get("type")
    if typ not in ("user", "assistant"):
        return None
    cleaned = _extract_claude_message_text(obj.get("message"), role=typ)
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
        s = _CODEX_PERM_CTX_RE.sub("", s)
        s = _CODEX_REC_PLUGINS_RE.sub("", s).strip()
        if not s or s.startswith(("# AGENTS.md instructions", "<recommended_plugins>")):
            continue
        chunks.append(s)
    joined = "\n".join(chunks).strip()
    return (role, joined) if joined else None


def _extract_codex_compacted_turns(payload: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Extract post-compaction summary / retained turns & uncompacted suffix from a Codex ``compacted`` record."""
    retained_turns: list[tuple[str, str]] = []
    rc = payload.get("retained_context")
    if isinstance(rc, Mapping):
        ordered: list[tuple[int, int, str, str]] = []
        seq = 0
        for key, role in (("user_messages", "user"), ("assistant_messages", "assistant")):
            items = rc.get(key)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                txt = item.get("text")
                if not isinstance(txt, str) or not txt.strip():
                    continue
                raw_ord = item.get("order")
                ord_val = int(raw_ord) if isinstance(raw_ord, int) and not isinstance(raw_ord, bool) else (1_000_000 + seq)
                ordered.append((ord_val, seq, role, txt.strip()))
                seq += 1
        ordered.sort(key=lambda t: (t[0], t[1]))
        retained_turns = [(r, txt) for _, _, r, txt in ordered]

    rh_turns: list[tuple[str, str]] = []
    rh = payload.get("replacement_history")
    if isinstance(rh, list):
        for item in rh:
            if isinstance(item, Mapping):
                turn = _extract_codex_response_item(item)
                if turn is not None:
                    rh_turns.append(turn)

    out: list[tuple[str, str]] = []
    raw_msg = payload.get("message")
    summary_txt = raw_msg.strip() if isinstance(raw_msg, str) else ""
    if summary_txt and (not rh_turns or rh_turns[-1][1] != summary_txt):
        out.append(("assistant", summary_txt))

    out.extend(retained_turns)
    seen_retained = set(retained_turns)
    for turn in rh_turns:
        if turn not in seen_retained:
            out.append(turn)
    return out


# ---------------------------------------------------------------------------
# Git status & technical identifier hotword extraction
# ---------------------------------------------------------------------------

_TECH_EXT = (
    r"(?:jsonl|json|pyi|py|sh|ps1|swift|toml|md|tsx|ts|jsx|mjs|js|vue|svelte|scss|css|html|sql|rs|go"
    r"|cpp|cc|c|hpp|h|java|kt|rb|php|yaml|yml|lock|ini|cfg|zsh|bash|env)"
)
_TECH_IDENT_RE = re.compile(
    rf"(?<![A-Za-z0-9_.\-])(?P<file>[A-Za-z0-9_][A-Za-z0-9_.\-]*\.{_TECH_EXT})(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_.\-])(?P<flag>--[a-z][a-z0-9]*(?:-[a-z0-9]+)*)(?![A-Za-z0-9_\-])"
    r"|(?<![A-Za-z0-9_])(?P<snake>_*[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+)(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_])(?P<camel>[a-z][a-z0-9]*(?:[A-Z][a-z0-9]*)+)(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_])(?P<pascal>[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+|[A-Z]{2,}[a-z0-9]{2,}(?:[A-Z][a-z0-9]*)*)(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_.\-])(?P<kebab>[A-Za-z][A-Za-z0-9]*(?:\.[0-9]+)*(?:-[A-Za-z0-9]+(?:\.[0-9]+)*)+)(?![A-Za-z0-9_\-])"
)

_KEBAB_STOP_SEGS = frozenset(
    "a an and as at by for from in into is it non of off on one or out over per re self step the to up well with".split()
)
_KEBAB_STOP_WORDS = frozenset({
    "read-only", "real-time", "short-term", "long-term", "high-level", "low-level",
    "trade-off", "trade-offs", "code-switching", "false-positive", "false-positives",
    "open-source", "full-text", "plain-text", "single-hop", "two-stage", "multi-turn",
    "utf-8", "utf-16", "sha-256", "covid-19",
})
_KEBAB_NUM_PREFIX_STOP = frozenset({
    "top", "utf", "sha", "iso", "rfc", "http", "line", "step", "part", "item", "page", "phase", "wave", "rule",
})
_IDENT_STOP = frozenset({"__init__", "__main__"})
_VER_ONLY_RE = re.compile(r"^[vV]?[0-9]+(?:\.[0-9]+)*(?:-[A-Za-z0-9.]+)?$")
_HASH_HEX_RE = re.compile(r"^[0-9a-fA-F]{7,}(?:-[0-9a-fA-F]{4,})+$")

BUILTIN_AGENT_HOTWORDS: tuple[str, ...] = (
    "Codex", "Claude Code", "Gemini", "Flash-Lite", "Luna", "Astra", "Sol",
    "Opus", "Sonnet", "Haiku", "Antigravity", "agy", "Ghostty", "tmux", "WSL2", "CLI",
    "Qwen", "DeepSeek", "豆包", "Kimi", "Whisper", "Groq", "OpenRouter", "DashScope",
    "subagent", "worktree", "rebase", "JSONL", "TOML", "竞态",
)

REPO_FILES_CACHE_TTL_SEC: float = 300.0
_REPO_SKIP_DIRS = frozenset({
    "node_modules", "vendor", "dist", "build", "out", "target", "third_party",
    "docs", "doc", "examples", "fixtures", "testdata", "test", "tests",
    "__tests__", "spec", "__pycache__", ".github", "migrations",
})
_REPO_SKIP_BASES = frozenset({
    "__init__.py", "__main__.py", "conftest.py", "setup.py",
    "index.ts", "index.js", "index.tsx", "mod.rs", "main.go", "package-lock.json",
})
_REPO_CORE_EXTS = frozenset(
    ".py .ts .tsx .js .vue .svelte .go .rs .swift .sh .ps1 .kt .java .rb .c .cpp .h .toml .sql".split()
)
_REPO_TEST_FILE_RE = re.compile(r"^test_|(?:_test|\.test|\.spec)\.", re.IGNORECASE)
_REPO_PRIORITY_ROOTS = frozenset({"src", "lib", "app", "pkg", "cmd", "internal"})


def _dedupe_hotwords_ci(words: Sequence[str]) -> list[str]:
    out: list[str] = []
    seen_low: set[str] = set()
    for raw in words:
        w = raw.strip()
        if w and (low := w.lower()) not in seen_low:
            seen_low.add(low)
            out.append(w)
    return out


def _keep_tech_token(m: re.Match[str]) -> str | None:
    tok = m.group(0)
    if not (4 <= len(tok) <= 48) or tok in _IDENT_STOP:
        return None
    if m.group("file"):
        stem = tok.rsplit(".", 1)[0]
        return tok if len(stem) >= 2 and any(c.isalpha() for c in stem) else None
    if m.group("kebab"):
        low = tok.lower()
        if len(tok) < 5 or low in _KEBAB_STOP_WORDS or _VER_ONLY_RE.match(tok) or _HASH_HEX_RE.match(tok):
            return None
        segs = low.split("-")
        if _KEBAB_STOP_SEGS.intersection(segs):
            return None
        if len(segs) == 2 and segs[1].isdigit() and segs[0] in _KEBAB_NUM_PREFIX_STOP:
            return None
    return tok


def _detect_host_is_wsl() -> bool:
    if sys.platform != "linux":
        return False
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in os.uname().release.lower()
    except (AttributeError, OSError):
        return False


_HOST_IS_WSL: bool = _detect_host_is_wsl()


def _effective_is_wsl(wsl: bool | None = None, env: Mapping[str, str] | None = None) -> bool:
    if wsl is not None:
        return bool(wsl)
    src = env if env is not None else os.environ
    return _HOST_IS_WSL or bool((src.get("WSL_DISTRO_NAME") or "").strip() or (src.get("WSL_INTEROP") or "").strip())


def is_wsl_slow_mount_path(
    path_str: str, *, wsl: bool | None = None, env: Mapping[str, str] | None = None
) -> bool:
    """Pure-string zero-I/O check blocking WSL2 ``/mnt/`` DrvFS and UNC mounts."""
    if not _effective_is_wsl(wsl, env):
        return False
    s = (path_str or "").strip()
    if not s:
        return False
    if s.lower().startswith("file://"):
        parsed = urllib.parse.urlparse(s)
        if parsed.netloc and parsed.netloc.lower() != "localhost":
            return True
        s = urllib.parse.unquote(parsed.path or "")
    if s.startswith(("\\\\", "//")) or (len(s) >= 2 and s[0].isalpha() and s[1] == ":"):
        return True
    if s.startswith(("/", "\\")):
        norm = posixpath.normpath("/" + s.replace("\\", "/").lstrip("/"))
        return norm == "/mnt" or norm.startswith("/mnt/")
    return False


def _safe_realpath(path_str: str, *, wsl: bool) -> str:
    """Resolve symlinks without ever calling ``lstat``/``stat`` on WSL2 ``/mnt/`` slow mounts."""
    if not path_str or is_wsl_slow_mount_path(path_str, wsl=wsl):
        return ""
    if not wsl:
        try:
            return _normalize_cwd(os.path.realpath(path_str))
        except OSError:
            return ""
    abs_p = path_str if path_str.startswith("/") else os.path.abspath(path_str)
    if is_wsl_slow_mount_path(abs_p, wsl=True):
        return ""
    todo = [seg for seg in abs_p.split("/") if seg and seg != "."]
    resolved: list[str] = []
    hops = 0
    while todo:
        seg = todo.pop(0)
        if seg == "..":
            if resolved:
                resolved.pop()
            continue
        cand = "/" + "/".join([*resolved, seg])
        if is_wsl_slow_mount_path(cand, wsl=True):
            return ""
        try:
            st = os.lstat(cand)
        except OSError:
            tail = posixpath.normpath("/" + "/".join([*resolved, seg, *todo]))
            return "" if is_wsl_slow_mount_path(tail, wsl=True) else _normalize_cwd(tail)
        if stat.S_ISLNK(st.st_mode):
            hops += 1
            if hops > 40:
                return ""
            try:
                target = os.readlink(cand)
            except OSError:
                return ""
            if is_wsl_slow_mount_path(target, wsl=True):
                return ""
            if target.startswith("/"):
                resolved = []
            todo = [x for x in target.split("/") if x and x != "."] + todo
        else:
            resolved.append(seg)
    out = "/" + "/".join(resolved)
    return "" if is_wsl_slow_mount_path(out, wsl=True) else _normalize_cwd(out)


def _resolve_safe_local_dir(path_str: str, *, wsl: bool) -> str:
    """Validate directory without touching ``/mnt/`` before, during, or after symlink resolution."""
    real = _safe_realpath(path_str, wsl=wsl)
    if not real:
        return ""
    try:
        return real if os.path.isdir(real) else ""
    except OSError:
        return ""


_BROAD_SYSTEM_DIRS_LOW: frozenset[str] = frozenset({
    "/", "/tmp", "/private", "/private/tmp", "/var", "/private/var", "/users", "/home", "/root",
    "/private/var/root", "/etc", "/private/etc", "/usr", "/system", "/library", "/applications", "/opt", "/mnt",
})


def _is_broad_or_unsafe_workspace_dir(dir_path: str, home: Path | None = None, *, wsl: bool = False) -> bool:
    norm = posixpath.normpath(_normalize_cwd(dir_path)) if dir_path else ""
    if not norm or norm == "." or norm.lower() in _BROAD_SYSTEM_DIRS_LOW:
        return True
    if posixpath.dirname(norm).lower() in ("/home", "/users"):
        return True
    if home is not None:
        h_norm = posixpath.normpath(_normalize_cwd(str(home)))
        h_real = _safe_realpath(h_norm, wsl=wsl)
        h_set = {p for p in (h_norm, h_real) if p}
        if norm in h_set or norm.lower() in {p.lower() for p in h_set}:
            return True
    return False


def _has_git_marker(dir_path: str) -> bool:
    try:
        return bool(dir_path) and os.path.exists(os.path.join(dir_path, ".git"))
    except OSError:
        return False


def _is_inside_git_worktree(dir_path: str, *, home: Path | None = None, max_depth: int = 12) -> bool:
    cur = _normalize_cwd(dir_path)
    for _ in range(max_depth + 1):
        if not cur:
            return False
        if _has_git_marker(cur):
            return True
        if _is_broad_or_unsafe_workspace_dir(cur, home):
            return False
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            return False
        cur = parent
    return False


_SHALLOW_SKIP_DIRS_LOW: frozenset[str] = frozenset(
    d.lower()
    for d in (
        _REPO_SKIP_DIRS
        | {
            ".git", ".hg", ".svn", ".venv", "venv", "env", ".env", ".tox", ".mypy_cache",
            ".pytest_cache", ".ruff_cache", ".next", ".nuxt", ".turbo", ".cache",
            "coverage", "htmlcov", "Pods", "DerivedData", "Library", "Logs", "Temp", "tmp",
        }
    )
)
_SHALLOW_DOC_AND_CODE_EXTS: frozenset[str] = _REPO_CORE_EXTS | frozenset(
    ".md .markdown .mdx .txt .rst .adoc .org .tex .pdf .doc .docx .xls .xlsx .ppt .pptx "
    ".csv .tsv .json .jsonl .yaml .yml .xml .html .css .scss .ipynb .proto .graphql .prisma".split()
)
_SHALLOW_SKIP_BASES_LOW: frozenset[str] = frozenset(
    b.lower() for b in (_REPO_SKIP_BASES | {".ds_store", "thumbs.db", "desktop.ini"})
)


def _file_stems_for_hotwords(filenames: Sequence[str], *, limit: int = 12) -> list[str]:
    out: list[str] = []
    seen_low: set[str] = set()
    for fname in filenames[:limit]:
        stem = os.path.splitext(fname)[0].strip()
        if not stem or len(stem) > 48 or stem.isdigit():
            continue
        min_len = 2 if any(ord(ch) > 127 for ch in stem) else 3
        if len(stem) >= min_len and (low := stem.lower()) not in seen_low:
            seen_low.add(low)
            out.append(stem)
    return out


def read_workspace_shallow_files(
    cwd: str,
    *,
    home: Path | None = None,
    max_files: int = 20,
    max_entries: int = 200,
    wsl: bool | None = None,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Bounded ``os.scandir`` fallback (root + 1 subdir level) for non-Git folders."""
    eff_wsl = _effective_is_wsl(wsl, env)
    eff_home = home if home is not None else Path(((env or os.environ).get("HOME") or "").strip() or Path.home())
    if _is_broad_or_unsafe_workspace_dir(cwd, eff_home, wsl=eff_wsl):
        return []
    real_cwd = _resolve_safe_local_dir(cwd, wsl=eff_wsl)
    if (
        not real_cwd
        or _is_broad_or_unsafe_workspace_dir(real_cwd, eff_home, wsl=eff_wsl)
        or _is_inside_git_worktree(real_cwd, home=eff_home)
    ):
        return []

    scanned = 0

    def _accept_file_name(name: str) -> bool:
        if not name or name.startswith((".", "~$")) or name.lower() in _SHALLOW_SKIP_BASES_LOW:
            return False
        stem, ext = os.path.splitext(name)
        return ext.lower() in _SHALLOW_DOC_AND_CODE_EXTS and 2 <= len(stem.strip()) <= 64

    def _scan_dir(dir_path: str, *, include_dirs: bool) -> tuple[list[str], list[str]]:
        nonlocal scanned
        files: list[str] = []
        dirs: list[str] = []
        try:
            with os.scandir(dir_path) as it:
                for entry in it:
                    if scanned >= max_entries:
                        break
                    scanned += 1
                    ename = entry.name
                    if not ename or ename.startswith((".", "~$")):
                        continue
                    try:
                        if include_dirs and entry.is_dir(follow_symlinks=False):
                            if ename.lower() not in _SHALLOW_SKIP_DIRS_LOW:
                                dirs.append(entry.path)
                        elif entry.is_file(follow_symlinks=False) and _accept_file_name(ename):
                            files.append(ename)
                    except OSError:
                        continue
        except OSError:
            return [], []
        files.sort(key=str.lower)
        dirs.sort(key=lambda p: os.path.basename(p).lower())
        return files, dirs

    root_files, sub_dirs = _scan_dir(real_cwd, include_dirs=True)
    sub_files: list[str] = []
    for sdir in sub_dirs:
        if scanned >= max_entries or len(root_files) + len(sub_files) >= max_files * 2:
            break
        lvl_files, _ = _scan_dir(sdir, include_dirs=False)
        sub_files.extend(lvl_files)

    out: list[str] = []
    seen_low: set[str] = set()
    for fname in (*root_files, *sub_files):
        if (low := fname.lower()) not in seen_low:
            seen_low.add(low)
            out.append(fname)
            if len(out) >= max_files:
                break
    return out


_IDE_SEARCH_SUBDIRS: tuple[str, ...] = (
    "repos", "projects", "code", "work", "dev", "src", "github", "workspace", "Developer", "Documents", "Desktop",
)
_IDE_HOME_SKIP_NAMES: frozenset[str] = frozenset({
    "library", ".cache", ".local", ".ssh", ".gnupg", ".npm", ".cargo", ".gemini", ".claude", ".codex",
})


def _expand_home_prefix(raw_path: str, home: Path) -> str:
    s = raw_path.strip()
    return str(home) if s == "~" else (str(home / s[2:]) if s.startswith("~/") else s)


def _collect_bounded_ancestors(start_path: str, *, home: Path, wsl: bool, max_levels: int = 12) -> list[str]:
    real = _safe_realpath(start_path, wsl=wsl)
    if not real:
        return []
    cur = _normalize_cwd(real if os.path.isdir(real) else os.path.dirname(real))
    ancestors: list[str] = []
    for _ in range(max_levels + 1):
        if not cur or _is_broad_or_unsafe_workspace_dir(cur, home, wsl=wsl):
            break
        if os.path.isdir(cur):
            ancestors.append(cur)
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            break
        cur = _normalize_cwd(parent)
    return ancestors


def _pick_unique_workspace_dir(candidates: Iterable[str], *, home: Path, wsl: bool) -> str:
    """Deduplicate by ``(st_dev, st_ino)``; accept unique dir, prefer sole ``.git`` on ties, or return ``''``."""
    unique_items: list[tuple[str, bool]] = []
    seen_inodes: set[tuple[int, int]] = set()
    for raw in candidates:
        safe_dir = _resolve_safe_local_dir(raw, wsl=wsl)
        if not safe_dir or _is_broad_or_unsafe_workspace_dir(safe_dir, home, wsl=wsl):
            continue
        try:
            st = os.stat(safe_dir)
        except OSError:
            continue
        fid = (st.st_dev, st.st_ino)
        if fid not in seen_inodes:
            seen_inodes.add(fid)
            unique_items.append((safe_dir, _has_git_marker(safe_dir)))
    if len(unique_items) == 1:
        return unique_items[0][0]
    git_dirs = [p for p, has_git in unique_items if has_git]
    return git_dirs[0] if len(git_dirs) == 1 else ""


def resolve_ide_workspace_cwd(
    win: Any,
    *,
    home: Path,
    known_cwds: Iterable[str] = (),
    wsl: bool | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Resolve local IDE workspace directory (Git repo or ordinary folder) without directory recursion."""
    eff_wsl = _effective_is_wsl(wsl, env)
    title_info = parse_ide_window_title(win)
    if title_info is not None and title_info.remote_kind == "remote":
        return ""

    root_name = title_info.root_name.strip() if title_info is not None else ""
    if root_name in (".", "..") or any(ch in root_name for ch in ("/", "\\", "\x00")):
        root_name = ""
    want_low = root_name.lower()

    # 1. macOS AXDocument file:// URL
    raw_doc = (getattr(win, "document", "") or "").strip()
    if raw_doc.lower().startswith("file://") and not is_wsl_slow_mount_path(raw_doc, wsl=eff_wsl):
        parsed_doc = urllib.parse.urlparse(raw_doc)
        if not parsed_doc.netloc or parsed_doc.netloc.lower() == "localhost":
            ancestors = _collect_bounded_ancestors(urllib.parse.unquote(parsed_doc.path or ""), home=home, wsl=eff_wsl)
            if want_low:
                if hit := next((a for a in ancestors if os.path.basename(a).lower() == want_low), ""):
                    return hit
                # External document outside named workspace (e.g. ~/.zshrc): ignore document and fall through.
            elif hit := next((a for a in ancestors if _has_git_marker(a)), ""):
                return hit

    # 2. Explicit path in IDE title (e.g. JetBrains [~/repos/proj] or Sublime ~/repos/proj/a.py):
    # authoritative for that window — never fall back to guessing same-named local folders if invalid/slow-mount.
    if title_info is not None and title_info.explicit_path:
        exp_expanded = _expand_home_prefix(title_info.explicit_path, home)
        if is_wsl_slow_mount_path(exp_expanded, wsl=eff_wsl):
            return ""
        exp_dir = _resolve_safe_local_dir(exp_expanded, wsl=eff_wsl)
        if exp_dir and not _is_broad_or_unsafe_workspace_dir(exp_dir, home, wsl=eff_wsl) and _has_git_marker(exp_dir):
            return exp_dir
        ancs = _collect_bounded_ancestors(exp_dir or exp_expanded, home=home, wsl=eff_wsl)
        if want_low and (hit := next((a for a in ancs if os.path.basename(a).lower() == want_low), "")):
            return hit
        if hit := next((a for a in ancs if _has_git_marker(a)), ""):
            return hit
        return exp_dir if (exp_dir and not _is_broad_or_unsafe_workspace_dir(exp_dir, home, wsl=eff_wsl)) else ""

    if not want_low:
        return ""

    # 3. Combine known active session CWDs and fixed 1-level candidate roots under unified inode/conflict rules.
    probe_cands = [
        c for c in known_cwds
        if c and not is_wsl_slow_mount_path(c, wsl=eff_wsl) and os.path.basename(c.rstrip("/\\")).lower() == want_low
    ]
    probe_cands.extend(str(home / sub / root_name) for sub in _IDE_SEARCH_SUBDIRS)
    if want_low not in _IDE_HOME_SKIP_NAMES:
        probe_cands.append(str(home / root_name))
    return _pick_unique_workspace_dir(probe_cands, home=home, wsl=eff_wsl)


_AGY_IDE_ACTIVE_WINDOW_SEC: float = 21600.0
_AGY_IDE_MAX_CANDIDATES: int = 16
_AGY_IDE_TAIL_SCAN_BYTES: int = 131072
_AGY_TOOL_PATH_KEYS: frozenset[str] = frozenset({
    "absolutepath", "cwd", "directorypath", "path", "searchdirectory", "targetfile",
    "file_path", "filepath", "directory", "target_directory", "workdir",
})
_AGY_URI_RE = re.compile(r"(?:file|vscode-remote)://[^\s\"'<>)\]`]+", re.IGNORECASE)
_AGY_ABS_PATH_RE = re.compile(
    r"(?:^|[\s\"':=\[({`])(?<!://)(/(?:[A-Za-z0-9._\u0080-\uffff-]+/)+[A-Za-z0-9._\u0080-\uffff-]+)"
)


@dataclass(frozen=True)
class _AgyIdeEvidence:
    ws_uris: tuple[str, ...]
    sys_cwds: tuple[str, ...]
    l2_paths: tuple[str, ...]
    l3_paths: tuple[str, ...]


_AGY_REMOTE_URI_SENTINEL = "::remote_workspace::"
_AGY_BRAIN_EVIDENCE_LOCK = threading.Lock()
_AGY_BRAIN_EVIDENCE_CACHE: OrderedDict[tuple[Any, ...], _AgyIdeEvidence] = OrderedDict()
_AGY_BRAIN_EVIDENCE_MAX = 64


def _decode_agy_uri_or_path(raw: str, home: Path, *, keep_remote_sentinel: bool = False) -> str:
    s = (raw or "").strip().rstrip(".,;:")
    if not s:
        return ""
    low = s.lower()
    if low.startswith("file://"):
        parsed = urllib.parse.urlparse(s)
        if parsed.netloc and parsed.netloc.lower() != "localhost":
            return _AGY_REMOTE_URI_SENTINEL if keep_remote_sentinel else ""
        s = urllib.parse.unquote(parsed.path or "")
    elif low.startswith("vscode-remote://"):
        parsed = urllib.parse.urlparse(s)
        if not (parsed.netloc or "").lower().startswith("wsl+"):
            return _AGY_REMOTE_URI_SENTINEL if keep_remote_sentinel else ""
        s = urllib.parse.unquote(parsed.path or "")
    elif s.startswith("~/"):
        s = str(home / s[2:])
    elif not s.startswith("/"):
        return ""
    s = s.split("#L", 1)[0] if "#L" in s else s
    # Preserve raw '..' segments here so _safe_realpath resolves symlinks before '..' in POSIX order.
    return _normalize_cwd(s) if s.startswith("/") else ""


def _canonical_evidence_path(cand_path: str, *, wsl: bool) -> str:
    """Canonicalize an evidence path safely (resolving symlinks before ``..`` without touching ``/mnt/``)."""
    if cand_path == _AGY_REMOTE_URI_SENTINEL:
        return ""
    return _safe_realpath(cand_path, wsl=wsl)


def _path_is_within_ws(cand_path: str, safe_ws: str, *, wsl: bool) -> bool:
    canon = _canonical_evidence_path(cand_path, wsl=wsl)
    return bool(canon and (canon == safe_ws or canon.startswith(safe_ws + "/")))


def _l1_matches_workspace(safe_ws: str, cand_path: str, home: Path, *, wsl: bool) -> bool:
    canon = _canonical_evidence_path(cand_path, wsl=wsl)
    if not canon:
        return False
    if canon == safe_ws:
        return True
    if not _is_broad_or_unsafe_workspace_dir(canon, home, wsl=wsl) and safe_ws.startswith(canon + "/"):
        return True
    return not _is_broad_or_unsafe_workspace_dir(safe_ws, home, wsl=wsl) and canon.startswith(safe_ws + "/")


def _is_agy_noise_path(cand_path: str, home: Path, *, wsl: bool = False) -> bool:
    if cand_path == _AGY_REMOTE_URI_SENTINEL:
        return False
    norm = posixpath.normpath(_normalize_cwd(cand_path)) if cand_path else ""
    if not norm or norm.lower() in _BROAD_SYSTEM_DIRS_LOW:
        return True
    h_norm = posixpath.normpath(_normalize_cwd(str(home)))
    h_real = _safe_realpath(h_norm, wsl=wsl)
    for h_base in {p for p in (h_norm, h_real) if p}:
        if norm == h_base:
            return True
        if norm.startswith(h_base + "/"):
            for sub in (".gemini", ".claude", ".codex", ".agents", ".harness", ".cache", ".local", "Library"):
                sub_dir = f"{h_base}/{sub}"
                if norm == sub_dir or norm.startswith(sub_dir + "/"):
                    return True
            return norm.startswith(h_base + "/.") and "/" not in norm[len(h_base) + 2 :]
    return norm.lower().startswith(
        ("/tmp/", "/private/", "/var/", "/usr/", "/etc/", "/system/", "/library/", "/applications/")
    )


def _extract_paths_from_metadata_block(meta_text: str, home: Path) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for raw_val in (
        *(m.group(0) for m in _AGY_URI_RE.finditer(meta_text)),
        *(m.group(1) for m in _AGY_ABS_PATH_RE.finditer(_AGY_URI_RE.sub(" ", meta_text))),
    ):
        if (p := _decode_agy_uri_or_path(raw_val, home)) and p not in seen:
            seen.add(p)
            paths.append(p)
    return paths


def _extract_tool_call_paths(obj: Mapping[str, Any], home: Path) -> list[str]:
    raw_calls = obj.get("tool_calls")
    if not isinstance(raw_calls, list):
        return []
    out: list[str] = []
    for tc in raw_calls:
        if not isinstance(tc, Mapping):
            continue
        fn_obj = tc.get("function")
        arg_sources = [tc.get("args"), tc.get("arguments"), tc.get("input"), fn_obj.get("arguments") if isinstance(fn_obj, Mapping) else None]
        for raw_args in arg_sources:
            args_map: Mapping[str, Any] | None = raw_args if isinstance(raw_args, Mapping) else None
            if args_map is None and isinstance(raw_args, str) and raw_args.lstrip().startswith("{"):
                try:
                    parsed = json.loads(raw_args)
                    args_map = parsed if isinstance(parsed, Mapping) else None
                except ValueError:
                    pass
            if args_map is None:
                continue
            for k, v in args_map.items():
                if str(k).strip().lower() not in _AGY_TOOL_PATH_KEYS:
                    continue
                vals = (v,) if isinstance(v, str) else (v[:8] if isinstance(v, list) else ())
                for item in vals:
                    if isinstance(item, str) and (p := _decode_agy_uri_or_path(item, home)):
                        out.append(p)
    return out


def _read_bounded_jsonl_dicts(f: BinaryIO, *, max_lines: int = 0) -> list[dict[str, Any]]:
    objs: list[dict[str, Any]] = []
    seen = 0
    while max_lines <= 0 or seen < max_lines:
        raw_ln, exceeded = _read_bounded_line(f, max_bytes=524288)
        if not raw_ln:
            break
        seen += 1
        if exceeded or not raw_ln.strip():
            continue
        try:
            obj = json.loads(raw_ln.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if isinstance(obj, dict):
            objs.append(obj)
    return objs


def _load_agy_ide_evidence(cid_dir: Path, tp: Path, st: os.stat_result, home: Path) -> _AgyIdeEvidence:
    sub_dir = cid_dir / ".system_generated" / "subagents"
    sub_files: list[Path] = []
    sub_sig: list[tuple[str, int, int]] = []
    if sub_dir.is_dir():
        try:
            for sf in sorted(sub_dir.glob("*.json"))[:16]:
                try:
                    sst = sf.stat()
                except OSError:
                    continue
                sub_files.append(sf)
                sub_sig.append((sf.name, sst.st_mtime_ns, sst.st_size))
        except OSError:
            pass
    cache_key = (str(tp), st.st_mtime_ns, st.st_size, tuple(sub_sig))
    with _AGY_BRAIN_EVIDENCE_LOCK:
        if (cached := _AGY_BRAIN_EVIDENCE_CACHE.get(cache_key)) is not None:
            _AGY_BRAIN_EVIDENCE_CACHE.move_to_end(cache_key)
            return cached

    ws_uris: list[str] = []
    sys_cwds: list[str] = []

    def _add_unique(dst: list[str], raw_u: str, *, keep_remote: bool = False) -> None:
        if (p := _decode_agy_uri_or_path(raw_u, home, keep_remote_sentinel=keep_remote)) and p not in dst:
            dst.append(p)

    for sf, (_, _, sf_size) in zip(sub_files, sub_sig):
        if sf_size > 262144:
            continue
        try:
            sdata = json.loads(sf.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        raw_uris = (sdata.get("workspaceUris") or sdata.get("workspace_uris")) if isinstance(sdata, Mapping) else None
        if isinstance(raw_uris, list):
            for u in raw_uris:
                raw_u = u if isinstance(u, str) else (u.get("uri") or u.get("path") if isinstance(u, Mapping) else "")
                if isinstance(raw_u, str):
                    _add_unique(ws_uris, raw_u, keep_remote=True)

    records: list[dict[str, Any]] = []
    try:
        with open(tp, "rb") as f:
            records = _read_bounded_jsonl_dicts(f, max_lines=32)
            for obj in records[:8]:
                if isinstance(obj.get("cwd"), str) and obj["cwd"].strip():
                    _add_unique(sys_cwds, obj["cwd"])
                # Never extract workspace CWD from USER_EXPLICIT prose (users often paste logs from other repos).
                if obj.get("source") == "SYSTEM":
                    c_str = obj.get("content")
                    if isinstance(c_str, str) and ("Working Directory:" in c_str or "Active Workspaces:" in c_str):
                        if m_ws := _AGY_WS_CWD_RE.search(c_str):
                            _add_unique(sys_cwds, m_ws.group(1))
            head_end = f.tell()
            if st.st_size > head_end:
                seek_to = max(head_end, st.st_size - _AGY_IDE_TAIL_SCAN_BYTES)
                f.seek(seek_to)
                if seek_to > head_end:
                    _read_bounded_line(f, max_bytes=524288)  # Drop partial mid-record prefix
                records.extend(_read_bounded_jsonl_dicts(f))
    except OSError:
        pass

    l2_paths: list[str] = []
    for rec in reversed(records):
        if rec.get("source") == "USER_EXPLICIT" or rec.get("type") == "USER_INPUT":
            content = rec.get("content")
            if isinstance(content, str) and (pos := content.find("<ADDITIONAL_METADATA")) >= 0:
                if extracted := _extract_paths_from_metadata_block(content[pos:], home):
                    l2_paths = extracted
                    break

    l3_paths = [
        p
        for rec in records
        if rec.get("source") == "MODEL" or rec.get("type") == "PLANNER_RESPONSE"
        for p in _extract_tool_call_paths(rec, home)
    ]

    ev = _AgyIdeEvidence(tuple(ws_uris), tuple(sys_cwds), tuple(l2_paths), tuple(l3_paths))
    with _AGY_BRAIN_EVIDENCE_LOCK:
        _AGY_BRAIN_EVIDENCE_CACHE[cache_key] = ev
        _AGY_BRAIN_EVIDENCE_CACHE.move_to_end(cache_key)
        while len(_AGY_BRAIN_EVIDENCE_CACHE) > _AGY_BRAIN_EVIDENCE_MAX:
            _AGY_BRAIN_EVIDENCE_CACHE.popitem(last=False)
    return ev


def _classify_agy_evidence_for_workspace(
    ev: _AgyIdeEvidence, safe_ws: str, home: Path, *, wsl: bool = False
) -> str:
    if not safe_ws:
        return "unknown"

    for tier_paths in (ev.ws_uris, ev.sys_cwds):
        if tier_paths:
            if any(_l1_matches_workspace(safe_ws, p, home, wsl=wsl) for p in tier_paths):
                return "match"
            if any(not _is_agy_noise_path(p, home, wsl=wsl) for p in tier_paths):
                return "other"

    if ev.l2_paths:
        m_in = sum(1 for p in ev.l2_paths if _path_is_within_ws(p, safe_ws, wsl=wsl))
        m_out = sum(
            1 for p in ev.l2_paths
            if not _path_is_within_ws(p, safe_ws, wsl=wsl) and not _is_agy_noise_path(p, home, wsl=wsl)
        )
        if m_in > m_out:
            return "match"
        if m_out > m_in:
            return "other"

    in_ws = sum(1 for p in ev.l3_paths if _path_is_within_ws(p, safe_ws, wsl=wsl))
    out_ws = sum(
        1 for p in ev.l3_paths
        if not _path_is_within_ws(p, safe_ws, wsl=wsl) and not _is_agy_noise_path(p, home, wsl=wsl)
    )
    total = in_ws + out_ws
    if in_ws >= 2 and total > 0 and (in_ws / total) >= 0.6:
        return "match"
    return "other" if (out_ws >= 2 and out_ws > in_ws) else "unknown"


def resolve_antigravity_ide_session(
    workspace_cwd: str,
    *,
    home: Path,
    now_ts: float | None = None,
    wsl: bool | None = None,
    env: Mapping[str, str] | None = None,
) -> SessionMatch | None:
    """Match an active desktop Antigravity IDE ``brain/<cid>`` conversation to ``workspace_cwd``."""
    eff_wsl = _effective_is_wsl(wsl, env)
    safe_ws = _resolve_safe_local_dir(workspace_cwd, wsl=eff_wsl)
    if not safe_ws or _is_broad_or_unsafe_workspace_dir(safe_ws, home, wsl=eff_wsl):
        return None
    eff_now = time.time() if now_ts is None else float(now_ts)

    # Strictly isolate from CLI ~/.gemini/antigravity-cli/brain.
    raw_cands: list[tuple[float, str, Path, Path, os.stat_result]] = []
    seen_cids: set[str] = set()
    for broot in (home / ".gemini" / "antigravity" / "brain", home / ".gemini" / "antigravity-ide" / "brain"):
        real_broot = _resolve_safe_local_dir(str(broot), wsl=eff_wsl)
        if not real_broot:
            continue
        try:
            with os.scandir(real_broot) as bit:
                for entry in bit:
                    cid = entry.name.strip().lower()
                    if not cid or cid.startswith(".") or cid in seen_cids:
                        continue
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    cid_dir = Path(entry.path)
                    logs_dir = cid_dir / ".system_generated" / "logs"
                    tp = logs_dir / "transcript_full.jsonl"
                    if not tp.is_file():
                        tp = logs_dir / "transcript.jsonl"
                        if not tp.is_file():
                            continue
                    try:
                        st = tp.stat()
                    except OSError:
                        continue
                    if st.st_size > 0 and (eff_now - st.st_mtime) <= _AGY_IDE_ACTIVE_WINDOW_SEC:
                        seen_cids.add(cid)
                        raw_cands.append((st.st_mtime, cid, cid_dir, tp, st))
        except OSError:
            continue

    raw_cands.sort(key=lambda item: item[0], reverse=True)
    matched = 0
    for mtime, cid, cid_dir, tp, st in raw_cands[:64]:
        if _is_agy_transcript_subagent(tp):
            continue
        matched += 1
        ev = _load_agy_ide_evidence(cid_dir, tp, st, home)
        if _classify_agy_evidence_for_workspace(ev, safe_ws, home, wsl=eff_wsl) == "match":
            return SessionMatch(
                cli="agy", session_id=cid, jsonl_path=str(tp), cwd=safe_ws, matched_by="ide_brain", jsonl_mtime=mtime
            )
        if matched >= _AGY_IDE_MAX_CANDIDATES:
            break
    return None


def read_repo_core_files(
    cwd: str,
    *,
    timeout_sec: float = 0.8,
    max_files: int = 20,
    wsl: bool | None = None,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Return up to ``max_files`` core tracked basenames when ``git status`` is clean."""
    eff_wsl = _effective_is_wsl(wsl, env)
    real_cwd = _resolve_safe_local_dir(cwd, wsl=eff_wsl)
    if not real_cwd:
        return []
    base = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", real_cwd]
    git_env = {**os.environ, "GIT_NO_LAZY_FETCH": "1"}

    def _run_nul(args: list[str]) -> list[str]:
        try:
            proc = subprocess.run(
                [*base, *args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=timeout_sec,
                check=False,
                env=git_env,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        if proc.returncode != 0:
            return []
        return [seg.decode("utf-8", errors="replace") for seg in proc.stdout[:262144].split(b"\0") if seg]

    def _is_core_source(rel_path: str) -> bool:
        if "\n" in rel_path or "\r" in rel_path:
            return False
        parts = rel_path.strip().split("/")
        base_name = parts[-1] if parts else ""
        if not base_name or base_name in _REPO_SKIP_BASES:
            return False
        _, ext = os.path.splitext(base_name)
        if ext.lower() not in _REPO_CORE_EXTS or _REPO_TEST_FILE_RE.search(base_name):
            return False
        return not _REPO_SKIP_DIRS.intersection(parts[:-1])

    tracked = [
        p.strip()
        for p in _run_nul(["ls-files", "-z", "--", ":(glob)*", ":(glob)*/*", ":(glob)*/*/*", ":(glob)*/*/*/*"])
        if p.strip() and _is_core_source(p)
    ]
    if not tracked:
        return []
    live_set = set(tracked)

    out: list[str] = []
    seen_low: set[str] = set()
    for p in _run_nul(["log", "-z", "-n", "12", "--no-merges", "--no-renames", "--name-only", "--format="]):
        p_clean = p.strip()
        if p_clean in live_set:
            b = os.path.basename(p_clean)
            if b.lower() not in seen_low:
                seen_low.add(b.lower())
                out.append(b)
                if len(out) >= max_files:
                    return out

    tracked.sort(
        key=lambda p: p.count("/") - (1 if p.split("/", 1)[0] in _REPO_PRIORITY_ROOTS else 0)
    )
    for p in tracked:
        b = os.path.basename(p)
        if b.lower() not in seen_low:
            seen_low.add(b.lower())
            out.append(b)
            if len(out) >= max_files:
                break
    return out


def read_git_status_summary(
    cwd: str,
    *,
    timeout_sec: float = 1.5,
    max_lines: int = 40,
    wsl: bool | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[str, list[str]]:
    """Run read-only ``git status --short`` (<= 1.5s timeout) and extract changed filenames."""
    eff_wsl = _effective_is_wsl(wsl, env)
    real_cwd = _resolve_safe_local_dir(cwd, wsl=eff_wsl)
    if not real_cwd:
        return "", []
    cmd = [
        "git",
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "-C",
        real_cwd,
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
    turns: Sequence[tuple[str, str]], *, max_words: int = 24
) -> list[str]:
    """Extract recent technical identifiers, flags, model slugs, and filenames from clean turns."""
    found: list[str] = []
    seen_low: set[str] = set()
    for _, text in reversed(turns[-12:]):
        for m in _TECH_IDENT_RE.finditer(text):
            tok = _keep_tech_token(m)
            if tok and (low := tok.lower()) not in seen_low:
                seen_low.add(low)
                found.append(tok)
                if len(found) >= max_words:
                    return found
    return found


DEFAULT_REFINER_CONTEXT_MAX_CHARS = 200000
DEFAULT_REFINER_CHUNK_CHARS = DEFAULT_REFINER_CONTEXT_MAX_CHARS // 4


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
        self._tmux_socket = str(tmux_socket).strip() if tmux_socket is not None else ""
        self._tmux_socket_name = (tmux_socket_name or "").strip()
        self._tmux_probe_fn = tmux_probe_fn
        self._lock = threading.RLock()
        self._states: dict[str, SessionState] = {}
        self._git_cache: dict[str, tuple[float, str, list[str]]] = {}
        self._repo_cache: dict[str, tuple[float, list[str]]] = {}
        self._shallow_cache: dict[str, tuple[float, list[str]]] = {}

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

        def _enrich_from_pane(cand: SessionMatch, matched_by: str) -> SessionMatch:
            proc_hit = next(
                (c for c in pane_proc_cands if c.session_key == cand.session_key), None
            )
            eff_cwd = _normalize_cwd(
                cand.cwd
                or (proc_hit.cwd if proc_hit else "")
                or active_pane.current_path
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
            pane_cwd = _normalize_cwd(active_pane.current_path)
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

        codex_root = self.home / ".codex"
        agy_root = self.home / ".gemini"
        codex_names = _load_codex_thread_names(self.home)
        by_key: dict[str, SessionMatch] = {}

        for pid, row_tty, cli_k in bounded_rows:
            pts_mtime = _safe_mtime(row_tty)
            proc_cwd = cwd_by_pid.get(pid, "")
            open_files = files_by_pid.get(pid, ())

            if cli_k == "codex":
                for target in open_files:
                    if (
                        not target.endswith(".jsonl")
                        or not os.path.basename(target).startswith("rollout-")
                        or not _is_path_under_dir(target, codex_root)
                        or not os.path.isfile(target)
                    ):
                        continue
                    is_cli, sid, meta_cwd = _read_codex_root_meta(target)
                    if not is_cli or not sid:
                        continue
                    cand = SessionMatch(
                        cli="codex",
                        session_id=sid,
                        jsonl_path=os.path.realpath(target),
                        cwd=meta_cwd or proc_cwd,
                        matched_by="tty_ps",
                        pts_mtime=pts_mtime,
                        jsonl_mtime=_safe_mtime(target),
                        thread_name=codex_names.get(sid, ""),
                    )
                    by_key[cand.session_key] = cand

            elif cli_k == "agy":
                for target in open_files:
                    if (
                        not target.endswith(".lock")
                        or "/presence/" not in target.replace("\\", "/")
                        or not _is_path_under_dir(target, agy_root)
                    ):
                        continue
                    cid = Path(target).stem.lower()
                    if not cid or not os.path.isfile(target):
                        continue
                    tp = _resolve_agy_transcript_path(self.home, cid)
                    if tp is not None and _is_agy_transcript_subagent(tp):
                        continue
                    tp_str, j_mtime = _agy_transcript_and_mtime(self.home, cid, target)
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
                    open_rollouts = _query_pids_codex_rollout_paths(
                        [pid for pid, cli_k in tty_agents if cli_k == "codex"],
                        self.home,
                        None,
                    )
                    if open_rollouts and os.path.realpath(cand.jsonl_path) not in open_rollouts:
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
        if cand.cli == "codex" and cand.jsonl_path:
            open_rollouts = _query_pids_codex_rollout_paths(matching_pids, self.home, None)
            return os.path.realpath(cand.jsonl_path) in open_rollouts
        if cand.cli == "agy" and cand.session_id:
            agy_root = self.home / ".gemini"
            open_by_pid, _ = _query_pids_open_files_and_cwds(matching_pids, None)
            want_stem = cand.session_id.lower()
            return any(
                target.endswith(".lock")
                and "/presence/" in target.replace("\\", "/")
                and _is_path_under_dir(target, agy_root)
                and Path(target).stem.lower() == want_stem
                for files in open_by_pid.values()
                for target in files
            )
        if cand.cli == "claude" and cand.cwd:
            _, cwd_by_pid = _query_pids_open_files_and_cwds(matching_pids, None)
            return any(
                pcwd and _cwd_match_rank(pcwd, cand.cwd, self.home) > 0
                for pcwd in cwd_by_pid.values()
            )
        return True

    def resolve_session(
        self,
        window_title: str = "",
        *,
        window_tty: str = "",
        window_cwd: str = "",
        window_pid: int = 0,
    ) -> SessionMatch | None:
        """Resolve foreground main CLI session via title O(1) lookup, tmux active pane, split TTY/CWD, or fallback."""
        with self._lock:
            rt_dirs = self._candidate_runtime_dirs()
            norm_tty = normalize_tty_path(window_tty)
            norm_cwd = _normalize_cwd(window_cwd)
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
            neutralize_outer_title = False

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
                        (
                            norm_tty
                            and any(
                                c.client_tty and _tty_matches(c.client_tty, norm_tty)
                                for c in tmux_snap.clients
                            )
                        )
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
                    elif is_real_proc and not any(
                        c.session_key == disk_hit.session_key for c in all_proc_cands
                    ):
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
                if has_proc and self.proc_root is not None:
                    if not outer_hint_owned_by_tmux and not tty_matches_tmux_client:
                        non_tmux_cands = scan_proc_root_sessions(
                            self.home,
                            self.proc_root,
                            excluded_pids=all_tmux_pids,
                            claimed_claude_sids=claimed_claude,
                        )
                        if non_tmux_cands and self._should_prefer_non_tmux(
                            eff_title, active_client, active_pane, non_tmux_cands
                        ):
                            return _disambiguate_candidates(
                                non_tmux_cands, eff_title, norm_cwd, self.home
                            )
                    return self._resolve_tmux_active_pane(
                        active_pane,
                        rt_dirs,
                        window_title=eff_title,
                        claimed_claude=claimed_claude,
                    )

                pane_hit, pane_authoritative_empty = self._resolve_tmux_active_pane_detailed(
                    active_pane,
                    rt_dirs,
                    window_title=eff_title,
                    claimed_claude=claimed_claude,
                )
                if outer_hint_owned_by_tmux or tty_matches_tmux_client:
                    return pane_hit
                if is_real_darwin:
                    non_tmux_cands = _collect_darwin_scoped_non_tmux_cands()
                    if non_tmux_cands and self._should_prefer_non_tmux(
                        eff_title, active_client, active_pane, non_tmux_cands
                    ):
                        return _disambiguate_candidates(
                            non_tmux_cands, eff_title, norm_cwd, self.home
                        )
                    return pane_hit
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
                non_tmux_cands = [
                    c
                    for c in all_fallback
                    if c.session_key not in tmux_bound_keys
                    and c.session_id not in tmux_pane_uuids
                ]
                if non_tmux_cands and self._should_prefer_non_tmux(
                    eff_title, active_client, active_pane, non_tmux_cands
                ):
                    return _disambiguate_candidates(
                        non_tmux_cands, eff_title, norm_cwd, self.home
                    )
                if (
                    pane_hit is not None
                    or pane_authoritative_empty
                    or (
                        _WORD_TMUX_RE.search(eff_title)
                        and not _TUI_SPINNER_RE.search(eff_title)
                    )
                ):
                    return pane_hit
                return _disambiguate_candidates(
                    non_tmux_cands or all_fallback, eff_title, norm_cwd, self.home
                )

            if has_proc and self.proc_root is not None:
                proc_cands = scan_proc_root_sessions(
                    self.home,
                    self.proc_root,
                    excluded_pids=all_tmux_pids or None,
                    claimed_claude_sids=claimed_claude,
                )
                if proc_cands:
                    return _disambiguate_candidates(proc_cands, eff_title, norm_cwd, self.home)
                if stale_hint_fallthrough or is_real_proc:
                    return None

            if is_real_darwin and len(darwin_win_tab_ttys) > 1:
                scoped_list = _collect_darwin_scoped_non_tmux_cands()
                if scoped_list:
                    return _disambiguate_candidates(scoped_list, eff_title, norm_cwd, self.home)
                return None

            if stale_hint_fallthrough and self.proc_root is not None:
                return None

            fallback_cands = _scan_nonproc_fallback_sessions(
                self.home,
                rt_dirs,
                window_cwd=norm_cwd,
                check_pid_alive=(self.proc_root is not None),
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
        if not state.cwd and isinstance(obj.get("cwd"), str) and obj["cwd"].strip():
            state.cwd = _normalize_cwd(obj["cwd"])

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
                    state.cwd = _normalize_cwd(payload["cwd"])
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
            return list(dict.fromkeys(st.cwd for st in self._states.values() if st.cwd))

    def build_workspace_hotwords(self, workspace_cwd: str) -> list[str]:
        if not workspace_cwd or is_wsl_slow_mount_path(workspace_cwd, env=self.env):
            return []
        _, git_hw = self._get_git_info(workspace_cwd)
        return list(git_hw[:24]) if git_hw else self._fallback_workspace_stems(workspace_cwd)

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
        include_git_info: bool = True,
    ) -> SessionSnapshot | None:
        """Resolve (if needed) and incrementally ingest new JSONL lines for the active session."""
        with self._lock:
            eff_match = (
                match
                if match is not None
                else self.resolve_session(
                    window_title,
                    window_tty=window_tty,
                    window_cwd=window_cwd,
                    window_pid=window_pid,
                )
            )
            if eff_match is None:
                return None
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
