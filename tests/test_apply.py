"""Tests for tlumi.commands.apply: secret masking in outputs."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from helpers import (
    emit_events,
    mock_output_value,
    mock_preview_result,
    mock_state,
    mock_up_result,
)
from pulumi.automation import Stack

from tlumi.commands.apply import run_apply


def _secret_wrapper(plaintext: object) -> dict:
    """Build the secret sig wrapper as it appears in exported state."""
    from tlumi.sanitize import _PULUMI_SECRET_SIG, _PULUMI_SECRET_VALUE

    return {_PULUMI_SECRET_SIG: _PULUMI_SECRET_VALUE, "plaintext": json.dumps(plaintext)}


def _stack_state(outputs: dict):
    """Mock exported state whose pulumi:pulumi:Stack resource carries outputs."""
    return mock_state(
        [
            {
                "type": "pulumi:pulumi:Stack",
                "urn": "urn:pulumi:default::p::pulumi:pulumi:Stack::p",
                "outputs": outputs,
            }
        ]
    )


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_apply_json_masks_secrets(mock_find, mock_config, mock_merge, mock_get_stack, capsys):
    """apply --json masks secret outputs by default (sourced from exported state)."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.up.return_value = mock_up_result(
        outputs={
            "url": mock_output_value("https://example.com"),
            "api_key": mock_output_value("secret-value-123", secret=True),
        },
    )
    mock_stack.export_stack.return_value = _stack_state(
        {
            "url": "https://example.com",
            "api_key": _secret_wrapper("secret-value-123"),
        }
    )
    mock_get_stack.return_value = mock_stack

    run_apply(auto_approve=True, json_output=True)

    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["outputs"]["url"] == "https://example.com"
    assert data["outputs"]["api_key"] == "(sensitive)"
    assert "secret-value-123" not in output


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_apply_json_shows_secrets_with_flag(
    mock_find, mock_config, mock_merge, mock_get_stack, capsys
):
    """apply --json --show-secrets reveals secret outputs."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.up.return_value = mock_up_result(
        outputs={
            "url": mock_output_value("https://example.com"),
            "api_key": mock_output_value("secret-value-123", secret=True),
        },
    )
    mock_get_stack.return_value = mock_stack

    run_apply(auto_approve=True, json_output=True, show_secrets=True)

    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["outputs"]["url"] == "https://example.com"
    assert data["outputs"]["api_key"] == "secret-value-123"


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_apply_human_masks_secrets(mock_find, mock_config, mock_merge, mock_get_stack, capsys):
    """apply in human mode masks secret outputs by default."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.up.return_value = mock_up_result(
        outputs={
            "url": mock_output_value("https://example.com"),
            "api_key": mock_output_value("secret-value-123", secret=True),
        },
    )
    mock_stack.export_stack.return_value = _stack_state(
        {
            "url": "https://example.com",
            "api_key": _secret_wrapper("secret-value-123"),
        }
    )
    mock_get_stack.return_value = mock_stack

    run_apply(auto_approve=True, json_output=False)

    output = capsys.readouterr().out
    assert "(sensitive)" in output
    assert "secret-value-123" not in output


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_apply_human_shows_secrets_with_flag(
    mock_find, mock_config, mock_merge, mock_get_stack, capsys
):
    """apply --show-secrets in human mode reveals secret outputs."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.up.return_value = mock_up_result(
        outputs={
            "api_key": mock_output_value("secret-value-123", secret=True),
        },
    )
    mock_get_stack.return_value = mock_stack

    run_apply(auto_approve=True, json_output=False, show_secrets=True)

    output = capsys.readouterr().out
    assert "secret-value-123" in output
    assert "(sensitive)" not in output


@patch("tlumi.commands.apply.confirm", return_value=True)
@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_apply_interactive_confirm_accepted(
    mock_find, mock_config, mock_merge, mock_get_stack, mock_confirm, capsys
):
    """apply in interactive mode previews, confirms, then applies."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.preview.side_effect = emit_events(
        {"create": 1}, kind="resource_pre", result=mock_preview_result()
    )
    mock_stack.up.side_effect = emit_events(
        {"create": 1}, kind="res_outputs", result=mock_up_result()
    )
    mock_get_stack.return_value = mock_stack

    run_apply(auto_approve=False, json_output=False)

    mock_stack.preview.assert_called_once()
    mock_confirm.assert_called_once()
    mock_stack.up.assert_called_once()


