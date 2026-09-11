"""SystemInfoTool — Read-only baseline tool returning safe environment info with field selection."""

import ctypes
import os
import platform
import shutil
import sys
import time
from typing import Any, ClassVar, cast

from ahjin.tools.base import BaseTool, ToolInvocationRequest, ToolInvocationResult

# Strict security boundary: only whitelisted safe fields can be retrieved.
SAFE_FIELDS_WHITELIST: frozenset[str] = frozenset({
    "os",
    "python",
    "machine",
    "platform",
    "cwd",
    "cpu",
    "memory",
    "gpu",
    "storage",
    "all_safe",
})


def _get_cpu_info() -> tuple[str, str, str]:
    """Retrieve (processor_model, core_count, architecture) safely."""
    cores = str(os.cpu_count() or "Unknown")
    arch = platform.machine()
    model = "Unknown"

    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            ) as key:
                val, _ = winreg.QueryValueEx(key, "ProcessorNameString")
                if isinstance(val, str) and val.strip():
                    model = val.strip()
        except Exception:
            pass
    elif sys.platform.startswith("linux"):
        try:
            with open("/proc/cpuinfo", "r", encoding="utf-8") as f:
                for line in f:
                    if "model name" in line:
                        m = line.split(":", 1)[1].strip()
                        if m:
                            model = m
                            break
        except Exception:
            pass
    elif sys.platform == "darwin":
        try:
            import subprocess

            out = subprocess.check_output(
                ["sysctl", "-n", "machdep.cpu.brand_string"], text=True
            ).strip()
            if out:
                model = out
        except Exception:
            pass

    if model == "Unknown":
        p = platform.processor()
        if p and p.strip():
            model = p.strip()

    return model, cores, arch


def _get_memory_info() -> str:
    """Retrieve physical RAM details safely with platform-specific probes."""
    if sys.platform == "win32":
        try:
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                total_gb = round(stat.ullTotalPhys / (1024**3), 1)
                avail_gb = round(stat.ullAvailPhys / (1024**3), 1)
                return f"{total_gb} GB Total ({avail_gb} GB Available, {stat.dwMemoryLoad}% used)"
        except Exception:
            pass
    elif sys.platform.startswith("linux"):
        try:
            mem_total = 0
            mem_avail = 0
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        mem_total = int(line.split()[1]) * 1024
                    elif line.startswith("MemAvailable:"):
                        mem_avail = int(line.split()[1]) * 1024
            if mem_total > 0:
                total_gb = round(mem_total / (1024**3), 1)
                avail_gb = round(mem_avail / (1024**3), 1)
                return f"{total_gb} GB Total ({avail_gb} GB Available)"
        except Exception:
            pass

    # POSIX sysconf fallback
    sysconf_func: Any = getattr(os, "sysconf", None)
    if callable(sysconf_func):
        try:
            page_size = int(str(sysconf_func("SC_PAGE_SIZE")))
            phys_pages = int(str(sysconf_func("SC_PHYS_PAGES")))
            mem_bytes = page_size * phys_pages
            if mem_bytes > 0:
                return f"{round(mem_bytes / (1024**3), 1)} GB Total"
        except Exception:
            pass

    return "Unavailable"


def _get_gpu_info() -> str:
    """Retrieve GPU information safely via standard platform facilities."""
    if sys.platform == "win32":
        try:
            import winreg

            gpus: list[str] = []
            guid = r"{4d36e968-e325-11ce-bfc1-08002be10318}"
            key_path = rf"SYSTEM\CurrentControlSet\Control\Class\{guid}"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as k:
                idx = 0
                while True:
                    try:
                        sub_name = winreg.EnumKey(k, idx)
                        idx += 1
                        if sub_name.isdigit():
                            with winreg.OpenKey(
                                winreg.HKEY_LOCAL_MACHINE, f"{key_path}\\{sub_name}"
                            ) as sk:
                                try:
                                    desc, _ = winreg.QueryValueEx(sk, "DriverDesc")
                                    if (
                                        isinstance(desc, str)
                                        and desc.strip()
                                        and desc.strip() not in gpus
                                    ):
                                        gpus.append(desc.strip())
                                except Exception:
                                    pass
                    except OSError:
                        break
            if gpus:
                return ", ".join(gpus)
        except Exception:
            pass

    return "Unavailable"


