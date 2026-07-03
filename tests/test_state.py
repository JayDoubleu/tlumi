"""Tests for tlumi.commands.state: pull, push, backup security."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest
from helpers import mock_state
from pulumi.automation import CommandError, Stack

from tlumi.commands.state import (
    _MAX_BACKUPS,
    _provider_matches,
    _rotate_backups,
    run_state_list,
    run_state_mv,
    run_state_pull,
    run_state_push,
    run_state_rm,
    run_state_show,
)
from tlumi.errors import WorkspaceError


@pytest.fixture(autouse=True)
def _stub_ciphertext_backup_export():
    """Route the backup's ciphertext export to whatever the tests stub for reads.

    ``_create_backup`` sources its snapshot from ``export_stack_no_secrets`` (a
    real ``pulumi stack export`` CLI call) so decrypted secrets never land on
    disk. Unit tests cannot run that CLI; they stub state reads either by
    setting ``stack.export_stack`` or by patching ``safe_export_stack``. Mirror
    the ciphertext export to ``safe_export_stack`` (looked up at call time so a
    per-test ``@patch`` is honoured) so both stubbing styles keep working; for
    the secret-free fixtures used here the backup content is identical either
    way. ``test_backup_does_not_persist_decrypted_secrets`` overrides this to
    supply a distinct ciphertext deployment.
    """
    from tlumi.commands import state as state_mod

    def _mirror(stack):
        return state_mod.safe_export_stack(stack)

    with patch("tlumi.commands.state.export_stack_no_secrets", side_effect=_mirror):
        yield


_RESOURCES = [
    {
        "urn": "urn:pulumi:default::myproj::aws:s3:BucketV2::my-bucket",
        "type": "aws:s3:BucketV2",
        "id": "my-bucket-abc123",
        "inputs": {"bucket": "my-bucket"},
        "outputs": {"arn": "arn:aws:s3:::my-bucket"},
    },
    {
        "urn": "urn:pulumi:default::myproj::aws:ec2:Instance::web-server",
        "type": "aws:ec2:Instance",
        "id": "i-1234567890abcdef",
        "inputs": {},
        "outputs": {},
    },
]


# --- state pull tests ---


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_pull_outputs_json(mock_find, mock_config, mock_get_stack, capsys):
    """state pull outputs valid JSON with version and deployment."""
    mock_get_stack.return_value.export_stack.return_value = mock_state(
        [{"type": "aws:s3:BucketV2", "urn": "urn:pulumi:default::p::aws:s3:BucketV2::b"}],
        version=3,
    )
    run_state_pull()
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["version"] == 3
    assert "resources" in data["deployment"]


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_pull_empty_state(mock_find, mock_config, mock_get_stack, capsys):
    """state pull with empty state outputs valid JSON."""
    mock_get_stack.return_value.export_stack.return_value = mock_state(version=None)
    run_state_pull()
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["version"] is None
    assert data["deployment"] == {}


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_pull_quiet_mode(mock_find, mock_config, mock_get_stack):
    """state pull calls get_stack with quiet=True."""
    mock_get_stack.return_value.export_stack.return_value = mock_state()
    run_state_pull()
    mock_get_stack.assert_called_once()
    _, kwargs = mock_get_stack.call_args
    assert kwargs.get("quiet") is True


# --- state push tests ---


def test_push_file_not_found():
    """state push raises WorkspaceError for missing file."""
    with pytest.raises(WorkspaceError, match="File not found"):
        run_state_push("/nonexistent/file.json", auto_approve=True)


def test_push_invalid_json(tmp_path):
    """state push raises WorkspaceError for invalid JSON."""
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("not json {{{")
    with pytest.raises(WorkspaceError, match="Invalid JSON"):
        run_state_push(str(bad_file), auto_approve=True)


def test_push_missing_keys(tmp_path):
    """state push raises WorkspaceError when version/deployment keys are missing."""
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(json.dumps({"foo": "bar"}))
    with pytest.raises(WorkspaceError, match="missing 'version' or 'deployment'"):
        run_state_push(str(bad_file), auto_approve=True)


def test_push_deployment_not_dict(tmp_path):
    """state push raises WorkspaceError when deployment is not a dict."""
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(json.dumps({"version": 3, "deployment": "not a dict"}))
    with pytest.raises(WorkspaceError, match="'deployment' must be a JSON object"):
        run_state_push(str(bad_file), auto_approve=True)


def test_push_json_mode_requires_auto_approve(tmp_path):
    """state push in JSON mode without --auto-approve raises error."""
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"version": 3, "deployment": {}}))
    with pytest.raises(WorkspaceError, match="JSON mode requires --auto-approve"):
        run_state_push(str(state_file), auto_approve=False, json_output=True)


def test_push_not_a_dict(tmp_path):
    """state push raises WorkspaceError when file contains a non-dict JSON."""
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(json.dumps([1, 2, 3]))
    with pytest.raises(WorkspaceError, match="expected a JSON object"):
        run_state_push(str(bad_file), auto_approve=True)


def test_push_version_negative_rejected(tmp_path):
    """state push rejects zero/negative version: Pulumi has only ever shipped 1, 2, 3."""
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(json.dumps({"version": -1, "deployment": {}}))
    with pytest.raises(WorkspaceError, match="'version' must be >= 1"):
        run_state_push(str(bad_file), auto_approve=True)


def test_push_version_bool_rejected(tmp_path):
    """A JSON boolean for version (Python's bool subclasses int) is rejected."""
    bad_file = tmp_path / "bad.json"
    # Use raw JSON to ensure `true` makes it through json.loads as a bool
    bad_file.write_text('{"version": true, "deployment": {}}')
    with pytest.raises(WorkspaceError, match="'version' must be an integer"):
        run_state_push(str(bad_file), auto_approve=True)


def test_push_urn_missing_prefix_rejected(tmp_path):
    """A resource whose urn does not start with 'urn:pulumi:' is rejected up front."""
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(
        json.dumps(
            {
                "version": 3,
                "deployment": {
                    "resources": [{"urn": "not-a-pulumi-urn", "type": "aws:s3:BucketV2"}]
                },
            }
        )
    )
    with pytest.raises(WorkspaceError, match="does not look like a Pulumi URN"):
        run_state_push(str(bad_file), auto_approve=True)


def test_push_dangling_parent_reference_rejected(tmp_path):
    """Referential integrity: parent must point at a URN declared in the same envelope."""
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(
        json.dumps(
            {
                "version": 3,
                "deployment": {
                    "resources": [
                        {
                            "urn": "urn:pulumi:default::p::aws:s3:Bucket::child",
                            "type": "aws:s3:Bucket",
                            "parent": "urn:pulumi:default::p::aws:s3:Bucket::missing",
                        }
                    ]
                },
            }
        )
    )
    with pytest.raises(WorkspaceError, match="parent references unknown URN"):
        run_state_push(str(bad_file), auto_approve=True)


def test_push_dangling_dependency_reference_rejected(tmp_path):
    """Referential integrity: dependency must point at a URN declared in the same envelope."""
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(
        json.dumps(
            {
                "version": 3,
                "deployment": {
                    "resources": [
                        {
                            "urn": "urn:pulumi:default::p::aws:s3:Bucket::a",
                            "type": "aws:s3:Bucket",
                            "dependencies": ["urn:pulumi:default::p::aws:s3:Bucket::nope"],
                        }
                    ]
                },
            }
        )
    )
    with pytest.raises(WorkspaceError, match="dependencies references unknown URN"):
        run_state_push(str(bad_file), auto_approve=True)


def test_push_dangling_provider_reference_rejected(tmp_path):
    """Referential integrity: provider URN must be declared in the envelope (sans ::id suffix)."""
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(
        json.dumps(
            {
                "version": 3,
                "deployment": {
                    "resources": [
                        {
                            "urn": "urn:pulumi:default::p::aws:s3:Bucket::a",
                            "type": "aws:s3:Bucket",
                            "provider": "urn:pulumi:default::p::pulumi:providers:aws::default::abc",
                        }
                    ]
                },
            }
        )
    )
    with pytest.raises(WorkspaceError, match="provider references unknown URN"):
        run_state_push(str(bad_file), auto_approve=True)


def test_push_dangling_propertydeps_reference_rejected(tmp_path):
    """Referential integrity: propertyDependencies entries must point at declared URNs."""
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(
        json.dumps(
            {
                "version": 3,
                "deployment": {
                    "resources": [
                        {
                            "urn": "urn:pulumi:default::p::aws:s3:Bucket::a",
                            "type": "aws:s3:Bucket",
                            "propertyDependencies": {
                                "tags": ["urn:pulumi:default::p::aws:s3:Bucket::ghost"]
                            },
                        }
                    ]
                },
            }
        )
    )
    with pytest.raises(WorkspaceError, match="propertyDependencies.*unknown URN"):
        run_state_push(str(bad_file), auto_approve=True)


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_push_success(mock_find, mock_config, mock_get_stack, tmp_path, capsys):
    """state push with valid file imports state and creates backup."""
    config = MagicMock()
    config.name = "p"
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state(version=2)
    mock_get_stack.return_value = mock_stack

    state_data = {
        "version": 3,
        "deployment": {
            "resources": [
                {
                    "type": "pulumi:pulumi:Stack",
                    "urn": "urn:pulumi:default::p::pulumi:pulumi:Stack::p-default",
                },
                {"type": "aws:s3:BucketV2", "urn": "urn:pulumi:default::p::aws:s3:BucketV2::b"},
            ]
        },
    }
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state_data))

    run_state_push(str(state_file), auto_approve=True)

    # Verify import_stack was called with the deployment from the SOURCE file,
    # not the pre-push state (a miswired restore would silently import the wrong
    # snapshot while reporting success). state push is the documented recovery
    # primitive, so the payload wiring is the load-bearing assertion here.
    mock_stack.import_stack.assert_called_once()
    imported = mock_stack.import_stack.call_args[0][0]
    assert imported.version == state_data["version"]
    assert imported.deployment == state_data["deployment"]

    # Verify backup was created
    backups = list((tmp_path / ".tlumi" / "backups").glob("state_push_*.json"))
    assert len(backups) == 1

    output = capsys.readouterr().out
    assert "State replaced successfully" in output
    assert "1 resource(s)" in output


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_push_json_output(mock_find, mock_config, mock_get_stack, tmp_path, capsys):
    """state push --json --auto-approve emits JSON result."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state(version=2)
    mock_get_stack.return_value = mock_stack

    state_data = {"version": 3, "deployment": {"resources": []}}
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state_data))

    run_state_push(str(state_file), auto_approve=True, json_output=True)

    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["pushed"] is True
    assert data["resources"] == 0
    assert "backup" in data
    assert "diff" in data
    assert data["diff"]["added"] == 0
    assert data["diff"]["removed"] == 0
    assert data["diff"]["unchanged"] == 0


# --- state show secret masking tests ---

_SECRET_SIG = "4dabf18193072939515e22adb298388d"
_SECRET_VAL = "1b47061264138c4ac30d75fd1eb44270"

# Exported state (Automation API export_stack runs `stack export --show-secrets`)
# stores each secret as the sig wrapper with a JSON-encoded plaintext field.
_RESOURCE_WITH_SECRET = {
    "urn": "urn:pulumi:default::myproj::aws:s3:BucketV2::my-bucket",
    "type": "aws:s3:BucketV2",
    "id": "my-bucket-abc123",
    "inputs": {
        "bucket": "my-bucket",
        "password": {_SECRET_SIG: _SECRET_VAL, "plaintext": json.dumps("hunter2-input")},
    },
    "outputs": {
        "arn": "arn:aws:s3:::my-bucket",
        "secret_key": {_SECRET_SIG: _SECRET_VAL, "plaintext": json.dumps("hunter2-output")},
    },
}


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_show_masks_secrets_by_default(mock_find, mock_config, mock_get_stack, capsys):
    """state show masks secret values by default."""
    mock_get_stack.return_value.export_stack.return_value = mock_state([_RESOURCE_WITH_SECRET])
    run_state_show("my-bucket")
    output = capsys.readouterr().out
    assert "(sensitive)" in output
    assert "hunter2-input" not in output
    assert "hunter2-output" not in output


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_show_json_masks_secrets_by_default(mock_find, mock_config, mock_get_stack, capsys):
    """state show --json masks secret values by default."""
    mock_get_stack.return_value.export_stack.return_value = mock_state([_RESOURCE_WITH_SECRET])
    run_state_show("my-bucket", json_output=True)
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["inputs"]["password"] == "(sensitive)"
    assert data["outputs"]["secret_key"] == "(sensitive)"


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_show_reveals_secrets_with_flag(mock_find, mock_config, mock_get_stack, capsys):
    """state show --show-secrets shows decoded plaintext, not the sig wrapper."""
    mock_get_stack.return_value.export_stack.return_value = mock_state([_RESOURCE_WITH_SECRET])
    run_state_show("my-bucket", show_secrets=True)
    output = capsys.readouterr().out
    assert "(sensitive)" not in output
    assert "hunter2-input" in output
    assert "hunter2-output" in output
    # The internal wrapper envelope must not leak into human display.
    assert _SECRET_SIG not in output
    assert "plaintext" not in output


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_show_secrets_flag_ciphertext_only_stays_masked(
    mock_find, mock_config, mock_get_stack, capsys
):
    """--show-secrets on a ciphertext-only wrapper masks instead of dumping base64."""
    resource = {
        "urn": "urn:pulumi:default::myproj::aws:s3:BucketV2::my-bucket",
        "type": "aws:s3:BucketV2",
        "id": "b-1",
        "inputs": {"password": {_SECRET_SIG: _SECRET_VAL, "ciphertext": "v1:AAAA:garbage"}},
        "outputs": {},
    }
    mock_get_stack.return_value.export_stack.return_value = mock_state([resource])
    run_state_show("my-bucket", show_secrets=True)
    output = capsys.readouterr().out
    assert "(sensitive)" in output
    assert "v1:AAAA:garbage" not in output
    assert _SECRET_SIG not in output


# --- state rm reference cleanup tests ---

_PROVIDER_URN = "urn:pulumi:default::myproj::pulumi:providers:aws::my-provider"
_BUCKET_URN = "urn:pulumi:default::myproj::aws:s3:BucketV2::my-bucket"

_PROVIDER_RESOURCE = {
    "urn": _PROVIDER_URN,
    "type": "pulumi:providers:aws",
    "id": "prov-123",
}

_BUCKET_WITH_PROVIDER = {
    "urn": _BUCKET_URN,
    "type": "aws:s3:BucketV2",
    "id": "my-bucket-abc123",
    "provider": _PROVIDER_URN,
    "inputs": {"bucket": "my-bucket"},
    "outputs": {},
}

_BUCKET_WITH_PROP_DEPS = {
    "urn": _BUCKET_URN,
    "type": "aws:s3:BucketV2",
    "id": "my-bucket-abc123",
    "propertyDependencies": {
        "bucket": [_PROVIDER_URN, "urn:pulumi:default::myproj::other::other-res"],
        "acl": [_PROVIDER_URN],
    },
    "inputs": {"bucket": "my-bucket"},
    "outputs": {},
}


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_rm_cleans_provider_reference(mock_find, mock_config, mock_get_stack, tmp_path):
    """state rm removes provider reference from dependent resources."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    stack_resource = {
        "urn": "urn:pulumi:default::myproj::pulumi:pulumi:Stack::myproj-default",
        "type": "pulumi:pulumi:Stack",
    }
    all_resources = [stack_resource, _PROVIDER_RESOURCE, _BUCKET_WITH_PROVIDER]

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state(all_resources, version=3)
    mock_get_stack.return_value = mock_stack

    run_state_rm("my-provider", auto_approve=True)

    # Check that import_stack was called with cleaned resources
    call_args = mock_stack.import_stack.call_args
    imported_state = call_args[0][0]
    remaining = imported_state.deployment["resources"]
    # Provider should be removed
    urns = [r["urn"] for r in remaining]
    assert _PROVIDER_URN not in urns
    # Bucket should have provider key removed
    bucket = [r for r in remaining if r.get("type") == "aws:s3:BucketV2"][0]
    assert "provider" not in bucket