@patch("tlumi.commands.apply.confirm", return_value=False)
@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_apply_interactive_confirm_rejected(
    mock_find, mock_config, mock_merge, mock_get_stack, mock_confirm, capsys
):
    """apply in interactive mode with rejected confirmation does not apply."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.preview.side_effect = emit_events(
        {"update": 1}, kind="resource_pre", result=mock_preview_result()
    )
    mock_get_stack.return_value = mock_stack

    run_apply(auto_approve=False, json_output=False)

    mock_stack.preview.assert_called_once()
    mock_stack.up.assert_not_called()
    output = capsys.readouterr().out
    assert "cancelled" in output.lower()


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_apply_no_changes_in_preview(mock_find, mock_config, mock_merge, mock_get_stack, capsys):
    """apply in interactive mode with no changes exits early without applying."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.preview.return_value = mock_preview_result({})
    mock_get_stack.return_value = mock_stack

    run_apply(auto_approve=False, json_output=False)

    mock_stack.preview.assert_called_once()
    mock_stack.up.assert_not_called()
    output = capsys.readouterr().out
    assert "up-to-date" in output.lower()


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_apply_auto_approve_skips_preview(
    mock_find, mock_config, mock_merge, mock_get_stack, capsys
):
    """apply --auto-approve skips preview and confirm, goes straight to up."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.up.return_value = mock_up_result(resource_changes={"create": 1})
    mock_get_stack.return_value = mock_stack

    run_apply(auto_approve=True, json_output=False)

    mock_stack.preview.assert_not_called()
    mock_stack.up.assert_called_once()


@patch("tlumi.commands._common.resolve_targets")
@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_apply_with_target(
    mock_find, mock_config, mock_merge, mock_get_stack, mock_resolve, capsys
):
    """apply --target passes resolved targets to up call."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.up.return_value = mock_up_result(resource_changes={"create": 1})
    mock_get_stack.return_value = mock_stack
    target_urn = "urn:pulumi:default::p::aws:s3:BucketV2::b"
    mock_resolve.return_value = [target_urn]

    run_apply(auto_approve=True, target=["my-bucket"], json_output=True)

    mock_resolve.assert_called_once_with(mock_stack, ["my-bucket"])
    call_kwargs = mock_stack.up.call_args[1]
    assert call_kwargs["target"] == [target_urn]


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_apply_json_output_structure(mock_find, mock_config, mock_merge, mock_get_stack, capsys):
    """apply --json --auto-approve emits correct JSON with changes, duration, outputs."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.up.side_effect = emit_events(
        {"create": 1, "update": 1},
        kind="res_outputs",
        result=mock_up_result(outputs={"url": mock_output_value("https://example.com")}),
    )
    mock_stack.export_stack.return_value = _stack_state({"url": "https://example.com"})
    mock_get_stack.return_value = mock_stack

    run_apply(auto_approve=True, json_output=True)

    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["changes"]["create"] == 1
    assert data["changes"]["update"] == 1
    assert "duration" in data
    assert data["outputs"]["url"] == "https://example.com"


# ---------------------------------------------------------------------------
# R7-9: Null summary path
# ---------------------------------------------------------------------------


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_apply_null_summary(mock_find, mock_config, mock_merge, mock_get_stack, capsys):
    """apply handles result.summary being None without AttributeError."""
    mock_stack = MagicMock(spec=Stack)
    result = MagicMock()
    result.summary = None
    result.outputs = {}
    mock_stack.up.return_value = result
    mock_get_stack.return_value = mock_stack

    run_apply(auto_approve=True, json_output=True)

    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["changes"]["create"] == 0


# ---------------------------------------------------------------------------
# R7-10: Callback errors suppressed in JSON mode
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# R10-S4: apply --json requires --auto-approve
# ---------------------------------------------------------------------------


def test_apply_json_requires_auto_approve():
    """apply --json without --auto-approve raises TlumiError before any setup."""
    from tlumi.errors import TlumiError

    with pytest.raises(TlumiError, match="--auto-approve is required"):
        run_apply(auto_approve=False, json_output=True)


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_apply_callback_errors_in_json_envelope(
    mock_find, mock_config, mock_merge, mock_get_stack, capsys
):
    """apply --json surfaces callback_errors in the JSON envelope (not as Rich text)."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.up.return_value = mock_up_result(resource_changes={"create": 1})
    mock_get_stack.return_value = mock_stack

    with patch("tlumi.commands.apply.EventHandler") as mock_handler_cls:
        handler = MagicMock()
        handler.callback_errors = 3
        handler.warnings = []
        handler.change_counts.return_value = {"create": 1}
        handler.start_live.return_value.__enter__ = MagicMock()
        handler.start_live.return_value.__exit__ = MagicMock(return_value=False)
        mock_handler_cls.return_value = handler

        run_apply(auto_approve=True, json_output=True)

    output = capsys.readouterr().out
    data = json.loads(output)
    # No human-readable warning text before the JSON envelope
    assert "callback" not in output.lower().split("{")[0]
    # But the count IS in the envelope so JSON consumers see the signal
    assert data["callback_errors"] == 3


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_apply_no_callback_errors_omits_field(
    mock_find, mock_config, mock_merge, mock_get_stack, capsys
):
    """No callback errors -> the JSON envelope omits the field (keeps clean shape)."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.up.return_value = mock_up_result(resource_changes={"create": 1})
    mock_get_stack.return_value = mock_stack

    with patch("tlumi.commands.apply.EventHandler") as mock_handler_cls:
        handler = MagicMock()
        handler.callback_errors = 0
        handler.warnings = []
        handler.change_counts.return_value = {"create": 1}
        handler.start_live.return_value.__enter__ = MagicMock()
        handler.start_live.return_value.__exit__ = MagicMock(return_value=False)
        mock_handler_cls.return_value = handler

        run_apply(auto_approve=True, json_output=True)

    data = json.loads(capsys.readouterr().out)
    assert "callback_errors" not in data


# ---------------------------------------------------------------------------
# --replace tests
# ---------------------------------------------------------------------------


@patch("tlumi.commands._common.resolve_targets")
@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_apply_with_replace(
    mock_find, mock_config, mock_merge, mock_get_stack, mock_resolve, capsys
):
    """apply --replace passes replace= to up call."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.up.return_value = mock_up_result(resource_changes={"replace": 1})
    mock_get_stack.return_value = mock_stack
    replace_urn = "urn:pulumi:default::p::aws:s3:BucketV2::b"
    mock_resolve.return_value = [replace_urn]

    run_apply(auto_approve=True, replace=["my-bucket"], json_output=True)

    call_kwargs = mock_stack.up.call_args[1]
    assert call_kwargs["replace"] == [replace_urn]
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["replace"] == [replace_urn]


