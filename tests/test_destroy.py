"""Tests for tlumi.commands.destroy: state display, confirm flow, JSON output."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from helpers import emit_events, mock_state, mock_summary_result
from pulumi.automation import Stack

from tlumi.commands.destroy import run_destroy

_SAMPLE_RESOURCES = [
    {
        "type": "pulumi:pulumi:Stack",
        "urn": "urn:pulumi:default::proj::pulumi:pulumi:Stack::proj-default",
    },
    {
        "type": "aws:s3:BucketV2",
        "urn": "urn:pulumi:default::proj::aws:s3:BucketV2::my-bucket",
    },
    {
        "type": "aws:ec2:Instance",
        "urn": "urn:pulumi:default::proj::aws:ec2:Instance::web-server",
    },
]


@patch("tlumi.commands.destroy.safe_export_stack")
@patch("tlumi.commands.destroy.get_stack")
@patch("tlumi.commands.destroy.merge_variables")
@patch("tlumi.commands.destroy.load_config")
@patch("tlumi.commands.destroy.find_project_dir")
def test_destroy_json_output_structure(
    mock_find, mock_config, mock_merge, mock_get_stack, mock_export, capsys
):
    """destroy --json --auto-approve emits correct JSON with changes and duration."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.destroy.side_effect = emit_events(
        {"delete": 2}, kind="res_outputs", result=mock_summary_result()
    )
    mock_get_stack.return_value = mock_stack
    mock_export.return_value = mock_state(_SAMPLE_RESOURCES)

    run_destroy(auto_approve=True, json_output=True)

    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["changes"]["delete"] == 2
    assert "duration" in data


@patch("tlumi.commands.destroy.safe_export_stack")
@patch("tlumi.commands.destroy.get_stack")
@patch("tlumi.commands.destroy.merge_variables")
@patch("tlumi.commands.destroy.load_config")
@patch("tlumi.commands.destroy.find_project_dir")
def test_destroy_empty_state(
    mock_find, mock_config, mock_merge, mock_get_stack, mock_export, capsys
):
    """destroy with empty state exits early."""
    mock_get_stack.return_value = MagicMock(spec=Stack)
    mock_export.return_value = mock_state(
        resources=[
            {
                "type": "pulumi:pulumi:Stack",
                "urn": "urn:pulumi:default::p::pulumi:pulumi:Stack::p-default",
            },
        ]
    )

    run_destroy(auto_approve=True, json_output=False)

    output = capsys.readouterr().out
    assert "No resources to destroy" in output


@patch("tlumi.commands.destroy.safe_export_stack")
@patch("tlumi.commands.destroy.get_stack")
@patch("tlumi.commands.destroy.merge_variables")
@patch("tlumi.commands.destroy.load_config")
@patch("tlumi.commands.destroy.find_project_dir")
def test_destroy_empty_state_json(
    mock_find, mock_config, mock_merge, mock_get_stack, mock_export, capsys
):
    """destroy --json with empty state emits JSON with zeros."""
    mock_get_stack.return_value = MagicMock(spec=Stack)
    mock_export.return_value = mock_state(
        resources=[
            {
                "type": "pulumi:pulumi:Stack",
                "urn": "urn:pulumi:default::p::pulumi:pulumi:Stack::p-default",
            },
        ]
    )

    run_destroy(auto_approve=True, json_output=True)

    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["changes"]["delete"] == 0
    assert data["duration"] == "0s"


