"""Core Dispatcher — Entry point for TaskRequest routing.

Pure dispatcher logic. Contains zero cognitive orchestration or business decisions.
Delegates cognitive planning exclusively to BERU.
"""

import time
from collections.abc import AsyncGenerator

import structlog

from ahjin.beru.orchestrator import BeruOrchestrator
from ahjin.core.types import TaskRequest, TaskResult
from ahjin.harness.runner import HarnessRunner

logger = structlog.get_logger()


class TaskDispatcher:
    """Dispatches TaskRequests to BERU and Harness."""

    def __init__(
        self,
        orchestrator: BeruOrchestrator | None = None,
        runner: HarnessRunner | None = None,
    ) -> None:
        self.orchestrator = orchestrator or BeruOrchestrator()
        self.runner = runner or HarnessRunner()

    async def dispatch(self, request: TaskRequest) -> TaskResult:
        """Route request through BERU -> Harness -> Result."""
        t0_disp = time.monotonic()
        logger.info(
            "[PROFILE] Dispatcher starting task",
            task_id=str(request.task_id),
            correlation_id=str(request.correlation_id),
        )

        # 1. BERU creates execution plan
        t0_beru = time.monotonic()
        plan = await self.orchestrator.plan(request)
        t_beru_ms = (time.monotonic() - t0_beru) * 1000.0

        # 2. Harness executes plan
        t0_harness = time.monotonic()
        result = await self.runner.run(plan, request.context)
        t_harness_ms = (time.monotonic() - t0_harness) * 1000.0

        t_total_ms = (time.monotonic() - t0_disp) * 1000.0
        logger.info(
            "[PROFILE] Dispatcher completed task",
            task_id=str(request.task_id),
            beru_planning_ms=round(t_beru_ms, 3),
            harness_execution_ms=round(t_harness_ms, 3),
            dispatcher_total_ms=round(t_total_ms, 3),
            success=result.success,
        )
        return result

    async def dispatch_stream(
        self, request: TaskRequest
    ) -> AsyncGenerator[tuple[str, TaskResult | None], None]:
        """Route request through BERU -> Harness (streaming) -> Result."""
        t0_disp = time.monotonic()
        logger.info(
            "[PROFILE] Dispatcher starting streaming task",
            task_id=str(request.task_id),
            correlation_id=str(request.correlation_id),
        )

        plan = await self.orchestrator.plan(request)

        async for chunk, task_result in self.runner.run_stream(plan, request.context):
            yield chunk, task_result

        t_total_ms = (time.monotonic() - t0_disp) * 1000.0
        logger.info(
            "[PROFILE] Dispatcher completed streaming task",
            task_id=str(request.task_id),
            dispatcher_total_ms=round(t_total_ms, 3),
        )
