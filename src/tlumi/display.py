"""Rich-based terminal output formatting for tlumi."""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Protocol

from rich.console import Console, ConsoleOptions, RenderableType, RenderResult
from rich.live import Live
from rich.markup import escape
from rich.padding import Padding
from rich.text import Text
from rich.theme import Theme
from rich.tree import Tree

from tlumi.resolve import display_from_urn
from tlumi.sanitize import _sanitize_value

if TYPE_CHECKING:
    from pulumi.automation import Deployment

    from tlumi.diffs import PropertyChange


class LiveDisplay(Protocol):
    """Minimal interface for Live-like display objects.

    Implemented by both Rich's ``Live`` and ``WindowLiveProxy``, so
    ``EventHandler`` can use either without ``type: ignore``.
    """

    @property
    def is_started(self) -> bool: ...
    @property
    def console(self) -> Console: ...
    def update(self, renderable: RenderableType) -> None: ...


tlumi_theme = Theme(
    {
        "create": "bold green",
        "update": "bold yellow",
        "delete": "bold red",
        "replace": "bold cyan",
        "info": "bold blue",
        "success": "bold green",
        "warning": "bold yellow",
        "error": "bold red",
        "muted": "dim",
        "timing": "dim",
    }
)


class _TlumiConsole(Console):
    """Console whose EPIPE handling stays catchable by ``cli._run()``.

    Rich >= 13.2 intercepts BrokenPipeError inside ``Console._check_buffer``
    and calls ``on_broken_pipe()``, whose default raises ``SystemExit(1)``,
    a BaseException that sails past ``_run()``'s BrokenPipeError handler and
    reproduces the silent exit 1 that handler exists to fix
    (``tlumi show | head``). Override: mute the console first so later
    teardown prints (window exit, Live refresh) are dropped instead of
    re-raising, then re-raise BrokenPipeError on the main thread so
    ``cli._run()`` decides the exit code. Non-main threads (the Live refresh
    thread, Pulumi event callbacks) keep Rich's SystemExit convention, which
    ``threading`` swallows silently; the muted console stops their writes and
    the main thread still controls the process exit code. On Rich < 13.2 this
    hook is never called and BrokenPipeError already propagates naturally.
    """

    def on_broken_pipe(self) -> None:
        self.quiet = True
        if threading.current_thread() is threading.main_thread():
            raise BrokenPipeError("stdout pipe closed by reader")
        raise SystemExit(1)


# emoji=False: Rich substitutes :name: emoji shortcodes at render time even
# with markup=False, silently corrupting real-world values (IPv6 groups like
# ':ab:', MAC bytes, any ':word:' string). tlumi never uses shortcodes itself.
console = _TlumiConsole(theme=tlumi_theme, emoji=False)

# Diagnostics (log notices, warnings) go here so they never contaminate the
# stdout data channel used by `state pull`, `output --raw`, and --json modes.
err_console = _TlumiConsole(stderr=True, theme=tlumi_theme, emoji=False)


def print_json(data: str) -> None:
    """Print pre-serialized JSON without Rich line-wrapping or highlighting."""
    console.print(data, soft_wrap=True, highlight=False, markup=False)


_DOTS = ("   ", ".  ", ".. ", "...")
_INSTALL_LOG_LINES = 6  # rolling window size for install live display

# Active window context (set when _run() enters Live display mode)
_window: _WindowContext | None = None


class _AnimatedStatus:
    """Single-line renderable that animates trailing '...' on each Live refresh."""

    def __init__(self, message: str):
        # Strip trailing '...'; the animation will add it back cyclically
        self._base = message.removesuffix("...")

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        phase = int(time.monotonic() * 3) % len(_DOTS)
        # emoji=False: Text.from_markup substitutes :name: shortcodes at
        # construction time, before Console(emoji=False) is ever consulted.
        yield Text.from_markup(f"{self._base}{_DOTS[phase]}", emoji=False)


