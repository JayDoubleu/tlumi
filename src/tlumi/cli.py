"""tlumi CLI - Terraform-like workflow for Python IaC."""

from __future__ import annotations

import dataclasses
import inspect
import json
import logging
import os
import re
import sys
import threading
import warnings
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer

import tlumi
from tlumi.display import (
    console,
    create_window,
    err_console,
    print_error,
    print_json,
)
from tlumi.errors import EngineError, TlumiError

# Suppress Pulumi's internal thread errors on interrupt (ValueError from closed pipes)
_original_excepthook = threading.excepthook
_log = logging.getLogger(__name__)
_json_active: bool = False
"""Module-level flag so _WorkspaceNoticeHandler can suppress output in JSON mode."""


class _WorkspaceNoticeHandler(logging.Handler):
    """Route tlumi.* log messages to Rich console output.

    WARNING+ maps to print_warning(); INFO maps to muted console text.
    Registered on the root "tlumi" logger so every submodule (workspace,
    engine, sdk_compat, commands.*) routes through here automatically.
    Earlier versions attached the handler to a hand-picked subset of
    loggers, which silently dropped warnings from any logger not on the
    list (e.g. tlumi.commands.deps, tlumi.commands.state).
    """

    def emit(self, record: logging.LogRecord) -> None:
        from rich.markup import escape

        if _json_active:
            return
        msg = self.format(record)
        # Route to stderr: these notices must not land on stdout, which is the
        # machine-readable data channel for `state pull` and `output --raw`
        # (neither sets _json_active, yet both need a clean stdout).
        if record.levelno >= logging.WARNING:
            err_console.print(f"  [warning]{escape(msg)}[/warning]")
        else:
            err_console.print(f"  [muted]{escape(msg)}[/muted]")


_ws_handler = _WorkspaceNoticeHandler()
_ws_handler.setLevel(logging.INFO)
_root_logger = logging.getLogger("tlumi")
_root_logger.addHandler(_ws_handler)
_root_logger.setLevel(logging.DEBUG)


def _quiet_threading_excepthook(args: threading.ExceptHookArgs) -> None:
    if (
        args.exc_type is ValueError
        and "closed file" in str(args.exc_value)
        and args.thread is not None
        and "pulumi" in args.thread.name.lower()
    ):
        _log.debug("Suppressed threading error in %s: %s", args.thread.name, args.exc_value)
        return
    _original_excepthook(args)


threading.excepthook = _quiet_threading_excepthook

# Suppress Pulumi's "Error in event stream" warnings on interrupt
warnings.filterwarnings("ignore", message="Error in event stream", module="pulumi")


@dataclass(frozen=True, slots=True)
class RunContext:
    """Runtime flags set once during _setup(), read-only afterward.

    Threaded through commands via typer.Context.ensure_object(). Frozen so
    that the documented "read-only after setup" invariant is enforced by
    the type system: updates go through ``dataclasses.replace`` and a new
    instance is stored back on the typer context.
    """

    verbose: bool = False
    json_output: bool = False


# Reusable option types for subcommands
_verbose_option = Annotated[
    bool,
    typer.Option("--verbose", help="Show full error output on failure."),
]
_var_option = Annotated[
    list[str] | None,
    typer.Option("--var", help="Set a variable (KEY=VALUE)."),
]
_var_file_option = Annotated[
    list[str] | None,
    typer.Option("--var-file", help="Load variables from YAML file."),
]
_json_option = Annotated[
    bool,
    typer.Option("--json", help="Output in JSON format."),
]
_target_option = Annotated[
    list[str] | None,
    typer.Option(
        "--target", "-t", help="Target specific resources (name, type::name, or URN). Repeatable."
    ),
]
_replace_option = Annotated[
    list[str] | None,
    typer.Option(
        "--replace",
        "-r",
        help="Force replacement of specific resources (name, type::name, or URN). Repeatable.",
    ),
]
_show_secrets_option = Annotated[
    bool,
    typer.Option("--show-secrets", help="Show secret values in cleartext."),
]

_TYPER_HAS_SUGGEST_COMMANDS = (
    "suggest_commands" in inspect.signature(typer.Typer.__init__).parameters
)