_BUCKET_WITH_PROVIDER_ID = {
    "urn": _BUCKET_URN,
    "type": "aws:s3:BucketV2",
    "id": "my-bucket-abc123",
    "provider": f"{_PROVIDER_URN}::prov-123",
    "inputs": {"bucket": "my-bucket"},
    "outputs": {},
}


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_rm_cleans_provider_reference_with_id_suffix(
    mock_find, mock_config, mock_get_stack, tmp_path
):
    """state rm removes provider reference even when it has a ::id suffix."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    stack_resource = {
        "urn": "urn:pulumi:default::myproj::pulumi:pulumi:Stack::myproj-default",
        "type": "pulumi:pulumi:Stack",
    }
    all_resources = [stack_resource, _PROVIDER_RESOURCE, _BUCKET_WITH_PROVIDER_ID]

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state(all_resources, version=3)
    mock_get_stack.return_value = mock_stack

    run_state_rm("my-provider", auto_approve=True)

    call_args = mock_stack.import_stack.call_args
    imported_state = call_args[0][0]
    remaining = imported_state.deployment["resources"]
    # Provider should be removed
    urns = [r["urn"] for r in remaining]
    assert _PROVIDER_URN not in urns
    # Bucket should have provider key removed
    bucket = [r for r in remaining if r.get("type") == "aws:s3:BucketV2"][0]
    assert "provider" not in bucket


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_rm_cleans_property_dependencies(mock_find, mock_config, mock_get_stack, tmp_path):
    """state rm cleans propertyDependencies of removed URN."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    stack_resource = {
        "urn": "urn:pulumi:default::myproj::pulumi:pulumi:Stack::myproj-default",
        "type": "pulumi:pulumi:Stack",
    }
    all_resources = [stack_resource, _PROVIDER_RESOURCE, _BUCKET_WITH_PROP_DEPS]

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state(all_resources, version=3)
    mock_get_stack.return_value = mock_stack

    run_state_rm("my-provider", auto_approve=True)

    call_args = mock_stack.import_stack.call_args
    imported_state = call_args[0][0]
    remaining = imported_state.deployment["resources"]
    bucket = [r for r in remaining if r.get("type") == "aws:s3:BucketV2"][0]
    prop_deps = bucket.get("propertyDependencies", {})
    # "bucket" should still have the other dep
    assert _PROVIDER_URN not in prop_deps.get("bucket", [])
    assert "urn:pulumi:default::myproj::other::other-res" in prop_deps.get("bucket", [])
    # "acl" had only the removed URN, so it should be removed entirely
    assert "acl" not in prop_deps


# --- backup file security tests ---


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_backup_file_permissions(mock_find, mock_config, mock_get_stack, tmp_path):
    """Backup files from state rm are created with mode 0o600."""
    import os

    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    stack_resource = {
        "urn": "urn:pulumi:default::myproj::pulumi:pulumi:Stack::myproj-default",
        "type": "pulumi:pulumi:Stack",
    }
    target = {
        "urn": "urn:pulumi:default::myproj::aws:s3:BucketV2::my-bucket",
        "type": "aws:s3:BucketV2",
        "id": "b-123",
        "inputs": {},
        "outputs": {},
    }

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state([stack_resource, target], version=3)
    mock_get_stack.return_value = mock_stack

    run_state_rm("my-bucket", auto_approve=True)

    backups = list((tmp_path / ".tlumi" / "backups").glob("state_rm_*.json"))
    assert len(backups) == 1
    mode = os.stat(backups[0]).st_mode & 0o777
    assert mode == 0o600


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_backup_refuses_symlink(mock_find, mock_config, mock_get_stack, tmp_path):
    """Backup creation fails if path is a symlink (O_EXCL prevents following)."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    stack_resource = {
        "urn": "urn:pulumi:default::myproj::pulumi:pulumi:Stack::myproj-default",
        "type": "pulumi:pulumi:Stack",
    }
    target = {
        "urn": "urn:pulumi:default::myproj::aws:s3:BucketV2::my-bucket",
        "type": "aws:s3:BucketV2",
        "id": "b-123",
        "inputs": {},
        "outputs": {},
    }

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state([stack_resource, target], version=3)
    mock_get_stack.return_value = mock_stack

    # Pre-create the backup dir and place a symlink where the backup file will go
    backup_dir = tmp_path / ".tlumi" / "backups"
    backup_dir.mkdir(parents=True)

    # We can't predict the exact timestamp, so place a file that _write_secure's
    # O_EXCL would catch. Instead, test that an existing file causes failure.
    # Monkeypatch datetime to control the timestamp.
    from datetime import datetime
    from unittest.mock import patch as mock_patch

    fixed_time = datetime(2026, 1, 1, 12, 0, 0)
    evil_target = tmp_path / "evil_file"
    evil_target.write_text("evil")
    symlink_path = backup_dir / f"state_rm_{fixed_time.strftime('%Y%m%d_%H%M%S_%f')}.json"
    symlink_path.symlink_to(evil_target)

    with mock_patch("tlumi.commands.state.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_time
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        with pytest.raises(WorkspaceError, match="Failed to create backup"):
            run_state_rm("my-bucket", auto_approve=True)

    # The symlink target should not have been modified
    assert evil_target.read_text() == "evil"


# ---------------------------------------------------------------------------
# Backup directory symlink checks
# ---------------------------------------------------------------------------


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_rm_rejects_backups_dir_symlink(mock_find, mock_config, mock_get_stack, tmp_path):
    """state rm refuses to proceed if .tlumi/backups is a symlink."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    stack_resource = {
        "urn": "urn:pulumi:default::myproj::pulumi:pulumi:Stack::myproj-default",
        "type": "pulumi:pulumi:Stack",
    }
    target = {
        "urn": "urn:pulumi:default::myproj::aws:s3:BucketV2::my-bucket",
        "type": "aws:s3:BucketV2",
        "id": "b-123",
        "inputs": {},
        "outputs": {},
    }

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state([stack_resource, target], version=3)
    mock_get_stack.return_value = mock_stack

    evil_target = tmp_path / "evil_dir"
    evil_target.mkdir()
    (config.tlumi_dir / "backups").symlink_to(evil_target)

    with pytest.raises(WorkspaceError, match="backups is a symlink"):
        run_state_rm("my-bucket", auto_approve=True)


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_mv_rejects_backups_dir_symlink(mock_find, mock_config, mock_get_stack, tmp_path):
    """state mv refuses to proceed if .tlumi/backups is a symlink."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    stack_resource = {
        "urn": "urn:pulumi:default::myproj::pulumi:pulumi:Stack::myproj-default",
        "type": "pulumi:pulumi:Stack",
    }
    target = {
        "urn": "urn:pulumi:default::myproj::aws:s3:BucketV2::my-bucket",
        "type": "aws:s3:BucketV2",
        "id": "b-123",
        "inputs": {},
        "outputs": {},
    }

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state([stack_resource, target], version=3)
    mock_get_stack.return_value = mock_stack

    evil_target = tmp_path / "evil_dir"
    evil_target.mkdir()
    (config.tlumi_dir / "backups").symlink_to(evil_target)

    with pytest.raises(WorkspaceError, match="backups is a symlink"):
        run_state_mv("my-bucket", "renamed-bucket", auto_approve=True)


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_push_rejects_source_symlink(mock_find, mock_config, mock_get_stack, tmp_path):
    """state push refuses to read the source file if it is a symlink.

    Without this, a redirected symlink could let `tlumi state push <link>` read
    /proc/self/environ or any other host file, surfacing as a confusing
    JSON-decode error instead of a clear refusal.
    """
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state([], version=3)
    mock_get_stack.return_value = mock_stack

    real_target = tmp_path / "real_state.json"
    real_target.write_text(json.dumps({"version": 3, "deployment": {"resources": []}}))
    link = tmp_path / "link_state.json"
    link.symlink_to(real_target)

    with pytest.raises(WorkspaceError, match="State file is a symlink"):
        run_state_push(str(link), auto_approve=True)


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_push_rejects_backups_dir_symlink(mock_find, mock_config, mock_get_stack, tmp_path):
    """state push refuses to proceed if .tlumi/backups is a symlink."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state([], version=3)
    mock_get_stack.return_value = mock_stack

    evil_target = tmp_path / "evil_dir"
    evil_target.mkdir()
    (config.tlumi_dir / "backups").symlink_to(evil_target)

    state_file = tmp_path / "new_state.json"
    state_file.write_text(json.dumps({"version": 3, "deployment": {"resources": []}}))

    with pytest.raises(WorkspaceError, match="backups is a symlink"):
        run_state_push(str(state_file), auto_approve=True)


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_push_backup_file_permissions(mock_find, mock_config, mock_get_stack, tmp_path):
    """Backup files from state push are created with mode 0o600."""
    import os

    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state(version=2)
    mock_get_stack.return_value = mock_stack

    state_data = {"version": 3, "deployment": {"resources": []}}
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state_data))

    run_state_push(str(state_file), auto_approve=True)

    backups = list((tmp_path / ".tlumi" / "backups").glob("state_push_*.json"))
    assert len(backups) == 1
    mode = os.stat(backups[0]).st_mode & 0o777
    assert mode == 0o600


