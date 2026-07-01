"""Tests for tlumi CLI argument parsing and command routing via Typer CliRunner."""

from __future__ import annotations

import importlib
import inspect
import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from tlumi.cli import app

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """Strip ANSI SGR sequences from CLI output.

    On GitHub Actions rich detects the CI environment and emits styled
    output even through CliRunner, splitting literals like "--var" with
    escape codes; assertions must run on the plain text.
    """
    return _ANSI_RE.sub("", text)


@pytest.fixture(autouse=True)
def _restore_cli_globals():
    """CliRunner invocations mutate cli module globals; restore them per test.

    _setup() flips _json_active and lowers the notice handler level; without
    restoration a --json invocation would leak into later tests.
    """
    import tlumi.cli as cli_mod

    orig_json = cli_mod._json_active
    orig_level = cli_mod._ws_handler.level
    yield
    cli_mod._json_active = orig_json
    cli_mod._ws_handler.setLevel(orig_level)


def test_windows_is_rejected_with_clear_message():
    """On Windows tlumi fails fast with a clear message, not a raw traceback."""
    with patch("tlumi.cli.sys.platform", "win32"):
        result = runner.invoke(app, ["version"])
    assert result.exit_code == 1
    assert "does not support Windows" in _plain(result.output)
    assert "WSL2" in _plain(result.output)


def test_version_flag():
    """--version prints version and exits 0."""
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "tlumi" in _plain(result.output)


def test_version_command():
    """'version' subcommand prints version."""
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "tlumi" in _plain(result.output)


