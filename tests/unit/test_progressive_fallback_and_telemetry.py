"""Unit tests for Progressive Harness Fallback, Whole-Request Routing Telemetry,
and Provider/Model Identity + Latency Separation in AHJIN 2.0.
"""

from typing import Any, AsyncGenerator
from uuid import uuid4

import httpx
import pytest

from ahjin.beru.orchestrator import BeruOrchestrator
from ahjin.beru.tool_planner import PlannerResult, PlannerStatus
from ahjin.beru.types import (
    CapabilityRequirements,
    ExecutionPlan,
    ExecutionStrategy,
    ModelStepIntent,
    PlanStep,
)
from ahjin.core.types import (
    RequestMetadata,
    RerouteAttempt,
    RuntimeInfo,
    TaskContext,
    TaskRequest,
    UserIntent,
)
from ahjin.harness.context import ContextAssembler
from ahjin.harness.gateway import ProviderGateway
from ahjin.harness.runner import HarnessRunner
from ahjin.interfaces.telegram.bot import _build_runtime_footer
from ahjin.local.types import LocalExecutionResult
from ahjin.models.catalog import ModelCatalog
from ahjin.models.health import ModelHealthStatus, ModelHealthTracker
from ahjin.models.router import ModelRouter
from ahjin.models.types import ModelCapabilities, ModelDescriptor, ModelTier
from ahjin.providers.base import BaseModelProvider
from ahjin.providers.registry import ProviderRegistry
from ahjin.providers.types import (
    ContextualizedPrompt,
    ModelInvocationRequest,
    ModelInvocationResponse,
)
from ahjin.telemetry import (
    STAGE_BERU_ANALYSIS,
    STAGE_MODEL_GENERATION,
    STAGE_TIME_TO_FIRST_TOKEN,
    RequestTimer,
)


class MockMultiModelProvider(BaseModelProvider):
    """Mock provider allowing per-model invocation behavior."""

    def __init__(
        self,
        provider_id: str,
        failing_models: set[str] | None = None,
        failure_exc_factory: Any | None = None,
    ) -> None:
        self._provider_id = provider_id
        self.failing_models = failing_models or set()
        self.failure_exc_factory = failure_exc_factory
        self.invoked_models: list[str] = []

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def get_default_model_id(self) -> str:
        return "mock/default"

    async def invoke(self, request: ModelInvocationRequest) -> ModelInvocationResponse:
        self.invoked_models.append(request.model_id)
        if request.model_id in self.failing_models:
            if self.failure_exc_factory:
                raise self.failure_exc_factory(request.model_id)
            req = httpx.Request("POST", "https://api.mock.test/v1/chat")
            resp = httpx.Response(status_code=402, request=req)
            raise httpx.HTTPStatusError("Payment Required", request=req, response=resp)

        return ModelInvocationResponse(
            invocation_id=request.invocation_id,
            content=f"Success from {request.model_id}",
            latency_ms=184.0,
            provider_id=self._provider_id,
            model_id=request.model_id,
        )

    async def invoke_stream(
        self, request: ModelInvocationRequest
    ) -> AsyncGenerator[str, None]:
        self.invoked_models.append(request.model_id)
        if request.model_id in self.failing_models:
            if self.failure_exc_factory:
                raise self.failure_exc_factory(request.model_id)
            req = httpx.Request("POST", "https://api.mock.test/v1/chat")
            resp = httpx.Response(status_code=402, request=req)
            raise httpx.HTTPStatusError("Payment Required", request=req, response=resp)

        yield "Chunk 1"
        yield " Chunk 2"


class MockLocalExecutor:
    def __init__(self, succeed: bool = True) -> None:
        self.succeed = succeed
        self.invoked = False

    async def invoke(
        self, prompt: ContextualizedPrompt, strategy: ExecutionStrategy
    ) -> LocalExecutionResult:
        self.invoked = True
        return LocalExecutionResult(
            model_used="gemma3:4b",
            output_text="Local fallback response",
            latency_ms=45.0,
            used_fallback=True,
            fallback_reason="all cloud exhausted",
        )

    async def invoke_stream(
        self, prompt: ContextualizedPrompt, strategy: ExecutionStrategy
    ) -> AsyncGenerator[tuple[str, LocalExecutionResult | None], None]:
        self.invoked = True
        res = LocalExecutionResult(
            model_used="gemma3:4b",
            output_text="Local stream fallback response",
            latency_ms=50.0,
            used_fallback=True,
            fallback_reason="all cloud exhausted",
        )
        yield "Local stream fallback response", res


