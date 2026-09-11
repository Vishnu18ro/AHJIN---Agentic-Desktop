"""Unit tests for Phase 6.X — Semantic Tool Intent & Authoritative Result Grounding."""

from pathlib import Path

import pytest

from ahjin.beru.orchestrator import BeruOrchestrator
from ahjin.beru.tool_planner import ToolIntentPlanner
from ahjin.beru.tools import extract_windows_path, may_require_tool
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
from ahjin.security.path_policy import SafePathPolicy
from ahjin.tools import FileReadTool, FileSearchTool, FileSendTool, ToolRegistry
from ahjin.tools.base import ToolInvocationRequest


class _MockTrackingProvider(BaseModelProvider):
    """Mock provider that tracks invocations and returns configured responses."""

    def __init__(self, response_text: str = "Mock response") -> None:
        self.response_text = response_text
        self.invocation_count = 0
        self.last_prompt = None

    @property
    def provider_id(self) -> str:
        return "mock_tracking"

    def get_default_model_id(self) -> str:
        return "mock-tracking-model"

    async def invoke(self, request: ModelInvocationRequest) -> ModelInvocationResponse:
        self.invocation_count += 1
        self.last_prompt = request.prompt
        return ModelInvocationResponse(
            invocation_id=request.invocation_id,
            content=self.response_text,
            provider_id=self.provider_id,
            model_id=request.model_id,
        )


def _build_gateway_with_provider(provider: BaseModelProvider) -> ProviderGateway:
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
        context=TaskContext(session_id="test-phase6x"),
    )


# --- 1. Path Awareness & Screening Tests ---


def test_may_require_tool_windows_path() -> None:
    """Windows absolute paths must trigger may_require_tool screening."""
    assert may_require_tool(r"C:\Users\vishn\Downloads\Resume\ send me the resume") is True
    assert may_require_tool(r"C:\Users\vishn\Downloads\Resume\Resume-.pdf send pdf") is True
    assert may_require_tool(r"C:\Users\vishn\test.dat view") is True


def test_may_require_tool_normal_chat() -> None:
    """Phase 7: may_require_tool() is a conservative pass-through gate.
    It returns True for ALL requests — the ToolIntentPlanner makes the semantic
    judgment, not the gate. Conversational requests reach the planner and the
    planner returns None (no tool invoked).
    """
    assert may_require_tool("Hello there!") is True
    assert may_require_tool("How are you doing?") is True
    assert may_require_tool("Write a python function to reverse a string") is True


def test_extract_windows_path() -> None:
    """Path extractor distinguishes directories from exact files."""
    dir_res = extract_windows_path(r"C:\Users\vishn\Downloads\Resume\ send me the resume")
    assert dir_res is not None
    path_str, is_dir = dir_res
    assert "Resume" in path_str
    assert is_dir is True

    file_res = extract_windows_path(r"C:\Users\vishn\Downloads\Resume\Resume-.pdf send pdf")
    assert file_res is not None
    path_str, is_dir = file_res
    assert path_str.endswith("Resume-.pdf")
    assert is_dir is False


# --- 2. Natural-Language Tool Intent (via ToolIntentPlanner) ---


@pytest.mark.asyncio
async def test_tool_planner_nl_send_resume() -> None:
    """send me my resume -> plans file_send with query='resume'."""
    json_resp = (
        '{"tool_name": "file_send", "parameters": {"path": ".", "query": "resume"}, '
        '"requires_reasoning": false}'
    )
    provider = _MockTrackingProvider(response_text=json_resp)
    gateway = _build_gateway_with_provider(provider)
    tool_registry = ToolRegistry()
    tool_registry.register(FileSendTool())

    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)
    req = await planner.plan_tool_intent("send me my resume")

    assert req is not None
    assert req.tool_name == "file_send"
    assert req.parameters["query"] == "resume"
    assert req.requires_reasoning is False


