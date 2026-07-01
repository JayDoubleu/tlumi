"""tlumi validate - Check config and syntax without running Pulumi."""

from __future__ import annotations

import json

from tlumi.config import find_project_dir, load_config, merge_variables
from tlumi.display import console, print_banner, print_json, print_success, print_warning
from tlumi.errors import ConfigError


def run_validate(
    var: list[str] | None = None,
    var_file: list[str] | None = None,
    json_output: bool = False,
) -> None:
    """Validate project configuration and entry file syntax."""
    config = load_config(find_project_dir())
    config = merge_variables(config, var=var, var_file=var_file, quiet=json_output)

    checks: list[str] = []

    if not json_output:
        print_banner("Validating project...")
        console.print()

    # Check 1: tlumi.yaml loaded successfully (already done by load_config)
    checks.append("tlumi.yaml")
    if not json_output:
        print_success("tlumi.yaml is valid")

    # Check 2: Entry file exists
    entry_path = config.entry_path
    if not entry_path.exists():
        raise ConfigError(
            f"Entry file not found: {entry_path}",
            hint=f"Create '{config.entry}' or update 'project.entry' in tlumi.yaml.",
        )
    checks.append(f"{config.entry} exists")
    if not json_output:
        print_success(f"{config.entry} exists")

    # Check 3: Syntax-check the entry file without executing it
    try:
        source = entry_path.read_text()
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(
            f"Cannot read {config.entry}: {e}",
            hint="Check file permissions and encoding.",
        ) from e
    try:
        compile(source, str(entry_path), "exec")
    except SyntaxError as e:
        raise ConfigError(
            f"Syntax error in {config.entry}: {e.msg} (line {e.lineno})",
        ) from None
    checks.append(f"{config.entry} syntax")
    if not json_output:
        print_success(f"{config.entry} syntax is valid")

    # Check 4: requirements.txt exists (warning, not error)
    req_path = config.project_dir / "requirements.txt"
    if req_path.exists():
        checks.append("requirements.txt")
        if not json_output:
            print_success("requirements.txt exists")
    else:
        if not json_output:
            print_warning("requirements.txt not found (optional)")

    if json_output:
        print_json(json.dumps({"valid": True, "checks": checks}))
        return

    console.print("\n  [success]Validation passed.[/success]")
