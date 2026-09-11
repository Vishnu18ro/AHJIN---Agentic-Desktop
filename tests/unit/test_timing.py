"""Unit tests for ahjin.telemetry.timing — RequestTimer."""

from __future__ import annotations

import time

import pytest

from ahjin.telemetry.timing import (
    ORDERED_STAGES,
    STAGE_BERU_ANALYSIS,
    STAGE_CONTEXT_ASSEMBLY,
    STAGE_MODEL_GENERATION,
    STAGE_MODEL_ROUTING,
    STAGE_PROVIDER_SETUP,
    STAGE_STREAM_PROCESSING,
    STAGE_TELEGRAM_DELIVERY,
    STAGE_TELEGRAM_RECEIVE,
    STAGE_TIME_TO_FIRST_TOKEN,
    STAGE_TOOL_EXECUTION,
    STAGE_TOOL_PLANNER,
    STAGE_TOOL_RESOLVER,
    STAGE_TOOL_SCREENING,
    RequestTimer,
)


class TestRequestTimerBasic:
    """Basic recording behaviour."""

    def test_get_returns_zero_for_unrecorded_stage(self) -> None:
        timer = RequestTimer()
        assert timer.get("nonexistent_stage") == 0.0

    def test_record_stores_duration(self) -> None:
        timer = RequestTimer()
        timer.record(STAGE_BERU_ANALYSIS, 42.5)
        assert timer.get(STAGE_BERU_ANALYSIS) == pytest.approx(42.5)

    def test_record_accumulates_on_repeat_calls(self) -> None:
        timer = RequestTimer()
        timer.record(STAGE_TOOL_EXECUTION, 10.0)
        timer.record(STAGE_TOOL_EXECUTION, 20.0)
        assert timer.get(STAGE_TOOL_EXECUTION) == pytest.approx(30.0)

    def test_snapshot_returns_all_recorded_stages(self) -> None:
        timer = RequestTimer()
        timer.record(STAGE_BERU_ANALYSIS, 5.0)
        timer.record(STAGE_MODEL_GENERATION, 1000.0)
        snap = timer.snapshot()
        assert snap[STAGE_BERU_ANALYSIS] == pytest.approx(5.0)
        assert snap[STAGE_MODEL_GENERATION] == pytest.approx(1000.0)

    def test_snapshot_is_a_copy(self) -> None:
        timer = RequestTimer()
        timer.record(STAGE_BERU_ANALYSIS, 5.0)
        snap = timer.snapshot()
        snap["injected"] = 9999.0
        # Original timer should be unaffected
        assert "injected" not in timer.snapshot()


class TestRequestTimerStartEnd:
    """start_stage / end_stage timing via wall-clock."""

    def test_start_end_records_positive_duration(self) -> None:
        timer = RequestTimer()
        timer.start_stage(STAGE_BERU_ANALYSIS)
        time.sleep(0.01)  # 10 ms
        timer.end_stage(STAGE_BERU_ANALYSIS)
        elapsed = timer.get(STAGE_BERU_ANALYSIS)
        assert elapsed >= 5.0, f"Expected >= 5ms, got {elapsed}ms"
        assert elapsed < 500.0, f"Expected < 500ms, got {elapsed}ms"

    def test_end_without_start_is_safe(self) -> None:
        """end_stage without start_stage must not raise and must leave stage at 0."""
        timer = RequestTimer()
        timer.end_stage(STAGE_TOOL_SCREENING)  # no start — should be silently ignored
        assert timer.get(STAGE_TOOL_SCREENING) == 0.0

    def test_multiple_start_end_calls_accumulate(self) -> None:
        """Repeated start/end for the same stage should accumulate durations."""
        timer = RequestTimer()
        for _ in range(3):
            timer.start_stage(STAGE_TOOL_EXECUTION)
            time.sleep(0.005)
            timer.end_stage(STAGE_TOOL_EXECUTION)
        total = timer.get(STAGE_TOOL_EXECUTION)
        assert total >= 10.0, f"Expected >= 10ms total, got {total}ms"


class TestRequestTimerWallClock:
    """wall_elapsed_ms covers real elapsed time."""

    def test_wall_elapsed_increases_over_time(self) -> None:
        timer = RequestTimer()
        time.sleep(0.01)
        elapsed = timer.wall_elapsed_ms()
        assert elapsed >= 5.0


