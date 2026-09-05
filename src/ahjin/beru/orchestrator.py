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

from ahjin.beru.tools import detect_tool_intent
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

    def __init__(self, tool_planner: "ToolIntentPlanner | None" = None) -> None:
        self.tool_planner = tool_planner

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

    async def plan(self, request: TaskRequest) -> ExecutionPlan:
        """Analyze TaskRequest and produce ExecutionPlan with ExecutionStrategy."""
        t0 = time.monotonic()
        text = request.intent.primary_text
        logger.info("[PROFILE] BERU planning start", task_id=str(request.task_id))

        # 1. Hybrid Tool Planning: Try LLM Tool Intent Planner first,
        # fallback to deterministic resolver
        tool_intent: ToolInvocationRequest | None = None
        if self.tool_planner is not None:
            tool_intent = await self.tool_planner.plan_tool_intent(text)

        if tool_intent is None:
            tool_intent = detect_tool_intent(text)

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

            # Combined Intent Orchestration: If request asks to SEND AND READ/ANALYZE
            lower_text = text.lower()
            send_kw = ("send", "attach", "give me", "upload", "share")
            read_kw = (
                "summarize", "summary", "explain", "read", "tell me", "what does", "analyze"
            )
            has_send = any(k in lower_text for k in send_kw)
            has_read = any(k in lower_text for k in read_kw)

            if has_send and has_read:
                # If primary tool was file_send, add file_read step
                if tool_intent.tool_name == "file_send":
                    read_intent = ToolInvocationRequest(
                        tool_name="file_read",
                        parameters=tool_intent.parameters,
                    )
                    steps.append(
                        PlanStep(step_type=StepType.TOOL_INVOCATION, tool_intent=read_intent)
                    )
                # If primary tool was file_read, add file_send step
                elif tool_intent.tool_name == "file_read":
                    send_intent = ToolInvocationRequest(
                        tool_name="file_send",
                        parameters=tool_intent.parameters,
                    )
                    steps.insert(
                        0,
                        PlanStep(step_type=StepType.TOOL_INVOCATION, tool_intent=send_intent),
                    )

            model_step = PlanStep(
                step_type=StepType.MODEL_INVOCATION,
                model_intent=ModelStepIntent(
                    instruction=text,
                    execution_strategy=ExecutionStrategy(preferred_tier="FAST"),
                ),
            )
            steps.append(model_step)

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
