"""ToolRegistry — Tool lookup and registration manager."""

from ahjin.tools.base import BaseTool


class ToolRegistry:
    """Registry for managing and looking up available AHJIN tools."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """Register a tool instance.

        Raises:
            ValueError: If a tool with the same name is already registered.
        """
        name = tool.tool_name
        if name in self._tools:
            raise ValueError(f"Tool '{name}' is already registered in ToolRegistry.")
        self._tools[name] = tool

    def get_tool(self, tool_name: str) -> BaseTool:
        """Retrieve a registered tool by name.

        Raises:
            KeyError: If tool_name is not registered.
        """
        if tool_name not in self._tools:
            raise KeyError(f"Tool '{tool_name}' is not registered in ToolRegistry.")
        return self._tools[tool_name]

    def list_tools(self) -> list[str]:
        """Return list of registered tool names."""
        return list(self._tools.keys())

    def has_tool(self, tool_name: str) -> bool:
        """Check if a tool name is registered."""
        return tool_name in self._tools