# --- backup failure aborts state mutation tests ---


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_rm_backup_failure_aborts(mock_find, mock_config, mock_get_stack, tmp_path):
    """state rm aborts with clear message when backup write fails."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    stack_resource = {
        "urn": "urn:pulumi:default::myproj::pulumi:pulumi:Stack::myproj-default",
        "type": "pulumi:pulumi:Stack",
    }
    target = {
        "urn": "urn:pulumi:default::myproj::aws:s3:BucketV2::my-bucket",
        "type": "aws:s3:BucketV2",
        "id": "b-123",
        "inputs": {},
        "outputs": {},
    }

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state([stack_resource, target], version=3)
    mock_get_stack.return_value = mock_stack

    with patch("tlumi.commands.state._write_secure", side_effect=OSError("disk full")):
        with pytest.raises(WorkspaceError, match="Failed to create backup") as exc_info:
            run_state_rm("my-bucket", auto_approve=True)
        assert "NOT modified" in exc_info.value.hint

    # State should not have been modified
    mock_stack.import_stack.assert_not_called()


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_push_backup_failure_aborts(mock_find, mock_config, mock_get_stack, tmp_path):
    """state push aborts with clear message when backup write fails."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state(version=2)
    mock_get_stack.return_value = mock_stack

    state_data = {"version": 3, "deployment": {"resources": []}}
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state_data))

    with patch("tlumi.commands.state._write_secure", side_effect=OSError("read-only fs")):
        with pytest.raises(WorkspaceError, match="Failed to create backup") as exc_info:
            run_state_push(str(state_file), auto_approve=True)
        assert "NOT modified" in exc_info.value.hint

    # State should not have been modified
    mock_stack.import_stack.assert_not_called()


# --- state rm/push hint includes backup path ---


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_rm_import_failure_hint_includes_backup(
    mock_find, mock_config, mock_get_stack, tmp_path
):
    """state rm import failure hint references the backup file."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    stack_resource = {
        "urn": "urn:pulumi:default::myproj::pulumi:pulumi:Stack::myproj-default",
        "type": "pulumi:pulumi:Stack",
    }
    target = {
        "urn": "urn:pulumi:default::myproj::aws:s3:BucketV2::my-bucket",
        "type": "aws:s3:BucketV2",
        "id": "b-123",
        "inputs": {},
        "outputs": {},
    }

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state([stack_resource, target], version=3)
    mock_stack.import_stack.side_effect = CommandError("corrupt state")
    mock_get_stack.return_value = mock_stack

    with pytest.raises(WorkspaceError, match="Failed to update state") as exc_info:
        run_state_rm("my-bucket", auto_approve=True)
    assert "state_rm_" in exc_info.value.hint
    assert "tlumi state push" in exc_info.value.hint


# ---------------------------------------------------------------------------
# state push: import failure hint includes backup (T6)
# ---------------------------------------------------------------------------


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.safe_export_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_push_import_failure_hint_includes_backup(
    mock_find,
    mock_config,
    mock_export,
    mock_get_stack,
    tmp_path,
):
    """state push shows backup path in hint when import_stack fails."""
    tlumi_dir = tmp_path / ".tlumi"
    tlumi_dir.mkdir()

    config = MagicMock()
    config.tlumi_dir = tlumi_dir
    mock_config.return_value = config
    mock_find.return_value = tmp_path

    # Current state (for backup)
    mock_export.return_value = mock_state([], version=1)

    mock_stack = MagicMock(spec=Stack)
    mock_stack.import_stack.side_effect = CommandError("import failed")
    mock_get_stack.return_value = mock_stack

    # Create a valid state file to push
    state_file = tmp_path / "new_state.json"
    state_file.write_text(json.dumps({"version": 2, "deployment": {"resources": []}}))

    with pytest.raises(WorkspaceError, match="Failed to import state") as exc_info:
        run_state_push(str(state_file), auto_approve=True)
    assert "state_push_" in exc_info.value.hint
    assert "tlumi state push" in exc_info.value.hint


# ---------------------------------------------------------------------------
# state push: OSError on read (S5)
# ---------------------------------------------------------------------------


@patch("tlumi.commands.state.find_project_dir")
def test_state_push_unreadable_file_raises_workspace_error(mock_find, tmp_path):
    """state push raises WorkspaceError when file cannot be read."""
    mock_find.return_value = tmp_path
    state_file = tmp_path / "state.json"
    state_file.write_text("{}")
    state_file.chmod(0o000)

    try:
        with pytest.raises(WorkspaceError, match="Cannot read file"):
            run_state_push(str(state_file), auto_approve=True)
    finally:
        state_file.chmod(0o644)


@patch("tlumi.commands.state.find_project_dir")
def test_state_push_directory_error_names_path(mock_find, tmp_path):
    """state push on a directory names the offending path, not a raw fd number.

    os.open() succeeds on a directory, so without a pre-check the failure came
    from os.fdopen() as 'Is a directory: <fd>' with no path in the message.
    """
    mock_find.return_value = tmp_path
    target_dir = tmp_path / "backups"
    target_dir.mkdir()

    with pytest.raises(WorkspaceError) as exc_info:
        run_state_push(str(target_dir), auto_approve=True)

    assert str(target_dir) in str(exc_info.value)
    assert "is a directory" in str(exc_info.value)


@patch("tlumi.commands.state.find_project_dir")
def test_state_push_fdopen_failure_closes_fd_and_names_path(mock_find, tmp_path, monkeypatch):
    """A raced fdopen failure closes the raw fd and still names the path."""
    mock_find.return_value = tmp_path
    state_file = tmp_path / "state.json"
    state_file.write_text("{}")

    real_close = os.close
    closed: list[int] = []

    def fake_fdopen(fd, *args, **kwargs):
        raise IsADirectoryError(21, "Is a directory", fd)

    def spy_close(fd):
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr("tlumi.commands.state.os.fdopen", fake_fdopen)
    monkeypatch.setattr("tlumi.commands.state.os.close", spy_close)

    with pytest.raises(WorkspaceError) as exc_info:
        run_state_push(str(state_file), auto_approve=True)

    assert str(state_file) in str(exc_info.value)
    assert len(closed) == 1


# ---------------------------------------------------------------------------
# R7-11: run_state_list all paths
# ---------------------------------------------------------------------------


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_list_json_output(mock_find, mock_config, mock_get_stack, capsys):
    """state list --json emits JSON array of resources."""
    mock_get_stack.return_value.export_stack.return_value = mock_state(
        [
            {
                "type": "pulumi:pulumi:Stack",
                "urn": "urn:pulumi:default::p::pulumi:pulumi:Stack::p-default",
            },
            {
                "type": "aws:s3:BucketV2",
                "urn": "urn:pulumi:default::p::aws:s3:BucketV2::my-bucket",
                "id": "b-123",
            },
        ]
    )
    run_state_list(json_output=True)
    output = capsys.readouterr().out
    data = json.loads(output)
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["type"] == "aws:s3:BucketV2"
    assert data[0]["name"] == "my-bucket"
    assert data[0]["id"] == "b-123"


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_list_json_empty(mock_find, mock_config, mock_get_stack, capsys):
    """state list --json with empty state emits empty JSON array."""
    mock_get_stack.return_value.export_stack.return_value = mock_state()
    run_state_list(json_output=True)
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data == []


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_list_human_with_resources(mock_find, mock_config, mock_get_stack, capsys):
    """state list in human mode displays resource type, name, and count."""
    mock_get_stack.return_value.export_stack.return_value = mock_state(
        [
            {
                "type": "pulumi:pulumi:Stack",
                "urn": "urn:pulumi:default::p::pulumi:pulumi:Stack::p-default",
            },
            {
                "type": "aws:s3:BucketV2",
                "urn": "urn:pulumi:default::p::aws:s3:BucketV2::my-bucket",
                "id": "b-123",
            },
        ]
    )
    run_state_list(json_output=False)
    output = capsys.readouterr().out
    assert "aws:s3:BucketV2" in output
    assert "my-bucket" in output
    assert "1 resource(s) in state" in output


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_list_human_empty(mock_find, mock_config, mock_get_stack, capsys):
    """state list in human mode with empty state prints muted message."""
    mock_get_stack.return_value.export_stack.return_value = mock_state()
    run_state_list(json_output=False)
    output = capsys.readouterr().out
    assert "No resources in state" in output


# ---------------------------------------------------------------------------
# R7-12: run_state_show JSON and human paths
# ---------------------------------------------------------------------------


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_show_json_output(mock_find, mock_config, mock_get_stack, capsys):
    """state show --json emits resource details as JSON."""
    mock_get_stack.return_value.export_stack.return_value = mock_state(_RESOURCES)
    run_state_show("my-bucket", json_output=True)
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["urn"] == "urn:pulumi:default::myproj::aws:s3:BucketV2::my-bucket"
    assert data["type"] == "aws:s3:BucketV2"
    assert data["id"] == "my-bucket-abc123"
    assert data["inputs"]["bucket"] == "my-bucket"


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_show_human_output(mock_find, mock_config, mock_get_stack, capsys):
    """state show in human mode displays URN, type, ID."""
    mock_get_stack.return_value.export_stack.return_value = mock_state(_RESOURCES)
    run_state_show("my-bucket")
    output = capsys.readouterr().out
    assert "URN:" in output
    assert "Type:" in output
    assert "ID:" in output
    assert "my-bucket-abc123" in output


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_show_json_reveals_secrets_with_flag(mock_find, mock_config, mock_get_stack, capsys):
    """state show --json --show-secrets reveals raw sentinel data."""
    mock_get_stack.return_value.export_stack.return_value = mock_state([_RESOURCE_WITH_SECRET])
    run_state_show("my-bucket", json_output=True, show_secrets=True)
    output = capsys.readouterr().out
    json.loads(output)  # Validate JSON parsability
    assert "(sensitive)" not in output
    # Raw sentinel should be present when show_secrets=True
    assert _SECRET_SIG in output


# ---------------------------------------------------------------------------
# state rm: provider prefix matching (S1)
# ---------------------------------------------------------------------------


@patch("tlumi.commands.state._write_secure")
@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.safe_export_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_rm_provider_prefix_no_false_match(
    mock_find,
    mock_config,
    mock_export,
    mock_get_stack,
    mock_write,
    tmp_path,
):
    """Provider 'urn:...:my-provider' should NOT match 'urn:...:my-provider-v2::id'."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    (tmp_path / ".tlumi" / "backups").mkdir(parents=True)
    mock_config.return_value = config
    mock_find.return_value = tmp_path

    provider_urn = "urn:pulumi:default::p::pulumi:providers:aws::my-provider"
    other_provider_ref = "urn:pulumi:default::p::pulumi:providers:aws::my-provider-v2::some-id"

    resources = [
        {
            "urn": "urn:pulumi:default::p::pulumi:pulumi:Stack::p-default",
            "type": "pulumi:pulumi:Stack",
        },
        {"urn": provider_urn, "type": "pulumi:providers:aws"},
        {
            "urn": "urn:pulumi:default::p::aws:s3:BucketV2::b",
            "type": "aws:s3:BucketV2",
            "provider": other_provider_ref,
        },
    ]

    mock_stack = MagicMock(spec=Stack)
    mock_export.return_value = mock_state(resources, version=1)
    mock_get_stack.return_value = mock_stack

    run_state_rm("my-provider", auto_approve=True)

    # Verify the bucket's provider was NOT removed (it references a different provider)
    import_call = mock_stack.import_stack.call_args[0][0]
    remaining = import_call.deployment["resources"]
    bucket = [r for r in remaining if r.get("type") == "aws:s3:BucketV2"][0]
    assert "provider" in bucket
    assert bucket["provider"] == other_provider_ref


