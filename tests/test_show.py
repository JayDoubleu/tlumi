"""Tests for tlumi.commands.show: run_show logic."""

from __future__ import annotations

import json
from unittest.mock import patch

from helpers import mock_state

from tlumi.commands.show import run_show

_STACK_RESOURCE = {
    "urn": "urn:pulumi:default::myproj::pulumi:pulumi:Stack::myproj-default",
    "type": "pulumi:pulumi:Stack",
}

_BUCKET_RESOURCE = {
    "urn": "urn:pulumi:default::myproj::aws:s3:BucketV2::my-bucket",
    "type": "aws:s3:BucketV2",
    "id": "my-bucket-abc123",
    "inputs": {"bucket": "my-app-assets-dev"},
    "outputs": {"arn": "arn:aws:s3:::my-app-assets-dev"},
}

_INSTANCE_RESOURCE = {
    "urn": "urn:pulumi:default::myproj::aws:ec2:Instance::web-server",
    "type": "aws:ec2:Instance",
    "id": "i-1234567890abcdef",
    "parent": "urn:pulumi:default::myproj::aws:s3:BucketV2::my-bucket",
    "dependencies": ["urn:pulumi:default::myproj::aws:s3:BucketV2::my-bucket"],
    "inputs": {},
    "outputs": {},
}


@patch("tlumi.commands.show.get_stack")
@patch("tlumi.commands.show.load_config")
@patch("tlumi.commands.show.find_project_dir")
def test_show_empty_state(mock_find, mock_config, mock_get_stack, capsys):
    """Empty state prints no-resources message."""
    mock_get_stack.return_value.export_stack.return_value = mock_state()
    run_show()
    output = capsys.readouterr().out
    assert "No resources in state" in output


@patch("tlumi.commands.show.get_stack")
@patch("tlumi.commands.show.load_config")
@patch("tlumi.commands.show.find_project_dir")
def test_show_skips_stack_resource(mock_find, mock_config, mock_get_stack, capsys):
    """Stack resources are filtered out."""
    mock_get_stack.return_value.export_stack.return_value = mock_state([_STACK_RESOURCE])
    run_show()
    output = capsys.readouterr().out
    assert "No resources in state" in output


@patch("tlumi.commands.show.get_stack")
@patch("tlumi.commands.show.load_config")
@patch("tlumi.commands.show.find_project_dir")
def test_show_displays_resources(mock_find, mock_config, mock_get_stack, capsys):
    """Resources are displayed with type, name, URN, ID, inputs, outputs."""
    mock_get_stack.return_value.export_stack.return_value = mock_state(
        [_STACK_RESOURCE, _BUCKET_RESOURCE]
    )
    run_show()
    output = capsys.readouterr().out
    assert "aws:s3:BucketV2" in output
    assert "my-bucket" in output
    assert "my-bucket-abc123" in output
    assert "my-app-assets-dev" in output
    assert "1 resource(s) in state" in output


@patch("tlumi.commands.show.get_stack")
@patch("tlumi.commands.show.load_config")
@patch("tlumi.commands.show.find_project_dir")
def test_show_displays_parent_and_deps(mock_find, mock_config, mock_get_stack, capsys):
    """Parent and dependency info is shown."""
    mock_get_stack.return_value.export_stack.return_value = mock_state(
        [_STACK_RESOURCE, _BUCKET_RESOURCE, _INSTANCE_RESOURCE]
    )
    run_show()
    output = capsys.readouterr().out
    assert "Parent:" in output
    assert "Depends on:" in output
    assert "2 resource(s) in state" in output


@patch("tlumi.commands.show.get_stack")
@patch("tlumi.commands.show.load_config")
@patch("tlumi.commands.show.find_project_dir")
def test_show_json_mode(mock_find, mock_config, mock_get_stack, capsys):
    """JSON mode emits raw state."""
    mock_get_stack.return_value.export_stack.return_value = mock_state(
        [_STACK_RESOURCE, _BUCKET_RESOURCE], version=3
    )
    run_show(json_output=True)
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["version"] == 3
    assert "deployment" in data
    assert len(data["deployment"]["resources"]) == 2


