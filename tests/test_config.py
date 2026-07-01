"""Tests for tlumi.config: load_config, merge_variables, find_project_dir."""

from __future__ import annotations

import pytest

from tlumi.config import find_project_dir, load_config, merge_variables
from tlumi.errors import ConfigError, ProjectNotFoundError

# ---------------------------------------------------------------------------
# load_config:happy paths
# ---------------------------------------------------------------------------


def test_load_config_minimal(tmp_path):
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: myproject\n")
    config = load_config(tmp_path)
    assert config.name == "myproject"
    assert config.entry == "infra.py"
    assert config.variables == {}


def test_load_config_with_variables(tmp_path):
    (tmp_path / "tlumi.yaml").write_text(
        "project:\n  name: myproj\nvariables:\n  region: us-east-1\n  count: 3\n"
    )
    config = load_config(tmp_path)
    assert config.variables == {"region": "us-east-1", "count": "3"}


def test_load_config_bool_as_lowercase(tmp_path):
    (tmp_path / "tlumi.yaml").write_text(
        "project:\n  name: myproj\nvariables:\n  enabled: true\n  debug: false\n"
    )
    config = load_config(tmp_path)
    assert config.variables["enabled"] == "true"
    assert config.variables["debug"] == "false"


# ---------------------------------------------------------------------------
# load_config:name validation
# ---------------------------------------------------------------------------


def test_load_config_missing_name(tmp_path):
    (tmp_path / "tlumi.yaml").write_text("project:\n  entry: main.py\n")
    with pytest.raises(ConfigError, match="Missing 'project.name'"):
        load_config(tmp_path)


def test_load_config_invalid_name_starts_with_digit(tmp_path):
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: 123bad\n")
    with pytest.raises(ConfigError, match="Invalid project name"):
        load_config(tmp_path)


def test_load_config_invalid_name_special_chars(tmp_path):
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: 'my project!'\n")
    with pytest.raises(ConfigError, match="Invalid project name"):
        load_config(tmp_path)


def test_load_config_name_with_trailing_newline_rejected(tmp_path):
    """A YAML block scalar yields a trailing newline; \\Z must reject it (F18)."""
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: |\n    myproj\n")
    with pytest.raises(ConfigError, match="Invalid project name"):
        load_config(tmp_path)


def test_load_config_valid_name_with_hyphens_underscores(tmp_path):
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: my-infra_v2\n")
    config = load_config(tmp_path)
    assert config.name == "my-infra_v2"


# ---------------------------------------------------------------------------
# load_config:entry path traversal
# ---------------------------------------------------------------------------


def test_load_config_entry_traversal_rejected(tmp_path):
    (tmp_path / "tlumi.yaml").write_text(
        "project:\n  name: myproj\n  entry: '../../../etc/passwd'\n"
    )
    with pytest.raises(ConfigError, match="escapes the project directory"):
        load_config(tmp_path)


# ---------------------------------------------------------------------------
# load_config:variable validation
# ---------------------------------------------------------------------------


def test_load_config_list_variable_rejected(tmp_path):
    (tmp_path / "tlumi.yaml").write_text(
        "project:\n  name: myproj\nvariables:\n  tags:\n    - a\n    - b\n"
    )
    with pytest.raises(ConfigError, match="scalar value"):
        load_config(tmp_path)


def test_load_config_dict_variable_rejected(tmp_path):
    (tmp_path / "tlumi.yaml").write_text(
        "project:\n  name: myproj\nvariables:\n  nested:\n    key: value\n"
    )
    with pytest.raises(ConfigError, match="scalar value"):
        load_config(tmp_path)


def test_load_config_null_variable_rejected(tmp_path):
    """A bare `key:` (YAML null) is rejected, not stored as the literal 'None' (F15)."""
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: myproj\nvariables:\n  region:\n")
    with pytest.raises(ConfigError, match="has no value"):
        load_config(tmp_path)


