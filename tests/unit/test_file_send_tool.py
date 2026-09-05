"""Unit tests for Phase 6C.3 FileSendTool attachment preparation and security."""

from pathlib import Path

import pytest

from ahjin.core.errors import ErrorCategory
from ahjin.security.path_policy import SafePathPolicy
from ahjin.tools.base import ToolInvocationRequest
from ahjin.tools.file_send import FileSendTool


@pytest.fixture
def temp_send_workspace(tmp_path: Path) -> tuple[Path, Path]:
    """Create temporary workspace and user downloads folder for FileSendTool testing."""
    ws = tmp_path / "ws"
    ws.mkdir()
    downloads = tmp_path / "Downloads"
    downloads.mkdir()

    # 1. Safe text file
    (downloads / "notes.txt").write_text("Meeting notes content", encoding="utf-8")

    # 2. Mock PDF file
    (downloads / "paper.pdf").write_bytes(b"%PDF-1.4 mock pdf data")

    # 3. Mock ZIP file
    (downloads / "archive.zip").write_bytes(b"PK\x03\x04 mock zip data")

    # 4. Sensitive file
    (ws / ".env").write_text("SECRET=123", encoding="utf-8")

    return ws, downloads


@pytest.mark.asyncio
async def test_file_send_text_success(temp_send_workspace: tuple[Path, Path]) -> None:
    ws, downloads = temp_send_workspace
    policy = SafePathPolicy(workspace_root=ws, additional_roots=[downloads])
    tool = FileSendTool(path_policy=policy)

    req = ToolInvocationRequest(
        tool_name="file_send",
        parameters={"path": "downloads/notes.txt"},
    )
    res = await tool.execute(req)

    assert res.success is True
    assert isinstance(res.output, dict)
    assert "ready_for_attachment" in res.output["text"]
    assert "Downloads/notes.txt" in res.output["text"]
    # Verify absolute Windows filesystem path is NOT in model-facing text (Correction #2)
    assert str(downloads.resolve()) not in res.output["text"]
    # Verify internal attachment_paths contains canonical resolved path
    assert len(res.output["attachment_paths"]) == 1
    assert res.output["attachment_paths"][0] == str((downloads / "notes.txt").resolve())


@pytest.mark.asyncio
async def test_file_send_pdf_success(temp_send_workspace: tuple[Path, Path]) -> None:
    ws, downloads = temp_send_workspace
    policy = SafePathPolicy(workspace_root=ws, additional_roots=[downloads])
    tool = FileSendTool(path_policy=policy)

    req = ToolInvocationRequest(
        tool_name="file_send",
        parameters={"path": "downloads/paper.pdf"},
    )
    res = await tool.execute(req)

    assert res.success is True
    assert isinstance(res.output, dict)
    assert "ready_for_attachment" in res.output["text"]
    assert "paper.pdf" in res.output["text"]


@pytest.mark.asyncio
async def test_file_send_zip_success(temp_send_workspace: tuple[Path, Path]) -> None:
    ws, downloads = temp_send_workspace
    policy = SafePathPolicy(workspace_root=ws, additional_roots=[downloads])
    tool = FileSendTool(path_policy=policy)

    req = ToolInvocationRequest(
        tool_name="file_send",
        parameters={"path": "downloads/archive.zip"},
    )
    res = await tool.execute(req)

    assert res.success is True
    assert isinstance(res.output, dict)
    assert "ready_for_attachment" in res.output["text"]
    assert "archive.zip" in res.output["text"]


@pytest.mark.asyncio
async def test_file_send_unauthorized_path(temp_send_workspace: tuple[Path, Path]) -> None:
    ws, downloads = temp_send_workspace
    policy = SafePathPolicy(workspace_root=ws)  # downloads is NOT an authorized root here
    tool = FileSendTool(path_policy=policy)

    req = ToolInvocationRequest(
        tool_name="file_send",
        parameters={"path": str(downloads / "paper.pdf")},
    )
    res = await tool.execute(req)

    assert res.success is False
    assert res.error is not None
    assert res.error.category == ErrorCategory.VALIDATION


@pytest.mark.asyncio
async def test_file_send_sensitive_file(temp_send_workspace: tuple[Path, Path]) -> None:
    ws, downloads = temp_send_workspace
    policy = SafePathPolicy(workspace_root=ws, additional_roots=[downloads])
    tool = FileSendTool(path_policy=policy)

    req = ToolInvocationRequest(
        tool_name="file_send",
        parameters={"path": ".env"},
    )
    res = await tool.execute(req)

    assert res.success is False
    assert res.error is not None
    assert res.error.code == "SENSITIVE_FILE_BLOCKED" or "prohibited" in res.error.message.lower()


@pytest.mark.asyncio
async def test_file_send_path_traversal(temp_send_workspace: tuple[Path, Path]) -> None:
    ws, downloads = temp_send_workspace
    policy = SafePathPolicy(workspace_root=ws, additional_roots=[downloads])
    tool = FileSendTool(path_policy=policy)

    req = ToolInvocationRequest(
        tool_name="file_send",
        parameters={"path": "../../Windows/System32/cmd.exe"},
    )
    res = await tool.execute(req)

    assert res.success is False
    assert res.error is not None
    assert res.error.category == ErrorCategory.VALIDATION
