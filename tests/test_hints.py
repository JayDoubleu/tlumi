"""Tests for _detect_hint() in cli.py: known error pattern detection."""

from __future__ import annotations

from tlumi.cli import _detect_hint
from tlumi.errors import Diagnostic, EngineError


def _make_engine_error(
    full_output: str = "", diagnostics: list[Diagnostic] | None = None
) -> EngineError:
    """Build an EngineError with the given output and diagnostics."""
    return EngineError(
        message="test error",
        diagnostics=diagnostics or [],
        full_output=full_output,
    )


def test_detect_hint_locked_state():
    """Locked state pattern returns unlock hint."""
    e = _make_engine_error("the stack is currently locked by another operation")
    assert "state unlock" in _detect_hint(e)


def test_detect_hint_concurrent_update():
    """Concurrent update pattern returns unlock hint."""
    e = _make_engine_error("concurrent update detected")
    assert "state unlock" in _detect_hint(e)


def test_detect_hint_missing_modules():
    """Missing module pattern returns init/deps hint."""
    e = _make_engine_error(diagnostics=[Diagnostic("", "No module named 'pulumi_aws'")])
    hint = _detect_hint(e)
    assert "tlumi init" in hint or "deps install" in hint


def test_detect_hint_module_not_found_error():
    """ModuleNotFoundError in output returns init/deps hint."""
    e = _make_engine_error("ModuleNotFoundError: No module named 'pulumi_azure'")
    hint = _detect_hint(e)
    assert "tlumi init" in hint or "deps install" in hint


def test_detect_hint_missing_module_suggests_deps_add():
    """A named missing module suggests 'tlumi deps add <module>' (the actual fix).

    Regression: the hint used to suggest only init/deps install, neither of
    which installs a provider that was never added to requirements.txt.
    """
    e = _make_engine_error("ModuleNotFoundError: No module named 'pulumi_gcp'")
    hint = _detect_hint(e)
    assert "tlumi deps add pulumi_gcp" in hint
    assert "deps install" in hint


def test_detect_hint_missing_module_dotted_uses_top_level():
    """A dotted submodule path suggests adding the top-level package."""
    e = _make_engine_error("No module named 'pulumi_gcp.storage'")
    assert "tlumi deps add pulumi_gcp" in _detect_hint(e)


def test_detect_hint_missing_module_from_diagnostics():
    """Module name extraction also works from diagnostic messages."""
    e = _make_engine_error(diagnostics=[Diagnostic("", "No module named 'pulumi_aws'")])
    assert "tlumi deps add pulumi_aws" in _detect_hint(e)


def test_detect_hint_missing_pulumi_sdk_suggests_init():
    """The base pulumi SDK is managed by init/deps install, not deps add."""
    e = _make_engine_error("ModuleNotFoundError: No module named 'pulumi'")
    hint = _detect_hint(e)
    assert "tlumi init" in hint
    assert "deps add" not in hint


def test_detect_hint_missing_module_without_name_is_generic():
    """When no module name can be extracted, all three fixes are offered."""
    e = _make_engine_error("modulenotfounderror was raised during program load")
    hint = _detect_hint(e)
    assert "tlumi deps add" in hint
    assert "deps install" in hint
    assert "tlumi init" in hint


def test_detect_hint_auth_errors():
    """Authorization errors return credentials hint."""
    for pattern in ["Unauthorized", "AccessDenied", "access denied", "Forbidden"]:
        e = _make_engine_error(pattern)
        assert "credentials" in _detect_hint(e).lower()


def test_detect_hint_network_errors():
    """Network errors return connection hint."""
    for pattern in ["connection refused", "could not resolve host"]:
        e = _make_engine_error(pattern)
        assert "network" in _detect_hint(e).lower() or "connection" in _detect_hint(e).lower()


def test_detect_hint_network_unreachable():
    """Network unreachable error returns connection hint."""
    e = _make_engine_error("network is unreachable")
    assert "network" in _detect_hint(e).lower() or "connection" in _detect_hint(e).lower()


def test_detect_hint_import_not_found():
    """Import resource not found returns verify hint."""
    e = _make_engine_error("resource not found during import")
    assert "resource type" in _detect_hint(e).lower() or "verify" in _detect_hint(e).lower()


def test_detect_hint_passphrase():
    """Passphrase errors return TLUMI_SECRETS_PASSPHRASE hint."""
    e = _make_engine_error("passphrase must be set")
    assert "TLUMI_SECRETS_PASSPHRASE" in _detect_hint(e)


def test_detect_hint_decryption_failed():
    """Decryption failed returns passphrase hint."""
    e = _make_engine_error("decryption failed for secret")
    assert "TLUMI_SECRETS_PASSPHRASE" in _detect_hint(e)


def test_detect_hint_config_missing():
    """Missing configuration returns config hint."""
    e = _make_engine_error("configuration key 'aws:region' is required but missing")
    assert (
        "provider configuration" in _detect_hint(e).lower() or "config" in _detect_hint(e).lower()
    )


def test_detect_hint_type_not_found():
    """Type not found returns provider install hint."""
    e = _make_engine_error("type not found: aws:lambda:FunctionX")
    hint = _detect_hint(e)
    assert "resource type" in hint.lower() or "deps add" in hint


def test_detect_hint_timeout():
    """Timeout errors return timeout hint."""
    for pattern in ["timeout exceeded", "timeout expired"]:
        e = _make_engine_error(pattern)
        hint = _detect_hint(e)
        assert "timed out" in hint.lower() or "timeout" in hint.lower()


def test_detect_hint_no_match():
    """Unrecognized error returns None."""
    e = _make_engine_error("some random error message")
    assert _detect_hint(e) is None


def test_detect_hint_diagnostics_checked():
    """Hints are detected from diagnostic messages too, not just full_output."""
    e = _make_engine_error(
        full_output="",
        diagnostics=[Diagnostic("aws:s3:BucketV2 (b)", "the stack is currently locked")],
    )
    assert "state unlock" in _detect_hint(e)
