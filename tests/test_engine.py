"""Tests for tlumi.engine and tlumi.diffs: URN parsing, event handling, and property diffs."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pulumi.automation import CommandError, OpType

from tlumi.diffs import (
    PropertyChange,
    _expand_complex_diff,
    _expand_value,
    _flatten_value,
    _format_diff_value,
    _format_json,
    _format_leaf,
    _get_nested,
    extract_property_diffs,
)
from tlumi.engine import _OP_DISPLAY, EventHandler, catch_engine_errors
from tlumi.errors import Diagnostic, EngineError
from tlumi.sanitize import (
    _PULUMI_SECRET_SIG,
    _PULUMI_SECRET_VALUE,
    _PULUMI_UNKNOWN,
    _sanitize_value,
)

# ---------------------------------------------------------------------------
# _OP_DISPLAY completeness
# ---------------------------------------------------------------------------


def test_op_display_covers_all_standard_ops():
    """Verify _OP_DISPLAY has entries for the main operation types."""
    expected = {
        OpType.CREATE,
        OpType.UPDATE,
        OpType.DELETE,
        OpType.REPLACE,
        OpType.CREATE_REPLACEMENT,
        OpType.DELETE_REPLACED,
        OpType.REFRESH,
        OpType.IMPORT,
        OpType.IMPORT_REPLACEMENT,
    }
    assert expected == set(_OP_DISPLAY.keys())


def test_op_display_tuple_structure():
    """Each _OP_DISPLAY entry should be a 4-tuple of strings."""
    for op, display in _OP_DISPLAY.items():
        assert len(display) == 4, f"{op} entry has wrong length"
        for item in display:
            assert isinstance(item, str), f"{op} entry contains non-string: {item!r}"


# ---------------------------------------------------------------------------
# _format_diff_value
# ---------------------------------------------------------------------------


def test_format_diff_value_string():
    assert _format_diff_value("hello") == '"hello"'


def test_format_diff_value_bool_true():
    assert _format_diff_value(True) == "true"


def test_format_diff_value_bool_false():
    assert _format_diff_value(False) == "false"


def test_format_diff_value_none():
    assert _format_diff_value(None) == "null"


def test_format_diff_value_number():
    assert _format_diff_value(42) == "42"


def test_format_diff_value_float():
    assert _format_diff_value(3.14) == "3.14"


def test_format_diff_value_long_string_truncated():
    long_str = "a" * 100
    result = _format_diff_value(long_str)
    assert len(result) <= 60
    assert result.endswith('..."')


def test_format_diff_value_secret():
    """[secret] ciphertext string now renders as (sensitive)."""
    assert _format_diff_value("[secret]") == "(sensitive)"


def test_format_diff_value_short_string_not_truncated():
    result = _format_diff_value("short")
    assert result == '"short"'


def test_format_diff_value_unknown_sentinel():
    """Pulumi's unknown value sentinel renders as (known after apply)."""
    assert _format_diff_value("04da6b54-80e4-46f7-96ec-b56ff0331ba9") == "(known after apply)"


def test_format_diff_value_unknown_in_string():
    """String containing the unknown sentinel also renders as (known after apply)."""
    val = "/subscriptions/04da6b54-80e4-46f7-96ec-b56ff0331ba9/resourceGroups/rg"
    assert _format_diff_value(val) == "(known after apply)"


def test_format_diff_value_unknown_in_dict():
    """Dict containing unknown sentinel preserves structure, masks only sentinel field."""
    val = {"address": "04da6b54-80e4-46f7-96ec-b56ff0331ba9", "username": "admin"}
    result = _format_diff_value(val)
    assert "(known after apply)" in result
    assert '"admin"' in result


def test_format_diff_value_unknown_in_list():
    """List containing unknown sentinel preserves structure, masks only sentinel element."""
    val = ["04da6b54-80e4-46f7-96ec-b56ff0331ba9", "other"]
    result = _format_diff_value(val)
    assert "(known after apply)" in result
    assert '"other"' in result


def test_format_diff_value_dict():
    result = _format_diff_value({"key": "val"})
    assert result == '{"key": "val"}'


def test_format_diff_value_list():
    result = _format_diff_value(["a", "b"])
    assert result == '["a", "b"]'


def test_format_diff_value_long_dict_flattened():
    """Long dicts flatten to dotted paths instead of truncating."""
    result = _format_diff_value({"key": "a" * 100})
    assert result.startswith("key")
    # Long leaf string is truncated at 60 chars
    assert '..."' in result


def test_format_diff_value_long_nested_dict_flattened():
    """Nested dicts flatten to dotted paths with full structure."""
    result = _format_diff_value(
        {
            "custom": {"metadata": {"value": "70"}, "type": "cpu"},
            "name": "cpu-rule",
        }
    )
    assert "\n" in result
    assert "custom.metadata.value" in result
    assert "custom.type" in result
    assert "name" in result
    assert '"70"' in result
    assert '"cpu"' in result
    assert '"cpu-rule"' in result


def test_format_diff_value_nested_dict():
    result = _format_diff_value({"a": {"b": 1}})
    assert result == '{"a": {"b": 1}}'


def test_format_diff_value_secret_dict():
    """Direct Pulumi secret-wrapped dict renders as (sensitive)."""
    val = {_PULUMI_SECRET_SIG: _PULUMI_SECRET_VALUE, "value": "my-password-123"}
    assert _format_diff_value(val) == "(sensitive)"


def test_format_diff_value_nested_secret():
    """Dict containing a nested secret preserves structure, masks only secret field."""
    val = {
        "address": "acr.azurecr.io",
        "password": {_PULUMI_SECRET_SIG: _PULUMI_SECRET_VALUE, "value": "secret123"},
        "username": "admin",
    }
    result = _format_diff_value(val)
    assert "(sensitive)" in result
    assert '"acr.azurecr.io"' in result
    assert '"admin"' in result
    assert "secret123" not in result


def test_format_diff_value_secret_in_list():
    """List containing a secret-wrapped element preserves structure, masks only secret."""
    val = [
        {_PULUMI_SECRET_SIG: _PULUMI_SECRET_VALUE, "value": "key1"},
        "normal",
    ]
    result = _format_diff_value(val)
    assert "(sensitive)" in result
    assert '"normal"' in result
    assert "key1" not in result


def test_format_diff_value_no_secret_normal_dict():
    """Dict without secret sentinel is NOT masked."""
    val = {"address": "acr.azurecr.io", "username": "admin"}
    result = _format_diff_value(val)
    assert result != "(sensitive)"
    assert "acr.azurecr.io" in result


# ---------------------------------------------------------------------------
# _get_nested
# ---------------------------------------------------------------------------


def test_get_nested_simple_key():
    assert _get_nested({"a": 1}, "a") == (True, 1)


def test_get_nested_dotted_path():
    assert _get_nested({"a": {"b": {"c": 3}}}, "a.b.c") == (True, 3)


def test_get_nested_bracket_notation():
    assert _get_nested({"items": [10, 20, 30]}, "items[1]") == (True, 20)


def test_get_nested_missing_key():
    assert _get_nested({"a": 1}, "b") == (False, None)


def test_get_nested_missing_nested():
    assert _get_nested({"a": {"b": 1}}, "a.c") == (False, None)


def test_get_nested_non_dict_traversal():
    assert _get_nested({"a": 42}, "a.b") == (False, None)


def test_get_nested_empty_data():
    assert _get_nested({}, "a") == (False, None)


def test_get_nested_mixed_path():
    data = {"tags": {"env": "prod"}}
    assert _get_nested(data, "tags.env") == (True, "prod")


def test_get_nested_quoted_map_key():
    """Pulumi detailed_diff emits tags["key"] for map-key changes; resolve it."""
    data = {"tags": {"env": "prod"}}
    assert _get_nested(data, 'tags["env"]') == (True, "prod")


def test_get_nested_quoted_map_key_with_dot_inside():
    """Quoted keys may themselves contain dots ('k8s.io/role'); the dot is part of the key."""
    data = {"labels": {"k8s.io/role": "worker"}}
    assert _get_nested(data, 'labels["k8s.io/role"]') == (True, "worker")


def test_get_nested_chained_quoted_keys():
    """Nested map-key access: nested["foo"]["bar"]."""
    data = {"nested": {"foo": {"bar": 7}}}
    assert _get_nested(data, 'nested["foo"]["bar"]') == (True, 7)