def _get_storage_info() -> str:
    """Retrieve system drive storage details via shutil."""
    drive = os.environ.get("SystemDrive", "C:") if sys.platform == "win32" else "/"
    if not drive.endswith(("\\", "/")):
        drive += "\\"
    try:
        usage = shutil.disk_usage(drive)
        total_gb = round(usage.total / (1024**3), 1)
        free_gb = round(usage.free / (1024**3), 1)
        used_gb = round(usage.used / (1024**3), 1)
        return f"{total_gb} GB Total, {free_gb} GB Free ({used_gb} GB Used on {drive})"
    except Exception:
        return "Unavailable"


class SystemInfoTool(BaseTool):
    """Deterministic read-only tool that provides safe system runtime info.

    Never exposes environment variables, API keys, credentials, or .env files.
    Enforces a strict whitelist on requested fields.
    """

    SUPPORTED_FIELDS: ClassVar[frozenset[str]] = SAFE_FIELDS_WHITELIST

    @property
    def tool_name(self) -> str:
        return "system_info"

    async def execute(self, request: ToolInvocationRequest) -> ToolInvocationResult:
        t0 = time.monotonic()
        try:
            raw_fields: Any = request.parameters.get("fields", ["all_safe"])
            field_list: list[Any] = (
                cast(list[Any], raw_fields) if isinstance(raw_fields, list) else ["all_safe"]
            )

            # Filter against security whitelist
            valid_fields: list[str] = [
                str(x) for x in field_list if isinstance(x, str) and x in SAFE_FIELDS_WHITELIST
            ]

            if "all_safe" in valid_fields or not valid_fields:
                active_fields: list[str] = [
                    "os",
                    "python",
                    "machine",
                    "platform",
                    "cwd",
                    "cpu",
                    "memory",
                    "gpu",
                    "storage",
                ]
            else:
                active_fields = valid_fields

            lines: list[str] = []
            for field in active_fields:
                if field == "os":
                    lines.append(f"OS: {platform.system()} ({platform.release()})")
                elif field == "python":
                    lines.append(f"Python: {sys.version.split()[0]}")
                elif field == "machine":
                    lines.append(f"Machine: {platform.machine()}")
                elif field == "platform":
                    lines.append(f"Platform: {sys.platform}")
                elif field == "cwd":
                    lines.append(f"CWD: {os.getcwd()}")
                elif field == "cpu":
                    model, cores, arch = _get_cpu_info()
                    lines.append(f"Processor: {model}")
                    lines.append(f"CPU Cores: {cores}")
                    lines.append(f"Architecture: {arch}")
                elif field == "memory":
                    lines.append(f"Memory: {_get_memory_info()}")
                elif field == "gpu":
                    lines.append(f"GPU: {_get_gpu_info()}")
                elif field == "storage":
                    lines.append(f"Storage: {_get_storage_info()}")

            if lines:
                output_text = "\n".join(lines)
            else:
                output_text = "No valid safe system info fields requested."

            latency_ms = (time.monotonic() - t0) * 1000.0
            return ToolInvocationResult(
                invocation_id=request.invocation_id,
                success=True,
                output=output_text,
                latency_ms=latency_ms,
            )
        except Exception as exc:
            latency_ms = (time.monotonic() - t0) * 1000.0
            return ToolInvocationResult(
                invocation_id=request.invocation_id,
                success=False,
                output=f"Failed to gather system info: {exc}",
                latency_ms=latency_ms,
            )