class _InstallLog:
    """Renderable showing an animated header with a rolling log of install output."""

    def __init__(self, header: str, lines: list[str]):
        self._header = header.removesuffix("...")
        self._lines = lines

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        phase = int(time.monotonic() * 3) % len(_DOTS)
        parts = [f"{self._header}{_DOTS[phase]}"]
        for line in self._lines[-_INSTALL_LOG_LINES:]:
            parts.append(f"    [muted]{escape(line)}[/muted]")
        # emoji=False: install output can echo ':word:' sequences (URLs with
        # ports, hashes); escape() neutralizes [markup] but not :shortcodes:.
        yield Text.from_markup("\n".join(parts), emoji=False)


class _LiveRenderable:
    """Renderable for the Live display region, with optional animated inner content."""

    def __init__(self, inner: RenderableType | None = None):
        self._inner = inner

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        if self._inner:
            if hasattr(self._inner, "__rich_console__"):
                yield from self._inner.__rich_console__(console, options)
            else:
                yield Text(str(self._inner))
            yield Text("\n", style="green")
        else:
            yield Text("", style="green")


class _WindowContext:
    """Manages a Live display region around command output.

    Starts a transient Live display that can host animated inner content
    (status spinners, engine progress). All console.print() calls route
    above the Live region automatically (Rich behavior).
    """

    def __init__(self) -> None:
        self._renderable = _LiveRenderable()
        self._live: Live | None = None

    @property
    def is_started(self) -> bool:
        """Whether the underlying Live display is currently running."""
        return self._live is not None and self._live.is_started

    def __enter__(self) -> _WindowContext:
        global _window
        _window = self
        self._renderable = _LiveRenderable()
        self._live = Live(
            self._renderable,
            console=console,
            refresh_per_second=8,
            transient=True,
        )
        self._live.__enter__()
        # Rich's Live.start() hides the cursor; restore it so prompts and
        # long-running operations show a visible caret.
        console.show_cursor(True)
        return self

    def __exit__(self, *exc) -> None:
        global _window
        _window = None
        assert self._live is not None  # nosec B101
        try:
            self._live.__exit__(*exc)
        finally:
            # transient=True clears the Live region on exit; print a trailing newline
            console.print()

    def set_inner(self, renderable: RenderableType) -> None:
        """Set animated content inside the Live display region."""
        self._renderable = _LiveRenderable(renderable)
        if self._live is not None:
            self._live.update(self._renderable)

    def clear_inner(self) -> None:
        """Remove animated content from the Live display region."""
        self._renderable = _LiveRenderable()
        if self._live is not None:
            self._live.update(self._renderable)

    def pause(self) -> None:
        """Pause Live for interactive terminal input (e.g. confirm prompts).

        With transient=True, stop() erases the live region and moves the cursor
        back to after the last permanent output, exactly where the prompt should appear.
        """
        assert self._live is not None  # nosec B101
        self._live.stop()

    def resume(self) -> None:
        """Resume Live after interactive input."""
        assert self._live is not None  # nosec B101
        self._live.start()
        # Live.start() re-hides the cursor; restore it again. See __enter__.
        console.show_cursor(True)


class WindowLiveProxy:
    """Duck-type proxy that routes Live-like calls through the window context.

    The engine's EventHandler uses self._live.update(), self._live.console.print(),
    and self._live.is_started. This proxy implements the same interface but composes
    the engine's animated log into the window's renderable instead of creating a
    separate Live display.
    """

    def __init__(self, window: _WindowContext):
        self._window = window
        self.console = console

    @property
    def is_started(self) -> bool:
        """Delegate to the window context's started state."""
        return self._window.is_started

    def update(self, renderable: RenderableType) -> None:
        self._window.set_inner(renderable)

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        self._window.clear_inner()


def create_window() -> _WindowContext:
    """Create a Live display region for command output."""
    return _WindowContext()


def _get_active_window() -> _WindowContext | None:
    """Return the active window context, if any."""
    return _window


@contextmanager
def animated_status(message: str, quiet: bool = False):
    """Context manager that shows an animated dots status line.

    The message should end with '...'. The trailing dots will animate.
    When quiet=True (JSON mode), no output is shown.
    When a window is active, composes into the window's Live display.
    """
    if quiet:
        yield
        return
    if _window:
        _window.set_inner(_AnimatedStatus(message))
        try:
            yield
        finally:
            _window.clear_inner()
    else:
        with Live(_AnimatedStatus(message), console=console, refresh_per_second=8):
            yield