def test_get_nested_quoted_key_missing():
    """Quoted-key lookup returns (False, None) when the key is absent."""
    data = {"tags": {"a": 1}}
    assert _get_nested(data, 'tags["missing"]') == (False, None)


def test_get_nested_list_index_after_quoted_key():
    """List index can follow a quoted-key access."""
    data = {"tags": {"items": [1, 2, 3]}}
    assert _get_nested(data, 'tags["items"][2]') == (True, 3)


# ---------------------------------------------------------------------------
# extract_property_diffs
# ---------------------------------------------------------------------------


def _make_meta(op, diffs=None, keys=None, old_inputs=None, new_inputs=None, detailed_diff=None):
    """Build a mock StepEventMetadata."""
    meta = MagicMock()
    meta.op = op
    meta.diffs = diffs
    meta.keys = keys
    meta.detailed_diff = detailed_diff

    if old_inputs is not None:
        meta.old = MagicMock()
        meta.old.inputs = old_inputs
    else:
        meta.old = None

    if new_inputs is not None:
        meta.new = MagicMock()
        meta.new.inputs = new_inputs
    else:
        meta.new = None

    return meta


def test_extract_property_diffs_create_shows_inputs():
    """CREATE ops show all new inputs as add entries."""
    meta = _make_meta(OpType.CREATE, new_inputs={"name": "foo", "location": "uksouth"})
    changes = extract_property_diffs(meta)
    assert len(changes) == 2
    assert all(c.kind == "add" for c in changes)
    # Sorted by key
    assert changes[0].path == "location"
    assert changes[0].new_value == '"uksouth"'
    assert changes[1].path == "name"
    assert changes[1].new_value == '"foo"'


def test_extract_property_diffs_create_empty_inputs():
    """CREATE with no inputs returns empty."""
    meta = _make_meta(OpType.CREATE, new_inputs={})
    assert extract_property_diffs(meta) == []


def test_extract_property_diffs_create_no_new():
    """CREATE with meta.new=None returns empty."""
    meta = _make_meta(OpType.CREATE)
    assert extract_property_diffs(meta) == []


def test_extract_property_diffs_create_nested_dict():
    """CREATE with nested dict expands to per-leaf entries."""
    meta = _make_meta(OpType.CREATE, new_inputs={"tags": {"env": "dev", "team": "platform"}})
    changes = extract_property_diffs(meta)
    paths = [c.path for c in changes]
    assert "tags.env" in paths
    assert "tags.team" in paths
    assert all(c.kind == "add" for c in changes)


def test_extract_property_diffs_create_with_secret():
    """CREATE with secret value shows (sensitive)."""
    secret_val = {
        "4dabf18193072939515e22adb298388d": "1b47061264138c4ac30d75fd1eb44270",
        "value": "[secret]",
    }
    meta = _make_meta(OpType.CREATE, new_inputs={"password": secret_val})
    changes = extract_property_diffs(meta)
    assert len(changes) == 1
    assert changes[0].new_value == "(sensitive)"


def test_extract_property_diffs_create_with_unknown():
    """CREATE with unknown sentinel shows (known after apply)."""
    meta = _make_meta(OpType.CREATE, new_inputs={"id": "04da6b54-80e4-46f7-96ec-b56ff0331ba9"})
    changes = extract_property_diffs(meta)
    assert len(changes) == 1
    assert changes[0].new_value == "(known after apply)"


def test_extract_property_diffs_create_skips_dunder_keys():
    """CREATE ops skip Pulumi internal keys like __internal."""
    meta = _make_meta(OpType.CREATE, new_inputs={"__internal": {}, "version": "2.92.0"})
    changes = extract_property_diffs(meta)
    assert len(changes) == 1
    assert changes[0].path == "version"


def test_extract_property_diffs_delete_skips_dunder_keys():
    """DELETE ops skip Pulumi internal keys like __internal."""
    meta = _make_meta(OpType.DELETE, old_inputs={"__internal": {}, "version": "2.92.0"})
    changes = extract_property_diffs(meta)
    assert len(changes) == 1
    assert changes[0].path == "version"


def test_extract_property_diffs_delete_shows_inputs():
    """DELETE ops show all old inputs as delete entries."""
    meta = _make_meta(OpType.DELETE, old_inputs={"name": "foo", "location": "uksouth"})
    changes = extract_property_diffs(meta)
    assert len(changes) == 2
    assert all(c.kind == "delete" for c in changes)
    # Sorted by key
    assert changes[0].path == "location"
    assert changes[0].old_value == '"uksouth"'
    assert changes[1].path == "name"
    assert changes[1].old_value == '"foo"'


def test_extract_property_diffs_delete_empty_inputs():
    """DELETE with no inputs returns empty."""
    meta = _make_meta(OpType.DELETE, old_inputs={})
    assert extract_property_diffs(meta) == []


def test_extract_property_diffs_delete_no_old():
    """DELETE with meta.old=None returns empty."""
    meta = _make_meta(OpType.DELETE)
    assert extract_property_diffs(meta) == []


def test_extract_property_diffs_delete_nested_dict():
    """DELETE with nested dict expands to per-leaf entries."""
    meta = _make_meta(OpType.DELETE, old_inputs={"config": {"retries": 3, "timeout": 30}})
    changes = extract_property_diffs(meta)
    paths = [c.path for c in changes]
    assert "config.retries" in paths
    assert "config.timeout" in paths
    assert all(c.kind == "delete" for c in changes)


def test_extract_property_diffs_update_with_diffs():
    """UPDATE with diffs list produces correct PropertyChange entries."""
    meta = _make_meta(
        OpType.UPDATE,
        diffs=["location"],
        old_inputs={"location": "uksouth"},
        new_inputs={"location": "westeurope"},
    )
    changes = extract_property_diffs(meta)
    assert len(changes) == 1
    assert changes[0].path == "location"
    assert changes[0].kind == "update"
    assert changes[0].old_value == '"uksouth"'
    assert changes[0].new_value == '"westeurope"'
    assert changes[0].forces_replacement is False


def test_extract_property_diffs_replace_with_keys():
    """REPLACE with replacement keys marks forces_replacement=True."""
    meta = _make_meta(
        OpType.REPLACE,
        diffs=["location"],
        keys=["location"],
        old_inputs={"location": "uksouth"},
        new_inputs={"location": "westeurope"},
    )
    changes = extract_property_diffs(meta)
    assert len(changes) == 1
    assert changes[0].forces_replacement is True


def test_extract_property_diffs_nested_dict_subkeys():
    """Nested dict values produce sub-key diffs."""
    meta = _make_meta(
        OpType.UPDATE,
        diffs=["tags"],
        old_inputs={"tags": {"env": "dev"}},
        new_inputs={"tags": {"env": "staging", "team": "platform"}},
    )
    changes = extract_property_diffs(meta)
    paths = {c.path for c in changes}
    assert "tags.env" in paths
    assert "tags.team" in paths

    env_change = next(c for c in changes if c.path == "tags.env")
    assert env_change.kind == "update"
    assert env_change.old_value == '"dev"'
    assert env_change.new_value == '"staging"'

    team_change = next(c for c in changes if c.path == "tags.team")
    assert team_change.kind == "add"
    assert team_change.new_value == '"platform"'


def test_extract_property_diffs_add_property():
    """New property in UPDATE shows as add."""
    meta = _make_meta(
        OpType.UPDATE,
        diffs=["description"],
        old_inputs={},
        new_inputs={"description": "new desc"},
    )
    changes = extract_property_diffs(meta)
    assert len(changes) == 1
    assert changes[0].kind == "add"
    assert changes[0].new_value == '"new desc"'


def test_extract_property_diffs_delete_property():
    """Removed property in UPDATE shows as delete."""
    meta = _make_meta(
        OpType.UPDATE,
        diffs=["description"],
        old_inputs={"description": "old desc"},
        new_inputs={},
    )
    changes = extract_property_diffs(meta)
    assert len(changes) == 1
    assert changes[0].kind == "delete"
    assert changes[0].old_value == '"old desc"'


def test_extract_property_diffs_no_diffs_list():
    """UPDATE with no diffs list returns empty."""
    meta = _make_meta(OpType.UPDATE, diffs=None, old_inputs={"a": 1}, new_inputs={"a": 2})
    assert extract_property_diffs(meta) == []


