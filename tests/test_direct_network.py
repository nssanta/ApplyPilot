from __future__ import annotations

import http.client
import socket
import threading
from unittest.mock import patch

import pytest

from applypilot.direct_network import DirectProxyError, _ProxyHandler, direct_proxy


def test_none_interface_disables_proxy() -> None:
    with direct_proxy(None) as proxy_url:
        assert proxy_url is None


def test_error_response_ignores_closed_client_socket() -> None:
    class ClosedClientWriter:
        closed = False

        def write(self, _: bytes) -> int:
            raise ConnectionResetError("client has disconnected")

    handler = object.__new__(_ProxyHandler)
    handler.wfile = ClosedClientWriter()
    handler.close_connection = False
    handler.send_response = lambda *_: None
    handler.send_header = lambda *_: None
    handler.end_headers = lambda: None

    _ProxyHandler._send_error_response(handler, 502, "upstream connection failed")

    assert handler.close_connection


def test_connect_failure_returns_502_to_live_client() -> None:
    def dialer(_: str, __: int, ___: int) -> socket.socket:
        raise OSError("upstream unavailable")

    with (
        patch("applypilot.direct_network.platform.system", return_value="Darwin"),
        patch("applypilot.direct_network.socket.if_nametoindex", return_value=9),
        direct_proxy("en0", _dialer=dialer) as proxy_url,
    ):
        assert proxy_url is not None
        port = int(proxy_url.rsplit(":", 1)[1])
        with socket.create_connection(("127.0.0.1", port), timeout=3) as client:
            client.sendall(b"CONNECT secure.example.test:443 HTTP/1.1\r\nHost: secure.example.test:443\r\n\r\n")
            response = client.recv(4096)
        assert response.startswith(b"HTTP/1.1 502 Bad Gateway\r\n")


def test_rejects_non_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("applypilot.direct_network.platform.system", lambda: "Linux")
    with pytest.raises(DirectProxyError, match="only on macOS"), direct_proxy("en0"):
        raise AssertionError("proxy should not start")


def test_rejects_unknown_interface(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("applypilot.direct_network.platform.system", lambda: "Darwin")

    def unknown_interface(_: str) -> int:
        raise OSError("interface not found")

    monkeypatch.setattr("applypilot.direct_network.socket.if_nametoindex", unknown_interface)
    with pytest.raises(DirectProxyError, match="invalid network interface"), direct_proxy("missing0"):
        raise AssertionError("proxy should not start")


def test_http_request_uses_interface_bound_dialer() -> None:
    observed: list[tuple[str, int, int]] = []
    peer_threads: list[threading.Thread] = []

    def dialer(host: str, port: int, interface_index: int) -> socket.socket:
        observed.append((host, port, interface_index))
        proxy_side, server_side = socket.socketpair()

        def respond() -> None:
            request = bytearray()
            while b"\r\n\r\n" not in request:
                request.extend(server_side.recv(4096))
            server_side.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            server_side.close()

        thread = threading.Thread(target=respond, daemon=True)
        thread.start()
        peer_threads.append(thread)
        return proxy_side

    with (
        patch("applypilot.direct_network.platform.system", return_value="Darwin"),
        patch("applypilot.direct_network.socket.if_nametoindex", return_value=13),
        direct_proxy("en0", _dialer=dialer) as proxy_url,
    ):
        assert proxy_url is not None
        proxy = http.client.HTTPConnection("127.0.0.1", int(proxy_url.rsplit(":", 1)[1]), timeout=3)
        proxy.request("GET", "http://jobs.example.test/search?q=go", headers={"Host": "jobs.example.test"})
        response = proxy.getresponse()
        assert response.status == 200
        assert response.read() == b"ok"
        proxy.close()
    for thread in peer_threads:
        thread.join(timeout=1)
    assert observed == [("jobs.example.test", 80, 13)]


def test_connect_tunnels_through_interface_bound_dialer() -> None:
    observed: list[tuple[str, int, int]] = []

    def dialer(host: str, port: int, interface_index: int) -> socket.socket:
        observed.append((host, port, interface_index))
        proxy_side, server_side = socket.socketpair()

        def respond() -> None:
            data = server_side.recv(64)
            if data:
                server_side.sendall(b"tls-response")
            server_side.close()

        threading.Thread(target=respond, daemon=True).start()
        return proxy_side

    with (
        patch("applypilot.direct_network.platform.system", return_value="Darwin"),
        patch("applypilot.direct_network.socket.if_nametoindex", return_value=9),
        direct_proxy("en0", _dialer=dialer) as proxy_url,
    ):
        assert proxy_url is not None
        proxy = http.client.HTTPConnection("127.0.0.1", int(proxy_url.rsplit(":", 1)[1]), timeout=3)
        proxy.set_tunnel("secure.example.test", 443)
        proxy.connect()
        proxy.sock.sendall(b"tls-request")
        assert proxy.sock.recv(64) == b"tls-response"
        proxy.close()
    assert observed == [("secure.example.test", 443, 9)]