def test_load_config_explicit_null_variable_rejected(tmp_path):
    """`key: null` is rejected the same way."""
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: myproj\nvariables:\n  region: null\n")
    with pytest.raises(ConfigError, match="has no value"):
        load_config(tmp_path)


def test_load_config_empty_string_variable_accepted(tmp_path):
    """An explicit empty string is allowed (distinct from null)."""
    (tmp_path / "tlumi.yaml").write_text('project:\n  name: myproj\nvariables:\n  region: ""\n')
    config = load_config(tmp_path)
    assert config.variables["region"] == ""


def test_merge_var_file_null_variable_rejected(tmp_path):
    """A null value in a --var-file is rejected like in tlumi.yaml (F15)."""
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: myproj\n")
    config = load_config(tmp_path)
    var_file = tmp_path / "vars.yaml"
    var_file.write_text("region:\n")
    with pytest.raises(ConfigError, match="has no value"):
        merge_variables(config, var_file=[str(var_file)])


# ---------------------------------------------------------------------------
# load_config:missing project
# ---------------------------------------------------------------------------


def test_load_config_no_file(tmp_path):
    with pytest.raises(ProjectNotFoundError):
        load_config(tmp_path)


# ---------------------------------------------------------------------------
# merge_variables
# ---------------------------------------------------------------------------


def _make_config(tmp_path, variables=None):
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: test\n")
    config = load_config(tmp_path)
    if variables:
        config.variables.update(variables)
    return config


def test_merge_var_overrides_yaml(tmp_path):
    config = _make_config(tmp_path, {"region": "us-east-1"})
    config = merge_variables(config, var=["region=eu-west-1"])
    assert config.variables["region"] == "eu-west-1"


def test_merge_var_file_overrides_yaml(tmp_path):
    config = _make_config(tmp_path, {"region": "us-east-1"})
    var_file = tmp_path / "vars.yaml"
    var_file.write_text("region: ap-south-1\n")
    config = merge_variables(config, var_file=[str(var_file)])
    assert config.variables["region"] == "ap-south-1"


def test_merge_var_overrides_var_file(tmp_path):
    config = _make_config(tmp_path)
    var_file = tmp_path / "vars.yaml"
    var_file.write_text("region: ap-south-1\n")
    config = merge_variables(config, var=["region=eu-west-1"], var_file=[str(var_file)])
    assert config.variables["region"] == "eu-west-1"


def test_merge_env_var(tmp_path, monkeypatch):
    config = _make_config(tmp_path)
    monkeypatch.setenv("TLUMI_VAR_REGION", "from-env")
    config = merge_variables(config)
    assert config.variables["region"] == "from-env"


def test_merge_env_var_lowercase(tmp_path, monkeypatch):
    config = _make_config(tmp_path)
    monkeypatch.setenv("TLUMI_VAR_MY_SETTING", "value")
    config = merge_variables(config)
    assert config.variables["my_setting"] == "value"


def test_merge_env_var_overridden_by_var_file(tmp_path, monkeypatch):
    config = _make_config(tmp_path)
    monkeypatch.setenv("TLUMI_VAR_REGION", "from-env")
    var_file = tmp_path / "vars.yaml"
    var_file.write_text("region: from-file\n")
    config = merge_variables(config, var_file=[str(var_file)])
    assert config.variables["region"] == "from-file"


def test_merge_env_var_overridden_by_var(tmp_path, monkeypatch):
    config = _make_config(tmp_path)
    monkeypatch.setenv("TLUMI_VAR_REGION", "from-env")
    config = merge_variables(config, var=["region=from-cli"])
    assert config.variables["region"] == "from-cli"


def test_merge_env_var_empty_name_ignored(tmp_path, monkeypatch):
    config = _make_config(tmp_path)
    monkeypatch.setenv("TLUMI_VAR_", "empty")
    config = merge_variables(config)
    assert "" not in config.variables


def test_merge_var_invalid_format(tmp_path):
    config = _make_config(tmp_path)
    with pytest.raises(ConfigError, match="Invalid variable format"):
        config = merge_variables(config, var=["noequals"])


