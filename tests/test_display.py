"""Tests for tlumi.display: extract_changes, confirm, format_diff_tree."""

from __future__ import annotations

import json
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from tlumi.diffs import PropertyChange
from tlumi.display import (
    _AnimatedStatus,
    _build_diff_text,
    _InstallLog,
    _TlumiConsole,
    confirm,
    console,
    err_console,
    extract_changes,
    format_diff_tree,
    format_engine_result,
    masked_outputs_from_state,
    print_apply_summary,
    print_json,
    print_outputs,
    print_plan_summary,
    prompt,
)


def _render(renderable) -> str:
    """Render a Rich renderable to plain text (no ANSI codes)."""
    buf = StringIO()
    console = Console(file=buf, force_terminal=True, no_color=True, width=120)
    console.print(renderable, highlight=False)
    return buf.getvalue()


def test_extract_changes_all_present():
    summary = {"create": 2, "update": 1, "replace": 4, "delete": 3}
    assert extract_changes(summary) == (2, 1, 4, 3)


def test_extract_changes_partial():
    summary = {"create": 5}
    assert extract_changes(summary) == (5, 0, 0, 0)


def test_extract_changes_empty():
    assert extract_changes({}) == (0, 0, 0, 0)


def test_extract_changes_zero_values():
    summary = {"create": 0, "update": 0, "replace": 0, "delete": 0}
    assert extract_changes(summary) == (0, 0, 0, 0)


def test_extract_changes_extra_keys_ignored():
    summary = {"create": 1, "same": 10, "update": 0, "delete": 2}
    assert extract_changes(summary) == (1, 0, 0, 2)


def test_extract_changes_with_replace():
    summary = {"create": 2, "update": 1, "replace": 5, "delete": 0}
    assert extract_changes(summary) == (2, 1, 5, 0)


# ---------------------------------------------------------------------------
# confirm
# ---------------------------------------------------------------------------


@patch("tlumi.display.console")
def test_confirm_accepts_yes(mock_console):
    mock_console.input.return_value = "yes"
    assert confirm("Proceed?") is True


@patch("tlumi.display.console")
def test_confirm_rejects_y(mock_console):
    mock_console.input.return_value = "y"
    assert confirm("Proceed?") is False


@patch("tlumi.display.console")
def test_confirm_rejects_no(mock_console):
    mock_console.input.return_value = "no"
    assert confirm("Proceed?") is False


@patch("tlumi.display.console")
def test_confirm_case_insensitive(mock_console):
    mock_console.input.return_value = "YES"
    assert confirm("Proceed?") is True


@patch("tlumi.display.console")
def test_confirm_rejects_empty(mock_console):
    mock_console.input.return_value = ""
    assert confirm("Proceed?") is False


@patch("tlumi.display.console")
def test_confirm_disables_emoji_shortcodes(mock_console):
    """confirm() must render its prompt with emoji=False.

    Console.input hard-defaults emoji=True, which would rewrite :shortcode:
    sequences in a resource name (e.g. 'cache:wave:node') to an emoji in a
    destructive confirmation, bypassing the console-level emoji=False invariant.
    """
    mock_console.input.return_value = "yes"
    confirm("Remove aws:s3:Bucket (cache:wave:node) from state?")
    assert mock_console.input.call_args.kwargs.get("emoji") is False


@patch("tlumi.display.console")
def test_prompt_disables_emoji_shortcodes(mock_console):
    """prompt() must render its prompt with emoji=False (same invariant as confirm())."""
    mock_console.input.return_value = "answer"
    prompt("Enter :wave: value:")
    assert mock_console.input.call_args.kwargs.get("emoji") is False


# ---------------------------------------------------------------------------
# format_engine_result
# ---------------------------------------------------------------------------


def test_format_engine_result_basic():
    result = format_engine_result({"create": 1, "update": 0, "delete": 2}, "5s")
    assert result == {
        "changes": {"create": 1, "update": 0, "replace": 0, "delete": 2, "import": 0},
        "duration": "5s",
    }


def test_format_engine_result_includes_replace():
    result = format_engine_result({"create": 1, "replace": 3, "delete": 0}, "2s")
    assert result["changes"]["replace"] == 3


def test_format_engine_result_with_targets():
    result = format_engine_result(
        {"create": 1},
        "3s",
        targets=["urn:pulumi:default::p::t::n"],
    )
    assert result["targets"] == ["urn:pulumi:default::p::t::n"]


