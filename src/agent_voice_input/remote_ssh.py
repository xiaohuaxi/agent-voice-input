"""Zero-install SSH remote Linux transport, whitelist enforcement, circuit breaker, and async prefetch."""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import errno
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Collection, Iterable, Mapping, Sequence
import zlib

from . import _remote_probe
from .app_scope import is_dedicated_ssh_client_window, is_remote_ide_window
from .config import (
    Config,
    RemoteConfig,
    RemoteHostConfig,
    ensure_secure_dir,
    expand_user_path,
    is_hermetic_env,
    redact_secrets,
)

MAX_COMPRESSED_STDOUT_BYTES = 2 * 1024 * 1024
MAX_DECOMPRESSED_STDOUT_BYTES = 2 * 1024 * 1024
MAX_STDERR_BYTES = 4096
PREFETCH_FRESH_TTL_SEC = 15.0
PREFETCH_HARD_TIMEOUT_SEC = 2.5

BREAKER_CONN_THRESHOLD = 2
BREAKER_CONN_COOLDOWN_SEC = 60.0
BREAKER_AUTH_COOLDOWN_SEC = 300.0

WSL_SYSTEM32_OPENSSH_Candidates: tuple[str, ...] = (
    "/mnt/c/Windows/System32/OpenSSH/ssh.exe",
    "/mnt/c/Program Files/OpenSSH/ssh.exe",
)

_SSH_CLIENT_BINS = frozenset({"ssh", "ssh.exe", "autossh", "mosh-client", "slogin", "et"})
_NON_INTERACTIVE_SSH_CMDS = frozenset(
    {"git-upload-pack", "git-receive-pack", "git-upload-archive", "sftp-server", "scp"}
)
# OpenSSH flags that consume a separate argument when not attached (e.g. `-p 2222`)
_SSH_ARG_FLAGS = frozenset(list("BbcDEeFIiJLlmOopQRSWw"))
# Eternal Terminal (`et`) CLI flags (short/long value-taking options & local-only flags)
# Note: `-t` requests a PTY (no argument); tunnels use `-L`/`--tunnel` and `-r`/`-R`/`--reversetunnel`.
_ET_SHORT_ARG_FLAGS = frozenset(list("upcrLlojR"))
_ET_LONG_ARG_FLAGS = frozenset(
    {
        "--username",
        "--port",
        "--command",
        "--initial-dir",
        "--tunnel",
        "--reversetunnel",
        "--jport",
        "--jumphost",
        "--logdir",
        "--ssh-option",
        "--ssh-config",
        "--serverfifo",
        "--host",
        "--prefix",
        "--cfgfile",
        "--terminal-path",
        "--name",
        "--attach",
    }
)
_ET_NON_INTERACTIVE_FLAGS = frozenset({"--help", "--version", "--list", "--kill", "--no-terminal"})

_HOST_TAG_RE = re.compile(r"\[@([A-Za-z0-9_][A-Za-z0-9._-]{0,63})\](?=$|[^0-9A-Za-z_])")
_RAW_BRACKET_AT_RE = re.compile(r"\[@([^\]]{1,128})\]")
_SSH_IDE_HOST_RE = re.compile(r"\[\s*SSH\s*:\s*([A-Za-z0-9_][A-Za-z0-9._-]{0,127})\s*\]", re.IGNORECASE)
_TAB_PREFIX_RE = re.compile(r"^(?:\[\d+/\d+\]\s+|\d+:\d+(?:\.\d+)?\*?\s+|[\u25cf\u2022*]\s+)+")
_TERM_DIM_TAIL_RE = re.compile(r"\s*(?:[—–-]|\s)\s*\d+\s*[×x]\s*\d+\s*$")
_TRAILING_TAGS_RE = re.compile(r"(?:\s*\[(?:#|§|@|(?:agy|gemini|claude|codex):)[^\]]*\])+\s*$", re.IGNORECASE)

# Standard prompt: `user@host: ~/path` or `user@host:/srv/app`
_USER_HOST_COLON_RE = re.compile(
    r"^(?P<user>[A-Za-z0-9._+-]+)@(?P<host>[A-Za-z0-9_][A-Za-z0-9._-]{0,127})\s*:\s*(?P<cwd>~(?:[/\\].*)?|/.*|[A-Za-z]:[/\\].*)$"
)
# RHEL/CentOS prompt: `[user@host dir]$` or `[user@host ~/dir]#`
_BRACKET_PROMPT_RE = re.compile(
    r"^\[(?P<user>[A-Za-z0-9._+-]+)@(?P<host>[A-Za-z0-9_][A-Za-z0-9._-]{0,127})\s+(?P<cwd>[^\]]+)\][$#%]?\s*$"
)
# Dedicated client title: `ubuntu@devbox - PuTTY` or `devbox - PuTTY`
_PUTTY_STYLE_TITLE_RE = re.compile(
    r"^(?:[A-Za-z0-9._+-]+@)?(?P<host>[A-Za-z0-9_][A-Za-z0-9._-]{0,127})\s+[-—–]\s+"
    r"(?:PuTTY|KiTTY|MobaXterm|WindTerm|SecureCRT|Xshell|Termius)\b",
    re.IGNORECASE,
)
# Explicit ssh command in title: `ssh devbox`, `ssh -p 2222 user@devbox`, `et user@devbox`, `C:\Program Files\OpenSSH\ssh.exe user@devbox`
_SSH_CMD_TITLE_RE = re.compile(
    r'^(?:Administrator:\s+)?(?P<q>")?(?P<prefix>(?=[A-Za-z]:[/\\])[A-Za-z]:[/\\](?:[^/\\:*?"<>|\r\n]+[/\\])*|/?(?:[^\s/\\"]+[/\\])*)'
    r'(?P<bin>ssh(?:\.exe)?|autossh|mosh(?:-client)?|slogin|et)(?(q)")(?P<rest>(?:\s+.*)?)$',
    re.IGNORECASE,
)
_WIN_DRIVE_SSH_PATH_RE = re.compile(r"^([A-Za-z]):[/\\](.*)$")
# Hook/TUI middle-dot cwd: `claude · ~/repos/foo [#id8] [@devbox]`
_MIDDOT_CWD_RE = re.compile(r"·\s*(?P<cwd>~(?:/[^\r\n\[]*)?|/[^\r\n\[]*)")

_RAW_PROBE_SRC: bytes = Path(__file__).resolve().with_name("_remote_probe.py").read_bytes()
_COMPRESSED_PROBE_B64_ASCII: str = base64.b64encode(zlib.compress(_RAW_PROBE_SRC, 9)).decode("ascii")


class _WindowAdapter:
    __slots__ = ("process", "bundle_id", "title", "document")

    def __init__(self, process: str, bundle_id: str, title: str, document: str = "") -> None:
        self.process = process
        self.bundle_id = bundle_id
        self.title = title
        self.document = document


@dataclass(frozen=True)
class RemoteForegroundInfo:
    is_remote: bool
    host_candidate: str = ""
    cwd_hint: str = ""
    effective_title: str = ""
    ambiguous_host: bool = False
    ide_remote_only: bool = False
    reason: str = ""


