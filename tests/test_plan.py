"""Tests for tlumi.commands.plan: preview, JSON, targets, replace, destroy, saved plans."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from helpers import emit_events, mock_preview_result
from pulumi.automation import Stack

from tlumi.commands.plan import run_plan
from tlumi.diffs import PropertyChange
from tlumi.errors import TlumiError


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_plan_json_output_structure(mock_find, mock_config, mock_merge, mock_get_stack, capsys):
    """plan --json emits correct JSON structure with change counts."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.preview.side_effect = emit_events(
        {"create": 2, "update": 1}, kind="resource_pre", result=mock_preview_result()
    )
    mock_get_stack.return_value = mock_stack

    run_plan(json_output=True)

    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["changes"]["create"] == 2
    assert data["changes"]["update"] == 1
    assert data["changes"]["replace"] == 0
    assert data["changes"]["delete"] == 0
    assert "resource_diffs" not in data


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_plan_human_prints_summary(mock_find, mock_config, mock_merge, mock_get_stack, capsys):
    """plan in human mode prints plan summary."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.preview.side_effect = emit_events(
        {"create": 1, "delete": 2}, kind="resource_pre", result=mock_preview_result()
    )
    mock_get_stack.return_value = mock_stack

    run_plan(json_output=False)

    output = capsys.readouterr().out
    assert "1 to add" in output
    assert "2 to destroy" in output


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_plan_no_changes(mock_find, mock_config, mock_merge, mock_get_stack, capsys):
    """plan with no changes prints up-to-date message."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.preview.return_value = mock_preview_result({})
    mock_get_stack.return_value = mock_stack

    run_plan(json_output=False)

    output = capsys.readouterr().out
    assert "up-to-date" in output


@patch("tlumi.commands._common.resolve_targets")
@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_plan_with_target(mock_find, mock_config, mock_merge, mock_get_stack, mock_resolve, capsys):
    """plan --target calls resolve_targets and includes targets in JSON."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.preview.return_value = mock_preview_result({"create": 1})
    mock_get_stack.return_value = mock_stack
    mock_resolve.return_value = ["urn:pulumi:default::p::aws:s3:BucketV2::b"]

    run_plan(target=["my-bucket"], json_output=True)

    mock_resolve.assert_called_once_with(mock_stack, ["my-bucket"])
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["targets"] == ["urn:pulumi:default::p::aws:s3:BucketV2::b"]
    mock_stack.preview.assert_called_once()
    call_kwargs = mock_stack.preview.call_args[1]
    assert call_kwargs["target"] == ["urn:pulumi:default::p::aws:s3:BucketV2::b"]


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_plan_json_includes_property_diffs(
    mock_find, mock_config, mock_merge, mock_get_stack, capsys
):
    """plan --json includes resource_diffs when handler collects diffs."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.preview.return_value = mock_preview_result({"create": 1})
    mock_get_stack.return_value = mock_stack

    # Patch EventHandler to inject property diffs
    with patch("tlumi.commands.plan.EventHandler") as mock_handler_cls:
        handler = MagicMock()
        handler.property_diffs = {
            "urn:pulumi:default::p::aws:s3:BucketV2::b": [
                PropertyChange.add("name", '"my-bucket"'),
            ],
        }
        handler.callback_errors = 0
        handler.warnings = []
        handler.change_counts.return_value = {"create": 1}
        handler.stack_outputs_changed = False
        mock_handler_cls.return_value = handler

        run_plan(json_output=True)

    output = capsys.readouterr().out
    data = json.loads(output)
    assert "resource_diffs" in data
    diffs = data["resource_diffs"]["urn:pulumi:default::p::aws:s3:BucketV2::b"]
    assert len(diffs) == 1
    assert diffs[0]["property"] == "name"
    assert diffs[0]["action"] == "add"
    assert diffs[0]["new"] == '"my-bucket"'


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_plan_json_no_changes_structure(mock_find, mock_config, mock_merge, mock_get_stack, capsys):
    """plan --json with no changes still emits valid JSON with all zeros."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.preview.return_value = mock_preview_result({})
    mock_get_stack.return_value = mock_stack

    run_plan(json_output=True)

    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["changes"] == {"create": 0, "update": 0, "replace": 0, "delete": 0, "import": 0}
    assert data["outputs_changed"] is False


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_plan_passes_variables(mock_find, mock_config, mock_merge, mock_get_stack, capsys):
    """plan passes --var and --var-file through to merge_variables."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.preview.return_value = mock_preview_result({})
    mock_get_stack.return_value = mock_stack
    mock_config.return_value = MagicMock()

    run_plan(var=["region=us-east-1"], var_file=["vars.yaml"], json_output=True)

    mock_merge.assert_called_once()
    call_kwargs = mock_merge.call_args[1]
    assert call_kwargs["var"] == ["region=us-east-1"]
    assert call_kwargs["var_file"] == ["vars.yaml"]


