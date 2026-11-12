"""HH chat browser adapter for an authenticated, already prepared session.

The adapter reads real application-chat threads from the observed iframe UI.
It never guesses a private API endpoint and only sends when the caller supplies
the exact thread, vacancy, employer and latest inbound fingerprint.
"""

from __future__ import annotations

import logging
import os
import re
import time
from contextlib import contextmanager
from itertools import pairwise
from urllib.parse import urlparse

from .chat_safety import latest_inbound_key, numeric_id, validate_binding
from .session import classify_session_page

LOGGER = logging.getLogger(__name__)
_MAX_VISIBLE_MESSAGES = 400
_MAX_RETURNED_MESSAGES = 100

# HH currently emits Magritte-style classes on the iframe message wrapper and
# its descendants.  Older builds used BEM-style names.  Only positive markers
# are accepted; a message with both kinds or no marker remains ``unknown``.
_OUTBOUND_MARKERS = {
    "magritte-outgoing",
    "magritte-tail-outgoing",
    "message-my",
    "message_my",
    "message-text-outgoing",
    "chat-bubble_outgoing",
}
_INBOUND_MARKERS = {
    "magritte-with-avatar",
    "chat-bubble-avatar",
    "chat-bubble-label",
    "message-with-avatar-space",
    "chat-bubble_bot",
    "chat-bubble_with-left-tail",
}


class ChatUnavailable(RuntimeError):
    pass


def _class_bases(classes: str) -> set[str]:
    """Strip Magritte/BEM hash suffixes from a concatenated class attribute."""
    result: set[str] = set()
    for token in str(classes or "").split():
        if not token:
            continue
        result.add(re.split(r"___|--", token, maxsplit=1)[0])
    return result


def message_direction(classes: str) -> str:
    """Classify one observed message without guessing from order or text.

    Outgoing and inbound markers must be mutually exclusive.  Unknown/ambiguous
    classes stay ``unknown`` so ``latest_inbound_key`` remains ``None`` and the
    reply path stays blocked.
    """
    bases = _class_bases(classes)
    outbound = bool(bases & _OUTBOUND_MARKERS)
    inbound = bool(bases & _INBOUND_MARKERS)
    if outbound and not inbound:
        return "outbound"
    if inbound and not outbound:
        return "inbound"
    return "unknown"


def clean_company(value: str) -> str:
    return " ".join(str(value or "").split())


def frame_thread(url: str) -> str | None:
    parsed = urlparse(url)
    match = re.fullmatch(r"/chat/([0-9]{1,20})", parsed.path.rstrip("/"))
    if parsed.scheme != "https" or parsed.hostname != "chatik.hh.ru" or not match:
        return None
    return match.group(1)


def vacancy_from_url(url: str) -> str:
    parsed = urlparse(url)
    match = re.fullmatch(r"/vacancy/([0-9]{1,20})", parsed.path.rstrip("/"))
    if parsed.scheme != "https" or parsed.hostname != "hh.ru" or not match:
        raise ChatUnavailable("Chat vacancy link not verified")
    return match.group(1)


