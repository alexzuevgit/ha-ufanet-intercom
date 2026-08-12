"""Real-aiohttp TLS loopback receipt proofs for the physical transport.

The synthetic resolver maps only the fixed provider hostname to 127.0.0.1. No
provider endpoint or physical device is contacted.
"""

from __future__ import annotations

import asyncio
import socket
import ssl
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from aiohttp import ClientSession, ClientTimeout, TCPConnector, web
from aiohttp.abc import AbstractResolver, ResolveResult

import custom_components.ufanet_intercom.api as api
from custom_components.ufanet_intercom.api import (
    UfanetOpenError,
    UfanetOpenUnknownOutcome,
)
from custom_components.ufanet_intercom.const import (
    BASE_URL,
    discovered_door_binding,
    discovered_door_key,
)

IDENTITY_KEY = bytes(range(32))
SHARED_ID = 424_242
TARGET_KEY = discovered_door_key(IDENTITY_KEY, SHARED_ID, 0)
TRUSTED_BINDINGS = {
    TARGET_KEY: discovered_door_binding(
        IDENTITY_KEY,
        shared_id=SHARED_ID,
        door=0,
        model=21,
        house=7001,
        contract=8001,
        cctv_number="synthetic-camera",
    )
}
INVENTORY = [
    {
        "id": SHARED_ID,
        "model": 21,
        "custom_name": "Synthetic loopback entrance",
        "string_view": "Synthetic loopback entrance",
        "open_type": "http",
        "disable_button": False,
        "is_blocked": False,
        "role": {"id": 2, "name": "synthetic-role"},
        "cctv_number": "synthetic-camera",
        "relays": [],
        "address": "synthetic-address",
        "contract": 8001,
        "house": 7001,
    }
]


class LoopbackResolver(AbstractResolver):
    """Resolve the one fixed production hostname to IPv4 loopback."""

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET
    ) -> list[ResolveResult]:
        assert host == "dom.ufanet.ru"
        return [
            ResolveResult(
                hostname=host,
                host="127.0.0.1",
                port=port,
                family=socket.AF_INET,
                proto=socket.IPPROTO_TCP,
                flags=0,
            )
        ]

    async def close(self) -> None:
        return None


@dataclass(slots=True)
class ReceiptState:
    mode: str = "success"
    physical_receipts: int = 0
    redirect_receipts: int = 0
    physical_started: asyncio.Event = field(default_factory=asyncio.Event)
    release_physical: asyncio.Event = field(default_factory=asyncio.Event)

    def reset(self, mode: str) -> None:
        self.mode = mode
        self.physical_receipts = 0
        self.redirect_receipts = 0
        self.physical_started = asyncio.Event()
        self.release_physical = asyncio.Event()


def _create_test_tls_contexts(tmp_path: Path) -> tuple[ssl.SSLContext, ssl.SSLContext]:
    """Create an ephemeral CA-like self-signed certificate for the fixed hostname."""

    certificate = tmp_path / "loopback-cert.pem"
    private_key = tmp_path / "loopback-key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(private_key),
            "-out",
            str(certificate),
            "-days",
            "1",
            "-subj",
            "/CN=dom.ufanet.ru",
            "-addext",
            "subjectAltName=DNS:dom.ufanet.ru",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
    )
    private_key.chmod(0o600)

    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certificate, private_key)
    client_context = ssl.create_default_context(cafile=certificate)
    client_context.check_hostname = True
    return server_context, client_context


async def _new_client(
    client_context: ssl.SSLContext,
) -> tuple[api.UfanetClient, ClientSession, ClientSession]:
    read_connector = TCPConnector(
        resolver=LoopbackResolver(),
        ssl=client_context,
        limit=1,
        force_close=True,
    )
    open_connector = TCPConnector(
        resolver=LoopbackResolver(),
        ssl=client_context,
        limit=1,
        force_close=True,
    )
    read_session = ClientSession(
        connector=read_connector,
        timeout=ClientTimeout(total=2),
        middlewares=(),
    )
    open_session = ClientSession(
        connector=open_connector,
        timeout=ClientTimeout(total=2),
        middlewares=(),
    )
    for session in (read_session, open_session):
        assert type(session) is ClientSession
        assert type(session._retry_connection) is bool
        session._retry_connection = False
        assert session._retry_connection is False
        assert session._middlewares == ()
        assert "Authorization" not in session.headers
    assert read_session.connector is read_connector
    assert open_session.connector is open_connector
    assert read_session.connector is not open_session.connector

    client = api.UfanetClient(
        read_session,
        "SYNTHETIC-CONTRACT",
        "synthetic-password",
        identity_key=IDENTITY_KEY,
        trusted_bindings=TRUSTED_BINDINGS,
        open_session=open_session,
    )
    await client.async_login_and_discover()
    return client, read_session, open_session


async def _close_sessions(*sessions: ClientSession) -> None:
    await asyncio.gather(*(session.close() for session in sessions))


