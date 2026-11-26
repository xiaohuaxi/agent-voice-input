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


def _is_ci() -> bool:
    return os.environ.get("CI", "").strip().lower() in {"1", "true", "yes"}

from agent_voice_input.asr_providers import (  # noqa: E402
    AsrContext,
    AsrError,
    AsrResult,
    BaseAsrProvider,
    HttpRequest,
    HttpResponse,
    reset_rate_limit_pacers,
)
from agent_voice_input.audio_proc import pack_pcm16_mono_wav  # noqa: E402
from agent_voice_input.config import Config, ConfigError, HotkeyConfig, RefinerConfig, _parse_simple_toml, load_config, load_env_files, parse_toml, resolve_credentials  # noqa: E402
from agent_voice_input.daemon import (  # noqa: E402
    MAX_ASR_CHUNK_CONCURRENCY,
    VoiceInputDaemon,
    _transcribe_wav_chunks,
    run_pipeline_once,
    send_control_command,
)
from agent_voice_input.platform_wsl import (  # noqa: E402
    FfmpegWavRecorder,
    ForegroundWindow,
    MicrophoneUnavailableError,
    NoMicrophoneError,
    PasteOutcome,
    SHIM_SCRIPT,
    WIN_SHIM_DIR,
    WslPlatform,
    is_windows_terminal_process,
    list_win_shim_sources,
    read_win_shim_source,
)
from agent_voice_input.refiner import (  # noqa: E402
    CodexRefiner,
    GeminiRefiner,
    OpenAICompatibleRefiner,
    OpenAIResponsesRefiner,
    RefinerResult,
    create_refiner,
    effective_refiner_timeout_sec,
)
from agent_voice_input.session_ctx import SessionTracker, write_session_registry_entry  # noqa: E402

import base64
import http.server


def _wait_for_control_socket(runtime_dir: Path, timeout_sec: float = 3.0) -> dict[str, object]:
    """Wait until the daemon control socket finishes bind -> chmod(0o600) -> listen and answers status."""
    deadline = time.monotonic() + timeout_sec
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            resp = send_control_command(runtime_dir, {"cmd": "status"}, timeout_sec=0.5)
            if resp.get("ok"):
                return resp
        except Exception as exc:
            last_err = exc
        time.sleep(0.02)
    raise AssertionError(
        f"daemon control socket at {runtime_dir / 'control.sock'} did not become ready within {timeout_sec}s: {last_err!r}"
    )


def _make_fake_jwt(
    *,
    exp: int | None = 4102444800,
    account_id: str = "acct-unit-test-0001",
    is_fedramp: bool = False,
) -> str:
    hdr = base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').decode("ascii").rstrip("=")
    payload: dict[str, object] = {"iss": "https://auth.openai.com"}
    if exp is not None:
        payload["exp"] = exp
    if account_id or is_fedramp:
        auth_ns: dict[str, object] = {}
        if account_id:
            auth_ns["chatgpt_account_id"] = account_id
        if is_fedramp:
            auth_ns["chatgpt_account_is_fedramp"] = True
        payload["https://api.openai.com/auth"] = auth_ns
    body = (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )
    return f"{hdr}.{body}.fakesig"


def _write_codex_auth_json(
    home_dir: Path,
    *,
    access_token: str | None = None,
    account_id: str = "acct-unit-test-0001",
    auth_mode: str = "chatgpt",
    openai_api_key: str | None = None,
    exp: int | None = 4102444800,
) -> Path:
    cdir = home_dir / ".codex"
    cdir.mkdir(parents=True, exist_ok=True)
    auth_file = cdir / "auth.json"
    tok = access_token if access_token is not None else _make_fake_jwt(exp=exp, account_id=account_id)
    obj: dict[str, object] = {
        "auth_mode": auth_mode,
        "OPENAI_API_KEY": openai_api_key,
    }
    if auth_mode == "api_key":
        obj["tokens"] = None
    else:
        tokens_dict: dict[str, object] = {
            "access_token": tok,
            "refresh_token": "rt.fake_unit_test_refresh",
        }
        if account_id:
            tokens_dict["account_id"] = account_id
        obj["tokens"] = tokens_dict
    auth_file.write_text(json.dumps(obj), encoding="utf-8")
    auth_file.chmod(0o600)
    return auth_file


def _sse_completed_frames(
    deltas: list[str],
    *,
    usage: dict[str, object] | None = None,
    output_only: str = "",
) -> list[str]:
    frames = [
        f"data: {json.dumps({'type': 'response.output_text.delta', 'delta': d}, ensure_ascii=False)}\n\n"
        for d in deltas
    ]
    resp_obj: dict[str, object] = {"id": "resp_mock_1"}
    if usage is not None:
        resp_obj["usage"] = usage
    if output_only:
        resp_obj["output"] = [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": output_only}],
            }
        ]
    frames.append(
        f"data: {json.dumps({'type': 'response.completed', 'response': resp_obj}, ensure_ascii=False)}\n\n"
    )
    return frames


class _LocalResponsesServer:
    """Offline localhost HTTP/1.1 Keep-Alive SSE server (deliberately omits Content-Type on 200)."""

    def __init__(self, responder=None, *, accept_delay_sec: float = 0.0) -> None:
        outer = self
        self.responder = responder
        self.accept_delay_sec = accept_delay_sec
        self.requests: list[dict[str, object]] = []
        self.accepted_conns = 0
        self._lock = threading.Lock()
        self._client_socks: set[object] = set()

        class _Server(http.server.ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

            def get_request(self):
                sock, addr = super().get_request()
                if outer.accept_delay_sec > 0:
                    time.sleep(outer.accept_delay_sec)
                with outer._lock:
                    outer.accepted_conns += 1
                    outer._client_socks.add(sock)
                return sock, addr

        class _Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                return

            def do_POST(self) -> None:
                clen = int(self.headers.get("Content-Length") or 0)
                raw_body = self.rfile.read(clen) if clen > 0 else b""
                try:
                    parsed_body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
                except ValueError:
                    parsed_body = {}
                with outer._lock:
                    seq_no = len(outer.requests) + 1
                    rec = {
                        "seq": seq_no,
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "raw_headers": dict(self.headers.items()),
                        "body": parsed_body,
                        "client_port": self.client_address[1],
                    }
                    outer.requests.append(rec)
                if outer.responder is not None:
                    outer.responder(self, rec)
                    return
                inp = parsed_body.get("input") or []
                last_txt = inp[-1]["content"][0]["text"] if inp else ""
                draft = (
                    last_txt.split("<asr_draft_to_clean>\n", 1)[1].split("\n</asr_draft_to_clean>", 1)[0]
                    if "<asr_draft_to_clean>\n" in last_txt
                    else last_txt
                )
                outer.send_sse_200(self, _sse_completed_frames([f"CLEAN#{seq_no}:", draft]))

        self._httpd = _Server(("127.0.0.1", 0), _Handler)
        self.port = self._httpd.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}/backend-api/codex"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @staticmethod
    def send_sse_200(
        handler: http.server.BaseHTTPRequestHandler,
        frames: list[str],
        *,
        set_cookies: list[str] = (),
        close_after: bool = False,
    ) -> None:
        handler.send_response(200)
        # Intentionally NO Content-Type header to match real Codex backend HTTP 200 behavior!
        for sc in set_cookies:
            handler.send_header("Set-Cookie", sc)
        handler.send_header("Transfer-Encoding", "chunked")
        handler.send_header("Connection", "close" if close_after else "keep-alive")
        handler.end_headers()
        for fr in frames:
            b = fr.encode("utf-8")
            handler.wfile.write(f"{len(b):x}\r\n".encode("ascii") + b + b"\r\n")
            handler.wfile.flush()
        handler.wfile.write(b"0\r\n\r\n")
        handler.wfile.flush()
        if close_after:
            handler.close_connection = True

    @staticmethod
    def send_json(
        handler: http.server.BaseHTTPRequestHandler,
        status: int,
        payload: object,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        raw = (
            payload.encode("utf-8")
            if isinstance(payload, str)
            else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(raw)))
        if headers:
            for k, v in headers.items():
                handler.send_header(k, v)
        handler.end_headers()
        handler.wfile.write(raw)
        handler.wfile.flush()

    def close(self) -> None:
        self._httpd.shutdown()
        with self._lock:
            socks = list(self._client_socks)
            self._client_socks.clear()
        for s in socks:
            try:
                s.shutdown(2)
            except Exception:
                pass
            try:
                s.close()
            except Exception:
                pass
        self._httpd.server_close()
        self._thread.join(timeout=1.0)


_MOCK_AUTH_APP_SERVER_CODE = r"""
import json, os, pathlib, sys
log_path = pathlib.Path(sys.argv[1])
mode = sys.argv[2]
token_out = sys.argv[3] if len(sys.argv) > 3 else ""
auth_json_out = sys.argv[4] if len(sys.argv) > 4 else ""
for raw in sys.stdin:
    msg = json.loads(raw)
    with log_path.open("a", encoding="utf-8") as lf:
        lf.write(json.dumps({**msg, "_env_has_oai": "OPENAI_API_KEY" in os.environ}, ensure_ascii=False) + "\n")
    m = msg.get("method")
    rid = msg.get("id")
    if m == "initialize":
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {"userAgent": "mock"}}) + "\n")
        sys.stdout.flush()
    elif m == "getAuthStatus":
        if mode == "fail":
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "refresh network error"}}) + "\n")
            sys.stdout.flush()
            continue
        if auth_json_out:
            p = pathlib.Path(auth_json_out)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({
                "auth_mode": "chatgpt",
                "tokens": {"access_token": token_out, "account_id": "acct-refreshed-disk", "refresh_token": "rt.2"}
            }), encoding="utf-8")
        auth_method = "apikey" if mode == "apikey" else "chatgpt"
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0",
            "id": rid,
            "result": {"authMethod": auth_method, "authToken": token_out, "requiresOpenaiAuth": True}
        }) + "\n")
        sys.stdout.flush()
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

    def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None, **_kw: object) -> AsrResult:
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
        self.hud_events: list[dict[str, object]] = []
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

    def hud(self, state: str, text: str = "", duration_ms: int = 0, **kw: object) -> None:
        self.huds.append(state)
        self.hud_events.append({"state": state, "duration_ms": duration_ms, **kw})

    def start_recording(self, runtime_dir: Path, *, loopback: bool = False, seq_id: int | None = None) -> None:
        return None

    def stop_recording(self) -> tuple[bytes, str]:
        return self.wav_bytes, self.source

    def cancel_recording(self) -> None:
        self.canceled += 1

    def switch_to_fallback_audio(self) -> bool:
        self.switched += 1
        return True

    def deliver_text(
        self,
        text: str,
        seq_id: int,
        target_hwnd: int = 0,
        *,
        origin_window: ForegroundWindow | None = None,
        fallback_clipboard_text: str | None = None,
        suppress_beep: object = False,
        is_cancelled: object = None,
        before_clipboard_write: object = None,
        before_commit: object = None,
        same_window_verifier: object = None,
    ) -> PasteOutcome:
        del origin_window, fallback_clipboard_text, suppress_beep
        if callable(is_cancelled) and is_cancelled():
            return PasteOutcome(False, False, "cancelled", "")
        if callable(before_clipboard_write):
            before_clipboard_write()
        if callable(before_commit) and not before_commit():
            return PasteOutcome(False, False, "cancelled", "")
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
        self.assertEqual(parsed["hotkey"]["scope"], "all")
        self.assertIs(parsed["hotkey"]["yield_remote_desktop"], True)
        self.assertEqual(parsed["hotkey"]["ignore_apps"], [])
        self.assertEqual(parsed["ui"]["language"], "auto")
        self.assertEqual(parsed["ui"]["position"], "bottom")
        self.assertEqual(parsed["audio"]["macos_device"], "default")
        self.assertEqual(parsed["asr"]["provider"], "auto")
        self.assertEqual(parsed["asr"]["model"], "")
        self.assertEqual(parsed["asr"]["api"], "auto")
        self.assertEqual(parsed["asr"]["effort"], "auto")
        self.assertEqual(parsed["refiner"]["provider"], "auto")
        self.assertEqual(parsed["refiner"]["model"], "")
        self.assertEqual(parsed["refiner"]["api"], "auto")
        self.assertNotIn("max_turns_per_thread", parsed["refiner"])
        self.assertIn("# max_turns_per_thread = 0", toml_raw)
        self.assertIn("# timeout_ms = 800", toml_raw)
        self.assertNotIn("max_turns_per_thread", (_REPO_ROOT / "README.md").read_text(encoding="utf-8"))
        self.assertNotIn("max_turns_per_thread", (_REPO_ROOT / "README.zh-CN.md").read_text(encoding="utf-8"))
        cfg, _ = load_config(config_path=toml_path, env={"HOME": "/tmp/nonexistent-home"})
        self.assertEqual(cfg.hotkey.scope, "all")
        self.assertIs(cfg.hotkey.yield_remote_desktop, True)
        self.assertEqual(cfg.hotkey.ignore_apps, ())
        self.assertIs(cfg.hotkey.only_in_terminals, False)
        self.assertEqual(cfg.ui.language, "auto")
        self.assertEqual(cfg.ui.position, "bottom")
        self.assertEqual(cfg.audio.macos_device, "default")
        self.assertEqual(cfg.refiner.provider, "auto")
        self.assertEqual(cfg.refiner.model, "")
        self.assertEqual(cfg.refiner.api, "auto")
        self.assertEqual(cfg.refiner.effective_api("codex"), "responses")
        self.assertEqual(cfg.refiner.effective_api("deepseek"), "chat-completions")
        self.assertIs(cfg.audio.beep, False)

        env_raw = env_path.read_text(encoding="utf-8")
        self.assertNotIn("API_ID", env_raw)
        # All example placeholder API keys stay commented out (matches `init` template) to prevent 401 hijacking
        loaded = load_env_files({}, env_paths=[env_path])
        self.assertEqual(loaded, {})
        for expected_key in (
            "DASHSCOPE_API_KEY",
            "DEEPSEEK_API_KEY",
            "DOUBAO_SPEECH_API_KEY",
            "ARK_API_KEY",
            "OPENROUTER_API_KEY",
            "GEMINI_API_KEY",
            "OPENAI_API_KEY",
            "GROQ_API_KEY",
            "SILICONFLOW_API_KEY",
        ):
            self.assertIn(f"# {expected_key}=", env_raw)
        # Legacy dual-header Doubao credentials stay commented out in config.example.env
        self.assertIn("# DOUBAO_ASR_APP_ID=", env_raw)
        self.assertIn("# DOUBAO_ASR_API_KEY=", env_raw)

    def test_pyproject_and_package_data_integrity(self) -> None:
        pyproject_path = _REPO_ROOT / "pyproject.toml"
        try:
            import tomllib  # type: ignore[import-not-found]
            data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        except ImportError:
            data = parse_toml(pyproject_path.read_text(encoding="utf-8"))

        proj = data["project"]
        self.assertEqual(proj["name"], "agent-voice-input")
        self.assertEqual(proj["version"], "0.16.0")
        self.assertEqual(proj["requires-python"], ">=3.10")
        self.assertEqual(proj["license"], "Apache-2.0")
        self.assertEqual(
            proj["scripts"]["agent-voice-input"],
            "agent_voice_input.cli:main",
        )
        pkg_data = data["tool"]["setuptools"]["package-data"]["agent_voice_input"]
        self.assertIn("win_shim.ps1", pkg_data)
        self.assertIn("win_shim/*.cs", pkg_data)
        self.assertIn("mac_shim/*.swift", pkg_data)
        manifest_raw = (_REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        self.assertIn("win_shim.ps1", manifest_raw)
        self.assertIn("recursive-include src/agent_voice_input/win_shim *.cs", manifest_raw)
        self.assertIn("recursive-include src/agent_voice_input/mac_shim *.swift", manifest_raw)
        self.assertIn("prune .agents", manifest_raw)
        self.assertIn("prune .claude", manifest_raw)
        self.assertTrue(SHIM_SCRIPT.is_file())
        self.assertTrue(WIN_SHIM_DIR.is_dir())
        self.assertEqual(
            SHIM_SCRIPT.resolve(),
            (_REPO_ROOT / "src" / "agent_voice_input" / "win_shim.ps1").resolve(),
        )
        self.assertGreaterEqual(len(list_win_shim_sources()), 7)
        self.assertTrue((_REPO_ROOT / "LICENSE").is_file())
        self.assertTrue((_REPO_ROOT / "bin" / "agent-voice-input").is_file())

    def test_codex_command_isolation_effort_mapping_and_no_fast_mode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            fake_codex = tmp / "codex"
            fake_codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_codex.chmod(0o755)
            _write_codex_auth_json(tmp)
            ref = CodexRefiner(Config(), env={"HOME": str(tmp), "CODEX_BIN": str(fake_codex)}, runtime_dir=tmp)
            cmd = ref._build_auth_refresh_cmd()
            joined = " ".join(cmd)
            self.assertIn("mcp_servers={}", cmd)
            self.assertIn("project_doc_max_bytes=0", cmd)
            self.assertIn("shell_tool", cmd)
            self.assertIn("multi_agent", cmd)
            self.assertNotIn("fast", joined.lower())
            self.assertNotIn("service_tier", joined.lower())
            ref.close()

            # Verify stateless /responses reasoning effort mapping across concrete / auto / default & strict no-Fast
            srv = _LocalResponsesServer()
            try:
                for eff, expected_wire_eff in (
                    ("none", "none"),
                    ("minimal", "minimal"),
                    ("low", "low"),
                    ("medium", "medium"),
                    ("high", "high"),
                    ("xhigh", "xhigh"),
                    ("max", "max"),
                    ("auto", None),
                    ("default", None),
                ):
                    cfg_eff = Config(
                        refiner=RefinerConfig(
                            provider="codex",
                            base_url=srv.base_url,
                            effort=eff,
                            timeout_sec=3.0,
                        )
                    )
                    r_eff = CodexRefiner(cfg_eff, env={"HOME": str(tmp)}, runtime_dir=tmp)
                    try:
                        out = r_eff.refine("测试档位")
                        self.assertTrue(out.text.endswith(":测试档位"))
                    finally:
                        r_eff.close()
                    sent_body = srv.requests[-1]["body"]
                    assert isinstance(sent_body, dict)
                    raw_json = json.dumps(sent_body).lower()
                    self.assertNotIn("service_tier", raw_json)
                    self.assertNotIn("previous_response_id", sent_body)
                    self.assertIs(sent_body["store"], False)
                    self.assertIs(sent_body["stream"], True)
                    self.assertEqual(sent_body["tools"], [])
                    self.assertEqual(sent_body["tool_choice"], "none")
                    self.assertIs(sent_body["parallel_tool_calls"], False)
                    if expected_wire_eff is None:
                        self.assertNotIn("reasoning", sent_body)
                    else:
                        self.assertEqual(sent_body["reasoning"], {"effort": expected_wire_eff})
            finally:
                srv.close()

    def test_win_shim_static_safety_invariants(self) -> None:
        import re

        shim = read_win_shim_source()
        self.assertIn("LLKHF_INJECTED", shim)
        self.assertIn("_chordTainted", shim)
        self.assertIn("ptt_cancel", shim)
        self.assertIn("CallNextHookEx", shim)
        self.assertIn("SendPasteChord", shim)
        self.assertIn("KEYEVENTF_EXTENDEDKEY", shim)
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
        self.assertIn(r"\u25D0 \u8BED\u97F3\u8BC6\u522B\u2026", shim)
        self.assertIn("Color.FromArgb(56, 189, 248)", shim)
        self.assertIn('st == "refining"', shim)
        self.assertIn(r"\u25C8 \u6574\u7406\u7CBE\u4FEE\u2026", shim)
        self.assertIn("Color.FromArgb(208, 156, 255)", shim)
        self.assertIn("Color.FromArgb(251, 191, 36)", shim)
        self.assertIn("Color.FromArgb(64, 18, 26)", shim)
        self.assertIn("Color.FromArgb(72, 38, 12)", shim)
        self.assertIn("Color.FromArgb(242, 255, 255, 255)", shim)
        self.assertNotIn("save vrec", shim)
        self.assertNotIn("[Console]::Beep", shim)
        self.assertNotIn("QueueUserWorkItem", shim)
        self.assertNotIn("GetText", shim)  # never saves/restores old clipboard
        self.assertIn("IsAnyPasteBlockingKeyDown", shim)
        self.assertIn("hotkey_held", shim)
        self.assertIn("force_clipboard_only", shim)
        self.assertIn("Set-ClipboardSafe", shim)
        self.assertIn("if ($fbTxt -ne $txt) { $clipOk = Set-ClipboardSafe $fbTxt $cmdEnqueuedTick $pasteDeadlineMs }", shim)
        self.assertIn('if ($clipOk) { "send_input_failed" } else { Get-ClipFailReason $cmdEnqueuedTick $pasteDeadlineMs }', shim)
        self.assertNotIn("$fbOk", shim)
        m_send = re.search(r"public static bool SendPasteChord\([^)]*\)\s*\{[^}]*\}", shim, re.S)
        self.assertIsNotNone(m_send)
        assert m_send is not None
        self.assertNotIn("Thread.Sleep", m_send.group(0))
        ps1_only = SHIM_SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("Start-Sleep", ps1_only.split('elseif ($op -eq "paste")', 1)[1])


class RefinerProtocolAndFallbackTests(unittest.TestCase):
    def test_codex_luna_incremental_delta_rotation_and_generation_reset(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _write_codex_auth_json(tmp, account_id="acct-stateless-01")
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

            srv = _LocalResponsesServer()
            cfg = Config(
                refiner=RefinerConfig(
                    provider="codex",
                    model="gpt-6-luna",
                    base_url=srv.base_url,
                    effort="low",
                    timeout_sec=5.0,
                )
            )
            ref = CodexRefiner(cfg, env={"HOME": str(tmp)}, runtime_dir=tmp)
            try:
                r1 = ref.refine("把 refiner 点 py 修一下", session_key=snap.session_key, tracker=tracker)
                self.assertEqual(r1.text, "CLEAN#1:把 refiner 点 py 修一下")
                self.assertEqual(r1.used_fallback, "")

                r2 = ref.refine("再跑单测", session_key=snap.session_key, tracker=tracker)
                self.assertEqual(r2.text, "CLEAN#2:再跑单测")

                with rollout.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "AgentMessage", "content": [{"type": "text", "text": "已更新 daemon.py"}]}}}) + "\n")
                tracker.sync_session(window_title="codex [#abcd1234]")
                r3 = ref.refine("继续提交", session_key=snap.session_key, tracker=tracker)
                self.assertEqual(r3.text, "CLEAN#3:继续提交")

                rollout.write_text(
                    meta_ln
                    + json.dumps({"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "UserMessage", "content": [{"type": "text", "text": "全新上下文"}]}}})
                    + "\n",
                    encoding="utf-8",
                )
                tracker.sync_session(window_title="codex [#abcd1234]")
                r4 = ref.refine("第四句", session_key=snap.session_key, tracker=tracker)
                self.assertEqual(r4.text, "CLEAN#4:第四句")
            finally:
                ref.close()
                srv.close()

            self.assertEqual(len(srv.requests), 4)
            # Keep-Alive socket reused across all 4 sequential requests!
            ports = [r["client_port"] for r in srv.requests]
            self.assertEqual(len(set(ports)), 1)

            b0 = srv.requests[0]["body"]
            b1 = srv.requests[1]["body"]
            b2 = srv.requests[2]["body"]
            b3 = srv.requests[3]["body"]
            assert isinstance(b0, dict) and isinstance(b1, dict) and isinstance(b2, dict) and isinstance(b3, dict)
            # Deterministic prompt_cache_key & session-id / thread-id per session_key
            pck = b0["prompt_cache_key"]
            self.assertTrue(all(b["prompt_cache_key"] == pck for b in (b0, b1, b2, b3)))
            h0 = srv.requests[0]["headers"]
            assert isinstance(h0, dict)
            self.assertEqual(h0.get("session-id"), pck)
            self.assertEqual(h0.get("thread-id"), pck)
            self.assertEqual(h0.get("chatgpt-account-id"), "acct-stateless-01")
            self.assertEqual(h0.get("originator"), "codex_cli_rs")
            self.assertEqual(h0.get("x-codex-routing-hint"), "model=gpt-6-luna")

            # Developer context chunk + final user draft layout with zero prior draft residue!
            self.assertEqual([it["role"] for it in b0["input"]], ["developer", "user"])
            self.assertIn("<session_reference_log", b0["input"][0]["content"][0]["text"])
            self.assertIn("refiner.py", b0["input"][0]["content"][0]["text"])
            self.assertNotIn("把 refiner 点 py 修一下", json.dumps(b1["input"], ensure_ascii=False))
            self.assertEqual(b0["input"][0]["content"][0]["text"], b1["input"][0]["content"][0]["text"])
            self.assertIn("daemon.py", b2["input"][0]["content"][0]["text"])
            self.assertIn("全新上下文", b3["input"][0]["content"][0]["text"])
            self.assertNotIn("daemon.py", b3["input"][0]["content"][0]["text"])

    def test_codex_fallbacks_to_asr_draft_and_redacts_detail_and_sse_errors(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            secret_acct = "acct-" + ("9" * 16)
            secret_tok = _make_fake_jwt(account_id=secret_acct)
            _write_codex_auth_json(tmp, access_token=secret_tok, account_id=secret_acct)
            env = {"HOME": str(tmp), "DASHSCOPE_API_KEY": "MOCK_SECRET_9999"}

            mode_box = ["fastapi_detail"]

            def err_responder(handler, _rec) -> None:
                m = mode_box[0]
                if m == "fastapi_detail":
                    _LocalResponsesServer.send_json(
                        handler,
                        400,
                        {"detail": f"Store must be false for {secret_acct} key=MOCK_SECRET_9999 tok={secret_tok}"},
                    )
                else:
                    _LocalResponsesServer.send_sse_200(
                        handler,
                        [
                            "data: "
                            + json.dumps(
                                {
                                    "type": "response.failed",
                                    "response": {
                                        "error": {
                                            "code": "invalid_prompt",
                                            "message": f"blocked {secret_acct} MOCK_SECRET_9999",
                                        }
                                    },
                                }
                            )
                            + "\n\n"
                        ],
                    )

            srv = _LocalResponsesServer(responder=err_responder)
            cfg = Config(refiner=RefinerConfig(provider="codex", base_url=srv.base_url, timeout_sec=3.0))
            ref = CodexRefiner(cfg, env=env, runtime_dir=tmp)
            try:
                res1 = ref.refine("绝不丢失的用户原话")
                self.assertEqual(res1.text, "绝不丢失的用户原话")
                self.assertEqual(res1.provider, "codex")
                self.assertEqual(res1.used_fallback, "asr_draft")
                self.assertIn("[REDACTED]", res1.warning)
                for forbidden in ("MOCK_SECRET_9999", secret_acct, secret_tok):
                    self.assertNotIn(forbidden, res1.warning)

                mode_box[0] = "sse_failed"
                res2 = ref.refine("流内错误回退原话")
                self.assertEqual(res2.text, "流内错误回退原话")
                self.assertEqual(res2.used_fallback, "asr_draft")
                for forbidden in ("MOCK_SECRET_9999", secret_acct, secret_tok):
                    self.assertNotIn(forbidden, res2.warning)

                # Single-hop ASR result bypasses Stage 2 automatically
                single = AsrResult(
                    text="Gemini单跳已精修",
                    provider="gemini",
                    model="gemini-2.5-flash",
                    latency_ms=50,
                    is_single_hop_refined=True,
                )
                res3 = ref.refine(single)
                self.assertEqual(res3.text, "Gemini单跳已精修")
                self.assertEqual(res3.used_fallback, "single_hop_passthrough")
            finally:
                ref.close()
                srv.close()

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
        self.assertEqual(out.model, "doubao-seed-2-0-mini-260428")
        self.assertEqual(seen[0].url, "https://ark.cn-beijing.volces.com/api/v3/chat/completions")
        self.assertEqual(seen[0].headers.get("Authorization"), f"Bearer {fake_val}")
        volc_payload = json.loads(seen[0].body.decode("utf-8"))
        self.assertEqual(volc_payload.get("thinking"), {"type": "disabled"})

        def err_transport(_req: HttpRequest) -> HttpResponse:
            return HttpResponse(500, {}, f"upstream leaked {fake_val}".encode("utf-8"))

        ref_err = OpenAICompatibleRefiner(cfg_volc, creds, env=env, transport=err_transport)
        fb = ref_err.refine("保留原稿")
        self.assertEqual(fb.text, "保留原稿")
        self.assertEqual(fb.used_fallback, "asr_draft")
        self.assertNotIn(fake_val, fb.warning)

        # 2. OpenRouter, DashScope, Volcengine, DeepSeek, Groq, SiliconFlow, OpenAI, and keyless loopback openai-compatible
        matrix = [
            ("openrouter", "", "", "auto", "https://openrouter.ai/api/v1/chat/completions", "google/gemini-3.8-flash", {"reasoning": {"effort": "minimal"}}),
            ("openrouter", "", "", "none", "https://openrouter.ai/api/v1/chat/completions", "google/gemini-3.8-flash", {"reasoning": {"effort": "minimal"}}),
            ("openrouter", "", "", "low", "https://openrouter.ai/api/v1/chat/completions", "google/gemini-3.8-flash", {"reasoning": {"effort": "low"}}),
            ("openrouter", "", "", "default", "https://openrouter.ai/api/v1/chat/completions", "google/gemini-3.8-flash", {}),
            ("openrouter", "google/gemini-2.5-flash-lite", "", "low", "https://openrouter.ai/api/v1/chat/completions", "google/gemini-2.5-flash-lite", {"reasoning": {"effort": "low"}}),
            ("openrouter", "google/gemini-2.5-flash-lite", "", "auto", "https://openrouter.ai/api/v1/chat/completions", "google/gemini-2.5-flash-lite", {"reasoning": {"effort": "none"}}),
            ("openrouter", "google/gemini-2.5-flash-lite", "", "default", "https://openrouter.ai/api/v1/chat/completions", "google/gemini-2.5-flash-lite", {}),
            ("openrouter", "minimax/minimax-m3.1-flash", "", "auto", "https://openrouter.ai/api/v1/chat/completions", "minimax/minimax-m3.1-flash", {"reasoning": {"effort": "minimal"}}),
            ("openrouter", "anthropic/claude-haiku-5.5", "", "none", "https://openrouter.ai/api/v1/chat/completions", "anthropic/claude-haiku-5.5", {"reasoning": {"effort": "none"}}),
            ("dashscope", "", "", "auto", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions", "qwen3.8-flash", {"enable_thinking": False}),
            ("dashscope", "", "", "low", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions", "qwen3.8-flash", {"enable_thinking": True}),
            ("groq", "", "", "low", "https://api.groq.com/openai/v1/chat/completions", "llama-3.3-70b-versatile", {}),
            ("siliconflow", "", "", "low", "https://api.siliconflow.cn/v1/chat/completions", "Qwen/Qwen2.5-7B-Instruct", {}),
            # Explicit api="chat-completions" on openai: reasoning models send reasoning_effort & omit temperature;
            # non-reasoning override gpt-4o-mini omits reasoning_effort so it never 400s
            ("openai", "", "", "low", "https://api.openai.com/v1/chat/completions", "gpt-6-luna", {"reasoning_effort": "low"}),
            ("openai", "gpt-4o-mini", "", "low", "https://api.openai.com/v1/chat/completions", "gpt-4o-mini", {}),
            ("openai", "o4-mini", "", "medium", "https://api.openai.com/v1/chat/completions", "o4-mini", {"reasoning_effort": "medium"}),
            ("openai", "gpt-5-mini", "", "default", "https://api.openai.com/v1/chat/completions", "gpt-5-mini", {}),
            ("openai", "gpt-6-luna", "", "auto", "https://api.openai.com/v1/chat/completions", "gpt-6-luna", {}),
            # Custom third-party base_url on openai stays conservatively on chat-completions under api="auto"
            ("openai", "gpt-6-luna", "https://relay.example.com/v1", "low", "https://relay.example.com/v1/chat/completions", "gpt-6-luna", {"reasoning_effort": "low"}),
            ("openrouter", "openai/o3-mini", "", "low", "https://openrouter.ai/api/v1/chat/completions", "openai/o3-mini", {"reasoning": {"effort": "low"}}),
            ("openai-compatible", "deepseek-flash", "https://api.deepseek.com/v1", "auto", "https://api.deepseek.com/v1/chat/completions", "deepseek-flash", {"thinking": {"type": "disabled"}}),
            ("openai-compatible", "local-qwen", "http://127.0.0.1:8000/v1", "auto", "http://127.0.0.1:8000/v1/chat/completions", "local-qwen", {}),
        ]
        no_temp_models = {"o4-mini", "gpt-5-mini", "gpt-6-luna", "openai/o3-mini"}
        for prov, m_override, b_url, eff, exp_url, exp_model, exp_extra in matrix:
            with self.subTest(prov=prov, model=m_override, base_url=b_url, effort=eff):
                seen.clear()
                sub_env = {} if (prov == "openai-compatible" and "127.0.0.1" in b_url) else env
                api_override = "chat-completions" if (prov == "openai" and not b_url) else "auto"
                c = Config(
                    refiner=RefinerConfig(
                        provider=prov, model=m_override, api=api_override, base_url=b_url, effort=eff
                    )
                )
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
                if "thinking" not in exp_extra:
                    self.assertNotIn("thinking", p_json)
                if "enable_thinking" not in exp_extra:
                    self.assertNotIn("enable_thinking", p_json)

        # 2b. Default official [refiner] provider="openai" (api="auto", empty or api.openai.com base_url)
        #     routes to stateless OpenAIResponsesRefiner (/v1/responses, store=false, no service_tier)
        oai_resp_seen: list[HttpRequest] = []

        def oai_resp_tx(req: HttpRequest) -> HttpResponse:
            oai_resp_seen.append(req)
            ev = {
                "type": "response.completed",
                "response": {
                    "usage": {"input_tokens": 200, "input_tokens_details": {"cached_tokens": 128}, "output_tokens": 8},
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": "官方 Responses OK"}]}],
                },
            }
            return HttpResponse(200, {"content-type": "text/event-stream"}, f"data: {json.dumps(ev)}\n\n".encode("utf-8"))

        cfg_oai_default = Config(refiner=RefinerConfig(provider="openai"))
        r_oai_default = create_refiner(cfg_oai_default, resolve_credentials(cfg_oai_default, env), env=env, transport=oai_resp_tx)
        self.assertIsInstance(r_oai_default, OpenAIResponsesRefiner)
        self.assertNotIsInstance(r_oai_default, CodexRefiner)
        try:
            res_oai_def = r_oai_default.refine("测试初稿", session_key="s-oai-def", fallback_context="ctx_0")
            self.assertEqual((res_oai_def.provider, res_oai_def.model, res_oai_def.text), ("openai", "gpt-6-luna", "官方 Responses OK"))
            self.assertEqual((res_oai_def.input_tokens, res_oai_def.cached_input_tokens), (200, 128))
            self.assertEqual(oai_resp_seen[0].url, "https://api.openai.com/v1/responses")
            oai_p = json.loads(oai_resp_seen[0].body.decode("utf-8"))
            self.assertEqual(oai_p["store"], False)
            self.assertEqual(oai_p["stream"], True)
            self.assertEqual(oai_p["reasoning"], {"effort": "low"})
            self.assertNotIn("service_tier", oai_p)
            self.assertTrue(oai_p.get("prompt_cache_key"))
        finally:
            r_oai_default.close()

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
        self.assertEqual(gem_out.model, "gemini-3.7-flash")
        self.assertIn("models/gemini-3.7-flash:generateContent", gem_seen[0].url)
        gem_body = json.loads(gem_seen[0].body.decode("utf-8"))
        self.assertEqual(gem_body["generationConfig"]["thinkingConfig"], {"thinkingLevel": "low"})

        # Legacy Gemini 2.5 keeps thinkingBudget
        gem_seen.clear()
        cfg_gem25 = Config(refiner=RefinerConfig(provider="gemini", model="gemini-2.5-flash-lite", effort="low"))
        gem_ref25 = create_refiner(cfg_gem25, resolve_credentials(cfg_gem25, env), env=env, transport=gem_ok_tx)
        gem_ref25.refine("Gemini 2.5 初稿")
        self.assertEqual(
            json.loads(gem_seen[0].body.decode("utf-8"))["generationConfig"]["thinkingConfig"],
            {"thinkingBudget": 1024},
        )

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

        # Legacy / invalid refiner provider raises ConfigError; cross-stage alias 'doubao' normalizes to 'volcengine'
        with self.assertRaises(ConfigError):
            create_refiner(Config(refiner=RefinerConfig(provider="codex-luna")))
        with self.assertRaises(ConfigError):
            create_refiner(Config(refiner=RefinerConfig(provider="unknown-refiner")))
        ref_db_alias = create_refiner(Config(refiner=RefinerConfig(provider="doubao")), env=env)
        self.assertIsInstance(ref_db_alias, OpenAICompatibleRefiner)
        self.assertEqual(ref_db_alias.provider_name, "volcengine")

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

    def test_refiner_http_and_codex_retries_two_buckets_and_unified_deadline(self) -> None:
        reset_rate_limit_pacers()
        env = {
            "OPENAI_API_KEY": "sk-oai-" + ("9" * 16),
            "GEMINI_API_KEY": "gem_" + ("9" * 16),
        }

        # 1. OpenAICompatibleRefiner retries 429 (honoring Retry-After) + transient empty output glitch
        now_oai = [10.0]
        sleeps_oai: list[float] = []
        oai_calls = [0]

        def tx_oai(req: HttpRequest) -> HttpResponse:
            oai_calls[0] += 1
            if oai_calls[0] == 1:
                return HttpResponse(429, {"Retry-After": "1.5"}, b'{"error":{"message":"Too many requests"}}')
            if oai_calls[0] == 2:
                return HttpResponse(200, {"content-type": "application/json"}, b'{"choices":[{"message":{"content":"   "}}]}')
            return HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps({"choices": [{"message": {"content": "精修重试后成功输出。"}}]}, ensure_ascii=False).encode("utf-8"),
            )

        cfg_oai = Config(refiner=RefinerConfig(provider="openai", max_retries=2, timeout_sec=30.0))
        ref_oai = OpenAICompatibleRefiner(
            cfg_oai,
            resolve_credentials(cfg_oai, env),
            env=env,
            transport=tx_oai,
            clock_fn=lambda: now_oai[0],
            sleep_fn=lambda s: (sleeps_oai.append(round(s, 3)), now_oai.__setitem__(0, now_oai[0] + s))[0],  # type: ignore[func-returns-value]
            rand_fn=lambda: 0.0,
        )
        r_ok = ref_oai.refine("原始草稿")
        self.assertEqual((r_ok.text, r_ok.used_fallback, r_ok.retries, oai_calls[0]), ("精修重试后成功输出。", "", 2, 3))
        self.assertEqual(sleeps_oai[0], 1.5)

        # 2. Content filter / HTTP 401 on OpenAICompatibleRefiner & SAFETY block on GeminiRefiner
        #    MUST fall back to asr_draft on attempt 1 WITHOUT retrying!
        for perm_resp in (
            HttpResponse(401, {"content-type": "application/json"}, b'{"error":{"message":"bad key"}}'),
            HttpResponse(
                200,
                {"content-type": "application/json"},
                b'{"choices":[{"finish_reason":"content_filter","message":{"content":""}}]}',
            ),
        ):
            c_perm = [0]
            ref_perm = OpenAICompatibleRefiner(
                cfg_oai,
                resolve_credentials(cfg_oai, env),
                env=env,
                transport=lambda _r, resp=perm_resp: (c_perm.__setitem__(0, c_perm[0] + 1), resp)[1],
                sleep_fn=lambda _s: None,
            )
            res_p = ref_perm.refine("保留语音原文")
            self.assertEqual((res_p.text, res_p.used_fallback, res_p.retries, c_perm[0]), ("保留语音原文", "asr_draft", 0, 1))

        gem_calls = [0]

        def tx_gem(req: HttpRequest) -> HttpResponse:
            gem_calls[0] += 1
            if gem_calls[0] == 1:
                return HttpResponse(503, {"content-type": "application/json"}, b'{"error":{"status":"UNAVAILABLE","message":"overloaded"}}')
            return HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps({"candidates": [{"content": {"parts": [{"text": "Gemini 精修恢复。"}]}}]}, ensure_ascii=False).encode("utf-8"),
            )

        cfg_gem = Config(refiner=RefinerConfig(provider="gemini", max_retries=2))
        res_gem = GeminiRefiner(
            cfg_gem,
            resolve_credentials(cfg_gem, env),
            env=env,
            transport=tx_gem,
            sleep_fn=lambda _s: None,
            rand_fn=lambda: 0.0,
        ).refine("Gemini 草稿")
        self.assertEqual((res_gem.text, res_gem.used_fallback, res_gem.retries, gem_calls[0]), ("Gemini 精修恢复。", "", 1, 2))

        gem_safe_calls = [0]
        res_gem_safe = GeminiRefiner(
            cfg_gem,
            resolve_credentials(cfg_gem, env),
            env=env,
            transport=lambda _r: (
                gem_safe_calls.__setitem__(0, gem_safe_calls[0] + 1),
                HttpResponse(200, {"content-type": "application/json"}, b'{"candidates":[{"finishReason":"SAFETY"}]}'),
            )[1],
            sleep_fn=lambda _s: None,
        ).refine("安全拦截回退初稿")
        self.assertEqual((res_gem_safe.text, res_gem_safe.used_fallback, res_gem_safe.retries, gem_safe_calls[0]), ("安全拦截回退初稿", "asr_draft", 0, 1))

        # 3. CodexRefiner / OpenAIResponsesRefiner: max_retries=0 skips retry; max_retries=2 recovers
        #    on transient 503 / SSE server_error, while permanent errors fail fast on attempt 1!
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            _write_codex_auth_json(rt)
            mode_c = ["503"]

            def flaky_responder(handler, rec) -> None:
                m = mode_c[0]
                if m == "503":
                    _LocalResponsesServer.send_json(handler, 503, {"detail": "backend overloaded"})
                elif m == "flaky_once" and int(rec["seq"]) == 1:
                    _LocalResponsesServer.send_sse_200(
                        handler,
                        [
                            'data: {"type":"response.failed","response":{"error":{"code":"server_error","message":"transient glitch"}}}\n\n'
                        ],
                    )
                elif m == "empty_completed":
                    _LocalResponsesServer.send_sse_200(
                        handler, _sse_completed_frames([], usage={"input_tokens": 120, "output_tokens": 2})
                    )
                elif m.startswith("perm:"):
                    _LocalResponsesServer.send_json(handler, 400, {"detail": m[5:]})
                else:
                    _LocalResponsesServer.send_sse_200(
                        handler, _sse_completed_frames(["恢复成功"])
                    )

            srv_r = _LocalResponsesServer(responder=flaky_responder)
            try:
                # 3a. max_retries = 0 -> strictly 1 attempt
                cfg_c0 = Config(refiner=RefinerConfig(provider="codex", base_url=srv_r.base_url, max_retries=0))
                ref_c0 = CodexRefiner(cfg_c0, env={"HOME": str(rt)}, runtime_dir=rt, sleep_fn=lambda _s: None)
                try:
                    r_c0 = ref_c0.refine("不重试直接回退")
                    self.assertEqual((r_c0.text, r_c0.used_fallback, r_c0.retries, len(srv_r.requests)), ("不重试直接回退", "asr_draft", 0, 1))
                finally:
                    ref_c0.close()

                # 3b. max_retries = 2 -> attempt 1 SSE server_error, retry 1 succeeds!
                srv_r.requests.clear()
                mode_c[0] = "flaky_once"
                cfg_c2 = Config(refiner=RefinerConfig(provider="codex", base_url=srv_r.base_url, max_retries=2))
                ref_c2 = CodexRefiner(
                    cfg_c2,
                    env={"HOME": str(rt)},
                    runtime_dir=rt,
                    sleep_fn=lambda _s: None,
                    rand_fn=lambda: 0.0,
                )
                try:
                    r_c2 = ref_c2.refine("常驻恢复成功")
                    self.assertEqual((r_c2.text, r_c2.used_fallback, r_c2.retries, len(srv_r.requests)), ("恢复成功", "", 1, 2))
                finally:
                    ref_c2.close()

                # 3c. Permanent errors (invalid model, content_filter, quota) MUST fail fast on attempt 1 with 0 retries!
                for perm_msg in ("invalid model gpt-999", "blocked by content_filter", "insufficient_quota"):
                    srv_r.requests.clear()
                    mode_c[0] = f"perm:{perm_msg}"
                    ref_cp = CodexRefiner(
                        cfg_c2,
                        env={"HOME": str(rt)},
                        runtime_dir=rt,
                        sleep_fn=lambda _s: None,
                    )
                    try:
                        r_cp = ref_cp.refine("永久错误保留初稿")
                        self.assertEqual(
                            (r_cp.text, r_cp.used_fallback, r_cp.retries, len(srv_r.requests)),
                            ("永久错误保留初稿", "asr_draft", 0, 1),
                        )
                    finally:
                        ref_cp.close()

                # 3d. Short filler draft (<=6 chars) completing normally with empty output avoids pointless retries,
                #     while longer draft (>6 chars) still retries and falls back to asr_draft!
                mode_c[0] = "empty_completed"
                srv_r.requests.clear()
                ref_short = CodexRefiner(cfg_c2, env={"HOME": str(rt)}, runtime_dir=rt, sleep_fn=lambda _s: None)
                try:
                    r_sh = ref_short.refine("嗯。")
                    self.assertEqual(
                        (r_sh.text, r_sh.used_fallback, r_sh.retries, len(srv_r.requests), r_sh.input_tokens),
                        ("嗯。", "", 0, 1, 120),
                    )
                    srv_r.requests.clear()
                    r_lg = ref_short.refine("这是一段超过六个字的有效初稿")
                    self.assertEqual(
                        (r_lg.text, r_lg.used_fallback, r_lg.retries, len(srv_r.requests)),
                        ("这是一段超过六个字的有效初稿", "asr_draft", 2, 3),
                    )
                finally:
                    ref_short.close()
            finally:
                srv_r.close()

        # 4. Refiner hitting _RATE_PACER queue timeout from a prior 429 Retry-After MUST gracefully
        #    fall back to asr_draft instead of raising an unhandled TimeoutError!
        from agent_voice_input.asr_providers import _RATE_PACER, make_rate_pacer_key

        reset_rate_limit_pacers()
        oai_key = env["OPENAI_API_KEY"]
        pkey_ref = make_rate_pacer_key("openai", "https://api.openai.com/v1/chat/completions", oai_key)
        _RATE_PACER.note_rate_limit(pkey_ref, now=100.0, retry_after_sec=50.0)
        cfg_pacer_to = Config(refiner=RefinerConfig(provider="openai", max_retries=2, timeout_sec=5.0))
        ref_pto = OpenAICompatibleRefiner(
            cfg_pacer_to,
            resolve_credentials(cfg_pacer_to, env),
            env=env,
            transport=lambda _r: HttpResponse(200, {}, b"{}"),
            clock_fn=lambda: 100.0,
            sleep_fn=lambda _s: None,
        )
        res_pto = ref_pto.refine("节流超时回退原稿")
        self.assertEqual((res_pto.text, res_pto.used_fallback, res_pto.retries), ("节流超时回退原稿", "asr_draft", 0))
        self.assertIn("rate-limit pacing exceeded", res_pto.warning)
        reset_rate_limit_pacers()


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
            # Explicit run_pipeline_once cancellation hooks (is_current_seq / is_cancelled) remain honored
            plat_direct = _FakePlatform()
            res_sup_seq = run_pipeline_once(
                _voiced_wav(),
                cfg=Config(refiner=RefinerConfig(provider="off")),
                platform=plat_direct,
                asr_provider=_FakeAsr("外部淘汰"),
                refiner=create_refiner(Config(refiner=RefinerConfig(provider="off"))),
                tracker=_isolated_tracker(Path(td)),
                seq_id=7,
                is_current_seq=lambda _s: False,
                env={},
            )
            self.assertFalse(res_sup_seq.ok)
            self.assertEqual(res_sup_seq.skipped_reason, "superseded")
            res_sup_cancel = run_pipeline_once(
                _voiced_wav(),
                cfg=Config(refiner=RefinerConfig(provider="off")),
                platform=plat_direct,
                asr_provider=_FakeAsr("显式取消"),
                refiner=create_refiner(Config(refiner=RefinerConfig(provider="off"))),
                tracker=_isolated_tracker(Path(td)),
                seq_id=8,
                is_cancelled=lambda: True,
                env={},
            )
            self.assertFalse(res_sup_cancel.ok)
            self.assertEqual(res_sup_cancel.skipped_reason, "superseded")
            self.assertEqual(plat_direct.deliveries, [])

            now = [10.0]
            gate_evt = threading.Event()
            plat = _FakePlatform(paste_outcome=PasteOutcome(False, True, "focus_changed", "chrome"))
            cfg_d = Config(refiner=RefinerConfig(provider="off"))
            cfg_d.audio.beep = True
            daemon = VoiceInputDaemon(
                cfg_d,
                env={},
                runtime_dir=Path(td),
                platform=plat,
                asr_provider=_FakeAsr("前序口述不淘汰", gate_evt=gate_evt),
                refiner=create_refiner(cfg_d),
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
            self.assertEqual(daemon.handle_control({"cmd": "status"})["pending_count"], 1)

            # User starts a new recording while seq 1 is still in flight -> seq 1 is NOT superseded!
            now[0] += 0.5
            r3 = daemon.handle_trigger("ptt_start")
            self.assertEqual(r3["state"], "recording")
            self.assertGreater(r3["seq_id"], seq1)

            # Release seq 1 ASR while seq 2 is recording: seq 1 delivers (focus_changed -> copied on toast layer)
            gate_evt.set()
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and daemon.last_result.get("seq_id") != seq1:
                time.sleep(0.02)
            self.assertTrue(daemon.last_result["ok"])
            self.assertEqual(daemon.last_result.get("skipped_reason"), "")
            self.assertEqual(len(plat.deliveries), 1)
            self.assertEqual(plat.deliveries[0][0], "前序口述不淘汰")
            self.assertEqual(daemon.state, "recording")
            copied_during_rec = [e for e in plat.hud_events if e["state"] == "copied"]
            self.assertEqual(len(copied_during_rec), 1)
            self.assertEqual(copied_during_rec[0].get("layer"), "toast")
            self.assertIs(copied_during_rec[0].get("clear_main"), False)

            # Cancel current PTT recording via chord taint -> only cancels seq 2, drains back to idle
            now[0] += 0.3
            rc = daemon.handle_trigger("ptt_cancel")
            self.assertEqual(rc["state"], "idle")
            self.assertEqual(plat.canceled, 1)

            # Normal cycle with focus_changed when idle: main capsule 'copied'
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
            self.assertEqual(
                plat.huds,
                ["recording", "recognizing", "recording", "recording", "copied", "hide", "recording", "recognizing", "copied"],
            )
            self.assertEqual(plat.hud_events[2].get("bg_count"), 1)
            self.assertIsNone(plat.hud_events[3].get("bg_count"))
            self.assertIs(plat.hud_events[3].get("clear_toast"), False)
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

                # 2b. Single late reflex user toggle within 1.5s of auto-stop is absorbed once ("already_stopped")
                daemon._last_trigger_ts = -1e9
                reflex_tap = daemon.handle_trigger("toggle")
                self.assertEqual(reflex_tap.get("ignored"), "already_stopped")
                self.assertEqual(daemon.state, "idle")
                self.assertEqual(daemon.seq_id, seq_auto)

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
            _wait_for_control_socket(rt, timeout_sec=3.0)
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
            # Monotonic RPC seq_ids: beep=101, deliver_text(key_state=102, paste=103), ping=104 (ignored payload seq_id=102!)
            self.assertEqual(seen_sids, [101, 104])
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
        self.assertEqual(ver_buf.getvalue().strip(), "agent-voice-input 0.16.0")

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
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.platform", "linux"),
                mock.patch("sys.stdout", buf),
            ):
                rc = cli_mod._cmd_doctor(cfg, merged_env)
            out = buf.getvalue()
            self.assertEqual(rc, 0, out)
            self.assertIn("selected=openrouter (model=qwen/qwen3-asr-1.7b, api=audio-transcriptions, effort=auto) -> OK", out)
            self.assertIn("openrouter=OK (via OPENROUTER_API_KEY)", out)
            self.assertIn("[refiner]   OK openrouter (model=google/gemini-3.8-flash, api=chat-completions, effort=low)", out)
            self.assertNotIn(secret_or, out)

            # Doctor with codex auth.json (verifying api=responses and zero leakage of access_token / account_id)
            doc_acct_secret = "acct-doctor-secret-7744"
            doc_jwt_secret = _make_fake_jwt(account_id=doc_acct_secret)
            _write_codex_auth_json(Path(td), access_token=doc_jwt_secret, account_id=doc_acct_secret)
            cfg_doc_codex, env_doc_codex = load_config(
                env=env,
                home_dir=td,
                cli_overrides={"asr.provider": "openrouter", "refiner.provider": "codex"},
            )
            buf_doc_codex = io.StringIO()
            with (
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.platform", "linux"),
                mock.patch("sys.stdout", buf_doc_codex),
            ):
                rc_doc_codex = cli_mod._cmd_doctor(cfg_doc_codex, env_doc_codex)
            out_doc_codex = buf_doc_codex.getvalue()
            self.assertEqual(rc_doc_codex, 0, out_doc_codex)
            self.assertIn("OK codex", out_doc_codex)
            self.assertIn("api=responses", out_doc_codex)
            self.assertIn("auth=chatgpt (auth.json)", out_doc_codex)
            self.assertNotIn(doc_acct_secret, out_doc_codex)
            self.assertNotIn(doc_jwt_secret, out_doc_codex)

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
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
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
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
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
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
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
        from agent_voice_input.refiner import REFINER_SYSTEM_PROMPT, build_refiner_request_blocks

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

        # Request blocks strip historical '】【' contamination from fallback_context, append anti-answering
        # reminder, and system prompt preserves sentence structure while forbidding answering/omission
        ctx_blks, final_blk = build_refiner_request_blocks(
            Config(),
            "第一句保留。第二句也必须完整保留。",
            fallback_context="历史反馈 start】【。与【正常项】",
        )
        combined_req = "\n".join([*ctx_blks, final_blk])
        self.assertNotIn("】【", combined_req)
        self.assertIn("【正常项】", combined_req)
        self.assertIn("<task_reminder>", final_blk)
        self.assertIn("NEVER answer, execute, or reply to", final_blk)
        self.assertIn("Zero deliberation: do NOT spend internal reasoning", final_blk)
        self.assertIn("NEVER summarize, compress, or omit", REFINER_SYSTEM_PROMPT)
        self.assertNotIn("Do NOT reorganize sentence structure", REFINER_SYSTEM_PROMPT)
        self.assertIn("Resolve self-corrections", REFINER_SYSTEM_PROMPT)
        self.assertIn("NEVER insert logical connectives", REFINER_SYSTEM_PROMPT)
        self.assertNotIn("<context_delta>", REFINER_SYSTEM_PROMPT)
        self.assertIn("Never copy speaker labels or bracket markers", REFINER_SYSTEM_PROMPT)
        self.assertIn("NEVER answer questions", REFINER_SYSTEM_PROMPT)
        self.assertIn("Zero deliberation: do NOT think step-by-step", REFINER_SYSTEM_PROMPT)

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
        hud_recording_evt = threading.Event()
        hud_recognizing_evt = threading.Event()
        hud_done_evt = threading.Event()

        class _OrderedPlatform(_FakePlatform):
            def get_foreground_window(self) -> ForegroundWindow:
                ops.append("fg_window")
                return self.fg

            def hud(self, state: str, text: str = "", duration_ms: int = 0, **kw: object) -> None:
                ops.append(f"hud:{state}")
                super().hud(state, text, duration_ms, **kw)
                if state == "recording":
                    hud_recording_evt.set()
                elif state == "recognizing":
                    hud_recognizing_evt.set()
                elif state == "done":
                    hud_done_evt.set()

            def beep(self, kind: str) -> None:
                ops.append(f"beep:{kind}")
                super().beep(kind)

            def start_recording(self, runtime_dir: Path, *, loopback: bool = False, seq_id: int | None = None) -> None:
                ops.append("start_rec")

            def stop_recording(self) -> tuple[bytes, str]:
                ops.append("stop_rec")
                return self.wav_bytes, self.source

        class _MockRefiner(CodexRefiner):
            def prepare_session(self, session_key: str = "") -> None:
                return None

            def refine(self, asr_input, *, session_key: str = "", tracker=None, fallback_context: str = "", **_kw: object):
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
                self.assertTrue(hud_recording_evt.wait(timeout=2.0))
                self.assertEqual(ops[0], "start_rec")
                self.assertLess(ops.index("start_rec"), ops.index("hud:recording"))
                self.assertLess(ops.index("start_rec"), ops.index("beep:start"))
                self.assertLess(ops.index("beep:start"), ops.index("fg_window"))
                ops.clear()
                daemon._last_trigger_ts = -1e9
                r_stop = daemon.handle_trigger("toggle")
                self.assertEqual(r_stop["state"], "recognizing")
                self.assertTrue(hud_recognizing_evt.wait(timeout=2.0))
                # stop_recording() runs BEFORE HUD 'recognizing' and beep so slow HUD RPCs never extend capture!
                self.assertEqual(ops[0], "stop_rec")
                self.assertLess(ops.index("stop_rec"), ops.index("hud:recognizing"))
                self.assertLess(ops.index("stop_rec"), ops.index("beep:stop"))

                self.assertTrue(hud_done_evt.wait(timeout=2.0))
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and daemon.state != "idle":
                    time.sleep(0.02)
                # Full two-stage HUD batch order: recognizing -> refining -> done, all after stop_rec!
                hud_seq = [op for op in ops if op.startswith("hud:")]
                self.assertEqual(hud_seq, ["hud:recognizing", "hud:refining", "hud:done"])
                self.assertIn("beep:stop", ops)
                self.assertLess(ops.index("stop_rec"), ops.index("hud:recognizing"))
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
            def hud(self, state: str, text: str = "", duration_ms: int = 0, **_kw: object) -> None:
                drain_ops.append(f"hud:{state}")

            def start_recording(self, runtime_dir: Path, *, loopback: bool = False, seq_id: int | None = None) -> None:
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
                self.assertEqual(drain_ops, ["stop_rec_enter"])
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

            def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None, **_kw: object) -> AsrResult:
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
            def prepare_session(self, session_key: str = "") -> None:
                return None

            def refine(self, asr_input, *, session_key: str = "", tracker=None, fallback_context: str = "", **_kw: object):
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

        # 2b. Summary telemetry records asr_retries, refiner_retries & redacted refiner_warning;
        #     multi-chunk sibling failure aborts sibling backoff promptly with real root-cause AsrError!
        with tempfile.TemporaryDirectory() as td_sum:
            secret_tok = "sk-summary-secret-token-7788"

            class _RetryAsr(BaseAsrProvider):
                provider_name = "openai"

                def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None, **_kw: object) -> AsrResult:
                    return AsrResult("初稿文本", "openai", "gpt-4o-mini-transcribe", 25, retries=2)

            class _RetryWarnRefiner(OpenAICompatibleRefiner):
                def refine(self, asr_draft, **kwargs):  # type: ignore[override]
                    return RefinerResult(
                        "初稿文本",
                        "openai",
                        "gpt-4o-mini",
                        40,
                        used_fallback="asr_draft",
                        warning=f"upstream 503 with key={secret_tok}",
                        retries=2,
                    )

            env_sum = {"OPENAI_API_KEY": secret_tok}
            cfg_sum = Config(refiner=RefinerConfig(provider="openai"))
            d_sum = VoiceInputDaemon(
                cfg_sum,
                env=env_sum,
                runtime_dir=Path(td_sum),
                platform=_FakePlatform(),
                asr_provider=_RetryAsr(cfg_sum, resolve_credentials(cfg_sum, env_sum), env=env_sum),
                refiner=_RetryWarnRefiner(cfg_sum, resolve_credentials(cfg_sum, env_sum), env=env_sum),
                tracker=_isolated_tracker(Path(td_sum)),
            )
            try:
                d_sum.handle_trigger("toggle")
                d_sum._last_trigger_ts = -1e9
                r_s = d_sum.handle_trigger("toggle")
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and d_sum.last_result.get("seq_id") != r_s["seq_id"]:
                    time.sleep(0.01)
                self.assertEqual(d_sum.last_result.get("asr_retries"), 2)
                self.assertEqual(d_sum.last_result.get("refiner_retries"), 2)
                self.assertIn("upstream 503", d_sum.last_result.get("refiner_warning", ""))
                self.assertNotIn(secret_tok, json.dumps(d_sum.last_result, ensure_ascii=False))
            finally:
                d_sum.close()

        # Multi-chunk sibling abort: chunk 0 enters 4.0s backoff, chunk 1 fails with HTTP 401 ->
        # chunk 0 aborts backoff within <0.4s and _transcribe_wav_chunks raises chunk 1's 401 error!
        from agent_voice_input.asr_providers import create_asr_provider

        reset_rate_limit_pacers()
        chunk_idx = [0]
        idx_lock = threading.Lock()
        both_in = threading.Barrier(2, timeout=1.0)

        def tx_sibling_abort(_req: HttpRequest) -> HttpResponse:
            both_in.wait()
            with idx_lock:
                chunk_idx[0] += 1
                n = chunk_idx[0]
            if n == 1:
                return HttpResponse(429, {"Retry-After": "4.0"}, b'{"error":{"message":"slow"}}')
            time.sleep(0.04)
            return HttpResponse(401, {"content-type": "application/json"}, b'{"error":{"message":"Invalid API key"}}')

        env_ab = {"OPENAI_API_KEY": "sk-oai-" + ("z" * 16)}
        cfg_ab = Config()
        cfg_ab.asr.provider = "openai"
        cfg_ab.asr.hotwords_file = ""
        prov_ab = create_asr_provider(cfg_ab, resolve_credentials(cfg_ab, env_ab), env=env_ab, transport=tx_sibling_abort)
        two_chunk_wav = pack_pcm16_mono_wav([3500, -3500] * 12000 + [0] * 6400 + [3500, -3500] * 12000)
        t_ab0 = time.monotonic()
        with self.assertRaises(AsrError) as cm_ab:
            _transcribe_wav_chunks(two_chunk_wav, eff_asr=prov_ab, asr_ctx=None, floor_dbfs=-50.0, max_chunk_sec=2.1)
        self.assertLess(time.monotonic() - t_ab0, 0.8)
        self.assertIn("Invalid API key", str(cm_ab.exception))
        reset_rate_limit_pacers()

        # 3. Empty Stage 1 finishing during seq 2 recording keeps main HUD on 'recording' (clear_main=False) & suppresses error beep
        sup_asr_in = threading.Event()
        sup_asr_rel = threading.Event()

        class _EmptySlowAsr(BaseAsrProvider):
            provider_name = "dashscope"

            def __init__(self) -> None:
                pass

            def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None, **_kw: object) -> AsrResult:
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
                self.assertEqual(daemon_sup.last_result.get("skipped_reason"), "empty_asr")
                self.assertEqual(daemon_sup.state, "recording")
                self.assertEqual(plat_sup.huds, [*huds_before, "recording", "asr_empty_retry"])
                self.assertIs(plat_sup.hud_events[-2].get("clear_toast"), False)
                self.assertIs(plat_sup.hud_events[-1].get("clear_main"), False)
                self.assertEqual(plat_sup.beeps, beeps_before)
                daemon_sup._last_trigger_ts = -1e9
                daemon_sup.handle_trigger("ptt_cancel")
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
        # 1. Config & env overrides for [hotkey] scope & backward-compatible only_in_terminals
        self.assertEqual(Config().hotkey.scope, "all")
        self.assertIs(Config().hotkey.only_in_terminals, False)
        self.assertIs(Config().hotkey.yield_remote_desktop, True)
        with tempfile.TemporaryDirectory() as td_cfg:
            home = Path(td_cfg)
            cfg_dir = home / ".config" / "agent-voice-input"
            cfg_dir.mkdir(parents=True)
            (cfg_dir / "config.toml").write_text("[hotkey]\nonly_in_terminals = false\n", encoding="utf-8")
            c_false, _ = load_config(env={"HOME": str(home)}, home_dir=home)
            self.assertIs(c_false.hotkey.only_in_terminals, False)

            # Ordinary *_ONLY_IN_TERMINALS env vars do not override config.toml; cli_overrides do
            c_env_ignored, _ = load_config(
                env={"HOME": str(home), "AGENT_VOICE_INPUT_ONLY_IN_TERMINALS": "true"},
                home_dir=home,
            )
            self.assertIs(c_env_ignored.hotkey.only_in_terminals, False)

            c_cli_true, _ = load_config(
                env={"HOME": str(home)},
                home_dir=home,
                cli_overrides={"hotkey.only_in_terminals": True},
            )
            self.assertIs(c_cli_true.hotkey.only_in_terminals, True)

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
                def start_recording(self, runtime_dir: Path, *, loopback: bool = False, seq_id: int | None = None) -> None:
                    start_calls[0] += 1

            plat = _CountingFakePlatform(paste_outcome=PasteOutcome(False, True, "focus_changed", "mstsc"))
            cfg = Config(hotkey=HotkeyConfig(scope="terminals"), refiner=RefinerConfig(provider="off"))
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
                    (ForegroundWindow(10, "mstsc.exe", "Remote Desktop"), "remote_client"),
                    (ForegroundWindow(11, "chrome.exe", "Google"), "non_terminal"),
                    (ForegroundWindow(0, "", ""), "non_terminal"),
                    (ForegroundWindow(0, "Windows App", "Win11", bundle_id="com.microsoft.rdc.macos"), "remote_client"),
                    (ForegroundWindow(0, "Safari", "Apple", bundle_id="com.apple.Safari"), "non_terminal"),
                ]
                for w_bad, exp_ignored in non_terms:
                    for act in ("toggle", "ptt_start"):
                        resp = daemon.handle_trigger(act, w_bad, from_hotkey=True)
                        self.assertEqual(
                            resp,
                            {"ok": False, "ignored": exp_ignored, "state": "idle", "seq_id": 0},
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

                # only_in_terminals = False maps to scope="all": non-remote apps start recording,
                # while remote desktop clients still yield silently until yield_remote_desktop=False
                daemon.cfg.hotkey.only_in_terminals = False
                self.assertEqual(daemon.cfg.hotkey.scope, "all")
                now[0] += 0.5
                r_rdp_yield = daemon.handle_trigger("toggle", rdp_win, from_hotkey=True)
                self.assertEqual(r_rdp_yield["ignored"], "remote_client")
                r_chrome = daemon.handle_trigger(
                    "toggle", ForegroundWindow(301, "chrome.exe", "Docs"), from_hotkey=True
                )
                self.assertTrue(r_chrome["ok"])
                self.assertEqual(r_chrome["state"], "recording")
                now[0] += 0.3
                daemon.handle_trigger("ptt_cancel")

                daemon.cfg.hotkey.yield_remote_desktop = False
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
            _wait_for_control_socket(rt_srv, timeout_sec=3.0)
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

            def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None, **_kw: object) -> AsrResult:
                with self._lk:
                    self.n += 1
                    idx = self.n
                return AsrResult(f"第{idx}段内容", "dashscope", "qwen3-asr-flash", 25, False)

        class _EchoRefiner(OpenAICompatibleRefiner):
            provider_name = "dashscope"

            def __init__(self) -> None:
                self.seen_drafts: list[str] = []

            def refine(self, asr_input, *, session_key="", tracker=None, fallback_context="", **_kw: object):  # type: ignore[override]
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

                def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None, **_kw: object) -> AsrResult:
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

                def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None, **_kw: object) -> AsrResult:
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

                def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None, **_kw: object) -> AsrResult:
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
                self.assertEqual(plat_clip_fail.huds[-1], "paste_failed_retry")
                self.assertFalse(daemon2.last_result["ok"])
                self.assertEqual(daemon2.last_result["delivery_status"], "undelivered_recoverable")
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
                unittest.mock.patch("agent_voice_input.daemon_pipeline.create_asr_provider", return_value=_FakeAsr("找回的 post-studio 长语音内容")),
                redirect_stdout(out_run),
                redirect_stderr(err_run),
            ):
                rc_r = cli_mod.main(["retry", "post-studio", "--refiner", "off", "--dry-run", "--title", "agy"])
            self.assertEqual(rc_r, 0)
            self.assertEqual(out_run.getvalue().strip(), "找回的 post-studio 长语音内容")
            self.assertEqual(list_failed_audios(rt), before_files)  # retry Neither deletes nor duplicates stashed WAVs!

    def test_no_microphone_fail_fast_and_mid_recording_disconnect_recovery(self) -> None:
        import subprocess
        from agent_voice_input.config import AudioConfig
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
            mplat.hud = lambda st, text="", duration_ms=0, **_kw: hud_events.append((st, duration_ms))  # type: ignore[method-assign]
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
            wplat._shim_rpc = lambda req, timeout_sec=4.0, **_kw: {"ok": False, "rec_error": "wasapi_endpoint_hr_0x80070490"}  # type: ignore[method-assign]
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
            mplat_short.hud = lambda st, text="", duration_ms=0, **_kw: hud_short.append((st, duration_ms))  # type: ignore[method-assign]
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
                r_short_start = daemon_short.handle_trigger("toggle", ForegroundWindow(0, "Ghostty", "agy", "com.mitchellh.ghostty"))
                self.assertTrue(r_short_start["ok"])
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and (
                    daemon_short.state != "idle" or ("mic_disconnected", 2200) not in hud_short or not beep_short
                ):
                    time.sleep(0.02)
                self.assertEqual(daemon_short.state, "idle")
                self.assertIsNone(daemon_short._max_dur_timer)
                self.assertEqual(daemon_short.last_result.get("skipped_reason"), "mic_disconnected")
                self.assertEqual(hud_short[-1], ("mic_disconnected", 2200))
                self.assertEqual(beep_short.count("error"), 1)
                # Racing max-duration watchdog callback for the disconnected seq_id must exit cleanly without
                # emitting stale HUD updates OR consuming _recorder_lost_stop_absorbed!
                daemon_short._on_max_duration_timeout(int(r_short_start["seq_id"]))
                self.assertEqual(hud_short[-1], ("mic_disconnected", 2200))
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
                def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None, **_kw: object) -> AsrResult:
                    asr_entered.set()
                    release_asr.wait(timeout=2.0)
                    return AsrResult("断麦前已说的前半段内容", "fake", "m", 15)

            hud_mid: list[str] = []
            beep_mid: list[str] = []
            pasted_mid: list[str] = []
            mplat2 = MacOSPlatform(Config(), env={"HOME": td}, cache_dir=Path(td) / "cache2", shim_cmd_factory=lambda: None)
            mplat2._ff_rec = FfmpegWavRecorder(cmd_factory=lambda _f, _s: [sys.executable, "-c", mid_drop_src])
            mplat2.hud = lambda st, text="", duration_ms=0, **_kw: hud_mid.append(st)  # type: ignore[method-assign]
            mplat2.beep = lambda kind: beep_mid.append(kind)  # type: ignore[method-assign]
            mplat2.resolve_foreground_window = lambda win=None: ForegroundWindow(0, "Ghostty", "agy", "com.mitchellh.ghostty")  # type: ignore[method-assign]
            mplat2.deliver_text = lambda txt, seq_id, target_hwnd=0, *, origin_window=None, **_kw: (pasted_mid.append(txt) or PasteOutcome(True, True, "ok", "Ghostty"))  # type: ignore[method-assign]

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
                while time.monotonic() < deadline and (not pasted_mid or daemon2.state != "idle" or "done" not in hud_mid):
                    time.sleep(0.02)
                self.assertEqual(pasted_mid, ["断麦前已说的前半段内容"])
                self.assertEqual(daemon2.state, "idle")
                self.assertEqual(beep_mid.count("error"), 1)
                self.assertIn("recognizing", hud_mid)
                self.assertIn("mic_disconnected", hud_mid)
                self.assertEqual(hud_mid[-1], "done")
                self.assertIs(daemon2.last_result.get("capture_interrupted"), True)
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

            # 5. Cross-feature seam: max_duration_sec auto-stop on a live FfmpegWavRecorder subprocess streams
            # live RMS levels (~20 Hz 1600-byte chunks) AND stops ffmpeg cleanly without firing a bogus
            # _on_recorder_lost / mic_disconnected callback!
            live_stream_src = (
                "import os, sys, time\n"
                "chunk = b'\\xe8\\x0e\\x18\\xf1' * 400\n"  # 1600 bytes (50ms @ 16kHz s16le voiced audio)
                "while True:\n"
                "    try:\n"
                "        os.write(1, chunk)\n"
                "    except OSError:\n"
                "        break\n"
                "    time.sleep(0.015)\n"
            )
            cfg_max = Config(
                audio=AudioConfig(max_duration_sec=0.14, min_duration_sec=0.05, min_voiced_sec=0.03, tail_drain_sec=0.0),
                refiner=RefinerConfig(provider="off"),
            )
            hud_max: list[str] = []
            beep_max: list[str] = []
            pasted_max: list[str] = []
            levels_seen: list[float] = []
            mplat_max = MacOSPlatform(cfg_max, env={"HOME": td}, cache_dir=Path(td) / "cache_max", shim_cmd_factory=lambda: None)
            mplat_max._ff_rec = FfmpegWavRecorder(cmd_factory=lambda _f, _s: [sys.executable, "-c", live_stream_src])
            mplat_max._has_managed_shim = lambda: True  # type: ignore[method-assign]
            mplat_max.hud_level = lambda lvl: levels_seen.append(lvl)  # type: ignore[method-assign]
            mplat_max.hud = lambda st, text="", duration_ms=0, **_kw: hud_max.append(st)  # type: ignore[method-assign]
            mplat_max.beep = lambda kind: beep_max.append(kind)  # type: ignore[method-assign]
            mplat_max.resolve_foreground_window = lambda win=None: ForegroundWindow(0, "Ghostty", "agy", "com.mitchellh.ghostty")  # type: ignore[method-assign]
            mplat_max.deliver_text = lambda txt, seq_id, target_hwnd=0, *, origin_window=None, **_kw: (pasted_max.append(txt) or PasteOutcome(True, True, "ok", "Ghostty"))  # type: ignore[method-assign]

            daemon_max = VoiceInputDaemon(
                cfg_max,
                env={"HOME": td},
                runtime_dir=rt,
                platform=mplat_max,
                asr_provider=_FakeAsr("最长录音自动完成不误报断麦"),
                refiner=create_refiner(cfg_max),
                tracker=_isolated_tracker(rt),
            )
            try:
                r_max = daemon_max.handle_trigger("toggle", ForegroundWindow(0, "Ghostty", "agy", "com.mitchellh.ghostty"))
                self.assertTrue(r_max["ok"])
                deadline = time.monotonic() + 2.5
                while time.monotonic() < deadline and (not pasted_max or daemon_max.state != "idle" or "done" not in hud_max):
                    time.sleep(0.02)
                self.assertEqual(pasted_max, ["最长录音自动完成不误报断麦"])
                self.assertEqual(daemon_max.state, "idle")
                self.assertNotIn("mic_disconnected", hud_max)
                self.assertEqual(hud_max[-1], "done")
                self.assertEqual(beep_max, ["start", "stop"])
                self.assertGreaterEqual(len(levels_seen), 2)
                self.assertTrue(any(v > 0.1 for v in levels_seen))
            finally:
                daemon_max.close()

    def test_daemon_start_recording_precedes_slow_resolver_and_cancels_on_error(self) -> None:
        in_resolver = threading.Event()
        release_resolver = threading.Event()
        events: list[str] = []
        rec_active = [False]
        rec_active_during_resolve = [False]
        fail_resolve = [False]
        fail_cancel = [False]
        start_exc: list[Exception | None] = [None]
        stop_exc: list[Exception | None] = [None]
        hud_records: list[tuple[str, int, dict[str, object]]] = []

        class _SlowResolvePlatform(_FakePlatform):
            def hud(self, state: str, text: str = "", duration_ms: int = 0, **_kw: object) -> None:
                events.append(f"hud:{state}")
                hud_records.append((state, duration_ms, dict(_kw)))
                super().hud(state, text, duration_ms)

            def beep(self, kind: str) -> None:
                events.append(f"beep:{kind}")
                super().beep(kind)

            def start_recording(self, runtime_dir: Path, *, loopback: bool = False, seq_id: int | None = None) -> None:
                events.append(f"start_rec:loopback={loopback}")
                if start_exc[0] is not None:
                    raise start_exc[0]
                rec_active[0] = True

            def stop_recording(self) -> tuple[bytes, str]:
                rec_active[0] = False
                if stop_exc[0] is not None:
                    raise stop_exc[0]
                return super().stop_recording()

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
                    ["start_rec:loopback=False", "hud:recording", "beep:start", "resolve_enter"],
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
                    ["start_rec:loopback=True", "hud:recording", "resolve_enter", "resolve_exit"],
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
                        "start_rec:loopback=False",
                        "hud:recording",
                        "beep:start",
                        "resolve_enter",
                        "cancel_rec",
                        "hud:error",
                        "beep:error",
                    ],
                )

                # 4. start_recording raising NoMicrophoneError / MicrophoneUnavailableError NEVER flashes hud:recording!
                fail_resolve[0] = False
                fail_cancel[0] = False
                for mic_err, exp_hud, exp_reason in (
                    (NoMicrophoneError("no input device"), "hud:no_mic", "no_mic"),
                    (
                        MicrophoneUnavailableError(
                            "bt switching",
                            reason_code="mic_unavailable",
                            hud_state="mic_unavailable",
                        ),
                        "hud:mic_unavailable",
                        "mic_unavailable",
                    ),
                ):
                    events.clear()
                    start_exc[0] = mic_err
                    daemon._last_trigger_ts = -1e9
                    r_mic = daemon.handle_trigger("toggle")
                    self.assertFalse(r_mic["ok"])
                    self.assertEqual(r_mic.get("skipped_reason"), exp_reason)
                    self.assertEqual(daemon.last_result.get("skipped_reason"), exp_reason)
                    self.assertEqual(daemon.state, "idle")
                    self.assertNotIn("hud:recording", events)
                    self.assertNotIn("cancel_rec", events)
                    self.assertEqual(events, ["start_rec:loopback=False", exp_hud, "beep:error"])
                start_exc[0] = None

                # 5. stop_recording raising NoMicrophoneError / MicrophoneUnavailableError (e.g. first TCC dialog denied)
                for stop_err, exp_state, exp_reason, exp_dur in (
                    (
                        MicrophoneUnavailableError(
                            "macOS Microphone access is denied",
                            reason_code="mic_permission",
                            hud_state="mic_permission",
                        ),
                        "mic_permission",
                        "mic_permission",
                        0,
                    ),
                    (NoMicrophoneError("mic unplugged on stop"), "no_mic", "no_mic", 2200),
                ):
                    daemon._last_trigger_ts = -1e9
                    self.assertTrue(daemon.handle_trigger("toggle")["ok"])
                    self.assertEqual(daemon.state, "recording")
                    events.clear()
                    hud_records.clear()
                    stop_exc[0] = stop_err
                    daemon._last_trigger_ts = -1e9
                    r_stop = daemon.handle_trigger("toggle")
                    self.assertFalse(r_stop["ok"])
                    self.assertEqual(r_stop.get("skipped_reason"), exp_reason)
                    self.assertEqual(daemon.last_result.get("skipped_reason"), exp_reason)
                    self.assertEqual(daemon.state, "idle")
                    self.assertEqual(events, [f"hud:{exp_state}", "beep:error"])
                    self.assertEqual(hud_records[-1][0], exp_state)
                    self.assertEqual(hud_records[-1][1], exp_dur)
                    self.assertTrue(hud_records[-1][2].get("clear_main"))
                stop_exc[0] = None
            finally:
                daemon.close()

    def test_daemon_recorder_recovered_and_ffmpeg_eof_stall_watchdog(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            hud_calls: list[tuple[str, dict[str, object]]] = []

            class _RecoverablePlatform(_FakePlatform):
                def __init__(self) -> None:
                    super().__init__()
                    self.rec_cb = None

                def set_recorder_recovered_callback(self, cb) -> None:
                    self.rec_cb = cb

                def hud(self, state: str, text: str = "", duration_ms: int = 0, **kw: object) -> None:
                    hud_calls.append((state, dict(kw)))
                    super().hud(state, text, duration_ms)

            plat = _RecoverablePlatform()
            plat.wav_to_return = pack_pcm16_mono_wav([3200, -3200] * 4800)
            cfg = Config(refiner=RefinerConfig(provider="off"))
            daemon = VoiceInputDaemon(
                cfg,
                env={"HOME": td},
                runtime_dir=rt,
                platform=plat,
                asr_provider=_FakeAsr("自动恢复后的完整语音"),
                refiner=create_refiner(cfg),
                tracker=_isolated_tracker(rt),
            )
            try:
                self.assertIsNotNone(plat.rec_cb)
                r_start = daemon.handle_trigger("toggle", ForegroundWindow(42, "Ghostty", "codex"))
                self.assertTrue(r_start["ok"])
                hud_calls.clear()

                # < 250ms recovery: silently recorded without toast HUD
                assert plat.rec_cb is not None
                plat.rec_cb(120)
                self.assertEqual(hud_calls, [])

                # >= 250ms recovery: emits audio_recovered toast with clear_main=False
                plat.rec_cb(320)
                self.assertEqual(hud_calls, [("audio_recovered", {"clear_main": False})])

                daemon._last_trigger_ts = -1e9
                r_stop = daemon.handle_trigger("toggle")
                self.assertTrue(r_stop["ok"])
                deadline = time.monotonic() + 2.5
                while time.monotonic() < deadline and daemon.last_result.get("seq_id") != r_stop["seq_id"]:
                    time.sleep(0.02)
                self.assertTrue(daemon.last_result["ok"])
                self.assertEqual(daemon.last_result.get("recovery_count"), 2)
                self.assertEqual(daemon.last_result.get("recovery_gap_ms"), 440)
            finally:
                daemon.close()

            # FfmpegWavRecorder: rc == 0 unexpected EOF after initial audio fires on_unexpected_exit & records unexpected_eof_rc0
            eof_evt = threading.Event()
            eof_src = (
                "import os, sys, time\n"
                "os.write(1, b'\\x10\\x00' * 1600)\n"
                "sys.stdout.flush()\n"
                "time.sleep(0.05)\n"
                "sys.exit(0)\n"
            )
            rec_eof = FfmpegWavRecorder(cmd_factory=lambda _f, _s: [sys.executable, "-c", eof_src])
            rec_eof.start(
                "avfoundation",
                ":default",
                rt,
                {"HOME": td},
                verify_startup=True,
                startup_timeout_sec=0.35,
                stall_timeout_sec=1.2,
                on_unexpected_exit=eof_evt.set,
            )
            try:
                self.assertTrue(eof_evt.wait(timeout=1.5))
                self.assertEqual(rec_eof.last_returncode, 0)
                self.assertIn("unexpected_eof_rc0", rec_eof.last_stderr)
            finally:
                rec_eof.stop(drain_sec=0.0)

            # FfmpegWavRecorder: stall_timeout_sec=0.12 frame stall watchdog kills frozen stream & tags [stream_stalled]
            stall_evt = threading.Event()
            stall_src = (
                "import os, sys, time\n"
                "os.write(1, b'\\x10\\x00' * 1600)\n"
                "sys.stdout.flush()\n"
                "time.sleep(10.0)\n"
            )
            rec_stall = FfmpegWavRecorder(cmd_factory=lambda _f, _s: [sys.executable, "-c", stall_src])
            t_stall = time.monotonic()
            rec_stall.start(
                "avfoundation",
                ":default",
                rt,
                {"HOME": td},
                verify_startup=True,
                startup_timeout_sec=0.35,
                stall_timeout_sec=0.12,
                on_unexpected_exit=stall_evt.set,
            )
            try:
                self.assertTrue(stall_evt.wait(timeout=1.5))
                self.assertLess(time.monotonic() - t_stall, 1.2)
                self.assertIn("[stream_stalled]", rec_stall.last_stderr)
            finally:
                rec_stall.stop(drain_sec=0.0)

            # FfmpegWavRecorder: 0-frame hang during verify_startup fails fast with MicrophoneUnavailableError
            zero_hang = FfmpegWavRecorder(cmd_factory=lambda _f, _s: [sys.executable, "-c", "import time; time.sleep(5)"])
            with self.assertRaises(MicrophoneUnavailableError) as zh_ctx:
                zero_hang.start("avfoundation", ":default", rt, {"HOME": td}, verify_startup=True, startup_timeout_sec=0.08)
            self.assertEqual(zh_ctx.exception.hud_state, "mic_unavailable")
            self.assertIn("startup_timeout_no_frames", zero_hang.last_stderr)
            self.assertFalse(zero_hang.is_running)

            # WslPlatform: rec_lost arriving during rec_start RPC before _rec_mode='winmm' is delivered cleanly
            from agent_voice_input.config import AudioConfig
            from agent_voice_input.platform_wsl import WslPlatform

            wplat_race = WslPlatform(Config(audio=AudioConfig(wsl_source="winmm")), env={"HOME": td})
            early_lost_evt = threading.Event()
            wplat_race.set_recorder_lost_callback(lambda seq_id=None: early_lost_evt.set())

            seen_rec_gens: list[int] = []

            def _racing_rpc(payload: dict[str, object], timeout_sec: float = 4.0, **kw: object) -> dict[str, object]:
                if payload.get("cmd") == "rec_start":
                    rg = int(payload.get("rec_gen") or 0)
                    seen_rec_gens.append(rg)
                    if len(seen_rec_gens) == 1:
                        wplat_race._on_shim_internal_event(
                            {"event": "rec_lost", "reason": "wasapi_device_unplugged", "rec_gen": rg}
                        )
                    return {"ok": True}
                return {"ok": True}

            wplat_race._ensure_shim = lambda *, ignore_cooldown=False: True  # type: ignore[method-assign]
            wplat_race._shim_rpc = _racing_rpc  # type: ignore[method-assign]
            wplat_race.start_recording(rt, seq_id=1)
            self.assertEqual(seen_rec_gens, [1])
            self.assertTrue(early_lost_evt.wait(timeout=1.0))
            self.assertEqual(wplat_race.last_audio_error, "wasapi_device_unplugged")
            wplat_race.cancel_recording()

            # Stale queued Windows rec_lost / rec_recovered from rec_gen=1 must NEVER stop a newly started session!
            wplat_stale = WslPlatform(Config(audio=AudioConfig(wsl_source="winmm")), env={"HOME": td})
            stale_gens: list[int] = []
            wplat_stale._ensure_shim = lambda *, ignore_cooldown=False: True  # type: ignore[method-assign]
            wplat_stale._shim_rpc = lambda payload, timeout_sec=4.0, **kw: (  # type: ignore[method-assign]
                (stale_gens.append(int(payload.get("rec_gen") or 0)) or {"ok": True})
                if payload.get("cmd") == "rec_start"
                else {"ok": True}
            )
            wplat_stale.is_allowed_terminal = lambda _w: True  # type: ignore[method-assign]
            wplat_stale.get_foreground_window = lambda: ForegroundWindow(1, "WindowsTerminal", "codex")  # type: ignore[method-assign]
            wplat_stale.hud = lambda *a, **k: None  # type: ignore[method-assign]
            wplat_stale.beep = lambda *a, **k: None  # type: ignore[method-assign]
            daemon_w = VoiceInputDaemon(
                cfg,
                env={"HOME": td},
                runtime_dir=rt,
                platform=wplat_stale,
                asr_provider=_FakeAsr("ok"),
                refiner=create_refiner(cfg),
                tracker=_isolated_tracker(rt),
            )
            try:
                self.assertTrue(daemon_w.handle_trigger("ptt_start")["ok"])
                gen1 = stale_gens[-1]
                daemon_w._last_trigger_ts = -1e9
                self.assertTrue(daemon_w.handle_trigger("ptt_cancel")["ok"])
                daemon_w._last_trigger_ts = -1e9
                self.assertTrue(daemon_w.handle_trigger("ptt_start")["ok"])
                self.assertEqual((daemon_w.state, daemon_w.seq_id), ("recording", 3))
                # Replay queued old events from gen1 AND unnumbered legacy events — neither may touch seq=3
                wplat_stale._on_shim_internal_event({"event": "rec_recovered", "gap_ms": 400, "rec_gen": gen1})
                wplat_stale._on_shim_internal_event({"event": "rec_lost", "reason": "wasapi_device_unplugged", "rec_gen": gen1})
                wplat_stale._on_shim_internal_event({"event": "rec_lost", "reason": "wasapi_device_unplugged"})
                self.assertEqual((daemon_w.state, daemon_w.seq_id), ("recording", 3))
                self.assertEqual(daemon_w._rec_recovery_count, 0)
                self.assertEqual(wplat_stale.last_audio_error, "")
            finally:
                daemon_w.close()

            # VoiceInputDaemon: _on_recorder_recovered arriving inside start_recording flushes AFTER recording(clear_toast=True)
            hud_seq_order: list[tuple[str, object]] = []

            class _EarlyRecoverPlatform(_FakePlatform):
                def hud(self, state: str, text: str = "", duration_ms: int = 0, **kw: object) -> None:
                    hud_seq_order.append((state, kw.get("clear_toast")))
                    super().hud(state, text, duration_ms)

                def start_recording(self, runtime_dir: Path, *, loopback: bool = False, seq_id: int | None = None) -> None:
                    daemon_er._on_recorder_recovered(350)

            plat_er = _EarlyRecoverPlatform()
            daemon_er = VoiceInputDaemon(
                cfg,
                env={"HOME": td},
                runtime_dir=rt,
                platform=plat_er,
                asr_provider=_FakeAsr("ok"),
                refiner=create_refiner(cfg),
                tracker=_isolated_tracker(rt),
            )
            try:
                self.assertTrue(daemon_er.handle_trigger("toggle")["ok"])
                self.assertEqual(hud_seq_order, [("recording", True), ("audio_recovered", None)])
                # Cancel seq=1 and start a fresh recording seq=3; stale callbacks from seq=1 must be ignored
                daemon_er._last_trigger_ts = -1e9
                self.assertTrue(daemon_er.handle_trigger("ptt_cancel")["ok"])
                daemon_er._last_trigger_ts = -1e9
                r_s3 = daemon_er.handle_trigger("ptt_start")
                self.assertTrue(r_s3["ok"])
                self.assertEqual((daemon_er.state, daemon_er.seq_id), ("recording", 3))
                self.assertEqual((daemon_er._rec_recovery_count, daemon_er._rec_recovery_gap_ms), (1, 350))
                daemon_er._on_recorder_recovered(500, seq_id=1)
                daemon_er._on_recorder_lost(seq_id=1)
                self.assertEqual((daemon_er.state, daemon_er.seq_id), ("recording", 3))
                self.assertEqual((daemon_er._rec_recovery_count, daemon_er._rec_recovery_gap_ms), (1, 350))
            finally:
                daemon_er.close()

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

            # 1. `start` when already running: prints aligned reload hint with or without CLI overrides
            buf_plain = io.StringIO()
            with (
                mock.patch(
                    "agent_voice_input.control_ipc.send_control_command",
                    return_value={"ok": True, "pid": 1234, "state": "idle"},
                ),
                mock.patch("sys.stdout", buf_plain),
            ):
                rc1 = cli_mod._cmd_start(parser.parse_args(["start"]), env)
            self.assertEqual(rc1, 0)
            self.assertEqual(
                buf_plain.getvalue().strip(),
                "Daemon    already running (pid=1234, idle)\n"
                "Next      run 'agent-voice-input restart' to reload config",
            )

            buf_ov = io.StringIO()
            with (
                mock.patch(
                    "agent_voice_input.control_ipc.send_control_command",
                    return_value={"ok": True, "pid": 1234, "state": "idle"},
                ),
                mock.patch("sys.stdout", buf_ov),
            ):
                rc2 = cli_mod._cmd_start(parser.parse_args(["start", "--asr", "groq"]), env)
            self.assertEqual(rc2, 0)
            self.assertEqual(
                buf_ov.getvalue().strip(),
                "Daemon    already running (pid=1234, idle)\n"
                "Notice    new CLI flag passed (--asr-provider)\n"
                "Next      run 'agent-voice-input restart' to apply changes",
            )

            # 2. `restart` in main(): stops then starts cleanly; bad config fails BEFORE stopping live daemon
            call_seq: list[str] = []
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch.object(cli_mod, "_cmd_stop", side_effect=lambda _e, **_kw: (call_seq.append("stop") or 0)),
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
                mock.patch.object(cli_mod, "_cmd_stop", side_effect=lambda _e, **_kw: (call_seq.append("stop") or 0)),
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
                mock.patch("agent_voice_input.control_ipc.send_control_command", side_effect=ConnectionRefusedError("dead")),
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
                mock.patch("agent_voice_input.control_ipc.send_control_command", return_value={"ok": True}),
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
                mock.patch(
                    "agent_voice_input.control_ipc.send_control_command",
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
                mock.patch("agent_voice_input.control_ipc.send_control_command", side_effect=ConnectionRefusedError("down")),
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

            # 6. CLI `toggle` uses timeout_sec >= 20.0 (covering 15s first-time TCC prompt) and distinguishes
            #    TimeoutError / socket.timeout from "daemon is not running"; `_window_from_mapping` parses tmux fields
            import socket as sock_mod
            from agent_voice_input.daemon import _window_from_mapping

            mapped_win = _window_from_mapping(
                {
                    "hwnd": 42,
                    "process": "Terminal",
                    "tty": "/dev/ttys004",
                    "tmux_socket": "/tmp/avi.sock",
                    "tmux_pane": "%3",
                }
            )
            self.assertEqual((mapped_win.tmux_socket, mapped_win.tmux_pane), ("/tmp/avi.sock", "%3"))

            seen_toggle_timeouts: list[float] = []

            def fake_toggle_ok(_rt_dir: Path, payload: dict[str, object], timeout_sec: float = 8.0) -> dict[str, object]:
                self.assertEqual(payload.get("cmd"), "toggle")
                seen_toggle_timeouts.append(timeout_sec)
                return {"ok": True, "state": "recording", "seq_id": 1}

            toggle_out = io.StringIO()
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("agent_voice_input.control_ipc.send_control_command", side_effect=fake_toggle_ok),
                mock.patch("sys.stdout", toggle_out),
            ):
                rc_tog_ok = cli_mod.main(["toggle"])
            self.assertEqual(rc_tog_ok, 0)
            self.assertEqual(len(seen_toggle_timeouts), 1)
            self.assertGreaterEqual(seen_toggle_timeouts[0], 20.0)
            self.assertIn('"recording"', toggle_out.getvalue())

            for timeout_exc in (TimeoutError("timed out"), sock_mod.timeout("socket timed out")):
                tog_to_err = io.StringIO()
                with (
                    mock.patch.dict(os.environ, env, clear=True),
                    mock.patch("agent_voice_input.control_ipc.send_control_command", side_effect=timeout_exc),
                    mock.patch("sys.stderr", tog_to_err),
                ):
                    rc_tog_to = cli_mod.main(["toggle"])
                self.assertEqual(rc_tog_to, 1)
                self.assertIn("timed out", tog_to_err.getvalue().lower())
                self.assertNotIn("daemon is not running", tog_to_err.getvalue())

            tog_down_err = io.StringIO()
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("agent_voice_input.control_ipc.send_control_command", side_effect=ConnectionRefusedError("refused")),
                mock.patch("sys.stderr", tog_down_err),
            ):
                rc_tog_down = cli_mod.main(["toggle"])
            self.assertEqual(rc_tog_down, 1)
            self.assertIn("agent-voice-input daemon is not running", tog_down_err.getvalue())

            st_to_out = io.StringIO()
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("agent_voice_input.control_ipc.send_control_command", side_effect=TimeoutError("status timeout")),
                mock.patch("sys.stdout", st_to_out),
            ):
                rc_st_to = cli_mod.main(["status"])
            self.assertEqual(rc_st_to, 1)
            st_to_obj = json.loads(st_to_out.getvalue())
            self.assertFalse(st_to_obj["ok"])
            self.assertEqual(st_to_obj["state"], "unresponsive")
            self.assertIn("timed out", st_to_obj["error"])


class SerialPipelineAndPasteBoundaryTests(unittest.TestCase):
    def test_serial_multi_utterance_fifo_no_separator_and_ptt_cancel_resumes_bg_stage(self) -> None:
        from agent_voice_input.refiner import BaseRefiner

        order_lock = threading.Lock()
        events: list[str] = []
        asr_inflight = [0]
        max_asr_inflight = [0]
        refine_inflight = [0]
        max_refine_inflight = [0]
        total_inflight = [0]
        max_total_inflight = [0]
        seg1_asr_entered = threading.Event()
        seg1_asr_release = threading.Event()
        seg2_refine_done = threading.Event()
        seg4_refine_entered = threading.Event()
        seg4_refine_release = threading.Event()

        class _OrderedAsr(BaseAsrProvider):
            provider_name = "dashscope"

            def __init__(self) -> None:
                self._idx = 0

            def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None, **_kw: object) -> AsrResult:
                with order_lock:
                    self._idx += 1
                    idx = self._idx
                    asr_inflight[0] += 1
                    total_inflight[0] += 1
                    max_asr_inflight[0] = max(max_asr_inflight[0], asr_inflight[0])
                    max_total_inflight[0] = max(max_total_inflight[0], total_inflight[0])
                    events.append(f"asr_start:{idx}")
                try:
                    if idx == 1:
                        seg1_asr_entered.set()
                        seg1_asr_release.wait(timeout=3.0)
                    txt = f"第{idx}段原话"
                    with order_lock:
                        events.append(f"asr_end:{idx}")
                    return AsrResult(text=txt, provider="dashscope", model="qwen3-asr-flash", latency_ms=8)
                finally:
                    with order_lock:
                        asr_inflight[0] -= 1
                        total_inflight[0] -= 1

        class _OrderedRefiner(BaseRefiner):
            provider_name = "codex"

            def __init__(self) -> None:
                self._idx = 0

            def refine(self, asr_input: object, **_kw: object) -> RefinerResult:
                raw = asr_input.text if isinstance(asr_input, AsrResult) else str(asr_input)
                with order_lock:
                    self._idx += 1
                    idx = self._idx
                    refine_inflight[0] += 1
                    total_inflight[0] += 1
                    max_refine_inflight[0] = max(max_refine_inflight[0], refine_inflight[0])
                    max_total_inflight[0] = max(max_total_inflight[0], total_inflight[0])
                    events.append(f"refine_start:{idx}")
                try:
                    if raw == "第1段原话":
                        seg2_refine_done.wait(timeout=2.0)
                    elif idx == 4:
                        seg4_refine_entered.set()
                        seg4_refine_release.wait(timeout=3.0)
                    out = f"精修:{raw}"
                    with order_lock:
                        events.append(f"refine_end:{idx}")
                    if raw == "第2段原话":
                        seg2_refine_done.set()
                    return RefinerResult(text=out, provider="codex", model="gpt-6-luna", latency_ms=10)
                finally:
                    with order_lock:
                        refine_inflight[0] -= 1
                        total_inflight[0] -= 1

        class _SerialPlat(_FakePlatform):
            def __init__(self) -> None:
                super().__init__()
                self.pasted_texts: list[str] = []

            def deliver_text(
                self,
                text: str,
                seq_id: int,
                target_hwnd: int = 0,
                *,
                origin_window: ForegroundWindow | None = None,
                fallback_clipboard_text: str | None = None,
                suppress_beep: object = False,
                is_cancelled: object = None,
                before_clipboard_write: object = None,
                before_commit: object = None,
                same_window_verifier: object = None,
            ) -> PasteOutcome:
                if callable(before_clipboard_write):
                    before_clipboard_write()
                if callable(before_commit) and not before_commit():
                    return PasteOutcome(False, False, "cancelled", "")
                with order_lock:
                    events.append(f"deliver:{text}")
                self.pasted_texts.append(text)
                self.deliveries.append((text, seq_id, target_hwnd))
                return PasteOutcome(True, True, "ok", "WindowsTerminal", used_clipboard_paste=True)

        with tempfile.TemporaryDirectory() as td:
            plat = _SerialPlat()
            cfg = Config(refiner=RefinerConfig(provider="codex"))
            cfg.audio.beep = True
            daemon = VoiceInputDaemon(
                cfg,
                env={},
                runtime_dir=Path(td),
                platform=plat,
                asr_provider=_OrderedAsr(),
                refiner=_OrderedRefiner(),
                tracker=_isolated_tracker(Path(td)),
                paste_settle_sec=0.0,
            )
            try:
                # Start & stop segment 1 -> enters ASR and blocks
                daemon.handle_trigger("toggle")
                daemon._last_trigger_ts = -1e9
                s1 = daemon.handle_trigger("toggle")
                self.assertTrue(seg1_asr_entered.wait(timeout=2.0))
                self.assertEqual(daemon.handle_control({"cmd": "status"})["pending_count"], 1)

                # While segment 1 is still in ASR, record & finish segment 2 and segment 3
                daemon._last_trigger_ts = -1e9
                daemon.handle_trigger("toggle")
                self.assertEqual(daemon.state, "recording")
                daemon._last_trigger_ts = -1e9
                s2 = daemon.handle_trigger("toggle")
                self.assertEqual(s2["state"], "recognizing")
                self.assertEqual(daemon.handle_control({"cmd": "status"})["pending_count"], 2)

                daemon._last_trigger_ts = -1e9
                daemon.handle_trigger("toggle")
                daemon._last_trigger_ts = -1e9
                s3 = daemon.handle_trigger("toggle")
                st_snap = daemon.handle_control({"cmd": "status"})
                self.assertEqual(st_snap["pending_count"], 3)
                self.assertNotIn("第1段", json.dumps(st_snap, ensure_ascii=False))

                # Release segment 1 -> Stage 1 runs strictly serially (max_asr_inflight == 1),
                # Stage 2 overlaps (#2 finishes refining BEFORE #1), yet delivery is strictly 1 -> 2 -> 3!
                seg1_asr_release.set()
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline and daemon.last_result.get("seq_id") != s3["seq_id"]:
                    time.sleep(0.02)

                self.assertEqual(max_asr_inflight[0], 1)
                self.assertEqual(max_refine_inflight[0], 2)
                self.assertLessEqual(max_total_inflight[0], 2)
                asr_events = [e for e in events if e.startswith("asr_")]
                self.assertEqual(
                    asr_events,
                    [
                        "asr_start:1",
                        "asr_end:1",
                        "asr_start:2",
                        "asr_end:2",
                        "asr_start:3",
                        "asr_end:3",
                    ],
                )
                self.assertLess(events.index("refine_end:2"), events.index("refine_end:1"))
                deliver_events = [e for e in events if e.startswith("deliver:")]
                self.assertEqual(
                    deliver_events,
                    [
                        "deliver:精修:第1段原话",
                        "deliver:精修:第2段原话",
                        "deliver:精修:第3段原话",
                    ],
                )
                # Exact text without any extra newlines or separators injected
                self.assertEqual(
                    plat.pasted_texts,
                    ["精修:第1段原话", "精修:第2段原话", "精修:第3段原话"],
                )
                self.assertEqual(daemon.state, "idle")
                self.assertEqual(daemon.handle_control({"cmd": "status"})["pending_count"], 0)

                # Segment 4 blocks in refining; starting & cancelling segment 5 via ptt_cancel
                # restores main HUD to 'refining' (not 'idle'/'hide') and never drops segment 4!
                daemon._last_trigger_ts = -1e9
                daemon.handle_trigger("ptt_start")
                daemon._last_trigger_ts = -1e9
                s4 = daemon.handle_trigger("ptt_stop")
                self.assertTrue(seg4_refine_entered.wait(timeout=2.0))
                self.assertEqual(daemon.state, "refining")

                daemon._last_trigger_ts = -1e9
                r_s5 = daemon.handle_trigger("ptt_start")
                self.assertEqual(r_s5["state"], "recording")
                daemon._last_trigger_ts = -1e9
                r_cancel = daemon.handle_trigger("ptt_cancel")
                self.assertEqual(r_cancel["state"], "refining")
                self.assertEqual(daemon.state, "refining")
                self.assertEqual(plat.hud_events[-1]["state"], "refining")
                self.assertIs(plat.hud_events[-1].get("clear_toast"), True)

                seg4_refine_release.set()
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and daemon.last_result.get("seq_id") != s4["seq_id"]:
                    time.sleep(0.02)
                self.assertEqual(plat.pasted_texts[-1], "精修:第4段原话")
                self.assertEqual(daemon.state, "idle")
            finally:
                daemon.close()

    def test_clipboard_settle_pacing_sleeps_only_before_clipboard_write(self) -> None:
        from agent_voice_input.refiner import BaseRefiner

        now = [100.0]
        timeline: list[tuple[str, float]] = []

        def fake_sleep(sec: float) -> None:
            timeline.append(("sleep", round(sec, 4)))
            now[0] += sec

        class _TimedAsr(BaseAsrProvider):
            provider_name = "dashscope"

            def __init__(self) -> None:
                self.n = 0

            def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None, **_kw: object) -> AsrResult:
                self.n += 1
                timeline.append((f"asr:{self.n}", round(now[0], 4)))
                if self.n == 2:
                    now[0] += 0.03
                return AsrResult(text=f"段{self.n}", provider="dashscope", model="qwen3-asr-flash", latency_ms=5)

        class _TimedRefiner(BaseRefiner):
            provider_name = "codex"

            def __init__(self) -> None:
                self.n = 0

            def refine(self, asr_input: object, **_kw: object) -> RefinerResult:
                self.n += 1
                raw = asr_input.text if isinstance(asr_input, AsrResult) else str(asr_input)
                timeline.append((f"refine:{self.n}", round(now[0], 4)))
                if self.n == 2:
                    now[0] += 0.02
                return RefinerResult(text=raw, provider="codex", model="gpt-6-luna", latency_ms=5)

        class _SettlePlat(_FakePlatform):
            def deliver_text(
                self,
                text: str,
                seq_id: int,
                target_hwnd: int = 0,
                *,
                before_clipboard_write: object = None,
                **_kw: object,
            ) -> PasteOutcome:
                timeline.append((f"enter_deliver:{text}", round(now[0], 4)))
                if callable(before_clipboard_write):
                    before_clipboard_write()
                timeline.append((f"write_clipboard:{text}", round(now[0], 4)))
                return PasteOutcome(True, True, "ok", "WindowsTerminal", used_clipboard_paste=True)

        with tempfile.TemporaryDirectory() as td:
            plat = _SettlePlat()
            daemon = VoiceInputDaemon(
                Config(refiner=RefinerConfig(provider="codex")),
                env={},
                runtime_dir=Path(td),
                platform=plat,
                asr_provider=_TimedAsr(),
                refiner=_TimedRefiner(),
                tracker=_isolated_tracker(Path(td)),
                clock_fn=lambda: now[0],
                sleep_fn=fake_sleep,
                paste_settle_sec=0.15,
            )
            try:
                # Enqueue segment 1, start segment 2 while segment 1 commits clipboard, then finish segment 2
                daemon.handle_trigger("toggle")
                now[0] += 0.25
                s1 = daemon.handle_trigger("toggle")
                now[0] += 0.25
                daemon.handle_trigger("toggle")
                dl_s1 = time.monotonic() + 2.0
                while time.monotonic() < dl_s1 and daemon.last_result.get("seq_id") != s1["seq_id"]:
                    time.sleep(0.005)
                daemon._last_trigger_ts = -1e9
                s2 = daemon.handle_trigger("toggle")
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and daemon.last_result.get("seq_id") != s2["seq_id"]:
                    time.sleep(0.01)

                names = [ev for ev, _ in timeline]
                # Zero sleep before asr:2 or refine:2; sleep happens strictly inside deliver_text before write_clipboard:段2!
                idx_asr2 = names.index("asr:2")
                idx_ref2 = names.index("refine:2")
                idx_ent2 = names.index("enter_deliver:段2")
                idx_w2 = names.index("write_clipboard:段2")
                self.assertNotIn("sleep", names[:idx_ent2 + 1])
                sleeps_between = [val for ev, val in timeline[idx_ent2:idx_w2] if ev == "sleep"]
                self.assertGreater(len(sleeps_between), 0)
                # Total settle sleep equals remaining delta 0.15 - (0.03 + 0.02) = 0.10s
                self.assertAlmostEqual(sum(sleeps_between), 0.10, places=3)
                t_w1 = dict(timeline)["write_clipboard:段1"]
                t_w2 = dict(timeline)["write_clipboard:段2"]
                self.assertAlmostEqual(t_w2 - t_w1, 0.15, places=3)
                self.assertLess(idx_asr2, idx_ref2)
            finally:
                daemon.close()

    def test_focus_drift_clipboard_double_newline_concat_no_transient_overwrite_and_idle_reset(self) -> None:
        asr_texts = iter(["第一段", "免贴不污染", "第二段", "第三段自动贴", "甲段", "乙段", "新一轮独立口述"])
        seg1_gate = threading.Event()

        class _SeqAsr(BaseAsrProvider):
            provider_name = "dashscope"

            def __init__(self) -> None:
                pass

            def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None, **_kw: object) -> AsrResult:
                txt = next(asr_texts)
                if txt == "第一段":
                    seg1_gate.wait(timeout=2.0)
                return AsrResult(text=txt, provider="dashscope", model="qwen3-asr-flash", latency_ms=5)

        class _ClipPlat(_FakePlatform):
            def __init__(self) -> None:
                super().__init__()
                self.clipboard_history: list[str] = []
                self.mode = "focus_changed"

            def deliver_text(
                self,
                text: str,
                seq_id: int,
                target_hwnd: int = 0,
                *,
                fallback_clipboard_text: str | None = None,
                suppress_beep: object = False,
                before_clipboard_write: object = None,
                **_kw: object,
            ) -> PasteOutcome:
                if callable(before_clipboard_write):
                    before_clipboard_write()
                muted = bool(suppress_beep() if callable(suppress_beep) else suppress_beep)
                if self.mode == "focus_changed":
                    clip_payload = fallback_clipboard_text if fallback_clipboard_text is not None else text
                    self.clipboard_history.append(clip_payload)
                    if not muted:
                        self.beeps.append("focus")
                    return PasteOutcome(False, True, "focus_changed", "Safari")
                self.clipboard_history.append(text)
                return PasteOutcome(True, True, "ok", "WindowsTerminal", used_clipboard_paste=True)

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sample_wav = tmp / "sample.wav"
            sample_wav.write_bytes(_voiced_wav())
            plat = _ClipPlat()
            cfg = Config(refiner=RefinerConfig(provider="off"))
            cfg.audio.beep = True
            daemon = VoiceInputDaemon(
                cfg,
                env={},
                runtime_dir=tmp,
                platform=plat,
                asr_provider=_SeqAsr(),
                refiner=create_refiner(cfg),
                tracker=_isolated_tracker(tmp),
                paste_settle_sec=0.0,
            )
            try:
                # Batch 1: start & stop segment 1 (blocked on seg1_gate), then start recording segment 2
                daemon.handle_trigger("toggle")
                daemon._last_trigger_ts = -1e9
                r_s1 = daemon.handle_trigger("toggle")

                daemon._last_trigger_ts = -1e9
                daemon.handle_trigger("toggle")
                self.assertEqual(daemon.state, "recording")
                beeps_on_s2_start = list(plat.beeps)

                # Segment 1 finishes with focus_changed DURING segment 2 recording:
                # copied -> layer="toast", clear_main=False, and ZERO 'focus' beep!
                seg1_gate.set()
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and daemon.last_result.get("seq_id") != r_s1["seq_id"]:
                    time.sleep(0.02)
                self.assertEqual(plat.clipboard_history, ["第一段"])
                self.assertEqual(plat.beeps, beeps_on_s2_start)
                self.assertEqual(plat.hud_events[-1]["state"], "copied")
                self.assertEqual(plat.hud_events[-1].get("layer"), "toast")
                self.assertIs(plat.hud_events[-1].get("clear_main"), False)

                # Stop segment 2 -> wait no: first test no_paste utterance & selftest_wav in the same active batch!
                # Wait: finish segment 2 as no_paste=True via control rec_stop, while starting segment 3 before idle!
                r_nopaste = daemon.handle_control({"cmd": "rec_stop", "no_paste": True})
                daemon._last_trigger_ts = -1e9
                daemon.handle_trigger("toggle")  # keep batch alive (recording segment 3 -> "第二段")
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and daemon.last_result.get("seq_id") != r_nopaste["seq_id"]:
                    time.sleep(0.02)
                # selftest_wav also must not touch _batch_unpasted_clips
                st_wav = daemon.handle_control({"cmd": "selftest_wav", "wav_path": str(sample_wav)})
                self.assertTrue(st_wav["ok"])
                self.assertEqual(st_wav["final_text"], "第二段")
                # Now finish real segment ("第三段" in seq? No, selftest consumed "第二段", so re-check: let's feed exact text)
                # Wait: selftest_wav consumed "第二段" from asr_texts! Let's verify _batch_unpasted_clips still == ["第一段"]!
                self.assertEqual(daemon._batch_unpasted_clips, ["第一段"])
            finally:
                daemon.close()

        # Clean deterministic check of S1 focus_changed + S2 focus_changed -> S3 simulated paste -> Batch 2 idle reset
        batch_texts = iter(["第一段", "第二段", "本段直接粘贴", "甲段", "乙段", "新一轮独立口述"])

        class _DetAsr(BaseAsrProvider):
            provider_name = "dashscope"

            def __init__(self) -> None:
                pass

            def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None, **_kw: object) -> AsrResult:
                return AsrResult(text=next(batch_texts), provider="dashscope", model="qwen3-asr-flash", latency_ms=5)

        with tempfile.TemporaryDirectory() as td2:
            plat2 = _ClipPlat()
            cfg2 = Config(refiner=RefinerConfig(provider="off"))
            daemon2 = VoiceInputDaemon(
                cfg2,
                env={},
                runtime_dir=Path(td2),
                platform=plat2,
                asr_provider=_DetAsr(),
                refiner=create_refiner(cfg2),
                tracker=_isolated_tracker(Path(td2)),
                paste_settle_sec=0.0,
            )
            try:
                # Hold worker on S1 by pausing _utterance_queue? Even simpler: record S2 while S1 is running!
                # Or hold _trigger_lock? Let's enqueue S1 & S2 while S1 is gated!
                s1_hold = threading.Event()
                orig_deliver = plat2.deliver_text
                deliver_idx = [0]

                def gated_deliver(text: str, seq_id: int, target_hwnd: int = 0, **kw: object) -> PasteOutcome:
                    deliver_idx[0] += 1
                    if deliver_idx[0] == 1:
                        res = orig_deliver(text, seq_id, target_hwnd, **kw)
                        s1_hold.wait(timeout=2.0)
                        return res
                    return orig_deliver(text, seq_id, target_hwnd, **kw)

                plat2.deliver_text = gated_deliver  # type: ignore[method-assign]

                daemon2.handle_trigger("toggle")
                daemon2._last_trigger_ts = -1e9
                daemon2.handle_trigger("toggle")  # S1 ("第一段")
                daemon2._last_trigger_ts = -1e9
                daemon2.handle_trigger("toggle")
                daemon2._last_trigger_ts = -1e9
                r_b1_s2 = daemon2.handle_trigger("toggle")  # S2 ("第二段") queued in same batch
                daemon2._last_trigger_ts = -1e9
                daemon2.handle_trigger("toggle")  # start S3 recording so batch stays active after S2!
                s1_hold.set()

                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and daemon2.last_result.get("seq_id") != r_b1_s2["seq_id"]:
                    time.sleep(0.02)
                # S2 wrote "第一段\n\n第二段" directly in one shot — NEVER transiently "第二段"!
                self.assertEqual(plat2.clipboard_history, ["第一段", "第一段\n\n第二段"])

                # S3 pastes via simulated Ctrl+V (used_clipboard_paste=True): clipboard gets ONLY "本段直接粘贴" & clears batch!
                plat2.mode = "paste_ok"
                daemon2._last_trigger_ts = -1e9
                r_b1_s3 = daemon2.handle_trigger("toggle")
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and daemon2.last_result.get("seq_id") != r_b1_s3["seq_id"]:
                    time.sleep(0.02)
                self.assertEqual(plat2.clipboard_history[-1], "本段直接粘贴")
                self.assertEqual(daemon2._batch_unpasted_clips, [])
                self.assertEqual(daemon2.state, "idle")

                # Batch 2: S4 ("甲段") + S5 ("乙段") focus_changed -> drains to idle -> Batch 3 ("新一轮独立口述")
                plat2.mode = "focus_changed"
                plat2.clipboard_history.clear()
                s4_hold = threading.Event()
                b2_idx = [0]

                def gated_deliver_b2(text: str, seq_id: int, target_hwnd: int = 0, **kw: object) -> PasteOutcome:
                    b2_idx[0] += 1
                    res = orig_deliver(text, seq_id, target_hwnd, **kw)
                    if b2_idx[0] == 1:
                        s4_hold.wait(timeout=2.0)
                    return res

                plat2.deliver_text = gated_deliver_b2  # type: ignore[method-assign]
                daemon2._last_trigger_ts = -1e9
                daemon2.handle_trigger("toggle")
                daemon2._last_trigger_ts = -1e9
                daemon2.handle_trigger("toggle")  # "甲段"
                daemon2._last_trigger_ts = -1e9
                daemon2.handle_trigger("toggle")
                daemon2._last_trigger_ts = -1e9
                r_b2_s5 = daemon2.handle_trigger("toggle")  # "乙段"
                s4_hold.set()
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and daemon2.last_result.get("seq_id") != r_b2_s5["seq_id"]:
                    time.sleep(0.02)
                self.assertEqual(plat2.clipboard_history, ["甲段", "甲段\n\n乙段"])
                self.assertEqual(daemon2.state, "idle")
                self.assertEqual(daemon2._batch_unpasted_clips, [])

                # New independent round after idle MUST NOT include "甲段" or "乙段"!
                daemon2._last_trigger_ts = -1e9
                daemon2.handle_trigger("toggle")
                daemon2._last_trigger_ts = -1e9
                r_b3 = daemon2.handle_trigger("toggle")
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and daemon2.last_result.get("seq_id") != r_b3["seq_id"]:
                    time.sleep(0.02)
                self.assertEqual(plat2.clipboard_history[-1], "新一轮独立口述")
            finally:
                daemon2.close()

    def test_wsl_deliver_text_hotkey_held_deferral_no_blind_retry_and_hold_timeout_fallback(self) -> None:
        from agent_voice_input.config import AudioConfig

        class _AliveShim:
            stdin = None
            stdout = None

            def poll(self) -> None:
                return None

            def kill(self) -> None:
                return None

            def wait(self, timeout: float | None = None) -> int:
                del timeout
                return 0

        plat = WslPlatform(Config(), env={})
        plat._shim = _AliveShim()  # type: ignore[assignment]
        plat._flush_shim_events = lambda timeout_sec=0.05: True  # type: ignore[method-assign]
        rpc_calls: list[tuple[dict[str, object], bool]] = []
        ks_replies = iter(
            [
                {"ok": True, "hotkey_held": True},
                {"ok": True, "hotkey_held": True},
                {"ok": True, "hotkey_held": False},
            ]
        )

        def fake_rpc(payload: dict[str, object], timeout_sec: float = 3.0, *, allow_retry: bool = True) -> dict[str, object]:
            rpc_calls.append((dict(payload), allow_retry))
            if payload.get("cmd") == "key_state":
                return next(ks_replies)
            return {"ok": True, "pasted": True, "clipboard": True, "reason": "ok", "fg_process": "WindowsTerminal"}

        plat._shim_rpc = fake_rpc  # type: ignore[method-assign]
        before_clip_cnt = [0]
        origin = ForegroundWindow(hwnd=321, process="WindowsTerminal", title="codex")
        out = plat.deliver_text(
            "按住右Ctrl松开后上屏",
            seq_id=10,
            target_hwnd=321,
            origin_window=origin,
            fallback_clipboard_text="回退段\n\n按住右Ctrl松开后上屏",
            before_clipboard_write=lambda: before_clip_cnt.__setitem__(0, before_clip_cnt[0] + 1),
        )
        self.assertTrue(out.pasted)
        self.assertTrue(out.clipboard)
        self.assertTrue(out.used_clipboard_paste)
        self.assertEqual(out.reason, "ok")
        self.assertEqual(before_clip_cnt[0], 1)
        self.assertEqual([p.get("cmd") for p, _ in rpc_calls], ["key_state", "key_state", "key_state", "paste"])
        for payload, allow_retry in rpc_calls:
            self.assertFalse(allow_retry, "non-idempotent paste/key_state RPC must never set allow_retry=True")
        self.assertEqual(rpc_calls[-1][0]["fallback_text"], "回退段\n\n按住右Ctrl松开后上屏")

        # 2. Focus switched while hotkey was held -> upon key release single paste RPC returns focus_changed with clipboard=True
        ks_drift = iter([{"ok": True, "hotkey_held": True}, {"ok": True, "hotkey_held": False}])
        drift_rpcs: list[dict[str, object]] = []

        def fake_rpc_drift(payload: dict[str, object], timeout_sec: float = 3.0, *, allow_retry: bool = True) -> dict[str, object]:
            drift_rpcs.append(dict(payload))
            if payload.get("cmd") == "key_state":
                return next(ks_drift)
            return {"ok": True, "pasted": False, "clipboard": True, "reason": "focus_changed", "fg_process": "chrome"}

        plat._shim_rpc = fake_rpc_drift  # type: ignore[method-assign]
        out_drift = plat.deliver_text(
            "松键时已切窗",
            seq_id=11,
            target_hwnd=321,
            origin_window=origin,
            fallback_clipboard_text="旧段\n\n松键时已切窗",
        )
        self.assertFalse(out_drift.pasted)
        self.assertTrue(out_drift.clipboard)
        self.assertFalse(out_drift.used_clipboard_paste)
        self.assertEqual(out_drift.reason, "focus_changed")
        self.assertEqual([r.get("cmd") for r in drift_rpcs], ["key_state", "key_state", "paste"])

        # 3. Hold timeout safety net -> sends single force_clipboard_only=True so text safely lands in clipboard
        plat._paste_hold_timeout_sec = 0.08
        timeout_rpcs: list[dict[str, object]] = []

        def fake_held_forever(payload: dict[str, object], timeout_sec: float = 3.0, *, allow_retry: bool = True) -> dict[str, object]:
            timeout_rpcs.append(dict(payload))
            if payload.get("cmd") == "key_state":
                return {"ok": True, "hotkey_held": True}
            if payload.get("force_clipboard_only"):
                return {"ok": True, "pasted": False, "clipboard": True, "reason": "focus_changed", "fg_process": "WindowsTerminal"}
            raise AssertionError("must not send normal auto-paste after hold timeout")

        plat._shim_rpc = fake_held_forever  # type: ignore[method-assign]
        out_to = plat.deliver_text(
            "超时兜底写剪贴板",
            seq_id=12,
            target_hwnd=321,
            origin_window=origin,
            fallback_clipboard_text="前段\n\n超时兜底写剪贴板",
        )
        self.assertFalse(out_to.pasted)
        self.assertTrue(out_to.clipboard)
        self.assertEqual(out_to.reason, "focus_changed")
        paste_timeout_rpcs = [r for r in timeout_rpcs if r.get("cmd") == "paste"]
        self.assertEqual(len(paste_timeout_rpcs), 1)
        self.assertTrue(paste_timeout_rpcs[0].get("force_clipboard_only"))
        self.assertEqual(paste_timeout_rpcs[0].get("fallback_text"), "前段\n\n超时兜底写剪贴板")

        # 4. Single-owner contract: hotkey_held detected inside paste RPC is persistently copied by win_shim itself;
        #    Python MUST return immediately in 1 single paste RPC with ZERO second force_clipboard_only RPC!
        plat._paste_hold_timeout_sec = 2.0
        single_rpc_calls: list[dict[str, object]] = []

        def fake_paste_hotkey_held(payload: dict[str, object], timeout_sec: float = 3.0, *, allow_retry: bool = True) -> dict[str, object]:
            single_rpc_calls.append(dict(payload))
            if payload.get("cmd") == "key_state":
                return {"ok": True, "hotkey_held": False}
            if payload.get("force_clipboard_only"):
                raise AssertionError("must NOT send second force_clipboard_only RPC after paste hotkey_held")
            return {"ok": True, "pasted": False, "clipboard": True, "reason": "hotkey_held", "fg_process": "WindowsTerminal"}

        plat._shim_rpc = fake_paste_hotkey_held  # type: ignore[method-assign]
        out_held_single = plat.deliver_text(
            "粘贴瞬间按住修饰键",
            seq_id=13,
            target_hwnd=321,
            origin_window=origin,
            fallback_clipboard_text="前段\n\n粘贴瞬间按住修饰键",
        )
        self.assertFalse(out_held_single.pasted)
        self.assertTrue(out_held_single.clipboard)
        self.assertFalse(out_held_single.used_clipboard_paste)
        self.assertEqual(out_held_single.reason, "hotkey_held")
        paste_single = [r for r in single_rpc_calls if r.get("cmd") == "paste"]
        self.assertEqual(len(paste_single), 1)
        self.assertNotIn("force_clipboard_only", paste_single[0])
        self.assertEqual(paste_single[0].get("fallback_text"), "前段\n\n粘贴瞬间按住修饰键")

        # 5. RPC timeout / unconfirmed reply -> NEVER sends second blind clipboard overwrite; returns shim_paste_unconfirmed
        #    so daemon stashes WAV in failed/ for retry.
        for bad_kind, bad_rpc in (
            ("timeout", lambda payload, timeout_sec=3.0, *, allow_retry=True: (_ for _ in ()).throw(queue.Empty()) if payload.get("cmd") == "paste" else {"ok": True, "hotkey_held": False}),
            ("empty_reply", lambda payload, timeout_sec=3.0, *, allow_retry=True: {} if payload.get("cmd") == "paste" else {"ok": True, "hotkey_held": False}),
            ("error_reply", lambda payload, timeout_sec=3.0, *, allow_retry=True: {"ok": False, "error": "pipe_broken"} if payload.get("cmd") == "paste" else {"ok": True, "hotkey_held": False}),
            ("key_state_timeout", lambda payload, timeout_sec=3.0, *, allow_retry=True: (_ for _ in ()).throw(TimeoutError("ks_timeout"))),
        ):
            seen_bad: list[dict[str, object]] = []

            def _wrap_bad(payload: dict[str, object], timeout_sec: float = 3.0, *, allow_retry: bool = True, _fn=bad_rpc) -> dict[str, object]:
                seen_bad.append(dict(payload))
                return _fn(payload, timeout_sec=timeout_sec, allow_retry=allow_retry)

            plat._shim_rpc = _wrap_bad  # type: ignore[method-assign]
            out_bad = plat.deliver_text("超时不盲覆写", seq_id=14, target_hwnd=321, origin_window=origin)
            self.assertFalse(out_bad.pasted, msg=bad_kind)
            self.assertFalse(out_bad.clipboard, msg=bad_kind)
            self.assertFalse(out_bad.used_clipboard_paste, msg=bad_kind)
            self.assertEqual(out_bad.reason, "shim_paste_unconfirmed", msg=bad_kind)
            self.assertFalse(
                any(r.get("force_clipboard_only") for r in seen_bad),
                msg=f"{bad_kind}: must never send blind force_clipboard_only overwrite",
            )

        # 5b. End-to-end with VoiceInputDaemon: paste RPC timeout stashes WAV into failed/ and triggers paste_failed_retry HUD
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            rt.mkdir()
            wsl_dplat = WslPlatform(Config(audio=AudioConfig(wsl_source="pulse", tail_drain_sec=0.0)), env={})
            wsl_dplat._shim = _AliveShim()  # type: ignore[assignment]
            wsl_dplat._flush_shim_events = lambda timeout_sec=0.05: True  # type: ignore[method-assign]
            wsl_dplat.get_foreground_window = lambda: origin  # type: ignore[method-assign]
            wsl_dplat.start_recording = lambda *a, **kw: None  # type: ignore[method-assign]
            wsl_dplat.stop_recording = lambda *a, **kw: (_voiced_wav(), "pulse")  # type: ignore[method-assign]
            wsl_dplat.beep = lambda _k: None  # type: ignore[method-assign]
            hud_states: list[str] = []
            wsl_dplat.hud = lambda st, *a, **kw: hud_states.append(st)  # type: ignore[method-assign]
            d_rpcs: list[dict[str, object]] = []

            def _timeout_on_paste(payload: dict[str, object], timeout_sec: float = 3.0, *, allow_retry: bool = True) -> dict[str, object]:
                d_rpcs.append(dict(payload))
                if payload.get("cmd") == "key_state":
                    return {"ok": True, "hotkey_held": False}
                raise queue.Empty()

            wsl_dplat._shim_rpc = _timeout_on_paste  # type: ignore[method-assign]
            daemon_to = VoiceInputDaemon(
                Config(refiner=RefinerConfig(provider="off"), audio=AudioConfig(wsl_source="pulse", tail_drain_sec=0.0)),
                env={},
                runtime_dir=rt,
                platform=wsl_dplat,
                asr_provider=_FakeAsr("超时暂存语音"),
                refiner=None,
                tracker=_isolated_tracker(Path(td)),
            )
            try:
                r_on = daemon_to.handle_trigger("toggle")
                self.assertTrue(r_on.get("ok"), msg=r_on)
                daemon_to._last_trigger_ts = -1e9
                r_off = daemon_to.handle_trigger("toggle")
                self.assertTrue(r_off.get("ok"), msg=r_off)
                self.assertIsNotNone(r_off.get("seq_id"), msg=r_off)
                dl = time.monotonic() + 3.0
                while time.monotonic() < dl and daemon_to.last_result.get("seq_id") != r_off["seq_id"]:
                    time.sleep(0.02)
                self.assertEqual(daemon_to.last_result.get("seq_id"), r_off["seq_id"], msg=daemon_to.last_result)
                self.assertFalse(daemon_to.last_result.get("ok"), msg=daemon_to.last_result)
                self.assertEqual(daemon_to.last_result.get("skipped_reason"), "shim_paste_unconfirmed", msg=daemon_to.last_result)
                saved_wav = str(daemon_to.last_result.get("saved_audio") or "")
                self.assertTrue(saved_wav and Path(saved_wav).is_file(), f"expected WAV stashed in failed/, got {saved_wav!r}")
                self.assertEqual(Path(saved_wav).parent.name, "failed")
                self.assertIn("paste_failed_retry", hud_states)
                self.assertFalse(any(r.get("force_clipboard_only") for r in d_rpcs))
            finally:
                wsl_dplat._shim = None
                daemon_to.close()

        # 6. WSL origin_drifted MUST respect before_clipboard_write settle interval (A paste -> B drifted copy-only)
        now_t = [200.0]
        order_log: list[tuple[str, float]] = []

        def fake_sleep_ab(sec: float) -> None:
            order_log.append(("sleep", round(sec, 4)))
            now_t[0] += sec

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt_ab"
            rt.mkdir()
            wsl_ab = WslPlatform(Config(audio=AudioConfig(wsl_source="pulse", tail_drain_sec=0.0)), env={})
            wsl_ab._shim = _AliveShim()  # type: ignore[assignment]
            wsl_ab._flush_shim_events = lambda timeout_sec=0.05: True  # type: ignore[method-assign]
            wsl_ab.beep = lambda _k: None  # type: ignore[method-assign]
            wsl_ab.hud = lambda *a, **kw: None  # type: ignore[method-assign]

            def _rpc_ab(payload: dict[str, object], timeout_sec: float = 3.0, *, allow_retry: bool = True) -> dict[str, object]:
                if payload.get("cmd") == "key_state":
                    return {"ok": True, "hotkey_held": False}
                tag = "fallback_copy" if payload.get("force_clipboard_only") else "paste"
                order_log.append((f"{tag}:{payload.get('text')}", round(now_t[0], 4)))
                if payload.get("force_clipboard_only"):
                    return {"ok": True, "pasted": False, "clipboard": True, "reason": "focus_changed", "fg_process": "chrome"}
                return {"ok": True, "pasted": True, "clipboard": True, "reason": "ok", "fg_process": "WindowsTerminal"}

            wsl_ab._shim_rpc = _rpc_ab  # type: ignore[method-assign]
            daemon_ab = VoiceInputDaemon(
                Config(refiner=RefinerConfig(provider="off")),
                env={},
                runtime_dir=rt,
                platform=wsl_ab,
                asr_provider=_FakeAsr("unused"),
                refiner=None,
                tracker=_isolated_tracker(Path(td)),
                clock_fn=lambda: now_t[0],
                sleep_fn=fake_sleep_ab,
            )
            try:
                from agent_voice_input.daemon import _QueuedUtterance
                from agent_voice_input.http_transport import CancelToken

                t_a = _QueuedUtterance(seq_id=1, raw_wav=b"", audio_source="winmm", origin_win=origin, cancel_token=CancelToken())
                out_a = daemon_ab._deliver_for_utterance(t_a, "第一句A", origin)
                self.assertTrue(out_a.pasted and out_a.used_clipboard_paste)
                # Advance only 30ms (< 150ms settle window) then deliver drifted utterance B
                now_t[0] += 0.03
                drifted_b = ForegroundWindow(hwnd=321, process="WindowsTerminal", title="codex", origin_drifted=True)
                t_b = _QueuedUtterance(seq_id=2, raw_wav=b"", audio_source="winmm", origin_win=drifted_b, cancel_token=CancelToken())
                out_b = daemon_ab._deliver_for_utterance(t_b, "切窗句B", drifted_b)
                self.assertFalse(out_b.pasted)
                self.assertTrue(out_b.clipboard)
                self.assertEqual(out_b.reason, "focus_changed")
                # B MUST have slept remaining ~0.12s BEFORE issuing force_clipboard_only!
                self.assertEqual(
                    order_log,
                    [("paste:第一句A", 200.0), ("sleep", 0.12), ("fallback_copy:切窗句B", 200.15)],
                )
            finally:
                wsl_ab._shim = None
                daemon_ab.close()

        # 6b. origin_drifted also handles force_clipboard_only RPC timeout cleanly (no crash -> shim_paste_unconfirmed)
        plat._shim_rpc = lambda payload, timeout_sec=3.0, *, allow_retry=True: (_ for _ in ()).throw(queue.Empty())  # type: ignore[method-assign]
        drifted_win = ForegroundWindow(hwnd=321, process="WindowsTerminal", title="codex", origin_drifted=True)
        out_drifted_to = plat.deliver_text("漂移超时", seq_id=15, target_hwnd=321, origin_window=drifted_win)
        self.assertFalse(out_drifted_to.pasted)
        self.assertFalse(out_drifted_to.clipboard)
        self.assertEqual(out_drifted_to.reason, "shim_paste_unconfirmed")

        # 6c. WSL paste RPC always passes monotonic deadline_ms and surfaces paste_deadline_exceeded cleanly (clipboard=False)
        deadline_seen: list[dict[str, object]] = []

        def _fake_deadline_exceeded(payload: dict[str, object], timeout_sec: float = 3.0, *, allow_retry: bool = True) -> dict[str, object]:
            deadline_seen.append(dict(payload))
            if payload.get("cmd") == "key_state":
                return {"ok": True, "hotkey_held": False}
            return {"ok": True, "pasted": False, "clipboard": False, "reason": "paste_deadline_exceeded", "fg_process": "WindowsTerminal"}

        plat._shim_rpc = _fake_deadline_exceeded  # type: ignore[method-assign]
        out_dl = plat.deliver_text("超时截止不迟贴", seq_id=16, target_hwnd=321, origin_window=origin)
        self.assertFalse(out_dl.pasted)
        self.assertFalse(out_dl.clipboard)
        self.assertEqual(out_dl.reason, "paste_deadline_exceeded")
        out_dl_fb = plat.deliver_text("切窗截止不迟写", seq_id=17, target_hwnd=321, origin_window=drifted_win)
        self.assertFalse(out_dl_fb.pasted)
        self.assertFalse(out_dl_fb.clipboard)
        self.assertEqual(out_dl_fb.reason, "paste_deadline_exceeded")
        paste_dl_reqs = [r for r in deadline_seen if r.get("cmd") == "paste"]
        self.assertEqual(len(paste_dl_reqs), 2)
        for req_item in paste_dl_reqs:
            self.assertEqual(int(req_item.get("deadline_ms") or 0), 3800)

    def test_audio_error_snapshot_queue_full_recorder_lost_absorption_and_fast_close(self) -> None:
        from agent_voice_input.daemon import MAX_PENDING_UTTERANCES

        # 1. Audio error snapshot isolation + exact _recorder_lost_seq absorption
        s1_in_asr = threading.Event()
        s1_rel_asr = threading.Event()

        class _GateFirstAsr(BaseAsrProvider):
            provider_name = "dashscope"

            def __init__(self) -> None:
                self.n = 0

            def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None, **_kw: object) -> AsrResult:
                self.n += 1
                if self.n == 1:
                    s1_in_asr.set()
                    s1_rel_asr.wait(timeout=3.0)
                return AsrResult(text=f"抢救语音{self.n}", provider="dashscope", model="qwen3-asr-flash", latency_ms=5)

        class _LostPlat(_FakePlatform):
            def __init__(self) -> None:
                super().__init__()
                self.last_audio_error = ""
                self._lost_cb = None

            def set_recorder_lost_callback(self, cb):
                self._lost_cb = cb

            def start_recording(self, runtime_dir: Path, *, loopback: bool = False, seq_id: int | None = None) -> None:
                # Starting a new recording resets platform.last_audio_error, just like real WslPlatform/MacOSPlatform
                self.last_audio_error = ""

        with tempfile.TemporaryDirectory() as td:
            now = [50.0]
            plat = _LostPlat()
            cfg = Config(refiner=RefinerConfig(provider="off"))
            daemon = VoiceInputDaemon(
                cfg,
                env={},
                runtime_dir=Path(td),
                platform=plat,
                asr_provider=_GateFirstAsr(),
                refiner=create_refiner(cfg),
                tracker=_isolated_tracker(Path(td)),
                clock_fn=lambda: now[0],
            )
            try:
                daemon.handle_trigger("toggle")
                plat.last_audio_error = "usb_mic_yanked_seq1"
                assert plat._lost_cb is not None
                plat._lost_cb(seq_id=daemon.seq_id)
                self.assertTrue(s1_in_asr.wait(timeout=2.0))

                # Even after 2.0s (> 1.5s), while seq 1 salvage is STILL inflight, first toggle stop is absorbed once!
                now[0] += 2.0
                r_abs = daemon.handle_trigger("toggle")
                self.assertEqual(r_abs.get("ignored"), "already_stopped")

                # Next toggle starts seq 2 recording -> resets plat.last_audio_error = ""!
                now[0] += 0.3
                r_s2 = daemon.handle_trigger("toggle")
                self.assertEqual(r_s2["state"], "recording")
                self.assertEqual(plat.last_audio_error, "")

                # Release seq 1 ASR: summary MUST still preserve snapped audio_error="usb_mic_yanked_seq1"!
                s1_rel_asr.set()
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and daemon.last_result.get("seq_id") != 1:
                    time.sleep(0.02)
                self.assertTrue(daemon.last_result["ok"])
                self.assertTrue(daemon.last_result.get("capture_interrupted"))
                self.assertEqual(daemon.last_result.get("audio_error"), "usb_mic_yanked_seq1")

                # Finish seq 2 -> clean summary must NOT inherit seq 1's audio_error
                now[0] += 0.3
                r_s2_stop = daemon.handle_trigger("toggle")
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and daemon.last_result.get("seq_id") != r_s2_stop["seq_id"]:
                    time.sleep(0.02)
                self.assertNotIn("audio_error", daemon.last_result)

                # Non-inflight recorder_lost (zero wav -> skipped immediately) does NOT absorb after 1.6s (> 1.5s)
                plat.wav_bytes = _zero_wav(0.5)
                now[0] += 0.5
                daemon.handle_trigger("toggle")
                plat.last_audio_error = "silent_unplug"
                plat._lost_cb(seq_id=daemon.seq_id)
                now[0] += 1.6
                plat.wav_bytes = _voiced_wav()
                r_after_window = daemon.handle_trigger("toggle")
                self.assertEqual(r_after_window["state"], "recording")
                now[0] += 0.3
                daemon.handle_trigger("ptt_cancel")
            finally:
                daemon.close()

        # 2. Bounded queue MAX_PENDING_UTTERANCES = 8 -> 9th start returns ignored="queue_full" without starting mic
        hold_q = threading.Event()
        q_in_asr = threading.Event()

        class _HoldAsr(BaseAsrProvider):
            provider_name = "dashscope"

            def __init__(self) -> None:
                pass

            def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None, **_kw: object) -> AsrResult:
                q_in_asr.set()
                hold_q.wait(timeout=3.0)
                return AsrResult(text="队列项", provider="dashscope", model="qwen3-asr-flash", latency_ms=2)

        with tempfile.TemporaryDirectory() as td_q:
            plat_q = _FakePlatform()
            start_calls = [0]
            plat_q.start_recording = lambda _rt, **_kw: start_calls.__setitem__(0, start_calls[0] + 1)  # type: ignore[method-assign]
            daemon_q = VoiceInputDaemon(
                Config(refiner=RefinerConfig(provider="off")),
                env={},
                runtime_dir=Path(td_q),
                platform=plat_q,
                asr_provider=_HoldAsr(),
                refiner=create_refiner(Config(refiner=RefinerConfig(provider="off"))),
                tracker=_isolated_tracker(Path(td_q)),
                paste_settle_sec=0.0,
            )
            try:
                for _ in range(MAX_PENDING_UTTERANCES):
                    daemon_q._last_trigger_ts = -1e9
                    r_on = daemon_q.handle_trigger("toggle")
                    self.assertTrue(r_on["ok"])
                    daemon_q._last_trigger_ts = -1e9
                    daemon_q.handle_trigger("toggle")
                self.assertTrue(q_in_asr.wait(timeout=2.0))
                self.assertEqual(start_calls[0], MAX_PENDING_UTTERANCES)
                self.assertEqual(daemon_q.handle_control({"cmd": "status"})["pending_count"], MAX_PENDING_UTTERANCES)

                daemon_q._last_trigger_ts = -1e9
                r_full = daemon_q.handle_trigger("toggle")
                self.assertFalse(r_full["ok"])
                self.assertEqual(r_full.get("ignored"), "queue_full")
                self.assertEqual(start_calls[0], MAX_PENDING_UTTERANCES)
                self.assertEqual(plat_q.hud_events[-1]["state"], "mic_unavailable")
                self.assertIs(plat_q.hud_events[-1].get("clear_main"), False)
            finally:
                hold_q.set()
                daemon_q.close()

        # 3. Fast daemon.close() (< 1.0s) when CodexRefiner is blocked mid-SSE stream
        #    AND JsonlShimMixin _closed respawn guard + per-proc _pending isolation
        with tempfile.TemporaryDirectory() as td_slow:
            tmp_s = Path(td_slow)
            _write_codex_auth_json(tmp_s)
            in_stream_evt = threading.Event()

            def slow_sse_responder(handler, _rec) -> None:
                handler.send_response(200)
                handler.send_header("Transfer-Encoding", "chunked")
                handler.send_header("Connection", "keep-alive")
                handler.end_headers()
                fr = b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'
                handler.wfile.write(f"{len(fr):x}\r\n".encode("ascii") + fr + b"\r\n")
                handler.wfile.flush()
                in_stream_evt.set()
                time.sleep(10.0)

            srv_slow = _LocalResponsesServer(responder=slow_sse_responder)
            try:
                cfg_s = Config(refiner=RefinerConfig(provider="codex", base_url=srv_slow.base_url, timeout_sec=15.0))
                ref_s = CodexRefiner(cfg_s, env={"HOME": str(tmp_s)}, runtime_dir=tmp_s)
                daemon_s = VoiceInputDaemon(
                    cfg_s,
                    env={"HOME": str(tmp_s)},
                    runtime_dir=tmp_s,
                    platform=_FakePlatform(),
                    asr_provider=_FakeAsr("慢精修关停测试"),
                    refiner=ref_s,
                    tracker=_isolated_tracker(tmp_s),
                )
                daemon_s.handle_trigger("toggle")
                daemon_s._last_trigger_ts = -1e9
                daemon_s.handle_trigger("toggle")
                self.assertTrue(in_stream_evt.wait(timeout=3.0))

                t_close0 = time.monotonic()
                daemon_s.close()
                close_elapsed = time.monotonic() - t_close0
                self.assertLess(close_elapsed, 1.0, f"daemon.close() hung for {close_elapsed:.2f}s on slow CodexRefiner")
            finally:
                srv_slow.close()

            # JsonlShimMixin: _closed prevents _ensure_shim resurrection, and real _shim_reader(proc_old)
            # flushes ONLY proc_old's _pending entries with 'shim_eof' without touching proc_new's RPCs!
            import io
            import queue as q_mod

            w_closed = WslPlatform(Config(), env={})
            w_closed.close()
            with self.assertRaises(RuntimeError):
                w_closed._ensure_shim()

            class _MockShimProc:
                def __init__(self, stdout_data: str = "", returncode: int | None = 0) -> None:
                    self.stdin = io.StringIO()
                    self.stdout = io.StringIO(stdout_data)
                    self._rc = returncode

                def poll(self) -> int | None:
                    return self._rc

                def kill(self) -> None:
                    self._rc = -9

                def wait(self, timeout: float | None = None) -> int:
                    return 0 if self._rc is None else self._rc

            w_iso = WslPlatform(Config(), env={})
            proc_old = _MockShimProc(
                json.dumps({"type": "reply", "reply": "ping", "seq_id": 2, "ok": True}) + "\n",
                returncode=0,
            )
            proc_new = _MockShimProc("", returncode=None)
            q_old: q_mod.Queue[dict[str, object]] = q_mod.Queue()
            q_new: q_mod.Queue[dict[str, object]] = q_mod.Queue()
            w_iso._shim = proc_new  # type: ignore[assignment]
            w_iso._pending[1] = (proc_old, q_old)  # type: ignore[assignment]
            w_iso._pending[2] = (proc_new, q_new)  # type: ignore[assignment]
            w_iso._shim_reader(proc_old)  # type: ignore[arg-type]
            self.assertEqual(q_old.get_nowait(), {"error": "shim_eof"})
            self.assertTrue(q_new.empty())
            self.assertNotIn(1, w_iso._pending)
            self.assertIs(w_iso._pending[2][0], proc_new)
            self.assertIs(w_iso._shim, proc_new)
            proc_new.stdin.close()
            proc_new.stdout.close()

    def test_normal_followed_by_silent_hud_task_drains_cleanly_without_stranded_main_capsule(self) -> None:
        gate_t1 = threading.Event()
        in_t1 = threading.Event()

        class _StagedAsr(BaseAsrProvider):
            provider_name = "dashscope"

            def __init__(self) -> None:
                self.n = 0

            def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None, **_kw: object) -> AsrResult:
                self.n += 1
                if self.n in (1, 4):
                    in_t1.set()
                    gate_t1.wait(timeout=2.0)
                return AsrResult(text=f"口述{self.n}", provider="dashscope", model="qwen3-asr-flash", latency_ms=4)

        with tempfile.TemporaryDirectory() as td:
            plat = _FakePlatform()
            cfg = Config(refiner=RefinerConfig(provider="off"))
            daemon = VoiceInputDaemon(
                cfg,
                env={},
                runtime_dir=Path(td),
                platform=plat,
                asr_provider=_StagedAsr(),
                refiner=create_refiner(cfg),
                tracker=_isolated_tracker(Path(td)),
                paste_settle_sec=0.0,
            )
            try:
                # Case A: Normal task #1 blocked in ASR; task #2 (silent_hud=True, no_paste=True) already queued behind #1
                daemon.handle_trigger("toggle")
                daemon._last_trigger_ts = -1e9
                daemon.handle_trigger("toggle")
                self.assertTrue(in_t1.wait(timeout=2.0))
                self.assertTrue(daemon._main_hud_visible)

                daemon._last_trigger_ts = -1e9
                daemon.handle_control({"cmd": "rec_start", "no_paste": True, "silent_hud": True})
                daemon._last_trigger_ts = -1e9
                r_silent2 = daemon.handle_control({"cmd": "rec_stop"})
                self.assertEqual(daemon.handle_control({"cmd": "status"})["pending_count"], 2)

                gate_t1.set()
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and daemon.last_result.get("seq_id") != r_silent2["seq_id"]:
                    time.sleep(0.02)
                self.assertEqual(daemon.state, "idle")
                self.assertFalse(daemon._main_hud_visible)
                # Task #1 emitted its own 'done' instead of lighting up 'recognizing' for silent task #2!
                self.assertEqual(plat.huds, ["recording", "recognizing", "done"])

                # Subsequent normal task #3 still lights up 'recognizing' -> 'done' cleanly
                plat.huds.clear()
                daemon._last_trigger_ts = -1e9
                daemon.handle_trigger("toggle")
                daemon._last_trigger_ts = -1e9
                r_norm3 = daemon.handle_trigger("toggle")
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and daemon.last_result.get("seq_id") != r_norm3["seq_id"]:
                    time.sleep(0.02)
                self.assertEqual(plat.huds, ["recording", "recognizing", "done"])
                self.assertFalse(daemon._main_hud_visible)

                # Case B: Normal task #4 finishes WHILE silent task #5 is actively recording -> when #5 finishes, 'hide' clears stale main HUD!
                plat.huds.clear()
                in_t1.clear()
                gate_t1.clear()
                daemon._last_trigger_ts = -1e9
                daemon.handle_trigger("toggle")
                daemon._last_trigger_ts = -1e9
                r_norm4 = daemon.handle_trigger("toggle")
                self.assertTrue(in_t1.wait(timeout=2.0))
                self.assertTrue(daemon._main_hud_visible)

                daemon._last_trigger_ts = -1e9
                daemon.handle_control({"cmd": "rec_start", "no_paste": True, "silent_hud": True})
                gate_t1.set()
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and daemon.last_result.get("seq_id") != r_norm4["seq_id"]:
                    time.sleep(0.02)
                # Now stop silent task #5 -> drains to idle and explicitly hides stranded main HUD!
                daemon._last_trigger_ts = -1e9
                r_silent5 = daemon.handle_control({"cmd": "rec_stop"})
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and daemon.last_result.get("seq_id") != r_silent5["seq_id"]:
                    time.sleep(0.02)
                self.assertEqual(daemon.state, "idle")
                self.assertFalse(daemon._main_hud_visible)
                self.assertEqual(plat.huds[-1], "hide")

                # Case C: Normal #6 -> Silent #7 -> Normal #8 queued; worker pops #8 right as foreground starts recording #9.
                # Background worker MUST re-check under _trigger_lock and NEVER overwrite #9's 'recording' main capsule!
                plat.huds.clear()
                in_t1.clear()
                gate_t1.clear()
                popped_t8 = threading.Event()
                allow_worker_trigger_lock = threading.Event()
                target_seq8 = [-1]

                orig_recompute = daemon._recompute_state_locked

                def hooked_recompute() -> str:
                    st = orig_recompute()
                    if (
                        target_seq8[0] > 0
                        and daemon._active_task is not None
                        and daemon._active_task.seq_id == target_seq8[0]
                        and threading.current_thread().name == "avi-utterance-worker"
                        and not popped_t8.is_set()
                    ):
                        popped_t8.set()
                    return st

                daemon._recompute_state_locked = hooked_recompute  # type: ignore[method-assign]

                orig_tlock = daemon._trigger_lock

                class _GateWorkerTriggerLock:
                    def __enter__(self_lock) -> Any:
                        if popped_t8.is_set() and threading.current_thread().name == "avi-utterance-worker":
                            allow_worker_trigger_lock.wait(timeout=2.0)
                        return orig_tlock.__enter__()

                    def __exit__(self_lock, exc_type: Any, exc: Any, tb: Any) -> Any:
                        return orig_tlock.__exit__(exc_type, exc, tb)

                daemon._trigger_lock = _GateWorkerTriggerLock()  # type: ignore[assignment]
                try:
                    # Block task #6 in ASR while we enqueue silent #7 and normal #8
                    in_s6 = threading.Event()
                    gate_s6 = threading.Event()
                    orig_transcribe = daemon.asr_provider.transcribe

                    def gated_transcribe(wav_b: bytes, context: AsrContext | None = None, **_kw: object) -> AsrResult:
                        res_asr = orig_transcribe(wav_b, context)
                        if res_asr.text == "口述6":
                            in_s6.set()
                            gate_s6.wait(timeout=2.0)
                        return res_asr

                    daemon.asr_provider.transcribe = gated_transcribe  # type: ignore[method-assign]
                    daemon._last_trigger_ts = -1e9
                    daemon.handle_trigger("toggle")
                    daemon._last_trigger_ts = -1e9
                    daemon.handle_trigger("toggle")
                    self.assertTrue(in_s6.wait(timeout=2.0))

                    daemon._last_trigger_ts = -1e9
                    daemon.handle_control({"cmd": "rec_start", "no_paste": True, "silent_hud": True})
                    daemon._last_trigger_ts = -1e9
                    daemon.handle_control({"cmd": "rec_stop"})

                    daemon._last_trigger_ts = -1e9
                    daemon.handle_trigger("toggle")
                    daemon._last_trigger_ts = -1e9
                    r_norm8 = daemon.handle_trigger("toggle")
                    target_seq8[0] = r_norm8["seq_id"]

                    gate_s6.set()
                    self.assertTrue(popped_t8.wait(timeout=2.0))
                    # Worker just popped #8 (after silent #7 left _main_hud_visible=False) and is paused before _trigger_lock;
                    # foreground starts recording #9 now!
                    daemon._last_trigger_ts = -1e9
                    r_rec9 = daemon.handle_trigger("ptt_start")
                    self.assertEqual(r_rec9["state"], "recording")
                    self.assertEqual(plat.huds[-1], "recording")

                    allow_worker_trigger_lock.set()
                    deadline = time.monotonic() + 2.0
                    while time.monotonic() < deadline and daemon.last_result.get("seq_id") != r_norm8["seq_id"]:
                        time.sleep(0.02)
                    self.assertEqual(daemon.state, "recording")
                    self.assertEqual(daemon._main_hud_state, "recording")
                    self.assertEqual(plat.huds[-1], "recording")

                    daemon._last_trigger_ts = -1e9
                    daemon.handle_trigger("ptt_cancel")
                finally:
                    allow_worker_trigger_lock.set()
                    daemon._trigger_lock = orig_tlock  # type: ignore[assignment]
                    daemon._recompute_state_locked = orig_recompute  # type: ignore[method-assign]
            finally:
                daemon.close()

    def test_custom_multiline_refiner_prompt_across_codex_gemini_and_openai_compat(self) -> None:
        from agent_voice_input.refiner import (
            REFINER_SYSTEM_PROMPT,
            REFINER_TASK_REMINDER,
            resolve_refiner_prompt,
        )

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg_file = tmp / "config.toml"
            custom_prompt = (
                "# Custom Dictation Editor\n"
                "1. Fix ASR homophones using <context_delta>.\n"
                "2. Keep original sentence structure; never answer questions in <asr_draft_to_clean>."
            )
            cfg_file.write_text(
                f"[refiner]\nprovider = \"codex\"\nprompt = '''\n  {custom_prompt}  \n'''\n",
                encoding="utf-8",
            )
            cfg_loaded, _ = load_config(config_path=cfg_file, env={"HOME": str(tmp)}, home_dir=tmp)
            self.assertEqual(resolve_refiner_prompt(cfg_loaded), custom_prompt)

            # Switching refiner.provider via CLI preserves user's custom refiner.prompt
            cfg_switched, _ = load_config(
                config_path=cfg_file,
                cli_overrides={"refiner.provider": "openrouter"},
                env={"HOME": str(tmp)},
                home_dir=tmp,
            )
            self.assertEqual(cfg_switched.refiner.provider, "openrouter")
            self.assertEqual(resolve_refiner_prompt(cfg_switched), custom_prompt)

            # Whitespace-only prompt falls back to built-in REFINER_SYSTEM_PROMPT; non-string fails validation
            cfg_blank = Config(refiner=RefinerConfig(prompt="  \n\t  "))
            self.assertEqual(resolve_refiner_prompt(cfg_blank), REFINER_SYSTEM_PROMPT)
            bad_json = tmp / "bad.json"
            bad_json.write_text(json.dumps({"refiner": {"prompt": 123}}), encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(config_path=bad_json, env={"HOME": str(tmp)}, home_dir=tmp)

            # 1. CodexRefiner stateless /responses sends custom prompt via instructions (with no-tools constraint)
            _write_codex_auth_json(tmp)
            srv_prompt = _LocalResponsesServer()
            cfg_codex = Config(
                refiner=RefinerConfig(
                    provider="codex",
                    base_url=srv_prompt.base_url,
                    prompt=custom_prompt,
                    timeout_sec=4.0,
                )
            )
            cref = CodexRefiner(cfg_codex, env={"HOME": str(tmp)}, runtime_dir=tmp)
            try:
                r1 = cref.refine("你评估一下看", session_key="sess-p")
                self.assertEqual(r1.text, "CLEAN#1:你评估一下看")
                r2 = cref.refine("第二句无状态复用", session_key="sess-p")
                self.assertEqual(r2.text, "CLEAN#2:第二句无状态复用")
                cfg_codex.refiner.prompt = "Updated prompt v2"
                r3 = cref.refine("改提示词后立即生效", session_key="sess-p")
                self.assertEqual(r3.text, "CLEAN#3:改提示词后立即生效")
            finally:
                cref.close()
                srv_prompt.close()
            self.assertEqual(len(srv_prompt.requests), 3)
            p_b0 = srv_prompt.requests[0]["body"]
            p_b2 = srv_prompt.requests[2]["body"]
            assert isinstance(p_b0, dict) and isinstance(p_b2, dict)
            self.assertTrue(p_b0["instructions"].startswith(custom_prompt))
            self.assertIn("no tools available", p_b0["instructions"].lower())
            self.assertTrue(p_b2["instructions"].startswith("Updated prompt v2"))
            self.assertIn(REFINER_TASK_REMINDER, p_b0["input"][-1]["content"][0]["text"])

            # 2. OpenAICompatibleRefiner & GeminiRefiner receive custom prompt + reminder
            oai_seen: list[HttpRequest] = []
            cfg_oai = Config(
                refiner=RefinerConfig(
                    provider="openai-compatible",
                    base_url="http://127.0.0.1:8000/v1",
                    model="local-qwen",
                    prompt=custom_prompt,
                )
            )
            oai_ref = create_refiner(
                cfg_oai,
                env={},
                transport=lambda req: (
                    oai_seen.append(req)
                    or HttpResponse(
                        200,
                        {"content-type": "application/json"},
                        json.dumps({"choices": [{"message": {"content": "oai ok"}}]}).encode("utf-8"),
                    )
                ),
            )
            self.assertEqual(oai_ref.refine("测试问题？").text, "oai ok")
            oai_payload = json.loads(oai_seen[0].body.decode("utf-8"))
            self.assertEqual(oai_payload["messages"][0], {"role": "system", "content": custom_prompt})
            self.assertIn(REFINER_TASK_REMINDER, oai_payload["messages"][1]["content"])

            gem_seen: list[HttpRequest] = []
            cfg_gem = Config(refiner=RefinerConfig(provider="gemini", prompt=custom_prompt))
            gem_env = {"GEMINI_API_KEY": "gem_test_" + ("k" * 16)}
            gem_ref = create_refiner(
                cfg_gem,
                resolve_credentials(cfg_gem, gem_env),
                env=gem_env,
                transport=lambda req: (
                    gem_seen.append(req)
                    or HttpResponse(
                        200,
                        {"content-type": "application/json"},
                        json.dumps({"candidates": [{"content": {"parts": [{"text": "gem ok"}]}}]}).encode("utf-8"),
                    )
                ),
            )
            self.assertEqual(gem_ref.refine("测试 Gemini 问题？").text, "gem ok")
            gem_payload = json.loads(gem_seen[0].body.decode("utf-8"))
            self.assertEqual(gem_payload["systemInstruction"], {"parts": [{"text": custom_prompt}]})
            self.assertIn(REFINER_TASK_REMINDER, gem_payload["contents"][0]["parts"][0]["text"])

    def test_stateless_responses_cookies_idle_reconnect_api_routing_and_codex_30m_refresh(self) -> None:
        # 1. Config effective_api(), official openai default responses vs custom base_url chat-completions,
        #    explicit api override priority, gemini/codex incompatibility & legacy max_turns_per_thread
        self.assertEqual(RefinerConfig(provider="codex").effective_api(), "responses")
        self.assertEqual(RefinerConfig(provider="gemini").effective_api(), "gemini")
        self.assertEqual(RefinerConfig(provider="off").effective_api(), "off")
        self.assertEqual(RefinerConfig(provider="openai").effective_api(), "responses")
        self.assertEqual(RefinerConfig(provider="openai", base_url="https://api.openai.com/v1").effective_api(), "responses")
        self.assertEqual(RefinerConfig(provider="openai", base_url="https://us.api.openai.com/v1").effective_api(), "responses")
        self.assertEqual(
            RefinerConfig(provider="openai", base_url="https://api.openai.com/v1/chat/completions").effective_api(),
            "chat-completions",
        )
        self.assertEqual(
            RefinerConfig(provider="openai", base_url="https://relay.example.com/v1").effective_api(),
            "chat-completions",
        )
        self.assertEqual(
            RefinerConfig(provider="openai", base_url="http://127.0.0.1:8080/v1").effective_api(),
            "chat-completions",
        )
        self.assertEqual(RefinerConfig(provider="openai", api="chat-completions").effective_api(), "chat-completions")
        self.assertEqual(
            RefinerConfig(provider="openai", api="responses", base_url="https://relay.example.com/v1").effective_api(),
            "responses",
        )
        self.assertEqual(RefinerConfig(provider="auto").effective_api("openai"), "responses")
        self.assertEqual(
            RefinerConfig(provider="auto", base_url="https://relay.example.com/v1").effective_api("openai"),
            "chat-completions",
        )
        for compat_prov in ("openrouter", "dashscope", "volcengine", "groq", "siliconflow"):
            self.assertEqual(RefinerConfig(provider=compat_prov).effective_api(), "chat-completions")
            self.assertEqual(RefinerConfig(provider=compat_prov, api="responses").effective_api(), "responses")

        with tempfile.TemporaryDirectory() as td_cfg:
            oai_test_env = {"HOME": td_cfg, "OPENAI_API_KEY": "sk-oai-" + ("6" * 14)}
            p_compat = Path(td_cfg) / "legacy.toml"
            p_compat.write_text('[refiner]\nprovider = "openai"\napi = "responses"\nmax_turns_per_thread = 42\n', encoding="utf-8")
            loaded_c, _ = load_config(config_path=p_compat, env={"HOME": td_cfg}, home_dir=Path(td_cfg))
            self.assertEqual(loaded_c.refiner.effective_api(), "responses")
            self.assertEqual(loaded_c.refiner.max_turns_per_thread, 42)
            ref_oai_resp = create_refiner(loaded_c, env=oai_test_env)
            self.assertIsInstance(ref_oai_resp, OpenAIResponsesRefiner)
            self.assertNotIsInstance(ref_oai_resp, CodexRefiner)
            ref_oai_resp.close()

            # Default provider="openai" (without explicit api) -> OpenAIResponsesRefiner;
            # custom third-party base_url -> OpenAICompatibleRefiner; explicit api always wins
            ref_oai_def = create_refiner(Config(refiner=RefinerConfig(provider="openai")), env=oai_test_env)
            self.assertIsInstance(ref_oai_def, OpenAIResponsesRefiner)
            self.assertNotIsInstance(ref_oai_def, CodexRefiner)
            ref_oai_def.close()

            ref_oai_proxy = create_refiner(
                Config(refiner=RefinerConfig(provider="openai", base_url="https://relay.example.com/v1")),
                env=oai_test_env,
            )
            self.assertIsInstance(ref_oai_proxy, OpenAICompatibleRefiner)

            ref_oai_chat = create_refiner(
                Config(refiner=RefinerConfig(provider="openai", api="chat-completions")),
                env=oai_test_env,
            )
            self.assertIsInstance(ref_oai_chat, OpenAICompatibleRefiner)

            ref_oai_proxy_resp = create_refiner(
                Config(refiner=RefinerConfig(provider="openai", api="responses", base_url="https://relay.example.com/v1")),
                env=oai_test_env,
            )
            self.assertIsInstance(ref_oai_proxy_resp, OpenAIResponsesRefiner)
            ref_oai_proxy_resp.close()

            for bad_toml in (
                '[refiner]\nprovider = "gemini"\napi = "responses"\n',
                '[refiner]\nprovider = "gemini"\napi = "chat-completions"\n',
                '[refiner]\nprovider = "codex"\napi = "chat-completions"\n',
                '[refiner]\nprovider = "openai"\napi = "invalid-proto"\n',
            ):
                bad_p = Path(td_cfg) / "bad_api.toml"
                bad_p.write_text(bad_toml, encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load_config(config_path=bad_p, env={"HOME": td_cfg}, home_dir=Path(td_cfg))

        # 2. OpenAIResponsesRefiner: HTTP 200 without Content-Type, LB Cookie allowlist, account-switch reset,
        #    and transparent 1x Keep-Alive idle-disconnect reconnect (zero max_retries consumed!)
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            drop_second_socket = [True]

            def cookie_and_drop_responder(handler, rec) -> None:
                seq = int(rec["seq"])
                if seq == 1:
                    _LocalResponsesServer.send_sse_200(
                        handler,
                        _sse_completed_frames(
                            ["R1-OK"],
                            usage={"input_tokens": 5000, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 12},
                        ),
                        set_cookies=[
                            "__cf_bm=bm_v1; Path=/; HttpOnly; Secure",
                            "__cflb=lb_node_a; Path=/; HttpOnly",
                            "__oailb=oai_shard_7; Path=/; Secure",
                            "chatgpt_user_secret=MUST_NEVER_BE_STORED; Path=/",
                        ],
                    )
                    return
                if seq == 2 and drop_second_socket[0]:
                    drop_second_socket[0] = False
                    # Simulate Cloudflare closing an idle Keep-Alive TCP connection before sending status line!
                    handler.close_connection = True
                    try:
                        handler.connection.shutdown(2)
                    except Exception:
                        pass
                    handler.connection.close()
                    return
                _LocalResponsesServer.send_sse_200(
                    handler,
                    _sse_completed_frames(
                        [f"R{seq}-OK"],
                        usage={"input_tokens": 5010, "input_tokens_details": {"cached_tokens": 4864}, "output_tokens": 14, "output_tokens_details": {"reasoning_tokens": 4}},
                    ),
                )

            srv = _LocalResponsesServer(responder=cookie_and_drop_responder)
            try:
                _write_codex_auth_json(tmp, account_id="acct-alpha")
                # max_retries = 0 proves idle-disconnect reconnect does NOT consume max_retries budget!
                cfg = Config(refiner=RefinerConfig(provider="codex", base_url=srv.base_url, max_retries=0, timeout_sec=5.0))
                ref = CodexRefiner(cfg, env={"HOME": str(tmp)}, runtime_dir=tmp)
                try:
                    res1 = ref.refine("第一轮拿 Cookie", session_key="codex:s1", fallback_context="ctx_body")
                    self.assertEqual(res1.text, "R1-OK")
                    self.assertEqual((res1.input_tokens, res1.cached_input_tokens, res1.output_tokens, res1.reasoning_tokens), (5000, 0, 12, 0))

                    # Req 2 hits simulated idle Keep-Alive disconnect on reused socket, reconnects 1x transparently & echoes LB cookies!
                    res2 = ref.refine("第二轮断连重发", session_key="codex:s1", fallback_context="ctx_body")
                    self.assertEqual((res2.text, res2.used_fallback, res2.retries), ("R3-OK", "", 0))
                    self.assertEqual((res2.input_tokens, res2.cached_input_tokens, res2.output_tokens, res2.reasoning_tokens), (5010, 4864, 14, 4))

                    hdr_reconnected = srv.requests[-1]["headers"]
                    assert isinstance(hdr_reconnected, dict)
                    cookie_sent = hdr_reconnected.get("cookie", "")
                    self.assertIn("__cf_bm=bm_v1", cookie_sent)
                    self.assertIn("__cflb=lb_node_a", cookie_sent)
                    self.assertIn("__oailb=oai_shard_7", cookie_sent)
                    self.assertNotIn("chatgpt_user_secret", cookie_sent)
                    port_acct_alpha = srv.requests[-1]["client_port"]

                    # Switch account_id in auth.json (bumping mtime_ns) -> clears LB cookies AND closes old Keep-Alive socket!
                    auth_p = _write_codex_auth_json(tmp, account_id="acct-beta")
                    st_prev = auth_p.stat()
                    os.utime(auth_p, ns=(st_prev.st_atime_ns, st_prev.st_mtime_ns + 5_000_000))
                    res3 = ref.refine("切换账号后清空 Cookie", session_key="codex:s1")
                    self.assertEqual(res3.used_fallback, "")
                    hdr_beta = srv.requests[-1]["headers"]
                    assert isinstance(hdr_beta, dict)
                    self.assertEqual(hdr_beta.get("chatgpt-account-id"), "acct-beta")
                    self.assertNotIn("cookie", hdr_beta)
                    self.assertNotEqual(srv.requests[-1]["client_port"], port_acct_alpha)
                finally:
                    ref.close()

                # Also verify provider="openai" + api="responses" wire format via OpenAIResponsesRefiner
                srv.requests.clear()
                cfg_oai_r = Config(
                    refiner=RefinerConfig(
                        provider="openai",
                        api="responses",
                        model="gpt-5-mini",
                        base_url=f"http://127.0.0.1:{srv.port}/v1",
                        effort="low",
                    )
                )
                env_oai = {"HOME": str(tmp), "OPENAI_API_KEY": "sk-oai-direct-responses-999"}
                ref_oai = create_refiner(cfg_oai_r, resolve_credentials(cfg_oai_r, env_oai), env=env_oai)
                self.assertIsInstance(ref_oai, OpenAIResponsesRefiner)
                try:
                    out_oai = ref_oai.refine("标准平台 Responses 测试", session_key="claude:s9", fallback_context="log_1")
                    self.assertEqual(out_oai.provider, "openai")
                    self.assertEqual(out_oai.used_fallback, "")
                    req_oai = srv.requests[-1]
                    self.assertEqual(req_oai["path"], "/v1/responses")
                    h_oai = req_oai["headers"]
                    assert isinstance(h_oai, dict)
                    self.assertEqual(h_oai.get("authorization"), "Bearer sk-oai-direct-responses-999")
                    self.assertNotIn("chatgpt-account-id", h_oai)
                    self.assertNotIn("originator", h_oai)
                finally:
                    ref_oai.close()
            finally:
                srv.close()

        # 3. CodexRefiner: api_key mode routing, JWT account_id fallback, 30-min (1800s) proactive refresh,
        #    300s failure cooldown while token still valid (>60s), HTTP 401 refresh+retry, Keyring mode,
        #    and strict env={"HOME": td} hermetic isolation!
        with tempfile.TemporaryDirectory() as td_auth:
            tmp_a = Path(td_auth)
            srv_a = _LocalResponsesServer()
            srv_oai_mode = _LocalResponsesServer()
            try:
                # 3a. auth_mode="api_key" routes to api.openai.com/v1 (or base_url) without subscription headers
                home_ak = tmp_a / "home_apikey"
                _write_codex_auth_json(home_ak, auth_mode="api_key", openai_api_key="sk-from-auth-json-111")
                ref_ak = CodexRefiner(
                    Config(refiner=RefinerConfig(provider="codex", base_url=f"http://127.0.0.1:{srv_oai_mode.port}/v1")),
                    env={"HOME": str(home_ak)},
                )
                try:
                    r_ak = ref_ak.refine("API Key 登录态路由")
                    self.assertEqual(r_ak.used_fallback, "")
                    self.assertEqual(srv_oai_mode.requests[-1]["path"], "/v1/responses")
                    h_ak = srv_oai_mode.requests[-1]["headers"]
                    assert isinstance(h_ak, dict)
                    self.assertEqual(h_ak.get("authorization"), "Bearer sk-from-auth-json-111")
                    self.assertNotIn("chatgpt-account-id", h_ak)
                finally:
                    ref_ak.close()

                # 3b. 30-minute (1800s) refresh window + JWT chatgpt_account_id claim fallback + Stage 1 OPENAI_API_KEY stripped
                home_30m = tmp_a / "home_30m"
                now_epoch = [1_750_000_000.0]
                now_mono = [100.0]
                tok_initial = _make_fake_jwt(exp=int(now_epoch[0]) + 2000, account_id="acct-from-jwt-claim")
                tok_refreshed = _make_fake_jwt(exp=int(now_epoch[0]) + 86400 * 10, account_id="acct-refreshed-disk")
                # Write auth.json WITHOUT top-level tokens.account_id to exercise JWT claim extraction!
                _write_codex_auth_json(home_30m, access_token=tok_initial, account_id="")
                rpc_log_30m = tmp_a / "rpc_30m.jsonl"
                auth_file_30m = home_30m / ".codex" / "auth.json"

                ref_30m = CodexRefiner(
                    Config(refiner=RefinerConfig(provider="codex", base_url=srv_a.base_url)),
                    env={"HOME": str(home_30m), "OPENAI_API_KEY": "sk-stage1-asr-key-must-not-leak"},
                    app_server_cmd=[
                        sys.executable,
                        "-u",
                        "-c",
                        _MOCK_AUTH_APP_SERVER_CODE,
                        str(rpc_log_30m),
                        "ok",
                        tok_refreshed,
                        str(auth_file_30m),
                    ],
                    time_fn=lambda: now_epoch[0],
                    clock_fn=lambda: now_mono[0],
                )
                try:
                    # At T+0 (exp - now = 2000s >= 1800s): uses auth.json directly, 0 getAuthStatus spawns!
                    srv_a.requests.clear()
                    r_pre = ref_30m.refine("未进三十分钟窗口")
                    self.assertEqual(r_pre.used_fallback, "")
                    self.assertFalse(rpc_log_30m.exists())
                    h_pre = srv_a.requests[-1]["headers"]
                    assert isinstance(h_pre, dict)
                    self.assertEqual(h_pre.get("chatgpt-account-id"), "acct-from-jwt-claim")
                    self.assertEqual(h_pre.get("authorization"), f"Bearer {tok_initial}")

                    # Advance clock by 210s -> exp - now = 1790s (< 1800s): triggers getAuthStatus(includeToken=True, refreshToken=True)!
                    now_epoch[0] += 210.0
                    now_mono[0] += 210.0
                    r_post = ref_30m.refine("进入三十分钟提前刷新窗口")
                    self.assertEqual(r_post.used_fallback, "")
                    self.assertTrue(rpc_log_30m.is_file())
                    rpc_lines = [json.loads(ln) for ln in rpc_log_30m.read_text(encoding="utf-8").splitlines() if ln.strip()]
                    get_auth_req = next(r for r in rpc_lines if r.get("method") == "getAuthStatus")
                    self.assertEqual(get_auth_req["params"], {"includeToken": True, "refreshToken": True})
                    self.assertFalse(get_auth_req["_env_has_oai"])
                    h_post = srv_a.requests[-1]["headers"]
                    assert isinstance(h_post, dict)
                    self.assertEqual(h_post.get("authorization"), f"Bearer {tok_refreshed}")
                    self.assertEqual(h_post.get("chatgpt-account-id"), "acct-refreshed-disk")
                finally:
                    ref_30m.close()

                # 3c. Transient refresh failure when 60 < exp - now < 1800 -> passes through with valid token + 300s cooldown;
                #     when exp - now <= 60 (hard expired) -> fails cleanly to asr_draft!
                home_cd = tmp_a / "home_cooldown"
                t_epoch_cd = [1_750_010_000.0]
                t_mono_cd = [500.0]
                tok_900s = _make_fake_jwt(exp=int(t_epoch_cd[0]) + 900, account_id="acct-cd")
                _write_codex_auth_json(home_cd, access_token=tok_900s, account_id="acct-cd")
                rpc_log_cd = tmp_a / "rpc_cd.jsonl"
                ref_cd = CodexRefiner(
                    Config(refiner=RefinerConfig(provider="codex", base_url=srv_a.base_url, max_retries=0)),
                    env={"HOME": str(home_cd)},
                    app_server_cmd=[sys.executable, "-u", "-c", _MOCK_AUTH_APP_SERVER_CODE, str(rpc_log_cd), "fail"],
                    time_fn=lambda: t_epoch_cd[0],
                    clock_fn=lambda: t_mono_cd[0],
                )
                try:
                    # Call 1 (rem=900s): refresh fails, falls back to still-valid tok_900s & sets 300s cooldown!
                    rcd1 = ref_cd.refine("刷新失败但旧票据仍有效")
                    self.assertEqual((rcd1.text, rcd1.used_fallback), ("CLEAN#" + str(len(srv_a.requests)) + ":刷新失败但旧票据仍有效", ""))
                    calls_after_1 = len([json.loads(ln) for ln in rpc_log_cd.read_text(encoding="utf-8").splitlines() if "getAuthStatus" in ln])
                    self.assertEqual(calls_after_1, 1)

                    # Call 2 (+120s < 300s cooldown): does NOT spawn getAuthStatus again!
                    t_epoch_cd[0] += 120.0
                    t_mono_cd[0] += 120.0
                    rcd2 = ref_cd.refine("冷却期内零子进程")
                    self.assertEqual(rcd2.used_fallback, "")
                    calls_after_2 = len([json.loads(ln) for ln in rpc_log_cd.read_text(encoding="utf-8").splitlines() if "getAuthStatus" in ln])
                    self.assertEqual(calls_after_2, 1)

                    # Call 3: advance past hard expiry (rem=30s <= 60s) -> fails cleanly to asr_draft!
                    t_epoch_cd[0] += 750.0
                    t_mono_cd[0] += 750.0
                    rcd3 = ref_cd.refine("真过期降级原稿")
                    self.assertEqual((rcd3.text, rcd3.used_fallback), ("真过期降级原稿", "asr_draft"))
                finally:
                    ref_cd.close()

                # 3d. HTTP 401 triggers refresh + 1 immediate retry; Keyring (no auth.json) exports token;
                #     and hermetic env={"HOME": empty_dir} never touches host ~/.codex or host PATH!
                tok_stale = _make_fake_jwt(exp=4102444800, account_id="acct-401")
                tok_fresh_401 = _make_fake_jwt(exp=4102488888, account_id="acct-401")

                def responder_401(handler, rec) -> None:
                    hdrs = rec["headers"]
                    assert isinstance(hdrs, dict)
                    if hdrs.get("authorization") != f"Bearer {tok_fresh_401}":
                        _LocalResponsesServer.send_json(handler, 401, {"detail": "Token revoked"})
                        return
                    _LocalResponsesServer.send_sse_200(handler, _sse_completed_frames(["401刷新恢复成功"]))

                srv_401 = _LocalResponsesServer(responder=responder_401)
                try:
                    # Keyring mode (NO auth.json on disk at all!)
                    home_kr = tmp_a / "home_keyring"
                    home_kr.mkdir(parents=True)
                    rpc_log_kr = tmp_a / "rpc_kr.jsonl"
                    tok_kr = _make_fake_jwt(exp=4102444800, account_id="acct-keyring-jwt")
                    srv_a.requests.clear()
                    ref_kr = CodexRefiner(
                        Config(refiner=RefinerConfig(provider="codex", base_url=srv_a.base_url)),
                        env={"HOME": str(home_kr)},
                        app_server_cmd=[sys.executable, "-u", "-c", _MOCK_AUTH_APP_SERVER_CODE, str(rpc_log_kr), "ok", tok_kr],
                    )
                    try:
                        r_kr1 = ref_kr.refine("钥匙串第一轮")
                        r_kr2 = ref_kr.refine("钥匙串第二轮内存复用")
                        self.assertEqual((r_kr1.used_fallback, r_kr2.used_fallback), ("", ""))
                        self.assertFalse((home_kr / ".codex" / "auth.json").exists())
                        kr_calls = [json.loads(ln) for ln in rpc_log_kr.read_text(encoding="utf-8").splitlines() if "getAuthStatus" in ln]
                        self.assertEqual(len(kr_calls), 1)
                        h_kr = srv_a.requests[-1]["headers"]
                        assert isinstance(h_kr, dict)
                        self.assertEqual(h_kr.get("chatgpt-account-id"), "acct-keyring-jwt")
                    finally:
                        ref_kr.close()

                    # HTTP 401 refresh & retry
                    home_401 = tmp_a / "home_401"
                    auth_401_path = _write_codex_auth_json(home_401, access_token=tok_stale, account_id="acct-401")
                    rpc_log_401 = tmp_a / "rpc_401.jsonl"
                    ref_401 = CodexRefiner(
                        Config(refiner=RefinerConfig(provider="codex", base_url=srv_401.base_url, max_retries=0)),
                        env={"HOME": str(home_401)},
                        app_server_cmd=[
                            sys.executable,
                            "-u",
                            "-c",
                            _MOCK_AUTH_APP_SERVER_CODE,
                            str(rpc_log_401),
                            "ok",
                            tok_fresh_401,
                            str(auth_401_path),
                        ],
                    )
                    try:
                        r_401 = ref_401.refine("遇401自动刷新重试")
                        self.assertEqual((r_401.text, r_401.used_fallback, len(srv_401.requests)), ("401刷新恢复成功", "", 2))
                    finally:
                        ref_401.close()

                    # Strict hermetic isolation: env={"HOME": empty_hermetic} with NO auth.json and NO PATH/CODEX_BIN
                    # MUST NEVER read host ~/.codex/auth.json or invoke host `codex` binary!
                    home_hermetic = tmp_a / "home_hermetic_empty"
                    home_hermetic.mkdir(parents=True)
                    srv_a.requests.clear()
                    ref_herm = CodexRefiner(
                        Config(refiner=RefinerConfig(provider="codex", base_url=srv_a.base_url, max_retries=0)),
                        env={"HOME": str(home_hermetic)},
                    )
                    try:
                        r_herm = ref_herm.refine("严格隔离不碰宿主机")
                        self.assertEqual((r_herm.text, r_herm.used_fallback), ("严格隔离不碰宿主机", "asr_draft"))
                        self.assertEqual(len(srv_a.requests), 0)
                    finally:
                        ref_herm.close()
                finally:
                    srv_401.close()
            finally:
                srv_a.close()
                srv_oai_mode.close()

    def test_message_boundary_chunking_git_status_toggle_and_anti_chat_wire_format(self) -> None:
        import concurrent.futures
        import subprocess
        from agent_voice_input.session_ctx import (
            DEFAULT_REFINER_CHUNK_CHARS,
            DEFAULT_REFINER_CONTEXT_MAX_CHARS,
            chunk_turns_for_refiner,
        )

        self.assertEqual(RefinerConfig().context_max_chars, 200000)
        self.assertEqual(DEFAULT_REFINER_CONTEXT_MAX_CHARS, 200000)
        self.assertEqual(DEFAULT_REFINER_CHUNK_CHARS, 50000)

        # 1. Deterministic immutable chunk boundaries: adding a new turn freezes earlier sealed chunks
        turns_base = [
            ("user", "U1-" + ("a" * 9000)),
            ("assistant", "A1-" + ("b" * 9000)),
            ("user", "U2-" + ("c" * 8000)),
        ]
        c_before = chunk_turns_for_refiner(turns_base, max_chars=200000, chunk_chars=16000)
        self.assertEqual(len(c_before), 3)
        self.assertIn("[Speaker]", c_before[0])
        self.assertIn("[CLI Agent]", c_before[1])
        self.assertNotIn("[Assistant]", "\n".join(c_before))

        turns_grown = [*turns_base, ("assistant", "A2-new-reply")]
        c_after = chunk_turns_for_refiner(turns_grown, max_chars=200000, chunk_chars=16000)
        self.assertEqual(c_before[0], c_after[0])
        self.assertEqual(c_before[1], c_after[1])
        self.assertIn("A2-new-reply", c_after[-1])

        # Whole-chunk 1/4-window eviction when total exceeds max_chars keeps surviving chunks byte-identical
        c_evicted = chunk_turns_for_refiner(turns_grown, max_chars=20000, chunk_chars=10000)
        self.assertEqual(len(c_evicted), 2)
        self.assertEqual(c_evicted[0], c_after[1])
        self.assertEqual(c_evicted[1], c_after[2])

        # 2. End-to-end wire payload verification: single user message + git status tail + zero assistant messages
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            subprocess.run(["git", "init", "-q"], cwd=str(tmp), check=True)
            (tmp / "feature_alpha.py").write_text("x = 1\n", encoding="utf-8")

            sid = "ef012345-0000-0000-0000-000000000099"
            sdir = tmp / ".codex" / "sessions" / "2026" / "11" / "16"
            sdir.mkdir(parents=True)
            rollout = sdir / f"rollout-2026-{sid}.jsonl"
            rollout.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": sid, "source": "cli", "cwd": str(tmp)}})
                + "\n"
                + json.dumps({"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "UserMessage", "content": [{"type": "text", "text": "先看下旧逻辑" + ("x" * 30000)}]}}})
                + "\n"
                + json.dumps({"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "AgentMessage", "content": [{"type": "text", "text": "旧逻辑已读完" + ("y" * 30000)}]}}})
                + "\n",
                encoding="utf-8",
            )
            write_session_registry_entry(tmp, cli="codex", session_id=sid, jsonl_path=str(rollout), cwd=str(tmp))
            tracker = SessionTracker(home_dir=tmp, env={}, runtime_dir=tmp, proc_root=tmp / "empty_proc")
            snap = tracker.sync_session(window_title="codex [#ef012345]")
            assert snap is not None

            # Verify config parsing & validation for include_git_status and context_max_chars
            cfg_toml = tmp / "ctx.toml"
            cfg_toml.write_text(
                "[refiner]\n"
                'provider = "openai-compatible"\n'
                'base_url = "http://127.0.0.1:8080/v1"\n'
                'model = "qwen-local"\n'
                "include_git_status = true\n"
                "context_max_chars = 200000\n",
                encoding="utf-8",
            )
            cfg_on, _ = load_config(config_path=cfg_toml, env={"HOME": str(tmp)}, home_dir=tmp)
            self.assertIs(cfg_on.refiner.include_git_status, True)
            self.assertEqual(cfg_on.refiner.context_max_chars, 200000)

            bad_ctx = tmp / "bad_ctx.toml"
            bad_ctx.write_text("[refiner]\ncontext_max_chars = -1\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(config_path=bad_ctx, env={"HOME": str(tmp)}, home_dir=tmp)

            # OpenAI-compatible wire payload: strictly 1 system + 1 user message (zero consecutive user / assistant msgs)
            oai_reqs: list[HttpRequest] = []
            oai_ref = create_refiner(
                cfg_on,
                env={},
                transport=lambda req: (
                    oai_reqs.append(req)
                    or HttpResponse(
                        200,
                        {"content-type": "application/json"},
                        json.dumps({"choices": [{"message": {"content": "修好的结果"}}]}).encode("utf-8"),
                    )
                ),
            )
            oai_ref.refine("feature alpha 点 py 怎么样了？", session_key=snap.session_key, tracker=tracker)
            body_on = json.loads(oai_reqs[-1].body.decode("utf-8"))
            msgs_on = body_on["messages"]
            self.assertEqual([m["role"] for m in msgs_on], ["system", "user"])
            user_on = msgs_on[1]["content"]
            self.assertEqual(user_on.count('<session_reference_log read_only="true">'), 2)
            final_pos_on = user_on.index("<workspace_git_status")
            prefix_on = user_on[:final_pos_on]
            # git_status sits inside the final block right before <asr_draft_to_clean>, never in the leading 50K prefix!
            self.assertNotIn("feature_alpha.py", prefix_on)
            self.assertIn("<workspace_git_status", user_on[final_pos_on:])
            self.assertIn("feature_alpha.py", user_on[final_pos_on:])
            self.assertLess(
                user_on.index("</workspace_git_status>"),
                user_on.index("<asr_draft_to_clean>"),
            )
            self.assertLess(
                user_on.index("</asr_draft_to_clean>"),
                user_on.index("<task_reminder>"),
            )

            # Flip include_git_status=False -> leading prefix stays 100% byte-identical, git block omitted!
            cfg_off = Config(
                refiner=RefinerConfig(
                    provider="openai-compatible",
                    base_url="http://127.0.0.1:8080/v1",
                    model="qwen-local",
                    include_git_status=False,
                    context_max_chars=200000,
                )
            )
            oai_ref_off = create_refiner(
                cfg_off,
                env={},
                transport=lambda req: (
                    oai_reqs.append(req)
                    or HttpResponse(
                        200,
                        {"content-type": "application/json"},
                        json.dumps({"choices": [{"message": {"content": "无 git 状态结果"}}]}).encode("utf-8"),
                    )
                ),
            )
            oai_ref_off.refine("第二句提问？", session_key=snap.session_key, tracker=tracker)
            msgs_off = json.loads(oai_reqs[-1].body.decode("utf-8"))["messages"]
            self.assertEqual([m["role"] for m in msgs_off], ["system", "user"])
            user_off = msgs_off[1]["content"]
            self.assertEqual(prefix_on, user_off[: user_off.index("<asr_draft_to_clean>")])
            self.assertNotIn('<workspace_git_status read_only="true">', user_off)
            self.assertNotIn("</workspace_git_status>", user_off)
            self.assertNotIn("feature_alpha.py", user_off)

            # Gemini native wire payload uses 3-turn stateless implicit-cache layout when stable prefix blocks exist
            gem_reqs: list[HttpRequest] = []
            gem_env = {"GEMINI_API_KEY": "gem_boundary_" + ("z" * 16)}
            cfg_gem = Config(refiner=RefinerConfig(provider="gemini", include_git_status=True))
            gem_ref = create_refiner(
                cfg_gem,
                resolve_credentials(cfg_gem, gem_env),
                env=gem_env,
                transport=lambda req: (
                    gem_reqs.append(req)
                    or HttpResponse(
                        200,
                        {"content-type": "application/json"},
                        json.dumps({"candidates": [{"content": {"parts": [{"text": "gem boundary ok"}]}}]}).encode("utf-8"),
                    )
                ),
            )
            gem_ref.refine("检查一下 feature alpha", session_key=snap.session_key, tracker=tracker)
            gem_body = json.loads(gem_reqs[-1].body.decode("utf-8"))
            self.assertEqual([c["role"] for c in gem_body["contents"]], ["user", "model", "user"])
            self.assertEqual(gem_body["contents"][1]["parts"], [{"text": "Understood."}])
            self.assertEqual(len(gem_body["contents"][0]["parts"]), 1)
            self.assertIn('<session_reference_log read_only="true">', gem_body["contents"][0]["parts"][0]["text"])
            self.assertEqual(len(gem_body["contents"][2]["parts"]), 2)
            self.assertIn('<session_reference_log read_only="true">', gem_body["contents"][2]["parts"][0]["text"])
            self.assertIn("<workspace_git_status", gem_body["contents"][2]["parts"][1]["text"])

            # 3. Stateless Responses wire layout: N developer <session_reference_log> items + 1 trailing user draft+git item,
            #    plus safe concurrent same-session execution!
            _write_codex_auth_json(tmp)
            srv_ov = _LocalResponsesServer()
            ref_ov = CodexRefiner(
                Config(refiner=RefinerConfig(provider="codex", base_url=srv_ov.base_url, include_git_status=True, timeout_sec=5.0)),
                env={"HOME": str(tmp)},
                runtime_dir=tmp / "rt_ov",
            )
            try:
                r_wire = ref_ov.refine("检查多块 developer 布局", session_key=snap.session_key, tracker=tracker)
                self.assertEqual(r_wire.used_fallback, "")
                w_body = srv_ov.requests[-1]["body"]
                assert isinstance(w_body, dict)
                w_in = w_body["input"]
                self.assertEqual([it["role"] for it in w_in], ["developer", "developer", "user"])
                self.assertNotIn("feature_alpha.py", w_in[0]["content"][0]["text"])
                self.assertNotIn("feature_alpha.py", w_in[1]["content"][0]["text"])
                self.assertIn("<workspace_git_status", w_in[2]["content"][0]["text"])
                self.assertIn("feature_alpha.py", w_in[2]["content"][0]["text"])
                self.assertLess(
                    w_in[2]["content"][0]["text"].index("</workspace_git_status>"),
                    w_in[2]["content"][0]["text"].index("<asr_draft_to_clean>"),
                )

                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    fa = pool.submit(ref_ov.refine, "同会话并发A", session_key="sess-same")
                    fb = pool.submit(ref_ov.refine, "同会话并发B", session_key="sess-same")
                    ra, rb = fa.result(timeout=5.0), fb.result(timeout=5.0)
                self.assertTrue(ra.text.endswith(":同会话并发A"))
                self.assertTrue(rb.text.endswith(":同会话并发B"))
            finally:
                ref_ov.close()
                srv_ov.close()

    def test_refiner_general_mode_strips_code_context_and_switches_prompt(self) -> None:
        import subprocess
        from agent_voice_input.refiner import (
            REFINER_GENERAL_SYSTEM_PROMPT,
            REFINER_SYSTEM_PROMPT,
            resolve_refiner_prompt,
        )
        from agent_voice_input.session_ctx import SessionMatch

        self.assertEqual(resolve_refiner_prompt(None, mode="agent"), REFINER_SYSTEM_PROMPT)
        self.assertEqual(resolve_refiner_prompt(None, mode="general"), REFINER_GENERAL_SYSTEM_PROMPT)
        self.assertNotEqual(REFINER_SYSTEM_PROMPT, REFINER_GENERAL_SYSTEM_PROMPT)
        self.assertIn("Mode: general text input", REFINER_GENERAL_SYSTEM_PROMPT)
        # Explicit user prompt overrides both modes
        cfg_custom = Config(refiner=RefinerConfig(prompt="CUSTOM_OVERRIDE_PROMPT"))
        self.assertEqual(resolve_refiner_prompt(cfg_custom, mode="general"), "CUSTOM_OVERRIDE_PROMPT")

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            subprocess.run(["git", "init", "-q"], cwd=str(tmp), check=True)
            (tmp / "secret_repo_module.py").write_text("x = 1\n", encoding="utf-8")
            jpath = tmp / "rollout-gen.jsonl"
            jpath.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "session_meta", "payload": {"id": "gen-1", "source": "cli", "cwd": str(tmp)}}),
                        json.dumps(
                            {
                                "type": "event_msg",
                                "payload": {
                                    "type": "item_completed",
                                    "item": {"type": "UserMessage", "content": [{"type": "text", "text": "讨论 SessionTracker 代码"}]},
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            tracker = SessionTracker(home_dir=tmp, runtime_dir=tmp / "rt", proc_root=tmp / "no_proc")
            snap = tracker.sync_session(
                match=SessionMatch(cli="codex", session_id="gen-1", jsonl_path=str(jpath), cwd=str(tmp), matched_by="test")
            )
            assert snap is not None

            # 1. OpenAICompatibleRefiner in general mode
            oai_reqs: list[HttpRequest] = []
            oai_ref = create_refiner(
                Config(refiner=RefinerConfig(provider="openai-compatible", base_url="http://127.0.0.1:8080/v1", model="qwen-local")),
                env={},
                transport=lambda req: (
                    oai_reqs.append(req)
                    or HttpResponse(
                        200,
                        {"content-type": "application/json"},
                        json.dumps({"choices": [{"message": {"content": "周末约饭结果"}}]}).encode("utf-8"),
                    )
                ),
            )
            oai_res = oai_ref.refine(
                "周末一起吃饭吧",
                session_key=snap.session_key,
                tracker=tracker,
                fallback_context="FALLBACK_LEAK",
                mode="general",
            )
            self.assertEqual(oai_res.context_chunks, 0)
            self.assertEqual(oai_res.context_chars, 0)
            oai_body = json.loads(oai_reqs[-1].body.decode("utf-8"))
            self.assertEqual(oai_body["messages"][0]["content"], REFINER_GENERAL_SYSTEM_PROMPT)
            oai_user = oai_body["messages"][1]["content"]
            self.assertNotIn('read_only="true"', oai_user)
            self.assertNotIn("</session_reference_log>", oai_user)
            self.assertNotIn("</workspace_git_status>", oai_user)
            self.assertNotIn("SessionTracker", oai_user)
            self.assertNotIn("secret_repo_module.py", oai_user)
            self.assertNotIn("FALLBACK_LEAK", oai_user)

            # 2. GeminiRefiner in general mode
            gem_reqs: list[HttpRequest] = []
            gem_env = {"GEMINI_API_KEY": "gem_gen_" + ("z" * 16)}
            cfg_gem = Config(refiner=RefinerConfig(provider="gemini"))
            gem_ref = create_refiner(
                cfg_gem,
                resolve_credentials(cfg_gem, gem_env),
                env=gem_env,
                transport=lambda req: (
                    gem_reqs.append(req)
                    or HttpResponse(
                        200,
                        {"content-type": "application/json"},
                        json.dumps({"candidates": [{"content": {"parts": [{"text": "gem general ok"}]}}]}).encode("utf-8"),
                    )
                ),
            )
            gem_ref.refine("周末一起吃饭吧", session_key=snap.session_key, tracker=tracker, mode="general")
            gem_body = json.loads(gem_reqs[-1].body.decode("utf-8"))
            self.assertEqual(gem_body["systemInstruction"]["parts"][0]["text"], REFINER_GENERAL_SYSTEM_PROMPT)
            gem_txt = json.dumps(gem_body["contents"], ensure_ascii=False)
            self.assertNotIn("</session_reference_log>", gem_txt)
            self.assertNotIn("</workspace_git_status>", gem_txt)
            self.assertNotIn("secret_repo_module.py", gem_txt)

            # 3. CodexRefiner (/responses) + run_pipeline_once in general mode
            _write_codex_auth_json(tmp)
            srv_gen = _LocalResponsesServer()
            ref_gen = CodexRefiner(
                Config(refiner=RefinerConfig(provider="codex", base_url=srv_gen.base_url, timeout_sec=5.0)),
                env={"HOME": str(tmp)},
                runtime_dir=tmp / "rt_gen",
            )
            seen_asr_hw: list[list[str]] = []

            class _SpyAsr:
                provider_name = "dashscope"

                def transcribe(self, wav_bytes: bytes, context: object = None, **_kw: object) -> AsrResult:
                    hw = list(getattr(context, "hotwords", ()))
                    seen_asr_hw.append(hw)
                    return AsrResult(text="今天天气真好", provider="dashscope", model="qwen3-asr-flash", latency_ms=20)

            try:
                pipe_gen = run_pipeline_once(
                    _voiced_wav(),
                    cfg=Config(refiner=RefinerConfig(provider="codex", base_url=srv_gen.base_url)),
                    tracker=tracker,
                    asr_provider=_SpyAsr(),
                    refiner=ref_gen,
                    window_title_override="[codex:#gen-1]",
                    dry_run=True,
                    env={"HOME": str(tmp)},
                    mode="general",
                )
                self.assertTrue(pipe_gen.ok)
                self.assertIsNone(pipe_gen.session)
                self.assertEqual(seen_asr_hw, [[]])
                gen_req = srv_gen.requests[-1]["body"]
                assert isinstance(gen_req, dict)
                self.assertIn("Mode: general text input", gen_req["instructions"])
                self.assertEqual(len(gen_req["input"]), 1)
                self.assertEqual(gen_req["input"][0]["role"], "user")
                only_txt = gen_req["input"][0]["content"][0]["text"]
                self.assertNotIn("</session_reference_log>", only_txt)
                self.assertNotIn("</workspace_git_status>", only_txt)
            finally:
                ref_gen.close()
                srv_gen.close()

    def test_codex_parallel_asr_socket_prewarm_and_hermetic_find_codex_bin(self) -> None:
        from agent_voice_input.refiner import _DISABLE, find_codex_bin
        import fcntl

        self.assertIn("shell_snapshot", _DISABLE)
        # Hermetic empty env={} and explicit PATH without codex MUST NEVER leak into host os.environ or /opt/homebrew!
        self.assertIsNone(find_codex_bin({}))
        with tempfile.TemporaryDirectory() as td_bin:
            self.assertIsNone(find_codex_bin({"HOME": td_bin, "PATH": td_bin}))

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home_p = tmp / "home"
            _write_codex_auth_json(home_p)
            agy_cid = "fedcba98-0000-4000-8000-000000000001"
            agy_pres = home_p / ".gemini" / "antigravity-cli" / "presence"
            agy_pres.mkdir(parents=True)
            agy_lock_fd = os.open(str(agy_pres / f"{agy_cid}.lock"), os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(agy_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            agy_logs = home_p / ".gemini" / "antigravity-cli" / "brain" / agy_cid / ".system_generated" / "logs"
            agy_logs.mkdir(parents=True)
            agy_file = agy_logs / "transcript_full.jsonl"
            agy_file.write_text(
                json.dumps({"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>\n预热会话\n</USER_REQUEST>"}) + "\n",
                encoding="utf-8",
            )
            rt_pre = tmp / "rt_pre"
            rt_pre.mkdir(mode=0o700)
            write_session_registry_entry(rt_pre, cli="agy", session_id=agy_cid, jsonl_path=str(agy_file), cwd=str(tmp))
            tracker = SessionTracker(home_dir=home_p, runtime_dir=rt_pre, proc_root=None)

            srv_pre = _LocalResponsesServer()
            cfg_pre = Config(refiner=RefinerConfig(provider="codex", base_url=srv_pre.base_url, timeout_sec=5.0))
            ref_pre = CodexRefiner(cfg_pre, env={"HOME": str(home_p)}, runtime_dir=rt_pre)
            saw_tcp_prewarm_during_asr = [False]

            class _SlowInspectAsr(BaseAsrProvider):
                provider_name = "dashscope"

                def __init__(self) -> None:
                    pass

                def transcribe(self, wav_bytes: bytes, context: object = None, *, is_cancelled: object = None, **_kw: object) -> AsrResult:
                    deadline_w = time.monotonic() + 1.5
                    while time.monotonic() < deadline_w:
                        # Verify prepare_session() connected TCP socket BEFORE any HTTP request is sent!
                        if srv_pre.accepted_conns >= 1 and len(srv_pre.requests) == 0:
                            saw_tcp_prewarm_during_asr[0] = True
                            break
                        time.sleep(0.01)
                    return AsrResult(text="并行预热语音草稿", provider="dashscope", model="qwen3-asr-flash", latency_ms=80)

            try:
                pipe_res = run_pipeline_once(
                    _voiced_wav(),
                    cfg=cfg_pre,
                    tracker=tracker,
                    asr_provider=_SlowInspectAsr(),
                    refiner=ref_pre,
                    window_title_override="[agy:#fedcba98]",
                    dry_run=True,
                    env={"HOME": str(home_p)},
                )
                self.assertTrue(pipe_res.ok)
                self.assertTrue(saw_tcp_prewarm_during_asr[0])
                self.assertEqual(pipe_res.final_text, "CLEAN#1:并行预热语音草稿")
                # Only 1 TCP connection total (reused from prepare_session prewarm)!
                self.assertEqual(srv_pre.accepted_conns, 1)
                self.assertLessEqual(pipe_res.refined.init_ms if pipe_res.refined else 999, 15)
            finally:
                ref_pre.close()
                srv_pre.close()
                os.close(agy_lock_fd)

    def test_pipeline_forwards_ui_language_to_builtin_hotword_filter(self) -> None:
        from agent_voice_input.config import AsrConfig, UiConfig

        seen_hotwords: list[list[str]] = []

        class _CaptureAsr:
            provider_name = "dashscope"

            def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None, **kw) -> AsrResult:
                seen_hotwords.append(list(context.hotwords) if context is not None else [])
                return AsrResult(text="hello", provider="dashscope", model="qwen3-asr-flash", latency_ms=5)

        class _PassRefiner:
            def refine(self, asr_res: Any, **kw) -> RefinerResult:
                txt = asr_res.text if isinstance(asr_res, AsrResult) else str(asr_res)
                return RefinerResult(text=txt, provider="none", model="none", latency_ms=1)

            def close(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            hw_file = root / "hotwords.txt"
            hw_file.write_text("自定义术语\n", encoding="utf-8")
            env_en = {"HOME": str(root / "home"), "AGENT_VOICE_INPUT_RUNTIME_DIR": str(root / "rt"), "LANG": "en_US.UTF-8"}
            env_zh = {**env_en, "LANG": "zh_CN.UTF-8"}
            voiced = _voiced_wav()

            for lang, cur_env in (("en", env_en), ("auto", env_en), ("auto", env_zh), ("zh", env_en)):
                cfg = Config(
                    asr=AsrConfig(hotwords_file=str(hw_file)),
                    ui=UiConfig(language=lang),
                )
                res = run_pipeline_once(
                    voiced,
                    cfg=cfg,
                    asr_provider=_CaptureAsr(),
                    refiner=_PassRefiner(),  # type: ignore[arg-type]
                    origin_window=ForegroundWindow(1, "WindowsTerminal.exe", "bash"),
                    mode="agent",
                    dry_run=True,
                    env=cur_env,
                )
                self.assertTrue(res.ok)

        hw_en, hw_auto_en, hw_auto_zh, hw_zh = seen_hotwords
        for hw_non_cjk in (hw_en, hw_auto_en):
            self.assertIn("自定义术语", hw_non_cjk)
            self.assertIn("Codex", hw_non_cjk)
            self.assertIn("Claude Code", hw_non_cjk)
            self.assertNotIn("豆包", hw_non_cjk)
        for hw_cjk in (hw_auto_zh, hw_zh):
            self.assertIn("自定义术语", hw_cjk)
            self.assertIn("Codex", hw_cjk)
            self.assertIn("Claude Code", hw_cjk)
            self.assertIn("豆包", hw_cjk)


class ObservabilityAndTelemetryTests(unittest.TestCase):
    def test_refiner_token_usage_extraction_and_none_vs_zero_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _write_codex_auth_json(tmp)

            def usage_responder(handler, rec) -> None:
                n = int(rec["seq"])
                if n == 1:
                    # Omitted input_tokens_details / output_tokens_details -> normalized to 0 when input/output present
                    _LocalResponsesServer.send_sse_200(
                        handler,
                        _sse_completed_frames(["OUT-1"], usage={"input_tokens": 4200, "output_tokens": 28}),
                    )
                elif n == 2:
                    _LocalResponsesServer.send_sse_200(
                        handler,
                        _sse_completed_frames(
                            [],
                            output_only="OUT-2",
                            usage={
                                "input_tokens": 4210,
                                "input_tokens_details": {"cached_tokens": 4096},
                                "output_tokens": 31,
                                "output_tokens_details": {"reasoning_tokens": 12},
                            },
                        ),
                    )
                else:
                    _LocalResponsesServer.send_sse_200(handler, _sse_completed_frames(["OUT-3"], usage=None))

            srv_u = _LocalResponsesServer(responder=usage_responder)
            cfg_codex = Config(refiner=RefinerConfig(provider="codex", base_url=srv_u.base_url, timeout_sec=5.0))
            ref_codex = CodexRefiner(cfg_codex, env={"HOME": str(tmp)}, runtime_dir=tmp / "rt_codex")
            try:
                c1 = ref_codex.refine("第一轮冷启动", session_key="agy:u1", fallback_context="ctx_alpha")
                self.assertEqual(c1.text, "OUT-1")
                self.assertEqual((c1.input_tokens, c1.cached_input_tokens, c1.output_tokens, c1.reasoning_tokens), (4200, 0, 28, 0))
                self.assertIsNotNone(c1.cached_input_tokens)
                self.assertGreaterEqual(c1.init_ms, 0)
                self.assertEqual(c1.context_chunks, 1)
                self.assertGreater(c1.context_chars, 0)

                c2 = ref_codex.refine("第二轮热命中", session_key="agy:u1", fallback_context="ctx_alpha")
                self.assertEqual(c2.text, "OUT-2")
                self.assertEqual((c2.input_tokens, c2.cached_input_tokens, c2.output_tokens, c2.reasoning_tokens), (4210, 4096, 31, 12))

                c3 = ref_codex.refine("第三轮无通知上报", session_key="agy:u1", fallback_context="ctx_alpha")
                self.assertEqual(c3.text, "OUT-3")
                self.assertEqual((c3.input_tokens, c3.cached_input_tokens, c3.output_tokens, c3.reasoning_tokens), (None, None, None, None))
            finally:
                ref_codex.close()
                srv_u.close()

            # OpenAICompatibleRefiner: OpenAI details (0 vs hit), DeepSeek cache hit, and omitted details -> None
            oai_payloads = [
                {
                    "choices": [{"message": {"content": "oai-miss"}}],
                    "usage": {
                        "prompt_tokens": 2048,
                        "completion_tokens": 19,
                        "prompt_tokens_details": {"cached_tokens": 0},
                        "completion_tokens_details": {"reasoning_tokens": 0},
                    },
                },
                {
                    "choices": [{"message": {"content": "deepseek-hit"}}],
                    "usage": {
                        "prompt_cache_hit_tokens": 1920,
                        "prompt_cache_miss_tokens": 140,
                        "completion_tokens": 22,
                        "completion_tokens_details": {"reasoning_tokens": 64},
                    },
                },
                {
                    "choices": [{"message": {"content": "bare-gateway"}}],
                    "usage": {"prompt_tokens": 500, "completion_tokens": 15},
                },
            ]
            oai_idx = [0]

            def oai_tx(_req: HttpRequest) -> HttpResponse:
                body = oai_payloads[oai_idx[0]]
                oai_idx[0] += 1
                return HttpResponse(200, {"content-type": "application/json"}, json.dumps(body).encode("utf-8"))

            cfg_oai = Config(
                refiner=RefinerConfig(
                    provider="openai-compatible",
                    base_url="http://127.0.0.1:8000/v1",
                    model="local-ds",
                )
            )
            ref_oai = OpenAICompatibleRefiner(cfg_oai, env={}, transport=oai_tx)
            o1 = ref_oai.refine("测试一", fallback_context="ctx_block")
            self.assertEqual((o1.input_tokens, o1.cached_input_tokens, o1.output_tokens, o1.reasoning_tokens), (2048, 0, 19, 0))
            o2 = ref_oai.refine("测试二", fallback_context="ctx_block")
            self.assertEqual((o2.input_tokens, o2.cached_input_tokens, o2.output_tokens, o2.reasoning_tokens), (2060, 1920, 22, 64))
            o3 = ref_oai.refine("测试三")
            self.assertEqual((o3.input_tokens, o3.cached_input_tokens, o3.output_tokens, o3.reasoning_tokens), (500, None, 15, None))
            self.assertEqual((o3.context_chunks, o3.context_chars), (0, 0))

            # GeminiRefiner: omitted cachedContentTokenCount -> 0 when promptTokenCount exists; absent usageMetadata -> None
            gem_payloads = [
                {
                    "candidates": [{"content": {"parts": [{"text": "gem-0"}]}}],
                    "usageMetadata": {"promptTokenCount": 3100, "candidatesTokenCount": 25},
                },
                {
                    "candidates": [{"content": {"parts": [{"text": "gem-hit"}]}}],
                    "usageMetadata": {
                        "promptTokenCount": 3120,
                        "cachedContentTokenCount": 2980,
                        "candidatesTokenCount": 27,
                        "thoughtsTokenCount": 48,
                    },
                },
                {"candidates": [{"content": {"parts": [{"text": "gem-none"}]}}]},
            ]
            gem_idx = [0]
            gem_env = {"GEMINI_API_KEY": "gem_usage_" + ("a" * 16)}
            cfg_gem = Config(refiner=RefinerConfig(provider="gemini"))
            ref_gem = GeminiRefiner(
                cfg_gem,
                resolve_credentials(cfg_gem, gem_env),
                env=gem_env,
                transport=lambda _r: (
                    gem_idx.__setitem__(0, gem_idx[0] + 1),
                    HttpResponse(200, {"content-type": "application/json"}, json.dumps(gem_payloads[gem_idx[0] - 1]).encode("utf-8")),
                )[1],
            )
            g1 = ref_gem.refine("g1")
            self.assertEqual((g1.input_tokens, g1.cached_input_tokens, g1.output_tokens, g1.reasoning_tokens), (3100, 0, 25, None))
            g2 = ref_gem.refine("g2")
            self.assertEqual((g2.input_tokens, g2.cached_input_tokens, g2.output_tokens, g2.reasoning_tokens), (3120, 2980, 27, 48))
            g3 = ref_gem.refine("g3")
            self.assertEqual((g3.input_tokens, g3.cached_input_tokens, g3.output_tokens, g3.reasoning_tokens), (None, None, None, None))

    def test_daemon_telemetry_jsonl_permissions_256kb_atomic_trim_and_zero_plaintext(self) -> None:
        import stat
        from agent_voice_input.daemon import TELEMETRY_MAX_BYTES, append_telemetry_record, read_telemetry_history
        from agent_voice_input.refiner import BaseRefiner

        secret_speech = "PRIVATE_USER_SPEECH_UTTERANCE_8821"
        secret_refined = "PRIVATE_REFINED_RESULT_TEXT_7732"
        secret_dialog = "PRIVATE_SESSION_CONTEXT_BODY_6643"
        secret_api_key = "sk-private-secret-" + ("z" * 18)

        class _InstrumentedRefiner(BaseRefiner):
            provider_name = "codex"

            def refine(self, asr_input, *, session_key="", tracker=None, fallback_context="", is_cancelled=None, **_kw: object):
                return RefinerResult(
                    text=secret_refined,
                    provider="codex",
                    model="gpt-6-luna",
                    latency_ms=85,
                    warning=f"minor warn key={secret_api_key}",
                    input_tokens=8192,
                    cached_input_tokens=7680,
                    output_tokens=36,
                    reasoning_tokens=0,
                    init_ms=4,
                    context_chars=12345,
                    context_chunks=2,
                )

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            rt = tmp / "rt"
            sid = "99887766-0000-4000-8000-000000000099"
            sdir = tmp / "home" / ".codex" / "sessions" / "2026" / "11" / "17"
            sdir.mkdir(parents=True)
            rollout = sdir / f"rollout-2026-{sid}.jsonl"
            rollout.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": sid, "source": "cli", "cwd": ""}})
                + "\n"
                + json.dumps({"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "UserMessage", "content": [{"type": "text", "text": secret_dialog}]}}})
                + "\n",
                encoding="utf-8",
            )
            rt.mkdir(parents=True, mode=0o700)
            write_session_registry_entry(rt, cli="codex", session_id=sid, jsonl_path=str(rollout), cwd="")
            env = {"OPENAI_API_KEY": secret_api_key, "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)}
            cfg = Config(refiner=RefinerConfig(provider="codex"))
            plat = _FakePlatform()
            plat.fg = ForegroundWindow(hwnd=101, process="WindowsTerminal", title=f"codex [#{sid[:8]}]")
            tracker = SessionTracker(home_dir=tmp / "home", env=env, runtime_dir=rt, proc_root=tmp / "no_proc")
            daemon = VoiceInputDaemon(
                cfg,
                env=env,
                runtime_dir=rt,
                platform=plat,
                asr_provider=_FakeAsr(secret_speech),
                refiner=_InstrumentedRefiner(),
                tracker=tracker,
                paste_settle_sec=0.0,
            )
            try:
                for i in range(25):
                    daemon._last_trigger_ts = -1e9
                    daemon.handle_trigger("toggle")
                    daemon._last_trigger_ts = -1e9
                    r_stop = daemon.handle_trigger("toggle")
                    deadline = time.monotonic() + 2.0
                    while time.monotonic() < deadline and daemon.last_result.get("seq_id") != r_stop["seq_id"]:
                        time.sleep(0.01)

                self.assertEqual(len(daemon.recent_results), 20)
                self.assertEqual(daemon.recent_results[0]["seq_id"], 6)
                self.assertEqual(daemon.recent_results[-1]["seq_id"], 25)

                lr = daemon.last_result
                for required_num_key in (
                    "asr_ms",
                    "refine_init_ms",
                    "refine_ms",
                    "deliver_ms",
                    "total_ms",
                    "ctx_chars",
                    "ctx_chunks",
                    "input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                    "reasoning_tokens",
                ):
                    self.assertIsInstance(lr[required_num_key], int, required_num_key)
                self.assertEqual(lr["asr_provider"], "dashscope")
                self.assertEqual(lr["refiner_provider"], "codex")
                self.assertEqual((lr["ctx_chars"], lr["ctx_chunks"]), (12345, 2))
                self.assertEqual((lr["input_tokens"], lr["cached_input_tokens"], lr["output_tokens"], lr["reasoning_tokens"]), (8192, 7680, 36, 0))

                tpath = rt / "telemetry.jsonl"
                self.assertTrue(tpath.is_file())
                self.assertEqual(stat.S_IMODE(tpath.stat().st_mode), 0o600)
                raw_telemetry = tpath.read_text(encoding="utf-8")
                combined_dump = json.dumps(
                    {"last": lr, "recent": list(daemon.recent_results)},
                    ensure_ascii=False,
                ) + "\n" + raw_telemetry
                for forbidden in (secret_speech, secret_refined, secret_dialog, secret_api_key):
                    self.assertNotIn(forbidden, combined_dump)
                self.assertIn("[REDACTED]", combined_dump)
            finally:
                daemon.close()

            # Push telemetry.jsonl well past 256 KB and verify atomic 0600 tail truncation on whole lines
            rt_trim = tmp / "rt_trim"
            rt_trim.mkdir(mode=0o700)
            for seq in range(1, 900):
                append_telemetry_record(
                    rt_trim,
                    {
                        "ts": 1700000000.0 + seq * 0.01,
                        "seq_id": seq,
                        "ok": True,
                        "asr_provider": "dashscope",
                        "refiner_provider": "codex",
                        "asr_ms": 300,
                        "refine_init_ms": 0,
                        "refine_ms": 400,
                        "deliver_ms": 20,
                        "total_ms": 720,
                        "ctx_chars": 150000,
                        "ctx_chunks": 3,
                        "input_tokens": 42000,
                        "cached_input_tokens": 41500,
                        "output_tokens": 40,
                        "reasoning_tokens": 0,
                        "pad": "x" * 180,
                    },
                )
            trim_file = rt_trim / "telemetry.jsonl"
            st_trim = trim_file.stat()
            self.assertLessEqual(st_trim.st_size, TELEMETRY_MAX_BYTES)
            self.assertGreater(st_trim.st_size, 90 * 1024)
            self.assertEqual(stat.S_IMODE(st_trim.st_mode), 0o600)
            self.assertEqual([p.name for p in rt_trim.iterdir() if ".tmp" in p.name], [])

            raw_lines = trim_file.read_bytes().splitlines()
            self.assertGreater(len(raw_lines), 100)
            parsed_trimmed = [json.loads(ln.decode("utf-8")) for ln in raw_lines]
            self.assertEqual(parsed_trimmed[-1]["seq_id"], 899)
            self.assertGreater(parsed_trimmed[0]["seq_id"], 1)
            self.assertEqual(
                [r["seq_id"] for r in read_telemetry_history(rt_trim, limit=3)],
                [897, 898, 899],
            )

    def test_cli_status_history_backward_compat_online_merge_and_offline_fallback(self) -> None:
        import io
        from agent_voice_input import cli as cli_mod
        from agent_voice_input.daemon import append_telemetry_record

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            rt = tmp / "rt_cli"
            rt.mkdir(mode=0o700)
            cfg_file = tmp / "cfg.toml"
            cfg_file.write_text('[refiner]\nprovider = "off"\n', encoding="utf-8")

            # Seed 2 historical records from a previous daemon lifecycle in telemetry.jsonl
            for old_seq in (101, 102):
                append_telemetry_record(
                    rt,
                    {"ts": 1700000100.0 + old_seq, "seq_id": old_seq, "ok": True, "total_ms": 500 + old_seq},
                )

            env_patch = {
                "HOME": str(tmp),
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
                "AGENT_VOICE_INPUT_CONFIG": str(cfg_file),
            }
            plat = _FakePlatform()
            cfg = Config(refiner=RefinerConfig(provider="off"))
            daemon = VoiceInputDaemon(
                cfg,
                env=env_patch,
                runtime_dir=rt,
                platform=plat,
                asr_provider=_FakeAsr("新守护进程口述"),
                paste_settle_sec=0.0,
            )
            srv_thread = threading.Thread(target=daemon.serve_forever, daemon=True)
            srv_thread.start()
            try:
                _wait_for_control_socket(rt, timeout_sec=3.0)

                # Run 2 live utterances on the current daemon (seq 1, 2)
                for _ in range(2):
                    daemon._last_trigger_ts = -1e9
                    daemon.handle_trigger("toggle")
                    daemon._last_trigger_ts = -1e9
                    rs = daemon.handle_trigger("toggle")
                    w_dl = time.monotonic() + 2.0
                    while time.monotonic() < w_dl and daemon.last_result.get("seq_id") != rs["seq_id"]:
                        time.sleep(0.01)

                with unittest.mock.patch.dict(os.environ, env_patch, clear=True):
                    # 1. Default `status` without --history omits recent_results (strictly backward-compatible)
                    buf_plain = io.StringIO()
                    with unittest.mock.patch("sys.stdout", buf_plain):
                        rc_plain = cli_mod.main(["--config", str(cfg_file), "status"])
                    self.assertEqual(rc_plain, 0)
                    obj_plain = json.loads(buf_plain.getvalue())
                    self.assertTrue(obj_plain["ok"])
                    self.assertEqual(obj_plain["last_result"]["seq_id"], 2)
                    self.assertNotIn("recent_results", obj_plain)

                    # 2. `status --history` (default const=10) merges prior telemetry.jsonl (101, 102) + live (1, 2) without duplicates
                    buf_hist = io.StringIO()
                    with unittest.mock.patch("sys.stdout", buf_hist):
                        rc_hist = cli_mod.main(["--config", str(cfg_file), "status", "--history"])
                    self.assertEqual(rc_hist, 0)
                    obj_hist = json.loads(buf_hist.getvalue())
                    self.assertEqual([r["seq_id"] for r in obj_hist["recent_results"]], [101, 102, 1, 2])

                    # 3. `status --history 3` caps at last 3
                    buf_h3 = io.StringIO()
                    with unittest.mock.patch("sys.stdout", buf_h3):
                        rc_h3 = cli_mod.main(["--config", str(cfg_file), "status", "--history", "3"])
                    self.assertEqual(rc_h3, 0)
                    obj_h3 = json.loads(buf_h3.getvalue())
                    self.assertEqual([r["seq_id"] for r in obj_h3["recent_results"]], [102, 1, 2])
            finally:
                daemon.close()
                srv_thread.join(timeout=2.0)

            # 4. Offline daemon fallback: plain `status` stays compact; `status --history 2` reads telemetry.jsonl
            with unittest.mock.patch.dict(os.environ, env_patch, clear=True):
                buf_off_plain = io.StringIO()
                with unittest.mock.patch("sys.stdout", buf_off_plain):
                    rc_off_plain = cli_mod.main(["--config", str(cfg_file), "status"])
                self.assertEqual(rc_off_plain, 1)
                self.assertEqual(json.loads(buf_off_plain.getvalue()), {"ok": False, "state": "stopped"})

                buf_off_hist = io.StringIO()
                with unittest.mock.patch("sys.stdout", buf_off_hist):
                    rc_off_hist = cli_mod.main(["--config", str(cfg_file), "status", "--history", "2"])
                self.assertEqual(rc_off_hist, 1)
                obj_off_hist = json.loads(buf_off_hist.getvalue())
                self.assertFalse(obj_off_hist["ok"])
                self.assertEqual(obj_off_hist["state"], "stopped")
                self.assertEqual([r["seq_id"] for r in obj_off_hist["recent_results"]], [1, 2])
                self.assertEqual(obj_off_hist["last_result"]["seq_id"], 2)

    def test_diagnose_live_works_without_logs_sqlite(self) -> None:
        import importlib.util
        import io
        from agent_voice_input.daemon import append_telemetry_record

        diag_script = _REPO_ROOT / ".agents" / "skills" / "voice-eval" / "scripts" / "diagnose_live.py"
        if not diag_script.is_file() and not (_REPO_ROOT / ".agents").exists():
            self.skipTest("repo-only .agents/skills/voice-eval is pruned from sdist")
        self.assertTrue(diag_script.is_file())
        spec = importlib.util.spec_from_file_location("avi_diagnose_live_test", diag_script)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            rt = tmp / "rt_no_sqlite"
            rt.mkdir(mode=0o700)
            cfg_file = tmp / "cfg.toml"
            cfg_file.write_text('[refiner]\nprovider = "codex"\n', encoding="utf-8")
            append_telemetry_record(
                rt,
                {
                    "ts": 1700000500.0,
                    "seq_id": 7,
                    "ok": True,
                    "session_key": "agy:secret-session-uuid",
                    "asr_provider": "dashscope",
                    "refiner_provider": "codex",
                    "asr_ms": 320,
                    "refine_init_ms": 0,
                    "refine_ms": 410,
                    "deliver_ms": 25,
                    "total_ms": 755,
                    "ctx_chars": 98000,
                    "ctx_chunks": 2,
                    "input_tokens": 28000,
                    "cached_input_tokens": 27500,
                    "output_tokens": 30,
                    "reasoning_tokens": 0,
                },
            )
            self.assertFalse((rt / "codex-home" / "logs_2.sqlite").exists())
            buf = io.StringIO()
            with (
                unittest.mock.patch.dict(os.environ, {"HOME": str(tmp), "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)}, clear=True),
                unittest.mock.patch("sys.stdout", buf),
            ):
                rc = mod.main(["--config", str(cfg_file), "--runtime-dir", str(rt), "--last", "5", "--json"])
            self.assertEqual(rc, 0)
            out_json = json.loads(buf.getvalue())
            self.assertIsNone(out_json["storage"]["legacy_codex_sqlite"])
            self.assertEqual(len(out_json["recent_results"]), 1)
            self.assertNotIn("secret-session-uuid", buf.getvalue())
            self.assertTrue(out_json["recent_results"][0]["session_tag"].startswith("agy#"))


class Step3ReviewFixesAndRecordingArchiveTests(unittest.TestCase):
    def test_p1_codex_unchanged_proactive_cooldown_and_401_same_token_no_resend(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            srv = _LocalResponsesServer()
            try:
                # 1. Proactive refresh (<1800s) where codex app-server returns the SAME unrotated token
                #    (even touching auth.json) MUST arm 300s cooldown so prepare_session() + refine()
                #    do NOT spawn 2 app-servers per utterance!
                home_unrot = tmp / "home_unrot"
                t_epoch = [1_750_020_000.0]
                t_mono = [1000.0]
                tok_1500s = _make_fake_jwt(exp=int(t_epoch[0]) + 1500, account_id="acct-unrot")
                auth_unrot = _write_codex_auth_json(home_unrot, access_token=tok_1500s, account_id="acct-unrot")
                rpc_log = tmp / "rpc_unrot.jsonl"

                ref = CodexRefiner(
                    Config(refiner=RefinerConfig(provider="codex", base_url=srv.base_url, timeout_sec=5.0)),
                    env={"HOME": str(home_unrot)},
                    app_server_cmd=[
                        sys.executable,
                        "-u",
                        "-c",
                        _MOCK_AUTH_APP_SERVER_CODE,
                        str(rpc_log),
                        "ok",
                        tok_1500s,
                        str(auth_unrot),
                    ],
                    time_fn=lambda: t_epoch[0],
                    clock_fn=lambda: t_mono[0],
                )
                try:
                    ref.prepare_session("codex:s1")
                    deadline_pw = time.monotonic() + 2.0
                    while time.monotonic() < deadline_pw and not rpc_log.is_file():
                        time.sleep(0.01)
                    # Wait for prewarm thread to settle
                    time.sleep(0.05)
                    r1 = ref.refine("第一句口述")
                    self.assertEqual(r1.used_fallback, "")
                    t_epoch[0] += 60.0
                    t_mono[0] += 60.0
                    r2 = ref.refine("冷却期内第二句口述")
                    self.assertEqual(r2.used_fallback, "")
                    rpc_calls = [
                        json.loads(ln)
                        for ln in rpc_log.read_text(encoding="utf-8").splitlines()
                        if "getAuthStatus" in ln
                    ]
                    self.assertEqual(len(rpc_calls), 1)
                finally:
                    ref.close()

                # 2. HTTP 401 where refresh returns the SAME dead token MUST fail fast to asr_draft
                #    after 1 HTTP request without resending the same dead token!
                srv_401 = _LocalResponsesServer(
                    responder=lambda h, _r: _LocalResponsesServer.send_json(h, 401, {"detail": "revoked"})
                )
                try:
                    home_dead = tmp / "home_dead"
                    tok_dead = _make_fake_jwt(exp=4102444800, account_id="acct-dead")
                    auth_dead = _write_codex_auth_json(home_dead, access_token=tok_dead, account_id="acct-dead")
                    rpc_dead = tmp / "rpc_dead.jsonl"
                    ref_dead = CodexRefiner(
                        Config(refiner=RefinerConfig(provider="codex", base_url=srv_401.base_url, max_retries=2)),
                        env={"HOME": str(home_dead)},
                        app_server_cmd=[
                            sys.executable,
                            "-u",
                            "-c",
                            _MOCK_AUTH_APP_SERVER_CODE,
                            str(rpc_dead),
                            "ok",
                            tok_dead,
                            str(auth_dead),
                        ],
                    )
                    try:
                        r_dead = ref_dead.refine("死票不重发")
                        self.assertEqual((r_dead.text, r_dead.used_fallback), ("死票不重发", "asr_draft"))
                        self.assertEqual(len(srv_401.requests), 1)
                    finally:
                        ref_dead.close()
                finally:
                    srv_401.close()
            finally:
                srv.close()

    def test_p1_responses_non_codex_reasoning_effort_model_gating(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            srv = _LocalResponsesServer()
            try:
                env_oai = {"HOME": str(tmp), "OPENAI_API_KEY": "sk-oai-" + ("8" * 16)}
                # 1. Non-reasoning OpenAI models on api="responses" omit `reasoning` field completely
                for non_rsn_model in ("gpt-4o-mini", "gpt-4o", "gpt-4.1"):
                    srv.requests.clear()
                    cfg_nr = Config(
                        refiner=RefinerConfig(
                            provider="openai",
                            api="responses",
                            model=non_rsn_model,
                            effort="low",
                            base_url=f"http://127.0.0.1:{srv.port}/v1",
                        )
                    )
                    ref_nr = create_refiner(cfg_nr, resolve_credentials(cfg_nr, env_oai), env=env_oai)
                    try:
                        res_nr = ref_nr.refine("非推理模型省略 reasoning")
                        self.assertEqual(res_nr.used_fallback, "")
                        sent_body = srv.requests[-1]["body"]
                        assert isinstance(sent_body, dict)
                        self.assertNotIn("reasoning", sent_body, non_rsn_model)
                    finally:
                        ref_nr.close()

                # 2. Reasoning OpenAI model maps effort="max" -> "xhigh"
                srv.requests.clear()
                cfg_rsn = Config(
                    refiner=RefinerConfig(
                        provider="openai",
                        api="responses",
                        model="gpt-5-mini",
                        effort="max",
                        base_url=f"http://127.0.0.1:{srv.port}/v1",
                    )
                )
                ref_rsn = create_refiner(cfg_rsn, resolve_credentials(cfg_rsn, env_oai), env=env_oai)
                try:
                    ref_rsn.refine("推理模型 max 映射 xhigh")
                    body_rsn = srv.requests[-1]["body"]
                    assert isinstance(body_rsn, dict)
                    self.assertEqual(body_rsn.get("reasoning"), {"effort": "xhigh"})
                finally:
                    ref_rsn.close()

                # 3. CodexRefiner in api_key mode: omits reasoning on gpt-4o-mini, maps max->xhigh on gpt-6-luna
                home_ak = tmp / "home_ak"
                _write_codex_auth_json(home_ak, auth_mode="api_key", openai_api_key="sk-ak-codex-777777")
                for c_model, c_eff, expected_rsn in (
                    ("gpt-4o-mini", "low", None),
                    ("gpt-6-luna", "max", {"effort": "xhigh"}),
                ):
                    srv.requests.clear()
                    ref_cak = CodexRefiner(
                        Config(
                            refiner=RefinerConfig(
                                provider="codex",
                                model=c_model,
                                effort=c_eff,
                                base_url=f"http://127.0.0.1:{srv.port}/v1",
                            )
                        ),
                        env={"HOME": str(home_ak)},
                    )
                    try:
                        ref_cak.refine("Codex API Key 模式门控")
                        b_cak = srv.requests[-1]["body"]
                        assert isinstance(b_cak, dict)
                        self.assertEqual(b_cak.get("reasoning"), expected_rsn)
                    finally:
                        ref_cak.close()
            finally:
                srv.close()

    def test_p2_truncated_chunked_sse_retries_and_atomic_conn_release_on_close_or_switch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _write_codex_auth_json(tmp, account_id="acct-sse-1")

            # 1. Truncated chunked stream after response.output_text.done WITHOUT response.completed
            #    MUST NOT accept partial text—must retry and recover on full response.completed!
            def truncated_then_ok(handler, rec) -> None:
                if int(rec["seq"]) == 1:
                    handler.send_response(200)
                    handler.send_header("Transfer-Encoding", "chunked")
                    handler.send_header("Connection", "keep-alive")
                    handler.end_headers()
                    premature_frames = [
                        'data: {"type":"response.output_text.delta","delta":"半截错误残片"}\n\n',
                        'data: {"type":"response.output_text.done","text":"半截错误残片"}\n\n',
                    ]
                    for fr in premature_frames:
                        b = fr.encode("utf-8")
                        handler.wfile.write(f"{len(b):x}\r\n".encode("ascii") + b + b"\r\n")
                    handler.wfile.flush()
                    # Drop TCP socket mid-chunked WITHOUT sending 0\r\n\r\n or response.completed!
                    handler.close_connection = True
                    try:
                        handler.connection.shutdown(2)
                    except Exception:
                        pass
                    handler.connection.close()
                    return
                _LocalResponsesServer.send_sse_200(handler, _sse_completed_frames(["完整精修结果"]))

            srv_trunc = _LocalResponsesServer(responder=truncated_then_ok)
            try:
                ref_t = CodexRefiner(
                    Config(refiner=RefinerConfig(provider="codex", base_url=srv_trunc.base_url, max_retries=2)),
                    env={"HOME": str(tmp)},
                    sleep_fn=lambda _s: None,
                    rand_fn=lambda: 0.0,
                )
                try:
                    res_t = ref_t.refine("测试半截断流")
                    self.assertEqual((res_t.text, res_t.used_fallback, res_t.retries), ("完整精修结果", "", 1))
                finally:
                    ref_t.close()

                # Also verify compatible gateway `data: [DONE]` marker works cleanly
                from agent_voice_input.refiner import _parse_responses_sse_lines

                done_lines = iter(
                    [
                        'data: {"type":"response.output_text.delta","delta":"兼容网关OK"}\n'.encode("utf-8"),
                        b"\n",
                        b"data: [DONE]\n",
                        b"\n",
                    ]
                )
                txt_d, _, _ = _parse_responses_sse_lines(
                    lambda: next(done_lines, b""),
                    provider_name="openai-compatible",
                    resp_headers={},
                )
                self.assertEqual(txt_d, "兼容网关OK")
            finally:
                srv_trunc.close()

            # 2. Atomic _finish_conn guard: switching account or closing mid-flight never leaks conn back into pool
            in_flight_evt = threading.Event()
            proceed_evt = threading.Event()

            def slow_responder(handler, _rec) -> None:
                in_flight_evt.set()
                proceed_evt.wait(timeout=2.0)
                _LocalResponsesServer.send_sse_200(handler, _sse_completed_frames(["并发切号完成"]))

            srv_race = _LocalResponsesServer(responder=slow_responder)
            try:
                ref_race = CodexRefiner(
                    Config(refiner=RefinerConfig(provider="codex", base_url=srv_race.base_url, timeout_sec=5.0)),
                    env={"HOME": str(tmp)},
                )
                try:
                    out_holder: list[RefinerResult] = []
                    th = threading.Thread(target=lambda: out_holder.append(ref_race.refine("并发请求")))
                    th.start()
                    self.assertTrue(in_flight_evt.wait(timeout=2.0))
                    # Simulate concurrent account identity switch while connection is active
                    from agent_voice_input.refiner import _ResponsesAuthTarget

                    ref_race._sync_auth_identity(
                        _ResponsesAuthTarget(
                            url=f"{srv_race.base_url}/responses",
                            bearer_token="tok-new",
                            identity_key="http://127.0.0.1|acct:switched-mid-flight",
                            is_codex_subscription=True,
                        )
                    )
                    proceed_evt.set()
                    th.join(timeout=2.0)
                    self.assertEqual(len(out_holder), 1)
                    with ref_race._pool._lock:
                        total_idle = sum(len(v) for v in ref_race._pool._idle.values())
                    self.assertEqual(total_idle, 0)
                finally:
                    ref_race.close()
            finally:
                srv_race.close()

    def test_p3_redact_secrets_oauth_jwt_and_find_codex_bin_dict_env(self) -> None:
        from agent_voice_input.config import redact_secrets
        from agent_voice_input.refiner import find_codex_bin

        fake_jwt = _make_fake_jwt(exp=4102444800, account_id="acct-secret-9988")
        rt_val = "rt_" + ("1" * 12)
        id_val = "id_" + ("2" * 12)
        at_c_val = "at_" + ("3" * 12)
        at_s_val = "at_" + ("4" * 12)
        acct_val = "acct_" + ("5" * 10)
        rt_q_val = "rt_" + ("6" * 10)
        acct_q_val = "acct_" + ("7" * 10)
        kv_val = "kv_" + ("8" * 10)
        sample = (
            f'json={{"access_token": "{fake_jwt}", "refresh_token": "{rt_val}", '
            f'"id_token": "{id_val}", "authToken": "{at_c_val}", "auth_token": "{at_s_val}", '
            f'"account_id": "{acct_val}"}} '
            f"bare_jwt={fake_jwt} query=https://example.com/cb?refresh_token={rt_q_val}&account_id={acct_q_val} "
            f"kv_line=authToken={kv_val}"
        )
        redacted = redact_secrets(sample, env={})
        for secret_piece in (
            fake_jwt,
            rt_val,
            id_val,
            at_c_val,
            at_s_val,
            acct_val,
            rt_q_val,
            acct_q_val,
            kv_val,
        ):
            self.assertNotIn(secret_piece, redacted)
        self.assertNotIn("eyJ", redacted)

        # find_codex_bin: dict(os.environ) with matching HOME checks ~/.local/bin/codex and system fallbacks,
        # whereas isolated HOME or missing PATH strictly stays hermetic!
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            fake_home = tmp / "fake_home"
            local_bin = fake_home / ".local" / "bin"
            local_bin.mkdir(parents=True)
            fake_codex = local_bin / "codex"
            fake_codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_codex.chmod(0o755)
            with unittest.mock.patch.dict(os.environ, {"HOME": str(fake_home), "PATH": "/nonexistent-dir"}, clear=True):
                copied_prod_env = dict(os.environ)
                self.assertEqual(find_codex_bin(copied_prod_env), str(fake_codex))
                other_home = tmp / "other_isolated_home"
                other_home.mkdir()
                self.assertIsNone(find_codex_bin({"HOME": str(other_home), "PATH": "/nonexistent-dir"}))

    def test_save_recordings_mp3_archive_permissions_ring_prune_and_ffmpeg_restore(self) -> None:
        import inspect
        import math
        import stat
        import subprocess
        from agent_voice_input.audio_proc import (
            AudioError,
            archive_recording_audio,
            ensure_16k_mono_s16_wav,
            list_saved_recordings,
            process_wav,
            prune_saved_recordings_dir,
            resolve_ffmpeg_bin,
            spawn_recording_archive,
        )
        from agent_voice_input.config import AudioConfig, resolve_saved_recordings_dir

        speech_band_wav = pack_pcm16_mono_wav(
            [int(5000 * math.sin(2.0 * math.pi * 440.0 * i / 16000.0)) for i in range(9600)]
        )
        try:
            ffmpeg_exe: str | None = resolve_ffmpeg_bin()
        except AudioError:
            ffmpeg_exe = None
        has_ffmpeg = ffmpeg_exe is not None
        if not has_ffmpeg and _is_ci():
            self.fail("ffmpeg must be installed in CI for recording archive test (silent downgrade forbidden)")
        has_mp3_encoder = False
        if has_ffmpeg:
            assert ffmpeg_exe is not None
            enc_proc = subprocess.run(
                [ffmpeg_exe, "-nostdin", "-hide_banner", "-encoders"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=10.0,
                check=False,
            )
            has_mp3_encoder = "libmp3lame" in enc_proc.stdout

        # 1. Defaults & validation
        self.assertIs(AudioConfig().save_recordings, False)
        self.assertEqual(AudioConfig().save_recordings_max_count, 300)
        for fn in (prune_saved_recordings_dir, archive_recording_audio, spawn_recording_archive):
            self.assertEqual(inspect.signature(fn).parameters["max_count"].default, 300)

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            # Hermetic path resolution under HOME and XDG_CACHE_HOME
            self.assertEqual(
                resolve_saved_recordings_dir({"HOME": str(tmp)}),
                tmp / ".cache" / "agent-voice-input" / "recordings",
            )
            self.assertEqual(
                resolve_saved_recordings_dir({"HOME": str(tmp), "XDG_CACHE_HOME": str(tmp / "xdg_c")}),
                tmp / "xdg_c" / "agent-voice-input" / "recordings",
            )

            for bad_toml in (
                "[audio]\nsave_recordings_max_count = 0\n",
                "[audio]\nsave_recordings_max_count = 5001\n",
                '[audio]\nsave_recordings = "not_a_bool"\n',
            ):
                bad_f = tmp / "bad_audio.toml"
                bad_f.write_text(bad_toml, encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load_config(config_path=bad_f, env={"HOME": str(tmp)}, home_dir=tmp)

            ok_f = tmp / "ok_audio.toml"
            ok_f.write_text("[audio]\nsave_recordings = true\nsave_recordings_max_count = 3\n", encoding="utf-8")
            cfg_rec, env_rec = load_config(config_path=ok_f, env={"HOME": str(tmp)}, home_dir=tmp)
            self.assertIs(cfg_rec.audio.save_recordings, True)
            self.assertEqual(cfg_rec.audio.save_recordings_max_count, 3)

            # 2. Archive 5 recordings with deterministic timestamps & max_count=3 -> retains newest 3 as 0600
            rec_dir = resolve_saved_recordings_dir(env_rec)
            created_names: list[str] = []
            for i in range(5):
                out_p = archive_recording_audio(
                    speech_band_wav,
                    rec_dir,
                    max_count=3,
                    now_ts=1_750_100_000.0 + i * 10.0,
                )
                assert out_p is not None
                self.assertTrue(out_p.name.startswith("rec-"))
                if has_ffmpeg:
                    if has_mp3_encoder:
                        self.assertEqual(out_p.suffix, ".mp3")
                    else:
                        self.assertIn(out_p.suffix, (".mp3", ".m4a"))
                else:
                    self.assertEqual(out_p.suffix, ".wav")
                created_names.append(out_p.name)

            self.assertEqual(stat.S_IMODE(rec_dir.stat().st_mode), 0o700)
            kept = list_saved_recordings(rec_dir)
            self.assertEqual([p.name for p in kept], list(reversed(created_names[-3:])))
            for p in kept:
                self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
                if has_ffmpeg:
                    if has_mp3_encoder:
                        # 0.6s @ 64 kbps CBR MP3 (~8 KB/s) yields ~4.8 KB (> 4000 B vs ~2.4 KB at 32 kbps)
                        self.assertEqual(p.suffix, ".mp3")
                        self.assertGreater(p.stat().st_size, 4000)
                    else:
                        self.assertIn(p.suffix, (".mp3", ".m4a"))
                        self.assertGreater(p.stat().st_size, 0)
                else:
                    self.assertEqual(p.suffix, ".wav")
                    self.assertEqual(p.read_bytes(), speech_band_wav)

            # 3. Restore archived recording -> 16kHz mono PCM WAV & verify gate passes (+ no-ffmpeg fallback check)
            if has_ffmpeg:
                restored_wav = ensure_16k_mono_s16_wav(kept[0])
                gate_restored = process_wav(restored_wav, cfg_rec.audio)
                self.assertTrue(gate_restored.passed)
                self.assertGreater(gate_restored.duration_sec, 0.4)

                fb_dir = tmp / "fallback_wav_dir"
                with unittest.mock.patch(
                    "agent_voice_input.audio_proc.resolve_ffmpeg_bin",
                    side_effect=AudioError("no ffmpeg"),
                ):
                    fb_out = archive_recording_audio(
                        speech_band_wav,
                        fb_dir,
                        max_count=3,
                        now_ts=1_750_100_100.0,
                    )
                assert fb_out is not None
                self.assertEqual(fb_out.suffix, ".wav")
                self.assertEqual(stat.S_IMODE(fb_out.stat().st_mode), 0o600)
                self.assertEqual(fb_out.read_bytes(), speech_band_wav)
            else:
                gate_restored = process_wav(kept[0].read_bytes(), cfg_rec.audio)
                self.assertTrue(gate_restored.passed)
                self.assertGreater(gate_restored.duration_sec, 0.4)

            # 4. End-to-end VoiceInputDaemon integration with isolated HOME + graceful stop() wait,
            #    while silence and retry sources do NOT archive!
            home_d = tmp / "daemon_home"
            rt_d = tmp / "daemon_rt"
            home_d.mkdir()
            rt_d.mkdir(mode=0o700)
            env_d = {"HOME": str(home_d), "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt_d)}
            cfg_d = Config(
                audio=AudioConfig(save_recordings=True, save_recordings_max_count=2),
                refiner=RefinerConfig(provider="off"),
            )
            # Silence input must NOT produce an archived recording
            res_sil = run_pipeline_once(
                _zero_wav(0.6),
                cfg=cfg_d,
                asr_provider=_FakeAsr("不会触发"),
                dry_run=True,
                env=env_d,
            )
            self.assertFalse(res_sil.ok)
            self.assertEqual(list_saved_recordings(resolve_saved_recordings_dir(env_d)), [])

            # retry audio_source must NOT duplicate archive
            run_pipeline_once(
                _voiced_wav(),
                cfg=cfg_d,
                asr_provider=_FakeAsr("retry不重复归档"),
                audio_source="retry",
                dry_run=True,
                env=env_d,
            )
            self.assertEqual(list_saved_recordings(resolve_saved_recordings_dir(env_d)), [])

            # Live daemon utterances archive asynchronously without blocking & flush cleanly on daemon.stop()
            plat_d = _FakePlatform()
            daemon_d = VoiceInputDaemon(
                cfg_d,
                env=env_d,
                runtime_dir=rt_d,
                platform=plat_d,
                asr_provider=_FakeAsr("守护进程异步归档测试"),
                paste_settle_sec=0.0,
            )
            try:
                for _ in range(3):
                    daemon_d._last_trigger_ts = -1e9
                    daemon_d.handle_trigger("toggle")
                    daemon_d._last_trigger_ts = -1e9
                    rs = daemon_d.handle_trigger("toggle")
                    dl = time.monotonic() + 2.0
                    while time.monotonic() < dl and daemon_d.last_result.get("seq_id") != rs["seq_id"]:
                        time.sleep(0.01)
            finally:
                daemon_d.stop()

            d_saved = list_saved_recordings(resolve_saved_recordings_dir(env_d))
            self.assertEqual(len(d_saved), 2)
            if has_ffmpeg:
                expected_exts = (".mp3",) if has_mp3_encoder else (".mp3", ".m4a")
                self.assertTrue(
                    all(p.suffix in expected_exts and stat.S_IMODE(p.stat().st_mode) == 0o600 for p in d_saved)
                )
            else:
                self.assertTrue(all(p.suffix == ".wav" and stat.S_IMODE(p.stat().st_mode) == 0o600 for p in d_saved))

    def test_wsl_wasapi_startup_no_frames_and_reopen_contracts(self) -> None:
        from agent_voice_input.config import AudioConfig, HotkeyConfig, UiConfig
        from agent_voice_input.daemon import _window_from_mapping
        from agent_voice_input.platform_wsl import WslPlatform

        # 1. Error classification parity: exclusive lock / busy vs. startup 0-frame stream vs. inactive endpoint
        err_in_use = WslPlatform._classify_wasapi_start_error("mic_busy:0x8889000A")
        self.assertIsInstance(err_in_use, MicrophoneUnavailableError)
        self.assertEqual(err_in_use.reason_code, "mic_unavailable")
        self.assertEqual(err_in_use.hud_state, "mic_unavailable")

        err_no_stream = WslPlatform._classify_wasapi_start_error("mic_busy:wasapi_startup_no_frames")
        self.assertIsInstance(err_no_stream, MicrophoneUnavailableError)
        self.assertEqual(err_no_stream.reason_code, "mic_no_stream")
        self.assertEqual(err_no_stream.hud_state, "mic_no_stream")

        err_inact = WslPlatform._classify_wasapi_start_error("no_mic:wasapi_endpoint_inactive")
        self.assertIsInstance(err_inact, NoMicrophoneError)
        self.assertEqual(err_inact.reason_code, "no_mic")
        self.assertEqual(err_inact.hud_state, "no_mic")

        # 2. Core Win32/WASAPI startup & reopen gate invariants in win_shim/*.cs
        shim = read_win_shim_source()
        start_rec_block = shim.split("public static bool StartRecording(bool loopback, long recGen, out string error)", 1)[1].split(
            "public static byte[] StopRecording(out string error)", 1
        )[0]
        self.assertLess(
            start_rec_block.index('"mic_busy:wasapi_startup_no_frames"'),
            start_rec_block.index("sess.InitOk = true;"),
        )
        reopen_block = start_rec_block.split("if (reopened) {", 1)[1]
        self.assertLess(
            reopen_block.index("WasapiReopenVerifyTimeoutMs"),
            reopen_block.index('EnqueueAudioEvent("rec_recovered"'),
        )
        self.assertIn("PeekMessage(out pm, IntPtr.Zero, 0, 0, 0);", start_rec_block)
        self.assertIn("HasPendingStopOrQuitCmd()", start_rec_block)

        # 3. End-to-end WSL stdio JSONL shim:
        #    (a) PTT <850ms release on charging-case 0-frame startup shows ONLY single mic_unavailable toast;
        #    (b) PTT <850ms release when first frame succeeds closes cleanly via queued ptt_stop;
        #    (c) Direct concurrent cancel_recording() both AFTER and BEFORE rec_start stdin write preserves order & stops device.
        mock_shim_code = (
            "import json, sys, threading\n"
            "lock = threading.Lock()\n"
            "def emit(obj):\n"
            "    with lock:\n"
            "        sys.stdout.write(json.dumps(obj) + '\\n')\n"
            "        sys.stdout.flush()\n"
            "emit({'event': 'ready', 'ui_culture': 'zh-CN'})\n"
            "cmds = []\n"
            "rec_cmds = []\n"
            "hold_evt = threading.Event()\n"
            "device_recording = [False]\n"
            "scenario = ['fail_0frames_ptt']\n"
            "for raw in iter(sys.stdin.readline, ''):\n"
            "    if not raw.strip():\n"
            "        continue\n"
            "    msg = json.loads(raw)\n"
            "    op = msg.get('cmd')\n"
            "    seq = int(msg.get('seq_id') or 0)\n"
            "    with lock:\n"
            "        cmds.append(msg)\n"
            "        if op in ('rec_start', 'rec_stop'):\n"
            "            rec_cmds.append(op)\n"
            "    if op == 'set_scenario':\n"
            "        scenario[0] = str(msg.get('mode') or '')\n"
            "        hold_evt.clear()\n"
            "        with lock:\n"
            "            cmds.clear()\n"
            "            rec_cmds.clear()\n"
            "        emit({'type': 'reply', 'reply': 'set_scenario', 'seq_id': seq, 'ok': True})\n"
            "    elif op == 'inject_hotkey':\n"
            "        emit({'type': 'reply', 'reply': 'inject_hotkey', 'seq_id': seq, 'ok': True})\n"
            "        emit({'event': msg.get('hk_event'), 'hwnd': 77, 'process': 'WindowsTerminal', 'title': 'codex'})\n"
            "    elif op == 'rec_start':\n"
            "        sc = scenario[0]\n"
            "        if sc == 'fail_0frames_ptt':\n"
            "            emit({'event': 'ptt_stop', 'hwnd': 77, 'process': 'WindowsTerminal', 'title': 'codex'})\n"
            "            device_recording[0] = False\n"
            "            emit({'type': 'reply', 'reply': 'rec_start', 'seq_id': seq, 'ok': False, 'rec_error': 'mic_busy:wasapi_startup_no_frames'})\n"
            "        elif sc == 'ok_early_ptt_stop':\n"
            "            emit({'event': 'ptt_stop', 'hwnd': 77, 'process': 'WindowsTerminal', 'title': 'codex'})\n"
            "            device_recording[0] = True\n"
            "            emit({'type': 'reply', 'reply': 'rec_start', 'seq_id': seq, 'ok': True, 'rec_error': ''})\n"
            "        elif sc == 'hold_inflight':\n"
            "            emit({'event': 'test_rec_start_received', 'seq_id': seq})\n"
            "            def _delayed_abort(s=seq):\n"
            "                hold_evt.wait(timeout=3.0)\n"
            "                device_recording[0] = False\n"
            "                emit({'type': 'reply', 'reply': 'rec_start', 'seq_id': s, 'ok': False, 'rec_error': 'mic_busy:wasapi_aborted'})\n"
            "            threading.Thread(target=_delayed_abort, daemon=True).start()\n"
            "        else:\n"
            "            device_recording[0] = True\n"
            "            emit({'type': 'reply', 'reply': 'rec_start', 'seq_id': seq, 'ok': True, 'rec_error': ''})\n"
            "    elif op == 'rec_stop':\n"
            "        was_rec = device_recording[0]\n"
            "        device_recording[0] = False\n"
            "        hold_evt.set()\n"
            "        emit({'type': 'reply', 'reply': 'rec_stop', 'seq_id': seq, 'ok': False, 'pcm_bytes': 0, 'wav_b64': '', 'rec_error': 'wasapi_zero_packets' if was_rec else 'not_recording'})\n"
            "    elif op == 'dump_state':\n"
            "        with lock:\n"
            "            snap_cmds = list(cmds)\n"
            "            snap_rec = list(rec_cmds)\n"
            "        emit({'type': 'reply', 'reply': 'dump_state', 'seq_id': seq, 'ok': True, 'cmds': snap_cmds, 'rec_cmds': snap_rec, 'device_recording': device_recording[0]})\n"
            "    elif op == 'quit':\n"
            "        device_recording[0] = False\n"
            "        hold_evt.set()\n"
            "        break\n"
            "    else:\n"
            "        emit({'type': 'reply', 'reply': op, 'seq_id': seq, 'ok': True})\n"
        )
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            cfg = Config(
                hotkey=HotkeyConfig(trigger_mode="push_to_talk", debounce_ms=0),
                audio=AudioConfig(wsl_source="auto", beep=True),
                ui=UiConfig(language="zh"),
            )
            wplat = WslPlatform(
                cfg,
                env={"HOME": td},
                shim_cmd_factory=lambda: [sys.executable, "-u", "-c", mock_shim_code],
            )
            wplat._primary_winmm = True
            wplat._use_winmm = True
            pulse_called = False

            def _forbid_pulse(*_a: object, **_k: object) -> None:
                nonlocal pulse_called
                pulse_called = True
                raise AssertionError("PulseAudio fallback must not run on WASAPI busy/cancel/shutdown")

            wplat._ff_rec.start = _forbid_pulse  # type: ignore[method-assign]
            daemon = VoiceInputDaemon(
                cfg,
                env={"HOME": td},
                runtime_dir=rt,
                platform=wplat,
                asr_provider=_FakeAsr("unused"),
                refiner=create_refiner(cfg),
                tracker=_isolated_tracker(rt),
            )
            rec_start_seen = threading.Event()
            hotkey_done_count = 0
            hotkey_cv = threading.Condition()

            def _on_shim_event(m: dict[str, object]) -> None:
                nonlocal hotkey_done_count
                ev = str(m.get("event") or "")
                if ev == "test_rec_start_received":
                    rec_start_seen.set()
                    return
                if ev in ("toggle", "ptt_start", "ptt_stop", "ptt_cancel"):
                    daemon.handle_trigger(ev, _window_from_mapping(m), from_hotkey=True)
                    with hotkey_cv:
                        hotkey_done_count += 1
                        hotkey_cv.notify_all()

            try:
                wplat.start_event_listener(_on_shim_event)

                # (a) Charging-case 0-frame failure + <850ms PTT release -> single mic_no_stream toast, idle state
                wplat._shim_rpc({"cmd": "inject_hotkey", "hk_event": "ptt_start"}, timeout_sec=2.0)
                with hotkey_cv:
                    self.assertTrue(hotkey_cv.wait_for(lambda: hotkey_done_count >= 2, timeout=2.0))
                self.assertEqual(daemon.state, "idle")
                self.assertFalse(pulse_called)
                self.assertEqual(daemon.last_result.get("skipped_reason"), "mic_no_stream")
                self.assertIn("mic_busy:wasapi_startup_no_frames", daemon.last_result.get("error", ""))

                st_a = wplat._shim_rpc({"cmd": "dump_state"}, timeout_sec=2.0) or {}
                hud_a = [c for c in (st_a.get("cmds") or []) if c.get("cmd") == "hud"]
                beep_a = [c.get("kind") for c in (st_a.get("cmds") or []) if c.get("cmd") == "beep"]
                self.assertEqual([c.get("state") for c in hud_a], ["mic_no_stream"])
                self.assertEqual(hud_a[0].get("layer"), "toast")
                self.assertIs(hud_a[0].get("clear_main"), True)
                self.assertEqual(hud_a[0].get("duration_ms"), 2600)
                self.assertEqual(hud_a[0].get("text"), "! 麦克风无音频流，请检查耳机/设备")
                self.assertEqual(beep_a, ["error"])
                self.assertEqual(st_a.get("rec_cmds"), ["rec_start"])
                self.assertIs(st_a.get("device_recording"), False)

                # (b) <850ms PTT release when first frame arrives -> queued ptt_stop stops capture cleanly
                with hotkey_cv:
                    hotkey_done_count = 0
                wplat._shim_rpc({"cmd": "set_scenario", "mode": "ok_early_ptt_stop"}, timeout_sec=2.0)
                wplat._shim_rpc({"cmd": "inject_hotkey", "hk_event": "ptt_start"}, timeout_sec=2.0)
                with hotkey_cv:
                    self.assertTrue(hotkey_cv.wait_for(lambda: hotkey_done_count >= 2, timeout=2.0))
                # Wait for background utterance worker to finish processing empty audio back to idle
                with daemon._queue_cv:
                    self.assertTrue(
                        daemon._queue_cv.wait_for(
                            lambda: daemon.state == "idle" and daemon._active_task is None and not daemon._utterance_queue,
                            timeout=2.0,
                        )
                    )
                st_b = wplat._shim_rpc({"cmd": "dump_state"}, timeout_sec=2.0) or {}
                self.assertEqual(st_b.get("rec_cmds"), ["rec_start", "rec_stop"])
                self.assertIs(st_b.get("device_recording"), False)
                self.assertEqual(daemon.state, "idle")

                # (c1) Concurrent cancel_recording() AFTER rec_start is received by shim -> ordered ['rec_start', 'rec_stop']
                rec_start_seen.clear()
                wplat._shim_rpc({"cmd": "set_scenario", "mode": "hold_inflight"}, timeout_sec=2.0)
                start_err: list[BaseException] = []
                start_done = threading.Event()

                def _bg_start() -> None:
                    try:
                        wplat.start_recording(rt, seq_id=99)
                    except BaseException as exc:
                        start_err.append(exc)
                    finally:
                        start_done.set()

                threading.Thread(target=_bg_start, daemon=True).start()
                self.assertTrue(rec_start_seen.wait(timeout=2.0))
                wplat.cancel_recording()
                self.assertTrue(start_done.wait(timeout=2.0))
                self.assertEqual(len(start_err), 1)
                self.assertIsInstance(start_err[0], MicrophoneUnavailableError)
                st_c1 = wplat._shim_rpc({"cmd": "dump_state"}, timeout_sec=2.0) or {}
                self.assertEqual(st_c1.get("rec_cmds"), ["rec_start", "rec_stop"])
                self.assertIs(st_c1.get("device_recording"), False)
                self.assertEqual(wplat._rec_mode, "")

                # (c2) Concurrent cancel_recording() BEFORE rec_start writes to stdin -> never inverted ['rec_stop', 'rec_start']
                wplat._shim_rpc({"cmd": "set_scenario", "mode": "ok_immediate"}, timeout_sec=2.0)
                orig_ensure = wplat._ensure_shim
                entered_ensure = threading.Event()
                release_ensure = threading.Event()

                def _paused_ensure() -> bool:
                    ok = orig_ensure()
                    if not entered_ensure.is_set():
                        entered_ensure.set()
                        release_ensure.wait(timeout=2.0)
                    return ok

                wplat._ensure_shim = _paused_ensure  # type: ignore[method-assign]
                pre_err: list[BaseException] = []
                pre_done = threading.Event()

                def _bg_pre_start() -> None:
                    try:
                        wplat.start_recording(rt, seq_id=100)
                    except BaseException as exc:
                        pre_err.append(exc)
                    finally:
                        pre_done.set()

                try:
                    threading.Thread(target=_bg_pre_start, daemon=True).start()
                    self.assertTrue(entered_ensure.wait(timeout=2.0))
                    wplat.cancel_recording()
                    release_ensure.set()
                    self.assertTrue(pre_done.wait(timeout=2.0))
                finally:
                    wplat._ensure_shim = orig_ensure  # type: ignore[method-assign]

                self.assertEqual(len(pre_err), 1)
                self.assertIsInstance(pre_err[0], MicrophoneUnavailableError)
                st_c2 = wplat._shim_rpc({"cmd": "dump_state"}, timeout_sec=2.0) or {}
                self.assertEqual(st_c2.get("rec_cmds"), [])
                self.assertIs(st_c2.get("device_recording"), False)
                self.assertEqual(wplat._rec_mode, "")
                self.assertFalse(pulse_called)

                # (d) Shutdown/close during in-flight rec_start with wsl_source='auto' must NEVER spawn PulseAudio fallback
                rec_start_seen.clear()
                wplat._shim_rpc({"cmd": "set_scenario", "mode": "hold_inflight"}, timeout_sec=2.0)
                close_err: list[BaseException] = []
                close_done = threading.Event()

                def _bg_close_start() -> None:
                    try:
                        wplat.start_recording(rt, seq_id=101)
                    except BaseException as exc:
                        close_err.append(exc)
                    finally:
                        close_done.set()

                threading.Thread(target=_bg_close_start, daemon=True).start()
                self.assertTrue(rec_start_seen.wait(timeout=2.0))
                daemon.close()
                self.assertTrue(close_done.wait(timeout=2.0))
                self.assertEqual(len(close_err), 1)
                self.assertFalse(pulse_called)
            finally:
                daemon.close()

    def test_v091_noop_passthrough_telemetry_openrouter_refiner_cache_and_pipeline_single_hop(self) -> None:
        from unittest import mock
        from agent_voice_input import prompts
        from agent_voice_input.daemon import build_pipeline_summary, run_pipeline_once
        from agent_voice_input.refiner import NoopRefiner, REFINER_SYSTEM_PROMPT, attach_single_hop_context
        from agent_voice_input.session_ctx import SessionMatch

        # 1. prompts.py shared constants match refiner exports and enforce backtick/silence rules on single-hop
        self.assertEqual(prompts.REFINER_SYSTEM_PROMPT, REFINER_SYSTEM_PROMPT)
        self.assertIn("never `refiner.py`", prompts.SINGLE_HOP_SYSTEM_PROMPT)
        self.assertIn("output nothing at all", prompts.SINGLE_HOP_SYSTEM_PROMPT)

        # 2. NoopRefiner forwards single-hop AsrResult token & context telemetry cleanly
        sh_asr_res = AsrResult(
            text="一步到位结果",
            provider="dashscope",
            model="qwen3.8-omni-flash",
            latency_ms=890,
            is_single_hop_refined=True,
            input_tokens=12321,
            cached_input_tokens=10752,
            output_tokens=15,
            reasoning_tokens=0,
            context_chars=65432,
            context_chunks=2,
            audio_tokens=210,
        )
        noop_res = NoopRefiner().refine(sh_asr_res)
        self.assertEqual(noop_res.used_fallback, "single_hop_passthrough")
        self.assertEqual(noop_res.input_tokens, 12321)
        self.assertEqual(noop_res.cached_input_tokens, 10752)
        self.assertEqual(noop_res.output_tokens, 15)
        self.assertEqual(noop_res.reasoning_tokens, 0)
        self.assertEqual(noop_res.audio_tokens, 210)
        self.assertEqual(noop_res.context_chars, 65432)
        self.assertEqual(noop_res.context_chunks, 2)

        # 3. OpenRouter Stage 2 refiner attaches cache_control on last sealed block for google/*,
        #    while DashScope keeps user.content as a single plain string
        seen_reqs: list[HttpRequest] = []

        def tx(req: HttpRequest) -> HttpResponse:
            seen_reqs.append(req)
            return HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps({"choices": [{"message": {"content": "精修文本"}}]}).encode("utf-8"),
            )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jpath = root / "sess.jsonl"
            jpath.write_text(
                "\n".join(
                    json.dumps({"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": ("长历史块内容_" * 5000) + f"#{i}"})
                    for i in range(4)
                )
                + "\n",
                encoding="utf-8",
            )
            tracker = SessionTracker(home_dir=root, runtime_dir=root / "rt", proc_root=root / "no_proc", env={})
            snap = tracker.sync_session(
                match=SessionMatch(cli="agy", session_id="s-cache", jsonl_path=str(jpath), cwd="", matched_by="test"),
                include_git_info=False,
            )
            assert snap is not None

            env = {"OPENROUTER_API_KEY": "sk-or-" + ("x" * 16), "DASHSCOPE_API_KEY": "sk-ali-" + ("y" * 16)}
            cfg_or = Config(refiner=RefinerConfig(provider="openrouter"))
            or_ref = create_refiner(cfg_or, resolve_credentials(cfg_or, env), env=env, transport=tx)
            or_out = or_ref.refine("初稿", session_key=snap.session_key, tracker=tracker)
            self.assertGreaterEqual(or_out.context_chunks, 2)
            or_msg_content = json.loads(seen_reqs[0].body.decode("utf-8"))["messages"][1]["content"]
            self.assertIsInstance(or_msg_content, list)
            sealed_idx = or_out.context_chunks - 2
            self.assertEqual(or_msg_content[sealed_idx].get("cache_control"), {"type": "ephemeral"})
            self.assertNotIn("cache_control", or_msg_content[or_out.context_chunks - 1])

            seen_reqs.clear()
            cfg_ali = Config(refiner=RefinerConfig(provider="dashscope"))
            ali_ref = create_refiner(cfg_ali, resolve_credentials(cfg_ali, env), env=env, transport=tx)
            ali_ref.refine("初稿", session_key=snap.session_key, tracker=tracker)
            ali_msg_content = json.loads(seen_reqs[0].body.decode("utf-8"))["messages"][1]["content"]
            self.assertIsInstance(ali_msg_content, str)

            # 4. run_pipeline_once attaches 50K blocks for terminal single-hop ASR, skips sync in coding_gui,
            #    and propagates single-hop token telemetry into build_pipeline_summary
            seen_ctxs: list[Any] = []

            class _FakeSingleHopAsr:
                provider_name = "dashscope"
                single_hop = True

                def transcribe(self, wav_bytes: bytes, context=None, **kw) -> AsrResult:
                    seen_ctxs.append(context)
                    return sh_asr_res

            voiced = _voiced_wav()
            tracker_term = mock.MagicMock(wraps=tracker)
            tracker_term.sync_session.return_value = snap
            res_term = run_pipeline_once(
                voiced,
                cfg=Config(refiner=RefinerConfig(provider="codex")),
                tracker=tracker_term,
                asr_provider=_FakeSingleHopAsr(),  # type: ignore[arg-type]
                origin_window=ForegroundWindow(1, "WindowsTerminal.exe", "agy [#s-cache]"),
                dry_run=True,
                env=env,
            )
            self.assertTrue(res_term.ok)
            self.assertGreaterEqual(len(seen_ctxs[0].context_blocks), 2)
            summary_term = build_pipeline_summary(1, res_term, dry_run=True)
            self.assertEqual(summary_term["input_tokens"], 12321)
            self.assertEqual(summary_term["cached_input_tokens"], 10752)
            self.assertEqual(summary_term["cache_hit_pct"], 87.3)
            self.assertNotIn("asr_input_tokens", summary_term)

            # coding_gui MUST isolate background CLI sessions in single-hop mode too
            seen_ctxs.clear()
            res_ide = run_pipeline_once(
                voiced,
                cfg=Config(refiner=RefinerConfig(provider="codex")),
                tracker=tracker,
                asr_provider=_FakeSingleHopAsr(),  # type: ignore[arg-type]
                origin_window=ForegroundWindow(2, "Code.exe", "refiner.py - Visual Studio Code"),
                dry_run=True,
                env=env,
            )
            self.assertTrue(res_ide.ok)
            self.assertEqual(seen_ctxs[0].context_blocks, ())
            self.assertEqual(seen_ctxs[0].mode, "agent")

            # attach_single_hop_context in general mode AND context_max_chars=0/include_git_status=False
            # clears legacy dialog_text & git_status so single-hop ASR never leaks disabled context!
            base_with_legacy = tracker.build_asr_context(snap, mode="agent")
            self.assertTrue(base_with_legacy.dialog_text)
            gen_ctx = attach_single_hop_context(
                Config(),
                base_with_legacy,
                tracker=tracker,
                session_key=snap.session_key,
                mode="general",
            )
            self.assertEqual(gen_ctx.mode, "general")
            self.assertEqual(gen_ctx.context_blocks, ())
            self.assertEqual(gen_ctx.git_block, "")
            self.assertEqual(gen_ctx.dialog_text, "")
            self.assertEqual(gen_ctx.git_status, "")
            self.assertTrue(gen_ctx.single_hop_prepared)

            cfg_zero_ctx = Config(refiner=RefinerConfig(context_max_chars=0, include_git_status=False))
            zero_ctx = attach_single_hop_context(
                cfg_zero_ctx,
                AsrContext(hotwords=["SessionTracker"], dialog_text="SECRET_DIALOG_LEAK", git_status="SECRET_GIT_LEAK"),
                tracker=tracker,
                session_key=snap.session_key,
                mode="agent",
            )
            self.assertEqual(zero_ctx.context_blocks, ())
            self.assertEqual(zero_ctx.git_block, "")
            self.assertEqual(zero_ctx.dialog_text, "")
            self.assertEqual(zero_ctx.git_status, "")
            self.assertTrue(zero_ctx.single_hop_prepared)

            sh_reqs: list[HttpRequest] = []
            from agent_voice_input.asr_providers import create_asr_provider

            def sh_tx(r: HttpRequest) -> HttpResponse:
                sh_reqs.append(r)
                return HttpResponse(200, {"content-type": "application/json"}, json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode("utf-8"))

            cfg_sh_ali = Config()
            cfg_sh_ali.asr.provider = "dashscope"
            cfg_sh_ali.asr.model = "qwen3.8-omni-flash"
            cfg_sh_ali.asr.hotwords_file = ""
            sh_prov = create_asr_provider(cfg_sh_ali, resolve_credentials(cfg_sh_ali, env), env=env, transport=sh_tx)
            sh_prov.transcribe(voiced, context=zero_ctx)
            sh_user_txt = json.dumps(json.loads(sh_reqs[0].body.decode("utf-8"))["messages"][1]["content"], ensure_ascii=False)
            self.assertNotIn("SECRET_DIALOG_LEAK", sh_user_txt)
            self.assertNotIn("SECRET_GIT_LEAK", sh_user_txt)
            self.assertNotIn("<session_context>", sh_user_txt)

            # Direct programmatic create_refiner rejects openai-compatible missing base_url / model (never leaks to api.openai.com)
            with self.assertRaises(ConfigError):
                create_refiner(Config(refiner=RefinerConfig(provider="openai-compatible", model="deepseek-flash")), env=env)
            with self.assertRaises(ConfigError):
                create_refiner(Config(refiner=RefinerConfig(provider="openai-compatible", base_url="https://api.deepseek.com/v1")), env=env)

            # _clean_out strips echoed <asr_draft_to_clean> wrapper unless literal tag existed in input draft
            from agent_voice_input.refiner import _clean_out

            self.assertEqual(_clean_out("<asr_draft_to_clean>\n精修正文", "原始初稿"), "精修正文")
            self.assertEqual(_clean_out("<asr_draft_to_clean>\n精修正文\n</asr_draft_to_clean>", "原始初稿"), "精修正文")
            self.assertEqual(_clean_out("<asr_draft_to_clean> 字面讨论", "<asr_draft_to_clean> 字面讨论"), "<asr_draft_to_clean> 字面讨论")

            # _cmd_doctor reports single-hop passthrough bypass and succeeds (rc=0) even without Codex login
            import io
            from agent_voice_input import cli as cli_mod

            cfg_doc_sh = Config(refiner=RefinerConfig(provider="codex"))
            cfg_doc_sh.asr.provider = "dashscope"
            cfg_doc_sh.asr.model = "qwen3.8-omni-flash"
            buf_doc_sh = io.StringIO()
            no_codex_env = {"HOME": str(root / "empty_home"), "AGENT_VOICE_INPUT_RUNTIME_DIR": str(root / "rt_doc"), "DASHSCOPE_API_KEY": env["DASHSCOPE_API_KEY"], "PATH": ""}
            with (
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                mock.patch("sys.platform", "linux"),
                mock.patch("sys.stdout", buf_doc_sh),
            ):
                rc_doc_sh = cli_mod._cmd_doctor(cfg_doc_sh, no_codex_env)
            self.assertEqual(rc_doc_sh, 0, buf_doc_sh.getvalue())
            self.assertIn("bypassed (single-hop ASR passthrough", buf_doc_sh.getvalue())


class V092aInflightCancelAndConcurrentStage2Tests(unittest.TestCase):
    def test_slow_sse_stream_instant_cancel_failed_stash_and_zero_pool_pollution(self) -> None:
        from agent_voice_input.asr_providers import CancelToken, _KEEPALIVE_POOL, urllib_http_transport
        from agent_voice_input.audio_proc import list_failed_audios

        first_delta_received = threading.Event()
        release_server_hang = threading.Event()

        def responder(handler: http.server.BaseHTTPRequestHandler, rec: dict[str, object]) -> None:
            if rec["seq"] == 1:
                handler.send_response(200)
                handler.send_header("Transfer-Encoding", "chunked")
                handler.send_header("Connection", "keep-alive")
                handler.end_headers()
                partial = (
                    f"data: {json.dumps({'type': 'response.output_text.delta', 'delta': 'POISON_LEFTOVER_CHUNK'}, ensure_ascii=False)}\n\n"
                ).encode("utf-8")
                handler.wfile.write(f"{len(partial):x}\r\n".encode("ascii") + partial + b"\r\n")
                handler.wfile.flush()
                first_delta_received.set()
                release_server_hang.wait(timeout=5.0)
                handler.close_connection = True
                return
            _LocalResponsesServer.send_sse_200(
                handler,
                _sse_completed_frames(["第二句干净连接成功"]),
            )

        srv = _LocalResponsesServer(responder=responder)
        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                rt = root / "runtime"
                rt.mkdir()
                cfg = Config(refiner=RefinerConfig(provider="openai", api="responses", base_url=srv.base_url, timeout_sec=10.0))
                env = {"OPENAI_API_KEY": "sk-unit-test-v092a"}
                ref = OpenAIResponsesRefiner(cfg, env=env)
                plat = _FakePlatform()
                daemon = VoiceInputDaemon(
                    cfg,
                    env=env,
                    runtime_dir=rt,
                    platform=plat,
                    asr_provider=_FakeAsr("第一句原始草稿绝不能回退上屏"),
                    refiner=ref,
                    tracker=_isolated_tracker(rt),
                    paste_settle_sec=0.0,
                )
                try:
                    daemon.handle_trigger("toggle")
                    daemon._last_trigger_ts = -1e9
                    s1 = daemon.handle_trigger("toggle")
                    self.assertTrue(first_delta_received.wait(timeout=2.0))

                    t_c0 = time.monotonic()
                    c_resp = daemon.handle_cancel()
                    cancel_ms = (time.monotonic() - t_c0) * 1000.0
                    self.assertTrue(c_resp["ok"], c_resp)
                    self.assertEqual(c_resp["cancelled_ids"], [s1["seq_id"]])
                    # handle_cancel() caps its commit-drain wait_for() at 250ms while _commit_worker_loop
                    # runs save_failed_audio() (os.fsync); under CI disk jitter a single fsync can hit that
                    # 250ms cap, so bound well below the 5s server hang / 10s HTTP timeout instead of 150ms.
                    self.assertLess(cancel_ms, 1000.0, f"cancel took too long: {cancel_ms:.1f}ms")
                    self.assertEqual(daemon.state, "idle")
                    self.assertEqual(daemon.handle_control({"cmd": "status"})["pending_count"], 0)
                    self.assertEqual(plat.deliveries, [], "cancelled request must NEVER fall back to asr_draft or paste!")
                    self.assertEqual(plat.hud_events[-1]["state"], "cancelled")
                    self.assertIs(plat.hud_events[-1].get("clear_main"), True)

                    # If handle_cancel() returned at its 250ms wait cap while fsync was still finishing,
                    # wait briefly for _commit_worker_loop to publish the cancelled summary & stashed WAV.
                    cancel_dl = time.monotonic() + 1.5
                    while time.monotonic() < cancel_dl and daemon.last_result.get("seq_id") != s1["seq_id"]:
                        time.sleep(0.01)
                    self.assertEqual(daemon.last_result["skipped_reason"], "cancelled")
                    self.assertFalse(daemon.last_result["ok"])

                    failed_wavs = list_failed_audios(rt)
                    self.assertEqual(len(failed_wavs), 1)
                    self.assertEqual(failed_wavs[0].stat().st_mode & 0o777, 0o600)

                    # Immediately run utterance #2 on the SAME OpenAIResponsesRefiner pool -> MUST open a fresh socket
                    daemon.asr_provider = _FakeAsr("第二句语音")
                    daemon._last_trigger_ts = -1e9
                    daemon.handle_trigger("toggle")
                    daemon._last_trigger_ts = -1e9
                    s2 = daemon.handle_trigger("toggle")
                    deadline = time.monotonic() + 2.0
                    while time.monotonic() < deadline and daemon.last_result.get("seq_id") != s2["seq_id"]:
                        time.sleep(0.02)
                    self.assertTrue(daemon.last_result["ok"], daemon.last_result)
                    self.assertEqual([d[0] for d in plat.deliveries], ["第二句干净连接成功"])
                    self.assertEqual(srv.accepted_conns, 2)
                    self.assertNotEqual(srv.requests[0]["client_port"], srv.requests[1]["client_port"])
                finally:
                    release_server_hang.set()
                    daemon.close()
        finally:
            release_server_hang.set()
            srv.close()

        # Also verify global _KEEPALIVE_POOL (_execute_keepalive) mid-read abort & SSLSocket.shutdown redline
        import ssl

        ssl_shutdown_called = [False]
        orig_ssl_shutdown = ssl.SSLSocket.shutdown

        def _forbid_ssl_shutdown(self_sock: ssl.SSLSocket, *a: object, **kw: object) -> object:
            ssl_shutdown_called[0] = True
            return orig_ssl_shutdown(self_sock, *a, **kw)

        ka_hang_entered = threading.Event()
        ka_release = threading.Event()

        def ka_responder(handler: http.server.BaseHTTPRequestHandler, rec: dict[str, object]) -> None:
            if rec["seq"] == 1:
                handler.send_response(200)
                handler.send_header("Content-Type", "application/json")
                handler.send_header("Content-Length", "128")
                handler.send_header("Connection", "keep-alive")
                handler.end_headers()
                handler.wfile.write(b'{"partial":')
                handler.wfile.flush()
                ka_hang_entered.set()
                ka_release.wait(timeout=5.0)
                handler.close_connection = True
                return
            _LocalResponsesServer.send_json(handler, 200, {"choices": [{"message": {"content": "干净响应"}}]})

        srv_ka = _LocalResponsesServer(responder=ka_responder)
        try:
            with unittest.mock.patch.object(ssl.SSLSocket, "shutdown", _forbid_ssl_shutdown):
                tok = CancelToken()
                err_box: list[Exception] = []

                def _bg_req() -> None:
                    try:
                        urllib_http_transport(
                            HttpRequest(
                                url=f"http://127.0.0.1:{srv_ka.port}/v1/chat/completions",
                                method="POST",
                                headers={"Content-Type": "application/json"},
                                body=b"{}",
                                timeout_sec=8.0,
                                cancel_token=tok,
                            )
                        )
                    except Exception as exc:
                        err_box.append(exc)

                th = threading.Thread(target=_bg_req, daemon=True)
                th.start()
                self.assertTrue(ka_hang_entered.wait(timeout=2.0))
                t0 = time.monotonic()
                self.assertTrue(tok.cancel("cancelled"))
                th.join(timeout=0.5)
                self.assertFalse(th.is_alive())
                self.assertLess((time.monotonic() - t0) * 1000.0, 150.0)
                self.assertEqual(len(err_box), 1)
                self.assertFalse(ssl_shutdown_called[0])

                # Next request to same origin on _KEEPALIVE_POOL gets a brand-new connection
                r2 = urllib_http_transport(
                    HttpRequest(
                        url=f"http://127.0.0.1:{srv_ka.port}/v1/chat/completions",
                        method="POST",
                        headers={"Content-Type": "application/json"},
                        body=b"{}",
                        timeout_sec=3.0,
                    )
                )
                self.assertEqual(r2.status, 200)
                self.assertEqual(srv_ka.accepted_conns, 2)
        finally:
            ka_release.set()
            _KEEPALIVE_POOL.close_all()
            srv_ka.close()

    def test_stage1_backoff_and_doubao_query_polling_cancel_and_retry_source_no_restash(self) -> None:
        from agent_voice_input.asr_providers import CancelToken, DashScopeAsrProvider, DoubaoAsrProvider
        from agent_voice_input.audio_proc import list_failed_audios

        reset_rate_limit_pacers()
        voiced = _voiced_wav()

        # 1. Stage 1 HTTP 503 retry backoff wakes in <100ms via CancelToken bound Event (using real time.sleep!)
        attempt_1_done = threading.Event()

        def tx_503(_req: HttpRequest) -> HttpResponse:
            attempt_1_done.set()
            return HttpResponse(503, {"content-type": "application/json"}, b'{"error":{"message":"overloaded"}}')

        cfg_ds = Config()
        cfg_ds.asr.provider = "dashscope"
        cfg_ds.asr.max_retries = 2
        cfg_ds.asr.timeout_sec = 10.0
        env_ds = {"DASHSCOPE_API_KEY": "sk-ds-test"}
        ds_prov = DashScopeAsrProvider(cfg_ds, resolve_credentials(cfg_ds, env_ds), env=env_ds, transport=tx_503)

        tok_backoff = CancelToken()
        res_box: list[object] = []

        def _run_ds() -> None:
            res_box.append(
                run_pipeline_once(
                    voiced,
                    cfg=Config(refiner=RefinerConfig(provider="off")),
                    asr_provider=ds_prov,
                    cancel_token=tok_backoff,
                    audio_source="winmm",
                    dry_run=True,
                    env=env_ds,
                )
            )

        th_ds = threading.Thread(target=_run_ds, daemon=True)
        th_ds.start()
        self.assertTrue(attempt_1_done.wait(timeout=2.0))
        time.sleep(0.02)
        t_b0 = time.monotonic()
        self.assertTrue(tok_backoff.cancel("cancelled"))
        th_ds.join(timeout=0.5)
        self.assertFalse(th_ds.is_alive())
        self.assertLess((time.monotonic() - t_b0) * 1000.0, 120.0)
        self.assertEqual(res_box[0].skipped_reason, "cancelled")  # type: ignore[union-attr]

        # 2. Doubao /query polling wakes immediately on CancelToken and retry audio_source never re-stashes failed/
        query_entered = threading.Event()

        def tx_doubao(req: HttpRequest) -> HttpResponse:
            if "/flash" in req.url:
                return HttpResponse(
                    200,
                    {"x-api-status-code": "45000030", "x-api-message": "resource not granted"},
                    b"{}",
                )
            if "/submit" in req.url:
                return HttpResponse(200, {"x-api-status-code": "20000000"}, b"{}")
            query_entered.set()
            return HttpResponse(200, {"x-api-status-code": "20000001"}, b"{}")

        cfg_db = Config(refiner=RefinerConfig(provider="off"))
        cfg_db.asr.provider = "doubao"
        cfg_db.asr.timeout_sec = 12.0
        env_db = {"DOUBAO_ASR_APP_ID": "app-1", "DOUBAO_ASR_API_KEY": "tok-1"}
        db_prov = DoubaoAsrProvider(cfg_db, resolve_credentials(cfg_db, env_db), env=env_db, transport=tx_doubao)

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            tok_db = CancelToken()
            db_res_box: list[object] = []

            def _run_db() -> None:
                db_res_box.append(
                    run_pipeline_once(
                        voiced,
                        cfg=cfg_db,
                        asr_provider=db_prov,
                        cancel_token=tok_db,
                        audio_source="retry",
                        runtime_dir=rt,
                        dry_run=True,
                        env=env_db,
                    )
                )

            th_db = threading.Thread(target=_run_db, daemon=True)
            th_db.start()
            self.assertTrue(query_entered.wait(timeout=2.0))
            t_q0 = time.monotonic()
            self.assertTrue(tok_db.cancel("cancelled"))
            th_db.join(timeout=0.5)
            self.assertFalse(th_db.is_alive())
            self.assertLess((time.monotonic() - t_q0) * 1000.0, 120.0)
            out_db = db_res_box[0]
            self.assertFalse(out_db.ok)  # type: ignore[union-attr]
            self.assertEqual(out_db.skipped_reason, "cancelled")  # type: ignore[union-attr]
            self.assertEqual(out_db.saved_audio, "")  # type: ignore[union-attr]
            self.assertEqual(list_failed_audios(rt), [], "retry source must NOT re-stash WAV on cancel")

            # Legacy bare is_cancelled callback keeps 'superseded' & >=15s threshold backward compatibility
            res_legacy = run_pipeline_once(
                voiced,
                cfg=cfg_db,
                asr_provider=_FakeAsr("legacy"),
                is_cancelled=lambda: True,
                audio_source="winmm",
                runtime_dir=rt,
                dry_run=True,
                env=env_db,
            )
            self.assertEqual(res_legacy.skipped_reason, "superseded")
            self.assertEqual(list_failed_audios(rt), [])

            # 3. CodexRefiner auth refresh cancellation decoupling:
            #    #1 cancels mid-getAuthStatus -> kills temp proc without poisoning cooldown; #2 refreshes cleanly!
            home_dir = rt / "codex_home"
            _write_codex_auth_json(home_dir, exp=int(time.time()) + 60)  # inside 30m proactive refresh window
            fresh_jwt = _make_fake_jwt(exp=int(time.time()) + 7200, account_id="acct-fresh-v092a")
            call_counter_file = rt / "auth_calls.txt"
            slow_first_auth_py = (
                "import json, pathlib, sys, time\n"
                f"cf = pathlib.Path({str(call_counter_file)!r})\n"
                "n = int(cf.read_text()) + 1 if cf.exists() else 1\n"
                "cf.write_text(str(n))\n"
                "for raw in sys.stdin:\n"
                "    m = json.loads(raw)\n"
                "    if m.get('method') == 'initialize':\n"
                "        sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':{}}) + '\\n'); sys.stdout.flush()\n"
                "    elif m.get('method') == 'getAuthStatus':\n"
                "        if n == 1: time.sleep(5.0)\n"
                f"        sys.stdout.write(json.dumps({{'jsonrpc':'2.0','id':m['id'],'result':{{'authMethod':'chatgpt','authToken':{fresh_jwt!r}}}}}) + '\\n'); sys.stdout.flush()\n"
            )
            srv_codex = _LocalResponsesServer()
            try:
                cfg_cx = Config(refiner=RefinerConfig(provider="codex", base_url=srv_codex.base_url, timeout_sec=8.0))
                cref = CodexRefiner(
                    cfg_cx,
                    env={"HOME": str(home_dir)},
                    app_server_cmd=[sys.executable, "-u", "-c", slow_first_auth_py],
                )
                try:
                    tok_cx1 = CancelToken()
                    r_cx1: list[RefinerResult] = []
                    r_cx2: list[RefinerResult] = []
                    t1 = threading.Thread(
                        target=lambda: r_cx1.append(cref.refine("请求一被取消", cancel_token=tok_cx1)),
                        daemon=True,
                    )
                    t1.start()
                    dl_cf = time.monotonic() + 2.0
                    while time.monotonic() < dl_cf and not call_counter_file.exists():
                        time.sleep(0.01)
                    t2 = threading.Thread(
                        target=lambda: r_cx2.append(cref.refine("请求二正常刷新")),
                        daemon=True,
                    )
                    t2.start()
                    time.sleep(0.03)
                    self.assertTrue(tok_cx1.cancel("cancelled"))
                    t1.join(timeout=1.0)
                    t2.join(timeout=2.0)
                    self.assertEqual(r_cx1[0].used_fallback, "cancelled")
                    self.assertEqual(r_cx2[0].used_fallback, "")
                    self.assertIn("请求二正常刷新", r_cx2[0].text)
                    self.assertEqual(call_counter_file.read_text(), "2")
                finally:
                    cref.close()
            finally:
                srv_codex.close()

    def test_out_of_order_stage2_strict_seq_delivery_per_utterance_origin_window_and_queue_wait_ms(self) -> None:
        from agent_voice_input.daemon import read_telemetry_history
        from agent_voice_input.refiner import BaseRefiner

        win_a = ForegroundWindow(hwnd=101, process="WindowsTerminal", title="claude · proj-a [#1111aaaa]")
        win_b = ForegroundWindow(hwnd=202, process="WindowsTerminal", title="codex · proj-b [#2222bbbb]")
        refine_done_order: list[str] = []
        s2_refined_evt = threading.Event()

        class _SeqAsr(BaseAsrProvider):
            provider_name = "dashscope"

            def __init__(self) -> None:
                self.n = 0

            def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None, **_kw: object) -> AsrResult:
                self.n += 1
                if self.n == 1:
                    time.sleep(0.02)
                return AsrResult(text=f"草稿{self.n}", provider="dashscope", model="qwen3-asr-flash", latency_ms=10)

        class _OutOfOrderRefiner(BaseRefiner):
            provider_name = "codex"

            def refine(self, asr_input: object, **_kw: object) -> RefinerResult:
                raw = asr_input.text if isinstance(asr_input, AsrResult) else str(asr_input)
                if raw == "草稿1":
                    # Wait until utterance #2 has already completed Stage 2 plus extra margin
                    s2_refined_evt.wait(timeout=2.0)
                    time.sleep(0.05)
                    refine_done_order.append("1")
                    return RefinerResult(text="精修A", provider="codex", model="gpt-6-luna", latency_ms=120)
                refine_done_order.append("2")
                s2_refined_evt.set()
                return RefinerResult(text="精修B", provider="codex", model="gpt-6-luna", latency_ms=8)

        class _FocusAwarePlat(_FakePlatform):
            def __init__(self) -> None:
                super().__init__()
                self.current_fg = win_b
                self.delivery_log: list[tuple[int, str, int, str]] = []
                self.clipboard_writes: list[str] = []

            def resolve_foreground_window(self, hint: ForegroundWindow | None = None) -> ForegroundWindow:
                return hint if (hint and hint.hwnd > 0) else self.current_fg

            def deliver_text(
                self,
                text: str,
                seq_id: int,
                target_hwnd: int = 0,
                *,
                origin_window: ForegroundWindow | None = None,
                fallback_clipboard_text: str | None = None,
                before_clipboard_write: object = None,
                before_commit: object = None,
                **_kw: object,
            ) -> PasteOutcome:
                if callable(before_clipboard_write):
                    before_clipboard_write()
                if callable(before_commit) and not before_commit():
                    return PasteOutcome(False, False, "cancelled", "")
                eff_hwnd = origin_window.hwnd if origin_window is not None else target_hwnd
                if eff_hwnd != self.current_fg.hwnd:
                    clip = fallback_clipboard_text if fallback_clipboard_text is not None else text
                    self.clipboard_writes.append(clip)
                    self.delivery_log.append((seq_id, text, eff_hwnd, "focus_changed"))
                    return PasteOutcome(False, True, "focus_changed", self.current_fg.process)
                self.clipboard_writes.append(text)
                self.delivery_log.append((seq_id, text, eff_hwnd, "ok"))
                return PasteOutcome(True, True, "ok", self.current_fg.process, used_clipboard_paste=True)

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            plat = _FocusAwarePlat()
            cfg = Config(refiner=RefinerConfig(provider="codex"))
            daemon = VoiceInputDaemon(
                cfg,
                env={},
                runtime_dir=rt,
                platform=plat,
                asr_provider=_SeqAsr(),
                refiner=_OutOfOrderRefiner(),
                tracker=_isolated_tracker(rt),
                paste_settle_sec=0.0,
            )
            try:
                # Utterance #1 recorded in Window A
                daemon.handle_trigger("toggle", win_a)
                daemon._last_trigger_ts = -1e9
                r1 = daemon.handle_trigger("toggle", win_a)

                # User switches to Window B and records Utterance #2 while #1 is in flight
                plat.current_fg = win_b
                daemon._last_trigger_ts = -1e9
                daemon.handle_trigger("toggle", win_b)
                daemon._last_trigger_ts = -1e9
                r2 = daemon.handle_trigger("toggle", win_b)

                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline and daemon.last_result.get("seq_id") != r2["seq_id"]:
                    time.sleep(0.02)

                # Stage 2 completed out of order (2 before 1), yet Stage 3 committed strictly 1 -> 2
                # with independent origin_window checks (#1 focus_changed vs Window A, #2 pasted into Window B)!
                self.assertEqual(refine_done_order, ["2", "1"])
                self.assertEqual(
                    plat.delivery_log,
                    [
                        (r1["seq_id"], "精修A", 101, "focus_changed"),
                        (r2["seq_id"], "精修B", 202, "ok"),
                    ],
                )
                hist = read_telemetry_history(rt, limit=10)
                self.assertEqual([h["seq_id"] for h in hist], [r1["seq_id"], r2["seq_id"]])
                for rec in hist:
                    self.assertIn("queue_wait_ms", rec)
                    self.assertIsInstance(rec["queue_wait_ms"], int)
                    self.assertGreaterEqual(rec["queue_wait_ms"], 0)
            finally:
                daemon.close()

    def test_layered_cancel_recording_vs_inflight_seq_target_hold_cancel_try_commit_and_cli(self) -> None:
        import io
        from agent_voice_input import cli as cli_mod
        from agent_voice_input.audio_proc import list_failed_audios
        from agent_voice_input.refiner import BaseRefiner

        s1_in_refine = threading.Event()
        s1_release = threading.Event()
        unstoppable_hang = threading.Event()
        s2_fast_done = threading.Event()

        class _FlexWavPlatform(_FakePlatform):
            def __init__(self) -> None:
                super().__init__()
                self.wav_queue: list[bytes] = []
                self.pasted_texts: list[str] = []
                self.commit_hook: object = None

            def stop_recording(self, drain: bool = True) -> tuple[bytes, str]:
                _ = drain
                if self.wav_queue:
                    return self.wav_queue.pop(0), "winmm"
                return _voiced_wav(), "winmm"

            def deliver_text(
                self,
                text: str,
                seq_id: int,
                target_hwnd: int = 0,
                *,
                before_clipboard_write: object = None,
                before_commit: object = None,
                **_kw: object,
            ) -> PasteOutcome:
                if callable(before_clipboard_write):
                    before_clipboard_write()
                if callable(before_commit) and not before_commit():
                    return PasteOutcome(False, False, "cancelled", "")
                if callable(self.commit_hook):
                    self.commit_hook(seq_id)
                self.pasted_texts.append(text)
                self.deliveries.append((text, seq_id, target_hwnd))
                return PasteOutcome(True, True, "ok", "WindowsTerminal", used_clipboard_paste=True)

        mode_box = ["scenario1"]

        class _ScenarioAsr(BaseAsrProvider):
            provider_name = "dashscope"

            def __init__(self) -> None:
                self.idx = 0

            def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None, *, cancel_token: object = None, **_kw: object) -> AsrResult:
                self.idx += 1
                return AsrResult(text=f"句{self.idx}", provider="dashscope", model="qwen3-asr-flash", latency_ms=5)

        class _ScenarioRefiner(BaseRefiner):
            provider_name = "codex"

            def refine(self, asr_input: object, *, cancel_token: object = None, **_kw: object) -> RefinerResult:
                raw = asr_input.text if isinstance(asr_input, AsrResult) else str(asr_input)
                sc = mode_box[0]
                if sc == "scenario1" and raw == "句1":
                    s1_in_refine.set()
                    s1_release.wait(timeout=3.0)
                    if cancel_token is not None and getattr(cancel_token, "is_cancelled")():
                        return RefinerResult(text=raw, provider="codex", model="gpt-6-luna", latency_ms=5, used_fallback="cancelled")
                    return RefinerResult(text="精修句1", provider="codex", model="gpt-6-luna", latency_ms=10)
                if sc == "scenario2":
                    ev = threading.Event()
                    if cancel_token is not None:
                        getattr(cancel_token, "bind_event")(ev)
                    s1_in_refine.set()
                    ev.wait(timeout=3.0)
                    return RefinerResult(text=raw, provider="codex", model="gpt-6-luna", latency_ms=5, used_fallback="cancelled")
                if sc == "scenario3":
                    if raw == "句11":
                        s1_in_refine.set()
                        # Simulates a wedged thread that ignores cancellation for 3s; slot.done_evt MUST still wake 0ms!
                        unstoppable_hang.wait(timeout=3.0)
                        return RefinerResult(text="慢句11", provider="codex", model="gpt-6-luna", latency_ms=50)
                    out = f"快{raw}"
                    s2_fast_done.set()
                    return RefinerResult(text=out, provider="codex", model="gpt-6-luna", latency_ms=5)
                return RefinerResult(text=f"精修{raw}", provider="codex", model="gpt-6-luna", latency_ms=5)

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            rt.mkdir()
            plat = _FlexWavPlatform()
            cfg = Config(refiner=RefinerConfig(provider="codex"))
            daemon = VoiceInputDaemon(
                cfg,
                env={},
                runtime_dir=rt,
                platform=plat,
                asr_provider=_ScenarioAsr(),
                refiner=_ScenarioRefiner(),
                tracker=_isolated_tracker(rt),
                paste_settle_sec=0.0,
            )
            srv_th = threading.Thread(target=daemon.serve_forever, daemon=True)
            srv_th.start()
            _wait_for_control_socket(rt, timeout_sec=3.0)
            try:
                # 1. Idle hold_cancel -> ignored ('not_inflight')
                r_idle_hc = daemon.handle_cancel(only_inflight=True)
                self.assertFalse(r_idle_hc["ok"])
                self.assertEqual(r_idle_hc["ignored"], "not_inflight")

                # 2. #1 in Stage 2 + #2 actively recording:
                #    - explicit only_inflight=True still ignores while recording ('not_inflight');
                #    - hold_cancel uses layered cancel: cancels ONLY active recording #2, restores 'refining' + toast,
                #      never stashes cancelled recording in failed/, and lets #1 deliver!
                daemon.handle_trigger("toggle")
                daemon._last_trigger_ts = -1e9
                r_u1 = daemon.handle_trigger("toggle")
                self.assertTrue(s1_in_refine.wait(timeout=2.0))

                daemon._last_trigger_ts = -1e9
                r_u2_rec = daemon.handle_trigger("toggle")
                self.assertEqual(daemon.state, "recording")

                r_only_inf = daemon.handle_cancel(only_inflight=True)
                self.assertFalse(r_only_inf["ok"])
                self.assertEqual(r_only_inf["ignored"], "not_inflight")
                self.assertEqual(daemon.state, "recording")

                daemon._on_platform_event({"event": "hold_cancel", "hold_ms": 800})
                self.assertEqual(daemon.state, "refining")
                self.assertEqual(daemon._main_hud_state, "refining")
                self.assertEqual(plat.hud_events[-1]["state"], "cancelled")
                self.assertIs(plat.hud_events[-1].get("clear_main"), False)
                self.assertEqual(list_failed_audios(rt), [])

                s1_release.set()
                dl = time.monotonic() + 2.0
                while time.monotonic() < dl and daemon.last_result.get("seq_id") != r_u1["seq_id"]:
                    time.sleep(0.02)
                self.assertEqual(plat.pasted_texts, ["精修句1"])

                # 3. Multi-utterance background cancel via hold_cancel:
                #    2 voiced utterances + 1 silent utterance -> all 3 cancelled, ONLY 2 voiced WAVs stashed!
                s1_in_refine.clear()
                mode_box[0] = "scenario2"
                silence_bytes = pack_pcm16_mono_wav(b"\x00\x00" * 8000, sample_rate=16000)
                plat.wav_queue = [_voiced_wav(), _voiced_wav(), silence_bytes]
                for _ in range(3):
                    daemon._last_trigger_ts = -1e9
                    daemon.handle_trigger("toggle")
                    daemon._last_trigger_ts = -1e9
                    daemon.handle_trigger("toggle")
                self.assertTrue(s1_in_refine.wait(timeout=2.0))
                daemon._on_platform_event({"event": "hold_cancel", "hold_ms": 1600})
                self.assertEqual(daemon.state, "idle")
                self.assertEqual(len(list_failed_audios(rt)), 2)

                # 4. --seq N (via CLI 'agent-voice-input cancel --seq N') cancels stuck head #1 in 0ms
                #    and immediately releases already-finished #2 to paste!
                s1_in_refine.clear()
                s2_fast_done.clear()
                daemon.asr_provider.idx = 10  # type: ignore[attr-defined]
                mode_box[0] = "scenario3"
                plat.pasted_texts.clear()
                daemon._last_trigger_ts = -1e9
                daemon.handle_trigger("toggle")
                daemon._last_trigger_ts = -1e9
                u_slow = daemon.handle_trigger("toggle")  # "句11"
                daemon._last_trigger_ts = -1e9
                daemon.handle_trigger("toggle")
                daemon._last_trigger_ts = -1e9
                u_fast = daemon.handle_trigger("toggle")  # "句12"
                self.assertTrue(s1_in_refine.wait(timeout=2.0))
                self.assertTrue(s2_fast_done.wait(timeout=2.0))
                self.assertEqual(plat.pasted_texts, [], "#2 must wait while #1 is head of _commit_queue")

                cfg_cli = Path(td) / "cfg.toml"
                cfg_cli.write_text('[refiner]\nprovider = "off"\n', encoding="utf-8")
                buf_cli = io.StringIO()
                t_seq0 = time.monotonic()
                with (
                    unittest.mock.patch.dict(os.environ, {"AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt)}, clear=False),
                    unittest.mock.patch("sys.stdout", buf_cli),
                ):
                    rc_cli = cli_mod.main(["--config", str(cfg_cli), "cancel", "--seq", str(u_slow["seq_id"])])
                self.assertEqual(rc_cli, 0, buf_cli.getvalue())
                cli_json = json.loads(buf_cli.getvalue().strip())
                self.assertEqual(cli_json["cancelled_ids"], [u_slow["seq_id"]])

                dl_fast = time.monotonic() + 0.3
                while time.monotonic() < dl_fast and daemon.last_result.get("seq_id") != u_fast["seq_id"]:
                    time.sleep(0.01)
                self.assertLess((time.monotonic() - t_seq0) * 1000.0, 200.0)
                self.assertEqual(plat.pasted_texts, ["快句12"])
                unstoppable_hang.set()

                # 5. try_commit() CAS race: once committed inside deliver_text, concurrent cancel returns already_committed
                mode_box[0] = "scenario4"
                race_results: list[dict[str, object]] = []
                plat.commit_hook = lambda seq_val: race_results.append(daemon.handle_cancel(seq_id=seq_val))
                daemon._last_trigger_ts = -1e9
                daemon.handle_trigger("toggle")
                daemon._last_trigger_ts = -1e9
                u_commit = daemon.handle_trigger("toggle")
                dl_c = time.monotonic() + 2.0
                while time.monotonic() < dl_c and daemon.last_result.get("seq_id") != u_commit["seq_id"]:
                    time.sleep(0.01)
                self.assertTrue(daemon.last_result["ok"])
                self.assertEqual(len(race_results), 1)
                self.assertFalse(race_results[0]["ok"])
                self.assertEqual(race_results[0]["ignored"], "already_committed")
                self.assertEqual(race_results[0]["committed_ids"], [u_commit["seq_id"]])
            finally:
                s1_release.set()
                unstoppable_hang.set()
                daemon.close()
                srv_th.join(timeout=2.0)

    def test_hold_cancel_full_state_matrix_layered_yield_and_pre_paste_intercept(self) -> None:
        from agent_voice_input.audio_proc import list_failed_audios
        from agent_voice_input.http_transport import sleep_cooperative
        from agent_voice_input.refiner import BaseRefiner

        refine_entered = threading.Event()
        refine_gate = threading.Event()
        deliver_wait_entered = threading.Event()
        deliver_exited = threading.Event()
        mode_box = ["hold_refine"]

        class _MatrixPlatform(_FakePlatform):
            def __init__(self) -> None:
                super().__init__()
                self.start_count = 0
                self.cancel_rec_count = 0
                self.pasted_texts: list[str] = []

            def start_recording(self, runtime_dir: Path, *, loopback: bool = False, seq_id: int | None = None) -> None:
                _ = (loopback, seq_id)
                self.start_count += 1
                super().start_recording(runtime_dir)

            def cancel_recording(self) -> None:
                self.cancel_rec_count += 1
                super().cancel_recording()

            def stop_recording(self, drain: bool = True) -> tuple[bytes, str]:
                _ = drain
                return _voiced_wav(), "winmm"

            def deliver_text(
                self,
                text: str,
                seq_id: int,
                target_hwnd: int = 0,
                *,
                is_cancelled: object = None,
                before_clipboard_write: object = None,
                before_commit: object = None,
                **_kw: object,
            ) -> PasteOutcome:
                if callable(before_clipboard_write):
                    before_clipboard_write()
                cancelled_fn = (lambda: bool(is_cancelled())) if callable(is_cancelled) else (lambda: False)
                if mode_box[0] == "pre_paste_wait":
                    deliver_wait_entered.set()
                    # Simulate waiting in key_state release loop while user holds hotkey toward 800ms
                    sleep_cooperative(2.0, sleep_fn=time.sleep, is_cancelled=cancelled_fn)
                    deliver_exited.set()
                if cancelled_fn() or (callable(before_commit) and not before_commit()):
                    return PasteOutcome(False, False, "cancelled", "")
                self.pasted_texts.append(text)
                self.deliveries.append((text, seq_id, target_hwnd))
                return PasteOutcome(True, True, "ok", "WindowsTerminal", used_clipboard_paste=True)

        class _MatrixAsr(BaseAsrProvider):
            provider_name = "dashscope"

            def __init__(self) -> None:
                pass

            def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None, *, cancel_token: object = None, **_kw: object) -> AsrResult:
                return AsrResult(text="测试语音内容", provider="dashscope", model="qwen3-asr-flash", latency_ms=5)

        class _MatrixRefiner(BaseRefiner):
            provider_name = "codex"

            def refine(self, asr_input: object, *, cancel_token: object = None, **_kw: object) -> RefinerResult:
                raw = asr_input.text if isinstance(asr_input, AsrResult) else str(asr_input)
                if mode_box[0] == "hold_refine":
                    ev = threading.Event()
                    if cancel_token is not None:
                        getattr(cancel_token, "bind_event")(ev)
                    refine_entered.set()
                    while not ev.is_set() and not refine_gate.is_set():
                        ev.wait(timeout=0.05)
                    if cancel_token is not None and getattr(cancel_token, "is_cancelled")():
                        return RefinerResult(text=raw, provider="codex", model="gpt-6-luna", latency_ms=5, used_fallback="cancelled")
                return RefinerResult(text=f"精修:{raw}", provider="codex", model="gpt-6-luna", latency_ms=5)

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td) / "rt"
            rt.mkdir()
            plat = _MatrixPlatform()
            cfg = Config(
                hotkey=HotkeyConfig(debounce_ms=0, scope="all", yield_remote_desktop=True),
                refiner=RefinerConfig(provider="codex"),
            )
            daemon = VoiceInputDaemon(
                cfg,
                env={},
                runtime_dir=rt,
                platform=plat,
                asr_provider=_MatrixAsr(),
                refiner=_MatrixRefiner(),
                tracker=_isolated_tracker(rt),
                paste_settle_sec=0.0,
            )
            try:
                # 1) idle + hold_cancel -> completely silent, no mic open, no HUD emitted
                daemon._on_platform_event({"event": "hold_cancel", "hold_ms": 800, "process": "WindowsTerminal"})
                self.assertEqual(daemon.state, "idle")
                self.assertEqual(plat.start_count, 0)
                self.assertEqual(plat.hud_events, [])
                self.assertEqual(list_failed_audios(rt), [])

                # 2) recording only + hold_cancel (even when focus moved to yielded remote desktop mstsc!)
                #    -> active recording MUST bypass yield, cancel recording, discard audio (no failed/), show cancelled toast
                daemon.handle_trigger("toggle", ForegroundWindow(hwnd=1, process="WindowsTerminal", title="term"), from_hotkey=True)
                self.assertEqual(daemon.state, "recording")
                self.assertEqual(plat.start_count, 1)
                daemon._on_platform_event({"event": "hold_cancel", "hold_ms": 800, "process": "mstsc.exe", "title": "Remote"})
                self.assertEqual(daemon.state, "idle")
                self.assertEqual(plat.cancel_rec_count, 1)
                self.assertEqual(plat.hud_events[-1]["state"], "cancelled")
                self.assertIs(plat.hud_events[-1].get("clear_main"), True)
                self.assertEqual(list_failed_audios(rt), [])

                # 3) inflight only + hold_cancel:
                #    - first verify non-recording hold_cancel in yielded remote desktop IS yielded;
                #    - then hold_cancel in normal window cancels inflight, never opens mic, stashes voiced WAV in failed/
                refine_entered.clear()
                refine_gate.clear()
                daemon.handle_trigger("toggle", ForegroundWindow(hwnd=1, process="WindowsTerminal", title="term"), from_hotkey=True)
                daemon.handle_trigger("toggle", ForegroundWindow(hwnd=1, process="WindowsTerminal", title="term"), from_hotkey=True)
                self.assertTrue(refine_entered.wait(timeout=2.0))
                starts_before_hc = plat.start_count

                daemon._on_platform_event({"event": "hold_cancel", "hold_ms": 800, "process": "mstsc.exe", "title": "Remote"})
                self.assertEqual(daemon.state, "refining", "non-recording hold_cancel in mstsc should still yield")

                daemon._on_platform_event({"event": "hold_cancel", "hold_ms": 800, "process": "WindowsTerminal", "title": "term"})
                self.assertEqual(daemon.state, "idle")
                self.assertEqual(plat.start_count, starts_before_hc, "hold_cancel must never trigger new recording")
                self.assertEqual(plat.hud_events[-1]["state"], "cancelled")
                self.assertIs(plat.hud_events[-1].get("clear_main"), True)
                self.assertEqual(len(list_failed_audios(rt)), 1)

                # 4) recording + inflight: two consecutive hold_cancel events perform layered cancel
                refine_entered.clear()
                refine_gate.clear()
                daemon.handle_trigger("toggle", ForegroundWindow(hwnd=1, process="WindowsTerminal", title="term"), from_hotkey=True)
                daemon.handle_trigger("toggle", ForegroundWindow(hwnd=1, process="WindowsTerminal", title="term"), from_hotkey=True)
                self.assertTrue(refine_entered.wait(timeout=2.0))
                # Start back-to-back recording #2 while #1 is refining
                daemon.handle_trigger("toggle", ForegroundWindow(hwnd=1, process="WindowsTerminal", title="term"), from_hotkey=True)
                self.assertEqual(daemon.state, "recording")
                failed_before_layered = len(list_failed_audios(rt))

                # First hold_cancel cancels ONLY recording #2, restores 'refining' main pill + toast, no new failed/ WAV
                daemon._on_platform_event({"event": "hold_cancel", "hold_ms": 800, "process": "WindowsTerminal"})
                self.assertEqual(daemon.state, "refining")
                self.assertEqual(daemon._main_hud_state, "refining")
                self.assertEqual(plat.hud_events[-1]["state"], "cancelled")
                self.assertIs(plat.hud_events[-1].get("clear_main"), False)
                self.assertEqual(len(list_failed_audios(rt)), failed_before_layered)

                # Second hold_cancel cancels background refining #1, stashes its WAV in failed/, transitions to idle
                daemon._on_platform_event({"event": "hold_cancel", "hold_ms": 800, "process": "WindowsTerminal"})
                self.assertEqual(daemon.state, "idle")
                self.assertEqual(plat.hud_events[-1]["state"], "cancelled")
                self.assertIs(plat.hud_events[-1].get("clear_main"), True)
                self.assertEqual(len(list_failed_audios(rt)), failed_before_layered + 1)

                # 5) Pre-paste key-release wait intercept: hold_cancel aborts delivery immediately (<200ms)
                mode_box[0] = "pre_paste_wait"
                deliver_wait_entered.clear()
                deliver_exited.clear()
                failed_before_paste = len(list_failed_audios(rt))
                daemon.handle_trigger("toggle", ForegroundWindow(hwnd=1, process="WindowsTerminal", title="term"), from_hotkey=True)
                daemon.handle_trigger("toggle", ForegroundWindow(hwnd=1, process="WindowsTerminal", title="term"), from_hotkey=True)
                self.assertTrue(deliver_wait_entered.wait(timeout=2.0))

                t_hc0 = time.monotonic()
                daemon._on_platform_event({"event": "hold_cancel", "hold_ms": 800, "process": "WindowsTerminal"})
                self.assertTrue(deliver_exited.wait(timeout=1.0))
                self.assertLess((time.monotonic() - t_hc0) * 1000.0, 250.0)
                dl_idle = time.monotonic() + 2.0
                while time.monotonic() < dl_idle and daemon.state != "idle":
                    time.sleep(0.01)
                self.assertEqual(daemon.state, "idle")
                self.assertEqual(plat.pasted_texts, [], "intercepted utterance must never paste")
                self.assertEqual(len(list_failed_audios(rt)), failed_before_paste + 1)

                # 6) Critical-section yield atomicity: if recording finishes while hold_cancel from a yielded
                #    remote-desktop window is waiting on _trigger_lock, hold_cancel MUST yield inside the lock
                #    instead of cancelling the earlier background refining task!
                mode_box[0] = "hold_refine"
                refine_entered.clear()
                refine_gate.clear()
                daemon.handle_trigger("toggle", ForegroundWindow(hwnd=1, process="WindowsTerminal", title="term"), from_hotkey=True)
                daemon.handle_trigger("toggle", ForegroundWindow(hwnd=1, process="WindowsTerminal", title="term"), from_hotkey=True)
                self.assertTrue(refine_entered.wait(timeout=2.0))
                daemon.handle_trigger("toggle", ForegroundWindow(hwnd=1, process="WindowsTerminal", title="term"), from_hotkey=True)
                self.assertEqual(daemon.state, "recording")

                hc_started = threading.Event()
                hc_done = threading.Event()

                def _late_yield_hc() -> None:
                    hc_started.set()
                    daemon._on_platform_event({"event": "hold_cancel", "hold_ms": 800, "process": "mstsc.exe", "title": "Remote"})
                    hc_done.set()

                with daemon._trigger_lock:
                    t_hc = threading.Thread(target=_late_yield_hc, daemon=True)
                    t_hc.start()
                    self.assertTrue(hc_started.wait(timeout=1.0))
                    # Give _late_yield_hc a moment to block on _trigger_lock while _recording_active is still True
                    time.sleep(0.03)
                    # Simulate active recording auto-ending before hold_cancel enters the critical section
                    with daemon._lock:
                        daemon._recording_active = False
                        daemon._recording_seq_id = -1
                        daemon._recompute_state_locked()
                self.assertTrue(hc_done.wait(timeout=2.0))
                t_hc.join(timeout=1.0)
                self.assertEqual(daemon.state, "refining", "yielded hold_cancel must not cancel background refining after recording ended")
                # Cleanly cancel the remaining background refining task
                daemon._on_platform_event({"event": "hold_cancel", "hold_ms": 800, "process": "WindowsTerminal"})
                self.assertEqual(daemon.state, "idle")
            finally:
                refine_gate.set()
                daemon.close()

    def test_slow_background_hud_never_overwrites_recording_and_drain_hides_main_hud(self) -> None:
        from agent_voice_input.refiner import BaseRefiner

        bg_hud_entered = threading.Event()
        asr_hold = threading.Event()

        class _SlowHudPlat(_FakePlatform):
            def __init__(self) -> None:
                super().__init__()
                self.main_history: list[str] = []

            def hud(
                self,
                state: str,
                *,
                duration_ms: int = 0,
                layer: str | None = None,
                clear_main: bool | None = None,
                clear_toast: bool | None = None,
                bg_count: int = 0,
            ) -> None:
                if state in ("recognizing", "refining"):
                    bg_hud_entered.set()
                    time.sleep(0.05)
                super().hud(
                    state,
                    duration_ms=duration_ms,
                    layer=layer,
                    clear_main=clear_main,
                    clear_toast=clear_toast,
                    bg_count=bg_count,
                )
                eff_lyr = (layer or "").strip().lower()
                if eff_lyr != "toast":
                    if state in ("recording", "recognizing", "refining"):
                        self.main_history.append(state)
                    elif state in ("done", "hide", "hide_all") or clear_main is True:
                        self.main_history.append(f"clear:{state}")

        class _GateAsr(BaseAsrProvider):
            provider_name = "dashscope"

            def __init__(self) -> None:
                pass

            def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None, **_kw: object) -> AsrResult:
                asr_hold.wait(timeout=2.0)
                return AsrResult(text="防覆盖口述", provider="dashscope", model="qwen3-asr-flash", latency_ms=5)

        class _QuickRefiner(BaseRefiner):
            provider_name = "codex"

            def refine(self, asr_input: object, **_kw: object) -> RefinerResult:
                raw = asr_input.text if isinstance(asr_input, AsrResult) else str(asr_input)
                return RefinerResult(text=raw, provider="codex", model="gpt-6-luna", latency_ms=5)

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            plat = _SlowHudPlat()
            daemon = VoiceInputDaemon(
                Config(refiner=RefinerConfig(provider="codex")),
                env={},
                runtime_dir=rt,
                platform=plat,
                asr_provider=_GateAsr(),
                refiner=_QuickRefiner(),
                tracker=_isolated_tracker(rt),
                paste_settle_sec=0.0,
            )
            try:
                daemon.handle_trigger("toggle")
                daemon._last_trigger_ts = -1e9
                s1 = daemon.handle_trigger("toggle")
                self.assertTrue(bg_hud_entered.wait(timeout=2.0))

                # Start recording #2 while #1's slow 'recognizing' HUD call is mid-flight,
                # then let #1 finish refining & delivering WHILE #2 is still recording!
                daemon._last_trigger_ts = -1e9
                r_rec2 = daemon.handle_trigger("toggle")
                self.assertEqual(r_rec2["state"], "recording")
                idx_rec2 = len(plat.main_history) - 1
                self.assertEqual(plat.main_history[idx_rec2], "recording")

                asr_hold.set()
                dl = time.monotonic() + 2.0
                while time.monotonic() < dl and daemon.last_result.get("seq_id") != s1["seq_id"]:
                    time.sleep(0.02)

                # While #2 is recording, ZERO non-recording main-capsule calls (recognizing/refining/done) may overwrite it!
                # The only allowed main update is refreshing recording's bg_count badge (1 -> 0) when #1 drains.
                self.assertTrue(all(st == "recording" for st in plat.main_history[idx_rec2:]))
                self.assertEqual(daemon._main_hud_state, "recording")
                self.assertEqual(daemon._main_hud_bg_count, 0)

                # Cancel recording #2 -> drains completely to idle with main HUD hidden (zero dangling HUD)
                daemon.handle_cancel()
                self.assertEqual(daemon.state, "idle")
                self.assertFalse(daemon._main_hud_visible)
                self.assertEqual(daemon._main_hud_state, "")
                self.assertEqual(daemon._main_hud_bg_count, 0)
            finally:
                asr_hold.set()
                daemon.close()

    def test_rapid_fire_recording_bg_count_badge_refresh_and_clear_main_invariants(self) -> None:
        from agent_voice_input.refiner import BaseRefiner

        gate1 = threading.Event()
        gate2 = threading.Event()
        s2_entered = threading.Event()
        s2_second_entered = threading.Event()

        class _HudRecordPlat(_FakePlatform):
            def __init__(self) -> None:
                super().__init__()
                self.hud_log: list[tuple[str, dict[str, object]]] = []
                self.copy_next_seq: set[int] = set()

            def hud(self, state: str, text: str = "", duration_ms: int = 0, **kw: object) -> None:
                super().hud(state, text=text, duration_ms=duration_ms, **kw)
                self.hud_log.append((state, dict(kw)))

            def deliver_text(self, text: str, seq_id: int, target_hwnd: int = 0, **kw: object) -> PasteOutcome:
                if seq_id in self.copy_next_seq:
                    if callable(bc := kw.get("before_commit")) and not bc():
                        return PasteOutcome(False, False, "cancelled", "WindowsTerminal")
                    return PasteOutcome(False, True, "focus_changed", "chrome")
                return super().deliver_text(text, seq_id, target_hwnd=target_hwnd, **kw)

        class _FastAsr(BaseAsrProvider):
            provider_name = "dashscope"

            def __init__(self) -> None:
                self.n = 0

            def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None, **_kw: object) -> AsrResult:
                self.n += 1
                return AsrResult(text=f"连发句子{self.n}", provider="dashscope", model="qwen3-asr-flash", latency_ms=4)

        class _GatedRefiner(BaseRefiner):
            provider_name = "codex"

            def __init__(self) -> None:
                self.n = 0
                self._lk = threading.Lock()

            def refine(self, asr_input: object, **_kw: object) -> RefinerResult:
                with self._lk:
                    self.n += 1
                    idx = self.n
                raw = asr_input.text if isinstance(asr_input, AsrResult) else str(asr_input)
                if idx == 1:
                    s2_entered.set()
                    gate1.wait(timeout=3.0)
                elif idx == 2:
                    s2_second_entered.set()
                    gate2.wait(timeout=3.0)
                return RefinerResult(text=raw, provider="codex", model="gpt-6-luna", latency_ms=5)

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            plat = _HudRecordPlat()
            daemon = VoiceInputDaemon(
                Config(refiner=RefinerConfig(provider="codex")),
                env={},
                runtime_dir=rt,
                platform=plat,
                asr_provider=_FastAsr(),
                refiner=_GatedRefiner(),
                tracker=_isolated_tracker(rt),
                paste_settle_sec=0.0,
            )
            try:
                # 1. Start & stop #1 (enters Stage 2 refiner and blocks on gate1)
                daemon.handle_trigger("toggle")
                self.assertEqual(plat.hud_log[-1], ("recording", {"clear_toast": True}))
                daemon._last_trigger_ts = -1e9
                s1 = daemon.handle_trigger("toggle")
                self.assertTrue(s2_entered.wait(timeout=2.0))

                # 2. Start & stop #2 while #1 is in-flight -> recording receives bg_count=1
                daemon._last_trigger_ts = -1e9
                daemon.handle_trigger("toggle")
                self.assertEqual(plat.hud_log[-1], ("recording", {"clear_toast": True, "bg_count": 1}))
                self.assertEqual(daemon._main_hud_bg_count, 1)
                daemon._last_trigger_ts = -1e9
                s2 = daemon.handle_trigger("toggle")
                plat.copy_next_seq.add(int(s2["seq_id"]))
                self.assertTrue(s2_second_entered.wait(timeout=2.0))

                # 3. Start #3 while BOTH #1 & #2 are in-flight -> recording receives bg_count=2
                daemon._last_trigger_ts = -1e9
                daemon.handle_trigger("toggle")
                self.assertEqual(plat.hud_log[-1], ("recording", {"clear_toast": True, "bg_count": 2}))
                self.assertEqual(daemon._main_hud_bg_count, 2)
                log_mark = len(plat.hud_log)

                # Release #1 while #3 is still recording -> refreshes bg_count 2 -> 1 with clear_toast=False
                gate1.set()
                dl = time.monotonic() + 2.0
                while time.monotonic() < dl and daemon.last_result.get("seq_id") != s1["seq_id"]:
                    time.sleep(0.02)
                self.assertEqual(daemon._main_hud_bg_count, 1)
                self.assertIn(("recording", {"clear_toast": False, "bg_count": 1}), plat.hud_log[log_mark:])

                # Release #2 (focus-switched -> copied toast) while #3 is still recording:
                # refreshes recording badge 1 -> 0 (clear_toast=False) BEFORE emitting copied toast (clear_main=False)!
                log_mark2 = len(plat.hud_log)
                gate2.set()
                dl = time.monotonic() + 2.0
                while time.monotonic() < dl and daemon.last_result.get("seq_id") != s2["seq_id"]:
                    time.sleep(0.02)
                self.assertEqual(daemon._main_hud_bg_count, 0)
                tail2 = plat.hud_log[log_mark2:]
                self.assertEqual(
                    tail2,
                    [
                        ("recording", {"clear_toast": False}),
                        ("copied", {"layer": "toast", "clear_main": False}),
                    ],
                )

                # 4. Cancel recording #3 when zero background tasks remain -> cancelled toast sets clear_main=True
                log_mark3 = len(plat.hud_log)
                res_c3 = daemon.handle_cancel()
                self.assertTrue(res_c3["ok"])
                self.assertEqual(daemon.state, "idle")
                self.assertFalse(daemon._main_hud_visible)
                self.assertEqual(plat.hud_log[log_mark3:], [("cancelled", {"clear_main": True})])

                # 5. Cancel background task while a new recording is active -> refreshes bg_count & keeps recording main
                gate1.clear()
                s2_entered.clear()
                gate3 = threading.Event()
                # Re-gate next refine by replacing refiner with a fresh gated refiner
                class _BlockRefiner(BaseRefiner):
                    provider_name = "codex"

                    def refine(self, asr_input: object, *, cancel_token: object = None, **_kw: object) -> RefinerResult:
                        s2_entered.set()
                        gate3.wait(timeout=2.0)
                        raw = asr_input.text if isinstance(asr_input, AsrResult) else str(asr_input)
                        return RefinerResult(text=raw, provider="codex", model="gpt-6-luna", latency_ms=5)

                daemon.refiner = _BlockRefiner()
                daemon._last_trigger_ts = -1e9
                daemon.handle_trigger("toggle")
                daemon._last_trigger_ts = -1e9
                s4 = daemon.handle_trigger("toggle")
                self.assertTrue(s2_entered.wait(timeout=2.0))
                daemon._last_trigger_ts = -1e9
                daemon.handle_trigger("toggle")
                self.assertEqual(plat.hud_log[-1], ("recording", {"clear_toast": True, "bg_count": 1}))
                log_mark4 = len(plat.hud_log)
                res_c4 = daemon.handle_cancel(seq_id=int(s4["seq_id"]))
                gate3.set()
                self.assertTrue(res_c4["ok"])
                self.assertEqual(daemon._main_hud_bg_count, 0)
                self.assertEqual(
                    plat.hud_log[log_mark4:],
                    [
                        ("recording", {"clear_toast": False}),
                        ("cancelled", {"clear_main": False}),
                    ],
                )
                daemon.handle_cancel()
                self.assertEqual(daemon.state, "idle")
                self.assertFalse(daemon._main_hud_visible)
            finally:
                gate1.set()
                gate2.set()
                daemon.close()

    def test_recorder_lost_stale_bg_snapshot_does_not_resurrect_permanent_main_hud(self) -> None:
        """If the last visible background task finishes after _on_recorder_lost releases _lock
        (e.g. during telemetry write), HUD emission MUST re-read state under _hud_lock and
        NEVER re-emit a stale recognizing/refining main pill."""
        from agent_voice_input.refiner import BaseRefiner

        voiced = _voiced_wav()
        s1_in_asr = threading.Event()
        s1_release = threading.Event()

        class _GatedAsr(BaseAsrProvider):
            provider_name = "mock_asr"

            def __init__(self) -> None:
                pass

            def transcribe(self, wav_bytes: bytes, context: object = None, **_kw: object) -> AsrResult:
                s1_in_asr.set()
                s1_release.wait(timeout=3.0)
                return AsrResult(text="first sentence", provider="mock_asr", latency_ms=10)

        class _PassRefiner(BaseRefiner):
            provider_name = "codex"

            def refine(self, asr_input: object, **_kw: object) -> RefinerResult:
                raw = asr_input.text if isinstance(asr_input, AsrResult) else str(asr_input)
                return RefinerResult(text=raw, provider="codex", model="gpt-6-luna", latency_ms=5)

        class _LostPlat(_FakePlatform):
            def __init__(self) -> None:
                super().__init__()
                self.hud_log: list[tuple[str, dict[str, Any]]] = []
                self.stop_calls = 0

            def hud(self, state: str, text: str = "", duration_ms: int = 0, **kw: Any) -> None:
                super().hud(state, text=text, duration_ms=duration_ms, **kw)
                self.hud_log.append((state, dict(kw)))

            def stop_recording(self, drain: bool = True) -> tuple[bytes, str]:
                self.stop_calls += 1
                if self.stop_calls == 1:
                    return voiced, "mock"
                return b"", "mock"

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            plat = _LostPlat()
            daemon = VoiceInputDaemon(
                Config(refiner=RefinerConfig(provider="codex")),
                env={},
                runtime_dir=rt,
                platform=plat,
                asr_provider=_GatedAsr(),
                refiner=_PassRefiner(),
                tracker=_isolated_tracker(rt),
                paste_settle_sec=0.0,
            )
            orig_append = daemon._append_telemetry
            hooked = [False]

            def _racing_append(rec: dict[str, Any]) -> None:
                orig_append(rec)
                if not hooked[0] and rec.get("skipped_reason") == "mic_disconnected":
                    hooked[0] = True
                    # Utterance #1 finishes RIGHT HERE between releasing _lock and emitting error HUD!
                    s1_release.set()
                    deadline = time.monotonic() + 2.0
                    while time.monotonic() < deadline and daemon._visible_inflight_count_locked() > 0:
                        time.sleep(0.01)

            daemon._append_telemetry = _racing_append  # type: ignore[method-assign]
            try:
                daemon.handle_trigger("toggle")
                daemon._last_trigger_ts = -1e9
                s1 = daemon.handle_trigger("toggle")
                self.assertTrue(s1["ok"])
                self.assertTrue(s1_in_asr.wait(timeout=2.0))

                daemon._last_trigger_ts = -1e9
                s2 = daemon.handle_trigger("toggle")
                self.assertTrue(s2["ok"])
                seq2 = int(s2["seq_id"])

                mark = len(plat.hud_log)
                daemon._on_recorder_lost(seq_id=seq2)

                tail = plat.hud_log[mark:]
                # Must NOT resurrect recognizing/refining after #1 finished; mic_disconnected clears main!
                disc_idx = next(i for i, (st, _) in enumerate(tail) if st == "mic_disconnected")
                self.assertEqual(tail[disc_idx][1].get("clear_main"), True)
                self.assertNotIn("recognizing", [st for st, _ in tail[disc_idx:]])
                self.assertNotIn("refining", [st for st, _ in tail[disc_idx:]])
                self.assertFalse(daemon._main_hud_visible)
                self.assertEqual(daemon.state, "idle")
            finally:
                s1_release.set()
                daemon.close()

    def test_silent_task_coexisting_with_visible_task_preserves_visible_main_hud_on_error_and_cancel(self) -> None:
        """When a silent_hud task is in refining (global state=refining) while a visible task is in
        recognizing (visible_only=recognizing), stop-error and cancel HUD paths MUST preserve the
        visible task's main pill instead of clearing it due to global_state != visible_stage."""
        from agent_voice_input.daemon import _CommitSlot, _QueuedUtterance, _Stage1State
        from agent_voice_input.platform_common import MicrophoneUnavailableError
        from agent_voice_input.refiner import BaseRefiner

        voiced = _voiced_wav()

        class _ErrStopPlat(_FakePlatform):
            def __init__(self) -> None:
                super().__init__()
                self.hud_log: list[tuple[str, dict[str, Any]]] = []

            def hud(self, state: str, text: str = "", duration_ms: int = 0, **kw: Any) -> None:
                super().hud(state, text=text, duration_ms=duration_ms, **kw)
                self.hud_log.append((state, dict(kw)))

            def stop_recording(self, drain: bool = True) -> tuple[bytes, str]:
                raise MicrophoneUnavailableError("device busy", reason_code="mic_unavailable", hud_state="mic_unavailable")

        class _NoopAsr(BaseAsrProvider):
            provider_name = "mock_asr"

            def __init__(self) -> None:
                pass

            def transcribe(self, wav_bytes: bytes, context: object = None, **_kw: object) -> AsrResult:
                return AsrResult(text="ok", provider="mock_asr", latency_ms=1)

        class _NoopRefiner(BaseRefiner):
            provider_name = "codex"

            def refine(self, asr_input: object, **_kw: object) -> RefinerResult:
                return RefinerResult(text="ok", provider="codex", model="gpt-6-luna", latency_ms=1)

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            plat = _ErrStopPlat()
            daemon = VoiceInputDaemon(
                Config(refiner=RefinerConfig(provider="codex")),
                env={},
                runtime_dir=rt,
                platform=plat,
                asr_provider=_NoopAsr(),
                refiner=_NoopRefiner(),
                tracker=_isolated_tracker(rt),
                paste_settle_sec=0.0,
            )
            try:
                # Seed a silent task in refining + visible tasks in recognizing (global != visible)
                silent_task = _QueuedUtterance(
                    seq_id=101,
                    raw_wav=voiced,
                    audio_source="mock",
                    origin_win=ForegroundWindow(1, "WindowsTerminal.exe", "term"),
                    no_paste=False,
                    silent_hud=True,
                )
                silent_slot = _CommitSlot(task=silent_task, st=_Stage1State(t_total0=0.0), stage="refining")
                vis_task = _QueuedUtterance(
                    seq_id=102,
                    raw_wav=voiced,
                    audio_source="mock",
                    origin_win=ForegroundWindow(1, "WindowsTerminal.exe", "term"),
                    no_paste=False,
                    silent_hud=False,
                )
                extra_vis = _QueuedUtterance(
                    seq_id=103,
                    raw_wav=voiced,
                    audio_source="mock",
                    origin_win=ForegroundWindow(1, "WindowsTerminal.exe", "term"),
                    no_paste=False,
                    silent_hud=False,
                )
                with daemon._lock:
                    daemon._commit_queue.append(silent_slot)
                    daemon._utterance_queue.append(vis_task)
                    daemon._utterance_queue.append(extra_vis)
                    daemon.seq_id = 103
                    daemon._main_hud_state = "recognizing"
                    daemon._main_hud_visible = True
                    self.assertEqual(daemon._recompute_state_locked(), "refining")
                    self.assertEqual(daemon._derive_bg_stage_locked(visible_only=True), "recognizing")

                # 1. Start + failing stop_recording must NOT emit clear_main=True while vis_task is active
                daemon._last_trigger_ts = -1e9
                r_start = daemon.handle_trigger("toggle")
                self.assertTrue(r_start["ok"])
                plat.hud_log.clear()
                daemon._last_trigger_ts = -1e9
                r_stop = daemon.handle_trigger("toggle")
                self.assertFalse(r_stop["ok"])
                self.assertEqual(r_stop["state"], "refining")
                self.assertTrue(daemon._main_hud_visible)
                self.assertEqual(daemon._main_hud_state, "recognizing")
                # First HUD event on stop MUST derive from visible tasks (recognizing), NEVER leak silent task's refining!
                self.assertEqual(
                    plat.hud_log,
                    [("recognizing", {}), ("mic_unavailable", {"clear_main": False})],
                )

                # 2. Cancelling extra_vis while vis_task & silent_task remain must keep vis_task main pill
                plat.hud_log.clear()
                r_can = daemon.handle_cancel(seq_id=103)
                self.assertTrue(r_can["ok"])
                self.assertEqual(r_can["state"], "refining")
                self.assertTrue(daemon._main_hud_visible)
                self.assertEqual(daemon._main_hud_state, "recognizing")
                self.assertEqual(plat.hud_log, [("cancelled", {"clear_main": False})])
            finally:
                with daemon._lock:
                    daemon._commit_queue.clear()
                    daemon._utterance_queue.clear()
                daemon.close()

    def test_win_shim_dll_cache_and_clip_guard_static_contracts(self) -> None:
        win_sources = list_win_shim_sources()
        self.assertGreaterEqual(len(win_sources), 7)
        for src_path in win_sources:
            raw_bytes = src_path.read_bytes()
            self.assertFalse(raw_bytes.startswith(b"\xef\xbb\xbf"), f"{src_path.name} must have no UTF-8 BOM")
            self.assertTrue(all(b < 128 for b in raw_bytes), f"{src_path.name} must stay strictly 7-bit ASCII")
            self.assertNotIn(b"\r", raw_bytes, f"{src_path.name} must use LF line endings")
        shim = read_win_shim_source()
        with tempfile.TemporaryDirectory() as empty_td:
            missing_ps1 = Path(empty_td) / "missing_win_shim.ps1"
            self.assertEqual(list_win_shim_sources(missing_ps1), [])
            with self.assertRaises(FileNotFoundError):
                read_win_shim_source(missing_ps1)
        for required_tok in (
            "CSharpCodeProvider",
            "CompileAssemblyFromSource",
            "VoiceWinShim_",
            ".tmp.dll",
            "[System.IO.File]::Move",
            "IsHungAppWindow",
            "CreateWindowEx",
            "new IntPtr(-3)",
            "ExcludeClipboardContentFromMonitorProcessing",
            "CanIncludeInClipboardHistory",
            "CanUploadToCloudClipboard",
            "ClipboardViewerIgnore",
            "Clipboard Viewer Ignore",
            "AgentVoiceInputTransient",
            "AgentVoiceInputRestored",
            "CancelPendingClipRestore",
            "WriteClipTextOnce",
            "FlushPendingClipRestore",
            "Set-ClipboardTransient",
            "RunClipStateSelfCheck",
            '$Mode -eq "clip-self-check"',
            "MaxClipSnapshotBytes = 10 * 1024 * 1024",
            "IsDeadlineExceeded",
            "paste_deadline_exceeded",
            "deadline_ms",
            "store.HasFormatWhenOpen(CF_DIBV5) ? CF_DIBV5 : CF_DIB",
            "step8 read exception refused voice paste",
            'reason = $(if ($clipOk) { "hotkey_held" } else { Get-ClipFailReason',
        ):
            self.assertIn(required_tok, shim, msg=required_tok)
        for forbidden_tok in (
            ".CompiledAssembly",
            "GetText",
            "QueueUserWorkItem",
            "$fbOk",
            "Set-ClipboardTransient $fbTxt",
        ):
            self.assertNotIn(forbidden_tok, shim, msg=forbidden_tok)
        # In WriteClipTextCore, both branches check deadline after EmptyWhenOpen() and before SetFormatBytesWhenOpen();
        # in the transient branch, pending restore timer must be stopped BEFORE EmptyWhenOpen().
        wc_block = shim.split("private static bool WriteClipTextCore(", 1)[1].split("private static void FlushClipRestoreCore(", 1)[0]
        persist_part, transient_part = wc_block.split("bool preHung = store.IsOwnerHung();", 1)
        self.assertLess(
            transient_part.index("if (stopTimerFn != null) stopTimerFn();"),
            transient_part.index("store.EmptyWhenOpen()"),
        )
        self.assertIn("nextSnap = ClipSnap.Unrestorable;", transient_part)
        for branch_name, branch_src in (("persistent", persist_part), ("transient", transient_part)):
            after_empty = branch_src.split("store.EmptyWhenOpen()", 1)[1]
            before_set = after_empty.split("store.SetFormatBytesWhenOpen(", 1)[0]
            self.assertIn("isDeadlineExceededFn()", before_set, msg=f"{branch_name} branch missing post-EmptyWhenOpen deadline check")
        # Read-only cache directory CS0016 write failures must fall back to uncached Add-Type without masking real C# errors.
        ps1_text = SHIM_SCRIPT.read_text(encoding="ascii")
        compile_err_block = ps1_text.split("if ($cr.Errors.HasErrors) {", 1)[1].split("} elseif (Test-Path -LiteralPath $tmpDll) {", 1)[0]
        self.assertIn('"CS0016"', compile_err_block)
        self.assertIn('"CS1567"', compile_err_block)
        self.assertIn("$onlyOutputWriteErr", compile_err_block)
        cs0016_branch, real_err_branch = compile_err_block.split("if ($hasNonWarningErr -and $onlyOutputWriteErr) {", 1)[1].split("} else {", 1)
        self.assertIn("falling back to uncached Add-Type", cs0016_branch)
        self.assertNotIn("$csCompileError =", cs0016_branch)
        self.assertIn("$csCompileError =", real_err_branch)
        self.assertIn('Write-InitFailureAndExit ("C# compile failed: " + $csCompileError)', ps1_text)
        self.assertIn("Add-Type -ReferencedAssemblies System.Windows.Forms, System.Drawing -TypeDefinition $mergedCsSource", ps1_text)

    def test_jsonl_shim_unready_hang_reap_backoff_pulse_fallback_and_self_heal(self) -> None:
        import subprocess
        from unittest import mock
        from agent_voice_input.config import AudioConfig
        from agent_voice_input.platform_wsl import WslPlatform

        # 1. Concurrent _ensure_shim() callers coalesce into 1 Popen and ALL wait until 'ready'
        slow_ready_shim = (
            "import json, sys, time\n"
            "time.sleep(0.08)\n"
            "sys.stdout.write(json.dumps({'event': 'ready', 'ui_culture': 'zh-CN'}) + '\\n'); sys.stdout.flush()\n"
            "for ln in sys.stdin:\n"
            "    req = json.loads(ln)\n"
            "    if req.get('cmd') == 'quit': break\n"
            "    sys.stdout.write(json.dumps({'type': 'reply', 'reply': req['cmd'], 'seq_id': req['seq_id'], 'ok': True}) + '\\n')\n"
            "    sys.stdout.flush()\n"
        )
        conc_spawns = [0]
        conc_lock = threading.Lock()

        def conc_factory() -> list[str]:
            with conc_lock:
                conc_spawns[0] += 1
            return [sys.executable, "-u", "-c", slow_ready_shim]

        plat_conc = WslPlatform(Config(), env={}, shim_cmd_factory=conc_factory)
        try:
            barrier = threading.Barrier(5)
            results: list[tuple[bool, bool]] = []
            res_lock = threading.Lock()

            def _worker() -> None:
                barrier.wait(timeout=2.0)
                ok = plat_conc._ensure_shim()
                with res_lock:
                    results.append((ok, plat_conc._spawned_ready))

            threads = [threading.Thread(target=_worker) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=3.0)
            self.assertEqual(conc_spawns[0], 1)
            self.assertEqual(results, [(True, True)] * 5)
        finally:
            plat_conc.close()

        # 2. Hung unready process is killed & reaped, enters exponential backoff (1->2->4->8s),
        #    cooldown fast-fails HUD & falls back wsl_source='auto' to pulse in <150ms, then self-heals!
        hung_then_healthy = (
            "import json, sys, time\n"
            "mode = sys.argv[1]\n"
            "if mode == 'hang':\n"
            "    time.sleep(30.0)\n"
            "    sys.exit(0)\n"
            "sys.stdout.write(json.dumps({'event': 'ready'}) + '\\n')\n"
            "sys.stdout.write(json.dumps({'event': 'toggle', 'hwnd': 501, 'process': 'WindowsTerminal', 'title': 'healed'}) + '\\n')\n"
            "sys.stdout.flush()\n"
            "for ln in sys.stdin:\n"
            "    req = json.loads(ln)\n"
            "    if req.get('cmd') == 'quit': break\n"
            "    sys.stdout.write(json.dumps({'type': 'reply', 'reply': req['cmd'], 'seq_id': req['seq_id'], 'ok': True}) + '\\n')\n"
            "    sys.stdout.flush()\n"
        )
        shim_mode = ["hang"]
        spawned_procs: list[subprocess.Popen[str]] = []
        orig_popen = subprocess.Popen

        def tracking_popen(*args: Any, **kwargs: Any) -> Any:
            p = orig_popen(*args, **kwargs)
            if args and isinstance(args[0], list) and len(args[0]) >= 4 and args[0][-1] in ("hang", "healthy"):
                spawned_procs.append(p)
            return p

        fake_pcm = (
            "import sys, time\n"
            "sys.stdout.buffer.write(b'\\x10\\x00' * 800)\n"
            "sys.stdout.buffer.flush()\n"
            "time.sleep(5.0)\n"
        )
        with tempfile.TemporaryDirectory() as td, mock.patch(
            "agent_voice_input.platform_common.subprocess.Popen", side_effect=tracking_popen
        ):
            rt = Path(td)
            plat_hang = WslPlatform(
                Config(audio=AudioConfig(wsl_source="auto")),
                env={},
                shim_cmd_factory=lambda: [sys.executable, "-u", "-c", hung_then_healthy, shim_mode[0]],
            )
            plat_hang._primary_winmm = True
            plat_hang._use_winmm = True
            plat_hang._shim_ready_timeout = 0.12
            plat_hang._ff_rec._cmd_factory = lambda _f, _s: [sys.executable, "-u", "-c", fake_pcm]
            try:
                ok_hang = plat_hang._ensure_shim()
                self.assertFalse(ok_hang)
                self.assertIsNone(plat_hang._shim)
                self.assertEqual(plat_hang._shim_fail_count, 1)
                self.assertEqual(len(spawned_procs), 1)
                self.assertIsNotNone(spawned_procs[0].poll(), "hung unready shim process must be killed & reaped")
                rem1 = plat_hang._shim_retry_after - time.monotonic()
                self.assertGreater(rem1, 0.5)
                self.assertLessEqual(rem1, 1.05)

                # Verify exponential backoff progression 1s -> 2s -> 4s -> 8s -> 8s (via ignore_cooldown)
                expected_backoffs = [2.0, 4.0, 8.0, 8.0]
                for exp_b in expected_backoffs:
                    self.assertFalse(plat_hang._ensure_shim(ignore_cooldown=True))
                    rem_b = plat_hang._shim_retry_after - time.monotonic()
                    self.assertGreater(rem_b, exp_b - 0.35)
                    self.assertLessEqual(rem_b, exp_b + 0.1)

                # During cooldown: _ensure_shim() and hud() return in < 30ms without spawning,
                # and start_recording(wsl_source='auto') smoothly falls back to pulse without stalling!
                spawns_before_cd = len(spawned_procs)
                t_cd0 = time.monotonic()
                self.assertFalse(plat_hang._ensure_shim())
                plat_hang.hud("recording")
                self.assertLess(time.monotonic() - t_cd0, 0.05)
                self.assertEqual(len(spawned_procs), spawns_before_cd)

                t_rec0 = time.monotonic()
                plat_hang.start_recording(rt, seq_id=1)
                self.assertLess(time.monotonic() - t_rec0, 0.4)
                self.assertEqual(plat_hang._rec_mode, "pulse")
                wav_fb, mode_fb = plat_hang.stop_recording(drain=False)
                self.assertEqual(mode_fb, "pulse")
                self.assertGreater(len(wav_fb), 44)

                # Expire cooldown & switch to healthy shim -> automatic recovery of events + RPC!
                healed_evts: list[dict[str, Any]] = []
                healed_done = threading.Event()
                plat_hang._on_event = lambda m: (healed_evts.append(m), healed_done.set())
                plat_hang._ensure_event_worker()
                shim_mode[0] = "healthy"
                plat_hang._shim_retry_after = 0.0
                r_ping = plat_hang._shim_rpc({"cmd": "ping"}, timeout_sec=2.0) or {}
                self.assertTrue(r_ping.get("ok"))
                self.assertEqual(plat_hang._shim_fail_count, 0)
                self.assertEqual(plat_hang.last_shim_error, "")
                self.assertTrue(healed_done.wait(timeout=2.0))
                self.assertEqual(healed_evts[0]["hwnd"], 501)
            finally:
                plat_hang.close()

        # 3. Bounded 5-attempt _respawn_shim_bg stops spawning after 5 consecutive failures (no infinite loop)
        bound_spawns = [0]

        def always_fail_factory() -> list[str]:
            bound_spawns[0] += 1
            return [sys.executable, "-u", "-c", "import sys; sys.exit(1)"]

        plat_bound = WslPlatform(Config(), env={}, shim_cmd_factory=always_fail_factory)
        plat_bound._shim_ready_timeout = 0.1
        plat_bound._shim_respawn_delays = (0.02, 0.02, 0.02, 0.02, 0.02)
        try:
            plat_bound.start_event_listener(lambda _e: None)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and (bound_spawns[0] < 6 or plat_bound._respawn_scheduled):
                time.sleep(0.02)
            # 1 initial attempt + 5 bounded background retries = exact 6 spawns
            self.assertEqual(bound_spawns[0], 6)
            self.assertFalse(plat_bound._respawn_scheduled)
            time.sleep(0.12)
            self.assertEqual(bound_spawns[0], 6)
        finally:
            plat_bound.close()

        # 4. Late/zombie events from an old managed shim process are strictly dropped
        import io
        plat_stale = WslPlatform(Config(), env={})
        stale_evts: list[dict[str, Any]] = []
        plat_stale._on_event = lambda m: stale_evts.append(m)
        plat_stale._ensure_event_worker()
        try:
            class _ZombieShim:
                def __init__(self, lines: str) -> None:
                    self.stdin = io.StringIO()
                    self.stdout = io.StringIO(lines)

                def poll(self) -> int | None:
                    return 0

                def kill(self) -> None:
                    pass

                def wait(self, timeout: float | None = None) -> int:
                    return 0

            zombie = _ZombieShim(
                json.dumps({"event": "ready", "ui_culture": "fr-FR", "caps": ["paste_clip"]}) + "\n"
                + json.dumps({"event": "toggle", "hwnd": 999, "process": "ZombieTerm", "title": "ghost"}) + "\n"
            )
            # Current self._shim is None (already reaped on timeout), while managed_spawn (proc_ready != None) flushes late buffer
            plat_stale._shim_reader(zombie, threading.Event())  # type: ignore[arg-type]
            plat_stale._flush_shim_events()
            self.assertEqual(stale_evts, [])
            self.assertEqual(plat_stale._win_ui_culture, "")
            self.assertEqual(plat_stale._shim_caps, frozenset())
        finally:
            plat_stale.close()

        # 5. Graceful _ensure_shim early-exit (empty cmd / Popen OSError) clears _spawning_shim so restart works
        from agent_voice_input.platform_macos import MacOSPlatform
        graceful_mode = ["empty"]
        graceful_calls = [0]

        def graceful_factory() -> list[str] | None:
            graceful_calls[0] += 1
            if graceful_mode[0] == "empty":
                return None
            if graceful_mode[0] == "oserror":
                return ["/nonexistent/avi_missing_shim_binary"]
            return [sys.executable, "-u", "-c", slow_ready_shim]

        plat_grace = MacOSPlatform(Config(), env={}, shim_cmd_factory=graceful_factory)
        try:
            plat_grace.start_event_listener(lambda _e: None)
            self.assertFalse(plat_grace._spawning_shim)
            self.assertTrue(plat_grace._shim_disabled)
            plat_grace.close()

            graceful_mode[0] = "oserror"
            plat_grace.start_event_listener(lambda _e: None)
            self.assertFalse(plat_grace._spawning_shim)
            self.assertTrue(plat_grace._shim_disabled)
            plat_grace.close()

            graceful_mode[0] = "ok"
            plat_grace.start_event_listener(lambda _e: None)
            self.assertTrue(plat_grace._spawned_ready)
            self.assertEqual(graceful_calls[0], 3)
        finally:
            plat_grace.close()

        # 6. Ready shim freezing with full stdin pipe never deadlocks close() against concurrent _shim_rpc
        frozen_stdin_shim = (
            "import json, sys, time\n"
            "sys.stdout.write(json.dumps({'event': 'ready'}) + '\\n'); sys.stdout.flush()\n"
            "time.sleep(30.0)\n"
        )
        plat_frozen = WslPlatform(
            Config(),
            env={},
            shim_cmd_factory=lambda: [sys.executable, "-u", "-c", frozen_stdin_shim],
        )
        try:
            self.assertTrue(plat_frozen._ensure_shim())
            big_blob = "X" * (512 * 1024)
            rpc_finished = threading.Event()

            def _flood_rpc() -> None:
                try:
                    for _ in range(8):
                        plat_frozen._shim_rpc({"cmd": "hud", "text": big_blob}, timeout_sec=0.3, allow_retry=False)
                except Exception:
                    pass
                finally:
                    rpc_finished.set()

            flood_t = threading.Thread(target=_flood_rpc, daemon=True)
            flood_t.start()
            time.sleep(0.08)
            t_close0 = time.monotonic()
            plat_frozen.close()
            self.assertLess(time.monotonic() - t_close0, 1.5)
            self.assertTrue(rpc_finished.wait(timeout=1.5))
        finally:
            plat_frozen.close()


class ControlProbeAndSelftestWavTests(unittest.TestCase):
    @staticmethod
    def _write_codex_rollout(
        path: Path,
        sid: str,
        cwd: str,
        turns: list[tuple[str, str]],
        *,
        source: object = "cli",
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
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": role,
                            "content": [
                                {
                                    "type": "input_text" if role == "user" else "output_text",
                                    "text": text,
                                }
                            ],
                        },
                    },
                    ensure_ascii=False,
                )
            )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_selftest_wav_no_telemetry_dry_run_redline_and_routing_mask(self) -> None:
        from agent_voice_input.audio_proc import get_failed_audio_dir
        from agent_voice_input.config import AudioConfig
        from agent_voice_input.refiner import BaseRefiner, build_refiner_context_chunks

        class _CtxTrackingRefiner(BaseRefiner):
            provider_name = "deepseek"

            def __init__(self, cfg: Config) -> None:
                self._cfg = cfg

            def refine(
                self,
                asr: AsrResult,
                *,
                session_key: str = "",
                tracker: object = None,
                fallback_context: str = "",
                mode: str = "agent",
                workspace_cwd: str = "",
                target_hint: str = "",
                **kw: object,
            ) -> RefinerResult:
                del target_hint, kw
                blocks = (
                    []
                    if mode == "general"
                    else build_refiner_context_chunks(
                        self._cfg,
                        session_key=session_key,
                        tracker=tracker,  # type: ignore[arg-type]
                        fallback_context=fallback_context,
                        workspace_cwd=workspace_cwd,
                    )[0]
                )
                return RefinerResult(
                    text=f"refined:{asr.text}",
                    provider="deepseek",
                    model="deepseek-chat",
                    latency_ms=3,
                    context_chars=sum(len(b) for b in blocks),
                    context_chunks=len(blocks),
                )

            def close(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "private_home"
            rt = tmp / "rt"
            cache = tmp / "cache"
            repo = home / "secret_parent" / "alpha-repo"
            repo.mkdir(parents=True)
            rt.mkdir(mode=0o700)
            cache.mkdir(mode=0o700)

            full_sid = "abcd1234-5678-90ef-1111-222233334444"
            rollout = self._write_codex_rollout(
                home / ".codex" / "sessions" / "2026" / "11" / "23" / f"rollout-2026-11-23T10-00-00-{full_sid}.jsonl",
                full_sid,
                str(repo),
                [
                    ("user", "请排查 PRIVATE_SELFTEST_DIALOG_5511 的路由问题"),
                    ("assistant", "已确认 alpha-repo 上下文"),
                ],
            )
            write_session_registry_entry(
                rt,
                cli="codex",
                session_id=full_sid,
                cwd=str(repo),
                jsonl_path=str(rollout),
                pid=os.getpid(),
            )

            env = {
                "HOME": str(home),
                "XDG_CACHE_HOME": str(cache),
                "XDG_RUNTIME_DIR": str(rt),
            }
            cfg = Config(
                audio=AudioConfig(save_recordings=True),
                refiner=RefinerConfig(provider="deepseek"),
            )
            plat = _FakePlatform()
            tracker = SessionTracker(home_dir=home, runtime_dir=rt, proc_root=tmp / "empty_proc", env=env)
            daemon = VoiceInputDaemon(
                cfg,
                env=env,
                runtime_dir=rt,
                platform=plat,
                asr_provider=_FakeAsr("原始语音文本"),
                refiner=_CtxTrackingRefiner(cfg),
                tracker=tracker,
            )
            try:
                wav_file = tmp / "sample.wav"
                wav_file.write_bytes(_voiced_wav())

                # 1. no_telemetry=True + hostile dry_run=False / no_paste=False attempt
                resp = daemon.handle_control(
                    {
                        "cmd": "selftest_wav",
                        "wav_path": str(wav_file),
                        "process": "WindowsTerminal.exe",
                        "title": "codex [#abcd1234]",
                        "no_telemetry": True,
                        "dry_run": False,
                        "no_paste": False,
                    }
                )
                self.assertTrue(resp["ok"], resp)
                self.assertEqual(resp["seq_id"], 0)
                self.assertEqual(resp["final_text"], "refined:原始语音文本")
                # Zero telemetry / state pollution
                self.assertEqual(daemon.seq_id, 0)
                self.assertEqual(daemon.last_result, {})
                self.assertEqual(list(daemon.recent_results), [])
                self.assertFalse(daemon.telemetry_path.exists())
                # Dry-run redline enforced: zero paste, HUD, beep, failed/, recordings/
                self.assertEqual(plat.deliveries, [])
                self.assertEqual(plat.huds, [])
                self.assertEqual(plat.beeps, [])
                self.assertFalse(get_failed_audio_dir(rt, create=False).exists())
                rec_dir = cache / "agent-voice-input" / "recordings"
                self.assertEqual(list(rec_dir.glob("*")) if rec_dir.exists() else [], [])

                # Masked routing summary: no full path, no full UUID, no dialog text
                routing = resp["routing"]
                self.assertEqual(routing["mode"], "agent")
                self.assertEqual(routing["session_key_masked"], "codex:abcd1234")
                self.assertEqual(routing["workspace_name"], "alpha-repo")
                self.assertGreater(routing["ctx_chars"], 0)
                self.assertGreaterEqual(routing["ctx_chunks"], 1)
                self.assertEqual(routing["ctx_fallback"], "")
                routing_json = json.dumps(routing, ensure_ascii=False)
                self.assertNotIn("secret_parent", routing_json)
                self.assertNotIn(str(repo), routing_json)
                self.assertNotIn(full_sid, routing_json)
                self.assertNotIn("PRIVATE_SELFTEST_DIALOG_5511", routing_json)

                # 2. Default no_telemetry=False stays backwards compatible
                resp_legacy = daemon.handle_control(
                    {
                        "cmd": "selftest_wav",
                        "wav_path": str(wav_file),
                        "process": "WindowsTerminal.exe",
                        "title": "codex [#abcd1234]",
                    }
                )
                self.assertTrue(resp_legacy["ok"], resp_legacy)
                self.assertEqual(resp_legacy["seq_id"], 1)
                self.assertEqual(daemon.seq_id, 1)
                self.assertEqual(daemon.last_result.get("seq_id"), 1)
                self.assertEqual(len(daemon.recent_results), 1)
                self.assertTrue(daemon.telemetry_path.is_file())
                self.assertEqual(resp_legacy["routing"]["session_key_masked"], "codex:abcd1234")
            finally:
                daemon.close()

    def test_selftest_wav_use_live_window_vs_default(self) -> None:
        class _CountingWindowPlatform(_FakePlatform):
            def __init__(self) -> None:
                super().__init__()
                self.fg = ForegroundWindow(hwnd=77, process="WeChat.exe", title="Alice")
                self.fg_calls = 0

            def get_foreground_window(self) -> ForegroundWindow:
                self.fg_calls += 1
                return self.fg

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            plat = _CountingWindowPlatform()
            cfg = Config(refiner=RefinerConfig(provider="off"))
            daemon = VoiceInputDaemon(
                cfg,
                env={},
                runtime_dir=tmp,
                platform=plat,
                asr_provider=_FakeAsr("测试文本"),
                refiner=create_refiner(cfg),
                tracker=_isolated_tracker(tmp),
            )
            try:
                wav_file = tmp / "sample.wav"
                wav_file.write_bytes(_voiced_wav())

                # Default use_live_window=False -> empty ForegroundWindow(), never calls get_foreground_window()
                r_default = daemon.handle_control(
                    {"cmd": "selftest_wav", "wav_path": str(wav_file), "no_telemetry": True}
                )
                self.assertTrue(r_default["ok"])
                self.assertEqual(plat.fg_calls, 0)
                self.assertEqual(r_default["routing"]["mode"], "agent")

                # Explicit non-bool string "false" must NOT enable use_live_window
                r_str = daemon.handle_control(
                    {
                        "cmd": "selftest_wav",
                        "wav_path": str(wav_file),
                        "no_telemetry": True,
                        "use_live_window": "false",
                    }
                )
                self.assertTrue(r_str["ok"])
                self.assertEqual(plat.fg_calls, 0)

                # use_live_window=True with no window fields -> calls get_foreground_window() (WeChat -> general)
                r_live = daemon.handle_control(
                    {
                        "cmd": "selftest_wav",
                        "wav_path": str(wav_file),
                        "no_telemetry": True,
                        "use_live_window": True,
                    }
                )
                self.assertTrue(r_live["ok"])
                self.assertEqual(plat.fg_calls, 1)
                self.assertEqual(r_live["routing"]["mode"], "general")

                # use_live_window=True WITH explicit window field -> explicit window wins, no live lookup
                r_explicit = daemon.handle_control(
                    {
                        "cmd": "selftest_wav",
                        "wav_path": str(wav_file),
                        "process": "WindowsTerminal.exe",
                        "no_telemetry": True,
                        "use_live_window": True,
                    }
                )
                self.assertTrue(r_explicit["ok"])
                self.assertEqual(plat.fg_calls, 1)
                self.assertEqual(r_explicit["routing"]["mode"], "agent")

                # Missing wav_path error
                r_missing = daemon.handle_control({"cmd": "selftest_wav", "wav_path": ""})
                self.assertFalse(r_missing["ok"])
                self.assertIn("wav_path not found", r_missing["error"])
            finally:
                daemon.close()

    def test_selftest_wav_nonblocking_control_plane_busy_latch_and_shutdown(self) -> None:
        from agent_voice_input.http_transport import CancelToken

        class _GateAsr(BaseAsrProvider):
            provider_name = "dashscope"

            def __init__(self) -> None:
                self.entered = threading.Event()
                self.release = threading.Event()
                self.cancelled_seen = threading.Event()

            def transcribe(
                self,
                wav_bytes: bytes,
                context: AsrContext | None = None,
                *,
                is_cancelled: object = None,
                cancel_token: CancelToken | None = None,
            ) -> AsrResult:
                del wav_bytes, context
                ev = threading.Event()
                if cancel_token is not None:
                    cancel_token.bind_event(ev)
                try:
                    self.entered.set()
                    deadline = time.monotonic() + 5.0
                    while time.monotonic() < deadline:
                        if ev.is_set() or (callable(is_cancelled) and is_cancelled()):
                            self.cancelled_seen.set()
                            raise AsrError("ASR cancelled")
                        if self.release.is_set():
                            return AsrResult("慢速自检文本", "dashscope", "mock", 12)
                        ev.wait(timeout=0.01)
                    return AsrResult("timeout", "dashscope", "mock", 12)
                finally:
                    if cancel_token is not None:
                        cancel_token.unbind_event(ev)

        with tempfile.TemporaryDirectory(prefix="avi-st-", dir="/tmp") as td:
            rt = Path(td)
            os.chmod(rt, 0o700)
            wav_file = rt / "sample.wav"
            wav_file.write_bytes(_voiced_wav())
            asr = _GateAsr()
            cfg = Config(refiner=RefinerConfig(provider="off"))
            plat = _FakePlatform()
            daemon = VoiceInputDaemon(
                cfg,
                env={},
                runtime_dir=rt,
                platform=plat,
                asr_provider=asr,
                refiner=create_refiner(cfg),
                tracker=_isolated_tracker(rt),
            )
            srv_th = threading.Thread(target=daemon.serve_forever, daemon=True)
            srv_th.start()
            try:
                _wait_for_control_socket(rt)

                # 1. Start slow selftest_wav in background client thread
                r1_box: list[dict[str, object]] = []

                def _run_c1() -> None:
                    r1_box.append(
                        send_control_command(
                            rt,
                            {"cmd": "selftest_wav", "wav_path": str(wav_file), "no_telemetry": True},
                            timeout_sec=5.0,
                        )
                    )

                c1_th = threading.Thread(target=_run_c1, daemon=True)
                c1_th.start()
                self.assertTrue(asr.entered.wait(timeout=2.0), "client 1 never entered ASR")

                # 2. While selftest_wav is blocked in ASR: status & probe_route reply in ms, 2nd selftest_wav gets busy!
                t_st0 = time.monotonic()
                st_resp = send_control_command(rt, {"cmd": "status"}, timeout_sec=1.0)
                self.assertTrue(st_resp.get("ok"), st_resp)
                self.assertLess(time.monotonic() - t_st0, 1.5)

                t_pr0 = time.monotonic()
                pr_resp = send_control_command(
                    rt,
                    {"cmd": "probe_route", "process": "WeChat.exe", "title": "Alice"},
                    timeout_sec=1.0,
                )
                self.assertTrue(pr_resp.get("ok"), pr_resp)
                self.assertEqual(pr_resp.get("mode"), "general")
                self.assertLess(time.monotonic() - t_pr0, 1.5)

                t_busy0 = time.monotonic()
                busy_resp = send_control_command(
                    rt,
                    {"cmd": "selftest_wav", "wav_path": str(wav_file), "no_telemetry": True},
                    timeout_sec=1.0,
                )
                self.assertFalse(busy_resp.get("ok"))
                self.assertEqual(busy_resp.get("error"), "selftest_wav busy")
                self.assertLess(time.monotonic() - t_busy0, 1.5)

                # 3. Release Client 1 and immediately fire sequential Client 3 -> no false busy!
                asr.release.set()
                c1_th.join(timeout=3.0)
                self.assertEqual(len(r1_box), 1)
                self.assertTrue(r1_box[0].get("ok"), r1_box[0])
                self.assertEqual(r1_box[0].get("final_text"), "慢速自检文本")

                r3 = send_control_command(
                    rt,
                    {"cmd": "selftest_wav", "wav_path": str(wav_file), "no_telemetry": True},
                    timeout_sec=2.0,
                )
                self.assertTrue(r3.get("ok"), r3)

                # 4. Shutdown while selftest_wav is in flight -> cancels cleanly, joins without hang
                asr.entered.clear()
                asr.release.clear()
                r4_box: list[dict[str, object]] = []

                def _run_c4() -> None:
                    try:
                        r4_box.append(
                            send_control_command(
                                rt,
                                {"cmd": "selftest_wav", "wav_path": str(wav_file), "no_telemetry": True},
                                timeout_sec=3.0,
                            )
                        )
                    except Exception:
                        pass

                c4_th = threading.Thread(target=_run_c4, daemon=True)
                c4_th.start()
                self.assertTrue(asr.entered.wait(timeout=2.0))

                t_shut0 = time.monotonic()
                shut_resp = send_control_command(rt, {"cmd": "shutdown"}, timeout_sec=2.0)
                self.assertTrue(shut_resp.get("ok"), shut_resp)
                srv_th.join(timeout=3.0)
                self.assertFalse(srv_th.is_alive(), "serve_forever did not exit promptly on shutdown")
                self.assertLess(time.monotonic() - t_shut0, 2.5)
                self.assertTrue(asr.cancelled_seen.wait(timeout=1.0))
                c4_th.join(timeout=2.0)

                # Post-shutdown direct control calls cleanly rejected
                post_st = daemon.handle_control({"cmd": "selftest_wav", "wav_path": str(wav_file)})
                self.assertFalse(post_st["ok"])
                self.assertEqual(post_st["error"], "daemon stopping")
                post_pr = daemon.handle_control({"cmd": "probe_route"})
                self.assertFalse(post_pr["ok"])
                self.assertEqual(post_pr["error"], "daemon stopping")
            finally:
                asr.release.set()
                daemon.close()
                srv_th.join(timeout=2.0)

    def test_probe_route_matrix_zero_cost_read_only_and_isolation(self) -> None:
        import contextlib
        import sqlite3
        from agent_voice_input.refiner import BaseRefiner

        class _NeverCallAsr(BaseAsrProvider):
            provider_name = "dashscope"

            def __init__(self) -> None:
                self.calls = 0

            def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None, **kw: object) -> AsrResult:
                del wav_bytes, context, kw
                self.calls += 1
                raise AssertionError("probe_route must never invoke ASR")

        class _NeverCallRefiner(BaseRefiner):
            provider_name = "deepseek"

            def __init__(self) -> None:
                self.calls = 0

            def refine(self, asr: AsrResult, **kw: object) -> RefinerResult:
                del asr, kw
                self.calls += 1
                raise AssertionError("probe_route must never invoke Refiner")

            def close(self) -> None:
                pass

        class _LiveCountingPlatform(_FakePlatform):
            def __init__(self) -> None:
                super().__init__()
                self.fg = ForegroundWindow(hwnd=88, process="WeChat.exe", title="Team Chat")
                self.fg_calls = 0

            def get_foreground_window(self) -> ForegroundWindow:
                self.fg_calls += 1
                return self.fg

        with tempfile.TemporaryDirectory(prefix="avi-pr-", dir="/tmp") as td:
            tmp = Path(td)
            os.chmod(tmp, 0o700)
            home = tmp / "home"
            rt = tmp / "rt"
            rt.mkdir(mode=0o700)
            term_repo = home / "work" / "term-repo"
            agy_repo = home / "work" / "agy-repo"
            codex_repo = home / "work" / "codex-repo"
            for r in (term_repo, agy_repo, codex_repo):
                r.mkdir(parents=True)
            (term_repo / "TermRouter.py").write_text("class TermRouter: pass\n", encoding="utf-8")
            (agy_repo / "AgyKernel.py").write_text("class AgyKernel: pass\n", encoding="utf-8")
            (codex_repo / "CodexBatcher.py").write_text("class CodexBatcher: pass\n", encoding="utf-8")

            # 1. Terminal Codex session + registry entry
            term_sid = "abcd1234-1111-2222-3333-444444444444"
            term_rollout = self._write_codex_rollout(
                home / ".codex" / "sessions" / "2026" / "11" / "23" / f"rollout-2026-11-23T10-00-00-{term_sid}.jsonl",
                term_sid,
                str(term_repo),
                [
                    ("user", "请帮我修复 PRIVATE_DIALOG_SENTENCE_9911 在 TermRouter 中的边界问题"),
                    ("assistant", "已定位 TermRouter 中的问题并给出方案。"),
                ],
                source="cli",
            )
            write_session_registry_entry(
                rt,
                cli="codex",
                session_id=term_sid,
                cwd=str(term_repo),
                jsonl_path=str(term_rollout),
                pid=os.getpid(),
            )

            # 2. AGY 2.0 conversation_summaries.db + brain transcript
            agy_cid = "75ae924a-aaaa-bbbb-cccc-dddddddddddd"
            ide_root = home / ".gemini" / "antigravity"
            agy_log = ide_root / "brain" / agy_cid / ".system_generated" / "logs" / "transcript_full.jsonl"
            agy_log.parent.mkdir(parents=True, exist_ok=True)
            agy_log.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "source": "USER_EXPLICIT",
                                "type": "USER_INPUT",
                                "content": "<USER_REQUEST>Refactor PRIVATE_AGY_BODY_8822 in AgyKernel</USER_REQUEST>",
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            {
                                "source": "MODEL",
                                "type": "PLANNER_RESPONSE",
                                "content": "Updating AgyKernel now.",
                            },
                            ensure_ascii=False,
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            agy_db = ide_root / "conversation_summaries.db"
            with contextlib.closing(sqlite3.connect(str(agy_db))) as conn:
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
                conn.execute(
                    "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        agy_cid,
                        "AGY Routing Task",
                        json.dumps([agy_repo.as_uri()]),
                        "2026-11-23T10:05:00Z",
                        "USER_EXPLICIT",
                        None,
                        0,
                        "antigravity",
                    ),
                )
                conn.commit()

            # 3. ChatGPT/Codex desktop SQLite DBs (local repo promotion + cloud chat veto)
            codex_sid = "01a207c8-9999-8888-7777-666666666666"
            codex_rollout = self._write_codex_rollout(
                home / ".codex" / "sessions" / "2026" / "11" / "23" / f"rollout-2026-11-23T10-05-00-{codex_sid}.jsonl",
                codex_sid,
                str(codex_repo),
                [
                    ("user", "分析 PRIVATE_CODEX_BODY_7733 与 CodexBatcher 吞吐"),
                    ("assistant", "正在检查 CodexBatcher 实现。"),
                ],
                source="vscode",
            )
            state_db = home / ".codex" / "state_5.sqlite"
            with contextlib.closing(sqlite3.connect(str(state_db))) as sconn:
                sconn.execute(
                    """
                    CREATE TABLE threads (
                        id TEXT PRIMARY KEY,
                        name TEXT,
                        cwd TEXT,
                        rollout_path TEXT,
                        originator TEXT,
                        archived INTEGER DEFAULT 0,
                        recency_at_ms INTEGER,
                        updated_at INTEGER
                    )
                    """
                )
                sconn.execute(
                    "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        codex_sid,
                        "Refactor QuartzBatcher",
                        str(codex_repo),
                        str(codex_rollout),
                        "Codex Desktop",
                        0,
                        1_760_000_100_000,
                        1_760_000_100,
                    ),
                )
                sconn.commit()
            codex_db = home / ".codex" / "sqlite" / "codex.db"
            codex_db.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.closing(sqlite3.connect(str(codex_db))) as cconn:
                cconn.execute(
                    """
                    CREATE TABLE local_thread_catalog (
                        thread_id TEXT,
                        display_title TEXT,
                        source_kind TEXT,
                        host_id TEXT,
                        cwd TEXT,
                        missing_candidate INTEGER DEFAULT 0,
                        source_recency_at TEXT,
                        updated_at INTEGER
                    )
                    """
                )
                cconn.executemany(
                    "INSERT INTO local_thread_catalog VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (codex_sid, "Refactor QuartzBatcher", "codex", "local", str(codex_repo), 0, "2026-11-23T10:05:00Z", 1_760_000_100),
                        (None, "Weekend Travel Plan", "chatgpt", "chatgpt:u1", None, 0, "2026-11-23T12:00:00Z", 1_760_000_900),
                    ],
                )
                cconn.commit()

            env = {"HOME": str(home), "XDG_RUNTIME_DIR": str(rt)}
            cfg = Config(refiner=RefinerConfig(provider="deepseek"))
            asr = _NeverCallAsr()
            refiner = _NeverCallRefiner()
            plat = _LiveCountingPlatform()
            tracker = SessionTracker(home_dir=home, runtime_dir=rt, proc_root=tmp / "empty_proc", env=env)
            daemon = VoiceInputDaemon(
                cfg,
                env=env,
                runtime_dir=rt,
                platform=plat,
                asr_provider=asr,
                refiner=refiner,
                tracker=tracker,
            )
            try:
                all_resps: list[dict[str, object]] = []
                # Pre-populate one real session in production tracker to prove probe_route never touches it
                pre_snap = tracker.sync_session(window_title="codex [#abcd1234]")
                self.assertIsNotNone(pre_snap)
                term_key = f"codex:{term_sid}"
                self.assertIn(term_key, tracker._states)
                prod_keys_before = list(tracker._states.keys())
                term_turns_before = list(tracker._states[term_key].clean_turns)

                # a) Terminal session hit
                r_term = daemon.handle_control(
                    {"cmd": "probe_route", "process": "WindowsTerminal.exe", "title": "codex [#abcd1234]"}
                )
                all_resps.append(r_term)
                self.assertTrue(r_term["ok"])
                self.assertEqual(r_term["window_category"], "terminal")
                self.assertEqual(r_term["mode"], "agent")
                self.assertTrue(r_term["should_sync"])
                self.assertEqual(r_term["hotwords_scope"], "coding")
                self.assertGreater(r_term["hotword_count"], 0)
                self.assertEqual(r_term["session_key_masked"], "codex:abcd1234")
                self.assertTrue(r_term["matched_by"])
                self.assertFalse(r_term["remote_vetoed"])
                self.assertEqual(r_term["workspace_name"], "term-repo")
                self.assertGreater(r_term["ctx_chars"], 0)
                self.assertGreaterEqual(r_term["ctx_chunks"], 1)
                self.assertGreaterEqual(r_term["sync_ms"], 0)

                # b) AGY 2.0 ide_db_title hit
                r_agy = daemon.handle_control(
                    {"cmd": "probe_route", "process": "Antigravity.exe", "title": "AGY Routing Task"}
                )
                all_resps.append(r_agy)
                self.assertTrue(r_agy["ok"])
                self.assertEqual(r_agy["window_category"], "coding_gui")
                self.assertEqual(r_agy["mode"], "agent")
                self.assertFalse(r_agy["should_sync"])
                self.assertEqual(r_agy["hotwords_scope"], "coding")
                self.assertEqual(r_agy["session_key_masked"], "agy:75ae924a")
                self.assertEqual(r_agy["matched_by"], "ide_db_title")
                self.assertFalse(r_agy["remote_vetoed"])
                self.assertEqual(r_agy["workspace_name"], "agy-repo")
                self.assertGreater(r_agy["ctx_chars"], 0)
                self.assertGreaterEqual(r_agy["ctx_chunks"], 1)

                # c) ChatGPT/Codex desktop local repo dynamic promotion to agent + coding
                r_cdx_local = daemon.handle_control(
                    {"cmd": "probe_route", "process": "ChatGPT.exe", "title": "Refactor QuartzBatcher"}
                )
                all_resps.append(r_cdx_local)
                self.assertTrue(r_cdx_local["ok"])
                self.assertEqual(r_cdx_local["window_category"], "general_gui")
                self.assertEqual(r_cdx_local["mode"], "agent")
                self.assertFalse(r_cdx_local["should_sync"])
                self.assertEqual(r_cdx_local["target_app_hint"], "")
                self.assertEqual(r_cdx_local["hotwords_scope"], "coding")
                self.assertEqual(r_cdx_local["session_key_masked"], "codex:01a207c8")
                self.assertEqual(r_cdx_local["matched_by"], "codex_desktop_db")
                self.assertFalse(r_cdx_local["remote_vetoed"])
                self.assertEqual(r_cdx_local["workspace_name"], "codex-repo")
                self.assertGreater(r_cdx_local["ctx_chars"], 0)
                self.assertGreaterEqual(r_cdx_local["ctx_chunks"], 1)

                # d) ChatGPT/Codex desktop cloud chat veto stays general
                r_cdx_cloud = daemon.handle_control(
                    {"cmd": "probe_route", "process": "ChatGPT.exe", "title": "Weekend Travel Plan"}
                )
                all_resps.append(r_cdx_cloud)
                self.assertTrue(r_cdx_cloud["ok"])
                self.assertEqual(r_cdx_cloud["window_category"], "general_gui")
                self.assertEqual(r_cdx_cloud["mode"], "general")
                self.assertFalse(r_cdx_cloud["should_sync"])
                self.assertEqual(r_cdx_cloud["session_key_masked"], "")
                self.assertEqual(r_cdx_cloud["matched_by"], "")
                self.assertFalse(r_cdx_cloud["remote_vetoed"])
                self.assertEqual(r_cdx_cloud["workspace_name"], "")
                self.assertEqual(r_cdx_cloud["ctx_chars"], 0)
                self.assertEqual(r_cdx_cloud["ctx_chunks"], 0)

                # e) general_gui hard isolation from background CLI sessions
                r_wechat = daemon.handle_control(
                    {"cmd": "probe_route", "process": "WeChat.exe", "title": "Alice"}
                )
                all_resps.append(r_wechat)
                self.assertTrue(r_wechat["ok"])
                self.assertEqual(r_wechat["window_category"], "general_gui")
                self.assertEqual(r_wechat["mode"], "general")
                self.assertFalse(r_wechat["should_sync"])
                self.assertEqual(r_wechat["target_app_hint"], "im_chat")
                self.assertEqual(r_wechat["hotwords_scope"], "chat")
                self.assertEqual(r_wechat["session_key_masked"], "")
                self.assertEqual(r_wechat["matched_by"], "")
                self.assertFalse(r_wechat["remote_vetoed"])
                self.assertEqual(r_wechat["workspace_name"], "")
                self.assertEqual(r_wechat["ctx_chars"], 0)
                self.assertEqual(r_wechat["ctx_chunks"], 0)

                # f) SSH Phase 0 veto (terminal remote host tag + remote IDE window)
                r_ssh = daemon.handle_control(
                    {
                        "cmd": "probe_route",
                        "process": "WindowsTerminal.exe",
                        "title": "[@prod-box] codex [#abcd1234]",
                        "cwd": str(term_repo),
                    }
                )
                all_resps.append(r_ssh)
                self.assertTrue(r_ssh["ok"])
                self.assertEqual(r_ssh["window_category"], "terminal")
                self.assertTrue(r_ssh["should_sync"])
                self.assertTrue(r_ssh["remote_vetoed"])
                self.assertEqual(r_ssh["session_key_masked"], "")
                self.assertEqual(r_ssh["workspace_name"], "")
                self.assertEqual(r_ssh["ctx_chars"], 0)
                self.assertEqual(r_ssh["ctx_chunks"], 0)

                r_rem_ide = daemon.handle_control(
                    {
                        "cmd": "probe_route",
                        "process": "Code.exe",
                        "title": "main.py - term-repo [SSH: prod-box] - Visual Studio Code",
                    }
                )
                all_resps.append(r_rem_ide)
                self.assertTrue(r_rem_ide["ok"])
                self.assertEqual(r_rem_ide["window_category"], "coding_gui")
                self.assertEqual(r_rem_ide["mode"], "agent")
                self.assertFalse(r_rem_ide["should_sync"])
                self.assertTrue(r_rem_ide["remote_vetoed"])
                self.assertEqual(r_rem_ide["session_key_masked"], "")
                self.assertEqual(r_rem_ide["workspace_name"], "")

                # g) use_live_window=False vs True
                self.assertEqual(plat.fg_calls, 0)
                r_empty = daemon.handle_control({"cmd": "probe_route"})
                all_resps.append(r_empty)
                self.assertEqual(plat.fg_calls, 0)
                self.assertEqual(r_empty["mode"], "general")
                r_live = daemon.handle_control({"cmd": "probe_route", "use_live_window": True})
                all_resps.append(r_live)
                self.assertEqual(plat.fg_calls, 1)
                self.assertEqual(r_live["target_app_hint"], "im_chat")

                # h) Global redlines: zero ASR/Refiner/telemetry/state/UI/path/dialog leakage & tracker isolation
                self.assertEqual(asr.calls, 0)
                self.assertEqual(refiner.calls, 0)
                self.assertEqual(daemon.state, "idle")
                self.assertEqual(daemon.seq_id, 0)
                self.assertEqual(daemon.last_result, {})
                self.assertEqual(list(daemon.recent_results), [])
                self.assertFalse(daemon.telemetry_path.exists())
                self.assertEqual(plat.deliveries, [])
                self.assertEqual(plat.huds, [])
                self.assertEqual(plat.beeps, [])
                self.assertEqual(list(tracker._states.keys()), prod_keys_before)
                self.assertEqual(list(tracker._states[term_key].clean_turns), term_turns_before)

                for resp in all_resps:
                    blob = json.dumps(resp, ensure_ascii=False)
                    for forbidden in (
                        str(home),
                        str(term_repo),
                        str(agy_repo),
                        str(codex_repo),
                        term_sid,
                        agy_cid,
                        codex_sid,
                        "PRIVATE_DIALOG_SENTENCE_9911",
                        "PRIVATE_AGY_BODY_8822",
                        "PRIVATE_CODEX_BODY_7733",
                    ):
                        self.assertNotIn(forbidden, blob)
            finally:
                daemon.close()

    def test_selftest_wav_deliver_validation_rejects_unsafe_or_non_bool(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            wav_file = tmp / "sample.wav"
            wav_file.write_bytes(_voiced_wav())
            plat = _FakePlatform()
            cfg = Config(refiner=RefinerConfig(provider="off"))
            daemon = VoiceInputDaemon(
                cfg,
                env={},
                runtime_dir=tmp,
                platform=plat,
                asr_provider=_FakeAsr("绝不应投递"),
                refiner=create_refiner(cfg),
                tracker=_isolated_tracker(tmp),
            )
            try:
                # 1. Non-bool deliver values rejected strictly
                for bad_val in ("true", "false", 1, 0, None):
                    r_bad = daemon.handle_control(
                        {
                            "cmd": "selftest_wav",
                            "wav_path": str(wav_file),
                            "use_live_window": True,
                            "deliver": bad_val,
                        }
                    )
                    self.assertFalse(r_bad["ok"], f"expected rejection for deliver={bad_val!r}")
                    self.assertIn("boolean", str(r_bad.get("error", "")))

                # 2. deliver=True without use_live_window=True rejected
                r_no_live = daemon.handle_control(
                    {
                        "cmd": "selftest_wav",
                        "wav_path": str(wav_file),
                        "deliver": True,
                    }
                )
                self.assertFalse(r_no_live["ok"])
                self.assertEqual(
                    r_no_live["error"],
                    "deliver requires use_live_window=true without window overrides",
                )

                # 3. deliver=True with use_live_window=True + virtual window override rejected
                for override in (
                    {"process": "WindowsTerminal.exe"},
                    {"title": "codex [#abcd1234]"},
                    {"hwnd": 123},
                ):
                    r_override = daemon.handle_control(
                        {
                            "cmd": "selftest_wav",
                            "wav_path": str(wav_file),
                            "use_live_window": True,
                            "deliver": True,
                            **override,
                        }
                    )
                    self.assertFalse(r_override["ok"], f"expected rejection for override={override!r}")
                    self.assertEqual(
                        r_override["error"],
                        "deliver requires use_live_window=true without window overrides",
                    )

                self.assertEqual(plat.deliveries, [])
                self.assertEqual(daemon.seq_id, 0)
                self.assertFalse(daemon.telemetry_path.exists())
            finally:
                daemon.close()

    def test_selftest_wav_deliver_live_window_real_delivery_and_focus_drift(self) -> None:
        from agent_voice_input.audio_proc import get_failed_audio_dir
        from agent_voice_input.config import AudioConfig

        class _DeliverCapturePlatform(_FakePlatform):
            def __init__(self) -> None:
                super().__init__()
                self.fg = ForegroundWindow(hwnd=321, process="WindowsTerminal.exe", title="codex")
                self.fg_calls = 0
                self.delivered_origins: list[ForegroundWindow | None] = []
                self.suppress_beep_args: list[object] = []

            def get_foreground_window(self) -> ForegroundWindow:
                self.fg_calls += 1
                return self.fg

            def deliver_text(
                self,
                text: str,
                seq_id: int,
                target_hwnd: int = 0,
                *,
                origin_window: ForegroundWindow | None = None,
                suppress_beep: object = False,
                **kw: object,
            ) -> PasteOutcome:
                del kw
                self.delivered_origins.append(origin_window)
                self.suppress_beep_args.append(suppress_beep)
                self.deliveries.append((text, seq_id, target_hwnd))
                return self.paste_outcome

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            rt = tmp / "rt"
            cache = tmp / "cache"
            rt.mkdir(mode=0o700)
            cache.mkdir(mode=0o700)
            wav_file = tmp / "sample.wav"
            wav_file.write_bytes(_voiced_wav())

            plat = _DeliverCapturePlatform()
            cfg = Config(
                audio=AudioConfig(save_recordings=True),
                refiner=RefinerConfig(provider="off"),
            )
            env = {"XDG_CACHE_HOME": str(cache), "XDG_RUNTIME_DIR": str(rt)}
            daemon = VoiceInputDaemon(
                cfg,
                env=env,
                runtime_dir=rt,
                platform=plat,
                asr_provider=_FakeAsr("真实桌面落字测试"),
                refiner=create_refiner(cfg),
                tracker=_isolated_tracker(rt),
            )
            try:
                # 1. Explicit deliver=True + use_live_window=True + no_telemetry=True -> real delivery
                r_ok = daemon.handle_control(
                    {
                        "cmd": "selftest_wav",
                        "wav_path": str(wav_file),
                        "use_live_window": True,
                        "deliver": True,
                        "no_telemetry": True,
                    }
                )
                self.assertTrue(r_ok["ok"], r_ok)
                self.assertEqual(r_ok["seq_id"], 0)
                self.assertTrue(r_ok["pasted"])
                self.assertTrue(r_ok["clipboard"])
                self.assertEqual(r_ok["paste_reason"], "ok")
                self.assertEqual(r_ok["delivery_status"], "pasted")
                self.assertTrue(r_ok["summary"]["pasted"])
                self.assertEqual(r_ok["summary"]["delivery_status"], "pasted")
                self.assertNotIn("dry_run", r_ok["summary"])
                self.assertEqual(plat.fg_calls, 1)
                self.assertEqual(len(plat.deliveries), 1)
                self.assertEqual(plat.deliveries[0], ("真实桌面落字测试", 0, 321))
                self.assertEqual(plat.delivered_origins[0], plat.fg)
                self.assertIs(plat.suppress_beep_args[0], True)

                # 2. Focus drift during deliver=True safely falls back to clipboard (copied)
                plat.paste_outcome = PasteOutcome(False, True, "focus_changed", "Notepad.exe")
                r_drift = daemon.handle_control(
                    {
                        "cmd": "selftest_wav",
                        "wav_path": str(wav_file),
                        "use_live_window": True,
                        "deliver": True,
                        "no_telemetry": True,
                    }
                )
                self.assertTrue(r_drift["ok"], r_drift)
                self.assertFalse(r_drift["pasted"])
                self.assertTrue(r_drift["clipboard"])
                self.assertEqual(r_drift["paste_reason"], "focus_changed")
                self.assertEqual(r_drift["delivery_status"], "copied")

                # 3. Delivery failure during deliver=True never stashes to failed/ or recordings/
                plat.paste_outcome = PasteOutcome(False, False, "paste_failed", "WindowsTerminal.exe")
                r_fail = daemon.handle_control(
                    {
                        "cmd": "selftest_wav",
                        "wav_path": str(wav_file),
                        "use_live_window": True,
                        "deliver": True,
                        "no_telemetry": True,
                    }
                )
                self.assertFalse(r_fail["ok"])
                self.assertFalse(r_fail["pasted"])
                self.assertFalse(r_fail["clipboard"])
                self.assertEqual(r_fail["delivery_status"], "undelivered_unrecoverable")

                # Global zero-pollution invariants across all 3 deliver=True runs
                self.assertEqual(daemon.seq_id, 0)
                self.assertEqual(daemon.last_result, {})
                self.assertEqual(list(daemon.recent_results), [])
                self.assertFalse(daemon.telemetry_path.exists())
                self.assertEqual(plat.huds, [])
                self.assertEqual(plat.beeps, [])
                self.assertFalse(get_failed_audio_dir(rt, create=False).exists())
                rec_dir = cache / "agent-voice-input" / "recordings"
                self.assertEqual(list(rec_dir.glob("*")) if rec_dir.exists() else [], [])
            finally:
                daemon.close()

    def test_selftest_wav_live_window_pinned_before_wav_gate_and_rejects_empty_window(self) -> None:
        from agent_voice_input import daemon_pipeline as pipeline_mod

        events: list[str] = []
        win_a = ForegroundWindow(hwnd=101, process="WindowsTerminal.exe", title="codex")
        win_b = ForegroundWindow(hwnd=202, process="Notepad.exe", title="other")

        class _DriftPlatform(_FakePlatform):
            def __init__(self) -> None:
                super().__init__()
                self.fg = win_a
                self.seen_origin: ForegroundWindow | None = None
                self.seen_suppress_beep: object = None

            def get_foreground_window(self) -> ForegroundWindow:
                events.append("get_fg")
                return self.fg

            def deliver_text(
                self,
                text: str,
                seq_id: int,
                target_hwnd: int = 0,
                *,
                origin_window: ForegroundWindow | None = None,
                suppress_beep: object = False,
                **kw: object,
            ) -> PasteOutcome:
                del text, seq_id, target_hwnd, kw
                events.append("deliver_text")
                self.seen_origin = origin_window
                self.seen_suppress_beep = suppress_beep
                if origin_window is None or origin_window.hwnd != self.fg.hwnd:
                    return PasteOutcome(False, True, "focus_changed", self.fg.process)
                return PasteOutcome(True, True, "ok", self.fg.process, used_clipboard_paste=True)

        class _OrderAsr(BaseAsrProvider):
            provider_name = "dashscope"

            def __init__(self) -> None:
                self.calls = 0

            def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None, **kw: object) -> AsrResult:
                del wav_bytes, context, kw
                self.calls += 1
                events.append("asr")
                return AsrResult("切窗测试文本", "dashscope", "mock", 5)

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            wav_file = rt / "sample.wav"
            wav_file.write_bytes(_voiced_wav())
            plat = _DriftPlatform()
            asr = _OrderAsr()
            cfg = Config(refiner=RefinerConfig(provider="off"))
            daemon = VoiceInputDaemon(
                cfg,
                env={},
                runtime_dir=rt,
                platform=plat,
                asr_provider=asr,
                refiner=create_refiner(cfg),
                tracker=_isolated_tracker(rt),
            )
            orig_process_wav = pipeline_mod.process_wav

            def _tracked_process_wav(*args: object, **kwargs: object) -> object:
                events.append("process_wav")
                # User switches to Window B while WAV gating runs!
                plat.fg = win_b
                return orig_process_wav(*args, **kwargs)  # type: ignore[arg-type]

            try:
                with unittest.mock.patch.object(pipeline_mod, "process_wav", side_effect=_tracked_process_wav):
                    r_drift = daemon.handle_control(
                        {
                            "cmd": "selftest_wav",
                            "wav_path": str(wav_file),
                            "use_live_window": True,
                            "deliver": True,
                            "no_telemetry": True,
                        }
                    )
                self.assertTrue(r_drift["ok"], r_drift)
                self.assertEqual(events, ["get_fg", "process_wav", "asr", "deliver_text"])
                self.assertEqual(plat.seen_origin, win_a)
                self.assertIs(plat.seen_suppress_beep, True)
                self.assertFalse(r_drift["pasted"])
                self.assertTrue(r_drift["clipboard"])
                self.assertEqual(r_drift["paste_reason"], "focus_changed")
                self.assertEqual(r_drift["delivery_status"], "copied")

                # Empty live foreground window must be rejected immediately on deliver=True
                events.clear()
                asr_before = asr.calls
                plat.fg = ForegroundWindow()
                r_empty = daemon.handle_control(
                    {
                        "cmd": "selftest_wav",
                        "wav_path": str(wav_file),
                        "use_live_window": True,
                        "deliver": True,
                        "no_telemetry": True,
                    }
                )
                self.assertFalse(r_empty["ok"])
                self.assertEqual(r_empty["error"], "no live foreground window")
                self.assertEqual(events, ["get_fg"])
                self.assertEqual(asr.calls, asr_before)
            finally:
                daemon.close()

    def test_selftest_wav_deliver_serializes_with_main_dictation_and_respects_clipboard_settle(self) -> None:
        now = [100.0]
        sleeps: list[float] = []

        def _clock() -> float:
            return now[0]

        def _sleep(sec: float) -> None:
            sleeps.append(sec)
            now[0] += sec

        main_in_deliver = threading.Event()
        release_main = threading.Event()
        in_flight = [0]
        max_concurrent = [0]
        deliver_order: list[str] = []
        suppress_beeps: dict[str, object] = {}
        obs_lock = threading.Lock()

        class _SerialDeliverPlatform(_FakePlatform):
            def __init__(self) -> None:
                super().__init__()
                self.fg = ForegroundWindow(hwnd=505, process="WindowsTerminal.exe", title="codex")

            def deliver_text(
                self,
                text: str,
                seq_id: int,
                target_hwnd: int = 0,
                *,
                origin_window: ForegroundWindow | None = None,
                suppress_beep: object = False,
                before_clipboard_write: object = None,
                before_commit: object = None,
                **kw: object,
            ) -> PasteOutcome:
                del target_hwnd, origin_window, kw
                with obs_lock:
                    in_flight[0] += 1
                    if in_flight[0] > max_concurrent[0]:
                        max_concurrent[0] = in_flight[0]
                try:
                    if callable(before_clipboard_write):
                        before_clipboard_write()
                    if callable(before_commit) and not before_commit():
                        return PasteOutcome(False, False, "cancelled", "")
                    tag = "selftest" if seq_id == 0 else "main"
                    with obs_lock:
                        suppress_beeps[tag] = suppress_beep
                        deliver_order.append(f"{tag}_start")
                    if tag == "main":
                        main_in_deliver.set()
                        release_main.wait(timeout=3.0)
                    with obs_lock:
                        deliver_order.append(f"{tag}_end")
                    return PasteOutcome(True, True, "ok", "WindowsTerminal.exe", used_clipboard_paste=True)
                finally:
                    with obs_lock:
                        in_flight[0] -= 1

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            wav_file = rt / "sample.wav"
            wav_file.write_bytes(_voiced_wav())
            plat = _SerialDeliverPlatform()
            cfg = Config(refiner=RefinerConfig(provider="off"))
            daemon = VoiceInputDaemon(
                cfg,
                env={},
                runtime_dir=rt,
                platform=plat,
                asr_provider=_FakeAsr("串行投递测试"),
                refiner=create_refiner(cfg),
                tracker=_isolated_tracker(rt),
                clock_fn=_clock,
                sleep_fn=_sleep,
                paste_settle_sec=0.15,
            )
            try:
                daemon.handle_trigger("toggle")
                daemon._last_trigger_ts = -1e9
                daemon.handle_trigger("toggle")
                self.assertTrue(main_in_deliver.wait(timeout=2.0), "main dictation did not enter deliver_text")

                selftest_reached_deliver = threading.Event()
                orig_deliver_for_selftest = daemon._deliver_for_selftest

                def _tracked_deliver_for_selftest(*args: object, **kwargs: object) -> PasteOutcome:
                    selftest_reached_deliver.set()
                    return orig_deliver_for_selftest(*args, **kwargs)  # type: ignore[arg-type]

                daemon._deliver_for_selftest = _tracked_deliver_for_selftest  # type: ignore[method-assign]

                st_res: list[dict[str, object]] = []

                def _run_st() -> None:
                    st_res.append(
                        daemon.handle_control(
                            {
                                "cmd": "selftest_wav",
                                "wav_path": str(wav_file),
                                "use_live_window": True,
                                "deliver": True,
                                "no_telemetry": True,
                            }
                        )
                    )

                st_th = threading.Thread(target=_run_st, daemon=True)
                st_th.start()
                self.assertTrue(
                    selftest_reached_deliver.wait(timeout=2.0),
                    "selftest did not reach _deliver_for_selftest while main held _deliver_lock",
                )
                # selftest must still be blocked behind _deliver_lock until main finishes
                self.assertEqual(max_concurrent[0], 1)
                self.assertEqual(deliver_order, ["main_start"])

                release_main.set()
                st_th.join(timeout=3.0)
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and daemon.state != "idle":
                    time.sleep(0.01)

                self.assertEqual(max_concurrent[0], 1)
                self.assertEqual(deliver_order, ["main_start", "main_end", "selftest_start", "selftest_end"])
                self.assertEqual(len(st_res), 1)
                self.assertTrue(st_res[0]["ok"], st_res[0])
                self.assertTrue(st_res[0]["pasted"])
                self.assertIs(suppress_beeps["selftest"], True)
                self.assertGreaterEqual(sum(sleeps), 0.149)
            finally:
                release_main.set()
                daemon.close()

    def test_probe_route_isolated_tracker_does_not_pollute_ide_disambiguation(self) -> None:
        from agent_voice_input.daemon import _Stage1State, _resolve_stage1_context

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            rt = tmp / "rt"
            rt.mkdir(mode=0o700)
            # Real workspace under ~/repos/dup-proj
            real_repo = home / "repos" / "dup-proj"
            real_repo.mkdir(parents=True)
            # Separate unrelated dir with the SAME basename used only by virtual probe_route
            probe_repo = tmp / "sandbox_other" / "dup-proj"
            probe_repo.mkdir(parents=True)

            probe_sid = "1234abcd-1111-2222-3333-444444444444"
            probe_rollout = self._write_codex_rollout(
                home / ".codex" / "sessions" / "2026" / "11" / "23" / f"rollout-2026-11-23T10-00-00-{probe_sid}.jsonl",
                probe_sid,
                str(probe_repo),
                [("user", "sandbox probe turn")],
            )
            write_session_registry_entry(
                rt,
                cli="codex",
                session_id=probe_sid,
                cwd=str(probe_repo),
                jsonl_path=str(probe_rollout),
                pid=os.getpid(),
            )

            env = {"HOME": str(home), "XDG_RUNTIME_DIR": str(rt)}
            cfg = Config(refiner=RefinerConfig(provider="off"))
            plat = _FakePlatform()
            tracker = SessionTracker(home_dir=home, runtime_dir=rt, proc_root=tmp / "empty_proc", env=env)
            daemon = VoiceInputDaemon(
                cfg,
                env=env,
                runtime_dir=rt,
                platform=plat,
                asr_provider=_FakeAsr("ok"),
                refiner=create_refiner(cfg),
                tracker=tracker,
            )
            try:
                self.assertEqual(dict(daemon.tracker._states), {})
                r_probe = daemon.handle_control(
                    {
                        "cmd": "probe_route",
                        "process": "WindowsTerminal.exe",
                        "title": "codex [#1234abcd]",
                    }
                )
                self.assertTrue(r_probe["ok"], r_probe)
                self.assertEqual(r_probe["session_key_masked"], "codex:1234abcd")
                self.assertEqual(r_probe["workspace_name"], "dup-proj")
                # Production tracker MUST have zero state pollution!
                self.assertEqual(dict(daemon.tracker._states), {})
                self.assertEqual(daemon.tracker.active_session_cwds(), [])

                # Real VS Code IDE window with same basename resolves cleanly to ~/repos/dup-proj
                ide_st = _Stage1State(t_total0=time.monotonic())
                _resolve_stage1_context(
                    ide_st,
                    cfg=cfg,
                    env=env,
                    tracker=daemon.tracker,
                    platform=plat,
                    origin_window=ForegroundWindow(
                        hwnd=99,
                        process="Code.exe",
                        title="main.py - dup-proj - Visual Studio Code",
                    ),
                    window_title_override=None,
                    audio_source="live",
                    explicit_mode="auto",
                    cancel_check=None,
                    attach_single_hop=False,
                )
                self.assertEqual(os.path.realpath(ide_st.workspace_cwd), os.path.realpath(real_repo))
            finally:
                daemon.close()

    def test_close_joins_inflight_control_thread_before_closing_refiner_and_tracker(self) -> None:
        from agent_voice_input.http_transport import CancelToken
        from agent_voice_input.refiner import BaseRefiner

        order: list[str] = []
        entered_refine = threading.Event()

        class _OrderedBlockingRefiner(BaseRefiner):
            provider_name = "deepseek"

            def refine(
                self,
                asr: AsrResult,
                *,
                cancel_token: CancelToken | None = None,
                is_cancelled: object = None,
                **kw: object,
            ) -> RefinerResult:
                del kw
                ev = threading.Event()
                if cancel_token is not None:
                    cancel_token.bind_event(ev)
                try:
                    entered_refine.set()
                    deadline = time.monotonic() + 5.0
                    while time.monotonic() < deadline:
                        if ev.is_set() or (callable(is_cancelled) and is_cancelled()):
                            time.sleep(0.05)
                            order.append("control_exited_refine")
                            return RefinerResult(asr.text, "deepseek", "mock", 1, "shutdown")
                        ev.wait(timeout=0.01)
                    return RefinerResult(asr.text, "deepseek", "mock", 1, "")
                finally:
                    if cancel_token is not None:
                        cancel_token.unbind_event(ev)

            def close(self) -> None:
                order.append("refiner_close")

        with tempfile.TemporaryDirectory(prefix="avi-cl-", dir="/tmp") as td:
            rt = Path(td)
            os.chmod(rt, 0o700)
            wav_file = rt / "sample.wav"
            wav_file.write_bytes(_voiced_wav())
            tracker = _isolated_tracker(rt)
            orig_tr_close = tracker.close

            def _tracked_tr_close() -> None:
                order.append("tracker_close")
                orig_tr_close()

            tracker.close = _tracked_tr_close  # type: ignore[method-assign]
            cfg = Config(refiner=RefinerConfig(provider="deepseek"))
            daemon = VoiceInputDaemon(
                cfg,
                env={},
                runtime_dir=rt,
                platform=_FakePlatform(),
                asr_provider=_FakeAsr("关闭顺序测试"),
                refiner=_OrderedBlockingRefiner(),
                tracker=tracker,
            )
            srv_th = threading.Thread(target=daemon.serve_forever, daemon=True)
            srv_th.start()
            client_th: threading.Thread | None = None
            try:
                _wait_for_control_socket(rt)

                def _call_st() -> None:
                    try:
                        send_control_command(
                            rt,
                            {"cmd": "selftest_wav", "wav_path": str(wav_file), "no_telemetry": True},
                            timeout_sec=4.0,
                        )
                    except Exception:
                        pass

                client_th = threading.Thread(target=_call_st, daemon=True)
                client_th.start()
                self.assertTrue(entered_refine.wait(timeout=2.0), "selftest_wav never entered refine")

                daemon.close()
                srv_th.join(timeout=3.0)
                client_th.join(timeout=2.0)

                self.assertIn("control_exited_refine", order)
                self.assertIn("refiner_close", order)
                self.assertIn("tracker_close", order)
                self.assertLess(order.index("control_exited_refine"), order.index("refiner_close"))
                self.assertLess(order.index("control_exited_refine"), order.index("tracker_close"))
            finally:
                daemon.close()
                srv_th.join(timeout=2.0)
                if client_th is not None:
                    client_th.join(timeout=2.0)

    def test_probe_route_and_selftest_sanitize_untrusted_matched_by_and_session_key(self) -> None:
        import hashlib
        from agent_voice_input.session_ctx import SessionSnapshot
        from agent_voice_input.workspace_ctx import SessionMatch

        class _UntrustedStubTracker:
            def __init__(self, snap: SessionSnapshot) -> None:
                self._snap = snap
                self.home = Path("/tmp")
                self.proc_root = None
                self.last_sync_remote_vetoed = False
                self.last_sync_target_drifted = False

            def sync_session(self, **kw: object) -> SessionSnapshot:
                del kw
                return self._snap

            def build_asr_context(
                self,
                snapshot: SessionSnapshot | None,
                *,
                extra_hotwords: object = (),
                mode: str = "agent",
                language: str = "auto",
                target_hint: str = "",
            ) -> AsrContext:
                del snapshot, extra_hotwords, language, target_hint
                return AsrContext(hotwords=("demo",), mode=mode)

            def get_refiner_context_with_prior(self, *args: object, **kw: object) -> tuple[str, list[str], str]:
                del args, kw
                return "", [], ""

            def get_state(self, key: str) -> None:
                del key
                return None

            def active_session_cwds(self) -> list[str]:
                return []

            def close(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            wav_file = rt / "sample.wav"
            wav_file.write_bytes(_voiced_wav())
            expected_hash = hashlib.sha256(b"/a/b").hexdigest()[:8]

            for origin, expected_mb in (("remote", "remote"), ("local", "other")):
                bad_match = SessionMatch(
                    cli="codex",
                    session_id="/a/b",
                    jsonl_path="",
                    cwd="",
                    matched_by="/demo/work",
                    origin=origin,
                    host_name="box1" if origin == "remote" else "",
                )
                bad_snap = SessionSnapshot(
                    match=bad_match,
                    clean_turns=(),
                    luna_synced_idx=0,
                    generation=1,
                    git_status="",
                    hotwords=(),
                    byte_offset=0,
                )
                cfg = Config(refiner=RefinerConfig(provider="off"))
                daemon = VoiceInputDaemon(
                    cfg,
                    env={},
                    runtime_dir=rt,
                    platform=_FakePlatform(),
                    asr_provider=_FakeAsr("安全脱敏文本"),
                    refiner=create_refiner(cfg),
                    tracker=_UntrustedStubTracker(bad_snap),  # type: ignore[arg-type]
                )
                try:
                    r_pr = daemon.handle_control(
                        {
                            "cmd": "probe_route",
                            "process": "WindowsTerminal.exe",
                            "title": "codex",
                        }
                    )
                    self.assertTrue(r_pr["ok"], r_pr)
                    self.assertEqual(r_pr["matched_by"], expected_mb)
                    self.assertEqual(r_pr["session_key_masked"], f"codex:{expected_hash}")
                    pr_blob = json.dumps(r_pr, ensure_ascii=False)
                    self.assertNotIn("/demo/work", pr_blob)
                    self.assertNotIn("/a/b", pr_blob)

                    r_st = daemon.handle_control(
                        {
                            "cmd": "selftest_wav",
                            "wav_path": str(wav_file),
                            "process": "WindowsTerminal.exe",
                            "title": "codex",
                            "no_telemetry": True,
                        }
                    )
                    self.assertTrue(r_st["ok"], r_st)
                    self.assertEqual(r_st["routing"]["session_key_masked"], f"codex:{expected_hash}")
                    st_routing_blob = json.dumps(r_st["routing"], ensure_ascii=False)
                    self.assertNotIn("/demo/work", st_routing_blob)
                    self.assertNotIn("/a/b", st_routing_blob)
                finally:
                    daemon.close()

    def test_probe_route_uses_readonly_active_cwds_snapshot_without_mutating_tracker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            rt = tmp / "rt"
            rt.mkdir(mode=0o700)
            # Non-default workspace path outside ~/repos, ~/projects, etc.
            custom_repo = tmp / "nondefault_vault" / "stealth-ide-proj"
            custom_repo.mkdir(parents=True)

            term_sid = "abcd5678-1111-2222-3333-444444444444"
            term_rollout = self._write_codex_rollout(
                home / ".codex" / "sessions" / "2026" / "11" / "23" / f"rollout-2026-11-23T11-00-00-{term_sid}.jsonl",
                term_sid,
                str(custom_repo),
                [("user", "work in nondefault vault")],
            )
            write_session_registry_entry(
                rt,
                cli="codex",
                session_id=term_sid,
                cwd=str(custom_repo),
                jsonl_path=str(term_rollout),
                pid=os.getpid(),
            )

            env = {"HOME": str(home), "XDG_RUNTIME_DIR": str(rt)}
            cfg = Config(refiner=RefinerConfig(provider="off"))
            # Verify that without an active terminal session in tracker, IDE lookup does not find the nondefault dir
            cold_tracker = SessionTracker(home_dir=home, runtime_dir=rt, proc_root=tmp / "empty_proc", env=env)
            cold_daemon = VoiceInputDaemon(
                cfg,
                env=env,
                runtime_dir=rt,
                platform=_FakePlatform(),
                asr_provider=_FakeAsr("ok"),
                refiner=create_refiner(cfg),
                tracker=cold_tracker,
            )
            try:
                r_cold = cold_daemon.handle_control(
                    {
                        "cmd": "probe_route",
                        "process": "Code.exe",
                        "title": "app.py - stealth-ide-proj - Visual Studio Code",
                    }
                )
                self.assertTrue(r_cold["ok"], r_cold)
                self.assertEqual(r_cold["workspace_name"], "")
            finally:
                cold_daemon.close()

            # Production tracker has synced the active terminal session in custom_repo
            prod_tracker = SessionTracker(home_dir=home, runtime_dir=rt, proc_root=tmp / "empty_proc", env=env)
            snap = prod_tracker.sync_session(window_title="codex [#abcd5678]")
            self.assertIsNotNone(snap)
            self.assertEqual(
                [os.path.realpath(c) for c in prod_tracker.active_session_cwds()],
                [os.path.realpath(custom_repo)],
            )
            before_states = {
                k: (
                    v.session_id,
                    v.cwd,
                    v.byte_offset,
                    v.generation,
                    v.cold_start_prior_checked,
                    v.cold_start_prior_hotwords_checked,
                )
                for k, v in prod_tracker._states.items()
            }

            daemon = VoiceInputDaemon(
                cfg,
                env=env,
                runtime_dir=rt,
                platform=_FakePlatform(),
                asr_provider=_FakeAsr("ok"),
                refiner=create_refiner(cfg),
                tracker=prod_tracker,
            )
            try:
                r_ide = daemon.handle_control(
                    {
                        "cmd": "probe_route",
                        "process": "Code.exe",
                        "title": "app.py - stealth-ide-proj - Visual Studio Code",
                    }
                )
                self.assertTrue(r_ide["ok"], r_ide)
                self.assertEqual(r_ide["mode"], "agent")
                self.assertEqual(r_ide["workspace_name"], "stealth-ide-proj")
                after_states = {
                    k: (
                        v.session_id,
                        v.cwd,
                        v.byte_offset,
                        v.generation,
                        v.cold_start_prior_checked,
                        v.cold_start_prior_hotwords_checked,
                    )
                    for k, v in daemon.tracker._states.items()
                }
                self.assertEqual(after_states, before_states)
            finally:
                daemon.close()

    def test_close_skips_refiner_and_tracker_close_when_control_thread_exceeds_join_deadline(self) -> None:
        from unittest import mock
        from agent_voice_input.refiner import BaseRefiner

        entered = threading.Event()
        release_stuck = threading.Event()
        closed_components: list[str] = []

        class _StuckRefiner(BaseRefiner):
            provider_name = "deepseek"

            def refine(self, asr: AsrResult, **kw: object) -> RefinerResult:
                del kw
                entered.set()
                release_stuck.wait(timeout=5.0)
                return RefinerResult(asr.text, "deepseek", "mock", 1, "shutdown")

            def close(self) -> None:
                closed_components.append("refiner")

        with tempfile.TemporaryDirectory(prefix="avi-stuck-", dir="/tmp") as td:
            rt = Path(td)
            os.chmod(rt, 0o700)
            wav_file = rt / "sample.wav"
            wav_file.write_bytes(_voiced_wav())
            tracker = _isolated_tracker(rt)
            orig_tr_close = tracker.close

            def _tracked_tr_close() -> None:
                closed_components.append("tracker")
                orig_tr_close()

            tracker.close = _tracked_tr_close  # type: ignore[method-assign]
            plat = _FakePlatform()
            orig_plat_close = plat.close

            def _tracked_plat_close() -> None:
                closed_components.append("platform")
                orig_plat_close()

            plat.close = _tracked_plat_close  # type: ignore[method-assign]

            cfg = Config(refiner=RefinerConfig(provider="deepseek"))
            daemon = VoiceInputDaemon(
                cfg,
                env={},
                runtime_dir=rt,
                platform=plat,
                asr_provider=_FakeAsr("卡死控制线程"),
                refiner=_StuckRefiner(),
                tracker=tracker,
            )
            srv_th = threading.Thread(target=daemon.serve_forever, daemon=True)
            srv_th.start()
            client_th: threading.Thread | None = None
            try:
                _wait_for_control_socket(rt)

                def _call_st() -> None:
                    try:
                        send_control_command(
                            rt,
                            {"cmd": "selftest_wav", "wav_path": str(wav_file), "no_telemetry": True},
                            timeout_sec=4.0,
                        )
                    except Exception:
                        pass

                client_th = threading.Thread(target=_call_st, daemon=True)
                client_th.start()
                self.assertTrue(entered.wait(timeout=2.0), "selftest_wav never entered stuck refine")

                with mock.patch("agent_voice_input.daemon._CLOSE_JOIN_TIMEOUT_SEC", 0.08):
                    t0 = time.monotonic()
                    daemon.close()
                    elapsed = time.monotonic() - t0

                self.assertLess(elapsed, 1.5, "close() must stay bounded when a control thread hangs")
                self.assertNotIn("refiner", closed_components)
                self.assertIn("tracker", closed_components)
                self.assertIn("platform", closed_components)

                # Once the stuck selftest_wav thread exits, deferred cleanup MUST close refiner automatically (once).
                release_stuck.set()
                srv_th.join(timeout=3.0)
                if client_th is not None:
                    client_th.join(timeout=3.0)
                deadline_ref = time.monotonic() + 2.0
                while time.monotonic() < deadline_ref and "refiner" not in closed_components:
                    time.sleep(0.01)
                self.assertEqual(closed_components.count("refiner"), 1)
                self.assertEqual(closed_components.count("tracker"), 1)
            finally:
                release_stuck.set()
                srv_th.join(timeout=3.0)
                if client_th is not None:
                    client_th.join(timeout=3.0)
                with daemon._lock:
                    rem_ctrl = list(daemon._control_threads)
                for th in rem_ctrl:
                    th.join(timeout=2.0)
                daemon.close()

    def test_agy20_same_window_verifier_blocks_session_switch_in_live_and_selftest_deliver(self) -> None:
        import contextlib
        import sqlite3
        from agent_voice_input.daemon import _make_desktop_same_window_verifier

        def _seed_agy_db(home: Path, repo_a: Path, repo_b: Path) -> tuple[str, str]:
            cid_a = "aaaa1111-2222-3333-4444-555555555555"
            cid_b = "bbbb1111-2222-3333-4444-555555555555"
            ide_root = home / ".gemini" / "antigravity"
            for cid, text in (
                (cid_a, "Discuss AlphaRouter session A"),
                (cid_b, "Discuss BetaRouter session B"),
            ):
                p = ide_root / "brain" / cid / ".system_generated" / "logs" / "transcript_full.jsonl"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(
                    json.dumps(
                        {
                            "source": "USER_EXPLICIT",
                            "type": "USER_INPUT",
                            "content": f"<USER_REQUEST>{text}</USER_REQUEST>",
                        },
                        ensure_ascii=False,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            db_path = ide_root / "conversation_summaries.db"
            with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
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
                    [
                        (
                            cid_a,
                            "Refactor AlphaRouter Pipeline",
                            json.dumps([repo_a.as_uri()]),
                            "2026-11-23T10:05:00Z",
                            "USER_EXPLICIT",
                            None,
                            0,
                            "antigravity",
                        ),
                        (
                            cid_b,
                            "Debug BetaRouter Timeout",
                            json.dumps([repo_b.as_uri()]),
                            "2026-11-23T10:06:00Z",
                            "USER_EXPLICIT",
                            None,
                            0,
                            "antigravity",
                        ),
                    ],
                )
                conn.commit()
            return cid_a, cid_b

        class _VerifyingAgyPlatform(_FakePlatform):
            def __init__(self, initial_fg: ForegroundWindow) -> None:
                super().__init__()
                self.fg = initial_fg
                self.verifiers_seen: int = 0
                self.pasted_texts: list[str] = []
                self.copied_texts: list[str] = []

            def get_foreground_window(self) -> ForegroundWindow:
                return self.fg

            def deliver_text(
                self,
                text: str,
                seq_id: int,
                target_hwnd: int = 0,
                *,
                origin_window: ForegroundWindow | None = None,
                same_window_verifier: object = None,
                before_commit: object = None,
                **kw: object,
            ) -> PasteOutcome:
                del seq_id, target_hwnd, kw
                if callable(before_commit) and not before_commit():
                    return PasteOutcome(False, False, "cancelled", "")
                if origin_window is not None and origin_window.origin_drifted:
                    self.copied_texts.append(text)
                    return PasteOutcome(False, True, "focus_changed", self.fg.process)
                if callable(same_window_verifier):
                    self.verifiers_seen += 1
                    if not same_window_verifier(self.fg):
                        self.copied_texts.append(text)
                        return PasteOutcome(False, True, "focus_changed", self.fg.process)
                self.pasted_texts.append(text)
                return PasteOutcome(True, True, "ok", self.fg.process, used_clipboard_paste=True)

        class _HookAsr(BaseAsrProvider):
            provider_name = "dashscope"

            def __init__(self, text: str, on_transcribe: object = None) -> None:
                self._text = text
                self._on_transcribe = on_transcribe

            def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None, **kw: object) -> AsrResult:
                del wav_bytes, context, kw
                if callable(self._on_transcribe):
                    self._on_transcribe()
                return AsrResult(self._text, "dashscope", "mock", 5)

        self.assertTrue(callable(_make_desktop_same_window_verifier))
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            rt = tmp / "rt"
            rt.mkdir(mode=0o700)
            repo_a = home / "repos" / "alpha-router"
            repo_b = home / "repos" / "beta-router"
            repo_a.mkdir(parents=True)
            repo_b.mkdir(parents=True)
            _seed_agy_db(home, repo_a, repo_b)
            wav_file = rt / "sample.wav"
            wav_file.write_bytes(_voiced_wav())

            win_a = ForegroundWindow(hwnd=701, process="Antigravity.exe", title="Refactor AlphaRouter Pipeline")
            win_b = ForegroundWindow(hwnd=701, process="Antigravity.exe", title="Debug BetaRouter Timeout")
            win_a_trunc = ForegroundWindow(hwnd=701, process="Antigravity.exe", title="Refactor AlphaRouter...")

            env = {"HOME": str(home), "XDG_RUNTIME_DIR": str(rt)}
            cfg = Config(refiner=RefinerConfig(provider="off"))

            # 1. Live dictation: same-window switch A -> B must fail closed to clipboard (focus_changed)
            plat = _VerifyingAgyPlatform(win_a)
            next_fg = [win_b]
            asr = _HookAsr("不要串台到会话B", on_transcribe=lambda: setattr(plat, "fg", next_fg[0]))
            tracker = SessionTracker(home_dir=home, runtime_dir=rt, proc_root=tmp / "empty_proc", env=env)
            daemon = VoiceInputDaemon(
                cfg,
                env=env,
                runtime_dir=rt,
                platform=plat,
                asr_provider=asr,
                refiner=create_refiner(cfg),
                tracker=tracker,
            )
            try:
                daemon.handle_trigger("toggle", win_a)
                daemon._last_trigger_ts = -1e9
                daemon.handle_trigger("toggle")
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline and daemon.state != "idle":
                    time.sleep(0.01)
                self.assertEqual(daemon.state, "idle")
                self.assertEqual(plat.verifiers_seen, 1)
                self.assertEqual(plat.pasted_texts, [])
                self.assertEqual(plat.copied_texts, ["不要串台到会话B"])
                self.assertEqual(daemon.last_result["paste_reason"], "focus_changed")
                self.assertEqual(daemon.last_result["delivery_status"], "copied")

                # 2. Live dictation: same-window truncated title still resolving to session A pastes normally
                plat.fg = win_a
                next_fg[0] = win_a_trunc
                daemon._last_trigger_ts = -1e9
                daemon.handle_trigger("toggle", win_a)
                daemon._last_trigger_ts = -1e9
                daemon.handle_trigger("toggle")
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline and daemon.state != "idle":
                    time.sleep(0.01)
                self.assertEqual(daemon.state, "idle")
                self.assertEqual(plat.verifiers_seen, 2)
                self.assertEqual(plat.pasted_texts, ["不要串台到会话B"])
                self.assertEqual(daemon.last_result["delivery_status"], "pasted")

                # 3. selftest_wav(deliver=True) with same-window switch A -> B safely falls back to clipboard
                plat.fg = win_a
                next_fg[0] = win_b
                r_st = daemon.handle_control(
                    {
                        "cmd": "selftest_wav",
                        "wav_path": str(wav_file),
                        "use_live_window": True,
                        "deliver": True,
                        "no_telemetry": True,
                    }
                )
                self.assertTrue(r_st["ok"], r_st)
                self.assertEqual(plat.verifiers_seen, 3)
                self.assertFalse(r_st["pasted"])
                self.assertTrue(r_st["clipboard"])
                self.assertEqual(r_st["paste_reason"], "focus_changed")
                self.assertEqual(r_st["delivery_status"], "copied")
            finally:
                daemon.close()

    def test_selftest_wav_no_telemetry_isolates_tracker_and_preserves_live_ssh_prefetch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            rt = tmp / "rt"
            rt.mkdir(mode=0o700)
            wav_file = rt / "sample.wav"
            wav_file.write_bytes(_voiced_wav())

            real_repo = tmp / "nondefault_vault" / "prod-ide-proj"
            real_repo.mkdir(parents=True)
            sandbox_repo = tmp / "sandbox_vault" / "selftest-proj"
            sandbox_repo.mkdir(parents=True)

            prod_sid = "1111aaaa-1111-2222-3333-444444444444"
            prod_rollout = self._write_codex_rollout(
                home / ".codex" / "sessions" / "2026" / "11" / "23" / f"rollout-2026-11-23T10-00-00-{prod_sid}.jsonl",
                prod_sid,
                str(real_repo),
                [("user", "prod turn")],
            )
            write_session_registry_entry(
                rt, cli="codex", session_id=prod_sid, cwd=str(real_repo), jsonl_path=str(prod_rollout), pid=os.getpid()
            )
            sb_sid = "2222bbbb-1111-2222-3333-444444444444"
            sb_rollout = self._write_codex_rollout(
                home / ".codex" / "sessions" / "2026" / "11" / "23" / f"rollout-2026-11-23T10-01-00-{sb_sid}.jsonl",
                sb_sid,
                str(sandbox_repo),
                [("user", "sandbox selftest turn")],
            )
            write_session_registry_entry(
                rt, cli="codex", session_id=sb_sid, cwd=str(sandbox_repo), jsonl_path=str(sb_rollout), pid=os.getpid()
            )

            env = {"HOME": str(home), "XDG_RUNTIME_DIR": str(rt)}
            cfg = Config(refiner=RefinerConfig(provider="off"))
            prod_tracker = SessionTracker(home_dir=home, runtime_dir=rt, proc_root=tmp / "empty_proc", env=env)
            self.assertIsNotNone(prod_tracker.sync_session(window_title="codex [#1111aaaa]"))
            before_keys = set(prod_tracker._states.keys())
            before_cwds = list(prod_tracker.active_session_cwds())
            cancel_prefetch_calls: list[object] = []
            orig_cancel_prefetch = prod_tracker.cancel_remote_prefetch

            def _spy_cancel_prefetch(*args: object, **kwargs: object) -> None:
                cancel_prefetch_calls.append((args, kwargs))
                orig_cancel_prefetch(*args, **kwargs)  # type: ignore[arg-type]

            prod_tracker.cancel_remote_prefetch = _spy_cancel_prefetch  # type: ignore[method-assign]
            daemon = VoiceInputDaemon(
                cfg,
                env=env,
                runtime_dir=rt,
                platform=_FakePlatform(),
                asr_provider=_FakeAsr("隔离自检"),
                refiner=create_refiner(cfg),
                tracker=prod_tracker,
            )
            closed_temp_trackers: list[SessionTracker] = []
            orig_make_probe = daemon._make_probe_tracker

            def _spy_make_probe() -> tuple[object, bool]:
                tr, owns = orig_make_probe()
                if owns and isinstance(tr, SessionTracker):
                    orig_cl = tr.close

                    def _track_close() -> None:
                        closed_temp_trackers.append(tr)
                        orig_cl()

                    tr.close = _track_close  # type: ignore[method-assign]
                return tr, owns

            daemon._make_probe_tracker = _spy_make_probe  # type: ignore[method-assign]
            try:
                # 1. selftest_wav(no_telemetry=True) on sandbox session must NOT pollute prod_tracker
                r1 = daemon.handle_control(
                    {
                        "cmd": "selftest_wav",
                        "wav_path": str(wav_file),
                        "process": "WindowsTerminal.exe",
                        "title": "codex [#2222bbbb]",
                        "no_telemetry": True,
                    }
                )
                self.assertTrue(r1["ok"], r1)
                self.assertEqual(r1["routing"]["session_key_masked"], "codex:2222bbbb")
                self.assertEqual(r1["routing"]["workspace_name"], "selftest-proj")
                self.assertEqual(set(prod_tracker._states.keys()), before_keys)
                self.assertEqual(prod_tracker.active_session_cwds(), before_cwds)
                self.assertEqual(cancel_prefetch_calls, [])
                self.assertEqual(len(closed_temp_trackers), 1)

                # 2. IDE selftest_wav(no_telemetry=True) still benefits from read-only active_session_cwds snapshot
                r2 = daemon.handle_control(
                    {
                        "cmd": "selftest_wav",
                        "wav_path": str(wav_file),
                        "process": "Code.exe",
                        "title": "main.py - prod-ide-proj - Visual Studio Code",
                        "no_telemetry": True,
                    }
                )
                self.assertTrue(r2["ok"], r2)
                self.assertEqual(r2["routing"]["workspace_name"], "prod-ide-proj")
                self.assertEqual(set(prod_tracker._states.keys()), before_keys)
                self.assertEqual(len(closed_temp_trackers), 2)
            finally:
                daemon.close()

    def test_selftest_wav_deliver_aborts_late_paste_on_client_timeout_or_disconnect(self) -> None:
        import socket

        class _GatedAsr(BaseAsrProvider):
            provider_name = "dashscope"

            def __init__(self) -> None:
                self.entered = threading.Event()
                self.release = threading.Event()

            def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None, **kw: object) -> AsrResult:
                del wav_bytes, context, kw
                self.entered.set()
                self.release.wait(timeout=5.0)
                return AsrResult("迟到不应粘贴的文本", "dashscope", "mock", 5)

        class _RecordDeliverPlatform(_FakePlatform):
            def __init__(self) -> None:
                super().__init__()
                self.fg = ForegroundWindow(hwnd=808, process="WindowsTerminal.exe", title="codex")
                self.deliver_calls: list[str] = []

            def get_foreground_window(self) -> ForegroundWindow:
                return self.fg

            def deliver_text(self, text: str, seq_id: int, target_hwnd: int = 0, **kw: object) -> PasteOutcome:
                del seq_id, target_hwnd, kw
                self.deliver_calls.append(text)
                return PasteOutcome(True, True, "ok", self.fg.process, used_clipboard_paste=True)

        with tempfile.TemporaryDirectory(prefix="avi-disc-", dir="/tmp") as td:
            rt = Path(td)
            os.chmod(rt, 0o700)
            wav_file = rt / "sample.wav"
            wav_file.write_bytes(_voiced_wav())
            plat = _RecordDeliverPlatform()
            asr = _GatedAsr()
            cfg = Config(refiner=RefinerConfig(provider="off"))
            daemon = VoiceInputDaemon(
                cfg,
                env={},
                runtime_dir=rt,
                platform=plat,
                asr_provider=asr,
                refiner=create_refiner(cfg),
                tracker=_isolated_tracker(rt),
            )
            srv_th = threading.Thread(target=daemon.serve_forever, daemon=True)
            srv_th.start()
            try:
                _wait_for_control_socket(rt)

                # Case 1: Client times out via send_control_command(timeout_sec=0.15) while ASR is blocked
                with self.assertRaises((TimeoutError, socket.timeout)):
                    send_control_command(
                        rt,
                        {
                            "cmd": "selftest_wav",
                            "wav_path": str(wav_file),
                            "use_live_window": True,
                            "deliver": True,
                            "no_telemetry": True,
                        },
                        timeout_sec=0.15,
                    )
                self.assertTrue(asr.entered.wait(timeout=2.0))
                asr.release.set()
                # Wait until selftest worker releases _selftest_lock
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    if daemon._selftest_lock.acquire(blocking=False):
                        daemon._selftest_lock.release()
                        break
                    time.sleep(0.01)
                self.assertEqual(plat.deliver_calls, [], "Timed-out selftest_wav must NEVER paste after client left")

                # Case 2: Client disconnects explicitly while timeout_sec is still generous (10s)
                asr.entered.clear()
                asr.release.clear()
                raw_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    raw_sock.settimeout(2.0)
                    raw_sock.connect(str(rt / "control.sock"))
                    req_bytes = (
                        json.dumps(
                            {
                                "cmd": "selftest_wav",
                                "wav_path": str(wav_file),
                                "use_live_window": True,
                                "deliver": True,
                                "no_telemetry": True,
                                "timeout_sec": 10.0,
                            }
                        )
                        + "\n"
                    ).encode("utf-8")
                    raw_sock.sendall(req_bytes)
                    self.assertTrue(asr.entered.wait(timeout=2.0))
                finally:
                    raw_sock.close()
                # Give kernel brief moment to deliver EOF/FIN on AF_UNIX peer before releasing ASR
                time.sleep(0.03)
                asr.release.set()
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    if daemon._selftest_lock.acquire(blocking=False):
                        daemon._selftest_lock.release()
                        break
                    time.sleep(0.01)
                self.assertEqual(
                    plat.deliver_calls, [], "Disconnected client's selftest_wav must NEVER paste after peer EOF"
                )

                # Case 3: Client disconnects while selftest_wav is queued waiting for _deliver_lock
                asr.entered.clear()
                asr.release.set()
                selftest_reached_deliver = threading.Event()
                orig_deliver_st = daemon._deliver_for_selftest

                def _spy_deliver_st(*args: object, **kwargs: object) -> PasteOutcome:
                    selftest_reached_deliver.set()
                    return orig_deliver_st(*args, **kwargs)  # type: ignore[arg-type]

                daemon._deliver_for_selftest = _spy_deliver_st  # type: ignore[method-assign]
                daemon._deliver_lock.acquire()
                raw_sock2 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    raw_sock2.settimeout(2.0)
                    raw_sock2.connect(str(rt / "control.sock"))
                    raw_sock2.sendall(
                        (
                            json.dumps(
                                {
                                    "cmd": "selftest_wav",
                                    "wav_path": str(wav_file),
                                    "use_live_window": True,
                                    "deliver": True,
                                    "no_telemetry": True,
                                    "timeout_sec": 10.0,
                                }
                            )
                            + "\n"
                        ).encode("utf-8")
                    )
                    self.assertTrue(selftest_reached_deliver.wait(timeout=2.0))
                    raw_sock2.close()
                    time.sleep(0.03)
                finally:
                    raw_sock2.close()
                    daemon._deliver_lock.release()

                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    if daemon._selftest_lock.acquire(blocking=False):
                        daemon._selftest_lock.release()
                        break
                    time.sleep(0.01)
                self.assertEqual(
                    plat.deliver_calls, [], "Client disconnect while waiting on _deliver_lock must abort delivery"
                )
            finally:
                asr.release.set()
                daemon.close()
                srv_th.join(timeout=2.0)

    def test_selftest_wav_does_not_break_live_auto_stop_or_recorder_lost_absorption(self) -> None:
        now = [500.0]

        def _clock() -> float:
            return now[0]

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            wav_file = rt / "sample.wav"
            wav_file.write_bytes(_voiced_wav())
            plat = _FakePlatform()
            cfg = Config(refiner=RefinerConfig(provider="off"))
            daemon = VoiceInputDaemon(
                cfg,
                env={},
                runtime_dir=rt,
                platform=plat,
                asr_provider=_FakeAsr("录音测试"),
                refiner=create_refiner(cfg),
                tracker=_isolated_tracker(rt),
                clock_fn=_clock,
                sleep_fn=lambda _: None,
            )
            try:
                # 1. Auto-stop absorption survives concurrent selftest_wav(no_telemetry=False)
                r_start = daemon.handle_trigger("toggle")
                self.assertTrue(r_start["ok"])
                rec_seq = r_start["seq_id"]
                now[0] += 1.0
                r_auto = daemon.handle_trigger("toggle", expected_seq_id=rec_seq)
                self.assertTrue(r_auto["ok"])
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and daemon.state != "idle":
                    time.sleep(0.01)

                r_st = daemon.handle_control({"cmd": "selftest_wav", "wav_path": str(wav_file)})
                self.assertTrue(r_st["ok"], r_st)
                self.assertGreater(daemon.seq_id, rec_seq)

                now[0] += 0.4  # still inside 1.5s auto-stop absorption window
                r_absorb = daemon.handle_trigger("toggle")
                self.assertTrue(r_absorb["ok"])
                self.assertEqual(r_absorb.get("ignored"), "already_stopped")
                self.assertFalse(daemon._recording_active)

                # 2. Recorder-lost absorption survives selftest_wav(no_telemetry=False) during/after stop_recording
                now[0] += 2.0
                r_start2 = daemon.handle_trigger("toggle")
                self.assertTrue(r_start2["ok"])
                rec_seq2 = r_start2["seq_id"]
                plat.wav_bytes = b""
                orig_stop = plat.stop_recording

                def _stop_with_concurrent_selftest(**kw: object) -> tuple[bytes, str]:
                    # Concurrent selftest_wav increments daemon.seq_id while _on_recorder_lost runs stop_recording!
                    st_mid = daemon.handle_control({"cmd": "selftest_wav", "wav_path": str(wav_file)})
                    self.assertTrue(st_mid["ok"], st_mid)
                    return orig_stop(**kw)  # type: ignore[arg-type]

                plat.stop_recording = _stop_with_concurrent_selftest  # type: ignore[method-assign]
                daemon._on_recorder_lost(seq_id=rec_seq2)
                self.assertGreater(daemon.seq_id, rec_seq2)

                now[0] += 0.4  # still inside 1.5s recorder-lost absorption window
                r_absorb_lost = daemon.handle_trigger("toggle")
                self.assertTrue(r_absorb_lost["ok"])
                self.assertEqual(r_absorb_lost.get("ignored"), "already_stopped")
                self.assertFalse(daemon._recording_active)
            finally:
                daemon.close()

    def test_close_probe_route_stuck_does_not_block_shared_refiner_or_tracker_close(self) -> None:
        from unittest import mock
        from agent_voice_input.refiner import BaseRefiner

        entered_probe = threading.Event()
        release_probe = threading.Event()
        closed_components: list[str] = []

        class _TrackedRefiner(BaseRefiner):
            provider_name = "deepseek"

            def refine(self, asr: AsrResult, **kw: object) -> RefinerResult:
                del kw
                return RefinerResult(asr.text, "deepseek", "mock", 1, "")

            def close(self) -> None:
                closed_components.append("refiner")

        with tempfile.TemporaryDirectory(prefix="avi-prcl-", dir="/tmp") as td:
            rt = Path(td)
            os.chmod(rt, 0o700)
            tracker = _isolated_tracker(rt)
            orig_tr_close = tracker.close

            def _tracked_tr_close() -> None:
                closed_components.append("tracker")
                orig_tr_close()

            tracker.close = _tracked_tr_close  # type: ignore[method-assign]
            cfg = Config(refiner=RefinerConfig(provider="deepseek"))
            daemon = VoiceInputDaemon(
                cfg,
                env={},
                runtime_dir=rt,
                platform=_FakePlatform(),
                asr_provider=_FakeAsr("ok"),
                refiner=_TrackedRefiner(),
                tracker=tracker,
            )
            orig_make_probe = daemon._make_probe_tracker

            def _slow_make_probe() -> tuple[object, bool]:
                tr, owns = orig_make_probe()
                if owns and isinstance(tr, SessionTracker):
                    orig_sync = tr.sync_session

                    def _blocking_sync(*args: object, **kwargs: object) -> object:
                        entered_probe.set()
                        release_probe.wait(timeout=5.0)
                        return orig_sync(*args, **kwargs)  # type: ignore[arg-type]

                    tr.sync_session = _blocking_sync  # type: ignore[method-assign]
                return tr, owns

            daemon._make_probe_tracker = _slow_make_probe  # type: ignore[method-assign]
            srv_th = threading.Thread(target=daemon.serve_forever, daemon=True)
            srv_th.start()
            client_th: threading.Thread | None = None
            try:
                _wait_for_control_socket(rt)

                def _call_probe() -> None:
                    try:
                        send_control_command(
                            rt,
                            {"cmd": "probe_route", "process": "WindowsTerminal.exe", "title": "codex"},
                            timeout_sec=4.0,
                        )
                    except Exception:
                        pass

                client_th = threading.Thread(target=_call_probe, daemon=True)
                client_th.start()
                self.assertTrue(entered_probe.wait(timeout=2.0), "probe_route never entered blocking sync")

                with mock.patch("agent_voice_input.daemon._CLOSE_JOIN_TIMEOUT_SEC", 0.08):
                    daemon.close()

                # probe_route uses an isolated tracker and never touches shared refiner/tracker -> both close immediately!
                self.assertEqual(closed_components.count("refiner"), 1)
                self.assertEqual(closed_components.count("tracker"), 1)
            finally:
                release_probe.set()
                srv_th.join(timeout=3.0)
                if client_th is not None:
                    client_th.join(timeout=3.0)
                daemon.close()

    def test_control_socket_loops_recv_until_newline_handles_chunks_large_and_oversize_frames(self) -> None:
        import socket
        from agent_voice_input.daemon import CONTROL_REQUEST_MAX_BYTES

        def _raw_exchange(sock_path: Path, chunks: list[bytes], delay_sec: float = 0.0) -> dict[str, object]:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(3.0)
                s.connect(str(sock_path))
                for idx, ch in enumerate(chunks):
                    s.sendall(ch)
                    if delay_sec > 0 and idx + 1 < len(chunks):
                        time.sleep(delay_sec)
                buf = b""
                while b"\n" not in buf and (piece := s.recv(16384)):
                    buf += piece
            return json.loads(buf.decode("utf-8").strip())

        with tempfile.TemporaryDirectory(prefix="avi-sock-", dir="/tmp") as td:
            rt = Path(td)
            os.chmod(rt, 0o700)
            cfg = Config(refiner=RefinerConfig(provider="off"))
            daemon = VoiceInputDaemon(
                cfg,
                env={},
                runtime_dir=rt,
                platform=_FakePlatform(),
                asr_provider=_FakeAsr("ok"),
                refiner=create_refiner(cfg),
                tracker=_isolated_tracker(rt),
            )
            srv_th = threading.Thread(target=daemon.serve_forever, daemon=True)
            srv_th.start()
            try:
                _wait_for_control_socket(rt)
                sock_path = rt / "control.sock"

                # 1. Fragmented multi-chunk JSON send across multiple TCP/AF_UNIX writes
                r_frag = _raw_exchange(sock_path, [b'{"cm', b'd": "sta', b'tus"}\n'], delay_sec=0.02)
                self.assertTrue(r_frag.get("ok"), r_frag)
                self.assertEqual(r_frag.get("state"), "idle")

                # 2. Valid JSON larger than 16 KiB (e.g., ~24 KiB title/padding)
                large_title = "X" * 24000
                large_payload = (
                    json.dumps({"cmd": "probe_route", "process": "WeChat.exe", "title": large_title}) + "\n"
                ).encode("utf-8")
                self.assertGreater(len(large_payload), 16384)
                r_large = _raw_exchange(sock_path, [large_payload[:9000], large_payload[9000:]], delay_sec=0.01)
                self.assertTrue(r_large.get("ok"), r_large)
                self.assertEqual(r_large.get("mode"), "general")

                # 3. Oversize request (> CONTROL_REQUEST_MAX_BYTES) rejected cleanly without killing server
                oversize = b'{"cmd":"status","pad":"' + (b"A" * (CONTROL_REQUEST_MAX_BYTES + 1024)) + b'"}\n'
                r_over = _raw_exchange(sock_path, [oversize])
                self.assertFalse(r_over.get("ok"), r_over)
                self.assertIn("exceeds", str(r_over.get("error") or "").lower())

                # Server remains healthy after oversize rejection
                r_after = send_control_command(rt, {"cmd": "status"}, timeout_sec=1.5)
                self.assertTrue(r_after.get("ok"), r_after)
            finally:
                daemon.close()
                srv_th.join(timeout=2.0)

    def test_probe_route_semaphore_limit_busy_and_exception_release(self) -> None:
        entered_count = [0]
        entered_lock = threading.Lock()
        all_four_entered = threading.Event()
        release_probes = threading.Event()
        fail_first = [True]

        with tempfile.TemporaryDirectory(prefix="avi-psem-", dir="/tmp") as td:
            rt = Path(td)
            os.chmod(rt, 0o700)
            cfg = Config(refiner=RefinerConfig(provider="off"))
            daemon = VoiceInputDaemon(
                cfg,
                env={},
                runtime_dir=rt,
                platform=_FakePlatform(),
                asr_provider=_FakeAsr("ok"),
                refiner=create_refiner(cfg),
                tracker=_isolated_tracker(rt),
            )
            orig_exec_probe = daemon._execute_probe_route

            def _gated_exec_probe(req: object, **kw: object) -> dict[str, object]:
                if isinstance(req, dict) and req.get("title") == "block_me":
                    with entered_lock:
                        entered_count[0] += 1
                        my_idx = entered_count[0]
                        if entered_count[0] == 4:
                            all_four_entered.set()
                    release_probes.wait(timeout=5.0)
                    if my_idx == 1 and fail_first[0]:
                        fail_first[0] = False
                        raise RuntimeError("injected probe failure")
                return orig_exec_probe(req, **kw)  # type: ignore[arg-type]

            daemon._execute_probe_route = _gated_exec_probe  # type: ignore[method-assign]
            srv_th = threading.Thread(target=daemon.serve_forever, daemon=True)
            srv_th.start()
            worker_ths: list[threading.Thread] = []
            results: list[dict[str, object]] = []
            res_lock = threading.Lock()
            try:
                _wait_for_control_socket(rt)

                def _call_blocking() -> None:
                    resp = send_control_command(
                        rt,
                        {"cmd": "probe_route", "process": "WeChat.exe", "title": "block_me"},
                        timeout_sec=5.0,
                    )
                    with res_lock:
                        results.append(resp)

                for _ in range(4):
                    th = threading.Thread(target=_call_blocking, daemon=True)
                    th.start()
                    worker_ths.append(th)

                self.assertTrue(all_four_entered.wait(timeout=3.0), "4 concurrent probe_route slots did not fill")

                # 5th concurrent probe_route MUST fail fast with "probe_route busy"
                r_fifth = send_control_command(
                    rt,
                    {"cmd": "probe_route", "process": "WeChat.exe", "title": "fifth"},
                    timeout_sec=1.5,
                )
                self.assertFalse(r_fifth.get("ok"), r_fifth)
                self.assertEqual(r_fifth.get("error"), "probe_route busy")

                # Release all 4 (one raises an exception) and verify semaphore recovers all 4 permits
                release_probes.set()
                for th in worker_ths:
                    th.join(timeout=3.0)
                self.assertEqual(len(results), 4)
                self.assertEqual(sum(1 for r in results if r.get("ok")), 3)
                self.assertEqual(sum(1 for r in results if not r.get("ok") and "injected" in str(r.get("error"))), 1)

                # Subsequent 4 sequential/fresh probe_route calls succeed cleanly
                r_after = send_control_command(
                    rt,
                    {"cmd": "probe_route", "process": "WeChat.exe", "title": "after_recovery"},
                    timeout_sec=1.5,
                )
                self.assertTrue(r_after.get("ok"), r_after)
                self.assertEqual(r_after.get("mode"), "general")
            finally:
                release_probes.set()
                daemon.close()
                srv_th.join(timeout=2.0)

    def test_control_peer_disconnected_distinguishes_shut_wr_from_close_and_e2e_delivery(self) -> None:
        import select
        import socket
        from unittest import mock
        from agent_voice_input.daemon import _is_control_peer_disconnected

        # 1. Direct socketpair verification: SHUT_WR must NOT be treated as disconnect; close() MUST be.
        srv_end, cli_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.assertFalse(_is_control_peer_disconnected(None))
            self.assertFalse(_is_control_peer_disconnected(srv_end))
            cli_end.sendall(b'{"cmd":"status"}\n')
            cli_end.shutdown(socket.SHUT_WR)
            self.assertEqual(srv_end.recv(1024), b'{"cmd":"status"}\n')
            self.assertFalse(
                _is_control_peer_disconnected(srv_end),
                "Client half-close shutdown(SHUT_WR) must NOT be treated as disconnected",
            )
            cli_end.close()
            time.sleep(0.02)
            self.assertTrue(
                _is_control_peer_disconnected(srv_end),
                "Full client close() MUST be detected as disconnected",
            )
            with (
                mock.patch.object(select, "kqueue", None, create=True),
                mock.patch.object(select, "poll", None, create=True),
            ):
                self.assertFalse(_is_control_peer_disconnected(srv_end))
        finally:
            srv_end.close()
            cli_end.close()

        # 2. End-to-end against real control.sock:
        #    (a) client sends selftest_wav + shutdown(SHUT_WR) -> succeeds & pastes normally;
        #    (b) client sends selftest_wav + close() while ASR is gated -> late paste is blocked.
        class _GatedAsr(BaseAsrProvider):
            provider_name = "dashscope"

            def __init__(self) -> None:
                self.entered = threading.Event()
                self.release = threading.Event()
                self.release.set()

            def transcribe(self, wav_bytes: bytes, context: AsrContext | None = None, **kw: object) -> AsrResult:
                del wav_bytes, context, kw
                self.entered.set()
                self.release.wait(timeout=5.0)
                return AsrResult("半关闭正常投递测试", "dashscope", "mock", 5)

        class _RecordDeliverPlatform(_FakePlatform):
            def __init__(self) -> None:
                super().__init__()
                self.fg = ForegroundWindow(hwnd=909, process="WindowsTerminal.exe", title="codex")
                self.deliver_calls: list[str] = []

            def get_foreground_window(self) -> ForegroundWindow:
                return self.fg

            def deliver_text(self, text: str, seq_id: int, target_hwnd: int = 0, **kw: object) -> PasteOutcome:
                del seq_id, target_hwnd, kw
                self.deliver_calls.append(text)
                return PasteOutcome(True, True, "ok", self.fg.process, used_clipboard_paste=True)

        with tempfile.TemporaryDirectory(prefix="avi-shutwr-", dir="/tmp") as td:
            rt = Path(td)
            os.chmod(rt, 0o700)
            wav_file = rt / "sample.wav"
            wav_file.write_bytes(_voiced_wav())
            plat = _RecordDeliverPlatform()
            asr = _GatedAsr()
            cfg = Config(refiner=RefinerConfig(provider="off"))
            daemon = VoiceInputDaemon(
                cfg,
                env={},
                runtime_dir=rt,
                platform=plat,
                asr_provider=asr,
                refiner=create_refiner(cfg),
                tracker=_isolated_tracker(rt),
            )
            srv_th = threading.Thread(target=daemon.serve_forever, daemon=True)
            srv_th.start()
            try:
                _wait_for_control_socket(rt)
                payload = (
                    json.dumps(
                        {
                            "cmd": "selftest_wav",
                            "wav_path": str(wav_file),
                            "use_live_window": True,
                            "deliver": True,
                            "no_telemetry": True,
                            "timeout_sec": 10.0,
                        }
                    )
                    + "\n"
                ).encode("utf-8")

                # (a) Half-close SHUT_WR client (like socat/nc) MUST receive ok=True and deliver normally
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s_half:
                    s_half.settimeout(3.0)
                    s_half.connect(str(rt / "control.sock"))
                    s_half.sendall(payload)
                    s_half.shutdown(socket.SHUT_WR)
                    buf = b""
                    while b"\n" not in buf and (chunk := s_half.recv(16384)):
                        buf += chunk
                resp_half = json.loads(buf.decode("utf-8").strip())
                self.assertTrue(resp_half.get("ok"), resp_half)
                self.assertTrue(resp_half.get("pasted"), resp_half)
                self.assertEqual(plat.deliver_calls, ["半关闭正常投递测试"])

                # (b) Full close() while ASR is blocked MUST block late paste
                plat.deliver_calls.clear()
                asr.entered.clear()
                asr.release.clear()
                s_close = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    s_close.settimeout(2.0)
                    s_close.connect(str(rt / "control.sock"))
                    s_close.sendall(payload)
                    self.assertTrue(asr.entered.wait(timeout=2.0))
                finally:
                    s_close.close()
                time.sleep(0.03)
                asr.release.set()
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    if daemon._selftest_lock.acquire(blocking=False):
                        daemon._selftest_lock.release()
                        break
                    time.sleep(0.01)
                self.assertEqual(
                    plat.deliver_calls,
                    [],
                    "Full close() peer disconnect must still block late paste",
                )
            finally:
                asr.release.set()
                daemon.close()
                srv_th.join(timeout=2.0)

    def test_control_peer_disconnected_kqueue_branch_flags_and_close(self) -> None:
        import select
        import types
        from unittest import mock
        from agent_voice_input.daemon import _is_control_peer_disconnected

        KQ_FILTER_WRITE = -2
        KQ_EV_ADD = 0x1
        KQ_EV_ONESHOT = 0x10
        KQ_EV_ERROR = 0x4000
        KQ_EV_EOF = 0x8000

        class _FakeConn:
            def __init__(self, fd: int = 42) -> None:
                self._fd = fd

            def fileno(self) -> int:
                return self._fd

        created: list[_FakeKQueue] = []
        captured_kevents: list[tuple[int, int, int]] = []
        next_outcome: list[object] = [[]]

        class _FakeKQueue:
            def __init__(self) -> None:
                self.closed = False
                self.control_calls: list[tuple[list[object], int, int]] = []
                created.append(self)

            def control(self, changelist: list[object], max_events: int, timeout: int) -> list[object]:
                self.control_calls.append((changelist, max_events, timeout))
                outcome = next_outcome[0]
                if isinstance(outcome, BaseException):
                    raise outcome
                assert isinstance(outcome, list)
                return outcome

            def close(self) -> None:
                self.closed = True

        def _fake_kevent(ident: int, *, filter: int, flags: int) -> object:
            captured_kevents.append((ident, filter, flags))
            return types.SimpleNamespace(ident=ident, filter=filter, flags=flags)

        with (
            mock.patch.object(select, "kqueue", _FakeKQueue, create=True),
            mock.patch.object(select, "kevent", _fake_kevent, create=True),
            mock.patch.object(select, "KQ_FILTER_WRITE", KQ_FILTER_WRITE, create=True),
            mock.patch.object(select, "KQ_EV_ADD", KQ_EV_ADD, create=True),
            mock.patch.object(select, "KQ_EV_ONESHOT", KQ_EV_ONESHOT, create=True),
            mock.patch.object(select, "KQ_EV_EOF", KQ_EV_EOF, create=True),
            mock.patch.object(select, "KQ_EV_ERROR", KQ_EV_ERROR, create=True),
        ):
            conn = _FakeConn(42)
            # 1. Empty event list (e.g. write buffer full) -> False, kq.close() called
            next_outcome[0] = []
            self.assertFalse(_is_control_peer_disconnected(conn))  # type: ignore[arg-type]
            self.assertTrue(created[-1].closed)
            self.assertEqual(captured_kevents[-1], (42, KQ_FILTER_WRITE, KQ_EV_ADD | KQ_EV_ONESHOT))
            self.assertEqual(created[-1].control_calls[-1][1:], (1, 0))

            # 2. Writable without KQ_EV_EOF (peer SHUT_WR on macOS/BSD: flags=0x1) -> False, kq.close() called
            next_outcome[0] = [types.SimpleNamespace(flags=KQ_EV_ADD)]
            self.assertFalse(_is_control_peer_disconnected(conn))  # type: ignore[arg-type]
            self.assertTrue(created[-1].closed)

            # 3. KQ_EV_EOF set (peer close() on macOS/BSD: flags=0x8001) -> True, kq.close() called
            next_outcome[0] = [types.SimpleNamespace(flags=KQ_EV_ADD | KQ_EV_EOF)]
            self.assertTrue(_is_control_peer_disconnected(conn))  # type: ignore[arg-type]
            self.assertTrue(created[-1].closed)

            # 4. KQ_EV_ERROR set -> True, kq.close() called
            next_outcome[0] = [types.SimpleNamespace(flags=KQ_EV_ERROR)]
            self.assertTrue(_is_control_peer_disconnected(conn))  # type: ignore[arg-type]
            self.assertTrue(created[-1].closed)

            # 5. OSError inside control() -> True, kq.close() still called via finally
            next_outcome[0] = OSError("bad fd")
            self.assertTrue(_is_control_peer_disconnected(conn))  # type: ignore[arg-type]
            self.assertTrue(created[-1].closed)

    def test_selftest_wav_invalid_or_overflow_timeout_sec_rejects_cleanly_without_locking_gate(self) -> None:
        from unittest import mock

        with tempfile.TemporaryDirectory(prefix="avi-to-", dir="/tmp") as td:
            rt = Path(td)
            os.chmod(rt, 0o700)
            wav_file = rt / "sample.wav"
            wav_file.write_bytes(_voiced_wav())
            cfg = Config(refiner=RefinerConfig(provider="off"))
            daemon = VoiceInputDaemon(
                cfg,
                env={},
                runtime_dir=rt,
                platform=_FakePlatform(),
                asr_provider=_FakeAsr("正常自检"),
                refiner=create_refiner(cfg),
                tracker=_isolated_tracker(rt),
            )
            srv_th = threading.Thread(target=daemon.serve_forever, daemon=True)
            srv_th.start()
            try:
                _wait_for_control_socket(rt)

                # Huge integer (10**400 triggers OverflowError in float()), huge finite float, negative, bool, string
                for bad_timeout in (10**400, 1e300, -1.0, True, "5"):
                    with self.subTest(bad_timeout=repr(bad_timeout)[:32]):
                        resp_bad = send_control_command(
                            rt,
                            {
                                "cmd": "selftest_wav",
                                "wav_path": str(wav_file),
                                "no_telemetry": True,
                                "timeout_sec": bad_timeout,
                            },
                            timeout_sec=2.0,
                        )
                        self.assertFalse(resp_bad.get("ok"), resp_bad)
                        self.assertIn("timeout_sec", str(resp_bad.get("error") or ""))

                # Synchronous handle_control path also rejects overflow cleanly without leaking _selftest_lock
                sync_bad = daemon.handle_control(
                    {
                        "cmd": "selftest_wav",
                        "wav_path": str(wav_file),
                        "no_telemetry": True,
                        "timeout_sec": 10**400,
                    }
                )
                self.assertFalse(sync_bad.get("ok"), sync_bad)
                self.assertIn("timeout_sec", str(sync_bad.get("error") or ""))

                # Even if Thread.start raises after acquiring gate, gate must be released cleanly
                with mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("thread_spawn_boom")):
                    resp_boom = send_control_command(
                        rt,
                        {
                            "cmd": "selftest_wav",
                            "wav_path": str(wav_file),
                            "no_telemetry": True,
                            "timeout_sec": 5.0,
                        },
                        timeout_sec=2.0,
                    )
                self.assertFalse(resp_boom.get("ok"), resp_boom)
                self.assertIn("thread_spawn_boom", str(resp_boom.get("error") or ""))

                # Subsequent valid selftest_wav MUST succeed (never stuck on "selftest_wav busy")
                resp_ok = send_control_command(
                    rt,
                    {
                        "cmd": "selftest_wav",
                        "wav_path": str(wav_file),
                        "no_telemetry": True,
                        "timeout_sec": 5.0,
                    },
                    timeout_sec=3.0,
                )
                self.assertTrue(resp_ok.get("ok"), resp_ok)
                self.assertEqual(resp_ok.get("final_text"), "正常自检")
            finally:
                daemon.close()
                srv_th.join(timeout=2.0)

    def test_selftest_wav_isolates_refiner_session_slot_while_preserving_real_session_context(self) -> None:
        from unittest import mock
        from agent_voice_input.refiner import (
            AgyRefiner,
            BaseRefiner,
            _AgyThreadState,
            build_refiner_request_blocks,
        )
        from agent_voice_input.session_ctx import SessionMatch, SessionSnapshot, SessionState

        with tempfile.TemporaryDirectory(prefix="avi-iso-ref-", dir="/tmp") as td:
            root = Path(td)
            rt = root / "rt"
            rt.mkdir(mode=0o700)
            repo = root / "demo-proj"
            repo.mkdir()
            wav_file = rt / "sample.wav"
            wav_file.write_bytes(_voiced_wav())

            real_skey = "codex:real-live-slot-8888"
            real_turns: list[tuple[str, str]] = [
                ("user", "请排查 AgyRefiner 会话槽隔离问题"),
                ("assistant", "好的，我会核对 _threads 键与上下文块构建逻辑。"),
            ]

            def _make_matched_tracker(cwd_val: str) -> SessionTracker:
                tr = _isolated_tracker(rt)
                match = SessionMatch(
                    cli="codex",
                    session_id="real-live-slot-8888",
                    jsonl_path=str(rt / "sess.jsonl"),
                    cwd=cwd_val,
                    matched_by="title_prefix",
                )
                st_obj = SessionState(
                    session_key=real_skey,
                    cli="codex",
                    session_id="real-live-slot-8888",
                    jsonl_path=match.jsonl_path,
                    cwd=cwd_val,
                    clean_turns=list(real_turns),
                )
                snap = SessionSnapshot(
                    match=match,
                    clean_turns=tuple(real_turns),
                    luna_synced_idx=0,
                    generation=1,
                    git_status="",
                    hotwords=("AgyRefiner", "SessionTracker"),
                    byte_offset=128,
                )
                tr._states[real_skey] = st_obj
                tr.sync_session = lambda **_kw: snap  # type: ignore[method-assign]
                return tr

            # 1. Spy refiner confirms session_key == "" while mode, routing, and real context blocks stay intact
            seen_calls: list[dict[str, object]] = []
            cfg_spy = Config(refiner=RefinerConfig(provider="deepseek", include_git_status=False))

            class _SpyRefiner(BaseRefiner):
                provider_name = "deepseek"

                def refine(self, asr_input: AsrResult | str, *, mode: str = "agent", **kw: object) -> RefinerResult:
                    draft = asr_input.text if isinstance(asr_input, AsrResult) else str(asr_input)
                    ctx_blocks, final_block = build_refiner_request_blocks(
                        cfg_spy,
                        draft,
                        session_key=str(kw.get("session_key") or ""),
                        tracker=kw.get("tracker"),  # type: ignore[arg-type]
                        fallback_context=str(kw.get("fallback_context") or ""),
                        mode=mode,
                        workspace_cwd=str(kw.get("workspace_cwd") or ""),
                        target_hint=str(kw.get("target_hint") or ""),
                    )
                    seen_calls.append(
                        {
                            "session_key": kw.get("session_key"),
                            "mode": mode,
                            "ctx_blocks": ctx_blocks,
                            "final_block": final_block,
                        }
                    )
                    return RefinerResult(
                        text=f"refined:{draft}",
                        provider="deepseek",
                        model="mock",
                        latency_ms=3,
                        used_fallback="",
                        context_chars=sum(len(b) for b in ctx_blocks),
                        context_chunks=len(ctx_blocks),
                    )

            for cwd_case in (str(repo), ""):
                seen_calls.clear()
                tr_case = _make_matched_tracker(cwd_case)
                daemon_spy = VoiceInputDaemon(
                    cfg_spy,
                    env={},
                    runtime_dir=rt,
                    platform=_FakePlatform(),
                    asr_provider=_FakeAsr("帮我看看会花槽隔离"),
                    refiner=_SpyRefiner(),
                    tracker=tr_case,
                )
                daemon_spy._make_probe_tracker = lambda _t=tr_case: (_t, False)  # type: ignore[method-assign]
                try:
                    resp = daemon_spy.handle_control(
                        {
                            "cmd": "selftest_wav",
                            "wav_path": str(wav_file),
                            "process": "WindowsTerminal.exe",
                            "title": "codex · demo-proj [#real-liv]",
                            "no_telemetry": True,
                        }
                    )
                    self.assertTrue(resp.get("ok"), resp)
                    self.assertEqual(len(seen_calls), 1)
                    self.assertEqual(seen_calls[0]["session_key"], "")
                    self.assertEqual(seen_calls[0]["mode"], "agent")
                    joined_ctx = "\n".join(seen_calls[0]["ctx_blocks"])  # type: ignore[arg-type]
                    self.assertIn("AgyRefiner", joined_ctx)
                    self.assertIn("_threads", joined_ctx)
                    self.assertEqual(
                        resp["routing"]["session_key_masked"],
                        "codex:real-liv",
                        resp["routing"],
                    )
                finally:
                    daemon_spy.close()

            # 2. Real AgyRefiner stateful _threads slot for the live session is neither read, mutated, nor evicted
            cfg_agy = Config(refiner=RefinerConfig(provider="agy", model="flash", include_git_status=False))
            agy_refiner = AgyRefiner(cfg_agy, env={}, runtime_dir=rt)
            live_state = _AgyThreadState(
                conversation_id="live-acp-conv-001",
                turn_count=3,
                sent_blocks=("<session_reference>old</session_reference>",),
                model="flash",
                effort="medium:0",
                prompt_hash="deadbeef",
            )
            agy_refiner._threads[(real_skey, "agent")] = live_state
            acp_calls: list[dict[str, object]] = []

            def _fake_acp_session(**kw: object) -> tuple[str, str, str, tuple[int | None, ...], int]:
                acp_calls.append(dict(kw))
                return "帮我看看会话槽隔离", "ephemeral-selftest-sid", "flash", (100, 0, 10, None), 5

            agy_refiner._run_acp_session = _fake_acp_session  # type: ignore[method-assign]
            tr_agy = _make_matched_tracker(str(repo))
            daemon_agy = VoiceInputDaemon(
                cfg_agy,
                env={},
                runtime_dir=rt,
                platform=_FakePlatform(),
                asr_provider=_FakeAsr("帮我看看会花槽隔离"),
                refiner=agy_refiner,
                tracker=tr_agy,
            )
            daemon_agy._make_probe_tracker = lambda _t=tr_agy: (_t, False)  # type: ignore[method-assign]
            try:
                with mock.patch("agent_voice_input.refiner.find_agy_bin", return_value="/usr/bin/agy"):
                    resp_agy = daemon_agy.handle_control(
                        {
                            "cmd": "selftest_wav",
                            "wav_path": str(wav_file),
                            "process": "WindowsTerminal.exe",
                            "title": "codex · demo-proj [#real-liv]",
                            "no_telemetry": True,
                        }
                    )
                self.assertTrue(resp_agy.get("ok"), resp_agy)
                self.assertEqual(resp_agy.get("final_text"), "帮我看看会话槽隔离")
                self.assertEqual(len(acp_calls), 1)
                self.assertEqual(acp_calls[0]["conversation_id"], "")
                self.assertFalse(acp_calls[0]["track_loaded_session"])
                self.assertIn("AgyRefiner", str(acp_calls[0]["prompt_text"]))
                # Live session slot in AgyRefiner._threads is 100% untouched
                self.assertEqual(list(agy_refiner._threads.keys()), [(real_skey, "agent")])
                self.assertEqual(live_state.conversation_id, "live-acp-conv-001")
                self.assertEqual(live_state.turn_count, 3)
                self.assertEqual(live_state.sent_blocks, ("<session_reference>old</session_reference>",))
            finally:
                daemon_agy.close()

    def test_r3_agy_db_veto_blocks_legacy_brain_fallback_and_isolated_no_cwd_preserves_multichunk_prior(self) -> None:
        """R3 polish items 1 & 6: AGY DB veto blocks legacy brain/ fallback; no-cwd isolated selftest keeps multi-chunk + prior."""
        import contextlib
        import sqlite3
        from agent_voice_input.refiner import BaseRefiner, build_refiner_request_blocks
        from agent_voice_input.session_ctx import SessionMatch, SessionSnapshot, SessionState

        with tempfile.TemporaryDirectory(prefix="avi-r3-d-", dir="/tmp") as td:
            root = Path(td)
            home = root / "home"
            rt = root / "rt"
            rt.mkdir(mode=0o700)
            repo_a = home / "repos" / "repo-a"
            repo_b = home / "repos" / "repo-b"
            repo_a.mkdir(parents=True)
            repo_b.mkdir(parents=True)
            doc_a = repo_a / "app.py"
            doc_a.write_text("print('a')\n", encoding="utf-8")

            ide_root = home / ".gemini" / "antigravity"
            db_path = ide_root / "conversation_summaries.db"
            # Seed a rich legacy brain/ session in repo_a that legacy fallback would match if NOT vetoed
            legacy_tp = ide_root / "brain" / "cid-legacy-a" / ".system_generated" / "logs" / "transcript_full.jsonl"
            legacy_tp.parent.mkdir(parents=True)
            legacy_tp.write_text(
                json.dumps({"source": "SYSTEM", "type": "SYSTEM_INFO", "cwd": str(repo_a), "content": f"Active Workspaces:\n- {repo_a}\n"})
                + "\n"
                + json.dumps({"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>Legacy repo A session</USER_REQUEST>"})
                + "\n"
                + json.dumps({"source": "MODEL", "type": "PLANNER_RESPONSE", "content": "Legacy reply for repo A."})
                + "\n",
                encoding="utf-8",
            )

            # Seed DB with: (1) cross-repo duplicate title ("Dup Title") and (2) matched title without transcript ("Fresh Title")
            db_path.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
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
                    [
                        ("cid-dup-a", "Dup Title", json.dumps([repo_a.as_uri()]), "2026-11-23T09:00:10Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                        ("cid-dup-b", "Dup Title", json.dumps([repo_b.as_uri()]), "2026-11-23T09:00:20Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                        ("cid-fresh-no-tp", "Fresh Title", json.dumps([repo_a.as_uri()]), "2026-11-23T09:01:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                    ],
                )
                conn.commit()

            cfg_off = Config(refiner=RefinerConfig(provider="off", include_git_status=False))
            env_iso = {"HOME": str(home), "WSL_DISTRO_NAME": ""}
            daemon_v = VoiceInputDaemon(
                cfg_off,
                env=env_iso,
                runtime_dir=rt,
                platform=_FakePlatform(),
                asr_provider=_FakeAsr("ok"),
                refiner=create_refiner(cfg_off),
                tracker=SessionTracker(home_dir=home, runtime_dir=rt, env=env_iso),
            )
            try:
                # 1a. Ambiguous DB title MUST veto legacy brain/ fallback even with document pointing inside repo_a!
                r_dup = daemon_v.handle_control(
                    {
                        "cmd": "probe_route",
                        "process": "Antigravity",
                        "bundle_id": "com.google.antigravity",
                        "title": "Dup Title",
                        "document": doc_a.as_uri(),
                    }
                )
                self.assertTrue(r_dup.get("ok"), r_dup)
                self.assertEqual(r_dup["session_key_masked"], "")
                self.assertEqual(r_dup["workspace_name"], "")

                # 1b. Matched DB title with no transcript yet MUST veto legacy brain/ fallback!
                r_nt = daemon_v.handle_control(
                    {
                        "cmd": "probe_route",
                        "process": "Antigravity",
                        "bundle_id": "com.google.antigravity",
                        "title": "Fresh Title",
                        "document": doc_a.as_uri(),
                    }
                )
                self.assertTrue(r_nt.get("ok"), r_nt)
                self.assertEqual(r_nt["session_key_masked"], "")
                self.assertEqual(r_nt["workspace_name"], "")

                # 1c. Unmatched title (clean DB miss) STILL allows legacy document -> brain/ fallback
                r_leg = daemon_v.handle_control(
                    {
                        "cmd": "probe_route",
                        "process": "Antigravity",
                        "bundle_id": "com.google.antigravity",
                        "title": "app.py — repo-a — Antigravity",
                        "document": doc_a.as_uri(),
                    }
                )
                self.assertTrue(r_leg.get("ok"), r_leg)
                self.assertEqual(r_leg["session_key_masked"], "agy:cid-lega")

                # 1d. Corrupt DB MUST veto legacy brain/ fallback!
                db_path.write_bytes(b"corrupt agy db bytes")
                r_corrupt = daemon_v.handle_control(
                    {
                        "cmd": "probe_route",
                        "process": "Antigravity",
                        "bundle_id": "com.google.antigravity",
                        "title": "app.py — repo-a — Antigravity",
                        "document": doc_a.as_uri(),
                    }
                )
                self.assertTrue(r_corrupt.get("ok"), r_corrupt)
                self.assertEqual(r_corrupt["session_key_masked"], "")
                self.assertEqual(r_corrupt["workspace_name"], "")
            finally:
                daemon_v.close()

            # 2. Item 6: No-cwd isolated selftest preserves multi-chunk blocks and prior_chunk (never flattened)
            wav_file = rt / "multi.wav"
            wav_file.write_bytes(_voiced_wav())
            skey_nocwd = "agy:nocwd-multi-9999"
            tr_multi = _isolated_tracker(rt)
            match_nocwd = SessionMatch(
                cli="agy",
                session_id="nocwd-multi-9999",
                jsonl_path=str(rt / "nocwd.jsonl"),
                cwd="",
                matched_by="ide_db_title",
            )
            st_nocwd = SessionState(
                session_key=skey_nocwd,
                cli="agy",
                session_id="nocwd-multi-9999",
                jsonl_path=match_nocwd.jsonl_path,
                cwd="",
                clean_turns=[("user", "chunk one"), ("assistant", "chunk two")],
            )
            snap_nocwd = SessionSnapshot(
                match=match_nocwd,
                clean_turns=tuple(st_nocwd.clean_turns),
                luna_synced_idx=0,
                generation=1,
                git_status="",
                hotwords=("ChunkAlpha",),
                byte_offset=64,
            )
            tr_multi._states[skey_nocwd] = st_nocwd
            tr_multi.sync_session = lambda **_kw: snap_nocwd  # type: ignore[method-assign]
            tr_multi.get_refiner_context_with_prior = (  # type: ignore[method-assign]
                lambda sk, **_kw: ("PRIOR_REF_BODY", ["CHUNK_ONE_BODY", "CHUNK_TWO_BODY"], "")
                if sk == skey_nocwd
                else ("", [], "")
            )

            seen_blocks: list[list[str]] = []
            cfg_spy = Config(refiner=RefinerConfig(provider="deepseek", include_git_status=False))

            class _MultiChunkSpyRefiner(BaseRefiner):
                provider_name = "deepseek"

                def refine(self, asr_input: AsrResult | str, *, mode: str = "agent", **kw: object) -> RefinerResult:
                    draft = asr_input.text if isinstance(asr_input, AsrResult) else str(asr_input)
                    self.assert_tracker = kw.get("tracker")
                    blocks, _final = build_refiner_request_blocks(
                        cfg_spy,
                        draft,
                        session_key=str(kw.get("session_key") or ""),
                        tracker=kw.get("tracker"),  # type: ignore[arg-type]
                        fallback_context=str(kw.get("fallback_context") or ""),
                        mode=mode,
                        workspace_cwd=str(kw.get("workspace_cwd") or ""),
                        target_hint=str(kw.get("target_hint") or ""),
                    )
                    seen_blocks.append(blocks)
                    return RefinerResult(
                        text=f"ok:{draft}",
                        provider="deepseek",
                        model="mock",
                        latency_ms=1,
                        context_chars=sum(len(b) for b in blocks),
                        context_chunks=len(blocks),
                    )

            spy_ref = _MultiChunkSpyRefiner()
            daemon_mc = VoiceInputDaemon(
                cfg_spy,
                env={},
                runtime_dir=rt,
                platform=_FakePlatform(),
                asr_provider=_FakeAsr("测试多块保留"),
                refiner=spy_ref,
                tracker=tr_multi,
            )
            daemon_mc._make_probe_tracker = lambda _t=tr_multi: (_t, False)  # type: ignore[method-assign]
            try:
                resp_mc = daemon_mc.handle_control(
                    {
                        "cmd": "selftest_wav",
                        "wav_path": str(wav_file),
                        "process": "WindowsTerminal.exe",
                        "title": "agy [#nocwd-mu]",
                        "no_telemetry": True,
                    }
                )
                self.assertTrue(resp_mc.get("ok"), resp_mc)
                self.assertTrue(getattr(spy_ref.assert_tracker, "has_bound_session", False))
                self.assertEqual(len(seen_blocks), 1)
                self.assertEqual(len(seen_blocks[0]), 3, seen_blocks[0])
                self.assertTrue(seen_blocks[0][0].startswith("<prior_session_reference"))
                self.assertIn("PRIOR_REF_BODY", seen_blocks[0][0])
                self.assertTrue(seen_blocks[0][1].startswith("<session_reference_log"))
                self.assertIn("CHUNK_ONE_BODY", seen_blocks[0][1])
                self.assertTrue(seen_blocks[0][2].startswith("<session_reference_log"))
                self.assertIn("CHUNK_TWO_BODY", seen_blocks[0][2])
            finally:
                daemon_mc.close()

    def test_selftest_wav_asr_exception_releases_shared_refiner_and_allows_close(self) -> None:
        from agent_voice_input.refiner import BaseRefiner

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            wav_file = rt / "boom.wav"
            wav_file.write_bytes(_voiced_wav())

            class _BoomAsr(BaseAsrProvider):
                provider_name = "boom_asr"

                def __init__(self) -> None:
                    pass

                def transcribe(
                    self,
                    wav_bytes: bytes,
                    context: object = None,
                    *,
                    is_cancelled: object = None,
                    cancel_token: object = None,
                ) -> AsrResult:
                    del wav_bytes, context, is_cancelled, cancel_token
                    raise RuntimeError("injected ASR boom")

            close_calls: list[str] = []

            class _TrackedRefiner(BaseRefiner):
                provider_name = "tracked"

                def refine(self, asr_input: AsrResult | str, **kwargs: object) -> RefinerResult:
                    del kwargs
                    txt = asr_input.text if isinstance(asr_input, AsrResult) else str(asr_input)
                    return RefinerResult(text=txt, provider="tracked", model="m", latency_ms=1)

                def close(self) -> None:
                    close_calls.append("refiner")

            daemon = VoiceInputDaemon(
                Config(refiner=RefinerConfig(provider="deepseek")),
                env={},
                runtime_dir=rt,
                platform=_FakePlatform(),
                asr_provider=_BoomAsr(),
                refiner=_TrackedRefiner(),
                tracker=_isolated_tracker(rt),
            )
            try:
                with self.assertRaisesRegex(RuntimeError, "injected ASR boom"):
                    daemon.handle_control(
                        {
                            "cmd": "selftest_wav",
                            "wav_path": str(wav_file),
                            "no_telemetry": True,
                        }
                    )
                with daemon._lock:
                    self.assertEqual(daemon._refiner_users, 0)
            finally:
                daemon.close()
            self.assertEqual(close_calls, ["refiner"])

    def test_stage2_vocabulary_hints_all_channels_gemini_3turn_telemetry_echo_and_hot_reload(self) -> None:
        from agent_voice_input.hotwords import (
            HotwordEntry,
            clear_hotwords_cache,
            format_stage2_vocabulary_hints,
        )
        from agent_voice_input.refiner import (
            BaseRefiner,
            GeminiRefiner,
            OpenAICompatibleRefiner,
            OpenAIResponsesRefiner,
            _clean_out,
        )

        vocab_blk = format_stage2_vocabulary_hints(
            [HotwordEntry("KestrelDB", ("凯斯特罗",)), HotwordEntry("Ghostty")]
        )
        self.assertTrue(vocab_blk.startswith('<vocabulary_hints read_only="true">'))
        self.assertIn("KestrelDB (ASR has misheard as: 凯斯特罗)", vocab_blk)

        class _MultiChunkTracker:
            has_bound_session = True

            def __init__(self, chunks: list[str], git_ctx: str = "cwd: /repo\nchanged_files: refiner.py") -> None:
                self._chunks = chunks
                self._git_ctx = git_ctx

            def get_refiner_context_with_prior(
                self, session_key: str, *, max_chars: int = 200000, include_git_status: bool = True, allow_prior: bool = True, **_kw: object
            ) -> tuple[str, list[str], str]:
                del session_key, max_chars, allow_prior
                return "", list(self._chunks), (self._git_ctx if include_git_status else "")

            def get_state(self, session_key: str) -> object:
                del session_key
                return object()

        # 1. _clean_out strips echoed <vocabulary_hints> blocks/tags while keeping literal user mentions
        self.assertEqual(_clean_out(f"{vocab_blk}\n检查 KestrelDB 配置", "检查 凯斯特罗 配置"), "检查 KestrelDB 配置")
        self.assertEqual(
            _clean_out("检查 <vocabulary_hints> 标签格式", "检查 <vocabulary_hints> 标签格式"),
            "检查 <vocabulary_hints> 标签格式",
        )

        # 2. OpenAIResponsesRefiner: leading developer[0] = <vocabulary_hints> in both agent & general;
        #    telemetry context_chunks/context_chars exclude static <vocabulary_hints>
        resp_reqs: list[HttpRequest] = []
        oai_env = {"OPENAI_API_KEY": "sk-oai-vocab-" + ("a" * 16)}

        def _resp_sse_tx(req: HttpRequest) -> HttpResponse:
            resp_reqs.append(req)
            ev = {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": f"{vocab_blk}\n已修正 KestrelDB"}],
                        }
                    ],
                },
            }
            return HttpResponse(200, {"content-type": "text/event-stream"}, f"data: {json.dumps(ev)}\n\n".encode("utf-8"))

        cfg_resp = Config(refiner=RefinerConfig(provider="openai", api="responses"))
        ref_resp = OpenAIResponsesRefiner(
            cfg_resp, resolve_credentials(cfg_resp, oai_env), provider_name="openai", env=oai_env, transport=_resp_sse_tx
        )
        try:
            r_agent = ref_resp.refine(
                "检查凯斯特罗",
                session_key="s-1",
                tracker=_MultiChunkTracker(["[User]\n查看代码"]),  # type: ignore[arg-type]
                mode="agent",
                vocabulary_hints=vocab_blk,
            )
            self.assertEqual(r_agent.text, "已修正 KestrelDB")
            self.assertEqual(r_agent.context_chunks, 1)
            self.assertGreater(r_agent.context_chars, 0)
            p_agent = json.loads(resp_reqs[-1].body.decode("utf-8"))
            self.assertEqual([it["role"] for it in p_agent["input"]], ["developer", "developer", "user"])
            self.assertEqual(p_agent["input"][0]["content"][0]["text"], vocab_blk)
            self.assertTrue(p_agent["input"][1]["content"][0]["text"].startswith("<session_reference_log"))

            r_gen = ref_resp.refine("检查凯斯特罗", mode="general", vocabulary_hints=vocab_blk)
            self.assertEqual(r_gen.text, "已修正 KestrelDB")
            self.assertEqual((r_gen.context_chunks, r_gen.context_chars), (0, 0))
            p_gen = json.loads(resp_reqs[-1].body.decode("utf-8"))
            self.assertEqual([it["role"] for it in p_gen["input"]], ["developer", "user"])
            self.assertEqual(p_gen["input"][0]["content"][0]["text"], vocab_blk)
        finally:
            ref_resp.close()

        # 3. OpenAICompatibleRefiner (OpenRouter google/*): skips explicit cache_control for single growing
        #    history block [vocab, log0] and general [vocab], and places cache_control on sealed_0 (idx 1)
        #    once a sealed block exists [vocab, sealed_0, tail_1], with accurate session-only telemetry counts
        or_reqs: list[HttpRequest] = []
        or_env = {"OPENROUTER_API_KEY": "sk-or-vocab-" + ("b" * 16)}

        def _or_tx(req: HttpRequest) -> HttpResponse:
            or_reqs.append(req)
            return HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps({"choices": [{"message": {"content": "OpenRouter OK"}}]}).encode("utf-8"),
            )

        cfg_or = Config(refiner=RefinerConfig(provider="openrouter", model="google/gemini-3.8-flash"))
        ref_or = OpenAICompatibleRefiner(
            cfg_or, resolve_credentials(cfg_or, or_env), provider_name="openrouter", env=or_env, transport=_or_tx
        )
        res_or_agent = ref_or.refine(
            "看日志",
            session_key="s-2",
            tracker=_MultiChunkTracker(["[User]\n单块日志"]),  # type: ignore[arg-type]
            mode="agent",
            vocabulary_hints=vocab_blk,
        )
        self.assertEqual(res_or_agent.context_chunks, 1)
        or_content_single = json.loads(or_reqs[-1].body.decode("utf-8"))["messages"][1]["content"]
        self.assertIsInstance(or_content_single, str)
        self.assertIn(vocab_blk, or_content_single)
        self.assertNotIn("cache_control", or_reqs[-1].body.decode("utf-8"))

        res_or_multi = ref_or.refine(
            "看日志",
            session_key="s-2-multi",
            tracker=_MultiChunkTracker(["[User]\n封口块日志", "[User]\n动态尾块日志"]),  # type: ignore[arg-type]
            mode="agent",
            vocabulary_hints=vocab_blk,
        )
        self.assertEqual(res_or_multi.context_chunks, 2)
        or_parts_multi = json.loads(or_reqs[-1].body.decode("utf-8"))["messages"][1]["content"]
        self.assertIsInstance(or_parts_multi, list)
        self.assertEqual(or_parts_multi[0]["text"], vocab_blk)
        self.assertNotIn("cache_control", or_parts_multi[0])
        self.assertEqual(or_parts_multi[1].get("cache_control"), {"type": "ephemeral"})
        self.assertNotIn("cache_control", or_parts_multi[2])

        res_or_gen = ref_or.refine("普通聊天", mode="general", vocabulary_hints=vocab_blk)
        self.assertEqual((res_or_gen.context_chunks, res_or_gen.context_chars), (0, 0))
        or_content_gen = json.loads(or_reqs[-1].body.decode("utf-8"))["messages"][1]["content"]
        self.assertIsInstance(or_content_gen, str)
        self.assertIn(vocab_blk, or_content_gen)
        self.assertNotIn("cache_control", or_reqs[-1].body.decode("utf-8"))

        # 4. Native GeminiRefiner 3-turn stateless implicit caching matrix
        gem_reqs: list[HttpRequest] = []
        gem_env = {"GEMINI_API_KEY": "AIza-gem-3turn-" + ("c" * 16)}

        def _gem_tx(req: HttpRequest) -> HttpResponse:
            gem_reqs.append(req)
            return HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps({"candidates": [{"content": {"parts": [{"text": "Gemini OK"}]}}]}).encode("utf-8"),
            )

        cfg_gem = Config(refiner=RefinerConfig(provider="gemini"))
        ref_gem = GeminiRefiner(cfg_gem, resolve_credentials(cfg_gem, gem_env), env=gem_env, transport=_gem_tx)

        # 4a. Empty context_blocks -> single user turn (no unnecessary placeholder)
        ref_gem.refine("纯草稿", mode="general")
        c_empty = json.loads(gem_reqs[-1].body.decode("utf-8"))["contents"]
        self.assertEqual([c["role"] for c in c_empty], ["user"])

        # 4b. General with <vocabulary_hints> -> 3-turn [user(vocab), model("Understood."), user(draft)], ctx_chunks=0
        rg_gen = ref_gem.refine("聊天词表", mode="general", vocabulary_hints=vocab_blk)
        self.assertEqual((rg_gen.context_chunks, rg_gen.context_chars), (0, 0))
        c_gen = json.loads(gem_reqs[-1].body.decode("utf-8"))["contents"]
        self.assertEqual([c["role"] for c in c_gen], ["user", "model", "user"])
        self.assertEqual(c_gen[0]["parts"], [{"text": vocab_blk}])
        self.assertEqual(c_gen[1]["parts"], [{"text": "Understood."}])
        self.assertEqual(len(c_gen[2]["parts"]), 1)
        self.assertIn("<asr_draft_to_clean>", c_gen[2]["parts"][0]["text"])

        # 4c. Vocab + 1 log block -> both stable blocks in turn 1, draft in turn 3
        rg_one = ref_gem.refine(
            "单块日志",
            session_key="s-g1",
            tracker=_MultiChunkTracker(["[User]\n单块对话"]),  # type: ignore[arg-type]
            mode="agent",
            vocabulary_hints=vocab_blk,
        )
        self.assertEqual(rg_one.context_chunks, 1)
        c_one = json.loads(gem_reqs[-1].body.decode("utf-8"))["contents"]
        self.assertEqual([c["role"] for c in c_one], ["user", "model", "user"])
        self.assertEqual(len(c_one[0]["parts"]), 2)
        self.assertEqual(c_one[0]["parts"][0]["text"], vocab_blk)
        self.assertTrue(c_one[0]["parts"][1]["text"].startswith("<session_reference_log"))
        self.assertEqual(len(c_one[2]["parts"]), 1)

        # 4d. Vocab + 2 log blocks -> [vocab, log0] in turn 1, [log1, draft] in turn 3
        rg_two = ref_gem.refine(
            "双块日志",
            session_key="s-g2",
            tracker=_MultiChunkTracker(["[User]\n封口块一", "[User]\n活跃尾块二"]),  # type: ignore[arg-type]
            mode="agent",
            vocabulary_hints=vocab_blk,
        )
        self.assertEqual(rg_two.context_chunks, 2)
        c_two = json.loads(gem_reqs[-1].body.decode("utf-8"))["contents"]
        self.assertEqual([c["role"] for c in c_two], ["user", "model", "user"])
        self.assertEqual(len(c_two[0]["parts"]), 2)
        self.assertEqual(c_two[0]["parts"][0]["text"], vocab_blk)
        self.assertIn("封口块一", c_two[0]["parts"][1]["text"])
        self.assertEqual(len(c_two[2]["parts"]), 2)
        self.assertIn("活跃尾块二", c_two[2]["parts"][0]["text"])
        self.assertIn("<asr_draft_to_clean>", c_two[2]["parts"][1]["text"])

        # 5. End-to-end daemon_pipeline hotwords.txt -> Stage 2 vocabulary_hints mode split + live hot reload
        with tempfile.TemporaryDirectory() as td:
            clear_hotwords_cache()
            try:
                hw_file = Path(td) / "hotwords.txt"
                hw_file.write_text(
                    "[global]\nGlobalName <- 格洛博\n[chat]\nChatFriend <- 查特\n[coding]\nCodingCrate <- 科丁\n",
                    encoding="utf-8",
                )
                seen_vocab: list[tuple[str, str]] = []

                class _CaptureVocabRefiner(BaseRefiner):
                    provider_name = "capture"

                    def refine(self, asr_input: AsrResult | str, *, mode: str = "agent", vocabulary_hints: str = "", **_kw: object) -> RefinerResult:
                        seen_vocab.append((mode, vocabulary_hints))
                        txt = asr_input.text if isinstance(asr_input, AsrResult) else str(asr_input)
                        return RefinerResult(text=txt, provider="capture", model="m", latency_ms=1)

                class _StaticAsr(BaseAsrProvider):
                    provider_name = "static_asr"

                    def __init__(self, text: str) -> None:
                        self._text = text

                    def transcribe(
                        self,
                        wav_bytes: bytes,
                        context: object = None,
                        *,
                        is_cancelled: object = None,
                        cancel_token: object = None,
                    ) -> AsrResult:
                        del wav_bytes, context, is_cancelled, cancel_token
                        return AsrResult(text=self._text, provider="static_asr", model="m", latency_ms=1)

                cfg_p = Config()
                cfg_p.asr.hotwords_file = str(hw_file)
                tracker_p = _isolated_tracker(Path(td))
                fake_asr = _StaticAsr("测试热加载")
                cap_ref = _CaptureVocabRefiner()

                run_pipeline_once(
                    _voiced_wav(),
                    cfg=cfg_p,
                    tracker=tracker_p,
                    asr_provider=fake_asr,
                    refiner=cap_ref,
                    mode="agent",
                    dry_run=True,
                    env={},
                )
                self.assertEqual(seen_vocab[-1][0], "agent")
                self.assertIn("CodingCrate (ASR has misheard as: 科丁)", seen_vocab[-1][1])
                self.assertIn("GlobalName (ASR has misheard as: 格洛博)", seen_vocab[-1][1])
                self.assertNotIn("ChatFriend", seen_vocab[-1][1])

                run_pipeline_once(
                    _voiced_wav(),
                    cfg=cfg_p,
                    tracker=tracker_p,
                    asr_provider=fake_asr,
                    refiner=cap_ref,
                    mode="general",
                    dry_run=True,
                    env={},
                )
                self.assertEqual(seen_vocab[-1][0], "general")
                self.assertIn("GlobalName (ASR has misheard as: 格洛博)", seen_vocab[-1][1])
                self.assertIn("ChatFriend (ASR has misheard as: 查特)", seen_vocab[-1][1])
                self.assertNotIn("CodingCrate", seen_vocab[-1][1])

                # Edit hotwords.txt on disk without restarting -> reflected on next utterance immediately
                hw_file.write_text(
                    "[global]\nHotReloadedTerm <- 热点词\n",
                    encoding="utf-8",
                )
                run_pipeline_once(
                    _voiced_wav(),
                    cfg=cfg_p,
                    tracker=tracker_p,
                    asr_provider=fake_asr,
                    refiner=cap_ref,
                    mode="general",
                    dry_run=True,
                    env={},
                )
                self.assertIn("HotReloadedTerm (ASR has misheard as: 热点词)", seen_vocab[-1][1])
                self.assertNotIn("GlobalName", seen_vocab[-1][1])
            finally:
                clear_hotwords_cache()

    def test_daemon_stage2_slot_forwards_vocabulary_hints_for_agent_and_general(self) -> None:
        from agent_voice_input.hotwords import clear_hotwords_cache
        from agent_voice_input.refiner import BaseRefiner

        with tempfile.TemporaryDirectory() as td:
            clear_hotwords_cache()
            try:
                hw_file = Path(td) / "hotwords.txt"
                hw_file.write_text(
                    "[global]\nGlobalName <- 格洛博\n"
                    "[coding]\nCodingCrate <- 科丁\n"
                    "[chat]\nChatFriend <- 查特\n",
                    encoding="utf-8",
                )
                seen_calls: list[tuple[str, str, str]] = []

                class _CaptureStage2Refiner(BaseRefiner):
                    provider_name = "capture_stage2"

                    def refine(
                        self,
                        asr_input: AsrResult | str,
                        *,
                        mode: str = "agent",
                        vocabulary_hints: str = "",
                        **_kw: object,
                    ) -> RefinerResult:
                        seen_calls.append((threading.current_thread().name, mode, vocabulary_hints))
                        txt = asr_input.text if isinstance(asr_input, AsrResult) else str(asr_input)
                        return RefinerResult(text=f"refined:{txt}", provider="capture_stage2", model="m", latency_ms=1)

                plat = _FakePlatform()
                cfg = Config(refiner=RefinerConfig(provider="deepseek"))
                cfg.asr.hotwords_file = str(hw_file)
                daemon = VoiceInputDaemon(
                    cfg,
                    env={},
                    runtime_dir=Path(td),
                    platform=plat,
                    asr_provider=_FakeAsr("热键队列测试"),
                    refiner=_CaptureStage2Refiner(),
                    tracker=_isolated_tracker(Path(td)),
                    paste_settle_sec=0.0,
                )
                try:
                    # 1. Agent mode (terminal foreground) via real hotkey queue -> _run_stage2_slot()
                    plat.fg = ForegroundWindow(hwnd=101, process="WindowsTerminal", title="codex")
                    daemon._last_trigger_ts = -1e9
                    self.assertTrue(daemon.handle_trigger("ptt_start")["ok"])
                    daemon._last_trigger_ts = -1e9
                    s1 = daemon.handle_trigger("ptt_stop")
                    deadline = time.monotonic() + 3.0
                    while time.monotonic() < deadline and daemon.last_result.get("seq_id") != s1["seq_id"]:
                        time.sleep(0.02)
                    self.assertEqual(daemon.last_result.get("seq_id"), s1["seq_id"])
                    self.assertEqual(len(seen_calls), 1)
                    self.assertTrue(seen_calls[0][0].startswith("avi-stage2-"), seen_calls[0][0])
                    self.assertEqual(seen_calls[0][1], "agent")
                    self.assertIn("CodingCrate (ASR has misheard as: 科丁)", seen_calls[0][2])
                    self.assertIn("GlobalName (ASR has misheard as: 格洛博)", seen_calls[0][2])
                    self.assertNotIn("ChatFriend", seen_calls[0][2])

                    # 2. General mode (general GUI foreground) via real hotkey queue -> _run_stage2_slot()
                    plat.fg = ForegroundWindow(hwnd=202, process="WeChat", title="微信")
                    daemon._last_trigger_ts = -1e9
                    self.assertTrue(daemon.handle_trigger("ptt_start")["ok"])
                    daemon._last_trigger_ts = -1e9
                    s2 = daemon.handle_trigger("ptt_stop")
                    deadline = time.monotonic() + 3.0
                    while time.monotonic() < deadline and daemon.last_result.get("seq_id") != s2["seq_id"]:
                        time.sleep(0.02)
                    self.assertEqual(daemon.last_result.get("seq_id"), s2["seq_id"])
                    self.assertEqual(len(seen_calls), 2)
                    self.assertTrue(seen_calls[1][0].startswith("avi-stage2-"), seen_calls[1][0])
                    self.assertEqual(seen_calls[1][1], "general")
                    self.assertIn("GlobalName (ASR has misheard as: 格洛博)", seen_calls[1][2])
                    self.assertIn("ChatFriend (ASR has misheard as: 查特)", seen_calls[1][2])
                    self.assertNotIn("CodingCrate", seen_calls[1][2])
                finally:
                    daemon.close()
            finally:
                clear_hotwords_cache()

    def test_vocabulary_hints_echo_strip_guard_noop_novocab_literal_draft_and_normal_echo(self) -> None:
        from agent_voice_input.hotwords import format_stage2_vocabulary_hints
        from agent_voice_input.refiner import NoopRefiner, OpenAICompatibleRefiner

        inline_example = "请保留 <vocabulary_hints>Alpha</vocabulary_hints> 作为示例"
        head_echo = '<vocabulary_hints read_only="true">\nGhostty\n</vocabulary_hints>\n打开 Ghostty'
        vocab_blk = format_stage2_vocabulary_hints(["Ghostty"])

        # 1. NoopRefiner (refiner off) MUST NEVER run strip_vocabulary_hints_echo
        noop = NoopRefiner()
        self.assertEqual(noop.refine(inline_example, vocabulary_hints=vocab_blk).text, inline_example)
        self.assertEqual(noop.refine(head_echo, vocabulary_hints=vocab_blk).text, head_echo)

        # 2. Refiner with NO vocabulary_hints sent -> MUST NOT strip echoed tags
        cfg = Config(refiner=RefinerConfig(provider="deepseek", model="deepseek-chat"))
        env = {"DEEPSEEK_API_KEY": "sk-ds-echo-" + ("f" * 16)}
        canned_reply = [head_echo]

        def _tx(req: HttpRequest) -> HttpResponse:
            del req
            return HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps({"choices": [{"message": {"content": canned_reply[0]}}]}).encode("utf-8"),
            )

        ref = OpenAICompatibleRefiner(
            cfg, resolve_credentials(cfg, env), env=env, transport=_tx, provider_name="deepseek"
        )
        res_no_vocab = ref.refine("打开 Ghostty", mode="general", vocabulary_hints="")
        self.assertEqual(res_no_vocab.text, head_echo)

        # 3. Refiner WITH vocabulary_hints AND original draft literally mentions <vocabulary_hints -> preserve!
        canned_reply[0] = head_echo
        res_lit_draft = ref.refine(
            "保留 <vocabulary_hints>Alpha</vocabulary_hints> 示例",
            mode="general",
            vocabulary_hints=vocab_blk,
        )
        self.assertEqual(res_lit_draft.text, head_echo)

        # 4. Refiner WITH vocabulary_hints AND clean draft:
        #    - leading/trailing echo IS stripped
        #    - inline mid-sentence example IS preserved
        canned_reply[0] = head_echo
        res_stripped = ref.refine("打开 高斯提", mode="general", vocabulary_hints=vocab_blk)
        self.assertEqual(res_stripped.text, "打开 Ghostty")

        canned_reply[0] = inline_example
        res_inline = ref.refine("请保留示例", mode="general", vocabulary_hints=vocab_blk)
        self.assertEqual(res_inline.text, inline_example)


if __name__ == "__main__":
    unittest.main()