@patch("tlumi.commands.show.get_stack")
@patch("tlumi.commands.show.load_config")
@patch("tlumi.commands.show.find_project_dir")
def test_show_json_empty_state(mock_find, mock_config, mock_get_stack, capsys):
    """JSON mode with empty state emits valid JSON."""
    mock_get_stack.return_value.export_stack.return_value = mock_state(version=None)
    run_show(json_output=True)
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["version"] is None
    assert data["deployment"] == {}


# ---------------------------------------------------------------------------
# Secret masking tests
# ---------------------------------------------------------------------------

_SECRET_SIG = "4dabf18193072939515e22adb298388d"
_SECRET_VAL = "1b47061264138c4ac30d75fd1eb44270"

_RESOURCE_WITH_SECRET = {
    "urn": "urn:pulumi:default::myproj::aws:s3:BucketV2::my-bucket",
    "type": "aws:s3:BucketV2",
    "id": "my-bucket-abc123",
    "inputs": {
        "bucket": "my-app-assets-dev",
        "password": {_SECRET_SIG: _SECRET_VAL, "value": "[secret]"},
    },
    "outputs": {
        "arn": "arn:aws:s3:::my-app-assets-dev",
        "secret_key": {_SECRET_SIG: _SECRET_VAL, "value": "[secret]"},
    },
}


@patch("tlumi.commands.show.get_stack")
@patch("tlumi.commands.show.load_config")
@patch("tlumi.commands.show.find_project_dir")
def test_show_masks_secrets_by_default(mock_find, mock_config, mock_get_stack, capsys):
    """Secrets are masked by default in human output."""
    mock_get_stack.return_value.export_stack.return_value = mock_state(
        [_STACK_RESOURCE, _RESOURCE_WITH_SECRET]
    )
    run_show()
    output = capsys.readouterr().out
    assert "(sensitive)" in output
    assert "[secret]" not in output


@patch("tlumi.commands.show.get_stack")
@patch("tlumi.commands.show.load_config")
@patch("tlumi.commands.show.find_project_dir")
def test_show_json_masks_secrets_by_default(mock_find, mock_config, mock_get_stack, capsys):
    """Secrets are masked in JSON output by default."""
    mock_get_stack.return_value.export_stack.return_value = mock_state(
        [_STACK_RESOURCE, _RESOURCE_WITH_SECRET], version=3
    )
    run_show(json_output=True)
    output = capsys.readouterr().out
    json.loads(output)  # Validate JSON parsability
    assert "(sensitive)" in output
    assert _SECRET_SIG not in output


@patch("tlumi.commands.show.get_stack")
@patch("tlumi.commands.show.load_config")
@patch("tlumi.commands.show.find_project_dir")
def test_show_secrets_revealed_with_flag(mock_find, mock_config, mock_get_stack, capsys):
    """--show-secrets reveals secret values."""
    mock_get_stack.return_value.export_stack.return_value = mock_state(
        [_STACK_RESOURCE, _RESOURCE_WITH_SECRET]
    )
    run_show(show_secrets=True)
    output = capsys.readouterr().out
    assert "(sensitive)" not in output


# ---------------------------------------------------------------------------
# R7-6: JSON mode with --show-secrets
# ---------------------------------------------------------------------------


@patch("tlumi.commands.show.get_stack")
@patch("tlumi.commands.show.load_config")
@patch("tlumi.commands.show.find_project_dir")
def test_show_json_shows_secrets_with_flag(mock_find, mock_config, mock_get_stack, capsys):
    """--json --show-secrets shows raw sentinel data instead of masking."""
    mock_get_stack.return_value.export_stack.return_value = mock_state(
        [_STACK_RESOURCE, _RESOURCE_WITH_SECRET], version=3
    )
    run_show(json_output=True, show_secrets=True)
    output = capsys.readouterr().out
    json.loads(output)  # Validate JSON parsability
    assert "(sensitive)" not in output
    # The secret sentinel sig should still be in the raw output
    assert _SECRET_SIG in output