class TestRequestTimerFailureSafety:
    """Instrumentation must never raise even on misuse."""

    def test_record_with_negative_duration_is_accepted(self) -> None:
        """Negative durations are unusual but must not raise."""
        timer = RequestTimer()
        timer.record(STAGE_STREAM_PROCESSING, -1.0)  # should not raise

    def test_start_end_on_same_stage_twice_is_safe(self) -> None:
        timer = RequestTimer()
        timer.start_stage(STAGE_MODEL_ROUTING)
        timer.start_stage(STAGE_MODEL_ROUTING)  # overwrite start — fine
        timer.end_stage(STAGE_MODEL_ROUTING)
        assert timer.get(STAGE_MODEL_ROUTING) >= 0.0


class TestOrderedStages:
    """ORDERED_STAGES must contain all stage constants in the correct order."""

    def test_all_stage_constants_in_ordered_stages(self) -> None:
        all_expected = {
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
        }
        ordered_set = set(ORDERED_STAGES)
        missing = all_expected - ordered_set
        assert not missing, f"Stages missing from ORDERED_STAGES: {missing}"

    def test_telegram_receive_is_first(self) -> None:
        assert ORDERED_STAGES[0] == STAGE_TELEGRAM_RECEIVE

    def test_telegram_delivery_is_last(self) -> None:
        assert ORDERED_STAGES[-1] == STAGE_TELEGRAM_DELIVERY

    def test_ttft_between_provider_setup_and_model_generation(self) -> None:
        ordered = list(ORDERED_STAGES)
        idx_provider_setup = ordered.index(STAGE_PROVIDER_SETUP)
        idx_ttft = ordered.index(STAGE_TIME_TO_FIRST_TOKEN)
        idx_model_gen = ordered.index(STAGE_MODEL_GENERATION)
        assert idx_provider_setup < idx_ttft < idx_model_gen, (
            f"Expected provider_setup({idx_provider_setup}) < "
            f"ttft({idx_ttft}) < model_generation({idx_model_gen})"
        )

    def test_ordered_stages_count(self) -> None:
        assert len(ORDERED_STAGES) == 13, (
            f"Expected 13 stages, got {len(ORDERED_STAGES)}: {list(ORDERED_STAGES)}"
        )


class TestFooterIntegration:
    """Verify snapshot values are suitable for footer rendering."""

    def test_snapshot_only_contains_recorded_stages(self) -> None:
        timer = RequestTimer()
        timer.record(STAGE_MODEL_GENERATION, 8500.0)
        snap = timer.snapshot()
        # Only model_generation should be present
        assert list(snap.keys()) == [STAGE_MODEL_GENERATION]

    def test_zero_ms_stages_absent_from_snapshot_by_default(self) -> None:
        timer = RequestTimer()
        snap = timer.snapshot()
        assert STAGE_BERU_ANALYSIS not in snap

    def test_stage_filtering_for_footer(self) -> None:
        """Simulate footer rendering logic: only show stages > 0."""
        timer = RequestTimer()
        timer.record(STAGE_MODEL_GENERATION, 8500.0)
        timer.record(STAGE_BERU_ANALYSIS, 12.0)
        snap = timer.snapshot()
        visible = {k: v for k, v in snap.items() if v > 0}
        assert STAGE_MODEL_GENERATION in visible
        assert STAGE_BERU_ANALYSIS in visible
        assert STAGE_TOOL_EXECUTION not in visible


class TestToolTimings:
    """Tool execution tracking on RequestTimer."""

    def test_record_tool_appends_in_order(self) -> None:
        timer = RequestTimer()
        timer.record_tool("file_search", 420.0)
        timer.record_tool("file_read", 180.0)
        timer.record_tool("file_send", 310.0)
        timings = timer.get_tool_timings()
        assert len(timings) == 3
        assert timings[0] == ("file_search", 420.0)
        assert timings[1] == ("file_read", 180.0)
        assert timings[2] == ("file_send", 310.0)

    def test_get_tool_timings_returns_copy(self) -> None:
        timer = RequestTimer()
        timer.record_tool("system_info", 25.0)
        timings = timer.get_tool_timings()
        timings.append(("injected", 999.0))
        assert len(timer.get_tool_timings()) == 1


