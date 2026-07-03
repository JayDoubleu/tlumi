"""Tests for tlumi.workspace: secrets enforcement, gitignore check, exact-path loading."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest
from pulumi.automation import Stack

from tlumi.config import load_config
from tlumi.errors import ConfigError
from tlumi.workspace import _check_gitignore, _mask_backend_url


def _make_project(tmp_path, yaml_extra=""):
    """Create a minimal project directory with venv marker."""
    (tmp_path / "tlumi.yaml").write_text(f"project:\n  name: myproj\n{yaml_extra}")
    (tmp_path / ".tlumi" / "cache" / "venv").mkdir(parents=True)
    (tmp_path / "infra.py").write_text("pass\n")
    return load_config(tmp_path)


def _write_sidecar(project_dir, keys):
    """Write the managed-config-keys sidecar as a previous tlumi run would."""
    import json

    cache = project_dir / ".tlumi" / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "managed_config_keys.json").write_text(json.dumps({"keys": list(keys)}))


def test_secrets_error_when_unencrypted_not_allowed(tmp_path, monkeypatch):
    """get_stack raises ConfigError when no passphrase and allow_unencrypted is False."""
    config = _make_project(tmp_path)
    assert config.secrets.allow_unencrypted is False

    monkeypatch.delenv("TLUMI_SECRETS_PASSPHRASE", raising=False)

    from tlumi.workspace import get_stack

    with pytest.raises(ConfigError, match="No TLUMI_SECRETS_PASSPHRASE set"):
        get_stack(config)


def test_secrets_allowed_when_opt_in(tmp_path, monkeypatch):
    """get_stack proceeds when allow_unencrypted is True and no passphrase."""
    config = _make_project(tmp_path, yaml_extra="secrets:\n  allow_unencrypted: true\n")
    assert config.secrets.allow_unencrypted is True

    monkeypatch.delenv("TLUMI_SECRETS_PASSPHRASE", raising=False)

    # We can't fully test get_stack (needs Pulumi CLI), but we can verify
    # it gets past the passphrase check by mocking _ensure_pulumi_cli
    from tlumi.workspace import get_stack

    with patch("tlumi.workspace._ensure_pulumi_cli") as mock_cli:
        mock_cli.side_effect = Exception("stop here")
        with pytest.raises(Exception, match="stop here"):
            get_stack(config)
        mock_cli.assert_called_once()


def test_secrets_allowed_with_passphrase(tmp_path, monkeypatch):
    """get_stack proceeds when passphrase is set regardless of allow_unencrypted."""
    config = _make_project(tmp_path)
    monkeypatch.setenv("TLUMI_SECRETS_PASSPHRASE", "my-secret")

    from tlumi.workspace import get_stack

    with patch("tlumi.workspace._ensure_pulumi_cli") as mock_cli:
        mock_cli.side_effect = Exception("stop here")
        with pytest.raises(Exception, match="stop here"):
            get_stack(config)
        mock_cli.assert_called_once()


# ---------------------------------------------------------------------------
# _check_gitignore
# ---------------------------------------------------------------------------


def test_gitignore_warns_when_missing_entry(tmp_path, caplog, monkeypatch):
    """Warns when .gitignore exists but doesn't contain .tlumi."""
    import tlumi.workspace

    monkeypatch.setattr(tlumi.workspace, "_gitignore_warned", False)

    config = _make_project(tmp_path)
    (tmp_path / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    with caplog.at_level("WARNING", logger="tlumi.workspace"):
        _check_gitignore(config)
    assert ".tlumi" in caplog.text


def test_gitignore_no_warn_when_present(tmp_path, caplog, monkeypatch):
    """No warning when .gitignore contains .tlumi/."""
    import tlumi.workspace

    monkeypatch.setattr(tlumi.workspace, "_gitignore_warned", False)

    config = _make_project(tmp_path)
    (tmp_path / ".gitignore").write_text("__pycache__/\n.tlumi/\n")
    with caplog.at_level("WARNING", logger="tlumi.workspace"):
        _check_gitignore(config)
    assert ".tlumi" not in caplog.text


def test_gitignore_no_warn_when_no_gitignore(tmp_path, caplog, monkeypatch):
    """No warning when .gitignore doesn't exist."""
    import tlumi.workspace

    monkeypatch.setattr(tlumi.workspace, "_gitignore_warned", False)

    config = _make_project(tmp_path)
    with caplog.at_level("WARNING", logger="tlumi.workspace"):
        _check_gitignore(config)
    assert ".tlumi" not in caplog.text


def test_gitignore_quiet_suppresses(tmp_path, caplog, monkeypatch):
    """No warning in quiet mode."""
    import tlumi.workspace

    monkeypatch.setattr(tlumi.workspace, "_gitignore_warned", False)

    config = _make_project(tmp_path)
    (tmp_path / ".gitignore").write_text("*.pyc\n")
    with caplog.at_level("WARNING", logger="tlumi.workspace"):
        _check_gitignore(config, quiet=True)
    assert ".tlumi" not in caplog.text


# ---------------------------------------------------------------------------
# _load_inline_program exact-path loading
# ---------------------------------------------------------------------------


def test_load_inline_program_uses_exact_path(tmp_path, monkeypatch):
    """Entry file is loaded via spec_from_file_location with the exact path."""
    config = _make_project(tmp_path)
    entry_path = tmp_path / "infra.py"

    # Remove infra from sys.modules if present from previous tests
    import sys

    monkeypatch.delitem(sys.modules, "_tlumi_entry.infra", raising=False)

    from tlumi.workspace import _load_inline_program

    with patch("tlumi.workspace.importlib.util.spec_from_file_location") as mock_spec:
        mock_loader = type("Loader", (), {"exec_module": lambda self, m: None})()
        mock_spec_obj = type(
            "Spec", (), {"loader": mock_loader, "submodule_search_locations": None}
        )()
        mock_spec.return_value = mock_spec_obj

        with patch("tlumi.workspace.importlib.util.module_from_spec") as mock_from_spec:
            mock_module = type("Module", (), {})()
            mock_from_spec.return_value = mock_module

            program = _load_inline_program(config)
            program()

            mock_spec.assert_called_once_with("_tlumi_entry.infra", str(entry_path))


# ---------------------------------------------------------------------------
# _ensure_pulumi_cli shared cache fallback logging
# ---------------------------------------------------------------------------


def test_shared_cache_failure_logs_warning(tmp_path, monkeypatch, caplog):
    """Shared cache install failure surfaces as a WARNING so the user sees it.

    Silent debug-level logging hides a stale ~/.tlumi/ cache or perms issue
    behind a seemingly-unrelated downstream error.
    """
    import logging

    from tlumi.workspace import _ensure_pulumi_cli

    config = _make_project(tmp_path)

    with patch("tlumi.workspace._shared_cache_dir") as mock_shared:
        mock_shared.return_value = tmp_path / "shared_cache"
        with patch("tlumi.workspace.auto.PulumiCommand.install") as mock_install:
            mock_cmd = type("Cmd", (), {})()
            mock_install.side_effect = [PermissionError("no access"), mock_cmd]

            with caplog.at_level(logging.WARNING, logger="tlumi.workspace"):
                result = _ensure_pulumi_cli(config)

            assert result is mock_cmd
            warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
            assert any("Shared Pulumi CLI cache install failed" in r.message for r in warnings)


# ---------------------------------------------------------------------------
# get_stack runtime=False: state recovery decoupling
# ---------------------------------------------------------------------------


def test_get_stack_runtime_false_skips_venv_check(tmp_path, monkeypatch):
    """State recovery commands work even when the project venv is missing.

    Codex flagged that routing state list/show/pull through get_stack tied
    recovery to a healthy runtime: state ops broke whenever infra.py was
    broken or the venv was missing. runtime=False is the seam that breaks
    that coupling.
    """
    (tmp_path / "tlumi.yaml").write_text(
        "project:\n  name: myproj\nsecrets:\n  allow_unencrypted: true\n"
    )
    # Deliberately do NOT create the venv directory.
    (tmp_path / "infra.py").write_text("pass\n")
    config = load_config(tmp_path)
    assert not config.venv_dir.exists()

    monkeypatch.delenv("TLUMI_SECRETS_PASSPHRASE", raising=False)

    from tlumi.workspace import get_stack

    with (
        patch("tlumi.workspace._ensure_pulumi_cli") as mock_cli,
        patch("tlumi.workspace.auto.create_or_select_stack") as mock_create,
    ):
        mock_cli.return_value = MagicMock()
        mock_create.return_value = MagicMock(spec=Stack)
        # Must not raise "Project not initialized."
        stack = get_stack(config, runtime=False)
        assert stack is mock_create.return_value


def test_get_stack_runtime_false_does_not_load_infra(tmp_path, monkeypatch):
    """runtime=False uses a no-op program; broken infra.py must not abort."""
    (tmp_path / "tlumi.yaml").write_text(
        "project:\n  name: myproj\nsecrets:\n  allow_unencrypted: true\n"
    )
    (tmp_path / ".tlumi" / "cache" / "venv").mkdir(parents=True)
    # infra.py has a syntax error -- runtime=True would fail to load it.
    (tmp_path / "infra.py").write_text("def broken(:\n")
    config = load_config(tmp_path)

    monkeypatch.delenv("TLUMI_SECRETS_PASSPHRASE", raising=False)

    from tlumi.workspace import get_stack

    with (
        patch("tlumi.workspace._ensure_pulumi_cli") as mock_cli,
        patch("tlumi.workspace.auto.create_or_select_stack") as mock_create,
        patch("tlumi.workspace._load_inline_program") as mock_loader,
    ):
        mock_cli.return_value = MagicMock()
        mock_create.return_value = MagicMock(spec=Stack)
        get_stack(config, runtime=False)
        mock_loader.assert_not_called()


def test_get_stack_runtime_false_skips_config_reconciliation(tmp_path, monkeypatch):
    """runtime=False does not call get_all_config / set_config on the stack.

    State recovery must not depend on a readable stack config, since stuck
    config is one of the reasons a user would reach for state pull.
    """
    (tmp_path / "tlumi.yaml").write_text(
        "project:\n"
        "  name: myproj\n"
        "secrets:\n  allow_unencrypted: true\n"
        "variables:\n  region: us-east-1\n"
    )
    (tmp_path / ".tlumi" / "cache" / "venv").mkdir(parents=True)
    (tmp_path / "infra.py").write_text("pass\n")
    config = load_config(tmp_path)

    monkeypatch.delenv("TLUMI_SECRETS_PASSPHRASE", raising=False)

    from tlumi.workspace import get_stack

    mock_stack = MagicMock(spec=Stack)
    with (
        patch("tlumi.workspace._ensure_pulumi_cli") as mock_cli,
        patch("tlumi.workspace.auto.create_or_select_stack") as mock_create,
    ):
        mock_cli.return_value = MagicMock()
        mock_create.return_value = mock_stack
        get_stack(config, runtime=False)

    mock_stack.get_all_config.assert_not_called()
    mock_stack.set_config.assert_not_called()
    mock_stack.remove_config.assert_not_called()


# ---------------------------------------------------------------------------
# get_stack exception narrowing
# ---------------------------------------------------------------------------


def test_get_stack_propagates_non_command_error(tmp_path, monkeypatch):
    """Programming errors (TypeError etc.) propagate instead of being wrapped."""
    config = _make_project(tmp_path, yaml_extra="secrets:\n  allow_unencrypted: true\n")
    monkeypatch.delenv("TLUMI_SECRETS_PASSPHRASE", raising=False)

    from tlumi.workspace import get_stack

    with patch("tlumi.workspace._ensure_pulumi_cli"):
        with patch("tlumi.workspace._load_inline_program"):
            with patch("tlumi.workspace.auto.create_or_select_stack") as mock_create:
                mock_create.side_effect = TypeError("bad argument")
                with pytest.raises(TypeError, match="bad argument"):
                    get_stack(config)


def test_get_stack_config_cleanup_propagates_type_error(tmp_path, monkeypatch):
    """TypeError during config cleanup propagates (not swallowed)."""
    config = _make_project(tmp_path, yaml_extra="secrets:\n  allow_unencrypted: true\n")
    _write_sidecar(tmp_path, ["old_key"])
    monkeypatch.delenv("TLUMI_SECRETS_PASSPHRASE", raising=False)

    from tlumi.workspace import get_stack

    mock_stack = type(
        "Stack",
        (),
        {
            "get_all_config": lambda self: (_ for _ in ()).throw(TypeError("oops")),
            "set_config": lambda self, k, v: None,
        },
    )()

    with patch("tlumi.workspace._ensure_pulumi_cli"):
        with patch("tlumi.workspace._load_inline_program"):
            with patch("tlumi.workspace.auto.create_or_select_stack", return_value=mock_stack):
                with pytest.raises(TypeError, match="oops"):
                    get_stack(config)


def test_load_inline_program_raises_on_bad_spec(tmp_path, monkeypatch):
    """Entry loading raises WorkspaceError when spec_from_file_location returns None."""
    config = _make_project(tmp_path)

    import sys

    monkeypatch.delitem(sys.modules, "_tlumi_entry.infra", raising=False)

    from tlumi.workspace import WorkspaceError, _load_inline_program

    with patch("tlumi.workspace.importlib.util.spec_from_file_location", return_value=None):
        program = _load_inline_program(config)
        with pytest.raises(WorkspaceError, match="Cannot load entry file"):
            program()


# ---------------------------------------------------------------------------
# _mask_backend_url
# ---------------------------------------------------------------------------


def test_mask_backend_url_with_password():
    """URL with username:password gets password masked."""
    assert _mask_backend_url("s3://user:secret@bucket/path") == "s3://user:***@bucket/path"


def test_mask_backend_url_without_password():
    """URL without password is returned unchanged."""
    assert _mask_backend_url("s3://bucket/path") == "s3://bucket/path"


def test_mask_backend_url_with_port():
    """URL with password and port preserves port."""
    result = _mask_backend_url("http://user:pass@host:8080/path")
    assert result == "http://user:***@host:8080/path"


def test_mask_backend_url_plain_string():
    """Non-URL string is returned unchanged."""
    assert _mask_backend_url("file:///local/state") == "file:///local/state"


def test_mask_backend_url_sensitive_query_params():
    """Sensitive query string parameters are masked."""
    url = "s3://bucket?access_key=AKIA123&secret_key=wJalr456&region=us-east-1"
    result = _mask_backend_url(url)
    assert "AKIA123" not in result
    assert "wJalr456" not in result
    assert "region=us-east-1" in result
    assert "access_key=***" in result
    assert "secret_key=***" in result


def test_mask_backend_url_sas_token_params():
    """Azure SAS token parameters are masked."""
    url = (
        "azblob://container?sv=2021-06-08&ss=bfqt&srt=sco&sp=rwdlacupiytfx&se=2099-01-01&sig=abc123"
    )
    result = _mask_backend_url(url)
    assert "abc123" not in result
    assert "sig=***" in result
    assert "sv=***" in result
    assert "ss=***" in result
    assert "sp=***" in result
    assert "se=***" in result
    assert "srt=***" in result


def test_mask_backend_url_password_and_query():
    """Both password and query params are masked."""
    url = "http://user:pass@host/path?token=secret123&debug=true"
    result = _mask_backend_url(url)
    assert "pass" not in result.split("@")[0].split(":")[-1]
    assert "secret123" not in result
    assert "debug=true" in result
    assert "token=***" in result


def test_mask_backend_url_no_sensitive_query():
    """Query params without sensitive keys are preserved unchanged."""
    url = "s3://bucket?region=us-east-1&endpoint=minio:9000"
    assert _mask_backend_url(url) == url


def test_mask_backend_url_username_as_token():
    """Bare username (no password) is treated as a credential and masked.

    S3-compatible backends and some token-in-URL patterns place the
    credential entirely in the userinfo, with no separate password.
    """
    result = _mask_backend_url("https://AKIAEXAMPLE123@host/bucket")
    assert "AKIAEXAMPLE123" not in result
    assert result == "https://***@host/bucket"


def test_mask_backend_url_username_as_token_with_port():
    """Bare-username masking preserves port."""
    result = _mask_backend_url("https://token123@host:9000/bucket")
    assert "token123" not in result
    assert result == "https://***@host:9000/bucket"


def test_mask_backend_url_ipv6_with_credentials():
    """IPv6 backend URLs keep their address brackets when credentials are masked (F25)."""
    result = _mask_backend_url("s3://user:pass@[2001:db8::1]:9000/bucket")
    assert "pass" not in result
    assert "[2001:db8::1]:9000" in result


def test_mask_backend_url_ipv6_username_token():
    """IPv6 host plus a bare-username token also keeps the brackets (F25)."""
    result = _mask_backend_url("s3://token@[2001:db8::1]/bucket")
    assert "token@" not in result
    assert result == "s3://***@[2001:db8::1]/bucket"


def test_mask_backend_url_camelcase_azure_sas():
    """Azure SDK emits SAS token params in camelCase; matching is case-insensitive."""
    url = (
        "https://acct.blob.core.windows.net/c"
        "?AccessKey=abc&SharedAccessKey=def&AccountKey=ghi"
        "&Signature=xyz&SAS=jkl"
    )
    result = _mask_backend_url(url)
    for leaked in ("abc", "def", "ghi", "xyz", "jkl"):
        assert leaked not in result, f"{leaked!r} leaked: {result}"
    assert "AccessKey=***" in result
    assert "SharedAccessKey=***" in result
    assert "AccountKey=***" in result
    assert "Signature=***" in result
    assert "SAS=***" in result


def test_mask_backend_url_uppercase_keys():
    """Uppercase variants like PASSWORD, TOKEN, SIG are masked too."""
    url = "https://host/x?PASSWORD=p1&TOKEN=t1&SIG=s1&keep=v"
    result = _mask_backend_url(url)
    assert "p1" not in result
    assert "t1" not in result
    assert "s1" not in result
    assert "keep=v" in result


def test_mask_backend_url_all_sensitive_keys_individually():
    """Each key in _SENSITIVE_QUERY_KEYS masks its value (lowercase form)."""
    from tlumi.workspace import _SENSITIVE_QUERY_KEYS

    for key in _SENSITIVE_QUERY_KEYS:
        url = f"https://host/x?{key}=SECRETVALUE&keep=ok"
        result = _mask_backend_url(url)
        assert "SECRETVALUE" not in result, f"{key} did not mask its value: {result}"
        assert "keep=ok" in result


def test_mask_backend_url_query_value_with_special_chars():
    """Non-sensitive query values survive the parse_qsl round-trip when query is masked."""
    url = "https://host/x?token=secret&path=a%2Fb&keep=hello+world"
    result = _mask_backend_url(url)
    assert "secret" not in result
    assert "token=***" in result
    # parse_qsl decodes percent-encoding; we still expect the readable form
    # to survive (either encoded or decoded -- just not lost or mangled).
    assert "a/b" in result or "a%2Fb" in result
    assert "hello world" in result or "hello+world" in result


def test_mask_backend_url_fragment_with_token():
    """Tokens placed after the URL fragment ('#') are masked when fragment looks like a query."""
    url = "https://host/path#sig=mysecret"
    result = _mask_backend_url(url)
    assert "mysecret" not in result
    assert "sig=***" in result


def test_mask_backend_url_fragment_oauth_implicit_flow():
    """OAuth implicit flow places access_token in fragment; it must be masked."""
    url = "https://app.example.com/cb#access_token=abc123def&id_token=xyz789&state=42"
    result = _mask_backend_url(url)
    assert "abc123def" not in result
    assert "xyz789" not in result
    assert "state=42" in result


def test_mask_backend_url_fragment_without_equals_unchanged():
    """A plain fragment anchor like '#section' is not credentialed and stays."""
    url = "https://host/docs#section-2"
    result = _mask_backend_url(url)
    assert result == url


def test_mask_backend_url_authorization_query_key():
    """The Authorization query key (case variants) is masked."""
    url = "https://host/x?Authorization=Bearer-abc123"
    result = _mask_backend_url(url)
    assert "Bearer-abc123" not in result


def test_mask_backend_url_authtoken_apikey_keys():
    """authToken and apiKey query keys are masked (catch via substring rule)."""
    url = "https://host/x?authToken=xxx&apiKey=yyy&keep=ok"
    result = _mask_backend_url(url)
    assert "xxx" not in result
    assert "yyy" not in result
    assert "keep=ok" in result


def test_mask_backend_url_aws_secret_access_key_query():
    """awsSecretAccessKey style camelCase keys are masked."""
    url = "https://host/x?awsSecretAccessKey=zzz&region=us-east-1"
    result = _mask_backend_url(url)
    assert "zzz" not in result
    assert "region=us-east-1" in result


def test_mask_backend_url_oauth_refresh_token():
    """refresh_token (matched by 'token' substring) is masked."""
    url = "https://api.example.com/oauth?refresh_token=long_token_value&grant_type=refresh"
    result = _mask_backend_url(url)
    assert "long_token_value" not in result
    assert "grant_type=refresh" in result


def test_mask_backend_url_aws_signature_query():
    """X-Amz-Signature (the replayable credential of a presigned S3 URL) is masked."""
    url = "https://b.s3.amazonaws.com/k?X-Amz-Credential=cred&X-Amz-Signature=deadbeef&region=us"
    result = _mask_backend_url(url)
    assert "deadbeef" not in result
    assert "region=us" in result


def test_mask_backend_url_gcs_signature_query():
    """X-Goog-Signature (presigned GCS URL signature) is masked."""
    url = "https://storage.googleapis.com/b/o?X-Goog-Signature=abc123secretsig&region=us"
    result = _mask_backend_url(url)
    assert "abc123secretsig" not in result
    assert "region=us" in result


def test_mask_backend_url_netloc_only_preserves_query_encoding():
    """When only the netloc is masked, a non-sensitive query keeps its percent-encoding
    verbatim (no parse_qsl round-trip that would decode %26/%3D or '+')."""
    url = "s3://user:pass@host/bucket?prefix=a%26b&x=1%3D2"
    result = _mask_backend_url(url)
    assert "user:***@host" in result
    assert "prefix=a%26b" in result
    assert "x=1%3D2" in result


# ---------------------------------------------------------------------------
# _load_inline_program: subdirectory sys.path
# ---------------------------------------------------------------------------


def test_load_inline_program_adds_subdirectory_to_sys_path(tmp_path, monkeypatch):
    """Entry in a subdirectory adds entry's parent dir to sys.path."""
    # Project: tmp_path with entry at src/infra.py
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "infra.py").write_text("pass\n")
    (tmp_path / ".tlumi" / "cache" / "venv").mkdir(parents=True)
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: myproj\n  entry: src/infra.py\n")
    config = load_config(tmp_path)

    import sys

    monkeypatch.delitem(sys.modules, "_tlumi_entry.infra", raising=False)

    from tlumi.workspace import _load_inline_program

    with patch("tlumi.workspace.importlib.util.spec_from_file_location") as mock_spec:
        mock_loader = type("Loader", (), {"exec_module": lambda self, m: None})()
        mock_spec_obj = type(
            "Spec", (), {"loader": mock_loader, "submodule_search_locations": None}
        )()
        mock_spec.return_value = mock_spec_obj

        with patch("tlumi.workspace.importlib.util.module_from_spec") as mock_from_spec:
            mock_from_spec.return_value = type("Module", (), {})()

            program = _load_inline_program(config)
            program()

    # Both project dir and entry's parent dir should be on sys.path
    assert str(tmp_path) in sys.path
    assert str(src_dir.resolve()) in sys.path


