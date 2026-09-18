"""Tests for friend features integrated into AHJIN canonical architecture."""

import pytest
from pathlib import Path
from PIL import Image

from ahjin.tools.image_edit import ImageEditTool
from ahjin.tools.screen_access import ScreenAccessTool
from ahjin.tools.file_search import FileSearchTool
from ahjin.tools.base import ToolInvocationRequest
from ahjin.security.path_policy import SafePathPolicy
from ahjin.agents.file_agent import FileAgent, FileSessionState
from ahjin.interfaces.telegram.mapper import TelegramMapper
from ahjin.core.types import Role, Modality
from ahjin.models.catalog import create_default_catalog
from ahjin.models.types import ModelRole


def test_nex_remains_primary():
    """Verify nex-agi/nex-n2.5-pro:free is the active primary model at priority 300."""
    cat = create_default_catalog()
    model = cat.get_model("nex-agi/nex-n2.5-pro:free")
    assert model is not None
    assert model.role == ModelRole.PRIMARY
    assert model.priority == 300
    assert model.provider_id == "openrouter"

    # Verify MiniMax is NOT in catalog
    with pytest.raises(KeyError):
        cat.get_model("minimax/minimax-m3")


def test_telegram_mapper_rolling_history_additive():
    """Verify rolling conversation history converts to domain ConversationTurns without replacing context."""
    raw_history = [
        {"role": "user", "content": "Hello AHJIN"},
        {"role": "assistant", "content": "Hello! How can I help you?"},
        {"role": "unknown", "content": "System notice"},
    ]
    req = TelegramMapper.to_task_request(
        chat_id=12345,
        message_text="Send me the report",
        conversation_history=raw_history,
    )

    assert req.intent.primary_text == "Send me the report"
    assert req.intent.modality == Modality.TEXT
    assert req.context.session_id == "telegram:12345"
    assert len(req.context.conversation_history) == 3
    assert req.context.conversation_history[0].role == Role.USER
    assert req.context.conversation_history[0].content == "Hello AHJIN"
    assert req.context.conversation_history[1].role == Role.ASSISTANT
    assert req.context.conversation_history[1].content == "Hello! How can I help you?"
    assert req.context.conversation_history[2].role == Role.SYSTEM


@pytest.mark.asyncio
async def test_image_edit_tool_operations(tmp_path: Path):
    """Verify ImageEditTool performs resize and error handling."""
    img_path = tmp_path / "test.png"
    img = Image.new("RGB", (200, 100), color="blue")
    img.save(img_path)

    policy = SafePathPolicy(workspace_root=tmp_path)
    tool = ImageEditTool(path_policy=policy)

    # 1. Resize operation
    req = ToolInvocationRequest(
        tool_name="image_edit",
        parameters={
            "path": str(img_path),
            "operation": "resize",
            "scale": "50%",
        },
    )
    res = await tool.execute(req)
    assert res.success
    assert res.output is not None
    assert "Successfully processed image" in res.output.get("text", "")

    # 2. Invalid operation
    bad_req = ToolInvocationRequest(
        tool_name="image_edit",
        parameters={
            "path": str(img_path),
            "operation": "rotate_invalid",
        },
    )
    bad_res = await tool.execute(bad_req)
    assert not bad_res.success
    assert "Invalid operation" in (bad_res.error.message if bad_res.error else "")


@pytest.mark.asyncio
async def test_screen_access_tool_structure(monkeypatch: pytest.MonkeyPatch):
    """Verify ScreenAccessTool tool_name and execution failure safety."""
    tool = ScreenAccessTool()
    assert tool.tool_name == "screen_access"
    
    # Mock tunnel and server to avoid spawning real background processes in unit test
    monkeypatch.setattr("ahjin.tools.screen_access._start_server", lambda: None)
    monkeypatch.setattr("ahjin.tools.screen_access._start_tunnel", lambda: "http://mock.tunnel.url")

    req = ToolInvocationRequest(
        tool_name="screen_access",
        parameters={},
    )
    res = await tool.execute(req)
    assert res.success
    assert "http://mock.tunnel.url" in res.output.get("text", "")


def test_find_matching_files_in_tool(tmp_path: Path):
    """Verify FileSearchTool.find_matching_files helper correctly discovers matching files."""
    f1 = tmp_path / "budget_report_2026.pdf"
    f2 = tmp_path / "notes.txt"
    f1.write_text("dummy")
    f2.write_text("dummy")

    policy = SafePathPolicy(workspace_root=tmp_path)
    tool = FileSearchTool(path_policy=policy)
    matches = tool.find_matching_files("budget", path_str=str(tmp_path))
    assert len(matches) == 1
    assert "budget_report_2026.pdf" in matches[0].name


