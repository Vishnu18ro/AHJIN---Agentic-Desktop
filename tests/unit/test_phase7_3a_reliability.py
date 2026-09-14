"""Tests for Phase 7.3A Controlled Reliability Fixes (Tests A-H)."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ahjin.beru.orchestrator import BeruOrchestrator
from ahjin.beru.tool_planner import (
    DEFAULT_PLANNER_TIMEOUT_SECONDS,
    PlannerResult,
    PlannerStatus,
    ToolIntentPlanner,
)
from ahjin.beru.tools import detect_tool_intent, may_require_tool
from ahjin.beru.types import StepType
from ahjin.core.config import settings
from ahjin.core.types import TaskContext, TaskRequest, UserIntent
from ahjin.providers.openrouter import OpenRouterProvider
from ahjin.security.path_policy import SafePathPolicy
from ahjin.tools.base import ToolInvocationRequest
from ahjin.tools.file_search import _MAX_FILES_SCANNED, FileSearchTool
from ahjin.tools.system_info import SystemInfoTool


# ===========================================================================
# TEST A: file_search with query="resume-", path="downloads" reaches Resume-.pdf
#         without removing the 2000-file safety limit.
# ===========================================================================
@pytest.mark.asyncio
async def test_file_search_downloads_reaches_resume_within_limit():
    assert _MAX_FILES_SCANNED == 2000, "Safety limit _MAX_FILES_SCANNED must remain exactly 2000"

    policy = SafePathPolicy()
    tool = FileSearchTool(path_policy=policy)

    req = ToolInvocationRequest(
        tool_name="file_search",
        parameters={"query": "resume-", "path": "downloads"},
    )
    res = await tool.execute(req)
    assert res.success is True
    assert res.output is not None
    assert len(res.output.discovered_paths) >= 1
    found_filenames = [Path(p).name.lower() for p in res.output.discovered_paths]
    assert "resume-.pdf" in found_filenames


# ===========================================================================
# TEST B: file_search/file_send with explicit path preserves that directory
#         rather than converting it to "."
# ===========================================================================
@pytest.mark.asyncio
async def test_orchestrator_preserves_explicit_directory_path():
    target_dir = r"C:\Users\vishn\Downloads\Archived\X_tra\P_OGV_NA\Resume\\"
    orchestrator = BeruOrchestrator()

    mock_planner = MagicMock()
    mock_planner.plan_tool_intent = AsyncMock(
        return_value=PlannerResult(
            status=PlannerStatus.TOOL_SELECTED,
            tool_intent=ToolInvocationRequest(
                tool_name="file_send",
                parameters={"path": target_dir, "query": "resume-"},
            ),
        )
    )
    orchestrator.tool_planner = mock_planner

    req = TaskRequest(
        intent=UserIntent(primary_text=f"{target_dir} send me the resume- here"),
        context=TaskContext(session_id="test_b"),
    )

    plan = await orchestrator.plan(req)
    assert len(plan.steps) >= 2
    search_step = plan.steps[0]
    assert search_step.step_type == StepType.TOOL_INVOCATION
    assert search_step.tool_intent is not None
    assert search_step.tool_intent.tool_name == "file_search"

    # Explicit directory must be preserved — NOT replaced with "."
    assert search_step.tool_intent.parameters["path"] == target_dir
    assert search_step.tool_intent.parameters["query"] == "resume-"


# ===========================================================================
# TEST C: explicit path with trailing "\" must work without error
# ===========================================================================
@pytest.mark.asyncio
async def test_orchestrator_preserves_trailing_slash_path():
    policy = SafePathPolicy()
    orchestrator = BeruOrchestrator(path_policy=policy)

    path_with_slash = r"C:\Users\vishn\Downloads\Archived\X_tra\P_OGV_NA\Resume\\"
    mock_planner = MagicMock()
    mock_planner.plan_tool_intent = AsyncMock(
        return_value=PlannerResult(
            status=PlannerStatus.TOOL_SELECTED,
            tool_intent=ToolInvocationRequest(
                tool_name="file_send",
                parameters={"path": path_with_slash, "query": "resume-"},
            ),
        )
    )
    orchestrator.tool_planner = mock_planner

    req = TaskRequest(
        intent=UserIntent(primary_text=f"send {path_with_slash}"),
        context=TaskContext(session_id="test_c"),
    )

    plan = await orchestrator.plan(req)
    assert plan.steps[0].tool_intent is not None
    assert plan.steps[0].tool_intent.parameters["path"] == path_with_slash

    # Verify FileSearchTool also accepts this path cleanly
    tool = FileSearchTool(path_policy=policy)
    res = await tool.execute(plan.steps[0].tool_intent)
    assert res.success is True
    assert len(res.output.discovered_paths) == 1
    assert Path(res.output.discovered_paths[0]).name == "Resume-.pdf"


# ===========================================================================
# TEST D: unsafe/out-of-root explicit path must be safely rejected
# ===========================================================================
@pytest.mark.asyncio
async def test_orchestrator_rejects_unsafe_out_of_root_path():
    orchestrator = BeruOrchestrator()

    for unsafe_path in [r"C:\Windows\System32", r"C:\Program Files\App", "../../etc/passwd"]:
        mock_planner = MagicMock()
        mock_planner.plan_tool_intent = AsyncMock(
            return_value=PlannerResult(
                status=PlannerStatus.TOOL_SELECTED,
                tool_intent=ToolInvocationRequest(
                    tool_name="file_send",
                    parameters={"path": unsafe_path, "query": "target"},
                ),
            )
        )
        orchestrator.tool_planner = mock_planner

        req = TaskRequest(
            intent=UserIntent(primary_text=f"send {unsafe_path}"),
            context=TaskContext(session_id="test_d"),
        )

        plan = await orchestrator.plan(req)
        # Must produce an explicit access denied error step
        assert len(plan.steps) == 1
        step = plan.steps[0]
        assert step.step_type == StepType.TOOL_INVOCATION
        assert step.deterministic_output is not None
        assert "Access denied" in step.deterministic_output
        # Must NEVER have fallen back to "."
        assert (
            step.tool_intent is None
            or step.tool_intent.parameters.get("path") != "."
        )


# ===========================================================================
# TEST E: planner configuration must be exactly 30.0 seconds (Harness-aligned)
# ===========================================================================
def test_planner_configuration_is_exactly_30_seconds():
    assert settings.tool_planner_timeout == 30.0
    assert DEFAULT_PLANNER_TIMEOUT_SECONDS == 30.0

    planner = ToolIntentPlanner()
    assert planner.planner_timeout == 30.0


# ===========================================================================
# TEST F: OpenRouter must reuse the persistent HTTP client / connection pool
# ===========================================================================
@pytest.mark.asyncio
async def test_openrouter_reuses_persistent_client():
    provider = OpenRouterProvider(api_key="sk-or-test-dummy-key")

    client1 = provider._get_client()
    client2 = provider._get_client()
    assert client1 is client2, "OpenRouterProvider must reuse the identical persistent AsyncClient"
    assert not client1.is_closed

    await provider.aclose()
    assert client1.is_closed, "aclose() must close the persistent AsyncClient"
    assert provider._client is None


# ===========================================================================
# TEST G: normal chat behavior must remain unchanged
# ===========================================================================
@pytest.mark.asyncio
async def test_normal_chat_behavior_unchanged():
    # may_require_tool returns True by default (delegating to semantic planner)
    assert may_require_tool("hi") is True
    assert may_require_tool("tell me a joke") is True

    orchestrator = BeruOrchestrator()
    mock_planner = MagicMock()
    mock_planner.plan_tool_intent = AsyncMock(
        return_value=PlannerResult(status=PlannerStatus.NO_TOOL)
    )
    orchestrator.tool_planner = mock_planner

    req = TaskRequest(
        intent=UserIntent(primary_text="hi"),
        context=TaskContext(session_id="test_g"),
    )

    plan = await orchestrator.plan(req)
    assert len(plan.steps) == 1
    assert plan.steps[0].step_type == StepType.MODEL_INVOCATION
    assert plan.steps[0].model_intent is not None
    assert plan.steps[0].model_intent.instruction == "hi"


# ===========================================================================
# TEST H: system_info behavior must remain unchanged
# ===========================================================================
@pytest.mark.asyncio
async def test_system_info_behavior_unchanged():
    shortcut = detect_tool_intent("what OS am I using")
    assert shortcut is not None
    assert shortcut.tool_name == "system_info"

    tool = SystemInfoTool()
    req = ToolInvocationRequest(
        tool_name="system_info",
        parameters={"fields": ["os", "cpu", "memory"]},
    )
    res = await tool.execute(req)
    assert res.success is True
    assert res.output is not None
    assert "os" in str(res.output).lower()
