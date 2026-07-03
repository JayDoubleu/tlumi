"""Tests for tlumi.commands.deps: _normalize_name, input validation."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tlumi.commands.deps import (
    _ensure_venv,
    _normalize_name,
    run_deps_add,
    run_deps_install,
    run_deps_list,
)
from tlumi.errors import TlumiError, WorkspaceError


def test_simple_package():
    assert _normalize_name("pulumi-aws") == "pulumi_aws"


def test_with_version_constraint():
    assert _normalize_name("pulumi-aws>=5.0.0") == "pulumi_aws"


def test_with_exact_version():
    assert _normalize_name("requests==2.31.0") == "requests"


def test_with_extras():
    assert _normalize_name("pulumi-azure-native[all]") == "pulumi_azure_native"


def test_with_markers():
    assert _normalize_name('typing-extensions>=4.0; python_version<"3.11"') == "typing_extensions"


def test_dots_normalized():
    assert _normalize_name("zope.interface") == "zope_interface"


def test_case_normalized():
    assert _normalize_name("PyYAML") == "pyyaml"


def test_complex_version():
    assert _normalize_name("pulumi-gcp>=7.0,<8.0") == "pulumi_gcp"


def test_url_spec_fallback():
    # URL-based specs are not valid Requirement, fallback parsing kicks in
    assert _normalize_name("git+https://github.com/foo/bar") == "git+https://github_com/foo/bar"


def test_normalize_name_rejects_empty_fallback():
    """_normalize_name raises WorkspaceError when both PEP 508 and regex fallback yield empty.

    A returned "" would let unrelated bogus specs collide as the same key in
    the existing_packages set and silently drop legitimate adds.
    """
    # A spec consisting only of operator chars triggers the empty-name fallback.
    with pytest.raises(TlumiError, match="Invalid package specifier"):
        _normalize_name("~=")


def test_plain_name():
    assert _normalize_name("requests") == "requests"


def test_hyphens_underscores_dots_all_normalized():
    assert _normalize_name("My-Package.Name") == "my_package_name"


# --- argument injection validation tests ---


def test_deps_add_rejects_flag_injection():
    """deps add rejects packages starting with '-' to prevent argument injection."""
    with pytest.raises(TlumiError, match="Invalid package specifier"):
        run_deps_add(["--index-url", "https://evil.com"])


def test_deps_add_rejects_dash_prefix():
    """deps add rejects -r flag to prevent file inclusion injection."""
    with pytest.raises(TlumiError, match="Invalid package specifier"):
        run_deps_add(["-r", "evil.txt"])


def test_deps_add_rejects_leading_space_dash():
    """A leading space must not smuggle a pip directive past the dash guard.

    uv/pip strip leading whitespace from a requirements line, so ' -r evil.txt'
    would be honored if the guard only checked startswith('-').
    """
    with pytest.raises(TlumiError, match="Invalid package specifier"):
        run_deps_add([" -r evil.txt"])
    with pytest.raises(TlumiError, match="Invalid package specifier"):
        run_deps_add(["\t--index-url https://evil/simple"])


def test_deps_add_rejects_newlines():
    """deps add rejects packages containing newline characters."""
    with pytest.raises(TlumiError, match="control characters"):
        run_deps_add(["pkg\n--index-url evil"])


def test_deps_add_rejects_carriage_return():
    """deps add rejects packages containing carriage return characters."""
    with pytest.raises(TlumiError, match="control characters"):
        run_deps_add(["pkg\r--index-url evil"])


def test_deps_add_rejects_unicode_line_separators():
    """deps add rejects line boundaries beyond \\n/\\r that pip's splitlines honours (F20)."""
    for bad in ("pkg\x0c--index-url=http://evil", "pkg\x1c-r/etc/passwd", "pkg evil"):
        with pytest.raises(TlumiError, match="control characters"):
            run_deps_add([bad])


def test_deps_add_rejects_nul_byte():
    """deps add rejects a NUL byte (not a splitlines boundary) explicitly (F20)."""
    with pytest.raises(TlumiError, match="control characters"):
        run_deps_add(["pkg\x00evil"])


# --- symlink validation tests ---


def test_deps_add_rejects_requirements_symlink(tmp_path):
    """deps add raises TlumiError when requirements.txt is a symlink."""
    # Create a minimal project structure
    config = MagicMock()
    config.project_dir = tmp_path
    config.venv_python = tmp_path / "venv" / "bin" / "python"
    config.venv_python.parent.mkdir(parents=True)
    config.venv_python.touch()
    config.venv_dir = tmp_path / "venv"

    # Create requirements.txt as a symlink
    evil_target = tmp_path / "evil_requirements"
    evil_target.write_text("pulumi\n")
    req_path = tmp_path / "requirements.txt"
    req_path.symlink_to(evil_target)

    with patch("tlumi.commands.deps.load_config", return_value=config):
        with patch("tlumi.commands.deps.find_project_dir", return_value=tmp_path):
            with pytest.raises(TlumiError, match="symlink"):
                run_deps_add(["new-package"])


