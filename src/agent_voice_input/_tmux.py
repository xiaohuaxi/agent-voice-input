"""Internal ``tmux`` server discovery, socket helpers, and output parsers."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
from typing import Mapping, Sequence

_TMUX_PANE_ID_RE = re.compile(r"^%\d+$")
_CTRL_STRIP_RE = re.compile(r"[\x00-\x1f\x7f]")

_TMUX_CLIENT_FMT = (
    "CLIENT\x1f#{session_id}\x1f#{window_id}\x1f#{pane_id}"
    "\x1f#{client_activity}\x1f#{client_tty}\x1f#{client_pid}\x1f#{client_flags}"
    "\x1f#{socket_path}"
)
_TMUX_PANE_FMT = (
    "PANE\x1f#{session_id}\x1f#{window_id}\x1f#{window_active}"
    "\x1f#{pane_id}\x1f#{pane_active}\x1f#{pane_pid}\x1f#{pane_tty}"
    "\x1f#{socket_path}\x1f#{pane_current_path}\x1f#{pane_title}"
)
_MAX_TMUX_SOCKET_TARGETS = 8


@dataclass(frozen=True)
class TmuxPaneInfo:
    pane_id: str
    session_id: str = ""
    window_id: str = ""
    window_active: bool = True
    pane_active: bool = True
    pane_pid: int = 0
    pane_tty: str = ""
    socket_path: str = ""
    current_path: str = ""
    pane_title: str = ""


@dataclass(frozen=True)
class TmuxClientInfo:
    session_id: str
    window_id: str = ""
    pane_id: str = ""
    activity: float = 0.0
    client_tty: str = ""
    client_pid: int = 0
    focused: bool = False
    socket_path: str = ""


@dataclass(frozen=True)
class TmuxServerSnapshot:
    clients: tuple[TmuxClientInfo, ...] = ()
    panes: tuple[TmuxPaneInfo, ...] = ()


def _norm_same_path(a: str, b: str) -> bool:
    pa = os.path.realpath(a) if os.path.exists(a) else os.path.normpath(a)
    pb = os.path.realpath(b) if os.path.exists(b) else os.path.normpath(b)
    return pa == pb


def _same_tmux_server(sock_a: str, sock_b: str) -> bool:
    if not sock_a and not sock_b:
        return True
    if not sock_a or not sock_b:
        return False
    return _norm_same_path(sock_a, sock_b)


def _is_live_owned_socket(sock_path: str | Path, *, uid: int | None = None) -> bool:
    eff_uid = os.getuid() if uid is None else uid
    try:
        st = os.stat(sock_path)
    except OSError:
        return False
    return stat.S_ISSOCK(st.st_mode) and st.st_uid == eff_uid


def preferred_tmux_socket_arg(sock_path: str | Path) -> str:
    """Prefer ``sock_path`` over ``realpath(sock_path)`` when it exists and has fewer UTF-8 bytes.

    Prevents macOS ``/tmp/...`` symlinks from expanding to ``/private/tmp/...`` (+8 bytes)
    against Darwin's 104-byte ``SUN_PATH`` limit while callers still deduplicate via ``realpath``.
    """
    raw = str(sock_path).strip()
    if not raw:
        return ""
    try:
        real = os.path.realpath(raw)
    except OSError:
        return raw
    if os.path.exists(raw) and len(raw.encode("utf-8")) < len(real.encode("utf-8")):
        return raw
    return real


def _candidate_default_tmux_sockets(env: Mapping[str, str]) -> list[Path]:
    """Return live owned ``tmux-<uid>/*`` server socket paths (``default`` first, bounded)."""
    uid = os.getuid()
    tmux_tmpdir = (env.get("TMUX_TMPDIR") or "").strip()
    if tmux_tmpdir:
        base_dirs = [tmux_tmpdir]
    else:
        raw_bases = ((env.get("TMPDIR") or "").strip(), "/tmp")
        base_dirs = list(dict.fromkeys(b for b in raw_bases if b))

    out: list[Path] = []
    seen: set[str] = set()

    def _add(cand: Path) -> None:
        if len(out) >= _MAX_TMUX_SOCKET_TARGETS:
            return
        if not _is_live_owned_socket(cand, uid=uid):
            return
        key = os.path.normpath(str(cand))
        if key not in seen:
            seen.add(key)
            out.append(cand)

    for base in base_dirs:
        if len(out) >= _MAX_TMUX_SOCKET_TARGETS:
            break
        uid_dir = Path(base) / f"tmux-{uid}"
        _add(uid_dir / "default")
        try:
            st = os.stat(uid_dir)
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid:
                continue
            with os.scandir(uid_dir) as it:
                extra_names = sorted(
                    entry.name for entry in it if entry.name != "default"
                )
        except OSError:
            continue
        for name in extra_names:
            if len(out) >= _MAX_TMUX_SOCKET_TARGETS:
                break
            _add(uid_dir / name)
    return out


def parse_tmux_socket_from_env(raw_tmux: str) -> str:
    """Extract the server socket path from ``$TMUX`` (``<socket_path>,<server_pid>,<session_idx>``)."""
    cleaned = _CTRL_STRIP_RE.sub("", (raw_tmux or "").strip())
    if not cleaned:
        return ""
    parts = cleaned.rsplit(",", 2)
    if len(parts) == 3 and parts[1].lstrip("-").isdigit() and parts[2].lstrip("-").isdigit():
        return parts[0].strip()
    return cleaned.split(",", 1)[0].strip()


def resolve_tmux_bin(env: Mapping[str, str] | None = None) -> str:
    eff_path = (env.get("PATH") if env is not None else None) or os.environ.get("PATH")
    found = shutil.which("tmux", path=eff_path)
    if found:
        return found
    for fallback in ("/usr/bin/tmux", "/usr/local/bin/tmux", "/opt/homebrew/bin/tmux"):
        if os.path.isfile(fallback) and os.access(fallback, os.X_OK):
            return fallback
    return ""


def parse_tmux_clients_output(stdout: str, default_socket: str = "") -> tuple[TmuxClientInfo, ...]:
    """Parse ``CLIENT`` lines from ``list-clients`` output into attached ``TmuxClientInfo`` items."""
    if not stdout:
        return ()
    clients: list[TmuxClientInfo] = []
    for raw_line in stdout.splitlines():
        if not raw_line.startswith("CLIENT\x1f"):
            continue
        parts = raw_line.split("\x1f", 8)
        if len(parts) < 8:
            continue
        _, sess_id, win_id, pane_id, act_s, c_tty, c_pid_s, flags_s = parts[:8]
        c_sock = parts[8].strip() if len(parts) >= 9 else ""
        flags = {f.strip() for f in flags_s.split(",") if f.strip()}
        if flags and "attached" not in flags:
            continue
        try:
            activity = float(act_s or 0.0)
        except ValueError:
            activity = 0.0
        try:
            c_pid = int(c_pid_s or 0)
        except ValueError:
            c_pid = 0
        clients.append(
            TmuxClientInfo(
                session_id=sess_id.strip(),
                window_id=win_id.strip(),
                pane_id=pane_id.strip(),
                activity=activity,
                client_tty=c_tty.strip(),
                client_pid=c_pid,
                focused="focused" in flags,
                socket_path=c_sock or default_socket,
            )
        )
    return tuple(clients)


def parse_tmux_panes_output(stdout: str, default_socket: str = "") -> tuple[TmuxPaneInfo, ...]:
    """Parse ``PANE`` lines from ``list-panes -a`` output into ``TmuxPaneInfo`` items."""
    if not stdout:
        return ()
    panes: list[TmuxPaneInfo] = []
    for raw_line in stdout.splitlines():
        if not raw_line.startswith("PANE\x1f"):
            continue
        parts = raw_line.split("\x1f", 10)
        if len(parts) < 11:
            continue
        (
            _,
            sess_id,
            win_id,
            win_act_s,
            pane_id,
            pane_act_s,
            p_pid_s,
            p_tty,
            sock_p,
            cur_path,
            p_title,
        ) = parts
        if not _TMUX_PANE_ID_RE.match(pane_id.strip()):
            continue
        try:
            p_pid = int(p_pid_s or 0)
        except ValueError:
            p_pid = 0
        panes.append(
            TmuxPaneInfo(
                pane_id=pane_id.strip(),
                session_id=sess_id.strip(),
                window_id=win_id.strip(),
                window_active=(win_act_s.strip() == "1"),
                pane_active=(pane_act_s.strip() == "1"),
                pane_pid=p_pid,
                pane_tty=p_tty.strip(),
                socket_path=sock_p.strip() or default_socket,
                current_path=cur_path.strip(),
                pane_title=p_title,
            )
        )
    return tuple(panes)


def parse_tmux_probe_output(stdout: str, default_socket: str = "") -> TmuxServerSnapshot:
    """Parse combined ``list-clients`` + ``list-panes -a`` output into a ``TmuxServerSnapshot``."""
    if not stdout:
        return TmuxServerSnapshot()
    raw_clients = parse_tmux_clients_output(stdout, default_socket=default_socket)
    panes = parse_tmux_panes_output(stdout, default_socket=default_socket)
    discovered_sock = default_socket
    if not discovered_sock:
        for c in raw_clients:
            if c.socket_path:
                discovered_sock = c.socket_path
                break
    if not discovered_sock:
        for p in panes:
            if p.socket_path:
                discovered_sock = p.socket_path
                break
    if discovered_sock and any(not c.socket_path for c in raw_clients):
        clients = tuple(
            TmuxClientInfo(
                session_id=c.session_id,
                window_id=c.window_id,
                pane_id=c.pane_id,
                activity=c.activity,
                client_tty=c.client_tty,
                client_pid=c.client_pid,
                focused=c.focused,
                socket_path=c.socket_path or discovered_sock,
            )
            for c in raw_clients
        )
    else:
        clients = raw_clients
    return TmuxServerSnapshot(clients=clients, panes=panes)


def probe_tmux_snapshot(
    *,
    env: Mapping[str, str] | None = None,
    tmux_socket: str | Path | None = None,
    tmux_socket_name: str | None = None,
    extra_sockets: Sequence[str | Path] = (),
    timeout_sec: float = 0.4,
) -> TmuxServerSnapshot:
    """Safely query attached ``tmux`` clients and panes with bounded timeout and ``stdin=DEVNULL``."""
    eff_env = os.environ if env is None else env
    disable_flag = (eff_env.get("AGENT_VOICE_INPUT_DISABLE_TMUX") or "").strip().lower()
    if disable_flag in ("1", "true", "yes", "on"):
        return TmuxServerSnapshot()

    tmux_bin = resolve_tmux_bin(eff_env)
    if not tmux_bin:
        return TmuxServerSnapshot()

    sock_override = str(tmux_socket or "").strip()
    name_override = (tmux_socket_name or "").strip()

    targets: list[tuple[list[str], str]] = []
    if sock_override:
        if sock_override.lower() in ("none", "off", "0", "false"):
            return TmuxServerSnapshot()
        sock_arg = preferred_tmux_socket_arg(sock_override)
        targets.append((["-S", sock_arg], sock_arg))
    elif name_override:
        if name_override.lower() in ("none", "off", "0", "false"):
            return TmuxServerSnapshot()
        targets.append((["-L", name_override], ""))
    else:
        cand_socks: list[str] = []
        for def_sock in _candidate_default_tmux_sockets(eff_env):
            if _is_live_owned_socket(def_sock):
                cand_socks.append(str(def_sock))
        for env_key in ("TMUX", "AGENT_VOICE_INPUT_TMUX_HINT"):
            env_sock = parse_tmux_socket_from_env(eff_env.get(env_key) or "")
            if env_sock and _is_live_owned_socket(env_sock):
                cand_socks.append(env_sock)
        for es in extra_sockets:
            es_str = str(es or "").strip()
            if es_str and _is_live_owned_socket(es_str):
                cand_socks.append(es_str)

        seen_target_socks: set[str] = set()
        for raw_s in cand_socks:
            if len(targets) >= _MAX_TMUX_SOCKET_TARGETS:
                break
            real_s = os.path.realpath(raw_s)
            if real_s in seen_target_socks:
                continue
            seen_target_socks.add(real_s)
            sock_arg = preferred_tmux_socket_arg(raw_s)
            targets.append((["-S", sock_arg], sock_arg))

    if not targets:
        return TmuxServerSnapshot()

    child_env = {k: v for k, v in eff_env.items() if k not in ("TMUX", "TMUX_PANE")}
    all_clients: list[TmuxClientInfo] = []
    all_panes: list[TmuxPaneInfo] = []
    seen_panes: set[tuple[str, str]] = set()
    seen_clients: set[tuple[str, str, int]] = set()

    probe_tail = [
        "list-clients",
        "-F",
        _TMUX_CLIENT_FMT,
        ";",
        "list-panes",
        "-a",
        "-F",
        _TMUX_PANE_FMT,
    ]
    for flag_args, hint_sock in targets:
        try:
            proc = subprocess.run(
                [tmux_bin, *flag_args, *probe_tail],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=child_env,
                timeout=timeout_sec,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0 or not proc.stdout:
            continue
        snap = parse_tmux_probe_output(
            proc.stdout.decode("utf-8", errors="replace"),
            default_socket=hint_sock,
        )
        for c in snap.clients:
            norm_cs = (
                os.path.realpath(c.socket_path)
                if c.socket_path and os.path.exists(c.socket_path)
                else c.socket_path
            )
            ck = (norm_cs, c.client_tty, c.client_pid)
            if ck not in seen_clients:
                seen_clients.add(ck)
                all_clients.append(c)
        for p in snap.panes:
            norm_ps = (
                os.path.realpath(p.socket_path)
                if p.socket_path and os.path.exists(p.socket_path)
                else p.socket_path
            )
            pk = (norm_ps, p.pane_id)
            if pk not in seen_panes:
                seen_panes.add(pk)
                all_panes.append(p)

    return TmuxServerSnapshot(clients=tuple(all_clients), panes=tuple(all_panes))
