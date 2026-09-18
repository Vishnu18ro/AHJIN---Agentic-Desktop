"""ModelCatalog — In-memory registry of known model descriptors."""

import structlog

from ahjin.models.types import (
    ModelCapabilities,
    ModelDescriptor,
    ModelLimits,
    ModelRole,
    ModelTier,
)

logger = structlog.get_logger()


class ModelCatalog:
    """In-memory catalog managing available ModelDescriptors."""

    def __init__(self) -> None:
        self._models: dict[str, ModelDescriptor] = {}

    def register(self, descriptor: ModelDescriptor) -> None:
        """Register a ModelDescriptor in the catalog."""
        self._models[descriptor.model_id] = descriptor
        logger.info(
            "Registered model in catalog",
            model_id=descriptor.model_id,
            provider_id=descriptor.provider_id,
            tier=descriptor.tier.value,
            role=descriptor.role.value,
            priority=descriptor.priority,
            quality_score=descriptor.quality_score,
            endpoint_verified=descriptor.endpoint_verified,
        )

    def get_model(self, model_id: str) -> ModelDescriptor:
        """Retrieve model descriptor by ID."""
        if model_id not in self._models:
            raise KeyError(f"Model '{model_id}' not found in ModelCatalog.")
        return self._models[model_id]

    def list_models(self) -> list[ModelDescriptor]:
        """List all active registered model descriptors."""
        return [m for m in self._models.values() if m.is_active]


