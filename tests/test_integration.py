"""Integration smoke test: init then validate a fresh project."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tlumi.commands.init import run_init
from tlumi.commands.validate import run_validate

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples"


def test_init_then_validate(tmp_path: Path, monkeypatch) -> None:
    """Create a fresh project with init, then validate it passes."""
    run_init(project_dir=tmp_path, name="testproject")

    tlumi_dir = tmp_path / ".tlumi"
    assert tlumi_dir.is_dir()
    assert (tlumi_dir / "state").is_dir()
    assert (tlumi_dir / "cache").is_dir()
    assert (tlumi_dir / "cache" / "pulumi_home").is_dir()
    assert (tlumi_dir / "cache" / "venv").is_dir()

    assert (tmp_path / "tlumi.yaml").is_file()
    assert (tmp_path / "infra.py").is_file()
    assert (tmp_path / "requirements.txt").is_file()

    monkeypatch.chdir(tmp_path)

    run_validate()


@pytest.mark.parametrize(
    "example_name",
    ["random", "aws-s3"],
)
def test_published_example_validates(example_name: str, tmp_path: Path, monkeypatch) -> None:
    """The canonical examples must validate cleanly so READMEs stay truthful.

    Copies the example into a tmp dir to avoid creating state in the source
    tree, then runs validate. validate only does syntax-level checks, so this
    catches regressions in the published infra.py without needing the cloud
    provider packages installed.
    """
    src = EXAMPLES_DIR / example_name
    if not src.is_dir():
        pytest.skip(f"example {example_name!r} not present in repo")

    dest = tmp_path / example_name
    shutil.copytree(src, dest)
    monkeypatch.chdir(dest)

    run_validate()