# ---------------------------------------------------------------------------
# --replace tests
# ---------------------------------------------------------------------------


@patch("tlumi.commands._common.resolve_targets")
@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_plan_with_replace(
    mock_find, mock_config, mock_merge, mock_get_stack, mock_resolve, capsys
):
    """plan --replace resolves identifiers and passes replace= to preview."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.preview.return_value = mock_preview_result({"replace": 1})
    mock_get_stack.return_value = mock_stack
    replace_urn = "urn:pulumi:default::p::aws:s3:BucketV2::b"
    mock_resolve.return_value = [replace_urn]

    run_plan(replace=["my-bucket"], json_output=True)

    # resolve_targets is called twice: once for target (None), once for replace
    assert mock_resolve.call_count == 1
    mock_resolve.assert_called_with(mock_stack, ["my-bucket"])

    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["replace"] == [replace_urn]
    call_kwargs = mock_stack.preview.call_args[1]
    assert call_kwargs["replace"] == [replace_urn]


@patch("tlumi.commands._common.resolve_targets")
@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_plan_replace_and_target(
    mock_find, mock_config, mock_merge, mock_get_stack, mock_resolve, capsys
):
    """plan --replace --target passes both to preview."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.preview.return_value = mock_preview_result({"replace": 1})
    mock_get_stack.return_value = mock_stack
    target_urn = "urn:pulumi:default::p::aws:s3:BucketV2::t"
    replace_urn = "urn:pulumi:default::p::aws:s3:BucketV2::r"
    mock_resolve.side_effect = [[target_urn], [replace_urn]]

    run_plan(target=["t"], replace=["r"], json_output=True)

    call_kwargs = mock_stack.preview.call_args[1]
    assert call_kwargs["target"] == [target_urn]
    assert call_kwargs["replace"] == [replace_urn]


# ---------------------------------------------------------------------------
# --destroy tests
# ---------------------------------------------------------------------------


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_plan_destroy(mock_find, mock_config, mock_merge, mock_get_stack, capsys):
    """plan --destroy calls preview_destroy instead of preview."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.preview_destroy.side_effect = emit_events(
        {"delete": 3}, kind="resource_pre", result=mock_preview_result()
    )
    mock_get_stack.return_value = mock_stack

    run_plan(destroy=True, json_output=True)

    mock_stack.preview_destroy.assert_called_once()
    mock_stack.preview.assert_not_called()
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["changes"]["delete"] == 3


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_plan_destroy_human_banner(mock_find, mock_config, mock_merge, mock_get_stack, capsys):
    """plan --destroy shows 'Previewing destruction...' banner."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.preview_destroy.return_value = mock_preview_result({"delete": 1})
    mock_get_stack.return_value = mock_stack

    run_plan(destroy=True, json_output=False)

    output = capsys.readouterr().out
    assert "Previewing destruction" in output


def test_plan_destroy_and_replace_mutual_exclusion():
    """plan --destroy --replace raises TlumiError."""
    with pytest.raises(TlumiError, match="--destroy and --replace"):
        run_plan(destroy=True, replace=["bucket"])


def test_plan_destroy_and_out_mutual_exclusion():
    """plan --destroy --out raises TlumiError."""
    with pytest.raises(TlumiError, match="--destroy and --out"):
        run_plan(destroy=True, out="plan.json")