# ---------------------------------------------------------------------------
# Test 1 to 9: Progressive Fallback & Selection Behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_progressive_fallback_traverses_multiple_cloud_models() -> None:
    """MiniMax fails -> Next model fails -> Next model fails -> 4th model succeeds."""
    catalog = ModelCatalog()
    # Register 4 FAST models with descending priorities
    m1 = ModelDescriptor(
        model_id="minimax/minimax-m3",
        provider_id="openrouter",
        tier=ModelTier.ALL,
        priority=300,
        capabilities=ModelCapabilities(),
    )
    m2 = ModelDescriptor(
        model_id="nvidia/nemotron-3.5-lightning:free",
        provider_id="openrouter",
        tier=ModelTier.FAST,
        priority=220,
        capabilities=ModelCapabilities(),
    )
    m3 = ModelDescriptor(
        model_id="nvidia/nemotron-3.5-lightning-30b-a3b",
        provider_id="nvidia",
        tier=ModelTier.FAST,
        priority=200,
        capabilities=ModelCapabilities(),
    )
    m4 = ModelDescriptor(
        model_id="mock/candidate-4",
        provider_id="openrouter",
        tier=ModelTier.FAST,
        priority=180,
        capabilities=ModelCapabilities(),
    )
    for m in (m1, m2, m3, m4):
        catalog.register(m)

    registry = ProviderRegistry()
    openrouter_prov = MockMultiModelProvider(
        "openrouter",
        failing_models={"minimax/minimax-m3", "nvidia/nemotron-3.5-lightning:free"},
    )
    nvidia_prov = MockMultiModelProvider(
        "nvidia",
        failing_models={"nvidia/nemotron-3.5-lightning-30b-a3b"},
    )
    registry.register(openrouter_prov)
    registry.register(nvidia_prov)

    tracker = ModelHealthTracker()
    router = ModelRouter(catalog=catalog, health_tracker=tracker)
    gateway = ProviderGateway(registry=registry, router=router)

    runner = HarnessRunner(
        gateway=gateway,
        context_assembler=ContextAssembler(),
    )

    strategy = ExecutionStrategy(
        preferred_tier="FAST",
        max_recovery_attempts=None,  # Dynamic progressive
    )
    plan = ExecutionPlan(
        task_id=uuid4(),
        correlation_id=uuid4(),
        steps=[
            PlanStep(
                model_intent=ModelStepIntent(
                    instruction="Hello world",
                    execution_strategy=strategy,
                )
            )
        ],
    )

    result = await runner.run(plan, context=TaskContext(session_id="test"))
    assert result.success is True
    assert result.output_text == "Success from mock/candidate-4"
    assert result.runtime_info is not None
    assert result.runtime_info.selected_model == "mock/candidate-4"
    assert result.runtime_info.provider_id == "openrouter"
    assert result.runtime_info.was_rerouted is True
    assert result.runtime_info.failed_model == "minimax/minimax-m3"
    assert result.runtime_info.failure_reason == "HTTP 402"
    # Verify that all 4 models were attempted progressively in priority order
    assert result.runtime_info.harness_route_history == [
        "minimax/minimax-m3",
        "nvidia/nemotron-3.5-lightning:free",
        "nvidia/nemotron-3.5-lightning-30b-a3b",
        "mock/candidate-4",
    ]


@pytest.mark.asyncio
async def test_failed_model_never_selected_twice_in_same_request() -> None:
    """Request-local exclusions must strictly prevent retrying failed models."""
    catalog = ModelCatalog()
    m1 = ModelDescriptor(
        model_id="minimax/minimax-m3",
        provider_id="openrouter",
        tier=ModelTier.ALL,
        priority=300,
        capabilities=ModelCapabilities(),
    )
    m2 = ModelDescriptor(
        model_id="nvidia/nemotron-3.5-lightning:free",
        provider_id="openrouter",
        tier=ModelTier.FAST,
        priority=200,
        capabilities=ModelCapabilities(),
    )
    catalog.register(m1)
    catalog.register(m2)

    registry = ProviderRegistry()
    prov = MockMultiModelProvider(
        "openrouter",
        failing_models={"minimax/minimax-m3"},
    )
    registry.register(prov)
    tracker = ModelHealthTracker()
    router = ModelRouter(catalog=catalog, health_tracker=tracker)
    gateway = ProviderGateway(registry=registry, router=router)

    runner = HarnessRunner(gateway=gateway, context_assembler=ContextAssembler())
    plan = ExecutionPlan(
        task_id=uuid4(),
        correlation_id=uuid4(),
        steps=[
            PlanStep(
                model_intent=ModelStepIntent(
                    instruction="Hi",
                    execution_strategy=ExecutionStrategy(preferred_tier="FAST"),
                )
            )
        ],
    )

    res = await runner.run(plan, context=TaskContext(session_id="test"))
    assert res.success is True
    assert res.runtime_info.selected_model == "nvidia/nemotron-3.5-lightning:free"
    # Ensure m1 was invoked once and NOT selected a second time
    assert prov.invoked_models.count("minimax/minimax-m3") == 1


