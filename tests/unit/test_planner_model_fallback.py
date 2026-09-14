"""PHASE 7.3C — Unit tests for Request-Local Model Rerouting & Architecture Isolation.

Explicit test suite validating Tests A through L as specified in Phase 7.3C requirements:
- TEST A: MiniMax succeeds -> planner uses MiniMax -> no fallback occurs.
- TEST B: MiniMax returns 402 -> next eligible candidate is selected -> planner succeeds.
- TEST C: MiniMax fails -> next candidate fails -> next candidate succeeds.
- TEST D: More than three eligible candidates exist -> fallback continues beyond three candidates.
- TEST E: All eligible candidates fail -> deterministic PLANNER_FAILURE -> no infinite loop.
- TEST F: After Request A causes MiniMax failure, Request B starts with MiniMax again.
- TEST G: Planner fallback does not modify ModelCatalog priorities.
- TEST H: Planner fallback does not modify normal HarnessRunner routing.
- TEST I: Normal response fallback remains unchanged.
- TEST J: Planner "none" result does not trigger unnecessary fallback.
- TEST K: Malformed planner JSON causes request-local candidate exclusion and rerouting.
- TEST L: End-to-end: planner MiniMax fails with 402 -> next model selected -> file_search/file_send
          execute -> deterministic successful file-send response intact.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest

from ahjin.beru.orchestrator import BeruOrchestrator
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
from ahjin.core.types import TaskContext, TaskRequest, UserIntent
from ahjin.harness.gateway import ProviderGateway
from ahjin.harness.runner import HarnessRunner
from ahjin.models.catalog import ModelCatalog
from ahjin.models.health import ModelHealthStatus
from ahjin.models.router import ModelRouter
from ahjin.models.types import ModelCapabilities, ModelDescriptor, ModelTier
from ahjin.providers.base import BaseModelProvider
from ahjin.providers.registry import ProviderRegistry
from ahjin.providers.types import (
    FinishReason,
    ModelInvocationRequest,
    ModelInvocationResponse,
    TokenUsage,
)
from ahjin.security.allow_all import AllowAllPermissionGate
from ahjin.security.path_policy import SafePathPolicy
from ahjin.tools import FileSearchTool, FileSendTool, ToolRegistry
from ahjin.tools.system_info import SystemInfoTool


class MockFallbackProvider(BaseModelProvider):
    """Configurable mock provider simulating model-specific behaviors and sequences."""

    def __init__(
        self,
        provider_id: str,
        responses: dict[str, Any],
        model_latencies: dict[str, float] | None = None,
    ) -> None:
        self._provider_id = provider_id
        # model_id -> str | Exception | list[str | Exception]
        self._responses = responses
        self._model_latencies = model_latencies or {}
        self.invoked_models: list[str] = []

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def get_default_model_id(self) -> str:
        return "default-mock"

    async def invoke(self, request: ModelInvocationRequest) -> ModelInvocationResponse:
        model = request.model_id or "default-mock"
        self.invoked_models.append(model)

        delay = self._model_latencies.get(model, 0.0)
        if delay > 0:
            await asyncio.sleep(delay)

        raw_behavior: Any = self._responses.get(model, "{}")
        if isinstance(raw_behavior, list):
            behavior_list: list[Any] = cast(list[Any], raw_behavior)
            behavior: Any = (
                behavior_list.pop(0) if len(behavior_list) > 1 else behavior_list[0]
            )
        else:
            behavior = raw_behavior

        if isinstance(behavior, Exception):
            raise behavior

        return ModelInvocationResponse(
            invocation_id=request.invocation_id,
            content=str(behavior),
            provider_id=self.provider_id,
            model_id=model,
            finish_reason=FinishReason.COMPLETE,
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
            latency_ms=10.0,
        )


def _make_http_error(status_code: int, message: str) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://api.test/v1/chat/completions")
    resp = httpx.Response(status_code, request=req)
    return httpx.HTTPStatusError(message, request=req, response=resp)


class MockStreamingFallbackProvider(BaseModelProvider):
    """Configurable mock provider simulating chunk-level streaming schedules and delays."""

    def __init__(
        self,
        provider_id: str,
        stream_schedules: dict[str, list[tuple[float, str | Exception]]],
    ) -> None:
        self._provider_id = provider_id
        self._stream_schedules = stream_schedules
        self.invoked_models: list[str] = []
        self.cancelled_models: list[str] = []

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def get_default_model_id(self) -> str:
        return "default-stream-mock"

    async def invoke(self, request: ModelInvocationRequest) -> ModelInvocationResponse:
        chunks: list[str] = []
        async for chunk in self.invoke_stream(request):
            chunks.append(chunk)
        return ModelInvocationResponse(
            invocation_id=request.invocation_id,
            content="".join(chunks),
            provider_id=self.provider_id,
            model_id=request.model_id or "default-stream-mock",
            finish_reason=FinishReason.COMPLETE,
        )

    async def invoke_stream(
        self, request: ModelInvocationRequest
    ) -> AsyncGenerator[str, None]:
        model = request.model_id or "default-stream-mock"
        self.invoked_models.append(model)
        schedule = self._stream_schedules.get(model, [])
        try:
            for delay, item in schedule:
                if delay > 0:
                    await asyncio.sleep(delay)
                if isinstance(item, Exception):
                    raise item
                yield str(item)
        except (GeneratorExit, asyncio.CancelledError):
            self.cancelled_models.append(model)
            raise

    def update_schedule(
        self, model: str, schedule: list[tuple[float, str | Exception]]
    ) -> None:
        self._stream_schedules[model] = schedule


# ---------------------------------------------------------------------------
# TEST A: MiniMax succeeds -> planner uses MiniMax -> no fallback occurs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_minimax_succeeds_no_fallback() -> None:
    """TEST A: When MiniMax succeeds, planner uses MiniMax directly without fallback."""
    catalog = ModelCatalog()
    catalog.register(
        ModelDescriptor(
            model_id="minimax/minimax-m3",
            provider_id="mock-prov",
            tier=ModelTier.FAST,
            priority=300,
            capabilities=ModelCapabilities(),
        )
    )
    catalog.register(
        ModelDescriptor(
            model_id="fallback-model",
            provider_id="mock-prov",
            tier=ModelTier.FAST,
            priority=200,
            capabilities=ModelCapabilities(),
        )
    )

    provider = MockFallbackProvider(
        provider_id="mock-prov",
        responses={
            "minimax/minimax-m3": (
                '{"tool_name": "system_info", "parameters": {"fields": ["cpu"]}}'
            ),
            "fallback-model": (
                '{"tool_name": "system_info", "parameters": {"fields": ["cpu"]}}'
            ),
        },
    )

    registry = ProviderRegistry()
    registry.register(provider)
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)

    tool_registry = ToolRegistry()
    tool_registry.register(SystemInfoTool())

    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)
    result = await planner.plan_tool_intent("what processor do I have?")

    assert result.status == PlannerStatus.TOOL_SELECTED
    assert result.tool_name == "system_info"
    assert provider.invoked_models == ["minimax/minimax-m3"]


# ---------------------------------------------------------------------------
# TEST B: MiniMax returns 402 -> next eligible ModelRouter candidate selected -> succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b_minimax_402_fallback_succeeds() -> None:
    """TEST B: When MiniMax returns HTTP 402, ModelRouter selects the next eligible model."""
    catalog = ModelCatalog()
    catalog.register(
        ModelDescriptor(
            model_id="minimax/minimax-m3",
            provider_id="mock-prov",
            tier=ModelTier.FAST,
            priority=300,
            capabilities=ModelCapabilities(),
        )
    )
    catalog.register(
        ModelDescriptor(
            model_id="candidate-2",
            provider_id="mock-prov",
            tier=ModelTier.FAST,
            priority=220,
            capabilities=ModelCapabilities(),
        )
    )

    provider = MockFallbackProvider(
        provider_id="mock-prov",
        responses={
            "minimax/minimax-m3": _make_http_error(402, "402 Payment Required"),
            "candidate-2": (
                '{"tool_name": "file_search", "parameters": {"query": "doc", "path": "downloads"}}'
            ),
        },
    )

    registry = ProviderRegistry()
    registry.register(provider)
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)

    tool_registry = ToolRegistry()
    tool_registry.register(FileSearchTool())

    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)
    result = await planner.plan_tool_intent("find doc in downloads")

    assert result.status == PlannerStatus.TOOL_SELECTED
    assert result.tool_name == "file_search"
    assert provider.invoked_models == ["minimax/minimax-m3", "candidate-2"]


# ---------------------------------------------------------------------------
# TEST C: MiniMax fails -> next candidate fails -> next candidate succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c_multi_failure_chain_succeeds() -> None:
    """TEST C: Multi-failure fallback chain (cand 1 fails -> cand 2 fails -> cand 3 succeeds)."""
    catalog = ModelCatalog()
    catalog.register(
        ModelDescriptor(
            model_id="minimax/minimax-m3",
            provider_id="mock-prov",
            tier=ModelTier.FAST,
            priority=300,
            capabilities=ModelCapabilities(),
        )
    )
    catalog.register(
        ModelDescriptor(
            model_id="candidate-2",
            provider_id="mock-prov",
            tier=ModelTier.FAST,
            priority=220,
            capabilities=ModelCapabilities(),
        )
    )
    catalog.register(
        ModelDescriptor(
            model_id="candidate-3",
            provider_id="mock-prov",
            tier=ModelTier.FAST,
            priority=180,
            capabilities=ModelCapabilities(),
        )
    )

    provider = MockFallbackProvider(
        provider_id="mock-prov",
        responses={
            "minimax/minimax-m3": _make_http_error(402, "402 Payment Required"),
            "candidate-2": _make_http_error(503, "503 Service Unavailable"),
            "candidate-3": (
                '{"tool_name": "file_send", "parameters": {"path": ".", "query": "report"}}'
            ),
        },
    )

    registry = ProviderRegistry()
    registry.register(provider)
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)

    tool_registry = ToolRegistry()
    tool_registry.register(FileSendTool())

    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)
    result = await planner.plan_tool_intent("send report")

    assert result.status == PlannerStatus.TOOL_SELECTED
    assert result.tool_name == "file_send"
    assert provider.invoked_models == [
        "minimax/minimax-m3",
        "candidate-2",
        "candidate-3",
    ]


# ---------------------------------------------------------------------------
# TEST D: More than three eligible candidates exist -> fallback continues beyond 3
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_d_fallback_continues_beyond_three_candidates() -> None:
    """TEST D: Fallback is NOT capped at 2 or 3 models; continues through catalog until success."""
    catalog = ModelCatalog()
    catalog.register(
        ModelDescriptor(
            model_id="cand-1",
            provider_id="mock-prov",
            tier=ModelTier.FAST,
            priority=400,
            capabilities=ModelCapabilities(),
        )
    )
    catalog.register(
        ModelDescriptor(
            model_id="cand-2",
            provider_id="mock-prov",
            tier=ModelTier.FAST,
            priority=300,
            capabilities=ModelCapabilities(),
        )
    )
    catalog.register(
        ModelDescriptor(
            model_id="cand-3",
            provider_id="mock-prov",
            tier=ModelTier.FAST,
            priority=200,
            capabilities=ModelCapabilities(),
        )
    )
    catalog.register(
        ModelDescriptor(
            model_id="cand-4",
            provider_id="mock-prov",
            tier=ModelTier.FAST,
            priority=100,
            capabilities=ModelCapabilities(),
        )
    )

    provider = MockFallbackProvider(
        provider_id="mock-prov",
        responses={
            "cand-1": _make_http_error(402, "402 Payment Required"),
            "cand-2": _make_http_error(500, "500 Internal Error"),
            "cand-3": _make_http_error(502, "502 Bad Gateway"),
            "cand-4": (
                '{"tool_name": "system_info", "parameters": {"fields": ["memory"]}}'
            ),
        },
    )

    registry = ProviderRegistry()
    registry.register(provider)
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)

    tool_registry = ToolRegistry()
    tool_registry.register(SystemInfoTool())

    # planner initialized without explicit max_attempts — uses dynamic catalog exhaustion
    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)
    result = await planner.plan_tool_intent("how much RAM do I have?")

    assert result.status == PlannerStatus.TOOL_SELECTED
    assert result.tool_name == "system_info"
    assert provider.invoked_models == ["cand-1", "cand-2", "cand-3", "cand-4"]


# ---------------------------------------------------------------------------
# TEST E: All eligible candidates fail -> deterministic PLANNER_FAILURE, no infinite loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e_all_candidates_exhausted_deterministic_failure_no_infinite_loop() -> None:
    """TEST E: When all eligible candidates fail, loop terminates cleanly with PLANNER_FAILURE."""
    catalog = ModelCatalog()
    catalog.register(
        ModelDescriptor(
            model_id="model-a",
            provider_id="mock-prov",
            tier=ModelTier.FAST,
            priority=300,
            capabilities=ModelCapabilities(),
        )
    )
    catalog.register(
        ModelDescriptor(
            model_id="model-b",
            provider_id="mock-prov",
            tier=ModelTier.FAST,
            priority=200,
            capabilities=ModelCapabilities(),
        )
    )

    provider = MockFallbackProvider(
        provider_id="mock-prov",
        responses={
            "model-a": _make_http_error(402, "402 Payment Required"),
            "model-b": _make_http_error(503, "503 Service Unavailable"),
        },
    )

    registry = ProviderRegistry()
    registry.register(provider)
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)

    tool_registry = ToolRegistry()
    tool_registry.register(SystemInfoTool())

    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)
    result = await planner.plan_tool_intent("specs")

    assert result.status == PlannerStatus.PLANNER_FAILURE
    assert not result
    assert "provider_error" in (result.failure_reason or "")
    # Verified: exactly 2 attempts, stopped deterministically via CapabilityUnavailableError
    assert provider.invoked_models == ["model-a", "model-b"]


# ---------------------------------------------------------------------------
# TEST F: Request A causes MiniMax failure -> Request B starts with MiniMax again
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f_request_b_retries_minimax_after_request_a_failure() -> None:
    """TEST F: Model exclusion is request-local.

    MiniMax failure on Request A does NOT blacklist it for Request B.
    """
    catalog = ModelCatalog()
    catalog.register(
        ModelDescriptor(
            model_id="minimax/minimax-m3",
            provider_id="mock-prov",
            tier=ModelTier.FAST,
            priority=300,
            capabilities=ModelCapabilities(),
        )
    )
    catalog.register(
        ModelDescriptor(
            model_id="fallback-model",
            provider_id="mock-prov",
            tier=ModelTier.FAST,
            priority=200,
            capabilities=ModelCapabilities(),
        )
    )

    # MiniMax: 1st invocation fails with 402, 2nd invocation succeeds with valid JSON
    provider = MockFallbackProvider(
        provider_id="mock-prov",
        responses={
            "minimax/minimax-m3": [
                _make_http_error(402, "402 Payment Required"),
                '{"tool_name": "system_info", "parameters": {"fields": ["os"]}}',
            ],
            "fallback-model": (
                '{"tool_name": "system_info", "parameters": {"fields": ["os"]}}'
            ),
        },
    )

    registry = ProviderRegistry()
    registry.register(provider)
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)

    tool_registry = ToolRegistry()
    tool_registry.register(SystemInfoTool())

    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)

    # Request A: MiniMax fails -> fallback succeeds
    res_a = await planner.plan_tool_intent("what OS am I using?")
    assert res_a.status == PlannerStatus.TOOL_SELECTED
    assert provider.invoked_models == ["minimax/minimax-m3", "fallback-model"]

    # Request B: Same planner instance and gateway. MiniMax MUST be tried first again!
    res_b = await planner.plan_tool_intent("what OS am I using?")
    assert res_b.status == PlannerStatus.TOOL_SELECTED
    # Proves Request B started with MiniMax again and did not bypass or blacklist it
    assert provider.invoked_models == [
        "minimax/minimax-m3",
        "fallback-model",
        "minimax/minimax-m3",
    ]


# ---------------------------------------------------------------------------
# TEST G: Planner fallback does NOT modify ModelCatalog priorities
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g_planner_fallback_does_not_modify_catalog_priorities() -> None:
    """TEST G: Planner fallback never alters ModelCatalog priorities."""
    catalog = ModelCatalog()
    catalog.register(
        ModelDescriptor(
            model_id="minimax/minimax-m3",
            provider_id="mock-prov",
            tier=ModelTier.FAST,
            priority=300,
            capabilities=ModelCapabilities(),
        )
    )
    catalog.register(
        ModelDescriptor(
            model_id="fallback-model",
            provider_id="mock-prov",
            tier=ModelTier.FAST,
            priority=200,
            capabilities=ModelCapabilities(),
        )
    )

    assert catalog.get_model("minimax/minimax-m3").priority == 300
    assert catalog.get_model("fallback-model").priority == 200

    provider = MockFallbackProvider(
        provider_id="mock-prov",
        responses={
            "minimax/minimax-m3": _make_http_error(402, "402 Payment Required"),
            "fallback-model": (
                '{"tool_name": "system_info", "parameters": {"fields": ["cpu"]}}'
            ),
        },
    )

    registry = ProviderRegistry()
    registry.register(provider)
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)

    tool_registry = ToolRegistry()
    tool_registry.register(SystemInfoTool())

    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)
    await planner.plan_tool_intent("specs")

    # Invariant: priorities remain strictly identical
    assert catalog.get_model("minimax/minimax-m3").priority == 300
    assert catalog.get_model("fallback-model").priority == 200


# ---------------------------------------------------------------------------
# TEST H: Planner fallback does NOT modify normal HarnessRunner routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_h_planner_fallback_does_not_modify_normal_harness_routing() -> None:
    """TEST H: ToolIntentPlanner fallback state does NOT bleed into normal HarnessRunner routing."""
    catalog = ModelCatalog()
    catalog.register(
        ModelDescriptor(
            model_id="minimax/minimax-m3",
            provider_id="mock-prov",
            tier=ModelTier.FAST,
            priority=300,
            capabilities=ModelCapabilities(),
        )
    )
    catalog.register(
        ModelDescriptor(
            model_id="secondary-model",
            provider_id="mock-prov",
            tier=ModelTier.FAST,
            priority=200,
            capabilities=ModelCapabilities(),
        )
    )

    provider = MockFallbackProvider(
        provider_id="mock-prov",
        responses={
            "minimax/minimax-m3": [
                _make_http_error(402, "402 Payment Required"),  # For planner
                "Normal response from MiniMax",  # For normal harness
            ],
            "secondary-model": (
                '{"tool_name": "system_info", "parameters": {"fields": ["cpu"]}}'
            ),
        },
    )

    registry = ProviderRegistry()
    registry.register(provider)
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)

    tool_registry = ToolRegistry()
    tool_registry.register(SystemInfoTool())

    # 1. Run planner with MiniMax failure
    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)
    plan_res = await planner.plan_tool_intent("cpu specs")
    assert plan_res.status == PlannerStatus.TOOL_SELECTED
    assert provider.invoked_models == ["minimax/minimax-m3", "secondary-model"]

    # 2. Run normal HarnessRunner path on the same gateway/router
    runner = HarnessRunner(
        gateway=gateway,
        tool_registry=tool_registry,
        permission_gate=AllowAllPermissionGate(),
    )
    normal_plan = ExecutionPlan(
        task_id=uuid4(),
        correlation_id=uuid4(),
        steps=[
            PlanStep(
                step_type=StepType.MODEL_INVOCATION,
                model_intent=ModelStepIntent(
                    instruction="generate normal response",
                    execution_strategy=ExecutionStrategy(
                        capability_requirements=CapabilityRequirements(),
                        preferred_tier=ModelTier.FAST.value,
                        recovery_policy=RecoveryPolicy.REROUTE,
                        require_verification=False,
                    ),
                ),
            )
        ],
    )
    context = TaskContext(session_id="s1")
    task_res = await runner.run(normal_plan, context)

    # Invariant: HarnessRunner selects MiniMax first, not secondary-model!
    assert task_res.success is True
    assert task_res.output_text == "Normal response from MiniMax"
    assert provider.invoked_models[-1] == "minimax/minimax-m3"


# ---------------------------------------------------------------------------
# TEST I: Normal response fallback remains unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_i_normal_response_fallback_remains_unchanged() -> None:
    """TEST I: HarnessRunner normal response fallback remains completely intact."""
    catalog = ModelCatalog()
    catalog.register(
        ModelDescriptor(
            model_id="normal-model-1",
            provider_id="mock-prov",
            tier=ModelTier.FAST,
            priority=300,
            capabilities=ModelCapabilities(),
        )
    )
    catalog.register(
        ModelDescriptor(
            model_id="normal-model-2",
            provider_id="mock-prov",
            tier=ModelTier.FAST,
            priority=200,
            capabilities=ModelCapabilities(),
        )
    )

    provider = MockFallbackProvider(
        provider_id="mock-prov",
        responses={
            "normal-model-1": _make_http_error(500, "500 Internal Server Error"),
            "normal-model-2": "Recovered response from normal-model-2",
        },
    )

    registry = ProviderRegistry()
    registry.register(provider)
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)

    runner = HarnessRunner(
        gateway=gateway,
        permission_gate=AllowAllPermissionGate(),
    )

    plan = ExecutionPlan(
        task_id=uuid4(),
        correlation_id=uuid4(),
        steps=[
            PlanStep(
                step_type=StepType.MODEL_INVOCATION,
                model_intent=ModelStepIntent(
                    instruction="generate response",
                    execution_strategy=ExecutionStrategy(
                        capability_requirements=CapabilityRequirements(),
                        preferred_tier=ModelTier.FAST.value,
                        recovery_policy=RecoveryPolicy.REROUTE,
                        require_verification=False,
                    ),
                ),
            )
        ],
    )
    context = TaskContext(session_id="s1")
    result = await runner.run(plan, context)

    assert result.success is True
    assert result.output_text == "Recovered response from normal-model-2"
    assert result.runtime_info is not None
    assert result.runtime_info.was_rerouted is True
    assert result.runtime_info.failed_model == "normal-model-1"
    assert result.runtime_info.selected_model == "normal-model-2"
    assert provider.invoked_models == ["normal-model-1", "normal-model-2"]


# ---------------------------------------------------------------------------
# TEST J: Planner "none" result does NOT trigger unnecessary fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_j_planner_none_does_not_trigger_fallback() -> None:
    """TEST J: When planner returns tool_name='none', it returns immediately without fallback."""
    catalog = ModelCatalog()
    catalog.register(
        ModelDescriptor(
            model_id="minimax/minimax-m3",
            provider_id="mock-prov",
            tier=ModelTier.FAST,
            priority=300,
            capabilities=ModelCapabilities(),
        )
    )
    catalog.register(
        ModelDescriptor(
            model_id="fallback-model",
            provider_id="mock-prov",
            tier=ModelTier.FAST,
            priority=200,
            capabilities=ModelCapabilities(),
        )
    )

    provider = MockFallbackProvider(
        provider_id="mock-prov",
        responses={
            "minimax/minimax-m3": (
                '{"tool_name": "none", "parameters": {}, "requires_reasoning": false}'
            ),
            "fallback-model": (
                '{"tool_name": "system_info", "parameters": {"fields": ["cpu"]}}'
            ),
        },
    )

    registry = ProviderRegistry()
    registry.register(provider)
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)

    tool_registry = ToolRegistry()
    tool_registry.register(SystemInfoTool())

    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)
    result = await planner.plan_tool_intent("tell me a joke about computers")

    assert result.status == PlannerStatus.NO_TOOL
    assert not result
    assert result.tool_name is None
    # Must NOT have invoked fallback-model
    assert provider.invoked_models == ["minimax/minimax-m3"]


# ---------------------------------------------------------------------------
# TEST K: Malformed planner JSON causes request-local candidate exclusion and rerouting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_k_malformed_json_causes_request_local_exclusion_and_rerouting() -> None:
    """TEST K: Unparseable JSON excludes candidate locally for current request only."""
    catalog = ModelCatalog()
    catalog.register(
        ModelDescriptor(
            model_id="model-bad-json",
            provider_id="mock-prov",
            tier=ModelTier.FAST,
            priority=300,
            capabilities=ModelCapabilities(),
        )
    )
    catalog.register(
        ModelDescriptor(
            model_id="model-good-json",
            provider_id="mock-prov",
            tier=ModelTier.FAST,
            priority=200,
            capabilities=ModelCapabilities(),
        )
    )

    provider = MockFallbackProvider(
        provider_id="mock-prov",
        responses={
            "model-bad-json": [
                "Here is your result: {malformed json without closing",
                '{"tool_name": "system_info", "parameters": {"fields": ["os"]}}',
            ],
            "model-good-json": (
                '{"tool_name": "system_info", "parameters": {"fields": ["os"]}}'
            ),
        },
    )

    registry = ProviderRegistry()
    registry.register(provider)
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)

    tool_registry = ToolRegistry()
    tool_registry.register(SystemInfoTool())

    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)

    # Request A: model-bad-json returns bad JSON -> reroutes to model-good-json
    res_a = await planner.plan_tool_intent("what OS am I using?")
    assert res_a.status == PlannerStatus.TOOL_SELECTED
    assert provider.invoked_models == ["model-bad-json", "model-good-json"]

    # Request B: model-bad-json is retried again (not permanently blacklisted)
    res_b = await planner.plan_tool_intent("what OS am I using?")
    assert res_b.status == PlannerStatus.TOOL_SELECTED
    assert provider.invoked_models == [
        "model-bad-json",
        "model-good-json",
        "model-bad-json",
    ]


# ---------------------------------------------------------------------------
# TEST L: End-to-end: planner MiniMax fails 402 -> reroutes -> file_search/file_send succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_l_e2e_minimax_402_reroute_to_file_search_send_success(
    tmp_path: Path,
) -> None:
    """TEST L: End-to-end user request 'send me AHJIN-1 from downloads' with 402 on planner MiniMax.

    Proves:
    1. Planner MiniMax fails with 402.
    2. Next model selected by ModelRouter successfully outputs file_search intent.
    3. Orchestrator plans file_search and file_send steps.
    4. HarnessRunner executes tool pipeline to locate file.
    5. Deterministic successful file-send confirmation output intact.
    """
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    target_file = downloads / "AHJIN-1.txt"
    target_file.write_text("AHJIN 2.0 system specifications content")

    policy = SafePathPolicy(workspace_root=tmp_path, additional_roots=[downloads])
    search_tool = FileSearchTool(path_policy=policy)
    send_tool = FileSendTool(path_policy=policy)

    tool_registry = ToolRegistry()
    tool_registry.register(search_tool)
    tool_registry.register(send_tool)

    catalog = ModelCatalog()
    catalog.register(
        ModelDescriptor(
            model_id="minimax/minimax-m3",
            provider_id="mock-prov",
            tier=ModelTier.FAST,
            priority=300,
            capabilities=ModelCapabilities(),
        )
    )
    catalog.register(
        ModelDescriptor(
            model_id="nemotron-fallback",
            provider_id="mock-prov",
            tier=ModelTier.FAST,
            priority=200,
            capabilities=ModelCapabilities(),
        )
    )

    provider = MockFallbackProvider(
        provider_id="mock-prov",
        responses={
            "minimax/minimax-m3": _make_http_error(402, "402 Payment Required"),
            "nemotron-fallback": (
                '{"tool_name": "file_search", '
                '"parameters": {"query": "AHJIN-1", "path": "downloads"}, '
                '"requires_reasoning": false}'
            ),
        },
    )

    registry = ProviderRegistry()
    registry.register(provider)
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)

    planner = ToolIntentPlanner(
        gateway=gateway,
        tool_registry=tool_registry,
        planner_timeout=5.0,
    )
    orchestrator = BeruOrchestrator(tool_planner=planner, path_policy=policy)
    runner = HarnessRunner(
        gateway=gateway,
        tool_registry=tool_registry,
        permission_gate=AllowAllPermissionGate(),
    )

    req = TaskRequest(
        intent=UserIntent(primary_text="send me AHJIN-1 file from downloads"),
        context=TaskContext(session_id="test-e2e-402"),
    )

    plan = await orchestrator.plan(req)

    # Must have planned file_search -> file_send chain
    assert len(plan.steps) >= 2
    assert plan.steps[0].tool_intent is not None
    assert plan.steps[0].tool_intent.tool_name == "file_search"

    result = await runner.run(plan, req.context)

    # Invariant: ONE PROVIDER FAILURE != ENTIRE AHJIN TASK FAILURE
    assert result.success is True
    assert len(result.file_attachments) == 1
    assert result.file_attachments[0].name == "AHJIN-1.txt"
    assert "AHJIN-1.txt" in (result.output_text or "")


# ===========================================================================
# TOOL INTENT PLANNER — RESPONSE-START TIMEOUT REWORK TESTS (Tests 1 through 10)
# ===========================================================================


@pytest.mark.asyncio
async def test_planner_response_start_fast_model_succeeds() -> None:
    """TEST 1: Fast model starts and finishes well before timeout -> succeeds directly."""
    catalog = ModelCatalog()
    catalog.register(
        ModelDescriptor(
            model_id="fast-model",
            provider_id="stream-prov",
            tier=ModelTier.FAST,
            priority=300,
            capabilities=ModelCapabilities(),
        )
    )

    provider = MockStreamingFallbackProvider(
        provider_id="stream-prov",
        stream_schedules={
            "fast-model": [
                (0.01, '{"tool_name": "system_info", '),
                (0.01, '"parameters": {"fields": ["cpu"]}}'),
            ],
        },
    )

    registry = ProviderRegistry()
    registry.register(provider)
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)

    tool_registry = ToolRegistry()
    tool_registry.register(SystemInfoTool())

    planner = ToolIntentPlanner(
        gateway=gateway,
        tool_registry=tool_registry,
        planner_timeout=0.15,
    )

    result = await planner.plan_tool_intent("check my processor")

    assert result.status == PlannerStatus.TOOL_SELECTED
    assert result.tool_name == "system_info"
    assert result.selected_model == "fast-model"
    assert result.was_rerouted is False
    assert provider.invoked_models == ["fast-model"]
    assert provider.cancelled_models == []


@pytest.mark.asyncio
async def test_planner_response_start_slow_generation_succeeds_once_started() -> None:
    """TEST 2: Model starts responding before 15s (scaled to 0.15s), but completes after 15s.

    Under the old completion timeout, this would time out and reroute.
    Under the response-start timeout, the startup timeout is satisfied by the first chunk,
    and the model is allowed to finish naturally without any total deadline.
    """
    catalog = ModelCatalog()
    catalog.register(
        ModelDescriptor(
            model_id="slow-healthy-model",
            provider_id="stream-prov",
            tier=ModelTier.FAST,
            priority=300,
            capabilities=ModelCapabilities(),
        )
    )
    catalog.register(
        ModelDescriptor(
            model_id="fallback-model",
            provider_id="stream-prov",
            tier=ModelTier.FAST,
            priority=200,
            capabilities=ModelCapabilities(),
        )
    )

    # First chunk arrives at 0.04s (< 0.15s startup threshold).
    # Subsequent chunks take 0.15s + 0.10s (total elapsed ~0.29s > 0.15s).
    provider = MockStreamingFallbackProvider(
        provider_id="stream-prov",
        stream_schedules={
            "slow-healthy-model": [
                (0.04, '{"tool_name": "system_info", '),
                (0.15, '"parameters": {"fields": ["cpu"]'),
                (0.10, "}}"),
            ],
            "fallback-model": [
                (0.01, '{"tool_name": "system_info", "parameters": {"fields": ["cpu"]}}'),
            ],
        },
    )

    registry = ProviderRegistry()
    registry.register(provider)
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)

    tool_registry = ToolRegistry()
    tool_registry.register(SystemInfoTool())

    planner = ToolIntentPlanner(
        gateway=gateway,
        tool_registry=tool_registry,
        planner_timeout=0.15,
    )

    t0 = time.monotonic()
    result = await planner.plan_tool_intent("check my processor")
    total_duration = time.monotonic() - t0

    # Total duration exceeded 0.15s startup timeout, but SUCCESS without reroute
    assert total_duration >= 0.20
    assert result.status == PlannerStatus.TOOL_SELECTED
    assert result.tool_name == "system_info"
    assert result.selected_model == "slow-healthy-model"
    assert result.was_rerouted is False
    assert provider.invoked_models == ["slow-healthy-model"]
    assert "slow-healthy-model" not in provider.cancelled_models


@pytest.mark.asyncio
async def test_planner_response_start_explicit_scaled_10s_40s_scenario() -> None:
    """EXPLICIT TEST CASE (matching 10s response / 40s completion / 15s timeout):

    First response at 100ms (representing 10s < 15s):
    Completion at 400ms (representing 40s > 15s):
    Expected: SUCCESS, NO reroute, NO timeout at 150ms.

    And:
    No response by 150ms (representing 15s):
    Expected: TIMEOUT, CANCEL, REROUTE.
    """
    catalog = ModelCatalog()
    catalog.register(
        ModelDescriptor(
            model_id="model-10s-40s",
            provider_id="stream-prov",
            tier=ModelTier.FAST,
            priority=300,
            capabilities=ModelCapabilities(),
        )
    )

    # 100ms response start, 400ms total completion
    provider = MockStreamingFallbackProvider(
        provider_id="stream-prov",
        stream_schedules={
            "model-10s-40s": [
                (0.10, '{"tool_name": "system_info", '),
                (0.30, '"parameters": {"fields": ["cpu"]}}'),
            ],
        },
    )

    registry = ProviderRegistry()
    registry.register(provider)
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)

    tool_registry = ToolRegistry()
    tool_registry.register(SystemInfoTool())

    planner = ToolIntentPlanner(
        gateway=gateway,
        tool_registry=tool_registry,
        planner_timeout=0.15,  # represents 15s timeout
    )

    t0 = time.monotonic()
    result = await planner.plan_tool_intent("check my processor")
    elapsed = time.monotonic() - t0

    assert elapsed >= 0.35  # Completed at ~400ms > 150ms timeout
    assert result.status == PlannerStatus.TOOL_SELECTED
    assert result.selected_model == "model-10s-40s"
    assert result.was_rerouted is False
    assert provider.cancelled_models == []

    # Second half of test: no response by 150ms -> TIMEOUT, CANCEL, REROUTE
    catalog2 = ModelCatalog()
    catalog2.register(
        ModelDescriptor(
            model_id="stalled-model-15s",
            provider_id="stream-prov",
            tier=ModelTier.FAST,
            priority=300,
            capabilities=ModelCapabilities(),
        )
    )
    catalog2.register(
        ModelDescriptor(
            model_id="healthy-candidate",
            provider_id="stream-prov",
            tier=ModelTier.FAST,
            priority=200,
            capabilities=ModelCapabilities(),
        )
    )

    provider2 = MockStreamingFallbackProvider(
        provider_id="stream-prov",
        stream_schedules={
            "stalled-model-15s": [
                (0.50, '{"tool_name": "system_info"}'),  # stalls past 0.15s
            ],
            "healthy-candidate": [
                (0.02, '{"tool_name": "system_info", "parameters": {"fields": ["cpu"]}}'),
            ],
        },
    )
    registry2 = ProviderRegistry()
    registry2.register(provider2)
    router2 = ModelRouter(catalog=catalog2)
    gateway2 = ProviderGateway(registry=registry2, router=router2)

    planner2 = ToolIntentPlanner(
        gateway=gateway2,
        tool_registry=tool_registry,
        planner_timeout=0.15,
    )

    result2 = await planner2.plan_tool_intent("check my processor")

    assert result2.status == PlannerStatus.TOOL_SELECTED
    assert result2.selected_model == "healthy-candidate"
    assert result2.was_rerouted is True
    assert result2.first_failed_model == "stalled-model-15s"
    assert result2.first_failure_reason == "timeout"
    assert "stalled-model-15s" in provider2.cancelled_models


@pytest.mark.asyncio
async def test_planner_response_start_stalled_model_times_out_and_reroutes() -> None:
    """TEST 3: Model produces no response for timeout threshold -> timeout + reroute."""
    catalog = ModelCatalog()
    catalog.register(
        ModelDescriptor(
            model_id="stalled-model",
            provider_id="stream-prov",
            tier=ModelTier.FAST,
            priority=300,
            capabilities=ModelCapabilities(),
        )
    )
    catalog.register(
        ModelDescriptor(
            model_id="fallback-model",
            provider_id="stream-prov",
            tier=ModelTier.FAST,
            priority=200,
            capabilities=ModelCapabilities(),
        )
    )

    provider = MockStreamingFallbackProvider(
        provider_id="stream-prov",
        stream_schedules={
            "stalled-model": [
                (0.40, '{"tool_name": "system_info"}'),  # stalls beyond 0.08s
            ],
            "fallback-model": [
                (0.02, '{"tool_name": "system_info", "parameters": {"fields": ["cpu"]}}'),
            ],
        },
    )

    registry = ProviderRegistry()
    registry.register(provider)
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)

    tool_registry = ToolRegistry()
    tool_registry.register(SystemInfoTool())

    planner = ToolIntentPlanner(
        gateway=gateway,
        tool_registry=tool_registry,
        planner_timeout=0.08,
    )

    result = await planner.plan_tool_intent("check my processor")

    assert result.status == PlannerStatus.TOOL_SELECTED
    assert result.tool_name == "system_info"
    assert result.selected_model == "fallback-model"
    assert result.was_rerouted is True
    assert result.first_failed_model == "stalled-model"
    assert result.first_failure_reason == "timeout"
    assert "stalled-model" in provider.cancelled_models
    assert provider.invoked_models == ["stalled-model", "fallback-model"]


@pytest.mark.asyncio
async def test_planner_response_start_fast_http_error_immediate_reroute() -> None:
    """TEST 4: Fast HTTP failure such as HTTP 402 -> immediate reroute; do NOT wait 15s."""
    catalog = ModelCatalog()
    catalog.register(
        ModelDescriptor(
            model_id="minimax/minimax-m3",
            provider_id="stream-prov",
            tier=ModelTier.FAST,
            priority=300,
            capabilities=ModelCapabilities(),
        )
    )
    catalog.register(
        ModelDescriptor(
            model_id="candidate-2",
            provider_id="stream-prov",
            tier=ModelTier.FAST,
            priority=200,
            capabilities=ModelCapabilities(),
        )
    )

    provider = MockStreamingFallbackProvider(
        provider_id="stream-prov",
        stream_schedules={
            "minimax/minimax-m3": [
                (0.001, _make_http_error(402, "402 Payment Required")),
            ],
            "candidate-2": [
                (0.01, '{"tool_name": "system_info", "parameters": {"fields": ["cpu"]}}'),
            ],
        },
    )

    registry = ProviderRegistry()
    registry.register(provider)
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)

    tool_registry = ToolRegistry()
    tool_registry.register(SystemInfoTool())

    # Bounded planner timeout set to 15.0 seconds
    planner = ToolIntentPlanner(
        gateway=gateway,
        tool_registry=tool_registry,
        planner_timeout=15.0,
    )

    t0 = time.monotonic()
    result = await planner.plan_tool_intent("check processor")
    elapsed = time.monotonic() - t0

    # MUST reroute immediately in milliseconds, NOT waiting 15s
    assert elapsed < 1.0
    assert result.status == PlannerStatus.TOOL_SELECTED
    assert result.selected_model == "candidate-2"
    assert result.was_rerouted is True
    assert result.first_failed_model == "minimax/minimax-m3"
    assert result.first_failure_reason == "HTTP 402"


@pytest.mark.asyncio
async def test_planner_response_start_multiple_stalled_candidates_catalog_wide_fallback() -> None:
    """TEST 5: Multiple stalled candidates -> catalog-wide fallback continues."""
    catalog = ModelCatalog()
    catalog.register(
        ModelDescriptor(
            model_id="candidate-a",
            provider_id="stream-prov",
            tier=ModelTier.FAST,
            priority=300,
            capabilities=ModelCapabilities(),
        )
    )
    catalog.register(
        ModelDescriptor(
            model_id="candidate-b",
            provider_id="stream-prov",
            tier=ModelTier.FAST,
            priority=250,
            capabilities=ModelCapabilities(),
        )
    )
    catalog.register(
        ModelDescriptor(
            model_id="candidate-c",
            provider_id="stream-prov",
            tier=ModelTier.FAST,
            priority=200,
            capabilities=ModelCapabilities(),
        )
    )

    provider = MockStreamingFallbackProvider(
        provider_id="stream-prov",
        stream_schedules={
            "candidate-a": [(0.40, '{"tool_name": "system_info"}')],
            "candidate-b": [(0.40, '{"tool_name": "system_info"}')],
            "candidate-c": [
                (0.01, '{"tool_name": "system_info", "parameters": {"fields": ["cpu"]}}')
            ],
        },
    )

    registry = ProviderRegistry()
    registry.register(provider)
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)

    tool_registry = ToolRegistry()
    tool_registry.register(SystemInfoTool())

    planner = ToolIntentPlanner(
        gateway=gateway,
        tool_registry=tool_registry,
        planner_timeout=0.06,
    )

    result = await planner.plan_tool_intent("check processor")

    assert result.status == PlannerStatus.TOOL_SELECTED
    assert result.selected_model == "candidate-c"
    assert result.was_rerouted is True
    assert result.first_failed_model == "candidate-a"
    assert result.first_failure_reason == "timeout"
    assert provider.cancelled_models == ["candidate-a", "candidate-b"]
    assert provider.invoked_models == ["candidate-a", "candidate-b", "candidate-c"]


@pytest.mark.asyncio
async def test_planner_response_start_local_ollama_universal_rule() -> None:
    """TEST 6: Local Ollama model follows the exact same universal response-start timeout rule.

    1. Starts responding within threshold -> succeeds naturally even with slow completion.
    2. Stalls on response-start -> times out, gets cancelled, and falls back.
    """
    catalog = ModelCatalog()
    catalog.register(
        ModelDescriptor(
            model_id="ollama/qwen2.5-coder",
            provider_id="ollama-mock",
            tier=ModelTier.FAST,
            priority=300,
            capabilities=ModelCapabilities(),
        )
    )
    catalog.register(
        ModelDescriptor(
            model_id="cloud-fallback",
            provider_id="cloud-mock",
            tier=ModelTier.FAST,
            priority=200,
            capabilities=ModelCapabilities(),
        )
    )

    # Sub-case 1: Ollama starts at 0.03s, completes at 0.20s with 0.10s startup timeout
    provider_ollama = MockStreamingFallbackProvider(
        provider_id="ollama-mock",
        stream_schedules={
            "ollama/qwen2.5-coder": [
                (0.03, '{"tool_name": "system_info", '),
                (0.12, '"parameters": {"fields": ["cpu"]}}'),
            ],
        },
    )
    provider_cloud = MockStreamingFallbackProvider(
        provider_id="cloud-mock",
        stream_schedules={
            "cloud-fallback": [
                (0.01, '{"tool_name": "system_info", "parameters": {"fields": ["cpu"]}}'),
            ],
        },
    )

    registry = ProviderRegistry()
    registry.register(provider_ollama)
    registry.register(provider_cloud)
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)

    tool_registry = ToolRegistry()
    tool_registry.register(SystemInfoTool())

    planner = ToolIntentPlanner(
        gateway=gateway,
        tool_registry=tool_registry,
        planner_timeout=0.10,
    )

    result_slow_healthy = await planner.plan_tool_intent("check processor")
    assert result_slow_healthy.status == PlannerStatus.TOOL_SELECTED
    assert result_slow_healthy.selected_model == "ollama/qwen2.5-coder"
    assert result_slow_healthy.was_rerouted is False
    assert "ollama/qwen2.5-coder" not in provider_ollama.cancelled_models

    # Sub-case 2: Ollama stalls (> 0.30s with 0.06s timeout) -> times out and falls back
    provider_ollama_stalled = MockStreamingFallbackProvider(
        provider_id="ollama-mock",
        stream_schedules={
            "ollama/qwen2.5-coder": [(0.30, '{"tool_name": "system_info"}')],
        },
    )
    registry2 = ProviderRegistry()
    registry2.register(provider_ollama_stalled)
    registry2.register(provider_cloud)
    router2 = ModelRouter(catalog=catalog)
    gateway2 = ProviderGateway(registry=registry2, router=router2)

    planner2 = ToolIntentPlanner(
        gateway=gateway2,
        tool_registry=tool_registry,
        planner_timeout=0.06,
    )

    result_stalled = await planner2.plan_tool_intent("check processor")
    assert result_stalled.status == PlannerStatus.TOOL_SELECTED
    assert result_stalled.selected_model == "cloud-fallback"
    assert result_stalled.was_rerouted is True
    assert result_stalled.first_failed_model == "ollama/qwen2.5-coder"
    assert result_stalled.first_failure_reason == "timeout"
    assert "ollama/qwen2.5-coder" in provider_ollama_stalled.cancelled_models


@pytest.mark.asyncio
async def test_planner_response_start_request_local_exclusion_isolated() -> None:
    """TEST 7: Request-local exclusion remains intact.

    A timeout failure in Request A excludes the model ONLY for Request A.
    Request B begins fresh with the primary model candidate.
    """
    catalog = ModelCatalog()
    catalog.register(
        ModelDescriptor(
            model_id="primary-candidate",
            provider_id="stream-prov",
            tier=ModelTier.FAST,
            priority=300,
            capabilities=ModelCapabilities(),
        )
    )
    catalog.register(
        ModelDescriptor(
            model_id="secondary-candidate",
            provider_id="stream-prov",
            tier=ModelTier.FAST,
            priority=200,
            capabilities=ModelCapabilities(),
        )
    )

    # Request A will stall on primary-candidate; Request B will succeed on primary-candidate
    provider = MockStreamingFallbackProvider(
        provider_id="stream-prov",
        stream_schedules={
            "primary-candidate": [
                (0.30, '{"tool_name": "system_info"}'),  # stalled for req 1
            ],
            "secondary-candidate": [
                (0.01, '{"tool_name": "system_info", "parameters": {"fields": ["cpu"]}}'),
            ],
        },
    )

    registry = ProviderRegistry()
    registry.register(provider)
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)

    tool_registry = ToolRegistry()
    tool_registry.register(SystemInfoTool())

    planner = ToolIntentPlanner(
        gateway=gateway,
        tool_registry=tool_registry,
        planner_timeout=0.08,
    )

    # Request 1: primary stalls -> secondary succeeds
    res1 = await planner.plan_tool_intent("check processor")
    assert res1.status == PlannerStatus.TOOL_SELECTED
    assert res1.selected_model == "secondary-candidate"
    assert res1.was_rerouted is True

    # Now make primary healthy for Request 2
    provider.update_schedule(
        "primary-candidate",
        [(0.01, '{"tool_name": "system_info", "parameters": {"fields": ["cpu"]}}')],
    )

    # Request 2: must attempt primary-candidate first (request-local isolation)
    res2 = await planner.plan_tool_intent("check processor")
    assert res2.status == PlannerStatus.TOOL_SELECTED
    assert res2.selected_model == "primary-candidate"
    assert res2.was_rerouted is False


@pytest.mark.asyncio
async def test_planner_response_start_health_tracker_intact() -> None:
    """TEST 8: HealthTracker behavior remains intact across streaming invocation."""
    catalog = ModelCatalog()
    catalog.register(
        ModelDescriptor(
            model_id="healthy-stream-model",
            provider_id="stream-prov",
            tier=ModelTier.FAST,
            priority=300,
            capabilities=ModelCapabilities(),
        )
    )

    provider = MockStreamingFallbackProvider(
        provider_id="stream-prov",
        stream_schedules={
            "healthy-stream-model": [
                (0.02, '{"tool_name": "system_info", "parameters": {"fields": ["cpu"]}}'),
            ],
        },
    )

    registry = ProviderRegistry()
    registry.register(provider)
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)

    tool_registry = ToolRegistry()
    tool_registry.register(SystemInfoTool())

    planner = ToolIntentPlanner(
        gateway=gateway,
        tool_registry=tool_registry,
        planner_timeout=0.20,
    )

    result = await planner.plan_tool_intent("check processor")
    assert result.status == PlannerStatus.TOOL_SELECTED

    # Check health tracker recorded success
    state = router.health_tracker.get_state("healthy-stream-model")
    assert state.status == ModelHealthStatus.HEALTHY
    assert state.consecutive_failures == 0
    assert state.ema_latency_ms > 0


@pytest.mark.asyncio
async def test_planner_response_start_harness_behavior_unchanged(tmp_path: Path) -> None:
    """TEST 9 & 10: Harness timeout and streaming behavior remain completely untouched.

    Verifies that HarnessRunner executes normally with streaming delivery and tool integration,
    completely decoupled from ToolIntentPlanner's response-start timeout rework.
    """
    test_file = tmp_path / "report.txt"
    test_file.write_text("Quarterly System Report Content")

    policy = SafePathPolicy(workspace_root=tmp_path)
    search_tool = FileSearchTool(path_policy=policy)
    send_tool = FileSendTool(path_policy=policy)

    tool_registry = ToolRegistry()
    tool_registry.register(search_tool)
    tool_registry.register(send_tool)

    catalog = ModelCatalog()
    catalog.register(
        ModelDescriptor(
            model_id="stream-model",
            provider_id="stream-prov",
            tier=ModelTier.FAST,
            priority=300,
            capabilities=ModelCapabilities(),
        )
    )

    provider = MockStreamingFallbackProvider(
        provider_id="stream-prov",
        stream_schedules={
            "stream-model": [
                (
                    0.02,
                    '{"tool_name": "file_search", "parameters": {"query": "report.txt"}}',
                ),
            ],
        },
    )

    registry = ProviderRegistry()
    registry.register(provider)
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)

    planner = ToolIntentPlanner(
        gateway=gateway,
        tool_registry=tool_registry,
        planner_timeout=1.0,
    )
    orchestrator = BeruOrchestrator(tool_planner=planner, path_policy=policy)
    runner = HarnessRunner(
        gateway=gateway,
        tool_registry=tool_registry,
        permission_gate=AllowAllPermissionGate(),
    )

    req = TaskRequest(
        intent=UserIntent(primary_text="find and send me report.txt"),
        context=TaskContext(session_id="test-harness-unchanged"),
    )

    plan = await orchestrator.plan(req)
    assert len(plan.steps) >= 1
    assert plan.steps[0].tool_intent is not None
    assert plan.steps[0].tool_intent.tool_name == "file_search"

    result = await runner.run(plan, req.context)
    assert result.success is True
    assert len(result.file_attachments) == 1
    assert result.file_attachments[0].name == "report.txt"