@patch("tlumi.commands.apply.confirm", return_value=True)
@patch("tlumi.commands._common.resolve_targets")
@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_apply_interactive_with_replace(
    mock_find, mock_config, mock_merge, mock_get_stack, mock_resolve, mock_confirm, capsys
):
    """apply --replace in interactive mode passes replace= to both preview and up."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.preview.side_effect = emit_events(
        {"replace": 1}, kind="resource_pre", result=mock_preview_result()
    )
    mock_stack.up.side_effect = emit_events(
        {"replace": 1}, kind="res_outputs", result=mock_up_result()
    )
    mock_get_stack.return_value = mock_stack
    replace_urn = "urn:pulumi:default::p::aws:s3:BucketV2::b"
    mock_resolve.return_value = [replace_urn]

    run_apply(auto_approve=False, replace=["my-bucket"], json_output=False)

    preview_kwargs = mock_stack.preview.call_args[1]
    assert preview_kwargs["replace"] == [replace_urn]
    up_kwargs = mock_stack.up.call_args[1]
    assert up_kwargs["replace"] == [replace_urn]


# ---------------------------------------------------------------------------
# --plan (saved plan file) tests
# ---------------------------------------------------------------------------


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_apply_plan_file(mock_find, mock_config, mock_merge, mock_get_stack, tmp_path, capsys):
    """apply --plan passes plan= to up and skips preview."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.up.return_value = mock_up_result(resource_changes={"create": 1})
    mock_get_stack.return_value = mock_stack

    plan_path = str(tmp_path / "plan.json")
    run_apply(auto_approve=True, plan_file=plan_path, json_output=True)

    mock_stack.preview.assert_not_called()
    call_kwargs = mock_stack.up.call_args[1]
    assert call_kwargs["plan"] == str((tmp_path / "plan.json").resolve())
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["plan_file"] == str((tmp_path / "plan.json").resolve())


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_apply_plan_file_preserves_plan_time_config(
    mock_find, mock_config, mock_merge, mock_get_stack, tmp_path, capsys
):
    """apply --plan calls get_stack with reconcile_config=False.

    Without this, get_stack's stale-config cleanup removes plan-time variables
    (recorded in the sidecar but absent from the now-narrower merged variables)
    before up(plan=...), and Pulumi saved plans do not re-inject config, so
    config.require() fails at apply time.
    """
    mock_stack = MagicMock(spec=Stack)
    mock_stack.up.return_value = mock_up_result(resource_changes={"create": 1})
    mock_get_stack.return_value = mock_stack

    plan_path = str(tmp_path / "plan.json")
    run_apply(auto_approve=True, plan_file=plan_path, json_output=True)

    assert mock_get_stack.call_args.kwargs["reconcile_config"] is False


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_apply_without_plan_reconciles_config(
    mock_find, mock_config, mock_merge, mock_get_stack, capsys
):
    """A normal apply (no --plan) still reconciles stack config."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.up.return_value = mock_up_result(resource_changes={"create": 1})
    mock_get_stack.return_value = mock_stack

    run_apply(auto_approve=True, json_output=True)

    assert mock_get_stack.call_args.kwargs["reconcile_config"] is True


def test_apply_plan_file_conflicts_with_var():
    """apply --plan with --var raises TlumiError."""
    from tlumi.errors import TlumiError

    with pytest.raises(TlumiError, match="--plan cannot be combined"):
        run_apply(auto_approve=True, plan_file="plan.json", var=["k=v"], json_output=True)


def test_apply_plan_file_conflicts_with_var_file():
    """apply --plan with --var-file raises TlumiError."""
    from tlumi.errors import TlumiError

    with pytest.raises(TlumiError, match="--plan cannot be combined"):
        run_apply(
            auto_approve=True, plan_file="plan.json", var_file=["vars.yaml"], json_output=True
        )


def test_apply_plan_file_conflicts_with_target():
    """apply --plan with --target raises TlumiError."""
    from tlumi.errors import TlumiError

    with pytest.raises(TlumiError, match="--plan cannot be combined"):
        run_apply(auto_approve=True, plan_file="plan.json", target=["x"], json_output=True)


def test_apply_plan_file_conflicts_with_replace():
    """apply --plan with --replace raises TlumiError."""
    from tlumi.errors import TlumiError

    with pytest.raises(TlumiError, match="--plan cannot be combined"):
        run_apply(auto_approve=True, plan_file="plan.json", replace=["x"], json_output=True)


def test_apply_plan_file_conflicts_with_tlumi_var_env(monkeypatch):
    """apply --plan rejects a stray TLUMI_VAR_* env var that would override the plan (F12)."""
    from tlumi.errors import TlumiError

    monkeypatch.setenv("TLUMI_VAR_REGION", "eu-west-1")
    with pytest.raises(TlumiError, match="--plan cannot be combined"):
        run_apply(auto_approve=True, plan_file="plan.json", json_output=True)


# ---------------------------------------------------------------------------
# Composite outputs with nested secrets (audit finding 60, supersedes F2)
# ---------------------------------------------------------------------------


def _composite_up_stack() -> MagicMock:
    """Mock a stack exhibiting the real SDK behavior for composite outputs.

    result.outputs carries the nested secret as decrypted plaintext with
    secret=False (the SDK only sets secret=True for whole-output secrets,
    so there is nothing for value-level sanitization to catch); exported
    state keeps the secret sig wrapper.
    """
    mock_stack = MagicMock(spec=Stack)
    mock_stack.up.return_value = mock_up_result(
        outputs={
            "db": mock_output_value(
                {"host": "db.example.com", "password": "hunter2"}, secret=False
            ),
        },
    )
    mock_stack.export_stack.return_value = _stack_state(
        {"db": {"host": "db.example.com", "password": _secret_wrapper("hunter2")}}
    )
    return mock_stack


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_apply_json_masks_nested_secret(mock_find, mock_config, mock_merge, mock_get_stack, capsys):
    """apply --json masks a secret nested inside a non-secret dict output."""
    mock_get_stack.return_value = _composite_up_stack()

    run_apply(auto_approve=True, json_output=True)

    output = capsys.readouterr().out
    assert "hunter2" not in output  # nested secret masked
    data = json.loads(output)
    assert data["outputs"]["db"] == {"host": "db.example.com", "password": "(sensitive)"}


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_apply_human_masks_nested_secret(
    mock_find, mock_config, mock_merge, mock_get_stack, capsys
):
    """apply in human mode masks a secret nested inside a non-secret dict output."""
    mock_get_stack.return_value = _composite_up_stack()

    run_apply(auto_approve=True, json_output=False)

    output = capsys.readouterr().out
    assert "hunter2" not in output
    assert "(sensitive)" in output
    assert "db.example.com" in output  # non-secret sibling preserved


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_apply_show_secrets_reveals_nested_and_skips_export(
    mock_find, mock_config, mock_merge, mock_get_stack, capsys
):
    """apply --show-secrets shows result.outputs plaintext without reading state."""
    mock_stack = _composite_up_stack()
    mock_get_stack.return_value = mock_stack

    run_apply(auto_approve=True, json_output=True, show_secrets=True)

    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["outputs"]["db"]["password"] == "hunter2"
    mock_stack.export_stack.assert_not_called()


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_apply_plan_masks_nested_secret(
    mock_find, mock_config, mock_merge, mock_get_stack, tmp_path, capsys
):
    """apply --plan (separate code path) also masks nested secrets in outputs."""
    mock_stack = _composite_up_stack()
    mock_get_stack.return_value = mock_stack

    run_apply(auto_approve=True, plan_file=str(tmp_path / "plan.json"), json_output=True)

    output = capsys.readouterr().out
    assert "hunter2" not in output
    data = json.loads(output)
    assert data["outputs"]["db"] == {"host": "db.example.com", "password": "(sensitive)"}


# ---------------------------------------------------------------------------
# Interactive gate: output-only and import-only changes must reach the
# confirm prompt instead of early-returning "No changes" (audit finding;
# verified live: Pulumi previews an edited pulumi.export() as SAME ops
# everywhere, with the pending outputs only on the Stack res_outputs_event).
# ---------------------------------------------------------------------------


def _emit_stack_outputs_change(old_outputs, new_outputs, result=None):
    """Preview side-effect emitting only a Stack outputs event."""
    from helpers import stack_outputs_event

    def _side_effect(*args, **kwargs):
        on_event = kwargs.get("on_event")
        if on_event is not None:
            on_event(stack_outputs_event(old_outputs, new_outputs))
        return result

    return _side_effect


@patch("tlumi.commands.apply.confirm", return_value=False)
@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_apply_interactive_detects_output_only_change(
    mock_find, mock_config, mock_merge, mock_get_stack, mock_confirm, capsys
):
    """An output-only change reaches the confirm prompt, not 'No changes'."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.preview.side_effect = _emit_stack_outputs_change(
        {"greeting": "world"}, {"greeting": "mars"}, result=mock_preview_result({})
    )
    mock_get_stack.return_value = mock_stack

    run_apply(auto_approve=False, json_output=False)

    mock_confirm.assert_called_once()
    mock_stack.up.assert_not_called()  # confirmation declined
    output = capsys.readouterr().out
    assert "outputs will change" in output
    assert "up-to-date" not in output.lower()