def test_extract_property_diffs_create_replacement_skipped():
    """CREATE_REPLACEMENT ops return empty (deduped into REPLACE in on_preview)."""
    meta = _make_meta(
        OpType.CREATE_REPLACEMENT,
        diffs=["size"],
        old_inputs={"size": 10},
        new_inputs={"size": 20},
    )
    assert extract_property_diffs(meta) == []


def test_extract_property_diffs_detailed_diff():
    """When detailed_diff is available, use it instead of inputs comparison."""
    diff_kind = MagicMock()
    diff_kind.value = "update"
    pdiff = MagicMock()
    pdiff.diff_kind = diff_kind

    meta = _make_meta(
        OpType.UPDATE,
        diffs=["location"],
        old_inputs={"location": "uksouth"},
        new_inputs={"location": "westeurope"},
    )
    meta.detailed_diff = {"location": pdiff}

    changes = extract_property_diffs(meta)
    assert len(changes) == 1
    assert changes[0].path == "location"
    assert changes[0].kind == "update"


def test_extract_property_diffs_detailed_diff_replacement():
    """detailed_diff with 'replace' in kind marks forces_replacement."""
    diff_kind = MagicMock()
    diff_kind.value = "update-replace"
    pdiff = MagicMock()
    pdiff.diff_kind = diff_kind

    meta = _make_meta(
        OpType.REPLACE,
        old_inputs={"location": "uksouth"},
        new_inputs={"location": "westeurope"},
    )
    meta.detailed_diff = {"location": pdiff}

    changes = extract_property_diffs(meta)
    assert len(changes) == 1
    assert changes[0].forces_replacement is True


def test_extract_property_diffs_dict_subkey_delete():
    """Nested dict sub-key removal shows as delete."""
    meta = _make_meta(
        OpType.UPDATE,
        diffs=["tags"],
        old_inputs={"tags": {"env": "dev", "team": "old"}},
        new_inputs={"tags": {"env": "dev"}},
    )
    changes = extract_property_diffs(meta)
    assert len(changes) == 1
    assert changes[0].path == "tags.team"
    assert changes[0].kind == "delete"


# ---------------------------------------------------------------------------
# F8/F9: Strategy-2 (inputs comparison) edge cases
# ---------------------------------------------------------------------------


def test_extract_property_diffs_strategy2_inputs_attr_is_none():
    """Strategy 2 must not raise when meta.old.inputs exists but is None (F8).

    StepEventStateMetadata.from_json sets inputs=data.get("inputs"), so the
    attribute is present and None (not {}) when the engine omits it. Before the
    fix `key in None` raised TypeError, was swallowed by the on_preview wrapper,
    and the property diff silently vanished.
    """
    meta = MagicMock()
    meta.op = OpType.UPDATE
    meta.diffs = ["name"]
    meta.keys = None
    meta.detailed_diff = None
    meta.old = MagicMock()
    meta.old.inputs = None  # attribute present but None
    meta.new = MagicMock()
    meta.new.inputs = {"name": "new-value"}

    changes = extract_property_diffs(meta)  # must not raise
    assert any(c.path == "name" and c.kind == "add" for c in changes)


def test_extract_property_diffs_strategy2_ghost_key_skipped():
    """A key in meta.diffs absent from BOTH inputs must not emit a phantom add (F9)."""
    meta = _make_meta(
        OpType.UPDATE,
        diffs=["ghost", "name"],
        old_inputs={"name": "old"},
        new_inputs={"name": "new"},
    )
    changes = extract_property_diffs(meta)
    paths = [c.path for c in changes]
    assert "ghost" not in paths
    assert "name" in paths


def test_extract_property_diffs_detailed_diff_update_path_absent_both():
    """detailed_diff update at a path absent from both inputs is skipped, not raised (F27)."""
    diff_kind = MagicMock()
    diff_kind.value = "update"
    pdiff = MagicMock()
    pdiff.diff_kind = diff_kind
    meta = _make_meta(OpType.UPDATE, old_inputs={"other": 1}, new_inputs={"other": 1})
    meta.detailed_diff = {"ghost": pdiff}
    # Would build PropertyChange.update(None, None) -> ValueError without the guard.
    assert extract_property_diffs(meta) == []


def test_extract_property_diffs_detailed_diff_update_path_in_new_only():
    """detailed_diff update at a path present only on the new side -> old_value=None (F27)."""
    diff_kind = MagicMock()
    diff_kind.value = "update"
    pdiff = MagicMock()
    pdiff.diff_kind = diff_kind
    meta = _make_meta(OpType.UPDATE, old_inputs={}, new_inputs={"added": "v"})
    meta.detailed_diff = {"added": pdiff}
    changes = extract_property_diffs(meta)
    assert len(changes) == 1
    assert changes[0].path == "added"
    assert changes[0].kind == "update"
    assert changes[0].old_value is None
    assert changes[0].new_value == '"v"'


def test_flatten_keeps_empty_nested_dict_and_list():
    """Empty nested containers stay visible in flattened output instead of vanishing (F10)."""
    from tlumi.diffs import _flatten_value

    pairs = dict(_flatten_value({"name": "x", "tags": {}, "items": []}))
    assert pairs["name"] == '"x"'
    assert pairs["tags"] == "{}"
    assert pairs["items"] == "[]"


def test_flatten_keeps_empty_dict_element_in_list():
    """An empty dict element keeps its list index visible (no misleading gap) (F10)."""
    from tlumi.diffs import _flatten_value

    pairs = dict(_flatten_value([{"name": "a"}, {}, {"name": "c"}]))
    assert pairs["[0].name"] == '"a"'
    assert pairs["[1]"] == "{}"
    assert pairs["[2].name"] == '"c"'


def test_animated_log_only_animates_marker(monkeypatch):
    """Literal '...' in a provider diagnostic is not clobbered by the spinner (F19)."""
    import time as _time

    from rich.console import Console

    from tlumi.engine import _AnimatedLog

    monkeypatch.setattr(_time, "monotonic", lambda: 0.0)  # phase 0 -> dots "   "
    log = _AnimatedLog([("creating...", True), ("Waiting for operation...", False)])
    console = Console(width=200)
    with console.capture() as cap:
        console.print(log)
    text = cap.get()
    assert "Waiting for operation..." in text  # diagnostic '...' preserved verbatim
    assert "creating..." not in text  # marker '...' replaced by the spinner


# ---------------------------------------------------------------------------
# _sanitize_value
# ---------------------------------------------------------------------------


def test_sanitize_value_direct_secret():
    """Direct secret wrapper dict is replaced with marker."""
    val = {_PULUMI_SECRET_SIG: _PULUMI_SECRET_VALUE, "value": "password123"}
    assert _sanitize_value(val) == "(sensitive)"


def test_sanitize_value_nested_secret_in_dict():
    """Secret nested in a dict is replaced, surrounding structure preserved."""
    val = {
        "address": "acr.azurecr.io",
        "password": {_PULUMI_SECRET_SIG: _PULUMI_SECRET_VALUE, "value": "secret"},
        "username": "admin",
    }
    result = _sanitize_value(val)
    assert result["address"] == "acr.azurecr.io"
    assert result["password"] == "(sensitive)"
    assert result["username"] == "admin"


def test_sanitize_value_nested_unknown_in_dict():
    """Unknown sentinel in a dict value is replaced, rest preserved."""
    val = {"address": _PULUMI_UNKNOWN, "username": "admin"}
    result = _sanitize_value(val)
    assert result["address"] == "(known after apply)"
    assert result["username"] == "admin"


def test_sanitize_value_unknown_in_string():
    """String containing unknown sentinel is replaced."""
    val = f"/subscriptions/{_PULUMI_UNKNOWN}/resourceGroups/rg"
    assert _sanitize_value(val) == "(known after apply)"


def test_sanitize_value_mixed_list():
    """List with secret and unknown elements sanitized per-element."""
    val = [
        {_PULUMI_SECRET_SIG: _PULUMI_SECRET_VALUE, "value": "x"},
        _PULUMI_UNKNOWN,
        "normal",
    ]
    result = _sanitize_value(val)
    assert result[0] == "(sensitive)"
    assert result[1] == "(known after apply)"
    assert result[2] == "normal"


def test_sanitize_value_deeply_nested():
    """Deeply nested secret is replaced at the right level."""
    val = {"registries": [{"password": {_PULUMI_SECRET_SIG: _PULUMI_SECRET_VALUE, "value": "x"}}]}
    result = _sanitize_value(val)
    assert result["registries"][0]["password"] == "(sensitive)"


