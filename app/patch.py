"""RFC 6902-style JSON Patch engine (``add`` / ``remove`` / ``replace``).

Each operation is ``{"op": ..., "path": ..., "value"?: ...}`` where ``path``
is an RFC 6901 pointer.  Operations are applied to a deep copy of the
document, strictly in order, since one operation may make a later path
valid or invalid.

Array semantics follow RFC 6902:

* ``add``   inserts before an index (``0``..``length``), or appends with the
            end-of-array token ``"-"``; an existing object member is
            replaced;
* ``remove``/``replace`` target an existing member/element;
* leading-zero indices (``"01"``) are not valid indices;
* ``"-"`` is only legal as the ``add`` target.

Every failure raises :class:`~app.errors.ApiError` whose ``loc`` pins the
fault at ``operations[i]`` (or one of its fields), matching the error body
of the other endpoints.

With ``with_undo=True`` :func:`apply_patch` additionally records the
inverse of every step — the target state observed *before* that step — and
returns the inverses in reverse application order, ready to be handed back
to :func:`apply_patch` to restore the original document.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from . import pointers
from .diff import leaf_facts
from .errors import (
    ApiError,
    INVALID_REQUEST,
    PATH_NOT_FOUND,
    POINTER_SYNTAX,
)
from .validation import validate_json_value

SUPPORTED_OPS = ("add", "remove", "replace")


@dataclass(frozen=True)
class _Operation:
    op: str
    path: str
    tokens: Tuple[str, ...]
    value: Any = None


def _parse_operation(raw: Any, index: int) -> _Operation:
    """Structurally validate one operation envelope (no document access)."""

    loc = f"operations[{index}]"
    if not isinstance(raw, dict):
        raise ApiError(
            INVALID_REQUEST,
            f"operation must be an object {{op, path[, value]}}, got "
            f"{type(raw).__name__}",
            loc=loc,
        )

    if "op" not in raw:
        raise ApiError(
            INVALID_REQUEST, "missing required field 'op'", loc=f"{loc}.op"
        )
    op = raw["op"]
    if not isinstance(op, str):
        raise ApiError(
            INVALID_REQUEST,
            f"'op' must be a string, got {type(op).__name__}",
            loc=f"{loc}.op",
        )
    if op not in SUPPORTED_OPS:
        raise ApiError(
            INVALID_REQUEST,
            f"unsupported op {op!r}: only {', '.join(SUPPORTED_OPS)} are "
            "supported",
            loc=f"{loc}.op",
        )

    if "path" not in raw:
        raise ApiError(
            INVALID_REQUEST,
            "missing required field 'path'",
            loc=f"{loc}.path",
        )
    path = raw["path"]
    if not isinstance(path, str):
        raise ApiError(
            INVALID_REQUEST,
            f"'path' must be an RFC 6901 pointer string, got "
            f"{type(path).__name__}",
            loc=f"{loc}.path",
        )
    try:
        tokens = tuple(pointers.parse_pointer(path))
    except pointers.PointerError as exc:
        raise ApiError(
            POINTER_SYNTAX,
            str(exc),
            loc=loc,
            detail={"path": path},
        ) from exc

    value: Any = None
    if op in ("add", "replace"):
        if "value" not in raw:
            raise ApiError(
                INVALID_REQUEST,
                f"op {op!r} requires a 'value' field",
                loc=f"{loc}.value",
            )
        value = raw["value"]
        validate_json_value(value, loc=f"{loc}.value")

    return _Operation(op=op, path=path, tokens=tokens, value=value)


def _resolve_parent(doc: Any, operation: _Operation, index: int) -> Any:
    """Resolve the parent container of the operation's target token.

    Raises ``PATH_NOT_FOUND`` located at ``operations[i]`` when an
    intermediate path is missing, is a scalar, or carries an invalid array
    index.
    """

    loc = f"operations[{index}]"
    if len(operation.tokens) == 0:
        return doc
    try:
        return pointers.resolve(doc, operation.tokens[:-1])
    except (KeyError, IndexError, TypeError) as exc:
        raise ApiError(
            PATH_NOT_FOUND,
            f"parent of path {operation.path!r} cannot be resolved: {exc}",
            loc=loc,
            detail={"path": operation.path},
        ) from exc


def _target_key(
    parent: Any, operation: _Operation, index: int, *, allow_end: bool
) -> Tuple[str, int | None]:
    """Validate the final token against *parent*.

    Returns ``(key, None)`` for objects and ``(token-as-string, index)`` for
    arrays (the index is ``-1`` for the array-end token).
    """

    loc = f"operations[{index}]"
    token = operation.tokens[-1]

    if isinstance(parent, dict):
        return str(token), None

    if isinstance(parent, list):
        if token == "-":
            if allow_end:
                return token, -1
            raise ApiError(
                PATH_NOT_FOUND,
                f"path {operation.path!r}: the end-of-array token '-' is "
                f"only valid for 'add', not for {operation.op!r}",
                loc=loc,
                detail={"path": operation.path},
            )
        idx = pointers.decode_array_index(str(token))
        if idx is None:
            raise ApiError(
                PATH_NOT_FOUND,
                f"path {operation.path!r}: token {str(token)!r} is not a "
                "valid array index (integers without leading zeros, or '-')",
                loc=loc,
                detail={"path": operation.path},
            )
        return str(token), idx

    raise ApiError(
        PATH_NOT_FOUND,
        f"path {operation.path!r} cannot be addressed: the parent is a "
        f"scalar ({type(parent).__name__}) and has no members or elements",
        loc=loc,
        detail={"path": operation.path},
    )


def _apply_one(
    doc: Any,
    operation: _Operation,
    index: int,
    inverses: List[Dict[str, Any]] | None = None,
) -> Any:
    """Apply a single parsed operation; returns the (possibly new) document.

    When *inverses* is supplied, the inverse of this step is appended to it
    in forward order.  The inverse is captured from the target state
    observed *before* the mutation (after every legality check has passed),
    so a failed step never records a partial inverse.
    """

    loc = f"operations[{index}]"

    def record(inverse: Dict[str, Any]) -> None:
        if inverses is not None:
            inverses.append(inverse)

    # Operations on the whole document: only add/replace are permitted —
    # RFC 6902 reserves an empty-path remove for an implementation-defined
    # root removal that would leave no document.
    if len(operation.tokens) == 0:
        if operation.op in ("add", "replace"):
            # Restore the previous whole document, whatever it was.
            record({"op": "replace", "path": "", "value": copy.deepcopy(doc)})
            return copy.deepcopy(operation.value)
        raise ApiError(
            INVALID_REQUEST,
            "cannot remove the entire document: the root pointer '' is not "
            "a valid target for 'remove'",
            loc=loc,
            detail={"path": ""},
        )

    parent = _resolve_parent(doc, operation, index)
    key, idx = _target_key(
        parent, operation, index, allow_end=operation.op == "add"
    )
    # The inverse re-targets the same slot via the parent pointer, rebuilt
    # with canonical ~0/~1 escaping and a real numeric array index.
    parent_pointer = "".join(
        "/" + str(token).replace("~", "~0").replace("/", "~1")
        for token in operation.tokens[:-1]
    )

    if isinstance(parent, dict):
        if operation.op == "add":
            if key in parent:
                # Adding an existing member replaces it (RFC 6902 4.1); the
                # inverse restores the previous value rather than deleting.
                record(
                    {
                        "op": "replace",
                        "path": pointers.build_pointer(
                            parent_pointer, key
                        ),
                        "value": copy.deepcopy(parent[key]),
                    }
                )
            else:
                record({"op": "remove",
                        "path": pointers.build_pointer(parent_pointer, key)})
            # Adding an existing member replaces it (RFC 6902 4.1).
            parent[key] = copy.deepcopy(operation.value)
        elif operation.op == "remove":
            if key not in parent:
                raise ApiError(
                    PATH_NOT_FOUND,
                    f"path {operation.path!r} does not exist: object has no "
                    f"member {key!r}",
                    loc=loc,
                    detail={"path": operation.path},
                )
            record(
                {
                    "op": "add",
                    "path": pointers.build_pointer(parent_pointer, key),
                    "value": copy.deepcopy(parent[key]),
                }
            )
            del parent[key]
        else:  # replace
            if key not in parent:
                raise ApiError(
                    PATH_NOT_FOUND,
                    f"path {operation.path!r} does not exist and cannot be "
                    f"replaced: object has no member {key!r}",
                    loc=loc,
                    detail={"path": operation.path},
                )
            record(
                {
                    "op": "replace",
                    "path": pointers.build_pointer(parent_pointer, key),
                    "value": copy.deepcopy(parent[key]),
                }
            )
            parent[key] = copy.deepcopy(operation.value)
        return doc

    # Array parent.
    length = len(parent)
    if operation.op == "add":
        # idx == -1 (the "-" token) appends; a numeric index may be equal to
        # the current length, also an append (insert position length).
        if idx == -1:
            insert_at = length
        elif idx > length:
            raise ApiError(
                PATH_NOT_FOUND,
                f"array index {idx} out of range for 'add' (length "
                f"{length}; insert positions 0..{length})",
                loc=loc,
                detail={"path": operation.path},
            )
        else:
            insert_at = idx
        # The inverse removes the inserted element at its concrete numeric
        # position — '-' is never emitted back, so undo needs no token.
        record(
            {
                "op": "remove",
                "path": pointers.build_pointer(parent_pointer, insert_at),
            }
        )
        if idx == -1:
            parent.append(copy.deepcopy(operation.value))
        else:
            parent.insert(insert_at, copy.deepcopy(operation.value))
    elif operation.op == "remove":
        if idx is None or not 0 <= idx < length:
            raise ApiError(
                PATH_NOT_FOUND,
                f"array index {idx} out of range for 'remove' (length "
                f"{length})",
                loc=loc,
                detail={"path": operation.path},
            )
        record(
            {
                "op": "add",
                "path": pointers.build_pointer(parent_pointer, idx),
                "value": copy.deepcopy(parent[idx]),
            }
        )
        del parent[idx]
    else:  # replace
        if idx is None or not 0 <= idx < length:
            raise ApiError(
                PATH_NOT_FOUND,
                f"array index {idx} out of range for 'replace' (length "
                f"{length})",
                loc=loc,
                detail={"path": operation.path},
            )
        record(
            {
                "op": "replace",
                "path": pointers.build_pointer(parent_pointer, idx),
                "value": copy.deepcopy(parent[idx]),
            }
        )
        parent[idx] = copy.deepcopy(operation.value)
    return doc


def apply_patch(
    document: Any, raw_operations: Any, *, with_undo: bool = False
) -> Dict[str, Any]:
    """Validate and apply an RFC 6902-style patch to a deep copy.

    Every operation envelope is validated before the document is touched, so
    a structural error never produces partial output; semantic failures are
    detected in order and are likewise reported before a response is built.

    Returns ``{"document": patched, "changedPaths": [...]}`` where
    ``changedPaths`` is the symmetric difference of both documents' RFC 6901
    leaf paths (leaves present on one side only, or present on both with
    different canonical values), sorted by pointer.

    With ``with_undo=True`` the result additionally carries
    ``"undoOperations"``: one inverse operation per applied step, recorded
    from each step's pre-execution target state and returned in **reverse**
    step order.  Applying them to ``patched`` via another /patch call
    restores a document equal to the input.
    """

    if not isinstance(raw_operations, list):
        raise ApiError(
            INVALID_REQUEST,
            "'operations' must be an array of RFC 6902 patch operations",
            loc="operations",
        )

    # Pass 1: envelopes (types, op names, pointer syntax, required values)
    # are validated up front, before any document state changes.
    operations: List[_Operation] = [
        _parse_operation(raw, i) for i, raw in enumerate(raw_operations)
    ]

    # Pass 2: apply in order on a private copy.  Each operation is checked
    # against the document state produced by its predecessors; on failure the
    # mutated copy is discarded and no partial result leaves the service.
    # The inverse of every applied step is appended in forward order, then
    # reversed below so the caller can feed the list straight back to /patch.
    patched = copy.deepcopy(document)
    forward_inverses: List[Dict[str, Any]] = [] if with_undo else None  # type: ignore[assignment]
    for i, operation in enumerate(operations):
        patched = _apply_one(
            patched, operation, i, forward_inverses
        )

    result: Dict[str, Any] = {}
    if with_undo:
        result["undoOperations"] = list(reversed(forward_inverses or []))

    before = leaf_facts(document)
    after = leaf_facts(patched)
    changed = sorted(
        path
        for path in set(before) | set(after)
        if (
            path not in before
            or path not in after
            or before[path].fingerprint != after[path].fingerprint
        )
    )
    result["document"] = patched
    result["changedPaths"] = changed
    return result
