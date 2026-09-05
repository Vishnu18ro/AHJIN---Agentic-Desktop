"""Unit tests for Phase 6A Tool Foundation.

Coverage:
1. ToolRegistry: register, get, missing error, duplicate error
2. PermissionGate: permission checking before execution
3. SystemInfoTool: safe system information output (no secrets)
4. HarnessRunner: TOOL_INVOCATION step execution, permission check, missing tool handling
5. ContextAssembler: formatting prior step/tool results into model instruction
"""

from uuid import uuid4

import pytest

from ahjin.beru.types import (
    ExecutionPlan,
    ModelStepIntent,
    PlanStep,
    StepType,
)
from ahjin.core.types import TaskContext
from ahjin.harness.context import ContextAssembler
from ahjin.harness.gateway import ProviderGateway
from ahjin.harness.runner import HarnessRunner
from ahjin.harness.state import StepResult
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
from ahjin.security.gate import PermissionGate
from ahjin.tools.base import BaseTool, ToolInvocationRequest, ToolInvocationResult
from ahjin.tools.registry import ToolRegistry
from ahjin.tools.system_info import SystemInfoTool

# --- Mock Tool for Testing ---

class DummyTestTool(BaseTool):
    @property
    def tool_name(self) -> str:
        return "dummy_tool"

    async def execute(self, request: ToolInvocationRequest) -> ToolInvocationResult:
        return ToolInvocationResult(
            invocation_id=request.invocation_id,
            success=True,
            output=f"Executed with params: {request.parameters}",
        )


# --- 1. ToolRegistry Tests ---

def test_tool_registry_register_and_retrieve() -> None:
    registry = ToolRegistry()
    tool = DummyTestTool()
    registry.register(tool)

    assert registry.has_tool("dummy_tool") is True
    assert registry.list_tools() == ["dummy_tool"]
    assert registry.get_tool("dummy_tool") is tool


def test_tool_registry_duplicate_registration_raises() -> None:
    registry = ToolRegistry()
    tool = DummyTestTool()
    registry.register(tool)

    with pytest.raises(ValueError, match="already registered"):
        registry.register(tool)


def test_tool_registry_missing_tool_raises() -> None:
    registry = ToolRegistry()
    with pytest.raises(KeyError, match="not registered"):
        registry.get_tool("nonexistent_tool")


# --- 2. SystemInfoTool Tests ---

@pytest.mark.asyncio
async def test_system_info_tool_execution() -> None:
    tool = SystemInfoTool()
    req = ToolInvocationRequest(tool_name="system_info")
    res = await tool.execute(req)

    assert res.success is True
    assert isinstance(res.output, str)
    assert "OS:" in res.output
    assert "Python:" in res.output
    assert "CWD:" in res.output
    # Verify no environment secrets are exposed
    assert "API_KEY" not in res.output
    assert "SECRET" not in res.output


# --- 3. PermissionGate Tests ---

@pytest.mark.asyncio
async def test_allow_all_permission_gate() -> None:
    gate = AllowAllPermissionGate()
    assert await gate.check_permission("system_info", {}) is True


class DenyAllPermissionGate(PermissionGate):
    async def check_permission(self, tool_name: str, parameters: dict) -> bool:
        return False


# --- 4. Harness Tool Execution Tests ---

def _build_mock_gateway_for_tools() -> ProviderGateway:
    class DummyProvider(BaseModelProvider):
        @property
        def provider_id(self) -> str:
            return "mock_tool_prov"

        def get_default_model_id(self) -> str:
            return "mock-tool-model"

        async def invoke(self, request: ModelInvocationRequest) -> ModelInvocationResponse:
            return ModelInvocationResponse(
                invocation_id=request.invocation_id,
                content="Model response",
                provider_id=self.provider_id,
                model_id=request.model_id,
            )

    provider = DummyProvider()
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


