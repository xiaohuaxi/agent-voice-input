#!/usr/bin/env python3
"""Multi-turn Stage 2 Refiner & end-to-end WAV benchmark on isolated runtime_dir.

Reads a real local root CLI session (agy / claude / codex) read-only—or a synthetic
multi-chunk session—and runs a 6-turn continuous dictation probe suite without
touching the host runtime_dir or ~/.codex, and without printing any session prose.
"""
from __future__ import annotations

import argparse
import dataclasses
import fcntl
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import statistics
import sys
import tempfile
import time
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SRC_ROOT = _REPO_ROOT / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from agent_voice_input import __version__  # noqa: E402
from agent_voice_input.config import (  # noqa: E402
    Config,
    RefinerConfig,
    ensure_secure_dir,
    expand_user_path,
    load_config,
    redact_secrets,
    resolve_credentials,
)
from agent_voice_input.daemon import run_pipeline_once  # noqa: E402
from agent_voice_input.refiner import (  # noqa: E402
    RefinerResult,
    build_refiner_request_blocks,
    create_refiner,
)
from agent_voice_input.session_ctx import (  # noqa: E402
    SessionMatch,
    SessionSnapshot,
    SessionTracker,
    _extract_agy_transcript_cwd,
    _is_agy_transcript_subagent,
    _read_claude_root_meta,
    _read_codex_root_meta,
    format_turns_text,
    write_session_registry_entry,
)

_SAFE_IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{3,28}$")
_HEX_LIKE_RE = re.compile(r"^[0-9a-fA-F-]{8,}$")
_STOP_IDENTS = frozenset(
    {
        "true",
        "false",
        "none",
        "null",
        "self",
        "this",
        "user",
        "assistant",
        "system",
        "main",
        "test",
        "tests",
        "file",
        "path",
        "json",
        "http",
        "https",
        "bash",
        "python",
        "python3",
        "readme.md",
        "agents.md",
        "claude.md",
    }
)


def _anon_session_tag(cli: str, session_id: str) -> str:
    digest = hashlib.sha256(f"{cli}:{session_id}".encode("utf-8")).hexdigest()[:8]
    return f"{cli}#{digest}"


def _detect_cli_for_file(p: Path) -> tuple[str, str, str] | None:
    """Return ``(cli, session_id, cwd)`` if ``p`` is a valid root session transcript."""
    name = p.name.lower()
    norm_str = str(p).replace("\\", "/")
    if name in ("transcript_full.jsonl", "transcript.jsonl") or "/antigravity-cli/brain/" in norm_str:
        if _is_agy_transcript_subagent(p):
            return None
        parts = p.parts
        cid = p.stem
        if "brain" in parts:
            idx = parts.index("brain")
            if idx + 1 < len(parts):
                cid = parts[idx + 1]
        cwd = _extract_agy_transcript_cwd(p)
        return ("agy", cid.lower(), cwd)

    if name.startswith("rollout-") or "/.codex/sessions/" in norm_str:
        is_cli, sid, cwd = _read_codex_root_meta(p)
        if not is_cli or not sid:
            return None
        return ("codex", sid, cwd)

    is_root, sid, cwd = _read_claude_root_meta(p)
    if not is_root or not sid:
        return None
    return ("claude", sid, cwd)


def _discover_candidate_files(home: Path, cli_filter: str) -> list[tuple[float, int, Path]]:
    patterns: list[str] = []
    if cli_filter in ("auto", "agy"):
        patterns.append(str(home / ".gemini" / "antigravity-cli" / "brain" / "*" / ".system_generated" / "logs" / "transcript_full.jsonl"))
        patterns.append(str(home / ".gemini" / "antigravity-cli" / "brain" / "*" / ".system_generated" / "logs" / "transcript.jsonl"))
    if cli_filter in ("auto", "codex"):
        patterns.append(str(home / ".codex" / "sessions" / "**" / "rollout-*.jsonl"))
    if cli_filter in ("auto", "claude"):
        patterns.append(str(home / ".claude" / "projects" / "**" / "*.jsonl"))

    found: list[tuple[float, int, Path]] = []
    seen_real: set[str] = set()
    for pat in patterns:
        for raw_p in glob.glob(pat, recursive=True):
            try:
                st = os.stat(raw_p)
            except OSError:
                continue
            if st.st_size < 4096:
                continue
            real_p = os.path.realpath(raw_p)
            if real_p in seen_real:
                continue
            seen_real.add(real_p)
            found.append((st.st_mtime, st.st_size, Path(real_p)))
    found.sort(key=lambda item: item[0], reverse=True)
    return found