def test_format_engine_result_with_outputs():
    result = format_engine_result(
        {},
        "1s",
        outputs={"bucket": "my-bucket"},
    )
    assert result["outputs"] == {"bucket": "my-bucket"}


def test_format_engine_result_without_optional():
    result = format_engine_result({}, "0s")
    assert "targets" not in result
    assert "outputs" not in result


# ---------------------------------------------------------------------------
# _build_diff_text
# ---------------------------------------------------------------------------


def test_build_diff_text_add():
    c = PropertyChange.add("tags.team", '"platform"')
    text = _build_diff_text(c, max_path=9)
    plain = text.plain
    assert plain.startswith("+ ")
    assert "tags.team" in plain
    assert '"platform"' in plain


def test_build_diff_text_update():
    c = PropertyChange.update("sku.name", '"Basic"', '"Standard"')
    text = _build_diff_text(c, max_path=8)
    plain = text.plain
    assert plain.startswith("~ ")
    assert '"Basic"' in plain
    assert "→" in plain
    assert '"Standard"' in plain


def test_build_diff_text_delete():
    c = PropertyChange.delete("tags.old", '"gone"')
    text = _build_diff_text(c, max_path=8)
    plain = text.plain
    assert plain.startswith("- ")
    assert '"gone"' in plain


def test_build_diff_text_forces_replacement():
    c = PropertyChange.update(
        "location",
        '"uksouth"',
        '"westeurope"',
        forces_replacement=True,
    )
    text = _build_diff_text(c, max_path=8)
    assert "(forces replacement)" in text.plain


def test_build_diff_text_strikethrough_on_old():
    """Old values in update/delete get strikethrough styling."""
    c = PropertyChange.update("x", '"old"', '"new"')
    text = _build_diff_text(c, max_path=1)
    # Find the span covering the old value and verify it has 'strike'
    old_start = text.plain.index('"old"')
    old_end = old_start + len('"old"')
    styles = [span for span in text._spans if span.start <= old_start and span.end >= old_end]
    assert any("strike" in str(s.style) for s in styles)


def test_build_diff_text_bold_on_new():
    """New values in update get bold styling."""
    c = PropertyChange.update("x", '"old"', '"new"')
    text = _build_diff_text(c, max_path=1)
    new_start = text.plain.index('"new"')
    new_end = new_start + len('"new"')
    styles = [span for span in text._spans if span.start <= new_start and span.end >= new_end]
    assert any("bold" in str(s.style) for s in styles)


# ---------------------------------------------------------------------------
# format_diff_tree
# ---------------------------------------------------------------------------


def test_format_diff_tree_has_tree_connectors():
    """Tree output contains Rich tree connector characters."""
    changes = [
        PropertyChange.add("a", '"x"'),
        PropertyChange.add("b", '"y"'),
    ]
    output = _render(format_diff_tree("~ Resource  name  will be updated", changes))
    assert "├──" in output
    assert "└──" in output


def test_format_diff_tree_root_is_header():
    """Tree root label is the resource header."""
    output = _render(
        format_diff_tree(
            "~ MyType  myname  will be updated",
            [PropertyChange.add("x", '"v"')],
        )
    )
    assert "MyType" in output
    assert "myname" in output


def test_format_diff_tree_no_changes():
    """Tree with no changes still renders the header."""
    output = _render(format_diff_tree("+ MyType  name  will be created", []))
    assert "MyType" in output
    assert "├──" not in output


def test_format_diff_tree_indented():
    """Tree output is indented 2 spaces (Padding)."""
    output = _render(
        format_diff_tree(
            "+ MyType  name  will be created",
            [PropertyChange.add("x", '"v"')],
        )
    )
    # Root line starts with 2-space indent from Padding
    first_line = output.splitlines()[0]
    assert first_line.startswith("  ")


def test_format_diff_tree_update_shows_arrow():
    """Update entries show the → arrow between old and new values."""
    changes = [PropertyChange.update("x", '"a"', '"b"')]
    output = _render(format_diff_tree("~ Res  n  updated", changes))
    assert "→" in output
    assert '"a"' in output
    assert '"b"' in output


def test_format_diff_tree_forces_replacement():
    changes = [
        PropertyChange.update(
            "loc",
            '"uk"',
            '"eu"',
            forces_replacement=True,
        )
    ]
    output = _render(format_diff_tree("~ Res  n  updated", changes))
    assert "(forces replacement)" in output