def test_merge_var_empty_key(tmp_path):
    config = _make_config(tmp_path)
    with pytest.raises(ConfigError, match="Invalid variable format"):
        config = merge_variables(config, var=["=value"])


def test_merge_var_file_not_found(tmp_path):
    config = _make_config(tmp_path)
    with pytest.raises(ConfigError, match="Variable file not found"):
        config = merge_variables(config, var_file=[str(tmp_path / "nope.yaml")])


def test_merge_var_file_bool_as_lowercase(tmp_path):
    config = _make_config(tmp_path)
    var_file = tmp_path / "vars.yaml"
    var_file.write_text("debug: true\n")
    config = merge_variables(config, var_file=[str(var_file)])
    assert config.variables["debug"] == "true"


def test_merge_var_file_list_rejected(tmp_path):
    config = _make_config(tmp_path)
    var_file = tmp_path / "vars.yaml"
    var_file.write_text("tags:\n  - a\n  - b\n")
    with pytest.raises(ConfigError, match="scalar value"):
        config = merge_variables(config, var_file=[str(var_file)])


# ---------------------------------------------------------------------------
# find_project_dir
# ---------------------------------------------------------------------------


def test_find_project_dir(tmp_path, monkeypatch):
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: test\n")
    monkeypatch.chdir(tmp_path)
    found = find_project_dir()
    assert found == tmp_path.resolve()


def test_find_project_dir_not_found(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ProjectNotFoundError):
        find_project_dir()


# ---------------------------------------------------------------------------
# load_config:secrets.allow_unencrypted
# ---------------------------------------------------------------------------


def test_load_config_allow_unencrypted_default(tmp_path):
    """allow_unencrypted defaults to False."""
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: myproj\n")
    config = load_config(tmp_path)
    assert config.secrets.allow_unencrypted is False


def test_load_config_allow_unencrypted_true(tmp_path):
    (tmp_path / "tlumi.yaml").write_text(
        "project:\n  name: myproj\nsecrets:\n  allow_unencrypted: true\n"
    )
    config = load_config(tmp_path)
    assert config.secrets.allow_unencrypted is True


# ---------------------------------------------------------------------------
# merge_variables:sensitive key warning
# ---------------------------------------------------------------------------


def test_merge_var_sensitive_key_warns(tmp_path, capsys):
    """--var with sensitive key name emits a warning."""
    config = _make_config(tmp_path)
    config = merge_variables(config, var=["db_password=secret123"])
    output = capsys.readouterr().out
    assert "sensitive" in output.lower() or "shell history" in output.lower()


def test_merge_var_sensitive_key_quiet_suppresses(tmp_path, capsys):
    """--var with sensitive key name in quiet mode emits no warning."""
    config = _make_config(tmp_path)
    config = merge_variables(config, var=["db_password=secret123"], quiet=True)
    output = capsys.readouterr().out
    assert "sensitive" not in output.lower() and "shell history" not in output.lower()


def test_merge_var_non_sensitive_key_no_warning(tmp_path, capsys):
    """--var with non-sensitive key name emits no warning."""
    config = _make_config(tmp_path)
    config = merge_variables(config, var=["region=us-east-1"])
    output = capsys.readouterr().out
    assert "shell history" not in output.lower()


# ---------------------------------------------------------------------------
# ProjectConfig frozen / merge_variables returns new instance
# ---------------------------------------------------------------------------


def test_project_config_is_frozen(tmp_path):
    """ProjectConfig is frozen: attribute assignment raises FrozenInstanceError."""
    import dataclasses

    config = _make_config(tmp_path)
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.name = "renamed"  # type: ignore[misc]


def test_merge_variables_returns_new_instance(tmp_path):
    """merge_variables returns a NEW ProjectConfig; the original is untouched."""
    config = _make_config(tmp_path, {"region": "us-east-1"})
    original_variables = dict(config.variables)
    merged = merge_variables(config, var=["region=eu-west-1"])
    # New instance distinct from input
    assert merged is not config
    # New instance carries the override
    assert merged.variables["region"] == "eu-west-1"
    # Original is untouched (the asymmetric guarantee that makes frozen safe)
    assert config.variables == original_variables


