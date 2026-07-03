"""Tests for tlumi.commands.init -- symlink checks, prompts, re-init behavior."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tlumi.commands.init import run_init
from tlumi.errors import ConfigError

MINIMAL_CONFIG = "project:\n  name: cloned-project\n  entry: infra.py\n"


@pytest.fixture()
def mock_uv():
    """Stub out uv binary discovery, venv creation, and package installs."""
    with (
        patch("tlumi.commands.init.ensure_uv", return_value=Path("/fake/uv")) as ensure,
        patch("tlumi.commands.init.uv_create_venv") as create_venv,
        patch("tlumi.commands.init.uv_install") as install,
    ):
        yield SimpleNamespace(ensure_uv=ensure, create_venv=create_venv, install=install)


@pytest.fixture()
def tty_stdin(monkeypatch):
    """Make init believe stdin is a TTY so the encryption prompt runs."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)


def _make_fully_initialized(project_dir: Path) -> Path:
    """Create tlumi.yaml plus a venv python marker; returns the .tlumi dir."""
    (project_dir / "tlumi.yaml").write_text(MINIMAL_CONFIG)
    venv_bin = project_dir / ".tlumi" / "cache" / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").touch()
    return project_dir / ".tlumi"


def test_init_rejects_tlumi_symlink(tmp_path: Path) -> None:
    """init raises ConfigError when .tlumi is a symlink."""
    target = tmp_path / "evil"
    target.mkdir()
    symlink = tmp_path / ".tlumi"
    symlink.symlink_to(target)

    with pytest.raises(ConfigError, match="symlink"):
        run_init(project_dir=tmp_path, name="testproject")


def test_init_file_write_oserror(tmp_path: Path) -> None:
    """init raises ConfigError when file writes fail with OSError."""
    with patch(
        "tlumi.commands.init.safe_write_text",
        side_effect=OSError("permission denied"),
    ):
        with pytest.raises(ConfigError, match="Failed to write project files"):
            run_init(project_dir=tmp_path, name="testproject")


def test_init_gitignore_symlink_skips_update(tmp_path: Path, capsys) -> None:
    """init skips .gitignore append when it is a symlink."""
    # Set up a minimal project so init renders templates
    target_file = tmp_path / "gitignore_target"
    target_file.write_text("# original content\n")

    gitignore = tmp_path / ".gitignore"
    gitignore.symlink_to(target_file)

    run_init(project_dir=tmp_path, name="testproject")

    # The symlink target should not have been modified with .tlumi/
    assert ".tlumi/" not in target_file.read_text()

    output = capsys.readouterr().out
    assert "symlink" in output


def test_init_rejects_tlumi_yaml_symlink(tmp_path: Path) -> None:
    """init raises ConfigError when tlumi.yaml is a dangling symlink."""
    # Dangling symlink: target doesn't exist, so config_exists=False and
    # init enters the new-project path where it would write through the symlink.
    symlink = tmp_path / "tlumi.yaml"
    symlink.symlink_to(tmp_path / "nonexistent")

    with pytest.raises(ConfigError, match="tlumi.yaml is a symlink"):
        run_init(project_dir=tmp_path, name="testproject")


def test_init_rejects_infra_py_symlink(tmp_path: Path) -> None:
    """init raises ConfigError when infra.py is a dangling symlink."""
    symlink = tmp_path / "infra.py"
    symlink.symlink_to(tmp_path / "nonexistent")

    with pytest.raises(ConfigError, match="infra.py is a symlink"):
        run_init(project_dir=tmp_path, name="testproject")


def test_init_rejects_requirements_txt_symlink(tmp_path: Path) -> None:
    """init raises ConfigError when requirements.txt is a dangling symlink."""
    symlink = tmp_path / "requirements.txt"
    symlink.symlink_to(tmp_path / "nonexistent")

    with pytest.raises(ConfigError, match="requirements.txt is a symlink"):
        run_init(project_dir=tmp_path, name="testproject")


def test_init_creates_tlumi_dir_with_0o700(tmp_path: Path) -> None:
    """Fresh init creates .tlumi/ with 0o700 permissions."""
    run_init(project_dir=tmp_path, name="testproject")

    tlumi_dir = tmp_path / ".tlumi"
    assert tlumi_dir.exists()
    assert oct(tlumi_dir.stat().st_mode & 0o777) == oct(0o700)


def test_init_fixes_insecure_tlumi_dir_permissions(tmp_path: Path) -> None:
    """Re-running init fixes .tlumi/ from 0o755 to 0o700."""
    tlumi_dir = tmp_path / ".tlumi"
    tlumi_dir.mkdir(mode=0o755)
    assert oct(tlumi_dir.stat().st_mode & 0o777) == oct(0o755)

    run_init(project_dir=tmp_path, name="testproject")

    assert oct(tlumi_dir.stat().st_mode & 0o777) == oct(0o700)


# ---------------------------------------------------------------------------
# Subdirectory symlink checks
# ---------------------------------------------------------------------------