def test_format_diff_tree_alignment():
    """Property paths are column-aligned within the tree."""
    changes = [
        PropertyChange.add("a", '"x"'),
        PropertyChange.add("long.path", '"y"'),
    ]
    output = _render(format_diff_tree("~ Res  n  updated", changes))
    lines = output.splitlines()
    # Find the two child lines (contain ├── or └──)
    children = [ln for ln in lines if "──" in ln]
    assert len(children) == 2
    # Both should have their value at the same column (paths are padded)
    idx0 = children[0].index('"x"')
    idx1 = children[1].index('"y"')
    assert idx0 == idx1


# ---------------------------------------------------------------------------
# _build_diff_text: multi-line (expanded) rendering
# ---------------------------------------------------------------------------


def test_build_diff_text_multiline_add():
    """Multi-line add value renders with expanded indentation."""
    multiline = '{\n  "name": "APP_ENV",\n  "value": "staging"\n}'
    c = PropertyChange.add("template.env", multiline)
    text = _build_diff_text(c, max_path=12)
    plain = text.plain
    assert plain.startswith("+ ")
    assert "template.env" in plain
    assert "\n" in plain
    assert "APP_ENV" in plain


def test_build_diff_text_multiline_update_has_arrow():
    """Multi-line update renders old, arrow, and new values."""
    old = '{\n  "address": "old.io"\n}'
    new = '{\n  "address": "new.io"\n}'
    c = PropertyChange.update("registry", old, new)
    text = _build_diff_text(c, max_path=8)
    plain = text.plain
    assert "→" in plain
    assert "old.io" in plain
    assert "new.io" in plain


def test_build_diff_text_multiline_delete():
    """Multi-line delete renders with expanded old value."""
    old = '[\n  "a",\n  "b"\n]'
    c = PropertyChange.delete("items", old)
    text = _build_diff_text(c, max_path=5)
    plain = text.plain
    assert plain.startswith("- ")
    assert '"a"' in plain
    assert '"b"' in plain


def test_build_diff_text_multiline_forces_replacement():
    """Multi-line update with forces_replacement shows the annotation."""
    old = '{\n  "a": 1\n}'
    new = '{\n  "a": 2\n}'
    c = PropertyChange.update(
        "config",
        old,
        new,
        forces_replacement=True,
    )
    text = _build_diff_text(c, max_path=6)
    assert "(forces replacement)" in text.plain


def test_build_diff_text_short_values_stay_inline():
    """Short single-line values use inline rendering, not expanded."""
    c = PropertyChange.update("location", '"uksouth"', '"westeurope"')
    text = _build_diff_text(c, max_path=8)
    plain = text.plain
    # Should be a single line (no newlines)
    assert "\n" not in plain
    assert "→" in plain


def test_build_diff_text_multiline_tree_rendering():
    """Multi-line values render correctly within a diff tree."""
    multiline = '{\n  "key": "value"\n}'
    changes = [PropertyChange.add("config", multiline)]
    output = _render(format_diff_tree("+ Res  n  created", changes))
    assert "key" in output
    assert "value" in output


# ---------------------------------------------------------------------------
# confirm: window pause/resume interaction
# ---------------------------------------------------------------------------


@patch("tlumi.display.console")
def test_confirm_pauses_and_resumes_window(mock_console):
    """When _window is set, pause() called before input, resume() after."""
    mock_window = MagicMock()
    call_order = []
    mock_window.pause.side_effect = lambda: call_order.append("pause")
    mock_window.resume.side_effect = lambda: call_order.append("resume")

    def fake_input(prompt, **kwargs):
        call_order.append("input")
        return "yes"

    mock_console.input.side_effect = fake_input

    with patch("tlumi.display._window", mock_window):
        result = confirm("Proceed?")

    assert result is True
    assert call_order == ["pause", "input", "resume"]


@patch("tlumi.display.console")
def test_confirm_no_pause_without_window(mock_console):
    """When _window is None, no pause/resume calls (no error)."""
    mock_console.input.return_value = "yes"

    with patch("tlumi.display._window", None):
        result = confirm("Proceed?")

    assert result is True


@patch("tlumi.display.console")
def test_confirm_resume_on_input_error(mock_console):
    """If console.input() raises, resume() is still called via try/finally."""
    mock_window = MagicMock()
    mock_console.input.side_effect = KeyboardInterrupt

    with patch("tlumi.display._window", mock_window):
        with pytest.raises(KeyboardInterrupt):
            confirm("Proceed?")

    mock_window.pause.assert_called_once()
    mock_window.resume.assert_called_once()


