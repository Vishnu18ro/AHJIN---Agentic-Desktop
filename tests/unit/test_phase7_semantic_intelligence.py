"""Phase 7 — Independent Semantic Capability Intelligence regression tests.

Tests the core Phase 7 invariants:
  1. may_require_tool() is a conservative pass-through: True for ALL tool requests
     regardless of phrasing, including short requests.
  2. detect_tool_intent() no longer false-positives on "find the bug in my code".
  3. ToolIntentPlanner correctly maps semantic paraphrases to tools via mock LLM.
  4. ToolIntentPlanner correctly returns None for non-tool requests.
"""

from pathlib import Path

import pytest

from ahjin.beru.orchestrator import BeruOrchestrator
from ahjin.beru.tool_planner import PlannerStatus, ToolIntentPlanner
from ahjin.beru.tools import detect_tool_intent, may_require_tool
from ahjin.beru.types import StepType
from ahjin.core.types import TaskContext, TaskRequest, UserIntent
from ahjin.harness.gateway import ProviderGateway
from ahjin.harness.runner import HarnessRunner
from ahjin.models.catalog import ModelCatalog
from ahjin.models.router import ModelRouter
from ahjin.models.types import ModelCapabilities, ModelDescriptor, ModelTier
from ahjin.providers.base import BaseModelProvider
from ahjin.providers.registry import ProviderRegistry
from ahjin.providers.types import ModelInvocationRequest, ModelInvocationResponse
from ahjin.security.allow_all import AllowAllPermissionGate
from ahjin.security.path_policy import SafePathPolicy
from ahjin.tools import FileReadTool, FileSearchTool, FileSendTool, ToolRegistry
from ahjin.tools.base import ToolInvocationRequest
from ahjin.tools.system_info import SystemInfoTool

# ---------------------------------------------------------------------------
# Test Infrastructure
# ---------------------------------------------------------------------------


class _MockPlannerProvider(BaseModelProvider):
    """Mock provider that returns a pre-configured JSON response."""

    def __init__(self, response_json: str) -> None:
        self._response_json = response_json

    @property
    def provider_id(self) -> str:
        return "mock_phase7"

    def get_default_model_id(self) -> str:
        return "mock-phase7-model"

    async def invoke(self, request: ModelInvocationRequest) -> ModelInvocationResponse:
        return ModelInvocationResponse(
            invocation_id=request.invocation_id,
            content=self._response_json,
            provider_id=self.provider_id,
            model_id=request.model_id,
        )


def _build_planner(response_json: str, *tools: object) -> ToolIntentPlanner:
    provider = _MockPlannerProvider(response_json)
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
    for tool in tools:
        tool_registry.register(tool)  # type: ignore[arg-type]

    return ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)


def _make_request(prompt: str) -> TaskRequest:
    return TaskRequest(
        intent=UserIntent(primary_text=prompt),
        context=TaskContext(session_id="test-phase7"),
    )


# ---------------------------------------------------------------------------
# Group A — Gate: Conservative pass-through for ALL tool request paraphrases
# ---------------------------------------------------------------------------


