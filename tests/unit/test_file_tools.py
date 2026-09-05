"""Unit tests for Phase 6C.1 File Intelligence tools."""

import zipfile
from pathlib import Path

import pypdf
import pytest

from ahjin.core.errors import ErrorCategory
from ahjin.security.path_policy import SafePathPolicy
from ahjin.tools.base import ToolInvocationRequest
from ahjin.tools.file_read import FileReadTool
from ahjin.tools.file_search import FileSearchTool


@pytest.fixture
def temp_workspace(tmp_path: Path) -> Path:
    """Create a temporary workspace structure for file tool testing."""
    ws = tmp_path / "workspace"
    ws.mkdir()

    # Safe text files
    src = ws / "src"
    src.mkdir()
    (src / "main.py").write_text("class Application:\n    pass\n", encoding="utf-8")
    (src / "router.py").write_text(
        "class ModelRouter:\n    def route(self):\n        pass\n",
        encoding="utf-8",
    )

    # Nested subfolder
    sub = src / "nested"
    sub.mkdir()
    (sub / "helper.py").write_text("# Helper module for ModelRouter\n", encoding="utf-8")

    # Sensitive files
    (ws / ".env").write_text("SECRET_KEY=supersecret\n", encoding="utf-8")
    (ws / "credentials.json").write_text('{"api_key": "123"}', encoding="utf-8")
    (ws / "cert.pem").write_text("-----BEGIN CERTIFICATE-----", encoding="utf-8")

    # Binary file
    (ws / "data.bin").write_bytes(b"hello\x00world")

    return ws


# ============================================================================
# SafePathPolicy Tests
# ============================================================================

def test_safe_path_policy_valid(temp_workspace: Path) -> None:
    policy = SafePathPolicy(workspace_root=temp_workspace)

    is_safe, resolved, err = policy.validate_safe_path("src/main.py")
    assert is_safe is True
    assert resolved == (temp_workspace / "src" / "main.py").resolve()
    assert err is None


def test_safe_path_policy_traversal(temp_workspace: Path) -> None:
    policy = SafePathPolicy(workspace_root=temp_workspace)

    is_safe, resolved, err = policy.validate_safe_path("../../outside.txt")
    assert is_safe is False
    assert resolved is None
    assert err is not None
    assert "outside" in err


def test_safe_path_policy_sensitive_files(temp_workspace: Path) -> None:
    policy = SafePathPolicy(workspace_root=temp_workspace)

    for sensitive in [".env", "credentials.json", "cert.pem", "my_secret_token.txt"]:
        is_safe, resolved, err = policy.validate_safe_path(sensitive)
        assert is_safe is False
        assert resolved is None
        assert err is not None
        assert "prohibited" in err or "sensitive" in err


# ============================================================================
# FileReadTool Tests
# ============================================================================

@pytest.mark.asyncio
async def test_file_read_success(temp_workspace: Path) -> None:
    policy = SafePathPolicy(workspace_root=temp_workspace)
    tool = FileReadTool(path_policy=policy)

    req = ToolInvocationRequest(tool_name="file_read", parameters={"path": "src/main.py"})
    res = await tool.execute(req)

    assert res.success is True
    assert res.output is not None
    assert "class Application:" in str(res.output)


@pytest.mark.asyncio
async def test_file_read_missing_file(temp_workspace: Path) -> None:
    policy = SafePathPolicy(workspace_root=temp_workspace)
    tool = FileReadTool(path_policy=policy)

    req = ToolInvocationRequest(tool_name="file_read", parameters={"path": "src/nonexistent.py"})
    res = await tool.execute(req)

    assert res.success is False
    assert res.error is not None
    assert res.error.category == ErrorCategory.VALIDATION
    assert "File not found" in res.error.message


@pytest.mark.asyncio
async def test_file_read_directory(temp_workspace: Path) -> None:
    policy = SafePathPolicy(workspace_root=temp_workspace)
    tool = FileReadTool(path_policy=policy)

    req = ToolInvocationRequest(tool_name="file_read", parameters={"path": "src"})
    res = await tool.execute(req)

    assert res.success is False
    assert res.error is not None
    assert res.error.category == ErrorCategory.VALIDATION
    assert "directory" in res.error.message


