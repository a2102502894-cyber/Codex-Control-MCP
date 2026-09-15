"""Real SQLite/Windows DPAPI transaction tests; isolated owner and SDK clients."""

import asyncio
import concurrent.futures
import secrets
import threading
import time
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit

import pytest
from codex_control_mcp.oauth import OwnerOAuth, OAuthStore, TokenError
from test_oauth import oauth_case, client, begin, consent, grant  # noqa: F401


def granted(case):
    cfg, provider, app, _ = case

    async def run():
        async with client(app) as http:
            registered, token, *_ = await grant(http, cfg, provider)
            sdk_client = await provider.get_client(registered["client_id"])
            refresh = await provider.load_refresh_token(
                sdk_client, token["refresh_token"]
            )
            return sdk_client, token, refresh

    return asyncio.run(run())


def fail_write(store, monkeypatch, kind):
    put = store.put

    def injected(k, *args, **kwargs):
        if k == kind:
            raise OSError("Injected storage failure")
        return put(k, *args, **kwargs)

    monkeypatch.setattr(store, "put", injected)


def test_issue_tokens_rolls_back_partial_pair(oauth_case, monkeypatch):
    _, provider, _, _ = oauth_case
    fail_write(provider.store, monkeypatch, "refresh")
    with pytest.raises(OSError):
        provider.issue_tokens("isolated", ["control", "offline_access"])
    assert provider.store.count("access") == provider.store.count("refresh") == 0


def test_failed_rotation_preserves_old_grant_and_allows_retry(oauth_case, monkeypatch):
    _, provider, _, _ = oauth_case
    sdk_client, token, refresh = granted(oauth_case)
    fail_write(provider.store, monkeypatch, "refresh")
    with pytest.raises(OSError):
        asyncio.run(
            provider.exchange_refresh_token(sdk_client, refresh, refresh.scopes)
        )
    assert provider.store.get("refresh", token["refresh_token"])
    assert provider.store.get("access", token["access_token"])
    assert provider.store.count("access") == 1
    assert provider.store.count("used_refresh") == 0
    monkeypatch.undo()
    new = asyncio.run(
        provider.exchange_refresh_token(sdk_client, refresh, refresh.scopes)
    )
    assert new.refresh_token != token["refresh_token"]


def test_failed_consent_keeps_pairing_and_pending_request(oauth_case, monkeypatch):
    cfg, provider, app, _ = oauth_case

    async def run():
        async with client(app) as http:
            _, pairing, _, _, initial = await begin(http, cfg, provider)
            fail_write(provider.store, monkeypatch, "code")
            with pytest.raises(OSError):
                await consent(http, initial, pairing)
            assert provider.store.get("pairing", pairing)
            assert provider.store.count("pending") == 1
            assert provider.store.get("enrollment", "active")
            monkeypatch.undo()
            accepted, _ = await consent(http, initial, pairing)
            assert accepted.status_code == 303

    asyncio.run(run())


def test_failed_code_exchange_keeps_authorization_code(oauth_case, monkeypatch):
    cfg, provider, app, _ = oauth_case

    async def run():
        async with client(app) as http:
            registered, pairing, _, _, initial = await begin(http, cfg, provider)
            accepted, _ = await consent(http, initial, pairing)
            code = parse_qs(urlsplit(accepted.headers["location"]).query)["code"][0]
            sdk_client = await provider.get_client(registered["client_id"])
            loaded = await provider.load_authorization_code(sdk_client, code)
            fail_write(provider.store, monkeypatch, "refresh")
            with pytest.raises(OSError):
                await provider.exchange_authorization_code(sdk_client, loaded)
            assert provider.store.get("code", code)
            assert provider.store.count("access") == 0
            monkeypatch.undo()
            assert (
                await provider.exchange_authorization_code(sdk_client, loaded)
            ).access_token

    asyncio.run(run())


def test_removed_client_cannot_exchange_preloaded_refresh(oauth_case):
    _, provider, _, _ = oauth_case
    sdk_client, _, refresh = granted(oauth_case)
    provider.store.remove_kinds("client")
    with pytest.raises(TokenError):
        asyncio.run(
            provider.exchange_refresh_token(sdk_client, refresh, refresh.scopes)
        )