class TestGatePassthrough:
    """may_require_tool() must return True for all tool-relevant requests,
    regardless of phrasing — including short requests."""

    # System info paraphrases
    def test_gate_os_paraphrase(self) -> None:
        assert may_require_tool("what OS am I using") is True

    def test_gate_specs_paraphrase(self) -> None:
        assert may_require_tool("what is the specs of my pc") is True

    def test_gate_specifications_paraphrase(self) -> None:
        assert may_require_tool("what are my computer specifications?") is True

    def test_gate_processor_paraphrase(self) -> None:
        assert may_require_tool("which processor am I running?") is True

    def test_gate_ram_paraphrase(self) -> None:
        assert may_require_tool("how much RAM does this machine have?") is True

    def test_gate_hardware_paraphrase(self) -> None:
        assert may_require_tool("what hardware is inside this machine?") is True

    def test_gate_tell_me_about_machine(self) -> None:
        assert may_require_tool("tell me about this machine") is True

    def test_gate_cpu_paraphrase(self) -> None:
        assert may_require_tool("what CPU does this computer have") is True

    def test_gate_windows_version(self) -> None:
        assert may_require_tool("what windows version do I have") is True

    # Short requests — MUST NOT be excluded by length (user correction applied)
    def test_gate_short_send_resume(self) -> None:
        """'send resume' is 2 tokens — must pass (short requests can be tool requests)."""
        assert may_require_tool("send resume") is True

    def test_gate_short_read_resume(self) -> None:
        """'read resume' is 2 tokens — must pass."""
        assert may_require_tool("read resume") is True

    def test_gate_short_my_pc(self) -> None:
        """'my PC?' is 2 tokens — must pass."""
        assert may_require_tool("my PC?") is True

    def test_gate_short_cpu_specs(self) -> None:
        """'CPU specs?' is 2 tokens — must pass."""
        assert may_require_tool("CPU specs?") is True

    # File operation paraphrases
    def test_gate_send_resume(self) -> None:
        assert may_require_tool("send me my resume") is True

    def test_gate_get_cv(self) -> None:
        assert may_require_tool("get my CV from Downloads") is True

    def test_gate_find_attach(self) -> None:
        assert (
            may_require_tool("my resume should be somewhere under Downloads, find it and attach it")
            is True
        )

    def test_gate_composite_request(self) -> None:
        assert (
            may_require_tool(
                "send me resume- from downloads it might be anywhere also return the location"
            )
            is True
        )

    def test_gate_find_and_summarize(self) -> None:
        assert may_require_tool("find my resume and summarize it") is True

    # Requests that were PREVIOUSLY false positives or misses — now all pass through
    def test_gate_find_bug_passes_to_planner(self) -> None:
        """'find the bug' should reach the planner (which returns None). Gate must pass it."""
        assert may_require_tool("find the bug in my code") is True

    def test_gate_windows_path(self) -> None:
        assert may_require_tool(r"C:\Users\vishn\Downloads\Resume\ send me the resume") is True


# ---------------------------------------------------------------------------
# Group B — detect_tool_intent(): False positive bug fix
# ---------------------------------------------------------------------------


class TestDetectToolIntentBugFix:
    """Verify 'find the bug in my code' no longer falsely fires file_search."""

    def test_find_bug_no_longer_triggers_file_search(self) -> None:
        """'find the ' prefix was removed from _FILE_SEARCH_PREFIXES — must be None now."""
        result = detect_tool_intent("find the bug in my code")
        assert result is None, (
            f"Expected None but got tool='{result.tool_name if result else None}'. "
            "The 'find the ' false positive must be fixed."
        )

    def test_find_the_error_no_longer_triggers_file_search(self) -> None:
        result = detect_tool_intent("find the error in this function")
        assert result is None

    def test_find_my_resume_still_works(self) -> None:
        """'find my ' is still in the prefix list — must still fire file_search."""
        result = detect_tool_intent("find my resume")
        assert result is not None
        assert result.tool_name == "file_search"

    def test_os_shortcut_still_works(self) -> None:
        result = detect_tool_intent("what OS am I using")
        assert result is not None
        assert result.tool_name == "system_info"

    def test_system_info_phrase_still_works(self) -> None:
        result = detect_tool_intent("system information")
        assert result is not None
        assert result.tool_name == "system_info"

    def test_whatsapp_shortcut_still_works(self) -> None:
        result = detect_tool_intent("open whatsapp web")
        assert result is not None
        assert result.tool_name == "browser"


# ---------------------------------------------------------------------------
# Group C — ToolIntentPlanner: System info semantic paraphrases
# ---------------------------------------------------------------------------


