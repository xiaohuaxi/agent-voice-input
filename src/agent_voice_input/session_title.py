"""Window title (OSC 2) and runtime session registry helper for Claude Code and Antigravity CLI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any, Callable, Mapping

from . import session_ctx as _sctx
from .config import get_runtime_dir

_SID_RE = re.compile(r"^[0-9a-f]{8}[0-9a-f-]{0,56}$")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
_VER_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?$")


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


def _walk_ancestor(
    pid0: int, proc_root: Path, pred: Callable[[str, list[str]], bool]
) -> tuple[int, list[str]]:
    cur, seen = pid0, set()
    for _ in range(32):
        if cur <= 1 or cur in seen:
            break
        seen.add(cur)
        info = _proc_info(proc_root / str(cur))
        if info is None:
            break
        if pred(info[0], info[4]):
            return cur, info[4]
        cur = info[1]
    return 0, []


def _find_tty(start_pid: int, proc_root: Path) -> str:
    if not proc_root.is_dir():
        try:
            r = subprocess.run(
                ["ps", "-o", "tty=", "-p", str(start_pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=1.0,
                check=False,
            )
            tty = r.stdout.decode("utf-8", errors="ignore").strip()
            return (
                (tty if tty.startswith("/dev/") else f"/dev/{tty}")
                if tty and tty not in ("?", "??")
                else ""
            )
        except (OSError, subprocess.SubprocessError):
            return ""
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
            if (
                t.startswith("/dev/pts/") or (t.startswith("/dev/tty") and t != "/dev/tty")
            ) and os.path.exists(t):
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
    if proc_root.is_dir():
        agy_pid, args = _walk_ancestor(
            pid0,
            proc_root,
            lambda c, a: c == "agy" or bool(a and os.path.basename(a[0]) == "agy"),
        )
        if not agy_pid or _sctx._is_headless_cli_invocation("agy", args):
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
        tty_path = _find_tty(agy_pid, proc_root)
    else:
        if not _sctx.is_agy_presence_lock_held_nonproc(lock_file):
            return False
        tty_path = _find_tty(pid0, proc_root)
    rt = runtime_dir if runtime_dir is not None else get_runtime_dir(eff_env, create=False)
    _sctx.write_session_registry_entry(
        rt, cli="agy", session_id=cid, jsonl_path=tp_str, cwd=cwd
    )
    write_osc2(tty_path, f"agy · {_short_cwd(cwd, str(eff_home))} [#{cid[:8]}]")
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
    target_pid = pid0
    if proc_root.is_dir():
        cl_pid, args = _walk_ancestor(
            pid0,
            proc_root,
            lambda c, a: c == "claude"
            or (
                (c == "node" or bool(_VER_RE.match(c)))
                and any(os.path.basename(x) == "claude" or "/claude/versions/" in x for x in a[:8])
            ),
        )
        if cl_pid:
            if _sctx._is_headless_cli_invocation("claude", args):
                return False
            target_pid = cl_pid
            cwd = cwd or _sctx._readlink_clean(proc_root / str(cl_pid) / "cwd")
    if tp:
        rt = runtime_dir if runtime_dir is not None else get_runtime_dir(eff_env, create=False)
        _sctx.write_session_registry_entry(
            rt, cli="claude", session_id=sid, jsonl_path=tp, cwd=cwd
        )
    write_osc2(_find_tty(target_pid, proc_root), f"claude · {_short_cwd(cwd, str(eff_home))} [#{sid[:8]}]")
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
