"""Embedding Services and Contracts for AHJIN RAG subsystem."""

import time
from abc import ABC, abstractmethod
from typing import Any

import httpx
import structlog
from pydantic import BaseModel, Field

from ahjin.core.config import settings

logger = structlog.get_logger()


class EmbeddingRequest(BaseModel):
    """Canonical embedding generation request contract."""

    input_text: str | list[str]
    model_id: str = Field(default_factory=lambda: settings.ollama_embedding_model)


class EmbeddingResponse(BaseModel):
    """Canonical embedding generation response contract."""

    embeddings: list[list[float]]
    model_id: str
    dimension: int
    prompt_tokens: int = 0
    latency_ms: float = 0.0


class BaseEmbeddingService(ABC):
    """Abstract interface for embedding generation services."""

    @abstractmethod
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        """Generate dense vector embeddings for input text."""


class OllamaEmbeddingService(BaseEmbeddingService):
    """Local Ollama-backed embedding service using OpenAI-compatible /v1/embeddings endpoint."""

    def __init__(
        self,
        base_url: str | None = None,
        embedding_model: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self.embedding_model = embedding_model or settings.ollama_embedding_model
        self.timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else settings.ollama_timeout_seconds
        )

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        """Invoke Ollama OpenAI-compatible /v1/embeddings endpoint."""
        start_time = time.monotonic()
        target_model = request.model_id or self.embedding_model

        input_payload = (
            [request.input_text] if isinstance(request.input_text, str) else request.input_text
        )

        payload: dict[str, Any] = {
            "model": target_model,
            "input": input_payload,
        }

        url = f"{self.base_url}/embeddings"
        logger.info("[PROFILE] Calling Ollama Embeddings API start", model=target_model, url=url)

        t0_net = time.monotonic()
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            resp = await client.post(url, json=payload)
            t_net_ms = (time.monotonic() - t0_net) * 1000.0
            resp.raise_for_status()

            data: dict[str, Any] = resp.json()

        elapsed_ms = (time.monotonic() - start_time) * 1000.0

        raw_data = data.get("data", [])
        embeddings: list[list[float]] = []
        if isinstance(raw_data, list):
            for item in raw_data:  # pyright: ignore[reportUnknownVariableType]
                if isinstance(item, dict) and "embedding" in item:
                    emb_list = item["embedding"]  # pyright: ignore[reportUnknownVariableType]
                    if isinstance(emb_list, list):
                        embeddings.append([float(x) for x in emb_list])  # pyright: ignore[reportUnknownVariableType,reportUnknownArgumentType]

        if not embeddings:
            raise ValueError(f"Ollama embedding model '{target_model}' returned empty embeddings.")

        dimension = len(embeddings[0])
        prompt_tokens: int = 0
        usage_data = data.get("usage")
        if isinstance(usage_data, dict):
            pt = usage_data.get("prompt_tokens")  # pyright: ignore[reportUnknownVariableType,reportUnknownMemberType]
            if isinstance(pt, (int, float)):
                prompt_tokens = int(pt)

        logger.info(
            "[PROFILE] Ollama Embeddings API response received",
            model=target_model,
            count=len(embeddings),
            dimension=dimension,
            network_http_ms=round(t_net_ms, 3),
            total_ms=round(elapsed_ms, 3),
        )

        return EmbeddingResponse(
            embeddings=embeddings,
            model_id=target_model,
            dimension=dimension,
            prompt_tokens=prompt_tokens,
            latency_ms=elapsed_ms,
        )