class TestSimplifiedFooterRendering:
    """Validate simplified user-facing Telegram footer formatting."""

    def test_footer_normal_request_without_tools(self) -> None:
        from ahjin.core.types import RuntimeInfo
        from ahjin.interfaces.telegram.bot import _build_runtime_footer

        info = RuntimeInfo(
            selected_model="nvidia/nemotron-3.5-lightning-30b-a3b",
            tier="FAST",
            provider_id="nvidia",
            total_ms=7310.0,
            timing={
                STAGE_BERU_ANALYSIS: 18.0,
                STAGE_CONTEXT_ASSEMBLY: 2.0,
                STAGE_MODEL_ROUTING: 1.0,
                STAGE_PROVIDER_SETUP: 0.0,
                STAGE_TELEGRAM_RECEIVE: 3.5,  # Excluded from user-facing AHJIN
                STAGE_STREAM_PROCESSING: 6840.0,
                STAGE_TIME_TO_FIRST_TOKEN: 800.0,  # Internal only
                STAGE_MODEL_GENERATION: 6040.0,  # Internal only
                STAGE_TELEGRAM_DELIVERY: 120.0,  # Internal only
            },
            health_status="HEALTHY",
        )
        footer = _build_runtime_footer(info)
        assert "Model: Nemotron Lightning 30B" in footer
        assert "Provider: NVIDIA" in footer
        assert "Route: FAST" in footer
        assert "⏱ Latency" in footer
        assert "├─ AHJIN: 21ms" in footer  # 18 + 2 + 1 = 21ms (excludes telegram_receive)
        assert "├─ Provider: 800ms" in footer
        assert "├─ Model: 6040ms" in footer
        assert "└─ Total: 7310ms" in footer
        assert "Tool:" not in footer
        assert "Tools:" not in footer
        assert "Path: Direct" in footer
        assert "Health: 🟢 Healthy" in footer

    def test_footer_single_tool_request(self) -> None:
        from ahjin.core.types import RuntimeInfo
        from ahjin.interfaces.telegram.bot import _build_runtime_footer

        info = RuntimeInfo(
            selected_model="nvidia/nemotron-3.5-lightning-30b-a3b",
            tier="FAST",
            provider_id="nvidia",
            total_ms=7310.0,
            timing={
                STAGE_BERU_ANALYSIS: 18.0,
                STAGE_CONTEXT_ASSEMBLY: 2.0,
                STAGE_MODEL_ROUTING: 1.0,
                STAGE_TOOL_EXECUTION: 420.0,
                STAGE_STREAM_PROCESSING: 6840.0,
            },
            tool_timings=[("system_info", 420.0)],
            executed_tools=["system_info"],
            health_status="HEALTHY",
        )
        footer = _build_runtime_footer(info)
        assert "├─ AHJIN: 21ms" in footer
        assert "├─ Tool: 420ms" in footer
        assert "├─ Model: 6840ms" in footer
        assert "└─ Total: 7310ms" in footer
        assert "Tool: System Info" in footer
        assert "Tools:" not in footer
        assert "Path: Direct" in footer
        assert "Health: 🟢 Healthy" in footer

    def test_footer_multiple_tools_request(self) -> None:
        from ahjin.core.types import RuntimeInfo
        from ahjin.interfaces.telegram.bot import _build_runtime_footer

        info = RuntimeInfo(
            selected_model="nvidia/nemotron-3.5-lightning-30b-a3b",
            tier="FAST",
            provider_id="nvidia",
            total_ms=7774.0,
            timing={
                STAGE_BERU_ANALYSIS: 20.0,
                STAGE_CONTEXT_ASSEMBLY: 3.0,
                STAGE_MODEL_ROUTING: 1.0,
                STAGE_TOOL_EXECUTION: 910.0,
                STAGE_STREAM_PROCESSING: 6840.0,
            },
            tool_timings=[
                ("file_search", 420.0),
                ("file_read", 180.0),
                ("file_send", 310.0),
            ],
            executed_tools=["file_search", "file_read", "file_send"],
            health_status="HEALTHY",
        )
        footer = _build_runtime_footer(info)
        assert "├─ AHJIN: 24ms" in footer
        assert "├─ File Search: 420ms" in footer
        assert "├─ File Read: 180ms" in footer
        assert "├─ File Send: 310ms" in footer
        assert "├─ Model: 6840ms" in footer
        assert "└─ Total: 7774ms" in footer
        assert "Tools: File Search, File Read, File Send" in footer
        assert "Path: Direct" in footer
        assert "Health: 🟢 Healthy" in footer

    def test_footer_milliseconds_only_no_seconds_format(self) -> None:
        """All latency numbers must end in 'ms', never 's' or decimal seconds."""
        from ahjin.core.types import RuntimeInfo
        from ahjin.interfaces.telegram.bot import _build_runtime_footer

        info = RuntimeInfo(
            selected_model="nvidia/nemotron-3.5-lightning-30b-a3b",
            tier="FAST",
            provider_id="nvidia",
            total_ms=28813.0,
            timing={
                STAGE_BERU_ANALYSIS: 5.0,
                STAGE_STREAM_PROCESSING: 28000.0,
            },
            health_status="HEALTHY",
        )
        footer = _build_runtime_footer(info)
        assert "28813ms" in footer
        assert "28000ms" in footer
        assert "5ms" in footer
        # Ensure no floating-point second representations appear in footer lines
        assert "28.8s" not in footer
        assert "28.0s" not in footer

    def test_footer_ahjin_latency_excludes_tool_planner(self) -> None:
        """ToolPlanner LLM duration must be subtracted from BERU to prevent double counting."""
        from ahjin.core.types import RuntimeInfo
        from ahjin.interfaces.telegram.bot import _build_runtime_footer

        info = RuntimeInfo(
            selected_model="nvidia/nemotron-3.5-lightning-30b-a3b",
            tier="FAST",
            provider_id="nvidia",
            total_ms=13031.0,
            timing={
                STAGE_BERU_ANALYSIS: 7025.0,  # includes 7000ms planner timeout
                STAGE_TOOL_PLANNER: 7000.0,   # LLM planner call to be subtracted
                STAGE_CONTEXT_ASSEMBLY: 3.0,
                STAGE_MODEL_ROUTING: 1.0,
                STAGE_PROVIDER_SETUP: 2.0,
                STAGE_STREAM_PROCESSING: 6000.0,
            },
            health_status="HEALTHY",
        )
        footer = _build_runtime_footer(info)
        # Expected AHJIN: (7025 - 7000) + 3 + 1 + 2 = 31ms
        assert "├─ AHJIN: 31ms" in footer
        assert "7025ms" not in footer
        # 7000ms must NOT appear in the AHJIN row (it must be subtracted from beru_analysis).
        # Phase 2 note: the 7000ms residual (Total 13031 - AHJIN 31 - Model 6000 = 7000ms)
        # MAY appear in the "Other" bucket — that is correct and expected behavior.
        # The key invariant is: the AHJIN row shows only 31ms, not 7025ms or 7000ms.
        assert "├─ AHJIN: 7000ms" not in footer
        assert "├─ AHJIN: 7025ms" not in footer
        assert "├─ Model: 6000ms" in footer
        assert "└─ Total: 13031ms" in footer

    def test_footer_integer_ms_accounting_strict(self) -> None:
        import re

        from ahjin.core.types import RuntimeInfo
        from ahjin.interfaces.telegram.bot import _build_runtime_footer

        info = RuntimeInfo(
            selected_model="nvidia/nemotron-3.5-lightning-30b-a3b",
            tier="FAST",
            provider_id="nvidia",
            total_ms=8500.4,
            timing={
                STAGE_BERU_ANALYSIS: 25.3,
                STAGE_TOOL_PLANNER: 10.1,
                STAGE_CONTEXT_ASSEMBLY: 2.4,
                STAGE_MODEL_ROUTING: 1.2,
                STAGE_PROVIDER_SETUP: 0.8,
                STAGE_TOOL_EXECUTION: 450.7,
                STAGE_STREAM_PROCESSING: 8000.1,
            },
            tool_timings=[("file_search", 450.7)],
            executed_tools=["file_search"],
            health_status="HEALTHY",
        )
        footer = _build_runtime_footer(info)

        # Extract all latency lines
        latency_pattern = re.compile(r"^[├└]─\s+([^:]+):\s+(\d+)ms$")
        latency_entries: dict[str, int] = {}
        for line in footer.splitlines():
            m = latency_pattern.match(line.strip())
            if m:
                label, val_str = m.groups()
                latency_entries[label] = int(val_str)

        assert "AHJIN" in latency_entries
        assert "Tool" in latency_entries
        assert "Model" in latency_entries
        assert "Total" in latency_entries

        # AHJIN = (25.3 - 10.1) + 2.4 + 1.2 + 0.8 = 19.6 -> 20ms
        assert latency_entries["AHJIN"] == 20
        assert latency_entries["Tool"] == 451
        assert latency_entries["Model"] == 8000
        assert latency_entries["Total"] == 8500
        # Disjoint buckets must not exceed Total
        assert (
            latency_entries["AHJIN"]
            + latency_entries["Tool"]
            + latency_entries["Model"]
            <= latency_entries["Total"]
        )

