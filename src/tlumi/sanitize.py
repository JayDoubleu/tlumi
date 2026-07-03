"""Pulumi sentinel value sanitization.

Replaces Pulumi's secret and unknown sentinels in nested data structures
with human-readable markers. Used by diff formatting, state display,
and show commands.
"""

from __future__ import annotations

import json
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


def _contains_secret_sentinel(value: object, _depth: int = 0) -> bool:
    """True when a Pulumi secret sentinel appears anywhere in ``value``.

    Real preview events scrub every secret input to a BYTE-IDENTICAL wrapper
    (``{<sig>: <magic>, "ciphertext": "[secret]"}``) on both the old and new
    side even when the engine reports the value changed, so neither raw nor
    sanitized comparison can detect the change. Diff extraction uses this to
    know it must fall back to the engine's own diff signal and still surface
    the secret (masked) rather than dropping it as a no-op. Returns True at
    the depth limit as the conservative default: surfacing a masked value is
    safe, silently dropping a real secret change is the bug.
    """
    if _depth >= _MAX_DEPTH:
        return True
    if isinstance(value, dict):
        if value.get(_PULUMI_SECRET_SIG) == _PULUMI_SECRET_VALUE:
            return True
        return any(_contains_secret_sentinel(v, _depth + 1) for v in value.values())
    if isinstance(value, list):
        return any(_contains_secret_sentinel(v, _depth + 1) for v in value)
    if isinstance(value, str):
        # Defense in depth, mirroring _sanitize_value: the sig may survive
        # inside a leaf string (embedded JSON) even when the wrapper-dict form
        # is lost, and a bare "[secret]" is Pulumi's scrubbed ciphertext.
        return _PULUMI_SECRET_SIG in value or value == "[secret]"
    return False


def _unwrap_secrets(value: object, _depth: int = 0) -> object:
    """Recursively replace secret wrapper dicts with their decoded plaintext.

    Sibling of _sanitize_value() for the human-readable --show-secrets render
    paths (show, state show): exported state stores each secret as
    ``{<sig>: <magic>, "plaintext": "<json-encoded value>"}`` and printing
    that wrapper verbatim makes the flag look broken. Pulumi JSON-encodes the
    ``plaintext`` field, so it is decoded before display; the decoded value is
    walked again defensively in case it embeds further wrappers.

    Fallbacks: a wrapper without a ``plaintext`` string (state exported
    without secrets carries ciphertext only) becomes the sensitive marker
    rather than dumping base64 ciphertext; an undecodable ``plaintext`` is
    returned as the raw string (the user asked for the value, and masking it
    would reproduce the very "flag looks broken" problem).

    This is a display-fidelity helper, not a mask: _sanitize_value() owns the
    masking guarantees. At _MAX_DEPTH the value is returned unchanged (no
    transformation past the cap).
    """
    if _depth >= _MAX_DEPTH:
        return value
    if isinstance(value, dict) and value.get(_PULUMI_SECRET_SIG) == _PULUMI_SECRET_VALUE:
        plaintext = value.get("plaintext")
        if not isinstance(plaintext, str):
            return _SENSITIVE_MARKER
        try:
            decoded = json.loads(plaintext)
        except (ValueError, RecursionError):
            # RecursionError: a pathologically nested plaintext string (e.g. an
            # attacker-shipped .tlumi/) is treated like any other undecodable
            # plaintext instead of crashing the command.
            return plaintext
        return _unwrap_secrets(decoded, _depth + 1)
    if isinstance(value, dict):
        return {k: _unwrap_secrets(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_unwrap_secrets(v, _depth + 1) for v in value]
    return value