# --- error path tests ---


def test_ensure_venv_raises_when_missing(tmp_path):
    """_ensure_venv raises WorkspaceError when venv/bin/python does not exist."""
    config = MagicMock()
    config.venv_python = tmp_path / "nonexistent" / "bin" / "python"
    with pytest.raises(WorkspaceError, match="Virtual environment not found"):
        _ensure_venv(config)


def test_deps_install_no_requirements(tmp_path):
    """run_deps_install raises WorkspaceError when requirements.txt is missing."""
    config = MagicMock()
    config.project_dir = tmp_path
    config.venv_python = tmp_path / "venv" / "bin" / "python"
    config.venv_python.parent.mkdir(parents=True)
    config.venv_python.touch()
    config.venv_dir = tmp_path / "venv"

    with patch("tlumi.commands.deps.load_config", return_value=config):
        with patch("tlumi.commands.deps.find_project_dir", return_value=tmp_path):
            with pytest.raises(WorkspaceError, match="No requirements.txt found"):
                run_deps_install()


def test_deps_list_calls_uv_list(tmp_path):
    """run_deps_list delegates to uv_list with the correct venv."""
    config = MagicMock()
    config.project_dir = tmp_path
    config.venv_python = tmp_path / "venv" / "bin" / "python"
    config.venv_python.parent.mkdir(parents=True)
    config.venv_python.touch()
    config.venv_dir = tmp_path / "venv"

    mock_result = MagicMock()
    mock_result.stdout = "Package  Version\n--------  -------\npulumi   3.0.0\n"

    with patch("tlumi.commands.deps.load_config", return_value=config):
        with patch("tlumi.commands.deps.find_project_dir", return_value=tmp_path):
            with patch("tlumi.commands.deps.ensure_uv", return_value="/usr/bin/uv"):
                with patch("tlumi.commands.deps.uv_list", return_value=mock_result) as mock_list:
                    run_deps_list()
                    mock_list.assert_called_once_with("/usr/bin/uv", config.venv_dir)


# ---------------------------------------------------------------------------
# R7-25: Duplicate package dedup
# ---------------------------------------------------------------------------


def test_deps_add_updates_existing_spec_in_place(tmp_path):
    """Re-adding a listed package with a new version spec updates the pin in place.

    The install below always runs with the new spec, so the manifest must record
    it too; otherwise the venv is upgraded while requirements.txt keeps the old
    pin and the next 'deps install' / fresh clone silently reverts.
    """
    config = MagicMock()
    config.project_dir = tmp_path
    config.venv_python = tmp_path / "venv" / "bin" / "python"
    config.venv_python.parent.mkdir(parents=True)
    config.venv_python.touch()
    config.venv_dir = tmp_path / "venv"

    # Pre-populate requirements.txt
    req_path = tmp_path / "requirements.txt"
    req_path.write_text("pulumi-aws>=5.0.0\n")

    with patch("tlumi.commands.deps.load_config", return_value=config):
        with patch("tlumi.commands.deps.find_project_dir", return_value=tmp_path):
            with patch("tlumi.commands.deps.ensure_uv", return_value="/usr/bin/uv"):
                with patch("tlumi.commands.deps.uv_install"):
                    run_deps_add(["pulumi-aws>=6.0.0"])

    # The single entry is updated to the new spec, not duplicated or left stale.
    lines = [ln for ln in req_path.read_text().splitlines() if ln.strip()]
    assert lines == ["pulumi-aws>=6.0.0"]


def test_deps_add_same_spec_is_noop(tmp_path):
    """Re-adding an identical spec does not duplicate the line."""
    config = MagicMock()
    config.project_dir = tmp_path
    config.venv_python = tmp_path / "venv" / "bin" / "python"
    config.venv_python.parent.mkdir(parents=True)
    config.venv_python.touch()
    config.venv_dir = tmp_path / "venv"

    req_path = tmp_path / "requirements.txt"
    req_path.write_text("pulumi-aws>=5.0.0\n")

    with patch("tlumi.commands.deps.load_config", return_value=config):
        with patch("tlumi.commands.deps.find_project_dir", return_value=tmp_path):
            with patch("tlumi.commands.deps.ensure_uv", return_value="/usr/bin/uv"):
                with patch("tlumi.commands.deps.uv_install"):
                    run_deps_add(["pulumi-aws>=5.0.0"])

    lines = [ln for ln in req_path.read_text().splitlines() if ln.strip()]
    assert lines == ["pulumi-aws>=5.0.0"]


