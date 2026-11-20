"""Offline tests for v0.11.0 long-running sleep/wake resilience and overhead bounds."""
from __future__ import annotations

import errno
import http.client
import http.server
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_voice_input.config import Config, RemoteConfig, RemoteHostConfig  # noqa: E402
from agent_voice_input.daemon import (  # noqa: E402
    VoiceInputDaemon,
    rotate_daemon_log_if_needed,
)
from agent_voice_input.diagnose import read_redacted_log_tail  # noqa: E402
from agent_voice_input.http_transport import (  # noqa: E402
    CancelToken,
    HttpRequest,
    HttpTransportError,
    _BoundedDnsResolver,
    _KEEPALIVE_POOL,
    _KeepAlivePool,
    _SplitTimeoutHTTPConnection,
    _is_idle_socket_stale,
    _resolve_system_proxy_key,
    evict_all_keepalive_connections,
    urllib_http_transport,
)
from agent_voice_input.platform_common import ForegroundWindow  # noqa: E402
from agent_voice_input.refiner import BaseRefiner, RefinerResult, _InstanceKeepAlivePool  # noqa: E402
from agent_voice_input.session_ctx import SessionMatch, SessionTracker  # noqa: E402


def _make_paired_conn(host: str = "127.0.0.1", port: int = 8080) -> tuple[_SplitTimeoutHTTPConnection, socket.socket]:
    client_sock, peer_sock = socket.socketpair()
    conn = _SplitTimeoutHTTPConnection(host, port, timeout=5.0)
    conn.sock = client_sock
    conn._cur_sock = client_sock
    return conn, peer_sock