def test_merge_variables_preserves_frozen_subconfigs(tmp_path):
    """merge_variables copies refs to BackendConfig/SecretsConfig (no useless deep copies)."""
    config = _make_config(tmp_path)
    merged = merge_variables(config, var=["region=us"])
    assert merged.backend is config.backend
    assert merged.secrets is config.secrets
    assert merged.name == config.name
    assert merged.entry == config.entry


def test_merge_var_file_external_path_accepted(tmp_path):
    """--var-file with path outside project dir is accepted."""
    config = _make_config(tmp_path)
    # Create a var file in a parent directory (outside project_dir)
    parent_var = tmp_path.parent / "external_vars.yaml"
    parent_var.write_text("region: us-east-1\n")
    try:
        config = merge_variables(config, var_file=[str(parent_var)])
        assert config.variables["region"] == "us-east-1"
    finally:
        parent_var.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# load_config: YAML error paths
# ---------------------------------------------------------------------------


def test_load_config_invalid_yaml_syntax(tmp_path):
    """Invalid YAML syntax raises ConfigError."""
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: [bad yaml\n")
    with pytest.raises(ConfigError, match="Cannot read tlumi.yaml"):
        load_config(tmp_path)


def test_load_config_empty_yaml(tmp_path):
    """Empty YAML file raises ConfigError for missing project.name."""
    (tmp_path / "tlumi.yaml").write_text("")
    with pytest.raises(ConfigError, match="Missing 'project.name'"):
        load_config(tmp_path)


def test_load_config_project_as_string(tmp_path):
    """project as a scalar string raises ConfigError."""
    (tmp_path / "tlumi.yaml").write_text("project: not-a-mapping\n")
    with pytest.raises(ConfigError, match="'project' must be a mapping"):
        load_config(tmp_path)


def test_load_config_backend_as_string(tmp_path):
    """backend as a scalar raises ConfigError."""
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: myproj\nbackend: not-a-mapping\n")
    with pytest.raises(ConfigError, match="'backend' must be a mapping"):
        load_config(tmp_path)


def test_load_config_secrets_as_string(tmp_path):
    """secrets as a scalar raises ConfigError."""
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: myproj\nsecrets: not-a-mapping\n")
    with pytest.raises(ConfigError, match="'secrets' must be a mapping"):
        load_config(tmp_path)


def test_load_config_variables_as_string(tmp_path):
    """variables as a scalar raises ConfigError."""
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: myproj\nvariables: not-a-mapping\n")
    with pytest.raises(ConfigError, match="'variables' must be a mapping"):
        load_config(tmp_path)


# ---------------------------------------------------------------------------
# R4-E1: load_config OSError/UnicodeDecodeError
# ---------------------------------------------------------------------------


def test_load_config_permission_error(tmp_path):
    """OSError on read_text raises ConfigError."""
    from pathlib import Path
    from unittest.mock import patch

    (tmp_path / "tlumi.yaml").write_text("project:\n  name: myproj\n")
    with patch.object(Path, "read_text", side_effect=PermissionError("forbidden")):
        with pytest.raises(ConfigError, match="Cannot read tlumi.yaml"):
            load_config(tmp_path)


# ---------------------------------------------------------------------------
# R4-E2: merge_variables var-file OSError
# ---------------------------------------------------------------------------


def test_merge_variables_var_file_permission_error(tmp_path):
    """OSError on var-file read_text raises ConfigError."""
    from tlumi.config import ProjectConfig

    config = ProjectConfig(name="test", project_dir=tmp_path)
    var_file = tmp_path / "vars.yaml"
    var_file.write_text("key: val")
    var_file.chmod(0o000)
    try:
        with pytest.raises(ConfigError, match="Cannot read variable file"):
            config = merge_variables(config, var_file=[str(var_file)])
    finally:
        var_file.chmod(0o644)


# ---------------------------------------------------------------------------
# R4-TD3: empty entry validation
# ---------------------------------------------------------------------------


def test_project_config_empty_entry_raises():
    """Empty entry string raises ConfigError in __post_init__."""
    from pathlib import Path

    from tlumi.config import ProjectConfig

    with pytest.raises(ConfigError, match="Entry file path must not be empty"):
        ProjectConfig(name="test", project_dir=Path("/tmp/test"), entry="")


# ---------------------------------------------------------------------------
# R6-T1: warn_unencrypted string rejection
# ---------------------------------------------------------------------------


def test_load_config_warn_unencrypted_string_rejected(tmp_path):
    """warn_unencrypted as a quoted string raises ConfigError."""
    (tmp_path / "tlumi.yaml").write_text(
        'project:\n  name: myproj\nsecrets:\n  warn_unencrypted: "false"\n'
    )
    with pytest.raises(ConfigError, match="'secrets.warn_unencrypted' must be a boolean"):
        load_config(tmp_path)


def test_load_config_allow_unencrypted_string_rejected(tmp_path):
    """allow_unencrypted as a quoted string raises ConfigError."""
    (tmp_path / "tlumi.yaml").write_text(
        'project:\n  name: myproj\nsecrets:\n  allow_unencrypted: "true"\n'
    )
    with pytest.raises(ConfigError, match="'secrets.allow_unencrypted' must be a boolean"):
        load_config(tmp_path)


# ---------------------------------------------------------------------------
# R6-T2: BackendConfig.resolved_url
# ---------------------------------------------------------------------------


def test_backend_config_resolved_url_default(tmp_path):
    """BackendConfig with no URL returns local file state path."""
    from tlumi.config import BackendConfig

    backend = BackendConfig()
    url = backend.resolved_url(tmp_path)
    assert url == f"file://{tmp_path / '.tlumi' / 'state'}"


def test_backend_config_resolved_url_custom():
    """BackendConfig with custom URL returns it unchanged."""
    from pathlib import Path

    from tlumi.config import BackendConfig

    backend = BackendConfig(url="s3://my-bucket/state")
    url = backend.resolved_url(Path("/unused"))
    assert url == "s3://my-bucket/state"


def test_backend_config_resolved_url_empty_string(tmp_path):
    """BackendConfig with empty string URL defaults to local file."""
    from tlumi.config import BackendConfig

    backend = BackendConfig(url="")
    url = backend.resolved_url(tmp_path)
    assert url == f"file://{tmp_path / '.tlumi' / 'state'}"


# ---------------------------------------------------------------------------
# R6-E7/E8: type validation for name, entry, provider
# ---------------------------------------------------------------------------


def test_load_config_name_as_integer_rejected(tmp_path):
    """YAML integer name raises ConfigError."""
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: 42\n")
    with pytest.raises(ConfigError, match="'project.name' must be a string"):
        load_config(tmp_path)


def test_load_config_name_as_boolean_rejected(tmp_path):
    """YAML boolean name raises ConfigError."""
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: true\n")
    with pytest.raises(ConfigError, match="'project.name' must be a string"):
        load_config(tmp_path)


def test_load_config_entry_as_boolean_rejected(tmp_path):
    """YAML boolean entry raises ConfigError."""
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: myproj\n  entry: true\n")
    with pytest.raises(ConfigError, match="'project.entry' must be a string"):
        load_config(tmp_path)


def test_load_config_entry_as_integer_rejected(tmp_path):
    """YAML integer entry raises ConfigError."""
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: myproj\n  entry: 42\n")
    with pytest.raises(ConfigError, match="'project.entry' must be a string"):
        load_config(tmp_path)


def test_load_config_backend_url_as_integer_rejected(tmp_path):
    """YAML integer backend.url raises ConfigError."""
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: myproj\nbackend:\n  url: 123\n")
    with pytest.raises(ConfigError, match="'backend.url' must be a string"):
        load_config(tmp_path)


def test_load_config_backend_url_as_boolean_rejected(tmp_path):
    """YAML boolean backend.url raises ConfigError."""
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: myproj\nbackend:\n  url: true\n")
    with pytest.raises(ConfigError, match="'backend.url' must be a string"):
        load_config(tmp_path)


# ---------------------------------------------------------------------------
# R8-S2: Colon-in-var-key namespace injection
# ---------------------------------------------------------------------------


def test_merge_var_colon_in_key_rejected(tmp_path):
    """--var with colon in key raises ConfigError."""
    config = _make_config(tmp_path)
    with pytest.raises(ConfigError, match="contains ':'"):
        config = merge_variables(config, var=["aws:region=us-east-1"])


def test_merge_var_file_colon_in_key_rejected(tmp_path):
    """--var-file with colon in key raises ConfigError."""
    config = _make_config(tmp_path)
    var_file = tmp_path / "vars.yaml"
    var_file.write_text("aws:region: us-east-1\n")
    with pytest.raises(ConfigError, match="contains ':'"):
        config = merge_variables(config, var_file=[str(var_file)])


def test_merge_env_var_colon_in_key_ignored(tmp_path, monkeypatch):
    """TLUMI_VAR_* with colon in derived key is silently ignored."""
    config = _make_config(tmp_path)
    monkeypatch.setenv("TLUMI_VAR_aws:region", "us-east-1")
    config = merge_variables(config)
    assert "aws:region" not in config.variables


# ---------------------------------------------------------------------------
# R10-S1: Absolute entry path rejection
# ---------------------------------------------------------------------------


def test_absolute_entry_path_rejected():
    """ProjectConfig rejects absolute entry paths."""
    from pathlib import Path

    from tlumi.config import ProjectConfig

    with pytest.raises(ConfigError, match="absolute path"):
        ProjectConfig(name="test", project_dir=Path("/tmp/test"), entry="/etc/passwd")


# ---------------------------------------------------------------------------
# R10-S3: Colon in yaml variable key rejected
# ---------------------------------------------------------------------------


def test_yaml_variable_with_colon_rejected(tmp_path):
    """Variable key with colon in tlumi.yaml raises ConfigError."""
    (tmp_path / "tlumi.yaml").write_text(
        "project:\n  name: myproj\nvariables:\n  'aws:region': us-east-1\n"
    )
    with pytest.raises(ConfigError, match="contains ':'"):
        load_config(tmp_path)


# ---------------------------------------------------------------------------
# Symlinked tlumi.yaml rejection
# ---------------------------------------------------------------------------


def test_load_config_rejects_symlinked_yaml(tmp_path):
    """load_config rejects tlumi.yaml that is a symlink."""
    real_yaml = tmp_path / "real.yaml"
    real_yaml.write_text("project:\n  name: myproj\n")
    config_path = tmp_path / "tlumi.yaml"
    config_path.symlink_to(real_yaml)
    with pytest.raises(ConfigError, match="symlink"):
        load_config(tmp_path)


# ---------------------------------------------------------------------------
# Colon in variable key via programmatic construction
# ---------------------------------------------------------------------------


def test_post_init_rejects_colon_in_variable_key():
    """ProjectConfig.__post_init__ rejects colon in variable keys."""
    from pathlib import Path

    from tlumi.config import ProjectConfig

    with pytest.raises(ConfigError, match="must not contain ':'"):
        ProjectConfig(
            name="test",
            project_dir=Path("/tmp/test"),
            variables={"aws:region": "us-east-1"},
        )


# ---------------------------------------------------------------------------
# merge_variables: full 4-layer priority chain
# ---------------------------------------------------------------------------


def test_merge_variables_full_priority_chain(tmp_path, monkeypatch):
    """All 4 variable layers applied: --var > --var-file > TLUMI_VAR_* > yaml."""
    config = _make_config(tmp_path, {"region": "from-yaml"})
    monkeypatch.setenv("TLUMI_VAR_REGION", "from-env")
    var_file = tmp_path / "vars.yaml"
    var_file.write_text("region: from-file\n")
    config = merge_variables(config, var=["region=from-cli"], var_file=[str(var_file)])
    assert config.variables["region"] == "from-cli"


def test_merge_variables_env_overrides_yaml(tmp_path, monkeypatch):
    """TLUMI_VAR_* overrides yaml when no --var-file or --var."""
    config = _make_config(tmp_path, {"region": "from-yaml"})
    monkeypatch.setenv("TLUMI_VAR_REGION", "from-env")
    config = merge_variables(config)
    assert config.variables["region"] == "from-env"


def test_merge_variables_var_file_overrides_env(tmp_path, monkeypatch):
    """--var-file overrides TLUMI_VAR_* when no --var."""
    config = _make_config(tmp_path, {"region": "from-yaml"})
    monkeypatch.setenv("TLUMI_VAR_REGION", "from-env")
    var_file = tmp_path / "vars.yaml"
    var_file.write_text("region: from-file\n")
    config = merge_variables(config, var_file=[str(var_file)])
    assert config.variables["region"] == "from-file"


# ---------------------------------------------------------------------------
# Public-release review: symlinked entry, numeric coercion, unknown keys
# ---------------------------------------------------------------------------


def test_load_config_allows_symlinked_entry(tmp_path):
    """A symlinked infra.py (README multi-env layout) is accepted (#46)."""
    import os

    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "infra.py").write_text("import pulumi\n")
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "tlumi.yaml").write_text("project:\n  name: dev\nsecrets:\n  allow_unencrypted: true\n")
    os.symlink("../shared/infra.py", dev / "infra.py")

    config = load_config(dev)
    assert config.entry == "infra.py"


