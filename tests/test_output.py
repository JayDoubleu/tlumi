"""Tests for tlumi.commands.output: run_output secret masking and error handling."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from helpers import mock_output_value, mock_state
from pulumi.automation import Stack

from tlumi.errors import WorkspaceError


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


@patch("tlumi.commands.output.get_stack")
@patch("tlumi.commands.output.load_config")
@patch("tlumi.commands.output.find_project_dir")
def test_output_masks_secrets_in_json(mock_find, mock_config, mock_stack, capsys):
    """JSON output masks secret values by default (sourced from exported state)."""
    mock_find.return_value = "/fake"
    mock_config.return_value = MagicMock()
    stack = MagicMock(spec=Stack)
    stack.export_stack.return_value = _stack_state(
        {
            "url": "https://example.com",
            "password": _secret_wrapper("super-secret"),
        }
    )
    mock_stack.return_value = stack

    from tlumi.commands.output import run_output

    run_output(json_output=True, show_secrets=False)

    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["url"] == "https://example.com"
    assert data["password"] == "(sensitive)"
    assert "super-secret" not in output


@patch("tlumi.commands.output.get_stack")
@patch("tlumi.commands.output.load_config")
@patch("tlumi.commands.output.find_project_dir")
def test_output_shows_secrets_with_flag(mock_find, mock_config, mock_stack, capsys):
    """JSON output shows secrets when show_secrets=True (from stack.outputs())."""
    mock_find.return_value = "/fake"
    mock_config.return_value = MagicMock()
    stack = MagicMock(spec=Stack)
    stack.outputs.return_value = {
        "password": mock_output_value("super-secret", secret=True),
    }
    mock_stack.return_value = stack

    from tlumi.commands.output import run_output

    run_output(json_output=True, show_secrets=True)

    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["password"] == "super-secret"


@patch("tlumi.commands.output.get_stack")
@patch("tlumi.commands.output.load_config")
@patch("tlumi.commands.output.find_project_dir")
def test_output_named_not_found(mock_find, mock_config, mock_stack):
    """Requesting a non-existent output raises WorkspaceError."""
    mock_find.return_value = "/fake"
    mock_config.return_value = MagicMock()
    stack = MagicMock(spec=Stack)
    stack.export_stack.return_value = _stack_state({"url": "https://example.com"})
    mock_stack.return_value = stack

    from tlumi.commands.output import run_output

    with pytest.raises(WorkspaceError, match="Output 'missing' not found"):
        run_output(name="missing")


@patch("tlumi.commands.output.get_stack")
@patch("tlumi.commands.output.load_config")
@patch("tlumi.commands.output.find_project_dir")
def test_output_named_masks_secret(mock_find, mock_config, mock_stack, capsys):
    """Named output masks secret when show_secrets=False."""
    mock_find.return_value = "/fake"
    mock_config.return_value = MagicMock()
    stack = MagicMock(spec=Stack)
    stack.export_stack.return_value = _stack_state({"password": _secret_wrapper("secret-val")})
    mock_stack.return_value = stack

    from tlumi.commands.output import run_output

    run_output(name="password", json_output=True, show_secrets=False)

    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["password"] == "(sensitive)"
    assert "secret-val" not in output


@patch("tlumi.commands.output.get_stack")
@patch("tlumi.commands.output.load_config")
@patch("tlumi.commands.output.find_project_dir")
def test_output_empty_json(mock_find, mock_config, mock_stack, capsys):
    """Empty outputs in JSON mode returns {}."""
    mock_find.return_value = "/fake"
    mock_config.return_value = MagicMock()
    stack = MagicMock(spec=Stack)
    stack.export_stack.return_value = _stack_state({})
    mock_stack.return_value = stack

    from tlumi.commands.output import run_output

    run_output(json_output=True)

    output = capsys.readouterr().out
    assert json.loads(output) == {}


@patch("tlumi.commands.output.get_stack")
@patch("tlumi.commands.output.load_config")
@patch("tlumi.commands.output.find_project_dir")
def test_output_raw_mode(mock_find, mock_config, mock_stack, capsys):
    """Raw mode prints just the value string."""
    mock_find.return_value = "/fake"
    mock_config.return_value = MagicMock()
    stack = MagicMock(spec=Stack)
    stack.export_stack.return_value = _stack_state({"url": "https://example.com"})
    mock_stack.return_value = stack

    from tlumi.commands.output import run_output

    run_output(name="url", raw=True)

    output = capsys.readouterr().out.strip()
    assert output == "https://example.com"


@patch("tlumi.commands.output.get_stack")
@patch("tlumi.commands.output.load_config")
@patch("tlumi.commands.output.find_project_dir")
def test_output_raw_secret_prints_sensitive(mock_find, mock_config, mock_stack, capsys):
    """Raw mode on a masked secret prints (sensitive) and exits normally."""
    mock_find.return_value = "/fake"
    mock_config.return_value = MagicMock()
    stack = MagicMock(spec=Stack)
    stack.export_stack.return_value = _stack_state({"password": _secret_wrapper("hunter2")})
    mock_stack.return_value = stack

    from tlumi.commands.output import run_output

    run_output(name="password", raw=True)

    output = capsys.readouterr().out
    assert output == "(sensitive)\n"


# ---------------------------------------------------------------------------
# R7-3: Named output human-mode display
# ---------------------------------------------------------------------------


@patch("tlumi.commands.output.get_stack")
@patch("tlumi.commands.output.load_config")
@patch("tlumi.commands.output.find_project_dir")
def test_output_named_human_mode(mock_find, mock_config, mock_stack, capsys):
    """Named output in human mode prints 'name = value'."""
    mock_find.return_value = "/fake"
    mock_config.return_value = MagicMock()
    stack = MagicMock(spec=Stack)
    stack.export_stack.return_value = _stack_state({"url": "https://example.com"})
    mock_stack.return_value = stack

    from tlumi.commands.output import run_output

    run_output(name="url")

    output = capsys.readouterr().out
    assert "url" in output
    assert "https://example.com" in output


@patch("tlumi.commands.output.get_stack")
@patch("tlumi.commands.output.load_config")
@patch("tlumi.commands.output.find_project_dir")
def test_output_named_human_shows_secrets(mock_find, mock_config, mock_stack, capsys):
    """Named output human mode with show_secrets reveals value."""
    mock_find.return_value = "/fake"
    mock_config.return_value = MagicMock()
    stack = MagicMock(spec=Stack)
    stack.outputs.return_value = {
        "password": mock_output_value("super-secret", secret=True),
    }
    mock_stack.return_value = stack

    from tlumi.commands.output import run_output

    run_output(name="password", show_secrets=True)

    output = capsys.readouterr().out
    assert "super-secret" in output
    assert "(sensitive)" not in output


@patch("tlumi.commands.output.get_stack")
@patch("tlumi.commands.output.load_config")
@patch("tlumi.commands.output.find_project_dir")
def test_output_named_human_masks_secret(mock_find, mock_config, mock_stack, capsys):
    """Named output human mode masks secrets by default."""
    mock_find.return_value = "/fake"
    mock_config.return_value = MagicMock()
    stack = MagicMock(spec=Stack)
    stack.export_stack.return_value = _stack_state({"password": _secret_wrapper("super-secret")})
    mock_stack.return_value = stack

    from tlumi.commands.output import run_output

    run_output(name="password")

    output = capsys.readouterr().out
    assert "(sensitive)" in output
    assert "super-secret" not in output


# ---------------------------------------------------------------------------
# R7-4: Empty outputs human mode
# ---------------------------------------------------------------------------


@patch("tlumi.commands.output.get_stack")
@patch("tlumi.commands.output.load_config")
@patch("tlumi.commands.output.find_project_dir")
def test_output_empty_human_mode(mock_find, mock_config, mock_stack, capsys):
    """Empty outputs in human mode prints muted message."""
    mock_find.return_value = "/fake"
    mock_config.return_value = MagicMock()
    stack = MagicMock(spec=Stack)
    stack.export_stack.return_value = _stack_state({})
    mock_stack.return_value = stack

    from tlumi.commands.output import run_output

    run_output()

    output = capsys.readouterr().out
    assert "No outputs defined" in output


# ---------------------------------------------------------------------------
# R7-5: CommandError from state/output reads
# ---------------------------------------------------------------------------


@patch("tlumi.commands.output.get_stack")
@patch("tlumi.commands.output.load_config")
@patch("tlumi.commands.output.find_project_dir")
def test_output_export_error_raises_workspace_error(mock_find, mock_config, mock_stack):
    """CommandError from export_stack (masked mode) is wrapped in WorkspaceError."""
    from pulumi.automation import CommandError

    mock_find.return_value = "/fake"
    mock_config.return_value = MagicMock()
    stack = MagicMock(spec=Stack)
    stack.export_stack.side_effect = CommandError.__new__(CommandError)
    mock_stack.return_value = stack

    from tlumi.commands.output import run_output

    with pytest.raises(WorkspaceError, match="Failed to read state"):
        run_output()


@patch("tlumi.commands.output.get_stack")
@patch("tlumi.commands.output.load_config")
@patch("tlumi.commands.output.find_project_dir")
def test_output_command_error_raises_workspace_error(mock_find, mock_config, mock_stack):
    """CommandError from stack.outputs() (show-secrets mode) is wrapped in WorkspaceError."""
    from pulumi.automation import CommandError

    mock_find.return_value = "/fake"
    mock_config.return_value = MagicMock()
    stack = MagicMock(spec=Stack)
    stack.outputs.side_effect = CommandError.__new__(CommandError)
    mock_stack.return_value = stack

    from tlumi.commands.output import run_output

    with pytest.raises(WorkspaceError, match="Failed to read outputs"):
        run_output(show_secrets=True)


# ---------------------------------------------------------------------------
# R8-M2: Bracket-containing output values (markup safety)
# ---------------------------------------------------------------------------


@patch("tlumi.commands.output.get_stack")
@patch("tlumi.commands.output.load_config")
@patch("tlumi.commands.output.find_project_dir")
def test_output_raw_brackets_not_interpreted(mock_find, mock_config, mock_stack, capsys):
    """Raw mode does not interpret brackets as Rich markup."""
    mock_find.return_value = "/fake"
    mock_config.return_value = MagicMock()
    stack = MagicMock(spec=Stack)
    stack.export_stack.return_value = _stack_state({"config": "[bold]not-markup[/bold]"})
    mock_stack.return_value = stack

    from tlumi.commands.output import run_output

    run_output(name="config", raw=True)

    output = capsys.readouterr().out
    assert "[bold]not-markup[/bold]" in output


@patch("tlumi.commands.output.get_stack")
@patch("tlumi.commands.output.load_config")
@patch("tlumi.commands.output.find_project_dir")
def test_output_named_on_empty_stack_errors(mock_find, mock_config, mock_stack):
    """Requesting a named output on a stack with zero outputs errors (not exit 0) (#12)."""
    mock_find.return_value = "/fake"
    mock_config.return_value = MagicMock()
    stack = MagicMock(spec=Stack)
    stack.export_stack.return_value = _stack_state({})
    mock_stack.return_value = stack

    from tlumi.commands.output import run_output

    with pytest.raises(WorkspaceError, match="Output 'db_password' not found"):
        run_output(name="db_password")


def test_output_raw_requires_name():
    """--raw without an output NAME is rejected (#15)."""
    from tlumi.commands.output import run_output
    from tlumi.errors import TlumiError

    with pytest.raises(TlumiError, match="--raw requires an output NAME"):
        run_output(raw=True)


def test_output_raw_and_json_mutually_exclusive():
    """--raw and --json cannot be combined (#15)."""
    from tlumi.commands.output import run_output
    from tlumi.errors import TlumiError

    with pytest.raises(TlumiError, match="--raw and --json cannot be combined"):
        run_output(name="x", raw=True, json_output=True)


# ---------------------------------------------------------------------------
# Composite outputs with nested secrets (audit finding 60)
# ---------------------------------------------------------------------------


def _composite_stack(mock_stack) -> MagicMock:
    """Mock a stack exhibiting the real SDK behavior for composite outputs.

    stack.outputs() reports the nested secret as decrypted plaintext with
    secret=False (the SDK only sets secret=True for whole-output secrets);
    exported state keeps the secret sig wrapper for sanitization.
    """
    stack = MagicMock(spec=Stack)
    stack.outputs.return_value = {
        "db": mock_output_value(
            {"host": "example.com", "password": "hunter2-nested"}, secret=False
        ),
    }
    stack.export_stack.return_value = _stack_state(
        {"db": {"host": "example.com", "password": _secret_wrapper("hunter2-nested")}}
    )
    mock_stack.return_value = stack
    return stack


@patch("tlumi.commands.output.get_stack")
@patch("tlumi.commands.output.load_config")
@patch("tlumi.commands.output.find_project_dir")
def test_output_composite_nested_secret_masked_human(mock_find, mock_config, mock_stack, capsys):
    """A secret nested in a dict output is masked in the default listing."""
    mock_find.return_value = "/fake"
    mock_config.return_value = MagicMock()
    stack = _composite_stack(mock_stack)

    from tlumi.commands.output import run_output

    run_output()

    output = capsys.readouterr().out
    assert "hunter2-nested" not in output
    assert "(sensitive)" in output
    assert "example.com" in output
    # Masked mode must not consult the plaintext channel at all
    stack.outputs.assert_not_called()


@patch("tlumi.commands.output.get_stack")
@patch("tlumi.commands.output.load_config")
@patch("tlumi.commands.output.find_project_dir")
def test_output_composite_nested_secret_masked_json(mock_find, mock_config, mock_stack, capsys):
    """A secret nested in a dict output is masked in --json mode."""
    mock_find.return_value = "/fake"
    mock_config.return_value = MagicMock()
    _composite_stack(mock_stack)

    from tlumi.commands.output import run_output

    run_output(json_output=True)

    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["db"] == {"host": "example.com", "password": "(sensitive)"}
    assert "hunter2-nested" not in output


@patch("tlumi.commands.output.get_stack")
@patch("tlumi.commands.output.load_config")
@patch("tlumi.commands.output.find_project_dir")
def test_output_composite_nested_secret_masked_named_raw(
    mock_find, mock_config, mock_stack, capsys
):
    """A secret nested in a dict output is masked in named --raw mode."""
    mock_find.return_value = "/fake"
    mock_config.return_value = MagicMock()
    _composite_stack(mock_stack)

    from tlumi.commands.output import run_output

    run_output(name="db", raw=True)

    output = capsys.readouterr().out
    assert "hunter2-nested" not in output
    assert "(sensitive)" in output


@patch("tlumi.commands.output.get_stack")
@patch("tlumi.commands.output.load_config")
@patch("tlumi.commands.output.find_project_dir")
def test_output_raw_composite_dict_is_json(mock_find, mock_config, mock_stack, capsys):
    """--raw on a dict output emits compact JSON, not Python repr.

    str() on a dict prints single-quoted repr, which is neither JSON nor
    shell-consumable; the masked composite must round-trip through json.loads.
    """
    mock_find.return_value = "/fake"
    mock_config.return_value = MagicMock()
    _composite_stack(mock_stack)

    from tlumi.commands.output import run_output

    run_output(name="db", raw=True)

    output = capsys.readouterr().out
    assert output.endswith("\n")
    data = json.loads(output)
    assert data == {"host": "example.com", "password": "(sensitive)"}
    # Compact separators: single line, no space padding after separators.
    assert output.strip() == '{"host":"example.com","password":"(sensitive)"}'


@patch("tlumi.commands.output.get_stack")
@patch("tlumi.commands.output.load_config")
@patch("tlumi.commands.output.find_project_dir")
def test_output_raw_list_is_json(mock_find, mock_config, mock_stack, capsys):
    """--raw on a list output emits compact JSON."""
    mock_find.return_value = "/fake"
    mock_config.return_value = MagicMock()
    stack = MagicMock(spec=Stack)
    stack.export_stack.return_value = _stack_state({"zones": ["a", "b", "c"]})
    mock_stack.return_value = stack

    from tlumi.commands.output import run_output

    run_output(name="zones", raw=True)

    output = capsys.readouterr().out
    assert json.loads(output) == ["a", "b", "c"]
    assert output.strip() == '["a","b","c"]'


@patch("tlumi.commands.output.get_stack")
@patch("tlumi.commands.output.load_config")
@patch("tlumi.commands.output.find_project_dir")
def test_output_raw_scalar_stays_unquoted(mock_find, mock_config, mock_stack, capsys):
    """--raw scalars keep str() rendering: a plain string gains no JSON quotes."""
    mock_find.return_value = "/fake"
    mock_config.return_value = MagicMock()
    stack = MagicMock(spec=Stack)
    stack.export_stack.return_value = _stack_state({"count": 42, "url": "https://example.com"})
    mock_stack.return_value = stack

    from tlumi.commands.output import run_output

    run_output(name="count", raw=True)
    assert capsys.readouterr().out == "42\n"

    run_output(name="url", raw=True)
    assert capsys.readouterr().out == "https://example.com\n"


@patch("tlumi.commands.output.get_stack")
@patch("tlumi.commands.output.load_config")
@patch("tlumi.commands.output.find_project_dir")
def test_output_raw_bool_and_none_match_json_channels(mock_find, mock_config, mock_stack, capsys):
    """--raw booleans/None emit JSON tokens, not Python repr ('True'/'None').

    --raw is the documented shell-capture surface and must agree with the
    lowercase 'true'/'false'/'null' every other tlumi channel emits.
    """
    mock_find.return_value = "/fake"
    mock_config.return_value = MagicMock()
    stack = MagicMock(spec=Stack)
    stack.export_stack.return_value = _stack_state(
        {"enabled": True, "disabled": False, "empty": None}
    )
    mock_stack.return_value = stack

    from tlumi.commands.output import run_output

    run_output(name="enabled", raw=True)
    assert capsys.readouterr().out == "true\n"

    run_output(name="disabled", raw=True)
    assert capsys.readouterr().out == "false\n"

    run_output(name="empty", raw=True)
    assert capsys.readouterr().out == "null\n"


@patch("tlumi.commands.output.get_stack")
@patch("tlumi.commands.output.load_config")
@patch("tlumi.commands.output.find_project_dir")
def test_output_composite_show_secrets_reveals_nested(mock_find, mock_config, mock_stack, capsys):
    """--show-secrets reveals nested secrets from stack.outputs() plaintext."""
    mock_find.return_value = "/fake"
    mock_config.return_value = MagicMock()
    stack = _composite_stack(mock_stack)

    from tlumi.commands.output import run_output

    run_output(json_output=True, show_secrets=True)

    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["db"]["password"] == "hunter2-nested"
    # show-secrets mode reads the plaintext channel, not exported state
    stack.export_stack.assert_not_called()


# ---------------------------------------------------------------------------
# Emoji shortcode corruption (audit finding 3)
# ---------------------------------------------------------------------------


@patch("tlumi.commands.output.get_stack")
@patch("tlumi.commands.output.load_config")
@patch("tlumi.commands.output.find_project_dir")
def test_output_named_human_no_emoji_substitution(mock_find, mock_config, mock_stack, capsys):
    """Values with ':name:' sequences render verbatim in human mode."""
    mock_find.return_value = "/fake"
    mock_config.return_value = MagicMock()
    stack = MagicMock(spec=Stack)
    stack.export_stack.return_value = _stack_state({"tricky": "deploy :tada: done"})
    mock_stack.return_value = stack

    from tlumi.commands.output import run_output

    run_output(name="tricky")

    output = capsys.readouterr().out
    assert "deploy :tada: done" in output


@patch("tlumi.commands.output.get_stack")
@patch("tlumi.commands.output.load_config")
@patch("tlumi.commands.output.find_project_dir")
def test_output_listing_ipv6_not_corrupted(mock_find, mock_config, mock_stack, capsys):
    """IPv6/MAC values with ':ab:'-like groups are not emoji-substituted."""
    mock_find.return_value = "/fake"
    mock_config.return_value = MagicMock()
    stack = MagicMock(spec=Stack)
    stack.export_stack.return_value = _stack_state(
        {"addr": "2001:db8::ab:1", "mac": "44:ab:9d:00:11:22"}
    )
    mock_stack.return_value = stack

    from tlumi.commands.output import run_output

    run_output()

    output = capsys.readouterr().out
    assert "2001:db8::ab:1" in output
    assert "44:ab:9d:00:11:22" in output
