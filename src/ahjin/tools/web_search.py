"""WebSearchTool — Live web search and information retrieval."""

import os
import time
import urllib.parse
from html.parser import HTMLParser
from typing import Any, cast

import httpx
import structlog

from ahjin.core.errors import AhjinError, ErrorCategory
from ahjin.tools.base import BaseTool, ToolInvocationRequest, ToolInvocationResult

logger = structlog.get_logger()

_DEFAULT_MAX_RESULTS = 5
_MAX_RESULTS_CAP = 10
_DEFAULT_TIMEOUT_SEC = 10.0


class _DDGLiteParser(HTMLParser):
    """HTML parser for DuckDuckGo Lite search results page."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self.current_link: str | None = None
        self.in_link = False
        self.in_snippet = False
        self.title_buf: list[str] = []
        self.snippet_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = dict(attrs)
        cls = attr_dict.get("class") or ""
        if tag == "a" and "result-link" in cls:
            self.current_link = attr_dict.get("href") or ""
            self.in_link = True
            self.title_buf = []
        elif tag == "td" and "result-snippet" in cls:
            self.in_snippet = True
            self.snippet_buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.in_link:
            self.in_link = False
        elif tag == "td" and self.in_snippet:
            self.in_snippet = False
            if self.current_link:
                title = "".join(self.title_buf).strip()
                snippet = "".join(self.snippet_buf).strip()
                domain = urllib.parse.urlparse(self.current_link).netloc
                if title:
                    self.results.append({
                        "title": title,
                        "url": self.current_link,
                        "domain": domain,
                        "snippet": snippet,
                    })
                self.current_link = None

    def handle_data(self, data: str) -> None:
        if self.in_link:
            self.title_buf.append(data)
        elif self.in_snippet:
            self.snippet_buf.append(data)


class WebSearchTool(BaseTool):
    """Tool for performing live web searches and returning grounded search results."""

    @property
    def tool_name(self) -> str:
        return "web_search"

    async def execute(self, request: ToolInvocationRequest) -> ToolInvocationResult:
        t0 = time.monotonic()
        query = str(request.parameters.get("query", "")).strip()

        if not query:
            latency_ms = (time.monotonic() - t0) * 1000.0
            return ToolInvocationResult(
                invocation_id=request.invocation_id,
                success=False,
                output=(
                    "[WEB SEARCH RESULTS]\n"
                    "Status: Error - Empty search query.\n"
                    "[/WEB SEARCH RESULTS]"
                ),
                error=AhjinError(
                    code="EMPTY_QUERY",
                    message="No search query provided.",
                    category=ErrorCategory.VALIDATION,
                ),
                latency_ms=latency_ms,
            )

        recency_days = request.parameters.get("recency_days")
        max_results_param = request.parameters.get("max_results")
        max_results = _DEFAULT_MAX_RESULTS
        if isinstance(max_results_param, int) and max_results_param > 0:
            max_results = min(max_results_param, _MAX_RESULTS_CAP)

        # 1. Try API search if key present, else fallback to DDG Lite
        tavily_key = os.environ.get("TAVILY_API_KEY")
        serper_key = os.environ.get("SERPER_API_KEY")

        results: list[dict[str, str]] = []
        search_error: str | None = None

        if tavily_key:
            results, search_error = await self._search_tavily(query, tavily_key, max_results)
        elif serper_key:
            results, search_error = await self._search_serper(query, serper_key, max_results)

        if not results and not tavily_key and not serper_key:
            results, search_error = await self._search_ddg_lite(query, max_results)

        latency_ms = (time.monotonic() - t0) * 1000.0

        if search_error and not results:
            output_text = (
                f"[WEB SEARCH RESULTS]\n"
                f"Query: \"{query}\"\n"
                f"Status: Failed - {search_error}\n"
                f"[/WEB SEARCH RESULTS]"
            )
            return ToolInvocationResult(
                invocation_id=request.invocation_id,
                success=False,
                output=output_text,
                error=AhjinError(
                    code="SEARCH_FAILED",
                    message=search_error,
                    category=ErrorCategory.TOOL,
                ),
                latency_ms=latency_ms,
            )

        if not results:
            output_text = (
                f"[WEB SEARCH RESULTS]\n"
                f"Query: \"{query}\"\n"
                f"Status: No relevant results found on the web.\n"
                f"[/WEB SEARCH RESULTS]"
            )
            return ToolInvocationResult(
                invocation_id=request.invocation_id,
                success=True,
                output=output_text,
                latency_ms=latency_ms,
            )

        formatted_lines = [
            "[WEB SEARCH RESULTS]",
            f"Query: \"{query}\"",
            f"Results count: {len(results)}",
        ]
        if isinstance(recency_days, int) and recency_days > 0:
            formatted_lines.append(f"Recency constraint: last {recency_days} days")

        formatted_lines.append("")

        for i, item in enumerate(results[:max_results], 1):
            formatted_lines.append(f"{i}. Title: {item.get('title', 'N/A')}")
            formatted_lines.append(f"   URL: {item.get('url', 'N/A')}")
            formatted_lines.append(f"   Domain: {item.get('domain', 'N/A')}")
            formatted_lines.append(f"   Snippet: {item.get('snippet', 'N/A')}")
            formatted_lines.append("")

        formatted_lines.append("[/WEB SEARCH RESULTS]")
        output_text = "\n".join(formatted_lines)

        return ToolInvocationResult(
            invocation_id=request.invocation_id,
            success=True,
            output=output_text,
            latency_ms=latency_ms,
        )

    async def _search_ddg_lite(
        self, query: str, max_results: int
    ) -> tuple[list[dict[str, str]], str | None]:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
        try:
            async with httpx.AsyncClient(
                headers=headers, follow_redirects=True, timeout=_DEFAULT_TIMEOUT_SEC
            ) as client:
                resp = await client.post(
                    "https://lite.duckduckgo.com/lite/",
                    data={"q": query},
                )
                if resp.status_code != 200:
                    return [], f"DuckDuckGo returned HTTP status {resp.status_code}"

                parser = _DDGLiteParser()
                parser.feed(resp.text)
                return parser.results[:max_results], None
        except Exception as exc:
            logger.warning("DuckDuckGo Lite search failed", query=query, error=str(exc))
            return [], f"Network error during web search: {exc}"

    async def _search_tavily(
        self, query: str, api_key: str, max_results: int
    ) -> tuple[list[dict[str, str]], str | None]:
        try:
            async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT_SEC) as client:
                resp = await client.post(
                    "https://api.tavily.com/search",
                    json={
                        "api_key": api_key,
                        "query": query,
                        "max_results": max_results,
                    },
                )
                if resp.status_code != 200:
                    return [], f"Tavily returned HTTP {resp.status_code}"
                data_obj: object = resp.json()
                if not isinstance(data_obj, dict):
                    return [], "Invalid response format from Tavily"
                data_dict = cast(dict[str, Any], data_obj)
                res_list = data_dict.get("results", [])
                results: list[dict[str, str]] = []
                if isinstance(res_list, list):
                    for item in cast(list[Any], res_list):
                        if isinstance(item, dict):
                            item_dict = cast(dict[str, Any], item)
                            url = str(item_dict.get("url", ""))
                            results.append({
                                "title": str(item_dict.get("title", "")),
                                "url": url,
                                "domain": urllib.parse.urlparse(url).netloc,
                                "snippet": str(item_dict.get("content", "")),
                            })
                return results, None
        except Exception as exc:
            logger.warning("Tavily search failed", query=query, error=str(exc))
            return [], f"Tavily search failed: {exc}"

    async def _search_serper(
        self, query: str, api_key: str, max_results: int
    ) -> tuple[list[dict[str, str]], str | None]:
        try:
            async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT_SEC) as client:
                resp = await client.post(
                    "https://google.serper.dev/search",
                    headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                    json={"q": query, "num": max_results},
                )
                if resp.status_code != 200:
                    return [], f"Serper returned HTTP {resp.status_code}"
                data_obj: object = resp.json()
                if not isinstance(data_obj, dict):
                    return [], "Invalid response format from Serper"
                data_dict = cast(dict[str, Any], data_obj)
                res_list = data_dict.get("organic", [])
                results: list[dict[str, str]] = []
                if isinstance(res_list, list):
                    for item in cast(list[Any], res_list):
                        if isinstance(item, dict):
                            item_dict = cast(dict[str, Any], item)
                            url = str(item_dict.get("link", ""))
                            results.append({
                                "title": str(item_dict.get("title", "")),
                                "url": url,
                                "domain": urllib.parse.urlparse(url).netloc,
                                "snippet": str(item_dict.get("snippet", "")),
                            })
                return results, None
        except Exception as exc:
            logger.warning("Serper search failed", query=query, error=str(exc))
            return [], f"Serper search failed: {exc}"