def _scrubbed_secret_wrapper() -> dict:
    """The wrapper shape preview events carry for secret output values."""
    from tlumi.sanitize import _PULUMI_SECRET_SIG, _PULUMI_SECRET_VALUE

    return {_PULUMI_SECRET_SIG: _PULUMI_SECRET_VALUE, "ciphertext": "[secret]"}


@patch("tlumi.commands.apply.confirm", return_value=False)
@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_apply_interactive_no_changes_with_secret_outputs_prints_hint(
    mock_find, mock_config, mock_merge, mock_get_stack, mock_confirm, capsys
):
    """The 'No changes' gate carries the blind-spot hint when outputs hold secrets.

    Preview scrubs secret output values to the identical wrapper on both
    sides, so a changed secret export cannot be detected; the gate must not
    flatly assert up-to-date.
    """
    mock_stack = MagicMock(spec=Stack)
    mock_stack.preview.side_effect = _emit_stack_outputs_change(
        {"extra": _scrubbed_secret_wrapper()},
        {"extra": _scrubbed_secret_wrapper()},
        result=mock_preview_result({}),
    )
    mock_get_stack.return_value = mock_stack

    run_apply(auto_approve=False, json_output=False)

    mock_confirm.assert_not_called()
    mock_stack.up.assert_not_called()
    output = capsys.readouterr().out
    assert "No changes" in output
    assert "secret output values cannot be compared in preview" in output
    assert "--auto-approve" in output


