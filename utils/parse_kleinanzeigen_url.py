"""
Utility for parsing Kleinanzeigen URLs and mapping them to /inserate API params.
"""

import re
from urllib.parse import urlparse, parse_qs, unquote


class InvalidUrlFilterError(ValueError):
    """Raised when a recognized URL filter has an invalid numeric value."""


def _parse_non_negative_int(value: str, field: str, *, minimum: int = 0) -> int:
    """Parse a decimal URL filter without accepting signs or partial values."""
    if not value.isascii() or not value.isdecimal():
        raise InvalidUrlFilterError(f"Invalid numeric value for {field}")

    parsed = int(value)
    if parsed < minimum:
        raise InvalidUrlFilterError(f"Invalid numeric value for {field}")
    return parsed


def parse_kleinanzeigen_url(url: str) -> dict:
    """Parse a Kleinanzeigen URL and extract all known parameters."""
    parsed = urlparse(url)
    path = unquote(parsed.path).strip("/")
    qs = parse_qs(parsed.query)

    result = {}

    # ── Query-string params ───────────────────────────────────────────────
    if "keywords" in qs:
        result["query"] = qs["keywords"][0]
    if "locationStr" in qs:
        result["location"] = qs["locationStr"][0]
    if "radius" in qs:
        result["radius"] = _parse_non_negative_int(qs["radius"][0], "radius")

    # ── Path segments ─────────────────────────────────────────────────────
    segments = path.split("/")
    filter_segment = None

    for i, seg in enumerate(segments):
        # Page — seite:2
        if seg.startswith("seite:"):
            result["page"] = _parse_non_negative_int(
                seg.removeprefix("seite:"), "page", minimum=1
            )

        # Page (generic search) — s-seite:2
        elif seg.startswith("s-seite:"):
            result["page"] = _parse_non_negative_int(
                seg.removeprefix("s-seite:"), "page", minimum=1
            )

        # Category slug — s-wohnwagen-mobile
        elif seg.startswith("s-") and "category_slug" not in result:
            result["category_slug"] = seg

        # Price range — preis:1000:15000
        elif seg.startswith("preis:"):
            parts = seg.split(":")
            if len(parts) != 3:
                raise InvalidUrlFilterError("Invalid price range")
            if parts[1]:
                result["min_price"] = _parse_non_negative_int(parts[1], "min_price")
            if parts[2]:
                result["max_price"] = _parse_non_negative_int(parts[2], "max_price")

        # Filter segment — c220+... or k0c220+...
        elif re.match(r"^k?\d*c\d+", seg):
            filter_segment = seg

        # Subcategory — second segment
        elif i == 1:
            result["subcategory"] = seg

        # Path keyword — between subcategory and filter segment
        elif filter_segment is None and i > 1:
            result["path_keyword"] = seg

    # ── Filter segment ────────────────────────────────────────────────────
    if filter_segment:
        fs = filter_segment[2:] if filter_segment.startswith("k0") else filter_segment

        for attr in fs.split("+"):
            # Category ID — c220
            if re.match(r"^c\d+$", attr):
                result["category_id"] = _parse_non_negative_int(
                    attr[1:], "category_id", minimum=1
                )
                continue

            if ":" not in attr:
                continue

            key, value = attr.split(":", 1)

            # Year — closed range (2008,2024), open range (2008,),
            # upper-bounded range (,2024), or the historic single-value form.
            if key.endswith(".ez_i"):
                if value.count(",") > 1:
                    raise InvalidUrlFilterError("Invalid year range")

                if "," in value:
                    year_from, year_to = value.split(",", 1)
                    if not year_from and not year_to:
                        raise InvalidUrlFilterError("Invalid year range")
                    if year_from:
                        result["year_from"] = _parse_non_negative_int(
                            year_from, "year_from", minimum=1
                        )
                    if year_to:
                        result["year_to"] = _parse_non_negative_int(
                            year_to, "year_to", minimum=1
                        )
                    else:
                        result["year_to"] = None
                elif value:
                    result["year_from"] = _parse_non_negative_int(
                        value, "year_from", minimum=1
                    )
                else:
                    raise InvalidUrlFilterError("Invalid year range")

                if (
                    result.get("year_from") is not None
                    and result.get("year_to") is not None
                    and result["year_from"] > result["year_to"]
                ):
                    raise InvalidUrlFilterError("Invalid year range")

            # Article type — *.art_s:wohnwagen
            elif key.endswith(".art_s"):
                result["art"] = value

            # Brands — *.marke_s:(fendt,knaus) or *.marke_s:fendt
            elif key.endswith(".marke_s"):
                if value.startswith("(") and value.endswith(")"):
                    result["brands"] = value[1:-1].split(",")
                else:
                    result["brands"] = [value]

            # Unknown attributes — keep for transparency
            else:
                result.setdefault("unknown_attrs", {})[key] = value

    return result


# Keys that map directly or indirectly to /inserate params
_MAPPED_SOURCE_KEYS = {
    "query",
    "path_keyword",
    "location",
    "radius",
    "min_price",
    "max_price",
    "page",
}


def map_to_inserate_params(parsed: dict) -> tuple[dict, dict]:
    """
    Map parsed URL params to /inserate API params.

    Returns:
        inserate_params: only keys the current /inserate endpoint understands
        unmapped:        everything that could not be expressed via /inserate yet
    """
    inserate_params = {}

    # query: prefer explicit QS keyword, fall back to path keyword
    if "query" in parsed:
        inserate_params["query"] = parsed["query"]
    elif "path_keyword" in parsed:
        inserate_params["query"] = parsed["path_keyword"]

    for key in ("location", "radius", "min_price", "max_price"):
        if key in parsed:
            inserate_params[key] = parsed[key]

    inserate_params["page_count"] = parsed.get("page", 1)

    unmapped = {k: v for k, v in parsed.items() if k not in _MAPPED_SOURCE_KEYS}

    return inserate_params, unmapped
