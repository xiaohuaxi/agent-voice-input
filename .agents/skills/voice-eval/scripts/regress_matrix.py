#!/usr/bin/env python3
"""Dual-platform (macOS & WSL2) regression matrix and live daemon verification suite.

Three composable verification layers (all redacted, zero conversation prose printed):
  Layer A (default, skip with --no-matrix):
    Offline, zero-cost, zero-disruption full routing matrix against an isolated
    0700 temp HOME + runtime_dir running a real VoiceInputDaemon over control.sock:
      1) Codex / Claude Code (with 2.1.330 giant attachment prefilter) / AGY CLI
         old-session hit -> same-dir new empty session prior_session (including
         cross-CLI inheritance) -> empty shell / unrelated dir zero crosstalk,
         using real `bin/agent-voice-input session-hook --cli claude|agy --quiet`.
      2) Dual-platform desktop & remote anti-crosstalk matrix (macOS bundle_id/document
         & Windows process/title): Antigravity 2.0 WAL DB (exact, truncated `...`,
         "New Chat" veto), ChatGPT/Codex unified desktop WAL DB (local promo vs.
         cloud `source_kind='chatgpt'` veto), Claude desktop / WeChat / Chrome
         (`general_gui`), VS Code / Cursor (`coding_gui`), and SSH remote veto
         (`putty.exe`, `[@remote]`, `[SSH: devbox]`).
  Layer B (--live-daemon):
    Read-only / non-destructive verification against the host's running daemon
    control.sock (`status` + drift check, `probe_route` virtual & live-window,
    `selftest_wav(no_telemetry=True)`, and opt-in `--deliver-live` GUI E2E paste).
  Layer C (--probe-real-clis):
    Strictly read-only compatibility check of locally installed Codex / Claude /
    AGY root transcripts and Antigravity 2.0 / Codex SQLite DBs (mode=ro).
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Mapping

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parents[3]
_SRC_ROOT = _REPO_ROOT / "src"
for _p in (str(_SRC_ROOT), str(_SCRIPT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent_voice_input import __version__  # noqa: E402
from agent_voice_input._remote_probe import (  # noqa: E402
    _CODEX_ENV_CTX_RE,
    _CODEX_PERM_CTX_RE,
    _CODEX_REC_PLUGINS_RE,
    _clean_jsonl_file,
    _is_agy_subagent,
    _read_bounded_line,
    _read_claude_root_meta,
    _read_codex_root_meta,
    _should_prefilter_skip_jsonl_line,
    encode_claude_project_dir,
)
from agent_voice_input._tmux import TmuxServerSnapshot  # noqa: E402
from agent_voice_input.asr_providers import AsrResult, BaseAsrProvider  # noqa: E402
from agent_voice_input.audio_proc import process_wav  # noqa: E402
from agent_voice_input.config import (  # noqa: E402
    Config,
    RefinerConfig,
    ensure_secure_dir,
    expand_user_path,
    get_runtime_dir,
    load_config,
    redact_secrets,
    resolve_credentials,
)
from agent_voice_input.daemon import (  # noqa: E402
    TELEMETRY_FILENAME,
    VoiceInputDaemon,
    describe_daemon_drift,
    send_control_command,
)
from agent_voice_input.platform_common import ForegroundWindow, PasteOutcome  # noqa: E402
from agent_voice_input.platform_macos import is_macos_terminal_window  # noqa: E402
from agent_voice_input.platform_wsl import is_windows_terminal_process  # noqa: E402
from agent_voice_input.refiner import BaseRefiner, RefinerResult  # noqa: E402
from agent_voice_input.session_ctx import (  # noqa: E402
    SessionTracker,
    write_session_registry_entry,
)
from agent_voice_input.workspace_ctx import (  # noqa: E402
    _CODEX_DESKTOP_ORIGINATORS,
    _effective_is_wsl,
    _get_sqlite_table_cols,
    _iter_agy_ide_roots,
    _open_readonly_sqlite,
)

try:
    import e2e_system as _e2e_mod  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - fallback only if sibling script missing
    _e2e_mod = None


LIVE_PROBE_TTS_SPEC: dict[str, Any] = {
    "id": "M1",
    "name": "regress_live_probe",
    "tts_text": "帮我检查一下当前窗口的语音输入路由和落字延迟",
}

_AGY_HELPER_CODE = """\
import fcntl
import json
import os
import subprocess
import sys

def main():
    locks = []
    try:
        for raw_p in sys.argv[1:]:
            fd = os.open(raw_p, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locks.append(fd)
        sys.stdout.write("READY\\n")
        sys.stdout.flush()
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            cmd = json.loads(line)
            if cmd.get("op") == "quit":
                break
            if cmd.get("op") == "run_hook":
                env = dict(os.environ)
                env.update(cmd.get("env") or {})
                proc = subprocess.run(
                    cmd["argv"],
                    input=json.dumps(cmd["payload"], ensure_ascii=False),
                    text=True,
                    capture_output=True,
                    env=env,
                    timeout=15,
                    check=False,
                )
                sys.stdout.write(json.dumps({
                    "rc": proc.returncode,
                    "stdout": proc.stdout,
                    "stderr": proc.stderr[:300],
                }) + "\\n")
                sys.stdout.flush()
    finally:
        for fd in locks:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass

if __name__ == "__main__":
    main()
"""


def _strip_fast_env(raw_env: Mapping[str, str]) -> dict[str, str]:
    clean = dict(raw_env)
    for k in list(clean.keys()):
        ku = k.upper()
        if "FAST" in ku and ("CODEX" in ku or "OPENAI" in ku or "VOICE" in ku):
            clean.pop(k, None)
    return clean


def _sanitize_process_env_no_fast() -> None:
    for k in list(os.environ.keys()):
        ku = k.upper()
        if "FAST" in ku and ("CODEX" in ku or "OPENAI" in ku or "VOICE" in ku):
            os.environ.pop(k, None)


_LAYER_A_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "LANGUAGE",
    "TZ",
    "TERM",
    "USER",
    "LOGNAME",
)
_LAYER_C_MAX_CANDIDATES: int = 5
_LAYER_C_SCHEMA_DRIFT_MIN_BYTES: int = 4096


def _build_layer_a_env(
    base_env: Mapping[str, str],
    *,
    iso_home: Path,
    iso_config: Path,
    iso_cache: Path,
    rt_dir: Path,
) -> dict[str, str]:
    """Build a minimal allowlisted env for Layer A, stripping host config, CODEX_HOME & API keys."""
    iso_env: dict[str, str] = {}
    for key in _LAYER_A_ENV_ALLOWLIST:
        val = base_env.get(key)
        if val is not None:
            iso_env[key] = val
    iso_env["HOME"] = str(iso_home)
    iso_env["XDG_CONFIG_HOME"] = str(iso_config)
    iso_env["XDG_CACHE_HOME"] = str(iso_cache)
    iso_env["XDG_RUNTIME_DIR"] = str(rt_dir)
    iso_env["AGENT_VOICE_INPUT_RUNTIME_DIR"] = str(rt_dir)
    iso_env["PYTHONPATH"] = str(_SRC_ROOT)
    iso_env["PYTHONUTF8"] = "1"
    return iso_env


def _make_short_tempdir(prefix: str = "avi-regress-") -> tempfile.TemporaryDirectory[str]:
    """Prefer ``/tmp`` on POSIX so AF_UNIX control.sock paths stay well below macOS's 104-byte cap."""
    if os.name == "posix":
        tmp_root = Path("/tmp")
        if tmp_root.is_dir() and os.access(tmp_root, os.W_OK | os.X_OK):
            try:
                return tempfile.TemporaryDirectory(prefix=prefix, dir=str(tmp_root))
            except OSError:
                pass
    return tempfile.TemporaryDirectory(prefix=prefix)


class _DummyAsr(BaseAsrProvider):
    provider_name = "offline_dummy"

    def __init__(self) -> None:
        pass

    def transcribe(self, wav_bytes: bytes, *, context: Any = None, is_cancelled: Any = None) -> AsrResult:
        del wav_bytes, context, is_cancelled
        return AsrResult(text="", provider="offline_dummy", model="none", latency_ms=0)


class _DummyRefiner(BaseRefiner):
    provider_name = "off"

    def __init__(self) -> None:
        pass

    def refine(self, asr: AsrResult, **kwargs: Any) -> RefinerResult:
        del kwargs
        return RefinerResult(text=asr.text, provider="off", model="", latency_ms=0)

    def close(self) -> None:
        pass


class _HeadlessMatrixPlatform:
    """Dual-platform headless backend using real terminal classification rules."""

    def __init__(self) -> None:
        self._shim_disabled = False
        self.last_shim_error = ""
        self.last_audio_error = ""

    def is_shim_ok(self) -> bool:
        return True

    def is_allowed_terminal(self, window: ForegroundWindow) -> bool:
        return is_windows_terminal_process(window.process) or is_macos_terminal_window(
            window.process, window.bundle_id
        )

    def set_recorder_lost_callback(self, cb: Callable[..., Any] | None) -> None:
        del cb

    def set_recorder_recovered_callback(self, cb: Callable[..., Any] | None) -> None:
        del cb

    def start_event_listener(self, on_hotkey_event: Callable[[dict[str, Any]], None]) -> None:
        del on_hotkey_event

    def get_foreground_window(self) -> ForegroundWindow:
        return ForegroundWindow()

    def start_recording(self, runtime_dir: Path, *, loopback: bool = False, seq_id: int | None = None) -> None:
        del runtime_dir, loopback, seq_id

    def stop_recording(self, *, drain: bool = True) -> tuple[bytes, str]:
        del drain
        return b"", "headless"

    def cancel_recording(self) -> None:
        pass

    def switch_to_fallback_audio(self) -> bool:
        return False

    def hud(self, state: str, **kwargs: Any) -> None:
        del state, kwargs

    def beep(self, kind: str) -> None:
        del kind

    def deliver_text(self, text: str, seq_id: int, target_hwnd: int = 0, **kwargs: Any) -> PasteOutcome:
        del text, seq_id, target_hwnd, kwargs
        return PasteOutcome(False, False, "headless_noop", "headless")

    def close(self) -> None:
        pass


def _wait_control_socket(rt_dir: Path, timeout_sec: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_sec
    last_err = ""
    while time.monotonic() < deadline:
        try:
            resp = send_control_command(rt_dir, {"cmd": "status"}, timeout_sec=0.5)
            if resp.get("ok"):
                return resp
            last_err = str(resp)
        except Exception as exc:
            last_err = str(exc)
        time.sleep(0.05)
    raise TimeoutError(f"control.sock did not become ready within {timeout_sec}s: {last_err}")


def _write_codex_rollout(
    path: Path,
    sid: str,
    cwd: str,
    turns: list[tuple[str, str]],
    *,
    source: str = "cli",
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            {"type": "session_meta", "payload": {"id": sid, "cwd": cwd, "source": source}},
            ensure_ascii=False,
        )
    ]
    for role, text in turns:
        lines.append(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "item": {
                            "type": "UserMessage" if role == "user" else "AgentMessage",
                            "content": [{"type": "text", "text": text}],
                        },
                    },
                },
                ensure_ascii=False,
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_claude_jsonl(
    path: Path,
    sid: str,
    cwd: str,
    turns: list[tuple[str, str]],
    *,
    inject_giant_attachment: bool = False,
) -> tuple[Path, bytes]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    giant_line_bytes = b""
    for idx, (role, text) in enumerate(turns):
        lines.append(
            json.dumps(
                {
                    "type": role,
                    "isSidechain": False,
                    "sessionId": sid,
                    "cwd": cwd,
                    "message": {
                        "role": role,
                        "content": [{"type": "text", "text": text}],
                    },
                },
                ensure_ascii=False,
            )
        )
        if inject_giant_attachment and idx == 0:
            giant_obj = {
                "type": "attachment",
                "sessionId": sid,
                "cwd": cwd,
                "attachment": {
                    "kind": "tool_output_blob",
                    "payload": "X" * 240000,
                },
            }
            giant_str = json.dumps(giant_obj, ensure_ascii=False)
            giant_line_bytes = giant_str.encode("utf-8")
            lines.append(giant_str)
    content = ("\n".join(lines) + "\n") if lines else ""
    path.write_text(content, encoding="utf-8")
    return path, giant_line_bytes


