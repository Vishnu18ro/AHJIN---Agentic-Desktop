"""Unit tests for Phase 6B Hybrid LLM Tool Intent Planning."""

import pytest

from ahjin.beru.orchestrator import BeruOrchestrator
from ahjin.beru.tool_planner import PlannerStatus, ToolIntentPlanner
from ahjin.beru.types import StepType
from ahjin.core.types import TaskContext, TaskRequest, UserIntent
from ahjin.harness.gateway import ProviderGateway
from ahjin.harness.runner import HarnessRunner
from ahjin.models.catalog import ModelCatalog
from ahjin.models.router import ModelRouter
from ahjin.models.types import ModelCapabilities, ModelDescriptor, ModelTier
from ahjin.providers.base import BaseModelProvider
from ahjin.providers.registry import ProviderRegistry
from ahjin.providers.types import (
    ModelInvocationRequest,
    ModelInvocationResponse,
)
from ahjin.security.allow_all import AllowAllPermissionGate
from ahjin.tools import FileReadTool, FileSearchTool, FileSendTool, ToolRegistry
from ahjin.tools.system_info import SystemInfoTool


def _build_mock_planner_gateway(response_json: str) -> ProviderGateway:
    class MockPlannerProvider(BaseModelProvider):
        @property
        def provider_id(self) -> str:
            return "mock_planner"

        def get_default_model_id(self) -> str:
            return "mock-planner-model"

        async def invoke(self, request: ModelInvocationRequest) -> ModelInvocationResponse:
            return ModelInvocationResponse(
                invocation_id=request.invocation_id,
                content=response_json,
                provider_id=self.provider_id,
                model_id=request.model_id,
            )

    provider = MockPlannerProvider()
    catalog = ModelCatalog()
    catalog.register(
        ModelDescriptor(
            model_id=provider.get_default_model_id(),
            provider_id=provider.provider_id,
            tier=ModelTier.FAST,
            capabilities=ModelCapabilities(),
        )
    )
    registry = ProviderRegistry()
    registry.register(provider)
    router = ModelRouter(catalog=catalog)
    return ProviderGateway(registry=registry, router=router)


def _make_request(prompt: str) -> TaskRequest:
    return TaskRequest(
        intent=UserIntent(primary_text=prompt),
        context=TaskContext(session_id="test-session"),
    )


# --- 1. LLM Planner Structured Output Tests ---

@pytest.mark.asyncio
async def test_llm_planner_os_field_extraction() -> None:
    json_resp = '{"tool_name": "system_info", "parameters": {"fields": ["os"]}}'
    gateway = _build_mock_planner_gateway(json_resp)
    tool_registry = ToolRegistry()
    tool_registry.register(SystemInfoTool())

    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)
    req = await planner.plan_tool_intent("What operating system am I using?")

    assert req is not None
    assert req.tool_name == "system_info"
    assert req.parameters == {"fields": ["os"]}


@pytest.mark.asyncio
async def test_llm_planner_python_field_extraction() -> None:
    json_resp = '{"tool_name": "system_info", "parameters": {"fields": ["python"]}}'
    gateway = _build_mock_planner_gateway(json_resp)
    tool_registry = ToolRegistry()
    tool_registry.register(SystemInfoTool())

    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)
    req = await planner.plan_tool_intent("What Python version am I running?")

    assert req is not None
    assert req.tool_name == "system_info"
    assert req.parameters == {"fields": ["python"]}


@pytest.mark.asyncio
async def test_llm_planner_machine_field_extraction() -> None:
    json_resp = '{"tool_name": "system_info", "parameters": {"fields": ["machine"]}}'
    gateway = _build_mock_planner_gateway(json_resp)
    tool_registry = ToolRegistry()
    tool_registry.register(SystemInfoTool())

    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)
    req = await planner.plan_tool_intent("What machine am I on?")

    assert req is not None
    assert req.tool_name == "system_info"
    assert req.parameters == {"fields": ["machine"]}


