"""Opt-in local HTTP proxy that binds upstream IPv4 sockets to a macOS interface."""

from __future__ import annotations

import contextlib
import http.server
import os
import platform
import select
import socket
import socketserver
import threading
import urllib.parse
from collections.abc import Callable, Iterator

_IP_BOUND_IF = 25  # macOS IP_BOUND_IF
_Dialer = Callable[[str, int, int], socket.socket]


class DirectProxyError(RuntimeError):
    """Raised when the interface-bound proxy cannot be configured or started."""


def _dial_ipv4(host: str, port: int, interface_index: int) -> socket.socket:
    """Resolve and connect to an IPv4 host with the selected interface bound."""
    try:
        addresses = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        if not addresses:
            raise OSError("no IPv4 address found")
        last_error: OSError | None = None
        for family, socktype, proto, _, address in addresses:
            upstream = socket.socket(family, socktype, proto)
            try:
                upstream.setsockopt(socket.IPPROTO_IP, _IP_BOUND_IF, interface_index)
                upstream.settimeout(15)
                upstream.connect(address)
                upstream.settimeout(None)
                return upstream
            except OSError as exc:
                last_error = exc
                upstream.close()
        if last_error is not None:
            raise last_error
        raise OSError("no usable IPv4 address found")
    except OSError as exc:
        raise DirectProxyError(f"could not connect to {host}:{port} through selected interface: {exc}") from exc


class _ProxyState:
    def __init__(self, interface_index: int, dialer: _Dialer) -> None:
        self.interface_index = interface_index
        self.dialer = dialer
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.upstreams: set[socket.socket] = set()

    def connect(self, host: str, port: int) -> socket.socket:
        upstream = self.dialer(host, port, self.interface_index)
        with self.lock:
            if self.stopping.is_set():
                upstream.close()
                raise OSError("proxy is shutting down")
            self.upstreams.add(upstream)
        return upstream

    def release(self, upstream: socket.socket) -> None:
        with self.lock:
            self.upstreams.discard(upstream)
        with contextlib.suppress(OSError):
            upstream.close()

    def close_all(self) -> None:
        self.stopping.set()
        with self.lock:
            upstreams = tuple(self.upstreams)
            self.upstreams.clear()
        for upstream in upstreams:
            with contextlib.suppress(OSError):
                upstream.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                upstream.close()


class _ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ApplyPilotDirectProxy/1"
    sys_version = ""

    @property
    def state(self) -> _ProxyState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: object) -> None:
        return

    def _target(self, raw_target: str) -> tuple[str, int, str]:
        parsed = urllib.parse.urlsplit(raw_target)
        if parsed.scheme.lower() != "http" or not parsed.hostname:
            raise ValueError("proxy requests must use an absolute http URL")
        port = parsed.port or 80
        path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        return parsed.hostname, port, path

    def _send_error_response(self, status: int, message: str) -> None:
        body = (message + "\n").encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Connection", "close")
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass
        finally:
            self.close_connection = True

    def _forward_http(self) -> None:
        try:
            host, port, path = self._target(self.path)
        except (ValueError, UnicodeError):
            self._send_error_response(400, "invalid proxy request")
            return
        upstream: socket.socket | None = None
        try:
            upstream = self.state.connect(host, port)
            lines = [f"{self.command} {path} {self.request_version}\r\n"]
            for name, value in self.headers.items():
                if name.lower() not in {"proxy-connection", "connection"}:
                    lines.append(f"{name}: {value}\r\n")
            lines.extend(("Connection: close\r\n", "\r\n"))
            upstream.sendall("".join(lines).encode("iso-8859-1"))
            if self.command not in {"GET", "HEAD"}:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > 16 * 1024 * 1024:
                    self._send_error_response(413, "request body too large")
                    return
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        raise OSError("client closed while sending request body")
                    upstream.sendall(chunk)
                    remaining -= len(chunk)
            while True:
                chunk = upstream.recv(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
            self.close_connection = True
        except (OSError, ValueError, DirectProxyError):
            if not self.wfile.closed:
                with contextlib.suppress(OSError):
                    self._send_error_response(502, "upstream connection failed")
        finally:
            if upstream is not None:
                self.state.release(upstream)

    do_GET = _forward_http
    do_HEAD = _forward_http
    do_POST = _forward_http
    do_PUT = _forward_http
    do_PATCH = _forward_http
    do_DELETE = _forward_http
    do_OPTIONS = _forward_http

    def do_CONNECT(self) -> None:
        authority = self.path.rsplit("@", 1)[-1]
        host, separator, port_text = authority.rpartition(":")
        if not separator or not host:
            self._send_error_response(400, "invalid connect target")
            return
        try:
            port = int(port_text)
            if not 1 <= port <= 65535:
                raise ValueError
            upstream = self.state.connect(host.strip("[]"), port)
        except (ValueError, OSError, DirectProxyError):
            self._send_error_response(502, "upstream connection failed")
            return

        try:
            self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self.wfile.flush()
            client = self.connection
            peers = (client, upstream)
            while not self.state.stopping.is_set():
                readable, _, _ = select.select(peers, (), (), 0.5)
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    destination = upstream if source is client else client
                    destination.sendall(data)
        except OSError:
            return
        finally:
            self.state.release(upstream)
            self.close_connection = True


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], handler: type[http.server.BaseHTTPRequestHandler], state: _ProxyState):
        self.state = state
        super().__init__(address, handler)


@contextlib.contextmanager
def direct_proxy(interface_name: str | None, *, _dialer: _Dialer | None = None) -> Iterator[str | None]:
    """Run an ephemeral loopback proxy whose upstream IPv4 sockets bind to a macOS interface.

    Pass the yielded URL as the HTTP and HTTPS proxy for a single operation. Passing
    ``None`` as the interface disables the proxy and yields ``None``.
    """
    if interface_name is None:
        yield None
        return
    if not isinstance(interface_name, str) or not interface_name.strip():
        raise DirectProxyError("interface name must be a non-empty string")
    if platform.system() != "Darwin":
        raise DirectProxyError("interface-bound proxy is supported only on macOS")
    try:
        interface_index = socket.if_nametoindex(interface_name)
    except OSError as exc:
        raise DirectProxyError(f"invalid network interface {interface_name!r}: {exc}") from exc
    if interface_index <= 0:
        raise DirectProxyError(f"invalid network interface {interface_name!r}")

    state = _ProxyState(interface_index, _dialer or _dial_ipv4)
    try:
        server = _ThreadingHTTPServer(("127.0.0.1", 0), _ProxyHandler, state)
    except OSError as exc:
        raise DirectProxyError(f"could not start local proxy: {exc}") from exc
    thread = threading.Thread(target=server.serve_forever, name="applypilot-direct-proxy", daemon=True)
    try:
        thread.start()
    except RuntimeError as exc:
        server.server_close()
        raise DirectProxyError(f"could not start local proxy thread: {exc}") from exc
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        state.close_all()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@contextlib.contextmanager
def hh_proxy() -> Iterator[str | None]:
    """Run an ephemeral loopback proxy bound to APPLYPILOT_HH_INTERFACE if configured."""
    interface = os.getenv("APPLYPILOT_HH_INTERFACE", "").strip()
    if interface:
        with direct_proxy(interface) as proxy_url:
            yield proxy_url
    else:
        yield None
