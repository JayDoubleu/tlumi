"""tlumi clean - Remove .tlumi/ directory to start fresh."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from rich.markup import escape

from tlumi.config import TLUMI_DIR, find_project_dir
from tlumi.display import confirm, console, print_banner, print_success, print_warning
from tlumi.errors import TlumiError

_log = logging.getLogger(__name__)


def _safe_rmtree(path: Path) -> None:
    """Remove a directory tree, unlinking symlinks instead of following them.

    Uses os.scandir() for efficient directory traversal. Symlinks are
    detected and unlinked at every level of the tree, not just the root.
    Unlinks are logged at debug only (visible with --verbose): venv trees
    legitimately contain symlinks (bin/python, lib64, ...), so per-file
    warnings would drown every routine clean in false alarms. The loud
    warning is reserved for the suspicious top-level ".tlumi is a symlink"
    case handled in run_clean().
    """
    if path.is_symlink():
        _log.debug("Unlinking symlink: %s -> %s", path, path.resolve())
        os.unlink(path)
        return
    with os.scandir(path) as entries:
        for entry in entries:
            if entry.is_symlink():
                _log.debug("Unlinking symlink: %s -> %s", entry.path, Path(entry.path).resolve())
                os.unlink(entry.path)
            elif entry.is_dir(follow_symlinks=False):
                _safe_rmtree(Path(entry.path))
            else:
                os.unlink(entry.path)
    path.rmdir()


def run_clean(auto_approve: bool = False, include_state: bool = False) -> None:
    """Remove .tlumi/ caches, optionally including state and backups."""
    project_dir = find_project_dir()
    tlumi_dir = project_dir / TLUMI_DIR

    if tlumi_dir.is_symlink():
        target = tlumi_dir.resolve()
        print_warning(
            f".tlumi is a symlink to {target}. Unlinking it (target will NOT be deleted)."
        )
        os.unlink(tlumi_dir)
        return

    if not tlumi_dir.exists():
        console.print()
        print_warning("Nothing to clean.")
        return

    # Check for state directory or backups directory with content
    state_dir = tlumi_dir / "state"
    backups_dir = tlumi_dir / "backups"
    try:
        has_state = (state_dir.exists() and any(state_dir.iterdir())) or (
            backups_dir.exists() and any(backups_dir.iterdir())
        )
    except OSError as e:
        print_warning(f"Cannot check state directory ({e}); preserving state as a precaution.")
        has_state = True

    print_banner("Clean")
    console.print()

    if has_state and not include_state:
        # Clean everything except state/ and backups/
        preserve = {"state", "backups"}
        try:
            removable = sorted(
                p for p in tlumi_dir.iterdir() if p.is_dir() and p.name not in preserve
            )
            removable_files = [p for p in tlumi_dir.iterdir() if p.is_file()]
        except OSError as e:
            raise TlumiError(
                f"Cannot list .tlumi/ contents: {e}",
                hint="Check directory permissions and try again.",
            ) from e

        if not removable and not removable_files:
            print_warning("Nothing to clean (only state and backups remain).")
            console.print("  [muted]Use --include-state to remove everything.[/muted]")
            return

        console.print(f"  Will remove from {escape(str(tlumi_dir))}/:")
        for p in removable:
            console.print(f"    {escape(p.name)}/")
        for p in removable_files:
            console.print(f"    {escape(p.name)}")
        console.print()
        console.print(
            "  [muted]Preserving: state/, backups/ (use --include-state to remove)[/muted]"
        )
        console.print()

        if not auto_approve:
            if not confirm("Proceed with removal?"):
                console.print("  [muted]Cancelled.[/muted]")
                return

        errors: list[str] = []
        for p in removable:
            try:
                _safe_rmtree(p)
            except OSError as e:
                errors.append(f"{p.name}: {e}")
        for p in removable_files:
            try:
                p.unlink()
            except OSError as e:
                errors.append(f"{p.name}: {e}")
        if errors:
            raise TlumiError(
                f"Failed to remove some items: {'; '.join(errors)}",
                hint="Check file permissions and try again.",
            )
        console.print()
        print_success("Cleaned caches from .tlumi/ directory.")
        console.print()
        console.print("  [muted]Hint: Run 'tlumi init' to reinitialize caches.[/muted]")
        return

    # Full removal (including state if --include-state)
    try:
        subdirs = sorted(p.name for p in tlumi_dir.iterdir() if p.is_dir())
    except OSError as e:
        raise TlumiError(
            f"Cannot list .tlumi/ contents: {e}",
            hint="Check directory permissions and try again.",
        ) from e
    if subdirs:
        console.print(f"  Will remove {escape(str(tlumi_dir))}/ containing:")
        for name in subdirs:
            console.print(f"    {escape(name)}/")
    else:
        console.print(f"  Will remove {escape(str(tlumi_dir))}/")

    console.print()

    if has_state and include_state:
        print_warning(
            "WARNING: This will permanently delete your infrastructure state and backups."
        )
        console.print()

    if not auto_approve:
        if not confirm("Proceed with removal?"):
            console.print("  [muted]Cancelled.[/muted]")
            return

    try:
        _safe_rmtree(tlumi_dir)
    except OSError as e:
        raise TlumiError(
            f"Failed to remove .tlumi/: {e}",
            hint="Check file permissions and try again.",
        ) from e
    console.print()
    print_success("Cleaned .tlumi/ directory.")
    console.print()
    console.print("  [muted]Hint: Run 'tlumi init' to reinitialize.[/muted]")