def test_init_rejects_cache_subdir_symlink(tmp_path: Path) -> None:
    """init raises ConfigError when .tlumi/cache is a symlink."""
    tlumi_dir = tmp_path / ".tlumi"
    tlumi_dir.mkdir(mode=0o700)
    cache_dir = tlumi_dir / "cache"
    cache_dir.symlink_to(tmp_path / "evil_target")

    with pytest.raises(ConfigError, match=r"\.tlumi/cache is a symlink"):
        run_init(project_dir=tmp_path, name="testproject")


def test_init_rejects_state_subdir_symlink(tmp_path: Path) -> None:
    """init raises ConfigError when .tlumi/state is a symlink."""
    tlumi_dir = tmp_path / ".tlumi"
    tlumi_dir.mkdir(mode=0o700)
    state_dir = tlumi_dir / "state"
    state_dir.symlink_to(tmp_path / "evil_target")

    with pytest.raises(ConfigError, match=r"\.tlumi/state is a symlink"):
        run_init(project_dir=tmp_path, name="testproject")


# ---------------------------------------------------------------------------
# R9-S1: Dangling .gitignore symlink
# ---------------------------------------------------------------------------


def test_init_skips_dangling_gitignore_symlink(tmp_path: Path, capsys) -> None:
    """init skips .gitignore creation when it is a dangling symlink."""
    gitignore = tmp_path / ".gitignore"
    gitignore.symlink_to(tmp_path / "nonexistent_target")

    run_init(project_dir=tmp_path, name="testproject")

    output = capsys.readouterr().out
    assert ".gitignore is a symlink" in output
    # The symlink should still exist (not overwritten)
    assert gitignore.is_symlink()


# ---------------------------------------------------------------------------
# R9-E2: Unreadable .gitignore encoding
# ---------------------------------------------------------------------------


def test_init_skips_unreadable_gitignore(tmp_path: Path, capsys) -> None:
    """init warns and skips when .gitignore has unreadable encoding."""
    gitignore = tmp_path / ".gitignore"
    gitignore.write_bytes(b"\xff\xfe invalid utf-8 content")

    run_init(project_dir=tmp_path, name="testproject")

    output = capsys.readouterr().out
    assert "unreadable encoding" in output


# ---------------------------------------------------------------------------
# Interactive secret-encryption prompt (isatty branch)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("answer", ["n", "N", "no", "NO", " no "])
def test_init_interactive_opt_out_allows_unencrypted(
    tmp_path: Path, tty_stdin, mock_uv, capsys, answer: str
) -> None:
    """Answering n/no to the encryption prompt scaffolds allow_unencrypted: true."""
    with patch("tlumi.commands.init.prompt", return_value=answer):
        run_init(project_dir=tmp_path, name="testproject")

    content = (tmp_path / "tlumi.yaml").read_text()
    assert "allow_unencrypted: true" in content

    output = capsys.readouterr().out
    assert "stored unencrypted in state" in output


@pytest.mark.parametrize("answer", ["", "y", "Y", "yes", "anything-else"])
def test_init_interactive_default_keeps_secure(
    tmp_path: Path, tty_stdin, mock_uv, capsys, answer: str
) -> None:
    """Enter/Y (and anything but n/no) keeps the secure allow_unencrypted: false."""
    with patch("tlumi.commands.init.prompt", return_value=answer):
        run_init(project_dir=tmp_path, name="testproject")

    content = (tmp_path / "tlumi.yaml").read_text()
    assert "allow_unencrypted: false" in content

    output = capsys.readouterr().out
    assert "TLUMI_SECRETS_PASSPHRASE" in output


def test_init_interactive_eof_keeps_secure_default(tmp_path: Path, tty_stdin, mock_uv) -> None:
    """EOF at the prompt (Ctrl-D) falls back to the secure default via prompt()."""
    # Exercises the real display.prompt() EOF contract: returns the default.
    with patch("tlumi.display.console.input", side_effect=EOFError):
        run_init(project_dir=tmp_path, name="testproject")

    content = (tmp_path / "tlumi.yaml").read_text()
    assert "allow_unencrypted: false" in content


