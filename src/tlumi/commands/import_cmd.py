"""tlumi import - Import existing cloud resources into state."""

from __future__ import annotations

from rich.markup import escape

from tlumi.config import find_project_dir, load_config, merge_variables
from tlumi.display import animated_status, console, print_banner, print_success
from tlumi.engine import EventHandler, catch_engine_errors
from tlumi.errors import Diagnostic, EngineError
from tlumi.workspace import get_stack


def _suggest_class(resource_type: str) -> str:
    """Extract a likely class name from a Pulumi type token.

    e.g. "aws:s3:BucketV2" -> "BucketV2"
    """
    return resource_type.split(":")[-1]


def _parse_diagnostics_block(lines: list[str]) -> list[Diagnostic]:
    """Parse a Pulumi CLI ``Diagnostics:`` section into Diagnostic entries.

    The section has a fixed shape: resource headers indented 2 spaces and
    ending with ``:``, message lines indented 4 or more, groups separated by
    blank lines, and the block terminated by the next top-level section
    (``Resources:``, ``Outputs:``) or the CommandResult envelope (indent < 2).
    """
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "Diagnostics:")
    except StopIteration:
        return []

    diagnostics: list[Diagnostic] = []
    resource = ""
    message_lines: list[str] = []

    def flush() -> None:
        nonlocal message_lines
        if message_lines:
            diagnostics.append(Diagnostic(resource, "\n".join(message_lines)))
        message_lines = []

    for line in lines[start + 1 :]:
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip())
        if indent < 2:
            break  # next top-level section or the CommandResult envelope
        if indent == 2 and stripped.endswith(":"):
            flush()
            resource = stripped[:-1]
        else:
            message_lines.append(stripped)
    flush()
    return diagnostics


def _extract_diagnostics(full_output: str) -> list[Diagnostic]:
    """Recover structured diagnostics from raw Pulumi CLI output.

    ``stack.import_resources()`` has no ``on_event`` callback, so unlike
    plan/apply the EventHandler never collects diagnostics and a failed
    import would otherwise surface only a bare "Import failed.". The full
    CLI output (already passed through redact_text by catch_engine_errors)
    contains the standard Pulumi ``Diagnostics:`` section; parse it back
    into Diagnostic entries so the failure renders with the same detail as
    plan/apply. Falls back to explicit ``error:`` lines when the CLI failed
    before the engine emitted a Diagnostics section.
    """
    lines = full_output.splitlines()
    diagnostics = _parse_diagnostics_block(lines)
    if diagnostics:
        return diagnostics

    seen: set[str] = set()
    fallback: list[Diagnostic] = []
    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith("error:") and stripped not in seen:
            seen.add(stripped)
            fallback.append(Diagnostic("", stripped))
    return fallback


def run_import(
    resource_type: str,
    name: str,
    resource_id: str,
    var: list[str] | None = None,
    var_file: list[str] | None = None,
    protect: bool = False,
) -> None:
    """Import an existing cloud resource into state.

    ``protect`` defaults to False (matching Terraform import semantics):
    imported resources are NOT marked protected in state, so a subsequent
    ``tlumi destroy`` works without manual state surgery. Pass ``protect=True``
    to opt back into Pulumi's default protection behavior.
    """
    config = load_config(find_project_dir())
    config = merge_variables(config, var=var, var_file=var_file)

    print_banner(f'Importing {resource_type} "{name}"...')
    console.print()

    with animated_status("  Loading workspace..."):
        stack = get_stack(config)

    handler = EventHandler()

    # import_resources() has no on_event callback, so handler.errors stays
    # empty; recover diagnostics from the raw CLI output instead so a failed
    # import reports its cause inline like plan/apply do.
    try:
        with catch_engine_errors(handler, "Import failed."):
            stack.import_resources(
                resources=[{"type": resource_type, "name": name, "id": resource_id}],
                protect=protect,
                on_output=lambda _: None,
            )
    except EngineError as e:
        if e.diagnostics:
            raise
        raise EngineError(
            e.message,
            diagnostics=_extract_diagnostics(e.full_output),
            full_output=e.full_output,
            hint=e.hint,
        ) from None

    print_success(f"Imported {resource_type} ({name}).")
    if protect:
        console.print(
            "  [muted]Protection enabled: 'tlumi destroy' will refuse this resource"
            " until an apply clears the protect flag.[/muted]"
        )
    console.print()

    class_name = _suggest_class(resource_type)
    console.print("  [muted]Next steps:[/muted]")
    console.print(f"  [muted]  1. Add the resource to {escape(config.entry)}:[/muted]")
    console.print()
    console.print(f'    [cyan]{escape(class_name)}("{escape(name)}", ...)[/cyan]')
    console.print()
    console.print("  [muted]  2. Run 'tlumi plan' to verify no changes are needed.[/muted]")
