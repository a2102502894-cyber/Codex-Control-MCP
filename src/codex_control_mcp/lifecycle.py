"""Current Windows logon-session stop signaling, with the creator's default DACL.
A stop event never executes commands and never changes an account's privileges.
"""

from __future__ import annotations
import ctypes
from ctypes import wintypes
import os
import re
from .errors import BridgeError


def _api():
    if os.name != "nt":
        raise BridgeError(
            "capability_unavailable",
            "Windows stop events are unavailable on this platform",
        )
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateEventW.argtypes = [
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    api.CreateEventW.restype = wintypes.HANDLE
    api.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    api.OpenEventW.restype = wintypes.HANDLE
    api.SetEvent.argtypes = [wintypes.HANDLE]
    api.SetEvent.restype = wintypes.BOOL
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.WaitForSingleObject.restype = wintypes.DWORD
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    return api


def event_name(instance_id):
    if not isinstance(instance_id, str) or not re.fullmatch(
        r"[0-9a-f]{32}", instance_id
    ):
        raise BridgeError(
            "invalid_arguments", "Invalid owned service instance identifier"
        )
    return "Local\\CodexControlStop-" + instance_id


class StopEvent:
    def __init__(self, instance_id):
        self.api = _api()
        self.name = event_name(instance_id)
        ctypes.set_last_error(0)
        self.handle = self.api.CreateEventW(None, True, False, self.name)
        if not self.handle:
            raise BridgeError(
                "permission_denied", "Could not create the owned lifecycle event"
            )
        if ctypes.get_last_error() == 183:
            self.close()
            raise BridgeError(
                "instance_already_running", "Lifecycle identifier already exists"
            )

    def requested(self):
        code = self.api.WaitForSingleObject(self.handle, 0)
        if code == 0:
            return True
        if code == 258:
            return False
        raise BridgeError(
            "execution_state_unknown", "Lifecycle event became unavailable"
        )

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def request_stop(instance_id):
    api = _api()
    handle = api.OpenEventW(0x0002, False, event_name(instance_id))
    if not handle:
        raise BridgeError(
            "capability_unavailable",
            "Owned stop event is absent or belongs to another Windows logon session",
        )
    try:
        if not api.SetEvent(handle):
            raise BridgeError("permission_denied", "Windows refused the stop signal")
    finally:
        api.CloseHandle(handle)


def create_owned_process_job(process_handle):
    """Attach only the supplied owned process to a kill-on-close Windows job.
    It changes process lifetime only, not filesystem, network, or user privileges.
    """
    import uuid
    import win32api
    import win32job

    job = win32job.CreateJobObject(None, "Local\\CodexControlJob-" + uuid.uuid4().hex)
    try:
        limits = win32job.QueryInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation
        )
        limits["BasicLimitInformation"]["LimitFlags"] |= (
            win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        win32job.SetInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation, limits
        )
        win32job.AssignProcessToJobObject(job, int(process_handle))
        return job
    except BaseException:
        win32api.CloseHandle(job)
        raise


def process_alive(pid):
    """Return True/False only when Windows proves it; None means unknown.
    A historical PID alone is never sufficient authority to terminate a process.
    """
    if os.name != "nt" or type(pid) is not int or pid <= 0:
        return None
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    api.GetExitCodeProcess.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    handle = api.OpenProcess(0x1000, False, pid)
    if not handle:
        return False if ctypes.get_last_error() == 87 else None
    try:
        code = wintypes.DWORD()
        if not api.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        return code.value == 259
    finally:
        api.CloseHandle(handle)
