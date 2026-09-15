"""Disable Python 3.13 WMI probes before the packaged application imports."""
import platform
import sys

if sys.platform.startswith("win") and hasattr(platform, "_wmi"):
    platform._wmi = None