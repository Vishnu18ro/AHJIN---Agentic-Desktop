"""Unit tests for Progress-Aware Watchdogs and Internal Planner Forensics.

Validates:
1. ToolIntentPlanner 15s inactivity watchdog:
   - Silent candidate times out after watchdog interval.
   - reasoning_content resets the inactivity timer and keeps the candidate alive.
   - Visible content resets the inactivity timer and satisfies the pre-content phase.
   - Each candidate receives an independent watchdog window.
   - PlannerAttemptTelemetry is accurately populated for all attempts.
2. HarnessRunner 30s provider startup watchdog:
   - Silent candidate times out during pre-response phase and triggers rerouting.
   - reasoning_content resets the startup watchdog.
   - Visible content satisfies the startup watchdog, allowing natural completion with NO 30s limit.
   - Each fallback candidate receives an independent fresh startup window.
   - Timeout classifies properly as "timeout" and records failure in health tracker.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any
from uuid import uuid4

import pytest

from ahjin.beru.tool_planner import PlannerStatus, ToolIntentPlanner
from ahjin.beru.types import (
    CapabilityRequirements,
    ExecutionPlan,
    ExecutionStrategy,
    ModelStepIntent,
    PlanStep,
    RecoveryPolicy,
    StepType,
)
from ahjin.core.types import TaskContext
from ahjin.harness.gateway import ProviderGateway
from ahjin.harness.runner import HarnessRunner
from ahjin.models.catalog import ModelCatalog
from ahjin.models.router import ModelRouter
from ahjin.models.types import ModelCapabilities, ModelDescriptor, ModelTier
from ahjin.providers.base import BaseModelProvider
from ahjin.providers.registry import ProviderRegistry
from ahjin.providers.types import (
    FinishReason,
    ModelInvocationRequest,
    ModelInvocationResponse,
    StreamChunk,
)
from ahjin.tools import FileSearchTool, ToolRegistry


class WatchdogMockProvider(BaseModelProvider):
    """Mock provider allowing fine-grained control of stream chunk timing and types."""

    def __init__(
        self,
        provider_id: str,
        stream_schedules: dict[str, list[tuple[float, Any]]],
    ) -> None:
        self._provider_id = provider_id
        self._stream_schedules = stream_schedules
        self.invoked_models: list[str] = []

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def get_default_model_id(self) -> str:
        return "default-mock"

    async def invoke(self, request: ModelInvocationRequest) -> ModelInvocationResponse:
        chunks: list[str] = []
        async for chunk in self.invoke_stream(request):
            if chunk:
                chunks.append(chunk)
        return ModelInvocationResponse(
            invocation_id=request.invocation_id,
            content="".join(chunks),
            provider_id=self.provider_id,
            model_id=request.model_id or "default-mock",
            finish_reason=FinishReason.COMPLETE,
            latency_ms=10.0,
        )

    async def invoke_stream(self, request: ModelInvocationRequest) -> AsyncGenerator[Any, None]:
        model = request.model_id or "default-mock"
        self.invoked_models.append(model)
        schedule = self._stream_schedules.get(model, [])
        for delay, item in schedule:
            if delay > 0:
                await asyncio.sleep(delay)
            if isinstance(item, Exception):
                raise item
            yield item


def _create_test_catalog(model_ids: list[str]) -> ModelCatalog:
    catalog = ModelCatalog()
    for i, mid in enumerate(model_ids):
        desc = ModelDescriptor(
            model_id=mid,
            provider_id="watchdog_mock",
            tier=ModelTier.FAST,
            capabilities=ModelCapabilities(
                supports_streaming=True,
                supports_tools=True,
                max_tokens=4096,
            ),
            cost_per_1k_input_tokens=0.001,
            cost_per_1k_output_tokens=0.002,
            priority=100 - i,  # Earlier models have higher priority
        )
        catalog.register(desc)
    return catalog


# ---------------------------------------------------------------------------
# 1. TOOL INTENT PLANNER WATCHDOG TESTS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_planner_inactivity_watchdog_times_out_on_silence() -> None:
    """A candidate that produces no progress for candidate_watchdog_seconds times out."""
    catalog = _create_test_catalog(["model-silent", "model-fallback"])
    registry = ProviderRegistry()
    provider = WatchdogMockProvider(
        provider_id="watchdog_mock",
        stream_schedules={
            "model-silent": [
                (
                    0.2,
                    '{"tool_name": "file_search", "parameters": {"path": ".", "query": "silent"}}',
                )
            ],
            "model-fallback": [
                (
                    0.01,
                    (
                        '{"tool_name": "file_search", '
                        '"parameters": {"path": ".", "query": "fallback"}}'
                    ),
                )
            ],
        },
    )
    registry.register(provider)
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)

    tool_registry = ToolRegistry()
    tool_registry.register(FileSearchTool())

    # 0.05s watchdog: model-silent delays 0.2s so it will time out at 0.05s
    planner = ToolIntentPlanner(
        gateway=gateway,
        tool_registry=tool_registry,
        planner_timeout=0.05,
    )

    result = await planner.plan_tool_intent("find silent file")

    assert result.status == PlannerStatus.TOOL_SELECTED
    assert result.selected_model == "model-fallback"
    assert result.was_rerouted is True
    assert result.first_failed_model == "model-silent"
    assert result.first_failure_reason == "timeout"
    assert "model-silent" in result.attempted_models
    assert "model-fallback" in result.attempted_models

    # Check attempt telemetry
    assert len(result.attempts_telemetry) == 2
    att1 = result.attempts_telemetry[0]
    assert att1.model_id == "model-silent"
    assert "TIMEOUT" in att1.outcome
    assert att1.error_reason == "timeout"
    assert att1.fallback_proceeded is True

    att2 = result.attempts_telemetry[1]
    assert att2.model_id == "model-fallback"
    assert "SUCCESS" in att2.outcome
    assert att2.fallback_proceeded is False


@pytest.mark.asyncio
async def test_planner_reasoning_progress_resets_watchdog() -> None:
    """Reasoning chunks keep the candidate alive even if total time exceeds the watchdog."""
    catalog = _create_test_catalog(["model-reasoning"])
    registry = ProviderRegistry()

    # Watchdog is 0.20s.
    # Model emits 4 reasoning chunks every 0.06s (total ~0.24s > 0.20s watchdog).
    # Then emits valid plan JSON.
    reasoning_chunk = StreamChunk("", is_progress=True, is_reasoning=True)
    provider = WatchdogMockProvider(
        provider_id="watchdog_mock",
        stream_schedules={
            "model-reasoning": [
                (0.06, reasoning_chunk),
                (0.06, reasoning_chunk),
                (0.06, reasoning_chunk),
                (0.06, reasoning_chunk),
                (0.01, '{"tool_name": "file_search", "parameters": {"path": ".", "query": "cv"}}'),
            ],
        },
    )
    registry.register(provider)
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)

    tool_registry = ToolRegistry()
    tool_registry.register(FileSearchTool())

    planner = ToolIntentPlanner(
        gateway=gateway,
        tool_registry=tool_registry,
        planner_timeout=0.20,
    )

    result = await planner.plan_tool_intent("find my cv")

    assert result.status == PlannerStatus.TOOL_SELECTED
    assert result.selected_model == "model-reasoning"
    assert result.was_rerouted is False
    assert len(result.attempts_telemetry) == 1
    att = result.attempts_telemetry[0]
    assert "SUCCESS" in att.outcome
    assert att.activity_type == "reasoning_content"
    assert att.first_activity_ms is not None
    assert att.first_activity_ms < 150.0  # Arrived within first 60ms interval


@pytest.mark.asyncio
async def test_planner_independent_watchdog_per_candidate() -> None:
    """Each candidate receives its own fresh watchdog timer."""
    catalog = _create_test_catalog(["model-1", "model-2", "model-3"])
    registry = ProviderRegistry()

    # Candidate 1: silent for 0.1s -> times out at 0.05s
    # Candidate 2: silent for 0.1s -> times out at 0.05s
    # Candidate 3: responds within 0.02s -> succeeds
    provider = WatchdogMockProvider(
        provider_id="watchdog_mock",
        stream_schedules={
            "model-1": [(0.1, "too late")],
            "model-2": [(0.1, "too late")],
            "model-3": [(0.02, '{"tool_name": "none"}')],
        },
    )
    registry.register(provider)
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)

    tool_registry = ToolRegistry()

    planner = ToolIntentPlanner(
        gateway=gateway,
        tool_registry=tool_registry,
        planner_timeout=0.05,
    )

    result = await planner.plan_tool_intent("hello")

    assert result.status == PlannerStatus.NO_TOOL
    assert result.selected_model == "model-3"
    assert len(result.attempts_telemetry) == 3
    assert "TIMEOUT" in result.attempts_telemetry[0].outcome
    assert "TIMEOUT" in result.attempts_telemetry[1].outcome
    assert "SUCCESS" in result.attempts_telemetry[2].outcome


# ---------------------------------------------------------------------------
# 2. HARNESS RUNNER WATCHDOG TESTS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_harness_startup_watchdog_times_out_on_silence() -> None:
    """Harness candidate producing zero progress within startup_timeout times out & reroutes."""
    catalog = _create_test_catalog(["harness-silent", "harness-active"])
    registry = ProviderRegistry()

    # Candidate 1: silent for 0.2s -> times out on 0.05s startup watchdog
    # Candidate 2: active immediately -> succeeds
    provider = WatchdogMockProvider(
        provider_id="watchdog_mock",
        stream_schedules={
            "harness-silent": [(0.2, "late chunk")],
            "harness-active": [
                (0.01, "Hello "),
                (0.01, "world!"),
            ],
        },
    )
    registry.register(provider)
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)

    runner = HarnessRunner(
        gateway=gateway,
        startup_timeout=0.05,
    )

    plan = ExecutionPlan(
        task_id=uuid4(),
        correlation_id=uuid4(),
        steps=[
            PlanStep(
                step_id=uuid4(),
                step_type=StepType.MODEL_INVOCATION,
                model_intent=ModelStepIntent(
                    instruction="say hello",
                    capability_requirements=CapabilityRequirements(),
                    execution_strategy=ExecutionStrategy(
                        require_verification=False,
                        recovery_policy=RecoveryPolicy.REROUTE,
                    ),
                ),
            )
        ],
    )
    context = TaskContext(session_id="test", memory_scope="test")

    chunks: list[str] = []
    final_result = None
    async for chunk, res in runner.run_stream(plan, context):
        if chunk:
            chunks.append(chunk)
        if res is not None:
            final_result = res

    assert "".join(chunks) == "Hello world!"
    assert final_result is not None
    assert final_result.runtime_info is not None
    info = final_result.runtime_info
    assert info.selected_model == "harness-active"
    assert info.was_rerouted is True
    assert info.failed_model == "harness-silent"
    assert info.failure_reason == "timeout"
    assert "harness-silent" in info.harness_route_history
    assert "harness-active" in info.harness_route_history


@pytest.mark.asyncio
async def test_harness_reasoning_progress_resets_startup_watchdog() -> None:
    """Reasoning chunks reset Harness startup watchdog and candidate is NOT killed."""
    catalog = _create_test_catalog(["harness-reasoner"])
    registry = ProviderRegistry()

    # Startup timeout is 0.20s.
    # Candidate emits 4 reasoning chunks every 0.06s (~0.24s total > 0.20s).
    # Then emits visible content.
    reasoning_chunk = StreamChunk("", is_progress=True, is_reasoning=True)
    provider = WatchdogMockProvider(
        provider_id="watchdog_mock",
        stream_schedules={
            "harness-reasoner": [
                (0.06, reasoning_chunk),
                (0.06, reasoning_chunk),
                (0.06, reasoning_chunk),
                (0.06, reasoning_chunk),
                (0.01, "Final response content"),
            ],
        },
    )
    registry.register(provider)
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)

    runner = HarnessRunner(
        gateway=gateway,
        startup_timeout=0.20,
    )

    plan = ExecutionPlan(
        task_id=uuid4(),
        correlation_id=uuid4(),
        steps=[
            PlanStep(
                step_id=uuid4(),
                step_type=StepType.MODEL_INVOCATION,
                model_intent=ModelStepIntent(
                    instruction="say final",
                    capability_requirements=CapabilityRequirements(),
                    execution_strategy=ExecutionStrategy(
                        require_verification=False,
                        recovery_policy=RecoveryPolicy.FAIL_FAST,
                    ),
                ),
            )
        ],
    )
    context = TaskContext(session_id="test", memory_scope="test")

    chunks: list[str] = []
    final_result = None
    async for chunk, res in runner.run_stream(plan, context):
        if chunk:
            chunks.append(chunk)
        if res is not None:
            final_result = res

    assert "".join(chunks) == "Final response content"
    assert final_result is not None
    assert final_result.runtime_info is not None
    info = final_result.runtime_info
    assert info.selected_model == "harness-reasoner"
    assert info.was_rerouted is False


@pytest.mark.asyncio
async def test_harness_natural_generation_has_no_startup_timeout_limit() -> None:
    """Once visible content arrives, generation completes naturally with NO startup timeout."""
    catalog = _create_test_catalog(["harness-long-gen"])
    registry = ProviderRegistry()

    # Startup timeout is 0.05s.
    # First token arrives at 0.01s (satisfying startup watchdog).
    # Next tokens arrive over 0.15s (far exceeding 0.05s startup timeout).
    provider = WatchdogMockProvider(
        provider_id="watchdog_mock",
        stream_schedules={
            "harness-long-gen": [
                (0.01, "Part 1 "),
                (0.05, "Part 2 "),
                (0.05, "Part 3 "),
                (0.05, "Part 4"),
            ],
        },
    )
    registry.register(provider)
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)

    runner = HarnessRunner(
        gateway=gateway,
        startup_timeout=0.05,
    )

    plan = ExecutionPlan(
        task_id=uuid4(),
        correlation_id=uuid4(),
        steps=[
            PlanStep(
                step_id=uuid4(),
                step_type=StepType.MODEL_INVOCATION,
                model_intent=ModelStepIntent(
                    instruction="generate parts",
                    capability_requirements=CapabilityRequirements(),
                    execution_strategy=ExecutionStrategy(
                        require_verification=False,
                        recovery_policy=RecoveryPolicy.FAIL_FAST,
                    ),
                ),
            )
        ],
    )
    context = TaskContext(session_id="test", memory_scope="test")

    chunks: list[str] = []
    final_result = None
    async for chunk, res in runner.run_stream(plan, context):
        if chunk:
            chunks.append(chunk)
        if res is not None:
            final_result = res

    assert "".join(chunks) == "Part 1 Part 2 Part 3 Part 4"
    assert final_result is not None
    assert final_result.runtime_info is not None
    assert final_result.runtime_info.selected_model == "harness-long-gen"
    assert final_result.runtime_info.was_rerouted is False


@pytest.mark.asyncio
async def test_planner_attempt_telemetry_forensics_accuracy() -> None:
    """Validate that every field of PlannerAttemptTelemetry is populated accurately."""
    catalog = _create_test_catalog(["m-err", "m-ok"])
    registry = ProviderRegistry()

    provider = WatchdogMockProvider(
        provider_id="watchdog_mock",
        stream_schedules={
            "m-err": [(0.01, ValueError("Intentional mock error"))],
            "m-ok": [
                (0.01, '{"tool_name": "file_search", "parameters": {"path": ".", "query": "test"}}')
            ],
        },
    )
    registry.register(provider)
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)

    tool_registry = ToolRegistry()
    tool_registry.register(FileSearchTool())

    planner = ToolIntentPlanner(
        gateway=gateway,
        tool_registry=tool_registry,
        planner_timeout=0.20,
    )

    result = await planner.plan_tool_intent("search files")

    assert result.status == PlannerStatus.TOOL_SELECTED
    assert result.selected_model == "m-ok"
    assert result.was_rerouted is True
    assert result.total_duration_ms > 0
    assert len(result.attempts_telemetry) == 2

    # Attempt 1 failed with error
    att1 = result.attempts_telemetry[0]
    assert att1.attempt_number == 1
    assert att1.model_id == "m-err"
    assert att1.provider_id == "watchdog_mock"
    assert att1.start_time > 0
    assert att1.end_time >= att1.start_time
    assert att1.elapsed_ms >= 0
    assert att1.outcome == "PROVIDER_ERROR"
    assert "Intentional mock error" in str(att1.error_reason)
    assert att1.fallback_proceeded is True

    # Attempt 2 succeeded
    att2 = result.attempts_telemetry[1]
    assert att2.attempt_number == 2
    assert att2.model_id == "m-ok"
    assert att2.provider_id == "watchdog_mock"
    assert att2.start_time > 0
    assert att2.end_time >= att2.start_time
    assert att2.elapsed_ms >= 0
    assert att2.outcome == "SUCCESS_TOOL_SELECTED"
    assert att2.activity_type == "content"
    assert att2.first_activity_ms is not None
    assert att2.fallback_proceeded is False


@pytest.mark.asyncio
async def test_harness_independent_startup_window_per_candidate() -> None:
    """Each candidate receives an independent 30s startup window on fallback."""
    catalog = _create_test_catalog(["h-c1", "h-c2", "h-c3"])
    registry = ProviderRegistry()

    # h-c1: silent for 0.25s -> times out at 0.10s
    # h-c2: silent for 0.25s -> times out at 0.10s
    # h-c3: responds at 0.02s -> succeeds
    provider = WatchdogMockProvider(
        provider_id="watchdog_mock",
        stream_schedules={
            "h-c1": [(0.25, "late-1")],
            "h-c2": [(0.25, "late-2")],
            "h-c3": [(0.02, "All good")],
        },
    )
    registry.register(provider)
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)

    runner = HarnessRunner(
        gateway=gateway,
        startup_timeout=0.10,
    )

    plan = ExecutionPlan(
        task_id=uuid4(),
        correlation_id=uuid4(),
        steps=[
            PlanStep(
                step_id=uuid4(),
                step_type=StepType.MODEL_INVOCATION,
                model_intent=ModelStepIntent(
                    instruction="say all good",
                    capability_requirements=CapabilityRequirements(),
                    execution_strategy=ExecutionStrategy(
                        require_verification=False,
                        recovery_policy=RecoveryPolicy.REROUTE,
                    ),
                ),
            )
        ],
    )
    context = TaskContext(session_id="test", memory_scope="test")

    chunks: list[str] = []
    final_result = None
    async for chunk, res in runner.run_stream(plan, context):
        if chunk:
            chunks.append(chunk)
        if res is not None:
            final_result = res

    assert "".join(chunks) == "All good"
    assert final_result is not None
    assert final_result.runtime_info is not None
    info = final_result.runtime_info
    assert info.selected_model == "h-c3"
    assert info.was_rerouted is True
    assert info.failed_model == "h-c1"
    assert info.failure_reason == "timeout"
    assert info.harness_route_history == ["h-c1", "h-c2", "h-c3"]
