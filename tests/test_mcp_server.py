import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from applypilot.chat_journal import ChatJournal
from applypilot.config import AppConfig
from applypilot.mcp_server import Service, main
from applypilot.storage import Store


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    root = tmp_path / "repo"
    root.mkdir()
    data = root / "private" / "data"
    data.mkdir(parents=True)
    return AppConfig(root, data, root / "private" / "config" / "profile.toml")


@pytest.fixture
def service(config: AppConfig, monkeypatch) -> Service:
    service = Service(config)
    profile = {"account": "default"}
    monkeypatch.setattr(
        service,
        "_track",
        lambda track: (config, {"resume": "Resume"}, {"resume_key": "ai"}, profile),
    )
    return service


def _store(config: AppConfig, *, status: str = "NOT_STARTED") -> Store:
    store = Store(config.db_path)
    store.start_campaign("campaign-1", "default", "ai", {}, source="hh.ru")
    store.save_campaign_items(
        "campaign-1", "default", "ai",
        [{
            "id": "123", "name": "AI engineer", "company": "Example",
            "description": "Build Python agents", "description_status": "ok",
            "score": 90, "score_decision": "pass", "filter_status": "pass",
            "url": "https://hh.ru/vacancy/123", "secret": "must not leak",
        }],
        status="ok", snapshot_path="fixture.json", source="hh.ru",
    )
    if status == "UNKNOWN":
        store.record(
            {"id": "123", "name": "AI engineer", "company": "Example"},
            "unknown", account="default",
        )
    return store


def test_capabilities_are_fail_closed(service: Service):
    result = service.capabilities()
    assert result["transport"] == "stdio"
    assert result["live_apply_enabled"] is False
    assert result["chat_send_enabled"] is False
    assert result["writes_require_confirm"] is True
    assert "chat_send" in result["write_tools"]


def test_scan_requires_confirmation_bounds_and_safe_scan_id(service: Service, monkeypatch):
    calls = []
    monkeypatch.setattr(service, "_json_command", lambda argv: calls.append(argv) or {
        "ok": True, "exit_code": 0, "timed_out": False, "retry": False,
        "data": {"status": "scan_complete", "scan_id": "a" * 32, "counts": {"hh.ru": 1}},
    })
    with pytest.raises(ValueError):
        service.scan("ai")
    with pytest.raises(ValueError):
        service.scan("ai", days=0, confirm=True)
    with pytest.raises(ValueError):
        service.scan("ai", scan_id="../profile", confirm=True)
    assert calls == []
    result = service.scan("ai", confirm=True)
    assert result["ok"] is True
    assert "--track" in calls[0] and "--json" in calls[0]


def test_partial_scan_is_not_reported_as_complete(service: Service, monkeypatch):
    monkeypatch.setattr(service, "_json_command", lambda argv: {
        "ok": False, "exit_code": 2, "timed_out": False, "retry": False,
        "data": {"status": "scan_partial", "scan_id": "b" * 32, "counts": {}},
    })
    result = service.scan("ai", confirm=True)
    assert result["ok"] is False
    assert result["incomplete"] is True
    assert result["retry"] is False


def test_review_decision_maps_flags_and_requires_confirmation(service: Service, monkeypatch):
    calls = []
    monkeypatch.setattr(service, "_json_command", lambda argv: calls.append(argv) or {
        "ok": True, "exit_code": 0, "timed_out": False, "retry": False,
        "data": {"actions": {"updated": {"hh.ru:1": "APPROVED"}, "blocked": {}}},
    })
    with pytest.raises(ValueError):
        service.review_decide("ai", ["1"])
    result = service.review_decide("ai", ["hh.ru:1", "2"], confirm=True)
    assert result["ok"] is True
    assert calls[0].count("--approve") == 2
    assert result["state"] == "APPROVE"
    with pytest.raises(ValueError):
        service.review_decide("ai", ["1"], state="PUSH", confirm=True)