class TestPlannerSystemInfoSemantics:
    """Planner correctly maps diverse system info paraphrases to system_info tool."""

    @pytest.mark.asyncio
    async def test_planner_specs_of_pc(self) -> None:
        json_resp = '{"tool_name": "system_info", "parameters": {"fields": ["all_safe"]}, "requires_reasoning": false}'  # noqa: E501
        planner = _build_planner(json_resp, SystemInfoTool())
        result = await planner.plan_tool_intent("what is the specs of my pc?")
        assert result is not None
        assert result.tool_name == "system_info"
        assert result.requires_reasoning is False

    @pytest.mark.asyncio
    async def test_planner_computer_specifications(self) -> None:
        json_resp = '{"tool_name": "system_info", "parameters": {"fields": ["all_safe"]}, "requires_reasoning": false}'  # noqa: E501
        planner = _build_planner(json_resp, SystemInfoTool())
        result = await planner.plan_tool_intent("what are my computer specifications?")
        assert result is not None
        assert result.tool_name == "system_info"

    @pytest.mark.asyncio
    async def test_planner_processor_paraphrase(self) -> None:
        json_resp = '{"tool_name": "system_info", "parameters": {"fields": ["cpu"]}, "requires_reasoning": false}'  # noqa: E501
        planner = _build_planner(json_resp, SystemInfoTool())
        result = await planner.plan_tool_intent("which processor am I running?")
        assert result is not None
        assert result.tool_name == "system_info"
        assert "cpu" in result.parameters.get("fields", [])

    @pytest.mark.asyncio
    async def test_planner_ram_paraphrase(self) -> None:
        json_resp = '{"tool_name": "system_info", "parameters": {"fields": ["memory"]}, "requires_reasoning": false}'  # noqa: E501
        planner = _build_planner(json_resp, SystemInfoTool())
        result = await planner.plan_tool_intent("how much RAM does this PC have?")
        assert result is not None
        assert result.tool_name == "system_info"
        assert "memory" in result.parameters.get("fields", [])

    @pytest.mark.asyncio
    async def test_planner_hardware_paraphrase(self) -> None:
        json_resp = '{"tool_name": "system_info", "parameters": {"fields": ["all_safe"]}, "requires_reasoning": false}'  # noqa: E501
        planner = _build_planner(json_resp, SystemInfoTool())
        result = await planner.plan_tool_intent("what hardware is inside this machine?")
        assert result is not None
        assert result.tool_name == "system_info"

    @pytest.mark.asyncio
    async def test_planner_tell_me_about_machine(self) -> None:
        json_resp = '{"tool_name": "system_info", "parameters": {"fields": ["all_safe"]}, "requires_reasoning": false}'  # noqa: E501
        planner = _build_planner(json_resp, SystemInfoTool())
        result = await planner.plan_tool_intent("tell me about this machine")
        assert result is not None
        assert result.tool_name == "system_info"

    @pytest.mark.asyncio
    async def test_planner_short_cpu_specs(self) -> None:
        json_resp = '{"tool_name": "system_info", "parameters": {"fields": ["cpu"]}, "requires_reasoning": false}'  # noqa: E501
        planner = _build_planner(json_resp, SystemInfoTool())
        result = await planner.plan_tool_intent("CPU specs?")
        assert result is not None
        assert result.tool_name == "system_info"


# ---------------------------------------------------------------------------
# Group D — ToolIntentPlanner: File request paraphrases
# ---------------------------------------------------------------------------


