"""Real loopback TLS/CONNECT failure fixtures verify retry safety."""
import asyncio
from datetime import datetime, timedelta, timezone
import ipaddress
import json
from pathlib import Path
import ssl
import uuid
import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from public_transport import SafeConnectTransport

ROOT = Path(__file__).resolve().parents[1] / "evidence"
ROOT.mkdir(exist_ok=True)
RUN = ROOT / ('retry-fixture-' + uuid.uuid4().hex[:8])
RUN.mkdir()
key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'CCM owned TLS fixture')])
cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
    .public_key(key.public_key()).serial_number(x509.random_serial_number())
    .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
    .not_valid_after(datetime.now(timezone.utc) + timedelta(hours=1))
    .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), False)
    .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
    .sign(key, hashes.SHA256()))
cert_path, key_path = RUN / 'fixture.crt', RUN / 'fixture.key'
cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
server_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
server_ssl.load_cert_chain(cert_path, key_path)
client_ssl = ssl.create_default_context(cafile=str(cert_path))

async def scenario(name, drops=0, response='ok', trusted=True):
    counts = {'connects': 0, 'target_posts': 0}
    events = []
    writers, tasks = set(), set()
    async def target(reader, writer):
        writers.add(writer)
        try:
            headers = await reader.readuntil(b'\r\n\r\n')
            size = next((int(line.split(b':', 1)[1]) for line in headers.split(b'\r\n')
                if line.lower().startswith(b'content-length:')), 0)
            await reader.readexactly(size)
            assert headers.startswith(b'POST ')
            counts['target_posts'] += 1
            if response == 'disconnect':
                return
            status = b'503 Service Unavailable' if response == '503' else b'200 OK'
            writer.write(b'HTTP/1.1 ' + status + b'\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK')
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
    server = await asyncio.start_server(target, '127.0.0.1', 0, ssl=server_ssl)
    target_port = server.sockets[0].getsockname()[1]
    async def copy(reader, writer):
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()
    async def proxy(reader, writer):
        writers.add(writer)
        upstream = None
        pumps = []
        try:
            headers = await reader.readuntil(b'\r\n\r\n')
            assert headers.startswith(b'CONNECT 127.0.0.1:')
            counts['connects'] += 1
            writer.write(b'HTTP/1.1 200 Connection established\r\n\r\n')
            await writer.drain()
            if counts['connects'] <= drops:
                return
            incoming, upstream = await asyncio.open_connection('127.0.0.1', target_port)
            writers.add(upstream)
            pumps = [asyncio.create_task(copy(reader, upstream)), asyncio.create_task(copy(incoming, writer))]
            tasks.update(pumps)
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            for task in pumps:
                task.cancel()
            if pumps:
                await asyncio.gather(*pumps, return_exceptions=True)
            writer.close()
            if upstream:
                upstream.close()
    proxy_server = await asyncio.start_server(proxy, '127.0.0.1', 0)
    proxy_port = proxy_server.sockets[0].getsockname()[1]
    result = {'name': name}
    try:
        transport = SafeConnectTransport(proxy=f'http://127.0.0.1:{proxy_port}',
            verify=client_ssl if trusted else ssl.create_default_context(), events=events)
        async with httpx.AsyncClient(transport=transport, trust_env=False, timeout=5) as client:
            try:
                r = await client.post(f'https://127.0.0.1:{target_port}/fixture', json={'operation': 'increment_once'})
                result['http_status'] = r.status_code
            except httpx.TransportError as exc:
                result['error_type'] = type(exc).__name__
        result.update(counts)
        result['retries'] = sum(bool(a.get('retried')) for r in events for a in r['attempts'])
        if name == 'tls_drop_then_success':
            result['pass'] = result.get('http_status') == 200 and counts == {'connects': 2, 'target_posts': 1} and result['retries'] == 1
        elif name == 'response_lost_after_post':
            result['pass'] = result.get('error_type') in ('RemoteProtocolError', 'ReadError') and counts['target_posts'] == 1 and counts['connects'] == 1 and result['retries'] == 0
        elif name == 'http_503_not_retried':
            result['pass'] = result.get('http_status') == 503 and counts['target_posts'] == 1 and result['retries'] == 0
        elif name == 'certificate_failure_not_retried':
            result['pass'] = result.get('error_type') == 'ConnectError' and counts['target_posts'] == 0 and counts['connects'] == 1 and result['retries'] == 0
        else:
            result['pass'] = result.get('error_type') == 'ConnectError' and counts == {'connects': 4, 'target_posts': 0} and result['retries'] == 3
        (RUN / (name + '.json')).write_text(json.dumps({'result': result, 'requests': events}, indent=2), 'utf-8')
        print(json.dumps(result), flush=True)
        return result
    finally:
        proxy_server.close()
        server.close()
        await proxy_server.wait_closed()
        await server.wait_closed()
        for writer in writers:
            writer.close()
        await asyncio.gather(*(w.wait_closed() for w in writers), return_exceptions=True)
        await asyncio.gather(*tasks, return_exceptions=True)

async def main():
    rows = []
    try:
        for args in [('tls_drop_then_success', 1), ('response_lost_after_post', 0, 'disconnect'),
            ('http_503_not_retried', 0, '503'), ('certificate_failure_not_retried', 0, 'ok', False),
            ('retry_limit_enforced', 99)]:
            rows.append(await scenario(*args))
        summary = {'pass': all(r['pass'] for r in rows), 'tests': rows, 'run': str(RUN)}
        (RUN / 'result.json').write_text(json.dumps(summary, indent=2), 'utf-8')
        return 0 if summary['pass'] else 1
    finally:
        cert_path.unlink()
        key_path.unlink()

if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