def test_sanitize_value_secret_string():
    """[secret] ciphertext string is replaced."""
    assert _sanitize_value("[secret]") == "(sensitive)"


def test_sanitize_value_passthrough_scalars():
    """Scalars without sentinels pass through unchanged."""
    assert _sanitize_value("hello") == "hello"
    assert _sanitize_value(42) == 42
    assert _sanitize_value(True) is True
    assert _sanitize_value(None) is None


def test_sanitize_value_string_with_secret_sig():
    """String containing secret sig key is replaced."""
    val = f'{{"key": "{_PULUMI_SECRET_SIG}"}}'
    assert _sanitize_value(val) == "(sensitive)"


# ---------------------------------------------------------------------------
# _format_leaf / _flatten_value
# ---------------------------------------------------------------------------


def test_format_leaf_string():
    assert _format_leaf("hello") == '"hello"'


def test_format_leaf_marker_passthrough():
    assert _format_leaf("(sensitive)") == "(sensitive)"
    assert _format_leaf("(known after apply)") == "(known after apply)"


def test_format_leaf_bool():
    assert _format_leaf(True) == "true"
    assert _format_leaf(False) == "false"


def test_format_leaf_none():
    assert _format_leaf(None) == "null"


def test_format_leaf_number():
    assert _format_leaf(42) == "42"
    assert _format_leaf(3.14) == "3.14"


def test_format_leaf_long_string_truncated():
    result = _format_leaf("a" * 100)
    assert len(result) <= 60
    assert result.endswith('..."')


def test_flatten_simple_dict():
    pairs = _flatten_value({"name": "cpu-rule", "type": "cpu"})
    assert ("name", '"cpu-rule"') in pairs
    assert ("type", '"cpu"') in pairs


def test_flatten_nested_dict():
    pairs = _flatten_value({"custom": {"metadata": {"value": "70"}, "type": "cpu"}})
    assert ("custom.metadata.value", '"70"') in pairs
    assert ("custom.type", '"cpu"') in pairs


def test_flatten_list():
    pairs = _flatten_value(["a", "b", "c"])
    assert ("[0]", '"a"') in pairs
    assert ("[1]", '"b"') in pairs
    assert ("[2]", '"c"') in pairs


def test_flatten_list_of_dicts():
    val = [{"name": "APP_ENV", "value": "staging"}, {"name": "DB_HOST", "value": "localhost"}]
    pairs = _flatten_value(val)
    assert ("[0].name", '"APP_ENV"') in pairs
    assert ("[0].value", '"staging"') in pairs
    assert ("[1].name", '"DB_HOST"') in pairs
    assert ("[1].value", '"localhost"') in pairs


def test_flatten_with_markers():
    val = {"address": "acr.io", "password": "(sensitive)"}
    pairs = _flatten_value(val)
    assert ("address", '"acr.io"') in pairs
    assert ("password", "(sensitive)") in pairs


def test_flatten_empty_dict():
    assert _flatten_value({}) == []


def test_flatten_empty_list():
    assert _flatten_value([]) == []


def test_flatten_scalar():
    assert _flatten_value("hello") == [("", '"hello"')]


# ---------------------------------------------------------------------------
# _format_json
# ---------------------------------------------------------------------------


def test_format_json_compact():
    """Short values stay on a single line."""
    result = _format_json({"key": "val"})
    assert result == '{"key": "val"}'
    assert "\n" not in result


def test_format_json_expanded():
    """Long values expand to multi-line JSON."""
    val = {"key": "a" * 100}
    result = _format_json(val)
    assert "\n" in result
    assert "  " in result  # indent=2


def test_format_json_unquotes_sensitive_marker():
    """Sensitive marker is unquoted in JSON output."""
    result = _format_json({"password": "(sensitive)", "user": "admin"})
    assert "(sensitive)" in result
    assert '"(sensitive)"' not in result


def test_format_json_unquotes_unknown_marker():
    """Unknown marker is unquoted in JSON output."""
    result = _format_json({"address": "(known after apply)"})
    assert "(known after apply)" in result
    assert '"(known after apply)"' not in result


def test_format_json_list():
    """Lists are formatted correctly."""
    result = _format_json(["a", "b"])
    assert result == '["a", "b"]'


def test_format_json_expanded_list():
    """Long lists expand to multi-line."""
    val = [
        {"name": "APP_ENV", "value": "staging"},
        {"name": "STORAGE_CONNECTION", "secretRef": "storage-connection"},
    ]
    result = _format_json(val)
    assert "\n" in result
    assert "APP_ENV" in result


# ---------------------------------------------------------------------------
# _format_diff_value: expanded behavior
# ---------------------------------------------------------------------------


def test_format_diff_value_nested_secret_preserves_non_secret_fields():
    """Non-secret fields are visible when a sibling is secret, flattened to paths."""
    val = {
        "address": "acr2885f79a.azurecr.io",
        "password": {_PULUMI_SECRET_SIG: _PULUMI_SECRET_VALUE, "value": "[secret]"},
        "username": "admin",
    }
    result = _format_diff_value(val)
    assert "acr2885f79a.azurecr.io" in result
    assert "(sensitive)" in result
    assert '"admin"' in result
    # Flattened paths
    assert "address" in result
    assert "password" in result
    assert "username" in result


def test_format_diff_value_mixed_unknown_preserves_structure():
    """Dict with one unknown field preserves the other fields."""
    val = {"id": _PULUMI_UNKNOWN, "name": "my-resource"}
    result = _format_diff_value(val)
    assert "(known after apply)" in result
    assert '"my-resource"' in result


def test_format_diff_value_compact_dict_stays_inline():
    """Short dict stays on one line."""
    result = _format_diff_value({"key": "val"})
    assert "\n" not in result
    assert result == '{"key": "val"}'


def test_format_diff_value_bool_in_json():
    """Bools in JSON dicts are serialized as JSON bools."""
    result = _format_diff_value({"enabled": True})
    assert "true" in result


def test_format_diff_value_missing_sentinel():
    """_MISSING sentinel returns empty string as a defensive guard."""
    from tlumi.diffs import _MISSING

    assert _format_diff_value(_MISSING) == ""


# ---------------------------------------------------------------------------
# _expand_complex_diff
# ---------------------------------------------------------------------------


def test_expand_complex_diff_both_dicts():
    """When both sides are dicts, expand to per-leaf inline diffs."""
    old = {"address": "acr.io", "password": "secret", "username": "old-user"}
    new = {"address": "new.io", "password": "secret", "username": "new-user"}
    changes = _expand_complex_diff("registries[0]", old, new)
    paths = {c.path for c in changes}
    assert "registries[0].address" in paths
    assert "registries[0].username" in paths
    # password unchanged, should be skipped
    assert "registries[0].password" not in paths
    addr = next(c for c in changes if c.path == "registries[0].address")
    assert addr.kind == "update"
    assert '"acr.io"' in addr.old_value
    assert '"new.io"' in addr.new_value


def test_expand_complex_diff_both_lists():
    """When both sides are lists, expand to per-element diffs."""
    old = [{"name": "APP_ENV", "value": "development-environment-value-long"}]
    new = [{"name": "APP_ENV", "value": "staging-environment-value-long-new"}]
    changes = _expand_complex_diff("env", old, new)
    paths = {c.path for c in changes}
    # name unchanged, skipped
    assert "env[0].name" not in paths
    assert "env[0].value" in paths
    val = next(c for c in changes if c.path == "env[0].value")
    assert val.kind == "update"
    assert "development" in val.old_value
    assert "staging" in val.new_value


def test_expand_complex_diff_compact_stays_inline():
    """When both sides are short dicts, keep as single inline PropertyChange."""
    old = {"k": "a"}
    new = {"k": "b"}
    changes = _expand_complex_diff("x", old, new)
    assert len(changes) == 1
    assert changes[0].path == "x"
    assert changes[0].kind == "update"


def test_expand_complex_diff_one_side_scalar():
    """When one side is scalar, fall back to single PropertyChange."""
    changes = _expand_complex_diff("x", "old-string", {"k": "v"})
    assert len(changes) == 1
    assert changes[0].path == "x"
    assert changes[0].kind == "update"