@pytest.mark.asyncio
async def test_capability_gate_remains_enforced_during_fallback() -> None:
    """A model lacking required capability must NEVER be selected during fallback."""
    catalog = ModelCatalog()
    # m1 has code capability but fails
    m1 = ModelDescriptor(
        model_id="minimax/minimax-m3",
        provider_id="openrouter",
        tier=ModelTier.ALL,
        priority=300,
        capabilities=ModelCapabilities(coding=True),
    )
    # m2 has higher priority than m3, but DOES NOT have coding capability
    m2 = ModelDescriptor(
        model_id="nvidia/nemotron-3.5-lightning:free",
        provider_id="openrouter",
        tier=ModelTier.FAST,
        priority=250,
        capabilities=ModelCapabilities(coding=False),
    )
    # m3 has lower priority, but HAS coding capability
    m3 = ModelDescriptor(
        model_id="deepseek-ai/deepseek-v4-pro-0813",
        provider_id="nvidia",
        tier=ModelTier.HEAVY,
        priority=150,
        capabilities=ModelCapabilities(coding=True),
    )
    for m in (m1, m2, m3):
        catalog.register(m)

    registry = ProviderRegistry()
    openrouter_prov = MockMultiModelProvider("openrouter", failing_models={"minimax/minimax-m3"})
    nvidia_prov = MockMultiModelProvider("nvidia")
    registry.register(openrouter_prov)
    registry.register(nvidia_prov)

    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)
    runner = HarnessRunner(gateway=gateway, context_assembler=ContextAssembler())

    reqs = CapabilityRequirements(requires_code=True)
    plan = ExecutionPlan(
        task_id=uuid4(),
        correlation_id=uuid4(),
        steps=[
            PlanStep(
                model_intent=ModelStepIntent(
                    instruction="Write a quicksort in Python",
                    capability_requirements=reqs,
                    execution_strategy=ExecutionStrategy(
                        capability_requirements=reqs,
                        preferred_tier="HEAVY",
                    ),
                )
            )
        ],
    )

    res = await runner.run(plan, context=TaskContext(session_id="test"))
    assert res.success is True
    # Must skip m2 (lacks coding) and pick m3 (has coding)
    assert res.runtime_info.selected_model == "deepseek-ai/deepseek-v4-pro-0813"
    assert "nvidia/nemotron-3.5-lightning:free" not in openrouter_prov.invoked_models


@pytest.mark.asyncio
async def test_health_filtering_remains_enforced() -> None:
    """Models marked UNHEALTHY must be filtered out by ModelRouter during fallback."""
    catalog = ModelCatalog()
    m1 = ModelDescriptor(
        model_id="minimax/minimax-m3",
        provider_id="openrouter",
        tier=ModelTier.ALL,
        priority=300,
        capabilities=ModelCapabilities(),
    )
    m2 = ModelDescriptor(
        model_id="nvidia/nemotron-3.5-lightning:free",
        provider_id="openrouter",
        tier=ModelTier.FAST,
        priority=200,
        capabilities=ModelCapabilities(),
    )
    m3 = ModelDescriptor(
        model_id="nvidia/nemotron-3.5-lightning-30b-a3b",
        provider_id="nvidia",
        tier=ModelTier.FAST,
        priority=100,
        capabilities=ModelCapabilities(),
    )
    for m in (m1, m2, m3):
        catalog.register(m)

    tracker = ModelHealthTracker()
    # Mark m2 as UNHEALTHY (3 failures)
    for _ in range(3):
        tracker.record_failure("nvidia/nemotron-3.5-lightning:free")
    assert (
        tracker.get_state("nvidia/nemotron-3.5-lightning:free").snapshot_status
        == ModelHealthStatus.UNHEALTHY
    )

    registry = ProviderRegistry()
    openrouter_prov = MockMultiModelProvider("openrouter", failing_models={"minimax/minimax-m3"})
    nvidia_prov = MockMultiModelProvider("nvidia")
    registry.register(openrouter_prov)
    registry.register(nvidia_prov)

    router = ModelRouter(catalog=catalog, health_tracker=tracker)
    gateway = ProviderGateway(registry=registry, router=router)
    runner = HarnessRunner(gateway=gateway, context_assembler=ContextAssembler())

    plan = ExecutionPlan(
        task_id=uuid4(),
        correlation_id=uuid4(),
        steps=[
            PlanStep(
                model_intent=ModelStepIntent(
                    instruction="Hello",
                    execution_strategy=ExecutionStrategy(preferred_tier="FAST"),
                )
            )
        ],
    )

    res = await runner.run(plan, context=TaskContext(session_id="test"))
    assert res.success is True
    # m2 was skipped because it's UNHEALTHY; m3 was selected
    assert res.runtime_info.selected_model == "nvidia/nemotron-3.5-lightning-30b-a3b"


