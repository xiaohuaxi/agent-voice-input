"""Safe, idempotent CLI integration manager for Claude Code, Codex CLI, and Antigravity CLI.

Pure Python 3.10+ standard library implementation (depends only on ``.config``).
Preserves dotfiles symlinks by atomically replacing the resolved target file in-place,
writes pre-edit backups under ``<cache_dir>/integrate-backups/<timestamp>/`` (0700/0600),
and edits Codex ``config.toml`` via targeted section-level patching validated by ``parse_toml``.
"""
from __future__ import annotations

import dataclasses, json, os, re, shutil, stat, sys, time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .config import ConfigError, ensure_secure_dir, expand_user_path, parse_toml

CLIS: tuple[str, ...] = ("claude", "codex", "agy")
CLAUDE_HOOK_EVENTS: tuple[str, ...] = ("SessionStart", "UserPromptSubmit")
CLAUDE_HOOK_COMMAND = (
    'sh -c \'b=$(command -v agent-voice-input 2>/dev/null || true); '
    '[ -x "$b" ] || b="$HOME/.local/bin/agent-voice-input"; '
    '[ -x "$b" ] || b="/opt/homebrew/bin/agent-voice-input"; '
    '[ -x "$b" ] && "$b" session-hook --cli claude --quiet >/dev/null 2>&1 || true\''
)
AGY_NAMESPACE_KEY = "voice-session-title"
AGY_HOOK_COMMAND = (
    'sh -c \'b=$(command -v agent-voice-input 2>/dev/null || true); '
    '[ -x "$b" ] || b="$HOME/.local/bin/agent-voice-input"; '
    '[ -x "$b" ] || b="/opt/homebrew/bin/agent-voice-input"; '
    'if [ -x "$b" ]; then "$b" session-hook --cli agy --quiet >/dev/null 2>&1 || true; fi; printf "{}\\n"\''
)
CODEX_THREAD_ID_TOKEN = "thread-id"
CODEX_DEFAULT_TERMINAL_TITLE: tuple[str, ...] = ("spinner", "project-name", "thread-title", CODEX_THREAD_ID_TOKEN)
_CODEX_DEFAULT_LINE = 'terminal_title = ["spinner", "project-name", "thread-title", "thread-id"]'

_KNOWN_HOOK_EVENTS: frozenset[str] = frozenset(
    {
        "SessionStart", "SessionEnd", "UserPromptSubmit", "PreInvocation", "PostInvocation",
        "PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest", "Notification",
        "Stop", "SubagentStart", "SubagentStop", "PreCompact", "BeforeAgent", "AfterAgent",
        "BeforeModel", "AfterModel", "BeforeTool", "AfterTool", "BeforeToolSelection", "PreCompress",
    }
)
_TOML_TUI_HEADER_RE = re.compile(r"^\s*\[\s*tui\s*\]\s*(?:#.*)?$")
_TOML_ANY_HEADER_RE = re.compile(r"^\s*\[")
_TOML_TITLE_RE = re.compile(r"^(\s*terminal_title\s*=\s*)(.*)$")


@dataclass(frozen=True)
class IntegrationStatus:
    cli: str
    installed: bool
    configured: bool
    config_path: str
    resolved_path: str
    is_symlink: bool
    necessity: str
    note: str
    detected_via: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class IntegrationAction:
    cli: str
    action: str
    config_path: str
    resolved_path: str
    is_symlink: bool
    backup_path: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _is_managed_voice_hook(val: Any) -> bool:
    return isinstance(val, str) and "agent-voice-input" in val and "session-hook" in val


def _is_any_voice_hook(val: Any) -> bool:
    return _is_managed_voice_hook(val) or (isinstance(val, str) and "voice-title.sh" in val)


def _event_entries_have_voice_hook(entries: Any) -> bool:
    if not isinstance(entries, list):
        return False
    for item in entries:
        if not isinstance(item, Mapping):
            continue
        if _is_any_voice_hook(item.get("command")):
            return True
        nested = item.get("hooks")
        if isinstance(nested, list) and any(
            isinstance(sub, Mapping) and _is_any_voice_hook(sub.get("command")) for sub in nested
        ):
            return True
    return False