@patch("tlumi.display.console")
def test_confirm_eof_raises_tlumi_error(mock_console):
    """confirm() raises TlumiError on EOFError so piped destructive ops fail loudly."""
    from tlumi.errors import TlumiError

    mock_console.input.side_effect = EOFError

    with patch("tlumi.display._window", None):
        with pytest.raises(TlumiError, match="stdin is not a terminal"):
            confirm("Proceed?")


@patch("tlumi.display.console")
def test_confirm_eof_with_window_resumes(mock_console):
    """confirm() resumes the window even when EOFError raises TlumiError."""
    from tlumi.errors import TlumiError

    mock_window = MagicMock()
    mock_console.input.side_effect = EOFError

    with patch("tlumi.display._window", mock_window):
        with pytest.raises(TlumiError):
            confirm("Proceed?")

    mock_window.pause.assert_called_once()
    mock_window.resume.assert_called_once()


# ---------------------------------------------------------------------------
# masked_outputs_from_state (audit finding 60)
# ---------------------------------------------------------------------------


def _secret_wrapper(plaintext: object) -> dict:
    """Build the secret sig wrapper as it appears in exported state."""
    from tlumi.sanitize import _PULUMI_SECRET_SIG, _PULUMI_SECRET_VALUE

    return {_PULUMI_SECRET_SIG: _PULUMI_SECRET_VALUE, "plaintext": json.dumps(plaintext)}


def _exported_state(resources: list | None) -> MagicMock:
    """Build a mock exported Deployment with the given resources list."""
    state = MagicMock()
    state.version = 3
    state.deployment = None if resources is None else {"resources": resources}
    return state


def _state_with_outputs(outputs: object) -> MagicMock:
    return _exported_state(
        [
            {"type": "custom:res:Other", "urn": "urn:pulumi:default::p::custom:res:Other::x"},
            {
                "type": "pulumi:pulumi:Stack",
                "urn": "urn:pulumi:default::p::pulumi:pulumi:Stack::p",
                "outputs": outputs,
            },
        ]
    )


def test_masked_outputs_masks_nested_secret():
    """A secret nested inside a composite output is masked, siblings preserved."""
    state = _state_with_outputs(
        {"db": {"host": "example.com", "password": _secret_wrapper("hunter2-nested")}}
    )
    result = masked_outputs_from_state(state)
    assert result == {"db": {"host": "example.com", "password": "(sensitive)"}}
    assert "hunter2-nested" not in repr(result)


def test_masked_outputs_masks_whole_output_secret():
    """A whole-output secret is masked."""
    state = _state_with_outputs({"top": _secret_wrapper("hunter2-top")})
    result = masked_outputs_from_state(state)
    assert result == {"top": "(sensitive)"}


def test_masked_outputs_preserves_plain_values():
    """Non-secret values pass through unchanged."""
    state = _state_with_outputs({"url": "https://example.com", "count": 3})
    assert masked_outputs_from_state(state) == {"url": "https://example.com", "count": 3}


def test_masked_outputs_no_stack_resource():
    """State without a pulumi:pulumi:Stack resource yields no outputs."""
    state = _exported_state(
        [{"type": "custom:res:Other", "urn": "urn:pulumi:default::p::custom:res:Other::x"}]
    )
    assert masked_outputs_from_state(state) == {}


def test_masked_outputs_empty_deployment():
    """A never-applied stack (deployment None) yields no outputs."""
    assert masked_outputs_from_state(_exported_state(None)) == {}


def test_masked_outputs_non_dict_outputs():
    """A malformed Stack resource outputs field yields no outputs."""
    assert masked_outputs_from_state(_state_with_outputs("garbage")) == {}


# ---------------------------------------------------------------------------
# print_outputs: rendering of pre-masked values (T8)
# ---------------------------------------------------------------------------


def test_print_outputs_renders_values(capsys):
    """print_outputs renders caller-provided (already masked) values."""
    outputs = {
        "url": "https://example.com",
        "password": "(sensitive)",
    }
    print_outputs(outputs)
    output = capsys.readouterr().out
    assert "https://example.com" in output
    assert "(sensitive)" in output


