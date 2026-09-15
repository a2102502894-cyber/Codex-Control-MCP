"""Evidence-bearing inherited proxy probe; no inference or credential logging."""
from __future__ import annotations
import ipaddress
import sys

# Executed by the official command/exec child, not in the bridge process.
# CONNECT succeeds before HTTPSConnection wraps the socket with the default
# certificate-validating TLS context. Never fall back to a direct connection.
PROBE_CODE = r'''
import http.client,json,os,ssl,time
from urllib.parse import urlsplit
raw=os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')
if not raw: raise RuntimeError('HTTPS_PROXY not inherited')
p=urlsplit(raw)
if p.scheme!='http' or not p.hostname or not p.port or p.username or p.password:
    raise RuntimeError('Probe requires a credential-free HTTP CONNECT proxy')
c=http.client.HTTPSConnection(p.hostname,p.port,timeout=10,context=ssl.create_default_context())
c.set_tunnel('www.microsoft.com',443)
start=time.monotonic()
try:
    c.connect()
    peer=c.sock.getpeername()
    tls=c.sock.version()
    c.request('HEAD','/',headers={'Host':'www.microsoft.com','User-Agent':'Codex-Control-MCP health proxy probe'})
    r=c.getresponse()
    print(json.dumps({'method':'HEAD','target':'https://www.microsoft.com/','http_status':r.status,
        'connect_verified':True,'tls_certificate_verified':True,'tls_version':tls,
        'socket_peer_host':peer[0],'socket_peer_port':peer[1],
        'proxy_host_from_environment':p.hostname,'proxy_port_from_environment':p.port,
        'elapsed_ms':round((time.monotonic()-start)*1000)}))
finally:
    c.close()
'''


def probe_argv():
    # A frozen binary is not a Python interpreter. Source deployment is the
    # supported production mode; do not accidentally invoke its CLI with -c.
    if getattr(sys, 'frozen', False):
        raise RuntimeError('Proxy probe requires source Python deployment')
    return [sys.executable, '-c', PROBE_CODE]


def route_is_verified(data):
    if not isinstance(data, dict):
        return False
    host = data.get('proxy_host_from_environment')
    peer = data.get('socket_peer_host')
    same_host = host == peer and bool(host)
    if host == 'localhost':
        try:
            same_host = ipaddress.ip_address(peer).is_loopback
        except (ValueError, TypeError):
            same_host = False
    return bool(
        data.get('connect_verified') is True
        and data.get('tls_certificate_verified') is True
        and data.get('tls_version') in {'TLSv1.2', 'TLSv1.3'}
        and same_host
        and type(data.get('proxy_port_from_environment')) is int
        and data['proxy_port_from_environment'] == data.get('socket_peer_port')
        and type(data.get('http_status')) is int
        and 200 <= data['http_status'] < 500
    )
