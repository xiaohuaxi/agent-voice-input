"""Offline unit tests for explicit ``refiner.provider = 'agy'`` (local AGY ACP zero-tool refiner)."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_voice_input import _remote_probe, cli, prompts  # noqa: E402
from agent_voice_input.config import (  # noqa: E402
    DEFAULT_REFINER_EFFORTS,
    DEFAULT_REFINER_MODELS,
    REFINER_AUTO_PRIORITY,
    VALID_REFINER_PROVIDERS,
    Config,
    ConfigError,
    RefinerConfig,
    explain_pipeline_selection,
    find_agy_bin,
    load_config,
    resolve_credentials,
    select_refiner_provider,
)
from agent_voice_input.daemon_pipeline import PipelineResult, build_pipeline_summary  # noqa: E402
from agent_voice_input.diagnose import (  # noqa: E402
    _TELEMETRY_NUMERIC_FIELDS,
    diagnose_record_flags,
)
from agent_voice_input.http_transport import CancelToken  # noqa: E402
from agent_voice_input.llm_common import AsrResult  # noqa: E402
from agent_voice_input.refiner import (  # noqa: E402
    _AGY_IDLE_TTL_SEC,
    AgyRefiner,
    NoopRefiner,
    _AgyRpcError,
    _agy_child_env,
    _agy_convert_usage,
    _agy_group_alive,
    _agy_match_effort,
    _agy_match_model,
    _agy_select_reject_option_id,
    _compute_agy_context_delta,
    create_refiner,
    format_prior_session_reference_chunk,
    format_session_reference_chunk,
)


FAKE_AGY_SCRIPT = textwrap.dedent(
    """\
    #!{python}
    import json
    import os
    import signal
    import sys
    import time

    log_path = os.environ.get("AGY_TEST_LOG", "")
    state_path = os.environ.get("AGY_TEST_STATE", "")
    scenario = os.environ.get("AGY_TEST_SCENARIO", "normal")

    def _log(entry):
        if not log_path:
            return
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\\n")

    def _load_state():
        if state_path and os.path.exists(state_path):
            with open(state_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        return {{"calls": 0, "prompts": 0, "sess_seq": 0, "sessions": {{}}}}

    def _save_state(st):
        if not state_path:
            return
        with open(state_path, "w", encoding="utf-8") as fh:
            json.dump(st, fh)

    def _send(obj):
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\\n")
        sys.stdout.flush()

    st = _load_state()
    st["calls"] = st.get("calls", 0) + 1
    call_idx = st["calls"]
    _save_state(st)

    _log({{
        "event": "spawn",
        "pid": os.getpid(),
        "call_idx": call_idx,
        "argv": sys.argv[1:],
        "cwd": os.getcwd(),
        "env_keys": sorted(os.environ.keys()),
    }})

    if scenario == "exit_fail_first" and call_idx == 1:
        sys.stderr.write("AGY_ERROR: {{\\"code\\": 503, \\"message\\": \\"transient upstream\\"}}\\n")
        sys.stderr.flush()
        sys.exit(3)

    if scenario == "ping_fail":
        sys.stderr.write("init failed\\n")
        sys.stderr.flush()
        sys.exit(2)

    if scenario == "print_timeout":
        sys.stderr.write("print timeout after 30s with turn in progress\\n")
        sys.stderr.flush()
        sys.exit(1)

    model_options = [
        {{"value": "gemini-3.8-flash", "name": "Gemini 3.8 Flash"}},
        {{"value": "gemini-4-carbon", "name": "Gemini 4 Carbon"}},
        {{"value": "gpt-oss-120b-medium", "name": "Fixed Medium"}},
    ]
    tl_options = [
        {{"value": "low", "name": "Low"}},
        {{"value": "medium", "name": "Medium"}},
        {{"value": "high", "name": "High"}},
        {{"value": "xhigh", "name": "Extra High"}},
    ]
    sessions_cfg = {{}}

    def _cfg_opts(sid):
        cur_model, cur_tl = sessions_cfg.get(sid, ("gemini-4-carbon", "high"))
        opts = [
            {{"id": "model", "currentValue": cur_model, "options": model_options}},
        ]
        if cur_model != "gpt-oss-120b-medium":
            opts.append({{"id": "thought_level", "currentValue": cur_tl, "options": tl_options}})
        return opts

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        msg = json.loads(line)
        mid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {{}}
        _log({{"event": "rpc", "pid": os.getpid(), "call_idx": call_idx, "method": method, "params": params}})

        if method == "initialize":
            init_res = {{"protocolVersion": 1, "serverInfo": {{"name": "fake-agy"}}}}
            if scenario != "no_list_capability":
                init_res["agentCapabilities"] = {{"sessionCapabilities": {{"list": {{}}, "resume": {{}}}}}}
            else:
                init_res["agentCapabilities"] = {{"sessionCapabilities": {{"resume": {{}}}}}}
            _send({{"jsonrpc": "2.0", "id": mid, "result": init_res}})
        elif method == "session/list":
            if scenario == "list_unsupported":
                _send({{"jsonrpc": "2.0", "id": mid, "error": {{"code": -32601, "message": "Method not found"}}}})
                continue
            if scenario == "list_error":
                _send({{"jsonrpc": "2.0", "id": mid, "error": {{"code": -32000, "message": "backend boot failed"}}}})
                continue
            if scenario == "slow_prewarm":
                time.sleep(0.25)
            elif scenario == "hang_prewarm":
                time.sleep(30.0)
            _send({{"jsonrpc": "2.0", "id": mid, "result": {{"sessions": []}}}})
        elif method == "session/new":
            st = _load_state()
            st["sess_seq"] = st.get("sess_seq", 0) + 1
            sseq = st["sess_seq"]
            sid = f"11111111-2222-3333-4444-{{sseq:012d}}"
            st.setdefault("sessions", {{}})[sid] = True
            _save_state(st)
            sessions_cfg[sid] = ("gemini-4-carbon", "high")
            if scenario == "interleaved_other_sid_update":
                _send({{
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {{"sessionId": sid, "update": {{"sessionUpdate": "agent_message_chunk", "content": {{"type": "text", "text": "PRE_PROMPT_STRAY_TEXT"}}}}}},
                }})
            _send({{"jsonrpc": "2.0", "id": mid, "result": {{"sessionId": sid, "configOptions": _cfg_opts(sid)}}}})
        elif method == "session/resume":
            sid = params.get("sessionId", "")
            if scenario == "wrong_resume_sid":
                _send({{"jsonrpc": "2.0", "id": mid, "result": {{"sessionId": "secret-upstream-sid-999", "configOptions": _cfg_opts(sid)}}}})
                continue
            if scenario == "resume_expired" and call_idx == 2:
                _send({{"jsonrpc": "2.0", "id": mid, "error": {{"code": -32602, "message": "conversation not found"}}}})
                continue
            if scenario == "slow_resume_expired" and call_idx == 2:
                time.sleep(0.25)
                _send({{"jsonrpc": "2.0", "id": mid, "error": {{"code": -32602, "message": "conversation not found"}}}})
                continue
            sessions_cfg.setdefault(sid, ("gemini-4-carbon", "high"))
            _send({{"jsonrpc": "2.0", "id": mid, "result": {{"sessionId": sid, "configOptions": _cfg_opts(sid)}}}})
        elif method == "session/set_config_option":
            sid = params.get("sessionId", "")
            cid = params.get("configId")
            val = params.get("value")
            cur_model, cur_tl = sessions_cfg.get(sid, ("gemini-4-carbon", "high"))
            if cid == "model":
                cur_model = val
            elif cid == "thought_level":
                cur_tl = val
            sessions_cfg[sid] = (cur_model, cur_tl)
            _send({{"jsonrpc": "2.0", "id": mid, "result": {{"configOptions": _cfg_opts(sid)}}}})
        elif method == "session/prompt":
            sid = params.get("sessionId", "")
            cur_model, cur_tl = sessions_cfg.get(sid, ("gemini-4-carbon", "high"))
            st = _load_state()
            st["prompts"] = st.get("prompts", 0) + 1
            prompt_idx = st["prompts"]
            _save_state(st)
            text = "".join(p.get("text", "") for p in (params.get("prompt") or []) if isinstance(p, dict))
            if scenario == "slow_hang" or (scenario == "slow_resume_expired" and call_idx == 3):
                time.sleep(30.0)
                _send({{"jsonrpc": "2.0", "id": mid, "result": {{"stopReason": "end_turn"}}}})
                continue
            if scenario == "eof_on_call2" and prompt_idx == 2:
                sys.exit(0)
            if scenario == "forbidden_tool_on_prompt2" and prompt_idx == 2:
                _send({{
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {{"update": {{"sessionUpdate": "tool_call", "toolCallId": "tc_p2", "name": "read_file"}}}},
                }})
                time.sleep(5.0)
                continue
            if scenario == "slow_hang_on_prompt2" and prompt_idx == 2:
                time.sleep(30.0)
                _send({{"jsonrpc": "2.0", "id": mid, "result": {{"stopReason": "end_turn"}}}})
                continue
            if scenario == "partial_stream_then_crash":
                _send({{
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {{"sessionId": sid, "update": {{"sessionUpdate": "agent_message_chunk", "content": {{"type": "text", "text": "秘密合同金额一百万"}}}}}},
                }})
                sys.exit(2)
            if scenario == "rpc_error_with_secret_data":
                _send({{
                    "jsonrpc": "2.0",
                    "id": mid,
                    "error": {{
                        "code": -32000,
                        "message": "failed near: 机密口述草稿内容123",
                        "data": {{"response": "秘密草稿原文", "detail": "internal-dump-xyz"}},
                    }},
                }})
                continue
            if scenario == "agy_error_stderr_secret_msg":
                sys.stderr.write(
                    'AGY_ERROR: {{"code": "RATE_LIMIT", "type": "quota", "message": "回显敏感草稿456"}}\\n'
                )
                sys.stderr.flush()
                sys.exit(3)
            if scenario == "safety_filter_period":
                _send({{
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {{"sessionId": sid, "update": {{"sessionUpdate": "agent_message_chunk", "content": {{"type": "text", "text": "This request was blocked by Gemini's filters."}}}}}},
                }})
                _send({{"jsonrpc": "2.0", "id": mid, "result": {{"stopReason": "end_turn"}}}})
                continue
            if scenario == "perm_reject_option":
                _send({{
                    "jsonrpc": "2.0",
                    "id": 9001,
                    "method": "session/request_permission",
                    "params": {{
                        "toolCall": {{
                            "name": "run_command",
                            "title": "查找合成口述片段",
                            "rawInput": {{"CommandLine": "ls"}},
                        }},
                        "options": [
                            {{"optionId": "allow_once", "kind": "allow_once"}},
                            {{"optionId": "reject_once", "kind": "reject_once"}},
                        ],
                    }},
                }})
                # Wait for client rejection response
                reply = json.loads(sys.stdin.readline())
                _log({{"event": "perm_reply", "reply": reply}})
                _send({{"jsonrpc": "2.0", "id": mid, "result": {{"stopReason": "end_turn", "response": "should be rejected"}}}})
                continue
            if scenario == "perm_no_reject_option":
                _send({{
                    "jsonrpc": "2.0",
                    "id": 9002,
                    "method": "session/request_permission",
                    "params": {{
                        "toolCall": {{"title": "查找合成口述片段"}},
                        "options": [{{"optionId": "allow_once", "kind": "allow_once"}}],
                    }},
                }})
                time.sleep(5.0)
                continue
            if scenario == "forbidden_tool":
                _send({{
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {{"update": {{"sessionUpdate": "tool_call", "toolCallId": "tc1", "name": "read_file", "title": "查找合成口述片段"}}}},
                }})
                time.sleep(5.0)
                continue
            if scenario == "forbidden_tool_title":
                _send({{
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {{"update": {{"sessionUpdate": "tool_call", "toolCallId": "tc2", "title": "查找合成口述片段"}}}},
                }})
                time.sleep(5.0)
                continue
            if scenario == "internal_tool_ok":
                _send({{
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {{"update": {{"sessionUpdate": "tool_call", "toolCallId": "tc_ok", "name": "finish"}}}},
                }})
                _send({{
                    "jsonrpc": "2.0",
                    "id": 9003,
                    "method": "custom/unsupported_ping",
                    "params": {{}},
                }})
                unsupported_reply = json.loads(sys.stdin.readline())
                _log({{"event": "unsupported_reply", "reply": unsupported_reply}})
            if scenario == "safety_filter":
                _send({{
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {{"sessionId": sid, "update": {{"sessionUpdate": "agent_message_chunk", "content": {{"type": "text", "text": "This request was blocked by Gemini's filters"}}}}}},
                }})
                _send({{"jsonrpc": "2.0", "id": mid, "result": {{"stopReason": "end_turn"}}}})
                continue
            if scenario == "refusal_stop":
                _send({{"jsonrpc": "2.0", "id": mid, "result": {{"stopReason": "refusal", "response": "I cannot comply"}}}})
                continue
            if scenario == "max_tokens":
                _send({{"jsonrpc": "2.0", "id": mid, "result": {{"stopReason": "max_tokens", "response": "partial"}}}})
                continue
            if scenario == "custom_free_text_stop_reason":
                _send({{"jsonrpc": "2.0", "id": mid, "result": {{"stopReason": "blocked: 机密口述草稿内容789", "response": "partial"}}}})
                continue
            if scenario == "empty_once" and call_idx == 1:
                _send({{"jsonrpc": "2.0", "id": mid, "result": {{"stopReason": "end_turn"}}}})
                continue

            # Extract draft from <asr_draft_to_clean>
            marker_s = "<asr_draft_to_clean>\\n"
            marker_e = "\\n</asr_draft_to_clean>"
            draft = text
            if marker_s in text and marker_e in text:
                draft = text.split(marker_s, 1)[1].split(marker_e, 1)[0]
            cleaned = f"CLEAN[{{cur_model}}/{{cur_tl}}]: {{draft}}"
            half = max(1, len(cleaned) // 2)
            if scenario == "interleaved_other_sid_update" and prompt_idx == 2:
                _send({{
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {{"sessionId": "11111111-2222-3333-4444-000000000001", "update": {{"sessionUpdate": "agent_message_chunk", "content": {{"type": "text", "text": "LEAKED_FROM_SESSION_A"}}}}}},
                }})
                _send({{
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {{"update": {{"sessionUpdate": "agent_message_chunk", "content": {{"type": "text", "text": "FORGED_MISSING_SID"}}}}}},
                }})
                _send({{
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {{"sessionId": None, "update": {{"sessionUpdate": "agent_message_chunk", "content": {{"type": "text", "text": "FORGED_NULL_SID"}}}}}},
                }})
            if scenario == "thought_chunks":
                _send({{
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {{"sessionId": "other-session-id", "update": {{"sessionUpdate": "agent_thought_chunk", "content": {{"type": "text", "text": "SECRET_OTHER_SID_THOUGHT"}}}}}},
                }})
                _send({{
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {{"sessionId": sid, "update": {{"sessionUpdate": "agent_thought_chunk", "content": {{"type": "text", "text": "秘密思考片段一"}}}}}},
                }})
                _send({{
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {{"sessionId": sid, "update": {{"sessionUpdate": "agent_thought_chunk", "content": {{"type": "text", "text": "secret_thought_2"}}}}}},
                }})
            elif scenario == "thought_bloat":
                _send({{
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {{"sessionId": sid, "update": {{"sessionUpdate": "agent_thought_chunk", "content": {{"type": "text", "text": "x" * 500}}}}}},
                }})
            _send({{
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {{"sessionId": sid, "update": {{"sessionUpdate": "agent_message_chunk", "content": {{"type": "text", "text": cleaned[:half]}}}}}},
            }})
            if scenario == "interleaved_other_sid_update" and prompt_idx == 2:
                _send({{
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {{"sessionId": "11111111-2222-3333-4444-000000000001", "update": {{"sessionUpdate": "agent_message_chunk", "content": {{"type": "text", "text": "LEAKED_MID_CHUNK"}}}}}},
                }})
            _send({{
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {{"sessionId": sid, "update": {{"sessionUpdate": "agent_message_chunk", "content": {{"type": "text", "text": cleaned[half:]}}}}}},
            }})
            if scenario in ("thought_chunks", "thought_bloat"):
                usage = {{"inputTokens": 1200, "cachedReadTokens": 0, "outputTokens": 180}}
            else:
                usage = (
                    {{"inputTokens": 200, "cachedReadTokens": 1200, "outputTokens": 45, "thoughtTokens": 12}}
                    if prompt_idx >= 2
                    else {{"inputTokens": 1500, "cachedReadTokens": 0, "outputTokens": 50, "thoughtTokens": 20}}
                )
            _send({{
                "jsonrpc": "2.0",
                "id": mid,
                "result": {{"stopReason": "end_turn", "usage": usage}},
            }})
    """
)


def _make_fake_agy(directory: Path, name: str = "agy") -> Path:
    script_path = directory / name
    script_path.write_text(FAKE_AGY_SCRIPT.format(python=sys.executable), encoding="utf-8")
    os.chmod(script_path, 0o755)
    return script_path


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestAgyConfigAndContracts(unittest.TestCase):
    def test_provider_tables_api_and_auto_exclusion(self) -> None:
        self.assertIn("agy", VALID_REFINER_PROVIDERS)
        self.assertNotIn("agy", REFINER_AUTO_PRIORITY)
        self.assertEqual(DEFAULT_REFINER_MODELS["agy"], "gemini-3.8-flash")
        self.assertEqual(DEFAULT_REFINER_EFFORTS["agy"], "low")

        cfg = Config(refiner=RefinerConfig(provider="agy"))
        self.assertEqual(cfg.refiner.effective_model("agy"), "gemini-3.8-flash")
        self.assertEqual(cfg.refiner.effective_effort("agy"), "low")
        self.assertEqual(cfg.refiner.effective_api("agy"), "agy-acp")
        self.assertFalse(cfg.refiner.has_explicit_effort("agy"))

        for bad_api in ("chat-completions", "responses"):
            bad_cfg = Config(refiner=RefinerConfig(provider="agy", api=bad_api))
            with self.assertRaises(ConfigError):
                bad_cfg.refiner.effective_api("agy")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_bin = _make_fake_agy(root)
            env = {"HOME": td, "PATH": td, "AGY_BIN": str(fake_bin)}
            creds = resolve_credentials(Config(), env)
            # Even with AGY_BIN and PATH pointing to agy, auto MUST NOT select agy
            self.assertEqual(select_refiner_provider(Config(), creds, env), "off")
            self.assertEqual(select_refiner_provider(cfg, creds, env), "agy")

            # Explicit effort tracking via load_config
            cfg_file = root / "config.toml"
            cfg_file.write_text('[refiner]\nprovider = "agy"\nmodel = "gemini-3.8-flash-medium"\n', encoding="utf-8")
            loaded, _ = load_config(config_path=cfg_file, env=env, home_dir=root, load_default_env_files=False)
            self.assertFalse(loaded.refiner.has_explicit_effort("agy"))
            self.assertEqual(loaded.refiner.effort, "low")

            loaded_exp, _ = load_config(
                config_path=cfg_file,
                cli_overrides={"refiner.effort": "high"},
                env=env,
                home_dir=root,
                load_default_env_files=False,
            )
            self.assertTrue(loaded_exp.refiner.has_explicit_effort("agy"))
            self.assertEqual(loaded_exp.refiner.effort, "high")

    def test_find_agy_bin_resolution_and_explain_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            local_bin = root / ".local" / "bin"
            local_bin.mkdir(parents=True)

            # Empty hermetic env with no HOME -> None
            self.assertIsNone(find_agy_bin({}))
            self.assertIsNone(find_agy_bin({"AGY_BIN": "~/bin/agy"}))
            self.assertIsNone(find_agy_bin({"HOME": td, "AGY_BIN": "~otheruser/agy"}))

            # ~/.local/bin/agy fallback
            local_agy = _make_fake_agy(local_bin)
            self.assertEqual(find_agy_bin({"HOME": td, "PATH": ""}), str(local_agy))

            # PATH takes precedence over ~/.local/bin/agy
            path_agy = _make_fake_agy(bin_dir)
            self.assertEqual(find_agy_bin({"HOME": td, "PATH": str(bin_dir)}), str(path_agy))

            # AGY_BIN takes precedence over PATH
            custom_agy = _make_fake_agy(root, name="custom-agy")
            self.assertEqual(
                find_agy_bin({"HOME": td, "PATH": str(bin_dir), "AGY_BIN": str(custom_agy)}),
                str(custom_agy),
            )
            # Non-existent AGY_BIN falls back to PATH
            self.assertEqual(
                find_agy_bin({"HOME": td, "PATH": str(bin_dir), "AGY_BIN": str(root / "missing")}),
                str(path_agy),
            )

            # Non-hermetic fallback path check (mocking Path.is_file / os.access & which)
            with patch("agent_voice_input.config.is_hermetic_env", return_value=False), patch(
                "agent_voice_input.config.shutil.which", return_value=None
            ):
                self.assertEqual(find_agy_bin({"HOME": td}), str(local_agy))

            # explain_pipeline_selection for ready vs missing agy
            cfg_agy = Config(refiner=RefinerConfig(provider="agy"))
            exp_ok = explain_pipeline_selection(cfg_agy, env={"HOME": td, "PATH": str(bin_dir)})
            self.assertEqual(exp_ok["refiner"]["provider"], "agy")
            self.assertEqual(exp_ok["refiner"]["api"], "agy-acp")
            self.assertEqual(exp_ok["refiner"]["model"], "gemini-3.8-flash")
            self.assertEqual(exp_ok["refiner"]["effort"], "low")
            self.assertTrue(exp_ok["refiner"]["ready"])
            self.assertIn(str(path_agy), exp_ok["refiner"]["reason"])

            exp_miss = explain_pipeline_selection(cfg_agy, env={"HOME": str(root / "empty"), "PATH": ""})
            self.assertFalse(exp_miss["refiner"]["ready"])
            self.assertIn("not found", exp_miss["refiner"]["reason"])

    def test_pure_acp_helpers_model_effort_usage_env_and_reject(self) -> None:
        # Env whitelist strips unrelated secrets and shell injection vars
        child_env = _agy_child_env(
            {
                "HOME": "/tmp/h",
                "PATH": "/usr/bin",
                "AGY_CUSTOM": "1",
                "GEMINI_API_KEY": "gm-key",
                "XDG_CONFIG_HOME": "/tmp/cfg",
                "OPENAI_API_KEY": "sk-secret",
                "DEEPSEEK_API_KEY": "ds-secret",
                "BASH_ENV": "/tmp/evil.sh",
            }
        )
        self.assertIn("HOME", child_env)
        self.assertIn("AGY_CUSTOM", child_env)
        self.assertIn("GEMINI_API_KEY", child_env)
        self.assertIn("XDG_CONFIG_HOME", child_env)
        self.assertNotIn("OPENAI_API_KEY", child_env)
        self.assertNotIn("DEEPSEEK_API_KEY", child_env)
        self.assertNotIn("BASH_ENV", child_env)

        # Reject option selector
        self.assertEqual(
            _agy_select_reject_option_id(
                [
                    {"optionId": "allow_once", "kind": "allow_once"},
                    {"optionId": "rej_all", "kind": "reject_always"},
                    {"optionId": "rej_1", "kind": "reject_once"},
                ]
            ),
            "rej_1",
        )
        self.assertEqual(
            _agy_select_reject_option_id([{"optionId": "rej_all", "kind": "reject_always"}]),
            "rej_all",
        )
        self.assertIsNone(_agy_select_reject_option_id([{"optionId": "allow_once", "kind": "allow_once"}]))
        self.assertIsNone(_agy_select_reject_option_id("not-a-list"))

        # Model & effort matching
        self.assertEqual(_agy_match_model("gemini-3.8-flash", ["gemini-3.8-flash"]), ("gemini-3.8-flash", None))
        self.assertEqual(
            _agy_match_model("gemini-3.8-flash-medium", ["gemini-3.8-flash"]),
            ("gemini-3.8-flash", "medium"),
        )
        with self.assertRaises(_AgyRpcError) as cm_empty:
            _agy_match_model("gemini-3.8-flash", [])
        self.assertEqual(cm_empty.exception.source, "config")
        with self.assertRaises(_AgyRpcError) as cm_unk:
            _agy_match_model("nonexistent-model", ["gemini-3.8-flash"])
        self.assertEqual(cm_unk.exception.source, "config")

        cfg_opts = [
            {
                "id": "thought_level",
                "currentValue": "high",
                "options": [{"value": "low"}, {"value": "medium"}, {"value": "high"}, {"value": "xhigh"}],
            }
        ]
        self.assertEqual(
            _agy_match_effort(
                "gemini-3.8-flash", "none", effort_explicit=True, implied_effort=None, config_options=cfg_opts
            ),
            "low",
        )
        self.assertEqual(
            _agy_match_effort(
                "gemini-3.8-flash", "minimal", effort_explicit=True, implied_effort=None, config_options=cfg_opts
            ),
            "low",
        )
        self.assertEqual(
            _agy_match_effort(
                "gemini-3.8-flash", "max", effort_explicit=True, implied_effort=None, config_options=cfg_opts
            ),
            "xhigh",
        )
        # Model suffix + default non-explicit effort uses suffix cleanly
        self.assertEqual(
            _agy_match_effort(
                "gemini-3.8-flash", "low", effort_explicit=False, implied_effort="medium", config_options=cfg_opts
            ),
            "medium",
        )
        # Conflict between suffix and explicit effort raises config error
        with self.assertRaises(_AgyRpcError) as cm_conf:
            _agy_match_effort(
                "gemini-3.8-flash", "high", effort_explicit=True, implied_effort="low", config_options=cfg_opts
            )
        self.assertEqual(cm_conf.exception.source, "config")
        # Fixed-effort model without thought_level
        self.assertIsNone(
            _agy_match_effort(
                "gpt-oss-120b-medium", "medium", effort_explicit=True, implied_effort=None, config_options=[]
            )
        )
        with self.assertRaises(_AgyRpcError):
            _agy_match_effort(
                "gpt-oss-120b-medium", "high", effort_explicit=True, implied_effort=None, config_options=[]
            )
        # xhigh maps to extra when backend only offers extra
        cfg_extra = [{"id": "thought_level", "options": [{"value": "low"}, {"value": "extra"}]}]
        self.assertEqual(
            _agy_match_effort(
                "gemini-4-carbon", "max", effort_explicit=True, implied_effort=None, config_options=cfg_extra
            ),
            "extra",
        )

        # Usage conversion
        self.assertEqual(_agy_convert_usage(None), (None, None, None, None))
        self.assertEqual(_agy_convert_usage({}), (None, None, None, None))
        self.assertEqual(
            _agy_convert_usage({"inputTokens": 300, "cachedReadTokens": 1200, "outputTokens": 40, "thoughtTokens": 7}),
            (1500, 1200, 40, 7),
        )
        self.assertEqual(
            _agy_convert_usage({"inputTokens": 300, "outputTokens": 40}),
            (300, None, 40, 0),
        )


class TestAgyRefinerProtocolAndSafety(unittest.TestCase):
    def _setup_fake_env(self, td: str, *, scenario: str = "normal") -> tuple[Path, Path, dict[str, str]]:
        root = Path(td)
        rt_dir = root / "runtime"
        fake_agy = _make_fake_agy(root)
        log_file = root / "agy.jsonl"
        state_file = root / "agy_state.json"
        env = {
            "HOME": td,
            "PATH": td,
            "AGY_BIN": str(fake_agy),
            "AGY_TEST_LOG": str(log_file),
            "AGY_TEST_STATE": str(state_file),
            "AGY_TEST_SCENARIO": scenario,
            "OPENAI_API_KEY": "sk-should-be-stripped",
        }
        return rt_dir, log_file, env

    def test_full_acp_lifecycle_resume_delta_eviction_max_turns_and_resume_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rt_dir, log_file, env = self._setup_fake_env(td)
            cfg = Config(refiner=RefinerConfig(provider="agy", max_turns_per_thread=2))
            ref = create_refiner(cfg, env=env, runtime_dir=rt_dir)
            self.assertIsInstance(ref, AgyRefiner)
            # Constructor must not eagerly create workdir or spawn subprocesses
            self.assertFalse((rt_dir / "agy-refiner").exists())

            try:
                # Turn 1: session/new with full context and [avi-refiner] prefix
                r1 = ref.refine(
                    "你好测试第一句",
                    session_key="sess-A",
                    fallback_context="block one context",
                )
                self.assertEqual(r1.used_fallback, "")
                self.assertEqual(r1.provider, "agy")
                self.assertEqual(r1.model, "gemini-3.8-flash")
                self.assertEqual(r1.text, "CLEAN[gemini-3.8-flash/low]: 你好测试第一句")
                self.assertEqual(r1.input_tokens, 1500)
                self.assertEqual(r1.cached_input_tokens, 0)
                self.assertEqual(r1.output_tokens, 50)
                self.assertEqual(r1.reasoning_tokens, 20)

                # Verify 0700 workdir and 0600 avi-refiner.md with zero-tool frontmatter
                workdir = rt_dir / "agy-refiner"
                agent_md = workdir / ".agents" / "agents" / "avi-refiner.md"
                self.assertEqual(os.stat(workdir).st_mode & 0o777, 0o700)
                self.assertEqual(os.stat(agent_md).st_mode & 0o777, 0o600)
                md_txt = agent_md.read_text(encoding="utf-8")
                self.assertIn("tools: []", md_txt)
                self.assertIn("inheritCustomizations: false", md_txt)
                self.assertIn("excludeDefaultComponents: true", md_txt)
                self.assertIn("subagent: false", md_txt)
                self.assertIn(
                    "Do NOT deliberate, analyze rules, or plan answers in internal reasoning",
                    md_txt,
                )

                # Turn 2: same session & unchanged prefix -> session/resume with delta only
                r2 = ref.refine(
                    "第二句继续",
                    session_key="sess-A",
                    fallback_context="block one context",
                )
                self.assertEqual(r2.used_fallback, "")
                self.assertEqual(r2.text, "CLEAN[gemini-3.8-flash/low]: 第二句继续")
                self.assertEqual(r2.input_tokens, 1400)
                self.assertEqual(r2.cached_input_tokens, 1200)
                self.assertEqual(r2.output_tokens, 45)
                self.assertEqual(r2.reasoning_tokens, 12)

                # Turn 3: hits max_turns_per_thread=2 -> opens fresh session/new
                r3 = ref.refine(
                    "第三句轮转",
                    session_key="sess-A",
                    fallback_context="block one context",
                )
                self.assertEqual(r3.used_fallback, "")
                self.assertEqual(r3.text, "CLEAN[gemini-3.8-flash/low]: 第三句轮转")

                # Turn 4: context prefix invalidated -> opens fresh session/new even though turn_count < max_turns
                r4 = ref.refine(
                    "第四句前缀变化",
                    session_key="sess-A",
                    fallback_context="completely different block",
                )
                self.assertEqual(r4.used_fallback, "")
            finally:
                ref.close()

            events = _read_jsonl(log_file)
            spawns = [e for e in events if e["event"] == "spawn"]
            # All 4 turns reuse a single live agy acp worker process!
            self.assertEqual(len(spawns), 1)
            worker_pid = spawns[0]["pid"]
            self.assertEqual(
                spawns[0]["argv"],
                ["acp", "--agent", "avi-refiner", "--include-default-customizations=false"],
            )
            self.assertNotIn("--dangerously-skip-permissions", spawns[0]["argv"])
            self.assertNotIn("--sandbox", spawns[0]["argv"])
            self.assertNotIn("OPENAI_API_KEY", spawns[0]["env_keys"])

            rpcs = [e for e in events if e["event"] == "rpc"]
            self.assertEqual({e["pid"] for e in rpcs}, {worker_pid})
            methods = [e["method"] for e in rpcs]
            # Turn 1: init + new + set_config(model) + set_config(thought_level) + prompt
            # Turn 2 (hot same session): ONLY session/prompt!
            # Turn 3 (max_turns rotation): new + set_config x2 + prompt (zero initialize)
            # Turn 4 (prefix changed): new + set_config x2 + prompt (zero initialize)
            self.assertEqual(
                methods,
                [
                    "initialize",
                    "session/new",
                    "session/set_config_option",
                    "session/set_config_option",
                    "session/prompt",
                    "session/prompt",
                    "session/new",
                    "session/set_config_option",
                    "session/set_config_option",
                    "session/prompt",
                    "session/new",
                    "session/set_config_option",
                    "session/set_config_option",
                    "session/prompt",
                ],
            )
            self.assertEqual(r2.init_ms, 0)
            self.assertEqual(r3.init_ms, 0)
            self.assertEqual(r4.init_ms, 0)

            # Verify prompt payloads: turn 1 has [avi-refiner] + block one; turn 2 keeps [avi-refiner] prefix and omits already-sent block one
            prompts_sent = [e["params"]["prompt"][0]["text"] for e in rpcs if e["method"] == "session/prompt"]
            p1, p2 = prompts_sent[0], prompts_sent[1]
            self.assertTrue(p1.startswith("[avi-refiner]\n"))
            self.assertIn("block one context", p1)
            self.assertIn("<task_reminder>", p1)
            self.assertTrue(p2.startswith("[avi-refiner]\n"))
            self.assertNotIn("block one context", p2)
            self.assertIn("第二句继续", p2)
            self.assertIn("<task_reminder>", p2)

            # Verify mcpServers == [] on every session/new and session/resume
            for e in rpcs:
                if e["method"] in ("session/new", "session/resume"):
                    self.assertEqual(e["params"]["mcpServers"], [])

    def test_resume_expired_falls_back_to_session_new_within_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rt_dir, log_file, env = self._setup_fake_env(td, scenario="resume_expired")
            ref = AgyRefiner(Config(refiner=RefinerConfig(provider="agy")), env=env, runtime_dir=rt_dir)
            try:
                r1 = ref.refine("turn 1", session_key="sess-exp", fallback_context="ctx-1")
                self.assertEqual(r1.used_fallback, "")
                # Evict idle worker to simulate idle TTL expiry before Turn 2 triggers session/resume
                self.assertEqual(ref.evict_connections(), 1)
                # Turn 2 tries session/resume (call 2 fails with -32602), kills worker, then transparently falls back to session/new (call 3)
                r2 = ref.refine("turn 2", session_key="sess-exp", fallback_context="ctx-1")
                self.assertEqual(r2.used_fallback, "")
                self.assertEqual(r2.retries, 0)
                self.assertIn("turn 2", r2.text)
            finally:
                ref.close()

            rpcs = [e for e in _read_jsonl(log_file) if e["event"] == "rpc"]
            self.assertTrue(any(e["call_idx"] == 2 and e["method"] == "session/resume" for e in rpcs))
            self.assertTrue(any(e["call_idx"] == 3 and e["method"] == "session/new" for e in rpcs))
            p3 = next(e["params"]["prompt"][0]["text"] for e in rpcs if e["call_idx"] == 3 and e["method"] == "session/prompt")
            self.assertTrue(p3.startswith("[avi-refiner]\n"))
            self.assertIn("ctx-1", p3)

    def test_model_suffix_and_conflict_handling(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rt_dir, _, env = self._setup_fake_env(td)
            # Suffix without explicit effort -> splits cleanly
            cfg_ok = Config(refiner=RefinerConfig(provider="agy", model="gemini-3.8-flash-medium"))
            ref_ok = AgyRefiner(cfg_ok, env=env, runtime_dir=rt_dir)
            try:
                res = ref_ok.refine("hello suffix")
                self.assertEqual(res.used_fallback, "")
                self.assertEqual(res.model, "gemini-3.8-flash")
                self.assertEqual(res.text, "CLEAN[gemini-3.8-flash/medium]: hello suffix")
            finally:
                ref_ok.close()

            # Suffix conflicts with explicit effort -> non-retryable config fallback
            cfg_bad = Config(refiner=RefinerConfig(provider="agy", model="gemini-3.8-flash-low", effort="high"))
            ref_bad = AgyRefiner(cfg_bad, env=env, runtime_dir=rt_dir)
            try:
                res_bad = ref_bad.refine("hello conflict")
                self.assertEqual(res_bad.used_fallback, "asr_draft")
                self.assertEqual(res_bad.retries, 0)
                self.assertIn("conflicts", res_bad.warning)
            finally:
                ref_bad.close()

    def test_permission_denial_forbidden_tool_internal_tool_and_safety_filters(self) -> None:
        # 1. session/request_permission with reject_once -> sends rejection & falls back without retry or leaking title/name
        with tempfile.TemporaryDirectory() as td:
            rt_dir, log_file, env = self._setup_fake_env(td, scenario="perm_reject_option")
            ref = AgyRefiner(Config(refiner=RefinerConfig(provider="agy", max_retries=2)), env=env, runtime_dir=rt_dir)
            try:
                res = ref.refine("draft text")
                self.assertEqual(res.used_fallback, "asr_draft")
                self.assertEqual(res.text, "draft text")
                self.assertEqual(res.retries, 0)
                self.assertIn("agy ACP permission denied", res.warning)
                self.assertNotIn("查找合成口述片段", res.warning)
                self.assertNotIn("run_command", res.warning)
            finally:
                ref.close()
            replies = [e["reply"] for e in _read_jsonl(log_file) if e["event"] == "perm_reply"]
            self.assertEqual(len(replies), 1)
            self.assertEqual(replies[0]["result"]["outcome"], {"outcome": "selected", "optionId": "reject_once"})

        # 2. session/request_permission without any reject option -> emergency kill + denied fallback without leaking title
        with tempfile.TemporaryDirectory() as td:
            rt_dir, _, env = self._setup_fake_env(td, scenario="perm_no_reject_option")
            ref = AgyRefiner(Config(refiner=RefinerConfig(provider="agy", max_retries=2)), env=env, runtime_dir=rt_dir)
            t0 = time.monotonic()
            try:
                res = ref.refine("draft no reject")
                self.assertEqual(res.used_fallback, "asr_draft")
                self.assertEqual(res.retries, 0)
                self.assertIn("agy ACP permission denied", res.warning)
                self.assertNotIn("查找合成口述片段", res.warning)
                self.assertLess(time.monotonic() - t0, 3.0)
            finally:
                ref.close()

        # 3. Forbidden tool_call (both name+title and title-only) -> immediate kill + tool fallback without leaking title/name
        for scen in ("forbidden_tool", "forbidden_tool_title"):
            with tempfile.TemporaryDirectory() as td:
                rt_dir, _, env = self._setup_fake_env(td, scenario=scen)
                ref = AgyRefiner(Config(refiner=RefinerConfig(provider="agy", max_retries=2)), env=env, runtime_dir=rt_dir)
                t0 = time.monotonic()
                try:
                    res = ref.refine("draft tool")
                    self.assertEqual(res.used_fallback, "asr_draft")
                    self.assertEqual(res.retries, 0)
                    self.assertIn("agy ACP blocked tool call", res.warning)
                    self.assertNotIn("查找合成口述片段", res.warning)
                    self.assertNotIn("read_file", res.warning)
                    self.assertLess(time.monotonic() - t0, 3.0)
                finally:
                    ref.close()

        # 4. Internal tool (finish) allowed & unsupported server RPC gets -32601
        with tempfile.TemporaryDirectory() as td:
            rt_dir, log_file, env = self._setup_fake_env(td, scenario="internal_tool_ok")
            ref = AgyRefiner(Config(refiner=RefinerConfig(provider="agy")), env=env, runtime_dir=rt_dir)
            try:
                res = ref.refine("internal ok")
                self.assertEqual(res.used_fallback, "")
                self.assertIn("internal ok", res.text)
            finally:
                ref.close()
            unsup = [e["reply"] for e in _read_jsonl(log_file) if e["event"] == "unsupported_reply"]
            self.assertEqual(len(unsup), 1)
            self.assertEqual(unsup[0]["error"]["code"], -32601)

        # 5. Safety filter, refusal stopReason, and max_tokens are non-retryable
        for scen in ("safety_filter", "refusal_stop", "max_tokens"):
            with tempfile.TemporaryDirectory() as td:
                rt_dir, _, env = self._setup_fake_env(td, scenario=scen)
                ref = AgyRefiner(Config(refiner=RefinerConfig(provider="agy", max_retries=2)), env=env, runtime_dir=rt_dir)
                try:
                    res = ref.refine(f"draft {scen}")
                    self.assertEqual(res.used_fallback, "asr_draft")
                    self.assertEqual(res.retries, 0)
                finally:
                    ref.close()

    def test_transient_retry_cancel_token_and_close_cleanup(self) -> None:
        # 1. Transient exit on attempt 1 succeeds on retry 1
        with tempfile.TemporaryDirectory() as td:
            rt_dir, _, env = self._setup_fake_env(td, scenario="exit_fail_first")
            ref = AgyRefiner(
                Config(refiner=RefinerConfig(provider="agy", max_retries=2)),
                env=env,
                runtime_dir=rt_dir,
                sleep_fn=lambda _s: None,
            )
            try:
                res = ref.refine("retry works")
                self.assertEqual(res.used_fallback, "")
                self.assertEqual(res.retries, 1)
                self.assertIn("retry works", res.text)
            finally:
                ref.close()

        # 2. Empty response on attempt 1 retries and succeeds
        with tempfile.TemporaryDirectory() as td:
            rt_dir, _, env = self._setup_fake_env(td, scenario="empty_once")
            ref = AgyRefiner(
                Config(refiner=RefinerConfig(provider="agy", max_retries=2)),
                env=env,
                runtime_dir=rt_dir,
                sleep_fn=lambda _s: None,
            )
            try:
                res = ref.refine("empty retry")
                self.assertEqual(res.used_fallback, "")
                self.assertEqual(res.retries, 1)
            finally:
                ref.close()

        # 3. CancelToken aborts hanging agy process group quickly
        with tempfile.TemporaryDirectory() as td:
            rt_dir, _, env = self._setup_fake_env(td, scenario="slow_hang")
            ref = AgyRefiner(Config(refiner=RefinerConfig(provider="agy", timeout_sec=15.0)), env=env, runtime_dir=rt_dir)
            tok = CancelToken()

            def _trigger() -> None:
                time.sleep(0.15)
                tok.cancel("user_cancelled")

            th = threading.Thread(target=_trigger, daemon=True)
            t0 = time.monotonic()
            th.start()
            try:
                res = ref.refine("will cancel", cancel_token=tok)
                elapsed = time.monotonic() - t0
                self.assertEqual(res.used_fallback, "user_cancelled")
                self.assertLess(elapsed, 3.0)
                self.assertEqual(len(ref._active_procs), 0)
            finally:
                th.join(timeout=1.0)
                ref.close()

        # 4. close() aborts in-flight call and leaves no active processes
        with tempfile.TemporaryDirectory() as td:
            rt_dir, _, env = self._setup_fake_env(td, scenario="slow_hang")
            ref = AgyRefiner(Config(refiner=RefinerConfig(provider="agy", timeout_sec=15.0)), env=env, runtime_dir=rt_dir)
            out_box: list = []

            def _worker() -> None:
                out_box.append(ref.refine("closing soon"))

            wt = threading.Thread(target=_worker, daemon=True)
            wt.start()
            time.sleep(0.15)
            ref.close()
            wt.join(timeout=3.0)
            self.assertFalse(wt.is_alive())
            self.assertEqual(len(ref._active_procs), 0)
            self.assertEqual(len(out_box), 1)
            self.assertEqual(out_box[0].used_fallback, "asr_draft")

    def test_edge_cases_missing_cli_symlink_agent_and_single_hop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rt_dir = Path(td) / "rt"
            # Missing CLI falls back safely
            ref_miss = AgyRefiner(
                Config(refiner=RefinerConfig(provider="agy")),
                env={"HOME": td, "PATH": ""},
                runtime_dir=rt_dir,
            )
            res_miss = ref_miss.refine("no cli")
            self.assertEqual(res_miss.used_fallback, "asr_draft")
            self.assertIn("not found", res_miss.warning)

            # Single-hop passthrough & empty draft return NoopRefiner result
            sh = AsrResult(text="already refined", provider="gemini", model="m", latency_ms=10, is_single_hop_refined=True)
            self.assertEqual(ref_miss.refine(sh).used_fallback, "single_hop_passthrough")
            self.assertEqual(ref_miss.refine("").text, "")

            # Symlink avi-refiner.md is rejected safely
            _, _, env = self._setup_fake_env(td)
            agents_dir = rt_dir / "agy-refiner" / ".agents" / "agents"
            agents_dir.mkdir(parents=True)
            os.chmod(rt_dir, 0o700)
            os.chmod(rt_dir / "agy-refiner", 0o700)
            os.chmod(rt_dir / "agy-refiner" / ".agents", 0o700)
            os.chmod(agents_dir, 0o700)
            (agents_dir / "avi-refiner.md").symlink_to(Path(td) / "target.md")
            ref_sym = AgyRefiner(Config(refiner=RefinerConfig(provider="agy")), env=env, runtime_dir=rt_dir)
            res_sym = ref_sym.refine("symlink check")
            self.assertEqual(res_sym.used_fallback, "asr_draft")
            self.assertIn("unsafe agy agent file", res_sym.warning)

    def test_remote_probe_filters_avi_refiner_sessions_and_prompt_rules(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p_direct = root / "direct.jsonl"
            p_direct.write_text(
                json.dumps({"source": "USER_EXPLICIT", "content": "[avi-refiner]\nYou are a stateless..."}) + "\n",
                encoding="utf-8",
            )
            p_wrapped = root / "wrapped.jsonl"
            p_wrapped.write_text(
                json.dumps({"source": "USER_EXPLICIT", "content": "<USER_REQUEST>\n[avi-refiner]\n..."}) + "\n",
                encoding="utf-8",
            )
            p_user = root / "user.jsonl"
            p_user.write_text(
                json.dumps({"source": "USER_EXPLICIT", "content": "<USER_REQUEST>\nFix the bug"}) + "\n",
                encoding="utf-8",
            )
            self.assertTrue(_remote_probe._is_agy_subagent(p_direct))
            self.assertTrue(_remote_probe._is_agy_subagent(p_wrapped))
            self.assertFalse(_remote_probe._is_agy_subagent(p_user))

            # Preceding SYSTEM / CHECKPOINT lines before first USER_EXPLICIT
            p_prefixed_sub = root / "prefixed_sub.jsonl"
            p_prefixed_sub.write_text(
                "\n".join(
                    [
                        json.dumps({"source": "SYSTEM", "type": "CHECKPOINT", "content": "checkpoint 0"}),
                        json.dumps({"source": "SYSTEM", "type": "SYSTEM_MESSAGE", "content": "normal boot info"}),
                        json.dumps({"source": "USER_EXPLICIT", "content": "<USER_REQUEST>\n[avi-refiner]\nhello"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            p_prefixed_user = root / "prefixed_user.jsonl"
            p_prefixed_user.write_text(
                "\n".join(
                    [
                        json.dumps({"source": "SYSTEM", "type": "CHECKPOINT", "content": "checkpoint 0"}),
                        json.dumps({"source": "USER_EXPLICIT", "content": "<USER_REQUEST>\nReal user question"}),
                        json.dumps({"source": "USER_EXPLICIT", "content": "[avi-refiner]\nlater quote"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            self.assertTrue(_remote_probe._is_agy_subagent(p_prefixed_sub))
            self.assertFalse(_remote_probe._is_agy_subagent(p_prefixed_user))

            # Oversized transcript_full.jsonl line (> 8192 bytes) after CHECKPOINT
            p_full_sub = root / "transcript_full_sub.jsonl"
            p_full_sub.write_text(
                json.dumps({"source": "SYSTEM", "type": "CHECKPOINT", "content": "x" * 10000})
                + "\n"
                + json.dumps({"source": "USER_EXPLICIT", "content": "[avi-refiner]\n" + ("ctx " * 5000)})
                + "\n",
                encoding="utf-8",
            )
            p_full_user = root / "transcript_full_user.jsonl"
            p_full_user.write_text(
                json.dumps({"source": "SYSTEM", "type": "CHECKPOINT", "content": "ckpt"})
                + "\n"
                + json.dumps({"source": "USER_EXPLICIT", "content": "Normal user prompt " + ("z" * 12000)})
                + "\n",
                encoding="utf-8",
            )
            self.assertTrue(_remote_probe._is_agy_subagent(p_full_sub))
            self.assertFalse(_remote_probe._is_agy_subagent(p_full_user))

            # 9 KB record with `content` BEFORE `source` (reversed field order must not be truncated at 8 KB)
            p_reordered_sub = root / "reordered_9kb_sub.jsonl"
            p_reordered_sub.write_text(
                json.dumps({"content": "[avi-refiner]\n" + ("a" * 9200), "source": "USER_EXPLICIT"}) + "\n",
                encoding="utf-8",
            )
            p_reordered_user = root / "reordered_9kb_user.jsonl"
            p_reordered_user.write_text(
                json.dumps({"content": "Normal dictation " + ("b" * 9200), "source": "USER_EXPLICIT"}) + "\n",
                encoding="utf-8",
            )
            self.assertTrue(_remote_probe._is_agy_subagent(p_reordered_sub))
            self.assertFalse(_remote_probe._is_agy_subagent(p_reordered_user))

        # Prompt anti-over-editing and zero-deliberation constraints present in both modes & version-cue brand repair guidance in agent mode
        for prompt_txt in (prompts.REFINER_SYSTEM_PROMPT, prompts.REFINER_GENERAL_SYSTEM_PROMPT):
            self.assertIn("NEVER add unstated parenthetical alternatives", prompt_txt)
            self.assertIn("NEVER upgrade parallel or either-or items", prompt_txt)
            self.assertIn("Zero deliberation: do NOT think step-by-step", prompt_txt)
            self.assertIn("emit the final cleaned text immediately in one pass.", prompt_txt)
        self.assertIn("Kimi 3.8 Flash -> Gemini 3.8 Flash", prompts.REFINER_SYSTEM_PROMPT)
        self.assertIn("Zero deliberation: do NOT spend internal reasoning", prompts.REFINER_TASK_REMINDER)
        self.assertIn("output ONLY the cleaned text immediately.</task_reminder>", prompts.REFINER_TASK_REMINDER)

    def test_hardening_isolation_eof_clear_privacy_deadline_refusal_and_relpath(self) -> None:
        # 1. Empty session_key reuses hot worker PID but never shares or stores a stateful conversation thread
        with tempfile.TemporaryDirectory() as td:
            rt_dir, log_file, env = self._setup_fake_env(td)
            ref = AgyRefiner(Config(refiner=RefinerConfig(provider="agy")), env=env, runtime_dir=rt_dir)
            try:
                r1 = ref.refine("邮件正文一", session_key="", mode="general", workspace_cwd="/same/dir")
                r2 = ref.refine("聊天正文二", session_key="", mode="general", workspace_cwd="/same/dir")
                self.assertEqual(r1.used_fallback, "")
                self.assertEqual(r2.used_fallback, "")
                self.assertEqual(len(ref._threads), 0)
            finally:
                ref.close()
            events = _read_jsonl(log_file)
            spawns = [e for e in events if e["event"] == "spawn"]
            self.assertEqual(len(spawns), 1)
            rpcs = [e for e in events if e["event"] == "rpc"]
            self.assertEqual({e["pid"] for e in rpcs}, {spawns[0]["pid"]})
            self.assertEqual([e["method"] for e in rpcs if e["method"] in ("session/new", "session/resume")], ["session/new", "session/new"])

        # 2. EOF failure on turn 2 kills worker & clears conversation_id so turn 3 spawns fresh PID with session/new
        with tempfile.TemporaryDirectory() as td:
            rt_dir, log_file, env = self._setup_fake_env(td, scenario="eof_on_call2")
            ref = AgyRefiner(Config(refiner=RefinerConfig(provider="agy", max_retries=0)), env=env, runtime_dir=rt_dir)
            try:
                r1 = ref.refine("turn 1", session_key="sess-eof", fallback_context="ctx-a")
                self.assertEqual(r1.used_fallback, "")
                r2 = ref.refine("turn 2", session_key="sess-eof", fallback_context="ctx-a")
                self.assertEqual(r2.used_fallback, "asr_draft")
                st_eof = ref._threads[("sess-eof", "agent")]
                self.assertEqual(st_eof.conversation_id, "")
                self.assertEqual(st_eof.sent_blocks, ())
                self.assertIsNone(ref._worker)
                r3 = ref.refine("turn 3", session_key="sess-eof", fallback_context="ctx-a")
                self.assertEqual(r3.used_fallback, "")
            finally:
                ref.close()
            events = _read_jsonl(log_file)
            spawns = [e for e in events if e["event"] == "spawn"]
            self.assertEqual(len(spawns), 2)
            self.assertNotEqual(spawns[0]["pid"], spawns[1]["pid"])
            sess_methods = [
                (e["call_idx"], e["method"])
                for e in events
                if e["event"] == "rpc" and e["method"] in ("session/new", "session/resume")
            ]
            self.assertEqual(sess_methods, [(1, "session/new"), (2, "session/new")])

        # 3. Privacy: partial streamed dictation text & error.data never leak into warning
        with tempfile.TemporaryDirectory() as td:
            rt_dir, _, env = self._setup_fake_env(td, scenario="partial_stream_then_crash")
            ref = AgyRefiner(Config(refiner=RefinerConfig(provider="agy", max_retries=0)), env=env, runtime_dir=rt_dir)
            try:
                res = ref.refine("原口述文本")
                self.assertEqual(res.used_fallback, "asr_draft")
                self.assertIn("exited 2", res.warning)
                self.assertNotIn("秘密合同", res.warning)
                self.assertNotIn("一百万", res.warning)
            finally:
                ref.close()

        with tempfile.TemporaryDirectory() as td:
            rt_dir, _, env = self._setup_fake_env(td, scenario="rpc_error_with_secret_data")
            ref = AgyRefiner(Config(refiner=RefinerConfig(provider="agy", max_retries=0)), env=env, runtime_dir=rt_dir)
            try:
                res = ref.refine("原口述文本")
                self.assertEqual(res.used_fallback, "asr_draft")
                self.assertIn("session/prompt", res.warning)
                self.assertIn("code=-32000", res.warning)
                self.assertNotIn("机密口述草稿内容123", res.warning)
                self.assertNotIn("秘密草稿原文", res.warning)
                self.assertNotIn("internal-dump-xyz", res.warning)
            finally:
                ref.close()

        with tempfile.TemporaryDirectory() as td:
            rt_dir, _, env = self._setup_fake_env(td, scenario="agy_error_stderr_secret_msg")
            ref = AgyRefiner(Config(refiner=RefinerConfig(provider="agy", max_retries=0)), env=env, runtime_dir=rt_dir)
            try:
                res = ref.refine("原口述文本")
                self.assertEqual(res.used_fallback, "asr_draft")
                self.assertIn("code=RATE_LIMIT", res.warning)
                self.assertIn("type=quota", res.warning)
                self.assertNotIn("回显敏感草稿456", res.warning)
            finally:
                ref.close()

        # 3b. Post-attempt cancellation never commits discarded conversation_id back into _threads
        with tempfile.TemporaryDirectory() as td:
            rt_dir, _, env = self._setup_fake_env(td)
            ref = AgyRefiner(Config(refiner=RefinerConfig(provider="agy", max_retries=0)), env=env, runtime_dir=rt_dir)
            tok = CancelToken()
            orig_run = ref._run_acp_session

            def _run_then_cancel(*args, **kwargs):
                out = orig_run(*args, **kwargs)
                # Simulate cancellation arriving right after _attempt succeeds, before retry loop returns
                tok.cancel("post_attempt_cancelled")
                return out

            try:
                with patch.object(ref, "_run_acp_session", side_effect=_run_then_cancel):
                    res_pc = ref.refine("discarded turn", session_key="sess-post-cancel", cancel_token=tok)
                self.assertEqual(res_pc.used_fallback, "post_attempt_cancelled")
                self.assertEqual(ref._threads[("sess-post-cancel", "agent")].conversation_id, "")
            finally:
                ref.close()

        # 4. Single absolute deadline bounds both session lock wait and resume->new fallback
        with tempfile.TemporaryDirectory() as td:
            rt_dir, _, env = self._setup_fake_env(td)
            ref = AgyRefiner(Config(refiner=RefinerConfig(provider="agy", timeout_sec=0.15, max_retries=0)), env=env, runtime_dir=rt_dir)
            st_locked = ref._get_thread_state(("sess-lock", "agent"))
            st_locked.lock.acquire()
            t0 = time.monotonic()
            try:
                res_lock = ref.refine("locked draft", session_key="sess-lock")
                elapsed = time.monotonic() - t0
                self.assertEqual(res_lock.used_fallback, "asr_draft")
                self.assertIn("session lock", res_lock.warning)
                self.assertLess(elapsed, 1.0)
            finally:
                st_locked.lock.release()
                ref.close()

        with tempfile.TemporaryDirectory() as td:
            rt_dir, _, env = self._setup_fake_env(td, scenario="slow_resume_expired")
            ref = AgyRefiner(Config(refiner=RefinerConfig(provider="agy", timeout_sec=0.5, max_retries=0)), env=env, runtime_dir=rt_dir)
            try:
                r1 = ref.refine("seed", session_key="sess-dl", fallback_context="ctx")
                self.assertEqual(r1.used_fallback, "")
                self.assertEqual(ref.evict_connections(), 1)
                t0 = time.monotonic()
                r2 = ref.refine("fallback timeout", session_key="sess-dl", fallback_context="ctx")
                elapsed = time.monotonic() - t0
                self.assertEqual(r2.used_fallback, "asr_draft")
                self.assertLess(elapsed, 1.5)
            finally:
                ref.close()

        # 5. Legitimate end_turn dictation mentioning 'stopReason=safety', refusal phrases, or quoting Gemini filter notice passes cleanly
        with tempfile.TemporaryDirectory() as td:
            rt_dir, _, env = self._setup_fake_env(td)
            ref = AgyRefiner(Config(refiner=RefinerConfig(provider="agy")), env=env, runtime_dir=rt_dir)
            try:
                res_safe = ref.refine("日志里写了 stopReason=safety，而且模型说 I cannot share internal reasoning。")
                self.assertEqual(res_safe.used_fallback, "")
                self.assertIn("stopReason=safety", res_safe.text)

                res_quote = ref.refine("刚才报错提示 This request was blocked by Gemini's filters 帮我排查一下")
                self.assertEqual(res_quote.used_fallback, "")
                self.assertIn("This request was blocked by Gemini's filters", res_quote.text)
            finally:
                ref.close()

        # Exact filter notice (with trailing period) still fails closed as filtered
        with tempfile.TemporaryDirectory() as td:
            rt_dir, _, env = self._setup_fake_env(td, scenario="safety_filter_period")
            ref = AgyRefiner(Config(refiner=RefinerConfig(provider="agy", max_retries=2)), env=env, runtime_dir=rt_dir)
            try:
                res_fp = ref.refine("draft filter period")
                self.assertEqual(res_fp.used_fallback, "asr_draft")
                self.assertEqual(res_fp.retries, 0)
            finally:
                ref.close()

        # 6. find_agy_bin returns absolute path for relative AGY_BIN and relative PATH entries
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rel_dir = root / "rel_bin"
            rel_dir.mkdir()
            fake_bin = _make_fake_agy(rel_dir)
            prev_cwd = os.getcwd()
            os.chdir(td)
            try:
                found_rel_env = find_agy_bin({"HOME": td, "PATH": "", "AGY_BIN": "./rel_bin/agy"})
                found_rel_path = find_agy_bin({"HOME": td, "PATH": "./rel_bin"})
                self.assertTrue(os.path.isabs(found_rel_env or ""))
                self.assertTrue(os.path.isabs(found_rel_path or ""))
                self.assertEqual(os.path.realpath(found_rel_env or ""), os.path.realpath(str(fake_bin)))
                self.assertEqual(os.path.realpath(found_rel_path or ""), os.path.realpath(str(fake_bin)))

                # Verify AgyRefiner works when cwd switches to workdir using relative AGY_BIN
                rt_dir = root / "rt"
                ref = AgyRefiner(
                    Config(refiner=RefinerConfig(provider="agy")),
                    env={"HOME": td, "PATH": "", "AGY_BIN": "./rel_bin/agy"},
                    runtime_dir=rt_dir,
                )
                try:
                    res = ref.refine("relative bin works")
                    self.assertEqual(res.used_fallback, "")
                    self.assertIn("relative bin works", res.text)
                finally:
                    ref.close()
            finally:
                os.chdir(prev_cwd)

    def test_idle_worker_reuse_ttl_eviction_power_resume_and_failure_kill(self) -> None:
        self.assertEqual(_AGY_IDLE_TTL_SEC, 3600.0)
        default_ref = AgyRefiner(Config(refiner=RefinerConfig(provider="agy")), env={})
        try:
            self.assertEqual(default_ref._idle_ttl_sec, 3600.0)
        finally:
            default_ref.close()

        # 1. Same session_key across 3 consecutive turns: identical PID, Turns 2 & 3 send ONLY session/prompt
        with tempfile.TemporaryDirectory() as td:
            rt_dir, log_file, env = self._setup_fake_env(td)
            ref = AgyRefiner(
                Config(refiner=RefinerConfig(provider="agy", max_turns_per_thread=5)),
                env=env,
                runtime_dir=rt_dir,
                idle_ttl_sec=30.0,
            )
            try:
                r1 = ref.refine("第一轮", session_key="sess-hot", fallback_context="shared ctx")
                r2 = ref.refine("第二轮", session_key="sess-hot", fallback_context="shared ctx")
                r3 = ref.refine("第三轮", session_key="sess-hot", fallback_context="shared ctx")
                self.assertEqual((r1.used_fallback, r2.used_fallback, r3.used_fallback), ("", "", ""))
                self.assertEqual((r2.init_ms, r3.init_ms), (0, 0))
                self.assertIsNotNone(ref._worker)
                live_pid = ref._worker.proc.pid  # type: ignore[union-attr]
                # probe_ping runs completely isolated without disturbing or replacing the warm worker
                st_ping, _, _ = ref.probe_ping()
                self.assertEqual(st_ping, "OK")
                self.assertIsNotNone(ref._worker)
                self.assertEqual(ref._worker.proc.pid, live_pid)  # type: ignore[union-attr]
            finally:
                ref.close()
            self.assertEqual(len(ref._active_procs), 0)
            self.assertIsNone(ref._worker)
            self.assertFalse(_agy_group_alive(live_pid))

            events = _read_jsonl(log_file)
            spawns = [e for e in events if e["event"] == "spawn"]
            # Spawn 1 = reusable worker (3 turns); Spawn 2 = isolated probe_ping
            self.assertEqual(len(spawns), 2)
            rpcs_w = [e["method"] for e in events if e["event"] == "rpc" and e["pid"] == live_pid]
            self.assertEqual(
                rpcs_w,
                [
                    "initialize",
                    "session/new",
                    "session/set_config_option",
                    "session/set_config_option",
                    "session/prompt",
                    "session/prompt",
                    "session/prompt",
                ],
            )
            rpcs_ping = [e["method"] for e in events if e["event"] == "rpc" and e["pid"] == spawns[1]["pid"]]
            self.assertEqual(rpcs_ping, ["initialize"])

        # 2. Idle TTL expiry automatically reaps old PID; next turn spawns new PID and seamlessly session/resumes same conversation_id
        with tempfile.TemporaryDirectory() as td:
            rt_dir, log_file, env = self._setup_fake_env(td)
            ref = AgyRefiner(
                Config(refiner=RefinerConfig(provider="agy")),
                env=env,
                runtime_dir=rt_dir,
                idle_ttl_sec=0.15,
            )
            try:
                r1 = ref.refine("idle seed", session_key="sess-ttl", fallback_context="ttl ctx")
                self.assertEqual(r1.used_fallback, "")
                cid1 = ref._threads[("sess-ttl", "agent")].conversation_id
                self.assertTrue(cid1)
                pid1 = ref._worker.proc.pid  # type: ignore[union-attr]

                # Wait for idle reaper timer to reap the worker
                dl = time.monotonic() + 2.0
                while time.monotonic() < dl and (ref._worker is not None or len(ref._active_procs) > 0):
                    time.sleep(0.02)
                self.assertIsNone(ref._worker)
                self.assertEqual(len(ref._active_procs), 0)
                self.assertFalse(_agy_group_alive(pid1))

                # Turn 2 spawns a fresh PID and resumes cid1 via session/resume
                r2 = ref.refine("after idle", session_key="sess-ttl", fallback_context="ttl ctx")
                self.assertEqual(r2.used_fallback, "")
                pid2 = ref._worker.proc.pid  # type: ignore[union-attr]
                self.assertNotEqual(pid1, pid2)
                self.assertEqual(ref._threads[("sess-ttl", "agent")].conversation_id, cid1)
            finally:
                ref.close()
            self.assertEqual(len(ref._active_procs), 0)

            events = _read_jsonl(log_file)
            pid2_rpcs = [e for e in events if e["event"] == "rpc" and e["pid"] == pid2]
            self.assertEqual(
                [e["method"] for e in pid2_rpcs],
                [
                    "initialize",
                    "session/resume",
                    "session/set_config_option",
                    "session/set_config_option",
                    "session/prompt",
                ],
            )
            resume_rpc = next(e for e in pid2_rpcs if e["method"] == "session/resume")
            self.assertEqual(resume_rpc["params"]["sessionId"], cid1)

        # 3. Power-resume hook (evict_connections) immediately stops idle worker and returns count
        with tempfile.TemporaryDirectory() as td:
            rt_dir, log_file, env = self._setup_fake_env(td)
            ref = AgyRefiner(
                Config(refiner=RefinerConfig(provider="agy")),
                env=env,
                runtime_dir=rt_dir,
                idle_ttl_sec=60.0,
            )
            try:
                self.assertEqual(ref.evict_connections(), 0)
                r1 = ref.refine("before sleep", session_key="sess-wake", fallback_context="wake ctx")
                self.assertEqual(r1.used_fallback, "")
                pid_before = ref._worker.proc.pid  # type: ignore[union-attr]
                self.assertEqual(ref.evict_connections(), 1)
                self.assertEqual(ref.evict_connections(), 0)
                self.assertIsNone(ref._worker)
                self.assertEqual(len(ref._active_procs), 0)
                self.assertFalse(_agy_group_alive(pid_before))
                # Next turn spawns a fresh worker and resumes seamlessly
                r2 = ref.refine("after wake", session_key="sess-wake", fallback_context="wake ctx")
                self.assertEqual(r2.used_fallback, "")
                self.assertNotEqual(ref._worker.proc.pid, pid_before)  # type: ignore[union-attr]
            finally:
                ref.close()

        # 4. Forbidden tool / cancel / timeout on turn 2 immediately kills warm worker, clears thread_state, and turn 3 starts fresh
        for scen, turn2_timeout_s, use_cancel in (
            ("forbidden_tool_on_prompt2", 10.0, False),
            ("slow_hang_on_prompt2", 0.25, False),
            ("slow_hang_on_prompt2", 10.0, True),
        ):
            with tempfile.TemporaryDirectory() as td:
                rt_dir, log_file, env = self._setup_fake_env(td, scenario=scen)
                ref = AgyRefiner(
                    Config(refiner=RefinerConfig(provider="agy", timeout_sec=10.0, max_retries=0)),
                    env=env,
                    runtime_dir=rt_dir,
                    idle_ttl_sec=60.0,
                )
                try:
                    r1 = ref.refine("turn 1 ok", session_key="sess-fail", fallback_context="ctx-f")
                    self.assertEqual(r1.used_fallback, "")
                    pid_old = ref._worker.proc.pid  # type: ignore[union-attr]

                    tok = CancelToken() if use_cancel else None
                    th = None
                    if tok is not None:
                        th = threading.Thread(target=lambda: (time.sleep(0.1), tok.cancel("user_cancelled")), daemon=True)
                        th.start()
                    ref.cfg.refiner.timeout_sec = turn2_timeout_s
                    try:
                        r2 = ref.refine("turn 2 fail", session_key="sess-fail", fallback_context="ctx-f", cancel_token=tok)
                    finally:
                        ref.cfg.refiner.timeout_sec = 10.0
                        if th is not None:
                            th.join(timeout=1.0)
                    self.assertNotEqual(r2.used_fallback, "")
                    self.assertIsNone(ref._worker)
                    self.assertEqual(len(ref._active_procs), 0)
                    self.assertFalse(_agy_group_alive(pid_old))
                    self.assertEqual(ref._threads[("sess-fail", "agent")].conversation_id, "")

                    # Turn 3 starts fresh on a brand-new PID with session/new
                    r3 = ref.refine("turn 3 fresh", session_key="sess-fail", fallback_context="ctx-f")
                    self.assertEqual(r3.used_fallback, "")
                    pid_new = ref._worker.proc.pid  # type: ignore[union-attr]
                    self.assertNotEqual(pid_old, pid_new)
                finally:
                    ref.close()
                events = _read_jsonl(log_file)
                new_rpcs = [e["method"] for e in events if e["event"] == "rpc" and e["pid"] == pid_new]
                self.assertIn("session/new", new_rpcs)
                self.assertNotIn("session/resume", new_rpcs)

    def test_review_hardening_session_id_filter_timer_error_sanitized_errors_and_deadline(self) -> None:
        # 1. Interleaved session/update chunks from another sessionId or before session/prompt never contaminate output
        with tempfile.TemporaryDirectory() as td:
            rt_dir, _, env = self._setup_fake_env(td, scenario="interleaved_other_sid_update")
            ref = AgyRefiner(Config(refiner=RefinerConfig(provider="agy")), env=env, runtime_dir=rt_dir)
            try:
                ra = ref.refine("会话A正文", session_key="sess-A", fallback_context="ctx-A")
                rb = ref.refine("会话B正文", session_key="sess-B", fallback_context="ctx-B")
                self.assertEqual(ra.used_fallback, "")
                self.assertEqual(ra.text, "CLEAN[gemini-3.8-flash/low]: 会话A正文")
                self.assertEqual(rb.used_fallback, "")
                self.assertEqual(rb.text, "CLEAN[gemini-3.8-flash/low]: 会话B正文")
                self.assertNotIn("PRE_PROMPT_STRAY_TEXT", ra.text)
                self.assertNotIn("PRE_PROMPT_STRAY_TEXT", rb.text)
                self.assertNotIn("LEAKED_FROM_SESSION_A", rb.text)
                self.assertNotIn("LEAKED_MID_CHUNK", rb.text)
                self.assertNotIn("FORGED_MISSING_SID", rb.text)
                self.assertNotIn("FORGED_NULL_SID", rb.text)
            finally:
                ref.close()

        # 2. Timer.start() failure synchronously reaps worker, releases both locks, and preserves the valid turn result
        with tempfile.TemporaryDirectory() as td:
            rt_dir, _, env = self._setup_fake_env(td)
            ref = AgyRefiner(
                Config(refiner=RefinerConfig(provider="agy")),
                env=env,
                runtime_dir=rt_dir,
                idle_ttl_sec=60.0,
            )
            try:
                with patch.object(threading.Timer, "start", side_effect=RuntimeError("can't start new thread")):
                    r1 = ref.refine("定时器失败也返回结果", session_key="sess-timer", fallback_context="ctx-t")
                self.assertEqual(r1.used_fallback, "")
                self.assertEqual(r1.text, "CLEAN[gemini-3.8-flash/low]: 定时器失败也返回结果")
                self.assertIsNone(ref._worker)
                self.assertEqual(len(ref._active_procs), 0)
                # Locks must be released: a follow-up refine on the same session_key succeeds immediately without deadlocking
                r2 = ref.refine("后续轮次不死锁", session_key="sess-timer", fallback_context="ctx-t")
                self.assertEqual(r2.used_fallback, "")
                self.assertEqual(r2.text, "CLEAN[gemini-3.8-flash/low]: 后续轮次不死锁")
            finally:
                ref.close()

        # 3. Unknown free-text stopReason & mismatched sessionId never leak upstream free text into warning / error
        with tempfile.TemporaryDirectory() as td:
            rt_dir, _, env = self._setup_fake_env(td, scenario="custom_free_text_stop_reason")
            ref = AgyRefiner(Config(refiner=RefinerConfig(provider="agy", max_retries=0)), env=env, runtime_dir=rt_dir)
            try:
                res = ref.refine("自定义停止原因草稿")
                self.assertEqual(res.used_fallback, "asr_draft")
                self.assertIn("agy ACP unexpected stopReason", res.warning)
                self.assertNotIn("机密口述草稿内容789", res.warning)
            finally:
                ref.close()

        with tempfile.TemporaryDirectory() as td:
            rt_dir, _, env = self._setup_fake_env(td, scenario="wrong_resume_sid")
            ref = AgyRefiner(Config(refiner=RefinerConfig(provider="agy", max_retries=0)), env=env, runtime_dir=rt_dir)
            try:
                workdir = ref._ensure_agent_workdir()
                with self.assertRaises(_AgyRpcError) as cm:
                    ref._run_acp_session(
                        agy_bin=env["AGY_BIN"],
                        workdir=workdir,
                        timeout_sec=5.0,
                        prompt_text="[avi-refiner]\nhello",
                        conversation_id="expected-local-cid-123",
                    )
                self.assertEqual(cm.exception.source, "conversation")
                self.assertEqual(str(cm.exception), "agy answered in an unexpected session")
                self.assertNotIn("secret-upstream-sid-999", str(cm.exception))
                self.assertNotIn("expected-local-cid-123", str(cm.exception))
            finally:
                ref.close()

        # 4. Exact sub-50ms deadline is respected without inflating remaining budget back to 50ms
        with tempfile.TemporaryDirectory() as td:
            rt_dir, _, env = self._setup_fake_env(td, scenario="slow_hang")
            ref = AgyRefiner(Config(refiner=RefinerConfig(provider="agy", max_retries=0)), env=env, runtime_dir=rt_dir)
            try:
                workdir = ref._ensure_agent_workdir()
                with self.assertRaises(_AgyRpcError) as cm_exp:
                    ref._run_acp_session(
                        agy_bin=env["AGY_BIN"],
                        workdir=workdir,
                        timeout_sec=0.02,
                        deadline=time.monotonic() - 0.001,
                        prompt_text="[avi-refiner]\nalready expired",
                    )
                self.assertEqual(cm_exp.exception.source, "hard_timeout")
                # Never even spawned a worker when deadline was already exhausted
                self.assertEqual(len(ref._active_procs), 0)
            finally:
                ref.close()

    def test_cli_doctor_and_ping_for_agy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rt_dir = root / "rt"
            fake_agy = _make_fake_agy(root)
            log_file = root / "ping.jsonl"
            cfg_path = root / "config.toml"
            cfg_path.write_text('[asr]\nprovider = "openai"\n[refiner]\nprovider = "agy"\n', encoding="utf-8")

            env_ok = {
                "HOME": td,
                "PATH": td,
                "AGY_BIN": str(fake_agy),
                "AGY_TEST_LOG": str(log_file),
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt_dir),
                "OPENAI_API_KEY": "sk-dummy",
            }
            cfg, merged_env = load_config(
                config_path=cfg_path, env=env_ok, home_dir=root, load_default_env_files=False
            )
            creds = resolve_credentials(cfg, merged_env)
            st_ok, _ms_ok, msg_ok = cli._probe_stage_ping(
                stage="refiner",
                provider="agy",
                cfg=cfg,
                creds=creds,
                env=merged_env,
                base_url="",
                transport=None,
                cache={},
            )
            self.assertEqual(st_ok, "OK")
            self.assertIn("ACP initialize handshake", msg_ok)
            # Confirm zero session/new or session/prompt calls during ping
            ping_methods = [e["method"] for e in _read_jsonl(log_file) if e["event"] == "rpc"]
            self.assertEqual(ping_methods, ["initialize"])

            # Ping failure path
            env_fail = dict(env_ok, AGY_TEST_SCENARIO="ping_fail")
            st_fail, _ms_fail, msg_fail = cli._probe_stage_ping(
                stage="refiner",
                provider="agy",
                cfg=cfg,
                creds=creds,
                env=env_fail,
                base_url="",
                transport=None,
                cache={},
            )
            self.assertEqual(st_fail, "FAIL")
            self.assertIn("FAIL", msg_fail)

            # Doctor output for agy ready vs missing
            buf_doc = io.StringIO()
            with patch.object(sys, "stdout", buf_doc):
                cli._cmd_doctor(cfg, merged_env, as_json=False, ping=False, remote_ping=False)
            doc_out = buf_doc.getvalue()
            self.assertIn("[refiner]   OK agy (model=gemini-3.8-flash, api=agy-acp, effort=low", doc_out)
            self.assertIn(str(fake_agy), doc_out)

            buf_miss = io.StringIO()
            env_miss = {"HOME": str(root / "nohome"), "PATH": "", "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt_dir)}
            with patch.object(sys, "stdout", buf_miss):
                rc_miss = cli._cmd_doctor(cfg, env_miss, as_json=False, ping=False, remote_ping=False)
            self.assertEqual(rc_miss, 1)
            self.assertIn("[refiner]   FAIL agy", buf_miss.getvalue())
            self.assertIn("agy CLI not found", buf_miss.getvalue())

    def test_prepare_session_prewarm_idempotent_concurrent_cancel_and_close(self) -> None:
        # 1. Basic prewarm runs initialize + session/list (zero session/new or prompt); subsequent refine reuses same PID with init_ms == 0
        with tempfile.TemporaryDirectory() as td:
            rt_dir, log_file, env = self._setup_fake_env(td)
            ref = AgyRefiner(
                Config(refiner=RefinerConfig(provider="agy")),
                env=env,
                runtime_dir=rt_dir,
                idle_ttl_sec=60.0,
            )
            try:
                # Concurrent / repeated calls are strictly idempotent (single worker spawn, single session/list)
                for _ in range(5):
                    ref.prepare_session("sess-warm")
                dl = time.monotonic() + 3.0
                while time.monotonic() < dl and (ref._worker is None or ref._prewarm_active > 0):
                    time.sleep(0.01)
                self.assertIsNotNone(ref._worker)
                self.assertEqual(ref._prewarm_active, 0)
                warm_pid = ref._worker.proc.pid  # type: ignore[union-attr]

                # Calling prepare_session again on an already-warm worker is a no-op
                ref.prepare_session("sess-warm")
                time.sleep(0.05)
                self.assertEqual(ref._worker.proc.pid, warm_pid)  # type: ignore[union-attr]

                events_pre = _read_jsonl(log_file)
                self.assertEqual(len([e for e in events_pre if e["event"] == "spawn"]), 1)
                self.assertEqual(
                    [e["method"] for e in events_pre if e["event"] == "rpc"],
                    ["initialize", "session/list"],
                )

                r1 = ref.refine("预热后首句", session_key="sess-warm", fallback_context="ctx-warm")
                self.assertEqual(r1.used_fallback, "")
                self.assertEqual(r1.init_ms, 0)
                self.assertEqual(ref._worker.proc.pid, warm_pid)  # type: ignore[union-attr]
            finally:
                ref.close()
            self.assertEqual(len(ref._active_procs), 0)
            events = _read_jsonl(log_file)
            self.assertEqual(len([e for e in events if e["event"] == "spawn"]), 1)
            self.assertEqual(
                [e["method"] for e in events if e["event"] == "rpc"],
                [
                    "initialize",
                    "session/list",
                    "session/new",
                    "session/set_config_option",
                    "session/set_config_option",
                    "session/prompt",
                ],
            )

        # 2. refine() arriving while slow_prewarm is still running waits once and reuses the warmed worker (zero duplicate spawn)
        with tempfile.TemporaryDirectory() as td:
            rt_dir, log_file, env = self._setup_fake_env(td, scenario="slow_prewarm")
            ref = AgyRefiner(
                Config(refiner=RefinerConfig(provider="agy")),
                env=env,
                runtime_dir=rt_dir,
                idle_ttl_sec=60.0,
            )
            try:
                ref.prepare_session("sess-overlap")
                # Wait briefly until prewarm thread has spawned worker & entered session/list
                dl = time.monotonic() + 2.0
                while time.monotonic() < dl and not ref._worker_is_prewarm:
                    time.sleep(0.005)
                r1 = ref.refine("并发重叠到达", session_key="sess-overlap")
                self.assertEqual(r1.used_fallback, "")
                self.assertEqual(r1.init_ms, 0)
            finally:
                ref.close()
            events = _read_jsonl(log_file)
            self.assertEqual(len([e for e in events if e["event"] == "spawn"]), 1)
            self.assertEqual(
                [e["method"] for e in events if e["event"] == "rpc"][:3],
                ["initialize", "session/list", "session/new"],
            )

        # 3. close() and evict_connections() cleanly abort an in-flight hang_prewarm with zero leaked procs/threads
        for action in ("close", "evict"):
            with tempfile.TemporaryDirectory() as td:
                rt_dir, _, env = self._setup_fake_env(td, scenario="hang_prewarm")
                ref = AgyRefiner(
                    Config(refiner=RefinerConfig(provider="agy")),
                    env=env,
                    runtime_dir=rt_dir,
                    idle_ttl_sec=60.0,
                )
                try:
                    ref.prepare_session("sess-abort")
                    dl = time.monotonic() + 2.0
                    while time.monotonic() < dl and ref._worker is None:
                        time.sleep(0.01)
                    self.assertIsNotNone(ref._worker)
                    pid = ref._worker.proc.pid  # type: ignore[union-attr]
                    t0 = time.monotonic()
                    if action == "close":
                        ref.close()
                    else:
                        self.assertEqual(ref.evict_connections(), 1)
                        dl2 = time.monotonic() + 2.0
                        while time.monotonic() < dl2 and ref._prewarm_active > 0:
                            time.sleep(0.01)
                    self.assertLess(time.monotonic() - t0, 2.5)
                    self.assertIsNone(ref._worker)
                    self.assertEqual(len(ref._active_procs), 0)
                    self.assertFalse(_agy_group_alive(pid))
                    self.assertEqual(ref._prewarm_active, 0)
                finally:
                    ref.close()

        # 4. idle_ttl_sec == 0 makes prepare_session() a no-op; -32601 list_unsupported degrades gracefully; list_error discards cleanly
        with tempfile.TemporaryDirectory() as td:
            rt_dir, log_file, env = self._setup_fake_env(td)
            ref0 = AgyRefiner(
                Config(refiner=RefinerConfig(provider="agy")),
                env=env,
                runtime_dir=rt_dir,
                idle_ttl_sec=0.0,
            )
            try:
                ref0.prepare_session("sess-nottl")
                time.sleep(0.05)
                self.assertIsNone(ref0._worker)
                self.assertEqual(_read_jsonl(log_file), [])
            finally:
                ref0.close()

        with tempfile.TemporaryDirectory() as td:
            rt_dir, log_file, env = self._setup_fake_env(td, scenario="list_unsupported")
            ref_u = AgyRefiner(
                Config(refiner=RefinerConfig(provider="agy")),
                env=env,
                runtime_dir=rt_dir,
                idle_ttl_sec=60.0,
            )
            try:
                ref_u.prepare_session("sess-unsup")
                dl = time.monotonic() + 2.0
                while time.monotonic() < dl and (ref_u._worker is None or ref_u._prewarm_active > 0):
                    time.sleep(0.01)
                self.assertIsNotNone(ref_u._worker)
                r1 = ref_u.refine("降级保留暖进程", session_key="sess-unsup")
                self.assertEqual(r1.used_fallback, "")
                self.assertEqual(r1.init_ms, 0)
            finally:
                ref_u.close()
            self.assertEqual(len([e for e in _read_jsonl(log_file) if e["event"] == "spawn"]), 1)

        with tempfile.TemporaryDirectory() as td:
            rt_dir, _, env = self._setup_fake_env(td, scenario="list_error")
            ref_e = AgyRefiner(
                Config(refiner=RefinerConfig(provider="agy")),
                env=env,
                runtime_dir=rt_dir,
                idle_ttl_sec=60.0,
            )
            try:
                ref_e.prepare_session("sess-lerr")
                dl = time.monotonic() + 2.0
                while time.monotonic() < dl and ref_e._prewarm_active > 0:
                    time.sleep(0.01)
                # Failed prewarm must cleanly discard worker instead of leaving a broken process
                self.assertIsNone(ref_e._worker)
                self.assertEqual(len(ref_e._active_procs), 0)
            finally:
                ref_e.close()

    def test_tail_append_incremental_continuation_and_reset_boundaries(self) -> None:
        # 1. Pure unit checks for _compute_agy_context_delta covering sealed chunks, tail growth, prior ref, rollback, and eviction
        b1 = format_session_reference_chunk("[Speaker]\n第一问\n\n[CLI Agent]\n第一答")
        b1_grown = format_session_reference_chunk(
            "[Speaker]\n第一问\n\n[CLI Agent]\n第一答\n\n[Speaker]\n第二问\n\n[CLI Agent]\n第二答"
        )
        b2 = format_session_reference_chunk("[Speaker]\n跨块第三问")
        b2_grown = format_session_reference_chunk("[Speaker]\n跨块第三问\n\n[CLI Agent]\n跨块第三答")
        prior_blk = format_prior_session_reference_chunk("[Speaker]\n上个会话历史")

        # Same tail grows on newline boundary -> resume with only delta wrapped as session_reference_log
        ok, delta = _compute_agy_context_delta((b1,), [b1_grown])
        self.assertTrue(ok)
        self.assertEqual(len(delta), 1)
        self.assertNotIn("第一问", delta[0])
        self.assertIn("第二问", delta[0])
        self.assertIn("第二答", delta[0])

        # Tail grows AND overflows into a new sealed+tail chunk -> resume with tail delta + new chunk
        ok2, delta2 = _compute_agy_context_delta((b1,), [b1_grown, b2])
        self.assertTrue(ok2)
        self.assertEqual(len(delta2), 2)
        self.assertIn("第二答", delta2[0])
        self.assertEqual(delta2[1], b2)

        # Sealed chunk 0 unchanged, tail chunk 1 grows -> resume with only chunk 1 delta
        ok3, delta3 = _compute_agy_context_delta((b1_grown, b2), [b1_grown, b2_grown])
        self.assertTrue(ok3)
        self.assertEqual(len(delta3), 1)
        self.assertNotIn("第一问", delta3[0])
        self.assertNotIn("跨块第三问", delta3[0])
        self.assertIn("跨块第三答", delta3[0])

        # In-place mutation without newline boundary -> must NOT resume
        b1_mut = format_session_reference_chunk("[Speaker]\n第一问\n\n[CLI Agent]\n第一答被原地改长")
        self.assertEqual(_compute_agy_context_delta((b1,), [b1_mut]), (False, []))

        # Truncation / rollback -> must NOT resume
        self.assertEqual(_compute_agy_context_delta((b1_grown,), [b1]), (False, []))
        self.assertEqual(_compute_agy_context_delta((b1_grown, b2), [b1_grown]), (False, []))

        # 50K rolling eviction (sealed block 0 dropped) -> must NOT resume
        self.assertEqual(_compute_agy_context_delta((b1_grown, b2), [b2, b2_grown]), (False, []))

        # Prior session reference: unchanged prior + new current chunk resumes; retired/changed prior forces fresh session
        self.assertEqual(_compute_agy_context_delta((prior_blk,), [prior_blk, b1]), (True, [b1]))
        self.assertEqual(_compute_agy_context_delta((prior_blk, b1), [b1_grown]), (False, []))
        prior_grown = format_prior_session_reference_chunk("[Speaker]\n上个会话历史\n\n[CLI Agent]\n新增")
        self.assertEqual(_compute_agy_context_delta((prior_blk,), [prior_grown]), (False, []))

        # 2. End-to-end AgyRefiner test with real SessionTracker-like multi-chunk growth, session switch, rollback, and max_turns
        class _DynamicTracker:
            has_bound_session = True

            def __init__(self) -> None:
                self.chunks_by_key: dict[str, list[str]] = {}

            def get_refiner_context_with_prior(self, session_key: str, **_kw: object) -> tuple[str, list[str], str]:
                return "", list(self.chunks_by_key.get(session_key, [])), ""

            def get_state(self, session_key: str) -> object | None:
                return object() if session_key in self.chunks_by_key else None

        with tempfile.TemporaryDirectory() as td:
            rt_dir, log_file, env = self._setup_fake_env(td)
            tracker = _DynamicTracker()
            ref = AgyRefiner(
                Config(refiner=RefinerConfig(provider="agy", max_turns_per_thread=4, include_git_status=False)),
                env=env,
                runtime_dir=rt_dir,
                idle_ttl_sec=60.0,
            )
            try:
                # Turn 1 (sess-inc): initial chunk
                tracker.chunks_by_key["sess-inc"] = ["[Speaker]\n第一轮内容\n\n[CLI Agent]\n第一轮回复"]
                r1 = ref.refine("口述1", session_key="sess-inc", tracker=tracker)  # type: ignore[arg-type]
                cid1 = ref._threads[("sess-inc", "agent")].conversation_id
                self.assertEqual(r1.used_fallback, "")
                self.assertTrue(cid1)

                # Turn 2 (sess-inc): tail grows in-place -> MUST reuse cid1 & send ONLY delta!
                tracker.chunks_by_key["sess-inc"] = [
                    "[Speaker]\n第一轮内容\n\n[CLI Agent]\n第一轮回复\n\n[Speaker]\n第二轮新增"
                ]
                r2 = ref.refine("口述2", session_key="sess-inc", tracker=tracker)  # type: ignore[arg-type]
                self.assertEqual(r2.used_fallback, "")
                self.assertEqual(ref._threads[("sess-inc", "agent")].conversation_id, cid1)
                self.assertEqual(ref._threads[("sess-inc", "agent")].turn_count, 2)

                # Turn 3 (sess-inc): sealed chunk 0 + new chunk 1 -> STILL reuses cid1!
                tracker.chunks_by_key["sess-inc"] = [
                    "[Speaker]\n第一轮内容\n\n[CLI Agent]\n第一轮回复\n\n[Speaker]\n第二轮新增",
                    "[Speaker]\n新块内容A",
                ]
                r3 = ref.refine("口述3", session_key="sess-inc", tracker=tracker)  # type: ignore[arg-type]
                self.assertEqual(r3.used_fallback, "")
                self.assertEqual(ref._threads[("sess-inc", "agent")].conversation_id, cid1)
                self.assertEqual(ref._threads[("sess-inc", "agent")].turn_count, 3)

                # Turn 4 (sess-inc): chunk 1 tail grows -> STILL reuses cid1 (turn 4/4)!
                tracker.chunks_by_key["sess-inc"] = [
                    "[Speaker]\n第一轮内容\n\n[CLI Agent]\n第一轮回复\n\n[Speaker]\n第二轮新增",
                    "[Speaker]\n新块内容A\n\n[CLI Agent]\n新块回复B",
                ]
                r4 = ref.refine("口述4", session_key="sess-inc", tracker=tracker)  # type: ignore[arg-type]
                self.assertEqual(r4.used_fallback, "")
                self.assertEqual(ref._threads[("sess-inc", "agent")].conversation_id, cid1)
                self.assertEqual(ref._threads[("sess-inc", "agent")].turn_count, 4)

                # Turn 5 (sess-inc): hits max_turns_per_thread=4 -> MUST open fresh session/new!
                r5 = ref.refine("口述5满轮", session_key="sess-inc", tracker=tracker)  # type: ignore[arg-type]
                cid2 = ref._threads[("sess-inc", "agent")].conversation_id
                self.assertEqual(r5.used_fallback, "")
                self.assertNotEqual(cid2, cid1)
                self.assertEqual(ref._threads[("sess-inc", "agent")].turn_count, 1)

                # Turn 6 (sess-inc): context rollback (chunk 0 altered) -> MUST open fresh session/new!
                tracker.chunks_by_key["sess-inc"] = ["[Speaker]\n被回滚重写的新历史"]
                r6 = ref.refine("口述6回滚", session_key="sess-inc", tracker=tracker)  # type: ignore[arg-type]
                cid3 = ref._threads[("sess-inc", "agent")].conversation_id
                self.assertEqual(r6.used_fallback, "")
                self.assertNotEqual(cid3, cid2)

                # Turn 7 (other session): switching session_key -> independent conversation_id
                tracker.chunks_by_key["sess-other"] = ["[Speaker]\n另一个项目会话"]
                r7 = ref.refine("口述7切项目", session_key="sess-other", tracker=tracker)  # type: ignore[arg-type]
                cid_other = ref._threads[("sess-other", "agent")].conversation_id
                self.assertEqual(r7.used_fallback, "")
                self.assertNotEqual(cid_other, cid3)
            finally:
                ref.close()

            rpcs = [e for e in _read_jsonl(log_file) if e["event"] == "rpc"]
            prompts_sent = [e["params"]["prompt"][0]["text"] for e in rpcs if e["method"] == "session/prompt"]
            new_calls = [e for e in rpcs if e["method"] == "session/new"]
            # Exactly 4 session/new calls: Turn 1 (initial), Turn 5 (max_turns), Turn 6 (rollback), Turn 7 (other session)
            self.assertEqual(len(new_calls), 4)
            # Turns 2, 3, 4 must all send [avi-refiner] prefix with delta only and NEVER repeat Turn 1 history
            for p_delta in prompts_sent[1:4]:
                self.assertTrue(p_delta.startswith("[avi-refiner]\n"))
                self.assertNotIn("第一轮内容", p_delta)
                self.assertNotIn("第一轮回复", p_delta)
            self.assertIn("第二轮新增", prompts_sent[1])
            self.assertIn("新块内容A", prompts_sent[2])
            self.assertNotIn("第二轮新增", prompts_sent[2])
            self.assertIn("新块回复B", prompts_sent[3])
            self.assertNotIn("新块内容A", prompts_sent[3])

    def test_agent_thought_chunk_observability_and_privacy(self) -> None:
        self.assertIn("thought_chunks", _TELEMETRY_NUMERIC_FIELDS)
        self.assertIn("thought_chars", _TELEMETRY_NUMERIC_FIELDS)

        # 1. Normal zero-thought turn: thought_chunks/chars stay 0 and are omitted from telemetry summary
        with tempfile.TemporaryDirectory() as td:
            rt_dir, _, env = self._setup_fake_env(td)
            ref = AgyRefiner(Config(refiner=RefinerConfig(provider="agy")), env=env, runtime_dir=rt_dir)
            try:
                r0 = ref.refine("零思考测试")
                self.assertEqual(r0.used_fallback, "")
                self.assertEqual((r0.thought_chunks, r0.thought_chars), (0, 0))
                summ0 = build_pipeline_summary(1, PipelineResult(seq_id=1, ok=True, refined=r0, final_text=r0.text), dry_run=True)
                self.assertNotIn("thought_chunks", summ0)
                self.assertNotIn("thought_chars", summ0)
            finally:
                ref.close()

        # 2. Streamed agent_thought_chunk counted accurately (ignoring other sessionId), zero content leaked
        with tempfile.TemporaryDirectory() as td:
            rt_dir, _, env = self._setup_fake_env(td, scenario="thought_chunks")
            ref = AgyRefiner(Config(refiner=RefinerConfig(provider="agy")), env=env, runtime_dir=rt_dir)
            try:
                rt = ref.refine("带少量思考测试")
                self.assertEqual(rt.used_fallback, "")
                # "秘密思考片段一" (7 chars) + "secret_thought_2" (16 chars) = 23 chars across 2 chunks
                self.assertEqual(rt.thought_chunks, 2)
                self.assertEqual(rt.thought_chars, 23)
                self.assertEqual(rt.reasoning_tokens, 0)
                self.assertNotIn("秘密思考", rt.text)
                self.assertNotIn("secret_thought_2", rt.text)
                self.assertNotIn("SECRET_OTHER_SID_THOUGHT", rt.text)

                summ = build_pipeline_summary(2, PipelineResult(seq_id=2, ok=True, refined=rt, final_text=rt.text), dry_run=True)
                self.assertEqual(summ["thought_chunks"], 2)
                self.assertEqual(summ["thought_chars"], 23)
                dumped = json.dumps(summ, ensure_ascii=False)
                self.assertNotIn("秘密思考", dumped)
                self.assertNotIn("secret_thought_2", dumped)
                self.assertNotIn("SECRET_OTHER_SID_THOUGHT", dumped)
                flags = diagnose_record_flags(summ, is_first_for_session=True)
                self.assertIn("THOUGHT(2/23c)", flags)
                self.assertNotIn("REASONING_BLOAT", flags)
            finally:
                ref.close()

        # 3. Heavy streamed thinking (>=400 chars) triggers REASONING_BLOAT even when usage.thoughtTokens == 0
        with tempfile.TemporaryDirectory() as td:
            rt_dir, _, env = self._setup_fake_env(td, scenario="thought_bloat")
            ref = AgyRefiner(Config(refiner=RefinerConfig(provider="agy")), env=env, runtime_dir=rt_dir)
            try:
                rb = ref.refine("长思考告警测试")
                self.assertEqual(rb.used_fallback, "")
                self.assertEqual(rb.thought_chunks, 1)
                self.assertEqual(rb.thought_chars, 500)
                self.assertEqual(rb.reasoning_tokens, 0)
                summ_b = build_pipeline_summary(3, PipelineResult(seq_id=3, ok=True, refined=rb, final_text=rb.text), dry_run=True)
                self.assertIn("REASONING_BLOAT", diagnose_record_flags(summ_b, is_first_for_session=True))
            finally:
                ref.close()


if __name__ == "__main__":
    unittest.main()
