"""Offline unit tests for flat personal hotwords, directory-locked edits, dual-track ordering, and Stage 1/2 projection."""
from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_voice_input.config import ConfigError
from agent_voice_input.hotwords import (
    HOTWORDS_MAX_BYTES,
    HOTWORDS_MAX_ENTRIES,
    HOTWORDS_QUOTA_SPECS,
    MAX_HOTWORD_ALIAS_CHARS,
    MAX_HOTWORD_ALIASES,
    MAX_HOTWORD_TERM_CHARS,
    MIN_HOTWORD_ALIAS_CHARS,
    SINGLE_HOP_HOTWORDS_MAX_CHARS,
    SINGLE_HOP_HOTWORDS_MAX_ITEMS,
    STAGE2_HOTWORDS_MAX_CHARS,
    STAGE2_HOTWORDS_MAX_ITEMS,
    STAGE2_VOCABULARY_HINTS_PREAMBLE,
    AmbiguousAlias,
    HotwordEntry,
    HotwordsLineIssue,
    HotwordsTable,
    add_hotword,
    add_hotwords,
    budget_hotwords_for_provider,
    budget_openai_keywords,
    budget_stage2_hotwords,
    clear_hotwords_cache,
    dedupe_hotwords_ci,
    format_budget_line,
    format_stage2_budget_line,
    format_stage2_entry_line,
    format_stage2_vocabulary_hints,
    load_hotwords_table,
    order_stage1_entries,
    order_stage2_entries,
    parse_hotwords_text,
    project_stage1_hotwords,
    remove_hotword,
    remove_hotwords,
    resolve_hotwords_quota_spec,
    resolve_personal_hotwords_path,
    sanitize_openai_keyword,
    summarize_stage2_hotwords_budget,
    validate_hotword_alias,
    validate_hotword_aliases,
    validate_hotword_term,
)
from agent_voice_input.llm_common import AsrContext


class PersonalHotwordsPathAndParserTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_hotwords_cache()

    def tearDown(self) -> None:
        clear_hotwords_cache()

    def test_resolve_personal_hotwords_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            custom = Path(td) / "custom_avi"
            self.assertEqual(
                resolve_personal_hotwords_path(env={"HOME": str(home)}),
                home / ".agent-voice-input" / "hotwords" / "personal.txt",
            )
            self.assertEqual(
                resolve_personal_hotwords_path(env={"HOME": str(home), "AGENT_VOICE_INPUT_HOME": str(custom)}),
                custom / "hotwords" / "personal.txt",
            )
            # Explicit home_dir takes precedence over AGENT_VOICE_INPUT_HOME
            self.assertEqual(
                resolve_personal_hotwords_path(
                    env={"AGENT_VOICE_INPUT_HOME": str(custom)},
                    home_dir=home,
                ),
                home / ".agent-voice-input" / "hotwords" / "personal.txt",
            )
            # Hermetic env without HOME or AGENT_VOICE_INPUT_HOME returns None
            self.assertIsNone(resolve_personal_hotwords_path(env={}))

    def test_validation_morphology_and_allowed_tech_symbols(self) -> None:
        # Allowed technical symbols must ALL pass without false rejections
        allowed_terms = (
            "C#",
            "F#",
            "C++",
            "Issue#42",
            "panic!",
            "Yahoo!",
            "!important",
            "AT&T",
            "$PATH",
            "CI/CD",
            "Next.js",
            "@scope/pkg",
            "foo::bar",
            "a|b",
            "why?",
            "A",  # 1-char canonical allowed
            "x" * MAX_HOTWORD_TERM_CHARS,  # exact 48 chars
        )
        for t in allowed_terms:
            self.assertEqual(validate_hotword_term(f"  {t}  "), t)

        # Disallowed terms
        for bad in (
            123,
            "",
            "   ",
            "x" * (MAX_HOTWORD_TERM_CHARS + 1),
            "bad\nline",
            "bad\rline",
            "bad\tword",
            "bad\x00null",
            "bad\x85nel",
            "Good\u2028Injected",
            "Good\u2029Injected",
            "<script>",
            "a>b",
            "这是一个句子。",
            "感叹句！",
            "问句？",
            "分号；",
            "[coding]",
            "#comment",
            "foo # bar",
            "Harness <- 哈尼斯",
        ):
            with self.assertRaises(ValueError, msg=f"expected ValueError for term {bad!r}"):
                validate_hotword_term(bad)  # type: ignore[arg-type]

        # Valid aliases (2..24 chars) including allowed symbols
        self.assertEqual(validate_hotword_alias("  哈尼斯  "), "哈尼斯")
        self.assertEqual(validate_hotword_alias("a" * MIN_HOTWORD_ALIAS_CHARS), "aa")
        self.assertEqual(validate_hotword_alias("a" * MAX_HOTWORD_ALIAS_CHARS), "a" * MAX_HOTWORD_ALIAS_CHARS)
        self.assertEqual(validate_hotword_alias("AT&T!"), "AT&T!")

        # Disallowed aliases
        for bad_al in (
            123,
            "",
            "   ",
            "哈",  # 1 char rejected
            "a",
            "a" * (MAX_HOTWORD_ALIAS_CHARS + 1),  # 25 chars rejected
            "foo\nbar",
            "foo\rbar",
            "foo\u2028bar",
            "foo,bar",
            "foo，bar",
            "foo <- bar",
            "#comment",
            "foo # comment",
            "<bad>",
            "a>b",
            "整句别名。",
            "疑问别名？",
            "感叹别名！",
            "分号别名；",
        ):
            with self.assertRaises(ValueError, msg=f"expected ValueError for alias {bad_al!r}"):
                validate_hotword_alias(bad_al)  # type: ignore[arg-type]

        # validate_hotword_aliases splitting, dedupe, 2-char non-blocking warning, >5 & conflict checks
        clean, warns = validate_hotword_aliases("Harness", ["哈尼, 哈尼斯，哈尼"])
        self.assertEqual(clean, ("哈尼", "哈尼斯"))
        self.assertEqual(len(warns), 1)
        self.assertIn("哈尼", warns[0])

        for bad_batch in (
            [123],  # type: ignore[list-item]
            ["ok\nnewline"],
            ["哈尼斯, "],
            ["Harness"],
            ["harness"],
            ["ExistingCanon"],
            ["a1", "a2", "a3", "a4", "a5", "a6"],
        ):
            with self.assertRaises(ValueError, msg=f"expected ValueError for batch {bad_batch!r}"):
                validate_hotword_aliases(
                    "Harness",
                    bad_batch,  # type: ignore[arg-type]
                    existing_canonical_lows={"existingcanon"},
                )

    def test_flat_parser_pin_escape_crlf_unicode_seps_and_legacy_sections(self) -> None:
        raw = (
            "\ufeff# Header comment\r\n"
            "\r\n"
            "[global]  # legacy section 1\r\n"
            "C#  # preserve hash in token\r\n"
            "!Harness <- 哈尼斯, 哈尼思  # pinned with aliases\r\n"
            r"\!important <- 感叹重要" + "\r\n"
            r"!\!important2 <- 置顶感叹" + "\r\n"
            "panic!\r\n"
            "!Yahoo!\r\n"
            "AT&T\r\n"
            "$PATH\r\n"
            "CI/CD\r\n"
            "Next.js\r\n"
            "@scope/pkg\r\n"
            "foo::bar\r\n"
            "EmptyRight <-   \r\n"
            "EmptyPiece <- 别名甲, , 别名乙, EmptyPiece\r\n"
            "[coding]\r\n"
            "[GLOBAL]\r\n"  # duplicate legacy section CI
            "Good\u2028InjectedTerm\r\n"  # must NOT be split into 2 lines!
            "Bad\u2029Split\r\n"
            "Bad\x85Nel\r\n"
            "<xml_injected>\r\n"
            "这是一整句中文。\r\n"
            "BadAlias1 <- 哈\r\n"
            "BadNested <- 正常别名, foo <- bar\r\n"
            "<- OrphanAlias\r\n"
            "!\r\n"
        )
        table = parse_hotwords_text(raw)
        self.assertEqual(table.legacy_sections, ("[global]", "[coding]"))
        self.assertEqual(
            table.terms,
            (
                "C#",
                "Harness",
                "!important",
                "!important2",
                "panic!",
                "Yahoo!",
                "AT&T",
                "$PATH",
                "CI/CD",
                "Next.js",
                "@scope/pkg",
                "foo::bar",
                "EmptyRight",
                "EmptyPiece",
            ),
        )
        # Ensure U+2028/U+2029 did NOT split out InjectedTerm or Split
        self.assertNotIn("InjectedTerm", table.terms)
        self.assertNotIn("Split", table.terms)
        self.assertEqual(len(table.invalid_lines), 9)
        self.assertIsInstance(table.invalid_lines[0], HotwordsLineIssue)
        self.assertEqual(table.invalid_lines[0].as_dict()["line_no"], table.invalid_lines[0].line_no)

        by_term = {e.term: e for e in table.entries}
        self.assertTrue(by_term["Harness"].pinned)
        self.assertEqual(by_term["Harness"].aliases, ("哈尼思", "哈尼斯"))
        self.assertFalse(by_term["!important"].pinned)
        self.assertEqual(by_term["!important"].format_line(), r"\!important <- 感叹重要")
        self.assertTrue(by_term["!important2"].pinned)
        self.assertEqual(by_term["!important2"].format_line(), r"!\!important2 <- 置顶感叹")
        self.assertFalse(by_term["panic!"].pinned)
        self.assertEqual(by_term["panic!"].format_line(), "panic!")
        self.assertTrue(by_term["Yahoo!"].pinned)
        self.assertEqual(by_term["Yahoo!"].format_line(), "!Yahoo!")
        self.assertEqual(by_term["EmptyRight"].aliases, ())
        self.assertEqual(by_term["EmptyPiece"].aliases, ("别名乙", "别名甲"))

        # Round-trip formatted lines through parser
        roundtrip_text = "\n".join(e.format_line() for e in table.entries) + "\n"
        rt_table = parse_hotwords_text(roundtrip_text)
        self.assertEqual(rt_table.entries, table.entries)
        self.assertEqual(rt_table.warnings_count, 0)

    def test_git_conflict_blocks_diff3_unclosed_and_decorative_separator(self) -> None:
        raw = (
            "# =======\n"
            "=======\n"  # standalone decorative line without <<<<<<< must NOT trigger conflict
            "BeforeTerm\n"
            "<<<<<<< HEAD\n"
            "LeftSideTerm\n"
            "||||||| merged common ancestors\n"
            "BaseSideTerm\n"
            "=======\n"
            "RightSideTerm\n"
            ">>>>>>> feature/branch\n"
            "MiddleTerm\n"
            "<<<<<<< outer\n"
            "OuterLeft\n"
            "<<<<<<< inner\n"
            "InnerLeft\n"
            ">>>>>>> inner\n"
            "StillInOuter\n"
            ">>>>>>> outer\n"
            "AfterNested\n"
            "<<<<<<< unclosed\n"
            "NeverClosedLeft\n"
            "=======\n"
            "NeverClosedRight\n"
        )
        table = parse_hotwords_text(raw)
        self.assertEqual(table.terms, ("=======", "BeforeTerm", "MiddleTerm", "AfterNested"))
        self.assertEqual(table.conflict_lines, (4, 12, 20))
        for skipped in ("LeftSideTerm", "BaseSideTerm", "RightSideTerm", "OuterLeft", "InnerLeft", "StillInOuter", "NeverClosedLeft", "NeverClosedRight"):
            self.assertNotIn(skipped, table.terms)
        self.assertGreater(table.warnings_count, 0)
        self.assertTrue(any("git conflict" in w for w in table.diagnostic_warnings()))

    def test_two_pass_duplicate_canonical_adjudication_and_ambiguous_aliases(self) -> None:
        # 1. Duplicate canonical casing: pinned wins over unpinned; otherwise lexicographically smallest wins regardless of order
        raw_a = (
            "harness <- 别名6, 别名5, 别名4, 别名3, 别名2, 别名1, 共享误听, CanonB\n"
            "!Harness <- 别名0\n"
            "alpha <- 共享误听\n"
            "Alpha\n"
            "CanonB\n"
        )
        raw_b = (
            "CanonB\n"
            "Alpha\n"
            "alpha <- 共享误听\n"
            "!Harness <- 别名0\n"
            "harness <- 别名6, 别名5, 别名4, 别名3, 别名2, 别名1, 共享误听, CanonB\n"
        )
        t_a = parse_hotwords_text(raw_a)
        t_b = parse_hotwords_text(raw_b)

        # Deterministic winning spelling & pinned flag
        map_a = {e.term.lower(): e for e in t_a.entries}
        map_b = {e.term.lower(): e for e in t_b.entries}
        self.assertEqual(map_a, map_b)
        self.assertEqual(map_a["harness"].term, "Harness")
        self.assertTrue(map_a["harness"].pinned)
        self.assertEqual(map_a["alpha"].term, "Alpha")
        self.assertFalse(map_a["alpha"].pinned)

        # Ambiguous alias '共享误听' hidden on BOTH Harness and Alpha, canonical terms preserved!
        self.assertEqual(
            t_a.ambiguous_aliases,
            (AmbiguousAlias(alias="共享误听", terms=("Alpha", "Harness")),),
        )
        self.assertEqual(t_a.ambiguous_aliases[0].as_dict(), {"alias": "共享误听", "terms": ["Alpha", "Harness"]})
        self.assertEqual(map_a["alpha"].aliases, ())
        # Canonical collision 'CanonB' dropped + warned; Pass B sorts & slices first 5 AFTER filtering!
        self.assertEqual(map_a["harness"].aliases, ("别名0", "别名1", "别名2", "别名3", "别名4"))
        self.assertEqual(len(t_a.alias_warnings), 1)
        self.assertIn("CanonB", t_a.alias_warnings[0])

        # Ambiguous alias beyond the 5th alias on a line is STILL caught in Pass A before 5-cap!
        raw_late_ambig = (
            "TermOne <- a1, a2, a3, a4, a5, 晚出现歧义\n"
            "TermTwo <- 晚出现歧义\n"
        )
        t_late = parse_hotwords_text(raw_late_ambig)
        self.assertEqual(
            t_late.ambiguous_aliases,
            (AmbiguousAlias(alias="晚出现歧义", terms=("TermOne", "TermTwo")),),
        )
        self.assertEqual({e.term: e.aliases for e in t_late.entries}["TermTwo"], ())

        # Unpinned duplicate: lexicographically smaller casing wins even if listed second
        t_case1 = parse_hotwords_text("kestrel\nKestrel\n")
        t_case2 = parse_hotwords_text("Kestrel\nkestrel\n")
        self.assertEqual(t_case1.terms, ("Kestrel",))
        self.assertEqual(t_case2.terms, ("Kestrel",))
        self.assertEqual(dedupe_hotwords_ci(["  Foo ", "foo", "", "Bar"]), ["Foo", "Bar"])

    def test_entry_limit_2000_keeps_scanning_conflicts_and_total_count(self) -> None:
        lines = [f"Term{i:04d}" for i in range(HOTWORDS_MAX_ENTRIES + 5)]
        # Duplicate of an earlier entry after 2000 should merge without inflating total_valid_entries
        lines.append("!Term0001")
        lines.extend(["<<<<<<< HEAD", "BadLeft", "=======", "BadRight", ">>>>>>> branch"])
        table = parse_hotwords_text("\n".join(lines) + "\n")

        self.assertEqual(len(table.entries), HOTWORDS_MAX_ENTRIES)
        self.assertTrue(table.entry_limit_exceeded)
        self.assertEqual(table.total_valid_entries, HOTWORDS_MAX_ENTRIES + 5)
        self.assertTrue(table.entries[1].pinned)  # Term0001 merged duplicate pin!
        self.assertEqual(table.conflict_lines, (HOTWORDS_MAX_ENTRIES + 7,))
        self.assertTrue(any("exceeded 2000 limit" in w for w in table.diagnostic_warnings()))


class ReadEditSeparationLockAndSymlinkTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_hotwords_cache()

    def tearDown(self) -> None:
        clear_hotwords_cache()

    def test_directory_symlink_allowed_leaf_file_symlink_rejected_and_zero_lockfiles(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            app_home = home / ".agent-voice-input"
            app_home.mkdir(parents=True, mode=0o700)
            env = {"HOME": str(home)}

            # Real hotwords directory (e.g. inside an external dotfiles repo) with 0755 permissions
            real_hw_dir = tmp / "harness_hotwords"
            real_hw_dir.mkdir(mode=0o755)
            mode_before = stat.S_IMODE(real_hw_dir.stat().st_mode)

            # Symlink ~/.agent-voice-input/hotwords -> real_hw_dir
            sym_hw_dir = app_home / "hotwords"
            sym_hw_dir.symlink_to(real_hw_dir)
            personal = sym_hw_dir / "personal.txt"

            # Add through directory symlink succeeds, creates 0600 file, leaves ZERO .lock files, never chmods target dir
            r1 = add_hotwords(personal, ["Harness", "!important"], env=env)
            self.assertTrue(r1.changed)
            self.assertEqual(r1.added, ("Harness", "!important"))
            self.assertEqual(stat.S_IMODE(personal.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(real_hw_dir.stat().st_mode), mode_before)
            self.assertEqual(sorted(p.name for p in real_hw_dir.iterdir()), ["personal.txt"])

            # Reading via symlink path and real path share the same realpath cache key
            t_sym = load_hotwords_table(personal, env=env, use_cache=True)
            t_real = load_hotwords_table(real_hw_dir / "personal.txt", env=env, use_cache=True)
            self.assertEqual(t_sym.terms, ("Harness", "!important"))
            self.assertEqual(t_real.terms, ("Harness", "!important"))

            # Writing via real path invalidates cache seen via symlink path immediately!
            r2 = add_hotword(real_hw_dir / "personal.txt", "Ghostty", pin=True, env=env)
            self.assertTrue(r2.changed)
            self.assertEqual(r2.pinned, ("Ghostty",))
            t_sym2 = load_hotwords_table(personal, env=env, use_cache=True)
            self.assertEqual(t_sym2.terms, ("Harness", "!important", "Ghostty"))

            # Default None path resolves via env automatically
            t_default = load_hotwords_table(None, env=env, use_cache=True)
            self.assertEqual(t_default.terms, ("Harness", "!important", "Ghostty"))
            self.assertEqual(load_hotwords_table(None, env={}), HotwordsTable(path=None, exists=False))
            self.assertEqual(load_hotwords_table(""), HotwordsTable(path=None, exists=False))

            # Leaf file symlink is REFUSED on BOTH read and write!
            other_file = tmp / "outside.txt"
            other_file.write_text("SneakyTerm\n", encoding="utf-8")
            leaf_sym = real_hw_dir / "leaf_sym.txt"
            leaf_sym.symlink_to(other_file)

            t_leaf = load_hotwords_table(leaf_sym, env=env, use_cache=False)
            self.assertFalse(t_leaf.exists)
            self.assertEqual(t_leaf.entries, ())
            self.assertEqual(len(t_leaf.file_warnings), 1)
            self.assertIn("symlink the hotwords/ directory instead", t_leaf.file_warnings[0])

            with self.assertRaises(ConfigError) as ctx_add:
                add_hotwords(leaf_sym, ["Injected"], env=env)
            self.assertIn(
                f"refusing to write hotwords through symlink {leaf_sym}; symlink the hotwords/ directory instead",
                str(ctx_add.exception),
            )
            with self.assertRaises(ConfigError) as ctx_rm:
                remove_hotwords(leaf_sym, ["SneakyTerm"], env=env)
            self.assertIn("symlink the hotwords/ directory instead", str(ctx_rm.exception))

            # Broken directory symlink on read degrades to empty table with file_warnings; on edit raises ConfigError
            broken_dir = tmp / "broken_hw_dir"
            broken_dir.symlink_to(tmp / "does_not_exist")
            t_broken = load_hotwords_table(broken_dir / "personal.txt", env=env)
            self.assertFalse(t_broken.exists)
            self.assertEqual(len(t_broken.file_warnings), 1)
            with self.assertRaises(ConfigError):
                add_hotwords(broken_dir / "personal.txt", ["Term"], env=env)

            # Directory as target file: read returns empty table, edit raises ConfigError
            dir_as_file = tmp / "dir_as_file"
            dir_as_file.mkdir()
            self.assertEqual(load_hotwords_table(dir_as_file, env=env), HotwordsTable(path=dir_as_file, exists=False))
            with self.assertRaises(ConfigError):
                add_hotwords(dir_as_file, ["Term"], env=env)

            # First-time add without prior init creates app_home & hotwords with 0700 automatically
            fresh_home = tmp / "fresh_home"
            fresh_path = fresh_home / ".agent-voice-input" / "hotwords" / "personal.txt"
            r_fresh = add_hotword(fresh_path, "FirstTerm")
            self.assertTrue(r_fresh.changed)
            self.assertEqual(stat.S_IMODE((fresh_home / ".agent-voice-input").stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((fresh_home / ".agent-voice-input" / "hotwords").stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(fresh_path.stat().st_mode), 0o600)

    def test_256kib_bounded_read_and_edit_side_guardrails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)

            # 1. Exact 256 KiB without trailing newline: NOT size_limit_exceeded, last line kept!
            exact_p = tmp / "exact_256k.txt"
            body_prefix = ("# " + "a" * 60 + "\n") * 4000  # 63 * 4000 = 252,000 bytes
            rem = HOTWORDS_MAX_BYTES - len(body_prefix.encode("utf-8"))
            # Add padding comment lines + final term without trailing '\n'
            pad_lines = ("# " + "b" * 97 + "\n") * ((rem - 10) // 100)
            leftover = HOTWORDS_MAX_BYTES - len((body_prefix + pad_lines).encode("utf-8"))
            final_term = "Z" * leftover
            exact_bytes = (body_prefix + pad_lines + final_term).encode("utf-8")
            self.assertEqual(len(exact_bytes), HOTWORDS_MAX_BYTES)
            exact_p.write_bytes(exact_bytes)

            t_exact = load_hotwords_table(exact_p, use_cache=False)
            self.assertFalse(t_exact.size_limit_exceeded)
            self.assertEqual(t_exact.terms, (final_term,))

            # Adding another term that would push past 256 KiB is rejected before writing!
            with self.assertRaises(ConfigError):
                add_hotword(exact_p, "OverflowTerm")
            self.assertEqual(exact_p.read_bytes(), exact_bytes)

            # 2. > 256 KiB with multi-byte UTF-8 straddling the 256 KiB boundary:
            #    read-side backtracks to last '\n' cleanly (no UnicodeDecodeError!) and sets size_limit_exceeded
            over_p = tmp / "over_256k.txt"
            line_good = "GoodPrefixTerm\n"
            cjk_line = "中文热词测试" * 5 + "\n"  # 30 chars, 91 UTF-8 bytes
            repeat = (HOTWORDS_MAX_BYTES // len(cjk_line.encode("utf-8"))) + 5
            over_bytes = (line_good + "# " + "x" * 10 + "\n" + cjk_line * repeat).encode("utf-8")
            self.assertGreater(len(over_bytes), HOTWORDS_MAX_BYTES)
            over_p.write_bytes(over_bytes)

            t_over = load_hotwords_table(over_p, use_cache=False)
            self.assertTrue(t_over.size_limit_exceeded)
            self.assertEqual(t_over.encoding_error, "")
            self.assertIn("GoodPrefixTerm", t_over.terms)
            self.assertTrue(any("256 KiB" in w for w in t_over.diagnostic_warnings()))

            # Edit-side REFUSES to touch > 256 KiB files
            with self.assertRaises(ConfigError) as exc_over:
                add_hotword(over_p, "NewTerm")
            self.assertIn("256 KiB", str(exc_over.exception))
            with self.assertRaises(ConfigError):
                remove_hotword(over_p, "GoodPrefixTerm")
            self.assertEqual(over_p.read_bytes(), over_bytes)

            # 3. Single >256 KiB line with NO '\n' degrades cleanly to empty entries + size_limit_exceeded
            no_nl_p = tmp / "no_nl_256k.txt"
            no_nl_p.write_bytes(b"A" * (HOTWORDS_MAX_BYTES + 10))
            t_no_nl = load_hotwords_table(no_nl_p, use_cache=False)
            self.assertTrue(t_no_nl.size_limit_exceeded)
            self.assertEqual(t_no_nl.entries, ())

            # 4. >256 KiB file with NUL bytes still reports encoding error
            nul_big = tmp / "nul_big.txt"
            nul_big.write_bytes(b"\xff\xfeA\x00" + b"B\x00" * (HOTWORDS_MAX_BYTES // 2 + 10))
            with contextlib.redirect_stderr(io.StringIO()):
                t_nul_big = load_hotwords_table(nul_big, use_cache=False)
            self.assertTrue(t_nul_big.size_limit_exceeded)
            self.assertIn("UTF-16 LE", t_nul_big.encoding_error)

            # 5. Edit-side refuses files with >2,000 entries OR unresolved git conflicts
            many_p = tmp / "over_2000.txt"
            many_p.write_text("\n".join(f"T{i:04d}" for i in range(HOTWORDS_MAX_ENTRIES + 2)) + "\n", encoding="utf-8")
            with self.assertRaises(ConfigError) as exc_2000:
                add_hotword(many_p, "Extra")
            self.assertIn("2000", str(exc_2000.exception))

            # File at exactly 2000 entries rejects adding a new entry (both with & without aliases)
            at_2000 = tmp / "at_2000.txt"
            at_2000.write_text("\n".join(f"W{i:04d}" for i in range(HOTWORDS_MAX_ENTRIES)) + "\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                add_hotword(at_2000, "OverflowPlain")
            with self.assertRaises(ConfigError):
                add_hotword(at_2000, "OverflowAliased", aliases=["超限别名"])

            conflict_p = tmp / "conflict.txt"
            conflict_p.write_text(
                "SafeTerm\n<<<<<<< HEAD\nLeft\n=======\nRight\n>>>>>>> main\n",
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError) as exc_conf:
                add_hotword(conflict_p, "NewTerm")
            self.assertIn("git conflict", str(exc_conf.exception))
            with self.assertRaises(ConfigError):
                remove_hotword(conflict_p, "SafeTerm")

    def test_edit_pin_upgrade_bidirectional_conflict_check_and_remove_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = tmp / "personal.txt"

            # Empty input on non-existent file does not create file
            self.assertFalse(add_hotwords(p, []).changed)
            self.assertFalse(p.exists())
            rm_empty = remove_hotwords(p, ["Missing"])
            self.assertFalse(rm_empty.changed)
            self.assertEqual(rm_empty.not_found, ("Missing",))

            # Aliases with != 1 term rejected
            with self.assertRaises(ValueError):
                add_hotwords(p, [], aliases=["哈尼斯"])
            with self.assertRaises(ValueError):
                add_hotwords(p, ["A", "B"], aliases=["哈尼斯"])

            # Add plain + pinned + escaped '!' + aliased entries preserving BOM & CRLF
            p.write_bytes(
                b"\xef\xbb\xbf# Personal vocabulary\r\n"
                b"  Harness <- " + "哈尼斯".encode("utf-8") + b"  # keep note\r\n"
                b"Alpha <- " + "共享歧义".encode("utf-8") + b"\r\n"
                b"Beta <- " + "共享歧义".encode("utf-8") + b"\r\n"
                b"PlainOne\r\n"
                b"plainone\r\n"
                b"Invalid<Line>\r\n"
                b"[legacy_sec]\r\n"
                b"\\!important"
            )

            # Bidirectional check catches HIDDEN ambiguous alias ('共享歧义')!
            with self.assertRaises(ValueError) as e1:
                add_hotword(p, "Gamma", aliases=["共享歧义"])
            self.assertIn("conflicts with existing mishearing alias", str(e1.exception))
            with self.assertRaises(ValueError) as e2:
                add_hotword(p, "共享歧义")
            self.assertIn("conflicts with existing mishearing alias", str(e2.exception))
            # Adding alias matching existing canonical term rejected
            with self.assertRaises(ValueError):
                add_hotword(p, "Harness", aliases=["Beta"])

            # In-place pin upgrade + alias merge preserves indent, inline comment, BOM, CRLF
            r_up = add_hotword(p, "harness", aliases=["哈尼思", "哈尼斯"], pin=True)
            self.assertTrue(r_up.changed)
            self.assertEqual(r_up.updated, ("Harness",))
            self.assertEqual(r_up.aliases_added, ("哈尼思",))
            self.assertEqual(r_up.pinned, ("Harness",))
            self.assertIn(
                b"  !Harness <- " + "哈尼斯, 哈尼思".encode("utf-8") + b"  # keep note\r\n",
                p.read_bytes(),
            )

            # Re-adding already-pinned term with existing alias is a no-op skip
            r_noop = add_hotword(p, "Harness", aliases=["哈尼斯"], pin=True)
            self.assertFalse(r_noop.changed)
            self.assertEqual(r_noop.skipped, ("Harness",))

            # Exceeding 5 aliases on in-place merge raises ValueError
            with self.assertRaises(ValueError):
                add_hotword(p, "Harness", aliases=["别1", "别2", "别3", "别4"])

            # In-place pin upgrade without aliases on escaped '!important' and new pinned term
            r_pin = add_hotwords(p, ["!important", "Harness", "BrandNew"], pin=True)
            self.assertTrue(r_pin.changed)
            self.assertEqual(r_pin.updated, ("!important",))
            self.assertEqual(r_pin.skipped, ("Harness",))
            self.assertEqual(r_pin.added, ("BrandNew",))
            self.assertEqual(r_pin.pinned, ("!important", "BrandNew"))
            raw_after = p.read_bytes()
            self.assertTrue(raw_after.startswith(b"\xef\xbb\xbf"))
            self.assertIn(b"!\\!important\r\n!BrandNew\r\n", raw_after)

            # Re-adding existing plain term without pin is no-op
            self.assertFalse(add_hotword(p, "BrandNew").changed)

            # New aliased term append
            r_new_al = add_hotword(p, "Ghostty", aliases=["高斯"], pin=True)
            self.assertTrue(r_new_al.changed)
            self.assertEqual(r_new_al.added, ("Ghostty",))
            self.assertEqual(r_new_al.pinned, ("Ghostty",))
            self.assertEqual(len(r_new_al.warnings), 1)  # 2-char alias warning

            # Remove deletes ALL duplicate CI lines ('PlainOne' and 'plainone') and escaped '!\!important'
            rm = remove_hotwords(p, ["PLAINONE", "!important", "NotHere"])
            self.assertTrue(rm.changed)
            self.assertEqual(rm.removed, ("PlainOne", "!important"))
            self.assertEqual(rm.not_found, ("NotHere",))
            t_after = load_hotwords_table(p, use_cache=False)
            self.assertNotIn("PlainOne", t_after.terms)
            self.assertNotIn("!important", t_after.terms)
            self.assertIn("Harness", t_after.terms)
            self.assertIn("Ghostty", t_after.terms)
            # No-op remove when term not present
            self.assertFalse(remove_hotword(p, "PlainOne").changed)

    def test_pin_and_alias_append_preserve_hidden_ambiguous_and_over5_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "personal.txt"
            p.write_text(
                "Alpha <- shared  # keep note\n"
                "Beta <- shared\n"
                "Gamma <- a1, a2, a3, a4, a5, a6\n"
                "Delta <- d1, d2, d3, d4, d5, d6\n"
                "Alpha # bad\u2028sep\n",
                encoding="utf-8",
            )

            # 1. Pin-only on Alpha adds '!' in place WITHOUT dropping hidden ambiguous 'shared' or inline comment
            r_pin = add_hotwords(p, ["Alpha"], pin=True)
            self.assertTrue(r_pin.changed)
            self.assertEqual(r_pin.updated, ("Alpha",))
            self.assertEqual(r_pin.pinned, ("Alpha",))
            txt1 = p.read_text(encoding="utf-8")
            self.assertIn("!Alpha <- shared  # keep note\n", txt1)
            self.assertIn("Beta <- shared\n", txt1)
            t1 = load_hotwords_table(p, use_cache=False)
            by_term1 = {e.term: e for e in t1.entries}
            self.assertTrue(by_term1["Alpha"].pinned)
            self.assertEqual(by_term1["Alpha"].aliases, ())
            self.assertEqual(by_term1["Beta"].aliases, ())
            self.assertTrue(
                any(a.alias == "shared" and set(a.terms) == {"Alpha", "Beta"} for a in t1.ambiguous_aliases),
                t1.ambiguous_aliases,
            )

            # 2. Appending a new alias to Beta preserves raw 'shared' (no silent reassignment to Alpha)
            r_beta = add_hotwords(p, ["Beta"], aliases=["贝塔"], pin=True)
            self.assertTrue(r_beta.changed)
            self.assertEqual(r_beta.aliases_added, ("贝塔",))
            txt2 = p.read_text(encoding="utf-8")
            self.assertIn("!Beta <- shared, 贝塔\n", txt2)
            t2 = load_hotwords_table(p, use_cache=False)
            by_term2 = {e.term: e for e in t2.entries}
            self.assertEqual(by_term2["Alpha"].aliases, ())
            self.assertEqual(by_term2["Beta"].aliases, ("贝塔",))
            self.assertTrue(
                any(a.alias == "shared" and set(a.terms) == {"Alpha", "Beta"} for a in t2.ambiguous_aliases),
                t2.ambiguous_aliases,
            )

            # 3. Pin-only on >5 manual aliases preserves all 6 aliases verbatim
            r_gamma_pin = add_hotwords(p, ["Gamma"], pin=True)
            self.assertTrue(r_gamma_pin.changed)
            self.assertIn("!Gamma <- a1, a2, a3, a4, a5, a6\n", p.read_text(encoding="utf-8"))

            # Re-adding the 6th alias on already-pinned Gamma is a no-op skip (still preserves all 6)
            r_gamma_skip = add_hotwords(p, ["Gamma"], aliases=["a6"])
            self.assertFalse(r_gamma_skip.changed)
            self.assertEqual(r_gamma_skip.skipped, ("Gamma",))

            # Pinning Delta while passing its existing 6th alias upgrades pin in place without truncating >5 aliases
            r_delta_pin = add_hotwords(p, ["Delta"], aliases=["d6"], pin=True)
            self.assertTrue(r_delta_pin.changed)
            self.assertEqual(r_delta_pin.aliases_added, ())
            self.assertEqual(r_delta_pin.pinned, ("Delta",))
            self.assertIn("!Delta <- d1, d2, d3, d4, d5, d6\n", p.read_text(encoding="utf-8"))

            # Appending a 7th alias on Gamma fails total-alias-limit check
            with self.assertRaises(ValueError):
                add_hotwords(p, ["Gamma"], aliases=["a7"])

            # 4. Entry with 4 effective + 1 hidden ambiguous alias (=5 raw) rejects adding a 6th alias
            p2 = Path(td) / "five_with_ambig.txt"
            p2.write_text("One <- amb, e1, e2, e3, e4\nTwo <- amb\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                add_hotwords(p2, ["One"], aliases=["e5"])

            # 5. remove_hotwords skips invalid U+2028 line (matches reader & pin/append validity)
            r_rm = remove_hotwords(p, ["Alpha"])
            self.assertTrue(r_rm.changed)
            self.assertEqual(r_rm.removed, ("Alpha",))
            txt_after_rm = p.read_text(encoding="utf-8")
            self.assertNotIn("!Alpha <- shared", txt_after_rm)
            self.assertIn("Alpha # bad\u2028sep\n", txt_after_rm)
            r_rm_again = remove_hotwords(p, ["Alpha"])
            self.assertFalse(r_rm_again.changed)
            self.assertEqual(r_rm_again.not_found, ("Alpha",))
            self.assertIn("Alpha # bad\u2028sep\n", p.read_text(encoding="utf-8"))

    def test_concurrent_directory_flock_atomic_rollback_encoding_and_ctime_reload(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            hw_dir = tmp / "real_dir"
            hw_dir.mkdir()
            sym_dir = tmp / "sym_dir"
            sym_dir.symlink_to(hw_dir)
            hw_real = hw_dir / "personal.txt"
            hw_sym = sym_dir / "personal.txt"

            # 1. Concurrent edits through BOTH real path and symlink path -> zero lost updates & zero .lock files
            errors: list[Exception] = []

            def _worker(tid: int) -> None:
                try:
                    target = hw_sym if tid % 2 == 0 else hw_real
                    for i in range(10):
                        add_hotwords(target, [f"Thread{tid}Term{i}"])
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=_worker, args=(tid,)) for tid in range(6)]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
            self.assertEqual(errors, [])
            expected = {f"Thread{tid}Term{i}" for tid in range(6) for i in range(10)}
            self.assertEqual(set(load_hotwords_table(hw_real, use_cache=False).terms), expected)
            self.assertEqual(sorted(p.name for p in hw_dir.iterdir()), ["personal.txt"])

            # 2. Atomic write rollback & IO fault injection
            fault_p = tmp / "fault.txt"
            fault_p.write_text("OriginalTerm\n", encoding="utf-8")
            with mock.patch("agent_voice_input.hotwords.os.replace", side_effect=OSError("ENOSPC")):
                with self.assertRaises(OSError):
                    add_hotword(fault_p, "NewTerm")
            self.assertEqual(fault_p.read_text(encoding="utf-8"), "OriginalTerm\n")
            self.assertEqual(list(tmp.glob(".*.tmp")), [])

            with mock.patch("agent_voice_input.hotwords.os.fchmod", side_effect=OSError("EPERM")):
                with self.assertRaises(OSError):
                    add_hotword(fault_p, "NewTerm")
            self.assertEqual(fault_p.read_text(encoding="utf-8"), "OriginalTerm\n")
            self.assertEqual(list(tmp.glob(".*.tmp")), [])

            # Faults on open/read/lstat/lock
            with mock.patch("agent_voice_input.hotwords.os.open", side_effect=OSError("EACCES")):
                t_err = load_hotwords_table(fault_p, use_cache=False)
                self.assertFalse(t_err.exists)
                self.assertEqual(len(t_err.file_warnings), 1)
                with self.assertRaises(ConfigError):
                    add_hotword(fault_p, "NewTerm")
            with mock.patch("agent_voice_input.hotwords._read_bounded_fd", side_effect=OSError("EIO")):
                t_io = load_hotwords_table(fault_p, use_cache=False)
                self.assertFalse(t_io.exists)
                self.assertEqual(len(t_io.file_warnings), 1)
            with mock.patch("agent_voice_input.hotwords.os.lstat", side_effect=OSError("EIO")):
                with self.assertRaises(ConfigError):
                    add_hotword(fault_p, "NewTerm")

            # Remove race when file vanishes between pre-check and lock
            vanish_p = tmp / "vanish.txt"
            vanish_p.write_text("Gone\n", encoding="utf-8")
            orig_inspect = __import__("agent_voice_input.hotwords", fromlist=["_inspect_writable_target"])._inspect_writable_target
            calls = [0]

            def _vanish_inspect(path: Path) -> os.stat_result | None:
                calls[0] += 1
                if calls[0] == 2:
                    return None
                return orig_inspect(path)

            with mock.patch("agent_voice_input.hotwords._inspect_writable_target", side_effect=_vanish_inspect):
                rm_v = remove_hotword(vanish_p, "Gone")
            self.assertFalse(rm_v.changed)

            # _read_full_for_edit open error surfaces ConfigError
            with mock.patch("agent_voice_input.hotwords.os.open", side_effect=[os.open(str(tmp), os.O_RDONLY), OSError("EACCES")]):
                with self.assertRaises(ConfigError):
                    add_hotword(fault_p, "FailOpenEdit")

            # 3. Bad encoding detection, warning dedupe, strict mode, cache eviction (>32 files)
            samples = (
                ("utf32le.txt", b"\xff\xfe\x00\x00A\x00\x00\x00", "UTF-32 LE"),
                ("utf32be.txt", b"\x00\x00\xfe\xff\x00\x00\x00A", "UTF-32 BE"),
                ("utf16le.txt", b"\xff\xfeA\x00", "UTF-16 LE"),
                ("utf16be.txt", b"\xfe\xff\x00A", "UTF-16 BE"),
                ("nul.txt", b"Alpha\x00Beta", "NUL byte"),
                ("gbk.txt", "豆包".encode("gbk"), "invalid UTF-8"),
            )
            for fname, payload, hint in samples:
                bp = tmp / fname
                bp.write_bytes(payload)
                err1 = io.StringIO()
                with contextlib.redirect_stderr(err1):
                    tb1 = load_hotwords_table(bp, use_cache=True)
                self.assertTrue(tb1.exists)
                self.assertIn(hint, tb1.encoding_error)
                self.assertIn("WARN:", err1.getvalue())
                self.assertTrue(any(hint in w for w in tb1.diagnostic_warnings()))
                err2 = io.StringIO()
                with contextlib.redirect_stderr(err2):
                    tb2 = load_hotwords_table(bp, use_cache=True)
                self.assertEqual(err2.getvalue(), "")
                self.assertEqual(tb2.encoding_error, tb1.encoding_error)
                with self.assertRaises(ConfigError):
                    load_hotwords_table(bp, use_cache=True, strict_encoding=True)
                with self.assertRaises(ConfigError):
                    load_hotwords_table(bp, use_cache=False, strict_encoding=True)
                with self.assertRaises(ConfigError):
                    add_hotword(bp, "Safe")

            # Cache eviction bounds both cache & bad-encoding warning dicts
            for idx in range(36):
                cp = tmp / f"cache_{idx}.txt"
                cp.write_text(f"Term{idx}\n", encoding="utf-8")
                self.assertEqual(load_hotwords_table(cp, use_cache=True).terms, (f"Term{idx}",))
                bad_i = tmp / f"bad_{idx}.txt"
                bad_i.write_bytes(b"A\x00B")
                with contextlib.redirect_stderr(io.StringIO()):
                    load_hotwords_table(bad_i, use_cache=False)

            # Delete file evicts cache cleanly
            del_p = tmp / "to_delete.txt"
            del_p.write_text("TempTerm\n", encoding="utf-8")
            self.assertEqual(load_hotwords_table(del_p).terms, ("TempTerm",))
            del_p.unlink()
            self.assertFalse(load_hotwords_table(del_p).exists)

            # Same-length overwrite with restored mtime invalidates via st_ctime_ns
            ctime_p = tmp / "ctime.txt"
            ctime_p.write_text("OldWordA\n", encoding="utf-8")
            self.assertEqual(load_hotwords_table(ctime_p).terms, ("OldWordA",))
            st_before = os.stat(ctime_p)
            time.sleep(0.02)
            with open(ctime_p, "r+b") as f:
                f.write(b"NewWordB\n")
                f.flush()
            os.utime(ctime_p, ns=(st_before.st_atime_ns, st_before.st_mtime_ns))
            st_after = os.stat(ctime_p)
            if st_after.st_ctime_ns != st_before.st_ctime_ns:
                self.assertEqual(load_hotwords_table(ctime_p).terms, ("NewWordB",))
            else:
                class _FakeStat:
                    st_mode = st_before.st_mode
                    st_mtime_ns = st_before.st_mtime_ns
                    st_ctime_ns = st_before.st_ctime_ns + 1
                    st_size = st_before.st_size
                    st_ino = st_before.st_ino
                    st_dev = st_before.st_dev

                with mock.patch("agent_voice_input.hotwords.os.fstat", return_value=_FakeStat()):
                    self.assertEqual(load_hotwords_table(ctime_p).terms, ("NewWordB",))

            # Bad AGENT_VOICE_INPUT_HOME in env when loading explicit path does not break parent check
            self.assertEqual(
                load_hotwords_table(ctime_p, env={"AGENT_VOICE_INPUT_HOME": "relative/bad"}, use_cache=False).terms,
                ("NewWordB",),
            )


class DualTrackOrderingAndProjectionTests(unittest.TestCase):
    def test_stage1_preserves_cjk_file_order_and_stage2_is_byte_stable(self) -> None:
        # Stage 1: pinned -> effective-aliased -> plain, preserving file order within each tier
        # Chinese terms placed ahead of ASCII terms must NOT be pushed back by ASCII sort!
        text_v1 = (
            "# Comment A\n"
            "豆包\n"
            "飞书\n"
            "Alpha\n"
            "Beta <- 贝塔\n"
            "中文别名词 <- 别名一\n"
            "!置顶中文\n"
            "!ZebraPin\n"
            "AmbigOnly <- 共享词\n"
            "AmbigTwo <- 共享词\n"
        )
        t1 = parse_hotwords_text(text_v1)

        s1 = order_stage1_entries(t1.entries)
        self.assertEqual(
            [e.term for e in s1],
            [
                "置顶中文",  # tier 0 (pinned), file order
                "ZebraPin",  # tier 0 (pinned), file order
                "Beta",  # tier 1 (effective alias), file order
                "中文别名词",  # tier 1 (effective alias), file order
                "豆包",  # tier 2 (plain), Chinese stays AHEAD of Alpha!
                "飞书",
                "Alpha",
                "AmbigOnly",  # hidden ambiguous alias -> drops to tier 2!
                "AmbigTwo",
            ],
        )

        # Stage 2: deterministic (tier, term.lower(), term) -> byte-identical across comments/blank lines/reordering
        text_v2 = (
            "\r\n# Different header & comments\r\n"
            "Alpha  # moved around\r\n"
            "!ZebraPin\r\n"
            "AmbigTwo <- 共享词\r\n"
            "飞书\r\n"
            "中文别名词 <- 别名一\r\n"
            "豆包\r\n"
            "AmbigOnly <- 共享词\r\n"
            "!置顶中文\r\n"
            "Beta <- 贝塔\r\n"
        )
        t2 = parse_hotwords_text(text_v2)

        block_v1 = format_stage2_vocabulary_hints(order_stage2_entries(t1.entries))
        block_v2 = format_stage2_vocabulary_hints(order_stage2_entries(t2.entries))
        self.assertEqual(block_v1, block_v2)
        self.assertEqual(t1.stage2_entries, t2.stage2_entries)
        self.assertEqual(
            [e.term for e in t1.stage2_entries],
            ["ZebraPin", "置顶中文", "Beta", "中文别名词", "Alpha", "AmbigOnly", "AmbigTwo", "豆包", "飞书"],
        )

    def test_quota_specs_and_dashscope_150_1800_and_single_hop_200_3000(self) -> None:
        self.assertEqual(HOTWORDS_QUOTA_SPECS["dashscope"].max_terms, 150)
        self.assertEqual(HOTWORDS_QUOTA_SPECS["dashscope"].max_chars, 1800)
        self.assertEqual(STAGE2_HOTWORDS_MAX_ITEMS, 200)
        self.assertEqual(STAGE2_HOTWORDS_MAX_CHARS, 3000)
        self.assertEqual(SINGLE_HOP_HOTWORDS_MAX_ITEMS, 200)
        self.assertEqual(SINGLE_HOP_HOTWORDS_MAX_CHARS, 3000)

        self.assertEqual(resolve_hotwords_quota_spec("doubao").key, "doubao")
        self.assertEqual(resolve_hotwords_quota_spec("volcengine").key, "doubao")
        self.assertEqual(resolve_hotwords_quota_spec("dashscope").key, "dashscope")
        self.assertEqual(
            resolve_hotwords_quota_spec("dashscope", model="qwen3-asr-flash", api="chat-completions").key,
            "dashscope",
        )
        self.assertEqual(
            resolve_hotwords_quota_spec("dashscope", model="qwen3.8-omni-flash").key,
            "single_hop",
        )
        self.assertEqual(resolve_hotwords_quota_spec("gemini").key, "single_hop")
        self.assertEqual(
            resolve_hotwords_quota_spec("gemini", api="audio-transcriptions", base_url="https://proxy.example/v1").key,
            "whisper",
        )
        self.assertEqual(resolve_hotwords_quota_spec("openai").key, "openai_keywords")
        self.assertEqual(resolve_hotwords_quota_spec("openai", model="whisper-1").key, "whisper")
        self.assertEqual(resolve_hotwords_quota_spec("openai", model="gpt-4o-transcribe").key, "whisper")
        self.assertEqual(resolve_hotwords_quota_spec("openai", api="chat-completions").key, "single_hop")
        self.assertEqual(resolve_hotwords_quota_spec("groq").key, "whisper")
        self.assertEqual(resolve_hotwords_quota_spec("groq", api="chat-completions").key, "single_hop")
        self.assertEqual(resolve_hotwords_quota_spec("deepseek").key, "deepseek_asr_unsupported")
        self.assertEqual(resolve_hotwords_quota_spec("whisper").key, "whisper")
        self.assertEqual(resolve_hotwords_quota_spec("single_hop").key, "single_hop")
        self.assertEqual(
            resolve_hotwords_quota_spec("openrouter", model="microsoft/mai-transcribe-2:nitro").key,
            "openrouter_keyterms",
        )
        self.assertEqual(
            resolve_hotwords_quota_spec("openrouter", model="deepgram/nova-3").key,
            "openrouter_stt_unsupported",
        )
        self.assertEqual(
            resolve_hotwords_quota_spec("openrouter", model="google/gemini-3.5-flash-lite").key,
            "single_hop",
        )
        self.assertEqual(resolve_hotwords_quota_spec("custom-unknown").key, "single_hop")

        # DashScope 150 terms / 1800 chars flat budgeter
        ds_many = [f"d{i:03d}" for i in range(160)]
        ds_res = budget_hotwords_for_provider(ds_many, "dashscope")
        self.assertEqual(ds_res.kept_count, 150)
        self.assertEqual(ds_res.dropped_count, 10)
        self.assertIn("+5 more", format_budget_line(ds_res))

        # Doubao stem collapse, max_term_chars, whole-word skip
        db_terms = [
            "ManualTop",
            "refiner.py",
            "refiner",
            "x" * 70,
            *(f"{chr(ord('a') + i)}" * 60 for i in range(8)),
            "z" * 60,
            "Tail",
        ]
        db_res = budget_hotwords_for_provider(db_terms, "doubao")
        self.assertEqual(db_res.kept[0], "ManualTop")
        self.assertIn("refiner", db_res.kept)
        self.assertNotIn("refiner.py", db_res.kept)
        self.assertIn("Tail", db_res.kept)
        self.assertEqual(db_res.dropped, ("x" * 70, "z" * 60))
        self.assertEqual(db_res.used_chars, 500)

        # OpenAI keywords sanitization
        self.assertEqual(sanitize_openai_keyword("<KestrelDB>"), "KestrelDB")
        self.assertEqual(sanitize_openai_keyword("foo\r\nbar\u2028baz"), "foo bar baz")
        self.assertEqual(sanitize_openai_keyword("<>"), "")
        self.assertEqual(sanitize_openai_keyword(123), "")  # type: ignore[arg-type]
        oai_res = budget_openai_keywords(["<KestrelDB>", "kestreldb", "<>", "foo\nbar"])
        self.assertEqual(oai_res.kept, ("KestrelDB", "foo bar"))
        self.assertEqual(oai_res.dropped, ("<>",))
        self.assertIn("[TRUNCATED", format_budget_line(oai_res))

        # Unsupported provider format_budget_line
        unsup_empty = budget_hotwords_for_provider([], "deepseek")
        self.assertIn("not passed by this route", format_budget_line(unsup_empty))
        unsup_nonempty = budget_hotwords_for_provider(["Alpha"], "deepseek")
        self.assertIn("1 manual terms unused", format_budget_line(unsup_nonempty))
        self.assertIn("[OK]", format_budget_line(budget_hotwords_for_provider(["Alpha"], "doubao")))

    def test_project_stage1_hotwords_three_buckets_reserve_backfill_and_doubao_stem_dedupe(self) -> None:
        # 0. Unbucketed AsrContext falls back cleanly to flat hotwords
        flat_ctx = AsrContext(hotwords=["FlatA", "FlatB"], hotwords_bucketed=False)
        flat_res = project_stage1_hotwords(flat_ctx, "doubao")
        self.assertEqual(flat_res.kept, ("FlatA", "FlatB"))
        self.assertEqual(flat_res.personal_kept, 0)

        # 1. General mode: 100% goes to personal; dynamic & builtin NEVER enter
        gen_ctx = AsrContext(
            mode="general",
            personal_hotwords=tuple(f"p{i:02d}" for i in range(60)),
            dynamic_hotwords=("DynLeak",),
            builtin_hotwords=("BuiltLeak",),
            hotwords_bucketed=True,
        )
        gen_res = project_stage1_hotwords(gen_ctx, "doubao")
        self.assertEqual(gen_res.kept_count, 50)
        self.assertEqual(gen_res.personal_kept, 50)
        self.assertEqual(gen_res.dynamic_kept, 0)
        self.assertEqual(gen_res.builtin_kept, 0)
        self.assertEqual(gen_res.personal_dropped, 10)
        self.assertNotIn("DynLeak", gen_res.kept)
        self.assertNotIn("BuiltLeak", gen_res.kept)
        self.assertEqual(gen_res.as_dict()["personal_kept"], 50)
        self.assertEqual(gen_res.as_dict()["personal_dropped"], 10)

        # 2. Agent mode ceil(30%) dynamic reserve:
        #    - Doubao (50): dyn_reserve = 15, personal pass1 <= 35
        #    - Whisper (35): dyn_reserve = 11 (ceil(10.5)), personal pass1 <= 24
        #    - DashScope (150): dyn_reserve = 45, personal pass1 <= 105
        ag_doubao = AsrContext(
            mode="agent",
            personal_hotwords=tuple(f"p{i:02d}" for i in range(60)),
            dynamic_hotwords=tuple(f"d{i:02d}" for i in range(20)),
            builtin_hotwords=tuple(f"b{i:02d}" for i in range(10)),
            hotwords_bucketed=True,
        )
        r_db = project_stage1_hotwords(ag_doubao, "doubao")
        self.assertEqual(r_db.kept_count, 50)
        self.assertEqual(r_db.personal_kept, 35)
        self.assertEqual(r_db.dynamic_kept, 15)
        self.assertEqual(r_db.builtin_kept, 0)
        self.assertEqual(r_db.personal_dropped, 25)

        r_wh = project_stage1_hotwords(ag_doubao, "whisper")
        self.assertEqual(r_wh.kept_count, 35)
        self.assertEqual(r_wh.personal_kept, 24)
        self.assertEqual(r_wh.dynamic_kept, 11)
        self.assertEqual(r_wh.builtin_kept, 0)

        ag_ds = AsrContext(
            mode="agent",
            personal_hotwords=tuple(f"p{i:03d}" for i in range(120)),
            dynamic_hotwords=tuple(f"d{i:03d}" for i in range(60)),
            builtin_hotwords=tuple(f"b{i:03d}" for i in range(20)),
            hotwords_bucketed=True,
        )
        r_ds = project_stage1_hotwords(ag_ds, "dashscope")
        self.assertEqual(r_ds.kept_count, 150)
        self.assertEqual(r_ds.personal_kept, 105)
        self.assertEqual(r_ds.dynamic_kept, 45)
        self.assertEqual(r_ds.builtin_kept, 0)

        # 3. Backfill order: personal_overflow -> dynamic_overflow -> builtin
        #    Sparse dynamic (3 items) allows personal_overflow to reclaim unused reserve!
        sparse_dyn = AsrContext(
            mode="agent",
            personal_hotwords=tuple(f"p{i:02d}" for i in range(50)),
            dynamic_hotwords=("d00", "d01", "d02"),
            builtin_hotwords=("b00", "b01"),
            hotwords_bucketed=True,
        )
        r_sparse_dyn = project_stage1_hotwords(sparse_dyn, "doubao")
        self.assertEqual(r_sparse_dyn.kept_count, 50)
        self.assertEqual(r_sparse_dyn.personal_kept, 47)
        self.assertEqual(r_sparse_dyn.dynamic_kept, 3)
        self.assertEqual(r_sparse_dyn.builtin_kept, 0)
        self.assertEqual(r_sparse_dyn.personal_dropped, 3)

        #    Sparse personal (5 items) + heavy dynamic (25 items) + builtin (5 items) -> all fit via Pass 3 backfill!
        sparse_pers = AsrContext(
            mode="agent",
            personal_hotwords=tuple(f"p{i:02d}" for i in range(5)),
            dynamic_hotwords=tuple(f"d{i:02d}" for i in range(25)),
            builtin_hotwords=tuple(f"b{i:02d}" for i in range(5)),
            hotwords_bucketed=True,
        )
        r_sparse_pers = project_stage1_hotwords(sparse_pers, "doubao")
        self.assertEqual(r_sparse_pers.kept_count, 35)
        self.assertEqual(r_sparse_pers.personal_kept, 5)
        self.assertEqual(r_sparse_pers.dynamic_kept, 25)
        self.assertEqual(r_sparse_pers.builtin_kept, 5)

        # 4. Shared Doubao stem collapse + cross-bucket deduplication
        stem_ctx = AsrContext(
            mode="agent",
            personal_hotwords=("refiner.py", "sharedterm"),
            dynamic_hotwords=("refiner", "SharedTerm", "hotwords.py", "hotwords"),
            builtin_hotwords=("REFINER", "Git"),
            hotwords_bucketed=True,
        )
        r_stem = project_stage1_hotwords(stem_ctx, "doubao")
        self.assertEqual(r_stem.kept, ("refiner", "sharedterm", "hotwords", "Git"))
        self.assertEqual(r_stem.personal_kept, 2)
        self.assertEqual(r_stem.dynamic_kept, 1)
        self.assertEqual(r_stem.builtin_kept, 1)
        self.assertEqual(r_stem.dropped, ())

        # 5. Edge cases: eff_max_terms < 3 (dyn_reserve=0), OpenAI sanitize drops, max_term_chars drops, 0 budget, Pass 2 char freedom
        tiny_ctx = AsrContext(
            mode="agent",
            personal_hotwords=("p1", "p2", "p3"),
            dynamic_hotwords=("d1",),
            builtin_hotwords=("b1",),
            hotwords_bucketed=True,
        )
        r_tiny = project_stage1_hotwords(tiny_ctx, "doubao", max_terms=2)
        self.assertEqual(r_tiny.kept, ("p1", "p2"))
        self.assertEqual(r_tiny.personal_kept, 2)
        self.assertEqual(r_tiny.dynamic_kept, 0)

        # Pass 1 char cap 70% vs Pass 2 unrestricted remaining chars (not locked to 30%):
        # max_chars=100 -> p_char_cap=70. Personal uses 10 chars, leaving 90 chars for dynamic (>30 chars)!
        char_ctx = AsrContext(
            mode="agent",
            personal_hotwords=("A" * 10,),
            dynamic_hotwords=("D" * 40, "E" * 40),
            builtin_hotwords=(),
            hotwords_bucketed=True,
        )
        r_char = project_stage1_hotwords(char_ctx, "doubao", max_terms=10, max_chars=100)
        self.assertEqual(r_char.kept, ("A" * 10, "D" * 40, "E" * 40))
        self.assertEqual(r_char.used_chars, 90)

        # Sanitization empty drops & oversized term drops across all 3 buckets
        dirty_oai = AsrContext(
            mode="agent",
            personal_hotwords=("<>", "P1"),
            dynamic_hotwords=("<>", "D1"),
            builtin_hotwords=("<>", "B1"),
            hotwords_bucketed=True,
        )
        r_oai = project_stage1_hotwords(dirty_oai, "openai", model="gpt-transcribe")
        self.assertEqual(r_oai.kept, ("P1", "D1", "B1"))
        self.assertEqual(r_oai.personal_dropped, 1)
        self.assertEqual(r_oai.dropped, ("<>", "<>", "<>"))

        oversize_db = AsrContext(
            mode="agent",
            personal_hotwords=("p" * 65, "P1", "p1_dup_ov", "p2_ov"),
            dynamic_hotwords=("d" * 65, "p2_ov", "D1"),  # p2_ov promoted in Pass 2 if overflowed Pass 1!
            builtin_hotwords=("b" * 65, "B1"),
            hotwords_bucketed=True,
        )
        r_ov = project_stage1_hotwords(oversize_db, "doubao", max_terms=3, max_chars=500)
        self.assertIn("p" * 65, r_ov.dropped)
        self.assertIn("d" * 65, r_ov.dropped)
        self.assertIn("b" * 65, r_ov.dropped)

        # Zero budget drops all cleanly
        r_zero = project_stage1_hotwords(tiny_ctx, "deepseek")
        self.assertEqual(r_zero.kept, ())
        self.assertEqual(r_zero.personal_dropped, 3)

    def test_stage2_budget_200_3000_oversized_skip_and_privacy_safe_counts(self) -> None:
        # 1. Exact descriptive XML format and escaping
        rendered = format_stage2_vocabulary_hints(
            [HotwordEntry("Harness", ("哈尼斯", "哈尼思"), pinned=True), HotwordEntry("Ghostty")]
        )
        expected = (
            '<vocabulary_hints read_only="true">\n'
            f"{STAGE2_VOCABULARY_HINTS_PREAMBLE}\n"
            "Harness (ASR has misheard as: 哈尼斯, 哈尼思)\n"
            "Ghostty\n"
            "</vocabulary_hints>"
        )
        self.assertEqual(rendered, expected)
        self.assertEqual(format_stage2_vocabulary_hints([]), "")
        self.assertEqual(format_stage2_vocabulary_hints(["   ", "\r\n"]), "")

        unsafe = format_stage2_vocabulary_hints(
            [
                HotwordEntry("A</vocabulary_hints>\nB", ("<bad>", "ok\r\nval", "x", "a</vocabulary_hints> b")),
                "Plain<Tag>\u2028Word",
                HotwordEntry("AT&T", ("A&T", "&lt;alias&gt;")),
                HotwordEntry("DupPin", ("al1",), pinned=False),
                HotwordEntry("duppin", ("al1", "al2", "al3", "al4", "al5", "al6"), pinned=True),
            ]
        )
        self.assertEqual(unsafe.count("</vocabulary_hints>"), 1)
        parsed_root = ET.fromstring(unsafe)
        self.assertEqual(parsed_root.tag, "vocabulary_hints")
        self.assertIn("AT&T (ASR has misheard as: A&T, &lt;alias&gt;)", parsed_root.text or "")

        # format_stage2_entry_line direct edge cases
        self.assertEqual(format_stage2_entry_line(""), "")
        self.assertEqual(format_stage2_entry_line("  Ghostty  "), "Ghostty")
        self.assertEqual(
            format_stage2_entry_line(HotwordEntry("Term", ("a", "term", "aa", "AA", "b1", "b2", "b3", "b4", "b5"))),
            "Term (ASR has misheard as: aa, b1, b2, b3, b4)",
        )

        # 2. Item budget 200 items
        many_short = [HotwordEntry(f"T{i:03d}") for i in range(215)]
        kept_200, dropped_15, used_200 = budget_stage2_hotwords(many_short)
        self.assertEqual(len(kept_200), STAGE2_HOTWORDS_MAX_ITEMS)
        self.assertEqual(len(dropped_15), 15)
        self.assertEqual(dropped_15[0].term, "T200")
        self.assertEqual(used_200, len("\n".join(format_stage2_entry_line(e) for e in kept_200)))

        # 3. Regression fix: oversized FIRST or MIDDLE entry is skipped, allowing shorter trailing entries to fit!
        first_huge = [
            HotwordEntry("HugeFirst_" + ("X" * 40)),
            HotwordEntry("ShortAfterFirst"),
        ]
        kept_fh, dropped_fh, used_fh = budget_stage2_hotwords(first_huge, max_chars=25)
        self.assertEqual([e.term for e in kept_fh], ["ShortAfterFirst"])
        self.assertEqual([e.term for e in dropped_fh], [first_huge[0].term])
        self.assertEqual(used_fh, len("ShortAfterFirst"))

        mid_huge = [
            HotwordEntry("FirstTerm"),
            HotwordEntry("VeryLongMiddleTerm_" + ("X" * 40)),
            HotwordEntry("ShortTail"),
        ]
        kept_mh, dropped_mh, used_mh = budget_stage2_hotwords(mid_huge, max_chars=len("FirstTerm\nShortTail"))
        self.assertEqual([e.term for e in kept_mh], ["FirstTerm", "ShortTail"])
        self.assertEqual([e.term for e in dropped_mh], [mid_huge[1].term])
        self.assertEqual(used_mh, len("FirstTerm\nShortTail"))

        # Zero budget drops all cleanly
        kept_z, dropped_z, used_z = budget_stage2_hotwords(mid_huge, max_items=0)
        self.assertEqual(kept_z, [])
        self.assertEqual(len(dropped_z), 3)
        self.assertEqual(used_z, 0)

        # 4. Stage2HotwordsBudgetSummary + privacy-safe as_safe_counts_dict() (ZERO term text!)
        with tempfile.TemporaryDirectory() as td:
            hw_path = Path(td) / "personal.txt"
            hw_path.write_text(
                "[global]\n"
                + "\n".join(f"SecretPersonalTerm{i:02d}" for i in range(10))
                + "\n",
                encoding="utf-8",
            )
            sum_path = summarize_stage2_hotwords_budget(hw_path, max_items=4)
            self.assertTrue(sum_path.truncated)
            self.assertEqual(sum_path.kept_count, 4)
            self.assertEqual(sum_path.dropped_count, 6)
            self.assertEqual(sum_path.warnings_count, 1)  # legacy [global]
            self.assertEqual(sum_path.as_dict()["kept_count"], 4)

            safe = sum_path.as_safe_counts_dict()
            self.assertEqual(
                set(safe.keys()),
                {
                    "total",
                    "kept",
                    "dropped_count",
                    "used_chars",
                    "max_items",
                    "max_chars",
                    "warnings_count",
                },
            )
            self.assertEqual(safe["kept"], 4)
            self.assertEqual(safe["dropped_count"], 6)
            self.assertNotIn("stage2_kept", safe)
            self.assertNotIn("stage2_dropped", safe)
            self.assertTrue(all(isinstance(v, int) for v in safe.values()))
            self.assertNotIn("SecretPersonalTerm", str(safe))

            self.assertIn("stage2: 4/4 items", format_stage2_budget_line(sum_path))
            self.assertIn("+1 more", format_stage2_budget_line(sum_path))
            self.assertIn("custom_lbl:", format_stage2_budget_line(sum_path, name="custom_lbl"))

            sum_ok = summarize_stage2_hotwords_budget(None)
            self.assertFalse(sum_ok.truncated)
            self.assertIn("[OK]", format_stage2_budget_line(sum_ok))
            # Scoped label branch
            scoped_sum = summarize_stage2_hotwords_budget(HotwordsTable(entries=(HotwordEntry("A"),)))
            self.assertEqual(scoped_sum.kept_count, 1)
            scoped_with_scopes = type(scoped_sum)(
                kept_count=1,
                dropped_count=0,
                total_input=1,
                used_chars=1,
                max_items=200,
                max_chars=3000,
                mode="agent",
                scopes=("personal",),
            )
            self.assertIn("stage2 (agent: personal):", format_stage2_budget_line(scoped_with_scopes))


if __name__ == "__main__":
    unittest.main()