class KeepAlivePoolAndBoundedDnsTests(unittest.TestCase):
    def tearDown(self) -> None:
        _KEEPALIVE_POOL.evict_all()

    def test_keepalive_pool_dual_clock_sleep_drift_and_clock_rollback_eviction(self) -> None:
        clocks = {"mono": 100.0, "wall": 1_700_000_000.0}
        pool = _KeepAlivePool(
            max_per_origin=3,
            idle_timeout_sec=45.0,
            sleep_drift_sec=15.0,
            mono_fn=lambda: clocks["mono"],
            wall_fn=lambda: clocks["wall"],
        )

        c1, p1 = _make_paired_conn()
        try:
            pool.release("http", "127.0.0.1", 8080, c1, proxy_key="")

            # Normal 10s progression (wall_age == mono_age == 10s < 45s): reuses socket
            clocks["mono"] += 10.0
            clocks["wall"] += 10.0
            got, reused = pool.acquire("http", "127.0.0.1", 8080, 5.0, proxy_key="")
            self.assertTrue(reused)
            self.assertIs(got, c1)
            pool.release("http", "127.0.0.1", 8080, got, proxy_key="")

            # Simulate OS sleep: monotonic barely advances (+1s) while wall clock jumps (+20s -> drift 19s > 15s)
            clocks["mono"] += 1.0
            clocks["wall"] += 20.0
            got2, reused2 = pool.acquire("http", "127.0.0.1", 8080, 5.0, proxy_key="")
            self.assertFalse(reused2)
            self.assertIsNot(got2, c1)
            self.assertIsNone(c1.sock)
            got2.close()
        finally:
            c1.close()
            p1.close()

        # Simulate large wall-clock rollback (-20s < -15s): evicts stale connection
        c2, p2 = _make_paired_conn()
        try:
            pool.release("http", "127.0.0.1", 8080, c2, proxy_key="")
            clocks["mono"] += 1.0
            clocks["wall"] -= 20.0
            got3, reused3 = pool.acquire("http", "127.0.0.1", 8080, 5.0, proxy_key="")
            self.assertFalse(reused3)
            self.assertIsNone(c2.sock)
            got3.close()
        finally:
            c2.close()
            p2.close()

        # Bidirectional drift: mono +30s while wall only +10s (wall rolled back 20s -> wall_age=+10s, |drift|=20s > 15s)
        c2b, p2b = _make_paired_conn()
        try:
            pool.release("http", "127.0.0.1", 8080, c2b, proxy_key="")
            clocks["mono"] += 30.0
            clocks["wall"] += 10.0
            got3b, reused3b = pool.acquire("http", "127.0.0.1", 8080, 5.0, proxy_key="")
            self.assertFalse(reused3b)
            self.assertIsNone(c2b.sock)
            got3b.close()
        finally:
            c2b.close()
            p2b.close()

        # Simulate normal monotonic TTL expiration (> 45s)
        c3, p3 = _make_paired_conn()
        try:
            pool.release("http", "127.0.0.1", 8080, c3, proxy_key="")
            clocks["mono"] += 46.0
            clocks["wall"] += 46.0
            got4, reused4 = pool.acquire("http", "127.0.0.1", 8080, 5.0, proxy_key="")
            self.assertFalse(reused4)
            self.assertIsNone(c3.sock)
            got4.close()
        finally:
            c3.close()
            p3.close()

    def test_keepalive_pool_zero_ms_select_probe_drops_eof_and_dirty_sockets(self) -> None:
        pool = _KeepAlivePool(max_per_origin=3, idle_timeout_sec=45.0)

        # 1) Peer closes connection (FIN/EOF) while idle -> 0ms select detects readable and drops it
        c_eof, p_eof = _make_paired_conn()
        try:
            pool.release("http", "127.0.0.1", 9000, c_eof, proxy_key="")
            p_eof.close()
            got, reused = pool.acquire("http", "127.0.0.1", 9000, 5.0, proxy_key="")
            self.assertFalse(reused)
            self.assertIsNone(c_eof.sock)
            got.close()
        finally:
            c_eof.close()
            p_eof.close()

        # 2) Stack has [clean_older, dirty_newer] -> drops dirty_newer and seamlessly reuses clean_older
        c_clean, p_clean = _make_paired_conn()
        c_dirty, p_dirty = _make_paired_conn()
        try:
            pool.release("http", "127.0.0.1", 9000, c_clean, proxy_key="")
            pool.release("http", "127.0.0.1", 9000, c_dirty, proxy_key="")
            p_dirty.sendall(b"HTTP/1.1 408 Request Timeout\r\n\r\n")
            got2, reused2 = pool.acquire("http", "127.0.0.1", 9000, 5.0, proxy_key="")
            self.assertTrue(reused2)
            self.assertIs(got2, c_clean)
            self.assertIsNone(c_dirty.sock)
        finally:
            c_clean.close()
            p_clean.close()
            c_dirty.close()
            p_dirty.close()

        # 3) Dirty socket passed to release() is rejected and closed immediately
        c_bad_rel, p_bad_rel = _make_paired_conn()
        try:
            p_bad_rel.sendall(b"stray")
            pool.release("http", "127.0.0.1", 9000, c_bad_rel, proxy_key="")
            self.assertIsNone(c_bad_rel.sock)
            self.assertEqual(pool.evict_all(), 0)
        finally:
            c_bad_rel.close()
            p_bad_rel.close()

        # 4) SSLSocket.pending() > 0 or negative fileno is immediately detected as stale
        class _PendingSsl:
            def pending(self) -> int:
                return 4

        class _ClosedFdSock:
            def fileno(self) -> int:
                return -1

        self.assertTrue(_is_idle_socket_stale(None))
        self.assertTrue(_is_idle_socket_stale(_PendingSsl()))
        self.assertTrue(_is_idle_socket_stale(_ClosedFdSock()))

    def test_keepalive_pool_proxy_key_isolation_and_inflight_proxy_switch(self) -> None:
        pool = _KeepAlivePool(max_per_origin=3, idle_timeout_sec=45.0)
        c_direct, p_direct = _make_paired_conn("api.example.com", 443)
        c_proxy_a, p_proxy_a = _make_paired_conn("api.example.com", 443)
        try:
            pool.release("https", "api.example.com", 443, c_direct, proxy_key="")
            pool.release(
                "https", "api.example.com", 443, c_proxy_a, proxy_key="http://proxy-a.corp:8080"
            )

            # Request for proxy-b must NOT reuse direct or proxy-a sockets
            got_b, reused_b = pool.acquire(
                "https", "api.example.com", 443, 5.0, proxy_key="http://proxy-b.corp:8080"
            )
            self.assertFalse(reused_b)
            got_b.close()

            # Request for proxy-a reuses c_proxy_a, and direct reuses c_direct
            got_a, reused_a = pool.acquire(
                "https", "api.example.com", 443, 5.0, proxy_key="http://proxy-a.corp:8080"
            )
            self.assertTrue(reused_a)
            self.assertIs(got_a, c_proxy_a)

            # In-flight proxy change: acquired under proxy-a, released after proxy switched to proxy-b -> dropped!
            pool.release(
                "https", "api.example.com", 443, got_a, proxy_key="http://proxy-b.corp:8080"
            )
            self.assertIsNone(got_a.sock)

            got_d, reused_d = pool.acquire("https", "api.example.com", 443, 5.0, proxy_key="")
            self.assertTrue(reused_d)
            self.assertIs(got_d, c_direct)
        finally:
            c_direct.close()
            p_direct.close()
            c_proxy_a.close()
            p_proxy_a.close()
            pool.evict_all()

        # Verify _InstanceKeepAlivePool proxy_key reflects dynamic HTTPS_PROXY/NO_PROXY without leaking credentials
        dyn_env = {"HTTPS_PROXY": "http://alice:super_secret_pw@proxy1.local:8080"}
        inst_pool = _InstanceKeepAlivePool(dyn_env, explicit_env=True)
        k1 = inst_pool._resolve_proxy_key("https", "api.openai.com", 443)
        self.assertIn("proxy1.local:8080", k1)
        self.assertNotIn("super_secret_pw", k1)
        dyn_env["HTTPS_PROXY"] = "http://alice:other_secret_pw@proxy1.local:8080"
        k2 = inst_pool._resolve_proxy_key("https", "api.openai.com", 443)
        self.assertNotEqual(k1, k2)
        dyn_env["NO_PROXY"] = "api.openai.com"
        self.assertEqual(inst_pool._resolve_proxy_key("https", "api.openai.com", 443), "")

    def test_keepalive_proxy_key_and_release_never_call_blocking_proxy_bypass_or_gethostbyname(self) -> None:
        # 1) Pure local NO_PROXY matching (CIDR, suffix, host:port) never touches proxy_bypass or gethostbyname
        with (
            mock.patch.dict(
                os.environ,
                {
                    "HTTPS_PROXY": "http://proxy.corp:8080",
                    "NO_PROXY": "127.0.0.0/8,.internal.corp,localhost:8443",
                },
                clear=False,
            ),
            mock.patch(
                "urllib.request.getproxies",
                return_value={"https": "http://proxy.corp:8080"},
            ),
            mock.patch(
                "urllib.request.proxy_bypass",
                side_effect=AssertionError("blocking urllib.request.proxy_bypass must not be called"),
            ),
            mock.patch(
                "socket.gethostbyname",
                side_effect=AssertionError("blocking socket.gethostbyname must not be called"),
            ),
        ):
            self.assertEqual(_resolve_system_proxy_key("https", "127.0.0.1", 443), "")
            self.assertEqual(_resolve_system_proxy_key("https", "api.internal.corp", 443), "")
            self.assertEqual(_resolve_system_proxy_key("https", "localhost", 8443), "")
            self.assertIn("proxy.corp:8080", _resolve_system_proxy_key("https", "localhost", 443))
            self.assertIn("proxy.corp:8080", _resolve_system_proxy_key("https", "api.openai.com", 443))

        # 1b) Lowercase no_proxy strictly overrides uppercase NO_PROXY (aligned with CPython urllib)
        with (
            mock.patch.dict(
                os.environ,
                {
                    "NO_PROXY": "*",
                    "no_proxy": "only-lower.local",
                },
                clear=False,
            ),
            mock.patch("urllib.request.getproxies", return_value={"https": "http://proxy.corp:8080"}),
        ):
            self.assertEqual(_resolve_system_proxy_key("https", "only-lower.local", 443), "")
            self.assertIn("proxy.corp:8080", _resolve_system_proxy_key("https", "api.openai.com", 443))

        with (
            mock.patch.dict(os.environ, {"NO_PROXY": "*", "no_proxy": ""}, clear=False),
            mock.patch("urllib.request.getproxies", return_value={"https": "http://proxy.corp:8080"}),
        ):
            self.assertIn("proxy.corp:8080", _resolve_system_proxy_key("https", "api.openai.com", 443))

        # 2) Full keepalive loopback request AND proxied request never call blocking proxy_bypass / gethostbyname
        proxied_paths: list[str] = []

        class _OkHandler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                proxied_paths.append(self.path)
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                pass

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _OkHandler)
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            with (
                mock.patch(
                    "urllib.request.proxy_bypass",
                    side_effect=AssertionError("blocking proxy_bypass called during keepalive request"),
                ),
                mock.patch(
                    "socket.gethostbyname",
                    side_effect=AssertionError("blocking gethostbyname called during keepalive request"),
                ),
            ):
                resp = urllib_http_transport(
                    HttpRequest(
                        url=f"http://127.0.0.1:{port}/ping",
                        method="GET",
                        headers={},
                        body=b"",
                        timeout_sec=3.0,
                    )
                )
            self.assertEqual(resp.status, 200)

            # Real proxied request through _NoDnsProxyHandler (never calls blocking proxy_bypass / gethostbyname)
            proxied_paths.clear()
            with (
                mock.patch.dict(os.environ, {"no_proxy": "", "NO_PROXY": ""}, clear=False),
                mock.patch("urllib.request.getproxies", return_value={"http": f"http://127.0.0.1:{port}"}),
                mock.patch(
                    "urllib.request.proxy_bypass",
                    side_effect=AssertionError("blocking proxy_bypass called during proxied request"),
                ),
                mock.patch(
                    "socket.gethostbyname",
                    side_effect=AssertionError("blocking gethostbyname called during proxied request"),
                ),
            ):
                resp_p = urllib_http_transport(
                    HttpRequest(
                        url=f"http://127.0.0.1:{port}/via-proxy",
                        method="GET",
                        headers={},
                        body=b"",
                        timeout_sec=3.0,
                    )
                )
            self.assertEqual(resp_p.status, 200)
            self.assertTrue(any(p.startswith("http://127.0.0.1:") for p in proxied_paths))
        finally:
            srv.shutdown()
            srv.server_close()
            th.join(timeout=2.0)
            _KEEPALIVE_POOL.evict_all()

    def test_keepalive_pool_evict_all_invalidates_inflight_lease_and_concurrent_stress(self) -> None:
        pool = _KeepAlivePool(max_per_origin=4, idle_timeout_sec=45.0)
        c1, p1 = _make_paired_conn()
        try:
            pool.release("http", "127.0.0.1", 8081, c1, proxy_key="")
            leased, reused = pool.acquire("http", "127.0.0.1", 8081, 5.0, proxy_key="")
            self.assertTrue(reused)
            self.assertIs(leased, c1)

            # Power resume triggers evict_all() while leased is in flight
            pool.evict_all()

            # Returning the pre-eviction lease must close and reject it
            pool.release("http", "127.0.0.1", 8081, leased, proxy_key="")
            self.assertIsNone(leased.sock)
            self.assertEqual(pool.evict_all(), 0)
        finally:
            c1.close()
            p1.close()

        # Concurrent acquire / release / evict_all stress with real socketpairs
        errors: list[BaseException] = []
        peers: list[socket.socket] = []
        peers_lock = threading.Lock()
        stop_evt = threading.Event()

        def _worker(tid: int) -> None:
            try:
                for i in range(40):
                    conn, reused = pool.acquire("http", "127.0.0.1", 8082, 2.0, proxy_key="")
                    if not reused:
                        cs, ps = socket.socketpair()
                        conn.sock = cs
                        conn._cur_sock = cs
                        with peers_lock:
                            peers.append(ps)
                    if (tid + i) % 7 == 0:
                        conn.abort_inflight()
                    pool.release("http", "127.0.0.1", 8082, conn, proxy_key="")
            except BaseException as exc:
                errors.append(exc)

        def _evictor() -> None:
            try:
                while not stop_evt.is_set():
                    pool.evict_all()
                    time.sleep(0.002)
            except BaseException as exc:
                errors.append(exc)

        ev_th = threading.Thread(target=_evictor, daemon=True)
        workers = [threading.Thread(target=_worker, args=(i,)) for i in range(4)]
        ev_th.start()
        for w in workers:
            w.start()
        for w in workers:
            w.join(timeout=10.0)
        stop_evt.set()
        ev_th.join(timeout=2.0)
        pool.evict_all()
        for ps in peers:
            ps.close()
        self.assertEqual(errors, [])

    def test_bounded_dns_cancel_token_interrupts_hung_getaddrinfo_immediately(self) -> None:
        dns_started = threading.Event()
        dns_release = threading.Event()

        def _hung_getaddrinfo(*_args: object, **_kwargs: object) -> list[object]:
            dns_started.set()
            dns_release.wait(timeout=5.0)
            return []

        tok = CancelToken()
        t0 = time.monotonic()

        def _cancel_soon() -> None:
            if dns_started.wait(timeout=2.0):
                time.sleep(0.02)
                tok.cancel("user_cancelled")

        canceller = threading.Thread(target=_cancel_soon, daemon=True)
        canceller.start()
        try:
            with (
                mock.patch("agent_voice_input.http_transport._should_use_system_proxy", return_value=False),
                mock.patch("socket.getaddrinfo", side_effect=_hung_getaddrinfo),
            ):
                req = HttpRequest(
                    url="https://hung-dns.example.invalid/v1/test",
                    method="GET",
                    headers={},
                    body=b"",
                    timeout_sec=5.0,
                    connect_timeout_sec=5.0,
                    cancel_token=tok,
                )
                with self.assertRaises(HttpTransportError) as ctx:
                    urllib_http_transport(req)
            elapsed = time.monotonic() - t0
            self.assertEqual(ctx.exception.retry_kind, "never")
            self.assertIn("cancelled", str(ctx.exception).lower())
            self.assertLess(elapsed, 0.5, f"DNS cancel took too long: {elapsed:.3f}s")
        finally:
            dns_release.set()
            canceller.join(timeout=2.0)

    def test_bounded_dns_timeout_and_max_inflight_worker_cap(self) -> None:
        resolver = _BoundedDnsResolver(max_inflight=2)
        gate = threading.Event()
        lock = threading.Lock()
        active = 0
        peak = 0
        calls = 0

        def _slow_dns(host: str, port: int, *_rest: object) -> list[tuple[object, ...]]:
            nonlocal active, peak, calls
            with lock:
                calls += 1
                active += 1
                peak = max(peak, active)
            try:
                gate.wait(timeout=5.0)
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
            finally:
                with lock:
                    active -= 1

        try:
            # 4 consecutive hung DNS lookups that time out quickly: at most 2 workers may ever run concurrently!
            for _ in range(4):
                with self.assertRaises(TimeoutError):
                    resolver.resolve(
                        "slow.example.invalid",
                        443,
                        deadline=time.monotonic() + 0.05,
                        resolver_fn=_slow_dns,
                    )
            self.assertLessEqual(peak, 2)
            self.assertEqual(calls, 2)
        finally:
            gate.set()

        # Once stuck workers drain, slots recover cleanly and subsequent lookups succeed
        deadline_wait = time.monotonic() + 2.0
        while time.monotonic() < deadline_wait:
            with lock:
                if active == 0:
                    break
            time.sleep(0.01)

        out = resolver.resolve(
            "ok.example.invalid",
            443,
            deadline=time.monotonic() + 1.0,
            resolver_fn=lambda host, port, *_: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))],
        )
        self.assertEqual(len(out), 1)


