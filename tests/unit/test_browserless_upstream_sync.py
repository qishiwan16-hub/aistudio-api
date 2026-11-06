from __future__ import annotations

import asyncio
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from aistudio_api.api.state import runtime_state
from aistudio_api.application import api_service_gemini as service
from aistudio_api.application.account_orchestrator import RequestRetryState
from aistudio_api.application.cookie_maintenance import CookieMaintenance
from aistudio_api.domain.errors import AuthError
from aistudio_api.infrastructure.account.session_cookies import (
    apply_set_cookies,
    load_cookies_from_auth_file,
    probe_session,
    session_anchor,
)
from aistudio_api.infrastructure.gateway.browserless import BrowserlessSession
from aistudio_api.infrastructure.waa.signer import RemoteProofSigner


def account_file(tmp_path, name, sid, auth_user="0"):
    folder = tmp_path / "accounts" / name
    folder.mkdir(parents=True)
    path = folder / "auth.json"
    path.write_text(json.dumps({"cookies": [
        {"name": "SID", "value": sid, "domain": ".google.com", "path": "/"},
        {"name": "__Secure-1PSIDTS", "value": "old", "domain": ".google.com", "path": "/"},
    ], "origins": [{"origin": "keep"}]}))
    (folder / "meta.json").write_text(json.dumps({"auth_user": auth_user}))
    return path


def test_cookie_updates_only_same_session_and_preserve_storage_state(tmp_path):
    first = account_file(tmp_path, "a", "session-a")
    sibling = account_file(tmp_path, "b", "session-a", "1")
    unrelated = account_file(tmp_path, "c", "session-c")
    assert apply_set_cookies(str(first), ["__Secure-1PSIDTS=new; Domain=.google.com; Secure"]) == ["__Secure-1PSIDTS"]
    assert load_cookies_from_auth_file(str(sibling))["__Secure-1PSIDTS"] == "new"
    assert load_cookies_from_auth_file(str(unrelated))["__Secure-1PSIDTS"] == "old"
    assert json.loads(first.read_text())["origins"] == [{"origin": "keep"}]


def test_cookie_response_cannot_overwrite_reimported_session(tmp_path):
    path = account_file(tmp_path, "a", "new-session")
    assert apply_set_cookies(str(path), ["__Secure-1PSIDTS=stale"], expected_anchor=session_anchor({"SID": "old-session"})) == []
    assert load_cookies_from_auth_file(str(path))["__Secure-1PSIDTS"] == "old"


def test_cookie_headers_ignore_foreign_domains_and_expired_values(tmp_path):
    path = account_file(tmp_path, "a", "session-a")
    assert apply_set_cookies(str(path), [
        "SID=foreign; Domain=evil.test", "SID=deleted; Domain=.google.com",
        "SID=expired; Max-Age=0", "SID=expired; Expires=Thu, 01 Jan 1970 00:00:00 GMT",
    ]) == []
    assert load_cookies_from_auth_file(str(path))["SID"] == "session-a"


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "location", "expected"), [
    (200, "", True), (302, "/welcome", False),
    (302, "https://accounts.google.com/ServiceLogin", False),
    (429, "", None), (503, "", None), (302, "https://other.test/welcome", None),
])
async def test_session_probe_is_conservative(status, location, expected):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, headers={"location": location})
    )) as client:
        assert await probe_session(client, {"SID": "test"}, "test-agent") is expected


@pytest.mark.asyncio
async def test_remote_proof_force_refresh_is_explicit():
    payloads = []

    def respond(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "proof": "proof"})

    signer = RemoteProofSigner("https://signer.test")
    signer._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        await signer.mint_proof(cookies={"SID": "test"})
        await signer.mint_proof(cookies={"SID": "test"}, force_refresh=True)
        assert "forceRefresh" not in payloads[0]
        assert payloads[1]["forceRefresh"] is True
    finally:
        await signer.close()


@pytest.mark.asyncio
async def test_proof_refresh_is_request_local_and_consumed(tmp_path):
    path = account_file(tmp_path, "a", "session-a")
    session = BrowserlessSession()
    session._auth_file = str(path)
    active = SimpleNamespace(auth_file=str(path), auth_user="0", tier="Free")
    session.ensure_ready = AsyncMock(return_value=active)
    session._signer.generate_snapshot = AsyncMock(return_value="proof")

    async def refreshed():
        session.request_proof_refresh()
        await asyncio.sleep(0)
        await session.generate_snapshot([])
        await session.generate_snapshot([])

    async def normal():
        await session.generate_snapshot([])

    try:
        await asyncio.gather(refreshed(), normal())
        calls = session._signer.generate_snapshot.await_args_list
        assert sorted(call.kwargs["force_refresh"] for call in calls) == [False, False, True]
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_each_account_gets_its_own_auth_recovery(monkeypatch):
    account = SimpleNamespace(id="a")
    monkeypatch.setattr(runtime_state, "account_service", SimpleNamespace(get_active_account=lambda: account))
    monkeypatch.setattr(service, "record_rotator_event", MagicMock())
    switch = AsyncMock(return_value=True)
    monkeypatch.setattr(service, "try_switch_account", switch)
    client = MagicMock()
    state = RequestRetryState(max_attempts=8)
    for attempt in (0, 1, 2):
        if attempt == 2:
            account.id = "b"
        state.begin_attempt()
        assert await service.handle_attempt_exception(
            AuthError("permission denied"), attempt=attempt, model_path="flash",
            normalized_model="flash", client=client, retry_state=state,
        )
    assert state.auth_retried == {"a", "b"}
    assert state.exhausted_accounts == {"a"}
    switch.assert_awaited_once_with(model="flash", failed_account_id="a", excluded_account_ids={"a"})


