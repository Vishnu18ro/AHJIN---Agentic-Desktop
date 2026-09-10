"""AHJIN Telemetry — Lightweight request timing instrumentation.

Design principles:
- Uses time.perf_counter() for monotonic, high-resolution timing.
- Async-safe: each RequestTimer is per-request and never shared across coroutines.
- Failure-proof: all public methods swallow exceptions so instrumentation cannot
  break or alter request behavior.
- Zero routing influence: purely observational; never consulted by routing, model
  selection, or fallback logic.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Stage names (string constants used as dict keys for zero-import convenience)
# ---------------------------------------------------------------------------

STAGE_BERU_ANALYSIS = "beru_analysis"
STAGE_TOOL_SCREENING = "tool_screening"
STAGE_TOOL_PLANNER = "tool_planner"
STAGE_TOOL_RESOLVER = "tool_resolver"
STAGE_TOOL_EXECUTION = "tool_execution"
STAGE_CONTEXT_ASSEMBLY = "context_assembly"
STAGE_MODEL_ROUTING = "model_routing"
STAGE_PROVIDER_SETUP = "provider_setup"
STAGE_TIME_TO_FIRST_TOKEN = "time_to_first_token"
STAGE_MODEL_GENERATION = "model_generation"
STAGE_STREAM_PROCESSING = "stream_processing"
STAGE_TELEGRAM_RECEIVE = "telegram_receive"
STAGE_TELEGRAM_DELIVERY = "telegram_delivery"

# Internal granular telemetry stages (internal only; excluded from ORDERED_STAGES
# to preserve the user-facing footer and existing test assertions)
STAGE_TELEGRAM_PLACEHOLDER = "telegram_placeholder"
STAGE_PROVIDER_REQUEST_START = "provider_request_start"
STAGE_HTTP_CONNECT = "http_connect"
STAGE_FIRST_SSE = "first_sse"
STAGE_FIRST_REASONING = "first_reasoning"
STAGE_REASONING_DURATION = "reasoning_duration"
STAGE_FIRST_VISIBLE_CONTENT = "first_visible_content"
STAGE_VISIBLE_GENERATION = "visible_generation"
STAGE_STREAM_COMPLETION = "stream_completion"
STAGE_FINAL_RESPONSE_ASSEMBLY = "final_response_assembly"

# Ordered stage list for footer rendering (matches the design requirement order)
ORDERED_STAGES: tuple[str, ...] = (
    STAGE_TELEGRAM_RECEIVE,
    STAGE_BERU_ANALYSIS,
    STAGE_TOOL_SCREENING,
    STAGE_TOOL_PLANNER,
    STAGE_TOOL_RESOLVER,
    STAGE_TOOL_EXECUTION,
    STAGE_CONTEXT_ASSEMBLY,
    STAGE_MODEL_ROUTING,
    STAGE_PROVIDER_SETUP,
    STAGE_TIME_TO_FIRST_TOKEN,
    STAGE_MODEL_GENERATION,
    STAGE_STREAM_PROCESSING,
    STAGE_TELEGRAM_DELIVERY,
)

# Human-readable display labels for each stage
STAGE_LABELS: dict[str, str] = {
    STAGE_TELEGRAM_RECEIVE: "Telegram Receive",
    STAGE_BERU_ANALYSIS: "BERU Analysis",
    STAGE_TOOL_SCREENING: "Tool Screening",
    STAGE_TOOL_PLANNER: "Tool Planner",
    STAGE_TOOL_RESOLVER: "Tool Resolver",
    STAGE_TOOL_EXECUTION: "Tool Execution",
    STAGE_CONTEXT_ASSEMBLY: "Context Assembly",
    STAGE_MODEL_ROUTING: "Model Routing",
    STAGE_PROVIDER_SETUP: "Provider Setup",
    STAGE_TIME_TO_FIRST_TOKEN: "Time to First Token",
    STAGE_MODEL_GENERATION: "Model Generation",
    STAGE_STREAM_PROCESSING: "Stream Processing",
    STAGE_TELEGRAM_DELIVERY: "Telegram Delivery",
    STAGE_TELEGRAM_PLACEHOLDER: "Telegram Placeholder",
    STAGE_PROVIDER_REQUEST_START: "Provider Request Start",
    STAGE_HTTP_CONNECT: "HTTP Connect",
    STAGE_FIRST_SSE: "First SSE Chunk",
    STAGE_FIRST_REASONING: "First Reasoning Chunk",
    STAGE_REASONING_DURATION: "Reasoning Phase Duration",
    STAGE_FIRST_VISIBLE_CONTENT: "First Visible Content Chunk",
    STAGE_VISIBLE_GENERATION: "Visible Generation Duration",
    STAGE_STREAM_COMPLETION: "Stream Completion",
    STAGE_FINAL_RESPONSE_ASSEMBLY: "Final Response Assembly",
}



@dataclass
class RequestTimer:
    """Per-request timing accumulator.

    All durations are stored and exposed in milliseconds.

    Usage::

        timer = RequestTimer()
        timer.start_stage("beru_analysis")
        ...
        timer.end_stage("beru_analysis")

        # Or record a pre-measured duration directly:
        timer.record("model_generation", 8500.0)

        snapshot = timer.snapshot()  # dict[str, float]

    This class is NOT thread-safe across multiple concurrent coroutines.
    Each request must use its own ``RequestTimer`` instance.
    """

    _durations: dict[str, float] = field(default_factory=dict)  # pyright: ignore[reportUnknownVariableType]
    _starts: dict[str, float] = field(default_factory=dict)  # pyright: ignore[reportUnknownVariableType]
    _wall_start: float = field(default_factory=time.perf_counter)
    _tool_timings: list[tuple[str, float]] = field(default_factory=list)  # pyright: ignore[reportUnknownVariableType]

    def start_stage(self, stage: str) -> None:
        """Record the start time of a named stage.

        Safe to call multiple times — subsequent calls overwrite the start.
        """
        try:
            self._starts[stage] = time.perf_counter()
        except Exception:  # pragma: no cover
            pass

    def end_stage(self, stage: str) -> None:
        """Record the end of a named stage and store its duration.

        Silently skips if ``start_stage`` was never called for this stage.
        Does NOT raise on any error.
        """
        try:
            t_start = self._starts.get(stage)
            if t_start is not None:
                elapsed_ms = (time.perf_counter() - t_start) * 1000.0
                # Accumulate if already recorded (e.g., multiple tool steps)
                self._durations[stage] = self._durations.get(stage, 0.0) + elapsed_ms
        except Exception:  # pragma: no cover
            pass

    def record(self, stage: str, duration_ms: float) -> None:
        """Directly record a duration (in milliseconds) for a stage.

        Useful when the caller has already measured elapsed time independently.
        Accumulates on repeated calls (e.g., multiple tool executions).
        """
        try:
            self._durations[stage] = self._durations.get(stage, 0.0) + duration_ms
        except Exception:  # pragma: no cover
            pass

    def record_tool(self, tool_name: str, duration_ms: float) -> None:
        """Record the execution duration (in milliseconds) for an individual tool.

        Appends to the internal ordered list of tool executions.
        """
        try:
            self._tool_timings.append((tool_name, float(duration_ms)))
        except Exception:  # pragma: no cover
            pass

    def get_tool_timings(self) -> list[tuple[str, float]]:
        """Return a copy of all recorded individual tool timings in milliseconds."""
        try:
            return list(self._tool_timings)
        except Exception:  # pragma: no cover
            return []

    def get(self, stage: str) -> float:
        """Return recorded milliseconds for a stage, or 0.0 if not recorded."""
        try:
            return self._durations.get(stage, 0.0)
        except Exception:  # pragma: no cover
            return 0.0

    def wall_elapsed_ms(self) -> float:
        """Return total wall-clock elapsed milliseconds since timer was created."""
        try:
            return (time.perf_counter() - self._wall_start) * 1000.0
        except Exception:  # pragma: no cover
            return 0.0

    def snapshot(self) -> dict[str, float]:
        """Return a shallow copy of all recorded stage durations in milliseconds."""
        try:
            return dict(self._durations)
        except Exception:  # pragma: no cover
            return {}

