"""BERU Orchestrator — Strategic cognitive decision engine.

BERU is the strategic cognitive orchestration layer.
BERU understands the TASK and determines:
- required capabilities
- execution strategy (tier preference, quality preference, recovery policy)
- verification requirements

BERU contains:
  ZERO model IDs
  ZERO provider IDs
  ZERO API endpoints
  ZERO hardcoded fallback chains

ModelRouter is responsible for resolving the concrete model.
"""

import time
from typing import TYPE_CHECKING

import structlog

from ahjin.beru.tool_planner import PlannerResult, PlannerStatus
from ahjin.beru.tools import detect_tool_intent, may_require_tool
from ahjin.beru.types import (
    CapabilityRequirements,
    ExecutionPlan,
    ExecutionStrategy,
    ModelStepIntent,
    PlanStep,
    RecoveryPolicy,
    StepType,
)
from ahjin.core.types import TaskRequest
from ahjin.models.types import ModelTier
from ahjin.security.path_policy import SafePathPolicy
from ahjin.telemetry import (
    STAGE_BERU_ANALYSIS,
    STAGE_TOOL_PLANNER,
    STAGE_TOOL_RESOLVER,
    STAGE_TOOL_SCREENING,
    RequestTimer,
)
from ahjin.tools.base import ToolInvocationRequest

if TYPE_CHECKING:
    from ahjin.beru.tool_planner import ToolIntentPlanner

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Keyword sets for task understanding heuristics.
# These represent BERU's current V2 task signal extraction mechanism.
# They are intentionally static and deterministic.
# Future improvement: richer NLP / intent classification without LLM calls.
# ---------------------------------------------------------------------------

_CODING_KEYWORDS: frozenset[str] = frozenset({
    "code", "python", "javascript", "typescript", "java", "c++", "golang",
    "function", "algorithm", "debug", "implement", "script", "program",
    "class", "module", "refactor", "compile", "syntax", "programming",
})

_REASONING_KEYWORDS: frozenset[str] = frozenset({
    "explain", "analyze", "analyse", "reason", "why", "compare",
    "solve", "math", "proof", "detailed", "evaluate", "assess",
    "summarize", "summarise", "think", "logical", "derive", "theorem",
    "calculate", "estimate", "infer",
})

# Multi-word vision phrases must be matched against the full text string,
# not against a split-word set (single words are matched against the token set).
_VISION_KEYWORDS: frozenset[str] = frozenset({
    "image", "picture", "photo", "screenshot", "diagram", "chart",
    "figure", "visual", "render", "pixel", "thumbnail",
})

_VISION_PHRASES: tuple[str, ...] = (
    "look at",
    "look at this",
    "in this image",
    "in this picture",
    "in this photo",
    "this image shows",
    "this picture shows",
    "attached image",
    "see the image",
    "see the photo",
    "see the picture",
)


