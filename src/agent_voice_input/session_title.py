"""Window title (OSC 2) and runtime session registry helper for Claude Code and Antigravity CLI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
from typing import Any, Callable, Mapping

from . import _tmux
from .config import get_runtime_dir

_sctx: Any = None


def _ensure_sctx() -> None:
    global _sctx
    if _sctx is None:
        from . import session_ctx as mod_sctx

        _sctx = mod_sctx

_SID_RE = re.compile(r"^[0-9a-f]{8}[0-9a-f-]{0,56}$")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
_HOST_TAG_VAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _resolve_ssh_host_tag(
    env: Mapping[str, str], *, explicit_env: bool, proc_root: Path
) -> str:
    """Return ``' [@<host_tag>]'`` when running inside an SSH session or with explicit host override."""
    override = _CTRL_RE.sub("", (env.get("AGENT_VOICE_INPUT_HOST_TAG") or "").strip()).lower().rstrip(".")
    if override and _HOST_TAG_VAL_RE.match(override):
        return f" [@{override}]"
    if not explicit_env and proc_root != Path("/proc"):
        return ""
    if not any((env.get(k) or "").strip() for k in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")):
        return ""
    raw_host = (env.get("HOSTNAME") or "").strip()
    if not raw_host and hasattr(os, "uname"):
        try:
            raw_host = os.uname().nodename
        except OSError:
            raw_host = ""
    if not raw_host:
        try:
            raw_host = socket.gethostname()
        except OSError:
            raw_host = ""
    short = _CTRL_RE.sub("", raw_host.strip()).lower().rstrip(".").split(".", 1)[0]
    if short and _HOST_TAG_VAL_RE.match(short):
        return f" [@{short}]"
    return ""


def _short_cwd(cwd: str, home: str) -> str:
    p, h = (cwd or "").rstrip("/"), (home or "").rstrip("/")
    if not p or (h and p == h):
        return "~"
    if h and p.startswith(h + "/"):
        parts = p[len(h) + 1 :].split("/")
        p = ("~/" + "/".join(parts)) if len(parts) <= 2 else ("~/…/" + parts[-1])
    elif len([x for x in p.split("/") if x]) > 2:
        p = "…/" + os.path.basename(p)
    return _CTRL_RE.sub("", p)[:48] or "~"


def _proc_info(pid_dir: Path) -> tuple[str, int, int, int, list[str]] | None:
    try:
        txt = (pid_dir / "stat").read_text(encoding="utf-8", errors="replace")
        lp, rp = txt.find("("), txt.rfind(")")
        if lp < 0 or rp <= lp:
            return None
        f = txt[rp + 2 :].split()
        raw = (pid_dir / "cmdline").read_bytes() if (pid_dir / "cmdline").exists() else b""
        return (
            txt[lp + 1 : rp],
            int(f[1]),
            int(f[3]),
            int(f[4]),
            [a.decode("utf-8", errors="ignore") for a in raw.split(b"\x00") if a],
        )
    except (OSError, ValueError, IndexError):
        return None


def _is_test_runner_proc(comm: str, args: list[str]) -> bool:
    if comm in ("pytest", "py.test"):
        return True
    for i, a in enumerate(args[:6]):
        base = os.path.basename(a)
        if base in ("pytest", "py.test") or (base.startswith("test_") and base.endswith(".py")):
            return True
        if a == "-m" and i + 1 < len(args) and args[i + 1].split(".")[0] in ("unittest", "pytest"):
            return True
    return False


def _has_proc_tty(pdir: Path, tty_nr: int, *, allow_fixture_tty: bool = False) -> bool:
    if tty_nr > 0:
        return True
    _ensure_sctx()
    for fn in ("0", "2"):
        t = _sctx._readlink_clean(pdir / "fd" / fn)
        if not t or t == "/dev/null":
            continue
        if t.startswith("/dev/pts/") or (t.startswith("/dev/tty") and t != "/dev/tty"):
            return True
        if allow_fixture_tty and os.path.basename(t).lower().startswith(("pts", "tty")):
            return True
    return False


def _walk_ancestor(
    pid0: int, proc_root: Path, pred: Callable[[str, list[str]], bool]
) -> tuple[int, list[str]]:
    _ensure_sctx()
    cur, seen = pid0, set()
    use_proc = proc_root.is_dir()
    allow_fixture_tty = use_proc and proc_root != Path("/proc")
    stop_after_this = False
    for _ in range(32):
        if cur <= 1 or cur in seen:
            break
        seen.add(cur)
        if use_proc:
            pdir = proc_root / str(cur)
            info = _proc_info(pdir)
            if info is None:
                break
            comm, ppid, psid, tty_nr, argv = info
            is_sl = cur == psid
            has_tty = _has_proc_tty(pdir, tty_nr, allow_fixture_tty=allow_fixture_tty)
        else:
            pinfo = _sctx.ps_proc_info(cur)
            if pinfo is None:
                break
            comm, ppid, tty_path, argv, is_sl = pinfo
            has_tty = bool(tty_path)
        if pred(comm, argv):
            return cur, argv
        if _is_test_runner_proc(comm, argv) or stop_after_this or ppid <= 1:
            break
        if is_sl:
            if has_tty:
                break
            stop_after_this = True
        cur = ppid
    return 0, []


def _find_ancestor_cli(pid0: int, proc_root: Path, target_cli: str) -> tuple[int, list[str]]:
    eff_box: list[str] = []

    def _pred(comm: str, argv: list[str]) -> bool:
        cli_k, eff = _sctx._match_cli_process(comm, argv)
        if cli_k == target_cli:
            eff_box[:] = eff
            return True
        return False

    pid, raw_args = _walk_ancestor(pid0, proc_root, _pred)
    return pid, (eff_box if pid else raw_args)


def _find_tty(start_pid: int, proc_root: Path) -> str:
    _ensure_sctx()
    if not proc_root.is_dir():
        cur, seen = start_pid, set()
        for _ in range(32):
            if cur <= 1 or cur in seen:
                break
            seen.add(cur)
            pinfo = _sctx.ps_proc_info(cur)
            if pinfo is None:
                break
            _comm, ppid, tty_path, _argv, is_sess_leader = pinfo
            if tty_path:
                return tty_path
            if is_sess_leader or ppid <= 1:
                break
            cur = ppid
        return ""
    allow_fixture_tty = proc_root != Path("/proc")
    info0 = _proc_info(proc_root / str(start_pid))
    if info0 is None:
        return ""
    origin_psid, cur, seen = info0[2], start_pid, set()
    for _ in range(32):
        if cur <= 1 or cur in seen:
            break
        seen.add(cur)
        pdir = proc_root / str(cur)
        info = _proc_info(pdir)
        if info is None or (origin_psid > 0 and info[2] != origin_psid):
            break
        _, ppid, psid, tty_nr, _ = info
        if tty_nr > 0:
            maj, minr = (tty_nr >> 8) & 0xFFF, (tty_nr & 0xFF) | ((tty_nr >> 12) & 0xFFF00)
            pts = f"/dev/pts/{(maj - 136) * 256 + minr}"
            if 136 <= maj <= 143 and os.path.exists(pts):
                return pts
        for fn in ("0", "2"):
            t = _sctx._readlink_clean(pdir / "fd" / fn)
            if not t or t == "/dev/null":
                continue
            is_dev_tty = t.startswith("/dev/pts/") or (
                t.startswith("/dev/tty") and t != "/dev/tty"
            )
            is_fixture = allow_fixture_tty and os.path.basename(t).lower().startswith(
                ("pts", "tty")
            )
            if (is_dev_tty or is_fixture) and os.path.exists(t):
                return t
        if cur == psid or ppid <= 1:
            break
        cur = ppid
    return ""


def write_osc2(tty_path: str, title: str) -> bool:
    try:
        if not tty_path or not stat.S_ISCHR(os.stat(tty_path).st_mode):
            return False
        fd = os.open(tty_path, os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            os.write(fd, f"\033]2;{_CTRL_RE.sub('', title)}\007".encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except OSError:
        return False


def _escape_tmux_title_arg(title: str) -> str:
    """Escape ``#`` for tmux format expansion and trailing ``;`` for tmux argv parsing."""
    s = _CTRL_RE.sub("", title).replace("#", "##")
    if s.endswith(";"):
        s = s[:-1] + r"\;"
    return s


