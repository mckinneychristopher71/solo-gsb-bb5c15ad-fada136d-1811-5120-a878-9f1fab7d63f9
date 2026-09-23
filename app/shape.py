"""Structural shape manifest and bottom-up shape hash for JSON documents.

``POST /shape`` answers one question: *do two JSON documents have the same
shape?*  The shape of a document is its tree of node types plus the object
key names — scalar **values** never participate.  The module produces

* a **manifest**: every node of the document listed by its RFC 6901 pointer
  and JSON node type (no leaf values, so the manifest never carries
  payloads), and
* a **shape hash**: a SHA-256 digest computed bottom-up over the tree, so
  two documents are same-shaped exactly when their hashes are equal.

Digest construction (all hashes SHA-256, 32 bytes).  Every preimage starts
with a one-byte domain tag naming the node kind; container preimages add a
u32be child count and length-prefixed key names, so distinct trees can
never serialize to the same byte string (no concatenation ambiguity)::

    null          0x10
    boolean       0x11                          (true/false not hashed)
    number        0x12                          (value not hashed)
    string        0x13                          (value not hashed)
    empty object  0x14
    empty array   0x15
    object        0x16 || u32be(members)
                       || ( u32be(len(key_utf8)) || key_utf8 || child )*
                       with keys sorted by Unicode code point
    array         0x17 || u32be(elements) || child*   (document order)

Empty containers and scalar types each get their own tag (matching the
leaf model of the Merkle tree, where empty ``{}`` / ``[]`` are terminal).
Object key names participate — they are part of the shape — while array
indices are implicit in the preserved element order.  The tags live in
their own ``0x1x`` range so a shape preimage can never be confused with a
Merkle leaf (``0x00``) or internal-node (``0x01``) preimage.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from typing import Any, Dict, List

from . import pointers

HASH_LEN = 32
U32BE = struct.Struct(">I")

TAG_NULL = b"\x10"
TAG_BOOLEAN = b"\x11"
TAG_NUMBER = b"\x12"
TAG_STRING = b"\x13"
TAG_EMPTY_OBJECT = b"\x14"
TAG_EMPTY_ARRAY = b"\x15"
TAG_OBJECT = b"\x16"
TAG_ARRAY = b"\x17"


class ShapeError(ValueError):
    """Raised for values that cannot appear in a JSON document."""


def node_type(value: Any) -> str:
    """Return the JSON node type name used in the manifest."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    raise ShapeError(  # pragma: no cover - guarded by validate_json_value
        f"unsupported type {type(value).__name__}"
    )


def _u32be_len(data: bytes) -> bytes:
    return U32BE.pack(len(data))


def shape_digest(value: Any) -> bytes:
    """Compute the bottom-up shape digest of a JSON value.

    Scalar preimages contain only the type tag — never the value — so
    ``1`` and ``2`` (or ``true`` and ``false``) share a digest, while
    ``1`` and ``"1"`` do not.  Object members are folded in
    sorted-by-code-point key order; array elements keep document order.
    """
    if value is None:
        return hashlib.sha256(TAG_NULL).digest()
    if isinstance(value, bool):
        return hashlib.sha256(TAG_BOOLEAN).digest()
    if isinstance(value, (int, float)):
        return hashlib.sha256(TAG_NUMBER).digest()
    if isinstance(value, str):
        return hashlib.sha256(TAG_STRING).digest()
    if isinstance(value, dict):
        if not value:
            return hashlib.sha256(TAG_EMPTY_OBJECT).digest()
        parts = [TAG_OBJECT, U32BE.pack(len(value))]
        for key in sorted(value):
            key_bytes = key.encode("utf-8")
            parts.append(_u32be_len(key_bytes))
            parts.append(key_bytes)
            parts.append(shape_digest(value[key]))
        return hashlib.sha256(b"".join(parts)).digest()
    if isinstance(value, list):
        if not value:
            return hashlib.sha256(TAG_EMPTY_ARRAY).digest()
        parts = [TAG_ARRAY, U32BE.pack(len(value))]
        for item in value:
            parts.append(shape_digest(item))
        return hashlib.sha256(b"".join(parts)).digest()
    raise ShapeError(  # pragma: no cover - guarded by validate_json_value
        f"unsupported type {type(value).__name__}"
    )


@dataclass(frozen=True)
class ShapeNode:
    """One manifest entry: RFC 6901 pointer + JSON node type."""

    pointer: str
    type: str


def shape_nodes(doc: Any) -> List[ShapeNode]:
    """List every node of *doc* (root at ``""``) sorted by pointer."""
    nodes: List[ShapeNode] = []

    def walk(value: Any, pointer: str) -> None:
        nodes.append(ShapeNode(pointer=pointer, type=node_type(value)))
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, pointers.build_pointer(pointer, key))
        elif isinstance(value, list):
            for idx, child in enumerate(value):
                walk(child, pointers.build_pointer(pointer, idx))

    walk(doc, "")
    nodes.sort(key=lambda node: node.pointer)
    return nodes


def shape_manifest(doc: Any) -> Dict[str, Any]:
    """Return the ``POST /shape`` response body for *doc*."""
    nodes = shape_nodes(doc)
    return {
        "shapeHash": shape_digest(doc).hex(),
        "nodeCount": len(nodes),
        "nodes": [
            {"path": node.pointer, "type": node.type} for node in nodes
        ],
    }
