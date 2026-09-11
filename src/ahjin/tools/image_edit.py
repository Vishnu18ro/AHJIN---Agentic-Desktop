"""ImageEditTool — Resize, compress, or manipulate image files."""

import io
import time
from pathlib import Path
from typing import Any

try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

from ahjin.core.errors import AhjinError, ErrorCategory
from ahjin.security.path_policy import SafePathPolicy
from ahjin.tools.base import BaseTool, ToolInvocationRequest, ToolInvocationResult


class ImageEditTool(BaseTool):
    """Tool for editing or compressing local image files."""

    def __init__(self, path_policy: SafePathPolicy | None = None) -> None:
        self.path_policy = path_policy or SafePathPolicy()

    @property
    def tool_name(self) -> str:
        return "image_edit"

    async def execute(self, request: ToolInvocationRequest) -> ToolInvocationResult:
        t0 = time.monotonic()

        if not HAS_PILLOW:
            return ToolInvocationResult(
                invocation_id=request.invocation_id,
                success=False,
                output=None,
                error=AhjinError(
                    code="MISSING_DEPENDENCY",
                    message="Pillow is not installed. Please install Pillow to use ImageEditTool.",
                    category=ErrorCategory.INTERNAL,
                ),
                latency_ms=(time.monotonic() - t0) * 1000.0,
            )

        target_path_str = str(request.parameters.get("path", "")).strip()
        operation = str(request.parameters.get("operation", "")).strip().lower()

        if not target_path_str:
            return self._err(request, "EMPTY_PATH", "No image path provided.", t0)
        
        if operation not in ("compress", "enlarge", "resize"):
            return self._err(request, "INVALID_OPERATION", f"Invalid operation: {operation}", t0)

        # Try to resolve path
        is_safe, resolved_path, error_reason = self.path_policy.validate_safe_path(target_path_str)
        target_file: Path | None = None

        if is_safe and resolved_path is not None and resolved_path.exists() and resolved_path.is_file():
            target_file = resolved_path
        else:
            # Fallback search if exact path not matched (similar to file_send)
            is_roots_safe, search_roots, _ = self.path_policy.get_search_roots(target_path_str)
            if is_roots_safe and search_roots:
                for s_root in search_roots:
                    if s_root.is_file() and s_root.name.lower() in target_path_str.lower():
                        target_file = s_root
                        break
                    elif s_root.is_dir():
                        for child in s_root.rglob("*"):
                            if child.is_file() and child.name.lower() in target_path_str.lower():
                                target_file = child
                                break
                        if target_file:
                            break

        if not target_file or not target_file.exists():
            return self._err(request, "FILE_NOT_FOUND", f"Could not find file: {target_path_str}", t0)
        
        if self.path_policy.is_sensitive_file(target_file):
            return self._err(request, "SENSITIVE_FILE", "Access to this file is prohibited.", t0)

        try:
            with Image.open(target_file) as img:
                original_format = img.format or target_file.suffix.lstrip(".").upper()
                if original_format.upper() == "JPG":
                    original_format = "JPEG"
                
                # RGB mode is required for JPEG saving
                if original_format == "JPEG" and img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                    
                processed_img = img.copy()

                # Process
                if operation == "enlarge" or operation == "resize":
                    scale_str = request.parameters.get("scale")
                    dim_str = request.parameters.get("target_dimensions")
                    
                    if scale_str and isinstance(scale_str, str) and scale_str.endswith("x"):
                        try:
                            factor = float(scale_str[:-1])
                            new_size = (int(img.width * factor), int(img.height * factor))
                            processed_img = processed_img.resize(new_size, Image.Resampling.LANCZOS)
                        except ValueError:
                            pass
                    elif dim_str and isinstance(dim_str, str) and "x" in dim_str.lower():
                        try:
                            parts = dim_str.lower().split("x")
                            new_size = (int(parts[0]), int(parts[1]))
                            processed_img = processed_img.resize(new_size, Image.Resampling.LANCZOS)
                        except ValueError:
                            pass
                
                # Compress / Save
                target_kb = request.parameters.get("target_kb")
                out_path = target_file.with_name(f"{target_file.stem}_processed{target_file.suffix}")
                
                if operation == "compress" and target_kb and isinstance(target_kb, (int, float)):
                    target_bytes = target_kb * 1024
                    quality = 95
                    
                    # Iteratively compress
                    while quality > 10:
                        buffer = io.BytesIO()
                        if original_format == "JPEG":
                            processed_img.save(buffer, format=original_format, quality=quality, optimize=True)
                        else:
                            processed_img.save(buffer, format=original_format)
                            
                        if buffer.tell() <= target_bytes or original_format != "JPEG":
                            break
                        quality -= 5
                        
                    # Save to file
                    with open(out_path, "wb") as f:
                        f.write(buffer.getvalue())
                else:
                    if original_format == "JPEG":
                        processed_img.save(out_path, format=original_format, quality=95)
                    else:
                        processed_img.save(out_path, format=original_format)
                
                final_size = out_path.stat().st_size
                
                output_text = (
                    f"Successfully processed image.\n"
                    f"Original: {target_file.name} ({target_file.stat().st_size} bytes)\n"
                    f"Processed: {out_path.name} ({final_size} bytes)\n"
                    f"Action: {operation}"
                )
                
                return ToolInvocationResult(
                    invocation_id=request.invocation_id,
                    success=True,
                    output={
                        "text": output_text,
                        "attachment_paths": [str(out_path.resolve())],
                    },
                    error=None,
                    latency_ms=(time.monotonic() - t0) * 1000.0,
                )
                
        except Exception as e:
            return self._err(request, "IMAGE_PROCESS_ERROR", f"Failed to process image: {str(e)}", t0)

    def _err(self, request: ToolInvocationRequest, code: str, msg: str, t0: float) -> ToolInvocationResult:
        return ToolInvocationResult(
            invocation_id=request.invocation_id,
            success=False,
            output=None,
            error=AhjinError(
                code=code,
                message=msg,
                category=ErrorCategory.VALIDATION,
            ),
            latency_ms=(time.monotonic() - t0) * 1000.0,
        )