def test_expand_complex_diff_unchanged_skipped():
    """Sub-fields with identical values in both old and new are omitted."""
    old = {
        "a": "same-value-unchanged",
        "b": "old-value-for-b-field",
        "c": "another-long-unchanged-value",
    }
    new = {
        "a": "same-value-unchanged",
        "b": "new-value-for-b-field",
        "c": "another-long-unchanged-value",
    }
    changes = _expand_complex_diff("prop", old, new)
    paths = {c.path for c in changes}
    assert "prop.a" not in paths
    assert "prop.c" not in paths
    assert "prop.b" in paths


def test_expand_complex_diff_forces_replacement():
    """forces_replacement propagates to all expanded PropertyChange entries."""
    old = {"address": "old.io", "username": "old-user"}
    new = {"address": "new.io", "username": "new-user"}
    changes = _expand_complex_diff("reg", old, new, forces_replacement=True)
    assert all(c.forces_replacement for c in changes)


def test_extract_property_diffs_complex_update_expanded():
    """End-to-end: complex dict update expands to per-leaf diffs in Strategy 2."""
    meta = _make_meta(
        OpType.UPDATE,
        diffs=["registries"],
        old_inputs={"registries": [{"address": "acr.io", "password": "secret", "username": "old"}]},
        new_inputs={"registries": [{"address": "new.io", "password": "secret", "username": "new"}]},
    )
    changes = extract_property_diffs(meta)
    paths = {c.path for c in changes}
    # Should have per-leaf paths, not the blob "registries"
    assert any("[0].address" in p for p in paths)
    assert any("[0].username" in p for p in paths)
    # password is same, should be skipped
    assert not any("password" in p for p in paths)


# ---------------------------------------------------------------------------
# _expand_value
# ---------------------------------------------------------------------------


def test_expand_value_scalar_add():
    """Scalar add returns single PropertyChange."""
    changes = _expand_value("name", "hello", "add")
    assert len(changes) == 1
    assert changes[0].path == "name"
    assert changes[0].kind == "add"
    assert changes[0].new_value == '"hello"'


def test_expand_value_scalar_delete():
    """Scalar delete returns single PropertyChange."""
    changes = _expand_value("name", "gone", "delete")
    assert len(changes) == 1
    assert changes[0].kind == "delete"
    assert changes[0].old_value == '"gone"'


def test_expand_value_dict_add():
    """Dict add expands to per-leaf entries."""
    val = {"latestRevision": True, "weight": 100}
    changes = _expand_value("traffic[0]", val, "add")
    paths = {c.path for c in changes}
    assert "traffic[0].latestRevision" in paths
    assert "traffic[0].weight" in paths
    assert all(c.kind == "add" for c in changes)
    rev = next(c for c in changes if "latestRevision" in c.path)
    assert rev.new_value == "true"


def test_expand_value_list_add():
    """List add expands to per-element entries."""
    val = [{"name": "APP_ENV", "value": "staging"}]
    changes = _expand_value("env", val, "add")
    paths = {c.path for c in changes}
    assert "env[0].name" in paths
    assert "env[0].value" in paths


def test_expand_value_dict_delete():
    """Dict delete expands to per-leaf entries with old_value."""
    val = {"server": "acr.io", "username": "admin"}
    changes = _expand_value("registries[0]", val, "delete")
    assert all(c.kind == "delete" for c in changes)
    assert all(c.old_value is not None for c in changes)
    assert all(c.new_value is None for c in changes)


def test_expand_value_forces_replacement():
    """forces_replacement propagates to all expanded entries."""
    val = {"a": 1, "b": 2}
    changes = _expand_value("x", val, "add", forces_replacement=True)
    assert all(c.forces_replacement for c in changes)


# ---------------------------------------------------------------------------
# EventHandler.on_preview
# ---------------------------------------------------------------------------


def _make_event(
    resource_pre=None, res_outputs=None, diagnostic=None, res_op_failed=None, timestamp=0
):
    """Build a mock EngineEvent.

    Absent event slots are None (mirroring the real SDK), so truthiness checks
    in the handler only fire for the slots a test populates.
    """
    event = MagicMock()
    event.resource_pre_event = resource_pre
    event.res_outputs_event = res_outputs
    event.diagnostic_event = diagnostic
    event.res_op_failed_event = res_op_failed
    event.timestamp = timestamp
    return event


def _make_resource_pre(urn, op, rtype="aws:s3:BucketV2", inputs=None):
    """Build a mock resource_pre_event."""
    pre = MagicMock()
    meta = MagicMock()
    meta.urn = urn
    meta.type = rtype
    meta.op = op
    meta.diffs = None
    meta.keys = None
    meta.detailed_diff = None
    meta.old = None
    meta.new = None
    if inputs is not None:
        meta.new = MagicMock()
        meta.new.inputs = inputs
    pre.metadata = meta
    return pre


def test_on_preview_skips_stack_type():
    """on_preview ignores pulumi:pulumi:Stack resources."""
    handler = EventHandler(quiet=True)
    pre = _make_resource_pre(
        "urn:pulumi:default::p::pulumi:pulumi:Stack::p-default",
        OpType.CREATE,
        rtype="pulumi:pulumi:Stack",
    )
    event = _make_event(resource_pre=pre)
    handler.on_preview(event)
    assert handler.property_diffs == {}


def test_on_preview_skips_same_op():
    """on_preview ignores SAME operations."""
    handler = EventHandler(quiet=True)
    pre = _make_resource_pre(
        "urn:pulumi:default::p::aws:s3:BucketV2::b",
        OpType.SAME,
    )
    event = _make_event(resource_pre=pre)
    handler.on_preview(event)
    assert handler.property_diffs == {}


def test_on_preview_skips_create_replacement():
    """on_preview skips CREATE_REPLACEMENT (deduped into REPLACE)."""
    handler = EventHandler(quiet=True)
    pre = _make_resource_pre(
        "urn:pulumi:default::p::aws:s3:BucketV2::b",
        OpType.CREATE_REPLACEMENT,
    )
    event = _make_event(resource_pre=pre)
    handler.on_preview(event)
    assert handler.property_diffs == {}


def test_on_preview_skips_delete_replaced():
    """on_preview skips DELETE_REPLACED (deduped into REPLACE)."""
    handler = EventHandler(quiet=True)
    pre = _make_resource_pre(
        "urn:pulumi:default::p::aws:s3:BucketV2::b",
        OpType.DELETE_REPLACED,
    )
    event = _make_event(resource_pre=pre)
    handler.on_preview(event)
    assert handler.property_diffs == {}


def test_on_preview_collects_diffs_when_quiet():
    """on_preview collects property_diffs even in quiet mode (for JSON output)."""
    handler = EventHandler(quiet=True)
    urn = "urn:pulumi:default::p::aws:s3:BucketV2::b"
    pre = _make_resource_pre(urn, OpType.CREATE, inputs={"name": "mybucket"})
    event = _make_event(resource_pre=pre)
    handler.on_preview(event)
    assert urn in handler.property_diffs
    assert len(handler.property_diffs[urn]) == 1
    assert handler.property_diffs[urn][0].path == "name"


def test_on_preview_no_diffs_when_no_inputs():
    """on_preview creates no property_diffs when there are no inputs."""
    handler = EventHandler(quiet=True)
    urn = "urn:pulumi:default::p::aws:s3:BucketV2::b"
    pre = _make_resource_pre(urn, OpType.CREATE)
    event = _make_event(resource_pre=pre)
    handler.on_preview(event)
    assert urn not in handler.property_diffs


# ---------------------------------------------------------------------------
# EventHandler.on_update
# ---------------------------------------------------------------------------


def test_on_update_collects_errors():
    """on_update collects error diagnostics."""
    handler = EventHandler(quiet=True)
    diag = MagicMock()
    diag.severity = "error"
    diag.message = "resource creation failed"
    diag.urn = ""
    event = _make_event(diagnostic=diag)
    handler.on_update(event)
    assert len(handler.errors) == 1
    assert handler.errors[0].message == "resource creation failed"


def test_on_update_skips_update_failed_message():
    """on_update ignores the generic 'update failed' error."""
    handler = EventHandler(quiet=True)
    diag = MagicMock()
    diag.severity = "error"
    diag.message = "update failed"
    diag.urn = ""
    event = _make_event(diagnostic=diag)
    handler.on_update(event)
    assert handler.errors == []


def test_on_update_skips_empty_diagnostics():
    """on_update ignores diagnostic events with empty messages."""
    handler = EventHandler(quiet=True)
    diag = MagicMock()
    diag.severity = "error"
    diag.message = "   "
    diag.urn = ""
    event = _make_event(diagnostic=diag)
    handler.on_update(event)
    assert handler.errors == []