def test_review_blocked_action_is_not_reported_as_success(service: Service, monkeypatch):
    monkeypatch.setattr(service, "_json_command", lambda argv: {
        "ok": True, "exit_code": 0, "timed_out": False, "retry": False,
        "data": {"actions": {"updated": {}, "blocked": {"1": "hard requirement"}}},
    })
    result = service.review_decide("ai", ["1"], confirm=True)
    assert result["ok"] is False
    assert result["blocked"] == {"1": "hard requirement"}


def test_apply_is_exact_and_requires_startup_optin(service: Service, monkeypatch):
    calls = []
    monkeypatch.setattr(service, "_json_command", lambda argv, timeout=None: calls.append(
        (argv, timeout)
    ) or {
        "ok": True, "exit_code": 0, "timed_out": False, "retry": False,
        "data": {"counts": {"confirmed": 1, "unknown": 0}},
    })
    with pytest.raises(ValueError):
        service.apply("ai", ["123"], confirm=True)
    service.enable_apply = True
    with pytest.raises(ValueError):
        service.apply("ai", ["123"])
    with pytest.raises(ValueError):
        service.apply("ai", [], confirm=True)
    result = service.apply("ai", ["123"], confirm=True)
    argv, timeout = calls[-1]
    assert "--run" in argv and "--id" in argv and "123" in argv
    assert timeout >= 1800
    assert result["submission_confirmed"] is True


def test_apply_dry_run_never_uses_live_mode(service: Service, monkeypatch):
    calls = []
    monkeypatch.setattr(service, "_json_command", lambda argv: calls.append(argv) or {
        "ok": True, "exit_code": 0, "timed_out": False, "retry": False,
        "data": {"package_count": 1},
    })
    result = service.apply_dry_run("ai", ["123"])
    assert "--dry-run" in calls[0] and "--run" not in calls[0]
    assert result["live"] is False
    assert result["submission_confirmed"] is False


def test_timeout_is_uncertain_and_not_retried(service: Service, monkeypatch):
    service.enable_apply = True
    monkeypatch.setattr(service, "_json_command", lambda argv, timeout=None: {
        "ok": False, "exit_code": None, "timed_out": True, "retry": False, "data": None,
    })
    result = service.apply("ai", ["123"], confirm=True)
    assert result["submission_confirmed"] is False
    assert result["retry"] is False
    assert "uncertain" in result["reason"].lower()


def test_subprocess_timeout_kills_group_and_does_not_return_output(service: Service, monkeypatch):
    import signal

    calls = []

    class Process:
        pid = 12345
        returncode = None
        attempts = 0

        def communicate(self, timeout=None):
            self.attempts += 1
            if self.attempts <= 2:
                raise subprocess.TimeoutExpired("private command", timeout or 1)
            return ("private stdout", "private stderr")

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(
        "applypilot.mcp_server.os.killpg", lambda pid, sig: calls.append(sig)
    )
    result = service._execute(["doctor"], timeout=1)
    assert calls == [signal.SIGTERM, signal.SIGKILL]
    assert result["timed_out"] is True
    json_result = service._json_command(["doctor"], timeout=1)
    assert json_result["timed_out"] is True
    assert json_result["retry"] is False
    assert "stdout" not in json_result


def test_status_and_queue_delegate_to_existing_cli(service: Service, monkeypatch):
    calls = []

    def fake(argv, timeout=None):
        calls.append(argv)
        return {
            "ok": True, "exit_code": 0, "timed_out": False, "retry": False,
            "data": {"track": "ai", "sources": {}},
        }

    monkeypatch.setattr(service, "_json_command", fake)
    assert service.status("ai", scan="a" * 32)["ok"] is True
    assert "--status" not in calls[0] and calls[0][0] == "status"
    assert service.queue("ai", view="ready")["ok"] is True
    assert calls[1][0] == "review" and "--view" in calls[1]
    with pytest.raises(ValueError):
        service.queue("ai", view="external_ats")


def test_vacancy_detail_uses_public_allowlist(config: AppConfig, service: Service):
    _store(config)
    result = service.vacancy_detail("ai", "123")
    assert result["found"] is True
    assert result["vacancy"]["description"] == "Build Python agents"
    assert "secret" not in result["vacancy"]