class PowerResumeAndDaemonLogRotationTests(unittest.TestCase):
    def tearDown(self) -> None:
        _KEEPALIVE_POOL.evict_all()

    def test_daemon_power_resume_evicts_pools_clears_stale_state_without_toast(self) -> None:
        class _DummyPlatform:
            def __init__(self) -> None:
                self.hud_calls: list[tuple[str, str]] = []

            def start_event_listener(self, cb: object) -> None:
                self._cb = cb

            def hud(self, state: str, text: str = "", *_a: object, **_kw: object) -> None:
                self.hud_calls.append((state, text))

            def close(self) -> None:
                pass

        class _DummyRefiner(BaseRefiner):
            provider_name = "dummy"

            def __init__(self) -> None:
                self.evict_calls = 0

            def refine(self, asr_input: object, **_kw: object) -> RefinerResult:
                return RefinerResult(text=str(asr_input), provider="dummy", model="m")

            def evict_connections(self) -> int:
                self.evict_calls += 1
                return 2

        with tempfile.TemporaryDirectory() as td:
            rt_dir = Path(td) / "rt"
            plat = _DummyPlatform()
            ref = _DummyRefiner()
            cfg = Config()
            cfg.asr.provider = "openai"
            cfg.refiner.provider = "off"
            daemon = VoiceInputDaemon(
                cfg,
                env={"HOME": td, "XDG_RUNTIME_DIR": str(rt_dir), "OPENAI_API_KEY": "sk-dummy-test"},
                runtime_dir=rt_dir,
                platform=plat,
                refiner=ref,
            )
            c_shared, p_shared = _make_paired_conn("api.openai.com", 443)
            try:
                _KEEPALIVE_POOL.release("https", "api.openai.com", 443, c_shared, proxy_key="")
                daemon._origin_win = ForegroundWindow(hwnd=999, process="WindowsTerminal.exe", title="old")
                daemon._last_trigger_ts = 12345.0
                daemon._clipboard_busy_until = 99999.0
                daemon._recorder_lost_stop_absorbed = True
                daemon._auto_stop_absorbed = True

                log_buf: list[str] = []
                with mock.patch("sys.stderr") as fake_err:
                    fake_err.write.side_effect = log_buf.append
                    daemon._on_platform_event({"event": "power_resume", "ts_ms": 42000})

                # Shared pool evicted and socket closed; refiner pool evicted
                self.assertIsNone(c_shared.sock)
                self.assertEqual(_KEEPALIVE_POOL.evict_all(), 0)
                self.assertEqual(ref.evict_calls, 1)

                # Stale window/hotkey transient state cleared
                self.assertEqual(daemon._origin_win, ForegroundWindow())
                self.assertLess(daemon._last_trigger_ts, 0.0)
                self.assertLess(daemon._clipboard_busy_until, 0.0)
                self.assertFalse(daemon._recorder_lost_stop_absorbed)
                self.assertFalse(daemon._auto_stop_absorbed)

                # Zero HUD / toast noise on power_resume
                self.assertEqual(plat.hud_calls, [])
                self.assertIn("power_resume", "".join(log_buf))

                # While actively recording, power_resume preserves _origin_win while still clearing debounce & pools
                active_win = ForegroundWindow(hwnd=777, process="Ghostty", title="active")
                daemon._recording_active = True
                daemon._origin_win = active_win
                daemon._last_trigger_ts = 555.0
                with mock.patch("sys.stderr"):
                    daemon._on_platform_event({"event": "power_resume", "ts_ms": 50000})
                self.assertEqual(daemon._origin_win, active_win)
                self.assertLess(daemon._last_trigger_ts, 0.0)
                self.assertEqual(ref.evict_calls, 2)
                self.assertEqual(plat.hud_calls, [])
            finally:
                c_shared.close()
                p_shared.close()
                daemon.close()

    def test_daemon_ptt_sleep_wake_sequence_stops_recording_and_evicts_pools(self) -> None:
        class _PttWakePlatform:
            def __init__(self) -> None:
                self.rec_started = 0
                self.rec_stopped = 0
                self.hud_calls: list[tuple[str, str]] = []
                self.last_audio_error = ""

            def start_event_listener(self, cb: object) -> None:
                self._cb = cb

            def is_allowed_terminal(self, window: ForegroundWindow) -> bool:
                return True

            def get_foreground_window(self) -> ForegroundWindow:
                return ForegroundWindow(hwnd=101, process="WindowsTerminal.exe", title="codex")

            def resolve_foreground_window(self, hint: ForegroundWindow | None = None) -> ForegroundWindow:
                return hint if (hint and hint.process) else self.get_foreground_window()

            def start_recording(self, runtime_dir: Path, *, loopback: bool = False, seq_id: int | None = None) -> None:
                _ = (runtime_dir, loopback, seq_id)
                self.rec_started += 1

            def stop_recording(self, *, drain: bool = True) -> tuple[bytes, str]:
                _ = drain
                self.rec_stopped += 1
                return b"", "mock_mic"

            def cancel_recording(self) -> None:
                pass

            def beep(self, kind: str) -> None:
                _ = kind

            def hud(self, state: str, text: str = "", *_a: object, **_kw: object) -> None:
                self.hud_calls.append((state, text))

            def close(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as td:
            rt_dir = Path(td) / "rt"
            plat = _PttWakePlatform()
            cfg = Config()
            cfg.asr.provider = "openai"
            cfg.refiner.provider = "off"
            cfg.hotkey.trigger_mode = "ptt"
            daemon = VoiceInputDaemon(
                cfg,
                env={"HOME": td, "XDG_RUNTIME_DIR": str(rt_dir), "OPENAI_API_KEY": "sk-dummy-test"},
                runtime_dir=rt_dir,
                platform=plat,
            )
            c_shared, p_shared = _make_paired_conn("api.openai.com", 443)
            try:
                _KEEPALIVE_POOL.release("https", "api.openai.com", 443, c_shared, proxy_key="")
                # User holds PTT -> starts recording
                daemon._on_platform_event({"event": "ptt_start", "hwnd": 101, "process": "WindowsTerminal.exe", "title": "codex"})
                self.assertEqual(plat.rec_started, 1)
                self.assertTrue(daemon._recording_active)

                # System sleeps & wakes while PTT was held -> shim emits ptt_stop BEFORE power_resume
                hud_before_resume = len(plat.hud_calls)
                with mock.patch("sys.stderr"):
                    daemon._on_platform_event({"event": "ptt_stop", "hwnd": 101, "process": "WindowsTerminal.exe", "title": "codex"})
                    hud_after_stop = len(plat.hud_calls)
                    daemon._on_platform_event({"event": "power_resume", "ts_ms": 99000})

                self.assertEqual(plat.rec_stopped, 1)
                self.assertFalse(daemon._recording_active)
                self.assertIsNone(c_shared.sock)
                self.assertEqual(_KEEPALIVE_POOL.evict_all(), 0)
                # power_resume itself emits zero extra HUD/toast calls
                self.assertEqual(len(plat.hud_calls), hud_after_stop)
                self.assertGreaterEqual(hud_after_stop, hud_before_resume)
            finally:
                c_shared.close()
                p_shared.close()
                daemon.close()

    def test_power_resume_bridge_contracts_in_win_and_mac_shims(self) -> None:
        win_path = REPO_ROOT / "src" / "agent_voice_input" / "win_shim.ps1"
        mac_path = REPO_ROOT / "src" / "agent_voice_input" / "mac_shim.swift"
        win_bytes = win_path.read_bytes()
        self.assertTrue(all(b < 0x80 for b in win_bytes), "win_shim.ps1 must stay strictly 7-bit ASCII")

        win_txt = win_bytes.decode("ascii")
        for token in (
            "WM_POWERBROADCAST",
            "PBT_APMRESUMEAUTOMATIC",
            "PBT_APMRESUMESUSPEND",
            "PowerBroadcastWindow",
            "ReinstallKeyboardHook",
            "ResetForPowerResume",
            "PowerResumeDebouncer",
            "RunPowerResumeSelfCheck",
            'event = "power_resume"',
        ):
            self.assertIn(token, win_txt)

        mac_txt = mac_path.read_text(encoding="utf-8")
        for token in (
            "NSWorkspace.didWakeNotification",
            "resetForPowerResume",
            "PowerResumeDebouncer",
            '"event": "power_resume"',
        ):
            self.assertIn(token, mac_txt)
        # Ensure self-check exits BEFORE registering NSWorkspace.didWakeNotification (headless safety)
        self.assertLess(
            mac_txt.index('if mode == "self-check" { Foundation.exit(runSelfCheck()) }'),
            mac_txt.index("NSWorkspace.didWakeNotification"),
        )
        # Ensure macOS wake observer checks debounce BEFORE resetting hotkey engine
        wake_idx = mac_txt.index("NSWorkspace.didWakeNotification")
        wake_block = mac_txt[wake_idx : wake_idx + 400]
        self.assertLess(
            wake_block.index("powerResumeDebouncer.shouldEmit"),
            wake_block.index("engine.resetForPowerResume()"),
        )

    def test_daemon_log_rotation_threshold_permissions_diagnose_and_error_resilience(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rt_dir = Path(td)
            log_path = rt_dir / "daemon.log"
            backup_path = rt_dir / "daemon.log.1"

            # Below threshold -> no rotation
            log_path.write_text("line-1\n", encoding="utf-8")
            os.chmod(log_path, 0o600)
            self.assertFalse(rotate_daemon_log_if_needed(log_path, max_bytes=256, rebind_stdio=False))
            self.assertFalse(backup_path.exists())

            # Exceed threshold -> rotates to daemon.log.1 with 0600, creates empty 0600 daemon.log
            payload1 = ("INFO first batch line\n" * 20)
            log_path.write_text(payload1, encoding="utf-8")
            os.chmod(log_path, 0o644)
            self.assertTrue(rotate_daemon_log_if_needed(log_path, max_bytes=256, rebind_stdio=False))
            self.assertTrue(log_path.is_file())
            self.assertTrue(backup_path.is_file())
            self.assertEqual(stat.S_IMODE(os.lstat(log_path).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.lstat(backup_path).st_mode), 0o600)
            self.assertEqual(log_path.read_text(encoding="utf-8"), "")
            self.assertEqual(backup_path.read_text(encoding="utf-8"), payload1)

            # diagnose tail falls back to daemon.log.1 seamlessly right after rotation while daemon.log is empty
            tail = read_redacted_log_tail(log_path, -1, env={"HOME": td})
            self.assertIn("INFO first batch line", tail)

            # Second rotation replaces daemon.log.1 (keeps only 1 historical backup)
            payload2 = ("INFO second batch line\n" * 20)
            log_path.write_text(payload2, encoding="utf-8")
            self.assertTrue(rotate_daemon_log_if_needed(log_path, max_bytes=256, rebind_stdio=False))
            self.assertEqual(backup_path.read_text(encoding="utf-8"), payload2)
            self.assertFalse((rt_dir / "daemon.log.2").exists())

            # Symlink safety: refuse to rotate when backup is a symlink
            victim = rt_dir / "victim.txt"
            victim.write_text("keep-me", encoding="utf-8")
            backup_path.unlink()
            backup_path.symlink_to(victim)
            log_path.write_text(payload2, encoding="utf-8")
            self.assertFalse(rotate_daemon_log_if_needed(log_path, max_bytes=256, rebind_stdio=False))
            self.assertEqual(victim.read_text(encoding="utf-8"), "keep-me")
            backup_path.unlink()

            # Error resilience 1: os.open ENOSPC leaves original daemon.log intact and rotation recovers later
            log_path.write_text(payload2, encoding="utf-8")
            with mock.patch(
                "agent_voice_input.daemon.os.open",
                side_effect=OSError(errno.ENOSPC, "No space left on device"),
            ):
                self.assertFalse(rotate_daemon_log_if_needed(log_path, max_bytes=256, rebind_stdio=False))
            self.assertTrue(log_path.is_file())
            self.assertEqual(log_path.read_text(encoding="utf-8"), payload2)
            self.assertEqual(list(rt_dir.glob(".daemon.log.rotate.*.tmp")), [])

            # Error resilience 2: second os.replace (tmp -> log_path) ENOSPC rolls back backup -> log_path cleanly
            real_replace = os.replace
            replace_calls = [0]

            def _fail_second_replace(src: object, dst: object) -> None:
                replace_calls[0] += 1
                if replace_calls[0] == 2:
                    raise OSError(errno.ENOSPC, "No space left on device")
                real_replace(src, dst)

            with mock.patch("agent_voice_input.daemon.os.replace", side_effect=_fail_second_replace):
                self.assertFalse(rotate_daemon_log_if_needed(log_path, max_bytes=256, rebind_stdio=False))
            self.assertTrue(log_path.is_file())
            self.assertEqual(log_path.read_text(encoding="utf-8"), payload2)
            self.assertEqual(list(rt_dir.glob(".daemon.log.rotate.*.tmp")), [])

            # Once ENOSPC clears, subsequent writes & rotation succeed normally
            with open(log_path, "a", encoding="utf-8") as af:
                af.write("recovered-after-enospc\n")
            self.assertTrue(rotate_daemon_log_if_needed(log_path, max_bytes=256, rebind_stdio=False))
            self.assertIn("recovered-after-enospc", backup_path.read_text(encoding="utf-8"))
            self.assertEqual(log_path.read_text(encoding="utf-8"), "")

            # Subprocess verification: real stdout/stderr fd redirected to daemon.log rebinds cleanly on rotation
            sub_log = rt_dir / "sub_daemon.log"
            sub_code = (
                "import sys; from pathlib import Path; "
                "from agent_voice_input.daemon import rotate_daemon_log_if_needed; "
                "p = Path(sys.argv[1]); "
                "print('OLD_LINE_' * 40, file=sys.stderr, flush=True); "
                "rotated = rotate_daemon_log_if_needed(p, max_bytes=128, rebind_stdio=True); "
                "assert rotated is True; "
                "print('NEW_AFTER_ROTATE', file=sys.stderr, flush=True)"
            )
            with open(sub_log, "ab") as lf:
                proc = subprocess.run(
                    [sys.executable, "-c", sub_code, str(sub_log)],
                    stdin=subprocess.DEVNULL,
                    stdout=lf,
                    stderr=lf,
                    env={"PYTHONPATH": str(SRC_DIR)},
                    timeout=10,
                    check=False,
                )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("OLD_LINE_", (rt_dir / "sub_daemon.log.1").read_text(encoding="utf-8"))
            self.assertIn("NEW_AFTER_ROTATE", sub_log.read_text(encoding="utf-8"))
            self.assertNotIn("OLD_LINE_", sub_log.read_text(encoding="utf-8"))

            # Subprocess partial-dup2 failure self-heal: 2nd dup2 (stderr) fails on rotation,
            # then next rotate_daemon_log_if_needed call self-heals stderr back onto primary log
            heal_log = rt_dir / "sub_heal.log"
            heal_code = (
                "import errno, os, sys; from pathlib import Path; from unittest import mock; "
                "from agent_voice_input.daemon import rotate_daemon_log_if_needed; "
                "p = Path(sys.argv[1]); "
                "print('BEFORE_PARTIAL_' * 20, file=sys.stderr, flush=True); "
                "real_dup2 = os.dup2; calls = [0]\n"
                "def _fail_second_dup2(fd, fd2):\n"
                "    calls[0] += 1\n"
                "    if calls[0] == 2:\n"
                "        raise OSError(errno.EBADF, 'simulated partial dup2 failure')\n"
                "    return real_dup2(fd, fd2)\n"
                "with mock.patch('agent_voice_input.daemon.os.dup2', side_effect=_fail_second_dup2):\n"
                "    ok1 = rotate_daemon_log_if_needed(p, max_bytes=128, rebind_stdio=True)\n"
                "assert ok1 is False\n"
                "print('STILL_ON_BACKUP', file=sys.stderr, flush=True)\n"
                "ok2 = rotate_daemon_log_if_needed(p, max_bytes=128, rebind_stdio=True)\n"
                "assert ok2 is False\n"
                "print('HEALED_ON_PRIMARY', file=sys.stderr, flush=True)\n"
            )
            with open(heal_log, "ab") as hf:
                proc_h = subprocess.run(
                    [sys.executable, "-c", heal_code, str(heal_log)],
                    stdin=subprocess.DEVNULL,
                    stdout=hf,
                    stderr=hf,
                    env={"PYTHONPATH": str(SRC_DIR)},
                    timeout=10,
                    check=False,
                )
            self.assertEqual(proc_h.returncode, 0)
            self.assertIn("STILL_ON_BACKUP", (rt_dir / "sub_heal.log.1").read_text(encoding="utf-8"))
            self.assertIn("HEALED_ON_PRIMARY", heal_log.read_text(encoding="utf-8"))
            self.assertNotIn("HEALED_ON_PRIMARY", (rt_dir / "sub_heal.log.1").read_text(encoding="utf-8"))


class SessionTrackerShortcutAndBoundedCacheTests(unittest.TestCase):
    def test_session_tracker_o1_unchanged_shortcut_zero_rehash_and_compaction_detection(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            rt = root / "rt"
            proj = root / "proj"
            home.mkdir()
            rt.mkdir()
            proj.mkdir()

            sid = "aaaa1111-2222-4333-8444-555566667777"
            cl_dir = home / ".claude" / "projects" / "-tmp-proj"
            cl_dir.mkdir(parents=True)
            jsonl_path = cl_dir / f"{sid}.jsonl"

            # Build a sizable (~120 KiB) multi-turn Claude transcript
            pad = "x" * 1000
            lines = [
                json.dumps(
                    {
                        "type": "user",
                        "isSidechain": False,
                        "cwd": str(proj),
                        "message": {"role": "user", "content": f"turn-{i} {pad}"},
                    }
                )
                for i in range(110)
            ]
            jsonl_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            tracker = SessionTracker(
                home_dir=home,
                runtime_dir=rt,
                proc_root=root / "empty_proc",
                env={"HOME": str(home), "XDG_RUNTIME_DIR": str(rt)},
                cold_start_fallback="off",
            )
            match = SessionMatch(
                cli="claude",
                session_id=sid,
                jsonl_path=str(jsonl_path),
                cwd=str(proj),
                matched_by="title_tag",
            )

            snap1 = tracker.sync_session(match=match, include_git_info=False)
            assert snap1 is not None
            self.assertEqual(len(snap1.clean_turns), 110)
            self.assertEqual(snap1.byte_offset, jsonl_path.stat().st_size)

            # 2nd and 3rd syncs on unchanged file MUST short-circuit in O(1) with ZERO prefix hashing!
            with (
                mock.patch.object(
                    tracker, "_verify_consumed_prefix", wraps=tracker._verify_consumed_prefix
                ) as spy_verify,
                mock.patch(
                    "agent_voice_input.session_ctx._hash_file_prefix",
                    wraps=__import__("agent_voice_input.session_ctx", fromlist=["_hash_file_prefix"])._hash_file_prefix,
                ) as spy_hash,
            ):
                snap2 = tracker.sync_session(match=match, include_git_info=False)
                snap3 = tracker.sync_session(match=match, include_git_info=False)
                self.assertEqual(spy_verify.call_count, 0)
                self.assertEqual(spy_hash.call_count, 0)
            assert snap2 is not None and snap3 is not None
            self.assertEqual(snap2.byte_offset, snap1.byte_offset)
            self.assertEqual(len(snap3.clean_turns), 110)

            # Appending a new line breaks O(1) shortcut, verifies prefix once, and returns to O(1) after
            with open(jsonl_path, "ab") as f:
                f.write(
                    (
                        json.dumps(
                            {
                                "type": "user",
                                "isSidechain": False,
                                "cwd": str(proj),
                                "message": {"role": "user", "content": "turn-110-appended"},
                            }
                        )
                        + "\n"
                    ).encode("utf-8")
                )
            snap_app = tracker.sync_session(match=match, include_git_info=False)
            assert snap_app is not None
            self.assertEqual(len(snap_app.clean_turns), 111)
            self.assertEqual(snap_app.clean_turns[-1], ("user", "turn-110-appended"))

            with mock.patch.object(
                tracker, "_verify_consumed_prefix", wraps=tracker._verify_consumed_prefix
            ) as spy_verify_after:
                snap_app_again = tracker.sync_session(match=match, include_git_info=False)
                self.assertEqual(spy_verify_after.call_count, 0)
            assert snap_app_again is not None
            self.assertEqual(len(snap_app_again.clean_turns), 111)

            # Compaction / truncation (shorter rewrite) still resets cleanly
            compacted = (
                json.dumps(
                    {
                        "type": "user",
                        "isSidechain": False,
                        "cwd": str(proj),
                        "message": {"role": "user", "content": "post-compaction-turn"},
                    }
                )
                + "\n"
            )
            jsonl_path.write_text(compacted, encoding="utf-8")
            snap_comp = tracker.sync_session(match=match, include_git_info=False)
            assert snap_comp is not None
            self.assertGreater(snap_comp.generation, snap_app.generation)
            self.assertEqual(list(snap_comp.clean_turns), [("user", "post-compaction-turn")])

            # Same-length in-place overwrite after coarse tick still detects corruption & resets
            old_bytes = jsonl_path.read_bytes()
            new_bytes = old_bytes.replace(b"post-compaction-turn", b"overwritten-same-len")
            self.assertEqual(len(new_bytes), len(old_bytes))
            st_pre = os.stat(jsonl_path)
            time.sleep(0.02)
            with open(jsonl_path, "r+b") as f:
                f.seek(0)
                f.write(new_bytes)
            if os.stat(jsonl_path).st_mtime_ns == st_pre.st_mtime_ns:
                os.utime(jsonl_path, ns=(st_pre.st_atime_ns, st_pre.st_mtime_ns + 1_000_000))
            snap_ow = tracker.sync_session(match=match, include_git_info=False)
            assert snap_ow is not None
            self.assertGreater(snap_ow.generation, snap_comp.generation)
            self.assertEqual(list(snap_ow.clean_turns), [("user", "overwritten-same-len")])

    def test_session_tracker_bounded_states_and_workspace_caches_lru_eviction(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            rt = root / "rt"
            home.mkdir()
            rt.mkdir()
            tracker = SessionTracker(
                home_dir=home,
                runtime_dir=rt,
                proc_root=root / "empty_proc",
                env={"HOME": str(home), "XDG_RUNTIME_DIR": str(rt)},
                cold_start_fallback="off",
                max_states=4,
                max_workspace_cache=3,
                git_cache_ttl_sec=60.0,
                repo_files_cache_ttl_sec=60.0,
            )

            matches: list[SessionMatch] = []
            for idx in range(7):
                sid = f"0000000{idx}-1111-4222-8333-444455556666"
                p = root / f"s{idx}.jsonl"
                p.write_text(
                    json.dumps(
                        {
                            "type": "user",
                            "isSidechain": False,
                            "message": {"role": "user", "content": f"hello-{idx}"},
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                matches.append(
                    SessionMatch(
                        cli="claude",
                        session_id=sid,
                        jsonl_path=str(p),
                        cwd=str(root / f"ws{idx}"),
                        matched_by="title_tag",
                    )
                )

            # Sync s0..s3 (fills capacity 4)
            for idx in range(4):
                tracker.sync_session(match=matches[idx], include_git_info=False)
            self.assertEqual(len(tracker._states), 4)

            # Touch s0 via get_state so s1 becomes the LRU victim; pin s1 to verify active-pin protection
            self.assertIsNotNone(tracker.get_state(matches[0].session_key))
            with tracker._lock:
                tracker._pin_state_locked(matches[1].session_key)

            # Sync s4, s5 -> evicts s2 and s3, NEVER evicting pinned s1 or recently touched s0
            tracker.sync_session(match=matches[4], include_git_info=False)
            tracker.sync_session(match=matches[5], include_git_info=False)
            self.assertEqual(len(tracker._states), 4)
            self.assertIn(matches[1].session_key, tracker._states)
            self.assertIn(matches[0].session_key, tracker._states)
            self.assertNotIn(matches[2].session_key, tracker._states)
            self.assertNotIn(matches[3].session_key, tracker._states)

            # Sync s6 while s1 is pinned -> capacity remains 4, s1 stays protected, s0 evicted
            tracker.sync_session(match=matches[6], include_git_info=False)
            self.assertEqual(len(tracker._states), 4)
            self.assertIn(matches[1].session_key, tracker._states)
            self.assertNotIn(matches[0].session_key, tracker._states)

            # Unpin s1 and sync s0 again -> now s1 can be evicted normally
            with tracker._lock:
                tracker._unpin_state_locked(matches[1].session_key)
            tracker.sync_session(match=matches[0], include_git_info=False)
            self.assertEqual(len(tracker._states), 4)
            self.assertNotIn(matches[1].session_key, tracker._states)

            # Workspace caches (_git_cache, _repo_cache, _shallow_cache) stay bounded at max_workspace_cache=3
            with (
                mock.patch(
                    "agent_voice_input.session_ctx.read_git_status_summary",
                    side_effect=lambda cwd, **_kw: (f"status:{cwd}", [Path(cwd).name]),
                ),
                mock.patch(
                    "agent_voice_input.session_ctx.read_repo_core_files",
                    side_effect=lambda cwd, **_kw: [f"{Path(cwd).name}.py"],
                ),
                mock.patch(
                    "agent_voice_input.session_ctx.read_workspace_shallow_files",
                    side_effect=lambda cwd, **_kw: [f"{Path(cwd).name}.md"],
                ),
            ):
                for idx in range(6):
                    ws = str(root / f"ws{idx}")
                    tracker._get_git_info(ws)
                    tracker._get_repo_files(ws)
                    tracker._get_shallow_files(ws)
                self.assertEqual(len(tracker._git_cache), 3)
                self.assertEqual(len(tracker._repo_cache), 3)
                self.assertEqual(len(tracker._shallow_cache), 3)
                self.assertNotIn(str(root / "ws0"), tracker._git_cache)
                self.assertIn(str(root / "ws5"), tracker._git_cache)

    def test_session_tracker_mid_scan_stat_change_does_not_cache_verified_stat(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            rt = root / "rt"
            home.mkdir()
            rt.mkdir()
            sid = "bbbb1111-2222-4333-8444-555566667777"
            p = root / f"{sid}.jsonl"
            p.write_text(
                json.dumps(
                    {
                        "type": "user",
                        "isSidechain": False,
                        "message": {"role": "user", "content": "mid-scan-turn"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            tracker = SessionTracker(
                home_dir=home,
                runtime_dir=rt,
                proc_root=root / "empty_proc",
                env={"HOME": str(home), "XDG_RUNTIME_DIR": str(rt)},
                cold_start_fallback="off",
            )
            match = SessionMatch(
                cli="claude",
                session_id=sid,
                jsonl_path=str(p),
                cwd=str(root),
                matched_by="title_tag",
            )

            # Simulate concurrent equal-length rewrite mid-scan: post_stat.st_mtime_ns != pre_stat.st_mtime_ns
            orig_fstat = os.fstat
            fstat_calls = [0]

            class _MutatedStat:
                def __init__(self, real_st: os.stat_result, bump_ns: int) -> None:
                    self.st_dev = real_st.st_dev
                    self.st_ino = real_st.st_ino
                    self.st_size = real_st.st_size
                    self.st_mtime_ns = real_st.st_mtime_ns + bump_ns
                    self.st_ctime_ns = real_st.st_ctime_ns + bump_ns

            def _mid_scan_fstat(fd: int) -> object:
                fstat_calls[0] += 1
                real_st = orig_fstat(fd)
                return _MutatedStat(real_st, 1_000_000 if fstat_calls[0] >= 2 else 0)

            with mock.patch("agent_voice_input.session_ctx.os.fstat", side_effect=_mid_scan_fstat):
                snap1 = tracker.sync_session(match=match, include_git_info=False)
            assert snap1 is not None
            st_obj = tracker.get_state(match.session_key)
            assert st_obj is not None
            # Mid-scan stat divergence MUST NOT mark verified_stat
            self.assertIsNone(st_obj.verified_stat)

            # Therefore next sync MUST verify consumed prefix instead of O(1) short-circuiting!
            with mock.patch.object(
                tracker, "_verify_consumed_prefix", wraps=tracker._verify_consumed_prefix
            ) as spy_verify:
                snap2 = tracker.sync_session(match=match, include_git_info=False)
                self.assertEqual(spy_verify.call_count, 1)
            assert snap2 is not None
            # After clean unchanged verification, verified_stat is now populated and 3rd sync short-circuits O(1)
            self.assertIsNotNone(st_obj.verified_stat)
            with mock.patch.object(
                tracker, "_verify_consumed_prefix", wraps=tracker._verify_consumed_prefix
            ) as spy_verify2:
                tracker.sync_session(match=match, include_git_info=False)
                self.assertEqual(spy_verify2.call_count, 0)

    def test_remote_etag_request_pins_state_against_lru_eviction_and_falls_back_if_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            rt = root / "rt"
            home.mkdir()
            rt.mkdir()
            host_cfg = RemoteHostConfig(name="devbox", ssh_target="devbox", timeout_ms=400)
            remote_cfg = RemoteConfig(hosts=(host_cfg,))

            class _FakeRemoteManager:
                def __init__(self) -> None:
                    self.calls: list[dict[str, object]] = []
                    self.hook: object = None

                def fetch_with_budget(
                    self,
                    hcfg: RemoteHostConfig,
                    req_dict: dict[str, object],
                    *,
                    cache_key: tuple[object, ...],
                    budget_ms: int,
                    allow_start_if_missing: bool = True,
                    allow_half_open: bool = False,
                    is_cancelled: object = None,
                    on_result: object = None,
                ) -> dict[str, object]:
                    _ = (hcfg, cache_key, budget_ms, allow_start_if_missing, allow_half_open, is_cancelled, on_result)
                    self.calls.append(dict(req_dict))
                    if callable(self.hook):
                        self.hook(req_dict)
                    if req_dict.get("etag"):
                        return {
                            "ok": True,
                            "cli": "claude",
                            "session_id": "rem-1111",
                            "not_modified": True,
                            "etag": str(req_dict["etag"]),
                            "cwd": "/srv/app",
                        }
                    return {
                        "ok": True,
                        "cli": "claude",
                        "session_id": "rem-1111",
                        "etag": "etag-v2",
                        "cwd": "/srv/app",
                        "turns": [["user", "recovered remote turn"]],
                    }

                def update_config(self, *_a: object, **_kw: object) -> None:
                    pass

                def close(self) -> None:
                    pass

            fake_mgr = _FakeRemoteManager()
            tracker = SessionTracker(
                home_dir=home,
                runtime_dir=rt,
                proc_root=root / "empty_proc",
                env={"HOME": str(home), "XDG_RUNTIME_DIR": str(rt)},
                remote_cfg=remote_cfg,
                remote_manager=fake_mgr,  # type: ignore[arg-type]
                cold_start_fallback="off",
                max_states=2,
            )

            # Seed remote session with ETag and 2 turns
            rem_state = tracker._apply_remote_probe_response(
                host_cfg,
                {
                    "ok": True,
                    "cli": "claude",
                    "session_id": "rem-1111",
                    "etag": "etag-v1",
                    "cwd": "/srv/app",
                    "turns": [["user", "remote turn 1"], ["assistant", "remote reply 1"]],
                },
            )
            assert rem_state is not None
            rem_key = rem_state.session_key

            # Prepare 3 local sessions to flood LRU (> max_states=2) WHILE ETag probe is in flight
            local_matches: list[SessionMatch] = []
            for idx in range(3):
                lp = root / f"flood_{idx}.jsonl"
                lp.write_text(
                    json.dumps(
                        {
                            "type": "user",
                            "isSidechain": False,
                            "message": {"role": "user", "content": f"flood-{idx}"},
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                local_matches.append(
                    SessionMatch(
                        cli="claude",
                        session_id=f"9999000{idx}-1111-4222-8333-444455556666",
                        jsonl_path=str(lp),
                        cwd=str(root),
                        matched_by="title_tag",
                    )
                )

            def _flood_during_etag(_req: dict[str, object]) -> None:
                for lm in local_matches:
                    tracker.sync_session(match=lm, include_git_info=False)

            fake_mgr.hook = _flood_during_etag
            rem_match = SessionMatch(
                cli="claude",
                session_id="rem-1111",
                jsonl_path="",
                cwd="/srv/app",
                matched_by="remote",
                origin="remote",
                host_name="devbox",
                remote_title="claude [claude:#rem-1111]",
            )
            snap = tracker._sync_remote_match(rem_match, include_git_info=False)
            assert snap is not None
            self.assertEqual(snap.clean_turns, (("user", "remote turn 1"), ("assistant", "remote reply 1")))
            self.assertEqual(len(fake_mgr.calls), 1)
            self.assertEqual(fake_mgr.calls[0]["etag"], "etag-v1")
            # Pin refcount must be cleanly released in finally
            self.assertEqual(tracker._active_state_refs, {})

            # Extreme fallback: if local cached state somehow disappears before not_modified applies,
            # _sync_remote_match retries unconditionally without etag and recovers turns
            fake_mgr.calls.clear()

            def _drop_state_once(req: dict[str, object]) -> None:
                if req.get("etag"):
                    with tracker._lock:
                        tracker._states.pop(rem_key, None)

            fake_mgr.hook = _drop_state_once
            snap_fb = tracker._sync_remote_match(rem_match, include_git_info=False)
            assert snap_fb is not None
            self.assertEqual(snap_fb.clean_turns, (("user", "recovered remote turn"),))
            self.assertEqual(len(fake_mgr.calls), 2)
            self.assertEqual(fake_mgr.calls[0]["etag"], "etag-v1")
            self.assertEqual(fake_mgr.calls[1]["etag"], "")
            self.assertEqual(tracker._active_state_refs, {})

            # Prefetch-with-ETag -> local state evicted BEFORE sync -> sync rebuilds etag=""
            # yet consumes cached prefetched not_modified -> MUST still trigger unconditional refetch!
            with tracker._lock:
                tracker._states.pop(rem_key, None)
            fake_mgr.calls.clear()
            served_cached_nm = [False]
            orig_fetch = fake_mgr.fetch_with_budget

            def _fetch_serving_stale_prefetch_nm(
                hcfg: RemoteHostConfig,
                req_dict: dict[str, object],
                *,
                cache_key: tuple[object, ...],
                budget_ms: int,
                allow_start_if_missing: bool = True,
                allow_half_open: bool = False,
                is_cancelled: object = None,
                on_result: object = None,
            ) -> dict[str, object]:
                if not served_cached_nm[0]:
                    served_cached_nm[0] = True
                    fake_mgr.calls.append(dict(req_dict))
                    return {
                        "ok": True,
                        "cli": "claude",
                        "session_id": "rem-1111",
                        "not_modified": True,
                        "etag": "etag-v2",
                        "cwd": "/srv/app",
                    }
                return orig_fetch(
                    hcfg,
                    req_dict,
                    cache_key=cache_key,
                    budget_ms=budget_ms,
                    allow_start_if_missing=allow_start_if_missing,
                    allow_half_open=allow_half_open,
                    is_cancelled=is_cancelled,
                    on_result=on_result,
                )

            fake_mgr.hook = None
            with mock.patch.object(fake_mgr, "fetch_with_budget", side_effect=_fetch_serving_stale_prefetch_nm):
                snap_prefetch_fb = tracker._sync_remote_match(rem_match, include_git_info=False)
            assert snap_prefetch_fb is not None
            self.assertEqual(snap_prefetch_fb.clean_turns, (("user", "recovered remote turn"),))
            self.assertEqual(len(fake_mgr.calls), 2)
            self.assertEqual(fake_mgr.calls[0]["etag"], "")
            self.assertEqual(fake_mgr.calls[1]["etag"], "")
            self.assertEqual(tracker._active_state_refs, {})


if __name__ == "__main__":
    unittest.main()
