"""Groq provider implementation."""

from collections.abc import AsyncGenerator
import json
import httpx
import structlog
from typing import Any
import time

from ahjin.core.config import settings
from ahjin.models.catalog import ModelTier
from ahjin.providers.base import BaseModelProvider
from ahjin.providers.types import ModelInvocationRequest, ModelInvocationResponse, FinishReason, TokenUsage

logger = structlog.get_logger()

class GroqProvider(BaseModelProvider):
    def __init__(self) -> None:
        self.api_key = settings.groq_api_key
        self.base_url = "https://api.groq.com/openai/v1"
        self.timeout_seconds = 60.0
        self.max_tokens = 4096
        self._default_model = "qwen/qwen3.6-27b"

        if not self.api_key:
            logger.warning("GroqProvider initialized without an API key")

    @property
    def provider_id(self) -> str:
        return "groq"

    def get_default_model_id(self) -> str:
        return self._default_model

    async def invoke(self, request: ModelInvocationRequest) -> ModelInvocationResponse:
        start_time = time.time()
        if not self.api_key:
            raise ValueError("Groq API key not configured")

        messages = []
        if request.prompt.system_instruction:
            messages.append({"role": "system", "content": request.prompt.system_instruction})

        for turn in request.prompt.conversation_history:
            messages.append({"role": turn.role.value, "content": turn.content})

        messages.append({"role": "user", "content": request.prompt.user_instruction})

        target_model_id = request.model_id or self._default_model

        payload = {
            "model": target_model_id,
            "messages": messages,
            "temperature": 0.0,
            "max_tokens": request.max_tokens or self.max_tokens,
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/chat/completions"

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()

        choices = data.get("choices", [])
        raw_content = choices[0]["message"].get("content") if choices else None

        if not raw_content:
            raise ValueError(f"Groq model '{target_model_id}' returned empty content.")

        latency = (time.time() - start_time) * 1000.0

        return ModelInvocationResponse(
            invocation_id=request.invocation_id,
            content=raw_content,
            finish_reason=FinishReason.COMPLETE,
            latency_ms=latency,
            provider_id=self.provider_id,
            model_id=target_model_id
        )

    async def invoke_stream(self, request: ModelInvocationRequest) -> AsyncGenerator[str, None]:
        if not self.api_key:
            raise ValueError("Groq API key not configured")

        messages = []
        if request.prompt.system_instruction:
            messages.append({"role": "system", "content": request.prompt.system_instruction})

        for turn in request.prompt.conversation_history:
            messages.append({"role": turn.role.value, "content": turn.content})

        messages.append({"role": "user", "content": request.prompt.user_instruction})

        target_model_id = request.model_id or self._default_model

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
                            logger.warning("Groq stream chunk decode failed", chunk=data_str)
