from datetime import date, datetime, timezone

import scrapers.inserate_ultra_optimized as scraper


def test_aware_cutoff_is_compared_in_kleinanzeigen_timezone():
    results = [
        {"id": "old", "published_at": "2026-05-04T09:59:00"},
        {"id": "equal", "published_at": "2026-05-04T10:00:00"},
        {"id": "new", "published_at": "2026-05-04T10:01:00"},
        {"id": "unknown", "published_at": None},
    ]
    # 08:00 UTC is 10:00 in Europe/Berlin on this date.
    cutoff = datetime(2026, 5, 4, 8, 0, tzinfo=timezone.utc)

    assert scraper._page_has_old_listings(results, cutoff)
    assert [
        item["id"] for item in scraper._filter_by_min_publish_date(results, cutoff)
    ] == ["equal", "new", "unknown"]


def test_naive_cutoff_keeps_existing_local_time_semantics():
    results = [{"id": "item", "published_at": "2026-05-04T09:59:00"}]

    assert scraper._page_has_old_listings(
        results,
        datetime(2026, 5, 4, 10, 0),
    )


def test_relative_dates_use_kleinanzeigen_calendar(monkeypatch):
    monkeypatch.setattr(scraper, "_kleinanzeigen_today", lambda: date(2026, 1, 2))

    assert scraper._parse_kleinanzeigen_date("Heute, 00:15") == ("2026-01-02T00:15:00")
    assert scraper._parse_kleinanzeigen_date("Gestern, 23:45") == (
        "2026-01-01T23:45:00"
    )
