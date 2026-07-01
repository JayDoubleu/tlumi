"""Pulumi sentinel value sanitization.

Replaces Pulumi's secret and unknown sentinels in nested data structures
with human-readable markers. Used by diff formatting, state display,
and show commands.
"""

from __future__ import annotations

from typing import Final

_PULUMI_UNKNOWN: Final = "04da6b54-80e4-46f7-96ec-b56ff0331ba9"
_PULUMI_SECRET_SIG: Final = "4dabf18193072939515e22adb298388d"
_PULUMI_SECRET_VALUE: Final = "1b47061264138c4ac30d75fd1eb44270"

_SENSITIVE_MARKER: Final = "(sensitive)"
_UNKNOWN_MARKER: Final = "(known after apply)"


_MAX_DEPTH: Final = 50


def _sanitize_value(value: object, _depth: int = 0) -> object:
    """Recursively replace sentinel nodes with marker strings.

    Only the sentinel node itself is replaced. Surrounding structure is preserved.
    At _MAX_DEPTH, returns the sensitive marker as a safe default to prevent
    leaking deeply nested secrets.
    """
    if _depth >= _MAX_DEPTH:
        return _SENSITIVE_MARKER
    if isinstance(value, dict) and value.get(_PULUMI_SECRET_SIG) == _PULUMI_SECRET_VALUE:
        return _SENSITIVE_MARKER
    if isinstance(value, str):
        if _PULUMI_UNKNOWN in value:
            return _UNKNOWN_MARKER
        if _PULUMI_SECRET_SIG in value:
            # Defense in depth: in normal state JSON, secrets appear as the
            # dict-wrapper handled above, but if the sig ever appears inside
            # a leaf string (debug logs, embedded JSON), mask the whole value.
            return _SENSITIVE_MARKER
        if value == "[secret]":
            return _SENSITIVE_MARKER
        return value
    if isinstance(value, dict):
        return {k: _sanitize_value(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_value(v, _depth + 1) for v in value]
    return value