def test_file_agent_intent_and_sessions():
    """Verify FileAgent intent parsing, location parsing, and session management."""
    agent = FileAgent()
    mgr = agent.session_manager

    # 1. Session tracking
    session = mgr.get_session(99999)
    assert session.chat_id == 99999
    assert session.state == FileSessionState.IDLE

    # 2. Intent detection
    is_req, query, loc = agent.detect_file_intent("can you please send me my resume from downloads")
    assert is_req is True
    assert "resume" in query.lower()
    assert loc == "downloads"

    # 3. Location resolution
    assert agent.resolve_location_text("in my desktop") == "desktop"
    assert agent.resolve_location_text("search everywhere please") == "pc"

    # 4. Disambiguation selection resolution
    candidates = [Path("c:/users/test/resume_v1.pdf"), Path("c:/users/test/resume_v2.pdf")]
    assert agent.resolve_selection_text("1", candidates) == 0
    assert agent.resolve_selection_text("second", candidates) == 1
    assert agent.resolve_selection_text("resume_v1", candidates) == 0


def test_persona_instruction_consistency():
    """Verify ContextAssembler and ContextualizedPrompt share the refined persona instruction."""
    from ahjin.harness.context import ContextAssembler
    from ahjin.beru.types import ModelStepIntent
    from ahjin.core.types import TaskContext
    from ahjin.providers.types import ContextualizedPrompt

    assembler = ContextAssembler()
    intent = ModelStepIntent(instruction="HI")
    context = TaskContext(session_id="test")
    prompt = assembler.assemble(intent=intent, task_context=context)

    # Must contain AHJIN 2.0 identity and adaptive greeting instruction
    assert "You are AHJIN 2.0, an Agentic AI Operating Layer." in prompt.system_instruction
    assert "When greeting the user or when explicitly asked about your identity, identify yourself as AHJIN 2.0." in prompt.system_instruction
    assert "Do not use a fixed greeting, fixed capability list, or repetitive self-introduction." in prompt.system_instruction

    # Default in ContextualizedPrompt must match
    default_prompt = ContextualizedPrompt(user_instruction="test")
    assert prompt.system_instruction == default_prompt.system_instruction


@pytest.mark.asyncio
async def test_idle_message_does_not_call_file_agent_planner():
    """Verify FileAgent while IDLE never calls tool_planner.plan_tool_intent on ordinary messages."""
    from unittest.mock import AsyncMock
    from ahjin.beru.tool_planner import ToolIntentPlanner

    mock_planner = AsyncMock(spec=ToolIntentPlanner)
    agent = FileAgent(tool_planner=mock_planner)

    # Test ordinary messages
    for msg in ("HEY", "hello", "what is python", "search the web for news", "why is the sky blue"):
        mock_update = AsyncMock()
        mock_update.message.chat_id = 42
        mock_update.message.text = msg
        mock_context = AsyncMock()

        handled = await agent.handle_message(mock_update, mock_context)
        assert handled is False, f"Expected {msg} to not be claimed by FileAgent"

    # Crucial assertion: tool_planner.plan_tool_intent was NEVER called
    assert mock_planner.plan_tool_intent.call_count == 0


@pytest.mark.asyncio
async def test_telegram_immediate_thinking_and_idle_routing():
    """Verify TelegramAdapter sends 'Thinking...' immediately and routes IDLE messages to Dispatcher."""
    from unittest.mock import AsyncMock, MagicMock
    from ahjin.interfaces.telegram.bot import TelegramAdapter
    from ahjin.core.types import TaskResult, RuntimeInfo
    from uuid import uuid4

    mock_dispatcher = MagicMock()
    
    # Mock dispatch_stream as async generator yielding one token and result
    async def mock_dispatch_stream(req):
        runtime_info = RuntimeInfo(
            selected_model="nex-agi/nex-n2.5-pro:free",
            provider_id="openrouter",
            timing={},
        )
        yield "Hello! I am AHJIN 2.0.", TaskResult(
            task_id=req.task_id or uuid4(),
            correlation_id=req.correlation_id or uuid4(),
            success=True,
            output_text="Hello! I am AHJIN 2.0.",
            runtime_info=runtime_info,
        )

    mock_dispatcher.dispatch_stream = mock_dispatch_stream

    mock_agent = MagicMock(spec=FileAgent)
    mock_agent.is_session_active.return_value = False  # IDLE session

    adapter = TelegramAdapter(dispatcher=mock_dispatcher, file_agent=mock_agent)

    event_log = []

    mock_placeholder = AsyncMock()
    async def mock_reply_text(text, **kwargs):
        event_log.append(f"reply:{text}")
        return mock_placeholder

    async def mock_edit_text(text, **kwargs):
        event_log.append(f"edit:{text[:10]}")

    mock_placeholder.edit_text = mock_edit_text

    mock_update = AsyncMock()
    mock_update.message.chat_id = 12345
    mock_update.message.text = "HEY"
    mock_update.message.reply_text = mock_reply_text
    mock_context = AsyncMock()

    await adapter._handle_message(mock_update, mock_context)

    # 1. "Thinking..." MUST be the very first action sent to Telegram
    assert len(event_log) >= 1
    assert event_log[0] == "reply:Thinking..."

    # 2. FileAgent was checked for active session, but handle_message was NOT called because session is IDLE
    mock_agent.is_session_active.assert_called_once_with(12345)
    mock_agent.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_active_file_agent_session_handled_without_canonical_dispatch():
    """Verify that when FileAgent session is ACTIVE, FileAgent handles message and Dispatcher is bypassed."""
    from unittest.mock import AsyncMock, MagicMock
    from ahjin.interfaces.telegram.bot import TelegramAdapter

    mock_dispatcher = MagicMock()
    mock_dispatcher.dispatch_stream = MagicMock()

    mock_agent = MagicMock(spec=FileAgent)
    mock_agent.is_session_active.return_value = True  # ACTIVE session
    mock_agent.handle_message = AsyncMock(return_value=True)  # Claimed

    adapter = TelegramAdapter(dispatcher=mock_dispatcher, file_agent=mock_agent)

    mock_placeholder = AsyncMock()
    mock_update = AsyncMock()
    mock_update.message.chat_id = 12345
    mock_update.message.text = "downloads"
    mock_update.message.reply_text = AsyncMock(return_value=mock_placeholder)
    mock_context = AsyncMock()

    await adapter._handle_message(mock_update, mock_context)

    # 1. "Thinking..." sent
    mock_update.message.reply_text.assert_called_once_with("Thinking...")

    # 2. FileAgent handled the message
    mock_agent.handle_message.assert_called_once_with(mock_update, mock_context)

    # 3. Placeholder was cleaned up
    mock_placeholder.delete.assert_called_once()

    # 4. Dispatcher was NOT called
    mock_dispatcher.dispatch_stream.assert_not_called()