@pytest.mark.asyncio
async def test_tool_planner_nl_read_resume() -> None:
    """read my resume -> plans file_read."""
    json_resp = (
        '{"tool_name": "file_read", "parameters": {"path": ".", "query": "resume"}, '
        '"requires_reasoning": false}'
    )
    provider = _MockTrackingProvider(response_text=json_resp)
    gateway = _build_gateway_with_provider(provider)
    tool_registry = ToolRegistry()
    tool_registry.register(FileReadTool())

    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)
    req = await planner.plan_tool_intent("read my resume")

    assert req is not None
    assert req.tool_name == "file_read"
    assert req.parameters["query"] == "resume"


@pytest.mark.asyncio
async def test_tool_planner_nl_extract_pdf() -> None:
    """extract the text from my PDF -> plans file_read with requires_reasoning=false."""
    json_resp = (
        '{"tool_name": "file_read", "parameters": {"path": ".", "query": "PDF"}, '
        '"requires_reasoning": false}'
    )
    provider = _MockTrackingProvider(response_text=json_resp)
    gateway = _build_gateway_with_provider(provider)
    tool_registry = ToolRegistry()
    tool_registry.register(FileReadTool())

    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)
    req = await planner.plan_tool_intent("extract the text from my PDF")

    assert req is not None
    assert req.tool_name == "file_read"
    assert req.requires_reasoning is False


@pytest.mark.asyncio
async def test_tool_planner_windows_exact_file() -> None:
    """Exact Windows file path is preserved in file_send request."""
    json_resp = (
        '{"tool_name": "file_send", "parameters": {'
        '"path": "C:\\\\Users\\\\vishn\\\\Downloads\\\\Resume\\\\Resume-.pdf"}, '
        '"requires_reasoning": false}'
    )
    provider = _MockTrackingProvider(response_text=json_resp)
    gateway = _build_gateway_with_provider(provider)
    tool_registry = ToolRegistry()
    tool_registry.register(FileSendTool())

    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)
    req = await planner.plan_tool_intent(
        r"C:\Users\vishn\Downloads\Resume\Resume-.pdf send pdf"
    )

    assert req is not None
    assert req.tool_name == "file_send"
    assert req.parameters["path"] == r"C:\Users\vishn\Downloads\Resume\Resume-.pdf"
    assert req.requires_reasoning is False


@pytest.mark.asyncio
async def test_tool_planner_windows_directory() -> None:
    """Directory path with query is preserved."""
    json_resp = (
        '{"tool_name": "file_send", "parameters": {'
        '"path": "C:\\\\Users\\\\vishn\\\\Downloads\\\\Resume\\\\", "query": "resume"}, '
        '"requires_reasoning": false}'
    )
    provider = _MockTrackingProvider(response_text=json_resp)
    gateway = _build_gateway_with_provider(provider)
    tool_registry = ToolRegistry()
    tool_registry.register(FileSendTool())

    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)
    req = await planner.plan_tool_intent(
        r"C:\Users\vishn\Downloads\Resume\ send me the resume"
    )

    assert req is not None
    assert req.tool_name == "file_send"
    assert req.parameters["path"] == "C:\\Users\\vishn\\Downloads\\Resume\\"
    assert req.parameters["query"] == "resume"


# --- 3. Composite Requests Orchestration Tests ---


@pytest.mark.asyncio
async def test_composite_find_and_summarize() -> None:
    """find my resume and summarize it -> search -> read -> MODEL (requires_reasoning=True)."""
    json_resp = (
        '{"tool_name": "file_search", "parameters": {"query": "resume", "path": "."}, '
        '"requires_reasoning": true}'
    )
    provider = _MockTrackingProvider(response_text=json_resp)
    gateway = _build_gateway_with_provider(provider)
    tool_registry = ToolRegistry()
    tool_registry.register(FileSearchTool())
    tool_registry.register(FileReadTool())

    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)
    orchestrator = BeruOrchestrator(tool_planner=planner)

    plan = await orchestrator.plan(_make_request("find my resume and summarize it"))

    assert len(plan.steps) == 3
    assert plan.steps[0].tool_intent is not None
    assert plan.steps[0].tool_intent.tool_name == "file_search"
    assert plan.steps[1].tool_intent is not None
    assert plan.steps[1].tool_intent.tool_name == "file_read"
    assert plan.steps[2].step_type == StepType.MODEL_INVOCATION
    assert plan.steps[2].model_intent is not None
    assert plan.steps[2].model_intent.capability_requirements.requires_reasoning is True


