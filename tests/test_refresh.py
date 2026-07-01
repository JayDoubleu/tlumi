"""Tests for tlumi.commands.refresh: state reconciliation, JSON output."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from helpers import emit_events, mock_summary_result
from pulumi.automation import Stack

from tlumi.commands.refresh import run_refresh


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_refresh_json_output_structure(mock_find, mock_config, mock_merge, mock_get_stack, capsys):
    """refresh --json emits correct JSON with changes and duration."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.refresh.side_effect = emit_events(
        {"update": 1}, kind="res_outputs", result=mock_summary_result()
    )
    mock_get_stack.return_value = mock_stack

    run_refresh(json_output=True)

    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["changes"]["update"] == 1
    assert "duration" in data


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_refresh_state_up_to_date(mock_find, mock_config, mock_merge, mock_get_stack, capsys):
    """refresh with no changes prints up-to-date message."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.refresh.side_effect = emit_events(
        {}, kind="res_outputs", result=mock_summary_result()
    )
    mock_get_stack.return_value = mock_stack

    run_refresh(json_output=False)

    output = capsys.readouterr().out
    assert "up-to-date" in output.lower()


@patch("tlumi.commands._common.resolve_targets")
@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_refresh_with_target(
    mock_find, mock_config, mock_merge, mock_get_stack, mock_resolve, capsys
):
    """refresh --target passes resolved targets to refresh call."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.refresh.side_effect = emit_events(
        {"update": 1}, kind="res_outputs", result=mock_summary_result()
    )
    mock_get_stack.return_value = mock_stack
    target_urn = "urn:pulumi:default::p::aws:s3:BucketV2::b"
    mock_resolve.return_value = [target_urn]

    run_refresh(target=["my-bucket"], json_output=True)

    mock_resolve.assert_called_once_with(mock_stack, ["my-bucket"])
    call_kwargs = mock_stack.refresh.call_args[1]
    assert call_kwargs["target"] == [target_urn]

    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["targets"] == [target_urn]


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_refresh_human_prints_summary(mock_find, mock_config, mock_merge, mock_get_stack, capsys):
    """refresh in human mode prints resource summary on changes."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.refresh.side_effect = emit_events(
        {"update": 2, "delete": 1}, kind="res_outputs", result=mock_summary_result()
    )
    mock_get_stack.return_value = mock_stack

    run_refresh(json_output=False)

    output = capsys.readouterr().out
    assert "2 updated" in output
    assert "1 destroyed" in output


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_refresh_json_no_changes(mock_find, mock_config, mock_merge, mock_get_stack, capsys):
    """refresh --json with no changes still has all zero counts."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.refresh.side_effect = emit_events(
        {}, kind="res_outputs", result=mock_summary_result()
    )
    mock_get_stack.return_value = mock_stack

    run_refresh(json_output=True)

    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["changes"] == {"create": 0, "update": 0, "replace": 0, "delete": 0, "import": 0}


# ---------------------------------------------------------------------------
# R7-9: Null summary path
# ---------------------------------------------------------------------------


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_refresh_null_summary(mock_find, mock_config, mock_merge, mock_get_stack, capsys):
    """refresh handles result.summary being None without AttributeError."""
    mock_stack = MagicMock(spec=Stack)
    result = MagicMock()
    result.summary = None
    mock_stack.refresh.return_value = result
    mock_get_stack.return_value = mock_stack

    run_refresh(json_output=True)

    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["changes"]["update"] == 0
