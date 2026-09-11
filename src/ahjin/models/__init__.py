"""Model Intelligence domain package for AHJIN 2.0."""

from ahjin.models.catalog import ModelCatalog, create_default_catalog
from ahjin.models.health import ModelHealthStatus, ModelHealthTracker
from ahjin.models.router import CapabilityUnavailableError, ModelRouter, ModelSelectionResult
from ahjin.models.types import (
    ModelCapabilities,
    ModelDescriptor,
    ModelLimits,
    ModelRole,
    ModelTier,
)

__all__ = [
    "CapabilityUnavailableError",
    "ModelCapabilities",
    "ModelCatalog",
    "ModelDescriptor",
    "ModelHealthStatus",
    "ModelHealthTracker",
    "ModelLimits",
    "ModelRole",
    "ModelRouter",
    "ModelSelectionResult",
    "ModelTier",
    "create_default_catalog",
]
