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
    _clean_claude_record,
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
    encode_claude_project_dir,
    extract_prior_sessions_hotwords,
)
from .app_scope import (
    _VSCODE_REJECT_TITLES,
    _strip_trailing_app_suffix,
    is_remote_ide_window,
    parse_ide_window_title,
)


@dataclass(frozen=True)
class SessionMatch:
    cli: str  # "codex" | "claude" | "agy" | "workspace" | "remote"
    session_id: str
    jsonl_path: str
    cwd: str = ""
    matched_by: str = ""  # "registry" | "title_uuid" | "title_prefix" | "tmux_registry" | "tmux_pane_title" | "tmux_proc_scan" | "tty_registry" | "tty_ps" | "proc_scan" | "recent_root" | "ide_brain" | "ide_db_title" | "codex_desktop_db" | "claude_desktop_code" | "claude_desktop_cowork" | "remote_*"
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
_AGY_IDE_WS_CWD_RE = re.compile(
    r"(?:Command Working Directory:\s*|Active Workspaces:\s*\r?\n-\s*)((?:/|(?i:vscode-remote|file)://)[^\r\n<]+)"
)
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


def _decode_vscode_remote_wsl_path(
    parts: urllib.parse.SplitResult,
    *,
    wsl: bool,
    env: Mapping[str, str] | None = None,
) -> str:
    """Decode ``vscode-remote://wsl+<distro>/...`` (including ``wsl%2B``) only on a matching WSL host."""
    if not wsl:
        return ""
    authority = urllib.parse.unquote(parts.netloc or "")
    if not authority.lower().startswith("wsl+"):
        return ""
    uri_distro = authority[4:].strip()
    src_env = os.environ if env is None else env
    cur_distro = (src_env.get("WSL_DISTRO_NAME") or "").strip()
    if cur_distro and (not uri_distro or uri_distro.lower() != cur_distro.lower()):
        return ""
    return urllib.parse.unquote(parts.path or "")


