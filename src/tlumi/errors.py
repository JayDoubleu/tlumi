"""User-friendly error types for tlumi."""

from typing import NamedTuple


class Diagnostic(NamedTuple):
    """A single diagnostic message from the Pulumi engine."""

    resource: str
    message: str


class TlumiError(Exception):
    """Base error for all tlumi operations. Caught at CLI boundary."""

    def __init__(self, message: str, hint: str | None = None):
        self.message = message
        self.hint = hint
        super().__init__(message)


class ConfigError(TlumiError):
    """Error loading or parsing tlumi.yaml."""


class WorkspaceError(TlumiError):
    """Error with the Pulumi workspace or stack operations."""


class ProjectNotFoundError(TlumiError):
    """No tlumi.yaml found in the current directory."""

    def __init__(self):
        super().__init__(
            "No tlumi.yaml found in the current directory.",
            hint="Run 'tlumi init' to create a new project, or change to your project directory.",
        )


class EngineError(TlumiError):
    """Pulumi engine operation failed with structured diagnostics."""

    def __init__(
        self,
        message: str,
        diagnostics: list[Diagnostic] | None = None,
        full_output: str = "",
        hint: str | None = None,
    ):
        super().__init__(message, hint=hint)
        # Copy the caller's list so we own a private container the caller
        # cannot mutate after construction (the constructor takes list[Diagnostic]
        # for ergonomic call sites; the stored attribute is immutable in spirit).
        self.diagnostics = list(diagnostics) if diagnostics else []
        self.full_output = full_output
