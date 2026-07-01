"""Tests for tlumi.commands.fmt: format and check modes."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tlumi.commands.fmt import run_fmt
from tlumi.errors import TlumiError


@patch("tlumi.commands.fmt.ensure_uv", return_value=Path("/usr/bin/uv"))
@patch("tlumi.commands.fmt.find_project_dir")
def test_fmt_success(mock_find, mock_uv, tmp_path, capsys):
    """fmt completes successfully when ruff exits 0."""
    mock_find.return_value = tmp_path

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        run_fmt()

    output = capsys.readouterr().out
    assert "Formatting complete" in output


@patch("tlumi.commands.fmt.ensure_uv", return_value=Path("/usr/bin/uv"))
@patch("tlumi.commands.fmt.find_project_dir")
def test_fmt_check_success(mock_find, mock_uv, tmp_path, capsys):
    """fmt --check succeeds when all files are formatted."""
    mock_find.return_value = tmp_path

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        run_fmt(check=True)

    output = capsys.readouterr().out
    assert "formatted correctly" in output


@patch("tlumi.commands.fmt.ensure_uv", return_value=Path("/usr/bin/uv"))
@patch("tlumi.commands.fmt.find_project_dir")
def test_fmt_check_needs_formatting(mock_find, mock_uv, tmp_path):
    """fmt --check exits non-zero when files need formatting."""
    mock_find.return_value = tmp_path

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="Would reformat infra.py\n",
            stderr="",
        )
        with pytest.raises(TlumiError, match="need formatting"):
            run_fmt(check=True)


@patch("tlumi.commands.fmt.ensure_uv", return_value=Path("/usr/bin/uv"))
@patch("tlumi.commands.fmt.find_project_dir")
def test_fmt_ruff_error(mock_find, mock_uv, tmp_path):
    """fmt raises TlumiError when ruff exits with code 2."""
    mock_find.return_value = tmp_path

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=2,
            stdout="",
            stderr="error: invalid argument",
        )
        with pytest.raises(TlumiError, match="Formatting failed"):
            run_fmt()


@patch("tlumi.commands.fmt.ensure_uv", return_value=Path("/usr/bin/uv"))
@patch("tlumi.commands.fmt.find_project_dir")
def test_fmt_oserror(mock_find, mock_uv, tmp_path):
    """fmt raises TlumiError when subprocess.run raises OSError."""
    mock_find.return_value = tmp_path

    with patch("subprocess.run", side_effect=OSError("command not found")):
        with pytest.raises(TlumiError, match="Failed to run ruff"):
            run_fmt()
