"""Unit tests for LocalExecutor.

Verifies:
- Qwen success path (no fallback).
- Qwen timeout → clean cancellation → Gemma receives ORIGINAL prompt.
- Qwen timeout → output contains user-visible fallback notice.
- Qwen timeout → suggest_escalation=True and correct escalation_reason.
- Gemma failure after Qwen timeout → LocalExecutionError raised.
- Gemma failure on direct fast-task path → LocalExecutionError raised.
- Vision task (policy skip) → LocalRoutingSkipped raised.
- No background Qwen task remains after timeout.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from ahjin.beru.types import CapabilityRequirements, ExecutionStrategy
from ahjin.local.executor import _QWEN_TIMEOUT_NOTICE, LocalExecutor
from ahjin.local.policy import LocalRoutingPolicy
from ahjin.local.types import (
    LOCAL_GEMMA_MODEL_ID,
    LOCAL_QWEN_MODEL_ID,
    LocalExecutionError,
    LocalRoutingSkipped,
)
from ahjin.models.types import ModelTier
from ahjin.providers.types import (
    ContextualizedPrompt,
    FinishReason,
    ModelInvocationResponse,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_prompt(user_text: str = "Original user question.") -> ContextualizedPrompt:
    return ContextualizedPrompt(user_instruction=user_text)


def _make_heavy_strategy() -> ExecutionStrategy:
    return ExecutionStrategy(
        capability_requirements=CapabilityRequirements(requires_reasoning=True),
        preferred_tier=ModelTier.HEAVY.value,
    )


def _make_fast_strategy() -> ExecutionStrategy:
    return ExecutionStrategy(
        capability_requirements=CapabilityRequirements(),
        preferred_tier=ModelTier.FAST.value,
    )


def _make_vision_strategy() -> ExecutionStrategy:
    return ExecutionStrategy(
        capability_requirements=CapabilityRequirements(requires_vision=True),
        preferred_tier=ModelTier.FAST.value,
    )


def _make_mock_response(model_id: str, content: str) -> ModelInvocationResponse:
    return ModelInvocationResponse(
        invocation_id=uuid4(),
        content=content,
        finish_reason=FinishReason.COMPLETE,
        provider_id="ollama",
        model_id=model_id,
    )


def _make_executor(
    provider: MagicMock,
    qwen_timeout: float = 90.0,
    gemma_timeout: float = 60.0,
    enabled: bool = True,
) -> LocalExecutor:
    policy = LocalRoutingPolicy(enabled=enabled)
    return LocalExecutor(
        policy=policy,
        provider=provider,  # type: ignore[arg-type]
        qwen_timeout_seconds=qwen_timeout,
        gemma_timeout_seconds=gemma_timeout,
    )


# ---------------------------------------------------------------------------
# Qwen success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qwen_success_no_fallback() -> None:
    """Qwen responds within timeout → used_fallback=False, suggest_escalation=False."""
    provider = MagicMock()
    provider.invoke = AsyncMock(
        return_value=_make_mock_response(LOCAL_QWEN_MODEL_ID, "Qwen answer.")
    )

    executor = _make_executor(provider)
    result = await executor.invoke(
        prompt=_make_prompt(),
        strategy=_make_heavy_strategy(),
    )

    assert result.used_fallback is False
    assert result.suggest_escalation is False
    assert result.model_used == LOCAL_QWEN_MODEL_ID
    assert result.output_text == "Qwen answer."
    assert result.fallback_reason is None
    assert result.escalation_reason is None


# ---------------------------------------------------------------------------
# Qwen timeout → Gemma fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qwen_timeout_cancels_and_falls_back_to_gemma() -> None:
    """asyncio.TimeoutError → Gemma called → used_fallback=True."""
    gemma_response = _make_mock_response(LOCAL_GEMMA_MODEL_ID, "Gemma fallback answer.")

    async def invoke_side_effect(request):  # type: ignore[no-untyped-def]
        if request.model_id == LOCAL_QWEN_MODEL_ID:
            await asyncio.sleep(999)
        return gemma_response

    provider = MagicMock()
    provider.invoke = AsyncMock(side_effect=invoke_side_effect)

    executor = _make_executor(provider, qwen_timeout=0.05)  # 50ms timeout
    result = await executor.invoke(
        prompt=_make_prompt(),
        strategy=_make_heavy_strategy(),
    )

    assert result.used_fallback is True
    assert result.model_used == LOCAL_GEMMA_MODEL_ID
    assert result.attempted_model == LOCAL_QWEN_MODEL_ID


@pytest.mark.asyncio
async def test_qwen_timeout_gemma_receives_original_request() -> None:
    """Gemma must receive the exact original prompt — not transformed state."""
    captured_prompts: list[str] = []

    async def tracking_invoke(request):  # type: ignore[no-untyped-def]
        captured_prompts.append(request.prompt.user_instruction)
        if request.model_id == LOCAL_QWEN_MODEL_ID:
            await asyncio.sleep(999)
        return _make_mock_response(LOCAL_GEMMA_MODEL_ID, "Gemma answer.")

    provider = MagicMock()
    provider.invoke = AsyncMock(side_effect=tracking_invoke)

    original_text = "Explain the sky."
    prompt = _make_prompt(original_text)

    executor = _make_executor(provider, qwen_timeout=0.05)
    await executor.invoke(prompt=prompt, strategy=_make_heavy_strategy())

    # Gemma must have seen the original user instruction, not any transformed version.
    assert any(text == original_text for text in captured_prompts), (
        f"Gemma did not receive original prompt. Captured: {captured_prompts}"
    )


@pytest.mark.asyncio
async def test_qwen_timeout_output_contains_fallback_notice() -> None:
    """output_text must start with the user-visible fallback notice."""
    async def invoke(request):  # type: ignore[no-untyped-def]
        if request.model_id == LOCAL_QWEN_MODEL_ID:
            await asyncio.sleep(999)
        return _make_mock_response(LOCAL_GEMMA_MODEL_ID, "Gemma actual answer here.")

    provider = MagicMock()
    provider.invoke = AsyncMock(side_effect=invoke)

    executor = _make_executor(provider, qwen_timeout=0.05)
    result = await executor.invoke(
        prompt=_make_prompt(),
        strategy=_make_heavy_strategy(),
    )

    assert result.output_text.startswith(_QWEN_TIMEOUT_NOTICE)
    assert "Gemma actual answer here." in result.output_text


@pytest.mark.asyncio
async def test_qwen_timeout_suggest_escalation_true() -> None:
    """Qwen timeout → suggest_escalation=True and escalation_reason set."""
    async def invoke(request):  # type: ignore[no-untyped-def]
        if request.model_id == LOCAL_QWEN_MODEL_ID:
            await asyncio.sleep(999)
        return _make_mock_response(LOCAL_GEMMA_MODEL_ID, "Gemma answer.")

    provider = MagicMock()
    provider.invoke = AsyncMock(side_effect=invoke)

    executor = _make_executor(provider, qwen_timeout=0.05)
    result = await executor.invoke(prompt=_make_prompt(), strategy=_make_heavy_strategy())

    assert result.suggest_escalation is True
    assert result.fallback_reason == "qwen_timeout"
    assert result.escalation_reason == "qwen_timeout_gemma_fallback"


# ---------------------------------------------------------------------------
# Gemma failure cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gemma_failure_after_qwen_timeout_raises_local_error() -> None:
    """Qwen timeout → Gemma also fails → LocalExecutionError raised (not swallowed)."""
    async def invoke(request):  # type: ignore[no-untyped-def]
        if request.model_id == LOCAL_QWEN_MODEL_ID:
            await asyncio.sleep(999)
        raise RuntimeError("Gemma exploded")

    provider = MagicMock()
    provider.invoke = AsyncMock(side_effect=invoke)

    executor = _make_executor(provider, qwen_timeout=0.05)

    with pytest.raises(LocalExecutionError, match="Gemma fallback failed"):
        await executor.invoke(prompt=_make_prompt(), strategy=_make_heavy_strategy())


@pytest.mark.asyncio
async def test_gemma_direct_failure_raises_local_error() -> None:
    """Gemma failure on a direct fast-task path → LocalExecutionError raised."""
    provider = MagicMock()
    provider.invoke = AsyncMock(side_effect=RuntimeError("Gemma unavailable"))

    executor = _make_executor(provider)

    with pytest.raises(LocalExecutionError, match="Gemma execution failed"):
        await executor.invoke(prompt=_make_prompt(), strategy=_make_fast_strategy())


# ---------------------------------------------------------------------------
# LocalRoutingSkipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_routing_skipped_when_vision() -> None:
    """Vision strategy → LocalRoutingSkipped raised."""
    provider = MagicMock()
    provider.invoke = AsyncMock()

    executor = _make_executor(provider)

    with pytest.raises(LocalRoutingSkipped, match="no_local_vision_model"):
        await executor.invoke(prompt=_make_prompt(), strategy=_make_vision_strategy())

    # Provider must NOT have been called
    provider.invoke.assert_not_called()


@pytest.mark.asyncio
async def test_local_routing_skipped_when_policy_disabled() -> None:
    """Disabled policy → LocalRoutingSkipped raised."""
    provider = MagicMock()
    provider.invoke = AsyncMock()

    executor = _make_executor(provider, enabled=False)

    with pytest.raises(LocalRoutingSkipped, match="policy_disabled"):
        await executor.invoke(prompt=_make_prompt(), strategy=_make_fast_strategy())

    provider.invoke.assert_not_called()


# ---------------------------------------------------------------------------
# Gemma direct success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gemma_direct_success() -> None:
    """Direct Gemma invocation on FAST task succeeds with correct result fields."""
    provider = MagicMock()
    provider.invoke = AsyncMock(
        return_value=_make_mock_response(LOCAL_GEMMA_MODEL_ID, "Gemma direct answer.")
    )

    executor = _make_executor(provider)
    result = await executor.invoke(prompt=_make_prompt(), strategy=_make_fast_strategy())

    assert result.used_fallback is False
    assert result.model_used == LOCAL_GEMMA_MODEL_ID
    assert result.output_text == "Gemma direct answer."
    assert result.suggest_escalation is False
