"""Workspace Git status, core/shallow file scanning, hotword extraction, and IDE session evidence."""
from __future__ import annotations

from collections import OrderedDict
import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import posixpath
import re
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from typing import Any, BinaryIO, Iterable, Mapping, Sequence
import urllib.parse

from ._remote_probe import (
    COLD_START_FALLBACK_MIN_CHARS,
    COLD_START_TOP_K,
    _AGY_WS_CWD_RE,
    _BROAD_SYSTEM_DIRS_LOW,
    _REPO_CORE_EXTS,
    _REPO_PRIORITY_ROOTS,
    _REPO_SKIP_BASES,
    _REPO_SKIP_DIRS,
    _REPO_TEST_FILE_RE,
    _SHALLOW_DOC_AND_CODE_EXTS,
    _SHALLOW_SKIP_BASES_LOW,
    _SHALLOW_SKIP_DIRS_LOW,
    _TECH_IDENT_RE,
    _clean_jsonl_file,
    _cwd_match_rank,
    _is_agy_subagent,
    _is_broad_or_unsafe_workspace_dir,
    _is_path_under_dir,
    _is_wsl_slow_mount_str,
    _keep_tech_token,
    _normalize_cwd,
    _read_bounded_line,
    _safe_realpath,
    build_slim_prior_candidate,
    extract_prior_sessions_hotwords,
)
from .app_scope import _VSCODE_REJECT_TITLES, is_remote_ide_window, parse_ide_window_title


@dataclass(frozen=True)
class SessionMatch:
    cli: str  # "codex" | "claude" | "agy" | "workspace" | "remote"
    session_id: str
    jsonl_path: str
    cwd: str = ""
    matched_by: str = ""  # "registry" | "title_uuid" | "title_prefix" | "tmux_registry" | "tmux_pane_title" | "tmux_proc_scan" | "tty_registry" | "tty_ps" | "proc_scan" | "recent_root" | "ide_brain" | "ide_db_title" | "remote_*"
    pts_mtime: float = 0.0
    jsonl_mtime: float = 0.0
    thread_name: str = ""
    origin: str = "local"
    host_name: str = ""
    remote_title: str = ""

    @property
    def session_key(self) -> str:
        if self.origin == "remote" and self.host_name:
            return f"remote:{self.host_name}:{self.cli}:{self.session_id}"
        return f"{self.cli}:{self.session_id}"


# ---------------------------------------------------------------------------
# Git status & technical identifier hotword extraction
# ---------------------------------------------------------------------------

BUILTIN_AGENT_HOTWORDS: tuple[str, ...] = (
    # Priority 1: Coding Agents & IDEs (kept first so low-quota ASR providers capture them first)
    "Claude Code", "Codex", "Antigravity", "agy", "Cursor", "Windsurf",
    "Aider", "OpenCode", "Gemini CLI", "VS Code", "JetBrains", "Copilot",
    # Priority 2: Model families & common API providers
    "Claude", "Opus", "Sonnet", "Haiku", "GPT", "OpenAI", "Gemini", "Flash",
    "Qwen", "DeepSeek", "Kimi", "Whisper", "Groq", "OpenRouter", "DashScope",
    "Ollama", "豆包",
    # Priority 3: Agent engineering & terminal workflow terms
    "subagent", "MCP", "SKILL.md", "AGENTS.md", "CLAUDE.md", "worktree",
    "tmux", "Ghostty", "WSL2", "SSH", "CLI", "hotkey",
    # Priority 4: Common software engineering terms
    "GitHub", "Git", "rebase", "cherry-pick", "PR", "CI/CD",
    "Docker", "Kubernetes", "Python", "TypeScript", "Rust", "Go",
    "React", "Next.js", "Tailwind", "API", "REST", "GraphQL",
    "OAuth", "JWT", "JSON", "JSONL", "YAML", "TOML",
    "SQL", "PostgreSQL", "Redis", "pytest", "ffmpeg", "regex",
    "async", "webhook", "stdout", "stderr", "README",
)

_CJK_HOTWORD_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_BUILTIN_AGENT_HOTWORDS_NON_CJK: tuple[str, ...] = tuple(
    w for w in BUILTIN_AGENT_HOTWORDS if not _CJK_HOTWORD_RE.search(w)
)


