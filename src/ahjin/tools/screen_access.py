"""ScreenAccessTool ΓÇö Remote screen viewing and control."""

import subprocess
import time
import sys
import threading
import re
from typing import Any

from ahjin.core.errors import AhjinError, ErrorCategory
from ahjin.tools.base import BaseTool, ToolInvocationRequest, ToolInvocationResult

_SERVER_PROCESS = None
_TUNNEL_PROCESS = None
_PUBLIC_URL = None

import socket

def _wait_for_server(host="127.0.0.1", port=8000, timeout=10):
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False

def _start_server():
    """Starts the FastAPI screen server as a background process."""
    global _SERVER_PROCESS
    if _SERVER_PROCESS is None or _SERVER_PROCESS.poll() is not None:
        cmd = [sys.executable, "-m", "uvicorn", "ahjin.web.screen_server:app", "--host", "127.0.0.1", "--port", "8000"]
        _SERVER_PROCESS = subprocess.Popen(
            cmd, 
            stdout=subprocess.DEVNULL, 
            stderr=subprocess.DEVNULL
        )
        
    if not _wait_for_server():
        raise RuntimeError("Screen server failed to start on 127.0.0.1:8000")

def _consume_stdout(process):
    global _PUBLIC_URL
    for line in process.stdout:
        print(f"[serveo.net] {line.strip()}")
        match = re.search(r'Forwarding HTTP traffic from (https://[a-zA-Z0-9.-]+)', line)
        if match:
            _PUBLIC_URL = match.group(1).replace("https://", "http://")

def _start_tunnel():
    """Starts SSH tunnel using serveo.net to expose port 8000."""
    global _TUNNEL_PROCESS
    global _PUBLIC_URL
    
    if _TUNNEL_PROCESS is None or _TUNNEL_PROCESS.poll() is not None:
        _PUBLIC_URL = None
        cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-R", "80:127.0.0.1:8000", "serveo.net"]
        # Use stdout=PIPE to read the URL, stderr=STDOUT to combine them
        _TUNNEL_PROCESS = subprocess.Popen(
            cmd, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.STDOUT, 
            text=True, 
            stdin=subprocess.DEVNULL,
            bufsize=1 # Line buffered
        )
        
        # Start a thread to read stdout without blocking
        t = threading.Thread(target=_consume_stdout, args=(_TUNNEL_PROCESS,), daemon=True)
        t.start()
        
        # Wait up to 10 seconds for URL
        timeout = 10
        start_wait = time.time()
        while _PUBLIC_URL is None and (time.time() - start_wait) < timeout:
            time.sleep(0.5)
            
        if _PUBLIC_URL is None:
            raise Exception("Failed to get tunnel URL within timeout")
            
    return _PUBLIC_URL

class ScreenAccessTool(BaseTool):
    """Tool for granting the user remote access to the screen."""

    @property
    def tool_name(self) -> str:
        return "screen_access"

    async def execute(self, request: ToolInvocationRequest) -> ToolInvocationResult:
        t0 = time.monotonic()
        
        try:
            # 1. Start the local server
            _start_server()
            
            # 2. Expose via SSH tunnel to localhost.run
            public_url = _start_tunnel()
            
            output_text = (
                f"Screen access enabled successfully.\n\n"
                f"URL: {public_url}\n\n"
                f"Open the link on your mobile device to view and interact with the screen. "
                f"Tap to click, or toggle the keyboard to type."
            )
            output = {
                "text": output_text,
            }

            return ToolInvocationResult(
                invocation_id=request.invocation_id,
                success=True,
                output=output,
                latency_ms=(time.monotonic() - t0) * 1000.0,
            )
            
        except Exception as e:
            return ToolInvocationResult(
                invocation_id=request.invocation_id,
                success=False,
                output=None,
                error=AhjinError(
                    code="SCREEN_ACCESS_FAILED",
                    message=f"Failed to establish screen access via localhost.run: {str(e)}",
                    category=ErrorCategory.INTERNAL,
                ),
                latency_ms=(time.monotonic() - t0) * 1000.0,
            )
