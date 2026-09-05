"""FileSendTool — Prepare safe filesystem files for interface chat attachment delivery."""

import time
from pathlib import Path
from typing import Any, cast

from ahjin.core.errors import AhjinError, ErrorCategory
from ahjin.security.path_policy import SafePathPolicy
from ahjin.tools.base import BaseTool, ToolInvocationRequest, ToolInvocationResult

_MAX_ATTACHMENT_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB limit
_MAX_BATCH_ATTACHMENTS = 5


class FileSendTool(BaseTool):
    """Tool for validating and preparing safe filesystem files for chat attachment delivery.

    Validates path authorization via SafePathPolicy, checks file existence, size limits,
    sensitive file blacklists, and system path blockage.
    Produces safe model-facing output (using relative display paths ONLY) and passes absolute
    paths in internal result payload for trusted attachment handlers.
    """

    def __init__(self, path_policy: SafePathPolicy | None = None) -> None:
        self.path_policy = path_policy or SafePathPolicy()

    @property
    def tool_name(self) -> str:
        return "file_send"

    async def execute(self, request: ToolInvocationRequest) -> ToolInvocationResult:
        t0 = time.monotonic()
        target_path_str = str(request.parameters.get("path", "")).strip()

        if not target_path_str:
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

        candidate_files: list[Path] = []

        # 1. Check if subpath validates directly via SafePathPolicy
        is_safe, resolved_path, error_reason = self.path_policy.validate_safe_path(target_path_str)
        if is_safe and resolved_path is not None and resolved_path.exists():
            if resolved_path.is_file():
                candidate_files.append(resolved_path)
            elif resolved_path.is_dir():
                ext_filter = request.parameters.get("file_extensions")
                target_exts: set[str] | None = None
                if isinstance(ext_filter, list):
                    target_exts = set()
                    raw_ext_list = cast(list[Any], ext_filter)
                    for item in raw_ext_list:
                        item_str = f"{item}".lower()
                        target_exts.add(item_str if item_str.startswith(".") else f".{item_str}")

                for child in resolved_path.rglob("*"):
                    if (
                        child.is_file()
                        and not self.path_policy.is_sensitive_file(child)
                        and not self.path_policy.is_system_blocked(child)
                    ):
                        if target_exts and child.suffix.lower() not in target_exts:
                            continue
                        candidate_files.append(child)
                        if len(candidate_files) >= _MAX_BATCH_ATTACHMENTS:
                            break
        else:
            # 2. Try search_roots for keyword / shortcut / relative subpath discovery
            is_roots_safe, search_roots, _ = self.path_policy.get_search_roots(
                target_path_str
            )
            if is_roots_safe and search_roots:
                query_term = str(request.parameters.get("query", target_path_str)).strip().lower()
                for s_root in search_roots:
                    if s_root.is_file() and not self.path_policy.is_sensitive_file(s_root):
                        candidate_files.append(s_root)
                    elif s_root.is_dir():
                        for child in s_root.rglob("*"):
                            if (
                                child.is_file()
                                and not self.path_policy.is_sensitive_file(child)
                                and not self.path_policy.is_system_blocked(child)
                            ):
                                skip_terms = ("pc", "downloads", "desktop", "documents")
                                if query_term and query_term not in skip_terms:
                                    if (
                                        query_term not in child.name.lower()
                                        and query_term not in child.as_posix().lower()
                                    ):
                                        continue
                                candidate_files.append(child)
                                if len(candidate_files) >= _MAX_BATCH_ATTACHMENTS:
                                    break
                        if candidate_files:
                            break

        if not candidate_files:
            latency_ms = (time.monotonic() - t0) * 1000.0
            err_msg = error_reason or f"File or path not found: '{target_path_str}'."
            err_code = (
                "FILE_NOT_FOUND"
                if "not found" in err_msg.lower() or "does not exist" in err_msg.lower()
                else "PATH_DENIED"
            )
            return ToolInvocationResult(
                invocation_id=request.invocation_id,
                success=False,
                output=None,
                error=AhjinError(
                    code=err_code,
                    message=err_msg,
                    category=ErrorCategory.VALIDATION,
                ),
                latency_ms=latency_ms,
            )

        # 3. Validate candidate files against safety, blacklist & size limits
        valid_attachments: list[Path] = []
        output_blocks: list[str] = []

        for file_path in candidate_files:
            if not file_path.exists() or not file_path.is_file():
                continue
            if self.path_policy.is_sensitive_file(file_path):
                latency_ms = (time.monotonic() - t0) * 1000.0
                return ToolInvocationResult(
                    invocation_id=request.invocation_id,
                    success=False,
                    output=None,
                    error=AhjinError(
                        code="SENSITIVE_FILE_BLOCKED",
                        message=f"Access to sensitive file '{file_path.name}' is prohibited.",
                        category=ErrorCategory.VALIDATION,
                    ),
                    latency_ms=latency_ms,
                )

            if self.path_policy.is_system_blocked(file_path):
                latency_ms = (time.monotonic() - t0) * 1000.0
                return ToolInvocationResult(
                    invocation_id=request.invocation_id,
                    success=False,
                    output=None,
                    error=AhjinError(
                        code="PATH_DENIED",
                        message=f"Access to system file '{file_path.name}' is prohibited.",
                        category=ErrorCategory.VALIDATION,
                    ),
                    latency_ms=latency_ms,
                )

            size_bytes = file_path.stat().st_size
            if size_bytes > _MAX_ATTACHMENT_SIZE_BYTES:
                latency_ms = (time.monotonic() - t0) * 1000.0
                return ToolInvocationResult(
                    invocation_id=request.invocation_id,
                    success=False,
                    output=None,
                    error=AhjinError(
                        code="FILE_TOO_LARGE",
                        message=(
                            f"File '{file_path.name}' exceeds maximum attachment "
                            "size limit of 50 MB."
                        ),
                        category=ErrorCategory.VALIDATION,
                    ),
                    latency_ms=latency_ms,
                )

            display_path = self._compute_display_path(file_path)
            valid_attachments.append(file_path)

            block = (
                "[FILE ATTACHMENT]\n"
                f"file: {file_path.name}\n"
                f"display_path: {display_path}\n"
                f"size: {size_bytes} bytes\n"
                f"type: {file_path.suffix.lower()}\n"
                "status: ready_for_attachment"
            )
            output_blocks.append(block)

        if not valid_attachments:
            latency_ms = (time.monotonic() - t0) * 1000.0
            return ToolInvocationResult(
                invocation_id=request.invocation_id,
                success=False,
                output=None,
                error=AhjinError(
                    code="FILE_NOT_FOUND",
                    message=f"No valid authorized file found for '{target_path_str}'.",
                    category=ErrorCategory.VALIDATION,
                ),
                latency_ms=latency_ms,
            )

        # Output text MUST NOT contain absolute Windows filesystem path (Correction #2)
        output_text = "\n\n".join(output_blocks)

        latency_ms = (time.monotonic() - t0) * 1000.0
        return ToolInvocationResult(
            invocation_id=request.invocation_id,
            success=True,
            output={
                "text": output_text,
                "attachment_paths": [str(p.resolve()) for p in valid_attachments],
            },
            error=None,
            latency_ms=latency_ms,
        )

    def _compute_display_path(self, file_path: Path) -> str:
        """Compute user-friendly relative display path relative to authorized roots."""
        for auth_root in self.path_policy.authorized_roots:
            try:
                rel = file_path.relative_to(auth_root)
                if auth_root == self.path_policy.workspace_root:
                    return rel.as_posix()
                return f"{auth_root.name}/{rel.as_posix()}"
            except ValueError:
                pass
        return file_path.as_posix()
