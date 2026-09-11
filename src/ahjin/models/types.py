"""Model domain types for catalog, capabilities, and routing."""

from enum import Enum

from pydantic import BaseModel, Field


class ModelTier(str, Enum):
    """Model tier classification for routing priority."""

    FAST = "FAST"
    HEAVY = "HEAVY"
    ALL = "ALL"


class ModelRole(str, Enum):
    """Model role in the routing topology."""

    PRIMARY = "PRIMARY"
    LIGHT_FALLBACK = "LIGHT_FALLBACK"
    HEAVY_FALLBACK = "HEAVY_FALLBACK"
    OFFLINE_FALLBACK = "OFFLINE_FALLBACK"


class ModelCapabilities(BaseModel):
    """Independent capability dimensions supported by a model."""

    reasoning: bool = False
    coding: bool = False
    vision: bool = False
    tool_calling: bool = False
    long_context: bool = False


class ModelLimits(BaseModel):
    """Context window and token generation boundaries."""

    max_context_tokens: int = 128000
    max_output_tokens: int = 4096


class ModelDescriptor(BaseModel):
    """Descriptor defining a model identity, capabilities, quality rating, and provider binding."""

    model_id: str
    provider_id: str
    tier: ModelTier = ModelTier.FAST
    role: ModelRole = ModelRole.LIGHT_FALLBACK
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    limits: ModelLimits = Field(default_factory=ModelLimits)
    priority: int = 100
    # Model strength/quality rating (0-100) for ranking among eligible models
    quality_score: int = 80
    is_active: bool = True
    endpoint_verified: bool = True