# ---------------------------------------------------------------------------
# R8-S3: state push version type check
# ---------------------------------------------------------------------------


def test_push_version_not_int(tmp_path):
    """state push raises WorkspaceError when version is not an integer."""
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(json.dumps({"version": "3", "deployment": {}}))
    with pytest.raises(WorkspaceError, match="'version' must be an integer"):
        run_state_push(str(bad_file), auto_approve=True)


def test_push_resources_not_list(tmp_path):
    """state push raises when deployment.resources is not a list."""
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(json.dumps({"version": 3, "deployment": {"resources": "not-a-list"}}))
    with pytest.raises(WorkspaceError, match="'deployment.resources' must be a list"):
        run_state_push(str(bad_file), auto_approve=True)


def test_push_resource_entry_not_dict(tmp_path):
    """state push rejects a non-object entry inside resources[]."""
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(json.dumps({"version": 3, "deployment": {"resources": ["not-an-object"]}}))
    with pytest.raises(WorkspaceError, match=r"resources\[0\] must be an object"):
        run_state_push(str(bad_file), auto_approve=True)


def test_push_resource_missing_urn(tmp_path):
    """state push rejects resources missing the urn key."""
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(
        json.dumps(
            {
                "version": 3,
                "deployment": {"resources": [{"type": "aws:s3:Bucket", "id": "b"}]},
            }
        )
    )
    with pytest.raises(WorkspaceError, match=r"resources\[0\] is missing required key 'urn'"):
        run_state_push(str(bad_file), auto_approve=True)


def test_push_resource_missing_type(tmp_path):
    """state push rejects resources missing the type key."""
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(
        json.dumps(
            {
                "version": 3,
                "deployment": {"resources": [{"urn": "urn:pulumi:default::p::t::n"}]},
            }
        )
    )
    with pytest.raises(WorkspaceError, match=r"resources\[0\] is missing required key 'type'"):
        run_state_push(str(bad_file), auto_approve=True)


def test_push_resource_empty_urn(tmp_path):
    """state push rejects resources with an empty urn string."""
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(
        json.dumps(
            {
                "version": 3,
                "deployment": {"resources": [{"urn": "", "type": "aws:s3:Bucket"}]},
            }
        )
    )
    with pytest.raises(WorkspaceError, match=r"resources\[0\]\.urn must be a non-empty string"):
        run_state_push(str(bad_file), auto_approve=True)


def test_push_resource_urn_not_string(tmp_path):
    """state push rejects resources whose urn is not a string."""
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(
        json.dumps(
            {
                "version": 3,
                "deployment": {"resources": [{"urn": 42, "type": "aws:s3:Bucket"}]},
            }
        )
    )
    with pytest.raises(WorkspaceError, match=r"resources\[0\]\.urn must be a non-empty string"):
        run_state_push(str(bad_file), auto_approve=True)


# ---------------------------------------------------------------------------
# R8-E2: Non-dict resource guard in _get_resources
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# T3: state rm backup content verification
# ---------------------------------------------------------------------------


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_rm_backup_content(mock_find, mock_config, mock_get_stack, tmp_path):
    """state rm backup file contains version and deployment keys."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    stack_resource = {
        "urn": "urn:pulumi:default::myproj::pulumi:pulumi:Stack::myproj-default",
        "type": "pulumi:pulumi:Stack",
    }
    target = {
        "urn": "urn:pulumi:default::myproj::aws:s3:BucketV2::my-bucket",
        "type": "aws:s3:BucketV2",
        "id": "b-123",
        "inputs": {},
        "outputs": {},
    }

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state([stack_resource, target], version=3)
    mock_get_stack.return_value = mock_stack

    run_state_rm("my-bucket", auto_approve=True)

    backups = list((tmp_path / ".tlumi" / "backups").glob("state_rm_*.json"))
    assert len(backups) == 1
    data = json.loads(backups[0].read_text())
    assert data["version"] == 3
    assert "deployment" in data
    assert "resources" in data["deployment"]


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_backup_does_not_persist_decrypted_secrets(
    mock_find, mock_config, mock_get_stack, tmp_path
):
    """state rm backups must store ciphertext, never decrypted secret plaintext.

    safe_export_stack (the SDK export) runs `stack export --show-secrets` and
    decrypts every secret to plaintext. Writing that to .tlumi/backups/ would
    drop decrypted secrets onto disk even when TLUMI_SECRETS_PASSPHRASE is set.
    The backup must instead use the ciphertext export (export_stack_no_secrets),
    which restores identically because the salt travels in secrets_providers.
    """
    config = MagicMock()
    config.name = "myproj"
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    stack_resource = {
        "urn": "urn:pulumi:default::myproj::pulumi:pulumi:Stack::myproj-default",
        "type": "pulumi:pulumi:Stack",
    }
    target = {
        "urn": "urn:pulumi:default::myproj::aws:s3:BucketV2::my-bucket",
        "type": "aws:s3:BucketV2",
        "id": "b-123",
        "inputs": {},
        # Decrypted (--show-secrets) form: the plaintext leaks the secret value.
        "outputs": {
            "secret_key": {_SECRET_SIG: _SECRET_VAL, "plaintext": json.dumps("hunter2-secret")},
        },
    }
    # The ciphertext export keeps the same secret as an encrypted wrapper.
    target_ciphertext = {
        **target,
        "outputs": {"secret_key": {_SECRET_SIG: _SECRET_VAL, "ciphertext": "v1:abc123=="}},
    }

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state([stack_resource, target], version=3)
    mock_get_stack.return_value = mock_stack

    ciphertext_state = mock_state([stack_resource, target_ciphertext], version=3)
    with patch("tlumi.commands.state.export_stack_no_secrets", return_value=ciphertext_state):
        run_state_rm("my-bucket", auto_approve=True)

    backups = list((tmp_path / ".tlumi" / "backups").glob("state_rm_*.json"))
    assert len(backups) == 1
    raw = backups[0].read_text()
    # No decrypted secret value on disk...
    assert "hunter2-secret" not in raw
    assert "plaintext" not in raw
    # ...but the secret is preserved in encrypted form so restore still works.
    assert "ciphertext" in raw


# ---------------------------------------------------------------------------
# T4: state rm import failure includes backup path
# ---------------------------------------------------------------------------


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_rm_import_oserror_includes_backup_path(
    mock_find, mock_config, mock_get_stack, tmp_path
):
    """state rm with OSError on import_stack includes backup path in hint."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    stack_resource = {
        "urn": "urn:pulumi:default::myproj::pulumi:pulumi:Stack::myproj-default",
        "type": "pulumi:pulumi:Stack",
    }
    target = {
        "urn": "urn:pulumi:default::myproj::aws:s3:BucketV2::my-bucket",
        "type": "aws:s3:BucketV2",
        "id": "b-123",
        "inputs": {},
        "outputs": {},
    }

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state([stack_resource, target], version=3)
    mock_stack.import_stack.side_effect = OSError("disk full")
    mock_get_stack.return_value = mock_stack

    with pytest.raises(WorkspaceError, match="Failed to update state") as exc_info:
        run_state_rm("my-bucket", auto_approve=True)
    assert "state_rm_" in exc_info.value.hint
    assert "tlumi state push" in exc_info.value.hint


# ---------------------------------------------------------------------------
# T8: _provider_matches unit tests
# ---------------------------------------------------------------------------


def test_provider_matches_exact_urn():
    """Exact URN match returns True."""
    assert (
        _provider_matches("urn:pulumi:default::p::prov::x", "urn:pulumi:default::p::prov::x")
        is True
    )


def test_provider_matches_urn_with_id_suffix():
    """URN::id format returns True."""
    assert (
        _provider_matches(
            "urn:pulumi:default::p::prov::x::some-id", "urn:pulumi:default::p::prov::x"
        )
        is True
    )


def test_provider_matches_no_match():
    """Unrelated URN returns False."""
    assert (
        _provider_matches("urn:pulumi:default::p::prov::y", "urn:pulumi:default::p::prov::x")
        is False
    )


def test_provider_matches_no_false_prefix():
    """Similar-prefix URN without :: separator returns False."""
    assert (
        _provider_matches("urn:pulumi:default::p::prov::x-v2::id", "urn:pulumi:default::p::prov::x")
        is False
    )


def test_provider_matches_empty_strings():
    """Empty strings return True (both empty)."""
    assert _provider_matches("", "") is True


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_list_non_dict_resources_ignored(mock_find, mock_config, mock_get_stack, capsys):
    """Non-dict entries in resources list are silently filtered out."""
    mock_get_stack.return_value.export_stack.return_value = mock_state(
        [
            "not-a-dict",
            {
                "type": "pulumi:pulumi:Stack",
                "urn": "urn:pulumi:default::p::pulumi:pulumi:Stack::p-default",
            },
            {
                "type": "aws:s3:BucketV2",
                "urn": "urn:pulumi:default::p::aws:s3:BucketV2::b",
                "id": "b-1",
            },
        ]
    )
    run_state_list(json_output=True)
    output = capsys.readouterr().out
    data = json.loads(output)
    assert len(data) == 1
    assert data[0]["type"] == "aws:s3:BucketV2"


# ---------------------------------------------------------------------------
# Backup rotation
# ---------------------------------------------------------------------------


def test_rotate_backups_removes_oldest(tmp_path):
    """Oldest backups are removed when count exceeds _MAX_BACKUPS."""
    for i in range(_MAX_BACKUPS + 3):
        (tmp_path / f"state_rm_2026010{i:02d}_000000_000000.json").write_text("{}")
    _rotate_backups(tmp_path)
    remaining = sorted(tmp_path.glob("state_*.json"))
    assert len(remaining) == _MAX_BACKUPS
    # The 3 oldest should be gone
    assert not (tmp_path / "state_rm_20260100_000000_000000.json").exists()
    assert not (tmp_path / "state_rm_20260101_000000_000000.json").exists()
    assert not (tmp_path / "state_rm_20260102_000000_000000.json").exists()


def test_rotate_backups_noop_under_limit(tmp_path):
    """No files removed when count is at or below _MAX_BACKUPS."""
    for i in range(_MAX_BACKUPS):
        (tmp_path / f"state_push_{i:04d}.json").write_text("{}")
    _rotate_backups(tmp_path)
    assert len(list(tmp_path.glob("state_*.json"))) == _MAX_BACKUPS


def test_rotate_backups_empty_dir(tmp_path):
    """No error on empty directory."""
    _rotate_backups(tmp_path)


