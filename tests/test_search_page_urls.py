"""Offline regressions for canonical Kleinanzeigen pagination URLs."""

import pytest

from scrapers.inserate_by_url import inject_page
from scrapers.inserate_ultra_optimized import build_search_page_url


def _job_url(url: str, page: int) -> str:
    return build_search_page_url({"url": url}, page)


@pytest.mark.parametrize("builder", [inject_page, _job_url])
def test_keyword_shortcut_uses_leading_s_seite_segment(builder):
    base = "https://www.kleinanzeigen.de/s-maerklin-3021/k0?sortierung=preis#angebote"

    assert builder(base, 1) == base
    assert builder(base, 2) == (
        "https://www.kleinanzeigen.de/s-seite:2/maerklin-3021/k0"
        "?sortierung=preis#angebote"
    )


@pytest.mark.parametrize("builder", [inject_page, _job_url])
def test_keyword_shortcut_replaces_existing_page_idempotently(builder):
    existing = "https://www.kleinanzeigen.de/s-seite:17/maerklin-3021/k0"
    expected = "https://www.kleinanzeigen.de/s-seite:2/maerklin-3021/k0"

    assert builder(existing, 2) == expected
    assert builder(builder(existing, 2), 2) == expected
    assert builder(existing, 1) == "https://www.kleinanzeigen.de/s-maerklin-3021/k0"


@pytest.mark.parametrize("builder", [inject_page, _job_url])
def test_keyword_shortcut_normalizes_the_previously_broken_suffix_form(builder):
    broken = "https://www.kleinanzeigen.de/s-maerklin-3021/k0/s-seite:8"

    assert builder(broken, 2) == (
        "https://www.kleinanzeigen.de/s-seite:2/maerklin-3021/k0"
    )


@pytest.mark.parametrize("builder", [inject_page, _job_url])
def test_category_page_precedes_filter_and_preserves_encoded_filters(builder):
    base = (
        "https://www.kleinanzeigen.de/s-autos/volkswagen/preis:1000:15000/klima/"
        "k0c216+autos.marke_s:(volkswagen%2Caudi)"
        "?sortierung=preis#angebote"
    )
    expected = (
        "https://www.kleinanzeigen.de/s-autos/volkswagen/preis:1000:15000/klima/"
        "seite:2/k0c216+autos.marke_s:(volkswagen%2Caudi)"
        "?sortierung=preis#angebote"
    )

    assert builder(base, 2) == expected
    assert builder(expected, 2) == expected


@pytest.mark.parametrize("builder", [inject_page, _job_url])
def test_category_existing_page_is_replaced_or_removed(builder):
    existing = "https://www.kleinanzeigen.de/s-wohnwagen-mobile/wohnwagen/seite:9/c220"

    assert builder(existing, 2) == (
        "https://www.kleinanzeigen.de/s-wohnwagen-mobile/wohnwagen/seite:2/c220"
    )
    assert builder(existing, 1) == (
        "https://www.kleinanzeigen.de/s-wohnwagen-mobile/wohnwagen/c220"
    )


@pytest.mark.parametrize("builder", [inject_page, _job_url])
def test_category_s_seite_variant_is_normalized(builder):
    existing = "https://www.kleinanzeigen.de/s-wohnwagen-mobile/s-seite:9/c220"

    assert builder(existing, 2) == (
        "https://www.kleinanzeigen.de/s-wohnwagen-mobile/seite:2/c220"
    )


@pytest.mark.parametrize("builder", [inject_page, _job_url])
def test_query_string_search_keeps_parameters_behind_page_path(builder):
    base = (
        "https://www.kleinanzeigen.de/s-anzeige:angebote"
        "?keywords=M%C3%A4rklin&locationStr=K%C3%B6ln&radius=50"
    )

    assert builder(base, 2) == (
        "https://www.kleinanzeigen.de/s-anzeige:angebote/s-seite:2"
        "?keywords=M%C3%A4rklin&locationStr=K%C3%B6ln&radius=50"
    )


@pytest.mark.parametrize("invalid_page", [True, 0, -1, 1.5, "2"])
def test_both_url_builders_reject_invalid_page_numbers(invalid_page):
    url = "https://www.kleinanzeigen.de/s-maerklin-3021/k0"

    with pytest.raises(ValueError):
        inject_page(url, invalid_page)
    with pytest.raises(ValueError):
        _job_url(url, invalid_page)
