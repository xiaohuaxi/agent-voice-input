"""Unit tests for Stage 2 refiners, host shim invariants, packaging, and VoiceInputDaemon."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _REPO_ROOT / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from agent_voice_input.asr_providers import AsrContext, AsrResult, BaseAsrProvider, HttpRequest, HttpResponse  # noqa: E402
from agent_voice_input.audio_proc import pack_pcm16_mono_wav  # noqa: E402
from agent_voice_input.config import Config, ConfigError, HotkeyConfig, RefinerConfig, _parse_simple_toml, load_config, load_env_files, parse_toml, resolve_credentials  # noqa: E402
from agent_voice_input.daemon import (  # noqa: E402
    MAX_ASR_CHUNK_CONCURRENCY,
    VoiceInputDaemon,
    _transcribe_wav_chunks,
    run_pipeline_once,
    send_control_command,
)
from agent_voice_input.platform_wsl import ForegroundWindow, PasteOutcome, SHIM_SCRIPT, WslPlatform, is_windows_terminal_process  # noqa: E402
from agent_voice_input.refiner import (  # noqa: E402
    CodexRefiner,
    GeminiRefiner,
    OpenAICompatibleRefiner,
    RefinerResult,
    create_refiner,
    effective_refiner_timeout_sec,
)
from agent_voice_input.session_ctx import SessionTracker, write_session_registry_entry  # noqa: E402

_MOCK_APP_SERVER_CODE = r"""
import json, os, sys
log_path = sys.argv[1]
mode = sys.argv[2] if len(sys.argv) > 2 else "ok"
threads = 0
turns = 0
def log(rec):
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()
for raw in sys.stdin:
    msg = json.loads(raw)
    log(msg)
    m = msg.get("method")
    rid = msg.get("id")
    if m == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {"userAgent": "mock"}})
    elif m == "thread/start":
        if mode == "fail_app":
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "boom secret=MOCK_SECRET_9999"}})
            continue
        threads += 1
        send({"jsonrpc": "2.0", "id": rid, "result": {"thread": {"id": f"thr-{threads}"}}})
    elif m == "turn/start":
        turns += 1
        tid = msg["params"]["threadId"]
        txt = msg["params"]["input"][0]["text"]
        send({"jsonrpc": "2.0", "id": rid, "result": {"turn": {"id": f"t-{turns}", "status": "inProgress", "items": []}}})
        draft = txt.split("<asr_draft_to_clean>\n", 1)[1].split("\n</asr_draft_to_clean>", 1)[0]
        cleaned = f"CLEAN[{tid}#{turns}]:{draft}"
        send({"jsonrpc": "2.0", "method": "item/completed", "params": {"threadId": tid, "item": {"type": "agentMessage", "text": cleaned}}})
        send({"jsonrpc": "2.0", "method": "turn/completed", "params": {"threadId": tid, "turn": {"id": f"t-{turns}", "status": "completed", "items": []}}})
