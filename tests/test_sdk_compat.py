"""Tests for tlumi.sdk_compat: source-position suppression patch.

Pulumi's runtime resource module embeds local filesystem paths into state
(sourcePosition, stackTrace). tlumi monkey-patches private SDK helpers so
these paths never make it into a state file. Upstream has not provided a
public option; this test pins the contract so a future SDK refactor that
renames the targets fails loudly here instead of silently regressing.
"""

from __future__ import annotations

import importlib
import logging
import sys

import pytest

# Imported for the autouse reload fixture; the module-level patch fires on import.
import tlumi.sdk_compat  # noqa: F401


def _reload_sdk_compat():
    """Re-import sdk_compat so its module-level patch runs again.

    Some tests deliberately pop the module from sys.modules to exercise the
    fail-open branch. In that case reload would fail with ``module not in
    sys.modules``, so we fall back to a fresh import. Either path leaves
    the patch in its applied state for the next test.
    """
    if "tlumi.sdk_compat" in sys.modules:
        importlib.reload(sys.modules["tlumi.sdk_compat"])
    else:
        importlib.import_module("tlumi.sdk_compat")


def test_patch_targets_exist():
    """The private helpers we patch must still exist in the installed SDK."""
    from pulumi.runtime import resource as r

    assert hasattr(r, "_get_stack_trace"), (
        "Pulumi SDK no longer exposes _get_stack_trace; update sdk_compat.py."
    )
    assert hasattr(r, "_get_source_position"), (
        "Pulumi SDK no longer exposes _get_source_position; update sdk_compat.py."
    )


def test_get_stack_trace_returns_empty_after_patch():
    """After import, _get_stack_trace must return an empty StackTrace.

    Empty means no frames; the proto default-constructed message. The patch
    is applied at sdk_compat module import time, which already happened by
    the time this test runs because tlumi.sdk_compat is imported by other
    tlumi modules.
    """
    from pulumi.runtime import resource as r
    from pulumi.runtime.proto import source_pb2

    result = r._get_stack_trace()
    assert isinstance(result, source_pb2.StackTrace)
    assert len(result.frames) == 0, (
        f"Patch did not suppress stack frames; got {len(result.frames)} frame(s). "
        "If Pulumi added a public suppression option, remove sdk_compat.py and "
        "switch to that. Otherwise the patch lost its effect."
    )


def test_get_source_position_returns_none_after_patch():
    """After import, _get_source_position must return None for any input."""
    from pulumi.runtime import resource as r
    from pulumi.runtime.proto import source_pb2

    result = r._get_source_position(source_pb2.StackTrace())
    assert result is None, (
        f"Patch did not suppress source position; got {result!r}. "
        "If Pulumi added a public suppression option, remove sdk_compat.py and "
        "switch to that. Otherwise the patch lost its effect."
    )


def test_patch_logs_warning_when_targets_missing(caplog, monkeypatch):
    """Fail-open behavior: if Pulumi renames the targets, log WARNING and continue.

    The patch must not raise. tlumi commands would otherwise be unable to run
    on a Pulumi version that ships before the user upgrades sdk_compat.py.
    """
    from pulumi.runtime import resource as r

    # Remove one of the patched attributes so the AttributeError branch fires
    monkeypatch.delattr(r, "_get_stack_trace", raising=False)

    # Drop the cached module so the import-time guard runs again
    sys.modules.pop("tlumi.sdk_compat", None)
    with caplog.at_level(logging.WARNING, logger="tlumi.sdk_compat"):
        importlib.import_module("tlumi.sdk_compat")

    assert any("SDK source position patch skipped" in rec.message for rec in caplog.records), (
        "Expected a WARNING when the patch targets are missing."
    )


def test_patch_skipped_when_stacktrace_message_missing(caplog, monkeypatch):
    """Fail-open: a future SDK that drops/renames StackTrace skips the patch (F24).

    The _get_stack_trace replacement builds source_pb2.StackTrace() at call time
    (every resource registration). Probing StackTrace at patch time turns a
    would-be per-register AttributeError into a clean, logged skip.
    """
    from pulumi.runtime.proto import source_pb2

    monkeypatch.delattr(source_pb2, "StackTrace", raising=False)

    sys.modules.pop("tlumi.sdk_compat", None)
    with caplog.at_level(logging.WARNING, logger="tlumi.sdk_compat"):
        importlib.import_module("tlumi.sdk_compat")

    assert any("SDK source position patch skipped" in rec.message for rec in caplog.records), (
        "Expected a WARNING when StackTrace is missing from source_pb2."
    )


@pytest.fixture(autouse=True)
def restore_patch():
    """Re-run the patch after tests that might disturb it."""
    yield
    _reload_sdk_compat()
