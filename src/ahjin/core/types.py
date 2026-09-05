"""Canonical domain type definitions for AHJIN 2.0."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from ahjin.core.errors import AhjinError


class Role(str, Enum):
    """Dialogue turn role."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class Modality(str, Enum):
    """Input modality capability."""

    TEXT = "text"
    TEXT_WITH_IMAGE = "text_with_image"
    DOCUMENT = "document"
    AUDIO = "audio"


class Attachment(BaseModel):
    """Non-text content attachment reference."""

    attachment_id: UUID = Field(default_factory=uuid4)
    mime_type: str
    uri: str | None = None
    inline_data: bytes | None = None


class UserIntent(BaseModel):
    """Normalized user intent."""

    primary_text: str
    attachments: list[Attachment] = Field(default_factory=list)  # pyright: ignore[reportUnknownVariableType]
    modality: Modality = Modality.TEXT


class ConversationTurn(BaseModel):
    """One turn of dialogue history."""

    turn_id: UUID = Field(default_factory=uuid4)
    role: Role
    content: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TaskContext(BaseModel):
    """Session and context history."""

    session_id: str
    conversation_history: list[ConversationTurn] = Field(default_factory=list)  # pyright: ignore[reportUnknownVariableType]


class RequestMetadata(BaseModel):
    """Request metadata and provenance."""

    schema_version: str = "1.0"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source_interface: str = "unknown"  # caller sets this; Core makes no interface assumption
    parent_task_id: UUID | None = None


class TaskRequest(BaseModel):
    """Canonical entry contract into AHJIN Core."""

    task_id: UUID = Field(default_factory=uuid4)
    correlation_id: UUID = Field(default_factory=uuid4)
    intent: UserIntent
    context: TaskContext
    metadata: RequestMetadata = Field(default_factory=RequestMetadata)


class RuntimeInfo(BaseModel):
    """Runtime observability metadata attached to TaskResult.

    Populated by HarnessRunner after execution. Used exclusively by Interface
    adapters to render diagnostic footers. Never consumed by core routing logic.
    """

    selected_model: str = ""
    tier: str = ""
    provider_id: str = ""
    # Timing in milliseconds (best-effort; 0.0 means not measured)
    ahjin_internal_ms: float = 0.0    # BERU + routing overhead
    model_api_ms: float = 0.0         # provider round-trip only
    total_ms: float = 0.0             # full request time
    # Rerouting
    was_rerouted: bool = False
    failed_model: str | None = None
    failure_reason: str | None = None
    # Health of selected model at response time
    health_status: str = "UNKNOWN"


class TaskResult(BaseModel):
    """Canonical result contract returned to Interface."""

    task_id: UUID
    correlation_id: UUID
    success: bool
    output_text: str | None = None
    error: AhjinError | None = None
    completed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    runtime_info: RuntimeInfo | None = None  # Set by HarnessRunner; None if unavailable
    # Phase 5 local escalation hook (type: LocalExecutionResult | None).
    # Set by HarnessRunner when Qwen timeout → Gemma fallback occurred.
    # Interface adapters check this to offer "higher-model opinion?" follow-up.
    # Always None on cloud-path executions — fully backward-compatible.
    local_escalation_hint: object | None = None

