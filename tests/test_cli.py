"""Tests for tlumi.cli: _run error handling, _emit_json_error, _handle_engine_error."""

from __future__ import annotations

import io
import json
import subprocess  # nosec B404 - tests spawn the venv python for a real-pipe check
import sys
import textwrap
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
import typer

from tlumi.cli import RunContext, _emit_json_error, _handle_engine_error, _run
from tlumi.errors import Diagnostic, EngineError, TlumiError

# _detect_hint tests live in test_hints.py (canonical location)


# ---------------------------------------------------------------------------
# _run: error branches
# ---------------------------------------------------------------------------


@patch("tlumi.cli.create_window")
def test_run_engine_error(mock_window):
    """EngineError → typer.Exit with code 1."""
    mock_window.return_value.__enter__ = MagicMock()
    mock_window.return_value.__exit__ = MagicMock(return_value=False)

    def boom():
        raise EngineError("engine failed", diagnostics=[Diagnostic("res", "msg")])

    with pytest.raises(typer.Exit) as exc_info:
        _run(boom, RunContext())
    assert exc_info.value.exit_code == 1


@patch("tlumi.cli.create_window")
def test_run_tlumi_error(mock_window):
    """TlumiError → typer.Exit with code 1."""
    mock_window.return_value.__enter__ = MagicMock()
    mock_window.return_value.__exit__ = MagicMock(return_value=False)

    def boom():
        raise TlumiError("something broke", hint="fix it")

    with pytest.raises(typer.Exit) as exc_info:
        _run(boom, RunContext())
    assert exc_info.value.exit_code == 1


@patch("tlumi.cli.create_window")
def test_run_keyboard_interrupt(mock_window):
    """KeyboardInterrupt → typer.Exit with code 130."""
    mock_window.return_value.__enter__ = MagicMock()
    mock_window.return_value.__exit__ = MagicMock(return_value=False)

    def boom():
        raise KeyboardInterrupt()

    with pytest.raises(typer.Exit) as exc_info:
        _run(boom, RunContext())
    assert exc_info.value.exit_code == 130


def test_run_keyboard_interrupt_json_mode(capsys):
    """KeyboardInterrupt in JSON mode emits JSON error, not Rich markup."""

    def boom():
        raise KeyboardInterrupt()

    with pytest.raises(typer.Exit) as exc_info:
        _run(boom, RunContext(json_output=True))
    assert exc_info.value.exit_code == 130
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data == {"error": "Interrupted"}


def test_run_keyboard_interrupt_windowless_routes_to_stderr(capsys):
    """Windowless Ctrl+C keeps stdout a pure data channel; message goes to stderr.

    Regression: interrupting `tlumi state pull > backup.json` used to append
    '\\n  Interrupted.\\n' to the backup file on stdout.
    """
    import tlumi.display as display

    orig_file = display.err_console._file
    display.err_console.file = sys.stderr  # bind to the captured stderr
    try:

        def boom():
            raise KeyboardInterrupt()

        with pytest.raises(typer.Exit) as exc_info:
            _run(boom, RunContext(), windowless=True)
        assert exc_info.value.exit_code == 130
        captured = capsys.readouterr()
        assert "Interrupted." in captured.err
        assert "Interrupted." not in captured.out
    finally:
        display.err_console._file = orig_file


# ---------------------------------------------------------------------------
# _run: BrokenPipeError (stdout reader closed the pipe early)
#
# These exercise the REAL mechanism: writes through the shared Rich display
# console fail with EPIPE, like `tlumi show | head`. Rich >= 13.2 intercepts
# BrokenPipeError inside Console._check_buffer and raises SystemExit(1);
# display._TlumiConsole re-raises BrokenPipeError so _run's handler engages.
# ---------------------------------------------------------------------------


class _BrokenPipeFile(io.TextIOBase):
    """File whose non-empty writes fail with EPIPE, like a closed-reader pipe."""

    def write(self, s: str) -> int:
        if s:
            raise BrokenPipeError()
        return 0

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


@contextmanager
def _broken_stdout_console():
    """Point the shared display console at an EPIPE-raising file.

    Restores the console's file AND its quiet flag: _TlumiConsole mutes
    itself on the first EPIPE, which would silence every later test. Also
    drops Rich's internal buffer: a failed write leaves its segments queued
    (rich clears the buffer only after a successful write), and they would
    re-flush into the next test's captured stdout. In production the muted
    console discards them on its next print, and the process exits anyway.
    """
    import tlumi.display as display

    orig_file = display.console._file
    orig_quiet = display.console.quiet
    display.console.file = _BrokenPipeFile()
    try:
        yield
    finally:
        display.console._file = orig_file
        display.console.quiet = orig_quiet
        del display.console._buffer[:]


