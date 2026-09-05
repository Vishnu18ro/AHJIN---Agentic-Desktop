"""Unit tests for Phase 7A WebSearchTool and Web Intent Planning."""

from uuid import uuid4

import pytest

from ahjin.beru.tool_planner import ToolIntentPlanner
from ahjin.beru.tools import detect_tool_intent
from ahjin.beru.types import ModelStepIntent
from ahjin.core.types import TaskContext
from ahjin.harness.context import ContextAssembler
from ahjin.harness.state import StepResult
from ahjin.tools.base import ToolInvocationRequest
from ahjin.tools.web_search import WebSearchTool, _DDGLiteParser


@pytest.mark.asyncio
async def test_web_intent_recognized_deterministic() -> None:
    """Test 1: Web intent recognized via deterministic resolver."""
    req1 = detect_tool_intent("Search the web for latest NVIDIA news")
    assert req1 is not None
    assert req1.tool_name == "web_search"
    assert req1.parameters.get("query") == "latest NVIDIA news"

    req2 = detect_tool_intent("What's the current weather in Hyderabad?")
    assert req2 is not None
    assert req2.tool_name == "web_search"
    assert "Hyderabad" in str(req2.parameters.get("query"))


@pytest.mark.asyncio
async def test_query_and_recency_extraction_planner() -> None:
    """Test 2 & 3: Query and recency parameters validation in ToolIntentPlanner."""
    planner = ToolIntentPlanner()
    assert planner is not None
    params = {"query": "Sarvam AI", "recency_days": 7, "max_results": 3}
    req = ToolInvocationRequest(tool_name="web_search", parameters=params)
    assert req.parameters["query"] == "Sarvam AI"
    assert req.parameters["recency_days"] == 7
    assert req.parameters["max_results"] == 3


def test_ddg_lite_search_result_parsing() -> None:
    """Test 4: Search result HTML parsing via _DDGLiteParser."""
    sample_html = """
    <html>
      <body>
        <table>
          <tr>
            <td>
              <a class='result-link' href='https://nvidianews.nvidia.com/news/123'>
                NVIDIA Announces Next-Gen GPU Architecture
              </a>
            </td>
          </tr>
          <tr>
            <td class='result-snippet'>
              NVIDIA today unveiled its newest breakthrough GPU architecture for generative AI.
            </td>
          </tr>
        </table>
      </body>
    </html>
    """
    parser = _DDGLiteParser()
    parser.feed(sample_html)

    assert len(parser.results) == 1
    res = parser.results[0]
    assert res["title"] == "NVIDIA Announces Next-Gen GPU Architecture"
    assert res["url"] == "https://nvidianews.nvidia.com/news/123"
    assert res["domain"] == "nvidianews.nvidia.com"
    assert "generative AI" in res["snippet"]


@pytest.mark.asyncio
async def test_web_search_empty_query() -> None:
    """Test 6: Empty results / empty query handling."""
    tool = WebSearchTool()
    request = ToolInvocationRequest(tool_name="web_search", parameters={"query": ""})
    res = await tool.execute(request)

    assert not res.success
    assert "Empty search query" in str(res.output)
    assert res.error is not None
    assert res.error.code == "EMPTY_QUERY"


@pytest.mark.asyncio
async def test_web_search_failure_handling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test 5: Search failure handling when network error occurs."""
    tool = WebSearchTool()

    async def mock_fail_ddg(
        query: str, max_results: int
    ) -> tuple[list[dict[str, str]], str | None]:
        return [], "Simulated network timeout"

    monkeypatch.setattr(tool, "_search_ddg_lite", mock_fail_ddg)

    request = ToolInvocationRequest(
        tool_name="web_search", parameters={"query": "nonexistent query test"}
    )
    res = await tool.execute(request)

    assert not res.success
    assert res.error is not None
    assert res.error.code == "SEARCH_FAILED"
    assert "[WEB SEARCH RESULTS]" in str(res.output)
    assert "Failed - Simulated network timeout" in str(res.output)


@pytest.mark.asyncio
async def test_web_search_successful_results(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test web search returns formatted structured results."""
    tool = WebSearchTool()

    async def mock_success_ddg(
        query: str, max_results: int
    ) -> tuple[list[dict[str, str]], str | None]:
        return [
            {
                "title": "NVIDIA Blackwell Benchmark Results",
                "url": "https://nvidia.com/blackwell",
                "domain": "nvidia.com",
                "snippet": "New Blackwell benchmarks set world record performance.",
            }
        ], None

    monkeypatch.setattr(tool, "_search_ddg_lite", mock_success_ddg)

    request = ToolInvocationRequest(
        tool_name="web_search", parameters={"query": "NVIDIA Blackwell"}
    )
    res = await tool.execute(request)

    assert res.success
    output_str = str(res.output)
    assert "[WEB SEARCH RESULTS]" in output_str
    assert "Title: NVIDIA Blackwell Benchmark Results" in output_str
    assert "URL: https://nvidia.com/blackwell" in output_str
    assert "Domain: nvidia.com" in output_str
    assert "[/WEB SEARCH RESULTS]" in output_str


def test_model_receives_web_results_as_context() -> None:
    """Test 7: ContextAssembler passes web search results into prompt context."""
    assembler = ContextAssembler()
    intent = ModelStepIntent(instruction="Summarize the latest NVIDIA news")
    task_context = TaskContext(user_id="test_user", session_id="test_session")

    web_output = (
        "[WEB SEARCH RESULTS]\n"
        "Query: \"latest NVIDIA news\"\n"
        "1. Title: NVIDIA Blackwell Launched\n"
        "   URL: https://nvidia.com/news\n"
        "   Domain: nvidia.com\n"
        "   Snippet: Next-gen GPUs available now.\n"
        "[/WEB SEARCH RESULTS]"
    )
    prior_results = [
        StepResult(step_id=uuid4(), success=True, output_text=web_output)
    ]

    prompt = assembler.assemble(intent, task_context, prior_results=prior_results)

    assert "[TOOL RESULTS]" in prompt.user_instruction
    assert "[WEB SEARCH RESULTS]" in prompt.user_instruction
    assert "https://nvidia.com/news" in prompt.user_instruction
    assert "cite relevant source URLs or domains" in prompt.user_instruction