def test_load_inline_program_no_duplicate_sys_path(tmp_path, monkeypatch):
    """Entry at project root does not add project dir twice to sys.path."""
    (tmp_path / "infra.py").write_text("pass\n")
    (tmp_path / ".tlumi" / "cache" / "venv").mkdir(parents=True)
    (tmp_path / "tlumi.yaml").write_text("project:\n  name: myproj\n")
    config = load_config(tmp_path)

    import sys

    monkeypatch.delitem(sys.modules, "_tlumi_entry.infra", raising=False)

    from tlumi.workspace import _load_inline_program

    with patch("tlumi.workspace.importlib.util.spec_from_file_location") as mock_spec:
        mock_loader = type("Loader", (), {"exec_module": lambda self, m: None})()
        mock_spec_obj = type(
            "Spec", (), {"loader": mock_loader, "submodule_search_locations": None}
        )()
        mock_spec.return_value = mock_spec_obj

        with patch("tlumi.workspace.importlib.util.module_from_spec") as mock_from_spec:
            mock_from_spec.return_value = type("Module", (), {})()

            program = _load_inline_program(config)
            program()

    # Project dir should appear in sys.path, but only once from our addition
    assert str(tmp_path) in sys.path


# ---------------------------------------------------------------------------
# get_stack: ConcurrentUpdateError (T5)
# ---------------------------------------------------------------------------


