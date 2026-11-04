from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway import browserless
from aistudio_api.infrastructure.gateway.browserless import BrowserlessSession


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("node_proxy", "global_proxy", "expected_proxy"),
    [
        ("", "http://global-proxy:8080", "http://global-proxy:8080"),
        ("http://node-proxy:8080", "http://global-proxy:8080", "http://node-proxy:8080"),
        ("direct", "http://global-proxy:8080", None),
    ],
)
async def test_browserless_uses_active_signer_proxy(
    monkeypatch, node_proxy, global_proxy, expected_proxy
):
    active = {
        "url": "https://signer.example",
        "proxy_url": node_proxy,
        "user_agent": "test-agent",
        "headers": {},
    }
    monkeypatch.setattr(browserless, "get_active_waa_server", lambda: active)
    monkeypatch.setattr(settings, "proxy_url", global_proxy)
    session = BrowserlessSession()

    class ReadyGate:
        def __init__(self):
            self.state = browserless.SessionState.READY
            self.context = SimpleNamespace(auth_file="account.json")

        @property
        def active_context(self):
            return self.context

        def is_ready(self):
            return True

        def reset(self):
            pass

    session._gate = ReadyGate()
    context = await session.ensure_ready()

    assert session._signer._remote_signer.proxy_url == expected_proxy
    assert context.auth_file == "account.json"
    await session.close()
