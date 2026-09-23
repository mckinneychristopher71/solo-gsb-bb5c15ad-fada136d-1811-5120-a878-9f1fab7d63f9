"""Leaf-level diffing for two JSON documents.

The diff uses exactly the same leaf model as the Merkle tree: scalars
(including ``null``) and empty objects/arrays are leaves, while non-empty
containers are expanded into their RFC 6901 descendants.  Value equality is
determined by canonical JSON bytes, so ``1`` and ``1.0`` (and ``true`` and
``1``) are distinct even though Python considers them equal.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence

from .canonical import canonical_scalar_bytes
from .tree import expand_leaves


@dataclass(frozen=True)
class LeafFact:
    """A leaf value together with its canonical-value fingerprint."""

    value: Any
    fingerprint: str


def leaf_facts(doc: Any) -> Dict[str, LeafFact]:
    """Expand *doc* and fingerprint every leaf's canonical value."""
    facts: Dict[str, LeafFact] = {}
    for leaf in expand_leaves(doc):
        canonical_value = canonical_scalar_bytes(leaf.value)
        facts[leaf.pointer] = LeafFact(
            value=leaf.value,
            fingerprint=hashlib.sha256(canonical_value).hexdigest(),
        )
    return facts


def _entry(
    path: str,
    before: Optional[LeafFact],
    after: Optional[LeafFact],
) -> Dict[str, Any]:
    return {
        "path": path,
        "beforeValue": None if before is None else before.value,
        "afterValue": None if after is None else after.value,
        "beforeFingerprint": None if before is None else before.fingerprint,
        "afterFingerprint": None if after is None else after.fingerprint,
    }


def diff_documents(
    before: Any,
    after: Any,
    paths: Optional[Sequence[str]] = None,
) -> Dict[str, list[Dict[str, Any]]]:
    """Align selected leaf paths and split them into added/removed/changed.

    When *paths* is ``None`` the union of both documents' leaf paths is used.
    An empty sequence is an explicit filter selecting no paths.  Each returned
    group is sorted by RFC 6901 pointer.
    """
    before_facts = leaf_facts(before)
    after_facts = leaf_facts(after)

    if paths is None:
        selected_paths = sorted(set(before_facts) | set(after_facts))
    else:
        selected_paths = sorted(set(paths))

    added: list[Dict[str, Any]] = []
    removed: list[Dict[str, Any]] = []
    changed: list[Dict[str, Any]] = []

    for path in selected_paths:
        before_fact = before_facts.get(path)
        after_fact = after_facts.get(path)

        if before_fact is None:
            added.append(_entry(path, None, after_fact))
        elif after_fact is None:
            removed.append(_entry(path, before_fact, None))
        elif before_fact.fingerprint != after_fact.fingerprint:
            changed.append(_entry(path, before_fact, after_fact))

    return {"added": added, "removed": removed, "changed": changed}
