"""Tests for tlumi.commands.cancel: state unlock."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pulumi.automation import CommandError, Stack

from tlumi.commands.cancel import _has_local_lock, run_cancel
from tlumi.errors import WorkspaceError


@patch("tlumi.commands.cancel.get_stack")
@patch("tlumi.commands.cancel.load_config")
@patch("tlumi.commands.cancel.find_project_dir")
def test_cancel_no_active_lock(mock_find, mock_config, mock_get_stack, capsys):
    """cancel prints message when the service backend reports no active update."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.cancel.side_effect = CommandError("no update is currently running")
    mock_get_stack.return_value = mock_stack

    run_cancel()

    output = capsys.readouterr().out
    assert "No active lock" in output


@patch("tlumi.commands.cancel.get_stack")
@patch("tlumi.commands.cancel.load_config")
@patch("tlumi.commands.cancel.find_project_dir")
def test_cancel_other_command_error_raises(mock_find, mock_config, mock_get_stack):
    """cancel raises WorkspaceError for non-lock-related errors."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.cancel.side_effect = CommandError("unexpected internal error")
    mock_get_stack.return_value = mock_stack

    with pytest.raises(WorkspaceError, match="Unlock failed"):
        run_cancel()


@patch("tlumi.commands.cancel._has_local_lock", return_value=True)
@patch("tlumi.commands.cancel.get_stack")
@patch("tlumi.commands.cancel.load_config")
@patch("tlumi.commands.cancel.find_project_dir")
def test_cancel_success_when_lock_present(
    mock_find, mock_config, mock_get_stack, mock_lock, capsys
):
    """cancel reports 'released' when a lock actually existed (default backend)."""
    mock_config.return_value.backend.url = None  # default file backend
    mock_stack = MagicMock(spec=Stack)
    mock_stack.cancel.return_value = None
    mock_get_stack.return_value = mock_stack

    run_cancel()

    output = capsys.readouterr().out
    assert "released" in output.lower()


@patch("tlumi.commands.cancel._has_local_lock", return_value=False)
@patch("tlumi.commands.cancel.get_stack")
@patch("tlumi.commands.cancel.load_config")
@patch("tlumi.commands.cancel.find_project_dir")
def test_cancel_file_backend_no_lock(mock_find, mock_config, mock_get_stack, mock_lock, capsys):
    """On the default file backend with no lock present, cancel does not claim release."""
    mock_config.return_value.backend.url = None  # default file backend
    mock_stack = MagicMock(spec=Stack)
    mock_stack.cancel.return_value = None
    mock_get_stack.return_value = mock_stack

    run_cancel()

    output = capsys.readouterr().out
    assert "No active lock" in output
    assert "released" not in output.lower()


@patch("tlumi.commands.cancel._has_local_lock", return_value=False)
@patch("tlumi.commands.cancel.get_stack")
@patch("tlumi.commands.cancel.load_config")
@patch("tlumi.commands.cancel.find_project_dir")
def test_cancel_custom_backend_reports_released(
    mock_find, mock_config, mock_get_stack, mock_lock, capsys
):
    """A custom backend.url does not consult the default lock dir; report optimistically."""
    mock_config.return_value.backend.url = "s3://my-bucket"
    mock_stack = MagicMock(spec=Stack)
    mock_stack.cancel.return_value = None
    mock_get_stack.return_value = mock_stack

    run_cancel()

    output = capsys.readouterr().out
    # _has_local_lock would say False, but it is not consulted for a custom backend.
    assert "released" in output.lower()


def test_has_local_lock_detects_lock_files(tmp_path):
    """_has_local_lock is True only when a lock file exists under .pulumi/locks/."""
    config = MagicMock()
    config.state_dir = tmp_path / "state"

    # No locks dir at all.
    assert _has_local_lock(config) is False

    # Empty locks dir (stale empty directories must not count).
    locks = config.state_dir / ".pulumi" / "locks" / "organization" / "proj" / "default"
    locks.mkdir(parents=True)
    assert _has_local_lock(config) is False

    # A real lock file present.
    (locks / "update-123.json").write_text("{}")
    assert _has_local_lock(config) is True
