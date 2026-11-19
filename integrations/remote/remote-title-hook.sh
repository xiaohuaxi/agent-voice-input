#!/usr/bin/env bash
# Lightweight standalone OSC 2 title hook for headless remote Linux hosts without agent-voice-input installed.
# Usage:
#   Claude Code (~/.claude/settings.json):  ~/.local/bin/remote-title-hook.sh claude
#   Antigravity CLI (~/.gemini/hooks.json): ~/.local/bin/remote-title-hook.sh agy
set -u
exec python3 -c '
import json, os, re, shutil, socket, stat, subprocess, sys

mode = sys.argv[1].strip().lower()
emit_json = mode in ("agy", "gemini")
try:
    raw = sys.stdin.read(131072)
    d = json.loads(raw) if raw.strip() else {}
    tp = os.path.basename(str(d.get("transcript_path") or "")).lower() if isinstance(d, dict) else ""
    if (
        isinstance(d, dict)
        and not (d.get("agent_id") or d.get("agentId") or d.get("isSidechain"))
        and not tp.startswith("agent-")
    ):
        cid = str(d.get("conversationId") or d.get("conversation_id") or "").strip().lower()
        sid = str(d.get("session_id") or d.get("sessionId") or "").strip().lower()
        if mode == "auto":
            emit_json = bool(cid)
        tok = cid if emit_json else (sid or cid)
        cli = "agy" if emit_json else "claude"
        if re.match(r"^[0-9a-f]{8}[0-9a-f-]{0,56}$", tok):
            host = (os.environ.get("AGENT_VOICE_INPUT_HOST_TAG") or socket.gethostname()).strip().lower().split(".", 1)[0]
            host = re.sub(r"[^a-z0-9._-]", "", host)[:64]
            wp = d.get("workspacePaths")
            cwd = str(d.get("cwd") or (wp[0] if isinstance(wp, list) and wp else "") or "").strip().rstrip("/")
            base = os.path.basename(cwd) or "~"
            title = re.sub(r"[\x00-\x1f\x7f]", "", f"{cli} · {base} [#{tok[:8]}]" + (f" [@{host}]" if host else ""))
            tty = (os.environ.get("AVI_TEST_TTY_PATH") or "").strip()
            if not tty:
                cur = os.getppid()
                for _ in range(24):
                    if cur <= 1:
                        break
                    try:
                        st_s = open(f"/proc/{cur}/stat", encoding="utf-8", errors="replace").read()
                    except OSError:
                        break
                    f = st_s[st_s.rfind(")") + 2 :].split()
                    tnr = int(f[4])
                    if tnr > 0 and 136 <= ((tnr >> 8) & 0xFFF) <= 143:
                        cand = f"/dev/pts/{(((tnr >> 8) & 0xFFF) - 136) * 256 + ((tnr & 0xFF) | ((tnr >> 12) & 0xFFF00))}"
                        if os.path.exists(cand):
                            tty = cand
                            break
                    cur = int(f[1])
            if tty and stat.S_ISCHR(os.stat(tty).st_mode):
                fd = os.open(tty, os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
                try:
                    os.write(fd, f"\033]2;{title}\007".encode("utf-8"))
                finally:
                    os.close(fd)
                pane = (os.environ.get("TMUX_PANE") or "").strip()
                if re.match(r"^%\d+$", pane):
                    esc = title.replace("#", "##")
                    tbin = shutil.which("tmux") or next((p for p in ("/usr/bin/tmux", "/usr/local/bin/tmux", "/opt/homebrew/bin/tmux", os.path.expanduser("~/.local/bin/tmux")) if os.path.isfile(p) and os.access(p, os.X_OK)), "tmux")
                    subprocess.run([tbin, "select-pane", "-t", pane, "-T", esc], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=0.4)
except Exception:
    pass
if emit_json:
    sys.stdout.write("{}\n")
' "${1:-auto}"