def test_load_config_still_rejects_dotdot_entry(tmp_path):
    """The '..' traversal rejection survives the lexical-check change (#46)."""
    (tmp_path / "tlumi.yaml").write_text(
        "project:\n  name: myproj\n  entry: '../shared/infra.py'\n"
    )
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_load_config_rejects_float_variable(tmp_path):
    """An unquoted decimal variable is rejected (YAML would corrupt 1.10 -> 1.1) (#37)."""
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: myproj\nvariables:\n  version: 1.10\n")
    with pytest.raises(ConfigError, match="decimal"):
        load_config(tmp_path)


def test_load_config_allows_int_variable(tmp_path):
    """Integer variables round-trip losslessly and are still allowed (#37)."""
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: myproj\nvariables:\n  count: 3\n")
    config = load_config(tmp_path)
    assert config.variables["count"] == "3"


def test_merge_variables_rejects_float_in_var_file(tmp_path):
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: myproj\n")
    config = load_config(tmp_path)
    var_file = tmp_path / "vars.yaml"
    var_file.write_text("version: 2.30\n")
    with pytest.raises(ConfigError, match="decimal"):
        merge_variables(config, var_file=[str(var_file)])


def test_load_config_warns_on_unknown_top_level_key(tmp_path, caplog):
    """A misspelled top-level key warns with a suggestion instead of silent drop (#47)."""
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: myproj\nvars:\n  region: us-east-1\n")
    with caplog.at_level("WARNING", logger="tlumi.config"):
        load_config(tmp_path)
    assert "Unknown key 'vars'" in caplog.text
    assert "variables" in caplog.text