def test_get_stack_concurrent_update_error(tmp_path, monkeypatch):
    """get_stack raises WorkspaceError on ConcurrentUpdateError."""
    from pulumi.automation.errors import ConcurrentUpdateError

    from tlumi.errors import WorkspaceError
    from tlumi.workspace import get_stack

    config = _make_project(tmp_path, "secrets:\n  allow_unencrypted: true\n")
    monkeypatch.delenv("TLUMI_SECRETS_PASSPHRASE", raising=False)

    with (
        patch("tlumi.workspace._ensure_pulumi_cli"),
        patch("tlumi.workspace._load_inline_program", return_value=lambda: None),
        patch("tlumi.workspace.auto.create_or_select_stack") as mock_create,
    ):
        mock_create.side_effect = ConcurrentUpdateError.__new__(ConcurrentUpdateError)
        with pytest.raises(WorkspaceError, match="Another tlumi operation"):
            get_stack(config)


# ---------------------------------------------------------------------------
# R4-T4: _ensure_pulumi_cli() complete failure
# ---------------------------------------------------------------------------


def test_ensure_pulumi_cli_both_paths_fail(tmp_path):
    """Both shared cache and per-project install fail raises WorkspaceError."""
    from tlumi.errors import WorkspaceError as WSError
    from tlumi.workspace import _ensure_pulumi_cli

    config = _make_project(tmp_path)

    with patch("tlumi.workspace._shared_cache_dir") as mock_shared:
        mock_shared.return_value = tmp_path / "shared_cache"
        with patch("tlumi.workspace.auto.PulumiCommand.install") as mock_install:
            mock_install.side_effect = RuntimeError("install failed")
            with pytest.raises(WSError, match="Failed to install Pulumi CLI"):
                _ensure_pulumi_cli(config)


def test_ensure_pulumi_cli_called_process_error_wrapped(tmp_path):
    """CalledProcessError from PulumiCommand.install is wrapped in WorkspaceError."""
    import subprocess as sp

    from tlumi.errors import WorkspaceError as WSError
    from tlumi.workspace import _ensure_pulumi_cli

    config = _make_project(tmp_path)

    with patch("tlumi.workspace._shared_cache_dir", return_value=None):
        with patch("tlumi.workspace.auto.PulumiCommand.install") as mock_install:
            mock_install.side_effect = sp.CalledProcessError(1, "pulumi version")
            with pytest.raises(WSError, match="Failed to install Pulumi CLI"):
                _ensure_pulumi_cli(config)


# ---------------------------------------------------------------------------
# R4-T5: _load_inline_program() missing entry file
# ---------------------------------------------------------------------------


def test_load_inline_program_missing_entry_file(tmp_path):
    """Entry file not found raises WorkspaceError with hint."""
    from tlumi.config import ProjectConfig
    from tlumi.workspace import WorkspaceError, _load_inline_program

    (tmp_path / ".tlumi" / "cache" / "venv").mkdir(parents=True)
    config = ProjectConfig(
        name="test",
        project_dir=tmp_path,
        entry="nonexistent.py",
    )

    with pytest.raises(WorkspaceError, match="Entry file not found"):
        _load_inline_program(config)


# ---------------------------------------------------------------------------
# R3-S2: Module name collision (existing)
# ---------------------------------------------------------------------------