class TestPlannerFileSemantics:
    """Planner correctly maps diverse file request paraphrases to file tools."""

    @pytest.mark.asyncio
    async def test_planner_send_resume(self) -> None:
        json_resp = '{"tool_name": "file_send", "parameters": {"path": ".", "query": "resume"}, "requires_reasoning": false}'  # noqa: E501
        planner = _build_planner(json_resp, FileSendTool())
        result = await planner.plan_tool_intent("send me my resume")
        assert result is not None
        assert result.tool_name == "file_send"
        assert result.parameters.get("query") == "resume"
        assert result.requires_reasoning is False

    @pytest.mark.asyncio
    async def test_planner_short_send_resume(self) -> None:
        """Short 2-token request must be handled correctly."""
        json_resp = '{"tool_name": "file_send", "parameters": {"path": ".", "query": "resume"}, "requires_reasoning": false}'  # noqa: E501
        planner = _build_planner(json_resp, FileSendTool())
        result = await planner.plan_tool_intent("send resume")
        assert result is not None
        assert result.tool_name == "file_send"

    @pytest.mark.asyncio
    async def test_planner_get_cv(self) -> None:
        json_resp = '{"tool_name": "file_search", "parameters": {"query": "cv", "path": "downloads"}, "requires_reasoning": false}'  # noqa: E501
        planner = _build_planner(json_resp, FileSearchTool())
        result = await planner.plan_tool_intent("get my CV from Downloads")
        assert result is not None
        assert result.tool_name == "file_search"
        assert result.parameters.get("path") == "downloads"

    @pytest.mark.asyncio
    async def test_planner_resume_somewhere_downloads(self) -> None:
        json_resp = '{"tool_name": "file_search", "parameters": {"query": "resume", "path": "downloads"}, "requires_reasoning": false}'  # noqa: E501
        planner = _build_planner(json_resp, FileSearchTool())
        result = await planner.plan_tool_intent(
            "my resume should be somewhere under Downloads, find it and attach it"
        )
        assert result is not None
        assert result.tool_name == "file_search"

    @pytest.mark.asyncio
    async def test_planner_find_resume_tell_location(self) -> None:
        json_resp = '{"tool_name": "file_search", "parameters": {"query": "resume", "path": "."}, "requires_reasoning": true}'  # noqa: E501
        planner = _build_planner(json_resp, FileSearchTool())
        result = await planner.plan_tool_intent("find my resume and tell me where it is")
        assert result is not None
        assert result.tool_name == "file_search"
        assert result.requires_reasoning is True

    @pytest.mark.asyncio
    async def test_planner_read_resume(self) -> None:
        json_resp = '{"tool_name": "file_read", "parameters": {"path": ".", "query": "resume"}, "requires_reasoning": false}'  # noqa: E501
        planner = _build_planner(json_resp, FileReadTool())
        result = await planner.plan_tool_intent("read resume")
        assert result is not None
        assert result.tool_name == "file_read"


# ---------------------------------------------------------------------------
# Group E — ToolIntentPlanner: Anti-false-positive (non-tool requests → None)
# ---------------------------------------------------------------------------


class TestPlannerNoToolCases:
    """Planner returns NO_TOOL for requests that are not tool operations."""

    @pytest.mark.asyncio
    async def test_planner_find_bug_returns_none(self) -> None:
        """The LLM correctly identifies 'find the bug' as non-filesystem context."""
        json_resp = '{"tool_name": "none", "parameters": {}, "requires_reasoning": false}'
        planner = _build_planner(json_resp, FileSearchTool(), FileReadTool())
        result = await planner.plan_tool_intent("find the bug in my code")
        assert not result
        assert result.status == PlannerStatus.NO_TOOL

    @pytest.mark.asyncio
    async def test_planner_read_code_returns_none(self) -> None:
        json_resp = '{"tool_name": "none", "parameters": {}, "requires_reasoning": false}'
        planner = _build_planner(json_resp, FileReadTool())
        result = await planner.plan_tool_intent(
            "read this code and explain it:\ndef hello():\n    print('hi')"
        )
        assert not result
        assert result.status == PlannerStatus.NO_TOOL

    @pytest.mark.asyncio
    async def test_planner_extract_paragraph_returns_none(self) -> None:
        json_resp = '{"tool_name": "none", "parameters": {}, "requires_reasoning": false}'
        planner = _build_planner(json_resp, FileReadTool())
        result = await planner.plan_tool_intent(
            "extract the key ideas from this paragraph: The universe is vast and..."
        )
        assert not result
        assert result.status == PlannerStatus.NO_TOOL

    @pytest.mark.asyncio
    async def test_planner_creative_writing_returns_none(self) -> None:
        json_resp = '{"tool_name": "none", "parameters": {}, "requires_reasoning": false}'
        planner = _build_planner(json_resp)
        result = await planner.plan_tool_intent("write a poem about the ocean")
        assert not result
        assert result.status == PlannerStatus.NO_TOOL

    @pytest.mark.asyncio
    async def test_planner_general_knowledge_returns_none(self) -> None:
        json_resp = '{"tool_name": "none", "parameters": {}, "requires_reasoning": false}'
        planner = _build_planner(json_resp)
        result = await planner.plan_tool_intent("what is the capital of France?")
        assert not result
        assert result.status == PlannerStatus.NO_TOOL


