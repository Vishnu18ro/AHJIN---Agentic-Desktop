"""Telegram Bot Adapter — V2 with runtime observability footer and /health /models commands."""

import asyncio
import time
from typing import Any

import structlog
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from ahjin.core.config import settings
from ahjin.core.dispatcher import TaskDispatcher
from ahjin.core.types import RerouteAttempt, RuntimeInfo
from ahjin.interfaces.base import BaseInterfaceAdapter
from ahjin.interfaces.telegram.mapper import TelegramMapper
from ahjin.models.health import ModelHealthStatus
from ahjin.models.router import ModelRouter
from ahjin.telemetry import (
    STAGE_BERU_ANALYSIS,
    STAGE_CONTEXT_ASSEMBLY,
    STAGE_FINAL_RESPONSE_ASSEMBLY,
    STAGE_MODEL_GENERATION,
    STAGE_MODEL_ROUTING,
    STAGE_PROVIDER_SETUP,
    STAGE_STREAM_PROCESSING,
    STAGE_TELEGRAM_DELIVERY,
    STAGE_TELEGRAM_PLACEHOLDER,
    STAGE_TELEGRAM_RECEIVE,
    STAGE_TIME_TO_FIRST_TOKEN,
    STAGE_TOOL_EXECUTION,
    STAGE_TOOL_PLANNER,
)

logger = structlog.get_logger()

# Telegram's hard per-message character limit.
# Messages exceeding this are split into sequential chunks.
TELEGRAM_MAX_MESSAGE_LENGTH = 4096

# Maximum length for the main body before appending footer.
# Footer is always kept on the final chunk.
_FOOTER_RESERVED = 350


def _chunk_message(text: str, chunk_size: int = TELEGRAM_MAX_MESSAGE_LENGTH) -> list[str]:
    """Split text into chunks of at most chunk_size characters.

    Splits on newline or space boundaries where possible to avoid
    cutting mid-word. Falls back to hard split if no boundary exists.
    """
    if len(text) <= chunk_size:
        return [text]

    chunks: list[str] = []
    while text:
        if len(text) <= chunk_size:
            chunks.append(text)
            break
        # Prefer splitting on a newline or space within the budget
        split_at = text.rfind("\n", 0, chunk_size)
        if split_at == -1:
            split_at = text.rfind(" ", 0, chunk_size)
        if split_at == -1:
            # No safe boundary found — hard split at chunk_size
            split_at = chunk_size
        chunks.append(text[:split_at].rstrip())
        text = text[split_at:].lstrip()
    return [c for c in chunks if c]


def _provider_display_name(provider_id: str) -> str:
    """Return a clean, human-readable provider name for the footer."""
    if not provider_id:
        return "Unknown"
    prov_map = {
        "openrouter": "OpenRouter",
        "nvidia": "NVIDIA",
        "ollama": "Ollama",
        "ahjin": "AHJIN",
    }
    return prov_map.get(provider_id.lower(), provider_id.title())


def _model_short_name(model_id: str) -> str:
    """Return a compact, human-readable model name for the footer."""
    label_map = {
        "minimax/minimax-m3": "MiniMax M3",
        "minimax/minimax-m3:free": "MiniMax M3",
        "nvidia/nemotron-3.5-lightning:free": "Nemotron 3.5 Lightning",
        "nvidia/nemotron-3-ultra-550b-a55b:free": "Nemotron Ultra",
        "nvidia/nemotron-3.5-lightning-30b-a3b": "Nemotron Lightning 30B",
        "nvidia/nemotron-3-ultra-550b-a55b": "Nemotron Ultra 550B",
        "deepseek-ai/deepseek-v4-pro-0813": "DeepSeek V4 Pro",
        "deepseek-ai/deepseek-v4-flash-0731": "DeepSeek V4 Flash",
        "moonshotai/kimi-k3": "Kimi K3",
        "gemma3:4b": "Gemma 3 4B",
        "ollama/gemma3:4b": "Gemma 3 4B",
        "qwen3:8b": "Qwen 3 8B",
        "ollama/qwen3:8b": "Qwen 3 8B",
    }
    return label_map.get(model_id, model_id.split("/")[-1])


