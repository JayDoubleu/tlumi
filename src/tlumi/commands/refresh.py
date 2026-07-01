"""tlumi refresh - Reconcile state with cloud reality."""

from __future__ import annotations

import json
import time

from tlumi.commands._common import setup_command
from tlumi.display import (
    console,
    format_engine_result,
    print_apply_summary,
    print_json,
    print_warning,
)
from tlumi.engine import EventHandler, catch_engine_errors, redirect_program_stdout


def run_refresh(
    var: list[str] | None = None,
    var_file: list[str] | None = None,
    target: list[str] | None = None,
    json_output: bool = False,
) -> None:
    """Refresh state to match cloud reality."""
    result_obj = setup_command(
        var=var,
        var_file=var_file,
        target=target,
        json_output=json_output,
        banner="Refreshing state...",
        status_msg="Loading state...",
    )
    stack = result_obj.stack
    resolved_targets = result_obj.resolved_targets

    start = time.time()
    handler = EventHandler(quiet=json_output)

    with catch_engine_errors(handler, "Refresh failed."):
        with redirect_program_stdout(json_output):
            with handler.start_live(quiet=json_output):
                stack.refresh(
                    on_event=handler.on_update,
                    on_output=lambda _: None,
                    target=resolved_targets,
                )

    if handler.callback_errors and not json_output:
        errs = handler.callback_errors
        print_warning(
            f"{errs} event callback error(s) occurred during refresh. Use --verbose for details."
        )

    elapsed = time.time() - start
    duration = f"{elapsed:.0f}s"

    resource_changes: dict[str, int] = handler.change_counts()

    create = resource_changes.get("create", 0)
    update = resource_changes.get("update", 0)
    replace = resource_changes.get("replace", 0)
    delete = resource_changes.get("delete", 0)

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
    if any([create, update, replace, delete]):
        print_apply_summary(
            create=create,
            update=update,
            replace=replace,
            delete=delete,
            duration=duration,
        )
    else:
        console.print("  [muted]State is up-to-date.[/muted]")
        console.print()
        console.print(f"  [timing]Duration: {duration}[/timing]")