def _make_typer(**kwargs: Any) -> typer.Typer:
    """Construct a Typer app with typer's own command suggestions disabled.

    typer >= 0.20 appends its own "Did you mean" onto click >= 8.3's
    UsageError, which already carries click's suggestion, so every unknown
    command printed a doubled message ("Did you mean 'state'? Did you mean
    'state'?"). Disable typer's copy; click's native single suggestion
    remains. The parameter does not exist below typer 0.20 (the dependency
    floor is 0.16), hence the feature detection.
    """
    if _TYPER_HAS_SUGGEST_COMMANDS:
        kwargs["suggest_commands"] = False
    return typer.Typer(**kwargs)


app = _make_typer(
    name="tlumi",
    help="Terraform-like workflow for Python infrastructure-as-code, powered by Pulumi.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

deps_app = _make_typer(
    name="deps",
    help="Manage provider packages.",
    no_args_is_help=True,
)
app.add_typer(deps_app, name="deps")

state_app = _make_typer(
    name="state",
    help="Inspect and manage state.",
    no_args_is_help=True,
)
app.add_typer(state_app, name="state")


def _setup(ctx: typer.Context, verbose: bool = False, json: bool = False) -> RunContext:
    """Extract RunContext and apply verbose/json flags.

    Verbose lowers the workspace notice handler to DEBUG so internal
    diagnostics ("config cleanup skipped", "venv lib unreadable", backup
    rotation failures, etc.) surface to the user. The handler is otherwise
    pinned at INFO so day-to-day output stays quiet.
    """
    global _json_active
    run_ctx = ctx.ensure_object(RunContext)
    if verbose or json:
        run_ctx = dataclasses.replace(
            run_ctx,
            verbose=run_ctx.verbose or verbose,
            json_output=run_ctx.json_output or json,
        )
        ctx.obj = run_ctx
    if verbose:
        _ws_handler.setLevel(logging.DEBUG)
    if json:
        _json_active = True
    return run_ctx


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"tlumi {tlumi.__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version", "-v", callback=_version_callback, is_eager=True, help="Show version."
        ),
    ] = False,
    verbose: _verbose_option = False,
) -> None:
    """tlumi - Terraform-like workflow for Python infrastructure-as-code."""
    if sys.platform == "win32":
        # tlumi relies on POSIX-only primitives (O_NOFOLLOW symlink guards, venv
        # bin/python layout, file:// backend paths). Fail with a clear message
        # instead of a raw AttributeError deep inside an internal helper.
        console.print(
            "  [error]Error:[/error] tlumi does not support Windows natively.\n"
            "  [muted]Run it under WSL2 (Windows Subsystem for Linux) instead.[/muted]"
        )
        raise typer.Exit(1)
    _setup(ctx, verbose)


@app.command()
def init(
    ctx: typer.Context,
    name: Annotated[
        str | None, typer.Option("--name", "-n", help="Project name (defaults to directory name).")
    ] = None,
    path: Annotated[
        Path | None, typer.Option("--path", "-p", help="Directory to initialize (defaults to cwd).")
    ] = None,
    verbose: _verbose_option = False,
) -> None:
    """Initialize a new tlumi project in the current directory."""
    _run(lambda: _do_init(name=name, path=path), _setup(ctx, verbose))


def _do_init(name: str | None, path: Path | None) -> None:
    from tlumi.commands.init import run_init

    run_init(project_dir=path, name=name)


@app.command()
def clean(
    ctx: typer.Context,
    auto_approve: Annotated[
        bool, typer.Option("--auto-approve", help="Skip confirmation prompt.")
    ] = False,
    include_state: Annotated[
        bool, typer.Option("--include-state", help="Also remove infrastructure state and backups.")
    ] = False,
    verbose: _verbose_option = False,
) -> None:
    """Remove re-creatable .tlumi/ caches; state and backups are kept unless --include-state."""
    _run(lambda: _do_clean(auto_approve, include_state), _setup(ctx, verbose))


