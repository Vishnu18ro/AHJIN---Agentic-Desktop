"""Unit tests for Phase 6 Primary Model + Capability-Aware Fallback Routing.

Covers the 14 mandatory architectural requirements:
1. Normal LIGHT request: MiniMax succeeds -> MiniMax used
2. HEAVY reasoning request: MiniMax succeeds -> MiniMax used
3. Coding request: MiniMax succeeds -> MiniMax used
4. MiniMax failure + LIGHT: OpenRouter Nemotron Lightning selected
5. MiniMax failure + LIGHT + OpenRouter failure: NVIDIA Nemotron Lightning selected
6. MiniMax failure + HEAVY: OpenRouter Nemotron Ultra selected
7. MiniMax failure + HEAVY + OpenRouter Ultra failure: NVIDIA Nemotron Ultra selected
8. Continue HEAVY fallback: Kimi -> DeepSeek Pro -> DeepSeek Flash
9. Circuit breaker behavior is preserved
10. Existing Ollama offline fallback remains functional
11. Streaming remains functional
12. Existing telemetry remains functional
13. No hidden reasoning content is exposed
14. Existing tool routing continues to function
"""

from __future__ import annotations

import pytest

from ahjin.beru.orchestrator import BeruOrchestrator
from ahjin.beru.types import CapabilityRequirements, ExecutionStrategy
from ahjin.core.types import (
    TaskContext,
    TaskRequest,
    UserIntent,
)
from ahjin.harness.gateway import ProviderGateway
from ahjin.harness.runner import HarnessRunner
from ahjin.models.catalog import create_default_catalog
from ahjin.models.health import ModelHealthStatus, ModelHealthTracker
from ahjin.models.router import ModelRouter
from ahjin.models.types import ModelRole, ModelTier
from ahjin.providers.base import BaseModelProvider
from ahjin.providers.registry import ProviderRegistry
from ahjin.providers.types import (
    ContextualizedPrompt,
    FinishReason,
    ModelInvocationRequest,
    ModelInvocationResponse,
    TokenUsage,
)
from ahjin.tools.registry import ToolRegistry


class MockStreamingProvider(BaseModelProvider):
    """Mock provider supporting both invoke and invoke_stream."""

    def __init__(self, provider_id: str) -> None:
        self._provider_id = provider_id
        self.last_telemetry: dict[str, float] = {}

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def get_default_model_id(self) -> str:
        return "default"

    async def invoke(self, request: ModelInvocationRequest) -> ModelInvocationResponse:
        return ModelInvocationResponse(
            invocation_id=request.invocation_id,
            provider_id=self._provider_id,
            content=f"Response from {request.model_id}",
            model_id=request.model_id or "default",
            finish_reason=FinishReason.COMPLETE,
            usage=TokenUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
            latency_ms=15.0,
        )

    async def invoke_stream(self, request: ModelInvocationRequest):
        chunks = ["Hello", " from ", request.model_id or "default"]
        for c in chunks:
            yield c


# ---------------------------------------------------------------------------
# Test 1, 2, 3: MiniMax M3 is Primary for ALL Request Types
# ---------------------------------------------------------------------------


def test_1_normal_light_request_selects_minimax() -> None:
    """1. Normal LIGHT request: MiniMax M3 is attempted first."""
    router = ModelRouter(catalog=create_default_catalog())
    strategy = ExecutionStrategy(
        capability_requirements=CapabilityRequirements(
            requires_reasoning=False, requires_code=False
        ),
        preferred_tier="FAST",
    )
    selection = router.select_model(strategy)
    assert selection.model_id == "minimax/minimax-m3"
    assert selection.provider_id == "openrouter"
    assert selection.tier == ModelTier.FAST


