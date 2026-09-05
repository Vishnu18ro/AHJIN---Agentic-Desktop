"""ModelCatalog — In-memory registry of known model descriptors."""

import structlog

from ahjin.models.types import ModelCapabilities, ModelDescriptor, ModelLimits, ModelTier

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
    """Create and return default ModelCatalog seeded with candidate models.

    Active catalog (7 models):
      FAST  tier: Nemotron Lightning 30B (#1)
      HEAVY tier: MiniMax M3 (#1 via OpenRouter), Nemotron Ultra (#2 via OpenRouter),
                  Nemotron Ultra (#3 via NVIDIA Direct), Kimi K3 (#4 via NVIDIA Direct),
                  DeepSeek V4 Pro (#5), DeepSeek V4 Flash (#6)

    Explicit preference is encoded in ``priority``. The router ranking formula
    sorts strictly by priority (DESC) so that catalog preference ordering is always
    preserved among eligible candidates.
    """
    catalog = ModelCatalog()

    # 1. FAST EXECUTION TIER Candidate (Verified Active Endpoint)
    catalog.register(
        ModelDescriptor(
            model_id="nvidia/nemotron-3.5-lightning-30b-a3b",
            provider_id="nvidia",
            tier=ModelTier.FAST,
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

    # 2. HEAVY / CORE REASONING TIER Candidates — V1 Benchmark Priority Order:
    # #1 MiniMax M3 (250) -> #2 OpenRouter Nemotron Ultra (230) ->
    # #3 NVIDIA Nemotron Ultra (200) -> #4 Kimi K3 (170) ->
    # #5 DeepSeek Pro (150) -> #6 DeepSeek Flash (130)

    # Priority #1: OpenRouter MiniMax M3 Free (Fastest Reasoning Model: 5.0s)
    catalog.register(
        ModelDescriptor(
            model_id="minimax/minimax-m3:free",
            provider_id="openrouter",
            tier=ModelTier.HEAVY,
            capabilities=ModelCapabilities(
                reasoning=True,
                coding=True,
                vision=False,
                tool_calling=True,
                long_context=True,
            ),
            limits=ModelLimits(max_context_tokens=128000, max_output_tokens=4096),
            priority=250,
            quality_score=95,
            endpoint_verified=True,
        )
    )

    # Priority #2: OpenRouter Nemotron Ultra 550B Free (Reliable Fallback: 62.9s)
    catalog.register(
        ModelDescriptor(
            model_id="nvidia/nemotron-3-ultra-550b-a55b:free",
            provider_id="openrouter",
            tier=ModelTier.HEAVY,
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

    # Priority #3: NVIDIA Direct Nemotron Ultra 550B
    catalog.register(
        ModelDescriptor(
            model_id="nvidia/nemotron-3-ultra-550b-a55b",
            provider_id="nvidia",
            tier=ModelTier.HEAVY,
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

    # Priority #4: NVIDIA Direct Kimi K3
    catalog.register(
        ModelDescriptor(
            model_id="moonshotai/kimi-k3",
            provider_id="nvidia",
            tier=ModelTier.HEAVY,
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

    # Priority #5: DeepSeek V4 Pro
    catalog.register(
        ModelDescriptor(
            model_id="deepseek-ai/deepseek-v4-pro-0813",
            provider_id="nvidia",
            tier=ModelTier.HEAVY,
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

    # Priority #6: DeepSeek V4 Flash
    catalog.register(
        ModelDescriptor(
            model_id="deepseek-ai/deepseek-v4-flash-0731",
            provider_id="nvidia",
            tier=ModelTier.HEAVY,
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

    # 3. LOCAL OLLAMA MODELS (Registered descriptors; priorities below active cloud models)
    # Local Fast Model: Gemma 3 4B
    catalog.register(
        ModelDescriptor(
            model_id="gemma3:4b",
            provider_id="ollama",
            tier=ModelTier.FAST,
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

    # Local Heavy / Reasoning Model: Qwen 3 8B
    catalog.register(
        ModelDescriptor(
            model_id="qwen3:8b",
            provider_id="ollama",
            tier=ModelTier.HEAVY,
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