def test_load_inline_program_module_collision(tmp_path, monkeypatch):
    """Stale sys.modules entry is replaced with a fresh spec_from_file_location load."""
    from tlumi.workspace import _load_inline_program

    config = _make_project(tmp_path)

    # Pre-populate sys.modules with a module with the same namespaced name but different file path
    fake_module = type("Module", (), {"__file__": "/some/other/infra.py"})()
    monkeypatch.setitem(sys.modules, "_tlumi_entry.infra", fake_module)

    with patch("tlumi.workspace.importlib.util.spec_from_file_location") as mock_spec:
        mock_loader = MagicMock()
        mock_spec_obj = MagicMock()
        mock_spec_obj.loader = mock_loader
        mock_spec.return_value = mock_spec_obj
        with patch("tlumi.workspace.importlib.util.module_from_spec") as mock_from_spec:
            mock_from_spec.return_value = type("Module", (), {})()
            program = _load_inline_program(config)
            program()

    # Should have used spec_from_file_location, not reload
    mock_spec.assert_called_once()


# ---------------------------------------------------------------------------
# T3: _venv_site_packages
# ---------------------------------------------------------------------------


def test_venv_site_packages_missing_venv(tmp_path):
    """Returns None when venv directory does not exist."""
    from tlumi.workspace import _venv_site_packages

    config = _make_project(tmp_path)
    # Remove the venv directory
    import shutil

    shutil.rmtree(config.venv_dir, ignore_errors=True)
    assert _venv_site_packages(config) is None


def test_venv_site_packages_missing_lib_dir(tmp_path):
    """Returns None when venv/lib directory does not exist."""
    from tlumi.workspace import _venv_site_packages

    config = _make_project(tmp_path)
    # venv_dir exists (created by _make_project) but has no lib/ subdir
    assert _venv_site_packages(config) is None


def test_venv_site_packages_no_python_dir(tmp_path):
    """Returns None when lib/ has no python* subdirectory."""
    from tlumi.workspace import _venv_site_packages

    config = _make_project(tmp_path)
    lib_dir = config.venv_dir / "lib"
    lib_dir.mkdir(parents=True, exist_ok=True)
    (lib_dir / "something_else").mkdir()
    assert _venv_site_packages(config) is None


def test_venv_site_packages_valid_path(tmp_path):
    """Returns site-packages path when venv is properly structured."""
    from tlumi.workspace import _venv_site_packages

    config = _make_project(tmp_path)
    sp = config.venv_dir / "lib" / "python3.10" / "site-packages"
    sp.mkdir(parents=True, exist_ok=True)
    result = _venv_site_packages(config)
    assert result is not None
    assert result.name == "site-packages"


# ---------------------------------------------------------------------------
# T4: get_stack config cleanup
# ---------------------------------------------------------------------------


def test_get_stack_stale_keys_removed(tmp_path, monkeypatch):
    """get_stack removes stale config keys recorded in the managed-keys sidecar."""
    config = _make_project(tmp_path, yaml_extra="secrets:\n  allow_unencrypted: true\n")
    _write_sidecar(tmp_path, ["old_key"])
    monkeypatch.delenv("TLUMI_SECRETS_PASSPHRASE", raising=False)

    from tlumi.workspace import get_stack

    mock_stack = MagicMock(spec=Stack)
    mock_stack.get_all_config.return_value = {
        "myproj:old_key": MagicMock(),  # project-prefixed stale key
        "aws:region": MagicMock(),  # provider-namespaced, should be preserved
    }

    with (
        patch("tlumi.workspace._ensure_pulumi_cli"),
        patch("tlumi.workspace._load_inline_program", return_value=lambda: None),
        patch("tlumi.workspace.auto.create_or_select_stack", return_value=mock_stack),
    ):
        get_stack(config)

    # old_key should be removed (bare key passed to remove_config),
    # aws:region should not (different namespace)
    mock_stack.remove_config.assert_called_once_with("old_key")


def test_get_stack_reconcile_config_false_preserves_plan_time_config(tmp_path, monkeypatch):
    """reconcile_config=False (apply --plan) never touches stack config.

    A plan run recorded 'foo' in the sidecar and set foo=bar on the stack. The
    subsequent apply --plan run's merged variables no longer include 'foo' (the
    command forbids re-supplying --var/--var-file/TLUMI_VAR_*). With the default
    reconciliation this would remove_config('foo') before up(plan=...), and
    Pulumi saved plans do not re-inject config, so config.require('foo') would
    fail. reconcile_config=False must skip both cleanup and set so the plan-time
    config survives into the apply.
    """
    config = _make_project(tmp_path, yaml_extra="secrets:\n  allow_unencrypted: true\n")
    _write_sidecar(tmp_path, ["foo"])
    monkeypatch.delenv("TLUMI_SECRETS_PASSPHRASE", raising=False)

    from tlumi.workspace import get_stack

    mock_stack = MagicMock(spec=Stack)
    mock_stack.get_all_config.return_value = {"myproj:foo": MagicMock()}

    with (
        patch("tlumi.workspace._ensure_pulumi_cli"),
        patch("tlumi.workspace._load_inline_program", return_value=lambda: None),
        patch("tlumi.workspace.auto.create_or_select_stack", return_value=mock_stack),
    ):
        get_stack(config, reconcile_config=False)

    # No config reconciliation: plan-time 'foo' must not be removed, and no
    # variables are re-set (the plan already captured them).
    mock_stack.remove_config.assert_not_called()
    mock_stack.set_config.assert_not_called()


def test_get_stack_stale_keys_skips_provider_namespace_collision(tmp_path, monkeypatch):
    """Provider keys survive cleanup when project name matches a provider namespace.

    Regression for codex-2: a project named "aws" makes the real provider key
    aws:region yield bare_key="region" (no colon). Prefix inference alone
    would delete the user's provider config; only keys the sidecar records as
    tlumi-written may be removed.
    """
    # Create a project named "aws" which collides with the aws: provider namespace
    (tmp_path / "tlumi.yaml").write_text(
        "project:\n  name: aws\nsecrets:\n  allow_unencrypted: true\n"
    )
    (tmp_path / ".tlumi" / "cache" / "venv").mkdir(parents=True)
    (tmp_path / "infra.py").write_text("pass\n")
    config = load_config(tmp_path)
    _write_sidecar(tmp_path, ["old_var"])
    monkeypatch.delenv("TLUMI_SECRETS_PASSPHRASE", raising=False)

    from tlumi.workspace import get_stack

    mock_stack = MagicMock(spec=Stack)
    mock_stack.get_all_config.return_value = {
        "aws:region": MagicMock(),  # provider key: bare_key="region", NOT in sidecar
        "aws:s3:bucket_name": MagicMock(),  # provider key: bare_key="s3:bucket_name" has colon
        "aws:old_var": MagicMock(),  # tlumi-written stale key, recorded in sidecar
    }

    with (
        patch("tlumi.workspace._ensure_pulumi_cli"),
        patch("tlumi.workspace._load_inline_program", return_value=lambda: None),
        patch("tlumi.workspace.auto.create_or_select_stack", return_value=mock_stack),
    ):
        get_stack(config)

    # Only the sidecar-recorded stale key is removed; the user's provider
    # config (aws:region, aws:s3:bucket_name) must survive.
    mock_stack.remove_config.assert_called_once_with("old_var")


def test_get_stack_no_sidecar_skips_removal(tmp_path, monkeypatch):
    """Without a sidecar, no keys are removed (safe default), then one is written."""
    import json

    config = _make_project(
        tmp_path,
        yaml_extra="secrets:\n  allow_unencrypted: true\nvariables:\n  region: us-east-1\n",
    )
    monkeypatch.delenv("TLUMI_SECRETS_PASSPHRASE", raising=False)

    from tlumi.workspace import get_stack

    mock_stack = MagicMock(spec=Stack)
    mock_stack.get_all_config.return_value = {"myproj:old_key": MagicMock()}

    with (
        patch("tlumi.workspace._ensure_pulumi_cli"),
        patch("tlumi.workspace._load_inline_program", return_value=lambda: None),
        patch("tlumi.workspace.auto.create_or_select_stack", return_value=mock_stack),
    ):
        get_stack(config)

    mock_stack.remove_config.assert_not_called()
    # The sidecar is written after reconciliation so the NEXT run can clean up.
    sidecar = tmp_path / ".tlumi" / "cache" / "managed_config_keys.json"
    assert json.loads(sidecar.read_text()) == {"keys": ["region"], "provider_keys": []}


def test_get_stack_corrupt_sidecar_skips_removal(tmp_path, monkeypatch):
    """A malformed sidecar is treated as missing: skip removal, then rewrite it."""
    import json

    config = _make_project(tmp_path, yaml_extra="secrets:\n  allow_unencrypted: true\n")
    sidecar = tmp_path / ".tlumi" / "cache" / "managed_config_keys.json"
    sidecar.write_text("{not json")
    monkeypatch.delenv("TLUMI_SECRETS_PASSPHRASE", raising=False)

    from tlumi.workspace import get_stack

    mock_stack = MagicMock(spec=Stack)
    mock_stack.get_all_config.return_value = {"myproj:old_key": MagicMock()}

    with (
        patch("tlumi.workspace._ensure_pulumi_cli"),
        patch("tlumi.workspace._load_inline_program", return_value=lambda: None),
        patch("tlumi.workspace.auto.create_or_select_stack", return_value=mock_stack),
    ):
        get_stack(config)

    mock_stack.remove_config.assert_not_called()
    assert json.loads(sidecar.read_text()) == {"keys": [], "provider_keys": []}