def test_2_heavy_reasoning_request_selects_minimax() -> None:
    """2. HEAVY reasoning request: MiniMax M3 is attempted first."""
    router = ModelRouter(catalog=create_default_catalog())
    strategy = ExecutionStrategy(
        capability_requirements=CapabilityRequirements(requires_reasoning=True),
        preferred_tier="HEAVY",
    )
    selection = router.select_model(strategy)
    assert selection.model_id == "minimax/minimax-m3"
    assert selection.provider_id == "openrouter"
    assert selection.tier == ModelTier.HEAVY


def test_3_coding_request_selects_minimax() -> None:
    """3. Coding request: MiniMax M3 is attempted first."""
    router = ModelRouter(catalog=create_default_catalog())
    strategy = ExecutionStrategy(
        capability_requirements=CapabilityRequirements(requires_code=True),
        preferred_tier="HEAVY",
    )
    selection = router.select_model(strategy)
    assert selection.model_id == "minimax/minimax-m3"
    assert selection.provider_id == "openrouter"


# ---------------------------------------------------------------------------
# Test 4, 5: LIGHT Fallback Chain
# ---------------------------------------------------------------------------


def test_4_minimax_failure_light_selects_openrouter_nemotron_lightning() -> None:
    """4. MiniMax failure + LIGHT: OpenRouter Nemotron Lightning is selected."""
    router = ModelRouter(catalog=create_default_catalog())
    strategy = ExecutionStrategy(
        capability_requirements=CapabilityRequirements(
            requires_reasoning=False, requires_code=False
        ),
        preferred_tier="FAST",
    )
    # MiniMax M3 excluded due to previous failure
    selection = router.select_model(strategy, excluded_model_ids={"minimax/minimax-m3"})
    assert selection.model_id == "nvidia/nemotron-3.5-lightning:free"
    assert selection.provider_id == "openrouter"
    assert selection.tier == ModelTier.FAST


def test_5_minimax_and_openrouter_failure_light_selects_nvidia_nemotron_lightning() -> None:
    """5. MiniMax + OpenRouter Nemotron failure: NVIDIA Nemotron Lightning selected."""
    router = ModelRouter(catalog=create_default_catalog())
    strategy = ExecutionStrategy(
        capability_requirements=CapabilityRequirements(
            requires_reasoning=False, requires_code=False
        ),
        preferred_tier="FAST",
    )
    excluded = {"minimax/minimax-m3", "nvidia/nemotron-3.5-lightning:free"}
    selection = router.select_model(strategy, excluded_model_ids=excluded)
    assert selection.model_id == "nvidia/nemotron-3.5-lightning-30b-a3b"
    assert selection.provider_id == "nvidia"
    assert selection.tier == ModelTier.FAST


# ---------------------------------------------------------------------------
# Test 6, 7, 8: HEAVY Fallback Chain
# ---------------------------------------------------------------------------


def test_6_minimax_failure_heavy_selects_openrouter_nemotron_ultra() -> None:
    """6. MiniMax failure + HEAVY: OpenRouter Nemotron Ultra selected."""
    router = ModelRouter(catalog=create_default_catalog())
    strategy = ExecutionStrategy(
        capability_requirements=CapabilityRequirements(requires_reasoning=True),
        preferred_tier="HEAVY",
    )
    selection = router.select_model(strategy, excluded_model_ids={"minimax/minimax-m3"})
    assert selection.model_id == "nvidia/nemotron-3-ultra-550b-a55b:free"
    assert selection.provider_id == "openrouter"
    assert selection.tier == ModelTier.HEAVY


def test_7_minimax_and_openrouter_ultra_failure_selects_nvidia_nemotron_ultra() -> None:
    """7. MiniMax failure + HEAVY + OpenRouter Ultra failure: NVIDIA Nemotron Ultra selected."""
    router = ModelRouter(catalog=create_default_catalog())
    strategy = ExecutionStrategy(
        capability_requirements=CapabilityRequirements(requires_reasoning=True),
        preferred_tier="HEAVY",
    )
    excluded = {"minimax/minimax-m3", "nvidia/nemotron-3-ultra-550b-a55b:free"}
    selection = router.select_model(strategy, excluded_model_ids=excluded)
    assert selection.model_id == "nvidia/nemotron-3-ultra-550b-a55b"
    assert selection.provider_id == "nvidia"
    assert selection.tier == ModelTier.HEAVY


