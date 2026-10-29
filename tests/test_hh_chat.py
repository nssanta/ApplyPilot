from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

from applypilot import hh_chat
from applypilot.chat_journal import ChatJournal
from applypilot.chat_safety import latest_inbound_key


def thread(messages=None, **changes):
    result = {
        "thread_id": "10", "vacancy_id": "20", "company": "Company",
        "complete": True,
        "messages": messages or [
            {"id": "100", "direction": "inbound", "text": "Question"}
        ],
    }
    result.update(changes)
    result["latest_inbound_key"] = latest_inbound_key(result["messages"], result["complete"])
    return result


@pytest.fixture
def browser(monkeypatch):
    state = SimpleNamespace(clicks=0, fills=[], fail_click=False)

    class Control:
        def fill(self, text):
            state.fills.append(text)

        def click(self):
            state.clicks += 1
            if state.fail_click:
                raise RuntimeError("uncertain transport after click")

    frame = SimpleNamespace(locator=lambda _: Control())
    page = SimpleNamespace(wait_for_timeout=lambda _: None)

    @contextmanager
    def open_page(_):
        yield page

    monkeypatch.setattr(hh_chat, "browser_page", open_page)
    monkeypatch.setattr(hh_chat, "open_thread", lambda *_a, **_k: frame)
    before = thread()
    monkeypatch.setattr(hh_chat, "read_stable", lambda _: deepcopy(before))
    tick = iter(range(1000))
    monkeypatch.setattr(hh_chat.time, "monotonic", lambda: next(tick))
    state.before = before
    state.key = before["latest_inbound_key"]
    return state


def send(journal, browser):
    return hh_chat.send_chat(
        None, journal, "account", "10", "20", "Company", browser.key, "Reply"
    )


@pytest.mark.parametrize("change", [
    {"thread_id": "11"}, {"vacancy_id": "21"}, {"company": "Other"},
    {"messages": [{"id": "101", "direction": "inbound", "text": "New question"}]},
])
def test_stale_after_fill_never_clicks(tmp_path, monkeypatch, browser, change):
    journal = ChatJournal(tmp_path / "chat.sqlite")
    changed = thread(**change)
    reads = iter([browser.before, changed])
    monkeypatch.setattr(hh_chat, "read_stable", lambda _: next(reads))
    result = send(journal, browser)
    assert result["status"] == "stale" and result["retry"] is False
    assert browser.fills == ["Reply"] and browser.clicks == 0
    assert journal.status("account", "10", browser.key) == "stale"


def test_ambiguous_click_persists_unknown_and_restart_blocks_duplicate(tmp_path, browser):
    path = tmp_path / "chat.sqlite"
    browser.fail_click = True
    result = send(ChatJournal(path), browser)
    assert result["status"] == "unknown" and result["sent"] is False
    assert browser.clicks == 1
    result = send(ChatJournal(path), browser)
    assert result["status"] == "unknown" and result["retry"] is False
    assert browser.clicks == 1 and browser.fills == ["Reply"]


@pytest.mark.parametrize("after", [
    thread([
        {"id": "99", "direction": "outbound", "text": "Reply"},
        {"id": "100", "direction": "inbound", "text": "Question"},
    ]),
    thread([{"id": "99", "direction": "outbound", "text": "Reply"}]),
    thread([{"id": "101", "direction": "outbound", "text": "Reply"}], thread_id="11"),
    thread([{"id": "101", "direction": "outbound", "text": "Reply"}], vacancy_id="21"),
    thread([{"id": "101", "direction": "outbound", "text": "Reply"}], company="Other"),
    thread([{"id": "101", "direction": "inbound", "text": "Reply"}]),
    thread([{"id": "101", "direction": "outbound", "text": "Reply changed"}]),
])
def test_old_or_wrong_recipient_message_never_confirms(tmp_path, monkeypatch, browser, after):
    monkeypatch.setattr(hh_chat, "read_frame", lambda _: deepcopy(after))
    journal = ChatJournal(tmp_path / "chat.sqlite")
    result = send(journal, browser)
    assert result["status"] == "unknown" and result["sent"] is False
    assert result["retry"] is False and browser.clicks == 1
    assert journal.status("account", "10", browser.key) == "unknown"