def get_builtin_agent_hotwords(language: str = "auto") -> tuple[str, ...]:
    """Return built-in agent hotwords, dropping CJK entries for explicit non-Chinese languages."""
    raw = (language or "auto").strip().lower()
    if not raw or raw == "auto" or raw == "zh" or raw.startswith(("zh-", "zh_", "zh.", "zh@")):
        return BUILTIN_AGENT_HOTWORDS
    return _BUILTIN_AGENT_HOTWORDS_NON_CJK

REPO_FILES_CACHE_TTL_SEC: float = 300.0


def _dedupe_hotwords_ci(words: Sequence[str]) -> list[str]:
    out: list[str] = []
    seen_low: set[str] = set()
    for raw in words:
        w = raw.strip()
        if w and (low := w.lower()) not in seen_low:
            seen_low.add(low)
            out.append(w)
    return out


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
    return _is_wsl_slow_mount_str(path_str)


def _resolve_safe_local_dir(path_str: str, *, wsl: bool) -> str:
    """Validate directory without touching ``/mnt/`` before, during, or after symlink resolution."""
    real = _safe_realpath(path_str, wsl=wsl)
    if not real:
        return ""
    try:
        return real if os.path.isdir(real) else ""
    except OSError:
        return ""


def _has_git_marker(dir_path: str) -> bool:
    try:
        return bool(dir_path) and os.path.exists(os.path.join(dir_path, ".git"))
    except OSError:
        return False


def _is_inside_git_worktree(
    dir_path: str, *, home: Path | None = None, max_depth: int = 12, wsl: bool = False
) -> bool:
    cur = _normalize_cwd(dir_path)
    for _ in range(max_depth + 1):
        if not cur or _is_broad_or_unsafe_workspace_dir(cur, home, wsl=wsl):
            return False
        if _has_git_marker(cur):
            return True
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            return False
        cur = parent
    return False


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
        or _is_inside_git_worktree(real_cwd, home=eff_home, wsl=eff_wsl)
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
    if is_remote_ide_window(win):
        return ""
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


def _load_agy_ide_evidence(
    cid_dir: Path, tp: Path, st: os.stat_result, home: Path, *, wsl: bool = False
) -> _AgyIdeEvidence:
    sub_dir = cid_dir / ".system_generated" / "subagents"
    sub_files: list[Path] = []
    sub_sig: list[tuple[str, int, int]] = []
    safe_sub_dir = _resolve_safe_local_dir(str(sub_dir), wsl=wsl)
    if safe_sub_dir:
        try:
            sub_entries: list[tuple[str, Path]] = []
            with os.scandir(safe_sub_dir) as sit:
                for entry in sit:
                    if entry.name.endswith(".json"):
                        sub_entries.append((entry.name, Path(entry.path)))
            sub_entries.sort(key=lambda pair: pair[0])
            for name, sf in sub_entries[:16]:
                safe_sf = _safe_realpath(str(sf), wsl=wsl)
                if not safe_sf or not os.path.isfile(safe_sf):
                    continue
                try:
                    sst = os.stat(safe_sf)
                except OSError:
                    continue
                sub_files.append(Path(safe_sf))
                sub_sig.append((name, sst.st_mtime_ns, sst.st_size))
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


_ISO_FRAC_RE = re.compile(r"(\.\d{6})\d+")
_AGY_DESKTOP_REJECT_TITLES: frozenset[str] = _VSCODE_REJECT_TITLES | frozenset({
    "new conversation",
    "new chat",
})


def _iter_agy_ide_roots(home: Path) -> tuple[Path, Path]:
    """Return desktop Antigravity roots, strictly isolated from ``~/.gemini/antigravity-cli``."""
    return (home / ".gemini" / "antigravity", home / ".gemini" / "antigravity-ide")


def _is_safe_agy_cid(cid: str) -> bool:
    s = (cid or "").strip()
    return bool(
        s
        and not s.startswith(".")
        and ".." not in s
        and not any(ch in s for ch in ("/", "\\", "\x00"))
    )


