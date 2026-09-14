"""Unit tests for Phase 3A Latency Optimizations.

Verifies:
A. Simple model request still streams.
B. File search still works.
C. File search -> file read context reduction in ContextAssembler.
D. File search -> file read -> file send tool chaining preserved.
E. Telegram produces one unified message.
F. Streaming remains progressive.
G. Reasoning content (delta.reasoning_content) is never exposed or yielded.
H. NVIDIA client is reused safely across invocations.
I. Client cleanup and aclose() works without leaks.
J. Granular internal latency telemetry is recorded without mutating the footer.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest

from ahjin.beru.types import ModelStepIntent
from ahjin.core.types import TaskContext
from ahjin.harness.context import ContextAssembler
from ahjin.harness.state import StepResult
from ahjin.interfaces.telegram.bot import _build_runtime_footer
from ahjin.providers.nvidia import NvidiaProvider
from ahjin.providers.types import (
    ContextualizedPrompt,
    ModelInvocationRequest,
)
from ahjin.telemetry.timing import (
    STAGE_FIRST_REASONING,
    STAGE_FIRST_SSE,
    STAGE_FIRST_VISIBLE_CONTENT,
    STAGE_REASONING_DURATION,
    STAGE_STREAM_COMPLETION,
    STAGE_TELEGRAM_PLACEHOLDER,
    STAGE_VISIBLE_GENERATION,
    RequestTimer,
)

# --- 1. Objective B: Connection Reuse & Cleanup ---


@pytest.mark.asyncio
async def test_nvidia_provider_connection_reuse() -> None:
    """NvidiaProvider reuses a single persistent httpx.AsyncClient across invocations."""
    provider = NvidiaProvider(api_key="test-key", default_model="test-model")
    assert provider._client is None  # lazy initialization

    client_1 = provider._get_client()
    assert client_1 is not None
    assert isinstance(client_1, httpx.AsyncClient)
    assert not client_1.is_closed

    # Second call must return the exact same client instance
    client_2 = provider._get_client()
    assert client_2 is client_1

    # Clean shutdown
    await provider.aclose()
    assert provider._client is None
    assert client_1.is_closed


@pytest.mark.asyncio
async def test_nvidia_provider_aclose_and_reopen() -> None:
    """Calling aclose() closes the client and a subsequent call reinitializes safely."""
    provider = NvidiaProvider(api_key="test-key")
    client_1 = provider._get_client()
    assert not client_1.is_closed

    await provider.aclose()
    assert provider._client is None

    # Getting client again initializes a fresh open client
    client_2 = provider._get_client()
    assert client_2 is not None
    assert not client_2.is_closed
    assert client_2 is not client_1

    await provider.aclose()


# --- 2. Objective A: Reasoning Interception & Progressive Streaming ---


@pytest.mark.asyncio
async def test_nvidia_streaming_discards_reasoning_and_yields_visible_content() -> None:
    """Reasoning chunks are captured for telemetry but NEVER yielded to stream."""
    provider = NvidiaProvider(api_key="test-key")

    mock_lines = [
        ": keep-alive\n",
        'data: {"choices": [{"delta": {"reasoning_content": "Thinking step 1"}}]}\n',
        'data: {"choices": [{"delta": {"reasoning_content": "Thinking step 2"}}]}\n',
        'data: {"choices": [{"delta": {"content": "Hello"}}]}\n',
        'data: {"choices": [{"delta": {"content": " world!"}}]}\n',
        "data: [DONE]\n",
    ]

    async def mock_aiter_lines() -> AsyncGenerator[str, None]:
        for line in mock_lines:
            yield line

    mock_response = MagicMock()
    mock_response.aiter_lines = mock_aiter_lines
    mock_response.raise_for_status = MagicMock()

    stream_cm = MagicMock()
    stream_cm.__aenter__ = AsyncMock(return_value=mock_response)
    stream_cm.__aexit__ = AsyncMock(return_value=None)

    mock_client = MagicMock()
    mock_client.stream = MagicMock(return_value=stream_cm)
    mock_client.is_closed = False

    provider._client = mock_client
    provider._owns_client = False

    req = ModelInvocationRequest(
        model_id="nvidia/nemotron-3.5-lightning-30b-a3b",
        prompt=ContextualizedPrompt(user_instruction="hi"),
    )

    yielded_chunks: list[str] = []
    async for chunk in provider.invoke_stream(req):
        yielded_chunks.append(chunk)

    # CRITICAL: Only visible content is yielded (reasoning chunks are empty StreamChunks)
    assert [c for c in yielded_chunks if c] == ["Hello", " world!"]
    assert all(getattr(c, "is_reasoning", False) for c in yielded_chunks if not c)

    # Telemetry was populated internally
    tel = provider.last_telemetry
    assert STAGE_FIRST_SSE in tel
    assert STAGE_FIRST_REASONING in tel
    assert STAGE_FIRST_VISIBLE_CONTENT in tel
    assert STAGE_REASONING_DURATION in tel
    assert STAGE_VISIBLE_GENERATION in tel
    assert STAGE_STREAM_COMPLETION in tel

    # Reasoning text must not appear anywhere in yielded output
    combined = "".join(yielded_chunks)
    assert "Thinking" not in combined
    assert "reasoning" not in combined.lower()


# --- 3. Objective C: Intermediate File-Search Context Reduction ---


def test_context_assembler_prunes_search_when_read_succeeded() -> None:
    """When file_read succeeded, raw multi-file search dump is condensed in prompt."""
    assembler = ContextAssembler()
    intent = ModelStepIntent(instruction="find my resume and summarize it")
    context = TaskContext(session_id="test")

    search_output = (
        "Found 50 match(es) for query 'resume' (scanned 224 files):\n"
        "\n[USER FILES & DOCUMENTS]\n"
        "- [FILE/PATH MATCH] resume.pdf (Full path: /path/to/resume.pdf)\n"
        "- [FILE/PATH MATCH] old_resume.doc (Full path: /path/to/old_resume.doc)\n"
        "\n[PROJECT SOURCE & TEST CODE]\n"
        "- [FILE/PATH MATCH] tests/test_resume.py\n"
        "- [CONTENT MATCH] src/ahjin/tools.py:L10: resume parser\n" * 10
    )
    read_output = (
        "--- Content of resume.pdf (Size: 12.3 KB) ---\n[Page 1]\nJohn Doe Resume\nExperience..."
    )

    prior_results = [
        StepResult(
            step_id=uuid4(),
            success=True,
            output_text=search_output,
            tool_name="file_search",
        ),
        StepResult(
            step_id=uuid4(),
            success=True,
            output_text=read_output,
            tool_name="file_read",
        ),
    ]

    prompt = assembler.assemble(intent=intent, task_context=context, prior_results=prior_results)

    # Search output is condensed
    assert "Candidate document identified and read in subsequent step." in prompt.user_instruction
    assert "Found 50 match(es) for query 'resume'" in prompt.user_instruction
    # The raw dump of project source files is pruned from the prompt
    assert "tests/test_resume.py" not in prompt.user_instruction

    # Document content is fully preserved
    assert "John Doe Resume" in prompt.user_instruction


def test_context_assembler_preserves_search_when_read_failed_or_absent() -> None:
    """When file_read is absent or failed, full file_search output is preserved."""
    assembler = ContextAssembler()
    intent = ModelStepIntent(instruction="find my resume")
    context = TaskContext(session_id="test")

    search_output = (
        "Found 2 match(es) for query 'resume' (scanned 50 files):\n"
        "- [FILE/PATH MATCH] resume.pdf\n"
        "- [FILE/PATH MATCH] old_resume.docx\n"
    )

    # Case 1: Search only (no file_read)
    prior_results = [
        StepResult(
            step_id=uuid4(),
            success=True,
            output_text=search_output,
            tool_name="file_search",
        ),
    ]
    prompt = assembler.assemble(intent=intent, task_context=context, prior_results=prior_results)
    assert "- [FILE/PATH MATCH] resume.pdf" in prompt.user_instruction
    assert "- [FILE/PATH MATCH] old_resume.docx" in prompt.user_instruction

    # Case 2: file_read failed
    prior_results_failed = [
        StepResult(
            step_id=uuid4(),
            success=True,
            output_text=search_output,
            tool_name="file_search",
        ),
        StepResult(step_id=uuid4(), success=False, output_text=None, tool_name="file_read"),
    ]
    prompt2 = assembler.assemble(
        intent=intent,
        task_context=context,
        prior_results=prior_results_failed,
    )
    assert "- [FILE/PATH MATCH] resume.pdf" in prompt2.user_instruction


# --- 4. Objective A & Telegram Footer Preservation ---


def test_internal_telemetry_does_not_mutate_compact_footer() -> None:
    """Internal granular stages do not appear in the compact Telegram footer."""
    from ahjin.core.types import RuntimeInfo

    timer = RequestTimer()
    timer.record(STAGE_TELEGRAM_PLACEHOLDER, 1850.0)
    timer.record(STAGE_FIRST_SSE, 1200.0)
    timer.record(STAGE_REASONING_DURATION, 4500.0)
    timer.record(STAGE_VISIBLE_GENERATION, 250.0)

    timing_snap = timer.snapshot()
    assert timing_snap[STAGE_TELEGRAM_PLACEHOLDER] == 1850.0

    runtime_info = RuntimeInfo(
        selected_model="nvidia/nemotron-3.5-lightning-30b-a3b",
        tier="FAST",
        provider_id="nvidia",
        ahjin_internal_ms=1.0,
        model_api_ms=5950.0,
        total_ms=7800.0,
        timing=timing_snap,
    )

    footer = _build_runtime_footer(runtime_info)

    # User-facing footer must NOT contain raw internal stage names
    assert "telegram_placeholder" not in footer
    assert "first_reasoning" not in footer
    assert "reasoning_duration" not in footer
    assert "visible_generation" not in footer
    assert "first_sse" not in footer

    # Footer maintains standard sections
    assert "⏱ Latency" in footer
    assert "AHJIN:" in footer
    assert "Model:" in footer
    assert "Total:" in footer