def parse_tmux_env(env: Mapping[str, str]) -> tuple[str, str]:
    """Return ``(tmux_socket, tmux_pane)`` when ``TMUX_PANE`` is a valid ``%<id>`` pane token."""
    pane = _CTRL_RE.sub("", (env.get("TMUX_PANE") or "").strip())
    if not _tmux._TMUX_PANE_ID_RE.match(pane):
        return "", ""
    sock = _tmux.parse_tmux_socket_from_env(env.get("TMUX") or "")
    return sock, pane


def _resolve_and_sync_tmux_pane(
    env: Mapping[str, str],
    *,
    tty_path: str,
    title: str,
    proc_root: Path,
    explicit_env: bool,
) -> tuple[str, str]:
    """Verify ``TMUX_PANE`` matches ``tty_path`` on live servers and sync ``#{pane_title}``."""
    if not tty_path:
        return "", ""
    if not explicit_env and proc_root != Path("/proc"):
        return "", ""
    sock, pane = parse_tmux_env(env)
    if not pane:
        return "", ""

    is_live_sock = bool(sock and _tmux._is_live_owned_socket(sock))
    if not is_live_sock:
        if explicit_env and proc_root != Path("/proc"):
            return sock, pane
        return "", ""

    tmux_bin = _tmux.resolve_tmux_bin(env)
    if not tmux_bin:
        return "", ""

    sock_arg = _tmux.preferred_tmux_socket_arg(sock)
    child_env = {k: v for k, v in env.items() if k not in ("TMUX", "TMUX_PANE")}
    try:
        probe = subprocess.run(
            [tmux_bin, "-S", sock_arg, "display-message", "-p", "-t", pane, "#{pane_tty}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=child_env,
            timeout=0.4,
            check=False,
        )
        if probe.returncode != 0:
            return "", ""
        pane_tty = probe.stdout.decode("utf-8", errors="ignore").strip()
        if not pane_tty or not _tmux._norm_same_path(pane_tty, tty_path):
            return "", ""
        # Verified in tmux 3.2a: `select-pane -t <pane> -T <title>` updates #{pane_title} only
        # and never changes pane_active or pane_last focus state.
        subprocess.run(
            [
                tmux_bin,
                "-S",
                sock_arg,
                "select-pane",
                "-t",
                pane,
                "-T",
                _escape_tmux_title_arg(title),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=child_env,
            timeout=0.4,
            check=False,
        )
        return sock_arg, pane
    except (OSError, subprocess.SubprocessError):
        return "", ""


def _payload_cwd(payload: Mapping[str, Any]) -> str:
    wp, ws = payload.get("workspacePaths"), payload.get("workspace")
    w0 = wp[0] if isinstance(wp, list) and wp and isinstance(wp[0], str) else ""
    return str(
        payload.get("cwd")
        or w0
        or (ws.get("current_dir") if isinstance(ws, dict) else "")
        or ""
    ).strip()


def update_agy_session(
    payload: Mapping[str, Any],
    *,
    home: Path | None = None,
    proc_root: Path = Path("/proc"),
    runtime_dir: Path | None = None,
    start_pid: int | None = None,
    env: Mapping[str, str] | None = None,
) -> bool:
    eff_env = os.environ if env is None else env
    cid = (
        str(
            payload.get("conversationId")
            or payload.get("conversation_id")
            or payload.get("sessionId")
            or payload.get("session_id")
            or ""
        )
        .strip()
        .lower()
    )
    if not _SID_RE.match(cid):
        return False
    eff_home = home or Path((eff_env.get("HOME") or "").strip() or os.path.expanduser("~"))
    lock_file = eff_home / ".gemini" / "antigravity-cli" / "presence" / f"{cid}.lock"
    if not lock_file.is_file():
        return False
    _ensure_sctx()
    resolved = _sctx._resolve_agy_transcript_path(eff_home, cid)
    raw_tp = str(payload.get("transcriptPath") or payload.get("transcript_path") or "").strip()
    tp_str = (
        str(resolved)
        if resolved
        else (
            raw_tp
            if raw_tp and os.path.isfile(raw_tp)
            else str(_sctx._default_agy_expected_transcript_path(eff_home, cid))
        )
    )
    if os.path.isfile(tp_str) and _sctx._is_agy_transcript_subagent(tp_str):
        return False
    cwd, pid0 = _payload_cwd(payload), (os.getpid() if start_pid is None else start_pid)
    agent_pid = 0
    if proc_root.is_dir():
        tty0 = _find_tty(pid0, proc_root)
        agy_pid, eff_args = _find_ancestor_cli(pid0, proc_root, "agy")
        if not agy_pid or _sctx._is_headless_cli_invocation("agy", eff_args):
            return False
        anc_tty = _find_tty(agy_pid, proc_root)
        if tty0 and anc_tty and not _tmux._norm_same_path(tty0, anc_tty):
            return False
        agy_dir, lock_real, sfx = (
            proc_root / str(agy_pid),
            os.path.realpath(lock_file),
            f"/presence/{cid}.lock",
        )
        try:
            fds = [
                _sctx._readlink_clean(agy_dir / "fd" / fn)
                for fn in os.listdir(agy_dir / "fd")
            ]
        except OSError:
            return False
        if not any(
            t
            and (
                t == str(lock_file)
                or t.replace("\\", "/").endswith(sfx)
                or os.path.realpath(t) == lock_real
            )
            for t in fds
        ):
            return False
        cwd = cwd or _sctx._readlink_clean(agy_dir / "cwd")
        tty_path = anc_tty or tty0
        agent_pid = agy_pid
    else:
        if not _sctx.is_agy_presence_lock_held_nonproc(lock_file):
            return False
        tty0 = _find_tty(pid0, proc_root)
        agy_pid, eff_args = _find_ancestor_cli(pid0, proc_root, "agy")
        tty_path = tty0
        if agy_pid:
            anc_tty = _find_tty(agy_pid, proc_root)
            if not (tty0 and anc_tty and not _tmux._norm_same_path(tty0, anc_tty)):
                if _sctx._is_headless_cli_invocation("agy", eff_args):
                    return False
                cwd = cwd or _sctx._query_pid_cwd_nonproc(agy_pid)
                agent_pid = agy_pid
                tty_path = anc_tty or tty0
    host_sfx = _resolve_ssh_host_tag(eff_env, explicit_env=(env is not None), proc_root=proc_root)
    title = f"agy · {_short_cwd(cwd, str(eff_home))} [#{cid[:8]}]{host_sfx}"
    tmux_sock, tmux_pane = _resolve_and_sync_tmux_pane(
        eff_env,
        tty_path=tty_path,
        title=title,
        proc_root=proc_root,
        explicit_env=(env is not None),
    )
    rt = runtime_dir if runtime_dir is not None else get_runtime_dir(eff_env, create=False)
    _sctx.write_session_registry_entry(
        rt,
        cli="agy",
        session_id=cid,
        jsonl_path=tp_str,
        cwd=cwd,
        tmux_pane=tmux_pane,
        tmux_socket=tmux_sock,
        tty=tty_path,
        agent_pid=agent_pid,
    )
    write_osc2(tty_path, title)
    return True


def update_claude_session(
    payload: Mapping[str, Any],
    *,
    home: Path | None = None,
    proc_root: Path = Path("/proc"),
    runtime_dir: Path | None = None,
    start_pid: int | None = None,
    env: Mapping[str, str] | None = None,
) -> bool:
    eff_env = os.environ if env is None else env
    sid = str(payload.get("session_id") or payload.get("sessionId") or "").strip().lower()
    if (payload.get("agent_id") or payload.get("agentId") or "") or not _SID_RE.match(sid):
        return False
    _ensure_sctx()
    eff_home = home or Path((eff_env.get("HOME") or "").strip() or os.path.expanduser("~"))
    cwd = _payload_cwd(payload)
    tp = str(payload.get("transcript_path") or payload.get("transcriptPath") or "").strip()
    if not tp and cwd:
        encs = _sctx.encode_claude_project_dir(cwd)
        tp = str(eff_home / ".claude" / "projects" / encs[0] / f"{sid}.jsonl") if encs else ""
    if tp and os.path.basename(tp).lower().startswith("agent-"):
        return False
    if (
        tp
        and os.path.isfile(tp)
        and os.path.getsize(tp) > 0
        and not _sctx._read_claude_root_meta(tp, allow_empty_or_meta_only=True)[0]
    ):
        return False
    pid0 = os.getpid() if start_pid is None else start_pid
    agent_pid = 0
    tty0 = _find_tty(pid0, proc_root)
    tty_path = tty0
    cl_pid, eff_args = _find_ancestor_cli(pid0, proc_root, "claude")
    if cl_pid:
        anc_tty = _find_tty(cl_pid, proc_root)
        # Ignore ancestor across a setsid + fresh PTY boundary (e.g. isolated PTY unit tests)
        if not (tty0 and anc_tty and not _tmux._norm_same_path(tty0, anc_tty)):
            if _sctx._is_headless_cli_invocation("claude", eff_args):
                return False
            agent_pid = cl_pid
            tty_path = anc_tty or tty0
            if not cwd:
                cwd = (
                    _sctx._readlink_clean(proc_root / str(cl_pid) / "cwd")
                    if proc_root.is_dir()
                    else _sctx._query_pid_cwd_nonproc(cl_pid)
                )
                if not tp and cwd:
                    encs = _sctx.encode_claude_project_dir(cwd)
                    tp = (
                        str(eff_home / ".claude" / "projects" / encs[0] / f"{sid}.jsonl")
                        if encs
                        else ""
                    )
    host_sfx = _resolve_ssh_host_tag(eff_env, explicit_env=(env is not None), proc_root=proc_root)
    title = f"claude · {_short_cwd(cwd, str(eff_home))} [#{sid[:8]}]{host_sfx}"
    tmux_sock, tmux_pane = _resolve_and_sync_tmux_pane(
        eff_env,
        tty_path=tty_path,
        title=title,
        proc_root=proc_root,
        explicit_env=(env is not None),
    )
    if tp:
        rt = runtime_dir if runtime_dir is not None else get_runtime_dir(eff_env, create=False)
        _sctx.write_session_registry_entry(
            rt,
            cli="claude",
            session_id=sid,
            jsonl_path=tp,
            cwd=cwd,
            tmux_pane=tmux_pane,
            tmux_socket=tmux_sock,
            tty=tty_path,
            agent_pid=agent_pid,
        )
    write_osc2(tty_path, title)
    return True


def run_session_hook(
    cli_mode: str = "auto",
    *,
    stdin_text: str | None = None,
    quiet_stdout: bool = False,
    env: Mapping[str, str] | None = None,
) -> int:
    """Execute hook update safely (never fails caller hook; respects per-CLI stdout contract)."""
    mode = (cli_mode or "auto").strip().lower()
    emit_json = mode in ("agy", "gemini")
    try:
        raw = sys.stdin.read() if stdin_text is None else stdin_text
        data = json.loads(raw) if raw.strip() else {}
        if isinstance(data, dict):
            if mode in ("agy", "gemini"):
                update_agy_session(data, env=env)
            elif mode == "claude":
                update_claude_session(data, env=env)
            else:
                if "conversationId" in data or "conversation_id" in data:
                    emit_json = True
                    update_agy_session(data, env=env)
                elif "session_id" in data or "sessionId" in data:
                    emit_json = False
                    update_claude_session(data, env=env)
    except Exception:
        pass
    if emit_json and not quiet_stdout:
        sys.stdout.write("{}\n")
        sys.stdout.flush()
    return 0


def main() -> None:
    run_session_hook("agy")


if __name__ == "__main__":
    main()