def test_deps_add_bare_name_preserves_existing_pin(tmp_path):
    """Re-adding a BARE name for an already-pinned package must not strip the pin.

    `deps add pulumi-aws` (no constraint) against an existing
    `pulumi-aws==6.0.0  # locked for prod` line must be a no-op: overwriting it
    with the bare name would silently discard the user-authored version pin and
    inline comment (data loss), and the unpinned install would then upgrade the
    venv off the pin.
    """
    config = MagicMock()
    config.project_dir = tmp_path
    config.venv_python = tmp_path / "venv" / "bin" / "python"
    config.venv_python.parent.mkdir(parents=True)
    config.venv_python.touch()
    config.venv_dir = tmp_path / "venv"

    req_path = tmp_path / "requirements.txt"
    req_path.write_text("pulumi-aws==6.0.0  # locked for prod\n")

    with patch("tlumi.commands.deps.load_config", return_value=config):
        with patch("tlumi.commands.deps.find_project_dir", return_value=tmp_path):
            with patch("tlumi.commands.deps.ensure_uv", return_value="/usr/bin/uv"):
                with patch("tlumi.commands.deps.uv_install"):
                    run_deps_add(["pulumi-aws"])

    lines = [ln for ln in req_path.read_text().splitlines() if ln.strip()]
    assert lines == ["pulumi-aws==6.0.0  # locked for prod"]


def test_deps_add_preserves_last_line_without_trailing_newline(tmp_path):
    """A hand-edited requirements.txt lacking a trailing newline must not merge.

    Appending the new entry directly would produce 'pulumi-randompulumi-aws',
    destroying both the existing package and the new one.
    """
    config = MagicMock()
    config.project_dir = tmp_path
    config.venv_python = tmp_path / "venv" / "bin" / "python"
    config.venv_python.parent.mkdir(parents=True)
    config.venv_python.touch()
    config.venv_dir = tmp_path / "venv"

    req_path = tmp_path / "requirements.txt"
    req_path.write_bytes(b"pulumi-random")  # no trailing newline

    with patch("tlumi.commands.deps.load_config", return_value=config):
        with patch("tlumi.commands.deps.find_project_dir", return_value=tmp_path):
            with patch("tlumi.commands.deps.ensure_uv", return_value="/usr/bin/uv"):
                with patch("tlumi.commands.deps.uv_install"):
                    run_deps_add(["pulumi-aws"])

    lines = [ln for ln in req_path.read_text().splitlines() if ln.strip()]
    assert lines == ["pulumi-random", "pulumi-aws"]


