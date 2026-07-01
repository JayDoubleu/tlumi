"""Tests for tlumi.sanitize: sentinel detection, sanitization, and unwrapping."""

from __future__ import annotations

import json

from tlumi.sanitize import (
    _MAX_DEPTH,
    _PULUMI_SECRET_SIG,
    _PULUMI_SECRET_VALUE,
    _PULUMI_UNKNOWN,
    _SENSITIVE_MARKER,
    _UNKNOWN_MARKER,
    _sanitize_value,
    _unwrap_secrets,
)


def _wrapper(**fields: object) -> dict:
    """Build a Pulumi secret wrapper dict with the given extra fields."""
    return {_PULUMI_SECRET_SIG: _PULUMI_SECRET_VALUE, **fields}


def test_sanitize_value_at_max_depth_still_sanitizes():
    """Sanitize works right at the max depth boundary (depth 49)."""
    # Build a nested dict that is exactly _MAX_DEPTH - 1 levels deep
    value = "04da6b54-80e4-46f7-96ec-b56ff0331ba9"  # unknown sentinel
    for _ in range(_MAX_DEPTH - 1):
        value = {"nested": value}
    result = _sanitize_value(value)
    # At depth _MAX_DEPTH - 1, the innermost value should be sanitized
    inner = result
    for _ in range(_MAX_DEPTH - 1):
        inner = inner["nested"]
    assert inner == "(known after apply)"


def test_sanitize_value_exceeds_max_depth_returns_sensitive():
    """Values beyond max depth return the sensitive marker (safe default)."""
    # Build a nested dict that exceeds _MAX_DEPTH
    value = "04da6b54-80e4-46f7-96ec-b56ff0331ba9"  # unknown sentinel
    for _ in range(_MAX_DEPTH + 1):
        value = {"nested": value}
    result = _sanitize_value(value)
    # Dig down: at max depth the value is replaced with the sensitive marker
    inner = result
    for _ in range(_MAX_DEPTH):
        inner = inner["nested"]
    assert inner == _SENSITIVE_MARKER


def test_sanitize_secret_at_depth_boundary():
    """Secret sentinel at depth 49 (last valid depth) is sanitized."""
    secret = {_PULUMI_SECRET_SIG: _PULUMI_SECRET_VALUE, "value": "[secret]"}
    value = secret
    for _ in range(_MAX_DEPTH - 1):
        value = {"nested": value}
    result = _sanitize_value(value)
    inner = result
    for _ in range(_MAX_DEPTH - 1):
        inner = inner["nested"]
    assert inner == _SENSITIVE_MARKER


def test_sanitize_secret_beyond_depth_boundary():
    """Secret sentinel beyond max depth returns sensitive marker (safe default)."""
    secret = {_PULUMI_SECRET_SIG: _PULUMI_SECRET_VALUE, "value": "[secret]"}
    value = secret
    for _ in range(_MAX_DEPTH + 1):
        value = {"nested": value}
    result = _sanitize_value(value)
    inner = result
    for _ in range(_MAX_DEPTH):
        inner = inner["nested"]
    # Beyond max depth, the value is replaced with the sensitive marker
    assert inner == _SENSITIVE_MARKER


def test_sanitize_secret_wrapper_dict_at_top_level():
    """Direct secret-wrapper dict is replaced with the sensitive marker."""
    secret = {_PULUMI_SECRET_SIG: _PULUMI_SECRET_VALUE, "value": "hunter2"}
    assert _sanitize_value(secret) == _SENSITIVE_MARKER


def test_sanitize_secret_wrapper_nested_in_dict():
    """Secret wrapper as a dict value is replaced; surrounding structure preserved."""
    secret = {_PULUMI_SECRET_SIG: _PULUMI_SECRET_VALUE, "value": "hunter2"}
    value = {"name": "alice", "credentials": secret, "active": True}
    result = _sanitize_value(value)
    assert result == {"name": "alice", "credentials": _SENSITIVE_MARKER, "active": True}


def test_sanitize_unknown_sentinel_string():
    """Strings containing the unknown sentinel UUID are replaced with the unknown marker."""
    assert _sanitize_value(_PULUMI_UNKNOWN) == _UNKNOWN_MARKER
    # Also when the sentinel is embedded in a larger string
    assert _sanitize_value(f"prefix-{_PULUMI_UNKNOWN}-suffix") == _UNKNOWN_MARKER


def test_sanitize_secret_sig_in_leaf_string():
    """Defense-in-depth: a leaf string containing the secret sig is masked.

    Normal state JSON wraps secrets as dicts, but debug logs / embedded JSON
    payloads can lose the wrapper. If we see the sig in plain text, mask the
    whole value.
    """
    assert _sanitize_value(f"some data with {_PULUMI_SECRET_SIG} embedded") == _SENSITIVE_MARKER


def test_sanitize_bracket_secret_string():
    """The literal '[secret]' ciphertext string is replaced with the marker."""
    assert _sanitize_value("[secret]") == _SENSITIVE_MARKER


def test_sanitize_bracket_secret_in_nested_dict():
    """The '[secret]' string is replaced wherever it appears."""
    value = {"db": {"password": "[secret]"}, "user": "alice"}
    result = _sanitize_value(value)
    assert result == {"db": {"password": _SENSITIVE_MARKER}, "user": "alice"}


