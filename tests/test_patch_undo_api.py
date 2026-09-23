"""End-to-end tests for POST /patch/undo (inverse RFC 6902 operations)."""

import copy
import random

from fastapi.testclient import TestClient

from app.main import app
from app.canonical import canonical_json_bytes

client = TestClient(app)


def _undo(document, operations):
    return client.post(
        "/patch/undo",
        json={"document": document, "operations": operations},
    )


def _patch(document, operations):
    return client.post(
        "/patch", json={"document": document, "operations": operations}
    )


def _replay(document, operations):
    """Apply operations via the real /patch endpoint and return the document."""
    resp = _patch(document, operations)
    assert resp.status_code == 200, resp.text
    return resp.json()["document"]


# ---------------------------------------------------------------------------
# Basic contract
# ---------------------------------------------------------------------------


def test_response_matches_patch_plus_undo_operations():
    doc = {"a": 1, "xs": ["a", "b"], "gone": "v"}
    ops = [
        {"op": "add", "path": "/c", "value": [1, 2]},
        {"op": "replace", "path": "/a", "value": 2},
        {"op": "remove", "path": "/gone"},
    ]
    resp = _undo(doc, ops)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # document/changedPaths are exactly what /patch would return.
    patch_body = _patch(doc, ops).json()
    assert body["document"] == patch_body["document"]
    assert body["changedPaths"] == patch_body["changedPaths"]

    # One inverse per step, reverse order.
    assert [op["op"] for op in body["undoOperations"]] == [
        "add",
        "replace",
        "remove",
    ]
    # Replaying the undo through /patch restores the original document.
    restored = _replay(body["document"], body["undoOperations"])
    assert restored == doc
    # The submitted document is never mutated.
    assert doc == {"a": 1, "xs": ["a", "b"], "gone": "v"}


def test_empty_operations_yields_empty_undo():
    doc = {"a": [1, 2], "b": {"c": {}}}
    resp = _undo(copy.deepcopy(doc), [])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "document": doc,
        "changedPaths": [],
        "undoOperations": [],
    }


# ---------------------------------------------------------------------------
# Array add: real numeric indices (including '-')
# ---------------------------------------------------------------------------


def test_array_dash_append_is_undone_at_numeric_index():
    doc = {"xs": ["a", "b"]}
    ops = [{"op": "add", "path": "/xs/-", "value": "c"}]
    body = _undo(doc, ops).json()
    assert body["document"] == {"xs": ["a", "b", "c"]}
    assert body["undoOperations"] == [
        {"op": "remove", "path": "/xs/2"}
    ]
    assert "-" not in body["undoOperations"][0]["path"]
    assert _replay(body["document"], body["undoOperations"]) == doc


def test_array_insert_at_length_is_undone_at_numeric_index():
    doc = {"xs": ["a", "b"]}
    ops = [{"op": "add", "path": "/xs/2", "value": "c"}]
    body = _undo(doc, ops).json()
    assert body["undoOperations"] == [
        {"op": "remove", "path": "/xs/2"}
    ]
    assert _replay(body["document"], body["undoOperations"]) == doc


def test_multiple_array_inserts_undo_in_reverse_with_stable_indices():
    doc = {"xs": ["a", "b"]}
    ops = [
        {"op": "add", "path": "/xs/0", "value": "z"},   # z a b
        {"op": "add", "path": "/xs/2", "value": "m"},   # z a m b
        {"op": "add", "path": "/xs/-", "value": "c"},   # z a m b c
    ]
    body = _undo(doc, ops).json()
    assert body["document"] == {"xs": ["z", "a", "m", "b", "c"]}
    # Reverse order: the last append (/xs/4) is removed first, so the
    # earlier recorded indices stay valid as the array shrinks.
    assert body["undoOperations"] == [
        {"op": "remove", "path": "/xs/4"},
        {"op": "remove", "path": "/xs/2"},
        {"op": "remove", "path": "/xs/0"},
    ]
    assert _replay(body["document"], body["undoOperations"]) == doc


def test_array_remove_and_replace_inverses():
    doc = {"xs": ["a", "b", "c"], "ys": [1, 2]}
    ops = [
        {"op": "remove", "path": "/xs/1"},
        {"op": "replace", "path": "/ys/0", "value": 9},
    ]
    body = _undo(doc, ops).json()
    assert body["undoOperations"] == [
        {"op": "replace", "path": "/ys/0", "value": 1},
        {"op": "add", "path": "/xs/1", "value": "b"},
    ]
    assert _replay(body["document"], body["undoOperations"]) == doc


# ---------------------------------------------------------------------------
# Object add over an existing member restores the old value
# ---------------------------------------------------------------------------


def test_add_to_existing_member_undo_replaces_old_value():
    doc = {"k": 1}
    ops = [{"op": "add", "path": "/k", "value": 2}]
    body = _undo(doc, ops).json()
    # The inverse must restore the old value ('replace'), not delete the
    # member ('remove').
    assert body["undoOperations"] == [
        {"op": "replace", "path": "/k", "value": 1}
    ]
    assert _replay(body["document"], body["undoOperations"]) == doc