@pytest.mark.asyncio
async def test_local_fallback_occurs_only_after_cloud_exhaustion() -> None:
    """Local executor is called only after all eligible cloud models fail."""
    catalog = ModelCatalog()
    m1 = ModelDescriptor(
        model_id="minimax/minimax-m3",
        provider_id="openrouter",
        tier=ModelTier.ALL,
        priority=300,
        capabilities=ModelCapabilities(),
    )
    catalog.register(m1)

    registry = ProviderRegistry()
    registry.register(
        MockMultiModelProvider("openrouter", failing_models={"minimax/minimax-m3"})
    )
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)

    local_mock = MockLocalExecutor()
    runner = HarnessRunner(
        gateway=gateway,
        context_assembler=ContextAssembler(),
        local_executor=local_mock,  # type: ignore[arg-type]
    )

    plan = ExecutionPlan(
        task_id=uuid4(),
        correlation_id=uuid4(),
        steps=[
            PlanStep(
                model_intent=ModelStepIntent(
                    instruction="Hello",
                    execution_strategy=ExecutionStrategy(preferred_tier="FAST"),
                )
            )
        ],
    )

    res = await runner.run(plan, context=TaskContext(session_id="test"))
    assert res.success is True
    assert local_mock.invoked is True
    assert res.runtime_info.selected_model == "gemma3:4b"
    assert res.runtime_info.provider_id == "ollama"
    assert res.runtime_info.was_rerouted is True


# ---------------------------------------------------------------------------
# Test 10 to 15: Whole-Request Routing Telemetry
# ---------------------------------------------------------------------------


def test_telemetry_case_1_direct_request() -> None:
    """Case 1: No rerouting anywhere in request lifecycle -> Path: Direct."""
    info = RuntimeInfo(
        selected_model="nex-agi/nex-n2.5-pro:free",
        provider_id="openrouter",
        tier="FAST",
        was_rerouted=False,
        health_status="HEALTHY",
    )
    footer = _build_runtime_footer(info)
    assert "Model: Nex N2.5 Pro" in footer
    assert "Provider: OpenRouter" in footer
    assert "Route: FAST" in footer
    assert "Path: Direct" in footer
    assert "Planner:" not in footer
    assert "Harness:" not in footer


def test_telemetry_case_2_planner_reroutes() -> None:
    """Case 2: Planner reroutes, Harness direct -> Path: ↪ Rerouted."""
    info = RuntimeInfo(
        selected_model="nvidia/nemotron-3.5-lightning:free",
        provider_id="openrouter",
        tier="FAST",
        was_rerouted=True,
        planner_was_rerouted=True,
        planner_failed_model="nex-agi/nex-n2.5-pro:free",
        planner_selected_model="nvidia/nemotron-3.5-lightning:free",
        planner_failure_reason="HTTP 402",
        harness_was_rerouted=False,
        health_status="HEALTHY",
    )
    footer = _build_runtime_footer(info)
    assert "Model: Nemotron 3.5 Lightning" in footer
    assert "Provider: OpenRouter" in footer
    assert "Path: ↪ Rerouted" in footer
    assert "Planner: Nex N2.5 Pro ❌ 402 → Nemotron 3.5 Lightning ✅" in footer
    assert "Harness: Nemotron 3.5 Lightning" in footer
    assert "Reason: HTTP 402" in footer


def test_telemetry_case_3_harness_reroutes() -> None:
    """Case 3: Planner direct (or skipped), Harness reroutes -> Path: ↪ Rerouted."""
    info = RuntimeInfo(
        selected_model="nvidia/nemotron-3.5-lightning:free",
        provider_id="openrouter",
        tier="FAST",
        was_rerouted=True,
        planner_was_rerouted=False,
        harness_was_rerouted=True,
        harness_failed_model="nex-agi/nex-n2.5-pro:free",
        harness_failure_reason="HTTP 402",
        health_status="HEALTHY",
    )
    footer = _build_runtime_footer(info)
    assert "Model: Nemotron 3.5 Lightning" in footer
    assert "Provider: OpenRouter" in footer
    assert "Path: ↪ Rerouted" in footer
    assert "Harness: Nex N2.5 Pro ❌ 402 → Nemotron 3.5 Lightning ✅" in footer
    assert "Planner:" not in footer
    assert "Reason: HTTP 402" in footer