def test_on_update_tracks_start_times():
    """on_update records resource start timestamps."""
    handler = EventHandler(quiet=True)
    urn = "urn:pulumi:default::p::aws:s3:BucketV2::b"
    pre = _make_resource_pre(urn, OpType.CREATE)
    event = _make_event(resource_pre=pre, timestamp=100)
    handler.on_update(event)
    assert handler._start_times[urn] == 100


# ---------------------------------------------------------------------------
# catch_engine_errors
# ---------------------------------------------------------------------------


def test_catch_engine_errors_converts_command_error():
    """catch_engine_errors converts CommandError to EngineError."""
    handler = EventHandler()
    handler.errors.append(Diagnostic("res", "something broke"))
    # CommandError expects a CommandResult; use a mock
    mock_result = MagicMock()
    mock_result.stdout = "output"
    mock_result.stderr = "pulumi failed"
    mock_result.code = 1
    with pytest.raises(EngineError) as exc_info:
        with catch_engine_errors(handler, "Operation failed"):
            raise CommandError(mock_result)
    assert exc_info.value.message == "Operation failed"
    assert len(exc_info.value.diagnostics) == 1


def test_catch_engine_errors_passes_non_command_error():
    """catch_engine_errors does not catch non-CommandError exceptions."""
    handler = EventHandler()
    with pytest.raises(TypeError, match="bad type"):
        with catch_engine_errors(handler, "Op failed"):
            raise TypeError("bad type")


def test_catch_engine_errors_no_error():
    """catch_engine_errors is a no-op when no exception occurs."""
    handler = EventHandler()
    with catch_engine_errors(handler, "Op failed"):
        pass  # no exception
    assert handler.errors == []


# ---------------------------------------------------------------------------
# Credential redaction at the three engine sinks (F3 regression guards)
#
# These assert behavior, not just presence: removing any redact_text() call
# in engine.py makes one of these fail. Without them the suite passed even
# with the guards deleted.
# ---------------------------------------------------------------------------


def test_on_update_redacts_credentials_in_error_diagnostic():
    """Error diagnostics are scrubbed before being stored in handler.errors."""
    handler = EventHandler(quiet=True)
    diag = MagicMock()
    diag.severity = "error"
    diag.message = "deploy failed: AKIAIOSFODNN7EXAMPLE secret_access_key=wJalrXUtnFEMI/K7MDENG"
    diag.urn = ""
    handler.on_update(_make_event(diagnostic=diag))
    assert len(handler.errors) == 1
    stored = handler.errors[0].message
    assert "AKIAIOSFODNN7EXAMPLE" not in stored
    assert "wJalrXUtnFEMI/K7MDENG" not in stored
    assert "***" in stored


def test_on_update_redacts_credentials_in_info_log():
    """Info-severity diagnostic lines are scrubbed before entering the log window."""
    handler = EventHandler(quiet=True)
    urn = "urn:pulumi:default::p::aws:s3:BucketV2::b"
    # Make the resource active so the info line is attributed and stored.
    handler.on_update(_make_event(resource_pre=_make_resource_pre(urn, OpType.CREATE)))
    diag = MagicMock()
    diag.severity = "info"
    diag.message = "still provisioning, token=eyJsecretvalue123 in use"
    diag.urn = urn
    handler.on_update(_make_event(diagnostic=diag))
    stored = "\n".join(handler._resource_logs.get(urn, []))
    assert "eyJsecretvalue123" not in stored
    assert "***" in stored


def test_catch_engine_errors_redacts_credentials_in_full_output():
    """catch_engine_errors scrubs credentials from the captured Pulumi stderr."""
    from pulumi.automation._cmd import CommandResult

    handler = EventHandler()
    result = CommandResult(
        stdout="",
        stderr=(
            "Azure deploy failed: AccountKey=AbCdEf1234567890==; SecretAccessKey: wJalrXUtnEXAMPLE"
        ),
        code=255,
    )
    with pytest.raises(EngineError) as exc_info:
        with catch_engine_errors(handler, "Apply failed."):
            raise CommandError(result)
    full = exc_info.value.full_output
    assert "AbCdEf1234567890==" not in full
    assert "wJalrXUtnEXAMPLE" not in full
    assert "***" in full


# ---------------------------------------------------------------------------
# start_live
# ---------------------------------------------------------------------------


def test_start_live_quiet_is_noop_context_and_closes_session():
    """start_live(quiet=True) yields a usable no-op context that closes the session."""
    handler = EventHandler()
    with handler.start_live(quiet=True):
        assert handler._closed is False
    # On exit the engine session is closed so late callbacks are dropped.
    assert handler._closed is True
    assert handler._live is None


def test_late_events_dropped_after_session_close():
    """Events arriving after the live session exits are ignored (no ghost output)."""
    handler = EventHandler(quiet=True)
    with handler.start_live(quiet=True):
        pass
    # A late diagnostic error must not be collected once the session is closed.
    diag = MagicMock()
    diag.severity = "error"
    diag.message = "late boom"
    diag.urn = ""
    handler.on_update(_make_event(diagnostic=diag))
    assert handler.errors == []


# ---------------------------------------------------------------------------
# EventHandler.callback_errors counter (E1)
# ---------------------------------------------------------------------------


def test_callback_errors_starts_at_zero():
    """New EventHandler has zero callback_errors."""
    handler = EventHandler()
    assert handler.callback_errors == 0


def test_callback_errors_increments_on_preview_error():
    """on_preview increments callback_errors when callback raises."""
    handler = EventHandler()
    event = MagicMock()
    event.resource_pre_event = MagicMock()
    # Make the inner method raise
    with patch.object(handler, "_on_preview", side_effect=RuntimeError("boom")):
        handler.on_preview(event)
    assert handler.callback_errors == 1


def test_callback_errors_increments_on_update_error():
    """on_update increments callback_errors when callback raises."""
    handler = EventHandler()
    event = MagicMock()
    event.resource_pre_event = MagicMock()
    with patch.object(handler, "_on_update", side_effect=RuntimeError("boom")):
        handler.on_update(event)
    assert handler.callback_errors == 1


# ---------------------------------------------------------------------------
# PropertyChange __post_init__ validation (TD1)
# ---------------------------------------------------------------------------


def test_property_change_add_with_old_value_raises():
    """PropertyChange 'add' with old_value raises ValueError."""
    with pytest.raises(ValueError, match="'add' changes must not have old_value"):
        PropertyChange(path="name", kind="add", old_value="bad", new_value="val")


def test_property_change_delete_with_new_value_raises():
    """PropertyChange 'delete' with new_value raises ValueError."""
    with pytest.raises(ValueError, match="'delete' changes must not have new_value"):
        PropertyChange(path="name", kind="delete", old_value="val", new_value="bad")


def test_property_change_factory_methods_pass_validation():
    """Factory classmethods produce valid PropertyChange objects."""
    add = PropertyChange.add("name", "val")
    assert add.kind == "add" and add.new_value == "val" and add.old_value is None

    delete = PropertyChange.delete("name", "val")
    assert delete.kind == "delete" and delete.old_value == "val" and delete.new_value is None

    update = PropertyChange.update("name", "old", "new")
    assert update.kind == "update" and update.old_value == "old" and update.new_value == "new"


# ---------------------------------------------------------------------------
# on_update completion cleans active resources
# ---------------------------------------------------------------------------


def test_on_update_completion_cleans_active_resources():
    """res_outputs_event removes the resource from active tracking."""
    handler = EventHandler(quiet=True)
    urn = "urn:pulumi:default::p::aws:s3:BucketV2::b"

    # Simulate pre_event (resource starts)
    pre = _make_resource_pre(urn, OpType.CREATE)
    event_start = _make_event(resource_pre=pre, timestamp=100)
    handler.on_update(event_start)
    assert urn in handler._active_resources

    # Simulate res_outputs_event (resource completes)
    outputs = MagicMock()
    outputs.metadata = MagicMock()
    outputs.metadata.urn = urn
    outputs.metadata.type = "aws:s3:BucketV2"
    outputs.metadata.op = OpType.CREATE
    event_done = _make_event(res_outputs=outputs, timestamp=105)
    handler.on_update(event_done)
    assert urn not in handler._active_resources
    assert urn not in handler._active_display


