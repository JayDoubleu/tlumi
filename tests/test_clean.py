"""Tests for tlumi.commands.clean: _safe_rmtree and backup preservation."""

from __future__ import annotations

import logging
from unittest.mock import patch

from tlumi.commands.clean import _safe_rmtree, run_clean


def test_safe_rmtree_removes_regular_tree(tmp_path):
    """_safe_rmtree removes a regular directory tree."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "file.txt").write_text("data")
    (target / "sub").mkdir()
    (target / "sub" / "nested.txt").write_text("nested")

    _safe_rmtree(target)
    assert not target.exists()


def test_safe_rmtree_unlinks_symlinks(tmp_path):
    """_safe_rmtree unlinks symlinks without following them."""
    # Create an external directory that should NOT be deleted
    external = tmp_path / "external"
    external.mkdir()
    (external / "important.txt").write_text("keep me")

    # Create the target tree with a symlink pointing to external
    target = tmp_path / "target"
    target.mkdir()
    (target / "file.txt").write_text("data")
    (target / "link_to_external").symlink_to(external)

    _safe_rmtree(target)

    # Target is removed
    assert not target.exists()
    # External directory is untouched
    assert external.exists()
    assert (external / "important.txt").read_text() == "keep me"


def test_safe_rmtree_handles_symlink_to_file(tmp_path):
    """_safe_rmtree handles symlinks to files."""
    external_file = tmp_path / "external.txt"
    external_file.write_text("external data")

    target = tmp_path / "target"
    target.mkdir()
    (target / "link").symlink_to(external_file)

    _safe_rmtree(target)

    assert not target.exists()
    assert external_file.exists()


def test_safe_rmtree_root_symlink_to_dir(tmp_path):
    """Root path being a symlink to a directory is unlinked, target untouched."""
    external = tmp_path / "external"
    external.mkdir()
    (external / "important.txt").write_text("keep me")

    symlink_root = tmp_path / "symlink_root"
    symlink_root.symlink_to(external)

    _safe_rmtree(symlink_root)

    # Symlink is removed
    assert not symlink_root.exists()
    # External directory is untouched
    assert external.exists()
    assert (external / "important.txt").read_text() == "keep me"


def test_safe_rmtree_root_symlink_to_file(tmp_path):
    """Root path being a symlink to a file is unlinked, file untouched."""
    external_file = tmp_path / "external.txt"
    external_file.write_text("keep me")

    symlink_root = tmp_path / "symlink_root"
    symlink_root.symlink_to(external_file)

    _safe_rmtree(symlink_root)

    assert not symlink_root.exists()
    assert external_file.exists()
    assert external_file.read_text() == "keep me"


# --- backup preservation tests ---


@patch("tlumi.commands.clean.find_project_dir")
def test_clean_preserves_backups_when_state_empty(mock_find, tmp_path, capsys):
    """clean without --include-state preserves backups even when state/ is empty."""
    mock_find.return_value = tmp_path

    tlumi_dir = tmp_path / ".tlumi"
    tlumi_dir.mkdir()
    # Empty state dir
    (tlumi_dir / "state").mkdir()
    # Backups dir with content
    backups_dir = tlumi_dir / "backups"
    backups_dir.mkdir()
    (backups_dir / "state_rm_20260101.json").write_text("{}")
    # Removable cache dir
    (tlumi_dir / "cache").mkdir()
    (tlumi_dir / "cache" / "venv").mkdir()

    run_clean(auto_approve=True, include_state=False)

    # Backups should be preserved
    assert backups_dir.exists()
    assert (backups_dir / "state_rm_20260101.json").exists()
    # Cache should be removed
    assert not (tlumi_dir / "cache").exists()


@patch("tlumi.commands.clean.find_project_dir")
def test_clean_selective_preserves_state_and_removes_cache(mock_find, tmp_path):
    """clean without --include-state removes cache dirs but preserves state/ and backups/."""
    mock_find.return_value = tmp_path

    tlumi_dir = tmp_path / ".tlumi"
    tlumi_dir.mkdir()
    # State dir with content
    state_dir = tlumi_dir / "state"
    state_dir.mkdir()
    (state_dir / "default.json").write_text("{}")
    # Backups dir with content
    backups_dir = tlumi_dir / "backups"
    backups_dir.mkdir()
    (backups_dir / "backup.json").write_text("{}")
    # Removable dirs
    cache_dir = tlumi_dir / "cache"
    cache_dir.mkdir()
    (cache_dir / "venv").mkdir()
    (cache_dir / "pulumi_home").mkdir()

    run_clean(auto_approve=True, include_state=False)

    assert state_dir.exists()
    assert backups_dir.exists()
    assert not cache_dir.exists()


@patch("tlumi.commands.clean.find_project_dir")
def test_clean_selective_nothing_to_remove(mock_find, tmp_path, capsys):
    """clean shows 'nothing to clean' when only state and backups remain."""
    mock_find.return_value = tmp_path

    tlumi_dir = tmp_path / ".tlumi"
    tlumi_dir.mkdir()
    (tlumi_dir / "state").mkdir()
    (tlumi_dir / "state" / "default.json").write_text("{}")

    run_clean(auto_approve=True, include_state=False)

    output = capsys.readouterr().out
    assert "Nothing to clean" in output


@patch("tlumi.commands.clean.find_project_dir")
def test_clean_full_removes_everything(mock_find, tmp_path):
    """clean --include-state removes the entire .tlumi/ directory."""
    mock_find.return_value = tmp_path

    tlumi_dir = tmp_path / ".tlumi"
    tlumi_dir.mkdir()
    (tlumi_dir / "state").mkdir()
    (tlumi_dir / "state" / "default.json").write_text("{}")
    (tlumi_dir / "cache").mkdir()

    run_clean(auto_approve=True, include_state=True)

    assert not tlumi_dir.exists()


@patch("tlumi.commands.clean.find_project_dir")
def test_clean_nothing_exists(mock_find, tmp_path, capsys):
    """clean when .tlumi/ does not exist says nothing to clean."""
    mock_find.return_value = tmp_path

    run_clean(auto_approve=True, include_state=False)

    output = capsys.readouterr().out
    assert "Nothing to clean" in output


@patch("tlumi.commands.clean.find_project_dir")
def test_clean_unlinks_symlinked_tlumi_dir(mock_find, tmp_path):
    """.tlumi as symlink is unlinked without touching target."""
    mock_find.return_value = tmp_path

    external = tmp_path / "external"
    external.mkdir()
    (external / "data.txt").write_text("keep")

    symlink = tmp_path / ".tlumi"
    symlink.symlink_to(external)

    run_clean(auto_approve=True)

    assert not symlink.exists()
    assert external.exists()
    assert (external / "data.txt").read_text() == "keep"


@patch("tlumi.commands.clean.find_project_dir")
def test_clean_full_no_state(mock_find, tmp_path):
    """clean removes .tlumi/ when there is no state (no --include-state needed)."""
    mock_find.return_value = tmp_path

    tlumi_dir = tmp_path / ".tlumi"
    tlumi_dir.mkdir()
    (tlumi_dir / "cache").mkdir()
    (tlumi_dir / "cache" / "venv").mkdir()

    run_clean(auto_approve=True, include_state=False)

    assert not tlumi_dir.exists()


# ---------------------------------------------------------------------------
# R9-E3: clean warns on unreadable state
# ---------------------------------------------------------------------------


@patch("tlumi.commands.clean.find_project_dir")
def test_clean_warns_on_unreadable_state(mock_find, tmp_path, capsys):
    """clean warns when state directory check fails with OSError."""
    mock_find.return_value = tmp_path

    tlumi_dir = tmp_path / ".tlumi"
    tlumi_dir.mkdir()
    state_dir = tlumi_dir / "state"
    state_dir.mkdir()
    (tlumi_dir / "cache").mkdir()

    # Make state_dir unreadable to trigger OSError on iterdir
    state_dir.chmod(0o000)
    try:
        run_clean(auto_approve=True, include_state=False)
    finally:
        state_dir.chmod(0o755)

    output = capsys.readouterr().out
    assert "Cannot check state directory" in output or "preserving state" in output


@patch("tlumi.commands.clean.find_project_dir")
def test_clean_removes_loose_files(mock_find, tmp_path):
    """clean removes loose files in .tlumi/ during selective clean."""
    mock_find.return_value = tmp_path

    tlumi_dir = tmp_path / ".tlumi"
    tlumi_dir.mkdir()
    (tlumi_dir / "state").mkdir()
    (tlumi_dir / "state" / "data.json").write_text("{}")
    (tlumi_dir / "Pulumi.yaml").write_text("")
    (tlumi_dir / "cache").mkdir()

    run_clean(auto_approve=True, include_state=False)

    assert (tlumi_dir / "state").exists()
    assert not (tlumi_dir / "Pulumi.yaml").exists()
    assert not (tlumi_dir / "cache").exists()


# ---------------------------------------------------------------------------
# Symlink unlinks during clean are silent (debug log only): venv trees
# legitimately contain symlinks (bin/python, lib64), so per-file warnings
# drowned every routine clean in false alarms.
# ---------------------------------------------------------------------------


def test_safe_rmtree_venv_symlinks_are_silent(tmp_path, capsys, caplog):
    """Expected venv-internal symlinks unlink without console warnings."""
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    real_python = tmp_path / "python3.13"
    real_python.write_text("#!/bin/sh")
    (venv / "bin" / "python").symlink_to(real_python)
    (venv / "lib").mkdir()
    (venv / "lib64").symlink_to(venv / "lib")

    with caplog.at_level(logging.DEBUG, logger="tlumi.commands.clean"):
        _safe_rmtree(venv)

    assert not venv.exists()
    assert real_python.exists()
    captured = capsys.readouterr()
    assert "Unlinking symlink" not in captured.out
    assert "Unlinking symlink" not in captured.err
    # Still observable with --verbose via the debug log
    assert any("Unlinking symlink" in r.getMessage() for r in caplog.records)


@patch("tlumi.commands.clean.find_project_dir")
def test_clean_prints_no_symlink_warnings_for_venv(mock_find, tmp_path, capsys):
    """tlumi clean on a normal project emits no symlink warnings for the venv."""
    mock_find.return_value = tmp_path

    tlumi_dir = tmp_path / ".tlumi"
    venv_bin = tlumi_dir / "cache" / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    real_python = tmp_path / "python3.13"
    real_python.write_text("")
    (venv_bin / "python").symlink_to(real_python)
    (venv_bin / "python3").symlink_to(venv_bin / "python")

    run_clean(auto_approve=True, include_state=False)

    output = capsys.readouterr().out
    assert "Unlinking symlink" not in output
    assert not (tlumi_dir / "cache").exists()
    assert real_python.exists()


@patch("tlumi.commands.clean.find_project_dir")
def test_clean_symlinked_tlumi_dir_still_warns_loudly(mock_find, tmp_path, capsys):
    """The suspicious top-level '.tlumi is a symlink' warning stays loud."""
    mock_find.return_value = tmp_path

    external = tmp_path / "external"
    external.mkdir()
    (tmp_path / ".tlumi").symlink_to(external)

    run_clean(auto_approve=True)

    output = capsys.readouterr().out
    assert ".tlumi is a symlink" in output
    assert external.exists()