def test_telemetry_case_4_planner_and_harness_both_reroute() -> None:
    """Case 4: Both Planner and Harness reroute -> both histories represented."""
    info = RuntimeInfo(
        selected_model="nvidia/nemotron-3-ultra-550b-a55b",
        provider_id="nvidia",
        tier="HEAVY",
        was_rerouted=True,
        planner_was_rerouted=True,
        planner_failed_model="nex-agi/nex-n2.5-pro:free",
        planner_selected_model="nvidia/nemotron-3.5-lightning:free",
        planner_failure_reason="HTTP 402",
        harness_was_rerouted=True,
        harness_failed_model="nvidia/nemotron-3.5-lightning:free",
        harness_failure_reason="network error",
        health_status="HEALTHY",
    )
    footer = _build_runtime_footer(info)
    assert "Model: Nemotron Ultra 550B" in footer
    assert "Provider: NVIDIA" in footer
    assert "Path: ↪ Rerouted" in footer
    assert "Planner: Nex N2.5 Pro ❌ 402 → Nemotron 3.5 Lightning ✅" in footer
    assert "Harness: Nemotron 3.5 Lightning ❌ network error → Nemotron Ultra 550B ✅" in footer
    assert "Reason: network error" in footer
    assert "From:" not in footer


def test_footer_structure_direct_order() -> None:
    """Validate exact order: Model/Provider/Route -> Latency -> Path -> Health."""
    info = RuntimeInfo(
        selected_model="nex-agi/nex-n2.5-pro:free",
        provider_id="openrouter",
        tier="FAST",
        total_ms=150.0,
        provider_api_ms=30.0,
        model_api_ms=110.0,
        was_rerouted=False,
        health_status="HEALTHY",
    )
    footer = _build_runtime_footer(info)
    assert "From:" not in footer

    idx_runtime = footer.index("⚡ AHJIN Runtime")
    idx_model = footer.index("Model: Nex N2.5 Pro")
    idx_provider = footer.index("Provider: OpenRouter")
    idx_route = footer.index("Route: FAST")
    idx_latency = footer.index("⏱ Latency")
    idx_total = footer.index("└─ Total: 150ms")
    idx_path = footer.index("Path: Direct")
    idx_health = footer.index("Health: 🟢 Healthy")

    # Order must strictly match:
    assert idx_runtime < idx_model < idx_provider < idx_route
    assert idx_route < idx_latency < idx_total
    assert idx_total < idx_path
    assert idx_path < idx_health


def test_footer_structure_rerouted_order_and_no_from_line() -> None:
    """Validate rerouted request order: Latency -> Path -> Histories -> Reason -> Health."""
    info = RuntimeInfo(
        selected_model="nvidia/nemotron-3.5-lightning:free",
        provider_id="openrouter",
        tier="FAST",
        total_ms=250.0,
        provider_api_ms=40.0,
        model_api_ms=200.0,
        was_rerouted=True,
        failed_model="nex-agi/nex-n2.5-pro:free",  # Populated, but must NOT show as 'From:'
        planner_was_rerouted=True,
        planner_failed_model="nex-agi/nex-n2.5-pro:free",
        planner_selected_model="nvidia/nemotron-3.5-lightning:free",
        planner_failure_reason="HTTP 402",
        harness_was_rerouted=False,
        health_status="HEALTHY",
    )
    footer = _build_runtime_footer(info)

    # 1. From: must be completely removed
    assert "From:" not in footer

    # 2. Section indices
    idx_latency = footer.index("⏱ Latency")
    idx_total = footer.index("└─ Total: 250ms")
    idx_path = footer.index("Path: ↪ Rerouted")
    idx_planner = footer.index("Planner: Nex N2.5 Pro ❌ 402 → Nemotron 3.5 Lightning ✅")
    idx_harness = footer.index("Harness: Nemotron 3.5 Lightning")
    idx_reason = footer.index("Reason: HTTP 402")
    idx_health = footer.index("Health: 🟢 Healthy")

    # Path section must be immediately after Latency section
    assert idx_latency < idx_total < idx_path
    assert idx_path < idx_planner < idx_harness < idx_reason
    # Health must be the final informational line
    assert idx_reason < idx_health



# ---------------------------------------------------------------------------
# Test 16 to 28: Provider/Model Identity, Dynamic Latency & Streaming
# ---------------------------------------------------------------------------


def test_provider_and_model_latency_separation_in_footer() -> None:
    """Provider latency and Model latency must render as separate dynamic variables."""
    timer = RequestTimer()
    timer.record(STAGE_BERU_ANALYSIS, 5.0)
    timer.record(STAGE_TIME_TO_FIRST_TOKEN, 184.0)
    timer.record(STAGE_MODEL_GENERATION, 2769.0)

    info = RuntimeInfo(
        selected_model="nex-agi/nex-n2.5-pro:free",
        provider_id="openrouter",
        tier="FAST",
        total_ms=9219.0,
        provider_api_ms=184.0,
        model_api_ms=2769.0,
        tool_timings=[("file_search", 2888.0), ("file_send", 10.0)],
        timing=timer.snapshot(),
        health_status="HEALTHY",
    )

    footer = _build_runtime_footer(info)
    assert "Model: Nex N2.5 Pro" in footer
    assert "Provider: OpenRouter" in footer
    assert "├─ AHJIN: 5ms" in footer
    assert "├─ Provider: 184ms" in footer
    assert "├─ Model: 2769ms" in footer
    assert "├─ File Search: 2888ms" in footer
    assert "├─ File Send: 10ms" in footer
    # Other = 9219 - 5 - 184 - 2888 - 10 - 2769 = 3363ms > 50ms
    assert "├─ Other: 3363ms" in footer
    assert "└─ Total: 9219ms" in footer


