"""Unit tests for response streaming in AHJIN 2.0.

Tests coverage:
1. Provider: SSE chunk parsing, [DONE], keepalives, multiple chunks, malformed SSE safety
2. Gateway: invoke_stream delegates to resolved provider
3. Harness: run_stream yields chunks, accumulates response, preserves normal routing
4. Dispatcher: dispatch_stream yields chunks and final TaskResult
5. Telegram: placeholder sent, throttled edits, final response, pre-first-token fallback
6. LocalExecutor: Qwen streaming timeout behavior
"""

import asyncio
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from ahjin.beru.types import (
    CapabilityRequirements,
    ExecutionPlan,
    ExecutionStrategy,
    ModelStepIntent,
    PlanStep,
    StepType,
)
from ahjin.core.dispatcher import TaskDispatcher
from ahjin.core.types import TaskContext, TaskRequest, UserIntent
from ahjin.harness.gateway import ProviderGateway
from ahjin.harness.runner import HarnessRunner
from ahjin.interfaces.telegram.bot import TelegramAdapter
from ahjin.local.executor import LocalExecutor, LocalRoutingPolicy
from ahjin.models.catalog import ModelCatalog
from ahjin.models.router import ModelRouter
from ahjin.models.types import ModelCapabilities, ModelDescriptor, ModelTier
from ahjin.providers.base import BaseModelProvider
from ahjin.providers.nvidia import NvidiaProvider
from ahjin.providers.registry import ProviderRegistry
from ahjin.providers.types import (
    ContextualizedPrompt,
    ModelInvocationRequest,
    ModelInvocationResponse,
)

# --- Mock Provider for Testing ---

class MockStreamProvider(BaseModelProvider):
    def __init__(self, provider_id: str = "mock_stream", chunks: list[str] | None = None) -> None:
        self._provider_id = provider_id
        self._chunks = chunks or ["Hello ", "world", "!"]

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def get_default_model_id(self) -> str:
        return "mock-stream-model"

    async def invoke(self, request: ModelInvocationRequest) -> ModelInvocationResponse:
        return ModelInvocationResponse(
            invocation_id=request.invocation_id,
            content="".join(self._chunks),
            provider_id=self.provider_id,
            model_id=request.model_id,
        )

    async def invoke_stream(
        self, request: ModelInvocationRequest
    ) -> AsyncGenerator[str, None]:
        for chunk in self._chunks:
            yield chunk


def _build_mock_gateway(provider: BaseModelProvider) -> ProviderGateway:
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


def _make_prompt(user_text: str = "Hello") -> ContextualizedPrompt:
    return ContextualizedPrompt(user_instruction=user_text)


# --- 1. Provider SSE Parsing Tests ---

@pytest.mark.asyncio
async def test_provider_sse_parsing() -> None:
    """Test NvidiaProvider SSE parsing for normal chunks, [DONE], keepalive, and malformed lines."""
    provider = NvidiaProvider(api_key="test-key")

    mock_lines = [
        b": keep-alive\n",
        b"\n",
        b'data: {"choices": [{"delta": {"content": "Hello"}}]}\n',
        b'data: {"choices": [{"delta": {"content": " "}}]}\n',
        b'data: {"choices": [{"delta": {"content": "World"}}]}\n',
        b'data: {"choices": [{"delta": {}}]}\n',  # empty delta
        b"data: invalid json syntax\n",  # malformed SSE
        b"data: [DONE]\n",
    ]

    async def mock_aiter_lines() -> AsyncGenerator[str, None]:
        for line in mock_lines:
            yield line.decode("utf-8")

    mock_response = MagicMock()
    mock_response.aiter_lines = mock_aiter_lines
    mock_response.raise_for_status = MagicMock()

    stream_cm = MagicMock()
    stream_cm.__aenter__ = AsyncMock(return_value=mock_response)
    stream_cm.__aexit__ = AsyncMock(return_value=None)

    mock_client = MagicMock()
    mock_client.stream = MagicMock(return_value=stream_cm)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    req = ModelInvocationRequest(model_id="nvidia/test", prompt=_make_prompt("hi"))
    with patch("httpx.AsyncClient", return_value=mock_client):
        chunks = []
        async for chunk in provider.invoke_stream(req):
            chunks.append(chunk)

    assert chunks == ["Hello", " ", "World"]


# --- 2. Gateway Streaming Tests ---

@pytest.mark.asyncio
async def test_gateway_invoke_stream() -> None:
    """Gateway invoke_stream delegates to resolved provider and yields chunks."""
    provider = MockStreamProvider(chunks=["Part1", "Part2"])
    gateway = _build_mock_gateway(provider)

    streamed = []
    async for chunk, _sel in gateway.invoke_stream(_make_prompt("test"), CapabilityRequirements()):
        streamed.append(chunk)

    assert streamed == ["Part1", "Part2"]


# --- 3. Harness Streaming Tests ---

@pytest.mark.asyncio
async def test_harness_runner_run_stream() -> None:
    """HarnessRunner run_stream yields chunks and final result."""
    provider = MockStreamProvider(chunks=["A", "B", "C"])
    gateway = _build_mock_gateway(provider)
    runner = HarnessRunner(gateway=gateway)

    plan = ExecutionPlan(
        task_id=uuid4(),
        correlation_id=uuid4(),
        steps=[
            PlanStep(
                step_type=StepType.MODEL_INVOCATION,
                model_intent=ModelStepIntent(
                    instruction="Hello",
                    execution_strategy=ExecutionStrategy(
                        capability_requirements=CapabilityRequirements(),
                        preferred_tier="FAST",
                    ),
                ),
            )
        ],
    )
    context = TaskContext(session_id="test")

    chunks = []
    final_result = None
    async for chunk, result in runner.run_stream(plan, context):
        if chunk:
            chunks.append(chunk)
        if result:
            final_result = result

    assert chunks == ["A", "B", "C"]
    assert final_result is not None
    assert final_result.success is True
    assert final_result.output_text == "ABC"