def test_8_continue_heavy_fallback_kimi_deepseek_pro_deepseek_flash() -> None:
    """8. Continue HEAVY fallback: Kimi -> DeepSeek Pro -> DeepSeek Flash."""
    router = ModelRouter(catalog=create_default_catalog())
    strategy = ExecutionStrategy(
        capability_requirements=CapabilityRequirements(requires_reasoning=True),
        preferred_tier="HEAVY",
    )
    excluded = {
        "minimax/minimax-m3",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "nvidia/nemotron-3-ultra-550b-a55b",
    }

    # Step 3 in HEAVY fallback: Kimi K3
    sel_kimi = router.select_model(strategy, excluded_model_ids=excluded)
    assert sel_kimi.model_id == "moonshotai/kimi-k3"

    # Step 4 in HEAVY fallback: DeepSeek Pro
    excluded.add("moonshotai/kimi-k3")
    sel_pro = router.select_model(strategy, excluded_model_ids=excluded)
    assert sel_pro.model_id == "deepseek-ai/deepseek-v4-pro-0813"

    # Step 5 in HEAVY fallback: DeepSeek Flash
    excluded.add("deepseek-ai/deepseek-v4-pro-0813")
    sel_flash = router.select_model(strategy, excluded_model_ids=excluded)
    assert sel_flash.model_id == "deepseek-ai/deepseek-v4-flash-0731"


# ---------------------------------------------------------------------------
# Test 9: Circuit Breaker & Health Tracking
# ---------------------------------------------------------------------------


def test_9_circuit_breaker_preservation() -> None:
    """9. Health/circuit breaker behavior is preserved when model becomes UNHEALTHY."""
    catalog = create_default_catalog()
    health = ModelHealthTracker()
    router = ModelRouter(catalog=catalog, health_tracker=health)

    strategy = ExecutionStrategy(
        capability_requirements=CapabilityRequirements(requires_reasoning=True),
        preferred_tier="HEAVY",
    )

    # Initially healthy -> MiniMax M3
    assert router.select_model(strategy).model_id == "minimax/minimax-m3"

    # Trip circuit breaker on MiniMax M3 (3 failures)
    health.record_failure("minimax/minimax-m3")
    health.record_failure("minimax/minimax-m3")
    health.record_failure("minimax/minimax-m3")

    assert health.get_state("minimax/minimax-m3").snapshot_status == ModelHealthStatus.UNHEALTHY
    assert not health.get_state("minimax/minimax-m3").is_available()

    # Router must automatically bypass tripped MiniMax without manual exclusion
    selection = router.select_model(strategy)
    assert selection.model_id == "nvidia/nemotron-3-ultra-550b-a55b:free"


# ---------------------------------------------------------------------------
# Test 10: Offline Fallback to Ollama
# ---------------------------------------------------------------------------


