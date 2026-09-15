"""User-approved Tabbit backend. Playwright is a library, never a second agent."""
from __future__ import annotations
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
from urllib.parse import urlsplit
from .common import CREATE_NO_WINDOW, InstanceLock, atomic_json
from .config import build_environment
from .errors import BridgeError
from .lifecycle import create_owned_process_job


class TabbitBrowser:
    backend = 'playwright.chromium -> installed Tabbit'

    def __init__(self, cfg):
        self.cfg, self.closed = cfg, False
        self.verified, self.verified_operations = False, set()
        self.lock = threading.RLock()
        self.responses = queue.Queue()
        self.sequence = 0
        self.proc = self.job = self.profile_lock = None
        self.read_thread = self.error_thread = None
        settings = cfg.browser
        candidates = [Path(settings['executable_path'])] if settings.get('executable_path') else [
            Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Tabbit/Application/Tabbit Browser.exe',
            Path(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')) / 'Tabbit/Application/Tabbit Browser.exe',
            Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData/Local'))) / 'Tabbit/Application/Tabbit Browser.exe',
        ]
        executable = next((p.resolve() for p in candidates if p.is_file()), None)
        if executable is None:
            raise BridgeError('runtime_missing', 'Tabbit is not installed at the configured or supported locations.')
        try:
            import playwright
        except ImportError as exc:
            raise BridgeError('runtime_missing', 'The Tabbit backend requires the installed Playwright dependency.') from exc
        driver = Path(playwright.__file__).parent / 'driver'
        worker = Path(__file__).with_name('tabbit_worker.cjs')
        if not (driver / 'node.exe').is_file() or not worker.is_file():
            raise BridgeError('runtime_missing', 'The installed Playwright driver is incomplete.')
        profile = Path(settings.get('profile_directory') or cfg.home / 'browser/tabbit-profile').expanduser().resolve()
        ordinary = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData/Local'))) / 'Tabbit/User Data'
        if profile.is_relative_to(ordinary.resolve()):
            raise BridgeError('invalid_config', 'Use a dedicated profile; the normal Tabbit profile cannot be attached.')
        profile.mkdir(parents=True, exist_ok=True)
        marker = profile / '.codex-control-profile.json'
        identity = {'owner': 'Codex-Control-MCP', 'home': os.path.normcase(str(cfg.home.resolve())), 'browser': 'tabbit'}
        if not marker.exists():
            if any(profile.iterdir()):
                raise BridgeError('invalid_config', 'The browser profile directory is nonempty and is not owned by this project.')
            try:
                with marker.open('x', encoding='utf-8') as stream:
                    json.dump(identity, stream)
            except FileExistsError:
                pass
        if json.loads(marker.read_text('utf-8')) != identity:
            raise BridgeError('invalid_config', 'The dedicated browser profile belongs to another bridge home.')
        self.info = {'backend': self.backend, 'browser_id': 'tabbit', 'executable_path': str(executable),
            'profile_directory': str(profile), 'headless': settings.get('headless', True),
            'official_browser_backend': False, 'model_turns_requested': False, 'transport': 'playwright_pipe'}
        try:
            self.profile_lock = InstanceLock(profile / '.codex-control.lock')
            self.profile_lock.acquire()
            env, proxy_info = build_environment(cfg)
            env.pop('DEBUG', None)
            env.pop('PWDEBUG', None)
            self.proc = subprocess.Popen([str(driver / 'node.exe'), str(worker), str(driver / 'package')],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                cwd=str(cfg.home), creationflags=CREATE_NO_WINDOW)
            self.job = create_owned_process_job(self.proc._handle)
            self.read_thread = threading.Thread(target=self._read, daemon=True, name='tabbit-responses')
            self.error_thread = threading.Thread(target=self._drain, daemon=True, name='tabbit-stderr')
            self.read_thread.start(); self.error_thread.start()
            params = {'executable_path': str(executable), 'profile_directory': str(profile),
                'headless': settings.get('headless', True), 'timeout_ms': settings.get('timeout_ms', 20000),
                'max_pages': settings.get('max_pages', 8)}
            proxy = env.get('HTTPS_PROXY') or env.get('HTTP_PROXY')
            if proxy:
                parsed = urlsplit(proxy)
                if parsed.scheme in ('http', 'https', 'socks5') and parsed.hostname and parsed.port:
                    host = '[' + parsed.hostname + ']' if ':' in parsed.hostname else parsed.hostname
                    params['proxy'] = {'server': f'{parsed.scheme}://{host}:{parsed.port}', 'bypass': 'localhost,127.0.0.1,[::1]'}
                    if parsed.username:
                        from urllib.parse import unquote
                        params['proxy'].update(username=unquote(parsed.username), password=unquote(parsed.password or ''))
            self.info.update(self._request('configure', params))
            self.info['proxy_source'] = proxy_info['source']
            self.info['worker_pid'] = self.proc.pid
        except BaseException:
            self.close()
            raise

    def _read(self):
        try:
            while raw := self.proc.stdout.readline(4 * 1024 * 1024 + 1):
                if len(raw) > 4 * 1024 * 1024:
                    break
                self.responses.put(json.loads(raw))
        except (OSError, ValueError):
            pass
        finally:
            self.responses.put(None)

    def _drain(self):
        try:
            while self.proc.stderr.readline(65536):
                pass  # Do not retain browser diagnostics containing site data.
        except (OSError, ValueError):
            pass

    def _request(self, tool, args):
        with self.lock:
            if self.closed or self.proc.poll() is not None:
                raise BridgeError('capability_unavailable', 'The owned Tabbit worker has stopped.')
            self.sequence += 1
            try:
                self.proc.stdin.write((json.dumps({'id': self.sequence, 'tool': tool, 'args': args}, ensure_ascii=True) + '\n').encode())
                self.proc.stdin.flush()
                response = self.responses.get(timeout=60)
                if response is None or response.get('id') != self.sequence:
                    raise OSError('Tabbit response stream closed')
            except (OSError, ValueError, queue.Empty) as exc:
                self.close(force=True)
                raise BridgeError('execution_state_unknown', 'The Tabbit connection was lost or timed out; the operation is not replayed.') from exc
            if response.get('error'):
                raise BridgeError(response['error']['code'], response['error']['message'])
            return response['result']

    def call(self, tool, args, forwarder=None):
        if tool not in {'browser_start','browser_snapshot','browser_click','browser_fill','browser_press','browser_scroll','browser_navigate','browser_close'}:
            raise BridgeError('capability_unavailable', 'Unknown Browser tool.')
        if tool in {'browser_click', 'browser_scroll'}:
            indexed = type(args.get('element_index')) is int
            coordinates = type(args.get('x')) in (int, float) and type(args.get('y')) in (int, float)
            if indexed == coordinates:
                raise BridgeError('invalid_arguments', 'Supply one observed element index or one coordinate pair.')
        out = self._request(tool, args)
        self.verified_operations.update(out.get('verified_operations', []))
        self.verified = bool(out.get('full_action_chain_verified', self.verified))
        if out.get('browser_version'):
            self.info['browser_version'] = out['browser_version']
        out.update(backend=self.backend, runtime=self.info.copy())
        return out

    def close(self, force=False):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            if self.proc:
                try:
                    if not force and self.proc.poll() is None:
                        self.proc.stdin.write((json.dumps({'id': 0, 'tool': 'shutdown'})+'\n').encode())
                        self.proc.stdin.flush()
                        self.proc.wait(5)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass
                finally:
                    if self.job:
                        self.job.Close(); self.job = None
                    if self.proc.poll() is None:
                        self.proc.terminate(); self.proc.wait(5)
                    self.proc.stdin.close()
                    for thread, stream in ((self.read_thread,self.proc.stdout),(self.error_thread,self.proc.stderr)):
                        if thread:
                            thread.join(1)
                        if not thread or not thread.is_alive():
                            stream.close()
            if self.profile_lock:
                self.profile_lock.close()
                self.profile_lock = None