@pytest.mark.asyncio
async def test_composite_find_summarize_and_send() -> None:
    """find my resume, summarize it, and send me the file -> search -> read -> send -> MODEL."""
    json_resp = (
        '{"tool_name": "file_search", "parameters": {"query": "resume", "path": "."}, '
        '"requires_reasoning": true}'
    )
    provider = _MockTrackingProvider(response_text=json_resp)
    gateway = _build_gateway_with_provider(provider)
    tool_registry = ToolRegistry()
    tool_registry.register(FileSearchTool())
    tool_registry.register(FileReadTool())
    tool_registry.register(FileSendTool())

    planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)
    orchestrator = BeruOrchestrator(tool_planner=planner)

    plan = await orchestrator.plan(
        _make_request("find my resume, summarize it, and send me the file")
    )

    assert len(plan.steps) == 4
    assert plan.steps[0].tool_intent.tool_name == "file_search"  # type: ignore[union-attr]
    assert plan.steps[1].tool_intent.tool_name == "file_read"  # type: ignore[union-attr]
    assert plan.steps[2].tool_intent.tool_name == "file_send"  # type: ignore[union-attr]
    assert plan.steps[3].step_type == StepType.MODEL_INVOCATION
    req_reason = plan.steps[3].model_intent.capability_requirements.requires_reasoning  # type: ignore[union-attr]
    assert req_reason is True


# --- 4. Directory Candidate Resolution Tests ---


@pytest.mark.asyncio
async def test_directory_candidate_ranking(tmp_path: Path) -> None:
    """Directory search with query ranks exact stem and newer files over partial matches."""
    draft_file = tmp_path / "Copy_of_resume.pdf"
    draft_file.write_text("draft resume")

    exact_file = tmp_path / "Resume.pdf"
    exact_file.write_text("exact resume content")

    notes_file = tmp_path / "notes.txt"
    notes_file.write_text("random notes")

    policy = SafePathPolicy(workspace_root=tmp_path)
    tool = FileSendTool(path_policy=policy)

    req = ToolInvocationRequest(
        tool_name="file_send",
        parameters={"path": str(tmp_path), "query": "resume"},
    )
    res = await tool.execute(req)

    assert res.success is True
    assert res.output is not None
    assert isinstance(res.output, dict)
    attachments = res.output.get("attachment_paths", [])
    assert len(attachments) == 1
    assert Path(attachments[0]).name == "Resume.pdf"


@pytest.mark.asyncio
async def test_directory_ambiguous_target_error(tmp_path: Path) -> None:
    """Directory with multiple files and no query returns AMBIGUOUS_DIRECTORY_TARGET error."""
    (tmp_path / "fileA.pdf").write_text("A")
    (tmp_path / "fileB.pdf").write_text("B")

    policy = SafePathPolicy(workspace_root=tmp_path)
    tool = FileSendTool(path_policy=policy)

    req = ToolInvocationRequest(
        tool_name="file_send",
        parameters={"path": str(tmp_path)},
    )
    res = await tool.execute(req)

    assert res.success is False
    assert res.error is not None
    assert res.error.code == "AMBIGUOUS_DIRECTORY_TARGET"


# --- 5. Authoritative Result Grounding & Invocations Count Tests ---


