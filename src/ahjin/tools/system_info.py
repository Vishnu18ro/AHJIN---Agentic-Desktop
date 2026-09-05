"""SystemInfoTool — Read-only baseline tool returning safe environment info with field selection."""

import os
import platform
import sys
import time
from typing import Any, ClassVar, cast

from ahjin.tools.base import BaseTool, ToolInvocationRequest, ToolInvocationResult

# Strict security boundary: only whitelisted safe fields can be retrieved.
SAFE_FIELDS_WHITELIST: frozenset[str] = frozenset({
    "os",
    "python",
    "machine",
    "platform",
    "cwd",
    "cpu",
    "memory",
    "all_safe",
})


class SystemInfoTool(BaseTool):
    """Deterministic read-only tool that provides safe system runtime info.

    Never exposes environment variables, API keys, credentials, or .env files.
    Enforces a strict whitelist on requested fields.
    """

    SUPPORTED_FIELDS: ClassVar[frozenset[str]] = SAFE_FIELDS_WHITELIST

    @property
    def tool_name(self) -> str:
        return "system_info"

    async def execute(self, request: ToolInvocationRequest) -> ToolInvocationResult:
        t0 = time.monotonic()
        try:
            raw_fields: Any = request.parameters.get("fields", ["all_safe"])
            field_list: list[Any] = (
                cast(list[Any], raw_fields) if isinstance(raw_fields, list) else ["all_safe"]
            )

            # Filter against security whitelist
            valid_fields: list[str] = [
                str(x) for x in field_list if isinstance(x, str) and x in SAFE_FIELDS_WHITELIST
            ]

            if "all_safe" in valid_fields or not valid_fields:
                active_fields: list[str] = [
                    "os",
                    "python",
                    "machine",
                    "platform",
                    "cwd",
                    "cpu",
                    "memory",
                ]
            else:
                active_fields = valid_fields

            lines: list[str] = []
            for field in active_fields:
                if field == "os":
                    lines.append(f"OS: {platform.system()} ({platform.release()})")
                elif field == "python":
                    lines.append(f"Python: {sys.version.split()[0]}")
                elif field == "machine":
                    lines.append(f"Machine: {platform.machine()}")
                elif field == "platform":
                    lines.append(f"Platform: {sys.platform}")
                elif field == "cwd":
                    lines.append(f"CWD: {os.getcwd()}")
                elif field == "cpu":
                    count = os.cpu_count() or "Unknown"
                    lines.append(f"CPU Cores: {count}")
                elif field == "memory":
                    sysconf_func: Any = getattr(os, "sysconf", None)
                    if callable(sysconf_func):
                        try:
                            page_size = int(str(sysconf_func("SC_PAGE_SIZE")))
                            phys_pages = int(str(sysconf_func("SC_PHYS_PAGES")))
                            mem_bytes = page_size * phys_pages
                        except Exception:
                            mem_bytes = 0
                    else:
                        mem_bytes = 0

                    mem_gb = round(mem_bytes / (1024**3), 1) if mem_bytes > 0 else "N/A"
                    lines.append(f"Memory: {mem_gb} GB architecture memory space")

            if lines:
                output_text = "\n".join(lines)
            else:
                output_text = "No valid safe system info fields requested."

            latency_ms = (time.monotonic() - t0) * 1000.0
            return ToolInvocationResult(
                invocation_id=request.invocation_id,
                success=True,
                output=output_text,
                latency_ms=latency_ms,
            )
        except Exception as exc:
            latency_ms = (time.monotonic() - t0) * 1000.0
            return ToolInvocationResult(
                invocation_id=request.invocation_id,
                success=False,
                output=f"Failed to gather system info: {exc}",
                latency_ms=latency_ms,
            )
