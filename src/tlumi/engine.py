"""Pulumi engine event handling for tlumi.

Provides structured event processing instead of parsing stdout strings.
Used by plan, apply, destroy, and refresh via ``on_event`` callbacks. The
import command does NOT use on_event (Pulumi's ``import_resources`` does
not accept it); import still routes its ``CommandError`` through
``catch_engine_errors`` so failures land as structured ``EngineError``.
"""

from __future__ import annotations

import contextlib
import logging
import sys
import time
from collections import deque
from contextlib import AbstractContextManager, contextmanager, nullcontext
from typing import NamedTuple

from pulumi.automation import CommandError, EngineEvent, OpType
from rich.console import Console, ConsoleOptions, RenderResult
from rich.live import Live
from rich.markup import escape
from rich.text import Text

from tlumi.diffs import PropertyChange, extract_property_diffs
from tlumi.display import LiveDisplay, WindowLiveProxy, _get_active_window, console
from tlumi.errors import Diagnostic, EngineError
from tlumi.redact import redact_text
from tlumi.resolve import display_from_urn, name_from_urn
from tlumi.sanitize import _MAX_DEPTH, _PULUMI_SECRET_SIG, _PULUMI_SECRET_VALUE

_log = logging.getLogger(__name__)

# Hint printed by plan/apply when nothing is pending but the Stack outputs
# contain secrets: preview scrubs secret output values to the identical
# wrapper on both the old and new side, so a changed secret export value is
# invisible to the outputs comparison (see stack_outputs_changed).
SECRET_OUTPUTS_BLIND_HINT = (  # nosec B105 - user-facing hint text, not a credential
    "Note: secret output values cannot be compared in preview."
    " If a secret export changed, apply it with 'tlumi apply --auto-approve'."
)


def _contains_secret_wrapper(value: object, _depth: int = 0) -> bool:
    """True when a Pulumi secret-sig wrapper appears anywhere in ``value``.

    Preview events scrub every secret output value to the identical wrapper
    dict on BOTH the old and new side (the Automation API's ``preview()``
    cannot request unscrubbed secrets), so an equality comparison of Stack
    outputs is blind to changed secret values. Callers use this to know the
    comparison could not see secret changes. Returns True at the depth limit
    as the conservative default: a spurious hint is harmless, while a missed
    one asserts "up-to-date" over a pending secret change.
    """
    if _depth >= _MAX_DEPTH:
        return True
    if isinstance(value, dict):
        if value.get(_PULUMI_SECRET_SIG) == _PULUMI_SECRET_VALUE:
            return True
        return any(_contains_secret_wrapper(v, _depth + 1) for v in value.values())
    if isinstance(value, list):
        return any(_contains_secret_wrapper(v, _depth + 1) for v in value)
    if isinstance(value, str):
        # Defense in depth, mirroring sanitize.py: the sig may survive inside
        # a leaf string (embedded JSON) even when the wrapper-dict form is lost.
        return _PULUMI_SECRET_SIG in value
    return False


class _OpDisplay(NamedTuple):
    symbol: str
    in_progress: str
    past_tense: str
    future: str