@pytest.mark.asyncio
async def test_llm_planner_cpu_memory_fields_extraction() -> None:
    json_resp = '{"tool_name": "system_info", "parameters": {"fields": ["cpu", "memory"]}}'
    gateway = _build_mock_planner_gateway(json_resp)
    tool_registry = ToolRegistry()
    tool_registry.register(SystemInfoTool())

    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)
    req = await planner.plan_tool_intent("Tell me about my CPU and RAM.")

    assert req is not None
    assert req.tool_name == "system_info"
    assert req.parameters == {"fields": ["cpu", "memory"]}


@pytest.mark.asyncio
async def test_llm_planner_all_safe_field_extraction() -> None:
    json_resp = '{"tool_name": "system_info", "parameters": {"fields": ["all_safe"]}}'
    gateway = _build_mock_planner_gateway(json_resp)
    tool_registry = ToolRegistry()
    tool_registry.register(SystemInfoTool())

    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)
    req = await planner.plan_tool_intent("Give me everything you safely know about my system.")

    assert req is not None
    assert req.tool_name == "system_info"
    assert req.parameters == {"fields": ["all_safe"]}


# --- 2. Security Validation Tests ---

@pytest.mark.asyncio
async def test_llm_planner_rejects_unregistered_tool() -> None:
    json_resp = '{"tool_name": "delete_file", "parameters": {"path": "/etc/passwd"}}'
    gateway = _build_mock_planner_gateway(json_resp)
    tool_registry = ToolRegistry()
    tool_registry.register(SystemInfoTool())  # delete_file is NOT registered

    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)
    req = await planner.plan_tool_intent("Delete my system files")

    assert not req  # Must reject invented tool!
    assert req.status == PlannerStatus.PLANNER_FAILURE


@pytest.mark.asyncio
async def test_llm_planner_rejects_unwhitelisted_parameters() -> None:
    json_resp = '{"tool_name": "system_info", "parameters": {"fields": ["passwords", "api_keys"]}}'
    gateway = _build_mock_planner_gateway(json_resp)
    tool_registry = ToolRegistry()
    tool_registry.register(SystemInfoTool())

    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)
    req = await planner.plan_tool_intent("Give me passwords and api keys")

    assert not req  # Must reject un-whitelisted parameter fields!
    assert req.status == PlannerStatus.PLANNER_FAILURE


# --- 3. Hybrid Deterministic Fallback Tests ---

@pytest.mark.asyncio
async def test_deterministic_fallback_when_llm_planner_unavailable() -> None:
    # Orchestrator with NO LLM planner
    orchestrator = BeruOrchestrator(tool_planner=None)
    request = _make_request("What OS am I using?")
    plan = await orchestrator.plan(request)

    assert len(plan.steps) == 2
    assert plan.steps[0].step_type == StepType.TOOL_INVOCATION
    assert plan.steps[0].tool_intent is not None
    assert plan.steps[0].tool_intent.tool_name == "system_info"


# --- 4. Tool -> Observation -> Model End-to-End Test ---

@pytest.mark.asyncio
async def test_e2e_tool_observation_reaches_subsequent_model() -> None:
    json_resp = '{"tool_name": "system_info", "parameters": {"fields": ["os"]}}'
    gateway = _build_mock_planner_gateway(json_resp)
    tool_registry = ToolRegistry()
    tool_registry.register(SystemInfoTool())
    permission_gate = AllowAllPermissionGate()

    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)
    orchestrator = BeruOrchestrator(tool_planner=planner)
    runner = HarnessRunner(
        gateway=gateway,
        tool_registry=tool_registry,
        permission_gate=permission_gate,
    )

    request = _make_request("What OS am I using?")
    plan = await orchestrator.plan(request)

    # Verify BERU created 2 steps: TOOL_INVOCATION -> MODEL_INVOCATION
    assert len(plan.steps) == 2
    assert plan.steps[0].step_type == StepType.TOOL_INVOCATION
    assert plan.steps[1].step_type == StepType.MODEL_INVOCATION

    # Execute plan through Harness
    result = await runner.run(plan, request.context)

    assert result.success is True
    assert result.output_text is not None


# --- 5. File Intelligence Tool Intent Planning Tests ---

@pytest.mark.asyncio
async def test_llm_planner_file_search_extraction() -> None:
    json_resp = '{"tool_name": "file_search", "parameters": {"query": "ModelRouter"}}'
    gateway = _build_mock_planner_gateway(json_resp)
    tool_registry = ToolRegistry()
    tool_registry.register(SystemInfoTool())
    tool_registry.register(FileSearchTool())

    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)
    req = await planner.plan_tool_intent("Find the file containing ModelRouter")

    assert req is not None
    assert req.tool_name == "file_search"
    assert req.parameters == {"query": "ModelRouter"}


