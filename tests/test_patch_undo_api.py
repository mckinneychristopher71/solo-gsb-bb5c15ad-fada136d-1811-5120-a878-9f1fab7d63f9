"""End-to-end tests for POST /patch/undo.

The contract under test: the response's ``undoOperations``, submitted to
``/patch`` together with the patched document, must restore the original
document. Failures must be identical to /patch (same code/loc, no partial
result).
"""

import copy

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _undo(document, operations):
    return client.post(
        "/patch/undo",
        json={"document": document, "operations": operations},
    )


def _roundtrip(document, operations):
    """Apply via /patch/undo, then replay undoOperations via /patch."""
    original = copy.deepcopy(document)
    resp = _undo(document, operations)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"document", "undoOperations"}

    # The returned undo list must itself be accepted by /patch.
    back = client.post(
        "/patch",
        json={
            "document": body["document"],
            "operations": body["undoOperations"],
        },
    )
    assert back.status_code == 200, back.text
    assert back.json()["document"] == original
    return body


# ---------------------------------------------------------------------------
# Round trips
# ---------------------------------------------------------------------------


def test_object_add_replace_remove_roundtrip():
    doc = {"a": 1, "b": {"x": 10}, "gone": "v"}
    ops = [
        {"op": "add", "path": "/c", "value": [1, 2]},
        {"op": "replace", "path": "/b/x", "value": 11},
        {"op": "remove", "path": "/gone"},
    ]
    body = _roundtrip(doc, ops)
    assert body["document"] == {
        "a": 1,
        "b": {"x": 11},
        "c": [1, 2],
    }
    # Inverse steps are emitted in reverse order.
    assert body["undoOperations"] == [
        {"op": "add", "path": "/gone", "value": "v"},
        {"op": "replace", "path": "/b/x", "value": 10},
        {"op": "remove", "path": "/c"},
    ]


def test_add_over_existing_object_member_restores_old_value():
    body = _roundtrip(
        {"a": {"nested": 1}},
        [{"op": "add", "path": "/a", "value": 2}],
    )
    # An add over an existing member is an overwrite, not an insertion: the
    # inverse must restore the old value rather than remove the member.
    assert body["undoOperations"] == [
        {"op": "add", "path": "/a", "value": {"nested": 1}}
    ]


def test_array_insert_dash_and_middle_index_use_numeric_remove():
    doc = {"xs": ["a", "b"]}
    body = _roundtrip(
        doc,
        [
            {"op": "add", "path": "/xs/-", "value": "c"},
            {"op": "add", "path": "/xs/1", "value": "m"},
            {"op": "add", "path": "/xs/0", "value": "z"},
        ],
    )
    # Every array insertion is undone at the concrete numeric position it
    # actually landed at; '-' never appears in an inverse path.
    assert body["undoOperations"] == [
        {"op": "remove", "path": "/xs/0"},
        {"op": "remove", "path": "/xs/1"},
        {"op": "remove", "path": "/xs/2"},
    ]
    assert all(
        not op["path"].endswith("/-") for op in body["undoOperations"]
    )


def test_array_insert_at_length_uses_numeric_index():
    body = _roundtrip(
        {"xs": ["a"]},
        [{"op": "add", "path": "/xs/1", "value": "b"}],
    )
    assert body["undoOperations"] == [{"op": "remove", "path": "/xs/1"}]


def test_array_remove_reinserts_at_original_index_with_shifts():
    doc = {"xs": ["a", "b", "c"]}
    body = _roundtrip(
        doc,
        [
            {"op": "remove", "path": "/xs/0"},
            {"op": "remove", "path": "/xs/1"},
        ],
    )
    # After the first removal ['b','c'], removing /xs/1 removes 'c'. The
    # reverse replay re-adds 'c' at 1, then 'a' at 0.
    assert body["undoOperations"] == [
        {"op": "add", "path": "/xs/1", "value": "c"},
        {"op": "add", "path": "/xs/0", "value": "a"},
    ]


def test_array_replace_roundtrip():
    body = _roundtrip(
        {"xs": [1, 2, 3]},
        [{"op": "replace", "path": "/xs/2", "value": 9}],
    )
    assert body["undoOperations"] == [
        {"op": "replace", "path": "/xs/2", "value": 3}
    ]


def test_mixed_interacting_operations_roundtrip():
    # The second op depends on the array created by the first; a later op
    # removes a member an earlier op added.
    doc = {}
    ops = [
        {"op": "add", "path": "/xs", "value": []},
        {"op": "add", "path": "/xs/-", "value": "v"},
        {"op": "add", "path": "/tmp", "value": 1},
        {"op": "remove", "path": "/tmp"},
    ]
    body = _roundtrip(doc, ops)
    assert body["document"] == {"xs": ["v"]}
    assert body["undoOperations"] == [
        # remove /tmp at forward step 3 -> re-add its value 1
        {"op": "add", "path": "/tmp", "value": 1},
        # add /tmp at step 2 had no member -> inverse remove
        {"op": "remove", "path": "/tmp"},
        # append to /xs -> numeric remove
        {"op": "remove", "path": "/xs/0"},
        # create /xs -> inverse remove
        {"op": "remove", "path": "/xs"},
    ]