def test_init_non_tty_defaults_secure_with_passphrase_notice(
    tmp_path: Path, mock_uv, capsys, monkeypatch
) -> None:
    """Piped/scripted init keeps the secure default, skips the prompt, and still
    prints the TLUMI_SECRETS_PASSPHRASE reminder so the first plan isn't a surprise."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    with patch("tlumi.commands.init.prompt") as mock_prompt:
        run_init(project_dir=tmp_path, name="testproject")

    mock_prompt.assert_not_called()
    content = (tmp_path / "tlumi.yaml").read_text()
    assert "allow_unencrypted: false" in content

    output = capsys.readouterr().out
    assert "TLUMI_SECRETS_PASSPHRASE" in output


# ---------------------------------------------------------------------------
# Re-init on a fully initialized project
# ---------------------------------------------------------------------------


def test_reinit_fully_initialized_enforces_0o700(tmp_path: Path, mock_uv, capsys) -> None:
    """Re-init on a fully initialized project repairs lax .tlumi/ permissions,
    even when the checkout directory name is not a valid project name."""
    project_dir = tmp_path / "01-infra.prod"
    project_dir.mkdir()
    tlumi_dir = _make_fully_initialized(project_dir)
    tlumi_dir.chmod(0o755)

    run_init(project_dir=project_dir)

    assert oct(tlumi_dir.stat().st_mode & 0o777) == oct(0o700)
    assert "Project already initialized." in capsys.readouterr().out


def test_reinit_installs_requirements_and_preserves_config(tmp_path: Path, mock_uv, capsys) -> None:
    """Re-init re-runs the idempotent dependency install and leaves tlumi.yaml alone."""
    _make_fully_initialized(tmp_path)
    req = tmp_path / "requirements.txt"
    req.write_text("pulumi>=3.100.0\n")
    config_before = (tmp_path / "tlumi.yaml").read_text()

    run_init(project_dir=tmp_path)

    output = capsys.readouterr().out
    assert "Project already initialized." in output
    assert (tmp_path / "tlumi.yaml").read_text() == config_before

    mock_uv.install.assert_called_once()
    call = mock_uv.install.call_args
    assert call.args[1] == tmp_path / ".tlumi" / "cache" / "venv"
    assert call.args[2] == ["-r", str(req)]
    # No template rendering or venv creation on the re-init path
    mock_uv.create_venv.assert_not_called()


def test_reinit_rejects_state_subdir_symlink(tmp_path: Path) -> None:
    """Re-init still runs the .tlumi subdir symlink checks before returning."""
    _make_fully_initialized(tmp_path)
    state_dir = tmp_path / ".tlumi" / "state"
    state_dir.symlink_to(tmp_path / "evil_target")

    with pytest.raises(ConfigError, match=r"\.tlumi/state is a symlink"):
        run_init(project_dir=tmp_path)


# ---------------------------------------------------------------------------
# Project name derivation and validation scope
# ---------------------------------------------------------------------------


def test_init_existing_project_ignores_invalid_dir_name(
    tmp_path: Path, mock_uv, tty_stdin, capsys
) -> None:
    """Setting up a cloned project works even when the checkout directory name
    would be an invalid project name; the real name lives in tlumi.yaml."""
    project_dir = tmp_path / "01-infra.prod"
    project_dir.mkdir()
    (project_dir / "tlumi.yaml").write_text(MINIMAL_CONFIG)

    with patch("tlumi.commands.init.prompt") as mock_prompt:
        run_init(project_dir=project_dir)

    # No encryption prompt on the existing-project path (no templates rendered)
    mock_prompt.assert_not_called()
    # tlumi.yaml is preserved, not re-rendered
    assert (project_dir / "tlumi.yaml").read_text() == MINIMAL_CONFIG

    output = capsys.readouterr().out
    assert "Setting up existing tlumi project" in output
    assert "Project setup complete!" in output


def test_init_fresh_invalid_dir_name_rejected_before_writes(tmp_path: Path) -> None:
    """Fresh init still validates the derived name, and fails before creating .tlumi/."""
    project_dir = tmp_path / "01-bad.name"
    project_dir.mkdir()

    with pytest.raises(ConfigError, match="Invalid project name"):
        run_init(project_dir=project_dir)

    assert not (project_dir / ".tlumi").exists()
    assert not (project_dir / "tlumi.yaml").exists()


def test_init_rejects_invalid_explicit_name(tmp_path: Path) -> None:
    """An explicit --name is still validated on fresh init."""
    with pytest.raises(ConfigError, match="Invalid project name"):
        run_init(project_dir=tmp_path, name="1nvalid")


def _raise_runtimeerror_on_symlink_resolve(monkeypatch) -> None:
    """Make Path.resolve() raise on symlinks, mimicking Python 3.10-3.12 loops.

    On those versions Path.resolve() raises RuntimeError('Symlink loop ...')
    for a looping symlink even in non-strict mode; 3.13+ returns cleanly.
    """
    orig_resolve = Path.resolve

    def fake_resolve(self, *a, **k):
        if self.is_symlink():
            raise RuntimeError(f"Symlink loop from {self}")
        return orig_resolve(self, *a, **k)

    monkeypatch.setattr(Path, "resolve", fake_resolve)


def test_init_tlumi_symlink_loop_clean_error(tmp_path: Path, monkeypatch) -> None:
    """A symlink-loop .tlumi yields a clean ConfigError, not a raw RuntimeError."""
    symlink = tmp_path / ".tlumi"
    symlink.symlink_to(symlink)  # self-referential loop
    _raise_runtimeerror_on_symlink_resolve(monkeypatch)

    with pytest.raises(ConfigError, match=".tlumi is a symlink"):
        run_init(project_dir=tmp_path, name="testproject")


def test_init_tlumi_yaml_symlink_loop_clean_error(tmp_path: Path, monkeypatch) -> None:
    """A symlink-loop tlumi.yaml yields a clean ConfigError, not a RuntimeError."""
    symlink = tmp_path / "tlumi.yaml"
    symlink.symlink_to(symlink)  # self-referential loop
    _raise_runtimeerror_on_symlink_resolve(monkeypatch)

    with pytest.raises(ConfigError, match="tlumi.yaml is a symlink"):
        run_init(project_dir=tmp_path, name="testproject")