@pytest.mark.asyncio
async def test_llm_planner_file_read_extraction() -> None:
    json_resp = '{"tool_name": "file_read", "parameters": {"path": "src/ahjin/runner.py"}}'
    gateway = _build_mock_planner_gateway(json_resp)
    tool_registry = ToolRegistry()
    tool_registry.register(SystemInfoTool())
    tool_registry.register(FileReadTool())

    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)
    req = await planner.plan_tool_intent("Read src/ahjin/runner.py")

    assert req is not None
    assert req.tool_name == "file_read"
    assert req.parameters == {"path": "src/ahjin/runner.py"}


@pytest.mark.asyncio
async def test_llm_planner_pc_file_search_extraction() -> None:
    json_resp = '{"tool_name": "file_search", "parameters": {"query": "resume", "path": "pc"}}'
    gateway = _build_mock_planner_gateway(json_resp)
    tool_registry = ToolRegistry()
    tool_registry.register(SystemInfoTool())
    tool_registry.register(FileSearchTool())

    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)
    req = await planner.plan_tool_intent("Find my resume on my PC")

    assert req is not None
    assert req.tool_name == "file_search"
    assert req.parameters == {"query": "resume", "path": "pc"}


@pytest.mark.asyncio
async def test_llm_planner_nested_discovery_extraction() -> None:
    json_resp = (
        '{"tool_name": "file_search", "parameters": {'
        '"query": "resume", "path": "downloads/archived", '
        '"file_extensions": [".pdf"], "search_mode": "discovery"}}'
    )
    gateway = _build_mock_planner_gateway(json_resp)
    tool_registry = ToolRegistry()
    tool_registry.register(SystemInfoTool())
    tool_registry.register(FileSearchTool())

    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)
    req = await planner.plan_tool_intent(
        "Find resume pdf inside Downloads Archived"
    )

    assert req is not None
    assert req.tool_name == "file_search"
    assert req.parameters["query"] == "resume"
    assert req.parameters["path"] == "downloads/archived"
    assert req.parameters["file_extensions"] == [".pdf"]


# --- 6. Tool Screening & Timeout Tests ---

def test_may_require_tool_screening() -> None:
    from ahjin.beru.tools import may_require_tool

    # Phase 7: may_require_tool() is a conservative pass-through gate.
    # It returns True for ALL requests — including conversational ones.
    # The ToolIntentPlanner is the semantic decision point, NOT this gate.
    # Conversational requests reach the planner; the planner returns None.
    assert may_require_tool("hi") is True
    assert may_require_tool("hello") is True
    assert may_require_tool("what is machine learning?") is True
    assert may_require_tool("explain transformers") is True

    # Tool-potential requests also return True (unchanged from Phase 6)
    assert may_require_tool("what OS am I using?") is True
    assert may_require_tool("find my resume") is True
    assert may_require_tool("send my resume") is True
    assert may_require_tool("search the web for NVIDIA") is True
    assert may_require_tool("open WhatsApp") is True
    assert may_require_tool("read page 3 of my resume") is True


@pytest.mark.asyncio
async def test_tool_planner_times_out_gracefully() -> None:
    import asyncio

    class SlowProvider(BaseModelProvider):
        @property
        def provider_id(self) -> str:
            return "slow_provider"

        def get_default_model_id(self) -> str:
            return "slow-model"

        async def invoke(self, request: ModelInvocationRequest) -> ModelInvocationResponse:
            await asyncio.sleep(2.0)
            return ModelInvocationResponse(
                invocation_id=request.invocation_id,
                content='{"tool_name": "system_info", "parameters": {"fields": ["os"]}}',
                provider_id=self.provider_id,
                model_id=request.model_id,
            )

    provider = SlowProvider()
    catalog = ModelCatalog()
    catalog.register(
        ModelDescriptor(
            model_id=provider.get_default_model_id(),
            provider_id=provider.provider_id,
            tier=ModelTier.FAST,
            capabilities=ModelCapabilities(),
        )
    )
    registry = ProviderRegistry()
    registry.register(provider)
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)
    tool_registry = ToolRegistry()
    tool_registry.register(SystemInfoTool())

    # Set planner_timeout to 0.1s so it times out quickly
    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry, planner_timeout=0.1)
    req = await planner.plan_tool_intent("What operating system am I using?")

    # Timeout must return PLANNER_FAILURE gracefully without throwing
    assert not req
    assert req.status == PlannerStatus.PLANNER_FAILURE


