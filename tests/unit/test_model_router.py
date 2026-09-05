"""Unit tests for BERU V2 Execution Strategy, ModelRouter, Catalog, Health, and Recovery."""

import pytest

from ahjin.beru.orchestrator import BeruOrchestrator
from ahjin.beru.types import CapabilityRequirements, ExecutionStrategy, RecoveryPolicy
from ahjin.core.types import TaskContext, TaskRequest, UserIntent
from ahjin.models.catalog import ModelCatalog, create_default_catalog
from ahjin.models.health import ModelHealthStatus, ModelHealthTracker
from ahjin.models.router import CapabilityUnavailableError, ModelRouter
from ahjin.models.types import (
    ModelCapabilities,
    ModelDescriptor,
    ModelTier,
)


def test_fast_execution_tier_selected_for_general_tasks() -> None:
    """Fast tier (Nemotron 3.5 Lightning) must be selected for general tasks."""
    router = ModelRouter(catalog=create_default_catalog())
    reqs = CapabilityRequirements(requires_reasoning=False, requires_code=False)

    selection = router.select_model(reqs)
    assert selection.model_id == "nvidia/nemotron-3.5-lightning-30b-a3b"
    assert selection.tier == ModelTier.FAST
    assert selection.max_output_tokens == 4096


def test_heavy_reasoning_tier_selected_when_reasoning_required() -> None:
    """Heavy tier must be selected when reasoning is explicitly required (MiniMax M3 preferred)."""
    router = ModelRouter(catalog=create_default_catalog())
    reqs = CapabilityRequirements(requires_reasoning=True)

    selection = router.select_model(reqs)
    assert selection.tier == ModelTier.HEAVY
    assert selection.model_id == "minimax/minimax-m3:free"


def test_stronger_incapable_model_must_never_beat_weaker_capable_model() -> None:
    """CRITICAL RULE: Hard capabilities determine ELIGIBILITY.

    Model A: quality=99, vision=False
    Model B: quality=70, vision=True
    Task requires: vision=True

    Model A MUST BE REJECTED. Model B MUST BE SELECTED.
    """
    catalog = ModelCatalog()
    catalog.register(
        ModelDescriptor(
            model_id="strong-text-model",
            provider_id="mock",
            tier=ModelTier.HEAVY,
            quality_score=99,
            capabilities=ModelCapabilities(reasoning=True, vision=False),
        )
    )
    catalog.register(
        ModelDescriptor(
            model_id="weaker-vision-model",
            provider_id="mock",
            tier=ModelTier.HEAVY,
            quality_score=70,
            capabilities=ModelCapabilities(reasoning=True, vision=True),
        )
    )

    router = ModelRouter(catalog=catalog)
    reqs = CapabilityRequirements(requires_vision=True)

    selection = router.select_model(reqs)
    assert selection.model_id == "weaker-vision-model", (
        f"Selected {selection.model_id} instead of weaker-vision-model. "
        "Incapable stronger model must NEVER beat a capable model!"
    )


def test_multiple_eligible_models_ranked_by_quality_score() -> None:
    """When multiple models satisfy all capabilities, strongest quality_score wins."""
    catalog = ModelCatalog()
    catalog.register(
        ModelDescriptor(
            model_id="model-low-quality",
            provider_id="mock",
            tier=ModelTier.HEAVY,
            quality_score=75,
            priority=100,
            capabilities=ModelCapabilities(reasoning=True),
        )
    )
    catalog.register(
        ModelDescriptor(
            model_id="model-high-quality",
            provider_id="mock",
            tier=ModelTier.HEAVY,
            quality_score=95,
            priority=100,
            capabilities=ModelCapabilities(reasoning=True),
        )
    )

    router = ModelRouter(catalog=catalog)
    reqs = CapabilityRequirements(requires_reasoning=True)

    selection = router.select_model(reqs)
    assert selection.model_id == "model-high-quality"


def test_endpoint_verified_does_not_artificially_overpower_higher_quality_score() -> None:
    """Fix 2: endpoint_verified must NOT allow lower quality model to beat higher quality model.

    Model A: quality_score = 95, endpoint_verified = False
    Model B: quality_score = 85, endpoint_verified = True

    Model A MUST WIN based on model strength/quality.
    """
    catalog = ModelCatalog()
    catalog.register(
        ModelDescriptor(
            model_id="unverified-high-quality",
            provider_id="mock",
            tier=ModelTier.HEAVY,
            quality_score=95,
            priority=100,
            endpoint_verified=False,
            capabilities=ModelCapabilities(reasoning=True),
        )
    )
    catalog.register(
        ModelDescriptor(
            model_id="verified-lower-quality",
            provider_id="mock",
            tier=ModelTier.HEAVY,
            quality_score=85,
            priority=100,
            endpoint_verified=True,
            capabilities=ModelCapabilities(reasoning=True),
        )
    )

    router = ModelRouter(catalog=catalog)
    reqs = CapabilityRequirements(requires_reasoning=True)

    selection = router.select_model(reqs)
    assert selection.model_id == "unverified-high-quality", (
        f"Selected {selection.model_id} instead of unverified-high-quality. "
        "endpoint_verified must NOT overpower higher quality_score!"
    )


def test_endpoint_verified_acts_as_tie_breaker_for_equal_scores() -> None:
    """Fix 2: endpoint_verified acts as a micro tie-breaker when quality scores are equal."""
    catalog = ModelCatalog()
    catalog.register(
        ModelDescriptor(
            model_id="unverified-equal-quality",
            provider_id="mock",
            tier=ModelTier.HEAVY,
            quality_score=90,
            priority=100,
            endpoint_verified=False,
            capabilities=ModelCapabilities(reasoning=True),
        )
    )
    catalog.register(
        ModelDescriptor(
            model_id="verified-equal-quality",
            provider_id="mock",
            tier=ModelTier.HEAVY,
            quality_score=90,
            priority=100,
            endpoint_verified=True,
            capabilities=ModelCapabilities(reasoning=True),
        )
    )

    router = ModelRouter(catalog=catalog)
    reqs = CapabilityRequirements(requires_reasoning=True)

    selection = router.select_model(reqs)
    assert selection.model_id == "verified-equal-quality"


