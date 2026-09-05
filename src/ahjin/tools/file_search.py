"""FileSearchTool — Recursively search files by path/filename and content within bounds."""

import os
import time
from pathlib import Path
from typing import Any, ClassVar, cast

from ahjin.core.errors import AhjinError, ErrorCategory
from ahjin.security.path_policy import SafePathPolicy
from ahjin.tools.base import BaseTool, ToolInvocationRequest, ToolInvocationResult

_MAX_FILES_SCANNED = 2000
_MAX_MATCHES_RETURNED = 50
_MAX_CONTENT_LINES_PER_FILE = 5
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
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
})


class FileSearchTool(BaseTool):
    """Tool for recursively searching files by filename/path and text content within safe bounds.

    Prevents path traversal, respects sensitive file blacklists, supports filename/directory
    discovery across all file types (including PDFs/docs), and performs text line matching on
    unencrypted text files.
    """

    EXCLUDED_DIRECTORY_NAMES: ClassVar[frozenset[str]] = _EXCLUDED_DIRS

    def __init__(self, path_policy: SafePathPolicy | None = None) -> None:
        self.path_policy = path_policy or SafePathPolicy()

    @property
    def tool_name(self) -> str:
        return "file_search"

    async def execute(self, request: ToolInvocationRequest) -> ToolInvocationResult:
        t0 = time.monotonic()
        query = str(request.parameters.get("query", "")).strip()

        if not query:
            latency_ms = (time.monotonic() - t0) * 1000.0
            return ToolInvocationResult(
                invocation_id=request.invocation_id,
                success=False,
                output=None,
                error=AhjinError(
                    code="EMPTY_QUERY",
                    message="No search query provided in parameters.",
                    category=ErrorCategory.VALIDATION,
                ),
                latency_ms=latency_ms,
            )

        sub_path_str = str(request.parameters.get("path", ".")).strip() or "."
        is_safe, search_roots, error_reason = self.path_policy.get_search_roots(sub_path_str)
        if not is_safe or not search_roots:
            latency_ms = (time.monotonic() - t0) * 1000.0
            err_msg = error_reason or f"Access to search path '{sub_path_str}' denied."
            err_code = (
                "PATH_NOT_FOUND"
                if "not found" in err_msg.lower()
                else "SEARCH_PATH_DENIED"
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

        raw_exts_obj: Any = request.parameters.get("file_extensions")
        target_exts: set[str] | None = None
        if isinstance(raw_exts_obj, list):
            raw_ext_list: list[Any] = cast(list[Any], raw_exts_obj)
            valid_ext_strings: list[str] = [
                str(item) for item in raw_ext_list if isinstance(item, str) and item.strip()
            ]
            target_exts = {
                s.lower() if s.startswith(".") else f".{s.lower()}"
                for s in valid_ext_strings
            }

        search_mode = str(request.parameters.get("search_mode", "auto")).strip().lower()

        user_path_matches: list[tuple[int, str]] = []
        user_content_matches: list[str] = []
        code_path_matches: list[tuple[int, str]] = []
        code_content_matches: list[str] = []
        seen_matched_files: set[Path] = set()

        files_scanned = 0
        query_lower = query.lower()

        # Search across target search roots
        for search_root in search_roots:
            total_matches = (
                len(user_path_matches)
                + len(user_content_matches)
                + len(code_path_matches)
                + len(code_content_matches)
            )
            if files_scanned >= _MAX_FILES_SCANNED or total_matches >= _MAX_MATCHES_RETURNED:
                break

            start_dir = search_root if search_root.is_dir() else search_root.parent

            for root, dirs, files in os.walk(start_dir):
                # Prune excluded directories in-place
                dirs[:] = [d for d in dirs if d.lower() not in _EXCLUDED_DIRS]

                for file_name in files:
                    total_matches = (
                        len(user_path_matches)
                        + len(user_content_matches)
                        + len(code_path_matches)
                        + len(code_content_matches)
                    )
                    limit_reached = total_matches >= _MAX_MATCHES_RETURNED
                    if files_scanned >= _MAX_FILES_SCANNED or limit_reached:
                        break

                    file_path = Path(root) / file_name

                    # Validate safety & sensitive blacklist
                    if self.path_policy.is_sensitive_file(file_path):
                        continue

                    if self.path_policy.is_system_blocked(file_path):
                        continue

                    if target_exts and file_path.suffix.lower() not in target_exts:
                        continue

                    files_scanned += 1

                    display_path_str = self._compute_display_path(file_path)
                    display_path_lower = display_path_str.lower()
                    file_name_lower = file_name.lower()
                    file_stem_lower = file_path.stem.lower()

                    # Calculate Path Rank Score (1=exact stem, 2=filename match, 3=path match)
                    path_rank = 999
                    path_matched = False
                    if file_stem_lower == query_lower:
                        path_rank = 1
                        path_matched = True
                    elif query_lower in file_name_lower:
                        path_rank = 2
                        path_matched = True
                    elif (
                        query_lower in display_path_lower
                        or query_lower in file_path.as_posix().lower()
                    ):
                        path_rank = 3
                        path_matched = True

                    content_matches: list[str] = []
                    is_binary = False
                    try:
                        with file_path.open("rb") as f:
                            sample = f.read(1024)
                            if b"\0" in sample:
                                is_binary = True
                    except Exception:
                        is_binary = True

                    if not is_binary and search_mode != "discovery_only":
                        try:
                            content = file_path.read_text(encoding="utf-8", errors="replace")
                            line_count = 0
                            for line_no, line in enumerate(content.splitlines(), start=1):
                                if query_lower in line.lower():
                                    snippet = line.strip()
                                    if len(snippet) > 120:
                                        snippet = snippet[:117] + "..."
                                    loc = f"{display_path_str}:L{line_no}"
                                    c_entry = f"- [CONTENT MATCH] {loc}: {snippet}"
                                    content_matches.append(c_entry)
                                    line_count += 1
                                    if line_count >= _MAX_CONTENT_LINES_PER_FILE:
                                        break
                        except Exception:
                            pass

                    if path_matched or content_matches:
                        if file_path in seen_matched_files:
                            continue
                        seen_matched_files.add(file_path)

                        path_str_lower = str(file_path).lower()
                        is_code_file = (
                            "\\tests\\" in path_str_lower
                            or "/tests/" in path_str_lower
                            or "\\src\\" in path_str_lower
                            or "/src/" in path_str_lower
                        )

                        if path_matched:
                            p_entry = (
                                f"- [FILE/PATH MATCH] {display_path_str} (Full path: {file_path})"
                            )
                            if is_code_file:
                                code_path_matches.append((path_rank, p_entry))
                            else:
                                user_path_matches.append((path_rank, p_entry))

                        for c_entry in content_matches:
                            if is_code_file:
                                code_content_matches.append(c_entry)
                            else:
                                user_content_matches.append(c_entry)

            total_matches = (
                len(user_path_matches)
                + len(user_content_matches)
                + len(code_path_matches)
                + len(code_content_matches)
            )
            if files_scanned >= _MAX_FILES_SCANNED or total_matches >= _MAX_MATCHES_RETURNED:
                break

        # Sort path matches by rank score (1=exact stem, 2=filename contains query, 3=path contains)
        user_path_matches.sort(key=lambda item: item[0])
        code_path_matches.sort(key=lambda item: item[0])

        user_file_entries: list[str] = (
            [item[1] for item in user_path_matches] + user_content_matches
        )
        project_code_entries: list[str] = (
            [item[1] for item in code_path_matches] + code_content_matches
        )

        total_matches = len(user_file_entries) + len(project_code_entries)
        if total_matches == 0:
            output = f"No matches found for query '{query}'."
        else:
            sections: list[str] = [
                f"Found {total_matches} match(es) for query '{query}' "
                f"(scanned {files_scanned} files):"
            ]
            if user_file_entries:
                sections.append("\n[USER FILES & DOCUMENTS]\n" + "\n".join(user_file_entries))
            if project_code_entries:
                sections.append(
                    "\n[PROJECT SOURCE & TEST CODE]\n" + "\n".join(project_code_entries)
                )
            output = "\n".join(sections)

        latency_ms = (time.monotonic() - t0) * 1000.0
        return ToolInvocationResult(
            invocation_id=request.invocation_id,
            success=True,
            output=output,
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
