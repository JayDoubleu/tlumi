"""tlumi deps - Manage Python provider packages."""

from __future__ import annotations

import logging
import re

from packaging.requirements import InvalidRequirement, Requirement

from tlumi._safeio import safe_append_text
from tlumi.config import ProjectConfig, find_project_dir, load_config
from tlumi.display import console, install_live, print_banner, print_success
from tlumi.errors import WorkspaceError
from tlumi.uv import ensure_uv
from tlumi.uv import run_install as uv_install
from tlumi.uv import run_list as uv_list

_log = logging.getLogger(__name__)


def _normalize_name(spec: str) -> str:
    """Extract normalized package name from a requirement specifier."""
    try:
        return Requirement(spec).name.lower().replace("-", "_").replace(".", "_")
    except InvalidRequirement:
        _log.warning("Falling back to manual parsing for: %s", spec)
        name = re.split(r"[~!=<>\[;@]", spec)[0].strip().lower().replace("-", "_").replace(".", "_")
        if not name:
            # Both PEP 508 parsing and the regex fallback failed to extract a
            # name. Returning empty would let unrelated weird specs collide as
            # the same package in the existing_packages set, silently dropping
            # legitimate adds.
            raise WorkspaceError(
                f"Invalid package specifier: '{spec}'",
                hint="Provide a valid PEP 508 requirement, e.g. 'pulumi-aws' or 'pulumi-aws>=6.0'.",
            )
        return name


def _ensure_venv(config: ProjectConfig) -> None:
    if not config.venv_python.exists():
        raise WorkspaceError(
            "Virtual environment not found.",
            hint="Run 'tlumi init' to set up the project.",
        )


def run_deps_add(packages: list[str]) -> None:
    """Add provider packages and install them."""
    # Guard against argument injection: a leading dash would be parsed as a
    # pip flag (e.g. "--index-url=evil"), and embedded newlines would forge
    # multiple requirements.txt entries. uv runs untrusted package names,
    # so these checks must happen before anything is written or executed.
    for pkg in packages:
        # Strip before the dash check: uv/pip strip leading whitespace from a
        # requirements line, so " -r /etc/passwd" (leading space) would otherwise
        # slip past a bare startswith("-") and be honored as a pip directive.
        if pkg.strip().startswith("-"):
            raise WorkspaceError(
                f"Invalid package specifier: '{pkg}'",
                hint="Package names must not start with '-'. "
                "Use a requirements file for advanced pip options.",
            )
        # str.splitlines() (pip's line splitter) treats eight more code points
        # as line boundaries beyond \n/\r (VT, FF, FS, GS, RS, NEL, LINE and
        # PARAGRAPH SEPARATOR); any of them could forge a second
        # requirements.txt directive. Reject anything that splits into more than
        # one line, plus an explicit NUL (not a splitlines boundary).
        if "\x00" in pkg or (pkg and pkg.splitlines() != [pkg]):
            raise WorkspaceError(
                "Invalid package specifier: contains control characters.",
            )

    config = load_config(find_project_dir())
    _ensure_venv(config)
    print_banner("Installing packages...")

    # Add to requirements.txt first, then install
    req_path = config.project_dir / "requirements.txt"
    if req_path.is_symlink():
        raise WorkspaceError(
            f"requirements.txt is a symlink to {req_path.resolve()}",
            hint="Remove the symlink before adding packages.",
        )
    try:
        existing_lines = req_path.read_text().splitlines() if req_path.exists() else []
    except (OSError, UnicodeDecodeError) as e:
        raise WorkspaceError(f"Cannot read requirements.txt: {e}") from e
    existing_packages = {
        _normalize_name(line)
        for line in existing_lines
        if line.strip() and not line.startswith("#") and not line.startswith("-")
    }

    new_entries = []
    for pkg in packages:
        pkg_name = _normalize_name(pkg)
        if pkg_name not in existing_packages:
            new_entries.append(pkg)

    if new_entries:
        try:
            safe_append_text(req_path, "".join(f"{entry}\n" for entry in new_entries))
        except OSError as e:
            raise WorkspaceError(f"Failed to write requirements.txt: {e}") from e

    uv = ensure_uv()
    with install_live("  Installing packages...") as add_line:
        uv_install(uv, config.venv_dir, [*packages], add_line=add_line)

    for pkg in packages:
        print_success(f"  Installed {pkg}")


def run_deps_install() -> None:
    """Install all packages from requirements.txt."""
    config = load_config(find_project_dir())
    _ensure_venv(config)
    req_path = config.project_dir / "requirements.txt"

    if not req_path.exists():
        raise WorkspaceError("No requirements.txt found.", hint="Run 'tlumi init' first.")

    print_banner("Installing dependencies from requirements.txt...")

    uv = ensure_uv()
    with install_live("  Installing dependencies...") as add_line:
        uv_install(uv, config.venv_dir, ["-r", str(req_path)], add_line=add_line)

    print_success("All dependencies installed.")


def run_deps_list() -> None:
    """List installed packages in the project venv."""
    config = load_config(find_project_dir())
    _ensure_venv(config)

    uv = ensure_uv()
    result = uv_list(uv, config.venv_dir)
    console.print()
    console.print(result.stdout, highlight=False)
