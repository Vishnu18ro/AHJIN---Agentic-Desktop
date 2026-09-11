"""ModelRouter — Capability-aware, tier-prioritized, health-safe model selection engine.

Zero LLM calls, zero network I/O, zero database queries during request-time routing.
Pure in-memory deterministic scoring.

Golden Rule:
HARD CAPABILITIES DETERMINE ELIGIBILITY.
MODEL STRENGTH DETERMINES PRIORITY AMONG ELIGIBLE MODELS.

Routing pipeline:
1. Hard Capability Gate   — eliminates incapable models (ELIGIBILITY)
2. Health/Excluded Filter — eliminates unhealthy or already-failed models
3. Hard Latency Constraint — eliminates models violating explicit max_latency_ms
4. Tier Match             — prefer requested tier; fall back to all healthy if unavailable
5. Ranking Pass           — priority is the primary ordinal key; quality_score (and
                            quality_preference) breaks ties within the same-priority group
"""

import time
from typing import NamedTuple

import structlog

from ahjin.beru.types import CapabilityRequirements, ExecutionStrategy
from ahjin.models.catalog import ModelCatalog, create_default_catalog
from ahjin.models.health import ModelHealthTracker
from ahjin.models.types import ModelDescriptor, ModelTier

logger = structlog.get_logger()


class CapabilityUnavailableError(Exception):
    """Raised when no available healthy model satisfies explicit required capabilities.

    Prevents implicit capability degradation.
    """

    def __init__(self, requirements: CapabilityRequirements) -> None:
        super().__init__(
            f"No healthy model available satisfying requirements: {requirements.model_dump()}"
        )
        self.requirements = requirements


class ModelSelectionResult(NamedTuple):
    """Result of ModelRouter selection."""

    provider_id: str
    model_id: str
    tier: ModelTier
    selection_time_ms: float
    max_output_tokens: int


