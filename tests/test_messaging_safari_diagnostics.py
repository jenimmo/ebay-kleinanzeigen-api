"""Schema diagnostics remain bounded and never export identity or UI values."""

import json

import pytest

from messaging.safari import (
    _identity_structure,
    _own_component_schema,
    _sanitize_structure,
)


def test_listing_profile_diagnostics_keep_only_fixed_scope_and_boolean_proof():
    result = _sanitize_structure({
        "page": "/s-anzeige/PRIVATE-SLUG/1234500001-84-123",
        "listing_recipient_verified": True,
        "nodes": [
            {"tag": "div", "profile_scope": "userprofile-vip", "class": "PRIVATE-CLASS", "text": "PRIVATE-NAME"},
            {"tag": "a", "profile_scope": "userprofile", "userId": "222", "link_path": "/s-bestandsliste.html", "href": "PRIVATE-URL"},
            {"tag": "a", "profile_scope": "PRIVATE", "userId": "PRIVATE-UID"},
        ],
    })
    assert result["page"] == "/s-anzeige/1234500001-84-123"
    assert result["listing_recipient_verified"] is True
    assert result["nodes"] == [
        {"tag": "div", "profile_scope": "userprofile-vip", "attributes": []},
        {"tag": "a", "profile_scope": "userprofile", "userId": "222", "link_path": "/s-bestandsliste.html", "attributes": []},
        {"tag": "a", "attributes": []},
    ]
    assert "PRIVATE" not in json.dumps(result)
    for invalid in ("true", 1, None, {"PRIVATE": True}):
        assert "listing_recipient_verified" not in _sanitize_structure({"nodes": [], "listing_recipient_verified": invalid})


def test_own_component_diagnostics_preserve_known_structure_without_values():
    diagnostic = _identity_structure(
        {
            "own_component_schema": [
                {
                    "source": "ssr",
                    "scan_limited": False,
                    "nodes": [
                        {
                            "tag": "astro-island",
                            "in_header": True,
                            "matches_user": True,
                            "matches_account": False,
                            "matches_profile": False,
                            "astro": True,
                            "attribute_names": [
                                "component-url",
                                "props",
                                "PRIVATE",
                                "onclick",
                            ],
                            "component-url": "https://example.org/PRIVATE",
                            "props": "PRIVATE",
                            "payload_kind": "astro_props",
                            "payload_present": True,
                            "payload_valid_json": True,
                            "payload_shape": [
                                {"path": [], "type": "object", "value": "PRIVATE"},
                                {
                                    "path": ["user", "id"],
                                    "type": "number",
                                    "value": "PRIVATE",
                                },
                                {"path": ["user", "PRIVATE"], "type": "string"},
                                {"path": ["user"], "type": "PRIVATE"},
                            ],
                        }
                    ],
                }
            ],
        }
    )
    assert diagnostic == {
        "sources": [],
        "own_component_schema": [
            {
                "source": "ssr",
                "scan_limited": False,
                "nodes": [
                    {
                        "tag": "astro-island",
                        "in_header": True,
                        "matches_user": True,
                        "matches_account": False,
                        "matches_profile": False,
                        "astro": True,
                        "attribute_names": ["component-url", "props"],
                        "payload_kind": "astro_props",
                        "payload_present": True,
                        "payload_valid_json": True,
                        "payload_shape": [
                            {"path": [], "type": "object"},
                            {"path": ["user", "id"], "type": "number"},
                        ],
                    }
                ],
            }
        ],
    }
    assert "PRIVATE" not in json.dumps(diagnostic)


def test_own_component_diagnostics_bound_sources_nodes_attributes_shapes_and_paths():
    node = {
        "tag": "astro-island",
        "attribute_names": ["props"] * 25,
        "payload_shape": [
            {"path": ["props", "[]", "data", "user", "id"], "type": "number"}
        ]
        * 25,
    }
    result = _own_component_schema(
        [
            {"source": "rendered", "scan_limited": True, "nodes": [node] * 13},
            {"source": "ssr", "nodes": [node]},
            {"source": "rendered", "nodes": [node]},
        ]
    )
    assert len(result) == 2
    assert len(result[0]["nodes"]) == 12
    assert len(result[0]["nodes"][0]["attribute_names"]) == 24
    assert len(result[0]["nodes"][0]["payload_shape"]) == 24
    assert result[0]["scan_limited"] is True
    invalid_path = {"path": ["props"] * 6, "type": "object"}
    assert (
        _own_component_schema(
            [
                {
                    "source": "ssr",
                    "nodes": [{"tag": "script", "payload_shape": [invalid_path]}],
                }
            ]
        )[0]["nodes"][0]["payload_shape"]
        == []
    )


@pytest.mark.parametrize("invalid", [None, "PRIVATE", {}, True])
def test_own_component_diagnostics_reject_nonarrays(invalid):
    assert _own_component_schema(invalid) is None


def test_own_component_diagnostics_reject_wrong_types_unknown_sources_and_tags():
    result = _own_component_schema(
        [
            {"source": "PRIVATE", "nodes": [{"tag": "div"}]},
            {
                "source": "ssr",
                "scan_limited": 1,
                "nodes": [
                    {"tag": "PRIVATE<script>"},
                    {
                        "tag": "div",
                        "attribute_names": [None, ["props"], "data-private"],
                        "in_header": "PRIVATE",
                        "matches_user": 1,
                        "payload_kind": "PRIVATE",
                        "payload_present": {},
                        "payload_valid_json": [],
                        "payload_shape": [None, {"path": [["user"]], "type": "object"}],
                    },
                ],
            },
        ]
    )
    assert result == [
        {
            "source": "ssr",
            "nodes": [
                {
                    "tag": "div",
                    "attribute_names": [],
                    "payload_shape": [],
                }
            ],
        }
    ]


def test_business_empty_variants_are_only_fixed_booleans_and_never_a_parser_result():
    valid = {
        "intro_text_content": True,
        "detail_text_content": False,
        "intro_inner_text": True,
        "detail_inner_text": False,
        "detail_without_whitespace": True,
        "detail_without_terminal_dot": False,
        "intro_aria_alt": True,
        "detail_aria_alt": False,
    }
    result = _sanitize_structure(
        {
            "nodes": [],
            "empty_variants": {
                **valid,
                "text": "PRIVATE",
                "conversations": [],
                "is_empty": True,
            },
        }
    )
    assert result["empty_variants"] == valid
    assert "conversations" not in result
    assert "is_empty" not in result
    assert "PRIVATE" not in json.dumps(result)
    invalid = {name: "PRIVATE" for name in valid}
    invalid["intro_text_content"] = 1
    assert (
        _sanitize_structure({"nodes": [], "empty_variants": invalid})["empty_variants"]
        == {}
    )