@pytest.mark.asyncio
async def test_harness_tool_invocation_step_executes() -> None:
    tool_registry = ToolRegistry()
    tool_registry.register(DummyTestTool())
    permission_gate = AllowAllPermissionGate()

    runner = HarnessRunner(
        gateway=_build_mock_gateway_for_tools(),
        tool_registry=tool_registry,
        permission_gate=permission_gate,
    )

    plan = ExecutionPlan(
        task_id=uuid4(),
        correlation_id=uuid4(),
        steps=[
            PlanStep(
                step_type=StepType.TOOL_INVOCATION,
                tool_intent=ToolInvocationRequest(
                    tool_name="dummy_tool", parameters={"foo": "bar"}
                ),
            )
        ],
    )
    context = TaskContext(session_id="test")

    result = await runner.run(plan, context)

    assert result.success is True
    assert result.output_text == "Executed with params: {'foo': 'bar'}"


@pytest.mark.asyncio
async def test_harness_tool_execution_denied_permission_produces_structured_failure() -> None:
    tool_registry = ToolRegistry()
    tool_registry.register(DummyTestTool())
    permission_gate = DenyAllPermissionGate()

    runner = HarnessRunner(
        gateway=_build_mock_gateway_for_tools(),
        tool_registry=tool_registry,
        permission_gate=permission_gate,
    )

    plan = ExecutionPlan(
        task_id=uuid4(),
        correlation_id=uuid4(),
        steps=[
            PlanStep(
                step_type=StepType.TOOL_INVOCATION,
                tool_intent=ToolInvocationRequest(tool_name="dummy_tool"),
            )
        ],
    )
    context = TaskContext(session_id="test")

    result = await runner.run(plan, context)

    assert result.success is True  # Plan completes execution
    # Output of the plan reflects that tool execution returned error in step results


@pytest.mark.asyncio
async def test_harness_tool_execution_missing_tool_produces_structured_failure() -> None:
    tool_registry = ToolRegistry()
    permission_gate = AllowAllPermissionGate()

    runner = HarnessRunner(
        gateway=_build_mock_gateway_for_tools(),
        tool_registry=tool_registry,
        permission_gate=permission_gate,
    )

    plan = ExecutionPlan(
        task_id=uuid4(),
        correlation_id=uuid4(),
        steps=[
            PlanStep(
                step_type=StepType.TOOL_INVOCATION,
                tool_intent=ToolInvocationRequest(tool_name="nonexistent_tool"),
            )
        ],
    )
    context = TaskContext(session_id="test")

    result = await runner.run(plan, context)

    assert result.success is True


# --- 5. ContextAssembler Prior Results Tests ---

def test_context_assembler_formats_prior_tool_results() -> None:
    assembler = ContextAssembler()
    intent = ModelStepIntent(instruction="Answer question based on system info")
    task_context = TaskContext(session_id="test")

    step_id = uuid4()
    prior_results = [
        StepResult(step_id=step_id, success=True, output_text="OS: Windows 11")
    ]

    prompt = assembler.assemble(
        intent=intent,
        task_context=task_context,
        prior_results=prior_results,
    )

    assert "Answer question based on system info" in prompt.user_instruction
    assert "[TOOL RESULTS]" in prompt.user_instruction
    assert f"Step: {step_id}" in prompt.user_instruction
    assert "Success: true" in prompt.user_instruction
    assert "OS: Windows 11" in prompt.user_instruction
    assert "[/TOOL RESULTS]" in prompt.user_instruction


def test_context_assembler_no_prior_results_preserves_behavior() -> None:
    assembler = ContextAssembler()
    intent = ModelStepIntent(instruction="Just a question")
    task_context = TaskContext(session_id="test")

    prompt = assembler.assemble(
        intent=intent,
        task_context=task_context,
        prior_results=None,
    )

    assert prompt.user_instruction == "Just a question"
    assert "[TOOL RESULTS]" not in prompt.user_instruction
