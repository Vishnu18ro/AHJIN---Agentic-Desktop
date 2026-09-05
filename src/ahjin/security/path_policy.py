"""SafePathPolicy — Enforces authorized PC root containment and sensitive file blacklisting."""

from pathlib import Path
from typing import ClassVar


class SafePathPolicy:
    """Policy for checking and enforcing safe filesystem access across authorized PC roots.

    Authorizes workspace root + standard user folders (Desktop, Documents, Downloads).
    Explicitly blocks system locations (C:\\Windows, Program Files, system dirs),
    rejects path traversal attempts, and blocks access to sensitive credential files.
    """

    SENSITIVE_FILENAMES: ClassVar[frozenset[str]] = frozenset({
        ".env",
        ".env.local",
        ".env.production",
        ".env.development",
        ".env.test",
        ".gitcredentials",
        ".npmrc",
        ".dockercfg",
        "id_rsa",
        "id_ed25519",
    })

    SENSITIVE_EXTENSIONS: ClassVar[frozenset[str]] = frozenset({
        ".pem",
        ".key",
        ".id_rsa",
        ".pfx",
        ".p12",
        ".asc",
    })

    SENSITIVE_SUBSTRINGS: ClassVar[frozenset[str]] = frozenset({
        "credential",
        "secret",
        "private_key",
        "password",
        "passwd",
        "api_key",
        "apikey",
        "auth_token",
    })

    BLOCKED_SYSTEM_NAMES: ClassVar[frozenset[str]] = frozenset({
        "windows",
        "system32",
        "syswow64",
        "program files",
        "program files (x86)",
        "system volume information",
    })

    def __init__(
        self,
        workspace_root: Path | str | None = None,
        additional_roots: list[Path | str] | None = None,
        include_user_folders: bool = True,
    ) -> None:
        if workspace_root is None:
            self.workspace_root = Path.cwd().resolve()
        else:
            self.workspace_root = Path(workspace_root).resolve()

        roots: list[Path] = [self.workspace_root]

        if additional_roots is not None:
            for extra in additional_roots:
                extra_path = Path(extra).resolve()
                if extra_path.exists() and extra_path.is_dir() and extra_path not in roots:
                    roots.append(extra_path)
        elif include_user_folders:
            home = Path.home().resolve()
            onedrive = home / "OneDrive"
            candidate_folders = [
                home / "Desktop",
                onedrive / "Desktop",
                home / "Documents",
                onedrive / "Documents",
                home / "Downloads",
                onedrive / "Downloads",
            ]
            for folder in candidate_folders:
                if folder.exists() and folder.is_dir() and folder not in roots:
                    roots.append(folder)

        self.authorized_roots: list[Path] = roots

    def is_sensitive_file(self, path: Path) -> bool:
        """Check if a file matches sensitive pattern blacklists."""
        name_lower = path.name.lower()

        if name_lower in self.SENSITIVE_FILENAMES:
            return True

        if name_lower.startswith(".env"):
            return True

        if path.suffix.lower() in self.SENSITIVE_EXTENSIONS:
            return True

        for sub in self.SENSITIVE_SUBSTRINGS:
            if sub in name_lower:
                return True

        return False

    def is_system_blocked(self, path: Path) -> bool:
        """Check if a path is inside a blocked system directory."""
        parts_lower = [part.lower() for part in path.parts]
        for blocked_name in self.BLOCKED_SYSTEM_NAMES:
            if blocked_name in parts_lower:
                return True

        path_str_lower = str(path.resolve()).lower()
        if "/etc" in path_str_lower or "/var" in path_str_lower or "/usr" in path_str_lower:
            return True

        return False

    def resolve_shortcut(self, target_str: str) -> Path | None:
        """Resolve standard user folder shortcut keywords (desktop, documents, downloads, pc)."""
        home = Path.home().resolve()
        onedrive = home / "OneDrive"
        clean = target_str.strip().lower().rstrip("/\\.")

        # Alias lookup mapping
        aliases: list[tuple[set[str], str]] = [
            (
                {"desktop", "my desktop", "the desktop", "desktop folder", "on my desktop"},
                "Desktop",
            ),
            (
                {"documents", "my documents", "the documents", "documents folder", "in documents"},
                "Documents",
            ),
            (
                {"downloads", "my downloads", "the downloads", "downloads folder", "in downloads"},
                "Downloads",
            ),
        ]

        for keys, folder_name in aliases:
            if clean in keys or clean.endswith(folder_name.lower()):
                for auth_root in self.authorized_roots:
                    if auth_root.name.lower() == folder_name.lower() and auth_root.exists():
                        return auth_root.resolve()
                for candidate in [home / folder_name, onedrive / folder_name]:
                    if candidate.exists() and candidate.is_dir():
                        return candidate.resolve()

        if clean in ("workspace", "workspace/"):
            return self.workspace_root

        # Handle folder prefix paths like "desktop/my_file.txt" or "downloads/resume.pdf"
        for prefix, folder_name in [
            ("desktop", "Desktop"),
            ("documents", "Documents"),
            ("downloads", "Downloads"),
        ]:
            if clean.startswith(f"{prefix}/") or clean.startswith(f"{prefix}\\"):
                rel_part = target_str.strip()[len(prefix) + 1 :]
                candidates = [
                    r for r in self.authorized_roots if r.name.lower() == folder_name.lower()
                ]
                candidates.extend([home / folder_name, onedrive / folder_name])
                for candidate_root in candidates:
                    p = candidate_root / rel_part
                    if p.exists() or p.parent.exists():
                        return p.resolve()

        return None

    def validate_safe_path(self, target_path_str: str) -> tuple[bool, Path | None, str | None]:
        """Validate and resolve a path string against authorized PC root containment.

        Returns:
            (True, resolved_path, None) if path is safe and authorized.
            (False, None, error_reason) if path violates policy.
        """
        if not target_path_str or not target_path_str.strip():
            return False, None, "Empty path provided."

        clean_str = target_path_str.strip()

        # 1. Check for folder shortcuts
        shortcut_path = self.resolve_shortcut(clean_str)
        if shortcut_path is not None:
            resolved_path = shortcut_path
        else:
            try:
                input_path = Path(clean_str)
                if input_path.is_absolute():
                    resolved_path = input_path.resolve()
                else:
                    # Check relative to workspace root first
                    candidate = (self.workspace_root / input_path).resolve()
                    if candidate.exists():
                        resolved_path = candidate
                    else:
                        # Check relative to other authorized roots
                        found_candidate = None
                        for root in self.authorized_roots:
                            c = (root / input_path).resolve()
                            if c.exists():
                                found_candidate = c
                        if found_candidate is not None:
                            resolved_path = found_candidate
                        else:
                            resolved_path = candidate
            except Exception as exc:
                return False, None, f"Invalid path syntax: {exc}"

        # 2. System path blockage check
        if self.is_system_blocked(resolved_path):
            return False, None, f"Access to system location '{clean_str}' is prohibited."

        # 3. Authorized root containment check
        is_contained = False
        for root in self.authorized_roots:
            try:
                resolved_path.relative_to(root)
                is_contained = True
                break
            except ValueError:
                continue

        if not is_contained:
            return False, None, f"Path '{clean_str}' is outside authorized PC roots."

        # 4. Sensitive file check
        if self.is_sensitive_file(resolved_path):
            return False, None, f"Access to sensitive file '{resolved_path.name}' is prohibited."

        return True, resolved_path, None

    def get_search_roots(self, sub_path_str: str) -> tuple[bool, list[Path], str | None]:
        """Get the list of target search root directories for a search request.

        If sub_path_str is '.', '', 'pc', 'user_roots', or 'all', returns all authorized roots.
        Otherwise validates and returns the specific authorized search directory.
        """
        clean = sub_path_str.strip().lower()
        if clean in (".", "", "pc", "all_roots", "user_roots", "all", "my_pc", "my pc"):
            return True, self.authorized_roots, None

        shortcut = self.resolve_shortcut(clean)
        if shortcut is not None and shortcut.exists() and shortcut.is_dir():
            if not self.is_system_blocked(shortcut):
                return True, [shortcut], None

        is_safe, resolved, err = self.validate_safe_path(sub_path_str)
        if not is_safe or resolved is None:
            return False, [], err

        if not resolved.exists():
            return False, [], f"Search path not found: '{sub_path_str}'."

        start_dir = resolved if resolved.is_dir() else resolved.parent
        return True, [start_dir], None