def test_deps_add_dedup_with_normalization(tmp_path):
    """deps add normalizes hyphens/underscores when checking for duplicates."""
    config = MagicMock()
    config.project_dir = tmp_path
    config.venv_python = tmp_path / "venv" / "bin" / "python"
    config.venv_python.parent.mkdir(parents=True)
    config.venv_python.touch()
    config.venv_dir = tmp_path / "venv"

    # Pre-populate with underscored variant
    req_path = tmp_path / "requirements.txt"
    req_path.write_text("pulumi_aws\n")

    with patch("tlumi.commands.deps.load_config", return_value=config):
        with patch("tlumi.commands.deps.find_project_dir", return_value=tmp_path):
            with patch("tlumi.commands.deps.ensure_uv", return_value="/usr/bin/uv"):
                with patch("tlumi.commands.deps.uv_install"):
                    run_deps_add(["pulumi-aws"])

    # Hyphen variant should be detected as duplicate
    lines = [ln for ln in req_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1


# ---------------------------------------------------------------------------
# R7-26: deps add write failure
# ---------------------------------------------------------------------------


def test_deps_add_write_failure(tmp_path):
    """deps add raises WorkspaceError when writing requirements.txt fails."""
    config = MagicMock()
    config.project_dir = tmp_path
    config.venv_python = tmp_path / "venv" / "bin" / "python"
    config.venv_python.parent.mkdir(parents=True)
    config.venv_python.touch()
    config.venv_dir = tmp_path / "venv"

    with patch("tlumi.commands.deps.load_config", return_value=config):
        with patch("tlumi.commands.deps.find_project_dir", return_value=tmp_path):
            with patch(
                "tlumi.commands.deps.safe_append_text",
                side_effect=OSError("disk full"),
            ):
                with pytest.raises(WorkspaceError, match="Failed to write requirements.txt"):
                    run_deps_add(["new-package"])


# ---------------------------------------------------------------------------
# R7-27: deps add read error
# ---------------------------------------------------------------------------


def test_deps_add_read_requirements_error(tmp_path):
    """deps add raises WorkspaceError when reading requirements.txt fails."""
    config = MagicMock()
    config.project_dir = tmp_path
    config.venv_python = tmp_path / "venv" / "bin" / "python"
    config.venv_python.parent.mkdir(parents=True)
    config.venv_python.touch()
    config.venv_dir = tmp_path / "venv"

    # Create requirements.txt but make it unreadable
    req_path = tmp_path / "requirements.txt"
    req_path.write_text("pulumi\n")
    req_path.chmod(0o000)

    try:
        with patch("tlumi.commands.deps.load_config", return_value=config):
            with patch("tlumi.commands.deps.find_project_dir", return_value=tmp_path):
                with pytest.raises(WorkspaceError, match="Cannot read requirements.txt"):
                    run_deps_add(["new-package"])
    finally:
        req_path.chmod(0o644)


# ---------------------------------------------------------------------------
# R8-E1: UnicodeDecodeError on requirements.txt
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# R9-E1: _normalize_name with tilde-equals, less-than, at-url
# ---------------------------------------------------------------------------


def test_normalize_name_tilde_equals():
    """Tilde-equals version spec is parsed correctly."""
    assert _normalize_name("pulumi~=3.0") == "pulumi"


def test_normalize_name_less_than():
    """Less-than version spec is parsed correctly."""
    assert _normalize_name("pulumi<4.0") == "pulumi"


def test_normalize_name_at_url():
    """@ URL spec fallback splits on @ correctly."""
    # "package @ https://..." is valid PEP 508 but the URL part makes Requirement fail
    # for some package specs; the fallback regex should handle it
    result = _normalize_name("git+https://github.com/foo/bar")
    # The fallback regex splits on @ and other chars
    assert "git" in result


# ---------------------------------------------------------------------------
# R10-S5: deps add rejects symlinked requirements.txt before read
# ---------------------------------------------------------------------------


def test_deps_add_rejects_symlinked_requirements_before_read(tmp_path):
    """deps add rejects symlinked requirements.txt even when it has no content to read."""
    config = MagicMock()
    config.project_dir = tmp_path
    config.venv_python = tmp_path / "venv" / "bin" / "python"
    config.venv_python.parent.mkdir(parents=True)
    config.venv_python.touch()
    config.venv_dir = tmp_path / "venv"

    # Create requirements.txt as a symlink to a non-existent target
    req_path = tmp_path / "requirements.txt"
    req_path.symlink_to(tmp_path / "nonexistent")

    with patch("tlumi.commands.deps.load_config", return_value=config):
        with patch("tlumi.commands.deps.find_project_dir", return_value=tmp_path):
            with pytest.raises(TlumiError, match="symlink"):
                run_deps_add(["new-package"])


def test_deps_add_unicode_decode_error(tmp_path):
    """deps add raises WorkspaceError when requirements.txt has invalid encoding."""
    config = MagicMock()
    config.project_dir = tmp_path
    config.venv_python = tmp_path / "venv" / "bin" / "python"
    config.venv_python.parent.mkdir(parents=True)
    config.venv_python.touch()
    config.venv_dir = tmp_path / "venv"

    # Write binary content that is not valid UTF-8
    req_path = tmp_path / "requirements.txt"
    req_path.write_bytes(b"\xff\xfe invalid utf-8")

    with patch("tlumi.commands.deps.load_config", return_value=config):
        with patch("tlumi.commands.deps.find_project_dir", return_value=tmp_path):
            with pytest.raises(WorkspaceError, match="Cannot read requirements.txt"):
                run_deps_add(["new-package"])


def test_deps_add_requirements_symlink_loop_clean_error(tmp_path):
    """A symlink-loop requirements.txt yields a clean WorkspaceError, not a raw
    RuntimeError from Path.resolve() (Python 3.10-3.12 raise on loops)."""
    from pathlib import Path

    config = MagicMock()
    config.project_dir = tmp_path
    config.venv_python = tmp_path / "venv" / "bin" / "python"
    config.venv_python.parent.mkdir(parents=True)
    config.venv_python.touch()
    config.venv_dir = tmp_path / "venv"

    req_path = tmp_path / "requirements.txt"
    req_path.symlink_to(req_path)  # self-referential loop

    orig_resolve = Path.resolve

    def fake_resolve(self, *a, **k):
        if self.is_symlink():
            raise RuntimeError(f"Symlink loop from {self}")
        return orig_resolve(self, *a, **k)

    with patch("tlumi.commands.deps.load_config", return_value=config):
        with patch("tlumi.commands.deps.find_project_dir", return_value=tmp_path):
            with patch.object(Path, "resolve", fake_resolve):
                with pytest.raises(WorkspaceError, match="requirements.txt is a symlink"):
                    run_deps_add(["new-package"])
