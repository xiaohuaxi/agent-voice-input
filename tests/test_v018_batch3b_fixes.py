"""Unit tests for v0.1.8 Batch 3B engineering refinements (E1, E2, E3, E5, E6, E7)."""
from __future__ import annotations

import http.server
import io
import os
from pathlib import Path
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_DIR = _REPO_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from agent_voice_input import audio_proc as audio_mod
from agent_voice_input import cli as cli_mod
from agent_voice_input import config as cfg_mod
from agent_voice_input import daemon as daemon_mod
from agent_voice_input import http_transport as http_mod
from agent_voice_input import platform_wsl as wsl_mod
from agent_voice_input.audio_proc import archive_recording_audio, pack_pcm16_mono_wav
from agent_voice_input.config import AsrConfig, Config, ConfigError, RefinerConfig, load_config, resolve_credentials
from agent_voice_input.http_transport import (
    DEFAULT_MAX_ERROR_RESPONSE_BYTES,
    DEFAULT_MAX_RESPONSE_BYTES,
    HttpRequest,
    HttpResponse,
    HttpTransportError,
    evict_all_keepalive_connections,
    inspect_proxy_config_warning,
    urllib_http_transport,
)
from agent_voice_input.platform_wsl import to_windows_path


def _sample_wav_bytes() -> bytes:
    return pack_pcm16_mono_wav([1000, -1000] * 800, sample_rate=16000)


class _KeepAliveTestServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: object, client_address: object) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class TestBatch3BE1ArchiveFallback(unittest.TestCase):
    def test_timeout_expired_breaks_immediately_to_wav_0600_and_cleans_temps(self) -> None:
        wav = _sample_wav_bytes()
        with tempfile.TemporaryDirectory() as td:
            rec_dir = Path(td) / "home" / "audio"
            calls: list[list[str]] = []

            def _fake_run(cmd: list[str], **kwargs: object) -> object:
                calls.append(list(cmd))
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 15.0))

            with (
                mock.patch.object(audio_mod, "resolve_ffmpeg_bin", return_value="/fake/ffmpeg"),
                mock.patch.object(audio_mod.subprocess, "run", side_effect=_fake_run),
            ):
                out = archive_recording_audio(
                    wav,
                    rec_dir,
                    timeout_sec=1.0,
                    utterance_id="20261127-120000-abcd",
                )

            self.assertIsNotNone(out)
            assert out is not None
            self.assertEqual(len(calls), 1, "must break after first timeout without trying AAC")
            self.assertEqual(out.name, "20261127-120000-abcd.wav")
            self.assertEqual(out.read_bytes(), wav)
            self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
            self.assertEqual([p.name for p in rec_dir.iterdir()], ["20261127-120000-abcd.wav"])

    def test_oserror_breaks_immediately_to_wav_0600_and_cleans_temps(self) -> None:
        wav = _sample_wav_bytes()
        with tempfile.TemporaryDirectory() as td:
            rec_dir = Path(td) / "home" / "audio"
            calls: list[list[str]] = []

            def _fake_run(cmd: list[str], **kwargs: object) -> object:
                calls.append(list(cmd))
                raise OSError("Exec format error")

            with (
                mock.patch.object(audio_mod, "resolve_ffmpeg_bin", return_value="/fake/ffmpeg"),
                mock.patch.object(audio_mod.subprocess, "run", side_effect=_fake_run),
            ):
                out = archive_recording_audio(
                    wav,
                    rec_dir,
                    timeout_sec=1.0,
                    utterance_id="20261127-120001-ef01",
                )

            self.assertIsNotNone(out)
            assert out is not None
            self.assertEqual(len(calls), 1, "must break after first OSError without trying AAC")
            self.assertEqual(out.name, "20261127-120001-ef01.wav")
            self.assertEqual(out.read_bytes(), wav)
            self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
            self.assertEqual([p.name for p in rec_dir.iterdir()], ["20261127-120001-ef01.wav"])

    def test_nonzero_returncode_still_tries_aac_fallback(self) -> None:
        wav = _sample_wav_bytes()
        with tempfile.TemporaryDirectory() as td:
            rec_dir = Path(td) / "home" / "audio"
            calls: list[list[str]] = []

            def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
                calls.append(list(cmd))
                if len(calls) == 1:
                    return subprocess.CompletedProcess(cmd, 1, b"", b"mp3 encoder missing")
                Path(cmd[-1]).write_bytes(b"FAKE-M4A-DATA")
                return subprocess.CompletedProcess(cmd, 0, b"", b"")

            with (
                mock.patch.object(audio_mod, "resolve_ffmpeg_bin", return_value="/fake/ffmpeg"),
                mock.patch.object(audio_mod.subprocess, "run", side_effect=_fake_run),
            ):
                out = archive_recording_audio(
                    wav,
                    rec_dir,
                    timeout_sec=1.0,
                    utterance_id="20261127-120002-1234",
                )

            self.assertIsNotNone(out)
            assert out is not None
            self.assertEqual(len(calls), 2)
            self.assertEqual(out.name, "20261127-120002-1234.m4a")
            self.assertEqual(out.read_bytes(), b"FAKE-M4A-DATA")
            self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
            self.assertEqual([p.name for p in rec_dir.iterdir()], ["20261127-120002-1234.m4a"])