def test_rotate_backups_nonexistent_dir(tmp_path):
    """No error when directory does not exist."""
    _rotate_backups(tmp_path / "does_not_exist")


def test_rotate_backups_ignores_non_state_files(tmp_path):
    """Files not matching state_*.json are not counted or removed."""
    for i in range(_MAX_BACKUPS + 2):
        (tmp_path / f"state_rm_{i:04d}.json").write_text("{}")
    (tmp_path / "other_file.json").write_text("{}")
    (tmp_path / "readme.txt").write_text("hi")
    _rotate_backups(tmp_path)
    assert len(list(tmp_path.glob("state_*.json"))) == _MAX_BACKUPS
    assert (tmp_path / "other_file.json").exists()
    assert (tmp_path / "readme.txt").exists()


def test_rotate_backups_mixed_prefixes_evicts_oldest_by_mtime(tmp_path):
    """Mixed state_rm_ and state_push_ backups are evicted by mtime, not name."""
    import time

    # Create _MAX_BACKUPS + 2 files with controlled mtime ordering.
    # Newer push files should survive even though "push" < "rm" lexicographically.
    all_files = []
    for i in range(_MAX_BACKUPS):
        p = tmp_path / f"state_rm_2026010{i:02d}_000000_000000.json"
        p.write_text("{}")
        all_files.append(p)
    # Two newer push backups (higher mtime)
    time.sleep(0.05)
    newer1 = tmp_path / "state_push_20260200_000000_000000.json"
    newer1.write_text("{}")
    all_files.append(newer1)
    time.sleep(0.05)
    newer2 = tmp_path / "state_push_20260201_000000_000000.json"
    newer2.write_text("{}")
    all_files.append(newer2)

    _rotate_backups(tmp_path)
    remaining = list(tmp_path.glob("state_*.json"))
    assert len(remaining) == _MAX_BACKUPS
    # Both newer push backups should survive (they have the latest mtime)
    assert newer1.exists()
    assert newer2.exists()


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_rm_rotates_backups(mock_find, mock_config, mock_get_stack, tmp_path):
    """state rm rotates old backups after creating a new one."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    backup_dir = config.tlumi_dir / "backups"
    backup_dir.mkdir(parents=True)
    # Pre-populate with _MAX_BACKUPS existing backups
    for i in range(_MAX_BACKUPS):
        (backup_dir / f"state_rm_2025010{i:02d}_000000_000000.json").write_text("{}")

    stack_resource = {
        "urn": "urn:pulumi:default::myproj::pulumi:pulumi:Stack::myproj-default",
        "type": "pulumi:pulumi:Stack",
    }
    target = {
        "urn": "urn:pulumi:default::myproj::aws:s3:BucketV2::my-bucket",
        "type": "aws:s3:BucketV2",
        "id": "b-123",
        "inputs": {},
        "outputs": {},
    }

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state([stack_resource, target], version=3)
    mock_get_stack.return_value = mock_stack

    run_state_rm("my-bucket", auto_approve=True)

    backups = list(backup_dir.glob("state_*.json"))
    assert len(backups) == _MAX_BACKUPS


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_rm_rotation_preserves_push_backup(mock_find, mock_config, mock_get_stack, tmp_path):
    """A chatty state rm must not rotate out the state_push restore point.

    Per-op quotas cap state_rm_/state_mv_/state_push_ independently. Under a
    global quota, 11+ rm backups would evict the single older push backup a
    user kept as a restore point -- silent data loss.
    """
    import time

    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    backup_dir = config.tlumi_dir / "backups"
    backup_dir.mkdir(parents=True)
    # The oldest file is the push backup we want to protect.
    push_backup = backup_dir / "state_push_20260101_000000_000000.json"
    push_backup.write_text("{}")
    time.sleep(0.02)
    # A full quota of newer rm backups, so the next rm triggers rotation.
    for i in range(_MAX_BACKUPS):
        (backup_dir / f"state_rm_2026020{i:02d}_000000_000000.json").write_text("{}")

    stack_resource = {
        "urn": "urn:pulumi:default::myproj::pulumi:pulumi:Stack::myproj-default",
        "type": "pulumi:pulumi:Stack",
    }
    target = {
        "urn": "urn:pulumi:default::myproj::aws:s3:BucketV2::my-bucket",
        "type": "aws:s3:BucketV2",
        "id": "b-123",
        "inputs": {},
        "outputs": {},
    }

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state([stack_resource, target], version=3)
    mock_get_stack.return_value = mock_stack

    run_state_rm("my-bucket", auto_approve=True)

    # The rm rotation trimmed only rm backups to the cap; the push backup (the
    # user's restore point) survives.
    assert push_backup.exists()
    assert len(list(backup_dir.glob("state_rm_*.json"))) == _MAX_BACKUPS


# ===========================================================================
# state mv tests
# ===========================================================================

_STACK_RESOURCE = {
    "urn": "urn:pulumi:default::myproj::pulumi:pulumi:Stack::myproj-default",
    "type": "pulumi:pulumi:Stack",
}

_RG_RESOURCE = {
    "urn": "urn:pulumi:default::myproj::azure-native:resources:ResourceGroup::rg",
    "type": "azure-native:resources:ResourceGroup",
    "id": "rg-123",
    "inputs": {"resourceGroupName": "rg"},
    "outputs": {"name": "rg"},
}

_STORAGE_RESOURCE = {
    "urn": "urn:pulumi:default::myproj::azure-native:storage:StorageAccount::storage",
    "type": "azure-native:storage:StorageAccount",
    "id": "sa-456",
    "parent": "urn:pulumi:default::myproj::azure-native:resources:ResourceGroup::rg",
    "dependencies": ["urn:pulumi:default::myproj::azure-native:resources:ResourceGroup::rg"],
    "propertyDependencies": {
        "resourceGroupName": [
            "urn:pulumi:default::myproj::azure-native:resources:ResourceGroup::rg"
        ],
    },
    "inputs": {},
    "outputs": {},
}

# Same dependency shape as _STORAGE_RESOURCE but without a parent reference, so
# tests can exercise reference rewriting without triggering the
# "rename-rejected-because-has-children" guard.
_STORAGE_PEER_RESOURCE = {
    "urn": "urn:pulumi:default::myproj::azure-native:storage:StorageAccount::storage",
    "type": "azure-native:storage:StorageAccount",
    "id": "sa-456",
    "dependencies": ["urn:pulumi:default::myproj::azure-native:resources:ResourceGroup::rg"],
    "propertyDependencies": {
        "resourceGroupName": [
            "urn:pulumi:default::myproj::azure-native:resources:ResourceGroup::rg"
        ],
    },
    "inputs": {},
    "outputs": {},
}


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_mv_renames_urn(mock_find, mock_config, mock_get_stack, tmp_path, capsys):
    """state mv renames the resource URN in state."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state(
        [_STACK_RESOURCE, _RG_RESOURCE],
        version=3,
    )
    mock_get_stack.return_value = mock_stack

    run_state_mv("rg", "new-rg", auto_approve=True)

    call_args = mock_stack.import_stack.call_args[0][0]
    remaining = call_args.deployment["resources"]
    rg = [r for r in remaining if r.get("type") == "azure-native:resources:ResourceGroup"][0]
    assert rg["urn"] == "urn:pulumi:default::myproj::azure-native:resources:ResourceGroup::new-rg"

    output = capsys.readouterr().out
    assert "Renamed" in output
    assert "new-rg" in output


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_mv_updates_all_references(mock_find, mock_config, mock_get_stack, tmp_path):
    """state mv updates dependencies and propertyDependencies references.

    Uses a peer resource (no parent ref) to isolate the dependency/propertyDeps
    rewrite; the parent-ref rewrite for a child is covered by
    test_state_mv_renames_parent_with_children_and_warns.
    """
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state(
        [_STACK_RESOURCE, _RG_RESOURCE, _STORAGE_PEER_RESOURCE],
        version=3,
    )
    mock_get_stack.return_value = mock_stack

    run_state_mv("rg", "new-rg", auto_approve=True)

    call_args = mock_stack.import_stack.call_args[0][0]
    remaining = call_args.deployment["resources"]
    new_urn = "urn:pulumi:default::myproj::azure-native:resources:ResourceGroup::new-rg"
    old_urn = "urn:pulumi:default::myproj::azure-native:resources:ResourceGroup::rg"

    storage = [r for r in remaining if r.get("type") == "azure-native:storage:StorageAccount"][0]
    assert new_urn in storage["dependencies"]
    assert old_urn not in storage["dependencies"]
    prop_deps = storage["propertyDependencies"]["resourceGroupName"]
    assert new_urn in prop_deps
    assert old_urn not in prop_deps


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_mv_updates_provider_reference(mock_find, mock_config, mock_get_stack, tmp_path):
    """state mv updates provider references (with ::id suffix)."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    provider_urn = "urn:pulumi:default::myproj::pulumi:providers:aws::my-prov"
    provider = {"urn": provider_urn, "type": "pulumi:providers:aws", "id": "prov-1"}
    bucket = {
        "urn": "urn:pulumi:default::myproj::aws:s3:BucketV2::b",
        "type": "aws:s3:BucketV2",
        "provider": f"{provider_urn}::prov-1",
        "inputs": {},
        "outputs": {},
    }

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state(
        [_STACK_RESOURCE, provider, bucket],
        version=3,
    )
    mock_get_stack.return_value = mock_stack

    run_state_mv("my-prov", "new-prov", auto_approve=True)

    call_args = mock_stack.import_stack.call_args[0][0]
    remaining = call_args.deployment["resources"]
    new_prov_urn = "urn:pulumi:default::myproj::pulumi:providers:aws::new-prov"
    bucket_r = [r for r in remaining if r.get("type") == "aws:s3:BucketV2"][0]
    assert bucket_r["provider"] == f"{new_prov_urn}::prov-1"


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_mv_urn_collision(mock_find, mock_config, mock_get_stack, tmp_path):
    """state mv raises error when destination URN already exists."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    # Two resources with different names
    r1 = {
        "urn": "urn:pulumi:default::myproj::aws:s3:BucketV2::bucket-a",
        "type": "aws:s3:BucketV2",
        "id": "a",
        "inputs": {},
        "outputs": {},
    }
    r2 = {
        "urn": "urn:pulumi:default::myproj::aws:s3:BucketV2::bucket-b",
        "type": "aws:s3:BucketV2",
        "id": "b",
        "inputs": {},
        "outputs": {},
    }

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state(
        [_STACK_RESOURCE, r1, r2],
        version=3,
    )
    mock_get_stack.return_value = mock_stack

    with pytest.raises(WorkspaceError, match="already exists"):
        run_state_mv("bucket-a", "bucket-b", auto_approve=True)

    mock_stack.import_stack.assert_not_called()


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_mv_same_source_destination_rejected(
    mock_find, mock_config, mock_get_stack, tmp_path
):
    """state mv foo foo is a no-op; reject with a clear error, not a collision lie."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    r1 = {
        "urn": "urn:pulumi:default::myproj::aws:s3:BucketV2::bucket-a",
        "type": "aws:s3:BucketV2",
        "id": "a",
        "inputs": {},
        "outputs": {},
    }
    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state(
        [_STACK_RESOURCE, r1],
        version=3,
    )
    mock_get_stack.return_value = mock_stack

    # Note: the bug this guards against is the collision loop matching the
    # source resource against itself when new_urn == old_urn. The user-facing
    # error must read as "no-op" rather than "already exists, pick a different
    # name" -- because the only colliding resource IS the source.
    with pytest.raises(WorkspaceError, match="(?i)source and destination are the same"):
        run_state_mv("bucket-a", "bucket-a", auto_approve=True)

    mock_stack.import_stack.assert_not_called()


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_mv_creates_backup(mock_find, mock_config, mock_get_stack, tmp_path):
    """state mv creates a backup file."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state(
        [_STACK_RESOURCE, _RG_RESOURCE],
        version=3,
    )
    mock_get_stack.return_value = mock_stack

    run_state_mv("rg", "new-rg", auto_approve=True)

    backups = list((tmp_path / ".tlumi" / "backups").glob("state_mv_*.json"))
    assert len(backups) == 1
    data = json.loads(backups[0].read_text())
    assert data["version"] == 3
    assert "deployment" in data


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_mv_empty_destination_rejected(mock_find, mock_config, mock_get_stack, tmp_path):
    """state mv rejects empty destination name."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state(
        [_STACK_RESOURCE, _RG_RESOURCE],
        version=3,
    )
    mock_get_stack.return_value = mock_stack

    with pytest.raises(WorkspaceError, match="cannot be empty"):
        run_state_mv("rg", "", auto_approve=True)


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_mv_type_name_destination(mock_find, mock_config, mock_get_stack, tmp_path, capsys):
    """state mv with type::name destination validates type matches."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state(
        [_STACK_RESOURCE, _RG_RESOURCE],
        version=3,
    )
    mock_get_stack.return_value = mock_stack

    # Type matches canonical type
    run_state_mv("rg", "azure-native:resources:ResourceGroup::new-rg", auto_approve=True)

    call_args = mock_stack.import_stack.call_args[0][0]
    remaining = call_args.deployment["resources"]
    rg = [r for r in remaining if r.get("type") == "azure-native:resources:ResourceGroup"][0]
    assert rg["urn"].endswith("::new-rg")


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_mv_type_mismatch_rejected(mock_find, mock_config, mock_get_stack, tmp_path):
    """state mv with type::name destination rejects type mismatch."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state(
        [_STACK_RESOURCE, _RG_RESOURCE],
        version=3,
    )
    mock_get_stack.return_value = mock_stack

    with pytest.raises(WorkspaceError, match="Type mismatch"):
        run_state_mv("rg", "wrong:Type::new-rg", auto_approve=True)


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_mv_backup_failure_aborts(mock_find, mock_config, mock_get_stack, tmp_path):
    """state mv aborts when backup fails."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state(
        [_STACK_RESOURCE, _RG_RESOURCE],
        version=3,
    )
    mock_get_stack.return_value = mock_stack

    with patch("tlumi.commands.state._write_secure", side_effect=OSError("disk full")):
        with pytest.raises(WorkspaceError, match="Failed to create backup") as exc_info:
            run_state_mv("rg", "new-rg", auto_approve=True)
        assert "NOT modified" in exc_info.value.hint

    mock_stack.import_stack.assert_not_called()


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_mv_import_failure_references_backup(
    mock_find, mock_config, mock_get_stack, tmp_path
):
    """state mv import failure hint references the backup file."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state(
        [_STACK_RESOURCE, _RG_RESOURCE],
        version=3,
    )
    mock_stack.import_stack.side_effect = CommandError("import failed")
    mock_get_stack.return_value = mock_stack

    with pytest.raises(WorkspaceError, match="Failed to update state") as exc_info:
        run_state_mv("rg", "new-rg", auto_approve=True)
    assert "state_mv_" in exc_info.value.hint
    assert "tlumi state push" in exc_info.value.hint


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_mv_renames_parent_with_children_and_warns(
    mock_find, mock_config, mock_get_stack, tmp_path, capsys
):
    """state mv renames a parent that has children, repointing their parent refs.

    Child URNs embed only the parent TYPE chain, never the parent NAME, so the
    rename is structurally safe; tlumi warns (children with code-derived names
    may still be replaced) but proceeds rather than refusing.
    """
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    captured = {}
    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state(
        [_STACK_RESOURCE, _RG_RESOURCE, _STORAGE_RESOURCE],
        version=3,
    )
    mock_stack.import_stack.side_effect = lambda d: captured.update(deployment=d.deployment)
    mock_get_stack.return_value = mock_stack

    run_state_mv("rg", "new-rg", auto_approve=True)

    # The rename proceeds and a warning about children is shown.
    mock_stack.import_stack.assert_called_once()
    out = capsys.readouterr().out
    assert "child resource" in out

    # The safety claim rests on the child's references being repointed to the
    # new parent URN; assert that actually happened (not just the warning).
    old_rg = "urn:pulumi:default::myproj::azure-native:resources:ResourceGroup::rg"
    new_rg = "urn:pulumi:default::myproj::azure-native:resources:ResourceGroup::new-rg"
    resources = {r["urn"]: r for r in captured["deployment"]["resources"]}
    child = resources["urn:pulumi:default::myproj::azure-native:storage:StorageAccount::storage"]
    assert child["parent"] == new_rg
    assert old_rg not in child["dependencies"]
    assert new_rg in child["dependencies"]
    assert child["propertyDependencies"]["resourceGroupName"] == [new_rg]


# ===========================================================================
# state rm: parent and dependencies cleanup tests
# ===========================================================================


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_rm_cleans_parent_reference(mock_find, mock_config, mock_get_stack, tmp_path):
    """state rm removes parent reference from child resources."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    parent_urn = "urn:pulumi:default::myproj::azure-native:resources:ResourceGroup::rg"
    child = {
        "urn": "urn:pulumi:default::myproj::azure-native:storage:StorageAccount::sa",
        "type": "azure-native:storage:StorageAccount",
        "id": "sa-1",
        "parent": parent_urn,
        "inputs": {},
        "outputs": {},
    }
    parent = {
        "urn": parent_urn,
        "type": "azure-native:resources:ResourceGroup",
        "id": "rg-1",
        "inputs": {},
        "outputs": {},
    }

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state(
        [_STACK_RESOURCE, parent, child],
        version=3,
    )
    mock_get_stack.return_value = mock_stack

    run_state_rm("rg", auto_approve=True)

    call_args = mock_stack.import_stack.call_args[0][0]
    remaining = call_args.deployment["resources"]
    sa = [r for r in remaining if r.get("type") == "azure-native:storage:StorageAccount"][0]
    assert "parent" not in sa


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_rm_cleans_dependencies_list(mock_find, mock_config, mock_get_stack, tmp_path):
    """state rm removes target URN from dependencies, preserving others."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    target_urn = "urn:pulumi:default::myproj::aws:s3:BucketV2::target-bucket"
    other_urn = "urn:pulumi:default::myproj::aws:ec2:Instance::web"
    dependent = {
        "urn": "urn:pulumi:default::myproj::aws:lambda:Function::fn",
        "type": "aws:lambda:Function",
        "id": "fn-1",
        "dependencies": [target_urn, other_urn],
        "inputs": {},
        "outputs": {},
    }
    target = {
        "urn": target_urn,
        "type": "aws:s3:BucketV2",
        "id": "b-1",
        "inputs": {},
        "outputs": {},
    }

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state(
        [_STACK_RESOURCE, target, dependent],
        version=3,
    )
    mock_get_stack.return_value = mock_stack

    run_state_rm("target-bucket", auto_approve=True)

    call_args = mock_stack.import_stack.call_args[0][0]
    remaining = call_args.deployment["resources"]
    fn = [r for r in remaining if r.get("type") == "aws:lambda:Function"][0]
    assert target_urn not in fn["dependencies"]
    assert other_urn in fn["dependencies"]


# ===========================================================================
# state mv: provider exact-match test (no ::id suffix)
# ===========================================================================


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_mv_updates_provider_exact_match(mock_find, mock_config, mock_get_stack, tmp_path):
    """state mv updates provider reference that is an exact URN match (no ::id suffix)."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    provider_urn = "urn:pulumi:default::myproj::pulumi:providers:aws::my-prov"
    provider = {"urn": provider_urn, "type": "pulumi:providers:aws", "id": "prov-1"}
    bucket = {
        "urn": "urn:pulumi:default::myproj::aws:s3:BucketV2::b",
        "type": "aws:s3:BucketV2",
        "provider": provider_urn,
        "inputs": {},
        "outputs": {},
    }

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state(
        [_STACK_RESOURCE, provider, bucket],
        version=3,
    )
    mock_get_stack.return_value = mock_stack

    run_state_mv("my-prov", "new-prov", auto_approve=True)

    call_args = mock_stack.import_stack.call_args[0][0]
    remaining = call_args.deployment["resources"]
    new_prov_urn = "urn:pulumi:default::myproj::pulumi:providers:aws::new-prov"
    bucket_r = [r for r in remaining if r.get("type") == "aws:s3:BucketV2"][0]
    assert bucket_r["provider"] == new_prov_urn