def _build_synthetic_session(rt_dir: Path, tracker: SessionTracker) -> SessionSnapshot:
    """Create an isolated ~145K-char multi-chunk synthetic session inside ``rt_dir``."""
    sid = "00000000-1111-4222-8333-444444444444"
    rollout = rt_dir / f"rollout-synthetic-{sid}.jsonl"
    lines: list[str] = [
        json.dumps({"type": "session_meta", "payload": {"id": sid, "source": "cli", "cwd": ""}})
    ]
    topics = [
        (
            "请检查 src/agent_voice_input/refiner.py 里的 build_refiner_request_blocks 和 CodexRefiner 超时退避逻辑。",
            "已核查 refiner.py：build_refiner_request_blocks 通过 SessionTracker.get_refiner_context 获取 50K 封口分块，MAX_THREADS_PER_APP_SERVER 控制单进程复用上限。\n"
            + ("# synthetic context padding line for deterministic multi-chunk cache evaluation\n" * 340),
        ),
        (
            "再看一下 session_ctx.py 的 chunk_turns_for_refiner 在新增对话时能不能保持前缀字节一致？",
            "chunk_turns_for_refiner 按单向累积切分 50000 字符块，主会话追加新轮次只会增长末尾未封口分块，前序已封口 <session_reference_log> 块字节级保持不变。\n"
            + ("# synthetic reference log filler block for stage2 prefix cache hit measurement\n" * 340),
        ),
        (
            "daemon.py 里的 telemetry.jsonl 权限和原子截尾是怎么实现的？",
            "append_telemetry_record 强制使用 0600 权限并在超过 TELEMETRY_MAX_BYTES (256KB) 时通过同目录临时文件与 os.replace 保留后半段完整 JSON 行。\n"
            + ("# synthetic daemon telemetry verification padding block\n" * 340),
        ),
    ]
    for idx in range(6):
        u_txt, a_txt = topics[idx % len(topics)]
        lines.append(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "item": {"type": "UserMessage", "content": [{"type": "text", "text": f"[Turn {idx + 1}] {u_txt}"}]},
                    },
                },
                ensure_ascii=False,
            )
        )
        lines.append(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "item": {"type": "AgentMessage", "content": [{"type": "text", "text": a_txt}]},
                    },
                },
                ensure_ascii=False,
            )
        )
    rollout.write_text("\n".join(lines) + "\n", encoding="utf-8")
    match = SessionMatch(cli="codex", session_id=sid, jsonl_path=str(rollout), cwd="", matched_by="synthetic")
    snap = tracker.sync_session(match=match)
    assert snap is not None
    return snap