def _decode_agy_uri_or_path(
    raw: str,
    home: Path,
    *,
    keep_remote_sentinel: bool = False,
    wsl: bool | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    s = (raw or "").strip().rstrip(".,;:")
    if not s:
        return ""
    low = s.lower()
    if low.startswith("file://"):
        try:
            parsed = urllib.parse.urlsplit(s)
        except ValueError:
            return ""
        if parsed.netloc and parsed.netloc.lower() != "localhost":
            return _AGY_REMOTE_URI_SENTINEL if keep_remote_sentinel else ""
        s = urllib.parse.unquote(parsed.path or "")
    elif low.startswith("vscode-remote://"):
        try:
            parsed = urllib.parse.urlsplit(s)
        except ValueError:
            return ""
        eff_wsl = _effective_is_wsl(wsl, env)
        decoded_wsl = _decode_vscode_remote_wsl_path(parsed, wsl=eff_wsl, env=env)
        if not decoded_wsl:
            return _AGY_REMOTE_URI_SENTINEL if keep_remote_sentinel else ""
        s = decoded_wsl
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


def _extract_paths_from_metadata_block(
    meta_text: str,
    home: Path,
    *,
    wsl: bool = False,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for raw_val in (
        *(m.group(0) for m in _AGY_URI_RE.finditer(meta_text)),
        *(m.group(1) for m in _AGY_ABS_PATH_RE.finditer(_AGY_URI_RE.sub(" ", meta_text))),
    ):
        if (p := _decode_agy_uri_or_path(raw_val, home, wsl=wsl, env=env)) and p not in seen:
            seen.add(p)
            paths.append(p)
    return paths


def _extract_tool_call_paths(
    obj: Mapping[str, Any],
    home: Path,
    *,
    wsl: bool = False,
    env: Mapping[str, str] | None = None,
) -> list[str]:
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
                    if isinstance(item, str) and (p := _decode_agy_uri_or_path(item, home, wsl=wsl, env=env)):
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
    cid_dir: Path,
    tp: Path,
    st: os.stat_result,
    home: Path,
    *,
    wsl: bool = False,
    env: Mapping[str, str] | None = None,
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
    src_env = os.environ if env is None else env
    cur_distro = (src_env.get("WSL_DISTRO_NAME") or "").strip().lower() if wsl else ""
    cache_key = (str(tp), st.st_mtime_ns, st.st_size, tuple(sub_sig), bool(wsl), cur_distro)
    with _AGY_BRAIN_EVIDENCE_LOCK:
        if (cached := _AGY_BRAIN_EVIDENCE_CACHE.get(cache_key)) is not None:
            _AGY_BRAIN_EVIDENCE_CACHE.move_to_end(cache_key)
            return cached

    ws_uris: list[str] = []
    sys_cwds: list[str] = []

    def _add_unique(dst: list[str], raw_u: str, *, keep_remote: bool = False) -> None:
        if (
            p := _decode_agy_uri_or_path(raw_u, home, keep_remote_sentinel=keep_remote, wsl=wsl, env=env)
        ) and p not in dst:
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
                    _add_unique(sys_cwds, obj["cwd"], keep_remote=True)
                # Never extract workspace CWD from USER_EXPLICIT prose (users often paste logs from other repos).
                if obj.get("source") == "SYSTEM":
                    c_str = obj.get("content")
                    if isinstance(c_str, str) and ("Working Directory:" in c_str or "Active Workspaces:" in c_str):
                        if m_ws := _AGY_IDE_WS_CWD_RE.search(c_str):
                            _add_unique(sys_cwds, m_ws.group(1), keep_remote=True)
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
                if extracted := _extract_paths_from_metadata_block(content[pos:], home, wsl=wsl, env=env):
                    l2_paths = extracted
                    break

    l3_paths = [
        p
        for rec in records
        if rec.get("source") == "MODEL" or rec.get("type") == "PLANNER_RESPONSE"
        for p in _extract_tool_call_paths(rec, home, wsl=wsl, env=env)
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
_NUM_TS_RE = re.compile(r"^\d+(?:\.\d+)?$")
_AGY_DESKTOP_REJECT_TITLES: frozenset[str] = _VSCODE_REJECT_TITLES | frozenset({
    "new conversation",
    "new chat",
})
_CODEX_DESKTOP_REJECT_TITLES: frozenset[str] = frozenset({
    "chatgpt",
    "codex",
    "new chat",
    "new conversation",
    "settings",
    "preferences",
    "welcome",
    "about",
    "search",
})
_CODEX_DESKTOP_APP_SUFFIXES: tuple[str, ...] = ("chatgpt", "codex")
_CODEX_DESKTOP_ORIGINATORS: tuple[str, ...] = ("Codex Desktop", "codex_work_desktop")


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
    if _NUM_TS_RE.match(s):
        val = float(s)
        return val / 1000.0 if val > 1e12 else (val if val > 0.0 else 0.0)
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


def _compute_trunc_prefix(win_title: str) -> str:
    if win_title.endswith("..."):
        cand_pfx = win_title[:-3]
    elif win_title.endswith("\u2026"):
        cand_pfx = win_title[:-1]
    else:
        return ""
    pfx_s = cand_pfx.strip()
    min_pfx = 4 if _CJK_HOTWORD_RE.search(pfx_s) else 8
    return cand_pfx if len(pfx_s) >= min_pfx else ""


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


def _open_readonly_sqlite(db_path: Path, *, wsl: bool) -> sqlite3.Connection | None:
    """Open a regular local SQLite DB read-only without ``immutable=1`` (WAL-safe, rejects symlinks)."""
    if is_wsl_slow_mount_path(str(db_path), wsl=wsl):
        return None
    try:
        lst = os.lstat(db_path)
    except OSError:
        return None
    if not stat.S_ISREG(lst.st_mode):
        return None
    safe_db = _safe_realpath(str(db_path), wsl=wsl)
    if not safe_db:
        return None
    uri = f"{Path(safe_db).as_uri()}?mode=ro"
    try:
        return sqlite3.connect(uri, uri=True, timeout=0.05)
    except (sqlite3.Error, OSError):
        return None


def _get_sqlite_table_cols(conn: sqlite3.Connection, table_name: str) -> set[str]:
    with contextlib.closing(conn.execute(f"PRAGMA table_info({table_name})")) as info_cur:
        return {str(r[1]).lower() for r in info_cur.fetchall() if len(r) > 1}


def _open_agy_ide_db_ro(root: Path, *, wsl: bool) -> sqlite3.Connection | None:
    """Open ``<root>/conversation_summaries.db`` read-only without ``immutable=1`` (WAL-safe)."""
    return _open_readonly_sqlite(root / "conversation_summaries.db", wsl=wsl)


def _get_agy_db_table_cols(conn: sqlite3.Connection) -> set[str]:
    return _get_sqlite_table_cols(conn, "conversation_summaries")


def _query_agy_ide_db_title_rows(
    root: Path,
    *,
    wsl: bool,
) -> tuple[list[tuple[str, str, str, Any]], bool]:
    """Read all non-CLI, root-level desktop conversation rows for Python-side Unicode title matching.

    Returns ``(rows, db_ok)`` where ``db_ok=False`` indicates ``conversation_summaries.db`` exists
    on disk (including as a symlink) but could not be opened/queried or lacks required columns.
    """
    db_path = root / "conversation_summaries.db"
    if not os.path.lexists(db_path):
        return [], True
    conn = _open_agy_ide_db_ro(root, wsl=wsl)
    if conn is None:
        return [], False
    try:
        with contextlib.closing(conn):
            cols = _get_agy_db_table_cols(conn)
            if "conversation_id" not in cols or "title" not in cols:
                return [], False
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
                return (
                    [
                        (str(r[0] or ""), str(r[1] or ""), str(r[2] or ""), r[3])
                        for r in cur.fetchall()
                    ],
                    True,
                )
    except (sqlite3.Error, OSError):
        return [], False


def _decode_structured_workspace_uri(
    raw_u: str, *, wsl: bool, env: Mapping[str, str] | None = None
) -> str:
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
        path = urllib.parse.unquote(parts.path or "")
    elif scheme == "vscode-remote":
        path = _decode_vscode_remote_wsl_path(parts, wsl=wsl, env=env)
        if not path:
            return ""
    else:
        return ""
    if not path.startswith("/") or "\x00" in path:
        return ""
    if is_wsl_slow_mount_path(path, wsl=wsl):
        return ""
    return path


def _normalize_unresolved_agy_uri(raw_u: str) -> str:
    s = (raw_u or "").strip().rstrip("/")
    if not s:
        return ""
    try:
        parts = urllib.parse.urlsplit(s)
    except ValueError:
        return s
    if parts.scheme:
        scheme = parts.scheme.lower()
        netloc = urllib.parse.unquote(parts.netloc or "").lower()
        path = posixpath.normpath(urllib.parse.unquote(parts.path or "")) if parts.path else ""
        return f"{scheme}://{netloc}{path}"
    return s


def _parse_agy_db_workspace_uris(
    raw_ws_uris: Any, home: Path, *, wsl: bool, env: Mapping[str, str] | None = None
) -> tuple[bool, list[str], list[str]]:
    """Return ``(has_raw_entries, local_safe_dirs, unresolved_uris)`` from DB ``workspace_uris`` JSON."""
    if not isinstance(raw_ws_uris, str):
        return False, [], []
    s = raw_ws_uris.strip()
    if not s:
        return False, [], []
    try:
        parsed = json.loads(s)
    except ValueError:
        return False, [], []
    if not isinstance(parsed, list):
        return False, [], []
    has_raw = False
    out: list[str] = []
    unresolved: list[str] = []
    for item in parsed:
        raw_u = item if isinstance(item, str) else (
            item.get("uri") or item.get("path") if isinstance(item, Mapping) else ""
        )
        if not isinstance(raw_u, str) or not raw_u.strip():
            continue
        has_raw = True
        decoded = _decode_structured_workspace_uri(raw_u, wsl=wsl, env=env)
        if decoded:
            safe_dir = _resolve_safe_local_dir(decoded, wsl=wsl)
            if safe_dir and not _is_broad_or_unsafe_workspace_dir(safe_dir, home, wsl=wsl):
                if safe_dir not in out:
                    out.append(safe_dir)
                continue
        norm_u = _normalize_unresolved_agy_uri(raw_u)
        if norm_u and norm_u not in unresolved:
            unresolved.append(norm_u)
    return has_raw, out, unresolved


def _scan_agy_ide_db_for_prior(
    root: Path,
    safe_ws: str,
    home: Path,
    *,
    wsl: bool,
    excl_low: str,
    env: Mapping[str, str] | None = None,
) -> tuple[list[tuple[float, str, Path]], set[str], bool]:
    """Scan all DB rows under ``root`` for exact ``safe_ws`` prior candidates and authoritative DB vetoes.

    Returns ``(matched_rows, vetoed_cids, db_ok)`` where ``db_ok=False`` indicates
    ``conversation_summaries.db`` exists on disk but failed to open/query so the caller
    must fail closed for that root (skip legacy ``brain/`` fallback).
    """
    db_path = root / "conversation_summaries.db"
    if not os.path.lexists(db_path):
        return [], set(), True
    conn = _open_agy_ide_db_ro(root, wsl=wsl)
    if conn is None:
        return [], set(), False
    matched_rows: list[tuple[float, str, Path]] = []
    vetoed_cids: set[str] = set()
    try:
        with contextlib.closing(conn):
            cols = _get_agy_db_table_cols(conn)
            if "conversation_id" not in cols:
                return [], set(), False
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
        return [], set(), False

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

        has_raw, ws_dirs, _ = _parse_agy_db_workspace_uris(str(raw_ws_uris or ""), home, wsl=wsl, env=env)
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

    return matched_rows, vetoed_cids, True


def resolve_antigravity_ide_session_by_title(
    window_title: str,
    *,
    home: Path | None = None,
    wsl: bool | None = None,
    env: Mapping[str, str] | None = None,
    veto_out: list[str] | None = None,
) -> SessionMatch | None:
    """Resolve an Antigravity 2.0 standalone desktop conversation directly by window title via ``conversation_summaries.db``."""
    win_title = (window_title or "").strip()
    if not win_title or win_title.lower() in _AGY_DESKTOP_REJECT_TITLES:
        return None
    eff_env = os.environ if env is None else env
    eff_home = home if home is not None else Path((eff_env.get("HOME") or "").strip() or Path.home())
    eff_wsl = _effective_is_wsl(wsl, env)

    def _veto(reason: str) -> None:
        if veto_out is not None:
            veto_out.append(reason)

    trunc_prefix = _compute_trunc_prefix(win_title)
    is_truncated_title = win_title.endswith("...") or win_title.endswith("\u2026")

    # Collect ALL title-matching DB rows across every root BEFORE checking transcripts so
    # brand-new sessions without transcripts yet still participate in cross-repo ambiguity vetoes.
    matched_rows: list[tuple[Path, str, str, Any, bool]] = []
    seen_keys: set[tuple[Path, str]] = set()

    for root in _iter_agy_ide_roots(eff_home):
        real_root = _resolve_safe_local_dir(str(root), wsl=eff_wsl)
        if not real_root:
            continue
        root_path = Path(real_root)
        rows, db_ok = _query_agy_ide_db_title_rows(root_path, wsl=eff_wsl)
        if not db_ok:
            _veto("db_error")
            return None
        for raw_cid, raw_title, raw_ws_uris, raw_lmt in rows:
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
            row_key = (root_path, cid.lower())
            if row_key in seen_keys:
                continue
            seen_keys.add(row_key)
            matched_rows.append((root_path, cid, raw_ws_uris, raw_lmt, is_exact))

    if not matched_rows:
        return None

    if not is_truncated_title:
        exact_only = [r for r in matched_rows if r[4]]
        if exact_only:
            matched_rows = exact_only

    if len(matched_rows) == 1:
        row = matched_rows[0]
        resolved = _resolve_agy_ide_transcript(row[0], row[1], wsl=eff_wsl)
        if resolved is None:
            _veto("no_transcript")
            return None
        tp, tp_st = resolved
        _, ws_dirs, _ = _parse_agy_db_workspace_uris(row[2], eff_home, wsl=eff_wsl, env=eff_env)
        ws_cwd = ws_dirs[0] if ws_dirs else ""
        return SessionMatch(
            cli="agy",
            session_id=row[1],
            jsonl_path=str(tp),
            cwd=ws_cwd,
            matched_by="ide_db_title",
            jsonl_mtime=tp_st.st_mtime,
        )

    # Multi-row match: verify full normalized workspace set (including unresolved roots) across ALL
    # matched DB rows (including rows without transcripts yet) before filtering by transcript availability.
    distinct_ws_sets: set[tuple[frozenset[str], frozenset[str]]] = set()
    parsed_rows: list[tuple[Path, str, list[str], Any]] = []
    for root_path, cid, raw_ws_uris, raw_lmt, _is_exact in matched_rows:
        has_raw, item_ws_dirs, item_unresolved = _parse_agy_db_workspace_uris(
            raw_ws_uris, eff_home, wsl=eff_wsl, env=eff_env
        )
        if has_raw and not item_ws_dirs:
            _veto("ambiguous_workspace")
            return None
        distinct_ws_sets.add((frozenset(item_ws_dirs), frozenset(item_unresolved)))
        parsed_rows.append((root_path, cid, item_ws_dirs, raw_lmt))
    if len(distinct_ws_sets) != 1:
        _veto("ambiguous_workspace")
        return None

    transcript_cands: list[tuple[Path, str, Path, os.stat_result, list[str], Any]] = []
    for root_path, cid, item_ws_dirs, raw_lmt in parsed_rows:
        resolved = _resolve_agy_ide_transcript(root_path, cid, wsl=eff_wsl)
        if resolved is None:
            continue
        tp, tp_st = resolved
        transcript_cands.append((root_path, cid, tp, tp_st, item_ws_dirs, raw_lmt))
    if not transcript_cands:
        _veto("no_transcript")
        return None

    def _cand_sort_key(
        item: tuple[Path, str, Path, os.stat_result, list[str], Any]
    ) -> tuple[float, float, float, float]:
        ann_mtime = _stat_agy_annotation_mtime(item[0], item[1])
        db_lmt = _parse_iso_timestamp(item[5])
        tp_mtime = float(item[3].st_mtime)
        return (max(ann_mtime, db_lmt, tp_mtime), ann_mtime, db_lmt, tp_mtime)

    chosen = max(transcript_cands, key=_cand_sort_key)
    ws_cwd = chosen[4][0] if chosen[4] else ""
    return SessionMatch(
        cli="agy",
        session_id=chosen[1],
        jsonl_path=str(chosen[2]),
        cwd=ws_cwd,
        matched_by="ide_db_title",
        jsonl_mtime=chosen[3].st_mtime,
    )


@dataclass(frozen=True)
class _CodexCatalogRow:
    thread_id: str
    display_title: str
    is_local: bool
    cwd: str
    rollout_path: str | None
    recency_ts: float
    updated_ts: float


def _validate_codex_rollout_meta(
    jsonl_path: str | Path, expected_thread_id: str
) -> bool:
    """Verify a Codex rollout's first line is a valid non-subagent ``session_meta`` matching ``expected_thread_id``."""
    want_tid = (expected_thread_id or "").strip().lower()
    if not want_tid:
        return False
    try:
        with open(jsonl_path, "rb") as f:
            first_line, exceeded = _read_bounded_line(f)
    except OSError:
        return False
    if not first_line or exceeded:
        return False
    try:
        obj = json.loads(first_line.decode("utf-8", errors="replace"))
    except ValueError:
        return False
    if not isinstance(obj, dict) or obj.get("type") != "session_meta":
        return False
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return False
    meta_id = str(payload.get("id") or "").strip().lower()
    if not meta_id or meta_id != want_tid:
        return False
    if bool(payload.get("is_subagent")) or bool(payload.get("parent_thread_id")):
        return False
    src = payload.get("source")
    if isinstance(src, Mapping):
        if "subagent" in src or any("subagent" in str(k).lower() for k in src):
            return False
    elif isinstance(src, str):
        s_low = src.strip().lower()
        if s_low == "subagent" or s_low.startswith("subagent:"):
            return False
    return True


def _validate_codex_local_cwd(
    raw_cwd: str, *, home: Path, wsl: bool, env: Mapping[str, str] | None = None
) -> str:
    """Validate a structured absolute local workspace path (or ``file://`` URI) from Codex DB."""
    s = (raw_cwd or "").strip()
    if not s or "\x00" in s:
        return ""
    if s.lower().startswith(("file://", "vscode-remote://")):
        s = _decode_structured_workspace_uri(s, wsl=wsl, env=env)
        if not s:
            return ""
    if not os.path.isabs(s) or is_wsl_slow_mount_path(s, wsl=wsl):
        return ""
    safe_dir = _resolve_safe_local_dir(s, wsl=wsl)
    if not safe_dir or _is_broad_or_unsafe_workspace_dir(safe_dir, home, wsl=wsl):
        return ""
    return safe_dir


def _validate_codex_rollout_path(
    raw_path: str, sessions_root: Path, expected_thread_id: str, *, wsl: bool
) -> tuple[str, tuple[Path, os.stat_result] | None]:
    """Validate a Codex rollout file is non-symlink, non-empty, under ``sessions_root``, and has matching root ``session_meta``."""
    s = (raw_path or "").strip()
    if not s or "\x00" in s or is_wsl_slow_mount_path(s, wsl=wsl):
        return "rejected", None
    rp = Path(s)
    try:
        lst = os.lstat(rp)
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "rejected", None
    if not stat.S_ISREG(lst.st_mode) or lst.st_size <= 0:
        return "rejected", None
    if hasattr(os, "getuid") and lst.st_uid != os.getuid():
        return "rejected", None
    real_rp = _safe_realpath(str(rp), wsl=wsl)
    if not real_rp:
        return "rejected", None
    real_rp_path = Path(real_rp)
    if not real_rp_path.is_relative_to(sessions_root):
        return "rejected", None
    if not _validate_codex_rollout_meta(real_rp_path, expected_thread_id):
        return "rejected", None
    return "ok", (real_rp_path, lst)


def _find_codex_rollout_in_sessions(
    sessions_root: Path, thread_id: str, *, wsl: bool
) -> tuple[Path, os.stat_result] | None:
    """Lightweight bounded fallback scan under ``~/.codex/sessions/`` by ``thread_id``."""
    tid_low = thread_id.strip().lower()
    if not tid_low:
        return None
    best: tuple[float, Path, os.stat_result] | None = None

    def _walk(dir_path: Path, depth: int) -> None:
        nonlocal best
        if depth > 4:
            return
        subdirs: list[Path] = []
        try:
            with os.scandir(dir_path) as it:
                for entry in it:
                    name = entry.name
                    if not name or name.startswith("."):
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            subdirs.append(Path(entry.path))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    nlow = name.lower()
                    if (
                        nlow == f"{tid_low}.jsonl"
                        or nlow.endswith(f"-{tid_low}.jsonl")
                        or nlow.endswith(f"_{tid_low}.jsonl")
                    ):
                        status, hit = _validate_codex_rollout_path(
                            entry.path, sessions_root, thread_id, wsl=wsl
                        )
                        if status == "ok" and hit is not None:
                            rp_path, rp_st = hit
                            if best is None or rp_st.st_mtime > best[0]:
                                best = (rp_st.st_mtime, rp_path, rp_st)
        except OSError:
            return
        subdirs.sort(key=lambda p: p.name, reverse=True)
        for sd in subdirs:
            _walk(sd, depth + 1)

    _walk(sessions_root, 0)
    return (best[1], best[2]) if best is not None else None


def _lookup_codex_state_rollout(
    codex_dir: Path, thread_id: str, *, wsl: bool
) -> tuple[str, str]:
    """Query ``~/.codex/state_5.sqlite`` ``threads`` for ``rollout_path`` by ``id``.

    Distinguishes missing DB / absent row (``"missing"`` -> may scan ``sessions/``) from
    present-but-unverifiable DB (``"unverifiable"`` -> reject to avoid bypassing ``archived=1``).
    """
    state_db = codex_dir / "state_5.sqlite"
    if not os.path.lexists(state_db):
        return "missing", ""
    conn = _open_readonly_sqlite(state_db, wsl=wsl)
    if conn is None:
        return "unverifiable", ""
    try:
        with contextlib.closing(conn):
            cols = _get_sqlite_table_cols(conn, "threads")
            if "id" not in cols or "rollout_path" not in cols or "archived" not in cols:
                return "unverifiable", ""
            with contextlib.closing(
                conn.execute(
                    "SELECT rollout_path, COALESCE(archived, 0) FROM threads WHERE id = ?",
                    (thread_id,),
                )
            ) as cur:
                row = cur.fetchone()
    except (sqlite3.Error, OSError):
        return "unverifiable", ""
    if row is None:
        return "missing", ""
    try:
        is_archived = int(row[1] or 0) != 0
    except (TypeError, ValueError):
        return "unverifiable", ""
    if is_archived:
        return "archived", ""
    rp = str(row[0] or "").strip()
    return ("found", rp) if rp else ("missing", "")


def _resolve_codex_rollout_for_thread(
    codex_dir: Path,
    thread_id: str,
    *,
    explicit_rollout_path: str | None = None,
    wsl: bool,
) -> tuple[Path, os.stat_result] | None:
    if not _is_safe_agy_cid(thread_id):
        return None
    safe_sessions = _resolve_safe_local_dir(str(codex_dir / "sessions"), wsl=wsl)
    if not safe_sessions:
        return None
    sessions_root = Path(safe_sessions)

    if explicit_rollout_path is not None:
        rp_str = explicit_rollout_path.strip()
        if rp_str:
            status, hit = _validate_codex_rollout_path(
                rp_str, sessions_root, thread_id, wsl=wsl
            )
            if status == "ok":
                return hit
            if status == "rejected":
                return None
        return _find_codex_rollout_in_sessions(sessions_root, thread_id, wsl=wsl)

    db_status, db_rp = _lookup_codex_state_rollout(codex_dir, thread_id, wsl=wsl)
    if db_status in ("archived", "unverifiable"):
        return None
    if db_status == "found":
        status, hit = _validate_codex_rollout_path(
            db_rp, sessions_root, thread_id, wsl=wsl
        )
        if status == "ok":
            return hit
        if status == "rejected":
            return None
    return _find_codex_rollout_in_sessions(sessions_root, thread_id, wsl=wsl)


def _query_codex_catalog_rows(db_path: Path, *, wsl: bool) -> list[_CodexCatalogRow]:
    """Read all non-missing rows from ``~/.codex/sqlite/codex.db`` ``local_thread_catalog``."""
    conn = _open_readonly_sqlite(db_path, wsl=wsl)
    if conn is None:
        return []
    try:
        with contextlib.closing(conn):
            cols = _get_sqlite_table_cols(conn, "local_thread_catalog")
            id_col = "thread_id" if "thread_id" in cols else ("id" if "id" in cols else "")
            if (
                not id_col
                or "display_title" not in cols
                or "source_kind" not in cols
                or "host_id" not in cols
                or "cwd" not in cols
            ):
                return []
            sel_src = "source_kind"
            sel_host = "host_id"
            sel_cwd = "cwd"
            rec_col = next(
                (
                    c
                    for c in (
                        "source_recency_at",
                        "recency_at",
                        "recency_ts",
                        "recency_at_ms",
                        "last_active_at",
                    )
                    if c in cols
                ),
                "",
            )
            upd_col = next(
                (
                    c
                    for c in (
                        "updated_at",
                        "updated_ts",
                        "updated_at_ms",
                        "last_modified_at",
                        "created_at",
                    )
                    if c in cols
                ),
                "",
            )
            sel_rec = rec_col if rec_col else "0"
            sel_upd = upd_col if upd_col else "0"
            wheres = ["COALESCE(display_title, '') != ''"]
            if "missing_candidate" in cols:
                wheres.insert(0, "COALESCE(missing_candidate, 0) = 0")
            sql = (
                f"SELECT {id_col}, display_title, {sel_src}, {sel_host}, {sel_cwd}, "
                f"{sel_rec}, {sel_upd} FROM local_thread_catalog WHERE {' AND '.join(wheres)}"
            )
            with contextlib.closing(conn.execute(sql)) as cur:
                raw_rows = cur.fetchall()
    except (sqlite3.Error, OSError):
        return []

    out: list[_CodexCatalogRow] = []
    for raw_tid, raw_title, raw_src, raw_host, raw_cwd, raw_rec, raw_upd in raw_rows:
        tid = str(raw_tid or "").strip()
        title = str(raw_title or "").strip()
        if not title:
            continue
        src_kind = str(raw_src or "").strip().lower()
        host_id = str(raw_host or "").strip().lower()
        cwd_str = str(raw_cwd or "").strip()
        is_local = src_kind != "chatgpt" and host_id == "local" and bool(cwd_str) and bool(tid)
        out.append(
            _CodexCatalogRow(
                thread_id=tid,
                display_title=title,
                is_local=is_local,
                cwd=cwd_str,
                rollout_path=None,
                recency_ts=_parse_iso_timestamp(raw_rec),
                updated_ts=_parse_iso_timestamp(raw_upd),
            )
        )
    return out


def _query_codex_state_desktop_rows(db_path: Path, *, wsl: bool) -> list[_CodexCatalogRow]:
    """Fallback query against ``~/.codex/state_5.sqlite`` ``threads`` when ``codex.db`` is absent."""
    conn = _open_readonly_sqlite(db_path, wsl=wsl)
    if conn is None:
        return []
    try:
        with contextlib.closing(conn):
            cols = _get_sqlite_table_cols(conn, "threads")
            title_col = "name" if "name" in cols else ("title" if "title" in cols else "")
            if (
                not title_col
                or "id" not in cols
                or "cwd" not in cols
                or "originator" not in cols
                or "archived" not in cols
            ):
                return []
            sel_rp = "rollout_path" if "rollout_path" in cols else "''"
            rec_col = next(
                (c for c in ("recency_at_ms", "source_recency_at", "recency_at") if c in cols),
                "",
            )
            upd_col = next(
                (c for c in ("updated_at_ms", "updated_at", "created_at") if c in cols),
                "",
            )
            sel_rec = rec_col if rec_col else "0"
            sel_upd = upd_col if upd_col else "0"
            wheres = [
                "COALESCE(archived, 0) = 0",
                f"COALESCE({title_col}, '') != ''",
                "originator IN (?, ?)",
            ]
            sql = (
                f"SELECT id, {title_col}, cwd, {sel_rp}, {sel_rec}, {sel_upd} "
                f"FROM threads WHERE {' AND '.join(wheres)}"
            )
            with contextlib.closing(conn.execute(sql, _CODEX_DESKTOP_ORIGINATORS)) as cur:
                raw_rows = cur.fetchall()
    except (sqlite3.Error, OSError):
        return []

    out: list[_CodexCatalogRow] = []
    for raw_tid, raw_title, raw_cwd, raw_rp, raw_rec, raw_upd in raw_rows:
        tid = str(raw_tid or "").strip()
        title = str(raw_title or "").strip()
        if not tid or not title:
            continue
        cwd_str = str(raw_cwd or "").strip()
        out.append(
            _CodexCatalogRow(
                thread_id=tid,
                display_title=title,
                is_local=bool(cwd_str),
                cwd=cwd_str,
                rollout_path=str(raw_rp or "").strip(),
                recency_ts=_parse_iso_timestamp(raw_rec),
                updated_ts=_parse_iso_timestamp(raw_upd),
            )
        )
    return out


def _collect_codex_title_candidates(
    rows: Sequence[_CodexCatalogRow], search_titles: Sequence[str]
) -> list[tuple[_CodexCatalogRow, bool]]:
    """Collect all rows matching any title variant by exact title or truncated prefix.

    Merges both tiers before cloud veto and cross-repo uniqueness checks so any candidate
    that could explain the displayed title participates in safety vetoes; exact matches
    still rank ahead of truncated prefix matches after all safety checks pass.
    """
    by_idx: dict[int, tuple[_CodexCatalogRow, bool]] = {}
    for raw_t in search_titles:
        t = (raw_t or "").strip()
        if not t:
            continue
        trunc_prefix = _compute_trunc_prefix(t)
        for idx, row in enumerate(rows):
            if row.display_title == t:
                by_idx[idx] = (row, True)
            elif (
                idx not in by_idx
                and trunc_prefix
                and row.display_title.startswith(trunc_prefix)
                and len(row.display_title) > len(trunc_prefix)
            ):
                by_idx[idx] = (row, False)
    return list(by_idx.values())


def _query_codex_archived_thread_ids(
    codex_dir: Path, thread_ids: Sequence[str], *, wsl: bool
) -> set[str] | None:
    """Batch-query ``state_5.sqlite`` ``threads`` for ``archived != 0`` among ``thread_ids``.

    Returns ``None`` if ``state_5.sqlite`` is absent or unreadable (caller falls back to
    single-candidate ``_resolve_codex_rollout_for_thread`` verification).
    """
    unique_ids = list(dict.fromkeys(tid for tid in thread_ids if tid))
    if not unique_ids:
        return set()
    state_db = codex_dir / "state_5.sqlite"
    if not os.path.lexists(state_db):
        return None
    conn = _open_readonly_sqlite(state_db, wsl=wsl)
    if conn is None:
        return None
    archived: set[str] = set()
    try:
        with contextlib.closing(conn):
            cols = _get_sqlite_table_cols(conn, "threads")
            if "id" not in cols or "archived" not in cols:
                return None
            for i in range(0, len(unique_ids), 500):
                batch = unique_ids[i : i + 500]
                placeholders = ",".join("?" for _ in batch)
                with contextlib.closing(
                    conn.execute(
                        f"SELECT id, COALESCE(archived, 0) FROM threads WHERE id IN ({placeholders})",
                        batch,
                    )
                ) as cur:
                    for raw_id, raw_arch in cur.fetchall():
                        try:
                            if int(raw_arch or 0) != 0:
                                archived.add(str(raw_id or ""))
                        except (TypeError, ValueError):
                            return None
    except (sqlite3.Error, OSError):
        return None
    return archived


def resolve_codex_desktop_session_by_title(
    window_title: str,
    *,
    home: Path | None = None,
    wsl: bool | None = None,
    env: Mapping[str, str] | None = None,
) -> SessionMatch | None:
    """Resolve a local-repo Codex Desktop thread by window title via ``~/.codex/sqlite/codex.db`` (or ``state_5.sqlite`` fallback).

    Explicitly returns ``None`` whenever any matching candidate is a cloud/non-local conversation
    or when local candidates span multiple distinct repositories.
    """
    win_title = (window_title or "").strip()
    if not win_title or win_title.lower() in _CODEX_DESKTOP_REJECT_TITLES:
        return None
    eff_env = os.environ if env is None else env
    eff_home = home if home is not None else Path((eff_env.get("HOME") or "").strip() or Path.home())
    eff_wsl = _effective_is_wsl(wsl, env)

    safe_codex_dir = _resolve_safe_local_dir(str(eff_home / ".codex"), wsl=eff_wsl)
    if not safe_codex_dir:
        return None
    codex_dir = Path(safe_codex_dir)

    codex_db_path = codex_dir / "sqlite" / "codex.db"
    from_codex_db = os.path.lexists(codex_db_path)
    if from_codex_db:
        # Present codex.db is authoritative: lock/corruption/missing columns must fail closed (never bypass cloud veto).
        rows = _query_codex_catalog_rows(codex_db_path, wsl=eff_wsl)
    else:
        rows = _query_codex_state_desktop_rows(codex_dir / "state_5.sqlite", wsl=eff_wsl)
    if not rows:
        return None

    search_titles = [win_title]
    stripped_title = _strip_trailing_app_suffix(
        win_title, _CODEX_DESKTOP_APP_SUFFIXES, allow_tight_dash=True
    )
    if (
        stripped_title
        and stripped_title != win_title
        and stripped_title.lower() not in _CODEX_DESKTOP_REJECT_TITLES
    ):
        search_titles.append(stripped_title)

    pool = _collect_codex_title_candidates(rows, search_titles)
    if not pool:
        return None
    # Cloud / non-local veto takes priority over recency and exact-vs-prefix across all matched candidates.
    if any(not row.is_local for row, _ in pool):
        return None

    # Require all matched local candidates to converge to a single validated local workspace directory.
    validated: list[tuple[_CodexCatalogRow, bool, str]] = []
    distinct_cwds: set[str] = set()
    for row, is_exact in pool:
        safe_cwd = _validate_codex_local_cwd(row.cwd, home=eff_home, wsl=eff_wsl, env=eff_env)
        if not safe_cwd:
            return None
        validated.append((row, is_exact, safe_cwd))
        distinct_cwds.add(safe_cwd)
    if len(distinct_cwds) != 1:
        return None

    if from_codex_db and len(validated) > 1:
        archived_ids = _query_codex_archived_thread_ids(
            codex_dir, [row.thread_id for row, _, _ in validated], wsl=eff_wsl
        )
        if archived_ids is not None:
            validated = [item for item in validated if item[0].thread_id not in archived_ids]
            if not validated:
                return None

    chosen, _, safe_cwd = max(
        validated,
        key=lambda item: (
            1 if item[1] else 0,
            item[0].recency_ts or item[0].updated_ts,
            item[0].updated_ts,
        ),
    )

    resolved = _resolve_codex_rollout_for_thread(
        codex_dir,
        chosen.thread_id,
        explicit_rollout_path=chosen.rollout_path,
        wsl=eff_wsl,
    )
    if resolved is None:
        return None
    rp_path, rp_stat = resolved
    return SessionMatch(
        cli="codex",
        session_id=chosen.thread_id,
        jsonl_path=str(rp_path),
        cwd=safe_cwd,
        matched_by="codex_desktop_db",
        jsonl_mtime=rp_stat.st_mtime,
    )


_CLAUDE_DESKTOP_REJECT_TITLES: frozenset[str] = frozenset(
    {
        "claude",
        "claude code",
        "new chat",
        "new conversation",
        "new session",
        "settings",
        "preferences",
        "welcome",
        "about",
        "search",
    }
)
_CLAUDE_DESKTOP_META_MAX_BYTES: int = 1048576  # 1 MiB safety cap
_CLAUDE_DESKTOP_ROUTE_RE = re.compile(
    r"^/(cowork|epitaxy|code)/(local_[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/?$",
    re.IGNORECASE,
)
_CLAUDE_JSONL_VALIDATE_HEAD_LINES: int = 64
_CLAUDE_JSONL_VALIDATE_MAX_LINES: int = 512
_CLAUDE_JSONL_VALIDATE_MAX_BYTES: int = 524288  # 512 KiB
_CLAUDE_META_CACHE_LOCK = threading.Lock()
_CLAUDE_META_CACHE_MAX: int = 2048


@dataclass(frozen=True)
class _ClaudeMetaSummary:
    base_dir: Path
    meta_path: Path
    meta_mtime: float
    session_id: str
    cli_session_id: str
    title: str
    started_env_id: str
    cwd: str
    fp_real: str
    fp_cwd: str
    origin_cwd: str
    user_selected_folders: Any
    resolved_folder_kinds: Any
    last_focused_ts: float
    last_activity_ts: float
    created_ts: float


_CLAUDE_META_CACHE: OrderedDict[str, tuple[int, int, _ClaudeMetaSummary | None]] = OrderedDict()


def _iter_claude_desktop_roots(home: Path) -> tuple[Path, Path]:
    """Return official and 3P Claude Desktop Application Support roots."""
    app_support = home / "Library" / "Application Support"
    return (app_support / "Claude", app_support / "Claude-3p")


def _parse_claude_desktop_page_url(page_url: str) -> tuple[str, str]:
    """Classify a sanitized Claude Desktop AXWebArea URL into ``('code'|'cowork'|'non_session'|'', local_sid)``."""
    raw = (page_url or "").strip()
    if not raw:
        return "", ""
    if len(raw) > 512 or any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        return "non_session", ""
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError:
        return "non_session", ""
    scheme = parts.scheme.lower()
    netloc = (parts.netloc or "").lower()
    if not ((scheme == "app" and netloc == "localhost") or (scheme == "https" and netloc == "claude.ai")):
        return "non_session", ""
    m = _CLAUDE_DESKTOP_ROUTE_RE.match(parts.path or "")
    if not m:
        return "non_session", ""
    kind = "cowork" if m.group(1).lower() == "cowork" else "code"
    local_sid = m.group(2)
    return (kind, local_sid) if _is_safe_agy_cid(local_sid) else ("non_session", "")


def _parse_claude_desktop_window_title(window_title: str) -> tuple[str, str]:
    """Classify a Claude Desktop window title into ``('code' | 'cowork' | '', stripped_title)``."""
    raw = (window_title or "").strip()
    if not raw or len(raw) > 512 or any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        return "", ""
    low = raw.lower()
    for admin_tag in (" [administrator]", " (admin)"):
        if low.endswith(admin_tag):
            raw = raw[: -len(admin_tag)].rstrip()
            low = raw.lower()
    if not raw or low in _CLAUDE_DESKTOP_REJECT_TITLES:
        return "", ""
    stripped_code = _strip_trailing_app_suffix(raw, ("claude code",), allow_tight_dash=True)
    if stripped_code != raw:
        if stripped_code and stripped_code.lower() not in _CLAUDE_DESKTOP_REJECT_TITLES:
            return "code", stripped_code
        return "", ""
    stripped_cowork = _strip_trailing_app_suffix(raw, ("claude",), allow_tight_dash=True)
    if stripped_cowork != raw:
        if stripped_cowork and stripped_cowork.lower() not in _CLAUDE_DESKTOP_REJECT_TITLES:
            return "cowork", stripped_cowork
        return "", ""
    return "", ""


def _iter_claude_account_dirs(
    home: Path, subdir_name: str, *, wsl: bool
) -> tuple[list[tuple[Path, Path]], bool]:
    """Bounded 2-level enumeration of ``<root>/<subdir>/<org>/<account>`` across Claude & Claude-3p.

    Returns ``(account_dirs, had_io_error)``. Missing paths (``FileNotFoundError`` /
    ``NotADirectoryError``) are skipped cleanly, whereas real read/permission errors
    propagate ``had_io_error=True`` so callers fail closed.
    """
    out: list[tuple[Path, Path]] = []
    had_io_error = False
    for root in _iter_claude_desktop_roots(home):
        sub_path = root / subdir_name
        try:
            os.lstat(sub_path)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            had_io_error = True
            continue
        safe_sub = _resolve_safe_local_dir(str(sub_path), wsl=wsl)
        if not safe_sub:
            continue
        base_dir = Path(safe_sub)
        org_dirs: list[Path] = []
        try:
            with os.scandir(base_dir) as org_it:
                for org_e in org_it:
                    oname = org_e.name
                    if not oname or oname.startswith(".") or oname.lower() == "skills-plugin":
                        continue
                    try:
                        if org_e.is_dir(follow_symlinks=False):
                            org_dirs.append(Path(org_e.path))
                    except (FileNotFoundError, NotADirectoryError):
                        continue
                    except OSError:
                        had_io_error = True
                        continue
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            had_io_error = True
            continue

        for org_dir in org_dirs:
            try:
                with os.scandir(org_dir) as acct_it:
                    for acct_e in acct_it:
                        aname = acct_e.name
                        if not aname or aname.startswith(".") or aname.lower() == "skills-plugin":
                            continue
                        try:
                            if acct_e.is_dir(follow_symlinks=False):
                                out.append((base_dir, Path(acct_e.path)))
                        except (FileNotFoundError, NotADirectoryError):
                            continue
                        except OSError:
                            had_io_error = True
                            continue
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError:
                had_io_error = True
                continue
    return out, had_io_error


def _load_claude_meta_cached(
    base_dir: Path, fpath: Path, lst: os.stat_result
) -> tuple[_ClaudeMetaSummary | None, bool]:
    """Stat-keyed cache of lightweight routing fields for a Claude Desktop ``local_*.json`` file.

    Returns ``(summary, had_io_error)``. Transient ``OSError`` reads never poison the LRU cache.
    """
    cache_key = str(fpath)
    sig_mtime = lst.st_mtime_ns
    sig_size = lst.st_size
    with _CLAUDE_META_CACHE_LOCK:
        cached = _CLAUDE_META_CACHE.get(cache_key)
        if cached is not None and cached[0] == sig_mtime and cached[1] == sig_size:
            _CLAUDE_META_CACHE.move_to_end(cache_key)
            return cached[2], False

    try:
        raw_text = fpath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, True

    summary: _ClaudeMetaSummary | None = None
    try:
        data = json.loads(raw_text)
    except ValueError:
        data = None
    if isinstance(data, dict) and not bool(data.get("isArchived")):
        fp = data.get("folderPlace")
        fp_real = str(fp.get("realPath") or "").strip() if isinstance(fp, Mapping) else ""
        fp_cwd = str(fp.get("cwd") or "").strip() if isinstance(fp, Mapping) else ""
        summary = _ClaudeMetaSummary(
            base_dir=base_dir,
            meta_path=fpath,
            meta_mtime=float(lst.st_mtime),
            session_id=str(data.get("sessionId") or "").strip(),
            cli_session_id=str(data.get("cliSessionId") or "").strip(),
            title=str(data.get("title") or "").strip(),
            started_env_id=str(data.get("startedFromEnvironmentId") or "").strip(),
            cwd=str(data.get("cwd") or "").strip(),
            fp_real=fp_real,
            fp_cwd=fp_cwd,
            origin_cwd=str(data.get("originCwd") or "").strip(),
            user_selected_folders=data.get("userSelectedFolders"),
            resolved_folder_kinds=data.get("resolvedFolderKinds"),
            last_focused_ts=_parse_iso_timestamp(data.get("lastFocusedAt")),
            last_activity_ts=_parse_iso_timestamp(data.get("lastActivityAt")),
            created_ts=_parse_iso_timestamp(data.get("createdAt")),
        )

    with _CLAUDE_META_CACHE_LOCK:
        _CLAUDE_META_CACHE[cache_key] = (sig_mtime, sig_size, summary)
        _CLAUDE_META_CACHE.move_to_end(cache_key)
        while len(_CLAUDE_META_CACHE) > _CLAUDE_META_CACHE_MAX:
            _CLAUDE_META_CACHE.popitem(last=False)
    return summary, False


def _stat_valid_claude_meta_file(
    fpath: Path, cur_uid: int | None
) -> tuple[os.stat_result | None, bool]:
    """Return ``(stat_result, had_io_error)``; distinguishes missing paths from real I/O errors."""
    try:
        lst = os.lstat(fpath)
    except (FileNotFoundError, NotADirectoryError):
        return None, False
    except OSError:
        return None, True
    if (
        not stat.S_ISREG(lst.st_mode)
        or lst.st_size <= 0
        or lst.st_size > _CLAUDE_DESKTOP_META_MAX_BYTES
    ):
        return None, False
    if cur_uid is not None and lst.st_uid != cur_uid:
        return None, False
    return lst, False


def _find_claude_meta_by_local_id(
    home: Path, subdir_name: str, local_sid: str, *, wsl: bool
) -> list[_ClaudeMetaSummary]:
    """Direct O(accounts) lookup of ``<local_sid>.json`` verifying internal ``sessionId == local_sid``."""
    if not _is_safe_agy_cid(local_sid):
        return []
    want_local_low = local_sid.strip().lower()
    cur_uid = os.getuid() if hasattr(os, "getuid") else None
    fname = f"{local_sid}.json"
    acct_dirs, dir_io_err = _iter_claude_account_dirs(home, subdir_name, wsl=wsl)
    if dir_io_err:
        return []
    hits: list[_ClaudeMetaSummary] = []
    for base_dir, acct_dir in acct_dirs:
        fpath = acct_dir / fname
        lst, stat_io_err = _stat_valid_claude_meta_file(fpath, cur_uid)
        if stat_io_err:
            return []
        if lst is None:
            continue
        summary, io_err = _load_claude_meta_cached(base_dir, fpath, lst)
        if io_err:
            return []
        if summary is not None:
            if not summary.session_id or summary.session_id.strip().lower() != want_local_low:
                return []
            hits.append(summary)
    return hits


def _scan_claude_session_metas(
    home: Path, subdir_name: str, *, wsl: bool
) -> tuple[list[_ClaudeMetaSummary], bool]:
    """Incremental stat-cached scan of ``local_*.json`` across Claude & Claude-3p.

    Returns ``(summaries, had_io_error)``. Callers performing title disambiguation must
    fail closed when ``had_io_error`` is ``True`` so unreadable candidates cannot hide conflicts.
    """
    cur_uid = os.getuid() if hasattr(os, "getuid") else None
    out: list[_ClaudeMetaSummary] = []
    scanned_bases: list[str] = []
    live_paths: set[str] = set()
    acct_dirs, had_io_error = _iter_claude_account_dirs(home, subdir_name, wsl=wsl)
    for base_dir, acct_dir in acct_dirs:
        b_pfx = str(base_dir) + os.sep
        if b_pfx not in scanned_bases:
            scanned_bases.append(b_pfx)
        try:
            with os.scandir(acct_dir) as file_it:
                for f_e in file_it:
                    fname = f_e.name
                    if not (fname.startswith("local_") and fname.endswith(".json")):
                        continue
                    fpath = Path(f_e.path)
                    lst, stat_io_err = _stat_valid_claude_meta_file(fpath, cur_uid)
                    if stat_io_err:
                        had_io_error = True
                        continue
                    if lst is None:
                        continue
                    live_paths.add(str(fpath))
                    summary, io_err = _load_claude_meta_cached(base_dir, fpath, lst)
                    if io_err:
                        had_io_error = True
                        continue
                    if summary is not None and summary.title:
                        out.append(summary)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            had_io_error = True
            continue
    if scanned_bases and not had_io_error:
        with _CLAUDE_META_CACHE_LOCK:
            stale = [
                k
                for k in _CLAUDE_META_CACHE
                if any(k.startswith(pfx) for pfx in scanned_bases) and k not in live_paths
            ]
            for k in stale:
                _CLAUDE_META_CACHE.pop(k, None)
    return out, had_io_error


def _validate_claude_desktop_jsonl(
    jsonl_path: Path, expected_cli_sid: str
) -> tuple[bool, str]:
    """Strictly verify a Claude Desktop transcript or audit JSONL carries explicit matching SID & clean dialogue.

    Enforces strict remaining-byte (512 KiB) and physical-line (512 lines, counting blank lines)
    budgets via ``readline(remaining + 1)``, inspecting at least 64 physical lines (or EOF) and
    stopping immediately without draining oversized lines when either budget is reached.
    """
    want_sid_low = (expected_cli_sid or "").strip().lower()
    resolved_sid = (expected_cli_sid or "").strip()
    saw_matching_sid = False
    saw_turn = False
    try:
        with open(jsonl_path, "rb") as f:
            phys_lines = 0
            scanned_bytes = 0
            while (
                phys_lines < _CLAUDE_JSONL_VALIDATE_MAX_LINES
                and scanned_bytes < _CLAUDE_JSONL_VALIDATE_MAX_BYTES
            ):
                remaining = _CLAUDE_JSONL_VALIDATE_MAX_BYTES - scanned_bytes
                raw = f.readline(remaining + 1)
                if not raw:
                    break
                phys_lines += 1
                scanned_bytes += len(raw)
                if len(raw) > remaining:
                    break
                if not raw.strip():
                    continue
                try:
                    obj = json.loads(raw.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if (
                    obj.get("agentId")
                    or obj.get("agent_id")
                    or obj.get("entrypoint") == "sdk-cli"
                    or bool(obj.get("isSidechain"))
                ):
                    return False, ""
                for sid_key in ("sessionId", "session_id"):
                    raw_sid = obj.get(sid_key)
                    if isinstance(raw_sid, str) and raw_sid.strip():
                        rec_sid = raw_sid.strip()
                        rec_low = rec_sid.lower()
                        if want_sid_low:
                            if rec_low != want_sid_low:
                                return False, ""
                        else:
                            want_sid_low = rec_low
                            resolved_sid = rec_sid
                        saw_matching_sid = True
                if not saw_turn and _clean_claude_record(obj) is not None:
                    saw_turn = True
                if (
                    phys_lines >= _CLAUDE_JSONL_VALIDATE_HEAD_LINES
                    and saw_matching_sid
                    and saw_turn
                ):
                    break
    except OSError:
        return False, ""
    if saw_turn and saw_matching_sid and resolved_sid:
        return True, resolved_sid
    return False, ""


def _resolve_claude_desktop_code_transcript(
    home: Path,
    cli_sid: str,
    cwd_hints: Sequence[str],
    *,
    wsl: bool,
) -> tuple[Path, os.stat_result] | None:
    if not _is_safe_agy_cid(cli_sid):
        return None
    safe_projects = _resolve_safe_local_dir(str(home / ".claude" / "projects"), wsl=wsl)
    if not safe_projects:
        return None
    projects_root = Path(safe_projects)
    cur_uid = os.getuid() if hasattr(os, "getuid") else None
    enc_dirs: list[str] = []
    for raw_c in cwd_hints:
        if not raw_c:
            continue
        for enc in encode_claude_project_dir(raw_c, wsl=wsl):
            if enc not in enc_dirs:
                enc_dirs.append(enc)
    for enc in enc_dirs:
        cand_tp = projects_root / enc / f"{cli_sid}.jsonl"
        try:
            tp_lst = os.lstat(cand_tp)
        except FileNotFoundError:
            continue
        except OSError:
            return None
        if not stat.S_ISREG(tp_lst.st_mode) or tp_lst.st_size <= 0:
            return None
        if cur_uid is not None and tp_lst.st_uid != cur_uid:
            return None
        if not _is_path_under_dir(cand_tp, projects_root, wsl=wsl):
            return None
        ok, _ = _validate_claude_desktop_jsonl(cand_tp, cli_sid)
        if not ok:
            return None
        return cand_tp, tp_lst
    return None


def _extract_kind_str(val: Any) -> str:
    if isinstance(val, str):
        return val.strip().lower()
    if isinstance(val, Mapping):
        raw_k = val.get("kind")
        if isinstance(raw_k, str):
            return raw_k.strip().lower()
    return ""


def _extract_cowork_folders(
    raw_folders: Any,
    raw_kinds: Any,
    *,
    home: Path,
    wsl: bool,
    env: Mapping[str, str] | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Extract ``(valid_local_folders, unresolved_raw_folders)`` from Cowork metadata.

    Never reads internal sandbox ``cwd``. Resolves ``resolvedFolderKinds`` by explicit path
    keys first, then unambiguous display matching, and uses positional indexing only for
    same-length pure-string lists; when ``resolvedFolderKinds`` is present but cannot be
    safely interpreted, keeps the session fail-safe with empty local folders while preserving
    raw paths for conflict disambiguation.
    """
    if not isinstance(raw_folders, list):
        return (), ()
    selected = [f.strip() for f in raw_folders if isinstance(f, str) and f.strip()]
    if not selected:
        return (), ()

    all_raw_unresolved = tuple(dict.fromkeys(_normalize_cwd(f, wsl=wsl) or f for f in selected))
    local_selected: list[str] = []

    if raw_kinds is None:
        local_selected = list(selected)
    elif isinstance(raw_kinds, Mapping):
        kind_map: dict[str, str] = {}
        for k, v in raw_kinds.items():
            if not isinstance(k, str) or not k.strip():
                continue
            k_str = k.strip()
            k_kind = _extract_kind_str(v)
            if not k_kind:
                return (), all_raw_unresolved
            kind_map[k_str] = k_kind
            norm_k = _normalize_cwd(k_str, wsl=wsl)
            if norm_k:
                kind_map[norm_k] = k_kind
        for f_str in selected:
            norm_f = _normalize_cwd(f_str, wsl=wsl)
            kind = kind_map.get(f_str) or (kind_map.get(norm_f) if norm_f else "")
            if not kind:
                return (), all_raw_unresolved
            if kind == "local":
                local_selected.append(f_str)
    elif isinstance(raw_kinds, list):
        if not raw_kinds:
            return (), all_raw_unresolved
        if all(isinstance(x, str) for x in raw_kinds):
            if len(raw_kinds) != len(selected):
                return (), all_raw_unresolved
            for f_str, raw_k in zip(selected, raw_kinds):
                kind = raw_k.strip().lower()
                if not kind:
                    return (), all_raw_unresolved
                if kind == "local":
                    local_selected.append(f_str)
        elif all(isinstance(x, Mapping) for x in raw_kinds):
            explicit_path_map: dict[str, str] = {}
            display_map: dict[str, str] = {}
            for item in raw_kinds:
                k_kind = _extract_kind_str(item)
                if not k_kind:
                    return (), all_raw_unresolved
                p_val = ""
                for pkey in ("path", "folderPath", "folder_path", "realPath", "uri"):
                    rv = item.get(pkey)
                    if isinstance(rv, str) and rv.strip():
                        p_val = rv.strip()
                        break
                if p_val:
                    if p_val in explicit_path_map and explicit_path_map[p_val] != k_kind:
                        return (), all_raw_unresolved
                    explicit_path_map[p_val] = k_kind
                    norm_p = _normalize_cwd(p_val, wsl=wsl)
                    if norm_p:
                        if norm_p in explicit_path_map and explicit_path_map[norm_p] != k_kind:
                            return (), all_raw_unresolved
                        explicit_path_map[norm_p] = k_kind
                else:
                    disp = str(item.get("display") or "").strip()
                    if not disp:
                        return (), all_raw_unresolved
                    if disp in display_map and display_map[disp] != k_kind:
                        return (), all_raw_unresolved
                    display_map[disp] = k_kind

            resolved_kinds: list[str] = [""] * len(selected)
            unmatched: list[tuple[int, str]] = []
            for idx, f_str in enumerate(selected):
                norm_f = _normalize_cwd(f_str, wsl=wsl)
                kind = explicit_path_map.get(f_str) or (
                    explicit_path_map.get(norm_f) if norm_f else ""
                )
                if kind:
                    resolved_kinds[idx] = kind
                else:
                    unmatched.append((idx, f_str))

            if unmatched:
                rem_basenames = [os.path.basename(f.rstrip("/\\")) for _, f in unmatched]
                dup_basenames = {b for b in rem_basenames if b and rem_basenames.count(b) > 1}
                for (idx, f_str), bname in zip(unmatched, rem_basenames):
                    kind = display_map.get(f_str, "")
                    if not kind and bname and bname not in dup_basenames:
                        kind = display_map.get(bname, "")
                    if not kind:
                        return (), all_raw_unresolved
                    resolved_kinds[idx] = kind

            for f_str, kind in zip(selected, resolved_kinds):
                if kind == "local":
                    local_selected.append(f_str)
        else:
            return (), all_raw_unresolved
    else:
        return (), all_raw_unresolved

    valid: list[str] = []
    unresolved: list[str] = []
    for f_str in local_selected:
        safe_dir = _validate_codex_local_cwd(f_str, home=home, wsl=wsl, env=env)
        if safe_dir:
            if safe_dir not in valid:
                valid.append(safe_dir)
        else:
            norm_u = _normalize_cwd(f_str, wsl=wsl) or f_str
            if norm_u not in unresolved:
                unresolved.append(norm_u)
    return tuple(valid), tuple(unresolved)


def _resolve_claude_desktop_cowork_transcript(
    summary: _ClaudeMetaSummary,
    *,
    wsl: bool,
) -> tuple[Path, os.stat_result, str] | None:
    session_id_raw = summary.session_id
    raw_sid = session_id_raw[len("local_") :] if session_id_raw.startswith("local_") else session_id_raw
    if len(raw_sid) < 8 or not _is_safe_agy_cid(raw_sid):
        return None
    session_dir = summary.meta_path.parent / raw_sid[:8]
    try:
        sd_lst = os.lstat(session_dir)
    except OSError:
        return None
    if not stat.S_ISDIR(sd_lst.st_mode) or not _is_path_under_dir(session_dir, summary.base_dir, wsl=wsl):
        return None

    cur_uid = os.getuid() if hasattr(os, "getuid") else None
    cli_sid = summary.cli_session_id
    if cli_sid:
        if not _is_safe_agy_cid(cli_sid):
            return None
        primary_tp = session_dir / ".claude" / "projects" / "session" / f"{cli_sid}.jsonl"
        try:
            p_lst = os.lstat(primary_tp)
        except FileNotFoundError:
            p_lst = None
        except OSError:
            return None
        if p_lst is not None:
            if not stat.S_ISREG(p_lst.st_mode) or p_lst.st_size <= 0:
                return None
            if cur_uid is not None and p_lst.st_uid != cur_uid:
                return None
            if not _is_path_under_dir(primary_tp, summary.base_dir, wsl=wsl):
                return None
            ok, resolved_sid = _validate_claude_desktop_jsonl(primary_tp, cli_sid)
            if not ok:
                return None
            return primary_tp, p_lst, resolved_sid

    audit_tp = session_dir / "audit.jsonl"
    try:
        a_lst = os.lstat(audit_tp)
    except OSError:
        return None
    if not stat.S_ISREG(a_lst.st_mode) or a_lst.st_size <= 0:
        return None
    if cur_uid is not None and a_lst.st_uid != cur_uid:
        return None
    if not _is_path_under_dir(audit_tp, summary.base_dir, wsl=wsl):
        return None
    ok, resolved_sid = _validate_claude_desktop_jsonl(audit_tp, cli_sid)
    if not ok:
        return None
    return audit_tp, a_lst, resolved_sid


def _finalize_claude_code_candidates(
    pool: Sequence[tuple[_ClaudeMetaSummary, bool]],
    *,
    home: Path,
    wsl: bool,
    env: Mapping[str, str],
) -> SessionMatch | None:
    if not pool:
        return None
    code_cands: list[tuple[_ClaudeMetaSummary, bool, str, tuple[str, ...]]] = []
    distinct_cwds: set[str] = set()
    distinct_sids: set[str] = set()
    for summary, is_exact in pool:
        if summary.started_env_id and summary.started_env_id.lower() != "local":
            return None
        safe_cwd = ""
        for cand_c in (summary.cwd, summary.fp_real, summary.fp_cwd):
            if cand_c:
                safe_cwd = _validate_codex_local_cwd(cand_c, home=home, wsl=wsl, env=env)
                if safe_cwd:
                    break
        if not safe_cwd or not summary.cli_session_id:
            return None
        distinct_cwds.add(safe_cwd)
        distinct_sids.add(summary.cli_session_id.lower())
        code_cands.append(
            (summary, is_exact, safe_cwd, (summary.cwd, summary.fp_real, summary.fp_cwd, safe_cwd, summary.origin_cwd))
        )
    # Fail closed if candidates span multiple repos OR multiple distinct active session IDs (e.g. Claude + Claude-3p).
    if len(distinct_cwds) != 1 or len(distinct_sids) != 1:
        return None

    chosen_summary, _, safe_cwd, cwd_hints = max(
        code_cands,
        key=lambda item: (
            1 if item[1] else 0,
            item[0].last_focused_ts,
            item[0].last_activity_ts,
            item[0].created_ts,
            item[0].meta_mtime,
        ),
    )
    resolved_code = _resolve_claude_desktop_code_transcript(
        home, chosen_summary.cli_session_id, cwd_hints, wsl=wsl
    )
    if resolved_code is None:
        return None
    tp_path, tp_stat = resolved_code
    return SessionMatch(
        cli="claude",
        session_id=chosen_summary.cli_session_id,
        jsonl_path=str(tp_path),
        cwd=safe_cwd,
        matched_by="claude_desktop_code",
        jsonl_mtime=tp_stat.st_mtime,
    )


def _finalize_claude_cowork_candidates(
    pool: Sequence[tuple[_ClaudeMetaSummary, bool]],
    *,
    home: Path,
    wsl: bool,
    env: Mapping[str, str],
) -> SessionMatch | None:
    if not pool:
        return None
    cowork_cands: list[tuple[_ClaudeMetaSummary, bool, tuple[str, ...]]] = []
    distinct_identities: set[str] = set()
    distinct_folder_sigs: set[tuple[frozenset[str], frozenset[str]]] = set()
    for summary, is_exact in pool:
        valid_folders, unresolved_raw = _extract_cowork_folders(
            summary.user_selected_folders,
            summary.resolved_folder_kinds,
            home=home,
            wsl=wsl,
            env=env,
        )
        sid_id = (summary.cli_session_id or summary.session_id).strip().lower()
        if not sid_id:
            return None
        distinct_identities.add(sid_id)
        distinct_folder_sigs.add((frozenset(valid_folders), frozenset(unresolved_raw)))
        cowork_cands.append((summary, is_exact, valid_folders))
    if len(distinct_identities) != 1 or len(distinct_folder_sigs) != 1:
        return None

    chosen_summary, _, chosen_folders = max(
        cowork_cands,
        key=lambda item: (
            1 if item[1] else 0,
            item[0].last_activity_ts,
            item[0].created_ts,
            item[0].meta_mtime,
        ),
    )
    resolved_cowork = _resolve_claude_desktop_cowork_transcript(chosen_summary, wsl=wsl)
    if resolved_cowork is None:
        return None
    tp_path, tp_stat, eff_sid = resolved_cowork
    eff_cwd = chosen_folders[0] if len(chosen_folders) == 1 else ""
    return SessionMatch(
        cli="claude",
        session_id=eff_sid,
        jsonl_path=str(tp_path),
        cwd=eff_cwd,
        matched_by="claude_desktop_cowork",
        jsonl_mtime=tp_stat.st_mtime,
    )


def resolve_claude_desktop_session_by_title(
    window_title: str,
    *,
    page_url: str = "",
    home: Path | None = None,
    wsl: bool | None = None,
    env: Mapping[str, str] | None = None,
) -> SessionMatch | None:
    """Resolve a Claude Desktop Code or Cowork session via exact AXWebArea route URL or Code-title fallback.

    Primary path uses ``page_url`` (``app://localhost/cowork/local_<uuid>`` or
    ``app://localhost/epitaxy/local_<uuid>``) to directly pinpoint the session metadata file,
    failing closed on cloud/blank routes (``/new``, ``/chat/...``, ``/epitaxy`` without ID).
    When ``page_url`` is absent, only ``" - Claude Code"`` titles fall back to incremental
    stat-cached scanning (failing closed on multi-SID ties), while bare ``" - Claude"`` titles
    fail closed to prevent cloud Chat from matching same-titled Cowork sessions.
    """
    eff_env = os.environ if env is None else env
    eff_home = home if home is not None else Path((eff_env.get("HOME") or "").strip() or Path.home())
    eff_wsl = _effective_is_wsl(wsl, env)

    route_kind, route_local_sid = _parse_claude_desktop_page_url(page_url)
    if route_kind == "non_session":
        return None
    if route_kind == "code":
        hits = _find_claude_meta_by_local_id(eff_home, "claude-code-sessions", route_local_sid, wsl=eff_wsl)
        return _finalize_claude_code_candidates(
            [(h, True) for h in hits], home=eff_home, wsl=eff_wsl, env=eff_env
        )
    if route_kind == "cowork":
        hits = _find_claude_meta_by_local_id(eff_home, "local-agent-mode-sessions", route_local_sid, wsl=eff_wsl)
        return _finalize_claude_cowork_candidates(
            [(h, True) for h in hits], home=eff_home, wsl=eff_wsl, env=eff_env
        )

    # No page_url available: only " - Claude Code" may fall back to title scanning;
    # " - Claude" without a /cowork/local_<uuid> route cannot distinguish Chat from Cowork -> fail closed.
    mode_kind, stripped_title = _parse_claude_desktop_window_title(window_title)
    if mode_kind != "code" or not stripped_title:
        return None
    summaries, had_io_error = _scan_claude_session_metas(eff_home, "claude-code-sessions", wsl=eff_wsl)
    if had_io_error or not summaries:
        return None

    trunc_prefix = _compute_trunc_prefix(stripped_title)
    is_truncated = stripped_title.endswith("...") or stripped_title.endswith("\u2026")
    pool: list[tuple[_ClaudeMetaSummary, bool]] = []
    for summary in summaries:
        is_exact = summary.title == stripped_title
        is_prefix = (
            not is_exact
            and bool(trunc_prefix)
            and summary.title.startswith(trunc_prefix)
            and len(summary.title) > len(trunc_prefix)
        )
        if is_exact or is_prefix:
            pool.append((summary, is_exact))
    if not pool:
        return None

    if not is_truncated:
        exact_only = [item for item in pool if item[1]]
        if exact_only:
            pool = exact_only
    return _finalize_claude_code_candidates(pool, home=eff_home, wsl=eff_wsl, env=eff_env)


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
        ev = _load_agy_ide_evidence(cid_dir, tp, st, home, wsl=eff_wsl, env=env)
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
    failed_db_roots: set[Path] = set()
    for root in _iter_agy_ide_roots(home):
        real_root = _resolve_safe_local_dir(str(root), wsl=eff_wsl)
        if not real_root:
            continue
        root_path = Path(real_root)
        matched_rows, vetoed, db_ok = _scan_agy_ide_db_for_prior(
            root_path, safe_ws, home, wsl=eff_wsl, excl_low=excl_low, env=env
        )
        per_root_vetoes[root_path] = vetoed
        if not db_ok:
            failed_db_roots.add(root_path)
            continue
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
        if root_path in failed_db_roots:
            continue
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
        ev = _load_agy_ide_evidence(cid_dir, tp, st, home, wsl=eff_wsl, env=env)
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
    git_env = {**(env if env is not None else os.environ), "GIT_NO_LAZY_FETCH": "1"}

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
    git_env = {**(env if env is not None else os.environ), "GIT_NO_LAZY_FETCH": "1"}
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout_sec,
            check=False,
            env=git_env,
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
        if base and (low := base.lower()) not in seen:
            seen.add(low)
            hotwords.append(base)

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