# ---------------------------------------------------------------------------
# deletedWith edge (F4/F5): the persisted state edge that state rm/mv/push
# previously ignored. replaceWith is registration-only (not persisted), so it
# is intentionally NOT handled.
# ---------------------------------------------------------------------------

_RG_URN = "urn:pulumi:default::myproj::azure-native:resources:ResourceGroup::rg"
_SA_DELETED_WITH_RG = {
    "urn": "urn:pulumi:default::myproj::azure-native:storage:StorageAccount::sa",
    "type": "azure-native:storage:StorageAccount",
    "id": "sa-1",
    "deletedWith": _RG_URN,
    "inputs": {},
    "outputs": {},
}


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_rm_cleans_deleted_with_reference(
    mock_find, mock_config, mock_get_stack, tmp_path, capsys
):
    """state rm strips the removed URN from other resources' deletedWith and warns first."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state(
        [_STACK_RESOURCE, _RG_RESOURCE, _SA_DELETED_WITH_RG],
        version=3,
    )
    mock_get_stack.return_value = mock_stack

    run_state_rm("rg", auto_approve=True)

    # The dependent is surfaced as a [deletedWith] dependent before removal.
    assert "deletedWith" in capsys.readouterr().out

    remaining = mock_stack.import_stack.call_args[0][0].deployment["resources"]
    sa = [r for r in remaining if r.get("type") == "azure-native:storage:StorageAccount"][0]
    assert "deletedWith" not in sa


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_mv_updates_deleted_with_reference(mock_find, mock_config, mock_get_stack, tmp_path):
    """state mv rewrites a deletedWith reference to the renamed URN."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state(
        [_STACK_RESOURCE, _RG_RESOURCE, _SA_DELETED_WITH_RG],
        version=3,
    )
    mock_get_stack.return_value = mock_stack

    run_state_mv("rg", "new-rg", auto_approve=True)

    remaining = mock_stack.import_stack.call_args[0][0].deployment["resources"]
    new_urn = "urn:pulumi:default::myproj::azure-native:resources:ResourceGroup::new-rg"
    sa = [r for r in remaining if r.get("type") == "azure-native:storage:StorageAccount"][0]
    assert sa["deletedWith"] == new_urn
    assert sa["deletedWith"] != _RG_URN


def test_push_dangling_deleted_with_reference_rejected(tmp_path):
    """Referential integrity: deletedWith must point at a URN declared in the envelope."""
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(
        json.dumps(
            {
                "version": 3,
                "deployment": {
                    "resources": [
                        {
                            "urn": "urn:pulumi:default::p::aws:s3:Bucket::a",
                            "type": "aws:s3:Bucket",
                            "deletedWith": "urn:pulumi:default::p::aws:s3:Bucket::gone",
                        }
                    ]
                },
            }
        )
    )
    with pytest.raises(WorkspaceError, match="deletedWith references unknown URN"):
        run_state_push(str(bad_file), auto_approve=True)


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.safe_export_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_push_provider_id_suffix_stripped_for_integrity(
    mock_find, mock_config, mock_export, mock_get_stack, tmp_path
):
    """state push strips a provider's ::id suffix before the referential check (F28).

    A consumer referencing ``<provider-urn>::<id>`` with the bare provider URN
    declared must pass validation and reach import_stack. A broken strip would
    wrongly reject it (and never call import_stack), so this guards the logic
    the only prior test could not distinguish from a no-op.
    """
    tlumi_dir = tmp_path / ".tlumi"
    tlumi_dir.mkdir()
    config = MagicMock()
    config.name = "p"
    config.tlumi_dir = tlumi_dir
    mock_config.return_value = config
    mock_find.return_value = tmp_path
    mock_export.return_value = mock_state([], version=1)
    mock_stack = MagicMock(spec=Stack)
    mock_get_stack.return_value = mock_stack

    provider_urn = "urn:pulumi:default::p::pulumi:providers:aws::default"
    state_file = tmp_path / "new_state.json"
    state_file.write_text(
        json.dumps(
            {
                "version": 3,
                "deployment": {
                    "resources": [
                        {"urn": provider_urn, "type": "pulumi:providers:aws"},
                        {
                            "urn": "urn:pulumi:default::p::aws:s3:Bucket::b",
                            "type": "aws:s3:Bucket",
                            "provider": f"{provider_urn}::a1b2c3d4-id",
                        },
                    ]
                },
            }
        )
    )

    run_state_push(str(state_file), auto_approve=True)

    mock_stack.import_stack.assert_called_once()


