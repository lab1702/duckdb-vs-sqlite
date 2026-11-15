"""Best-effort description of the machine and software the benchmark ran on."""

from __future__ import annotations

import os
import platform
import sqlite3
import subprocess
import sys
from importlib import metadata

import psutil


def _cpu_name() -> str:
    system = platform.system()
    try:
        if system == "Linux":
            with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.lower().startswith(("model name", "hardware", "cpu model")):
                        return line.split(":", 1)[1].strip()
        elif system == "Darwin":
            out = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            if out.stdout.strip():
                return out.stdout.strip()
        elif system == "Windows":
            import winreg  # type: ignore[import-not-found]

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
            ) as key:
                return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
    except Exception:
        pass
    return platform.processor() or platform.machine() or "unknown"


def _os_name() -> str:
    system = platform.system()
    if system == "Darwin":
        return f"macOS {platform.mac_ver()[0]}".strip()
    if system == "Windows":
        return f"Windows {platform.release()} ({platform.version()})"
    if system == "Linux":
        try:
            info = platform.freedesktop_os_release()
            return f"{info.get('PRETTY_NAME', 'Linux')} (kernel {platform.release()})"
        except Exception:
            return f"Linux {platform.release()}"
    return platform.platform()


def _version(package: str) -> str:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return "unknown"


def collect() -> dict:
    return {
        "os": _os_name(),
        "os_family": platform.system() or "unknown",
        "machine": platform.machine(),
        "cpu": _cpu_name(),
        "cores_physical": psutil.cpu_count(logical=False),
        "cores_logical": psutil.cpu_count(logical=True) or os.cpu_count(),
        "ram_gb": round(psutil.virtual_memory().total / 1024**3, 1),
        "python": f"{platform.python_implementation()} {platform.python_version()}",
        "python_executable": sys.executable,
        "sqlite": sqlite3.sqlite_version,
        "duckdb": _version("duckdb"),
        "pyarrow": _version("pyarrow"),
    }