@patch("tlumi.commands.apply.confirm", return_value=False)
@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_apply_interactive_no_changes_plain_outputs_no_hint(
    mock_find, mock_config, mock_merge, mock_get_stack, mock_confirm, capsys
):
    """Plain equal outputs keep the bare 'No changes' line (no blind-spot hint)."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.preview.side_effect = _emit_stack_outputs_change(
        {"greeting": "world"}, {"greeting": "world"}, result=mock_preview_result({})
    )
    mock_get_stack.return_value = mock_stack

    run_apply(auto_approve=False, json_output=False)

    mock_confirm.assert_not_called()
    mock_stack.up.assert_not_called()
    output = capsys.readouterr().out
    assert "No changes" in output
    assert "secret output values" not in output


@patch("tlumi.commands.apply.confirm", return_value=False)
@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_apply_interactive_detects_import_only_change(
    mock_find, mock_config, mock_merge, mock_get_stack, mock_confirm, capsys
):
    """An import-only preview shows '1 to import' and reaches the prompt."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.preview.side_effect = emit_events(
        {"import": 1}, kind="resource_pre", result=mock_preview_result({})
    )
    mock_get_stack.return_value = mock_stack

    run_apply(auto_approve=False, json_output=False)

    mock_confirm.assert_called_once()
    mock_stack.up.assert_not_called()
    output = capsys.readouterr().out
    assert "1 to import" in output


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_apply_zero_changes_no_dangling_resources_header(
    mock_find, mock_config, mock_merge, mock_get_stack, capsys
):
    """A zero-change auto-approve apply prints no empty 'Resources:' header."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.up.return_value = mock_up_result()
    mock_get_stack.return_value = mock_stack

    run_apply(auto_approve=True, json_output=False)

    output = capsys.readouterr().out
    assert "Resources:" not in output
    assert "No resources changed" in output


@patch("tlumi.commands._common.get_stack")
@patch("tlumi.commands._common.merge_variables")
@patch("tlumi.commands._common.load_config")
@patch("tlumi.commands._common.find_project_dir")
def test_apply_counts_imported_resources(
    mock_find, mock_config, mock_merge, mock_get_stack, capsys
):
    """Completed imports show in the summary ('1 imported') and JSON changes."""
    mock_stack = MagicMock(spec=Stack)
    mock_stack.up.side_effect = emit_events({"import": 1}, result=mock_up_result())
    mock_get_stack.return_value = mock_stack

    run_apply(auto_approve=True, json_output=True)

    data = json.loads(capsys.readouterr().out)
    assert data["changes"]["import"] == 1