@dataclass
class _HostBreakerState:
    consecutive_failures: int = 0
    cooldown_until: float = 0.0
    tripped: bool = False
    half_open_in_flight: bool = False
    last_error: str = ""
    last_latency_ms: int = 0


@dataclass
class _PrefetchJob:
    host_name: str
    cache_key: tuple[Any, ...]
    seq_id: int
    started_mono: float
    gen: int = 0
    owns_half_open: bool = False
    applied: bool = False
    done_evt: threading.Event = field(default_factory=threading.Event)
    abort_evt: threading.Event = field(default_factory=threading.Event)
    completed_mono: float = 0.0
    response: dict[str, Any] | None = None
    thread: threading.Thread | None = None


def collect_local_hostnames(
    env: Mapping[str, str] | None = None,
    *,
    extra: Iterable[str] | None = None,
    include_synthetic_defaults: bool = False,
    include_mac_placeholder: bool = False,
    configured_remote_names: Collection[str] = (),
) -> frozenset[str]:
    """Build lowercase set of hostnames belonging to the local machine."""
    names: set[str] = {"localhost", "localhost.localdomain", "127.0.0.1", "::1"}
    src_env = os.environ if env is None else env
    raw_candidates: list[str] = []
    if hasattr(os, "uname"):
        try:
            raw_candidates.append(os.uname().nodename)
        except OSError:
            pass
    try:
        raw_candidates.append(socket.gethostname())
    except OSError:
        pass
    for k in ("HOSTNAME", "COMPUTERNAME"):
        val = (src_env.get(k) or "").strip()
        if val:
            raw_candidates.append(val)
    if extra is not None:
        raw_candidates.extend(extra)

    for raw in raw_candidates:
        clean = (raw or "").strip().lower().rstrip(".")
        if clean:
            names.add(clean)
            short = clean.split(".", 1)[0]
            if short:
                names.add(short)
            if clean.endswith(".local"):
                names.add(clean[:-6])

    remote_low = {r.strip().lower() for r in configured_remote_names if r}
    if include_synthetic_defaults:
        if "host" not in remote_low:
            names.add("host")
        if include_mac_placeholder and "mac" not in remote_low:
            names.add("mac")
    return frozenset(names - remote_low)


def parse_window_host_tag(window_title: str) -> tuple[str | None, bool, bool]:
    """Extract ``[@<host_tag>]`` from ``window_title``.

    Returns ``(host_tag_low, is_ambiguous, has_malformed_bracket_at)``.
    """
    if not window_title or "[@" not in window_title:
        return None, False, False
    valid_tags: list[str] = []
    for m in _HOST_TAG_RE.finditer(window_title):
        tag = m.group(1).strip().lower().rstrip(".")
        if tag and tag not in valid_tags:
            valid_tags.append(tag)
    raw_all = [m.group(1).strip() for m in _RAW_BRACKET_AT_RE.finditer(window_title)]
    if raw_all and not valid_tags:
        return None, False, True
    if any(not _HOST_TAG_RE.fullmatch(f"[@{r}]") for r in raw_all):
        return None, True, True
    if len(valid_tags) > 1:
        return None, True, False
    return (valid_tags[0] if valid_tags else None), False, False


def strip_host_tags_from_title(window_title: str) -> str:
    if not window_title or "[@" not in window_title:
        return window_title or ""
    return re.sub(r"\s*\[@[^\]]*\]", "", window_title).strip()


def _extract_et_target_from_argv(argv: Sequence[str]) -> tuple[str, bool]:
    """Parse Eternal Terminal (``et``) ``argv``; return ``(host_only_low, is_interactive_session)``."""
    i = 1
    n = len(argv)
    dest = ""
    host_from_flag = ""
    remote_args: list[str] = []
    saw_batch_yes = False
    saw_no_term = False

    while i < n:
        tok = argv[i]
        if tok == "--":
            if i + 1 < n:
                dest = argv[i + 1]
                remote_args = list(argv[i + 2 :])
            break
        if tok.startswith("--") and len(tok) > 2:
            if "=" in tok:
                opt_name, opt_val = tok.split("=", 1)
                opt_low = opt_name.lower()
                if opt_low in _ET_NON_INTERACTIVE_FLAGS:
                    return "", False
                if opt_low == "--host" and opt_val.strip():
                    host_from_flag = opt_val.strip()
                elif opt_low == "--ssh-option" and opt_val.strip().lower() == "batchmode=yes":
                    saw_batch_yes = True
                i += 1
                continue
            opt_low = tok.lower()
            if opt_low in _ET_NON_INTERACTIVE_FLAGS:
                return "", False
            if opt_low in _ET_LONG_ARG_FLAGS:
                if i + 1 < n:
                    val = argv[i + 1].strip()
                    if opt_low == "--host" and val:
                        host_from_flag = val
                    elif opt_low == "--ssh-option" and val.lower() == "batchmode=yes":
                        saw_batch_yes = True
                i += 2
                continue
            i += 1
            continue
        if tok.startswith("-") and len(tok) > 1:
            if tok in ("-h", "-?"):
                return "", False
            if tok == "-o" and i + 1 < n:
                if argv[i + 1].strip().lower() == "batchmode=yes":
                    saw_batch_yes = True
                i += 2
                continue
            if tok.startswith("-o"):
                if tok[2:].strip().lower() == "batchmode=yes":
                    saw_batch_yes = True
                i += 1
                continue
            flags_str = tok[1:]
            consumed_next = False
            for idx_f, ch in enumerate(flags_str):
                if ch == "h":
                    return "", False
                if ch == "N":
                    saw_no_term = True
                if ch in _ET_SHORT_ARG_FLAGS:
                    if idx_f == len(flags_str) - 1:
                        i += 2
                        consumed_next = True
                    break
            if not consumed_next:
                i += 1
            continue
        dest = tok
        remote_args = list(argv[i + 1 :])
        break

    if saw_batch_yes or saw_no_term:
        return "", False
    if remote_args:
        first_rcmd = os.path.basename(remote_args[0].strip("'\"")).lower()
        if first_rcmd in _NON_INTERACTIVE_SSH_CMDS or (
            first_rcmd.startswith("python") and len(remote_args) >= 2 and remote_args[1] == "-"
        ):
            return "", False
    clean_dest = (dest or host_from_flag or "").strip()
    if "@" in clean_dest:
        clean_dest = clean_dest.rsplit("@", 1)[-1]
    if clean_dest.count(":") == 1:
        host_part, port_part = clean_dest.rsplit(":", 1)
        if host_part and port_part.isdigit():
            clean_dest = host_part
    clean_dest = clean_dest.strip().lower().rstrip(".")
    if not clean_dest or clean_dest.startswith("-"):
        return "", True
    return clean_dest, True


