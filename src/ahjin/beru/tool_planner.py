"""BERU Tool Intent Planner — LLM-assisted tool intent resolution with strict validation."""

import asyncio
import json
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, cast

import httpx
import structlog

from ahjin.beru.types import CapabilityRequirements
from ahjin.core.config import settings
from ahjin.models.router import CapabilityUnavailableError
from ahjin.providers.types import ContextualizedPrompt
from ahjin.tools.base import ToolInvocationRequest
from ahjin.tools.system_info import SAFE_FIELDS_WHITELIST

if TYPE_CHECKING:
    from ahjin.harness.gateway import ProviderGateway
    from ahjin.tools.registry import ToolRegistry

logger = structlog.get_logger()

# Default bounded timeout in seconds for intent planning calls.
# Configurable via settings.tool_planner_timeout (default 15.0s).
DEFAULT_PLANNER_TIMEOUT_SECONDS: float = settings.tool_planner_timeout

# Defensive upper iteration bound solely to prevent infinite loops.
# Normal candidate exhaustion is governed by ModelRouter raising CapabilityUnavailableError.
_DEFENSIVE_MAX_ATTEMPTS: int = 50


class PlannerStatus(str, Enum):
    """Explicit 3-state outcome of tool intent planning."""

    TOOL_SELECTED = "tool_selected"
    NO_TOOL = "no_tool"
    PLANNER_FAILURE = "planner_failure"


@dataclass(frozen=True)
class PlannerResult:
    """Structured result of ToolIntentPlanner evaluation.

    Explicitly distinguishes between:
    - TOOL_SELECTED: user needs an authoritative tool execution.
    - NO_TOOL: user explicitly asked general chat / knowledge / reasoning.
    - PLANNER_FAILURE: timeout, provider error, or malformed JSON (NEVER conflated with NO_TOOL).
    """

    status: PlannerStatus
    tool_intent: ToolInvocationRequest | None = None
    failure_reason: str | None = None

    # Backward-compatible convenience accessors
    @property
    def tool_name(self) -> str | None:
        return self.tool_intent.tool_name if self.tool_intent else None

    @property
    def parameters(self) -> dict[str, Any]:
        return self.tool_intent.parameters if self.tool_intent else {}

    @property
    def requires_reasoning(self) -> bool:
        return self.tool_intent.requires_reasoning if self.tool_intent else False

    def __bool__(self) -> bool:
        """Truthiness matches whether a tool was selected."""
        return self.status == PlannerStatus.TOOL_SELECTED and self.tool_intent is not None