@pytest.mark.asyncio
async def test_streaming_measures_ttft_and_generation_separately() -> None:
    """HarnessRunner.run_stream dynamically separates TTFT (Provider) and generation (Model)."""
    catalog = ModelCatalog()
    catalog.register(
        ModelDescriptor(
            model_id="nex-agi/nex-n2.5-pro:free",
            provider_id="openrouter",
            tier=ModelTier.ALL,
            priority=300,
            capabilities=ModelCapabilities(),
        )
    )
    registry = ProviderRegistry()
    registry.register(MockMultiModelProvider("openrouter"))

    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)
    runner = HarnessRunner(gateway=gateway, context_assembler=ContextAssembler())

    timer = RequestTimer()
    plan = ExecutionPlan(
        task_id=uuid4(),
        correlation_id=uuid4(),
        steps=[
            PlanStep(
                model_intent=ModelStepIntent(
                    instruction="Hello",
                    execution_strategy=ExecutionStrategy(preferred_tier="FAST"),
                )
            )
        ],
    )

    chunks = []
    final_info = None
    async for chunk, info in runner.run_stream(
        plan, context=TaskContext(session_id="test"), timer=timer
    ):
        chunks.append(chunk)
        if info:
            final_info = info

    assert "".join(chunks) == "Chunk 1 Chunk 2"
    assert final_info is not None
    assert final_info.runtime_info is not None
    assert final_info.runtime_info.selected_model == "nex-agi/nex-n2.5-pro:free"
    assert final_info.runtime_info.provider_id == "openrouter"
    # Verify TTFT stage was recorded
    assert STAGE_TIME_TO_FIRST_TOKEN in timer.snapshot()
    assert final_info.runtime_info.provider_api_ms >= 0.0


@pytest.mark.asyncio
async def test_orchestrator_forwards_planner_reroute_to_execution_plan() -> None:
    """BeruOrchestrator must forward planner route history onto the ExecutionPlan."""

    class MockPlanner:
        async def plan_tool_intent(self, text: str) -> PlannerResult:
            return PlannerResult(
                status=PlannerStatus.NO_TOOL,
                attempted_models=["minimax/minimax-m3", "nvidia/nemotron-3.5-lightning:free"],
                selected_model="nvidia/nemotron-3.5-lightning:free",
                first_failed_model="minimax/minimax-m3",
                first_failure_reason="HTTP 402",
                was_rerouted=True,
            )

    orchestrator = BeruOrchestrator(tool_planner=MockPlanner())  # type: ignore[arg-type]
    req = TaskRequest(
        task_id=uuid4(),
        correlation_id=uuid4(),
        intent=UserIntent(
            primary_text="Could you retrieve that invoice document for me?",
        ),
        context=TaskContext(session_id="test_session"),
        metadata=RequestMetadata(),
    )

    plan = await orchestrator.plan(req)
    assert plan.planner_was_rerouted is True
    assert plan.planner_failed_model == "minimax/minimax-m3"
    assert plan.planner_failure_reason == "HTTP 402"
    assert plan.planner_route_history == [
        "minimax/minimax-m3",
        "nvidia/nemotron-3.5-lightning:free",
    ]


@pytest.mark.asyncio
async def test_defensive_guard_allows_more_than_five_cloud_fallbacks() -> None:
    """Verifies Harness traverses 7 cloud models sequentially without an arbitrary 2/3/4/5 limit."""
    catalog = ModelCatalog()
    failing = set()
    for i in range(1, 8):
        m_id = f"mock/cloud-model-{i}"
        catalog.register(
            ModelDescriptor(
                model_id=m_id,
                provider_id="openrouter",
                tier=ModelTier.FAST,
                priority=100 - i,  # model-1 highest priority down to model-7
                capabilities=ModelCapabilities(),
            )
        )
        if i < 7:
            failing.add(m_id)

    registry = ProviderRegistry()
    registry.register(MockMultiModelProvider("openrouter", failing_models=failing))
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)
    runner = HarnessRunner(gateway=gateway, context_assembler=ContextAssembler())

    plan = ExecutionPlan(
        task_id=uuid4(),
        correlation_id=uuid4(),
        steps=[
            PlanStep(
                model_intent=ModelStepIntent(
                    instruction="Test 7-model traversal",
                    execution_strategy=ExecutionStrategy(preferred_tier="FAST"),
                )
            )
        ],
    )

    res = await runner.run(plan, context=TaskContext(session_id="test"))
    assert res.success is True
    assert res.runtime_info.selected_model == "mock/cloud-model-7"
    assert len(res.runtime_info.harness_route_history) == 7
    assert res.runtime_info.harness_route_history == [f"mock/cloud-model-{i}" for i in range(1, 8)]