def test_help():
    """--help lists available commands."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "plan" in _plain(result.output)
    assert "apply" in _plain(result.output)
    assert "destroy" in _plain(result.output)


def test_plan_help():
    """plan --help shows plan-specific options."""
    result = runner.invoke(app, ["plan", "--help"])
    assert result.exit_code == 0
    assert "--var" in _plain(result.output)
    assert "--target" in _plain(result.output)
    assert "--replace" in _plain(result.output)
    assert "--json" in _plain(result.output)


@patch("tlumi.cli._do_plan")
def test_plan_json_threads_args(mock_do_plan):
    """plan --json --var X=1 --target res forwards args to _do_plan."""
    result = runner.invoke(app, ["plan", "--json", "--var", "X=1", "--target", "my-res"])
    assert result.exit_code == 0
    mock_do_plan.assert_called_once()
    call_kwargs = mock_do_plan.call_args[1]
    assert call_kwargs["var"] == ["X=1"]
    assert call_kwargs["json_output"] is True


@patch("tlumi.cli._do_apply")
def test_apply_json_threads_args(mock_do_apply):
    """apply --json --auto-approve --var Y=2 forwards args to _do_apply."""
    result = runner.invoke(app, ["apply", "--json", "--auto-approve", "--var", "Y=2"])
    assert result.exit_code == 0
    mock_do_apply.assert_called_once()
    call_kwargs = mock_do_apply.call_args[1]
    assert call_kwargs["json_output"] is True


@patch("tlumi.cli._do_destroy")
def test_destroy_json_threads_args(mock_do_destroy):
    """destroy --json --auto-approve --var Z=3 forwards args."""
    result = runner.invoke(app, ["destroy", "--json", "--auto-approve", "--var", "Z=3"])
    assert result.exit_code == 0
    mock_do_destroy.assert_called_once()


@patch("tlumi.cli._do_plan")
def test_plan_error_exit_code(mock_do_plan):
    """TlumiError from plan yields exit code 1."""
    from tlumi.errors import TlumiError

    mock_do_plan.side_effect = TlumiError("test error")
    result = runner.invoke(app, ["plan", "--json"])
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert "error" in data


@patch("tlumi.cli._do_plan")
def test_verbose_before_command(mock_do_plan):
    """--verbose before command name works."""
    result = runner.invoke(app, ["--verbose", "plan", "--json"])
    assert result.exit_code == 0
    mock_do_plan.assert_called_once()


@patch("tlumi.cli._do_plan")
def test_verbose_after_command(mock_do_plan):
    """--verbose after command name also works."""
    result = runner.invoke(app, ["plan", "--verbose", "--json"])
    assert result.exit_code == 0
    mock_do_plan.assert_called_once()


# ---------------------------------------------------------------------------
# Option-wiring seam: every Typer command must forward its flags to the
# correct run_* parameters. These tests patch the run_* function itself (not
# the _do_* bridge), so a swapped positional argument, a mutated flag
# default, or a dropped option in cli.py fails here. Each command gets a
# defaults case and a flags-set case.
# ---------------------------------------------------------------------------

_PLAN_DEFAULTS: dict[str, Any] = {
    "var": None,
    "var_file": None,
    "target": None,
    "replace": None,
    "destroy": False,
    "out": None,
    "json_output": False,
}
_APPLY_DEFAULTS: dict[str, Any] = {
    "auto_approve": False,
    "var": None,
    "var_file": None,
    "target": None,
    "replace": None,
    "plan_file": None,
    "json_output": False,
    "show_secrets": False,
}
_DESTROY_DEFAULTS: dict[str, Any] = {
    "auto_approve": False,
    "var": None,
    "var_file": None,
    "target": None,
    "json_output": False,
}

WIRING_CASES: list[tuple[list[str], str, str, dict[str, Any]]] = [
    # init
    (["init"], "tlumi.commands.init", "run_init", {"project_dir": None, "name": None}),
    (
        ["init", "--name", "proj", "--path", "some/dir"],
        "tlumi.commands.init",
        "run_init",
        {"project_dir": Path("some/dir"), "name": "proj"},
    ),
    # clean (destructive booleans: a swap of auto_approve/include_state must fail)
    (
        ["clean"],
        "tlumi.commands.clean",
        "run_clean",
        {"auto_approve": False, "include_state": False},
    ),
    (
        ["clean", "--auto-approve"],
        "tlumi.commands.clean",
        "run_clean",
        {"auto_approve": True, "include_state": False},
    ),
    (
        ["clean", "--include-state"],
        "tlumi.commands.clean",
        "run_clean",
        {"auto_approve": False, "include_state": True},
    ),
    # plan
    (["plan"], "tlumi.commands.plan", "run_plan", dict(_PLAN_DEFAULTS)),
    (
        [
            "plan",
            "--var",
            "A=1",
            "--var",
            "B=2",
            "--var-file",
            "vars.yaml",
            "--target",
            "t1",
            "--replace",
            "r1",
            "--out",
            "plan.json",
            "--json",
        ],
        "tlumi.commands.plan",
        "run_plan",
        {
            "var": ["A=1", "B=2"],
            "var_file": ["vars.yaml"],
            "target": ["t1"],
            "replace": ["r1"],
            "destroy": False,
            "out": "plan.json",
            "json_output": True,
        },
    ),
    (
        ["plan", "--destroy"],
        "tlumi.commands.plan",
        "run_plan",
        {**_PLAN_DEFAULTS, "destroy": True},
    ),
    # apply
    (["apply"], "tlumi.commands.apply", "run_apply", dict(_APPLY_DEFAULTS)),
    (
        [
            "apply",
            "--auto-approve",
            "--var",
            "X=1",
            "--var-file",
            "v.yaml",
            "--target",
            "t",
            "--replace",
            "r",
            "--show-secrets",
            "--json",
        ],
        "tlumi.commands.apply",
        "run_apply",
        {
            "auto_approve": True,
            "var": ["X=1"],
            "var_file": ["v.yaml"],
            "target": ["t"],
            "replace": ["r"],
            "plan_file": None,
            "json_output": True,
            "show_secrets": True,
        },
    ),
    (
        ["apply", "--plan", "saved.json", "--auto-approve"],
        "tlumi.commands.apply",
        "run_apply",
        {**_APPLY_DEFAULTS, "plan_file": "saved.json", "auto_approve": True},
    ),
    # destroy
    (["destroy"], "tlumi.commands.destroy", "run_destroy", dict(_DESTROY_DEFAULTS)),
    (
        [
            "destroy",
            "--auto-approve",
            "--var",
            "X=1",
            "--var-file",
            "v.yaml",
            "--target",
            "t",
            "--json",
        ],
        "tlumi.commands.destroy",
        "run_destroy",
        {
            "auto_approve": True,
            "var": ["X=1"],
            "var_file": ["v.yaml"],
            "target": ["t"],
            "json_output": True,
        },
    ),
    # validate
    (
        ["validate"],
        "tlumi.commands.validate",
        "run_validate",
        {"var": None, "var_file": None, "json_output": False},
    ),
    (
        ["validate", "--var", "X=1", "--var-file", "v.yaml", "--json"],
        "tlumi.commands.validate",
        "run_validate",
        {"var": ["X=1"], "var_file": ["v.yaml"], "json_output": True},
    ),
    # refresh
    (
        ["refresh"],
        "tlumi.commands.refresh",
        "run_refresh",
        {"var": None, "var_file": None, "target": None, "json_output": False},
    ),
    (
        ["refresh", "--var", "X=1", "--var-file", "v.yaml", "--target", "t", "--json"],
        "tlumi.commands.refresh",
        "run_refresh",
        {"var": ["X=1"], "var_file": ["v.yaml"], "target": ["t"], "json_output": True},
    ),
    # import
    (
        ["import", "aws:s3:BucketV2", "my-bucket", "bucket-123"],
        "tlumi.commands.import_cmd",
        "run_import",
        {
            "resource_type": "aws:s3:BucketV2",
            "name": "my-bucket",
            "resource_id": "bucket-123",
            "var": None,
            "var_file": None,
            "protect": False,
        },
    ),
    (
        [
            "import",
            "aws:s3:BucketV2",
            "my-bucket",
            "bucket-123",
            "--var",
            "X=1",
            "--var-file",
            "v.yaml",
        ],
        "tlumi.commands.import_cmd",
        "run_import",
        {
            "resource_type": "aws:s3:BucketV2",
            "name": "my-bucket",
            "resource_id": "bucket-123",
            "var": ["X=1"],
            "var_file": ["v.yaml"],
            "protect": False,
        },
    ),
    (
        ["import", "aws:s3:BucketV2", "my-bucket", "bucket-123", "--protect"],
        "tlumi.commands.import_cmd",
        "run_import",
        {
            "resource_type": "aws:s3:BucketV2",
            "name": "my-bucket",
            "resource_id": "bucket-123",
            "var": None,
            "var_file": None,
            "protect": True,
        },
    ),
    # fmt
    (["fmt"], "tlumi.commands.fmt", "run_fmt", {"check": False}),
    (["fmt", "--check"], "tlumi.commands.fmt", "run_fmt", {"check": True}),
    # output
    (
        ["output"],
        "tlumi.commands.output",
        "run_output",
        {"name": None, "json_output": False, "raw": False, "show_secrets": False},
    ),
    (
        ["output", "myout", "--json", "--show-secrets"],
        "tlumi.commands.output",
        "run_output",
        {"name": "myout", "json_output": True, "raw": False, "show_secrets": True},
    ),
    (
        ["output", "myout", "--raw"],
        "tlumi.commands.output",
        "run_output",
        {"name": "myout", "json_output": False, "raw": True, "show_secrets": False},
    ),
    # show
    (
        ["show"],
        "tlumi.commands.show",
        "run_show",
        {"json_output": False, "show_secrets": False},
    ),
    (
        ["show", "--json", "--show-secrets"],
        "tlumi.commands.show",
        "run_show",
        {"json_output": True, "show_secrets": True},
    ),
    # deps
    (
        ["deps", "add", "pulumi-aws", "pulumi-gcp"],
        "tlumi.commands.deps",
        "run_deps_add",
        {"packages": ["pulumi-aws", "pulumi-gcp"]},
    ),
    (["deps", "install"], "tlumi.commands.deps", "run_deps_install", {}),
    (["deps", "list"], "tlumi.commands.deps", "run_deps_list", {}),
    # state
    (["state", "list"], "tlumi.commands.state", "run_state_list", {"json_output": False}),
    (["state", "list", "--json"], "tlumi.commands.state", "run_state_list", {"json_output": True}),
    (
        ["state", "show", "my-res"],
        "tlumi.commands.state",
        "run_state_show",
        {"resource": "my-res", "json_output": False, "show_secrets": False},
    ),
    (
        ["state", "show", "my-res", "--json", "--show-secrets"],
        "tlumi.commands.state",
        "run_state_show",
        {"resource": "my-res", "json_output": True, "show_secrets": True},
    ),
    (
        ["state", "rm", "my-res"],
        "tlumi.commands.state",
        "run_state_rm",
        {"resource": "my-res", "auto_approve": False},
    ),
    (
        ["state", "rm", "my-res", "--auto-approve"],
        "tlumi.commands.state",
        "run_state_rm",
        {"resource": "my-res", "auto_approve": True},
    ),
    (
        ["state", "mv", "src-res", "dst-res"],
        "tlumi.commands.state",
        "run_state_mv",
        {"source": "src-res", "destination": "dst-res", "auto_approve": False},
    ),
    (
        ["state", "mv", "src-res", "dst-res", "--auto-approve"],
        "tlumi.commands.state",
        "run_state_mv",
        {"source": "src-res", "destination": "dst-res", "auto_approve": True},
    ),
    (["state", "pull"], "tlumi.commands.state", "run_state_pull", {}),
    (
        ["state", "push", "f.json"],
        "tlumi.commands.state",
        "run_state_push",
        {"file_path": "f.json", "auto_approve": False, "json_output": False},
    ),
    (
        ["state", "push", "f.json", "--auto-approve", "--json"],
        "tlumi.commands.state",
        "run_state_push",
        {"file_path": "f.json", "auto_approve": True, "json_output": True},
    ),
    (["state", "unlock"], "tlumi.commands.cancel", "run_cancel", {}),
]


@pytest.mark.parametrize(
    ("argv", "module_path", "func_name", "expected"),
    WIRING_CASES,
    ids=[" ".join(case[0]) for case in WIRING_CASES],
)
def test_command_wiring(argv, module_path, func_name, expected):
    """CLI flags reach the run_* function with the exact expected values."""
    module = importlib.import_module(module_path)
    sig = inspect.signature(getattr(module, func_name))
    with patch.object(module, func_name) as mock_fn:
        result = runner.invoke(app, argv)
    assert result.exit_code == 0, f"{argv}: exit {result.exit_code}\n{result.output}"
    assert mock_fn.call_count == 1, f"{argv}: expected exactly one call to {func_name}"
    call = mock_fn.call_args
    # Bind against the real signature so positional-vs-keyword call style in
    # the _do_* bridge does not matter; only the semantic mapping is checked.
    bound = sig.bind(*call.args, **call.kwargs)
    bound.apply_defaults()
    assert dict(bound.arguments) == expected


# ---------------------------------------------------------------------------
# Unknown-command suggestions: typer >= 0.20 used to append its own
# "Did you mean" onto click >= 8.3's, doubling the message on every typo.
# Exactly one layer must stay on: suggest_commands=False is passed only when
# click >= 8.3 provides its own native suggestion, otherwise typer's remains.
# ---------------------------------------------------------------------------


def _expected_suggestion_count() -> int:
    """1 when either click (>= 8.3) or typer (>= 0.20) can suggest, else 0.

    Only the legacy floor combo (typer < 0.20 with click < 8.3) has no
    suggestion layer at all; every other combination must show exactly one.
    Asserting == (not <=) makes a total loss of suggestions fail the suite.
    """
    from tlumi.cli import _CLICK_HAS_NATIVE_SUGGESTIONS, _TYPER_HAS_SUGGEST_COMMANDS

    return 1 if (_CLICK_HAS_NATIVE_SUGGESTIONS or _TYPER_HAS_SUGGEST_COMMANDS) else 0


def test_unknown_command_suggestion_shown_exactly_once():
    """A typo'd command shows exactly one 'Did you mean' suggestion.

    CliRunner's ``result.output`` already merges stderr, so count on it
    alone; adding ``result.stderr`` would double-count a single message.
    """
    result = runner.invoke(app, ["plann"])
    assert result.exit_code == 2
    assert _plain(result.output).count("Did you mean") == _expected_suggestion_count()


def test_unknown_subcommand_suggestion_shown_exactly_once():
    """A typo'd subcommand (state lst) also shows exactly one suggestion."""
    result = runner.invoke(app, ["state", "lst"])
    assert result.exit_code == 2
    assert _plain(result.output).count("Did you mean") == _expected_suggestion_count()


# ---------------------------------------------------------------------------
# clean help text must match actual behavior (state preserved by default)
# ---------------------------------------------------------------------------


def test_clean_help_says_state_is_kept():
    """clean --help describes the default (caches only, state kept)."""
    result = runner.invoke(app, ["clean", "--help"])
    assert result.exit_code == 0
    normalized = " ".join(_plain(result.output).split())
    assert "state and backups are kept" in normalized
    # The old, wrong claim that state is removed by default must not return.
    assert "(venv, state, caches)" not in normalized