def _do_clean(auto_approve: bool, include_state: bool) -> None:
    from tlumi.commands.clean import run_clean

    run_clean(auto_approve=auto_approve, include_state=include_state)


@app.command()
def plan(
    ctx: typer.Context,
    var: _var_option = None,
    var_file: _var_file_option = None,
    target: _target_option = None,
    replace: _replace_option = None,
    destroy: Annotated[
        bool, typer.Option("--destroy", help="Preview destruction of all resources.")
    ] = False,
    out: Annotated[
        str | None, typer.Option("--out", "-o", help="Save plan to file for later apply.")
    ] = None,
    json: _json_option = False,
    verbose: _verbose_option = False,
) -> None:
    """Preview infrastructure changes without applying them."""
    _run(
        lambda: _do_plan(
            var=var,
            var_file=var_file,
            target=target,
            replace=replace,
            destroy=destroy,
            out=out,
            json_output=json,
        ),
        _setup(ctx, verbose, json),
    )


def _do_plan(
    var: list[str] | None,
    var_file: list[str] | None,
    target: list[str] | None = None,
    replace: list[str] | None = None,
    destroy: bool = False,
    out: str | None = None,
    json_output: bool = False,
) -> None:
    from tlumi.commands.plan import run_plan

    run_plan(
        var=var,
        var_file=var_file,
        target=target,
        replace=replace,
        destroy=destroy,
        out=out,
        json_output=json_output,
    )


@app.command()
def apply(
    ctx: typer.Context,
    auto_approve: Annotated[
        bool, typer.Option("--auto-approve", help="Skip confirmation prompt.")
    ] = False,
    var: _var_option = None,
    var_file: _var_file_option = None,
    target: _target_option = None,
    replace: _replace_option = None,
    plan_file: Annotated[
        str | None, typer.Option("--plan", help="Apply a saved plan file (from plan --out).")
    ] = None,
    show_secrets: _show_secrets_option = False,
    json: _json_option = False,
    verbose: _verbose_option = False,
) -> None:
    """Apply infrastructure changes."""
    _run(
        lambda: _do_apply(
            auto_approve,
            var=var,
            var_file=var_file,
            target=target,
            replace=replace,
            plan_file=plan_file,
            show_secrets=show_secrets,
            json_output=json,
        ),
        _setup(ctx, verbose, json),
    )


def _do_apply(
    auto_approve: bool,
    var: list[str] | None,
    var_file: list[str] | None,
    target: list[str] | None = None,
    replace: list[str] | None = None,
    plan_file: str | None = None,
    show_secrets: bool = False,
    json_output: bool = False,
) -> None:
    from tlumi.commands.apply import run_apply

    run_apply(
        auto_approve=auto_approve,
        var=var,
        var_file=var_file,
        target=target,
        replace=replace,
        plan_file=plan_file,
        show_secrets=show_secrets,
        json_output=json_output,
    )


@app.command()
def destroy(
    ctx: typer.Context,
    auto_approve: Annotated[
        bool, typer.Option("--auto-approve", help="Skip confirmation prompt.")
    ] = False,
    var: _var_option = None,
    var_file: _var_file_option = None,
    target: _target_option = None,
    json: _json_option = False,
    verbose: _verbose_option = False,
) -> None:
    """Destroy all managed infrastructure."""
    _run(
        lambda: _do_destroy(
            auto_approve, var=var, var_file=var_file, target=target, json_output=json
        ),
        _setup(ctx, verbose, json),
    )


def _do_destroy(
    auto_approve: bool,
    var: list[str] | None,
    var_file: list[str] | None,
    target: list[str] | None = None,
    json_output: bool = False,
) -> None:
    from tlumi.commands.destroy import run_destroy

    run_destroy(
        auto_approve=auto_approve,
        var=var,
        var_file=var_file,
        target=target,
        json_output=json_output,
    )


@app.command()
def validate(
    ctx: typer.Context,
    var: _var_option = None,
    var_file: _var_file_option = None,
    json: _json_option = False,
    verbose: _verbose_option = False,
) -> None:
    """Check project configuration and Python syntax (does not validate infrastructure logic)."""
    _run(
        lambda: _do_validate(var=var, var_file=var_file, json_output=json),
        _setup(ctx, verbose, json),
    )


