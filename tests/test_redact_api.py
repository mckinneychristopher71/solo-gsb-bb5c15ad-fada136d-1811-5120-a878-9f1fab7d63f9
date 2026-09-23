"""End-to-end tests for POST /redact (deterministic leaf redaction)."""

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _redact(document, rules):
    return client.post("/redact", json={"document": document, "rules": rules})


# ---------------------------------------------------------------------------
# mask
# ---------------------------------------------------------------------------


def test_mask_literal_leaf_paths():
    doc = {"subject": {"name": "Ada", "ssn": "078-05-1120"}, "ok": True}
    resp = _redact(doc, [
        {"pattern": "/subject/ssn", "action": "mask", "value": "***"},
    ])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["document"] == {
        "subject": {"name": "Ada", "ssn": "***"},
        "ok": True,
    }
    assert body["applied"] == [
        {"path": "/subject/ssn", "action": "mask",
         "ruleIndex": 0, "pattern": "/subject/ssn"},
    ]


def test_mask_constant_may_be_any_json_value():
    resp = _redact(
        {"a": 1, "b": "x"},
        [{"pattern": "/a", "action": "mask", "value": {"k": [1, 2]}},
         {"pattern": "/b", "action": "mask", "value": None}],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["document"] == {"a": {"k": [1, 2]}, "b": None}


def test_mask_does_not_mutate_input_document():
    doc = {"xs": ["a", "b"]}
    resp = _redact(doc, [{"pattern": "/xs/*", "action": "mask", "value": "X"}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"] == {"xs": ["X", "X"]}
    assert doc == {"xs": ["a", "b"]}


def test_mask_constant_is_deep_copied_per_leaf():
    constant = {"nested": []}
    resp = _redact(
        {"a": 1, "b": 2},
        [{"pattern": "/*", "action": "mask", "value": constant}],
    )
    assert resp.status_code == 200, resp.text
    document = resp.json()["document"]
    document["a"]["nested"].append("mutated")
    assert document["b"] == {"nested": []}


# ---------------------------------------------------------------------------
# drop
# ---------------------------------------------------------------------------


def test_drop_removes_object_members():
    doc = {"a": 1, "b": 2, "c": 3}
    resp = _redact(doc, [{"pattern": "/b", "action": "drop"}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"] == {"a": 1, "c": 3}
    assert resp.json()["applied"] == [
        {"path": "/b", "action": "drop", "ruleIndex": 0, "pattern": "/b"},
    ]


def test_drop_array_element_becomes_null_without_shifting():
    doc = {"xs": ["a", "b", "c"]}
    resp = _redact(doc, [{"pattern": "/xs/1", "action": "drop"}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"] == {"xs": ["a", None, "c"]}
    assert doc == {"xs": ["a", "b", "c"]}


def test_drop_all_members_leaves_an_empty_container():
    resp = _redact({"a": 1}, [{"pattern": "/**", "action": "drop"}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"] == {}
    assert [entry["path"] for entry in resp.json()["applied"]] == ["/a"]


# ---------------------------------------------------------------------------
# Wildcards
# ---------------------------------------------------------------------------


def test_star_matches_exactly_one_level():
    doc = {"emails": ["a@x", "b@y"], "nested": {"deep": 1}}
    resp = _redact(
        doc,
        [{"pattern": "/emails/*", "action": "mask", "value": "r"}],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"] == {
        "emails": ["r", "r"],
        "nested": {"deep": 1},
    }
    assert len(resp.json()["applied"]) == 2


def test_star_in_middle_of_path():
    doc = {"users": {"ada": {"age": 36}, "bob": {"age": 40}, "keep": 1}}
    resp = _redact(
        doc,
        [{"pattern": "/users/*/age", "action": "mask", "value": 0}],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"] == {
        "users": {"ada": {"age": 0}, "bob": {"age": 0}, "keep": 1},
    }


def test_star_does_not_cross_levels():
    doc = {"a": 1, "b": {"c": 2}}
    resp = _redact(doc, [{"pattern": "/*", "action": "drop"}])
    assert resp.status_code == 200, resp.text
    # /a is removed; /b/c is two levels down and survives (with /b intact).
    assert resp.json()["document"] == {"b": {"c": 2}}
    assert [e["path"] for e in resp.json()["applied"]] == ["/a"]


@pytest.mark.parametrize(
    "pattern,expected_paths",
    [
        ("/**",
         ["/a", "/b/c", "/b/d/0", "/b/d/1", "/name",
          "/x/name", "/x/y/name", "/x/y/z/name"]),
        # '**' matches zero levels, so /x/**/name also hits /x/name.
        ("/x/**/name", ["/x/name", "/x/y/name", "/x/y/z/name"]),
        ("/**/0", ["/b/d/0"]),
    ],
)
def test_globstar_matches_any_number_of_levels(pattern, expected_paths):
    doc = {"a": 1, "b": {"c": 2, "d": [3, 4]}, "name": "top",
           "x": {"name": "one", "y": {"name": "two", "z": {"name": "three"}}}}
    resp = _redact(doc, [{"pattern": pattern, "action": "drop"}])
    assert resp.status_code == 200, resp.text
    assert [e["path"] for e in resp.json()["applied"]] == expected_paths


def test_wildcards_apply_to_empty_containers_as_leaves():
    doc = {"objs": [{}, {"keep": 1}], "emptyArr": []}
    resp = _redact(
        doc,
        [{"pattern": "/objs/0", "action": "mask", "value": None},
         {"pattern": "/emptyArr", "action": "drop"}],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"] == {"objs": [None, {"keep": 1}]}


# ---------------------------------------------------------------------------
# Rule precedence
# ---------------------------------------------------------------------------


def test_literal_beats_star_beats_globstar():
    doc = {"a": {"b": 1}}
    resp = _redact(doc, [
        {"pattern": "/**", "action": "mask", "value": "globstar"},
        {"pattern": "/*/*", "action": "mask", "value": "star"},
        {"pattern": "/a/b", "action": "mask", "value": "literal"},
    ])
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"] == {"a": {"b": "literal"}}
    assert resp.json()["applied"][0]["ruleIndex"] == 2


def test_star_beats_globstar_even_when_globstar_listed_first():
    doc = {"a": {"b": 1}}
    resp = _redact(doc, [
        {"pattern": "/**", "action": "mask", "value": "globstar"},
        {"pattern": "/*/*", "action": "mask", "value": "star"},
    ])
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"] == {"a": {"b": "star"}}
    assert resp.json()["applied"][0]["ruleIndex"] == 1


def test_equal_specificity_resolves_by_rule_order():
    doc = {"a": {"b": 1}}
    resp = _redact(doc, [
        {"pattern": "/*/b", "action": "mask", "value": "first"},
        {"pattern": "/a/*", "action": "mask", "value": "second"},
    ])
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"] == {"a": {"b": "first"}}
    assert resp.json()["applied"][0]["ruleIndex"] == 0


def test_precedence_compares_literal_token_count_not_pattern_length():
    # One literal + one star beats a rule whose tokens are all wildcards even
    # though both patterns span two levels.
    doc = {"a": {"b": 1, "c": 2}}
    resp = _redact(doc, [
        {"pattern": "/*/*", "action": "mask", "value": "star"},
        {"pattern": "/a/*", "action": "mask", "value": "literal-prefix"},
    ])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    by_path = {e["path"]: e for e in body["applied"]}
    assert body["document"]["a"]["b"] == "literal-prefix"
    assert body["document"]["a"]["c"] == "literal-prefix"
    assert by_path["/a/b"]["ruleIndex"] == 1


# ---------------------------------------------------------------------------
# Root / scalar documents and applied ordering
# ---------------------------------------------------------------------------


def test_root_scalar_can_be_masked_via_empty_fragment():
    resp = _redact(42, [{"pattern": "", "action": "mask", "value": "X"}])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["document"] == "X"
    assert body["applied"] == [
        {"path": "", "action": "mask", "ruleIndex": 0, "pattern": ""},
    ]


def test_root_empty_container_is_a_matchable_leaf():
    resp = _redact({}, [{"pattern": "", "action": "mask", "value": []}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"] == []


def test_root_drop_is_rejected():
    resp = _redact(42, [{"pattern": "", "action": "drop"}])
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "INVALID_REQUEST"
    assert err["loc"] == "rules[0]"
    assert err["detail"]["path"] == ""
    assert "document" not in resp.json()


def test_applied_is_sorted_by_rfc6901_path():
    doc = {"z": 1, "a": {"y": 2, "b": [3, 4]}}
    resp = _redact(doc, [{"pattern": "/**", "action": "drop"}])
    assert resp.status_code == 200, resp.text
    paths = [e["path"] for e in resp.json()["applied"]]
    assert paths == sorted(paths)
    assert paths == ["/a/b/0", "/a/b/1", "/a/y", "/z"]


def test_unmatched_leaves_are_untouched_and_omitted_from_applied():
    doc = {"a": 1, "b": 2}
    resp = _redact(doc, [{"pattern": "/a", "action": "mask", "value": 0}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"] == {"a": 0, "b": 2}
    assert [e["path"] for e in resp.json()["applied"]] == ["/a"]


def test_empty_rules_is_a_valid_noop():
    doc = {"a": [1, 2], "b": {}}
    resp = _redact(doc, [])
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"document": doc, "applied": []}


# ---------------------------------------------------------------------------
# Escaped tokens and literal wildcards in keys
# ---------------------------------------------------------------------------


def test_rfc6901_escapes_apply_to_patterns():
    doc = {"a/b": 1, "tilde~key": 2}
    resp = _redact(doc, [
        {"pattern": "/a~1b", "action": "drop"},
        {"pattern": "/tilde~0key", "action": "mask", "value": 0},
    ])
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"] == {"tilde~key": 0}


def test_wildcard_token_matches_a_member_named_like_a_wildcard():
    doc = {"a/b": {"*": 1}}
    resp = _redact(doc, [{"pattern": "/a~1b/*", "action": "drop"}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"] == {"a/b": {}}


# ---------------------------------------------------------------------------
# Rule validation / unified error format
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rule,expected_code,expected_loc",
    [
        ({"pattern": "no-leading-slash", "action": "drop"},
         "POINTER_SYNTAX", "rules[0]"),
        ({"pattern": "/bad~2", "action": "drop"},
         "POINTER_SYNTAX", "rules[0]"),
        ({"pattern": "/a*", "action": "drop"},
         "POINTER_SYNTAX", "rules[0]"),
        ({"pattern": "/**", "action": "drop", "extra": True},
         None, None),  # extra fields are ignored
        ({"action": "drop"}, "INVALID_REQUEST", "rules[0].pattern"),
        ({"pattern": "/a"}, "INVALID_REQUEST", "rules[0].action"),
        ({"pattern": 42, "action": "drop"},
         "INVALID_REQUEST", "rules[0].pattern"),
        ({"pattern": "/a", "action": 42},
         "INVALID_REQUEST", "rules[0].action"),
        ({"pattern": "/a", "action": "redact"},
         "INVALID_REQUEST", "rules[0].action"),
        ({"pattern": "/a", "action": "mask"},
         "INVALID_REQUEST", "rules[0].value"),
    ],
)
def test_invalid_rules_use_the_unified_error_body(
    rule, expected_code, expected_loc
):
    if expected_code is None:
        resp = _redact({"a": 1}, [rule])
        assert resp.status_code == 200, resp.text
        return
    resp = _redact({"a": 1}, [rule])
    assert resp.status_code == 400, resp.text
    err = resp.json()["error"]
    assert err["code"] == expected_code
    assert err["loc"] == expected_loc


def test_loc_points_at_the_offending_rule_index():
    doc = {"a": 1}
    resp = _redact(doc, [
        {"pattern": "/a", "action": "mask", "value": 0},
        {"pattern": "not-a-pointer", "action": "drop"},
    ])
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "POINTER_SYNTAX"
    assert err["loc"] == "rules[1]"
    # The document is untouched on failure.
    assert doc == {"a": 1}


def test_rules_must_be_an_array():
    resp = _redact({"a": 1}, {"pattern": "/a", "action": "drop"})
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "INVALID_REQUEST"
    assert err["loc"] == "rules"


def test_non_object_rule_envelope_is_rejected():
    resp = _redact({"a": 1}, ["drop"])
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "rules[0]"


def test_non_finite_mask_value_is_rejected_at_value_loc():
    resp = client.post(
        "/redact",
        content=b'{"document": {"a": 1}, "rules": ['
                b'{"pattern": "/a", "action": "mask", "value": NaN}]}',
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "UNSUPPORTED_VALUE"
    assert err["loc"] == "rules[0].value"


def test_request_level_errors_match_other_endpoints():
    resp = client.post("/redact", json={"rules": []})
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "document"

    resp = client.post("/redact", json={"document": {}})
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "rules"

    resp = client.post("/redact", json=[1])
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "body"

    resp = client.post(
        "/redact",
        content=b'{"document": {"a": 1},',
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_JSON"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_redaction_is_deterministic_across_calls():
    doc = {"nested": {"a": 1, "b": [2, 3]}}
    rules = [
        {"pattern": "/**", "action": "mask", "value": "g"},
        {"pattern": "/nested/b/*", "action": "mask", "value": "s"},
        {"pattern": "/nested/a", "action": "mask", "value": "L"},
    ]
    first = _redact(doc, rules).json()
    second = _redact(doc, rules).json()
    assert first == second
    # /nested/a is won by the literal rule; /nested/b/* (one literal + one
    # star) is more specific than the pure globstar; the globstar wins only
    # the leaves no narrower rule reaches.
    assert first["document"] == {"nested": {"a": "L", "b": ["s", "s"]}}


# ---------------------------------------------------------------------------
# Meta / OpenAPI
# ---------------------------------------------------------------------------


def test_redact_is_listed_in_service_metadata():
    info = client.get("/").json()
    assert info["endpoints"]["redact"] == "POST /redact"


def test_openapi_documents_redact_contract():
    schema = client.get("/openapi.json").json()
    op = schema["paths"]["/redact"]["post"]
    assert op["operationId"] == "redact"
    assert "RedactRequest" in schema["components"]["schemas"]
    assert "RedactResponse" in schema["components"]["schemas"]
    assert "RedactRule" in schema["components"]["schemas"]
    assert "AppliedRedaction" in schema["components"]["schemas"]
    assert "200" in op["responses"]