# ---------------------------------------------------------------------------
# Group F — Paraphrase Equivalence (orchestrator level)
# ---------------------------------------------------------------------------


class TestParaphraseEquivalence:
    """Equivalent paraphrases must produce equivalent plan types."""

    @pytest.mark.asyncio
    async def test_system_info_paraphrases_produce_system_info_plan(self) -> None:
        """Both 'what OS am I using' and 'what operating system...' should produce system_info plans."""  # noqa: E501
        json_resp = '{"tool_name": "system_info", "parameters": {"fields": ["os"]}, "requires_reasoning": false}'  # noqa: E501
        planner = _build_planner(json_resp, SystemInfoTool())
        orchestrator = BeruOrchestrator(tool_planner=planner)

        plan_a = await orchestrator.plan(_make_request("what OS am I using"))
        plan_b = await orchestrator.plan(
            _make_request("what operating system is this machine running?")
        )

        assert plan_a.steps[0].step_type == StepType.TOOL_INVOCATION
        assert plan_b.steps[0].step_type == StepType.TOOL_INVOCATION
        assert plan_a.steps[0].tool_intent is not None
        assert plan_b.steps[0].tool_intent is not None
        assert plan_a.steps[0].tool_intent.tool_name == "system_info"
        assert plan_b.steps[0].tool_intent.tool_name == "system_info"

    @pytest.mark.asyncio
    async def test_file_paraphrases_produce_file_operation_plan(self) -> None:
        """'send me my resume' and 'get my CV from Downloads' both produce file operation plans."""
        json_resp_send = '{"tool_name": "file_send", "parameters": {"path": ".", "query": "resume"}, "requires_reasoning": false}'  # noqa: E501
        json_resp_search = '{"tool_name": "file_search", "parameters": {"query": "cv", "path": "downloads"}, "requires_reasoning": false}'  # noqa: E501

        planner_a = _build_planner(json_resp_send, FileSendTool())
        planner_b = _build_planner(json_resp_search, FileSearchTool())

        orchestrator_a = BeruOrchestrator(tool_planner=planner_a)
        orchestrator_b = BeruOrchestrator(tool_planner=planner_b)

        plan_a = await orchestrator_a.plan(_make_request("send me my resume"))
        plan_b = await orchestrator_b.plan(_make_request("get my CV from Downloads"))

        # Both must have a tool invocation as first step
        assert plan_a.steps[0].step_type == StepType.TOOL_INVOCATION
        assert plan_b.steps[0].step_type == StepType.TOOL_INVOCATION
        assert plan_a.steps[0].tool_intent is not None
        assert plan_b.steps[0].tool_intent is not None
        assert plan_a.steps[0].tool_intent.tool_name in ("file_send", "file_search", "file_read")
        assert plan_b.steps[0].tool_intent.tool_name in ("file_send", "file_search", "file_read")


# ---------------------------------------------------------------------------
# Group G — SystemInfo Capability Expansion (Phase 7.2)
# ---------------------------------------------------------------------------