@pytest.mark.asyncio
async def test_ordinary_message_end_to_end_call_count():
    """Verify that an ordinary message ('HEY') results in exactly 2 LLM/gateway calls (1 planner + 1 runner)."""
    from unittest.mock import AsyncMock, MagicMock
    from ahjin.beru.orchestrator import BeruOrchestrator
    from ahjin.beru.tool_planner import ToolIntentPlanner
    from ahjin.core.dispatcher import TaskDispatcher
    from ahjin.harness.runner import HarnessRunner
    from ahjin.interfaces.telegram.bot import TelegramAdapter
    from ahjin.tools.registry import ToolRegistry
    from ahjin.security import AllowAllPermissionGate
    from ahjin.models.router import ModelRouter

    # Mock gateway
    mock_gateway = MagicMock()
    mock_gateway.router = ModelRouter()

    # Track invoke_stream calls and ordering
    gateway_calls = []

    async def mock_invoke_stream(prompt, requirements=None, excluded_model_ids=None):
        selection = mock_gateway.router.select_model(requirements, excluded_model_ids=excluded_model_ids)
        if "JSON" in prompt.system_instruction or "tool_name" in prompt.system_instruction:
            # Planner response
            gateway_calls.append("planner")
            yield '{"tool_name": "none", "parameters": {}, "requires_reasoning": false}', selection
        else:
            # Runner/generation response
            gateway_calls.append("generation")
            yield "Hello! ", selection
            yield "I am AHJIN 2.0.", selection

    mock_gateway.invoke_stream = mock_invoke_stream

    registry = ToolRegistry()
    planner = ToolIntentPlanner(gateway=mock_gateway, tool_registry=registry)
    orchestrator = BeruOrchestrator(tool_planner=planner)
    runner = HarnessRunner(
        gateway=mock_gateway,
        tool_registry=registry,
        permission_gate=AllowAllPermissionGate(),
    )
    dispatcher = TaskDispatcher(orchestrator=orchestrator, runner=runner)

    file_agent = FileAgent(gateway=mock_gateway, tool_planner=planner)
    adapter = TelegramAdapter(dispatcher=dispatcher, file_agent=file_agent)

    event_sequence = []
    mock_placeholder = AsyncMock()

    async def mock_reply_text(text, **kwargs):
        event_sequence.append(f"reply:{text}")
        return mock_placeholder

    async def mock_edit_text(text, **kwargs):
        event_sequence.append("edit")

    mock_placeholder.edit_text = mock_edit_text

    mock_update = AsyncMock()
    mock_update.message.chat_id = 777
    mock_update.message.text = "HEY"
    mock_update.message.reply_text = mock_reply_text
    mock_context = AsyncMock()

    await adapter._handle_message(mock_update, mock_context)

    # 1. First event MUST be Telegram "Thinking..."
    assert len(event_sequence) >= 1
    assert event_sequence[0] == "reply:Thinking..."

    # 2. Total gateway/LLM calls MUST be exactly 2 (canonical: 1 planner, 1 generation)
    assert len(gateway_calls) == 2, f"Expected 2 gateway calls, got {len(gateway_calls)}: {gateway_calls}"
    assert gateway_calls == ["planner", "generation"]



