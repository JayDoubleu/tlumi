"""tlumi deps - Manage Python provider packages."""

from __future__ import annotations

import logging
import os
import re

from packaging.requirements import InvalidRequirement, Requirement

from tlumi._safeio import safe_append_text, safe_write_text
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


def _has_version_constraint(spec: str) -> bool:
    """True if the spec pins a version (has a specifier or a direct URL).

    A bare package name carries no constraint, so re-adding one must NOT
    overwrite an existing pinned line: doing so would silently strip a
    user-authored version pin (and any inline comment) and the unpinned install
    would then upgrade the venv off the pin. Only a spec that actually pins a
    version replaces the stored line.
    """
    try:
        req = Requirement(spec)
        return bool(req.specifier or req.url)
    except InvalidRequirement:
        # Fallback: any comparison operator or a direct '@ url' means a pin.
        return bool(re.search(r"[~!=<>]", spec)) or "@" in spec


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
            f"requirements.txt is a symlink to {os.readlink(req_path)}",
            hint="Remove the symlink before adding packages.",
        )
    try:
        existing_content = req_path.read_text() if req_path.exists() else ""
    except (OSError, UnicodeDecodeError) as e:
        raise WorkspaceError(f"Cannot read requirements.txt: {e}") from e
    existing_lines = existing_content.splitlines()

    # Map each already-listed package (by normalized name) to its line index so a
    # re-add with a NEW version spec updates the pin in place instead of being
    # silently dropped. The install below always runs with the given spec, so a
    # dropped write would leave the venv upgraded while requirements.txt kept the
    # old pin, and the next 'deps install' / fresh clone would revert it.
    existing_index: dict[str, int] = {}
    for i, line in enumerate(existing_lines):
        if line.strip() and not line.startswith("#") and not line.startswith("-"):
            existing_index[_normalize_name(line)] = i

    lines = list(existing_lines)
    new_entries: list[str] = []
    updated_existing = False
    for pkg in packages:
        idx = existing_index.get(_normalize_name(pkg))
        if idx is None:
            new_entries.append(pkg)
        elif lines[idx] != pkg and _has_version_constraint(pkg):
            # Only a spec that pins a version replaces the stored line. A bare
            # name (or any spec without a constraint) leaves the existing pin and
            # inline comment intact instead of clobbering them.
            lines[idx] = pkg
            updated_existing = True

    if updated_existing:
        # A pinned spec changed; append cannot edit an existing line, so rewrite
        # the whole file. This also repairs a missing trailing newline for free.
        content = "\n".join(lines + new_entries)
        if content:
            content += "\n"
        try:
            safe_write_text(req_path, content)
        except OSError as e:
            raise WorkspaceError(f"Failed to write requirements.txt: {e}") from e
    elif new_entries:
        # Append-only path. Prepend a newline when the existing file lacks a
        # trailing one, so the first new entry does not merge into the last line
        # (mirrors init.py's .gitignore handling).
        prefix = "\n" if existing_content and not existing_content.endswith("\n") else ""
        try:
            safe_append_text(req_path, prefix + "".join(f"{entry}\n" for entry in new_entries))
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
