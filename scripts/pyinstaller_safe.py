"""Launch PyInstaller while bypassing a broken Windows WMI query provider.

Python 3.13's platform module prefers WMI for Windows version and CPU probes.
If WMI is unhealthy, importing PyInstaller can hang before build logging starts.
Setting platform._wmi to None activates platform.py's own documented fallbacks
(sys.getwindowsversion and PROCESSOR_* environment variables) without changing
system services or the packaged application.
"""
from __future__ import annotations
import platform
import runpy
import sys

if sys.platform.startswith("win") and hasattr(platform, "_wmi"):
    platform._wmi = None

runpy.run_module("PyInstaller", run_name="__main__", alter_sys=True)