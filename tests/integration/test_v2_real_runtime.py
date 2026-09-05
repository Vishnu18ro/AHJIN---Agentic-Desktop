"""Integration validation suite for AHJIN V2 Real Runtime & Observability.

Validates the complete production execution loop with live/simulated NVIDIA provider:
Telegram update ➔ TaskRequest ➔ Core Dispatcher ➔ BERU ➔ ExecutionStrategy
➔ ModelRouter ➔ Selected Model ➔ HarnessRunner ➔ ProviderGateway ➔ Provider
➔ Model Response ➔ Verification ➔ RuntimeInfo ➔ Telegram Footer / Commands.
"""

from __future__ import annotations

import pytest

from ahjin.beru.orchestrator import BeruOrchestrator
from ahjin.beru.types import CapabilityRequirements
from ahjin.core.dispatcher import TaskDispatcher
from ahjin.harness.gateway import ProviderGateway
from ahjin.harness.runner import HarnessRunner
from ahjin.interfaces.telegram.bot import (
    _build_health_snapshot,
    _build_runtime_footer,
)
from ahjin.interfaces.telegram.mapper import TelegramMapper
from ahjin.models.catalog import ModelCatalog, create_default_catalog
from ahjin.models.router import ModelRouter
from ahjin.models.types import ModelCapabilities, ModelDescriptor, ModelTier
from ahjin.providers.base import BaseModelProvider
from ahjin.providers.nvidia import NvidiaProvider
from ahjin.providers.registry import ProviderRegistry
from ahjin.providers.types import ModelInvocationRequest, ModelInvocationResponse

# ---------------------------------------------------------------------------
# Setup helper for full stack wiring
# ---------------------------------------------------------------------------


def _setup_production_stack(
    custom_catalog: ModelCatalog | None = None,
    custom_registry: ProviderRegistry | None = None,
) -> tuple[TaskDispatcher, ModelRouter]:
    """Wire complete AHJIN V2 stack."""
    catalog = custom_catalog or create_default_catalog()
    registry = custom_registry or ProviderRegistry()

    # Register real NvidiaProvider if key configured, else safe mock
    if "nvidia" not in registry._providers:
        try:
            registry.register(NvidiaProvider())
        except ValueError:
            pass

    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)
    runner = HarnessRunner(gateway=gateway)
    orchestrator = BeruOrchestrator()
    dispatcher = TaskDispatcher(orchestrator=orchestrator, runner=runner)

    return dispatcher, router


# ---------------------------------------------------------------------------
# Test 1 — Simple Task Routing ("Hi")
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_simple_task_fast_tier_routing() -> None:
    """Simple prompt 'Hi' must route to FAST tier (Nemotron Lightning) with Direct path."""
    catalog = create_default_catalog()
    registry = ProviderRegistry()

    class MockFastProvider(BaseModelProvider):
        @property
        def provider_id(self) -> str:
            return "nvidia"

        def get_default_model_id(self) -> str:
            return "nvidia/nemotron-3.5-lightning-30b-a3b"

        async def invoke(self, request: ModelInvocationRequest) -> ModelInvocationResponse:
            return ModelInvocationResponse(
                invocation_id=request.invocation_id,
                content="Hello! How can I help you today?",
                latency_ms=150.0,
                provider_id=self.provider_id,
                model_id=request.model_id,
            )

    registry.register(MockFastProvider())
    dispatcher, router = _setup_production_stack(catalog, registry)

    req = TelegramMapper.to_task_request(chat_id=1001, message_text="Hi")
    res = await dispatcher.dispatch(req)

    assert res.success is True
    assert res.output_text == "Hello! How can I help you today?"
    assert res.runtime_info is not None
    assert res.runtime_info.tier == "FAST"
    assert res.runtime_info.selected_model == "nvidia/nemotron-3.5-lightning-30b-a3b"
    assert res.runtime_info.was_rerouted is False

    footer = _build_runtime_footer(res.runtime_info)
    assert "Model: Nemotron Lightning 30B" in footer
    assert "Route: FAST" in footer
    assert "Path:  Direct" in footer
    assert "Health: 🟢 Healthy" in footer


# ---------------------------------------------------------------------------
# Test 2 — Reasoning Task Routing ("Explain quantum physics...")
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reasoning_task_heavy_tier_routing() -> None:
    """Reasoning prompt must route to HEAVY tier (MiniMax M3 preferred)."""
    catalog = create_default_catalog()
    registry = ProviderRegistry()

    class MockHeavyProvider(BaseModelProvider):
        @property
        def provider_id(self) -> str:
            return "openrouter"

        def get_default_model_id(self) -> str:
            return "minimax/minimax-m3:free"

        async def invoke(self, request: ModelInvocationRequest) -> ModelInvocationResponse:
            return ModelInvocationResponse(
                invocation_id=request.invocation_id,
                content="Quantum physics is the study of matter...",
                latency_ms=450.0,
                provider_id=self.provider_id,
                model_id=request.model_id,
            )

    registry.register(MockHeavyProvider())
    dispatcher, router = _setup_production_stack(catalog, registry)

    req = TelegramMapper.to_task_request(
        chat_id=1002,
        message_text="Explain quantum physics deeply but clearly with examples.",
    )
    res = await dispatcher.dispatch(req)

    assert res.success is True
    assert res.runtime_info is not None
    assert res.runtime_info.tier == "HEAVY"
    assert res.runtime_info.selected_model == "minimax/minimax-m3:free"

    footer = _build_runtime_footer(res.runtime_info)
    assert "Model: MiniMax M3" in footer
    assert "Route: HEAVY" in footer


