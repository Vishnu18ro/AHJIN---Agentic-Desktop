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
from ahjin.telemetry import (
    STAGE_CONTEXT_ASSEMBLY,
    STAGE_MODEL_GENERATION,
    STAGE_MODEL_ROUTING,
    STAGE_PROVIDER_SETUP,
    STAGE_STREAM_PROCESSING,
    STAGE_TIME_TO_FIRST_TOKEN,
    STAGE_TOOL_EXECUTION,
    RequestTimer,
)
from ahjin.tools.registry import ToolRegistry

logger = structlog.get_logger()


def _extract_tool_metadata(
    state_step_results: list[StepResult], timer: RequestTimer | None
) -> tuple[list[tuple[str, float]], list[str]]:
    """Extract ordered tool execution timings and names from timer or state results."""
    timings = (
        timer.get_tool_timings()
        if timer is not None and timer.get_tool_timings()
        else [
            (s.tool_name, s.tool_duration_ms)
            for s in state_step_results
            if s.tool_name is not None
        ]
    )
    names = [t[0] for t in timings]
    return timings, names


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

    async def run(
        self,
        plan: ExecutionPlan,
        context: TaskContext,
        timer: RequestTimer | None = None,
    ) -> TaskResult:
        """Run execution plan steps sequentially with same-request failure recovery."""
        logger.info("Harness running plan", plan_id=str(plan.plan_id), steps=len(plan.steps))
        state = ExecutionState(task_id=plan.task_id, plan_id=plan.plan_id)

        last_output: str | None = None
        runtime_info: RuntimeInfo | None = None
        local_escalation_hint: LocalExecutionResult | None = None

        for step in plan.steps:
            if step.deterministic_output is not None:
                step_res = StepResult(
                    step_id=step.step_id,
                    success=True,
                    output_text=step.deterministic_output,
                )
                last_output = step.deterministic_output
                state.step_results.append(step_res)
                runtime_info = RuntimeInfo(
                    selected_model="deterministic",
                    tier="FAST",
                    provider_id="ahjin",
                    ahjin_internal_ms=0.0,
                    model_api_ms=0.0,
                    total_ms=0.0,
                    tool_timings=[],
                    executed_tools=[],
                    was_rerouted=False,
                    failed_model=None,
                    failure_reason=None,
                    health_status="HEALTHY",
                )
                continue

            if step.step_type == StepType.MODEL_INVOCATION and step.model_intent:
                intent = step.model_intent
                strategy = intent.execution_strategy

                # Check if prior tool satisfied request without needing model generation
                prior_successful_tools = [r for r in state.step_results if r.success]
                has_file_send = any(
                    r.tool_name == "file_send" or bool(r.attachment_paths)
                    for r in prior_successful_tools
                )
                has_ambiguity = any(r.is_ambiguous for r in state.step_results)
                requires_reasoning = intent.capability_requirements.requires_reasoning

                if has_ambiguity and not requires_reasoning:
                    ambig_res = next(r for r in state.step_results if r.is_ambiguous)
                    step_res = StepResult(
                        step_id=step.step_id,
                        success=True,
                        output_text=ambig_res.output_text,
                    )
                    last_output = ambig_res.output_text
                    state.step_results.append(step_res)

                    tool_timings, executed_tools = _extract_tool_metadata(
                        state.step_results, timer
                    )
                    total_tool_ms = sum(t[1] for t in tool_timings) if tool_timings else 0.0
                    runtime_info = RuntimeInfo(
                        selected_model="deterministic",
                        tier="FAST",
                        provider_id="ahjin",
                        ahjin_internal_ms=0.0,
                        model_api_ms=0.0,
                        total_ms=round(total_tool_ms, 1),
                        tool_timings=tool_timings,
                        executed_tools=executed_tools,
                        was_rerouted=False,
                        failed_model=None,
                        failure_reason=None,
                        health_status="HEALTHY",
                    )
                    continue

                if has_file_send and not requires_reasoning:
                    send_res = next(
                        r for r in reversed(prior_successful_tools)
                        if r.tool_name == "file_send" or r.attachment_paths
                    )
                    att_names = [p.name for p in send_res.attachment_paths]
                    if len(att_names) == 1:
                        confirm_text = f"Sent {att_names[0]} 📄"
                    elif len(att_names) > 1:
                        confirm_text = f"Sent {len(att_names)} files: {', '.join(att_names)} 📄"
                    else:
                        confirm_text = "File sent successfully 📄"

                    step_res = StepResult(
                        step_id=step.step_id,
                        success=True,
                        output_text=confirm_text,
                    )
                    last_output = confirm_text
                    state.step_results.append(step_res)

                    tool_timings, executed_tools = _extract_tool_metadata(
                        state.step_results, timer
                    )
                    total_tool_ms = sum(t[1] for t in tool_timings) if tool_timings else 0.0
                    runtime_info = RuntimeInfo(
                        selected_model="deterministic",
                        tier="FAST",
                        provider_id="ahjin",
                        ahjin_internal_ms=0.0,
                        model_api_ms=0.0,
                        total_ms=round(total_tool_ms, 1),
                        tool_timings=tool_timings,
                        executed_tools=executed_tools,
                        was_rerouted=False,
                        failed_model=None,
                        failure_reason=None,
                        health_status="HEALTHY",
                    )
                    continue

                # Strategy fields that govern execution behaviour
                max_attempts: int = strategy.max_recovery_attempts
                require_verification: bool = strategy.require_verification
                recovery_policy: RecoveryPolicy = strategy.recovery_policy

                if timer is not None:
                    timer.start_stage(STAGE_CONTEXT_ASSEMBLY)
                t0_ctx = time.monotonic()
                prompt = self.context_assembler.assemble(
                    intent=intent,
                    task_context=context,
                    prior_results=state.step_results,
                )
                t_ctx_ms = (time.monotonic() - t0_ctx) * 1000.0
                if timer is not None:
                    timer.end_stage(STAGE_CONTEXT_ASSEMBLY)
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
                            if timer is not None:
                                # model_routing: only the ModelRouter.select_model() time,
                                # already measured inside the router and exposed on selection.
                                timer.record(
                                    STAGE_MODEL_ROUTING,
                                    gw_result.selection.selection_time_ms,
                                )
                                # model_generation: the actual provider HTTP round-trip.
                                timer.record(
                                    STAGE_MODEL_GENERATION,
                                    gw_result.response.latency_ms,
                                )
                                # provider_setup: gateway overhead that is neither router
                                # selection nor provider generation (HTTP client init,
                                # request serialisation, provider lookup, etc.).
                                provider_setup_ms = (
                                    t_gw_ms
                                    - gw_result.selection.selection_time_ms
                                    - gw_result.response.latency_ms
                                )
                                if provider_setup_ms > 0:
                                    timer.record(STAGE_PROVIDER_SETUP, provider_setup_ms)
                                # Attach internal provider telemetry if available
                                try:
                                    prov_id = gw_result.selection.provider_id
                                    prov = self.gateway.registry.get_provider(prov_id)
                                    last_tel: Any = getattr(prov, "last_telemetry", None)
                                    if isinstance(last_tel, dict):
                                        tel_dict: dict[str, float] = (
                                            cast(dict[str, float], last_tel)
                                        )
                                        for stage_k, stage_v in tel_dict.items():
                                            timer.record(str(stage_k), float(stage_v))
                                except Exception:
                                    pass
                                # time_to_first_token: not applicable for non-streaming invoke.
                                # Left unrecorded; footer will show SKIPPED for this stage.
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
                            tool_timings, executed_tools = _extract_tool_metadata(
                                state.step_results, timer
                            )
                            runtime_info = RuntimeInfo(
                                selected_model=response.model_id,
                                tier=selection.tier.value,
                                provider_id=selection.provider_id,
                                ahjin_internal_ms=round(max(ahjin_overhead_ms, 0.0), 1),
                                model_api_ms=round(response.latency_ms, 1),
                                total_ms=round(step_total_ms, 1),
                                tool_timings=tool_timings,
                                executed_tools=executed_tools,
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
                        tool_timings, executed_tools = _extract_tool_metadata(
                            state.step_results, timer
                        )
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
                            tool_timings=tool_timings,
                            executed_tools=executed_tools,
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
                if step.tool_intent:
                    tool_name = step.tool_intent.tool_name
                    prior_search = next(
                        (r for r in reversed(state.step_results) if r.tool_name == "file_search"),
                        None,
                    )
                    if prior_search is not None and tool_name in ("file_send", "file_read"):
                        if prior_search.is_ambiguous:
                            ambig_msg = (
                                prior_search.output_text
                                or (
                                    "Multiple matching files found. "
                                    "Please specify which file you would like."
                                )
                            )
                            step_res = StepResult(
                                step_id=step.step_id,
                                success=False,
                                is_ambiguous=True,
                                output_text=ambig_msg,
                                tool_name=tool_name,
                            )
                            last_output = ambig_msg
                            state.step_results.append(step_res)
                            continue

                        if prior_search.discovered_paths:
                            discovered_path = str(prior_search.discovered_paths[0])
                            step.tool_intent.parameters["path"] = discovered_path
                            step.tool_intent.parameters.pop("query", None)
                        else:
                            q_str = step.tool_intent.parameters.get("query", "")
                            not_found_msg = (
                                prior_search.output_text
                                or f"No matching files found for '{q_str}'."
                            )
                            step_res = StepResult(
                                step_id=step.step_id,
                                success=False,
                                output_text=not_found_msg,
                                tool_name=tool_name,
                            )
                            last_output = not_found_msg
                            state.step_results.append(step_res)
                            continue

                t0_tool = time.perf_counter()
                if timer is not None:
                    timer.start_stage(STAGE_TOOL_EXECUTION)
                step_res = await self._execute_tool_step(step)
                tool_elapsed_ms = (time.perf_counter() - t0_tool) * 1000.0
                step_res.tool_duration_ms = tool_elapsed_ms
                if timer is not None:
                    timer.end_stage(STAGE_TOOL_EXECUTION)
                    tool_name = step.tool_intent.tool_name if step.tool_intent else "tool"
                    timer.record_tool(tool_name, tool_elapsed_ms)
                state.step_results.append(step_res)
                if step_res.output_text is not None:
                    last_output = step_res.output_text

        file_attachments: list[Path] = [
            att_path for s_res in state.step_results for att_path in s_res.attachment_paths
        ]
        return TaskResult(
            task_id=plan.task_id,
            correlation_id=plan.correlation_id,
            success=True,
            output_text=last_output,
            runtime_info=runtime_info,
            file_attachments=file_attachments,
            local_escalation_hint=local_escalation_hint,
        )

    async def run_stream(
        self,
        plan: ExecutionPlan,
        context: TaskContext,
        timer: RequestTimer | None = None,
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
            if step.deterministic_output is not None:
                step_res = StepResult(
                    step_id=step.step_id,
                    success=True,
                    output_text=step.deterministic_output,
                )
                last_output = step.deterministic_output
                state.step_results.append(step_res)
                runtime_info = RuntimeInfo(
                    selected_model="deterministic",
                    tier="FAST",
                    provider_id="ahjin",
                    ahjin_internal_ms=0.0,
                    model_api_ms=0.0,
                    total_ms=0.0,
                    tool_timings=[],
                    executed_tools=[],
                    was_rerouted=False,
                    failed_model=None,
                    failure_reason=None,
                    health_status="HEALTHY",
                )
                yield step.deterministic_output, None
                continue

            if step.step_type == StepType.MODEL_INVOCATION and step.model_intent:
                intent = step.model_intent
                strategy = intent.execution_strategy

                # Check if prior tool satisfied request without needing model generation
                prior_successful_tools = [r for r in state.step_results if r.success]
                has_file_send = any(
                    r.tool_name == "file_send" or bool(r.attachment_paths)
                    for r in prior_successful_tools
                )
                has_ambiguity = any(r.is_ambiguous for r in state.step_results)
                requires_reasoning = intent.capability_requirements.requires_reasoning

                if has_ambiguity and not requires_reasoning:
                    ambig_res = next(r for r in state.step_results if r.is_ambiguous)
                    yield ambig_res.output_text or "", None
                    step_res = StepResult(
                        step_id=step.step_id,
                        success=True,
                        output_text=ambig_res.output_text,
                    )
                    last_output = ambig_res.output_text
                    state.step_results.append(step_res)

                    tool_timings, executed_tools = _extract_tool_metadata(
                        state.step_results, timer
                    )
                    total_tool_ms = sum(t[1] for t in tool_timings) if tool_timings else 0.0
                    runtime_info = RuntimeInfo(
                        selected_model="deterministic",
                        tier="FAST",
                        provider_id="ahjin",
                        ahjin_internal_ms=0.0,
                        model_api_ms=0.0,
                        total_ms=round(total_tool_ms, 1),
                        tool_timings=tool_timings,
                        executed_tools=executed_tools,
                        was_rerouted=False,
                        failed_model=None,
                        failure_reason=None,
                        health_status="HEALTHY",
                    )
                    continue

                if has_file_send and not requires_reasoning:
                    send_res = next(
                        r for r in reversed(prior_successful_tools)
                        if r.tool_name == "file_send" or r.attachment_paths
                    )
                    att_names = [p.name for p in send_res.attachment_paths]
                    if len(att_names) == 1:
                        confirm_text = f"Sent {att_names[0]} 📄"
                    elif len(att_names) > 1:
                        confirm_text = f"Sent {len(att_names)} files: {', '.join(att_names)} 📄"
                    else:
                        confirm_text = "File sent successfully 📄"

                    yield confirm_text, None

                    step_res = StepResult(
                        step_id=step.step_id,
                        success=True,
                        output_text=confirm_text,
                    )
                    last_output = confirm_text
                    state.step_results.append(step_res)

                    tool_timings, executed_tools = _extract_tool_metadata(
                        state.step_results, timer
                    )
                    total_tool_ms = sum(t[1] for t in tool_timings) if tool_timings else 0.0
                    runtime_info = RuntimeInfo(
                        selected_model="deterministic",
                        tier="FAST",
                        provider_id="ahjin",
                        ahjin_internal_ms=0.0,
                        model_api_ms=0.0,
                        total_ms=round(total_tool_ms, 1),
                        tool_timings=tool_timings,
                        executed_tools=executed_tools,
                        was_rerouted=False,
                        failed_model=None,
                        failure_reason=None,
                        health_status="HEALTHY",
                    )
                    continue

                max_attempts: int = strategy.max_recovery_attempts
                require_verification: bool = strategy.require_verification
                recovery_policy: RecoveryPolicy = strategy.recovery_policy

                if timer is not None:
                    timer.start_stage(STAGE_CONTEXT_ASSEMBLY)
                prompt = self.context_assembler.assemble(
                    intent=intent,
                    task_context=context,
                    prior_results=state.step_results,
                )
                if timer is not None:
                    timer.end_stage(STAGE_CONTEXT_ASSEMBLY)

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
                            t0_stream = time.monotonic()
                            first_token = True
                            t0_first_token: float = t0_stream  # wall-clock when stream call starts

                            async for chunk, selection in self.gateway.invoke_stream(
                                prompt=prompt,
                                requirements=strategy,
                                excluded_model_ids=excluded_models,
                            ):
                                if first_token:
                                    t_first_token = time.monotonic()
                                    if timer is not None:
                                        # model_routing: pure ModelRouter.select_model() time,
                                        # already measured by the router; exposed on selection.
                                        timer.record(
                                            STAGE_MODEL_ROUTING,
                                            selection.selection_time_ms,
                                        )
                                        # time_to_first_token: wall time from invoke_stream()
                                        # call until first content chunk arrives in the runner.
                                        # Includes HTTP connection + provider_setup internally,
                                        # but provider_setup cannot be exposed without modifying
                                        # the provider — left SKIPPED in footer.
                                        ttft_ms = (t_first_token - t0_first_token) * 1000.0
                                        timer.record(STAGE_TIME_TO_FIRST_TOKEN, ttft_ms)
                                    first_token = False
                                last_selection = selection
                                accumulated_content.append(chunk)
                                yield chunk, None

                            full_response = "".join(accumulated_content)
                            t_stream_ms = (time.monotonic() - t0_stream) * 1000.0
                            if timer is not None:
                                # model_generation: time from first token to last token.
                                t_first_token_elapsed = timer.get(STAGE_TIME_TO_FIRST_TOKEN)
                                model_gen_ms = t_stream_ms - t_first_token_elapsed
                                if model_gen_ms > 0:
                                    timer.record(STAGE_MODEL_GENERATION, model_gen_ms)
                                # stream_processing: total wall-clock for the entire stream.
                                timer.record(STAGE_STREAM_PROCESSING, t_stream_ms)
                                # Attach internal provider telemetry if available
                                if last_selection is not None:
                                    try:
                                        prov_id = last_selection.provider_id
                                        prov = self.gateway.registry.get_provider(prov_id)
                                        last_tel: Any = getattr(prov, "last_telemetry", None)
                                        if isinstance(last_tel, dict):
                                            tel_dict: dict[str, float] = (
                                                cast(dict[str, float], last_tel)
                                            )
                                            for stage_k, stage_v in tel_dict.items():
                                                timer.record(str(stage_k), float(stage_v))
                                    except Exception:
                                        pass
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
                                tool_timings, executed_tools = _extract_tool_metadata(
                                    state.step_results, timer
                                )
                                runtime_info = RuntimeInfo(
                                    selected_model=last_selection.model_id,
                                    tier=last_selection.tier.value,
                                    provider_id=last_selection.provider_id,
                                    ahjin_internal_ms=0.0,
                                    model_api_ms=round(t_stream_ms, 1),
                                    total_ms=round(step_total_ms, 1),
                                    tool_timings=tool_timings,
                                    executed_tools=executed_tools,
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
                            tool_timings, executed_tools = _extract_tool_metadata(
                                state.step_results, timer
                            )
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
                                tool_timings=tool_timings,
                                executed_tools=executed_tools,
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
                if step.tool_intent:
                    tool_name = step.tool_intent.tool_name
                    prior_search = next(
                        (r for r in reversed(state.step_results) if r.tool_name == "file_search"),
                        None,
                    )
                    if prior_search is not None and tool_name in ("file_send", "file_read"):
                        if prior_search.is_ambiguous:
                            ambig_msg = (
                                prior_search.output_text
                                or (
                                    "Multiple matching files found. "
                                    "Please specify which file you would like."
                                )
                            )
                            step_res = StepResult(
                                step_id=step.step_id,
                                success=False,
                                is_ambiguous=True,
                                output_text=ambig_msg,
                                tool_name=tool_name,
                            )
                            last_output = ambig_msg
                            state.step_results.append(step_res)
                            yield ambig_msg, None
                            continue

                        if prior_search.discovered_paths:
                            discovered_path = str(prior_search.discovered_paths[0])
                            step.tool_intent.parameters["path"] = discovered_path
                            step.tool_intent.parameters.pop("query", None)
                        else:
                            q_str = step.tool_intent.parameters.get("query", "")
                            not_found_msg = (
                                prior_search.output_text
                                or f"No matching files found for '{q_str}'."
                            )
                            step_res = StepResult(
                                step_id=step.step_id,
                                success=False,
                                output_text=not_found_msg,
                                tool_name=tool_name,
                            )
                            last_output = not_found_msg
                            state.step_results.append(step_res)
                            yield not_found_msg, None
                            continue

                t0_tool = time.perf_counter()
                if timer is not None:
                    timer.start_stage(STAGE_TOOL_EXECUTION)
                step_res = await self._execute_tool_step(step)
                tool_elapsed_ms = (time.perf_counter() - t0_tool) * 1000.0
                step_res.tool_duration_ms = tool_elapsed_ms
                if timer is not None:
                    timer.end_stage(STAGE_TOOL_EXECUTION)
                    tool_name = step.tool_intent.tool_name if step.tool_intent else "tool"
                    timer.record_tool(tool_name, tool_elapsed_ms)
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
            discovered_paths: list[Path] = []
            is_ambiguous: bool = False
            output_obj: object = res.output
            if hasattr(output_obj, "discovered_paths"):
                raw_disc = getattr(output_obj, "discovered_paths", [])
                if isinstance(raw_disc, list):
                    for dp in cast(list[Any], raw_disc):
                        if isinstance(dp, (str, Path)):
                            discovered_paths.append(Path(dp))
                is_ambiguous = bool(getattr(output_obj, "is_ambiguous", False))
                output_str = str(output_obj)
            elif isinstance(output_obj, dict):
                res_dict = cast(dict[str, Any], output_obj)
                output_str = str(res_dict.get("text", ""))
                raw_paths = res_dict.get("attachment_paths", [])
                if isinstance(raw_paths, list):
                    for p in cast(list[Any], raw_paths):
                        if isinstance(p, (str, Path)):
                            attachment_paths.append(Path(p))
                raw_disc = res_dict.get("discovered_paths", [])
                if isinstance(raw_disc, list):
                    for dp in cast(list[Any], raw_disc):
                        if isinstance(dp, (str, Path)):
                            discovered_paths.append(Path(dp))
                is_ambiguous = bool(res_dict.get("is_ambiguous", False))
            elif res.output is not None:
                output_str = str(res.output)

            return StepResult(
                step_id=step.step_id,
                success=res.success,
                output_text=output_str,
                error=res.error,
                attachment_paths=attachment_paths,
                discovered_paths=discovered_paths,
                is_ambiguous=is_ambiguous,
                tool_name=tool_name,
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
