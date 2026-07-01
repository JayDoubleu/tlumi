"""Tests for tlumi.diffs: property-diff extraction edge cases.

Covers regressions confirmed by the public-release review:
- truncation must not silently drop changed leaves (compare raw, format later)
- top-level secret wrappers must not produce a trailing-dot path
- non-ASCII values render without \\uXXXX escaping
- detailed_diff (Strategy 1) add/delete branches pull from the correct side
"""

from __future__ import annotations

from unittest.mock import MagicMock

from pulumi.automation import OpType

from tlumi.diffs import (
    _expand_complex_diff,
    _format_json,
    extract_property_diffs,
)
from tlumi.sanitize import _PULUMI_SECRET_SIG, _PULUMI_SECRET_VALUE


def _meta(op, *, diffs=None, detailed_diff=None, old_inputs=None, new_inputs=None):
    meta = MagicMock()
    meta.op = op
    meta.diffs = diffs
    meta.keys = None
    meta.detailed_diff = detailed_diff
    meta.old = MagicMock(inputs=old_inputs) if old_inputs is not None else None
    meta.new = MagicMock(inputs=new_inputs) if new_inputs is not None else None
    return meta


def _detailed(diff_kind_value):
    pdiff = MagicMock()
    pdiff.diff_kind = MagicMock()
    pdiff.diff_kind.value = diff_kind_value
    return pdiff


_SHARED = "arn:aws:iam::123456789012:policy/very-long-shared-prefix-name-"
_OLD = _SHARED + "OLD"
_NEW = _SHARED + "NEW"


# ---------------------------------------------------------------------------
# Truncation must not drop changed leaves (#0)
# ---------------------------------------------------------------------------


def test_long_changed_leaf_not_dropped_strategy2():
    """A leaf differing only past the truncation point is still reported (meta.diffs path)."""
    old = {"web": {"policyArn": _OLD, "replicas": 2}}
    new = {"web": {"policyArn": _NEW, "replicas": 3}}
    changes = extract_property_diffs(
        _meta(
            OpType.UPDATE, diffs=["config"], old_inputs={"config": old}, new_inputs={"config": new}
        )
    )
    paths = {c.path for c in changes}
    assert "config.web.policyArn" in paths
    assert "config.web.replicas" in paths


def test_long_changed_leaf_not_dropped_strategy1():
    """Same, via the detailed_diff (Strategy 1) path."""
    old = {"web": {"policyArn": _OLD, "replicas": 2}}
    new = {"web": {"policyArn": _NEW, "replicas": 3}}
    changes = extract_property_diffs(
        _meta(
            OpType.UPDATE,
            detailed_diff={"config": _detailed("update")},
            old_inputs={"config": old},
            new_inputs={"config": new},
        )
    )
    paths = {c.path for c in changes}
    assert "config.web.policyArn" in paths


def test_truncated_update_values_remain_distinct():
    """A long top-level update renders visibly different old/new (divergence-aware truncation)."""
    changes = extract_property_diffs(
        _meta(
            OpType.UPDATE,
            diffs=["policyArn"],
            old_inputs={"policyArn": _OLD},
            new_inputs={"policyArn": _NEW},
        )
    )
    update = [c for c in changes if c.kind == "update" and c.path == "policyArn"]
    assert len(update) == 1
    assert update[0].old_value != update[0].new_value


def test_unchanged_long_leaf_not_reported():
    """A leaf whose raw value is identical (even if long) is not falsely reported as changed."""
    same = _SHARED + "SAME"
    old = {"web": {"policyArn": same, "replicas": 2}}
    new = {"web": {"policyArn": same, "replicas": 3}}
    changes = extract_property_diffs(
        _meta(
            OpType.UPDATE, diffs=["config"], old_inputs={"config": old}, new_inputs={"config": new}
        )
    )
    paths = {c.path for c in changes}
    assert "config.web.policyArn" not in paths
    assert "config.web.replicas" in paths


# ---------------------------------------------------------------------------
# Top-level secret-wrapper path has no trailing dot (#1)
# ---------------------------------------------------------------------------


def test_secret_wrapper_add_has_no_trailing_dot():
    secret = {_PULUMI_SECRET_SIG: _PULUMI_SECRET_VALUE, "value": "hunter2"}
    changes = extract_property_diffs(_meta(OpType.CREATE, new_inputs={"password": secret}))
    pw = [c for c in changes if c.path.startswith("password")]
    assert pw, "expected a change for the secret input"
    for c in pw:
        assert not c.path.endswith("."), f"malformed trailing-dot path: {c.path!r}"
        assert c.path == "password"
        assert c.new_value == "(sensitive)"


# ---------------------------------------------------------------------------
# _format_json keeps non-ASCII readable (#2)
# ---------------------------------------------------------------------------


def test_format_json_preserves_non_ascii():
    out = _format_json({"city": "München", "emoji": "✓"})
    assert "München" in out
    assert "✓" in out
    assert "\\u" not in out


