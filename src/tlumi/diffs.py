"""Property diff extraction and formatting for tlumi.

Handles Pulumi's property-level change detection and diff expansion for
plan/preview output. Sentinel detection and sanitization live in sanitize.py;
SDK monkey-patching lives in sdk_compat.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final, Literal

from pulumi.automation import OpType
from pulumi.automation.events import StepEventMetadata

import tlumi.sdk_compat  # noqa: F401  -- triggers SDK patch at import time
from tlumi.sanitize import (
    _MAX_DEPTH,
    _SENSITIVE_MARKER,
    _UNKNOWN_MARKER,
    _sanitize_value,
)

_MISSING = object()

# Ops that can have property-level diffs
# CREATE_REPLACEMENT and DELETE_REPLACED are skipped in on_preview() (deduped into REPLACE)
_DIFF_OPS = {OpType.CREATE, OpType.UPDATE, OpType.REPLACE, OpType.DELETE}

_DIFF_VALUE_MAX_LEN: Final = 60


@dataclass(frozen=True, slots=True)
class PropertyChange:
    """A single property-level change within a resource diff."""

    path: str
    kind: Literal["add", "update", "delete"]
    old_value: str | None = None
    new_value: str | None = None
    forces_replacement: bool = False

    def __post_init__(self) -> None:
        if self.kind == "add" and self.old_value is not None:
            raise ValueError("'add' changes must not have old_value")
        if self.kind == "delete" and self.new_value is not None:
            raise ValueError("'delete' changes must not have new_value")
        if self.kind == "update" and self.old_value is None and self.new_value is None:
            raise ValueError("'update' changes must have at least one of old_value/new_value")

    @classmethod
    def add(
        cls, path: str, value: str | None = None, *, forces_replacement: bool = False
    ) -> PropertyChange:
        return cls(path=path, kind="add", new_value=value, forces_replacement=forces_replacement)

    @classmethod
    def delete(
        cls, path: str, value: str | None = None, *, forces_replacement: bool = False
    ) -> PropertyChange:
        return cls(path=path, kind="delete", old_value=value, forces_replacement=forces_replacement)

    @classmethod
    def update(
        cls,
        path: str,
        old_value: str | None = None,
        new_value: str | None = None,
        *,
        forces_replacement: bool = False,
    ) -> PropertyChange:
        return cls(
            path=path,
            kind="update",
            old_value=old_value,
            new_value=new_value,
            forces_replacement=forces_replacement,
        )


def _format_json(value: object) -> str:
    """Serialize a sanitized value to JSON, expanding if longer than _DIFF_VALUE_MAX_LEN chars.

    Markers like (sensitive) and (known after apply) are unquoted in the output.
    """

    def _default(obj):
        return str(obj)

    # ensure_ascii=False keeps non-ASCII values readable and consistent with
    # the string branches (_format_leaf quotes raw characters), and makes the
    # compactness check below measure real rendered width instead of \uXXXX
    # escape sequences (6 chars per non-ASCII character).
    compact = json.dumps(value, separators=(", ", ": "), default=_default, ensure_ascii=False)
    if len(compact) <= _DIFF_VALUE_MAX_LEN:
        result = compact
    else:
        result = json.dumps(value, indent=2, default=_default, ensure_ascii=False)
    # Unquote sentinel markers so they read as bare (sensitive) / (known after
    # apply) rather than quoted strings. This is a blunt string replace: a real
    # cleartext value that happens to equal a marker literally would also be
    # unquoted (shown as masked). That is intentional over-masking -- the safe
    # direction -- and the result here is only ever embedded as a string value
    # by an outer json.dumps, so it never yields invalid JSON in practice.
    result = result.replace(f'"{_SENSITIVE_MARKER}"', _SENSITIVE_MARKER)
    result = result.replace(f'"{_UNKNOWN_MARKER}"', _UNKNOWN_MARKER)
    return result


def _format_leaf(value: object) -> str:
    """Format a leaf value for flattened path output."""
    if isinstance(value, str):
        if value in (_SENSITIVE_MARKER, _UNKNOWN_MARKER):
            return value
        s = f'"{value}"'
        if len(s) > _DIFF_VALUE_MAX_LEN:
            return s[: _DIFF_VALUE_MAX_LEN - 4] + '..."'
        return s
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return str(value)


def _format_flat_leaf(value: object) -> str:
    """Format a raw flattened leaf for display.

    Non-empty containers never reach here (_flatten_raw recurses into them);
    explicitly empty ones keep their key visible with a compact form instead
    of silently dropping it from the diff.
    """
    if isinstance(value, dict):
        return "{}"
    if isinstance(value, list):
        return "[]"
    return _format_leaf(value)


def _strip_dunder_keys(value: object, _depth: int = 0) -> object:
    """Recursively drop Pulumi-internal dunder keys (__defaults, __meta, ...).

    Bridged providers attach bookkeeping like ``__defaults: []`` inside every
    nested object input; those keys are noise in user-facing diffs. The
    top-level filter in extract_property_diffs() only covers depth 0, so
    display paths strip nested occurrences here. Returns new containers
    (never mutates the input); scalars pass through unchanged.
    """
    if _depth >= _MAX_DEPTH:
        return value
    if isinstance(value, dict):
        return {
            k: _strip_dunder_keys(v, _depth + 1)
            for k, v in value.items()
            if not (isinstance(k, str) and k.startswith("__"))
        }
    if isinstance(value, list):
        return [_strip_dunder_keys(v, _depth + 1) for v in value]
    return value


def _flatten_raw(value: object, prefix: str = "", _depth: int = 0) -> list[tuple[str, object]]:
    """Flatten a sanitized dict/list into (relative_path, raw_leaf) pairs.

    Raw leaves are kept so change detection can compare actual values:
    _format_leaf() truncates long strings at a fixed offset, which is
    non-injective, so comparing formatted output would treat two values that
    differ only past the cut as equal. Format with _format_flat_leaf() only
    at display time.

    Pulumi-internal dunder keys (__defaults etc.) are skipped at every level,
    matching the top-level filter in extract_property_diffs().

    Returns empty list at max recursion depth as a safety measure.
    """
    if _depth >= _MAX_DEPTH:
        return []
    if isinstance(value, dict):
        pairs: list[tuple[str, object]] = []
        for k, v in value.items():
            if isinstance(k, str) and k.startswith("__"):
                continue
            sub = f"{prefix}.{k}" if prefix else k
            if isinstance(v, (dict, list)) and v:
                pairs.extend(_flatten_raw(v, sub, _depth + 1))
            else:
                pairs.append((sub, v))
        return pairs
    if isinstance(value, list):
        pairs = []
        for i, v in enumerate(value):
            sub = f"{prefix}[{i}]" if prefix else f"[{i}]"
            if isinstance(v, (dict, list)) and v:
                pairs.extend(_flatten_raw(v, sub, _depth + 1))
            else:
                pairs.append((sub, v))
        return pairs
    return [(prefix, value)]


def _flatten_value(value: object, prefix: str = "", _depth: int = 0) -> list[tuple[str, str]]:
    """Flatten a sanitized dict/list into (relative_path, formatted_leaf) pairs.

    Returns empty list at max recursion depth as a safety measure.
    """
    return [(p, _format_flat_leaf(v)) for p, v in _flatten_raw(value, prefix, _depth)]


def _leaf_equal(a: object, b: object) -> bool:
    """Strict leaf equality for change detection.

    The type check stops Python's cross-type equality (True == 1, 1 == 1.0)
    from hiding a change that would render differently.
    """
    return type(a) is type(b) and a == b


def _deep_equal(a: object, b: object, _depth: int = 0) -> bool:
    """Type-aware deep equality for change detection.

    Python's == is cross-type inside containers too ({"n": True} == {"n": 1}),
    which would hide a nested change that renders differently. Recurses into
    dicts/lists and compares leaves with _leaf_equal().

    At max recursion depth, reports "not equal" as the safe default: showing
    an unchanged value is noise, hiding a real change is the bug.
    """
    if _depth >= _MAX_DEPTH:
        return False
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a) != set(b):
            return False
        return all(_deep_equal(a[k], b[k], _depth + 1) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return False
        return all(_deep_equal(x, y, _depth + 1) for x, y in zip(a, b))
    return _leaf_equal(a, b)


def _join_path(path: str, key: str) -> str:
    """Join a property path with a flattened sub-key.

    An empty key means the flattened value was a scalar at the root (e.g. a
    secret wrapper sanitized down to a marker string): the change applies to
    the path itself, and a plain join would emit a malformed "path." with a
    trailing dot.
    """
    if not key:
        return path
    return f"{path}{key}" if key.startswith("[") else f"{path}.{key}"


def _first_divergence(a: str, b: str) -> int:
    """Index of the first character at which two strings differ."""
    for i, (ca, cb) in enumerate(zip(a, b)):
        if ca != cb:
            return i
    return min(len(a), len(b))


_DIVERGENCE_CONTEXT: Final = 10


def _truncate_at_divergence(value: str, divergence: int) -> str:
    """Quote and truncate a long string keeping the divergence point visible.

    Fixed-offset truncation renders two values that differ only past the cut
    as byte-identical. Anchoring the window shortly before the first
    divergent character keeps a changed pair visibly different.
    """
    start = max(0, divergence - _DIVERGENCE_CONTEXT)
    window_len = _DIFF_VALUE_MAX_LEN - 12  # room for quotes and ellipses
    window = value[start : start + window_len]
    prefix = '"...' if start > 0 else '"'
    suffix = '..."' if start + window_len < len(value) else '"'
    return f"{prefix}{window}{suffix}"


def _format_leaf_pair(old_value: object, new_value: object) -> tuple[str, str]:
    """Format a changed pair of sanitized leaves, keeping them visibly distinct.

    When both sides are long strings whose fixed-offset truncations collide
    (raw values differ only past the cut), re-truncate around the first
    divergent character instead.
    """
    old_fmt = _format_flat_leaf(old_value)
    new_fmt = _format_flat_leaf(new_value)
    if (
        old_fmt == new_fmt
        and isinstance(old_value, str)
        and isinstance(new_value, str)
        and old_value != new_value
    ):
        divergence = _first_divergence(old_value, new_value)
        return (
            _truncate_at_divergence(old_value, divergence),
            _truncate_at_divergence(new_value, divergence),
        )
    return old_fmt, new_fmt


def _format_diff_value(value: object) -> str:
    """Format a value for diff display: quoted strings, lowercase bools, flattened paths."""
    if value is _MISSING:
        return ""
    value = _strip_dunder_keys(_sanitize_value(value))
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        if value in (_SENSITIVE_MARKER, _UNKNOWN_MARKER):
            return value
        s = f'"{value}"'
        if len(s) > _DIFF_VALUE_MAX_LEN:
            return s[: _DIFF_VALUE_MAX_LEN - 4] + '..."'
        return s
    if isinstance(value, (dict, list)):
        # Already sanitized by _sanitize_value above
        compact = _format_json(value)
        if "\n" not in compact:
            return compact
        pairs = _flatten_value(value)
        if not pairs:
            return compact
        max_path = max(len(p) for p, _ in pairs)
        return "\n".join(f"{p:<{max_path}}  {v}" for p, v in pairs)
    return str(value)


def _format_update_pair(old_value: object, new_value: object) -> tuple[str, str]:
    """Format an update's old/new values, keeping truncated strings distinct.

    _format_diff_value() truncates long strings at a fixed offset, so two raw
    values that differ only past it would render byte-identical and the
    change would look like a no-op. On collision, re-truncate around the
    first divergent character.
    """
    old_fmt = _format_diff_value(old_value)
    new_fmt = _format_diff_value(new_value)
    if old_fmt != new_fmt:
        return old_fmt, new_fmt
    old_san = _sanitize_value(old_value)
    new_san = _sanitize_value(new_value)
    if isinstance(old_san, str) and isinstance(new_san, str) and old_san != new_san:
        divergence = _first_divergence(old_san, new_san)
        return (
            _truncate_at_divergence(old_san, divergence),
            _truncate_at_divergence(new_san, divergence),
        )
    return old_fmt, new_fmt


def _expand_value(
    path: str,
    value: object,
    kind: Literal["add", "delete"],
    forces_replacement: bool = False,
) -> list[PropertyChange]:
    """Expand a complex add/delete value into per-leaf PropertyChange entries.

    When value is a dict/list, sanitize and flatten into individual leaf entries.
    Falls back to single PropertyChange with _format_diff_value() for scalars.
    """
    if not isinstance(value, (dict, list)):
        formatted = _format_diff_value(value)
        if kind == "add":
            return [PropertyChange.add(path, formatted, forces_replacement=forces_replacement)]
        return [PropertyChange.delete(path, formatted, forces_replacement=forces_replacement)]

    sanitized = _strip_dunder_keys(_sanitize_value(value))
    pairs = _flatten_value(sanitized)
    if not pairs:
        formatted = _format_diff_value(value)
        if kind == "add":
            return [PropertyChange.add(path, formatted, forces_replacement=forces_replacement)]
        return [PropertyChange.delete(path, formatted, forces_replacement=forces_replacement)]

    changes: list[PropertyChange] = []
    for key, leaf_val in pairs:
        sub_path = _join_path(path, key)
        if kind == "add":
            changes.append(
                PropertyChange.add(sub_path, leaf_val, forces_replacement=forces_replacement)
            )
        else:
            changes.append(
                PropertyChange.delete(sub_path, leaf_val, forces_replacement=forces_replacement)
            )
    return changes


def _expand_complex_diff(
    path: str,
    old_val: object,
    new_val: object,
    forces_replacement: bool = False,
) -> list[PropertyChange]:
    """Expand a complex update (both sides dict/list) into per-leaf inline diffs.

    When both old and new values are dicts or lists, sanitize and flatten each
    side, then diff the raw flattened leaves (formatting only for output),
    producing one PropertyChange per changed leaf. Unchanged sub-fields are
    skipped entirely.

    Falls back to a single PropertyChange with formatted values when:
    - One or both sides are not dict/list (scalar)
    - Both sides are compact enough to fit inline (<=60 chars)

    Returns [] when the values differ only inside Pulumi-internal dunder keys
    (__defaults etc.): once those are stripped for display, the rendered old
    and new would be byte-identical, which reads as an unexplainable no-op
    update. The comparison uses RAW stripped values (pre-sanitize) so
    genuinely changed secrets that both mask to (sensitive) still differ raw
    and are still reported.
    """
    if _deep_equal(_strip_dunder_keys(old_val), _strip_dunder_keys(new_val)):
        return []
    if not (isinstance(old_val, (dict, list)) and isinstance(new_val, (dict, list))):
        old_fmt, new_fmt = _format_update_pair(old_val, new_val)
        return [
            PropertyChange.update(
                path,
                old_fmt,
                new_fmt,
                forces_replacement=forces_replacement,
            )
        ]

    old_san = _strip_dunder_keys(_sanitize_value(old_val))
    new_san = _strip_dunder_keys(_sanitize_value(new_val))

    # If both compact, keep as single inline change
    old_compact = _format_json(old_san)
    new_compact = _format_json(new_san)
    if "\n" not in old_compact and "\n" not in new_compact:
        return [
            PropertyChange.update(
                path,
                old_compact,
                new_compact,
                forces_replacement=forces_replacement,
            )
        ]

    # Compare raw leaves, not formatted ones: truncating formatting is
    # non-injective, so formatted comparison would silently drop changes that
    # differ only past the truncation point.
    old_pairs = dict(_flatten_raw(old_san))
    new_pairs = dict(_flatten_raw(new_san))
    all_keys = sorted(set(old_pairs) | set(new_pairs))

    changes: list[PropertyChange] = []
    for key in all_keys:
        sub_path = _join_path(path, key)
        in_old = key in old_pairs
        in_new = key in new_pairs
        if in_old and in_new:
            if _leaf_equal(old_pairs[key], new_pairs[key]):
                continue  # unchanged
            old_fmt, new_fmt = _format_leaf_pair(old_pairs[key], new_pairs[key])
            changes.append(
                PropertyChange.update(
                    sub_path,
                    old_fmt,
                    new_fmt,
                    forces_replacement=forces_replacement,
                )
            )
        elif in_new:
            changes.append(
                PropertyChange.add(
                    sub_path,
                    _format_flat_leaf(new_pairs[key]),
                    forces_replacement=forces_replacement,
                )
            )
        else:
            changes.append(
                PropertyChange.delete(
                    sub_path,
                    _format_flat_leaf(old_pairs[key]),
                    forces_replacement=forces_replacement,
                )
            )

    # If expansion produced nothing, the raw values differ (the dunder-strip
    # check above returned) but every sanitized leaf compares equal -- e.g.
    # changed secrets that both mask to (sensitive). Fall back to a single
    # change so the update is not silently hidden, even though the rendered
    # values may look identical.
    if not changes:
        return [
            PropertyChange.update(
                path,
                old_compact,
                new_compact,
                forces_replacement=forces_replacement,
            )
        ]

    return changes


def _tokenize_path(path: str) -> list[str]:
    """Split a Pulumi detailed_diff path into key/index segments.

    Pulumi's detailed_diff emits three syntaxes that mix freely:
      - ``field.subfield``      dotted struct access
      - ``field[0]``            list index
      - ``field["map.key"]``    quoted map key (the key may contain dots)

    The naive ``replace("[", ".").replace("]", "").split(".")`` approach
    miscounts the quoted-key case: ``tags["foo.bar"]`` becomes
    ``["tags", "\"foo", "bar\""]`` which never matches anything in the
    actual data.

    This tokenizer walks the path one character at a time, treating
    bracket pairs (quoted or unquoted) as a single segment.
    """
    tokens: list[str] = []
    i = 0
    n = len(path)
    while i < n:
        c = path[i]
        if c == ".":
            i += 1
            continue
        if c == "[":
            # Find the matching closing bracket. Skip a quoted body if present
            # so brackets/dots inside the quotes don't terminate the segment.
            j = i + 1
            if j < n and path[j] in ('"', "'"):
                quote = path[j]
                j += 1
                while j < n and path[j] != quote:
                    j += 1
                # j now points at the closing quote (or past the end)
                if j < n:
                    tokens.append(path[i + 2 : j])
                    j += 1  # consume the closing quote
                # Skip until ']'
                while j < n and path[j] != "]":
                    j += 1
            else:
                # Unquoted bracket body (list index or unquoted key)
                while j < n and path[j] != "]":
                    j += 1
                tokens.append(path[i + 1 : j])
            i = j + 1 if j < n else j
            continue
        # Dotted/leading segment: read until the next '.' or '['
        j = i
        while j < n and path[j] not in (".", "["):
            j += 1
        if j > i:
            tokens.append(path[i:j])
        i = j
    return [t for t in tokens if t]


def _get_nested(data: object, path: str) -> tuple[bool, object]:
    """Traverse a dotted/bracketed Pulumi detailed_diff path into nested data.

    Returns (found, value). Handles dotted fields (``a.b.c``), list indices
    (``a[0]``), and quoted map keys (``tags["foo"]``, including keys that
    themselves contain dots like ``tags["k8s.io/role"]``).
    """
    current: object = data
    for segment in _tokenize_path(path):
        if isinstance(current, dict):
            if segment not in current:
                return False, None
            current = current[segment]
        elif isinstance(current, list):
            try:
                current = current[int(segment)]
            except (ValueError, IndexError):
                return False, None
        else:
            return False, None
    return True, current


def extract_property_diffs(meta: StepEventMetadata) -> list[PropertyChange]:
    """Extract property-level changes from a StepEventMetadata.

    Five paths:
    0. Non-diff ops (SAME, etc.): early return []
    1. CREATE: all new inputs shown as "add" entries
    2. DELETE: all old inputs shown as "delete" entries
    3. UPDATE/REPLACE with detailed_diff (populated natively by the pinned SDK)
    4. UPDATE/REPLACE fallback: compare meta.old.inputs vs meta.new.inputs
       using meta.diffs as the changed-key list, with one level of sub-key
       diffing for dict values (handles nested dicts by diffing sub-keys)
    """
    op = meta.op
    if op not in _DIFF_OPS:
        return []

    # CREATE: show all new inputs as "add" entries
    if op == OpType.CREATE:
        new_inputs = getattr(meta.new, "inputs", {}) if meta.new else {}
        if not new_inputs:
            return []
        changes: list[PropertyChange] = []
        for key in sorted(new_inputs):
            # Pulumi stores internal metadata under dunder keys (__defaults,
            # __provider, etc.); these are noise in user-facing diffs.
            if key.startswith("__"):
                continue
            changes.extend(_expand_value(key, new_inputs[key], "add"))
        return changes

    # DELETE: show all old inputs as "delete" entries
    if op == OpType.DELETE:
        old_inputs = getattr(meta.old, "inputs", {}) if meta.old else {}
        if not old_inputs:
            return []
        changes = []
        for key in sorted(old_inputs):
            # Pulumi stores internal metadata under dunder keys (__defaults,
            # __provider, etc.); these are noise in user-facing diffs.
            if key.startswith("__"):
                continue
            changes.extend(_expand_value(key, old_inputs[key], "delete"))
        return changes

    # Determine which keys force replacement
    replacement_keys: set[str] = set()
    if hasattr(meta, "keys") and meta.keys:
        replacement_keys = set(meta.keys)

    # Strategy 1: detailed_diff from the SDK (populated natively; pulumi/pulumi#21713)
    detailed = getattr(meta, "detailed_diff", None)
    if detailed:
        changes = []
        for path, pdiff in sorted(detailed.items()):
            # Skip paths touching Pulumi-internal dunder segments
            # (__defaults etc.) -- noise, same as the top-level filter.
            if any(seg.startswith("__") for seg in _tokenize_path(path)):
                continue
            diff_kind = getattr(pdiff, "diff_kind", None)
            if diff_kind is None:
                continue
            kind_str = str(diff_kind.value) if hasattr(diff_kind, "value") else str(diff_kind)

            forces = "replace" in kind_str
            if "add" in kind_str:
                kind = "add"
            elif "delete" in kind_str:
                kind = "delete"
            else:
                kind = "update"

            old_inputs = getattr(meta.old, "inputs", {}) if meta.old else {}
            new_inputs = getattr(meta.new, "inputs", {}) if meta.new else {}

            if kind == "update":
                found_old, raw_old = _get_nested(old_inputs, path)
                found_new, raw_new = _get_nested(new_inputs, path)
                if found_old and found_new:
                    changes.extend(_expand_complex_diff(path, raw_old, raw_new, forces))
                elif not found_old and not found_new:
                    # The SDK reported an update at a path neither side has -- a
                    # detailed_diff/inputs inconsistency. PropertyChange.update
                    # would raise "must have at least one of old/new" and the
                    # callback wrapper would count a silent error. Skip and
                    # let the next preview reconcile.
                    continue
                else:
                    old_val = _format_diff_value(raw_old) if found_old else None
                    new_val = _format_diff_value(raw_new) if found_new else None
                    changes.append(
                        PropertyChange.update(
                            path,
                            old_val,
                            new_val,
                            forces_replacement=forces,
                        )
                    )
            elif kind == "add":
                found, val = _get_nested(new_inputs, path)
                if found:
                    changes.extend(_expand_value(path, val, "add", forces))
                else:
                    changes.append(PropertyChange.add(path, forces_replacement=forces))
            else:  # delete
                found, val = _get_nested(old_inputs, path)
                if found:
                    changes.extend(_expand_value(path, val, "delete", forces))
                else:
                    changes.append(PropertyChange.delete(path, forces_replacement=forces))
        return changes

    # Strategy 2: compare old.inputs vs new.inputs using meta.diffs
    diffs_list = getattr(meta, "diffs", None)
    if not diffs_list:
        return []

    # getattr's default does not help when the attribute exists but is None
    # (StepEventStateMetadata.from_json uses inputs=data.get("inputs")), so
    # coerce None to {} -- otherwise `key in None` below raises TypeError, the
    # on_preview wrapper swallows it as a callback error, and the property diffs
    # silently vanish.
    old_inputs = (getattr(meta.old, "inputs", None) or {}) if meta.old else {}
    new_inputs = (getattr(meta.new, "inputs", None) or {}) if meta.new else {}
    if not old_inputs and not new_inputs:
        return []

    changes = []
    for key in diffs_list:
        # Pulumi-internal dunder keys (__defaults etc.) are noise in
        # user-facing diffs, same as the CREATE/DELETE top-level filter.
        if isinstance(key, str) and key.startswith("__"):
            continue
        forces = key in replacement_keys
        has_old = key in old_inputs
        has_new = key in new_inputs
        old_val = old_inputs[key] if has_old else _MISSING  # type: ignore[assignment]
        new_val = new_inputs[key] if has_new else _MISSING  # type: ignore[assignment]

        # One level of sub-key diffing when both sides are dicts
        if isinstance(old_val, dict) and isinstance(new_val, dict):
            old_dict = old_val
            new_dict = new_val
            all_subkeys = sorted(set(old_dict) | set(new_dict))
            for sk in all_subkeys:
                # Nested Pulumi-internal dunder keys (__defaults etc.) are
                # noise, same as at the top level.
                if isinstance(sk, str) and sk.startswith("__"):
                    continue
                sub_path = f"{key}.{sk}"
                has_sub_old = sk in old_dict
                has_sub_new = sk in new_dict
                sub_old = old_dict[sk] if has_sub_old else _MISSING
                sub_new = new_dict[sk] if has_sub_new else _MISSING
                # _deep_equal, not ==: Python's cross-type equality (True == 1,
                # 1 == 1.0, also inside containers) would hide a change that
                # renders differently.
                if has_sub_old and has_sub_new and _deep_equal(sub_old, sub_new):
                    continue
                if sub_old is _MISSING:
                    changes.extend(_expand_value(sub_path, sub_new, "add", forces))
                elif sub_new is _MISSING:
                    changes.extend(_expand_value(sub_path, sub_old, "delete", forces))
                else:
                    changes.extend(_expand_complex_diff(sub_path, sub_old, sub_new, forces))
        else:
            if old_val is _MISSING and new_val is _MISSING:
                # Key listed in meta.diffs but absent from both inputs -- an SDK
                # inconsistency. Skip rather than emit a phantom "+ key" with an
                # empty value (mirrors the Strategy-1 guard and the sub-key loop).
                continue
            if old_val is _MISSING:
                changes.extend(_expand_value(key, new_val, "add", forces))
            elif new_val is _MISSING:
                changes.extend(_expand_value(key, old_val, "delete", forces))
            elif isinstance(old_val, (dict, list)) and isinstance(new_val, (dict, list)):
                changes.extend(_expand_complex_diff(key, old_val, new_val, forces))
            else:
                old_fmt, new_fmt = _format_update_pair(old_val, new_val)
                changes.append(
                    PropertyChange.update(
                        key,
                        old_fmt,
                        new_fmt,
                        forces_replacement=forces,
                    )
                )

    return changes