@contextmanager
def install_live(header: str):
    """Context manager providing a live-updating display for install output.

    Yields a callback ``add_line(text)`` that appends a line to the rolling log.
    The header animates dots while lines scroll underneath.
    """
    lines: list[str] = []
    renderable = _InstallLog(header, lines)

    if _window:
        _window.set_inner(renderable)
        try:
            yield lines.append
        finally:
            _window.clear_inner()
    else:
        with Live(renderable, console=console, refresh_per_second=8):
            yield lines.append


def print_banner(text: str) -> None:
    console.print(f"\n  [info]{escape(text)}[/info]")


def print_success(text: str) -> None:
    console.print(f"  [success]{escape(text)}[/success]")


def print_warning(text: str) -> None:
    console.print(f"  [warning]{escape(text)}[/warning]")


def print_error(text: str, hint: str | None = None, stream: Console | None = None) -> None:
    out = stream if stream is not None else console
    out.print()
    out.print(f"  [error]Error:[/error] {escape(text)}")
    if hint:
        out.print()
        out.print(f"  [muted]Hint: {escape(hint)}[/muted]")


def print_targets(urns: list[str]) -> None:
    console.print(f"\n  Targeting {len(urns)} resource(s):")
    for urn in urns:
        console.print(f"    [cyan]{escape(display_from_urn(urn))}[/cyan]")


def print_replace_targets(urns: list[str]) -> None:
    console.print(f"\n  Replacing {len(urns)} resource(s):")
    for urn in urns:
        console.print(f"    [replace]{escape(display_from_urn(urn))}[/replace]")


_DIFF_INDENT = "    "

_KIND_SYMBOLS = {
    "add": ("+ ", "bold green"),
    "delete": ("- ", "bold red"),
    "update": ("~ ", "bold yellow"),
}


def _is_multiline(change: PropertyChange) -> bool:
    """Check if either value in a change contains newlines."""
    return (change.old_value is not None and "\n" in change.old_value) or (
        change.new_value is not None and "\n" in change.new_value
    )


def _append_value_lines(text: Text, value: str, style: str) -> None:
    """Append each line of a multi-line value with indentation."""
    for line in value.splitlines():
        text.append(f"\n{_DIFF_INDENT}{line}", style=style)


def _build_diff_text_inline(change: PropertyChange, max_path: int) -> Text:
    """Build a styled Text for a single-line property change."""
    padded = f"{change.path:<{max_path}}"
    text = Text()

    if change.kind == "add":
        text.append("+ ", style="bold green")
        text.append(f"{padded}  ", style="dim")
        text.append(change.new_value or "", style="green")
    elif change.kind == "delete":
        text.append("- ", style="bold red")
        text.append(f"{padded}  ", style="dim")
        text.append(change.old_value or "", style="red strike")
    else:  # update
        text.append("~ ", style="bold yellow")
        text.append(f"{padded}  ", style="dim")
        text.append(change.old_value or "", style="red strike")
        text.append(" → ", style="dim")
        text.append(change.new_value or "", style="green bold")

    if change.forces_replacement:
        text.append("  (forces replacement)", style="cyan")

    return text


def _build_diff_text_expanded(change: PropertyChange) -> Text:
    """Build a styled Text for a multi-line property change."""
    symbol, symbol_style = _KIND_SYMBOLS.get(change.kind, ("~ ", "bold yellow"))
    text = Text()
    text.append(symbol, style=symbol_style)
    text.append(change.path, style="dim")

    if change.kind == "add":
        _append_value_lines(text, change.new_value or "", "green")
    elif change.kind == "delete":
        _append_value_lines(text, change.old_value or "", "red strike")
    else:  # update
        if change.old_value:
            _append_value_lines(text, change.old_value, "red strike")
        text.append(f"\n{_DIFF_INDENT}→ ", style="dim")
        if change.new_value:
            # First line of new value goes after the arrow
            new_lines = (change.new_value or "").splitlines()
            if new_lines:
                text.append(new_lines[0], style="green bold")
                for line in new_lines[1:]:
                    text.append(f"\n{_DIFF_INDENT}{line}", style="green bold")

    if change.forces_replacement:
        text.append(f"\n{_DIFF_INDENT}(forces replacement)", style="cyan")

    return text