def _parse_iso_timestamp(raw: Any) -> float:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        val = float(raw)
        return val / 1000.0 if val > 1e12 else (val if val > 0.0 else 0.0)
    s = str(raw or "").strip()
    if not s:
        return 0.0
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    s = _ISO_FRAC_RE.sub(r"\1", s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, dt.timestamp())


def _resolve_agy_ide_transcript(
    root: Path,
    cid: str,
    *,
    wsl: bool,
    require_uid_match: bool = False,
) -> tuple[Path, os.stat_result] | None:
    if not _is_safe_agy_cid(cid):
        return None
    real_broot = _resolve_safe_local_dir(str(root / "brain"), wsl=wsl)
    if not real_broot:
        return None
    broot_path = Path(real_broot)
    cid_dir = broot_path / cid
    try:
        cid_lst = os.lstat(cid_dir)
    except OSError:
        return None
    if not stat.S_ISDIR(cid_lst.st_mode):
        return None
    logs_dir = cid_dir / ".system_generated" / "logs"
    for cand_name in ("transcript_full.jsonl", "transcript.jsonl"):
        cand_tp = logs_dir / cand_name
        try:
            tp_lst = os.lstat(cand_tp)
        except OSError:
            continue
        if not stat.S_ISREG(tp_lst.st_mode) or tp_lst.st_size <= 0:
            continue
        if require_uid_match and hasattr(os, "getuid") and tp_lst.st_uid != os.getuid():
            continue
        if not _is_path_under_dir(cand_tp, broot_path, wsl=wsl):
            continue
        if _is_agy_subagent(cand_tp):
            return None
        return cand_tp, tp_lst
    return None


def _stat_agy_annotation_mtime(root: Path, cid: str) -> float:
    if not _is_safe_agy_cid(cid):
        return 0.0
    try:
        lst = os.lstat(root / "annotations" / f"{cid}.pbtxt")
    except OSError:
        return 0.0
    return float(lst.st_mtime) if stat.S_ISREG(lst.st_mode) else 0.0


def _open_agy_ide_db_ro(root: Path, *, wsl: bool) -> sqlite3.Connection | None:
    """Open ``<root>/conversation_summaries.db`` read-only without ``immutable=1`` (WAL-safe)."""
    db_path = root / "conversation_summaries.db"
    if is_wsl_slow_mount_path(str(db_path), wsl=wsl):
        return None
    try:
        lst = os.lstat(db_path)
    except OSError:
        return None
    if not stat.S_ISREG(lst.st_mode):
        return None
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    try:
        return sqlite3.connect(uri, uri=True, timeout=0.05)
    except (sqlite3.Error, OSError):
        return None


def _get_agy_db_table_cols(conn: sqlite3.Connection) -> set[str]:
    with contextlib.closing(conn.execute("PRAGMA table_info(conversation_summaries)")) as info_cur:
        return {str(r[1]).lower() for r in info_cur.fetchall() if len(r) > 1}


def _query_agy_ide_db_title_rows(
    root: Path,
    *,
    wsl: bool,
) -> list[tuple[str, str, str, Any]]:
    """Read all non-CLI, root-level desktop conversation rows for Python-side Unicode title matching."""
    conn = _open_agy_ide_db_ro(root, wsl=wsl)
    if conn is None:
        return []
    try:
        with contextlib.closing(conn):
            cols = _get_agy_db_table_cols(conn)
            if "conversation_id" not in cols or "title" not in cols:
                return []
            sel_ws = "workspace_uris" if "workspace_uris" in cols else "''"
            sel_lmt = "last_modified_time" if "last_modified_time" in cols else "''"
            wheres: list[str] = []
            if "source" in cols:
                wheres.append("COALESCE(source, '') != 'CORTEX_TRAJECTORY_SOURCE_CLI'")
            if "parent_conversation_id" in cols:
                wheres.append("COALESCE(parent_conversation_id, '') = ''")
            if "nesting_depth" in cols:
                wheres.append("COALESCE(nesting_depth, 0) = 0")
            if "agent_name" in cols:
                wheres.append("COALESCE(agent_name, '') != 'avi-refiner'")
            wheres.append("COALESCE(title, '') != ''")
            sql = (
                f"SELECT conversation_id, title, {sel_ws}, {sel_lmt} "
                f"FROM conversation_summaries WHERE {' AND '.join(wheres)}"
            )
            with contextlib.closing(conn.execute(sql)) as cur:
                return [
                    (str(r[0] or ""), str(r[1] or ""), str(r[2] or ""), r[3])
                    for r in cur.fetchall()
                ]
    except (sqlite3.Error, OSError):
        return []


