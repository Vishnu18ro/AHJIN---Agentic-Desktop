"""FileSearchTool — Recursively search files by path/filename and content within bounds."""

import os
import time
from pathlib import Path
from typing import Any, ClassVar, cast

from ahjin.core.errors import AhjinError, ErrorCategory
from ahjin.security.path_policy import SafePathPolicy
from ahjin.tools.base import BaseTool, ToolInvocationRequest, ToolInvocationResult

_MAX_FILES_SCANNED = 20000
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
    ".cache",
    "appdata",
    "$recycle.bin",
})


def _is_excluded_directory(dir_name: str, parent_dir: Path | None = None) -> bool:
    """Check whether a directory is an irrelevant temporary, cache, or excluded directory.

    Prevents temporary folders (e.g. .tmp.driveupload, .cache) and nested git repos
    from starving legitimate user directories within the _MAX_FILES_SCANNED budget.
    """
    dl = dir_name.lower()
    if dl in _EXCLUDED_DIRS:
        return True
    if dl.startswith((".", "$recycle.bin")):
        return True
    if dl.endswith(".tmp") or dl in ("temp", "tmp"):
        return True
    if parent_dir is not None:
        try:
            if (parent_dir / dir_name / ".git").exists():
                return True
        except (OSError, PermissionError):
            pass
    return False


class SearchResultString(str):
    """String subclass that preserves discovered_paths and is_ambiguous metadata."""

    discovered_paths: list[str]
    is_ambiguous: bool

    def __new__(
        cls,
        text: str,
        discovered_paths: list[str] | None = None,
        is_ambiguous: bool = False,
    ) -> "SearchResultString":
        obj = super().__new__(cls, text)
        obj.discovered_paths = discovered_paths or []
        obj.is_ambiguous = is_ambiguous
        return obj

    def get(self, key: str, default: Any = None) -> Any:
        if key == "text":
            return str(self)
        if key == "discovered_paths":
            return self.discovered_paths
        if key == "is_ambiguous":
            return self.is_ambiguous
        return default


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

        user_path_matches: list[tuple[int, str, Path]] = []
        user_content_matches: list[str] = []
        code_path_matches: list[tuple[int, str, Path]] = []
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
                total_matches = (
                    len(user_path_matches)
                    + len(user_content_matches)
                    + len(code_path_matches)
                    + len(code_content_matches)
                )
                if files_scanned >= _MAX_FILES_SCANNED or total_matches >= _MAX_MATCHES_RETURNED:
                    break

                # Prune excluded and temporary directories in-place
                root_path = Path(root)
                dirs[:] = [d for d in dirs if not _is_excluded_directory(d, root_path)]

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
                                code_path_matches.append((path_rank, p_entry, file_path))
                            else:
                                user_path_matches.append((path_rank, p_entry, file_path))

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

        ranked_user_paths = [item[2] for item in user_path_matches]
        ranked_code_paths = [item[2] for item in code_path_matches]
        all_discovered_paths = ranked_user_paths + ranked_code_paths

        # Ambiguity check: if multiple distinct candidate files share the top score
        is_ambiguous = False
        if len(user_path_matches) > 1:
            top_rank = user_path_matches[0][0]
            top_rank_files = [item[2] for item in user_path_matches if item[0] == top_rank]
            if len(top_rank_files) > 1:
                # Ambiguous if multiple different file candidates have the same best rank
                is_ambiguous = True

        total_matches = len(user_file_entries) + len(project_code_entries)
        if total_matches == 0:
            output = f"No matches found for query '{query}'."
        else:
            header_prefix = (
                f"Multiple matching files found ({total_matches} candidates)"
                if is_ambiguous
                else f"Found {total_matches} match(es)"
            )
            sections: list[str] = [
                f"{header_prefix} for query '{query}' (scanned {files_scanned} files):"
            ]
            if user_file_entries:
                sections.append("\n[USER FILES & DOCUMENTS]\n" + "\n".join(user_file_entries))
            if project_code_entries:
                sections.append(
                    "\n[PROJECT SOURCE & TEST CODE]\n" + "\n".join(project_code_entries)
                )
            output = "\n".join(sections)

        latency_ms = (time.monotonic() - t0) * 1000.0
        output_res = SearchResultString(
            output,
            discovered_paths=[str(p) for p in all_discovered_paths],
            is_ambiguous=is_ambiguous,
        )
        return ToolInvocationResult(
            invocation_id=request.invocation_id,
            success=True,
            output=output_res,
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

    def find_matching_files(
        self,
        query: str,
        path_str: str = ".",
        file_extensions: list[str] | None = None,
        max_results: int = 10,
    ) -> list[Path]:
        """Programmatically find and rank matching files across authorized roots.

        Returns ranked list of Path objects (exact stem match > filename match > path match).
        Used by FileAgent for conversational multi-turn file disambiguation.
        """
        clean_query = query.strip().lower()
        if not clean_query:
            return []

        is_safe, search_roots, _ = self.path_policy.get_search_roots(path_str)
        if not is_safe or not search_roots:
            return []

        target_exts: set[str] | None = None
        if file_extensions:
            target_exts = {
                e.lower() if e.startswith(".") else f".{e.lower()}"
                for e in file_extensions
                if isinstance(e, str) and e.strip()
            }

        ranked_matches: list[tuple[int, Path]] = []
        seen: set[Path] = set()
        scanned = 0

        for search_root in search_roots:
            if len(ranked_matches) >= max_results or scanned >= _MAX_FILES_SCANNED:
                break
            start_dir = search_root if search_root.is_dir() else search_root.parent
            if not start_dir.exists():
                continue
            for root, dirs, files in os.walk(start_dir):
                if len(ranked_matches) >= max_results or scanned >= _MAX_FILES_SCANNED:
                    break
                root_path = Path(root)
                dirs[:] = [d for d in dirs if not _is_excluded_directory(d, root_path)]
                for file_name in files:
                    if len(ranked_matches) >= max_results or scanned >= _MAX_FILES_SCANNED:
                        break
                    file_path = Path(root) / file_name
                    if self.path_policy.is_sensitive_file(file_path):
                        continue
                    if self.path_policy.is_system_blocked(file_path):
                        continue
                    if target_exts and file_path.suffix.lower() not in target_exts:
                        continue
                    scanned += 1

                    file_name_lower = file_name.lower()
                    file_stem_lower = file_path.stem.lower()
                    display_path_lower = self._compute_display_path(file_path).lower()

                    rank = 999
                    if file_stem_lower == clean_query:
                        rank = 1
                    elif clean_query in file_name_lower:
                        rank = 2
                    elif (
                        clean_query in display_path_lower
                        or clean_query in file_path.as_posix().lower()
                    ):
                        rank = 3

                    if rank < 999 and file_path not in seen:
                        seen.add(file_path)
                        ranked_matches.append((rank, file_path))

        ranked_matches.sort(key=lambda x: x[0])
        return [p for _, p in ranked_matches[:max_results]]
