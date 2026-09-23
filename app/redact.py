"""Deterministic leaf redaction driven by RFC 6901 fragment patterns.

A rule is ``{"pattern": ..., "action": "mask"|"drop", "value"?: ...}`` whose
``pattern`` is an RFC 6901 pointer fragment supporting three kinds of
reference token:

* a literal token (e.g. ``/subject/name``, ``/a/0``; ``~0``/``~1`` escapes
  work exactly as in a plain RFC 6901 pointer);
* ``*`` matches exactly one token, whatever it is;
* ``**`` matches zero or more tokens (a recursive wildcard, e.g. ``/**/name``
  matches both ``/name`` and ``/a/b/name``).

The empty fragment ``""`` is the literal root pointer and matches only a
root-scalar/root-empty-container document.

Redaction operates on the same leaf model as :mod:`app.tree` (scalars,
``null`` and empty ``{}`` / ``[]`` are leaves), so empty containers are
redactable even though they have no descendants:

* ``mask`` replaces the leaf's value with a fixed constant (the rule's
  ``value``, any JSON value);
* ``drop`` removes an object member or sets an array element to ``null``;
  dropping the whole document (the root pointer) is rejected.

When one leaf is hit by several rules the winner is deterministic:

1. more literal tokens beat fewer (``*``/``**`` are both non-literal);
2. at an equal literal count, more ``*`` tokens beat more ``**`` tokens;
3. still tied, the rule appearing first in the request wins.

Every rule envelope (including its pattern syntax) is validated before the
document is touched, so a bad rule never yields a partially redacted result.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from . import pointers
from .errors import (
    ApiError,
    INVALID_REQUEST,
    POINTER_SYNTAX,
)
from .validation import validate_json_value

SUPPORTED_ACTIONS = ("mask", "drop")
_LITERAL = 0
_STAR = 1
_GLOBSTAR = 2


@dataclass(frozen=True)
class RedactRule:
    """One parsed redaction rule (the pattern is already tokenized)."""

    index: int
    pattern: str
    tokens: Tuple[str, ...]
    action: str
    value: Any = None

    @property
    def specificity(self) -> Tuple[int, int, int]:
        """Sort key for the literal > '*' > '**' precedence.

        Bigger is more specific; the negated rule index keeps the request's
        array order as the final tie-breaker (earlier wins).
        """
        literals = sum(1 for kind in self._kinds() if kind == _LITERAL)
        stars = sum(1 for kind in self._kinds() if kind == _STAR)
        return (literals, stars, -self.index)

    def _kinds(self) -> List[int]:
        return [_token_kind(token) for token in self.tokens]


def _token_kind(token: str) -> int:
    if token == "**":
        return _GLOBSTAR
    if token == "*":
        return _STAR
    return _LITERAL


def _parse_pattern(pattern: str, loc: str) -> Tuple[str, ...]:
    """Validate an RFC 6901 fragment with ``*``/``**`` wildcard tokens.

    Escape validation (``~`` must be followed by ``0``/``1``) is delegated to
    :func:`pointers.parse_pointer` so plain pointers and rule patterns share
    one syntax; a ``*`` may only appear as a whole token — a token that mixes
    a star with other characters cannot be a literal member name either
    (stars are not escapable), so it is rejected as a pattern error.
    """
    try:
        tokens = tuple(pointers.parse_pointer(pattern))
    except pointers.PointerError as exc:
        raise ApiError(
            POINTER_SYNTAX, str(exc), loc=loc, detail={"pattern": pattern}
        ) from exc

    for token in tokens:
        if "*" in token and token not in ("*", "**"):
            raise ApiError(
                POINTER_SYNTAX,
                f"pattern token {token!r} mixes '*' with other characters: "
                "'*' may only appear as a whole token matching one level, "
                "or '**' matching any number of levels",
                loc=loc,
                detail={"pattern": pattern, "token": token},
            )
    return tokens


def _parse_rule(raw: Any, index: int) -> RedactRule:
    """Structurally validate one rule envelope (no document access)."""

    loc = f"rules[{index}]"
    if not isinstance(raw, dict):
        raise ApiError(
            INVALID_REQUEST,
            "rule must be an object {pattern, action[, value]}, got "
            f"{type(raw).__name__}",
            loc=loc,
        )

    if "pattern" not in raw:
        raise ApiError(
            INVALID_REQUEST, "missing required field 'pattern'",
            loc=f"{loc}.pattern",
        )
    pattern = raw["pattern"]
    if not isinstance(pattern, str):
        raise ApiError(
            INVALID_REQUEST,
            f"'pattern' must be an RFC 6901 fragment string, got "
            f"{type(pattern).__name__}",
            loc=f"{loc}.pattern",
        )
    tokens = _parse_pattern(pattern, loc)

    if "action" not in raw:
        raise ApiError(
            INVALID_REQUEST, "missing required field 'action'",
            loc=f"{loc}.action",
        )
    action = raw["action"]
    if not isinstance(action, str):
        raise ApiError(
            INVALID_REQUEST,
            f"'action' must be a string, got {type(action).__name__}",
            loc=f"{loc}.action",
        )
    if action not in SUPPORTED_ACTIONS:
        raise ApiError(
            INVALID_REQUEST,
            f"unsupported action {action!r}: only "
            f"{', '.join(SUPPORTED_ACTIONS)} are supported",
            loc=f"{loc}.action",
        )

    value: Any = None
    if action == "mask":
        if "value" not in raw:
            raise ApiError(
                INVALID_REQUEST,
                "action 'mask' requires a 'value' field holding the "
                "replacement constant",
                loc=f"{loc}.value",
            )
        value = raw["value"]
        validate_json_value(value, loc=f"{loc}.value")

    return RedactRule(
        index=index,
        pattern=pattern,
        tokens=tokens,
        action=action,
        value=value,
    )


# ---------------------------------------------------------------------------
# Pattern matching
# ---------------------------------------------------------------------------


def _pattern_matches(pattern_tokens: Tuple[str, ...], path_tokens: Tuple[str, ...]):
    """Memoized wildcard match between a pattern and a concrete leaf path.

    Literal tokens compare for equality, ``*`` consumes exactly one path
    token and ``**`` consumes zero or more; every memo key maps to a boolean.
    """
    memo: Dict[Tuple[int, int], bool] = {}

    def match(pi: int, li: int) -> bool:
        key = (pi, li)
        if key in memo:
            return memo[key]

        if pi == len(pattern_tokens):
            result = li == len(path_tokens)
        elif pattern_tokens[pi] == "**":
            # '**' matches zero tokens (skip it) or one token (stay on it).
            result = match(pi + 1, li) or (
                li < len(path_tokens) and match(pi, li + 1)
            )
        elif li == len(path_tokens):
            result = False
        elif pattern_tokens[pi] == "*":
            result = match(pi + 1, li + 1)
        else:
            result = (
                pattern_tokens[pi] == path_tokens[li]
                and match(pi + 1, li + 1)
            )

        memo[key] = result
        return result

    return match(0, 0)


def _winning_rule(
    path_tokens: Tuple[str, ...], rules: List[RedactRule]
) -> RedactRule | None:
    """Return the highest-precedence rule matching *path_tokens*, if any."""
    winner: RedactRule | None = None
    for rule in rules:
        if _pattern_matches(rule.tokens, path_tokens):
            if winner is None or rule.specificity > winner.specificity:
                winner = rule
    return winner


# ---------------------------------------------------------------------------
# Leaf traversal + application
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _LeafTarget:
    """A leaf plus the handle of its parent slot (None for the root leaf)."""

    path: str
    tokens: Tuple[str, ...]
    value: Any
    parent: Any
    key: Any  # str for objects, int for arrays, None for the root leaf


def _collect_leaf_targets(doc: Any) -> List[_LeafTarget]:
    """Depth-first walk yielding every leaf with its addressing handle.

    The same walk/leaf rules as :func:`app.tree.expand_leaves` apply:
    scalars, ``null`` and empty ``{}`` / ``[]`` are leaves.  The parent/key
    handles are captured up front so member removal or element nulling later
    does not disturb the traversal.
    """
    targets: List[_LeafTarget] = []

    def walk(value: Any, pointer: str, tokens: Tuple[str, ...],
             parent: Any, key: Any) -> None:
        if isinstance(value, dict) and len(value) > 0:
            for member, child in value.items():
                walk(
                    child,
                    pointers.build_pointer(pointer, member),
                    tokens + (member,),
                    value,
                    member,
                )
        elif isinstance(value, list) and len(value) > 0:
            for idx, child in enumerate(value):
                walk(
                    child,
                    pointers.build_pointer(pointer, idx),
                    tokens + (str(idx),),
                    value,
                    idx,
                )
        else:
            targets.append(
                _LeafTarget(
                    path=pointer,
                    tokens=tokens,
                    value=value,
                    parent=parent,
                    key=key,
                )
            )

    walk(doc, "", (), None, None)
    return targets


def redact_document(document: Any, raw_rules: Any) -> Dict[str, Any]:
    """Validate *raw_rules* and apply the winning rule to every leaf.

    Returns ``{"document": redacted, "applied": [...]}`` where ``applied``
    lists one entry per redacted leaf sorted by RFC 6901 pointer.  Rules are
    fully validated first; rule selection is then computed against the
    original document and applied to a deep copy, keeping the operation
    atomic and order-independent.
    """

    if not isinstance(raw_rules, list):
        raise ApiError(
            INVALID_REQUEST,
            "'rules' must be an array of {pattern, action[, value]} objects",
            loc="rules",
        )
    rules: List[RedactRule] = [
        _parse_rule(raw, i) for i, raw in enumerate(raw_rules)
    ]

    # Decide every leaf's fate before mutating anything; dropping a member
    # cannot change another decision because decisions are computed from the
    # original shape.
    targets = _collect_leaf_targets(document)
    decisions: List[Tuple[_LeafTarget, RedactRule]] = []
    for target in targets:
        rule = _winning_rule(target.tokens, rules)
        if rule is None:
            continue
        if rule.action == "drop" and target.parent is None:
            raise ApiError(
                INVALID_REQUEST,
                f"pattern {rule.pattern!r} drops the whole document: the "
                "root pointer '' cannot be used with action 'drop' "
                "(object members are removed and array elements nulled, "
                "but the document itself has no parent slot)",
                loc=f"rules[{rule.index}]",
                detail={"pattern": rule.pattern, "path": ""},
            )
        decisions.append((target, rule))

    result = copy.deepcopy(document)
    # Re-walk the copy and apply by path order; parent handles were captured
    # against the original, so resolve them once more against the copy.
    copy_targets = {target.path: target for target in _collect_leaf_targets(result)}
    applied: List[Dict[str, Any]] = []
    for target, rule in sorted(decisions, key=lambda item: item[0].path):
        copied = copy_targets[target.path]
        if rule.action == "mask":
            replacement = copy.deepcopy(rule.value)
            if copied.parent is None:
                result = replacement
            else:
                copied.parent[copied.key] = replacement
        else:  # drop
            if isinstance(copied.parent, dict):
                del copied.parent[copied.key]
            else:
                copied.parent[copied.key] = None
        applied.append(
            {
                "path": target.path,
                "action": rule.action,
                "ruleIndex": rule.index,
                "pattern": rule.pattern,
            }
        )

    return {"document": result, "applied": applied}