def test_sanitize_non_sentinel_strings_unchanged():
    """Regular strings are returned unchanged (no false positives)."""
    assert _sanitize_value("hello") == "hello"
    assert _sanitize_value("a normal config value") == "a normal config value"
    # A UUID that is NOT the sentinel passes through unchanged.
    assert (
        _sanitize_value("00000000-0000-0000-0000-000000000000")
        == "00000000-0000-0000-0000-000000000000"
    )


def test_sanitize_unknown_in_list():
    """Unknown sentinels inside lists are replaced; other items preserved."""
    value = ["alice", _PULUMI_UNKNOWN, "bob"]
    assert _sanitize_value(value) == ["alice", _UNKNOWN_MARKER, "bob"]


def test_sanitize_preserves_scalar_types():
    """Bools, ints, floats, None pass through unchanged."""
    assert _sanitize_value(True) is True
    assert _sanitize_value(False) is False
    assert _sanitize_value(42) == 42
    assert _sanitize_value(3.14) == 3.14
    assert _sanitize_value(None) is None


def test_sanitize_mid_depth_unknown_sentinel():
    """Unknown sentinel at mid-depth (well below max) is replaced inline."""
    value = {"a": {"b": {"c": _PULUMI_UNKNOWN}}}
    result = _sanitize_value(value)
    assert result == {"a": {"b": {"c": _UNKNOWN_MARKER}}}


def test_sanitize_partial_secret_wrapper_not_replaced():
    """A dict with only the sig key but a different value is NOT treated as a secret."""
    # The wrapper requires BOTH the sig key AND the matching value constant.
    fake = {_PULUMI_SECRET_SIG: "not-the-magic-value", "value": "data"}
    result = _sanitize_value(fake)
    assert isinstance(result, dict)
    assert result["value"] == "data"


# ---------------------------------------------------------------------------
# _unwrap_secrets: --show-secrets human-display decoding of secret wrappers
# ---------------------------------------------------------------------------


def test_unwrap_decodes_string_plaintext():
    """The wrapper's JSON-encoded plaintext string decodes to the raw value."""
    wrapped = _wrapper(plaintext=json.dumps("hunter2"))
    assert _unwrap_secrets(wrapped) == "hunter2"


def test_unwrap_decodes_compound_plaintext():
    """A compound plaintext (whole-dict secret) decodes to the structure."""
    wrapped = _wrapper(plaintext=json.dumps({"user": "alice", "pass": "hunter2"}))
    assert _unwrap_secrets(wrapped) == {"user": "alice", "pass": "hunter2"}


def test_unwrap_nested_in_dict_preserves_structure():
    """Wrappers nested inside dicts/lists are replaced; siblings untouched."""
    value = {
        "host": "example.com",
        "password": _wrapper(plaintext=json.dumps("hunter2")),
        "tags": [_wrapper(plaintext=json.dumps("t1")), "plain"],
    }
    assert _unwrap_secrets(value) == {
        "host": "example.com",
        "password": "hunter2",
        "tags": ["t1", "plain"],
    }


def test_unwrap_without_plaintext_falls_back_to_marker():
    """A ciphertext-only wrapper (state exported without secrets) is masked."""
    wrapped = _wrapper(ciphertext="v1:AAAA:base64garbage")
    assert _unwrap_secrets(wrapped) == _SENSITIVE_MARKER


def test_unwrap_undecodable_plaintext_returns_raw_string():
    """A plaintext field that is not valid JSON is shown as-is, not masked."""
    wrapped = _wrapper(plaintext="not json {")
    assert _unwrap_secrets(wrapped) == "not json {"


def test_unwrap_non_wrapper_values_unchanged():
    """Non-wrapper dicts, lists, and scalars pass through untouched."""
    fake = {_PULUMI_SECRET_SIG: "not-the-magic-value", "plaintext": '"x"'}
    assert _unwrap_secrets(fake) == fake
    assert _unwrap_secrets("hello") == "hello"
    assert _unwrap_secrets(42) == 42
    assert _unwrap_secrets(None) is None
    assert _unwrap_secrets(["a", 1]) == ["a", 1]


def test_unwrap_does_not_mask_sentinel_strings():
    """Unlike _sanitize_value, unwrapping leaves sentinel-ish strings alone."""
    # A leaf string containing the sig is a display-fidelity concern for
    # _sanitize_value only; --show-secrets mode shows stored values verbatim.
    assert _unwrap_secrets("[secret]") == "[secret]"


def test_unwrap_at_max_depth_returns_value_unchanged():
    """Past the depth cap, values are returned as-is (no transformation)."""
    wrapped = _wrapper(plaintext=json.dumps("deep"))
    value: object = wrapped
    for _ in range(_MAX_DEPTH + 1):
        value = {"nested": value}
    result = _unwrap_secrets(value)
    inner = result
    for _ in range(_MAX_DEPTH):
        inner = inner["nested"]
    # At the cap the wrapper survives untouched (helper is not a mask).
    assert inner == {"nested": wrapped}
