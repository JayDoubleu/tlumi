"""Tests for tlumi.commands.validate: config and syntax validation."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tlumi.commands.validate import run_validate
from tlumi.errors import ConfigError


def _setup_project(tmp_path: Path, entry: str = "infra.py", entry_content: str = "pass\n") -> None:
    """Create a minimal project directory for validation."""
    (tmp_path / "tlumi.yaml").write_text(f"project:\n  name: myproj\n  entry: {entry}\n")
    if entry_content is not None:
        (tmp_path / entry).write_text(entry_content)


@patch("tlumi.commands.validate.find_project_dir")
@patch("tlumi.commands.validate.merge_variables")
def test_validate_syntax_error_raises_config_error(mock_merge, mock_find, tmp_path, capsys):
    """validate raises ConfigError on Python syntax errors."""
    _setup_project(tmp_path, entry_content="def foo(\n")
    mock_find.return_value = tmp_path
    mock_merge.side_effect = lambda cfg, **_: cfg

    with pytest.raises(ConfigError, match="Syntax error"):
        run_validate()


@patch("tlumi.commands.validate.find_project_dir")
@patch("tlumi.commands.validate.merge_variables")
def test_validate_missing_entry_raises_config_error(mock_merge, mock_find, tmp_path):
    """validate raises ConfigError when entry file does not exist."""
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: myproj\n  entry: missing.py\n")
    mock_find.return_value = tmp_path
    mock_merge.side_effect = lambda cfg, **_: cfg

    with pytest.raises(ConfigError, match="Entry file not found"):
        run_validate()


@patch("tlumi.commands.validate.find_project_dir")
@patch("tlumi.commands.validate.merge_variables")
def test_validate_unreadable_entry_raises_config_error(mock_merge, mock_find, tmp_path):
    """validate raises ConfigError when entry file cannot be read."""
    _setup_project(tmp_path)
    mock_find.return_value = tmp_path
    mock_merge.side_effect = lambda cfg, **_: cfg
    entry_path = tmp_path / "infra.py"
    entry_path.chmod(0o000)

    try:
        with pytest.raises(ConfigError, match="Cannot read"):
            run_validate()
    finally:
        entry_path.chmod(0o644)


@patch("tlumi.commands.validate.find_project_dir")
@patch("tlumi.commands.validate.merge_variables")
def test_validate_json_output_structure(mock_merge, mock_find, tmp_path, capsys):
    """validate --json emits correct JSON structure."""
    _setup_project(tmp_path)
    mock_find.return_value = tmp_path
    mock_merge.side_effect = lambda cfg, **_: cfg

    run_validate(json_output=True)

    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["valid"] is True
    assert "tlumi.yaml" in data["checks"]
    assert "infra.py exists" in data["checks"]
    assert "infra.py syntax" in data["checks"]


@patch("tlumi.commands.validate.find_project_dir")
@patch("tlumi.commands.validate.merge_variables")
def test_validate_unicode_error_raises_config_error(mock_merge, mock_find, tmp_path):
    """validate raises ConfigError on unreadable encoding."""
    _setup_project(tmp_path)
    mock_find.return_value = tmp_path
    mock_merge.side_effect = lambda cfg, **_: cfg
    # Write binary content that can't be decoded as UTF-8
    (tmp_path / "infra.py").write_bytes(b"\x80\x81\x82\x83")

    with pytest.raises(ConfigError, match="Cannot read"):
        run_validate()
