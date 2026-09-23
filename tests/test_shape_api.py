"""End-to-end tests for POST /shape."""

import hashlib
import struct

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

U32 = struct.Struct(">I")


def _shape(document):
    return client.post("/shape", json={"document": document})


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_shape_manifest_lists_every_node_sorted_by_pointer():
    resp = _shape(
        {
            "subject": {"name": "Ada", "age": 36},
            "emails": ["ada@example.com", "ada@analytical.engine"],
            "empty": {},
            "a/b": {"~k": [True, None]},
        }
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["nodes"] == [
        {"path": "", "type": "object"},
        {"path": "/a~1b", "type": "object"},
        {"path": "/a~1b/~0k", "type": "array"},
        {"path": "/a~1b/~0k/0", "type": "boolean"},
        {"path": "/a~1b/~0k/1", "type": "null"},
        {"path": "/emails", "type": "array"},
        {"path": "/emails/0", "type": "string"},
        {"path": "/emails/1", "type": "string"},
        {"path": "/empty", "type": "object"},
        {"path": "/subject", "type": "object"},
        {"path": "/subject/age", "type": "number"},
        {"path": "/subject/name", "type": "string"},
    ]
    assert data["nodeCount"] == len(data["nodes"])
    assert len(data["shapeHash"]) == 64
    int(data["shapeHash"], 16)


def test_shape_hash_matches_hand_computed_preimage():
    # Locks the digest construction: domain tags, u32be counts, sorted
    # length-prefixed keys, scalar values absent from every preimage.
    h_num = hashlib.sha256(b"\x12").digest()
    h_true = hashlib.sha256(b"\x11").digest()
    h_null = hashlib.sha256(b"\x10").digest()
    h_empty_obj = hashlib.sha256(b"\x14").digest()
    h_arr = hashlib.sha256(b"\x17" + U32.pack(2) + h_true + h_null).digest()
    expected = _sha256(
        b"\x16" + U32.pack(3)
        + U32.pack(1) + b"a" + h_num
        + U32.pack(1) + b"b" + h_arr
        + U32.pack(1) + b"c" + h_empty_obj
    )

    resp = _shape({"a": 1, "b": [True, None], "c": {}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["shapeHash"] == expected


def test_scalar_values_do_not_affect_shape_hash():
    first = _shape({"a": 1, "b": "x", "c": True, "d": None}).json()
    second = _shape({"a": 2, "b": "y", "c": False, "d": None}).json()
    assert first == second  # manifest and hash are value-free

    # 1 and 1.0 are both JSON numbers: same shape even though the canonical
    # value bytes used by /commit and /diff differ.
    assert (
        _shape({"a": 1}).json()["shapeHash"]
        == _shape({"a": 1.0}).json()["shapeHash"]
    )


def test_node_type_change_changes_shape_hash():
    assert (
        _shape({"a": 1}).json()["shapeHash"]
        != _shape({"a": "1"}).json()["shapeHash"]
    )
    assert (
        _shape({"a": None}).json()["shapeHash"]
        != _shape({"a": False}).json()["shapeHash"]
    )
    assert (
        _shape({"a": 1}).json()["shapeHash"]
        != _shape({"a": True}).json()["shapeHash"]
    )


def test_object_key_order_does_not_affect_shape_hash():
    first = _shape({"a": 1, "b": [1], "é": {"z": None}}).json()["shapeHash"]
    second = _shape({"é": {"z": None}, "b": [1], "a": 1}).json()["shapeHash"]
    assert first == second


def test_object_key_names_participate_in_shape_hash():
    assert (
        _shape({"a": 1}).json()["shapeHash"]
        != _shape({"b": 1}).json()["shapeHash"]
    )


def test_array_order_is_significant():
    # Both arrays hold one number and one string; only the order differs.
    assert (
        _shape([1, "x"]).json()["shapeHash"]
        != _shape(["x", 1]).json()["shapeHash"]
    )
    assert (
        _shape([[1], ["x"]]).json()["shapeHash"]
        != _shape([["x"], [1]]).json()["shapeHash"]
    )


def test_empty_containers_have_distinct_encodings():
    assert (
        _shape({}).json()["shapeHash"] != _shape([]).json()["shapeHash"]
    )
    assert (
        _shape({"a": {}}).json()["shapeHash"]
        != _shape({"a": []}).json()["shapeHash"]
    )
    # An empty object is not the same shape as a non-empty one.
    assert (
        _shape({}).json()["shapeHash"]
        != _shape({"a": 1}).json()["shapeHash"]
    )
    data = _shape({}).json()
    assert data["nodes"] == [{"path": "", "type": "object"}]
    assert data["shapeHash"] == _sha256(b"\x14")


def test_root_scalar_and_empty_documents():
    assert _shape(42).json() == {
        "shapeHash": _sha256(b"\x12"),
        "nodeCount": 1,
        "nodes": [{"path": "", "type": "number"}],
    }
    assert _shape("x").json()["shapeHash"] == _sha256(b"\x13")
    assert _shape(None).json()["shapeHash"] == _sha256(b"\x10")
    assert _shape(False).json()["shapeHash"] == _sha256(b"\x11")
    assert _shape([]).json()["nodes"] == [{"path": "", "type": "array"}]


def test_same_shape_documents_compare_equal_end_to_end():
    before = {
        "subject": {"name": "Ada", "age": 36},
        "emails": ["ada@example.com", "ada@analytical.engine"],
        "empty": {},
    }
    same_shape = {
        "subject": {"name": "Bob", "age": 41},
        "emails": ["bob@example.com", "bob@other.io"],
        "empty": {},
    }
    different_shape = {
        "subject": {"name": "Bob", "age": "41"},
        "emails": ["bob@example.com", "bob@other.io"],
        "empty": {},
    }
    assert (
        _shape(before).json()["shapeHash"]
        == _shape(same_shape).json()["shapeHash"]
    )
    assert (
        _shape(before).json()["shapeHash"]
        != _shape(different_shape).json()["shapeHash"]
    )


def test_non_object_body_is_rejected():
    resp = client.post("/shape", json=[1, 2])
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "INVALID_REQUEST"
    assert err["loc"] == "body"


def test_missing_document_is_rejected():
    resp = client.post("/shape", json={})
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "INVALID_REQUEST"
    assert err["loc"] == "document"


def test_non_finite_number_is_rejected_at_document_loc():
    resp = client.post(
        "/shape",
        content=b'{"document": {"a": NaN}}',
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "UNSUPPORTED_VALUE"
    assert err["loc"] == "document.a"


def test_openapi_documents_shape_contract():
    schema = client.get("/openapi.json").json()
    op = schema["paths"]["/shape"]["post"]
    assert op["operationId"] == "shape"
    assert "ShapeRequest" in schema["components"]["schemas"]
    assert "ShapeResponse" in schema["components"]["schemas"]
    assert "ShapeNodeModel" in schema["components"]["schemas"]
    assert "200" in op["responses"]