# --- 7. File Search Chaining and Deterministic Fallback Tests ---

@pytest.mark.asyncio
async def test_beru_plan_file_search_primary_and_read() -> None:
    """find my resume and summarize it -> file_search -> file_read -> model."""
    json_resp = '{"tool_name": "file_search", "parameters": {"query": "resume", "path": "."}}'
    gateway = _build_mock_planner_gateway(json_resp)
    tool_registry = ToolRegistry()
    tool_registry.register(FileSearchTool())
    tool_registry.register(FileReadTool())
    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)
    orchestrator = BeruOrchestrator(tool_planner=planner)

    request = _make_request("find my resume and summarize it")
    plan = await orchestrator.plan(request)

    assert len(plan.steps) == 3
    assert plan.steps[0].step_type == StepType.TOOL_INVOCATION
    assert plan.steps[0].tool_intent is not None
    assert plan.steps[0].tool_intent.tool_name == "file_search"
    assert plan.steps[1].step_type == StepType.TOOL_INVOCATION
    assert plan.steps[1].tool_intent is not None
    assert plan.steps[1].tool_intent.tool_name == "file_read"
    assert plan.steps[2].step_type == StepType.MODEL_INVOCATION


@pytest.mark.asyncio
async def test_beru_plan_file_search_primary_and_send() -> None:
    """find my resume and send me the file -> file_search -> file_send -> model."""
    json_resp = '{"tool_name": "file_search", "parameters": {"query": "resume", "path": "."}}'
    gateway = _build_mock_planner_gateway(json_resp)
    tool_registry = ToolRegistry()
    tool_registry.register(FileSearchTool())
    tool_registry.register(FileSendTool())
    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)
    orchestrator = BeruOrchestrator(tool_planner=planner)

    request = _make_request("find my resume and send me the file")
    plan = await orchestrator.plan(request)

    assert len(plan.steps) == 3
    assert plan.steps[0].step_type == StepType.TOOL_INVOCATION
    assert plan.steps[0].tool_intent is not None
    assert plan.steps[0].tool_intent.tool_name == "file_search"
    assert plan.steps[1].step_type == StepType.TOOL_INVOCATION
    assert plan.steps[1].tool_intent is not None
    assert plan.steps[1].tool_intent.tool_name == "file_send"
    assert plan.steps[2].step_type == StepType.MODEL_INVOCATION


@pytest.mark.asyncio
async def test_beru_plan_file_search_read_and_send() -> None:
    """find my resume, summarize it, and send me the file.
    Expected: file_search -> file_read -> file_send -> model.
    """
    json_resp = '{"tool_name": "file_search", "parameters": {"query": "resume", "path": "."}}'
    gateway = _build_mock_planner_gateway(json_resp)
    tool_registry = ToolRegistry()
    tool_registry.register(FileSearchTool())
    tool_registry.register(FileReadTool())
    tool_registry.register(FileSendTool())
    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)
    orchestrator = BeruOrchestrator(tool_planner=planner)

    request = _make_request("find my resume, summarize it, and send me the file")
    plan = await orchestrator.plan(request)

    assert len(plan.steps) == 4
    assert plan.steps[0].step_type == StepType.TOOL_INVOCATION
    assert plan.steps[0].tool_intent is not None
    assert plan.steps[0].tool_intent.tool_name == "file_search"
    assert plan.steps[1].step_type == StepType.TOOL_INVOCATION
    assert plan.steps[1].tool_intent is not None
    assert plan.steps[1].tool_intent.tool_name == "file_read"
    assert plan.steps[2].step_type == StepType.TOOL_INVOCATION
    assert plan.steps[2].tool_intent is not None
    assert plan.steps[2].tool_intent.tool_name == "file_send"
    assert plan.steps[3].step_type == StepType.MODEL_INVOCATION