def _select_session(
    *,
    args: argparse.Namespace,
    home: Path,
    rt_dir: Path,
    tracker: SessionTracker,
    include_git_cwd: bool,
) -> tuple[SessionSnapshot, str]:
    if args.synthetic:
        return _build_synthetic_session(rt_dir, tracker), "synthetic"

    if args.session_file:
        p = Path(args.session_file).expanduser().resolve()
        if not p.is_file():
            raise RuntimeError(f"--session-file not found: {p}")
        detected = _detect_cli_for_file(p)
        if detected is None:
            raise RuntimeError(f"--session-file is not a recognized root CLI session transcript: {p.name}")
        cli_k, sid, cwd = detected
        match = SessionMatch(
            cli=cli_k,
            session_id=sid,
            jsonl_path=str(p),
            cwd=cwd if include_git_cwd else "",
            matched_by="explicit_file",
        )
        snap = tracker.sync_session(match=match)
        if snap is None or not snap.clean_turns:
            raise RuntimeError(f"session file produced 0 clean turns: {p.name}")
        return snap, _anon_session_tag(cli_k, sid)

    cands = _discover_candidate_files(home, args.cli)
    # Prefer sizable files (>= 64 KB) first so multi-chunk prefix caching is exercised
    large_cands = [c for c in cands if c[1] >= 64_000][:45]
    small_cands = [c for c in cands if c[1] < 64_000][:15]

    best_fallback: tuple[int, SessionSnapshot, str] | None = None
    for _mtime, _sz, p in (*large_cands, *small_cands):
        detected = _detect_cli_for_file(p)
        if detected is None:
            continue
        cli_k, sid, cwd = detected
        if args.cli != "auto" and cli_k != args.cli:
            continue
        match = SessionMatch(
            cli=cli_k,
            session_id=sid,
            jsonl_path=str(p),
            cwd=cwd if include_git_cwd else "",
            matched_by="auto_scan",
        )
        snap = tracker.sync_session(match=match)
        if snap is None or len(snap.clean_turns) < 2:
            continue
        if args.prefer_compacted and snap.generation <= 0:
            continue
        char_len = len(format_turns_text(snap.clean_turns, max_chars=500_000))
        tag = _anon_session_tag(cli_k, sid)
        if char_len >= 65_000:
            return snap, tag
        if best_fallback is None or char_len > best_fallback[0]:
            best_fallback = (char_len, snap, tag)

    if best_fallback is not None:
        return best_fallback[1], best_fallback[2]
    return _build_synthetic_session(rt_dir, tracker), "synthetic_fallback"


def _is_safe_code_ident(tok: str) -> bool:
    if not _SAFE_IDENT_RE.match(tok):
        return False
    low = tok.lower()
    if low in _STOP_IDENTS or _HEX_LIKE_RE.match(tok):
        return False
    if low.startswith(("sk-", "gsk_", "ghp_", "xox", "ak-", "ai-")):
        return False
    if "/" in tok or "\\" in tok or "@" in tok:
        return False
    # Require structural code characteristics (extension, underscore, hyphen, or mixed case)
    has_ext = any(low.endswith(ext) for ext in (".py", ".ts", ".js", ".rs", ".go", ".swift", ".ps1", ".toml", ".jsonl", ".yaml", ".md"))
    has_sep = ("_" in tok) or ("-" in tok)
    has_camel = any(c.isupper() for c in tok[1:]) and any(c.islower() for c in tok)
    return has_ext or has_sep or has_camel


def _to_spoken_hint(ident: str) -> str:
    """Convert ``refiner.py`` / ``chunk_turns`` / ``SessionTracker`` into realistic ASR-like spoken text."""
    s = ident
    if "." in s:
        stem, ext = s.rsplit(".", 1)
        stem_words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", stem).replace("_", " ").replace("-", " ").lower()
        return f"{stem_words} 点 {ext.lower()}"
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s).replace("_", " ").replace("-", " ").lower()
    return spaced


def _pick_session_identifiers(snap: SessionSnapshot, context_joined: str) -> tuple[tuple[str, str], tuple[str, str]]:
    file_cand: str | None = None
    sym_cand: str | None = None
    for hw in snap.hotwords:
        cand = hw.strip().strip("`'\".,:;()[]{}")
        if "/" in cand:
            cand = cand.rsplit("/", 1)[-1]
        if not _is_safe_code_ident(cand):
            continue
        if cand not in context_joined:
            continue
        # Skip ambiguous underscore/hyphen twins that both exist in the same context
        if "_" in cand and cand.replace("_", "-") in context_joined:
            continue
        if "-" in cand and cand.replace("-", "_") in context_joined:
            continue
        if "." in cand and file_cand is None:
            file_cand = cand
        elif "." not in cand and sym_cand is None:
            sym_cand = cand
        if file_cand and sym_cand:
            break
    id1 = (file_cand, _to_spoken_hint(file_cand)) if file_cand else ("refiner.py", "refiner 点 py")
    id2 = (sym_cand, _to_spoken_hint(sym_cand)) if sym_cand else ("SessionTracker", "session tracker")
    return id1, id2


