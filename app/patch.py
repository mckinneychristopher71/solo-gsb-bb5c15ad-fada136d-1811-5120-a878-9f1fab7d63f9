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

:func:`apply_patch_with_undo` additionally records, for every forward step,
an inverse operation that restores the exact pre-step target state; the
inverse list is returned in reverse order so it can be replayed through
``/patch`` to roll the patched document back to the original.
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


def _encode_pointer(prefix: str, token: Any) -> str:
    """Append one reference token to *prefix* using RFC 6901 escapes."""
    raw = str(token).replace("~", "~0").replace("/", "~1")
    return prefix + "/" + raw


def _parent_pointer(operation: _Operation) -> str:
    """RFC 6901 pointer of the operation's parent container."""
    pointer = ""
    for token in operation.tokens[:-1]:
        pointer = _encode_pointer(pointer, token)
    return pointer


def _apply_one(doc: Any, operation: _Operation, index: int) -> Tuple[Any, Dict[str, Any]]:
    """Apply a single parsed operation.

    Returns ``(new_doc, inverse)`` where *inverse* is a JSON-serializable
    RFC 6902 operation that restores the target to its exact pre-step state.
    The inverse is valid against the document state produced by this step;
    callers replay the collected inverses in reverse order.
    """

    loc = f"operations[{index}]"

    # Operations on the whole document: only add/replace are permitted —
    # RFC 6902 reserves an empty-path remove for an implementation-defined
    # root removal that would leave no document.
    if len(operation.tokens) == 0:
        if operation.op in ("add", "replace"):
            # Restoring the previous whole document is always a root
            # add/replace of the deep-copied pre-step root.
            inverse = {"op": "add", "path": "", "value": copy.deepcopy(doc)}
            return copy.deepcopy(operation.value), inverse
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

    if isinstance(parent, dict):
        if operation.op == "add":
            # Adding an existing member replaces it (RFC 6902 4.1). The
            # inverse restores the old value; a member that did not exist is
            # removed instead.
            if key in parent:
                inverse = {
                    "op": "add",
                    "path": operation.path,
                    "value": copy.deepcopy(parent[key]),
                }
            else:
                inverse = {"op": "remove", "path": operation.path}
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
            inverse = {
                "op": "add",
                "path": operation.path,
                "value": copy.deepcopy(parent[key]),
            }
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
            inverse = {
                "op": "replace",
                "path": operation.path,
                "value": copy.deepcopy(parent[key]),
            }
            parent[key] = copy.deepcopy(operation.value)
        return doc, inverse

    # Array parent.
    length = len(parent)
    if operation.op == "add":
        # idx == -1 (the "-" token) appends; a numeric index may be equal to
        # the current length, also an append (insert position length).
        if idx == -1:
            insert_at = length
            parent.append(copy.deepcopy(operation.value))
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
            parent.insert(insert_at, copy.deepcopy(operation.value))
        # Undo an insertion by removing the element at the concrete numeric
        # position it actually landed at ('-' never appears in the inverse).
        inverse = {
            "op": "remove",
            "path": _encode_pointer(_parent_pointer(operation), insert_at),
        }
    elif operation.op == "remove":
        if idx is None or not 0 <= idx < length:
            raise ApiError(
                PATH_NOT_FOUND,
                f"array index {idx} out of range for 'remove' (length "
                f"{length})",
                loc=loc,
                detail={"path": operation.path},
            )
        removed = parent[idx]
        del parent[idx]
        # Re-insert the removed element at its original numeric position; an
        # 'add' at that index shifts the later elements back into place.
        inverse = {
            "op": "add",
            "path": _encode_pointer(_parent_pointer(operation), idx),
            "value": copy.deepcopy(removed),
        }
    else:  # replace
        if idx is None or not 0 <= idx < length:
            raise ApiError(
                PATH_NOT_FOUND,
                f"array index {idx} out of range for 'replace' (length "
                f"{length})",
                loc=loc,
                detail={"path": operation.path},
            )
        inverse = {
            "op": "replace",
            "path": operation.path,
            "value": copy.deepcopy(parent[idx]),
        }
        parent[idx] = copy.deepcopy(operation.value)
    return doc, inverse


def _parse_and_apply(
    document: Any, raw_operations: Any
) -> Tuple[Any, List[Dict[str, Any]], List[_Operation]]:
    """Validate all envelopes, then apply them in order to a deep copy.

    Returns the patched document, one recorded inverse per applied step (in
    forward order) and the parsed operations. Every operation envelope is
    validated before the document is touched, so a structural error never
    produces partial output; semantic failures are detected in order and the
    mutated copy is discarded.
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
    patched = copy.deepcopy(document)
    inverses: List[Dict[str, Any]] = []
    for i, operation in enumerate(operations):
        patched, inverse = _apply_one(patched, operation, i)
        inverses.append(inverse)
    return patched, inverses, operations


def apply_patch(
    document: Any, raw_operations: Any
) -> Dict[str, Any]:
    """Validate and apply an RFC 6902-style patch to a deep copy.

    Every operation envelope is validated before the document is touched, so
    a structural error never produces partial output; semantic failures are
    detected in order and are likewise reported before a response is built.

    Returns ``{"document": patched, "changedPaths": [...]}`` where
    ``changedPaths`` is the symmetric difference of both documents' RFC 6901
    leaf paths (leaves present on one side only, or present on both with
    different canonical values), sorted by pointer.
    """

    patched, _inverses, _operations = _parse_and_apply(document, raw_operations)

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
    return {"document": patched, "changedPaths": changed}


def apply_patch_with_undo(
    document: Any, raw_operations: Any
) -> Dict[str, Any]:
    """Apply a patch and return it together with replayable undo operations.

    This runs the exact same validation and ordered application as
    :func:`apply_patch` (so every failure carries the same
    ``operations[i]``-located error and no partial result is returned), and
    additionally records each forward step's pre-step target state.

    Returns ``{"document": patched, "undoOperations": [...]}`` where
    ``undoOperations`` is the per-step inverse list in **reverse** order, so
    submitting it as the ``operations`` of ``/patch`` with the patched
    document restores the original document byte-for-byte (canonically).

    Inverse details:

    * an array ``add`` (including the ``-`` end token) is undone with a
      ``remove`` at the concrete numeric index the value was inserted at;
    * an array ``remove`` is undone with an ``add`` of the removed value at
      the original numeric index;
    * an object ``add`` of a new member is undone with ``remove``; an
      ``add``/``replace`` over an existing member is undone by restoring the
      previous value;
    * a root-path (``""``) add/replace is undone by restoring the previous
      whole document.
    """

    patched, inverses, _operations = _parse_and_apply(document, raw_operations)
    return {"document": patched, "undoOperations": list(reversed(inverses))}
