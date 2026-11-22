"""Pure-stdlib HTTP/1.1 Keep-Alive transport, split-phase timeouts, cancellation, and rate pacing."""
from __future__ import annotations

import atexit
import base64
import contextlib
from dataclasses import dataclass, field
import datetime
import email.utils
import errno
import hashlib
import http.client
import io
import ipaddress
import json
import math
import os
import random
import re
import select
import selectors
import socket
import ssl
import string
import sys
import threading
import time
from typing import Any, Callable, Iterator, Mapping, Sequence
import urllib.error
import urllib.parse
import urllib.request

from .config import is_loopback_url as _is_loopback_url, redact_secrets as _redact_secrets

DEFAULT_USER_AGENT = "agent-voice-input/0.1"
DEFAULT_CONNECT_TIMEOUT_SEC: float = 9.0
_STREAM_CHUNK_BYTES = 65536


class HttpTransportError(RuntimeError):
    """Structured HTTP transport failure carrying pre-stringification retry classification."""

    def __init__(self, message: str, *, retry_kind: str = "never") -> None:
        super().__init__(message)
        self.retry_kind = retry_kind



class CancelToken:
    """Thread-safe three-state cancellation & commit token (ACTIVE -> CANCELLED | COMMITTED)."""

    ACTIVE: int = 0
    CANCELLED: int = 1
    COMMITTED: int = 2

    __slots__ = ("_lock", "_state", "_reason", "_next_id", "_hooks", "_events")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = 0  # 0: ACTIVE, 1: CANCELLED, 2: COMMITTED
        self._reason = ""
        self._next_id = 0
        self._hooks: dict[int, Callable[[], None]] = {}
        self._events: set[threading.Event] = set()

    @property
    def state(self) -> int:
        with self._lock:
            return self._state

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    def is_cancelled(self) -> bool:
        with self._lock:
            return self._state == 1

    def is_committed(self) -> bool:
        with self._lock:
            return self._state == 2

    def try_commit(self) -> bool:
        """Atomically transition ACTIVE -> COMMITTED. Returns False if already CANCELLED."""
        with self._lock:
            if self._state == 1:
                return False
            if self._state == 0:
                self._state = 2
                self._hooks.clear()
                self._events.clear()
            return True

    def _mark_cancelled_locked(
        self, reason: str
    ) -> tuple[bool, bool, list[threading.Event], list[Callable[[], None]]]:
        with self._lock:
            if self._state == 2:
                return False, True, [], []
            if self._state != 0:
                return False, False, [], []
            self._state = 1
            self._reason = reason or "cancelled"
            events = list(self._events)
            hooks = list(self._hooks.values())
            self._events.clear()
            self._hooks.clear()
            return True, False, events, hooks

    @staticmethod
    def cancel_many(
        tokens: Sequence[CancelToken],
        reason: str = "cancelled",
    ) -> tuple[list[CancelToken], list[CancelToken]]:
        """Transition ALL active tokens to CANCELLED BEFORE firing any event or abort hook."""
        cancelled: list[CancelToken] = []
        committed: list[CancelToken] = []
        events: list[threading.Event] = []
        hooks: list[Callable[[], None]] = []
        eff_reason = reason or "cancelled"
        for tok in tokens:
            did_cancel, was_committed, evs, hks = tok._mark_cancelled_locked(eff_reason)
            if did_cancel:
                cancelled.append(tok)
                events.extend(evs)
                hooks.extend(hks)
            elif was_committed:
                committed.append(tok)
        for ev in events:
            try:
                ev.set()
            except Exception:
                pass
        for fn in hooks:
            try:
                fn()
            except Exception:
                pass
        return cancelled, committed

    def cancel(self, reason: str = "cancelled") -> bool:
        """Atomically transition ACTIVE -> CANCELLED and fire bound events/hooks outside lock."""
        cancelled, _ = CancelToken.cancel_many((self,), reason)
        return bool(cancelled)

    def register_abort(self, fn: Callable[[], None]) -> int:
        fire_now = False
        hid = 0
        with self._lock:
            if self._state == 1:
                fire_now = True
            elif self._state == 0:
                self._next_id += 1
                hid = self._next_id
                self._hooks[hid] = fn
        if fire_now:
            try:
                fn()
            except Exception:
                pass
        return hid

    def unregister_abort(self, hook_id: int) -> None:
        if not hook_id:
            return
        with self._lock:
            self._hooks.pop(hook_id, None)

    def bind_event(self, ev: threading.Event) -> None:
        set_now = False
        with self._lock:
            if self._state == 1:
                set_now = True
            elif self._state == 0:
                self._events.add(ev)
        if set_now:
            ev.set()

    def unbind_event(self, ev: threading.Event) -> None:
        with self._lock:
            self._events.discard(ev)


@dataclass(frozen=True)
class HttpRequest:
    url: str
    method: str
    headers: Mapping[str, str]
    body: bytes
    timeout_sec: float
    deadline: float | None = None
    connect_timeout_sec: float = DEFAULT_CONNECT_TIMEOUT_SEC
    cancel_token: CancelToken | None = None


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> dict[str, Any]:
        text = self.body.decode("utf-8", errors="replace").strip()
        if not text:
            return {}
        data = json.loads(text)
        if isinstance(data, dict):
            return data
        return {"_data": data}

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


HttpTransport = Callable[[HttpRequest], HttpResponse]


def _origin_tuple(parsed: urllib.parse.SplitResult) -> tuple[str, str, int | None, str | None, str | None]:
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        port = -1
    if port is None:
        if scheme == "https":
            port = 443
        elif scheme == "http":
            port = 80
    return (scheme, host, port, parsed.username, parsed.password)


def _close_quietly(obj: Any) -> None:
    if obj is None:
        return
    close_fn = getattr(obj, "close", None)
    if callable(close_fn):
        try:
            close_fn()
        except Exception:
            pass


def _update_stream_socket_timeout(stream: Any, timeout_sec: float) -> None:
    """Best-effort tighten underlying HTTPResponse / HTTPError socket timeout before each read."""
    for attr_chain in (
        ("fp", "fp", "raw", "_sock"),
        ("fp", "fp", "_sock"),
        ("fp", "raw", "_sock"),
        ("fp", "_sock"),
        ("raw", "_sock"),
        ("_sock",),
    ):
        cur = stream
        for attr in attr_chain:
            cur = getattr(cur, attr, None)
            if cur is None:
                break
        settimeout = getattr(cur, "settimeout", None)
        if callable(settimeout):
            try:
                settimeout(max(0.005, timeout_sec))
            except Exception:
                pass
            return


class _DeadlineRawStreamProxy:
    """Enforce wall-clock deadline across every buffered fp read/read1/readline call."""

    def __init__(self, fp: Any, deadline: float) -> None:
        self._fp = fp
        self._deadline = deadline

    def _guard(self) -> None:
        rem = self._deadline - time.monotonic()
        if rem <= 0:
            raise TimeoutError("HTTP response body read exceeded total request deadline")
        _update_stream_socket_timeout(self._fp, rem)

    def _check_post(self) -> None:
        if time.monotonic() > self._deadline:
            raise TimeoutError("HTTP response body read exceeded total request deadline")

    def read(self, amt: int | None = None) -> bytes:
        self._guard()
        data = self._fp.read() if amt is None else self._fp.read(amt)
        self._check_post()
        return data

    def read1(self, n: int = -1) -> bytes:
        self._guard()
        fn = getattr(self._fp, "read1", None)
        data = fn(n) if callable(fn) else self._fp.read(n)
        self._check_post()
        return data

    def readline(self, limit: int = -1) -> bytes:
        self._guard()
        data = self._fp.readline(limit)
        self._check_post()
        return data

    def readinto(self, b: Any) -> int:
        self._guard()
        n = self._fp.readinto(b)
        self._check_post()
        return n

    def __getattr__(self, name: str) -> Any:
        return getattr(self._fp, name)


def _install_deadline_proxy(stream: Any, deadline: float) -> None:
    host = stream.fp if isinstance(stream, urllib.error.HTTPError) and getattr(stream, "fp", None) is not None else stream
    inner_fp = getattr(host, "fp", None)
    if inner_fp is None:
        return
    if isinstance(inner_fp, _DeadlineRawStreamProxy):
        inner_fp._deadline = deadline
        return
    try:
        host.fp = _DeadlineRawStreamProxy(inner_fp, deadline)
    except Exception:
        pass


def _read_stream_with_deadline(
    stream: Any, deadline: float | None, *, max_bytes: int | None = None
) -> bytes:
    if deadline is None:
        return stream.read() if max_bytes is None else stream.read(max_bytes)
    _install_deadline_proxy(stream, deadline)
    read_step = getattr(stream, "read1", None)
    chunks: list[bytes] = []
    total = 0
    while True:
        if max_bytes is not None and total >= max_bytes:
            break
        rem = deadline - time.monotonic()
        if rem <= 0:
            raise TimeoutError("HTTP response body read exceeded total request deadline")
        _update_stream_socket_timeout(stream, rem)
        step_size = (
            _STREAM_CHUNK_BYTES
            if max_bytes is None
            else min(_STREAM_CHUNK_BYTES, max_bytes - total)
        )
        chunk = read_step(step_size) if callable(read_step) else stream.read(step_size)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if time.monotonic() > deadline:
            raise TimeoutError("HTTP response body read exceeded total request deadline")
    return b"".join(chunks)