def _build_probe_suite(
    snap: SessionSnapshot,
    cfg: Config,
    tracker: SessionTracker,
) -> list[dict[str, Any]]:
    ctx_blocks, _ = build_refiner_request_blocks(
        cfg,
        "probe",
        session_key=snap.session_key,
        tracker=tracker,
    )
    ctx_joined = "\n".join(ctx_blocks)
    (id1_exact, id1_spoken), (id2_exact, id2_spoken) = _pick_session_identifiers(snap, ctx_joined)
    req_id1 = [id1_exact] if id1_exact in ctx_joined else []
    req_id2 = [id2_exact] if id2_exact in ctx_joined else []

    return [
        {
            "id": "R1",
            "name": "cold_ident",
            "draft": f"呃帮我看一下 {id1_spoken} 里面的 {id2_spoken} 逻辑有没有漏掉边界情况",
            "expect_idents": [*req_id1, *req_id2],
            "mutate_delta": False,
        },
        {
            "id": "R2",
            "name": "anti_answer",
            "draft": "为什么这里的缓存命中率一直是零啊？是不是哪里漏传了参数？",
            "expect_idents": [],
            "is_question": True,
            "mutate_delta": False,
        },
        {
            "id": "R3",
            "name": "self_correct",
            "draft": "嗯把超时时间先改成三十秒，不对不对改成九十秒，然后跑一遍单测看看",
            "expect_idents": [],
            "is_correction": True,
            "mutate_delta": False,
        },
        {
            "id": "R4",
            "name": "delta_append",
            "draft": f"好，那再核对一下 {id1_spoken} 的改动范围准备收尾吧",
            "expect_idents": req_id1,
            "mutate_delta": True,
        },
        {
            "id": "R5",
            "name": "tech_casing",
            "draft": f"你帮我把 jsonl 解析还有 http 重试那块跟 {id2_spoken} 对齐一下，总结列个清单",
            "expect_idents": [*req_id2, "JSONL", "HTTP"],
            "anti_hijack": True,
            "mutate_delta": False,
        },
        {
            "id": "R6",
            "name": "multi_clause",
            "draft": "先确认一下权限是不是零六零零，如果没问题的话再跑一次全量测试，最后告诉我结果就行",
            "expect_idents": [],
            "expect_clause_groups": [("0600", "零六零零"), ("结果",)],
            "mutate_delta": False,
        },
    ]


def _evaluate_fidelity(probe: dict[str, Any], res: RefinerResult) -> list[str]:
    flags: list[str] = []
    out = res.text.strip()
    draft = str(probe["draft"])
    if res.used_fallback:
        flags.append(f"FALLBACK({res.used_fallback})!")
    if any(
        m in out
        for m in (
            "<asr_draft_to_clean>",
            "</asr_draft_to_clean>",
            "<session_reference_log",
            "<task_reminder>",
            "[Speaker]",
            "[CLI Agent]",
            "```",
        )
    ):
        flags.append("LEAKED_MARKER!")
    if probe.get("is_question"):
        has_q = ("?" in out) or ("？" in out)
        if not has_q or len(out) > int(len(draft) * 2.0) + 25:
            flags.append("ANSWERED_QUESTION?")
    if probe.get("anti_hijack"):
        if len(out) > int(len(draft) * 2.0) + 30 or "\n- " in out or "\n1." in out:
            flags.append("HIJACKED_SUMMARY?")
    dropped_clause = False
    if probe.get("is_correction"):
        if "三十" in out or "30" in out or "不对不对" in out or ("九十" not in out and "90" not in out):
            flags.append("UNRESOLVED_CORRECTION?")
    elif len(out) < int(len(draft) * 0.45):
        dropped_clause = True
    for group in probe.get("expect_clause_groups", ()):
        if not any(tok in out for tok in group):
            dropped_clause = True
            break
    if dropped_clause:
        flags.append("DROPPED_CLAUSE?")
    for target_id in probe.get("expect_idents", ()):
        if target_id not in out:
            flags.append(f"MISSING_IDENT({target_id})?")
    return flags


def _format_md_cell(text: str, *, multiline_arrow: bool = False, max_chars: int = 240) -> str:
    nl_sep = " ⏎ " if multiline_arrow else " "
    cleaned = text.replace("|", "\\|").replace("\r", "").replace("\n", nl_sep).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars].rstrip() + " …[truncated]"


def _fmt_opt_int(val: int | None) -> str:
    return "-" if val is None else str(val)