def test_unknown_application_status_is_quarantined(config: AppConfig, service: Service):
    _store(config, status="UNKNOWN")
    result = service.application_status("ai", "123")
    assert result["found"] is True
    assert result["status"] == "UNKNOWN"
    assert result["retry"] is False


def test_scans_lists_local_execution_ids(config: AppConfig, service: Service):
    Store(config.db_path).create_execution("e" * 32, {"track": "ai"})
    result = service.scans()
    assert result["count"] == 1
    assert result["scans"][0]["scan_id"] == "e" * 32


def test_letter_preview_is_local_and_never_sends(config: AppConfig, service: Service, monkeypatch):
    _store(config)
    monkeypatch.setattr(service, "_track", lambda track: (
        config, {"resume": "Resume"}, {"resume_key": "ai"},
        {"account": "default", "cover_letter": {"mode": "off"}},
    ))
    result = service.letter_preview("ai", "123")
    assert result["source"] == "disabled"
    assert result["sent"] is False


def test_chat_dry_run_reports_journal_without_sending(service: Service, monkeypatch):
    key = "a" * 64
    journal = ChatJournal(service.journal_path)
    assert journal.reserve("default", "10", "20", key, "Reply")
    monkeypatch.setattr("applypilot.hh_chat.preview_chat", lambda *args: {
        "thread_id": "10", "vacancy_id": "20", "company": "Company",
        "complete": True, "latest_inbound_key": key, "message_count": 1,
    })
    result = service.chat_dry_run("10", "20", "Company", key, "Reply")
    assert result["would_send"] is False
    assert result["journal_state"] == "unknown"
    assert result["sent"] is False and result["retry"] is False


def test_chat_dry_run_before_first_reply_does_not_create_journal(service: Service, monkeypatch):
    key = "b" * 64
    monkeypatch.setattr("applypilot.hh_chat.preview_chat", lambda *args: {
        "thread_id": "10", "vacancy_id": "20", "company": "Company",
        "complete": True, "latest_inbound_key": key, "message_count": 1,
    })
    result = service.chat_dry_run("10", "20", "Company", key, "Reply")
    assert result["would_send"] is True
    assert result["journal_state"] is None
    assert not service.journal_path.exists()


def test_chat_send_requires_optin_and_confirmation(service: Service, monkeypatch):
    key = "a" * 64
    calls = []
    monkeypatch.setattr("applypilot.hh_chat.send_chat", lambda *args: calls.append(args) or {
        "status": "sent", "sent": True, "retry": False,
    })
    with pytest.raises(ValueError):
        service.chat_send("10", "20", "Company", key, "Reply", confirm=True)
    service.enable_chat_send = True
    with pytest.raises(ValueError):
        service.chat_send("10", "20", "Company", key, "Reply")
    result = service.chat_send("10", "20", "Company", key, "Reply", confirm=True)
    assert result["submission_confirmed"] is True
    assert result["retry"] is False
    assert len(calls) == 1


def test_one_shot_call_is_json_and_rejects_unknown_arguments(tmp_path, capsys):
    assert main(["--root", str(tmp_path), "--call", "capabilities"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["transport"] == "stdio"
    assert main([
        "--root", str(tmp_path), "--call", "capabilities",
        "--arguments", '{"secret": true}',
    ]) == 2
    assert "unexpected arguments" in capsys.readouterr().out


def test_tools_expose_side_effect_annotations(service: Service):
    pytest.importorskip("mcp")
    from applypilot.mcp_server import create_server

    tools = {tool.name: tool for tool in asyncio.run(create_server(service).list_tools())}
    assert tools["apply"].annotations.readOnlyHint is False
    assert tools["chat_send"].annotations.destructiveHint is True
    assert tools["apply_dry_run"].annotations.readOnlyHint is False
    assert tools["apply_dry_run"].annotations.destructiveHint is False
    assert tools["queue"].annotations.readOnlyHint is True