@pytest.mark.asyncio
async def test_file_read_sensitive_file(temp_workspace: Path) -> None:
    policy = SafePathPolicy(workspace_root=temp_workspace)
    tool = FileReadTool(path_policy=policy)

    req = ToolInvocationRequest(tool_name="file_read", parameters={"path": ".env"})
    res = await tool.execute(req)

    assert res.success is False
    assert res.error is not None
    assert res.error.category == ErrorCategory.VALIDATION


@pytest.mark.asyncio
async def test_file_read_binary_file(temp_workspace: Path) -> None:
    policy = SafePathPolicy(workspace_root=temp_workspace)
    tool = FileReadTool(path_policy=policy)

    req = ToolInvocationRequest(tool_name="file_read", parameters={"path": "data.bin"})
    res = await tool.execute(req)

    assert res.success is False
    assert res.error is not None
    assert res.error.category == ErrorCategory.VALIDATION
    assert "binary" in res.error.message.lower()


# ============================================================================
# FileSearchTool Tests
# ============================================================================

@pytest.mark.asyncio
async def test_file_search_match(temp_workspace: Path) -> None:
    policy = SafePathPolicy(workspace_root=temp_workspace)
    tool = FileSearchTool(path_policy=policy)

    req = ToolInvocationRequest(
        tool_name="file_search",
        parameters={"query": "ModelRouter", "path": "src"},
    )
    res = await tool.execute(req)

    assert res.success is True
    assert res.output is not None
    assert "Found" in str(res.output)
    assert "router.py" in str(res.output)
    assert "helper.py" in str(res.output)


@pytest.mark.asyncio
async def test_file_search_extension_filter(temp_workspace: Path) -> None:
    policy = SafePathPolicy(workspace_root=temp_workspace)
    tool = FileSearchTool(path_policy=policy)

    req = ToolInvocationRequest(
        tool_name="file_search",
        parameters={"query": "ModelRouter", "path": "src", "file_extensions": [".py"]},
    )
    res = await tool.execute(req)

    assert res.success is True
    assert "router.py" in str(res.output)


@pytest.mark.asyncio
async def test_file_search_no_matches(temp_workspace: Path) -> None:
    policy = SafePathPolicy(workspace_root=temp_workspace)
    tool = FileSearchTool(path_policy=policy)

    req = ToolInvocationRequest(
        tool_name="file_search",
        parameters={"query": "NonExistentTermXYZ", "path": "src"},
    )
    res = await tool.execute(req)

    assert res.success is True
    assert "No matches found" in str(res.output)


@pytest.mark.asyncio
async def test_file_search_sensitive_exclusion(temp_workspace: Path) -> None:
    policy = SafePathPolicy(workspace_root=temp_workspace)
    tool = FileSearchTool(path_policy=policy)

    # Query term present in .env
    req = ToolInvocationRequest(
        tool_name="file_search",
        parameters={"query": "SECRET_KEY"},
    )
    res = await tool.execute(req)

    assert res.success is True
    assert ".env" not in str(res.output)


# ============================================================================
# Phase 6C.2 PC-Wide Multi-Root & System Blocking Tests
# ============================================================================