def _do_validate(
    var: list[str] | None, var_file: list[str] | None, json_output: bool = False
) -> None:
    from tlumi.commands.validate import run_validate

    run_validate(var=var, var_file=var_file, json_output=json_output)


@app.command()
def refresh(
    ctx: typer.Context,
    var: _var_option = None,
    var_file: _var_file_option = None,
    target: _target_option = None,
    json: _json_option = False,
    verbose: _verbose_option = False,
) -> None:
    """Reconcile state with actual cloud resources."""
    _run(
        lambda: _do_refresh(var=var, var_file=var_file, target=target, json_output=json),
        _setup(ctx, verbose, json),
    )


def _do_refresh(
    var: list[str] | None,
    var_file: list[str] | None,
    target: list[str] | None = None,
    json_output: bool = False,
) -> None:
    from tlumi.commands.refresh import run_refresh

    run_refresh(var=var, var_file=var_file, target=target, json_output=json_output)


@app.command("import")
def import_resource(
    ctx: typer.Context,
    resource_type: Annotated[
        str, typer.Argument(help="Pulumi resource type (e.g. aws:s3:BucketV2).")
    ],
    name: Annotated[str, typer.Argument(help="Logical name for the resource.")],
    resource_id: Annotated[str, typer.Argument(help="Cloud provider ID of the existing resource.")],
    var: _var_option = None,
    var_file: _var_file_option = None,
    protect: Annotated[
        bool,
        typer.Option(
            "--protect",
            help="Mark the imported resource protected in state (Pulumi's default). "
            "Without this flag, imported resources can be destroyed like any other.",
        ),
    ] = False,
    verbose: _verbose_option = False,
) -> None:
    """Import an existing cloud resource into state."""
    _run(
        lambda: _do_import(
            resource_type, name, resource_id, var=var, var_file=var_file, protect=protect
        ),
        _setup(ctx, verbose),
    )


def _do_import(
    resource_type: str,
    name: str,
    resource_id: str,
    var: list[str] | None,
    var_file: list[str] | None,
    protect: bool = False,
) -> None:
    from tlumi.commands.import_cmd import run_import

    run_import(resource_type, name, resource_id, var=var, var_file=var_file, protect=protect)


@app.command()
def fmt(
    ctx: typer.Context,
    check: Annotated[
        bool, typer.Option("--check", help="Check formatting without modifying files.")
    ] = False,
    verbose: _verbose_option = False,
) -> None:
    """Format project Python files (uses ruff)."""
    _run(lambda: _do_fmt(check=check), _setup(ctx, verbose))


def _do_fmt(check: bool = False) -> None:
    from tlumi.commands.fmt import run_fmt

    run_fmt(check=check)


@app.command()
def version() -> None:
    """Show version information."""
    typer.echo(f"tlumi {tlumi.__version__}")


@app.command()
def output(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Specific output name to display.")] = None,
    json: Annotated[bool, typer.Option("--json", help="Output in JSON format.")] = False,
    raw: Annotated[bool, typer.Option("--raw", help="Output raw value (no formatting).")] = False,
    show_secrets: _show_secrets_option = False,
    verbose: _verbose_option = False,
) -> None:
    """Display output values from the current state."""
    _run(
        lambda: _do_output(name, json_output=json, raw=raw, show_secrets=show_secrets),
        _setup(ctx, verbose, json),
        windowless=raw,
    )


def _do_output(name: str | None, json_output: bool, raw: bool, show_secrets: bool = False) -> None:
    from tlumi.commands.output import run_output

    run_output(name=name, json_output=json_output, raw=raw, show_secrets=show_secrets)


@app.command()
def show(
    ctx: typer.Context,
    json: _json_option = False,
    show_secrets: _show_secrets_option = False,
    verbose: _verbose_option = False,
) -> None:
    """Show full state summary of all resources."""
    _run(lambda: _do_show(json_output=json, show_secrets=show_secrets), _setup(ctx, verbose, json))


