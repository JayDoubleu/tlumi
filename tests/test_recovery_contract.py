"""Verifies the runtime=False contract: recovery commands must pass runtime=False to get_stack.

These tests pin the ADR-007 contract at the call-site level. A regression that
drops the flag in any recovery command (state list/show/pull/rm/mv/push, show,
cancel, output) would silently re-couple recovery to a healthy infra.py / venv.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


def _patched_recovery(target: str):
    """Patch get_stack at the given import location and return a kwargs capture list."""
    captured: list[dict] = []

    def _fake_get_stack(*args, **kwargs):
        captured.append(kwargs)
        # Raise to short-circuit further command logic in tests
        raise RuntimeError("intentional short-circuit")

    return captured, patch(target, side_effect=_fake_get_stack)


@pytest.fixture
def fake_config(tmp_path):
    """Provide a usable ProjectConfig-shaped mock for recovery commands."""
    from unittest.mock import MagicMock

    cfg = MagicMock()
    cfg.project_dir = tmp_path
    cfg.tlumi_dir = tmp_path / ".tlumi"
    cfg.tlumi_dir.mkdir()
    return cfg


def _assert_runtime_false(captured: list[dict]) -> None:
    assert captured, "get_stack was never called"
    assert captured[-1].get("runtime") is False, (
        "Recovery command must call get_stack with runtime=False"
    )


def test_state_list_passes_runtime_false(fake_config):
    captured, ctx = _patched_recovery("tlumi.commands.state.get_stack")
    with ctx:
        with patch("tlumi.commands.state.load_config", return_value=fake_config):
            with patch(
                "tlumi.commands.state.find_project_dir", return_value=fake_config.project_dir
            ):
                from tlumi.commands.state import run_state_list

                with pytest.raises(RuntimeError, match="short-circuit"):
                    run_state_list()
    _assert_runtime_false(captured)


def test_state_pull_passes_runtime_false(fake_config):
    captured, ctx = _patched_recovery("tlumi.commands.state.get_stack")
    with ctx:
        with patch("tlumi.commands.state.load_config", return_value=fake_config):
            with patch(
                "tlumi.commands.state.find_project_dir", return_value=fake_config.project_dir
            ):
                from tlumi.commands.state import run_state_pull

                with pytest.raises(RuntimeError, match="short-circuit"):
                    run_state_pull()
    _assert_runtime_false(captured)


def test_output_passes_runtime_false(fake_config):
    captured, ctx = _patched_recovery("tlumi.commands.output.get_stack")
    with ctx:
        with patch("tlumi.commands.output.load_config", return_value=fake_config):
            with patch(
                "tlumi.commands.output.find_project_dir", return_value=fake_config.project_dir
            ):
                from tlumi.commands.output import run_output

                with pytest.raises(RuntimeError, match="short-circuit"):
                    run_output()
    _assert_runtime_false(captured)


def test_show_passes_runtime_false(fake_config):
    captured, ctx = _patched_recovery("tlumi.commands.show.get_stack")
    with ctx:
        with patch("tlumi.commands.show.load_config", return_value=fake_config):
            with patch(
                "tlumi.commands.show.find_project_dir", return_value=fake_config.project_dir
            ):
                from tlumi.commands.show import run_show

                with pytest.raises(RuntimeError, match="short-circuit"):
                    run_show()
    _assert_runtime_false(captured)


def test_cancel_passes_runtime_false(fake_config):
    captured, ctx = _patched_recovery("tlumi.commands.cancel.get_stack")
    with ctx:
        with patch("tlumi.commands.cancel.load_config", return_value=fake_config):
            with patch(
                "tlumi.commands.cancel.find_project_dir", return_value=fake_config.project_dir
            ):
                from tlumi.commands.cancel import run_cancel

                with pytest.raises(RuntimeError, match="short-circuit"):
                    run_cancel()
    _assert_runtime_false(captured)


def test_state_show_passes_runtime_false(fake_config):
    captured, ctx = _patched_recovery("tlumi.commands.state.get_stack")
    with ctx:
        with patch("tlumi.commands.state.load_config", return_value=fake_config):
            with patch(
                "tlumi.commands.state.find_project_dir", return_value=fake_config.project_dir
            ):
                from tlumi.commands.state import run_state_show

                with pytest.raises(RuntimeError, match="short-circuit"):
                    run_state_show("some-resource")
    _assert_runtime_false(captured)


def test_state_rm_passes_runtime_false(fake_config):
    captured, ctx = _patched_recovery("tlumi.commands.state.get_stack")
    with ctx:
        with patch("tlumi.commands.state.load_config", return_value=fake_config):
            with patch(
                "tlumi.commands.state.find_project_dir", return_value=fake_config.project_dir
            ):
                from tlumi.commands.state import run_state_rm

                with pytest.raises(RuntimeError, match="short-circuit"):
                    run_state_rm("some-resource", auto_approve=True)
    _assert_runtime_false(captured)


def test_state_mv_passes_runtime_false(fake_config):
    captured, ctx = _patched_recovery("tlumi.commands.state.get_stack")
    with ctx:
        with patch("tlumi.commands.state.load_config", return_value=fake_config):
            with patch(
                "tlumi.commands.state.find_project_dir", return_value=fake_config.project_dir
            ):
                from tlumi.commands.state import run_state_mv

                with pytest.raises(RuntimeError, match="short-circuit"):
                    run_state_mv("a", "b", auto_approve=True)
    _assert_runtime_false(captured)


def test_state_push_passes_runtime_false(fake_config, tmp_path):
    import json

    # A valid same-project state file so file validation + the cross-project
    # guard pass and execution reaches get_stack.
    fake_config.name = "p"
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "version": 3,
                "deployment": {
                    "resources": [
                        {
                            "type": "aws:s3:BucketV2",
                            "urn": "urn:pulumi:default::p::aws:s3:BucketV2::b",
                        }
                    ]
                },
            }
        )
    )
    captured, ctx = _patched_recovery("tlumi.commands.state.get_stack")
    with ctx:
        with patch("tlumi.commands.state.load_config", return_value=fake_config):
            with patch(
                "tlumi.commands.state.find_project_dir", return_value=fake_config.project_dir
            ):
                from tlumi.commands.state import run_state_push

                with pytest.raises(RuntimeError, match="short-circuit"):
                    run_state_push(str(state_file), auto_approve=True)
    _assert_runtime_false(captured)
