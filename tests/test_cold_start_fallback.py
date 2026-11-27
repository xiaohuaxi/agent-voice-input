"""Offline unit tests for Agent Session cold-start fallback (Layer 0, Layer 1, Layer 2, SSH remote, local diag)."""
from __future__ import annotations

import contextlib
import io
import json
import math
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import wave

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_voice_input import _remote_probe as rp_mod  # noqa: E402
from agent_voice_input import prompts as prompts_mod  # noqa: E402
from agent_voice_input.asr_providers import BaseAsrProvider  # noqa: E402
from agent_voice_input.config import (  # noqa: E402
    Config,
    ConfigError,
    RefinerConfig,
    RemoteConfig,
    RemoteHostConfig,
    load_config,
)
from agent_voice_input.daemon import (  # noqa: E402
    build_pipeline_summary,
    run_pipeline_once,
)
from agent_voice_input.diagnose import (  # noqa: E402
    _sanitize_diag_record,
    collect_live_diagnostics,
)
from agent_voice_input.http_transport import HttpResponse  # noqa: E402
from agent_voice_input.llm_common import AsrContext, AsrResult  # noqa: E402
from agent_voice_input.platform_common import ForegroundWindow  # noqa: E402
from agent_voice_input.refiner import (  # noqa: E402
    BaseRefiner,
    OpenAICompatibleRefiner,
    RefinerResult,
    attach_single_hop_context,
    build_refiner_context_chunks,
    build_refiner_request_blocks,
)
from agent_voice_input.remote_ssh import (  # noqa: E402
    _validate_probe_response_schema,
    build_probe_stdin_payload,
    decode_probe_stdout,
)
from agent_voice_input.session_ctx import (  # noqa: E402
    BUILTIN_AGENT_HOTWORDS,
    SessionMatch,
    SessionTracker,
)
from agent_voice_input.workspace_ctx import (  # noqa: E402
    discover_antigravity_ide_prior_sessions,
    extract_prior_sessions_hotwords,
)


def _make_voiced_wav(duration_sec: float = 0.6, freq_hz: float = 440.0) -> bytes:
    sr = 16000
    n = int(sr * duration_sec)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        frames = bytearray()
        for i in range(n):
            v = int(12000 * math.sin(2.0 * math.pi * freq_hz * i / sr))
            frames.extend(struct.pack("<h", v))
        wf.writeframes(bytes(frames))
    return buf.getvalue()