def test_unhealthy_strongest_model_skipped_and_healthy_alternative_selected() -> None:
    """If the preferred model is UNHEALTHY, router selects next best healthy eligible model."""
    catalog = create_default_catalog()
    health = ModelHealthTracker()

    # Mark top MiniMax M3 model unhealthy
    health.record_failure("minimax/minimax-m3:free")
    health.record_failure("minimax/minimax-m3:free")
    health.record_failure("minimax/minimax-m3:free")
    target_state = health.get_state("minimax/minimax-m3:free")
    assert target_state.status == ModelHealthStatus.UNHEALTHY

    router = ModelRouter(catalog=catalog, health_tracker=health)
    reqs = CapabilityRequirements(requires_reasoning=True)

    selection = router.select_model(reqs)
    assert selection.model_id == "nvidia/nemotron-3-ultra-550b-a55b:free"
    assert selection.tier == ModelTier.HEAVY


def test_capability_unavailable_error_if_all_models_incapable() -> None:
    """CapabilityUnavailableError raised if no model satisfies required capabilities."""
    catalog = ModelCatalog()
    catalog.register(
        ModelDescriptor(
            model_id="text-only-model",
            provider_id="mock",
            tier=ModelTier.HEAVY,
            capabilities=ModelCapabilities(vision=False),
        )
    )

    router = ModelRouter(catalog=catalog)
    reqs = CapabilityRequirements(requires_vision=True)

    with pytest.raises(CapabilityUnavailableError):
        router.select_model(reqs)


@pytest.mark.asyncio
async def test_beru_produces_execution_strategy_without_model_ids() -> None:
    """BERU must produce ExecutionStrategy with capability requirements and ZERO model IDs."""
    orchestrator = BeruOrchestrator()
    task_text = "Write a Python script to sort an array"
    request = TaskRequest(
        intent=UserIntent(primary_text=task_text),
        context=TaskContext(session_id="test_session"),
    )

    plan = await orchestrator.plan(request)
    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.model_intent is not None
    strategy = step.model_intent.execution_strategy

    assert strategy.capability_requirements.requires_code is True
    assert strategy.preferred_tier == ModelTier.HEAVY
    assert strategy.recovery_policy == RecoveryPolicy.REROUTE

    # Verify BERU contains NO model IDs or provider names in string representation
    strategy_str = str(strategy.model_dump())
    assert "nvidia" not in strategy_str.lower()
    assert "deepseek" not in strategy_str.lower()
    assert "nemotron" not in strategy_str.lower()


@pytest.mark.asyncio
async def test_harness_same_request_rerouting_on_degraded_model_failure() -> None:
    """Fix 1: When primary model fails with standard httpx error (no model_id attribute),

    ProviderGateway attaches model_id to exception so HarnessRunner excludes the failed model
    even if it is DEGRADED (1 failure) and not UNHEALTHY.
    """
    from uuid import uuid4

    import httpx

    from ahjin.beru.types import ExecutionPlan, ModelStepIntent, PlanStep, StepType
    from ahjin.harness.gateway import ProviderGateway
    from ahjin.harness.runner import HarnessRunner
    from ahjin.providers.base import BaseModelProvider
    from ahjin.providers.registry import ProviderRegistry
    from ahjin.providers.types import ModelInvocationRequest, ModelInvocationResponse

    invoked_models: list[str] = []

    class MockOpenRouterProvider(BaseModelProvider):
        provider_id = "openrouter"

        def get_default_model_id(self) -> str:
            return "minimax/minimax-m3:free"

        async def invoke(self, request: ModelInvocationRequest) -> ModelInvocationResponse:
            invoked_models.append(request.model_id)
            if request.model_id == "minimax/minimax-m3:free":
                # Standard httpx exception without custom model_id attribute
                raise httpx.RequestError(
                    "Connection reset",
                    request=httpx.Request("POST", "http://test"),
                )
            return ModelInvocationResponse(
                invocation_id=request.invocation_id,
                provider_id="openrouter",
                content="Response from alternative model",
                model_id=request.model_id,
            )

    registry = ProviderRegistry()
    registry.register(MockOpenRouterProvider())
    router = ModelRouter(catalog=create_default_catalog())
    gateway = ProviderGateway(registry=registry, router=router)
    runner = HarnessRunner(gateway=gateway)

    strategy = ExecutionStrategy(
        capability_requirements=CapabilityRequirements(requires_reasoning=True),
        preferred_tier="HEAVY",
    )

    plan = ExecutionPlan(
        task_id=uuid4(),
        correlation_id=uuid4(),
        steps=[
            PlanStep(
                step_type=StepType.MODEL_INVOCATION,
                model_intent=ModelStepIntent(
                    instruction="Explain quantum physics",
                    execution_strategy=strategy,
                ),
            )
        ],
    )
    context = TaskContext(session_id="test")

    res = await runner.run(plan, context)
    assert res.success is True
    assert res.output_text == "Response from alternative model"
    # Verify that the primary model was attempted first, failed, and WAS NOT selected again
    assert invoked_models[0] == "minimax/minimax-m3:free"
    assert "minimax/minimax-m3:free" not in invoked_models[1:]
    assert invoked_models[1] == "nvidia/nemotron-3-ultra-550b-a55b:free"
    assert invoked_models[0] != invoked_models[1]

