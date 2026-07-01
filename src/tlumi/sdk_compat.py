"""SDK compatibility patches for Pulumi Automation API.

Patches:
1. Source position suppression: patches _get_stack_trace and
   _get_source_position in pulumi.runtime.resource to return empty values,
   preventing local filesystem paths from being persisted in state.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)


# --- Source position suppression ---
# Pulumi persists local filesystem paths (sourcePosition, stackTrace) in state.
# These are metadata-only (engine skips them in diff/mustWrite) but leak
# developer machine paths into shared state backends. Patch the SDK to
# return empty values so no paths are recorded.
try:
    from pulumi.runtime import resource as _pulumi_resource
    from pulumi.runtime.proto import source_pb2 as _source_pb2

    # Verify the functions exist before patching (AttributeError if not)
    _pulumi_resource._get_stack_trace  # noqa: B018
    _pulumi_resource._get_source_position  # noqa: B018
    # The _get_stack_trace replacement constructs _source_pb2.StackTrace() at
    # call time (a hot path on every resource registration). Probe it now so a
    # future SDK that drops or renames StackTrace is caught by the except below
    # and the patch is skipped (fail open), rather than raising AttributeError
    # on every register. (_get_source_position returns None and has no such
    # dependency, so it needs no probe.)
    _source_pb2.StackTrace  # noqa: B018

    _pulumi_resource._get_stack_trace = lambda: _source_pb2.StackTrace()
    _pulumi_resource._get_source_position = lambda _: None  # type: ignore[assignment]

    _log.debug("SDK source position patch applied: paths suppressed from state")
except (ImportError, AttributeError) as _exc:
    # Fail open rather than refuse to run: the patch targets private SDK
    # functions, and a missing one means a newer Pulumi version moved them.
    # Hard-failing would break every tlumi command on SDK upgrades, so we
    # warn loudly and let state continue (paths will reappear until the
    # patch is updated or upstream lands a real fix).
    _log.warning(
        "SDK source position patch skipped: SDK layout changed (%s); "
        "local paths may appear in state",
        _exc,
    )