def test_print_outputs_empty(capsys):
    """print_outputs with empty dict shows 'No outputs'."""
    print_outputs({})
    output = capsys.readouterr().out
    assert "No outputs" in output


def test_print_outputs_plain_values(capsys):
    """print_outputs handles plain string values gracefully."""
    outputs = {"plain_key": "plain_value"}
    print_outputs(outputs)
    output = capsys.readouterr().out
    assert "plain_key" in output
    assert "plain_value" in output


def test_print_outputs_composite_value(capsys):
    """print_outputs renders a composite (dict) value via repr."""
    outputs = {"db": {"host": "db.example.com", "password": "(sensitive)"}}
    print_outputs(outputs)
    output = capsys.readouterr().out
    assert "db.example.com" in output
    assert "(sensitive)" in output


# ---------------------------------------------------------------------------
# Emoji shortcode substitution disabled (audit finding 3)
# ---------------------------------------------------------------------------


def test_consoles_do_not_substitute_emoji_shortcodes(capsys):
    """Both consoles render ':name:' sequences verbatim (emoji=False)."""
    console.print("deploy :tada: done", markup=False, highlight=False)
    err_console.print("deploy :tada: done", markup=False, highlight=False)
    captured = capsys.readouterr()
    assert ":tada:" in captured.out
    assert ":tada:" in captured.err


def test_print_outputs_no_emoji_substitution(capsys):
    """Output values with ':name:' sequences (IPv6, MACs) are not corrupted."""
    outputs = {
        "addr": "2001:db8::ab:1",
        "mac": "44:ab:9d:00:11:22",
        "msg": "deploy :tada: done",
    }
    print_outputs(outputs)
    output = capsys.readouterr().out
    assert "2001:db8::ab:1" in output
    assert "44:ab:9d:00:11:22" in output
    assert ":tada:" in output


def test_print_json_no_emoji_substitution(capsys):
    """print_json does not rewrite ':name:' sequences inside JSON strings."""
    print_json(json.dumps({"msg": "deploy :tada: done", "addr": "2001:db8::ab:1"}))
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["msg"] == "deploy :tada: done"
    assert data["addr"] == "2001:db8::ab:1"


def test_install_log_no_emoji_substitution():
    """_InstallLog lines with ':name:' sequences render verbatim.

    Text.from_markup has its own emoji=True default that substitutes
    shortcodes at construction time, before Console(emoji=False) is ever
    consulted; the renderable must pass emoji=False itself.
    """
    lines = ["Downloading pkg :ab: 44:ab:9d:00:11:22", "deploy :tada: done"]
    rendered = _render(_InstallLog("Installing...", lines))
    assert "44:ab:9d:00:11:22" in rendered
    assert ":ab:" in rendered
    assert ":tada:" in rendered


def test_animated_status_no_emoji_substitution():
    """_AnimatedStatus messages with ':name:' sequences render verbatim."""
    rendered = _render(_AnimatedStatus("Checking 2001:db8::ab:1..."))
    assert ":ab:" in rendered


# ---------------------------------------------------------------------------
# _TlumiConsole: EPIPE handling stays catchable by cli._run()
# ---------------------------------------------------------------------------


class _EpipeFile(StringIO):
    """File whose non-empty writes fail with EPIPE."""

    def write(self, s: str) -> int:
        if s:
            raise BrokenPipeError()
        return 0

    def isatty(self) -> bool:
        return False


def test_tlumi_console_print_raises_broken_pipe_in_main_thread():
    """A broken-pipe write surfaces as BrokenPipeError, not Rich's SystemExit(1).

    Rich >= 13.2 converts EPIPE into SystemExit(1) (a BaseException) inside
    Console._check_buffer, which would sail past cli._run's handler.
    """
    c = _TlumiConsole(file=_EpipeFile())
    with pytest.raises(BrokenPipeError):
        c.print("data the reader will never see")


def test_tlumi_console_on_broken_pipe_mutes_console():
    """on_broken_pipe() mutes the console so teardown prints are dropped."""
    c = _TlumiConsole(file=_EpipeFile())
    with pytest.raises(BrokenPipeError):
        c.on_broken_pipe()
    assert c.quiet is True
    c.print("dropped, not raised")  # must not raise on the muted console