def _format_failure_reason(reason: str | None) -> str:
    """Format failure reason compactly for chronological attempt chain."""
    if not reason:
        return ""
    r = reason.strip()
    if r.lower().startswith("http_"):
        code_part = r[5:].strip()
        if code_part.isdigit():
            return code_part
    if r.upper().startswith("HTTP "):
        code_part = r[5:].strip()
        if code_part.isdigit():
            return code_part
    if r.isdigit():
        return r
    if r in ("inactivity_timeout", "timeout", "startup_timeout", "qwen_timeout"):
        return "timeout"
    label_map = {
        "empty_response": "empty response",
        "invalid_json": "invalid JSON",
        "unregistered_tool": "unregistered tool",
        "invalid_params": "invalid params",
    }
    return label_map.get(r, r)


def _format_reroute_chain(
    component_name: str,
    attempts: list[RerouteAttempt],
    fallback_model: str | None = None,
    fallback_target: str | None = None,
    fallback_reason: str | None = None,
) -> str | None:
    """Format a chronological rerouting attempt chain for a component (Planner or Harness).

    Displays:
    {component_name}: {model_1} ❌ {reason} → {model_2} ❌ {reason} → {final_model} ✅
    """
    valid_attempts = [
        att for att in attempts
        if att.model_id and att.model_id != "unknown"
    ]
    if valid_attempts:
        items: list[str] = []
        for att in valid_attempts:
            name = _model_short_name(att.model_id)
            if att.success:
                items.append(f"{name} ✅")
            else:
                reason_label = _format_failure_reason(att.reason)
                items.append(f"{name} ❌ {reason_label}" if reason_label else f"{name} ❌")
        if items:
            return f"{component_name}: {' → '.join(items)}"

    if fallback_model:
        from_name = _model_short_name(fallback_model)
        reason_label = _format_failure_reason(fallback_reason)
        from_item = f"{from_name} ❌ {reason_label}" if reason_label else f"{from_name} ❌"
        if fallback_target:
            to_name = _model_short_name(fallback_target)
            return f"{component_name}: {from_item} → {to_name} ✅"
        return f"{component_name}: {from_item}"

    return None


TOOL_DISPLAY_MAP: dict[str, str] = {
    "system_info": "System Info",
    "file_search": "File Search",
    "file_read": "File Read",
    "file_send": "File Send",
    "web_search": "Web Search",
    "browser": "Browser",
}


def _tool_display_name(tool_name: str) -> str:
    """Map tool snake_case identifier to a clean human-readable label."""
    return TOOL_DISPLAY_MAP.get(tool_name, tool_name.replace("_", " ").title())


def _health_icon(status: str) -> str:
    """Map health status string to a compact emoji indicator."""
    return {
        ModelHealthStatus.HEALTHY.value: "🟢",
        ModelHealthStatus.DEGRADED.value: "🟡",
        ModelHealthStatus.UNHEALTHY.value: "🔴",
    }.get(status, "⚪")


