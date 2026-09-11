import re
from pathlib import Path

from ahjin.tools.base import ToolInvocationRequest

_QUOTED_WINDOWS_PATH_RE = re.compile(
    r'["\']([a-zA-Z]:[\\/][^"\'<>:|?*\r\n\t]+)["\']'
)
_UNQUOTED_WINDOWS_PATH_RE = re.compile(
    r'[a-zA-Z]:[\\/](?:[^\s<>:"|?*\r\n\t]+[\\/]?)*'
)


def extract_windows_path(text: str) -> tuple[str, bool] | None:
    """Extract a Windows absolute path and classify whether it is a directory or exact file.

    Returns:
        tuple of (cleaned_path_str, is_directory) if found, else None.
    """
    quoted_match = _QUOTED_WINDOWS_PATH_RE.search(text)
    if quoted_match:
        raw_path = quoted_match.group(1).strip()
    else:
        unquoted_match = _UNQUOTED_WINDOWS_PATH_RE.search(text)
        if not unquoted_match:
            return None
        raw_path = unquoted_match.group(0).strip(" \t\r\n'\"<>,")

    if not raw_path:
        return None

    # Check if syntactically or physically a directory
    is_dir = raw_path.endswith(("\\", "/"))
    if not is_dir:
        try:
            p = Path(raw_path)
            if p.exists() and p.is_dir():
                is_dir = True
            elif not p.suffix:
                # No file extension typically implies directory path
                is_dir = True
        except Exception:
            pass

    return raw_path, is_dir

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
    # NOTE: "find the " was deliberately removed — it is too broad and falsely
    # matches non-filesystem requests like "find the bug in my code".
    # The semantic planner handles ambiguous "find" requests correctly.
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


def may_require_tool(text: str) -> bool:
    """Conservative structural gate — ToolIntentPlanner is the semantic decision point.

    This function does NOT enumerate tool-related vocabulary or attempt semantic
    classification. Its contract is: when uncertain, return True and let the planner
    decide. The planner is responsible for the semantic judgment of whether a tool
    is needed.

    Returns:
        True for all requests (planner runs and makes the semantic decision).
        The Windows path check documents intent for filesystem-context requests
        but does not change behaviour since the default is already True.

    Invariants:
        - No keyword-based routing.
        - No token-length heuristics (short requests may be tool requests).
        - No hardcoded phrase patterns for semantic exclusion.
        - When in doubt: True.
    """
    # Windows absolute paths explicitly confirm filesystem context.
    # Documented here for clarity, but the default return True covers this anyway.
    if _QUOTED_WINDOWS_PATH_RE.search(text) or _UNQUOTED_WINDOWS_PATH_RE.search(text):
        return True

    # Conservative default: delegate all semantic judgment to ToolIntentPlanner.
    return True


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