def test_add_new_member_undo_removes_it():
    doc = {"a": 1}
    ops = [{"op": "add", "path": "/b", "value": {"nested": [1]}}]
    body = _undo(doc, ops).json()
    assert body["undoOperations"] == [
        {"op": "remove", "path": "/b"}
    ]
    assert _replay(body["document"], body["undoOperations"]) == doc


def test_escaped_member_names_round_trip():
    doc = {"a/b": 1, "tilde~key": 2}
    ops = [
        {"op": "replace", "path": "/a~1b", "value": 10},
        {"op": "add", "path": "/x~0y~1z", "value": 3},
        {"op": "remove", "path": "/tilde~0key"},
    ]
    body = _undo(doc, ops).json()
    assert body["undoOperations"] == [
        {"op": "add", "path": "/tilde~0key", "value": 2},
        {"op": "remove", "path": "/x~0y~1z"},
        {"op": "replace", "path": "/a~1b", "value": 1},
    ]
    assert _replay(body["document"], body["undoOperations"]) == doc


# ---------------------------------------------------------------------------
# Root-path operations restore the previous whole document
# ---------------------------------------------------------------------------


def test_root_replace_undo_restores_previous_document():
    doc = {"a": 1, "nested": {"x": [1, 2]}}
    ops = [{"op": "replace", "path": "", "value": [1, 2, 3]}]
    body = _undo(doc, ops).json()
    assert body["document"] == [1, 2, 3]
    assert body["undoOperations"] == [
        {"op": "replace", "path": "", "value": doc}
    ]
    assert _replay(body["document"], body["undoOperations"]) == doc


def test_root_add_has_same_inverse_as_root_replace():
    doc = 42
    ops = [{"op": "add", "path": "", "value": {"a": 1}}]
    body = _undo(doc, ops).json()
    assert body["undoOperations"] == [
        {"op": "replace", "path": "", "value": 42}
    ]
    assert _replay(body["document"], body["undoOperations"]) == doc


def test_root_replacement_mid_sequence_still_rolls_back_everything():
    # Replace the root, then edit the *new* document. Undo replays the later
    # inverses first, then the root replacement's own inverse — the document
    # state before that step, i.e. the original document.
    doc = {"a": 1}
    ops = [
        {"op": "replace", "path": "", "value": {"xs": ["a", "b"]}},
        {"op": "add", "path": "/xs/-", "value": "c"},
        {"op": "add", "path": "/k", "value": 9},
    ]
    body = _undo(doc, ops).json()
    assert body["document"] == {"xs": ["a", "b", "c"], "k": 9}
    assert body["undoOperations"] == [
        {"op": "remove", "path": "/k"},
        {"op": "remove", "path": "/xs/2"},
        {"op": "replace", "path": "", "value": {"a": 1}},
    ]
    assert _replay(body["document"], body["undoOperations"]) == doc


# ---------------------------------------------------------------------------
# Interacting steps
# ---------------------------------------------------------------------------


def test_operations_that_create_and_later_remove_paths_undo_cleanly():
    doc = {}
    ops = [
        {"op": "add", "path": "/xs", "value": []},
        {"op": "add", "path": "/xs/-", "value": "v"},
        {"op": "add", "path": "/tmp", "value": 1},
        {"op": "remove", "path": "/tmp"},
    ]
    body = _undo(doc, ops).json()
    assert body["document"] == {"xs": ["v"]}
    # The add of /tmp and its later remove cancel out, but both inverses are
    # still present and replay safely (add /tmp then remove /xs/0 then
    # remove /xs — see the reversed list below).
    assert _replay(body["document"], body["undoOperations"]) == doc


def test_nested_container_values_are_deep_copied_into_undo():
    doc = {"a": {"nested": [1]}}
    ops = [{"op": "replace", "path": "/a", "value": {"nested": [2]}}]
    body = _undo(doc, ops).json()
    # The undo snapshot is independent of the caller's document: mutating the
    # input after the request must not alter the captured old value.
    doc["a"]["nested"].append(999)
    assert body["undoOperations"] == [
        {"op": "replace", "path": "/a", "value": {"nested": [1]}}
    ]
    restored = _replay(body["document"], body["undoOperations"])
    assert restored == {"a": {"nested": [1]}}


# ---------------------------------------------------------------------------
# Canonical round trips (1 vs 1.0 must both survive undo)
# ---------------------------------------------------------------------------


def test_canonically_distinct_numbers_survive_undo():
    doc = {"a": 1}
    ops = [{"op": "replace", "path": "/a", "value": 1.0}]
    body = _undo(doc, ops).json()
    restored = _replay(body["document"], body["undoOperations"])
    assert canonical_json_bytes(restored) == canonical_json_bytes(doc)
    # Undo goes back to the integer 1, not 1.0.
    assert isinstance(body["undoOperations"][0]["value"], int)


