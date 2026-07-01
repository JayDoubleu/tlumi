"""Unified resource resolution for tlumi.

Provides URN parsing helpers and a single resource resolver used by both
the engine (target resolution) and state commands (resource lookup).
"""

from __future__ import annotations

from pulumi import automation as auto

from tlumi.errors import WorkspaceError
from tlumi.workspace import safe_export_stack


def name_from_urn(urn: str) -> str:
    """Extract the resource name from a Pulumi URN.

    URN format: ``urn:pulumi:<stack>::<project>::<type>::<name>``. The name is
    everything after the third ``::`` and MAY itself contain ``::`` (Pulumi
    permits it), so we parse positionally with ``split("::", 3)`` rather than
    taking the last segment (which would truncate ``foo::bar`` to ``bar``).
    """
    parts = urn.split("::", 3)
    if len(parts) >= 4:
        return parts[3]
    return parts[-1] if parts else urn


def type_from_urn(urn: str) -> str:
    """Extract the resource type from a Pulumi URN (third ``::`` field).

    For child resources this includes the parent type chain
    (``pkg:Parent$pkg:Child``). Parsed positionally so a name containing ``::``
    does not shift the type field.
    """
    parts = urn.split("::", 3)
    if len(parts) >= 3:
        return parts[2]
    return urn


def display_from_urn(urn: str) -> str:
    """Format a URN as 'type (name)' for display."""
    parts = urn.split("::", 3)
    if len(parts) >= 4:
        return f"{parts[2]} ({parts[3]})"
    if len(parts) >= 2:
        return f"{parts[-2]} ({parts[-1]})"
    return urn


def _is_condemned(r: dict) -> bool:
    """True if a state entry is slated for deletion.

    Either the old copy of a create-before-delete replacement (``delete: true``)
    or a resource pending replacement (``pendingReplacement: true``). Pulumi
    permits duplicate URNs in a valid snapshot as long as all but one such entry
    are condemned.
    """
    return bool(r.get("delete") or r.get("pendingReplacement"))


def _dedupe_by_urn(matches: list[dict]) -> list[dict]:
    """Collapse duplicate-URN entries to a single live entry per URN.

    During a create-before-delete replacement (or an apply interrupted before
    the old copy is deleted) two state entries share one URN: the live resource
    and the condemned old copy. Without this, name/type lookups see two matches
    and raise an unsatisfiable "ambiguous" error -- the suggested URN / type::name
    hints cannot disambiguate byte-identical entries. Entries with distinct URNs
    are preserved so genuine ambiguity still raises.
    """
    by_urn: dict[str, dict] = {}
    for r in matches:
        urn = r.get("urn", "")
        existing = by_urn.get(urn)
        if existing is None or (_is_condemned(existing) and not _is_condemned(r)):
            by_urn[urn] = r
    return list(by_urn.values())


def resolve_resource(resources: list[dict], identifier: str) -> dict:
    """Find a resource by full URN, type::name, or just name.

    Raises WorkspaceError on not-found or ambiguous name-only matches.
    """
    # Exact URN match. Pulumi may store duplicate-URN entries during a
    # create-before-delete replacement (the live copy plus the old copy marked
    # delete:true); prefer the live entry over a condemned one.
    urn_matches = [r for r in resources if r.get("urn") == identifier]
    if urn_matches:
        return _dedupe_by_urn(urn_matches)[0]

    # Type + name match (e.g. "aws:s3:Bucket::my-bucket"). URN type segments
    # never contain "::" (types are "pkg:mod:Type", chained with "$" for
    # children) while resource names MAY (Pulumi permits it), so the
    # identifier splits at the FIRST "::". The whole identifier is also tried
    # as a bare name so a resource literally named "web::primary" resolves;
    # if both interpretations match, that is a genuine ambiguity.
    if "::" in identifier:
        req_type, _, req_name = identifier.partition("::")
        matches = []
        for r in resources:
            urn = r.get("urn", "")
            name = name_from_urn(urn)
            if name == identifier:
                matches.append(r)
                continue
            if name != req_name:
                continue
            # Match against both URN-derived type and canonical state type.
            # URN type includes parent prefix for child resources (e.g.
            # "pkg:Parent$aws:s3:Bucket"), while canonical type is just
            # "aws:s3:Bucket".
            if type_from_urn(urn) == req_type or r.get("type", "") == req_type:
                matches.append(r)
        matches = _dedupe_by_urn(matches)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            match_list = ", ".join(
                f"{type_from_urn(r.get('urn', ''))}::{name_from_urn(r.get('urn', ''))}"
                for r in matches
            )
            raise WorkspaceError(
                f"Ambiguous '{identifier}' matches {len(matches)} resources: {match_list}",
                hint="Use the full URN to disambiguate.",
            )
        raise WorkspaceError(
            f"Resource not found: {identifier}",
            hint="Run 'tlumi state list' to see available resources.",
        )

    # Name-only match: check for ambiguity (collapsing duplicate-URN
    # replacement pairs to their live entry first).
    matches = _dedupe_by_urn(
        [r for r in resources if name_from_urn(r.get("urn", "")) == identifier]
    )

    if not matches:
        raise WorkspaceError(
            f"Resource not found: {identifier}",
            hint="Run 'tlumi state list' to see available resources.",
        )

    if len(matches) > 1:
        match_list = ", ".join(f"{type_from_urn(r.get('urn', ''))}::{identifier}" for r in matches)
        raise WorkspaceError(
            f"Ambiguous name '{identifier}' matches {len(matches)} resources: {match_list}",
            hint="Use type::name format to disambiguate.",
        )

    return matches[0]


def resolve_targets(
    stack: auto.Stack,
    identifiers: list[str],
    resources: list[dict] | None = None,
) -> list[str]:
    """Resolve resource identifiers to full Pulumi URNs."""
    to_resolve = []
    resolved = []
    for ident in identifiers:
        if ident.startswith("urn:pulumi:"):
            resolved.append(ident)
        else:
            to_resolve.append(ident)

    if not to_resolve:
        return resolved

    # Load state once (skip if pre-loaded resources provided)
    if resources is None:
        state = safe_export_stack(stack)
        resources = []
        if state.deployment:
            resources = [
                r
                for r in state.deployment.get("resources", [])
                if r.get("type") != "pulumi:pulumi:Stack"
            ]

    for ident in to_resolve:
        found = resolve_resource(resources, ident)
        urn = found.get("urn", "")
        if not urn:
            raise WorkspaceError(
                f"Resource '{ident}' has no URN in state.",
                hint="State may be corrupted. Run 'tlumi state pull' to inspect.",
            )
        resolved.append(urn)

    return resolved
