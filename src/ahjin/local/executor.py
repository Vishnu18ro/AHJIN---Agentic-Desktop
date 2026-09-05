"""LocalExecutor — Ollama local execution engine with Qwen 90-second budget.

Responsibilities:
- Consult LocalRoutingPolicy (BERU's ExecutionStrategy as input).
- Route to Qwen 3 8B or Gemma 3 4B.
- Enforce the Qwen 90-second application-level deadline via asyncio.wait_for.
- On Qwen timeout: cancel cleanly, then invoke Gemma with the ORIGINAL prompt.
- Never mutate the caller's ContextualizedPrompt.
- Raise LocalRoutingSkipped when policy says no local execution.
- Raise LocalExecutionError when local execution fails and cloud should be tried.

What this module is NOT:
- It is NOT a replacement for ModelRouter.
- It does NOT select cloud models.
- OLLAMA_TIMEOUT_SECONDS is NOT changed.
"""

import asyncio
import time
from collections.abc import AsyncGenerator

import httpx
import structlog

from ahjin.beru.types import ExecutionStrategy
from ahjin.local.policy import LocalRoutingPolicy
from ahjin.local.types import (
    LOCAL_GEMMA_MODEL_ID,
    LOCAL_QWEN_MODEL_ID,
    LocalExecutionError,
    LocalExecutionResult,
    LocalRoutingSkipped,
)
from ahjin.providers.ollama import OllamaProvider
from ahjin.providers.types import ContextualizedPrompt, ModelInvocationRequest

logger = structlog.get_logger()

# Template for the user-visible fallback notice.
# Gemma's actual answer is appended after two newlines.
_QWEN_TIMEOUT_NOTICE = (
    "Qwen 3 8B was taking longer than the 90-second local reasoning budget, "
    "so I'm answering with the faster local model.\n\n"
)


