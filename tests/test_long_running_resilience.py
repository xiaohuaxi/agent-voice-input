"""Offline tests for v0.11.0 long-running sleep/wake resilience and overhead bounds."""
from __future__ import annotations

import base64
import errno
import http.client
import http.server
import io
import json
import os
from pathlib import Path
import select
import shutil
import socket
import ssl
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
    _SplitTimeoutHTTPSConnection,
    _is_idle_socket_stale,
    _normalize_proxy_fingerprint,
    _parse_http_proxy_route,
    _redact_proxy_error_text,
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
                    "NO_PROXY": "127.0.0.0/8,.internal.corp,*.aliyuncs.com,*.port.local:8443,*.,localhost:8443",
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
            self.assertEqual(_resolve_system_proxy_key("https", "dashscope.aliyuncs.com", 443), "")
            self.assertEqual(_resolve_system_proxy_key("https", "aliyuncs.com", 443), "")
            self.assertIn("proxy.corp:8080", _resolve_system_proxy_key("https", "evilaliyuncs.com", 443))
            self.assertEqual(_resolve_system_proxy_key("https", "svc.port.local", 8443), "")
            self.assertIn("proxy.corp:8080", _resolve_system_proxy_key("https", "svc.port.local", 443))
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


def _recv_http_message(
    sock: socket.socket, stop_evt: threading.Event
) -> tuple[str, dict[str, str], bytes, list[tuple[str, str]]] | None:
    """Read one HTTP/1.x request head + Content-Length body from a persistent loopback socket."""
    buf = b""
    sock.settimeout(0.05)
    while b"\r\n\r\n" not in buf:
        if stop_evt.is_set():
            return None
        try:
            chunk = sock.recv(4096)
        except TimeoutError:
            continue
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
    head_bytes, rest = buf.split(b"\r\n\r\n", 1)
    lines = head_bytes.decode("iso-8859-1").split("\r\n")
    req_line = lines[0]
    hdrs: dict[str, str] = {}
    raw_hdr_pairs: list[tuple[str, str]] = []
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            k_low = k.strip().lower()
            v_str = v.strip()
            hdrs[k_low] = v_str
            raw_hdr_pairs.append((k_low, v_str))
    clen = int(hdrs.get("content-length") or "0")
    body = rest
    while len(body) < clen:
        if stop_evt.is_set():
            return None
        try:
            chunk = sock.recv(max(4096, clen - len(body)))
        except TimeoutError:
            continue
        except OSError:
            return None
        if not chunk:
            return None
        body += chunk
    return req_line, hdrs, body[:clen], raw_hdr_pairs


