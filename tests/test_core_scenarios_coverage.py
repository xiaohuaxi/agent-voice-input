"""Scenario-driven offline tests for Tier 1 pure logic core and cross-layer helpers.

Uses real inputs, real temporary directories/git worktrees, and local 127.0.0.1 loopback
HTTP servers—zero fake syscall mocks.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import http.server
import io
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_voice_input import _remote_probe  # noqa: E402
from agent_voice_input import config as cfg_mod  # noqa: E402
from agent_voice_input import hud as hud_mod  # noqa: E402
from agent_voice_input import llm_common as llm_mod  # noqa: E402
from agent_voice_input import prompts as prompts_mod  # noqa: E402
from agent_voice_input.app_scope import (  # noqa: E402
    IdeTitleInfo,
    classify_target_app_hint,
    is_dedicated_ssh_client_window,
    is_remote_ide_window,
    match_app_patterns,
    parse_ide_window_title,
)
from agent_voice_input.asr_providers import _resolve_dashscope_url  # noqa: E402
from agent_voice_input.audio_proc import (  # noqa: E402
    pack_pcm16_mono_wav,
    resolve_failed_audio,
    save_failed_audio,
)
from agent_voice_input.config import (  # noqa: E402
    AsrConfig,
    Config,
    ConfigError,
    HotkeyConfig,
    RefinerConfig,
    RemoteConfig,
    RemoteHostConfig,
    _infer_provider_from_explicit_asr_hints,
    _infer_provider_from_explicit_refiner_hints,
    _parse_simple_toml,
    classify_asr_model_api,
    default_config_file_candidates,
    default_env_file_candidates,
    expand_user_path,
    find_codex_bin,
    has_codex_local_auth,
    is_dashscope_native_multimodal_model,
    is_gemini_native_url,
    is_hermetic_env,
    load_config,
    load_env_files,
    parse_env_text,
    parse_toml,
    redact_secrets,
    resolve_asr_api,
    resolve_codex_home_dir,
    resolve_credentials,
    resolve_target_config_paths,
    select_asr_provider,
    select_refiner_provider,
)
from agent_voice_input.http_transport import _SameOriginSafeRedirectHandler  # noqa: E402
from agent_voice_input.refiner import _InstanceKeepAlivePool  # noqa: E402
from agent_voice_input.remote_ssh import extract_ssh_target_from_argv  # noqa: E402
from agent_voice_input.session_ctx import write_session_registry_entry  # noqa: E402


@dataclass(frozen=True)
class _Win:
    process: str = ""
    title: str = ""
    bundle_id: str = ""
    document: str = ""


class TestHudAndPromptsScenarios(unittest.TestCase):
    def test_hud_language_hotkey_and_audio_level_edge_cases(self) -> None:
        self.assertEqual(
            hud_mod.resolve_ui_language("", win_ui_culture="zh-CN", env={}),
            "zh",
        )
        self.assertEqual(
            hud_mod.resolve_ui_language(None, win_ui_culture="en-US", env={"LANG": "zh_CN.UTF-8"}),
            "en",
        )
        self.assertEqual(
            hud_mod.resolve_ui_language("auto", env={"LC_ALL": "", "LANG": "zh_HK.UTF-8"}),
            "zh",
        )
        self.assertEqual(hud_mod.resolve_ui_language("zh-TW", env={}), "zh")
        self.assertEqual(hud_mod.resolve_ui_language("en_GB.UTF-8", env={}), "en")

        self.assertEqual(hud_mod.format_hotkey_label("", "zh"), "右 Ctrl")
        self.assertEqual(hud_mod.format_hotkey_label("f12", "en"), "F12")
        self.assertEqual(hud_mod.format_hotkey_label("f25", "en"), "f25")
        self.assertEqual(hud_mod.format_hotkey_label("0x7b", "en"), "0x7B")
        self.assertEqual(hud_mod.format_hotkey_label("0x", "en"), "0x")
        self.assertEqual(hud_mod.format_hotkey_label("PageDown", "zh"), "PageDown")

        self.assertIn("松开右 Ctrl 结束", hud_mod.format_hud_text("recording", "zh", trigger_mode="ptt"))
        self.assertIn("按 PageDown 结束", hud_mod.format_hud_text("recording", "zh", hotkey="PageDown"))
        self.assertEqual(hud_mod.format_hud_text("idle", "zh"), "")
        self.assertEqual(hud_mod.format_hud_text("unknown_err", "en"), "✕ Transcription failed")

        self.assertEqual(hud_mod.recording_countdown_warn_sec(float("nan")), 0)
        self.assertEqual(hud_mod.recording_countdown_warn_sec(0.0), 0)
        self.assertEqual(hud_mod.recording_countdown_warn_sec(12.0), 4)
        self.assertEqual(hud_mod.recording_countdown_warn_sec(60.0), 15)
        self.assertEqual(hud_mod.format_recording_duration(5), "5s")
        self.assertEqual(hud_mod.format_recording_duration(125), "2m 05s")
        self.assertEqual(hud_mod.format_countdown_hint(9, "zh"), "(9s 后自动完成)")
        self.assertEqual(hud_mod.format_countdown_hint(9, "en"), "(auto-finish in 9s)")

        self.assertEqual(hud_mod.pcm16le_rms_level(b"\x00"), 0.0)
        self.assertEqual(hud_mod.pcm16le_rms_level(b"\x00\x00" * 32), 0.0)
        loud_pcm = struct.pack("<h", 24000) * 32
        self.assertEqual(hud_mod.pcm16le_rms_level(loud_pcm), 1.0)
        mid_pcm = struct.pack("<h", 1200) * 32
        self.assertGreater(hud_mod.pcm16le_rms_level(mid_pcm), 0.1)
        self.assertLess(hud_mod.pcm16le_rms_level(mid_pcm), 0.9)

    def test_prompts_target_hint_echo_stripping_edges(self) -> None:
        self.assertEqual(prompts_mod.format_target_app_hint("AI_CHAT"), prompts_mod.TARGET_APP_HINTS["ai_chat"])
        self.assertEqual(prompts_mod.format_target_app_hint("unknown"), "")
        self.assertEqual(prompts_mod.strip_target_hint_echo(""), "")
        self.assertEqual(prompts_mod.strip_target_hint_echo("plain text"), "plain text")

        # Modified body inside trailing <target_app_hint ...>...</target_app_hint> and self-closing tag
        self.assertEqual(
            prompts_mod.strip_target_hint_echo(
                '请检查日志 <target_app_hint category="im_chat">paraphrased hint</target_app_hint>'
            ),
            "请检查日志",
        )
        self.assertEqual(
            prompts_mod.strip_target_hint_echo('测试自闭合 <target_app_hint category="ai_chat" />'),
            "测试自闭合",
        )
        # Trailing unclosed / malformed tag must NOT truncate preceding text when not matching full block
        malformed_tail = "保留正文 <target_app_hint broken\nmultiline />"
        self.assertEqual(prompts_mod.strip_target_hint_echo(malformed_tail), malformed_tail)

        # 5 blocks exhausting the 4-pass loop (L171->178) and falling through to standalone-line cleanup
        nested = (
            '<target_app_hint category="a">1</target_app_hint>' * 4
            + '<target_app_hint category="a" />\n核心内容'
        )
        self.assertEqual(prompts_mod.strip_target_hint_echo(nested), "核心内容")

        # Orphan closing tag at end without opening tag in body
        self.assertEqual(
            prompts_mod.strip_target_hint_echo("这段话末尾多出了闭合标签</target_app_hint>"),
            "这段话末尾多出了闭合标签",
        )
        # Prose genuinely discussing multiline <target_app_hint ... </target_app_hint> preserves closing tag
        prose_mention = "Please inspect <target_app_hint\nin prompts.py before </target_app_hint>"
        self.assertEqual(prompts_mod.strip_target_hint_echo(prose_mention), prose_mention)


class TestAppScopeWindowTitleScenarios(unittest.TestCase):
    def test_pattern_matching_ssh_kitty_and_claude_desktop_disambiguation(self) -> None:
        self.assertFalse(match_app_patterns(_Win(), ["code"]))
        self.assertTrue(match_app_patterns(_Win(process="Code.exe"), ["", "   ", "code"]))

        # macOS bare "kitty" without bundle_id is NOT treated as Windows KiTTY SSH client
        self.assertFalse(is_dedicated_ssh_client_window(_Win(process="kitty", title="zsh"), is_macos=True))
        self.assertTrue(is_dedicated_ssh_client_window(_Win(process="kitty", title="zsh"), is_macos=False))

        # Windows Claude Desktop vs Claude Code CLI in console named claude.exe
        self.assertEqual(
            classify_target_app_hint(_Win(process="claude.exe", title="Project Plan — Claude")),
            "ai_chat",
        )
        self.assertEqual(
            classify_target_app_hint(_Win(process="claude.exe", title="claude · repo [#12345678] - Claude")),
            "",
        )
        self.assertEqual(
            classify_target_app_hint(_Win(process="claude.exe", title="⠋ Working - Claude")),
            "",
        )

    def test_ide_window_titles_and_remote_detection_matrix(self) -> None:
        cases: list[tuple[_Win, IdeTitleInfo | None]] = [
            (_Win(process="Code.exe", title=""), None),
            (_Win(process="Code.exe", title="Visual Studio Code [Administrator]"), None),
            (
                _Win(process="Code.exe", title="● app.py - my-service - WSL: Ubuntu - Visual Studio Code"),
                IdeTitleInfo(root_name="my-service", remote_kind="wsl"),
            ),
            (
                _Win(process="Code.exe", title="app.py - my-service - Dev Container: py3 - Code"),
                IdeTitleInfo(root_name="my-service", remote_kind="container"),
            ),
            (
                _Win(process="Code.exe", title="app.py - my-service [Read-Only] - Visual Studio Code"),
                IdeTitleInfo(root_name="my-service [Read-Only]", remote_kind=""),
            ),
            (_Win(process="Zed", title="Welcome — Zed"), None),
            (_Win(process="Zed", title="notes.md — Zed"), None),
            (
                _Win(process="Zed", title="my-zed [SSH: box01] — Zed Preview"),
                IdeTitleInfo(root_name="my-zed", remote_kind="remote"),
            ),
            (_Win(process="Xcode", title="Organizer — Xcode"), None),
            (_Win(process="pycharm64.exe", title="Welcome to PyCharm"), None),
            (
                _Win(process="idea64.exe", title="my-core [main] - IntelliJ IDEA"),
                IdeTitleInfo(root_name="my-core [main]", explicit_path=""),
            ),
            (
                _Win(process="goland64.exe", title="my-core [~/go/src/my-core] — main.go — GoLand"),
                IdeTitleInfo(root_name="my-core", explicit_path="~/go/src/my-core"),
            ),
            (_Win(process="sublime_text.exe", title="Sublime Text (UNREGISTERED)"), None),
            (_Win(process="sublime_text.exe", title="scratch.txt (UNREGISTERED) - Sublime Text"), None),
            (
                _Win(process="sublime_text.exe", title="~/work/proj/app.py (my-proj) - Sublime Text"),
                IdeTitleInfo(root_name="my-proj", explicit_path="~/work/proj/app.py"),
            ),
            (
                _Win(process="sublime_text.exe", title="app.py - sub-workspace - Sublime Text"),
                IdeTitleInfo(root_name="sub-workspace", explicit_path=""),
            ),
            (_Win(process="sublime_text.exe", title="app.py - untitled - Sublime Text"), None),
            (_Win(process="sublime_text.exe", title="app.py - helper.py - Sublime Text"), None),
        ]
        for win, expected in cases:
            with self.subTest(win=win):
                self.assertEqual(parse_ide_window_title(win), expected)

        # Remote IDE URI & title checks (WSL must NOT be flagged as remote)
        self.assertTrue(is_remote_ide_window(_Win(process="Code", document="sftp://box/srv/a.py")))
        self.assertFalse(
            is_remote_ide_window(_Win(process="Code", document="vscode-remote://wsl+Ubuntu/home/u/a.py"))
        )
        self.assertTrue(
            is_remote_ide_window(
                _Win(process="Code", title="a.py - vscode-remote://ssh-remote+box/srv - Code")
            )
        )
        self.assertFalse(
            is_remote_ide_window(
                _Win(process="Code", title="a.py - vscode-remote://wsl+Ubuntu/home/u - Code")
            )
        )


class TestLlmCommonMappingsAndUsageScenarios(unittest.TestCase):
    def test_token_usage_audio_tokens_and_lenticular_stripping(self) -> None:
        self.assertIsNone(llm_mod._opt_int(True))
        self.assertIsNone(llm_mod._opt_int(None))
        self.assertEqual(llm_mod._opt_int(42.0), 42)
        self.assertIsNone(llm_mod._opt_int(42.5))
        self.assertIsNone(llm_mod._opt_int("42"))

        # DeepSeek cache hit + miss sum vs partial miss, and fallback when details dicts are empty
        self.assertEqual(
            llm_mod._extract_token_usage(
                {"usage": {"prompt_cache_hit_tokens": 100, "prompt_cache_miss_tokens": 25, "completion_tokens": 10}}
            ),
            (125, 100, 10, None),
        )
        self.assertEqual(
            llm_mod._extract_token_usage(
                {
                    "usage": {
                        "prompt_cache_hit_tokens": 50,
                        "input_tokens_details": {},
                        "output_tokens_details": {},
                        "reasoning_output_tokens": 7,
                    }
                }
            ),
            (None, 50, None, 7),
        )

        self.assertIsNone(llm_mod._extract_audio_tokens({}))
        self.assertEqual(
            llm_mod._extract_audio_tokens({"usage": {"input_tokens_details": {}, "audio_input_tokens": 88}}),
            88,
        )
        self.assertIsNone(llm_mod._extract_gemini_audio_tokens({"usageMetadata": {"promptTokensDetails": "bad"}}))
        self.assertIsNone(
            llm_mod._extract_gemini_audio_tokens(
                {"usageMetadata": {"promptTokensDetails": [{"modality": "TEXT", "tokenCount": 5}, {"modality": "AUDIO"}]}}
            )
        )
        self.assertEqual(
            llm_mod._extract_gemini_audio_tokens(
                {
                    "usageMetadata": {
                        "prompt_tokens_details": [
                            {"modality": "audio", "token_count": 30},
                            {"modality": "AUDIO", "tokenCount": 12},
                        ]
                    }
                }
            ),
            42,
        )

        self.assertEqual(llm_mod.strip_lenticular_artifacts("你好【 \t 】世界"), "你好世界")
        self.assertEqual(llm_mod.strip_lenticular_artifacts("前】中间保留【后"), "前】中间保留【后")
        self.assertEqual(llm_mod.strip_lenticular_artifacts("测试】 【清理"), "测试 清理")
        self.assertEqual(llm_mod.strip_lenticular_artifacts("测试】【清理"), "测试清理")

    def test_effort_mapping_message_extraction_and_key_hints(self) -> None:
        with self.assertRaises(ConfigError):
            llm_mod.build_gemini_generation_config("gemini-3.5-flash", "bogus")
        self.assertEqual(
            llm_mod.build_gemini_generation_config("gemini-2.0-flash", "low"),
            {"temperature": 0.1},
        )
        self.assertEqual(
            llm_mod.build_gemini_generation_config("gemini-2.5-pro", "auto")["thinkingConfig"],
            {"thinkingBudget": 128},
        )
        self.assertEqual(
            llm_mod.build_gemini_generation_config("gemini-3-pro", "minimal")["thinkingConfig"],
            {"thinkingLevel": "low"},
        )

        with self.assertRaises(ConfigError):
            llm_mod.apply_chat_effort_params({}, provider="openai", model="gpt-4o-mini", effort="bogus")

        # OpenRouter thinking model branches
        for or_model in ("anthropic/claude-3.7-sonnet:thinking", "qwen/qwen3-32b", "openai/o3-mini"):
            p_or: dict[str, object] = {}
            llm_mod.apply_chat_effort_params(p_or, provider="openrouter", model=or_model, effort="auto")
            self.assertEqual(p_or["reasoning"], {"effort": "none"})

        # DashScope hybrid vs non-hybrid (thinking / asr) vs omni
        p_ds_text: dict[str, object] = {}
        llm_mod.apply_chat_effort_params(p_ds_text, provider="dashscope", model="qwen3.8-flash", effort="low")
        self.assertEqual(p_ds_text, {"enable_thinking": True})

        p_ds_skip: dict[str, object] = {}
        llm_mod.apply_chat_effort_params(p_ds_skip, provider="dashscope", model="qwen3-asr-flash", effort="low")
        self.assertEqual(p_ds_skip, {})

        # Volcengine thinking enabled with effort clamping & non-thinking no-op
        p_volc_hi: dict[str, object] = {"temperature": 0.2}
        llm_mod.apply_chat_effort_params(
            p_volc_hi, provider="volcengine", model="doubao-seed-2-0-mini-260428", effort="max"
        )
        self.assertEqual(p_volc_hi, {"thinking": {"type": "enabled"}, "reasoning_effort": "high"})

        p_volc_plain: dict[str, object] = {"temperature": 0.2}
        llm_mod.apply_chat_effort_params(
            p_volc_plain, provider="volcengine", model="doubao-lite-32k", effort="low"
        )
        self.assertEqual(p_volc_plain, {"temperature": 0.2})

        # DeepSeek via openai-compatible URL
        p_ds_compat: dict[str, object] = {}
        llm_mod.apply_chat_effort_params(
            p_ds_compat,
            provider="openai-compatible",
            model="deepseek-reasoner",
            effort="auto",
            base_url="https://api.deepseek.com/v1",
        )
        self.assertEqual(p_ds_compat, {})
        llm_mod.apply_chat_effort_params(
            p_ds_compat,
            provider="deepseek",
            model="deepseek-flash",
            effort="medium",
        )
        self.assertEqual(p_ds_compat, {"thinking": {"type": "enabled"}})

        # Multimodal content array & error extraction
        self.assertEqual(llm_mod.extract_chat_message_text({"choices": []}), "")
        self.assertEqual(llm_mod.extract_chat_message_text({"choices": [{"message": None}]}), "")
        self.assertEqual(llm_mod.extract_chat_message_text({"choices": [{"message": {"content": 123}}]}), "")
        self.assertEqual(
            llm_mod.extract_chat_message_text(
                {
                    "choices": [
                        {
                            "message": {
                                "content": [
                                    "Hello ",
                                    {"type": "reasoning", "text": "internal"},
                                    {"thought": True, "text": "hidden"},
                                    {"type": "image_url", "url": "x"},
                                    99,
                                    {"type": "output_text", "text": "world!"},
                                ]
                            }
                        }
                    ]
                }
            ),
            "Hello world!",
        )

        self.assertEqual(llm_mod.extract_openai_compat_error({"error": "plain failure"}), ("plain failure", "plain failure"))
        self.assertEqual(
            llm_mod.extract_openai_compat_error({"choices": [{"error": {"message": "choice boom"}}]}),
            ({"message": "choice boom"}, "choice boom"),
        )
        self.assertEqual(
            llm_mod.extract_openai_compat_error({"choices": [{"error": "raw choice boom"}]}),
            ("raw choice boom", "raw choice boom"),
        )
        self.assertEqual(
            llm_mod.extract_openai_compat_error({"choices": [{"finish_reason": "error"}]}),
            ({"type": "server_error"}, "upstream generation aborted with finish_reason=error"),
        )

        self.assertIn("DASHSCOPE_API_KEY", llm_mod._missing_key_hint("dashscope", stage="asr"))
        self.assertEqual(
            llm_mod._missing_key_hint("openai-compatible", stage="refiner"),
            "missing openai-compatible API key (set refiner.api_key_env or OPENAI_API_KEY)",
        )


class TestConfigTomlAndValidationScenarios(unittest.TestCase):
    def test_simple_toml_parser_comprehensive_valid_constructs(self) -> None:
        toml_doc = (
            "\ufeff# Leading comment with CRLF\r\n"
            "title = \"Escapes: \\b\\t\\n\\f\\r\\\"\\\\ \\u4F60\\U0001F600\"\r\n"
            "lit = 'C:\\Users\\dev\\literal'\r\n"
            "ml_basic = \"\"\"\r\n"
            "alpha \\\t\r\n"
            "  \r\n"
            "beta\r\n"
            "gamma\n"
            "\\u597D\\U0001F680\"\"\"\"\" # ends with 5 quotes -> 2 trailing quotes in value\r\n"
            "ml_lit = '''\r\n"
            "line1\r\n"
            "line2\n"
            "''''\r\n"
            '"quoted.seg" . \'lit.seg\' . leaf = 0x1a_2b\r\n'
            "flags = [true, false]\r\n"
            "floats = [1.5, 2e-3, -0.25]\r\n"
            "matrix = [[1, 2], [3_000, 4]]\r\n"
            "\r\n"
            "[[clusters]]\r\n"
            "name = \"c1\"\r\n"
            "[clusters.meta]\r\n"
            "role = \"primary\"\r\n"
            "[[clusters.nodes]]\r\n"
            "ip = \"10.0.0.1\"\r\n"
            "[[clusters.nodes]]\r\n"
            "ip = \"10.0.0.2\"\r\n"
            "\r\n"
            "[[clusters]]\r\n"
            "name = \"c2\"\r\n"
            "[clusters.meta]\r\n"
            "role = \"replica\"\r\n"
        )
        parsed = _parse_simple_toml(toml_doc)
        self.assertEqual(parsed["title"], 'Escapes: \b\t\n\f\r"\\ 你😀')
        self.assertEqual(parsed["ml_basic"], 'alpha beta\ngamma\n好🚀""')
        self.assertEqual(parsed["ml_lit"], "line1\nline2\n'")
        self.assertEqual(parsed["quoted.seg"]["lit.seg"]["leaf"], 0x1A2B)
        self.assertEqual(parsed["flags"], [True, False])
        self.assertEqual(parsed["floats"], [1.5, 0.002, -0.25])
        self.assertEqual(parsed["matrix"], [[1, 2], [3000, 4]])
        self.assertEqual(len(parsed["clusters"]), 2)
        self.assertEqual(parsed["clusters"][0]["meta"]["role"], "primary")
        self.assertEqual(len(parsed["clusters"][0]["nodes"]), 2)
        self.assertEqual(parsed["clusters"][1]["meta"]["role"], "replica")
        self.assertEqual(parse_toml("\ufeff[ui]\nposition = 'top'\n")["ui"]["position"], "top")
        with self.assertRaises(ConfigError):
            parse_toml("broken = [1, ")

    def test_simple_toml_parser_syntax_error_diagnostics_matrix(self) -> None:
        bad_cases: list[tuple[str, str]] = [
            ('k = "unterminated\n', "unterminated string"),
            ('k = "eof_unterminated', "unterminated double-quoted"),
            ('k = "bad\\', "unterminated escape"),
            ('k = "bad\\\n"', "unterminated escape"),
            ('k = "\\q"', "unsupported escape"),
            ('k = "\\u12G4"', "invalid Unicode escape"),
            ('k = "\\uD800"', "invalid Unicode scalar"),
            ('k = "ctrl\x01char"', "control character"),
            ('k = """unterminated', "unterminated multiline double-quoted"),
            ('k = """too_many""""""', "too many consecutive quotes"),
            ('k = """esc_eof\\', "unterminated escape"),
            ('k = """\\q"""', "unsupported escape"),
            ('k = """ctrl\x01"""', "control character"),
            ("k = 'unterminated\n", "unterminated single-quoted"),
            ("k = 'eof_single", "unterminated single-quoted"),
            ("k = 'ctrl\x01'", "control character"),
            ("k = '''unterminated", "unterminated multiline single-quoted"),
            ("k = '''too_many''''''", "too many consecutive quotes"),
            ("k = '''ctrl\x01'''", "control character"),
            ("# comment with \x01\nk = 1\n", "control character"),
            ("bare\rk = 1\n", "control character"),
            ("k = 1 extra_tok\n", "unexpected trailing tokens"),
            ("k = 1 extra_tok\r\n", "unexpected trailing tokens"),
            ("k = 1 extra_eof", "unexpected trailing tokens"),
            ("k = 1 \x01\n", "control character"),
            (" = 1\n", "empty key"),
            ("a. = 1\n", "trailing dot"),
            ("a.   \n", "expected 'key = value'"),
            ('"""ml""" = 1\n', "multiline string not allowed in key"),
            ("'''ml''' = 1\n", "multiline string not allowed in key"),
            ("bad$key = 1\n", "invalid key segment"),
            ('"a" "b" = 1\n', "unexpected character"),
            ("a . = 1\n", "trailing dot"),
            ("k = [1, 2", "unterminated array"),
            ("k = [1, ", "unterminated array"),
            ("k = [1 2]", "expected ',' or ']'"),
            ('k = [1, "two"]', "heterogeneous array"),
            ("k = 1e999\n", "non-finite float"),
            ("k = bare_word\n", "invalid TOML value"),
            ("k = val\x01\n", "control character"),
            ("[[unclosed_aot]\n", "invalid array-of-tables header"),
            ("[unclosed_tbl\n", "invalid table header"),
            ("[dup]\na = 1\n[dup]\nb = 2\n", "duplicate table"),
            ("[tbl]\na = 1\n[[tbl]]\na = 2\n", "cannot redefine table"),
            ("s = 1\n[[s.sub]]\na = 2\n", "cannot traverse non-table key"),
            ("s = [1, 2]\n[[s]]\na = 3\n", "cannot redefine key"),
            ("s = 1\n[s]\na = 2\n", "cannot redefine key"),
            ("just_a_word # trailing comment\n", "expected 'key = value'"),
            ("bad\x01line\n", "control character"),
            ("k = # missing\n", "missing value after '='"),
            ("k =\r", "control character"),
            ("a = 1\na.b = 2\n", "key conflict"),
            ("a = 1\na = 2\n", "duplicate key"),
        ]
        for idx, (doc, expected_msg) in enumerate(bad_cases):
            with self.subTest(case=idx, doc=doc):
                with self.assertRaisesRegex(ConfigError, expected_msg):
                    _parse_simple_toml(doc)

    def test_env_json_cli_overrides_and_config_paths(self) -> None:
        env_parsed = parse_env_text(
            "\ufeff# comment\n"
            "export EMPTY_VAL=\n"
            "UNCLOSED_SINGLE='hello_world\n"
            'ESCAPED_DBL="a\\nb\\rc\\td\\"e\\\\f\\$g\\zh"\n'
            "EMPTY_HASH= # inline comment\n"
            "NO_EQUALS_LINE\n"
            "1INVALID_NAME=abc\n"
        )
        self.assertEqual(env_parsed["EMPTY_VAL"], "")
        self.assertEqual(env_parsed["UNCLOSED_SINGLE"], "hello_world")
        self.assertEqual(env_parsed["ESCAPED_DBL"], 'a\nb\rc\td"e\\f$gzh')
        self.assertEqual(env_parsed["EMPTY_HASH"], "")
        self.assertNotIn("1INVALID_NAME", env_parsed)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertEqual(expand_user_path("~", home_dir=root), root)
            self.assertEqual(expand_user_path("~/cfg", home_dir=root), root / "cfg")

            # VOICE_INPUT_CONFIG fallback in target paths, env candidates, and load_config
            vi_cfg = root / "custom" / "voice.json"
            vi_cfg.parent.mkdir(parents=True)
            vi_cfg.write_text(
                json.dumps({"ui": {"position": "top"}, "refiner": {"provider": "deepseek"}}),
                encoding="utf-8",
            )
            env_vi = {"HOME": str(root), "VOICE_INPUT_CONFIG": str(vi_cfg), "XDG_CONFIG_HOME": str(root / ".config")}
            cdir, ctoml, cenv = resolve_target_config_paths(env=env_vi, home_dir=root)
            self.assertEqual((cdir, ctoml, cenv), (vi_cfg.parent, vi_cfg, vi_cfg.parent / "agent-voice-input.env"))
            self.assertIn(vi_cfg.parent / "agent-voice-input.env", default_env_file_candidates(env_vi))
            self.assertTrue(len(default_config_file_candidates(env_vi)) >= 4)

            cfg_loaded, _ = load_config(
                env=env_vi,
                cli_overrides={"hotkey": {"trigger_mode": "ptt"}, "audio.beep": "yes"},
            )
            self.assertEqual(cfg_loaded.ui.position, "top")
            self.assertEqual(cfg_loaded.hotkey.trigger_mode, "ptt")
            self.assertTrue(cfg_loaded.audio.beep)

            # Missing explicit config files & malformed JSON roots
            with self.assertRaisesRegex(ConfigError, "AGENT_VOICE_INPUT_CONFIG file does not exist"):
                load_config(env={"HOME": str(root), "AGENT_VOICE_INPUT_CONFIG": str(root / "missing.toml")})
            with self.assertRaisesRegex(ConfigError, "VOICE_INPUT_CONFIG file does not exist"):
                load_config(env={"HOME": str(root), "VOICE_INPUT_CONFIG": str(root / "missing2.toml")})

            bad_json = root / "bad.json"
            bad_json.write_text("{invalid", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "invalid JSON"):
                load_config(config_path=bad_json, env={"HOME": str(root)})

            arr_json = root / "arr.json"
            arr_json.write_text("[1, 2, 3]", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "JSON config root must be an object"):
                load_config(config_path=arr_json, env={"HOME": str(root)})

            with self.assertRaisesRegex(ConfigError, "invalid CLI override key"):
                load_config(config_path=vi_cfg, env={"HOME": str(root)}, cli_overrides={"bad_scalar": 1})

    def test_provider_inference_codex_discovery_and_redaction_matrix(self) -> None:
        ref_url_cases = [
            ("https://api.deepseek.com/v1", "deepseek"),
            ("https://dashscope.aliyuncs.com/compatible-mode/v1", "dashscope"),
            ("https://ark.cn-beijing.volces.com/api/v3", "volcengine"),
            ("https://openrouter.ai/api/v1", "openrouter"),
            ("https://generativelanguage.googleapis.com/v1beta", "gemini"),
            ("https://api.openai.com/v1", "openai"),
            ("https://api.groq.com/openai/v1", "groq"),
            ("https://api.siliconflow.cn/v1", "siliconflow"),
            ("https://chatgpt.com/backend-api/codex", "codex"),
            ("https://llm-gateway.corp.example/v1", "openai-compatible"),
        ]
        for url, expected_prov in ref_url_cases:
            rc = RefinerConfig(provider="auto", base_url=url, model="m")
            self.assertEqual(_infer_provider_from_explicit_refiner_hints(rc), expected_prov)
            cfg = Config(refiner=rc)
            self.assertEqual(select_refiner_provider(cfg, resolve_credentials(cfg, {}), {"HOME": ""}), expected_prov)

        self.assertEqual(
            _infer_provider_from_explicit_refiner_hints(RefinerConfig(provider="auto", api_key_env="GROQ_API_KEY")),
            "groq",
        )
        self.assertEqual(
            _infer_provider_from_explicit_refiner_hints(RefinerConfig(provider="auto", api_key_env="CORP_LLM_KEY")),
            "openai-compatible",
        )

        asr_url_cases = [
            ("https://dashscope.aliyuncs.com/compatible-mode/v1", "dashscope"),
            ("https://openspeech.bytedance.com/api/v3/auc/bigmodel", "doubao"),
            ("https://openrouter.ai/api/v1", "openrouter"),
            ("https://generativelanguage.googleapis.com/v1beta", "gemini"),
            ("https://api.openai.com/v1", "openai"),
            ("https://api.groq.com/openai/v1", "groq"),
            ("https://api.siliconflow.cn/v1", "siliconflow"),
            ("https://stt.corp.example/v1", "openai-compatible"),
        ]
        for url, expected_prov in asr_url_cases:
            ac = AsrConfig(provider="auto", base_url=url)
            self.assertEqual(_infer_provider_from_explicit_asr_hints(ac), expected_prov)
            cfg = Config(asr=ac)
            self.assertEqual(select_asr_provider(cfg, resolve_credentials(cfg, {})), expected_prov)

        self.assertEqual(
            _infer_provider_from_explicit_asr_hints(AsrConfig(provider="auto", doubao_mode="flash")),
            "doubao",
        )
        self.assertEqual(
            _infer_provider_from_explicit_asr_hints(AsrConfig(provider="auto", api_key_env="DOUBAO_ASR_API_KEY")),
            "doubao",
        )
        self.assertEqual(
            _infer_provider_from_explicit_asr_hints(AsrConfig(provider="auto", api_key_env="GROQ_API_KEY")),
            "groq",
        )
        self.assertEqual(
            _infer_provider_from_explicit_asr_hints(AsrConfig(provider="auto", api_key_env="CORP_STT_KEY")),
            "openai-compatible",
        )

        self.assertTrue(is_gemini_native_url("https://generativelanguage.googleapis.com/v1beta"))
        self.assertFalse(is_gemini_native_url("https://generativelanguage.googleapis.com/v1beta/openai"))
        self.assertFalse(is_gemini_native_url("https://openrouter.ai/api/v1"))
        self.assertFalse(cfg_mod._host_matches_domain("http://[::1", "openai.com"))
        self.assertFalse(cfg_mod._host_matches_domain("not-a-url", "openai.com"))
        self.assertTrue(
            is_dashscope_native_multimodal_model("qwen3-asr-flash", "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation")
        )
        self.assertIsNone(classify_asr_model_api(""))

        # Real temporary NVM / fnm Codex binary discovery & local auth edge cases
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            self.assertFalse(is_hermetic_env(None))
            self.assertIsNone(resolve_codex_home_dir({"CODEX_HOME": "~otheruser/.codex"}))
            self.assertEqual(
                resolve_codex_home_dir({"HOME": str(home), "CODEX_HOME": "~/.codex-alt"}),
                home / ".codex-alt",
            )
            self.assertIsNone(find_codex_bin({"CODEX_BIN": "~otheruser/bin/codex"}))

            nvm_v20 = home / ".nvm" / "versions" / "node" / "v20.11.0" / "bin"
            nvm_v22 = home / ".nvm" / "versions" / "node" / "v22.4.0" / "bin"
            fnm_v21 = home / ".local" / "share" / "fnm" / "node-versions" / "v21.0.0" / "installation" / "bin"
            for d in (nvm_v20, nvm_v22, fnm_v21):
                d.mkdir(parents=True)
                bin_f = d / "codex"
                bin_f.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                os.chmod(bin_f, 0o755)

            found_nvm = find_codex_bin(
                {"HOME": str(home), "NVM_DIR": str(home / ".nvm"), "PATH": ""}
            )
            self.assertEqual(found_nvm, str(nvm_v22 / "codex"))

            # Direct PATH hit
            found_path = find_codex_bin({"HOME": str(home), "PATH": str(fnm_v21)})
            self.assertEqual(found_path, str(fnm_v21 / "codex"))

            # Codex auth.json variants: corrupt JSON -> keyring fallback, OPENAI_API_KEY in json, auth_mode=api_key
            cdir = home / ".codex"
            cdir.mkdir()
            auth_f = cdir / "auth.json"
            cfg_f = cdir / "config.toml"

            auth_f.write_text("{corrupt-json", encoding="utf-8")
            cfg_f.write_text("", encoding="utf-8")  # 0-byte config.toml -> False
            self.assertFalse(has_codex_local_auth({"HOME": str(home), "PATH": ""}))

            cfg_f.write_text('cli_auth_credentials_store = "keyring"\n', encoding="utf-8")
            self.assertTrue(has_codex_local_auth({"HOME": str(home), "PATH": str(nvm_v22)}))

            auth_f.write_text(json.dumps({"OPENAI_API_KEY": "sk-local-auth-key"}), encoding="utf-8")
            self.assertTrue(has_codex_local_auth({"HOME": str(home), "PATH": ""}))

            auth_f.write_text(json.dumps({"auth_mode": "api_key"}), encoding="utf-8")
            self.assertTrue(
                has_codex_local_auth({"HOME": str(home), "PATH": "", "OPENAI_API_KEY": "sk-env-key"})
            )

            # Credentials None cfg & secret redaction URL/JSON escaping
            creds_none = resolve_credentials(None, {"DASHSCOPE_API_KEY": "ds-secret-val"})
            self.assertEqual(creds_none.dashscope.value, "ds-secret-val")  # type: ignore[union-attr]
            red = redact_secrets(
                'url=https://x?k=sec%2Fval%2B1 json="sec\\"q" short=ab',
                {"MY_SECRET_TOKEN": 'sec"q'},
                extra_secrets=["ab", "sec/val+1"],
            )
            self.assertNotIn("sec%2Fval%2B1", red)
            self.assertNotIn('sec\\"q', red)
            self.assertIn("short=ab", red)

    def test_section_validation_and_remote_host_lookup_errors(self) -> None:
        # RemoteConfig list conversion & lookup_host unique short name vs ambiguous short name
        rc = RemoteConfig(
            hosts=[  # type: ignore[arg-type]
                RemoteHostConfig(name="gpu01.corp.internal", ssh_target="ubuntu@gpu01.corp.internal"),
                RemoteHostConfig(name="web01.prod.internal", ssh_target="ubuntu@web01.prod.internal"),
                RemoteHostConfig(name="web01.stage.internal", ssh_target="ubuntu@web01.stage.internal"),
            ]
        )
        self.assertIsInstance(rc.hosts, tuple)
        self.assertIsNone(rc.lookup_host(""))
        self.assertIsNone(rc.lookup_host("ubuntu@-bad"))
        self.assertEqual(rc.lookup_host("ubuntu@gpu01.corp.internal").name, "gpu01.corp.internal")  # type: ignore[union-attr]
        self.assertEqual(rc.lookup_host("gpu01").name, "gpu01.corp.internal")  # type: ignore[union-attr]
        self.assertIsNone(rc.lookup_host("web01"))  # Ambiguous across prod & stage!

        # Section dict & field validation errors
        bad_section_dicts: list[tuple[dict[str, object], str]] = [
            ({"unknown_sec": {}}, "unknown top-level config section"),
            ({"hotkey": "not_a_table"}, r"\[hotkey\] must be a table"),
            ({"hotkey": {"bad_opt": 1}}, "unknown hotkey option"),
            ({"hotkey": {"key": "   "}}, "hotkey.key must not be empty"),
            ({"hotkey": {"trigger_mode": "hold"}}, "hotkey.trigger_mode"),
            ({"hotkey": {"tap_max_ms": 0}}, "hotkey.tap_max_ms must be > 0"),
            ({"hotkey": {"debounce_ms": -5}}, "hotkey.debounce_ms must be >= 0"),
            ({"hotkey": {"debounce_ms": "1_00"}}, "NEVER_RAISE"),  # valid string int check below
            ({"hotkey": {"scope": "desktop"}}, "hotkey.scope"),
            ({"hotkey": {"yield_remote_desktop": "maybe"}}, "invalid boolean"),
            ({"hotkey": {"ignore_apps": ["", "***"]}}, "bare wildcard"),
            ({"ui": "bad"}, r"\[ui\] must be a table"),
            ({"ui": {"theme": "dark"}}, "unknown ui option"),
            ({"audio": "bad"}, r"\[audio\] must be a table"),
            ({"audio": {"bad_au": 1}}, "unknown audio option"),
            ({"audio": {"floor_dbfs": True}}, "got boolean"),
            ({"audio": {"floor_dbfs": "not-float"}}, "invalid float"),
            ({"audio": {"wsl_source": "alsa"}}, "audio.wsl_source"),
            ({"audio": {"min_duration_sec": -0.1}}, "min_duration_sec"),
            ({"audio": {"max_duration_sec": -1}}, "max_duration_sec"),
            ({"asr": "bad"}, r"\[asr\] must be a table"),
            ({"asr": {"bad_asr": 1}}, "unknown asr option"),
            ({"asr": {"provider": "unknown"}}, "asr.provider"),
            ({"asr": {"api": "grpc"}}, "asr.api"),
            ({"asr": {"doubao_mode": "stream"}}, "asr.doubao_mode"),
            ({"asr": {"timeout_sec": 0}}, "asr.timeout_sec"),
            ({"asr": {"provider": "auto", "api_key_env": "CORP_STT_KEY"}}, "asr.base_url must be explicitly set"),
            ({"refiner": "bad"}, r"\[refiner\] must be a table"),
            ({"refiner": {"bad_ref": 1}}, "unknown refiner option"),
            ({"refiner": {"provider": "unknown"}}, "refiner.provider"),
            ({"refiner": {"provider": "deepseek", "effort": "turbo"}}, "refiner.effort"),
            ({"refiner": {"provider": "auto", "effort": "turbo"}}, "refiner.effort"),
            ({"refiner": {"max_turns_per_thread": 0}}, "max_turns_per_thread"),
            ({"refiner": {"timeout_sec": 0}}, "refiner.timeout_sec"),
            ({"refiner": {"provider": "auto", "api_key_env": "CORP_LLM_KEY"}}, "refiner.base_url"),
            ({"refiner": {"provider": "auto", "base_url": "https://llm.corp/v1"}}, "refiner.model"),
            ({"remote": "bad"}, r"\[remote\] must be a table"),
            ({"remote": {"bad_rem": 1}}, "unknown remote option"),
            ({"remote": {"timeout_ms": 99999}}, "remote.timeout_ms"),
            ({"remote": {"ssh_bin": "   "}}, "must not be empty"),
            ({"remote": {"ssh_bin": "ssh\x01"}}, "control characters"),
            ({"remote": {"ssh_bin": "bad$ssh"}}, "invalid executable name"),
            ({"remote": {"hosts": "not-a-list"}}, "remote.hosts: expected array"),
            ({"remote": {"hosts": ["   "]}}, r"remote\.hosts\[0\] must not be empty"),
            ({"remote": {"hosts": [123]}}, "expected string or table"),
            ({"remote": {"hosts": [{"bad_k": "x"}]}}, "unknown remote.hosts"),
            ({"remote": {"hosts": [{}]}}, "must specify 'name' or 'ssh_target'"),
            ({"remote": {"hosts": [{"name": "a", "aliases": "bad"}]}}, "expected array of strings"),
            (
                {
                    "remote": {
                        "hosts": [
                            {"name": "box1", "ssh_target": "u@box1", "aliases": ["shared"]},
                            {"name": "box2", "ssh_target": "u@box2", "aliases": ["shared"]},
                        ]
                    }
                },
                "duplicate host name or alias",
            ),
            (
                {"remote": {"hosts": [{"name": "dup", "ssh_target": "u@b1"}, {"name": "dup", "ssh_target": "u@b2"}]}},
                "duplicate host name",
            ),
        ]
        for idx, (sec_dict, pat) in enumerate(bad_section_dicts):
            if pat == "NEVER_RAISE":
                c_ok = Config()
                cfg_mod._apply_section_dict(c_ok, sec_dict)
                self.assertEqual(c_ok.hotkey.debounce_ms, 100)
                continue
            with self.subTest(case=idx, sec=sec_dict):
                c = Config()
                with self.assertRaisesRegex(ConfigError, pat):
                    cfg_mod._apply_section_dict(c, sec_dict)
                    cfg_mod._validate_config(c, env={"CORP_STT_KEY": "k1", "CORP_LLM_KEY": "k2"})

        # Direct programmatic dataclass edge checks
        self.assertEqual(RefinerConfig(provider="disabled").effective_api(""), "off")
        self.assertEqual(_normalize_off := cfg_mod._normalize_refiner_provider(False), "off")
        self.assertEqual(_normalize_off, "off")
        with self.assertRaises(ConfigError):
            RefinerConfig(api=123).effective_api()  # type: ignore[arg-type]
        with self.assertRaises(ConfigError):
            resolve_asr_api("openai", "gpt-4o-mini-transcribe", api="invalid-api")
        with self.assertRaises(ConfigError):
            RemoteHostConfig(name="-bad", ssh_target="ubuntu@10.0.0.1")
        with self.assertRaises(ConfigError):
            RemoteHostConfig(name="ok", ssh_target="ubuntu@10.0.0.1", aliases="not-tuple")  # type: ignore[arg-type]
        with self.assertRaises(ConfigError):
            RemoteHostConfig(name="ok", ssh_target="ubuntu@10.0.0.1", aliases=(123,))  # type: ignore[arg-type]
        with self.assertRaises(ConfigError):
            cfg_mod._validate_audio_config(Config(audio=cfg_mod.AudioConfig(max_duration_sec=True)))  # type: ignore[arg-type]
        with self.assertRaises(ConfigError):
            cfg_mod._validate_audio_config(Config(audio=cfg_mod.AudioConfig(save_recordings="bad")))  # type: ignore[arg-type]
        with self.assertRaises(ConfigError):
            cfg_mod._validate_refiner_config(Config(refiner=RefinerConfig(prompt=123)))  # type: ignore[arg-type]
        with self.assertRaises(ConfigError):
            cfg_mod._validate_refiner_config(Config(refiner=RefinerConfig(include_git_status="bad")))  # type: ignore[arg-type]
        with self.assertRaises(ConfigError):
            cfg_mod._validate_refiner_config(
                Config(refiner=RefinerConfig(provider="auto", api="chat-completions")),
                env={"HOME": "/nonexistent", "GEMINI_API_KEY": "AIzaSyDummyKeyForValidation"},
            )

        # Skipping None values in section dicts & boolean string false
        c_nones = Config()
        cfg_mod._apply_section_dict(
            c_nones,
            {
                "hotkey": {"key": None, "yield_remote_desktop": "off"},
                "ui": {"language": None},
                "audio": {"floor_dbfs": None, "target_dbfs": -18.0, "min_voiced_sec": 0.2},
                "asr": {"provider": None},
                "refiner": {"provider": None},
                "remote": {"hosts": None},
            },
        )
        cfg_mod._validate_config(c_nones)
        self.assertFalse(c_nones.hotkey.yield_remote_desktop)
        self.assertEqual(c_nones.audio.target_dbfs, -18.0)