# Map Pulumi operation types to display properties
_OP_DISPLAY: dict[OpType, _OpDisplay] = {
    OpType.CREATE: _OpDisplay(
        "[bold green]+[/bold green]",
        "creating",
        "created",
        "will be [bold green]created[/bold green]",
    ),
    OpType.UPDATE: _OpDisplay(
        "[bold yellow]~[/bold yellow]",
        "updating",
        "updated",
        "will be [bold yellow]updated[/bold yellow]",
    ),
    OpType.DELETE: _OpDisplay(
        "[bold red]-[/bold red]",
        "deleting",
        "deleted",
        "will be [bold red]destroyed[/bold red]",
    ),
    OpType.REPLACE: _OpDisplay(
        "[bold cyan]-/+[/bold cyan]",
        "replacing",
        "replaced",
        "will be [bold cyan]replaced[/bold cyan]",
    ),
    OpType.CREATE_REPLACEMENT: _OpDisplay(
        "[bold cyan]+[/bold cyan]",
        "creating",
        "created",
        "will be [bold cyan]created[/bold cyan] (replacement)",
    ),
    OpType.DELETE_REPLACED: _OpDisplay(
        "[bold cyan]-[/bold cyan]",
        "deleting",
        "deleted",
        "will be [bold cyan]deleted[/bold cyan] (replaced)",
    ),
    OpType.REFRESH: _OpDisplay(
        "[bold blue]~[/bold blue]",
        "refreshing",
        "refreshed",
        "will be [bold blue]refreshed[/bold blue]",
    ),
    OpType.IMPORT: _OpDisplay(
        "[bold green]=[/bold green]",
        "importing",
        "imported",
        "will be [bold green]imported[/bold green]",
    ),
    OpType.IMPORT_REPLACEMENT: _OpDisplay(
        "[bold cyan]=[/bold cyan]",
        "importing",
        "imported",
        "will be [bold cyan]imported[/bold cyan] (replacement)",
    ),
}

_LOG_WINDOW_SIZE = 5
_DOTS = ("   ", ".  ", ".. ", "...")

# Map a Pulumi OpType to its change-summary key (for excluding the hidden Stack).
# OpType.REFRESH is deliberately absent: during `tlumi refresh` the engine emits
# REFRESH only on resource_pre_events (which drive the progress display, not the
# counts), while the res_outputs_events that _count_op consumes carry the refresh
# OUTCOME op (SAME for no drift, UPDATE/DELETE for drift), verified against a live
# stack. Mapping REFRESH here would count nothing today and could over-count if a
# future engine emitted REFRESH outcome ops for unchanged resources.
_OP_SUMMARY_KEY: dict[OpType, str] = {
    OpType.CREATE: "create",
    OpType.UPDATE: "update",
    OpType.REPLACE: "replace",
    OpType.DELETE: "delete",
    OpType.IMPORT: "import",
    OpType.IMPORT_REPLACEMENT: "import",
}


@contextmanager
def redirect_program_stdout(active: bool):
    """Redirect the user program's stdout to stderr while ``active``.

    infra.py runs as an in-process inline program, so a ``print()`` in the
    user's code writes to tlumi's stdout during preview/up/destroy/refresh. In
    --json mode that corrupts the machine-readable envelope, so route program
    stdout to stderr for the duration of the engine operation. tlumi's own JSON
    is written after the context exits, to the real stdout.
    """
    if not active:
        yield
        return
    with contextlib.redirect_stdout(sys.stderr):
        yield


# Guide colors for diff trees (match the operation symbol color)
_DIFF_GUIDE_STYLE = {
    OpType.CREATE: "dim green",
    OpType.UPDATE: "dim yellow",
    OpType.REPLACE: "dim cyan",
    OpType.DELETE: "dim red",
}


class _AnimatedLog:
    """Renderable that animates the in-progress marker's '...' on each Live refresh.

    Each line carries an ``animate`` flag: only the in-progress marker line
    (``creating...``) has its trailing ``...`` swapped for the spinner. Raw
    provider diagnostic lines that happen to contain a literal ``...``
    (``Waiting for operation...``) are rendered verbatim.
    """

    def __init__(self, lines: list[tuple[str, bool]]):
        self._lines = lines

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        phase = int(time.monotonic() * 3) % len(_DOTS)
        dots = _DOTS[phase]
        markup = "\n".join(
            ln.replace("...", dots) if animate else ln for ln, animate in self._lines
        )
        # emoji=False: Text.from_markup substitutes :name: shortcodes at
        # construction time regardless of the console's emoji setting, which
        # would corrupt provider data containing :word: sequences (IPv6
        # groups like :ab:, MAC bytes). escape() does not neutralize these.
        yield Text.from_markup(markup, emoji=False)