def _do_show(json_output: bool = False, show_secrets: bool = False) -> None:
    from tlumi.commands.show import run_show

    run_show(json_output=json_output, show_secrets=show_secrets)


# --- deps subcommands ---


@deps_app.command("add")
def deps_add(
    ctx: typer.Context,
    packages: Annotated[list[str], typer.Argument(help="Packages to install (e.g. pulumi-aws).")],
    verbose: _verbose_option = False,
) -> None:
    """Add and install provider packages."""
    _run(lambda: _do_deps_add(packages), _setup(ctx, verbose))


def _do_deps_add(packages: list[str]) -> None:
    from tlumi.commands.deps import run_deps_add

    run_deps_add(packages)


@deps_app.command("install")
def deps_install(
    ctx: typer.Context,
    verbose: _verbose_option = False,
) -> None:
    """Install all packages from requirements.txt."""
    _run(lambda: _do_deps_install(), _setup(ctx, verbose))


def _do_deps_install() -> None:
    from tlumi.commands.deps import run_deps_install

    run_deps_install()


@deps_app.command("list")
def deps_list(
    ctx: typer.Context,
    verbose: _verbose_option = False,
) -> None:
    """List installed provider packages."""
    _run(lambda: _do_deps_list(), _setup(ctx, verbose))


def _do_deps_list() -> None:
    from tlumi.commands.deps import run_deps_list

    run_deps_list()


# --- state subcommands ---


@state_app.command("list")
def state_list(
    ctx: typer.Context,
    json: _json_option = False,
    verbose: _verbose_option = False,
) -> None:
    """List all resources in state."""
    _run(lambda: _do_state_list(json_output=json), _setup(ctx, verbose, json))


def _do_state_list(json_output: bool = False) -> None:
    from tlumi.commands.state import run_state_list

    run_state_list(json_output=json_output)


@state_app.command("show")
def state_show(
    ctx: typer.Context,
    resource: Annotated[
        str, typer.Argument(help="Resource identifier (name, type::name, or URN).")
    ],
    json: _json_option = False,
    show_secrets: _show_secrets_option = False,
    verbose: _verbose_option = False,
) -> None:
    """Show details of a resource in state."""
    _run(
        lambda: _do_state_show(resource, json_output=json, show_secrets=show_secrets),
        _setup(ctx, verbose, json),
    )


def _do_state_show(resource: str, json_output: bool = False, show_secrets: bool = False) -> None:
    from tlumi.commands.state import run_state_show

    run_state_show(resource, json_output=json_output, show_secrets=show_secrets)


@state_app.command("rm")
def state_rm(
    ctx: typer.Context,
    resource: Annotated[
        str, typer.Argument(help="Resource identifier (name, type::name, or URN).")
    ],
    auto_approve: Annotated[
        bool, typer.Option("--auto-approve", help="Skip confirmation prompt.")
    ] = False,
    verbose: _verbose_option = False,
) -> None:
    """Remove a resource from state without destroying it."""
    _run(lambda: _do_state_rm(resource, auto_approve), _setup(ctx, verbose))


def _do_state_rm(resource: str, auto_approve: bool) -> None:
    from tlumi.commands.state import run_state_rm

    run_state_rm(resource, auto_approve=auto_approve)


@state_app.command("mv")
def state_mv(
    ctx: typer.Context,
    source: Annotated[
        str, typer.Argument(help="Source resource identifier (name, type::name, or URN).")
    ],
    destination: Annotated[str, typer.Argument(help="New resource name.")],
    auto_approve: Annotated[
        bool, typer.Option("--auto-approve", help="Skip confirmation prompt.")
    ] = False,
    verbose: _verbose_option = False,
) -> None:
    """Rename a resource in state."""
    _run(lambda: _do_state_mv(source, destination, auto_approve), _setup(ctx, verbose))


def _do_state_mv(source: str, destination: str, auto_approve: bool) -> None:
    from tlumi.commands.state import run_state_mv

    run_state_mv(source, destination, auto_approve=auto_approve)


@state_app.command("pull")
def state_pull(
    ctx: typer.Context,
    verbose: _verbose_option = False,
) -> None:
    """Export state as JSON to stdout."""
    _run(lambda: _do_state_pull(), _setup(ctx, verbose), windowless=True)


