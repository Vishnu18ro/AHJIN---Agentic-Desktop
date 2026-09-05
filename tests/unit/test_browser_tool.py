"""Unit tests for Phase 7B BrowserTool and Browser Session Management."""

from unittest.mock import AsyncMock

import pytest

from ahjin.tools.base import ToolInvocationRequest
from ahjin.tools.browser import BrowserSessionManager, BrowserTool
from ahjin.tools.registry import ToolRegistry


@pytest.mark.asyncio
async def test_browser_tool_registration() -> None:
    """Test 1: Browser tool registration in ToolRegistry."""
    registry = ToolRegistry()
    tool = BrowserTool()
    registry.register(tool)

    assert registry.has_tool("browser")
    assert registry.get_tool("browser").tool_name == "browser"


@pytest.mark.asyncio
async def test_browser_navigation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test 2: Navigation (browser_navigate)."""
    tool = BrowserTool()

    mock_page = AsyncMock()
    mock_page.is_closed.return_value = False
    mock_page.url = "https://example.com"
    mock_page.title = AsyncMock(return_value="Example Domain")
    mock_page.evaluate = AsyncMock(return_value="Example Domain Text")
    mock_page.query_selector_all = AsyncMock(return_value=[])

    async def mock_get_page(headless: bool = False) -> AsyncMock:
        return mock_page

    monkeypatch.setattr(tool.session, "get_or_create_page", mock_get_page)

    req = ToolInvocationRequest(
        tool_name="browser",
        parameters={"action": "navigate", "url": "https://example.com"},
    )
    res = await tool.execute(req)

    assert res.success
    assert "[BROWSER OBSERVATION]" in str(res.output)
    assert "https://example.com" in str(res.output)
    mock_page.goto.assert_called_once()


@pytest.mark.asyncio
async def test_browser_observation_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test 3: Observation parsing (browser_observe)."""
    tool = BrowserTool()

    mock_page = AsyncMock()
    mock_page.is_closed.return_value = False
    mock_page.url = "https://example.com/test"
    mock_page.title = AsyncMock(return_value="Test Page")
    mock_page.evaluate = AsyncMock(return_value="Welcome to the test page")

    mock_input = AsyncMock()
    mock_input.evaluate = AsyncMock(return_value="input")
    mock_input.get_attribute = AsyncMock(
        side_effect=lambda attr: "q" if attr == "name" else "text"
    )
    mock_input.input_value = AsyncMock(return_value="")

    mock_page.query_selector_all = AsyncMock(
        side_effect=lambda sel: [mock_input] if "input" in sel else []
    )

    async def mock_get_page(headless: bool = False) -> AsyncMock:
        return mock_page

    monkeypatch.setattr(tool.session, "get_or_create_page", mock_get_page)

    req = ToolInvocationRequest(tool_name="browser", parameters={"action": "observe"})
    res = await tool.execute(req)

    assert res.success
    output_str = str(res.output)
    assert "URL: https://example.com/test" in output_str
    assert "Title: Test Page" in output_str
    assert "VISIBLE TEXT:" in output_str
    assert "INTERACTIVE ELEMENTS:" in output_str