@pytest.mark.asyncio
async def test_final_model_and_provider_identity_after_fallback() -> None:
    """The footer must display the Model and Provider that actually produced the response."""
    catalog = ModelCatalog()
    catalog.register(
        ModelDescriptor(
            model_id="minimax/minimax-m3",
            provider_id="openrouter",
            tier=ModelTier.ALL,
            priority=300,
            capabilities=ModelCapabilities(),
        )
    )
    catalog.register(
        ModelDescriptor(
            model_id="nvidia/nemotron-3-ultra-550b-a55b",
            provider_id="nvidia",
            tier=ModelTier.ALL,
            priority=200,
            capabilities=ModelCapabilities(),
        )
    )

    registry = ProviderRegistry()
    registry.register(
        MockMultiModelProvider("openrouter", failing_models={"minimax/minimax-m3"})
    )
    registry.register(MockMultiModelProvider("nvidia"))

    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)
    runner = HarnessRunner(gateway=gateway, context_assembler=ContextAssembler())

    plan = ExecutionPlan(
        task_id=uuid4(),
        correlation_id=uuid4(),
        steps=[
            PlanStep(
                model_intent=ModelStepIntent(
                    instruction="Hello",
                    execution_strategy=ExecutionStrategy(preferred_tier="FAST"),
                )
            )
        ],
    )

    res = await runner.run(plan, context=TaskContext(session_id="test"))
    assert res.success is True
    assert res.runtime_info.selected_model == "nvidia/nemotron-3-ultra-550b-a55b"
    assert res.runtime_info.provider_id == "nvidia"

    footer = _build_runtime_footer(res.runtime_info)
    assert "Model: Nemotron Ultra 550B" in footer
    assert "Provider: NVIDIA" in footer
    # Must NOT say OpenRouter or MiniMax as final
    assert "Provider: OpenRouter" not in footer


def test_individual_tool_timings_remain_dynamic_in_footer() -> None:
    """File Search, File Read, File Send, Web Search, and Browser timings are
    all dynamically preserved.
    """
    timer = RequestTimer()
    timer.record(STAGE_BERU_ANALYSIS, 3.0)
    timer.record(STAGE_TIME_TO_FIRST_TOKEN, 120.0)
    timer.record(STAGE_MODEL_GENERATION, 1500.0)

    info = RuntimeInfo(
        selected_model="minimax/minimax-m3",
        provider_id="openrouter",
        tier="FAST",
        total_ms=5000.0,
        provider_api_ms=120.0,
        model_api_ms=1500.0,
        tool_timings=[
            ("file_search", 345.0),
            ("file_read", 89.0),
            ("file_send", 25.0),
            ("web_search", 412.0),
            ("browser", 678.0),
        ],
        timing=timer.snapshot(),
        health_status="HEALTHY",
    )

    footer = _build_runtime_footer(info)
    assert "├─ File Search: 345ms" in footer
    assert "├─ File Read: 89ms" in footer
    assert "├─ File Send: 25ms" in footer
    assert "├─ Web Search: 412ms" in footer
    assert "├─ Browser: 678ms" in footer
    assert "├─ Provider: 120ms" in footer
    assert "├─ Model: 1500ms" in footer
    assert "└─ Total: 5000ms" in footer


# ---------------------------------------------------------------------------
# Test 29 to 33: Full Chronological Rerouting Telemetry Chain
# ---------------------------------------------------------------------------


def test_chronological_planner_reroute_chain_multi_candidate() -> None:
    """Validate multi-candidate chronological Planner chain with multiple failure types."""
    info = RuntimeInfo(
        selected_model="gemma3:4b",
        provider_id="ollama",
        tier="FAST",
        was_rerouted=True,
        planner_was_rerouted=True,
        planner_attempts=[
            RerouteAttempt(model_id="nex-agi/nex-n2.5-pro:free", success=False, reason="HTTP 402"),
            RerouteAttempt(
                model_id="nvidia/nemotron-3.5-lightning:free",
                success=False,
                reason="inactivity_timeout",
            ),
            RerouteAttempt(
                model_id="nvidia/nemotron-3.5-lightning-30b-a3b",
                success=False,
                reason="timeout",
            ),
            RerouteAttempt(model_id="gemma3:4b", success=True),
        ],
        harness_was_rerouted=False,
        health_status="LOCAL",
    )
    footer = _build_runtime_footer(info)
    expected_planner_line = (
        "Planner: Nex N2.5 Pro ❌ 402 → Nemotron 3.5 Lightning ❌ timeout → "
        "Nemotron Lightning 30B ❌ timeout → Gemma 3 4B ✅"
    )
    assert expected_planner_line in footer
    assert "Harness: Gemma 3 4B" in footer


