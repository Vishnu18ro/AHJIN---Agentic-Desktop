"""Cognitive orchestration types owned by BERU."""

from datetime import datetime, timezone
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from ahjin.tools.base import ToolInvocationRequest


class StepType(str, Enum):
    """Execution step classification."""

    MODEL_INVOCATION = "MODEL_INVOCATION"
    TOOL_INVOCATION = "TOOL_INVOCATION"
    AGENT_INVOCATION = "AGENT_INVOCATION"
    VERIFICATION = "VERIFICATION"


class CapabilityRequirements(BaseModel):
    """Provider-agnostic capability requirements specified by BERU."""

    requires_reasoning: bool = False
    requires_code: bool = False
    requires_vision: bool = False
    max_latency_ms: int | None = None


class RecoveryPolicy(str, Enum):
    """BERU recovery behavior on invocation failure."""

    REROUTE = "REROUTE"
    FAIL_FAST = "FAIL_FAST"


class ExecutionStrategy(BaseModel):
    """Provider/model-agnostic strategic task execution plan determined by BERU."""

    capability_requirements: CapabilityRequirements = Field(default_factory=CapabilityRequirements)
    preferred_tier: str = "FAST"
    max_recovery_attempts: int = 2
    require_verification: bool = True
    recovery_policy: RecoveryPolicy = RecoveryPolicy.REROUTE
    quality_preference: str = "balanced"  # "speed", "quality", "balanced"


class ModelStepIntent(BaseModel):
    """Model instruction and strategic intent specified by BERU."""

    instruction: str
    capability_requirements: CapabilityRequirements = Field(default_factory=CapabilityRequirements)
    execution_strategy: ExecutionStrategy = Field(default_factory=ExecutionStrategy)


class PlanStep(BaseModel):
    """Single unit of work in an ExecutionPlan."""

    step_id: UUID = Field(default_factory=uuid4)
    step_type: StepType = StepType.MODEL_INVOCATION
    model_intent: ModelStepIntent | None = None
    tool_intent: ToolInvocationRequest | None = None
    depends_on: list[UUID] = Field(default_factory=list)  # pyright: ignore[reportUnknownVariableType]
    timeout_seconds: float = 30.0


class ExecutionPlan(BaseModel):
    """Cognitive execution plan produced by BERU."""

    plan_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    correlation_id: UUID
    steps: list[PlanStep]
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