def test_deterministic_file_search_fallback() -> None:
    from ahjin.beru.tools import detect_tool_intent

    req = detect_tool_intent("find my resume")
    assert req is not None
    assert req.tool_name == "file_search"
    assert req.parameters["query"] == "resume"
    assert req.parameters["path"] == "."


def test_deterministic_file_search_with_folder() -> None:
    from ahjin.beru.tools import detect_tool_intent

    req = detect_tool_intent("find my resume inside the archived folder")
    assert req is not None
    assert req.tool_name == "file_search"
    assert req.parameters["query"] == "resume"
    assert req.parameters["path"] == "downloads/archived"


def test_deterministic_file_search_complex_phrase() -> None:
    from ahjin.beru.tools import detect_tool_intent

    req = detect_tool_intent("find my resume and summarize it")
    assert req is not None
    assert req.tool_name == "file_search"
    assert req.parameters["query"] == "resume"

    req2 = detect_tool_intent("where is my contract on desktop")
    assert req2 is not None
    assert req2.tool_name == "file_search"
    assert req2.parameters["query"] == "contract"
    assert req2.parameters["path"] == "desktop"


# ---------------------------------------------------------------------------
# Phase 2 Regression Tests: Deterministic Bypass & Planner Invocation Control
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_deterministic_os_query_bypasses_planner() -> None:
    """'what OS am I using?' must hit detect_tool_intent() and NEVER invoke the LLM planner.

    Phase 2 latency optimization: once detect_tool_intent() returns system_info,
    the orchestrator must short-circuit and not call ToolIntentPlanner.plan_tool_intent().
    """
    # Build a planner that records whether it was invoked
    planner_invoked = []

    class TrackingPlannerProvider(BaseModelProvider):
        @property
        def provider_id(self) -> str:
            return "tracking_planner"

        def get_default_model_id(self) -> str:
            return "tracking-model"

        async def invoke(self, request: ModelInvocationRequest) -> ModelInvocationResponse:
            planner_invoked.append(True)
            return ModelInvocationResponse(
                invocation_id=request.invocation_id,
                content='{"tool_name": "system_info", "parameters": {"fields": ["os"]}}',
                provider_id=self.provider_id,
                model_id=request.model_id,
            )

    provider = TrackingPlannerProvider()
    catalog = ModelCatalog()
    catalog.register(
        ModelDescriptor(
            model_id=provider.get_default_model_id(),
            provider_id=provider.provider_id,
            tier=ModelTier.FAST,
            capabilities=ModelCapabilities(),
        )
    )
    registry = ProviderRegistry()
    registry.register(provider)
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)

    tool_registry = ToolRegistry()
    tool_registry.register(SystemInfoTool())

    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)
    orchestrator = BeruOrchestrator(tool_planner=planner)

    request = _make_request("what OS am I using?")
    plan = await orchestrator.plan(request)

    # Planner must NOT have been invoked (deterministic bypass)
    assert planner_invoked == [], (
        "LLM ToolIntentPlanner was incorrectly invoked for a deterministically-matchable "
        "OS query. Phase 2 requires detect_tool_intent() to short-circuit the planner."
    )

    # Plan must still correctly call system_info tool
    assert len(plan.steps) == 2
    assert plan.steps[0].step_type == StepType.TOOL_INVOCATION
    assert plan.steps[0].tool_intent is not None
    assert plan.steps[0].tool_intent.tool_name == "system_info"
    assert plan.steps[1].step_type == StepType.MODEL_INVOCATION