# ---------------------------------------------------------------------------
# R8-E2: Non-dict resource guard
# ---------------------------------------------------------------------------


@patch("tlumi.commands.show.get_stack")
@patch("tlumi.commands.show.load_config")
@patch("tlumi.commands.show.find_project_dir")
def test_show_non_dict_resources_ignored(mock_find, mock_config, mock_get_stack, capsys):
    """Non-dict entries in resources list are silently filtered out."""
    mock_get_stack.return_value.export_stack.return_value = mock_state(
        [_STACK_RESOURCE, "not-a-dict", _BUCKET_RESOURCE]
    )
    run_show()
    output = capsys.readouterr().out
    assert "aws:s3:BucketV2" in output
    assert "1 resource(s) in state" in output


# ---------------------------------------------------------------------------
# Hardening sweep: Pulumi bookkeeping (dunder) keys hidden from human display
# ---------------------------------------------------------------------------

_PROVIDER_WITH_DUNDER = {
    "urn": "urn:pulumi:default::myproj::pulumi:providers:aws::default",
    "type": "pulumi:providers:aws",
    "id": "prov-1",
    "inputs": {"__internal": {}},
    "outputs": {},
}

_BUCKET_WITH_DUNDER = {
    "urn": "urn:pulumi:default::myproj::aws:s3:BucketV2::my-bucket",
    "type": "aws:s3:BucketV2",
    "id": "my-bucket-abc123",
    "inputs": {"bucket": "my-app-assets-dev", "__internal": {}},
    "outputs": {"arn": "arn:aws:s3:::my-app-assets-dev", "__pulumi_raw_state_delta": {"x": 1}},
}


@patch("tlumi.commands.show.get_stack")
@patch("tlumi.commands.show.load_config")
@patch("tlumi.commands.show.find_project_dir")
def test_show_human_hides_dunder_keys(mock_find, mock_config, mock_get_stack, capsys):
    """Human-readable show hides top-level __* bookkeeping keys, keeps user keys."""
    mock_get_stack.return_value.export_stack.return_value = mock_state(
        [_STACK_RESOURCE, _BUCKET_WITH_DUNDER]
    )
    run_show()
    output = capsys.readouterr().out
    assert "__internal" not in output
    assert "__pulumi_raw_state_delta" not in output
    assert "bucket" in output
    assert "arn" in output


@patch("tlumi.commands.show.get_stack")
@patch("tlumi.commands.show.load_config")
@patch("tlumi.commands.show.find_project_dir")
def test_show_human_suppresses_dunder_only_section(mock_find, mock_config, mock_get_stack, capsys):
    """A resource whose inputs hold only bookkeeping keys shows no Inputs section."""
    mock_get_stack.return_value.export_stack.return_value = mock_state(
        [_STACK_RESOURCE, _PROVIDER_WITH_DUNDER]
    )
    run_show()
    output = capsys.readouterr().out
    assert "Inputs:" not in output
    assert "__internal" not in output


@patch("tlumi.commands.show.get_stack")
@patch("tlumi.commands.show.load_config")
@patch("tlumi.commands.show.find_project_dir")
def test_show_json_keeps_dunder_keys(mock_find, mock_config, mock_get_stack, capsys):
    """show --json keeps bookkeeping keys: it mirrors the state pull envelope."""
    mock_get_stack.return_value.export_stack.return_value = mock_state(
        [_STACK_RESOURCE, _BUCKET_WITH_DUNDER], version=3
    )
    run_show(json_output=True)
    data = json.loads(capsys.readouterr().out)
    bucket = [r for r in data["deployment"]["resources"] if r.get("type") == "aws:s3:BucketV2"][0]
    assert bucket["inputs"]["__internal"] == {}
    assert bucket["outputs"]["__pulumi_raw_state_delta"] == {"x": 1}