def _decode_structured_workspace_uri(raw_u: str, *, wsl: bool) -> str:
    """Parse structured local ``file://`` (and WSL-host ``vscode-remote://wsl+...``) URIs without stripping trailing punctuation."""
    s = (raw_u or "").strip()
    if not s:
        return ""
    try:
        parts = urllib.parse.urlsplit(s)
    except ValueError:
        return ""
    scheme = parts.scheme.lower()
    if scheme == "file":
        if parts.netloc and parts.netloc.lower() != "localhost":
            return ""
    elif scheme == "vscode-remote":
        if not wsl or not (parts.netloc or "").lower().startswith("wsl+"):
            return ""
    else:
        return ""
    path = urllib.parse.unquote(parts.path or "")
    if not path.startswith("/") or "\x00" in path:
        return ""
    if is_wsl_slow_mount_path(path, wsl=wsl):
        return ""
    return path


def _parse_agy_db_workspace_uris(raw_ws_uris: Any, home: Path, *, wsl: bool) -> tuple[bool, list[str]]:
    """Return ``(has_raw_entries, local_safe_dirs)`` from DB ``workspace_uris`` JSON."""
    if not isinstance(raw_ws_uris, str):
        return False, []
    s = raw_ws_uris.strip()
    if not s:
        return False, []
    try:
        parsed = json.loads(s)
    except ValueError:
        return False, []
    if not isinstance(parsed, list):
        return False, []
    has_raw = False
    out: list[str] = []
    for item in parsed:
        raw_u = item if isinstance(item, str) else (
            item.get("uri") or item.get("path") if isinstance(item, Mapping) else ""
        )
        if not isinstance(raw_u, str) or not raw_u.strip():
            continue
        has_raw = True
        decoded = _decode_structured_workspace_uri(raw_u, wsl=wsl)
        if not decoded:
            continue
        safe_dir = _resolve_safe_local_dir(decoded, wsl=wsl)
        if safe_dir and not _is_broad_or_unsafe_workspace_dir(safe_dir, home, wsl=wsl) and safe_dir not in out:
            out.append(safe_dir)
    return has_raw, out


def _scan_agy_ide_db_for_prior(
    root: Path,
    safe_ws: str,
    home: Path,
    *,
    wsl: bool,
    excl_low: str,
) -> tuple[list[tuple[float, str, Path]], set[str]]:
    """Scan all DB rows under ``root`` for exact ``safe_ws`` prior candidates and authoritative DB vetoes."""
    conn = _open_agy_ide_db_ro(root, wsl=wsl)
    if conn is None:
        return [], set()
    matched_rows: list[tuple[float, str, Path]] = []
    vetoed_cids: set[str] = set()
    try:
        with contextlib.closing(conn):
            cols = _get_agy_db_table_cols(conn)
            if "conversation_id" not in cols:
                return [], set()
            sel_ws = "workspace_uris" if "workspace_uris" in cols else "''"
            sel_src = "source" if "source" in cols else "''"
            sel_parent = "parent_conversation_id" if "parent_conversation_id" in cols else "''"
            sel_depth = "nesting_depth" if "nesting_depth" in cols else "0"
            sel_agent = "agent_name" if "agent_name" in cols else "''"
            sql = (
                f"SELECT conversation_id, {sel_ws}, {sel_src}, {sel_parent}, {sel_depth}, {sel_agent} "
                "FROM conversation_summaries"
            )
            with contextlib.closing(conn.execute(sql)) as cur:
                rows = cur.fetchall()
    except (sqlite3.Error, OSError):
        return [], set()

    seen_matched: set[str] = set()
    for raw_cid, raw_ws_uris, raw_src, raw_parent, raw_depth, raw_agent in rows:
        cid = str(raw_cid or "").strip()
        cid_low = cid.lower()
        if not cid:
            continue
        src = str(raw_src or "").strip()
        parent_cid = str(raw_parent or "").strip()
        agent_name = str(raw_agent or "").strip()
        try:
            depth = int(raw_depth or 0)
        except (TypeError, ValueError):
            depth = 1
        if (
            src == "CORTEX_TRAJECTORY_SOURCE_CLI"
            or bool(parent_cid)
            or depth != 0
            or agent_name == "avi-refiner"
        ):
            vetoed_cids.add(cid_low)
            continue

        has_raw, ws_dirs = _parse_agy_db_workspace_uris(str(raw_ws_uris or ""), home, wsl=wsl)
        if any(_cwd_match_rank(safe_ws, p, home, wsl=wsl) == 2 for p in ws_dirs):
            vetoed_cids.add(cid_low)
            if cid_low == excl_low or cid_low in seen_matched:
                continue
            resolved = _resolve_agy_ide_transcript(root, cid, wsl=wsl, require_uid_match=True)
            if resolved is not None:
                tp, st = resolved
                seen_matched.add(cid_low)
                matched_rows.append((st.st_mtime, cid, tp))
        elif has_raw:
            # DB explicitly binds this conversation to another workspace or remote URI; block legacy fallback.
            vetoed_cids.add(cid_low)

    return matched_rows, vetoed_cids


