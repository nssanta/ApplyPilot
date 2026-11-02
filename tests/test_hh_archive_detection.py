"""HH archival notices must not be mistaken for a missing submit button."""

from applypilot.autoapply import _is_archived_vacancy, apply_one


class FakeBody:
    def __init__(self, text: str):
        self.text = text

    def inner_text(self):
        return self.text


class FakePage:
    url = "https://hh.ru/vacancy/123456789"

    def __init__(self, title: str, body: str):
        self._title = title
        self._body = body
        self.clicked = False

    def goto(self, url: str, **kwargs):
        self.url = url

    def wait_for_timeout(self, ms: int):
        pass

    def title(self):
        return self._title

    def locator(self, selector: str):
        if selector == "body":
            return FakeBody(self._body)
        raise AssertionError("Archive must be rejected before response controls are queried")


def test_archived_vacancy_is_classified_before_click():
    page = FakePage(
        "Вакансия Middle+/Senior Python Backend Developer (вакансия в архиве c 1 января 2020)",
        "Информация о компании и требования",
    )
    result = apply_one(page, {"url": page.url}, "AI/LLM Engineer")
    assert result.status == "needs_manual"
    assert "archived or closed" in result.note


def test_archive_banner_in_body_is_detected():
    page = FakePage("Python Developer", "Эта вакансия в архиве, посмотрите похожие")
    result = apply_one(page, {"url": page.url}, "AI/LLM Engineer")
    assert result.status == "needs_manual"
    assert "archived or closed" in result.note


def test_incidental_archive_reference_is_not_a_vacancy_closure():
    assert not _is_archived_vacancy("Мы храним архив старых технических документов")
    assert not _is_archived_vacancy("Откликайтесь на открытую вакансию Python разработчика")