@patch("tlumi.commands.destroy.confirm", return_value=True)
@patch("tlumi.commands.destroy.safe_export_stack")
@patch("tlumi.commands.destroy.get_stack")
@patch("tlumi.commands.destroy.merge_variables")
@patch("tlumi.commands.destroy.load_config")
@patch("tlumi.commands.destroy.find_project_dir")
def test_destroy_confirm_accepted(
    mock_find, mock_config, mock_merge, mock_get_stack, mock_export, mock_confirm, capsys
):
    """destroy with confirmation accepted proceeds with destroy."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.destroy.side_effect = emit_events(
        {"delete": 1}, kind="res_outputs", result=mock_summary_result()
    )
    mock_get_stack.return_value = mock_stack
    mock_export.return_value = mock_state(_SAMPLE_RESOURCES)

    run_destroy(auto_approve=False, json_output=False)

    mock_stack.destroy.assert_called_once()
    mock_confirm.assert_called_once()


@patch("tlumi.commands.destroy.confirm", return_value=False)
@patch("tlumi.commands.destroy.safe_export_stack")
@patch("tlumi.commands.destroy.get_stack")
@patch("tlumi.commands.destroy.merge_variables")
@patch("tlumi.commands.destroy.load_config")
@patch("tlumi.commands.destroy.find_project_dir")
def test_destroy_confirm_rejected(
    mock_find, mock_config, mock_merge, mock_get_stack, mock_export, mock_confirm, capsys
):
    """destroy with confirmation rejected does not destroy."""
    mock_get_stack.return_value = MagicMock(spec=Stack)
    mock_export.return_value = mock_state(_SAMPLE_RESOURCES)

    run_destroy(auto_approve=False, json_output=False)

    mock_get_stack.return_value.destroy.assert_not_called()
    output = capsys.readouterr().out
    assert "cancelled" in output.lower()


@patch("tlumi.commands.destroy.safe_export_stack")
@patch("tlumi.commands.destroy.get_stack")
@patch("tlumi.commands.destroy.merge_variables")
@patch("tlumi.commands.destroy.load_config")
@patch("tlumi.commands.destroy.find_project_dir")
def test_destroy_auto_approve_skips_confirm(
    mock_find, mock_config, mock_merge, mock_get_stack, mock_export, capsys
):
    """destroy --auto-approve skips confirmation prompt."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.destroy.side_effect = emit_events(
        {"delete": 1}, kind="res_outputs", result=mock_summary_result()
    )
    mock_get_stack.return_value = mock_stack
    mock_export.return_value = mock_state(_SAMPLE_RESOURCES)

    with patch("tlumi.commands.destroy.confirm") as mock_confirm:
        run_destroy(auto_approve=True, json_output=False)
        mock_confirm.assert_not_called()

    mock_stack.destroy.assert_called_once()


@patch("tlumi.commands.destroy.resolve_targets")
@patch("tlumi.commands.destroy.safe_export_stack")
@patch("tlumi.commands.destroy.get_stack")
@patch("tlumi.commands.destroy.merge_variables")
@patch("tlumi.commands.destroy.load_config")
@patch("tlumi.commands.destroy.find_project_dir")
def test_destroy_with_target(
    mock_find, mock_config, mock_merge, mock_get_stack, mock_export, mock_resolve, capsys
):
    """destroy --target filters display and passes targets to destroy call."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.destroy.side_effect = emit_events(
        {"delete": 1}, kind="res_outputs", result=mock_summary_result()
    )
    mock_get_stack.return_value = mock_stack
    mock_export.return_value = mock_state(_SAMPLE_RESOURCES)
    target_urn = "urn:pulumi:default::proj::aws:s3:BucketV2::my-bucket"
    mock_resolve.return_value = [target_urn]

    run_destroy(auto_approve=True, target=["my-bucket"], json_output=True)

    # resolve_targets called with pre-loaded resources
    mock_resolve.assert_called_once()
    call_kwargs = mock_resolve.call_args[1]
    assert "resources" in call_kwargs

    output = capsys.readouterr().out
    data = json.loads(output)
    assert data.get("targets") == [target_urn]

    # destroy called with resolved targets
    call_kwargs = mock_stack.destroy.call_args[1]
    assert call_kwargs["target"] == [target_urn]


@patch("tlumi.commands.destroy.safe_export_stack")
@patch("tlumi.commands.destroy.get_stack")
@patch("tlumi.commands.destroy.merge_variables")
@patch("tlumi.commands.destroy.load_config")
@patch("tlumi.commands.destroy.find_project_dir")
def test_destroy_state_loaded_once(
    mock_find, mock_config, mock_merge, mock_get_stack, mock_export, capsys
):
    """destroy loads state exactly once via safe_export_stack."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.destroy.side_effect = emit_events(
        {"delete": 1}, kind="res_outputs", result=mock_summary_result()
    )
    mock_get_stack.return_value = mock_stack
    mock_export.return_value = mock_state(_SAMPLE_RESOURCES)

    run_destroy(auto_approve=True, json_output=True)

    mock_export.assert_called_once_with(mock_stack)


# ---------------------------------------------------------------------------
# R7-7: Human-mode resource display
# ---------------------------------------------------------------------------


