"""BERU Tool Intent Resolver — Deterministic tool signal detection."""

from ahjin.tools.base import ToolInvocationRequest

_SYSTEM_INFO_PHRASES: tuple[str, ...] = (
    "operating system",
    "what os",
    "which os",
    "python version",
    "what machine",
    "about this system",
    "system info",
    "system information",
)


def detect_tool_intent(text: str) -> ToolInvocationRequest | None:
    """Analyze request text for deterministic tool execution signals.

    Returns:
        ToolInvocationRequest if a known tool intent is detected, or None.
    """
    lower_text = text.lower()
    if any(phrase in lower_text for phrase in _SYSTEM_INFO_PHRASES):
        return ToolInvocationRequest(tool_name="system_info", parameters={})
    return None