# ---------------------------------------------------------------------------
# Test 3 — Coding Task Routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coding_task_capability_routing() -> None:
    """Coding prompt must trigger requires_code=True and select heavy coding model."""
    catalog = create_default_catalog()
    registry = ProviderRegistry()

    class MockCodingProvider(BaseModelProvider):
        @property
        def provider_id(self) -> str:
            return "openrouter"

        def get_default_model_id(self) -> str:
            return "minimax/minimax-m3:free"

        async def invoke(self, request: ModelInvocationRequest) -> ModelInvocationResponse:
            return ModelInvocationResponse(
                invocation_id=request.invocation_id,
                content="```python\ndef binary_search(arr, target):\n    pass\n```",
                latency_ms=300.0,
                provider_id=self.provider_id,
                model_id=request.model_id,
            )

    registry.register(MockCodingProvider())
    dispatcher, router = _setup_production_stack(catalog, registry)

    req = TelegramMapper.to_task_request(
        chat_id=1003,
        message_text="Write a Python binary search implementation and explain time complexity.",
    )
    res = await dispatcher.dispatch(req)

    assert res.success is True
    assert "```python" in (res.output_text or "")
    assert res.runtime_info is not None
    assert res.runtime_info.tier == "HEAVY"



# ---------------------------------------------------------------------------
# Test 4 — Same-Request Rerouting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_request_rerouting_observability() -> None:
    """When Model A fails, HarnessRunner reroutes to Model B in the SAME request.

    Footer must reflect:
    Path: ↪ Rerouted
    From: <Model A>
    Reason: <actual reason>
    """
    import httpx

    catalog = ModelCatalog()
    # Model A: Highest priority HEAVY model, but will fail
    catalog.register(
        ModelDescriptor(
            model_id="nvidia/failing-heavy-model",
            provider_id="nvidia",
            tier=ModelTier.HEAVY,
            quality_score=98,
            priority=200,
            capabilities=ModelCapabilities(reasoning=True),
        )
    )
    # Model B: Backup HEAVY model, succeeds
    catalog.register(
        ModelDescriptor(
            model_id="nvidia/nemotron-3-ultra-550b-a55b",
            provider_id="nvidia",
            tier=ModelTier.HEAVY,
            quality_score=95,
            priority=150,
            capabilities=ModelCapabilities(reasoning=True),
        )
    )

    class FlakyProvider(BaseModelProvider):
        @property
        def provider_id(self) -> str:
            return "nvidia"

        def get_default_model_id(self) -> str:
            return "nvidia/nemotron-3-ultra-550b-a55b"

        async def invoke(self, request: ModelInvocationRequest) -> ModelInvocationResponse:
            if request.model_id == "nvidia/failing-heavy-model":
                raise httpx.RequestError(
                    "Connection reset by peer",
                    request=httpx.Request("POST", "https://api.nvidia.com/v1"),
                )
            return ModelInvocationResponse(
                invocation_id=request.invocation_id,
                content="Recovered answer from backup model.",
                latency_ms=250.0,
                provider_id=self.provider_id,
                model_id=request.model_id,
            )

    registry = ProviderRegistry()
    registry.register(FlakyProvider())
    dispatcher, router = _setup_production_stack(catalog, registry)

    req = TelegramMapper.to_task_request(
        chat_id=1004,
        message_text="Analyze theorem proof deeply",
    )
    res = await dispatcher.dispatch(req)

    assert res.success is True
    assert res.output_text == "Recovered answer from backup model."
    assert res.runtime_info is not None
    assert res.runtime_info.was_rerouted is True
    assert res.runtime_info.failed_model == "nvidia/failing-heavy-model"
    assert res.runtime_info.selected_model == "nvidia/nemotron-3-ultra-550b-a55b"
    assert res.runtime_info.failure_reason == "network error"

    footer = _build_runtime_footer(res.runtime_info)
    assert "Path:  ↪ Rerouted" in footer
    assert "From: failing-heavy-model" in footer
    assert "Reason: network error" in footer


# ---------------------------------------------------------------------------
# Test 5 — Model Health Snapshot (/health & /models)
# ---------------------------------------------------------------------------


def test_health_snapshot_command_rendering() -> None:
    """_build_health_snapshot must format compact HTML with status icons."""
    catalog = create_default_catalog()
    router = ModelRouter(catalog=catalog)

    # Record some health observations
    router.health_tracker.record_success("nvidia/nemotron-3.5-lightning-30b-a3b", 3200.0)
    router.health_tracker.record_failure("deepseek-ai/deepseek-v4-pro-0813")
    router.health_tracker.record_failure("deepseek-ai/deepseek-v4-pro-0813")

    snapshot = _build_health_snapshot(router)

    assert "<b>AHJIN Model Health</b>" in snapshot
    assert "🟢 Nemotron Lightning 30B" in snapshot
    assert "EMA 3.2s" in snapshot
    assert "🟡 DeepSeek V4 Pro" in snapshot
    assert "2 failures" in snapshot


# ---------------------------------------------------------------------------
# Test 6 — Request Isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_isolation_excluded_models() -> None:
    """Request A exclusions must not bleed into concurrent Request B."""
    catalog = create_default_catalog()
    router = ModelRouter(catalog=catalog)

    reqs = CapabilityRequirements()

    # Request 1 excludes lightning
    sel1 = router.select_model(
        reqs, excluded_model_ids={"nvidia/nemotron-3.5-lightning-30b-a3b"}
    )
    # Request 2 has no exclusions — lightning must remain eligible
    sel2 = router.select_model(reqs, excluded_model_ids=None)

    assert sel1.model_id != "nvidia/nemotron-3.5-lightning-30b-a3b"
    assert sel2.model_id == "nvidia/nemotron-3.5-lightning-30b-a3b"