_PLANNER_SYSTEM_PROMPT = """You are the Tool Intent Planner for AHJIN 2.0.
Your task: analyze the user's request and determine whether a registered AHJIN tool
should be invoked, and with what parameters.

You are the SOLE SEMANTIC DECISION POINT. Determine what the user needs done based on
the MEANING of their request — not based on specific keywords or phrases.

═══════════════════════════════════════════════════════════════════════════════
OUTPUT FORMAT
═══════════════════════════════════════════════════════════════════════════════

If a tool is needed:
  {"tool_name": "<name>", "parameters": {...}, "requires_reasoning": <bool>}

If NO tool is needed (general conversation, knowledge Q&A, creative writing, code generation):
  {"tool_name": "none", "parameters": {}, "requires_reasoning": false}

Output ONLY valid JSON matching one of the above shapes.
Do NOT output explanations or markdown formatting outside the JSON.

═══════════════════════════════════════════════════════════════════════════════
AVAILABLE TOOLS AND THEIR CAPABILITIES
═══════════════════════════════════════════════════════════════════════════════

1. system_info
   KNOWS: The current runtime state of THIS machine right now.
     - Operating system name and release build (Windows, Linux, macOS)
     - Processor / CPU model name (e.g. Intel Core i7, AMD Ryzen), core count, architecture
     - Physical RAM / memory amount (total GB, available GB, usage percentage)
     - Graphics adapter / GPU details (e.g. Intel Iris Xe, NVIDIA GeForce)
     - System drive storage capacity (total GB, free GB, used GB)
     - Python runtime version currently running on this machine
     - Current working directory of the process
     - Platform identifier string
   FIELDS: ["os", "python", "machine", "platform", "cwd", "cpu", "memory", "gpu",
            "storage", "all_safe"]

   USE system_info when the user is asking about THIS machine's hardware, OS, or
   runtime state. The user's phrasing does not need to match these field names —
   use semantic judgment to identify what they are asking about.

   SYSTEM_INFO PARAPHRASE EXAMPLES (all of these → system_info):
     "what OS am I using?" → fields: ["os"]
     "what operating system is running on this machine?" → fields: ["os"]
     "what Windows version do I have?" → fields: ["os"]
     "what are my computer specs?" → fields: ["all_safe"]
     "what is the specs of my PC?" → fields: ["all_safe"]
     "what are my computer specifications?" → fields: ["all_safe"]
     "tell me about this machine" → fields: ["all_safe"]
     "what hardware is inside this PC?" → fields: ["all_safe"]
     "which processor am I running?" → fields: ["cpu"]
     "what CPU does this machine have?" → fields: ["cpu"]
     "how many cores does my processor have?" → fields: ["cpu"]
     "how much RAM does this PC have?" → fields: ["memory"]
     "how much memory is installed on this computer?" → fields: ["memory"]
     "how much RAM do I have?" → fields: ["memory"]
     "what GPU is in this computer?" → fields: ["gpu"]
     "what graphics card do I have?" → fields: ["gpu"]
     "how much storage do I have?" → fields: ["storage"]
     "how much free disk space is left?" → fields: ["storage"]
     "what is my hard drive capacity?" → fields: ["storage"]
     "my computer specifications" → fields: ["all_safe"]
     "CPU specs?" → fields: ["cpu"]
     "memory info" → fields: ["memory"]
     "what Python version is installed?" → fields: ["python"]
     "hardware details of this system" → fields: ["all_safe"]

2. file_search
   CAN DO: Search local filesystem for files in authorized roots (Downloads, Documents, etc.).
   USE when: the user wants to FIND or LOCATE a file whose full path is not known.
   PARAMETERS:
     "query": filename keyword or subject to search for
     "path": folder to search in ("downloads", "desktop", "documents", "." for all)
     "file_extensions": optional array e.g. [".pdf"]
     "search_mode": optional "discovery" or "content"

3. file_read
   CAN DO: Read the contents of a local file (text, PDF, ZIP).
   USE when: the user wants to see, extract, or reason about the CONTENT of a specific file.
   PARAMETERS:
     "path": file path; use "." + query if location unknown
     "content_scope": "entire_document", "page", "relevant", "metadata", "archive_listing"
     "page_number": optional int (1-indexed)
     "query": optional search query for relevant content

4. file_send
   CAN DO: Prepare and attach a local file as a chat document attachment.
   USE when: the user wants to RECEIVE a file as an attachment.
   PARAMETERS:
     "path": file or folder path
     "query": optional filename/keyword if path is a folder or unknown

   FILE OPERATION PARAPHRASE EXAMPLES:
     "send me my resume" → file_send, path=".", query="resume"
     "get my CV from Downloads" → file_search, query="cv", path="downloads"
     "send resume" → file_send, path=".", query="resume"
     "read resume" → file_read, path=".", query="resume"
     "attach my portfolio document" → file_send, path=".", query="portfolio"
     "my resume should be somewhere under Downloads, find it and attach it"
         → file_search, query="resume", path="downloads"
     "send me resume- from downloads" → file_search, query="resume-", path="downloads"
     "find my resume and tell me where it is"
         → file_search, query="resume", path=".", requires_reasoning=true
     "find my resume, summarize it, and send it"
         → file_search, query="resume" (orchestrator handles chain)
     "can you get that document and send it to me?" → file_send, path=".", query="document"

5. web_search
   CAN DO: Search the live web for current, real-time, or recent information.
   USE when: the user wants current news, live prices, weather, or information that
   requires internet access to answer accurately.
   PARAMETERS:
     "query": search query
     "recency_days": optional int
     "max_results": optional int

6. browser
   CAN DO: Control a live browser to navigate pages, click elements, type, scroll, screenshot.
   USE when: the user wants to OPERATE a browser right now.
   PARAMETERS:
     "action": "open", "navigate", "observe", "click", "type", "press", "scroll", "screenshot"
     "url": target URL
     "selector": CSS selector or element description
     "text": text to type
     "direction": "down" or "up"
     "key": key name e.g. "Enter"

═══════════════════════════════════════════════════════════════════════════════
SEMANTIC DECISION RULES
═══════════════════════════════════════════════════════════════════════════════

A. TOOL DOMAIN
   Invoke a tool ONLY when the user's request requires AHJIN to OPERATE on something
   external right now: this machine's filesystem, this machine's system state, the
   live web, or a live browser session.

B. NO-TOOL CASES → output {"tool_name": "none", ...}
   Do NOT invoke a tool when the user is:
   - Asking a general knowledge question (history, geography, science, mathematics)
   - Requesting creative writing, stories, jokes, or poems
   - Asking for code generation, debugging help, or algorithm explanations
   - Asking AHJIN to reason about, summarize, or explain content that is ALREADY IN
     the user's current message (i.e., the content was pasted into the chat)

C. CRITICAL CONTEXT DISCRIMINATION — "find", "read", "extract" can appear in both
   tool and non-tool contexts. The DECIDING QUESTION is:
     "Is the content the user wants to work with ON THIS MACHINE (filesystem/system),
      or is it ALREADY IN THIS MESSAGE (pasted text, inline code, quoted paragraph)?"

   Content ON THIS MACHINE → tool required:
     "find my resume" → file_search (resume is a file on disk)
     "read my resume" → file_read (resume is a file on disk)
     "extract text from my PDF" → file_read (PDF is a file on disk)
     "what is the specs of my PC?" → system_info (specs are machine state, not in message)

   Content IN THIS MESSAGE → NO tool:
     "find the bug in this code: [... code block ...]" → none (code is in the message)
     "read this code and explain it: [... code block ...]" → none (code is in the message)
     "extract the key ideas from this paragraph: [... text ...]" → none (text is in the message)
     "analyze this algorithm: [... code ...]" → none (content is in the message)
     "write a function that reads a file" → none (code generation task, no file to read)

   When the user does NOT provide content in the message and asks about something that
   sounds like it might be on their machine (a resume, their computer's specs, etc.),
   assume it IS on the machine and invoke the appropriate tool.

D. requires_reasoning
   - false: pure operational actions where the tool result IS the complete answer
     (e.g. "send me my resume", "what OS?", "find my resume" — result speaks for itself)
   - true: user wants the model to reason, summarize, explain, or answer questions
     ABOUT the tool result (e.g. "summarize my resume", "explain my system specs",
     "find my resume and tell me where it is")

E. PATH EXTRACTION
   - Windows absolute path given (e.g. C:\\Users\\...\\file.pdf): preserve exactly in "path"
   - Directory path (ends with \\ or /): output as "path"; add "query" for file target
   - No path, file keyword only: set "path": ".", "query": "<keyword>"
   - Folder name mentioned (e.g. "downloads", "desktop", "archived"): use as "path"
   - Nested folder (e.g. "inside Downloads Archived"): combine as "downloads/archived"

═══════════════════════════════════════════════════════════════════════════════
OUTPUT EXAMPLES
═══════════════════════════════════════════════════════════════════════════════

{"tool_name": "system_info", "parameters": {"fields": ["all_safe"]},
 "requires_reasoning": false}
{"tool_name": "system_info", "parameters": {"fields": ["cpu", "memory"]},
 "requires_reasoning": false}
{"tool_name": "file_send", "parameters": {"path": ".", "query": "resume"},
 "requires_reasoning": false}
{"tool_name": "file_send",
 "parameters": {"path": "C:\\\\Users\\\\...\\\\Resume\\\\Resume-.pdf"},
 "requires_reasoning": false}
{"tool_name": "file_search",
 "parameters": {"query": "resume", "path": "downloads"},
 "requires_reasoning": false}
{"tool_name": "file_read", "parameters": {"path": ".", "query": "resume"},
 "requires_reasoning": true}
{"tool_name": "web_search",
 "parameters": {"query": "latest NVIDIA news"},
 "requires_reasoning": true}
{"tool_name": "browser",
 "parameters": {"action": "navigate", "url": "https://web.whatsapp.com"},
 "requires_reasoning": false}
{"tool_name": "none", "parameters": {}, "requires_reasoning": false}
"""