def test_sdk_deserializes_detailed_diff():
    """SDK natively deserializes detailedDiff into PropertyDiff objects (fixed in 3.224.0)."""
    from pulumi.automation.events import DiffKind, PropertyDiff, StepEventMetadata

    test_data = {
        "op": "same",
        "urn": "urn:pulumi:test::test::test::test",
        "type": "test",
        "provider": "",
        "old": None,
        "new": None,
        "detailedDiff": {"field": {"diffKind": "update", "inputDiff": False}},
    }
    meta = StepEventMetadata.from_json(test_data)
    assert meta.detailed_diff is not None
    assert len(meta.detailed_diff) == 1
    value = meta.detailed_diff["field"]
    assert isinstance(value, PropertyDiff)
    assert value.diff_kind == DiffKind.UPDATE
    assert value.input_diff is False


def test_sdk_empty_dict_without_detailed_diff():
    """SDK returns empty dict when detailedDiff key is absent."""
    from pulumi.automation.events import StepEventMetadata

    test_data = {
        "op": "same",
        "urn": "urn:pulumi:test::test::test::test",
        "type": "test",
        "provider": "",
        "old": None,
        "new": None,
    }
    meta = StepEventMetadata.from_json(test_data)
    # SDK returns {} (falsy) when key is absent
    assert meta.detailed_diff == {}


# ---------------------------------------------------------------------------
# Source position suppression (sdk_compat.py)
# ---------------------------------------------------------------------------


def test_source_position_patch_suppresses_stack_trace():
    """_get_stack_trace is replaced by the sdk_compat patch."""
    import inspect

    from pulumi.runtime import resource
    from pulumi.runtime.proto import source_pb2

    # Verify the function is the patched lambda (not the original SDK function)
    source_file = inspect.getfile(resource._get_stack_trace)
    assert "sdk_compat" in source_file

    result = resource._get_stack_trace()
    assert isinstance(result, source_pb2.StackTrace)
    assert len(result.frames) == 0
    # Empty StackTrace serializes to zero bytes (no paths on wire)
    assert result.SerializeToString() == b""


def test_source_position_patch_suppresses_source_position():
    """_get_source_position is replaced by the sdk_compat patch."""
    import inspect

    from pulumi.runtime import resource
    from pulumi.runtime.proto import source_pb2

    # Verify the function is the patched lambda
    source_file = inspect.getfile(resource._get_source_position)
    assert "sdk_compat" in source_file

    # Must return None even for a non-empty StackTrace
    non_empty = source_pb2.StackTrace(frames=[source_pb2.StackFrame()])
    assert resource._get_source_position(non_empty) is None


def test_source_position_patch_was_applied():
    """Verify the source position patch applied at import time.

    The actual try/except guard cannot be re-executed without
    importlib.reload(), so we verify the patched functions exist
    and are lambdas from sdk_compat.py (not the original SDK functions).
    """
    import inspect

    from pulumi.runtime import resource

    # Both functions should be lambdas defined in sdk_compat.py
    assert resource._get_stack_trace.__name__ == "<lambda>"
    assert resource._get_source_position.__name__ == "<lambda>"
    assert "sdk_compat" in inspect.getfile(resource._get_stack_trace)
    assert "sdk_compat" in inspect.getfile(resource._get_source_position)


# ---------------------------------------------------------------------------
# Preview error/warning collection, res_op_failed, human-mode preview render
# (public-release review: #23, #8, #9, #33, #10)
# ---------------------------------------------------------------------------


def _make_diag(severity, message, urn=""):
    diag = MagicMock()
    diag.severity = severity
    diag.message = message
    diag.urn = urn
    return diag


def test_on_preview_collects_error_diagnostics():
    """A failing preview must surface real error detail, not a bare 'Preview failed.'."""
    handler = EventHandler(quiet=True)
    diag = _make_diag("error", "python inline source runtime error: boom from infra.py")
    handler.on_preview(_make_event(diagnostic=diag))
    assert len(handler.errors) == 1
    assert "boom from infra.py" in handler.errors[0].message


def test_on_preview_ignores_update_failed_summary():
    """The generic 'update failed' summary line is not collected as a diagnostic."""
    handler = EventHandler(quiet=True)
    handler.on_preview(_make_event(diagnostic=_make_diag("error", "update failed")))
    assert handler.errors == []


def test_warnings_collected_on_preview_and_update():
    """pulumi.log.warn / provider warnings are collected on both preview and update."""
    h1 = EventHandler(quiet=True)
    h1.on_preview(_make_event(diagnostic=_make_diag("warning", "DEPRECATION: foo is deprecated")))
    assert len(h1.warnings) == 1
    assert "DEPRECATION" in h1.warnings[0].message

    h2 = EventHandler(quiet=True)
    h2.on_update(_make_event(diagnostic=_make_diag("warning", "provider warning here")))
    assert len(h2.warnings) == 1


def test_render_warnings_prints_non_quiet():
    """render_warnings prints collected warnings (and is a no-op when quiet)."""
    from rich.console import Console

    import tlumi.display as display

    handler = EventHandler(quiet=False)
    handler.warnings.append(("aws:s3:Bucket b", "deprecated attribute"))

    cap = Console()
    with cap.capture() as captured:
        orig = display.console
        display.console = cap
        try:
            handler.render_warnings()
        finally:
            display.console = orig
    assert "deprecated attribute" in captured.get()

    quiet = EventHandler(quiet=True)
    quiet.warnings.append(("r", "should not print"))
    cap2 = Console()
    with cap2.capture() as captured2:
        orig = display.console
        display.console = cap2
        try:
            quiet.render_warnings()
        finally:
            display.console = orig
    assert captured2.get() == ""


def test_res_op_failed_clears_active_and_resets_fallback():
    """A failed resource op stops animating and no longer captures later log lines."""
    handler = EventHandler(quiet=True)
    urn = "urn:pulumi:default::p::aws:s3:BucketV2::bad"
    handler.on_update(_make_event(resource_pre=_make_resource_pre(urn, OpType.CREATE), timestamp=1))
    assert urn in handler._active_resources
    assert handler._last_started_urn == urn

    failed = MagicMock()
    failed.metadata = MagicMock()
    failed.metadata.urn = urn
    failed.metadata.type = "aws:s3:BucketV2"
    handler.on_update(_make_event(res_op_failed=failed, timestamp=2))

    assert urn not in handler._active_resources
    assert urn not in handler._active_display
    assert handler._last_started_urn == ""


def test_on_preview_human_mode_renders_header_and_diff_tree():
    """The default (non-quiet) plan rendering path produces a header and a diff tree."""
    from rich.console import Console

    import tlumi.engine as engine

    handler = EventHandler(quiet=False)
    pre = _make_resource_pre(
        "urn:pulumi:default::p::aws:s3:BucketV2::mybucket",
        OpType.CREATE,
        inputs={"bucket": "my-bucket-name", "acl": "private"},
    )

    cap = Console()
    orig = engine.console
    engine.console = cap
    try:
        with cap.capture() as captured:
            handler.on_preview(_make_event(resource_pre=pre))
    finally:
        engine.console = orig

    out = captured.get()
    assert "mybucket" in out
    assert "aws:s3:BucketV2" in out
    # CREATE expands inputs as adds, so the diff tree shows the input values.
    assert "bucket" in out
    assert handler.callback_errors == 0


# ---------------------------------------------------------------------------
# subtract_stack: hidden pulumi:pulumi:Stack excluded from counts (#13)
# redirect_program_stdout: user print() kept off stdout in JSON mode (#14)
# ---------------------------------------------------------------------------


def _make_res_outputs(urn, op, rtype="aws:s3:BucketV2"):
    out = MagicMock()
    out.metadata = MagicMock()
    out.metadata.urn = urn
    out.metadata.type = rtype
    out.metadata.op = op
    return out


def test_change_counts_excludes_stack_on_preview():
    """The Stack is never counted; a real create resource is (matches the listing)."""
    handler = EventHandler(quiet=True)
    handler.on_preview(
        _make_event(
            resource_pre=_make_resource_pre(
                "urn:pulumi:default::p::pulumi:pulumi:Stack::p",
                OpType.CREATE,
                rtype="pulumi:pulumi:Stack",
            )
        )
    )
    handler.on_preview(
        _make_event(
            resource_pre=_make_resource_pre(
                "urn:pulumi:default::p::aws:s3:BucketV2::b", OpType.CREATE
            )
        )
    )
    assert handler.change_counts() == {"create": 1}