def _build_runtime_footer(info: RuntimeInfo) -> str:
    """Build simplified runtime observability footer from RuntimeInfo.

    Displays high-level execution timings in milliseconds:
    - AHJIN: AHJIN orchestration only (BERU, context assembly, model routing, provider setup).
    - Provider: Observable external provider/API communication turnaround.
    - Model: Observable model response / generation duration.
    - Tool: Actual tool execution time (individual tool rows when tools ran).
    - Telegram: Telegram API delivery time (edit_text calls).
    - Other: Residual = Total - AHJIN - Provider - Tools - Model - Telegram.
              Only shown when measurable (> 50ms).
    - Total: Complete end-to-end elapsed time.

    Detailed internal stages remain recorded in info.timing for diagnostics.
    Never exposes API keys, tokens, stack traces, or raw HTTP payloads.
    """
    route_label = "↪ Rerouted" if info.was_rerouted else "Direct"
    health_icon = _health_icon(info.health_status)

    lines = [
        "━" * 18,
        "⚡ AHJIN Runtime",
        "",
        f"Model: {_model_short_name(info.selected_model)}",
        f"Provider: {_provider_display_name(info.provider_id)}",
        f"Route: {info.tier}",
    ]

    lines.append("")
    lines.append("⏱ Latency")

    timing = info.timing
    has_provider_timing = False
    if timing:
        # AHJIN orchestration overhead only (strictly disjoint from Tool & Model;
        # tool_planner LLM duration is subtracted to avoid double counting, and
        # telegram_receive is excluded from user-facing AHJIN per specification).
        ahjin_ms = (
            max(
                timing.get(STAGE_BERU_ANALYSIS, 0.0)
                - timing.get(STAGE_TOOL_PLANNER, 0.0),
                0.0,
            )
            + timing.get(STAGE_CONTEXT_ASSEMBLY, 0.0)
            + timing.get(STAGE_MODEL_ROUTING, 0.0)
            + timing.get(STAGE_PROVIDER_SETUP, 0.0)
        )
        if ahjin_ms == 0.0 and info.ahjin_internal_ms > 0:
            ahjin_ms = info.ahjin_internal_ms

        provider_ms = timing.get(STAGE_TIME_TO_FIRST_TOKEN, 0.0) or info.provider_api_ms
        model_ms = timing.get(STAGE_MODEL_GENERATION, 0.0) or info.model_api_ms

        if provider_ms > 0 or info.provider_api_ms > 0 or STAGE_TIME_TO_FIRST_TOKEN in timing:
            has_provider_timing = True

        if provider_ms == 0.0 and model_ms == 0.0:
            model_ms = (
                timing.get(STAGE_STREAM_PROCESSING, 0.0)
                or info.model_api_ms
            )

        telegram_ms = timing.get(STAGE_TELEGRAM_DELIVERY, 0.0)
        tool_llm_ms = timing.get(STAGE_TOOL_PLANNER, 0.0)
    else:
        ahjin_ms = info.ahjin_internal_ms
        provider_ms = info.provider_api_ms
        model_ms = info.model_api_ms
        if provider_ms > 0 or info.provider_api_ms > 0:
            has_provider_timing = True
        telegram_ms = 0.0
        tool_llm_ms = 0.0

    # Sum explicit per-tool durations for "other" budget calculation
    tool_timings = info.tool_timings
    total_tool_ms = sum(t_ms for _, t_ms in tool_timings) if tool_timings else (
        timing.get(STAGE_TOOL_EXECUTION, 0.0) if timing else 0.0
    )

    lines.append(f"├─ AHJIN: {int(round(ahjin_ms))}ms")
    if tool_llm_ms > 0:
        lines.append(f"├─ Tool LLM: {int(round(tool_llm_ms))}ms")
    if has_provider_timing and provider_ms > 0:
        lines.append(f"├─ Provider: {int(round(provider_ms))}ms")

    # Tool execution latency (only if tools actually executed)
    if tool_timings:
        if len(tool_timings) == 1:
            _, t_ms = tool_timings[0]
            lines.append(f"├─ Tool: {int(round(t_ms))}ms")
        else:
            for t_name, t_ms in tool_timings:
                disp_name = _tool_display_name(t_name)
                lines.append(f"├─ {disp_name}: {int(round(t_ms))}ms")
    elif timing and timing.get(STAGE_TOOL_EXECUTION, 0.0) > 0:
        t_ms = timing.get(STAGE_TOOL_EXECUTION, 0.0)
        lines.append(f"├─ Tool: {int(round(t_ms))}ms")

    lines.append(f"├─ Model: {int(round(model_ms))}ms")

    # Telegram delivery bucket (only when measured)
    if telegram_ms > 0:
        lines.append(f"├─ Telegram: {int(round(telegram_ms))}ms")

    # "Other" residual bucket:
    # Total - AHJIN - Provider - all tool execution - Model - Telegram
    # Represents unmeasured gaps (provider TLS handshake, context assembly if untracked, etc.)
    # Only shown when it exceeds 50ms to avoid noise from sub-millisecond Python overhead.
    if info.total_ms > 0:
        effective_provider_ms = provider_ms if has_provider_timing else 0.0
        other_ms = (
            info.total_ms
            - ahjin_ms
            - tool_llm_ms
            - effective_provider_ms
            - total_tool_ms
            - model_ms
            - telegram_ms
        )
        if other_ms > 50.0:
            lines.append(f"├─ Other: {int(round(other_ms))}ms")

    lines.append(f"└─ Total: {int(round(info.total_ms))}ms")

    # Path section immediately follows Latency section
    lines.append("")
    lines.append(f"Path: {route_label}")

    if info.was_rerouted:
        reason = info.failure_reason or info.harness_failure_reason or info.planner_failure_reason

        # Case 2 or Case 4: Planner rerouted
        if info.planner_was_rerouted:
            planner_target = (
                info.planner_selected_model
                or (info.planner_route_history[-1] if info.planner_route_history else None)
                or info.selected_model
            )
            p_chain = _format_reroute_chain(
                "Planner",
                attempts=info.planner_attempts,
                fallback_model=info.planner_failed_model,
                fallback_target=planner_target,
                fallback_reason=info.planner_failure_reason,
            )
            if p_chain:
                lines.append(p_chain)

        # Case 3 or Case 4: Harness rerouted
        if info.harness_was_rerouted:
            h_chain = _format_reroute_chain(
                "Harness",
                attempts=info.harness_attempts,
                fallback_model=info.harness_failed_model,
                fallback_target=info.selected_model,
                fallback_reason=info.harness_failure_reason,
            )
            if h_chain:
                lines.append(h_chain)
        elif info.planner_was_rerouted and not info.harness_was_rerouted:
            lines.append(
                f"Harness: {_model_short_name(info.selected_model)}"
            )

        # Note: "From:" line removed per UX refinement specification

        if reason:
            lines.append(f"Reason: {reason}")

    # Tool metadata line (only when tool(s) executed)
    tool_meta_names = []
    if tool_timings:
        tool_meta_names = [_tool_display_name(t[0]) for t in tool_timings]
    elif info.executed_tools:
        tool_meta_names = [_tool_display_name(t) for t in info.executed_tools]

    if tool_meta_names:
        lines.append("")
        if len(tool_meta_names) == 1:
            lines.append(f"Tool: {tool_meta_names[0]}")
        else:
            lines.append(f"Tools: {', '.join(tool_meta_names)}")

    lines.append("")
    lines.append(f"Health: {health_icon} {info.health_status.title()}")
    lines.append("━" * 18)
    return "\n".join(lines)