def test_tlumi_console_on_broken_pipe_worker_thread_raises_system_exit():
    """Non-main threads keep Rich's SystemExit convention (threading swallows it).

    The Live refresh thread and Pulumi event callbacks must not propagate
    BrokenPipeError through threading.excepthook as a stderr traceback.
    """
    import threading

    c = _TlumiConsole(file=_EpipeFile())
    caught: dict[str, BaseException] = {}

    def worker() -> None:
        try:
            c.on_broken_pipe()
        except BaseException as e:  # noqa: BLE001 - asserting the exact type
            caught["exc"] = e

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert isinstance(caught["exc"], SystemExit)
    assert c.quiet is True


# ---------------------------------------------------------------------------
# R8-S1: _InstallLog markup injection
# ---------------------------------------------------------------------------


def test_install_log_escapes_brackets():
    """_InstallLog escapes Rich markup in log lines."""
    lines = ["Installing [secret] package"]
    log = _InstallLog("Installing...", lines)
    rendered = _render(log)
    # The literal text "[secret]" should appear in output, not be interpreted as markup
    assert "[secret]" in rendered


# ---------------------------------------------------------------------------
# Plan/apply summaries: import counts, output-only changes, and the dangling
# empty "Resources:" header (audit findings).
# ---------------------------------------------------------------------------


def test_print_plan_summary_includes_import_count(capsys):
    print_plan_summary(create=1, import_count=2)
    out = capsys.readouterr().out
    assert "2 to import" in out
    assert "1 to add" in out


def test_print_plan_summary_output_only_change(capsys):
    print_plan_summary(outputs_changed=True)
    out = capsys.readouterr().out
    assert "outputs will change" in out
    assert "up-to-date" not in out


def test_print_plan_summary_no_changes_unchanged(capsys):
    print_plan_summary()
    out = capsys.readouterr().out
    assert "No changes" in out


def test_print_apply_summary_includes_import_count(capsys):
    print_apply_summary(import_count=1, duration="2s")
    out = capsys.readouterr().out
    assert "1 imported" in out
    assert "Resources:" in out


def test_print_apply_summary_zero_changes_no_dangling_header(capsys):
    print_apply_summary(duration="1s")
    out = capsys.readouterr().out
    assert "Resources:" not in out
    assert "No resources changed" in out
    assert "Duration: 1s" in out


def test_format_engine_result_includes_import():
    result = format_engine_result({"create": 1, "import": 2}, "3s")
    assert result["changes"]["import"] == 2
    assert result["changes"]["create"] == 1


# ---------------------------------------------------------------------------
# Window Live rendering path (_WindowContext, WindowLiveProxy): previously
# never executed by any test (audit finding).
# ---------------------------------------------------------------------------


def _capture_console():
    import io

    from rich.console import Console

    return Console(file=io.StringIO(), force_terminal=True, no_color=True, width=100)


def test_window_context_lifecycle(monkeypatch):
    """create_window() starts a Live region, registers globally, and tears down."""
    import tlumi.display as display

    monkeypatch.setattr(display, "console", _capture_console())
    window = display.create_window()
    assert window.is_started is False
    assert display._get_active_window() is None
    with window as w:
        assert w is window
        assert window.is_started is True
        assert display._get_active_window() is window
        window.set_inner("spinner text")
        window.clear_inner()
    assert window.is_started is False
    assert display._get_active_window() is None


def test_window_pause_resume(monkeypatch):
    """pause() stops the Live region for prompts; resume() restarts it."""
    import tlumi.display as display

    monkeypatch.setattr(display, "console", _capture_console())
    with display.create_window() as window:
        window.pause()
        assert window.is_started is False
        window.resume()
        assert window.is_started is True


def test_window_live_proxy_routes_through_window(monkeypatch):
    """WindowLiveProxy duck-types Live: update() composes into the window."""
    import tlumi.display as display

    monkeypatch.setattr(display, "console", _capture_console())
    with display.create_window() as window:
        proxy = display.WindowLiveProxy(window)
        assert proxy.is_started is True
        with proxy:
            proxy.update("engine progress line")
        # exiting the proxy clears the inner content but keeps the window alive
        assert window.is_started is True


def test_engine_start_live_uses_window_proxy(monkeypatch):
    """EventHandler.start_live() composes into an active window via the proxy."""
    import tlumi.display as display
    from tlumi.engine import EventHandler

    monkeypatch.setattr(display, "console", _capture_console())
    with display.create_window():
        handler = EventHandler()
        ctx = handler.start_live()
        with ctx as live:
            assert isinstance(live, display.WindowLiveProxy)