def _hook_map_has_voice_hook(obj: Any) -> bool:
    if not isinstance(obj, Mapping):
        return False
    for key, val in obj.items():
        if key in _KNOWN_HOOK_EVENTS and _event_entries_have_voice_hook(val):
            return True
        if isinstance(val, Mapping) and any(
            sk in _KNOWN_HOOK_EVENTS and _event_entries_have_voice_hook(sv) for sk, sv in val.items()
        ):
            return True
    return False


def has_codex_voice_title(parsed_toml: Mapping[str, Any]) -> bool:
    """Return True if parsed Codex ``config.toml`` has ``[tui].terminal_title`` thread-id or legacy ``notify``."""
    if not isinstance(parsed_toml, Mapping):
        return False
    tui = parsed_toml.get("tui")
    if isinstance(tui, Mapping):
        tt = tui.get("terminal_title")
        if isinstance(tt, list) and any(x == CODEX_THREAD_ID_TOKEN for x in tt if isinstance(x, str)):
            return True
    notify = parsed_toml.get("notify")
    if isinstance(notify, str):
        return _is_any_voice_hook(notify) or "agent-voice-input" in notify
    if isinstance(notify, list):
        return any(isinstance(t, str) and (_is_any_voice_hook(t) or "agent-voice-input" in t) for t in notify)
    return False


def _resolve_home(home_dir: str | Path | None, env: Mapping[str, str] | None) -> Path:
    if home_dir is not None:
        return Path(home_dir)
    if env is not None and (raw := (env.get("HOME") or "").strip()):
        return Path(raw)
    return Path.home()


def _resolve_cache_dir(
    cache_dir: str | Path | None,
    home: Path,
    env: Mapping[str, str] | None,
    *,
    home_explicit: bool = False,
) -> Path:
    if cache_dir is not None:
        return Path(cache_dir)
    src_env = None if (home_explicit and env is None) else (os.environ if env is None else env)
    if src_env is not None and (xdg := (src_env.get("XDG_CACHE_HOME") or "").strip()):
        return expand_user_path(xdg, env=src_env, home_dir=home) / "agent-voice-input"
    return home / ".cache" / "agent-voice-input"


def _detect_platform(platform: str | None, env: Mapping[str, str] | None) -> str:
    if platform is not None:
        low = platform.strip().lower()
        return "darwin" if low in ("darwin", "macos") else ("wsl" if "wsl" in low else (low or "linux"))
    if sys.platform == "darwin":
        return "darwin"
    src_env = os.environ if env is None else env
    return "wsl" if (src_env.get("WSL_DISTRO_NAME") or src_env.get("WSL_INTEROP")) else sys.platform


def _platform_necessity(plat: str) -> tuple[str, str]:
    if plat == "darwin":
        return (
            "optional",
            "Optional on macOS: Ghostty, Terminal.app, iTerm2, and tmux work without hooks; "
            "useful for other terminals (WezTerm/Kitty/Alacritty/Warp) with same-dir multi-windows.",
        )
    if plat == "wsl":
        return (
            "recommended",
            "Recommended on WSL2 for multiple same-dir Windows Terminal tabs without tmux; inside tmux hooks are optional.",
        )
    return ("optional", "Optional: inside tmux works without hooks; useful for multi-window terminals sharing the same directory.")


def _cli_paths(cli: str, home: Path, env: Mapping[str, str] | None, home_explicit: bool) -> tuple[Path, Path, tuple[Path, ...]]:
    if cli == "claude":
        d = home / ".claude"
        return d, d / "settings.json", ()
    if cli == "codex":
        src_env = None if home_explicit else (os.environ if env is None else env)
        ch = "" if src_env is None else (src_env.get("CODEX_HOME") or "").strip()
        d = expand_user_path(ch, env=src_env, home_dir=home) if ch else (home / ".codex")
        return d, d / "config.toml", ()
    d = home / ".gemini"
    return d, d / "config" / "hooks.json", (d / "hooks.json", d / "settings.json")


