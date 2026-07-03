"""Tests for tlumi.commands.import_cmd: _suggest_class helper and run_import."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pulumi.automation import Stack

from tlumi.commands.import_cmd import (
    _extract_diagnostics,
    _parse_diagnostics_block,
    _suggest_class,
    run_import,
)
from tlumi.errors import Diagnostic, EngineError


def test_suggest_class_standard_type():
    """Standard Pulumi type returns the last component."""
    assert _suggest_class("aws:s3:BucketV2") == "BucketV2"


def test_suggest_class_single_part():
    """Single-part type returns itself."""
    assert _suggest_class("CustomResource") == "CustomResource"


def test_suggest_class_empty_string():
    """Empty string returns itself."""
    assert _suggest_class("") == ""


# ---------------------------------------------------------------------------
# run_import()
# ---------------------------------------------------------------------------


@patch("tlumi.commands.import_cmd.get_stack")
@patch("tlumi.commands.import_cmd.merge_variables")
@patch("tlumi.commands.import_cmd.load_config")
@patch("tlumi.commands.import_cmd.find_project_dir")
def test_run_import_happy_path(mock_find, mock_config, mock_merge, mock_get_stack, capsys):
    """run_import calls import_resources and prints success."""
    mock_config.return_value.entry = "infra.py"
    # merge_variables returns a new ProjectConfig now; pretend identity here.
    mock_merge.side_effect = lambda cfg, **_: cfg
    mock_stack = MagicMock(spec=Stack)
    mock_get_stack.return_value = mock_stack

    run_import("aws:s3:BucketV2", "my-bucket", "bucket-abc123")

    mock_stack.import_resources.assert_called_once()
    call_kwargs = mock_stack.import_resources.call_args
    resources_arg = (
        call_kwargs[1]["resources"] if "resources" in call_kwargs[1] else call_kwargs[0][0]
    )
    assert resources_arg[0]["type"] == "aws:s3:BucketV2"
    assert resources_arg[0]["name"] == "my-bucket"
    assert resources_arg[0]["id"] == "bucket-abc123"

    output = capsys.readouterr().out
    assert "Imported" in output
    assert "my-bucket" in output


@patch("tlumi.commands.import_cmd.get_stack")
@patch("tlumi.commands.import_cmd.merge_variables")
@patch("tlumi.commands.import_cmd.load_config")
@patch("tlumi.commands.import_cmd.find_project_dir")
def test_run_import_shows_next_steps(mock_find, mock_config, mock_merge, mock_get_stack, capsys):
    """run_import prints next steps with suggested class name."""
    mock_config.return_value.entry = "infra.py"
    mock_merge.side_effect = lambda cfg, **_: cfg
    mock_get_stack.return_value = MagicMock(spec=Stack)

    run_import("aws:s3:BucketV2", "my-bucket", "bucket-abc123")

    output = capsys.readouterr().out
    assert "Next steps" in output
    assert "BucketV2" in output


@patch("tlumi.commands.import_cmd.get_stack")
@patch("tlumi.commands.import_cmd.merge_variables")
@patch("tlumi.commands.import_cmd.load_config")
@patch("tlumi.commands.import_cmd.find_project_dir")
def test_run_import_passes_variables(mock_find, mock_config, mock_merge, mock_get_stack):
    """run_import passes var and var_file to merge_variables."""
    mock_config.return_value.entry = "infra.py"
    mock_merge.side_effect = lambda cfg, **_: cfg
    mock_get_stack.return_value = MagicMock(spec=Stack)

    run_import("aws:s3:BucketV2", "b", "id", var=["k=v"], var_file=["vars.yaml"])

    mock_merge.assert_called_once()
    _, kwargs = mock_merge.call_args
    assert kwargs.get("var") == ["k=v"] or mock_merge.call_args[0][1] == ["k=v"]


@patch("tlumi.commands.import_cmd.get_stack")
@patch("tlumi.commands.import_cmd.merge_variables")
@patch("tlumi.commands.import_cmd.load_config")
@patch("tlumi.commands.import_cmd.find_project_dir")
def test_run_import_engine_error(mock_find, mock_config, mock_merge, mock_get_stack):
    """run_import wraps CommandError via catch_engine_errors."""
    from pulumi.automation import CommandError

    mock_config.return_value.entry = "infra.py"
    mock_merge.side_effect = lambda cfg, **_: cfg
    mock_stack = MagicMock(spec=Stack)
    mock_result = MagicMock()
    mock_result.stdout = ""
    mock_result.stderr = "import failed"
    mock_result.code = 1
    mock_stack.import_resources.side_effect = CommandError(mock_result)
    mock_get_stack.return_value = mock_stack

    with pytest.raises(EngineError, match="Import failed"):
        run_import("aws:s3:BucketV2", "b", "id")


# ---------------------------------------------------------------------------
# protect flag (Terraform parity: imported resources are NOT protected)
# ---------------------------------------------------------------------------


@patch("tlumi.commands.import_cmd.get_stack")
@patch("tlumi.commands.import_cmd.merge_variables")
@patch("tlumi.commands.import_cmd.load_config")
@patch("tlumi.commands.import_cmd.find_project_dir")
def test_run_import_defaults_to_unprotected(mock_find, mock_config, mock_merge, mock_get_stack):
    """import_resources is called with protect=False by default.

    Pulumi's CLI defaults `pulumi import` to protect=true, and the SDK only
    forwards the flag when it is not None. Without an explicit protect=False,
    every imported resource lands in state with protect: true and a later
    `tlumi destroy` fails with no tlumi-native unprotect path.
    """
    mock_config.return_value.entry = "infra.py"
    mock_merge.side_effect = lambda cfg, **_: cfg
    mock_stack = MagicMock(spec=Stack)
    mock_get_stack.return_value = mock_stack

    run_import("aws:s3:BucketV2", "my-bucket", "bucket-abc123")

    _, kwargs = mock_stack.import_resources.call_args
    assert kwargs["protect"] is False


@patch("tlumi.commands.import_cmd.get_stack")
@patch("tlumi.commands.import_cmd.merge_variables")
@patch("tlumi.commands.import_cmd.load_config")
@patch("tlumi.commands.import_cmd.find_project_dir")
def test_run_import_protect_opt_in(mock_find, mock_config, mock_merge, mock_get_stack, capsys):
    """protect=True is forwarded to the SDK and a protection note is printed."""
    mock_config.return_value.entry = "infra.py"
    mock_merge.side_effect = lambda cfg, **_: cfg
    mock_stack = MagicMock(spec=Stack)
    mock_get_stack.return_value = mock_stack

    run_import("aws:s3:BucketV2", "my-bucket", "bucket-abc123", protect=True)

    _, kwargs = mock_stack.import_resources.call_args
    assert kwargs["protect"] is True

    output = capsys.readouterr().out
    assert "Protection enabled" in output


@patch("tlumi.commands.import_cmd.get_stack")
@patch("tlumi.commands.import_cmd.merge_variables")
@patch("tlumi.commands.import_cmd.load_config")
@patch("tlumi.commands.import_cmd.find_project_dir")
def test_run_import_no_protect_note_by_default(
    mock_find, mock_config, mock_merge, mock_get_stack, capsys
):
    """No protection note is printed for the default (unprotected) import."""
    mock_config.return_value.entry = "infra.py"
    mock_merge.side_effect = lambda cfg, **_: cfg
    mock_get_stack.return_value = MagicMock(spec=Stack)

    run_import("aws:s3:BucketV2", "my-bucket", "bucket-abc123")

    output = capsys.readouterr().out
    assert "Protection enabled" not in output


# ---------------------------------------------------------------------------
# diagnostics extraction (failed imports surface their cause like plan/apply)
# ---------------------------------------------------------------------------

# Shape captured from a real failing `tlumi import not:a:realtype foo bar`:
# str(CommandError) envelope wrapping the Pulumi CLI output.
_REAL_FAILURE_OUTPUT = (
    "\n code: 1"
    "\n stdout: Importing (default):\n"
    "\n"
    "    pulumi:pulumi:Stack improj-default running error: update failed:"
    " failed to validate provider config: could not find latest version"
    " for provider not: 404 HTTP error\n"
    "    pulumi:pulumi:Stack improj-default **failed** 1 error\n"
    "Diagnostics:\n"
    "  pulumi:pulumi:Stack (improj-default):\n"
    "    error: update failed: failed to validate provider config:"
    " could not find latest version for provider not: 404 HTTP error\n"
    "\n"
    "Resources:\n"
    "    1 unchanged\n"
    "    1 errored\n"
    "\n"
    "Duration: 1s\n"
    "\n stderr: "
)


def test_extract_diagnostics_parses_diagnostics_block():
    """The Pulumi Diagnostics: section is parsed into (resource, message) pairs."""
    diags = _extract_diagnostics(_REAL_FAILURE_OUTPUT)

    assert len(diags) == 1
    resource, message = diags[0]
    assert resource == "pulumi:pulumi:Stack (improj-default)"
    assert "could not find latest version for provider not" in message


def test_extract_diagnostics_multiple_resources():
    """Blank-line-separated diagnostic groups each become an entry."""
    output = (
        "\n code: 1\n stdout: \n"
        "Diagnostics:\n"
        "  aws:s3:Bucket (my-bucket):\n"
        "    error: creating bucket: BucketAlreadyExists\n"
        "\n"
        "  pulumi:pulumi:Stack (proj-default):\n"
        "    error: update failed\n"
        "\n"
        "Resources:\n"
        "\n stderr: "
    )
    diags = _extract_diagnostics(output)

    assert diags == [
        Diagnostic("aws:s3:Bucket (my-bucket)", "error: creating bucket: BucketAlreadyExists"),
        Diagnostic("pulumi:pulumi:Stack (proj-default)", "error: update failed"),
    ]


def test_extract_diagnostics_multiline_message():
    """Continuation lines of a diagnostic message are preserved."""
    output = (
        "Diagnostics:\n"
        "  aws:s3:Bucket (b):\n"
        "    error: 1 error occurred:\n"
        "        * creating bucket: AccessDenied\n"
        "Resources:\n"
    )
    diags = _extract_diagnostics(output)

    assert len(diags) == 1
    assert diags[0].message == "error: 1 error occurred:\n* creating bucket: AccessDenied"


def test_parse_diagnostics_block_stops_at_envelope():
    """The CommandResult ' stderr: ' envelope line (indent 1) ends the block."""
    lines = [
        "Diagnostics:",
        "  aws:s3:Bucket (b):",
        "    error: boom",
        " stderr: unrelated trailing content:",
    ]
    diags = _parse_diagnostics_block(lines)

    assert diags == [Diagnostic("aws:s3:Bucket (b)", "error: boom")]


def test_extract_diagnostics_fallback_to_error_lines():
    """Without a Diagnostics: block, explicit error: lines are surfaced, deduped."""
    output = (
        "\n code: 255"
        "\n stdout: "
        "\n stderr: error: passphrase must be set\n"
        "error: passphrase must be set\n"
        "some unrelated line\n"
    )
    diags = _extract_diagnostics(output)

    assert diags == [Diagnostic("", "error: passphrase must be set")]


def test_extract_diagnostics_empty_when_nothing_recognized():
    """Output with neither a Diagnostics block nor error: lines yields no entries."""
    assert _extract_diagnostics("\n code: 1\n stdout: \n stderr: ") == []


@patch("tlumi.commands.import_cmd.get_stack")
@patch("tlumi.commands.import_cmd.merge_variables")
@patch("tlumi.commands.import_cmd.load_config")
@patch("tlumi.commands.import_cmd.find_project_dir")
def test_run_import_failure_surfaces_diagnostics(
    mock_find, mock_config, mock_merge, mock_get_stack
):
    """A failed import raises EngineError WITH diagnostics parsed from CLI output.

    import_resources() has no on_event callback, so unlike plan/apply the
    EventHandler collects nothing; run_import must recover the cause from the
    raw output or the user sees only a bare 'Import failed.'.
    """
    from pulumi.automation import CommandError

    mock_config.return_value.entry = "infra.py"
    mock_merge.side_effect = lambda cfg, **_: cfg
    mock_stack = MagicMock(spec=Stack)
    mock_result = MagicMock()
    mock_result.stdout = ""
    mock_result.stderr = ""
    mock_result.code = 1
    mock_result.__str__ = lambda self: _REAL_FAILURE_OUTPUT  # type: ignore[method-assign]
    mock_stack.import_resources.side_effect = CommandError(mock_result)
    mock_get_stack.return_value = mock_stack

    with pytest.raises(EngineError) as exc_info:
        run_import("not:a:realtype", "foo", "bar")

    err = exc_info.value
    assert err.message == "Import failed."
    assert err.diagnostics, "diagnostics must be populated from the CLI output"
    assert "could not find latest version" in err.diagnostics[0].message
    assert err.full_output  # verbose path still has the raw output
