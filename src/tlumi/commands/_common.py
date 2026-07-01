"""Common command setup helpers to reduce boilerplate across commands."""

from __future__ import annotations

from dataclasses import dataclass

from pulumi import automation as auto

from tlumi.config import ProjectConfig, find_project_dir, load_config, merge_variables
from tlumi.display import (
    animated_status,
    console,
    print_banner,
    print_replace_targets,
    print_targets,
)
from tlumi.resolve import resolve_targets
from tlumi.workspace import get_stack


@dataclass(frozen=True, slots=True)
class SetupResult:
    """Result from setup_command() with config, stack, and resolved identifiers."""

    config: ProjectConfig
    stack: auto.Stack
    resolved_targets: list[str] | None
    resolved_replace: list[str] | None


def setup_command(
    var: list[str] | None = None,
    var_file: list[str] | None = None,
    target: list[str] | None = None,
    replace: list[str] | None = None,
    json_output: bool = False,
    banner: str | None = None,
    status_msg: str = "Analyzing infrastructure...",
) -> SetupResult:
    """Consolidate the repeated config/stack/target setup across commands.

    Returns SetupResult; access fields by name. Tuple-unpacking was previously
    supported but silently dropped resolved_replace, so callers must use
    attribute access.
    """
    config = load_config(find_project_dir())
    config = merge_variables(config, var=var, var_file=var_file, quiet=json_output)

    if not json_output and banner:
        print_banner(banner)
        console.print()

    with animated_status(f"  {status_msg}", quiet=json_output):
        stack = get_stack(config, quiet=json_output)

    resolved = None
    if target:
        resolved = resolve_targets(stack, target)
        if not json_output:
            print_targets(resolved)

    resolved_rep = None
    if replace:
        resolved_rep = resolve_targets(stack, replace)
        if not json_output:
            print_replace_targets(resolved_rep)

    return SetupResult(config, stack, resolved, resolved_rep)
