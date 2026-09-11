"""FileSendTool — Prepare safe filesystem files for interface chat attachment delivery."""

import os
import time
from pathlib import Path
from typing import Any, cast

from ahjin.core.errors import AhjinError, ErrorCategory
from ahjin.security.path_policy import SafePathPolicy
from ahjin.tools.base import BaseTool, ToolInvocationRequest, ToolInvocationResult

_MAX_ATTACHMENT_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB limit
_MAX_BATCH_ATTACHMENTS = 5
_MAX_FILES_SCANNED = 500
_EXCLUDED_DIRS: frozenset[str] = frozenset({
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    ".idea",
    ".vscode",
    "build",
    "dist",
})


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
        path_param = request.parameters.get("path")
        query_param = request.parameters.get("query")
        target_path_str = str(path_param or query_param or "").strip()

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

                query_filter = str(request.parameters.get("query", "")).strip().lower()
                skip_roots = ("pc", "downloads", "desktop", "documents", ".")
                has_active_query = bool(query_filter and query_filter not in skip_roots)

                dir_files: list[Path] = []
                for child in resolved_path.rglob("*"):
                    if (
                        child.is_file()
                        and not self.path_policy.is_sensitive_file(child)
                        and not self.path_policy.is_system_blocked(child)
                    ):
                        if target_exts and child.suffix.lower() not in target_exts:
                            continue
                        dir_files.append(child)

                if dir_files:
                    if has_active_query:
                        scored_candidates: list[tuple[int, float, Path]] = []
                        preferred_exts = {".pdf", ".docx", ".doc", ".txt"}
                        for child in dir_files:
                            stem_lower = child.stem.lower()
                            name_lower = child.name.lower()
                            if query_filter not in name_lower and query_filter not in stem_lower:
                                continue

                            # Rank scoring:
                            # 1: exact stem match (e.g. resume == resume.pdf)
                            # 2: stem stripped of trailing punctuation (e.g. resume- -> resume)
                            # 3: stem starts with query (e.g. resumeI.pdf, resume_2026.pdf)
                            # 4: stem starts with "my " + query (e.g. my resume.pdf)
                            # 5: stem ends with query (e.g. tcs resume.pdf)
                            # 6: other match with preferred document extension
                            clean_stem = stem_lower.rstrip(" -_.")
                            if stem_lower == query_filter:
                                rank = 1
                            elif clean_stem == query_filter:
                                rank = 2
                            elif stem_lower.startswith(query_filter):
                                rank = 3
                            elif stem_lower.startswith(f"my {query_filter}"):
                                rank = 4
                            elif stem_lower.endswith(query_filter):
                                rank = 5
                            elif child.suffix.lower() in preferred_exts:
                                rank = 6
                            else:
                                rank = 7

                            try:
                                mtime = child.stat().st_mtime
                            except Exception:
                                mtime = 0.0

                            scored_candidates.append((rank, mtime, child))

                        if scored_candidates:
                            # Sort by rank ascending (1 best), then mtime descending (newest first)
                            scored_candidates.sort(key=lambda item: (item[0], -item[1]))
                            best_rank = scored_candidates[0][0]
                            top_tier = [item for item in scored_candidates if item[0] == best_rank]
                            allow_batch = bool(request.parameters.get("batch", False))
                            max_files = _MAX_BATCH_ATTACHMENTS if allow_batch else 1
                            for _, _, best_file in top_tier:
                                candidate_files.append(best_file)
                                if len(candidate_files) >= max_files:
                                    break
                    elif len(dir_files) == 1:
                        candidate_files.append(dir_files[0])
                    else:
                        # Multiple files present without a query to disambiguate
                        latency_ms = (time.monotonic() - t0) * 1000.0
                        samples = [f.name for f in dir_files[:4]]
                        sample_str = ", ".join(samples)
                        return ToolInvocationResult(
                            invocation_id=request.invocation_id,
                            success=False,
                            output=None,
                            error=AhjinError(
                                code="AMBIGUOUS_DIRECTORY_TARGET",
                                message=(
                                    f"Directory '{resolved_path.name}' contains multiple files. "
                                    f"Please specify which file to send (e.g. {sample_str})."
                                ),
                                category=ErrorCategory.VALIDATION,
                            ),
                            latency_ms=latency_ms,
                        )

        if not candidate_files:
            # 2. Try search_roots for keyword / shortcut / relative subpath discovery
            search_target = (
                str(query_param).strip()
                if (target_path_str in (".", "") and query_param)
                else target_path_str
            )
            is_roots_safe, search_roots, _ = self.path_policy.get_search_roots(
                search_target
            )
            if not is_roots_safe or not search_roots:
                # If target was relative or not found as shortcut, try all authorized roots
                if target_path_str in (".", "") or query_param:
                    _, search_roots, _ = self.path_policy.get_search_roots(".")

            if search_roots:
                query_term = str(query_param or target_path_str).strip().lower()
                scanned_count = 0
                for s_root in search_roots:
                    if s_root.is_file() and not self.path_policy.is_sensitive_file(s_root):
                        candidate_files.append(s_root)
                    elif s_root.is_dir():
                        for root_dir, dirs, files in os.walk(s_root):
                            dirs[:] = [d for d in dirs if d.lower() not in _EXCLUDED_DIRS]
                            for fname in files:
                                scanned_count += 1
                                if scanned_count > _MAX_FILES_SCANNED:
                                    break
                                child = Path(root_dir) / fname
                                if (
                                    not self.path_policy.is_sensitive_file(child)
                                    and not self.path_policy.is_system_blocked(child)
                                ):
                                    skip_terms = ("pc", "downloads", "desktop", "documents", ".")
                                    if query_term and query_term not in skip_terms:
                                        if (
                                            query_term not in child.name.lower()
                                            and query_term not in child.stem.lower()
                                        ):
                                            continue
                                    candidate_files.append(child)
                            hit_limit = (
                                len(candidate_files) >= _MAX_BATCH_ATTACHMENTS
                                or scanned_count > _MAX_FILES_SCANNED
                            )
                            if hit_limit:
                                break
                        if candidate_files or scanned_count > _MAX_FILES_SCANNED:
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