def _build_health_snapshot(router: ModelRouter) -> str:
    """Build a compact health snapshot for /health command output."""
    models = router.catalog.list_models()
    if not models:
        return "No models registered."

    lines = ["<b>AHJIN Model Health</b>", ""]
    for descriptor in models:
        state = router.health_tracker.get_state(descriptor.model_id)
        status = state.snapshot_status.value
        icon = _health_icon(status)
        ema = state.snapshot_ema_latency_ms
        failures = state.snapshot_consecutive_failures

        name = _model_short_name(descriptor.model_id)
        tier_label = descriptor.tier.value

        if ema > 0:
            latency_label = f"EMA {ema / 1000:.1f}s"
        else:
            latency_label = "no latency data"

        status_line = f"{icon} {name} [{tier_label}]"
        detail_line = f"   {status.title()}"
        if failures > 0:
            detail_line += f" · {failures} failure{'s' if failures != 1 else ''}"
        detail_line += f" · {latency_label}"

        lines.append(status_line)
        lines.append(detail_line)
        lines.append("")

    return "\n".join(lines).strip()


class TelegramAdapter(BaseInterfaceAdapter):
    """Telegram Interface Adapter — V2 with runtime observability."""

    def __init__(
        self,
        token: str | None = None,
        dispatcher: TaskDispatcher | None = None,
        router: ModelRouter | None = None,
    ) -> None:
        self.token = token or settings.telegram_bot_token
        self.dispatcher = dispatcher or TaskDispatcher()
        self.router = router  # Optional; enables /health and /models commands
        self.app: Application[Any, Any, Any, Any, Any, Any] | None = None
        self._stop_event: asyncio.Event = asyncio.Event()

    @property
    def interface_id(self) -> str:
        return "telegram"

    async def _start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command."""
        if update.message:
            await update.message.reply_text(
                "Welcome to AHJIN 2.0 — Personal Agentic AI Operating Layer."
            )

    async def _health_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /health command — compact model health snapshot."""
        if not update.message:
            return
        if self.router is None:
            await update.message.reply_text("Health tracking not available.")
            return
        snapshot = _build_health_snapshot(self.router)
        await update.message.reply_text(snapshot, parse_mode="HTML")

    async def _models_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /models command — alias for /health."""
        await self._health_command(update, context)

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming text messages with progressive response streaming."""
        if not update.message or not update.message.text:
            return

        t0_recv = time.monotonic()
        chat_id = update.message.chat_id
        text = update.message.text

        logger.info("[PROFILE] Telegram update received", chat_id=chat_id, text_length=len(text))

        # 1. Map Telegram input to TaskRequest
        t0_map = time.monotonic()
        request = TelegramMapper.to_task_request(chat_id, text)
        t_map_ms = (time.monotonic() - t0_map) * 1000.0

        # 2. Send initial placeholder message
        t0_placeholder = time.monotonic()
        placeholder_msg = await update.message.reply_text("Thinking...")
        t_placeholder_ms = (time.monotonic() - t0_placeholder) * 1000.0

        accumulated_text = ""
        first_token_received = False
        last_edit_time = 0.0
        final_task_result = None
        t0_dispatch = time.monotonic()

        try:
            async for chunk, task_result in self.dispatcher.dispatch_stream(request):
                if chunk:
                    if not first_token_received:
                        first_token_received = True
                    accumulated_text += chunk
                    now = time.monotonic()
                    if now - last_edit_time >= 1.0:
                        last_edit_time = now
                        display_text = (
                            accumulated_text[:4000]
                            if len(accumulated_text) > 4000
                            else accumulated_text
                        )
                        try:
                            await placeholder_msg.edit_text(display_text)
                        except Exception as edit_err:
                            logger.debug("Intermediate edit ignored", error=str(edit_err))

                if task_result is not None:
                    final_task_result = task_result

        except Exception as exc:
            logger.error(
                "Streaming dispatch failed",
                chat_id=chat_id,
                error=str(exc),
                first_token_received=first_token_received,
            )
            if not first_token_received:
                # Fall back to non-streaming dispatch
                try:
                    result = await self.dispatcher.dispatch(request)
                    final_task_result = result
                    accumulated_text = TelegramMapper.to_telegram_response(result)
                except Exception as fallback_exc:
                    logger.error(
                        "Fallback non-streaming dispatch failed",
                        chat_id=chat_id,
                        error=str(fallback_exc),
                    )
                    err_msg = "An internal error occurred. Please try again."
                    try:
                        await placeholder_msg.edit_text(err_msg)
                    except Exception:
                        await update.message.reply_text(err_msg)
                    return
            else:
                # Partial response received, append stream interruption note
                accumulated_text += "\n\n[Stream interrupted due to error]"

        t_dispatch_ms = (time.monotonic() - t0_dispatch) * 1000.0

        t0_assembly = time.monotonic()
        # Build response & footer
        response_text = ""
        if final_task_result is not None:
            response_text = TelegramMapper.to_telegram_response(final_task_result)
        if not response_text:
            response_text = accumulated_text or "No response received."

        # --- Single-message delivery: build footer BEFORE chunking ---
        # We estimate STAGE_TELEGRAM_DELIVERY using the time elapsed since
        # dispatch completed. This is slightly conservative but avoids needing
        # a second message to carry the footer.
        footer = ""
        runtime_info_snapshot = (
            final_task_result.runtime_info
            if final_task_result is not None and final_task_result.runtime_info is not None
            else None
        )

        if runtime_info_snapshot is not None:
            # Estimate total wall-clock at footer-build time (before final edit).
            # STAGE_TELEGRAM_DELIVERY is estimated rather than exact to preserve
            # the single-message UX: actual edit_text latency is typically 100-400ms
            # and is a negligible fraction of the overall wall-clock total.
            est_total_ms = (time.monotonic() - t0_recv) * 1000.0
            # Rough estimate for Telegram API delivery: time since dispatch finished
            est_telegram_ms = (time.monotonic() - t0_dispatch) * 1000.0 - t_dispatch_ms
            t_assembly_ms = (time.monotonic() - t0_assembly) * 1000.0
            updated_timing = dict(runtime_info_snapshot.timing)
            updated_timing[STAGE_TELEGRAM_RECEIVE] = round(t_map_ms, 1)
            updated_timing[STAGE_TELEGRAM_DELIVERY] = round(max(est_telegram_ms, 0.0), 1)
            updated_timing[STAGE_TELEGRAM_PLACEHOLDER] = round(t_placeholder_ms, 1)
            updated_timing[STAGE_FINAL_RESPONSE_ASSEMBLY] = round(t_assembly_ms, 1)
            patched_info = runtime_info_snapshot.model_copy(
                update={"total_ms": round(est_total_ms, 1), "timing": updated_timing}
            )
            footer = "\n\n" + _build_runtime_footer(patched_info)

            logger.info(
                "[PROFILE] Telegram internal latency telemetry",
                chat_id=chat_id,
                placeholder_ms=round(t_placeholder_ms, 1),
                map_ms=round(t_map_ms, 1),
                dispatch_ms=round(t_dispatch_ms, 1),
                assembly_ms=round(t_assembly_ms, 1),
                total_ms=round(est_total_ms, 1),
            )

        # Unify answer + footer in ONE message (historical single-message UX restored).
        full_text = response_text + footer
        chunks = _chunk_message(full_text)

        # Final edit & chunk delivery
        t0_reply = time.monotonic()
        try:
            await placeholder_msg.edit_text(chunks[0])
        except Exception as edit_err:
            logger.warning("Final edit_text failed, sending reply instead", error=str(edit_err))
            await update.message.reply_text(chunks[0])

        if len(chunks) > 1:
            for i, chunk in enumerate(chunks[1:]):
                await update.message.reply_text(chunk)
                logger.info(
                    "[PROFILE] Telegram chunk sent",
                    chat_id=chat_id,
                    chunk_index=i + 2,
                    total_chunks=len(chunks),
                    chunk_length=len(chunk),
                )
        t_reply_ms = (time.monotonic() - t0_reply) * 1000.0

        # Document attachment delivery if present in TaskResult.
        # Attachments are always separate messages (binary files cannot be
        # embedded in text messages on Telegram by design).
        if final_task_result is not None and final_task_result.file_attachments:
            for att_path in final_task_result.file_attachments:
                if att_path.exists() and att_path.is_file():
                    try:
                        with att_path.open("rb") as doc_file:
                            await update.message.reply_document(
                                document=doc_file, filename=att_path.name
                            )
                        logger.info(
                            "[PROFILE] Telegram document attachment sent",
                            chat_id=chat_id,
                            file_name=att_path.name,
                        )
                    except Exception as att_err:
                        logger.error(
                            "Failed to send Telegram document attachment",
                            chat_id=chat_id,
                            file_name=att_path.name,
                            error=str(att_err),
                        )

        t_total_ms = (time.monotonic() - t0_recv) * 1000.0

        model_name = (
            final_task_result.runtime_info.selected_model
            if final_task_result and final_task_result.runtime_info
            else "unknown"
        )
        was_rerouted = (
            final_task_result.runtime_info.was_rerouted
            if final_task_result and final_task_result.runtime_info
            else False
        )

        logger.info(
            "[PROFILE] Telegram message pipeline finished",
            chat_id=chat_id,
            input_mapping_ms=round(t_map_ms, 3),
            core_dispatch_ms=round(t_dispatch_ms, 3),
            telegram_send_ms=round(t_reply_ms, 3),
            total_end_to_end_ms=round(t_total_ms, 3),
            response_chunks=len(chunks),
            response_total_chars=len(response_text),
            model_used=model_name,
            was_rerouted=was_rerouted,
        )

    async def start(self) -> None:
        """Start Telegram bot application and block until stopped."""
        if not self.token:
            logger.warning("TELEGRAM_BOT_TOKEN not configured. Skipping Telegram adapter start.")
            return

        self.app = Application.builder().token(self.token).build()
        self.app.add_handler(CommandHandler("start", self._start_command))
        self.app.add_handler(CommandHandler("health", self._health_command))
        self.app.add_handler(CommandHandler("models", self._models_command))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message))

        logger.info("Starting Telegram bot polling...")
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()  # type: ignore[union-attr]

        logger.info("AHJIN Telegram adapter is live — waiting for updates.")
        # Block here indefinitely. Without this the coroutine returns immediately,
        # tearing down the event loop before any Telegram updates can arrive.
        await self._stop_event.wait()

    async def stop(self) -> None:
        """Stop Telegram bot application."""
        self._stop_event.set()  # unblock start()
        if self.app:
            logger.info("Stopping Telegram bot...")
            if self.app.updater:
                await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