@patch("tlumi.cli._pacify_broken_stdout")
def test_run_broken_pipe_exits_zero(mock_pacify, capsys):
    """EPIPE surfacing through console.print (`tlumi show | head`) exits 0 quietly."""

    def fn():
        from tlumi.display import console

        for i in range(50):
            console.print(f"resource line {i}")

    with _broken_stdout_console():
        with pytest.raises(typer.Exit) as exc_info:
            _run(fn, RunContext())
    assert exc_info.value.exit_code == 0
    mock_pacify.assert_called_once()
    assert capsys.readouterr().err == ""


@patch("tlumi.cli._pacify_broken_stdout")
def test_run_broken_pipe_windowless_exits_zero(mock_pacify, capsys):
    """Windowless commands (state pull, output --raw) also exit 0 on EPIPE."""
    from tlumi.display import print_json as display_print_json

    def fn():
        display_print_json('{"version": 3, "deployment": {}}')

    with _broken_stdout_console():
        with pytest.raises(typer.Exit) as exc_info:
            _run(fn, RunContext(), windowless=True)
    assert exc_info.value.exit_code == 0
    mock_pacify.assert_called_once()
    assert capsys.readouterr().err == ""


@patch("tlumi.cli._pacify_broken_stdout")
def test_run_broken_pipe_from_window_teardown_exits_zero(mock_pacify, capsys):
    """EPIPE raised during real window teardown (after fn succeeded) also exits 0.

    The window's __exit__ prints a trailing newline through the display
    console; with the reader gone that write is the first to hit EPIPE.
    """
    with _broken_stdout_console():
        with pytest.raises(typer.Exit) as exc_info:
            _run(lambda: None, RunContext())
    assert exc_info.value.exit_code == 0
    mock_pacify.assert_called_once()
    assert capsys.readouterr().err == ""


@patch("tlumi.cli._pacify_broken_stdout")
def test_run_broken_pipe_during_error_report_keeps_exit_one(mock_pacify):
    """EPIPE while printing a failure must not convert exit 1 into exit 0.

    A failed command piped to a truncating reader must still report failure;
    both the JSON envelope path and the Rich print_error path are covered.
    """

    def boom():
        raise TlumiError("real failure")

    for run_ctx in (RunContext(json_output=True), RunContext()):
        with _broken_stdout_console():
            with pytest.raises(typer.Exit) as exc_info:
                _run(boom, run_ctx)
        assert exc_info.value.exit_code == 1


@patch("tlumi.cli._pacify_broken_stdout")
def test_run_broken_pipe_during_interrupt_report_keeps_exit_130(mock_pacify):
    """EPIPE while printing the JSON interrupt envelope keeps exit 130."""

    def boom():
        raise KeyboardInterrupt()

    with _broken_stdout_console():
        with pytest.raises(typer.Exit) as exc_info:
            _run(boom, RunContext(json_output=True))
    assert exc_info.value.exit_code == 130