@pytest.mark.asyncio
@pytest.mark.parametrize(("attempt", "has_output", "message"), [
    (4, False, "denied"), (0, True, "denied"), (0, False, "Region not supported"),
])
async def test_no_auth_retry_after_output_or_budget_or_region(monkeypatch, attempt, has_output, message):
    monkeypatch.setattr(runtime_state, "account_service", None)
    monkeypatch.setattr(service, "record_rotator_event", MagicMock())
    switch = AsyncMock()
    monkeypatch.setattr(service, "try_switch_account", switch)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as error:
        await service.handle_attempt_exception(AuthError(message), attempt=attempt,
            model_path="flash", normalized_model="flash", client=MagicMock(),
            retry_state=RequestRetryState(), has_yielded_data=has_output)
    assert error.value.status_code == 403
    switch.assert_not_awaited()


def test_retry_budget_accounts_for_available_pool(monkeypatch):
    accounts = [SimpleNamespace(id=str(i), enabled=True, tier="Free") for i in range(6)]
    monkeypatch.setattr(runtime_state, "account_service", SimpleNamespace(list_accounts=lambda: accounts))
    monkeypatch.setattr(runtime_state, "rotator", SimpleNamespace(is_account_available=lambda *a, **k: True))
    assert RequestRetryState.for_model("gemini-3.8-flash").max_attempts == 12


@pytest.mark.asyncio
async def test_cookie_maintenance_expires_only_matching_sessions(tmp_path, monkeypatch):
    first = account_file(tmp_path, "a", "session-a")
    sibling = account_file(tmp_path, "b", "session-a", "1")
    other = account_file(tmp_path, "c", "session-c")
    paths = {"a": first, "b": sibling, "c": other}
    for path in paths.values():
        os.utime(path, (time.time() - 1000, time.time() - 1000))
    accounts = [SimpleNamespace(id=name, enabled=True) for name in paths]
    store = SimpleNamespace(list_accounts=lambda: accounts, get_auth_path=lambda name: paths[name])
    rotator = SimpleNamespace(get_account_stats=lambda name: None, record_session_expired=MagicMock())
    requests = []

    def respond(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200)
        return httpx.Response(401 if "session-a" in request.headers["cookie"] else 429)

    maintenance = CookieMaintenance(store, rotator)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await maintenance.tick(client)
        await maintenance.tick(client)
    assert {call.args[0] for call in rotator.record_session_expired.call_args_list} == {"a", "b"}
    assert len(requests) == 4  # One probe+rotation per login session; no repeated tick.


@pytest.mark.asyncio
async def test_cookie_maintenance_waits_after_import_and_does_not_rotate_on_probe_error(tmp_path):
    path = account_file(tmp_path, "a", "session-a")
    store = SimpleNamespace(list_accounts=lambda: [SimpleNamespace(id="a", enabled=True)], get_auth_path=lambda name: path)
    rotator = SimpleNamespace(get_account_stats=lambda name: None, record_session_expired=MagicMock())
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: (calls.append(request), httpx.Response(429))[1]
    )) as client:
        maintenance = CookieMaintenance(store, rotator)
        await maintenance.tick(client)
        assert calls == []
        maintenance._last_attempt[session_anchor(load_cookies_from_auth_file(str(path)))] = time.time() - 1000
        await maintenance.tick(client)
    assert len(calls) == 1
    rotator.record_session_expired.assert_not_called()


def test_cookie_update_cannot_undo_a_newer_rolling_cookie(tmp_path):
    path = account_file(tmp_path, "a", "same-session")
    old = load_cookies_from_auth_file(str(path))
    apply_set_cookies(str(path), ["__Secure-1PSIDTS=newer"])
    assert apply_set_cookies(str(path), ["__Secure-1PSIDTS=older"], expected_cookies=old) == []
    assert load_cookies_from_auth_file(str(path))["__Secure-1PSIDTS"] == "newer"


@pytest.mark.asyncio
async def test_cookie_maintenance_ignores_401_after_cookie_reimport(tmp_path):
    path = account_file(tmp_path, "a", "same-session")
    os.utime(path, (time.time() - 1000, time.time() - 1000))
    store = SimpleNamespace(list_accounts=lambda: [SimpleNamespace(id="a", enabled=True)], get_auth_path=lambda name: path)
    rotator = SimpleNamespace(get_account_stats=lambda name: None, record_session_expired=MagicMock())

    def respond(request):
        if request.method == "GET":
            return httpx.Response(200)
        apply_set_cookies(str(path), ["__Secure-1PSIDTS=reimported"])
        return httpx.Response(401)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await CookieMaintenance(store, rotator).tick(client)
    rotator.record_session_expired.assert_not_called()


@pytest.mark.asyncio
async def test_rotator_excludes_exhausted_accounts_without_recycling(tmp_path, monkeypatch):
    from aistudio_api.application.account_rotator import AccountRotator
    from aistudio_api.infrastructure.account.account_store import AccountStore

    store = AccountStore(accounts_dir=tmp_path / "pool")
    account = store.save_account(name="test", email=None, storage_state={"cookies": []})
    rotator = AccountRotator(store)
    assert await rotator.get_next_account(excluded_account_ids={account.id}) is None