@contextmanager
def catch_engine_errors(handler: EventHandler, message: str):
    """Wrap a Pulumi operation to convert CommandError into EngineError.

    Both the per-resource diagnostics (populated by on_update) and the
    full Pulumi stderr go through redact_text before they reach the
    EngineError. Provider failures routinely echo credentials in error
    messages (AWS keys, connection strings, bearer tokens), and the
    EngineError is later printed to the console and emitted in JSON.
    """
    try:
        yield
    except CommandError as e:
        raise EngineError(
            message,
            diagnostics=handler.errors,
            full_output=redact_text(str(e)),
        ) from None


class EventHandler:
    """Collects and displays Pulumi engine events.

    Callbacks are invoked from Pulumi's event-stream thread. In practice,
    all callbacks execute sequentially on Pulumi's single event-stream thread.
    Under CPython, the GIL ensures attribute mutations (list.append,
    dict.__setitem__) are atomic, and all collected state (errors, etc.) is
    only read after the operation completes, so no additional locking is
    needed. If free-threaded Python (PEP 703, opt-in from 3.13+) is ever
    supported, revisit with explicit locks.
    """

    def __init__(self, quiet: bool = False):
        self.quiet = quiet
        self.errors: list[Diagnostic] = []
        self.warnings: list[Diagnostic] = []
        self.property_diffs: dict[str, list[PropertyChange]] = {}
        self._start_times: dict[str, int] = {}
        self._active_resources: dict[str, str] = {}  # urn -> name
        self._active_display: dict[str, str] = {}  # urn -> markup line for in-progress
        self._last_started_urn: str = ""
        self._live: LiveDisplay | None = None
        self._closed: bool = False  # set once the live session exits; drops late events
        self._resource_logs: dict[str, deque[str]] = {}  # urn -> rolling log window
        self._preview_last_had_diffs: bool = False
        self._callback_errors: int = 0
        # Set during preview when the pulumi:pulumi:Stack resource's outputs
        # differ between old and new state. Output-only changes (editing a
        # pulumi.export() value) emit SAME for every resource, so op counts
        # alone cannot detect them; commands gate "No changes" on this too.
        self._stack_outputs_changed: bool = False
        # Set during preview when either side of the Stack outputs comparison
        # contains a secret-sig wrapper: the comparison above is blind to
        # changed secret values (both sides arrive scrubbed to the identical
        # wrapper), so commands print SECRET_OUTPUTS_BLIND_HINT instead of
        # asserting up-to-date when nothing else is pending.
        self._stack_outputs_contain_secrets: bool = False
        # Per-op count of the resources the engine actually displays (non-Stack,
        # non-SAME, replacements counted once). Pulumi's change_summary is an
        # unreliable basis for the user-facing summary: it counts the hidden
        # pulumi:pulumi:Stack resource (e.g. "1 to add" for an empty program) but
        # not default providers, so its totals do not match tlumi's resource
        # listing. Counting the displayed events instead keeps the summary and
        # the listing consistent by construction.
        self.op_counts: dict[str, int] = {}

    @property
    def callback_errors(self) -> int:
        """Number of errors encountered in on_preview/on_update callbacks."""
        return self._callback_errors

    @property
    def stack_outputs_changed(self) -> bool:
        """True when preview saw the Stack resource's outputs change.

        This is the signal for output-only changes: Pulumi previews an
        edited ``pulumi.export()`` value as SAME ops everywhere (even
        change_summary reports only ``same``), while the final Stack
        res_outputs_event carries the pending outputs in ``new.outputs``.
        Verified against a live stack; see tests for the event shapes.

        Blind spot: secret output values are scrubbed to the identical
        secret-sig wrapper on BOTH the old and new side of the event (the
        Automation API's ``preview()`` cannot request unscrubbed secrets),
        so a changed secret export value with zero resource changes leaves
        this flag False. ``stack_outputs_contain_secrets`` reports when the
        comparison was blind; applying such a change requires
        ``apply --auto-approve`` (which skips the preview gate).
        """
        return self._stack_outputs_changed

    @property
    def stack_outputs_contain_secrets(self) -> bool:
        """True when preview saw a secret-sig wrapper in the Stack outputs.

        Signals that the ``stack_outputs_changed`` comparison was blind to
        secret values; commands use it to print SECRET_OUTPUTS_BLIND_HINT
        alongside "No changes" instead of flatly asserting up-to-date.
        """
        return self._stack_outputs_contain_secrets

    def _count_op(self, op: OpType) -> None:
        """Tally a displayed resource operation for the user-facing summary."""
        key = _OP_SUMMARY_KEY.get(op)
        if key:
            self.op_counts[key] = self.op_counts.get(key, 0) + 1

    def change_counts(self) -> dict[str, int]:
        """Return the per-op counts of displayed resources (matches the listing)."""
        return dict(self.op_counts)

    def start_live(self, quiet: bool = False) -> AbstractContextManager:
        """Start a Live display for rolling log output. Caller must use as context manager.

        When a window is active, returns a proxy that composes the engine's
        animated log into the window's Live display instead of creating a
        separate one (Rich doesn't support nested Live contexts).

        The returned context manager also bounds the engine session: on exit it
        marks the handler closed so any late callbacks (the Pulumi gRPC event
        servicer can deliver events after up()/destroy()/refresh() return) are
        dropped instead of re-injecting ghost content or printing after the
        summary.
        """
        self._closed = False
        if quiet:
            return self._session(nullcontext())
        window = _get_active_window()
        if window:
            proxy = WindowLiveProxy(window)
            self._live = proxy
            return self._session(proxy)
        live = Live("", console=console, refresh_per_second=8, transient=True)
        self._live = live
        return self._session(live)

    @contextmanager
    def _session(self, inner: AbstractContextManager):
        """Wrap the live context so the engine stops touching the display on exit."""
        try:
            with inner:
                yield inner
        finally:
            self._closed = True
            self._live = None

    def render_warnings(self) -> None:
        """Print collected provider/program warnings after an operation.

        Warnings are gathered during preview/update (pulumi.log.warn, provider
        deprecations, engine warnings) and surfaced here so they are not
        silently dropped. No-op in quiet/JSON mode (commands include them in the
        JSON envelope instead).
        """
        if self.quiet or not self.warnings:
            return
        from tlumi.display import print_warning

        for resource, msg in self.warnings:
            print_warning(f"{resource}: {msg}" if resource else msg)

    def _print(self, markup: str) -> None:
        """Print a line, routing through Live if active."""
        if self.quiet:
            return
        if self._live and self._live.is_started:
            self._live.console.print(markup, highlight=False)
        else:
            console.print(markup, highlight=False)

    def _update_log_window(self, line: str, urn: str = "") -> None:
        """Add a diagnostic line to the rolling log window for a resource."""
        target = urn if urn in self._active_resources else ""
        if target:
            if target not in self._resource_logs:
                self._resource_logs[target] = deque(maxlen=_LOG_WINDOW_SIZE)
            self._resource_logs[target].append(line)
        self._refresh_live()

    def _refresh_live(self) -> None:
        """Refresh the live display: each active resource followed by its log lines."""
        if not (self._live and self._live.is_started):
            return
        # (markup, animate): only the in-progress marker animates its '...';
        # raw provider log lines render verbatim so a literal '...' in a
        # diagnostic is not clobbered by the spinner.
        lines: list[tuple[str, bool]] = []
        for urn in list(self._active_resources):
            if urn in self._active_display:
                lines.append((self._active_display[urn], True))
            if urn in self._resource_logs:
                lines.extend((ln, False) for ln in self._resource_logs[urn])
        if lines:
            self._live.update(_AnimatedLog(lines))
        else:
            self._live.update("")

    def on_preview(self, event: EngineEvent) -> None:
        """Display planned resource operations during preview.

        For replacements, Pulumi emits three events per resource:
        CREATE_REPLACEMENT, REPLACE, and DELETE_REPLACED. We display
        only the REPLACE event (the canonical entry) and skip the
        other two to avoid duplicate output, matching Terraform's
        single-line-per-resource convention.

        Wrapped in a broad exception guard so that a rendering or diff
        extraction bug does not abort the entire Pulumi operation.
        """
        if self._closed:
            return
        try:
            self._on_preview(event)
        except Exception:
            self._callback_errors += 1
            _log.warning("on_preview callback error", exc_info=True)

    def _collect_diagnostic(self, diag) -> None:
        """Collect error/warning diagnostics into handler state.

        Shared by preview and update so a failing preview surfaces real error
        detail (not a bare "Preview failed.") and warnings are never dropped.
        Messages are redacted: provider errors routinely echo credentials.
        """
        msg = diag.message.strip()
        if not msg:
            return
        resource = display_from_urn(diag.urn) if diag.urn else ""
        if diag.severity == "error":
            if msg != "update failed":
                self.errors.append(Diagnostic(resource, redact_text(msg)))
        elif diag.severity == "warning":
            self.warnings.append(Diagnostic(resource, redact_text(msg)))

    def _on_preview(self, event: EngineEvent) -> None:
        if event.diagnostic_event:
            self._collect_diagnostic(event.diagnostic_event)
        if event.res_outputs_event:
            meta = event.res_outputs_event.metadata
            if meta.type == "pulumi:pulumi:Stack":
                old_outputs = (getattr(meta.old, "outputs", None) if meta.old else None) or {}
                new_outputs = (getattr(meta.new, "outputs", None) if meta.new else None) or {}
                if new_outputs != old_outputs:
                    self._stack_outputs_changed = True
                if _contains_secret_wrapper(old_outputs) or _contains_secret_wrapper(new_outputs):
                    self._stack_outputs_contain_secrets = True
        if event.resource_pre_event:
            meta = event.resource_pre_event.metadata
            if meta.type == "pulumi:pulumi:Stack" or meta.op == OpType.SAME:
                return

            # Skip replacement sub-steps; REPLACE covers both
            if meta.op in (OpType.CREATE_REPLACEMENT, OpType.DELETE_REPLACED):
                return

            display = _OP_DISPLAY.get(meta.op)
            if display:
                # Count this displayed resource so the plan summary matches the
                # listing (done even in quiet/JSON mode).
                self._count_op(meta.op)

                # Extract property-level diffs (needed for JSON output even when quiet)
                changes = extract_property_diffs(meta)
                if changes:
                    self.property_diffs[meta.urn] = changes

                if self.quiet:
                    return

                symbol, _, _, action = display
                name = escape(name_from_urn(meta.urn))
                etype = escape(meta.type)
                header = f"{symbol} [bold]{etype}[/bold]  [cyan]{name}[/cyan]  {action}"

                # Blank line separator only after diff trees (not between plain lines)
                if self._preview_last_had_diffs:
                    console.print()

                if changes:
                    from tlumi.display import format_diff_tree

                    guide = _DIFF_GUIDE_STYLE.get(meta.op, "dim")
                    console.print(
                        format_diff_tree(header, changes, guide_style=guide),
                        highlight=False,
                    )
                    self._preview_last_had_diffs = True
                else:
                    console.print(f"  {header}", highlight=False)
                    self._preview_last_had_diffs = False

    def on_update(self, event: EngineEvent) -> None:
        """Display resource progress during apply/destroy and collect errors.

        Wrapped in a broad exception guard so that a rendering bug
        does not abort the entire Pulumi operation.
        """
        if self._closed:
            return
        try:
            self._on_update(event)
        except Exception:
            self._callback_errors += 1
            _log.warning("on_update callback error", exc_info=True)

    def _on_update(self, event: EngineEvent) -> None:
        # Show in-progress state when operations start
        if event.resource_pre_event:
            meta = event.resource_pre_event.metadata
            self._start_times[meta.urn] = event.timestamp
            self._last_started_urn = meta.urn
            if meta.type != "pulumi:pulumi:Stack" and meta.op != OpType.SAME:
                self._active_resources[meta.urn] = name_from_urn(meta.urn)
                display = _OP_DISPLAY.get(meta.op)
                if display:
                    symbol, in_progress, _, _ = display
                    name = escape(name_from_urn(meta.urn))
                    etype = escape(meta.type)
                    line = f"  {symbol} [bold]{etype}[/bold]  [cyan]{name}[/cyan]  {in_progress}..."
                    self._active_display[meta.urn] = line
                    self._refresh_live()

        # A resource operation that fails emits res_op_failed_event INSTEAD of
        # res_outputs_event. Without handling it the resource would stay stuck
        # animating "creating..." and later log lines would be misattributed to
        # its still-active URN.
        if event.res_op_failed_event:
            meta = event.res_op_failed_event.metadata
            self._active_resources.pop(meta.urn, None)
            self._active_display.pop(meta.urn, None)
            self._resource_logs.pop(meta.urn, None)
            if self._last_started_urn == meta.urn:
                self._last_started_urn = ""
            self._refresh_live()
            if meta.type != "pulumi:pulumi:Stack":
                name = escape(name_from_urn(meta.urn))
                etype = escape(meta.type)
                self._print(
                    f"  [bold red]x[/bold red] [bold]{etype}[/bold]  [cyan]{name}[/cyan]  "
                    f"[error]failed[/error]"
                )

        # Display completed resources
        if event.res_outputs_event:
            meta = event.res_outputs_event.metadata
            self._active_resources.pop(meta.urn, None)
            self._active_display.pop(meta.urn, None)
            if meta.type == "pulumi:pulumi:Stack" or meta.op == OpType.SAME:
                return
            # Replacement emits CREATE_REPLACEMENT + REPLACE + DELETE_REPLACED;
            # count only the canonical REPLACE so the summary isn't inflated.
            if meta.op not in (OpType.CREATE_REPLACEMENT, OpType.DELETE_REPLACED):
                self._count_op(meta.op)
            display = _OP_DISPLAY.get(meta.op)
            if display:
                symbol, _, past_tense, _ = display
                name = escape(name_from_urn(meta.urn))
                etype = escape(meta.type)
                start = self._start_times.get(meta.urn)
                timing = ""
                if start and event.timestamp > start:
                    timing = f"  [dim]({event.timestamp - start}s)[/dim]"
                self._resource_logs.pop(meta.urn, None)
                self._refresh_live()
                self._print(
                    f"  {symbol} [bold]{etype}[/bold]  [cyan]{name}[/cyan]  {past_tense}{timing}"
                )

        # Display and collect diagnostics
        if event.diagnostic_event:
            diag = event.diagnostic_event
            msg = diag.message.strip()
            if not msg:
                return
            if diag.severity in ("error", "warning"):
                # Collected (redacted) into self.errors / self.warnings; surfaced
                # after the operation. Provider messages routinely echo creds.
                self._collect_diagnostic(diag)
            elif diag.severity in ("info", "info#err"):
                # Attribute diagnostic lines to a resource if possible
                attr_urn = ""
                if diag.urn and diag.urn in self._active_resources:
                    attr_urn = diag.urn
                elif self._last_started_urn in self._active_resources:
                    attr_urn = self._last_started_urn
                prefix = f"{self._active_resources.get(attr_urn, '')}: " if attr_urn else ""
                for line in msg.splitlines():
                    line = line.strip()
                    if line:
                        self._update_log_window(
                            f"    [dim]{escape(prefix)}{escape(redact_text(line))}[/dim]",
                            urn=attr_urn,
                        )
