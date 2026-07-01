"""tlumi output - Display stack output values."""

from __future__ import annotations

import json
import sys

from pulumi.automation import CommandError

from tlumi.config import find_project_dir, load_config
from tlumi.display import (
    animated_status,
    console,
    masked_outputs_from_state,
    print_json,
    print_outputs,
)
from tlumi.errors import TlumiError, WorkspaceError
from tlumi.redact import redact_text
from tlumi.workspace import get_stack, safe_export_stack


def run_output(
    name: str | None = None,
    json_output: bool = False,
    raw: bool = False,
    show_secrets: bool = False,
) -> None:
    """Display output values from the current state."""
    # --raw is a single-value shell-capture mode; reject combinations that would
    # silently ignore it (matches the mutual-exclusion style in plan/apply).
    if raw and name is None:
        raise TlumiError(
            "--raw requires an output NAME.",
            hint="e.g. tlumi output my_output --raw",
        )
    if raw and json_output:
        raise TlumiError(
            "--raw and --json cannot be combined.",
            hint="Choose one machine-readable format.",
        )

    config = load_config(find_project_dir())

    suppress = json_output or raw
    with animated_status("  Loading outputs...", quiet=suppress):
        # output reads stack outputs from state and never invokes infra.py;
        # use runtime=False so a broken entry file or missing venv does not
        # block reading already-applied outputs. Matches the ADR-007 contract
        # for state-only commands.
        stack = get_stack(config, quiet=suppress, runtime=False)
        if show_secrets:
            try:
                outputs = stack.outputs()
            except CommandError as e:
                raise WorkspaceError(
                    f"Failed to read outputs: {redact_text(str(e))}",
                    hint="Run 'tlumi state unlock' if the state is locked.",
                ) from e
            output_values: dict[str, object] = {k: v.value for k, v in outputs.items()}
        else:
            # Do NOT use stack.outputs() here: its secret flag only marks
            # whole-output secrets, so a composite output with a nested secret
            # arrives as decrypted plaintext with secret=False and nothing for
            # sanitization to catch. Exported state keeps the secret wrappers.
            output_values = masked_outputs_from_state(safe_export_stack(stack))

    # A requested-but-missing output must always be an error (exit 1), even when
    # the stack has no outputs at all -- otherwise a script lookup gets a false
    # success exactly when something is wrong (apply skipped or partial).
    if name is not None:
        if name not in output_values:
            if output_values:
                hint = f"Available outputs: {', '.join(sorted(output_values.keys()))}"
            else:
                hint = "The stack has no outputs. Run 'tlumi apply' first."
            raise WorkspaceError(f"Output '{name}' not found.", hint=hint)

        val = output_values[name]
        if json_output:
            print_json(json.dumps({name: val}, indent=2))
        elif raw:
            # Bypass Rich entirely: when stdout is a pipe/file Rich would
            # hard-wrap at width 80, injecting newlines into the captured value.
            # Composite values are emitted as compact JSON: str() would print
            # Python repr (single quotes), which is neither JSON nor
            # shell-consumable. Scalars stay str() (a plain string must not
            # gain JSON quotes in shell capture).
            if isinstance(val, (dict, list)):
                rendered = json.dumps(val, separators=(",", ":"))
            else:
                rendered = str(val)
            sys.stdout.write(rendered + "\n")
        else:
            console.print(f"  {name} = {val!r}", highlight=False, markup=False)
        return

    if not output_values:
        if json_output:
            print_json(json.dumps({}))
        else:
            console.print("  [muted]No outputs defined.[/muted]")
        return

    if json_output:
        print_json(json.dumps(output_values, indent=2))
    else:
        print_outputs(output_values)
