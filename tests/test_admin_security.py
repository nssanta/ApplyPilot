from __future__ import annotations

import http.client
import json
import re
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

import pytest

from applypilot.admin import INDEX_HTML, AdminApp, _handler, serve
from applypilot.config import AppConfig


@contextmanager
def running_admin(tmp_path):
    app = AdminApp(AppConfig.discover(root=tmp_path))
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(app))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield app, server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(port, method, path, *, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def browser_headers(port, *, token=None, origin=None, content_type="application/json"):
    headers = {
        "Host": f"127.0.0.1:{port}",
        "Content-Type": content_type,
    }
    if origin is not None:
        headers["Origin"] = origin
    if token is not None:
        headers["X-ApplyPilot-Token"] = token
    return headers


def get_token(port):
    status, headers, body = request(port, "GET", "/")
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Content-Security-Policy"] == "frame-ancestors 'none'"
    match = re.search(rb'const ADMIN_TOKEN="([A-Za-z0-9_-]+)"', body)
    assert match
    return match.group(1).decode("ascii")


def test_every_post_route_rejects_requests_without_csrf_token(tmp_path):
    mutating_routes = (
        "/api/job", "/api/stop", "/api/settings", "/api/letter", "/api/track",
        "/api/track/edit", "/api/track/delete", "/api/queue", "/api/applied",
        "/api/letter-sent", "/api/viewed", "/api/bad", "/api/watch",
    )
    with running_admin(tmp_path) as (_app, port):
        headers = browser_headers(port, origin=f"http://127.0.0.1:{port}")
        for path in mutating_routes:
            status, _headers, body = request(port, "POST", path, body=b"{}", headers=headers)
            assert status == 403, (path, body)
        assert not (tmp_path / "private/data/admin-settings.json").exists()


def test_post_requires_same_origin_json_and_launch_token(tmp_path):
    with running_admin(tmp_path) as (app, port):
        token = get_token(port)
        valid_body = json.dumps({"base_url": "https://api.example.invalid/v1/chat/completions"})
        cases = (
            (browser_headers(port, token=token), 403),
            (browser_headers(port, token=token, origin="null"), 403),
            (browser_headers(port, token=token, origin=f"http://attacker.example:{port}"), 403),
            (browser_headers(port, token="wrong", origin=f"http://127.0.0.1:{port}"), 403),
            (browser_headers(port, token=token, origin=f"http://127.0.0.1:{port}",
                             content_type="text/plain"), 415),
        )
        for headers, expected in cases:
            status, _response_headers, _body = request(
                port, "POST", "/api/settings", body=valid_body, headers=headers)
            assert status == expected
        assert not app.settings_path.exists()

        valid = browser_headers(port, token=token, origin=f"http://127.0.0.1:{port}")
        status, _headers, body = request(port, "POST", "/api/settings", body=valid_body, headers=valid)
        assert status == 200, body
        assert json.loads(app.settings_path.read_text(encoding="utf-8"))["base_url"] == (
            "https://api.example.invalid/v1/chat/completions")


def test_host_check_blocks_dns_rebinding_reads_and_posts(tmp_path):
    with running_admin(tmp_path) as (_app, port):
        hostile = {"Host": f"attacker.example:{port}"}
        status, _headers, body = request(port, "GET", "/api/settings", headers=hostile)
        assert status == 403
        assert json.loads(body)["error"] == "invalid Host header"

        token = get_token(port)
        hostile.update({"Origin": f"http://attacker.example:{port}",
                        "Content-Type": "application/json", "X-ApplyPilot-Token": token})
        status, _headers, _body = request(port, "POST", "/api/watch", body=b'{"action":"install"}',
                                         headers=hostile)
        assert status == 403


def test_admin_tokens_are_random_per_instance(tmp_path):
    first = AdminApp(AppConfig.discover(root=tmp_path))
    second = AdminApp(AppConfig.discover(root=tmp_path))

    assert len(first._csrf_token) >= 40
    assert first._csrf_token != second._csrf_token


def test_admin_refuses_public_bind_addresses(tmp_path):
    with pytest.raises(ValueError, match="loopback"):
        serve(AppConfig.discover(root=tmp_path), host="0.0.0.0")


def test_admin_rejects_insecure_remote_provider_url(tmp_path):
    app = AdminApp(AppConfig.discover(root=tmp_path))

    with pytest.raises(ValueError, match="HTTPS"):
        app.save_settings({"base_url": "http://example.com/v1/chat/completions"})

    saved = app.save_settings({"base_url": "http://127.0.0.1:9000/v1/chat/completions"})
    assert saved["base_url"] == "http://127.0.0.1:9000/v1/chat/completions"


def test_admin_ui_escapes_event_arguments_and_only_links_to_hh():
    assert "function jsArg(s)" in INDEX_HTML
    assert "function safeHHUrl(raw)" in INDEX_HTML
    assert "function safeVerdict(v)" in INDEX_HTML
    assert "function safeInt(v," in INDEX_HTML
    assert "String.fromCharCode(0x2028)" in INDEX_HTML
    assert "String.fromCharCode(0x2029)" in INDEX_HTML
    assert "\u2028" not in INDEX_HTML
    assert "\u2029" not in INDEX_HTML
    assert 'href="${r.url}"' not in INDEX_HTML
    assert 'href="${f.url}"' not in INDEX_HTML
    assert '<span class="pill ${r.verdict}">' not in INDEX_HTML
    assert '<b>${t.label}</b>' not in INDEX_HTML
    assert 'onclick="genLetter(\'${' not in INDEX_HTML