@patch("tlumi.commands._common.resolve_targets")
@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_plan_destroy_with_target(
    mock_find, mock_config, mock_merge, mock_get_stack, mock_resolve, capsys
):
    """plan --destroy --target passes targets to preview_destroy."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.preview_destroy.return_value = mock_preview_result({"delete": 1})
    mock_get_stack.return_value = mock_stack
    target_urn = "urn:pulumi:default::p::aws:s3:BucketV2::b"
    mock_resolve.return_value = [target_urn]

    run_plan(destroy=True, target=["b"], json_output=True)

    call_kwargs = mock_stack.preview_destroy.call_args[1]
    assert call_kwargs["target"] == [target_urn]


# ---------------------------------------------------------------------------
# --out (saved plan) tests
# ---------------------------------------------------------------------------


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_plan_out_passes_plan_path(
    mock_find, mock_config, mock_merge, mock_get_stack, tmp_path, capsys
):
    """plan --out passes plan= to preview."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.preview.return_value = mock_preview_result({"create": 1})
    mock_get_stack.return_value = mock_stack

    plan_path = str(tmp_path / "plan.json")
    run_plan(out=plan_path, json_output=True)

    call_kwargs = mock_stack.preview.call_args[1]
    assert "plan" in call_kwargs
    assert call_kwargs["plan"] == str((tmp_path / "plan.json").resolve())


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_plan_out_json_includes_plan_file(
    mock_find, mock_config, mock_merge, mock_get_stack, tmp_path, capsys
):
    """plan --out --json includes plan_file key in output."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.preview.return_value = mock_preview_result({"create": 1})
    mock_get_stack.return_value = mock_stack

    plan_path = str(tmp_path / "plan.json")
    run_plan(out=plan_path, json_output=True)

    output = capsys.readouterr().out
    data = json.loads(output)
    assert "plan_file" in data


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_plan_out_human_prints_success(
    mock_find, mock_config, mock_merge, mock_get_stack, tmp_path, capsys
):
    """plan --out in human mode prints success message with file path."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.preview.return_value = mock_preview_result({"create": 1})
    mock_get_stack.return_value = mock_stack

    plan_path = str(tmp_path / "plan.json")
    run_plan(out=plan_path, json_output=False)

    output = capsys.readouterr().out
    assert "Plan saved" in output


# ---------------------------------------------------------------------------
# Import counts and output-only changes in plan output (audit findings).
# ---------------------------------------------------------------------------


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_plan_json_includes_import_and_outputs_changed(
    mock_find, mock_config, mock_merge, mock_get_stack, capsys
):
    """plan --json reports import counts and the outputs_changed signal."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.preview.side_effect = emit_events(
        {"import": 1}, kind="resource_pre", result=mock_preview_result({})
    )
    mock_get_stack.return_value = mock_stack

    run_plan(json_output=True)

    data = json.loads(capsys.readouterr().out)
    assert data["changes"]["import"] == 1
    assert data["outputs_changed"] is False


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_plan_human_shows_import_count(mock_find, mock_config, mock_merge, mock_get_stack, capsys):
    """Human plan summary renders 'N to import' instead of dropping imports."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.preview.side_effect = emit_events(
        {"import": 1, "create": 1}, kind="resource_pre", result=mock_preview_result({})
    )
    mock_get_stack.return_value = mock_stack

    run_plan(json_output=False)

    out = capsys.readouterr().out
    assert "1 to import" in out
    assert "1 to add" in out


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_plan_output_only_change_not_reported_as_no_changes(
    mock_find, mock_config, mock_merge, mock_get_stack, capsys
):
    """A pending output-only change surfaces in plan instead of 'No changes'."""
    from helpers import stack_outputs_event

    def _emit(*args, **kwargs):
        on_event = kwargs.get("on_event")
        if on_event is not None:
            on_event(stack_outputs_event({"greeting": "world"}, {"greeting": "mars"}))
        return mock_preview_result({})

    mock_stack = MagicMock(spec=Stack)
    mock_stack.preview.side_effect = _emit
    mock_get_stack.return_value = mock_stack

    run_plan(json_output=False)
    out = capsys.readouterr().out
    assert "outputs will change" in out
    assert "up-to-date" not in out.lower()

    mock_stack.preview.side_effect = _emit
    run_plan(json_output=True)
    data = json.loads(capsys.readouterr().out)
    assert data["outputs_changed"] is True