# ---------------------------------------------------------------------------
# detailed_diff add/delete branches pull from the correct side (#32)
# ---------------------------------------------------------------------------


def test_detailed_diff_add_uses_new_side():
    changes = extract_property_diffs(
        _meta(
            OpType.UPDATE,
            detailed_diff={"tags": _detailed("add")},
            old_inputs={},
            new_inputs={"tags": "production"},
        )
    )
    add = [c for c in changes if c.path == "tags"]
    assert len(add) == 1
    assert add[0].kind == "add"
    assert add[0].new_value == '"production"'
    assert add[0].old_value is None
    assert add[0].forces_replacement is False


def test_detailed_diff_delete_uses_old_side():
    changes = extract_property_diffs(
        _meta(
            OpType.UPDATE,
            detailed_diff={"tags": _detailed("delete")},
            old_inputs={"tags": "production"},
            new_inputs={},
        )
    )
    delete = [c for c in changes if c.path == "tags"]
    assert len(delete) == 1
    assert delete[0].kind == "delete"
    assert delete[0].old_value == '"production"'
    assert delete[0].new_value is None


def test_detailed_diff_add_replace_forces_replacement():
    changes = extract_property_diffs(
        _meta(
            OpType.REPLACE,
            detailed_diff={"name": _detailed("add-replace")},
            old_inputs={},
            new_inputs={"name": "x"},
        )
    )
    add = [c for c in changes if c.path == "name"]
    assert add and add[0].kind == "add" and add[0].forces_replacement is True


def test_detailed_diff_delete_replace_forces_replacement():
    changes = extract_property_diffs(
        _meta(
            OpType.REPLACE,
            detailed_diff={"name": _detailed("delete-replace")},
            old_inputs={"name": "x"},
            new_inputs={},
        )
    )
    delete = [c for c in changes if c.path == "name"]
    assert delete and delete[0].kind == "delete" and delete[0].forces_replacement is True


def test_detailed_diff_add_value_not_found_fallback():
    """When the added path is absent from new inputs, a value-less add is emitted."""
    changes = extract_property_diffs(
        _meta(
            OpType.UPDATE,
            detailed_diff={"ghost": _detailed("add")},
            old_inputs={},
            new_inputs={},
        )
    )
    add = [c for c in changes if c.path == "ghost"]
    assert len(add) == 1
    assert add[0].kind == "add"
    assert add[0].new_value is None


def test_expand_complex_diff_leaf_only_in_new_is_add():
    # Values must be large enough to exceed the inline-compact threshold so the
    # per-leaf flatten/diff path runs (small dicts intentionally stay compact).
    keep = "z" * 60
    old = {"keep": keep}
    new = {"keep": keep, "added": "new-value"}
    changes = _expand_complex_diff("cfg", old, new, False)
    by_path = {c.path: c for c in changes}
    assert "cfg.added" in by_path
    assert by_path["cfg.added"].kind == "add"


def test_expand_complex_diff_leaf_only_in_old_is_delete():
    keep = "z" * 60
    old = {"keep": keep, "removed": "old-value"}
    new = {"keep": keep}
    changes = _expand_complex_diff("cfg", old, new, False)
    by_path = {c.path: c for c in changes}
    assert "cfg.removed" in by_path
    assert by_path["cfg.removed"].kind == "delete"


# ---------------------------------------------------------------------------
# Nested dunder keys (__defaults etc.) are filtered at every level (#11)
# ---------------------------------------------------------------------------


def test_nested_dunder_keys_filtered_on_create():
    """Bridged-provider __defaults inside nested inputs never reach CREATE diffs."""
    changes = extract_property_diffs(
        _meta(
            OpType.CREATE,
            new_inputs={"versioningConfiguration": {"__defaults": [], "status": "Enabled"}},
        )
    )
    paths = {c.path for c in changes}
    assert "versioningConfiguration.status" in paths
    assert not any("__defaults" in p for p in paths)


def test_nested_dunder_keys_filtered_on_delete():
    changes = extract_property_diffs(
        _meta(OpType.DELETE, old_inputs={"cfg": {"__defaults": ["a"], "size": 1}})
    )
    paths = {c.path for c in changes}
    assert "cfg.size" in paths
    assert not any("__defaults" in p for p in paths)


def test_deeply_nested_dunder_keys_filtered():
    """Dunder keys two levels down are filtered too, not just one level."""
    changes = extract_property_diffs(
        _meta(OpType.CREATE, new_inputs={"a": {"b": {"__meta": 1, "c": 2}}})
    )
    paths = {c.path for c in changes}
    assert paths == {"a.b.c"}