class TestCrossLayerHighValueScenarios(unittest.TestCase):
    def test_refiner_https_proxy_resolution_matrix(self) -> None:
        pool_no_exp = _InstanceKeepAlivePool({"HTTPS_PROXY": "http://10.0.0.2:8888"}, explicit_env=False)
        self.assertIsNone(pool_no_exp._resolve_https_proxy("127.0.0.1", 443))
        self.assertIsNone(pool_no_exp._resolve_https_proxy("localhost", 443))

        self.assertIsNone(
            _InstanceKeepAlivePool(
                {"HTTPS_PROXY": "http://proxy.corp:8080", "NO_PROXY": "*"}, explicit_env=True
            )._resolve_https_proxy("api.deepseek.com", 443)
        )
        pool_np = _InstanceKeepAlivePool(
            {
                "ALL_PROXY": "proxy.corp",
                "no_proxy": ".internal.corp, direct.example.com:443",
            },
            explicit_env=True,
        )
        self.assertIsNone(pool_np._resolve_https_proxy("svc.internal.corp", 443))
        self.assertIsNone(pool_np._resolve_https_proxy("direct.example.com", 443))
        self.assertEqual(pool_np._resolve_https_proxy("api.openai.com", 443), ("proxy.corp", 8080, {}))

        # Unsupported scheme / invalid port / empty proxy -> None
        self.assertIsNone(_InstanceKeepAlivePool({}, explicit_env=True)._resolve_https_proxy("api.openai.com", 443))
        self.assertIsNone(
            _InstanceKeepAlivePool({"HTTPS_PROXY": "socks5://127.0.0.1:1080"}, explicit_env=True)._resolve_https_proxy(
                "api.openai.com", 443
            )
        )
        self.assertIsNone(
            _InstanceKeepAlivePool({"HTTPS_PROXY": "http://proxy.corp:99999"}, explicit_env=True)._resolve_https_proxy(
                "api.openai.com", 443
            )
        )

        # Basic Proxy-Authorization header with percent-encoded credentials
        pool_auth = _InstanceKeepAlivePool(
            {"HTTPS_PROXY": "http://dev%40corp:s3cr%3At@gw.corp.local:3128"},
            explicit_env=True,
        )
        host, port, hdrs = pool_auth._resolve_https_proxy("api.deepseek.com", 443)  # type: ignore[misc]
        self.assertEqual((host, port), ("gw.corp.local", 3128))
        expected_b64 = base64.b64encode(b"dev@corp:s3cr:t").decode("ascii")
        self.assertEqual(hdrs, {"Proxy-Authorization": f"Basic {expected_b64}"})

    def test_same_origin_safe_redirect_handler_over_real_loopback_http(self) -> None:
        class _RedirectApp(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                del format, args

            def _base(self) -> str:
                host, port = self.server.server_address[:2]
                return f"http://{host}:{port}"

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/r302":
                    self.send_response(302)
                    self.send_header("Location", f"{self._base()}/ok")
                    self.end_headers()
                    self.wfile.write(b"redirecting")
                elif self.path == "/r_root_no_slash":
                    self.send_response(301)
                    self.send_header("URI", self._base())
                    self.end_headers()
                elif self.path == "/ok" or self.path == "/":
                    body = f"GET:{self.path}".encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path == "/cross_port":
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1:1/steal")
                    self.end_headers()
                elif self.path == "/bad_scheme":
                    self.send_response(302)
                    self.send_header("Location", "ftp://127.0.0.1/file")
                    self.end_headers()
                elif self.path == "/loop":
                    self.send_response(302)
                    self.send_header("Location", f"{self._base()}/loop")
                    self.end_headers()
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or "0")
                payload = self.rfile.read(length) if length > 0 else b""
                if self.path == "/r307":
                    self.send_response(307)
                    self.send_header("Location", "/echo post")
                    self.end_headers()
                elif self.path == "/echo%20post":
                    resp = b"POST:" + payload + b":" + self.headers.get("X-Custom", "").encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(resp)))
                    self.end_headers()
                    self.wfile.write(resp)
                else:
                    self.send_response(404)
                    self.end_headers()

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RedirectApp)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            host, port = srv.server_address[:2]
            base = f"http://{host}:{port}"
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                _SameOriginSafeRedirectHandler(),
            )

            # 1. Same-origin 302 and 301 (URI header + empty path normalization)
            req_get = urllib.request.Request(f"{base}/r302")
            setattr(req_get, "_voice_deadline", time.monotonic() + 5.0)
            setattr(req_get, "_voice_req_timeout", 2.0)
            with opener.open(req_get, timeout=2.0) as resp:
                self.assertEqual(resp.read(), b"GET:/ok")

            with opener.open(f"{base}/r_root_no_slash", timeout=2.0) as resp_root:
                self.assertEqual(resp_root.read(), b"GET:/")

            # 2. Same-origin 307 POST preserves method, body, custom headers, and %20 URL escaping
            req_post = urllib.request.Request(
                f"{base}/r307",
                data=b"audio-bytes",
                headers={"X-Custom": "kept"},
                method="POST",
            )
            setattr(req_post, "_voice_deadline", time.monotonic() + 5.0)
            setattr(req_post, "_voice_connect_timeout", 2.0)
            with opener.open(req_post, timeout=2.0) as resp_post:
                self.assertEqual(resp_post.read(), b"POST:audio-bytes:kept")

            # 3. Cross-origin port, disallowed scheme, infinite redirect loop, and expired deadline
            with self.assertRaisesRegex(urllib.error.URLError, "refusing cross-origin redirect"):
                opener.open(f"{base}/cross_port", timeout=2.0)

            with self.assertRaisesRegex(urllib.error.URLError, "not allowed"):
                opener.open(f"{base}/bad_scheme", timeout=2.0)

            with self.assertRaises(urllib.error.URLError):
                opener.open(f"{base}/loop", timeout=2.0)

            req_exp = urllib.request.Request(f"{base}/r302")
            setattr(req_exp, "_voice_deadline", time.monotonic() - 0.1)
            with self.assertRaises(TimeoutError):
                opener.open(req_exp, timeout=1.0)

            # 4. Non-loopback plain HTTP redirect rejection
            handler = _SameOriginSafeRedirectHandler()
            ext_req = urllib.request.Request("http://api.example.com/v1/asr")
            with self.assertRaisesRegex(urllib.error.URLError, "refusing non-HTTPS redirect"):
                handler.redirect_request(
                    ext_req, io.BytesIO(b""), 302, "Found", {}, "http://api.example.com/v1/asr2"
                )
        finally:
            srv.shutdown()
            srv.server_close()
            t.join(timeout=2.0)

    def test_dashscope_regional_url_and_ssh_argv_target_extraction(self) -> None:
        ds_cases = [
            ("", True, "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"),
            ("", False, "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"),
            (
                "https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
                True,
                "https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
            ),
            (
                "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/",
                True,
                "https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
            ),
            (
                "https://dashscope-intl.aliyuncs.com/api/v1",
                True,
                "https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
            ),
            (
                "https://proxy.corp.local/dashscope",
                True,
                "https://proxy.corp.local/dashscope/api/v1/services/aigc/multimodal-generation/generation",
            ),
            (
                "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions",
                False,
                "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions",
            ),
            (
                "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                False,
                "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions",
            ),
            (
                "https://dashscope-intl.aliyuncs.com/api/v1",
                False,
                "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions",
            ),
            (
                "https://gateway.corp.local/v1",
                False,
                "https://gateway.corp.local/v1/chat/completions",
            ),
            (
                "https://gateway.corp.local/root",
                False,
                "https://gateway.corp.local/root/compatible-mode/v1/chat/completions",
            ),
        ]
        for raw_url, use_native, expected in ds_cases:
            self.assertEqual(_resolve_dashscope_url(raw_url, use_native), expected)

        ssh_cases = [
            ([], ("", False)),
            (["bash", "-l"], ("", False)),
            (["ssh", "--", "ubuntu@DevBox.Internal."], ("devbox.internal", True)),
            (["ssh", "-o", "BatchMode=yes", "devbox"], ("", False)),
            (["ssh", "-oBatchMode=yes", "devbox"], ("", False)),
            (["ssh", "-fNT", "-L", "8080:localhost:80", "devbox"], ("", False)),
            (["ssh", "-W", "10.0.0.2:22", "bastion"], ("", False)),
            (["ssh", "-vvp", "2222", "-p2222", "ssh://xhx@GPU-01.Corp:2222/"], ("gpu-01.corp", True)),
            (["ssh", "devbox", "python3", "-"], ("", False)),
            (["ssh", "-v"], ("", True)),
        ]
        for argv, expected_res in ssh_cases:
            self.assertEqual(extract_ssh_target_from_argv(argv), expected_res, argv)

    def test_remote_probe_inprocess_registry_proc_git_and_compaction(self) -> None:
        self.assertEqual(_remote_probe.run_probe("invalid"), {"ok": False, "reason": "bad_request"})  # type: ignore[arg-type]
        pong = _remote_probe.run_probe({"ping_only": True})
        self.assertTrue(pong["ok"] and pong["pong"])

        saved_env_snapshot = dict(os.environ)

        def _restore_env() -> None:
            os.environ.clear()
            os.environ.update(saved_env_snapshot)

        self.addCleanup(_restore_env)
        for k in list(os.environ):
            if k.startswith("GIT_"):
                os.environ.pop(k, None)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rhome = root / "home"
            rproc = root / "proc"
            rrt = root / "rt"
            git_repo = rhome / "repos" / "core-svc"
            for d in (rhome, rproc, rrt, git_repo / "src"):
                d.mkdir(parents=True, exist_ok=True)
            os.chmod(rrt, 0o700)
            os.environ["HOME"] = str(rhome)
            os.environ["XDG_CONFIG_HOME"] = str(rhome / ".config")
            os.environ["GIT_CONFIG_NOSYSTEM"] = "1"

            # Initialize a real tiny local git repo with hermetic environment (isolated from GIT_* & hooks)
            has_git = shutil.which("git") is not None
            if has_git:
                git_clean_env = dict(os.environ)
                subprocess.run(
                    ["git", "-c", "init.defaultBranch=main", "init", "-q"],
                    cwd=str(git_repo),
                    env=git_clean_env,
                    check=True,
                )
                (git_repo / "src" / "engine_core.py").write_text("VALUE = 1\n", encoding="utf-8")
                subprocess.run(
                    ["git", "add", "src/engine_core.py"],
                    cwd=str(git_repo),
                    env=git_clean_env,
                    check=True,
                )
                subprocess.run(
                    [
                        "git",
                        "-c",
                        "user.name=Test",
                        "-c",
                        "user.email=test@example.com",
                        "-c",
                        "commit.gpgsign=false",
                        "commit",
                        "-q",
                        "-m",
                        "init",
                    ],
                    cwd=str(git_repo),
                    env=git_clean_env,
                    check=True,
                )

            env = {
                "HOME": str(rhome),
                "AVI_REMOTE_PROC_ROOT": str(rproc),
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rrt),
                "AVI_DISABLE_TMUX_PROBE": "1",
            }

            # 1. Pure workspace-only fallback (`remote_cwd_only`) + clean worktree `repo_files`
            r_ws = _remote_probe.run_probe({"v": 1, "cwd_hint": "~/repos/core-svc"}, env=env, home_dir=rhome, proc_root=rproc)
            self.assertTrue(r_ws["ok"])
            self.assertEqual(r_ws["matched_by"], "remote_cwd_only")
            if has_git:
                self.assertIn("engine_core.py", r_ws["repo_files"])
                # Dirty file exercises `_read_remote_git_status` hotword extraction
                (git_repo / "src" / "dirty_worker.py").write_text("X = 2\n", encoding="utf-8")
                r_dirty = _remote_probe.run_probe(
                    {"v": 1, "cwd_hint": "~/repos/core-svc"}, env=env, home_dir=rhome, proc_root=rproc
                )
                self.assertIn("dirty_worker.py", r_dirty["git_hotwords"])

            # 2. Codex compacted session + live `/proc` fd discovery + unscoped disambiguation
            cx_sid = "0196bbbb-1111-7000-8000-000000000099"
            cx_dir = rhome / ".codex" / "sessions" / "2026" / "11" / "19"
            cx_dir.mkdir(parents=True, exist_ok=True)
            cx_jf = cx_dir / f"rollout-2026-11-19T10-00-00-{cx_sid}.jsonl"
            cx_records = [
                {"type": "session_meta", "payload": {"id": cx_sid, "source": "cli", "cwd": str(git_repo)}},
                {
                    "type": "compacted",
                    "payload": {
                        "message": "Compacted codex summary",
                        "retained_context": {
                            "user_messages": [{"order": 1, "text": "retained user msg"}],
                            "assistant_messages": [{"order": 2, "text": "retained assistant msg"}],
                        },
                        "replacement_history": [
                            {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": "post-compact user"}],
                            }
                        ],
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "item": {"type": "AgentMessage", "text": "final agent reply"},
                    },
                },
            ]
            cx_jf.write_text("\n".join(json.dumps(r) for r in cx_records) + "\n", encoding="utf-8")

            pdir = rproc / "401"
            (pdir / "fd").mkdir(parents=True)
            (pdir / "stat").write_text("401 (codex) S 1 401 401 0 0 0 0 0 0 0 0 0 0 0 20 0 1 0 100\n", encoding="utf-8")
            (pdir / "comm").write_text("codex\n", encoding="utf-8")
            (pdir / "cmdline").write_bytes(b"codex\x00")
            os.symlink(str(git_repo), pdir / "cwd")
            os.symlink(str(cx_jf), pdir / "fd" / "5")

            write_session_registry_entry(
                rrt,
                cli="codex",
                session_id=cx_sid,
                jsonl_path=str(cx_jf),
                cwd=str(git_repo),
                agent_pid=401,
            )

            # Registry Tier 1 lookup & unscoped Tier 2b live `/proc` lookup
            r_reg = _remote_probe.run_probe(
                {"v": 1, "token": cx_sid[:8], "cli": "codex"}, env=env, home_dir=rhome, proc_root=rproc
            )
            self.assertTrue(r_reg["ok"])
            self.assertEqual(r_reg["matched_by"], "remote_registry")
            self.assertEqual(r_reg["compaction_count"], 1)
            self.assertEqual(r_reg["turns"][-1], ["assistant", "final agent reply"])

            r_unscoped = _remote_probe.run_probe(
                {"v": 1, "title": "codex · core-svc"}, env=env, home_dir=rhome, proc_root=rproc
            )
            self.assertTrue(r_unscoped["ok"])
            self.assertEqual(r_unscoped["session_id"], cx_sid)

            # 3. AGY `CHECKPOINT` compaction & `transcript.jsonl` -> `transcript_full.jsonl` upgrade
            agy_cid = "dddd5555-6666-4777-8888-999900001111"
            agy_logs = rhome / ".gemini" / "antigravity-cli" / "brain" / agy_cid / ".system_generated" / "logs"
            agy_logs.mkdir(parents=True, exist_ok=True)
            agy_compact = agy_logs / "transcript.jsonl"
            agy_full = agy_logs / "transcript_full.jsonl"
            agy_compact.write_text("{}\n", encoding="utf-8")
            agy_full.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "source": "USER_EXPLICIT",
                                "type": "USER_INPUT",
                                "content": f"<user_information>\nCommand Working Directory: {git_repo}\n</user_information>\n<USER_REQUEST>old turn</USER_REQUEST>",
                            }
                        ),
                        json.dumps({"source": "SYSTEM", "type": "CHECKPOINT", "content": "AGY checkpoint summary"}),
                        json.dumps(
                            {
                                "source": "USER_EXPLICIT",
                                "type": "USER_INPUT",
                                "content": "<USER_REQUEST>after checkpoint</USER_REQUEST>",
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            write_session_registry_entry(
                rrt,
                cli="agy",
                session_id=agy_cid,
                jsonl_path=str(agy_compact),
                cwd=str(git_repo),
            )
            r_agy = _remote_probe.run_probe(
                {"v": 1, "token": agy_cid[:8], "cli": "gemini"},
                env=env,
                home_dir=rhome,
                proc_root=rproc,
            )
            self.assertTrue(r_agy["ok"])
            self.assertEqual(r_agy["compaction_count"], 1)
            self.assertEqual(
                r_agy["turns"],
                [["assistant", "AGY checkpoint summary"], ["user", "after checkpoint"]],
            )

            # 4. Direct storage & /proc fallback without registry + Codex thread_name disambiguation + flock
            import fcntl

            for reg_f in (rrt / "sessions").glob("*.json"):
                reg_f.unlink()

            # Step 2 (/proc hit by token) & Step 3 (storage hit by token for codex, claude, and locked agy)
            r_proc_tok = _remote_probe.run_probe(
                {"v": 1, "token": cx_sid[:8]}, env=env, home_dir=rhome, proc_root=rproc
            )
            self.assertTrue(r_proc_tok["ok"])
            self.assertEqual(r_proc_tok["matched_by"], "remote_title_token")

            # Remove /proc/401 fd so Codex falls through to Step 3 direct storage lookup
            (pdir / "fd" / "5").unlink()
            (rhome / ".codex" / "session_index.jsonl").write_text(
                json.dumps({"id": cx_sid, "thread_name": "RefactorEngineCore"}) + "\n",
                encoding="utf-8",
            )
            r_stor_cx = _remote_probe.run_probe(
                {"v": 1, "token": cx_sid[:8], "cli": "codex"},
                env=env,
                home_dir=rhome,
                proc_root=rproc,
            )
            self.assertTrue(r_stor_cx["ok"])
            self.assertEqual(r_stor_cx["session_id"], cx_sid)

            # AGY presence lock held via real flock in open file descriptor -> Step 3 storage hit
            pres_dir = rhome / ".gemini" / "antigravity-cli" / "presence"
            pres_dir.mkdir(parents=True, exist_ok=True)
            agy_lock_file = pres_dir / f"{agy_cid}.lock"
            with open(agy_lock_file, "w", encoding="utf-8") as lf_handle:
                fcntl.flock(lf_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                r_stor_agy = _remote_probe.run_probe(
                    {"v": 1, "token": agy_cid[:8], "cli": "agy"},
                    env=env,
                    home_dir=rhome,
                    proc_root=rproc,
                )
                self.assertTrue(r_stor_agy["ok"])
                self.assertEqual(r_stor_agy["session_id"], agy_cid)

            # Second live Claude process in a different repo to test _collect_descendants, --session-id,
            # Headless CLI rejection (-p / exec), and _disambiguate_unscoped_cands (ambiguous vs title score)
            other_repo = rhome / "repos" / "billing-api"
            other_repo.mkdir(parents=True, exist_ok=True)
            cl_sid2 = "eeee6666-7777-4888-8999-000011112222"
            enc_other = _remote_probe._encode_claude_dirs(str(other_repo))[0]
            cl_dir2 = rhome / ".claude" / "projects" / enc_other
            cl_dir2.mkdir(parents=True, exist_ok=True)
            (cl_dir2 / f"{cl_sid2}.jsonl").write_text(
                json.dumps(
                    {
                        "sessionId": cl_sid2,
                        "cwd": str(other_repo),
                        "isSidechain": False,
                        "type": "user",
                        "message": {"role": "user", "content": "check billing invoice"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            # Re-link codex fd on 401 in git_repo, add shell 500 -> child claude 501 (--session-id) in other_repo,
            # plus headless `claude -p` 502 and `codex exec` 503 (both must be ignored!)
            os.symlink(str(cx_jf), pdir / "fd" / "5")
            for pid_n, ppid_n, comm_n, cwd_n, argv_n in (
                (500, 1, "zsh", str(other_repo), ["zsh"]),
                (501, 500, "claude", str(other_repo), ["claude", f"--session-id={cl_sid2}"]),
                (502, 1, "claude", str(other_repo), ["claude", "-p", "headless prompt"]),
                (503, 1, "codex", str(git_repo), ["codex", "exec", "headless task"]),
            ):
                pd = rproc / str(pid_n)
                (pd / "fd").mkdir(parents=True, exist_ok=True)
                (pd / "stat").write_text(
                    f"{pid_n} ({comm_n}) S {ppid_n} {pid_n} {pid_n} 0 0 0 0 0 0 0 0 0 0 0 20 0 1 0 100\n",
                    encoding="utf-8",
                )
                (pd / "comm").write_text(f"{comm_n}\n", encoding="utf-8")
                (pd / "cmdline").write_bytes(b"\x00".join(x.encode("utf-8") for x in argv_n) + b"\x00")
                os.symlink(cwd_n, pd / "cwd")

            self.assertEqual(_remote_probe._collect_descendants(rproc, 500), {500, 501})

            # Unscoped generic title across two distinct repos -> ambiguous!
            r_amb = _remote_probe.run_probe({"v": 1, "title": "zsh"}, env=env, home_dir=rhome, proc_root=rproc)
            self.assertFalse(r_amb["ok"])
            self.assertEqual(r_amb["reason"], "ambiguous")

            # Title mentioning Codex thread_name or repo basename disambiguates cleanly!
            r_by_tname = _remote_probe.run_probe(
                {"v": 1, "title": "RefactorEngineCore"}, env=env, home_dir=rhome, proc_root=rproc
            )
            self.assertTrue(r_by_tname["ok"])
            self.assertEqual(r_by_tname["session_id"], cx_sid)

            r_by_base = _remote_probe.run_probe(
                {"v": 1, "title": "working on billing-api"}, env=env, home_dir=rhome, proc_root=rproc
            )
            self.assertTrue(r_by_base["ok"])
            self.assertEqual(r_by_base["session_id"], cl_sid2)

    def test_failed_audio_stash_and_selector_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            with self.assertRaises(FileNotFoundError):
                resolve_failed_audio(rt)

            wav = pack_pcm16_mono_wav(struct.pack("<h", 4000) * 800, sample_rate=16000)
            p1 = save_failed_audio(wav, rt, label="asr_timeout", duration_sec=2.0, now_ts=1700000100.0)
            p2 = save_failed_audio(wav, rt, label="asr_timeout", duration_sec=2.0, now_ts=1700000100.0)
            p3 = save_failed_audio(wav, rt, label="cancelled", duration_sec=5.0, now_ts=1700000200.0)
            assert p1 is not None and p2 is not None and p3 is not None
            self.assertNotEqual(p1, p2)  # collision suffix _2.wav

            self.assertEqual(resolve_failed_audio(rt), p3)
            self.assertEqual(resolve_failed_audio(rt, "1"), p3)
            self.assertEqual(resolve_failed_audio(rt, str(p1)), p1)
            self.assertEqual(resolve_failed_audio(rt, p2.stem), p2)
            self.assertEqual(resolve_failed_audio(rt, "cancelled"), p3)
            with self.assertRaisesRegex(FileNotFoundError, "out of range"):
                resolve_failed_audio(rt, "99")
            with self.assertRaisesRegex(FileNotFoundError, "ambiguous"):
                resolve_failed_audio(rt, "asr_timeout")
            with self.assertRaisesRegex(FileNotFoundError, "no failed audio matching"):
                resolve_failed_audio(rt, "nonexistent_tag")


if __name__ == "__main__":
    unittest.main()
