"""End-to-end tests for POST /merge."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _merge(base, left, right):
    return client.post(
        "/merge", json={"base": base, "left": left, "right": right}
    )


# ---------------------------------------------------------------------------
# Clean merges
# ---------------------------------------------------------------------------


def test_merge_takes_one_sided_edits_including_deletes_and_adds():
    base = {
        "both": "same",
        "leftEdit": 1,
        "rightEdit": 2,
        "leftDelete": "x",
        "rightDelete": "y",
        "nested": {"a": 1, "b": 2, "c": 3},
    }
    left = {
        "both": "same",
        "leftEdit": 10,
        "rightEdit": 2,
        "nested": {"a": 10, "b": 2, "c": 3},
        "leftAdded": "L",
    }
    right = {
        "both": "same",
        "leftEdit": 1,
        "rightEdit": 20,
        "leftDelete": "x",
        "nested": {"a": 1, "b": 2},
        "rightAdded": "R",
    }

    resp = _merge(base, left, right)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "merged": {
            "both": "same",
            "leftEdit": 10,
            "rightEdit": 20,
            "nested": {"a": 10, "b": 2},
            "leftAdded": "L",
            "rightAdded": "R",
        }
    }


def test_merge_accepts_identical_concurrent_changes():
    base = {"a": 1, "nested": {"x": 1}}
    left = {"a": 5, "nested": {"x": 5}}
    right = {"a": 5, "nested": {"x": 5}}
    assert _merge(base, left, right).json() == {
        "merged": {"a": 5, "nested": {"x": 5}}
    }

    # Both deleting the same member is agreement, not a conflict.
    base = {"a": 1, "b": 2}
    left = {"b": 2}
    right = {"b": 2}
    assert _merge(base, left, right).json() == {"merged": {"b": 2}}

    # Both independently adding the same key/value is agreement.
    base = {}
    left = {"k": {"x": 1}}
    right = {"k": {"x": 1}}
    assert _merge(base, left, right).json() == {"merged": {"k": {"x": 1}}}


def test_merge_treats_present_null_and_missing_member_as_distinct():
    base = {"a": None, "b": 1}

    # Left changes null -> 0; right leaves the real null. The edit wins and
    # the present null must not be mistaken for "deleted".
    left = {"a": 0, "b": 1}
    right = {"a": None, "b": 1}
    assert _merge(base, left, right).json() == {"merged": {"a": 0, "b": 1}}

    # Replacing an existing value with null is a genuine modify: if the
    # other side deleted the member instead, that is a delete/modify
    # conflict (present null on one side vs absent member on the other).
    base = {"a": "v", "b": 1}
    left = {"a": None, "b": 1}
    right = {"b": 1}
    resp = _merge(base, left, right)
    assert resp.status_code == 200
    conflict = resp.json()["conflicts"][0]
    assert conflict["path"] == "/a"
    assert conflict["base"] == {"present": True, "value": "v"}
    assert conflict["left"] == {"present": True, "value": None}
    assert conflict["right"] == {"present": False, "value": None}

    # Conversely, one side deleting while the other keeps the value is the
    # deletion alone and merges cleanly.
    assert _merge({"a": "v", "b": 1}, {"b": 1}, {"a": "v", "b": 1}).json() == {
        "merged": {"b": 1}
    }


def test_merge_integer_one_and_float_one_point_zero_are_different_edits():
    # Canonical JSON bytes: 1 vs 1.0 — both changed them "to numbers" but
    # not to the same canonical value, hence a conflict.
    resp = _merge({"a": 0}, {"a": 1}, {"a": 1.0})
    assert resp.status_code == 200
    conflicts = resp.json()["conflicts"]
    assert [c["path"] for c in conflicts] == ["/a"]
    assert conflicts[0]["left"] == {"present": True, "value": 1}
    assert conflicts[0]["right"] == {"present": True, "value": 1.0}

    # Canonical equality is per-side-vs-base: an untouched side never blocks
    # the other edit, regardless of how its float/int renders.
    assert _merge({"a": 1}, {"a": 1}, {"a": 2}).json() == {
        "merged": {"a": 2}
    }
    assert _merge({"a": 1.0}, {"a": 1.0}, {"a": 2}).json() == {
        "merged": {"a": 2}
    }

    # The reverse: base integer, left changes, right keeps base as the same
    # integer; the one-sided edit wins (float 1.0 would instead conflict).
    assert _merge({"a": 1}, {"a": 2}, {"a": 1}).json() == {
        "merged": {"a": 2}
    }


def test_merge_arrays_are_atomic_values():
    base = {"tags": [1, 2], "kept": [0]}
    left = {"tags": [1, 2, 3], "kept": [0]}
    right = {"tags": [0, 2], "kept": [0]}
    resp = _merge(base, left, right)
    assert [c["path"] for c in resp.json()["conflicts"]] == ["/tags"]
    conflict = resp.json()["conflicts"][0]
    assert conflict["base"]["value"] == [1, 2]
    assert conflict["left"]["value"] == [1, 2, 3]
    assert conflict["right"]["value"] == [0, 2]

    # One-sided array edit merges cleanly even when elements differ.
    resp = _merge(base, left, {"tags": [1, 2], "kept": [0]})
    assert resp.json() == {"merged": {"tags": [1, 2, 3], "kept": [0]}}


def test_merge_handles_empty_containers_and_root_scalars():
    # Empty objects are present leaves but still recurse as objects.
    base = {"e": {}, "n": 1}
    left = {"e": {"x": 1}, "n": 1}
    right = {"e": {}, "n": 2, "added": []}
    assert _merge(base, left, right).json() == {
        "merged": {"e": {"x": 1}, "n": 2, "added": []}
    }

    # Root scalars follow the same atomic three-way rule.
    assert _merge(1, 2, 1).json() == {"merged": 2}
    assert _merge(1, 2, 2).json() == {"merged": 2}
    conflicts = _merge(1, 2, 3).json()["conflicts"]
    assert conflicts == [
        {
            "path": "",
            "base": {"present": True, "value": 1},
            "left": {"present": True, "value": 2},
            "right": {"present": True, "value": 3},
        }
    ]


def test_merge_member_order_is_deterministic():
    base = {"z": 1, "a": 1}
    left = {"z": 2, "a": 1}
    right = {"z": 1, "a": 2, "m": 9, "b": 8}
    merged = _merge(base, left, right).json()["merged"]
    # Base members keep base order; concurrent adds follow sorted.
    assert list(merged.keys()) == ["z", "a", "b", "m"]
    assert merged == {"z": 2, "a": 2, "b": 8, "m": 9}


# ---------------------------------------------------------------------------
# Conflicts, including nested ones
# ---------------------------------------------------------------------------


def test_merge_returns_all_conflicts_sorted_with_no_partial_document():
    base = {
        "bothChange": 1,
        "leftMod": "a",
        "rightMod": "b",
        "nested": {"x": 0, "y": 0, "z": [1]},
        "deep": {"child": {"leaf": "v"}},
    }
    left = {
        "bothChange": 2,
        "leftMod": "L",
        "rightMod": "R-both",
        "nested": {"x": 10, "y": 11, "z": [1, 2]},
        "deep": {"child": {"leaf": "L"}},
    }
    right = {
        "bothChange": 3,
        "leftMod": "R-also",
        "rightMod": "R-both",
        "nested": {"x": 20, "y": 21, "z": [9]},
        "deep": {"child": {"leaf": "R"}},
    }

    resp = _merge(base, left, right)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "merged" not in body
    assert [c["path"] for c in body["conflicts"]] == [
        "/bothChange",
        "/deep/child/leaf",
        "/leftMod",
        "/nested/x",
        "/nested/y",
        "/nested/z",
    ]

    by_path = {c["path"]: c for c in body["conflicts"]}
    assert by_path["/bothChange"]["base"]["value"] == 1
    assert by_path["/bothChange"]["left"]["value"] == 2
    assert by_path["/bothChange"]["right"]["value"] == 3
    # Each side carries its presence marker.
    for c in body["conflicts"]:
        assert set(c["base"]) == {"present", "value"}
        assert set(c["left"]) == {"present", "value"}
        assert set(c["right"]) == {"present", "value"}
        assert c["base"]["present"] is True


def test_merge_nested_delete_vs_modify_is_a_conflict_at_the_member_path():
    base = {"a": {"b": {"c": 1, "d": 2}}, "keep": 0}
    left = {"a": {"b": {"c": 9, "d": 2}}, "keep": 0}
    # Right deletes the inner member while left modifies it.
    right = {"a": {"b": {"d": 2}}, "keep": 0}

    conflicts = _merge(base, left, right).json()["conflicts"]
    assert conflicts == [
        {
            "path": "/a/b/c",
            "base": {"present": True, "value": 1},
            "left": {"present": True, "value": 9},
            "right": {"present": False, "value": None},
        }
    ]

    # Mirror image: left deletes, right modifies.
    conflicts = _merge(base, right, left).json()["conflicts"]
    assert conflicts[0]["path"] == "/a/b/c"
    assert conflicts[0]["left"]["present"] is False
    assert conflicts[0]["right"] == {"present": True, "value": 9}


def test_merge_subtree_replacement_vs_member_edit_conflicts_at_the_root_path():
    base = {"a": {"b": {"c": 1}}, "other": 1}
    # Left replaces the whole subtree with a scalar; right edits a member.
    left = {"a": 42, "other": 1}
    right = {"a": {"b": {"c": 2}}, "other": 1}

    conflicts = _merge(base, left, right).json()["conflicts"]
    assert conflicts == [
        {
            "path": "/a",
            "base": {"present": True, "value": {"b": {"c": 1}}},
            "left": {"present": True, "value": 42},
            "right": {"present": True, "value": {"b": {"c": 2}}},
        }
    ]

    # Left deletes the subtree while right edits inside it: one conflict at
    # /a (not one per descendant).
    left = {"other": 1}
    right = {"a": {"b": {"c": 2}}, "other": 1}
    conflicts = _merge(base, left, right).json()["conflicts"]
    assert [c["path"] for c in conflicts] == ["/a"]
    assert conflicts[0]["left"]["present"] is False
    assert conflicts[0]["right"]["value"] == {"b": {"c": 2}}


def test_merge_divergent_adds_are_conflicts_with_absent_base():
    base = {"present": 1}
    left = {"present": 1, "new": "L"}
    right = {"present": 1, "new": "R"}
    conflict = _merge(base, left, right).json()["conflicts"][0]
    assert conflict == {
        "path": "/new",
        "base": {"present": False, "value": None},
        "left": {"present": True, "value": "L"},
        "right": {"present": True, "value": "R"},
    }


def test_merge_escaped_rfc6901_tokens_are_sorted_as_pointer_strings():
    base = {"a/b": 1, "a~b": 1, "a": {"x": 1}}
    left = {"a/b": 2, "a~b": 2, "a": {"x": 2}}
    right = {"a/b": 3, "a~b": 3, "a": {"x": 3}}
    conflicts = _merge(base, left, right).json()["conflicts"]
    # Sorting is over the RFC 6901 pointer strings, i.e. the escaped forms:
    # index 2 of '/a/x' is '/' (0x2F) and of '/a~0b' is '~' (0x7E), and
    # '~0' sorts before '~1'.
    assert [c["path"] for c in conflicts] == [
        "/a/x",
        "/a~0b",
        "/a~1b",
    ]


def test_merge_deeply_nested_conflicts_mix_with_unrelated_clean_edits():
    base = {
        "config": {
            "db": {"host": "h", "port": 5432, "tls": False},
            "cache": {"ttl": 60, "enabled": True},
        },
        "scalar": "v",
    }
    left = {
        "config": {
            "db": {"host": "hL", "port": 6432, "tls": False},
            "cache": {"ttl": 90, "enabled": True},
        },
        "scalar": "L",
    }
    right = {
        "config": {
            "db": {"host": "h2", "port": 5432, "tls": True},
            "cache": {"ttl": 120, "enabled": True},
        },
        "scalar": "v",
        "added": 9,
    }

    body = _merge(base, left, right).json()
    assert "merged" not in body
    paths = [c["path"] for c in body["conflicts"]]
    assert paths == [
        "/config/cache/ttl",
        "/config/db/host",
    ]
    host = next(c for c in body["conflicts"] if c["path"] == "/config/db/host")
    assert host["base"]["value"] == "h"
    assert host["left"]["value"] == "hL"
    assert host["right"]["value"] == "h2"
    ttl = next(c for c in body["conflicts"] if c["path"] == "/config/cache/ttl")
    assert ttl["base"]["value"] == 60
    assert ttl["left"]["value"] == 90
    assert ttl["right"]["value"] == 120


def test_merge_nested_clean_edits_on_sibling_paths_combine():
    base = {
        "config": {
            "db": {"host": "h", "port": 5432},
            "cache": {"ttl": 60, "enabled": True},
        },
        "scalar": "v",
    }
    left = {
        "config": {
            "db": {"host": "hL", "port": 5432},
            "cache": {"ttl": 60, "enabled": True},
        },
        "scalar": "v",
    }
    right = {
        "config": {
            "db": {"host": "h", "port": 6432},
            "cache": {"ttl": 120, "enabled": False},
        },
        "scalar": "R",
        "added": 9,
    }
    assert _merge(base, left, right).json() == {
        "merged": {
            "config": {
                "db": {"host": "hL", "port": 6432},
                "cache": {"ttl": 120, "enabled": False},
            },
            "scalar": "R",
            "added": 9,
        }
    }


# ---------------------------------------------------------------------------
# Validation / error contract
# ---------------------------------------------------------------------------


def test_merge_requires_three_documents_with_locs():
    resp = client.post("/merge", json={"left": {}, "right": {}})
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "base"

    resp = client.post("/merge", json={"base": {}, "right": {}})
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "left"

    resp = client.post("/merge", json={"base": {}, "left": {}})
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "right"

    resp = client.post("/merge", json=[1])
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "body"


def test_merge_uses_strict_json_parsing_and_value_validation():
    # Malformed JSON: same INVALID_JSON structure as other endpoints.
    resp = client.post(
        "/merge",
        content=b'{"base": {"a": 1},',
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_JSON"

    # Duplicate keys anywhere in the strict parse are rejected.
    resp = client.post(
        "/merge",
        content=b'{"base": {"a": 1, "a": 2}, "left": {}, "right": {}}',
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_JSON"

    # NaN is an unsupported JSON value, located at the offending side.
    resp = client.post(
        "/merge",
        content=b'{"base": 1, "left": NaN, "right": 1}',
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "UNSUPPORTED_VALUE"
    assert err["loc"] == "left"


def test_merge_does_not_mutate_input_documents():
    base = {"a": {"b": 1}, "arr": [1]}
    left = {"a": {"b": 2}, "arr": [1]}
    right = {"a": {"b": 1}, "arr": [1], "c": None}

    import copy

    base_snap = copy.deepcopy(base)
    left_snap = copy.deepcopy(left)
    right_snap = copy.deepcopy(right)
    resp = _merge(base, left, right)
    assert resp.status_code == 200
    assert base == base_snap
    assert left == left_snap
    assert right == right_snap

    # Also when conflicts are produced.
    left = {"a": {"b": 2}, "arr": [9]}
    right = {"a": {"b": 3}, "arr": [8]}
    _merge(base, left, right)
    assert base == base_snap
    assert left == {"a": {"b": 2}, "arr": [9]}
    assert right == {"a": {"b": 3}, "arr": [8]}


# ---------------------------------------------------------------------------
# Meta / OpenAPI
# ---------------------------------------------------------------------------


def test_merge_is_listed_in_service_metadata():
    info = client.get("/").json()
    assert info["endpoints"]["merge"] == "POST /merge"


def test_openapi_documents_merge_contract():
    schema = client.get("/openapi.json").json()
    op = schema["paths"]["/merge"]["post"]
    assert op["operationId"] == "merge"
    components = schema["components"]["schemas"]
    for name in (
        "MergeRequest",
        "MergeSide",
        "MergeConflict",
        "MergeSuccessResponse",
        "MergeConflictResponse",
    ):
        assert name in components
    assert "200" in op["responses"]