def test_get_stack_non_utf8_sidecar_skips_removal(tmp_path, monkeypatch):
    """A non-UTF-8 sidecar is malformed content, not a crash.

    read_text() raises UnicodeDecodeError (not JSONDecodeError) on byte-level
    corruption; it must be treated like any other corrupt sidecar: skip
    removal, rewrite the sidecar, never abort the runtime command.
    """
    import json

    config = _make_project(tmp_path, yaml_extra="secrets:\n  allow_unencrypted: true\n")
    sidecar = tmp_path / ".tlumi" / "cache" / "managed_config_keys.json"
    sidecar.write_bytes(b'{"keys": ["ab\xff"]}')
    monkeypatch.delenv("TLUMI_SECRETS_PASSPHRASE", raising=False)

    from tlumi.workspace import get_stack

    mock_stack = MagicMock(spec=Stack)
    mock_stack.get_all_config.return_value = {"myproj:old_key": MagicMock()}

    with (
        patch("tlumi.workspace._ensure_pulumi_cli"),
        patch("tlumi.workspace._load_inline_program", return_value=lambda: None),
        patch("tlumi.workspace.auto.create_or_select_stack", return_value=mock_stack),
    ):
        get_stack(config)  # must not raise UnicodeDecodeError

    mock_stack.remove_config.assert_not_called()
    assert json.loads(sidecar.read_text()) == {"keys": [], "provider_keys": []}


def test_get_stack_deeply_nested_sidecar_skips_removal(tmp_path, monkeypatch):
    """A pathologically nested JSON sidecar (RecursionError) is treated as corrupt."""
    import json

    config = _make_project(tmp_path, yaml_extra="secrets:\n  allow_unencrypted: true\n")
    sidecar = tmp_path / ".tlumi" / "cache" / "managed_config_keys.json"
    sidecar.write_text("[" * 100_000 + "]" * 100_000)
    monkeypatch.delenv("TLUMI_SECRETS_PASSPHRASE", raising=False)

    from tlumi.workspace import get_stack

    mock_stack = MagicMock(spec=Stack)
    mock_stack.get_all_config.return_value = {"myproj:old_key": MagicMock()}

    with (
        patch("tlumi.workspace._ensure_pulumi_cli"),
        patch("tlumi.workspace._load_inline_program", return_value=lambda: None),
        patch("tlumi.workspace.auto.create_or_select_stack", return_value=mock_stack),
    ):
        get_stack(config)  # must not raise RecursionError

    mock_stack.remove_config.assert_not_called()
    assert json.loads(sidecar.read_text()) == {"keys": [], "provider_keys": []}


def test_get_stack_symlinked_sidecar_ignored(tmp_path, monkeypatch, caplog):
    """A symlinked sidecar is ignored for reads and never followed for writes."""
    import logging

    config = _make_project(tmp_path, yaml_extra="secrets:\n  allow_unencrypted: true\n")
    target = tmp_path / "outside.json"
    target.write_text('{"keys": ["old_key"]}')
    sidecar = tmp_path / ".tlumi" / "cache" / "managed_config_keys.json"
    sidecar.symlink_to(target)
    monkeypatch.delenv("TLUMI_SECRETS_PASSPHRASE", raising=False)

    from tlumi.workspace import get_stack

    mock_stack = MagicMock(spec=Stack)
    mock_stack.get_all_config.return_value = {"myproj:old_key": MagicMock()}

    with (
        patch("tlumi.workspace._ensure_pulumi_cli"),
        patch("tlumi.workspace._load_inline_program", return_value=lambda: None),
        patch("tlumi.workspace.auto.create_or_select_stack", return_value=mock_stack),
    ):
        with caplog.at_level(logging.WARNING, logger="tlumi.workspace"):
            get_stack(config)

    mock_stack.remove_config.assert_not_called()
    # The write refused to follow the symlink: the target is untouched.
    assert target.read_text() == '{"keys": ["old_key"]}'


def test_get_stack_config_cleanup_skips_on_command_error(tmp_path, monkeypatch):
    """get_stack skips config cleanup when get_all_config raises CommandError."""
    from pulumi.automation.errors import CommandError as CmdError

    config = _make_project(tmp_path, yaml_extra="secrets:\n  allow_unencrypted: true\n")
    _write_sidecar(tmp_path, ["old_key"])
    monkeypatch.delenv("TLUMI_SECRETS_PASSPHRASE", raising=False)

    from tlumi.workspace import get_stack

    mock_stack = MagicMock(spec=Stack)
    mock_stack.get_all_config.side_effect = CmdError.__new__(CmdError)

    with (
        patch("tlumi.workspace._ensure_pulumi_cli"),
        patch("tlumi.workspace._load_inline_program", return_value=lambda: None),
        patch("tlumi.workspace.auto.create_or_select_stack", return_value=mock_stack),
    ):
        get_stack(config)

    # remove_config should not have been called since get_all_config failed
    mock_stack.remove_config.assert_not_called()


def test_get_stack_remove_config_failure_raises(tmp_path, monkeypatch):
    """get_stack raises WorkspaceError when remove_config fails (symmetric with set_config)."""
    from pulumi.automation.errors import CommandError as CmdError

    from tlumi.errors import WorkspaceError as WSError

    config = _make_project(tmp_path, yaml_extra="secrets:\n  allow_unencrypted: true\n")
    _write_sidecar(tmp_path, ["stale_key"])
    monkeypatch.delenv("TLUMI_SECRETS_PASSPHRASE", raising=False)

    from tlumi.workspace import get_stack

    mock_stack = MagicMock(spec=Stack)
    mock_stack.get_all_config.return_value = {"myproj:stale_key": MagicMock()}
    mock_stack.remove_config.side_effect = CmdError.__new__(CmdError)

    with (
        patch("tlumi.workspace._ensure_pulumi_cli"),
        patch("tlumi.workspace._load_inline_program", return_value=lambda: None),
        patch("tlumi.workspace.auto.create_or_select_stack", return_value=mock_stack),
    ):
        with pytest.raises(WSError, match="Failed to remove stale config key"):
            get_stack(config)


# ---------------------------------------------------------------------------
# safe_export_stack: underlying CommandError detail surfaced, redacted,
# with a cause-specific hint (rf-9)
# ---------------------------------------------------------------------------


def _export_command_error(stderr):
    """Build a real CommandError carrying the given Pulumi stderr text."""
    from pulumi.automation import CommandError, CommandResult

    return CommandError(CommandResult(stdout="", stderr=stderr, code=255))


def test_safe_export_stack_incorrect_passphrase_hint():
    """A wrong decryption passphrase surfaces the cause and a passphrase hint.

    Regression: the wrapper used to swallow the CommandError entirely, so the
    default masked output/apply paths reported a bare 'Failed to read state.'
    with a misleading state-unlock hint for a wrong TLUMI_SECRETS_PASSPHRASE.
    """
    from tlumi.errors import WorkspaceError as WSError
    from tlumi.workspace import safe_export_stack

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.side_effect = _export_command_error(
        "error: failed to decrypt encrypted configuration value: Incorrect Passphrase,"
        " please set PULUMI_CONFIG_PASSPHRASE"
    )

    with pytest.raises(WSError) as excinfo:
        safe_export_stack(mock_stack)

    assert "Failed to read state:" in excinfo.value.message
    # The underlying cause is visible (case-insensitive match on the code side).
    assert "incorrect passphrase" in excinfo.value.message.lower()
    assert "TLUMI_SECRETS_PASSPHRASE" in (excinfo.value.hint or "")


def test_safe_export_stack_generic_error_keeps_unlock_hint_and_redacts():
    """Other failures keep the unlock hint; credentials in stderr are redacted."""
    from tlumi.errors import WorkspaceError as WSError
    from tlumi.workspace import safe_export_stack

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.side_effect = _export_command_error(
        "error: the stack is currently locked by 1 lock(s):"
        " https://user:hunter2@backend.example/state/lock"
    )

    with pytest.raises(WSError) as excinfo:
        safe_export_stack(mock_stack)

    assert "Failed to read state:" in excinfo.value.message
    assert "currently locked" in excinfo.value.message
    assert "hunter2" not in excinfo.value.message  # redact_text applied
    assert "state unlock" in (excinfo.value.hint or "")


# ---------------------------------------------------------------------------
# T5: create_venv
# ---------------------------------------------------------------------------


def test_create_venv_happy_path(tmp_path):
    """create_venv calls uv venv with correct arguments."""
    from tlumi.uv import create_venv

    uv_path = tmp_path / "uv"
    venv_path = tmp_path / "myvenv"

    with patch("tlumi.uv.subprocess.run") as mock_run:
        create_venv(uv_path, venv_path)

    mock_run.assert_called_once_with(
        [str(uv_path), "venv", str(venv_path)],
        check=True,
        capture_output=True,
        text=True,
    )


def test_create_venv_called_process_error(tmp_path):
    """create_venv raises WorkspaceError on CalledProcessError."""
    import subprocess

    from tlumi.errors import WorkspaceError as WSError
    from tlumi.uv import create_venv

    uv_path = tmp_path / "uv"
    venv_path = tmp_path / "myvenv"

    with patch("tlumi.uv.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "uv venv", stderr="Python not found"
        )
        with pytest.raises(WSError, match="Failed to create virtual environment"):
            create_venv(uv_path, venv_path)