def test_nested_dunder_keys_filtered_strategy2_subkeys():
    """A changed nested __defaults produces no diff entry (meta.diffs path)."""
    changes = extract_property_diffs(
        _meta(
            OpType.UPDATE,
            diffs=["cfg"],
            old_inputs={"cfg": {"__defaults": [], "size": 1}},
            new_inputs={"cfg": {"__defaults": ["x"], "size": 2}},
        )
    )
    paths = {c.path for c in changes}
    assert "cfg.size" in paths
    assert not any("__defaults" in p for p in paths)


def test_top_level_dunder_key_in_diffs_list_skipped():
    """A __defaults entry in meta.diffs itself is skipped, matching CREATE/DELETE."""
    changes = extract_property_diffs(
        _meta(
            OpType.UPDATE,
            diffs=["__defaults", "size"],
            old_inputs={"__defaults": [], "size": 1},
            new_inputs={"__defaults": ["x"], "size": 2},
        )
    )
    paths = {c.path for c in changes}
    assert paths == {"size"}


def test_dunder_path_in_detailed_diff_skipped():
    """A detailed_diff entry touching a dunder segment is skipped (Strategy 1)."""
    changes = extract_property_diffs(
        _meta(
            OpType.UPDATE,
            detailed_diff={
                "cfg.__defaults": _detailed("update"),
                "cfg.status": _detailed("update"),
            },
            old_inputs={"cfg": {"__defaults": [], "status": "off"}},
            new_inputs={"cfg": {"__defaults": ["x"], "status": "on"}},
        )
    )
    paths = {c.path for c in changes}
    assert "cfg.status" in paths
    assert not any("__defaults" in p for p in paths)


def test_nested_dunder_not_in_compact_json_value():
    """Dunder keys are stripped from compact JSON renderings of dict values."""
    changes = extract_property_diffs(
        _meta(
            OpType.UPDATE,
            diffs=["cfg"],
            old_inputs={"cfg": "old"},
            new_inputs={"cfg": {"__defaults": [], "status": "on"}},
        )
    )
    update = [c for c in changes if c.path == "cfg"]
    assert len(update) == 1
    assert "__defaults" not in (update[0].new_value or "")
    assert "status" in (update[0].new_value or "")


def test_expand_complex_diff_strips_dunder_in_compact():
    old = {"__defaults": [], "status": "off"}
    new = {"__defaults": [], "status": "on"}
    changes = _expand_complex_diff("cfg", old, new, False)
    assert len(changes) == 1
    assert "__defaults" not in (changes[0].old_value or "")
    assert "__defaults" not in (changes[0].new_value or "")


# ---------------------------------------------------------------------------
# Cross-type == must not hide nested bool<->int / int<->float changes (#33)
# ---------------------------------------------------------------------------


def test_bool_to_int_nested_change_not_hidden():
    """True -> 1 one level down registers as a change (meta.diffs path)."""
    changes = extract_property_diffs(
        _meta(
            OpType.UPDATE,
            diffs=["tags"],
            old_inputs={"tags": {"count": True}},
            new_inputs={"tags": {"count": 1}},
        )
    )
    assert len(changes) == 1
    assert changes[0].path == "tags.count"
    assert changes[0].kind == "update"
    assert changes[0].old_value == "true"
    assert changes[0].new_value == "1"


def test_int_to_float_nested_change_not_hidden():
    changes = extract_property_diffs(
        _meta(
            OpType.UPDATE,
            diffs=["tags"],
            old_inputs={"tags": {"count": 1}},
            new_inputs={"tags": {"count": 1.0}},
        )
    )
    assert len(changes) == 1
    assert changes[0].old_value == "1"
    assert changes[0].new_value == "1.0"


def test_bool_to_int_two_levels_down_not_hidden():
    """Cross-type equality inside containers ({"n": True} == {"n": 1}) is caught."""
    changes = extract_property_diffs(
        _meta(
            OpType.UPDATE,
            diffs=["tags"],
            old_inputs={"tags": {"meta": {"count": True}}},
            new_inputs={"tags": {"meta": {"count": 1}}},
        )
    )
    assert len(changes) == 1
    assert changes[0].path == "tags.meta"
    assert changes[0].old_value != changes[0].new_value


def test_unchanged_nested_subkey_still_skipped():
    """Genuinely equal sub-keys (same type, same value) stay skipped."""
    changes = extract_property_diffs(
        _meta(
            OpType.UPDATE,
            diffs=["tags"],
            old_inputs={"tags": {"count": 1, "name": "a"}},
            new_inputs={"tags": {"count": 1, "name": "b"}},
        )
    )
    paths = {c.path for c in changes}
    assert paths == {"tags.name"}


def test_equal_nested_container_subkey_skipped():
    """Equal nested containers do not produce noise entries."""
    changes = extract_property_diffs(
        _meta(
            OpType.UPDATE,
            diffs=["cfg"],
            old_inputs={"cfg": {"opts": {"a": 1}, "size": 1}},
            new_inputs={"cfg": {"opts": {"a": 1}, "size": 2}},
        )
    )
    paths = {c.path for c in changes}
    assert paths == {"cfg.size"}