def _fmt_hit_pct(in_tok: int | None, cached_tok: int | None) -> str:
    if in_tok is None or cached_tok is None or in_tok <= 0:
        return "-"
    return f"{100.0 * cached_tok / in_tok:.1f}%"


def _clone_Config_for_provider(
    base_cfg: Config,
    provider: str,
    *,
    model_override: str,
    effort_override: str,
    max_chars: int,
    include_git: bool,
    args_api: str = "",
) -> Config:
    eff_model = model_override if model_override else (base_cfg.refiner.model if provider == base_cfg.refiner.provider else "")
    eff_url = base_cfg.refiner.base_url if provider == base_cfg.refiner.provider else ""
    eff_key_env = base_cfg.refiner.api_key_env if provider == base_cfg.refiner.provider else ""
    ref_cfg = RefinerConfig(
        provider=provider,
        model=eff_model,
        api=(args_api or (base_cfg.refiner.api if provider == base_cfg.refiner.provider else "auto")),
        base_url=eff_url,
        api_key_env=eff_key_env,
        effort=effort_override or base_cfg.refiner.effort,
        timeout_sec=base_cfg.refiner.timeout_sec,
        max_retries=base_cfg.refiner.max_retries,
        force_on_single_hop=base_cfg.refiner.force_on_single_hop,
        prompt=base_cfg.refiner.prompt,
        include_git_status=include_git,
        context_max_chars=max_chars,
    )
    return Config(
        hotkey=base_cfg.hotkey,
        audio=dataclasses.replace(base_cfg.audio, save_recordings=False),
        ui=base_cfg.ui,
        asr=base_cfg.asr,
        refiner=ref_cfg,
    )


