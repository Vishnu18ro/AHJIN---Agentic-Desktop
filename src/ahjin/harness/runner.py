"""Harness Runner — Step sequencing, verification, and same-request recovery engine.

Execution contract:
- require_verification from ExecutionStrategy is ENFORCED:
  if False → verifier is skipped entirely.
- recovery_policy from ExecutionStrategy is ENFORCED:
  FAIL_FAST → no rerouting, first failure returns immediately.
  REROUTE   → same-request rerouting up to max_recovery_attempts.
- Failed model identity comes from exc.model_id (attached by ProviderGateway).
- excluded_models is request-local; no global shared exclusion state.
- RuntimeInfo is populated after each invocation for Telegram observability.
"""

import asyncio
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, cast

import httpx
import structlog

from ahjin.beru.types import (
    ExecutionPlan,
    PlanStep,
    RecoveryPolicy,
    StepType,
)
from ahjin.core.errors import AhjinError, ErrorCategory
from ahjin.core.types import RuntimeInfo, TaskContext, TaskResult
from ahjin.harness.connectivity import ConnectivityChecker
from ahjin.harness.context import ContextAssembler
from ahjin.harness.gateway import ProviderGateway
from ahjin.harness.state import ExecutionState, StepResult
from ahjin.harness.verifier import ResponseVerifier, VerificationError
from ahjin.local.executor import LocalExecutor
from ahjin.local.types import LocalExecutionError, LocalExecutionResult, LocalRoutingSkipped
from ahjin.models.router import CapabilityUnavailableError
from ahjin.security.gate import PermissionGate
from ahjin.tools.registry import ToolRegistry

logger = structlog.get_logger()