class _LoopbackHttpProxyServer:
    """Real loopback TCP/HTTP proxy supporting HTTP keep-alive forwarding and HTTPS CONNECT tunnels."""

    def __init__(
        self,
        *,
        expected_auth: str | None = None,
        hang_connect: bool = False,
        reject_connect_status: str | None = None,
        truncate_connect_headers: bool = False,
        drop_second_req_after_read: bool = False,
    ) -> None:
        self.expected_auth = expected_auth
        self.hang_connect = hang_connect
        self.reject_connect_status = reject_connect_status
        self.truncate_connect_headers = truncate_connect_headers
        self.drop_second_req_after_read = drop_second_req_after_read
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._active_socks: set[socket.socket] = set()
        self._threads: list[threading.Thread] = []
        self.tcp_accepts = 0
        self.connect_lines: list[str] = []
        self.connect_host_header_counts: list[int] = []
        self.connect_requests: list[tuple[str, dict[str, str]]] = []
        self.http_requests: list[tuple[str, str, dict[str, str], bytes]] = []
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(16)
        self._srv.settimeout(0.05)
        self.port = self._srv.getsockname()[1]
        self._accept_th = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_th.start()

    def _track(self, sock: socket.socket) -> None:
        with self._lock:
            self._active_socks.add(sock)

    def _untrack_and_close(self, sock: socket.socket | None) -> None:
        if sock is None:
            return
        with self._lock:
            self._active_socks.discard(sock)
        try:
            sock.close()
        except OSError:
            pass

    def close_all_client_sockets(self) -> None:
        with self._lock:
            socks = list(self._active_socks)
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass

    def stop(self) -> None:
        self._stop.set()
        try:
            self._srv.close()
        except OSError:
            pass
        self.close_all_client_sockets()
        self._accept_th.join(timeout=2.0)
        with self._lock:
            ths = list(self._threads)
        for th in ths:
            th.join(timeout=2.0)

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._srv.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with self._lock:
                self.tcp_accepts += 1
            self._track(client)
            th = threading.Thread(target=self._handle_client, args=(client,), daemon=True)
            with self._lock:
                self._threads.append(th)
            th.start()

    def _handle_client(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        req_count_on_conn = 0
        try:
            while not self._stop.is_set():
                msg = _recv_http_message(client, self._stop)
                if msg is None:
                    break
                req_line, hdrs, body, raw_hdr_pairs = msg
                parts = req_line.split(" ", 2)
                if len(parts) < 2:
                    break
                method, target = parts[0].upper(), parts[1]
                req_count_on_conn += 1

                if method == "CONNECT":
                    with self._lock:
                        self.connect_lines.append(req_line)
                        self.connect_host_header_counts.append(
                            sum(1 for k, _ in raw_hdr_pairs if k == "host")
                        )
                        self.connect_requests.append((target, dict(hdrs)))
                    if self.hang_connect:
                        self._stop.wait(timeout=5.0)
                        break
                    if self.truncate_connect_headers:
                        client.sendall(b"HTTP/1.1 200 Connection Established\r\nX-Partial: 1\r\n")
                        break
                    if self.reject_connect_status:
                        client.sendall(
                            f"HTTP/1.1 {self.reject_connect_status}\r\n"
                            "Proxy-Authenticate: Basic realm=\"corp\"\r\n"
                            "Connection: close\r\n\r\n".encode("iso-8859-1")
                        )
                        break
                    if self.expected_auth is not None and hdrs.get("proxy-authorization") != self.expected_auth:
                        client.sendall(
                            b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                            b"Proxy-Authenticate: Basic realm=\"corp\"\r\n"
                            b"Connection: close\r\n\r\n"
                        )
                        break
                    t_host, t_port_s = target.rsplit(":", 1)
                    connect_host = (
                        "127.0.0.1"
                        if t_host.lower() in ("localhost", "[::1]")
                        else t_host.strip("[]")
                    )
                    upstream = socket.create_connection((connect_host, int(t_port_s)), timeout=2.0)
                    self._track(upstream)
                    client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    self._relay_tunnel(client, upstream)
                    break

                with self._lock:
                    self.http_requests.append((method, target, dict(hdrs), body))

                if self.drop_second_req_after_read and req_count_on_conn == 2:
                    self.drop_second_req_after_read = False
                    break

                if self.expected_auth is not None and hdrs.get("proxy-authorization") != self.expected_auth:
                    err_body = b'{"error":"proxy_auth_required"}'
                    client.sendall(
                        b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                        b"Content-Type: application/json\r\n"
                        b"Connection: keep-alive\r\n"
                        + f"Content-Length: {len(err_body)}\r\n\r\n".encode("ascii")
                        + err_body
                    )
                    continue

                if target.endswith("/redir307"):
                    base_prefix = target[: -len("/redir307")]
                    client.sendall(
                        b"HTTP/1.1 307 Temporary Redirect\r\n"
                        + f"Location: {base_prefix}/echo\r\n".encode("iso-8859-1")
                        + b"Content-Length: 0\r\n"
                        b"Connection: keep-alive\r\n\r\n"
                    )
                    continue

                if target.endswith("/407"):
                    err_body = b'{"error":"auth"}'
                    client.sendall(
                        b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                        b"Content-Type: application/json\r\n"
                        b"Connection: keep-alive\r\n"
                        + f"Content-Length: {len(err_body)}\r\n\r\n".encode("ascii")
                        + err_body
                    )
                    continue

                payload = json.dumps(
                    {
                        "method": method,
                        "target": target,
                        "host": hdrs.get("host", ""),
                        "proxy_auth": hdrs.get("proxy-authorization", ""),
                        "body": body.decode("utf-8", errors="replace"),
                        "conn_req_no": req_count_on_conn,
                    }
                ).encode("utf-8")
                close_hdr = ""
                should_close = False
                if target.endswith("/close-proxy"):
                    # Emit duplicate Proxy-Connection headers (close THEN keep-alive) and deliberately
                    # keep the server socket OPEN so the test fails if the client drops earlier headers!
                    close_hdr = "Proxy-Connection: close\r\nProxy-Connection: keep-alive\r\n"
                    should_close = False
                elif target.endswith("/close-conn"):
                    close_hdr = "Connection: close\r\nConnection: keep-alive\r\n"
                    should_close = False
                else:
                    close_hdr = "Connection: keep-alive\r\n"
                client.sendall(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/json\r\n"
                    + close_hdr.encode("ascii")
                    + f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii")
                    + payload
                )
                if should_close:
                    break
        except OSError:
            pass
        finally:
            self._untrack_and_close(upstream)
            self._untrack_and_close(client)

    def _relay_tunnel(self, client: socket.socket, upstream: socket.socket) -> None:
        client.setblocking(False)
        upstream.setblocking(False)
        while not self._stop.is_set():
            try:
                rlist, _, _ = select.select([client, upstream], [], [], 0.05)
            except (OSError, ValueError):
                break
            if not rlist:
                continue
            for src in rlist:
                dst = upstream if src is client else client
                try:
                    data = src.recv(16384)
                except BlockingIOError:
                    continue
                except OSError:
                    return
                if not data:
                    return
                view = memoryview(data)
                sent = 0
                while sent < len(data) and not self._stop.is_set():
                    try:
                        n = dst.send(view[sent:])
                        if n <= 0:
                            return
                        sent += n
                    except BlockingIOError:
                        select.select([], [dst], [], 0.05)
                    except OSError:
                        return


class _LoopbackTlsOriginServer:
    """Real OpenSSL TLS HTTP/1.1 origin server recording ClientHello SNI and Keep-Alive requests."""

    def __init__(self, cert_path: Path, key_path: Path) -> None:
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._active_socks: set[socket.socket] = set()
        self._threads: list[threading.Thread] = []
        self.sni_names: list[str | None] = []
        self.requests: list[tuple[str, str, dict[str, str], bytes]] = []
        self.tls_handshakes = 0
        self._ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        self._ctx.sni_callback = self._on_sni
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(16)
        self._srv.settimeout(0.05)
        self.port = self._srv.getsockname()[1]
        self._accept_th = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_th.start()

    def _on_sni(self, _ssl_sock: ssl.SSLSocket, server_name: str | None, _ctx: ssl.SSLContext) -> None:
        with self._lock:
            self.sni_names.append(server_name)

    def stop(self) -> None:
        self._stop.set()
        try:
            self._srv.close()
        except OSError:
            pass
        with self._lock:
            socks = list(self._active_socks)
            ths = list(self._threads)
        for s in socks:
            try:
                s.close()
            except OSError:
                pass
        self._accept_th.join(timeout=2.0)
        for th in ths:
            th.join(timeout=2.0)

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                raw_sock, _ = self._srv.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with self._lock:
                self._active_socks.add(raw_sock)
            th = threading.Thread(target=self._handle_conn, args=(raw_sock,), daemon=True)
            with self._lock:
                self._threads.append(th)
            th.start()

    def _handle_conn(self, raw_sock: socket.socket) -> None:
        tls_sock: ssl.SSLSocket | None = None
        req_no = 0
        try:
            raw_sock.settimeout(2.0)
            tls_sock = self._ctx.wrap_socket(raw_sock, server_side=True)
            with self._lock:
                self._active_socks.discard(raw_sock)
                self._active_socks.add(tls_sock)
                self.tls_handshakes += 1
            while not self._stop.is_set():
                msg = _recv_http_message(tls_sock, self._stop)
                if msg is None:
                    break
                req_line, hdrs, body, _ = msg
                parts = req_line.split(" ", 2)
                if len(parts) < 2:
                    break
                method, path = parts[0].upper(), parts[1]
                req_no += 1
                with self._lock:
                    self.requests.append((method, path, dict(hdrs), body))
                if path == "/redir307":
                    tls_sock.sendall(
                        b"HTTP/1.1 307 Temporary Redirect\r\n"
                        b"Location: /v1/echo\r\n"
                        b"Content-Length: 0\r\n"
                        b"Connection: keep-alive\r\n\r\n"
                    )
                    continue
                payload = json.dumps(
                    {
                        "method": method,
                        "path": path,
                        "host": hdrs.get("host", ""),
                        "proxy_auth": hdrs.get("proxy-authorization", ""),
                        "body": body.decode("utf-8", errors="replace"),
                        "conn_req_no": req_no,
                    }
                ).encode("utf-8")
                tls_sock.sendall(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Connection: keep-alive\r\n"
                    + f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii")
                    + payload
                )
        except OSError:
            pass
        finally:
            sock_to_close = tls_sock if tls_sock is not None else raw_sock
            with self._lock:
                self._active_socks.discard(raw_sock)
                if tls_sock is not None:
                    self._active_socks.discard(tls_sock)
            try:
                sock_to_close.close()
            except OSError:
                pass


def _generate_localhost_cert(tmp_dir: Path) -> tuple[Path, Path]:
    """Generate a self-signed cert valid ONLY for DNS:localhost and IP:::1 (NOT IP:127.0.0.1)."""
    openssl_bin = shutil.which("openssl")
    if not openssl_bin:
        raise unittest.SkipTest("openssl binary unavailable for loopback TLS cert generation")
    cfg_path = tmp_dir / "openssl.cnf"
    cert_path = tmp_dir / "localhost.crt"
    key_path = tmp_dir / "localhost.key"
    cfg_path.write_text(
        "[req]\n"
        "prompt = no\n"
        "distinguished_name = dn\n"
        "x509_extensions = v3_req\n"
        "[dn]\n"
        "CN = localhost\n"
        "[v3_req]\n"
        "subjectAltName = DNS:localhost,IP:::1\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [
            openssl_bin,
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-nodes",
            "-keyout",
            str(key_path),
            "-out",
            str(cert_path),
            "-days",
            "1",
            "-config",
            str(cfg_path),
            "-extensions",
            "v3_req",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"openssl cert generation failed: {proc.stderr.strip()}")
    return cert_path, key_path


class HttpProxyKeepAliveTests(unittest.TestCase):
    def setUp(self) -> None:
        _KEEPALIVE_POOL.evict_all()

    def tearDown(self) -> None:
        _KEEPALIVE_POOL.evict_all()

    def _clean_proxy_env(self, extra: dict[str, str]) -> dict[str, str]:
        base = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
        base.update(extra)
        return base

    def test_https_target_via_http_connect_proxy_reuses_tunnel_and_verifies_target_sni(self) -> None:
        expected_auth = "Basic " + base64.b64encode(b"alice:s@cret 1").decode("ascii")
        with tempfile.TemporaryDirectory(prefix="avi-tls-proxy-") as td:
            cert_path, key_path = _generate_localhost_cert(Path(td))
            client_ssl_ctx = ssl.create_default_context(cafile=str(cert_path))
            self.assertTrue(client_ssl_ctx.check_hostname)
            self.assertEqual(client_ssl_ctx.verify_mode, ssl.CERT_REQUIRED)

            origin = _LoopbackTlsOriginServer(cert_path, key_path)
            proxy = _LoopbackHttpProxyServer(expected_auth=expected_auth)
            try:
                proxy_url = f"http://alice:s%40cret%201@127.0.0.1:{proxy.port}"
                env_map = self._clean_proxy_env({"HTTPS_PROXY": proxy_url})
                with (
                    mock.patch.dict(os.environ, env_map, clear=True),
                    mock.patch.object(_KEEPALIVE_POOL, "_get_ssl_context", return_value=client_ssl_ctx),
                ):
                    r1 = urllib_http_transport(
                        HttpRequest(
                            url=f"https://localhost:{origin.port}/v1/echo?n=1",
                            method="GET",
                            headers={},
                            body=b"",
                            timeout_sec=3.0,
                        )
                    )
                    r2 = urllib_http_transport(
                        HttpRequest(
                            url=f"https://localhost:{origin.port}/v1/echo?n=2",
                            method="POST",
                            headers={"Content-Type": "application/json"},
                            body=b'{"hello":"tunnel"}',
                            timeout_sec=3.0,
                        )
                    )
                    r3 = urllib_http_transport(
                        HttpRequest(
                            url=f"https://localhost:{origin.port}/redir307",
                            method="POST",
                            headers={"Content-Type": "text/plain"},
                            body=b"keep-me",
                            timeout_sec=3.0,
                        )
                    )

                # Also verify schemeless proxy shorthand normalizes to the same pool key & reuses tunnel!
                env_schemeless = self._clean_proxy_env(
                    {"HTTPS_PROXY": f"alice:s%40cret%201@127.0.0.1:{proxy.port}"}
                )
                with (
                    mock.patch.dict(os.environ, env_schemeless, clear=True),
                    mock.patch.object(_KEEPALIVE_POOL, "_get_ssl_context", return_value=client_ssl_ctx),
                ):
                    r4 = urllib_http_transport(
                        HttpRequest(
                            url=f"https://localhost:{origin.port}/v1/echo?n=4",
                            method="GET",
                            headers={},
                            body=b"",
                            timeout_sec=3.0,
                        )
                    )

                self.assertEqual(r1.status, 200)
                self.assertEqual(r2.status, 200)
                self.assertEqual(r3.status, 200)
                self.assertEqual(r4.status, 200)
                self.assertEqual(r1.json()["path"], "/v1/echo?n=1")
                self.assertEqual(r1.json()["host"], f"localhost:{origin.port}")
                self.assertEqual(r1.json()["proxy_auth"], "")
                self.assertEqual(r2.json()["body"], '{"hello":"tunnel"}')
                self.assertEqual(r3.json()["method"], "POST")
                self.assertEqual(r3.json()["path"], "/v1/echo")
                self.assertEqual(r3.json()["body"], "keep-me")
                self.assertEqual(r4.json()["conn_req_no"], 5)

                # Exactly ONE TCP connection, ONE CONNECT tunnel, and ONE TLS handshake for all 5 HTTPS hops!
                self.assertEqual(proxy.tcp_accepts, 1)
                self.assertEqual(len(proxy.connect_requests), 1)
                self.assertEqual(proxy.connect_requests[0][0], f"localhost:{origin.port}")
                self.assertEqual(proxy.connect_requests[0][1].get("proxy-authorization"), expected_auth)
                self.assertEqual(origin.tls_handshakes, 1)
                # Real TLS SNI + cert verification used target_host ("localhost"), NOT proxy_host ("127.0.0.1")!
                self.assertEqual(origin.sni_names, ["localhost"])
            finally:
                _KEEPALIVE_POOL.evict_all()
                proxy.stop()
                origin.stop()

    def test_http_target_via_http_proxy_reuses_connection_with_absolute_uri_and_auth(self) -> None:
        expected_auth = "Basic " + base64.b64encode(b"bob:p:ss").decode("ascii")
        proxy = _LoopbackHttpProxyServer(expected_auth=expected_auth)
        direct_paths: list[tuple[str, str]] = []

        class _DirectOriginHandler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                direct_paths.append((self.path, self.headers.get("Proxy-Authorization", "")))
                body = b'{"direct":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                pass

        direct_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _DirectOriginHandler)
        direct_port = direct_srv.server_address[1]
        direct_th = threading.Thread(target=direct_srv.serve_forever, daemon=True)
        direct_th.start()
        try:
            proxy_url = f"http://bob:p%3Ass@127.0.0.1:{proxy.port}"
            env_map = self._clean_proxy_env({"HTTP_PROXY": proxy_url})
            with mock.patch.dict(os.environ, env_map, clear=True):
                r1 = urllib_http_transport(
                    HttpRequest(
                        url=f"http://127.0.0.1:{direct_port}/v1/ping?k=1",
                        method="GET",
                        headers={},
                        body=b"",
                        timeout_sec=3.0,
                    )
                )
                r2 = urllib_http_transport(
                    HttpRequest(
                        url=f"http://127.0.0.1:{direct_port}/v1/ping?k=2",
                        method="POST",
                        headers={"Content-Type": "text/plain"},
                        body=b"payload",
                        timeout_sec=3.0,
                    )
                )
                r3 = urllib_http_transport(
                    HttpRequest(
                        url=f"http://127.0.0.1:{direct_port}/redir307",
                        method="POST",
                        headers={"Content-Type": "text/plain"},
                        body=b"redir-body",
                        timeout_sec=3.0,
                    )
                )

                self.assertEqual(r1.status, 200)
                self.assertEqual(r2.status, 200)
                self.assertEqual(r3.status, 200)
                self.assertEqual(r1.json()["target"], f"http://127.0.0.1:{direct_port}/v1/ping?k=1")
                self.assertEqual(r1.json()["host"], f"127.0.0.1:{direct_port}")
                self.assertEqual(r1.json()["proxy_auth"], expected_auth)
                self.assertEqual(r2.json()["conn_req_no"], 2)
                self.assertEqual(r3.json()["target"], f"http://127.0.0.1:{direct_port}/echo")
                self.assertEqual(r3.json()["body"], "redir-body")
                self.assertEqual(r3.json()["conn_req_no"], 4)
                self.assertEqual(proxy.tcp_accepts, 1)

                # Duplicate Proxy-Connection: close + keep-alive (with server socket kept OPEN)
                # must still prevent returning the socket to the pool!
                r_close = urllib_http_transport(
                    HttpRequest(
                        url=f"http://127.0.0.1:{direct_port}/close-proxy",
                        method="GET",
                        headers={},
                        body=b"",
                        timeout_sec=3.0,
                    )
                )
                self.assertEqual(r_close.status, 200)
                r_next = urllib_http_transport(
                    HttpRequest(
                        url=f"http://127.0.0.1:{direct_port}/v1/after-close",
                        method="GET",
                        headers={},
                        body=b"",
                        timeout_sec=3.0,
                    )
                )
                self.assertEqual(r_next.status, 200)
                self.assertEqual(r_next.json()["conn_req_no"], 1)
                self.assertEqual(proxy.tcp_accepts, 2)

                # Duplicate Connection: close + keep-alive (with server socket kept OPEN) also discards socket
                r_close_conn = urllib_http_transport(
                    HttpRequest(
                        url=f"http://127.0.0.1:{direct_port}/close-conn",
                        method="GET",
                        headers={},
                        body=b"",
                        timeout_sec=3.0,
                    )
                )
                self.assertEqual(r_close_conn.status, 200)
                r_after_conn_close = urllib_http_transport(
                    HttpRequest(
                        url=f"http://127.0.0.1:{direct_port}/v1/after-conn-close",
                        method="GET",
                        headers={},
                        body=b"",
                        timeout_sec=3.0,
                    )
                )
                self.assertEqual(r_after_conn_close.status, 200)
                self.assertEqual(r_after_conn_close.json()["conn_req_no"], 1)
                self.assertEqual(proxy.tcp_accepts, 3)

                # Plain HTTP 407 is returned cleanly and NEVER pooled even if proxy sent keep-alive
                r_407 = urllib_http_transport(
                    HttpRequest(
                        url=f"http://127.0.0.1:{direct_port}/407",
                        method="GET",
                        headers={},
                        body=b"",
                        timeout_sec=3.0,
                    )
                )
                self.assertEqual(r_407.status, 407)
                r_post_407 = urllib_http_transport(
                    HttpRequest(
                        url=f"http://127.0.0.1:{direct_port}/v1/after-407",
                        method="GET",
                        headers={},
                        body=b"",
                        timeout_sec=3.0,
                    )
                )
                self.assertEqual(r_post_407.status, 200)
                self.assertEqual(r_post_407.json()["conn_req_no"], 1)
                self.assertEqual(proxy.tcp_accepts, 4)

            # NO_PROXY bypass goes direct to origin with relative path & ZERO Proxy-Authorization leakage
            env_bypass = self._clean_proxy_env({"HTTP_PROXY": proxy_url, "NO_PROXY": "127.0.0.1"})
            with mock.patch.dict(os.environ, env_bypass, clear=True):
                r_dir = urllib_http_transport(
                    HttpRequest(
                        url=f"http://127.0.0.1:{direct_port}/direct-check",
                        method="GET",
                        headers={},
                        body=b"",
                        timeout_sec=3.0,
                    )
                )
            self.assertEqual(r_dir.status, 200)
            self.assertEqual(r_dir.json(), {"direct": True})
            self.assertEqual(direct_paths, [("/direct-check", "")])
            self.assertEqual(proxy.tcp_accepts, 4)
        finally:
            _KEEPALIVE_POOL.evict_all()
            proxy.stop()
            direct_srv.shutdown()
            direct_srv.server_close()
            direct_th.join(timeout=2.0)

    def test_http_proxy_pool_isolation_by_target_proxy_and_credentials(self) -> None:
        proxy_a = _LoopbackHttpProxyServer()
        proxy_b = _LoopbackHttpProxyServer()
        try:
            # 1. Same proxy, different target ports do NOT share pool bucket
            env_a = self._clean_proxy_env({"HTTP_PROXY": f"http://127.0.0.1:{proxy_a.port}"})
            with mock.patch.dict(os.environ, env_a, clear=True):
                r_t1 = urllib_http_transport(HttpRequest("http://127.0.0.1:18001/a", "GET", {}, b"", 3.0))
                r_t2 = urllib_http_transport(HttpRequest("http://127.0.0.1:18002/b", "GET", {}, b"", 3.0))
                self.assertEqual(r_t1.json()["conn_req_no"], 1)
                self.assertEqual(r_t2.json()["conn_req_no"], 1)
                self.assertEqual(proxy_a.tcp_accepts, 2)
                # Repeating both targets reuses their respective pooled sockets (still 2 accepts)
                r_t1b = urllib_http_transport(HttpRequest("http://127.0.0.1:18001/a2", "GET", {}, b"", 3.0))
                r_t2b = urllib_http_transport(HttpRequest("http://127.0.0.1:18002/b2", "GET", {}, b"", 3.0))
                self.assertEqual(r_t1b.json()["conn_req_no"], 2)
                self.assertEqual(r_t2b.json()["conn_req_no"], 2)
                self.assertEqual(proxy_a.tcp_accepts, 2)

            # 2. Different proxy server gets its own bucket
            env_b = self._clean_proxy_env({"HTTP_PROXY": f"http://127.0.0.1:{proxy_b.port}"})
            with mock.patch.dict(os.environ, env_b, clear=True):
                r_b1 = urllib_http_transport(HttpRequest("http://127.0.0.1:18001/b1", "GET", {}, b"", 3.0))
                r_b2 = urllib_http_transport(HttpRequest("http://127.0.0.1:18001/b2", "GET", {}, b"", 3.0))
                self.assertEqual(r_b1.json()["conn_req_no"], 1)
                self.assertEqual(r_b2.json()["conn_req_no"], 2)
                self.assertEqual(proxy_b.tcp_accepts, 1)

            # 3. Same proxy + same target, different credentials isolate buckets & never leak auth headers
            env_u1 = self._clean_proxy_env({"HTTP_PROXY": f"http://u1:secretA@127.0.0.1:{proxy_a.port}"})
            env_u2 = self._clean_proxy_env({"HTTP_PROXY": f"http://u2:secretB@127.0.0.1:{proxy_a.port}"})
            auth_u1 = "Basic " + base64.b64encode(b"u1:secretA").decode("ascii")
            auth_u2 = "Basic " + base64.b64encode(b"u2:secretB").decode("ascii")
            with mock.patch.dict(os.environ, env_u1, clear=True):
                ru1 = urllib_http_transport(HttpRequest("http://127.0.0.1:18001/u1", "GET", {}, b"", 3.0))
            with mock.patch.dict(os.environ, env_u2, clear=True):
                ru2 = urllib_http_transport(HttpRequest("http://127.0.0.1:18001/u2", "GET", {}, b"", 3.0))
            with mock.patch.dict(os.environ, env_a, clear=True):
                r_noauth = urllib_http_transport(HttpRequest("http://127.0.0.1:18001/noauth", "GET", {}, b"", 3.0))

            self.assertEqual(ru1.json()["proxy_auth"], auth_u1)
            self.assertEqual(ru1.json()["conn_req_no"], 1)
            self.assertEqual(ru2.json()["proxy_auth"], auth_u2)
            self.assertEqual(ru2.json()["conn_req_no"], 1)
            self.assertEqual(r_noauth.json()["proxy_auth"], "")
            self.assertEqual(r_noauth.json()["conn_req_no"], 3)
            self.assertEqual(proxy_a.tcp_accepts, 4)
        finally:
            _KEEPALIVE_POOL.evict_all()
            proxy_a.stop()
            proxy_b.stop()

    def test_http_proxy_connect_407_timeout_cancel_idle_close_eviction_and_redaction(self) -> None:
        one_char_pw = "4"
        auth_b64 = base64.b64encode(f"u:{one_char_pw}".encode("utf-8")).decode("ascii")

        # 1. Proxy returns 407 on HTTPS CONNECT echoing 1-char password '4' + Basic token + custom text:
        #    CONNECT failure exposes only numeric status code '407' intact (NOT '[REDACTED]07'!),
        #    never leaks free-text reason phrase or Basic token, and closes socket without pooling!
        proxy_407 = _LoopbackHttpProxyServer(
            reject_connect_status=f"407 Proxy Auth Failed {one_char_pw} {auth_b64} LeakedReasonDetail"
        )
        try:
            env_407 = self._clean_proxy_env(
                {"HTTPS_PROXY": f"http://u:{one_char_pw}@127.0.0.1:{proxy_407.port}"}
            )
            with mock.patch.dict(os.environ, env_407, clear=True):
                with self.assertRaises(HttpTransportError) as cm_407:
                    urllib_http_transport(
                        HttpRequest("https://api.example.invalid/v1/test", "POST", {}, b"{}", 3.0)
                    )
            err_407_msg = str(cm_407.exception)
            self.assertEqual(cm_407.exception.retry_kind, "never")
            self.assertIn("status 407", err_407_msg)
            self.assertNotIn("[REDACTED]07", err_407_msg)
            self.assertNotIn(auth_b64, err_407_msg)
            self.assertNotIn("LeakedReasonDetail", err_407_msg)
            self.assertEqual(evict_all_keepalive_connections(), 0)
        finally:
            proxy_407.stop()

        # 1b. Short password redaction preserves normal digits ('status 407') while scrubbing URL userinfo & Basic token
        route_short = _parse_http_proxy_route(f"http://u:{one_char_pw}@127.0.0.1:8080")
        redacted_short = _redact_proxy_error_text(
            f"status 407 via http://u:{one_char_pw}@127.0.0.1:8080 auth Basic {auth_b64} ({auth_b64})",
            f"http://u:{one_char_pw}@127.0.0.1:8080",
            route_short,
        )
        self.assertIn("status 407", redacted_short)
        self.assertNotIn("[REDACTED]07", redacted_short)
        self.assertNotIn(f"u:{one_char_pw}@", redacted_short)
        self.assertNotIn(auth_b64, redacted_short)
        self.assertIn("u:[REDACTED]@", redacted_short)

        # 1c. Truncated CONNECT 200 response (EOF before end-of-headers blank line) raises error & never pools
        proxy_trunc = _LoopbackHttpProxyServer(truncate_connect_headers=True)
        try:
            env_trunc = self._clean_proxy_env({"HTTPS_PROXY": f"http://127.0.0.1:{proxy_trunc.port}"})
            with mock.patch.dict(os.environ, env_trunc, clear=True):
                with self.assertRaises(HttpTransportError) as cm_trunc:
                    urllib_http_transport(
                        HttpRequest("https://api.example.invalid/v1/test", "GET", {}, b"", 3.0)
                    )
            self.assertEqual(cm_trunc.exception.retry_kind, "transient")
            self.assertIn("truncated", str(cm_trunc.exception).lower())
            self.assertEqual(evict_all_keepalive_connections(), 0)
        finally:
            proxy_trunc.stop()

        # 2. Proxy hangs during CONNECT -> bounded by connect_timeout_sec AND interruptible by CancelToken
        secret_pw = "ProxyTopSecret_407!"
        proxy_hang = _LoopbackHttpProxyServer(hang_connect=True)
        try:
            env_hang = self._clean_proxy_env(
                {"HTTPS_PROXY": f"http://corpuser:{secret_pw}@127.0.0.1:{proxy_hang.port}"}
            )
            with mock.patch.dict(os.environ, env_hang, clear=True):
                t0 = time.monotonic()
                with self.assertRaises(HttpTransportError) as cm_to:
                    urllib_http_transport(
                        HttpRequest(
                            "https://api.example.invalid/v1/test",
                            "POST",
                            {},
                            b"{}",
                            timeout_sec=4.0,
                            connect_timeout_sec=0.12,
                        )
                    )
                self.assertLess(time.monotonic() - t0, 0.7)
                self.assertEqual(cm_to.exception.retry_kind, "transient")
                self.assertNotIn(secret_pw, str(cm_to.exception))
                self.assertEqual(evict_all_keepalive_connections(), 0)

                tok = CancelToken()
                timer = threading.Timer(0.05, tok.cancel, args=("user_cancelled",))
                timer.start()
                t1 = time.monotonic()
                try:
                    with self.assertRaises(HttpTransportError) as cm_can:
                        urllib_http_transport(
                            HttpRequest(
                                "https://api.example.invalid/v1/test",
                                "POST",
                                {},
                                b"{}",
                                timeout_sec=4.0,
                                connect_timeout_sec=3.0,
                                cancel_token=tok,
                            )
                        )
                finally:
                    timer.cancel()
                self.assertLess(time.monotonic() - t1, 0.6)
                self.assertEqual(cm_can.exception.retry_kind, "never")
                self.assertIn("cancelled", str(cm_can.exception).lower())
                self.assertEqual(evict_all_keepalive_connections(), 0)
        finally:
            proxy_hang.stop()

        # 3. Idle server close, mid-send stale close retry, and evict_all_keepalive_connections()
        proxy_live = _LoopbackHttpProxyServer(drop_second_req_after_read=True)
        try:
            env_live = self._clean_proxy_env({"HTTP_PROXY": f"http://127.0.0.1:{proxy_live.port}"})
            with mock.patch.dict(os.environ, env_live, clear=True):
                r1 = urllib_http_transport(HttpRequest("http://127.0.0.1:18080/one", "POST", {}, b"p1", 3.0))
                self.assertEqual(r1.status, 200)
                self.assertEqual(proxy_live.tcp_accepts, 1)

                # 3a. Race-window stale close (server closes after reading 2nd req on reused socket):
                #     keepalive transparently retries on a fresh socket!
                r2 = urllib_http_transport(HttpRequest("http://127.0.0.1:18080/two", "POST", {}, b"p2", 3.0))
                self.assertEqual(r2.status, 200)
                self.assertEqual(r2.json()["body"], "p2")
                self.assertEqual(proxy_live.tcp_accepts, 2)

                # 3b. Idle close while sitting in pool: 0ms select probe drops dead socket and reconnects
                proxy_live.close_all_client_sockets()
                time.sleep(0.03)
                r3 = urllib_http_transport(HttpRequest("http://127.0.0.1:18080/three", "POST", {}, b"p3", 3.0))
                self.assertEqual(r3.status, 200)
                self.assertEqual(proxy_live.tcp_accepts, 3)

                # 3c. Power-resume evict_all_keepalive_connections() evicts the pooled proxy socket
                evicted = evict_all_keepalive_connections()
                self.assertGreaterEqual(evicted, 1)
                r4 = urllib_http_transport(HttpRequest("http://127.0.0.1:18080/four", "GET", {}, b"", 3.0))
                self.assertEqual(r4.status, 200)
                self.assertEqual(r4.json()["conn_req_no"], 1)
                self.assertEqual(proxy_live.tcp_accepts, 4)
        finally:
            _KEEPALIVE_POOL.evict_all()
            proxy_live.stop()

    def test_caller_proxy_authorization_stripped_from_origin_and_direct_requests(self) -> None:
        caller_auth = "Basic " + base64.b64encode(b"caller:hdr-secret").decode("ascii")
        url_auth = "Basic " + base64.b64encode(b"urluser:url-secret").decode("ascii")

        direct_seen: list[tuple[str, str]] = []

        class _DirectProbeHandler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                direct_seen.append((self.path, self.headers.get("Proxy-Authorization", "")))
                body = b'{"direct":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                pass

        direct_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _DirectProbeHandler)
        direct_port = direct_srv.server_address[1]
        direct_th = threading.Thread(target=direct_srv.serve_forever, daemon=True)
        direct_th.start()

        with tempfile.TemporaryDirectory(prefix="avi-pauth-strip-") as td:
            cert_path, key_path = _generate_localhost_cert(Path(td))
            client_ssl_ctx = ssl.create_default_context(cafile=str(cert_path))
            origin = _LoopbackTlsOriginServer(cert_path, key_path)
            proxy = _LoopbackHttpProxyServer()
            try:
                # 1. HTTPS CONNECT via unauthenticated proxy URL + mixed-case caller Proxy-Authorization:
                #    proxy receives caller_auth in CONNECT; origin NEVER sees it (neither initial nor 307 redirect)!
                orig_hdrs = {"pRoXy-AuThOrIzAtIoN": caller_auth, "X-Test": "1"}
                env_https = self._clean_proxy_env({"HTTPS_PROXY": f"http://127.0.0.1:{proxy.port}"})
                with (
                    mock.patch.dict(os.environ, env_https, clear=True),
                    mock.patch.object(_KEEPALIVE_POOL, "_get_ssl_context", return_value=client_ssl_ctx),
                ):
                    r_https = urllib_http_transport(
                        HttpRequest(f"https://localhost:{origin.port}/v1/echo", "GET", orig_hdrs, b"", 3.0)
                    )
                    r_redir = urllib_http_transport(
                        HttpRequest(f"https://localhost:{origin.port}/redir307", "POST", orig_hdrs, b"ok", 3.0)
                    )
                self.assertEqual(r_https.status, 200)
                self.assertEqual(r_redir.status, 200)
                self.assertEqual(orig_hdrs["pRoXy-AuThOrIzAtIoN"], caller_auth)  # caller dict not mutated
                self.assertEqual(proxy.connect_requests[0][1].get("proxy-authorization"), caller_auth)
                self.assertEqual(r_https.json()["proxy_auth"], "")
                self.assertEqual(r_redir.json()["proxy_auth"], "")

                # 2. HTTPS CONNECT via authenticated proxy URL + caller Proxy-Authorization:
                #    proxy URL creds win, caller header still stripped from origin!
                env_https_urlauth = self._clean_proxy_env(
                    {"HTTPS_PROXY": f"http://urluser:url-secret@127.0.0.1:{proxy.port}"}
                )
                with (
                    mock.patch.dict(os.environ, env_https_urlauth, clear=True),
                    mock.patch.object(_KEEPALIVE_POOL, "_get_ssl_context", return_value=client_ssl_ctx),
                ):
                    r_urlauth = urllib_http_transport(
                        HttpRequest(
                            f"https://localhost:{origin.port}/v1/urlauth",
                            "GET",
                            {"Proxy-Authorization": caller_auth},
                            b"",
                            3.0,
                        )
                    )
                self.assertEqual(r_urlauth.status, 200)
                self.assertEqual(proxy.connect_requests[-1][1].get("proxy-authorization"), url_auth)
                self.assertEqual(r_urlauth.json()["proxy_auth"], "")

                # 3. Plain HTTP proxy with unauthenticated proxy URL forwards caller Proxy-Authorization to proxy
                env_http = self._clean_proxy_env({"HTTP_PROXY": f"http://127.0.0.1:{proxy.port}"})
                with mock.patch.dict(os.environ, env_http, clear=True):
                    r_http = urllib_http_transport(
                        HttpRequest(
                            f"http://127.0.0.1:{direct_port}/via-http-proxy",
                            "GET",
                            {"proxy-authorization": caller_auth},
                            b"",
                            3.0,
                        )
                    )
                self.assertEqual(r_http.status, 200)
                self.assertEqual(r_http.json()["proxy_auth"], caller_auth)

                # 4. NO_PROXY direct request strips caller Proxy-Authorization (NEVER leaked to origin!)
                env_direct = self._clean_proxy_env(
                    {"HTTP_PROXY": f"http://127.0.0.1:{proxy.port}", "NO_PROXY": "127.0.0.1"}
                )
                with mock.patch.dict(os.environ, env_direct, clear=True):
                    r_dir = urllib_http_transport(
                        HttpRequest(
                            f"http://127.0.0.1:{direct_port}/direct-no-leak",
                            "GET",
                            {"Proxy-Authorization": caller_auth},
                            b"",
                            3.0,
                        )
                    )
                self.assertEqual(r_dir.status, 200)
                self.assertEqual(direct_seen, [("/direct-no-leak", "")])

                # 5. CRLF / non-latin-1 caller Proxy-Authorization is rejected BEFORE connecting
                #    for BOTH plain HTTP proxy and HTTPS CONNECT proxy without echoing the value!
                accepts_before_bad_auth = proxy.tcp_accepts
                for bad_val in ("Basic c2VjcmV0\r\nX-Injected: evil", "Basic 非法编码"):
                    for env_case, url_case in (
                        (env_http, f"http://127.0.0.1:{direct_port}/bad-auth-http"),
                        (env_https, f"https://localhost:{origin.port}/bad-auth-https"),
                    ):
                        with mock.patch.dict(os.environ, env_case, clear=True):
                            with self.assertRaises(HttpTransportError) as cm_bad_auth:
                                urllib_http_transport(
                                    HttpRequest(url_case, "GET", {"Proxy-Authorization": bad_val}, b"", 3.0)
                                )
                        self.assertEqual(cm_bad_auth.exception.retry_kind, "never")
                        self.assertEqual(str(cm_bad_auth.exception), "Invalid Proxy-Authorization header")
                        self.assertNotIn("c2VjcmV0", str(cm_bad_auth.exception))
                        self.assertNotIn("X-Injected", str(cm_bad_auth.exception))
                self.assertEqual(proxy.tcp_accepts, accepts_before_bad_auth)
            finally:
                _KEEPALIVE_POOL.evict_all()
                proxy.stop()
                origin.stop()
                direct_srv.shutdown()
                direct_srv.server_close()
                direct_th.join(timeout=2.0)

    def test_ipv6_origin_connect_brackets_and_unbracketed_tls_server_hostname(self) -> None:
        with tempfile.TemporaryDirectory(prefix="avi-ipv6-proxy-") as td:
            cert_path, key_path = _generate_localhost_cert(Path(td))
            real_ctx = ssl.create_default_context(cafile=str(cert_path))
            observed_server_hostnames: list[str | None] = []

            class _SpySslCtx:
                verify_mode = real_ctx.verify_mode
                check_hostname = real_ctx.check_hostname

                def wrap_socket(
                    self,
                    sock: socket.socket,
                    *,
                    server_hostname: str | None = None,
                    do_handshake_on_connect: bool = True,
                ) -> ssl.SSLSocket:
                    observed_server_hostnames.append(server_hostname)
                    return real_ctx.wrap_socket(
                        sock,
                        server_hostname=server_hostname,
                        do_handshake_on_connect=do_handshake_on_connect,
                    )

            origin = _LoopbackTlsOriginServer(cert_path, key_path)
            proxy = _LoopbackHttpProxyServer()
            try:
                env_map = self._clean_proxy_env({"HTTPS_PROXY": f"http://127.0.0.1:{proxy.port}"})
                with (
                    mock.patch.dict(os.environ, env_map, clear=True),
                    mock.patch.object(_KEEPALIVE_POOL, "_get_ssl_context", return_value=_SpySslCtx()),
                ):
                    r1 = urllib_http_transport(
                        HttpRequest(f"https://[::1]:{origin.port}/v1/ipv6", "GET", {}, b"", 3.0)
                    )
                    r2 = urllib_http_transport(
                        HttpRequest(f"https://[::1]:{origin.port}/v1/ipv6-2", "GET", {}, b"", 3.0)
                    )
                self.assertEqual(r1.status, 200)
                self.assertEqual(r2.status, 200)
                # RFC-compliant HTTP/1.1 + single brackets + exactly one Host header across Python 3.10-3.14
                self.assertEqual(proxy.connect_lines, [f"CONNECT [::1]:{origin.port} HTTP/1.1"])
                self.assertEqual(proxy.connect_host_header_counts, [1])
                self.assertEqual(proxy.connect_requests[0][1].get("host"), f"[::1]:{origin.port}")
                # Unbracketed IPv6 passed to wrap_socket(..., server_hostname=...) and verified against IP:::1 SAN
                self.assertEqual(observed_server_hostnames, ["::1"])
                self.assertEqual(r1.json()["host"], f"[::1]:{origin.port}")
                self.assertEqual(r2.json()["conn_req_no"], 2)
                self.assertEqual(proxy.tcp_accepts, 1)

                # Simulate CPython 3.12+ set_tunnel() pre-populating an unbracketed Host header in _tunnel_headers:
                # _tunnel() must still emit exactly one canonical bracketed Host header matching CONNECT authority.
                c_py312 = _SplitTimeoutHTTPSConnection(
                    "127.0.0.1",
                    proxy.port,
                    timeout=3.0,
                    context=_SpySslCtx(),
                    connect_timeout=3.0,
                )
                try:
                    c_py312.set_tunnel(
                        "::1",
                        origin.port,
                        headers={"Host": f"::1:{origin.port}", "Proxy-Authorization": "Basic dTpw"},
                    )
                    c_py312.connect()
                finally:
                    c_py312.close()
                self.assertEqual(proxy.connect_lines[-1], f"CONNECT [::1]:{origin.port} HTTP/1.1")
                self.assertEqual(proxy.connect_host_header_counts[-1], 1)
                self.assertEqual(proxy.connect_requests[-1][1].get("host"), f"[::1]:{origin.port}")
                self.assertEqual(proxy.connect_requests[-1][1].get("proxy-authorization"), "Basic dTpw")
                self.assertEqual(observed_server_hostnames[-1], "::1")
            finally:
                _KEEPALIVE_POOL.evict_all()
                proxy.stop()
                origin.stop()

    def test_http_target_via_https_tls_proxy_reuses_encrypted_connection_and_https_target_still_fails_closed(
        self,
    ) -> None:
        expected_auth = "Basic " + base64.b64encode(b"alice:tls-secret").decode("ascii")
        with tempfile.TemporaryDirectory(prefix="avi-https-proxy-") as td:
            cert_path, key_path = _generate_localhost_cert(Path(td))
            client_ssl_ctx = ssl.create_default_context(cafile=str(cert_path))
            tls_proxy = _LoopbackTlsOriginServer(cert_path, key_path)
            try:
                # 1. Valid single-layer TLS proxy: http:// target via https:// proxy
                proxy_url = f"https://alice:tls-secret@localhost:{tls_proxy.port}"
                env_http = self._clean_proxy_env({"HTTP_PROXY": proxy_url})
                with (
                    mock.patch.dict(os.environ, env_http, clear=True),
                    mock.patch.object(_KEEPALIVE_POOL, "_get_ssl_context", return_value=client_ssl_ctx),
                ):
                    r1 = urllib_http_transport(
                        HttpRequest("http://127.0.0.1:18080/v1/tls-proxy?n=1", "GET", {}, b"", 3.0)
                    )
                    r2 = urllib_http_transport(
                        HttpRequest(
                            "http://127.0.0.1:18080/v1/tls-proxy?n=2",
                            "POST",
                            {"Content-Type": "text/plain"},
                            b"hello-tls-proxy",
                            3.0,
                        )
                    )
                self.assertEqual(r1.status, 200)
                self.assertEqual(r2.status, 200)
                self.assertEqual(r1.json()["path"], "http://127.0.0.1:18080/v1/tls-proxy?n=1")
                self.assertEqual(r1.json()["host"], "127.0.0.1:18080")
                self.assertEqual(r1.json()["proxy_auth"], expected_auth)
                self.assertEqual(r2.json()["body"], "hello-tls-proxy")
                self.assertEqual(r2.json()["conn_req_no"], 2)
                self.assertEqual(tls_proxy.tls_handshakes, 1)
                self.assertEqual(tls_proxy.sni_names, ["localhost"])

                # 2. https:// target via https:// proxy STILL fails closed before any TCP/TLS handshake
                env_https = self._clean_proxy_env({"HTTPS_PROXY": proxy_url})
                with (
                    mock.patch.dict(os.environ, env_https, clear=True),
                    mock.patch.object(_KEEPALIVE_POOL, "_get_ssl_context", return_value=client_ssl_ctx),
                ):
                    with self.assertRaises(HttpTransportError) as cm_fc:
                        urllib_http_transport(
                            HttpRequest("https://api.example.invalid/v1/fail", "GET", {}, b"", 3.0)
                        )
                self.assertEqual(cm_fc.exception.retry_kind, "never")
                self.assertIn("unsupported proxy scheme 'https'", str(cm_fc.exception).lower())
                self.assertNotIn("tls-secret", str(cm_fc.exception))
                self.assertEqual(tls_proxy.tls_handshakes, 1)
            finally:
                _KEEPALIVE_POOL.evict_all()
                tls_proxy.stop()

    def test_unsupported_and_malformed_proxies_fail_closed_without_connecting(self) -> None:
        # 1. Route parser & fingerprint unit coverage
        r_default_port = _parse_http_proxy_route("http://proxy.corp")
        r_schemeless = _parse_http_proxy_route("proxy.corp")
        self.assertIsNotNone(r_default_port)
        self.assertEqual(r_default_port, r_schemeless)
        assert r_default_port is not None
        self.assertEqual((r_default_port.host, r_default_port.port), ("proxy.corp", 80))
        self.assertEqual(r_default_port.proxy_key, "http://proxy.corp:80")

        r_v6 = _parse_http_proxy_route("http://[::1]:8080/")
        assert r_v6 is not None
        self.assertEqual((r_v6.host, r_v6.port, r_v6.proxy_key), ("::1", 8080, "http://[::1]:8080"))

        r_auth = _parse_http_proxy_route("http://u%401:p%3Aw@127.0.0.1:8080")
        assert r_auth is not None
        self.assertEqual(
            r_auth.proxy_auth_header,
            "Basic " + base64.b64encode(b"u@1:p:w").decode("ascii"),
        )
        self.assertNotIn("p:w", r_auth.proxy_key)
        self.assertNotIn("p%3Aw", r_auth.proxy_key)
        self.assertNotIn(r_auth.proxy_auth_header, repr(r_auth))

        for non_poolable in (
            "",
            "https://127.0.0.1:8443",
            "socks5://127.0.0.1:1080",
            "socks5h://127.0.0.1:1080",
            "http://",
            "http://:8080",
            "http://127.0.0.1:notaport",
            "http://127.0.0.1:0",
            "http://127.0.0.1:70000",
            "http://[::1:8080",
            "http://bad host:8080",
        ):
            self.assertIsNone(_parse_http_proxy_route(non_poolable), f"expected None for {non_poolable!r}")
        self.assertTrue(_normalize_proxy_fingerprint("http://[::1:8080"))

        # 2. Trap listeners proving ZERO TCP connections are made to either proxy OR origin on fail-closed
        proxy_trap = _LoopbackHttpProxyServer()
        origin_trap = _LoopbackHttpProxyServer()
        try:
            fail_closed_cases = (
                (
                    {"HTTPS_PROXY": f"https://user:TopSecretPass99@127.0.0.1:{proxy_trap.port}"},
                    f"https://127.0.0.1:{origin_trap.port}/v1/x",
                    "unsupported proxy scheme",
                ),
                (
                    {"ALL_PROXY": f"socks5://user:TopSecretPass99@127.0.0.1:{proxy_trap.port}"},
                    f"https://127.0.0.1:{origin_trap.port}/v1/x",
                    "unsupported proxy scheme",
                ),
                (
                    {"HTTPS_PROXY": "http://"},
                    f"https://127.0.0.1:{origin_trap.port}/v1/x",
                    "invalid proxy url",
                ),
                (
                    {"HTTPS_PROXY": "http://user:TopSecretPass99@[::1:8080"},
                    f"https://127.0.0.1:{origin_trap.port}/v1/x",
                    "invalid proxy url",
                ),
                (
                    {"HTTP_PROXY": "http://user:TopSecretPass99@127.0.0.1:notaport"},
                    f"http://127.0.0.1:{origin_trap.port}/v1/x",
                    "invalid proxy url",
                ),
            )
            for proxy_env, target_url, expected_frag in fail_closed_cases:
                env_bad = self._clean_proxy_env(proxy_env)
                with mock.patch.dict(os.environ, env_bad, clear=True):
                    with self.assertRaises(HttpTransportError) as cm_bad:
                        urllib_http_transport(
                            HttpRequest(
                                target_url,
                                "GET",
                                {},
                                b"",
                                timeout_sec=1.0,
                                connect_timeout_sec=0.2,
                            )
                        )
                msg = str(cm_bad.exception)
                self.assertEqual(cm_bad.exception.retry_kind, "never", f"for {proxy_env!r}")
                self.assertIn(expected_frag, msg.lower(), f"for {proxy_env!r}: {msg}")
                if "unsupported proxy scheme" in expected_frag:
                    self.assertIn("HTTP_PROXY/HTTPS_PROXY=http://...", msg)
                    self.assertIn("NO_PROXY", msg)
                self.assertNotIn("TopSecretPass99", msg)
                self.assertNotIn("[::1:8080", msg)
                self.assertNotIn("socks5://", msg)
                self.assertNotIn(f"https://user:", msg)

            self.assertEqual(proxy_trap.tcp_accepts, 0)
            self.assertEqual(origin_trap.tcp_accepts, 0)

            # 3. Dual-protocol env (HTTP_PROXY=http://... + ALL_PROXY=socks5://...) selects scheme-specific
            #    http:// proxy and keeps Keep-Alive working without false rejection!
            env_dual = self._clean_proxy_env(
                {
                    "HTTP_PROXY": f"http://127.0.0.1:{proxy_trap.port}",
                    "ALL_PROXY": "socks5://127.0.0.1:1080",
                }
            )
            with mock.patch.dict(os.environ, env_dual, clear=True):
                r_dual = urllib_http_transport(
                    HttpRequest(f"http://127.0.0.1:{origin_trap.port}/dual", "GET", {}, b"", 2.0)
                )
            self.assertEqual(r_dual.status, 200)
            self.assertEqual(proxy_trap.tcp_accepts, 1)
            self.assertEqual(origin_trap.tcp_accepts, 0)
        finally:
            _KEEPALIVE_POOL.evict_all()
            proxy_trap.stop()
            origin_trap.stop()


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
