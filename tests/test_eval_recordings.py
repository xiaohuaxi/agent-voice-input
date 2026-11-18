"""Offline unit tests for .agents/skills/voice-eval/scripts/eval_recordings.py."""
from __future__ import annotations

import array
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agent_voice_input.asr_providers import AsrResult  # noqa: E402
from agent_voice_input.audio_proc import pack_pcm16_mono_wav  # noqa: E402
from agent_voice_input.refiner import RefinerResult, build_refiner_request_blocks  # noqa: E402
from agent_voice_input.session_ctx import SessionMatch, SessionTracker  # noqa: E402


def _load_eval_module():
    script_path = REPO_ROOT / ".agents" / "skills" / "voice-eval" / "scripts" / "eval_recordings.py"
    spec = importlib.util.spec_from_file_location("eval_recordings_mod", script_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


EVAL = _load_eval_module()


def _make_voiced_wav(duration_sec: float = 3.0, freq_hz: float = 220.0) -> bytes:
    n = int(16000 * duration_sec)
    samples = array.array(
        "h",
        (int(6000 * math.sin(2.0 * math.pi * freq_hz * i / 16000.0)) for i in range(n)),
    )
    return pack_pcm16_mono_wav(samples, sample_rate=16000)


class PointInTimeTruncationTests(unittest.TestCase):
    def test_agy_truncation_excludes_self_and_future_turns_while_capturing_submitted_ref(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "transcript_full.jsonl"
            dst = root / "out" / "trunc.jsonl"
            records = [
                {
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "created_at": "2026-11-18T02:00:00Z",
                    "content": "<USER_REQUEST>先看一下 refiner.py 的缓存逻辑</USER_REQUEST>",
                },
                {
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-11-18T02:00:10Z",
                    "content": "已检查 SessionTracker 与 build_refiner_request_blocks。",
                },
                {
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "created_at": "2026-11-18T02:01:05Z",
                    "content": "<USER_REQUEST>SECRET_LEAK_SELF_UTTERANCE 顺便改一下超时</USER_REQUEST>",
                },
                {
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-11-18T02:01:20Z",
                    "content": "FUTURE_ASSISTANT_LEAK 已修改超时配置。",
                },
            ]
            src.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")

            # Utterance recorded from 02:00:30Z (cutoff) to 02:01:03Z (stop)
            cutoff_ts = EVAL.extract_jsonl_record_ts({"ts": "2026-11-18T02:00:30Z"})
            stop_ts = EVAL.extract_jsonl_record_ts({"ts": "2026-11-18T02:01:03Z"})
            assert cutoff_ts is not None and stop_ts is not None

            kept_n, submitted_ref, submitted_ts = EVAL.truncate_session_jsonl_point_in_time(
                src,
                cli="agy",
                cutoff_ts=cutoff_ts,
                stop_ts=stop_ts,
                dst_jsonl=dst,
            )
            self.assertEqual(kept_n, 2)
            self.assertEqual(stat.S_IMODE(dst.stat().st_mode), 0o600)
            self.assertEqual(submitted_ref, "SECRET_LEAK_SELF_UTTERANCE 顺便改一下超时")
            self.assertIsNotNone(submitted_ts)

            tracker = SessionTracker(home_dir=root, runtime_dir=root / "rt", proc_root=root / "no_proc", env={})
            snap = tracker.sync_session(
                match=SessionMatch(cli="agy", session_id="s1", jsonl_path=str(dst), cwd="", matched_by="test")
            )
            assert snap is not None
            self.assertEqual(len(snap.clean_turns), 2)
            joined = "\n".join(t for _, t in snap.clean_turns)
            self.assertIn("refiner.py", joined)
            self.assertNotIn("SECRET_LEAK_SELF_UTTERANCE", joined)
            self.assertNotIn("FUTURE_ASSISTANT_LEAK", joined)

    def test_codex_untimestamped_header_kept_but_post_cutoff_untimestamped_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "rollout-test.jsonl"
            dst = root / "trunc.jsonl"
            lines = [
                {"type": "session_meta", "payload": {"id": "cid-1", "source": "cli", "cwd": ""}},
                {
                    "timestamp": "2026-11-18T02:00:05Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "item": {"type": "UserMessage", "content": [{"type": "text", "text": "历史问题"}]},
                    },
                },
                {
                    "timestamp": "2026-11-18T02:02:00Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "item": {"type": "UserMessage", "content": [{"type": "text", "text": "当前录音提交稿"}]},
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "item": {"type": "AgentMessage", "content": [{"type": "text", "text": "无时间戳未来回复"}]},
                    },
                },
            ]
            src.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")
            cutoff_ts = EVAL.extract_jsonl_record_ts({"ts": "2026-11-18T02:01:30Z"})
            stop_ts = EVAL.extract_jsonl_record_ts({"ts": "2026-11-18T02:01:58Z"})
            kept_n, sub_ref, _ = EVAL.truncate_session_jsonl_point_in_time(
                src, cli="codex", cutoff_ts=cutoff_ts, stop_ts=stop_ts, dst_jsonl=dst
            )
            self.assertEqual(kept_n, 2)
            self.assertEqual(sub_ref, "当前录音提交稿")
            raw_trunc = dst.read_text(encoding="utf-8")
            self.assertNotIn("当前录音提交稿", raw_trunc)
            self.assertNotIn("无时间戳未来回复", raw_trunc)