def _do_state_pull() -> None:
    from tlumi.commands.state import run_state_pull

    run_state_pull()


@state_app.command("push")
def state_push(
    ctx: typer.Context,
    file: Annotated[str, typer.Argument(help="Path to JSON state file.")],
    auto_approve: Annotated[
        bool, typer.Option("--auto-approve", help="Skip confirmation prompt.")
    ] = False,
    json: _json_option = False,
    verbose: _verbose_option = False,
) -> None:
    """Replace current state with contents of a JSON file."""
    _run(lambda: _do_state_push(file, auto_approve, json_output=json), _setup(ctx, verbose, json))


def _do_state_push(file_path: str, auto_approve: bool, json_output: bool = False) -> None:
    from tlumi.commands.state import run_state_push

    run_state_push(file_path, auto_approve=auto_approve, json_output=json_output)


@state_app.command("unlock")
def state_unlock(
    ctx: typer.Context,
    verbose: _verbose_option = False,
) -> None:
    """Release a stale state lock from a previous interrupted operation."""
    _run(lambda: _do_state_unlock(), _setup(ctx, verbose))


def _do_state_unlock() -> None:
    from tlumi.commands.cancel import run_cancel

    run_cancel()


# --- Error handling ---


def _pacify_broken_stdout() -> None:
    """Point stdout's file descriptor at /dev/null after an EPIPE.

    Prevents interpreter-shutdown flushes and window teardown writes from
    raising a second BrokenPipeError (which would surface as a traceback
    on stderr after we already decided to exit quietly).
    """
    try:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    except (OSError, ValueError):  # nosec B110 - stdout may not have a real fd
        pass


def _run(fn: Callable[[], None], run_ctx: RunContext, *, windowless: bool = False) -> None:
    """Run a command function with unified error handling.

    In normal mode, wraps output in a Live display region. animated_status
    and the engine's Live display compose into it rather than creating
    separate Live contexts.

    ``windowless=True`` skips the Live region (and its trailing blank line) for
    commands whose stdout must stay a clean data channel without --json:
    ``state pull`` and ``output --raw``.

    An early-closed stdout pipe (``tlumi show | head``) exits 0 quietly:
    truncated output is the reader's choice, not a failure.
    """
    ctx = nullcontext() if (run_ctx.json_output or windowless) else create_window()
    # Windowless commands (state pull, output --raw) keep stdout a clean data
    # channel; route their human error output to stderr too, not just notices.
    error_stream = err_console if windowless else None
    try:
        with ctx:
            try:
                fn()
            except EngineError as e:
                if run_ctx.json_output:
                    _emit_json_error(e)
                    raise typer.Exit(1) from None
                else:
                    _handle_engine_error(e, verbose=run_ctx.verbose, stream=error_stream)
            except TlumiError as e:
                if run_ctx.json_output:
                    _emit_json_error(e)
                else:
                    print_error(e.message, e.hint, stream=error_stream)
                raise typer.Exit(1) from None
            except KeyboardInterrupt:
                if run_ctx.json_output:
                    print_json(json.dumps({"error": "Interrupted"}))
                else:
                    # Clear the ^C line and print cleanly. Windowless commands
                    # keep stdout a pure data channel even on interrupt.
                    sys.stderr.write("\r")
                    out = error_stream if error_stream is not None else console
                    out.print("\n  [warning]Interrupted.[/warning]")
                raise typer.Exit(130) from None
            except Exception:
                try:
                    console.show_cursor(True)
                except Exception:  # nosec B110 - best-effort cursor restore
                    pass
                raise
    except BrokenPipeError:
        # The reader closed stdout early (e.g. `tlumi show | head -5`). POSIX
        # CLI convention: exit quietly instead of click's silent exit 1, which
        # fails `set -e`/pipefail scripts with no error text at all.
        _pacify_broken_stdout()
        raise typer.Exit(0) from None


