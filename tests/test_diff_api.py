"""End-to-end tests for POST /diff."""

import hashlib

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def fp(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _diff(before, after, paths=None):
    payload = {"before": before, "after": after}
    if paths is not None:
        payload["paths"] = paths
    return client.post("/diff", json=payload)


def test_diff_reports_sorted_added_removed_and_changed_leaves():
    before = {
        "removed": "old",
        "changed": 1,
        "same": True,
        "nested": {"x": 1, "y": 2},
        "array": [1, 2],
        "emptyBefore": {},
    }
    after = {
        "added": "new",
        "changed": 2,
        "same": True,
        "nested": {"x": 1, "z": 3},
        "array": [1, 3],
        "emptyAfter": [],
    }

    resp = _diff(before, after)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert [entry["path"] for entry in data["added"]] == [
        "/added",
        "/emptyAfter",
        "/nested/z",
    ]
    assert [entry["path"] for entry in data["removed"]] == [
        "/emptyBefore",
        "/nested/y",
    ]
    assert [entry["path"] for entry in data["changed"]] == [
        "/array/1",
        "/changed",
    ]
    assert data["added"][0] == {
        "path": "/added",
        "beforeValue": None,
        "afterValue": "new",
        "beforeFingerprint": None,
        "afterFingerprint": fp('"new"'),
    }
    changed = next(item for item in data["changed"] if item["path"] == "/changed")
    assert changed == {
        "path": "/changed",
        "beforeValue": 1,
        "afterValue": 2,
        "beforeFingerprint": fp("1"),
        "afterFingerprint": fp("2"),
    }


def test_diff_uses_canonical_value_fingerprints():
    # Python compares 1 == 1.0 and True == 1, but canonical JSON does not.
    resp = _diff({"a": 1, "b": True}, {"a": 1.0, "b": 1})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    values = {entry["path"]: entry for entry in data["changed"]}
    assert values["/a"]["beforeFingerprint"] == fp("1")
    assert values["/a"]["afterFingerprint"] == fp("1.0")
    assert values["/b"]["beforeFingerprint"] == fp("true")
    assert values["/b"]["afterFingerprint"] == fp("1")


def test_diff_null_value_has_a_fingerprint_but_absence_is_null():
    resp = _diff({"actualNull": None, "removed": None}, {"actualNull": 0})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    changed = body["changed"][0]
    removed = body["removed"][0]

    assert changed["path"] == "/actualNull"
    assert changed["beforeValue"] is None
    assert changed["beforeFingerprint"] == fp("null")
    assert changed["afterValue"] == 0
    assert changed["afterFingerprint"] == fp("0")

    assert removed["path"] == "/removed"
    assert removed["beforeValue"] is None
    assert removed["beforeFingerprint"] == fp("null")
    assert removed["afterValue"] is None
    assert removed["afterFingerprint"] is None


def test_diff_empty_documents_and_empty_containers_follow_leaf_rules():
    assert _diff({}, {}).json() == {"added": [], "removed": [], "changed": []}

    resp = _diff({}, {"empty": {}, "scalar": "x"})
    assert resp.status_code == 200
    body = resp.json()
    assert [entry["path"] for entry in body["added"]] == ["/empty", "/scalar"]
    assert body["added"][0]["afterValue"] == {}
    assert body["added"][0]["afterFingerprint"] == fp("{}")

    assert _diff({"a": {"deep": []}}, {}).json()["removed"] == [
        {
            "path": "/a/deep",
            "beforeValue": [],
            "afterValue": None,
            "beforeFingerprint": fp("[]"),
            "afterFingerprint": None,
        }
    ]


def test_diff_root_scalars_and_empty_roots_use_empty_pointer():
    assert _diff(42, 42).json() == {"added": [], "removed": [], "changed": []}

    resp = _diff(42, "x")
    assert resp.status_code == 200
    assert resp.json()["changed"] == [
        {
            "path": "",
            "beforeValue": 42,
            "afterValue": "x",
            "beforeFingerprint": fp("42"),
            "afterFingerprint": fp('"x"'),
        }
    ]

    resp = _diff({}, [])
    assert resp.status_code == 200
    assert resp.json()["changed"][0]["path"] == ""
    assert resp.json()["changed"][0]["beforeFingerprint"] == fp("{}")
    assert resp.json()["changed"][0]["afterFingerprint"] == fp("[]")


def test_diff_uses_escaped_rfc6901_pointers():
    before = {"a/b": 1, "tilde~key": 2}
    after = {"a/b": 1, "tilde~key": 3}

    resp = _diff(before, after, ["/tilde~0key"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["changed"] == [
        {
            "path": "/tilde~0key",
            "beforeValue": 2,
            "afterValue": 3,
            "beforeFingerprint": fp("2"),
            "afterFingerprint": fp("3"),
        }
    ]

    assert _diff(before, after).json()["added"] == []


def test_diff_paths_filter_accepts_exact_leaves_on_either_side():
    before = {"a": 1, "nested": {"x": 1}, "gone": {}}
    after = {"a": 2, "nested": {"x": 1}, "added": []}

    # Shuffled input must not affect path-sorted response groups.
    resp = _diff(before, after, ["/gone", "/added", "/a"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [entry["path"] for entry in body["added"]] == ["/added"]
    assert [entry["path"] for entry in body["removed"]] == ["/gone"]
    assert [entry["path"] for entry in body["changed"]] == ["/a"]

    # The duplicate is rejected before any result is produced.
    resp = _diff(before, after, ["/a", "/added", "/gone", "/a"])
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "DUPLICATE_PATH"
    assert err["loc"] == "paths[3]"

    # An empty filter selects nothing; omitting paths selects everything.
    assert _diff(before, after, []).json() == {
        "added": [],
        "removed": [],
        "changed": [],
    }


def test_diff_paths_filter_rejects_nonleaf_missing_and_malformed_entries():
    before = {"container": {"leaf": 1}, "empty": {}}
    after = {"other": 1}

    resp = _diff(before, after, ["/container"])
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "PATH_NOT_LEAF"
    assert err["loc"] == "paths[0]"
    assert err["detail"]["path"] == "/container"
    assert err["detail"]["leafChildren"] == ["/container/leaf"]

    # A path that is a non-empty container in either document is rejected.
    assert (
        _diff({"a": {}}, {"a": {"leaf": 1}}, ["/a"]).status_code == 400
    )

    # Empty containers remain exact leaf paths.
    assert _diff(before, after, ["/empty"]).status_code == 200

    resp = _diff(before, after, ["/missing"])
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "PATH_NOT_FOUND"
    assert err["loc"] == "paths[0]"

    resp = _diff(before, after, ["/bad~x"])
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "POINTER_SYNTAX"
    assert err["loc"] == "paths[0]"

    resp = _diff(before, after, [42])
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "paths[0]"

    resp = _diff(before, after, [None])
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "paths[0]"

    resp = _diff(before, after, "/container")
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "paths"


def test_diff_root_filter_is_leaf_only():
    assert _diff(42, 43, [""]).status_code == 200

    # Root is a leaf on one side and replaced by a container on the other:
    # the exact root leaf remains a valid removed/added filter entry.
    resp = _diff(42, {"a": 1}, [""])
    assert resp.status_code == 200
    assert [entry["path"] for entry in resp.json()["removed"]] == [""]

    # The same rule applies below the root.
    resp = _diff({"a": 1}, {"a": {"b": 2}}, ["/a"])
    assert resp.status_code == 200
    assert [entry["path"] for entry in resp.json()["removed"]] == ["/a"]

    resp = _diff({"a": 1}, {"b": 2}, [""])
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "PATH_NOT_LEAF"
    assert err["loc"] == "paths[0]"


def test_diff_validates_request_as_one_atomic_operation():
    # Missing sides use the same error body/loc convention.
    resp = client.post("/diff", json={"after": {}})
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "before"

    resp = client.post("/diff", json={"before": {}, "after": {}})
    assert resp.status_code == 200

    # Malformed JSON means no partial diff result.
    resp = client.post("/diff", content=b'{"before": {},', headers={})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_JSON"

    # Duplicate keys in either document are rejected by the strict parser.
    resp = client.post(
        "/diff",
        content=b'{"before": {"a": 1, "a": 2}, "after": {}}',
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_JSON"