class LocalExecutor:
    """Executes requests against the local Ollama fleet.

    Enforces the Qwen 90-second application-level deadline.
    Falls back to Gemma on Qwen timeout, preserving the original user prompt.
    """

    def __init__(
        self,
        policy: LocalRoutingPolicy,
        provider: OllamaProvider,
        qwen_timeout_seconds: float = 90.0,
        gemma_timeout_seconds: float = 60.0,
    ) -> None:
        self._policy = policy
        self._provider = provider
        self._qwen_timeout_seconds = qwen_timeout_seconds
        self._gemma_timeout_seconds = gemma_timeout_seconds

    async def invoke(
        self,
        prompt: ContextualizedPrompt,
        strategy: ExecutionStrategy,
        max_tokens: int = 2048,
    ) -> LocalExecutionResult:
        """Invoke a local Ollama model according to the routing policy.

        Args:
            prompt:     The ORIGINAL ContextualizedPrompt assembled by the Harness.
                        This object is NEVER mutated. Gemma receives an identical
                        copy if Qwen times out.
            strategy:   BERU's computed ExecutionStrategy.
            max_tokens: Maximum tokens for the local invocation.

        Returns:
            LocalExecutionResult on success.

        Raises:
            LocalRoutingSkipped:  Policy decided local execution is inappropriate.
            LocalExecutionError:  Local execution failed; cloud fallback should run.
        """
        decision = self._policy.decide(strategy)

        if not decision.use_local:
            logger.info(
                "Local routing skipped",
                skip_reason=decision.skip_reason,
            )
            raise LocalRoutingSkipped(decision.skip_reason or "policy_decision")

        assert decision.model_id is not None  # guaranteed by LocalRoutingPolicy
        target_model = decision.model_id

        logger.info("LocalExecutor: routing to local model", model_id=target_model)

        if target_model == LOCAL_QWEN_MODEL_ID:
            return await self._invoke_qwen_with_fallback(prompt, max_tokens)
        else:
            return await self._invoke_gemma(prompt, max_tokens)

    async def invoke_stream(
        self,
        prompt: ContextualizedPrompt,
        strategy: ExecutionStrategy,
        max_tokens: int = 2048,
    ) -> AsyncGenerator[tuple[str, LocalExecutionResult], None]:
        """Invoke a local model with streaming tokens and Qwen 90s fallback."""
        decision = self._policy.decide(strategy)

        if not decision.use_local:
            logger.info("Local routing skipped", skip_reason=decision.skip_reason)
            raise LocalRoutingSkipped(decision.skip_reason or "policy_decision")

        assert decision.model_id is not None
        target_model = decision.model_id
        logger.info("LocalExecutor: streaming from local model", model_id=target_model)

        if target_model == LOCAL_QWEN_MODEL_ID:
            async for chunk, res in self._invoke_qwen_stream_with_fallback(prompt, max_tokens):
                yield chunk, res
        else:
            async for chunk, res in self._invoke_gemma_stream(prompt, max_tokens):
                yield chunk, res

    async def _invoke_gemma_stream(
        self,
        prompt: ContextualizedPrompt,
        max_tokens: int,
    ) -> AsyncGenerator[tuple[str, LocalExecutionResult], None]:
        request = ModelInvocationRequest(
            prompt=prompt,
            model_id=LOCAL_GEMMA_MODEL_ID,
            max_tokens=max_tokens,
        )
        t0 = time.monotonic()
        accumulated: list[str] = []
        try:
            async for chunk in self._provider.invoke_stream(request):
                accumulated.append(chunk)
                res = LocalExecutionResult(
                    output_text="".join(accumulated),
                    model_used=LOCAL_GEMMA_MODEL_ID,
                    latency_ms=(time.monotonic() - t0) * 1000.0,
                    used_fallback=False,
                    suggest_escalation=False,
                )
                yield chunk, res
        except Exception as exc:
            raise LocalExecutionError(f"Gemma stream execution failed: {exc}") from exc

    async def _invoke_qwen_stream_with_fallback(
        self,
        prompt: ContextualizedPrompt,
        max_tokens: int,
    ) -> AsyncGenerator[tuple[str, LocalExecutionResult], None]:
        request = ModelInvocationRequest(
            prompt=prompt,
            model_id=LOCAL_QWEN_MODEL_ID,
            max_tokens=max_tokens,
        )
        t0 = time.monotonic()
        qwen_chunks: list[str] = []
        try:
            # Enforce 90-second deadline on Qwen stream
            stream_gen = self._provider.invoke_stream(request)
            qwen_stream_active = True
            while qwen_stream_active:
                elapsed = time.monotonic() - t0
                remaining = self._qwen_timeout_seconds - elapsed
                if remaining <= 0:
                    raise asyncio.TimeoutError()
                try:
                    chunk = await asyncio.wait_for(stream_gen.__anext__(), timeout=remaining)
                    qwen_chunks.append(chunk)
                    res = LocalExecutionResult(
                        output_text="".join(qwen_chunks),
                        model_used=LOCAL_QWEN_MODEL_ID,
                        latency_ms=(time.monotonic() - t0) * 1000.0,
                        used_fallback=False,
                        suggest_escalation=False,
                        attempted_model=LOCAL_QWEN_MODEL_ID,
                    )
                    yield chunk, res
                except StopAsyncIteration:
                    qwen_stream_active = False
        except (asyncio.TimeoutError, httpx.TimeoutException):
            logger.warning(
                "LocalExecutor: Qwen 3 8B timed out during stream — "
                "safely cancelling and falling back to Gemma"
            )
            # Yield fallback notice first
            notice_res = LocalExecutionResult(
                output_text=_QWEN_TIMEOUT_NOTICE,
                model_used=LOCAL_GEMMA_MODEL_ID,
                latency_ms=(time.monotonic() - t0) * 1000.0,
                used_fallback=True,
                fallback_reason="qwen_timeout",
                suggest_escalation=True,
                escalation_reason="qwen_timeout_gemma_fallback",
                attempted_model=LOCAL_QWEN_MODEL_ID,
            )
            yield _QWEN_TIMEOUT_NOTICE, notice_res

            # Invoke Gemma with ORIGINAL prompt
            gemma_req = ModelInvocationRequest(
                prompt=prompt,
                model_id=LOCAL_GEMMA_MODEL_ID,
                max_tokens=max_tokens,
            )
            gemma_accumulated: list[str] = [_QWEN_TIMEOUT_NOTICE]
            try:
                async for chunk in self._provider.invoke_stream(gemma_req):
                    gemma_accumulated.append(chunk)
                    res = LocalExecutionResult(
                        output_text="".join(gemma_accumulated),
                        model_used=LOCAL_GEMMA_MODEL_ID,
                        latency_ms=(time.monotonic() - t0) * 1000.0,
                        used_fallback=True,
                        fallback_reason="qwen_timeout",
                        suggest_escalation=True,
                        escalation_reason="qwen_timeout_gemma_fallback",
                        attempted_model=LOCAL_QWEN_MODEL_ID,
                    )
                    yield chunk, res
            except Exception as exc:
                raise LocalExecutionError(
                    f"Gemma fallback stream after Qwen timeout failed: {exc}"
                ) from exc
        except Exception as exc:
            raise LocalExecutionError(f"Qwen stream failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Internal execution methods
    # ------------------------------------------------------------------

    async def _invoke_qwen_with_fallback(
        self,
        prompt: ContextualizedPrompt,
        max_tokens: int,
    ) -> LocalExecutionResult:
        """Attempt Qwen within the 90-second budget; fall back to Gemma if exceeded."""
        request = ModelInvocationRequest(
            prompt=prompt,
            model_id=LOCAL_QWEN_MODEL_ID,
            max_tokens=max_tokens,
        )

        t0 = time.monotonic()
        try:
            response = await asyncio.wait_for(
                self._provider.invoke(request),
                timeout=self._qwen_timeout_seconds,
            )
            latency_ms = (time.monotonic() - t0) * 1000.0
            logger.info(
                "LocalExecutor: Qwen completed successfully",
                latency_ms=round(latency_ms, 1),
            )
            return LocalExecutionResult(
                output_text=response.content,
                model_used=LOCAL_QWEN_MODEL_ID,
                latency_ms=latency_ms,
                used_fallback=False,
                suggest_escalation=False,
                attempted_model=LOCAL_QWEN_MODEL_ID,
            )

        except asyncio.TimeoutError:
            # asyncio.wait_for already cancelled the Qwen coroutine.
            # We log and proceed directly to the Gemma fallback.
            # No partial Qwen state is carried forward.
            elapsed_s = time.monotonic() - t0
            logger.warning(
                "LocalExecutor: Qwen exceeded 90-second budget — falling back to Gemma",
                elapsed_seconds=round(elapsed_s, 1),
                qwen_timeout_seconds=self._qwen_timeout_seconds,
            )

        # Gemma fallback — receives the EXACT SAME original prompt.
        # No transformation, no injection of Qwen's partial output.
        gemma_request = ModelInvocationRequest(
            prompt=prompt,        # original, unmodified
            model_id=LOCAL_GEMMA_MODEL_ID,
            max_tokens=max_tokens,
        )

        t1 = time.monotonic()
        try:
            gemma_response = await asyncio.wait_for(
                self._provider.invoke(gemma_request),
                timeout=self._gemma_timeout_seconds,
            )
        except Exception as gemma_exc:
            logger.error(
                "LocalExecutor: Gemma fallback failed after Qwen timeout",
                error=str(gemma_exc),
            )
            raise LocalExecutionError(
                f"Gemma fallback failed after Qwen timeout: {gemma_exc}"
            ) from gemma_exc

        fallback_latency_ms = (time.monotonic() - t1) * 1000.0
        logger.info(
            "LocalExecutor: Gemma fallback succeeded",
            latency_ms=round(fallback_latency_ms, 1),
        )

        # Prepend the user-visible fallback notice to Gemma's actual answer.
        output_text = _QWEN_TIMEOUT_NOTICE + gemma_response.content

        return LocalExecutionResult(
            output_text=output_text,
            model_used=LOCAL_GEMMA_MODEL_ID,
            latency_ms=fallback_latency_ms,
            used_fallback=True,
            fallback_reason="qwen_timeout",
            suggest_escalation=True,
            escalation_reason="qwen_timeout_gemma_fallback",
            attempted_model=LOCAL_QWEN_MODEL_ID,
        )

    async def _invoke_gemma(
        self,
        prompt: ContextualizedPrompt,
        max_tokens: int,
    ) -> LocalExecutionResult:
        """Invoke Gemma directly for FAST / general tasks."""
        request = ModelInvocationRequest(
            prompt=prompt,
            model_id=LOCAL_GEMMA_MODEL_ID,
            max_tokens=max_tokens,
        )

        t0 = time.monotonic()
        try:
            response = await asyncio.wait_for(
                self._provider.invoke(request),
                timeout=self._gemma_timeout_seconds,
            )
            latency_ms = (time.monotonic() - t0) * 1000.0
            logger.info(
                "LocalExecutor: Gemma completed successfully",
                latency_ms=round(latency_ms, 1),
            )
            return LocalExecutionResult(
                output_text=response.content,
                model_used=LOCAL_GEMMA_MODEL_ID,
                latency_ms=latency_ms,
                used_fallback=False,
                suggest_escalation=False,
                attempted_model=LOCAL_GEMMA_MODEL_ID,
            )
        except Exception as exc:
            logger.error(
                "LocalExecutor: Gemma execution failed",
                error=str(exc),
            )
            raise LocalExecutionError(f"Gemma execution failed: {exc}") from exc
