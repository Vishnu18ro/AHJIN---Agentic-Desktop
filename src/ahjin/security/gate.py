from abc import ABC, abstractmethod
from typing import Any


class PermissionGate(ABC):
    """Abstract Permission Gate interface for checking tool execution rights."""

    @abstractmethod
    async def check_permission(self, tool_name: str, parameters: dict[str, Any]) -> bool:
        """Check if action is authorized."""
