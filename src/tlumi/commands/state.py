"""tlumi state - Inspect and manage state."""

from __future__ import annotations

import json
import logging
import os
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any, Final, cast

from pulumi.automation import CommandError, Deployment
from rich.markup import escape

from tlumi._safeio import safe_mkdir
from tlumi.config import DEFAULT_STACK, find_project_dir, load_config
from tlumi.diffs import _strip_dunder_keys
from tlumi.display import (
    animated_status,
    confirm,
    console,
    print_json,
    print_success,
    print_warning,
)
from tlumi.errors import WorkspaceError
from tlumi.redact import redact_text
from tlumi.resolve import display_from_urn, name_from_urn, resolve_resource
from tlumi.workspace import get_stack, safe_export_stack

_log = logging.getLogger(__name__)

_MAX_BACKUPS: Final = 10


def _rotate_backups(backup_dir: Path, op_prefix: str = "state_") -> None:
    """Remove oldest backups for a given operation prefix when count exceeds _MAX_BACKUPS.

    Per-op quotas prevent a chatty operation (e.g. a scripted retry loop on
    ``state rm``) from rotating out the ``state_push`` backup the user
    actually wanted to revert to. Each op (``state_rm_``, ``state_mv_``,
    ``state_push_``) gets its own independent cap.

    Lists all ``<op_prefix>*.json`` files sorted by modification time (oldest
    first) and removes the oldest. Best-effort: rotation failures are surfaced
    via ``_log.warning`` so a stuck rotation is visible at default verbosity
    rather than silently filling the backup directory.
    """
    try:
        backups = sorted(backup_dir.glob(f"{op_prefix}*.json"), key=lambda p: p.stat().st_mtime)
    except OSError:
        _log.warning("Cannot list backups in %s (rotation skipped)", backup_dir, exc_info=True)
        return
    to_remove = len(backups) - _MAX_BACKUPS
    if to_remove <= 0:
        return
    for path in backups[:to_remove]:
        try:
            path.unlink()
            _log.debug("Rotated old backup: %s", path.name)
        except OSError as e:
            _log.warning("Failed to rotate backup %s: %s", path.name, e)


def _provider_matches(provider: str, urn: str) -> bool:
    """Check if a provider reference matches a URN.

    Pulumi stores provider refs as ``URN::id``, so we need to match both
    the exact URN and the ``URN::`` prefix.
    """
    return provider == urn or provider.startswith(urn + "::")


def _write_secure(path: Path, data: dict) -> None:
    """Write JSON data to a file with secure permissions (0o600).

    Uses O_CREAT|O_EXCL to fail if the path already exists (prevents
    symlink-following attacks) and sets permissions at creation time
    (no permission window).
    """
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)


def _create_backup(config, op: str, deployment_version, deployment) -> tuple[str, str]:
    """Create a timestamped backup of state for a mutating operation.

    Returns (timestamp, relative_path). ``op`` is the short operation name
    (``rm``, ``mv``, ``push``) used in the filename and rotation prefix.
    Centralizes: symlink guard on backups/, mkdir with 0o700, secure write
    with O_CREAT|O_EXCL, per-op rotation. Replaces three near-identical
    blocks in run_state_rm / run_state_mv / run_state_push.
    """
    backup_dir = config.tlumi_dir / "backups"
    if backup_dir.is_symlink():
        raise WorkspaceError(
            f"backups is a symlink to {backup_dir.resolve()}",
            hint="Remove the symlink. A malicious repository may have created it.",
        )
    try:
        safe_mkdir(backup_dir, mode=0o700)
    except OSError as e:
        raise WorkspaceError(
            f"Cannot create backup directory: {e}",
            hint="Check disk space and directory permissions.",
        ) from e
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    op_prefix = f"state_{op}_"
    backup_path = backup_dir / f"{op_prefix}{timestamp}.json"
    try:
        _write_secure(backup_path, {"version": deployment_version, "deployment": deployment or {}})
    except OSError as e:
        raise WorkspaceError(
            f"Failed to create backup: {e}",
            hint="State was NOT modified. Fix the issue and try again.",
        ) from e
    _rotate_backups(backup_dir, op_prefix=op_prefix)
    return timestamp, f".tlumi/backups/{op_prefix}{timestamp}.json"


