"""Tool execution subsystem abstract interfaces (Stubbed for v2)."""

from abc import ABC, abstractmethod
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from ahjin.core.errors import AhjinError


class ToolInvocationRequest(BaseModel):
    """Tool invocation request contract."""

    invocation_id: UUID = Field(default_factory=uuid4)
    tool_name: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    requires_reasoning: bool = False


class ToolInvocationResult(BaseModel):
    """Tool invocation result contract."""

    invocation_id: UUID
    success: bool
    output: Any | None = None
    error: AhjinError | None = None
    latency_ms: float = 0.0


class BaseTool(ABC):
    """Abstract interface for all AHJIN tools."""

    @property
    @abstractmethod
    def tool_name(self) -> str:
        """Unique tool name."""

    @abstractmethod
    async def execute(self, request: ToolInvocationRequest) -> ToolInvocationResult:
        """Execute tool logic."""
