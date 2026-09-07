"""Synthetic-only model discovery: exact IDs, bounded GET and owned cleanup."""

import asyncio
import importlib
import json

import pytest
from aiohttp import ClientSession, web


@pytest.fixture
def models():
    return importlib.import_module("custom_components.ufanet_intercom.voice_models")


IDS = ["large-v3", "large-v3-turbo", "small", "gigaam-v3-e2e-rnnt"]


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        " large-v3",
        "small ",
        "a\nb",
        "a\x00b",
        "модель",
        "a?b",
        "a#b",
        "a" * 129,
        1,
        None,
        "default",
    ],
)
def test_model_id_rejects_without_repair(models, value):
    assert not models.valid_model_id(value)


@pytest.mark.parametrize("value", IDS + ["Org/Exact_ID:v1.2", "a" * 128])
def test_exact_manual_ids(models, value):
    assert models.valid_model_id(value)


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        (
            "https://stt.invalid/v1/audio/transcriptions",
            "https://stt.invalid/v1/models",
        ),
        (
            "https://stt.invalid/proxy/v1/audio/transcriptions",
            "https://stt.invalid/proxy/v1/models",
        ),
        ("https://stt.invalid/audio/transcriptions", "https://stt.invalid/models"),
        ("https://stt.invalid/custom", None),
        ("https://stt.invalid/v1/audio/transcriptions/", None),
        ("https://stt.invalid/a/../v1/audio/transcriptions", None),
        ("https://stt.invalid/%2e%2e/v1/audio/transcriptions", None),
    ],
)
def test_catalog_sibling_only(models, endpoint, expected):
    assert models.catalog_url(models.SttConfig(endpoint=endpoint)) == expected


def test_catalog_dedupe_sort_ignore_alias_no_default(models):
    payload = {
        "data": [{"id": i} for i in IDS + IDS + ["default", " invalid"]],
        "aliases": {"default": "gigaam-v3-e2e-rnnt"},
    }
    assert models.parse_model_catalog(
        json.dumps(payload).encode(), openai=False
    ) == tuple(sorted(IDS))


@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        b"{}",
        b'{"data":{}}',
        b'{"data":[],"data":[]}',
        b'{"data":[],"bad":NaN}',
        b'{"data":[],"bad":Infinity}',
        b"\xff",
        b"[" * 1500,
        json.dumps({"data": [{"id": "small"}] * 257}).encode(),
    ],
)
def test_invalid_catalog_fails_closed(models, body):
    with pytest.raises(ValueError):
        models.parse_model_catalog(body, openai=False)


def test_public_openai_filter_only_transcription_not_realtime(models):
    accepted = [
        "whisper-1",
        "gpt-4o-transcribe",
        "gpt-4o-mini-transcribe",
        "gpt-4o-transcribe-diarize",
        "gpt-4o-mini-transcribe-2025-12-15",
    ]
    rejected = [
        "gpt-4o",
        "text-embedding-3-small",
        "dall-e-3",
        "tts-1",
        "gpt-4o-realtime-preview",
        "gpt-realtime",
        "gpt-4o-transcribe-garbage",
        "small",
    ]
    body = json.dumps({"data": [{"id": i} for i in accepted + rejected]}).encode()
    assert models.parse_model_catalog(body, openai=True) == tuple(sorted(accepted))
    assert "small" in models.parse_model_catalog(body, openai=False)


class Content:
    def __init__(self, body):
        self.body = body
        self.limits = []

    async def read(self, limit):
        self.limits.append(limit)
        if isinstance(self.body, BaseException):
            raise self.body
        chunk, self.body = self.body[:limit], self.body[limit:]
        return chunk


class Response:
    def __init__(self, body=b'{"data":[{"id":"small"}]}', status=200, headers=None):
        self.status = status
        self.headers = {} if headers is None else headers
        self.content = Content(body)
        self.released = False

    def release(self):
        self.released = True


class Session:
    def __init__(self, response, kwargs):
        self.response = response
        self.kwargs = kwargs
        self.requests = []
        self._retry_connection = True
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True

    async def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def fake_session(models, monkeypatch, response):
    sessions = []

    def factory(**kwargs):
        session = Session(response, kwargs)
        sessions.append(session)
        return session

    monkeypatch.setattr(models, "ClientSession", factory)
    return sessions