def _build_diff_text(change: PropertyChange, max_path: int) -> Text:
    """Build a styled Text object for a single property change.

    Dispatches to inline or expanded rendering based on whether
    values contain newlines.
    """
    if _is_multiline(change):
        return _build_diff_text_expanded(change)
    return _build_diff_text_inline(change, max_path)


def format_diff_tree(
    header: str, changes: Sequence[PropertyChange], guide_style: str = "dim"
) -> Padding:
    """Build a Rich Tree showing a resource header with property-level diffs.

    The header is Rich markup for the resource line. Changes are PropertyChange
    objects rendered as tree children with strikethrough old values, bold new
    values, and tree-connector grouping. Returns a Padding-wrapped Tree indented
    to match the 2-space resource line indent.
    """
    tree = Tree(header, guide_style=guide_style)

    if changes:
        max_path = max(len(c.path) for c in changes)
        for c in changes:
            tree.add(_build_diff_text(c, max_path))

    return Padding(tree, (0, 0, 0, 2))


def extract_changes(change_summary: Mapping[Any, int]) -> tuple[int, int, int, int]:
    """Extract (create, update, replace, delete) counts from a Pulumi change summary."""
    return (
        change_summary.get("create", 0),
        change_summary.get("update", 0),
        change_summary.get("replace", 0),
        change_summary.get("delete", 0),
    )


def format_engine_result(
    resource_changes: Mapping[Any, int],
    duration: str,
    targets: list[str] | None = None,
    outputs: Mapping[str, object] | None = None,
    callback_errors: int = 0,
    warnings: Sequence[tuple[str, str]] | None = None,
) -> dict[str, object]:
    """Build a JSON-serializable dict for engine operation results.

    Used by apply, destroy, and refresh in --json mode. ``callback_errors``
    is the count of exceptions raised from on_preview/on_update callbacks.
    Surfacing it in the envelope means a diff-extraction or rendering
    crash during --json is no longer invisible to JSON consumers.
    ``warnings`` are (resource, message) pairs from provider/program warnings.
    """
    result: dict = {
        "changes": {
            "create": resource_changes.get("create", 0),
            "update": resource_changes.get("update", 0),
            "replace": resource_changes.get("replace", 0),
            "delete": resource_changes.get("delete", 0),
            "import": resource_changes.get("import", 0),
        },
        "duration": duration,
    }
    if targets:
        result["targets"] = targets
    if outputs is not None:
        result["outputs"] = outputs
    if callback_errors:
        result["callback_errors"] = callback_errors
    if warnings:
        result["warnings"] = [{"resource": r, "message": m} for r, m in warnings]
    return result


def print_plan_summary(
    create: int = 0,
    update: int = 0,
    replace: int = 0,
    delete: int = 0,
    import_count: int = 0,
    outputs_changed: bool = False,
) -> None:
    parts = []
    if import_count:
        parts.append(f"[create]{import_count} to import[/create]")
    if create:
        parts.append(f"[create]{create} to add[/create]")
    if update:
        parts.append(f"[update]{update} to change[/update]")
    if replace:
        parts.append(f"[replace]{replace} to replace[/replace]")
    if delete:
        parts.append(f"[delete]{delete} to destroy[/delete]")

    if parts:
        console.print(f"\n  Plan: {', '.join(parts)}.")
    elif outputs_changed:
        # Output-only changes: no resource ops, but pulumi.export() values
        # will change on apply.
        console.print("\n  Plan: [update]outputs will change[/update].")
    else:
        console.print("\n  [muted]No changes. Infrastructure is up-to-date.[/muted]")


