"""Stable errors shared across provider, tool, and workspace boundaries."""


class RepoPilotError(Exception):
    """Base class for expected RepoPilot failures."""


class ProviderError(RepoPilotError):
    """An LLM provider request failed."""


class ProviderTimeoutError(ProviderError):
    """An LLM provider request timed out."""


class ProviderHTTPError(ProviderError):
    """An LLM provider returned a non-success status."""


class ProviderResponseError(ProviderError):
    """An LLM provider returned an invalid response."""


class ToolError(RepoPilotError):
    """A tool could not execute its requested operation."""


class UnknownToolError(ToolError):
    """A requested tool is not registered."""


class DuplicateToolError(ToolError):
    """Two tools attempted to register the same name."""


class PermissionDeniedError(RepoPilotError):
    """A tool call was denied by policy or approval."""


class WorkspaceError(RepoPilotError):
    """A workspace could not be created, inspected, or cleaned up."""


class PathBoundaryError(WorkspaceError):
    """A path resolves outside its workspace."""


class CommandExecutionError(RepoPilotError):
    """A command could not be started."""


class RuntimeLimitError(RepoPilotError):
    """A configured runtime limit was reached."""