def test_change_counts_empty_program_is_zero():
    """An empty program (only the Stack) yields no counted changes."""
    handler = EventHandler(quiet=True)
    handler.on_preview(
        _make_event(
            resource_pre=_make_resource_pre(
                "urn:pulumi:default::p::pulumi:pulumi:Stack::p",
                OpType.CREATE,
                rtype="pulumi:pulumi:Stack",
            )
        )
    )
    assert handler.change_counts() == {}


def test_change_counts_delete_on_update_completion():
    """Destroy/apply count completed (res_outputs) resources, excluding the Stack."""
    handler = EventHandler(quiet=True)
    handler.on_update(
        _make_event(
            res_outputs=_make_res_outputs(
                "urn:pulumi:default::p::pulumi:pulumi:Stack::p",
                OpType.DELETE,
                rtype="pulumi:pulumi:Stack",
            )
        )
    )
    handler.on_update(
        _make_event(
            res_outputs=_make_res_outputs(
                "urn:pulumi:default::p::aws:s3:BucketV2::b", OpType.DELETE
            )
        )
    )
    assert handler.change_counts() == {"delete": 1}


def test_change_counts_replacement_counted_once():
    """A replacement (CREATE_REPLACEMENT + REPLACE + DELETE_REPLACED) counts once."""
    handler = EventHandler(quiet=True)
    urn = "urn:pulumi:default::p::aws:s3:BucketV2::b"
    for op in (OpType.CREATE_REPLACEMENT, OpType.REPLACE, OpType.DELETE_REPLACED):
        handler.on_update(_make_event(res_outputs=_make_res_outputs(urn, op)))
    assert handler.change_counts() == {"replace": 1}


def test_redirect_program_stdout_active_routes_to_stderr(capsys):
    """When active, program stdout is redirected to stderr; inactive is a passthrough."""
    import sys

    from tlumi.engine import redirect_program_stdout

    with redirect_program_stdout(True):
        print("PROGRAM LINE")
    captured = capsys.readouterr()
    assert "PROGRAM LINE" in captured.err
    assert "PROGRAM LINE" not in captured.out

    with redirect_program_stdout(False):
        sys.stdout.write("DIRECT LINE\n")
    captured = capsys.readouterr()
    assert "DIRECT LINE" in captured.out


# ---------------------------------------------------------------------------
# Import op visibility (audit: IMPORT/IMPORT_REPLACEMENT were invisible and
# uncounted) and refresh outcome-op counting (codex-4 investigation).
# ---------------------------------------------------------------------------


def test_change_counts_import_on_preview():
    """An IMPORT pre-event is counted so 'N to import' reaches the summary."""
    handler = EventHandler(quiet=True)
    handler.on_preview(
        _make_event(
            resource_pre=_make_resource_pre(
                "urn:pulumi:default::p::random:index/randomString:RandomString::imp",
                OpType.IMPORT,
                rtype="random:index/randomString:RandomString",
            )
        )
    )
    assert handler.change_counts() == {"import": 1}
    assert handler.callback_errors == 0


def test_change_counts_import_replacement_counts_as_import():
    """IMPORT_REPLACEMENT maps into the import count."""
    handler = EventHandler(quiet=True)
    handler.on_preview(
        _make_event(
            resource_pre=_make_resource_pre(
                "urn:pulumi:default::p::aws:s3:BucketV2::b", OpType.IMPORT_REPLACEMENT
            )
        )
    )
    assert handler.change_counts() == {"import": 1}


def test_change_counts_import_on_update_completion():
    """A completed import during apply is counted from its res_outputs event."""
    handler = EventHandler(quiet=True)
    handler.on_update(
        _make_event(
            res_outputs=_make_res_outputs(
                "urn:pulumi:default::p::random:index/randomString:RandomString::imp",
                OpType.IMPORT,
                rtype="random:index/randomString:RandomString",
            )
        )
    )
    assert handler.change_counts() == {"import": 1}


def test_import_preview_renders_will_be_imported():
    """Human-mode preview shows the import line instead of silently dropping it."""
    from rich.console import Console

    import tlumi.engine as engine

    handler = EventHandler(quiet=False)
    pre = _make_resource_pre(
        "urn:pulumi:default::p::random:index/randomString:RandomString::impstr",
        OpType.IMPORT,
        rtype="random:index/randomString:RandomString",
    )

    cap = Console()
    orig = engine.console
    engine.console = cap
    try:
        with cap.capture() as captured:
            handler.on_preview(_make_event(resource_pre=pre))
    finally:
        engine.console = orig

    out = captured.get()
    assert "impstr" in out
    assert "imported" in out
    assert handler.callback_errors == 0


def test_refresh_outcome_ops_counted_from_outputs_events():
    """Refresh counting contract, verified against a live stack:

    resource_pre_events carry OpType.REFRESH (progress display only), while
    res_outputs_events carry the refresh OUTCOME op: SAME for no drift,
    UPDATE/DELETE for drift. Only outcome ops are counted, so a clean refresh
    reports 'State is up-to-date' and drift is counted as update/delete.
    OpType.REFRESH is deliberately absent from _OP_SUMMARY_KEY.
    """
    urn = "urn:pulumi:default::p::random:index/randomPet:RandomPet::pet"
    rtype = "random:index/randomPet:RandomPet"

    # Clean refresh: REFRESH pre + SAME outcome -> nothing counted.
    handler = EventHandler(quiet=True)
    handler.on_update(_make_event(resource_pre=_make_resource_pre(urn, OpType.REFRESH)))
    handler.on_update(_make_event(res_outputs=_make_res_outputs(urn, OpType.SAME, rtype=rtype)))
    assert handler.change_counts() == {}

    # Drifted refresh: REFRESH pre + UPDATE outcome -> counted as update.
    handler = EventHandler(quiet=True)
    handler.on_update(_make_event(resource_pre=_make_resource_pre(urn, OpType.REFRESH)))
    handler.on_update(_make_event(res_outputs=_make_res_outputs(urn, OpType.UPDATE, rtype=rtype)))
    assert handler.change_counts() == {"update": 1}


# ---------------------------------------------------------------------------
# Output-only change detection: Pulumi previews an edited pulumi.export() as
# SAME ops everywhere; the Stack res_outputs_event old/new outputs are the
# only signal (verified against a live stack).
# ---------------------------------------------------------------------------


def test_stack_outputs_changed_set_when_outputs_differ():
    from helpers import stack_outputs_event

    handler = EventHandler(quiet=True)
    handler.on_preview(stack_outputs_event({"greeting": "world"}, {"greeting": "mars"}))
    assert handler.stack_outputs_changed is True
    assert handler.callback_errors == 0


def test_stack_outputs_changed_false_when_outputs_equal():
    from helpers import stack_outputs_event

    handler = EventHandler(quiet=True)
    handler.on_preview(stack_outputs_event({"greeting": "world"}, {"greeting": "world"}))
    assert handler.stack_outputs_changed is False


def test_stack_outputs_changed_sticky_across_events():
    """The flag latches: a later equal-outputs Stack event must not clear it."""
    from helpers import stack_outputs_event

    handler = EventHandler(quiet=True)
    handler.on_preview(stack_outputs_event({"a": 1}, {"a": 2}))
    handler.on_preview(stack_outputs_event({"a": 1}, {"a": 1}))
    assert handler.stack_outputs_changed is True


def test_stack_outputs_changed_first_apply_with_exports():
    """First-ever apply of a program with exports: old state absent, new outputs pending."""
    from helpers import stack_outputs_event

    handler = EventHandler(quiet=True)
    handler.on_preview(stack_outputs_event(None, {"greeting": "world"}))
    assert handler.stack_outputs_changed is True


def test_stack_outputs_changed_empty_program_stays_false():
    """An empty program (no exports) must keep the documented 'No changes' behavior."""
    from helpers import stack_outputs_event

    handler = EventHandler(quiet=True)
    handler.on_preview(stack_outputs_event(None, {}))
    assert handler.stack_outputs_changed is False


def test_stack_outputs_changed_ignores_non_stack_resources():
    from helpers import stack_outputs_event

    handler = EventHandler(quiet=True)
    ev = stack_outputs_event({"a": 1}, {"a": 2})
    ev.res_outputs_event.metadata.type = "aws:s3:BucketV2"
    handler.on_preview(ev)
    assert handler.stack_outputs_changed is False
