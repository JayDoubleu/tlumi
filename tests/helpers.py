"""Shared test fixtures for tlumi tests."""

from __future__ import annotations

from unittest.mock import MagicMock


def mock_state(resources=None, version=3):
    """Create a mock Pulumi state object for testing.

    Args:
        resources: List of resource dicts, or None for empty state.
        version: State version number.
    """
    state = MagicMock()
    state.version = version
    if resources is None:
        state.deployment = None
    else:
        state.deployment = {"resources": resources}
    return state


def mock_preview_result(change_summary=None):
    """Create a mock Pulumi PreviewResult."""
    result = MagicMock()
    result.change_summary = change_summary or {}
    return result


def mock_up_result(outputs=None, resource_changes=None):
    """Create a mock Pulumi UpResult."""
    result = MagicMock()
    result.outputs = outputs or {}
    summary = MagicMock()
    summary.resource_changes = resource_changes or {}
    result.summary = summary
    return result


def mock_summary_result(resource_changes=None):
    """Create a mock Pulumi result with summary (destroy/refresh)."""
    result = MagicMock()
    summary = MagicMock()
    summary.resource_changes = resource_changes or {}
    result.summary = summary
    return result


def emit_events(counts=None, *, kind="res_outputs", result=None):
    """Side-effect for a mocked stack.up/preview/destroy/refresh that drives on_event.

    The engine derives the user-facing summary from the resources it observes via
    on_event (not from result.summary), so a mock must fire synthetic events. Use
    kind="res_outputs" for up/destroy/refresh (completed resources) and
    kind="resource_pre" for preview (planned resources). Returns ``result``.
    """
    from pulumi.automation import OpType

    op_map = {
        "create": OpType.CREATE,
        "update": OpType.UPDATE,
        "replace": OpType.REPLACE,
        "delete": OpType.DELETE,
        "import": OpType.IMPORT,
    }

    def _meta(urn, op):
        m = MagicMock()
        m.urn = urn
        m.type = "aws:s3:BucketV2"
        m.op = op
        m.diffs = None
        m.keys = None
        m.detailed_diff = None
        m.old = None
        m.new = None
        return m

    def _side_effect(*args, **kwargs):
        on_event = kwargs.get("on_event")
        if on_event is not None:
            for key, n in (counts or {}).items():
                op = op_map.get(key)
                if op is None:
                    continue
                for i in range(n):
                    ev = MagicMock()
                    ev.resource_pre_event = None
                    ev.res_outputs_event = None
                    ev.diagnostic_event = None
                    ev.res_op_failed_event = None
                    ev.timestamp = 0
                    slot = MagicMock()
                    slot.metadata = _meta(f"urn:pulumi:default::p::aws:s3:BucketV2::{key}{i}", op)
                    if kind == "res_outputs":
                        ev.res_outputs_event = slot
                    else:
                        ev.resource_pre_event = slot
                    on_event(ev)
        return result

    return _side_effect


def mock_output_value(value, secret=False):
    """Create a mock Pulumi OutputValue."""
    out = MagicMock()
    out.value = value
    out.secret = secret
    return out


def stack_outputs_event(old_outputs, new_outputs):
    """Build a res_outputs_event for the hidden pulumi:pulumi:Stack resource.

    Mirrors the live event shape of a preview: the final Stack outputs event
    carries the current outputs in ``old.outputs`` and the pending outputs in
    ``new.outputs`` (op SAME even when outputs change; verified against a
    live stack). Used to test output-only-change detection.
    """
    from pulumi.automation import OpType

    ev = MagicMock()
    ev.resource_pre_event = None
    ev.diagnostic_event = None
    ev.res_op_failed_event = None
    ev.timestamp = 0
    slot = MagicMock()
    meta = MagicMock()
    meta.urn = "urn:pulumi:default::p::pulumi:pulumi:Stack::p"
    meta.type = "pulumi:pulumi:Stack"
    meta.op = OpType.SAME
    old = MagicMock()
    old.outputs = old_outputs
    new = MagicMock()
    new.outputs = new_outputs
    meta.old = old if old_outputs is not None else None
    meta.new = new if new_outputs is not None else None
    slot.metadata = meta
    ev.res_outputs_event = slot
    return ev
