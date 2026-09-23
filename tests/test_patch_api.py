"""End-to-end tests for POST /patch (RFC 6902 add/remove/replace)."""

import copy

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _patch(document, operations):
    return client.post(
        "/patch", json={"document": document, "operations": operations}
    )


# ---------------------------------------------------------------------------
# Object operations
# ---------------------------------------------------------------------------


def test_add_replace_remove_object_members_and_changed_paths():
    doc = {"a": 1, "b": {"x": 10}, "gone": "v"}
    ops = [
        {"op": "add", "path": "/c", "value": [1, 2]},
        {"op": "replace", "path": "/b/x", "value": 11},
        {"op": "remove", "path": "/gone"},
    ]
    resp = _patch(doc, ops)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["document"] == {
        "a": 1,
        "b": {"x": 11},
        "c": [1, 2],
    }
    assert body["changedPaths"] == ["/b/x", "/c/0", "/c/1", "/gone"]
    # The input document must not be mutated.
    assert doc == {"a": 1, "b": {"x": 10}, "gone": "v"}


def test_add_to_existing_member_is_a_replace():
    resp = _patch({"a": 1}, [{"op": "add", "path": "/a", "value": 2}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"] == {"a": 2}
    assert resp.json()["changedPaths"] == ["/a"]


def test_add_value_is_deep_copied():
    value = {"nested": [1]}
    resp = _patch({}, [{"op": "add", "path": "/k", "value": value}])
    assert resp.status_code == 200, resp.text
    value["nested"].append(2)  # mutate the caller-side object afterwards
    assert resp.json()["document"] == {"k": {"nested": [1]}}


# ---------------------------------------------------------------------------
# Array operations
# ---------------------------------------------------------------------------


def test_array_add_insert_prepend_append_and_dash():
    doc = {"xs": ["a", "b"]}

    resp = _patch(doc, [{"op": "add", "path": "/xs/1", "value": "m"}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"] == {"xs": ["a", "m", "b"]}
    # Insertion shifts later elements, so both the inserted leaf and the
    # shifted leaf are reported as changed.
    assert resp.json()["changedPaths"] == ["/xs/1", "/xs/2"]

    resp = _patch(doc, [{"op": "add", "path": "/xs/0", "value": "z"}])
    assert resp.json()["document"] == {"xs": ["z", "a", "b"]}

    resp = _patch(doc, [{"op": "add", "path": "/xs/-", "value": "c"}])
    assert resp.json()["document"] == {"xs": ["a", "b", "c"]}
    assert resp.json()["changedPaths"] == ["/xs/2"]

    # Insert at index == length (without '-') also appends.
    resp = _patch(doc, [{"op": "add", "path": "/xs/2", "value": "c"}])
    assert resp.json()["document"] == {"xs": ["a", "b", "c"]}

    # The original document stays untouched across all requests.
    assert doc == {"xs": ["a", "b"]}


def test_array_remove_and_replace_shift_and_changed_paths():
    doc = {"xs": ["a", "b", "c"]}

    resp = _patch(doc, [{"op": "remove", "path": "/xs/1"}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"] == {"xs": ["a", "c"]}
    assert resp.json()["changedPaths"] == ["/xs/1", "/xs/2"]

    resp = _patch(doc, [{"op": "replace", "path": "/xs/2", "value": "C"}])
    assert resp.json()["document"] == {"xs": ["a", "b", "C"]}
    assert resp.json()["changedPaths"] == ["/xs/2"]


def test_index_zero_is_valid_and_nested_arrays_work():
    doc = {"m": [[0]]}
    resp = _patch(
        doc,
        [
            {"op": "replace", "path": "/m/0/0", "value": 9},
            {"op": "remove", "path": "/m/0"},
        ],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"] == {"m": []}
    # The replaced inner element /m/0/0 vanishes with the inner array, and
    # the outer array becomes the empty-array leaf /m.
    assert resp.json()["changedPaths"] == ["/m", "/m/0/0"]


# ---------------------------------------------------------------------------
# Operation ordering
# ---------------------------------------------------------------------------


def test_operations_apply_in_order_and_interact():
    # The second op depends on the array created by the first; a later op
    # removes a member an earlier op added.
    doc = {}
    ops = [
        {"op": "add", "path": "/xs", "value": []},
        {"op": "add", "path": "/xs/-", "value": "v"},
        {"op": "add", "path": "/tmp", "value": 1},
        {"op": "remove", "path": "/tmp"},
    ]
    resp = _patch(doc, ops)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["document"] == {"xs": ["v"]}
    # The root leaf "" (the original empty object) is replaced by a container.
    assert body["changedPaths"] == ["", "/xs/0"]
    assert doc == {}


# ---------------------------------------------------------------------------
# Root document
# ---------------------------------------------------------------------------


def test_replace_whole_document_via_empty_pointer():
    resp = _patch(42, [{"op": "replace", "path": "", "value": {"a": 1}}])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["document"] == {"a": 1}
    assert body["changedPaths"] == ["", "/a"]


def test_remove_root_is_rejected():
    resp = _patch({"a": 1}, [{"op": "remove", "path": ""}])
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "INVALID_REQUEST"
    assert err["loc"] == "operations[0]"


def test_empty_operations_is_a_valid_noop():
    doc = {"a": [1, 2], "b": {"c": {}}}
    resp = _patch(copy.deepcopy(doc), [])
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"document": doc, "changedPaths": []}


# ---------------------------------------------------------------------------
# No-op patches
# ---------------------------------------------------------------------------


def test_canonically_unchanged_patch_reports_no_changed_paths():
    # Replacing with an equal value leaves the canonical document identical.
    resp = _patch({"a": 1}, [{"op": "replace", "path": "/a", "value": 1}])
    assert resp.status_code == 200
    assert resp.json()["changedPaths"] == []
    assert resp.json()["document"] == {"a": 1}


def test_canonical_value_changes_are_detected_int_vs_float():
    # 1 -> 1.0 is identical in Python equality but different canonical JSON.
    resp = _patch({"a": 1}, [{"op": "replace", "path": "/a", "value": 1.0}])
    assert resp.status_code == 200
    assert resp.json()["changedPaths"] == ["/a"]


# ---------------------------------------------------------------------------
# Escaped pointer tokens
# ---------------------------------------------------------------------------


def test_escaped_rfc6901_tokens_in_patch_paths():
    doc = {"a/b": 1, "tilde~key": 2}
    resp = _patch(
        doc,
        [
            {"op": "replace", "path": "/a~1b", "value": 10},
            {"op": "remove", "path": "/tilde~0key"},
            {"op": "add", "path": "/x~0y~1z", "value": 3},
        ],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"] == {"a/b": 10, "x~y/z": 3}
    assert resp.json()["changedPaths"] == ["/a~1b", "/tilde~0key", "/x~0y~1z"]


# ---------------------------------------------------------------------------
# Error localization and atomicity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "operations,expected_code,expected_loc",
    [
        # Missing object member / out-of-range element.
        ([{"op": "remove", "path": "/missing"}], "PATH_NOT_FOUND",
         "operations[0]"),
        ([{"op": "replace", "path": "/missing", "value": 1}],
         "PATH_NOT_FOUND", "operations[0]"),
        ([{"op": "replace", "path": "/xs/5", "value": 1}], "PATH_NOT_FOUND",
         "operations[0]"),
        ([{"op": "remove", "path": "/xs/3"}], "PATH_NOT_FOUND",
         "operations[0]"),
        # Invalid array tokens.
        ([{"op": "remove", "path": "/xs/-"}], "PATH_NOT_FOUND",
         "operations[0]"),
        ([{"op": "replace", "path": "/xs/-", "value": 1}], "PATH_NOT_FOUND",
         "operations[0]"),
        ([{"op": "add", "path": "/xs/01", "value": 1}], "PATH_NOT_FOUND",
         "operations[0]"),
        ([{"op": "add", "path": "/xs/9", "value": 1}], "PATH_NOT_FOUND",
         "operations[0]"),
        # Bad parent containers.
        ([{"op": "add", "path": "/a/child", "value": 1}], "PATH_NOT_FOUND",
         "operations[0]"),
        ([{"op": "remove", "path": "/missing/child"}], "PATH_NOT_FOUND",
         "operations[0]"),
        ([{"op": "add", "path": "/xs/0/x", "value": 1}], "PATH_NOT_FOUND",
         "operations[0]"),
        # Pointer syntax errors.
        ([{"op": "remove", "path": "no-leading-slash"}], "POINTER_SYNTAX",
         "operations[0]"),
        ([{"op": "remove", "path": "/bad~x"}], "POINTER_SYNTAX",
         "operations[0]"),
    ],
)
def test_invalid_targets_are_rejected_with_operations_loc(
    operations, expected_code, expected_loc
):
    doc = {"a": 1, "xs": ["a", "b", "c"]}
    resp = _patch(doc, operations)
    assert resp.status_code == 400, resp.text
    err = resp.json()["error"]
    assert err["code"] == expected_code
    assert err["loc"] == expected_loc
    assert err["detail"]["path"] == operations[0]["path"]


def test_error_loc_points_at_the_failing_operation_index():
    doc = {"xs": [1, 2]}
    ops = [
        {"op": "replace", "path": "/xs/0", "value": 0},
        {"op": "remove", "path": "/xs/9"},
    ]
    resp = _patch(doc, ops)
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "PATH_NOT_FOUND"
    assert err["loc"] == "operations[1]"


def test_earlier_envelope_error_is_reported_before_any_application():
    # Both envelopes are checked first; the malformed pointer at index 1 is a
    # structural error even though index 0 would apply fine.
    ops = [
        {"op": "add", "path": "/a", "value": 2},
        {"op": "remove", "path": "/bad~2"},
    ]
    resp = _patch({"a": 1}, ops)
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "POINTER_SYNTAX"
    assert err["loc"] == "operations[1]"


def test_no_partial_result_is_returned_on_failure():
    doc = {"a": 1, "xs": [1]}
    resp = _patch(
        doc,
        [
            {"op": "replace", "path": "/a", "value": 2},
            {"op": "remove", "path": "/missing"},
        ],
    )
    assert resp.status_code == 400
    body = resp.json()
    assert "document" not in body
    assert "changedPaths" not in body
    # The submitted document is untouched.
    assert doc == {"a": 1, "xs": [1]}


def test_unsupported_and_malformed_operation_envelopes():
    base = {"a": 1}

    resp = _patch(base, [{"op": "move", "path": "/a", "from": "/b"}])
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "INVALID_REQUEST"
    assert err["loc"] == "operations[0].op"

    resp = _patch(base, [{"path": "/a"}])
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "operations[0].op"

    resp = _patch(base, [{"op": "remove"}])
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "operations[0].path"

    resp = _patch(base, [{"op": "add", "path": "/b"}])
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "operations[0].value"

    resp = _patch(base, [{"op": 42, "path": "/a"}])
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "operations[0].op"

    resp = _patch(base, [{"op": "remove", "path": 7}])
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "operations[0].path"

    resp = _patch(base, ["remove"])
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "operations[0]"

    resp = _patch(base, {"op": "remove", "path": "/a"})
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "operations"


def test_non_finite_value_is_rejected_at_value_loc():
    resp = client.post(
        "/patch",
        content=b'{"document": {"a": 1}, "operations": ['
                b'{"op": "replace", "path": "/a", "value": NaN}]}',
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "UNSUPPORTED_VALUE"
    assert err["loc"] == "operations[0].value"


def test_request_level_errors_match_other_endpoints():
    resp = client.post("/patch", json={"operations": []})
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "document"

    resp = client.post("/patch", json={"document": {}})
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "operations"

    resp = client.post("/patch", json=[1])
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "body"

    resp = client.post(
        "/patch",
        content=b'{"document": {"a": 1},',
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_JSON"


# ---------------------------------------------------------------------------
# Meta / OpenAPI
# ---------------------------------------------------------------------------


def test_patch_is_listed_in_service_metadata():
    info = client.get("/").json()
    assert info["endpoints"]["patch"] == "POST /patch"


def test_openapi_documents_patch_contract():
    schema = client.get("/openapi.json").json()
    op = schema["paths"]["/patch"]["post"]
    assert op["operationId"] == "patch"
    assert "PatchRequest" in schema["components"]["schemas"]
    assert "PatchResponse" in schema["components"]["schemas"]
    assert "PatchOperation" in schema["components"]["schemas"]
    assert "200" in op["responses"]