class HarnessRunner:
    """Executes ExecutionPlan steps reliably with verification and same-request rerouting."""

    def __init__(
        self,
        context_assembler: ContextAssembler | None = None,
        gateway: ProviderGateway | None = None,
        verifier: ResponseVerifier | None = None,
        local_executor: LocalExecutor | None = None,
        connectivity_checker: ConnectivityChecker | None = None,
        tool_registry: ToolRegistry | None = None,
        permission_gate: PermissionGate | None = None,
    ) -> None:
        self.context_assembler = context_assembler or ContextAssembler()
        self.gateway = gateway or ProviderGateway()
        self.verifier = verifier or ResponseVerifier()
        # Optional local execution layer (Phase 5).
        # When None, behaviour is 100% identical to pre-Phase-5.
        self.local_executor = local_executor
        self.connectivity_checker = connectivity_checker or ConnectivityChecker()
        self.tool_registry = tool_registry
        self.permission_gate = permission_gate

    async def run(self, plan: ExecutionPlan, context: TaskContext) -> TaskResult:
        """Run execution plan steps sequentially with same-request failure recovery."""
        logger.info("Harness running plan", plan_id=str(plan.plan_id), steps=len(plan.steps))
        state = ExecutionState(task_id=plan.task_id, plan_id=plan.plan_id)

        last_output: str | None = None
        runtime_info: RuntimeInfo | None = None
        local_escalation_hint: LocalExecutionResult | None = None

        for step in plan.steps:
            if step.step_type == StepType.MODEL_INVOCATION and step.model_intent:
                intent = step.model_intent
                strategy = intent.execution_strategy

                # Strategy fields that govern execution behaviour
                max_attempts: int = strategy.max_recovery_attempts
                require_verification: bool = strategy.require_verification
                recovery_policy: RecoveryPolicy = strategy.recovery_policy

                t0_ctx = time.monotonic()
                prompt = self.context_assembler.assemble(
                    intent=intent,
                    task_context=context,
                    prior_results=state.step_results,
                )
                t_ctx_ms = (time.monotonic() - t0_ctx) * 1000.0
                logger.info(
                    "[PROFILE] ContextAssembler execution",
                    step_id=str(step.step_id),
                    context_assembly_ms=round(t_ctx_ms, 3),
                )

                excluded_models: set[str] = set()
                attempts = 0
                step_success = False

                first_failed_model: str | None = None
                first_failure_reason: str | None = None
                cloud_error: Exception | None = None
                step_t0 = time.monotonic()

                # ── CLOUD EXECUTION LOOP (ONLINE FIRST) ──────────────────────────────
                # When online, AHJIN uses its global cloud model fleet via ModelRouter.
                # If a cloud model fails, the existing cloud reroute loop tries other cloud models.
                # Local Gemma/Qwen are NEVER invoked while cloud models remain viable.

                is_online = self.connectivity_checker.is_online()

                if is_online:
                    while attempts < max_attempts and not step_success:
                        attempts += 1
                        t0_gw = time.monotonic()
                        try:
                            gw_result = await self.gateway.invoke(
                                prompt=prompt,
                                requirements=strategy,
                                excluded_model_ids=excluded_models,
                            )
                            t_gw_ms = (time.monotonic() - t0_gw) * 1000.0
                            response = gw_result.response
                            selection = gw_result.selection

                            logger.info(
                                "[PROFILE] ProviderGateway execution",
                                step_id=str(step.step_id),
                                gateway_invoke_ms=round(t_gw_ms, 3),
                                attempt=attempts,
                                model_id=response.model_id,
                            )

                            if require_verification:
                                ver_res = self.verifier.verify(response.content)
                                if not ver_res.is_valid:
                                    raise VerificationError(
                                        f"Verification failed: {ver_res.reason}",
                                        model_id=response.model_id,
                                    )

                            step_res = StepResult(
                                step_id=step.step_id,
                                success=True,
                                output_text=response.content,
                            )
                            last_output = response.content
                            step_success = True
                            state.step_results.append(step_res)

                            step_total_ms = (time.monotonic() - step_t0) * 1000.0
                            ahjin_overhead_ms = step_total_ms - response.latency_ms
                            health_state = self.gateway.router.health_tracker.get_state(
                                response.model_id
                            )
                            runtime_info = RuntimeInfo(
                                selected_model=response.model_id,
                                tier=selection.tier.value,
                                provider_id=selection.provider_id,
                                ahjin_internal_ms=round(max(ahjin_overhead_ms, 0.0), 1),
                                model_api_ms=round(response.latency_ms, 1),
                                total_ms=round(step_total_ms, 1),
                                was_rerouted=(first_failed_model is not None),
                                failed_model=first_failed_model,
                                failure_reason=first_failure_reason,
                                health_status=health_state.snapshot_status.value,
                            )

                        except asyncio.CancelledError:
                            logger.warning("Step cancelled", step_id=str(step.step_id))
                            raise
                        except (
                            httpx.HTTPStatusError,
                            httpx.RequestError,
                            VerificationError,
                            CapabilityUnavailableError,
                        ) as exc:
                            cloud_error = exc
                            failed_model = getattr(exc, "model_id", None)
                            if failed_model:
                                excluded_models.add(str(failed_model))
                                if first_failed_model is None:
                                    first_failed_model = str(failed_model)
                                    first_failure_reason = _classify_failure_reason(exc)

                            logger.warning(
                                "[PROFILE] Model invocation failed — checking recovery",
                                step_id=str(step.step_id),
                                attempt=attempts,
                                max_attempts=max_attempts,
                                recovery_policy=recovery_policy.value,
                                error=str(exc),
                            )

                            if recovery_policy == RecoveryPolicy.FAIL_FAST:
                                break

                            if (
                                attempts >= max_attempts
                                or isinstance(exc, CapabilityUnavailableError)
                            ):
                                break

                # ── OFFLINE / LOCAL FALLBACK ─────────────────────────────────────────
                # Attempted ONLY when offline OR when entire cloud fleet has failed/been exhausted.
                if not step_success and self.local_executor is not None:
                    try:
                        local_result = await self.local_executor.invoke(
                            prompt=prompt,
                            strategy=strategy,
                        )
                        step_total_ms = local_result.latency_ms
                        runtime_info = RuntimeInfo(
                            selected_model=local_result.model_used,
                            tier=(
                                "FAST"
                                if local_result.model_used == "gemma3:4b"
                                else "HEAVY"
                            ),
                            provider_id="ollama",
                            ahjin_internal_ms=0.0,
                            model_api_ms=round(local_result.latency_ms, 1),
                            total_ms=round(step_total_ms, 1),
                            was_rerouted=(
                            local_result.used_fallback
                            or (first_failed_model is not None)
                        ),
                            failed_model=first_failed_model or (
                                local_result.attempted_model
                                if local_result.used_fallback
                                else None
                            ),
                            failure_reason=first_failure_reason or local_result.fallback_reason,
                            health_status="LOCAL",
                        )
                        if local_result.suggest_escalation:
                            local_escalation_hint = local_result

                        step_res = StepResult(
                            step_id=step.step_id,
                            success=True,
                            output_text=local_result.output_text,
                        )
                        last_output = local_result.output_text
                        step_success = True
                        state.step_results.append(step_res)

                        logger.info(
                            "Local execution succeeded",
                            model_used=local_result.model_used,
                            used_fallback=local_result.used_fallback,
                            suggest_escalation=local_result.suggest_escalation,
                        )

                    except LocalRoutingSkipped as skip_exc:
                        logger.info(
                            "Local routing skipped",
                            reason=skip_exc.reason,
                        )
                    except LocalExecutionError as local_err:
                        logger.warning(
                            "Local execution failed",
                            error=local_err.reason,
                        )

                if not step_success:
                    exc_to_report = cloud_error or RuntimeError("All execution paths failed")
                    err = AhjinError(
                        category=ErrorCategory.PROVIDER,
                        code="INVOCATION_FAILED",
                        message=str(exc_to_report),
                        is_retryable=isinstance(exc_to_report, httpx.RequestError),
                    )
                    step_res = StepResult(
                        step_id=step.step_id,
                        success=False,
                        error=err,
                    )
                    state.step_results.append(step_res)
                    return TaskResult(
                        task_id=plan.task_id,
                        correlation_id=plan.correlation_id,
                        success=False,
                        error=err,
                    )
            elif step.step_type == StepType.TOOL_INVOCATION:
                step_res = await self._execute_tool_step(step)
                state.step_results.append(step_res)
                if step_res.output_text is not None:
                    last_output = step_res.output_text

        return TaskResult(
            task_id=plan.task_id,
            correlation_id=plan.correlation_id,
            success=True,
            output_text=last_output,
            runtime_info=runtime_info,
            local_escalation_hint=local_escalation_hint,
        )

    async def run_stream(
        self, plan: ExecutionPlan, context: TaskContext
    ) -> AsyncGenerator[tuple[str, TaskResult | None], None]:
        """Run execution plan steps with progressive streaming of output chunks."""
        logger.info(
            "Harness running plan with streaming",
            plan_id=str(plan.plan_id),
            steps=len(plan.steps),
        )
        state = ExecutionState(task_id=plan.task_id, plan_id=plan.plan_id)

        last_output: str | None = None
        runtime_info: RuntimeInfo | None = None
        local_escalation_hint: LocalExecutionResult | None = None

        for step in plan.steps:
            if step.step_type == StepType.MODEL_INVOCATION and step.model_intent:
                intent = step.model_intent
                strategy = intent.execution_strategy

                max_attempts: int = strategy.max_recovery_attempts
                require_verification: bool = strategy.require_verification
                recovery_policy: RecoveryPolicy = strategy.recovery_policy

                prompt = self.context_assembler.assemble(
                    intent=intent,
                    task_context=context,
                    prior_results=state.step_results,
                )

                excluded_models: set[str] = set()
                attempts = 0
                step_success = False

                first_failed_model: str | None = None
                first_failure_reason: str | None = None
                cloud_error: Exception | None = None
                step_t0 = time.monotonic()

                is_online = self.connectivity_checker.is_online()

                if is_online:
                    while attempts < max_attempts and not step_success:
                        attempts += 1
                        try:
                            accumulated_content: list[str] = []
                            last_selection = None

                            async for chunk, selection in self.gateway.invoke_stream(
                                prompt=prompt,
                                requirements=strategy,
                                excluded_model_ids=excluded_models,
                            ):
                                last_selection = selection
                                accumulated_content.append(chunk)
                                yield chunk, None

                            full_response = "".join(accumulated_content)
                            if require_verification:
                                ver_res = self.verifier.verify(full_response)
                                if not ver_res.is_valid:
                                    raise VerificationError(
                                        f"Verification failed: {ver_res.reason}",
                                        model_id=last_selection.model_id if last_selection else "",
                                    )

                            step_res = StepResult(
                                step_id=step.step_id,
                                success=True,
                                output_text=full_response,
                            )
                            last_output = full_response
                            step_success = True
                            state.step_results.append(step_res)

                            step_total_ms = (time.monotonic() - step_t0) * 1000.0
                            if last_selection:
                                health_state = self.gateway.router.health_tracker.get_state(
                                    last_selection.model_id
                                )
                                runtime_info = RuntimeInfo(
                                    selected_model=last_selection.model_id,
                                    tier=last_selection.tier.value,
                                    provider_id=last_selection.provider_id,
                                    ahjin_internal_ms=0.0,
                                    model_api_ms=round(step_total_ms, 1),
                                    total_ms=round(step_total_ms, 1),
                                    was_rerouted=(first_failed_model is not None),
                                    failed_model=first_failed_model,
                                    failure_reason=first_failure_reason,
                                    health_status=health_state.snapshot_status.value,
                                )

                        except asyncio.CancelledError:
                            logger.warning("Step stream cancelled", step_id=str(step.step_id))
                            raise
                        except (
                            httpx.HTTPStatusError,
                            httpx.RequestError,
                            VerificationError,
                            CapabilityUnavailableError,
                        ) as exc:
                            cloud_error = exc
                            failed_model = getattr(exc, "model_id", None)
                            if failed_model:
                                excluded_models.add(str(failed_model))
                                if first_failed_model is None:
                                    first_failed_model = str(failed_model)
                                    first_failure_reason = _classify_failure_reason(exc)

                            if recovery_policy == RecoveryPolicy.FAIL_FAST:
                                break

                            if (
                                attempts >= max_attempts
                                or isinstance(exc, CapabilityUnavailableError)
                            ):
                                break

                # ── OFFLINE / LOCAL FALLBACK ─────────────────────────────────────────
                if not step_success and self.local_executor is not None:
                    try:
                        last_local_result = None
                        async for chunk, local_result in self.local_executor.invoke_stream(
                            prompt=prompt,
                            strategy=strategy,
                        ):
                            last_local_result = local_result
                            yield chunk, None

                        if last_local_result:
                            step_total_ms = last_local_result.latency_ms
                            runtime_info = RuntimeInfo(
                                selected_model=last_local_result.model_used,
                                tier=(
                                    "FAST"
                                    if last_local_result.model_used == "gemma3:4b"
                                    else "HEAVY"
                                ),
                                provider_id="ollama",
                                ahjin_internal_ms=0.0,
                                model_api_ms=round(last_local_result.latency_ms, 1),
                                total_ms=round(step_total_ms, 1),
                                was_rerouted=(
                                    last_local_result.used_fallback
                                    or (first_failed_model is not None)
                                ),
                                failed_model=first_failed_model or (
                                    last_local_result.attempted_model
                                    if last_local_result.used_fallback
                                    else None
                                ),
                                failure_reason=(
                                    first_failure_reason or last_local_result.fallback_reason
                                ),
                                health_status="LOCAL",
                            )
                            if last_local_result.suggest_escalation:
                                local_escalation_hint = last_local_result

                            step_res = StepResult(
                                step_id=step.step_id,
                                success=True,
                                output_text=last_local_result.output_text,
                            )
                            last_output = last_local_result.output_text
                            step_success = True
                            state.step_results.append(step_res)

                    except LocalRoutingSkipped as skip_exc:
                        logger.info("Local routing skipped", reason=skip_exc.reason)
                    except LocalExecutionError as local_err:
                        logger.warning("Local execution failed", error=local_err.reason)

                if not step_success:
                    exc_to_report = cloud_error or RuntimeError("All execution paths failed")
                    err = AhjinError(
                        category=ErrorCategory.PROVIDER,
                        code="INVOCATION_FAILED",
                        message=str(exc_to_report),
                        is_retryable=isinstance(exc_to_report, httpx.RequestError),
                    )
                    step_res = StepResult(
                        step_id=step.step_id,
                        success=False,
                        error=err,
                    )
                    state.step_results.append(step_res)
                    final_task_result = TaskResult(
                        task_id=plan.task_id,
                        correlation_id=plan.correlation_id,
                        success=False,
                        error=err,
                    )
                    yield "", final_task_result
                    return
            elif step.step_type == StepType.TOOL_INVOCATION:
                step_res = await self._execute_tool_step(step)
                state.step_results.append(step_res)
                if step_res.output_text is not None:
                    last_output = step_res.output_text

        file_attachments: list[Path] = [
            att_path for s_res in state.step_results for att_path in s_res.attachment_paths
        ]
        final_task_result = TaskResult(
            task_id=plan.task_id,
            correlation_id=plan.correlation_id,
            success=True,
            output_text=last_output,
            runtime_info=runtime_info,
            file_attachments=file_attachments,
            local_escalation_hint=local_escalation_hint,
        )
        yield "", final_task_result

    async def _execute_tool_step(self, step: PlanStep) -> StepResult:
        """Execute a TOOL_INVOCATION step with permission checking and registry lookup."""
        if not step.tool_intent:
            err = AhjinError(
                category=ErrorCategory.VALIDATION,
                code="MISSING_TOOL_INTENT",
                message=f"PlanStep {step.step_id} of type TOOL_INVOCATION is missing tool_intent.",
            )
            return StepResult(step_id=step.step_id, success=False, error=err)

        tool_name = step.tool_intent.tool_name
        parameters = step.tool_intent.parameters

        if self.permission_gate is None:
            err = AhjinError(
                category=ErrorCategory.TOOL,
                code="PERMISSION_GATE_MISSING",
                message=f"PermissionGate is not configured; cannot authorize tool '{tool_name}'.",
            )
            return StepResult(step_id=step.step_id, success=False, error=err)

        authorized = await self.permission_gate.check_permission(tool_name, parameters)
        if not authorized:
            logger.warning("Permission denied for tool execution", tool_name=tool_name)
            err = AhjinError(
                category=ErrorCategory.TOOL,
                code="PERMISSION_DENIED",
                message=f"Permission denied for tool '{tool_name}'.",
            )
            return StepResult(step_id=step.step_id, success=False, error=err)

        if self.tool_registry is None or not self.tool_registry.has_tool(tool_name):
            logger.warning("Tool not found in registry", tool_name=tool_name)
            err = AhjinError(
                category=ErrorCategory.TOOL,
                code="TOOL_NOT_FOUND",
                message=f"Tool '{tool_name}' not found in ToolRegistry.",
            )
            return StepResult(step_id=step.step_id, success=False, error=err)

        tool = self.tool_registry.get_tool(tool_name)
        try:
            res = await tool.execute(step.tool_intent)
            output_str: str | None = None
            attachment_paths: list[Path] = []
            output_obj: object = res.output
            if isinstance(output_obj, dict):
                res_dict = cast(dict[str, Any], output_obj)
                output_str = str(res_dict.get("text", ""))
                raw_paths = res_dict.get("attachment_paths", [])
                if isinstance(raw_paths, list):
                    for p in cast(list[Any], raw_paths):
                        if isinstance(p, (str, Path)):
                            attachment_paths.append(Path(p))
            elif res.output is not None:
                output_str = str(res.output)

            return StepResult(
                step_id=step.step_id,
                success=res.success,
                output_text=output_str,
                error=res.error,
                attachment_paths=attachment_paths,
            )
        except Exception as exc:
            logger.error(
                "Unhandled error during tool execution",
                tool_name=tool_name,
                error=str(exc),
            )
            err = AhjinError(
                category=ErrorCategory.INTERNAL,
                code="TOOL_EXECUTION_EXCEPTION",
                message=f"Tool '{tool_name}' raised exception: {exc}",
            )
            return StepResult(step_id=step.step_id, success=False, error=err)


def _classify_failure_reason(exc: Exception) -> str:
    """Classify the failure reason for observability reporting.

    Returns a human-readable string describing the actual cause.
    Never invents reasons — reports what the exception type indicates.
    """
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.RequestError):
        return "network error"
    if isinstance(exc, VerificationError):
        return "verification failure"
    if isinstance(exc, CapabilityUnavailableError):
        return "capability unavailable"
    return "provider error"