@pytest.mark.asyncio
async def test_successful_file_send_deterministic_confirmation(tmp_path: Path) -> None:
    """Successful FileSend produces deterministic confirmation without second model call."""
    test_pdf = tmp_path / "Resume-.pdf"
    test_pdf.write_bytes(b"%PDF-1.4 test")

    policy = SafePathPolicy(workspace_root=tmp_path)
    send_tool = FileSendTool(path_policy=policy)
    tool_registry = ToolRegistry()
    tool_registry.register(send_tool)

    mock_provider = _MockTrackingProvider(response_text="I cannot access filesystem")
    gateway = _build_gateway_with_provider(mock_provider)

    json_resp = (
        f'{{"tool_name": "file_send", "parameters": {{"path": "{test_pdf.as_posix()}"}}, '
        f'"requires_reasoning": false}}'
    )
    planner_provider = _MockTrackingProvider(response_text=json_resp)
    planner_gw = _build_gateway_with_provider(planner_provider)
    planner = ToolIntentPlanner(gateway=planner_gw, tool_registry=tool_registry)

    orchestrator = BeruOrchestrator(tool_planner=planner)
    runner = HarnessRunner(
        gateway=gateway,
        tool_registry=tool_registry,
        permission_gate=AllowAllPermissionGate(),
    )

    request = _make_request(f"{test_pdf.as_posix()} send pdf")
    plan = await orchestrator.plan(request)

    # Execute plan
    result = await runner.run(plan, request.context)

    # 1. Authoritative result: MUST NOT contradict with "I cannot access filesystem"
    assert result.success is True
    assert result.output_text == "Sent Resume-.pdf 📄"
    assert "cannot access" not in result.output_text.lower()

    # 2. Latency protection: generation model was NEVER invoked for confirmation!
    assert mock_provider.invocation_count == 0

    # 3. File attachment is attached
    assert len(result.file_attachments) == 1
    assert result.file_attachments[0].name == "Resume-.pdf"


@pytest.mark.asyncio
async def test_reasoning_request_invokes_model_with_authoritative_grounding(
    tmp_path: Path,
) -> None:
    """When reasoning IS requested, model is invoked with strict anti-contradiction prompt."""
    test_file = tmp_path / "doc.txt"
    test_file.write_text("Antigravity agentic architecture notes.")

    policy = SafePathPolicy(workspace_root=tmp_path)
    read_tool = FileReadTool(path_policy=policy)
    tool_registry = ToolRegistry()
    tool_registry.register(read_tool)

    mock_provider = _MockTrackingProvider(
        response_text="Here is a summary of the doc: Antigravity notes."
    )
    gateway = _build_gateway_with_provider(mock_provider)

    json_resp = (
        f'{{"tool_name": "file_read", "parameters": {{"path": "{test_file.as_posix()}"}}, '
        f'"requires_reasoning": true}}'
    )
    planner_provider = _MockTrackingProvider(response_text=json_resp)
    planner_gw = _build_gateway_with_provider(planner_provider)
    planner = ToolIntentPlanner(gateway=planner_gw, tool_registry=tool_registry)

    orchestrator = BeruOrchestrator(tool_planner=planner)
    runner = HarnessRunner(
        gateway=gateway,
        tool_registry=tool_registry,
        permission_gate=AllowAllPermissionGate(),
    )

    request = _make_request(f"read and summarize {test_file.as_posix()}")
    plan = await orchestrator.plan(request)

    result = await runner.run(plan, request.context)

    # Model was invoked for reasoning
    assert mock_provider.invocation_count == 1
    assert result.success is True
    assert "summary" in result.output_text.lower()  # type: ignore[operator]

    # Context assembler injected authoritative system constraints
    assert mock_provider.last_prompt is not None
    prompt_str = mock_provider.last_prompt.user_instruction
    assert "AUTHORITATIVE SYSTEM CONSTRAINTS" in prompt_str
    assert "NEVER state 'I cannot access your filesystem'" in prompt_str
