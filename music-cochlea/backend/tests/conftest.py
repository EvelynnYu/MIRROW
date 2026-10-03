"""The distributed suite is offline: accidental live HTTP is rejected."""
import pytest
import httpx
import requests
from starlette.testclient import _TestClientTransport

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        raise RuntimeError('Live networking is disabled in offline tests')
    monkeypatch.setattr(requests.sessions.Session, 'request', blocked)
    original = httpx.AsyncClient.send
    async def send(client, request, *args, **kwargs):
        if isinstance(client._transport, httpx.ASGITransport):
            return await original(client, request, *args, **kwargs)
        raise RuntimeError('Live networking is disabled in offline tests')
    monkeypatch.setattr(httpx.AsyncClient, 'send', send)
    original_sync = httpx.Client.send
    def send_sync(client, request, *args, **kwargs):
        if isinstance(client._transport, _TestClientTransport):
            return original_sync(client, request, *args, **kwargs)
        return blocked(client, request, *args, **kwargs)
    monkeypatch.setattr(httpx.Client, 'send', send_sync)