def _write_agy_transcript(
    path: Path,
    cwd: str,
    turns: list[tuple[str, str]],
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if cwd:
        lines.append(
            json.dumps(
                {
                    "source": "SYSTEM",
                    "type": "SYSTEM_INFO",
                    "cwd": cwd,
                    "content": f"Active Workspaces:\n- {cwd}\n",
                },
                ensure_ascii=False,
            )
        )
    for role, text in turns:
        if role == "user":
            lines.append(
                json.dumps(
                    {
                        "source": "USER_EXPLICIT",
                        "type": "USER_INPUT",
                        "content": f"<USER_REQUEST>\n{text}\n</USER_REQUEST>",
                    },
                    ensure_ascii=False,
                )
            )
        else:
            lines.append(
                json.dumps(
                    {
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "content": text,
                    },
                    ensure_ascii=False,
                )
            )
    path.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")
    return path


def _make_check(
    check_id: str,
    layer: str,
    status: str,
    detail: str,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": check_id,
        "layer": layer,
        "status": status,  # PASS | FAIL | WARN | SKIP
        "detail": detail,
    }
    if metrics:
        item["metrics"] = metrics
    return item


def _check_asserts(
    check_id: str,
    layer: str,
    resp: Mapping[str, Any],
    assertions: list[tuple[str, bool]],
    *,
    extra_metrics: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    failed = [desc for desc, ok in assertions if not ok]
    metrics: dict[str, Any] = {
        "window_category": resp.get("window_category", ""),
        "mode": resp.get("mode", ""),
        "target_app_hint": resp.get("target_app_hint", ""),
        "hotwords_scope": resp.get("hotwords_scope", ""),
        "session_key_masked": resp.get("session_key_masked", ""),
        "matched_by": resp.get("matched_by", ""),
        "remote_vetoed": bool(resp.get("remote_vetoed", False)),
        "workspace_name": resp.get("workspace_name", ""),
        "ctx_chars": int(resp.get("ctx_chars") or 0),
        "ctx_fallback": resp.get("ctx_fallback", ""),
        "sync_ms": int(resp.get("sync_ms") or 0),
    }
    if extra_metrics:
        metrics.update(extra_metrics)
    if failed:
        return _make_check(
            check_id,
            layer,
            "FAIL",
            f"failed assertions: {'; '.join(failed)}",
            metrics,
        )
    return _make_check(
        check_id,
        layer,
        "PASS",
        (
            f"mode={metrics['mode']} cat={metrics['window_category']} "
            f"key={metrics['session_key_masked'] or '-'} matched={metrics['matched_by'] or '-'} "
            f"ws={metrics['workspace_name'] or '-'} ctx={metrics['ctx_chars']} "
            f"fb={metrics['ctx_fallback'] or '-'} remote_veto={metrics['remote_vetoed']}"
        ),
        metrics,
    )


def _create_wal_sqlite(db_path: Path, statements: list[tuple[str, tuple[Any, ...]]]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        for sql, params in statements:
            conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def run_layer_a_matrix(
    clean_env: Mapping[str, str],
    redact_fn: Callable[[Any], str] | None = None,
) -> list[dict[str, Any]]:
    """Layer A: offline zero-cost routing matrix over real VoiceInputDaemon control.sock."""
    checks: list[dict[str, Any]] = []
    bin_avi = _REPO_ROOT / "bin" / "agent-voice-input"
    redact = redact_fn if redact_fn is not None else (lambda s: redact_secrets(str(s), clean_env))

    with _make_short_tempdir(prefix="avi-regress-") as raw_td:
        td = Path(os.path.realpath(raw_td))
        os.chmod(td, 0o700)
        iso_home = ensure_secure_dir(td / "home")
        iso_config = ensure_secure_dir(td / "config")
        iso_cache = ensure_secure_dir(td / "cache")
        rt_dir = ensure_secure_dir(td / "rt")
        no_proc = td / "no_proc"  # intentionally absent -> symmetric across Linux/WSL2 & macOS

        repo_alpha = iso_home / "repos" / "alpha-service"
        repo_beta = iso_home / "repos" / "beta-studio"
        repo_gamma = iso_home / "repos" / "gamma-codex"
        unrelated_dir = iso_home / "tmp-unrelated"
        for repo in (repo_alpha, repo_beta, repo_gamma):
            (repo / ".git").mkdir(parents=True, exist_ok=True)
        (repo_alpha / "router_engine.py").write_text("class RouterEngine:\n    pass\n", encoding="utf-8")
        (repo_beta / "canvas_panel.ts").write_text("export const canvasPanel = true;\n", encoding="utf-8")
        (repo_gamma / "worker_pool.py").write_text("class WorkerPool:\n    pass\n", encoding="utf-8")
        unrelated_dir.mkdir(parents=True, exist_ok=True)

        iso_env = _build_layer_a_env(
            clean_env,
            iso_home=iso_home,
            iso_config=iso_config,
            iso_cache=iso_cache,
            rt_dir=rt_dir,
        )

        # ---- Build CLI sessions (distinct 8-char prefixes for registry isolation) ----
        rich_pad = "RouterEngine SessionTracker incremental prefix cache chunk verification details. " * 28
        codex_old_sid = "a1110001-2222-4333-8444-555555555501"
        codex_gamma_prior_sid = "a1110002-2222-4333-8444-555555555502"
        claude_old_sid = "b2220001-3333-4444-8555-666666666601"
        claude_new_gamma_sid = "b2220002-3333-4444-8555-666666666602"
        agy_old_cid = "c3330001-4444-4555-8666-777777777701"
        agy_new_alpha_cid = "c3330002-4444-4555-8666-777777777702"

        codex_old_path = _write_codex_rollout(
            iso_home / ".codex" / "sessions" / "2026" / "11" / "23" / f"rollout-2026-11-23T09-00-00-{codex_old_sid}.jsonl",
            codex_old_sid,
            str(repo_alpha),
            [
                ("user", "请排查 alpha-service 里的 RouterEngine 路由逻辑和缓存分块边界。"),
                ("assistant", f"已定位 alpha-service 的 RouterEngine 逻辑。{rich_pad}"),
                ("user", "确认增量读取与前缀分块没有回退。"),
                ("assistant", f"已确认增量字节偏移稳定。{rich_pad}"),
            ],
        )
        write_session_registry_entry(
            rt_dir,
            cli="codex",
            session_id=codex_old_sid,
            jsonl_path=str(codex_old_path),
            cwd=str(repo_alpha),
        )

        # Rich Codex prior session in repo_gamma (for cross-CLI Codex -> Claude prior_session inheritance)
        _write_codex_rollout(
            iso_home / ".codex" / "sessions" / "2026" / "11" / "23" / f"rollout-2026-11-23T08-30-00-{codex_gamma_prior_sid}.jsonl",
            codex_gamma_prior_sid,
            str(repo_gamma),
            [
                ("user", "请检查 gamma-codex 仓库里 WorkerPool 的任务调度和退避重试。"),
                ("assistant", f"已完成 WorkerPool 并发与退避分析。{rich_pad}"),
                ("user", "把关键字段和队列上限也记下来。"),
                ("assistant", f"已记录 WorkerPool 队列阈值与取消令牌边界。{rich_pad}"),
            ],
        )

        # Claude old long session in repo_alpha + 2.1.330 giant attachment (>240KB)
        claude_alpha_dir = iso_home / ".claude" / "projects" / encode_claude_project_dir(str(repo_alpha))[0]
        claude_old_path, giant_att_bytes = _write_claude_jsonl(
            claude_alpha_dir / f"{claude_old_sid}.jsonl",
            claude_old_sid,
            str(repo_alpha),
            [
                ("user", "帮我审一下 alpha-service 里 Claude 会话的字节预过滤和增量解析。"),
                ("assistant", f"已核查 alpha-service 的 Claude 会话增量解析逻辑。{rich_pad}"),
                ("user", "确认超大 attachment 不会被 json.loads 阻塞。"),
                ("assistant", f"已确认 >1KB 非对话 attachment 行在字节层直接跳过。{rich_pad}"),
            ],
            inject_giant_attachment=True,
        )
        prefilter_ok = (
            len(giant_att_bytes) > 200_000
            and _should_prefilter_skip_jsonl_line("claude", giant_att_bytes)
        )

        # Brand-new empty Claude session in repo_gamma (cross-CLI inherits Codex prior session)
        claude_gamma_dir = iso_home / ".claude" / "projects" / encode_claude_project_dir(str(repo_gamma))[0]
        claude_new_gamma_path, _ = _write_claude_jsonl(
            claude_gamma_dir / f"{claude_new_gamma_sid}.jsonl",
            claude_new_gamma_sid,
            str(repo_gamma),
            [],
        )

        # AGY old long session + brand-new empty AGY session in repo_alpha
        agy_old_tp = _write_agy_transcript(
            iso_home / ".gemini" / "antigravity-cli" / "brain" / agy_old_cid / ".system_generated" / "logs" / "transcript_full.jsonl",
            str(repo_alpha),
            [
                ("user", "核对 alpha-service 的 AGY 会话注册表和 presence 锁机制。"),
                ("assistant", f"已核对 AGY transcript_full.jsonl 清洗逻辑。{rich_pad}"),
                ("user", "确认多轮增量续读正常。"),
                ("assistant", f"增量续读与上下文分块均正常。{rich_pad}"),
            ],
        )
        agy_new_tp = _write_agy_transcript(
            iso_home / ".gemini" / "antigravity-cli" / "brain" / agy_new_alpha_cid / ".system_generated" / "logs" / "transcript_full.jsonl",
            str(repo_alpha),
            [],
        )
        agy_presence_dir = iso_home / ".gemini" / "antigravity-cli" / "presence"
        agy_presence_dir.mkdir(parents=True, exist_ok=True)
        agy_old_lock = agy_presence_dir / f"{agy_old_cid}.lock"
        agy_new_lock = agy_presence_dir / f"{agy_new_alpha_cid}.lock"

        # ---- Build Desktop DB fixtures (WAL mode) ----
        # 1) Antigravity 2.0 standalone desktop app
        agy_desk_cid = "dddd4444-5555-4666-8777-888888888801"
        agy_desk_title = "Implement Dual Platform Regression Matrix Pipeline"
        _write_agy_transcript(
            iso_home / ".gemini" / "antigravity" / "brain" / agy_desk_cid / ".system_generated" / "logs" / "transcript_full.jsonl",
            str(repo_beta),
            [
                ("user", "请在 beta-studio 仓库里实现双端回归矩阵管线并核对 canvas_panel.ts。"),
                ("assistant", f"已在 beta-studio 中确认 canvas_panel.ts 与 WAL 路由契约。{rich_pad}"),
            ],
        )
        _create_wal_sqlite(
            iso_home / ".gemini" / "antigravity" / "conversation_summaries.db",
            [
                (
                    """CREATE TABLE conversation_summaries (
                        conversation_id TEXT PRIMARY KEY,
                        title TEXT,
                        workspace_uris TEXT,
                        last_modified_time TEXT,
                        source TEXT,
                        parent_conversation_id TEXT,
                        nesting_depth INTEGER,
                        agent_name TEXT
                    )""",
                    (),
                ),
                (
                    """INSERT INTO conversation_summaries
                       (conversation_id, title, workspace_uris, last_modified_time, source, parent_conversation_id, nesting_depth, agent_name)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        agy_desk_cid,
                        agy_desk_title,
                        json.dumps([repo_beta.as_uri()]),
                        "2026-11-23T15:00:00Z",
                        "CORTEX_TRAJECTORY_SOURCE_CASCADE",
                        "",
                        0,
                        "",
                    ),
                ),
            ],
        )

        # 2) ChatGPT / Codex unified desktop app (local repo promotion vs. cloud veto)
        codex_desk_tid = "eeee5555-6666-4777-8888-999999999901"
        codex_desk_rollout = _write_codex_rollout(
            iso_home / ".codex" / "sessions" / "2026" / "11" / "23" / f"rollout-2026-11-23T11-00-00-{codex_desk_tid}.jsonl",
            codex_desk_tid,
            str(repo_gamma),
            [
                ("user", "重构 gamma-codex 仓库的 WorkerPool 调度队列。"),
                ("assistant", f"已完成 gamma-codex WorkerPool 重构方案与单测要点。{rich_pad}"),
                ("user", "确认并发取消边界和释放顺序。"),
                ("assistant", f"已核对取消令牌与锁释放顺序。{rich_pad}"),
            ],
            source="desktop",
        )
        _create_wal_sqlite(
            iso_home / ".codex" / "sqlite" / "codex.db",
            [
                (
                    """CREATE TABLE local_thread_catalog (
                        thread_id TEXT PRIMARY KEY,
                        display_title TEXT,
                        source_kind TEXT,
                        host_id TEXT,
                        cwd TEXT,
                        source_recency_at TEXT,
                        updated_at TEXT,
                        missing_candidate INTEGER
                    )""",
                    (),
                ),
                (
                    """INSERT INTO local_thread_catalog
                       (thread_id, display_title, source_kind, host_id, cwd, source_recency_at, updated_at, missing_candidate)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        codex_desk_tid,
                        "Refactor Queue Scheduler",
                        "codex",
                        "local",
                        str(repo_gamma),
                        "2026-11-23T15:10:00Z",
                        "2026-11-23T15:10:00Z",
                        0,
                    ),
                ),
                (
                    """INSERT INTO local_thread_catalog
                       (thread_id, display_title, source_kind, host_id, cwd, source_recency_at, updated_at, missing_candidate)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        "ffff6666-7777-4888-8999-000000000001",
                        "Weekend Travel Itinerary",
                        "chatgpt",
                        "cloud",
                        "",
                        "2026-11-23T15:12:00Z",
                        "2026-11-23T15:12:00Z",
                        0,
                    ),
                ),
            ],
        )
        _create_wal_sqlite(
            iso_home / ".codex" / "state_5.sqlite",
            [
                (
                    """CREATE TABLE threads (
                        id TEXT PRIMARY KEY,
                        name TEXT,
                        cwd TEXT,
                        originator TEXT,
                        rollout_path TEXT,
                        archived INTEGER,
                        updated_at INTEGER
                    )""",
                    (),
                ),
                (
                    """INSERT INTO threads (id, name, cwd, originator, rollout_path, archived, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        codex_desk_tid,
                        "Refactor Queue Scheduler",
                        str(repo_gamma),
                        "Codex Desktop",
                        str(codex_desk_rollout),
                        0,
                        1795446600,
                    ),
                ),
            ],
        )

        # ---- Claude Desktop Code & Cowork fixtures (Claude + Claude-3p dual roots) ----
        claude_desk_code_sid = "1111aaaa-2222-4333-8444-555566667777"
        claude_desk_cowork_sid = "2222bbbb-3333-4444-8555-666677778888"
        claude_desk_cowork_local_id = "local_abcdef12-3456-4789-8abc-def012345678"
        claude_desk_code_tp = (
            iso_home
            / ".claude"
            / "projects"
            / encode_claude_project_dir(str(repo_alpha), wsl=False)[0]
            / f"{claude_desk_code_sid}.jsonl"
        )
        _write_claude_jsonl(
            claude_desk_code_tp,
            claude_desk_code_sid,
            str(repo_alpha),
            [
                ("user", "Inspect DesktopContextRouter and ClaudeCodeSessionBinder."),
                ("assistant", "Checked DesktopContextRouter and verified ClaudeCodeSessionBinder invariants."),
            ],
        )
        claude_3p_code_meta = (
            iso_home
            / "Library"
            / "Application Support"
            / "Claude-3p"
            / "claude-code-sessions"
            / "org-demo"
            / "acct-demo"
            / "local_1111aaaa-2222-4333-8444-555566667777.json"
        )
        claude_3p_code_meta.parent.mkdir(parents=True, exist_ok=True)
        claude_3p_code_meta.write_text(
            json.dumps(
                {
                    "sessionId": "local_1111aaaa-2222-4333-8444-555566667777",
                    "cliSessionId": claude_desk_code_sid,
                    "title": "Harden Desktop Router",
                    "cwd": str(repo_alpha),
                    "isArchived": False,
                    "lastFocusedAt": 1795446700000,
                    "lastActivityAt": 1795446700000,
                }
            ),
            encoding="utf-8",
        )

        claude_cowork_acct = (
            iso_home
            / "Library"
            / "Application Support"
            / "Claude"
            / "local-agent-mode-sessions"
            / "org-demo"
            / "acct-demo"
        )
        claude_cowork_acct.mkdir(parents=True, exist_ok=True)
        (claude_cowork_acct / f"{claude_desk_cowork_local_id}.json").write_text(
            json.dumps(
                {
                    "sessionId": claude_desk_cowork_local_id,
                    "cliSessionId": claude_desk_cowork_sid,
                    "title": "Quarterly Launch Brief",
                    "cwd": str(claude_cowork_acct / "abcdef12" / "outputs"),
                    "userSelectedFolders": [],
                    "isArchived": False,
                    "lastActivityAt": 1795446800000,
                }
            ),
            encoding="utf-8",
        )
        claude_cowork_tp = (
            claude_cowork_acct
            / "abcdef12"
            / ".claude"
            / "projects"
            / "session"
            / f"{claude_desk_cowork_sid}.jsonl"
        )
        claude_cowork_tp.parent.mkdir(parents=True, exist_ok=True)
        claude_cowork_tp.write_text(
            "\n".join(
                [
                    json.dumps({"type": "system", "cwd": "/private/var/empty", "sessionId": claude_desk_cowork_sid}),
                    json.dumps(
                        {
                            "type": "user",
                            "isSidechain": False,
                            "sessionId": claude_desk_cowork_sid,
                            "message": {"role": "user", "content": [{"type": "text", "text": "Draft Q4 LaunchChecklist milestones."}]},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "assistant",
                            "isSidechain": False,
                            "sessionId": claude_desk_cowork_sid,
                            "message": {"role": "assistant", "content": [{"type": "text", "text": "Outlined Q4 LaunchChecklist owners."}]},
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        # ---- Run real `bin/agent-voice-input session-hook` for Claude & AGY ----
        def _run_claude_hook(sid: str, tp: Path, cwd: Path) -> tuple[int, str]:
            proc = subprocess.run(
                [sys.executable, str(bin_avi), "session-hook", "--cli", "claude", "--quiet"],
                input=json.dumps({"session_id": sid, "transcript_path": str(tp), "cwd": str(cwd)}),
                text=True,
                capture_output=True,
                env=iso_env,
                start_new_session=True,
                timeout=15,
                check=False,
            )
            return proc.returncode, redact(proc.stderr[:200])

        rc_cl_old, err_cl_old = _run_claude_hook(claude_old_sid, claude_old_path, repo_alpha)
        rc_cl_new, err_cl_new = _run_claude_hook(claude_new_gamma_sid, claude_new_gamma_path, repo_gamma)

        agy_helper_script = td / "agy"
        agy_helper_script.write_text(_AGY_HELPER_CODE, encoding="utf-8")
        agy_proc: subprocess.Popen[str] | None = None
        daemon: VoiceInputDaemon | None = None
        srv_th: threading.Thread | None = None
        try:
            agy_proc = subprocess.Popen(
                [sys.executable, str(agy_helper_script), str(agy_old_lock), str(agy_new_lock)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=iso_env,
                start_new_session=True,
            )
            assert agy_proc.stdout is not None and agy_proc.stdin is not None
            ready_line = agy_proc.stdout.readline().strip()
            if ready_line != "READY":
                stderr_tail = redact((agy_proc.stderr.read() if agy_proc.stderr else "")[:200])
                raise RuntimeError(f"agy helper failed to start: ready={ready_line!r} err={stderr_tail!r}")

            def _run_agy_hook(cid: str, tp: Path, cwd: Path) -> tuple[int, str]:
                assert agy_proc is not None and agy_proc.stdin is not None and agy_proc.stdout is not None
                req = {
                    "op": "run_hook",
                    "argv": [sys.executable, str(bin_avi), "session-hook", "--cli", "agy", "--quiet"],
                    "payload": {"conversationId": cid, "transcriptPath": str(tp), "cwd": str(cwd)},
                    "env": iso_env,
                }
                agy_proc.stdin.write(json.dumps(req) + "\n")
                agy_proc.stdin.flush()
                raw_resp = agy_proc.stdout.readline().strip()
                parsed = json.loads(raw_resp) if raw_resp else {}
                return int(parsed.get("rc", -1)), redact(str(parsed.get("stderr") or ""))

            rc_agy_old, err_agy_old = _run_agy_hook(agy_old_cid, agy_old_tp, repo_alpha)
            rc_agy_new, err_agy_new = _run_agy_hook(agy_new_alpha_cid, agy_new_tp, repo_alpha)

            hooks_ok = (
                rc_cl_old == 0
                and rc_cl_new == 0
                and rc_agy_old == 0
                and rc_agy_new == 0
                and (rt_dir / "sessions" / f"claude-{claude_old_sid[:8]}.json").is_file()
                and (rt_dir / "sessions" / f"claude-{claude_new_gamma_sid[:8]}.json").is_file()
                and (rt_dir / "sessions" / f"agy-{agy_old_cid[:8]}.json").is_file()
                and (rt_dir / "sessions" / f"agy-{agy_new_alpha_cid[:8]}.json").is_file()
            )
            checks.append(
                _make_check(
                    "A0_session_hooks_and_prefilter",
                    "A",
                    "PASS" if (hooks_ok and prefilter_ok) else "FAIL",
                    (
                        "real session-hook (--cli claude|agy --quiet) wrote 4 registry entries; "
                        f"2.1.330 giant attachment ({len(giant_att_bytes)}B) prefiltered={prefilter_ok}"
                    )
                    if (hooks_ok and prefilter_ok)
                    else redact(
                        f"hooks_ok={hooks_ok} prefilter_ok={prefilter_ok} "
                        f"rcs=({rc_cl_old},{rc_cl_new},{rc_agy_old},{rc_agy_new}) "
                        f"errs=({err_cl_old!r},{err_cl_new!r},{err_agy_old!r},{err_agy_new!r})"
                    ),
                )
            )

            # ---- Start real headless VoiceInputDaemon ----
            try:
                cfg = Config(refiner=RefinerConfig(provider="off", cold_start_fallback="auto"))
                tracker = SessionTracker(
                    home_dir=iso_home,
                    runtime_dir=rt_dir,
                    proc_root=no_proc,
                    env=iso_env,
                    tmux_probe_fn=lambda: TmuxServerSnapshot(),
                    cold_start_fallback="auto",
                )
                daemon = VoiceInputDaemon(
                    cfg,
                    env=iso_env,
                    runtime_dir=rt_dir,
                    platform=_HeadlessMatrixPlatform(),
                    asr_provider=_DummyAsr(),
                    refiner=_DummyRefiner(),
                    tracker=tracker,
                )
                srv_th = threading.Thread(target=daemon.serve_forever, name="avi-regress-daemon", daemon=True)
                srv_th.start()
                _wait_control_socket(rt_dir, timeout_sec=6.0)
            except Exception as exc:
                checks.append(
                    _make_check(
                        "A0_headless_daemon_startup",
                        "A",
                        "FAIL",
                        f"headless daemon startup failed: {redact(exc)}",
                    )
                )
                return checks

            def _probe(payload: dict[str, Any]) -> dict[str, Any]:
                req = {"cmd": "probe_route", **payload}
                return send_control_command(rt_dir, req, timeout_sec=6.0)

            # ---- A1: Three CLIs old/new sessions & zero crosstalk ----
            r_codex_win = _probe(
                {
                    "process": "WindowsTerminal.exe",
                    "title": f"codex · alpha-service [{codex_old_sid}]",
                }
            )
            checks.append(
                _check_asserts(
                    "A1_codex_old_win",
                    "A",
                    r_codex_win,
                    [
                        ("ok", r_codex_win.get("ok") is True),
                        ("mode==agent", r_codex_win.get("mode") == "agent"),
                        ("key==codex_old", r_codex_win.get("session_key_masked") == f"codex:{codex_old_sid[:8]}"),
                        ("ws==alpha-service", r_codex_win.get("workspace_name") == "alpha-service"),
                        ("ctx_chars>1000", int(r_codex_win.get("ctx_chars") or 0) > 1000),
                        ("no_fallback", r_codex_win.get("ctx_fallback") == ""),
                    ],
                )
            )

            r_codex_mac = _probe(
                {
                    "bundle_id": "com.mitchellh.ghostty",
                    "process": "Ghostty",
                    "title": f"codex · alpha-service [codex:#{codex_old_sid[:8]}]",
                }
            )
            checks.append(
                _check_asserts(
                    "A1_codex_old_mac",
                    "A",
                    r_codex_mac,
                    [
                        ("ok", r_codex_mac.get("ok") is True),
                        ("mode==agent", r_codex_mac.get("mode") == "agent"),
                        ("key==codex_old", r_codex_mac.get("session_key_masked") == f"codex:{codex_old_sid[:8]}"),
                        ("ws==alpha-service", r_codex_mac.get("workspace_name") == "alpha-service"),
                        ("ctx_chars>1000", int(r_codex_mac.get("ctx_chars") or 0) > 1000),
                    ],
                )
            )

            r_claude_old = _probe(
                {
                    "process": "WindowsTerminal.exe",
                    "title": f"claude · alpha-service [#{claude_old_sid[:8]}]",
                }
            )
            checks.append(
                _check_asserts(
                    "A1_claude_old_with_21330_attachment",
                    "A",
                    r_claude_old,
                    [
                        ("ok", r_claude_old.get("ok") is True),
                        ("mode==agent", r_claude_old.get("mode") == "agent"),
                        ("key==claude_old", r_claude_old.get("session_key_masked") == f"claude:{claude_old_sid[:8]}"),
                        ("matched==registry", r_claude_old.get("matched_by") == "registry"),
                        ("ws==alpha-service", r_claude_old.get("workspace_name") == "alpha-service"),
                        ("1000<ctx_chars<50000", 1000 < int(r_claude_old.get("ctx_chars") or 0) < 50000),
                    ],
                )
            )

            r_agy_old = _probe(
                {
                    "bundle_id": "com.mitchellh.ghostty",
                    "process": "Ghostty",
                    "title": f"agy · alpha-service [#{agy_old_cid[:8]}]",
                }
            )
            checks.append(
                _check_asserts(
                    "A1_agy_old_mac",
                    "A",
                    r_agy_old,
                    [
                        ("ok", r_agy_old.get("ok") is True),
                        ("mode==agent", r_agy_old.get("mode") == "agent"),
                        ("key==agy_old", r_agy_old.get("session_key_masked") == f"agy:{agy_old_cid[:8]}"),
                        ("matched==registry", r_agy_old.get("matched_by") == "registry"),
                        ("ws==alpha-service", r_agy_old.get("workspace_name") == "alpha-service"),
                        ("ctx_chars>1000", int(r_agy_old.get("ctx_chars") or 0) > 1000),
                    ],
                )
            )

            # Same-dir new empty Claude session inherits Codex prior_session (cross-CLI!)
            r_cross_prior = _probe(
                {
                    "process": "WindowsTerminal.exe",
                    "title": f"claude · gamma-codex [#{claude_new_gamma_sid[:8]}]",
                }
            )
            checks.append(
                _check_asserts(
                    "A1_new_empty_claude_cross_cli_prior_session",
                    "A",
                    r_cross_prior,
                    [
                        ("ok", r_cross_prior.get("ok") is True),
                        ("mode==agent", r_cross_prior.get("mode") == "agent"),
                        ("key==claude_new", r_cross_prior.get("session_key_masked") == f"claude:{claude_new_gamma_sid[:8]}"),
                        ("ctx_fallback==prior_session", r_cross_prior.get("ctx_fallback") == "prior_session"),
                        ("ws==gamma-codex", r_cross_prior.get("workspace_name") == "gamma-codex"),
                        ("ctx_chars>500", int(r_cross_prior.get("ctx_chars") or 0) > 500),
                    ],
                )
            )

            # Same-dir new empty AGY session triggers prior_session in repo_alpha
            r_agy_prior = _probe(
                {
                    "bundle_id": "com.mitchellh.ghostty",
                    "process": "Ghostty",
                    "title": f"agy · alpha-service [#{agy_new_alpha_cid[:8]}]",
                }
            )
            checks.append(
                _check_asserts(
                    "A1_new_empty_agy_prior_session",
                    "A",
                    r_agy_prior,
                    [
                        ("ok", r_agy_prior.get("ok") is True),
                        ("mode==agent", r_agy_prior.get("mode") == "agent"),
                        ("key==agy_new", r_agy_prior.get("session_key_masked") == f"agy:{agy_new_alpha_cid[:8]}"),
                        ("ctx_fallback==prior_session", r_agy_prior.get("ctx_fallback") == "prior_session"),
                        ("ws==alpha-service", r_agy_prior.get("workspace_name") == "alpha-service"),
                        ("ctx_chars>500", int(r_agy_prior.get("ctx_chars") or 0) > 500),
                    ],
                )
            )

            # Empty shell / unrelated dir zero crosstalk (Windows + macOS)
            for tag, win_req in (
                (
                    "A1_empty_shell_win_zero_crosstalk",
                    {
                        "process": "WindowsTerminal.exe",
                        "title": "dev@localhost: ~/tmp-unrelated",
                        "cwd": str(unrelated_dir),
                    },
                ),
                (
                    "A1_empty_shell_mac_zero_crosstalk",
                    {
                        "bundle_id": "com.mitchellh.ghostty",
                        "process": "Ghostty",
                        "title": "~/tmp-unrelated",
                    },
                ),
            ):
                r_sh = _probe(win_req)
                checks.append(
                    _check_asserts(
                        tag,
                        "A",
                        r_sh,
                        [
                            ("ok", r_sh.get("ok") is True),
                            ("session_key_masked==''", r_sh.get("session_key_masked") == ""),
                            ("ctx_chars==0", int(r_sh.get("ctx_chars") or 0) == 0),
                        ],
                    )
                )

            # ---- A2: Desktop Apps & Remote Anti-Crosstalk Dual-Platform Matrix ----
            # 1) Antigravity 2.0 standalone desktop app
            r_agy2_exact = _probe(
                {
                    "bundle_id": "com.google.antigravity",
                    "process": "Antigravity",
                    "title": agy_desk_title,
                }
            )
            checks.append(
                _check_asserts(
                    "A2_agy2_desktop_exact_mac",
                    "A",
                    r_agy2_exact,
                    [
                        ("ok", r_agy2_exact.get("ok") is True),
                        ("cat==coding_gui", r_agy2_exact.get("window_category") == "coding_gui"),
                        ("mode==agent", r_agy2_exact.get("mode") == "agent"),
                        ("matched==ide_db_title", r_agy2_exact.get("matched_by") == "ide_db_title"),
                        ("ws==beta-studio", r_agy2_exact.get("workspace_name") == "beta-studio"),
                        ("key==agy_desk", r_agy2_exact.get("session_key_masked") == f"agy:{agy_desk_cid[:8]}"),
                        ("ctx_chars>0", int(r_agy2_exact.get("ctx_chars") or 0) > 0),
                    ],
                )
            )

            r_agy2_trunc = _probe(
                {
                    "process": "Antigravity.exe",
                    "title": "Implement Dual Platform Regression...",
                }
            )
            checks.append(
                _check_asserts(
                    "A2_agy2_desktop_trunc_win",
                    "A",
                    r_agy2_trunc,
                    [
                        ("ok", r_agy2_trunc.get("ok") is True),
                        ("cat==coding_gui", r_agy2_trunc.get("window_category") == "coding_gui"),
                        ("mode==agent", r_agy2_trunc.get("mode") == "agent"),
                        ("matched==ide_db_title", r_agy2_trunc.get("matched_by") == "ide_db_title"),
                        ("ws==beta-studio", r_agy2_trunc.get("workspace_name") == "beta-studio"),
                        ("ctx_chars>0", int(r_agy2_trunc.get("ctx_chars") or 0) > 0),
                    ],
                )
            )

            r_agy2_newchat = _probe(
                {
                    "bundle_id": "com.google.antigravity",
                    "process": "Antigravity",
                    "title": "New Chat",
                }
            )
            checks.append(
                _check_asserts(
                    "A2_agy2_desktop_new_chat_veto",
                    "A",
                    r_agy2_newchat,
                    [
                        ("ok", r_agy2_newchat.get("ok") is True),
                        ("key==''", r_agy2_newchat.get("session_key_masked") == ""),
                        ("ws==''", r_agy2_newchat.get("workspace_name") == ""),
                        ("ctx_chars==0", int(r_agy2_newchat.get("ctx_chars") or 0) == 0),
                    ],
                )
            )

            # 2) ChatGPT / Codex unified desktop app (local promo vs cloud veto, macOS & Windows)
            for tag, win_req in (
                (
                    "A2_codex_desktop_local_promo_mac",
                    {
                        "bundle_id": "com.openai.codex",
                        "process": "ChatGPT",
                        "title": "Refactor Queue Scheduler",
                    },
                ),
                (
                    "A2_codex_desktop_local_promo_win",
                    {
                        "process": "ChatGPT.exe",
                        "title": "Refactor Queue Scheduler - ChatGPT",
                    },
                ),
            ):
                r_cd = _probe(win_req)
                checks.append(
                    _check_asserts(
                        tag,
                        "A",
                        r_cd,
                        [
                            ("ok", r_cd.get("ok") is True),
                            ("mode==agent", r_cd.get("mode") == "agent"),
                            ("hotwords_scope==coding", r_cd.get("hotwords_scope") == "coding"),
                            ("matched==codex_desktop_db", r_cd.get("matched_by") == "codex_desktop_db"),
                            ("key==codex_desk", r_cd.get("session_key_masked") == f"codex:{codex_desk_tid[:8]}"),
                            ("ws==gamma-codex", r_cd.get("workspace_name") == "gamma-codex"),
                            ("ctx_chars>0", int(r_cd.get("ctx_chars") or 0) > 0),
                        ],
                    )
                )

            for tag, win_req in (
                (
                    "A2_codex_desktop_cloud_veto_mac",
                    {
                        "bundle_id": "com.openai.codex",
                        "process": "ChatGPT",
                        "title": "Weekend Travel Itinerary",
                    },
                ),
                (
                    "A2_codex_desktop_cloud_veto_win",
                    {
                        "process": "ChatGPT.exe",
                        "title": "Weekend Travel Itinerary - ChatGPT",
                    },
                ),
            ):
                r_cc = _probe(win_req)
                checks.append(
                    _check_asserts(
                        tag,
                        "A",
                        r_cc,
                        [
                            ("ok", r_cc.get("ok") is True),
                            ("mode==general", r_cc.get("mode") == "general"),
                            ("target_app_hint==ai_chat", r_cc.get("target_app_hint") == "ai_chat"),
                            ("hotwords_scope==chat", r_cc.get("hotwords_scope") == "chat"),
                            ("key==''", r_cc.get("session_key_masked") == ""),
                            ("ctx_chars==0", int(r_cc.get("ctx_chars") or 0) == 0),
                        ],
                    )
                )

            # 2b) Claude Desktop (Code + Cowork exact route promo vs same-title Chat / no-route fail-closed)
            r_cl_code = _probe(
                {
                    "bundle_id": "com.anthropic.claudefordesktop",
                    "process": "Claude",
                    "title": "Harden Desktop Router - Claude Code",
                    "page_url": "app://localhost/epitaxy/local_1111aaaa-2222-4333-8444-555566667777",
                }
            )
            checks.append(
                _check_asserts(
                    "A2_claude_desktop_code_promo_mac",
                    "A",
                    r_cl_code,
                    [
                        ("ok", r_cl_code.get("ok") is True),
                        ("mode==agent", r_cl_code.get("mode") == "agent"),
                        ("hotwords_scope==coding", r_cl_code.get("hotwords_scope") == "coding"),
                        ("matched==claude_desktop_code", r_cl_code.get("matched_by") == "claude_desktop_code"),
                        ("key==claude_code", r_cl_code.get("session_key_masked") == f"claude:{claude_desk_code_sid[:8]}"),
                        ("ws==alpha-service", r_cl_code.get("workspace_name") == "alpha-service"),
                        ("ctx_chars>0", int(r_cl_code.get("ctx_chars") or 0) > 0),
                    ],
                )
            )

            r_cl_cowork = _probe(
                {
                    "bundle_id": "com.anthropic.claudefordesktop",
                    "process": "Claude",
                    "title": "Quarterly Launch Brief - Claude",
                    "page_url": f"app://localhost/cowork/{claude_desk_cowork_local_id}",
                }
            )
            checks.append(
                _check_asserts(
                    "A2_claude_desktop_cowork_promo_mac",
                    "A",
                    r_cl_cowork,
                    [
                        ("ok", r_cl_cowork.get("ok") is True),
                        ("mode==agent", r_cl_cowork.get("mode") == "agent"),
                        ("matched==claude_desktop_cowork", r_cl_cowork.get("matched_by") == "claude_desktop_cowork"),
                        ("key==claude_cowork", r_cl_cowork.get("session_key_masked") == f"claude:{claude_desk_cowork_sid[:8]}"),
                        ("ws==''", r_cl_cowork.get("workspace_name") == ""),
                        ("ctx_fallback==''", r_cl_cowork.get("ctx_fallback") == ""),
                        ("ctx_chars>0", int(r_cl_cowork.get("ctx_chars") or 0) > 0),
                    ],
                )
            )

            # Same title as Cowork ("Quarterly Launch Brief - Claude") on cloud Chat / blank route / no URL MUST stay general+ai_chat
            for tag, chat_page_url in (
                ("A2_claude_desktop_same_title_chat_route_veto", "app://localhost/chat/99998888"),
                ("A2_claude_desktop_same_title_no_route_veto", ""),
            ):
                r_cl_chat_same = _probe(
                    {
                        "bundle_id": "com.anthropic.claudefordesktop",
                        "process": "Claude",
                        "title": "Quarterly Launch Brief - Claude",
                        "page_url": chat_page_url,
                    }
                )
                checks.append(
                    _check_asserts(
                        tag,
                        "A",
                        r_cl_chat_same,
                        [
                            ("ok", r_cl_chat_same.get("ok") is True),
                            ("mode==general", r_cl_chat_same.get("mode") == "general"),
                            ("target_app_hint==ai_chat", r_cl_chat_same.get("target_app_hint") == "ai_chat"),
                            ("hotwords_scope==chat", r_cl_chat_same.get("hotwords_scope") == "chat"),
                            ("key==''", r_cl_chat_same.get("session_key_masked") == ""),
                            ("ctx_chars==0", int(r_cl_chat_same.get("ctx_chars") or 0) == 0),
                        ],
                    )
                )

            # 3) Claude desktop / WeChat IM / Browser (general_gui, zero crosstalk)
            for tag, win_req, exp_hint in (
                (
                    "A2_claude_desktop_mac",
                    {"bundle_id": "com.anthropic.claudefordesktop", "process": "Claude", "title": "Claude"},
                    "ai_chat",
                ),
                (
                    "A2_claude_desktop_win",
                    {"process": "Claude.exe", "title": "Design Review - Claude"},
                    "ai_chat",
                ),
                (
                    "A2_wechat_im_mac",
                    {"bundle_id": "com.tencent.xinWeChat", "process": "WeChat", "title": "WeChat"},
                    "im_chat",
                ),
                (
                    "A2_wechat_im_win",
                    {"process": "WeChat.exe", "title": "微信"},
                    "im_chat",
                ),
                (
                    "A2_browser_mac",
                    {"bundle_id": "com.google.Chrome", "process": "Google Chrome", "title": "Docs - Google Chrome"},
                    "",
                ),
                (
                    "A2_browser_win",
                    {"process": "chrome.exe", "title": "Docs - Google Chrome"},
                    "",
                ),
            ):
                r_gen = _probe(win_req)
                checks.append(
                    _check_asserts(
                        tag,
                        "A",
                        r_gen,
                        [
                            ("ok", r_gen.get("ok") is True),
                            ("cat==general_gui", r_gen.get("window_category") == "general_gui"),
                            ("mode==general", r_gen.get("mode") == "general"),
                            (f"hint=={exp_hint!r}", r_gen.get("target_app_hint") == exp_hint),
                            ("key==''", r_gen.get("session_key_masked") == ""),
                            ("ctx_chars==0", int(r_gen.get("ctx_chars") or 0) == 0),
                        ],
                    )
                )

            # 4) VS Code / Cursor IDE (coding_gui)
            r_vscode_mac = _probe(
                {
                    "bundle_id": "com.microsoft.VSCode",
                    "process": "Code",
                    "title": "router_engine.py — alpha-service",
                    "document": (repo_alpha / "router_engine.py").as_uri(),
                }
            )
            checks.append(
                _check_asserts(
                    "A2_vscode_coding_gui_mac",
                    "A",
                    r_vscode_mac,
                    [
                        ("ok", r_vscode_mac.get("ok") is True),
                        ("cat==coding_gui", r_vscode_mac.get("window_category") == "coding_gui"),
                        ("should_sync==False", r_vscode_mac.get("should_sync") is False),
                        ("mode==agent", r_vscode_mac.get("mode") == "agent"),
                        ("key==''", r_vscode_mac.get("session_key_masked") == ""),
                        ("ws==alpha-service", r_vscode_mac.get("workspace_name") == "alpha-service"),
                    ],
                )
            )

            r_cursor_win = _probe(
                {
                    "process": "Cursor.exe",
                    "title": "canvas_panel.ts - beta-studio - Cursor",
                }
            )
            checks.append(
                _check_asserts(
                    "A2_cursor_coding_gui_win",
                    "A",
                    r_cursor_win,
                    [
                        ("ok", r_cursor_win.get("ok") is True),
                        ("cat==coding_gui", r_cursor_win.get("window_category") == "coding_gui"),
                        ("should_sync==False", r_cursor_win.get("should_sync") is False),
                        ("mode==agent", r_cursor_win.get("mode") == "agent"),
                        ("key==''", r_cursor_win.get("session_key_masked") == ""),
                        ("ws==beta-studio", r_cursor_win.get("workspace_name") == "beta-studio"),
                    ],
                )
            )

            # 5) SSH remote anti-crosstalk (putty.exe, [@remote], [SSH: devbox])
            for tag, win_req in (
                (
                    "A2_remote_veto_putty_win",
                    {"process": "putty.exe", "title": "dev@prod-box: ~/app"},
                ),
                (
                    "A2_remote_veto_at_remote_mac",
                    {
                        "bundle_id": "com.mitchellh.ghostty",
                        "process": "Ghostty",
                        "title": f"codex · alpha-service [#{codex_old_sid[:8]}] [@remote]",
                    },
                ),
                (
                    "A2_remote_veto_at_remote_win",
                    {
                        "process": "WindowsTerminal.exe",
                        "title": f"claude · alpha-service [#{claude_old_sid[:8]}] [@remote]",
                    },
                ),
                (
                    "A2_remote_veto_ssh_ide_mac",
                    {
                        "bundle_id": "com.microsoft.VSCode",
                        "process": "Code",
                        "title": "app.py — alpha-service [SSH: devbox]",
                    },
                ),
                (
                    "A2_remote_veto_ssh_ide_win",
                    {
                        "process": "Code.exe",
                        "title": "app.py - alpha-service [SSH: devbox] - Visual Studio Code",
                    },
                ),
            ):
                r_rem = _probe(win_req)
                checks.append(
                    _check_asserts(
                        tag,
                        "A",
                        r_rem,
                        [
                            ("ok", r_rem.get("ok") is True),
                            ("remote_vetoed==True", r_rem.get("remote_vetoed") is True),
                            ("key==''", r_rem.get("session_key_masked") == ""),
                            ("ws==''", r_rem.get("workspace_name") == ""),
                            ("ctx_chars==0", int(r_rem.get("ctx_chars") or 0) == 0),
                        ],
                    )
                )
        except Exception as exc:
            checks.append(
                _make_check(
                    "A0_matrix_runtime_error",
                    "A",
                    "FAIL",
                    f"layer A matrix failed: {redact(exc)}",
                )
            )
        finally:
            if daemon is not None:
                try:
                    daemon.close()
                except Exception:
                    pass
            if srv_th is not None and srv_th.is_alive():
                srv_th.join(timeout=4.0)
            if agy_proc is not None:
                try:
                    if agy_proc.stdin is not None:
                        agy_proc.stdin.write(json.dumps({"op": "quit"}) + "\n")
                        agy_proc.stdin.flush()
                        agy_proc.stdin.close()
                except Exception:
                    pass
                try:
                    agy_proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    agy_proc.kill()
                    agy_proc.wait(timeout=2.0)
                for pipe in (agy_proc.stdout, agy_proc.stderr):
                    if pipe is not None:
                        try:
                            pipe.close()
                        except OSError:
                            pass

    return checks


def _count_telemetry_lines(rt_dir: Path) -> int:
    tp = rt_dir / TELEMETRY_FILENAME
    try:
        if not tp.is_file() or tp.is_symlink():
            return 0
        with open(tp, "rb") as f:
            return sum(1 for ln in f if ln.strip())
    except OSError:
        return 0


def _resolve_live_wav(
    wav_arg: str,
    *,
    cfg: Config,
    clean_env: Mapping[str, str],
) -> tuple[Path, float, bool]:
    if wav_arg.strip():
        p = Path(wav_arg.strip()).expanduser().resolve()
        if not p.is_file():
            raise RuntimeError(f"--wav file not found: {p.name}")
        gate = process_wav(p, cfg.audio)
        if not gate.passed:
            raise RuntimeError(f"--wav failed audio gate ({gate.reason})")
        return p, round(gate.duration_sec, 2), False

    if _e2e_mod is None:
        raise RuntimeError("e2e_system.py unavailable for OS TTS synthesis; pass --wav explicitly")
    cache_home = (clean_env.get("XDG_CACHE_HOME") or "").strip()
    base_cache = (
        expand_user_path(cache_home, env=clean_env)
        if cache_home
        else (expand_user_path("~", env=clean_env) / ".cache")
    )
    cache_dir = ensure_secure_dir(base_cache / "agent-voice-input" / "eval-wavs")
    item = _e2e_mod._prepare_cached_tts_item(
        LIVE_PROBE_TTS_SPEC,
        cache_dir=cache_dir,
        refresh_tts=False,
        env=clean_env,
        audio_cfg=cfg.audio,
    )
    return Path(item["wav_path"]), float(item["duration_sec"]), True


def _extract_foreground_app_from_probe(resp: Mapping[str, Any]) -> tuple[str, str]:
    """Extract (app_name/process, bundle_id) from a live probe_route response when present."""
    actual_app = ""
    for key in ("app_name", "process", "fg_process"):
        val = str(resp.get(key) or "").strip()
        if val:
            actual_app = val
            break
    actual_bundle_id = ""
    for key in ("bundle_id", "fg_bundle_id"):
        val = str(resp.get(key) or "").strip()
        if val:
            actual_bundle_id = val
            break
    return actual_app, actual_bundle_id


def _query_live_foreground_app(cfg: Config, env: Mapping[str, str]) -> tuple[str, str]:
    """Read-only host foreground app fallback when daemon probe_route omits app_name/bundle_id."""
    try:
        if sys.platform == "darwin":
            from agent_voice_input.platform_macos import MacOSPlatform

            app_name, bundle_id, _, _ = MacOSPlatform(cfg, env=env)._query_frontmost_process_and_title()
            return str(app_name or "").strip(), str(bundle_id or "").strip()
        if _effective_is_wsl(env=env):
            from agent_voice_input.platform_wsl import probe_wsl_host

            info = probe_wsl_host(cfg, env)
            return str(info.get("fg_process") or "").strip(), ""
    except Exception:
        pass
    return "", ""


def _match_expected_app(
    expect_app: str,
    *,
    actual_app: str = "",
    actual_bundle_id: str = "",
) -> bool:
    """Case-insensitive exact match against actual bundle_id or app_name/process (with .exe stem)."""
    exp = expect_app.strip().lower()
    if not exp:
        return True
    candidates: set[str] = set()
    bid = actual_bundle_id.strip().lower()
    if bid:
        candidates.add(bid)
    app = actual_app.strip().lower()
    if app:
        candidates.add(app)
        if app.endswith(".exe") and len(app) > 4:
            candidates.add(app[:-4])
    if not candidates:
        return False
    if exp in candidates:
        return True
    if exp.endswith(".exe") and len(exp) > 4 and exp[:-4] in candidates:
        return True
    return False


def run_layer_b_live_daemon(
    cfg: Config,
    raw_env: Mapping[str, str],
    clean_env: Mapping[str, str],
    redact_fn: Callable[[Any], str],
    *,
    runtime_dir_arg: str,
    wav_arg: str,
    allow_drift: bool,
    skip_selftest_wav: bool,
    deliver_live: bool,
    delay_sec: float,
    show_text: bool,
    expect_app: str = "",
) -> list[dict[str, Any]]:
    """Layer B: connect to host running daemon control.sock without restarting or disturbing it."""
    checks: list[dict[str, Any]] = []
    rt_dir = (
        Path(runtime_dir_arg).expanduser()
        if runtime_dir_arg.strip()
        else get_runtime_dir(raw_env, create=False)
    )
    sock_path = rt_dir / "control.sock"
    if not sock_path.exists():
        checks.append(
            _make_check(
                "B1_daemon_status",
                "B",
                "FAIL",
                "running daemon control.sock not found; start daemon or pass --runtime-dir",
            )
        )
        return checks

    # B1: status & code_drift check
    try:
        st = send_control_command(rt_dir, {"cmd": "status"}, timeout_sec=3.0)
    except Exception as exc:
        checks.append(
            _make_check("B1_daemon_status", "B", "FAIL", f"status IPC failed: {redact_fn(exc)}")
        )
        return checks

    drift = describe_daemon_drift(st)
    drifted = bool(drift.get("drifted", False))
    st_ok = bool(st.get("ok"))
    shim_ok = bool(st.get("shim_ok"))
    b1_metrics = {
        "version": str(st.get("version") or ""),
        "state": str(st.get("state") or ""),
        "shim_ok": shim_ok,
        "asr_provider": str(st.get("asr_provider") or ""),
        "refiner_provider": str(st.get("refiner_provider") or ""),
        "code_drift": drifted,
        "running_build_id": str(drift.get("running_build_id") or ""),
        "current_build_id": str(drift.get("current_build_id") or ""),
    }
    if not st_ok or not shim_ok:
        checks.append(
            _make_check(
                "B1_daemon_status",
                "B",
                "FAIL",
                f"ok={st_ok} shim_ok={shim_ok} shim_error={redact_fn(st.get('shim_error') or '')}",
                b1_metrics,
            )
        )
        return checks
    if drifted:
        b1_status = "WARN" if allow_drift else "FAIL"
        note = redact_fn(drift.get("note") or "daemon code_drift detected; restart daemon")
        checks.append(_make_check("B1_daemon_status", "B", b1_status, note, b1_metrics))
    else:
        checks.append(
            _make_check(
                "B1_daemon_status",
                "B",
                "PASS",
                (
                    f"v{b1_metrics['version']} state={b1_metrics['state']} shim_ok=True "
                    f"asr={b1_metrics['asr_provider']} refiner={b1_metrics['refiner_provider']} drift=False"
                ),
                b1_metrics,
            )
        )

    # B2: probe_route (virtual general_gui, virtual [@remote] SSH veto, live window)
    is_mac = sys.platform == "darwin"
    gen_req = (
        {"cmd": "probe_route", "bundle_id": "com.tencent.xinWeChat", "process": "WeChat", "title": "WeChat"}
        if is_mac
        else {"cmd": "probe_route", "process": "WeChat.exe", "title": "微信"}
    )
    rem_req = (
        {
            "cmd": "probe_route",
            "bundle_id": "com.mitchellh.ghostty",
            "process": "Ghostty",
            "title": "codex · proj [#deadbeef] [@remote]",
        }
        if is_mac
        else {
            "cmd": "probe_route",
            "process": "WindowsTerminal.exe",
            "title": "codex · proj [#deadbeef] [@remote]",
        }
    )

    try:
        r_gen = send_control_command(rt_dir, gen_req, timeout_sec=5.0)
        if not r_gen.get("ok") and "unknown cmd" in str(r_gen.get("error") or "").lower():
            checks.append(
                _make_check(
                    "B2_probe_route",
                    "B",
                    "FAIL",
                    "running daemon does not support probe_route yet; restart daemon on latest code",
                )
            )
            return checks
        checks.append(
            _check_asserts(
                "B2_probe_route_general_gui",
                "B",
                r_gen,
                [
                    ("ok", r_gen.get("ok") is True),
                    ("mode==general", r_gen.get("mode") == "general"),
                    ("key==''", r_gen.get("session_key_masked") == ""),
                    ("ctx_chars==0", int(r_gen.get("ctx_chars") or 0) == 0),
                ],
            )
        )
    except Exception as exc:
        checks.append(_make_check("B2_probe_route_general_gui", "B", "FAIL", redact_fn(exc)))

    try:
        r_rem = send_control_command(rt_dir, rem_req, timeout_sec=5.0)
        checks.append(
            _check_asserts(
                "B2_probe_route_remote_veto",
                "B",
                r_rem,
                [
                    ("ok", r_rem.get("ok") is True),
                    ("remote_vetoed==True", r_rem.get("remote_vetoed") is True),
                    ("key==''", r_rem.get("session_key_masked") == ""),
                    ("ctx_chars==0", int(r_rem.get("ctx_chars") or 0) == 0),
                ],
            )
        )
    except Exception as exc:
        checks.append(_make_check("B2_probe_route_remote_veto", "B", "FAIL", redact_fn(exc)))

    # Prepare WAV before optional delay_sec so TTS synthesis finishes before window switch.
    check_name = "B3_deliver_live" if deliver_live else "B3_selftest_wav"
    wav_info: tuple[Path, float, bool] | None = None
    if not (skip_selftest_wav and not deliver_live):
        try:
            wav_info = _resolve_live_wav(wav_arg, cfg=cfg, clean_env=clean_env)
        except Exception as exc:
            checks.append(
                _make_check(
                    check_name,
                    "B",
                    "FAIL",
                    f"WAV preparation failed: {redact_fn(exc)}",
                )
            )
            return checks

    if delay_sec > 0.0:
        time.sleep(delay_sec)

    r_live: dict[str, Any] = {}
    r_live_ok = False
    r_live_err = ""
    try:
        r_live = send_control_command(rt_dir, {"cmd": "probe_route", "use_live_window": True}, timeout_sec=5.0)
        r_live_ok = bool(r_live.get("ok") is True)
        checks.append(
            _check_asserts(
                "B2_probe_route_live_window",
                "B",
                r_live,
                [
                    ("ok", r_live_ok),
                    ("mode_valid", r_live.get("mode") in ("agent", "general")),
                ],
            )
        )
    except Exception as exc:
        r_live_err = redact_fn(exc)
        checks.append(_make_check("B2_probe_route_live_window", "B", "FAIL", r_live_err))

    # B3: selftest_wav (dry-run by default, or opt-in --deliver-live)
    if skip_selftest_wav and not deliver_live:
        checks.append(_make_check("B3_selftest_wav", "B", "SKIP", "skipped via --skip-selftest-wav"))
        return checks

    exp_app_norm = expect_app.strip()
    actual_app = ""
    actual_bundle_id = ""
    if deliver_live and exp_app_norm:
        if not r_live_ok:
            reason = r_live_err or redact_fn(r_live.get("error") or "probe_route returned ok=False")
            checks.append(
                _make_check(
                    check_name,
                    "B",
                    "FAIL",
                    (
                        f"aborted --deliver-live without pasting: live probe_route failed before "
                        f"verifying expected_app={redact_fn(exp_app_norm)!r} ({reason})"
                    ),
                    {
                        "aborted_before_deliver": True,
                        "expected_app": exp_app_norm,
                        "actual_app": "",
                        "actual_bundle_id": "",
                        "pasted": False,
                        "clipboard": False,
                    },
                )
            )
            return checks
        actual_app, actual_bundle_id = _extract_foreground_app_from_probe(r_live)
        if not actual_app and not actual_bundle_id:
            actual_app, actual_bundle_id = _query_live_foreground_app(cfg, clean_env)
        if not _match_expected_app(
            exp_app_norm,
            actual_app=actual_app,
            actual_bundle_id=actual_bundle_id,
        ):
            checks.append(
                _make_check(
                    check_name,
                    "B",
                    "FAIL",
                    redact_fn(
                        f"foreground app mismatch before --deliver-live (aborted without pasting): "
                        f"expected_app={exp_app_norm!r} actual_app={actual_app!r} "
                        f"actual_bundle_id={actual_bundle_id!r}"
                    ),
                    {
                        "aborted_before_deliver": True,
                        "expected_app": exp_app_norm,
                        "actual_app": actual_app,
                        "actual_bundle_id": actual_bundle_id,
                        "pasted": False,
                        "clipboard": False,
                    },
                )
            )
            return checks

    assert wav_info is not None
    wav_path, dur_sec, used_tts = wav_info

    tel_before = _count_telemetry_lines(rt_dir)
    req_st: dict[str, Any] = {
        "cmd": "selftest_wav",
        "wav_path": str(wav_path),
        "no_telemetry": True,
    }
    if deliver_live:
        req_st["use_live_window"] = True
        req_st["deliver"] = True

    try:
        res_st = send_control_command(rt_dir, req_st, timeout_sec=60.0)
    except Exception as exc:
        checks.append(_make_check(check_name, "B", "FAIL", f"selftest_wav IPC failed: {redact_fn(exc)}"))
        return checks
    tel_after = _count_telemetry_lines(rt_dir)
    tel_delta = tel_after - tel_before

    summary = res_st.get("summary") if isinstance(res_st.get("summary"), Mapping) else {}
    routing = res_st.get("routing") if isinstance(res_st.get("routing"), Mapping) else {}
    raw_final_chars = res_st.get("final_chars")
    if raw_final_chars is None:
        raw_final_chars = summary.get("final_chars")
    if raw_final_chars is None and isinstance(res_st.get("final_text"), str):
        raw_final_chars = len(res_st.get("final_text") or "")
    final_chars = int(raw_final_chars or 0)

    b3_metrics: dict[str, Any] = {
        "duration_sec": dur_sec,
        "used_os_tts": used_tts,
        "asr_ms": int(summary.get("asr_ms") or 0),
        "refine_ms": int(summary.get("refine_ms") or 0),
        "total_ms": int(summary.get("total_ms") or 0),
        "reasoning_tokens": summary.get("reasoning_tokens"),
        "cached_input_tokens": summary.get("cached_input_tokens"),
        "asr_chars": int(summary.get("asr_chars") or 0),
        "final_chars": final_chars,
        "telemetry_lines_delta": tel_delta,
        "routing": dict(routing),
    }
    if deliver_live:
        b3_metrics.update(
            {
                "pasted": bool(res_st.get("pasted", False)),
                "clipboard": bool(res_st.get("clipboard", False)),
                "paste_reason": str(res_st.get("paste_reason") or ""),
                "delivery_status": str(res_st.get("delivery_status") or ""),
            }
        )
        if exp_app_norm:
            b3_metrics.update(
                {
                    "expected_app": exp_app_norm,
                    "actual_app": actual_app,
                    "actual_bundle_id": actual_bundle_id,
                }
            )
    if show_text:
        b3_metrics["asr_text"] = redact_fn(res_st.get("asr_text") or "")
        b3_metrics["final_text"] = redact_fn(res_st.get("final_text") or "")

    ok_res = bool(res_st.get("ok"))
    no_tel_ok = tel_delta == 0
    if deliver_live:
        pasted_ok = bool(res_st.get("pasted"))
        delivered_ok = pasted_ok and final_chars > 0
        passed = ok_res and no_tel_ok and delivered_ok
        fail_prefix = ""
        if ok_res and not pasted_ok and bool(res_st.get("clipboard")):
            fail_prefix = (
                f"clipboard-only fallback (not pasted; delivery_status={b3_metrics['delivery_status'] or '-'}, "
                f"paste_reason={b3_metrics['paste_reason'] or '-'}); "
            )
        elif ok_res and not pasted_ok:
            fail_prefix = (
                f"not pasted (delivery_status={b3_metrics['delivery_status'] or '-'}, "
                f"paste_reason={b3_metrics['paste_reason'] or '-'}); "
            )
        elif ok_res and final_chars <= 0:
            fail_prefix = "empty final_chars=0; "
        detail = (
            f"{fail_prefix}ok={ok_res} pasted={b3_metrics['pasted']} clipboard={b3_metrics['clipboard']} "
            f"delivery_status={b3_metrics['delivery_status']} paste_reason={b3_metrics['paste_reason']} "
            f"final_chars={final_chars} asr_ms={b3_metrics['asr_ms']} refine_ms={b3_metrics['refine_ms']} "
            f"rsn_tok={b3_metrics['reasoning_tokens']} mode={routing.get('mode', '')} "
            f"key={routing.get('session_key_masked', '') or '-'} tel_delta={tel_delta}"
        )
    else:
        passed = ok_res and no_tel_ok and final_chars > 0
        detail = (
            f"ok={ok_res} asr_ms={b3_metrics['asr_ms']} refine_ms={b3_metrics['refine_ms']} "
            f"rsn_tok={b3_metrics['reasoning_tokens']} final_chars={final_chars} "
            f"mode={routing.get('mode', '')} key={routing.get('session_key_masked', '') or '-'} "
            f"tel_delta={tel_delta}"
        )
    if not ok_res and res_st.get("error"):
        detail += f" err={redact_fn(res_st.get('error'))}"
    checks.append(_make_check(check_name, "B", "PASS" if passed else "FAIL", detail, b3_metrics))
    return checks


def _find_latest_files(root: Path, suffix: str, *, max_depth: int = 5, name_prefix: str = "") -> list[tuple[float, int, Path]]:
    if not root.is_dir():
        return []
    found: list[tuple[float, int, Path]] = []

    def _walk(d: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            with os.scandir(d) as it:
                for entry in it:
                    name = entry.name
                    if not name:
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if name.startswith(".") and name != ".system_generated":
                                continue
                            _walk(Path(entry.path), depth + 1)
                        elif entry.is_file(follow_symlinks=False):
                            if not name.endswith(suffix):
                                continue
                            if name_prefix and not name.startswith(name_prefix):
                                continue
                            st = entry.stat(follow_symlinks=False)
                            if st.st_size > 0:
                                found.append((st.st_mtime, st.st_size, Path(entry.path)))
                    except OSError:
                        continue
        except OSError:
            return

    _walk(root, 0)
    found.sort(key=lambda item: item[0], reverse=True)
    return found


_CODEX_EMPTY_META_TYPES: frozenset[str] = frozenset(
    {
        "session_meta",
        "turn_context",
        "token_usage_record",
        "world_state",
        "inter_agent_communication_metadata",
    }
)
_CODEX_EMPTY_EVENT_MSG_TYPES: frozenset[str] = frozenset(
    {
        "token_count",
        "agent_reasoning",
        "agent_reasoning_raw_content",
        "turn_started",
        "turn_complete",
        "turn_aborted",
        "task_started",
        "task_complete",
        "context_compacted",
        "thread_rolled_back",
        "thread_settings_applied",
        "session_configured",
        "shutdown_complete",
        "entered_review_mode",
        "exited_review_mode",
        "warning",
        "error",
        "background_event",
    }
)
_CODEX_NON_DIALOGUE_ITEM_TYPES: frozenset[str] = frozenset(
    {
        "commandexecution",
        "contextcompaction",
        "reasoning",
        "filechange",
        "mcptoolcall",
        "websearch",
        "collabtoolcall",
        "imageview",
        "enteredreviewmode",
        "exitedreviewmode",
    }
)
_CODEX_INSTRUCTIONS_CTX_RE = re.compile(
    r"<INSTRUCTIONS>.*?</INSTRUCTIONS>", re.DOTALL | re.IGNORECASE
)
_CLAUDE_EMPTY_META_TYPES: frozenset[str] = frozenset(
    {
        "system",
        "queue-operation",
        "file-history-snapshot",
        "progress",
        "attachment",
        "last-prompt",
        "cost-state",
        "ai-title",
        "atis-latch",
        "summary",
    }
)
_AGY_EMPTY_META_TYPES: frozenset[str] = frozenset(
    {
        "SYSTEM_PROMPT",
        "SYSTEM_INFO",
        "SYSTEM_MESSAGE",
        "USER_SETTINGS_CHANGE",
        "METADATA",
        "metadata",
    }
)


def _is_codex_bootstrap_response_item(payload: dict[str, Any]) -> bool:
    """Return True iff a Codex ``response_item`` payload is pure startup/system instruction metadata."""
    role = payload.get("role")
    if role in ("developer", "system"):
        return True
    if payload.get("type") != "message" or role != "user":
        return False
    content = payload.get("content")
    if not isinstance(content, list) or not content:
        return False
    for blk in content:
        if not isinstance(blk, dict):
            return False
        if str(blk.get("type") or "").lower() not in ("input_text", "output_text", "text"):
            return False
        raw_t = blk.get("text")
        if not isinstance(raw_t, str):
            return False
        s = raw_t.strip()
        if not s or s.startswith("# AGENTS.md instructions"):
            continue
        s = _CODEX_ENV_CTX_RE.sub("", s)
        s = _CODEX_PERM_CTX_RE.sub("", s)
        s = _CODEX_REC_PLUGINS_RE.sub("", s)
        s = _CODEX_INSTRUCTIONS_CTX_RE.sub("", s).strip()
        if not s or s.startswith(("# AGENTS.md instructions", "<recommended_plugins>")):
            continue
        return False
    return True


def _is_known_empty_metadata_transcript(cli_name: str, path: Path) -> bool:
    """Return True iff every non-empty JSONL line is valid JSON of a known non-dialogue metadata type."""
    saw_any = False
    try:
        with open(path, "rb") as f:
            while True:
                raw_line, exceeded = _read_bounded_line(f)
                if not raw_line:
                    break
                if exceeded:
                    return False
                body = raw_line.strip()
                if not body:
                    continue
                saw_any = True
                try:
                    obj = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    return False
                if not isinstance(obj, dict):
                    return False
                typ = obj.get("type")
                if not isinstance(typ, str) or not typ.strip():
                    return False
                if cli_name == "codex":
                    if typ in _CODEX_EMPTY_META_TYPES:
                        continue
                    if typ == "response_item":
                        pl = obj.get("payload")
                        if isinstance(pl, dict) and _is_codex_bootstrap_response_item(pl):
                            continue
                        return False
                    if typ == "event_msg":
                        pl = obj.get("payload")
                        if not isinstance(pl, dict):
                            return False
                        ptyp = str(pl.get("type") or "").strip()
                        if ptyp == "item_completed":
                            item = pl.get("item")
                            if not isinstance(item, dict):
                                return False
                            raw_item_type = item.get("type")
                            item_type = raw_item_type.strip() if isinstance(raw_item_type, str) else ""
                            norm_item_type = item_type.replace("_", "").replace("-", "").lower()
                            has_role = bool(str(item.get("role") or "").strip())
                            if norm_item_type in _CODEX_NON_DIALOGUE_ITEM_TYPES and not has_role:
                                continue
                            return False
                        item = pl.get("item")
                        if isinstance(item, dict) and item and any(
                            k in item for k in ("role", "content", "text", "message")
                        ):
                            return False
                        if ptyp in _CODEX_EMPTY_EVENT_MSG_TYPES:
                            continue
                        return False
                    return False
                if cli_name == "claude":
                    if typ in _CLAUDE_EMPTY_META_TYPES or typ.startswith("api-request"):
                        continue
                    return False
                if cli_name == "agy":
                    src = obj.get("source")
                    if src not in (None, "", "SYSTEM"):
                        return False
                    if typ in _AGY_EMPTY_META_TYPES:
                        continue
                    return False
                return False
    except OSError:
        return False
    return saw_any


def _probe_real_cli_candidates(
    check_id: str,
    cli_name: str,
    candidates: list[tuple[int, Path]],
    *,
    home: Path,
    eff_wsl: bool,
    redact_fn: Callable[[Any], str],
    empty_msg: str,
) -> dict[str, Any]:
    """Evaluate up to 5 latest root session candidates; never let older turns mask newer drift/errors."""
    if not candidates:
        return _make_check(check_id, "C", "SKIP", empty_msg)

    drift_hit: tuple[int, int, int] | None = None  # (candidate_idx, size_bytes, parse_ms)
    first_exc: tuple[int, int, Exception] | None = None  # (candidate_idx, size_bytes, exc)
    older_pass: tuple[int, int, int, int, int] | None = None  # (idx, turns, chars, size, parse_ms)
    max_empty_size = 0
    empty_meta_count = 0
    checked = 0

    for idx, (size, path) in enumerate(candidates[:_LAYER_C_MAX_CANDIDATES], start=1):
        checked = idx
        t0 = time.monotonic()
        try:
            turns, _, _ = _clean_jsonl_file(cli_name, str(path), home=home, wsl=eff_wsl)
            parse_ms = max(0, int(round((time.monotonic() - t0) * 1000)))
        except Exception as exc:
            if first_exc is None:
                first_exc = (idx, size, exc)
            continue
        if len(turns) > 0:
            chars = sum(len(txt) for _, txt in turns)
            if drift_hit is None and first_exc is None:
                return _make_check(
                    check_id,
                    "C",
                    "PASS",
                    f"turns={len(turns)} chars={chars} file_kb={round(size / 1024.0, 1)} parse_ms={parse_ms}",
                    {
                        "turns": len(turns),
                        "chars": chars,
                        "file_kb": round(size / 1024.0, 1),
                        "parse_ms": parse_ms,
                        "candidates_checked": idx,
                    },
                )
            older_pass = (idx, len(turns), chars, size, parse_ms)
            break
        if size >= _LAYER_C_SCHEMA_DRIFT_MIN_BYTES:
            if _is_known_empty_metadata_transcript(cli_name, path):
                empty_meta_count += 1
                max_empty_size = max(max_empty_size, size)
            elif drift_hit is None:
                drift_hit = (idx, size, parse_ms)
        else:
            max_empty_size = max(max_empty_size, size)

    older_note = (
        f"; older candidate#{older_pass[0]} still parsed turns={older_pass[1]} (chars={older_pass[2]})"
        if older_pass is not None
        else ""
    )
    if drift_hit is not None and (first_exc is None or drift_hit[0] <= first_exc[0]):
        d_idx, d_size, d_ms = drift_hit
        file_kb = round(d_size / 1024.0, 1)
        metrics: dict[str, Any] = {
            "turns": 0,
            "chars": 0,
            "file_kb": file_kb,
            "size_bytes": d_size,
            "parse_ms": d_ms,
            "drift_candidate": d_idx,
            "candidates_checked": checked,
        }
        if older_pass is not None:
            metrics["older_ok_candidate"] = older_pass[0]
            metrics["older_ok_turns"] = older_pass[1]
        return _make_check(
            check_id,
            "C",
            "FAIL",
            f"possible upstream schema drift: candidate#{d_idx} file_kb={file_kb} ({d_size}B >= {_LAYER_C_SCHEMA_DRIFT_MIN_BYTES}B) parsed 0 turns (checked={checked}){older_note}",
            metrics,
        )
    if first_exc is not None:
        e_idx, e_size, exc = first_exc
        metrics = {
            "turns": 0,
            "chars": 0,
            "file_kb": round(e_size / 1024.0, 1),
            "size_bytes": e_size,
            "error_candidate": e_idx,
            "candidates_checked": checked,
        }
        if older_pass is not None:
            metrics["older_ok_candidate"] = older_pass[0]
            metrics["older_ok_turns"] = older_pass[1]
        return _make_check(
            check_id,
            "C",
            "FAIL",
            f"parse error on candidate#{e_idx}: {redact_fn(exc)}{older_note}",
            metrics,
        )
    empty_desc = (
        f"all {checked} latest root session(s) are empty/metadata-only sessions (0 turns, {empty_meta_count} metadata-only >= {_LAYER_C_SCHEMA_DRIFT_MIN_BYTES}B)"
        if empty_meta_count > 0
        else f"all {checked} latest root session(s) are small empty sessions (<{_LAYER_C_SCHEMA_DRIFT_MIN_BYTES}B, 0 turns)"
    )
    return _make_check(
        check_id,
        "C",
        "SKIP",
        empty_desc,
        {
            "turns": 0,
            "chars": 0,
            "max_size_bytes": max_empty_size,
            "empty_metadata_sessions": empty_meta_count,
            "candidates_checked": checked,
        },
    )


def run_layer_c_real_clis(raw_env: Mapping[str, str], redact_fn: Callable[[Any], str]) -> list[dict[str, Any]]:
    """Layer C: strictly read-only compatibility probe of local CLI transcripts & desktop DBs."""
    checks: list[dict[str, Any]] = []
    home = expand_user_path("~", env=raw_env)
    eff_wsl = _effective_is_wsl(env=raw_env)

    # 1) Codex CLI latest root sessions (scan top 5, prefer non-empty)
    codex_dir = home / ".codex" / "sessions"
    codex_cands: list[tuple[int, Path]] = []
    if codex_dir.is_dir():
        for _, size, cand in _find_latest_files(codex_dir, ".jsonl", max_depth=5, name_prefix="rollout-"):
            is_cli, sid, _ = _read_codex_root_meta(cand, wsl=eff_wsl)
            if is_cli and sid:
                codex_cands.append((size, cand))
                if len(codex_cands) >= _LAYER_C_MAX_CANDIDATES:
                    break
    checks.append(
        _probe_real_cli_candidates(
            "C1_real_codex_cli",
            "codex",
            codex_cands,
            home=home,
            eff_wsl=eff_wsl,
            redact_fn=redact_fn,
            empty_msg="no local Codex root session found",
        )
    )

    # 2) Claude Code latest root sessions (scan top 5, prefer non-empty)
    claude_dir = home / ".claude" / "projects"
    claude_cands: list[tuple[int, Path]] = []
    if claude_dir.is_dir():
        for _, size, cand in _find_latest_files(claude_dir, ".jsonl", max_depth=3):
            if cand.name.lower().startswith("agent-"):
                continue
            is_root, sid, _ = _read_claude_root_meta(cand, wsl=eff_wsl)
            if is_root and sid:
                claude_cands.append((size, cand))
                if len(claude_cands) >= _LAYER_C_MAX_CANDIDATES:
                    break
    checks.append(
        _probe_real_cli_candidates(
            "C2_real_claude_cli",
            "claude",
            claude_cands,
            home=home,
            eff_wsl=eff_wsl,
            redact_fn=redact_fn,
            empty_msg="no local Claude Code root session found",
        )
    )

    # 3) AGY CLI latest root sessions (scan top 5, prefer non-empty)
    agy_brain = home / ".gemini" / "antigravity-cli" / "brain"
    agy_cands: list[tuple[int, Path]] = []
    if agy_brain.is_dir():
        cands: list[tuple[float, int, Path]] = []
        try:
            with os.scandir(agy_brain) as it:
                for entry in it:
                    if not entry.is_dir(follow_symlinks=False) or entry.name.startswith("."):
                        continue
                    logs_dir = Path(entry.path) / ".system_generated" / "logs"
                    for fname in ("transcript_full.jsonl", "transcript.jsonl"):
                        tp = logs_dir / fname
                        try:
                            st = os.lstat(tp)
                        except OSError:
                            continue
                        if st.st_size > 0 and not _is_agy_subagent(tp):
                            cands.append((st.st_mtime, st.st_size, tp))
                            break
        except OSError:
            pass
        cands.sort(key=lambda item: item[0], reverse=True)
        agy_cands = [(size, tp) for _, size, tp in cands[:_LAYER_C_MAX_CANDIDATES]]
    checks.append(
        _probe_real_cli_candidates(
            "C3_real_agy_cli",
            "agy",
            agy_cands,
            home=home,
            eff_wsl=eff_wsl,
            redact_fn=redact_fn,
            empty_msg="no local AGY CLI root session found",
        )
    )

    # 4) Antigravity 2.0 conversation_summaries.db (mode=ro, validate ALL existing roots from _iter_agy_ide_roots)
    agy_dbs: list[Path] = [
        cand_db
        for root in _iter_agy_ide_roots(home)
        if os.path.lexists(cand_db := root / "conversation_summaries.db")
    ]
    if not agy_dbs:
        checks.append(_make_check("C4_real_agy2_db", "C", "SKIP", "conversation_summaries.db not present"))
    else:
        ok_roots: list[dict[str, Any]] = []
        failed_roots: list[dict[str, Any]] = []
        for agy_db in agy_dbs:
            root_name = agy_db.parent.name
            t0 = time.monotonic()
            conn = _open_readonly_sqlite(agy_db, wsl=eff_wsl)
            if conn is None:
                failed_roots.append(
                    {"db_root": root_name, "error": "failed to open conversation_summaries.db in mode=ro"}
                )
                continue
            try:
                with contextlib.closing(conn):
                    cols = _get_sqlite_table_cols(conn, "conversation_summaries")
                    required = {"conversation_id", "title"}
                    missing = sorted(required - cols)
                    if missing:
                        failed_roots.append(
                            {"db_root": root_name, "error": f"missing required columns: {missing}"}
                        )
                    else:
                        with contextlib.closing(conn.execute("SELECT COUNT(*) FROM conversation_summaries")) as cur:
                            row_count = int((cur.fetchone() or (0,))[0])
                        q_ms = max(0, int(round((time.monotonic() - t0) * 1000)))
                        ok_roots.append(
                            {
                                "db_root": root_name,
                                "rows": row_count,
                                "has_workspace_uris": "workspace_uris" in cols,
                                "column_count": len(cols),
                                "query_ms": q_ms,
                            }
                        )
            except (sqlite3.Error, OSError) as exc:
                failed_roots.append({"db_root": root_name, "error": redact_fn(exc)})

        if failed_roots:
            fail_desc = "; ".join(f"{f['db_root']}: {f['error']}" for f in failed_roots)
            ok_suffix = (
                f" (ok roots: {', '.join(r['db_root'] for r in ok_roots)})"
                if ok_roots
                else ""
            )
            checks.append(
                _make_check(
                    "C4_real_agy2_db",
                    "C",
                    "FAIL",
                    f"{fail_desc}{ok_suffix}",
                    {
                        "roots_checked": len(agy_dbs),
                        "failed_roots": failed_roots,
                        "passed_roots": ok_roots,
                    },
                )
            )
        elif len(ok_roots) == 1:
            r0 = ok_roots[0]
            checks.append(
                _make_check(
                    "C4_real_agy2_db",
                    "C",
                    "PASS",
                    (
                        f"mode=ro ok root={r0['db_root']} rows={r0['rows']} "
                        f"has_workspace_uris={r0['has_workspace_uris']} query_ms={r0['query_ms']}"
                    ),
                    {
                        **r0,
                        "db_roots": [r0["db_root"]],
                        "roots_checked": 1,
                    },
                )
            )
        else:
            total_rows = sum(int(r["rows"]) for r in ok_roots)
            total_ms = sum(int(r["query_ms"]) for r in ok_roots)
            roots_summary = ", ".join(
                f"{r['db_root']}(rows={r['rows']},ws={r['has_workspace_uris']})" for r in ok_roots
            )
            checks.append(
                _make_check(
                    "C4_real_agy2_db",
                    "C",
                    "PASS",
                    f"mode=ro ok roots={len(ok_roots)} [{roots_summary}] query_ms={total_ms}",
                    {
                        "db_root": ",".join(r["db_root"] for r in ok_roots),
                        "db_roots": [r["db_root"] for r in ok_roots],
                        "roots_checked": len(ok_roots),
                        "rows": total_rows,
                        "has_workspace_uris": all(bool(r["has_workspace_uris"]) for r in ok_roots),
                        "query_ms": total_ms,
                        "per_root": ok_roots,
                    },
                )
            )

    # 5) ChatGPT / Codex unified desktop codex.db (mode=ro; fallback to state_5.sqlite when codex.db absent)
    codex_db = home / ".codex" / "sqlite" / "codex.db"
    state_db = home / ".codex" / "state_5.sqlite"
    if os.path.lexists(codex_db):
        t0 = time.monotonic()
        conn = _open_readonly_sqlite(codex_db, wsl=eff_wsl)
        if conn is None:
            checks.append(_make_check("C5_real_codex_desktop_db", "C", "FAIL", "failed to open codex.db in mode=ro"))
        else:
            try:
                with contextlib.closing(conn):
                    cols = _get_sqlite_table_cols(conn, "local_thread_catalog")
                    has_id = "thread_id" in cols or "id" in cols
                    required = {"display_title", "source_kind", "host_id", "cwd"}
                    missing = sorted(required - cols)
                    if not has_id:
                        missing.insert(0, "thread_id|id")
                    if missing:
                        checks.append(
                            _make_check("C5_real_codex_desktop_db", "C", "FAIL", f"missing required columns: {missing}")
                        )
                    else:
                        with contextlib.closing(
                            conn.execute(
                                "SELECT COUNT(*), "
                                "SUM(CASE WHEN LOWER(COALESCE(source_kind, '')) != 'chatgpt' "
                                "AND LOWER(COALESCE(host_id, '')) = 'local' "
                                "AND COALESCE(cwd, '') != '' THEN 1 ELSE 0 END) "
                                "FROM local_thread_catalog"
                            )
                        ) as cur:
                            r = cur.fetchone() or (0, 0)
                            total_rows = int(r[0] or 0)
                            local_rows = int(r[1] or 0)
                        q_ms = max(0, int(round((time.monotonic() - t0) * 1000)))
                        checks.append(
                            _make_check(
                                "C5_real_codex_desktop_db",
                                "C",
                                "PASS",
                                f"mode=ro ok db=codex.db rows={total_rows} local_repo_rows={local_rows} query_ms={q_ms}",
                                {
                                    "db": "codex.db",
                                    "rows": total_rows,
                                    "local_repo_rows": local_rows,
                                    "column_count": len(cols),
                                    "query_ms": q_ms,
                                },
                            )
                        )
            except (sqlite3.Error, OSError) as exc:
                checks.append(_make_check("C5_real_codex_desktop_db", "C", "FAIL", redact_fn(exc)))
    elif os.path.lexists(state_db):
        t0 = time.monotonic()
        conn = _open_readonly_sqlite(state_db, wsl=eff_wsl)
        if conn is None:
            checks.append(
                _make_check("C5_real_codex_desktop_db", "C", "FAIL", "failed to open state_5.sqlite in mode=ro")
            )
        else:
            try:
                with contextlib.closing(conn):
                    cols = _get_sqlite_table_cols(conn, "threads")
                    has_title = "name" in cols or "title" in cols
                    required = {"id", "cwd", "originator", "archived"}
                    missing = sorted(required - cols)
                    if not has_title:
                        missing.insert(0, "name|title")
                    if missing:
                        checks.append(
                            _make_check(
                                "C5_real_codex_desktop_db",
                                "C",
                                "FAIL",
                                f"state_5.sqlite missing required columns: {missing}",
                            )
                        )
                    else:
                        placeholders = ", ".join("?" for _ in _CODEX_DESKTOP_ORIGINATORS)
                        with contextlib.closing(
                            conn.execute(
                                f"SELECT COUNT(*), "
                                f"SUM(CASE WHEN COALESCE(archived, 0) = 0 "
                                f"AND originator IN ({placeholders}) "
                                f"AND COALESCE(cwd, '') != '' THEN 1 ELSE 0 END) "
                                f"FROM threads",
                                _CODEX_DESKTOP_ORIGINATORS,
                            )
                        ) as cur:
                            r = cur.fetchone() or (0, 0)
                            total_rows = int(r[0] or 0)
                            desktop_rows = int(r[1] or 0)
                        q_ms = max(0, int(round((time.monotonic() - t0) * 1000)))
                        checks.append(
                            _make_check(
                                "C5_real_codex_desktop_db",
                                "C",
                                "PASS",
                                f"mode=ro ok db=state_5.sqlite rows={total_rows} local_repo_rows={desktop_rows} query_ms={q_ms}",
                                {
                                    "db": "state_5.sqlite",
                                    "rows": total_rows,
                                    "local_repo_rows": desktop_rows,
                                    "column_count": len(cols),
                                    "query_ms": q_ms,
                                },
                            )
                        )
            except (sqlite3.Error, OSError) as exc:
                checks.append(_make_check("C5_real_codex_desktop_db", "C", "FAIL", redact_fn(exc)))
    else:
        checks.append(_make_check("C5_real_codex_desktop_db", "C", "SKIP", "codex.db and state_5.sqlite not present"))

    return checks


def _render_markdown(report: Mapping[str, Any]) -> str:
    counts = report["counts"]
    lines = [
        f"# agent-voice-input regress_matrix (v{report['version']})",
        "",
        (
            f"- Platform: `{report['platform']}` | Elapsed: `{report['elapsed_ms']} ms` | "
            f"Summary: **PASS={counts['PASS']} WARN={counts['WARN']} SKIP={counts['SKIP']} FAIL={counts['FAIL']}**"
        ),
        "",
        "| Layer | Check | Status | Detail |",
        "| --- | --- | --- | --- |",
    ]
    for item in report["checks"]:
        det = str(item.get("detail") or "").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {item['layer']} | `{item['id']}` | {item['status']} | {det} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    _sanitize_process_env_no_fast()
    t0 = time.monotonic()
    ap = argparse.ArgumentParser(
        description="Dual-platform (macOS & WSL2) routing regression matrix and live daemon check."
    )
    ap.add_argument("--config", default="", help="Optional path to config.toml")
    ap.add_argument("--no-matrix", action="store_true", help="Skip Layer A offline routing matrix")
    ap.add_argument("--live-daemon", action="store_true", help="Run Layer B checks against host daemon control.sock")
    ap.add_argument("--runtime-dir", default="", help="Override runtime_dir for Layer B")
    ap.add_argument("--wav", default="", help="Optional WAV path for Layer B selftest_wav")
    ap.add_argument("--allow-drift", action="store_true", help="Downgrade daemon code_drift from FAIL to WARN")
    ap.add_argument("--skip-selftest-wav", action="store_true", help="Skip Layer B selftest_wav (status + probe_route only)")
    ap.add_argument(
        "--deliver-live",
        action="store_true",
        help=(
            "Opt-in Layer B GUI E2E delivery (selftest_wav with use_live_window=True, deliver=True, "
            "no_telemetry=True; strongly recommended to pair with --expect-app)"
        ),
    )
    ap.add_argument(
        "--expect-app",
        default="",
        help=(
            "Strongly recommended guard for --deliver-live: case-insensitive expected foreground "
            "bundle_id or app_name/process verified after --delay-sec before pasting (aborts on mismatch)"
        ),
    )
    ap.add_argument("--delay-sec", type=float, default=0.0, help="Seconds to wait before --deliver-live (default: 0)")
    ap.add_argument(
        "--show-text",
        action="store_true",
        help="Include redacted test utterance asr_text/final_text in Layer B output (default: omitted)",
    )
    ap.add_argument("--probe-real-clis", action="store_true", help="Run Layer C read-only check on local CLIs & desktop DBs")
    ap.add_argument("--json", action="store_true", help="Emit structured machine-readable JSON")
    args = ap.parse_args(argv)

    if args.skip_selftest_wav and args.deliver_live:
        ap.error("--skip-selftest-wav and --deliver-live are mutually exclusive")
    if not args.live_daemon:
        layer_b_only: list[str] = []
        if args.deliver_live:
            layer_b_only.append("--deliver-live")
        if args.expect_app.strip():
            layer_b_only.append("--expect-app")
        if args.skip_selftest_wav:
            layer_b_only.append("--skip-selftest-wav")
        if args.allow_drift:
            layer_b_only.append("--allow-drift")
        if args.wav:
            layer_b_only.append("--wav")
        if args.show_text:
            layer_b_only.append("--show-text")
        if layer_b_only:
            ap.error(f"{', '.join(layer_b_only)} require --live-daemon")
    if args.expect_app.strip() and not args.deliver_live:
        ap.error("--expect-app requires --deliver-live")

    raw_env: Mapping[str, str] = dict(os.environ)
    clean_env = _strip_fast_env(raw_env)
    redact_fn: Callable[[Any], str] = lambda s: redact_secrets(str(s), clean_env)

    checks: list[dict[str, Any]] = []
    if not args.no_matrix:
        checks.extend(run_layer_a_matrix(clean_env, redact_fn=redact_fn))
    if args.live_daemon:
        cfg: Config | None = None
        try:
            cfg, loaded_env = load_config(config_path=args.config or None)
            raw_env = loaded_env
            clean_env = _strip_fast_env(raw_env)
            creds = resolve_credentials(cfg, clean_env)
            real_secrets = [s for s in creds.all_secret_values() if s and len(s) >= 4]
            redact_fn = lambda s: redact_secrets(str(s), clean_env, cfg=cfg, extra_secrets=real_secrets)
        except Exception as exc:
            checks.append(
                _make_check("B0_config_load", "B", "FAIL", f"config load failed: {redact_fn(exc)}")
            )
        if cfg is not None:
            checks.extend(
                run_layer_b_live_daemon(
                    cfg,
                    raw_env,
                    clean_env,
                    redact_fn,
                    runtime_dir_arg=args.runtime_dir,
                    wav_arg=args.wav,
                    allow_drift=bool(args.allow_drift),
                    skip_selftest_wav=bool(args.skip_selftest_wav),
                    deliver_live=bool(args.deliver_live),
                    delay_sec=max(0.0, float(args.delay_sec)),
                    show_text=bool(args.show_text),
                    expect_app=str(args.expect_app or "").strip(),
                )
            )
    if args.probe_real_clis:
        checks.extend(run_layer_c_real_clis(raw_env, redact_fn))

    counts = {"PASS": 0, "WARN": 0, "SKIP": 0, "FAIL": 0}
    for item in checks:
        st = str(item.get("status") or "FAIL")
        counts[st] = counts.get(st, 0) + 1

    plat_label = (
        "macos"
        if sys.platform == "darwin"
        else ("wsl2" if _effective_is_wsl(env=raw_env) else sys.platform)
    )
    report: dict[str, Any] = {
        "ok": bool(checks) and counts["FAIL"] == 0,
        "version": __version__,
        "platform": plat_label,
        "elapsed_ms": max(0, int(round((time.monotonic() - t0) * 1000))),
        "counts": counts,
        "checks": checks,
    }

    if args.json:
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write(_render_markdown(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
