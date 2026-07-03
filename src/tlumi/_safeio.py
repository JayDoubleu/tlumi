"""Symlink-safe file and directory primitives.

Wraps ``os.open`` with ``O_NOFOLLOW`` to close the TOCTOU window between
an explicit ``Path.is_symlink()`` check and a subsequent ``write_text`` /
``mkdir``. Callers should still call ``is_symlink()`` first to produce
clear error messages; these helpers are the defense-in-depth layer that
catches the race if an attacker swaps the path between check and write.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


def safe_write_text(path: Path, content: str, mode: int = 0o644) -> None:
    """Write ``content`` to ``path``, refusing to follow symlinks.

    Uses ``O_NOFOLLOW``: ``open()`` fails with ``OSError(ELOOP)`` if the
    final component is a symlink. ``O_TRUNC`` overwrites existing content.
    """
    fd = os.open(
        str(path),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
        mode,
    )
    with os.fdopen(fd, "w") as f:
        f.write(content)


def safe_append_text(path: Path, content: str, mode: int = 0o644) -> None:
    """Append ``content`` to ``path``, refusing to follow symlinks."""
    fd = os.open(
        str(path),
        os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
        mode,
    )
    with os.fdopen(fd, "a") as f:
        f.write(content)


def safe_chmod_dir(path: Path, mode: int) -> None:
    """chmod a directory without following a symlink in the final component.

    ``Path.chmod`` follows symlinks, so an attacker who swaps the path for a
    symlink between creation and chmod could redirect the mode change to the
    target. Opening with ``O_NOFOLLOW | O_DIRECTORY`` (a symlink raises
    ``OSError(ELOOP)``, a non-directory raises ``ENOTDIR``) and fchmod-ing the
    fd closes that TOCTOU window. Linux does not support
    ``os.chmod(follow_symlinks=False)``, so the fd dance is the portable path.
    """
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    try:
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def safe_mkdir(path: Path, mode: int = 0o700, exist_ok: bool = True) -> None:
    """Create ``path`` as a directory. Refuse if it exists as a symlink.

    ``os.mkdir`` is atomic and never follows symlinks for the leaf
    component, so a successful creation is safe. If the leaf already
    exists, verify via ``lstat`` that it's a real directory (not a
    symlink) before treating ``exist_ok=True`` as success.
    """
    try:
        os.mkdir(path, mode=mode)
        return
    except FileExistsError:
        if not exist_ok:
            raise
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        raise OSError(f"{path} exists as a symlink")
    if not stat.S_ISDIR(st.st_mode):
        raise OSError(f"{path} exists but is not a directory")