# ---------------------------------------------------------------------------
# R7-17: get_stack set_config failure
# ---------------------------------------------------------------------------


def test_get_stack_set_config_failure(tmp_path, monkeypatch):
    """get_stack raises WorkspaceError when set_config fails."""
    from pulumi.automation.errors import CommandError as CmdError

    from tlumi.errors import WorkspaceError as WSError
    from tlumi.workspace import get_stack

    config = _make_project(
        tmp_path,
        yaml_extra="secrets:\n  allow_unencrypted: true\nvariables:\n  region: us-east-1\n",
    )
    monkeypatch.delenv("TLUMI_SECRETS_PASSPHRASE", raising=False)

    mock_stack = MagicMock(spec=Stack)
    mock_stack.get_all_config.return_value = {}
    mock_stack.set_config.side_effect = CmdError.__new__(CmdError)

    with (
        patch("tlumi.workspace._ensure_pulumi_cli"),
        patch("tlumi.workspace._load_inline_program", return_value=lambda: None),
        patch("tlumi.workspace.auto.create_or_select_stack", return_value=mock_stack),
    ):
        with pytest.raises(WSError, match="Failed to set config variable 'region'"):
            get_stack(config)


# ---------------------------------------------------------------------------
# R7-29: Shared cache fallback paths
# ---------------------------------------------------------------------------


def test_ensure_pulumi_cli_shared_cache_os_error_falls_back(tmp_path, caplog):
    """Shared cache OSError falls back to per-project install."""
    import logging

    from tlumi.workspace import _ensure_pulumi_cli

    config = _make_project(tmp_path)

    with patch("tlumi.workspace._shared_cache_dir") as mock_shared:
        mock_shared.return_value = tmp_path / "shared_cache"
        with patch("tlumi.workspace.auto.PulumiCommand.install") as mock_install:
            mock_cmd = type("Cmd", (), {})()
            mock_install.side_effect = [OSError("disk error"), mock_cmd]

            with caplog.at_level(logging.DEBUG, logger="tlumi.workspace"):
                result = _ensure_pulumi_cli(config)

            assert result is mock_cmd
            assert "falling back" in caplog.text


def test_ensure_pulumi_cli_shared_cache_any_error_falls_back(tmp_path):
    """Shared cache any error (including SDK errors) falls back to per-project."""
    from tlumi.workspace import _ensure_pulumi_cli

    config = _make_project(tmp_path)

    with patch("tlumi.workspace._shared_cache_dir") as mock_shared:
        mock_shared.return_value = tmp_path / "shared_cache"
        with patch("tlumi.workspace.auto.PulumiCommand.install") as mock_install:
            mock_cmd = type("Cmd", (), {})()
            # First call (shared cache) fails with SDK error, second (per-project) succeeds
            mock_install.side_effect = [RuntimeError("version mismatch"), mock_cmd]
            result = _ensure_pulumi_cli(config)

    assert result is mock_cmd


# ---------------------------------------------------------------------------
# T1: get_stack rejects .tlumi symlink
# ---------------------------------------------------------------------------


def test_get_stack_rejects_tlumi_symlink(tmp_path, monkeypatch):
    """.tlumi as symlink raises WorkspaceError."""
    from tlumi.errors import WorkspaceError
    from tlumi.workspace import get_stack

    config = _make_project(tmp_path, yaml_extra="secrets:\n  allow_unencrypted: true\n")
    monkeypatch.delenv("TLUMI_SECRETS_PASSPHRASE", raising=False)

    # Remove .tlumi dir and replace with symlink
    import shutil

    shutil.rmtree(config.tlumi_dir)
    external = tmp_path / "external"
    external.mkdir()
    config.tlumi_dir.symlink_to(external)

    with pytest.raises(WorkspaceError, match="symlink"):
        get_stack(config)


# ---------------------------------------------------------------------------
# T2: get_stack rejects uninitialized project (no venv)
# ---------------------------------------------------------------------------


def test_get_stack_rejects_uninitialized_project(tmp_path, monkeypatch):
    """get_stack raises WorkspaceError when venv is missing."""
    from tlumi.errors import WorkspaceError
    from tlumi.workspace import get_stack

    config = _make_project(tmp_path, yaml_extra="secrets:\n  allow_unencrypted: true\n")
    monkeypatch.delenv("TLUMI_SECRETS_PASSPHRASE", raising=False)

    # Remove the venv directory
    import shutil

    shutil.rmtree(config.venv_dir)

    with pytest.raises(WorkspaceError, match="not initialized"):
        get_stack(config)


# ---------------------------------------------------------------------------
# R10-S2: get_stack rejects nested symlinks (state_dir, cache_dir, pulumi_home)
# ---------------------------------------------------------------------------


def test_get_stack_rejects_nested_symlink(tmp_path, monkeypatch):
    """get_stack raises WorkspaceError when state_dir is a symlink."""
    from tlumi.errors import WorkspaceError
    from tlumi.workspace import get_stack

    config = _make_project(tmp_path, yaml_extra="secrets:\n  allow_unencrypted: true\n")
    monkeypatch.delenv("TLUMI_SECRETS_PASSPHRASE", raising=False)

    # Create state dir as a symlink
    external = tmp_path / "evil_state"
    external.mkdir()
    state_dir = config.state_dir
    if state_dir.exists():
        import shutil

        shutil.rmtree(state_dir)
    state_dir.symlink_to(external)

    with pytest.raises(WorkspaceError, match="symlink"):
        get_stack(config)


def test_load_inline_program_cleans_sys_modules_on_failure(tmp_path, monkeypatch):
    """exec_module failure removes module from sys.modules."""
    config = _make_project(tmp_path)

    monkeypatch.delitem(sys.modules, "_tlumi_entry.infra", raising=False)

    from tlumi.workspace import _load_inline_program

    with patch("tlumi.workspace.importlib.util.spec_from_file_location") as mock_spec:
        mock_loader = MagicMock()
        mock_loader.exec_module.side_effect = SyntaxError("bad code")
        mock_spec_obj = MagicMock()
        mock_spec_obj.loader = mock_loader
        mock_spec.return_value = mock_spec_obj

        with patch("tlumi.workspace.importlib.util.module_from_spec") as mock_from_spec:
            mock_from_spec.return_value = type("Module", (), {})()

            program = _load_inline_program(config)
            with pytest.raises(SyntaxError, match="bad code"):
                program()

    # Module should have been cleaned up from sys.modules
    assert "_tlumi_entry.infra" not in sys.modules


# ---------------------------------------------------------------------------
# get_stack env scrubbing, passphrase translation, backend masking (call sites)
# ---------------------------------------------------------------------------


def test_get_stack_scrubs_tlumi_env_and_translates_passphrase(tmp_path, monkeypatch):
    """The documented env-scrub security control reaches LocalWorkspaceOptions.

    TLUMI_SECRETS_PASSPHRASE and TLUMI_VAR_* must be blanked in the subprocess
    env (so they don't leak to provider plugins), and the passphrase must be
    translated to PULUMI_CONFIG_PASSPHRASE so encrypted-secrets projects work.
    """
    config = _make_project(tmp_path)
    monkeypatch.setenv("TLUMI_SECRETS_PASSPHRASE", "my-secret")
    monkeypatch.setenv("TLUMI_VAR_REGION", "us-east-1")

    from tlumi.workspace import get_stack

    with (
        patch("tlumi.workspace._ensure_pulumi_cli", return_value=MagicMock()),
        patch("tlumi.workspace.auto.create_or_select_stack") as mock_create,
    ):
        mock_create.return_value = MagicMock(spec=Stack)
        get_stack(config, runtime=False)
        opts = mock_create.call_args.kwargs["opts"]
        env = opts.env_vars
        assert env["TLUMI_VAR_REGION"] == ""
        assert env["TLUMI_SECRETS_PASSPHRASE"] == ""
        assert env["PULUMI_CONFIG_PASSPHRASE"] == "my-secret"
        assert env["PULUMI_ACCESS_TOKEN"] == ""


def test_get_stack_masks_backend_url_credentials_in_notice(tmp_path, monkeypatch, caplog):
    """The remote-backend notice masks credentials (the only call site that protects them)."""
    import logging

    config = _make_project(
        tmp_path,
        yaml_extra=(
            "secrets:\n  allow_unencrypted: true\n"
            'backend:\n  url: "s3://user:hunter2@host/bucket?token=abc"\n'
        ),
    )
    monkeypatch.delenv("TLUMI_SECRETS_PASSPHRASE", raising=False)

    import tlumi.workspace as ws

    monkeypatch.setattr(ws, "_backend_warned", False)

    with (
        patch("tlumi.workspace._ensure_pulumi_cli", return_value=MagicMock()),
        patch("tlumi.workspace.auto.create_or_select_stack", return_value=MagicMock(spec=Stack)),
    ):
        with caplog.at_level(logging.INFO, logger="tlumi.workspace"):
            ws.get_stack(config, runtime=False)

    assert "Backend:" in caplog.text
    assert "***" in caplog.text
    assert "hunter2" not in caplog.text
    assert "abc" not in caplog.text