def test_load_config_warns_on_unknown_secrets_key(tmp_path, caplog):
    (tmp_path / "tlumi.yaml").write_text(
        "project:\n  name: myproj\nsecrets:\n  allow_unencripted: true\n"
    )
    with caplog.at_level("WARNING", logger="tlumi.config"):
        load_config(tmp_path)
    assert "allow_unencripted" in caplog.text
    assert "allow_unencrypted" in caplog.text


def test_load_config_no_warning_for_known_keys(tmp_path, caplog):
    (tmp_path / "tlumi.yaml").write_text(
        "project:\n  name: myproj\n  entry: infra.py\n"
        "backend:\n  url: s3://b\n"
        "secrets:\n  allow_unencrypted: true\n  warn_unencrypted: false\n"
        "variables:\n  region: us-east-1\n"
    )
    with caplog.at_level("WARNING", logger="tlumi.config"):
        load_config(tmp_path)
    assert "Unknown key" not in caplog.text


# ---------------------------------------------------------------------------
# Hardening sweep: non-decimal int variables are lossy under YAML 1.1 and
# must be rejected like floats (0777 -> 511, 1:30 -> 90 with no warning)
# ---------------------------------------------------------------------------


def test_load_config_rejects_octal_int_variable(tmp_path):
    """A leading-zero octal variable is rejected with the ORIGINAL text, not 511."""
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: myproj\nvariables:\n  mode: 0777\n")
    with pytest.raises(ConfigError, match="non-decimal") as excinfo:
        load_config(tmp_path)
    # The value echoed back is the user's raw text, not PyYAML's int rewrite.
    assert "(0777)" in str(excinfo.value)
    assert "(511)" not in str(excinfo.value)
    assert 'mode: "0777"' in (excinfo.value.hint or "")