# ===========================================================================
# Public-release review: URN parsing with '::' names, cross-project guard,
# non-empty push diff (#3, #4, #36)
# ===========================================================================


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_mv_name_with_double_colon(mock_find, mock_config, mock_get_stack, tmp_path):
    """A resource whose name contains '::' is renamed correctly (positional URN parse)."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    old_urn = "urn:pulumi:default::myproj::my:mod:Comp::foo::bar"
    res = {"urn": old_urn, "type": "my:mod:Comp", "id": "x", "inputs": {}, "outputs": {}}
    captured = {}

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state([_STACK_RESOURCE, res], version=3)
    mock_stack.import_stack.side_effect = lambda d: captured.update(deployment=d.deployment)
    mock_get_stack.return_value = mock_stack

    run_state_mv(old_urn, "baz", auto_approve=True)

    mock_stack.import_stack.assert_called_once()
    new_urns = [r["urn"] for r in captured["deployment"]["resources"]]
    assert "urn:pulumi:default::myproj::my:mod:Comp::baz" in new_urns
    # The buggy split-on-every-:: would have produced ...::foo::baz instead.
    assert "urn:pulumi:default::myproj::my:mod:Comp::foo::baz" not in new_urns


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_push_rejects_state_from_other_project(mock_find, mock_config, mock_get_stack, tmp_path):
    """A state file whose URNs embed a different project name is refused (#4)."""
    config = MagicMock()
    config.name = "myproj"
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state(version=2)
    mock_get_stack.return_value = mock_stack

    state_data = {
        "version": 3,
        "deployment": {
            "resources": [
                {
                    "type": "aws:s3:BucketV2",
                    "urn": "urn:pulumi:default::otherproj::aws:s3:BucketV2::b",
                },
            ]
        },
    }
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state_data))

    with pytest.raises(WorkspaceError, match="otherproj"):
        run_state_push(str(state_file), auto_approve=True)
    mock_stack.import_stack.assert_not_called()


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_push_diff_counts_nonempty(mock_find, mock_config, mock_get_stack, tmp_path, capsys):
    """Push diff reports correct added/removed/unchanged against a non-empty current state (#36)."""
    config = MagicMock()
    config.name = "p"
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    def urn(n):
        return f"urn:pulumi:default::p::aws:s3:BucketV2::{n}"

    # Current state has A and B (plus a Stack that must be excluded from counts).
    current = mock_state(
        [
            {"type": "pulumi:pulumi:Stack", "urn": "urn:pulumi:default::p::pulumi:pulumi:Stack::p"},
            {"type": "aws:s3:BucketV2", "urn": urn("A")},
            {"type": "aws:s3:BucketV2", "urn": urn("B")},
        ],
        version=3,
    )
    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = current
    mock_get_stack.return_value = mock_stack

    # Pushed file declares B and C.
    state_data = {
        "version": 3,
        "deployment": {
            "resources": [
                {"type": "aws:s3:BucketV2", "urn": urn("B")},
                {"type": "aws:s3:BucketV2", "urn": urn("C")},
            ]
        },
    }
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state_data))

    run_state_push(str(state_file), auto_approve=True, json_output=True)

    data = json.loads(capsys.readouterr().out)
    assert data["diff"] == {"added": 1, "removed": 1, "unchanged": 1}


# ===========================================================================
# Hardening sweep: state show rendering (indentation + dunder keys)
# ===========================================================================

_RESOURCE_WITH_NESTED_INPUTS = {
    "urn": "urn:pulumi:default::myproj::aws:s3:BucketV2::my-bucket",
    "type": "aws:s3:BucketV2",
    "id": "b-1",
    "inputs": {"bucket": "my-bucket", "tags": {"env": "dev"}},
    "outputs": {},
}

_RESOURCE_WITH_DUNDER_KEYS = {
    "urn": "urn:pulumi:default::myproj::aws:s3:BucketV2::my-bucket",
    "type": "aws:s3:BucketV2",
    "id": "b-1",
    "inputs": {"bucket": "my-bucket", "__internal": {}},
    "outputs": {"arn": "arn:aws:s3:::my-bucket", "__pulumi_raw_state_delta": {"x": 1}},
}


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_show_human_indents_whole_json_block(mock_find, mock_config, mock_get_stack, capsys):
    """Every line of the Inputs/Outputs JSON dump carries the 2-space prefix.

    The old f"  {json.dumps(...)}" prefixed only the first line, leaving the
    closing brace and continuation lines at column 0, misaligned under the
    'Inputs:' header.
    """
    mock_get_stack.return_value.export_stack.return_value = mock_state(
        [_RESOURCE_WITH_NESTED_INPUTS]
    )
    run_state_show("my-bucket")
    lines = capsys.readouterr().out.splitlines()
    # Opening and closing braces are both indented; nothing sits at column 0.
    assert "  {" in lines
    assert "  }" in lines
    assert "{" not in lines
    assert "}" not in lines
    assert '    "bucket": "my-bucket",' in lines


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_show_human_hides_dunder_keys(mock_find, mock_config, mock_get_stack, capsys):
    """Human-readable state show hides Pulumi bookkeeping keys (top-level __*)."""
    mock_get_stack.return_value.export_stack.return_value = mock_state([_RESOURCE_WITH_DUNDER_KEYS])
    run_state_show("my-bucket")
    output = capsys.readouterr().out
    assert "__internal" not in output
    assert "__pulumi_raw_state_delta" not in output
    # User-defined keys are still rendered
    assert "bucket" in output
    assert "arn" in output


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_show_human_suppresses_dunder_only_section(
    mock_find, mock_config, mock_get_stack, capsys
):
    """A section whose only keys are bookkeeping keys is omitted entirely."""
    provider = {
        "urn": "urn:pulumi:default::myproj::pulumi:providers:aws::default",
        "type": "pulumi:providers:aws",
        "id": "prov-1",
        "inputs": {"__internal": {}},
        "outputs": {},
    }
    mock_get_stack.return_value.export_stack.return_value = mock_state([provider])
    run_state_show("default")
    output = capsys.readouterr().out
    assert "Inputs:" not in output


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_show_json_keeps_dunder_keys(mock_find, mock_config, mock_get_stack, capsys):
    """state show --json is a faithful dump: bookkeeping keys are preserved."""
    mock_get_stack.return_value.export_stack.return_value = mock_state([_RESOURCE_WITH_DUNDER_KEYS])
    run_state_show("my-bucket", json_output=True)
    data = json.loads(capsys.readouterr().out)
    assert data["inputs"]["__internal"] == {}
    assert data["outputs"]["__pulumi_raw_state_delta"] == {"x": 1}


_RESOURCE_WITH_NESTED_DUNDER_KEYS = {
    "urn": "urn:pulumi:default::myproj::aws:s3:BucketV2::my-bucket",
    "type": "aws:s3:BucketV2",
    "id": "b-1",
    "inputs": {
        "bucket": "my-bucket",
        "versioning": {"__defaults": [], "enabled": True},
    },
    "outputs": {
        "rules": [{"__defaults": [], "prefix": "logs/"}],
    },
}


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_show_human_hides_nested_dunder_keys(mock_find, mock_config, mock_get_stack, capsys):
    """Dunder filtering is recursive: bridged-provider __defaults nested inside
    object inputs (and inside lists) are hidden, matching plan-diff behavior."""
    mock_get_stack.return_value.export_stack.return_value = mock_state(
        [_RESOURCE_WITH_NESTED_DUNDER_KEYS]
    )
    run_state_show("my-bucket")
    output = capsys.readouterr().out
    assert "__defaults" not in output
    assert "enabled" in output
    assert "prefix" in output


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_show_json_keeps_nested_dunder_keys(mock_find, mock_config, mock_get_stack, capsys):
    """state show --json keeps nested bookkeeping keys (faithful dump)."""
    mock_get_stack.return_value.export_stack.return_value = mock_state(
        [_RESOURCE_WITH_NESTED_DUNDER_KEYS]
    )
    run_state_show("my-bucket", json_output=True)
    data = json.loads(capsys.readouterr().out)
    assert data["inputs"]["versioning"]["__defaults"] == []
    assert data["outputs"]["rules"][0]["__defaults"] == []


# ===========================================================================
# Hardening sweep: state push symlink component guard (codex-3)
# ===========================================================================


def test_state_push_rejects_relative_path_with_symlinked_parent(tmp_path, monkeypatch):
    """A relative source path whose parent directory is a symlink is refused.

    The leaf-only guard let 'dir/state.json' through when 'dir' was a symlink,
    so a malicious cloned repo could redirect the read to arbitrary host files.
    """
    monkeypatch.chdir(tmp_path)
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    (real_dir / "state.json").write_text(json.dumps({"version": 3, "deployment": {}}))
    (tmp_path / "link_dir").symlink_to(real_dir)

    with pytest.raises(WorkspaceError, match="symlink component"):
        run_state_push("link_dir/state.json", auto_approve=True)


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_push_allows_absolute_path_through_symlinked_parent(
    mock_find, mock_config, mock_get_stack, tmp_path
):
    """An absolute path through a symlinked parent dir is allowed.

    Absolute paths are user-typed, not repo-controlled; /tmp is a symlink on
    macOS, so the component-wise rule must not apply to them.
    """
    config = MagicMock()
    config.name = "p"
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state(version=2)
    mock_get_stack.return_value = mock_stack

    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    (real_dir / "state.json").write_text(
        json.dumps({"version": 3, "deployment": {"resources": []}})
    )
    link_dir = tmp_path / "link_dir"
    link_dir.symlink_to(real_dir)

    run_state_push(str(link_dir / "state.json"), auto_approve=True)

    mock_stack.import_stack.assert_called_once()


def test_state_push_rejects_relative_leaf_symlink(tmp_path, monkeypatch):
    """A relative leaf symlink still gets the clear leaf-specific message."""
    monkeypatch.chdir(tmp_path)
    real = tmp_path / "real_state.json"
    real.write_text(json.dumps({"version": 3, "deployment": {}}))
    (tmp_path / "link.json").symlink_to(real)

    with pytest.raises(WorkspaceError, match="State file is a symlink"):
        run_state_push("link.json", auto_approve=True)


# ===========================================================================
# Hardening sweep: push diff direction (added vs removed)
# ===========================================================================