def _resolve_target_file(target: Path, *, for_write: bool) -> tuple[Path, bool, bool, str]:
    try:
        lst = os.lstat(target)
    except FileNotFoundError:
        return target, False, False, ""
    except OSError as exc:
        return target, False, False, f"cannot inspect {target}: {exc}"
    is_link = stat.S_ISLNK(lst.st_mode)
    if is_link:
        try:
            st, real = os.stat(target), target.resolve(strict=True)
        except FileNotFoundError:
            return target, True, False, f"dangling symlink: {target}"
        except OSError as exc:
            return target, True, False, f"cannot resolve symlink {target}: {exc}"
    else:
        st, real = lst, target
    if not stat.S_ISREG(st.st_mode):
        return real, is_link, True, f"target is not a regular file: {real}"
    if for_write and st.st_uid != os.getuid():
        return real, is_link, True, f"refusing to modify file owned by uid={st.st_uid}: {real}"
    return real, is_link, True, ""


def _backup_existing_file(real_path: Path, *, cli: str, cache_dir: Path) -> Path:
    ensure_secure_dir(cache_dir)
    backups_root = ensure_secure_dir(cache_dir / "integrate-backups")
    stamp_dir = ensure_secure_dir(backups_root / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    raw_bytes = real_path.read_bytes()
    idx = 0
    while True:
        dst = stamp_dir / (f"{cli}-{real_path.name}" if idx == 0 else f"{cli}-{real_path.name}.{idx}")
        try:
            fd = os.open(str(dst), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            break
        except FileExistsError:
            idx += 1
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(raw_bytes)
        f.flush()
    return dst


def _atomic_write_text(real_path: Path, content: str, *, mode: int) -> None:
    real_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = real_path.parent / f".{real_path.name}.avi.{os.getpid()}.{time.monotonic_ns()}.tmp"
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            fd = -1
            f.write(content)
            f.flush()
        os.replace(tmp_path, real_path)
    except Exception:
        if fd >= 0:  # pragma: no cover - fdopen failure guard
            os.close(fd)
        tmp_path.unlink(missing_ok=True)
        raise


def _read_json_dict(real_path: Path) -> tuple[dict[str, Any] | None, str]:
    try:
        parsed = json.loads(real_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"invalid or unreadable JSON in {real_path}: {exc}"
    return (parsed, "") if isinstance(parsed, dict) else (None, f"expected JSON object at root of {real_path}")


def _claude_merge(data: dict[str, Any]) -> tuple[dict[str, Any] | None, bool, str]:
    out = json.loads(json.dumps(data))
    hooks = out.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        return None, False, "'hooks' in settings.json must be a JSON object"
    changed = False
    for event in CLAUDE_HOOK_EVENTS:
        entries = hooks.get(event)
        if entries is None:
            hooks[event] = [{"hooks": [{"type": "command", "command": CLAUDE_HOOK_COMMAND}]}]
            changed = True
        elif not isinstance(entries, list):
            return None, False, f"'hooks.{event}' in settings.json must be a JSON array"
        elif not _event_entries_have_voice_hook(entries):
            entries.append({"hooks": [{"type": "command", "command": CLAUDE_HOOK_COMMAND}]})
            changed = True
    return out, changed, ""


def _claude_uninstall(data: dict[str, Any]) -> tuple[dict[str, Any] | None, bool, str]:
    out = json.loads(json.dumps(data))
    hooks = out.get("hooks")
    if hooks is None:
        return out, False, ""
    if not isinstance(hooks, dict):
        return None, False, "'hooks' in settings.json must be a JSON object"
    removed = False
    for event in list(hooks.keys()):
        entries = hooks[event]
        if not isinstance(entries, list):
            continue
        new_entries: list[Any] = []
        ev_removed = False
        for item in entries:
            if isinstance(item, dict) and _is_managed_voice_hook(item.get("command")):
                ev_removed = True
                continue
            if isinstance(item, dict) and isinstance(item.get("hooks"), list):
                kept = [s for s in item["hooks"] if not (isinstance(s, dict) and _is_managed_voice_hook(s.get("command")))]
                if len(kept) != len(item["hooks"]):
                    ev_removed = True
                    if kept:
                        item["hooks"] = kept
                        new_entries.append(item)
                    continue
            new_entries.append(item)
        if ev_removed:
            removed = True
            if new_entries:
                hooks[event] = new_entries
            else:
                del hooks[event]
    if removed and not hooks:
        del out["hooks"]
    return out, removed, ""


def _agy_namespace_entry() -> dict[str, Any]:
    return {
        "SessionStart": [{"type": "command", "command": AGY_HOOK_COMMAND, "timeout": 5}],
        "PreInvocation": [{"type": "command", "command": AGY_HOOK_COMMAND, "timeout": 5}],
    }


def _find_legacy_agy_hook(legacy_files: tuple[Path, ...]) -> Path | None:
    for cand in legacy_files:
        real, _, exists, err = _resolve_target_file(cand, for_write=False)
        if err or not exists:
            continue
        parsed, jerr = _read_json_dict(real)
        if parsed is not None and not jerr and (
            _hook_map_has_voice_hook(parsed.get("hooks"))
            or (cand.name.lower() == "hooks.json" and _hook_map_has_voice_hook(parsed))
        ):
            return cand
    return None


def _skip_toml_quoted(s: str, i: int) -> int:
    q, n = s[i], len(s)
    i += 1
    while i < n:
        if q == '"' and s[i] == "\\":
            i += 2
        elif s[i] == q:
            return i + 1
        else:
            i += 1
    return n


def _locate_tui_and_title(lines: list[str]) -> tuple[tuple[int, int] | None, int | None]:
    """Locate ``[tui]`` section ``(start, end)`` and ``terminal_title = ...`` line index outside multiline strings."""
    in_delim: str | None = None
    sec_start: int | None = None
    sec_end = len(lines)
    key_idx: int | None = None
    for idx, line in enumerate(lines):
        if in_delim is None:
            if sec_start is None:
                if _TOML_TUI_HEADER_RE.match(line):
                    sec_start = idx
            elif sec_end == len(lines):
                if _TOML_ANY_HEADER_RE.match(line):
                    sec_end = idx
                elif key_idx is None and _TOML_TITLE_RE.match(line):
                    key_idx = idx
        i, n = 0, len(line)
        while i < n:
            if in_delim is not None:
                if in_delim == '"""' and line[i] == "\\":
                    i += 2
                elif line.startswith(in_delim, i):
                    in_delim = None
                    i += 3
                else:
                    i += 1
            elif line[i] == "#":
                break
            elif line.startswith(('"""', "'''"), i):
                in_delim, i = line[i : i + 3], i + 3
            elif line[i] in ('"', "'"):
                i = _skip_toml_quoted(line, i)
            else:
                i += 1
    return ((sec_start, sec_end) if sec_start is not None else None), key_idx


def _split_single_line_toml_array(rhs: str) -> tuple[str, str] | None:
    s = rhs.lstrip()
    if not s.startswith("["):
        return None
    depth = i = 0
    while i < len(s):
        ch = s[i]
        if ch in ('"', "'"):
            i = _skip_toml_quoted(s, i)
            continue
        if ch == "#":
            return None
        if ch == "[":
            depth += 1
            if depth > 1:
                return None
        elif ch == "]":
            depth -= 1
            if depth == 0:
                tail = s[i + 1 :]
                return (s[1:i], tail) if (not tail.strip() or tail.strip().startswith("#")) else None
        i += 1
    return None


def _find_simple_multiline_end(lines: list[str], key_idx: int, end_idx: int) -> int | None:
    m = _TOML_TITLE_RE.match(lines[key_idx])
    if m is None or m.group(2).strip() != "[":
        return None
    for idx in range(key_idx + 1, end_idx):
        s = lines[idx].strip()
        if not s:
            continue
        if s.startswith("]"):
            after = s[1:].strip()
            return idx if (not after or after.startswith("#")) else None
        # Reject inline comments or non-string-literal items (fail-closed)
        i = 0
        while i < len(s):
            if s[i] in " \t,":
                i += 1
            elif s[i] in ('"', "'"):
                end = _skip_toml_quoted(s, i)
                if end - i < 2 or s[end - 1] != s[i]:
                    return None
                i = end
            else:
                return None
    return None


def _patch_codex_toml(raw_text: str, *, uninstall: bool) -> tuple[str | None, bool, str]:
    action_verb = "remove" if uninstall else "add"
    hint = f'please {action_verb} "thread-id" in [tui].terminal_title in ~/.codex/config.toml manually'
    try:
        parsed = parse_toml(raw_text) if raw_text.strip() else {}
    except ConfigError as exc:
        return None, False, f"invalid TOML in config.toml: {exc}"

    tui = parsed.get("tui")
    tt = tui.get("terminal_title") if isinstance(tui, Mapping) else None
    has_tid = isinstance(tt, list) and any(x == CODEX_THREAD_ID_TOKEN for x in tt if isinstance(x, str))
    if uninstall:
        if not has_tid:
            return raw_text, False, ""
    else:
        if has_tid:
            return raw_text, False, ""
        if tt is not None and not isinstance(tt, list):
            return None, False, f"[tui].terminal_title is not an array; {hint}"

    nl = "\r\n" if "\r\n" in raw_text else "\n"
    lines = raw_text.splitlines(keepends=True)
    sec, key_idx = _locate_tui_and_title(lines)
    new_lines = list(lines)

    if not uninstall:
        if sec is None:
            if not raw_text.strip():
                candidate = f"[tui]{nl}{_CODEX_DEFAULT_LINE}{nl}"
            else:
                prefix = raw_text if raw_text.endswith(("\n", "\r\n")) else raw_text + nl
                if not prefix.endswith((nl + nl, "\n\n")):
                    prefix += nl
                candidate = f"{prefix}[tui]{nl}{_CODEX_DEFAULT_LINE}{nl}"
        elif key_idx is None:
            start_idx = sec[0]
            if not new_lines[start_idx].endswith(("\n", "\r\n")):
                new_lines[start_idx] += nl
            new_lines.insert(start_idx + 1, f"{_CODEX_DEFAULT_LINE}{nl}")
            candidate = "".join(new_lines)
        else:
            line = lines[key_idx]
            line_nl = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
            m = _TOML_TITLE_RE.match(line[: -len(line_nl)] if line_nl else line)
            assert m is not None
            prefix_eq, rhs = m.group(1), m.group(2)
            single = _split_single_line_toml_array(rhs)
            if single is not None:
                inner, tail = single
                if not inner.strip():
                    new_inner = f'"{CODEX_THREAD_ID_TOKEN}"'
                elif inner.rstrip().endswith(","):
                    new_inner = f'{inner.rstrip()} "{CODEX_THREAD_ID_TOKEN}"'
                else:
                    new_inner = f'{inner.rstrip()}, "{CODEX_THREAD_ID_TOKEN}"'
                new_lines[key_idx] = f"{prefix_eq}[{new_inner}]{tail}{line_nl or nl}"
            else:
                close_idx = _find_simple_multiline_end(lines, key_idx, sec[1])
                if close_idx is None:
                    return None, False, f"cannot safely patch complex multiline/commented [tui].terminal_title; {hint}"
                prev_idx = close_idx - 1
                while prev_idx > key_idx and not new_lines[prev_idx].strip():
                    prev_idx -= 1
                indent = "  "
                if prev_idx > key_idx:
                    pl = new_lines[prev_idx]
                    indent = pl[: len(pl) - len(pl.lstrip(" \t"))] or "  "
                    pnl = "\r\n" if pl.endswith("\r\n") else ("\n" if pl.endswith("\n") else nl)
                    pbody = pl.rstrip("\r\n")
                    if not pbody.rstrip().endswith(","):
                        new_lines[prev_idx] = f"{pbody},{pnl}"
                new_lines.insert(close_idx, f'{indent}"{CODEX_THREAD_ID_TOKEN}",{nl}')
            candidate = "".join(new_lines)
    else:
        if sec is None or key_idx is None:
            return None, False, f"cannot locate [tui].terminal_title line; {hint}"
        start_idx, end_idx = sec
        line = lines[key_idx]
        line_nl = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
        m = _TOML_TITLE_RE.match(line[: -len(line_nl)] if line_nl else line)
        assert m is not None
        prefix_eq, rhs = m.group(1), m.group(2)
        single = _split_single_line_toml_array(rhs)
        if tuple(tt) == CODEX_DEFAULT_TERMINAL_TITLE:
            if single is not None:
                del new_lines[key_idx]
                sec_end = end_idx - 1
            else:
                close_idx = _find_simple_multiline_end(lines, key_idx, end_idx)
                if close_idx is None:
                    return None, False, f"cannot safely remove multiline terminal_title; {hint}"
                del new_lines[key_idx : close_idx + 1]
                sec_end = end_idx - (close_idx - key_idx + 1)
            if all(not ln.strip() for ln in new_lines[start_idx + 1 : sec_end]):
                del new_lines[start_idx:sec_end]
                if start_idx > 0 and not new_lines[start_idx - 1].strip() and (
                    start_idx == len(new_lines) or _TOML_ANY_HEADER_RE.match(new_lines[start_idx])
                ):
                    del new_lines[start_idx - 1]
        else:
            if single is None:
                return None, False, f"cannot safely edit complex multiline terminal_title; {hint}"
            kept = [x for x in tt if not (isinstance(x, str) and x == CODEX_THREAD_ID_TOKEN)]
            new_lines[key_idx] = f"{prefix_eq}{json.dumps(kept, ensure_ascii=False)}{single[1]}{line_nl}"
        candidate = "".join(new_lines)

    try:
        validated = parse_toml(candidate) if candidate.strip() else {}
    except ConfigError as exc:
        return None, False, f"patched TOML failed validation ({exc}); {hint}"
    vtui = validated.get("tui")
    vtt = vtui.get("terminal_title") if isinstance(vtui, Mapping) else None
    now_has = isinstance(vtt, list) and CODEX_THREAD_ID_TOKEN in vtt
    if now_has == uninstall:  # pragma: no cover - defensive post-check
        return None, False, f"post-patch validation mismatch; {hint}"
    return candidate, True, ""


def _normalize_targets(targets: str | Sequence[str]) -> tuple[bool, tuple[str, ...]]:
    items = (targets.strip().lower(),) if isinstance(targets, str) else tuple(str(t).strip().lower() for t in targets)
    if items == ("all",):
        return True, CLIS
    if not items:
        raise ValueError(f"targets must not be empty (expected 'all' or subset of {CLIS})")
    seen: list[str] = []
    for item in items:
        if item not in CLIS:
            raise ValueError(f"unsupported CLI target {item!r} (expected one of {CLIS} or 'all')")
        if item not in seen:
            seen.append(item)
    return False, tuple(seen)


def inspect_integrations(
    *,
    home_dir: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    which_fn: Callable[[str], str | None] | None = None,
) -> list[IntegrationStatus]:
    """Read-only inspection of Claude Code, Codex CLI, and Antigravity CLI hook/title status."""
    home = _resolve_home(home_dir, env)
    necessity, note = _platform_necessity(_detect_platform(platform, env))
    which = shutil.which if which_fn is None else which_fn
    results: list[IntegrationStatus] = []

    for cli in CLIS:
        cfg_dir, primary, legacy_files = _cli_paths(cli, home, env, home_dir is not None)
        installed = bool(which(cli)) or cfg_dir.exists() or cfg_dir.is_symlink()
        real, is_link, exists, err = _resolve_target_file(primary, for_write=False)
        configured, detected_via = False, ""
        if err:
            detected_via = "invalid_config"
        elif cli == "claude" and exists:
            parsed, jerr = _read_json_dict(real)
            if jerr or parsed is None:
                err, detected_via = jerr, "invalid_config"
            elif _hook_map_has_voice_hook(parsed.get("hooks")):
                configured = True
                hooks = parsed.get("hooks", {})
                missing = [ev for ev in CLAUDE_HOOK_EVENTS if not _event_entries_have_voice_hook(hooks.get(ev))]
                detected_via = f"settings.json (partial: missing {', '.join(missing)})" if missing else "settings.json"
        elif cli == "agy":
            if exists:
                parsed, jerr = _read_json_dict(real)
                if jerr or parsed is None:
                    err, detected_via = jerr, "invalid_config"
                elif _hook_map_has_voice_hook(parsed) or _hook_map_has_voice_hook(parsed.get("hooks")):
                    configured, detected_via = True, "config/hooks.json"
            if not configured and not err and (hit := _find_legacy_agy_hook(legacy_files)) is not None:
                configured, detected_via = True, f"legacy:{hit}"
        elif cli == "codex" and exists:
            try:
                raw = real.read_text(encoding="utf-8")
                parsed_toml = parse_toml(raw) if raw.strip() else {}
            except (OSError, ConfigError) as exc:
                err, detected_via = f"invalid TOML in {real}: {exc}", "invalid_config"
            else:
                if has_codex_voice_title(parsed_toml):
                    configured = True
                    tui = parsed_toml.get("tui")
                    tt = tui.get("terminal_title") if isinstance(tui, Mapping) else None
                    detected_via = "terminal_title" if (isinstance(tt, list) and CODEX_THREAD_ID_TOKEN in tt) else "legacy:notify"
        results.append(IntegrationStatus(cli, installed, configured, str(primary), str(real), is_link, necessity, note, detected_via, err))
    return results


def apply_integrations(
    targets: str | Sequence[str] = "all",
    *,
    dry_run: bool = False,
    uninstall: bool = False,
    home_dir: str | Path | None = None,
    cache_dir: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    which_fn: Callable[[str], str | None] | None = None,
) -> list[IntegrationAction]:
    """Install or uninstall CLI integrations with symlink preservation, backup, and dry-run support."""
    is_all, selected = _normalize_targets(targets)
    home = _resolve_home(home_dir, env)
    eff_cache = _resolve_cache_dir(cache_dir, home, env, home_explicit=home_dir is not None)
    which = shutil.which if which_fn is None else which_fn
    _ = _detect_platform(platform, env)
    actions: list[IntegrationAction] = []

    for cli in selected:
        cfg_dir, primary, legacy_files = _cli_paths(cli, home, env, home_dir is not None)
        installed = bool(which(cli)) or cfg_dir.exists() or cfg_dir.is_symlink()
        real, is_link, exists, err = _resolve_target_file(primary, for_write=not dry_run)

        def _act(action: str, detail: str = "", backup: str = "") -> IntegrationAction:
            return IntegrationAction(cli, action, str(primary), str(real), is_link, backup, detail)

        def _commit(new_text: str, detail: str) -> IntegrationAction:
            if dry_run:
                return _act("would_uninstall" if uninstall else "would_install", detail)
            backup, mode = "", 0o600
            if exists:
                mode = stat.S_IMODE(os.stat(real).st_mode)
                backup = str(_backup_existing_file(real, cli=cli, cache_dir=eff_cache))
            _atomic_write_text(real, new_text, mode=mode)
            return _act("uninstalled" if uninstall else "installed", detail, backup)

        if is_all and not installed:
            actions.append(_act("not_installed", "CLI binary and config directory not found; skipped"))
            continue
        if err:
            actions.append(_act("error", err))
            continue

        try:
            if cli == "claude":
                if not exists and uninstall:
                    actions.append(_act("not_configured"))
                    continue
                data, jerr = _read_json_dict(real) if exists else ({}, "")
                if jerr or data is None:
                    actions.append(_act("error", jerr))
                    continue
                updated, changed, cerr = _claude_uninstall(data) if uninstall else _claude_merge(data)
                if cerr or updated is None:
                    actions.append(_act("error", cerr))
                elif not changed:
                    actions.append(_act("not_configured" if uninstall else "already_configured"))
                else:
                    detail = "removed managed SessionStart/UserPromptSubmit hooks" if uninstall else "ensured SessionStart and UserPromptSubmit hooks"
                    actions.append(_commit(json.dumps(updated, indent=2, ensure_ascii=False) + "\n", detail))
            elif cli == "agy":
                legacy_hit = _find_legacy_agy_hook(legacy_files)
                legacy_rm = f"legacy hook present in {legacy_hit}; remove manually if desired" if legacy_hit else ""
                if not exists and uninstall:
                    actions.append(_act("not_configured", legacy_rm))
                    continue
                if not exists and legacy_hit is not None:
                    actions.append(_act("already_configured", f"already configured in legacy {legacy_hit}; not duplicating"))
                    continue
                data, jerr = _read_json_dict(real) if exists else ({}, "")
                if jerr or data is None:
                    actions.append(_act("error", jerr))
                    continue
                if uninstall:
                    if AGY_NAMESPACE_KEY not in data:
                        actions.append(_act("not_configured", legacy_rm))
                    else:
                        updated = {k: v for k, v in data.items() if k != AGY_NAMESPACE_KEY}
                        actions.append(_commit(json.dumps(updated, indent=2, ensure_ascii=False) + "\n", f"removed '{AGY_NAMESPACE_KEY}' namespace"))
                elif _hook_map_has_voice_hook(data) or _hook_map_has_voice_hook(data.get("hooks")):
                    actions.append(_act("already_configured"))
                elif legacy_hit is not None:
                    actions.append(_act("already_configured", f"already configured in legacy {legacy_hit}; not duplicating"))
                elif AGY_NAMESPACE_KEY in data and not isinstance(data[AGY_NAMESPACE_KEY], dict):
                    actions.append(_act("error", f"'{AGY_NAMESPACE_KEY}' in {real} must be a JSON object"))
                else:
                    updated = dict(data)
                    updated[AGY_NAMESPACE_KEY] = _agy_namespace_entry()
                    actions.append(_commit(json.dumps(updated, indent=2, ensure_ascii=False) + "\n", f"added '{AGY_NAMESPACE_KEY}' (SessionStart + PreInvocation)"))
            else:  # codex
                if not exists and uninstall:
                    actions.append(_act("not_configured"))
                    continue
                raw_toml = real.read_text(encoding="utf-8") if exists else ""
                patched, changed, terr = _patch_codex_toml(raw_toml, uninstall=uninstall)
                if terr or patched is None:
                    actions.append(_act("error", terr))
                elif not changed:
                    note = "legacy 'notify' hook present; remove manually if desired" if (
                        uninstall and raw_toml.strip() and has_codex_voice_title(parse_toml(raw_toml))
                    ) else ""
                    actions.append(_act("not_configured" if uninstall else "already_configured", note))
                else:
                    detail = 'removed "thread-id" from [tui].terminal_title' if uninstall else 'ensured "thread-id" in [tui].terminal_title'
                    actions.append(_commit(patched, detail))
        except (OSError, ConfigError) as exc:
            actions.append(_act("error", str(exc)))
    return actions


def format_status_json(statuses: Sequence[IntegrationStatus]) -> list[dict[str, Any]]:
    return [s.to_dict() for s in statuses]


def format_actions_json(actions: Sequence[IntegrationAction]) -> list[dict[str, Any]]:
    return [a.to_dict() for a in actions]


def format_status_text(statuses: Sequence[IntegrationStatus]) -> str:
    lines = [f"Note ({statuses[0].necessity}): {statuses[0].note}"] if statuses else []
    for s in statuses:
        if s.error:
            state = f"error ({s.error})"
        elif s.configured:
            state = f"configured ({s.detected_via})" if s.detected_via else "configured"
        else:
            state = "not configured" if s.installed else "not installed"
        path_txt = f"{s.config_path} -> {s.resolved_path}" if s.is_symlink else s.config_path
        lines.append(f"[{s.cli:<6}] {state} | {path_txt}")
    return "\n".join(lines)


def format_actions_text(actions: Sequence[IntegrationAction], *, dry_run: bool = False) -> str:
    prefix = "[dry-run] " if dry_run else ""
    lines: list[str] = []
    for a in actions:
        path_txt = f"{a.config_path} (symlink -> {a.resolved_path})" if a.is_symlink else a.config_path
        msg = f"{prefix}[{a.cli:<6}] {a.action}: {path_txt}"
        if a.detail:
            msg += f" — {a.detail}"
        lines.append(msg)
        if a.backup_path:
            lines.append(f"         backup: {a.backup_path}")
    return "\n".join(lines)
