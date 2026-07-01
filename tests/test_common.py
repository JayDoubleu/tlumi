"""Tests for tlumi.commands._common: setup_command helper."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from pulumi.automation import Stack

from tlumi.commands._common import setup_command


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_setup_command_returns_result(mock_find, mock_config, mock_merge, mock_get_stack):
    """setup_command returns SetupResult with all four fields populated."""
    config = MagicMock()
    mock_config.return_value = config
    # merge_variables is the post-frozen-ProjectConfig source of the final
    # config; pretend it's the identity transform for this test so the
    # assertion still targets the value that flowed into get_stack.
    mock_merge.return_value = config
    stack = MagicMock(spec=Stack)
    mock_get_stack.return_value = stack

    result = setup_command(banner="Test")
    assert result.config is config
    assert result.stack is stack
    assert result.resolved_targets is None
    assert result.resolved_replace is None


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_setup_command_quiet_suppresses_banner(
    mock_find, mock_config, mock_merge, mock_get_stack, capsys
):
    """setup_command in JSON mode suppresses banner output."""
    mock_config.return_value = MagicMock()
    mock_get_stack.return_value = MagicMock(spec=Stack)

    setup_command(banner="Should Not Appear", json_output=True)
    output = capsys.readouterr().out
    assert "Should Not Appear" not in output


@patch("tlumi.commands._common.resolve_targets")
@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_setup_command_resolves_targets(
    mock_find, mock_config, mock_merge, mock_get_stack, mock_resolve
):
    """setup_command resolves targets when provided."""
    mock_config.return_value = MagicMock()
    stack = MagicMock(spec=Stack)
    mock_get_stack.return_value = stack
    mock_resolve.return_value = ["urn:pulumi:default::p::aws:s3:BucketV2::b"]

    result = setup_command(target=["b"])
    mock_resolve.assert_called_once_with(stack, ["b"])
    assert result.resolved_targets == ["urn:pulumi:default::p::aws:s3:BucketV2::b"]


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_setup_command_passes_variables(mock_find, mock_config, mock_merge, mock_get_stack):
    """setup_command passes var and var_file to merge_variables."""
    config = MagicMock()
    mock_config.return_value = config
    mock_get_stack.return_value = MagicMock(spec=Stack)

    setup_command(var=["region=us-east-1"], var_file=["prod.yaml"])

    mock_merge.assert_called_once_with(
        config,
        var=["region=us-east-1"],
        var_file=["prod.yaml"],
        quiet=False,
    )


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_setup_command_passes_quiet_to_merge(mock_find, mock_config, mock_merge, mock_get_stack):
    """setup_command passes json_output as quiet to merge_variables."""
    config = MagicMock()
    mock_config.return_value = config
    mock_get_stack.return_value = MagicMock(spec=Stack)

    setup_command(json_output=True)

    mock_merge.assert_called_once_with(config, var=None, var_file=None, quiet=True)


@patch("tlumi.commands._common.resolve_targets")
@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_setup_command_resolves_replace(
    mock_find, mock_config, mock_merge, mock_get_stack, mock_resolve
):
    """setup_command resolves replace targets when provided."""
    mock_config.return_value = MagicMock()
    stack = MagicMock(spec=Stack)
    mock_get_stack.return_value = stack
    mock_resolve.return_value = ["urn:pulumi:default::p::aws:s3:BucketV2::b"]

    result = setup_command(replace=["b"], json_output=True)
    mock_resolve.assert_called_once_with(stack, ["b"])
    assert result.resolved_replace == ["urn:pulumi:default::p::aws:s3:BucketV2::b"]