class ModelRouter:
    """Capability-aware, tier-prioritized model router."""

    def __init__(
        self,
        catalog: ModelCatalog | None = None,
        health_tracker: ModelHealthTracker | None = None,
    ) -> None:
        self.catalog = catalog if catalog is not None else create_default_catalog()
        self.health_tracker = health_tracker or ModelHealthTracker()

    def select_model(
        self,
        strategy_or_requirements: ExecutionStrategy | CapabilityRequirements,
        excluded_model_ids: set[str] | None = None,
    ) -> ModelSelectionResult:
        """Select optimal model matching requirements and policy.

        1. Capability Gate (HARD ELIGIBILITY): Rejects any model missing a required capability.
        2. Health & Excluded Filter: Removes unhealthy or already-failed models.
        3. Hard Latency Constraint: Removes models violating explicit max_latency_ms.
        4. Tier Match: Prefer requested tier; fall back to all eligible if unavailable.
        5. Ranking Pass: Ranks ELIGIBLE models by quality_score weighted by quality_preference.

        Pure in-memory evaluation.
        """
        t0 = time.monotonic()
        excluded = excluded_model_ids or set()

        # Unpack strategy fields
        if isinstance(strategy_or_requirements, ExecutionStrategy):
            requirements = strategy_or_requirements.capability_requirements
            target_tier = ModelTier(strategy_or_requirements.preferred_tier)
            quality_preference: str = strategy_or_requirements.quality_preference
        else:
            requirements = strategy_or_requirements
            prefer_heavy = requirements.requires_reasoning or requirements.requires_code
            target_tier = ModelTier.HEAVY if prefer_heavy else ModelTier.FAST
            quality_preference = "balanced"

        all_models = self.catalog.list_models()

        if not all_models:
            raise RuntimeError("ModelCatalog is empty. Register models before routing.")

        # 1. HARD CAPABILITY GATE — Hard requirements determine ELIGIBILITY.
        # A stronger model missing a required capability MUST NEVER beat a capable model.
        capable_models: list[ModelDescriptor] = []
        for model in all_models:
            caps = model.capabilities
            if requirements.requires_code and not caps.coding:
                continue
            if requirements.requires_vision and not caps.vision:
                continue
            if requirements.requires_reasoning and not caps.reasoning:
                continue
            capable_models.append(model)

        # 2. Capability Safety Guard — do not silently degrade capabilities
        if not capable_models:
            logger.error(
                "No models in catalog support requested capabilities",
                requirements=requirements.model_dump(),
            )
            raise CapabilityUnavailableError(requirements)

        # 3. Health & Excluded Models Filter
        healthy_models = [
            m
            for m in capable_models
            if m.model_id not in excluded
            and self.health_tracker.get_state(m.model_id).is_available()
        ]

        if not healthy_models:
            logger.error(
                "All capable models are currently UNHEALTHY or excluded",
                requirements=requirements.model_dump(),
                total_capable=len(capable_models),
                excluded_count=len(excluded),
            )
            raise CapabilityUnavailableError(requirements)

        # 4. Hard Latency Constraint — enforce max_latency_ms if specified.
        #    We use observed EMA latency as the best available runtime estimate.
        #    Only applied when EMA latency has been observed (> 0) so that models
        #    with no recorded latency are still considered (give them a chance).
        max_lat = requirements.max_latency_ms
        if max_lat is not None and max_lat > 0:
            latency_filtered = [
                m
                for m in healthy_models
                if self.health_tracker.get_state(m.model_id).snapshot_ema_latency_ms == 0.0
                or self.health_tracker.get_state(m.model_id).snapshot_ema_latency_ms <= max_lat
            ]
            # Only apply the filter if at least one model passes; otherwise fall back to all
            # healthy to avoid total unavailability due to stale latency data.
            if latency_filtered:
                healthy_models = latency_filtered
            else:
                logger.warning(
                    "max_latency_ms constraint would eliminate all candidates; "
                    "relaxing latency filter to avoid starvation",
                    max_latency_ms=max_lat,
                )

        # 5. Tier Determination (Match target tier or ModelTier.ALL if possible, else fallback)
        tier_matched = [
            m for m in healthy_models if m.tier == target_tier or m.tier == ModelTier.ALL
        ]
        candidates = tier_matched if tier_matched else healthy_models

        # 6. RANKING PASS — Two-key sort: catalog preference ordinal first, then quality.
        #
        # Key 1 — ``priority`` (DESC): Explicit catalog preference order encoded by the operator.
        #   A higher-priority model ALWAYS beats a lower-priority model among eligible candidates,
        #   regardless of quality_score or quality_preference. No arithmetic constant required.
        #
        # Key 2 — blended score (DESC): Used ONLY to break ties within the same-priority group.
        #   quality_preference adjusts the weight given to quality_score vs. speed (latency):
        #     "quality"  → maximize quality_score; latency penalty is minimal
        #     "speed"    → heavy latency penalty; quality_score weight is reduced
        #     "balanced" → moderate on both axes (default)
        #   endpoint_verified contributes only a +0.001 micro tie-breaker.

        if quality_preference == "speed":
            quality_weight = 1.0
            latency_penalty = 0.1   # strong latency penalty
        elif quality_preference == "quality":
            quality_weight = 3.0
            latency_penalty = 0.001  # almost no latency penalty
        else:  # "balanced"
            quality_weight = 2.0
            latency_penalty = 0.01

        def _blended_score(model: ModelDescriptor) -> float:
            health = self.health_tracker.get_state(model.model_id)
            score = float(model.quality_score) * quality_weight
            ema = health.snapshot_ema_latency_ms
            if ema > 0:
                score -= latency_penalty * ema
            if model.endpoint_verified:
                score += 0.001  # micro tie-breaker only
            return score

        # Stable two-key sort: primary = priority DESC, secondary = blended_score DESC.
        # Python's sort is stable, so equal-priority models preserve insertion order as a tertiary.
        best_model = max(candidates, key=lambda m: (m.priority, _blended_score(m)))

        assert best_model is not None

        elapsed_ms = (time.monotonic() - t0) * 1000.0
        reported_tier = target_tier if best_model.tier == ModelTier.ALL else best_model.tier

        logger.info(
            "[PROFILE] ModelRouter selection complete",
            selected_model=best_model.model_id,
            provider_id=best_model.provider_id,
            tier=reported_tier.value,
            target_tier=target_tier.value,
            quality_score=best_model.quality_score,
            quality_preference=quality_preference,
            selection_time_ms=round(elapsed_ms, 3),
        )

        return ModelSelectionResult(
            provider_id=best_model.provider_id,
            model_id=best_model.model_id,
            tier=reported_tier,
            selection_time_ms=elapsed_ms,
            max_output_tokens=best_model.limits.max_output_tokens,
        )
