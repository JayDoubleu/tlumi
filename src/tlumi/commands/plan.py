"""tlumi plan - Preview infrastructure changes."""

from __future__ import annotations

import json
from pathlib import Path

from tlumi.commands._common import setup_command
from tlumi.display import (
    extract_changes,
    print_json,
    print_plan_summary,
    print_success,
    print_warning,
)
from tlumi.engine import EventHandler, catch_engine_errors, redirect_program_stdout
from tlumi.errors import TlumiError


def run_plan(
    var: list[str] | None = None,
    var_file: list[str] | None = None,
    target: list[str] | None = None,
    replace: list[str] | None = None,
    destroy: bool = False,
    out: str | None = None,
    json_output: bool = False,
) -> None:
    """Preview changes without applying them."""
    if destroy and replace:
        raise TlumiError(
            "--destroy and --replace cannot be used together.",
            hint=(
                "Use --destroy alone to preview destruction,"
                " or --replace to force resource recreation."
            ),
        )
    if destroy and out:
        raise TlumiError(
            "--destroy and --out cannot be used together.",
            hint="Destroy previews cannot be saved as plan files.",
        )

    banner = "Previewing destruction..." if destroy else "Previewing changes..."
    result_obj = setup_command(
        var=var,
        var_file=var_file,
        target=target,
        replace=replace,
        json_output=json_output,
        banner=banner,
        status_msg="Analyzing infrastructure...",
    )
    stack = result_obj.stack
    resolved_targets = result_obj.resolved_targets
    resolved_replace = result_obj.resolved_replace

    handler = EventHandler(quiet=json_output)

    preview_kwargs: dict = {
        "on_event": handler.on_preview,
        "on_output": lambda _: None,
        "target": resolved_targets,
    }
    if resolved_replace:
        preview_kwargs["replace"] = resolved_replace

    if out:
        preview_kwargs["plan"] = str(Path(out).resolve())

    with catch_engine_errors(handler, "Preview failed."):
        with redirect_program_stdout(json_output):
            # Counts come from the handler's displayed events (change_counts), not
            # the return value; the call runs the preview (and writes the plan file
            # when --out is set) for its side effects.
            if destroy:
                stack.preview_destroy(
                    on_event=handler.on_preview,
                    on_output=lambda _: None,
                    target=resolved_targets,
                )
            else:
                stack.preview(**preview_kwargs)

    if handler.callback_errors and not json_output:
        errs = handler.callback_errors
        print_warning(
            f"{errs} event callback error(s) occurred during preview. Use --verbose for details."
        )

    # Exclude the hidden pulumi:pulumi:Stack resource from the counts so the
    # summary matches the (Stack-filtered) resource listing.
    counts = handler.change_counts()
    create, update, replace_count, delete = extract_changes(counts)
    import_count = counts.get("import", 0)

    if json_output:
        output: dict = {
            "changes": {
                "create": create,
                "update": update,
                "replace": replace_count,
                "delete": delete,
                "import": import_count,
            },
            "outputs_changed": handler.stack_outputs_changed,
        }
        if resolved_targets:
            output["targets"] = resolved_targets
        if resolved_replace:
            output["replace"] = resolved_replace
        if out:
            output["plan_file"] = str(Path(out).resolve())
        if handler.callback_errors:
            output["callback_errors"] = handler.callback_errors
        if handler.warnings:
            output["warnings"] = [
                {"resource": resource, "message": msg} for resource, msg in handler.warnings
            ]
        if handler.property_diffs:
            resource_diffs = {}
            for urn, changes in handler.property_diffs.items():
                resource_diffs[urn] = [
                    {
                        "property": c.path,
                        "action": c.kind,
                        **({"old": c.old_value} if c.old_value is not None else {}),
                        **({"new": c.new_value} if c.new_value is not None else {}),
                        **({"forces_replacement": True} if c.forces_replacement else {}),
                    }
                    for c in changes
                ]
            output["resource_diffs"] = resource_diffs
        print_json(json.dumps(output))
        return

    handler.render_warnings()
    print_plan_summary(
        create=create,
        update=update,
        replace=replace_count,
        delete=delete,
        import_count=import_count,
        outputs_changed=handler.stack_outputs_changed,
    )

    if out:
        print_success(f"Plan saved to {Path(out).resolve()}")
