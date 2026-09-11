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
from pathlib import Path
from typing import Any
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

        behavior = self._responses.get(model, "{}")
        if isinstance(behavior, list):
            if len(behavior) > 1:
                item = behavior.pop(0)
            else:
                item = behavior[0]
            behavior = item

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
