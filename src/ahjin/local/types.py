"""Local routing layer — type definitions and model ID constants.

All data contracts for the local execution subsystem live here.
No imports from ahjin.local sub-modules; only imports from core/providers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Local model IDs — single source of truth for the local fleet.
# All local/ modules import these; they must never be duplicated.
# ---------------------------------------------------------------------------

LOCAL_GEMMA_MODEL_ID = "gemma3:4b"
LOCAL_QWEN_MODEL_ID = "qwen3:8b"

# ---------------------------------------------------------------------------
# Routing decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LocalRoutingDecision:
    """Decision produced by LocalRoutingPolicy.

    ``use_local=True``  — execute locally; ``model_id`` names the local model.
    ``use_local=False`` — skip local execution; ``skip_reason`` explains why.
    """

    use_local: bool
    model_id: str | None = None          # "gemma3:4b" | "qwen3:8b" | None
    skip_reason: str | None = None       # set when use_local=False


# ---------------------------------------------------------------------------
# Execution result
# ---------------------------------------------------------------------------


@dataclass
class LocalExecutionResult:
    """Result returned by LocalExecutor on successful local execution.

    ``output_text``        — final answer (includes fallback notice when used_fallback=True).
    ``model_used``         — actual model that produced the answer.
    ``used_fallback``      — True when Qwen timed out and Gemma answered instead.
    ``fallback_reason``    — "qwen_timeout" | None.
    ``latency_ms``         — wall-clock time for the successful local invocation.
    ``suggest_escalation`` — True when a higher-model opinion may be warranted.
    ``escalation_reason``  — "qwen_timeout_gemma_fallback" | None.
    """

    output_text: str
    model_used: str
    latency_ms: float
    used_fallback: bool = False
    fallback_reason: str | None = None
    suggest_escalation: bool = False
    escalation_reason: str | None = None
    # Internal: preserve which model was originally attempted (for observability)
    attempted_model: str | None = field(default=None)


# ---------------------------------------------------------------------------
# Sentinel exceptions
# ---------------------------------------------------------------------------


class LocalRoutingSkipped(Exception):
    """Raised by LocalExecutor when LocalRoutingPolicy decides local is inappropriate.

    HarnessRunner catches this and falls through to the existing cloud path.
    Carries a human-readable reason for structured logging.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class LocalExecutionError(Exception):
    """Raised by LocalExecutor when local execution fails and cloud should be attempted.

    Distinct from LocalRoutingSkipped: skipped = policy choice, error = runtime failure.
    HarnessRunner catches this and falls through to the existing cloud REROUTE loop.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