def _get_resources(stack) -> list[dict]:
    """Get all non-Stack resources from state."""
    state = safe_export_stack(stack)
    if not state.deployment:
        return []
    resources = state.deployment.get("resources", [])
    return [r for r in resources if isinstance(r, dict) and r.get("type") != "pulumi:pulumi:Stack"]


def run_state_list(json_output: bool = False) -> None:
    """List all resources in state."""
    config = load_config(find_project_dir())

    if not json_output:
        console.print()
    with animated_status("  Loading state...", quiet=json_output):
        stack = get_stack(config, quiet=json_output, runtime=False)

    resources = _get_resources(stack)

    if json_output:
        result = []
        for r in resources:
            result.append(
                {
                    "type": r.get("type", ""),
                    "name": name_from_urn(r.get("urn", "")),
                    "id": r.get("id", ""),
                }
            )
        print_json(json.dumps(result))
        return

    if not resources:
        console.print("  [muted]No resources in state.[/muted]")
        return

    console.print()
    for r in resources:
        rtype = r.get("type", "")
        name = name_from_urn(r.get("urn", ""))
        resource_id = r.get("id", "")

        console.print(
            f"  [bold]{escape(rtype)}[/bold]  [cyan]{escape(name)}[/cyan]", highlight=False
        )
        if resource_id:
            console.print(f"    [dim]ID: {escape(resource_id)}[/dim]")

    console.print(f"\n  [muted]{len(resources)} resource(s) in state.[/muted]")


def run_state_show(resource: str, json_output: bool = False, show_secrets: bool = False) -> None:
    """Show details of a specific resource."""
    from tlumi.sanitize import _sanitize_value, _unwrap_secrets

    config = load_config(find_project_dir())

    if not json_output:
        console.print()
    with animated_status("  Loading state...", quiet=json_output):
        stack = get_stack(config, quiet=json_output, runtime=False)

    resources = _get_resources(stack)
    found = resolve_resource(resources, resource)

    inputs = found.get("inputs", {})
    outputs = found.get("outputs", {})
    if not show_secrets:
        inputs = _sanitize_value(inputs)
        outputs = _sanitize_value(outputs)

    if json_output:
        print_json(
            json.dumps(
                {
                    "urn": found.get("urn", ""),
                    "type": found.get("type", ""),
                    "id": found.get("id", ""),
                    "inputs": inputs,
                    "outputs": outputs,
                }
            )
        )
        return

    # Human-readable rendering: with --show-secrets, decode the exported-state
    # secret wrappers to their plaintext values (the --json envelope above
    # keeps the raw wrappers, faithful to the state-pull format), then hide
    # Pulumi bookkeeping keys ("__*", recursively, matching the plan-diff
    # behavior). _sanitize_value/_unwrap_secrets can collapse a top-level
    # secret wrapper to a scalar, so only dict-shaped values are filtered.
    if show_secrets:
        inputs = _unwrap_secrets(inputs)
        outputs = _unwrap_secrets(outputs)
    if isinstance(inputs, dict):
        inputs = _strip_dunder_keys(inputs)
    if isinstance(outputs, dict):
        outputs = _strip_dunder_keys(outputs)

    console.print()
    console.print(f"  [bold]URN:[/bold]  {escape(found.get('urn', ''))}", highlight=False)
    console.print(f"  [bold]Type:[/bold] {escape(found.get('type', ''))}", highlight=False)
    console.print(f"  [bold]ID:[/bold]   {escape(found.get('id', ''))}", highlight=False)

    if inputs:
        console.print("\n  [bold]Inputs:[/bold]")
        console.print(
            textwrap.indent(json.dumps(inputs, indent=2, default=str), "  "),
            highlight=False,
            markup=False,
        )

    if outputs:
        console.print("\n  [bold]Outputs:[/bold]")
        console.print(
            textwrap.indent(json.dumps(outputs, indent=2, default=str), "  "),
            highlight=False,
            markup=False,
        )


