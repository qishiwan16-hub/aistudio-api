"""Tests for system and configuration routes."""

from __future__ import annotations

import httpx
import pytest

from aistudio_api.api.app import app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


@pytest.mark.asyncio
async def test_health_endpoint():
    async with _client() as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_webdriver_status_endpoint():
    async with _client() as client:
        resp = await client.get("/wd/hub/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["busy"] is False


@pytest.mark.asyncio
async def test_auth_check_endpoint():
    async with _client() as client:
        resp = await client.get("/auth/check")
        assert resp.status_code == 200
        data = resp.json()
        assert "auth_enabled" in data


@pytest.mark.asyncio
async def test_spa_endpoints():
    import asyncio

    async with _client() as client:
        responses = await asyncio.gather(
            *[
                client.get(path, headers={"Accept": "text/html"})
                for path in ("/", "/login", "/accounts", "/settings")
            ]
        )
        for resp in responses:
            assert resp.status_code == 200
            assert "<!DOCTYPE html>" in resp.text
            assert "Accept" in resp.headers.get("Vary", "")
            assert "no-store" in resp.headers.get("Cache-Control", "")


@pytest.mark.asyncio
async def test_signer_server_management_routes_roundtrip_proxy(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from aistudio_api.api import routes_system
    from aistudio_api.infrastructure.gateway import model_defaults

    config_path = tmp_path / "config.yaml"
    config_path.write_text("waa:\n  active_server: remote\n  servers: []\n", encoding="utf-8")
    monkeypatch.setattr(model_defaults, "_resolve_config_path", lambda _path=None: config_path)
    monkeypatch.setattr(routes_system, "_resolve_config_path", lambda _path=None: config_path)

    async with _client() as client:
        saved = await client.put(
            "/config/signer-servers",
            json={
                "id": "remote",
                "name": "Remote signer",
                "url": "https://signer.example",
                "proxy_url": "http://proxy.example:8080",
                "headers": {"X-Test": "yes"},
            },
        )
        assert saved.status_code == 200
        assert saved.json()["server"]["proxy_url"] == "http://proxy.example:8080"
        listed = await client.get("/config/signer-servers")
        assert listed.json()["servers"][0]["proxy_url"] == "http://proxy.example:8080"
        switched = await client.post(
            "/config/signer-servers/switch", json={"server_id": "remote"}
        )
        assert switched.status_code == 200
        removed = await client.delete("/config/signer-servers/remote")
        assert removed.status_code == 400

    response = MagicMock(status_code=200)
    response.json.return_value = {"ok": True}

    created_clients = []

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            created_clients.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url):
            return response

    fake_httpx = SimpleNamespace(AsyncClient=FakeAsyncClient)
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)
    result = await routes_system.test_signer_server(
        routes_system.TestSignerServerRequest(
            url="https://signer.example", proxy_url="http://proxy.example:8080"
        )
    )
    assert result["ok"] is True
    assert created_clients[0].kwargs["proxy"] == "http://proxy.example:8080"


@pytest.mark.asyncio
async def test_get_config_endpoint():
    async with _client() as client:
        resp = await client.get("/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "port" in data
        assert "browser_port" in data
        assert "yaml_content" in data


@pytest.mark.asyncio
async def test_update_config_yaml_validation():
    async with _client() as client:
        resp = await client.put(
            "/config/yaml", json={"yaml_content": "invalid: [yaml: broken"}
        )
        assert resp.status_code == 400

        resp_non_dict = await client.put(
            "/config/yaml", json={"yaml_content": "- item1\n- item2"}
        )
        assert resp_non_dict.status_code == 400


@pytest.mark.asyncio
async def test_rotation_status_and_clear_cooldown():
    from unittest.mock import MagicMock

    from aistudio_api.api.state import runtime_state
    from aistudio_api.application.account_rotator import AccountRotator

    mock_rotator = MagicMock(spec=AccountRotator)
    mock_rotator.get_all_stats.return_value = {"acc_1": {"requests": 1}}

    runtime_state.rotator = mock_rotator
    async with _client() as client:
        resp = await client.get("/rotation")
        assert resp.status_code == 200
        data = resp.json()
        assert data["mode"] == "sticky"
        assert "accounts" in data

        resp_clear = await client.post(
            "/rotation/clear-cooldown", json={"account_id": "acc_1"}
        )
        assert resp_clear.status_code == 200
        mock_rotator.clear_cooldown.assert_called_once_with("acc_1", model=None)
