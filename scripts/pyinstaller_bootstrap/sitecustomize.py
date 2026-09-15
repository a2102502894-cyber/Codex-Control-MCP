"""Build-only Python startup workaround for an unhealthy Windows WMI provider."""
import platform
import sys

if sys.platform.startswith("win") and hasattr(platform, "_wmi"):
    platform._wmi = None