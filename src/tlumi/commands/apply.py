"""tlumi apply - Apply infrastructure changes."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from tlumi.commands._common import setup_command
from tlumi.display import (
    confirm,
    console,
    extract_changes,
    format_engine_result,
    masked_outputs_from_state,
    print_apply_summary,
    print_json,
    print_outputs,
    print_plan_summary,
    print_success,
    print_warning,
)
from tlumi.engine import (
    SECRET_OUTPUTS_BLIND_HINT,
    EventHandler,
    catch_engine_errors,
    redirect_program_stdout,
)
from tlumi.errors import TlumiError
from tlumi.workspace import safe_export_stack

if TYPE_CHECKING:
    from pulumi.automation import Stack, UpResult


def _display_outputs(stack: Stack, result: UpResult, show_secrets: bool) -> dict[str, object]:
    """Collect ``{name: value}`` outputs for display after an up.

    Default-masked mode ignores the values in ``result.outputs`` entirely:
    the OutputValue.secret flag only marks whole-output secrets (a composite
    output with a nested secret reports secret=False with fully decrypted
    plaintext), so masked values are sourced from exported state where
    Pulumi's secret wrappers survive for sanitization to mask.
    """
    if not result.outputs:
        return {}
    if show_secrets:
        return {k: v.value for k, v in result.outputs.items()}
    return masked_outputs_from_state(safe_export_stack(stack))


def run_apply(
    auto_approve: bool = False,
    var: list[str] | None = None,
    var_file: list[str] | None = None,
    target: list[str] | None = None,
    replace: list[str] | None = None,
    plan_file: str | None = None,
    json_output: bool = False,
    show_secrets: bool = False,
) -> None:
    """Apply infrastructure changes."""
    if json_output and not auto_approve:
        raise TlumiError(
            "--auto-approve is required in JSON mode for destructive operations.",
            hint="Add --auto-approve to confirm.",
        )

    # --plan captures all configuration; reject conflicting options
    if plan_file:
        conflicts = []
        if var:
            conflicts.append("--var")
        if var_file:
            conflicts.append("--var-file")
        if target:
            conflicts.append("--target")
        if replace:
            conflicts.append("--replace")
        env_var_keys = sorted(k for k in os.environ if k.startswith("TLUMI_VAR_"))
        if env_var_keys:
            conflicts.append(f"TLUMI_VAR_* ({', '.join(env_var_keys)})")
        if conflicts:
            raise TlumiError(
                f"--plan cannot be combined with {', '.join(conflicts)}.",
                hint=(
                    "The plan file captures all configuration (variables, targets, "
                    "replacements) from the original plan. Unset the conflicting flags "
                    "and any TLUMI_VAR_* environment variables before applying a plan."
                ),
            )

    result_obj = setup_command(
        var=var,
        var_file=var_file,
        target=target,
        replace=replace,
        json_output=json_output,
        banner="Applying changes...",
        status_msg="Analyzing infrastructure...",
        # A saved plan carries the config it was generated against; reconciling
        # would remove those plan-time variables before up(plan=...). Preserve them.
        reconcile_config=not plan_file,
    )
    stack = result_obj.stack
    resolved_targets = result_obj.resolved_targets
    resolved_replace = result_obj.resolved_replace

    # Build shared kwargs for both preview and up calls
    target_kwargs: dict = {}
    if resolved_targets:
        target_kwargs["target"] = resolved_targets
    if resolved_replace:
        target_kwargs["replace"] = resolved_replace

    # When applying a saved plan, skip interactive preview (the plan IS the reviewed preview)
    if plan_file:
        resolved_plan = str(Path(plan_file).resolve())

        start = time.time()
        apply_handler = EventHandler(quiet=json_output)

        with catch_engine_errors(apply_handler, "Apply failed."):
            with redirect_program_stdout(json_output):
                with apply_handler.start_live(quiet=json_output):
                    result = stack.up(
                        on_event=apply_handler.on_update,
                        on_output=lambda _: None,
                        plan=resolved_plan,
                    )

        if apply_handler.callback_errors and not json_output:
            errs = apply_handler.callback_errors
            print_warning(
                f"{errs} event callback error(s) occurred during apply. Use --verbose for details."
            )

        elapsed = time.time() - start
        duration = f"{elapsed:.0f}s"

        resource_changes: dict[str, int] = apply_handler.change_counts()

        output_dict = _display_outputs(stack, result, show_secrets)

        if json_output:
            json_result = format_engine_result(
                resource_changes,
                duration,
                outputs=output_dict,
                callback_errors=apply_handler.callback_errors,
                warnings=apply_handler.warnings,
            )
            json_result["plan_file"] = resolved_plan
            print_json(json.dumps(json_result))
            return

        apply_handler.render_warnings()
        print_apply_summary(
            create=resource_changes.get("create", 0),
            update=resource_changes.get("update", 0),
            replace=resource_changes.get("replace", 0),
            delete=resource_changes.get("delete", 0),
            duration=duration,
            import_count=resource_changes.get("import", 0),
        )
        print_success(f"Applied plan from {plan_file}")

        if output_dict:
            print_outputs(output_dict)
        return

    # When interactive (no --auto-approve, no --json), preview first for confirmation
    if not auto_approve and not json_output:
        preview_handler = EventHandler()

        preview_kwargs: dict = {
            "on_event": preview_handler.on_preview,
            "on_output": lambda _: None,
            **target_kwargs,
        }

        with catch_engine_errors(preview_handler, "Preview failed."):
            stack.preview(**preview_kwargs)

        if preview_handler.callback_errors:
            errs = preview_handler.callback_errors
            print_warning(
                f"{errs} event callback error(s) occurred"
                " during preview. Use --verbose for details."
            )

        # Warnings are rendered after the actual apply (below), not here, to
        # avoid showing the same provider/program warnings twice in the
        # interactive flow (once after preview, once after apply).
        counts = preview_handler.change_counts()
        create, update, replace_count, delete = extract_changes(counts)
        import_count = counts.get("import", 0)
        # Displayed op counts alone cannot see output-only changes (Pulumi
        # previews an edited pulumi.export() as SAME everywhere); gate on the
        # Stack outputs signal too, or the apply would be skipped entirely.
        outputs_changed = preview_handler.stack_outputs_changed

        if not any([create, update, replace_count, delete, import_count]) and not outputs_changed:
            console.print("  [muted]No changes. Infrastructure is up-to-date.[/muted]")
            if preview_handler.stack_outputs_contain_secrets:
                # Preview scrubs secret output values to identical wrappers on
                # both sides, so a changed secret export is invisible here; do
                # not flatly assert up-to-date when the comparison was blind.
                console.print(f"  [muted]{SECRET_OUTPUTS_BLIND_HINT}[/muted]")
            return

        print_plan_summary(
            create=create,
            update=update,
            replace=replace_count,
            delete=delete,
            import_count=import_count,
            outputs_changed=outputs_changed,
        )
        console.print()

        if not confirm("Do you want to apply these changes?"):
            console.print("\n  [muted]Apply cancelled.[/muted]")
            return

        console.print()

    # Apply with structured event tracking and live rolling log
    start = time.time()
    apply_handler = EventHandler(quiet=json_output)

    with catch_engine_errors(apply_handler, "Apply failed."):
        with redirect_program_stdout(json_output):
            with apply_handler.start_live(quiet=json_output):
                result = stack.up(
                    on_event=apply_handler.on_update,
                    on_output=lambda _: None,
                    **target_kwargs,
                )

    if apply_handler.callback_errors and not json_output:
        errs = apply_handler.callback_errors
        print_warning(
            f"{errs} event callback error(s) occurred during apply. Use --verbose for details."
        )

    elapsed = time.time() - start
    duration = f"{elapsed:.0f}s"

    resource_changes = apply_handler.change_counts()

    output_dict = _display_outputs(stack, result, show_secrets)

    if json_output:
        json_result = format_engine_result(
            resource_changes,
            duration,
            targets=resolved_targets,
            outputs=output_dict,
            callback_errors=apply_handler.callback_errors,
            warnings=apply_handler.warnings,
        )
        if resolved_replace:
            json_result["replace"] = resolved_replace
        print_json(json.dumps(json_result))
        return

    apply_handler.render_warnings()
    print_apply_summary(
        create=resource_changes.get("create", 0),
        update=resource_changes.get("update", 0),
        replace=resource_changes.get("replace", 0),
        delete=resource_changes.get("delete", 0),
        duration=duration,
        import_count=resource_changes.get("import", 0),
    )

    if output_dict:
        print_outputs(output_dict)
