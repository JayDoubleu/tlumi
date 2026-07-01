"""Tests for tlumi._safeio: symlink-safe file/dir primitives."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from tlumi._safeio import safe_append_text, safe_chmod_dir, safe_mkdir, safe_write_text


def test_safe_write_text_creates_new_file(tmp_path: Path) -> None:
    """safe_write_text creates a new file with the given content."""
    p = tmp_path / "out.txt"
    safe_write_text(p, "hello")
    assert p.read_text() == "hello"


def test_safe_write_text_overwrites_existing(tmp_path: Path) -> None:
    """safe_write_text replaces existing file contents."""
    p = tmp_path / "out.txt"
    p.write_text("old")
    safe_write_text(p, "new")
    assert p.read_text() == "new"


def test_safe_write_text_refuses_symlink(tmp_path: Path) -> None:
    """safe_write_text fails with OSError when path is a symlink (O_NOFOLLOW)."""
    target = tmp_path / "target.txt"
    target.write_text("orig")
    link = tmp_path / "link.txt"
    link.symlink_to(target)

    with pytest.raises(OSError):
        safe_write_text(link, "should-not-reach-target")
    # Target must remain unchanged
    assert target.read_text() == "orig"


def test_safe_write_text_sets_mode(tmp_path: Path) -> None:
    """safe_write_text applies the requested mode at creation."""
    p = tmp_path / "secret.txt"
    safe_write_text(p, "x", mode=0o600)
    actual_mode = p.stat().st_mode & 0o777
    # umask may clear some bits; check that no extra perms were granted
    assert actual_mode & ~0o600 == 0


def test_safe_append_text_appends(tmp_path: Path) -> None:
    """safe_append_text appends to existing file."""
    p = tmp_path / "log.txt"
    p.write_text("line1\n")
    safe_append_text(p, "line2\n")
    assert p.read_text() == "line1\nline2\n"


def test_safe_append_text_creates_if_missing(tmp_path: Path) -> None:
    """safe_append_text creates file if it doesn't exist."""
    p = tmp_path / "log.txt"
    safe_append_text(p, "first\n")
    assert p.read_text() == "first\n"


def test_safe_append_text_refuses_symlink(tmp_path: Path) -> None:
    """safe_append_text fails when path is a symlink."""
    target = tmp_path / "target.txt"
    target.write_text("orig\n")
    link = tmp_path / "link.txt"
    link.symlink_to(target)

    with pytest.raises(OSError):
        safe_append_text(link, "leak\n")
    assert target.read_text() == "orig\n"


def test_safe_mkdir_creates_directory(tmp_path: Path) -> None:
    """safe_mkdir creates a new directory."""
    p = tmp_path / "newdir"
    safe_mkdir(p)
    assert p.is_dir()


def test_safe_mkdir_existing_dir_ok_by_default(tmp_path: Path) -> None:
    """safe_mkdir tolerates pre-existing real directory."""
    p = tmp_path / "newdir"
    p.mkdir()
    # Should not raise
    safe_mkdir(p)
    assert p.is_dir()


def test_safe_mkdir_refuses_symlink_to_dir(tmp_path: Path) -> None:
    """safe_mkdir raises OSError when the target exists as a symlink to a dir."""
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)

    with pytest.raises(OSError, match="symlink"):
        safe_mkdir(link)


def test_safe_mkdir_refuses_existing_file(tmp_path: Path) -> None:
    """safe_mkdir raises OSError when the path exists as a regular file."""
    p = tmp_path / "file.txt"
    p.write_text("x")

    with pytest.raises(OSError, match="not a directory"):
        safe_mkdir(p)


def test_safe_mkdir_exist_ok_false_raises(tmp_path: Path) -> None:
    """safe_mkdir with exist_ok=False raises FileExistsError when dir exists."""
    p = tmp_path / "dir"
    p.mkdir()
    with pytest.raises(FileExistsError):
        safe_mkdir(p, exist_ok=False)


def test_safe_mkdir_applies_mode(tmp_path: Path) -> None:
    """safe_mkdir applies the requested permission mode."""
    p = tmp_path / "secure"
    safe_mkdir(p, mode=0o700)
    actual_mode = stat.S_IMODE(os.lstat(p).st_mode)
    # umask may clear bits; check no extra perms
    assert actual_mode & ~0o700 == 0


def test_safe_chmod_dir_sets_mode(tmp_path: Path) -> None:
    """safe_chmod_dir changes a directory's permission mode (F23)."""
    d = tmp_path / "d"
    d.mkdir(mode=0o755)
    safe_chmod_dir(d, 0o700)
    assert stat.S_IMODE(os.lstat(d).st_mode) == 0o700


def test_safe_chmod_dir_refuses_symlink(tmp_path: Path) -> None:
    """safe_chmod_dir refuses to chmod through a symlink, leaving the target untouched (F23)."""
    target = tmp_path / "target"
    target.mkdir(mode=0o755)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(OSError):
        safe_chmod_dir(link, 0o700)
    assert stat.S_IMODE(os.lstat(target).st_mode) == 0o755