class ToolIntentPlanner:
    """LLM-assisted tool intent planner.

    Converts user input into a structured PlannerResult using MiniMax M3.
    Strictly enforces that tools exist in ToolRegistry and parameters match whitelists.
    Explicitly distinguishes between TOOL_SELECTED, NO_TOOL, and PLANNER_FAILURE.
    Does NOT execute tools or possess permission to perform actions.
    """

    def __init__(
        self,
        gateway: "ProviderGateway | None" = None,
        tool_registry: "ToolRegistry | None" = None,
        planner_timeout: float | None = None,
        max_attempts: int | None = None,
    ) -> None:
        self.gateway = gateway
        self.tool_registry = tool_registry
        self.planner_timeout = (
            planner_timeout if planner_timeout is not None else settings.tool_planner_timeout
        )
        self.max_attempts: int | None = max_attempts

    async def plan_tool_intent(self, text: str) -> PlannerResult:
        """Attempt to plan a structured tool invocation from natural language text.

        Uses model-agnostic fallback via ModelRouter: if the primary candidate fails
        (HTTP error, timeout, network error, invalid JSON), it is excluded and the next
        eligible model candidate from the catalog is attempted until the recovery budget
        is exhausted.

        Returns:
            A PlannerResult with status TOOL_SELECTED, NO_TOOL, or PLANNER_FAILURE.
            A failure (timeout, provider error, malformed JSON) is NEVER returned as NO_TOOL.
        """
        if self.gateway is None or self.tool_registry is None:
            logger.warning("ToolIntentPlanner: Gateway or tool registry is uninitialized")
            return PlannerResult(
                status=PlannerStatus.PLANNER_FAILURE,
                failure_reason="uninitialized_dependencies",
            )

        prompt = ContextualizedPrompt(
            system_instruction=_PLANNER_SYSTEM_PROMPT,
            user_instruction=text,
        )
        requirements = CapabilityRequirements(
            requires_reasoning=False,
            requires_code=False,
            requires_vision=False,
        )

        excluded_models: set[str] = set()
        attempts = 0
        last_failure_reason: str = "unknown"
        max_loop_iterations = (
            self.max_attempts if self.max_attempts is not None else _DEFENSIVE_MAX_ATTEMPTS
        )

        while attempts < max_loop_iterations:
            attempts += 1

            current_model_id: str | None = None
            if hasattr(self.gateway, "router"):
                try:
                    candidate = self.gateway.router.select_model(
                        requirements, excluded_model_ids=excluded_models
                    )
                    current_model_id = candidate.model_id
                except CapabilityUnavailableError:
                    if not last_failure_reason or last_failure_reason == "unknown":
                        last_failure_reason = "capability_unavailable"
                    logger.warning(
                        "ToolIntentPlanner: No capable models available from router",
                        attempt=attempts,
                        excluded_models=list(excluded_models),
                    )
                    break

            try:
                result = await asyncio.wait_for(
                    self.gateway.invoke(
                        prompt=prompt,
                        requirements=requirements,
                        excluded_model_ids=excluded_models,
                    ),
                    timeout=self.planner_timeout,
                )
                selected_model_id = result.selection.model_id
                raw_content = result.response.content.strip()
                if not raw_content:
                    logger.warning(
                        "ToolIntentPlanner: Model returned empty response",
                        model_id=selected_model_id,
                        attempt=attempts,
                    )
                    excluded_models.add(selected_model_id)
                    last_failure_reason = "empty_response"
                    continue

                # Clean possible markdown code fences if model included them
                if raw_content.startswith("```"):
                    lines = raw_content.splitlines()
                    raw_content = "\n".join(
                        [line for line in lines if not line.startswith("```")]
                    ).strip()

                try:
                    parsed_obj: Any = json.loads(raw_content)
                except json.JSONDecodeError:
                    # Try finding JSON substring between first { and last }
                    first_brace = raw_content.find("{")
                    last_brace = raw_content.rfind("}")
                    if first_brace != -1 and last_brace > first_brace:
                        try:
                            parsed_obj = json.loads(raw_content[first_brace : last_brace + 1])
                        except json.JSONDecodeError as exc:
                            logger.warning(
                                "ToolIntentPlanner: Model produced invalid JSON",
                                error=str(exc),
                                model_id=selected_model_id,
                                attempt=attempts,
                            )
                            excluded_models.add(selected_model_id)
                            last_failure_reason = "invalid_json"
                            continue
                    else:
                        logger.warning(
                            "ToolIntentPlanner: No JSON object found in response",
                            raw_preview=raw_content[:100],
                            model_id=selected_model_id,
                            attempt=attempts,
                        )
                        excluded_models.add(selected_model_id)
                        last_failure_reason = "invalid_json"
                        continue

                parsed_dict: dict[str, Any] = cast(dict[str, Any], parsed_obj)

                tool_name_val: Any = parsed_dict.get("tool_name")
                if not isinstance(tool_name_val, str) or not tool_name_val:
                    logger.warning(
                        "ToolIntentPlanner: Model response missing tool_name",
                        model_id=selected_model_id,
                        attempt=attempts,
                    )
                    excluded_models.add(selected_model_id)
                    last_failure_reason = "missing_tool_name"
                    continue

                # Explicit NO_TOOL state
                if tool_name_val.lower() == "none":
                    logger.info(
                        "ToolIntentPlanner: Determined no tool needed",
                        intent="conversational/reasoning",
                        model_id=selected_model_id,
                    )
                    return PlannerResult(status=PlannerStatus.NO_TOOL)

                tool_name: str = tool_name_val

                # 1. Authority Validation: Tool MUST exist in ToolRegistry
                if not self.tool_registry.has_tool(tool_name):
                    logger.warning(
                        "ToolIntentPlanner: Model requested unregistered tool",
                        requested_tool=tool_name,
                        model_id=selected_model_id,
                        attempt=attempts,
                    )
                    excluded_models.add(selected_model_id)
                    last_failure_reason = f"unregistered_tool:{tool_name}"
                    continue

                raw_params: Any = parsed_dict.get("parameters")
                parameters: dict[str, Any] = (
                    cast(dict[str, Any], raw_params) if isinstance(raw_params, dict) else {}
                )

                # 2. Parameter Whitelist Validation
                if tool_name == "system_info":
                    raw_fields: Any = parameters.get("fields")
                    field_list: list[Any] = (
                        cast(list[Any], raw_fields)
                        if isinstance(raw_fields, list)
                        else ["all_safe"]
                    )

                    valid_fields: list[str] = [
                        str(x)
                        for x in field_list
                        if isinstance(x, str) and x in SAFE_FIELDS_WHITELIST
                    ]
                    if not valid_fields:
                        logger.warning(
                            "ToolIntentPlanner: Model requested invalid fields for system_info",
                            requested_fields=raw_fields,
                            model_id=selected_model_id,
                            attempt=attempts,
                        )
                        excluded_models.add(selected_model_id)
                        last_failure_reason = "invalid_system_info_fields"
                        continue
                    parameters["fields"] = valid_fields

                elif tool_name == "file_search":
                    raw_query: Any = parameters.get("query")
                    if not isinstance(raw_query, str) or not raw_query.strip():
                        logger.warning(
                            "ToolIntentPlanner: Invalid or missing query for file_search",
                            model_id=selected_model_id,
                            attempt=attempts,
                        )
                        excluded_models.add(selected_model_id)
                        last_failure_reason = "invalid_file_search_query"
                        continue
                    parameters["query"] = raw_query.strip()
                    raw_path: Any = parameters.get("path")
                    if raw_path and isinstance(raw_path, str) and raw_path.strip():
                        parameters["path"] = raw_path.strip()

                elif tool_name == "file_read":
                    raw_path: Any = parameters.get("path")
                    raw_query: Any = parameters.get("query")
                    if not raw_path or not str(raw_path).strip():
                        if raw_query and str(raw_query).strip():
                            raw_path = "."
                        else:
                            logger.warning(
                                "ToolIntentPlanner: Invalid or missing path for file_read",
                                model_id=selected_model_id,
                                attempt=attempts,
                            )
                            excluded_models.add(selected_model_id)
                            last_failure_reason = "invalid_file_read_path"
                            continue
                    parameters["path"] = str(raw_path).strip()
                    if raw_query and isinstance(raw_query, str):
                        parameters["query"] = raw_query.strip()

                elif tool_name == "file_send":
                    raw_path: Any = parameters.get("path")
                    raw_query: Any = parameters.get("query")
                    if not raw_path or not str(raw_path).strip():
                        if raw_query and str(raw_query).strip():
                            raw_path = "."
                        else:
                            logger.warning(
                                "ToolIntentPlanner: Invalid or missing path for file_send",
                                model_id=selected_model_id,
                                attempt=attempts,
                            )
                            excluded_models.add(selected_model_id)
                            last_failure_reason = "invalid_file_send_path"
                            continue
                    parameters["path"] = str(raw_path).strip()
                    if raw_query and isinstance(raw_query, str):
                        parameters["query"] = raw_query.strip()

                elif tool_name == "web_search":
                    raw_query: Any = parameters.get("query")
                    if not isinstance(raw_query, str) or not raw_query.strip():
                        logger.warning(
                            "ToolIntentPlanner: Invalid or missing query for web_search",
                            model_id=selected_model_id,
                            attempt=attempts,
                        )
                        excluded_models.add(selected_model_id)
                        last_failure_reason = "invalid_web_search_query"
                        continue
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
                        if "url" in parameters:
                            parameters["action"] = "navigate"
                        else:
                            parameters["action"] = "observe"
                    else:
                        parameters["action"] = raw_action.strip().lower()

                raw_reasoning: Any = parsed_dict.get("requires_reasoning")
                requires_reasoning = (
                    bool(raw_reasoning) if isinstance(raw_reasoning, bool) else False
                )

                logger.info(
                    "ToolIntentPlanner: Planned structured tool intent",
                    tool_name=tool_name,
                    parameters=parameters,
                    requires_reasoning=requires_reasoning,
                    model_id=selected_model_id,
                    attempt=attempts,
                )
                req = ToolInvocationRequest(
                    tool_name=tool_name,
                    parameters=parameters,
                    requires_reasoning=requires_reasoning,
                )
                return PlannerResult(
                    status=PlannerStatus.TOOL_SELECTED,
                    tool_intent=req,
                )

            except asyncio.TimeoutError:
                failed_id = current_model_id
                if failed_id:
                    excluded_models.add(failed_id)
                logger.warning(
                    "ToolIntentPlanner: Intent planning timed out",
                    timeout_seconds=self.planner_timeout,
                    failed_model=failed_id,
                    attempt=attempts,
                    max_attempts=max_loop_iterations,
                )
                last_failure_reason = "timeout"
                continue

            except CapabilityUnavailableError as cap_err:
                logger.warning(
                    "ToolIntentPlanner: No capable models available",
                    error=str(cap_err),
                    attempt=attempts,
                )
                if not last_failure_reason or last_failure_reason == "unknown":
                    last_failure_reason = "capability_unavailable"
                break

            except (httpx.HTTPStatusError, httpx.RequestError) as http_err:
                failed_id = getattr(http_err, "model_id", current_model_id)
                if failed_id:
                    excluded_models.add(str(failed_id))
                logger.warning(
                    "ToolIntentPlanner: Planning provider HTTP/network error",
                    error=str(http_err),
                    failed_model=failed_id,
                    attempt=attempts,
                    max_attempts=max_loop_iterations,
                )
                last_failure_reason = f"provider_error: {http_err}"
                continue

            except Exception as exc:
                failed_id = getattr(exc, "model_id", current_model_id)
                if failed_id:
                    excluded_models.add(str(failed_id))
                logger.warning(
                    "ToolIntentPlanner: Planning provider unhandled error",
                    error=str(exc),
                    failed_model=failed_id,
                    attempt=attempts,
                    max_attempts=max_loop_iterations,
                )
                last_failure_reason = f"provider_error: {exc}"
                continue

        logger.warning(
            "ToolIntentPlanner: All planning attempts exhausted or failed",
            attempts=attempts,
            max_attempts=max_loop_iterations,
            failure_reason=last_failure_reason,
            excluded_models=list(excluded_models),
        )
        return PlannerResult(
            status=PlannerStatus.PLANNER_FAILURE,
            failure_reason=last_failure_reason,
        )