def run_state_rm(resource: str, auto_approve: bool = False) -> None:
    """Remove a resource from state without destroying it."""
    config = load_config(find_project_dir())

    console.print()
    with animated_status("  Loading state..."):
        stack = get_stack(config, runtime=False)

    state = safe_export_stack(stack)
    if not state.deployment:
        raise WorkspaceError("No resources in state.")

    all_resources = state.deployment.get("resources", [])
    non_stack = [r for r in all_resources if r.get("type") != "pulumi:pulumi:Stack"]
    found = resolve_resource(non_stack, resource)

    urn = found.get("urn", "")
    rtype = found.get("type", "")
    name = name_from_urn(urn)

    # Check for resources that depend on or are parented to this resource
    dependents = []
    for r in all_resources:
        if r.get("urn") == urn:
            continue
        dep_label = f"{r.get('type', '')} ({name_from_urn(r.get('urn', ''))})"
        if r.get("parent") == urn:
            dependents.append(f"{dep_label} [parent]")
        elif urn in (r.get("dependencies") or []):
            dependents.append(dep_label)
        elif _provider_matches(r.get("provider") or "", urn):
            dependents.append(f"{dep_label} [provider]")
        elif r.get("deletedWith") == urn:
            dependents.append(f"{dep_label} [deletedWith]")
        else:
            for prop_deps in (r.get("propertyDependencies") or {}).values():
                if urn in (prop_deps or []):
                    dependents.append(f"{dep_label} [propertyDependencies]")
                    break

    print_warning(f"This will remove {rtype} ({name}) from state only.")
    print_warning("The actual cloud resource will NOT be destroyed.")
    if dependents:
        console.print()
        print_warning(f"{len(dependents)} resource(s) depend on this resource:")
        for dep in dependents:
            console.print(f"    [warning]- {escape(dep)}[/warning]")
        print_warning("Removing it may leave dangling references in state.")
    console.print()

    if not auto_approve:
        if not confirm(f"Remove {escape(rtype)} ({escape(name)}) from state?"):
            console.print("\n  [muted]Cancelled.[/muted]")
            return

    # Backup state before modification
    _, backup_rel = _create_backup(config, "rm", state.version, state.deployment)
    console.print(f"  [muted]State backed up to {backup_rel}[/muted]")

    # Walk all resources, remove the target, and clean up five reference types
    # in remaining resources: parent, dependencies, provider, deletedWith, and
    # propertyDependencies. (replaceWith is a registration-only ResourceOptions
    # field, not persisted in state, so it needs no cleanup.) Each resource is
    # shallow-copied on first mutation to avoid modifying the original state data.
    modified_resources = []
    for r in all_resources:
        if r.get("urn") == urn:
            continue
        # Remove dangling parent reference
        if r.get("parent") == urn:
            r = dict(r)  # shallow copy to avoid mutating original
            del r["parent"]
        # Remove from dependencies list
        deps = r.get("dependencies")
        if deps and urn in deps:
            r = dict(r)
            r["dependencies"] = [d for d in deps if d != urn]
        # Remove dangling provider reference (provider URNs may have ::id suffix)
        if _provider_matches(r.get("provider") or "", urn):
            r = dict(r)
            del r["provider"]
        # Remove dangling deletedWith reference (scalar URN, like parent)
        if r.get("deletedWith") == urn:
            r = dict(r)
            del r["deletedWith"]
        # Remove from propertyDependencies
        prop_deps = r.get("propertyDependencies")
        if prop_deps:
            cleaned = {}
            changed = False
            for prop_key, dep_list in prop_deps.items():
                if dep_list and urn in dep_list:
                    filtered = [d for d in dep_list if d != urn]
                    changed = True
                    if filtered:
                        cleaned[prop_key] = filtered
                else:
                    cleaned[prop_key] = dep_list
            if changed:
                r = dict(r)
                if cleaned:
                    r["propertyDependencies"] = cleaned
                else:
                    del r["propertyDependencies"]
        modified_resources.append(r)
    deployment = cast(dict[str, Any], state.deployment)
    deployment["resources"] = modified_resources
    try:
        stack.import_stack(state)
    except (CommandError, OSError) as e:
        raise WorkspaceError(
            f"Failed to update state: {redact_text(str(e))}",
            hint=f"State backup saved to {backup_rel}. "
            f"Restore with 'tlumi state push {backup_rel}'.",
        ) from e

    print_success(f"Removed {rtype} ({name}) from state.")


