"""Unit tests for Phase 6B Hybrid LLM Tool Intent Planning."""

import pytest

from ahjin.beru.orchestrator import BeruOrchestrator
from ahjin.beru.tool_planner import ToolIntentPlanner
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
from ahjin.tools import FileReadTool, FileSearchTool, ToolRegistry
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

    assert req is None  # Must reject invented tool!


@pytest.mark.asyncio
async def test_llm_planner_rejects_unwhitelisted_parameters() -> None:
    json_resp = '{"tool_name": "system_info", "parameters": {"fields": ["passwords", "api_keys"]}}'
    gateway = _build_mock_planner_gateway(json_resp)
    tool_registry = ToolRegistry()
    tool_registry.register(SystemInfoTool())

    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)
    req = await planner.plan_tool_intent("Give me passwords and api keys")

    assert req is None  # Must reject un-whitelisted parameter fields!


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

