"""Unit tests for BERU Intelligent Tool Planning (Phase 6B)."""

import pytest

from ahjin.beru.orchestrator import BeruOrchestrator
from ahjin.beru.types import StepType
from ahjin.core.types import TaskContext, TaskRequest, UserIntent
from ahjin.models.types import ModelTier


def _make_request(prompt: str) -> TaskRequest:
    return TaskRequest(
        intent=UserIntent(primary_text=prompt),
        context=TaskContext(session_id="test-session"),
    )


@pytest.mark.asyncio
async def test_beru_plans_system_info_tool_for_os_query() -> None:
    orchestrator = BeruOrchestrator()
    request = _make_request("What operating system am I using?")
    plan = await orchestrator.plan(request)

    assert len(plan.steps) == 2
    step = plan.steps[0]
    assert step.step_type == StepType.TOOL_INVOCATION
    assert step.tool_intent is not None
    assert step.tool_intent.tool_name == "system_info"


@pytest.mark.asyncio
async def test_beru_plans_system_info_tool_for_python_version_query() -> None:
    orchestrator = BeruOrchestrator()
    request = _make_request("What Python version am I running?")
    plan = await orchestrator.plan(request)

    assert len(plan.steps) == 2
    step = plan.steps[0]
    assert step.step_type == StepType.TOOL_INVOCATION
    assert step.tool_intent is not None
    assert step.tool_intent.tool_name == "system_info"


@pytest.mark.asyncio
async def test_beru_plans_normal_question_as_model_invocation() -> None:
    orchestrator = BeruOrchestrator()
    request = _make_request("What is machine learning?")
    plan = await orchestrator.plan(request)

    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.step_type == StepType.MODEL_INVOCATION
    assert step.model_intent is not None
    assert step.model_intent.execution_strategy.preferred_tier == ModelTier.FAST.value


@pytest.mark.asyncio
async def test_beru_plans_reasoning_request_as_heavy_model_invocation() -> None:
    orchestrator = BeruOrchestrator()
    request = _make_request("Explain why the sky is blue step by step")
    plan = await orchestrator.plan(request)

    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.step_type == StepType.MODEL_INVOCATION
    assert step.model_intent is not None
    assert step.model_intent.capability_requirements.requires_reasoning is True
    assert step.model_intent.execution_strategy.preferred_tier == ModelTier.HEAVY.value


@pytest.mark.asyncio
async def test_beru_plans_coding_request_as_heavy_model_invocation() -> None:
    orchestrator = BeruOrchestrator()
    request = _make_request("Write a python script to parse JSON")
    plan = await orchestrator.plan(request)

    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.step_type == StepType.MODEL_INVOCATION
    assert step.model_intent is not None
    assert step.model_intent.capability_requirements.requires_code is True
    assert step.model_intent.execution_strategy.preferred_tier == ModelTier.HEAVY.value


@pytest.mark.asyncio
async def test_beru_plans_vision_request_as_heavy_model_invocation() -> None:
    orchestrator = BeruOrchestrator()
    request = _make_request("Look at this image and describe the diagram")
    plan = await orchestrator.plan(request)

    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.step_type == StepType.MODEL_INVOCATION
    assert step.model_intent is not None
    assert step.model_intent.capability_requirements.requires_vision is True
    assert step.model_intent.execution_strategy.preferred_tier == ModelTier.HEAVY.value