# ---------------------------------------------------------------------------
# Errors: same localization and atomicity as /patch
# ---------------------------------------------------------------------------


def test_failure_uses_patch_error_loc_and_returns_no_partial_result():
    doc = {"a": 1, "xs": [1]}
    resp = _undo(
        doc,
        [
            {"op": "replace", "path": "/a", "value": 2},
            {"op": "remove", "path": "/missing"},
        ],
    )
    assert resp.status_code == 400
    body = resp.json()
    err = body["error"]
    assert err["code"] == "PATH_NOT_FOUND"
    assert err["loc"] == "operations[1]"
    assert "document" not in body
    assert "undoOperations" not in body
    assert doc == {"a": 1, "xs": [1]}


def test_envelope_errors_match_patch_locations():
    doc = {"a": 1}

    resp = _undo(doc, [{"op": "move", "path": "/a", "from": "/b"}])
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "operations[0].op"

    resp = _undo(doc, [{"op": "add", "path": "/bad~x", "value": 1}])
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "POINTER_SYNTAX"
    assert err["loc"] == "operations[0]"

    resp = _undo(doc, [{"op": "add", "path": "/b"}])
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "operations[0].value"


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
# Property-style randomized round trips
# ---------------------------------------------------------------------------


def _random_doc(rng, depth=0):
    if depth >= 3:
        return rng.choice([1, "s", None, True])
    kind = rng.randrange(6)
    if kind == 0:
        return rng.randrange(100)
    if kind == 1:
        return rng.choice(["a", "b", "c"])
    if kind == 2:
        return [_random_doc(rng, depth + 1) for _ in range(rng.randrange(4))]
    if kind == 3:
        return {
            f"k{i}": _random_doc(rng, depth + 1)
            for i in range(rng.randrange(4))
        }
    return rng.choice([None, True, 3.5])


def _random_operations(rng, doc):
    operations = []
    cur = copy.deepcopy(doc)
    for _ in range(rng.randrange(1, 8)):
        if isinstance(cur, dict):
            keys = list(cur)
            if keys and rng.random() < 0.8:
                key = rng.choice(keys)
                op = rng.choice(["remove", "replace"])
                if op == "remove":
                    operations.append({"op": "remove", "path": "/" + key})
                    del cur[key]
                    continue
                value = _random_doc(rng)
                operations.append(
                    {"op": "replace", "path": "/" + key, "value": value}
                )
                cur[key] = value
                continue
            key = f"new{rng.randrange(5)}"
            value = _random_doc(rng)
            operations.append(
                {"op": "add", "path": "/" + key, "value": value}
            )
            cur[key] = value
        elif isinstance(cur, list):
            if cur and rng.random() < 0.7:
                idx = rng.randrange(len(cur))
                op = rng.choice(["remove", "replace"])
                if op == "remove":
                    operations.append({"op": "remove", "path": f"/{idx}"})
                    del cur[idx]
                    continue
                value = _random_doc(rng)
                operations.append(
                    {"op": "replace", "path": f"/{idx}", "value": value}
                )
                cur[idx] = value
            else:
                token = "-" if rng.random() < 0.5 else str(len(cur))
                value = _random_doc(rng)
                operations.append(
                    {"op": "add", "path": "/" + token, "value": value}
                )
                cur.append(value)
        else:
            # Keep `cur` independent of the object already captured in
            # `operations`; otherwise a later in-place mutation of the
            # container would rewrite the value of this recorded op.
            value = _random_doc(rng)
            operations.append(
                {"op": "replace", "path": "", "value": copy.deepcopy(value)}
            )
            cur = value
    return operations


def test_randomized_patches_undo_to_original():
    rng = random.Random(20260923)
    for _ in range(60):
        doc = _random_doc(rng)
        ops = _random_operations(rng, doc)
        resp = _undo(doc, ops)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Every inverse is a legal /patch envelope with numeric array tokens.
        for inv in body["undoOperations"]:
            assert inv["op"] in ("add", "remove", "replace")
            assert "-" not in inv["path"].split("/")[1:]
        restored = _replay(body["document"], body["undoOperations"])
        assert canonical_json_bytes(restored) == canonical_json_bytes(doc)


# ---------------------------------------------------------------------------
# Meta / OpenAPI
# ---------------------------------------------------------------------------


def test_patch_undo_is_listed_in_service_metadata():
    info = client.get("/").json()
    assert info["endpoints"]["patchUndo"] == "POST /patch/undo"


def test_openapi_documents_patch_undo_contract():
    schema = client.get("/openapi.json").json()
    op = schema["paths"]["/patch/undo"]["post"]
    assert op["operationId"] == "patchUndo"
    assert "PatchUndoResponse" in schema["components"]["schemas"]
    assert "200" in op["responses"]
