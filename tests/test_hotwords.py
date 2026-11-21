"""Offline unit tests for layered plain-text hotwords, scene auto-mounting, hot reload, quota budgeter, and CLI."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_voice_input.app_scope import resolve_hotwords_scope
from agent_voice_input.asr_providers import (
    _build_doubao_corpus_context,
    _coerce_context,
    _compose_context_text,
    _compose_stt_prompt_text,
    _format_vocabulary_hints,
    load_hotwords_file as asr_load_hotwords_file,
    transcribe,
)
from agent_voice_input.audio_proc import pack_pcm16_mono_wav
from agent_voice_input import cli as cli_mod
from agent_voice_input.cli import main as cli_main
from agent_voice_input.config import Config, ConfigError
from agent_voice_input.daemon import run_pipeline_once
from agent_voice_input.hotwords import (
    DEFAULT_HOTWORDS_FILE,
    HOTWORDS_QUOTA_SPECS,
    HotwordsTable,
    add_hotwords,
    budget_hotwords_for_provider,
    clear_hotwords_cache,
    dedupe_hotwords_ci,
    format_budget_line,
    load_hotwords_file,
    load_hotwords_table,
    normalize_hotword_scope,
    parse_hotwords_text,
    remove_hotwords,
    resolve_hotwords_path,
    resolve_hotwords_quota_spec,
    summarize_hotwords_budgets,
    validate_hotword_term,
)
from agent_voice_input.http_transport import HttpRequest, HttpResponse
from agent_voice_input.llm_common import AsrContext, AsrResult
from agent_voice_input.platform_common import ForegroundWindow
from agent_voice_input.refiner import NoopRefiner
from agent_voice_input.session_ctx import SessionTracker, write_session_registry_entry
from agent_voice_input.workspace_ctx import BUILTIN_AGENT_HOTWORDS


def _voiced_wav() -> bytes:
    return pack_pcm16_mono_wav(([4000, -4000] * 3200), sample_rate=16000)


class _SpyAsrProvider:
    provider_name = "dashscope"
    single_hop = False

    def __init__(self, text: str = "测试转写", *, hotwords_file: str = "", env: dict[str, str] | None = None) -> None:
        self.text = text
        self.hotwords_file = hotwords_file
        self.env = env or {}
        self.seen_contexts: list[AsrContext] = []

    def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None, **kwargs: object) -> AsrResult:
        eff_ctx = _coerce_context(context, self.hotwords_file, env=self.env)
        self.seen_contexts.append(eff_ctx)
        return AsrResult(text=self.text, provider=self.provider_name, model="qwen3-asr-flash", latency_ms=10)


class _DummyPlatform:
    def get_foreground_window(self) -> ForegroundWindow:
        return ForegroundWindow(101, "WindowsTerminal.exe", "bash")

    def is_allowed_terminal(self, win: ForegroundWindow) -> bool:
        return (win.process or "").strip().lower() in ("windowsterminal.exe", "ghostty")

    def switch_to_fallback_audio(self) -> bool:
        return False


class HotwordsParserAndEditorTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_hotwords_cache()

    def tearDown(self) -> None:
        clear_hotwords_cache()

    def test_parse_legacy_flat_and_layered_hotwords_preserves_casing_and_hash_terms(self) -> None:
        raw = "\ufeff# Top standalone comment\n"
        raw += "\n"
        raw += "  MyCustomBrand\n"
        raw += "mycustombrand\n"
        raw += "C#\n"
        raw += "F#  # functional lang\n"
        raw += "Issue#42 # inline comment\n"
        raw += "\n"
        raw += "[global] # explicit global continuation\n"
        raw += "GlobalTwo\n"
        raw += "MYCUSTOMBRAND\n"
        raw += "\n"
        raw += "[CHAT]\n"
        raw += "# Colleagues\n"
        raw += "小明\n"
        raw += "GlobalTwo\n"
        raw += "小红  # PM\n"
        raw += "\n"
        raw += "[ coding ]\n"
        raw += "KestrelRouter\n"
        raw += "kestrelrouter\n"
        raw += "SessionTracker\n"
        raw += "\n"
        raw += "[unknown_future_section]\n"
        raw += "ShouldBeIgnored\n"

        table = parse_hotwords_text(raw)
        self.assertEqual(table.global_terms, ("MyCustomBrand", "C#", "F#", "Issue#42", "GlobalTwo"))
        self.assertEqual(table.chat_terms, ("小明", "GlobalTwo", "小红"))
        self.assertEqual(table.coding_terms, ("KestrelRouter", "SessionTracker"))

        self.assertEqual(table.terms_for_scope("global"), table.global_terms)
        self.assertEqual(table.terms_for_scope("chat"), table.chat_terms)
        self.assertEqual(table.terms_for_scope("coding"), table.coding_terms)

        # Scene merging: scene-specific terms come FIRST, then global, CI-deduped
        self.assertEqual(
            table.merged_for_scene("coding"),
            ["KestrelRouter", "SessionTracker", "MyCustomBrand", "C#", "F#", "Issue#42", "GlobalTwo"],
        )
        self.assertEqual(
            table.merged_for_scene("chat"),
            ["小明", "GlobalTwo", "小红", "MyCustomBrand", "C#", "F#", "Issue#42"],
        )
        self.assertEqual(
            table.merged_for_scene("global"),
            ["MyCustomBrand", "C#", "F#", "Issue#42", "GlobalTwo"],
        )
        self.assertEqual(
            table.merged_for_scene("all"),
            ["MyCustomBrand", "C#", "F#", "Issue#42", "GlobalTwo", "小明", "小红", "KestrelRouter", "SessionTracker"],
        )
        self.assertEqual(
            table.as_dict(),
            {
                "global": ["MyCustomBrand", "C#", "F#", "Issue#42", "GlobalTwo"],
                "chat": ["小明", "GlobalTwo", "小红"],
                "coding": ["KestrelRouter", "SessionTracker"],
            },
        )
        self.assertEqual(dedupe_hotwords_ci(["  Foo ", "foo", "", "Bar"]), ["Foo", "Bar"])

    def test_validation_and_scope_errors(self) -> None:
        self.assertEqual(validate_hotword_term("  C#  "), "C#")
        self.assertEqual(validate_hotword_term("Claude Code"), "Claude Code")
        for bad in (
            123,
            "",
            "   ",
            "bad\nline",
            "bad\rline",
            "bad\tword",
            "bad\x00null",
            "bad\x0bvt",
            "bad\x0cff",
            "bad\x85nel",
            "\u2028",
            "Good\u2028[chat]\u2028Injected",
            "Good\u2029[coding]\u2029Injected",
            "[coding]",
            "[foo]",
            "#comment",
            "foo # bar",
        ):
            with self.assertRaises(ValueError, msg=f"expected ValueError for {bad!r}"):
                validate_hotword_term(bad)  # type: ignore[arg-type]

        self.assertEqual(normalize_hotword_scope("CODING"), "coding")
        self.assertEqual(normalize_hotword_scope("all", allow_all=True), "all")
        with self.assertRaises(ValueError):
            normalize_hotword_scope("all", allow_all=False)
        with self.assertRaises(ValueError):
            normalize_hotword_scope("bogus", allow_all=True)

    def test_add_and_remove_preserve_comments_layout_permissions_and_reject_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            hw_path = tmp / "nested" / "dir" / "hotwords.txt"

            # Empty input on non-existent file does not create file
            empty_add = add_hotwords(hw_path, [], scope="global")
            self.assertFalse(empty_add.changed)
            self.assertFalse(hw_path.exists())

            empty_rm = remove_hotwords(hw_path, ["Missing"], scope="global")
            self.assertFalse(empty_rm.changed)
            self.assertEqual(empty_rm.not_found, ("Missing",))

            # Auto-create parent dirs + atomic write with 0600 permissions
            r1 = add_hotwords(hw_path, ["BrandAlpha", "brandalpha", "C#"], scope="global")
            self.assertTrue(r1.changed)
            self.assertEqual(r1.added, ("BrandAlpha", "C#"))
            self.assertEqual(r1.skipped, ())
            self.assertEqual(stat.S_IMODE(hw_path.stat().st_mode), 0o600)
            self.assertEqual(hw_path.read_text(encoding="utf-8"), "[global]\nBrandAlpha\nC#\n")

            # Add duplicate (CI) + new term
            r2 = add_hotwords(hw_path, ["brandalpha", "BrandBeta"], scope="global")
            self.assertTrue(r2.changed)
            self.assertEqual(r2.added, ("BrandBeta",))
            self.assertEqual(r2.skipped, ("brandalpha",))

            # Pure skip does not rewrite unnecessarily
            r2_dup = add_hotwords(hw_path, ["BRANDALPHA"], scope="global")
            self.assertFalse(r2_dup.changed)
            self.assertEqual(r2_dup.skipped, ("BRANDALPHA",))

            # Hand-edit file with comments, blank lines, and unknown section
            hw_path.write_text(
                "# Top header comment\n"
                "TopLegacyWord  # keep comment\n"
                "\n"
                "[chat]\n"
                "# Chat team\n"
                "Alice\n"
                "\n"
                "[unknown]\n"
                "KeepUntouched\n",
                encoding="utf-8",
            )
            # Add to implicit top global region -> preserves blank line and [chat]/[unknown]
            r3 = add_hotwords(hw_path, ["TopTwo"], scope="global")
            self.assertTrue(r3.changed)
            txt3 = hw_path.read_text(encoding="utf-8")
            self.assertIn("# Top header comment\nTopLegacyWord  # keep comment\nTopTwo\n\n[chat]\n", txt3)
            self.assertIn("[unknown]\nKeepUntouched\n", txt3)

            # Add to existing [chat] and new [coding]
            add_hotwords(hw_path, ["Bob"], scope="chat")
            add_hotwords(hw_path, ["KestrelRouter", "Alice"], scope="coding")
            txt4 = hw_path.read_text(encoding="utf-8")
            self.assertIn("# Chat team\nAlice\nBob\n\n[unknown]\nKeepUntouched\n\n[coding]\nKestrelRouter\nAlice\n", txt4)

            # Add to [global] when file starts directly with [chat] (no top terms)
            chat_only = tmp / "chat_only.txt"
            chat_only.write_text("# comment\n[chat]\nOnlyChat\n", encoding="utf-8")
            add_hotwords(chat_only, ["NewGlobal"], scope="global")
            self.assertEqual(
                chat_only.read_text(encoding="utf-8"),
                "# comment\n[chat]\nOnlyChat\n\n[global]\nNewGlobal\n",
            )

            # Remove from specific scope only (leaves [coding] Alice untouched)
            rm_chat = remove_hotwords(hw_path, ["alice", "NoSuchTerm"], scope="chat")
            self.assertTrue(rm_chat.changed)
            self.assertEqual(rm_chat.removed, ("Alice",))
            self.assertEqual(rm_chat.not_found, ("NoSuchTerm",))
            t_after_chat = load_hotwords_table(hw_path, use_cache=False)
            self.assertEqual(t_after_chat.chat_terms, ("Bob",))
            self.assertEqual(t_after_chat.coding_terms, ("KestrelRouter", "Alice"))
            self.assertIn("# Chat team\n", hw_path.read_text(encoding="utf-8"))

            # Remove with scope="all" removes across all scopes, leaving unknown section untouched
            rm_all = remove_hotwords(hw_path, ["alice", "topTwo", "KeepUntouched"], scope="all")
            self.assertTrue(rm_all.changed)
            self.assertEqual(rm_all.removed, ("Alice", "TopTwo"))
            self.assertEqual(rm_all.not_found, ("KeepUntouched",))
            self.assertIn("[unknown]\nKeepUntouched\n", hw_path.read_text(encoding="utf-8"))

            # Symlink target refusal & directory refusal
            real_target = tmp / "real.txt"
            real_target.write_text("Safe\n", encoding="utf-8")
            sym_target = tmp / "link.txt"
            sym_target.symlink_to(real_target)
            with self.assertRaises(ConfigError):
                add_hotwords(sym_target, ["Injected"], scope="global")
            with self.assertRaises(ConfigError):
                remove_hotwords(sym_target, ["Safe"], scope="global")

            dir_target = tmp / "subdir"
            dir_target.mkdir()
            with self.assertRaises(ConfigError):
                add_hotwords(dir_target, ["Injected"], scope="global")

            file_as_parent = tmp / "not_a_dir"
            file_as_parent.write_text("x", encoding="utf-8")
            with self.assertRaises(ConfigError):
                add_hotwords(file_as_parent / "hotwords.txt", ["Term"], scope="global")

    def test_legacy_path_fallback_and_stat_cache_hot_reload(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            env = {"HOME": str(home)}
            self.assertIsNone(resolve_hotwords_path("", env=env))

            # Legacy ~/.config/voice-input/hotwords.txt used when primary does not exist
            legacy = home / ".config" / "voice-input" / "hotwords.txt"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("LegacyTerm\n", encoding="utf-8")
            self.assertEqual(resolve_hotwords_path(DEFAULT_HOTWORDS_FILE, env=env), legacy)
            self.assertEqual(load_hotwords_file(DEFAULT_HOTWORDS_FILE, env=env), ["LegacyTerm"])

            # Once primary exists, primary wins
            primary = home / ".config" / "agent-voice-input" / "hotwords.txt"
            primary.parent.mkdir(parents=True)
            primary.write_text("PrimaryTerm\n", encoding="utf-8")
            self.assertEqual(resolve_hotwords_path(DEFAULT_HOTWORDS_FILE, env=env), primary)
            self.assertEqual(load_hotwords_file(DEFAULT_HOTWORDS_FILE, env=env), ["PrimaryTerm"])
            # Cached hit returns same terms
            self.assertEqual(load_hotwords_file(DEFAULT_HOTWORDS_FILE, env=env), ["PrimaryTerm"])

            # Modify file -> stat cache invalidates immediately
            add_hotwords(primary, ["UpdatedTerm"], scope="coding")
            self.assertEqual(
                load_hotwords_file(DEFAULT_HOTWORDS_FILE, env=env, scene="coding"),
                ["UpdatedTerm", "PrimaryTerm"],
            )

            # Delete file -> cache evicts immediately
            primary.unlink()
            legacy.unlink()
            self.assertEqual(load_hotwords_file(DEFAULT_HOTWORDS_FILE, env=env), [])

            # Non-regular file (directory) returns empty table cleanly
            dir_p = Path(td) / "hw_dir"
            dir_p.mkdir()
            self.assertEqual(load_hotwords_table(dir_p), HotwordsTable(path=dir_p, exists=False))
            self.assertEqual(load_hotwords_table(""), HotwordsTable(path=None, exists=False))

            # Cache eviction keeps bounded size when loading >32 distinct files
            for idx in range(35):
                p_i = Path(td) / f"hw_{idx}.txt"
                p_i.write_text(f"Term{idx}\n", encoding="utf-8")
                self.assertEqual(load_hotwords_file(p_i), [f"Term{idx}"])

    def test_atomic_write_rollback_and_io_fault_injection(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            hw_path = tmp / "hotwords.txt"
            hw_path.write_text("[global]\nOriginalTerm\n", encoding="utf-8")

            # Fault during os.replace: original file stays intact and no leftover .tmp files remain
            with mock.patch("agent_voice_input.hotwords.os.replace", side_effect=OSError("ENOSPC")):
                with self.assertRaises(OSError):
                    add_hotwords(hw_path, ["NewTerm"], scope="global")
            self.assertEqual(hw_path.read_text(encoding="utf-8"), "[global]\nOriginalTerm\n")
            self.assertEqual(list(tmp.glob(".*.tmp")), [])

            # Fault before fdopen (os.fchmod): closes fd and unlinks temp file cleanly
            with mock.patch("agent_voice_input.hotwords.os.fchmod", side_effect=OSError("EPERM")):
                with self.assertRaises(OSError):
                    add_hotwords(hw_path, ["NewTerm"], scope="global")
            self.assertEqual(hw_path.read_text(encoding="utf-8"), "[global]\nOriginalTerm\n")
            self.assertEqual(list(tmp.glob(".*.tmp")), [])

            # Unreadable file on load degrades cleanly to empty table
            with mock.patch.object(Path, "read_bytes", side_effect=OSError("EACCES")):
                self.assertEqual(
                    load_hotwords_table(hw_path, use_cache=False),
                    HotwordsTable(path=hw_path, exists=False),
                )

            # Parent mkdir failure surfaces ConfigError
            missing_child = tmp / "blocked_parent" / "hotwords.txt"
            with mock.patch.object(Path, "mkdir", side_effect=OSError("EROFS")):
                with self.assertRaises(ConfigError):
                    add_hotwords(missing_child, ["NewTerm"], scope="global")

    def test_concurrent_edits_lock_safety_bom_crlf_preservation_and_ctime_hot_reload(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            hw_path = tmp / "concurrent.txt"

            # 1. Concurrent add_hotwords across 6 threads -> zero lost updates under flock
            errors: list[Exception] = []

            def _worker(tid: int) -> None:
                try:
                    for i in range(10):
                        add_hotwords(hw_path, [f"Thread{tid}Term{i}"], scope="coding")
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=_worker, args=(tid,)) for tid in range(6)]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
            self.assertEqual(errors, [])
            loaded_terms = set(load_hotwords_table(hw_path, use_cache=False).coding_terms)
            expected_terms = {f"Thread{tid}Term{i}" for tid in range(6) for i in range(10)}
            self.assertEqual(loaded_terms, expected_terms)

            # Symlink lock file is safely rejected
            sym_lock_target = tmp / "sym_target.txt"
            sym_lock_target.write_text("Alpha\n", encoding="utf-8")
            bad_lock = tmp / f".{sym_lock_target.name}.lock"
            bad_lock.symlink_to(tmp / "elsewhere.lock")
            with self.assertRaises(ConfigError):
                add_hotwords(sym_lock_target, ["Beta"], scope="global")

            # 2. Preserve UTF-8 BOM + CRLF across add (including no-trailing-EOL & new section) and remove
            bom_crlf = tmp / "bom_crlf.txt"
            bom_crlf.write_bytes(b"\xef\xbb\xbf# Header\r\n[global]\r\nAlpha")
            add_hotwords(bom_crlf, ["Beta"], scope="global")
            self.assertEqual(
                bom_crlf.read_bytes(),
                b"\xef\xbb\xbf# Header\r\n[global]\r\nAlpha\r\nBeta\r\n",
            )
            add_hotwords(bom_crlf, ["ChatOne"], scope="chat")
            self.assertEqual(
                bom_crlf.read_bytes(),
                b"\xef\xbb\xbf# Header\r\n[global]\r\nAlpha\r\nBeta\r\n\r\n[chat]\r\nChatOne\r\n",
            )
            remove_hotwords(bom_crlf, ["Alpha"], scope="global")
            self.assertEqual(
                bom_crlf.read_bytes(),
                b"\xef\xbb\xbf# Header\r\n[global]\r\nBeta\r\n\r\n[chat]\r\nChatOne\r\n",
            )

            # 3. Same-length in-place overwrite with restored mtime still invalidates cache via st_ctime_ns
            ctime_file = tmp / "ctime_cache.txt"
            ctime_file.write_text("OldWordA\n", encoding="utf-8")
            self.assertEqual(load_hotwords_file(ctime_file), ["OldWordA"])
            st_before = os.stat(ctime_file)
            time.sleep(0.02)
            with open(ctime_file, "r+b") as f:
                f.write(b"NewWordB\n")
                f.flush()
            os.utime(ctime_file, ns=(st_before.st_atime_ns, st_before.st_mtime_ns))
            st_after = os.stat(ctime_file)
            self.assertEqual(st_after.st_mtime_ns, st_before.st_mtime_ns)
            self.assertEqual(st_after.st_size, st_before.st_size)
            self.assertEqual(st_after.st_ino, st_before.st_ino)
            if st_after.st_ctime_ns != st_before.st_ctime_ns:
                self.assertEqual(load_hotwords_file(ctime_file), ["NewWordB"])
            else:
                # Deterministic fallback on coarse-granularity filesystems: advance only st_ctime_ns
                class _FakeStat:
                    st_mode = st_before.st_mode
                    st_mtime_ns = st_before.st_mtime_ns
                    st_ctime_ns = st_before.st_ctime_ns + 1
                    st_size = st_before.st_size
                    st_ino = st_before.st_ino
                    st_dev = st_before.st_dev

                with mock.patch("agent_voice_input.hotwords.os.stat", return_value=_FakeStat()):
                    self.assertEqual(load_hotwords_file(ctime_file), ["NewWordB"])


class HotwordsSceneAndDaemonHotReloadTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_hotwords_cache()

    def tearDown(self) -> None:
        clear_hotwords_cache()

    def test_resolve_hotwords_scope_matrix(self) -> None:
        is_term = lambda w: (w.process or "").lower() in ("windowsterminal.exe", "ghostty")
        self.assertEqual(resolve_hotwords_scope(has_agent_session=True), "coding")
        self.assertEqual(resolve_hotwords_scope(mode="agent"), "coding")
        self.assertEqual(resolve_hotwords_scope(mode="general", target_hint="im_chat"), "chat")
        self.assertEqual(resolve_hotwords_scope(mode="general", target_hint="ai_chat"), "chat")
        self.assertEqual(
            resolve_hotwords_scope(ForegroundWindow(1, "WeChat", "群聊", bundle_id="com.tencent.xinwechat"), mode="general"),
            "chat",
        )
        self.assertEqual(
            resolve_hotwords_scope(ForegroundWindow(2, "Safari", "Mail", bundle_id="com.apple.Safari"), mode="general"),
            "global",
        )
        # Direct window classification without mode
        self.assertEqual(resolve_hotwords_scope(target_hint="im_chat"), "chat")
        self.assertEqual(resolve_hotwords_scope(window_category="terminal"), "coding")
        self.assertEqual(resolve_hotwords_scope(window_category="coding_gui"), "coding")
        self.assertEqual(
            resolve_hotwords_scope(ForegroundWindow(3, "Ghostty", "bash"), is_terminal=is_term),
            "coding",
        )
        self.assertEqual(
            resolve_hotwords_scope(ForegroundWindow(4, "Code.exe", "main.py - Visual Studio Code")),
            "coding",
        )
        self.assertEqual(
            resolve_hotwords_scope(ForegroundWindow(5, "ChatGPT", "New chat", bundle_id="com.openai.chat")),
            "chat",
        )
        self.assertEqual(
            resolve_hotwords_scope(ForegroundWindow(6, "Safari", "Docs", bundle_id="com.apple.Safari")),
            "global",
        )
        self.assertEqual(resolve_hotwords_scope(None), "global")

    def test_daemon_scene_auto_mounting_priority_and_live_hot_reload(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            rt = tmp / "rt"
            rt.mkdir(parents=True, mode=0o700)
            hw_file = home / ".config" / "agent-voice-input" / "hotwords.txt"
            hw_file.parent.mkdir(parents=True)
            hw_file.write_text(
                "[global]\n"
                "MyBrandGlobal\n"
                "\n"
                "[chat]\n"
                "ChatColleagueA\n"
                "\n"
                "[coding]\n"
                "CodingManualTerm\n",
                encoding="utf-8",
            )

            # Seed a prior same-cwd session (Layer 1 prior hotword) and an active CLI session
            prior_sid = "11112222-1111-2222-3333-444444444444"
            sid = "abcd1234-1111-2222-3333-444444444444"
            repo_dir = tmp / "proj"
            repo_dir.mkdir()
            sdir = home / ".codex" / "sessions" / "2026" / "11" / "20"
            sdir.mkdir(parents=True)
            prior_file = sdir / f"rollout-2026-11-20T11-00-00-{prior_sid}.jsonl"
            prior_file.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": prior_sid, "source": "cli", "cwd": str(repo_dir)}}) + "\n"
                + json.dumps({
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "item": {
                            "type": "UserMessage",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "优化 PriorLayerOneController 的缓存和 PriorLayerOneController 状态机，"
                                        + ("详细上下文背景记录用于满足冷启动历史会话最小长度阈值。" * 45)
                                    ),
                                }
                            ],
                        },
                    },
                }) + "\n",
                encoding="utf-8",
            )
            sfile = sdir / f"rollout-2026-11-20T12-00-00-{sid}.jsonl"
            sfile.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": sid, "source": "cli", "cwd": str(repo_dir)}}) + "\n"
                + json.dumps({
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "item": {
                            "type": "UserMessage",
                            "content": [{"type": "text", "text": "检查 LiveSessionSymbol.py 的逻辑"}],
                        },
                    },
                }) + "\n",
                encoding="utf-8",
            )
            write_session_registry_entry(
                rt, cli="codex", session_id=sid, jsonl_path=str(sfile), cwd=str(repo_dir)
            )

            env = {"HOME": str(home)}
            tracker = SessionTracker(home_dir=home, env=env, runtime_dir=rt, proc_root=tmp / "noproc")
            cfg = Config()
            cfg.asr.hotwords_file = str(hw_file)
            cfg.refiner.provider = "off"
            plat = _DummyPlatform()

            # 1. Terminal + Agent session -> manual (coding + global) > session dynamic > prior Layer 1 > builtin
            asr_term = _SpyAsrProvider(hotwords_file=str(hw_file), env=env)
            res_term = run_pipeline_once(
                _voiced_wav(),
                cfg=cfg,
                tracker=tracker,
                asr_provider=asr_term,
                refiner=NoopRefiner(),
                platform=plat,
                origin_window=ForegroundWindow(10, "WindowsTerminal.exe", "codex [#abcd1234]"),
                audio_source="winmm",
                dry_run=True,
                env=env,
            )
            self.assertTrue(res_term.ok)
            hw_term = asr_term.seen_contexts[-1].hotwords
            self.assertEqual(hw_term[:2], ["CodingManualTerm", "MyBrandGlobal"])
            self.assertNotIn("ChatColleagueA", hw_term)
            self.assertIn("LiveSessionSymbol.py", hw_term)
            self.assertIn("PriorLayerOneController", hw_term)
            self.assertLess(hw_term.index("MyBrandGlobal"), hw_term.index("LiveSessionSymbol.py"))
            self.assertLess(hw_term.index("LiveSessionSymbol.py"), hw_term.index("PriorLayerOneController"))
            self.assertLess(hw_term.index("PriorLayerOneController"), hw_term.index(BUILTIN_AGENT_HOTWORDS[0]))

            # 2. IDE window -> coding + global + builtin (no chat words)
            asr_ide = _SpyAsrProvider(hotwords_file=str(hw_file), env=env)
            res_ide = run_pipeline_once(
                _voiced_wav(),
                cfg=cfg,
                tracker=tracker,
                asr_provider=asr_ide,
                refiner=NoopRefiner(),
                platform=plat,
                origin_window=ForegroundWindow(11, "Code.exe", "main.py - Visual Studio Code"),
                audio_source="winmm",
                dry_run=True,
                env=env,
            )
            self.assertTrue(res_ide.ok)
            hw_ide = asr_ide.seen_contexts[-1].hotwords
            self.assertEqual(hw_ide[:2], ["CodingManualTerm", "MyBrandGlobal"])
            self.assertNotIn("ChatColleagueA", hw_ide)
            self.assertIn(BUILTIN_AGENT_HOTWORDS[0], hw_ide)

            # 3. IM / AI Chat windows -> chat + global ONLY (zero coding words, zero builtin coding words)
            for chat_win in (
                ForegroundWindow(12, "WeChat", "工作群", bundle_id="com.tencent.xinwechat"),
                ForegroundWindow(13, "ChatGPT", "New chat", bundle_id="com.openai.chat"),
            ):
                asr_chat = _SpyAsrProvider(hotwords_file=str(hw_file), env=env)
                res_chat = run_pipeline_once(
                    _voiced_wav(),
                    cfg=cfg,
                    tracker=tracker,
                    asr_provider=asr_chat,
                    refiner=NoopRefiner(),
                    platform=plat,
                    origin_window=chat_win,
                    audio_source="winmm",
                    dry_run=True,
                    env=env,
                )
                self.assertTrue(res_chat.ok)
                self.assertEqual(asr_chat.seen_contexts[-1].hotwords, ["ChatColleagueA", "MyBrandGlobal"])
                self.assertEqual(asr_chat.seen_contexts[-1].hotwords_scope, "chat")

            # 4. General non-chat window -> global ONLY
            asr_gen = _SpyAsrProvider(hotwords_file=str(hw_file), env=env)
            res_gen = run_pipeline_once(
                _voiced_wav(),
                cfg=cfg,
                tracker=tracker,
                asr_provider=asr_gen,
                refiner=NoopRefiner(),
                platform=plat,
                origin_window=ForegroundWindow(14, "Safari", "Mail", bundle_id="com.apple.Safari"),
                audio_source="winmm",
                dry_run=True,
                env=env,
            )
            self.assertTrue(res_gen.ok)
            self.assertEqual(asr_gen.seen_contexts[-1].hotwords, ["MyBrandGlobal"])
            self.assertEqual(asr_gen.seen_contexts[-1].hotwords_scope, "global")

            # 5. Hot reload WITHOUT restarting daemon/tracker: edit via add_hotwords/remove_hotwords
            add_hotwords(hw_file, ["HotReloadedChatTerm"], scope="chat")
            remove_hotwords(hw_file, ["ChatColleagueA"], scope="chat")
            asr_chat2 = _SpyAsrProvider(hotwords_file=str(hw_file), env=env)
            res_chat2 = run_pipeline_once(
                _voiced_wav(),
                cfg=cfg,
                tracker=tracker,
                asr_provider=asr_chat2,
                refiner=NoopRefiner(),
                platform=plat,
                origin_window=ForegroundWindow(15, "Feishu.exe", "飞书"),
                audio_source="winmm",
                dry_run=True,
                env=env,
            )
            self.assertTrue(res_chat2.ok)
            self.assertEqual(asr_chat2.seen_contexts[-1].hotwords, ["HotReloadedChatTerm", "MyBrandGlobal"])


class HotwordsQuotaBudgeterTests(unittest.TestCase):
    def test_resolve_quota_specs_across_all_providers(self) -> None:
        self.assertEqual(resolve_hotwords_quota_spec("doubao").key, "doubao")
        self.assertEqual(resolve_hotwords_quota_spec("volcengine").key, "doubao")
        self.assertEqual(resolve_hotwords_quota_spec("dashscope").key, "dashscope")
        # Default DashScope STT model uses chat-completions endpoint but is NOT single-hop -> stays 413-char dashscope!
        self.assertEqual(
            resolve_hotwords_quota_spec("dashscope", model="qwen3-asr-flash", api="chat-completions").key,
            "dashscope",
        )
        self.assertEqual(
            resolve_hotwords_quota_spec("dashscope", model="qwen3.8-omni-flash", api="chat-completions").key,
            "single_hop",
        )
        self.assertEqual(resolve_hotwords_quota_spec("dashscope", model="qwen3.8-omni-flash").key, "single_hop")
        self.assertEqual(resolve_hotwords_quota_spec("gemini").key, "single_hop")
        self.assertEqual(
            resolve_hotwords_quota_spec("gemini", api="audio-transcriptions", base_url="https://proxy.example/v1").key,
            "whisper",
        )
        self.assertEqual(resolve_hotwords_quota_spec("openai").key, "whisper")
        self.assertEqual(resolve_hotwords_quota_spec("openai", api="chat-completions").key, "single_hop")
        self.assertEqual(resolve_hotwords_quota_spec("groq").key, "whisper")
        self.assertEqual(resolve_hotwords_quota_spec("siliconflow").key, "whisper")
        self.assertEqual(resolve_hotwords_quota_spec("openai-compatible").key, "whisper")
        self.assertEqual(resolve_hotwords_quota_spec("whisper").key, "whisper")
        self.assertEqual(resolve_hotwords_quota_spec("single_hop").key, "single_hop")
        self.assertEqual(resolve_hotwords_quota_spec("openrouter", model="qwen/qwen3-asr-1.7b").key, "openrouter_stt_unsupported")
        self.assertEqual(resolve_hotwords_quota_spec("openrouter", model="deepgram/nova-3").key, "openrouter_keyterms")
        self.assertEqual(resolve_hotwords_quota_spec("openrouter", model="google/gemini-3.5-flash-lite").key, "single_hop")
        self.assertEqual(resolve_hotwords_quota_spec("custom-unknown").key, "single_hop")
        self.assertIn("doubao", HOTWORDS_QUOTA_SPECS)

    def test_doubao_whisper_dashscope_single_hop_and_openrouter_quotas(self) -> None:
        # 1. Doubao: 30 terms / 180 chars / 60 char per term / stem collapse / whole-word skip past oversized
        db_terms = [
            "ManualTop",
            "refiner.py",
            "refiner",
            "x" * 70,  # >60 chars -> whole-word skipped & marked truncated (never chopped in half!)
            "y" * 60,
            "z" * 60,
            "w" * 60,  # 9 + 7 + 60 + 60 = 136; +60 = 196 > 180 -> skipped, loop continues
            "TailShort",
        ]
        db_res = budget_hotwords_for_provider(db_terms, "doubao")
        self.assertEqual(db_res.kept[0], "ManualTop")
        self.assertIn("refiner", db_res.kept)
        self.assertNotIn("refiner.py", db_res.kept)
        self.assertNotIn("x" * 60, db_res.kept)
        self.assertNotIn("x" * 70, db_res.kept)
        self.assertIn("y" * 60, db_res.kept)
        self.assertIn("z" * 60, db_res.kept)
        self.assertIn("TailShort", db_res.kept)
        self.assertEqual(db_res.dropped, ("x" * 70, "w" * 60))
        self.assertTrue(db_res.truncated)
        self.assertLessEqual(db_res.used_chars, 180)
        self.assertIn("doubao:", format_budget_line(db_res))
        self.assertIn("TRUNCATED", format_budget_line(db_res))

        # 2. Doubao > 30 short terms -> truncated at 30 terms, manual head preserved
        many_short = [f"w{i:02d}" for i in range(40)]
        db_30 = budget_hotwords_for_provider(many_short, "doubao")
        self.assertEqual(db_30.kept_count, 30)
        self.assertEqual(db_30.dropped_count, 10)
        self.assertEqual(db_30.kept[0], "w00")
        self.assertEqual(db_30.dropped[0], "w30")
        self.assertIn("+5 more", format_budget_line(db_30))

        # 3. Whisper: head-first selection up to 35 terms / 353 chars, reversed at prompt tail
        whisper_terms = ["ManualFirst", "ManualSecond", "manualfirst", *[f"dyn_{i:02d}" for i in range(50)]]
        w_res = budget_hotwords_for_provider(whisper_terms, "whisper")
        self.assertEqual(w_res.kept[:2], ("ManualFirst", "ManualSecond"))
        self.assertEqual(w_res.kept_count, 35)
        self.assertLessEqual(w_res.used_chars, 353)
        prompt = _compose_stt_prompt_text(AsrContext(hotwords=whisper_terms, dialog_text="背景对话" * 100))
        self.assertTrue(prompt.endswith("、ManualSecond、ManualFirst"))

        # Oversized single term is NEVER chopped in half; skipped as dropped/truncated and subsequent short term fits!
        huge_first = budget_hotwords_for_provider(["A" * 500, "ShortFits"], "dashscope")
        self.assertEqual(huge_first.kept, ("ShortFits",))
        self.assertEqual(huge_first.dropped, ("A" * 500,))
        self.assertTrue(huge_first.truncated)
        huge_whisper = budget_hotwords_for_provider(["A" * 500, "B"], "whisper", max_chars=20)
        self.assertEqual(huge_whisper.kept, ("B",))
        self.assertEqual(huge_whisper.dropped, ("A" * 500,))
        self.assertTrue(huge_whisper.truncated)
        zero_cap = budget_hotwords_for_provider(["Alpha"], "whisper", max_chars=0)
        self.assertEqual(zero_cap.kept, ())
        self.assertEqual(zero_cap.dropped, ("Alpha",))

        # 4. DashScope context_text does NOT chop words mid-term
        ds_terms = [f"LongTechnicalIdentifier_{i:02d}" for i in range(40)]
        ctx_txt = _compose_context_text(AsrContext(hotwords=ds_terms), max_chars=2500)
        self.assertTrue(ctx_txt.startswith("常用技术术语："))
        self.assertLessEqual(len(ctx_txt), 420)
        rendered_terms = ctx_txt[len("常用技术术语：") :].split("、")
        for tok in rendered_terms:
            self.assertIn(tok, ds_terms, f"term was chopped mid-word: {tok!r}")

        # 5. Single-hop vocabulary hints uses unified 45-term / 480-char budgeter
        vocab_xml = _format_vocabulary_hints(ds_terms)
        self.assertTrue(vocab_xml.startswith('<vocabulary_hints read_only="true">\n'))
        body_line = vocab_xml.splitlines()[1]
        self.assertLessEqual(len(body_line), 480)
        for tok in body_line.split(", "):
            self.assertIn(tok, ds_terms)
        self.assertEqual(_format_vocabulary_hints([]), "")

        # 6. OpenRouter STT unsupported vs keyterms
        or_unsup = budget_hotwords_for_provider(["Alpha", "Beta"], "openrouter", model="qwen/qwen3-asr-1.7b")
        self.assertEqual(or_unsup.kept, ())
        self.assertEqual(or_unsup.dropped, ("Alpha", "Beta"))
        self.assertIn("0/0 terms", format_budget_line(or_unsup))
        or_unsup_empty = budget_hotwords_for_provider([], "openrouter", model="qwen/qwen3-asr-1.7b")
        self.assertIn("not passed by this route", format_budget_line(or_unsup_empty))

        or_kt = budget_hotwords_for_provider([f"t{i}" for i in range(60)], "openrouter", model="deepgram/nova-3")
        self.assertEqual(or_kt.kept_count, 50)
        self.assertEqual(or_kt.dropped_count, 10)

        # 7. Doubao corpus JSON delegates to unified budgeter
        raw_corpus = _build_doubao_corpus_context(AsrContext(hotwords=["Alpha", "Beta"]))
        assert raw_corpus is not None
        self.assertEqual(json.loads(raw_corpus)["hotwords"], [{"word": "Alpha"}, {"word": "Beta"}])

    def test_summarize_hotwords_budgets_shapes(self) -> None:
        s_map = summarize_hotwords_budgets(
            ["GlobalA", "GlobalB"],
            {"chat": ["ChatA"], "coding": ["CodeA", "CodeB", "CodeC"]},
            active_provider="doubao",
            active_model="bigmodel",
            active_api="doubao",
        )
        self.assertEqual(s_map["reference_scene"], "coding")
        self.assertEqual(s_map["reference_count"], 5)
        self.assertEqual(s_map["active"]["provider"], "doubao")
        self.assertEqual(s_map["active"]["by_scene"]["chat"]["kept_count"], 3)
        self.assertEqual(s_map["providers"]["whisper"]["max_terms"], 35)

        s_seq = summarize_hotwords_budgets(["GlobalA"], ["ScopeOnly"], active_provider="dashscope")
        self.assertEqual(s_seq["reference_scene"], "scoped")
        self.assertEqual(s_seq["reference_count"], 2)

        s_none = summarize_hotwords_budgets(["GlobalA"], None)
        self.assertEqual(s_none["reference_scene"], "global")
        self.assertIsNone(s_none["active"])

    def test_asr_coerce_context_and_transcribe_scene_derivation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            hw_path = Path(td) / "hotwords.txt"
            hw_path.write_text("[global]\nGlobTerm\n[chat]\nChatTerm\n[coding]\nCodeTerm\n", encoding="utf-8")

            self.assertEqual(asr_load_hotwords_file(str(hw_path), scene="coding"), ["CodeTerm", "GlobTerm"])
            self.assertEqual(_coerce_context(None, str(hw_path)).hotwords, ["GlobTerm"])
            self.assertEqual(
                _coerce_context(AsrContext(mode="general", target_hint="im_chat"), str(hw_path)).hotwords,
                ["ChatTerm", "GlobTerm"],
            )
            self.assertEqual(
                _coerce_context({"mode": "agent", "hotwords": ["DynTerm"]}, str(hw_path)).hotwords,
                ["CodeTerm", "GlobTerm", "DynTerm"],
            )
            # Preloaded context is NOT re-merged out of order
            pre = AsrContext(hotwords=["CodeTerm", "GlobTerm", "DynTerm"], hotwords_scope="coding", manual_hotwords_loaded=True)
            self.assertEqual(_coerce_context(pre, str(hw_path)).hotwords, ["CodeTerm", "GlobTerm", "DynTerm"])

            # Direct transcribe() call with DashScope verifies hotwords flow end-to-end
            captured: list[HttpRequest] = []

            def _fake_tx(req: HttpRequest) -> HttpResponse:
                captured.append(req)
                return HttpResponse(200, {"content-type": "application/json"}, json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode("utf-8"))

            cfg = Config()
            cfg.asr.provider = "dashscope"
            cfg.asr.hotwords_file = str(hw_path)
            res = transcribe(
                _voiced_wav(),
                cfg,
                env={"DASHSCOPE_API_KEY": "sk-dummy-test-key"},
                context=AsrContext(mode="agent", hotwords=["DynTerm"]),
                transport=_fake_tx,
            )
            self.assertEqual(res.text, "ok")
            body = json.loads(captured[0].body.decode("utf-8"))
            sys_txt = body["messages"][0]["content"][0]["text"]
            self.assertIn("CodeTerm、GlobTerm、DynTerm", sys_txt)

            # Direct single-hop transcribe() on Gemini & ChatAudioAsrProvider uses 480-char single-hop budget without duplication
            # 22 terms * 19 chars + 21 * 2 = 460 chars (> 413 DashScope cap, <= 480 single-hop cap)
            sh_terms = [f"SingleHopTermId_{i:03d}" for i in range(22)]
            gem_captured: list[HttpRequest] = []

            def _gem_tx(req: HttpRequest) -> HttpResponse:
                gem_captured.append(req)
                return HttpResponse(
                    200,
                    {"content-type": "application/json"},
                    json.dumps({"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}).encode("utf-8"),
                )

            cfg_gem = Config()
            cfg_gem.asr.provider = "gemini"
            cfg_gem.asr.hotwords_file = ""
            transcribe(
                _voiced_wav(),
                cfg_gem,
                env={"GEMINI_API_KEY": "AIza-fake-gemini-key"},
                context=AsrContext(hotwords=sh_terms, dialog_text="讨论单跳热词"),
                transport=_gem_tx,
            )
            gem_body = json.loads(gem_captured[0].body.decode("utf-8"))
            gem_ctx_txt = gem_body["contents"][0]["parts"][0]["text"]
            self.assertIn('<vocabulary_hints read_only="true">', gem_ctx_txt)
            self.assertIn(sh_terms[-1], gem_ctx_txt)
            self.assertNotIn("常用技术术语：", gem_ctx_txt)

            chat_captured: list[HttpRequest] = []

            def _chat_tx(req: HttpRequest) -> HttpResponse:
                chat_captured.append(req)
                return HttpResponse(
                    200,
                    {"content-type": "application/json"},
                    json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode("utf-8"),
                )

            cfg_chat = Config()
            cfg_chat.asr.provider = "openrouter"
            cfg_chat.asr.model = "google/gemini-3.5-flash-lite"
            cfg_chat.asr.hotwords_file = ""
            transcribe(
                _voiced_wav(),
                cfg_chat,
                env={"OPENROUTER_API_KEY": "sk-or-fake-openrouter-key"},
                context=AsrContext(hotwords=sh_terms, dialog_text="讨论单跳热词"),
                transport=_chat_tx,
            )
            chat_body = json.loads(chat_captured[0].body.decode("utf-8"))
            chat_ctx_txt = chat_body["messages"][1]["content"][0]["text"]
            self.assertIn('<vocabulary_hints read_only="true">', chat_ctx_txt)
            self.assertIn(sh_terms[-1], chat_ctx_txt)
            self.assertNotIn("常用技术术语：", chat_ctx_txt)


class HotwordsCliTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_hotwords_cache()

    def tearDown(self) -> None:
        clear_hotwords_cache()

    def _run_cli(self, argv: list[str], env: dict[str, str]) -> tuple[int, str, str]:
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        old_env = dict(os.environ)
        try:
            os.environ.clear()
            os.environ.update(env)
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                rc = cli_main(argv)
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        return rc, out_buf.getvalue(), err_buf.getvalue()

    def test_cli_hotwords_list_add_remove_json_and_errors(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            home.mkdir()
            hw_path = tmp / "custom_hotwords.txt"
            cfg_path = tmp / "config.toml"
            cfg_path.write_text(
                f'[asr]\nprovider = "doubao"\nhotwords_file = "{hw_path}"\n',
                encoding="utf-8",
            )
            env = {
                "HOME": str(home),
                "DOUBAO_ASR_APP_ID": "test-app-id",
                "DOUBAO_ASR_API_KEY": "test-access-key",
            }

            # 1. list when file does not exist yet
            rc, out, err = self._run_cli(["hotwords", "list", "--config", str(cfg_path)], env)
            self.assertEqual(rc, 0, err)
            self.assertIn("not created yet", out)
            self.assertIn("doubao (global): 0/30 terms, 0/180 chars", out)

            # 2. add to global and coding
            rc, out, err = self._run_cli(
                ["hotwords", "add", "MyBrand", "C#", "--config", str(cfg_path)],
                env,
            )
            self.assertEqual(rc, 0, err)
            self.assertIn("Added (2):   MyBrand, C#", out)
            self.assertIn("doubao: 2/30 terms", out)

            rc, out, err = self._run_cli(
                ["hotwords", "add", "KestrelRouter", "MyBrand", "--scope", "coding", "--config", str(cfg_path)],
                env,
            )
            self.assertEqual(rc, 0, err)
            self.assertIn("scope=coding", out)

            # Duplicate add in same scope reports Skipped
            rc, out, err = self._run_cli(
                ["hotwords", "add", "kestrelrouter", "--scope", "coding", "--json", "--config", str(cfg_path)],
                env,
            )
            self.assertEqual(rc, 0, err)
            add_json = json.loads(out)
            self.assertFalse(add_json["changed"])
            self.assertEqual(add_json["skipped"], ["kestrelrouter"])

            # Add to chat scope
            rc, out, err = self._run_cli(
                ["hotwords", "add", "小明", "--scope", "chat", "--config", str(cfg_path)],
                env,
            )
            self.assertEqual(rc, 0, err)

            # 3. list --json (all / global / coding)
            rc, out, err = self._run_cli(["hotwords", "list", "--json", "--config", str(cfg_path)], env)
            self.assertEqual(rc, 0, err)
            data_all = json.loads(out)
            self.assertTrue(data_all["exists"])
            self.assertEqual(data_all["active_asr"]["provider"], "doubao")
            self.assertEqual(data_all["scopes"]["global"], ["MyBrand", "C#"])
            self.assertEqual(data_all["scopes"]["chat"], ["小明"])
            self.assertEqual(data_all["scopes"]["coding"], ["KestrelRouter", "MyBrand"])
            self.assertEqual(data_all["mounted"]["coding"], ["KestrelRouter", "MyBrand", "C#"])

            rc, out, err = self._run_cli(
                ["hotwords", "list", "--scope", "coding", "--config", str(cfg_path)],
                env,
            )
            self.assertEqual(rc, 0, err)
            self.assertIn("[coding]", out)
            self.assertNotIn("[chat]", out)

            rc, out, err = self._run_cli(
                ["hotwords", "list", "--scope", "global", "--json", "--config", str(cfg_path)],
                env,
            )
            self.assertEqual(rc, 0, err)
            self.assertEqual(list(json.loads(out)["scopes"].keys()), ["global"])

            # 4. remove from single scope vs --scope all
            rc, out, err = self._run_cli(
                ["hotwords", "remove", "小明", "NotHere", "--scope", "chat", "--config", str(cfg_path)],
                env,
            )
            self.assertEqual(rc, 0, err)
            self.assertIn("Removed (1):   小明", out)
            self.assertIn("Not found (1): NotHere", out)

            rc, out, err = self._run_cli(
                ["hotwords", "remove", "mybrand", "--scope", "all", "--json", "--config", str(cfg_path)],
                env,
            )
            self.assertEqual(rc, 0, err)
            rm_json = json.loads(out)
            self.assertTrue(rm_json["changed"])
            self.assertEqual(rm_json["removed"], ["MyBrand"])

            # 5. Overflow truncation warning shown on Doubao
            many = [f"OverTerm{i:02d}" for i in range(35)]
            rc, out, err = self._run_cli(
                ["hotwords", "add", *many, "--scope", "coding", "--config", str(cfg_path)],
                env,
            )
            self.assertEqual(rc, 0, err)
            self.assertIn("TRUNCATED", out)

            # 6. Invalid term returns exit code 2 (config error)
            rc, out, err = self._run_cli(
                ["hotwords", "add", "[badheader]", "--config", str(cfg_path)],
                env,
            )
            self.assertEqual(rc, 2)
            self.assertIn("section header", err)

            # 7. Default DashScope (qwen3-asr-flash) CLI quota matches 413-char DashScope route (NOT 480-char single-hop)
            rc, out, err = self._run_cli(
                ["hotwords", "list", "--asr", "dashscope", "--json", "--config", str(cfg_path)],
                {**env, "DASHSCOPE_API_KEY": "sk-dummy-dashscope"},
            )
            self.assertEqual(rc, 0, err)
            ds_json = json.loads(out)
            self.assertEqual(ds_json["budgets"]["active"]["reference"]["spec_key"], "dashscope")
            self.assertEqual(ds_json["budgets"]["active"]["reference"]["max_chars"], 413)

            rc, out, err = self._run_cli(
                [
                    "hotwords",
                    "list",
                    "--asr",
                    "dashscope",
                    "--asr-model",
                    "qwen3.8-omni-flash",
                    "--json",
                    "--config",
                    str(cfg_path),
                ],
                {**env, "DASHSCOPE_API_KEY": "sk-dummy-dashscope"},
            )
            self.assertEqual(rc, 0, err)
            omni_json = json.loads(out)
            self.assertEqual(omni_json["budgets"]["active"]["reference"]["spec_key"], "single_hop")
            self.assertEqual(omni_json["budgets"]["active"]["reference"]["max_chars"], 480)

            # 8. Empty hotwords_file in config returns exit code 2
            empty_cfg = tmp / "empty_hw.toml"
            empty_cfg.write_text('[asr]\nhotwords_file = ""\n', encoding="utf-8")
            rc, out, err = self._run_cli(["hotwords", "list", "--config", str(empty_cfg)], env)
            self.assertEqual(rc, 2)
            self.assertIn("asr.hotwords_file is empty", err)

    def test_cli_posix_c_locale_and_minimal_path_cjk_hotwords(self) -> None:
        # 1. Direct unit checks for surrogate recovery, stdio reconfiguration guard, and path preservation
        surrogate_zh = "豆包".encode("utf-8").decode("ascii", errors="surrogateescape")
        surrogate_cfg = "/tmp/" + "中文配置".encode("utf-8").decode("ascii", errors="surrogateescape") + "/config.toml"
        self.assertEqual(cli_mod._recover_utf8_surrogates(surrogate_zh), "豆包")
        self.assertEqual(cli_mod._recover_utf8_surrogates("Codex"), "Codex")
        bad_bytes = b"\xff\xfe".decode("ascii", errors="surrogateescape")
        self.assertEqual(cli_mod._recover_utf8_surrogates(bad_bytes), bad_bytes)

        reconfigured: list[tuple[str, str]] = []

        class _AsciiStream:
            encoding = "ANSI_X3.4-1968"

            def reconfigure(self, *, encoding: str, errors: str) -> None:
                reconfigured.append((encoding, errors))

        # Upgrades stdio only when POSIX locale is ASCII and PYTHONIOENCODING is unset
        with (
            mock.patch.object(sys, "stdout", _AsciiStream()),
            mock.patch.object(sys, "stderr", _AsciiStream()),
            mock.patch.object(cli_mod.locale, "getpreferredencoding", return_value="ANSI_X3.4-1968"),
        ):
            cli_mod._configure_posix_ascii_stdio(env={})
        self.assertEqual(reconfigured, [("utf-8", "replace"), ("utf-8", "replace")])

        # Respects explicit PYTHONIOENCODING even under ASCII locale
        reconfigured.clear()
        with (
            mock.patch.object(sys, "stdout", _AsciiStream()),
            mock.patch.object(sys, "stderr", _AsciiStream()),
            mock.patch.object(cli_mod.locale, "getpreferredencoding", return_value="ANSI_X3.4-1968"),
        ):
            cli_mod._configure_posix_ascii_stdio(env={"PYTHONIOENCODING": "ascii"})
        self.assertEqual(reconfigured, [])

        # Does not override stdio when locale is already UTF-8 (e.g. C.UTF-8)
        reconfigured.clear()
        with (
            mock.patch.object(sys, "stdout", _AsciiStream()),
            mock.patch.object(sys, "stderr", _AsciiStream()),
            mock.patch.object(cli_mod.locale, "getpreferredencoding", return_value="UTF-8"),
        ):
            cli_mod._configure_posix_ascii_stdio(env={})
        self.assertEqual(reconfigured, [])

        # Only text business fields are recovered; filesystem path arguments keep surrogateescape intact
        parsed_hw = cli_mod._build_parser().parse_args(
            ["hotwords", "add", surrogate_zh, "--config", surrogate_cfg]
        )
        cli_mod._recover_parsed_text_args(parsed_hw)
        self.assertEqual(parsed_hw.terms, ["豆包"])
        self.assertEqual(parsed_hw.config, surrogate_cfg)

        surrogate_title = "终端 · 中文会话".encode("utf-8").decode("ascii", errors="surrogateescape")
        surrogate_wav = "/tmp/" + "录音样本".encode("utf-8").decode("ascii", errors="surrogateescape") + ".wav"
        parsed_once = cli_mod._build_parser().parse_args(
            ["once", "--file", surrogate_wav, "--title", surrogate_title, "--config", surrogate_cfg]
        )
        cli_mod._recover_parsed_text_args(parsed_once)
        self.assertEqual(parsed_once.title, "终端 · 中文会话")
        self.assertEqual(parsed_once.wav_file, surrogate_wav)
        self.assertEqual(parsed_once.config, surrogate_cfg)

        parsed_retry = cli_mod._build_parser().parse_args(
            ["retry", surrogate_wav, "--title", surrogate_title, "--config", surrogate_cfg]
        )
        cli_mod._recover_parsed_text_args(parsed_retry)
        self.assertEqual(parsed_retry.title, "终端 · 中文会话")
        self.assertEqual(parsed_retry.target, surrogate_wav)
        self.assertEqual(parsed_retry.config, surrogate_cfg)

        # 2. Subprocess end-to-end verification under strict non-UTF-8 POSIX C locale + minimal PATH
        # Uses raw UTF-8 bytes for non-ASCII CLI args & paths so the test itself also passes when
        # the parent Python process runs under LC_ALL=C PYTHONCOERCECLOCALE=0 PYTHONUTF8=0.
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home_dir = tmp / "home"
            rt_dir = tmp / "run"
            home_dir.mkdir(parents=True)
            rt_dir.mkdir(parents=True, mode=0o700)

            # Create a non-ASCII UTF-8 directory and config file using bytes paths (safe under ASCII fsencode)
            cjk_cfg_dir_b = os.path.join(os.fsencode(tmp), "中文配置".encode("utf-8"))
            os.makedirs(cjk_cfg_dir_b, exist_ok=True)
            cjk_cfg_path_b = os.path.join(cjk_cfg_dir_b, b"config.toml")
            hw_path_ascii = tmp / "scoped_hotwords.txt"
            with open(cjk_cfg_path_b, "wb") as fh:
                fh.write(f'[asr]\nhotwords_file = "{hw_path_ascii.as_posix()}"\n'.encode("utf-8"))

            c_env = {
                "PATH": "/usr/bin:/bin",
                "HOME": str(home_dir),
                "XDG_CONFIG_HOME": str(home_dir / ".config"),
                "XDG_CACHE_HOME": str(tmp / "cache"),
                "XDG_RUNTIME_DIR": str(rt_dir),
                "LC_ALL": "C",
                "LANG": "C",
                "PYTHONCOERCECLOCALE": "0",
                "PYTHONUTF8": "0",
                "PYTHONPATH": str(SRC_DIR),
            }
            zh_term_b = "豆包".encode("utf-8")

            def _run(sub: list[str | bytes]) -> subprocess.CompletedProcess[bytes]:
                return subprocess.run(
                    [sys.executable, "-m", "agent_voice_input.cli", *sub],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    env=c_env,
                    timeout=10,
                    check=False,
                )

            for sub in (
                ["--help"],
                ["--version"],
                ["init"],
                ["hotwords", "add", "Codex", "--scope", "coding", "--config", cjk_cfg_path_b],
                ["hotwords", "add", zh_term_b, "--scope", "global", "--config", cjk_cfg_path_b],
                ["hotwords", "list", "--config", cjk_cfg_path_b],
                ["diagnose", "--json", "--config", cjk_cfg_path_b],
            ):
                proc = _run(sub)
                self.assertEqual(
                    proc.returncode,
                    0,
                    f"subcmd={sub!r} failed under LC_ALL=C: stderr={proc.stderr!r} stdout={proc.stdout!r}",
                )

            list_json = _run(["hotwords", "list", "--json", "--config", cjk_cfg_path_b])
            self.assertEqual(list_json.returncode, 0, list_json.stderr)
            payload = json.loads(list_json.stdout.decode("utf-8"))
            self.assertIn("豆包", payload["scopes"]["global"])
            self.assertIn("Codex", payload["scopes"]["coding"])
            self.assertEqual(hw_path_ascii.read_text(encoding="utf-8").count("豆包"), 1)

            rm_proc = _run(["hotwords", "remove", zh_term_b, "--scope", "global", "--config", cjk_cfg_path_b])
            self.assertEqual(rm_proc.returncode, 0, rm_proc.stderr)
            list_after = json.loads(
                _run(["hotwords", "list", "--json", "--config", cjk_cfg_path_b]).stdout.decode("utf-8")
            )
            self.assertNotIn("豆包", list_after["scopes"]["global"])


if __name__ == "__main__":
    unittest.main()