@pytest.mark.asyncio
async def test_real_aiohttp_tls_physical_transport_has_bounded_receipts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Prove exactly-once receipt behavior using real aiohttp over local TLS."""

    assert BASE_URL == "https://dom.ufanet.ru"
    state = ReceiptState()

    async def login(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "token": {
                    "access": "aaa.bbb.ccc",
                    "refresh": "ddd.eee.fff",
                    "exp": int(time.time()) + 3600,
                }
            }
        )

    async def inventory(_request: web.Request) -> web.Response:
        return web.json_response(INVENTORY)

    async def redirect_target(_request: web.Request) -> web.Response:
        state.redirect_receipts += 1
        return web.json_response({"result": True})

    async def open_entrance(request: web.Request) -> web.StreamResponse:
        assert request.query == {"door": "0"}
        assert request.headers.get("Authorization") == "JWT aaa.bbb.ccc"
        assert request.headers.getall("Authorization") == ["JWT aaa.bbb.ccc"]
        state.physical_receipts += 1
        state.physical_started.set()
        mode = state.mode
        if mode == "success":
            return web.json_response({"result": True})
        if mode == "401":
            return web.json_response({"detail": "synthetic"}, status=401)
        if mode == "redirect":
            raise web.HTTPFound("/redirect-target")
        if mode == "drop":
            assert request.transport is not None
            request.transport.abort()
            return web.Response()
        if mode == "wait_success":
            await state.release_physical.wait()
            return web.json_response({"result": True})
        raise AssertionError(mode)

    app = web.Application()
    app.router.add_post("/api/v1/auth/auth_by_contract/", login)
    app.router.add_post("/api/v1/auth/refresh/", login)
    app.router.add_get("/api/v0/skud/shared/", inventory)
    app.router.add_get("/api/v0/skud/shared/{shared_id}/open/", open_entrance)
    app.router.add_get("/redirect-target", redirect_target)

    server_context, client_context = _create_test_tls_contexts(tmp_path)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_context)
    await site.start()
    server = site._server
    assert server is not None and server.sockets
    port = server.sockets[0].getsockname()[1]

    monkeypatch.setattr(api, "BASE_URL", f"https://dom.ufanet.ru:{port}")
    monkeypatch.setattr(
        api,
        "_OPEN_TIMEOUT",
        ClientTimeout(total=0.25, connect=0.1, sock_read=0.2),
    )

    async def run_open(expected_error: type[BaseException] | None = None) -> str | None:
        api._current_open_gate().last_attempt = None
        client, read_session, open_session = await _new_client(client_context)
        try:
            if expected_error is None:
                await client.async_open(TARGET_KEY)
            else:
                with pytest.raises(expected_error):
                    await client.async_open(TARGET_KEY)
            return client.last_physical_outcome
        finally:
            await _close_sessions(read_session, open_session)

    try:
        state.reset("success")
        assert await run_open() == "confirmed"
        assert state.physical_receipts == 1

        state.reset("success")
        api._current_open_gate().last_attempt = None
        client, read_session, open_session = await _new_client(client_context)
        try:
            with pytest.raises(UfanetOpenError):
                await client.async_open("arbitrary")
            assert state.physical_receipts == 0
        finally:
            await _close_sessions(read_session, open_session)

        for mode in ("401", "redirect"):
            state.reset(mode)
            assert await run_open(UfanetOpenError) == "not_confirmed"
            assert state.physical_receipts == 1
            assert state.redirect_receipts == 0

        state.reset("drop")
        assert await run_open(UfanetOpenUnknownOutcome) == "unknown"
        assert state.physical_receipts == 1

        state.reset("wait_success")
        api._current_open_gate().last_attempt = None
        client, read_session, open_session = await _new_client(client_context)
        try:
            timeout_task = asyncio.create_task(client.async_open(TARGET_KEY))
            await asyncio.wait_for(state.physical_started.wait(), timeout=1)
            with pytest.raises(UfanetOpenUnknownOutcome):
                await timeout_task
            assert state.physical_receipts == 1
            with pytest.raises(UfanetOpenError, match="cooldown"):
                await client.async_open(TARGET_KEY)
            assert state.physical_receipts == 1
        finally:
            state.release_physical.set()
            await _close_sessions(read_session, open_session)

        state.reset("wait_success")
        api._current_open_gate().last_attempt = None
        client, read_session, open_session = await _new_client(client_context)
        try:
            cancellation_task = asyncio.create_task(client.async_open(TARGET_KEY))
            await asyncio.wait_for(state.physical_started.wait(), timeout=1)
            cancellation_task.cancel("synthetic-private-cancel")
            with pytest.raises(asyncio.CancelledError) as raised:
                await cancellation_task
            assert raised.value.args == ()
            assert getattr(raised.value, "__notes__", []) == []
            assert state.physical_receipts == 1
            with pytest.raises(UfanetOpenError, match="cooldown"):
                await client.async_open(TARGET_KEY)
            assert state.physical_receipts == 1
        finally:
            state.release_physical.set()
            await _close_sessions(read_session, open_session)

        state.reset("wait_success")
        api._current_open_gate().last_attempt = None
        client, read_session, open_session = await _new_client(client_context)
        try:
            first = asyncio.create_task(client.async_open(TARGET_KEY))
            await asyncio.wait_for(state.physical_started.wait(), timeout=1)
            with pytest.raises(api.UfanetConcurrentOpenError):
                await client.async_open(TARGET_KEY)
            assert state.physical_receipts == 1
            state.release_physical.set()
            await first
            assert state.physical_receipts == 1
        finally:
            state.release_physical.set()
            await _close_sessions(read_session, open_session)

        state.reset("success")
        api._current_open_gate().last_attempt = None
        client, read_session, open_session = await _new_client(client_context)
        try:
            await client.async_open(TARGET_KEY)
            assert state.physical_receipts == 1
            with pytest.raises(UfanetOpenError, match="cooldown"):
                await client.async_open(TARGET_KEY)
            assert state.physical_receipts == 1
        finally:
            await _close_sessions(read_session, open_session)
    finally:
        await runner.cleanup()
