"""tlumi show - Display full state summary."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

from rich.markup import escape

from tlumi.config import find_project_dir, load_config
from tlumi.diffs import _strip_dunder_keys
from tlumi.display import animated_status, console, print_json
from tlumi.resolve import name_from_urn, type_from_urn
from tlumi.sanitize import _sanitize_value, _unwrap_secrets
from tlumi.workspace import get_stack, safe_export_stack


def run_show(json_output: bool = False, show_secrets: bool = False) -> None:
    """Display all resources in state with their details."""
    config = load_config(find_project_dir())

    if not json_output:
        console.print()
    with animated_status("  Loading state...", quiet=json_output):
        stack = get_stack(config, quiet=json_output, runtime=False)

    state = safe_export_stack(stack)

    # JSON mode: emit raw state (same format as state pull)
    if json_output:
        deployment = state.deployment or {}
        if not show_secrets:
            deployment = cast(Mapping[str, Any], _sanitize_value(deployment))
        print_json(
            json.dumps(
                {
                    "version": state.version,
                    "deployment": deployment,
                },
                indent=2,
            )
        )
        return

    if not state.deployment:
        console.print("  [muted]No resources in state.[/muted]")
        return

    all_resources = state.deployment.get("resources", [])
    resources = [
        r for r in all_resources if isinstance(r, dict) and r.get("type") != "pulumi:pulumi:Stack"
    ]

    if not resources:
        console.print("  [muted]No resources in state.[/muted]")
        return

    console.print("  Current state:")

    for r in resources:
        urn = r.get("urn", "")
        rtype = type_from_urn(urn)
        name = name_from_urn(urn)
        resource_id = r.get("id", "")
        parent = r.get("parent")
        deps = r.get("dependencies", [])
        inputs = r.get("inputs", {})
        outputs = r.get("outputs", {})

        if show_secrets:
            # Human display decodes the exported-state secret wrappers to
            # their plaintext values; `show --json --show-secrets` keeps the
            # raw envelope (faithful to the state-pull format).
            inputs = _unwrap_secrets(inputs)
            outputs = _unwrap_secrets(outputs)
        else:
            inputs = _sanitize_value(inputs)
            outputs = _sanitize_value(outputs)
        # Hide Pulumi bookkeeping keys ("__*", recursively, matching plan
        # diffs). _sanitize_value/_unwrap_secrets can collapse a top-level
        # secret wrapper to a scalar, so only dict-shaped values are filtered
        # (and only dicts reach the .items() rendering below).
        if isinstance(inputs, dict):
            inputs = _strip_dunder_keys(inputs)
        if isinstance(outputs, dict):
            outputs = _strip_dunder_keys(outputs)

        console.print()
        console.print(
            f"  [bold]{escape(rtype)}[/bold]  [cyan]{escape(name)}[/cyan]", highlight=False
        )
        console.print(f"    URN:    {urn}", highlight=False, markup=False)
        if resource_id:
            console.print(f"    ID:     {resource_id}", highlight=False, markup=False)
        if parent:
            console.print(f"    Parent: {parent}", highlight=False, markup=False)
        if deps:
            console.print("    Depends on:")
            for dep in deps:
                console.print(f"      - {dep}", highlight=False, markup=False)
        if isinstance(inputs, dict) and inputs:
            console.print("    Inputs:")
            for k, v in sorted(inputs.items()):
                console.print(
                    f"      {k}  = {json.dumps(v, default=str)}", highlight=False, markup=False
                )
        if isinstance(outputs, dict) and outputs:
            console.print("    Outputs:")
            for k, v in sorted(outputs.items()):
                console.print(
                    f"      {k}  = {json.dumps(v, default=str)}", highlight=False, markup=False
                )

    console.print(f"\n  [muted]{len(resources)} resource(s) in state.[/muted]")