def test_second_preloaded_refresh_revokes_new_descendants(oauth_case):
    _, provider, _, _ = oauth_case
    sdk_client, _, refresh = granted(oauth_case)
    new = asyncio.run(
        provider.exchange_refresh_token(sdk_client, refresh, refresh.scopes)
    )
    with pytest.raises(TokenError):
        asyncio.run(
            provider.exchange_refresh_token(sdk_client, refresh, refresh.scopes)
        )
    assert asyncio.run(provider.load_access_token(new.access_token)) is None
    assert provider.store.count("refresh") == 0


def test_revoke_rotated_refresh_revokes_current_family(oauth_case):
    cfg, provider, app, _ = oauth_case
    sdk_client, old, refresh = granted(oauth_case)
    new = asyncio.run(
        provider.exchange_refresh_token(sdk_client, refresh, refresh.scopes)
    )

    async def run():
        async with client(app) as http:
            result = await http.post(
                "/revoke",
                data={"client_id": sdk_client.client_id, "token": old["refresh_token"]},
            )
            assert result.status_code == 200
        assert await provider.load_access_token(new.access_token) is None

    asyncio.run(run())


def test_revocation_waits_for_rotation_then_removes_new_grants(oauth_case, monkeypatch):
    cfg, provider, _, _ = oauth_case
    sdk_client, old, refresh = granted(oauth_case)
    other = OwnerOAuth(cfg)
    writing, proceed, revoking = threading.Event(), threading.Event(), threading.Event()
    put = provider.store.put

    def paused(kind, *args, **kwargs):
        result = put(kind, *args, **kwargs)
        if kind == "access":
            writing.set()
            assert proceed.wait(5)
        return result

    monkeypatch.setattr(provider.store, "put", paused)

    def rotate():
        return asyncio.run(
            provider.exchange_refresh_token(sdk_client, refresh, refresh.scopes)
        )

    def revoke():
        revoking.set()
        return asyncio.run(other.revoke_token(refresh))

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(rotate)
            assert writing.wait(5)
            second = pool.submit(revoke)
            assert revoking.wait(5)
            proceed.set()
            new = first.result(10)
            second.result(10)
        assert asyncio.run(other.load_access_token(new.access_token)) is None
        assert other.store.count("refresh") == 0
    finally:
        proceed.set()
        other.close()


def test_nested_store_failure_rolls_back_outer_transaction(tmp_path):
    store = OAuthStore(tmp_path)
    try:
        with pytest.raises(RuntimeError):
            with store.transaction():
                store.put("test", "one", {"n": 1}, time.time() + 60)
                with store.transaction():
                    store.put("test", "two", {"n": 2}, time.time() + 60)
                raise RuntimeError("rollback")
        assert store.count("test") == 0
    finally:
        store.close()


@pytest.mark.parametrize("key", ["中文", "😀", "\ud800"])
def test_store_handles_opaque_unicode_keys_without_losing_records(tmp_path, key):
    store = OAuthStore(tmp_path)
    try:
        store.put("test", key, {"ok": True}, time.time() + 60)
        assert store.get("test", key, consume=True) == {"ok": True}
        assert store.get("test", key) is None
    finally:
        store.close()


def test_six_python_processes_have_one_authorization_code_consumer(tmp_path):
    key = secrets.token_urlsafe(32)
    store = OAuthStore(tmp_path)
    store.put("code", key, {"isolated": True}, time.time() + 90)
    store.close()
    program = """from pathlib import Path
import sys,time
from codex_control_mcp.oauth import OAuthStore
home=Path(sys.argv[1]); key=sys.argv[2]; ident=sys.argv[3]
store=OAuthStore(home)
(home/(ident+'.ready')).write_text('ready')
deadline=time.monotonic()+15
while not (home/'go').exists():
    if time.monotonic()>deadline: raise RuntimeError('Test barrier timeout')
    time.sleep(.01)
try: print(int(store.get('code',key,consume=True) is not None))
finally: store.close()
"""
    children = []
    try:
        for i in range(6):
            children.append(
                subprocess.Popen(
                    [sys.executable, "-c", program, str(tmp_path), key, str(i)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            )
        deadline = time.monotonic() + 15
        while len(list(tmp_path.glob("*.ready"))) < 6:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        (tmp_path / "go").write_text("go")
        results = [p.communicate(timeout=20) for p in children]
        assert all(p.returncode == 0 for p in children), [
            err.decode("utf-8", "replace") for _, err in results
        ]
        assert sum(int(out.strip()) for out, _ in results) == 1
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
                child.wait(5)
