"""Three-way merge of two JSON documents branched from a common base.

The merge descends **object members only**, addressing every slot by its RFC
6901 path, and applies the usual three-way rule relative to ``base``:

* a slot changed on exactly one side takes that side's value (an unchanged
  side never blocks the other side's edit, including its deletion);
* when both sides changed the slot to the *same canonical JSON value*, that
  value is taken;
* divergent changes at the same path — including delete-versus-modify,
  delete-versus-add's sibling edit, and two different add values — are
  reported as conflicts.

Two deliberate strictness choices match the rest of the service:

* only objects are recursed into; an **array is one atomic value**, so two
  sides editing different elements of the same array conflict instead of
  being element-wise spliced;
* equality is **canonical JSON equality**, so a missing member is not the
  same as ``null`` and ``1`` is not the same as ``1.0`` (nor ``true``).

Conflict entries carry, for each of the three sides, an explicit presence
flag and the value, so absence stays distinguishable from a JSON ``null``.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List

from . import pointers
from .canonical import canonical_json_bytes

# Sentinel for "the member does not exist on this side". It must be a
# distinct object so that an actual JSON null is never confused with absence.
_MISSING = object()


def _canonical_equal(left: Any, right: Any) -> bool:
    """Compare two present JSON values by canonical bytes.

    This keeps ``1`` != ``1.0`` (integer vs float encodings), ``true`` != 1
    and object member order insignificant, exactly as /diff and /commit do.
    """
    return canonical_json_bytes(left) == canonical_json_bytes(right)


def _side(value: Any) -> Dict[str, Any]:
    """Render one side of a conflict slot as ``{"present", "value"}``."""
    if value is _MISSING:
        return {"present": False, "value": None}
    return {"present": True, "value": value}


def _conflict(path: str, base: Any, left: Any, right: Any) -> Dict[str, Any]:
    return {
        "path": path,
        "base": _side(base),
        "left": _side(left),
        "right": _side(right),
    }


def _merge_objects(
    base: Dict[str, Any],
    left: Dict[str, Any],
    right: Dict[str, Any],
    prefix: str,
    conflicts: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Merge three objects present at *prefix* member by member.

    Output member order is deterministic: members already in ``base`` keep
    its insertion order (those that survive the merge), followed by members
    introduced on either side, sorted by Unicode code point.  This mirrors
    RFC 6901's key ordering without depending on either edit's key order.
    """
    merged: Dict[str, Any] = {}
    base_keys = list(base.keys())
    added_keys = sorted((set(left.keys()) | set(right.keys())) - set(base_keys))
    for key in base_keys + added_keys:
        path = pointers.build_pointer(prefix, key)
        result = _merge_slot(
            base.get(key, _MISSING),
            left.get(key, _MISSING),
            right.get(key, _MISSING),
            path,
            conflicts,
        )
        if result is not _MISSING:
            merged[key] = result
    return merged


def _merge_slot(
    base: Any,
    left: Any,
    right: Any,
    path: str,
    conflicts: List[Dict[str, Any]],
) -> Any:
    """Merge one addressed slot; return the winner or :data:`_MISSING`.

    Conflicts discovered below the slot are appended to *conflicts*; on
    return the slot may still be missing (a deletion) even when conflicts
    elsewhere exist — the endpoint discards every merged value whenever the
    conflict list is non-empty.
    """
    # Member-wise recursion applies only when all three sides hold an object
    # at this path. Replacing a scalar/base with an object, or deleting one
    # side's object, falls through to the atomic three-way rule.
    if (
        isinstance(base, dict)
        and isinstance(left, dict)
        and isinstance(right, dict)
    ):
        return _merge_objects(base, left, right, path, conflicts)

    left_present = left is not _MISSING
    right_present = right is not _MISSING

    if not left_present and not right_present:
        # Both sides lack the member: either both deleted it or neither side
        # introduced it; either way the merged result lacks it.
        return _MISSING

    if not left_present:
        if base is _MISSING:
            # Added on the right only; the left simply never had this slot.
            return copy.deepcopy(right)
        if not _canonical_equal(right, base):
            # Left deleted the member while the right modified it: conflict.
            conflicts.append(_conflict(path, base, left, right))
            return _MISSING
        # Right kept the base value, left deleted it: the deletion wins.
        return _MISSING

    if not right_present:
        if base is _MISSING:
            # Added on the left only.
            return copy.deepcopy(left)
        if not _canonical_equal(left, base):
            # Right deleted the member while the left modified it: conflict.
            conflicts.append(_conflict(path, base, left, right))
            return _MISSING
        return _MISSING

    # Both sides hold the slot.
    if base is _MISSING:
        # Added independently by both sides: they must agree canonically.
        if _canonical_equal(left, right):
            return copy.deepcopy(left)
        conflicts.append(_conflict(path, base, left, right))
        return _MISSING

    left_changed = not _canonical_equal(left, base)
    right_changed = not _canonical_equal(right, base)

    if not left_changed:
        # Left untouched: the right value is either the base itself or the
        # sole edit (identical concurrent edits included below).
        return copy.deepcopy(right)
    if not right_changed:
        return copy.deepcopy(left)
    if _canonical_equal(left, right):
        # Both sides made exactly the same canonical change.
        return copy.deepcopy(left)

    conflicts.append(_conflict(path, base, left, right))
    return _MISSING


def merge_documents(
    base: Any, left: Any, right: Any
) -> Dict[str, Any]:
    """Three-way-merge *left* and *right* (both branched from *base*).

    Returns ``{"merged": <document>}`` when every path reconciles, or
    ``{"conflicts": [...]}`` with every divergent path sorted by RFC 6901
    pointer and no partial merged document.
    """
    conflicts: List[Dict[str, Any]] = []
    merged = _merge_slot(base, left, right, "", conflicts)
    if conflicts:
        conflicts.sort(key=lambda entry: entry["path"])
        return {"conflicts": conflicts}
    return {"merged": merged}
