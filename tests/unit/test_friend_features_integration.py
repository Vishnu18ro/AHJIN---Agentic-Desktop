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

