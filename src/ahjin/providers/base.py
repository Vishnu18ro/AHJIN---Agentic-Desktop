"""Base Abstract Model Provider Interface."""

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator

from ahjin.providers.types import (
    ModelInvocationRequest,
    ModelInvocationResponse,
    StreamChunk,
)


class BaseModelProvider(ABC):
    """Abstract interface for all model providers."""

    @property
    @abstractmethod
    def provider_id(self) -> str:
        """Return unique provider identifier."""

    @abstractmethod
    def get_default_model_id(self) -> str:
        """Return default model identifier for this provider."""

    @abstractmethod
    async def invoke(self, request: ModelInvocationRequest) -> ModelInvocationResponse:
        """Invoke model endpoint and return canonical response."""

    async def invoke_stream(self, request: ModelInvocationRequest) -> AsyncGenerator[str, None]:
        """Invoke model endpoint and yield content chunks asynchronously.

        Default fallback: invokes model non-streaming and yields total content.
        """
        res = await self.invoke(request)
        yield StreamChunk(res.content, is_progress=True, is_reasoning=False)

    async def aclose(self) -> None:
        """Close any underlying network clients or resources cleanly."""
        return None
