"""AllowAllPermissionGate — Baseline permission gate implementation."""

from typing import Any

from ahjin.security.gate import PermissionGate


class AllowAllPermissionGate(PermissionGate):
    """Baseline permission gate that authorizes registered safe tool executions."""

    async def check_permission(self, tool_name: str, parameters: dict[str, Any]) -> bool:
        """Check if tool execution is authorized."""
        return True