def test_root_add_replace_restores_old_document():
    body = _roundtrip(
        42,
        [{"op": "replace", "path": "", "value": {"a": [1, 2]}}],
    )
    assert body["document"] == {"a": [1, 2]}
    assert body["undoOperations"] == [
        {"op": "add", "path": "", "value": 42}
    ]

    body = _roundtrip(
        {"a": 1},
        [{"op": "add", "path": "", "value": ["x", "y"]}],
    )
    assert body["undoOperations"] == [
        {"op": "add", "path": "", "value": {"a": 1}}
    ]


def test_empty_operations_returns_empty_undo():
    doc = {"a": [1, 2]}
    body = _roundtrip(copy.deepcopy(doc), [])
    assert body["document"] == doc
    assert body["undoOperations"] == []


def test_canonical_value_types_are_preserved():
    # 1 -> 1.0 is a real change and must round-trip back to the integer 1.
    body = _roundtrip(
        {"a": 1, "b": True, "c": None},
        [
            {"op": "replace", "path": "/a", "value": 1.0},
            {"op": "replace", "path": "/b", "value": False},
            {"op": "replace", "path": "/c", "value": "x"},
        ],
    )
    assert body["undoOperations"] == [
        {"op": "replace", "path": "/c", "value": None},
        {"op": "replace", "path": "/b", "value": True},
        {"op": "replace", "path": "/a", "value": 1},
    ]


def test_escaped_pointer_tokens_roundtrip():
    doc = {"a/b": 1, "tilde~key": 2}
    body = _roundtrip(
        doc,
        [
            {"op": "replace", "path": "/a~1b", "value": 10},
            {"op": "remove", "path": "/tilde~0key"},
            {"op": "add", "path": "/x~0y~1z", "value": 3},
        ],
    )
    assert body["undoOperations"] == [
        {"op": "remove", "path": "/x~0y~1z"},
        {"op": "add", "path": "/tilde~0key", "value": 2},
        {"op": "replace", "path": "/a~1b", "value": 1},
    ]


def test_nested_array_paths_are_rebuilt_correctly():
    doc = {"m": [[0]]}
    body = _roundtrip(
        doc,
        [
            {"op": "replace", "path": "/m/0/0", "value": 9},
            {"op": "add", "path": "/m/-", "value": [5]},
            {"op": "remove", "path": "/m/0"},
        ],
    )
    assert body["undoOperations"] == [
        {"op": "add", "path": "/m/0", "value": [9]},
        {"op": "remove", "path": "/m/1"},
        {"op": "replace", "path": "/m/0/0", "value": 0},
    ]


def test_input_document_is_not_mutated():
    doc = {"a": 1, "xs": [1]}
    ops = [
        {"op": "replace", "path": "/a", "value": 2},
        {"op": "add", "path": "/xs/-", "value": 2},
    ]
    resp = _undo(doc, ops)
    assert resp.status_code == 200, resp.text
    assert doc == {"a": 1, "xs": [1]}


def test_undo_values_are_deep_copied():
    doc = {"k": {"nested": [1]}}
    resp = _undo(doc, [{"op": "replace", "path": "/k", "value": 2}])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Mutate the caller-side original after the request; the recorded undo
    # value must be an independent deep copy.
    doc["k"]["nested"].append(2)
    assert body["undoOperations"] == [
        {"op": "replace", "path": "/k", "value": {"nested": [1]}}
    ]


# ---------------------------------------------------------------------------
# Errors: identical localization / atomicity as /patch
# ---------------------------------------------------------------------------


def test_semantic_failure_has_same_loc_as_patch_and_no_partial_result():
    doc = {"a": 1, "xs": [1]}
    ops = [
        {"op": "replace", "path": "/a", "value": 2},
        {"op": "remove", "path": "/missing"},
    ]
    resp = _undo(doc, ops)
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "PATH_NOT_FOUND"
    assert err["loc"] == "operations[1]"
    assert "document" not in resp.json()
    assert "undoOperations" not in resp.json()
    assert doc == {"a": 1, "xs": [1]}

    # /patch reports the identical error body.
    patch_resp = client.post(
        "/patch", json={"document": doc, "operations": ops}
    )
    assert patch_resp.status_code == 400
    assert patch_resp.json()["error"] == err


def test_structural_envelope_error_matches_patch():
    ops = [
        {"op": "add", "path": "/a", "value": 2},
        {"op": "remove", "path": "/bad~2"},
    ]
    resp = _undo({"a": 1}, ops)
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "POINTER_SYNTAX"
    assert err["loc"] == "operations[1]"


def test_root_remove_is_rejected():
    resp = _undo({"a": 1}, [{"op": "remove", "path": ""}])
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "INVALID_REQUEST"
    assert err["loc"] == "operations[0]"


def test_request_level_errors_match_patch():
    resp = client.post("/patch/undo", json={"operations": []})
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "document"

    resp = client.post("/patch/undo", json={"document": {}})
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "operations"

    resp = client.post("/patch/undo", json=[1])
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "body"


# ---------------------------------------------------------------------------
# Meta / OpenAPI
# ---------------------------------------------------------------------------


def test_patch_undo_is_listed_in_service_metadata():
    info = client.get("/").json()
    assert info["endpoints"]["patchUndo"] == "POST /patch/undo"


def test_openapi_documents_patch_undo_contract():
    schema = client.get("/openapi.json").json()
    op = schema["paths"]["/patch/undo"]["post"]
    assert op["operationId"] == "patch_undo"
    assert "PatchRequest" in schema["components"]["schemas"]
    assert "PatchUndoResponse" in schema["components"]["schemas"]
    assert "200" in op["responses"]