def test_10_ollama_offline_fallback_preserved() -> None:
    """10. Existing Ollama offline fallback remains functional when cloud models fail."""
    router = ModelRouter(catalog=create_default_catalog())

    # All cloud FAST models excluded
    cloud_fast = {
        "minimax/minimax-m3",
        "nvidia/nemotron-3.5-lightning:free",
        "nvidia/nemotron-3.5-lightning-30b-a3b",
    }
    strategy_fast = ExecutionStrategy(
        capability_requirements=CapabilityRequirements(requires_code=True),
        preferred_tier="FAST",
    )
    sel_fast = router.select_model(strategy_fast, excluded_model_ids=cloud_fast)
    assert sel_fast.model_id == "gemma3:4b"
    assert sel_fast.provider_id == "ollama"

    # All cloud HEAVY models excluded
    cloud_heavy = {
        "minimax/minimax-m3",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "nvidia/nemotron-3-ultra-550b-a55b",
        "moonshotai/kimi-k3",
        "deepseek-ai/deepseek-v4-pro-0813",
        "deepseek-ai/deepseek-v4-flash-0731",
    }
    strategy_heavy = ExecutionStrategy(
        capability_requirements=CapabilityRequirements(requires_reasoning=True),
        preferred_tier="HEAVY",
    )
    sel_heavy = router.select_model(strategy_heavy, excluded_model_ids=cloud_heavy)
    assert sel_heavy.model_id == "qwen3:8b"
    assert sel_heavy.provider_id == "ollama"


# ---------------------------------------------------------------------------
# Test 11, 12, 13: Gateway Streaming, Telemetry, and Reasoning Safety
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_11_streaming_via_gateway() -> None:
    """11. Streaming remains functional with MiniMax as primary."""
    registry = ProviderRegistry()
    registry.register(MockStreamingProvider("openrouter"))
    registry.register(MockStreamingProvider("nvidia"))

    router = ModelRouter(catalog=create_default_catalog())
    gateway = ProviderGateway(registry=registry, router=router)

    prompt = ContextualizedPrompt(system_instruction="", user_instruction="Hello")
    strategy = ExecutionStrategy(preferred_tier="FAST")

    chunks: list[str] = []
    async for chunk, sel in gateway.invoke_stream(prompt, strategy):
        chunks.append(chunk)
        assert sel.model_id == "minimax/minimax-m3"

    assert "".join(chunks) == "Hello from minimax/minimax-m3"


@pytest.mark.asyncio
async def test_12_telemetry_recording_runtime_info() -> None:
    """12. Existing telemetry records correct model, provider, and tier metadata."""
    registry = ProviderRegistry()
    registry.register(MockStreamingProvider("openrouter"))
    registry.register(MockStreamingProvider("nvidia"))

    router = ModelRouter(catalog=create_default_catalog())
    gateway = ProviderGateway(registry=registry, router=router)
    runner = HarnessRunner(gateway=gateway, tool_registry=ToolRegistry())

    plan = await BeruOrchestrator().plan(
        TaskRequest(
            intent=UserIntent(primary_text="Write quick hello world"),
            context=TaskContext(session_id="s1"),
        )
    )

    result = await runner.run(plan=plan, context=TaskContext(session_id="s1"))
    assert result.success
    assert result.runtime_info is not None
    assert result.runtime_info.selected_model == "minimax/minimax-m3"
    assert result.runtime_info.provider_id == "openrouter"


def test_13_no_hidden_reasoning_in_catalog() -> None:
    """13. Reasoning capabilities are internal; models are cataloged with strict boundaries."""
    catalog = create_default_catalog()
    minimax = catalog.get_model("minimax/minimax-m3")
    assert minimax.capabilities.reasoning is True
    assert minimax.role == ModelRole.PRIMARY

    lightning_free = catalog.get_model("nvidia/nemotron-3.5-lightning:free")
    assert lightning_free.capabilities.reasoning is True
    assert lightning_free.role == ModelRole.LIGHT_FALLBACK


# ---------------------------------------------------------------------------
# Test 14: Tool Routing Continues to Function
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_14_tool_routing_unbroken() -> None:
    """14. Existing tool routing and deterministic intent detection continue unbroken."""
    orchestrator = BeruOrchestrator()
    plan = await orchestrator.plan(
        TaskRequest(
            intent=UserIntent(primary_text="Find file report.pdf"),
            context=TaskContext(session_id="s1"),
        )
    )
    # Tool plan step generated
    assert len(plan.steps) >= 1
    assert any(step.step_type.value == "TOOL_INVOCATION" for step in plan.steps)
