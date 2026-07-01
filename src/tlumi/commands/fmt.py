"""tlumi fmt - Format project Python files using ruff."""

from __future__ import annotations

import subprocess  # nosec B404

from rich.markup import escape

from tlumi.config import find_project_dir
from tlumi.display import console, print_banner, print_success
from tlumi.errors import TlumiError
from tlumi.uv import ensure_uv


def run_fmt(*, check: bool = False) -> None:
    """Format Python files in the project directory using ruff."""
    project_dir = find_project_dir()

    print_banner("Formatting..." if not check else "Checking format...")
    console.print()

    uv = ensure_uv()
    cmd = [
        str(uv),
        "tool",
        "run",
        "ruff",
        "format",
        "--exclude",
        ".tlumi",
    ]
    if check:
        cmd.append("--check")
    cmd.append(str(project_dir))

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)  # nosec B603
    except OSError as e:
        raise TlumiError(
            f"Failed to run ruff: {e}",
            hint="Check that uv is working correctly.",
        ) from e

    if result.returncode == 0:
        if check:
            print_success("All files are formatted correctly.")
        else:
            print_success("Formatting complete.")
    elif check and result.returncode == 1:
        # --check mode: files need formatting
        if result.stdout.strip():
            for line in result.stdout.strip().splitlines():
                console.print(f"  [muted]{escape(line)}[/muted]")
            console.print()
        raise TlumiError(
            "Some files need formatting.",
            hint="Run 'tlumi fmt' to fix.",
        )
    else:
        # ruff's real error reports often span many lines (file path, caret,
        # hint). Show the full output before raising so the user can fix the
        # underlying problem instead of just seeing the first line.
        full = (result.stderr.strip() or result.stdout.strip() or "").rstrip()
        if full:
            for line in full.splitlines():
                console.print(f"  [muted]{escape(line)}[/muted]")
            console.print()
        first_line = full.splitlines()[0] if full else "ruff exited with an error"
        if result.returncode == 2:
            hint = "ruff received invalid arguments or encountered an internal error."
        else:
            hint = "Check that your Python files have valid syntax."
        raise TlumiError(f"Formatting failed: {first_line}", hint=hint)