def print_apply_summary(
    create: int = 0,
    update: int = 0,
    replace: int = 0,
    delete: int = 0,
    duration: str = "",
    import_count: int = 0,
) -> None:
    parts = []
    if import_count:
        parts.append(f"[create]{import_count} imported[/create]")
    if create:
        parts.append(f"[create]{create} created[/create]")
    if update:
        parts.append(f"[update]{update} updated[/update]")
    if replace:
        parts.append(f"[replace]{replace} replaced[/replace]")
    if delete:
        parts.append(f"[delete]{delete} destroyed[/delete]")

    if parts:
        console.print("\n  Resources:")
        for part in parts:
            console.print(f"    {part}")
    else:
        # A zero-change apply (e.g. --auto-approve with nothing pending, or an
        # output-only change) must not print a dangling empty "Resources:" header.
        console.print("\n  [muted]No resources changed.[/muted]")

    if duration:
        console.print(f"\n  [timing]Duration: {duration}[/timing]")


_STACK_RESOURCE_TYPE = "pulumi:pulumi:Stack"


def masked_outputs_from_state(state: Deployment) -> dict[str, object]:
    """Extract stack outputs from exported state with every secret masked.

    The Automation API's OutputValue.secret flag only marks whole-output
    secrets: a composite output (a dict/list containing a nested secret)
    reports secret=False and carries fully decrypted plaintext with no
    sentinels, so default-masked display must never trust stack.outputs().
    Exported state keeps Pulumi's secret signature wrapper on every secret
    node (top-level and nested), which _sanitize_value() replaces with
    '(sensitive)' while preserving non-secret siblings.
    """
    deployment = state.deployment or {}
    resources = deployment.get("resources") or []
    if not isinstance(resources, list):
        return {}
    for resource in resources:
        if isinstance(resource, dict) and resource.get("type") == _STACK_RESOURCE_TYPE:
            outputs = resource.get("outputs")
            if not isinstance(outputs, dict):
                return {}
            sanitized = _sanitize_value(outputs)
            return sanitized if isinstance(sanitized, dict) else {}
    return {}


def print_outputs(outputs: Mapping[str, object]) -> None:
    """Print stack outputs as an aligned 'key = value' listing.

    Values must already be masked by the caller: default-masked paths pass
    sanitized values from masked_outputs_from_state(); --show-secrets paths
    pass plaintext values from stack.outputs().
    """
    if not outputs:
        console.print("  [muted]No outputs.[/muted]")
        return

    console.print("\n  Outputs:")
    max_key = max(len(k) for k in outputs)
    for key, value in sorted(outputs.items()):
        console.print(f"    {key:<{max_key}}  = {value!r}", highlight=False, markup=False)


def print_created_files(files: list[str]) -> None:
    console.print("  Created:")
    for f in files:
        console.print(f"    [success]{f}[/success]")


def prompt(message: str, default: str = "") -> str:
    """Prompt for free-form input, pausing the Live window during input.

    Same window pause/resume discipline as confirm(): without it the Live
    refresh thread emits clear-line sequences over the (newline-less) prompt and
    erases the question ~125ms after it appears. On EOF (piped stdin) returns the
    provided default rather than raising -- callers use this for optional setup
    questions that have a safe default, not destructive confirmations.
    """
    if _window:
        _window.pause()
    try:
        try:
            return console.input(message)
        except EOFError:
            return default
    finally:
        if _window:
            _window.resume()


def confirm(message: str) -> bool:
    """Prompt for confirmation, requiring the full word 'yes'.

    Pauses the Live display during input to prevent the refresh thread
    from overwriting the prompt. On EOF (piped stdin with no answer),
    raise so a scripted destructive operation fails loudly instead of
    silently cancelling: JSON mode already requires ``--auto-approve``
    for the same reason, and silent cancellation under a pipe makes
    scripted tooling indistinguishable from a successful no-op.
    """
    from tlumi.errors import TlumiError

    if _window:
        _window.pause()
    try:
        try:
            response = console.input(f"  {message} Type [bold]'yes'[/bold] to confirm: ")
        except EOFError as e:
            raise TlumiError(
                "Cannot prompt for confirmation: stdin is not a terminal.",
                hint="Pass '--auto-approve' to skip the prompt in scripted use.",
            ) from e
    finally:
        if _window:
            _window.resume()
    return response.strip().lower() == "yes"