def run_state_mv(source: str, destination: str, auto_approve: bool = False) -> None:
    """Rename a resource in state."""
    config = load_config(find_project_dir())

    console.print()
    with animated_status("  Loading state..."):
        stack = get_stack(config, runtime=False)

    state = safe_export_stack(stack)
    if not state.deployment:
        raise WorkspaceError("No resources in state.")

    all_resources = state.deployment.get("resources", [])
    non_stack = [r for r in all_resources if r.get("type") != "pulumi:pulumi:Stack"]
    found = resolve_resource(non_stack, source)

    old_urn = found.get("urn", "")
    rtype = found.get("type", "")
    old_name = name_from_urn(old_urn)

    # Construct new URN positionally: the name is everything after the third
    # '::' and may itself contain '::', so split with maxsplit=3 and replace the
    # name field only. Splitting on every '::' would corrupt names like
    # "foo::bar" (renaming "foo::bar" -> "baz" must not yield "foo::baz").
    urn_parts = old_urn.split("::", 3)
    if len(urn_parts) < 4:
        raise WorkspaceError(
            f"Cannot parse URN: {old_urn}",
            hint="State may be corrupted. Run 'tlumi state pull' to inspect.",
        )
    urn_type = urn_parts[2]

    # If destination contains ::, parse as type::name and validate type matches.
    # Split on the FIRST '::' (consistent with resolve_resource): the name part
    # may itself contain '::', so rpartition would mis-parse
    # 'aws:s3:BucketV2::foo::bar' as type 'aws:s3:BucketV2::foo'.
    if "::" in destination:
        dest_type, _, dest_name = destination.partition("::")
        if dest_type != rtype and dest_type != urn_type:
            raise WorkspaceError(
                f"Type mismatch: source is '{rtype}' but destination specifies '{dest_type}'.",
                hint="Omit the type prefix to rename, or ensure types match.",
            )
        new_name = dest_name
    else:
        new_name = destination

    if not new_name:
        raise WorkspaceError("Destination name cannot be empty.")

    urn_parts[3] = new_name
    new_urn = "::".join(urn_parts)

    # No-op rename (source and destination resolve to the same URN). Surface
    # this clearly instead of falling through to the collision loop below,
    # where the source resource would match new_urn and produce a misleading
    # "already exists, choose a different name" error.
    if new_urn == old_urn:
        raise WorkspaceError(
            f"Source and destination are the same: {display_from_urn(old_urn)}.",
            hint="Pick a destination name different from the current one.",
        )

    # Check for URN collision against other resources (skip self defensively;
    # new_urn == old_urn is already rejected above).
    for r in all_resources:
        if r.get("urn") == new_urn and r.get("urn") != old_urn:
            raise WorkspaceError(
                f"A resource with URN '{new_urn}' already exists in state.",
                hint="Choose a different destination name.",
            )

    # A child URN embeds only the parent TYPE chain
    # (urn:pulumi:...::parent_type$child_type::child_name), never the parent's
    # NAME, so renaming a parent does not invalidate its children's URNs: the
    # rewrite loop below repoints each child's `parent` reference to the new URN
    # and the children themselves are untouched. We therefore allow the rename
    # but warn, because a child whose code-side name is DERIVED from the parent
    # name (e.g. f"{parent}-child") will still be replaced on the next apply.
    children = []
    for r in all_resources:
        if r.get("parent") == old_urn and r.get("urn") != old_urn:
            child_name = name_from_urn(r.get("urn", ""))
            children.append(f"{r.get('type', '')} ({child_name})")

    if children:
        labels = "\n    - ".join(children)
        print_warning(
            f"{rtype} ({old_name}) has {len(children)} child resource(s):\n    - {labels}\n"
            "  Their URNs are preserved and re-parented automatically. If a child's name in"
            " your code is derived from this parent's name, it will be replaced on the next apply."
        )

    console.print(
        f"  Renaming [bold]{escape(rtype)}[/bold]"
        f" [cyan]{escape(old_name)}[/cyan]"
        f" -> [cyan]{escape(new_name)}[/cyan]"
    )
    console.print()

    if not auto_approve:
        if not confirm(f"Rename {escape(rtype)} ({escape(old_name)}) to ({escape(new_name)})?"):
            console.print("\n  [muted]Cancelled.[/muted]")
            return

    # Backup state before modification
    _, backup_rel = _create_backup(config, "mv", state.version, state.deployment)
    console.print(f"  [muted]State backed up to {backup_rel}[/muted]")

    # Walk all resources and rewrite URN references
    modified_resources = []
    for r in all_resources:
        mutated = False

        # Rename the target resource's URN
        if r.get("urn") == old_urn:
            r = dict(r)
            r["urn"] = new_urn
            mutated = True

        # Update parent references
        if r.get("parent") == old_urn:
            if not mutated:
                r = dict(r)
                mutated = True
            r["parent"] = new_urn

        # Update dependencies list
        deps = r.get("dependencies")
        if deps and old_urn in deps:
            if not mutated:
                r = dict(r)
                mutated = True
            r["dependencies"] = [new_urn if d == old_urn else d for d in deps]

        # Update provider references
        prov = r.get("provider") or ""
        if _provider_matches(prov, old_urn):
            if not mutated:
                r = dict(r)
                mutated = True
            if prov == old_urn:
                r["provider"] = new_urn
            elif prov.startswith(old_urn + "::"):
                r["provider"] = new_urn + prov[len(old_urn) :]

        # Update deletedWith reference (scalar URN, like parent). deletedWith is
        # a persisted state edge; replaceWith is registration-only (not in
        # state) so it needs no rewrite.
        if r.get("deletedWith") == old_urn:
            if not mutated:
                r = dict(r)
                mutated = True
            r["deletedWith"] = new_urn

        # Update propertyDependencies
        prop_deps = r.get("propertyDependencies")
        if prop_deps:
            updated_prop_deps = {}
            prop_changed = False
            for prop_key, dep_list in prop_deps.items():
                if dep_list and old_urn in dep_list:
                    updated_prop_deps[prop_key] = [new_urn if d == old_urn else d for d in dep_list]
                    prop_changed = True
                else:
                    updated_prop_deps[prop_key] = dep_list
            if prop_changed:
                if not mutated:
                    r = dict(r)
                r["propertyDependencies"] = updated_prop_deps

        modified_resources.append(r)

    deployment = cast(dict[str, Any], state.deployment)
    deployment["resources"] = modified_resources
    try:
        stack.import_stack(state)
    except (CommandError, OSError) as e:
        raise WorkspaceError(
            f"Failed to update state: {redact_text(str(e))}",
            hint=f"State backup saved to {backup_rel}. "
            f"Restore with 'tlumi state push {backup_rel}'.",
        ) from e

    print_success(f"Renamed {rtype} ({old_name}) to ({new_name}).")