class TestSystemInfoExpandedCapabilities:
    """SystemInfoTool accurately extracts CPU model, cores, physical RAM, GPU, storage."""

    @pytest.mark.asyncio
    async def test_system_info_probes_execute_safely(self) -> None:
        tool = SystemInfoTool()
        req = ToolInvocationRequest(
            tool_name="system_info",
            parameters={"fields": ["cpu", "memory", "gpu", "storage"]},
        )
        res = await tool.execute(req)
        assert res.success is True
        assert res.output is not None
        output_str = str(res.output)
        assert "Processor:" in output_str
        assert "CPU Cores:" in output_str
        assert "Memory:" in output_str
        assert "GPU:" in output_str
        assert "Storage:" in output_str

    @pytest.mark.asyncio
    async def test_system_info_all_safe_includes_expanded_fields(self) -> None:
        tool = SystemInfoTool()
        req = ToolInvocationRequest(
            tool_name="system_info",
            parameters={"fields": ["all_safe"]},
        )
        res = await tool.execute(req)
        assert res.success is True
        output_str = str(res.output)
        assert "OS:" in output_str
        assert "Processor:" in output_str
        assert "CPU Cores:" in output_str
        assert "Memory:" in output_str
        assert "GPU:" in output_str
        assert "Storage:" in output_str

    @pytest.mark.asyncio
    async def test_planner_rejects_unwhitelisted_system_info_fields(self) -> None:
        json_resp = (
            '{"tool_name": "system_info", '
            '"parameters": {"fields": ["passwords", "credit_cards"]}}'
        )
        planner = _build_planner(json_resp, SystemInfoTool())
        res = await planner.plan_tool_intent("give me passwords and credit cards")
        assert not res
        assert res.status == PlannerStatus.PLANNER_FAILURE


# ---------------------------------------------------------------------------
# Group H — 3-State Planner Contract (Phase 7.2)
# ---------------------------------------------------------------------------


class TestThreeStatePlannerContract:
    """ToolIntentPlanner produces explicit TOOL_SELECTED, NO_TOOL, and PLANNER_FAILURE."""

    @pytest.mark.asyncio
    async def test_planner_tool_selected_state(self) -> None:
        json_resp = (
            '{"tool_name": "system_info", '
            '"parameters": {"fields": ["cpu"]}, "requires_reasoning": false}'
        )
        planner = _build_planner(json_resp, SystemInfoTool())
        res = await planner.plan_tool_intent("which processor do I have?")
        assert res.status == PlannerStatus.TOOL_SELECTED
        assert res.tool_name == "system_info"
        assert bool(res) is True

    @pytest.mark.asyncio
    async def test_planner_no_tool_state(self) -> None:
        json_resp = '{"tool_name": "none", "parameters": {}, "requires_reasoning": false}'
        planner = _build_planner(json_resp)
        res = await planner.plan_tool_intent("write a poem about dawn")
        assert res.status == PlannerStatus.NO_TOOL
        assert res.tool_intent is None
        assert bool(res) is False

    @pytest.mark.asyncio
    async def test_planner_failure_on_invalid_json(self) -> None:
        json_resp = "This is not valid json at all!"
        planner = _build_planner(json_resp)
        res = await planner.plan_tool_intent("what OS am I using?")
        assert res.status == PlannerStatus.PLANNER_FAILURE
        assert res.failure_reason == "invalid_json"
        assert bool(res) is False

    @pytest.mark.asyncio
    async def test_planner_failure_on_missing_tool_name(self) -> None:
        json_resp = '{"parameters": {}}'
        planner = _build_planner(json_resp)
        res = await planner.plan_tool_intent("what OS am I using?")
        assert res.status == PlannerStatus.PLANNER_FAILURE
        assert res.failure_reason == "missing_tool_name"
        assert bool(res) is False


# ---------------------------------------------------------------------------
# Group I — Orchestrator Planner Failure Contract (Phase 7.2)
# ---------------------------------------------------------------------------


