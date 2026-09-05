"""FileReadTool — Safely read text files, PDF documents, and ZIP archives."""

import time
import zipfile
from pathlib import Path
from typing import Any

import pypdf

from ahjin.core.errors import AhjinError, ErrorCategory
from ahjin.security.path_policy import SafePathPolicy
from ahjin.tools.base import BaseTool, ToolInvocationRequest, ToolInvocationResult

_MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB limit for text/PDF files
_MAX_ZIP_ENTRIES = 500


class FileReadTool(BaseTool):
    """Tool for reading safe text files, PDF documents, and ZIP archives within authorized bounds.

    Strictly rejects directory paths, path traversal, unknown binary files, sensitive
    credential files, and files outside authorized roots.
    Supports PDF page-by-page text extraction, ZIP archive structure inspection, and
    content_scope filtering (page, relevant, entire_document, metadata, archive_listing).
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
                    message=f"Path '{path_str}' is a directory, not a readable file.",
                    category=ErrorCategory.VALIDATION,
                ),
                latency_ms=latency_ms,
            )

        display_path = self._compute_display_path(resolved_path)
        content_scope = str(request.parameters.get("content_scope", "entire_document")).lower()
        page_no_param: Any = request.parameters.get("page_number")
        page_number: int | None = int(page_no_param) if isinstance(page_no_param, int) else None
        query_param = request.parameters.get("query")
        query: str | None = str(query_param).strip() if query_param else None

        suffix = resolved_path.suffix.lower()

        # ── 1. PDF FILE HANDLING ─────────────────────────────────────────────
        if suffix == ".pdf":
            try:
                output_text = self._read_pdf_file(
                    resolved_path,
                    display_path,
                    content_scope=content_scope,
                    page_number=page_number,
                    query=query,
                )
                latency_ms = (time.monotonic() - t0) * 1000.0
                return ToolInvocationResult(
                    invocation_id=request.invocation_id,
                    success=True,
                    output=output_text,
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
                        code="PDF_EXTRACTION_FAILED",
                        message=f"PDF extraction failed for '{display_path}': {exc}",
                        category=ErrorCategory.TOOL,
                    ),
                    latency_ms=latency_ms,
                )

        # ── 2. ZIP ARCHIVE HANDLING ──────────────────────────────────────────
        if suffix == ".zip":
            try:
                output_text = self._inspect_zip_archive(resolved_path, display_path)
                latency_ms = (time.monotonic() - t0) * 1000.0
                return ToolInvocationResult(
                    invocation_id=request.invocation_id,
                    success=True,
                    output=output_text,
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
                        code="ZIP_INVALID",
                        message=f"ZIP inspection failed for '{display_path}': {exc}",
                        category=ErrorCategory.TOOL,
                    ),
                    latency_ms=latency_ms,
                )

        # ── 3. STANDARD TEXT FILE HANDLING ────────────────────────────────────
        # Detect unknown binary content
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
                            message=f"Cannot read binary file '{display_path}'.",
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
                    message=f"Error checking file '{display_path}': {exc}",
                    category=ErrorCategory.TOOL,
                ),
                latency_ms=latency_ms,
            )

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
                        f"File '{display_path}' exceeds maximum allowed size of "
                        f"{_MAX_FILE_SIZE_BYTES // (1024 * 1024)} MB."
                    ),
                    category=ErrorCategory.VALIDATION,
                ),
                latency_ms=latency_ms,
            )

        try:
            raw_content = resolved_path.read_text(encoding="utf-8", errors="replace")

            if content_scope == "metadata":
                lines = raw_content.splitlines()
                output_text = (
                    f"[FILE METADATA: {display_path}]\n"
                    f"Format: text ({resolved_path.suffix.lower()})\n"
                    f"Size: {file_size} bytes\n"
                    f"Line count: {len(lines)}"
                )
            elif content_scope == "relevant" and query:
                matching_lines: list[str] = []
                for line_no, line in enumerate(raw_content.splitlines(), start=1):
                    if query.lower() in line.lower():
                        snippet = line.strip()
                        if len(snippet) > 120:
                            snippet = snippet[:117] + "..."
                        matching_lines.append(f"L{line_no}: {snippet}")
                if matching_lines:
                    output_text = (
                        f"[FILE CONTENT: {display_path} (Matches for '{query}')]\n"
                        + "\n".join(matching_lines[:30])
                    )
                else:
                    output_text = (
                        f"[FILE CONTENT: {display_path}]\nNo lines matching '{query}'."
                    )
            else:
                output_text = f"--- FILE: {display_path} ---\n{raw_content}"

            latency_ms = (time.monotonic() - t0) * 1000.0
            return ToolInvocationResult(
                invocation_id=request.invocation_id,
                success=True,
                output=output_text,
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
                    code="FILE_READ_FAILED",
                    message=f"Failed to read file '{display_path}': {exc}",
                    category=ErrorCategory.TOOL,
                ),
                latency_ms=latency_ms,
            )

    def _read_pdf_file(
        self,
        file_path: Path,
        display_path: str,
        content_scope: str,
        page_number: int | None,
        query: str | None,
    ) -> str:
        """Extract text from a PDF file page-by-page using pypdf."""
        reader = pypdf.PdfReader(file_path)
        num_pages = len(reader.pages)

        if num_pages == 0:
            return f"[FILE CONTENT: {display_path}]\n(PDF document contains 0 pages)"

        if content_scope == "metadata":
            return (
                f"[FILE METADATA: {display_path}]\n"
                "Format: PDF document\n"
                f"Page count: {num_pages}\n"
                f"Size: {file_path.stat().st_size} bytes"
            )

        pages_to_extract: list[int] = []
        if page_number is not None:
            if 1 <= page_number <= num_pages:
                pages_to_extract = [page_number]
            else:
                return (
                    f"[FILE CONTENT: {display_path}]\n"
                    f"Requested page {page_number} is out of bounds (total pages: {num_pages})."
                )
        else:
            pages_to_extract = list(range(1, num_pages + 1))

        page_blocks: list[str] = []
        total_chars = 0
        max_chars = 50000

        for p_num in pages_to_extract:
            if total_chars >= max_chars:
                page_blocks.append(
                    f"\n[Page {p_num}]\n(Truncated: Max character limit reached)"
                )
                break

            try:
                page_obj = reader.pages[p_num - 1]
                extracted_text = (page_obj.extract_text() or "").strip()
            except Exception as page_err:
                extracted_text = f"(Error extracting page {p_num}: {page_err})"

            if not extracted_text:
                extracted_text = "(Empty page or unscannable text)"

            if content_scope == "relevant" and query:
                if query.lower() not in extracted_text.lower():
                    continue

            block = f"--- Page {p_num} ---\n{extracted_text}"
            page_blocks.append(block)
            total_chars += len(block)

        if not page_blocks:
            if content_scope == "relevant" and query:
                return f"[FILE CONTENT: {display_path}]\nNo pages found matching query '{query}'."
            return f"[FILE CONTENT: {display_path}]\n(No text could be extracted from PDF)"

        header = f"[FILE CONTENT: {display_path} ({num_pages} pages)]\n"
        return header + "\n\n".join(page_blocks)

    def _inspect_zip_archive(self, file_path: Path, display_path: str) -> str:
        """Safely inspect ZIP archive contents without extracting files to disk."""
        with zipfile.ZipFile(file_path, "r") as zf:
            infolist = zf.infolist()
            total_entries = len(infolist)
            if total_entries > _MAX_ZIP_ENTRIES:
                infolist = infolist[:_MAX_ZIP_ENTRIES]
                truncated = True
            else:
                truncated = False

            lines: list[str] = [
                f"[ARCHIVE CONTENTS: {display_path}] (Total entries: {total_entries})",
                "--------------------------------------------------",
            ]

            total_uncompressed = 0
            for info in infolist:
                entry_name = info.filename
                if (
                    ".." in entry_name
                    or entry_name.startswith("/")
                    or entry_name.startswith("\\")
                ):
                    lines.append(f"- [BLOCKED TRAVERSAL ENTRY]: {entry_name}")
                    continue

                size_bytes = info.file_size
                total_uncompressed += size_bytes
                kind = "DIR" if info.is_dir() else "FILE"
                lines.append(f"- [{kind}] {entry_name} ({size_bytes} bytes)")

            if truncated:
                lines.append("\n(Truncated: Showing first 500 entries)")

            lines.append(f"\nTotal uncompressed size: {total_uncompressed} bytes")
            return "\n".join(lines)

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
