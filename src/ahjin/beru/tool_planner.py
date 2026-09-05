"""BERU Tool Intent Planner — LLM-assisted tool intent resolution with strict validation."""

import json
from typing import TYPE_CHECKING, Any, cast

import structlog

from ahjin.beru.types import CapabilityRequirements
from ahjin.providers.types import ContextualizedPrompt
from ahjin.tools.base import ToolInvocationRequest
from ahjin.tools.system_info import SAFE_FIELDS_WHITELIST

if TYPE_CHECKING:
    from ahjin.harness.gateway import ProviderGateway
    from ahjin.tools.registry import ToolRegistry

logger = structlog.get_logger()

_PLANNER_SYSTEM_PROMPT = """You are the Tool Intent Planner for AHJIN 2.0.
Your sole job is to analyze the user's input and decide if a registered tool should be called.

AVAILABLE TOOLS CATALOG:
1. system_info
   Purpose: Retrieve safe runtime/system information.
   Supported fields: ["os", "python", "machine", "platform", "cwd", "cpu", "memory", "all_safe"]

2. file_search
   Purpose: Search files on PC or workspace by filename, directory path, or text content.
   Parameters:
     "query": core search term (e.g. "resume", "notes", "ModelRouter").
     "path": target root or nested folder constraint (e.g. "downloads/archived", "desktop").
     "file_extensions": optional array of extension filters (e.g. [".pdf"], [".py"]).
     "search_mode": optional "discovery" or "content".

3. file_read
   Purpose: Read the contents of a safe text file, PDF document, or ZIP archive.
   Parameters:
     "path": target file path (e.g. "desktop/resume.pdf").
     "content_scope": optional "entire_document", "page", "relevant", "metadata", "archive_listing".
     "page_number": optional integer (1-indexed page number for PDFs).
     "query": optional search query for relevant content filtering.

4. file_send
   Purpose: Prepare an actual file from PC/workspace to send as a chat document attachment.
   Parameters:
     "path": target file or folder path (e.g. "desktop/resume.pdf", "downloads/archived").
     "query": optional filename or target keyword if path is a folder.

5. web_search
   Purpose: Search the live web for current news, weather, stock prices, or research.
   Parameters:
     "query": search query (e.g. "latest NVIDIA news", "weather in Hyderabad").
     "recency_days": optional integer for recent filtering (e.g. 7).
     "max_results": optional integer max count (e.g. 5).

6. browser
   Purpose: Control a live browser to open pages, click, type, scroll, or inspect.
   Parameters:
     "action": action name ("open", "navigate", "observe", "click", "type", "press", etc.).
     "url": target URL (for "open" or "navigate").
     "selector": CSS selector or element description (for "click" or "type").
     "text": text to type (for "type").
     "description": description or link text for click fallback.
     "direction": "down" or "up" (for "scroll").
     "key": key name e.g. "Enter" (for "press").

INSTRUCTIONS:
- You MUST invoke a tool when asked to send/read files, search the web, OR control a browser.
- Intent Mapping:
  - User asks to OPEN BROWSER, GO TO URL, CLICK, TYPE, SCROLL, TAKE SCREENSHOT -> output "browser".
  - User asks to SEND, ATTACH, GIVE, or RETURN a file -> output "file_send".
  - User asks to READ, EXTRACT, SUMMARIZE, or ASK ABOUT content -> output "file_read".
  - User asks to FIND, LOCATE, or SEARCH local files -> output "file_search".
  - User asks to SEARCH THE WEB or FIND LATEST/CURRENT info -> output "web_search".
- Path Extraction Rules:
  - If user mentions nested folders (e.g. "inside Downloads Archived"), combine them into
    "path": "downloads/archived".
- Output ONLY valid JSON matching the schema:
  {"tool_name": "browser", "parameters": {"action": "navigate", "url": "https://google.com"}}
  or
  {"tool_name": "web_search", "parameters": {"query": "latest NVIDIA news"}}
  or
  {"tool_name": "file_send", "parameters": {"path": "downloads/archived/resume.pdf"}}
  or
  {"tool_name": "file_read", "parameters": {"path": "desktop/notes.txt",
   "content_scope": "entire_document"}}
  or
  {"tool_name": "file_search", "parameters": {"query": "resume", "path": "downloads/archived"}}
  or
  {"tool_name": "system_info", "parameters": {"fields": ["os"]}}
- Do NOT output explanations or markdown formatting outside the JSON object.
"""