class TestBatch3BE2HttpResponseLimit(unittest.TestCase):
    def test_default_limit_constant_is_16_mib(self) -> None:
        self.assertEqual(DEFAULT_MAX_RESPONSE_BYTES, 16 * 1024 * 1024)

    def test_limits_chunked_no_cl_errors_and_pool_eviction(self) -> None:
        limit = 4096
        err_limit = 2048
        conn_lock = threading.Lock()
        client_ports: list[int] = []

        class _Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                return

            def _record_port(self) -> None:
                with conn_lock:
                    client_ports.append(int(self.client_address[1]))

            def _send_chunked(self, status: int, total_bytes: int, extra_hdrs: dict[str, str] | None = None) -> None:
                self.send_response(status)
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Connection", "keep-alive")
                for k, v in (extra_hdrs or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                rem = total_bytes
                block = b"c" * 1024
                try:
                    while rem > 0:
                        n = min(len(block), rem)
                        chunk = block[:n]
                        self.wfile.write(f"{n:x}\r\n".encode("ascii") + chunk + b"\r\n")
                        rem -= n
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except OSError:
                    self.close_connection = True

            def do_GET(self) -> None:
                self._record_port()
                if self.path == "/ping":
                    payload = b"ok"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if self.path == "/exact":
                    payload = b"a" * limit
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if self.path == "/over":
                    payload = b"b" * (limit + 1)
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()
                    try:
                        self.wfile.write(payload)
                    except OSError:
                        self.close_connection = True
                    return
                if self.path == "/chunked-exact":
                    self._send_chunked(200, limit)
                    return
                if self.path == "/chunked-over":
                    self._send_chunked(200, limit + 1)
                    return
                if self.path == "/no-cl-over":
                    self.send_response(200)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    try:
                        self.wfile.write(b"z" * (limit + 512))
                    except OSError:
                        pass
                    return
                if self.path == "/err-429-big":
                    self._send_chunked(
                        429,
                        err_limit + 4096,
                        extra_hdrs={"Retry-After": "7", "X-RateLimit-Remaining": "0"},
                    )
                    return
                if self.path == "/err-401-big":
                    payload = b"u" * (err_limit + 4096)
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Bearer realm="api"')
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()
                    try:
                        self.wfile.write(payload)
                    except OSError:
                        self.close_connection = True
                    return
                if self.path == "/redir":
                    self.send_response(302)
                    self.send_header("Location", "/ping")
                    self.send_header("Content-Length", "0")
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()
                    return
                if self.path == "/real-16m-exact":
                    total = DEFAULT_MAX_RESPONSE_BYTES
                    self.send_response(200)
                    self.send_header("Content-Length", str(total))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    blk = b"m" * (256 * 1024)
                    for _ in range(total // len(blk)):
                        self.wfile.write(blk)
                    return
                if self.path == "/real-16m-over":
                    total = DEFAULT_MAX_RESPONSE_BYTES + 1
                    self.send_response(200)
                    self.send_header("Content-Length", str(total))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    blk = b"m" * (256 * 1024)
                    try:
                        for _ in range(total // len(blk)):
                            self.wfile.write(blk)
                        self.wfile.write(b"!")
                    except OSError:
                        pass
                    return
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

        srv = _KeepAliveTestServer(("127.0.0.1", 0), _Handler)
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        base = f"http://127.0.0.1:{port}"
        evict_all_keepalive_connections()
        try:
            clean_env = {"NO_PROXY": "*"}

            # 1. Exact threshold (Content-Length & chunked) passes and keeps connection reusable
            r1 = urllib_http_transport(
                HttpRequest(f"{base}/exact", "GET", {}, b"", 5.0, max_response_bytes=limit),
                env=clean_env,
            )
            self.assertEqual(r1.status, 200)
            self.assertEqual(len(r1.body), limit)
            port_after_exact = client_ports[-1]

            r2 = urllib_http_transport(
                HttpRequest(f"{base}/chunked-exact", "GET", {}, b"", 5.0, max_response_bytes=limit),
                env=clean_env,
            )
            self.assertEqual(r2.status, 200)
            self.assertEqual(len(r2.body), limit)
            self.assertEqual(client_ports[-1], port_after_exact, "keep-alive connection should be reused")

            # 2. Threshold + 1 (Content-Length) raises HttpTransportError(retry_kind="never") & drops bad conn
            with self.assertRaises(HttpTransportError) as cm_over:
                urllib_http_transport(
                    HttpRequest(f"{base}/over", "GET", {}, b"", 5.0, max_response_bytes=limit),
                    env=clean_env,
                )
            self.assertEqual(cm_over.exception.retry_kind, "never")
            bad_port = client_ports[-1]

            r_after_over = urllib_http_transport(
                HttpRequest(f"{base}/ping", "GET", {}, b"", 5.0, max_response_bytes=limit),
                env=clean_env,
            )
            self.assertEqual(r_after_over.status, 200)
            self.assertEqual(r_after_over.body, b"ok")
            self.assertNotEqual(client_ports[-1], bad_port, "oversized response must close bad conn and open fresh one")

            # 3. Threshold + 1 (chunked) and no-Content-Length both fail with retry_kind="never"
            with self.assertRaises(HttpTransportError) as cm_chk:
                urllib_http_transport(
                    HttpRequest(f"{base}/chunked-over", "GET", {}, b"", 5.0, max_response_bytes=limit),
                    env=clean_env,
                )
            self.assertEqual(cm_chk.exception.retry_kind, "never")
            bad_chunked_port = client_ports[-1]

            r_after_chk = urllib_http_transport(
                HttpRequest(f"{base}/ping", "GET", {}, b"", 5.0, max_response_bytes=limit),
                env=clean_env,
            )
            self.assertEqual(r_after_chk.status, 200)
            self.assertNotEqual(client_ports[-1], bad_chunked_port)

            with self.assertRaises(HttpTransportError) as cm_nocl:
                urllib_http_transport(
                    HttpRequest(f"{base}/no-cl-over", "GET", {}, b"", 5.0, max_response_bytes=limit),
                    env=clean_env,
                )
            self.assertEqual(cm_nocl.exception.retry_kind, "never")

            # 4. Oversized 429 & 401 MUST preserve status & headers, truncate body, and close bad conn
            with mock.patch.object(http_mod, "DEFAULT_MAX_ERROR_RESPONSE_BYTES", err_limit):
                r429 = urllib_http_transport(
                    HttpRequest(f"{base}/err-429-big", "GET", {}, b"", 5.0, max_response_bytes=limit),
                    env=clean_env,
                )
                self.assertEqual(r429.status, 429)
                self.assertEqual(r429.headers.get("retry-after"), "7")
                self.assertEqual(r429.headers.get("x-ratelimit-remaining"), "0")
                self.assertEqual(len(r429.body), err_limit)
                port_429 = client_ports[-1]

                r_next = urllib_http_transport(
                    HttpRequest(f"{base}/ping", "GET", {}, b"", 5.0, max_response_bytes=limit),
                    env=clean_env,
                )
                self.assertEqual(r_next.status, 200)
                self.assertNotEqual(client_ports[-1], port_429, "truncated 429 must not return bad conn to pool")

                r401 = urllib_http_transport(
                    HttpRequest(f"{base}/err-401-big", "GET", {}, b"", 5.0, max_response_bytes=limit),
                    env=clean_env,
                )
                self.assertEqual(r401.status, 401)
                self.assertEqual(r401.headers.get("www-authenticate"), 'Bearer realm="api"')
                self.assertEqual(len(r401.body), err_limit)
                port_401 = client_ports[-1]

                r_next2 = urllib_http_transport(
                    HttpRequest(f"{base}/ping", "GET", {}, b"", 5.0, max_response_bytes=limit),
                    env=clean_env,
                )
                self.assertEqual(r_next2.status, 200)
                self.assertNotEqual(client_ports[-1], port_401, "truncated 401 must not return bad conn to pool")

            # 5. Redirect contract remains intact
            r_redir = urllib_http_transport(
                HttpRequest(f"{base}/redir", "GET", {}, b"", 5.0, max_response_bytes=limit),
                env=clean_env,
            )
            self.assertEqual(r_redir.status, 200)
            self.assertEqual(r_redir.body, b"ok")

            # 6. Default 16 MiB boundary works end-to-end on real payload
            r_16m = urllib_http_transport(
                HttpRequest(f"{base}/real-16m-exact", "GET", {}, b"", 15.0),
                env=clean_env,
            )
            self.assertEqual(r_16m.status, 200)
            self.assertEqual(len(r_16m.body), DEFAULT_MAX_RESPONSE_BYTES)

            with self.assertRaises(HttpTransportError) as cm_16m_over:
                urllib_http_transport(
                    HttpRequest(f"{base}/real-16m-over", "GET", {}, b"", 15.0),
                    env=clean_env,
                )
            self.assertEqual(cm_16m_over.exception.retry_kind, "never")
        finally:
            evict_all_keepalive_connections()
            srv.shutdown()
            srv.server_close()


class TestBatch3BE3UnixSocketPathLength(unittest.TestCase):
    def test_darwin_and_linux_exact_byte_boundaries(self) -> None:
        # macOS: 103 passes, 104 fails
        p103 = "/" + "a" * 102
        p104 = "/" + "a" * 103
        self.assertEqual(len(os.fsencode(p103)), 103)
        self.assertEqual(len(os.fsencode(p104)), 104)
        daemon_mod._validate_unix_socket_path_length(p103, platform="darwin")
        with self.assertRaises(OSError) as cm_mac:
            daemon_mod._validate_unix_socket_path_length(p104, platform="darwin")
        self.assertIn("104 bytes", str(cm_mac.exception))
        self.assertIn("103-byte", str(cm_mac.exception))
        self.assertIn("AGENT_VOICE_INPUT_RUNTIME_DIR", str(cm_mac.exception))

        # Linux/WSL2: 107 passes, 108 fails
        p107 = "/" + "a" * 106
        p108 = "/" + "a" * 107
        self.assertEqual(len(os.fsencode(p107)), 107)
        self.assertEqual(len(os.fsencode(p108)), 108)
        daemon_mod._validate_unix_socket_path_length(p107, platform="linux")
        with self.assertRaises(OSError) as cm_lin:
            daemon_mod._validate_unix_socket_path_length(p108, platform="linux")
        self.assertIn("108 bytes", str(cm_lin.exception))
        self.assertIn("107-byte", str(cm_lin.exception))
        self.assertIn("AGENT_VOICE_INPUT_RUNTIME_DIR", str(cm_lin.exception))

    def test_cjk_path_uses_encoded_byte_length_not_char_count(self) -> None:
        # 35 CJK chars * 3 UTF-8 bytes + 2 ASCII bytes = 107 bytes (char len 37) passes Linux; +1 byte = 108 fails
        cjk_107 = "/a" + "汉" * 35
        cjk_108 = "/aa" + "汉" * 35
        self.assertEqual(len(cjk_107), 37)
        self.assertEqual(len(cjk_107.encode("utf-8")), 107)
        self.assertEqual(len(cjk_108), 38)
        self.assertEqual(len(cjk_108.encode("utf-8")), 108)
        with mock.patch.object(daemon_mod.os, "fsencode", side_effect=lambda p: str(p).encode("utf-8")):
            daemon_mod._validate_unix_socket_path_length(cjk_107, platform="linux")
            with self.assertRaises(OSError) as cm:
                daemon_mod._validate_unix_socket_path_length(cjk_108, platform="linux")
            # macOS limit (103) rejects cjk_107 as well
            with self.assertRaises(OSError) as cm_mac:
                daemon_mod._validate_unix_socket_path_length(cjk_107, platform="darwin")
        self.assertIn("108 bytes", str(cm.exception))
        self.assertIn("AGENT_VOICE_INPUT_RUNTIME_DIR", str(cm.exception))
        self.assertIn("107 bytes", str(cm_mac.exception))
        self.assertIn("103-byte", str(cm_mac.exception))

    def test_serve_forever_rejects_long_socket_before_starting_bridge_without_leftovers(self) -> None:
        class _DummyPlatform:
            def __init__(self) -> None:
                self.started = False
                self.closed = False

            def start_event_listener(self, cb: object) -> None:
                self.started = True

            def close(self) -> None:
                self.closed = True

        class _DummyAsr:
            provider_name = "dummy"

        class _DummyRefiner:
            def close(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as td:
            # Make runtime_dir deep enough so runtime_dir / "control.sock" exceeds 107 bytes on all platforms
            deep_dir = Path(td)
            while len(os.fsencode(deep_dir / "control.sock")) <= 112:
                deep_dir = deep_dir / ("seg_" + "x" * 20)
            deep_dir.mkdir(parents=True, mode=0o700)
            plat = _DummyPlatform()
            cfg = Config(asr=AsrConfig(provider="dashscope"), refiner=RefinerConfig(provider="off"))
            env = {"HOME": td, "DASHSCOPE_API_KEY": "sk-test-e3"}
            d = daemon_mod.VoiceInputDaemon(
                cfg,
                env=env,
                runtime_dir=deep_dir,
                platform=plat,
                asr_provider=_DummyAsr(),  # type: ignore[arg-type]
                refiner=_DummyRefiner(),  # type: ignore[arg-type]
            )
            with self.assertRaises(OSError) as cm:
                d.serve_forever()
            self.assertIn("AGENT_VOICE_INPUT_RUNTIME_DIR", str(cm.exception))
            self.assertFalse(plat.started, "bridge must not be spawned when socket path exceeds limit")
            self.assertTrue(plat.closed)
            self.assertFalse(d.lock_path.exists())
            self.assertFalse(d.pid_path.exists())
            self.assertFalse(d.sock_path.exists())


class TestBatch3BE5WslPathBytes(unittest.TestCase):
    def test_cjk_and_trailing_space_preserved_and_ascii_surrogateescape_roundtrip(self) -> None:
        raw_win = b"C:\\Users\\\xe4\xb8\xad\xe6\x96\x87\\My Dir \r\n"
        expected_bytes = b"C:\\Users\\\xe4\xb8\xad\xe6\x96\x87\\My Dir "
        seen_kwargs: list[dict[str, object]] = []

        def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            seen_kwargs.append(dict(kwargs))
            return subprocess.CompletedProcess(cmd, 0, raw_win, b"")

        # UTF-8 fsdecode round-trip (preserving trailing space, stripping only \r\n)
        with (
            mock.patch.object(wsl_mod.os.path, "isfile", return_value=True),
            mock.patch.object(wsl_mod.subprocess, "run", side_effect=_fake_run),
        ):
            res = to_windows_path("/mnt/c/Users/中文/My Dir ")
        self.assertEqual(res, "C:\\Users\\中文\\My Dir ")
        self.assertNotIn("text", seen_kwargs[0], "must read bytes rather than text=True")

        # Simulated ASCII locale (LC_ALL=C): os.fsdecode uses surrogateescape and os.fsencode restores exact bytes
        with (
            mock.patch.object(wsl_mod.os.path, "isfile", return_value=True),
            mock.patch.object(wsl_mod.subprocess, "run", side_effect=_fake_run),
            mock.patch.object(
                wsl_mod.os,
                "fsdecode",
                side_effect=lambda b: b.decode("ascii", "surrogateescape") if isinstance(b, bytes) else str(b),
            ),
        ):
            res_ascii = to_windows_path("/mnt/c/Users/test")
        self.assertEqual(res_ascii.encode("ascii", "surrogateescape"), expected_bytes)

    def test_failed_candidate_with_non_ascii_stderr_falls_back_safely_and_raises_runtime_error(self) -> None:
        # 1. First candidate fails with non-UTF8/non-ASCII stderr -> falls through cleanly to second candidate
        call_idx = 0

        def _fake_run_fallback(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            nonlocal call_idx
            call_idx += 1
            if call_idx == 1:
                return subprocess.CompletedProcess(cmd, 1, b"\xff\xfeignored\r\n", b"\xff\xfe\xe4\xb8\xadbad")
            return subprocess.CompletedProcess(cmd, 0, b"C:\\ok\\path \n", b"")

        with (
            mock.patch.object(wsl_mod.os.path, "isfile", return_value=True),
            mock.patch.object(wsl_mod.subprocess, "run", side_effect=_fake_run_fallback),
        ):
            self.assertEqual(to_windows_path("/tmp/x"), "C:\\ok\\path ")

        # 2. All candidates fail with non-ASCII stderr -> raises RuntimeError (never UnicodeDecodeError)
        def _fake_run_all_fail(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(cmd, 2, b"", b"\x80\xff\xfe\xe9\x94\x99")

        with (
            mock.patch.object(wsl_mod.os.path, "isfile", return_value=True),
            mock.patch.object(wsl_mod.subprocess, "run", side_effect=_fake_run_all_fail),
        ):
            with self.assertRaises(RuntimeError):
                to_windows_path("/tmp/bad")


class TestBatch3BE6CustomHomeHintsAndDoctorFfmpegEnv(unittest.TestCase):
    def test_custom_app_home_and_config_hints_without_hardcoded_default(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            home.mkdir()
            custom_home = root / "custom_avi_home"
            custom_home.mkdir(mode=0o700)
            alt_cfg_dir = root / "alt_cfg_dir"
            alt_cfg_dir.mkdir(mode=0o700)
            alt_cfg_path = alt_cfg_dir / "my.toml"
            alt_cfg_path.write_text('[asr]\nprovider = "openai"\n', encoding="utf-8")
            rt = root / "rt"
            rt.mkdir(mode=0o700)

            env = {
                "HOME": str(home),
                "PATH": "",
                "AGENT_VOICE_INPUT_HOME": str(custom_home),
                "AGENT_VOICE_INPUT_CONFIG": str(alt_cfg_path),
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
            }
            expected_env_file = str(custom_home / ".env")
            expected_hw_file = str(custom_home / "hotwords" / "personal.txt")

            # 1. inspect_proxy_config_warning uses read-side default_env_file_candidates (custom_home/.env)
            proxy_warn = inspect_proxy_config_warning({**env, "ALL_PROXY": "socks5://127.0.0.1:1080"})
            self.assertIsNotNone(proxy_warn)
            assert proxy_warn is not None
            self.assertIn(expected_env_file, proxy_warn[1])
            self.assertNotIn("~/.agent-voice-input", proxy_warn[1])

            # 2. _provider_auth_fix_hint honors custom_home/.env and never confuses AGENT_VOICE_INPUT_CONFIG dir
            cfg_loaded, eff_env = load_config(env=env)
            for prov, stage, c_obj in (
                ("openai", "asr", cfg_loaded),
                ("codex", "refiner", cfg_loaded),
                ("openai-compatible", "refiner", Config(refiner=RefinerConfig(provider="openai-compatible", api_key_env="MY_KEY"))),
                ("unknown_prov", "asr", cfg_loaded),
            ):
                hint = cli_mod._provider_auth_fix_hint(prov, stage=stage, cfg=c_obj, env=eff_env)
                self.assertIn(expected_env_file, hint)
                self.assertNotIn("~/.agent-voice-input", hint)
                self.assertNotIn(str(alt_cfg_dir / ".env"), hint)

            # 3. _run_startup_preflight_ping model-not-found points to actual cfg.config_path, 401 to custom_home/.env
            cfg_oa = Config(
                asr=AsrConfig(provider="openai", model="gpt-transcribe-missing"),
                refiner=RefinerConfig(provider="off"),
                config_path=str(alt_cfg_path),
            )
            env_oa = {**env, "OPENAI_API_KEY": "sk-test-e6"}
            creds_oa = resolve_credentials(cfg_oa, env_oa)
            pipe_oa = {
                "asr": {"provider": "openai", "model": "gpt-transcribe-missing", "ready": True},
                "refiner": {"provider": "off", "model": "", "ready": False},
            }
            rows_model = cli_mod._run_startup_preflight_ping(
                cfg=cfg_oa,
                creds=creds_oa,
                env=env_oa,
                pipe_info=pipe_oa,
                transport=lambda _req: HttpResponse(200, {}, b'{"data":[{"id":"gpt-transcribe"}]}'),
            )
            joined_model = "\n".join(txt for _, txt in rows_model)
            self.assertIn(str(alt_cfg_path), joined_model)
            self.assertNotIn("~/.agent-voice-input", joined_model)

            rows_401 = cli_mod._run_startup_preflight_ping(
                cfg=cfg_oa,
                creds=creds_oa,
                env=env_oa,
                pipe_info=pipe_oa,
                transport=lambda _req: HttpResponse(401, {}, b'{"error":"bad"}'),
            )
            joined_401 = "\n".join(txt for _, txt in rows_401)
            self.assertIn(expected_env_file, joined_401)
            self.assertNotIn("~/.agent-voice-input", joined_401)

            # 4. doctor missing ASR key, codex refiner FAIL, and custom refiner.api_key_env all point to custom_home/.env
            for cfg_case in (
                Config(asr=AsrConfig(provider="auto"), refiner=RefinerConfig(provider="off")),
                Config(asr=AsrConfig(provider="auto"), refiner=RefinerConfig(provider="codex")),
                Config(
                    asr=AsrConfig(provider="auto"),
                    refiner=RefinerConfig(provider="openai-compatible", base_url="http://127.0.0.1:9999/v1", model="m", api_key_env="CUSTOM_REF_KEY"),
                ),
            ):
                buf = io.StringIO()
                with (
                    mock.patch.object(cli_mod.sys, "platform", "linux"),
                    mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                    mock.patch("agent_voice_input.audio_proc.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"),
                    mock.patch("agent_voice_input.refiner.inspect_codex_auth_status", return_value=("FAIL", "not logged in")),
                    mock.patch("sys.stdout", buf),
                ):
                    cli_mod._cmd_doctor(cfg_case, env)
                out = buf.getvalue()
                self.assertIn(expected_env_file, out)
                self.assertNotIn("~/.agent-voice-input", out)

            # 5. load_config rejected asr.hotwords_file points to custom_home/hotwords/personal.txt
            bad_cfg = alt_cfg_dir / "bad.toml"
            bad_cfg.write_text('[asr]\nprovider = "doubao"\nhotwords_file = "x.txt"\n', encoding="utf-8")
            with self.assertRaises(ConfigError) as cm_hw:
                load_config(config_path=bad_cfg, env=env)
            self.assertIn(expected_hw_file, str(cm_hw.exception))
            self.assertNotIn("~/.agent-voice-input", str(cm_hw.exception))

            # 6. Static --help text stays static (no host/custom env absolute path leaked)
            parser = cli_mod._build_parser()
            with mock.patch.dict(os.environ, {"AGENT_VOICE_INPUT_HOME": str(custom_home)}):
                init_help = parser._subparsers._group_actions[0].choices["init"].format_help()  # type: ignore[union-attr]
                hw_help = parser._subparsers._group_actions[0].choices["hotwords"].format_help()  # type: ignore[union-attr]
            self.assertNotIn(str(custom_home), init_help)
            self.assertNotIn(str(custom_home), hw_help)
            self.assertIn("AGENT_VOICE_INPUT_HOME", init_help)
            self.assertIn("AGENT_VOICE_INPUT_CONFIG", init_help)
            self.assertIn("AGENT_VOICE_INPUT_HOME", hw_help)

    def test_doctor_passes_st_env_to_resolve_ffmpeg_bin(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            home.mkdir()
            rt = root / "rt"
            rt.mkdir(mode=0o700)
            fake_bin_dir = root / "custom_bin"
            fake_bin_dir.mkdir(mode=0o700)
            fake_ff = fake_bin_dir / "ffmpeg"
            fake_ff.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_ff.chmod(0o755)

            orig_isfile = os.path.isfile

            def _hide_sys_ffmpeg(path: object) -> bool:
                if str(path) in ("/usr/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
                    return False
                return orig_isfile(path)

            env = {
                "HOME": str(home),
                "PATH": str(fake_bin_dir),
                "AGENT_VOICE_INPUT_RUNTIME_DIR": str(rt),
                "DEEPSEEK_API_KEY": "sk-test-ffmpeg-env",
            }
            buf = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"PATH": "/nonexistent_host_path"}, clear=False),
                mock.patch.object(audio_mod.os.path, "isfile", side_effect=_hide_sys_ffmpeg),
                mock.patch.object(cli_mod.sys, "platform", "linux"),
                mock.patch("agent_voice_input.platform_wsl.is_wsl", return_value=False),
                mock.patch("sys.stdout", buf),
            ):
                rc = cli_mod._cmd_doctor(Config(), env)
            out = buf.getvalue()
            self.assertEqual(rc, 0, out)
            self.assertIn(f"[ffmpeg]    OK ({fake_ff})", out)


class TestBatch3BE7HygieneChecks(unittest.TestCase):
    def test_remote_hook_header_claude_hook_issue_template_and_hotwords_comments(self) -> None:
        remote_hook = (_REPO_ROOT / "integrations" / "remote" / "remote-title-hook.sh").read_text(encoding="utf-8")
        self.assertIn("[#", remote_hook)
        self.assertIn("[@", remote_hook)
        self.assertIn("docs/context-routing.md", remote_hook)

        claude_hook = (_REPO_ROOT / "integrations" / "claude" / "voice-title.sh").read_text(encoding="utf-8")
        self.assertIn("--quiet >/dev/null 2>&1", claude_hook)

        bug_tpl = (_REPO_ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml").read_text(encoding="utf-8")
        self.assertNotIn("1.0.0", bug_tpl)
        self.assertIn("agent-voice-input --version", bug_tpl)

        hw_src = (_REPO_ROOT / "src" / "agent_voice_input" / "hotwords.py").read_text(encoding="utf-8")
        self.assertNotIn("docs/benchmarks/", hw_src)

        zh_readme = (_REPO_ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
        self.assertIn(
            "integrations/README.zh-CN.md#5-ssh-远端-linux-主机零安装直连--可选单文件标题钩子",
            zh_readme,
        )


if __name__ == "__main__":
    unittest.main()
