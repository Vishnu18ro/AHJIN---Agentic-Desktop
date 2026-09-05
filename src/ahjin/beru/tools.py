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


_WEB_SEARCH_PREFIXES: tuple[str, ...] = (
    "search the web for",
    "search online for",
    "search online",
    "search the web",
    "look up online",
    "look up",
    "find the latest",
    "current weather in",
    "current weather",
    "current price of",
    "latest news",
    "recent papers on",
)


def detect_tool_intent(text: str) -> ToolInvocationRequest | None:
    """Analyze request text for deterministic tool execution signals.

    Returns:
        ToolInvocationRequest if a known tool intent is detected, or None.
    """
    lower_text = text.lower()
    if any(phrase in lower_text for phrase in _SYSTEM_INFO_PHRASES):
        return ToolInvocationRequest(tool_name="system_info", parameters={})

    if "open whatsapp web" in lower_text:
        return ToolInvocationRequest(
            tool_name="browser",
            parameters={"action": "navigate", "url": "https://web.whatsapp.com"},
        )
    elif "take a screenshot" in lower_text or "screenshot of the current page" in lower_text:
        return ToolInvocationRequest(
            tool_name="browser",
            parameters={"action": "screenshot"},
        )
    elif lower_text.startswith("open google and search"):
        query = text[len("open google and search"):].strip(" :?-")
        return ToolInvocationRequest(
            tool_name="browser",
            parameters={
                "action": "type",
                "selector": "textarea[name='q'], input[name='q']",
                "text": query,
                "press_enter": True,
            },
        )
    elif "open google" in lower_text:
        return ToolInvocationRequest(
            tool_name="browser",
            parameters={"action": "navigate", "url": "https://www.google.com"},
        )
    elif lower_text.startswith("go to http"):
        url = text[len("go to "):].strip()
        return ToolInvocationRequest(
            tool_name="browser",
            parameters={"action": "navigate", "url": url},
        )

    for prefix in _WEB_SEARCH_PREFIXES:
        if prefix in lower_text:
            idx = lower_text.find(prefix)
            raw_query = text[idx + len(prefix) :].strip(" :?-")
            query = raw_query if raw_query else text.strip()
            return ToolInvocationRequest(
                tool_name="web_search",
                parameters={"query": query},
            )

    return None
