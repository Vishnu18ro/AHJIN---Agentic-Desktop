"""AHJIN 2.0 CLI entry point."""

import asyncio
import sys

import structlog

from ahjin.beru.orchestrator import BeruOrchestrator
from ahjin.beru.tool_planner import ToolIntentPlanner
from ahjin.core.config import settings
from ahjin.core.dispatcher import TaskDispatcher
from ahjin.harness.gateway import ProviderGateway
from ahjin.harness.runner import HarnessRunner
from ahjin.interfaces.telegram.bot import TelegramAdapter
from ahjin.local import LocalExecutor, LocalRoutingPolicy
from ahjin.models import ModelRouter, create_default_catalog
from ahjin.providers.nvidia import NvidiaProvider
from ahjin.providers.ollama import OllamaProvider
from ahjin.providers.openrouter import OpenRouterProvider
from ahjin.providers.registry import ProviderRegistry
from ahjin.security import AllowAllPermissionGate
from ahjin.tools import FileReadTool, FileSearchTool, ToolRegistry
from ahjin.tools.system_info import SystemInfoTool

logger = structlog.get_logger()


async def main() -> None:
    """Bootstrap AHJIN 2.0 application."""
    logger.info("AHJIN 2.0 starting up", version="2.0.0")

    # --- Tool & Security bootstrap ---
    tool_registry = ToolRegistry()
    tool_registry.register(SystemInfoTool())
    tool_registry.register(FileReadTool())
    tool_registry.register(FileSearchTool())
    logger.info("ToolRegistry initialized with baseline tools", tools=tool_registry.list_tools())

    permission_gate = AllowAllPermissionGate()

    # --- Provider & Model Catalog bootstrap ---
    registry = ProviderRegistry()
    registry.register(NvidiaProvider())

    if settings.openrouter_api_key:
        registry.register(OpenRouterProvider())
        logger.info("OpenRouterProvider registered successfully.")

    ollama_provider: OllamaProvider | None = None
    if settings.ollama_enabled:
        ollama_provider = OllamaProvider()
        registry.register(ollama_provider)
        logger.info("OllamaProvider registered successfully.")

    catalog = create_default_catalog()
    router = ModelRouter(catalog=catalog)

    # --- Dependency wiring ---
    local_executor: LocalExecutor | None = None
    if ollama_provider is not None:
        local_policy = LocalRoutingPolicy()
        local_executor = LocalExecutor(policy=local_policy, provider=ollama_provider)
        logger.info("LocalExecutor registered successfully — Phase 5 local routing enabled.")

    gateway = ProviderGateway(registry=registry, router=router)

    # Wire Hybrid Tool Intent Planner into BERU Orchestrator
    tool_planner = ToolIntentPlanner(gateway=gateway, tool_registry=tool_registry)
    orchestrator = BeruOrchestrator(tool_planner=tool_planner)

    runner = HarnessRunner(
        gateway=gateway,
        local_executor=local_executor,
        tool_registry=tool_registry,
        permission_gate=permission_gate,
    )
    dispatcher = TaskDispatcher(orchestrator=orchestrator, runner=runner)
    adapter = TelegramAdapter(dispatcher=dispatcher, router=router)

    logger.info("AHJIN 2.0 initialization complete — Multi-Model Router ready")

    # --- Start interfaces ---
    await adapter.start()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
