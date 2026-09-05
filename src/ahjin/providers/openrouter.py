"""OpenRouter Model Provider implementation.

All OpenRouter-specific authentication, HTTP headers, payload formatting,
and API error handling stay entirely within this file.
"""

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

logger = structlog.get_logger()


class OpenRouterProvider(BaseModelProvider):
    """OpenRouter API Model Provider."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        default_model: str | None = None,
        max_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.api_key = settings.openrouter_api_key if api_key is None else api_key
        self.base_url = (base_url or settings.openrouter_base_url).rstrip("/")
        self.default_model = default_model or ""
        self.max_tokens = max_tokens if max_tokens is not None else settings.nvidia_max_tokens

        self.timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else settings.openrouter_timeout_seconds
        )

        # Fast-fail: do not allow construction with unconfigured credentials.
        if not self.api_key:
            raise ValueError(
                "OPENROUTER_API_KEY is not configured. "
                "Set it in environment or .env before constructing OpenRouterProvider."
            )

    @property
    def provider_id(self) -> str:
        return "openrouter"

    def get_default_model_id(self) -> str:
        return self.default_model

    async def invoke(self, request: ModelInvocationRequest) -> ModelInvocationResponse:
        """Invoke OpenRouter OpenAI-compatible chat completions API."""
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
                "Specify model_id in request or initialize OpenRouterProvider(default_model=...)."
            )

        payload: dict[str, Any] = {
            "model": target_model_id,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": request.max_tokens or self.max_tokens,
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://ahjin.ai",
            "X-Title": "AHJIN 2.0",
        }

        url = f"{self.base_url}/chat/completions"
        t_prep_ms = (time.monotonic() - t0_prep) * 1000.0

        logger.info("[PROFILE] Calling OpenRouter API start", model=payload["model"], url=url)

        t0_net = time.monotonic()
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            resp = await client.post(url, json=payload, headers=headers)
            t_net_ms = (time.monotonic() - t0_net) * 1000.0
            resp.raise_for_status()

            t0_parse = time.monotonic()
            data: dict[str, Any] = resp.json()
            t_parse_ms = (time.monotonic() - t0_parse) * 1000.0

        elapsed_ms = (time.monotonic() - start_time) * 1000.0

        logger.info(
            "[PROFILE] OpenRouter API response received",
            model=payload["model"],
            payload_prep_ms=round(t_prep_ms, 3),
            network_http_ms=round(t_net_ms, 3),
            json_parse_ms=round(t_parse_ms, 3),
            provider_total_ms=round(elapsed_ms, 3),
        )

        choices = data.get("choices", [])
        raw_content: str | None = choices[0]["message"].get("content") if choices else None

        if not raw_content:
            raise ValueError(
                f"OpenRouter model '{payload['model']}' returned empty content. "
                "Try a different model or retry the request."
            )

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
            "[PROFILE] OpenRouter finish_reason mapped",
            raw_finish_reason=raw_finish_reason,
            canonical_finish_reason=finish_reason.value,
        )

        usage_data = data.get("usage", {})
        usage = TokenUsage(
            prompt_tokens=usage_data.get("prompt_tokens", 0),
            completion_tokens=usage_data.get("completion_tokens", 0),
            total_tokens=usage_data.get("total_tokens", 0),
        )

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
        """Invoke OpenRouter API with stream=True and yield text chunks."""
        import json

        messages: list[dict[str, str]] = []
        if request.prompt.system_instruction:
            messages.append({"role": "system", "content": request.prompt.system_instruction})

        for turn in request.prompt.conversation_history:
            messages.append({"role": turn.role.value, "content": turn.content})

        messages.append({"role": "user", "content": request.prompt.user_instruction})

        target_model_id = request.model_id or self.default_model
        if not target_model_id:
            raise ValueError("model_id is not specified in request or provider default.")

        payload: dict[str, Any] = {
            "model": target_model_id,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": request.max_tokens or self.max_tokens,
            "stream": True,
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://ahjin.ai",
            "X-Title": "AHJIN 2.0",
        }

        url = f"{self.base_url}/chat/completions"

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line or line.startswith(":"):
                        continue
                    if line.startswith("data: "):
                        data_str = line[6:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            data_json = json.loads(data_str)
                            choices = data_json.get("choices", [])
                            if choices:
                                delta = choices[0].get("delta", {})
                                content = delta.get("content")
                                if content:
                                    yield content
                        except json.JSONDecodeError:
                            continue