"""


def _voiced_wav() -> bytes:
    return pack_pcm16_mono_wav([3000, -3000] * 4000)


def _zero_wav(seconds: float = 1.0) -> bytes:
    return pack_pcm16_mono_wav([0] * int(16000 * seconds))


class _FakeAsr(BaseAsrProvider):
    provider_name = "dashscope"

    def __init__(self, text: str = "把 refiner 点 py 改一下", gate_evt: threading.Event | None = None) -> None:
        self.text = text
        self.gate_evt = gate_evt
        self.calls = 0

    def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None) -> AsrResult:
        self.calls += 1
        if self.gate_evt is not None:
            self.gate_evt.wait(timeout=3.0)
        return AsrResult(text=self.text, provider="dashscope", model="qwen3-asr-flash", latency_ms=12)


class _FakePlatform:
    def __init__(self, wav_bytes: bytes = b"", source: str = "pulse", paste_outcome: PasteOutcome | None = None) -> None:
        self.wav_bytes = wav_bytes or _voiced_wav()
        self.source = source
        self.paste_outcome = paste_outcome or PasteOutcome(True, True, "ok", "WindowsTerminal")
        self.fg = ForegroundWindow(hwnd=101, process="WindowsTerminal", title="codex [#abcd1234]")
        self.beeps: list[str] = []
        self.huds: list[str] = []
        self.deliveries: list[tuple[str, int, int]] = []
        self.canceled = 0
        self.switched = 0
        self.listener = None

    def start_event_listener(self, cb):
        self.listener = cb

    def get_foreground_window(self) -> ForegroundWindow:
        return self.fg

    def beep(self, kind: str) -> None:
        self.beeps.append(kind)

    def hud(self, state: str, text: str = "", duration_ms: int = 0) -> None:
        self.huds.append(state)

    def start_recording(self, runtime_dir: Path) -> None:
        return None

    def stop_recording(self) -> tuple[bytes, str]:
        return self.wav_bytes, self.source

    def cancel_recording(self) -> None:
        self.canceled += 1

    def switch_to_fallback_audio(self) -> bool:
        self.switched += 1
        return True

    def deliver_text(self, text: str, seq_id: int, target_hwnd: int = 0) -> PasteOutcome:
        self.deliveries.append((text, seq_id, target_hwnd))
        return self.paste_outcome

    def close(self) -> None:
        return None


class ExamplesAndShimInvariantsTests(unittest.TestCase):
    def test_example_configs_parse_cleanly_without_secrets_or_typos(self) -> None:
        toml_path = _REPO_ROOT / "config.example.toml"
        env_path = _REPO_ROOT / "config.example.env"
        toml_raw = toml_path.read_text(encoding="utf-8")
        parsed = parse_toml(toml_raw)
        parsed_simple = _parse_simple_toml(toml_raw)
        self.assertEqual(parsed, parsed_simple)
        self.assertEqual(parsed["hotkey"]["key"], "RControl")
        self.assertIs(parsed["hotkey"]["only_in_terminals"], True)
        self.assertEqual(parsed["ui"]["language"], "auto")
        self.assertEqual(parsed["ui"]["position"], "bottom")
        self.assertEqual(parsed["audio"]["macos_device"], "default")
        self.assertEqual(parsed["asr"]["provider"], "auto")
        self.assertEqual(parsed["asr"]["api"], "auto")
        self.assertEqual(parsed["asr"]["effort"], "auto")
        self.assertEqual(parsed["refiner"]["provider"], "codex")
        cfg, _ = load_config(config_path=toml_path, env={"HOME": "/tmp/nonexistent-home"})
        self.assertIs(cfg.hotkey.only_in_terminals, True)
        self.assertEqual(cfg.ui.language, "auto")
        self.assertEqual(cfg.ui.position, "bottom")
        self.assertEqual(cfg.audio.macos_device, "default")
        self.assertEqual(cfg.refiner.provider, "codex")
        self.assertEqual(cfg.refiner.model, "gpt-6-luna")
        self.assertIs(cfg.audio.beep, False)

        env_raw = env_path.read_text(encoding="utf-8")
        self.assertNotIn("API_ID", env_raw)
        loaded = load_env_files({}, env_paths=[env_path])
        for expected_key in (
            "DASHSCOPE_API_KEY",
            "DOUBAO_ASR_APP_ID",
            "DOUBAO_ASR_API_KEY",
            "ARK_API_KEY",
            "OPENROUTER_API_KEY",
            "GEMINI_API_KEY",
            "OPENAI_API_KEY",
            "GROQ_API_KEY",
            "SILICONFLOW_API_KEY",
        ):
            self.assertIn(expected_key, loaded)

    def test_pyproject_and_package_data_integrity(self) -> None:
        pyproject_path = _REPO_ROOT / "pyproject.toml"
        try:
            import tomllib  # type: ignore[import-not-found]
            data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        except ImportError:
            data = parse_toml(pyproject_path.read_text(encoding="utf-8"))

        proj = data["project"]
        self.assertEqual(proj["name"], "agent-voice-input")
        self.assertEqual(proj["version"], "0.2.1")
        self.assertEqual(proj["requires-python"], ">=3.10")
        self.assertEqual(proj["license"], "Apache-2.0")
        self.assertEqual(
            proj["scripts"]["agent-voice-input"],
            "agent_voice_input.cli:main",
        )
        pkg_data = data["tool"]["setuptools"]["package-data"]["agent_voice_input"]
        self.assertIn("win_shim.ps1", pkg_data)
        self.assertIn("mac_shim.swift", pkg_data)
        manifest_raw = (_REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        self.assertIn("win_shim.ps1", manifest_raw)
        self.assertIn("mac_shim.swift", manifest_raw)
        self.assertTrue(SHIM_SCRIPT.is_file())
        self.assertEqual(
            SHIM_SCRIPT.resolve(),
            (_REPO_ROOT / "src" / "agent_voice_input" / "win_shim.ps1").resolve(),
        )
        self.assertTrue((_REPO_ROOT / "LICENSE").is_file())
        self.assertTrue((_REPO_ROOT / "bin" / "agent-voice-input").is_file())

    def test_codex_command_isolation_effort_mapping_and_no_fast_mode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            fake_codex = tmp / "codex"
            fake_codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_codex.chmod(0o755)
            ref = CodexRefiner(Config(), env={"CODEX_BIN": str(fake_codex)}, runtime_dir=tmp)
            cmd = ref._build_cmd(tmp)
            joined = " ".join(cmd)
            self.assertIn("mcp_servers={}", cmd)
            self.assertIn("project_doc_max_bytes=0", cmd)
            self.assertIn("shell_tool", cmd)
            self.assertIn("multi_agent", cmd)
            self.assertNotIn("fast", joined.lower())
            self.assertNotIn("service_tier", joined.lower())

            # Verify JSON-RPC thread/start & turn/start effort mapping across auto / explicit / max / default
            for eff, expected_rpc_eff in (
                ("low", "low"),
                ("auto", "low"),
                ("none", "none"),
                ("max", "xhigh"),
                ("default", None),
            ):
                rpc_log = tmp / f"rpc_{eff}.jsonl"
                cfg_eff = Config(refiner=RefinerConfig(provider="codex", effort=eff, timeout_sec=3.0))
                r_eff = CodexRefiner(
                    cfg_eff,
                    env={},
                    runtime_dir=tmp,
                    app_server_cmd=[sys.executable, "-u", "-c", _MOCK_APP_SERVER_CODE, str(rpc_log), "ok"],
                )
                try:
                    out = r_eff.refine("测试档位")
                    self.assertEqual(out.text, "CLEAN[thr-1#1]:测试档位")
                finally:
                    r_eff.close()
                recs = [json.loads(ln) for ln in rpc_log.read_text(encoding="utf-8").splitlines() if ln.strip()]
                t_start = next(r for r in recs if r.get("method") == "thread/start")
                turn_start = next(r for r in recs if r.get("method") == "turn/start")
                raw_rpc = rpc_log.read_text(encoding="utf-8").lower()
                self.assertNotIn("service_tier", raw_rpc)
                if expected_rpc_eff is None:
                    self.assertNotIn("config", t_start["params"])
                    self.assertNotIn("effort", turn_start["params"])
                else:
                    self.assertEqual(t_start["params"]["config"], {"model_reasoning_effort": expected_rpc_eff})
                    self.assertEqual(turn_start["params"]["effort"], expected_rpc_eff)

    def test_win_shim_static_safety_invariants(self) -> None:
        shim = (_REPO_ROOT / "src" / "agent_voice_input" / "win_shim.ps1").read_text(encoding="utf-8")
        self.assertIn("LLKHF_INJECTED", shim)
        self.assertIn("_chordTainted", shim)
        self.assertIn("ptt_cancel", shim)
        self.assertIn("CallNextHookEx", shim)
        self.assertIn("SendCtrlV", shim)
        self.assertIn("focus_changed", shim)
        self.assertIn("SoundPlayer", shim)
        self.assertIn("ShowWithoutActivation", shim)
        self.assertIn("0x08000000", shim)  # WS_EX_NOACTIVATE
        self.assertIn("IsQuitTimer", shim)  # WinForms Timer WM_TIMER must never exit bridgeLoop
        self.assertIn("[int]$Beep = 0", shim)
        self.assertIn("IAudioClient", shim)
        self.assertIn("IAudioCaptureClient", shim)
        self.assertIn("0x88000000u", shim)
        self.assertIn("wasapi_zero_packets", shim)
        # Two-stage HUD copy & colors + backward-compatible 'processing' alias
        self.assertIn('st == "recognizing" || st == "processing"', shim)
        self.assertIn(r"\u25D0 \u6B63\u5728\u8BED\u97F3\u8BC6\u522B\u2026", shim)
        self.assertIn("Color.FromArgb(56, 189, 248)", shim)
        self.assertIn('st == "refining"', shim)
        self.assertIn(r"\u25C8 \u6B63\u5728\u6574\u7406\u7CBE\u4FEE\u2026", shim)
        self.assertIn("Color.FromArgb(208, 156, 255)", shim)
        self.assertIn("Color.FromArgb(251, 191, 36)", shim)
        self.assertIn("Color.FromArgb(64, 18, 26)", shim)
        self.assertIn("Color.FromArgb(72, 38, 12)", shim)
        self.assertIn("Color.FromArgb(242, 255, 255, 255)", shim)
        self.assertNotIn("save vrec", shim)
        self.assertNotIn("[Console]::Beep", shim)
        self.assertNotIn("QueueUserWorkItem", shim)
        self.assertNotIn("GetText", shim)  # never saves/restores old clipboard


class RefinerProtocolAndFallbackTests(unittest.TestCase):
    def test_codex_luna_incremental_delta_rotation_and_generation_reset(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            rpc_log = tmp / "rpc.jsonl"
            sid = "abcd1234-0000-0000-0000-000000000001"
            sdir = tmp / ".codex" / "sessions" / "2026" / "11" / "13"
            sdir.mkdir(parents=True)
            rollout = sdir / f"rollout-2026-{sid}.jsonl"
            meta_ln = json.dumps({"type": "session_meta", "payload": {"id": sid, "source": "cli", "cwd": str(tmp)}}) + "\n"
            rollout.write_text(
                meta_ln
                + json.dumps({"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "UserMessage", "content": [{"type": "text", "text": "请检查 refiner.py"}]}}})
                + "\n",
                encoding="utf-8",
            )
            write_session_registry_entry(tmp, cli="codex", session_id=sid, jsonl_path=str(rollout), cwd=str(tmp))
            tracker = SessionTracker(home_dir=tmp, env={}, runtime_dir=tmp, proc_root=tmp / "empty_proc")
            snap = tracker.sync_session(window_title="codex [#abcd1234]")
            self.assertIsNotNone(snap)
            assert snap is not None

            cfg = Config(refiner=RefinerConfig(provider="codex", model="gpt-6-luna", effort="low", max_turns_per_thread=2, timeout_sec=5.0))
            ref = CodexRefiner(
                cfg,
                env={},
                runtime_dir=tmp,
                app_server_cmd=[sys.executable, "-u", "-c", _MOCK_APP_SERVER_CODE, str(rpc_log), "ok"],
            )
            try:
                # Turn 1: starts thr-1 and sends initial turn delta
                r1 = ref.refine("把 refiner 点 py 修一下", session_key=snap.session_key, tracker=tracker)
                self.assertEqual(r1.text, "CLEAN[thr-1#1]:把 refiner 点 py 修一下")
                self.assertEqual(r1.used_fallback, "")

                # Turn 2 with no new session log lines: reuses thr-1 and sends empty <context_delta>
                r2 = ref.refine("再跑单测", session_key=snap.session_key, tracker=tracker)
                self.assertEqual(r2.text, "CLEAN[thr-1#2]:再跑单测")

                # Append a new message; Turn 3 hits max_turns_per_thread=2 -> rotates to thr-2 with full context
                with rollout.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "AgentMessage", "content": [{"type": "text", "text": "已更新 daemon.py"}]}}}) + "\n")
                tracker.sync_session(window_title="codex [#abcd1234]")
                r3 = ref.refine("继续提交", session_key=snap.session_key, tracker=tracker)
                self.assertEqual(r3.text, "CLEAN[thr-2#3]:继续提交")

                # Truncate/rewrite rollout -> generation increments -> rotates to thr-3 even before max_turns
                rollout.write_text(
                    meta_ln
                    + json.dumps({"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "UserMessage", "content": [{"type": "text", "text": "全新上下文"}]}}})
                    + "\n",
                    encoding="utf-8",
                )
                tracker.sync_session(window_title="codex [#abcd1234]")
                r4 = ref.refine("第四句", session_key=snap.session_key, tracker=tracker)
                self.assertEqual(r4.text, "CLEAN[thr-3#4]:第四句")
            finally:
                ref.close()

            records = [json.loads(ln) for ln in rpc_log.read_text(encoding="utf-8").splitlines() if ln.strip()]
            methods = [r.get("method") for r in records]
            self.assertEqual(methods[:2], ["initialize", "initialized"])
            t_starts = [r for r in records if r.get("method") == "thread/start"]
            self.assertEqual(len(t_starts), 3)
            self.assertTrue(all(t["params"]["ephemeral"] is True for t in t_starts))
            turn_inputs = [r["params"]["input"][0]["text"] for r in records if r.get("method") == "turn/start"]
            self.assertIn("refiner.py", turn_inputs[0])
            self.assertIn("<context_delta>\n\n</context_delta>", turn_inputs[1])
            self.assertIn("daemon.py", turn_inputs[2])
            self.assertIn("全新上下文", turn_inputs[3])

    def test_codex_fallbacks_to_exec_then_asr_draft_and_redacts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            rpc_log = tmp / "rpc_fail.jsonl"
            env = {"DASHSCOPE_API_KEY": "MOCK_SECRET_9999"}
            cfg = Config(refiner=RefinerConfig(provider="codex", timeout_sec=3.0))

            # Fallback 1: app-server fails -> codex exec succeeds
            ref_exec = CodexRefiner(
                cfg,
                env=env,
                runtime_dir=tmp,
                app_server_cmd=[sys.executable, "-u", "-c", _MOCK_APP_SERVER_CODE, str(rpc_log), "fail_app"],
                exec_runner=lambda envlp, cwd: "EXEC_REFINED_TEXT",
            )
            res1 = ref_exec.refine("原始语音草稿")
            self.assertEqual(res1.text, "EXEC_REFINED_TEXT")
            self.assertEqual(res1.provider, "codex")
            self.assertEqual(res1.used_fallback, "codex-exec")
            self.assertNotIn("MOCK_SECRET_9999", res1.warning)
            self.assertIn("[REDACTED]", res1.warning)
            ref_exec.close()

            # Fallback 2: both app-server and codex exec fail -> returns original draft without losing text
            def broken_exec(_envlp: str, _cwd: Path) -> str:
                raise RuntimeError("exec failed key=MOCK_SECRET_9999")

            ref_draft = CodexRefiner(
                cfg,
                env=env,
                runtime_dir=tmp,
                app_server_cmd=[sys.executable, "-u", "-c", _MOCK_APP_SERVER_CODE, str(rpc_log), "fail_app"],
                exec_runner=broken_exec,
            )
            res2 = ref_draft.refine("绝不丢失的用户原话")
            self.assertEqual(res2.text, "绝不丢失的用户原话")
            self.assertEqual(res2.used_fallback, "asr_draft")
            self.assertNotIn("MOCK_SECRET_9999", res2.warning)
            ref_draft.close()

            # Single-hop ASR result bypasses Stage 2 automatically
            single = AsrResult(text="Gemini单跳已精修", provider="gemini", model="gemini-2.5-flash", latency_ms=50, is_single_hop_refined=True)
            res3 = ref_draft.refine(single)
            self.assertEqual(res3.text, "Gemini单跳已精修")
            self.assertEqual(res3.used_fallback, "single_hop_passthrough")

    def test_built_in_and_openai_compatible_refiners_effort_and_error_redaction(self) -> None:
        seen: list[HttpRequest] = []
        fake_val = "dummy-volc-" + ("0" * 12)
        gem_fake = "dummy-gem-" + ("9" * 12)
        env = {
            "HUOSHAN_API_KEY": fake_val,
            "OPENROUTER_API_KEY": "sk-or-v1-" + ("1" * 16),
            "DASHSCOPE_API_KEY": "dummy-dash-" + ("2" * 12),
            "GROQ_API_KEY": "gsk_" + ("3" * 16),
            "SILICONFLOW_API_KEY": "sk-sf-" + ("4" * 14),
            "OPENAI_API_KEY": "sk-oai-" + ("5" * 14),
            "GEMINI_API_KEY": gem_fake,
        }

        def ok_transport(req: HttpRequest) -> HttpResponse:
            seen.append(req)
            return HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps({"choices": [{"message": {"content": "整理后的云端输出"}}]}).encode("utf-8"),
            )

        # 1. Volcengine Ark built-in provider resolves default URL, default model & HUOSHAN_API_KEY/ARK_API_KEY
        cfg_volc = Config(refiner=RefinerConfig(provider="volcengine"))
        creds = resolve_credentials(cfg_volc, env)
        ref = create_refiner(cfg_volc, creds, env=env, transport=ok_transport)
        self.assertIsInstance(ref, OpenAICompatibleRefiner)
        out = ref.refine("火山初稿", fallback_context="user: hi")
        self.assertEqual(out.text, "整理后的火山输出" if False else "整理后的云端输出")
        self.assertEqual(out.provider, "volcengine")
        self.assertEqual(out.model, "doubao-seed-1-6-flash")
        self.assertEqual(seen[0].url, "https://ark.cn-beijing.volces.com/api/v3/chat/completions")
        self.assertEqual(seen[0].headers.get("Authorization"), f"Bearer {fake_val}")

        def err_transport(_req: HttpRequest) -> HttpResponse:
            return HttpResponse(500, {}, f"upstream leaked {fake_val}".encode("utf-8"))

        ref_err = OpenAICompatibleRefiner(cfg_volc, creds, env=env, transport=err_transport)
        fb = ref_err.refine("保留原稿")
        self.assertEqual(fb.text, "保留原稿")
        self.assertEqual(fb.used_fallback, "asr_draft")
        self.assertNotIn(fake_val, fb.warning)

        # 2. OpenRouter, DashScope, Groq, SiliconFlow, OpenAI, and keyless loopback openai-compatible
        matrix = [
            ("openrouter", "", "", "low", "https://openrouter.ai/api/v1/chat/completions", "google/gemini-2.5-flash-lite", {"reasoning": {"effort": "low"}}),
            ("openrouter", "", "", "auto", "https://openrouter.ai/api/v1/chat/completions", "google/gemini-2.5-flash-lite", {"reasoning": {"effort": "none"}}),
            ("openrouter", "", "", "default", "https://openrouter.ai/api/v1/chat/completions", "google/gemini-2.5-flash-lite", {}),
            ("dashscope", "", "", "low", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions", "qwen-turbo-latest", {}),
            ("groq", "", "", "low", "https://api.groq.com/openai/v1/chat/completions", "llama-3.3-70b-versatile", {}),
            ("siliconflow", "", "", "low", "https://api.siliconflow.cn/v1/chat/completions", "Qwen/Qwen2.5-7B-Instruct", {}),
            # gpt-4o-mini omits reasoning_effort so default [refiner] provider="openai" never 400s
            ("openai", "", "", "low", "https://api.openai.com/v1/chat/completions", "gpt-4o-mini", {}),
            # OpenAI reasoning models omit incompatible custom temperature (both with reasoning_effort and effort="default"/"auto")
            ("openai", "o4-mini", "", "medium", "https://api.openai.com/v1/chat/completions", "o4-mini", {"reasoning_effort": "medium"}),
            ("openai", "gpt-5-mini", "", "default", "https://api.openai.com/v1/chat/completions", "gpt-5-mini", {}),
            ("openai", "gpt-6-luna", "", "auto", "https://api.openai.com/v1/chat/completions", "gpt-6-luna", {}),
            ("openrouter", "openai/o3-mini", "", "low", "https://openrouter.ai/api/v1/chat/completions", "openai/o3-mini", {"reasoning": {"effort": "low"}}),
            ("openai-compatible", "local-qwen", "http://127.0.0.1:8000/v1", "low", "http://127.0.0.1:8000/v1/chat/completions", "local-qwen", {}),
        ]
        no_temp_models = {"o4-mini", "gpt-5-mini", "gpt-6-luna", "openai/o3-mini"}
        for prov, m_override, b_url, eff, exp_url, exp_model, exp_extra in matrix:
            with self.subTest(prov=prov, model=m_override, effort=eff):
                seen.clear()
                sub_env = {} if prov == "openai-compatible" else env
                c = Config(refiner=RefinerConfig(provider=prov, model=m_override, base_url=b_url, effort=eff))
                r_inst = create_refiner(c, resolve_credentials(c, sub_env), env=sub_env, transport=ok_transport)
                res_m = r_inst.refine("测试初稿")
                self.assertEqual(res_m.provider, prov)
                self.assertEqual(res_m.model, exp_model)
                self.assertEqual(seen[0].url, exp_url)
                p_json = json.loads(seen[0].body.decode("utf-8"))
                self.assertEqual(p_json["model"], exp_model)
                for k, v in exp_extra.items():
                    self.assertEqual(p_json.get(k), v)
                if exp_model in no_temp_models:
                    self.assertNotIn("temperature", p_json)
                else:
                    self.assertEqual(p_json.get("temperature"), 0.1)
                if "reasoning" not in exp_extra:
                    self.assertNotIn("reasoning", p_json)
                if "reasoning_effort" not in exp_extra:
                    self.assertNotIn("reasoning_effort", p_json)

        # 3. Native Gemini Stage 2 refiner (:generateContent + thinkingConfig + thought filtering + fallback redaction)
        gem_seen: list[HttpRequest] = []

        def gem_ok_tx(req: HttpRequest) -> HttpResponse:
            gem_seen.append(req)
            return HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps(
                    {
                        "candidates": [
                            {
                                "content": {
                                    "parts": [
                                        {"thought": True, "text": "internal thought"},
                                        {"text": "Gemini 精修定稿"},
                                    ]
                                }
                            }
                        ]
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
            )

        cfg_gem = Config(refiner=RefinerConfig(provider="gemini", effort="low"))
        gem_ref = create_refiner(cfg_gem, resolve_credentials(cfg_gem, env), env=env, transport=gem_ok_tx)
        self.assertIsInstance(gem_ref, GeminiRefiner)
        gem_out = gem_ref.refine("Gemini 初稿", fallback_context="上下文")
        self.assertEqual(gem_out.text, "Gemini 精修定稿")
        self.assertEqual(gem_out.provider, "gemini")
        self.assertEqual(gem_out.model, "gemini-2.5-flash-lite")
        self.assertIn("models/gemini-2.5-flash-lite:generateContent", gem_seen[0].url)
        gem_body = json.loads(gem_seen[0].body.decode("utf-8"))
        self.assertEqual(gem_body["generationConfig"]["thinkingConfig"], {"thinkingBudget": 1024})

        gem_err_ref = GeminiRefiner(
            cfg_gem,
            resolve_credentials(cfg_gem, env),
            env=env,
            transport=lambda _r: HttpResponse(500, {}, f"bad key {gem_fake}".encode("utf-8")),
        )
        gem_fb = gem_err_ref.refine("Gemini 保底原稿")
        self.assertEqual(gem_fb.text, "Gemini 保底原稿")
        self.assertEqual(gem_fb.used_fallback, "asr_draft")
        self.assertNotIn(gem_fake, gem_fb.warning)

        # Legacy / invalid refiner provider raises ConfigError
        with self.assertRaises(ConfigError):
            create_refiner(Config(refiner=RefinerConfig(provider="codex-luna")))
        with self.assertRaises(ConfigError):
            create_refiner(Config(refiner=RefinerConfig(provider="doubao")))

    def test_http_refiners_redact_secret_spanning_180_char_truncation_boundary(self) -> None:
        volc_secret = "volcprefix-" + ("0" * 16)
        gem_secret = "geminiprefix-" + ("9" * 16)
        env = {
            "HUOSHAN_API_KEY": volc_secret,
            "GEMINI_API_KEY": gem_secret,
        }
        pad = ("x" * 168) + " "  # len=169 -> secret spans indices 169..196/198, crossing char 180

        volc_body = f"{pad}{volc_secret} trailing_tail"
        self.assertLess(len(pad), 180)
        self.assertGreater(len(pad) + len(volc_secret), 180)
        cfg_volc = Config(refiner=RefinerConfig(provider="volcengine"))
        volc_ref = OpenAICompatibleRefiner(
            cfg_volc,
            resolve_credentials(cfg_volc, env),
            env=env,
            transport=lambda _r: HttpResponse(502, {}, volc_body.encode("utf-8")),
        )
        volc_fb = volc_ref.refine("边界脱敏原稿")
        self.assertEqual(volc_fb.text, "边界脱敏原稿")
        self.assertEqual(volc_fb.used_fallback, "asr_draft")
        self.assertIn("[REDACTED]", volc_fb.warning)
        self.assertNotIn(volc_secret, volc_fb.warning)
        self.assertNotIn("volcprefix", volc_fb.warning)

        gem_body = f"{pad}{gem_secret} trailing_tail"
        self.assertGreater(len(pad) + len(gem_secret), 180)
        cfg_gem = Config(refiner=RefinerConfig(provider="gemini"))
        gem_ref = GeminiRefiner(
            cfg_gem,
            resolve_credentials(cfg_gem, env),
            env=env,
            transport=lambda _r: HttpResponse(502, {}, gem_body.encode("utf-8")),
        )
        gem_fb = gem_ref.refine("Gemini 边界脱敏原稿")
        self.assertEqual(gem_fb.text, "Gemini 边界脱敏原稿")
        self.assertEqual(gem_fb.used_fallback, "asr_draft")
        self.assertIn("[REDACTED]", gem_fb.warning)
        self.assertNotIn(gem_secret, gem_fb.warning)
        self.assertNotIn("geminipref", gem_fb.warning)

    def test_long_text_refiner_timeout_adaptive_relaxation(self) -> None:
        # Small unit-test timeout (< 10.0s) and short text (<= 600 chars) keep exact base timeout
        self.assertEqual(effective_refiner_timeout_sec(5.0, 1500), 5.0)
        self.assertEqual(effective_refiner_timeout_sec(12.0, 600), 12.0)
        # Long text (> 600 chars) with base >= 10.0s scales at +0.04s/char up to 240.0s cap
        self.assertAlmostEqual(effective_refiner_timeout_sec(12.0, 1600), 52.0)
        self.assertEqual(effective_refiner_timeout_sec(12.0, 10000), 240.0)

        seen_timeouts: list[float] = []

        def fake_tx(req: HttpRequest) -> HttpResponse:
            seen_timeouts.append(req.timeout_sec)
            return HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps({"choices": [{"message": {"content": "精修长文"}}]}, ensure_ascii=False).encode("utf-8"),
            )

        env = {"OPENAI_API_KEY": "sk-oai-" + ("7" * 16)}
        cfg = Config(refiner=RefinerConfig(provider="openai", timeout_sec=12.0))
        ref = OpenAICompatibleRefiner(cfg, resolve_credentials(cfg, env), env=env, transport=fake_tx)
        ref.refine("短文")
        ref.refine("字" * 1600)
        self.assertEqual(seen_timeouts[0], 12.0)
        self.assertAlmostEqual(seen_timeouts[1], 52.0)


def _isolated_tracker(base: Path) -> SessionTracker:
    return SessionTracker(home_dir=base / "home", env={}, runtime_dir=base, proc_root=base / "noproc")


class DaemonStateMachineAndSocketTests(unittest.TestCase):
    def test_zero_wav_auto_switches_pulse_to_winmm(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            plat = _FakePlatform(wav_bytes=_zero_wav(1.0), source="pulse")
            res = run_pipeline_once(
                _zero_wav(1.0),
                cfg=Config(),
                platform=plat,
                audio_source="pulse",
                asr_provider=_FakeAsr(),
                refiner=create_refiner(Config(refiner=RefinerConfig(provider="off"))),
                tracker=_isolated_tracker(Path(td)),
                env={},
            )
        self.assertFalse(res.ok)
        self.assertEqual(res.skipped_reason, "silence")
        self.assertTrue(res.switched_fallback)
        self.assertEqual(plat.switched, 1)

    def test_debounce_ptt_cancel_superseded_seq_and_focus_changed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            now = [10.0]
            gate_evt = threading.Event()
            plat = _FakePlatform(paste_outcome=PasteOutcome(False, True, "focus_changed", "chrome"))
            daemon = VoiceInputDaemon(
                Config(refiner=RefinerConfig(provider="off")),
                env={},
                runtime_dir=Path(td),
                platform=plat,
                asr_provider=_FakeAsr("旧轮次语音", gate_evt=gate_evt),
                refiner=create_refiner(Config(refiner=RefinerConfig(provider="off"))),
                tracker=_isolated_tracker(Path(td)),
                clock_fn=lambda: now[0],
            )
            # Start recording seq 1
            r1 = daemon.handle_trigger("toggle")
            self.assertTrue(r1["ok"])
            self.assertEqual(daemon.state, "recording")

            # Immediate duplicate within debounce_ms (180ms) is ignored
            now[0] += 0.05
            deb = daemon.handle_trigger("toggle")
            self.assertEqual(deb.get("ignored"), "debounced")

            # Stop recording seq 1 -> enters background recognizing stage blocked on gate_evt
            now[0] += 0.5
            r2 = daemon.handle_trigger("toggle")
            self.assertEqual(r2["state"], "recognizing")
            seq1 = r2["seq_id"]

            # User starts a new recording while seq 1 is still in flight -> increments seq_id to 2, superseding seq 1!
            now[0] += 0.5
            r3 = daemon.handle_trigger("ptt_start")
            self.assertEqual(r3["state"], "recording")
            self.assertGreater(r3["seq_id"], seq1)

            # Release seq 1 ASR; it must NOT call deliver_text because seq_id has advanced
            gate_evt.set()
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and daemon.last_result.get("seq_id") != seq1:
                time.sleep(0.02)
            self.assertEqual(daemon.last_result.get("skipped_reason"), "superseded")
            self.assertEqual(len(plat.deliveries), 0)

            # Cancel current PTT recording via chord taint
            now[0] += 0.3
            rc = daemon.handle_trigger("ptt_cancel")
            self.assertEqual(rc["state"], "idle")
            self.assertEqual(plat.canceled, 1)

            # Normal cycle with focus_changed (browser active): clipboard=True, pasted=False
            now[0] += 0.5
            daemon.handle_trigger("toggle")
            now[0] += 0.5
            s_stop = daemon.handle_trigger("toggle")
            seq_final = s_stop["seq_id"]
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and daemon.last_result.get("seq_id") != seq_final:
                time.sleep(0.02)
            self.assertTrue(daemon.last_result["ok"])
            self.assertFalse(daemon.last_result["pasted"])
            self.assertEqual(daemon.last_result["paste_reason"], "focus_changed")
            # Superseded seq 1 must NOT emit a stale HUD state overwriting seq 2's 'recording'
            self.assertEqual(plat.huds, ["recording", "recognizing", "recording", "hide", "recording", "recognizing", "copied"])
            daemon.close()

    def test_max_duration_sec_auto_stop_early_cancel_and_expected_seq_guard(self) -> None:
        from agent_voice_input.config import AudioConfig

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            plat = _FakePlatform()
            daemon = VoiceInputDaemon(
                Config(
                    audio=AudioConfig(max_duration_sec=0.06),
                    refiner=RefinerConfig(provider="off"),
                ),
                env={},
                runtime_dir=rt,
                platform=plat,
                asr_provider=_FakeAsr("十分钟自动结束并正常转写"),
                refiner=create_refiner(Config(refiner=RefinerConfig(provider="off"))),
                tracker=_isolated_tracker(rt),
            )
            try:
                # 1. Reaching max_duration_sec automatically stops recording (bypassing hotkey debounce) & completes ASR/paste
                r_start = daemon.handle_trigger("toggle")
                self.assertTrue(r_start["ok"])
                self.assertEqual(daemon.state, "recording")
                seq_auto = r_start["seq_id"]
                self.assertIsNotNone(daemon._max_dur_timer)

                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and daemon.last_result.get("seq_id") != seq_auto:
                    time.sleep(0.01)
                self.assertEqual(daemon.state, "idle")
                self.assertIsNone(daemon._max_dur_timer)
                self.assertTrue(daemon.last_result.get("ok"))
                self.assertEqual(plat.deliveries[-1][0], "十分钟自动结束并正常转写")
                self.assertEqual(plat.huds, ["recording", "recognizing", "done"])

                # 2. Stale expected_seq_id when idle (same seq_id!) MUST NOT toggle a new recording
                stale_idle = daemon.handle_trigger("toggle", expected_seq_id=seq_auto)
                self.assertEqual(stale_idle.get("ignored"), "stale_seq")
                self.assertEqual(daemon.state, "idle")

                # 3. Manual early stop & cancel cancel _max_dur_timer immediately; stale expected_seq_id cannot stop newer round
                daemon.cfg.audio.max_duration_sec = 30.0
                daemon._last_trigger_ts = -1e9
                r_s2 = daemon.handle_trigger("toggle")
                seq2 = r_s2["seq_id"]
                t_obj2 = daemon._max_dur_timer
                self.assertIsNotNone(t_obj2)
                assert t_obj2 is not None
                self.assertTrue(t_obj2.is_alive())

                # Stale seq_auto while seq2 is recording -> ignored="stale_seq", seq2 stays recording
                stale_cross = daemon.handle_trigger("toggle", expected_seq_id=seq_auto)
                self.assertEqual(stale_cross.get("ignored"), "stale_seq")
                self.assertEqual(daemon.state, "recording")
                self.assertEqual(daemon.seq_id, seq2)

                # Manual stop cancels timer immediately
                daemon._last_trigger_ts = -1e9
                daemon.handle_trigger("toggle")
                self.assertIsNone(daemon._max_dur_timer)
                t_obj2.join(timeout=0.5)
                self.assertFalse(t_obj2.is_alive())

                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and daemon.last_result.get("seq_id") != seq2:
                    time.sleep(0.01)

                # Manual ptt_cancel also cancels timer immediately
                daemon._last_trigger_ts = -1e9
                daemon.handle_trigger("ptt_start")
                t_obj3 = daemon._max_dur_timer
                self.assertIsNotNone(t_obj3)
                assert t_obj3 is not None
                daemon.handle_trigger("ptt_cancel")
                self.assertIsNone(daemon._max_dur_timer)
                t_obj3.join(timeout=0.5)
                self.assertFalse(t_obj3.is_alive())

                # Silent audio reaching max_duration_sec stops cleanly into silence gate ('empty' HUD)
                plat.wav_bytes = _zero_wav(0.5)
                daemon.cfg.audio.max_duration_sec = 0.05
                daemon._last_trigger_ts = -1e9
                r_sil = daemon.handle_trigger("toggle")
                seq_sil = r_sil["seq_id"]
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and daemon.last_result.get("seq_id") != seq_sil:
                    time.sleep(0.01)
                self.assertEqual(daemon.state, "idle")
                self.assertEqual(daemon.last_result.get("skipped_reason"), "silence")
                self.assertEqual(plat.huds[-1], "empty")
                self.assertIsNone(daemon._max_dur_timer)
            finally:
                daemon.close()
                self.assertIsNone(daemon._max_dur_timer)

    def test_unix_control_socket_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            plat = _FakePlatform()
            daemon = VoiceInputDaemon(
                Config(refiner=RefinerConfig(provider="off")),
                env={},
                runtime_dir=rt,
                platform=plat,
                asr_provider=_FakeAsr("通过控制套接字上屏"),
                refiner=create_refiner(Config(refiner=RefinerConfig(provider="off"))),
                tracker=_isolated_tracker(rt),
            )
            t = threading.Thread(target=daemon.serve_forever, daemon=True)
            t.start()
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not daemon.sock_path.exists():
                time.sleep(0.02)
            self.assertEqual(daemon.sock_path.stat().st_mode & 0o777, 0o600)

            # Client disconnecting before reading reply must NOT crash serve_forever with BrokenPipeError
            import socket as _sock
            with _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM) as s_abrupt:
                s_abrupt.connect(str(daemon.sock_path))
                s_abrupt.sendall(b'{"cmd":"status"}\n')
            st = send_control_command(rt, {"cmd": "status"})
            self.assertEqual(st["state"], "idle")

            t1 = send_control_command(rt, {"cmd": "toggle"})
            self.assertEqual(t1["state"], "recording")
            time.sleep(0.22)  # pass 180ms debounce
            t2 = send_control_command(rt, {"cmd": "toggle"})
            self.assertEqual(t2["state"], "recognizing")

            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                cur = send_control_command(rt, {"cmd": "status"})
                if cur["state"] == "idle" and cur["last_result"].get("seq_id") == t2["seq_id"]:
                    break
                time.sleep(0.02)
            self.assertEqual(plat.deliveries[-1][0], "通过控制套接字上屏")
            self.assertEqual(plat.huds, ["recording", "recognizing", "done"])

            # Empty audio cycle emits 'empty' HUD state
            plat.wav_bytes = _zero_wav(0.5)
            time.sleep(0.22)
            send_control_command(rt, {"cmd": "toggle"})
            time.sleep(0.22)
            t4 = send_control_command(rt, {"cmd": "toggle"})
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                cur = send_control_command(rt, {"cmd": "status"})
                if cur["state"] == "idle" and cur["last_result"].get("seq_id") == t4["seq_id"]:
                    break
                time.sleep(0.02)
            self.assertEqual(plat.huds[-1], "empty")

            bye = send_control_command(rt, {"cmd": "shutdown"})
            self.assertTrue(bye["stopping"])
            t.join(timeout=3.0)
            self.assertFalse(t.is_alive())
            self.assertFalse(daemon.sock_path.exists())

    def test_wsl_shim_crash_self_healing_and_rpc_recovery(self) -> None:
        from agent_voice_input.platform_wsl import WslPlatform

        crashed_THEN_healthy = (
            "import json, sys\n"
            "gen = int(sys.argv[1])\n"
            "sys.stdout.write(json.dumps({'event': 'ready', 'gen': gen}) + '\\n'); sys.stdout.flush()\n"
            "if gen == 2:\n"
            "    sys.stdout.write(json.dumps({'event': 'toggle', 'hwnd': 88, 'process': 'WindowsTerminal', 'title': 'ok'}) + '\\n'); sys.stdout.flush()\n"
            "for ln in sys.stdin:\n"
            "    req = json.loads(ln)\n"
            "    if req.get('cmd') == 'quit': break\n"
            "    if gen == 1 and req.get('cmd') == 'beep': sys.exit(2)\n"
            "    sys.stdout.write(json.dumps({'type': 'reply', 'reply': req['cmd'], 'seq_id': req['seq_id'], 'ok': True}) + '\\n')\n"
            "    sys.stdout.flush()\n"
        )
        spawns = [0]
        events: list[dict] = []
        evt_done = threading.Event()

        def factory() -> list[str]:
            spawns[0] += 1
            return [sys.executable, "-u", "-c", crashed_THEN_healthy, str(spawns[0])]

        def on_evt(msg: dict) -> None:
            events.append(msg)
            evt_done.set()

        plat = WslPlatform(Config(), env={}, shim_cmd_factory=factory)
        try:
            plat.start_event_listener(on_evt)
            self.assertEqual(spawns[0], 1)
            # First beep crashes gen=1 shim mid-RPC; _shim_rpc must self-heal into gen=2 and succeed!
            res = plat._shim_rpc({"cmd": "beep", "kind": "start"}, timeout_sec=3.0)
            self.assertTrue(res.get("ok"))
            self.assertGreaterEqual(spawns[0], 2)
            self.assertTrue(evt_done.wait(timeout=3.0))
            self.assertEqual(events[0]["hwnd"], 88)
            plat.hud("recording")
            ping = plat._shim_rpc({"cmd": "ping"}, timeout_sec=2.0)
            self.assertTrue(ping.get("ok"))
        finally:
            plat.close()

    def test_wsl_shim_event_callback_sync_rpc_no_deadlock_and_no_seq_collision(self) -> None:
        import subprocess
        from agent_voice_input.platform_wsl import WslPlatform

        mock_shim = (
            "import json, sys\n"
            "sys.stdout.write(json.dumps({'event': 'ready'}) + '\\n'); sys.stdout.flush()\n"
            "sys.stdout.write(json.dumps({'event': 'toggle', 'hwnd': 77, 'process': 'WindowsTerminal', 'title': 't'}) + '\\n'); sys.stdout.flush()\n"
            "for ln in sys.stdin:\n"
            "    req = json.loads(ln)\n"
            "    if req.get('cmd') == 'quit': break\n"
            "    sys.stdout.write(json.dumps({'reply': req['cmd'], 'seq_id': req['seq_id'], 'ok': True, 'pasted': True, 'clipboard': True}) + '\\n')\n"
            "    sys.stdout.flush()\n"
        )
        plat = WslPlatform(Config(), env={})
        done = threading.Event()
        seen_sids: list[int] = []
        errors: list[Exception] = []

        def on_evt(msg: dict) -> None:
            try:
                # Synchronous RPCs inside event callback + business seq_id=102 collision check
                r_beep = plat._shim_rpc({"cmd": "beep", "kind": "start"}, timeout_sec=1.5)
                seen_sids.append(int(r_beep["seq_id"]))
                out = plat.deliver_text("hello", seq_id=102, target_hwnd=77)
                self.assertTrue(out.pasted)
                r_ping = plat._shim_rpc({"cmd": "ping", "seq_id": 102}, timeout_sec=1.5)
                seen_sids.append(int(r_ping["seq_id"]))
            except Exception as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                done.set()

        plat._on_event = on_evt
        plat._ensure_event_worker()
        proc = subprocess.Popen([sys.executable, "-u", "-c", mock_shim], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        plat._shim = proc
        threading.Thread(target=plat._shim_reader, args=(proc,), daemon=True).start()
        try:
            self.assertTrue(plat._ready.wait(timeout=2.0))
            self.assertTrue(done.wait(timeout=2.0), "hotkey event callback deadlocked on synchronous _shim_rpc")
            self.assertEqual(errors, [])
            # Monotonic RPC seq_ids: beep=101, deliver_text(paste)=102, ping=103 (ignored payload seq_id=102!)
            self.assertEqual(seen_sids, [101, 103])
        finally:
            plat.close()

        # Also verify daemon rolls back state="idle" if get_foreground_window() or stop_recording() raises
        with tempfile.TemporaryDirectory() as td:
            bad_plat = _FakePlatform()
            daemon = VoiceInputDaemon(
                Config(refiner=RefinerConfig(provider="off")),
                env={},
                runtime_dir=Path(td),
                platform=bad_plat,
                asr_provider=_FakeAsr(),
                refiner=create_refiner(Config(refiner=RefinerConfig(provider="off"))),
                tracker=_isolated_tracker(Path(td)),
            )
            bad_plat.get_foreground_window = lambda: (_ for _ in ()).throw(RuntimeError("fg boom"))  # type: ignore[method-assign]
            r_start = daemon.handle_trigger("toggle")
            self.assertFalse(r_start["ok"])
            self.assertEqual(daemon.state, "idle")
            self.assertEqual(bad_plat.canceled, 1)

            bad_plat.get_foreground_window = lambda: ForegroundWindow(1, "WindowsTerminal", "t")  # type: ignore[method-assign]
            bad_plat.stop_recording = lambda: (_ for _ in ()).throw(RuntimeError("stop boom"))  # type: ignore[method-assign]
            daemon._last_trigger_ts = -1e9
            self.assertTrue(daemon.handle_trigger("toggle")["ok"])
            self.assertEqual(daemon.state, "recording")
            daemon._last_trigger_ts = -1e9
            r_stop = daemon.handle_trigger("toggle")
            self.assertFalse(r_stop["ok"])
            self.assertEqual(daemon.state, "idle")
            daemon.close()

    def test_cli_common_flags_and_doctor_redacted_diagnostics(self) -> None:
        import io
        from unittest import mock
        from agent_voice_input import cli as cli_mod

        parser = cli_mod._build_parser()

        before = parser.parse_args(
            [
                "--config",
                "/tmp/a.toml",
                "--asr-provider",
                "openrouter",
                "--asr-model",
                "google/gemini-2.5-flash",
                "--asr-api",
                "chat-completions",
                "--asr-effort",
                "none",
                "--refiner-provider",
                "openrouter",
                "--refiner-effort",
                "medium",
                "doctor",
            ]
        )
        after = parser.parse_args(
            [
                "doctor",
                "--config",
                "/tmp/a.toml",
                "--asr",
                "openrouter",
                "--asr-model",
                "google/gemini-2.5-flash",
                "--asr-api",
                "chat-completions",
                "--asr-effort",
                "none",
                "--refiner",
                "openrouter",
                "--effort",
                "medium",
            ]
        )
        status_before = parser.parse_args(["--config", "/tmp/b.toml", "status"])
        status_after = parser.parse_args(["status", "--config", "/tmp/b.toml"])
        hook_parsed = parser.parse_args(["session-hook", "--cli", "agy"])
        for parsed in (before, after):
            self.assertEqual(parsed.config, "/tmp/a.toml")
            self.assertEqual(parsed.asr, "openrouter")
            self.assertEqual(parsed.asr_model, "google/gemini-2.5-flash")
            self.assertEqual(parsed.asr_api, "chat-completions")
            self.assertEqual(parsed.asr_effort, "none")
            self.assertEqual(parsed.refiner, "openrouter")
            self.assertEqual(parsed.effort, "medium")
        self.assertEqual(status_before.config, "/tmp/b.toml")
        self.assertEqual(status_after.config, "/tmp/b.toml")
        self.assertEqual(hook_parsed.subcmd, "session-hook")
        self.assertEqual(hook_parsed.cli, "agy")
        ver_buf = io.StringIO()
        with mock.patch("sys.stdout", ver_buf), self.assertRaises(SystemExit) as ver_exit:
            parser.parse_args(["--version"])
        self.assertEqual(ver_exit.exception.code, 0)
        self.assertEqual(ver_buf.getvalue().strip(), "agent-voice-input 0.2.1")

        # Offline _cmd_doctor verification: displays resolved provider/model/api/effort and NEVER leaks raw secrets
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            secret_or = "sk-or-v1-" + ("8" * 20)
            env = {
                "HOME": td,
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
                "OPENROUTER_API_KEY": secret_or,
            }
            cfg, merged_env = load_config(
                env=env,
                home_dir=td,
                cli_overrides={
                    "asr.provider": "auto",
                    "refiner.provider": "openrouter",
                    "refiner.effort": "low",
                },
            )
            buf = io.StringIO()
            with (
                mock.patch.object(cli_mod, "is_wsl", return_value=False),
                mock.patch.object(cli_mod, "resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.platform", "linux"),
                mock.patch("sys.stdout", buf),
            ):
                rc = cli_mod._cmd_doctor(cfg, merged_env)
            out = buf.getvalue()
            self.assertEqual(rc, 0, out)
            self.assertIn("selected=openrouter (model=qwen/qwen3-asr-1.7b, api=audio-transcriptions, effort=auto) -> OK", out)
            self.assertIn("openrouter=OK (via OPENROUTER_API_KEY)", out)
            self.assertIn("[refiner]   OK openrouter (model=google/gemini-2.5-flash-lite, effort=low)", out)
            self.assertNotIn(secret_or, out)

            # Loopback keyless vs explicit api_key_env parity in _cmd_doctor:
            # 1) No api_key_env -> loopback openai-compatible is OK without credentials.
            # 2) Explicit api_key_env unset -> MUST report MISSING KEY (rc=1) for both ASR and Refiner even on loopback!
            loop_env = {"HOME": td, "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)}
            cfg_loop_ok = Config()
            cfg_loop_ok.asr.provider = "openai-compatible"
            cfg_loop_ok.asr.base_url = "http://127.0.0.1:8000/v1"
            cfg_loop_ok.refiner = RefinerConfig(
                provider="openai-compatible",
                model="local-qwen",
                base_url="http://127.0.0.1:8001/v1",
            )
            buf_loop_ok = io.StringIO()
            with (
                mock.patch.object(cli_mod, "is_wsl", return_value=False),
                mock.patch.object(cli_mod, "resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.platform", "linux"),
                mock.patch("sys.stdout", buf_loop_ok),
            ):
                self.assertEqual(cli_mod._cmd_doctor(cfg_loop_ok, loop_env), 0, buf_loop_ok.getvalue())

            cfg_loop_asr_miss = Config()
            cfg_loop_asr_miss.asr.provider = "openai-compatible"
            cfg_loop_asr_miss.asr.base_url = "http://127.0.0.1:8000/v1"
            cfg_loop_asr_miss.asr.api_key_env = "MISSING_LOCAL_ASR_KEY"
            cfg_loop_asr_miss.refiner = RefinerConfig(provider="off")
            buf_asr_miss = io.StringIO()
            with (
                mock.patch.object(cli_mod, "is_wsl", return_value=False),
                mock.patch.object(cli_mod, "resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.platform", "linux"),
                mock.patch("sys.stdout", buf_asr_miss),
            ):
                rc_asr_miss = cli_mod._cmd_doctor(cfg_loop_asr_miss, loop_env)
            self.assertEqual(rc_asr_miss, 1, buf_asr_miss.getvalue())
            self.assertIn("MISSING KEY", buf_asr_miss.getvalue())
            self.assertIn("asr_custom=not set", buf_asr_miss.getvalue())

            cfg_loop_ref_miss = Config()
            cfg_loop_ref_miss.asr.provider = "openai-compatible"
            cfg_loop_ref_miss.asr.base_url = "http://127.0.0.1:8000/v1"
            cfg_loop_ref_miss.refiner = RefinerConfig(
                provider="openai-compatible",
                model="local-qwen",
                base_url="http://127.0.0.1:8001/v1",
                api_key_env="MISSING_LOCAL_REF_KEY",
            )
            buf_ref_miss = io.StringIO()
            with (
                mock.patch.object(cli_mod, "is_wsl", return_value=False),
                mock.patch.object(cli_mod, "resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.platform", "linux"),
                mock.patch("sys.stdout", buf_ref_miss),
            ):
                rc_ref_miss = cli_mod._cmd_doctor(cfg_loop_ref_miss, loop_env)
            self.assertEqual(rc_ref_miss, 1, buf_ref_miss.getvalue())
            self.assertIn("[refiner]   MISSING KEY openai-compatible", buf_ref_miss.getvalue())

            # Top-level cli.main() exception redaction: non-prefix UUID-like secret stored ONLY in .env
            # under a custom api_key_env pointer MUST still be redacted on uncaught Exception & ConfigError!
            cfg_dir = Path(td) / ".config" / "agent-voice-input"
            cfg_dir.mkdir(parents=True)
            custom_uuid_secret = "11111111-" + ("2222-" * 3) + ("3" * 12)
            (cfg_dir / "config.toml").write_text(
                '[asr]\nprovider = "openai-compatible"\nbase_url = "http://127.0.0.1:8000/v1"\napi_key_env = "CUSTOM_PLAIN_CRED"\n',
                encoding="utf-8",
            )
            (cfg_dir / "agent-voice-input.env").write_text(
                f'CUSTOM_PLAIN_CRED="{custom_uuid_secret}"\n',
                encoding="utf-8",
            )
            err_buf = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"HOME": td, "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)}, clear=True),
                mock.patch.object(cli_mod, "_cmd_doctor", side_effect=RuntimeError(f"boom {custom_uuid_secret}")),
                mock.patch("sys.stderr", err_buf),
            ):
                rc_err = cli_mod.main(["doctor"])
            self.assertEqual(rc_err, 1)
            self.assertNotIn(custom_uuid_secret, err_buf.getvalue())
            self.assertIn("[REDACTED]", err_buf.getvalue())

            err_cfg_buf = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"HOME": td, "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)}, clear=True),
                mock.patch.object(cli_mod, "_cmd_doctor", side_effect=ConfigError(f"bad cfg {custom_uuid_secret}")),
                mock.patch("sys.stderr", err_cfg_buf),
            ):
                rc_cfg_err = cli_mod.main(["doctor"])
            self.assertEqual(rc_cfg_err, 2)
            self.assertNotIn(custom_uuid_secret, err_cfg_buf.getvalue())
            self.assertIn("[REDACTED]", err_cfg_buf.getvalue())

    def test_lenticular_artifact_cleanup_and_refiner_anti_truncation_guards(self) -> None:
        from agent_voice_input.asr_providers import strip_lenticular_artifacts, transcribe
        from agent_voice_input.refiner import REFINER_SYSTEM_PROMPT, build_refiner_envelope

        # Unmatched inverted '】【' artifacts are stripped without harming legitimate paired brackets
        self.assertEqual(
            strip_lenticular_artifacts("刚才我是先执行 stop，之后重新执行 start】【。"),
            "刚才我是先执行 stop，之后重新执行 start。",
        )
        self.assertEqual(strip_lenticular_artifacts("hello】 【world"), "hello world")
        self.assertEqual(strip_lenticular_artifacts("【合法前缀】尾巴】【】【。"), "【合法前缀】尾巴。")
        self.assertEqual(
            strip_lenticular_artifacts("请看【第一点】【第二点】与《设计规范》arr[0]。"),
            "请看【第一点】【第二点】与《设计规范》arr[0]。",
        )
        self.assertEqual(
            strip_lenticular_artifacts("把【配置】改一下，参考 [a](b) 和 arr[0][1]"),
            "把【配置】改一下，参考 [a](b) 和 arr[0][1]",
        )
        self.assertEqual(strip_lenticular_artifacts("单独左括号【保留"), "单独左括号【保留")

        # Envelope strips historical '】【' contamination from <context_delta>, and prompt forbids sentence deletion
        envlp = build_refiner_envelope("历史反馈 start】【。与【正常项】", "第一句保留。第二句也必须完整保留。")
        self.assertNotIn("】【", envlp)
        self.assertIn("【正常项】", envlp)
        self.assertIn("严禁概括、压缩或删减", REFINER_SYSTEM_PROMPT)
        self.assertIn("严禁把 <context_delta> 中的括号", REFINER_SYSTEM_PROMPT)

        # DashScope multi-part list content joins all segments and strips trailing '】【' artifact
        cfg = Config()
        cfg.asr.provider = "dashscope"
        cfg.asr.hotwords_file = ""
        asr_out = transcribe(
            _voiced_wav(),
            cfg,
            env={"DASHSCOPE_API_KEY": "dummy_ali_key_val"},
            context=AsrContext(dialog_text="上次转录为 start】【。"),
            transport=lambda _req: HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": [
                                        {"text": "先执行 stop，重新执行 start"},
                                        {"text": "，这样才有反应了】【。"},
                                    ]
                                }
                            }
                        ]
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
            ),
        )
        self.assertEqual(asr_out.text, "先执行 stop，重新执行 start，这样才有反应了。")

    def test_hud_precedes_stop_drain_and_beep_plus_stage_telemetry(self) -> None:
        ops: list[str] = []

        class _OrderedPlatform(_FakePlatform):
            def get_foreground_window(self) -> ForegroundWindow:
                ops.append("fg_window")
                return self.fg

            def hud(self, state: str, text: str = "", duration_ms: int = 0) -> None:
                ops.append(f"hud:{state}")
                super().hud(state, text, duration_ms)

            def beep(self, kind: str) -> None:
                ops.append(f"beep:{kind}")
                super().beep(kind)

            def start_recording(self, runtime_dir: Path) -> None:
                ops.append("start_rec")

            def stop_recording(self) -> tuple[bytes, str]:
                ops.append("stop_rec")
                return self.wav_bytes, self.source

        class _MockRefiner(CodexRefiner):
            def refine(self, asr_input, *, session_key: str = "", tracker=None, fallback_context: str = ""):
                return RefinerResult("精修后完整句子不丢字。", "codex", "gpt-6-luna", 37)

            def close(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as td:
            plat = _OrderedPlatform()
            cfg = Config()
            cfg.audio.beep = True
            daemon = VoiceInputDaemon(
                cfg,
                env={},
                runtime_dir=Path(td),
                platform=plat,
                asr_provider=_FakeAsr("精修前原始初稿句子"),
                refiner=_MockRefiner(cfg, env={}, runtime_dir=Path(td)),
                tracker=_isolated_tracker(Path(td)),
            )
            try:
                self.assertTrue(daemon.handle_trigger("toggle")["ok"])
                self.assertEqual(ops, ["hud:recording", "beep:start", "start_rec", "fg_window"])
                ops.clear()
                daemon._last_trigger_ts = -1e9
                r_stop = daemon.handle_trigger("toggle")
                self.assertEqual(r_stop["state"], "recognizing")
                # HUD switches to 'recognizing' BEFORE stop_recording() (and its tail_drain_sec) or beep!
                self.assertEqual(ops[:3], ["hud:recognizing", "stop_rec", "beep:stop"])

                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and daemon.state != "idle":
                    time.sleep(0.02)
                # Full two-stage HUD sequence: recognizing -> stop_rec -> beep:stop -> refining -> done!
                self.assertEqual(ops, ["hud:recognizing", "stop_rec", "beep:stop", "hud:refining", "hud:done"])
                st = daemon.handle_control({"cmd": "status"})
                lr = st["last_result"]
                self.assertTrue(lr["ok"])
                self.assertAlmostEqual(lr["raw_audio_sec"], 0.5, places=2)
                self.assertAlmostEqual(lr["voiced_audio_sec"], 0.5, places=2)
                self.assertEqual(lr["asr_ms"], 12)
                self.assertEqual(lr["refine_ms"], 37)
                self.assertEqual(lr["asr_chars"], len("精修前原始初稿句子"))
                self.assertEqual(lr["final_chars"], len("精修后完整句子不丢字。"))
                self.assertNotIn("精修后完整句子", json.dumps(lr, ensure_ascii=False))
            finally:
                daemon.close()

        # Verify tail_drain_sec serialization: status stays non-blocking, <debounce_ms arrival is debounced,
        # and a legitimate next toggle waits for stop_recording() to finish instead of re-entering recorder.
        drain_ops: list[str] = []
        in_drain = threading.Event()
        release_drain = threading.Event()
        sampled_deb = threading.Event()
        sampled_next = threading.Event()
        clock_val = [10.0]

        def controlled_clock() -> float:
            v = clock_val[0]
            if v == 20.05:
                sampled_deb.set()
            elif v == 20.50:
                sampled_next.set()
            return v

        class _DrainPlatform(_FakePlatform):
            def hud(self, state: str, text: str = "", duration_ms: int = 0) -> None:
                drain_ops.append(f"hud:{state}")

            def start_recording(self, runtime_dir: Path) -> None:
                drain_ops.append("start_rec")

            def stop_recording(self) -> tuple[bytes, str]:
                drain_ops.append("stop_rec_enter")
                in_drain.set()
                release_drain.wait(timeout=2.0)
                drain_ops.append("stop_rec_exit")
                return self.wav_bytes, self.source

        with tempfile.TemporaryDirectory() as td2:
            d2 = VoiceInputDaemon(
                Config(refiner=RefinerConfig(provider="off")),
                env={},
                runtime_dir=Path(td2),
                platform=_DrainPlatform(),
                asr_provider=_FakeAsr("并发排空测试"),
                refiner=create_refiner(Config(refiner=RefinerConfig(provider="off"))),
                tracker=_isolated_tracker(Path(td2)),
                clock_fn=controlled_clock,
            )
            try:
                self.assertEqual(d2.handle_trigger("toggle")["state"], "recording")
                drain_ops.clear()
                clock_val[0] = 20.0
                out_stop: dict = {}
                out_deb: dict = {}
                out_next: dict = {}
                t_stop = threading.Thread(target=lambda: out_stop.update(d2.handle_trigger("toggle")))
                t_stop.start()
                self.assertTrue(in_drain.wait(timeout=2.0))
                self.assertEqual(drain_ops, ["hud:recognizing", "stop_rec_enter"])
                self.assertEqual(d2.handle_control({"cmd": "status"})["state"], "recognizing")

                # Arrival at +50ms (<180ms debounce) while drain is still holding _trigger_lock
                clock_val[0] = 20.05
                t_deb = threading.Thread(target=lambda: out_deb.update(d2.handle_trigger("toggle")))
                t_deb.start()
                self.assertTrue(sampled_deb.wait(timeout=1.0))

                # Legitimate next toggle at +500ms while drain is still active -> must queue behind stop_rec_exit!
                clock_val[0] = 20.50
                t_next = threading.Thread(target=lambda: out_next.update(d2.handle_trigger("toggle")))
                t_next.start()
                self.assertTrue(sampled_next.wait(timeout=1.0))
                time.sleep(0.01)
                self.assertNotIn("start_rec", drain_ops)

                release_drain.set()
                for th in (t_stop, t_deb, t_next):
                    th.join(timeout=2.0)
                self.assertEqual(out_stop.get("state"), "recognizing")
                self.assertEqual(out_deb.get("ignored"), "debounced")
                self.assertEqual(out_next.get("state"), "recording")
                self.assertLess(drain_ops.index("stop_rec_exit"), drain_ops.index("start_rec"))
            finally:
                d2.close()

    def test_two_stage_recognizing_to_refining_state_and_hud_transitions(self) -> None:
        in_asr = threading.Event()
        release_asr = threading.Event()
        in_refiner = threading.Event()
        release_refiner = threading.Event()

        class _BlockingAsr(BaseAsrProvider):
            provider_name = "dashscope"

            def __init__(self, single_hop: bool = False) -> None:
                self.single_hop = single_hop

            def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None) -> AsrResult:
                in_asr.set()
                release_asr.wait(timeout=2.0)
                return AsrResult(
                    text="双阶段语音识别初稿",
                    provider="openrouter" if self.single_hop else "dashscope",
                    model="google/gemini-2.5-flash" if self.single_hop else "qwen3-asr-flash",
                    latency_ms=15,
                    is_single_hop_refined=self.single_hop,
                )

        class _BlockingRefiner(CodexRefiner):
            def refine(self, asr_input, *, session_key: str = "", tracker=None, fallback_context: str = ""):
                in_refiner.set()
                release_refiner.wait(timeout=2.0)
                return RefinerResult("双阶段整理精修定稿。", "codex", "gpt-6-luna", 28)

            def close(self) -> None:
                return None

        # 1. Stage 1 ('recognizing') -> Stage 2 ('refining') -> 'done' -> daemon 'idle'
        with tempfile.TemporaryDirectory() as td:
            plat = _FakePlatform()
            cfg = Config(refiner=RefinerConfig(provider="codex"))
            daemon = VoiceInputDaemon(
                cfg,
                env={},
                runtime_dir=Path(td),
                platform=plat,
                asr_provider=_BlockingAsr(),
                refiner=_BlockingRefiner(cfg, env={}, runtime_dir=Path(td)),
                tracker=_isolated_tracker(Path(td)),
            )
            try:
                self.assertEqual(daemon.handle_trigger("toggle")["state"], "recording")
                daemon._last_trigger_ts = -1e9
                r_stop = daemon.handle_trigger("toggle")
                self.assertEqual(r_stop["state"], "recognizing")

                self.assertTrue(in_asr.wait(timeout=2.0))
                self.assertEqual(daemon.handle_control({"cmd": "status"})["state"], "recognizing")
                self.assertEqual(plat.huds, ["recording", "recognizing"])

                release_asr.set()
                self.assertTrue(in_refiner.wait(timeout=2.0))
                self.assertEqual(daemon.handle_control({"cmd": "status"})["state"], "refining")
                self.assertEqual(plat.huds, ["recording", "recognizing", "refining"])

                release_refiner.set()
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and daemon.state != "idle":
                    time.sleep(0.02)
                self.assertEqual(daemon.state, "idle")
                self.assertEqual(plat.huds, ["recording", "recognizing", "refining", "done"])
                self.assertEqual(plat.deliveries[-1][0], "双阶段整理精修定稿。")
            finally:
                daemon.close()

        # 2. Single-hop ASR (e.g. OpenRouter ChatAudioAsrProvider / Gemini without force_on_single_hop) skips 'refining' HUD cleanly
        in_asr.clear()
        release_asr.set()
        with tempfile.TemporaryDirectory() as td_sh:
            plat_sh = _FakePlatform()
            cfg_sh = Config(refiner=RefinerConfig(provider="codex", force_on_single_hop=False))
            ref_sh = CodexRefiner(cfg_sh, env={}, runtime_dir=Path(td_sh))
            daemon_sh = VoiceInputDaemon(
                cfg_sh,
                env={},
                runtime_dir=Path(td_sh),
                platform=plat_sh,
                asr_provider=_BlockingAsr(single_hop=True),
                refiner=ref_sh,
                tracker=_isolated_tracker(Path(td_sh)),
            )
            try:
                daemon_sh.handle_trigger("toggle")
                daemon_sh._last_trigger_ts = -1e9
                daemon_sh.handle_trigger("toggle")
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and daemon_sh.state != "idle":
                    time.sleep(0.02)
                self.assertEqual(plat_sh.huds, ["recording", "recognizing", "done"])
                self.assertNotIn("refining", plat_sh.huds)
            finally:
                daemon_sh.close()

        # 3. Superseded empty/error Stage 1 must NOT emit stale 'refining'/'empty' HUD or error beep during seq 2 recording
        sup_asr_in = threading.Event()
        sup_asr_rel = threading.Event()

        class _EmptySlowAsr(BaseAsrProvider):
            provider_name = "dashscope"

            def __init__(self) -> None:
                pass

            def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None) -> AsrResult:
                sup_asr_in.set()
                sup_asr_rel.wait(timeout=2.0)
                return AsrResult(text="", provider="dashscope", model="qwen3-asr-flash", latency_ms=10)

        with tempfile.TemporaryDirectory() as td_sup:
            plat_sup = _FakePlatform()
            cfg_sup = Config(refiner=RefinerConfig(provider="codex"))
            cfg_sup.audio.beep = True
            daemon_sup = VoiceInputDaemon(
                cfg_sup,
                env={},
                runtime_dir=Path(td_sup),
                platform=plat_sup,
                asr_provider=_EmptySlowAsr(),
                refiner=_BlockingRefiner(cfg_sup, env={}, runtime_dir=Path(td_sup)),
                tracker=_isolated_tracker(Path(td_sup)),
            )
            try:
                daemon_sup.handle_trigger("toggle")
                daemon_sup._last_trigger_ts = -1e9
                r_s1 = daemon_sup.handle_trigger("toggle")
                self.assertTrue(sup_asr_in.wait(timeout=2.0))
                daemon_sup._last_trigger_ts = -1e9
                r_s2 = daemon_sup.handle_trigger("toggle")
                self.assertEqual(r_s2["state"], "recording")
                beeps_before = list(plat_sup.beeps)
                huds_before = list(plat_sup.huds)
                sup_asr_rel.set()
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and daemon_sup.last_result.get("seq_id") != r_s1["seq_id"]:
                    time.sleep(0.02)
                self.assertEqual(daemon_sup.state, "recording")
                self.assertEqual(plat_sup.huds, huds_before)
                self.assertEqual(plat_sup.beeps, beeps_before)
            finally:
                daemon_sup.close()

    def test_no_paste_silent_hud_selftest_wav_and_audio_error_observability(self) -> None:
        from agent_voice_input.platform_wsl import WslPlatform

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            plat = _FakePlatform()
            cfg = Config(refiner=RefinerConfig(provider="off"))
            daemon = VoiceInputDaemon(
                cfg,
                env={"DASHSCOPE_API_KEY": "SECRET_VAL_7788"},
                runtime_dir=tmp,
                platform=plat,
                asr_provider=_FakeAsr("免打扰实测第一句。第二句完整保留。"),
                refiner=create_refiner(cfg),
                tracker=_isolated_tracker(tmp),
            )
            try:
                # 1. no_paste + silent_hud control cycle: full ASR/refiner, zero paste, zero HUD/beep
                r1 = daemon.handle_control({"cmd": "rec_start", "no_paste": True, "silent_hud": True})
                self.assertTrue(r1["ok"])
                daemon._last_trigger_ts = -1e9
                r2 = daemon.handle_control({"cmd": "rec_stop"})
                self.assertEqual(r2["state"], "recognizing")
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and daemon.state != "idle":
                    time.sleep(0.02)
                self.assertEqual(plat.deliveries, [])
                self.assertEqual(plat.huds, [])
                self.assertEqual(plat.beeps, [])
                st1 = daemon.handle_control({"cmd": "status"})["last_result"]
                self.assertTrue(st1["ok"])
                self.assertTrue(st1["dry_run"])
                self.assertEqual(st1["paste_reason"], "dry_run")
                self.assertNotIn("免打扰实测第一句", json.dumps(st1, ensure_ascii=False))

                # 2. selftest_wav returns full text in response while keeping status last_result private
                wav_file = tmp / "sample.wav"
                wav_file.write_bytes(_voiced_wav())
                st_wav = daemon.handle_control({"cmd": "selftest_wav", "wav_path": str(wav_file), "title": "codex [#abcd1234]"})
                self.assertTrue(st_wav["ok"])
                self.assertEqual(st_wav["final_text"], "免打扰实测第一句。第二句完整保留。")
                self.assertEqual(plat.deliveries, [])
                st2 = daemon.handle_control({"cmd": "status"})["last_result"]
                self.assertNotIn("免打扰实测第一句", json.dumps(st2, ensure_ascii=False))

                # 3. Empty audio attaches redacted platform.last_audio_error into last_result
                plat.wav_bytes = b""
                plat.source = "winmm"
                plat.last_audio_error = "ffmpeg_empty(rc=1, stderr=token=SECRET_VAL_7788)"  # type: ignore[attr-defined]
                daemon._last_trigger_ts = -1e9
                daemon.handle_control({"cmd": "rec_start", "no_paste": True, "silent_hud": True})
                daemon._last_trigger_ts = -1e9
                daemon.handle_control({"cmd": "rec_stop"})
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and daemon.state != "idle":
                    time.sleep(0.02)
                st3 = daemon.handle_control({"cmd": "status"})["last_result"]
                self.assertFalse(st3["ok"])
                self.assertEqual(st3["skipped_reason"], "empty_audio")
                self.assertIn("[REDACTED]", st3["audio_error"])
                self.assertNotIn("SECRET_VAL_7788", st3["audio_error"])
            finally:
                daemon.close()

        # 4. WslPlatform auto fallback unlatches when secondary also fails instead of locking forever
        wplat = WslPlatform(Config(), env={"VOICE_INPUT_PULSE_SOURCE": "custom_src"})
        self.assertFalse(wplat._use_winmm)
        self.assertTrue(wplat.switch_to_fallback_audio())
        self.assertTrue(wplat._use_winmm)
        self.assertFalse(wplat.switch_to_fallback_audio())
        self.assertFalse(wplat._use_winmm)

    def test_end_to_end_pipeline_openrouter_single_hop_vs_dashscope_two_stage(self) -> None:
        from agent_voice_input.asr_providers import create_asr_provider

        asr_calls: list[str] = []
        ref_calls: list[str] = []
        env = {
            "OPENROUTER_API_KEY": "sk-or-v1-" + ("7" * 16),
            "DASHSCOPE_API_KEY": "dummy-dash-" + ("6" * 12),
        }

        def or_chat_tx(req: HttpRequest) -> HttpResponse:
            asr_calls.append(req.url)
            return HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps({"choices": [{"message": {"content": "OpenRouter 单跳直出文本"}}]}, ensure_ascii=False).encode("utf-8"),
            )

        def dash_asr_tx(req: HttpRequest) -> HttpResponse:
            asr_calls.append(req.url)
            return HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps({"choices": [{"message": {"content": "百炼一阶段初稿"}}]}, ensure_ascii=False).encode("utf-8"),
            )

        def or_refiner_tx(req: HttpRequest) -> HttpResponse:
            ref_calls.append(req.url)
            return HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps({"choices": [{"message": {"content": "二阶段精修后文本"}}]}, ensure_ascii=False).encode("utf-8"),
            )

        with tempfile.TemporaryDirectory() as td:
            tracker = SessionTracker(home_dir=Path(td), env={}, runtime_dir=Path(td), proc_root=Path(td) / "no_proc")

            # Case A: OpenRouter multimodal single-hop skips Stage 2 HTTP call by default
            cfg_sh = Config()
            cfg_sh.asr.provider = "openrouter"
            cfg_sh.asr.model = "google/gemini-2.5-flash"
            cfg_sh.asr.hotwords_file = ""
            cfg_sh.refiner.provider = "openrouter"
            creds_sh = resolve_credentials(cfg_sh, env)
            res_sh = run_pipeline_once(
                _voiced_wav(),
                cfg=cfg_sh,
                creds=creds_sh,
                tracker=tracker,
                asr_provider=create_asr_provider(cfg_sh, creds_sh, env=env, transport=or_chat_tx),
                refiner=create_refiner(cfg_sh, creds_sh, env=env, transport=or_refiner_tx),
                dry_run=True,
                env=env,
            )
            self.assertTrue(res_sh.ok)
            self.assertEqual(res_sh.final_text, "OpenRouter 单跳直出文本")
            self.assertEqual(len(asr_calls), 1)
            self.assertEqual(len(ref_calls), 0)
            assert res_sh.refined is not None
            self.assertEqual(res_sh.refined.used_fallback, "single_hop_passthrough")

            # Case B: DashScope qwen3-asr-flash (even though it hits compatible-mode/v1/chat/completions)
            # has is_single_hop_refined=False, so Stage 2 refiner MUST run!
            asr_calls.clear()
            ref_calls.clear()
            cfg_ds = Config()
            cfg_ds.asr.provider = "dashscope"
            cfg_ds.asr.model = "qwen3-asr-flash"
            cfg_ds.asr.hotwords_file = ""
            cfg_ds.refiner.provider = "openrouter"
            creds_ds = resolve_credentials(cfg_ds, env)
            res_ds = run_pipeline_once(
                _voiced_wav(),
                cfg=cfg_ds,
                creds=creds_ds,
                tracker=tracker,
                asr_provider=create_asr_provider(cfg_ds, creds_ds, env=env, transport=dash_asr_tx),
                refiner=create_refiner(cfg_ds, creds_ds, env=env, transport=or_refiner_tx),
                dry_run=True,
                env=env,
            )
            self.assertTrue(res_ds.ok)
            self.assertEqual(res_ds.final_text, "二阶段精修后文本")
            self.assertEqual(len(asr_calls), 1)
            self.assertEqual(len(ref_calls), 1)

    def test_only_in_terminals_config_windows_whitelist_and_daemon_gate_invariants(self) -> None:
        # 1. Config & env overrides for [hotkey] only_in_terminals
        self.assertIs(Config().hotkey.only_in_terminals, True)
        with tempfile.TemporaryDirectory() as td_cfg:
            home = Path(td_cfg)
            cfg_dir = home / ".config" / "agent-voice-input"
            cfg_dir.mkdir(parents=True)
            (cfg_dir / "config.toml").write_text("[hotkey]\nonly_in_terminals = false\n", encoding="utf-8")
            c_false, _ = load_config(env={"HOME": str(home)}, home_dir=home)
            self.assertIs(c_false.hotkey.only_in_terminals, False)

            for truthy in ("1", "true", "TRUE", "yes", "on"):
                c_t, _ = load_config(
                    env={"HOME": str(home), "AGENT_VOICE_INPUT_ONLY_IN_TERMINALS": truthy},
                    home_dir=home,
                )
                self.assertIs(c_t.hotkey.only_in_terminals, True)

            (cfg_dir / "config.toml").write_text("[hotkey]\nonly_in_terminals = true\n", encoding="utf-8")
            for falsy in ("0", "false", "FALSE", "no", "off"):
                c_f, _ = load_config(
                    env={"HOME": str(home), "VOICE_INPUT_ONLY_IN_TERMINALS": falsy},
                    home_dir=home,
                )
                self.assertIs(c_f.hotkey.only_in_terminals, False)

            # AGENT_VOICE_INPUT_ONLY_IN_TERMINALS beats VOICE_INPUT_ONLY_IN_TERMINALS
            c_pref, _ = load_config(
                env={
                    "HOME": str(home),
                    "AGENT_VOICE_INPUT_ONLY_IN_TERMINALS": "true",
                    "VOICE_INPUT_ONLY_IN_TERMINALS": "false",
                },
                home_dir=home,
            )
            self.assertIs(c_pref.hotkey.only_in_terminals, True)

            with self.assertRaises(ConfigError):
                load_config(
                    env={"HOME": str(home), "AGENT_VOICE_INPUT_ONLY_IN_TERMINALS": "maybe"},
                    home_dir=home,
                )
            (cfg_dir / "config.toml").write_text("[hotkey]\nonly_in_terminals = 123\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(env={"HOME": str(home)}, home_dir=home)

        # 2. Windows terminal whitelist single source of truth
        wplat = WslPlatform(Config(), env={"VOICE_INPUT_PULSE_SOURCE": "custom_src"})
        for ok_proc in (
            "WindowsTerminal",
            "WindowsTerminal.exe",
            "OpenConsole.exe",
            "mintty",
            "wezterm",
            "wezterm-gui.exe",
            "alacritty",
            "ghostty.exe",
            "conhost",
            "putty",
            "tabby",
            "hyper",
            "kitty",
            "rio.exe",
        ):
            self.assertTrue(is_windows_terminal_process(ok_proc), msg=ok_proc)
            self.assertTrue(wplat.is_allowed_terminal(ForegroundWindow(1, ok_proc, "t")), msg=ok_proc)

        for bad_proc in ("mstsc.exe", "mstsc", "chrome.exe", "msedge.exe", "Code.exe", "explorer", ""):
            self.assertFalse(is_windows_terminal_process(bad_proc), msg=bad_proc)
            self.assertFalse(wplat.is_allowed_terminal(ForegroundWindow(1, bad_proc, "t")), msg=bad_proc)

        # 3. Daemon gate: non-terminal start blocked cleanly; active recording NEVER stuck on window switch!
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            now = [100.0]
            start_calls = [0]

            class _CountingFakePlatform(_FakePlatform):
                def start_recording(self, runtime_dir: Path) -> None:
                    start_calls[0] += 1

            plat = _CountingFakePlatform(paste_outcome=PasteOutcome(False, True, "focus_changed", "mstsc"))
            cfg = Config(refiner=RefinerConfig(provider="off"))
            cfg.audio.beep = True
            daemon = VoiceInputDaemon(
                cfg,
                env={},
                runtime_dir=rt,
                platform=plat,
                asr_provider=_FakeAsr("切窗停止不卡死"),
                refiner=create_refiner(cfg),
                tracker=_isolated_tracker(rt),
                clock_fn=lambda: now[0],
            )
            try:
                non_terms = [
                    ForegroundWindow(10, "mstsc.exe", "Remote Desktop"),
                    ForegroundWindow(11, "chrome.exe", "Google"),
                    ForegroundWindow(0, "", ""),
                    ForegroundWindow(0, "Windows App", "Win11", bundle_id="com.microsoft.rdc.macos"),
                    ForegroundWindow(0, "Safari", "Apple", bundle_id="com.apple.Safari"),
                ]
                for w_bad in non_terms:
                    for act in ("toggle", "ptt_start"):
                        resp = daemon.handle_trigger(act, w_bad, from_hotkey=True)
                        self.assertEqual(
                            resp,
                            {"ok": False, "ignored": "non_terminal", "state": "idle", "seq_id": 0},
                        )
                # None window with from_hotkey=True also blocked
                self.assertEqual(
                    daemon.handle_trigger("toggle", None, from_hotkey=True),
                    {"ok": False, "ignored": "non_terminal", "state": "idle", "seq_id": 0},
                )
                self.assertEqual(start_calls[0], 0)
                self.assertEqual(plat.huds, [])
                self.assertEqual(plat.beeps, [])
                self.assertEqual(daemon._last_trigger_ts, -1e9)

                # Immediately (+10ms < 180ms debounce_ms!) pressing hotkey in WindowsTerminal.exe MUST start recording
                # because blocked non_terminal triggers never polluted _last_trigger_ts!
                now[0] += 0.01
                wt_win = ForegroundWindow(200, "WindowsTerminal.exe", "codex [#abcd1234]")
                r_start = daemon.handle_trigger("toggle", wt_win, from_hotkey=True)
                self.assertTrue(r_start["ok"])
                self.assertEqual(r_start["state"], "recording")
                self.assertEqual(r_start["seq_id"], 1)
                self.assertEqual(start_calls[0], 1)
                self.assertEqual(plat.huds, ["recording"])
                self.assertEqual(plat.beeps, ["start"])

                # Mid-recording user switches focus to Windows App / mstsc.exe and presses hotkey to stop (toggle):
                # MUST NOT be blocked by only_in_terminals!
                now[0] += 0.5
                rdp_win = ForegroundWindow(0, "Windows App", "Win11", bundle_id="com.microsoft.rdc.macos")
                r_stop = daemon.handle_trigger("toggle", rdp_win, from_hotkey=True)
                self.assertTrue(r_stop["ok"])
                self.assertEqual(r_stop["state"], "recognizing")
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and daemon.state != "idle":
                    time.sleep(0.02)
                self.assertEqual(daemon.state, "idle")
                self.assertEqual(plat.huds[-1], "copied")

                # PTT start in Ghostty -> switch to Windows App -> ptt_stop works cleanly
                now[0] += 0.5
                ghostty_win = ForegroundWindow(0, "Ghostty", "zsh", bundle_id="com.mitchellh.ghostty")
                self.assertEqual(
                    daemon.handle_trigger("ptt_start", ghostty_win, from_hotkey=True)["state"],
                    "recording",
                )
                now[0] += 0.5
                r_ptt_stop = daemon.handle_trigger("ptt_stop", rdp_win, from_hotkey=True)
                self.assertTrue(r_ptt_stop["ok"])
                self.assertEqual(r_ptt_stop["state"], "recognizing")
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and daemon.state != "idle":
                    time.sleep(0.02)

                # PTT start in Chinese macOS 终端 -> switch to Windows App -> ptt_cancel works cleanly
                now[0] += 0.5
                zh_term = ForegroundWindow(0, "终端", "zsh", bundle_id="com.apple.Terminal")
                self.assertEqual(
                    daemon.handle_trigger("ptt_start", zh_term, from_hotkey=True)["state"],
                    "recording",
                )
                now[0] += 0.3
                r_cancel = daemon.handle_trigger("ptt_cancel", rdp_win, from_hotkey=True)
                self.assertTrue(r_cancel["ok"])
                self.assertEqual(r_cancel["state"], "idle")
                self.assertEqual(plat.canceled, 1)

                # Explicit CLI control toggle (from_hotkey=False, window=None) is NOT blocked even if plat.fg is chrome
                plat.fg = ForegroundWindow(300, "chrome.exe", "Browser")
                now[0] += 0.5
                r_cli = daemon.handle_control({"cmd": "toggle"})
                self.assertTrue(r_cli["ok"])
                self.assertEqual(r_cli["state"], "recording")
                now[0] += 0.3
                daemon.handle_trigger("ptt_cancel")

                # only_in_terminals = False allows starting hotkey recording in non-terminal windows
                daemon.cfg.hotkey.only_in_terminals = False
                now[0] += 0.5
                r_any = daemon.handle_trigger("toggle", rdp_win, from_hotkey=True)
                self.assertTrue(r_any["ok"])
                self.assertEqual(r_any["state"], "recording")
                now[0] += 0.3
                daemon.handle_trigger("ptt_cancel")
            finally:
                daemon.close()

        # 4. serve_forever registers hotkey listener with from_hotkey=True
        with tempfile.TemporaryDirectory() as td_srv:
            rt_srv = Path(td_srv)
            plat_srv = _FakePlatform()
            daemon_srv = VoiceInputDaemon(
                Config(refiner=RefinerConfig(provider="off")),
                env={},
                runtime_dir=rt_srv,
                platform=plat_srv,
                asr_provider=_FakeAsr("监听回调门禁"),
                refiner=create_refiner(Config(refiner=RefinerConfig(provider="off"))),
                tracker=_isolated_tracker(rt_srv),
            )
            t_srv = threading.Thread(target=daemon_srv.serve_forever, daemon=True)
            t_srv.start()
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not daemon_srv.sock_path.exists():
                time.sleep(0.02)
            assert plat_srv.listener is not None
            try:
                # Non-terminal event from shim listener is ignored
                plat_srv.listener({"event": "toggle", "hwnd": 55, "process": "mstsc", "title": "RDP"})
                self.assertEqual(daemon_srv.state, "idle")
                self.assertEqual(daemon_srv.seq_id, 0)
                self.assertEqual(plat_srv.huds, [])

                # Terminal event from shim listener starts recording; non-terminal toggle stops it
                plat_srv.listener({"event": "toggle", "hwnd": 77, "process": "WindowsTerminal", "title": "codex"})
                self.assertEqual(daemon_srv.state, "recording")
                daemon_srv._last_trigger_ts = -1e9
                plat_srv.listener({"event": "toggle", "hwnd": 55, "process": "mstsc", "title": "RDP"})
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and daemon_srv.state != "idle":
                    time.sleep(0.02)
                self.assertEqual(daemon_srv.state, "idle")
            finally:
                send_control_command(rt_srv, {"cmd": "shutdown"})
                t_srv.join(timeout=3.0)

    def test_long_audio_chunk_pipeline_refiner_timeout_scaling_and_failed_retry_cli(self) -> None:
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from agent_voice_input import cli as cli_mod
        from agent_voice_input.audio_proc import list_failed_audios

        # 1. Refiner timeout scaling preserves short explicit budgets and expands for long transcripts
        self.assertEqual(effective_refiner_timeout_sec(3.0, 120), 3.0)
        self.assertEqual(effective_refiner_timeout_sec(3.0, 2000), 3.0)
        self.assertEqual(effective_refiner_timeout_sec(90.0, 600), 90.0)
        self.assertAlmostEqual(effective_refiner_timeout_sec(90.0, 2100), 150.0, places=2)
        self.assertEqual(effective_refiner_timeout_sec(90.0, 10000), 240.0)

        seen_timeouts: list[float] = []

        def _capture_tx(req: HttpRequest) -> HttpResponse:
            seen_timeouts.append(req.timeout_sec)
            return HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps({"choices": [{"message": {"content": "精修长文输出"}}]}, ensure_ascii=False).encode("utf-8"),
            )

        cfg_slow = Config(refiner=RefinerConfig(provider="siliconflow", timeout_sec=90.0))
        env_sf = {"SILICONFLOW_API_KEY": "sf_fake_key_123456"}
        ref_slow = OpenAICompatibleRefiner(
            cfg_slow,
            resolve_credentials(cfg_slow, env_sf),
            provider_name="siliconflow",
            env=env_sf,
            transport=_capture_tx,
        )
        ref_slow.refine("长" * 1600)
        self.assertAlmostEqual(seen_timeouts[0], 130.0, places=2)

        # 2. Multi-chunk concurrent Stage 1 transcription -> ordered merge -> single Stage 2 refinement pass
        sr = 16000
        long_samples = (
            [3800, -3800] * int(1.6 * sr)  # 3.2s
            + [0] * int(0.5 * sr)          # 0.5s pause
            + [3800, -3800] * int(1.6 * sr)  # 3.2s -> total 6.9s (> max_chunk_sec=4.5s)
        )
        long_wav = pack_pcm16_mono_wav(long_samples)

        class _ChunkRecordingAsr(BaseAsrProvider):
            provider_name = "dashscope"

            def __init__(self) -> None:
                self._lk = threading.Lock()
                self.n = 0

            def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None) -> AsrResult:
                with self._lk:
                    self.n += 1
                    idx = self.n
                return AsrResult(f"第{idx}段内容", "dashscope", "qwen3-asr-flash", 25, False)

        class _EchoRefiner(OpenAICompatibleRefiner):
            provider_name = "dashscope"

            def __init__(self) -> None:
                self.seen_drafts: list[str] = []

            def refine(self, asr_input, *, session_key="", tracker=None, fallback_context=""):  # type: ignore[override]
                txt = asr_input.text if isinstance(asr_input, AsrResult) else str(asr_input)
                self.seen_drafts.append(txt)
                return RefinerResult(f"整篇精修：{txt}", "dashscope", "qwen-turbo-latest", 18)

            def close(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            chunk_asr = _ChunkRecordingAsr()
            echo_ref = _EchoRefiner()
            plat_ok = _FakePlatform(wav_bytes=long_wav)
            res_multi = run_pipeline_once(
                long_wav,
                cfg=Config(refiner=RefinerConfig(provider="dashscope")),
                tracker=_isolated_tracker(rt),
                asr_provider=chunk_asr,
                refiner=echo_ref,
                platform=plat_ok,
                audio_source="avfoundation",
                runtime_dir=rt,
                max_chunk_sec=4.5,
                env={},
            )
            self.assertTrue(res_multi.ok)
            self.assertEqual(res_multi.asr_chunks, 2)
            self.assertEqual(len(res_multi.asr_chunk_secs), 2)
            self.assertAlmostEqual(sum(res_multi.asr_chunk_secs), 6.9, delta=0.1)
            self.assertEqual(chunk_asr.n, 2)
            self.assertEqual(len(echo_ref.seen_drafts), 1)
            self.assertIn("，", echo_ref.seen_drafts[0])
            self.assertTrue(res_multi.final_text.startswith("整篇精修："))
            self.assertEqual(list_failed_audios(rt), [])  # successful run never writes to failed/

            # 2b. 5-way parallel chunk transcription: 5 chunks run in a single simultaneous wave; >5 chunks cap at 5 while preserving order
            import array as _array
            import wave as _wave

            self.assertEqual(MAX_ASR_CHUNK_CONCURRENCY, 5)

            def _build_distinct_chunks_wav(num_chunks: int) -> bytes:
                segs: list[int] = []
                for k in range(num_chunks):
                    amp = 2000 + k * 600
                    segs.extend([amp, -amp] * int(1.0 * sr))  # 2.0s voiced segment
                    if k + 1 < num_chunks:
                        segs.extend([0] * int(0.5 * sr))  # 0.5s silent pause
                return pack_pcm16_mono_wav(segs)

            def _chunk_id_from_wav(chunk_bytes: bytes) -> int:
                with _wave.open(io.BytesIO(chunk_bytes), "rb") as wf:
                    raw = wf.readframes(wf.getnframes())
                samples = _array.array("h")
                samples.frombytes(raw)
                if sys.byteorder != "little":
                    samples.byteswap()
                peak = max((abs(int(x)) for x in samples), default=0)
                return max(0, int(round((peak - 2000) / 600.0)))

            barrier_5 = threading.Barrier(5, timeout=2.0)

            class _FiveWaveAsr(BaseAsrProvider):
                provider_name = "dashscope"

                def __init__(self) -> None:
                    pass

                def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None) -> AsrResult:
                    cid = _chunk_id_from_wav(wav_bytes)
                    barrier_5.wait()
                    return AsrResult(f"第{cid + 1}段", "dashscope", "qwen3-asr-flash", 10, False)

            five_res, five_cnt, _ = _transcribe_wav_chunks(
                _build_distinct_chunks_wav(5),
                eff_asr=_FiveWaveAsr(),
                asr_ctx=AsrContext("", "", ""),
                floor_dbfs=-50.0,
                max_chunk_sec=3.0,
            )
            self.assertEqual(five_cnt, 5)
            self.assertEqual(five_res.text, "第1段，第2段，第3段，第4段，第5段")

            class _CapFiveAsr(BaseAsrProvider):
                provider_name = "dashscope"

                def __init__(self) -> None:
                    self._lk = threading.Lock()
                    self.active = 0
                    self.peak_active = 0

                def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None) -> AsrResult:
                    cid = _chunk_id_from_wav(wav_bytes)
                    with self._lk:
                        self.active += 1
                        if self.active > self.peak_active:
                            self.peak_active = self.active
                    try:
                        time.sleep(0.06 if cid < 2 else 0.02)
                        return AsrResult(f"第{cid + 1}段", "dashscope", "qwen3-asr-flash", 12, False)
                    finally:
                        with self._lk:
                            self.active -= 1

            cap_asr = _CapFiveAsr()
            seven_res, seven_cnt, _ = _transcribe_wav_chunks(
                _build_distinct_chunks_wav(7),
                eff_asr=cap_asr,
                asr_ctx=AsrContext("", "", ""),
                floor_dbfs=-50.0,
                max_chunk_sec=3.0,
            )
            self.assertEqual(seven_cnt, 7)
            self.assertEqual(cap_asr.peak_active, 5)
            self.assertEqual(seven_res.text, "第1段，第2段，第3段，第4段，第5段，第6段，第7段")

            # 3. Daemon ASR failure stashes WAV labeled with cwd basename; subsequent successful recording NEVER deletes it!
            class _BoomThenOkAsr(BaseAsrProvider):
                provider_name = "dashscope"

                def __init__(self) -> None:
                    self.calls = 0

                def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None) -> AsrResult:
                    self.calls += 1
                    if self.calls == 1:
                        raise RuntimeError("DashScope HTTP 400: The audio is too long")
                    return AsrResult("恢复成功后的完整文本", "dashscope", "qwen3-asr-flash", 15, False)

            boom_asr = _BoomThenOkAsr()
            plat_d = _FakePlatform(wav_bytes=long_wav)
            plat_d.fg = ForegroundWindow(
                0, "Ghostty", "agy", bundle_id="com.mitchellh.ghostty", cwd="/Users/demo/repos/post-studio"
            )
            daemon = VoiceInputDaemon(
                Config(refiner=RefinerConfig(provider="off")),
                env={},
                runtime_dir=rt,
                platform=plat_d,
                asr_provider=boom_asr,
                refiner=create_refiner(Config(refiner=RefinerConfig(provider="off"))),
                tracker=_isolated_tracker(rt),
            )
            try:
                daemon.handle_trigger("toggle", plat_d.fg)
                daemon._last_trigger_ts = -1e9
                r_stop1 = daemon.handle_trigger("toggle", plat_d.fg)
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and daemon.last_result.get("seq_id") != r_stop1["seq_id"]:
                    time.sleep(0.02)
                self.assertFalse(daemon.last_result["ok"])
                saved_path_str = daemon.last_result.get("saved_audio") or ""
                self.assertTrue(saved_path_str.endswith(".wav"))
                self.assertIn("post-studio", Path(saved_path_str).name)
                self.assertTrue(Path(saved_path_str).is_file())

                # Second recording succeeds -> first failed recording MUST remain intact on disk!
                daemon._last_trigger_ts = -1e9
                daemon.handle_trigger("toggle", plat_d.fg)
                daemon._last_trigger_ts = -1e9
                r_stop2 = daemon.handle_trigger("toggle", plat_d.fg)
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and daemon.last_result.get("seq_id") != r_stop2["seq_id"]:
                    time.sleep(0.02)
                self.assertTrue(daemon.last_result["ok"])
                self.assertTrue(Path(saved_path_str).is_file())
            finally:
                daemon.close()

            # 4. Paste + clipboard double failure emits 'error' HUD and also stashes failed WAV
            plat_clip_fail = _FakePlatform(
                wav_bytes=_voiced_wav(),
                paste_outcome=PasteOutcome(False, False, "clipboard_failed", "Ghostty"),
            )
            plat_clip_fail.fg = ForegroundWindow(0, "Ghostty", "zsh", bundle_id="com.mitchellh.ghostty", cwd="/tmp/other-proj")
            daemon2 = VoiceInputDaemon(
                Config(refiner=RefinerConfig(provider="off")),
                env={},
                runtime_dir=rt,
                platform=plat_clip_fail,
                asr_provider=_FakeAsr("剪贴板挂了"),
                refiner=create_refiner(Config(refiner=RefinerConfig(provider="off"))),
                tracker=_isolated_tracker(rt),
            )
            try:
                daemon2.handle_trigger("toggle", plat_clip_fail.fg)
                daemon2._last_trigger_ts = -1e9
                r_cf = daemon2.handle_trigger("toggle", plat_clip_fail.fg)
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and daemon2.last_result.get("seq_id") != r_cf["seq_id"]:
                    time.sleep(0.02)
                self.assertEqual(plat_clip_fail.huds[-1], "error")
                self.assertIn("other-proj", Path(daemon2.last_result["saved_audio"]).name)
            finally:
                daemon2.close()

            # 5. CLI 'retry --list' & 'retry post-studio --dry-run' recover the exact target recording cleanly
            iso_home = Path(td) / "home"
            iso_home.mkdir(parents=True)
            cli_env = {"HOME": str(iso_home), "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)}
            out_list = io.StringIO()
            with unittest.mock.patch.dict(os.environ, cli_env, clear=True), redirect_stdout(out_list):
                rc_l = cli_mod.main(["retry", "--list"])
            self.assertEqual(rc_l, 0)
            self.assertIn("post-studio", out_list.getvalue())
            self.assertIn("other-proj", out_list.getvalue())

            before_files = list_failed_audios(rt)
            out_run, err_run = io.StringIO(), io.StringIO()
            with (
                unittest.mock.patch.dict(os.environ, cli_env, clear=True),
                unittest.mock.patch("agent_voice_input.daemon.create_asr_provider", return_value=_FakeAsr("找回的 post-studio 长语音内容")),
                redirect_stdout(out_run),
                redirect_stderr(err_run),
            ):
                rc_r = cli_mod.main(["retry", "post-studio", "--refiner", "off", "--dry-run", "--title", "agy"])
            self.assertEqual(rc_r, 0)
            self.assertEqual(out_run.getvalue().strip(), "找回的 post-studio 长语音内容")
            self.assertEqual(list_failed_audios(rt), before_files)  # retry Neither deletes nor duplicates stashed WAVs!

    def test_no_microphone_fail_fast_and_mid_recording_disconnect_recovery(self) -> None:
        import subprocess
        from agent_voice_input.hud import format_hud_text
        from agent_voice_input.platform_macos import MacOSPlatform, probe_macos_host
        from agent_voice_input.platform_wsl import FfmpegWavRecorder, NoMicrophoneError, WslPlatform

        self.assertEqual(format_hud_text("no_mic", "zh"), "✕ 无可用麦克风")
        self.assertEqual(format_hud_text("no_mic", "en"), "✕ No microphone found")
        self.assertEqual(format_hud_text("mic_disconnected", "zh"), "✕ 麦克风已断开")
        self.assertEqual(format_hud_text("mic_disconnected", "en"), "✕ Microphone disconnected")

        # 1. MacOSPlatform + Daemon fail-fast on rc=251 ("Audio device not found"): immediate no_mic HUD + error beep!
        no_dev_src = (
            "import sys\n"
            "sys.stderr.write('[AVFoundation indev] Audio device not found\\n')\n"
            "sys.stderr.flush()\n"
            "sys.exit(251)\n"
        )
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            hud_events: list[tuple[str, int]] = []
            beeps: list[str] = []
            mplat = MacOSPlatform(Config(), env={"HOME": td}, cache_dir=Path(td) / "cache", shim_cmd_factory=lambda: None)
            mplat._ff_rec = FfmpegWavRecorder(cmd_factory=lambda _f, _s: [sys.executable, "-c", no_dev_src])
            mplat.hud = lambda st, text="", duration_ms=0: hud_events.append((st, duration_ms))  # type: ignore[method-assign]
            mplat.beep = lambda kind: beeps.append(kind)  # type: ignore[method-assign]
            mplat.resolve_foreground_window = lambda win=None: ForegroundWindow(0, "Ghostty", "agy", "com.mitchellh.ghostty")  # type: ignore[method-assign]

            cfg_off = Config(refiner=RefinerConfig(provider="off"))
            daemon = VoiceInputDaemon(
                cfg_off,
                env={"HOME": td},
                runtime_dir=rt,
                platform=mplat,
                asr_provider=_FakeAsr("unused"),
                refiner=create_refiner(cfg_off),
                tracker=_isolated_tracker(rt),
            )
            try:
                resp = daemon.handle_trigger("toggle", ForegroundWindow(0, "Ghostty", "agy", "com.mitchellh.ghostty"))
                self.assertFalse(resp["ok"])
                self.assertEqual(resp["state"], "idle")
                self.assertEqual(resp.get("skipped_reason"), "no_mic")
                self.assertEqual(daemon.state, "idle")
                self.assertEqual(daemon.last_result.get("skipped_reason"), "no_mic")
                self.assertIn("Audio device not found", daemon.last_result.get("error", ""))
                self.assertEqual(hud_events[-1], ("no_mic", 2200))
                self.assertEqual(beeps, ["error"])  # never played 'start'!
            finally:
                daemon.close()

            # 2. WSL2 WASAPI E_NOTFOUND (0x80070490) fails fast with NoMicrophoneError without dead pulse fallback
            wplat = WslPlatform(Config(), env={})
            wplat._use_winmm = True
            wplat._shim_rpc = lambda req, timeout_sec=4.0: {"ok": False, "rec_error": "wasapi_endpoint_hr_0x80070490"}  # type: ignore[method-assign]
            with self.assertRaises(NoMicrophoneError):
                wplat.start_recording(rt)

            # 3a. Mid-recording disconnect after 0.6s of pure silence (>= min_duration_sec, unvoiced):
            # reports mic_disconnected HUD (2200ms) + single error beep (NOT 'empty' / double beep)!
            short_drop_src = (
                "import os, sys, time\n"
                "os.write(1, (b'\\x00\\x00' * 9600))\n"  # 0.6s @ 16kHz pure silence before disconnect
                "sys.stdout.flush()\n"
                "time.sleep(0.06)\n"
                "sys.stderr.write('Device unplugged\\n')\n"
                "sys.exit(251)\n"
            )
            hud_short: list[tuple[str, int]] = []
            beep_short: list[str] = []
            mplat_short = MacOSPlatform(Config(), env={"HOME": td}, cache_dir=Path(td) / "cache_short", shim_cmd_factory=lambda: None)
            mplat_short._ff_rec = FfmpegWavRecorder(cmd_factory=lambda _f, _s: [sys.executable, "-c", short_drop_src])
            mplat_short.hud = lambda st, text="", duration_ms=0: hud_short.append((st, duration_ms))  # type: ignore[method-assign]
            mplat_short.beep = lambda kind: beep_short.append(kind)  # type: ignore[method-assign]
            mplat_short.resolve_foreground_window = lambda win=None: ForegroundWindow(0, "Ghostty", "agy", "com.mitchellh.ghostty")  # type: ignore[method-assign]
            daemon_short = VoiceInputDaemon(
                cfg_off,
                env={"HOME": td},
                runtime_dir=rt,
                platform=mplat_short,
                asr_provider=_FakeAsr("unused"),
                refiner=create_refiner(cfg_off),
                tracker=_isolated_tracker(rt),
            )
            try:
                self.assertTrue(daemon_short.handle_trigger("toggle", ForegroundWindow(0, "Ghostty", "agy", "com.mitchellh.ghostty"))["ok"])
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and daemon_short.state != "idle":
                    time.sleep(0.02)
                self.assertEqual(daemon_short.state, "idle")
                self.assertEqual(daemon_short.last_result.get("skipped_reason"), "mic_disconnected")
                self.assertEqual(hud_short[-1], ("mic_disconnected", 2200))
                self.assertEqual(beep_short.count("error"), 1)
                # User's instinctive 'stop' tap right after disconnect is absorbed once without wiping mic_disconnected HUD
                daemon_short._last_trigger_ts = -1e9
                r_abs = daemon_short.handle_trigger("toggle", ForegroundWindow(0, "Ghostty", "agy", "com.mitchellh.ghostty"))
                self.assertEqual(r_abs.get("ignored"), "already_stopped")
                self.assertEqual(hud_short[-1], ("mic_disconnected", 2200))
            finally:
                daemon_short.close()

            # 3b. Mid-recording Bluetooth disconnect AFTER capturing 0.8s of speech:
            # alerts user, absorbs user's instinctive 'stop' toggle while ASR is in-flight, AND pastes rescued speech!
            mid_drop_src = (
                "import os, sys, time\n"
                "os.write(1, (b'\\xe8\\x0e\\x18\\xf1' * 6400))\n"  # 0.8s @ 16kHz s16le voiced audio
                "sys.stdout.flush()\n"
                "time.sleep(0.08)\n"
                "sys.stderr.write('Input/output error\\n')\n"
                "sys.exit(251)\n"
            )
            asr_entered = threading.Event()
            release_asr = threading.Event()

            class _SlowRescueAsr(_FakeAsr):
                def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None) -> AsrResult:
                    asr_entered.set()
                    release_asr.wait(timeout=2.0)
                    return AsrResult("断麦前已说的前半段内容", "fake", "m", 15)

            hud_mid: list[str] = []
            beep_mid: list[str] = []
            pasted_mid: list[str] = []
            mplat2 = MacOSPlatform(Config(), env={"HOME": td}, cache_dir=Path(td) / "cache2", shim_cmd_factory=lambda: None)
            mplat2._ff_rec = FfmpegWavRecorder(cmd_factory=lambda _f, _s: [sys.executable, "-c", mid_drop_src])
            mplat2.hud = lambda st, text="", duration_ms=0: hud_mid.append(st)  # type: ignore[method-assign]
            mplat2.beep = lambda kind: beep_mid.append(kind)  # type: ignore[method-assign]
            mplat2.resolve_foreground_window = lambda win=None: ForegroundWindow(0, "Ghostty", "agy", "com.mitchellh.ghostty")  # type: ignore[method-assign]
            mplat2.deliver_text = lambda txt, seq_id, target_hwnd=0, *, origin_window=None: (pasted_mid.append(txt) or PasteOutcome(True, True, "ok", "Ghostty"))  # type: ignore[method-assign]

            daemon2 = VoiceInputDaemon(
                cfg_off,
                env={"HOME": td},
                runtime_dir=rt,
                platform=mplat2,
                asr_provider=_SlowRescueAsr(),
                refiner=create_refiner(cfg_off),
                tracker=_isolated_tracker(rt),
            )
            try:
                r_start = daemon2.handle_trigger("toggle", ForegroundWindow(0, "Ghostty", "agy", "com.mitchellh.ghostty"))
                self.assertTrue(r_start["ok"])
                self.assertTrue(asr_entered.wait(timeout=2.0))
                # User presses toggle to "finish speaking" while rescue ASR is in-flight: must NOT supersede rescue!
                daemon2._last_trigger_ts = -1e9
                r_stop_tap = daemon2.handle_trigger("toggle", ForegroundWindow(0, "Ghostty", "agy", "com.mitchellh.ghostty"))
                self.assertTrue(r_stop_tap["ok"])
                self.assertEqual(r_stop_tap.get("ignored"), "already_stopped")
                self.assertEqual(r_stop_tap["seq_id"], r_start["seq_id"])
                release_asr.set()
                deadline = time.monotonic() + 2.5
                while time.monotonic() < deadline and not pasted_mid:
                    time.sleep(0.02)
                self.assertEqual(pasted_mid, ["断麦前已说的前半段内容"])
                self.assertEqual(daemon2.state, "idle")
                self.assertEqual(beep_mid.count("error"), 1)
                self.assertEqual(hud_mid[-1], "done")
            finally:
                release_asr.set()
                daemon2.close()

            # 4. probe_macos_host sets avfoundation_ok=False when zero audio input devices exist
            empty_avf_out = "[AVFoundation indev @ 0x1] AVFoundation audio devices:\n[in#0] Error opening input\n"
            with (
                unittest.mock.patch("agent_voice_input.platform_macos.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                unittest.mock.patch(
                    "agent_voice_input.platform_macos.subprocess.run",
                    return_value=subprocess.CompletedProcess(["ffmpeg"], 1, "", empty_avf_out),
                ),
                unittest.mock.patch("agent_voice_input.platform_macos.compile_mac_shim", return_value=(None, "skip")),
            ):
                info = probe_macos_host(Config(), env={"HOME": td}, cache_dir=Path(td) / "c3")
            self.assertEqual(info["audio_devices"], [])
            self.assertFalse(info["avfoundation_ok"])

    def test_daemon_start_recording_precedes_slow_resolver_and_cancels_on_error(self) -> None:
        in_resolver = threading.Event()
        release_resolver = threading.Event()
        events: list[str] = []
        rec_active = [False]
        rec_active_during_resolve = [False]
        fail_resolve = [False]
        fail_cancel = [False]

        class _SlowResolvePlatform(_FakePlatform):
            def hud(self, state: str, text: str = "", duration_ms: int = 0) -> None:
                events.append(f"hud:{state}")
                super().hud(state, text, duration_ms)

            def beep(self, kind: str) -> None:
                events.append(f"beep:{kind}")
                super().beep(kind)

            def start_recording(self, runtime_dir: Path, *, loopback: bool = False) -> None:
                events.append(f"start_rec:loopback={loopback}")
                rec_active[0] = True

            def cancel_recording(self) -> None:
                events.append("cancel_rec")
                rec_active[0] = False
                super().cancel_recording()
                if fail_cancel[0]:
                    raise RuntimeError("cancel failed too")

            def resolve_foreground_window(self, window: ForegroundWindow | None = None) -> ForegroundWindow:
                events.append("resolve_enter")
                rec_active_during_resolve[0] = rec_active[0]
                in_resolver.set()
                release_resolver.wait(timeout=2.0)
                if fail_resolve[0]:
                    raise RuntimeError("osascript timed out")
                events.append("resolve_exit")
                return ForegroundWindow(42, "Ghostty", "codex [#12345678]", bundle_id="com.mitchellh.ghostty")

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            plat = _SlowResolvePlatform()
            cfg = Config(refiner=RefinerConfig(provider="off"))
            cfg.audio.beep = True
            daemon = VoiceInputDaemon(
                cfg,
                env={},
                runtime_dir=rt,
                platform=plat,
                asr_provider=_FakeAsr("不吞首字"),
                refiner=create_refiner(cfg),
                tracker=_isolated_tracker(rt),
            )
            try:
                # 1. Slow resolve_foreground_window runs AFTER start_recording() has already opened the mic!
                res_box: list[dict[str, object]] = []
                t = threading.Thread(
                    target=lambda: res_box.append(
                        daemon.handle_trigger(
                            "toggle",
                            ForegroundWindow(0, "Ghostty", "", bundle_id="com.mitchellh.ghostty"),
                            from_hotkey=True,
                        )
                    ),
                    daemon=True,
                )
                t.start()
                self.assertTrue(in_resolver.wait(timeout=2.0))
                # While resolver is still blocked, mic capture is ALREADY running and start beep already played!
                self.assertTrue(rec_active[0])
                self.assertTrue(rec_active_during_resolve[0])
                self.assertEqual(
                    events,
                    ["hud:recording", "start_rec:loopback=False", "beep:start", "resolve_enter"],
                )
                release_resolver.set()
                t.join(timeout=2.0)
                self.assertTrue(res_box[0]["ok"])
                self.assertEqual(daemon.state, "recording")
                self.assertEqual(daemon._origin_win.title, "codex [#12345678]")

                # Cancel clean before next subcase
                daemon.handle_trigger("ptt_cancel")
                self.assertFalse(rec_active[0])

                # 2. Loopback mode suppresses start beep so prompt tone is never captured into system loopback
                events.clear()
                in_resolver.clear()
                release_resolver.set()
                daemon._last_trigger_ts = -1e9
                r_loop = daemon.handle_trigger("ptt_start", loopback=True)
                self.assertTrue(r_loop["ok"])
                self.assertEqual(
                    events,
                    ["hud:recording", "start_rec:loopback=True", "resolve_enter", "resolve_exit"],
                )
                daemon.handle_trigger("ptt_cancel")

                # 3. Resolver raises after start_recording -> cancel_recording() cleans up orphan recorder & rolls back
                events.clear()
                fail_resolve[0] = True
                fail_cancel[0] = True  # Even if cancel_recording raises, rollback completes cleanly
                daemon._last_trigger_ts = -1e9
                r_err = daemon.handle_trigger("toggle")
                self.assertFalse(r_err["ok"])
                self.assertIn("osascript timed out", str(r_err["error"]))
                self.assertEqual(daemon.state, "idle")
                self.assertFalse(rec_active[0])
                self.assertEqual(
                    events,
                    [
                        "hud:recording",
                        "start_rec:loopback=False",
                        "beep:start",
                        "resolve_enter",
                        "cancel_rec",
                        "hud:error",
                        "beep:error",
                    ],
                )
            finally:
                daemon.close()

    def test_cli_restart_running_hints_stale_stop_cleanup_and_log_trim(self) -> None:
        import io
        from unittest import mock
        from agent_voice_input import cli as cli_mod

        parser = cli_mod._build_parser()
        r_pre = parser.parse_args(["--config", "/tmp/c.toml", "--asr", "groq", "restart"])
        r_post = parser.parse_args(["restart", "--config", "/tmp/c.toml", "--refiner", "openrouter", "--effort", "low"])
        self.assertEqual((r_pre.subcmd, r_pre.config, r_pre.asr), ("restart", "/tmp/c.toml", "groq"))
        self.assertEqual(
            (r_post.subcmd, r_post.config, r_post.refiner, r_post.effort),
            ("restart", "/tmp/c.toml", "openrouter", "low"),
        )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rt = root / "rt"
            rt.mkdir()
            env = {"HOME": str(root), "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)}

            # 1. `start` when already running: prints reload hint with or without CLI overrides
            buf_plain = io.StringIO()
            with (
                mock.patch.object(
                    cli_mod, "send_control_command", return_value={"ok": True, "pid": 1234, "state": "idle"}
                ),
                mock.patch("sys.stdout", buf_plain),
            ):
                rc1 = cli_mod._cmd_start(parser.parse_args(["start"]), env)
            self.assertEqual(rc1, 0)
            self.assertEqual(
                buf_plain.getvalue().strip(),
                "already running (pid=1234, state=idle) (use 'restart' to reload config)",
            )

            buf_ov = io.StringIO()
            with (
                mock.patch.object(
                    cli_mod, "send_control_command", return_value={"ok": True, "pid": 1234, "state": "idle"}
                ),
                mock.patch("sys.stdout", buf_ov),
            ):
                rc2 = cli_mod._cmd_start(parser.parse_args(["start", "--asr", "groq"]), env)
            self.assertEqual(rc2, 0)
            self.assertEqual(
                buf_ov.getvalue().strip(),
                "already running (pid=1234, state=idle); run 'agent-voice-input restart' to apply new flags/config",
            )

            # 2. `restart` in main(): stops then starts cleanly; bad config fails BEFORE stopping live daemon
            call_seq: list[str] = []
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch.object(cli_mod, "_cmd_stop", side_effect=lambda _e: (call_seq.append("stop") or 0)),
                mock.patch.object(
                    cli_mod,
                    "_cmd_start",
                    side_effect=lambda a, _e, **_kw: (call_seq.append(f"start:{getattr(a, 'asr', None)}") or 0),
                ),
            ):
                rc_restart = cli_mod.main(["restart", "--asr", "dashscope"])
            self.assertEqual(rc_restart, 0)
            self.assertEqual(call_seq, ["stop", "start:dashscope"])

            call_seq.clear()
            err_buf = io.StringIO()
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch.object(cli_mod, "_cmd_stop", side_effect=lambda _e: (call_seq.append("stop") or 0)),
                mock.patch("sys.stderr", err_buf),
            ):
                rc_bad = cli_mod.main(["restart", "--config", str(root / "missing.toml")])
            self.assertEqual(rc_bad, 2)
            self.assertEqual(call_seq, [])

            # 3. `stop` cleans stale daemon.pid and control.sock when process is gone, preserves if PermissionError
            pid_file = rt / "daemon.pid"
            sock_file = rt / "control.sock"
            pid_file.write_text("999991\n", encoding="utf-8")
            sock_file.write_text("stale", encoding="utf-8")

            def fake_kill_dead(pid: int, sig: int) -> None:
                raise ProcessLookupError(3, "No such process")

            with (
                mock.patch.object(cli_mod, "send_control_command", side_effect=ConnectionRefusedError("dead")),
                mock.patch.object(cli_mod.os, "kill", side_effect=fake_kill_dead),
                mock.patch("sys.stdout", io.StringIO()),
            ):
                self.assertEqual(cli_mod._cmd_stop(env), 0)
            self.assertFalse(pid_file.exists())
            self.assertFalse(sock_file.exists())

            # Alive process (PermissionError on kill(pid, 0)) does NOT unlink active files
            pid_file.write_text("999992\n", encoding="utf-8")
            sock_file.write_text("alive", encoding="utf-8")

            def fake_kill_perm(pid: int, sig: int) -> None:
                raise PermissionError(1, "Operation not permitted")

            with (
                mock.patch.object(cli_mod, "send_control_command", return_value={"ok": True}),
                mock.patch.object(cli_mod.os, "kill", side_effect=fake_kill_perm),
                mock.patch("sys.stdout", io.StringIO()),
            ):
                self.assertEqual(cli_mod._cmd_stop(env), 0)
            self.assertTrue(pid_file.exists())
            self.assertTrue(sock_file.exists())

            # 4. `_trim_log_file_if_needed`: <=1 MiB untouched; >1 MiB keeps ~256 KiB line-aligned 0600; symlink ignored
            log_path = rt / "daemon.log"
            small_payload = b"small line\n" * 100
            log_path.write_bytes(small_payload)
            cli_mod._trim_log_file_if_needed(log_path)
            self.assertEqual(log_path.read_bytes(), small_payload)

            lines = [f"line-{i:06d} 中文日志内容校验不截半个UTF8字符\n".encode("utf-8") for i in range(25000)]
            big_payload = b"".join(lines)
            self.assertGreater(len(big_payload), 1_048_576)
            log_path.write_bytes(big_payload)
            os.chmod(log_path, 0o644)
            cli_mod._trim_log_file_if_needed(log_path)

            trimmed = log_path.read_bytes()
            self.assertLessEqual(len(trimmed), 262_144)
            self.assertGreater(len(trimmed), 250_000)
            decoded = trimmed.decode("utf-8")  # Must be valid UTF-8 starting cleanly on a line boundary!
            self.assertTrue(decoded.startswith("line-"))
            self.assertTrue(decoded.endswith("line-024999 中文日志内容校验不截半个UTF8字符\n"))
            self.assertEqual(os.stat(log_path).st_mode & 0o777, 0o600)
            self.assertEqual([p.name for p in rt.iterdir() if ".trim." in p.name], [])

            # Symlink guard: target file > 1 MiB MUST NOT be truncated or replaced via symlink
            target_big = root / "external_target.log"
            target_big.write_bytes(big_payload)
            sym_log = rt / "sym_daemon.log"
            sym_log.symlink_to(target_big)
            cli_mod._trim_log_file_if_needed(sym_log)
            self.assertTrue(sym_log.is_symlink())
            self.assertEqual(target_big.stat().st_size, len(big_payload))

            # 5. Daemon `status` reports shim_ok & redacted shim_error; `_cmd_start` surfaces degraded shim & early-exit log tail
            sec = "sk-super-secret-start-diag-token"
            env_sec = {**env, "DASHSCOPE_API_KEY": sec}
            plat_deg = _FakePlatform()
            daemon_deg = VoiceInputDaemon(
                Config(refiner=RefinerConfig(provider="off")),
                env=env_sec,
                runtime_dir=rt / "rt_status",
                platform=plat_deg,
                asr_provider=_FakeAsr(),
                refiner=create_refiner(Config(refiner=RefinerConfig(provider="off"))),
                tracker=_isolated_tracker(rt),
            )
            try:
                st_ok = daemon_deg.handle_control({"cmd": "status"})
                self.assertTrue(st_ok["shim_ok"])
                self.assertNotIn("shim_error", st_ok)

                plat_deg._shim_disabled = True  # type: ignore[attr-defined]
                plat_deg.last_shim_error = f"Xcode CLT missing token={sec}"  # type: ignore[attr-defined]
                st_deg = daemon_deg.handle_control({"cmd": "status"})
                self.assertFalse(st_deg["shim_ok"])
                self.assertIn("Xcode CLT missing", st_deg["shim_error"])
                self.assertNotIn(sec, st_deg["shim_error"])
            finally:
                daemon_deg.close()

            # `_cmd_start` when already running in degraded shim mode prints WARN to stderr
            warn_out, warn_err = io.StringIO(), io.StringIO()
            with (
                mock.patch.object(
                    cli_mod,
                    "send_control_command",
                    return_value={
                        "ok": True,
                        "pid": 2468,
                        "state": "idle",
                        "shim_ok": False,
                        "shim_error": "Xcode Command Line Tools not installed; run 'xcode-select --install'",
                    },
                ),
                mock.patch("sys.stdout", warn_out),
                mock.patch("sys.stderr", warn_err),
            ):
                self.assertEqual(cli_mod._cmd_start(parser.parse_args(["start"]), env_sec), 0)
            self.assertIn("already running (pid=2468", warn_out.getvalue())
            self.assertIn("xcode-select --install", warn_err.getvalue())

            # `_cmd_start` on early exit prints ONLY newly appended daemon.log lines and redacts secrets
            rt_early = root / "rt_early"
            rt_early.mkdir()
            early_log = rt_early / "daemon.log"
            early_log.write_text("OLD_YESTERDAY_ERROR_MUST_NOT_SHOW\n", encoding="utf-8")

            class _DeadProc:
                pid = 77777
                returncode = 1

                def poll(self) -> int:
                    return 1

            def fake_early_popen(_cmd, *, stdout=None, stderr=None, **_kw):
                assert stdout is not None
                stdout.write(f"RuntimeError: Add-Type C# compile failed with key={sec}\n")
                stdout.flush()
                return _DeadProc()

            early_err = io.StringIO()
            with (
                mock.patch.object(cli_mod, "send_control_command", side_effect=ConnectionRefusedError("down")),
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch.object(cli_mod.subprocess, "Popen", side_effect=fake_early_popen),
                mock.patch("sys.stderr", early_err),
            ):
                rc_early = cli_mod._cmd_start(
                    parser.parse_args(["start"]),
                    {**env_sec, "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt_early)},
                    cfg=Config(refiner=RefinerConfig(provider="off")),
                )
            self.assertEqual(rc_early, 1)
            err_txt = early_err.getvalue()
            self.assertIn("daemon exited early (rc=1)", err_txt)
            self.assertIn("Add-Type C# compile failed", err_txt)
            self.assertNotIn("OLD_YESTERDAY_ERROR_MUST_NOT_SHOW", err_txt)
            self.assertNotIn(sec, err_txt)


if __name__ == "__main__":
    unittest.main()
