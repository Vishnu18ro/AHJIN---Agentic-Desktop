"""Model Provider Request and Response contracts.

Also owns ContextualizedPrompt — the assembled prompt handed to the provider layer.
ContextualizedPrompt is a provider-boundary concept, not a Core domain type.
"""

from datetime import datetime, timezone
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from ahjin.core.types import ConversationTurn


class ContextualizedPrompt(BaseModel):
    """Assembled prompt ready for provider translation.

    Lives at the provider boundary — this is the shape providers consume.
    Constructed by ContextAssembler in the Harness/context layer.
    Not a Core domain type.
    """

    system_instruction: str = "You are AHJIN 2.0, an Agentic AI Operating Layer."
    conversation_history: list[ConversationTurn] = Field(default_factory=list)  # pyright: ignore[reportUnknownVariableType]
    user_instruction: str


class FinishReason(str, Enum):
    """Model invocation finish reason."""

    COMPLETE = "COMPLETE"
    MAX_TOKENS = "MAX_TOKENS"
    STOP_SEQUENCE = "STOP_SEQUENCE"
    ERROR = "ERROR"


class TokenUsage(BaseModel):
    """Token usage metrics."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ModelInvocationRequest(BaseModel):
    """Request sent to a model provider."""

    invocation_id: UUID = Field(default_factory=uuid4)
    prompt: ContextualizedPrompt
    model_id: str
    max_tokens: int | None = None


class ModelInvocationResponse(BaseModel):
    """Canonical model response returned by provider."""

    invocation_id: UUID
    content: str
    finish_reason: FinishReason = FinishReason.COMPLETE
    usage: TokenUsage | None = None
    latency_ms: float = 0.0
    provider_id: str
    model_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class StreamChunk(str):
    """String subclass representing a stream chunk, optionally carrying progress/event metadata.

    Inherits from str so that all string methods, string checks (isinstance(chunk, str)),
    concatenations, joins, and comparisons remain completely transparent and backward-compatible
    with existing consumers and tests.

    Attributes:
        is_progress: True if chunk signals legitimate stream activity (resets watchdog).
        is_reasoning: True if chunk signals internal reasoning (suppressed from user/JSON).
    """

    is_progress: bool = True
    is_reasoning: bool = False

    def __new__(
        cls,
        content: str = "",
        *,
        is_progress: bool = True,
        is_reasoning: bool = False,
    ) -> "StreamChunk":
        instance = super().__new__(cls, content)
        instance.is_progress = is_progress
        instance.is_reasoning = is_reasoning
        return instance
