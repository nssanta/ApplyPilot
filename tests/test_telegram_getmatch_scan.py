"""Getmatch scanner can enrich public vacancies linked from saved Telegram messages."""

from __future__ import annotations

from pathlib import Path

from applypilot.config import AppConfig
from applypilot.multisource import scan_source
from applypilot.providers.getmatch import GetMatchProvider
from applypilot.providers.registry import ProviderRegistry
from applypilot.storage import Store

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/public"


class SpyGetmatch(GetMatchProvider):
    def __init__(self) -> None:
        self.detail_urls: list[str] = []
        self.scan_filters: dict | None = None

    def discover(self, filters):
        self.scan_filters = dict(filters)
        self.detail_urls = list(filters.get("detail_urls") or [])
        # Simulate a successful authenticated transport-independent detail scan
        # using a public synthetic HTML fixture, with no network or bot clicks.
        return super().discover({"fixture_path": str(FIXTURES / "getmatch_active_llm.html")})


def test_telegram_hints_use_saved_public_ids_and_restore_bot_links(tmp_path: Path) -> None:
    config = AppConfig(
        tmp_path, tmp_path / "data", FIXTURES / "profile.toml",
        FIXTURES / "search.toml",
    )
    store = Store(config.db_path)
    store.save_telegram_messages("default", [{
        "message_id": 101,
        "chat": "@g_jobbot",
        "sent_at": "2026-10-22T10:00:00+00:00",
        "text": "Example synthetic LLM Engineer",
        "vacancy_refs": [{"source": "getmatch.ru", "id": "36887"}],
        "buttons": [{"text": "Откликнуться в боте", "kind": "callback", "url": ""}],
        "application_likely": True,
        "has_callback": True,
    }])
    provider = SpyGetmatch()
    registry = ProviderRegistry([provider])
    result = scan_source(
        config, "ai_core", config.load_profile(), config.load_search(),
        "getmatch.ru", registry=registry, telegram_hints=True,
        request_budget=3, details_limit=3,
    )
    assert provider.detail_urls == ["https://getmatch.ru/vacancies/36887"]
    assert result["items"] == 1
    assert result["telegram_links"] == 1
    rows = store.list_campaign_vacancies("default", "ai_core", "all", 10, source="getmatch.ru")
    assert len(rows) == 1
    assert rows[0]["telegram_message_id"] == 101
    assert rows[0]["telegram_buttons"][0]["kind"] == "callback"
    assert store.source_attempts("default", "getmatch.ru") == []
    assert provider.scan_filters is not None
    sitemap_state = Path(provider.scan_filters["state_path"])
    telegram_state = Path(provider.scan_filters["telegram_state_path"])
    assert sitemap_state.name.startswith("sitemap_seen-")
    assert telegram_state.name.startswith("telegram_seen-")
    assert sitemap_state.parent == telegram_state.parent
    assert sitemap_state != telegram_state


def test_telegram_hints_require_explicit_opt_in(tmp_path: Path) -> None:
    config = AppConfig(
        tmp_path, tmp_path / "data", FIXTURES / "profile.toml",
        FIXTURES / "search.toml",
    )
    provider = SpyGetmatch()
    result = scan_source(
        config, "ai_core", config.load_profile(), config.load_search(),
        "getmatch.ru", registry=ProviderRegistry([provider]),
    )
    assert provider.detail_urls == []
    assert result["telegram_links"] == 0


def test_telegram_hints_prioritize_ai_titles_without_discarding_other_roles(
    tmp_path: Path,
) -> None:
    config = AppConfig(
        tmp_path, tmp_path / "data", FIXTURES / "profile.toml",
        FIXTURES / "search.toml",
    )
    store = Store(config.db_path)
    for message_id, title, vacancy_id in (
        (201, "Backend Python", "36789"),
        (200, "LLM Agent Engineer", "36887"),
    ):
        store.save_telegram_messages("default", [{
            "message_id": message_id, "chat": "@g_jobbot",
            "sent_at": f"2026-10-22T10:{message_id % 60:02}:00+00:00",
            "text": title,
            "vacancy_refs": [{"source": "getmatch.ru", "id": vacancy_id}],
            "buttons": [], "application_likely": False, "has_callback": False,
        }])
    provider = SpyGetmatch()
    scan_source(
        config, "ai_core", config.load_profile(), config.load_search(),
        "getmatch.ru", registry=ProviderRegistry([provider]),
        telegram_hints=True,
    )
    assert provider.detail_urls == [
        "https://getmatch.ru/vacancies/36887",
        "https://getmatch.ru/vacancies/36789",
    ]
