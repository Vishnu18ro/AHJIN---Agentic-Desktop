"""Unit tests for LocalRoutingPolicy.

Verifies:
- Policy consumes BERU's ExecutionStrategy, not raw text.
- HEAVY / reasoning / code tasks route to Qwen.
- FAST / general tasks route to Gemma.
- Vision tasks skip local execution.
- Disabled policy skips local execution.
"""

from ahjin.beru.types import CapabilityRequirements, ExecutionStrategy
from ahjin.local.policy import LocalRoutingPolicy
from ahjin.local.types import LOCAL_GEMMA_MODEL_ID, LOCAL_QWEN_MODEL_ID
from ahjin.models.types import ModelTier


def _make_strategy(
    requires_reasoning: bool = False,
    requires_code: bool = False,
    requires_vision: bool = False,
    preferred_tier: str = ModelTier.FAST.value,
) -> ExecutionStrategy:
    """Build a minimal ExecutionStrategy mirroring BERU's output."""
    return ExecutionStrategy(
        capability_requirements=CapabilityRequirements(
            requires_reasoning=requires_reasoning,
            requires_code=requires_code,
            requires_vision=requires_vision,
        ),
        preferred_tier=preferred_tier,
    )


# ---------------------------------------------------------------------------
# Core decision tests
# ---------------------------------------------------------------------------


def test_heavy_reasoning_routes_to_qwen() -> None:
    """requires_reasoning=True → Qwen."""
    policy = LocalRoutingPolicy(enabled=True)
    strategy = _make_strategy(
        requires_reasoning=True, preferred_tier=ModelTier.HEAVY.value
    )
    decision = policy.decide(strategy)
    assert decision.use_local is True
    assert decision.model_id == LOCAL_QWEN_MODEL_ID
    assert decision.skip_reason is None


def test_heavy_coding_routes_to_qwen() -> None:
    """requires_code=True → Qwen."""
    policy = LocalRoutingPolicy(enabled=True)
    strategy = _make_strategy(
        requires_code=True, preferred_tier=ModelTier.HEAVY.value
    )
    decision = policy.decide(strategy)
    assert decision.use_local is True
    assert decision.model_id == LOCAL_QWEN_MODEL_ID


def test_heavy_tier_alone_routes_to_qwen() -> None:
    """preferred_tier=HEAVY alone (no specific cap flags) → Qwen."""
    policy = LocalRoutingPolicy(enabled=True)
    decision = policy.decide(_make_strategy(preferred_tier=ModelTier.HEAVY.value))
    assert decision.use_local is True
    assert decision.model_id == LOCAL_QWEN_MODEL_ID


def test_fast_general_routes_to_gemma() -> None:
    """No special requirements → Gemma."""
    policy = LocalRoutingPolicy(enabled=True)
    decision = policy.decide(_make_strategy())
    assert decision.use_local is True
    assert decision.model_id == LOCAL_GEMMA_MODEL_ID
    assert decision.skip_reason is None


def test_fast_tier_explicit_routes_to_gemma() -> None:
    """preferred_tier=FAST explicitly → Gemma."""
    policy = LocalRoutingPolicy(enabled=True)
    decision = policy.decide(_make_strategy(preferred_tier=ModelTier.FAST.value))
    assert decision.use_local is True
    assert decision.model_id == LOCAL_GEMMA_MODEL_ID


# ---------------------------------------------------------------------------
# Skip conditions
# ---------------------------------------------------------------------------


def test_vision_skips_local() -> None:
    """Vision requirement → use_local=False, correct skip_reason."""
    policy = LocalRoutingPolicy(enabled=True)
    decision = policy.decide(_make_strategy(requires_vision=True))
    assert decision.use_local is False
    assert decision.skip_reason == "no_local_vision_model"
    assert decision.model_id is None


def test_policy_disabled_skips_local() -> None:
    """Policy enabled=False → use_local=False regardless of requirements."""
    policy = LocalRoutingPolicy(enabled=False)
    # Even a FAST task should be skipped
    decision = policy.decide(_make_strategy())
    assert decision.use_local is False
    assert decision.skip_reason == "policy_disabled"


def test_policy_disabled_skips_heavy_too() -> None:
    """Disabled policy skips even HEAVY tasks."""
    policy = LocalRoutingPolicy(enabled=False)
    strategy = _make_strategy(
        requires_reasoning=True, preferred_tier=ModelTier.HEAVY.value
    )
    decision = policy.decide(strategy)
    assert decision.use_local is False
    assert decision.skip_reason == "policy_disabled"


# ---------------------------------------------------------------------------
# Interface contract test
# ---------------------------------------------------------------------------


def test_consumes_execution_strategy_not_raw_text() -> None:
    """Policy.decide() accepts ExecutionStrategy — no raw-text parameter."""
    policy = LocalRoutingPolicy()
    strategy = _make_strategy()
    # This must not raise TypeError — confirms the interface is correct
    decision = policy.decide(strategy)
    assert isinstance(decision.use_local, bool)


def test_enabled_property() -> None:
    """enabled property reflects constructor argument."""
    assert LocalRoutingPolicy(enabled=True).enabled is True
    assert LocalRoutingPolicy(enabled=False).enabled is False
