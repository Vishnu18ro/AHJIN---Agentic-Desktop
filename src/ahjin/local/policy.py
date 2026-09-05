"""LocalRoutingPolicy — BERU-output-aware local model selection.

Consumes the ExecutionStrategy already produced by BERU.
Does NOT re-analyse raw user text. BERU is the sole task intelligence layer.

Decision logic reads from strategy.capability_requirements and
strategy.preferred_tier — the authoritative BERU outputs.
"""

import structlog

from ahjin.beru.types import ExecutionStrategy
from ahjin.local.types import LOCAL_GEMMA_MODEL_ID, LOCAL_QWEN_MODEL_ID, LocalRoutingDecision
from ahjin.models.types import ModelTier

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Local model IDs re-exported from types for test convenience.
# Tests should import directly from ahjin.local.types.
# ---------------------------------------------------------------------------
_GEMMA_MODEL_ID = LOCAL_GEMMA_MODEL_ID
_QWEN_MODEL_ID = LOCAL_QWEN_MODEL_ID


class LocalRoutingPolicy:
    """Decides whether a request should be executed locally and selects the model.

    Inputs: BERU's computed ExecutionStrategy.
    Outputs: LocalRoutingDecision.

    This class contains ZERO keyword analysis and ZERO raw-text inspection.
    BERU already did that work; we read its authoritative output.
    """

    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    def decide(self, strategy: ExecutionStrategy) -> LocalRoutingDecision:
        """Return a LocalRoutingDecision based on BERU's ExecutionStrategy.

        Decision table (evaluated in order):

        1. Policy disabled                → use_local=False ("policy_disabled")
        2. requires_vision                → use_local=False ("no_local_vision_model")
        3. HEAVY tier / reasoning / code  → use_local=True, model=qwen3:8b
        4. Otherwise (FAST / general)     → use_local=True, model=gemma3:4b
        """
        reqs = strategy.capability_requirements

        if not self._enabled:
            logger.debug("Local routing policy disabled — skipping local execution")
            return LocalRoutingDecision(use_local=False, skip_reason="policy_disabled")

        if reqs.requires_vision:
            logger.debug(
                "Vision capability required — no local vision model available",
                skip_reason="no_local_vision_model",
            )
            return LocalRoutingDecision(
                use_local=False, skip_reason="no_local_vision_model"
            )

        # HEAVY tier or demanding capabilities → Qwen
        prefer_heavy = (
            strategy.preferred_tier == ModelTier.HEAVY.value
            or reqs.requires_reasoning
            or reqs.requires_code
        )

        if prefer_heavy:
            logger.debug(
                "Local routing decision: Qwen 3 8B",
                preferred_tier=strategy.preferred_tier,
                requires_reasoning=reqs.requires_reasoning,
                requires_code=reqs.requires_code,
            )
            return LocalRoutingDecision(use_local=True, model_id=LOCAL_QWEN_MODEL_ID)

        logger.debug(
            "Local routing decision: Gemma 3 4B",
            preferred_tier=strategy.preferred_tier,
        )
        return LocalRoutingDecision(use_local=True, model_id=LOCAL_GEMMA_MODEL_ID)
