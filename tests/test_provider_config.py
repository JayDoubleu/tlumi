"""Tests for provider_config: namespaced Pulumi provider settings in tlumi.yaml.

Covers parsing/validation in tlumi.config and the stack reconciliation in
tlumi.workspace.get_stack (set verbatim, recorded in the sidecar, stale keys
cleaned, unmanaged keys left alone).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from pulumi.automation import Stack

from tlumi.config import load_config, merge_variables
from tlumi.errors import ConfigError

# ---------------------------------------------------------------------------
# load_config: parsing and validation
# ---------------------------------------------------------------------------


def test_provider_config_parsed(tmp_path):
    (tmp_path / "tlumi.yaml").write_text(
        "project:\n  name: myproj\n"
        "provider_config:\n"
        "  azure-native:location: westeurope\n"
        "  aws:region: eu-west-1\n"
    )
    config = load_config(tmp_path)
    assert config.provider_config == {
        "azure-native:location": "westeurope",
        "aws:region": "eu-west-1",
    }
    assert config.variables == {}


def test_provider_config_default_empty(tmp_path):
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: myproj\n")
    assert load_config(tmp_path).provider_config == {}


def test_provider_config_bare_key_rejected(tmp_path):
    (tmp_path / "tlumi.yaml").write_text(
        "project:\n  name: myproj\nprovider_config:\n  location: westeurope\n"
    )
    with pytest.raises(ConfigError, match="no namespace"):
        load_config(tmp_path)


def test_provider_config_project_namespace_rejected(tmp_path):
    (tmp_path / "tlumi.yaml").write_text(
        "project:\n  name: myproj\nprovider_config:\n  myproj:region: eu\n"
    )
    with pytest.raises(ConfigError, match="own namespace"):
        load_config(tmp_path)


def test_provider_config_value_coerced_bool_and_int(tmp_path):
    (tmp_path / "tlumi.yaml").write_text(
        "project:\n  name: myproj\n"
        "provider_config:\n"
        "  azure-native:disablePricingCheck: true\n"
        "  aws:maxRetries: 5\n"
    )
    config = load_config(tmp_path)
    assert config.provider_config["azure-native:disablePricingCheck"] == "true"
    assert config.provider_config["aws:maxRetries"] == "5"


def test_provider_config_lossy_float_rejected(tmp_path):
    # Same lossy-YAML rule as variables: an unquoted 1.10 corrupts to 1.1.
    (tmp_path / "tlumi.yaml").write_text(
        "project:\n  name: myproj\nprovider_config:\n  aws:apiVersion: 1.10\n"
    )
    with pytest.raises(ConfigError, match="rewrites"):
        load_config(tmp_path)


def test_provider_config_not_a_mapping_rejected(tmp_path):
    (tmp_path / "tlumi.yaml").write_text(
        "project:\n  name: myproj\nprovider_config:\n  - azure-native:location\n"
    )
    with pytest.raises(ConfigError, match="mapping"):
        load_config(tmp_path)


def test_provider_config_quoted_value_stays_string(tmp_path):
    (tmp_path / "tlumi.yaml").write_text(
        'project:\n  name: myproj\nprovider_config:\n  aws:region: "1.10"\n'
    )
    assert load_config(tmp_path).provider_config["aws:region"] == "1.10"


def test_merge_variables_preserves_provider_config(tmp_path):
    (tmp_path / "tlumi.yaml").write_text(
        "project:\n  name: myproj\n"
        "provider_config:\n  azure-native:location: westeurope\n"
        "variables:\n  region: eu\n"
    )
    config = load_config(tmp_path)
    merged = merge_variables(config, var=["region=us"])
    assert merged.variables["region"] == "us"
    assert merged.provider_config == {"azure-native:location": "westeurope"}


# ---------------------------------------------------------------------------
# get_stack reconciliation
# ---------------------------------------------------------------------------


def _make_project(tmp_path, yaml_extra=""):
    (tmp_path / "tlumi.yaml").write_text(f"project:\n  name: myproj\n{yaml_extra}")
    (tmp_path / ".tlumi" / "cache" / "venv").mkdir(parents=True)
    (tmp_path / "infra.py").write_text("pass\n")
    return load_config(tmp_path)


def _run_get_stack(config, mock_stack):
    from tlumi.workspace import get_stack

    with (
        patch("tlumi.workspace._ensure_pulumi_cli"),
        patch("tlumi.workspace._load_inline_program", return_value=lambda: None),
        patch("tlumi.workspace.auto.create_or_select_stack", return_value=mock_stack),
    ):
        get_stack(config)


def test_get_stack_sets_provider_config_verbatim(tmp_path, monkeypatch):
    """A provider key is set with its full namespaced form and recorded."""
    config = _make_project(
        tmp_path,
        yaml_extra=(
            "secrets:\n  allow_unencrypted: true\n"
            "provider_config:\n  azure-native:location: westeurope\n"
        ),
    )
    monkeypatch.delenv("TLUMI_SECRETS_PASSPHRASE", raising=False)

    mock_stack = MagicMock(spec=Stack)
    mock_stack.get_all_config.return_value = {}
    _run_get_stack(config, mock_stack)

    keys_set = {c.args[0] for c in mock_stack.set_config.call_args_list}
    assert "azure-native:location" in keys_set
    sidecar = tmp_path / ".tlumi" / "cache" / "managed_config_keys.json"
    assert json.loads(sidecar.read_text()) == {
        "keys": [],
        "provider_keys": ["azure-native:location"],
    }


def test_get_stack_removes_stale_provider_config(tmp_path, monkeypatch):
    """A provider key tlumi wrote before but no longer in tlumi.yaml is removed."""
    config = _make_project(tmp_path, yaml_extra="secrets:\n  allow_unencrypted: true\n")
    cache = tmp_path / ".tlumi" / "cache"
    (cache / "managed_config_keys.json").write_text(
        json.dumps({"keys": [], "provider_keys": ["azure-native:location"]})
    )
    monkeypatch.delenv("TLUMI_SECRETS_PASSPHRASE", raising=False)

    mock_stack = MagicMock(spec=Stack)
    mock_stack.get_all_config.return_value = {"azure-native:location": MagicMock()}
    _run_get_stack(config, mock_stack)

    mock_stack.remove_config.assert_called_once_with("azure-native:location")
    assert json.loads((cache / "managed_config_keys.json").read_text()) == {
        "keys": [],
        "provider_keys": [],
    }


def test_get_stack_keeps_unmanaged_provider_config(tmp_path, monkeypatch):
    """A provider key tlumi never wrote (absent from the sidecar) is left alone."""
    config = _make_project(tmp_path, yaml_extra="secrets:\n  allow_unencrypted: true\n")
    cache = tmp_path / ".tlumi" / "cache"
    (cache / "managed_config_keys.json").write_text(json.dumps({"keys": [], "provider_keys": []}))
    monkeypatch.delenv("TLUMI_SECRETS_PASSPHRASE", raising=False)

    mock_stack = MagicMock(spec=Stack)
    mock_stack.get_all_config.return_value = {"azure-native:location": MagicMock()}
    _run_get_stack(config, mock_stack)

    mock_stack.remove_config.assert_not_called()


def test_get_stack_old_sidecar_without_provider_keys_field(tmp_path, monkeypatch):
    """A pre-feature sidecar (no provider_keys field) means nothing to clean."""
    config = _make_project(tmp_path, yaml_extra="secrets:\n  allow_unencrypted: true\n")
    cache = tmp_path / ".tlumi" / "cache"
    (cache / "managed_config_keys.json").write_text(json.dumps({"keys": ["region"]}))
    monkeypatch.delenv("TLUMI_SECRETS_PASSPHRASE", raising=False)

    mock_stack = MagicMock(spec=Stack)
    mock_stack.get_all_config.return_value = {"azure-native:location": MagicMock()}
    _run_get_stack(config, mock_stack)

    mock_stack.remove_config.assert_not_called()
