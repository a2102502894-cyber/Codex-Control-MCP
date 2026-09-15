"""Real Windows inherited-pipe regression with a controlled protocol fixture."""
import os
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from codex_control_mcp import rpc as rpc_module


@pytest.mark.skipif(os.name != 'nt', reason='Windows job and pipe semantics')
def test_close_stops_owned_descendant_and_releases_inherited_pipes(tmp_path, monkeypatch):
    import win32api
    import win32event
    import win32process

    fixture = tmp_path / 'protocol_fixture.py'
    child_record = tmp_path / 'child.txt'
    fixture.write_text('''import json, pathlib, subprocess, sys
request = json.loads(sys.stdin.readline())
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                         stdout=sys.stdout, stderr=sys.stderr)
pathlib.Path(sys.argv[1]).write_text(str(child.pid))
print(json.dumps({'id': request['id'], 'result': {}}), flush=True)
for line in sys.stdin:
    pass
''', 'utf-8')
    real_popen = subprocess.Popen

    def launch_owned_fixture(argv, **kwargs):
        return real_popen([sys.executable, str(fixture), str(child_record)], **kwargs)

    monkeypatch.setattr(rpc_module, 'subprocess', SimpleNamespace(
        Popen=launch_owned_fixture, PIPE=subprocess.PIPE, TimeoutExpired=subprocess.TimeoutExpired))
    audit = SimpleNamespace(emit=lambda *args, **kwargs: None)
    schema = SimpleNamespace(validate=lambda *args: None, validate_response=lambda *args: None)
    app = rpc_module.AppServer(SimpleNamespace(codex_path=sys.executable, cli_version='fixture'),
                              schema, SimpleNamespace(cwd=str(tmp_path), experimental=True),
                              {key: os.environ[key] for key in ('SystemRoot', 'WINDIR', 'TEMP', 'TMP')
                               if key in os.environ}, audit)
    child = win32api.OpenProcess(0x100000 | 0x1000 | 0x0001, False, int(child_record.read_text()))
    finished = threading.Event()
    failures = []

    def close():
        try:
            app.close()
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()

    closer = threading.Thread(target=close, daemon=True)
    closer.start()
    try:
        assert finished.wait(10), 'Closing the server hung on a descendant-held pipe'
        assert not failures
        assert win32event.WaitForSingleObject(child, 1000) == win32event.WAIT_OBJECT_0
        assert not app.read_thread.is_alive() and not app.error_thread.is_alive()
        assert app.proc.poll() == 0
    finally:
        if win32event.WaitForSingleObject(child, 0) != win32event.WAIT_OBJECT_0:
            win32process.TerminateProcess(child, 1)
        win32api.CloseHandle(child)
        if app.proc.poll() is None:
            app.proc.terminate()
            app.proc.wait(4)
        closer.join(3)