def resolve_antigravity_ide_session_by_title(
    window_title: str,
    *,
    home: Path | None = None,
    wsl: bool | None = None,
    env: Mapping[str, str] | None = None,
) -> SessionMatch | None:
    """Resolve an Antigravity 2.0 standalone desktop conversation directly by window title via ``conversation_summaries.db``."""
    win_title = (window_title or "").strip()
    if not win_title or win_title.lower() in _AGY_DESKTOP_REJECT_TITLES:
        return None
    eff_env = os.environ if env is None else env
    eff_home = home if home is not None else Path((eff_env.get("HOME") or "").strip() or Path.home())
    eff_wsl = _effective_is_wsl(wsl, env)

    trunc_prefix = ""
    if win_title.endswith("..."):
        cand_pfx = win_title[:-3]
    elif win_title.endswith("\u2026"):
        cand_pfx = win_title[:-1]
    else:
        cand_pfx = ""
    if cand_pfx:
        pfx_s = cand_pfx.strip()
        min_pfx = 4 if _CJK_HOTWORD_RE.search(pfx_s) else 8
        if len(pfx_s) >= min_pfx:
            trunc_prefix = cand_pfx

    exact_cands: list[tuple[Path, str, Path, os.stat_result, str, Any]] = []
    prefix_cands: list[tuple[Path, str, Path, os.stat_result, str, Any]] = []
    seen_exact: set[str] = set()
    seen_prefix: set[str] = set()

    for root in _iter_agy_ide_roots(eff_home):
        real_root = _resolve_safe_local_dir(str(root), wsl=eff_wsl)
        if not real_root:
            continue
        root_path = Path(real_root)
        for raw_cid, raw_title, raw_ws_uris, raw_lmt in _query_agy_ide_db_title_rows(
            root_path, wsl=eff_wsl
        ):
            cid = raw_cid.strip()
            db_title = raw_title.strip()
            if not cid or not db_title:
                continue
            is_exact = db_title == win_title
            is_prefix = (
                not is_exact
                and bool(trunc_prefix)
                and db_title.startswith(trunc_prefix)
                and len(db_title) > len(trunc_prefix)
            )
            if not is_exact and not is_prefix:
                continue
            cid_key = cid.lower()
            if is_exact:
                if cid_key in seen_exact:
                    continue
                resolved = _resolve_agy_ide_transcript(root_path, cid, wsl=eff_wsl)
                if resolved is None:
                    continue
                tp, tp_st = resolved
                seen_exact.add(cid_key)
                exact_cands.append((root_path, cid, tp, tp_st, raw_ws_uris, raw_lmt))
            elif not exact_cands and cid_key not in seen_prefix:
                resolved = _resolve_agy_ide_transcript(root_path, cid, wsl=eff_wsl)
                if resolved is None:
                    continue
                tp, tp_st = resolved
                seen_prefix.add(cid_key)
                prefix_cands.append((root_path, cid, tp, tp_st, raw_ws_uris, raw_lmt))

    cands = exact_cands if exact_cands else prefix_cands
    if not cands:
        return None
    if len(cands) == 1:
        chosen = cands[0]
    else:
        def _cand_sort_key(
            item: tuple[Path, str, Path, os.stat_result, str, Any]
        ) -> tuple[int, float, float, float]:
            ann_mtime = _stat_agy_annotation_mtime(item[0], item[1])
            db_lmt = _parse_iso_timestamp(item[5])
            return (1 if ann_mtime > 0.0 else 0, ann_mtime, db_lmt, float(item[3].st_mtime))

        chosen = max(cands, key=_cand_sort_key)
    _, ws_dirs = _parse_agy_db_workspace_uris(chosen[4], eff_home, wsl=eff_wsl)
    ws_cwd = ws_dirs[0] if ws_dirs else ""
    return SessionMatch(
        cli="agy",
        session_id=chosen[1],
        jsonl_path=str(chosen[2]),
        cwd=ws_cwd,
        matched_by="ide_db_title",
        jsonl_mtime=chosen[3].st_mtime,
    )


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
    for broot in (r / "brain" for r in _iter_agy_ide_roots(home)):
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
                    tp: Path | None = None
                    for cand_name in ("transcript_full.jsonl", "transcript.jsonl"):
                        cand_tp = logs_dir / cand_name
                        if eff_wsl:
                            safe_tp = _safe_realpath(str(cand_tp), wsl=True)
                            if safe_tp and os.path.isfile(safe_tp):
                                tp = cand_tp
                                break
                        elif cand_tp.is_file():
                            tp = cand_tp
                            break
                    if tp is None:
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
        if _is_agy_subagent(tp):
            continue
        matched += 1
        ev = _load_agy_ide_evidence(cid_dir, tp, st, home, wsl=eff_wsl)
        if _classify_agy_evidence_for_workspace(ev, safe_ws, home, wsl=eff_wsl) == "match":
            return SessionMatch(
                cli="agy", session_id=cid, jsonl_path=str(tp), cwd=safe_ws, matched_by="ide_brain", jsonl_mtime=mtime
            )
        if matched >= _AGY_IDE_MAX_CANDIDATES:
            break
    return None