class BeruOrchestrator:
    """BERU engine for task understanding, execution strategy, and plan generation.

    Decides WHAT capabilities and strategy are required.
    Contains ZERO model IDs, provider names, or API endpoints.
    """

    def __init__(
        self,
        tool_planner: "ToolIntentPlanner | None" = None,
        path_policy: SafePathPolicy | None = None,
    ) -> None:
        self.tool_planner = tool_planner
        self._path_policy = path_policy

    @property
    def path_policy(self) -> SafePathPolicy:
        if self._path_policy is not None:
            return self._path_policy
        if self.tool_planner:
            reg = getattr(self.tool_planner, "tool_registry", None)
            if reg and hasattr(reg, "get_tool"):
                for name in ("file_send", "file_read", "file_search"):
                    try:
                        if reg.has_tool(name):
                            t = reg.get_tool(name)
                            p = getattr(t, "path_policy", None)
                            if isinstance(p, SafePathPolicy):
                                return p
                    except Exception:
                        pass
        return SafePathPolicy()

    @path_policy.setter
    def path_policy(self, policy: SafePathPolicy | None) -> None:
        self._path_policy = policy

    def analyze_task_requirements(self, text: str) -> CapabilityRequirements:
        """Analyze task text to determine provider-agnostic capability requirements.

        Uses deterministic keyword and phrase heuristics.
        Multi-word vision phrases are checked against the full text (not split tokens)
        to avoid false negatives like "look at" being broken into ["look", "at"].
        """
        lower_text = text.lower()
        words = frozenset(lower_text.split())

        requires_code = bool(words & _CODING_KEYWORDS)
        requires_reasoning = bool(words & _REASONING_KEYWORDS)

        # Single-word vision keywords
        requires_vision = bool(words & _VISION_KEYWORDS)
        # Multi-word vision phrases — must check against full string
        if not requires_vision:
            requires_vision = any(phrase in lower_text for phrase in _VISION_PHRASES)

        return CapabilityRequirements(
            requires_reasoning=requires_reasoning,
            requires_code=requires_code,
            requires_vision=requires_vision,
        )

    async def plan(
        self,
        request: TaskRequest,
        timer: RequestTimer | None = None,
    ) -> ExecutionPlan:
        """Analyze TaskRequest and produce ExecutionPlan with ExecutionStrategy."""
        t0 = time.monotonic()
        text = request.intent.primary_text
        logger.info("[PROFILE] BERU planning start", task_id=str(request.task_id))

        if timer is not None:
            timer.start_stage(STAGE_BERU_ANALYSIS)

        # --- Phase 1: Deterministic resolver (zero latency, always runs first) ---
        # detect_tool_intent() is a pure-Python prefix/phrase matcher with O(n) cost
        # measured in microseconds. When it returns a match it is HIGH-CONFIDENCE and
        # the LLM planner adds NO additional value — skip it unconditionally.
        if timer is not None:
            timer.start_stage(STAGE_TOOL_RESOLVER)
        tool_intent: ToolInvocationRequest | None = detect_tool_intent(text)
        if timer is not None:
            timer.end_stage(STAGE_TOOL_RESOLVER)

        # --- Phase 2: LLM Tool Intent Planner (only for ambiguous requests) ---
        # Invoked ONLY when:
        #   a) detect_tool_intent() found nothing (not an obvious deterministic pattern), AND
        #   b) may_require_tool() signals keyword/phrase tool-potential (avoids invoking
        #      the planner on plain conversation like "HI" or "Write a poem").
        if tool_intent is None:
            if timer is not None:
                timer.start_stage(STAGE_TOOL_SCREENING)
            needs_planner = self.tool_planner is not None and may_require_tool(text)
            if timer is not None:
                timer.end_stage(STAGE_TOOL_SCREENING)

            if needs_planner:
                if timer is not None:
                    timer.start_stage(STAGE_TOOL_PLANNER)
                planner_res: PlannerResult = await self.tool_planner.plan_tool_intent(text)  # type: ignore[union-attr]
                if timer is not None:
                    timer.end_stage(STAGE_TOOL_PLANNER)

                if planner_res.status == PlannerStatus.TOOL_SELECTED:
                    tool_intent = planner_res.tool_intent
                elif planner_res.status == PlannerStatus.PLANNER_FAILURE:
                    fail_reason = planner_res.failure_reason or "Intent planning unavailable"
                    fail_step = PlanStep(
                        step_type=StepType.TOOL_INVOCATION,
                        deterministic_output=(
                            "⚠️ Tool intent planning was unable to process your request "
                            f"({fail_reason}). Please retry or clarify your request."
                        ),
                    )
                    if timer is not None:
                        timer.end_stage(STAGE_BERU_ANALYSIS)
                    return ExecutionPlan(
                        task_id=request.task_id,
                        correlation_id=request.correlation_id,
                        steps=[fail_step],
                    )

        if tool_intent is not None:
            logger.info(
                "[PROFILE] BERU selected tool execution",
                task_id=str(request.task_id),
                tool_name=tool_intent.tool_name,
                parameters=tool_intent.parameters,
            )
            steps: list[PlanStep] = [
                PlanStep(
                    step_type=StepType.TOOL_INVOCATION,
                    tool_intent=tool_intent,
                )
            ]

            # Combined Intent Orchestration: Handle file_search, file_send, file_read chaining
            lower_text = text.lower()
            send_kw = ("send", "attach", "give me", "upload", "share")
            read_kw = (
                "summarize", "summary", "explain", "read", "tell me", "what does", "analyze"
            )
            has_send = any(k in lower_text for k in send_kw)
            has_read = any(k in lower_text for k in read_kw)

            # Enforce primary execution architecture for file send/retrieve:
            # semantic planner -> file_search -> actual path -> file_send(actual path)
            if tool_intent.tool_name in ("file_send", "file_read"):
                raw_path = str(tool_intent.parameters.get("path", "")).strip()
                query_val = tool_intent.parameters.get("query")
                is_exact_file = False
                if raw_path and raw_path not in (".", "", "downloads", "desktop", "documents"):
                    is_safe_exact, resolved_file, exact_err = (
                        self.path_policy.validate_safe_path(raw_path)
                    )
                    if not is_safe_exact:
                        fail_reason = exact_err or f"Access to path '{raw_path}' denied."
                        err_step = PlanStep(
                            step_type=StepType.TOOL_INVOCATION,
                            deterministic_output=f"⚠️ Access denied: {fail_reason}",
                        )
                        if timer is not None:
                            timer.end_stage(STAGE_BERU_ANALYSIS)
                        return ExecutionPlan(
                            task_id=request.task_id,
                            correlation_id=request.correlation_id,
                            steps=[err_step],
                        )
                    if resolved_file is not None and resolved_file.is_file():
                        is_exact_file = True
                        tool_intent.parameters["path"] = str(resolved_file)

                if not is_exact_file:
                    if query_val:
                        search_query = str(query_val).strip()
                    else:
                        clean_text = text.replace(raw_path, "").strip() if raw_path else ""
                        search_query = clean_text or text

                    if raw_path and raw_path not in (".", ""):
                        is_safe, roots, err_reason = self.path_policy.get_search_roots(raw_path)
                        if is_safe and roots:
                            search_path = raw_path
                        else:
                            fail_reason = err_reason or f"Access to path '{raw_path}' denied."
                            err_step = PlanStep(
                                step_type=StepType.TOOL_INVOCATION,
                                deterministic_output=f"⚠️ Access denied: {fail_reason}",
                            )
                            if timer is not None:
                                timer.end_stage(STAGE_BERU_ANALYSIS)
                            return ExecutionPlan(
                                task_id=request.task_id,
                                correlation_id=request.correlation_id,
                                steps=[err_step],
                            )
                    else:
                        search_path = "."

                    search_intent = ToolInvocationRequest(
                        tool_name="file_search",
                        parameters={"query": search_query, "path": search_path},
                        requires_reasoning=tool_intent.requires_reasoning,
                    )
                    steps = [
                        PlanStep(step_type=StepType.TOOL_INVOCATION, tool_intent=search_intent),
                        PlanStep(step_type=StepType.TOOL_INVOCATION, tool_intent=tool_intent),
                    ]
                    if tool_intent.tool_name == "file_send" and has_read:
                        read_intent = ToolInvocationRequest(
                            tool_name="file_read",
                            parameters={"path": search_path, "query": search_query},
                        )
                        steps.append(
                            PlanStep(step_type=StepType.TOOL_INVOCATION, tool_intent=read_intent)
                        )
                    elif tool_intent.tool_name == "file_read" and has_send:
                        send_intent = ToolInvocationRequest(
                            tool_name="file_send",
                            parameters={"path": search_path, "query": search_query},
                        )
                        steps.append(
                            PlanStep(step_type=StepType.TOOL_INVOCATION, tool_intent=send_intent)
                        )
            elif tool_intent.tool_name == "file_search":
                read_intent = ToolInvocationRequest(
                    tool_name="file_read",
                    parameters=dict(tool_intent.parameters),
                )
                send_intent = ToolInvocationRequest(
                    tool_name="file_send",
                    parameters=dict(tool_intent.parameters),
                )
                if has_send and has_read:
                    steps.append(
                        PlanStep(step_type=StepType.TOOL_INVOCATION, tool_intent=read_intent)
                    )
                    steps.append(
                        PlanStep(step_type=StepType.TOOL_INVOCATION, tool_intent=send_intent)
                    )
                elif has_read:
                    steps.append(
                        PlanStep(step_type=StepType.TOOL_INVOCATION, tool_intent=read_intent)
                    )
                elif has_send:
                    steps.append(
                        PlanStep(step_type=StepType.TOOL_INVOCATION, tool_intent=send_intent)
                    )
            elif has_send and has_read:
                # If primary tool was file_read, add file_send step
                if tool_intent.tool_name == "file_read":
                    send_intent = ToolInvocationRequest(
                        tool_name="file_send",
                        parameters=dict(tool_intent.parameters),
                    )
                    steps.insert(
                        0,
                        PlanStep(step_type=StepType.TOOL_INVOCATION, tool_intent=send_intent),
                    )

            requires_reasoning = getattr(tool_intent, "requires_reasoning", False)
            # If plan includes file_read for content interpretation, reasoning is required
            has_read_step = any(
                s.tool_intent and s.tool_intent.tool_name == "file_read" for s in steps
            )
            if has_read_step and has_read:
                requires_reasoning = True

            model_step = PlanStep(
                step_type=StepType.MODEL_INVOCATION,
                model_intent=ModelStepIntent(
                    instruction=text,
                    capability_requirements=CapabilityRequirements(
                        requires_reasoning=requires_reasoning,
                    ),
                    execution_strategy=ExecutionStrategy(preferred_tier="FAST"),
                ),
            )
            steps.append(model_step)

            if timer is not None:
                timer.end_stage(STAGE_BERU_ANALYSIS)
            return ExecutionPlan(
                task_id=request.task_id,
                correlation_id=request.correlation_id,
                steps=steps,
            )

        reqs = self.analyze_task_requirements(text)

        # Strategic tier selection based on task cognitive demands
        prefer_heavy = reqs.requires_reasoning or reqs.requires_code or reqs.requires_vision
        target_tier = ModelTier.HEAVY if prefer_heavy else ModelTier.FAST

        # quality_preference: heavy tasks prioritise correctness; simple tasks prioritise speed.
        quality_preference = "quality" if prefer_heavy else "speed"

        strategy = ExecutionStrategy(
            capability_requirements=reqs,
            preferred_tier=target_tier,
            max_recovery_attempts=2,
            require_verification=True,
            recovery_policy=RecoveryPolicy.REROUTE,
            quality_preference=quality_preference,
        )

        model_intent = ModelStepIntent(
            instruction=text,
            capability_requirements=reqs,
            execution_strategy=strategy,
        )

        step = PlanStep(
            step_type=StepType.MODEL_INVOCATION,
            model_intent=model_intent,
        )

        plan_res = ExecutionPlan(
            task_id=request.task_id,
            correlation_id=request.correlation_id,
            steps=[step],
        )

        t_beru_ms = (time.monotonic() - t0) * 1000.0
        if timer is not None:
            timer.end_stage(STAGE_BERU_ANALYSIS)
        logger.info(
            "[PROFILE] BERU planning end",
            task_id=str(request.task_id),
            planning_ms=round(t_beru_ms, 3),
            target_tier=target_tier.value,
            quality_preference=quality_preference,
            requires_reasoning=reqs.requires_reasoning,
            requires_code=reqs.requires_code,
            requires_vision=reqs.requires_vision,
        )
        return plan_res