@pytest.mark.asyncio
async def test_browser_click(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test 4: Click action (browser_click)."""
    tool = BrowserTool()

    mock_page = AsyncMock()
    mock_page.is_closed.return_value = False
    mock_page.url = "https://example.com"
    mock_page.title = AsyncMock(return_value="Page")
    mock_page.evaluate = AsyncMock(return_value="Text")
    mock_page.query_selector_all = AsyncMock(return_value=[])

    async def mock_get_page(headless: bool = False) -> AsyncMock:
        return mock_page

    monkeypatch.setattr(tool.session, "get_or_create_page", mock_get_page)

    req = ToolInvocationRequest(
        tool_name="browser",
        parameters={"action": "click", "selector": "#submit-btn"},
    )
    res = await tool.execute(req)

    assert res.success
    mock_page.click.assert_called_with("#submit-btn", timeout=5000)


@pytest.mark.asyncio
async def test_browser_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test 5: Type action (browser_type)."""
    tool = BrowserTool()

    mock_page = AsyncMock()
    mock_page.is_closed.return_value = False
    mock_page.url = "https://example.com"
    mock_page.title = AsyncMock(return_value="Page")
    mock_page.evaluate = AsyncMock(return_value="Text")
    mock_page.query_selector_all = AsyncMock(return_value=[])

    async def mock_get_page(headless: bool = False) -> AsyncMock:
        return mock_page

    monkeypatch.setattr(tool.session, "get_or_create_page", mock_get_page)

    req = ToolInvocationRequest(
        tool_name="browser",
        parameters={"action": "type", "selector": "input[name='q']", "text": "NVIDIA"},
    )
    res = await tool.execute(req)

    assert res.success
    mock_page.fill.assert_called_with("input[name='q']", "NVIDIA", timeout=5000)


@pytest.mark.asyncio
async def test_browser_press(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test 6: Press key action (browser_press)."""
    tool = BrowserTool()

    mock_page = AsyncMock()
    mock_page.is_closed.return_value = False
    mock_page.url = "https://example.com"
    mock_page.title = AsyncMock(return_value="Page")
    mock_page.evaluate = AsyncMock(return_value="Text")
    mock_page.query_selector_all = AsyncMock(return_value=[])

    async def mock_get_page(headless: bool = False) -> AsyncMock:
        return mock_page

    monkeypatch.setattr(tool.session, "get_or_create_page", mock_get_page)

    req = ToolInvocationRequest(
        tool_name="browser",
        parameters={"action": "press", "key": "Enter"},
    )
    res = await tool.execute(req)

    assert res.success
    mock_page.keyboard.press.assert_called_with("Enter")


@pytest.mark.asyncio
async def test_browser_scroll(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test 7: Scroll action (browser_scroll)."""
    tool = BrowserTool()

    mock_page = AsyncMock()
    mock_page.is_closed.return_value = False
    mock_page.url = "https://example.com"
    mock_page.title = AsyncMock(return_value="Page")
    mock_page.evaluate = AsyncMock(return_value="Text")
    mock_page.query_selector_all = AsyncMock(return_value=[])

    async def mock_get_page(headless: bool = False) -> AsyncMock:
        return mock_page

    monkeypatch.setattr(tool.session, "get_or_create_page", mock_get_page)

    req = ToolInvocationRequest(
        tool_name="browser",
        parameters={"action": "scroll", "direction": "down", "amount": 500},
    )
    res = await tool.execute(req)

    assert res.success
    mock_page.evaluate.assert_any_call("window.scrollBy(0, 500)")


@pytest.mark.asyncio
async def test_browser_screenshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test 8: Screenshot action (browser_screenshot)."""
    tool = BrowserTool()

    mock_page = AsyncMock()
    mock_page.is_closed.return_value = False
    mock_page.url = "https://example.com"
    mock_page.title = AsyncMock(return_value="Screenshot Page")

    async def mock_get_page(headless: bool = False) -> AsyncMock:
        return mock_page

    monkeypatch.setattr(tool.session, "get_or_create_page", mock_get_page)

    req = ToolInvocationRequest(
        tool_name="browser",
        parameters={"action": "screenshot"},
    )
    res = await tool.execute(req)

    assert res.success
    assert "SCREENSHOT_CAPTURED" in str(res.output)
    mock_page.screenshot.assert_called_once()


@pytest.mark.asyncio
async def test_browser_session_reuse(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test 9: Session reuse across multi-step browser commands."""
    session_mgr = BrowserSessionManager()
    tool = BrowserTool(session_manager=session_mgr)

    mock_page = AsyncMock()
    mock_page.is_closed.return_value = False

    async def mock_get_page(headless: bool = False) -> AsyncMock:
        return mock_page

    monkeypatch.setattr(session_mgr, "get_or_create_page", mock_get_page)

    req1 = ToolInvocationRequest(tool_name="browser", parameters={"action": "open", "url": "https://google.com"})
    req2 = ToolInvocationRequest(tool_name="browser", parameters={"action": "observe"})

    res1 = await tool.execute(req1)
    res2 = await tool.execute(req2)

    assert res1.success
    assert res2.success
    assert session_mgr.action_count == 2


@pytest.mark.asyncio
async def test_action_budget_limit() -> None:
    """Test 10: Action budget per task limit enforced."""
    session_mgr = BrowserSessionManager()
    session_mgr._action_count = 10  # Exceed limit
    tool = BrowserTool(session_manager=session_mgr)

    req = ToolInvocationRequest(tool_name="browser", parameters={"action": "observe"})
    res = await tool.execute(req)

    assert not res.success
    assert "ACTION_BUDGET_EXCEEDED" in str(res.output)
    assert res.error is not None
    assert res.error.code == "ACTION_BUDGET_EXCEEDED"


@pytest.mark.asyncio
async def test_invalid_selector_handling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test 11: Invalid selector handling (ELEMENT_NOT_FOUND)."""
    tool = BrowserTool()

    mock_page = AsyncMock()
    mock_page.is_closed.return_value = False
    mock_page.click.side_effect = Exception("Element not found")
    mock_page.get_by_text.return_value.click.side_effect = Exception("Text not found")

    async def mock_get_page(headless: bool = False) -> AsyncMock:
        return mock_page

    monkeypatch.setattr(tool.session, "get_or_create_page", mock_get_page)

    req = ToolInvocationRequest(
        tool_name="browser",
        parameters={"action": "click", "selector": "#nonexistent"},
    )
    res = await tool.execute(req)

    assert not res.success
    assert "ELEMENT_NOT_FOUND" in str(res.output)
    assert res.error is not None
    assert res.error.code == "ELEMENT_NOT_FOUND"


@pytest.mark.asyncio
async def test_navigation_failure_handling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test 12: Navigation failure / timeout handling."""
    tool = BrowserTool()

    mock_page = AsyncMock()
    mock_page.is_closed.return_value = False
    mock_page.goto.side_effect = Exception("Timeout 15000ms exceeded")

    async def mock_get_page(headless: bool = False) -> AsyncMock:
        return mock_page

    monkeypatch.setattr(tool.session, "get_or_create_page", mock_get_page)

    req = ToolInvocationRequest(
        tool_name="browser",
        parameters={"action": "navigate", "url": "https://unreachable-site.invalid"},
    )
    res = await tool.execute(req)

    assert not res.success
    assert "TIMEOUT" in str(res.output)
    assert res.error is not None
    assert res.error.code == "TIMEOUT"


@pytest.mark.asyncio
async def test_confirmation_required_for_side_effect() -> None:
    """Test 13: Confirmation required for side-effect actions."""
    tool = BrowserTool()
    req = ToolInvocationRequest(
        tool_name="browser",
        parameters={"action": "send_message", "is_side_effect": True},
    )
    res = await tool.execute(req)

    assert not res.success
    assert "CONFIRMATION_REQUIRED" in str(res.output)
    assert res.error is not None
    assert res.error.code == "CONFIRMATION_REQUIRED"


@pytest.mark.asyncio
async def test_authentication_required_handling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test 14: Authentication required detection on login pages."""
    tool = BrowserTool()

    mock_page = AsyncMock()
    mock_page.is_closed.return_value = False
    mock_page.url = "https://web.whatsapp.com"
    mock_page.title = AsyncMock(return_value="WhatsApp Web")
    mock_page.evaluate = AsyncMock(
        return_value="To use WhatsApp on your computer: Scan QR Code to log in"
    )

    async def mock_get_page(headless: bool = False) -> AsyncMock:
        return mock_page

    monkeypatch.setattr(tool.session, "get_or_create_page", mock_get_page)

    req = ToolInvocationRequest(
        tool_name="browser",
        parameters={"action": "navigate", "url": "https://web.whatsapp.com"},
    )
    res = await tool.execute(req)

    assert res.success
    assert "AUTHENTICATION_REQUIRED" in str(res.output)


@pytest.mark.asyncio
async def test_browser_close(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test 15: Safe browser session closure (browser_close)."""
    session_mgr = BrowserSessionManager()
    mock_close = AsyncMock()
    monkeypatch.setattr(session_mgr, "close", mock_close)

    tool = BrowserTool(session_manager=session_mgr)
    req = ToolInvocationRequest(tool_name="browser", parameters={"action": "close"})
    res = await tool.execute(req)

    assert res.success
    assert "CLOSED" in str(res.output)
    mock_close.assert_called_once()


@pytest.mark.asyncio
async def test_headless_defaults_to_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test 16: Ensure headless defaults to False for visible browser execution."""
    session_mgr = BrowserSessionManager()
    tool = BrowserTool(session_manager=session_mgr)
    captured_headless: list[bool] = []

    mock_page = AsyncMock()
    mock_page.is_closed.return_value = False
    mock_page.url = "https://example.com"
    mock_page.title = AsyncMock(return_value="Example")
    mock_page.evaluate = AsyncMock(return_value="Text")
    mock_page.query_selector_all = AsyncMock(return_value=[])

    async def mock_get_page(headless: bool = False) -> AsyncMock:
        captured_headless.append(headless)
        return mock_page

    monkeypatch.setattr(session_mgr, "get_or_create_page", mock_get_page)

    req = ToolInvocationRequest(
        tool_name="browser",
        parameters={"action": "navigate", "url": "https://example.com"},
    )
    res = await tool.execute(req)

    assert res.success
    assert len(captured_headless) == 1
    assert captured_headless[0] is False


@pytest.mark.asyncio
async def test_verification_required_handling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test 17: Verification / CAPTCHA detection on security pages."""
    session_mgr = BrowserSessionManager()
    tool = BrowserTool(session_manager=session_mgr)

    mock_page = AsyncMock()
    mock_page.is_closed.return_value = False
    mock_page.url = "https://www.google.com/sorry/index?continue=https://google.com"
    mock_page.title = AsyncMock(return_value="Google CAPTCHA")
    mock_page.evaluate = AsyncMock(return_value="Unusual traffic from your computer network")
    mock_page.query_selector_all = AsyncMock(return_value=[])

    async def mock_get_page(headless: bool = False) -> AsyncMock:
        return mock_page

    monkeypatch.setattr(session_mgr, "get_or_create_page", mock_get_page)

    req = ToolInvocationRequest(
        tool_name="browser",
        parameters={"action": "navigate", "url": "https://google.com"},
    )
    res = await tool.execute(req)

    assert res.success
    assert "VERIFICATION_REQUIRED" in str(res.output)


@pytest.mark.asyncio
async def test_screenshot_session_persistence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test 18: Screenshot action preserves visible browser session."""
    session_mgr = BrowserSessionManager()
    tool = BrowserTool(session_manager=session_mgr)

    mock_page = AsyncMock()
    mock_page.is_closed.return_value = False
    mock_page.url = "https://example.com"
    mock_page.title = AsyncMock(return_value="Example")

    async def mock_get_page(headless: bool = False) -> AsyncMock:
        return mock_page

    monkeypatch.setattr(session_mgr, "get_or_create_page", mock_get_page)

    req = ToolInvocationRequest(tool_name="browser", parameters={"action": "screenshot"})
    res = await tool.execute(req)

    assert res.success
    assert "SCREENSHOT_CAPTURED" in str(res.output)
    # Session must remain open and not closed
    assert not mock_page.close.called