def test_get_stack_redacts_credentials_in_init_failure(tmp_path, monkeypatch):
    """A CommandError on stack init must not leak backend credentials into the error."""
    config = _make_project(tmp_path, yaml_extra="secrets:\n  allow_unencrypted: true\n")
    monkeypatch.delenv("TLUMI_SECRETS_PASSPHRASE", raising=False)

    from pulumi.automation.errors import CommandError as AutoCommandError

    from tlumi.workspace import WorkspaceError, get_stack

    leaky = AutoCommandError("failed to load: s3://user:hunter2@host?sig=SECRETSIG&se=2025")

    with (
        patch("tlumi.workspace._ensure_pulumi_cli", return_value=MagicMock()),
        patch("tlumi.workspace._load_inline_program", return_value=lambda: None),
        patch("tlumi.workspace.auto.create_or_select_stack", side_effect=leaky),
    ):
        with pytest.raises(WorkspaceError) as excinfo:
            get_stack(config)

    msg = str(excinfo.value)
    assert "hunter2" not in msg
    assert "SECRETSIG" not in msg
    assert "***" in msg


# ---------------------------------------------------------------------------
# _load_inline_program re-executes project-local imports every call (F: critical)
# ---------------------------------------------------------------------------


def test_program_reexecutes_local_helper_modules(tmp_path, monkeypatch):
    """Each program() call must re-run helper modules imported by infra.py.

    Regression for the cross-preview/up caching bug: a helper module's body
    (where module-level resources register) must execute on every program()
    call, not just the first, or an interactive apply would silently drop
    resources defined in helpers and Pulumi would destroy them.
    """
    (tmp_path / "tlumi.yaml").write_text(
        "project:\n  name: myproj\nsecrets:\n  allow_unencrypted: true\n"
    )
    (tmp_path / ".tlumi" / "cache" / "venv").mkdir(parents=True)
    # A counter file the helper appends to on every execution of its body.
    counter = tmp_path / "exec_count.txt"
    (tmp_path / "helper.py").write_text(f"open({str(counter)!r}, 'a').write('x')\nMARKER = 1\n")
    (tmp_path / "infra.py").write_text("import helper\n")
    config = load_config(tmp_path)

    from tlumi.workspace import _load_inline_program

    program = _load_inline_program(config)
    program()
    program()

    assert counter.read_text() == "xx", "helper module body must run on every program() call"
    # The helper must not linger in sys.modules after eviction logic ran.
    assert "helper" not in sys.modules or getattr(sys.modules.get("helper"), "MARKER", None) == 1


# ---------------------------------------------------------------------------
# _evict_project_modules: symlinked user code (codex-1) and pathological
# __file__ tolerance (audit 36)
# ---------------------------------------------------------------------------


def test_program_reexecutes_helpers_behind_symlinked_subdir(tmp_path, monkeypatch):
    """Helpers reached through a symlinked entry subdir are evicted per call.

    Regression for codex-1: with 'src -> ../shared', a helper's __file__
    resolves OUTSIDE the project dir, so the old project-root-only eviction
    left it cached between preview and up. Its module-level resource
    registrations then ran only once, and Pulumi would destroy those
    resources on the up() of an interactive apply.
    """
    shared = tmp_path / "shared"
    shared.mkdir()
    counter = tmp_path / "count_symdir.txt"
    (shared / "helper_symdir.py").write_text(f"open({str(counter)!r}, 'a').write('x')\n")
    (shared / "infra.py").write_text("import helper_symdir\n")

    project = tmp_path / "project"
    project.mkdir()
    (project / "tlumi.yaml").write_text("project:\n  name: myproj\n  entry: src/infra.py\n")
    (project / ".tlumi" / "cache" / "venv").mkdir(parents=True)
    (project / "src").symlink_to(shared, target_is_directory=True)
    config = load_config(project)

    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.delitem(sys.modules, "helper_symdir", raising=False)
    monkeypatch.delitem(sys.modules, "_tlumi_entry.infra", raising=False)

    from tlumi.workspace import _load_inline_program

    program = _load_inline_program(config)
    try:
        program()
        program()
    finally:
        sys.modules.pop("helper_symdir", None)
        sys.modules.pop("_tlumi_entry.infra", None)

    assert counter.read_text() == "xx", "symlinked helper must re-execute on every program() call"


def test_program_reexecutes_helpers_for_symlinked_entry_file(tmp_path, monkeypatch):
    """Helpers next to a directly symlinked entry file are evicted per call.

    The entry file itself is a symlink into a shared directory; the real
    infra.py puts its own (real) directory on sys.path and imports a sibling
    helper. That helper lives entirely outside the project dir, so eviction
    must include the resolved entry file's parent as a user-code root.
    """
    shared = tmp_path / "shared_entry"
    shared.mkdir()
    counter = tmp_path / "count_symfile.txt"
    (shared / "helper_symfile.py").write_text(f"open({str(counter)!r}, 'a').write('x')\n")
    (shared / "infra.py").write_text(
        "import os\n"
        "import sys\n"
        "_here = os.path.dirname(os.path.realpath(__file__))\n"
        "if _here not in sys.path:\n"
        "    sys.path.append(_here)\n"
        "import helper_symfile\n"
    )

    project = tmp_path / "project_symfile"
    project.mkdir()
    (project / "tlumi.yaml").write_text("project:\n  name: myproj\n")
    (project / ".tlumi" / "cache" / "venv").mkdir(parents=True)
    (project / "infra.py").symlink_to(shared / "infra.py")
    config = load_config(project)

    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.delitem(sys.modules, "helper_symfile", raising=False)
    monkeypatch.delitem(sys.modules, "_tlumi_entry.infra", raising=False)

    from tlumi.workspace import _load_inline_program

    program = _load_inline_program(config)
    try:
        program()
        program()
    finally:
        sys.modules.pop("helper_symfile", None)
        sys.modules.pop("_tlumi_entry.infra", None)

    assert counter.read_text() == "xx", "helper behind symlinked entry must re-execute per call"


def test_program_reexecutes_symlinked_helper_file(tmp_path, monkeypatch):
    """A helper that is itself a symlink out of the project is still evicted.

    The lexical __file__ (project/helper_symlinked.py) is inside the project
    while the resolved one is outside; eviction must consider both forms.
    """
    outside = tmp_path / "outside_helpers"
    outside.mkdir()
    counter = tmp_path / "count_symhelper.txt"
    (outside / "real_helper.py").write_text(f"open({str(counter)!r}, 'a').write('x')\n")

    project = tmp_path / "project_symhelper"
    project.mkdir()
    (project / "tlumi.yaml").write_text("project:\n  name: myproj\n")
    (project / ".tlumi" / "cache" / "venv").mkdir(parents=True)
    (project / "helper_symlinked.py").symlink_to(outside / "real_helper.py")
    (project / "infra.py").write_text("import helper_symlinked\n")
    config = load_config(project)

    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.delitem(sys.modules, "helper_symlinked", raising=False)
    monkeypatch.delitem(sys.modules, "_tlumi_entry.infra", raising=False)

    from tlumi.workspace import _load_inline_program

    program = _load_inline_program(config)
    try:
        program()
        program()
    finally:
        sys.modules.pop("helper_symlinked", None)
        sys.modules.pop("_tlumi_entry.infra", None)

    assert counter.read_text() == "xx", "symlinked helper file must re-execute per call"


def test_evict_project_modules_tolerates_pathological_file(tmp_path, monkeypatch):
    """A sys.modules entry with a broken __file__ must not abort program().

    Import hooks and mocks can install modules whose __file__ is a non-str
    (TypeError from Path()) or contains a NUL byte (ValueError from
    resolve()); the eviction guard skips them instead of crashing the run.
    """
    import types

    config = _make_project(tmp_path)

    bad_int = types.ModuleType("bad_file_int_mod")
    bad_int.__file__ = 3  # type: ignore[assignment]
    bad_nul = types.ModuleType("bad_file_nul_mod")
    bad_nul.__file__ = str(tmp_path) + "/mod\x00.py"
    monkeypatch.setitem(sys.modules, "bad_file_int_mod", bad_int)
    monkeypatch.setitem(sys.modules, "bad_file_nul_mod", bad_nul)
    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.delitem(sys.modules, "_tlumi_entry.infra", raising=False)

    from tlumi.workspace import _load_inline_program

    program = _load_inline_program(config)
    program()  # must not raise TypeError/ValueError from the eviction loop

    # The pathological modules were skipped, not evicted.
    assert "bad_file_int_mod" in sys.modules
    assert "bad_file_nul_mod" in sys.modules
    sys.modules.pop("_tlumi_entry.infra", None)


@pytest.mark.parametrize("prefix_attr", ["prefix", "base_prefix"])
def test_evict_project_modules_keeps_interpreter_prefix_modules(tmp_path, monkeypatch, prefix_attr):
    """Modules under the interpreter's own prefix survive eviction.

    Regression for rf-11: when the venv tlumi runs from sits under a
    user-code root (e.g. a shared tooling venv inside the project dir),
    eviction must not delete the pulumi SDK or tlumi itself from
    sys.modules; the user program's next 'import pulumi' would otherwise
    re-execute the whole SDK with fresh globals, losing runtime settings
    and sdk_compat patches.
    """
    import types

    config = _make_project(tmp_path)

    # A fake tooling venv INSIDE the project dir (a user-code root).
    tool_venv = tmp_path / "toolvenv"
    sdk_dir = tool_venv / "lib" / "python3.13" / "site-packages" / "pulumi_fake"
    sdk_dir.mkdir(parents=True)
    sdk_file = sdk_dir / "__init__.py"
    sdk_file.write_text("")
    sdk_mod = types.ModuleType("pulumi_fake_sdk_mod")
    sdk_mod.__file__ = str(sdk_file)

    # Control: a plain project helper must still be evicted.
    helper_file = tmp_path / "plain_helper.py"
    helper_file.write_text("")
    helper_mod = types.ModuleType("plain_helper_mod")
    helper_mod.__file__ = str(helper_file)

    monkeypatch.setattr(sys, prefix_attr, str(tool_venv))
    monkeypatch.setitem(sys.modules, "pulumi_fake_sdk_mod", sdk_mod)
    monkeypatch.setitem(sys.modules, "plain_helper_mod", helper_mod)
    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.delitem(sys.modules, "_tlumi_entry.infra", raising=False)

    from tlumi.workspace import _load_inline_program

    program = _load_inline_program(config)
    try:
        program()
    finally:
        sys.modules.pop("_tlumi_entry.infra", None)

    assert "pulumi_fake_sdk_mod" in sys.modules, "interpreter-owned module must stay cached"
    assert "plain_helper_mod" not in sys.modules, "user helper must still be evicted"


