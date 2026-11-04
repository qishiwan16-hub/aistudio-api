from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from aistudio_api.infrastructure.waa import signer as signer_module
from aistudio_api.infrastructure.waa.signer import RemoteProofSigner


@pytest.mark.asyncio
async def test_remote_proof_signer_uses_configured_proxy(monkeypatch):
    created = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.is_closed = False
            self.aclose = AsyncMock()
            created.append(self)

    monkeypatch.setattr(signer_module.httpx, "AsyncClient", FakeClient)
    signer = RemoteProofSigner(
        server_url="https://signer.example",
        proxy_url="socks5://127.0.0.1:1080",
    )

    client = await signer._get_client()
    assert client.kwargs["proxy"] == "socks5://127.0.0.1:1080"
    assert client.kwargs["timeout"] == 30.0
    await signer.close()
    client.aclose.assert_awaited_once()
