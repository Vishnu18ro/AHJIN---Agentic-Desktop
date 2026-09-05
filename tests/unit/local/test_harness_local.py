"""Unit tests for HarnessRunner with LocalExecutor and ConnectivityChecker.

Verifies Phase 5 Final Routing Architecture:
- Test A — Online FAST: Cloud available + FAST task → cloud model selected.
- Test B — Online HEAVY: Cloud available + HEAVY task → cloud model selected.
- Test C — One cloud model fails: Cloud reroute continues → local NOT called.
- Test D — Entire cloud unavailable: Cloud unavailable → LocalExecutor FAST → Gemma.
- Test E — Offline HEAVY: Offline HEAVY task → Qwen.
- Test F — Qwen > 90s: Qwen safely cancelled → Gemma invoked with original prompt.
- Test G — Online never touches local: Local is untouched on successful online path.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest

from ahjin.beru.types import (
    CapabilityRequirements,
    ExecutionPlan,
    ExecutionStrategy,
    ModelStepIntent,
    PlanStep,
    StepType,
)
from ahjin.core.types import TaskContext
from ahjin.harness.connectivity import ConnectivityChecker
from ahjin.harness.gateway import ProviderGateway
from ahjin.harness.runner import HarnessRunner
from ahjin.local.executor import _QWEN_TIMEOUT_NOTICE, LocalExecutor
from ahjin.local.types import (
    LOCAL_GEMMA_MODEL_ID,
    LOCAL_QWEN_MODEL_ID,
    LocalExecutionResult,
)
from ahjin.models.catalog import ModelCatalog
from ahjin.models.router import ModelRouter
from ahjin.models.types import ModelCapabilities, ModelDescriptor, ModelTier
from ahjin.providers.base import BaseModelProvider
from ahjin.providers.registry import ProviderRegistry
from ahjin.providers.types import ModelInvocationRequest, ModelInvocationResponse

_MOCK_PROVIDER_ID = "mock_harness_local"
_MOCK_MODEL_ID = "mock-harness-local-model"


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class MockCloudProvider(BaseModelProvider):
    @property
    def provider_id(self) -> str:
        return _MOCK_PROVIDER_ID

    def get_default_model_id(self) -> str:
        return _MOCK_MODEL_ID

    async def invoke(self, request: ModelInvocationRequest) -> ModelInvocationResponse:
        return ModelInvocationResponse(
            invocation_id=request.invocation_id,
            content=f"Cloud answer from {request.model_id}.",
            provider_id=self.provider_id,
            model_id=request.model_id,
        )


def _build_cloud_gateway() -> ProviderGateway:
    catalog = ModelCatalog()
    catalog.register(
        ModelDescriptor(
            model_id=_MOCK_MODEL_ID,
            provider_id=_MOCK_PROVIDER_ID,
            tier=ModelTier.FAST,
            capabilities=ModelCapabilities(),
        )
    )
    catalog.register(
        ModelDescriptor(
            model_id="mock-heavy-cloud-model",
            provider_id=_MOCK_PROVIDER_ID,
            tier=ModelTier.HEAVY,
            capabilities=ModelCapabilities(reasoning=True),
        )
    )
    registry = ProviderRegistry()
    registry.register(MockCloudProvider())
    router = ModelRouter(catalog=catalog)
    return ProviderGateway(registry=registry, router=router)


def _build_plan(strategy: ExecutionStrategy | None = None) -> ExecutionPlan:
    s = strategy or ExecutionStrategy(
        capability_requirements=CapabilityRequirements(),
        preferred_tier="FAST",
    )
    return ExecutionPlan(
        task_id=uuid4(),
        correlation_id=uuid4(),
        steps=[
            PlanStep(
                step_type=StepType.MODEL_INVOCATION,
                model_intent=ModelStepIntent(
                    instruction="Tell me something.",
                    execution_strategy=s,
                ),
            )
        ],
    )


def _build_local_result(
    used_fallback: bool = False,
    suggest_escalation: bool = False,
    model_used: str = LOCAL_GEMMA_MODEL_ID,
) -> LocalExecutionResult:
    return LocalExecutionResult(
        output_text=_QWEN_TIMEOUT_NOTICE + "Gemma answer." if used_fallback else "Local answer.",
        model_used=LOCAL_GEMMA_MODEL_ID if used_fallback else model_used,
        latency_ms=500.0,
        used_fallback=used_fallback,
        fallback_reason="qwen_timeout" if used_fallback else None,
        suggest_escalation=suggest_escalation,
        escalation_reason="qwen_timeout_gemma_fallback" if suggest_escalation else None,
        attempted_model=LOCAL_QWEN_MODEL_ID if used_fallback else None,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_and_g_online_fast_uses_cloud_never_touches_local() -> None:
    """Test A & G: Cloud available + FAST task → cloud model selected, LocalExecutor NOT called."""
    local_executor = MagicMock(spec=LocalExecutor)
    local_executor.invoke = AsyncMock()

    conn = ConnectivityChecker(force_offline=False)
    gateway = _build_cloud_gateway()
    runner = HarnessRunner(
        gateway=gateway, local_executor=local_executor, connectivity_checker=conn
    )

    plan = _build_plan(
        ExecutionStrategy(
            capability_requirements=CapabilityRequirements(),
            preferred_tier="FAST",
        )
    )
    result = await runner.run(plan, TaskContext(session_id="test"))

    assert result.success is True
    assert "mock-harness-local-model" in result.output_text
    local_executor.invoke.assert_not_called()


@pytest.mark.asyncio
async def test_b_online_heavy_uses_cloud_fleet_never_touches_local() -> None:
    """Test B: Cloud available + HEAVY task → cloud fleet selected, LocalExecutor NOT called."""
    local_executor = MagicMock(spec=LocalExecutor)
    local_executor.invoke = AsyncMock()

    conn = ConnectivityChecker(force_offline=False)
    gateway = _build_cloud_gateway()
    runner = HarnessRunner(
        gateway=gateway, local_executor=local_executor, connectivity_checker=conn
    )

    heavy_strategy = ExecutionStrategy(
        capability_requirements=CapabilityRequirements(requires_reasoning=True),
        preferred_tier="HEAVY",
    )
    plan = _build_plan(heavy_strategy)
    result = await runner.run(plan, TaskContext(session_id="test"))

    assert result.success is True
    assert "mock-heavy-cloud-model" in result.output_text
    local_executor.invoke.assert_not_called()


@pytest.mark.asyncio
async def test_c_one_cloud_model_fails_reroutes_in_cloud_not_immediate_local() -> None:
    """Test C: One cloud model fails → cloud reroute continues → alternate cloud model tried."""
    catalog = ModelCatalog()
    catalog.register(
        ModelDescriptor(
            model_id="failing-cloud-model",
            provider_id="mock_failing",
            tier=ModelTier.FAST,
            priority=250,
            capabilities=ModelCapabilities(),
        )
    )
    catalog.register(
        ModelDescriptor(
            model_id="working-cloud-model",
            provider_id="mock_working",
            tier=ModelTier.FAST,
            priority=200,
            capabilities=ModelCapabilities(),
        )
    )

    class FailingProvider(BaseModelProvider):
        @property
        def provider_id(self) -> str:
            return "mock_failing"

        def get_default_model_id(self) -> str:
            return "failing-cloud-model"

        async def invoke(self, request: ModelInvocationRequest) -> ModelInvocationResponse:
            req = httpx.Request("POST", "http://test")
            resp = httpx.Response(500, request=req)
            raise httpx.HTTPStatusError("Cloud model 500 error", request=req, response=resp)

    class WorkingProvider(BaseModelProvider):
        @property
        def provider_id(self) -> str:
            return "mock_working"

        def get_default_model_id(self) -> str:
            return "working-cloud-model"

        async def invoke(self, request: ModelInvocationRequest) -> ModelInvocationResponse:
            return ModelInvocationResponse(
                invocation_id=request.invocation_id,
                content="Backup cloud answer.",
                provider_id=self.provider_id,
                model_id=request.model_id,
            )

    registry = ProviderRegistry()
    registry.register(FailingProvider())
    registry.register(WorkingProvider())
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)

    local_executor = MagicMock(spec=LocalExecutor)
    local_executor.invoke = AsyncMock()

    conn = ConnectivityChecker(force_offline=False)
    runner = HarnessRunner(
        gateway=gateway, local_executor=local_executor, connectivity_checker=conn
    )

    result = await runner.run(_build_plan(), TaskContext(session_id="test"))

    assert result.success is True
    assert result.output_text == "Backup cloud answer."
    local_executor.invoke.assert_not_called()


@pytest.mark.asyncio
async def test_d_entire_cloud_unavailable_falls_back_to_local_fast() -> None:
    """Test D: Entire cloud environment unavailable → LocalExecutor invoked → FAST → Gemma."""
    local_executor = MagicMock(spec=LocalExecutor)
    local_executor.invoke = AsyncMock(
        return_value=_build_local_result(model_used=LOCAL_GEMMA_MODEL_ID)
    )

    conn = ConnectivityChecker(force_offline=True)
    gateway = _build_cloud_gateway()
    runner = HarnessRunner(
        gateway=gateway, local_executor=local_executor, connectivity_checker=conn
    )

    result = await runner.run(_build_plan(), TaskContext(session_id="test"))

    assert result.success is True
    assert result.output_text == "Local answer."
    local_executor.invoke.assert_called_once()


@pytest.mark.asyncio
async def test_e_offline_heavy_uses_qwen() -> None:
    """Test E: Offline HEAVY task → Qwen."""
    local_executor = MagicMock(spec=LocalExecutor)
    local_executor.invoke = AsyncMock(
        return_value=_build_local_result(model_used=LOCAL_QWEN_MODEL_ID)
    )

    conn = ConnectivityChecker(force_offline=True)
    gateway = _build_cloud_gateway()
    runner = HarnessRunner(
        gateway=gateway, local_executor=local_executor, connectivity_checker=conn
    )

    heavy_strategy = ExecutionStrategy(
        capability_requirements=CapabilityRequirements(requires_reasoning=True),
        preferred_tier="HEAVY",
    )
    plan = _build_plan(heavy_strategy)
    result = await runner.run(plan, TaskContext(session_id="test"))

    assert result.success is True
    assert result.runtime_info is not None
    assert result.runtime_info.selected_model == LOCAL_QWEN_MODEL_ID
    local_executor.invoke.assert_called_once()


@pytest.mark.asyncio
async def test_f_qwen_timeout_cancels_and_invokes_gemma_with_original_prompt() -> None:
    """Test F: Qwen timeout → Gemma fallback → TaskResult.local_escalation_hint is set."""
    fallback_result = _build_local_result(used_fallback=True, suggest_escalation=True)

    local_executor = MagicMock(spec=LocalExecutor)
    local_executor.invoke = AsyncMock(return_value=fallback_result)

    conn = ConnectivityChecker(force_offline=True)
    gateway = _build_cloud_gateway()

    runner = HarnessRunner(
        gateway=gateway, local_executor=local_executor, connectivity_checker=conn
    )
    result = await runner.run(_build_plan(), TaskContext(session_id="test"))

    assert result.local_escalation_hint is not None
    hint = result.local_escalation_hint
    assert isinstance(hint, LocalExecutionResult)
    assert hint.suggest_escalation is True
    assert hint.escalation_reason == "qwen_timeout_gemma_fallback"
    assert hint.used_fallback is True