def test_run_broken_pipe_end_to_end_subprocess():
    """Real pipe: child prints through _run, reader closes early, child exits 0.

    This is the actual `tlumi show | head` mechanism end-to-end: a fresh
    process, a real OS pipe whose read end closes after 50 bytes, Rich
    console output, and an empty stderr.
    """
    script = textwrap.dedent(
        """
        import sys
        import typer
        import tlumi.cli as cli
        from tlumi.display import console

        def fn():
            for i in range(5000):
                console.print(f"resource line {i} with some padding text")

        try:
            cli._run(fn, cli.RunContext())
        except typer.Exit as e:
            raise SystemExit(e.exit_code)
        raise SystemExit(0)
        """
    )
    proc = subprocess.Popen(  # nosec B603 - fixed argv, venv python, test-only
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdout is not None and proc.stderr is not None
    proc.stdout.read(50)
    proc.stdout.close()  # reader hangs up -> child's next write gets EPIPE
    stderr = proc.stderr.read()
    proc.stderr.close()
    returncode = proc.wait(timeout=30)
    assert returncode == 0, stderr.decode()
    assert stderr == b""


def test_run_success():
    """Successful function completes without raising."""
    called = False

    def ok():
        nonlocal called
        called = True

    _run(ok, RunContext(json_output=True))
    assert called


def test_workspace_notice_handler_suppressed_in_json_mode():
    """_WorkspaceNoticeHandler suppresses output when _json_active is True."""
    import logging

    import tlumi.cli as cli_mod

    original = cli_mod._json_active
    try:
        cli_mod._json_active = True
        handler = cli_mod._WorkspaceNoticeHandler()
        record = logging.LogRecord(
            name="tlumi.workspace",
            level=logging.WARNING,
            pathname="",
            lineno=0,
            msg="test warning",
            args=(),
            exc_info=None,
        )
        # Should not raise or produce output
        handler.emit(record)
    finally:
        cli_mod._json_active = original


def test_workspace_notice_handler_routes_to_stderr(capsys):
    """In non-JSON mode, notices go to STDERR, keeping stdout a clean data channel.

    This is the load-bearing part of the cli.py:44 fix: state pull / output --raw
    do not set _json_active yet must not see notices on stdout.
    """
    import logging

    import tlumi.cli as cli_mod
    import tlumi.display as display

    original = cli_mod._json_active
    # Save the raw _file attribute (None means "resolve sys.stderr lazily");
    # reading .file would resolve it and make the restore bind a stale stream.
    orig_err_file = display.err_console._file
    try:
        cli_mod._json_active = False
        display.err_console.file = sys.stderr  # ensure bound to the captured stderr
        handler = cli_mod._WorkspaceNoticeHandler()
        for level in (logging.WARNING, logging.INFO):
            handler.emit(
                logging.LogRecord(
                    name="tlumi.workspace",
                    level=level,
                    pathname="",
                    lineno=0,
                    msg="notice text here",
                    args=(),
                    exc_info=None,
                )
            )
        captured = capsys.readouterr()
        assert "notice text here" in captured.err
        assert "notice text here" not in captured.out
    finally:
        cli_mod._json_active = original
        display.err_console._file = orig_err_file


# ---------------------------------------------------------------------------
# _emit_json_error
# ---------------------------------------------------------------------------


def test_emit_json_error_plain(capsys):
    """TlumiError emits JSON with error and optional hint."""
    e = TlumiError("bad config", hint="fix your yaml")
    _emit_json_error(e)
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["error"] == "bad config"
    assert data["hint"] == "fix your yaml"
    assert "diagnostics" not in data


def test_emit_json_error_no_hint(capsys):
    """TlumiError without hint omits the hint key."""
    e = TlumiError("bad config")
    _emit_json_error(e)
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["error"] == "bad config"
    assert "hint" not in data


def test_emit_json_error_engine_error(capsys):
    """EngineError emits JSON with diagnostics array."""
    e = EngineError(
        "apply failed",
        diagnostics=[Diagnostic("my-bucket", "access denied"), Diagnostic("", "general error")],
    )
    _emit_json_error(e)
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["error"] == "apply failed"
    assert len(data["diagnostics"]) == 2
    assert data["diagnostics"][0] == {"resource": "my-bucket", "message": "access denied"}
    assert data["diagnostics"][1] == {"resource": "", "message": "general error"}


def test_emit_json_error_engine_error_empty_diagnostics(capsys):
    """EngineError with empty diagnostics omits the diagnostics key."""
    e = EngineError("apply failed")
    _emit_json_error(e)
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["error"] == "apply failed"
    assert "diagnostics" not in data


def test_emit_json_error_engine_error_populates_hint_from_pattern(capsys):
    """EngineError without explicit hint -> _detect_hint runs -> JSON includes the hint.

    Regression for the chain EngineError -> _emit_json_error -> _detect_hint.
    JSON consumers must see the same recovery guidance as the human path.
    """
    e = EngineError("apply failed", full_output="error: state is currently locked")
    _emit_json_error(e)
    data = json.loads(capsys.readouterr().out)
    assert data["error"] == "apply failed"
    assert "hint" in data
    assert "tlumi state unlock" in data["hint"]


def test_handle_engine_error_rich_path_escapes_markup(capsys):
    """Resource names and messages containing Rich-markup-looking text are escaped."""
    e = EngineError(
        "apply failed",
        diagnostics=[Diagnostic("[bold]evil[/bold]", "msg with [link=x]markup[/link]")],
        full_output="",
    )
    with pytest.raises(typer.Exit):
        _handle_engine_error(e, verbose=False)
    output = capsys.readouterr().out
    # The markup characters survive as literal text, not as styled output
    assert "[bold]evil[/bold]" in output
    assert "[link=x]markup[/link]" in output


# ---------------------------------------------------------------------------
# _run: cursor restore on unexpected exception
# ---------------------------------------------------------------------------


@patch("tlumi.cli.create_window")
@patch("tlumi.cli.console")
def test_run_unexpected_exception_restores_cursor(mock_console, mock_window):
    """Unexpected exceptions restore cursor visibility before propagating."""
    mock_window.return_value.__enter__ = MagicMock()
    mock_window.return_value.__exit__ = MagicMock(return_value=False)

    def boom():
        raise RuntimeError("unexpected")

    with pytest.raises(RuntimeError, match="unexpected"):
        _run(boom, RunContext())

    mock_console.show_cursor.assert_called_once_with(True)


# ---------------------------------------------------------------------------
# _run: JSON mode errors (T4)
# ---------------------------------------------------------------------------


def test_run_engine_error_json_mode(capsys):
    """EngineError in JSON mode emits JSON error and exits."""

    def boom():
        raise EngineError("engine failed", diagnostics=[Diagnostic("res", "msg")])

    with pytest.raises(typer.Exit) as exc_info:
        _run(boom, RunContext(json_output=True))
    assert exc_info.value.exit_code == 1
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["error"] == "engine failed"
    assert data["diagnostics"][0]["resource"] == "res"


def test_run_tlumi_error_json_mode(capsys):
    """TlumiError in JSON mode emits JSON error with hint."""

    def boom():
        raise TlumiError("bad config", hint="fix it")

    with pytest.raises(typer.Exit) as exc_info:
        _run(boom, RunContext(json_output=True))
    assert exc_info.value.exit_code == 1
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["error"] == "bad config"
    assert data["hint"] == "fix it"


# ---------------------------------------------------------------------------
# _handle_engine_error: verbose output (T9)
# ---------------------------------------------------------------------------


def test_handle_engine_error_verbose_shows_full_output(capsys):
    """Verbose mode shows full output."""
    e = EngineError("failed", full_output="line1\nline2\n")
    with pytest.raises(typer.Exit):
        _handle_engine_error(e, verbose=True)
    output = capsys.readouterr().out
    assert "line1" in output
    assert "line2" in output


def test_handle_engine_error_non_verbose_suggests_flag(capsys):
    """Non-verbose mode suggests --verbose when output is available."""
    e = EngineError("failed", full_output="some output")
    with pytest.raises(typer.Exit):
        _handle_engine_error(e, verbose=False)
    output = capsys.readouterr().out
    assert "--verbose" in output


# ---------------------------------------------------------------------------
# T7: _quiet_threading_excepthook tests
# ---------------------------------------------------------------------------


def _make_excepthook_args(exc_type, exc_value, thread):
    """Create a mock ExceptHookArgs (structseq, not constructible with kwargs)."""
    args = MagicMock()
    args.exc_type = exc_type
    args.exc_value = exc_value
    args.exc_traceback = None
    args.thread = thread
    return args


def test_quiet_threading_excepthook_suppresses_pulumi_value_error():
    """Pulumi thread ValueError with 'closed file' is suppressed."""
    from tlumi.cli import _quiet_threading_excepthook

    thread = MagicMock()
    thread.name = "pulumi-event-stream"
    args = _make_excepthook_args(ValueError, ValueError("I/O operation on closed file"), thread)
    # Should not raise (suppressed)
    with patch("tlumi.cli._original_excepthook") as mock_hook:
        _quiet_threading_excepthook(args)
        mock_hook.assert_not_called()


def test_quiet_threading_excepthook_passes_non_pulumi():
    """Non-Pulumi thread ValueError is passed through to original hook."""
    from tlumi.cli import _quiet_threading_excepthook

    thread = MagicMock()
    thread.name = "my-worker-thread"
    args = _make_excepthook_args(ValueError, ValueError("I/O operation on closed file"), thread)
    with patch("tlumi.cli._original_excepthook") as mock_hook:
        _quiet_threading_excepthook(args)
        mock_hook.assert_called_once_with(args)


def test_quiet_threading_excepthook_passes_non_value_error():
    """Non-ValueError in Pulumi thread is passed through to original hook."""
    from tlumi.cli import _quiet_threading_excepthook

    thread = MagicMock()
    thread.name = "pulumi-event-stream"
    args = _make_excepthook_args(RuntimeError, RuntimeError("something else"), thread)
    with patch("tlumi.cli._original_excepthook") as mock_hook:
        _quiet_threading_excepthook(args)
        mock_hook.assert_called_once_with(args)


def test_handle_engine_error_with_hint(capsys):
    """Detected hint is displayed."""
    e = EngineError("failed", full_output="the stack is currently locked")
    with pytest.raises(typer.Exit):
        _handle_engine_error(e, verbose=False)
    output = capsys.readouterr().out
    assert "state unlock" in output