def test_exact_new_outbound_and_recipient_confirms(tmp_path, monkeypatch, browser):
    after = thread([{"id": "101", "direction": "outbound", "text": "Reply"}])
    monkeypatch.setattr(hh_chat, "read_frame", lambda _: after)
    journal = ChatJournal(tmp_path / "chat.sqlite")
    result = send(journal, browser)
    assert result == {"status": "sent", "sent": True, "retry": False}
    assert journal.status("account", "10", browser.key) == "sent"


def test_wrong_recipient_before_fill_does_not_reserve(tmp_path, monkeypatch, browser):
    monkeypatch.setattr(hh_chat, "read_stable", lambda _: thread(company="Other"))
    journal = ChatJournal(tmp_path / "chat.sqlite")
    with pytest.raises(ValueError):
        send(journal, browser)
    assert browser.clicks == 0 and browser.fills == []
    assert journal.status("account", "10", browser.key) is None


def test_dry_run_validates_without_fill_click_or_reserve(tmp_path, monkeypatch, browser):
    result = hh_chat.preview_chat(None, "10", "20", "Company", browser.key)
    assert result["thread_id"] == "10"
    assert result["latest_inbound_key"] == browser.key
    assert browser.fills == [] and browser.clicks == 0


def test_dry_run_rejects_changed_binding(monkeypatch, browser):
    monkeypatch.setattr(hh_chat, "read_stable", lambda _: thread(company="Other"))
    with pytest.raises(ValueError):
        hh_chat.preview_chat(None, "10", "20", "Company", browser.key)


@pytest.mark.parametrize("url", [
    "https://evil.example/chat/10",
    "https://chatik.hh.ru.evil/chat/10",
    "http://chatik.hh.ru/chat/10",
    "https://chatik.hh.ru/chat/-1",
    "https://chatik.hh.ru/chat/10/evil",
])
def test_exact_chat_origin(url):
    assert hh_chat.frame_thread(url) is None


def test_virtual_scroll_is_bounded():
    class EmptyText:
        first = None

        def __init__(self):
            self.first = self

        def count(self):
            return 0

    class Anchor:
        def get_attribute(self, _):
            return "/chat/10"

        def locator(self, _):
            return EmptyText()

        def wait_for(self, **_):
            pass

        def evaluate(self, _):
            scrolls.append(1)
            return True

    class Anchors:
        first = Anchor()

        def count(self):
            return 1

        def nth(self, _):
            return self.first

    scrolls = []
    frame = SimpleNamespace(
        locator=lambda _: Anchors(),
        page=SimpleNamespace(wait_for_timeout=lambda _: None),
    )
    result = hh_chat.walk_chat_links(frame, 20)
    assert len(result) == 1 and result[0]["thread_id"] == "10"
    assert len(scrolls) <= 3
    assert hh_chat.walk_chat_links(frame, 1)[0]["thread_id"] == "10"


@pytest.mark.parametrize("href,navigated", [
    ("/chat/11", "https://chatik.hh.ru/chat/10"),
    ("https://evil.example/chat/10", "https://chatik.hh.ru/chat/10"),
    ("/chat/10", "https://chatik.hh.ru/chat/11"),
])
def test_open_thread_does_not_return_wrong_navigation(monkeypatch, href, navigated):
    class Node:
        first = None

        def __init__(self, kind="normal"):
            self.first = self
            self.kind = kind

        def count(self):
            return 1

        def nth(self, _):
            return self

        def inner_text(self):
            return "Мои отклики Резюме"

        def get_attribute(self, _):
            return href if self.kind == "anchor" else "/vacancy/20"

        def locator(self, selector):
            return Node("anchor" if "chatik-open-chat" in selector else "normal")

        def click(self):
            if self.kind == "anchor":
                frame.url = navigated

        def wait_for(self, **_):
            pass

    frame = SimpleNamespace(
        url="https://chatik.hh.ru/chat/11",
        locator=lambda s: Node("anchor" if "chatik-open-chat" in s else "normal"),
        wait_for_url=lambda _: None,
    )
    page = SimpleNamespace(
        url="https://hh.ru/applicant/negotiations",
        frames=[frame],
        locator=lambda _: Node(),
        goto=lambda *_a, **_k: None,
        wait_for_timeout=lambda _: None,
    )
    monkeypatch.setattr(hh_chat, "walk_chat_links", lambda *_a, **_k: [])
    with pytest.raises(hh_chat.ChatUnavailable):
        hh_chat.open_thread(page, thread_id="10")
