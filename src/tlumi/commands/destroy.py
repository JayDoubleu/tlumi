"""tlumi destroy - Tear down all infrastructure."""

from __future__ import annotations

import json
import time

from rich.markup import escape

from tlumi.config import find_project_dir, load_config, merge_variables
from tlumi.display import (
    animated_status,
    confirm,
    console,
    format_engine_result,
    print_apply_summary,
    print_banner,
    print_json,
    print_targets,
    print_warning,
)
from tlumi.engine import EventHandler, catch_engine_errors, redirect_program_stdout
from tlumi.errors import TlumiError
from tlumi.resolve import name_from_urn, resolve_targets
from tlumi.workspace import get_stack, safe_export_stack


def run_destroy(
    auto_approve: bool = False,
    var: list[str] | None = None,
    var_file: list[str] | None = None,
    target: list[str] | None = None,
    json_output: bool = False,
) -> None:
    """Destroy all managed infrastructure."""
    if json_output and not auto_approve:
        raise TlumiError(
            "--auto-approve is required in JSON mode for destructive operations.",
            hint="Add --auto-approve to confirm.",
        )

    config = load_config(find_project_dir())
    config = merge_variables(config, var=var, var_file=var_file, quiet=json_output)

    if not json_output:
        print_banner("Destroying infrastructure...")
        console.print()

    with animated_status("  Loading state...", quiet=json_output):
        stack = get_stack(config, quiet=json_output)

    # Load state for display and target resolution (single read)
    state = safe_export_stack(stack)
    raw_resources: list[dict] = []
    if state.deployment:
        raw_resources = [
            r
            for r in state.deployment.get("resources", [])
            if isinstance(r, dict) and r.get("type") != "pulumi:pulumi:Stack"
        ]

    resolved_targets = None
    if target:
        resolved_targets = resolve_targets(stack, target, resources=raw_resources)
        if not json_output:
            print_targets(resolved_targets)

    if not raw_resources:
        if json_output:
            print_json(
                json.dumps(
                    {
                        "changes": {"create": 0, "update": 0, "replace": 0, "delete": 0},
                        "duration": "0s",
                    }
                )
            )
            return
        console.print("  [muted]No resources to destroy.[/muted]")
        return

    # Filter display to targeted resources when --target is active
    if resolved_targets:
        target_set = set(resolved_targets)
        display_resources = [
            (r.get("type", ""), name_from_urn(r.get("urn", "")))
            for r in raw_resources
            if r.get("urn", "") in target_set
        ]
    else:
        display_resources = [
            (r.get("type", ""), name_from_urn(r.get("urn", ""))) for r in raw_resources
        ]

    if not json_output:
        for rtype, name in display_resources:
            console.print(
                f"  [bold red]-[/bold red] [bold]{escape(rtype)}[/bold]"
                f"  [cyan]{escape(name)}[/cyan]"
                f"  will be [bold red]destroyed[/bold red]",
                highlight=False,
            )

        console.print(f"\n  Plan: [delete]{len(display_resources)} to destroy[/delete].")
        console.print()

    if not auto_approve and not json_output:
        if not confirm("Are you sure you want to destroy all resources?"):
            console.print("\n  [muted]Destroy cancelled.[/muted]")
            return

    if not json_output:
        console.print()
    start = time.time()
    handler = EventHandler(quiet=json_output)

    with catch_engine_errors(handler, "Destroy failed."):
        with redirect_program_stdout(json_output):
            with handler.start_live(quiet=json_output):
                stack.destroy(
                    on_event=handler.on_update,
                    on_output=lambda _: None,
                    target=resolved_targets,
                )

    if handler.callback_errors and not json_output:
        errs = handler.callback_errors
        print_warning(
            f"{errs} event callback error(s) occurred during destroy. Use --verbose for details."
        )

    elapsed = time.time() - start
    duration = f"{elapsed:.0f}s"

    resource_changes: dict[str, int] = handler.change_counts()

    if json_output:
        print_json(
            json.dumps(
                format_engine_result(
                    resource_changes,
                    duration,
                    targets=resolved_targets,
                    callback_errors=handler.callback_errors,
                    warnings=handler.warnings,
                )
            )
        )
        return

    handler.render_warnings()
    print_apply_summary(
        delete=resource_changes.get("delete", 0),
        duration=duration,
    )