@pytest.mark.asyncio
async def test_ambiguous_query_still_invokes_planner() -> None:
    """Ambiguous tool-potential requests (not matched by detect_tool_intent) must
    still invoke the LLM planner when may_require_tool() returns True.

    This verifies that the Phase 2 deterministic bypass does NOT accidentally
    disable the LLM planner for genuinely ambiguous inputs.
    """
    planner_invoked = []

    class TrackingPlannerProvider(BaseModelProvider):
        @property
        def provider_id(self) -> str:
            return "tracking_planner2"

        def get_default_model_id(self) -> str:
            return "tracking-model-2"

        async def invoke(self, request: ModelInvocationRequest) -> ModelInvocationResponse:
            planner_invoked.append(True)
            # Return a valid file_search response
            content = (
                '{"tool_name": "file_search", '
                '"parameters": {"query": "contract", "path": "documents"}}'
            )
            return ModelInvocationResponse(
                invocation_id=request.invocation_id,
                content=content,
                provider_id=self.provider_id,
                model_id=request.model_id,
            )

    provider = TrackingPlannerProvider()
    catalog = ModelCatalog()
    catalog.register(
        ModelDescriptor(
            model_id=provider.get_default_model_id(),
            provider_id=provider.provider_id,
            tier=ModelTier.FAST,
            capabilities=ModelCapabilities(),
        )
    )
    registry = ProviderRegistry()
    registry.register(provider)
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)

    tool_registry = ToolRegistry()
    tool_registry.register(FileSearchTool())

    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)
    orchestrator = BeruOrchestrator(tool_planner=planner)

    # "I need my contract document" does not match any deterministic prefix pattern
    # but has tool-potential keywords ("document"), so planner SHOULD be invoked.
    request = _make_request("I need my contract document")
    plan = await orchestrator.plan(request)

    # Planner MUST have been invoked (ambiguous = needs LLM)
    assert planner_invoked, (
        "LLM ToolIntentPlanner was NOT invoked for an ambiguous tool-potential query. "
        "Phase 2 changes must still route ambiguous requests to the planner."
    )
    # Plan should contain a file_search tool step (from planner response)
    tool_steps = [s for s in plan.steps if s.step_type == StepType.TOOL_INVOCATION]
    assert len(tool_steps) >= 1
    assert tool_steps[0].tool_intent is not None
    assert tool_steps[0].tool_intent.tool_name == "file_search"


@pytest.mark.asyncio
async def test_hi_uses_exactly_one_model_invocation_zero_planner() -> None:
    """'HI' must: skip LLM planner, produce exactly 1 model step, zero tool steps.

    This is success criterion [1] from Phase 2: 'HI' uses exactly ONE runtime
    LLM invocation (the response model call). The tool planner must NOT be invoked.
    """
    planner_invoked = []

    class TrackingPlannerProvider(BaseModelProvider):
        @property
        def provider_id(self) -> str:
            return "tracking_hi_planner"

        def get_default_model_id(self) -> str:
            return "tracking-hi-model"

        async def invoke(self, request: ModelInvocationRequest) -> ModelInvocationResponse:
            planner_invoked.append(True)
            return ModelInvocationResponse(
                invocation_id=request.invocation_id,
                content='{"tool_name": "none", "parameters": {}, "requires_reasoning": false}',
                provider_id=self.provider_id,
                model_id=request.model_id,
            )

    provider = TrackingPlannerProvider()
    catalog = ModelCatalog()
    catalog.register(
        ModelDescriptor(
            model_id=provider.get_default_model_id(),
            provider_id=provider.provider_id,
            tier=ModelTier.FAST,
            capabilities=ModelCapabilities(),
        )
    )
    registry = ProviderRegistry()
    registry.register(provider)
    router = ModelRouter(catalog=catalog)
    gateway = ProviderGateway(registry=registry, router=router)

    tool_registry = ToolRegistry()
    tool_registry.register(SystemInfoTool())

    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)
    orchestrator = BeruOrchestrator(tool_planner=planner)

    request = _make_request("HI")
    plan = await orchestrator.plan(request)

    # Phase 7: the planner now runs on ALL requests (conservative gate).
    # For 'HI', the planner returns None (no tool) — planner_invoked will be [True].
    # What matters is: zero tool steps, exactly 1 model step.
    # The planner correctly identifies 'HI' as non-tool and returns None.
    # (planner_invoked count verification intentionally removed per Phase 7 architecture)

    # Zero tool steps, exactly 1 model step
    tool_steps = [s for s in plan.steps if s.step_type == StepType.TOOL_INVOCATION]
    model_steps = [s for s in plan.steps if s.step_type == StepType.MODEL_INVOCATION]
    assert len(tool_steps) == 0, f"Expected 0 tool steps for 'HI', got {len(tool_steps)}"
    assert len(model_steps) == 1, f"Expected 1 model step for 'HI', got {len(model_steps)}"