class TestOrchestratorPlannerFailureContract:
    """A planner failure MUST NOT silently fall back to ungrounded conversational chat."""

    @pytest.mark.asyncio
    async def test_planner_failure_produces_deterministic_error_step(self) -> None:
        json_resp = "invalid json response"
        planner = _build_planner(json_resp)
        orchestrator = BeruOrchestrator(tool_planner=planner)

        request = _make_request("tell me which processor I am using")
        plan = await orchestrator.plan(request)

        # Plan must NOT fall through to conversational chat
        assert len(plan.steps) == 1
        assert plan.steps[0].deterministic_output is not None
        assert "⚠️ Tool intent planning was unable to process" in plan.steps[0].deterministic_output

    @pytest.mark.asyncio
    async def test_runner_executes_deterministic_output_directly(self) -> None:
        json_resp = "invalid json response"
        planner = _build_planner(json_resp)
        orchestrator = BeruOrchestrator(tool_planner=planner)
        runner = HarnessRunner()

        request = _make_request("tell me which processor I am using")
        plan = await orchestrator.plan(request)
        result = await runner.run(plan, request.context)

        assert result.success is True
        assert result.output_text is not None
        assert "⚠️ Tool intent planning was unable to process" in result.output_text
        assert result.runtime_info is not None
        assert result.runtime_info.selected_model == "deterministic"


# ---------------------------------------------------------------------------
# Group J — Search-to-Send Execution Flow & Ambiguity (Phase 7.2)
# ---------------------------------------------------------------------------


class TestSearchToSendArchitecture:
    """file_search -> actual discovered path -> file_send, preserving ambiguity handling."""

    @pytest.mark.asyncio
    async def test_search_to_send_propagates_actual_path(self, tmp_path: Path) -> None:
        downloads = tmp_path / "Downloads"
        downloads.mkdir()
        resume_file = downloads / "Resume_Vishnu.pdf"
        resume_file.write_bytes(b"%PDF-1.4 mock resume content")

        policy = SafePathPolicy(workspace_root=tmp_path, additional_roots=[downloads])
        search_tool = FileSearchTool(path_policy=policy)
        send_tool = FileSendTool(path_policy=policy)

        registry = ToolRegistry()
        registry.register(search_tool)
        registry.register(send_tool)

        runner = HarnessRunner(tool_registry=registry, permission_gate=AllowAllPermissionGate())

        # Build plan: search -> send
        json_resp = (
            '{"tool_name": "file_search", '
            '"parameters": {"query": "Resume", "path": "downloads"}}'
        )
        planner = _build_planner(json_resp, search_tool, send_tool)
        orchestrator = BeruOrchestrator(tool_planner=planner)

        request = _make_request("send me my resume from downloads")
        plan = await orchestrator.plan(request)

        assert plan.steps[0].tool_intent.tool_name == "file_search"
        assert plan.steps[1].tool_intent.tool_name == "file_send"

        result = await runner.run(plan, request.context)
        assert result.success is True
        assert len(result.file_attachments) == 1
        assert result.file_attachments[0].name == "Resume_Vishnu.pdf"

    @pytest.mark.asyncio
    async def test_search_to_send_stops_on_ambiguity(self, tmp_path: Path) -> None:
        downloads = tmp_path / "Downloads"
        downloads.mkdir()
        (downloads / "Resume_Tech.pdf").write_bytes(b"%PDF-1.4 Tech")
        (downloads / "Resume_Mgmt.pdf").write_bytes(b"%PDF-1.4 Mgmt")

        policy = SafePathPolicy(workspace_root=tmp_path, additional_roots=[downloads])
        search_tool = FileSearchTool(path_policy=policy)
        send_tool = FileSendTool(path_policy=policy)

        registry = ToolRegistry()
        registry.register(search_tool)
        registry.register(send_tool)

        runner = HarnessRunner(tool_registry=registry, permission_gate=AllowAllPermissionGate())

        json_resp = (
            '{"tool_name": "file_search", '
            '"parameters": {"query": "Resume", "path": "downloads"}}'
        )
        planner = _build_planner(json_resp, search_tool, send_tool)
        orchestrator = BeruOrchestrator(tool_planner=planner)

        request = _make_request("send me my resume from downloads")
        plan = await orchestrator.plan(request)

        result = await runner.run(plan, request.context)
        # Must NOT arbitrarily attach one file
        assert len(result.file_attachments) == 0
        assert "Multiple matching files found" in (result.output_text or "")