@contextmanager
def browser_page(config):
    """Open the same persistent Chrome profile used by the HH apply boundary."""
    from playwright.sync_api import sync_playwright

    from .browser import launch_context
    from .direct_network import hh_proxy

    os.environ.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH", str(config.root / "private" / "browsers")
    )
    profile = config.load_profile()
    visible = os.getenv("APPLYPILOT_CHAT_VISIBLE", "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    with hh_proxy() as proxy_url, sync_playwright() as playwright:
        session = launch_context(
            playwright,
            data_dir=config.data_dir,
            state_path=config.data_dir / "hh_session.json",
            profile=profile,
            headless=not visible,
            proxy_url=proxy_url,
        )
        try:
            yield session.context.new_page()
        finally:
            session.close()


def _verify_application_page(page) -> None:
    page.goto(
        "https://hh.ru/applicant/negotiations",
        wait_until="domcontentloaded",
        timeout=20000,
    )
    auth = classify_session_page(page.url, page.locator("body").inner_text())
    if auth.status != "confirmed":
        raise ChatUnavailable("Authenticated HH application page unavailable")


def open_thread(page, vacancy_id: str | None = None, thread_id: str | None = None):
    if vacancy_id is not None:
        numeric_id(vacancy_id)
    if thread_id is not None:
        numeric_id(thread_id)
    _verify_application_page(page)
    rows = page.locator('[data-qa="negotiations-item"]')
    selected = None
    for index in range(min(rows.count(), 100)):
        row = rows.nth(index)
        link = row.locator('[data-qa="negotiations-item-vacancy"]').first
        href = link.get_attribute("href") or ""
        absolute = "https://hh.ru" + href if href.startswith("/") else href
        try:
            matches = vacancy_from_url(absolute) == vacancy_id
        except ChatUnavailable:
            matches = False
        if matches or (
            thread_id is not None
            and row.locator('[data-qa="open_chat"]').count()
        ):
            selected = row
            break
    if selected is None:
        raise ChatUnavailable("Vacancy not on current authenticated applications page")
    selected.locator('[data-qa="open_chat"]').click()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        frames = [frame for frame in page.frames if frame_thread(frame.url)]
        if len(frames) == 1:
            frame = frames[0]
            if thread_id is not None and frame_thread(frame.url) != thread_id:
                frame.locator('[data-qa="chatik-back-to-chats-button"]').click()
                walk_chat_links(frame, 100, target=thread_id)
                anchor = frame.locator(f'[data-qa="chatik-open-chat-{thread_id}"]')
                if not anchor.count():
                    raise ChatUnavailable("Thread not found in bounded virtual chat list")
                href = anchor.get_attribute("href") or ""
                if frame_thread("https://chatik.hh.ru" + href) != thread_id:
                    raise ChatUnavailable("Thread link not verified")
                anchor.click()
                frame.wait_for_url(lambda url: frame_thread(str(url)) == thread_id)
            if thread_id is not None and frame_thread(frame.url) != thread_id:
                raise ChatUnavailable("Requested chat navigation not confirmed")
            frame.locator('[data-qa="chatik-header-vacancy-link"]').wait_for(state="visible")
            return frame
        page.wait_for_timeout(100)
    raise ChatUnavailable("Verified HH chat iframe unavailable")


def read_frame(frame) -> dict:
    thread_id = frame_thread(frame.url)
    if not thread_id:
        raise ChatUnavailable("Unexpected chat origin")
    header = frame.locator('[data-qa="participant-info-title"]').first
    company = clean_company(header.inner_text())
    vacancy_id = vacancy_from_url(
        frame.locator('[data-qa="chatik-header-vacancy-link"]').first.get_attribute("href") or ""
    )
    roots = frame.locator('[data-qa^="chatik-chat-message-"]')
    messages = []
    for index in range(min(roots.count(), _MAX_VISIBLE_MESSAGES)):
        root = roots.nth(index)
        qa = root.get_attribute("data-qa") or ""
        match = re.fullmatch(r"chatik-chat-message-([0-9]+)", qa)
        if not match:
            continue  # Ignore observed nested ...-text nodes.
        classes = root.evaluate(
            '(node) => [node, ...node.querySelectorAll("[class]")]'
            '.map(n => String(n.className)).join(" ")'
        )
        direction = message_direction(classes)
        text = root.locator('[data-qa="chat-bubble-text"]').first
        value = text.inner_text() if text.count() else ""
        messages.append({"id": match.group(1), "direction": direction, "text": value})
    # DOM message order must agree with the observed ascending message IDs.
    ordered = all(
        int(left["id"]) < int(right["id"]) for left, right in pairwise(messages)
    )
    complete = (
        bool(messages)
        and ordered
        and roots.count() <= _MAX_VISIBLE_MESSAGES
        and len(messages[-1]["text"]) <= 12000
    )
    result = {
        "thread_id": thread_id,
        "vacancy_id": vacancy_id,
        "company": company,
        "complete": complete,
        "messages": messages[-_MAX_RETURNED_MESSAGES:],
    }
    result["latest_inbound_key"] = latest_inbound_key(result["messages"], complete)
    return result


def read_stable(frame) -> dict:
    frame.locator('[data-qa^="chatik-chat-message-"]').first.wait_for(state="visible")
    previous = read_frame(frame)
    for _ in range(5):
        frame.page.wait_for_timeout(200)
        current = read_frame(frame)
        if current == previous:
            return current
        previous = current
    previous["complete"] = False
    previous["latest_inbound_key"] = None
    return previous


def read_chat(config, thread_id: str) -> dict:
    numeric_id(thread_id)
    with browser_page(config) as page:
        return read_stable(open_thread(page, thread_id=thread_id))


def preview_chat(config, thread_id: str, vacancy_id: str, company: str,
                 inbound_key: str) -> dict:
    """Re-read and validate the exact binding without filling or clicking."""
    numeric_id(thread_id)
    numeric_id(vacancy_id)
    with browser_page(config) as page:
        frame = open_thread(page, thread_id=thread_id)
        current = read_stable(frame)
        validate_binding(
            current, thread_id, vacancy_id, clean_company(company), inbound_key
        )
        return {
            "thread_id": thread_id,
            "vacancy_id": vacancy_id,
            "company": clean_company(company),
            "complete": bool(current.get("complete")),
            "latest_inbound_key": current.get("latest_inbound_key"),
            "message_count": len(current.get("messages") or []),
        }


def list_chats(config, limit: int = 20) -> dict:
    with browser_page(config) as page:
        frame = open_first_thread(page)
        frame.locator('[data-qa="chatik-back-to-chats-button"]').click()
        result = walk_chat_links(frame, limit)
        return {
            "chats": result,
            "incomplete": True,
            "reason": "Bounded HH virtual list scroll; older chats may be absent",
        }


def walk_chat_links(frame, limit: int, target: str | None = None) -> list[dict]:
    anchors = frame.locator('[data-qa^="chatik-open-chat-"]')
    anchors.first.wait_for(state="visible")
    collected: dict[str, dict] = {}
    no_progress = 0
    for _ in range(30):
        previous_count = len(collected)
        for index in range(anchors.count()):
            anchor = anchors.nth(index)
            href = anchor.get_attribute("href") or ""
            thread_id = frame_thread("https://chatik.hh.ru" + href)
            if not thread_id:
                continue  # Actual list skeleton uses /chat/-1.
            if target == thread_id:
                return list(collected.values())
            title = anchor.locator('[data-qa="chat-cell-title"]').first
            subtitle = anchor.locator('[data-qa="chat-cell-subtitle"]').first
            collected[thread_id] = {
                "thread_id": thread_id,
                "title": title.inner_text()[:500] if title.count() else "",
                "subtitle": subtitle.inner_text()[:1000] if subtitle.count() else "",
                "has_badge": anchor.locator('[data-qa="chatik-info-badges"]').count() > 0,
            }
            if target is None and len(collected) >= limit:
                return list(collected.values())[:limit]
        no_progress = no_progress + 1 if len(collected) == previous_count else 0
        if no_progress >= 3 or not anchors.count():
            break
        moved = anchors.first.evaluate(
            """(n) => {
                for (let p=n; p; p=p.parentElement) {
                    if (p.scrollHeight > p.clientHeight+20 &&
                        /auto|scroll/.test(getComputedStyle(p).overflowY)) {
                        const before=p.scrollTop;
                        p.scrollTop += p.clientHeight * 0.8;
                        return p.scrollTop !== before;
                    }
                }
                return false;
            }"""
        )
        frame.page.wait_for_timeout(600)
        if not moved and no_progress >= 2:
            break
    return list(collected.values())[:limit]


def open_first_thread(page):
    # Select only a real existing application row, then obtain its observed chat iframe.
    _verify_application_page(page)
    buttons = page.locator('[data-qa="negotiations-item"] [data-qa="open_chat"]')
    if not buttons.count():
        raise ChatUnavailable("No application chat controls available")
    buttons.first.click()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        frames = [frame for frame in page.frames if frame_thread(frame.url)]
        if len(frames) == 1:
            frames[0].locator('[data-qa="chatik-header-vacancy-link"]').wait_for(
                state="visible"
            )
            return frames[0]
        page.wait_for_timeout(100)
    raise ChatUnavailable("Verified HH chat iframe unavailable")


def send_chat(config, journal, account: str, thread_id: str, vacancy_id: str,
              company: str, inbound_key: str, reply: str) -> dict:
    """Send one exact reply with an at-most-once journal and fresh binding."""
    numeric_id(thread_id)
    numeric_id(vacancy_id)
    normalized_company = clean_company(company)
    with browser_page(config) as page:
        frame = open_thread(page, thread_id=thread_id)
        before = read_stable(frame)
        validate_binding(before, thread_id, vacancy_id, normalized_company, inbound_key)
        if not journal.reserve(account, thread_id, vacancy_id, inbound_key, reply):
            return {
                "status": journal.status(account, thread_id, inbound_key),
                "sent": False,
                "retry": False,
                "reason": "Reply already reserved; no duplicate send",
            }
        clicked = False
        try:
            composer = frame.locator(
                '[data-qa="chatik-message-input"] [data-qa="text-input"]'
            )
            composer.fill(reply)
            current = read_stable(frame)
            validate_binding(
                current, thread_id, vacancy_id, normalized_company, inbound_key
            )
            previous_ids = {message["id"] for message in current["messages"]}
            boundary = max(int(message_id) for message_id in previous_ids)
            clicked = True  # Any click exception is uncertain, never retry automatically.
            frame.locator('[data-qa="chatik-do-send-message"]').click()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                after = read_frame(frame)
                verified = any(
                    message["id"] not in previous_ids
                    and int(message["id"]) > boundary
                    and message["direction"] == "outbound"
                    and message["text"] == reply
                    for message in after["messages"]
                )
                if (
                    verified
                    and after["thread_id"] == thread_id
                    and after["vacancy_id"] == vacancy_id
                    and after["company"] == normalized_company
                ):
                    journal.finish(account, thread_id, inbound_key, "sent")
                    return {"status": "sent", "sent": True, "retry": False}
                page.wait_for_timeout(200)
            journal.finish(account, thread_id, inbound_key, "unknown")
            return {
                "status": "unknown",
                "sent": False,
                "retry": False,
                "reason": "No new exact outgoing message confirmed; inspect thread manually",
            }
        except Exception:  # noqa: BLE001 - an uncertain send must never be retried automatically
            state = "unknown" if clicked else "stale"
            try:
                journal.finish(account, thread_id, inbound_key, state)
            except Exception:
                LOGGER.warning("Could not persist chat journal outcome", exc_info=True)
            return {
                "status": state,
                "sent": False,
                "retry": False,
                "reason": "Chat changed or browser failed; inspect thread before another action",
            }

# Patterns for the employer's latest unanswered messages.  An invitation, a
# test task or an external form always goes to the owner; a plain question can
# be answered from the private facts.
_INVITE = re.compile(
    r"созвон|позвон|звонок|собеседован|интервью|встреч|zoom|телемост|google meet|"
    r"календар|слот|удобно.{0,60}(?:время|созвон|сейчас|поговорить)|обсудить вакансию",
    re.IGNORECASE,
)
# "Не готовы пригласить вас на интервью" names an interview but declines it,
# and so does "после собеседования ... решили продолжить поиски".
_DECLINED_INVITE = re.compile(
    r"(?:не готовы|не можем|не сможем)\s+(?:\w+\s+){0,3}пригласить|продолжить поиск",
    re.IGNORECASE,
)
# "Продолжаем рассматривать кандидатов ... по итогам собеседований" is a hold:
# the employer owes the next step, not us.
_STILL_DECIDING = re.compile(
    r"продолжаем рассматривать|оста[её]тся в числе|остаетесь в числе|остаётесь в числе",
    re.IGNORECASE,
)
_TASK = re.compile(r"тестов\w*|техническ\w* задани|\bтз\b|домашн\w* задани", re.IGNORECASE)
_FORM = re.compile(r"forms\.gle|forms\.yandex|docs\.google\.com/forms|t\.me/\w*bot|анкет", re.IGNORECASE)
_HANDED_OVER = re.compile(
    r"передал\w* (?:\w+ )?работодател|представитель работодателя ознакомится|"
    r"рассмотрим ваше резюме|мы свяжемся с вами|свяжемся с вами|спасибо за ответы",
    re.IGNORECASE,
)
_REJECT = re.compile(
    r"к сожалению|не готовы предложить|не можем предложить|выбрали другого|отказ|"
    r"не подходит|приняли решение не|"
    r"закрыли (?:эту |данную )?(?:позицию|вакансию)|(?:позиция|вакансия) (?:уже )?закрыта",
    re.IGNORECASE,
)


def classify_thread(messages: list[dict]) -> dict:
    """Who owes the next message and what the employer asked for.

    The badge on the chat list disappears once a chat is opened, so it cannot
    tell whether a reply is owed.  This looks at the messages themselves: the
    thread waits for us when the employer wrote after our last message.
    """
    known = [m for m in messages if m.get("direction") in {"inbound", "outbound"}]
    if not known:
        return {"waiting_for_us": False, "kind": "empty", "needs_owner": False, "unanswered": ""}
    last_out = max((i for i, m in enumerate(known) if m["direction"] == "outbound"), default=-1)
    unanswered = "\n".join(str(m.get("text") or "") for m in known[last_out + 1:]).strip()
    waiting = known[-1]["direction"] == "inbound"
    # Bot screenings post many questions in a row; the last message tells
    # where the thread stands now.
    latest = str(known[-1].get("text") or "")
    if not waiting:
        kind = "answered"
    elif _DECLINED_INVITE.search(latest):
        kind = "rejection"
    elif _STILL_DECIDING.search(latest):
        kind = "handed_over"
    elif _INVITE.search(latest):
        kind = "invite"
    elif _HANDED_OVER.search(latest):
        kind = "handed_over"
    elif _REJECT.search(latest):
        kind = "rejection"
    elif _FORM.search(latest):
        kind = "form"
    elif _TASK.search(latest):
        kind = "test_task"
    elif "?" in latest:
        kind = "question"
    else:
        kind = "info"
    return {
        "waiting_for_us": waiting and kind not in {"rejection", "handed_over"},
        "kind": kind,
        "needs_owner": kind in {"invite", "test_task", "form"},
        "unanswered": unanswered[-1500:],
    }


def triage_chats(config, limit: int = 30) -> dict:
    """Open each recent chat once and report the ones that wait for a reply."""
    rows: list[dict] = []
    with browser_page(config) as page:
        frame = open_first_thread(page)
        frame.locator('[data-qa="chatik-back-to-chats-button"]').click()
        chats = walk_chat_links(frame, limit)
        for chat in chats:
            thread_id = chat["thread_id"]
            try:
                anchor = frame.locator(f'[data-qa="chatik-open-chat-{thread_id}"]')
                if not anchor.count():
                    walk_chat_links(frame, 200, target=thread_id)
                if not anchor.count():
                    raise ChatUnavailable("thread is not in the loaded chat list")
                anchor.click()
                frame.wait_for_url(lambda url, tid=thread_id: frame_thread(str(url)) == tid)
                data = read_stable(frame)
                rows.append({
                    **chat, "vacancy_id": data.get("vacancy_id"), "company": data.get("company"),
                    **classify_thread(list(data.get("messages") or [])),
                })
                frame.locator('[data-qa="chatik-back-to-chats-button"]').click()
            except Exception as exc:  # noqa: BLE001 - one broken chat must not hide the rest
                rows.append({**chat, "error": str(exc)[:300]})
                try:
                    frame.locator('[data-qa="chatik-back-to-chats-button"]').click(timeout=3000)
                except Exception as back_exc:  # noqa: BLE001
                    LOGGER.debug("chat list return failed: %s", back_exc)
    rows.sort(key=lambda row: (not row.get("needs_owner"), not row.get("waiting_for_us"),
                               "error" not in row))
    return {
        "chats": rows,
        "waiting_for_us": sum(1 for row in rows if row.get("waiting_for_us")),
        "needs_owner": sum(1 for row in rows if row.get("needs_owner")),
        "errors": sum(1 for row in rows if "error" in row),
        "checked": len(rows),
        "note": "Only the newest chats are listed; older ones may be absent.",
    }



_CHAT_URL = "https://chatik.hh.ru/chat/{thread_id}"
_OPTIONS_JS = """() => [...document.querySelectorAll('button')]
    .filter(e => e.offsetParent !== null && !e.getAttribute('data-qa'))
    .map(e => (e.innerText || '').trim()).filter(Boolean)"""


def _bot_state(page) -> dict:
    """Latest robot question and its quick-reply buttons on a chatik page."""
    body = page.locator("body").inner_text()
    options = page.evaluate(_OPTIONS_JS)
    tail = body.rsplit("Робот-рекрутер", 1)[-1] if "Робот-рекрутер" in body else ""
    lines = [line.strip() for line in tail.splitlines() if line.strip()]
    question = next((line for line in lines if line not in options and not re.fullmatch(r"\d{1,2}:\d{2}", line)), "")
    return {"question": question, "options": options}


def chat_options(config, thread_id: str) -> dict:
    """Read the screening bot's latest question and its buttons; no clicks."""
    numeric_id(thread_id)
    with browser_page(config) as page:
        page.goto(_CHAT_URL.format(thread_id=thread_id), wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2000)
        return {"thread_id": thread_id, **_bot_state(page)}


def chat_choose(config, thread_id: str, question: str, option: str) -> dict:
    """Click one exact quick-reply button when the bot still asks ``question``.

    The click happens only if the latest robot question matches and exactly one
    visible button carries ``option``. Afterwards the page is reloaded: success
    means the question moved on or the bot no longer offers the same buttons.
    """
    numeric_id(thread_id)
    with browser_page(config) as page:
        page.goto(_CHAT_URL.format(thread_id=thread_id), wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2000)
        before = _bot_state(page)
        if " ".join(before["question"].split()) != " ".join(str(question).split()):
            return {"status": "question_changed", "clicked": False, **before}
        buttons = page.get_by_role("button", name=option, exact=True)
        if buttons.count() != 1 or option not in before["options"]:
            return {"status": "option_missing", "clicked": False, **before}
        buttons.first.click(timeout=15000)
        page.wait_for_timeout(2500)
        page.reload(wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2000)
        after = _bot_state(page)
        moved = after["question"] != before["question"] or after["options"] != before["options"]
        return {"status": "success" if moved else "unknown_after_click", "clicked": True,
                "before": before, "after": after}