@pytest.mark.asyncio
async def test_authenticated_one_get_request_local_header_and_cleanup(
    models, monkeypatch
):
    response = Response(json.dumps({"data": [{"id": i} for i in IDS]}).encode())
    sessions = fake_session(models, monkeypatch, response)
    result = await models.async_discover_models(
        models.SttConfig(
            endpoint="https://stt.invalid/base/v1/audio/transcriptions",
            token="SYNTHETIC-KEY",
        )
    )
    assert result.models == tuple(sorted(IDS)) and result.error is None
    (session,) = sessions
    assert session.closed and response.released and session._retry_connection is False
    assert session.kwargs["timeout"].total <= 10
    assert session.kwargs["trust_env"] is False
    assert not session.kwargs.get("headers")
    assert session.requests == [
        (
            "https://stt.invalid/base/v1/models",
            {
                "headers": {
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "Authorization": "Bearer SYNTHETIC-KEY",
                },
                "allow_redirects": False,
                "auto_decompress": False,
            },
        )
    ]
    assert all(0 < n <= 16384 for n in response.content.limits)
    assert "SYNTHETIC" not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "status",
        "redirect",
        "declared",
        "stream",
        "gzip",
        "malformed",
        "empty",
        "timeout",
        "connect",
        "cancel_read",
        "cancel_get",
    ],
)
async def test_failures_and_cancellation_close_every_owner(
    models, monkeypatch, caplog, failure
):
    response = Response()
    if failure == "status":
        response.status = 401
    elif failure == "redirect":
        response.status = 302
        response.headers["Location"] = "https://other.invalid/SECRET"
    elif failure == "declared":
        response.headers["Content-Length"] = str(256 * 1024 + 1)
    elif failure == "stream":
        response.content.body = b" " * (256 * 1024 + 1)
    elif failure == "gzip":
        response.headers["Content-Encoding"] = "gzip"
    elif failure == "malformed":
        response.content.body = b"SECRET-BODY"
    elif failure == "empty":
        response.content.body = b'{"data":[]}'
    elif failure in ("timeout", "cancel_read"):
        response.content.body = (
            TimeoutError("SECRET") if failure == "timeout" else asyncio.CancelledError()
        )
    elif failure in ("connect", "cancel_get"):
        response = (
            OSError("SECRET") if failure == "connect" else asyncio.CancelledError()
        )
    sessions = fake_session(models, monkeypatch, response)
    call = models.async_discover_models(
        models.SttConfig(
            endpoint="https://stt.invalid/v1/audio/transcriptions", token="SECRET-KEY"
        )
    )
    if failure.startswith("cancel"):
        with pytest.raises(asyncio.CancelledError):
            await call
    else:
        result = await call
        assert result.models == () and result.error in (
            "models_unavailable",
            "models_empty",
        )
        assert "SECRET" not in repr(result) + caplog.text
    assert len(sessions) == 1 and sessions[0].closed
    assert len(sessions[0].requests) == 1
    if isinstance(response, Response):
        assert response.released
        if failure == "declared":
            assert not response.content.limits
        if failure == "stream":
            assert sum(response.content.limits) <= 256 * 1024 + 1


@pytest.mark.asyncio
async def test_unsupported_path_never_creates_session(models, monkeypatch):
    sessions = fake_session(models, monkeypatch, Response())
    result = await models.async_discover_models(
        models.SttConfig(endpoint="https://stt.invalid/custom")
    )
    assert result.models == () and result.error == "models_unsupported"
    assert sessions == []


@pytest.mark.asyncio
async def test_real_loopback_get_auth_and_no_redirect_following(models, monkeypatch):
    receipts = []
    sessions = []
    mode = "success"

    async def catalog(request):
        receipts.append((request.path, request.headers.get("Authorization")))
        if mode == "redirect":
            raise web.HTTPFound("/must-not-follow")
        if mode == "disconnect":
            request.transport.close()
            return web.Response()
        return web.json_response(
            {"data": [{"id": i} for i in IDS], "aliases": {"default": IDS[-1]}}
        )

    def factory(**kwargs):
        session = ClientSession(**kwargs)
        sessions.append(session)
        return session

    monkeypatch.setattr(models, "ClientSession", factory)
    app = web.Application()
    app.router.add_get("/prefix/v1/models", catalog)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        port = site._server.sockets[0].getsockname()[1]
        config = models.SttConfig(
            endpoint=f"http://127.0.0.1:{port}/prefix/v1/audio/transcriptions",
            token="SYNTHETIC-LOCAL-KEY",
        )
        result = await models.async_discover_models(config)
        assert result.models == tuple(sorted(IDS))
        mode = "redirect"
        assert (
            await models.async_discover_models(config)
        ).error == "models_unavailable"
        mode = "disconnect"
        assert (
            await models.async_discover_models(config)
        ).error == "models_unavailable"
        assert receipts == [("/prefix/v1/models", "Bearer SYNTHETIC-LOCAL-KEY")] * 3
        assert len(sessions) == 3 and all(s.closed for s in sessions)
    finally:
        await runner.cleanup()