# --- 4. Dispatcher Streaming Tests ---

@pytest.mark.asyncio
async def test_dispatcher_dispatch_stream() -> None:
    """TaskDispatcher dispatch_stream yields streamed chunks and completed TaskResult."""
    provider = MockStreamProvider(chunks=["Chunk1 ", "Chunk2"])
    gateway = _build_mock_gateway(provider)
    runner = HarnessRunner(gateway=gateway)
    dispatcher = TaskDispatcher(runner=runner)

    request = TaskRequest(
        intent=UserIntent(primary_text="Say hi"),
        context=TaskContext(session_id="test-session"),
    )
    chunks = []
    final_result = None
    async for chunk, result in dispatcher.dispatch_stream(request):
        if chunk:
            chunks.append(chunk)
        if result:
            final_result = result

    assert chunks == ["Chunk1 ", "Chunk2"]
    assert final_result is not None
    assert final_result.success is True
    assert final_result.output_text == "Chunk1 Chunk2"


# --- 5. Telegram Adapter Streaming Tests ---

@pytest.mark.asyncio
async def test_telegram_adapter_streaming_flow() -> None:
    """TelegramAdapter sends placeholder, edits message, and completes final text."""
    provider = MockStreamProvider(chunks=["Hello ", "world"])
    gateway = _build_mock_gateway(provider)
    runner = HarnessRunner(gateway=gateway)
    dispatcher = TaskDispatcher(runner=runner)

    adapter = TelegramAdapter(token="fake", dispatcher=dispatcher)

    # Mock Telegram Update & Message
    placeholder_msg = AsyncMock()
    placeholder_msg.message_id = 100
    placeholder_msg.edit_text = AsyncMock()

    update = MagicMock()
    update.message = AsyncMock()
    update.message.chat_id = 123
    update.message.text = "Hello bot"
    update.message.reply_text = AsyncMock(return_value=placeholder_msg)

    context = MagicMock()

    await adapter._handle_message(update, context)

    # Verify placeholder was sent
    update.message.reply_text.assert_called_with("Thinking...")

    # Verify placeholder.edit_text was called for final output
    assert placeholder_msg.edit_text.called
    final_edit_call = placeholder_msg.edit_text.call_args_list[-1][0][0]
    assert "Hello world" in final_edit_call
    assert "AHJIN Runtime" in final_edit_call


@pytest.mark.asyncio
async def test_telegram_adapter_pre_first_token_fallback() -> None:
    """TelegramAdapter falls back to dispatch() if stream fails before first token."""
    dispatcher = MagicMock(spec=TaskDispatcher)

    async def failing_stream(req):
        raise RuntimeError("Cloud connection failed before token")
        yield  # make it a generator  # type: ignore[unreachable]

    dispatcher.dispatch_stream = failing_stream

    fallback_result = MagicMock()
    fallback_result.runtime_info = None
    dispatcher.dispatch = AsyncMock(return_value=fallback_result)

    adapter = TelegramAdapter(token="fake", dispatcher=dispatcher)

    placeholder_msg = AsyncMock()
    update = MagicMock()
    update.message = AsyncMock()
    update.message.chat_id = 123
    update.message.text = "Hello bot"
    update.message.reply_text = AsyncMock(return_value=placeholder_msg)

    with patch(
        "ahjin.interfaces.telegram.bot.TelegramMapper.to_telegram_response",
        return_value="Fallback answer",
    ):
        await adapter._handle_message(update, MagicMock())

    # Should have called dispatch() fallback
    dispatcher.dispatch.assert_called_once()
    assert placeholder_msg.edit_text.called
    assert "Fallback answer" in placeholder_msg.edit_text.call_args_list[-1][0][0]


# --- 6. Local Executor Streaming & Qwen Timeout Tests ---

@pytest.mark.asyncio
async def test_local_executor_qwen_streaming_timeout_fallback() -> None:
    """LocalExecutor Qwen streaming timeout discards partial output and falls back to Gemma."""
    policy = LocalRoutingPolicy()

    # Mock Ollama provider
    ollama_provider = MagicMock()

    async def slow_qwen_stream(req: ModelInvocationRequest) -> AsyncGenerator[str, None]:
        yield "Qwen starting..."
        await asyncio.sleep(0.5)  # Simulate slow response
        yield "Qwen finished"

    async def fast_gemma_stream(req: ModelInvocationRequest) -> AsyncGenerator[str, None]:
        yield "Gemma fallback answer"

    def mock_invoke_stream(req: ModelInvocationRequest) -> AsyncGenerator[str, None]:
        if "qwen" in req.model_id:
            return slow_qwen_stream(req)
        return fast_gemma_stream(req)

    ollama_provider.invoke_stream = mock_invoke_stream

    executor = LocalExecutor(
        policy=policy, provider=ollama_provider, qwen_timeout_seconds=0.1
    )

    strategy = ExecutionStrategy(preferred_tier="HEAVY")
    prompt = _make_prompt("Hard math question")

    chunks = []
    async for chunk, _res in executor.invoke_stream(prompt=prompt, strategy=strategy):
        chunks.append(chunk)

    full_output = "".join(chunks)
    assert "Qwen 3 8B was taking longer than" in full_output
    assert "Gemma fallback answer" in full_output