def _agy_ide_evidence_matches_exact_workspace(
    ev: _AgyIdeEvidence, safe_ws: str, home: Path, *, wsl: bool = False
) -> bool:
    """Require explicit ``workspaceUris`` or system ``cwd`` exact match (rank == 2); never L2/L3 path guesses."""
    if not safe_ws:
        return False
    for tier_paths in (ev.ws_uris, ev.sys_cwds):
        if tier_paths:
            return any(
                _cwd_match_rank(safe_ws, p, home, wsl=wsl) == 2
                for p in tier_paths
                if p != _AGY_REMOTE_URI_SENTINEL
            )
    return False


def discover_antigravity_ide_prior_sessions(
    workspace_cwd: str,
    *,
    home: Path,
    wsl: bool | None = None,
    env: Mapping[str, str] | None = None,
    max_keep: int = COLD_START_TOP_K + 2,
    exclude_session_id: str = "",
) -> list[dict[str, Any]]:
    """Discover historical desktop Antigravity IDE ``brain/<cid>`` sessions for exact ``workspace_cwd``.

    Strictly isolated to ``~/.gemini/antigravity`` and ``~/.gemini/antigravity-ide``
    with no age limit, excluding subagents and broad/unsafe workspaces.
    Combines Antigravity 2.0 ``conversation_summaries.db`` ``workspace_uris`` matches
    and legacy ``brain/<cid>`` L1 workspace evidence.
    """
    eff_wsl = _effective_is_wsl(wsl, env)
    safe_ws = _resolve_safe_local_dir(workspace_cwd, wsl=eff_wsl)
    if not safe_ws or _is_broad_or_unsafe_workspace_dir(safe_ws, home, wsl=eff_wsl):
        return []
    excl_low = (exclude_session_id or "").strip().lower()

    parsed: list[dict[str, Any]] = []
    seen_cids: set[str] = set()

    # 1. Antigravity 2.0 standalone desktop: exact workspace_uris matches & authoritative per-root DB vetoes
    db_cands: list[tuple[float, str, Path]] = []
    per_root_vetoes: dict[Path, set[str]] = {}
    for root in _iter_agy_ide_roots(home):
        real_root = _resolve_safe_local_dir(str(root), wsl=eff_wsl)
        if not real_root:
            continue
        root_path = Path(real_root)
        matched_rows, vetoed = _scan_agy_ide_db_for_prior(
            root_path, safe_ws, home, wsl=eff_wsl, excl_low=excl_low
        )
        per_root_vetoes[root_path] = vetoed
        for mtime, cid, tp in matched_rows:
            cid_low = cid.lower()
            if cid_low not in seen_cids:
                seen_cids.add(cid_low)
                db_cands.append((mtime, cid, tp))

    db_cands.sort(key=lambda item: item[0], reverse=True)
    for mtime, cid, tp in db_cands:
        turns, _, _ = _clean_jsonl_file("agy", str(tp), home=None)
        slim = build_slim_prior_candidate(
            cli="agy",
            session_id=cid,
            jsonl_path=str(tp),
            cwd=safe_ws,
            jsonl_mtime=mtime,
            turns=turns,
        )
        if slim is not None:
            parsed.append(slim)
            if len(parsed) >= max_keep:
                break

    # 2. Legacy VS Code Fork desktop brain/<cid> scan (deduplicated & never resurrecting DB-vetoed CIDs)
    raw_cands: list[tuple[float, str, Path, Path, os.stat_result, Path]] = []
    for root in _iter_agy_ide_roots(home):
        real_root = _resolve_safe_local_dir(str(root), wsl=eff_wsl)
        if not real_root:
            continue
        root_path = Path(real_root)
        root_vetoes = per_root_vetoes.get(root_path, set())
        real_broot = _resolve_safe_local_dir(str(root_path / "brain"), wsl=eff_wsl)
        if not real_broot:
            continue
        broot_path = Path(real_broot)
        try:
            with os.scandir(real_broot) as bit:
                for entry in bit:
                    cid = entry.name.strip().lower()
                    if (
                        not cid
                        or cid.startswith(".")
                        or cid == excl_low
                        or cid in seen_cids
                        or cid in root_vetoes
                    ):
                        continue
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    cid_dir = Path(entry.path)
                    logs_dir = cid_dir / ".system_generated" / "logs"
                    tp = None
                    for cand_name in ("transcript_full.jsonl", "transcript.jsonl"):
                        cand_tp = logs_dir / cand_name
                        if not _is_path_under_dir(cand_tp, broot_path, wsl=eff_wsl):
                            continue
                        safe_tp = _safe_realpath(str(cand_tp), wsl=eff_wsl)
                        if safe_tp and os.path.isfile(safe_tp):
                            tp = Path(safe_tp)
                            break
                    if tp is None:
                        continue
                    try:
                        st = tp.stat()
                    except OSError:
                        continue
                    if st.st_size > 0 and st.st_uid == os.getuid():
                        seen_cids.add(cid)
                        raw_cands.append((st.st_mtime, cid, cid_dir, tp, st, broot_path))
        except OSError:
            continue

    raw_cands.sort(key=lambda item: item[0], reverse=True)
    inspected = 0
    legacy_added = 0
    for mtime, cid, cid_dir, tp, st, _ in raw_cands[:64]:
        if _is_agy_subagent(tp):
            continue
        inspected += 1
        ev = _load_agy_ide_evidence(cid_dir, tp, st, home, wsl=eff_wsl)
        if _agy_ide_evidence_matches_exact_workspace(ev, safe_ws, home, wsl=eff_wsl):
            turns, _, _ = _clean_jsonl_file("agy", str(tp), home=None)
            slim = build_slim_prior_candidate(
                cli="agy",
                session_id=cid,
                jsonl_path=str(tp),
                cwd=safe_ws,
                jsonl_mtime=mtime,
                turns=turns,
            )
            if slim is not None:
                parsed.append(slim)
                legacy_added += 1
                if legacy_added >= max_keep:
                    break
        if inspected >= 24:
            break

    parsed.sort(key=lambda item: float(item.get("jsonl_mtime") or 0.0), reverse=True)
    return parsed[:max_keep]


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
