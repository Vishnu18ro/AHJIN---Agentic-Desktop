"""NVIDIA Model Provider implementation.

All NVIDIA-specific authentication, HTTP headers, payload formatting,
and API error handling stay entirely within this file.
"""

import json
import time
from collections.abc import AsyncGenerator
from typing import Any

import httpx
import structlog

from ahjin.core.config import settings
from ahjin.providers.base import BaseModelProvider
from ahjin.providers.types import (
    FinishReason,
    ModelInvocationRequest,
    ModelInvocationResponse,
    TokenUsage,
)
from ahjin.telemetry.timing import (
    STAGE_FIRST_REASONING,
    STAGE_FIRST_SSE,
    STAGE_FIRST_VISIBLE_CONTENT,
    STAGE_HTTP_CONNECT,
    STAGE_PROVIDER_REQUEST_START,
    STAGE_REASONING_DURATION,
    STAGE_STREAM_COMPLETION,
    STAGE_VISIBLE_GENERATION,
)

logger = structlog.get_logger()


class NvidiaProvider(BaseModelProvider):
    """NVIDIA API Model Provider with connection pooling and stream telemetry."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        default_model: str | None = None,
        max_tokens: int | None = None,
        timeout_seconds: float | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = settings.nvidia_api_key if api_key is None else api_key
        self.base_url = (base_url or settings.nvidia_base_url).rstrip("/")
        self.default_model = default_model or ""
        # max_tokens: configuration-driven fallback.
        self.max_tokens = max_tokens if max_tokens is not None else settings.nvidia_max_tokens
        # timeout_seconds: configuration-driven HTTP client timeout.
        self.timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else settings.nvidia_timeout_seconds
        )
        self._client: httpx.AsyncClient | None = client
        self._owns_client: bool = client is None
        self.last_telemetry: dict[str, float] = {}

        # Fast-fail: do not allow construction with unconfigured credentials.
        if not self.api_key:
            raise ValueError(
                "NVIDIA_API_KEY is not configured. "
                "Set it in environment or .env before constructing NvidiaProvider."
            )

    @property
    def provider_id(self) -> str:
        return "nvidia"

    def get_default_model_id(self) -> str:
        return self.default_model

    def _get_client(self) -> httpx.AsyncClient:
        """Get or lazily create persistent httpx.AsyncClient with connection pooling."""
        if self._client is None or self._client.is_closed:
            limits = httpx.Limits(
                max_keepalive_connections=20,
                max_connections=50,
                keepalive_expiry=60.0,
            )
            self._client = httpx.AsyncClient(
                timeout=self.timeout_seconds,
                limits=limits,
            )
            self._owns_client = True
        return self._client

    async def aclose(self) -> None:
        """Explicitly and safely close the persistent HTTP client."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    def close(self) -> None:
        """Synchronous cleanup for non-async teardown paths."""
        if self._client is not None and not self._client.is_closed:
            try:
                import asyncio
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(self._client.aclose())
                else:
                    loop.run_until_complete(self._client.aclose())
            except Exception:
                pass
            self._client = None

    async def __aenter__(self) -> "NvidiaProvider":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.aclose()

    async def invoke(self, request: ModelInvocationRequest) -> ModelInvocationResponse:
        """Invoke NVIDIA OpenAI-compatible chat completions API."""
        start_time = time.monotonic()

        t0_prep = time.monotonic()
        messages: list[dict[str, str]] = []
        if request.prompt.system_instruction:
            messages.append({"role": "system", "content": request.prompt.system_instruction})

        for turn in request.prompt.conversation_history:
            messages.append({"role": turn.role.value, "content": turn.content})

        messages.append({"role": "user", "content": request.prompt.user_instruction})

        target_model_id = request.model_id or self.default_model
        if not target_model_id:
            raise ValueError(
                "model_id is not specified in request or provider default. "
                "Specify model_id in request or initialize NvidiaProvider(default_model=...)."
            )

        payload = {
            "model": target_model_id,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": request.max_tokens or self.max_tokens,
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        url = f"{self.base_url}/chat/completions"
        t_prep_ms = (time.monotonic() - t0_prep) * 1000.0

        logger.info("[PROFILE] Calling NVIDIA API start", model=payload["model"], url=url)

        client = self._get_client()
        t0_net = time.monotonic()
        resp = await client.post(url, json=payload, headers=headers, timeout=self.timeout_seconds)
        t_net_ms = (time.monotonic() - t0_net) * 1000.0
        resp.raise_for_status()

        t0_parse = time.monotonic()
        data: dict[str, Any] = resp.json()
        t_parse_ms = (time.monotonic() - t0_parse) * 1000.0

        elapsed_ms = (time.monotonic() - start_time) * 1000.0

        logger.info(
            "[PROFILE] NVIDIA API response received",
            model=payload["model"],
            payload_prep_ms=round(t_prep_ms, 3),
            network_http_ms=round(t_net_ms, 3),
            json_parse_ms=round(t_parse_ms, 3),
            provider_total_ms=round(elapsed_ms, 3),
        )

        choices = data.get("choices", [])
        raw_content: str | None = choices[0]["message"].get("content") if choices else None

        if not raw_content:
            # Model returned empty or null content — surface as an invocation error
            # so the error boundary in HarnessRunner handles it correctly.
            raise ValueError(
                f"NVIDIA model '{payload['model']}' returned empty content. "
                "Try a different model or retry the request."
            )

        # Map NVIDIA's raw finish_reason to AHJIN's canonical FinishReason.
        # NVIDIA returns: 'stop' (natural end), 'length' (max_tokens hit), others.
        # Previously hardcoded to COMPLETE — this masked truncation events.
        raw_finish_reason: str = (
            choices[0].get("finish_reason") or "stop"
        ) if choices else "stop"
        if raw_finish_reason == "length":
            finish_reason = FinishReason.MAX_TOKENS
        elif raw_finish_reason in ("stop", "eos"):
            finish_reason = FinishReason.COMPLETE
        else:
            finish_reason = FinishReason.COMPLETE

        logger.info(
            "[PROFILE] NVIDIA finish_reason mapped",
            raw_finish_reason=raw_finish_reason,
            canonical_finish_reason=finish_reason.value,
            max_tokens_configured=payload["max_tokens"],
        )

        usage_data = data.get("usage", {})
        usage = TokenUsage(
            prompt_tokens=usage_data.get("prompt_tokens", 0),
            completion_tokens=usage_data.get("completion_tokens", 0),
            total_tokens=usage_data.get("total_tokens", 0),
        )

        self.last_telemetry = {
            STAGE_PROVIDER_REQUEST_START: 0.0,
            STAGE_HTTP_CONNECT: round(t_net_ms, 1),
            STAGE_STREAM_COMPLETION: round(elapsed_ms, 1),
        }

        return ModelInvocationResponse(
            invocation_id=request.invocation_id,
            content=raw_content,
            finish_reason=finish_reason,
            usage=usage,
            latency_ms=elapsed_ms,
            provider_id=self.provider_id,
            model_id=str(payload["model"]),
        )

    async def invoke_stream(
        self, request: ModelInvocationRequest
    ) -> AsyncGenerator[str, None]:
        """Invoke NVIDIA API with stream=True, reusing persistent HTTP client."""
        messages: list[dict[str, str]] = []
        if request.prompt.system_instruction:
            messages.append({"role": "system", "content": request.prompt.system_instruction})

        for turn in request.prompt.conversation_history:
            messages.append({"role": turn.role.value, "content": turn.content})

        messages.append({"role": "user", "content": request.prompt.user_instruction})

        target_model_id = request.model_id or self.default_model
        if not target_model_id:
            raise ValueError("model_id is not specified in request or provider default.")

        payload = {
            "model": target_model_id,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": request.max_tokens or self.max_tokens,
            "stream": True,
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/chat/completions"

        t0_start = time.perf_counter()
        t_connect: float | None = None
        t_first_sse: float | None = None
        t_first_reasoning: float | None = None
        t_first_visible: float | None = None
        t_stream_completion: float | None = None
        reasoning_chunks_count = 0
        visible_chunks_count = 0

        client = self._get_client()
        async with client.stream(
            "POST", url, json=payload, headers=headers, timeout=self.timeout_seconds
        ) as response:
            t_connect = time.perf_counter()
            response.raise_for_status()
            async for line in response.aiter_lines():
                line = line.strip()
                if not line or line.startswith(":"):
                    continue
                if line.startswith("data: "):
                    if t_first_sse is None:
                        t_first_sse = time.perf_counter()
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        data_json = json.loads(data_str)
                        choices = data_json.get("choices", [])
                        if choices:
                            delta = choices[0].get("delta", {})

                            # Telemetry: Record reasoning phase arrival
                            # CRITICAL: NEVER yield reasoning_content to stream or user!
                            reasoning_chunk = delta.get("reasoning_content")
                            if reasoning_chunk:
                                if t_first_reasoning is None:
                                    t_first_reasoning = time.perf_counter()
                                reasoning_chunks_count += 1

                            # Stream delivery: Yield visible content chunks immediately
                            content = delta.get("content")
                            if content:
                                if t_first_visible is None:
                                    t_first_visible = time.perf_counter()
                                visible_chunks_count += 1
                                yield content
                    except json.JSONDecodeError:
                        continue

        t_stream_completion = time.perf_counter()

        # Compute internal granular latency telemetry
        http_connect_ms = (t_connect - t0_start) * 1000.0 if t_connect else 0.0
        first_sse_ms = (t_first_sse - t0_start) * 1000.0 if t_first_sse else 0.0
        first_reasoning_ms = (t_first_reasoning - t0_start) * 1000.0 if t_first_reasoning else 0.0
        first_visible_ms = (t_first_visible - t0_start) * 1000.0 if t_first_visible else 0.0

        reasoning_duration_ms = 0.0
        if t_first_reasoning:
            end_reasoning = t_first_visible or t_stream_completion
            if end_reasoning > t_first_reasoning:
                reasoning_duration_ms = (end_reasoning - t_first_reasoning) * 1000.0

        visible_gen_ms = 0.0
        if t_first_visible and t_stream_completion > t_first_visible:
            visible_gen_ms = (t_stream_completion - t_first_visible) * 1000.0

        stream_completion_ms = (t_stream_completion - t0_start) * 1000.0

        self.last_telemetry = {
            STAGE_PROVIDER_REQUEST_START: 0.0,
            STAGE_HTTP_CONNECT: round(http_connect_ms, 1),
            STAGE_FIRST_SSE: round(first_sse_ms, 1),
            STAGE_FIRST_REASONING: round(first_reasoning_ms, 1),
            STAGE_FIRST_VISIBLE_CONTENT: round(first_visible_ms, 1),
            STAGE_REASONING_DURATION: round(reasoning_duration_ms, 1),
            STAGE_VISIBLE_GENERATION: round(visible_gen_ms, 1),
            STAGE_STREAM_COMPLETION: round(stream_completion_ms, 1),
        }

        logger.info(
            "[PROFILE] NVIDIA stream telemetry",
            model=target_model_id,
            http_connect_ms=round(http_connect_ms, 1),
            first_sse_ms=round(first_sse_ms, 1),
            first_reasoning_ms=round(first_reasoning_ms, 1),
            first_visible_ms=round(first_visible_ms, 1),
            reasoning_duration_ms=round(reasoning_duration_ms, 1),
            visible_generation_ms=round(visible_gen_ms, 1),
            stream_completion_ms=round(stream_completion_ms, 1),
            reasoning_chunks=reasoning_chunks_count,
            visible_chunks=visible_chunks_count,
        )

