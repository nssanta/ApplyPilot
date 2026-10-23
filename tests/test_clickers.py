from __future__ import annotations

from pathlib import Path

from applypilot.clickers import (
    CLICKERS,
    apply_with_clicker,
    clicker_specs,
    inspect_page,
    preflight,
)


def test_registry_exposes_guarded_habr_and_getmatch_verified_channels() -> None:
    assert set(CLICKERS) == {
        "career.habr.com", "geekjob.ru", "yandex.ru/jobs", "getmatch.ru",
    }
    specs = {spec["source"]: spec for spec in clicker_specs()}
    habr = specs["career.habr.com"]
    assert habr["submit_verified"] is True
    assert habr["apply_status"] == "verified_native_one_click"
    assert habr["selectors"] == [
        ".create-vacancy-response__apply button[type='submit']",
    ]
    assert specs["getmatch.ru"]["submit_verified"] is True
    assert specs["getmatch.ru"]["apply_status"] == "verified_telegram_callback"
    assert specs["getmatch.ru"]["selectors"] == []
    for source in ("geekjob.ru", "yandex.ru/jobs"):
        assert specs[source]["submit_verified"] is False
        assert specs[source]["apply_status"] == "needs_live_validation"
        assert specs[source]["selectors"] == []


def test_preflight_requires_local_profile(tmp_path: Path) -> None:
    missing = preflight("career.habr.com", tmp_path / "missing")
    assert missing["status"] == "profile_missing"
    assert missing["ready_to_submit"] is False

    profile = tmp_path / "profile"
    profile.mkdir()
    ready = preflight("career.habr.com", profile)
    assert ready["status"] == "ready_for_inspection"
    assert ready["ready_to_submit"] is False


def test_getmatch_preflight_uses_telegram_session() -> None:
    result = preflight("getmatch.ru", None)
    assert result["status"] == "telegram_session_required"
    assert result["ready_to_submit"] is False


class _FakeElement:
    def __init__(self, tag: str, text: str = "", href: str = "",
                 input_type: str = "", aria: str = "") -> None:
        self.tag = tag
        self.text = text
        self.href = href
        self.input_type = input_type
        self.aria = aria

    def evaluate(self, script: str) -> str:
        return self.tag

    def inner_text(self, timeout: int = 500) -> str:
        return self.text

    def get_attribute(self, name: str) -> str | None:
        return {
            "href": self.href,
            "type": self.input_type,
            "aria-label": self.aria,
        }.get(name)


class _FakeLocator:
    def __init__(self, items: list[_FakeElement]) -> None:
        self.items = items

    def count(self) -> int:
        return len(self.items)

    def nth(self, index: int) -> _FakeElement:
        return self.items[index]


class _FakePage:
    def __init__(self, items: list[_FakeElement]) -> None:
        self.items = items
        self.clicks = 0
        self.submits = 0

    def locator(self, selector: str) -> _FakeLocator:
        assert selector == "button, a, input, textarea, select"
        return _FakeLocator(self.items)


def test_inspect_page_never_clicks_or_submits() -> None:
    page = _FakePage([
        _FakeElement("button", "Откликнуться"),
        _FakeElement("a", "Вакансия", href="https://example.com/vacancy/1"),
        _FakeElement("input", input_type="file", aria="resume"),
    ])
    result = inspect_page(page)
    assert result["status"] == "inspected"
    assert result["submitted"] is False
    assert result["clicked"] is False
    assert result["count"] == 3
    assert result["elements"][0]["text"] == "Откликнуться"
    assert result["elements"][1]["href"].endswith("/vacancy/1")
    assert page.clicks == 0 and page.submits == 0


def test_apply_with_clicker_is_explicitly_blocked() -> None:
    outcome = apply_with_clicker("career.habr.com", {"id": "1"})
    assert outcome.status == "needs_live_validation"
    assert "dispatch-apply --allow-live --enable-habr" in outcome.note