class _SameOriginSafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Blocks cross-origin or HTTPS->HTTP downgrade redirects and enforces request deadline."""

    def http_error_302(
        self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any
    ) -> Any:
        if "location" in headers:
            newurl = headers["location"]
        elif "uri" in headers:
            newurl = headers["uri"]
        else:
            return None

        deadline: float | None = getattr(req, "_voice_deadline", None)
        if deadline is not None and (deadline - time.monotonic()) <= 0:
            _close_quietly(fp)
            raise TimeoutError("HTTP redirect exceeded total request deadline")

        urlparts = urllib.parse.urlparse(newurl)
        if urlparts.scheme.lower() not in ("http", "https", ""):
            _close_quietly(fp)
            raise urllib.error.URLError(
                f"{msg} - Redirection to url '{newurl}' is not allowed"
            )
        if not urlparts.path and urlparts.netloc:
            parts_list = list(urlparts)
            parts_list[2] = "/"
            newurl = urllib.parse.urlunparse(parts_list)
        else:
            newurl = urllib.parse.urlunparse(urlparts)

        newurl = urllib.parse.quote(newurl, encoding="iso-8859-1", safe=string.punctuation)
        newurl = urllib.parse.urljoin(req.full_url, newurl)

        new = self.redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None

        if hasattr(req, "redirect_dict"):
            visited = new.redirect_dict = req.redirect_dict  # type: ignore[attr-defined]
            if (
                visited.get(newurl, 0) >= self.max_repeats
                or len(visited) >= self.max_redirections
            ):
                _close_quietly(fp)
                raise urllib.error.URLError(self.inf_msg + msg)
        else:
            visited = new.redirect_dict = req.redirect_dict = {}  # type: ignore[attr-defined]
        visited[newurl] = visited.get(newurl, 0) + 1

        try:
            _read_stream_with_deadline(fp, deadline, max_bytes=_STREAM_CHUNK_BYTES)
        finally:
            _close_quietly(fp)

        if deadline is not None:
            next_timeout: float | None = deadline - time.monotonic()
            if next_timeout <= 0:
                raise TimeoutError("HTTP redirect exceeded total request deadline")
            req_cap = getattr(req, "_voice_req_timeout", None)
            if isinstance(req_cap, (int, float)) and req_cap > 0:
                next_timeout = min(float(req_cap), next_timeout)
        else:
            next_timeout = getattr(req, "timeout", None)
        new.timeout = next_timeout  # type: ignore[attr-defined]
        return self.parent.open(new, timeout=next_timeout)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        old_parsed = urllib.parse.urlsplit(req.full_url)
        resolved_url = urllib.parse.urljoin(req.full_url, newurl)
        target = urllib.parse.urlsplit(resolved_url)

        old_origin = _origin_tuple(old_parsed)
        new_origin = _origin_tuple(target)
        if old_origin != new_origin or old_origin[2] == -1:
            _close_quietly(fp)
            raise urllib.error.URLError(
                f"refusing cross-origin redirect from {old_parsed.scheme}://{old_parsed.netloc} "
                f"to {target.scheme}://{target.netloc}"
            )
        if new_origin[0] != "https" and not _is_loopback_url(resolved_url):
            _close_quietly(fp)
            raise urllib.error.URLError(
                f"refusing non-HTTPS redirect to {target.scheme}://{target.netloc}"
            )
        if code in (307, 308):
            merged_hdrs: dict[str, str] = {}
            for k, v in req.unredirected_hdrs.items():
                if k.lower() != "host":
                    merged_hdrs[k] = v
            for k, v in req.headers.items():
                if k.lower() != "host":
                    merged_hdrs[k] = v
            new_req: urllib.request.Request | None = urllib.request.Request(
                resolved_url.replace(" ", "%20"),
                data=req.data,
                headers=merged_hdrs,
                origin_req_host=req.origin_req_host,
                unverifiable=True,
                method=req.get_method(),
            )
        else:
            new_req = super().redirect_request(req, fp, code, msg, headers, resolved_url)

        if new_req is not None:
            if hasattr(req, "_voice_deadline"):
                setattr(new_req, "_voice_deadline", getattr(req, "_voice_deadline"))
            if hasattr(req, "_voice_req_timeout"):
                setattr(new_req, "_voice_req_timeout", getattr(req, "_voice_req_timeout"))
            if hasattr(req, "_voice_connect_timeout"):
                setattr(new_req, "_voice_connect_timeout", getattr(req, "_voice_connect_timeout"))
            if hasattr(req, "_voice_cancel_token"):
                setattr(new_req, "_voice_cancel_token", getattr(req, "_voice_cancel_token"))
        return new_req


_NONBLOCK_CONNECT_ERRNOS = frozenset(
    e
    for e in (
        getattr(errno, "EINPROGRESS", None),
        getattr(errno, "EALREADY", None),
        getattr(errno, "EWOULDBLOCK", None),
        getattr(errno, "EAGAIN", None),
        getattr(errno, "EINTR", None),
        getattr(errno, "WSAEINPROGRESS", None),
        getattr(errno, "WSAEWOULDBLOCK", None),
    )
    if e is not None
)
_ABORT_ERRNO = getattr(errno, "ECANCELED", errno.ECONNABORTED)
_DNS_MAX_INFLIGHT = 8


def _is_conn_aborted(conn_owner: Any) -> bool:
    return bool(conn_owner is not None and getattr(conn_owner, "_aborted", False))


class _BoundedDnsResolver:
    """Bounded-concurrency, cancelable and deadline-enforced DNS resolver (lazy daemon workers)."""

    def __init__(self, *, max_inflight: int = _DNS_MAX_INFLIGHT) -> None:
        self.max_inflight = max(1, int(max_inflight))
        self._sem = threading.BoundedSemaphore(self.max_inflight)

    def resolve(
        self,
        host: str,
        port: int,
        *,
        deadline: float | None = None,
        conn_owner: Any = None,
        resolver_fn: Callable[..., list[Any]] | None = None,
    ) -> list[Any]:
        if _is_conn_aborted(conn_owner):
            raise OSError(_ABORT_ERRNO, "HTTP DNS lookup aborted")
        if deadline is not None and (deadline - time.monotonic()) <= 0:
            raise TimeoutError(f"HTTP DNS lookup timed out for {host}:{port}")

        acquired = self._sem.acquire(blocking=False)
        while not acquired:
            if _is_conn_aborted(conn_owner):
                raise OSError(_ABORT_ERRNO, "HTTP DNS lookup aborted")
            if deadline is not None:
                rem_slot = deadline - time.monotonic()
                if rem_slot <= 0:
                    raise TimeoutError(f"HTTP DNS lookup timed out for {host}:{port}")
                wait_slot = min(0.02, rem_slot)
            else:
                wait_slot = 0.02
            acquired = self._sem.acquire(timeout=max(0.001, wait_slot))

        done_evt = threading.Event()
        completed = [False]
        result_box: list[list[Any] | None] = [None]
        err_box: list[BaseException | None] = [None]
        fn = resolver_fn if resolver_fn is not None else socket.getaddrinfo

        def _worker() -> None:
            try:
                result_box[0] = fn(host, port, 0, socket.SOCK_STREAM)
            except BaseException as exc:
                err_box[0] = exc
            finally:
                completed[0] = True
                done_evt.set()
                self._sem.release()

        reg_wait = getattr(conn_owner, "_register_wait_event", None)
        unreg_wait = getattr(conn_owner, "_unregister_wait_event", None)
        if callable(reg_wait):
            reg_wait(done_evt)
        try:
            try:
                th = threading.Thread(target=_worker, name="avi-dns-resolve", daemon=True)
                th.start()
            except BaseException:
                self._sem.release()
                raise

            has_wake_hook = callable(reg_wait)
            while True:
                if _is_conn_aborted(conn_owner):
                    raise OSError(_ABORT_ERRNO, "HTTP DNS lookup aborted")
                if completed[0]:
                    break
                if deadline is not None:
                    rem = deadline - time.monotonic()
                    if rem <= 0:
                        raise TimeoutError(f"HTTP DNS lookup timed out for {host}:{port}")
                    wait_s = rem if has_wake_hook else min(0.02, rem)
                else:
                    wait_s = 0.05 if has_wake_hook else 0.02
                done_evt.wait(max(0.001, wait_s))
                if _is_conn_aborted(conn_owner):
                    raise OSError(_ABORT_ERRNO, "HTTP DNS lookup aborted")
                if completed[0]:
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError(f"HTTP DNS lookup timed out for {host}:{port}")
        finally:
            if callable(unreg_wait):
                unreg_wait(done_evt)

        if err_box[0] is not None:
            raise err_box[0]
        return result_box[0] or []


_DNS_RESOLVER = _BoundedDnsResolver()


def _create_connection_with_deadline(
    address: tuple[str, int],
    conn_deadline: float | None,
    source_address: tuple[str, int] | None = None,
    fallback_timeout: float | None = None,
    conn_owner: Any = None,
) -> socket.socket:
    host, port = address
    if _is_conn_aborted(conn_owner):
        raise OSError(_ABORT_ERRNO, "HTTP TCP connect aborted")
    last_err: OSError | None = None
    if conn_deadline is not None:
        dns_deadline: float | None = conn_deadline
    elif fallback_timeout is not None and fallback_timeout > 0:
        dns_deadline = time.monotonic() + float(fallback_timeout)
    else:
        dns_deadline = None
    infos = _DNS_RESOLVER.resolve(
        host,
        port,
        deadline=dns_deadline,
        conn_owner=conn_owner,
        resolver_fn=socket.getaddrinfo,
    )
    if not infos:
        raise OSError("getaddrinfo returns an empty list")
    abort_lock: threading.Lock | None = getattr(conn_owner, "_abort_lock", None) if conn_owner is not None else None
    for af, socktype, proto, _, sa in infos:
        if _is_conn_aborted(conn_owner):
            raise OSError(_ABORT_ERRNO, "HTTP TCP connect aborted")
        now0 = time.monotonic()
        if conn_deadline is not None:
            rem = conn_deadline - now0
            if rem <= 0:
                raise TimeoutError(f"HTTP TCP connect timed out for {host}:{port}")
            step_timeout: float | None = max(0.005, rem)
            eff_deadline: float | None = conn_deadline
        elif fallback_timeout is not None and fallback_timeout > 0:
            step_timeout = float(fallback_timeout)
            eff_deadline = now0 + step_timeout
        else:
            step_timeout = fallback_timeout
            eff_deadline = None
        sock: socket.socket | None = None
        try:
            sock = socket.socket(af, socktype, proto)
            if source_address:
                sock.bind(source_address)
            if abort_lock is not None:
                with abort_lock:
                    if _is_conn_aborted(conn_owner):
                        raise OSError(_ABORT_ERRNO, "HTTP TCP connect aborted")
                    setattr(conn_owner, "_cur_sock", sock)
            sock.setblocking(False)
            err = sock.connect_ex(sa)
            if err != 0:
                if err not in _NONBLOCK_CONNECT_ERRNOS:
                    raise OSError(err, os.strerror(err))
                with selectors.DefaultSelector() as sel:
                    sel.register(sock, selectors.EVENT_WRITE)
                    while True:
                        if _is_conn_aborted(conn_owner):
                            raise OSError(_ABORT_ERRNO, "HTTP TCP connect aborted")
                        if eff_deadline is not None:
                            rem_w = eff_deadline - time.monotonic()
                            if rem_w <= 0:
                                raise TimeoutError(f"HTTP TCP connect timed out for {host}:{port}")
                            wait_s = min(0.05, rem_w)
                        else:
                            wait_s = 0.05
                        ready = sel.select(max(0.001, wait_s))
                        if _is_conn_aborted(conn_owner):
                            raise OSError(_ABORT_ERRNO, "HTTP TCP connect aborted")
                        if ready:
                            so_err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                            if so_err != 0:
                                raise OSError(so_err, os.strerror(so_err))
                            break
            if _is_conn_aborted(conn_owner):
                raise OSError(_ABORT_ERRNO, "HTTP TCP connect aborted")
            if step_timeout is not None:
                sock.settimeout(step_timeout)
            else:
                sock.setblocking(True)
            return sock
        except OSError as exc:
            last_err = exc
            if abort_lock is not None and sock is not None:
                with abort_lock:
                    if getattr(conn_owner, "_cur_sock", None) is sock:
                        setattr(conn_owner, "_cur_sock", None)
            if sock is not None:
                _close_quietly(sock)
            if _is_conn_aborted(conn_owner):
                raise
            if conn_deadline is not None and time.monotonic() >= conn_deadline:
                raise TimeoutError(f"HTTP TCP connect timed out for {host}:{port}") from None
    assert last_err is not None
    raise last_err


class _DeadlineSocketIO(socket.SocketIO):
    """SocketIO tightening per-recv socket timeout and releasing abort ownership before fd close."""

    def __init__(
        self,
        sock: socket.socket,
        mode: str,
        deadline_getter: Callable[[], float | None],
        on_close: Callable[[socket.socket], None] | None = None,
    ) -> None:
        super().__init__(sock, mode)  # type: ignore[arg-type]
        self._deadline_getter = deadline_getter
        self._on_close = on_close

    def readinto(self, b: Any) -> int | None:
        dl = self._deadline_getter()
        if dl is not None:
            rem = dl - time.monotonic()
            if rem <= 0:
                self._timeout_occurred = True  # type: ignore[attr-defined]
                raise TimeoutError("HTTP socket read exceeded deadline")
            cur_to = self._sock.gettimeout()  # type: ignore[attr-defined]
            if cur_to is None or rem < cur_to:
                self._sock.settimeout(max(0.001, rem))  # type: ignore[attr-defined]
        return super().readinto(b)

    def close(self) -> None:
        cb, self._on_close = self._on_close, None
        if cb is not None:
            cb(self._sock)  # type: ignore[arg-type]
        super().close()


def _make_deadline_response_class(
    deadline_getter: Callable[[], float | None],
    on_fp_closing: Callable[[Any], None] | None = None,
) -> type[http.client.HTTPResponse]:
    class _BoundDeadlineHTTPResponse(http.client.HTTPResponse):
        def __init__(
            self,
            sock: Any,
            debuglevel: int = 0,
            method: str | None = None,
            url: str | None = None,
        ) -> None:
            self._raw_resp_sock = sock
            super().__init__(sock, debuglevel=debuglevel, method=method, url=url)
            if isinstance(sock, socket.socket):
                old_fp = self.fp
                sock._io_refs += 1  # type: ignore[attr-defined]
                try:
                    self.fp = io.BufferedReader(
                        _DeadlineSocketIO(sock, "r", deadline_getter, on_close=on_fp_closing)
                    )
                except BaseException:
                    sock._io_refs -= 1  # type: ignore[attr-defined]
                    raise
                finally:
                    _close_quietly(old_fp)

        def close(self) -> None:
            if on_fp_closing is not None and not isinstance(self._raw_resp_sock, socket.socket):
                on_fp_closing(self._raw_resp_sock)
            super().close()

    return _BoundDeadlineHTTPResponse


def _handshake_tls_with_deadline(
    tls_sock: Any, conn_deadline: float | None, conn_owner: Any = None
) -> None:
    """Complete TLS handshake strictly within ``conn_deadline`` even under slow-drip flights."""
    if _is_conn_aborted(conn_owner):
        raise OSError(_ABORT_ERRNO, "TLS handshake aborted")
    if conn_deadline is None and conn_owner is None:
        tls_sock.do_handshake()
        return
    if not isinstance(tls_sock, ssl.SSLSocket):
        if conn_deadline is not None:
            rem = conn_deadline - time.monotonic()
            if rem <= 0:
                raise TimeoutError("TLS handshake timed out")
            tls_sock.settimeout(max(0.005, rem))
        tls_sock.do_handshake()
        if _is_conn_aborted(conn_owner):
            raise OSError(_ABORT_ERRNO, "TLS handshake aborted")
        if conn_deadline is not None and time.monotonic() > conn_deadline:
            raise TimeoutError("TLS handshake exceeded connect timeout")
        return

    tls_sock.setblocking(False)
    with selectors.DefaultSelector() as sel:
        while True:
            if _is_conn_aborted(conn_owner):
                raise OSError(_ABORT_ERRNO, "TLS handshake aborted")
            if conn_deadline is not None:
                rem = conn_deadline - time.monotonic()
                if rem <= 0:
                    raise TimeoutError("TLS handshake timed out")
                wait_s = min(0.05, rem) if conn_owner is not None else rem
            else:
                wait_s = 0.05
            try:
                tls_sock.do_handshake()
                if _is_conn_aborted(conn_owner):
                    raise OSError(_ABORT_ERRNO, "TLS handshake aborted")
                return
            except ssl.SSLWantReadError:
                sel.register(tls_sock, selectors.EVENT_READ)
                try:
                    ready = sel.select(max(0.001, wait_s))
                finally:
                    sel.unregister(tls_sock)
                if not ready and conn_deadline is not None and time.monotonic() >= conn_deadline:
                    raise TimeoutError("TLS handshake timed out") from None
            except ssl.SSLWantWriteError:
                sel.register(tls_sock, selectors.EVENT_WRITE)
                try:
                    ready = sel.select(max(0.001, wait_s))
                finally:
                    sel.unregister(tls_sock)
                if not ready and conn_deadline is not None and time.monotonic() >= conn_deadline:
                    raise TimeoutError("TLS handshake timed out") from None


def _connect_tcp_and_tunnel(
    conn: http.client.HTTPConnection, conn_deadline: float | None
) -> None:
    sys.audit("http.client.connect", conn, conn.host, conn.port)
    fb_t = float(conn.timeout) if isinstance(conn.timeout, (int, float)) else None
    conn.sock = _create_connection_with_deadline(
        (conn.host, conn.port),
        conn_deadline,
        conn.source_address,
        fallback_timeout=fb_t,
        conn_owner=conn,
    )
    try:
        try:
            conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as exc:
            if exc.errno != errno.ENOPROTOOPT:
                raise
        if getattr(conn, "_tunnel_host", None):
            if conn_deadline is not None:
                rem = conn_deadline - time.monotonic()
                if rem <= 0:
                    raise TimeoutError("HTTP proxy CONNECT timed out")
                conn.sock.settimeout(max(0.005, rem))
            if hasattr(conn, "_phase_deadline"):
                setattr(conn, "_phase_deadline", conn_deadline)
            try:
                conn._tunnel()  # type: ignore[attr-defined]
            finally:
                if hasattr(conn, "_phase_deadline"):
                    setattr(conn, "_phase_deadline", None)
            if _is_conn_aborted(conn):
                raise OSError(_ABORT_ERRNO, "HTTP proxy CONNECT aborted")
            if conn_deadline is not None and time.monotonic() > conn_deadline:
                raise TimeoutError("HTTP proxy CONNECT exceeded connect timeout")
    except BaseException:
        abort_lock = getattr(conn, "_abort_lock", None)
        if abort_lock is not None:
            with abort_lock:
                setattr(conn, "_cur_sock", None)
        _close_quietly(conn.sock)
        conn.sock = None
        raise


_LEGAL_TUNNEL_HEADER_NAME_RE = re.compile(rb"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


class _SplitTimeoutConnMixin:
    """Shared connect/I/O deadline & socket.shutdown abort lifecycle for HTTP and HTTPS connections."""

    sock: Any
    timeout: Any

    def _tunnel(self) -> None:
        tunnel_host = (getattr(self, "_tunnel_host", None) or "").strip()
        tunnel_port = getattr(self, "_tunnel_port", None)
        if not tunnel_host or tunnel_port is None:
            return
        bare_host = (
            tunnel_host[1:-1]
            if (tunnel_host.startswith("[") and tunnel_host.endswith("]"))
            else tunnel_host
        )
        if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in bare_host):
            raise ValueError(f"Tunnel host contains invalid characters: {bare_host!r}")
        wire_host = f"[{bare_host}]" if ":" in bare_host else bare_host
        try:
            wire_host_bytes = wire_host.encode("ascii")
        except UnicodeEncodeError:
            wire_host_bytes = wire_host.encode("idna")
        authority_bytes = b"%s:%d" % (wire_host_bytes, int(tunnel_port))
        tunnel_headers = getattr(self, "_tunnel_headers", {}) or {}
        connect_lines = [
            b"CONNECT %s HTTP/1.1\r\n" % (authority_bytes,),
            b"Host: %s\r\n" % (authority_bytes,),
        ]
        for header, value in tunnel_headers.items():
            if str(header).lower() == "host":
                continue
            header_bytes = str(header).encode("latin-1")
            value_bytes = str(value).encode("latin-1")
            if not _LEGAL_TUNNEL_HEADER_NAME_RE.match(header_bytes) or any(
                bad in value_bytes for bad in (b"\r", b"\n", b"\x00")
            ):
                raise ValueError(f"Invalid tunnel header: {header!r}")
            connect_lines.append(b"%s: %s\r\n" % (header_bytes, value_bytes))
        connect_lines.append(b"\r\n")
        self.send(b"".join(connect_lines))  # type: ignore[attr-defined]

        max_line = int(getattr(http.client, "_MAXLINE", 65536))
        response = self.response_class(self.sock, method=getattr(self, "_method", None))  # type: ignore[attr-defined]
        try:
            _, code, _ = response._read_status()
            if code != http.HTTPStatus.OK:
                self.close()
                raise OSError(f"HTTP proxy CONNECT failed with status {int(code)}")
            while True:
                line = response.fp.readline(max_line + 1)
                if len(line) > max_line:
                    raise http.client.LineTooLong("header line")
                if not line:
                    self.close()
                    raise http.client.RemoteDisconnected(
                        "HTTP proxy CONNECT response truncated before end of headers"
                    )
                if line in (b"\r\n", b"\n"):
                    break
        finally:
            _close_quietly(response)

    def _init_split_timeout(
        self,
        connect_timeout: float | None,
        io_deadline: float | None,
        cancel_token: CancelToken | None,
    ) -> None:
        self._connect_timeout = connect_timeout
        self._io_deadline = io_deadline
        self._phase_deadline: float | None = None
        self._abort_lock = threading.Lock()
        self._aborted = False
        self._borrow_gen = 0
        self._cur_sock: socket.socket | None = None
        self._wait_events: set[threading.Event] = set()
        self._in_getresponse = False
        self._handed_off_to_resp = False
        self._bound_token = cancel_token
        self._bound_hook_id = (
            cancel_token.register_abort(self.make_abort_fn())
            if cancel_token is not None
            else 0
        )
        self.response_class = _make_deadline_response_class(  # type: ignore[misc]
            self._current_read_deadline, self._on_response_fp_closing
        )

    def make_abort_fn(self) -> Callable[[], None]:
        with self._abort_lock:
            expected_gen = self._borrow_gen

        def _abort_for_gen() -> None:
            self.abort_inflight(expected_gen=expected_gen)

        return _abort_for_gen

    def _release_bound_hook(self) -> None:
        tok, hid = self._bound_token, self._bound_hook_id
        self._bound_token, self._bound_hook_id = None, 0
        if tok is not None and hid:
            tok.unregister_abort(hid)

    def _on_response_fp_closing(self, resp_sock: Any) -> None:
        release_hook = False
        with self._abort_lock:
            if self._handed_off_to_resp or self.sock is None:
                if self._cur_sock is resp_sock:
                    self._cur_sock = None
                self._handed_off_to_resp = False
                release_hook = True
        if release_hook:
            self._release_bound_hook()

    def _register_wait_event(self, ev: threading.Event) -> None:
        wake_now = False
        with self._abort_lock:
            if self._aborted:
                wake_now = True
            else:
                self._wait_events.add(ev)
        if wake_now:
            ev.set()

    def _unregister_wait_event(self, ev: threading.Event) -> None:
        with self._abort_lock:
            self._wait_events.discard(ev)

    def abort_inflight(self, *, expected_gen: int | None = None) -> None:
        wait_evts: list[threading.Event] = []
        with self._abort_lock:
            if expected_gen is not None and expected_gen != self._borrow_gen:
                return
            self._aborted = True
            if self._wait_events:
                wait_evts = list(self._wait_events)
                self._wait_events.clear()
            sock = self._cur_sock
            if sock is not None:
                try:
                    # REDLINE: Always call base socket.socket.shutdown, NEVER SSLSocket.shutdown
                    # (which sets self._sslobj = None unsafely across threads)!
                    socket.socket.shutdown(sock, socket.SHUT_RDWR)
                except OSError:
                    pass
        for ev in wait_evts:
            try:
                ev.set()
            except Exception:
                pass

    def close(self) -> None:
        if self._in_getresponse:
            # CPython HTTPConnection.getresponse() calls self.close() on Connection: close while
            # handing the live socket to HTTPResponse; preserve _cur_sock & hook until fp closes.
            with self._abort_lock:
                self._handed_off_to_resp = True
            super().close()  # type: ignore[misc]
            return
        self._release_bound_hook()
        with self._abort_lock:
            self._handed_off_to_resp = False
            self._cur_sock = None
        super().close()  # type: ignore[misc]

    def _current_read_deadline(self) -> float | None:
        if self._phase_deadline is not None and self._io_deadline is not None:
            return min(self._phase_deadline, self._io_deadline)
        return self._phase_deadline if self._phase_deadline is not None else self._io_deadline

    def _compute_connect_deadline(self) -> float | None:
        now = time.monotonic()
        if self._io_deadline is not None and now >= self._io_deadline:
            raise TimeoutError("HTTP request exceeded deadline before connect")
        conn_dl = (
            now + max(0.005, float(self._connect_timeout))
            if isinstance(self._connect_timeout, (int, float)) and self._connect_timeout > 0
            else None
        )
        if self._io_deadline is not None:
            conn_dl = self._io_deadline if conn_dl is None else min(conn_dl, self._io_deadline)
        return conn_dl

    def _apply_post_connect_timeout(self) -> None:
        io_t = float(self.timeout) if isinstance(self.timeout, (int, float)) else None
        if self._io_deadline is not None:
            rem_io = self._io_deadline - time.monotonic()
            if rem_io <= 0:
                raise TimeoutError("HTTP request exceeded total deadline after connect")
            io_t = rem_io if io_t is None else min(io_t, rem_io)
        if self.sock is not None:
            if io_t is not None:
                self.sock.settimeout(max(0.005, io_t))
            else:
                self.sock.setblocking(True)

    def _arm_response_deadline(self) -> None:
        now = time.monotonic()
        req_dl = (
            now + max(0.005, float(self.timeout))
            if isinstance(self.timeout, (int, float)) and self.timeout > 0
            else None
        )
        if self._io_deadline is not None:
            rem = self._io_deadline - now
            if rem <= 0:
                raise TimeoutError("HTTP response header wait exceeded total request deadline")
            eff_dl = self._io_deadline if req_dl is None else min(self._io_deadline, req_dl)
        else:
            eff_dl = req_dl
        self._phase_deadline = eff_dl
        if eff_dl is not None and self.sock is not None:
            self.sock.settimeout(max(0.005, eff_dl - now))

    def getresponse(self) -> http.client.HTTPResponse:
        self._arm_response_deadline()
        self._in_getresponse = True
        try:
            resp: http.client.HTTPResponse = super().getresponse()  # type: ignore[misc]
        except BaseException:
            self._in_getresponse = False
            self._release_bound_hook()
            with self._abort_lock:
                self._handed_off_to_resp = False
                self._cur_sock = None
            raise
        self._in_getresponse = False
        if self._io_deadline is not None:
            _install_deadline_proxy(resp, self._io_deadline)
        return resp


class _SplitTimeoutHTTPConnection(_SplitTimeoutConnMixin, http.client.HTTPConnection):
    """HTTPConnection separating cumulative connect/tunnel budget from request I/O timeout."""

    def __init__(
        self,
        host: str,
        port: int | None = None,
        *,
        connect_timeout: float | None = DEFAULT_CONNECT_TIMEOUT_SEC,
        io_deadline: float | None = None,
        cancel_token: CancelToken | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(host, port, **kwargs)
        self._init_split_timeout(connect_timeout, io_deadline, cancel_token)

    def connect(self) -> None:
        conn_dl = self._compute_connect_deadline()
        _connect_tcp_and_tunnel(self, conn_dl)
        try:
            self._apply_post_connect_timeout()
        except BaseException:
            with self._abort_lock:
                self._cur_sock = None
            _close_quietly(self.sock)
            self.sock = None
            raise


class _SplitTimeoutHTTPSConnection(_SplitTimeoutConnMixin, http.client.HTTPSConnection):
    """HTTPSConnection bounding TCP + proxy CONNECT + TLS handshake under connect_timeout."""

    def __init__(
        self,
        host: str,
        port: int | None = None,
        *,
        connect_timeout: float | None = DEFAULT_CONNECT_TIMEOUT_SEC,
        io_deadline: float | None = None,
        cancel_token: CancelToken | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(host, port, **kwargs)
        self._init_split_timeout(connect_timeout, io_deadline, cancel_token)

    def connect(self) -> None:
        conn_dl = self._compute_connect_deadline()
        _connect_tcp_and_tunnel(self, conn_dl)
        try:
            raw_sni = self._tunnel_host if self._tunnel_host else self.host  # type: ignore[attr-defined]
            server_hostname = (
                raw_sni[1:-1]
                if isinstance(raw_sni, str) and raw_sni.startswith("[") and raw_sni.endswith("]")
                else raw_sni
            )
            if conn_dl is not None and (conn_dl - time.monotonic()) <= 0:
                raise TimeoutError("TLS handshake timed out")
            with self._abort_lock:
                if self._aborted:
                    raise OSError(_ABORT_ERRNO, "HTTPS connect aborted before TLS wrap")
                tls_sock = self._context.wrap_socket(  # type: ignore[attr-defined]
                    self.sock,
                    server_hostname=server_hostname,
                    do_handshake_on_connect=False,
                )
                self.sock = tls_sock
                self._cur_sock = tls_sock
            _handshake_tls_with_deadline(self.sock, conn_dl, conn_owner=self)
            self._apply_post_connect_timeout()
        except BaseException:
            with self._abort_lock:
                self._cur_sock = None
            _close_quietly(self.sock)
            self.sock = None
            raise


class _SplitTimeoutHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        conn_t = getattr(req, "_voice_connect_timeout", DEFAULT_CONNECT_TIMEOUT_SEC)
        io_dl = getattr(req, "_voice_deadline", None)
        ctok = getattr(req, "_voice_cancel_token", None)
        return self.do_open(
            lambda host, **kw: _SplitTimeoutHTTPConnection(
                host, connect_timeout=conn_t, io_deadline=io_dl, cancel_token=ctok, **kw
            ),
            req,
        )


class _SplitTimeoutHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        conn_t = getattr(req, "_voice_connect_timeout", DEFAULT_CONNECT_TIMEOUT_SEC)
        io_dl = getattr(req, "_voice_deadline", None)
        ctok = getattr(req, "_voice_cancel_token", None)
        return self.do_open(
            lambda host, **kw: _SplitTimeoutHTTPSConnection(
                host, connect_timeout=conn_t, io_deadline=io_dl, cancel_token=ctok, **kw
            ),
            req,
            context=self._context,
        )


class _NoDnsProxyHandler(urllib.request.ProxyHandler):
    """ProxyHandler routing by pre-decided request proxy without calling blocking proxy_bypass()."""

    def proxy_open(self, req: urllib.request.Request, proxy: str, type: str) -> Any:
        _ = type
        orig_type = req.type
        parsed_p = urllib.parse.urlsplit(proxy if "://" in proxy else f"{orig_type}://{proxy}")
        proxy_type = (parsed_p.scheme or orig_type).lower()
        hostport = parsed_p.netloc or parsed_p.path
        if not hostport:
            return None
        if parsed_p.username is not None:
            user = urllib.parse.unquote(parsed_p.username)
            pw = urllib.parse.unquote(parsed_p.password or "")
            creds = base64.b64encode(f"{user}:{pw}".encode("utf-8")).decode("ascii")
            req.add_header("Proxy-authorization", f"Basic {creds}")
        if "@" in hostport:
            hostport = hostport.rsplit("@", 1)[1]
        req.set_proxy(urllib.parse.unquote(hostport), proxy_type)
        if orig_type == proxy_type or orig_type == "https":
            return None
        return self.parent.open(req, timeout=req.timeout)


_INITIAL_PROXIES = urllib.request.getproxies()
_SAFE_OPENER = urllib.request.build_opener(
    _NoDnsProxyHandler(_INITIAL_PROXIES),
    _SplitTimeoutHTTPHandler(),
    _SplitTimeoutHTTPSHandler(),
    _SameOriginSafeRedirectHandler(),
)
_DEFAULT_SAFE_OPENER = _SAFE_OPENER

_OriginKey = tuple[str, str, int, str]
_MAX_REDIRECTS = 5
_SLEEP_WALL_DRIFT_SEC: float = 15.0
_STALE_CONN_ERRORS = (
    http.client.RemoteDisconnected,
    http.client.CannotSendRequest,
    http.client.BadStatusLine,
    ConnectionError,
    ssl.SSLEOFError,
)


def _is_idle_socket_stale(sock: Any) -> bool:
    """0ms non-blocking probe for an idle keep-alive socket (detects EOF/RST/dirty bytes)."""
    if sock is None:
        return True
    if callable(pending_fn := getattr(sock, "pending", None)):
        try:
            if int(pending_fn()) > 0:
                return True
        except Exception:
            return True
    fileno_fn = getattr(sock, "fileno", None)
    if not callable(fileno_fn):
        return False
    try:
        fd = fileno_fn()
    except (AttributeError, io.UnsupportedOperation, TypeError):
        return False
    except (OSError, ValueError):
        return True
    if not isinstance(fd, int) or fd < 0:
        return True
    try:
        rlist, _, _ = select.select([sock], [], [], 0.0)
        return bool(rlist)
    except (OSError, ValueError):
        return True


_ANY_SCHEME_USERINFO_SECRET_RE = re.compile(
    r"([a-z][a-z0-9+.-]*://[^/\s:@]*:)[^/\s@]+(@)", re.IGNORECASE
)
_RAW_PROXY_PASSWORD_RE = re.compile(
    r"^(?:[a-z][a-z0-9+.-]*://)?[^/\s:@]*:([^/\s@]+)@", re.IGNORECASE
)
_SAFE_PROXY_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]{0,15}$")


@dataclass(frozen=True)
class _HttpProxyRoute:
    host: str
    port: int
    proxy_key: str
    proxy_scheme: str = "http"
    proxy_auth_header: str = field(default="", repr=False)


def _validate_proxy_auth_value(val: str) -> None:
    if any(bad in val for bad in ("\r", "\n", "\x00")):
        raise HttpTransportError("Invalid Proxy-Authorization header", retry_kind="never")
    try:
        val.encode("latin-1")
    except UnicodeEncodeError:
        raise HttpTransportError("Invalid Proxy-Authorization header", retry_kind="never") from None


def _normalize_proxy_fingerprint(proxy_url: str) -> str:
    raw = proxy_url.strip()
    if not raw:
        return ""
    try:
        parsed = urllib.parse.urlsplit(raw if "://" in raw else f"http://{raw}")
    except ValueError:
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    scheme = (parsed.scheme or "http").lower()
    host = (parsed.hostname or "").lower()
    if not host:
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    try:
        port = parsed.port
    except ValueError:
        port = -1
    if port is None:
        if scheme == "http":
            port = 80
        elif scheme == "https":
            port = 443
    auth_part = ""
    if parsed.username is not None or parsed.password is not None:
        user = urllib.parse.unquote(parsed.username or "")
        pw = urllib.parse.unquote(parsed.password or "")
        cred = f"{user}:{pw}".encode("utf-8")
        auth_part = f"|auth:{hashlib.sha256(cred).hexdigest()[:12]}"
    host_disp = f"[{host}]" if ":" in host else host
    return f"{scheme}://{host_disp}:{port if port is not None else ''}{auth_part}"


def _get_proxies_from_env(env: Mapping[str, str]) -> dict[str, str]:
    """Extract ``*_proxy`` map from ``env`` (lowercase overrides uppercase; empty lowercase clears)."""
    proxies: dict[str, str] = {}
    for name, value in env.items():
        low = name.lower()
        if not low.endswith("_proxy") or name == low:
            continue
        proto = low[:-6]
        if proto and (val := str(value or "").strip()):
            proxies[proto] = val
    for name, value in env.items():
        if not name.endswith("_proxy") or name != name.lower():
            continue
        proto = name[:-6]
        if not proto:
            continue
        val = str(value or "").strip()
        if val:
            proxies[proto] = val
        else:
            proxies.pop(proto, None)
    return proxies


def _select_system_proxy_url(proxies: Mapping[str, str], scheme: str) -> str:
    if not proxies:
        return ""
    return (proxies.get(scheme.lower()) or proxies.get("all") or "").strip()


_UNSUPPORTED_PROXY_HINT = (
    "only http:// proxies are supported; set HTTP_PROXY/HTTPS_PROXY=http://... or configure NO_PROXY"
)


def _resolve_http_proxy_route(
    proxy_url: str, *, target_scheme: str = "https"
) -> tuple[_HttpProxyRoute | None, str]:
    """Validate and parse a selected system proxy URL for Keep-Alive pooling.

    Supports:
    - Standard ``http://`` proxies (including schemeless ``host[:port]``) for both ``http://`` and
      ``https://`` targets;
    - Single-layer ``https://`` proxies for ``http://`` targets (TLS directly to proxy, no
      TLS-over-TLS tunnel needed).

    Fails closed for ``https://`` proxies with ``https://`` targets (to prevent plaintext CONNECT
    credential leakage), unsupported schemes (e.g. ``socks5://``), and malformed proxy URLs.
    """
    raw = proxy_url.strip()
    if not raw:
        return None, "invalid proxy URL configuration"
    try:
        parsed = urllib.parse.urlsplit(raw if "://" in raw else f"http://{raw}")
    except ValueError:
        return None, "invalid proxy URL configuration"
    proxy_scheme = (parsed.scheme or "").lower()
    tgt_scheme = target_scheme.lower()
    if proxy_scheme == "https" and tgt_scheme == "http":
        default_port = 443
    elif proxy_scheme == "http":
        default_port = 80
    elif proxy_scheme == "https":
        return (
            None,
            f"unsupported proxy scheme 'https' for https:// targets ({_UNSUPPORTED_PROXY_HINT})",
        )
    else:
        if proxy_scheme and _SAFE_PROXY_SCHEME_RE.match(proxy_scheme):
            return (
                None,
                f"unsupported proxy scheme '{proxy_scheme}' ({_UNSUPPORTED_PROXY_HINT})",
            )
        return None, f"unsupported proxy scheme ({_UNSUPPORTED_PROXY_HINT})"
    host = (parsed.hostname or "").strip()
    if not host or any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in host):
        return None, "invalid proxy URL configuration"
    try:
        port = parsed.port
    except ValueError:
        return None, "invalid proxy URL configuration"
    if port is None:
        port = default_port
    if port <= 0 or port > 65535:
        return None, "invalid proxy URL configuration"
    auth_header = ""
    if parsed.username is not None or parsed.password is not None:
        user = urllib.parse.unquote(parsed.username or "")
        pw = urllib.parse.unquote(parsed.password or "")
        creds = base64.b64encode(f"{user}:{pw}".encode("utf-8")).decode("ascii")
        auth_header = f"Basic {creds}"
    return (
        _HttpProxyRoute(
            host=host,
            port=port,
            proxy_key=_normalize_proxy_fingerprint(raw),
            proxy_scheme=proxy_scheme,
            proxy_auth_header=auth_header,
        ),
        "",
    )


def _parse_http_proxy_route(
    proxy_url: str, *, target_scheme: str = "https"
) -> _HttpProxyRoute | None:
    route, _ = _resolve_http_proxy_route(proxy_url, target_scheme=target_scheme)
    return route


def _redact_proxy_error_text(
    text: str,
    raw_proxy_url: str = "",
    proxy_route: _HttpProxyRoute | None = None,
    *,
    caller_proxy_auth: str = "",
) -> str:
    if not text:
        return text
    out = _ANY_SCHEME_USERINFO_SECRET_RE.sub(r"\1[REDACTED]\2", text)
    auth_secrets: set[str] = set()
    for auth_val in (
        proxy_route.proxy_auth_header if proxy_route is not None else "",
        caller_proxy_auth,
    ):
        auth_s = (auth_val or "").strip()
        if auth_s:
            auth_secrets.add(auth_s)
            parts = auth_s.split(None, 1)
            if len(parts) == 2 and parts[1].strip():
                auth_secrets.add(parts[1].strip())
    long_pw_secrets: set[str] = set()
    raw = raw_proxy_url.strip()
    if raw:
        raw_pw_candidates: list[str] = []
        try:
            parsed = urllib.parse.urlsplit(raw if "://" in raw else f"http://{raw}")
        except ValueError:
            parsed = None
        if parsed is not None and parsed.password:
            raw_pw_candidates.append(parsed.password)
        else:
            m = _RAW_PROXY_PASSWORD_RE.match(raw)
            if m and m.group(1):
                raw_pw_candidates.append(m.group(1))
        for raw_pw in raw_pw_candidates:
            if len(raw_pw) >= 4:
                long_pw_secrets.add(raw_pw)
            unquoted_pw = urllib.parse.unquote(raw_pw)
            if len(unquoted_pw) >= 4:
                long_pw_secrets.add(unquoted_pw)
                pct_pw = urllib.parse.quote(unquoted_pw, safe="")
                if len(pct_pw) >= 4:
                    long_pw_secrets.add(pct_pw)
    all_secrets = auth_secrets | long_pw_secrets
    for secret in sorted(all_secrets, key=len, reverse=True):
        out = out.replace(secret, "[REDACTED]")
    return _redact_secrets(out, env={}, extra_secrets=all_secrets)


def _proxy_bypass_no_dns(
    host: str, port: int | None = None, *, env: Mapping[str, str] | None = None
) -> bool:
    """Pure local NO_PROXY matcher without ever invoking blocking DNS (socket.gethostbyname)."""
    src = os.environ if env is None else env
    raw_no_proxy = src["no_proxy"] if "no_proxy" in src else src.get("NO_PROXY", "")
    no_proxy = raw_no_proxy.strip()
    if not no_proxy:
        return False
    h_low = host.strip().lower().strip("[]")
    if not h_low:
        return False
    host_ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
    try:
        host_ip = ipaddress.ip_address(h_low)
    except ValueError:
        host_ip = None

    for raw_pat in no_proxy.split(","):
        pat = raw_pat.strip().lower()
        if not pat:
            continue
        if pat == "*":
            return True
        pat_host = pat
        pat_port: int | None = None
        if pat.startswith("["):
            end_br = pat.find("]")
            if end_br > 0:
                pat_host = pat[1:end_br]
                rest = pat[end_br + 1 :]
                if rest.startswith(":") and rest[1:].isdigit():
                    pat_port = int(rest[1:])
        elif pat.count(":") == 1:
            h_part, p_part = pat.split(":", 1)
            if p_part.isdigit():
                pat_host = h_part
                pat_port = int(p_part)
        if pat_port is not None and port is not None and pat_port != port:
            continue
        if "/" in pat_host:
            if host_ip is not None:
                try:
                    if host_ip in ipaddress.ip_network(pat_host, strict=False):
                        return True
                except ValueError:
                    pass
            continue
        if host_ip is not None:
            try:
                if host_ip == ipaddress.ip_address(pat_host):
                    return True
            except ValueError:
                pass
        pat_norm = (pat_host[2:] if pat_host.startswith("*.") else pat_host).lstrip(".")
        if pat_norm and (h_low == pat_norm or h_low.endswith("." + pat_norm)):
            return True
    return False


def _resolve_system_proxy_key(scheme: str, host: str, port: int | None = None) -> str:
    proxy_url = _select_system_proxy_url(urllib.request.getproxies(), scheme)
    if not proxy_url:
        return ""
    if _proxy_bypass_no_dns(host, port):
        return ""
    return _normalize_proxy_fingerprint(proxy_url)


class _KeepAlivePool:
    """Thread-safe same-origin HTTP/1.1 Keep-Alive connection pool with dual-clock sleep eviction."""

    _MAX_IDLE_PER_ORIGIN = 5
    _SLEEP_WALL_DRIFT_SEC = _SLEEP_WALL_DRIFT_SEC

    def __init__(
        self,
        *,
        max_per_origin: int = _MAX_IDLE_PER_ORIGIN,
        idle_timeout_sec: float = 45.0,
        sleep_drift_sec: float = _SLEEP_WALL_DRIFT_SEC,
        mono_fn: Callable[[], float] = time.monotonic,
        wall_fn: Callable[[], float] = time.time,
    ) -> None:
        self.max_per_origin = max_per_origin
        self.idle_timeout_sec = idle_timeout_sec
        self.sleep_drift_sec = max(0.0, float(sleep_drift_sec))
        self._mono_fn = mono_fn
        self._wall_fn = wall_fn
        self._lock = threading.Lock()
        self._evict_gen: int = 0
        self._idle: dict[_OriginKey, list[tuple[http.client.HTTPConnection, float, float]]] = {}
        self._ssl_ctx: ssl.SSLContext | None = None

    def _get_ssl_context(self) -> ssl.SSLContext:
        with self._lock:
            if self._ssl_ctx is None:
                self._ssl_ctx = ssl.create_default_context()
            return self._ssl_ctx

    def _resolve_proxy_key(self, scheme: str, host: str, port: int) -> str:
        return _resolve_system_proxy_key(scheme, host, port)

    def _is_entry_expired(
        self, mono_ts: float, wall_ts: float, now_mono: float, now_wall: float
    ) -> bool:
        mono_age = now_mono - mono_ts
        wall_age = now_wall - wall_ts
        if mono_age < 0.0 or mono_age > self.idle_timeout_sec:
            return True
        if wall_age > self.idle_timeout_sec:
            return True
        if abs(wall_age - mono_age) > self.sleep_drift_sec:
            return True
        return False

    def _new_connection(
        self,
        scheme: str,
        host: str,
        port: int,
        timeout_sec: float,
        *,
        connect_timeout_sec: float = DEFAULT_CONNECT_TIMEOUT_SEC,
        io_deadline: float | None = None,
        proxy_route: _HttpProxyRoute | None = None,
    ) -> http.client.HTTPConnection:
        if proxy_route is not None:
            if scheme == "https":
                conn = _SplitTimeoutHTTPSConnection(
                    proxy_route.host,
                    proxy_route.port,
                    timeout=timeout_sec,
                    context=self._get_ssl_context(),
                    connect_timeout=connect_timeout_sec,
                    io_deadline=io_deadline,
                )
                tunnel_hdrs = (
                    {"Proxy-Authorization": proxy_route.proxy_auth_header}
                    if proxy_route.proxy_auth_header
                    else None
                )
                conn.set_tunnel(host, port, headers=tunnel_hdrs)
                return conn
            if proxy_route.proxy_scheme == "https":
                return _SplitTimeoutHTTPSConnection(
                    proxy_route.host,
                    proxy_route.port,
                    timeout=timeout_sec,
                    context=self._get_ssl_context(),
                    connect_timeout=connect_timeout_sec,
                    io_deadline=io_deadline,
                )
            return _SplitTimeoutHTTPConnection(
                proxy_route.host,
                proxy_route.port,
                timeout=timeout_sec,
                connect_timeout=connect_timeout_sec,
                io_deadline=io_deadline,
            )
        if scheme == "https":
            return _SplitTimeoutHTTPSConnection(
                host,
                port,
                timeout=timeout_sec,
                context=self._get_ssl_context(),
                connect_timeout=connect_timeout_sec,
                io_deadline=io_deadline,
            )
        return _SplitTimeoutHTTPConnection(
            host,
            port,
            timeout=timeout_sec,
            connect_timeout=connect_timeout_sec,
            io_deadline=io_deadline,
        )

    def _prune_idle_locked(
        self, now_mono: float, now_wall: float
    ) -> list[http.client.HTTPConnection]:
        to_close: list[http.client.HTTPConnection] = []
        for k in list(self._idle.keys()):
            bucket = self._idle[k]
            kept: list[tuple[http.client.HTTPConnection, float, float]] = []
            for c, m_ts, w_ts in bucket:
                if (
                    self._is_entry_expired(m_ts, w_ts, now_mono, now_wall)
                    or getattr(c, "sock", None) is None
                    or getattr(c, "_aborted", False)
                ):
                    to_close.append(c)
                else:
                    kept.append((c, m_ts, w_ts))
            if kept:
                self._idle[k] = kept
            else:
                del self._idle[k]
        return to_close

    def acquire(
        self,
        scheme: str,
        host: str,
        port: int,
        timeout_sec: float,
        *,
        allow_reuse: bool = True,
        connect_timeout_sec: float = DEFAULT_CONNECT_TIMEOUT_SEC,
        io_deadline: float | None = None,
        proxy_key: str | None = None,
        proxy_route: _HttpProxyRoute | None = None,
    ) -> tuple[http.client.HTTPConnection, bool]:
        if proxy_key is not None:
            eff_proxy_key = str(proxy_key)
        elif proxy_route is not None:
            eff_proxy_key = proxy_route.proxy_key
        else:
            eff_proxy_key = self._resolve_proxy_key(scheme, host, port)
        key: _OriginKey = (scheme, host, port, eff_proxy_key)
        now_mono = self._mono_fn()
        now_wall = self._wall_fn()
        first_pass = True

        while True:
            reused_conn: http.client.HTTPConnection | None = None
            with self._lock:
                cur_gen = self._evict_gen
                to_close = self._prune_idle_locked(now_mono, now_wall) if first_pass else []
                first_pass = False
                if allow_reuse and key in self._idle:
                    bucket = self._idle[key]
                    reused_conn, _, _ = bucket.pop()
                    if not bucket:
                        del self._idle[key]
            for c in to_close:
                _close_quietly(c)

            if reused_conn is None:
                break

            try:
                sock = getattr(reused_conn, "sock", None)
                if (
                    sock is None
                    or getattr(reused_conn, "_aborted", False)
                    or _is_idle_socket_stale(sock)
                ):
                    _close_quietly(reused_conn)
                    continue
                reused_conn.timeout = timeout_sec
                if hasattr(reused_conn, "_connect_timeout"):
                    setattr(reused_conn, "_connect_timeout", connect_timeout_sec)
                if hasattr(reused_conn, "_io_deadline"):
                    setattr(reused_conn, "_io_deadline", io_deadline)
                if hasattr(reused_conn, "_phase_deadline"):
                    setattr(reused_conn, "_phase_deadline", None)
                abort_lock = getattr(reused_conn, "_abort_lock", None)
                aborted_race = False
                if abort_lock is not None:
                    with abort_lock:
                        if getattr(reused_conn, "_aborted", False):
                            aborted_race = True
                        else:
                            setattr(
                                reused_conn,
                                "_borrow_gen",
                                int(getattr(reused_conn, "_borrow_gen", 0)) + 1,
                            )
                            setattr(reused_conn, "_cur_sock", sock)
                if aborted_race:
                    _close_quietly(reused_conn)
                    continue
                setattr(reused_conn, "_pool_evict_gen", cur_gen)
                setattr(reused_conn, "_pool_proxy_key", eff_proxy_key)
                sock.settimeout(timeout_sec)
                return reused_conn, True
            except Exception:
                _close_quietly(reused_conn)
                continue

        if proxy_route is not None:
            new_conn = self._new_connection(
                scheme,
                host,
                port,
                timeout_sec,
                connect_timeout_sec=connect_timeout_sec,
                io_deadline=io_deadline,
                proxy_route=proxy_route,
            )
        else:
            new_conn = self._new_connection(
                scheme,
                host,
                port,
                timeout_sec,
                connect_timeout_sec=connect_timeout_sec,
                io_deadline=io_deadline,
            )
        setattr(new_conn, "_pool_evict_gen", cur_gen)
        setattr(new_conn, "_pool_proxy_key", eff_proxy_key)
        return new_conn, False

    def release(
        self,
        scheme: str,
        host: str,
        port: int,
        conn: http.client.HTTPConnection,
        *,
        proxy_key: str | None = None,
    ) -> None:
        if hasattr(conn, "_io_deadline"):
            setattr(conn, "_io_deadline", None)
        if hasattr(conn, "_phase_deadline"):
            setattr(conn, "_phase_deadline", None)
        if callable(rel_fn := getattr(conn, "_release_bound_hook", None)):
            rel_fn()
        abort_lock = getattr(conn, "_abort_lock", None)
        if abort_lock is not None:
            with abort_lock:
                if getattr(conn, "_aborted", False):
                    abort_aborted = True
                else:
                    abort_aborted = False
                    setattr(conn, "_borrow_gen", int(getattr(conn, "_borrow_gen", 0)) + 1)
            if abort_aborted:
                _close_quietly(conn)
                return
        sock = getattr(conn, "sock", None)
        if (
            sock is None
            or getattr(conn, "_aborted", False)
            or self.max_per_origin <= 0
            or _is_idle_socket_stale(sock)
        ):
            _close_quietly(conn)
            return
        eff_proxy_key = (
            self._resolve_proxy_key(scheme, host, port)
            if proxy_key is None
            else str(proxy_key)
        )
        conn_proxy_key = getattr(conn, "_pool_proxy_key", eff_proxy_key)
        if conn_proxy_key != eff_proxy_key:
            _close_quietly(conn)
            return

        now_mono = self._mono_fn()
        now_wall = self._wall_fn()
        key: _OriginKey = (scheme, host, port, eff_proxy_key)
        to_close: list[http.client.HTTPConnection] = []
        with self._lock:
            conn_gen = getattr(conn, "_pool_evict_gen", self._evict_gen)
            if conn_gen != self._evict_gen:
                to_close.append(conn)
            else:
                bucket = self._idle.get(key, [])
                kept: list[tuple[http.client.HTTPConnection, float, float]] = []
                for c, m_ts, w_ts in bucket:
                    if (
                        self._is_entry_expired(m_ts, w_ts, now_mono, now_wall)
                        or getattr(c, "sock", None) is None
                        or getattr(c, "_aborted", False)
                    ):
                        to_close.append(c)
                    else:
                        kept.append((c, m_ts, w_ts))
                while len(kept) >= self.max_per_origin:
                    old_c, _, _ = kept.pop(0)
                    to_close.append(old_c)
                if getattr(conn, "_aborted", False) or getattr(conn, "sock", None) is None:
                    to_close.append(conn)
                else:
                    kept.append((conn, now_mono, now_wall))
                if kept:
                    self._idle[key] = kept
                else:
                    self._idle.pop(key, None)
        for c in to_close:
            _close_quietly(c)

    def evict_all(self) -> int:
        """Close and evict all idle connections and invalidate any in-flight pre-eviction leases."""
        to_close: list[http.client.HTTPConnection] = []
        with self._lock:
            self._evict_gen += 1
            for bucket in self._idle.values():
                for c, _, _ in bucket:
                    to_close.append(c)
            self._idle.clear()
        for c in to_close:
            abort_lock = getattr(c, "_abort_lock", None)
            if abort_lock is not None:
                with abort_lock:
                    setattr(c, "_borrow_gen", int(getattr(c, "_borrow_gen", 0)) + 1)
            _close_quietly(c)
        return len(to_close)

    def close_all(self) -> None:
        self.evict_all()


_KEEPALIVE_POOL = _KeepAlivePool()
atexit.register(_KEEPALIVE_POOL.close_all)


def evict_all_keepalive_connections() -> int:
    """Evict all idle sockets from the shared HTTP keep-alive pool (e.g. on power_resume)."""
    return _KEEPALIVE_POOL.evict_all()


def _validate_request_url(
    url: str,
) -> tuple[urllib.parse.SplitResult, tuple[str, str, int | None, str | None, str | None]]:
    parsed = urllib.parse.urlsplit(url)
    origin = _origin_tuple(parsed)
    scheme, host, port, _, _ = origin
    if scheme not in ("https", "http") or not host or port is None or port <= 0:
        raise urllib.error.URLError(f"invalid or unsupported URL: {url}")
    if scheme != "https" and not _is_loopback_url(url):
        raise urllib.error.URLError(
            f"refusing non-HTTPS request to {parsed.scheme}://{parsed.netloc}"
        )
    return parsed, origin


def _should_use_system_proxy(scheme: str, host: str, port: int | None = None) -> bool:
    return bool(_resolve_system_proxy_key(scheme, host, port))


def _combine_cancel_check(
    is_cancelled: Callable[[], bool] | None,
    cancel_token: CancelToken | None,
) -> Callable[[], bool] | None:
    if cancel_token is None:
        return is_cancelled
    if is_cancelled is None:
        return cancel_token.is_cancelled
    return lambda: cancel_token.is_cancelled() or is_cancelled()


def _register_conn_abort(cancel_token: CancelToken | None, conn: Any) -> int:
    if cancel_token is None:
        return 0
    make_fn = getattr(conn, "make_abort_fn", None)
    abort_fn = make_fn() if callable(make_fn) else getattr(conn, "abort_inflight", None)
    return cancel_token.register_abort(abort_fn) if callable(abort_fn) else 0


def _unregister_conn_abort(cancel_token: CancelToken | None, hook_id: int) -> None:
    if cancel_token is not None and hook_id:
        cancel_token.unregister_abort(hook_id)


def _extract_header_values(resp: Any, name: str) -> list[str]:
    headers = getattr(resp, "headers", None)
    if headers is None:
        return []
    get_all = getattr(headers, "get_all", None)
    if callable(get_all):
        vals = get_all(name)
        return [str(v) for v in vals if v is not None] if vals else []
    if isinstance(headers, Mapping):
        target = name.lower()
        return [str(v) for k, v in headers.items() if str(k).lower() == target and v is not None]
    return []


def _can_reuse_pooled_conn(
    conn: Any,
    resp: Any,
    conn_header: str = "",
    *,
    body_drained: bool,
    aborted: bool,
    proxy_conn_header: str = "",
) -> bool:
    raw_headers = [
        conn_header,
        proxy_conn_header,
        *_extract_header_values(resp, "connection"),
        *_extract_header_values(resp, "proxy-connection"),
    ]
    close_tokens = {
        tok_s.strip().lower()
        for hdr in raw_headers
        if hdr
        for tok_s in hdr.split(",")
        if tok_s.strip()
    }
    return (
        body_drained
        and not aborted
        and not bool(getattr(resp, "_aborted", False))
        and not bool(getattr(resp, "will_close", True))
        and "close" not in close_tokens
        and getattr(conn, "sock", None) is not None
    )


def _format_http_proxy_request_target(host: str, port: int, target_path: str) -> str:
    host_part = f"[{host}]" if ":" in host else host
    authority = host_part if port == 80 else f"{host_part}:{port}"
    return f"http://{authority}{target_path}"


def _execute_keepalive(
    req: HttpRequest,
    *,
    wall_deadline: float,
    hdrs_init: dict[str, str],
    proxy_key: str = "",
    proxy_route: _HttpProxyRoute | None = None,
) -> HttpResponse:
    eff_proxy_key = proxy_key or (proxy_route.proxy_key if proxy_route is not None else "")
    cur_url = req.url
    cur_method = req.method.upper()
    cur_body = req.body
    cur_headers = {k: v for k, v in hdrs_init.items() if k.lower() != "proxy-authorization"}
    if not any(k.lower() == "connection" for k in cur_headers):
        cur_headers["Connection"] = "keep-alive"
    conn_timeout = max(0.005, float(req.connect_timeout_sec))

    redirects = 0
    visited: dict[str, int] = {}

    while True:
        parsed, origin = _validate_request_url(cur_url)
        scheme, host, port, _, _ = origin
        assert port is not None

        rem = wall_deadline - time.monotonic()
        if rem <= 0:
            raise TimeoutError(
                "HTTP redirect exceeded total request deadline"
                if redirects > 0
                else f"HTTP request exceeded deadline before send for {req.url}"
            )
        eff_timeout = max(0.005, min(float(req.timeout_sec), rem))
        path = parsed.path or "/"
        target_path = f"{path}?{parsed.query}" if parsed.query else path
        if proxy_route is not None and scheme == "http":
            wire_target = _format_http_proxy_request_target(host, port, target_path)
            wire_headers = dict(cur_headers)
            if proxy_route.proxy_auth_header:
                wire_headers["Proxy-Authorization"] = proxy_route.proxy_auth_header
        else:
            wire_target = target_path
            wire_headers = cur_headers
        wire_body: bytes | None = (
            cur_body
            if (cur_body or cur_method not in ("GET", "HEAD", "DELETE", "OPTIONS"))
            else None
        )

        conn: http.client.HTTPConnection | None = None
        resp: http.client.HTTPResponse | None = None
        hook_id = 0
        tok = req.cancel_token
        for attempt in range(2):
            if tok is not None and tok.is_cancelled():
                raise HttpTransportError("HTTP request cancelled", retry_kind="never")
            conn, reused = _KEEPALIVE_POOL.acquire(
                scheme,
                host,
                port,
                eff_timeout,
                allow_reuse=(attempt == 0),
                connect_timeout_sec=conn_timeout,
                io_deadline=wall_deadline,
                proxy_key=eff_proxy_key,
                proxy_route=proxy_route,
            )
            hook_id = _register_conn_abort(tok, conn)
            try:
                if not reused:
                    conn.connect()
                conn.request(cur_method, wire_target, body=wire_body, headers=wire_headers)
                rem_resp = wall_deadline - time.monotonic()
                if rem_resp <= 0:
                    raise TimeoutError("HTTP response header wait exceeded total request deadline")
                sock = getattr(conn, "sock", None)
                if sock is not None:
                    sock.settimeout(max(0.005, min(float(req.timeout_sec), rem_resp)))
                resp = conn.getresponse()
                break
            except BaseException as exc:
                _unregister_conn_abort(tok, hook_id)
                hook_id = 0
                aborted = getattr(conn, "_aborted", False) or (tok is not None and tok.is_cancelled())
                _close_quietly(conn)
                conn = None
                if aborted:
                    raise HttpTransportError("HTTP request cancelled", retry_kind="never") from None
                if isinstance(exc, _STALE_CONN_ERRORS) and reused and attempt == 0:
                    rem_retry = wall_deadline - time.monotonic()
                    if rem_retry <= 0:
                        raise TimeoutError("HTTP retry exceeded total request deadline") from None
                    eff_timeout = max(0.005, min(float(req.timeout_sec), rem_retry))
                    continue
                raise

        assert conn is not None and resp is not None
        status = int(getattr(resp, "status", None) or resp.getcode() or 200)
        resp_hdrs = {k.lower(): v for k, v in resp.headers.items()}
        raw_loc = resp.headers.get("Location") or resp.headers.get("URI")
        is_redirect = (
            status in (301, 302, 303, 307, 308)
            and raw_loc is not None
            and (
                status in (307, 308)
                or (status in (301, 302, 303) and cur_method in ("GET", "HEAD", "POST"))
            )
        )

        can_reuse = False
        try:
            body = _read_stream_with_deadline(
                resp, wall_deadline, max_bytes=(_STREAM_CHUNK_BYTES if is_redirect else None)
            )
            body_fully_drained = (
                cur_method == "HEAD"
                or status in (204, 304)
                or (bool(getattr(resp, "chunked", False)) and resp.isclosed())
                or (not getattr(resp, "chunked", False) and getattr(resp, "length", None) == 0)
            )
            can_reuse = (
                status != 407
                and _can_reuse_pooled_conn(
                    conn,
                    resp,
                    resp_hdrs.get("connection", ""),
                    body_drained=body_fully_drained,
                    aborted=bool(
                        getattr(conn, "_aborted", False) or (tok is not None and tok.is_cancelled())
                    ),
                    proxy_conn_header=resp_hdrs.get("proxy-connection", ""),
                )
            )
        finally:
            _unregister_conn_abort(tok, hook_id)
            if getattr(conn, "_aborted", False) or (tok is not None and tok.is_cancelled()):
                can_reuse = False
            _close_quietly(resp)
            if can_reuse:
                _KEEPALIVE_POOL.release(scheme, host, port, conn, proxy_key=eff_proxy_key)
            else:
                _close_quietly(conn)

        if getattr(conn, "_aborted", False) or (tok is not None and tok.is_cancelled()):
            raise HttpTransportError("HTTP request cancelled", retry_kind="never")

        if not is_redirect:
            return HttpResponse(status=status, headers=resp_hdrs, body=body)

        assert raw_loc is not None
        if (wall_deadline - time.monotonic()) <= 0:
            raise TimeoutError("HTTP redirect exceeded total request deadline")

        loc_parts = urllib.parse.urlparse(raw_loc)
        if loc_parts.scheme.lower() not in ("http", "https", ""):
            raise urllib.error.URLError(f"Redirection to url '{raw_loc}' is not allowed")
        if not loc_parts.path and loc_parts.netloc:
            parts_list = list(loc_parts)
            parts_list[2] = "/"
            norm_loc = urllib.parse.urlunparse(parts_list)
        else:
            norm_loc = urllib.parse.urlunparse(loc_parts)
        norm_loc = urllib.parse.quote(norm_loc, encoding="iso-8859-1", safe=string.punctuation)
        resolved_url = urllib.parse.urljoin(cur_url, norm_loc).replace(" ", "%20")

        target = urllib.parse.urlsplit(resolved_url)
        new_origin = _origin_tuple(target)
        if origin != new_origin or origin[2] == -1:
            raise urllib.error.URLError(
                f"refusing cross-origin redirect from {parsed.scheme}://{parsed.netloc} "
                f"to {target.scheme}://{target.netloc}"
            )
        if new_origin[0] != "https" and not _is_loopback_url(resolved_url):
            raise urllib.error.URLError(
                f"refusing non-HTTPS redirect to {target.scheme}://{target.netloc}"
            )

        redirects += 1
        visited[resolved_url] = visited.get(resolved_url, 0) + 1
        if redirects > _MAX_REDIRECTS or visited[resolved_url] > 4:
            raise urllib.error.URLError(f"redirect limit ({_MAX_REDIRECTS}) exceeded")

        if status in (307, 308):
            cur_headers = {k: v for k, v in cur_headers.items() if k.lower() != "host"}
        else:
            if cur_method == "POST" or (status == 303 and cur_method != "HEAD"):
                cur_method = "GET"
            cur_body = b""
            cur_headers = {
                k: v
                for k, v in cur_headers.items()
                if k.lower() not in ("host", "content-length", "content-type", "transfer-encoding")
            }
        cur_url = resolved_url


_TRANSIENT_OS_ERRNOS = frozenset(
    e
    for e in (
        getattr(errno, "ETIMEDOUT", None),
        getattr(errno, "ECONNRESET", None),
        getattr(errno, "ECONNABORTED", None),
        getattr(errno, "ECONNREFUSED", None),
        getattr(errno, "EPIPE", None),
        getattr(errno, "ENETUNREACH", None),
        getattr(errno, "EHOSTUNREACH", None),
        getattr(errno, "ENETDOWN", None),
        getattr(errno, "EHOSTDOWN", None),
    )
    if e is not None
)


def classify_transport_error(exc: BaseException) -> str:
    """Classify low-level socket/TLS/HTTP transport exceptions BEFORE stringification."""
    if isinstance(exc, HttpTransportError):
        return exc.retry_kind
    rk = getattr(exc, "retry_kind", None)
    if isinstance(rk, str) and rk in ("rate_limit", "transient", "never"):
        return rk

    outer_msg = str(exc).lower()
    if any(
        frag in outer_msg
        for frag in (
            "refusing cross-origin redirect",
            "refusing non-https",
            "is not allowed",
            "invalid or unsupported url",
            "redirect limit",
            "no host given",
            "rate-limit pacing exceeded",
            "rate-limit concurrency queue exceeded",
        )
    ):
        return "never"

    inner: BaseException = exc
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, BaseException):
        inner = exc.reason

    if isinstance(inner, ssl.SSLCertVerificationError) or isinstance(
        exc, ssl.SSLCertVerificationError
    ):
        return "never"

    if isinstance(inner, ssl.SSLEOFError):
        return "transient"

    if isinstance(inner, ssl.SSLError):
        reason_up = f"{getattr(inner, 'reason', '') or ''} {inner}".upper()
        if any(
            tok in reason_up
            for tok in (
                "CERTIFICATE_VERIFY_FAILED",
                "CERT_",
                "WRONG_VERSION_NUMBER",
                "UNSUPPORTED_PROTOCOL",
                "PROTOCOL_VERSION",
                "NO_CIPHERS",
                "HOSTNAME_MISMATCH",
            )
        ):
            return "never"
        if any(
            tok in reason_up
            for tok in ("TIMED OUT", "TIMEOUT", "EOF", "RESET", "BROKEN PIPE", "SYSCALL")
        ):
            return "transient"
        return "never"

    if isinstance(inner, socket.gaierror):
        eai_again = getattr(socket, "EAI_AGAIN", None)
        msg_low = str(inner).lower()
        if (
            (eai_again is not None and getattr(inner, "errno", None) == eai_again)
            or "temporary failure" in msg_low
            or "try again" in msg_low
        ):
            return "transient"
        return "never"

    if isinstance(inner, (TimeoutError, socket.timeout, ConnectionError)):
        return "transient"

    if isinstance(
        inner,
        (
            http.client.RemoteDisconnected,
            http.client.IncompleteRead,
            http.client.BadStatusLine,
            http.client.CannotSendRequest,
            http.client.ResponseNotReady,
        ),
    ):
        return "transient"

    if isinstance(inner, (http.client.InvalidURL, http.client.LineTooLong)):
        return "never"

    if isinstance(inner, http.client.HTTPException):
        return "transient"

    if isinstance(inner, OSError):
        if getattr(inner, "errno", None) in _TRANSIENT_OS_ERRNOS:
            return "transient"
        if "timed out" in str(inner).lower():
            return "transient"

    return "never"


def _header_ci(headers: Mapping[str, str], name: str) -> str:
    target = name.lower()
    if target in headers:
        return str(headers[target] or "").strip()
    for k, v in headers.items():
        if k.lower() == target:
            return str(v or "").strip()
    return ""


def parse_retry_after(
    headers: Mapping[str, str], *, now_epoch: float | None = None
) -> float | None:
    """Parse standard ``Retry-After`` (seconds or HTTP-date) or ``Retry-After-Ms`` header."""
    raw_ms = _header_ci(headers, "retry-after-ms")
    if raw_ms:
        try:
            ms_val = float(raw_ms)
            if math.isfinite(ms_val) and ms_val >= 0:
                return ms_val / 1000.0
        except ValueError:
            pass

    raw = _header_ci(headers, "retry-after")
    if not raw:
        return None
    try:
        sec_val = float(raw)
        if math.isfinite(sec_val) and sec_val >= 0:
            return sec_val
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        ref = time.time() if now_epoch is None else now_epoch
        return max(0.0, dt.timestamp() - ref)
    except Exception:
        return None


_NON_RETRYABLE_HTTP_STATUSES = frozenset({400, 401, 402, 403, 404, 405, 410, 413, 415, 422})


def _is_transient_http_status(status: int) -> bool:
    return status in (408, 425, 500, 502, 503, 504) or (520 <= status <= 529)


def _is_doubao_rate_limit(status: int, api_code: str, api_msg: str) -> tuple[bool, bool]:
    """Return ``(is_rate_limit, is_concurrency_limit)`` for Doubao AUC responses."""
    if api_code in ("45000131", "45000030"):
        return False, False
    low = api_msg.lower()
    if any(
        perm in low
        for perm in (
            "lifetime",
            "package exhausted",
            "trial quota exhausted",
            "arrearage",
            "overdue",
        )
    ):
        return False, False
    is_conc = "concurrency" in low or "concurrent" in low
    if status == 429:
        return True, is_conc
    if any(
        pat in low
        for pat in (
            "quota exceeded for types: qps",
            "quota exceeded for types: concurrency",
            "qps limit",
            "exceeded qps",
            "qps exceeded",
            "concurrency limit",
            "concurrent limit",
            "too many requests",
            "rate limit",
            "ratelimit",
            "request too frequent",
            "too frequent",
        )
    ):
        return True, is_conc
    return False, False


def classify_http_failure(
    provider: str,
    status: int,
    headers: Mapping[str, str],
    data: Mapping[str, Any] | None = None,
    *,
    non_json: bool = False,
    api_code: str = "",
    api_msg: str = "",
) -> tuple[str, float | None, bool]:
    """Classify an HTTP/vendor failure into ``('rate_limit' | 'transient' | 'never', retry_after, is_conc)``."""
    prov = provider.strip().lower()
    retry_after = parse_retry_after(headers)

    if non_json:
        if status == 429:
            return "rate_limit", retry_after, False
        if status in _NON_RETRYABLE_HTTP_STATUSES:
            return "never", None, False
        if status == 200 or _is_transient_http_status(status):
            return "transient", retry_after, False
        return "never", None, False

    payload = data if isinstance(data, Mapping) else {}

    if prov == "doubao":
        is_rl, is_conc = _is_doubao_rate_limit(status, api_code, api_msg)
        if is_rl:
            return "rate_limit", retry_after, is_conc
        if api_code == "45000131":
            return "never", None, False
        if api_code == "55000031" or api_code.startswith("550"):
            return "transient", retry_after, False
        if _is_transient_http_status(status):
            return "transient", retry_after, False
        return "never", None, False

    if prov == "dashscope":
        raw_err = payload.get("error")
        err_dict = raw_err if isinstance(raw_err, Mapping) else {}
        code_str = str(
            api_code or payload.get("code") or err_dict.get("code") or err_dict.get("type") or ""
        ).strip()
        msg_str = str(
            api_msg or payload.get("message") or err_dict.get("message") or ""
        ).lower()
        code_low = code_str.lower()
        if any(
            perm in code_low or perm in msg_str
            for perm in ("arrearage", "allocationquota", "freetieronly", "invalidapikey")
        ):
            return "never", None, False
        is_conc = "concurrency" in code_low or "concurrency" in msg_str
        if (
            status == 429
            or code_low.startswith("throttling")
            or "rate_limit" in code_low
            or "ratelimit" in code_low
            or "requests rate limit" in msg_str
        ):
            return "rate_limit", retry_after, is_conc
        if (
            _is_transient_http_status(status)
            or code_low.startswith(("internalerror", "serviceunavailable", "modelunavailable"))
            or "system error" in msg_str
        ):
            return "transient", retry_after, False
        return "never", None, False

    if prov == "gemini":
        pf = payload.get("promptFeedback")
        if isinstance(pf, Mapping) and pf.get("blockReason"):
            return "never", None, False
        raw_err = payload.get("error")
        err_dict = raw_err if isinstance(raw_err, Mapping) else {}
        g_status = str(err_dict.get("status") or "").strip().upper()
        g_code = err_dict.get("code")
        g_msg = str(err_dict.get("message") or "").lower()
        if g_status in (
            "INVALID_ARGUMENT",
            "PERMISSION_DENIED",
            "UNAUTHENTICATED",
            "NOT_FOUND",
            "FAILED_PRECONDITION",
        ):
            return "never", None, False
        if "billing" in g_msg or "limit: 0" in g_msg:
            return "never", None, False
        if status == 429 or g_code == 429 or g_status == "RESOURCE_EXHAUSTED":
            return "rate_limit", retry_after, ("concurrent" in g_msg)
        if (
            _is_transient_http_status(status)
            or g_code in (500, 502, 503, 504)
            or g_status in ("UNAVAILABLE", "INTERNAL", "DEADLINE_EXCEEDED")
        ):
            return "transient", retry_after, False
        return "never", None, False

    # OpenRouter / OpenAI / Groq / SiliconFlow / Volcengine Ark / OpenAI-compatible
    raw_err = payload.get("error")
    choices = payload.get("choices")
    choice0 = (
        choices[0]
        if isinstance(choices, list) and choices and isinstance(choices[0], Mapping)
        else {}
    )
    if not raw_err and isinstance(choice0.get("error"), Mapping):
        raw_err = choice0.get("error")
    err_dict = raw_err if isinstance(raw_err, Mapping) else {}
    err_code = err_dict.get("code")
    err_type = str(err_dict.get("type") or "").strip().lower()
    err_msg = str(err_dict.get("message") or raw_err or "").lower()
    meta = err_dict.get("metadata")
    meta_type = str(meta.get("error_type") or "").strip().lower() if isinstance(meta, Mapping) else ""
    finish_reason = str(choice0.get("finish_reason") or "").strip().lower()

    if status == 402 or err_code == 402:
        return "never", None, False
    if (
        str(err_code).lower() == "insufficient_quota"
        or err_type == "insufficient_quota"
        or "check your plan and billing" in err_msg
    ):
        return "never", None, False
    if err_code in _NON_RETRYABLE_HTTP_STATUSES or err_type in (
        "invalid_request_error",
        "authentication_error",
        "permission_error",
        "not_found_error",
    ):
        return "never", None, False

    is_conc = "concurrency" in err_msg or "concurrent" in err_msg
    if (
        status == 429
        or err_code == 429
        or "rate_limit" in err_type
        or "rate_limit" in meta_type
        or "throttl" in err_type
        or str(err_code).lower() in ("rate_limit_exceeded", "server_overloaded")
    ):
        return "rate_limit", retry_after, is_conc

    if (
        _is_transient_http_status(status)
        or (isinstance(err_code, int) and _is_transient_http_status(err_code))
        or meta_type in ("provider_overloaded", "provider_unavailable", "server", "timeout")
        or err_type in ("server_error", "internal_error", "service_unavailable", "timeout")
        or (finish_reason == "error" and status == 200)
    ):
        return "transient", retry_after, False

    return "never", None, False


def compute_retry_delay(
    retry_kind: str,
    retry_index: int,
    *,
    retry_after_sec: float | None = None,
    rand_fn: Callable[[], float] = random.random,
) -> float:
    """Compute equal-jitter backoff delay for 1-based ``retry_index`` honoring ``Retry-After``."""
    idx = max(1, int(retry_index))
    u = max(0.0, min(1.0, float(rand_fn())))
    if retry_kind == "rate_limit":
        cap = min(8.0, float(2**idx))
    else:
        cap = min(2.0, 0.25 * float(2 ** (idx - 1)))
    jittered = cap * (0.5 + 0.5 * u)
    if retry_after_sec is not None and retry_after_sec > 0:
        return max(jittered, float(retry_after_sec))
    return jittered


@dataclass
class _PacerBucket:
    next_allowed_at: float = 0.0
    cooldown_until: float = 0.0
    serialize_until: float = 0.0
    min_interval_sec: float = 0.55
    inflight_lock: threading.Lock = field(default_factory=threading.Lock)


class _ProviderRatePacer:
    """Lightweight per-credential request pacer activated ONLY after a real rate-limit event."""

    DEFAULT_MIN_INTERVAL_SEC = 0.55
    DEFAULT_COOLDOWN_SEC = 60.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[str, _PacerBucket] = {}

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()

    def note_rate_limit(
        self,
        key: str,
        *,
        now: float,
        retry_after_sec: float | None = None,
        is_concurrency: bool = False,
        min_interval_sec: float = DEFAULT_MIN_INTERVAL_SEC,
        cooldown_sec: float = DEFAULT_COOLDOWN_SEC,
    ) -> None:
        if not key:
            return
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                b = _PacerBucket()
                self._buckets[key] = b
            b.min_interval_sec = max(b.min_interval_sec, float(min_interval_sec))
            b.cooldown_until = max(b.cooldown_until, now + float(cooldown_sec))
            if is_concurrency:
                b.serialize_until = max(b.serialize_until, now + float(cooldown_sec))
            floor_wait = float(retry_after_sec) if (retry_after_sec and retry_after_sec > 0) else b.min_interval_sec
            b.next_allowed_at = max(b.next_allowed_at, now + floor_wait)

    @contextlib.contextmanager
    def guard(
        self,
        key: str,
        *,
        deadline: float | None,
        clock_fn: Callable[[], float],
        sleep_fn: Callable[[float], None],
        is_cancelled: Callable[[], bool] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> Iterator[bool]:
        if not key:
            yield False
            return
        eff_cancelled = _combine_cancel_check(is_cancelled, cancel_token)
        now = clock_fn()
        with self._lock:
            b = self._buckets.get(key)
            if b is None or (now >= b.cooldown_until and now >= b.serialize_until):
                b = None
            else:
                need_serial = now < b.serialize_until
                inflight_lock = b.inflight_lock
        if b is None:
            yield False
            return

        acquired_serial = False
        waited = False
        if need_serial:
            t_lock = clock_fn()
            if eff_cancelled is not None or (deadline is not None and clock_fn is time.monotonic):
                while True:
                    if eff_cancelled is not None and eff_cancelled():
                        yield False
                        return
                    if deadline is not None and clock_fn is time.monotonic:
                        rem_lock = deadline - clock_fn()
                        if rem_lock <= 0:
                            raise TimeoutError("rate-limit concurrency queue exceeded timeout budget")
                        step_to = min(0.05, rem_lock) if eff_cancelled is not None else max(0.005, rem_lock)
                    else:
                        step_to = 0.05
                    if inflight_lock.acquire(timeout=max(0.002, step_to)):
                        acquired_serial = True
                        break
                    if deadline is not None and clock_fn is time.monotonic and (deadline - clock_fn()) <= 0:
                        raise TimeoutError("rate-limit concurrency queue exceeded timeout budget")
            else:
                inflight_lock.acquire()
                acquired_serial = True
            waited = (clock_fn() - t_lock) > 0.001

        try:
            with self._lock:
                now2 = clock_fn()
                if now2 < b.cooldown_until:
                    slot_at = max(now2, b.next_allowed_at)
                    if deadline is not None and slot_at >= deadline:
                        raise TimeoutError("rate-limit pacing exceeded remaining timeout budget")
                    b.next_allowed_at = slot_at + b.min_interval_sec
                    wait_sec = max(0.0, slot_at - now2)
                else:
                    wait_sec = 0.0
            if wait_sec > 0:
                waited = True
                sleep_cooperative(
                    wait_sec,
                    sleep_fn=sleep_fn,
                    is_cancelled=eff_cancelled,
                    cancel_token=cancel_token,
                )
            yield waited
        finally:
            if acquired_serial:
                inflight_lock.release()


def sleep_cooperative(
    seconds: float,
    *,
    sleep_fn: Callable[[float], None],
    is_cancelled: Callable[[], bool] | None = None,
    cancel_token: CancelToken | None = None,
) -> None:
    if seconds <= 0:
        return
    if (cancel_token is not None and cancel_token.is_cancelled()) or (
        is_cancelled is not None and is_cancelled()
    ):
        return
    if sleep_fn is time.sleep and cancel_token is not None:
        ev = threading.Event()
        cancel_token.bind_event(ev)
        try:
            if is_cancelled is None:
                ev.wait(seconds)
                return
            end_ev = time.monotonic() + seconds
            while True:
                if is_cancelled() or ev.is_set():
                    return
                rem_ev = end_ev - time.monotonic()
                if rem_ev <= 0:
                    return
                if ev.wait(min(0.05, rem_ev)):
                    return
        finally:
            cancel_token.unbind_event(ev)
    if sleep_fn is not time.sleep or is_cancelled is None or seconds <= 0.1:
        sleep_fn(seconds)
        return
    end = time.monotonic() + seconds
    while True:
        if is_cancelled():
            return
        rem = end - time.monotonic()
        if rem <= 0:
            return
        sleep_fn(min(0.05, rem))


_sleep_cooperative = sleep_cooperative


def make_rate_pacer_key(provider_name: str, base_url: str, secret: str = "") -> str:
    parsed = urllib.parse.urlsplit(base_url.strip())
    host = (parsed.netloc or base_url.strip()).lower()
    fp = hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12] if secret else "anon"
    return f"{provider_name.strip().lower()}|{host}|{fp}"


_RATE_PACER = _ProviderRatePacer()


def reset_rate_limit_pacers() -> None:
    """Clear process-local adaptive rate-limit pacer state (used in unit tests)."""
    _RATE_PACER.reset()


def urllib_http_transport(req: HttpRequest) -> HttpResponse:
    """Default pure-stdlib HTTP transport with Keep-Alive pooling, HTTP proxy support, and safe redirects."""
    wall_deadline = (
        req.deadline if req.deadline is not None else (time.monotonic() + float(req.timeout_sec))
    )
    caller_proxy_auth = ""
    hdrs_out: dict[str, str] = {}
    for k, v in req.headers.items():
        if k.lower() == "proxy-authorization":
            if v is not None:
                raw_auth = str(v)
                if raw_auth:
                    _validate_proxy_auth_value(raw_auth)
                    stripped_auth = raw_auth.strip()
                    if stripped_auth:
                        caller_proxy_auth = stripped_auth
        else:
            hdrs_out[k] = v
    if not any(k.lower() == "user-agent" for k in hdrs_out):
        hdrs_out["User-Agent"] = DEFAULT_USER_AGENT

    raw_proxy_url = ""
    proxy_route: _HttpProxyRoute | None = None
    try:
        if req.cancel_token is not None and req.cancel_token.is_cancelled():
            raise HttpTransportError("HTTP request cancelled", retry_kind="never")
        rem = wall_deadline - time.monotonic()
        if rem <= 0:
            raise TimeoutError(f"HTTP request exceeded deadline before send for {req.url}")

        _, origin = _validate_request_url(req.url)
        scheme, host, port, _, _ = origin

        if not _should_use_system_proxy(scheme, host, port):
            return _execute_keepalive(
                req, wall_deadline=wall_deadline, hdrs_init=hdrs_out, proxy_key=""
            )

        cur_proxies = urllib.request.getproxies()
        raw_proxy_url = _select_system_proxy_url(cur_proxies, scheme)
        proxy_route, proxy_err = _resolve_http_proxy_route(raw_proxy_url, target_scheme=scheme)
        if proxy_route is None:
            safe_cfg_msg = _redact_proxy_error_text(
                f"HTTP proxy configuration error for {req.url}: {proxy_err}",
                raw_proxy_url,
                None,
                caller_proxy_auth=caller_proxy_auth,
            )
            raise HttpTransportError(safe_cfg_msg, retry_kind="never")

        if not proxy_route.proxy_auth_header and caller_proxy_auth:
            auth_fp = hashlib.sha256(caller_proxy_auth.encode("utf-8")).hexdigest()[:12]
            proxy_route = _HttpProxyRoute(
                host=proxy_route.host,
                port=proxy_route.port,
                proxy_key=f"{proxy_route.proxy_key}|auth:{auth_fp}",
                proxy_scheme=proxy_route.proxy_scheme,
                proxy_auth_header=caller_proxy_auth,
            )

        return _execute_keepalive(
            req,
            wall_deadline=wall_deadline,
            hdrs_init=hdrs_out,
            proxy_key=proxy_route.proxy_key,
            proxy_route=proxy_route,
        )
    except (urllib.error.URLError, TimeoutError, socket.timeout, http.client.HTTPException, OSError) as exc:
        if req.cancel_token is not None and req.cancel_token.is_cancelled():
            raise HttpTransportError("HTTP request cancelled", retry_kind="never") from None
        rkind = classify_transport_error(exc)
        safe_msg = _redact_proxy_error_text(
            f"HTTP transport error for {req.url}: {exc}",
            raw_proxy_url,
            proxy_route,
            caller_proxy_auth=caller_proxy_auth,
        )
        raise HttpTransportError(safe_msg, retry_kind=rkind) from None