def _bucket_urn(name):
    return f"urn:pulumi:default::p::aws:s3:BucketV2::{name}"


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_push_diff_direction_json_asymmetric_counts(
    mock_find, mock_config, mock_get_stack, tmp_path, capsys
):
    """Asymmetric counts pin the diff direction: swapping added/removed fails.

    Current state has {A}; pushed file has {B, C}. Correct: added=2, removed=1.
    An inverted diff would report added=1, removed=2.
    """
    config = MagicMock()
    config.name = "p"
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state(
        [{"type": "aws:s3:BucketV2", "urn": _bucket_urn("A")}], version=3
    )
    mock_get_stack.return_value = mock_stack

    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "version": 3,
                "deployment": {
                    "resources": [
                        {"type": "aws:s3:BucketV2", "urn": _bucket_urn("B")},
                        {"type": "aws:s3:BucketV2", "urn": _bucket_urn("C")},
                    ]
                },
            }
        )
    )

    run_state_push(str(state_file), auto_approve=True, json_output=True)

    data = json.loads(capsys.readouterr().out)
    assert data["diff"] == {"added": 2, "removed": 1, "unchanged": 0}


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_push_diff_direction_human_names_correct_resources(
    mock_find, mock_config, mock_get_stack, tmp_path, capsys
):
    """The +/- lines shown before confirmation name the right resources.

    Current state has {A, B}; pushed file has {B, C}. C must appear under '+'
    (it is new in the pushed file) and A under '-' (it will be dropped).
    """
    config = MagicMock()
    config.name = "p"
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state(
        [
            {"type": "aws:s3:BucketV2", "urn": _bucket_urn("A")},
            {"type": "aws:s3:BucketV2", "urn": _bucket_urn("B")},
        ],
        version=3,
    )
    mock_get_stack.return_value = mock_stack

    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "version": 3,
                "deployment": {
                    "resources": [
                        {"type": "aws:s3:BucketV2", "urn": _bucket_urn("B")},
                        {"type": "aws:s3:BucketV2", "urn": _bucket_urn("C")},
                    ]
                },
            }
        )
    )

    run_state_push(str(state_file), auto_approve=True)

    output = capsys.readouterr().out
    assert "+1" in output
    assert "-1" in output
    assert "1 unchanged" in output
    assert "+ aws:s3:BucketV2 (C)" in output
    assert "- aws:s3:BucketV2 (A)" in output
    # Direction inversion would put A under '+' and C under '-'
    assert "+ aws:s3:BucketV2 (A)" not in output
    assert "- aws:s3:BucketV2 (C)" not in output


# ===========================================================================
# Hardening sweep: confirmation declined leaves state untouched, no backup
# ===========================================================================


@patch("tlumi.commands.state.confirm", return_value=False)
@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_rm_confirm_declined_no_mutation_no_backup(
    mock_find, mock_config, mock_get_stack, mock_confirm, tmp_path, capsys
):
    """Declining the state rm prompt mutates nothing and creates no backup."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state([_STACK_RESOURCE, _RG_RESOURCE], version=3)
    mock_get_stack.return_value = mock_stack

    run_state_rm("rg", auto_approve=False)

    mock_confirm.assert_called_once()
    mock_stack.import_stack.assert_not_called()
    assert not (config.tlumi_dir / "backups").exists()
    assert "Cancelled." in capsys.readouterr().out


@patch("tlumi.commands.state.confirm", return_value=False)
@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_mv_confirm_declined_no_mutation_no_backup(
    mock_find, mock_config, mock_get_stack, mock_confirm, tmp_path, capsys
):
    """Declining the state mv prompt mutates nothing and creates no backup."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state([_STACK_RESOURCE, _RG_RESOURCE], version=3)
    mock_get_stack.return_value = mock_stack

    run_state_mv("rg", "new-rg", auto_approve=False)

    mock_confirm.assert_called_once()
    mock_stack.import_stack.assert_not_called()
    assert not (config.tlumi_dir / "backups").exists()
    assert "Cancelled." in capsys.readouterr().out


@patch("tlumi.commands.state.confirm", return_value=False)
@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_push_confirm_declined_no_mutation_no_backup(
    mock_find, mock_config, mock_get_stack, mock_confirm, tmp_path, capsys
):
    """Declining the state push prompt mutates nothing and creates no backup."""
    config = MagicMock()
    config.name = "p"
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state(version=2)
    mock_get_stack.return_value = mock_stack

    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "version": 3,
                "deployment": {"resources": [{"type": "aws:s3:BucketV2", "urn": _bucket_urn("B")}]},
            }
        )
    )

    run_state_push(str(state_file), auto_approve=False)

    mock_confirm.assert_called_once()
    mock_stack.import_stack.assert_not_called()
    assert not (config.tlumi_dir / "backups").exists()
    assert "Cancelled." in capsys.readouterr().out


# ===========================================================================
# Hardening sweep: state rm/mv reference-rewriting edge branches
# ===========================================================================


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_rm_deletes_property_dependencies_key_when_emptied(
    mock_find, mock_config, mock_get_stack, tmp_path
):
    """When cleanup empties propertyDependencies, the key is deleted, not left as {}."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    dependent = {
        "urn": "urn:pulumi:default::myproj::aws:lambda:Function::fn",
        "type": "aws:lambda:Function",
        "id": "fn-1",
        "propertyDependencies": {"role": [_PROVIDER_URN]},
        "inputs": {},
        "outputs": {},
    }
    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state(
        [_STACK_RESOURCE, _PROVIDER_RESOURCE, dependent], version=3
    )
    mock_get_stack.return_value = mock_stack

    run_state_rm("my-provider", auto_approve=True)

    remaining = mock_stack.import_stack.call_args[0][0].deployment["resources"]
    fn = [r for r in remaining if r.get("type") == "aws:lambda:Function"][0]
    assert "propertyDependencies" not in fn


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_rm_preserves_unrelated_property_dependencies_key(
    mock_find, mock_config, mock_get_stack, tmp_path
):
    """A propertyDependencies key that never referenced the removed URN is untouched."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    other_urn = "urn:pulumi:default::myproj::other::other-res"
    dependent = {
        "urn": "urn:pulumi:default::myproj::aws:lambda:Function::fn",
        "type": "aws:lambda:Function",
        "id": "fn-1",
        "propertyDependencies": {"role": [_PROVIDER_URN], "tags": [other_urn]},
        "inputs": {},
        "outputs": {},
    }
    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state(
        [_STACK_RESOURCE, _PROVIDER_RESOURCE, dependent], version=3
    )
    mock_get_stack.return_value = mock_stack

    run_state_rm("my-provider", auto_approve=True)

    remaining = mock_stack.import_stack.call_args[0][0].deployment["resources"]
    fn = [r for r in remaining if r.get("type") == "aws:lambda:Function"][0]
    assert fn["propertyDependencies"] == {"tags": [other_urn]}


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_mv_propdeps_only_reference_copy_on_write(
    mock_find, mock_config, mock_get_stack, tmp_path
):
    """A resource whose ONLY reference to the moved URN is propertyDependencies is rewritten.

    Exercises the copy-on-write branch where propertyDependencies is the first
    (and only) mutation: the imported copy carries the new URN while the
    original state dict is left unmodified.
    """
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    old_urn = _RG_RESOURCE["urn"]
    dependent = {
        "urn": "urn:pulumi:default::myproj::aws:lambda:Function::fn",
        "type": "aws:lambda:Function",
        "id": "fn-1",
        "propertyDependencies": {"role": [old_urn]},
        "inputs": {},
        "outputs": {},
    }
    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state(
        [_STACK_RESOURCE, _RG_RESOURCE, dependent], version=3
    )
    mock_get_stack.return_value = mock_stack

    run_state_mv("rg", "new-rg", auto_approve=True)

    new_urn = "urn:pulumi:default::myproj::azure-native:resources:ResourceGroup::new-rg"
    remaining = mock_stack.import_stack.call_args[0][0].deployment["resources"]
    fn = [r for r in remaining if r.get("type") == "aws:lambda:Function"][0]
    assert fn["propertyDependencies"] == {"role": [new_urn]}
    # Copy-on-write: the original resource dict must not have been mutated
    assert dependent["propertyDependencies"] == {"role": [old_urn]}


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_mv_corrupt_urn_rejected(mock_find, mock_config, mock_get_stack, tmp_path):
    """A source resource whose URN has fewer than four '::' fields is refused."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    corrupt = {
        "urn": "urn:pulumi:default::myproj::badres",
        "type": "aws:s3:BucketV2",
        "id": "x",
        "inputs": {},
        "outputs": {},
    }
    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state([_STACK_RESOURCE, corrupt], version=3)
    mock_get_stack.return_value = mock_stack

    with pytest.raises(WorkspaceError, match="Cannot parse URN"):
        run_state_mv("badres", "newname", auto_approve=True)

    mock_stack.import_stack.assert_not_called()


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_mv_destination_name_containing_double_colon(
    mock_find, mock_config, mock_get_stack, tmp_path, capsys
):
    """A type::name destination whose NAME contains '::' splits on the first
    '::' (consistent with resolve_resource), not the last."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state(
        [_STACK_RESOURCE, _RG_RESOURCE],
        version=3,
    )
    mock_get_stack.return_value = mock_stack

    run_state_mv("rg", "azure-native:resources:ResourceGroup::new::rg", auto_approve=True)

    call_args = mock_stack.import_stack.call_args[0][0]
    remaining = call_args.deployment["resources"]
    rg = [r for r in remaining if r.get("type") == "azure-native:resources:ResourceGroup"][0]
    assert rg["urn"].endswith("azure-native:resources:ResourceGroup::new::rg")


# ---------------------------------------------------------------------------
# Hostile-repo hardening: symlink loops and deeply nested JSON
# ---------------------------------------------------------------------------


def _raise_on_symlink_resolve(monkeypatch) -> None:
    """Make Path.resolve() raise on symlinks, mimicking Python 3.10-3.12 loops."""
    from pathlib import Path

    orig_resolve = Path.resolve

    def fake_resolve(self, *a, **k):
        if self.is_symlink():
            raise RuntimeError(f"Symlink loop from {self}")
        return orig_resolve(self, *a, **k)

    monkeypatch.setattr(Path, "resolve", fake_resolve)


def test_state_push_source_symlink_loop_clean_error(tmp_path, monkeypatch):
    """A symlink-loop source path yields a clean WorkspaceError, not a raw
    RuntimeError from Path.resolve() (Python 3.10-3.12 raise on loops)."""
    link = tmp_path / "link_state.json"
    link.symlink_to(link)  # self-referential loop
    _raise_on_symlink_resolve(monkeypatch)

    with pytest.raises(WorkspaceError, match="State file is a symlink"):
        run_state_push(str(link), auto_approve=True)


@patch("tlumi.commands.state.get_stack")
@patch("tlumi.commands.state.load_config")
@patch("tlumi.commands.state.find_project_dir")
def test_state_rm_backups_symlink_loop_clean_error(
    mock_find, mock_config, mock_get_stack, tmp_path, monkeypatch
):
    """A symlink-loop .tlumi/backups yields a clean WorkspaceError, not a raw
    RuntimeError from Path.resolve()."""
    config = MagicMock()
    config.tlumi_dir = tmp_path / ".tlumi"
    config.tlumi_dir.mkdir()
    mock_config.return_value = config

    stack_resource = {
        "urn": "urn:pulumi:default::myproj::pulumi:pulumi:Stack::myproj-default",
        "type": "pulumi:pulumi:Stack",
    }
    target = {
        "urn": "urn:pulumi:default::myproj::aws:s3:BucketV2::my-bucket",
        "type": "aws:s3:BucketV2",
        "id": "b-123",
        "inputs": {},
        "outputs": {},
    }
    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.return_value = mock_state([stack_resource, target], version=3)
    mock_get_stack.return_value = mock_stack

    backups = config.tlumi_dir / "backups"
    backups.symlink_to(backups)  # self-referential loop
    _raise_on_symlink_resolve(monkeypatch)

    with pytest.raises(WorkspaceError, match="backups is a symlink"):
        run_state_rm("my-bucket", auto_approve=True)


def test_state_push_deeply_nested_json_clean_error(tmp_path):
    """A pathologically nested JSON source file yields a clean WorkspaceError
    (json.loads raises RecursionError, not JSONDecodeError), not a traceback."""
    deep = "[" * 100000 + "]" * 100000
    state_file = tmp_path / "deep.json"
    state_file.write_text(deep)

    with pytest.raises(WorkspaceError, match="Invalid JSON"):
        run_state_push(str(state_file), auto_approve=True)