def extract_ssh_target_from_argv(argv: Sequence[str]) -> tuple[str, bool]:
    """Parse ``ssh`` command ``argv``; return ``(host_only_low, is_interactive_session)``.

    Returns ``("", False)`` for background git/scp/BatchMode helper invocations.
    """
    if not argv:
        return "", False
    a0 = os.path.basename(argv[0].replace("\\", "/")).lstrip("-").lower()
    if a0 == "et":
        return _extract_et_target_from_argv(argv)
    if a0 not in _SSH_CLIENT_BINS and not a0.startswith("ssh"):
        return "", False

    i = 1
    n = len(argv)
    dest = ""
    remote_args: list[str] = []
    saw_batch_yes = False
    saw_no_cmd = False

    while i < n:
        tok = argv[i]
        if not dest:
            if tok == "--":
                if i + 1 < n:
                    dest = argv[i + 1]
                    remote_args = list(argv[i + 2 :])
                break
            if tok.startswith("-") and len(tok) > 1:
                if tok == "-o" and i + 1 < n:
                    opt_v = argv[i + 1].strip().lower()
                    if opt_v == "batchmode=yes":
                        saw_batch_yes = True
                    i += 2
                    continue
                if tok.startswith("-o"):
                    if tok[2:].strip().lower() == "batchmode=yes":
                        saw_batch_yes = True
                    i += 1
                    continue
                # Combined or single short flags
                flags_str = tok[1:]
                consumed_next = False
                for idx_f, ch in enumerate(flags_str):
                    if ch == "N" or ch == "W":
                        if ch == "N":
                            saw_no_cmd = True
                        if ch == "W":
                            return "", False
                    if ch in _SSH_ARG_FLAGS:
                        if idx_f == len(flags_str) - 1:
                            i += 2
                            consumed_next = True
                        break
                if not consumed_next:
                    i += 1
                continue
            dest = tok
            remote_args = list(argv[i + 1 :])
            break
        i += 1

    if saw_batch_yes or saw_no_cmd:
        return "", False
    if remote_args:
        first_rcmd = os.path.basename(remote_args[0].strip("'\"")).lower()
        if first_rcmd in _NON_INTERACTIVE_SSH_CMDS or (
            first_rcmd.startswith("python") and len(remote_args) >= 2 and remote_args[1] == "-"
        ):
            return "", False

    clean_dest = (dest or "").strip()
    if clean_dest.lower().startswith("ssh://"):
        clean_dest = clean_dest[6:].split("/", 1)[0]
        if ":" in clean_dest:
            clean_dest = clean_dest.rsplit(":", 1)[0]
    if "@" in clean_dest:
        clean_dest = clean_dest.rsplit("@", 1)[-1]
    clean_dest = clean_dest.strip().lower().rstrip(".")
    if not clean_dest or clean_dest.startswith("-"):
        return "", True
    return clean_dest, True


def _clean_extracted_cwd(raw_cwd: str) -> str:
    s = _TRAILING_TAGS_RE.sub("", (raw_cwd or "").strip())
    s = _TERM_DIM_TAIL_RE.sub("", s).strip()
    return s


def _parse_title_remote_hints(
    window_title: str, local_hostnames: Collection[str]
) -> tuple[bool, str, str]:
    """Inspect ``window_title`` for ``user@host:cwd`` or ``ssh ...`` signatures.

    Returns ``(is_remote, host_candidate, cwd_hint)``.
    """
    raw = (window_title or "").strip()
    if not raw:
        return False, "", ""

    body = _TAB_PREFIX_RE.sub("", raw).strip()
    # Unwrap quoted tmux set-titles format e.g. `0:0.0 "dev@devbox: ~/repo"`
    if '"' in body and body.endswith('"'):
        qpos = body.find('"')
        if 0 <= qpos < len(body) - 2:
            inner = body[qpos + 1 : -1].strip()
            if inner:
                body = inner

    body_no_tags = _TERM_DIM_TAIL_RE.sub("", _TRAILING_TAGS_RE.sub("", body)).strip()

    # 1. `ssh ...` command in window title
    m_ssh = _SSH_CMD_TITLE_RE.match(body_no_tags)
    if m_ssh:
        rest = (m_ssh.group("rest") or "").strip()
        if not rest:
            if m_ssh.group("prefix"):
                return False, "", ""
            return True, "", ""
        tokens = [m_ssh.group("bin"), *rest.split()]
        host_cand, is_inter = extract_ssh_target_from_argv(tokens)
        if not is_inter:
            return False, "", ""
        if host_cand and host_cand in local_hostnames:
            return False, "", ""
        return True, host_cand, ""

    if body_no_tags.lower() == "openssh ssh client":
        return True, "", ""

    # 2. `user@host: ~/path`
    m_uh = _USER_HOST_COLON_RE.match(body_no_tags)
    if m_uh:
        h_low = m_uh.group("host").strip().lower().rstrip(".")
        cwd_part = _clean_extracted_cwd(m_uh.group("cwd"))
        if h_low in local_hostnames:
            return False, "", cwd_part
        return True, h_low, cwd_part

    # 3. `[user@host dir]$`
    m_br = _BRACKET_PROMPT_RE.match(body_no_tags)
    if m_br:
        h_low = m_br.group("host").strip().lower().rstrip(".")
        raw_dir = _clean_extracted_cwd(m_br.group("cwd"))
        cwd_part = raw_dir if (raw_dir == "~" or raw_dir.startswith(("~/", "/"))) else ""
        if h_low in local_hostnames:
            return False, "", cwd_part
        return True, h_low, cwd_part

    # 4. `ubuntu@devbox - PuTTY`
    m_pt = _PUTTY_STYLE_TITLE_RE.match(body_no_tags)
    if m_pt:
        h_low = m_pt.group("host").strip().lower().rstrip(".")
        if h_low not in local_hostnames:
            return True, h_low, ""

    return False, "", ""


def extract_local_prompt_cwd(window_title: str, local_hostnames: Collection[str]) -> str | None:
    """When ``window_title`` is an explicit local shell prompt (e.g. ``dev@MyPC: ~``), return its path."""
    raw = (window_title or "").strip()
    if not raw:
        return None
    body = _TERM_DIM_TAIL_RE.sub(
        "", _TRAILING_TAGS_RE.sub("", _TAB_PREFIX_RE.sub("", raw).strip())
    ).strip()
    m_uh = _USER_HOST_COLON_RE.match(body)
    if m_uh:
        h_low = m_uh.group("host").strip().lower().rstrip(".")
        if h_low in local_hostnames:
            return _clean_extracted_cwd(m_uh.group("cwd"))
    return None


