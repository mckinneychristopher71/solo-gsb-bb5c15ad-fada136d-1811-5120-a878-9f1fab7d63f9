"""Hand-written OpenAPI surface for every route.

All routes deliberately accept a raw JSON :class:`fastapi.Request` (so they
can return the documented precise ``loc`` errors instead of Pydantic 422s),
which means FastAPI cannot infer their schemas automatically.  The Pydantic
models below describe the wire contract — including the two proof formats
and the /merge success/conflict responses — and :func:`install_openapi`
merges them into the generated document while keeping the auto-generated
metadata for the health/index routes.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field

HexHash = Field(
    ...,
    pattern=r"^[0-9a-fA-F]{64}$",
    description="SHA-256 digest as 64 hexadecimal characters (32 bytes).",
)

COMMIT_EXAMPLE_DOC = {
    "subject": {"name": "Ada", "age": 36, "isAdult": True, "nickname": None},
    "emails": ["ada@example.com", "ada@analytical.engine"],
    "empty": {},
}


# ---------------------------------------------------------------------------
# Standard (legacy) format
# ---------------------------------------------------------------------------


class ProofStepModel(BaseModel):
    side: Literal["left", "right"] = Field(
        ..., description=(
            "Where the sibling sits relative to the running hash. "
            "`right` means H(running, sibling), `left` means "
            "H(sibling, running)."
        )
    )
    sibling: str = HexHash


class StandardCommitLeaf(BaseModel):
    path: str = Field(..., description="RFC 6901 pointer of the disclosed leaf.")
    value: Any = Field(..., description="Canonical leaf value (scalar or {} / []).")
    proof: List[ProofStepModel] = Field(
        ...,
        description="Sibling path from the leaf up to the root; empty for a "
        "single-leaf tree.",
    )


class StandardVerifyLeaf(StandardCommitLeaf):
    pass


class StandardCommitRequest(BaseModel):
    document: Any = Field(..., description="The nested JSON document to commit.")
    reveal: List[str] = Field(
        ..., description="RFC 6901 pointers of the leaves to disclose."
    )
    proofMode: Optional[Literal["standard"]] = Field(
        None,
        description="Omit or set to 'standard' for per-leaf {side, sibling} "
        "proofs (the backwards-compatible default).",
    )


class StandardCommitResponse(BaseModel):
    root: str = HexHash
    leaves: List[StandardCommitLeaf]


class StandardVerifyRequest(BaseModel):
    root: str = HexHash
    leaves: List[StandardVerifyLeaf]


# ---------------------------------------------------------------------------
# Compact multi-leaf format
# ---------------------------------------------------------------------------


class ProofNodeModel(BaseModel):
    level: int = Field(
        ...,
        ge=0,
        description=(
            "Tree level, 0-indexed from the leaf level.  Level 0 is the leaf "
            "level; the root is at the final level and is never listed."
        ),
    )
    index: int = Field(
        ...,
        ge=0,
        description=(
            "Position of the node within its level.  Each (level, index) "
            "pair appears at most once in the whole proof."
        ),
    )
    hash: str = HexHash


class CompactCommitLeaf(BaseModel):
    path: str = Field(..., description="RFC 6901 pointer of the disclosed leaf.")
    index: int = Field(
        ...,
        ge=0,
        description=(
            "Zero-based position of the leaf in the sorted-by-pointer leaf "
            "set (the same ordering used to build the root)."
        ),
    )
    value: Any = Field(..., description="Canonical leaf value (scalar or {} / []).")


class CompactCommitRequest(BaseModel):
    document: Any = Field(..., description="The nested JSON document to commit.")
    reveal: List[str] = Field(
        ..., description="RFC 6901 pointers of the leaves to disclose."
    )
    proofMode: Literal["compact"] = Field(
        ...,
        description="Selects the deduplicated multi-leaf proof format.",
    )


class CompactCommitResponse(BaseModel):
    root: str = HexHash
    proofMode: Literal["compact"]
    leafCount: int = Field(
        ...,
        ge=1,
        description="Total number of leaves the committed tree was built "
        "from (the verifier needs it to know each level's width).",
    )
    leaves: List[CompactCommitLeaf]
    proofNodes: List[ProofNodeModel] = Field(
        ...,
        description=(
            "Sibling nodes required by the fold, unique by (level, index) "
            "and sorted level-first.  A sibling needed by several disclosed "
            "leaves occurs exactly once.  The last node of an odd-sized "
            "level is never listed: it is paired with a copy of itself."
        ),
    )


class CompactVerifyRequest(BaseModel):
    root: str = HexHash
    leafCount: int = Field(..., ge=1)
    leaves: List[CompactCommitLeaf]
    proofNodes: List[ProofNodeModel]


# ---------------------------------------------------------------------------
# Shared responses / errors
# ---------------------------------------------------------------------------


class VerifiedLeaf(BaseModel):
    path: str
    value: Any


class StandardVerifyResponse(BaseModel):
    valid: bool = True
    root: str = HexHash
    leaves: List[VerifiedLeaf]


# ---------------------------------------------------------------------------
# Leaf-level document diff
# ---------------------------------------------------------------------------


class DiffRequest(BaseModel):
    before: Any = Field(..., description="The original JSON document.")
    after: Any = Field(..., description="The new JSON document.")
    paths: Optional[List[str]] = Field(
        None,
        description=(
            "Optional exact RFC 6901 leaf paths to compare. Omit to compare "
            "the union of all leaves; every entry must be a leaf in at least "
            "one document and duplicates/non-leaf paths are rejected."
        ),
    )


class DiffEntry(BaseModel):
    path: str = Field(..., description="RFC 6901 pointer of the changed leaf.")
    beforeValue: Optional[Any] = Field(
        None, description="Leaf value in `before`; null means absent."
    )
    afterValue: Optional[Any] = Field(
        None, description="Leaf value in `after`; null means absent."
    )
    beforeFingerprint: Optional[str] = Field(
        None,
        description="SHA-256 hex fingerprint of canonical `beforeValue`; "
        "null means the path was absent.",
    )
    afterFingerprint: Optional[str] = Field(
        None,
        description="SHA-256 hex fingerprint of canonical `afterValue`; "
        "null means the path was absent.",
    )


class DiffResponse(BaseModel):
    added: List[DiffEntry] = Field(
        ..., description="Leaf paths present only in `after`, sorted by path."
    )
    removed: List[DiffEntry] = Field(
        ..., description="Leaf paths present only in `before`, sorted by path."
    )
    changed: List[DiffEntry] = Field(
        ...,
        description="Leaf paths present on both sides whose canonical values "
        "differ, sorted by path.",
    )


# ---------------------------------------------------------------------------
# RFC 6902-style JSON patch
# ---------------------------------------------------------------------------


class PatchOperation(BaseModel):
    op: Literal["add", "remove", "replace"] = Field(
        ...,
        description=(
            "RFC 6902 operation kind. `add` inserts an array element (or "
            "appends with the '-' end token) / sets an object member "
            "(replacing an existing one); `remove` deletes an existing "
            "member or element; `replace` overwrites an existing value."
        ),
    )
    path: str = Field(
        ...,
        description=(
            "RFC 6901 pointer to the target. Array tokens are zero-based "
            "indices without leading zeros; '-' addresses the end of an "
            "array and is only valid for `add`."
        ),
    )
    value: Optional[Any] = Field(
        None,
        description=(
            "The new value for `add`/`replace` (required for both); absent "
            "for `remove`. May be any JSON value, including a nested "
            "container."
        ),
    )


class PatchRequest(BaseModel):
    document: Any = Field(..., description="The JSON document to patch.")
    operations: List[PatchOperation] = Field(
        ...,
        description=(
            "RFC 6902 operations applied in array order to a deep copy of "
            "`document`. Every operation is validated before any result is "
            "returned: an invalid path, parent container or array index in "
            "any operation fails the whole request with no partial result."
        ),
    )


class PatchResponse(BaseModel):
    document: Any = Field(..., description="The patched JSON document.")
    changedPaths: List[str] = Field(
        ...,
        description=(
            "RFC 6901 leaf paths that differ from the input document — "
            "removed leaves, added leaves and leaves whose canonical value "
            "changed — sorted by pointer. An operation that leaves the "
            "canonical document unchanged contributes no paths."
        ),
    )


# ---------------------------------------------------------------------------
# Deterministic leaf redaction
# ---------------------------------------------------------------------------


class RedactRule(BaseModel):
    pattern: str = Field(
        ...,
        description=(
            "RFC 6901 fragment selecting leaf paths. Every reference token "
            "is either a literal name/index (with ~0/~1 escapes), '*' "
            "matching exactly one token, or '**' matching zero or more "
            "tokens. The empty fragment '' matches only the root leaf."
        ),
    )
    action: Literal["mask", "drop"] = Field(
        ...,
        description=(
            "`mask` replaces each matched leaf with the fixed `value` "
            "constant; `drop` removes an object member or sets an array "
            "element to null. Dropping the root pointer '' is rejected."
        ),
    )
    value: Optional[Any] = Field(
        None,
        description=(
            "Replacement constant for action `mask` (required); may be any "
            "JSON value, including a nested container. Ignored for `drop`."
        ),
    )


class RedactRequest(BaseModel):
    document: Any = Field(..., description="The JSON document to redact.")
    rules: List[RedactRule] = Field(
        ...,
        description=(
            "Redaction rules evaluated against every RFC 6901 leaf "
            "(scalars, null and empty containers are leaves). When several "
            "rules hit one leaf the winner is, in order: the rule with more "
            "literal tokens, then more '*' tokens over '**', then the "
            "earlier rule in this array. Patterns are validated up front "
            "before any leaf is changed."
        ),
    )


class AppliedRedaction(BaseModel):
    path: str = Field(
        ..., description="RFC 6901 pointer of the redacted leaf."
    )
    action: Literal["mask", "drop"]
    ruleIndex: int = Field(
        ...,
        ge=0,
        description="Array index of the rule that won this leaf.",
    )
    pattern: str = Field(
        ..., description="Pattern carried by the winning rule."
    )


class RedactResponse(BaseModel):
    document: Any = Field(..., description="The redacted JSON document.")
    applied: List[AppliedRedaction] = Field(
        ...,
        description=(
            "One entry per redacted leaf, sorted by RFC 6901 pointer. "
            "Leaves no rule matched are omitted."
        ),
    )


# ---------------------------------------------------------------------------
# Structural shape manifest
# ---------------------------------------------------------------------------


class ShapeRequest(BaseModel):
    document: Any = Field(
        ..., description="The JSON document to fingerprint structurally."
    )


class ShapeNodeModel(BaseModel):
    path: str = Field(
        ...,
        description="RFC 6901 pointer of the node ('' is the document root).",
    )
    type: Literal["object", "array", "string", "number", "boolean", "null"] = (
        Field(
            ...,
            description=(
                "JSON node type. Scalar values are never reported — only "
                "their type — so the manifest carries no payloads."
            ),
        )
    )


class ShapeResponse(BaseModel):
    shapeHash: str = HexHash
    nodeCount: int = Field(
        ...,
        ge=1,
        description="Total number of nodes listed (the root included).",
    )
    nodes: List[ShapeNodeModel] = Field(
        ...,
        description=(
            "Every node of the document, sorted by RFC 6901 pointer. "
            "Containers list their descendants; scalar values never appear."
        ),
    )


class CompactVerifyResponse(StandardVerifyResponse):
    proofMode: Literal["compact"]


# ---------------------------------------------------------------------------
# Three-way document merge
# ---------------------------------------------------------------------------


class MergeRequest(BaseModel):
    base: Any = Field(
        ...,
        description=(
            "The common ancestor JSON document that both sides branched "
            "from."
        ),
    )
    left: Any = Field(..., description="The first edited JSON document.")
    right: Any = Field(..., description="The second edited JSON document.")


class MergeSide(BaseModel):
    present: bool = Field(
        ...,
        description=(
            "Whether the path exists on this side. A missing member "
            "(present=false) is distinct from a present JSON null value."
        ),
    )
    value: Optional[Any] = Field(
        None,
        description=(
            "The value on this side; null is used for the value when "
            "present=true and the value itself is null, and also when "
            "present=false (check `present` to distinguish the two)."
        ),
    )


class MergeConflict(BaseModel):
    path: str = Field(
        ...,
        description=(
            "RFC 6901 pointer of the divergent path ('' for the whole "
            "document)."
        ),
    )
    base: MergeSide = Field(..., description="The base side at `path`.")
    left: MergeSide = Field(..., description="The left side at `path`.")
    right: MergeSide = Field(..., description="The right side at `path`.")


class MergeSuccessResponse(BaseModel):
    merged: Any = Field(
        ...,
        description=(
            "The merged JSON document. Arrays are treated as a single value "
            "rather than element-wise merged; present null is distinct from "
            "a missing member, and canonical JSON equality decides whether "
            "both sides agreed (so 1 and 1.0 are different edits)."
        ),
    )


class MergeConflictResponse(BaseModel):
    conflicts: List[MergeConflict] = Field(
        ...,
        description=(
            "Every divergent RFC 6901 path, sorted by pointer. When "
            "non-empty no partial merged document is returned. Each entry "
            "gives all three sides' presence flags and values."
        ),
    )


class ErrorDetail(BaseModel):
    code: str
    message: str
    loc: Optional[str] = Field(
        None, description="JSON-Pointer-style location of the offending field."
    )
    detail: Optional[Dict[str, Any]] = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


def _ref(name: str) -> Dict[str, Any]:
    return {"$ref": f"#/components/schemas/{name}"}


def _model_schemas(*models: type[BaseModel]) -> Dict[str, Any]:
    """Return {name: schema} for the models, inlining any ``$defs``."""
    schemas: Dict[str, Any] = {}
    for model in models:
        generated = model.model_json_schema(
            ref_template="#/components/schemas/{model}"
        )
        defs = generated.pop("$defs", {})
        for name, schema in defs.items():
            schemas.setdefault(name, schema)
        schemas[model.__name__] = generated
    return schemas


def _json_content(schema: Dict[str, Any], example: Any = None) -> Dict[str, Any]:
    content: Dict[str, Any] = {
        "content": {"application/json": {"schema": schema}}
    }
    if example is not None:
        content["content"]["application/json"]["example"] = example
    return content


def install_openapi(app: FastAPI) -> None:
    """Replace the auto-generated OpenAPI document with the full contract."""

    def _openapi() -> Dict[str, Any]:
        cached = getattr(app, "_custom_openapi_schema", None)
        if cached is not None:
            return cached

        generated = app._orig_openapi()  # type: ignore[attr-defined]

        components = generated.setdefault("components", {}).setdefault(
            "schemas", {}
        )
        components.update(
            _model_schemas(
                ProofStepModel,
                StandardCommitRequest,
                StandardCommitResponse,
                StandardVerifyRequest,
                ProofNodeModel,
                CompactCommitRequest,
                CompactCommitResponse,
                CompactVerifyRequest,
                StandardVerifyResponse,
                CompactVerifyResponse,
                DiffRequest,
                DiffResponse,
                PatchOperation,
                PatchRequest,
                PatchResponse,
                RedactRule,
                RedactRequest,
                AppliedRedaction,
                RedactResponse,
                ShapeRequest,
                ShapeNodeModel,
                ShapeResponse,
                MergeRequest,
                MergeSide,
                MergeConflict,
                MergeSuccessResponse,
                MergeConflictResponse,
                ErrorResponse,
            )
        )

        error_responses = {
            "400": {"description": "Structural/input error (BAD_PROOF, "
                    "INVALID_REQUEST, POINTER_SYNTAX, ...)",
                    **_json_content(_ref("ErrorResponse"))},
            "413": {"description": "Request body exceeds MAX_BODY_BYTES",
                    **_json_content(_ref("ErrorResponse"))},
            "422": {"description": "ROOT_MISMATCH: structurally valid proof "
                    "that does not rebuild the committed root",
                    **_json_content(_ref("ErrorResponse"))},
        }

        commit_request_schema = {
            "oneOf": [
                _ref("StandardCommitRequest"),
                _ref("CompactCommitRequest"),
            ]
        }
        commit_response_schema = {
            "oneOf": [
                _ref("StandardCommitResponse"),
                _ref("CompactCommitResponse"),
            ]
        }
        verify_request_schema = {
            "oneOf": [
                _ref("StandardVerifyRequest"),
                _ref("CompactVerifyRequest"),
            ],
            "description": (
                "The format is detected automatically: a body containing "
                "`proofNodes`/`leafCount` is treated as a compact proof; "
                "otherwise each leaf must carry a standard `proof` array."
            ),
        }
        verify_response_schema = {
            "oneOf": [
                _ref("StandardVerifyResponse"),
                _ref("CompactVerifyResponse"),
            ]
        }

        generated["paths"]["/commit"] = {
            "post": {
                "summary": "Commit a document and generate disclosure proofs",
                "description": (
                    "Expands the document into sorted RFC 6901 leaves, "
                    "builds the reproducible Merkle root and returns, for "
                    "each requested leaf, its value plus an inclusion "
                    "proof. With `proofMode: \"compact\"` the response "
                    "carries `leafCount`, a per-leaf sorted `index` and one "
                    "deduplicated `proofNodes` array; without it, the "
                    "response keeps the legacy per-leaf `proof` shape."
                ),
                "operationId": "commit",
                "requestBody": _json_content(
                    commit_request_schema,
                    example={
                        "document": COMMIT_EXAMPLE_DOC,
                        "reveal": ["/subject/age", "/emails/1", "/empty"],
                        "proofMode": "compact",
                    },
                ),
                "responses": {
                    "200": {
                        "description": "Root and disclosures (only selected "
                        "leaves are returned)",
                        **_json_content(
                            commit_response_schema,
                            example={
                                "root": "9f0c000000000000000000000000000000000000000000000000000000000000",
                                "proofMode": "compact",
                                "leafCount": 8,
                                "leaves": [
                                    {"path": "/emails/1", "index": 2,
                                     "value": "ada@analytical.engine"},
                                    {"path": "/empty", "index": 3,
                                     "value": {}},
                                    {"path": "/subject/age", "index": 5,
                                     "value": 36},
                                ],
                                "proofNodes": [
                                    {"level": 0, "index": 0,
                                     "hash": "ab" * 32},
                                    {"level": 1, "index": 1,
                                     "hash": "cd" * 32},
                                ],
                            },
                        ),
                    },
                    **error_responses,
                },
            }
        }

        generated["paths"]["/verify"] = {
            "post": {
                "summary": "Verify disclosures against a committed root",
                "description": (
                    "Statelessly rebuilds the root from the disclosed leaf "
                    "values and the proof. The compact shape is rebuilt "
                    "bottom-up from `leafCount`, the per-leaf `index` and "
                    "the unique `{level,index,hash}` proof nodes. Index "
                    "range/uniqueness, the odd-level last-node duplication "
                    "rule and missing/extraneous proof nodes are structural "
                    "errors (400 BAD_PROOF with a precise `loc`); a valid "
                    "structure that folds to another root is 422 "
                    "ROOT_MISMATCH."
                ),
                "operationId": "verify",
                "requestBody": _json_content(
                    verify_request_schema,
                    example={
                        "root": "9f0c000000000000000000000000000000000000000000000000000000000000",
                        "leafCount": 8,
                        "leaves": [
                            {"path": "/subject/age", "index": 5, "value": 36}
                        ],
                        "proofNodes": [
                            {"level": 0, "index": 4, "hash": "ab" * 32},
                            {"level": 1, "index": 3, "hash": "cd" * 32},
                            {"level": 2, "index": 0, "hash": "ef" * 32},
                        ],
                    },
                ),
                "responses": {
                    "200": {
                        "description": "All disclosures rebuild the committed "
                        "root",
                        **_json_content(verify_response_schema),
                    },
                    **error_responses,
                },
            }
        }

        generated["paths"]["/diff"] = {
            "post": {
                "summary": "Report leaf-level differences between two documents",
                "description": (
                    "Expands both documents using the same RFC 6901 leaf "
                    "rules as /commit (scalars, null and empty containers "
                    "are leaves), aligns sorted leaf paths and returns added, "
                    "removed and changed entries. Each entry includes both "
                    "values (null on the absent side) and SHA-256 "
                    "fingerprints of the canonical value bytes. An optional "
                    "paths array permits only pointers that are exact leaves "
                    "in at least one document; duplicates and paths that are "
                    "non-leaf containers in both documents fail with a "
                    "precise location."
                ),
                "operationId": "diff",
                "requestBody": _json_content(
                    _ref("DiffRequest"),
                    example={
                        "before": {
                            "name": "Ada",
                            "age": 36,
                            "active": True,
                            "tags": [],
                            "removed": {"only": 1},
                        },
                        "after": {
                            "name": "Ada",
                            "age": 37,
                            "active": False,
                            "tags": [],
                            "added": "yes",
                        },
                        "paths": ["/age", "/active", "/added", "/removed/only"],
                    },
                ),
                "responses": {
                    "200": {
                        "description": "Stable, path-sorted leaf differences.",
                        **_json_content(
                            _ref("DiffResponse"),
                            example={
                                "added": [
                                    {
                                        "path": "/added",
                                        "beforeValue": None,
                                        "afterValue": "yes",
                                        "beforeFingerprint": None,
                                        "afterFingerprint": "ab" * 32,
                                    }
                                ],
                                "removed": [
                                    {
                                        "path": "/removed/only",
                                        "beforeValue": 1,
                                        "afterValue": None,
                                        "beforeFingerprint": "cd" * 32,
                                        "afterFingerprint": None,
                                    }
                                ],
                                "changed": [
                                    {
                                        "path": "/active",
                                        "beforeValue": True,
                                        "afterValue": False,
                                        "beforeFingerprint": "ef" * 32,
                                        "afterFingerprint": "01" * 32,
                                    },
                                    {
                                        "path": "/age",
                                        "beforeValue": 36,
                                        "afterValue": 37,
                                        "beforeFingerprint": "02" * 32,
                                        "afterFingerprint": "03" * 32,
                                    },
                                ],
                            },
                        ),
                    },
                    **error_responses,
                },
            }
        }

        generated["paths"]["/patch"] = {
            "post": {
                "summary": "Apply ordered RFC 6902 add/remove/replace operations",
                "description": (
                    "Applies the operations in array order to a deep copy of "
                    "`document` using RFC 6901 pointer paths and RFC 6902 "
                    "array semantics: `add` inserts before an index or "
                    "appends with '-' (object members are replaced if they "
                    "already exist); `remove` and `replace` require the "
                    "target to exist; leading-zero indices are invalid. The "
                    "request is validated as one atomic unit — an invalid "
                    "path, parent container or array index in any operation "
                    "fails with a `loc` of `operations[i]` (or one of its "
                    "fields) and no partial result is returned. On success "
                    "the response carries the patched document plus "
                    "`changedPaths`: the leaf paths (same leaf rules as "
                    "/commit) that were added, removed or canonically "
                    "changed, sorted by RFC 6901 pointer."
                ),
                "operationId": "patch",
                "requestBody": _json_content(
                    _ref("PatchRequest"),
                    example={
                        "document": {
                            "subject": {"name": "Ada", "age": 36},
                            "emails": ["ada@example.com"],
                            "role": None,
                        },
                        "operations": [
                            {"op": "replace", "path": "/subject/age",
                             "value": 37},
                            {"op": "add", "path": "/emails/-",
                             "value": "ada@analytical.engine"},
                            {"op": "add", "path": "/subject/title",
                             "value": "Countess"},
                            {"op": "remove", "path": "/role"},
                        ],
                    },
                ),
                "responses": {
                    "200": {
                        "description": (
                            "Patched document and RFC 6901-sorted changed "
                            "leaf paths."
                        ),
                        **_json_content(
                            _ref("PatchResponse"),
                            example={
                                "document": {
                                    "subject": {
                                        "name": "Ada",
                                        "age": 37,
                                        "title": "Countess",
                                    },
                                    "emails": [
                                        "ada@example.com",
                                        "ada@analytical.engine",
                                    ],
                                },
                                "changedPaths": [
                                    "/emails/1",
                                    "/role",
                                    "/subject/age",
                                    "/subject/title",
                                ],
                            },
                        ),
                    },
                    **error_responses,
                },
            }
        }

        generated["paths"]["/redact"] = {
            "post": {
                "summary": "Mask or drop JSON leaves by RFC 6901 patterns",
                "description": (
                    "Applies deterministic redaction to a deep copy of "
                    "`document`. Each rule pattern is an RFC 6901 fragment "
                    "whose tokens are literals, '*' (exactly one level) or "
                    "'**' (zero or more levels); action `mask` replaces "
                    "each matched leaf with a fixed constant while `drop` "
                    "removes an object member or nulls an array element. "
                    "The same leaf rules as /commit apply, so scalars, "
                    "null and empty {} / [] containers are addressable "
                    "leaves. When several rules hit a leaf the winner is "
                    "the rule with more literal tokens, then more '*' "
                    "tokens over '**', then the earlier array entry; the "
                    "root pointer '' may be masked but not dropped. Every "
                    "pattern is syntactically validated before the "
                    "document is touched. The response carries the redacted "
                    "document plus an `applied` list (path, action, "
                    "ruleIndex, pattern) sorted by RFC 6901 pointer."
                ),
                "operationId": "redact",
                "requestBody": _json_content(
                    _ref("RedactRequest"),
                    example={
                        "document": {
                            "subject": {
                                "name": "Ada",
                                "age": 36,
                                "ssn": "078-05-1120",
                            },
                            "emails": ["ada@example.com", "ada@x.io"],
                            "empty": {},
                        },
                        "rules": [
                            {"pattern": "/subject/ssn",
                             "action": "mask", "value": "***"},
                            {"pattern": "/emails/*",
                             "action": "mask", "value": "redacted"},
                            {"pattern": "/subject/age", "action": "drop"},
                        ],
                    },
                ),
                "responses": {
                    "200": {
                        "description": "Redacted document and path-sorted "
                        "applied rules.",
                        **_json_content(
                            _ref("RedactResponse"),
                            example={
                                "document": {
                                    "subject": {
                                        "name": "Ada",
                                        "ssn": "***",
                                    },
                                    "emails": ["redacted", "redacted"],
                                    "empty": {},
                                },
                                "applied": [
                                    {"path": "/emails/0", "action": "mask",
                                     "ruleIndex": 1,
                                     "pattern": "/emails/*"},
                                    {"path": "/emails/1", "action": "mask",
                                     "ruleIndex": 1,
                                     "pattern": "/emails/*"},
                                    {"path": "/subject/age", "action": "drop",
                                     "ruleIndex": 2,
                                     "pattern": "/subject/age"},
                                    {"path": "/subject/ssn", "action": "mask",
                                     "ruleIndex": 0,
                                     "pattern": "/subject/ssn"},
                                ],
                            },
                        ),
                    },
                    **error_responses,
                },
            }
        }

        generated["paths"]["/shape"] = {
            "post": {
                "summary": "Structural manifest and shape hash of a document",
                "description": (
                    "Returns the value-free shape of `document`: every node "
                    "listed by RFC 6901 pointer and JSON node type (scalars "
                    "contribute only their type, never their value), plus a "
                    "bottom-up SHA-256 `shapeHash`. The hash folds each "
                    "node under a domain tag with length-prefixed "
                    "boundaries: object members are sorted by key code "
                    "point, array elements keep their order, empty "
                    "containers and scalar types carry distinct tags, and "
                    "scalar values never enter the preimage. Two documents "
                    "have the same shape exactly when their `shapeHash` "
                    "values are equal."
                ),
                "operationId": "shape",
                "requestBody": _json_content(
                    _ref("ShapeRequest"),
                    example={
                        "document": {
                            "subject": {"name": "Ada", "age": 36},
                            "emails": ["ada@example.com"],
                            "empty": {},
                        },
                    },
                ),
                "responses": {
                    "200": {
                        "description": "Path-sorted structural manifest and "
                        "the bottom-up shape hash.",
                        **_json_content(
                            _ref("ShapeResponse"),
                            example={
                                "shapeHash": "ab" * 32,
                                "nodeCount": 7,
                                "nodes": [
                                    {"path": "", "type": "object"},
                                    {"path": "/emails", "type": "array"},
                                    {"path": "/emails/0", "type": "string"},
                                    {"path": "/empty", "type": "object"},
                                    {"path": "/subject", "type": "object"},
                                    {"path": "/subject/age",
                                     "type": "number"},
                                    {"path": "/subject/name",
                                     "type": "string"},
                                ],
                            },
                        ),
                    },
                    **error_responses,
                },
            }
        }

        generated["paths"]["/merge"] = {
            "post": {
                "summary": "Three-way merge two edits branched from one base",
                "description": (
                    "Merges `left` and `right`, both edited from the common "
                    "`base`, by descending object members and addressing "
                    "every slot as an RFC 6901 path. A slot changed on only "
                    "one side takes that side's value (a deletion included), "
                    "and slots both sides changed to the same canonical JSON "
                    "value take that value. Arrays are never split: the "
                    "whole array is one value, so concurrent element edits "
                    "conflict. Equality is canonical: a missing member is "
                    "not null and 1 is not 1.0 (nor true). Divergent edits "
                    "at the same path — delete vs modify included — are "
                    "reported as conflicts sorted by RFC 6901 pointer, each "
                    "with every side's presence flag and value; when any "
                    "conflict exists the response carries no partial merged "
                    "document."
                ),
                "operationId": "merge",
                "requestBody": _json_content(
                    _ref("MergeRequest"),
                    example={
                        "base": {
                            "subject": {"name": "Ada", "age": 36},
                            "tags": ["a", "b"],
                            "role": "analyst",
                        },
                        "left": {
                            "subject": {"name": "Ada", "age": 37},
                            "tags": ["a", "b", "z"],
                            "role": "owner",
                            "team": "engine",
                        },
                        "right": {
                            "subject": {"name": "Ada", "age": 36},
                            "tags": ["a", "c"],
                            "team": "engine",
                        },
                    },
                ),
                "responses": {
                    "200": {
                        "description": (
                            "Either the merged document (`merged`) or, when "
                            "the edits cannot be reconciled, the complete "
                            "path-sorted `conflicts` list with no merged "
                            "document."
                        ),
                        **_json_content(
                            {
                                "oneOf": [
                                    _ref("MergeSuccessResponse"),
                                    _ref("MergeConflictResponse"),
                                ]
                            },
                            example={
                                "conflicts": [
                                    {
                                        "path": "/role",
                                        "base": {"present": True,
                                                 "value": "analyst"},
                                        "left": {"present": True,
                                                 "value": "owner"},
                                        "right": {"present": False,
                                                 "value": None},
                                    },
                                    {
                                        "path": "/tags",
                                        "base": {"present": True,
                                                 "value": ["a", "b"]},
                                        "left": {"present": True,
                                                 "value": ["a", "b", "z"]},
                                        "right": {"present": True,
                                                 "value": ["a", "c"]},
                                    },
                                ]
                            },
                        ),
                    },
                    **error_responses,
                },
            }
        }

        app._custom_openapi_schema = generated
        return generated

    # Stash the original generator before swapping it in.
    app._orig_openapi = app.openapi  # type: ignore[attr-defined]
    app.openapi = _openapi  # type: ignore[method-assign]
