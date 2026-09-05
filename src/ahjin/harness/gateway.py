"""ProviderGateway — Capability matching and model provider lookup.

Translates BERU ExecutionStrategy / CapabilityRequirements via ModelRouter into a concrete
(Provider, ModelID) selection and delegates invocation.

CRITICAL CONTRACT:
- ModelRouter is the authoritative selector in ALL production paths.
- There is NO silent fallback that bypasses ModelRouter in production.
- A KeyError from the provider registry (unknown provider_id) raises explicitly
  so the bug is visible rather than masked by a default model bypass.
- ProviderGateway attaches exc.model_id before re-raising so HarnessRunner
  can reliably identify and exclude the failed model during same-request recovery.
"""

import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass

import structlog

from ahjin.beru.types import CapabilityRequirements, ExecutionStrategy
from ahjin.models.router import ModelRouter, ModelSelectionResult
from ahjin.providers.base import BaseModelProvider
from ahjin.providers.registry import ProviderRegistry
from ahjin.providers.types import (
    ContextualizedPrompt,
    ModelInvocationRequest,
    ModelInvocationResponse,
)

logger = structlog.get_logger()


@dataclass(frozen=True)
class GatewayInvocationResult:
    """Result of a single Gateway invocation, including routing metadata.

    Carries both the model response and the selection metadata needed by
    HarnessRunner to build RuntimeInfo for Telegram observability.
    """

    response: ModelInvocationResponse
    selection: ModelSelectionResult


class ProviderGateway:
    """Matches execution strategy to concrete providers and invokes them."""

    def __init__(
        self,
        registry: ProviderRegistry | None = None,
        router: ModelRouter | None = None,
    ) -> None:
        self.registry = registry or ProviderRegistry()
        self.router = router or ModelRouter()

    async def invoke(
        self,
        prompt: ContextualizedPrompt,
        requirements: ExecutionStrategy | CapabilityRequirements,
        excluded_model_ids: set[str] | None = None,
    ) -> GatewayInvocationResult:
        """Resolve provider and model via ModelRouter and invoke model.

        ModelRouter is ALWAYS authoritative for model selection.
        Provider lookup failure raises explicitly — no silent default-model bypass.

        Returns GatewayInvocationResult bundling both the model response and the
        selection metadata for use by HarnessRunner's RuntimeInfo assembly.
        """
        logger.debug(
            "Execution requirements received by gateway",
            excluded_models=list(excluded_model_ids or []),
        )

        selection = self.router.select_model(
            requirements, excluded_model_ids=excluded_model_ids
        )

        # Provider lookup: if provider_id is not registered this raises KeyError explicitly.
        # This surfaces configuration/setup errors rather than silently selecting an
        # arbitrary default model, which would bypass ModelRouter's authoritative decision.
        provider: BaseModelProvider = self.registry.get_provider(selection.provider_id)

        model_id = selection.model_id
        max_tokens = selection.max_output_tokens

        request = ModelInvocationRequest(
            prompt=prompt,
            model_id=model_id,
            max_tokens=max_tokens,
        )

        logger.info(
            "Invoking provider via gateway",
            provider_id=provider.provider_id,
            model_id=request.model_id,
            tier=selection.tier.value,
            router_time_ms=round(selection.selection_time_ms, 3),
        )

        try:
            response = await provider.invoke(request)
            self.router.health_tracker.record_success(model_id, response.latency_ms)
            return GatewayInvocationResult(response=response, selection=selection)
        except Exception as exc:
            self.router.health_tracker.record_failure(model_id)
            # Attach selected model_id to exception so HarnessRunner can identify
            # and exclude the exact failed model during same-request recovery.
            exc.model_id = model_id  # pyright: ignore[reportAttributeAccessIssue]
            raise

    async def invoke_stream(
        self,
        prompt: ContextualizedPrompt,
        requirements: ExecutionStrategy | CapabilityRequirements,
        excluded_model_ids: set[str] | None = None,
    ) -> AsyncGenerator[tuple[str, ModelSelectionResult], None]:
        """Resolve provider and model via ModelRouter and yield text chunks."""
        selection = self.router.select_model(
            requirements, excluded_model_ids=excluded_model_ids
        )
        provider: BaseModelProvider = self.registry.get_provider(selection.provider_id)
        model_id = selection.model_id
        max_tokens = selection.max_output_tokens

        request = ModelInvocationRequest(
            prompt=prompt,
            model_id=model_id,
            max_tokens=max_tokens,
        )

        logger.info(
            "Invoking provider stream via gateway",
            provider_id=provider.provider_id,
            model_id=request.model_id,
            tier=selection.tier.value,
        )

        t0 = time.monotonic()
        try:
            async for chunk in provider.invoke_stream(request):
                yield chunk, selection
            latency_ms = (time.monotonic() - t0) * 1000.0
            self.router.health_tracker.record_success(model_id, latency_ms)
        except Exception as exc:
            self.router.health_tracker.record_failure(model_id)
            exc.model_id = model_id  # pyright: ignore[reportAttributeAccessIssue]
            raise
