"""End-to-End integration test for Phase 6B Tool Execution Flow.

Flow:
TaskRequest ("What operating system am I using?")
    ↓
BeruOrchestrator (produces TOOL_INVOCATION -> MODEL_INVOCATION PlanSteps)
    ↓
HarnessRunner
    ↓
PermissionGate (AllowAllPermissionGate)
    ↓
ToolRegistry
    ↓
SystemInfoTool
    ↓
ExecutionState & TaskResult (passed to Model for final response)
"""

import pytest

from ahjin.beru.orchestrator import BeruOrchestrator
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
from ahjin.tools.registry import ToolRegistry
from ahjin.tools.system_info import SystemInfoTool


def _build_mock_gateway(response_text: str = "You are running Windows 11.") -> ProviderGateway:
    class MockE2EProvider(BaseModelProvider):
        @property
        def provider_id(self) -> str:
            return "mock_e2e_prov"

        def get_default_model_id(self) -> str:
            return "mock-e2e-model"

        async def invoke(self, request: ModelInvocationRequest) -> ModelInvocationResponse:
            return ModelInvocationResponse(
                invocation_id=request.invocation_id,
                content=response_text,
                provider_id=self.provider_id,
                model_id=request.model_id,
            )

    provider = MockE2EProvider()
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
async def test_e2e_system_info_tool_execution() -> None:
    # 1. Setup ToolRegistry and PermissionGate
    tool_registry = ToolRegistry()
    tool_registry.register(SystemInfoTool())

    permission_gate = AllowAllPermissionGate()
    gateway = _build_mock_gateway("You are running Windows 11.")

    # 2. Setup HarnessRunner with injected dependencies
    runner = HarnessRunner(
        gateway=gateway,
        tool_registry=tool_registry,
        permission_gate=permission_gate,
    )

    # 3. Create BERU Orchestrator and plan for system info request
    orchestrator = BeruOrchestrator()
    request = TaskRequest(
        intent=UserIntent(primary_text="What operating system am I using?"),
        context=TaskContext(session_id="e2e-session"),
    )

    plan = await orchestrator.plan(request)

    # Verify BERU produced TOOL_INVOCATION step followed by MODEL_INVOCATION step
    assert len(plan.steps) == 2
    assert plan.steps[0].step_type == StepType.TOOL_INVOCATION
    assert plan.steps[0].tool_intent is not None
    assert plan.steps[0].tool_intent.tool_name == "system_info"
    assert plan.steps[1].step_type == StepType.MODEL_INVOCATION

    # 4. Execute plan through HarnessRunner
    result = await runner.run(plan, request.context)

    # 5. Verify result
    assert result.success is True
    assert result.output_text == "You are running Windows 11."