class ToolIntentPlanner:
    """LLM-assisted tool intent planner.

    Converts user input into a validated ToolInvocationRequest using an LLM.
    Strictly enforces that tools exist in ToolRegistry and parameters match whitelists.
    Does NOT execute tools or possess permission to perform actions.
    """

    def __init__(
        self,
        gateway: "ProviderGateway | None" = None,
        tool_registry: "ToolRegistry | None" = None,
    ) -> None:
        self.gateway = gateway
        self.tool_registry = tool_registry

    async def plan_tool_intent(self, text: str) -> ToolInvocationRequest | None:
        """Attempt to plan a structured tool invocation from natural language text.

        Returns:
            A validated ToolInvocationRequest if a valid tool intent is identified,
            or None if no tool is needed or if validation fails.
        """
        if self.gateway is None or self.tool_registry is None:
            return None

        prompt = ContextualizedPrompt(
            system_instruction=_PLANNER_SYSTEM_PROMPT,
            user_instruction=text,
        )

        try:
            # Use FAST tier model for planning
            result = await self.gateway.invoke(
                prompt=prompt,
                requirements=CapabilityRequirements(
                    requires_reasoning=False,
                    requires_code=False,
                    requires_vision=False,
                ),
            )
            raw_content = result.response.content.strip()
            # Clean possible markdown code fences if model included them
            if raw_content.startswith("```"):
                lines = raw_content.splitlines()
                raw_content = "\n".join(
                    [line for line in lines if not line.startswith("```")]
                ).strip()

            parsed_obj: Any = json.loads(raw_content)
            if not isinstance(parsed_obj, dict):
                return None

            parsed_dict: dict[str, Any] = cast(dict[str, Any], parsed_obj)

            tool_name_val: Any = parsed_dict.get("tool_name")
            if not isinstance(tool_name_val, str) or not tool_name_val or tool_name_val == "none":
                return None

            tool_name: str = tool_name_val

            # 1. Authority Validation: Tool MUST exist in ToolRegistry
            if not self.tool_registry.has_tool(tool_name):
                logger.warning(
                    "ToolIntentPlanner: Model requested unregistered tool",
                    requested_tool=tool_name,
                )
                return None

            raw_params: Any = parsed_dict.get("parameters")
            parameters: dict[str, Any] = (
                cast(dict[str, Any], raw_params) if isinstance(raw_params, dict) else {}
            )

            # 2. Parameter Whitelist Validation
            if tool_name == "system_info":
                raw_fields: Any = parameters.get("fields")
                field_list: list[Any] = (
                    cast(list[Any], raw_fields) if isinstance(raw_fields, list) else ["all_safe"]
                )

                valid_fields: list[str] = [
                    str(x) for x in field_list if isinstance(x, str) and x in SAFE_FIELDS_WHITELIST
                ]
                if not valid_fields:
                    logger.warning(
                        "ToolIntentPlanner: Model requested invalid fields for system_info",
                        requested_fields=raw_fields,
                    )
                    return None
                parameters["fields"] = valid_fields

            elif tool_name == "file_search":
                raw_query: Any = parameters.get("query")
                if not isinstance(raw_query, str) or not raw_query.strip():
                    logger.warning("ToolIntentPlanner: Invalid or missing query for file_search")
                    return None
                parameters["query"] = raw_query.strip()

            elif tool_name == "file_read":
                raw_path: Any = parameters.get("path")
                if not isinstance(raw_path, str) or not raw_path.strip():
                    logger.warning("ToolIntentPlanner: Invalid or missing path for file_read")
                    return None
                parameters["path"] = raw_path.strip()

            elif tool_name == "file_send":
                raw_path: Any = parameters.get("path")
                if not isinstance(raw_path, str) or not raw_path.strip():
                    logger.warning("ToolIntentPlanner: Invalid or missing path for file_send")
                    return None
                parameters["path"] = raw_path.strip()

            elif tool_name == "web_search":
                raw_query: Any = parameters.get("query")
                if not isinstance(raw_query, str) or not raw_query.strip():
                    logger.warning("ToolIntentPlanner: Invalid or missing query for web_search")
                    return None
                parameters["query"] = raw_query.strip()
                raw_recency: Any = parameters.get("recency_days")
                if isinstance(raw_recency, int) and raw_recency > 0:
                    parameters["recency_days"] = raw_recency
                raw_max: Any = parameters.get("max_results")
                if isinstance(raw_max, int) and raw_max > 0:
                    parameters["max_results"] = raw_max

            elif tool_name == "browser":
                raw_action: Any = parameters.get("action")
                if not isinstance(raw_action, str) or not raw_action.strip():
                    # Infer navigate if url present, else observe
                    if "url" in parameters:
                        parameters["action"] = "navigate"
                    else:
                        parameters["action"] = "observe"
                else:
                    parameters["action"] = raw_action.strip().lower()

            logger.info(
                "ToolIntentPlanner: Planned structured tool intent",
                tool_name=tool_name,
                parameters=parameters,
            )
            return ToolInvocationRequest(tool_name=tool_name, parameters=parameters)

        except Exception as exc:
            logger.debug("ToolIntentPlanner LLM planning bypassed", error=str(exc))
            return None