def detect_remote_foreground(
    window_title: str = "",
    *,
    window_process: str = "",
    bundle_id: str = "",
    document: str = "",
    local_hostnames: Collection[str] = (),
    ssh_proc_host: str | None = None,
    pane_title_override: str = "",
    is_macos: bool | None = None,
) -> RemoteForegroundInfo:
    """Evaluate Phase 0 Remote Veto & target host extraction from foreground window signals."""
    win_obj = _WindowAdapter(window_process, bundle_id, window_title, document)

    # 1. Remote IDE window ([SSH: host], vscode-remote://ssh-remote+..., Codespaces, Tunnel)
    if is_remote_ide_window(win_obj):
        m_ide = _SSH_IDE_HOST_RE.search(window_title or "")
        ide_host = m_ide.group(1).strip().lower().rstrip(".") if m_ide else ""
        return RemoteForegroundInfo(
            is_remote=True,
            host_candidate=ide_host,
            ide_remote_only=True,
            reason="remote_ide",
        )

    eff_title = (pane_title_override or window_title or "").strip()

    # 2. Explicit [@host_tag] in title (or pane_title)
    host_tag, tag_ambig, tag_malformed = parse_window_host_tag(eff_title)
    if not host_tag and not tag_ambig and not tag_malformed and pane_title_override and window_title:
        host_tag, tag_ambig, tag_malformed = parse_window_host_tag(window_title)

    if tag_malformed or tag_ambig:
        return RemoteForegroundInfo(
            is_remote=True,
            ambiguous_host=True,
            effective_title=strip_host_tags_from_title(eff_title),
            reason="invalid_or_ambiguous_host_tag",
        )

    _, title_host, title_cwd = _parse_title_remote_hints(eff_title, local_hostnames)
    if not title_cwd:
        m_mid = _MIDDOT_CWD_RE.search(strip_host_tags_from_title(eff_title))
        if m_mid:
            title_cwd = _clean_extracted_cwd(m_mid.group("cwd"))

    if host_tag:
        if host_tag not in local_hostnames:
            return RemoteForegroundInfo(
                is_remote=True,
                host_candidate=host_tag,
                cwd_hint=title_cwd,
                effective_title=strip_host_tags_from_title(eff_title),
                reason="host_tag",
            )

    # 3. Local TTY / tmux pane / terminal child running interactive `ssh <dest>`
    if ssh_proc_host is not None:
        eff_h = host_tag if (host_tag and host_tag not in local_hostnames) else (ssh_proc_host or title_host)
        if eff_h in local_hostnames:
            eff_h = ""
        return RemoteForegroundInfo(
            is_remote=True,
            host_candidate=eff_h,
            cwd_hint=title_cwd,
            effective_title=strip_host_tags_from_title(eff_title),
            reason="local_ssh_proc",
        )

    # 4. Window title `user@remote:path` or `ssh ...`
    is_title_rem, t_host, t_cwd = _parse_title_remote_hints(eff_title, local_hostnames)
    if not is_title_rem and pane_title_override and window_title:
        is_title_rem, t_host, t_cwd = _parse_title_remote_hints(window_title, local_hostnames)
    if is_title_rem:
        return RemoteForegroundInfo(
            is_remote=True,
            host_candidate=t_host,
            cwd_hint=t_cwd or title_cwd,
            effective_title=strip_host_tags_from_title(eff_title),
            reason="title_ssh",
        )

    # 5. Dedicated SSH client process (PuTTY, Windows KiTTY, MobaXterm, WindTerm, SecureCRT, Xshell, Termius)
    if is_dedicated_ssh_client_window(win_obj, is_macos=is_macos):
        return RemoteForegroundInfo(
            is_remote=True,
            host_candidate=title_host,
            cwd_hint=title_cwd,
            effective_title=strip_host_tags_from_title(eff_title),
            reason="dedicated_ssh_client",
        )

    return RemoteForegroundInfo(is_remote=False)


# ---------------------------------------------------------------------------
# SSH binary resolution, safe argv construction, and bounded gzip decoding
# ---------------------------------------------------------------------------