def _emit_json_error(e: TlumiError) -> None:
    """Emit a JSON-formatted error object for --json mode."""
    result: dict = {"error": e.message}
    hint = e.hint
    if not hint and isinstance(e, EngineError):
        hint = _detect_hint(e)
    if hint:
        result["hint"] = hint
    if isinstance(e, EngineError) and e.diagnostics:
        result["diagnostics"] = [
            {"resource": resource, "message": msg} for resource, msg in e.diagnostics
        ]
    print_json(json.dumps(result))


def _handle_engine_error(e: EngineError, *, verbose: bool, stream=None) -> None:
    """Display structured Pulumi engine diagnostics.

    Caller (``_run``) already routes JSON-mode errors to ``_emit_json_error``,
    so this function only handles the human-readable Rich rendering path.
    ``stream`` defaults to stdout; windowless commands pass the stderr console
    so their stdout data channel stays clean even on the error path.
    """
    from rich.markup import escape

    out = stream if stream is not None else console

    out.print(f"\n  [error]Error:[/error] {escape(e.message)}")

    for resource, msg in e.diagnostics:
        if resource:
            out.print(f"  [bold]{escape(resource)}[/bold]:")
            out.print(f"    {escape(msg)}")
        else:
            out.print(f"  {escape(msg)}")

    if e.diagnostics:
        out.print()

    # Detect known errors and provide actionable hints
    hint = _detect_hint(e)

    if verbose and e.full_output.strip():
        out.print("  [muted]Full output:[/muted]")
        for line in e.full_output.splitlines():
            if line.strip():
                out.print(f"  [muted]{escape(line)}[/muted]")
        out.print()
    elif not verbose and e.full_output.strip():
        out.print("  [muted]Use --verbose for full details.[/muted]")
        out.print()

    if hint:
        out.print(f"  [muted]Hint: {escape(hint)}[/muted]")

    raise typer.Exit(1) from None


def _detect_hint(e: EngineError) -> str | None:
    """Detect known error patterns and return actionable hints."""
    output = e.full_output.lower()
    for _, msg in e.diagnostics:
        output += " " + msg.lower()

    if "currently locked" in output or "concurrent update" in output:
        return "Run 'tlumi state unlock' to release the state lock."

    if "no module named" in output or "modulenotfounderror" in output:
        # Best-effort: pull the module name out of "No module named 'X'"
        # (output is already lowercased; PyPI names are case-insensitive and
        # treat '_' and '-' as equivalent, so the module name usually works
        # verbatim as a package name, e.g. pulumi_gcp -> pulumi-gcp).
        match = re.search(r"no module named '([a-z0-9_.]+)'", output)
        module = match.group(1).split(".")[0] if match else None
        if module == "pulumi":
            # The base SDK is managed by init/deps install, not deps add.
            return (
                "Run 'tlumi init' to set up the project, "
                "or 'tlumi deps install' if already initialized."
            )
        if module:
            return (
                f"Run 'tlumi deps add {module}' to install the missing package, "
                "or 'tlumi deps install' if requirements.txt already lists it."
            )
        return (
            "Run 'tlumi deps add <package>' to install the missing provider, "
            "'tlumi deps install' if requirements.txt already lists it, "
            "or 'tlumi init' if the project is not set up."
        )

    if (
        "unauthorized" in output
        or "accessdenied" in output
        or "access denied" in output
        or "forbidden" in output
    ):
        return "Check your cloud provider credentials."

    if (
        "connection refused" in output
        or "could not resolve" in output
        or ("network" in output and "unreachable" in output)
    ):
        return "Check your network connection."

    if "not found" in output and "import" in output:
        return "Verify the resource type, name, and ID are correct."

    if "passphrase" in output or "decryption failed" in output:
        return "Check TLUMI_SECRETS_PASSPHRASE is set correctly."

    if "configuration" in output and ("missing" in output or "required" in output):
        return "Check your provider configuration (e.g. region, project, credentials)."

    if "type not found" in output:
        return (
            "Verify the resource type is correct, "
            "or install the missing provider with 'tlumi deps add'."
        )

    if "timeout" in output and ("exceeded" in output or "expired" in output):
        return "The operation timed out. Check your network connection or cloud provider status."

    return None