def _run_wav_benchmark(
    *,
    wav_path: Path,
    providers: list[str],
    base_cfg: Config,
    env: dict[str, str],
    home: Path,
    rt_dir: Path,
    snap: SessionSnapshot,
    args: argparse.Namespace,
    redact_fn: Any,
) -> str:
    lines: list[str] = [
        "## End-to-End WAV Benchmark (`run_pipeline_once`)",
        "",
        "| Provider | Model | ASR (ms) | Init (ms) | Refine (ms) | Total (ms) | Ctx (chunks/chars) | In Tok | Cached Tok | Hit % | Fallback |",
        "| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :--- |",
    ]
    for prov in providers:
        p_cfg = _clone_Config_for_provider(
            base_cfg,
            prov,
            model_override=args.model,
            effort_override=args.effort,
            max_chars=args.max_chars,
            include_git=not args.no_git_status,
            args_api=args.api,
        )
        creds = resolve_credentials(p_cfg, env)
        prov_rt = ensure_secure_dir(rt_dir / f"wav-{prov}")
        wav_home = home
        lock_fd = -1
        try:
            if snap.match.cli == "agy":
                # Allow offline/closed agy sessions to pass registry presence validation in isolation
                wav_home = ensure_secure_dir(prov_rt / "agy-home")
                pres_dir = ensure_secure_dir(wav_home / ".gemini" / "antigravity-cli" / "presence")
                lock_file = pres_dir / f"{snap.match.session_id.strip().lower()}.lock"
                lock_fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            write_session_registry_entry(
                prov_rt,
                cli=snap.match.cli,
                session_id=snap.match.session_id,
                jsonl_path=snap.match.jsonl_path,
                cwd=snap.match.cwd,
            )
            w_tracker = SessionTracker(
                home_dir=wav_home,
                env=env,
                runtime_dir=prov_rt,
                proc_root=rt_dir / "no_proc",
            )
            ref = create_refiner(p_cfg, creds, env=env, runtime_dir=prov_rt)
            try:
                res = run_pipeline_once(
                    wav_path,
                    cfg=p_cfg,
                    creds=creds,
                    tracker=w_tracker,
                    refiner=ref,
                    origin_window=None,
                    window_title_override=f"[{snap.match.cli}:#{snap.match.session_id}]",
                    audio_source="selftest_wav",
                    dry_run=True,
                    runtime_dir=prov_rt,
                    env=env,
                )
                r = res.refined
                in_t = r.input_tokens if r else None
                c_t = r.cached_input_tokens if r else None
                lines.append(
                    f"| `{prov}` | `{r.model if r else '-'}` | {res.asr.latency_ms if res.asr else 0} | "
                    f"{res.refine_init_ms} | {r.latency_ms if r else 0} | {res.total_ms} | "
                    f"{res.ctx_chunks} / {res.ctx_chars} | {_fmt_opt_int(in_t)} | {_fmt_opt_int(c_t)} | "
                    f"{_fmt_hit_pct(in_t, c_t)} | `{redact_fn(r.used_fallback or '-') if r else res.skipped_reason}` |"
                )
            finally:
                ref.close()
        finally:
            if lock_fd >= 0:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                try:
                    os.close(lock_fd)
                except OSError:
                    pass
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Isolated multi-turn Refiner benchmark on real local root CLI sessions."
    )
    ap.add_argument("--config", help="Optional path to config.toml")
    ap.add_argument("--providers", default="", help="Comma-separated refiner providers (default: configured provider)")
    ap.add_argument("--model", default="", help="Override refiner model (single-provider run)")
    ap.add_argument("--api", default="", help="Override refiner API mode (default: configured api)")
    ap.add_argument("--effort", default="", help="Override refiner effort (default: configured effort)")
    ap.add_argument("--cli", choices=("auto", "agy", "claude", "codex"), default="auto", help="Preferred local session CLI")
    ap.add_argument("--session-file", default="", help="Explicit path to a local session JSONL file")
    ap.add_argument("--synthetic", action="store_true", help="Use deterministic synthetic ~145K multi-chunk session")
    ap.add_argument("--prefer-compacted", action="store_true", help="Prefer sessions with compaction boundaries")
    ap.add_argument("--max-chars", type=int, default=0, help="Override refiner.context_max_chars (0 = use config default)")
    ap.add_argument("--no-git-status", action="store_true", help="Omit workspace git status block during benchmark")
    ap.add_argument("--prewarm-ms", type=int, default=0, help="Optional prepare_session lead time before R1 (ms)")
    ap.add_argument(
        "--rounds",
        "--turns",
        dest="rounds",
        type=int,
        default=6,
        help="Number of continuous probe rounds (1..6, default: 6)",
    )
    ap.add_argument("--wav", default="", help="Optional WAV file to also run end-to-end Stage 1 + Stage 2 benchmark")
    ap.add_argument("--out", default="", help="Optional path to save the redacted Markdown report")
    args = ap.parse_args(argv)

    base_cfg, raw_env = load_config(config_path=args.config or None)
    max_chars = args.max_chars if args.max_chars > 0 else int(base_cfg.refiner.context_max_chars)
    args.max_chars = max_chars
    providers = [
        p.strip().lower()
        for p in (args.providers.split(",") if args.providers.strip() else [base_cfg.refiner.provider])
        if p.strip()
    ]
    if not providers:
        providers = ["codex"]
    if len(providers) > 1 and args.model:
        ap.error("--model only supports a single provider")

    with tempfile.TemporaryDirectory(prefix="avi-bench-") as td:
        rt_dir = ensure_secure_dir(Path(td) / "rt")
        env = dict(raw_env)
        env["AGENT_VOICE_INPUT_RUNTIME_DIR"] = str(rt_dir)
        env["XDG_CACHE_HOME"] = str(ensure_secure_dir(Path(td) / "cache"))
        # Never allow any Fast-mode env override in benchmark subprocesses
        for k in list(env.keys()):
            if "FAST" in k.upper() and "CODEX" in k.upper():
                env.pop(k, None)

        all_creds = resolve_credentials(base_cfg, env)
        secrets = all_creds.all_secret_values()
        redact_fn = lambda s: redact_secrets(str(s), env, cfg=base_cfg, extra_secrets=secrets)

        home = expand_user_path("~", env=env)
        base_tracker = SessionTracker(home_dir=home, env=env, runtime_dir=rt_dir, proc_root=rt_dir / "no_proc")
        snap, session_tag = _select_session(
            args=args,
            home=home,
            rt_dir=rt_dir,
            tracker=base_tracker,
            include_git_cwd=not args.no_git_status,
        )
        base_turns = list(snap.clean_turns)
        raw_dialog_chars = len(format_turns_text(base_turns, max_chars=max(500_000, max_chars * 2)))

        probe_cfg = _clone_Config_for_provider(
            base_cfg,
            providers[0],
            model_override=args.model,
            effort_override=args.effort,
            max_chars=max_chars,
            include_git=not args.no_git_status,
            args_api=args.api,
        )
        probes = _build_probe_suite(snap, probe_cfg, base_tracker)[: max(1, min(6, int(args.rounds)))]
        init_blocks, _ = build_refiner_request_blocks(
            probe_cfg,
            "probe",
            session_key=snap.session_key,
            tracker=base_tracker,
        )

        ts_iso = time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime())
        report_parts: list[str] = [
            f"# `agent-voice-input` Refiner Benchmark Report (`v{__version__}`)",
            "",
            f"- Timestamp: `{ts_iso}`",
            f"- Session Sample: `{session_tag}` (turns=`{len(base_turns)}`, generation=`{snap.generation}`, raw_dialog_chars=`{raw_dialog_chars}`, initial_ctx=`{len(init_blocks)} chunks / {sum(len(b) for b in init_blocks)} chars`, max_chars=`{max_chars}`)",
            f"- Isolated Runtime Dir: `<temp>/avi-bench-*` (auto-cleaned, host runtime untouched)",
            f"- Providers: `{', '.join(providers)}` | Prewarm Lead: `{args.prewarm_ms}ms`",
            "",
        ]

        summary_rows: list[str] = []
        sample_io_sections: list[str] = []

        for prov in providers:
            p_cfg = _clone_Config_for_provider(
                base_cfg,
                prov,
                model_override=args.model,
                effort_override=args.effort,
                max_chars=max_chars,
                include_git=not args.no_git_status,
                args_api=args.api,
            )
            creds = resolve_credentials(p_cfg, env)
            prov_rt = ensure_secure_dir(rt_dir / f"prov-{prov}")
            # Fresh tracker copy per provider so R4 in-memory delta mutation starts identically
            p_tracker = SessionTracker(home_dir=home, env=env, runtime_dir=prov_rt, proc_root=rt_dir / "no_proc")
            p_snap = p_tracker.sync_session(match=snap.match)
            assert p_snap is not None

            ref = create_refiner(p_cfg, creds, env=env, runtime_dir=prov_rt)
            eff_model = p_cfg.refiner.effective_model(prov) if prov != "off" else "-"
            round_rows: list[str] = []
            io_rows: list[str] = []
            warm_latencies: list[int] = []
            stationary_hits: list[float] = []
            delta_hit: float | None = None
            cold_init_ms = 0
            cold_ref_ms = 0
            cold_hit_str = "-"
            total_rsn = 0
            rsn_seen = False
            pass_count = 0

            try:
                if args.prewarm_ms > 0:
                    ref.prepare_session(p_snap.session_key)
                    time.sleep(args.prewarm_ms / 1000.0)

                for idx, probe in enumerate(probes):
                    if probe.get("mutate_delta"):
                        st_obj = p_tracker.get_state(p_snap.session_key)
                        if st_obj is not None:
                            st_obj.clean_turns.append(("user", "刚改完的地方单测跑过了吗？"))
                            st_obj.clean_turns.append(("assistant", "单测已全部通过，耗时与预期一致。"))

                    res = ref.refine(
                        str(probe["draft"]),
                        session_key=p_snap.session_key,
                        tracker=p_tracker,
                    )
                    flags = _evaluate_fidelity(probe, res)
                    if not flags:
                        pass_count += 1
                    status_str = "PASS" if not flags else ", ".join(flags)

                    in_t = res.input_tokens
                    c_t = res.cached_input_tokens
                    out_t = res.output_tokens
                    rsn_t = res.reasoning_tokens
                    if rsn_t is not None:
                        rsn_seen = True
                        total_rsn += rsn_t
                    hit_str = _fmt_hit_pct(in_t, c_t)
                    hit_val = (100.0 * c_t / in_t) if (in_t and c_t is not None and in_t > 0) else None

                    if idx == 0:
                        cold_init_ms = res.init_ms
                        cold_ref_ms = res.latency_ms
                        cold_hit_str = hit_str
                    else:
                        warm_latencies.append(res.latency_ms)
                        if probe.get("mutate_delta"):
                            delta_hit = hit_val
                        elif hit_val is not None:
                            stationary_hits.append(hit_val)

                    warn_note = f" ({redact_fn(res.warning)[:60]})" if res.warning else ""
                    round_rows.append(
                        f"| {probe['id']} | `{probe['name']}` | {res.context_chunks} / {res.context_chars} | "
                        f"{res.init_ms} | {res.latency_ms} | {_fmt_opt_int(in_t)} | {_fmt_opt_int(c_t)} | "
                        f"{hit_str} | {_fmt_opt_int(out_t)} / {_fmt_opt_int(rsn_t)} | {status_str}{warn_note} |"
                    )
                    safe_draft = _format_md_cell(redact_fn(probe["draft"]), multiline_arrow=False, max_chars=240)
                    safe_out = _format_md_cell(redact_fn(res.text), multiline_arrow=True, max_chars=240)
                    io_rows.append(f"| {probe['id']} (`{probe['name']}`) | {safe_draft} | {safe_out} | {status_str} |")
            finally:
                ref.close()

            warm_p50 = f"{int(round(statistics.median(warm_latencies)))}" if warm_latencies else "-"
            warm_avg = f"{int(round(statistics.mean(warm_latencies)))}" if warm_latencies else "-"
            warm_max = f"{max(warm_latencies)}" if warm_latencies else "-"
            stat_hit_avg = f"{statistics.mean(stationary_hits):.1f}%" if stationary_hits else "-"
            delta_hit_str = f"{delta_hit:.1f}%" if delta_hit is not None else "-"
            rsn_sum_str = str(total_rsn) if rsn_seen else "-"

            summary_rows.append(
                f"| `{prov}` | `{eff_model}` | `{p_cfg.refiner.effort}` | {cold_init_ms} / {cold_ref_ms} ({cold_hit_str}) | "
                f"{warm_p50} / {warm_avg} / {warm_max} | {stat_hit_avg} | {delta_hit_str} | {rsn_sum_str} | {pass_count}/{len(probes)} |"
            )

            report_parts.extend(
                [
                    f"## Provider: `{prov}` (`model={eff_model}`, `effort={p_cfg.refiner.effort}`)",
                    "",
                    "| Round | Probe | Ctx (chunks / chars) | Init (ms) | Refine (ms) | In Tok | Cached Tok | Hit % | Out / Rsn Tok | Fidelity |",
                    "| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :--- |",
                    *round_rows,
                    "",
                ]
            )
            sample_io_sections.extend(
                [
                    f"### Synthetic Probe Outputs (`{prov}` / `{eff_model}`)",
                    "",
                    "| Round | Synthetic Dictated Draft | Refined Output | Check |",
                    "| :--- | :--- | :--- | :--- |",
                    *io_rows,
                    "",
                ]
            )

        summary_block = [
            "## Summary Comparison",
            "",
            "| Provider | Model | Effort | R1 Cold Init/Total ms (Hit%) | Warm R2..RN p50/avg/max (ms) | Warm Stationary Hit% | R4 Post-Delta Hit% | Total Rsn Tok | Fidelity |",
            "| :--- | :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: |",
            *summary_rows,
            "",
        ]
        # Insert summary right after header metadata
        report_parts[7:7] = summary_block
        report_parts.extend(sample_io_sections)

        if args.wav:
            wav_p = Path(args.wav).expanduser().resolve()
            if not wav_p.is_file():
                raise RuntimeError(f"--wav file not found: {wav_p}")
            report_parts.append(
                _run_wav_benchmark(
                    wav_path=wav_p,
                    providers=providers,
                    base_cfg=base_cfg,
                    env=env,
                    home=home,
                    rt_dir=rt_dir,
                    snap=snap,
                    args=args,
                    redact_fn=redact_fn,
                )
            )

        full_md = "\n".join(report_parts).rstrip() + "\n"
        if args.out:
            out_p = Path(args.out).expanduser().resolve()
            out_p.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(out_p), os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(full_md)
        print(full_md, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