def test_chronological_planner_chain_skips_unhealthy_model() -> None:
    """Unhealthy models that were never attempted must NOT appear in the attempt chain."""
    info = RuntimeInfo(
        selected_model="gemma3:4b",
        provider_id="ollama",
        tier="FAST",
        was_rerouted=True,
        planner_was_rerouted=True,
        # Nex N2.5 Pro was unhealthy, so only Nemotron Lightning and Gemma were attempted
        planner_attempts=[
            RerouteAttempt(
                model_id="nvidia/nemotron-3.5-lightning:free",
                success=False,
                reason="timeout",
            ),
            RerouteAttempt(
                model_id="nvidia/nemotron-3.5-lightning-30b-a3b",
                success=False,
                reason="timeout",
            ),
            RerouteAttempt(model_id="gemma3:4b", success=True),
        ],
        harness_was_rerouted=False,
        health_status="LOCAL",
    )
    footer = _build_runtime_footer(info)
    expected_planner_line = (
        "Planner: Nemotron 3.5 Lightning ❌ timeout → "
        "Nemotron Lightning 30B ❌ timeout → Gemma 3 4B ✅"
    )
    assert expected_planner_line in footer
    assert "Nex N2.5 Pro" not in footer
    assert "MiniMax" not in footer


def test_chronological_harness_reroute_chain_multi_candidate() -> None:
    """Validate multi-candidate chronological Harness chain independent of Planner."""
    info = RuntimeInfo(
        selected_model="nvidia/nemotron-3-ultra-550b-a55b",
        provider_id="nvidia",
        tier="HEAVY",
        was_rerouted=True,
        planner_was_rerouted=False,
        harness_was_rerouted=True,
        harness_attempts=[
            RerouteAttempt(model_id="nex-agi/nex-n2.5-pro:free", success=False, reason="HTTP 402"),
            RerouteAttempt(
                model_id="nvidia/nemotron-3-ultra-550b-a55b:free",
                success=False,
                reason="timeout",
            ),
            RerouteAttempt(model_id="nvidia/nemotron-3-ultra-550b-a55b", success=True),
        ],
        health_status="HEALTHY",
    )
    footer = _build_runtime_footer(info)
    assert "Planner:" not in footer
    expected_harness_line = (
        "Harness: Nex N2.5 Pro ❌ 402 → Nemotron Ultra ❌ timeout → Nemotron Ultra 550B ✅"
    )
    assert expected_harness_line in footer


def test_chronological_independent_both_rerouted() -> None:
    """Planner and Harness reroute chains must remain completely independent."""
    info = RuntimeInfo(
        selected_model="gemma3:4b",
        provider_id="ollama",
        tier="FAST",
        was_rerouted=True,
        planner_was_rerouted=True,
        planner_attempts=[
            RerouteAttempt(model_id="nex-agi/nex-n2.5-pro:free", success=False, reason="HTTP 402"),
            RerouteAttempt(model_id="nvidia/nemotron-3.5-lightning:free", success=True),
        ],
        harness_was_rerouted=True,
        harness_attempts=[
            RerouteAttempt(
                model_id="nvidia/nemotron-3.5-lightning:free",
                success=False,
                reason="network error",
            ),
            RerouteAttempt(model_id="gemma3:4b", success=True),
        ],
        health_status="LOCAL",
    )
    footer = _build_runtime_footer(info)
    assert "Planner: Nex N2.5 Pro ❌ 402 → Nemotron 3.5 Lightning ✅" in footer
    assert "Harness: Nemotron 3.5 Lightning ❌ network error → Gemma 3 4B ✅" in footer


def test_chronological_all_attempts_failed_no_success_indicator() -> None:
    """When all candidate attempts fail, all display failure reasons and none displays ✅."""
    info = RuntimeInfo(
        selected_model="system",
        provider_id="ahjin",
        tier="FAST",
        was_rerouted=True,
        planner_was_rerouted=True,
        planner_attempts=[
            RerouteAttempt(model_id="nex-agi/nex-n2.5-pro:free", success=False, reason="HTTP 402"),
            RerouteAttempt(
                model_id="nvidia/nemotron-3.5-lightning:free",
                success=False,
                reason="timeout",
            ),
        ],
        health_status="UNHEALTHY",
    )
    footer = _build_runtime_footer(info)
    assert "Planner: Nex N2.5 Pro ❌ 402 → Nemotron 3.5 Lightning ❌ timeout" in footer
    assert "✅" not in footer

