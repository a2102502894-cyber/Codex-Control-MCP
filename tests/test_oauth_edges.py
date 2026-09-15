"""Additional authorization boundary tests; no clipboard, browser or live account."""
import asyncio
import concurrent.futures
import json
import secrets
import time
from urllib.parse import urlencode

import pytest
from codex_control_mcp.oauth import OAuthStore
from test_oauth import oauth_case,client,begin,consent,grant,ISSUER  # noqa: F401


def test_non_ascii_csrf_is_rejected_not_internal_error(oauth_case):
    cfg,provider,app,owner=oauth_case
    async def run():
        async with client(app) as c:
            _,pairing,_,_,initial=await begin(c,cfg,provider)
            response,_=await consent(c,initial,pairing,csrf='中文错误输入')
            assert response.status_code==403
            assert provider.store.count('access')==0
    asyncio.run(run())


def test_bearer_authentication_scheme_is_case_insensitive(oauth_case):
    _,_,app,owner=oauth_case
    async def run():
        async with client(app) as c:
            for scheme in ('Bearer','bearer','BEARER'):
                result=await c.get('/mcp',headers={'Authorization':scheme+' '+owner})
                assert result.status_code==200
    asyncio.run(run())


def test_duplicate_resource_field_is_rejected_before_grant_consumption(oauth_case):
    cfg,provider,app,_=oauth_case
    async def run():
        async with client(app) as c:
            _,token,request,*_=await grant(c,cfg,provider)
            before=provider.store.count('access')
            body=urlencode([*request.items(),('resource',provider.resource)])
            response=await c.post('/token',content=body,headers={'Content-Type':'application/x-www-form-urlencoded'})
            assert response.status_code==400
            assert response.json()['error']=='invalid_request'
            assert provider.store.count('access')==before
    asyncio.run(run())


def test_conflicting_query_parameters_are_rejected(oauth_case):
    _,_,app,_=oauth_case
    async def run():
        async with client(app) as c:
            response=await c.get('/authorize?client_id=one&client_id=two')
            assert response.status_code==400 and response.json()['error']=='invalid_request'
    asyncio.run(run())


def test_duplicate_json_registration_fields_are_rejected(oauth_case):
    cfg,provider,app,_=oauth_case
    from codex_control_mcp.oauth import create_pairing_secret
    create_pairing_secret(cfg.home)
    async def run():
        async with client(app) as c:
            body='{"redirect_uris":["https://chatgpt.com/connector_platform_oauth_redirect"],"grant_types":["authorization_code","refresh_token"],"client_name":"one","client_name":"two"}'
            response=await c.post('/register',content=body,headers={'Content-Type':'application/json'})
            assert response.status_code==400 and response.json()['error']=='invalid_request'
            assert provider.store.count('client')==0
    asyncio.run(run())


def test_opaque_oauth_records_have_single_consumer_across_connections(tmp_path):
    first=OAuthStore(tmp_path);second=OAuthStore(tmp_path)
    try:
        key=secrets.token_urlsafe(32)
        first.put('code',key,{'fixture':True},time.time()+60)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda store:store.get('code',key,consume=True),[first,second]))
        assert sum(x is not None for x in results)==1
    finally:first.close();second.close()


def test_expired_access_record_is_refused(oauth_case):
    cfg,provider,app,owner=oauth_case
    token=provider.issue_tokens('isolated',['control'])
    with provider.store.db:
        provider.store.db.execute("UPDATE entries SET expires=? WHERE kind='access'",(time.time()-1,))
    async def run():
        async with client(app) as c:
            response=await c.get('/mcp',headers={'Authorization':'Bearer '+token.access_token})
            assert response.status_code==401
    asyncio.run(run())
