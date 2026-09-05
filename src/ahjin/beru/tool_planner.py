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
     "query": core search term (e.g. "resume", "notes", "ModelRouter"). Do NOT use generic
              file extension words ("pdf", "py", "txt") or folder path names as query when
              a specific target term is present.
     "path": target root or nested folder constraint (e.g. "downloads/archived", "desktop").
     "file_extensions": optional array of extension filters (e.g. [".pdf"], [".py"]).
     "search_mode": optional "discovery" (find files/folders by name) or "content".
   Path shortcuts: "pc" (search all roots), "desktop", "downloads", "documents", or subpaths.

3. file_read
   Purpose: Read the contents of a safe text file on the user's PC or workspace.
   Parameters: {"path": "desktop/resume.txt"}

INSTRUCTIONS:
- You MUST invoke a tool whenever the user asks to read, search, view, or inspect local files.
- Path Extraction Rules:
  - If user mentions nested folders (e.g. "inside Downloads Archived"), combine them into
    "path": "downloads/archived".
  - Do NOT extract directory path names (e.g. "archived", "downloads") as the search query.
- Query Extraction Rules:
  - Extract the core target keyword (e.g. "resume") into query.
  - Do NOT set query to generic extension words ("pdf", "file") when specific terms exist.
- Extension Extraction Rules:
  - Extract extension words ("pdf", "python", "txt") into "file_extensions": [".pdf"], [".py"].
- Output ONLY valid JSON matching the schema:
  {"tool_name": "file_search", "parameters": {"query": "resume",
  "path": "downloads/archived", "file_extensions": [".pdf"], "search_mode": "discovery"}}
  or
  {"tool_name": "file_read", "parameters": {"path": "desktop/notes.txt"}}
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

            logger.info(
                "ToolIntentPlanner: Planned structured tool intent",
                tool_name=tool_name,
                parameters=parameters,
            )
            return ToolInvocationRequest(tool_name=tool_name, parameters=parameters)

        except Exception as exc:
            logger.debug("ToolIntentPlanner LLM planning bypassed", error=str(exc))
            return None