def _write_claude_jsonl(
    home: Path,
    cwd: str,
    sid: str,
    turns: list[tuple[str, str]],
    *,
    mtime: float | None = None,
    branch: str = "",
    sidechain: bool = False,
    filename: str | None = None,
) -> Path:
    enc = rp_mod.encode_claude_project_dir(cwd)[0]
    pdir = home / ".claude" / "projects" / enc
    pdir.mkdir(parents=True, exist_ok=True)
    path = pdir / (filename or f"{sid}.jsonl")
    lines: list[str] = []
    if not turns:
        rec = {
            "type": "summary",
            "sessionId": sid,
            "cwd": cwd,
            "isSidechain": sidechain,
        }
        if branch:
            rec["gitBranch"] = branch
        lines.append(json.dumps(rec, ensure_ascii=False))
    else:
        for role, text in turns:
            rec = {
                "type": role,
                "sessionId": sid,
                "cwd": cwd,
                "isSidechain": sidechain,
                "message": {"role": role, "content": text},
            }
            if branch:
                rec["gitBranch"] = branch
            lines.append(json.dumps(rec, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _write_codex_jsonl(
    home: Path,
    cwd: str,
    sid: str,
    turns: list[tuple[str, str]],
    *,
    date_path: str = "2026/11/19",
    mtime: float | None = None,
    source: str = "cli",
    branch: str = "",
) -> Path:
    sdir = home / ".codex" / "sessions" / date_path
    sdir.mkdir(parents=True, exist_ok=True)
    path = sdir / f"rollout-{date_path.replace('/', '-')}T10-00-00-{sid}.jsonl"
    meta_payload: dict[str, object] = {"id": sid, "cwd": cwd, "source": source}
    if branch:
        meta_payload["git"] = {"branch": branch}
    lines = [json.dumps({"type": "session_meta", "payload": meta_payload}, ensure_ascii=False)]
    for role, text in turns:
        item_type = "UserMessage" if role == "user" else "AgentMessage"
        lines.append(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "item": {"type": item_type, "content": [{"type": "text", "text": text}]},
                    },
                },
                ensure_ascii=False,
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _write_agy_jsonl(
    home: Path,
    cwd: str,
    cid: str,
    turns: list[tuple[str, str]],
    *,
    ide: bool = False,
    mtime: float | None = None,
    subagent: bool = False,
) -> Path:
    base_dir = (
        home / ".gemini" / "antigravity" / "brain"
        if ide
        else home / ".gemini" / "antigravity-cli" / "brain"
    )
    logs_dir = base_dir / cid / ".system_generated" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / "transcript_full.jsonl"
    lines: list[str] = []
    if subagent:
        lines.append(
            json.dumps(
                {
                    "source": "SYSTEM",
                    "type": "SYSTEM_MESSAGE",
                    "content": "[Message] timestamp=2026-11-19T10:00:00Z sender=parent-id <subagent_reminder>",
                },
                ensure_ascii=False,
            )
        )
    else:
        lines.append(
            json.dumps(
                {
                    "source": "SYSTEM",
                    "type": "SYSTEM_MESSAGE",
                    "cwd": cwd,
                    "content": f"Active Workspaces:\n- {cwd}\nCommand Working Directory: {cwd}",
                },
                ensure_ascii=False,
            )
        )
    for role, text in turns:
        if role == "user":
            lines.append(
                json.dumps(
                    {"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": text},
                    ensure_ascii=False,
                )
            )
        else:
            lines.append(
                json.dumps(
                    {"source": "MODEL", "type": "PLANNER_RESPONSE", "content": text},
                    ensure_ascii=False,
                )
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _make_rich_turns(tag: str, *, pairs: int = 2, pad_chars: int = 900) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    filler = "x" * max(0, pad_chars - 80)
    for i in range(pairs):
        out.append(("user", f"Let us inspect {tag}Controller and {tag}_helper.py step {i}. {filler}"))
        out.append(("assistant", f"Updated {tag}Manager with --{tag.lower()}-flag for step {i}. {filler}"))
    return out


class _CaptureAsr(BaseAsrProvider):
    provider_name = "capture-asr"

    def __init__(self, *, text: str = "hello draft", single_hop: bool = False) -> None:
        self.text = text
        self.single_hop = single_hop
        self.seen_contexts: list[AsrContext] = []

    def transcribe(
        self,
        wav_bytes: bytes,
        *,
        context: AsrContext | None = None,
        is_cancelled=None,
        cancel_token=None,
    ) -> AsrResult:
        ctx = context if isinstance(context, AsrContext) else AsrContext()
        self.seen_contexts.append(ctx)
        return AsrResult(
            text=self.text,
            provider=self.provider_name,
            model="capture-m",
            latency_ms=10,
            is_single_hop_refined=self.single_hop,
            context_chars=sum(len(b) for b in ctx.context_blocks),
            context_chunks=len(ctx.context_blocks),
        )


class _CaptureRefiner(BaseRefiner):
    provider_name = "capture-ref"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.seen_calls: list[tuple[list[str], str, str, str]] = []

    def refine(
        self,
        asr_input: AsrResult | str,
        *,
        session_key: str = "",
        tracker: SessionTracker | None = None,
        fallback_context: str = "",
        is_cancelled=None,
        cancel_token=None,
        mode: str = "agent",
        workspace_cwd: str = "",
    ) -> RefinerResult:
        draft = asr_input.text if isinstance(asr_input, AsrResult) else str(asr_input)
        blocks, final_blk = build_refiner_request_blocks(
            self.cfg,
            draft,
            session_key=session_key,
            tracker=tracker,
            fallback_context=fallback_context,
            mode=mode,
            workspace_cwd=workspace_cwd,
        )
        self.seen_calls.append((blocks, final_blk, session_key, workspace_cwd))
        used_prior = any(b.startswith("<prior_session_reference") for b in blocks)
        return RefinerResult(
            text=draft + "!",
            provider=self.provider_name,
            model="cap-ref",
            latency_ms=12,
            context_chars=sum(len(b) for b in blocks),
            context_chunks=len(blocks),
            used_prior_session=used_prior,
        )


class TestColdStartConfigAndPrompts(unittest.TestCase):
    def test_config_cold_start_fallback_values_and_validation(self) -> None:
        self.assertEqual(Config().refiner.cold_start_fallback, "auto")
        with tempfile.TemporaryDirectory() as td:
            for raw, expected in (("auto", "auto"), ("HOTWORDS", "hotwords"), ("Off", "off")):
                p = Path(td) / f"cfg_{expected}.toml"
                p.write_text(f'[refiner]\ncold_start_fallback = "{raw}"\n', encoding="utf-8")
                cfg, _ = load_config(config_path=p, env={"HOME": td})
                self.assertEqual(cfg.refiner.cold_start_fallback, expected)

            p_bad = Path(td) / "bad.toml"
            p_bad.write_text('[refiner]\ncold_start_fallback = "always"\n', encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(config_path=p_bad, env={"HOME": td})

            p_type = Path(td) / "bad_type.toml"
            p_type.write_text("[refiner]\ncold_start_fallback = 123\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(config_path=p_type, env={"HOME": td})

        with self.assertRaises(ConfigError):
            from agent_voice_input.config import _validate_refiner_config

            _validate_refiner_config(Config(refiner=RefinerConfig(cold_start_fallback=True)))  # type: ignore[arg-type]

    def test_prompts_isolate_prior_session_reference(self) -> None:
        self.assertIn("<prior_session_reference>", prompts_mod.REFINER_SYSTEM_PROMPT)
        self.assertIn("always prioritize the current session and workspace", prompts_mod.REFINER_SYSTEM_PROMPT)
        self.assertIn("<prior_session_reference>", prompts_mod.SINGLE_HOP_SYSTEM_PROMPT)
        self.assertIn("<prior_session_reference>", prompts_mod.REFINER_TASK_REMINDER)
        self.assertNotIn("prioritize the current session", prompts_mod.REFINER_GENERAL_SYSTEM_PROMPT)


class TestColdStartDiscoverySelectionAndGuards(unittest.TestCase):
    def test_exact_cwd_no_age_limit_no_branch_bias_and_subagent_exclusions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            repo = home / "repos" / "alpha"
            parent = home / "repos"
            child = repo / "subpkg"
            sibling = home / "repos" / "alpha-wt"
            for d in (repo, child, sibling):
                d.mkdir(parents=True, exist_ok=True)
            now = time.time()

            # 180-day-old session in EXACT repo (no age limit!)
            _write_claude_jsonl(
                home,
                str(repo),
                "old-exact-180d",
                _make_rich_turns("LegacyAlpha", pairs=2, pad_chars=900),
                mtime=now - 180 * 86400,
                branch="main",
            )
            # Parent dir, child dir, sibling worktree, broad dir: ALL must be rejected!
            _write_claude_jsonl(home, str(parent), "parent-sess", _make_rich_turns("ParentLeak"), mtime=now - 10)
            _write_claude_jsonl(home, str(child), "child-sess", _make_rich_turns("ChildLeak"), mtime=now - 10)
            _write_claude_jsonl(home, str(sibling), "sib-sess", _make_rich_turns("SibLeak"), mtime=now - 10)
            _write_claude_jsonl(home, str(home), "home-sess", _make_rich_turns("HomeLeak"), mtime=now - 10)
            # Subagents in exact repo: ALL must be rejected!
            _write_claude_jsonl(
                home, str(repo), "cl-sub", _make_rich_turns("ClSub"), mtime=now - 5, sidechain=True
            )
            _write_claude_jsonl(
                home, str(repo), "cl-agent-file", _make_rich_turns("ClAgent"), mtime=now - 5, filename="agent-abc.jsonl"
            )
            _write_codex_jsonl(
                home, str(repo), "cx-sub", _make_rich_turns("CxSub"), mtime=now - 5, source="exec"
            )
            _write_agy_jsonl(
                home, str(repo), "agy-sub", _make_rich_turns("AgySub"), mtime=now - 5, subagent=True
            )

            cands = rp_mod.discover_cli_prior_sessions(str(repo), home=home, wsl=False)
            self.assertEqual([c["session_id"] for c in cands], ["old-exact-180d"])

            # Broad directories always return empty
            self.assertEqual(rp_mod.discover_cli_prior_sessions(str(home), home=home, wsl=False), [])
            self.assertEqual(rp_mod.discover_cli_prior_sessions("/tmp", home=home, wsl=False), [])

            # Branch is NOT filtered or weighted: newer session on feature-x beats older session on main
            _write_claude_jsonl(
                home,
                str(repo),
                "newer-other-branch",
                _make_rich_turns("FeatureBranch", pairs=2, pad_chars=900),
                mtime=now - 60,
                branch="feat/unrelated",
            )
            cands2 = rp_mod.discover_cli_prior_sessions(str(repo), home=home, wsl=False)
            best, _ = rp_mod.select_cold_start_prior_candidate(cands2, preferred_cli="claude")
            self.assertIsNotNone(best)
            self.assertEqual(best["session_id"], "newer-other-branch")

    def test_sweet_spot_same_cli_priority_cross_cli_and_fallback_tier(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            repo = home / "repos" / "beta"
            repo.mkdir(parents=True, exist_ok=True)
            now = time.time()

            # 1) Newest is thin (< 3000 chars, only 2 msgs); 2nd is rich cross-CLI (agy, NO presence lock!);
            # 3rd is rich same-CLI (codex, slightly older within Top-5)
            _write_codex_jsonl(
                home, str(repo), "cx-thin", [("user", "a" * 600), ("assistant", "b" * 600)],
                date_path="2026/11/20", mtime=now - 10,
            )
            _write_agy_jsonl(
                home, str(repo), "agy-rich-newest", _make_rich_turns("AgyRich", pairs=2, pad_chars=900),
                mtime=now - 20,
            )
            _write_codex_jsonl(
                home, str(repo), "cx-rich-older", _make_rich_turns("CodexRich", pairs=2, pad_chars=900),
                date_path="2026/11/19", mtime=now - 30,
            )
            _write_claude_jsonl(
                home, str(repo), "cl-stub-tiny", [("user", "hi"), ("assistant", "ok")],
                mtime=now - 5,
            )

            cands = rp_mod.discover_cli_prior_sessions(str(repo), home=home, wsl=False)
            # Stub < 1000 chars excluded; agy without presence lock IS included!
            sids = {c["session_id"] for c in cands}
            self.assertEqual(sids, {"cx-thin", "agy-rich-newest", "cx-rich-older"})

            # Preferred CLI = codex -> same CLI sweet-spot wins within Top-5
            best_cx, top5 = rp_mod.select_cold_start_prior_candidate(
                cands, preferred_cli="codex", exclude_session_id="current-new"
            )
            self.assertIsNotNone(best_cx)
            self.assertEqual(best_cx["session_id"], "cx-rich-older")
            self.assertEqual(len(top5), 3)

            # Preferred CLI = claude (no claude in sweet-spot) -> cross-CLI newest sweet-spot (agy) wins
            best_cl, _ = rp_mod.select_cold_start_prior_candidate(
                cands, preferred_cli="claude", exclude_session_id="current-new"
            )
            self.assertIsNotNone(best_cl)
            self.assertEqual(best_cl["session_id"], "agy-rich-newest")

            # Excluding current session_id
            best_excl, _ = rp_mod.select_cold_start_prior_candidate(
                cands, preferred_cli="codex", exclude_session_id="cx-rich-older"
            )
            self.assertIsNotNone(best_excl)
            self.assertEqual(best_excl["session_id"], "agy-rich-newest")

        # Fallback tier: when NO Top-5 hits 3000 chars, pick max chars among >= 1000 chars
        fb_cands = [
            {"cli": "claude", "session_id": "s1", "jsonl_mtime": 300.0, "turns": (("user", "a" * 1200),), "total_chars": 1200},
            {"cli": "codex", "session_id": "s2", "jsonl_mtime": 200.0, "turns": (("user", "b" * 2400),), "total_chars": 2400},
            {"cli": "agy", "session_id": "s3", "jsonl_mtime": 100.0, "turns": (("user", "c" * 1800),), "total_chars": 1800},
        ]
        best_fb, _ = rp_mod.select_cold_start_prior_candidate(fb_cands, preferred_cli="claude")
        self.assertIsNotNone(best_fb)
        self.assertEqual(best_fb["session_id"], "s2")

    def test_agy_ide_prior_isolation_from_cli(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            repo = home / "repos" / "gamma"
            repo.mkdir(parents=True, exist_ok=True)
            now = time.time()

            # CLI prior session in ~/.gemini/antigravity-cli/brain
            _write_agy_jsonl(home, str(repo), "cli-only-cid", _make_rich_turns("CliOnly"), ide=False, mtime=now - 10)
            # IDE prior session in ~/.gemini/antigravity/brain (older than 6h — still eligible!)
            _write_agy_jsonl(
                home, str(repo), "ide-old-cid", _make_rich_turns("IdeOnly"), ide=True, mtime=now - 30 * 86400
            )
            # IDE subagent — must be excluded
            _write_agy_jsonl(home, str(repo), "ide-sub-cid", _make_rich_turns("IdeSub"), ide=True, mtime=now - 5, subagent=True)

            cli_cands = rp_mod.discover_cli_prior_sessions(str(repo), home=home, wsl=False)
            ide_cands = discover_antigravity_ide_prior_sessions(str(repo), home=home, wsl=False)

            self.assertEqual([c["session_id"] for c in cli_cands], ["cli-only-cid"])
            self.assertEqual([c["session_id"] for c in ide_cands], ["ide-old-cid"])

    def test_agy20_db_prior_discovery_and_cold_start_sync_ide_db_title(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            repo = home / "repos" / "agy20-repo"
            other_repo = home / "repos" / "other-repo"
            sub_dir = repo / "subpkg"
            for d in (repo, other_repo, sub_dir):
                d.mkdir(parents=True, exist_ok=True)
            now = time.time()

            ide_root = home / ".gemini" / "antigravity"
            ide_brain = ide_root / "brain"

            def _write_plain_agy20_tp(cid: str, turns: list[tuple[str, str]], mtime: float) -> Path:
                logs_dir = ide_brain / cid / ".system_generated" / "logs"
                logs_dir.mkdir(parents=True, exist_ok=True)
                tp = logs_dir / "transcript_full.jsonl"
                lines = [
                    json.dumps(
                        {
                            "source": "USER_EXPLICIT" if r == "user" else "MODEL",
                            "type": "USER_INPUT" if r == "user" else "PLANNER_RESPONSE",
                            "content": t,
                        },
                        ensure_ascii=False,
                    )
                    for r, t in turns
                ]
                tp.write_text("\n".join(lines) + "\n", encoding="utf-8")
                os.utime(tp, (mtime, mtime))
                return tp

            # AGY 2.0 transcripts have NO SYSTEM/subagents L1 evidence; workspace link lives in DB!
            _write_plain_agy20_tp("agy20-prior-rich", _make_rich_turns("PrismIndexer", pairs=2, pad_chars=900), now - 3600)
            _write_plain_agy20_tp("agy20-other-repo", _make_rich_turns("OtherRepoLeak", pairs=2, pad_chars=900), now - 100)
            _write_plain_agy20_tp("agy20-sub-dir", _make_rich_turns("SubDirLeak", pairs=2, pad_chars=900), now - 90)
            cur_tp = _write_plain_agy20_tp("agy20-cur-cold", [("user", "Start new task")], now - 10)
            # P1 #2: DB-vetoed sessions that STILL contain local SYSTEM cwd=repo in transcript MUST NOT be resurrected!
            _write_agy_jsonl(home, str(repo), "agy20-remote-veto", _make_rich_turns("RemoteVetoLeak", pairs=2, pad_chars=900), ide=True, mtime=now - 15)
            _write_agy_jsonl(home, str(repo), "agy20-parent-veto", _make_rich_turns("ParentVetoLeak", pairs=2, pad_chars=900), ide=True, mtime=now - 14)
            _write_agy_jsonl(home, str(repo), "agy20-refiner-veto", _make_rich_turns("RefinerVetoLeak", pairs=2, pad_chars=900), ide=True, mtime=now - 13)
            _write_agy_jsonl(home, str(repo), "agy20-other-veto", _make_rich_turns("OtherWsVetoLeak", pairs=2, pad_chars=900), ide=True, mtime=now - 12)
            # CLI session in exact repo must remain strictly isolated from agy_ide scope
            _write_agy_jsonl(home, str(repo), "cli-exact-cid", _make_rich_turns("CliLeak", pairs=2, pad_chars=900), ide=False, mtime=now - 5)

            db_path = ide_root / "conversation_summaries.db"
            db_rows: list[tuple[object, ...]] = [
                ("agy20-prior-rich", "Prior Rich Task", json.dumps(["file://[invalid/path", repo.as_uri()]), "2025-01-01T10:00:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                ("agy20-other-repo", "Other Repo Task", json.dumps([other_repo.as_uri()]), "2026-11-23T08:00:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                ("agy20-sub-dir", "Sub Dir Task", json.dumps([sub_dir.as_uri()]), "2026-11-23T08:05:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                ("agy20-cur-cold", "Current Cold Task", json.dumps([repo.as_uri()]), "2026-11-23T09:00:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                ("agy20-remote-veto", "Remote SSH Session", json.dumps(["ssh://prod/srv/app"]), "2026-11-23T09:01:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                ("agy20-parent-veto", "Subagent Row", json.dumps([repo.as_uri()]), "2026-11-23T09:02:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "agy20-cur-cold", 0, ""),
                ("agy20-refiner-veto", "Refiner Row", json.dumps([repo.as_uri()]), "2026-11-23T09:03:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, "avi-refiner"),
                ("agy20-other-veto", "Rebound To Other Repo", json.dumps([other_repo.as_uri()]), "2026-11-23T09:04:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                ("agy20-bad-uri-only", "Malformed Unrelated Row", json.dumps(["file://[invalid/path"]), "2026-11-23T09:05:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
            ]
            # Insert >210 newer unrelated rows so older repo prior is not truncated by a 200-row cap
            for i in range(215):
                db_rows.append((
                    f"agy20-noise-{i:03d}",
                    f"Noise {i:03d}",
                    json.dumps([other_repo.as_uri()]),
                    f"2026-11-23T11:{i // 60:02d}:{i % 60:02d}Z",
                    "CORTEX_TRAJECTORY_SOURCE_CASCADE",
                    "",
                    0,
                    "",
                ))
            with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    """
                    CREATE TABLE conversation_summaries (
                        conversation_id TEXT PRIMARY KEY,
                        title TEXT,
                        workspace_uris TEXT,
                        last_modified_time TEXT,
                        source TEXT,
                        parent_conversation_id TEXT,
                        nesting_depth INTEGER,
                        agent_name TEXT
                    )
                    """
                )
                conn.executemany(
                    "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    db_rows,
                )
                conn.commit()

            cands = discover_antigravity_ide_prior_sessions(
                str(repo), home=home, wsl=False, exclude_session_id="agy20-cur-cold"
            )
            self.assertEqual([c["session_id"] for c in cands], ["agy20-prior-rich"])

            # Verify sync_ide_session with matched_by="ide_db_title" uses prior_scope="agy_ide" (never leaks CLI prior!)
            env = {"HOME": str(home), "WSL_DISTRO_NAME": "", "WSL_INTEROP": ""}
            cfg = Config(refiner=RefinerConfig(provider="auto", cold_start_fallback="auto"))
            tracker = SessionTracker(home_dir=home, proc_root=None, env=env, cold_start_fallback="auto")
            try:
                snap = tracker.sync_ide_session(
                    SessionMatch(
                        cli="agy",
                        session_id="agy20-cur-cold",
                        jsonl_path=str(cur_tp),
                        cwd=str(repo),
                        matched_by="ide_db_title",
                    ),
                    include_git_info=False,
                )
                self.assertTrue(snap.prior_hotwords_used)
                self.assertIn("PrismIndexerController", snap.hotwords)
                self.assertNotIn("CliLeakController", snap.hotwords)
                blocks, _ = build_refiner_context_chunks(cfg, session_key=snap.session_key, tracker=tracker)
                self.assertTrue(blocks and blocks[0].startswith("<prior_session_reference"))
                self.assertIn("PrismIndexerController", blocks[0])
                self.assertNotIn("CliLeakController", blocks[0])
            finally:
                tracker.close()

    def test_codex_reverse_date_bounded_scan_and_wsl_mnt_guard(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            repo = home / "repos" / "delta"
            repo.mkdir(parents=True, exist_ok=True)
            # Create files across multiple months; newer date dir is scanned first
            _write_codex_jsonl(home, str(repo), "old-month", _make_rich_turns("OldMonth"), date_path="2025/01/05", mtime=100.0)
            _write_codex_jsonl(home, str(repo), "new-month", _make_rich_turns("NewMonth"), date_path="2026/11/19", mtime=200.0)

            files = rp_mod._iter_codex_rollout_files_desc(home, max_files=1, wsl=False)
            self.assertEqual(len(files), 1)
            self.assertIn("new-month", files[0])

            # WSL /mnt/ slow mount path rejected with zero I/O
            self.assertEqual(rp_mod.discover_cli_prior_sessions("/mnt/c/Users/x/repo", home=home, wsl=True), [])


class TestColdStartLayer0Layer1Layer2AndEviction(unittest.TestCase):
    def test_layer0_terminal_no_snap_uses_win_cwd_and_respects_remote_veto_and_broad_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            repo = home / "repos" / "l0proj"
            repo.mkdir(parents=True, exist_ok=True)
            (repo / "alpha_service.py").write_text("print('hi')\n", encoding="utf-8")
            _write_claude_jsonl(
                home, str(repo), "prior-l0", _make_rich_turns("QuantumRouter", pairs=2, pad_chars=900)
            )

            env = {"HOME": str(home), "WSL_DISTRO_NAME": "", "WSL_INTEROP": ""}
            tracker = SessionTracker(home_dir=home, proc_root=None, env=env)
            cfg = Config(refiner=RefinerConfig(provider="auto", cold_start_fallback="auto"))
            asr = _CaptureAsr(text="check alpha service")
            ref = _CaptureRefiner(cfg)
            wav = _make_voiced_wav()

            # 1) Terminal with sync_session returning None (e.g. brand-new Codex / OpenCode / empty shell)
            #    but valid win.cwd -> Layer 0 sets workspace_cwd (shallow_files + Layer 1 prior hotwords)
            with patch.object(tracker, "resolve_session", return_value=None):
                res = run_pipeline_once(
                    wav,
                    cfg=cfg,
                    tracker=tracker,
                    asr_provider=asr,
                    refiner=ref,
                    origin_window=ForegroundWindow(1, "Ghostty", "opencode", cwd=str(repo), bundle_id="com.mitchellh.ghostty"),
                    env=env,
                    dry_run=True,
                )
            self.assertTrue(res.ok)
            self.assertEqual(res.ctx_fallback, "prior_hotwords")
            self.assertIn("QuantumRouterController", asr.seen_contexts[0].hotwords)
            self.assertIn("alpha_service", asr.seen_contexts[0].hotwords)
            # Layer 0 has no active Agent session -> Stage 2 gets workspace files, NOT <prior_session_reference>
            blocks, final_blk, _, ws_cwd = ref.seen_calls[0]
            self.assertEqual(ws_cwd, str(repo.resolve()))
            self.assertEqual(blocks, [])
            self.assertIn("alpha_service.py", final_blk)

            # 2) Broad dir win.cwd (~) MUST NOT trigger Layer 0
            asr2 = _CaptureAsr()
            ref2 = _CaptureRefiner(cfg)
            with patch.object(tracker, "resolve_session", return_value=None):
                res_broad = run_pipeline_once(
                    wav,
                    cfg=cfg,
                    tracker=tracker,
                    asr_provider=asr2,
                    refiner=ref2,
                    origin_window=ForegroundWindow(2, "Ghostty", "zsh", cwd=str(home), bundle_id="com.mitchellh.ghostty"),
                    env=env,
                    dry_run=True,
                )
            self.assertEqual(res_broad.ctx_fallback, "")
            self.assertEqual(ref2.seen_calls[0][3], "")

            # 3) Remote-vetoed SSH terminal (unwhitelisted host) MUST NOT leak local win.cwd!
            asr3 = _CaptureAsr()
            ref3 = _CaptureRefiner(cfg)
            res_ssh = run_pipeline_once(
                wav,
                cfg=cfg,
                tracker=tracker,
                asr_provider=asr3,
                refiner=ref3,
                origin_window=ForegroundWindow(
                    3, "Ghostty", "user@unlisted-box: ~/app [@unlisted-box]",
                    cwd=str(repo), bundle_id="com.mitchellh.ghostty",
                ),
                env=env,
                dry_run=True,
            )
            self.assertEqual(res_ssh.ctx_fallback, "")
            self.assertNotIn("QuantumRouterController", asr3.seen_contexts[0].hotwords)
            self.assertEqual(ref3.seen_calls[0][3], "")
            tracker.close()

    def test_ordinary_ide_gets_layer1_hotwords_only_never_layer2_prose(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            repo = home / "repos" / "ideproj"
            repo.mkdir(parents=True, exist_ok=True)
            (repo / "editor_core.py").write_text("x = 1\n", encoding="utf-8")
            _write_claude_jsonl(
                home, str(repo), "cli-prior-ide", _make_rich_turns("VoxelEngine", pairs=2, pad_chars=900)
            )
            env = {"HOME": str(home), "WSL_DISTRO_NAME": "", "WSL_INTEROP": ""}
            tracker = SessionTracker(home_dir=home, proc_root=None, env=env)
            cfg = Config(refiner=RefinerConfig(provider="auto", cold_start_fallback="auto"))
            asr = _CaptureAsr()
            ref = _CaptureRefiner(cfg)
            win = ForegroundWindow(
                10, "Cursor", "editor_core.py — ideproj — Cursor", bundle_id="com.todesktop.230313mzl4w4u92"
            )
            res = run_pipeline_once(
                _make_voiced_wav(),
                cfg=cfg,
                tracker=tracker,
                asr_provider=asr,
                refiner=ref,
                origin_window=win,
                env=env,
                dry_run=True,
            )
            self.assertTrue(res.ok)
            self.assertEqual(res.ctx_fallback, "prior_hotwords")
            hw = asr.seen_contexts[0].hotwords
            self.assertIn("VoxelEngineController", hw)
            # Hotword priority: workspace stems/git BEFORE prior hotwords BEFORE BUILTIN_AGENT_HOTWORDS
            self.assertLess(hw.index("editor_core"), hw.index("VoxelEngineController"))
            self.assertLess(hw.index("VoxelEngineController"), hw.index(BUILTIN_AGENT_HOTWORDS[0]))
            # Ordinary IDE MUST NOT receive <prior_session_reference> prose
            self.assertEqual(ref.seen_calls[0][0], [])
            tracker.close()

    def test_layer2_freeze_immutability_prefix_stability_and_natural_eviction(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            repo = home / "repos" / "freezeproj"
            repo.mkdir(parents=True, exist_ok=True)
            now = time.time()

            # Rich prior session (> 25K chars so tail slice is exercised!)
            prior_turns = _make_rich_turns("PriorFrozen", pairs=20, pad_chars=1200)
            prior_path = _write_claude_jsonl(
                home, str(repo), "prior-rich-1", prior_turns, mtime=now - 120
            )
            # Brand-new empty current session (0 turns)
            cur_path = _write_claude_jsonl(home, str(repo), "new-cold-1", [], mtime=now - 10)

            env = {"HOME": str(home), "WSL_DISTRO_NAME": "", "WSL_INTEROP": ""}
            # Use context_max_chars=40000 to verify natural eviction cleanly without huge files
            cfg = Config(refiner=RefinerConfig(provider="auto", context_max_chars=40000, cold_start_fallback="auto"))
            tracker = SessionTracker(
                home_dir=home,
                proc_root=None,
                env=env,
                context_max_chars=cfg.refiner.context_max_chars,
                cold_start_fallback="auto",
            )
            match_cur = SessionMatch(
                cli="claude", session_id="new-cold-1", jsonl_path=str(cur_path), cwd=str(repo), matched_by="registry"
            )

            # Step 1: 0 turns in new session -> freezes prior chunk (<= 25K) and never pollutes clean_turns
            snap0 = tracker.sync_session(match_cur, include_git_info=False)
            self.assertIsNotNone(snap0)
            self.assertEqual(snap0.clean_turns, ())
            self.assertTrue(snap0.prior_hotwords_used)
            self.assertIn("PriorFrozenController", snap0.hotwords)

            blocks0, _ = build_refiner_context_chunks(cfg, session_key=snap0.session_key, tracker=tracker)
            self.assertEqual(len(blocks0), 1)
            self.assertTrue(blocks0[0].startswith('<prior_session_reference read_only="true" scope="same_workspace_previous_session">'))
            self.assertIn("PriorFrozenController", blocks0[0])
            st_obj = tracker.get_state(snap0.session_key)
            self.assertIsNotNone(st_obj)
            self.assertLessEqual(len(st_obj.cold_start_prior_chunk), rp_mod.COLD_START_PRIOR_MAX_CHARS)
            self.assertEqual(st_obj.clean_turns, [])
            frozen_block0_bytes = blocks0[0]

            # Step 2: Mutate the ORIGINAL prior JSONL file on disk (simulate concurrent session in sibling tab)
            with open(prior_path, "a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "type": "user",
                            "sessionId": "prior-rich-1",
                            "cwd": str(repo),
                            "isSidechain": False,
                            "message": {"role": "user", "content": "MUTATED_AFTER_FREEZE_SHOULD_NEVER_APPEAR"},
                        }
                    )
                    + "\n"
                )

            # Append 4 turns (~5,000 chars) to NEW session -> NO early retirement at 2 rounds / 4K chars!
            new_turns_4 = [
                ("user", "NewTaskAlpha " + ("a" * 1200)),
                ("assistant", "NewReplyAlpha " + ("b" * 1200)),
                ("user", "NewTaskBeta " + ("c" * 1200)),
                ("assistant", "NewReplyBeta " + ("d" * 1200)),
            ]
            with open(cur_path, "a", encoding="utf-8") as f:
                for r, t in new_turns_4:
                    f.write(
                        json.dumps(
                            {
                                "type": r,
                                "sessionId": "new-cold-1",
                                "cwd": str(repo),
                                "isSidechain": False,
                                "message": {"role": r, "content": t},
                            }
                        )
                        + "\n"
                    )

            snap1 = tracker.sync_session(match_cur, include_git_info=False)
            self.assertIsNotNone(snap1)
            self.assertEqual(len(snap1.clean_turns), 4)
            # Same-cwd cross-session prior hotwords stay active past 4 turns, strictly ranked after current turn_hw
            self.assertTrue(snap1.prior_hotwords_used)
            self.assertIn("PriorFrozenController", snap1.hotwords)
            self.assertIn("NewTaskAlpha", snap1.hotwords)
            self.assertLess(snap1.hotwords.index("NewTaskAlpha"), snap1.hotwords.index("PriorFrozenController"))

            blocks1, _ = build_refiner_context_chunks(cfg, session_key=snap1.session_key, tracker=tracker)
            self.assertGreaterEqual(len(blocks1), 2)
            # Block 0 MUST be 100% byte-identical (zero prefix cache churn, immune to prior file mutation!)
            self.assertEqual(blocks1[0], frozen_block0_bytes)
            self.assertNotIn("MUTATED_AFTER_FREEZE_SHOULD_NEVER_APPEAR", blocks1[0])
            self.assertTrue(blocks1[1].startswith('<session_reference_log read_only="true">'))
            self.assertIn("NewTaskAlpha", blocks1[1])

            # Single-Hop shares exact same frozen block 0!
            sh_ctx = attach_single_hop_context(
                cfg, AsrContext(), tracker=tracker, session_key=snap1.session_key, mode="agent"
            )
            self.assertEqual(sh_ctx.context_blocks[0], frozen_block0_bytes)
            self.assertEqual(sh_ctx.context_blocks[1], blocks1[1])

            # Step 3: Grow new session past total window (40,000 chars) -> NATURAL EVICTION!
            with open(cur_path, "a", encoding="utf-8") as f:
                for i in range(6):
                    f.write(
                        json.dumps(
                            {
                                "type": "user" if i % 2 == 0 else "assistant",
                                "sessionId": "new-cold-1",
                                "cwd": str(repo),
                                "isSidechain": False,
                                "message": {"role": "user" if i % 2 == 0 else "assistant", "content": f"GrowTurn{i} " + ("z" * 4000)},
                            }
                        )
                        + "\n"
                    )
            snap2 = tracker.sync_session(match_cur, include_git_info=False)
            self.assertIsNotNone(snap2)
            blocks2, _ = build_refiner_context_chunks(cfg, session_key=snap2.session_key, tracker=tracker)
            self.assertTrue(blocks2)
            self.assertFalse(any(b.startswith("<prior_session_reference") for b in blocks2))
            self.assertTrue(tracker.get_state(snap2.session_key).cold_start_prior_retired)

            # Step 4: Even after compaction shrinks clean_turns to 1 short summary, prior is NEVER re-attached!
            with open(cur_path, "a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "type": "system",
                            "subtype": "compact_boundary",
                            "sessionId": "new-cold-1",
                            "cwd": str(repo),
                            "isSidechain": False,
                        }
                    )
                    + "\n"
                )
                f.write(
                    json.dumps(
                        {
                            "type": "user",
                            "isCompactSummary": True,
                            "sessionId": "new-cold-1",
                            "cwd": str(repo),
                            "isSidechain": False,
                            "message": {"role": "user", "content": "Compact summary of new session."},
                        }
                    )
                    + "\n"
                )
            snap3 = tracker.sync_session(match_cur, include_git_info=False)
            self.assertEqual(len(snap3.clean_turns), 1)
            blocks3, _ = build_refiner_context_chunks(cfg, session_key=snap3.session_key, tracker=tracker)
            self.assertFalse(any(b.startswith("<prior_session_reference") for b in blocks3))
            tracker.close()

    def test_modes_auto_hotwords_off_and_prompt_cache_key_binds_current_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            repo = home / "repos" / "modeproj"
            repo.mkdir(parents=True, exist_ok=True)
            now = time.time()
            _write_claude_jsonl(home, str(repo), "prior-mode-src", _make_rich_turns("NebulaCache"), mtime=now - 100)
            cur_path = _write_claude_jsonl(home, str(repo), "cur-mode-dst", [], mtime=now - 5)
            env = {"HOME": str(home), "WSL_DISTRO_NAME": "", "WSL_INTEROP": ""}
            match_cur = SessionMatch(
                cli="claude", session_id="cur-mode-dst", jsonl_path=str(cur_path), cwd=str(repo), matched_by="registry"
            )
            wav = _make_voiced_wav()

            for fb_mode, expect_hw, expect_prior_blk, expect_enum in (
                ("auto", True, True, "prior_session"),
                ("hotwords", True, False, "prior_hotwords"),
                ("off", False, False, ""),
            ):
                cfg = Config(refiner=RefinerConfig(provider="auto", cold_start_fallback=fb_mode))
                tracker = SessionTracker(home_dir=home, proc_root=None, env=env, cold_start_fallback=fb_mode)
                asr = _CaptureAsr()
                ref = _CaptureRefiner(cfg)
                with patch.object(tracker, "resolve_session", return_value=match_cur):
                    res = run_pipeline_once(
                        wav,
                        cfg=cfg,
                        tracker=tracker,
                        asr_provider=asr,
                        refiner=ref,
                        origin_window=ForegroundWindow(1, "Ghostty", "claude", cwd=str(repo), bundle_id="com.mitchellh.ghostty"),
                        env=env,
                        dry_run=True,
                    )
                self.assertEqual(res.ctx_fallback, expect_enum, fb_mode)
                self.assertEqual("NebulaCacheController" in asr.seen_contexts[0].hotwords, expect_hw, fb_mode)
                has_prior_blk = any(b.startswith("<prior_session_reference") for b in ref.seen_calls[0][0])
                self.assertEqual(has_prior_blk, expect_prior_blk, fb_mode)
                # session_key passed to refiner is ALWAYS current session, never borrowed prior ID!
                self.assertEqual(ref.seen_calls[0][2], "claude:cur-mode-dst")
                self.assertNotIn("prior-mode-src", ref.seen_calls[0][2])
                tracker.close()

    def test_warm_session_on_first_sync_skips_prior_scan_and_cache_reuses_within_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            repo = home / "repos" / "warmproj"
            repo.mkdir(parents=True, exist_ok=True)
            now = time.time()
            _write_claude_jsonl(home, str(repo), "prior-w", _make_rich_turns("WarmPrior"), mtime=now - 100)
            warm_path = _write_claude_jsonl(home, str(repo), "already-warm", _make_rich_turns("CurrentWarm"), mtime=now - 5)
            cold_path = _write_claude_jsonl(home, str(repo), "cold-cache-1", [], mtime=now - 2)
            env = {"HOME": str(home), "WSL_DISTRO_NAME": "", "WSL_INTEROP": ""}
            tracker = SessionTracker(home_dir=home, proc_root=None, env=env, prior_cache_ttl_sec=60.0)

            # Warm session (>= 4 turns) retires Layer 2 <prior_session_reference> on first sync,
            # while still discovering same-cwd Layer 1 prior hotwords once (shared via 60s cache!).
            with patch(
                "agent_voice_input.session_ctx.discover_cli_prior_sessions",
                wraps=rp_mod.discover_cli_prior_sessions,
            ) as m_disc_warm:
                snap_w = tracker.sync_session(
                    SessionMatch(cli="claude", session_id="already-warm", jsonl_path=str(warm_path), cwd=str(repo)),
                    include_git_info=False,
                )
                self.assertEqual(m_disc_warm.call_count, 1)
                self.assertTrue(snap_w.prior_hotwords_used)
                self.assertIn("WarmPriorController", snap_w.hotwords)
                self.assertIn("CurrentWarmController", snap_w.hotwords)
                self.assertLess(
                    snap_w.hotwords.index("CurrentWarmController"),
                    snap_w.hotwords.index("WarmPriorController"),
                )
                # Cold session + workspace hotwords reuse the 60s cache (still only 1 scan total!)
                tracker.sync_session(
                    SessionMatch(cli="claude", session_id="cold-cache-1", jsonl_path=str(cold_path), cwd=str(repo)),
                    include_git_info=False,
                )
                hw = tracker.get_workspace_prior_hotwords(str(repo))
                self.assertIn("WarmPriorController", hw)
                self.assertEqual(m_disc_warm.call_count, 1)
            st_w = tracker.get_state(snap_w.session_key)
            self.assertTrue(st_w.cold_start_prior_retired)
            self.assertEqual(st_w.cold_start_prior_chunk, "")

            # cold_start_fallback="off" still performs 0 calls to discover_cli_prior_sessions
            tracker_off = SessionTracker(home_dir=home, proc_root=None, env=env, cold_start_fallback="off")
            with patch("agent_voice_input.session_ctx.discover_cli_prior_sessions") as m_disc_off:
                snap_off = tracker_off.sync_session(
                    SessionMatch(cli="claude", session_id="already-warm", jsonl_path=str(warm_path), cwd=str(repo)),
                    include_git_info=False,
                )
                m_disc_off.assert_not_called()
            self.assertFalse(snap_off.prior_hotwords_used)
            self.assertNotIn("WarmPriorController", snap_off.hotwords)
            tracker_off.close()
            tracker.close()

    def test_single_hop_sets_prior_session_ctx_fallback_and_openai_compat_refiner_wires_flag(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            repo = home / "repos" / "shproj"
            repo.mkdir(parents=True, exist_ok=True)
            now = time.time()
            _write_claude_jsonl(home, str(repo), "prior-sh", _make_rich_turns("OmniPrior"), mtime=now - 60)
            cur_path = _write_claude_jsonl(home, str(repo), "cur-sh", [], mtime=now - 5)
            env = {"HOME": str(home), "WSL_DISTRO_NAME": "", "WSL_INTEROP": "", "DEEPSEEK_API_KEY": "sk-test-1234"}
            match_cur = SessionMatch(cli="claude", session_id="cur-sh", jsonl_path=str(cur_path), cwd=str(repo))

            # 1) Single-hop ASR path
            cfg_sh = Config(refiner=RefinerConfig(provider="off", cold_start_fallback="auto"))
            tracker_sh = SessionTracker(home_dir=home, proc_root=None, env=env)
            asr_sh = _CaptureAsr(text="omni clean", single_hop=True)
            with patch.object(tracker_sh, "resolve_session", return_value=match_cur):
                res_sh = run_pipeline_once(
                    _make_voiced_wav(),
                    cfg=cfg_sh,
                    tracker=tracker_sh,
                    asr_provider=asr_sh,
                    origin_window=ForegroundWindow(1, "Ghostty", "claude", cwd=str(repo), bundle_id="com.mitchellh.ghostty"),
                    env=env,
                    dry_run=True,
                )
            self.assertTrue(res_sh.ok)
            self.assertEqual(res_sh.ctx_fallback, "prior_session")
            self.assertTrue(asr_sh.seen_contexts[0].context_blocks[0].startswith("<prior_session_reference"))
            tracker_sh.close()

            # 2) OpenAICompatibleRefiner sets used_prior_session=True on RefinerResult
            cfg_oai = Config(refiner=RefinerConfig(provider="deepseek", model="deepseek-flash", cold_start_fallback="auto"))
            tracker_oai = SessionTracker(home_dir=home, proc_root=None, env=env)
            snap = tracker_oai.sync_session(match_cur, include_git_info=False)
            seen_payloads: list[dict] = []

            def _fake_transport(req):
                seen_payloads.append(json.loads(req.body.decode("utf-8")))
                return HttpResponse(
                    status=200,
                    headers={},
                    body=json.dumps({"choices": [{"message": {"content": "clean output"}}]}).encode("utf-8"),
                )

            ref_oai = OpenAICompatibleRefiner(cfg_oai, env=env, transport=_fake_transport)
            r_res = ref_oai.refine("raw draft", session_key=snap.session_key, tracker=tracker_oai)
            self.assertTrue(r_res.used_prior_session)
            self.assertIn("<prior_session_reference", seen_payloads[0]["messages"][1]["content"])

            # 3) Antigravity IDE cold-start session borrows ONLY from IDE brain (never CLI)
            _write_agy_jsonl(home, str(repo), "ide-prior-1", _make_rich_turns("IdeBrainOnly"), ide=True, mtime=now - 500)
            cur_ide_tp = _write_agy_jsonl(home, str(repo), "ide-new-cold", [], ide=True, mtime=now - 2)
            ide_match = SessionMatch(
                cli="agy", session_id="ide-new-cold", jsonl_path=str(cur_ide_tp), cwd=str(repo), matched_by="ide_brain"
            )
            snap_ide = tracker_oai.sync_ide_session(ide_match, include_git_info=False)
            self.assertTrue(snap_ide.prior_hotwords_used)
            self.assertIn("IdeBrainOnlyController", snap_ide.hotwords)
            self.assertNotIn("OmniPriorController", snap_ide.hotwords)
            ide_blocks, _ = build_refiner_context_chunks(cfg_oai, session_key=snap_ide.session_key, tracker=tracker_oai)
            self.assertEqual(len(ide_blocks), 1)
            self.assertIn("IdeBrainOnlyController", ide_blocks[0])
            self.assertNotIn("OmniPriorController", ide_blocks[0])
            tracker_oai.close()

            # 4) Pipeline summary records ctx_fallback enum cleanly
            summary_sh = build_pipeline_summary(1, res_sh)
            self.assertEqual(summary_sh["ctx_fallback"], "prior_session")


class TestColdStartSshRemoteAndDiagnose(unittest.TestCase):
    def test_remote_probe_subprocess_single_rtt_prior_turns_and_tracker_integration(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "rhome"
            repo = home / "repos" / "rproj"
            repo.mkdir(parents=True, exist_ok=True)
            now = time.time()

            # Rich historical session on remote host (90 days old, different CLI)
            _write_codex_jsonl(
                home, str(repo), "rem-prior-cx", _make_rich_turns("RemoteKernel", pairs=2, pad_chars=900),
                date_path="2026/08/10", mtime=now - 90 * 86400,
            )
            # Current empty remote Claude session targeted by title token
            cur_sid = "11111111-2222-3333-4444-555555555555"
            _write_claude_jsonl(home, str(repo), cur_sid, [], mtime=now - 5)

            # End-to-end standalone subprocess execution of _remote_probe.py (exact SSH stdin/stdout wire format!)
            req = {
                "v": 1,
                "token": cur_sid,
                "cli": "claude",
                "explicit_cli": True,
                "title": f"claude [{cur_sid}]",
                "cwd_hint": str(repo),
                "etag": "",
                "max_chars": 200000,
                "include_git": False,
                "include_repo_files": False,
                "cold_start_fallback": "auto",
            }
            probe_stdin = build_probe_stdin_payload(req)
            proc = subprocess.run(
                [sys.executable, "-"],
                input=probe_stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={"HOME": str(home), "AVI_REMOTE_PROC_ROOT": str(Path(td) / "no_proc")},
                timeout=15,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", errors="replace"))
            decoded = decode_probe_stdout(proc.stdout)
            self.assertTrue(decoded["ok"])
            self.assertEqual(decoded["turns"], [])
            self.assertGreaterEqual(len(decoded["prior_turns"]), 4)
            self.assertTrue(any("RemoteKernelController" in t for _, t in decoded["prior_turns"]))

            # When cold_start_fallback="off", remote probe returns NO prior_turns
            req_off = {**req, "cold_start_fallback": "off"}
            resp_off = rp_mod.run_probe(req_off, env={"HOME": str(home)}, home_dir=home, proc_root=Path(td) / "no_proc")
            self.assertTrue(resp_off["ok"])
            self.assertNotIn("prior_turns", resp_off)

            # Schema validation rejects oversized / malformed prior_turns
            with self.assertRaises(ValueError):
                _validate_probe_response_schema(
                    {"ok": True, "cli": "claude", "session_id": cur_sid, "turns": [], "prior_turns": [["user", "x" * 31000]]}
                )
            with self.assertRaises(ValueError):
                _validate_probe_response_schema(
                    {"ok": True, "cli": "claude", "session_id": cur_sid, "turns": [], "prior_turns": [["badrole", "hi"]]}
                )

            # Local SessionTracker consumes remote prior_turns, freezes block 0, and sends cold_start_fallback="off" next time
            hcfg = RemoteHostConfig(name="devbox", ssh_target="devbox")
            tracker = SessionTracker(
                home_dir=home,
                proc_root=None,
                env={"HOME": str(home)},
                remote_cfg=RemoteConfig(hosts=(hcfg,)),
                cold_start_fallback="auto",
            )
            st = tracker._apply_remote_probe_response(hcfg, decoded, req_max_chars=200000)
            self.assertIsNotNone(st)
            self.assertTrue(st.cold_start_prior_checked)
            self.assertIn("RemoteKernelController", st.cold_start_prior_chunk)
            self.assertIn("RemoteKernelController", st.cold_start_prior_hotwords)

            # Next probe request for same session keeps cold_start_fallback="auto" and passes prior_checked_session_id
            next_req, _ = tracker._build_remote_req_and_key(
                hcfg, effective_title=f"claude [#{cur_sid[:8]}]", cwd_hint=str(repo), include_git_info=False
            )
            self.assertEqual(next_req["cold_start_fallback"], "auto")
            self.assertEqual(next_req["prior_checked_session_id"], cur_sid)

            # Remote probe skips prior discovery for the already-checked session_id (0ms overhead)...
            resp_same_checked = rp_mod.run_probe(
                {**req, "prior_checked_session_id": cur_sid},
                env={"HOME": str(home)},
                home_dir=home,
                proc_root=Path(td) / "no_proc",
            )
            self.assertTrue(resp_same_checked["ok"])
            self.assertNotIn("prior_turns", resp_same_checked)
            self.assertNotIn("prior_hotwords", resp_same_checked)

            # ...but when a NEW session B opens in the same directory, prior_checked_session_id (A) does NOT skip B!
            new_sid_b = "66666666-7777-8888-9999-aaaaaaaaaaaa"
            _write_claude_jsonl(home, str(repo), new_sid_b, [], mtime=now - 1)
            resp_new_b = rp_mod.run_probe(
                {
                    **req,
                    "token": new_sid_b,
                    "title": f"claude [{new_sid_b}]",
                    "prior_checked_session_id": cur_sid,
                },
                env={"HOME": str(home)},
                home_dir=home,
                proc_root=Path(td) / "no_proc",
            )
            self.assertTrue(resp_new_b["ok"])
            self.assertEqual(resp_new_b["session_id"], new_sid_b)
            self.assertIn("prior_turns", resp_new_b)
            self.assertIn("RemoteKernelController", resp_new_b.get("prior_hotwords", []))

            # Remote eviction naturally retires frozen prior chunk
            evict_resp = {
                **decoded,
                "etag": "new-etag",
                "turns": [("user", "turn 1"), ("assistant", "reply 1")],
                "evicted_chunks": 1,
                "prior_turns": [],
            }
            tracker._apply_remote_probe_response(hcfg, evict_resp, req_max_chars=200000)
            prior_after, _, _ = tracker.get_refiner_context_with_prior(st.session_key, max_chars=200000)
            self.assertEqual(prior_after, "")
            self.assertTrue(st.cold_start_prior_retired)
            tracker.close()

    def test_diagnose_exposes_ctx_fallback_enum_and_blocks_prose_or_path_leakage(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rt_dir = Path(td) / "rt"
            rt_dir.mkdir(parents=True, exist_ok=True)
            cfg = Config(refiner=RefinerConfig(provider="off"))
            env = {"HOME": td}

            # Verify build_pipeline_summary + _sanitize_diag_record allowlist
            clean_ok = _sanitize_diag_record(
                {"ok": True, "ctx_fallback": "prior_session", "secret_prior_text": "SENSITIVE_PROSE"},
                lambda s: s,
            )
            self.assertEqual(clean_ok["ctx_fallback"], "prior_session")
            self.assertNotIn("secret_prior_text", clean_ok)

            # Non-enum / path / prose stuffed into ctx_fallback MUST be sanitized to ""
            clean_bad = _sanitize_diag_record(
                {"ok": True, "ctx_fallback": "/home/secret/repo/private.jsonl"},
                lambda s: s,
            )
            self.assertEqual(clean_bad["ctx_fallback"], "")

            # End-to-end diag.jsonl -> collect_live_diagnostics
            tel_path = rt_dir / "diag.jsonl"
            rec_line = json.dumps(
                {
                    "ts": time.time(),
                    "seq_id": 1,
                    "ok": True,
                    "mode": "agent",
                    "session_key": "claude:abc12345",
                    "ctx_fallback": "prior_hotwords",
                    "prior_prose": "NEVER_LEAK_THIS_TEXT",
                }
            )
            tel_path.write_text(rec_line + "\n", encoding="utf-8")
            diag = collect_live_diagnostics(cfg, env, runtime_dir=rt_dir, last=5)
            self.assertEqual(diag["recent_results"][0]["ctx_fallback"], "prior_hotwords")
            self.assertNotIn("NEVER_LEAK_THIS_TEXT", json.dumps(diag))

    def test_extract_prior_sessions_hotwords_frequency_ranking(self) -> None:
        s1 = [("user", "Check AlphaBus and UniqueOne in main.py")]
        s2 = [("assistant", "AlphaBus handles BetaQueue and AlphaBus retries")]
        s3 = [("user", "AlphaBus plus GammaWorker")]
        hw = extract_prior_sessions_hotwords([s1, s2, s3], max_words=5)
        self.assertEqual(hw[0], "AlphaBus")
        self.assertIn("BetaQueue", hw)
        self.assertEqual(extract_prior_sessions_hotwords([], max_words=5), [])
        self.assertEqual(rp_mod.slice_prior_turns_tail([("user", "a" * 500)], max_chars=100), [("user", "a" * 90)])


class TestColdStartAstraReviewFixes(unittest.TestCase):
    def test_p1_explicit_exact_cwd_evidence_for_agy_ide_and_claude_dir_collision(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            proj = home / "repos" / "proj"
            child = proj / "child"
            proj.mkdir(parents=True, exist_ok=True)
            child.mkdir(parents=True, exist_ok=True)

            # 1) Antigravity IDE brain with ONLY L2/L3 paths inside /proj/child (no workspaceUris / sys_cwds)
            #    MUST NOT be borrowed as prior for /proj!
            logs_dir = home / ".gemini" / "antigravity" / "brain" / "ide-l3-only" / ".system_generated" / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            tp = logs_dir / "transcript_full.jsonl"
            child_file = str(child / "worker.py")
            l3_lines = [
                json.dumps({"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": f"Check {child_file} " + ("u" * 1200)}),
                json.dumps(
                    {
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "content": "Done editing " + ("a" * 1200),
                        "tool_calls": [
                            {"args": {"path": child_file}},
                            {"args": {"targetfile": str(child / "helper.py")}},
                        ],
                    }
                ),
            ]
            tp.write_text("\n".join(l3_lines) + "\n", encoding="utf-8")
            self.assertEqual(discover_antigravity_ide_prior_sessions(str(proj), home=home, wsl=False), [])

            # 2) Claude Code encoded dir collision (/a-b vs /a/b both encode to -a-b):
            #    JSONL missing explicit cwd MUST NOT be borrowed when exact_only=True!
            dir_dash = home / "a-b"
            dir_slash = home / "a" / "b"
            dir_dash.mkdir(parents=True, exist_ok=True)
            dir_slash.mkdir(parents=True, exist_ok=True)
            enc = rp_mod.encode_claude_project_dir(str(dir_dash))[0]
            self.assertEqual(enc, rp_mod.encode_claude_project_dir(str(dir_slash))[0])
            pdir = home / ".claude" / "projects" / enc
            pdir.mkdir(parents=True, exist_ok=True)
            no_cwd_file = pdir / "nocwd-session.jsonl"
            no_cwd_lines = [
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": "nocwd-session",
                        "isSidechain": False,
                        "message": {"role": "user", "content": "CollisionSecretToken " + ("x" * 1500)},
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "sessionId": "nocwd-session",
                        "isSidechain": False,
                        "message": {"role": "assistant", "content": "Reply " + ("y" * 1500)},
                    }
                ),
            ]
            no_cwd_file.write_text("\n".join(no_cwd_lines) + "\n", encoding="utf-8")
            self.assertEqual(rp_mod.discover_cli_prior_sessions(str(dir_slash), home=home, wsl=False), [])

    def test_p1_unknown_title_hint_checks_window_descendant_ssh_before_returning_none(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            repo = home / "repos" / "localproj"
            repo.mkdir(parents=True, exist_ok=True)
            (repo / "local_secret.py").write_text("x = 1\n", encoding="utf-8")
            _write_claude_jsonl(home, str(repo), "local-prior", _make_rich_turns("LocalSecretToken"))

            # Build fake /proc with window PID 5000 -> descendant interactive `ssh unlisted-host` PID 5001
            proc_root = Path(td) / "proc"
            p5000 = proc_root / "5000"
            p5001 = proc_root / "5001"
            p5000.mkdir(parents=True, exist_ok=True)
            p5001.mkdir(parents=True, exist_ok=True)
            (p5000 / "comm").write_text("ghostty\n", encoding="utf-8")
            (p5000 / "cmdline").write_bytes(b"ghostty\x00")
            (p5000 / "stat").write_text("5000 (ghostty) S 1 5000 5000 0 5000\n", encoding="utf-8")
            (p5001 / "comm").write_text("ssh\n", encoding="utf-8")
            (p5001 / "cmdline").write_bytes(b"ssh\x00unlisted-host\x00")
            (p5001 / "stat").write_text("5001 (ssh) S 5000 5001 5000 0 5001\n", encoding="utf-8")

            env = {"HOME": str(home), "WSL_DISTRO_NAME": "", "WSL_INTEROP": ""}
            cfg = Config(refiner=RefinerConfig(provider="auto", cold_start_fallback="auto"))
            tracker = SessionTracker(home_dir=home, proc_root=proc_root, env=env)
            asr = _CaptureAsr()
            ref = _CaptureRefiner(cfg)

            # Title has unknown session tag [#deadbeef] (no [@host]), no tty, has window_pid=5000 + local win.cwd
            win_ssh = ForegroundWindow(
                1, "Ghostty", "claude [#deadbeef]", cwd=str(repo), pid=5000, bundle_id="com.mitchellh.ghostty"
            )
            res_ssh = run_pipeline_once(
                _make_voiced_wav(),
                cfg=cfg,
                tracker=tracker,
                asr_provider=asr,
                refiner=ref,
                origin_window=win_ssh,
                env=env,
                dry_run=True,
            )
            self.assertTrue(tracker.last_sync_remote_vetoed)
            self.assertEqual(res_ssh.ctx_fallback, "")
            self.assertEqual(ref.seen_calls[0][3], "")
            self.assertNotIn("LocalSecretTokenController", asr.seen_contexts[0].hotwords)

            # Without ssh child (PID 6000 is just a local shell), unknown tag still allows Layer 0!
            p6000 = proc_root / "6000"
            p6000.mkdir(parents=True, exist_ok=True)
            (p6000 / "comm").write_text("zsh\n", encoding="utf-8")
            (p6000 / "cmdline").write_bytes(b"zsh\x00")
            (p6000 / "stat").write_text("6000 (zsh) S 1 6000 6000 0 6000\n", encoding="utf-8")
            asr_loc = _CaptureAsr()
            ref_loc = _CaptureRefiner(cfg)
            win_loc = ForegroundWindow(
                2, "Ghostty", "claude [#deadbeef]", cwd=str(repo), pid=6000, bundle_id="com.mitchellh.ghostty"
            )
            res_loc = run_pipeline_once(
                _make_voiced_wav(),
                cfg=cfg,
                tracker=tracker,
                asr_provider=asr_loc,
                refiner=ref_loc,
                origin_window=win_loc,
                env=env,
                dry_run=True,
            )
            self.assertFalse(tracker.last_sync_remote_vetoed)
            self.assertEqual(res_loc.ctx_fallback, "prior_hotwords")
            self.assertEqual(ref_loc.seen_calls[0][3], str(repo.resolve()))
            self.assertIn("LocalSecretTokenController", asr_loc.seen_contexts[0].hotwords)
            tracker.close()

    def test_p1_remote_hotwords_mode_and_workspace_cli_never_return_prior_turns(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "rhome"
            repo = home / "repos" / "rboundary"
            repo.mkdir(parents=True, exist_ok=True)
            now = time.time()
            # Two historical sessions so Top-5 aggregation across multiple sessions is verified
            _write_claude_jsonl(home, str(repo), "hist-1", _make_rich_turns("AlphaBus"), mtime=now - 100)
            _write_codex_jsonl(home, str(repo), "hist-2", _make_rich_turns("BetaQueue"), mtime=now - 50)
            cur_sid = "22222222-3333-4444-5555-666666666666"
            _write_claude_jsonl(home, str(repo), cur_sid, [], mtime=now - 5)

            # 1) hotwords mode on real Agent session -> returns prior_hotwords from Top-5, NEVER prior_turns!
            resp_hw = rp_mod.run_probe(
                {
                    "v": 1,
                    "token": cur_sid,
                    "cli": "claude",
                    "explicit_cli": True,
                    "cwd_hint": str(repo),
                    "cold_start_fallback": "hotwords",
                    "include_git": False,
                    "include_repo_files": False,
                },
                env={"HOME": str(home)},
                home_dir=home,
                proc_root=Path(td) / "no_proc",
            )
            self.assertTrue(resp_hw["ok"])
            self.assertNotIn("prior_turns", resp_hw)
            self.assertIn("AlphaBusController", resp_hw.get("prior_hotwords", []))
            self.assertIn("BetaQueueController", resp_hw.get("prior_hotwords", []))
            validated_hw = _validate_probe_response_schema(resp_hw)
            self.assertEqual(validated_hw["prior_turns"], [])
            self.assertIn("AlphaBusController", validated_hw["prior_hotwords"])

            # 2) auto mode hitting cli == "workspace" (pure directory fallback) -> NEVER returns prior_turns!
            #    Simulate workspace-only resolution via a clean directory + mocked _resolve_remote_session returning workspace
            ws_hit = {
                "cli": "workspace",
                "session_id": "cwd-deadbeef1234",
                "jsonl_path": "",
                "cwd": str(repo),
                "matched_by": "remote_cwd_only",
                "pts_mtime": 0.0,
                "jsonl_mtime": 0.0,
                "thread_name": "",
            }
            with patch.object(rp_mod, "_resolve_remote_session", return_value=(ws_hit, "")):
                resp_ws = rp_mod.run_probe(
                    {
                        "v": 1,
                        "cwd_hint": str(repo),
                        "cold_start_fallback": "auto",
                        "include_git": False,
                        "include_repo_files": False,
                    },
                    env={"HOME": str(home)},
                    home_dir=home,
                    proc_root=Path(td) / "no_proc",
                )
            self.assertTrue(resp_ws["ok"])
            self.assertEqual(resp_ws["cli"], "workspace")
            self.assertNotIn("prior_turns", resp_ws)
            self.assertIn("AlphaBusController", resp_ws.get("prior_hotwords", []))

            # Even if a spoofed/legacy workspace response carried prior_turns, local SessionTracker ignores Layer 2!
            hcfg = RemoteHostConfig(name="devbox", ssh_target="devbox")
            tracker = SessionTracker(home_dir=home, proc_root=None, env={"HOME": str(home)}, cold_start_fallback="auto")
            ws_st = tracker._apply_remote_probe_response(
                hcfg,
                {**validated_hw, "cli": "workspace", "session_id": "cwd-deadbeef1234", "prior_turns": [("user", "secret")]},
                req_max_chars=200000,
            )
            self.assertIsNotNone(ws_st)
            self.assertEqual(ws_st.cold_start_prior_chunk, "")
            self.assertIn("AlphaBusController", ws_st.cold_start_prior_hotwords)
            tracker.close()

    def test_p1_xml_wrapper_overhead_exact_boundaries_for_prior_only_and_prior_plus_current(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            repo = home / "repos" / "wrapproj"
            repo.mkdir(parents=True, exist_ok=True)
            now = time.time()
            _write_claude_jsonl(home, str(repo), "prior-wrap", _make_rich_turns("WrapBound", pairs=3, pad_chars=1200), mtime=now - 100)
            cur_path = _write_claude_jsonl(home, str(repo), "cur-wrap", [], mtime=now - 5)
            env = {"HOME": str(home), "WSL_DISTRO_NAME": "", "WSL_INTEROP": ""}

            # 1) Prior-only boundary: context_max_chars = 3000 -> wrapped <prior_session_reference> MUST be <= 3000!
            cfg_tight = Config(refiner=RefinerConfig(provider="auto", context_max_chars=3000, cold_start_fallback="auto"))
            tracker = SessionTracker(home_dir=home, proc_root=None, env=env, context_max_chars=3000)
            match_cur = SessionMatch(cli="claude", session_id="cur-wrap", jsonl_path=str(cur_path), cwd=str(repo))
            snap = tracker.sync_session(match_cur, include_git_info=False)
            blocks, _ = build_refiner_context_chunks(cfg_tight, session_key=snap.session_key, tracker=tracker)
            self.assertEqual(len(blocks), 1)
            wrapped_prior_len = len(blocks[0])
            self.assertLessEqual(wrapped_prior_len, 3000)

            # Exact max_chars == wrapped_prior_len keeps prior; max_chars == wrapped_prior_len - 1 retires prior!
            cfg_exact = Config(refiner=RefinerConfig(provider="auto", context_max_chars=wrapped_prior_len, cold_start_fallback="auto"))
            blocks_exact, _ = build_refiner_context_chunks(cfg_exact, session_key=snap.session_key, tracker=tracker)
            self.assertEqual(len(blocks_exact), 1)
            self.assertEqual(len(blocks_exact[0]), wrapped_prior_len)

            cfg_minus1 = Config(refiner=RefinerConfig(provider="auto", context_max_chars=wrapped_prior_len - 1, cold_start_fallback="auto"))
            blocks_over, _ = build_refiner_context_chunks(cfg_minus1, session_key=snap.session_key, tracker=tracker)
            self.assertEqual(blocks_over, [])
            self.assertTrue(tracker.get_state(snap.session_key).cold_start_prior_retired)

            # 2) Prior + current block exact boundary: sum(len(b)) <= max_chars kept, +1 char retires prior!
            tracker2 = SessionTracker(home_dir=home, proc_root=None, env=env, context_max_chars=3000)
            cur_path2 = _write_claude_jsonl(home, str(repo), "cur-wrap-2", [("user", "HelloCurrentTurn")], mtime=now - 2)
            match_cur2 = SessionMatch(cli="claude", session_id="cur-wrap-2", jsonl_path=str(cur_path2), cwd=str(repo))
            snap2 = tracker2.sync_session(match_cur2, include_git_info=False)
            st2 = tracker2.get_state(snap2.session_key)
            cur_body_len = len("[Speaker]\nHelloCurrentTurn")
            exact_both = (
                len(st2.cold_start_prior_chunk)
                + rp_mod.PRIOR_SESSION_WRAPPER_OVERHEAD_CHARS
                + cur_body_len
                + rp_mod.SESSION_REF_WRAPPER_OVERHEAD_CHARS
                + 2  # "\n\n" separator between wrapped blocks
            )
            cfg_both_ok = Config(refiner=RefinerConfig(provider="auto", context_max_chars=exact_both, cold_start_fallback="auto"))
            blocks_both, _ = build_refiner_context_chunks(cfg_both_ok, session_key=snap2.session_key, tracker=tracker2)
            self.assertEqual(len(blocks_both), 2)
            self.assertEqual(len("\n\n".join(blocks_both)), exact_both)

            cfg_both_over = Config(refiner=RefinerConfig(provider="auto", context_max_chars=exact_both - 1, cold_start_fallback="auto"))
            blocks_evict, _ = build_refiner_context_chunks(cfg_both_over, session_key=snap2.session_key, tracker=tracker2)
            self.assertEqual(len(blocks_evict), 1)
            self.assertFalse(blocks_evict[0].startswith("<prior_session_reference"))
            self.assertTrue(st2.cold_start_prior_retired)
            tracker.close()
            tracker2.close()

    def test_round2_p1_unrelated_tmux_client_never_misbinds_or_blocks_window_ssh_check(self) -> None:
        from agent_voice_input._tmux import TmuxClientInfo, TmuxPaneInfo, TmuxServerSnapshot

        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            repo = home / "repos" / "winproj"
            repo.mkdir(parents=True, exist_ok=True)
            (repo / "win_mod.py").write_text("x = 1\n", encoding="utf-8")
            _write_claude_jsonl(home, str(repo), "prior-win", _make_rich_turns("WinScopeToken"))

            # Fake /proc:
            # - PID 7000: unrelated tmux client (pane running ssh other-box!)
            # - PID 8000: focused local Ghostty window (just zsh, NO ssh!)
            # - PID 9000: another focused Ghostty window running its OWN descendant ssh secret-box!
            proc_root = Path(td) / "proc"
            for pid, ppid, comm, cmdline in (
                (7000, 1, "tmux", b"tmux\x00attach\x00"),
                (7001, 7000, "ssh", b"ssh\x00other-remote\x00"),
                (8000, 1, "ghostty", b"ghostty\x00"),
                (8001, 8000, "zsh", b"zsh\x00"),
                (9000, 1, "ghostty", b"ghostty\x00"),
                (9001, 9000, "ssh", b"ssh\x00secret-remote\x00"),
            ):
                pdir = proc_root / str(pid)
                pdir.mkdir(parents=True, exist_ok=True)
                (pdir / "comm").write_text(comm + "\n", encoding="utf-8")
                (pdir / "cmdline").write_bytes(cmdline)
                (pdir / "stat").write_text(f"{pid} ({comm}) S {ppid} {pid} {ppid} 0 {pid}\n", encoding="utf-8")

            env = {"HOME": str(home), "WSL_DISTRO_NAME": "", "WSL_INTEROP": ""}
            cfg = Config(refiner=RefinerConfig(provider="auto", cold_start_fallback="auto"))

            # Case A: Sole attached tmux client (PID 7000) is running SSH, but focused window is PID 8000 (local zsh)!
            snap_rem_tmux = TmuxServerSnapshot(
                clients=(
                    TmuxClientInfo(
                        socket_path="/tmp/t.sock", client_tty="/dev/pts/99", client_pid=7000,
                        session_id="$0", window_id="@0", pane_id="%0", activity=100,
                    ),
                ),
                panes=(
                    TmuxPaneInfo(
                        socket_path="/tmp/t.sock", session_id="$0", window_id="@0", pane_id="%0",
                        pane_pid=7001, pane_tty="/dev/pts/98", pane_title="other [@other-remote]",
                        current_path=str(repo), pane_active=True, window_active=True,
                    ),
                ),
            )
            tr_a = SessionTracker(
                home_dir=home, proc_root=proc_root, env=env, tmux_probe_fn=lambda: snap_rem_tmux
            )
            asr_a = _CaptureAsr()
            ref_a = _CaptureRefiner(cfg)
            res_a = run_pipeline_once(
                _make_voiced_wav(),
                cfg=cfg,
                tracker=tr_a,
                asr_provider=asr_a,
                refiner=ref_a,
                origin_window=ForegroundWindow(
                    1, "Ghostty", "claude [#deadbeef]", cwd=str(repo), pid=8000, bundle_id="com.mitchellh.ghostty"
                ),
                env=env,
                dry_run=True,
            )
            # Unrelated tmux SSH client MUST NOT veto focused local window 8000!
            self.assertFalse(tr_a.last_sync_remote_vetoed)
            self.assertEqual(res_a.ctx_fallback, "prior_hotwords")
            self.assertEqual(ref_a.seen_calls[0][3], str(repo.resolve()))
            tr_a.close()

            # Case B: Sole attached tmux client (PID 7000) is LOCAL, while focused window PID 9000 runs its OWN ssh!
            snap_loc_tmux = TmuxServerSnapshot(
                clients=(
                    TmuxClientInfo(
                        socket_path="/tmp/t.sock", client_tty="/dev/pts/99", client_pid=7000,
                        session_id="$0", window_id="@0", pane_id="%0", activity=100,
                    ),
                ),
                panes=(
                    TmuxPaneInfo(
                        socket_path="/tmp/t.sock", session_id="$0", window_id="@0", pane_id="%0",
                        pane_pid=8001, pane_tty="/dev/pts/98", pane_title="local-shell",
                        current_path=str(repo), pane_active=True, window_active=True,
                    ),
                ),
            )
            tr_b = SessionTracker(
                home_dir=home, proc_root=proc_root, env=env, tmux_probe_fn=lambda: snap_loc_tmux
            )
            asr_b = _CaptureAsr()
            ref_b = _CaptureRefiner(cfg)
            res_b = run_pipeline_once(
                _make_voiced_wav(),
                cfg=cfg,
                tracker=tr_b,
                asr_provider=asr_b,
                refiner=ref_b,
                origin_window=ForegroundWindow(
                    2, "Ghostty", "claude [#deadbeef]", cwd=str(repo), pid=9000, bundle_id="com.mitchellh.ghostty"
                ),
                env=env,
                dry_run=True,
            )
            # Unrelated local tmux client MUST NOT skip window 9000's descendant SSH check!
            self.assertTrue(tr_b.last_sync_remote_vetoed)
            self.assertEqual(res_b.ctx_fallback, "")
            self.assertEqual(ref_b.seen_calls[0][3], "")
            self.assertNotIn("WinScopeTokenController", asr_b.seen_contexts[0].hotwords)
            tr_b.close()

    def test_round2_p1_session_state_cwd_change_retires_old_dir_prior_and_hotwords(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            proj = home / "repos" / "proj"
            child = proj / "child"
            proj.mkdir(parents=True, exist_ok=True)
            child.mkdir(parents=True, exist_ok=True)
            now = time.time()

            _write_agy_jsonl(home, str(proj), "ide-prior-root", _make_rich_turns("RootPriorSym"), ide=True, mtime=now - 200)
            cur_tp = _write_agy_jsonl(home, str(proj), "ide-same-cid", [], ide=True, mtime=now - 5)

            env = {"HOME": str(home), "WSL_DISTRO_NAME": "", "WSL_INTEROP": ""}
            cfg = Config(refiner=RefinerConfig(provider="auto", cold_start_fallback="auto"))
            tracker = SessionTracker(home_dir=home, proc_root=None, env=env)

            # First sync in /proj -> freezes prior chunk & hotwords bound to /proj
            m_root = SessionMatch(
                cli="agy", session_id="ide-same-cid", jsonl_path=str(cur_tp), cwd=str(proj), matched_by="ide_brain"
            )
            snap1 = tracker.sync_ide_session(m_root, include_git_info=False)
            self.assertTrue(snap1.prior_hotwords_used)
            self.assertIn("RootPriorSymController", snap1.hotwords)
            blocks1, _ = build_refiner_context_chunks(cfg, session_key=snap1.session_key, tracker=tracker)
            self.assertEqual(len(blocks1), 1)
            self.assertIn("RootPriorSymController", blocks1[0])

            # Same IDE cid switches to /proj/child -> MUST retire old /proj frozen chunk & hotwords immediately!
            m_child = SessionMatch(
                cli="agy", session_id="ide-same-cid", jsonl_path=str(cur_tp), cwd=str(child), matched_by="ide_brain"
            )
            snap2 = tracker.sync_ide_session(m_child, include_git_info=False)
            self.assertFalse(snap2.prior_hotwords_used)
            self.assertNotIn("RootPriorSymController", snap2.hotwords)
            blocks2, _ = build_refiner_context_chunks(cfg, session_key=snap2.session_key, tracker=tracker)
            self.assertEqual(blocks2, [])
            st = tracker.get_state(snap2.session_key)
            self.assertTrue(st.cold_start_prior_retired)
            self.assertEqual(st.cold_start_prior_chunk, "")
            self.assertEqual(st.cold_start_prior_hotwords, ())
            tracker.close()

    def test_round2_p1_agy_cli_prior_never_infers_cwd_from_user_explicit_prose(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            repo = home / "repos" / "target"
            repo.mkdir(parents=True, exist_ok=True)

            # Craft an agy CLI transcript with NO cwd field and NO SYSTEM workspace header,
            # only a USER_EXPLICIT message pasting "Active Workspaces:\n- /.../target" from another repo's log!
            logs_dir = home / ".gemini" / "antigravity-cli" / "brain" / "pasted-log-cid" / ".system_generated" / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            tp = logs_dir / "transcript_full.jsonl"
            lines = [
                json.dumps(
                    {
                        "source": "USER_EXPLICIT",
                        "type": "USER_INPUT",
                        "content": f"Look at this pasted log:\nActive Workspaces:\n- {repo}\nPastedSecretSym " + ("u" * 1600),
                    }
                ),
                json.dumps(
                    {
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "content": "Analyzed pasted log " + ("a" * 1600),
                    }
                ),
            ]
            tp.write_text("\n".join(lines) + "\n", encoding="utf-8")

            # Strict system-only extraction returns "" and discover_cli_prior_sessions rejects it!
            self.assertEqual(rp_mod._extract_agy_cwd(tp, strict_system_only=True), "")
            self.assertEqual(rp_mod.discover_cli_prior_sessions(str(repo), home=home, wsl=False), [])

    def test_p2_shared_max_turns_limit_slim_cache_and_bounded_dir_eviction(self) -> None:
        # 1) slice_prior_turns_tail obeys COLD_START_PRIOR_MAX_TURNS (500) matching _validate_probe_response_schema
        many_turns = [("user" if i % 2 == 0 else "assistant", f"t{i}") for i in range(600)]
        sliced = rp_mod.slice_prior_turns_tail(many_turns, max_chars=25000)
        self.assertEqual(len(sliced), rp_mod.COLD_START_PRIOR_MAX_TURNS)
        validated = _validate_probe_response_schema(
            {"ok": True, "cli": "claude", "session_id": "abc12345", "turns": [], "prior_turns": [[r, t] for r, t in sliced]}
        )
        self.assertEqual(len(validated["prior_turns"]), rp_mod.COLD_START_PRIOR_MAX_TURNS)
        with self.assertRaises(ValueError):
            _validate_probe_response_schema(
                {
                    "ok": True,
                    "cli": "claude",
                    "session_id": "abc12345",
                    "turns": [],
                    "prior_turns": [["user", "a"]] * (rp_mod.COLD_START_PRIOR_MAX_TURNS + 1),
                }
            )

        # 2) Slimmed cache entries (no raw full "turns") and bounded 16-dir capacity in SessionTracker
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            env = {"HOME": str(home), "WSL_DISTRO_NAME": "", "WSL_INTEROP": ""}
            tracker = SessionTracker(home_dir=home, proc_root=None, env=env, prior_cache_ttl_sec=60.0)
            for idx in range(20):
                d = home / "repos" / f"proj_{idx:02d}"
                d.mkdir(parents=True, exist_ok=True)
                if idx == 0:
                    _write_claude_jsonl(home, str(d), "big-sess", _make_rich_turns("BigCache", pairs=25, pad_chars=1200))
                tracker._get_prior_candidates_cached(str(d), scope="cli")
            self.assertLessEqual(len(tracker._prior_cache), 16)

            cands = rp_mod.discover_cli_prior_sessions(str(home / "repos" / "proj_00"), home=home, wsl=False)
            self.assertEqual(len(cands), 1)
            self.assertNotIn("turns", cands[0])
            self.assertEqual(cands[0]["turn_count"], 50)
            self.assertLessEqual(len(cands[0]["hotword_counts"]), rp_mod.COLD_START_PRIOR_MAX_HOTWORDS)
            self.assertLessEqual(
                len(rp_mod.format_prior_turns_body(cands[0]["tail_turns"])),
                rp_mod.COLD_START_PRIOR_MAX_CHARS,
            )
            tracker.close()

    def test_p2_agy_ide_evidence_respects_wsl_mnt_symlink_guard(self) -> None:
        from agent_voice_input import workspace_ctx as ws_mod

        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            cid_dir = home / ".gemini" / "antigravity" / "brain" / "cid-wsl-guard"
            sys_gen = cid_dir / ".system_generated"
            logs_dir = sys_gen / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            tp = logs_dir / "transcript_full.jsonl"
            tp.write_text(
                json.dumps({"source": "SYSTEM", "type": "SYSTEM_MESSAGE", "cwd": str(home / "repos" / "ok")}) + "\n",
                encoding="utf-8",
            )
            # Symlink subagents -> /mnt/c/slow_mount_trap
            os.symlink("/mnt/c/slow_mount_trap", sys_gen / "subagents")

            real_stat = os.stat
            touched_mnt: list[str] = []

            def _guard_stat(path, *args, **kwargs):
                if "/mnt/" in str(path):
                    touched_mnt.append(str(path))
                return real_stat(path, *args, **kwargs)

            st_tp = tp.stat()
            with patch("os.stat", side_effect=_guard_stat):
                ev = ws_mod._load_agy_ide_evidence(cid_dir, tp, st_tp, home, wsl=True)
            self.assertEqual(touched_mnt, [])
            self.assertEqual(ev.ws_uris, ())
            self.assertEqual(ev.sys_cwds, (str(home / "repos" / "ok"),))

    def test_symlink_workspace_prior_discovery_across_realpath_and_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            home = base / "home"
            real_root = base / "data_vol"
            real_ws = real_root / "symproj"
            sub_ws = real_ws / "subpkg"
            other_ws = real_root / "otherproj"
            for d in (home / "repos", real_ws, sub_ws, other_ws):
                d.mkdir(parents=True, exist_ok=True)
            (real_ws / "sym_core.py").write_text("x = 1\n", encoding="utf-8")

            link_ws = home / "repos" / "symproj"
            link_ws2 = home / "work_alias"
            broad_link = home / "broad_home_link"
            os.symlink(real_ws, link_ws)
            os.symlink(real_ws, link_ws2)
            os.symlink(home, broad_link)

            now = time.time()
            # Prior Claude session recorded under logical symlink path `link_ws`
            # (exact mirror of macOS /var/folders -> /private/var/folders & user symlink workspaces)
            _write_claude_jsonl(
                home,
                str(link_ws),
                "prior-sym-claude",
                _make_rich_turns("SymPrior", pairs=2, pad_chars=900),
                mtime=now - 120,
            )
            # Decoy sessions in parent, child, sibling, and broad home: MUST NEVER match!
            _write_claude_jsonl(home, str(real_root), "decoy-parent", _make_rich_turns("ParentDecoy"), mtime=now - 10)
            _write_claude_jsonl(home, str(sub_ws), "decoy-child", _make_rich_turns("ChildDecoy"), mtime=now - 10)
            _write_claude_jsonl(home, str(other_ws), "decoy-other", _make_rich_turns("OtherDecoy"), mtime=now - 10)
            _write_claude_jsonl(home, str(home), "decoy-home", _make_rich_turns("HomeDecoy"), mtime=now - 10)

            # 1) Direct discovery succeeds from realpath, original symlink, AND second symlink alias
            for probe_cwd in (str(real_ws), str(link_ws), str(link_ws2)):
                cands = rp_mod.discover_cli_prior_sessions(probe_cwd, home=home, wsl=False)
                self.assertEqual([c["session_id"] for c in cands], ["prior-sym-claude"], probe_cwd)

            # Cross-directory & broad symlink isolation strictly preserved
            empty_other = real_root / "clean_other"
            empty_other.mkdir(parents=True, exist_ok=True)
            self.assertEqual(
                [c["session_id"] for c in rp_mod.discover_cli_prior_sessions(str(other_ws), home=home, wsl=False)],
                ["decoy-other"],
            )
            self.assertEqual(
                [c["session_id"] for c in rp_mod.discover_cli_prior_sessions(str(sub_ws), home=home, wsl=False)],
                ["decoy-child"],
            )
            self.assertEqual(
                [c["session_id"] for c in rp_mod.discover_cli_prior_sessions(str(real_root), home=home, wsl=False)],
                ["decoy-parent"],
            )
            self.assertEqual(rp_mod.discover_cli_prior_sessions(str(empty_other), home=home, wsl=False), [])
            self.assertEqual(rp_mod.discover_cli_prior_sessions(str(broad_link), home=home, wsl=False), [])

            # 2) Brand-new empty Claude session stored under REALPATH still finds prior stored under SYMLINK,
            #    and SessionTracker shares canonical cache across realpath & symlink queries (only 1 scan!)
            cur_real = _write_claude_jsonl(home, str(real_ws), "cur-real-cold", [], mtime=now - 2)
            env = {"HOME": str(home), "WSL_DISTRO_NAME": "", "WSL_INTEROP": ""}
            cfg = Config(refiner=RefinerConfig(provider="auto", cold_start_fallback="auto"))
            tracker = SessionTracker(home_dir=home, proc_root=None, env=env, prior_cache_ttl_sec=60.0)
            with patch(
                "agent_voice_input.session_ctx.discover_cli_prior_sessions",
                wraps=rp_mod.discover_cli_prior_sessions,
            ) as m_disc:
                snap = tracker.sync_session(
                    SessionMatch(
                        cli="claude", session_id="cur-real-cold", jsonl_path=str(cur_real), cwd=str(real_ws)
                    ),
                    include_git_info=False,
                )
                self.assertIsNotNone(snap)
                self.assertTrue(snap.prior_hotwords_used)
                self.assertIn("SymPriorController", snap.hotwords)
                self.assertNotIn("OtherDecoyController", snap.hotwords)
                blocks, _ = build_refiner_context_chunks(cfg, session_key=snap.session_key, tracker=tracker)
                self.assertEqual(len(blocks), 1)
                self.assertIn("SymPriorController", blocks[0])
                # Canonical cache hit across symlink & second alias (0 extra scans!)
                self.assertIn("SymPriorController", tracker.get_workspace_prior_hotwords(str(link_ws)))
                self.assertIn("SymPriorController", tracker.get_workspace_prior_hotwords(str(link_ws2)))
                self.assertEqual(m_disc.call_count, 1)

            # 3) Layer 0 terminal fallback (daemon resolves win.cwd -> realpath) finds symlink-stored prior
            asr_l0 = _CaptureAsr()
            ref_l0 = _CaptureRefiner(cfg)
            with patch.object(tracker, "resolve_session", return_value=None):
                res_l0 = run_pipeline_once(
                    _make_voiced_wav(),
                    cfg=cfg,
                    tracker=tracker,
                    asr_provider=asr_l0,
                    refiner=ref_l0,
                    origin_window=ForegroundWindow(
                        1, "Ghostty", "zsh", cwd=str(link_ws), bundle_id="com.mitchellh.ghostty"
                    ),
                    env=env,
                    dry_run=True,
                )
            self.assertEqual(res_l0.ctx_fallback, "prior_hotwords")
            self.assertIn("SymPriorController", asr_l0.seen_contexts[0].hotwords)
            self.assertEqual(ref_l0.seen_calls[0][3], str(real_ws.resolve()))

            # 4) Ordinary IDE (resolve_ide_workspace_cwd returns realpath) gets Layer 1 hotwords only
            asr_ide = _CaptureAsr()
            ref_ide = _CaptureRefiner(cfg)
            res_ide = run_pipeline_once(
                _make_voiced_wav(),
                cfg=cfg,
                tracker=tracker,
                asr_provider=asr_ide,
                refiner=ref_ide,
                origin_window=ForegroundWindow(
                    2, "Cursor", "sym_core.py — symproj — Cursor", bundle_id="com.todesktop.230313mzl4w4u92"
                ),
                env=env,
                dry_run=True,
            )
            self.assertEqual(res_ide.ctx_fallback, "prior_hotwords")
            self.assertIn("SymPriorController", asr_ide.seen_contexts[0].hotwords)
            self.assertEqual(ref_ide.seen_calls[0][0], [])
            tracker.close()

            # 5) Antigravity IDE prior stored under symlink is found symmetrically via realpath & alias
            _write_agy_jsonl(home, str(link_ws), "ide-sym-prior", _make_rich_turns("IdeSymPrior"), ide=True, mtime=now - 50)
            for probe_cwd in (str(real_ws), str(link_ws), str(link_ws2)):
                ide_cands = discover_antigravity_ide_prior_sessions(probe_cwd, home=home, wsl=False)
                self.assertEqual([c["session_id"] for c in ide_cands], ["ide-sym-prior"], probe_cwd)
            self.assertEqual(discover_antigravity_ide_prior_sessions(str(other_ws), home=home, wsl=False), [])

    def test_encode_claude_project_dir_macos_private_prefix_and_wsl_mnt_project_scan_guard(self) -> None:
        # 1) Deterministic macOS /private/(var|tmp|etc) reverse symlink encoding (runs on Linux too!)
        def _fake_macos_realpath(p: str, *, wsl: bool = False) -> str:
            if p in ("/var/folders/xx/repo", "/private/var/folders/xx/repo"):
                return "/private/var/folders/xx/repo"
            return p

        with patch.object(rp_mod, "_safe_realpath", side_effect=_fake_macos_realpath):
            encs_real = rp_mod.encode_claude_project_dir("/private/var/folders/xx/repo", wsl=False)
            self.assertEqual(encs_real, ["-private-var-folders-xx-repo", "-var-folders-xx-repo"])
            encs_link = rp_mod.encode_claude_project_dir("/var/folders/xx/repo", wsl=False)
            self.assertEqual(encs_link, ["-var-folders-xx-repo", "-private-var-folders-xx-repo"])

        # Non-macOS / distinct /private/var path MUST NOT fabricate an unrelated /var alias
        with patch.object(rp_mod, "_safe_realpath", side_effect=lambda p, *, wsl=False: p):
            self.assertEqual(
                rp_mod.encode_claude_project_dir("/private/var/folders/xx/repo", wsl=False),
                ["-private-var-folders-xx-repo"],
            )

        # 2) WSL2 zero-I/O /mnt/ guard during Claude project-dir alias scan
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            home = base / "home"
            real_ws = base / "real_wsl_proj"
            link_ws = home / "repos" / "wsl_link"
            real_ws.mkdir(parents=True, exist_ok=True)
            link_ws.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(real_ws, link_ws)

            _write_claude_jsonl(home, str(link_ws), "wsl-sym-ok", _make_rich_turns("WslSafeSym"), mtime=time.time() - 30)
            # Poison ~/.claude/projects with /mnt/ dir symlink, /mnt/ file symlink, and /mnt/ cwd JSONL
            projects_dir = home / ".claude" / "projects"
            os.symlink("/mnt/c/slow_mount_trap", projects_dir / "-mnt-trap-dir")
            other_pdir = projects_dir / "-other-proj"
            other_pdir.mkdir(parents=True, exist_ok=True)
            os.symlink("/mnt/c/slow_mount_trap/trap.jsonl", other_pdir / "trap.jsonl")
            (other_pdir / "mnt_cwd.jsonl").write_text(
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": "mnt-cwd-sess",
                        "cwd": "/mnt/c/Users/alice/repo",
                        "isSidechain": False,
                        "message": {"role": "user", "content": "x" * 1500},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            real_stat = os.stat
            real_lstat = os.lstat
            touched_mnt: list[str] = []

            def _guard_stat(path, *args, **kwargs):
                if "/mnt/" in str(path):
                    touched_mnt.append(f"stat:{path}")
                return real_stat(path, *args, **kwargs)

            def _guard_lstat(path, *args, **kwargs):
                if "/mnt/" in str(path):
                    touched_mnt.append(f"lstat:{path}")
                return real_lstat(path, *args, **kwargs)

            with patch("os.stat", side_effect=_guard_stat), patch("os.lstat", side_effect=_guard_lstat):
                cands = rp_mod.discover_cli_prior_sessions(str(real_ws), home=home, wsl=True)
            self.assertEqual(touched_mnt, [])
            self.assertEqual([c["session_id"] for c in cands], ["wsl-sym-ok"])

    def test_stage1_slot_purification_and_remote_warm_prior_hotwords(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            home = base / "home"
            repo = home / "repos" / "slotproj"
            repo.mkdir(parents=True, exist_ok=True)
            git_env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(home),
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "t@example.com",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "t@example.com",
            }
            subprocess.run(["git", "init", "-q"], cwd=str(repo), env=git_env, check=True)
            (repo / "base_init.py").write_text("x = 0\n", encoding="utf-8")
            subprocess.run(["git", "add", "base_init.py"], cwd=str(repo), env=git_env, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), env=git_env, check=True)

            # Create 15 dirty files so Stage 1 cap (10) vs Stage 2 full file list (<=30) is verified
            for idx in range(15):
                (repo / f"dirty_mod_{idx:02d}.py").write_text(f"v = {idx}\n", encoding="utf-8")

            now = time.time()
            _write_claude_jsonl(home, str(repo), "prior-slot-1", _make_rich_turns("PriorSlot"), mtime=now - 120)
            warm_turns = _make_rich_turns("ActiveTopic", pairs=3, pad_chars=500)
            cur_sid = "33333333-4444-5555-6666-777777777777"
            cur_path = _write_claude_jsonl(home, str(repo), cur_sid, warm_turns, mtime=now - 5)

            env = {"HOME": str(home), "PATH": git_env["PATH"], "WSL_DISTRO_NAME": "", "WSL_INTEROP": ""}
            tracker = SessionTracker(home_dir=home, proc_root=None, env=env, git_cache_ttl_sec=0.0)
            try:
                # 1) Local warm session: turn_hw BEFORE git_hw[:10] (no bare stem dupes) BEFORE prior_hw
                snap = tracker.sync_session(
                    SessionMatch(cli="claude", session_id=cur_sid, jsonl_path=str(cur_path), cwd=str(repo)),
                    include_git_info=True,
                )
                self.assertIsNotNone(snap)
                self.assertTrue(snap.prior_hotwords_used)
                self.assertIn("ActiveTopicController", snap.hotwords)
                self.assertIn("PriorSlotController", snap.hotwords)
                self.assertIn("dirty_mod_00.py", snap.hotwords)
                self.assertNotIn("dirty_mod_00", snap.hotwords)
                self.assertIn("dirty_mod_09.py", snap.hotwords)
                self.assertNotIn("dirty_mod_10.py", snap.hotwords)
                self.assertLess(snap.hotwords.index("ActiveTopicController"), snap.hotwords.index("dirty_mod_00.py"))
                self.assertLess(snap.hotwords.index("dirty_mod_00.py"), snap.hotwords.index("PriorSlotController"))

                # build_workspace_hotwords also compacts duplicates and caps to 10
                ws_hw = tracker.build_workspace_hotwords(str(repo))
                self.assertEqual(len(ws_hw), 10)
                self.assertIn("dirty_mod_00.py", ws_hw)
                self.assertNotIn("dirty_mod_00", ws_hw)
                self.assertNotIn("dirty_mod_10.py", ws_hw)

                # Stage 2 preserves full git_status & changed_files (>10) untouched!
                prior_blk, _, git_ctx = tracker.get_refiner_context_with_prior(snap.session_key, include_git_status=True)
                self.assertEqual(prior_blk, "")
                self.assertIn("dirty_mod_14.py", git_ctx)

                # 2) Remote probe on warm session (>= 4 turns): returns prior_hotwords (NOT prior_turns),
                #    and _read_remote_git_status emits basenames without bare stem duplicates!
                resp_rem = rp_mod.run_probe(
                    {
                        "v": 1,
                        "token": cur_sid,
                        "cli": "claude",
                        "explicit_cli": True,
                        "cwd_hint": str(repo),
                        "include_git": True,
                        "include_repo_files": False,
                        "cold_start_fallback": "auto",
                    },
                    env=env,
                    home_dir=home,
                    proc_root=base / "no_proc",
                )
                self.assertTrue(resp_rem["ok"])
                self.assertGreaterEqual(len(resp_rem["turns"]), 6)
                self.assertNotIn("prior_turns", resp_rem)
                self.assertIn("PriorSlotController", resp_rem.get("prior_hotwords", []))
                self.assertNotIn("ActiveTopicController", resp_rem.get("prior_hotwords", []))
                self.assertIn("dirty_mod_00.py", resp_rem["git_hotwords"])
                self.assertNotIn("dirty_mod_00", resp_rem["git_hotwords"])
                self.assertIn("dirty_mod_14.py", resp_rem["git_hotwords"])

                # Remote tracker consumes warm prior_hotwords, keeps them active >= 4 turns,
                # orders turn_hw > git_hw[:10] > prior_hw, and keeps full Stage 2 git_ctx!
                hcfg = RemoteHostConfig(name="devbox", ssh_target="devbox")
                rem_tracker = SessionTracker(
                    home_dir=home,
                    proc_root=None,
                    env=env,
                    remote_cfg=RemoteConfig(hosts=(hcfg,)),
                    cold_start_fallback="auto",
                )
                try:
                    with patch.object(rem_tracker.remote_manager, "fetch_with_budget", return_value=resp_rem):
                        rem_snap = rem_tracker._sync_remote_match(
                            SessionMatch(
                                cli="claude",
                                session_id=cur_sid,
                                jsonl_path="",
                                cwd=str(repo),
                                origin="remote",
                                host_name="devbox",
                            ),
                            window_title=f"claude [#{cur_sid[:8]}]",
                            include_git_info=True,
                        )
                    self.assertIsNotNone(rem_snap)
                    self.assertTrue(rem_snap.prior_hotwords_used)
                    self.assertIn("ActiveTopicController", rem_snap.hotwords)
                    self.assertIn("PriorSlotController", rem_snap.hotwords)
                    self.assertIn("dirty_mod_00.py", rem_snap.hotwords)
                    self.assertNotIn("dirty_mod_00", rem_snap.hotwords)
                    self.assertNotIn("dirty_mod_10.py", rem_snap.hotwords)
                    self.assertLess(
                        rem_snap.hotwords.index("ActiveTopicController"),
                        rem_snap.hotwords.index("dirty_mod_00.py"),
                    )
                    self.assertLess(
                        rem_snap.hotwords.index("dirty_mod_00.py"),
                        rem_snap.hotwords.index("PriorSlotController"),
                    )
                    rem_prior_blk, _, rem_git_ctx = rem_tracker.get_refiner_context_with_prior(
                        rem_snap.session_key, include_git_status=True
                    )
                    self.assertEqual(rem_prior_blk, "")
                    self.assertIn("dirty_mod_14.py", rem_git_ctx)
                finally:
                    rem_tracker.close()
            finally:
                tracker.close()


if __name__ == "__main__":
    unittest.main()