def test_load_config_rejects_sexagesimal_int_variable(tmp_path):
    """A sexagesimal value (1:30 -> 90 under YAML 1.1) is rejected."""
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: myproj\nvariables:\n  duration: 1:30\n")
    with pytest.raises(ConfigError, match="non-decimal") as excinfo:
        load_config(tmp_path)
    assert "1:30" in str(excinfo.value)


def test_load_config_rejects_hex_int_variable(tmp_path):
    """A hex value (0x1A -> 26 under YAML 1.1) is rejected."""
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: myproj\nvariables:\n  flag: 0x1A\n")
    with pytest.raises(ConfigError, match="non-decimal"):
        load_config(tmp_path)


def test_load_config_rejects_underscore_int_variable(tmp_path):
    """An underscore-separated value (1_000 -> 1000 under YAML 1.1) is rejected."""
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: myproj\nvariables:\n  size: 1_000\n")
    with pytest.raises(ConfigError, match="non-decimal"):
        load_config(tmp_path)


def test_load_config_quoted_octal_variable_preserved(tmp_path):
    """Quoting the value (as the hint suggests) preserves the exact text."""
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: myproj\nvariables:\n  mode: '0777'\n")
    config = load_config(tmp_path)
    assert config.variables["mode"] == "0777"


def test_load_config_negative_int_variable_allowed(tmp_path):
    """Plain decimal ints (including negatives) still round-trip losslessly."""
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: myproj\nvariables:\n  offset: -17\n")
    config = load_config(tmp_path)
    assert config.variables["offset"] == "-17"


