"""FileReadTool — Read text files safely within workspace bounds."""

import time
from pathlib import Path

from ahjin.core.errors import AhjinError, ErrorCategory
from ahjin.security.path_policy import SafePathPolicy
from ahjin.tools.base import BaseTool, ToolInvocationRequest, ToolInvocationResult

_MAX_FILE_SIZE_BYTES = 200 * 1024  # 200 KB


class FileReadTool(BaseTool):
    """Tool for reading safe text files within the configured workspace bounds.

    Strictly rejects directory paths, path traversal, binary files, sensitive credential
    files, and files outside the workspace root.
    """

    def __init__(self, path_policy: SafePathPolicy | None = None) -> None:
        self.path_policy = path_policy or SafePathPolicy()

    @property
    def tool_name(self) -> str:
        return "file_read"

    async def execute(self, request: ToolInvocationRequest) -> ToolInvocationResult:
        t0 = time.monotonic()
        path_str = str(request.parameters.get("path", "")).strip()

        if not path_str:
            latency_ms = (time.monotonic() - t0) * 1000.0
            return ToolInvocationResult(
                invocation_id=request.invocation_id,
                success=False,
                output=None,
                error=AhjinError(
                    code="EMPTY_PATH",
                    message="No file path provided in parameters.",
                    category=ErrorCategory.VALIDATION,
                ),
                latency_ms=latency_ms,
            )

        is_safe, resolved_path, error_reason = self.path_policy.validate_safe_path(path_str)
        if not is_safe or resolved_path is None:
            latency_ms = (time.monotonic() - t0) * 1000.0
            return ToolInvocationResult(
                invocation_id=request.invocation_id,
                success=False,
                output=None,
                error=AhjinError(
                    code="PATH_ACCESS_DENIED",
                    message=error_reason or f"Access to path '{path_str}' denied.",
                    category=ErrorCategory.VALIDATION,
                ),
                latency_ms=latency_ms,
            )

        if not resolved_path.exists():
            latency_ms = (time.monotonic() - t0) * 1000.0
            return ToolInvocationResult(
                invocation_id=request.invocation_id,
                success=False,
                output=None,
                error=AhjinError(
                    code="FILE_NOT_FOUND",
                    message=f"File not found: '{path_str}'.",
                    category=ErrorCategory.VALIDATION,
                ),
                latency_ms=latency_ms,
            )

        if resolved_path.is_dir():
            latency_ms = (time.monotonic() - t0) * 1000.0
            return ToolInvocationResult(
                invocation_id=request.invocation_id,
                success=False,
                output=None,
                error=AhjinError(
                    code="PATH_IS_DIRECTORY",
                    message=f"Path '{path_str}' is a directory, not a text file.",
                    category=ErrorCategory.VALIDATION,
                ),
                latency_ms=latency_ms,
            )

        # Detect binary file content
        try:
            with resolved_path.open("rb") as f:
                sample = f.read(8192)
                if b"\0" in sample:
                    latency_ms = (time.monotonic() - t0) * 1000.0
                    return ToolInvocationResult(
                        invocation_id=request.invocation_id,
                        success=False,
                        output=None,
                        error=AhjinError(
                            code="BINARY_FILE",
                            message=f"Cannot read binary file '{path_str}'.",
                            category=ErrorCategory.VALIDATION,
                        ),
                        latency_ms=latency_ms,
                    )
        except Exception as exc:
            latency_ms = (time.monotonic() - t0) * 1000.0
            return ToolInvocationResult(
                invocation_id=request.invocation_id,
                success=False,
                output=None,
                error=AhjinError(
                    code="FILE_CHECK_ERROR",
                    message=f"Error checking file '{path_str}': {exc}",
                    category=ErrorCategory.TOOL,
                ),
                latency_ms=latency_ms,
            )

        # Enforce maximum file size check
        file_size = resolved_path.stat().st_size
        if file_size > _MAX_FILE_SIZE_BYTES:
            latency_ms = (time.monotonic() - t0) * 1000.0
            return ToolInvocationResult(
                invocation_id=request.invocation_id,
                success=False,
                output=None,
                error=AhjinError(
                    code="FILE_TOO_LARGE",
                    message=(
                        f"File '{path_str}' exceeds maximum allowed size of "
                        f"{_MAX_FILE_SIZE_BYTES // 1024} KB."
                    ),
                    category=ErrorCategory.VALIDATION,
                ),
                latency_ms=latency_ms,
            )

        try:
            content = resolved_path.read_text(encoding="utf-8", errors="replace")
            relative_display_path: Path = resolved_path
            for auth_root in self.path_policy.authorized_roots:
                try:
                    relative_display_path = resolved_path.relative_to(auth_root)
                    break
                except ValueError:
                    pass
            output = f"--- FILE: {relative_display_path} ---\n{content}"

            latency_ms = (time.monotonic() - t0) * 1000.0
            return ToolInvocationResult(
                invocation_id=request.invocation_id,
                success=True,
                output=output,
                error=None,
                latency_ms=latency_ms,
            )
        except Exception as exc:
            latency_ms = (time.monotonic() - t0) * 1000.0
            return ToolInvocationResult(
                invocation_id=request.invocation_id,
                success=False,
                output=None,
                error=AhjinError(
                    code="FILE_READ_ERROR",
                    message=f"Failed to read file '{path_str}': {exc}",
                    category=ErrorCategory.TOOL,
                ),
                latency_ms=latency_ms,
            )