def create_default_catalog() -> ModelCatalog:
    """Create and return default ModelCatalog seeded with Phase 6 production models.

    Phase 6 Architecture:
      PRIMARY (Attempted First for ALL Requests):
        - Nex N2.5 Pro (OpenRouter :free, tier=ALL, priority=300)

      LIGHT FALLBACK CHAIN (After Nex N2.5 Pro failure on FAST/LIGHT):
        1. Nemotron Lightning (OpenRouter :free, priority=220)
        2. Nemotron Lightning (NVIDIA Direct, priority=200)
        3. Gemma 3 4B (Ollama Offline, priority=100)

      HEAVY FALLBACK CHAIN (After Nex N2.5 Pro failure on HEAVY):
        1. Nemotron Ultra (OpenRouter :free, priority=230)
        2. Nemotron Ultra (NVIDIA Direct, priority=200)
        3. Kimi K3 (NVIDIA Direct, priority=170)
        4. DeepSeek V4 Pro (NVIDIA Direct, priority=150)
        5. DeepSeek V4 Flash (NVIDIA Direct, priority=130)
        6. Qwen 3 8B (Ollama Offline, priority=120)
    """
    catalog = ModelCatalog()

    # 1. PRIMARY MODEL — Attempted first for every request regardless of complexity
    catalog.register(
        ModelDescriptor(
            model_id="nex-agi/nex-n2.5-pro:free",
            provider_id="openrouter",
            tier=ModelTier.ALL,
            role=ModelRole.PRIMARY,
            capabilities=ModelCapabilities(
                reasoning=True,
                coding=True,
                vision=False,
                tool_calling=True,
                long_context=True,
            ),
            limits=ModelLimits(max_context_tokens=128000, max_output_tokens=4096),
            priority=300,
            # Inherited primary slot score; priority=300 is unique so quality_score does not alter routing decisions
            quality_score=95,
            endpoint_verified=True,
        )
    )

    # 2. LIGHT FALLBACK #1: OpenRouter Nemotron Lightning Free
    catalog.register(
        ModelDescriptor(
            model_id="nvidia/nemotron-3.5-lightning:free",
            provider_id="openrouter",
            tier=ModelTier.FAST,
            role=ModelRole.LIGHT_FALLBACK,
            capabilities=ModelCapabilities(
                reasoning=True,
                coding=True,
                vision=False,
                tool_calling=True,
                long_context=False,
            ),
            limits=ModelLimits(max_context_tokens=128000, max_output_tokens=4096),
            priority=220,
            quality_score=85,
            endpoint_verified=True,
        )
    )

    # 3. LIGHT FALLBACK #2: NVIDIA Direct Nemotron Lightning 30B
    catalog.register(
        ModelDescriptor(
            model_id="nvidia/nemotron-3.5-lightning-30b-a3b",
            provider_id="nvidia",
            tier=ModelTier.FAST,
            role=ModelRole.LIGHT_FALLBACK,
            capabilities=ModelCapabilities(
                reasoning=False,
                coding=True,
                vision=False,
                tool_calling=True,
                long_context=False,
            ),
            limits=ModelLimits(max_context_tokens=128000, max_output_tokens=4096),
            priority=200,
            quality_score=85,
            endpoint_verified=True,
        )
    )

    # 4. HEAVY FALLBACK #1: OpenRouter Nemotron Ultra 550B Free
    catalog.register(
        ModelDescriptor(
            model_id="nvidia/nemotron-3-ultra-550b-a55b:free",
            provider_id="openrouter",
            tier=ModelTier.HEAVY,
            role=ModelRole.HEAVY_FALLBACK,
            capabilities=ModelCapabilities(
                reasoning=True,
                coding=True,
                vision=False,
                tool_calling=True,
                long_context=True,
            ),
            limits=ModelLimits(max_context_tokens=128000, max_output_tokens=4096),
            priority=230,
            quality_score=95,
            endpoint_verified=True,
        )
    )

    # 5. HEAVY FALLBACK #2: NVIDIA Direct Nemotron Ultra 550B
    catalog.register(
        ModelDescriptor(
            model_id="nvidia/nemotron-3-ultra-550b-a55b",
            provider_id="nvidia",
            tier=ModelTier.HEAVY,
            role=ModelRole.HEAVY_FALLBACK,
            capabilities=ModelCapabilities(
                reasoning=True,
                coding=True,
                vision=False,
                tool_calling=True,
                long_context=True,
            ),
            limits=ModelLimits(max_context_tokens=128000, max_output_tokens=4096),
            priority=200,
            quality_score=95,
            endpoint_verified=True,
        )
    )

    # 6. HEAVY FALLBACK #3: NVIDIA Direct Kimi K3
    catalog.register(
        ModelDescriptor(
            model_id="moonshotai/kimi-k3",
            provider_id="nvidia",
            tier=ModelTier.HEAVY,
            role=ModelRole.HEAVY_FALLBACK,
            capabilities=ModelCapabilities(
                reasoning=True,
                coding=True,
                vision=False,
                tool_calling=True,
                long_context=True,
            ),
            limits=ModelLimits(max_context_tokens=128000, max_output_tokens=4096),
            priority=170,
            quality_score=87,
            endpoint_verified=False,
        )
    )

    # 7. HEAVY FALLBACK #4: DeepSeek V4 Pro
    catalog.register(
        ModelDescriptor(
            model_id="deepseek-ai/deepseek-v4-pro-0813",
            provider_id="nvidia",
            tier=ModelTier.HEAVY,
            role=ModelRole.HEAVY_FALLBACK,
            capabilities=ModelCapabilities(
                reasoning=True,
                coding=True,
                vision=False,
                tool_calling=True,
                long_context=True,
            ),
            limits=ModelLimits(max_context_tokens=128000, max_output_tokens=4096),
            priority=150,
            quality_score=92,
            endpoint_verified=False,
        )
    )

    # 8. HEAVY FALLBACK #5: DeepSeek V4 Flash
    catalog.register(
        ModelDescriptor(
            model_id="deepseek-ai/deepseek-v4-flash-0731",
            provider_id="nvidia",
            tier=ModelTier.HEAVY,
            role=ModelRole.HEAVY_FALLBACK,
            capabilities=ModelCapabilities(
                reasoning=True,
                coding=True,
                vision=False,
                tool_calling=True,
                long_context=True,
            ),
            limits=ModelLimits(max_context_tokens=128000, max_output_tokens=4096),
            priority=130,
            quality_score=90,
            endpoint_verified=False,
        )
    )

    # 9. LOCAL OFFLINE FAST: Gemma 3 4B (Ollama)
    catalog.register(
        ModelDescriptor(
            model_id="gemma3:4b",
            provider_id="ollama",
            tier=ModelTier.FAST,
            role=ModelRole.OFFLINE_FALLBACK,
            capabilities=ModelCapabilities(
                reasoning=False,
                coding=True,
                vision=False,
                tool_calling=False,
                long_context=False,
            ),
            limits=ModelLimits(max_context_tokens=128000, max_output_tokens=4096),
            priority=100,
            quality_score=80,
            endpoint_verified=True,
        )
    )

    # 10. LOCAL OFFLINE HEAVY: Qwen 3 8B (Ollama)
    catalog.register(
        ModelDescriptor(
            model_id="qwen3:8b",
            provider_id="ollama",
            tier=ModelTier.HEAVY,
            role=ModelRole.OFFLINE_FALLBACK,
            capabilities=ModelCapabilities(
                reasoning=True,
                coding=True,
                vision=False,
                tool_calling=True,
                long_context=True,
            ),
            limits=ModelLimits(max_context_tokens=128000, max_output_tokens=4096),
            priority=120,
            quality_score=85,
            endpoint_verified=True,
        )
    )

    return catalog


