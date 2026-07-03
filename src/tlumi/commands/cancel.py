"""tlumi state unlock - Release stale lock on the state."""

from __future__ import annotations

from pulumi.automation import CommandError

from tlumi.config import ProjectConfig, find_project_dir, load_config
from tlumi.display import animated_status, console, print_banner, print_success
from tlumi.errors import WorkspaceError
from tlumi.redact import redact_text
from tlumi.workspace import get_stack


def _has_local_lock(config: ProjectConfig) -> bool:
    """True if the local file backend has any lock file for this project.

    The default file:// backend implements `cancel` by deleting lock files and
    succeeds silently when there are none, so the CommandError branch (which is
    a Pulumi Cloud signal) never fires. Inspecting the lock directory lets us
    tell "released a stale lock" from "there was nothing to release".
    """
    locks_dir = config.state_dir / ".pulumi" / "locks"
    if not locks_dir.exists():
        return False
    try:
        return any(p.is_file() for p in locks_dir.rglob("*"))
    except OSError:
        # If we cannot read the lock dir, fall back to the optimistic message
        # rather than guessing there is no lock.
        return True


def run_cancel() -> None:
    """Release a stale state lock from a previous interrupted operation."""
    config = load_config(find_project_dir())

    print_banner("Unlocking state...")
    console.print()

    # _has_local_lock inspects the DEFAULT lock path (.tlumi/state/.pulumi/locks).
    # Only trust it for the default backend; a custom backend.url (even a custom
    # file:// path) stores locks elsewhere, so fall back to the optimistic
    # "released" message there rather than mis-reporting a real unlock as a no-op.
    is_default_backend = config.backend.url is None
    had_lock = _has_local_lock(config) if is_default_backend else True

    with animated_status("  Releasing state lock..."):
        stack = get_stack(config, runtime=False)
        try:
            stack.cancel()
        except CommandError as e:
            err = str(e)
            if "no update is currently running" in err.lower():
                console.print("  [muted]No active lock to release.[/muted]")
                return
            raise WorkspaceError(f"Unlock failed: {redact_text(err)}") from None

    if not had_lock:
        console.print("  [muted]No active lock to release.[/muted]")
        return

    print_success("State lock released.")