def test_multi_root_path_policy(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    downloads = tmp_path / "Downloads"
    downloads.mkdir()

    (desktop / "resume.txt").write_text("John Doe Resume", encoding="utf-8")
    (downloads / "paper.pdf").write_text("Machine Learning PDF", encoding="utf-8")

    policy = SafePathPolicy(workspace_root=ws, additional_roots=[desktop, downloads])

    # 1. Desktop authorized
    is_safe, resolved, err = policy.validate_safe_path(str(desktop / "resume.txt"))
    assert is_safe is True
    assert resolved == (desktop / "resume.txt").resolve()

    # 2. Downloads authorized
    is_safe, resolved, err = policy.validate_safe_path(str(downloads / "paper.pdf"))
    assert is_safe is True

    # 3. System paths blocked
    for blocked in ["C:\\Windows\\System32\\cmd.exe", "C:\\Program Files\\app.exe", "/etc/passwd"]:
        is_safe, resolved, err = policy.validate_safe_path(blocked)
        assert is_safe is False
        assert err is not None
        assert "prohibited" in err or "outside" in err


@pytest.mark.asyncio
async def test_multi_root_search_pc(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    downloads = tmp_path / "Downloads"
    downloads.mkdir()

    (desktop / "resume.txt").write_text("My Resume for Software Engineer", encoding="utf-8")
    (downloads / "notes.txt").write_text("Resume highlights", encoding="utf-8")

    policy = SafePathPolicy(workspace_root=ws, additional_roots=[desktop, downloads])
    tool = FileSearchTool(path_policy=policy)

    req = ToolInvocationRequest(
        tool_name="file_search",
        parameters={"query": "Resume", "path": "pc"},
    )
    res = await tool.execute(req)

    assert res.success is True
    assert res.output is not None
    assert "resume.txt" in str(res.output)
    assert "notes.txt" in str(res.output)


# ============================================================================
# Deep File/Folder Name Discovery Tests (6C.2 Targeted Fix)
# ============================================================================

@pytest.mark.asyncio
async def test_file_search_deep_nested_discovery(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    downloads = tmp_path / "Downloads"
    deep_dir = downloads / "Archived" / "X_tra" / "P_OGV_NA" / "Resume"
    deep_dir.mkdir(parents=True)

    # Create target resume PDF (binary null byte sample)
    resume_pdf = deep_dir / "Resume.pdf"
    resume_pdf.write_bytes(b"%PDF-1.4 \x00 mock pdf resume binary stream")

    # Create dummy file without resume in name or content
    (downloads / "other.txt").write_text("Hello world", encoding="utf-8")

    policy = SafePathPolicy(workspace_root=ws, additional_roots=[downloads])
    tool = FileSearchTool(path_policy=policy)

    # 1. Test deep filename match
    req = ToolInvocationRequest(
        tool_name="file_search",
        parameters={"query": "resume", "path": "downloads"},
    )
    res = await tool.execute(req)
    assert res.success is True
    assert res.output is not None
    assert "FILE/PATH MATCH" in res.output
    assert "Resume.pdf" in res.output

    # 2. Test deep directory-name match
    (deep_dir / "my_cv.txt").write_text("CV details", encoding="utf-8")
    req_dir = ToolInvocationRequest(
        tool_name="file_search",
        parameters={"query": "Resume", "path": "downloads"},
    )
    res_dir = await tool.execute(req_dir)
    assert res_dir.success is True
    assert "my_cv.txt" in str(res_dir.output)

    # 3. Test extension filtering (.pdf only)
    req_ext = ToolInvocationRequest(
        tool_name="file_search",
        parameters={
            "query": "resume",
            "path": "downloads",
            "file_extensions": [".pdf"],
        },
    )
    res_ext = await tool.execute(req_ext)
    assert res_ext.success is True
    assert "Resume.pdf" in str(res_ext.output)
    assert "my_cv.txt" not in str(res_ext.output)


@pytest.mark.asyncio
async def test_file_search_combined_path_and_content(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "Resume_notes.txt").write_text("resume summary line", encoding="utf-8")

    policy = SafePathPolicy(workspace_root=ws)
    tool = FileSearchTool(path_policy=policy)

    req = ToolInvocationRequest(
        tool_name="file_search",
        parameters={"query": "resume"},
    )
    res = await tool.execute(req)
    assert res.success is True
    assert res.output is not None
    assert "FILE/PATH MATCH" in res.output
    assert "CONTENT MATCH" in res.output
    assert "Resume_notes.txt" in res.output


@pytest.mark.asyncio
async def test_file_search_nested_path_constraint_and_ranking(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    downloads = tmp_path / "Downloads"
    archived = downloads / "Archived" / "X_tra" / "P_OGV_NA" / "Resume"
    archived.mkdir(parents=True)

    target_pdf = archived / "Resume.pdf"
    target_pdf.write_bytes(b"%PDF-1.4 \x00 mock resume stream")

    root_pdf = downloads / "rootfile.pdf"
    root_pdf.write_bytes(b"%PDF-1.4 \x00 root pdf resume stream")

    watch_htm = archived / "watch.htm"
    watch_htm.write_text(
        "<html><script>function resumeVideo(){}</script></html>",
        encoding="utf-8",
    )

    policy = SafePathPolicy(workspace_root=ws, additional_roots=[downloads])
    tool = FileSearchTool(path_policy=policy)

    req = ToolInvocationRequest(
        tool_name="file_search",
        parameters={"query": "resume", "path": "downloads/archived"},
    )
    res = await tool.execute(req)

    assert res.success is True
    assert res.output is not None
    assert "Resume.pdf" in res.output
    assert "rootfile.pdf" not in res.output

    output_str = str(res.output)
    path_idx = output_str.find("[FILE/PATH MATCH]")
    content_idx = output_str.find("[CONTENT MATCH]")
    assert path_idx != -1
    if content_idx != -1:
        assert path_idx < content_idx


@pytest.mark.asyncio
async def test_file_search_nonexistent_nested_path(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    downloads = tmp_path / "Downloads"
    downloads.mkdir()

    policy = SafePathPolicy(workspace_root=ws, additional_roots=[downloads])
    tool = FileSearchTool(path_policy=policy)

    req = ToolInvocationRequest(
        tool_name="file_search",
        parameters={"query": "resume", "path": "downloads/DoesNotExist"},
    )
    res = await tool.execute(req)

    assert res.success is False
    assert res.error is not None
    assert res.error.code == "PATH_NOT_FOUND" or "not found" in res.error.message.lower()


@pytest.mark.asyncio
async def test_file_search_path_traversal_blocked(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    downloads = tmp_path / "Downloads"
    downloads.mkdir()

    policy = SafePathPolicy(workspace_root=ws, additional_roots=[downloads])
    tool = FileSearchTool(path_policy=policy)

    req = ToolInvocationRequest(
        tool_name="file_search",
        parameters={"query": "cmd", "path": "downloads/../../Windows"},
    )
    res = await tool.execute(req)

    assert res.success is False
    assert res.error is not None
    assert res.error.category == ErrorCategory.VALIDATION


# ============================================================================
# Phase 6C.3 PDF & ZIP FileReadTool Tests
# ============================================================================


@pytest.mark.asyncio
async def test_file_read_pdf_extraction(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    pdf_path = ws / "test_doc.pdf"

    # Create a real 2-page PDF using pypdf.PdfWriter
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_blank_page(width=100, height=100)
    with pdf_path.open("wb") as f:
        writer.write(f)

    policy = SafePathPolicy(workspace_root=ws)
    tool = FileReadTool(path_policy=policy)

    # 1. Read entire document
    req = ToolInvocationRequest(tool_name="file_read", parameters={"path": "test_doc.pdf"})
    res = await tool.execute(req)
    assert res.success is True
    assert "2 pages" in str(res.output)
    assert "--- Page 1 ---" in str(res.output)

    # 2. Read specific page_number=2
    req_p2 = ToolInvocationRequest(
        tool_name="file_read",
        parameters={"path": "test_doc.pdf", "content_scope": "page", "page_number": 2},
    )
    res_p2 = await tool.execute(req_p2)
    assert res_p2.success is True
    assert "--- Page 2 ---" in str(res_p2.output)
    assert "--- Page 1 ---" not in str(res_p2.output)


@pytest.mark.asyncio
async def test_file_read_zip_inspection(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    zip_path = ws / "sample.zip"

    # Create a real ZIP file
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("notes.txt", "Sample notes inside zip")
        zf.writestr("src/main.py", "print('hello')")

    policy = SafePathPolicy(workspace_root=ws)
    tool = FileReadTool(path_policy=policy)

    req = ToolInvocationRequest(tool_name="file_read", parameters={"path": "sample.zip"})
    res = await tool.execute(req)

    assert res.success is True
    assert res.output is not None
    assert "ARCHIVE CONTENTS" in str(res.output)
    assert "notes.txt" in str(res.output)
    assert "src/main.py" in str(res.output)