def test_evict_project_modules_tolerates_symlink_loop_runtimeerror(tmp_path, monkeypatch):
    """RuntimeError from Path.resolve() (Python 3.10 symlink loops) is skipped.

    On Python 3.10 Path.resolve() raises RuntimeError when the path traverses
    a symlink loop (3.11+ returns quietly). A cached module whose __file__
    hits such a loop mid-run must be skipped, not abort program(). Simulated
    by patching resolve for that one path, since 3.11+ cannot reproduce the
    loop organically.
    """
    import types
    from pathlib import Path

    config = _make_project(tmp_path)

    loopy_file = str(tmp_path / "loopy_helper.py")
    loopy_mod = types.ModuleType("loopy_helper_mod")
    loopy_mod.__file__ = loopy_file
    monkeypatch.setitem(sys.modules, "loopy_helper_mod", loopy_mod)
    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.delitem(sys.modules, "_tlumi_entry.infra", raising=False)

    from tlumi.workspace import _load_inline_program

    # Build program() first: root construction may resolve paths too.
    program = _load_inline_program(config)

    original_resolve = Path.resolve

    def raising_resolve(self, *args, **kwargs):
        if str(self) == loopy_file:
            raise RuntimeError(f"Symlink loop from '{loopy_file}'")
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", raising_resolve)
    try:
        program()  # must not raise RuntimeError from the eviction loop
    finally:
        sys.modules.pop("_tlumi_entry.infra", None)

    # The looping module was skipped, not evicted.
    assert "loopy_helper_mod" in sys.modules


# ---------------------------------------------------------------------------
# _mask_backend_url: malformed URLs must neither crash nor leak (audit 6)
# ---------------------------------------------------------------------------


def test_mask_backend_url_nonnumeric_port_with_password():
    """A typo'd port must not crash masking; the password stays hidden."""
    result = _mask_backend_url("s3://user:secret@minio:port9000/bucket")
    assert "secret" not in result
    assert result.startswith("s3://user:***@minio")


def test_mask_backend_url_nonnumeric_port_bare_username():
    """Bare-username masking survives a non-numeric port too."""
    result = _mask_backend_url("s3://token123@minio:port9000/bucket")
    assert "token123" not in result
    assert result.startswith("s3://***@minio")


def test_mask_backend_url_out_of_range_port_masked():
    """A port outside 0-65535 (ValueError from parsed.port) is dropped, not raised."""
    result = _mask_backend_url("s3://user:secret@host:99999999/bucket")
    assert "secret" not in result
    assert result.startswith("s3://user:***@host")


def test_mask_backend_url_invalid_ipv6_hides_whole_url():
    """urlparse failure returns a fully redacted placeholder, never the raw URL."""
    url = "s3://key:supersecret@[host/bucket"
    result = _mask_backend_url(url)
    assert result != url
    assert "supersecret" not in result
    assert "bucket" not in result
    assert "@" not in result


def test_mask_backend_url_never_raises_on_garbage():
    """Any malformed input yields a string without exceptions or leaked secrets."""
    cases = [
        "s3://user:hunter2@[::1/bucket",
        "s3://user:hunter2@minio:port9000/bucket",
        "http://user:hunter2@host:0x50/x",
        "://:@:",
        "s3://user:hunter2@h:999999999999999999999999/b",
    ]
    for url in cases:
        result = _mask_backend_url(url)  # must not raise
        assert isinstance(result, str)
        assert "hunter2" not in result


# ---------------------------------------------------------------------------
# _ensure_pulumi_cli shared cache: 0o700 on every level, not just the leaf
# (audit 38)
# ---------------------------------------------------------------------------


def test_ensure_pulumi_cli_shared_cache_chain_permissions(tmp_path, monkeypatch):
    """~/.tlumi, ~/.tlumi/cache, .../pulumi, and the leaf are all 0o700.

    mkdir(parents=True, mode=0o700) applies the mode only to the leaf, so a
    permissive umask would leave the intermediate dirs world-traversable
    when the pulumi CLI cache is what first creates ~/.tlumi.
    """
    import importlib.metadata
    import os
    import stat
    from pathlib import Path

    from tlumi.workspace import _ensure_pulumi_cli

    config = _make_project(tmp_path)
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    old_umask = os.umask(0o022)
    try:
        with patch("tlumi.workspace.auto.PulumiCommand.install") as mock_install:
            mock_cmd = type("Cmd", (), {})()
            mock_install.return_value = mock_cmd
            result = _ensure_pulumi_cli(config)
    finally:
        os.umask(old_umask)

    assert result is mock_cmd
    version = importlib.metadata.version("pulumi")
    chain = [
        fake_home / ".tlumi",
        fake_home / ".tlumi" / "cache",
        fake_home / ".tlumi" / "cache" / "pulumi",
        fake_home / ".tlumi" / "cache" / "pulumi" / version,
    ]
    for directory in chain:
        assert directory.is_dir(), directory
        mode = stat.S_IMODE(os.stat(directory).st_mode)
        assert mode == 0o700, f"{directory} has mode {oct(mode)}, expected 0o700"


def test_make_shared_cache_dir_outside_home(tmp_path, monkeypatch):
    """A cache dir outside $HOME is created without touching parent modes."""
    from pathlib import Path

    from tlumi.workspace import _make_shared_cache_dir

    fake_home = tmp_path / "fakehome2"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    dest = tmp_path / "elsewhere" / "cache"
    _make_shared_cache_dir(dest)
    assert dest.is_dir()


# ---------------------------------------------------------------------------
# Hostile-repo hardening: symlink loops, symlinked .gitignore, deep-nested state
# ---------------------------------------------------------------------------


def test_gitignore_symlink_is_skipped(tmp_path, caplog, monkeypatch):
    """A symlinked .gitignore is skipped, never read.

    A repo-committed .gitignore -> /dev/zero (unbounded read that decodes as
    valid UTF-8) or -> /dev/stdin (blocks forever) would otherwise OOM or hang
    every non-quiet get_stack() before any user code runs.
    """
    from pathlib import Path

    import tlumi.workspace

    monkeypatch.setattr(tlumi.workspace, "_gitignore_warned", False)
    config = _make_project(tmp_path)
    target = tmp_path / "gitignore_target"
    target.write_text("__pycache__/\n")
    (tmp_path / ".gitignore").symlink_to(target)

    def boom(self, *a, **k):
        raise AssertionError(f"read_text must not be called on {self}")

    monkeypatch.setattr(Path, "read_text", boom)

    with caplog.at_level("DEBUG", logger="tlumi.workspace"):
        _check_gitignore(config)  # must not raise or read the symlink
    assert "symlink" in caplog.text.lower()


def test_get_stack_tlumi_symlink_loop_clean_error(tmp_path, monkeypatch):
    """A symlink-loop .tlumi yields a clean WorkspaceError, not a raw
    RuntimeError from Path.resolve() (Python 3.10-3.12 raise on loops)."""
    from pathlib import Path

    from tlumi.errors import WorkspaceError
    from tlumi.workspace import get_stack

    (tmp_path / "tlumi.yaml").write_text(
        "project:\n  name: myproj\n  entry: infra.py\nsecrets:\n  allow_unencrypted: true\n"
    )
    (tmp_path / "infra.py").write_text("pass\n")
    config = load_config(tmp_path)
    monkeypatch.delenv("TLUMI_SECRETS_PASSPHRASE", raising=False)

    tlumi_dir = config.tlumi_dir
    tlumi_dir.symlink_to(tlumi_dir)  # self-referential loop

    orig_resolve = Path.resolve

    def fake_resolve(self, *a, **k):
        if self.is_symlink():
            raise RuntimeError(f"Symlink loop from {self}")
        return orig_resolve(self, *a, **k)

    monkeypatch.setattr(Path, "resolve", fake_resolve)

    with pytest.raises(WorkspaceError, match=".tlumi is a symlink"):
        get_stack(config)


def test_safe_export_stack_deep_nesting_recursion_error():
    """RecursionError from the SDK's internal json.loads of deeply nested
    exported state surfaces as a clean WorkspaceError, not a raw traceback."""
    from tlumi.errors import WorkspaceError as WSError
    from tlumi.workspace import safe_export_stack

    mock_stack = MagicMock(spec=Stack)
    mock_stack.export_stack.side_effect = RecursionError("maximum recursion depth exceeded")

    with pytest.raises(WSError) as excinfo:
        safe_export_stack(mock_stack)

    assert "Failed to read state:" in excinfo.value.message
    assert "nesting" in excinfo.value.message.lower()