class MetricsAndReplayTests(unittest.TestCase):
    def test_sample_and_aggregate_metrics_and_redacted_compare(self) -> None:
        m_bad = EVAL.evaluate_sample_metrics(
            "呃那个 To B X 里面的 Lula 模型还有 refiner 点 py 改一下，对，主要就是这些。",
            "To B X 里面的 Lula 模型还有 refiner 点 py 改一下。嗯。对，主要就是这些。",
            session_hotwords=("SessionTracker",),
        )
        self.assertGreaterEqual(m_bad["homophone_error_hits"], 3)
        self.assertGreaterEqual(m_bad["filler_residue_hits"], 2)
        self.assertFalse(m_bad["has_newline"])

        m_good = EVAL.evaluate_sample_metrics(
            "呃那个 To B X 里面的 Lula 模型还有 refiner 点 py 改一下，对，主要就是这些。",
            "把 Codex 里的 Luna 模型和 refiner.py 改一下。\n顺便对齐 SessionTracker。",
            session_hotwords=("SessionTracker",),
        )
        self.assertEqual(m_good["homophone_error_hits"], 0)
        self.assertEqual(m_good["filler_residue_hits"], 0)
        self.assertGreaterEqual(m_good["canonical_tech_hits"], 4)
        self.assertTrue(m_good["has_newline"])

        samples_base = [
            {
                "recording_name": "rec-1.mp3",
                "recording_sha1": "sha_1",
                "ok": True,
                "duration_sec": 5.0,
                "session_tag": "agy#11111111",
                "asr_cached": False,
                "asr_ms": 1200,
                "refine_ms": 1800,
                "is_warm": False,
                "cache_hit_pct": 0.0,
                "asr_draft": "初稿一",
                "refined_text": "SECRET_PROSE_ONE sk-secret123456789",
                "submitted_user_ref": "提交一",
                **m_bad,
            },
            {
                "recording_name": "rec-2.mp3",
                "recording_sha1": "sha_2",
                "ok": True,
                "duration_sec": 25.0,
                "session_tag": "agy#11111111",
                "asr_cached": True,
                "asr_ms": 2400,
                "refine_ms": 2200,
                "is_warm": True,
                "cache_hit_pct": 94.5,
                "asr_draft": "初稿二",
                "refined_text": "SECRET_PROSE_TWO",
                "submitted_user_ref": "提交二",
                **m_bad,
            },
        ]
        samples_cand = [
            {**samples_base[0], "refine_ms": 1500, "refined_text": "CAND_PROSE_ONE", **m_good},
            {**samples_base[1], "refine_ms": 1700, "cache_hit_pct": 96.0, "refined_text": "CAND_PROSE_TWO", **m_good},
        ]
        agg = EVAL.aggregate_run_metrics(samples_base)
        self.assertEqual(agg["valid_samples"], 2)
        self.assertEqual(agg["long_samples"], 1)
        self.assertEqual(agg["warm_cache_hit_pct_p50"], 94.5)

        with tempfile.TemporaryDirectory() as td:
            diff_p = Path(td) / "eval-runs" / "diff_base_vs_cand.md"
            table_md = EVAL.compare_runs(
                {"tag": "base", "samples": samples_base},
                {"tag": "cand", "samples": samples_cand},
                diff_out_path=diff_p,
                redact_fn=lambda s: s.replace("sk-secret123456789", "[REDACTED]"),
                diff_disp="~/.cache/agent-voice-input/eval-runs/diff_base_vs_cand.md",
            )
            self.assertNotIn("SECRET_PROSE", table_md)
            self.assertNotIn("CAND_PROSE", table_md)
            self.assertEqual(stat.S_IMODE(diff_p.stat().st_mode), 0o600)
            diff_txt = diff_p.read_text(encoding="utf-8")
            self.assertNotIn("sk-secret123456789", diff_txt)
            self.assertIn("[REDACTED]", diff_txt)

    def test_end_to_end_cli_replay_asr_cache_and_sticky_session_order(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            rec_dir = root / "recordings"
            eval_dir = root / "eval-runs"
            host_rt = root / "host-rt"
            rec_dir.mkdir(parents=True)
            host_rt.mkdir(parents=True)

            sid = "395438e5-0000-4000-8000-000000000001"
            brain_logs = home / ".gemini" / "antigravity-cli" / "brain" / sid / ".system_generated" / "logs"
            brain_logs.mkdir(parents=True)
            cfg_file = home / ".config" / "agent-voice-input" / "config.toml"
            cfg_file.parent.mkdir(parents=True)
            cfg_file.write_text('[asr]\nprovider = "dashscope"\n[refiner]\nprovider = "codex"\n', encoding="utf-8")

            t0 = 1794967200.0
            transcript = brain_logs / "transcript_full.jsonl"
            transcript.write_text(
                "\n".join(
                    json.dumps(r, ensure_ascii=False)
                    for r in [
                        {"source": "USER_EXPLICIT", "type": "USER_INPUT", "created_at": t0 - 60, "content": "查看 SessionTracker"},
                        {"source": "MODEL", "type": "PLANNER_RESPONSE", "created_at": t0 - 50, "content": "SessionTracker 位于 session_ctx.py"},
                        {"source": "USER_EXPLICIT", "type": "USER_INPUT", "created_at": t0 + 12, "content": "第一条口述提交稿"},
                        {"source": "MODEL", "type": "PLANNER_RESPONSE", "created_at": t0 + 18, "content": "收到第一条回复"},
                        {"source": "USER_EXPLICIT", "type": "USER_INPUT", "created_at": t0 + 42, "content": "第二条口述提交稿"},
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            r1 = rec_dir / "rec-20261118-100010-aaaaaa.wav"
            r2 = rec_dir / "rec-20261118-100040-bbbbbb.wav"
            r1.write_bytes(_make_voiced_wav(4.0))
            r2.write_bytes(_make_voiced_wav(12.0))
            os.utime(r1, (t0 + 10.0, t0 + 10.0))
            os.utime(r2, (t0 + 40.0, t0 + 40.0))

            (host_rt / "telemetry.jsonl").write_text(
                "\n".join(
                    json.dumps(r)
                    for r in [
                        {"ts": t0 + 11.5, "ok": True, "session_key": f"agy:{sid}", "raw_audio_sec": 4.0, "total_ms": 1500, "ctx_chars": 65},
                        {"ts": t0 + 42.0, "ok": True, "session_key": f"agy:{sid}", "raw_audio_sec": 12.0, "total_ms": 2000, "ctx_chars": 110},
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            asr_calls: list[int] = []
            seen_ctx_Turn_counts: list[tuple[str, int]] = []

            class FakeAsr:
                provider_name = "dashscope"

                def transcribe(self, wav_bytes: bytes, context=None, **kwargs) -> AsrResult:
                    asr_calls.append(len(wav_bytes))
                    idx = len(asr_calls)
                    return AsrResult(
                        text=f"初稿第{idx}条 private_utterance_{idx}",
                        provider="dashscope",
                        model="qwen3-asr-flash",
                        latency_ms=300 * idx,
                    )

            class FakeRefiner:
                provider_name = "codex"

                def __init__(self) -> None:
                    self.calls = 0

                def prepare_session(self, session_key: str) -> None:
                    pass

                def refine(self, asr_res, *, session_key: str = "", tracker=None, **kwargs) -> RefinerResult:
                    self.calls += 1
                    st = tracker.get_state(session_key) if tracker else None
                    turns_n = len(st.clean_turns) if st else 0
                    seen_ctx_Turn_counts.append((session_key, turns_n))
                    blocks, _ = build_refiner_request_blocks(tracker_cfg, asr_res.text, session_key=session_key, tracker=tracker)
                    joined_ctx = "\n".join(blocks)
                    if self.calls == 1:
                        assert "第一条口述提交稿" not in joined_ctx
                        assert "收到第一条回复" not in joined_ctx
                    else:
                        assert "第一条口述提交稿" in joined_ctx
                        assert "第二条口述提交稿" not in joined_ctx
                    return RefinerResult(
                        text=f"精修第{self.calls}条 SessionTracker",
                        provider="codex",
                        model="gpt-6-luna",
                        latency_ms=400,
                        context_chars=sum(len(b) for b in blocks),
                        context_chunks=len(blocks),
                        input_tokens=2000,
                        cached_input_tokens=0 if self.calls == 1 else 1900,
                        output_tokens=20,
                        reasoning_tokens=0,
                    )

                def close(self) -> None:
                    pass

            tracker_cfg, _ = EVAL.load_config(config_path=cfg_file, home_dir=home, env={"HOME": str(home), "API_KEY_ALI": "sk-fake-ali-key"}, load_default_env_files=False)
            common_args = [
                "--config", str(cfg_file),
                "--home-dir", str(home),
                "--recordings-dir", str(rec_dir),
                "--eval-dir", str(eval_dir),
                "--host-runtime-dir", str(host_rt),
                "--no-git-status",
            ]

            with (
                mock.patch.dict(os.environ, {"HOME": str(home), "API_KEY_ALI": "sk-fake-ali-key"}, clear=False),
                mock.patch.object(EVAL, "create_asr_provider", side_effect=lambda *a, **kw: FakeAsr()),
                mock.patch.object(EVAL, "create_refiner", side_effect=lambda *a, **kw: FakeRefiner()),
                mock.patch("sys.stderr", io.StringIO()),
            ):
                buf1 = io.StringIO()
                with mock.patch("sys.stdout", buf1):
                    rc1 = EVAL.main([*common_args, "--tag", "base_v1"])
                self.assertEqual(rc1, 0)
                self.assertEqual(len(asr_calls), 2)
                # Utterance 1 saw 2 historical turns; utterance 2 saw 4 historical turns on same session_key
                self.assertEqual(seen_ctx_Turn_counts, [(f"agy:{sid}", 2), (f"agy:{sid}", 4)])
                self.assertNotIn("private_utterance", buf1.getvalue())

                # Second run reuses Stage 1 ASR disk cache (0 new ASR calls)
                buf2 = io.StringIO()
                with mock.patch("sys.stdout", buf2):
                    rc2 = EVAL.main([*common_args, "--tag", "cand_v2"])
                self.assertEqual(rc2, 0)
                self.assertEqual(len(asr_calls), 2)

                # Compare mode
                buf3 = io.StringIO()
                with mock.patch("sys.stdout", buf3):
                    rc3 = EVAL.main([*common_args, "--compare", "base_v1", "cand_v2"])
                self.assertEqual(rc3, 0)
                self.assertNotIn("private_utterance", buf3.getvalue())

                # Pure Stage 2 replay via --asr-from reuses frozen base_v1 asr_draft with 0 ASR calls
                buf4 = io.StringIO()
                with mock.patch("sys.stdout", buf4):
                    rc4 = EVAL.main([*common_args, "--tag", "cand_stage2", "--manifest-from", "base_v1", "--asr-from", "base_v1"])
                self.assertEqual(rc4, 0)
                self.assertEqual(len(asr_calls), 2)
                s2_doc = json.loads((eval_dir / "cand_stage2.json").read_text(encoding="utf-8"))
                self.assertEqual(len(s2_doc["samples"]), 2)
                self.assertTrue(all(s["asr_cached"] for s in s2_doc["samples"]))
                self.assertTrue(all("is_single_hop_refined" in s for s in s2_doc["samples"]))

                # Missing --manifest-from exits non-zero instead of silently writing 0-sample run
                with self.assertRaises(SystemExit):
                    EVAL.main([*common_args, "--tag", "bad_mf", "--manifest-from", "nonexistent_manifest"])

            # Refuses placing eval output inside repository
            with self.assertRaises(ValueError):
                EVAL._resolve_eval_dir({}, str(REPO_ROOT))
            with self.assertRaises(ValueError):
                EVAL._resolve_eval_dir({}, str(REPO_ROOT / "sub-eval"))

            # Tight CJK-adjacent homophone errors & canonical Opus/Codex/Luna hits
            m_cjk = EVAL.evaluate_sample_metrics(
                "把那个To B X订阅里的Lula模型和refiner点py改一下，就这一点我再补充一下。",
                "把那个To B X订阅里的Lula模型和refiner点py改一下。就这一点我再补充一下。",
            )
            self.assertGreaterEqual(m_cjk["homophone_error_hits"], 3)
            self.assertGreaterEqual(m_cjk["filler_residue_hits"], 1)
            m_cjk_fixed = EVAL.evaluate_sample_metrics(
                "把那个To B X订阅里的Lula模型和refiner点py改一下，就这一点我再补充一下。",
                "把Codex订阅里的Luna模型、Opus和refiner.py改一下。",
            )
            self.assertEqual(m_cjk_fixed["homophone_error_hits"], 0)
            self.assertEqual(m_cjk_fixed["filler_residue_hits"], 0)
            self.assertGreaterEqual(m_cjk_fixed["canonical_tech_hits"], 4)

            self.assertEqual(stat.S_IMODE(eval_dir.stat().st_mode), 0o700)
            for fname in ("asr_cache.json", "base_v1.json", "cand_v2.json", "diff_base_v1_vs_cand_v2.md"):
                fpath = eval_dir / fname
                self.assertTrue(fpath.is_file(), fname)
                self.assertEqual(stat.S_IMODE(fpath.stat().st_mode), 0o600)

    def test_v091_single_hop_replay_bypasses_asr_cache_tracks_e2e_backticks_and_paired_retention(self) -> None:
        diag_path = REPO_ROOT / ".agents" / "skills" / "voice-eval" / "scripts" / "diagnose_live.py"
        diag_spec = importlib.util.spec_from_file_location("diagnose_live_mod", diag_path)
        assert diag_spec is not None and diag_spec.loader is not None
        diag_mod = importlib.util.module_from_spec(diag_spec)
        diag_spec.loader.exec_module(diag_mod)

        # 1. diagnose_live: single_hop_passthrough is NOT flagged as FALLBACK and uses 3300ms threshold
        flags_fast = diag_mod._diagnose_record_flags(
            {
                "ok": True,
                "session_key": "agy:1",
                "refiner_provider": "off",
                "refiner_fallback": "single_hop_passthrough",
                "asr_ms": 2200,
                "refine_ms": 0,
                "input_tokens": 12000,
                "cached_input_tokens": 10000,
                "ctx_chars": 45000,
            },
            is_first_for_session=False,
        )
        self.assertEqual(flags_fast, [])
        flags_slow = diag_mod._diagnose_record_flags(
            {
                "ok": True,
                "session_key": "",
                "refiner_provider": "off",
                "refiner_fallback": "single_hop_passthrough",
                "asr_ms": 3400,
                "refine_ms": 0,
            },
            is_first_for_session=True,
        )
        self.assertIn("NO_SESSION_CTX", flags_slow)
        self.assertIn("ASR_SLOW", flags_slow)
        self.assertTrue(all(not f.startswith("FALLBACK") for f in flags_slow))
        # general mode does NOT warn NO_SESSION_CTX; force_on_single_hop keeps 3300ms ASR threshold via is_single_hop_refined
        flags_gen_sh = diag_mod._diagnose_record_flags(
            {
                "ok": True,
                "mode": "general",
                "session_key": "",
                "is_single_hop_refined": True,
                "refiner_provider": "openrouter",
                "refiner_fallback": "",
                "asr_ms": 2500,
                "refine_ms": 600,
            },
            is_first_for_session=True,
        )
        self.assertEqual(flags_gen_sh, [])

        # 2. Single-hop replay attaches context_blocks, skips asr_cache.json, records e2e_ms/backtick_hits,
        #    and computes paired char_retention against a two-stage baseline
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home, rec_dir, eval_dir, host_rt = root / "home", root / "rec", root / "eval", root / "rt"
            rec_dir.mkdir(parents=True)
            host_rt.mkdir(parents=True)
            sid = "395438e5-0000-4000-8000-000000000009"
            brain_logs = home / ".gemini" / "antigravity-cli" / "brain" / sid / ".system_generated" / "logs"
            brain_logs.mkdir(parents=True)
            cfg_file = home / ".config" / "agent-voice-input" / "config.toml"
            cfg_file.parent.mkdir(parents=True)
            cfg_file.write_text('[asr]\nprovider = "dashscope"\n[refiner]\nprovider = "off"\n', encoding="utf-8")

            t0 = 1794967200.0
            (brain_logs / "transcript_full.jsonl").write_text(
                "\n".join(
                    json.dumps(r, ensure_ascii=False)
                    for r in [
                        {"source": "USER_EXPLICIT", "type": "USER_INPUT", "created_at": t0 - 60, "content": "看 refiner.py"},
                        {"source": "MODEL", "type": "PLANNER_RESPONSE", "created_at": t0 - 50, "content": "已打开 refiner.py"},
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            r1 = rec_dir / "rec-20261118-100010-111111.wav"
            r2 = rec_dir / "rec-20261118-100040-222222.wav"
            r1.write_bytes(_make_voiced_wav(4.0))
            r2.write_bytes(_make_voiced_wav(12.0))
            os.utime(r1, (t0 + 10.0, t0 + 10.0))
            os.utime(r2, (t0 + 40.0, t0 + 40.0))
            (host_rt / "telemetry.jsonl").write_text(
                "\n".join(
                    json.dumps(r)
                    for r in [
                        {"ts": t0 + 11.0, "ok": True, "session_key": f"agy:{sid}", "raw_audio_sec": 4.0, "total_ms": 900},
                        {"ts": t0 + 41.0, "ok": True, "session_key": f"agy:{sid}", "raw_audio_sec": 12.0, "total_ms": 950},
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            sh_calls: list[Any] = []

            class FakeSingleHopAsr:
                provider_name = "dashscope"
                single_hop = True

                def transcribe(self, wav_bytes: bytes, context=None, **kw) -> AsrResult:
                    sh_calls.append(context)
                    n = len(sh_calls)
                    return AsrResult(
                        text="把 `refiner.py` 改好" if n == 1 else "直接把 refiner.py 改好就行",
                        provider="dashscope",
                        model="qwen3.8-omni-flash",
                        latency_ms=920 if n == 1 else 860,
                        is_single_hop_refined=True,
                        input_tokens=5000,
                        cached_input_tokens=0 if n == 1 else 4500,
                        output_tokens=10,
                        reasoning_tokens=0,
                        context_chars=sum(len(b) for b in (context.context_blocks if context else ())),
                        context_chunks=len(context.context_blocks if context else ()),
                    )

            common = [
                "--config", str(cfg_file),
                "--home-dir", str(home),
                "--recordings-dir", str(rec_dir),
                "--eval-dir", str(eval_dir),
                "--host-runtime-dir", str(host_rt),
                "--no-git-status",
            ]
            with (
                mock.patch.dict(os.environ, {"HOME": str(home), "API_KEY_ALI": "sk-fake-ali-key"}, clear=False),
                mock.patch.object(EVAL, "create_asr_provider", side_effect=lambda *a, **kw: FakeSingleHopAsr()),
                mock.patch("sys.stderr", io.StringIO()),
            ):
                with mock.patch("sys.stdout", io.StringIO()):
                    EVAL.main([*common, "--asr-model", "qwen3.8-omni-flash", "--tag", "sh_run1"])
                    EVAL.main([*common, "--asr-model", "qwen3.8-omni-flash", "--tag", "sh_run2"])
                # Single-hop MUST NOT reuse asr_cache.json across runs (4 real calls across 2 runs)
                self.assertEqual(len(sh_calls), 4)
                self.assertTrue(all(len(c.context_blocks) >= 1 for c in sh_calls))
                self.assertFalse((eval_dir / "asr_cache.json").exists())

                sh_doc = json.loads((eval_dir / "sh_run1.json").read_text(encoding="utf-8"))
                self.assertIsNone(sh_doc["aggregate"]["char_retention_p50"])
                self.assertEqual(sh_doc["aggregate"]["e2e_ms_p50"], 890.0)
                self.assertEqual(sh_doc["aggregate"]["warm_cache_hit_pct_p50"], 90.0)
                self.assertEqual(sh_doc["aggregate"]["backtick_hits"], 2)
                self.assertEqual(sh_doc["aggregate"]["backtick_samples"], 1)

                # Write synthetic two-stage base with real asr_draft and verify paired retention calculation
                base_two_stage = {
                    "tag": "two_stage_base",
                    "meta": {"asr_provider": "dashscope"},
                    "samples": [
                        {
                            **sh_doc["samples"][0],
                            "used_fallback": "",
                            "asr_ms": 1200,
                            "refine_ms": 1500,
                            "e2e_ms": 2700,
                            "asr_draft": "呃那个把 refiner 点 py 改好一下吧",
                            "asr_chars": 23,
                            "char_retention": 0.6,
                        },
                        {
                            **sh_doc["samples"][1],
                            "used_fallback": "",
                            "asr_ms": 1400,
                            "refine_ms": 1600,
                            "e2e_ms": 3000,
                            "asr_draft": "嗯直接把那个 refiner 点 py 改好就行了",
                            "asr_chars": 25,
                            "char_retention": 0.7,
                        },
                    ],
                }
                cmp_md = EVAL.compare_runs(base_two_stage, sh_doc)
                self.assertIn("Single-hop `char_retention` is computed relative to the paired Stage 1 `asr_draft`", cmp_md)
                self.assertNotIn("| Char retention `final/asr` p50 (all / `>=10s`) | `56.5% / 68.0%` | `- / -` |", cmp_md)

                # Legacy single-hop JSON (where asr_draft duplicated refined_text) still uses paired two-stage draft
                legacy_sh_doc = {
                    **sh_doc,
                    "samples": [{**s, "asr_draft": s["refined_text"], "asr_chars": len(s["refined_text"]), "char_retention": 1.0} for s in sh_doc["samples"]],
                }
                cmp_legacy_md = EVAL.compare_runs(base_two_stage, legacy_sh_doc)
                self.assertNotIn("100.0% / 100.0%", cmp_legacy_md)

                # --asr-from on a single-hop JSON recovers refined_text instead of treating empty asr_draft as empty_asr
                with mock.patch("sys.stdout", io.StringIO()):
                    EVAL.main([*common, "--asr-from", "sh_run1", "--tag", "sh_from_frozen"])
                sh_frozen_doc = json.loads((eval_dir / "sh_from_frozen.json").read_text(encoding="utf-8"))
                self.assertEqual(sh_frozen_doc["aggregate"]["valid_samples"], 2)
                self.assertTrue(all(s["ok"] and s["refined_text"] for s in sh_frozen_doc["samples"]))

                # mode='general' telemetry record MUST NOT fallback-match nearby CLI session history
                r3 = rec_dir / "rec-20261118-100050-333333.wav"
                r3.write_bytes(_make_voiced_wav(4.0))
                os.utime(r3, (t0 + 50.0, t0 + 50.0))
                (host_rt / "telemetry.jsonl").write_text(
                    "\n".join(
                        json.dumps(r)
                        for r in [
                            {"ts": t0 + 11.0, "ok": True, "session_key": f"agy:{sid}", "raw_audio_sec": 4.0, "total_ms": 900},
                            {"ts": t0 + 41.0, "ok": True, "session_key": f"agy:{sid}", "raw_audio_sec": 12.0, "total_ms": 950},
                            {"ts": t0 + 51.0, "ok": True, "mode": "general", "session_key": "", "raw_audio_sec": 4.0, "total_ms": 800},
                        ]
                    )
                    + "\n",
                    encoding="utf-8",
                )
                sh_calls.clear()
                with mock.patch("sys.stdout", io.StringIO()):
                    EVAL.main([*common, "--asr-model", "qwen3.8-omni-flash", "--tag", "sh_with_gen"])
                gen_doc = json.loads((eval_dir / "sh_with_gen.json").read_text(encoding="utf-8"))
                gen_sample = [s for s in gen_doc["samples"] if s["recording_name"] == r3.name][0]
                self.assertEqual(gen_sample["mode"], "general")
                self.assertEqual(gen_sample["session_tag"], "none")
                self.assertEqual(gen_sample["ctx_chunks"], 0)


if __name__ == "__main__":
    unittest.main()