def resolve_ssh_executable(
    raw_ssh_bin: str = "ssh",
    *,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve ``ssh`` or Windows ``ssh.exe`` safely (compatible with WSL2 ``appendWindowsPath=false``)."""
    src_env = os.environ if env is None else env
    s = (raw_ssh_bin or "ssh").strip()
    if not s or s.startswith("-") or any(ord(c) < 0x20 or ord(c) == 0x7F for c in s):
        return None

    hermetic = is_hermetic_env(env)
    raw_path = src_env.get("PATH")

    if "/" in s or "\\" in s or s.startswith("~"):
        s_unq = s[1:-1].strip() if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'") else s
        if not s_unq or s_unq.startswith("-"):
            return None
        p = expand_user_path(s_unq, env=src_env)
        if p.is_file() and (os.access(str(p), os.X_OK) or str(p).lower().endswith(".exe")):
            return str(p)
        if m_win := _WIN_DRIVE_SSH_PATH_RE.match(s_unq):
            rest_posix = m_win.group(2).replace("\\", "/").lstrip("/")
            wsl_cand = Path(f"/mnt/{m_win.group(1).lower()}/{rest_posix}")
            if wsl_cand.is_file():
                return str(wsl_cand)
        return None

    if s.lower() == "ssh.exe":
        if raw_path is not None:
            if raw_path and (found := shutil.which("ssh.exe", path=raw_path)):
                return found
        elif not hermetic:
            if found := shutil.which("ssh.exe"):
                return found
        if not hermetic or bool(src_env.get("WSL_DISTRO_NAME") or src_env.get("WSL_INTEROP")):
            for cand in WSL_SYSTEM32_OPENSSH_Candidates:
                if os.path.isfile(cand):
                    return cand
        return None

    if raw_path is not None:
        if raw_path and (found := shutil.which(s, path=raw_path)):
            return found
        if hermetic:
            return None
    elif not hermetic:
        if found := shutil.which(s):
            return found
    if not hermetic:
        for std_p in (
            f"/usr/bin/{s}",
            f"/bin/{s}",
            f"/opt/homebrew/bin/{s}",
            f"/usr/local/bin/{s}",
            f"/opt/local/bin/{s}",
        ):
            if os.path.isfile(std_p) and os.access(std_p, os.X_OK):
                return std_p
    return None


def _resolve_control_socket_pattern(runtime_dir: Path | None) -> str:
    """Return a short POSIX ``ControlPath`` off ``/mnt/`` (must fit 104-byte macOS ``sun_path`` limit)."""
    uid = os.getuid()
    if runtime_dir is not None:
        rt_str = str(runtime_dir)
        if not rt_str.startswith("/mnt/") and len(rt_str) <= 68:
            try:
                ensure_secure_dir(runtime_dir)
                return str(runtime_dir / "cm-%C")
            except Exception:
                pass
    fallback_dir = Path(f"/tmp/avi-ssh-{uid}")
    try:
        ensure_secure_dir(fallback_dir)
        return str(fallback_dir / "cm-%C")
    except Exception:
        return ""


def build_safe_ssh_argv(
    ssh_bin_path: str,
    host_cfg: RemoteHostConfig,
    *,
    runtime_dir: Path | None = None,
    connect_timeout_sec: int = 2,
    enable_multiplexing: bool = True,
) -> list[str]:
    """Construct zero-injection ``ssh`` argv ending strictly with ``['--', ssh_target, 'python3', '-']``."""
    target = host_cfg.ssh_target.strip()
    if not target or target.startswith("-") or any(c.isspace() or ord(c) < 0x20 for c in target):
        raise ValueError(f"unsafe ssh_target rejected: {host_cfg.ssh_target!r}")
    if not ssh_bin_path or ssh_bin_path.startswith("-"):
        raise ValueError(f"unsafe ssh_bin rejected: {ssh_bin_path!r}")

    is_win_exe = ssh_bin_path.lower().endswith(".exe")
    mux_args: list[str] = []
    if not is_win_exe:
        if enable_multiplexing:
            ctl_pat = _resolve_control_socket_pattern(runtime_dir)
            if ctl_pat:
                mux_args = [
                    "-o",
                    "ControlMaster=auto",
                    "-o",
                    f"ControlPath={ctl_pat}",
                    "-o",
                    "ControlPersist=10m",
                ]
        else:
            mux_args = [
                "-o",
                "ControlMaster=no",
                "-o",
                "ControlPath=none",
            ]

    eff_conn_to = max(1, min(10, int(connect_timeout_sec)))
    return [
        ssh_bin_path,
        "-T",
        "-x",
        "-a",
        "-o",
        "BatchMode=yes",
        "-o",
        "RequestTTY=no",
        "-o",
        "RemoteCommand=none",
        "-o",
        "PermitLocalCommand=no",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        f"ConnectTimeout={eff_conn_to}",
        "-o",
        "ServerAliveInterval=2",
        "-o",
        "ServerAliveCountMax=1",
        "-o",
        "LogLevel=ERROR",
        *mux_args,
        "--",
        target,
        "python3",
        "-",
    ]


def build_probe_stdin_payload(req_dict: Mapping[str, Any]) -> bytes:
    """Build compact (~15 KB) ASCII-safe zlib-packed ``_remote_probe.py`` + ``REQ`` entrypoint."""
    json_literal = json.dumps(dict(req_dict), ensure_ascii=True, separators=(",", ":"))
    script = (
        "import base64, json, zlib\n"
        f"_REQ = json.loads({json_literal!r})\n"
        f"_SRC = zlib.decompress(base64.b64decode({_COMPRESSED_PROBE_B64_ASCII!r}))\n"
        'exec(compile(_SRC, "_remote_probe.py", "exec"), {"__name__": "__main__", "REQ": _REQ})\n'
    )
    return script.encode("ascii")


def decode_probe_stdout(
    raw_stdout: bytes, *, max_decompressed_bytes: int = MAX_DECOMPRESSED_STDOUT_BYTES
) -> dict[str, Any]:
    """Safely frame-extract and stream-decompress probe response with strict ``2 MiB`` ceiling."""
    if not raw_stdout:
        raise ValueError("empty remote probe output")

    magic = _remote_probe.PROBE_FRAME_MAGIC
    idx = raw_stdout.find(magic)
    if idx >= 0:
        hdr_end = idx + len(magic) + 4
        if len(raw_stdout) < hdr_end:
            raise ValueError("truncated probe frame header")
        (gz_len,) = struct.unpack(">I", raw_stdout[idx + len(magic) : hdr_end])
        if gz_len <= 0 or gz_len > MAX_COMPRESSED_STDOUT_BYTES:
            raise ValueError(f"invalid compressed probe length: {gz_len}")
        if len(raw_stdout) < hdr_end + gz_len:
            raise ValueError("truncated compressed probe payload")
        gz_bytes = raw_stdout[hdr_end : hdr_end + gz_len]
    elif raw_stdout[:2] == b"\x1f\x8b":
        if len(raw_stdout) > MAX_COMPRESSED_STDOUT_BYTES:
            raise ValueError(f"compressed remote probe output too large: {len(raw_stdout)}")
        gz_bytes = raw_stdout
    else:
        raise ValueError("missing probe magic header in stdout")

    decomp = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = decomp.decompress(gz_bytes, max_decompressed_bytes + 1)
    if decomp.unconsumed_tail or len(out) > max_decompressed_bytes:
        raise ValueError("remote probe response exceeded 2 MiB decompression cap")
    tail = decomp.flush(max_decompressed_bytes + 1 - len(out))
    out += tail
    if len(out) > max_decompressed_bytes:
        raise ValueError("remote probe response exceeded 2 MiB decompression cap")
    if not decomp.eof:
        raise ValueError("truncated gzip stream in remote probe output")

    parsed = json.loads(out.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("remote probe response must be a JSON object")
    return _validate_probe_response_schema(parsed)


def _validate_probe_response_schema(data: dict[str, Any]) -> dict[str, Any]:
    if not bool(data.get("ok")):
        return {"ok": False, "reason": str(data.get("reason") or "failed")[:128]}
    if data.get("pong"):
        return {
            "ok": True,
            "pong": True,
            "python": str(data.get("python") or "")[:32],
            "hostname": str(data.get("hostname") or "")[:128],
        }
    cli = str(data.get("cli") or "").strip().lower()
    if cli not in ("codex", "claude", "agy", "workspace"):
        raise ValueError(f"unexpected remote cli {cli!r}")
    raw_sid = data.get("session_id")
    if not isinstance(raw_sid, str) or len(raw_sid) > 128:
        raise ValueError("invalid remote session_id")
    sid = raw_sid.strip().lower()
    if not sid or any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in sid):
        raise ValueError("invalid remote session_id")
    not_mod = bool(data.get("not_modified") or data.get("unchanged"))
    turns_out: list[tuple[str, str]] = []
    prior_turns_out: list[tuple[str, str]] = []
    if not not_mod:
        raw_turns = data.get("turns") or []
        if not isinstance(raw_turns, list) or len(raw_turns) > 5000:
            raise ValueError("invalid remote turns list")
        for item in raw_turns:
            if (
                not isinstance(item, (list, tuple))
                or len(item) != 2
                or item[0] not in ("user", "assistant")
                or not isinstance(item[1], str)
            ):
                raise ValueError("invalid remote turn entry")
            turns_out.append((item[0], item[1]))
        raw_prior = data.get("prior_turns") or []
        if not isinstance(raw_prior, list) or len(raw_prior) > _remote_probe.COLD_START_PRIOR_MAX_TURNS:
            raise ValueError("invalid remote prior_turns list")
        prior_chars = 0
        prior_char_cap = _remote_probe.COLD_START_PRIOR_MAX_CHARS + 5000
        for item in raw_prior:
            if (
                not isinstance(item, (list, tuple))
                or len(item) != 2
                or item[0] not in ("user", "assistant")
                or not isinstance(item[1], str)
            ):
                raise ValueError("invalid remote prior_turn entry")
            prior_chars += len(item[1])
            if prior_chars > prior_char_cap:
                raise ValueError("remote prior_turns exceeds size limit")
            prior_turns_out.append((item[0], item[1]))

    def _clean_str_list(key: str, cap: int) -> list[str]:
        raw = data.get(key) or []
        if not isinstance(raw, list):
            return []
        out_l: list[str] = []
        for x in raw[:cap]:
            if isinstance(x, str) and x.strip() and len(x) <= 128 and "\x00" not in x:
                out_l.append(x.strip())
        return out_l

    evicted_raw = data.get("evicted_chunks", 0)
    comp_raw = data.get("compaction_count", 0)
    return {
        "ok": True,
        "not_modified": not_mod,
        "unchanged": not_mod,
        "etag": str(data.get("etag") or "")[:160],
        "cli": cli,
        "session_id": sid,
        "matched_by": str(data.get("matched_by") or "remote")[:64],
        "cwd": str(data.get("cwd") or "").strip()[:512],
        "git_status": str(data.get("git_status") or "")[:8192],
        "git_hotwords": _clean_str_list("git_hotwords", 48),
        "repo_files": _clean_str_list("repo_files", 32),
        "shallow_files": _clean_str_list("shallow_files", 32),
        "files_included": bool(data.get("files_included")),
        "turns": turns_out,
        "prior_turns": prior_turns_out,
        "prior_hotwords": _clean_str_list("prior_hotwords", _remote_probe.COLD_START_PRIOR_MAX_HOTWORDS),
        "evicted_chunks": int(evicted_raw) if isinstance(evicted_raw, int) and not isinstance(evicted_raw, bool) else 0,
        "compaction_count": int(comp_raw) if isinstance(comp_raw, int) and not isinstance(comp_raw, bool) else 0,
    }


def _is_auth_or_hostkey_failure(stderr_text: str) -> bool:
    low = (stderr_text or "").lower()
    return any(
        phrase in low
        for phrase in (
            "permission denied",
            "host key verification failed",
            "remote host identification has changed",
            "authentication failed",
            "publickey,password",
        )
    )


def _terminate_and_reap_proc(proc: subprocess.Popen[bytes]) -> None:
    try:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=0.5)
            except Exception:
                pass
    finally:
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except Exception:
                    pass


def _write_pipe_and_close(pipe: Any, data: bytes) -> None:
    try:
        pipe.write(data)
        pipe.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def _read_pipe_bounded(pipe: Any, sink: bytearray, max_bytes: int) -> None:
    try:
        fd = pipe.fileno()
        while True:
            chunk = os.read(fd, 16384)
            if not chunk:
                break
            rem = max_bytes + 1 - len(sink)
            if rem > 0:
                sink.extend(chunk[:rem])
    except (OSError, ValueError):
        pass


# ---------------------------------------------------------------------------
# Stateful RemoteSshManager (Circuit Breaker + Async Prefetch + Zero-Leak Close)
# ---------------------------------------------------------------------------


class RemoteSshManager:
    """Thread-safe manager for SSH remote context fetching, prefetching, and per-host circuit breaking."""

    def __init__(
        self,
        remote_cfg: RemoteConfig,
        *,
        env: Mapping[str, str] | None = None,
        runtime_dir: Path | None = None,
        clock_fn: Callable[[], float] = time.monotonic,
        hard_timeout_sec: float = PREFETCH_HARD_TIMEOUT_SEC,
        enable_multiplexing: bool = True,
    ) -> None:
        self.remote_cfg = remote_cfg
        self.env = os.environ if env is None else env
        self.runtime_dir = runtime_dir
        self.clock_fn = clock_fn
        self.hard_timeout_sec = max(0.2, float(hard_timeout_sec))
        self.enable_multiplexing = enable_multiplexing
        self._lock = threading.RLock()
        self._closed = False
        self._breakers: dict[str, _HostBreakerState] = {}
        self._inflight: dict[str, _PrefetchJob] = {}
        self._completed: dict[str, _PrefetchJob] = {}
        self._host_gen: dict[str, int] = {}
        self._cancelled_seqs: set[int] = set()
        self._active_procs: set[subprocess.Popen[bytes]] = set()
        self._bg_threads: set[threading.Thread] = set()

    def update_config(self, remote_cfg: RemoteConfig, *, runtime_dir: Path | None = None) -> None:
        with self._lock:
            self.remote_cfg = remote_cfg
            if runtime_dir is not None:
                self.runtime_dir = runtime_dir

    def breaker_snapshot(self) -> dict[str, dict[str, Any]]:
        now = self.clock_fn()
        with self._lock:
            out: dict[str, dict[str, Any]] = {}
            for name, st in self._breakers.items():
                rem = max(0.0, st.cooldown_until - now)
                out[name] = {
                    "open": rem > 0.0,
                    "tripped": st.tripped,
                    "consecutive_failures": st.consecutive_failures,
                    "cooldown_remaining_sec": round(rem, 1),
                    "last_error": st.last_error,
                    "last_latency_ms": st.last_latency_ms,
                }
            return out

    def _can_attempt_locked(self, host_name: str, *, allow_half_open: bool) -> tuple[bool, bool]:
        if self._closed:
            return False, False
        st = self._breakers.get(host_name)
        if st is None:
            return True, False
        now = self.clock_fn()
        if now < st.cooldown_until:
            return False, False
        if st.tripped:
            if not allow_half_open or st.half_open_in_flight:
                return False, False
            st.half_open_in_flight = True
            return True, True
        return True, False

    def _record_success_locked(self, host_name: str, latency_ms: int) -> None:
        st = self._breakers.setdefault(host_name, _HostBreakerState())
        st.consecutive_failures = 0
        st.cooldown_until = 0.0
        st.tripped = False
        st.half_open_in_flight = False
        st.last_error = ""
        st.last_latency_ms = latency_ms

    def _record_failure_locked(
        self, host_name: str, err_msg: str, *, is_auth: bool, latency_ms: int
    ) -> None:
        st = self._breakers.setdefault(host_name, _HostBreakerState())
        was_tripped = st.tripped
        st.half_open_in_flight = False
        st.consecutive_failures += 1
        st.last_error = redact_secrets(err_msg, self.env)[:200]
        st.last_latency_ms = latency_ms
        now = self.clock_fn()
        if is_auth:
            st.tripped = True
            st.cooldown_until = now + BREAKER_AUTH_COOLDOWN_SEC
        elif was_tripped or st.consecutive_failures >= BREAKER_CONN_THRESHOLD:
            st.tripped = True
            st.cooldown_until = now + BREAKER_CONN_COOLDOWN_SEC

    def _clear_half_open_on_abort_locked(self, host_name: str) -> None:
        st = self._breakers.get(host_name)
        if st is not None:
            st.half_open_in_flight = False

    def _run_ssh_probe_once(
        self,
        host_cfg: RemoteHostConfig,
        req_dict: Mapping[str, Any],
        *,
        abort_evt: threading.Event | None = None,
        timeout_sec: float | None = None,
    ) -> tuple[dict[str, Any] | None, str, bool, bool]:
        """Return ``(parsed_resp, error_msg, is_auth_error, was_aborted)``."""
        raw_sbin = host_cfg.ssh_bin or self.remote_cfg.ssh_bin or "ssh"
        ssh_exe = resolve_ssh_executable(raw_sbin, env=self.env)
        if not ssh_exe:
            return None, f"ssh binary not found: {raw_sbin}", False, False

        eff_timeout = max(0.15, float(timeout_sec if timeout_sec is not None else self.hard_timeout_sec))
        conn_to = max(1, min(5, int(eff_timeout) + 1))
        argv = build_safe_ssh_argv(
            ssh_exe,
            host_cfg,
            runtime_dir=self.runtime_dir,
            connect_timeout_sec=conn_to,
            enable_multiplexing=self.enable_multiplexing,
        )
        stdin_bytes = build_probe_stdin_payload(req_dict)

        child_env = dict(self.env)
        child_env.setdefault("LC_ALL", "C.UTF-8")

        with self._lock:
            if self._closed or (abort_evt is not None and abort_evt.is_set()):
                return None, "aborted", False, True
            try:
                proc = subprocess.Popen(
                    argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                    env=child_env,
                )
            except OSError as exc:
                return None, f"failed to spawn ssh: {exc}", False, False
            self._active_procs.add(proc)

        out_buf = bytearray()
        err_buf = bytearray()
        stdin_pipe = None
        t_in: threading.Thread | None = None
        t_out: threading.Thread | None = None
        t_err: threading.Thread | None = None

        try:
            stdin_pipe, proc.stdin = proc.stdin, None
            assert stdin_pipe is not None and proc.stdout is not None and proc.stderr is not None
            t_in = threading.Thread(
                target=_write_pipe_and_close,
                args=(stdin_pipe, stdin_bytes),
                daemon=True,
            )
            t_out = threading.Thread(
                target=_read_pipe_bounded,
                args=(proc.stdout, out_buf, MAX_COMPRESSED_STDOUT_BYTES),
                daemon=True,
            )
            t_err = threading.Thread(
                target=_read_pipe_bounded,
                args=(proc.stderr, err_buf, MAX_STDERR_BYTES),
                daemon=True,
            )
            t_in.start()
            stdin_pipe = None
            t_out.start()
            t_err.start()

            deadline = time.monotonic() + eff_timeout
            while proc.poll() is None:
                if self._closed or (abort_evt is not None and abort_evt.is_set()):
                    _terminate_and_reap_proc(proc)
                    t_in.join(timeout=0.3)
                    t_out.join(timeout=0.3)
                    t_err.join(timeout=0.3)
                    return None, "aborted", False, True
                rem = deadline - time.monotonic()
                if rem <= 0.0:
                    _terminate_and_reap_proc(proc)
                    t_in.join(timeout=0.3)
                    t_out.join(timeout=0.3)
                    t_err.join(timeout=0.3)
                    return None, f"ssh probe timed out after {int(eff_timeout * 1000)}ms", False, False
                if abort_evt is not None:
                    if abort_evt.wait(timeout=min(0.02, rem)):
                        continue
                else:
                    time.sleep(min(0.02, rem))

            t_in.join(timeout=0.5)
            t_out.join(timeout=0.5)
            t_err.join(timeout=0.5)
            rc = proc.returncode
            stderr_txt = bytes(err_buf).decode("utf-8", errors="replace").strip()
            if rc != 0:
                is_auth = _is_auth_or_hostkey_failure(stderr_txt)
                return None, (stderr_txt or f"ssh exited with code {rc}"), is_auth, False

            try:
                decoded = decode_probe_stdout(bytes(out_buf))
            except Exception as dec_exc:
                return None, f"invalid remote probe payload: {dec_exc}", False, False
            return decoded, "", False, False
        finally:
            if stdin_pipe is not None:
                try:
                    stdin_pipe.close()
                except Exception:
                    pass
            with self._lock:
                self._active_procs.discard(proc)
            _terminate_and_reap_proc(proc)

    def spawn_async_task(self, fn: Callable[[], None], *, seq_id: int = 0) -> bool:
        """Run a short-lived preflight/routing task off the hotkey trigger lock."""
        with self._lock:
            if self._closed or (seq_id > 0 and seq_id in self._cancelled_seqs):
                return False

            def _runner() -> None:
                try:
                    with self._lock:
                        if self._closed or (seq_id > 0 and seq_id in self._cancelled_seqs):
                            return
                    fn()
                except Exception:
                    pass
                finally:
                    with self._lock:
                        self._bg_threads.discard(threading.current_thread())

            th = threading.Thread(target=_runner, name="avi-ssh-dispatch", daemon=True)
            self._bg_threads = {t for t in self._bg_threads if t.is_alive()}
            self._bg_threads.add(th)
            try:
                th.start()
            except BaseException:
                self._bg_threads.discard(th)
                raise
            return True

    def _start_or_join_job(
        self,
        host_cfg: RemoteHostConfig,
        req_dict: Mapping[str, Any],
        *,
        cache_key: tuple[Any, ...],
        seq_id: int = 0,
        allow_half_open: bool = True,
        on_result: Callable[[RemoteHostConfig, dict[str, Any]], None] | None = None,
    ) -> _PrefetchJob | None:
        with self._lock:
            if self._closed or (seq_id > 0 and seq_id in self._cancelled_seqs):
                return None
            existing = self._inflight.get(host_cfg.name)
            active_existing = (
                existing
                if (
                    existing is not None
                    and not existing.done_evt.is_set()
                    and not existing.abort_evt.is_set()
                )
                else None
            )
            if active_existing is not None and active_existing.cache_key == cache_key:
                if seq_id > 0:
                    active_existing.seq_id = seq_id
                return active_existing

            if active_existing is not None and active_existing.owns_half_open:
                if not allow_half_open:
                    # Keep the in-flight half-open recovery probe running uninterrupted
                    return None
                active_existing.owns_half_open = False
                active_existing.abort_evt.set()
                self._inflight.pop(host_cfg.name, None)
                can_run, claimed_ho = True, True
            else:
                can_run, claimed_ho = self._can_attempt_locked(
                    host_cfg.name, allow_half_open=allow_half_open
                )
                if not can_run:
                    return None
                if active_existing is not None:
                    active_existing.abort_evt.set()
                    self._inflight.pop(host_cfg.name, None)

            comp_prev = self._completed.get(host_cfg.name)
            if comp_prev is not None and comp_prev.cache_key != cache_key:
                self._completed.pop(host_cfg.name, None)

            gen = self._host_gen.get(host_cfg.name, 0) + 1
            self._host_gen[host_cfg.name] = gen
            job = _PrefetchJob(
                host_name=host_cfg.name,
                cache_key=cache_key,
                seq_id=seq_id,
                started_mono=self.clock_fn(),
                gen=gen,
                owns_half_open=claimed_ho,
            )
            self._inflight[host_cfg.name] = job

            def _worker() -> None:
                t0 = time.monotonic()
                ho_settled = False
                try:
                    resp, err, is_auth, aborted = self._run_ssh_probe_once(
                        host_cfg,
                        req_dict,
                        abort_evt=job.abort_evt,
                        timeout_sec=self.hard_timeout_sec,
                    )
                    lat_ms = max(0, int(round((time.monotonic() - t0) * 1000)))
                    should_apply = False
                    resp_with_gen: dict[str, Any] | None = None
                    with self._lock:
                        is_stale = (
                            aborted
                            or job.abort_evt.is_set()
                            or self._closed
                            or job.gen != self._host_gen.get(host_cfg.name, 0)
                        )
                        if is_stale:
                            if job.owns_half_open:
                                self._clear_half_open_on_abort_locked(host_cfg.name)
                                ho_settled = True
                        elif resp is not None:
                            self._record_success_locked(host_cfg.name, lat_ms)
                            ho_settled = True
                            resp_with_gen = {**resp, "_probe_gen": job.gen}
                            job.response = resp_with_gen
                            job.completed_mono = self.clock_fn()
                            should_apply = on_result is not None
                        else:
                            self._record_failure_locked(
                                host_cfg.name, err, is_auth=is_auth, latency_ms=lat_ms
                            )
                            ho_settled = True

                    if should_apply and resp_with_gen is not None and on_result is not None:
                        try:
                            on_result(host_cfg, resp_with_gen)
                            with self._lock:
                                job.applied = True
                        except Exception:
                            pass

                    with self._lock:
                        if (
                            resp is not None
                            and not job.abort_evt.is_set()
                            and not self._closed
                            and job.gen == self._host_gen.get(host_cfg.name, 0)
                        ):
                            self._completed[host_cfg.name] = job
                finally:
                    with self._lock:
                        if job.owns_half_open and not ho_settled:
                            self._clear_half_open_on_abort_locked(host_cfg.name)
                        if self._inflight.get(host_cfg.name) is job:
                            self._inflight.pop(host_cfg.name, None)
                        self._bg_threads.discard(threading.current_thread())
                    job.done_evt.set()

            th = threading.Thread(
                target=_worker, name=f"avi-ssh-prefetch-{host_cfg.name}", daemon=True
            )
            job.thread = th
            self._bg_threads = {t for t in self._bg_threads if t.is_alive()}
            self._bg_threads.add(th)
            try:
                th.start()
            except BaseException:
                if job.owns_half_open:
                    self._clear_half_open_on_abort_locked(host_cfg.name)
                if self._inflight.get(host_cfg.name) is job:
                    self._inflight.pop(host_cfg.name, None)
                self._bg_threads.discard(th)
                job.done_evt.set()
                raise
            return job

    def start_prefetch(
        self,
        host_cfg: RemoteHostConfig,
        req_dict: Mapping[str, Any],
        *,
        cache_key: tuple[Any, ...],
        seq_id: int = 0,
        on_result: Callable[[RemoteHostConfig, dict[str, Any]], None] | None = None,
    ) -> bool:
        """Start non-blocking background SSH probe during ``start_recording``."""
        return (
            self._start_or_join_job(
                host_cfg,
                req_dict,
                cache_key=cache_key,
                seq_id=seq_id,
                allow_half_open=True,
                on_result=on_result,
            )
            is not None
        )

    def fetch_with_budget(
        self,
        host_cfg: RemoteHostConfig,
        req_dict: Mapping[str, Any],
        *,
        cache_key: tuple[Any, ...],
        budget_ms: int,
        allow_start_if_missing: bool = True,
        allow_half_open: bool = False,
        is_cancelled: Callable[[], bool] | None = None,
        on_result: Callable[[RemoteHostConfig, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any] | None:
        """Return fresh prefetched response in 0ms, or wait up to ``budget_ms`` without blocking longer."""
        now = self.clock_fn()
        job: _PrefetchJob | None = None
        with self._lock:
            if self._closed:
                return None
            comp = self._completed.get(host_cfg.name)
            if (
                comp is not None
                and comp.cache_key == cache_key
                and comp.response is not None
                and 0.0 <= (now - comp.completed_mono) <= PREFETCH_FRESH_TTL_SEC
            ):
                self._completed.pop(host_cfg.name, None)
                out_fast = dict(comp.response)
                out_fast["_already_applied"] = comp.applied
                out_fast.setdefault("_probe_gen", comp.gen)
                return out_fast

            inflight = self._inflight.get(host_cfg.name)
            if (
                inflight is not None
                and inflight.cache_key == cache_key
                and not inflight.done_evt.is_set()
                and not inflight.abort_evt.is_set()
            ):
                job = inflight
            elif allow_start_if_missing:
                job = self._start_or_join_job(
                    host_cfg,
                    req_dict,
                    cache_key=cache_key,
                    allow_half_open=allow_half_open,
                    on_result=on_result,
                )

        if job is None:
            return None

        eff_wait_sec = max(0.0, float(budget_ms) / 1000.0)
        deadline = time.monotonic() + eff_wait_sec
        while not job.done_evt.is_set():
            if self._closed or job.abort_evt.is_set() or (is_cancelled is not None and is_cancelled()):
                job.abort_evt.set()
                return None
            rem = deadline - time.monotonic()
            if rem <= 0.0:
                # Budget expired: return None immediately for smooth 400ms fallback while
                # allowing the background job to warm the ETag cache for the next turn!
                return None
            job.done_evt.wait(timeout=min(0.02, rem))

        with self._lock:
            if self._completed.get(host_cfg.name) is job:
                self._completed.pop(host_cfg.name, None)
            if (
                job.abort_evt.is_set()
                or self._closed
                or job.cache_key != cache_key
                or job.response is None
            ):
                return None
            out_resp = dict(job.response)
            out_resp["_already_applied"] = job.applied
            out_resp.setdefault("_probe_gen", job.gen)
            return out_resp

    def cancel_prefetch(self, *, seq_id: int | None = None) -> None:
        to_abort: list[_PrefetchJob] = []
        with self._lock:
            if seq_id is not None and seq_id > 0:
                self._cancelled_seqs.add(seq_id)
                if len(self._cancelled_seqs) > 256:
                    for old_s in sorted(self._cancelled_seqs)[:-128]:
                        self._cancelled_seqs.discard(old_s)
            for job in list(self._inflight.values()):
                if seq_id is None or job.seq_id == seq_id:
                    job.abort_evt.set()
                    to_abort.append(job)
            if seq_id is None:
                self._completed.clear()
        if seq_id is None:
            with self._lock:
                procs = list(self._active_procs)
            for p in procs:
                _terminate_and_reap_proc(p)
        else:
            for job in to_abort:
                if job.thread is not None and job.thread.is_alive():
                    job.done_evt.wait(timeout=0.15)

    def probe_host_ping(
        self, host_cfg: RemoteHostConfig, *, timeout_sec: float = 3.0
    ) -> tuple[str, int, str]:
        """Synchronous ``doctor --ping`` health check for a configured remote host."""
        t0 = time.monotonic()
        resp, err, is_auth, _ = self._run_ssh_probe_once(
            host_cfg, {"v": 1, "ping_only": True}, timeout_sec=timeout_sec
        )
        ms = max(0, int(round((time.monotonic() - t0) * 1000)))
        if resp is not None and resp.get("ok"):
            py_ver = resp.get("python") or "3.x"
            return "OK", ms, f"OK ({ms}ms, target={host_cfg.ssh_target}, python={py_ver})"
        clean_err = redact_secrets(err or "unreachable", self.env)[:160]
        hint = ""
        if is_auth:
            hint = f"; Fix: verify 'ssh {host_cfg.ssh_target}' succeeds without password prompt"
        else:
            raw_sbin = (host_cfg.ssh_bin or self.remote_cfg.ssh_bin or "ssh").strip().lower()
            if raw_sbin != "ssh.exe" and any(os.path.isfile(c) for c in WSL_SYSTEM32_OPENSSH_Candidates):
                hint = (
                    f"; WSL2 hint: if VPN/keys live on Windows, set ssh_bin = \"ssh.exe\" "
                    f"or enable networkingMode=mirrored in .wslconfig"
                )
        return "FAIL", ms, f"FAIL ({ms}ms, target={host_cfg.ssh_target}: {clean_err}{hint})"

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for job in self._inflight.values():
                job.abort_evt.set()
            procs = list(self._active_procs)
            threads = [t for t in self._bg_threads if t.is_alive()]
            self._completed.clear()
        for p in procs:
            _terminate_and_reap_proc(p)
        cur = threading.current_thread()
        deadline = time.monotonic() + 1.5
        for th in threads:
            if th is cur:
                continue
            rem = deadline - time.monotonic()
            if rem <= 0.0:
                break
            th.join(timeout=rem)
        with self._lock:
            self._inflight.clear()
            self._bg_threads = {t for t in self._bg_threads if t.is_alive()}
            self._active_procs = {p for p in self._active_procs if p.poll() is None}