@patch("tlumi.commands.destroy.safe_export_stack")
@patch("tlumi.commands.destroy.get_stack")
@patch("tlumi.commands.destroy.merge_variables")
@patch("tlumi.commands.destroy.load_config")
@patch("tlumi.commands.destroy.find_project_dir")
def test_destroy_displays_resource_list(
    mock_find, mock_config, mock_merge, mock_get_stack, mock_export, capsys
):
    """destroy in human mode displays resource type and name before confirm."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.destroy.side_effect = emit_events(
        {"delete": 2}, kind="res_outputs", result=mock_summary_result()
    )
    mock_get_stack.return_value = mock_stack
    mock_export.return_value = mock_state(_SAMPLE_RESOURCES)

    run_destroy(auto_approve=True, json_output=False)

    output = capsys.readouterr().out
    assert "aws:s3:BucketV2" in output
    assert "my-bucket" in output
    assert "web-server" in output
    assert "2 to destroy" in output


# ---------------------------------------------------------------------------
# R7-8: Null deployment path
# ---------------------------------------------------------------------------


@patch("tlumi.commands.destroy.safe_export_stack")
@patch("tlumi.commands.destroy.get_stack")
@patch("tlumi.commands.destroy.merge_variables")
@patch("tlumi.commands.destroy.load_config")
@patch("tlumi.commands.destroy.find_project_dir")
def test_destroy_null_deployment(
    mock_find, mock_config, mock_merge, mock_get_stack, mock_export, capsys
):
    """destroy with deployment=None exits early with no-resources message."""
    mock_get_stack.return_value = MagicMock(spec=Stack)
    mock_export.return_value = mock_state()  # deployment=None

    run_destroy(auto_approve=True, json_output=False)

    output = capsys.readouterr().out
    assert "No resources to destroy" in output


# ---------------------------------------------------------------------------
# R7-9: Null summary path
# ---------------------------------------------------------------------------


@patch("tlumi.commands.destroy.safe_export_stack")
@patch("tlumi.commands.destroy.get_stack")
@patch("tlumi.commands.destroy.merge_variables")
@patch("tlumi.commands.destroy.load_config")
@patch("tlumi.commands.destroy.find_project_dir")
def test_destroy_null_summary(
    mock_find, mock_config, mock_merge, mock_get_stack, mock_export, capsys
):
    """destroy handles result.summary being None without error."""
    mock_stack = MagicMock(spec=Stack)
    result = MagicMock()
    result.summary = None
    mock_stack.destroy.return_value = result
    mock_get_stack.return_value = mock_stack
    mock_export.return_value = mock_state(_SAMPLE_RESOURCES)

    run_destroy(auto_approve=True, json_output=True)

    output = capsys.readouterr().out
    data = json.loads(output)
    # All change counts should be zero when summary is None
    assert data["changes"]["delete"] == 0


# ---------------------------------------------------------------------------
# R8-E2: Non-dict resource guard in destroy
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# R10-S4: destroy --json requires --auto-approve
# ---------------------------------------------------------------------------


def test_destroy_json_requires_auto_approve():
    """destroy --json without --auto-approve raises TlumiError."""
    from tlumi.errors import TlumiError

    with (
        patch("tlumi.commands.destroy.load_config"),
        patch("tlumi.commands.destroy.find_project_dir"),
        patch("tlumi.commands.destroy.merge_variables"),
    ):
        with pytest.raises(TlumiError, match="--auto-approve is required"):
            run_destroy(auto_approve=False, json_output=True)


@patch("tlumi.commands.destroy.safe_export_stack")
@patch("tlumi.commands.destroy.get_stack")
@patch("tlumi.commands.destroy.merge_variables")
@patch("tlumi.commands.destroy.load_config")
@patch("tlumi.commands.destroy.find_project_dir")
def test_destroy_non_dict_resources_ignored(
    mock_find, mock_config, mock_merge, mock_get_stack, mock_export, capsys
):
    """Non-dict entries in resources list are silently filtered out."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.destroy.side_effect = emit_events(
        {"delete": 1}, kind="res_outputs", result=mock_summary_result()
    )
    mock_get_stack.return_value = mock_stack
    mock_export.return_value = mock_state(
        [
            {
                "type": "pulumi:pulumi:Stack",
                "urn": "urn:pulumi:default::p::pulumi:pulumi:Stack::p-default",
            },
            "not-a-dict",
            {"type": "aws:s3:BucketV2", "urn": "urn:pulumi:default::p::aws:s3:BucketV2::b"},
        ]
    )

    run_destroy(auto_approve=True, json_output=False)

    output = capsys.readouterr().out
    assert "aws:s3:BucketV2" in output
    assert "1 to destroy" in output