def run_state_pull() -> None:
    """Export state as JSON to stdout."""
    config = load_config(find_project_dir())
    stack = get_stack(config, quiet=True, runtime=False)
    state = safe_export_stack(stack)

    print_json(
        json.dumps(
            {
                "version": state.version,
                "deployment": state.deployment or {},
            },
            indent=2,
        )
    )


def run_state_push(file_path: str, auto_approve: bool = False, json_output: bool = False) -> None:
    """Replace current state with contents of a JSON file."""
    # JSON mode requires --auto-approve
    if json_output and not auto_approve:
        raise WorkspaceError(
            "JSON mode requires --auto-approve for state push.",
            hint="Add '--auto-approve' to skip the confirmation prompt.",
        )

    # Read and parse file
    path = Path(file_path)
    if path.is_symlink():
        # Match the symlink-guard pattern used everywhere else in tlumi: a
        # malicious or surprising symlink could redirect a 'state push' to
        # read process env / SSH keys / arbitrary host files, and the error
        # would surface as a confusing JSON-decode failure.
        raise WorkspaceError(
            f"State file is a symlink to {path.resolve()}",
            hint="Pass the resolved path directly, or remove the symlink.",
        )
    if not path.is_absolute():
        # Relative paths are the attacker-controlled surface (a malicious
        # cloned repo can ship a symlinked directory so an in-repo path like
        # 'dir/state.json' redirects the read to arbitrary host files), so
        # every component from the cwd down to the leaf must be a real entry.
        # Absolute paths keep the leaf-only check: they are user-typed, and
        # e.g. /tmp is a symlink on macOS, so rejecting symlinked parents
        # there would break legitimate usage.
        probe = Path(".")
        for part in path.parts:
            probe = probe / part
            if probe.is_symlink():
                raise WorkspaceError(
                    f"State file path contains a symlink component: {probe}",
                    hint="Pass the resolved path directly, or remove the symlink.",
                )
    if not path.exists():
        raise WorkspaceError(
            f"File not found: {file_path}",
            hint="Use 'tlumi state pull' to export a valid state file.",
        )
    if path.is_dir():
        # os.open() succeeds on a directory and the failure would surface from
        # os.fdopen() as "Is a directory: <fd number>" with no path in sight;
        # catch the common mistake (pointing at .tlumi/backups instead of a
        # backup file) up front with a clear message.
        raise WorkspaceError(
            f"Not a file: {file_path} is a directory.",
            hint="Pass the state JSON file itself, "
            "e.g. .tlumi/backups/state_push_<timestamp>.json.",
        )

    try:
        # O_NOFOLLOW closes the TOCTOU window between the is_symlink() checks
        # above and this read: a leaf swapped for a symlink in between fails
        # the open with ELOOP instead of being followed.
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
        try:
            f = os.fdopen(fd, encoding="utf-8")
        except OSError:
            # fdopen failure (e.g. the leaf raced into a directory after the
            # is_dir() check) leaves the raw fd open; close it before the
            # error propagates. fdopen errors name the fd, not the path, so
            # the wrapper message below carries file_path.
            os.close(fd)
            raise
        with f:
            raw = f.read()
    except (OSError, UnicodeDecodeError) as e:
        raise WorkspaceError(
            f"Cannot read file {file_path}: {e}",
            hint="Check file path, permissions, and encoding (must be UTF-8).",
        ) from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise WorkspaceError(
            f"Invalid JSON in {file_path}: {e}",
            hint="Use 'tlumi state pull' to export a valid state file.",
        ) from e

    # Validate structure
    if not isinstance(data, dict):
        raise WorkspaceError(
            f"Invalid state file: expected a JSON object, got {type(data).__name__}.",
            hint="Use 'tlumi state pull' to export a valid state file.",
        )
    if "version" not in data or "deployment" not in data:
        raise WorkspaceError(
            "Invalid state file: missing 'version' or 'deployment' keys.",
            hint="Use 'tlumi state pull' to export a valid state file.",
        )
    if not isinstance(data["version"], int) or isinstance(data["version"], bool):
        raise WorkspaceError(
            "Invalid state file: 'version' must be an integer,"
            f" got {type(data['version']).__name__}.",
            hint="Use 'tlumi state pull' to export a valid state file.",
        )
    if data["version"] < 1:
        # Pulumi has shipped checkpoint versions 1, 2, and 3. A negative or
        # zero version is meaningless and would either crash import_stack or
        # be silently coerced -- reject up front so the error path is clear.
        raise WorkspaceError(
            f"Invalid state file: 'version' must be >= 1, got {data['version']}.",
            hint="Use 'tlumi state pull' to export a valid state file.",
        )
    if not isinstance(data["deployment"], dict):
        raise WorkspaceError(
            "Invalid state file: 'deployment' must be a JSON object,"
            f" got {type(data['deployment']).__name__}.",
            hint="Use 'tlumi state pull' to export a valid state file.",
        )

    # Count non-Stack resources for display
    deploy_resources = data["deployment"].get("resources", [])
    if not isinstance(deploy_resources, list):
        raise WorkspaceError(
            "Invalid state file: 'deployment.resources' must be a list,"
            f" got {type(deploy_resources).__name__}.",
            hint="Use 'tlumi state pull' to export a valid state file.",
        )
    # Validate each resource entry shape and URN format before import_stack so
    # a malformed file fails fast with a clear error rather than leaving Pulumi
    # mid-import in an undefined state. URNs all start with 'urn:pulumi:'; a
    # truncated or hand-edited file that drops the prefix would otherwise pass
    # the previous "non-empty string" check and reach resolve_resource where
    # the failure mode is much less clear.
    declared_urns: set[str] = set()
    for i, r in enumerate(deploy_resources):
        if not isinstance(r, dict):
            raise WorkspaceError(
                f"Invalid state file: resources[{i}] must be an object, got {type(r).__name__}.",
                hint="Use 'tlumi state pull' to export a valid state file.",
            )
        for required in ("urn", "type"):
            if required not in r:
                raise WorkspaceError(
                    f"Invalid state file: resources[{i}] is missing required key '{required}'.",
                    hint="Use 'tlumi state pull' to export a valid state file.",
                )
            if not isinstance(r[required], str) or not r[required]:
                raise WorkspaceError(
                    f"Invalid state file: resources[{i}].{required} must be a non-empty string.",
                    hint="Use 'tlumi state pull' to export a valid state file.",
                )
        urn = r["urn"]
        if not urn.startswith("urn:pulumi:"):
            raise WorkspaceError(
                f"Invalid state file: resources[{i}].urn does not look like a Pulumi URN "
                f"(missing 'urn:pulumi:' prefix): {urn!r}.",
                hint="Use 'tlumi state pull' to export a valid state file.",
            )
        declared_urns.add(urn)

    # Referential integrity: parent/dependencies/provider/propertyDependencies
    # must all point at URNs declared in the same envelope (or be the special
    # Stack URN, which lives in declared_urns when present). A push that
    # carries dangling refs would otherwise import cleanly and only fail on
    # the next plan/apply with an opaque Pulumi-side error.
    for i, r in enumerate(deploy_resources):
        parent = r.get("parent")
        if isinstance(parent, str) and parent and parent not in declared_urns:
            raise WorkspaceError(
                f"Invalid state file: resources[{i}].parent references unknown URN: {parent!r}.",
                hint="The parent must appear elsewhere in deployment.resources.",
            )
        deps = r.get("dependencies") or []
        if isinstance(deps, list):
            for dep in deps:
                if isinstance(dep, str) and dep and dep not in declared_urns:
                    raise WorkspaceError(
                        f"Invalid state file: resources[{i}].dependencies references unknown URN: "
                        f"{dep!r}.",
                        hint="Every dependency must appear in deployment.resources.",
                    )
        provider = r.get("provider")
        if isinstance(provider, str) and provider:
            # Provider refs are "<provider_urn>" or "<provider_urn>::<id>". Match
            # by prefix against declared URNs (like _provider_matches) instead of
            # positional splitting, so a provider whose name contains '::' is not
            # mis-parsed.
            if not any(_provider_matches(provider, u) for u in declared_urns):
                raise WorkspaceError(
                    f"Invalid state file: resources[{i}].provider references unknown URN: "
                    f"{provider!r}.",
                    hint="The provider must appear in deployment.resources.",
                )
        # deletedWith is a persisted state edge (scalar URN) the integrity
        # verifier enforces; replaceWith is registration-only (not persisted)
        # so it is intentionally not validated here.
        deleted_with = r.get("deletedWith")
        if isinstance(deleted_with, str) and deleted_with and deleted_with not in declared_urns:
            raise WorkspaceError(
                f"Invalid state file: resources[{i}].deletedWith references unknown URN: "
                f"{deleted_with!r}.",
                hint="The deletedWith target must appear in deployment.resources.",
            )
        prop_deps = r.get("propertyDependencies") or {}
        if isinstance(prop_deps, dict):
            for prop_key, dep_list in prop_deps.items():
                if not isinstance(dep_list, list):
                    continue
                for dep in dep_list:
                    if isinstance(dep, str) and dep and dep not in declared_urns:
                        raise WorkspaceError(
                            f"Invalid state file: resources[{i}].propertyDependencies"
                            f"[{prop_key!r}] references unknown URN: {dep!r}.",
                            hint="Every dependency must appear in deployment.resources.",
                        )

    resource_count = sum(1 for r in deploy_resources if r.get("type") != "pulumi:pulumi:Stack")

    config = load_config(find_project_dir())

    # Reject a state file from a different project/stack: its URNs embed the
    # source project name, so importing it would orphan every resource and the
    # next plan/apply would delete-all and re-create-all. Pulumi's own
    # import_stack does not check this. tlumi pins the stack to "default".
    for urn in declared_urns:
        head = urn.split("::", 3)
        if len(head) >= 2 and (head[0] != f"urn:pulumi:{DEFAULT_STACK}" or head[1] != config.name):
            raise WorkspaceError(
                f"State file belongs to project '{head[1]}' (stack segment {head[0]!r}); "
                f"it cannot be pushed into project '{config.name}'.",
                hint="Push only a state file exported from this same project, "
                "or rename 'project.name' in tlumi.yaml to match.",
            )

    if not json_output:
        console.print()

    with animated_status("  Loading state...", quiet=json_output):
        stack = get_stack(config, quiet=json_output, runtime=False)  # state push

    # Load current state for diff and backup (single export)
    current_state = safe_export_stack(stack)
    current_urns = set()
    if current_state.deployment:
        for r in current_state.deployment.get("resources", []):
            if isinstance(r, dict) and r.get("type") != "pulumi:pulumi:Stack":
                current_urns.add(r.get("urn", ""))

    new_urns = set()
    for r in deploy_resources:
        if isinstance(r, dict) and r.get("type") != "pulumi:pulumi:Stack":
            new_urns.add(r.get("urn", ""))

    added = new_urns - current_urns
    removed = current_urns - new_urns
    unchanged = current_urns & new_urns

    if not json_output:
        print_warning(f"This will replace the current state with {file_path}.")
        console.print(f"  The new state contains {resource_count} resource(s).")
        console.print()
        if added or removed:
            console.print(
                f"  [green]+{len(added)}[/green] added,"
                f" [red]-{len(removed)}[/red] removed,"
                f" {len(unchanged)} unchanged"
            )
            if added:
                for urn in sorted(added):
                    console.print(f"    [green]+ {escape(display_from_urn(urn))}[/green]")
            if removed:
                for urn in sorted(removed):
                    console.print(f"    [red]- {escape(display_from_urn(urn))}[/red]")
            console.print()
        else:
            console.print("  No resource changes detected.")
            console.print()

    if not auto_approve:
        if not confirm("Replace current state?"):
            console.print("\n  [muted]Cancelled.[/muted]")
            return

    # Backup current state
    _, backup_rel = _create_backup(config, "push", current_state.version, current_state.deployment)

    if not json_output:
        console.print(f"  [muted]State backed up to {backup_rel}[/muted]")

    # Import the new state
    try:
        stack.import_stack(Deployment(version=data["version"], deployment=data["deployment"]))
    except (CommandError, OSError) as e:
        raise WorkspaceError(
            f"Failed to import state: {redact_text(str(e))}",
            hint=f"State backup saved to {backup_rel}. "
            f"Restore with 'tlumi state push {backup_rel}'.",
        ) from e

    if json_output:
        print_json(
            json.dumps(
                {
                    "pushed": True,
                    "resources": resource_count,
                    "backup": backup_rel,
                    "diff": {
                        "added": len(added),
                        "removed": len(removed),
                        "unchanged": len(unchanged),
                    },
                }
            )
        )
    else:
        print_success(f"State replaced successfully ({resource_count} resource(s)).")
