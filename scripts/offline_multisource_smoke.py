#!/usr/bin/env python3
"""Reproducible offline Multi-Source smoke on public synthetic fixtures.

No network, no browser, no real application.  It scans saved public fixtures,
persists normalized/scored rows into a temporary SQLite database, approves one
Getmatch row, and proves that the dry-run plan blocks live-unverified forms.

The defaults work in a clean public checkout.  The owner may point the smoke at
private real fixtures/config through APPLYPILOT_SMOKE_FIXTURES,
APPLYPILOT_SMOKE_PROFILE and APPLYPILOT_SMOKE_SEARCH without committing them.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from applypilot.config import AppConfig, effective_search
from applypilot.multisource import queue_plan, scan_sources
from applypilot.storage import Store

SOURCES = ["career.habr.com", "geekjob.ru", "getmatch.ru", "yandex.ru/jobs", "dreamjob.ru"]
FIXTURES = Path(os.environ.get(
    "APPLYPILOT_SMOKE_FIXTURES", ROOT / "tests/fixtures/public",
)).resolve()
PROFILE = Path(os.environ.get(
    "APPLYPILOT_SMOKE_PROFILE", FIXTURES / "profile.toml",
)).resolve()
SEARCH = Path(os.environ.get(
    "APPLYPILOT_SMOKE_SEARCH", FIXTURES / "search.toml",
)).resolve()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="applypilot-smoke-") as tmp:
        data_dir = Path(tmp) / "data"
        config = AppConfig(ROOT, data_dir, PROFILE, SEARCH)
        profile = config.load_profile()
        search = effective_search(config.load_search())
        result = scan_sources(
            config, "ai_core", profile, search, sources=SOURCES,
            fixture_dir=FIXTURES, limit=100,
        )
        print("sources:")
        for row in result["sources"]:
            print(f"  {row['source']}: status={row['status']} items={row.get('items', 0)} "
                  f"account={row.get('account', '')}")
        store = Store(config.db_path)
        total = 0
        top_total = 0
        review_total = 0
        for source in SOURCES:
            account = result["accounts"][source]
            rows = store.list_campaign_vacancies(account, "ai_core", "all", 100, source=source)
            top = store.list_campaign_vacancies(account, "ai_core", "top", 100, source=source)
            review = store.list_campaign_vacancies(account, "ai_core", "review", 100, source=source)
            total += len(rows)
            top_total += len(top)
            review_total += len(review)
            print(f"queue {source}: all={len(rows)} top={len(top)} review={len(review)}")
        print(f"total={total} top={top_total} review={review_total}")
        assert total >= 20, "expected a useful fixture corpus"
        assert top_total >= 2, "TOP must contain strong-but-unverified AI roles"
        assert review_total >= 4, "review must contain promising unknown conditions"

        account = result["accounts"]["getmatch.ru"]
        store.set_review_state(
            account, "getmatch.ru", "36887", "APPROVED",
            reason="offline smoke: role and stack match ai_core", track="ai_core",
        )
        plan = queue_plan(store, account, "ai_core", "getmatch.ru", 100)
        print("plan getmatch.ru:")
        print(f"  mode={plan['mode']} planned={plan['planned_count']} "
              f"blocked={plan['blocked_count']} apply={plan['apply_status']}")
        for row in plan["blocked"][:5]:
            print(f"  BLOCKED {row['id']} {row['reason']}")
        assert plan["mode"] == "dry_run"
        assert plan["planned_count"] == 0
        assert plan["blocked_count"] >= 1
        assert all("needs_live_validation" in row["reason"] for row in plan["blocked"])
        assert store.source_attempts(account, "getmatch.ru") == []
        print("OK: no real attempt was recorded")
        print(json.dumps({"db": str(config.db_path), "sources": result["accounts"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
