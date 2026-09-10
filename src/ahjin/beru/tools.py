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
)


_FILE_SEARCH_PREFIXES: tuple[str, ...] = (
    "find my ",
    "find the file ",
    "find file ",
    "find a file ",
    "find files ",
    "find the ",
    "where is my ",
    "where is the file ",
    "where is the ",
    "look for my ",
    "look for the file ",
    "look for the ",
    "search for my ",
    "search for the file ",
    "search for the ",
    "locate my ",
    "locate the file ",
    "locate the ",
    "search file ",
    "search files ",
    "list files ",
)

_FOLDER_HINTS: tuple[tuple[str, str], ...] = (
    ("inside the archived folder", "downloads/archived"),
    ("in the archived folder", "downloads/archived"),
    ("inside archived", "downloads/archived"),
    ("in archived", "downloads/archived"),
    ("archived folder", "downloads/archived"),
    ("inside the downloads folder", "downloads"),
    ("in the downloads folder", "downloads"),
    ("inside downloads", "downloads"),
    ("in downloads", "downloads"),
    ("downloads folder", "downloads"),
    ("inside the documents folder", "documents"),
    ("in the documents folder", "documents"),
    ("inside documents", "documents"),
    ("in documents", "documents"),
    ("documents folder", "documents"),
    ("inside the desktop folder", "desktop"),
    ("in the desktop folder", "desktop"),
    ("on my desktop", "desktop"),
    ("on desktop", "desktop"),
    ("inside desktop", "desktop"),
    ("in desktop", "desktop"),
    ("desktop folder", "desktop"),
)


_TOOL_TRIGGER_KEYWORDS: frozenset[str] = frozenset({
    # System info
    "os", "system", "platform", "cpu", "ram", "memory", "hardware", "cwd",
    # File actions & formats
    "file", "files", "folder", "folders", "directory", "document", "documents",
    "pdf", "txt", "csv", "docx", "zip", "resume", "notes", "archive", "attachment",
    # Web search & recency
    "google", "bing", "search", "web", "online", "weather", "latest", "current",
    "news", "today", "stock", "price", "score", "recent",
    # Browser & web apps
    "browser", "whatsapp", "gmail", "youtube", "chrome", "edge", "website",
    "webpage", "url", "http", "https", "site", "screenshot",
})

_TOOL_TRIGGER_PHRASES: tuple[str, ...] = (
    # System
    "what os", "which os", "operating system", "python version", "system info", "about this system",
    # File queries & actions
    "find file", "search file", "read file", "open file", "send file", "attach file",
    "where is", "look for", "list files", "find my", "send my", "read my", "inside folder",
    "page 1", "page 2", "page 3", "page number",
    # Web search
    "search for", "look up", "find online", "current weather", "latest news", "search the web",
    # Browser
    "open whatsapp", "open google", "go to", "take a screenshot", "click on", "type in",
)


def may_require_tool(text: str) -> bool:
    """Determine if a request text potentially requires tool evaluation.

    Fast deterministic check to prevent unnecessary LLM planning invocations
    for ordinary conversation, coding, or Q&A requests.

    Returns:
        True if the text contains explicit tool signals or tool-potential keywords/phrases.
        False if the text is ordinary conversation or general knowledge Q&A.
    """
    if detect_tool_intent(text) is not None:
        return True

    lower_text = text.lower()

    # Check multi-word tool trigger phrases
    if any(phrase in lower_text for phrase in _TOOL_TRIGGER_PHRASES):
        return True

    # Tokenize words (stripping common punctuation)
    words = frozenset(
        w.strip(".,?!:;()[]{}'\"") for w in lower_text.split()
    )

    return bool(words & _TOOL_TRIGGER_KEYWORDS)


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

    for prefix in _FILE_SEARCH_PREFIXES:
        if prefix in lower_text:
            idx = lower_text.find(prefix)
            after_prefix = text[idx + len(prefix) :].strip(" :?-.,")
            if not after_prefix:
                continue

            path_hint = "."
            for folder_kw, folder_path in _FOLDER_HINTS:
                if folder_kw in lower_text:
                    path_hint = folder_path
                    break

            clean_query = after_prefix
            for sep in (
                ",",
                " and ",
                " inside ",
                " in ",
                " on ",
                " then ",
                " please",
                "...",
            ):
                sep_idx = clean_query.lower().find(sep)
                if sep_idx != -1:
                    clean_query = clean_query[:sep_idx]

            clean_query = clean_query.strip(" :?-.,'\"")
            if clean_query.lower().endswith(" file"):
                clean_query = clean_query[:-5].strip()
            elif clean_query.lower().endswith(" document"):
                clean_query = clean_query[:-9].strip()

            if clean_query:
                return ToolInvocationRequest(
                    tool_name="file_search",
                    parameters={"query": clean_query, "path": path_hint},
                )

    return None