def test_load_config_bool_variable_still_lowercase(tmp_path):
    """The strict int loader leaves YAML bool coercion untouched."""
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: myproj\nvariables:\n  debug: True\n")
    config = load_config(tmp_path)
    assert config.variables["debug"] == "true"


def test_merge_variables_rejects_octal_in_var_file(tmp_path):
    """The strict loader also guards --var-file parsing."""
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: myproj\n")
    config = load_config(tmp_path)
    var_file = tmp_path / "vars.yaml"
    var_file.write_text("mode: 0644\n")
    with pytest.raises(ConfigError, match="non-decimal") as excinfo:
        merge_variables(config, var_file=[str(var_file)])
    assert "0644" in str(excinfo.value)


def test_merge_variables_quoted_octal_in_var_file_preserved(tmp_path):
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: myproj\n")
    config = load_config(tmp_path)
    var_file = tmp_path / "vars.yaml"
    var_file.write_text("mode: '0644'\n")
    config = merge_variables(config, var_file=[str(var_file)])
    assert config.variables["mode"] == "0644"


def test_strict_loader_does_not_leak_into_stock_safe_loader():
    """Importing tlumi.config must not mutate yaml.SafeLoader's resolvers."""
    import yaml

    import tlumi.config  # noqa: F401  (ensure the module-level setup ran)

    assert yaml.safe_load("x: 0777")["x"] == 511
    assert yaml.safe_load("x: 3")["x"] == 3
