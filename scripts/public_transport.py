"""Acceptance client transport: bounded recovery before target HTTP is sent.

httpcore 1.0.9 does not wrap proxy.start_tls in its ordinary connection retry
loop. This wrapper covers that phase without retrying application traffic.
It is a client utility, not an alteration of ChatGPT's HTTP client or Clash.
"""
from __future__ import annotations
import asyncio
import ssl
import time
import httpx

BASE_TRANSPORT = httpx.AsyncHTTPTransport

def certificate_error(exc):
    pending, seen = [exc], set()
    while pending:
        item = pending.pop()
        if item is None or id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, ssl.SSLCertVerificationError):
            return True
        pending.extend((item.__cause__, item.__context__))
    return False

class SafeConnectTransport(httpx.AsyncBaseTransport):
    def __init__(self, *, connect_retries=3, events=None, **kwargs):
        if type(connect_retries) is not int or not 0 <= connect_retries <= 3:
            raise ValueError('connect_retries must be between 0 and 3')
        self.connect_retries = connect_retries
        self.events = events if events is not None else []
        kwargs['retries'] = 0
        kwargs.setdefault('limits', httpx.Limits(max_keepalive_connections=10, keepalive_expiry=5))
        self.inner = BASE_TRANSPORT(**kwargs)

    async def handle_async_request(self, request):
        original_trace = request.extensions.get('trace')
        row = {'method': request.method, 'path': request.url.path, 'attempts': []}
        self.events.append(row)
        try:
            for attempt in range(self.connect_retries + 1):
                started = time.monotonic()
                state = {'attempt': attempt + 1, 'target_headers_started': False,
                         'connect_phase_failed': False, 'events': []}
                row['attempts'].append(state)
                async def trace(name, info):
                    event = {'event': name, 'ms': round((time.monotonic()-started)*1000, 2)}
                    if name.endswith('send_request_headers.started'):
                        core_request = info.get('request')
                        if getattr(core_request, 'method', None) != b'CONNECT':
                            # Unknown header events are also treated as sent.
                            state['target_headers_started'] = True
                    if name in ('connection.connect_tcp.failed', 'connection.start_tls.failed',
                                'proxy.start_tls.failed'):
                        state['connect_phase_failed'] = True
                        event['error_type'] = type(info.get('exception')).__name__
                    state['events'].append(event)
                    if original_trace is not None:
                        await original_trace(name, info)
                request.extensions['trace'] = trace
                try:
                    response = await self.inner.handle_async_request(request)
                    state['http_status'] = response.status_code
                    return response
                except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                    state['error_type'] = type(exc).__name__
                    state['certificate_error'] = certificate_error(exc)
                    allowed = (attempt < self.connect_retries and state['connect_phase_failed']
                        and not state['target_headers_started'] and not state['certificate_error'])
                    state['retried'] = allowed
                    if not allowed:
                        raise
                    await asyncio.sleep(min(0.5 * 2 ** attempt, 2))
                except BaseException as exc:
                    state['error_type'] = type(exc).__name__
                    state['retried'] = False
                    raise
        finally:
            if original_trace is None:
                request.extensions.pop('trace', None)
            else:
                request.extensions['trace'] = original_trace

    async def aclose(self):
        await self.inner.aclose()
