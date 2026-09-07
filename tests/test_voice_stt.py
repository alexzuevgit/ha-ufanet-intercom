"""Strict contract tests for the bounded external speech-to-text client.

All audio and provider data are synthetic. Network tests bind only to loopback.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import socket
import struct
import wave
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import FrozenInstanceError, dataclass
from io import BytesIO
from typing import Any
from uuid import uuid4

import pytest
from aiohttp import ClientSession, ClientTimeout, FormData, web
from multidict import CIMultiDict

from custom_components.ufanet_intercom.voice_stt import (
    MAX_PCM_BYTES,
    MAX_PCM_SECONDS,
    MAX_STT_RESPONSE_BYTES,
    MAX_TRANSCRIPT_UTF8_BYTES,
    PCM_BYTE_ORDER,
    PCM_CHANNELS,
    PCM_SAMPLE_RATE_HZ,
    PCM_SAMPLE_WIDTH_BYTES,
    PCM_SIGNED,
    STT_ERROR_MESSAGE,
    SttClient,
    SttConfig,
    SttError,
    pcm_to_wav,
)


@dataclass(slots=True)
class RecordedRequest:
    url: str
    kwargs: dict[str, Any]


class FakeContent:
    def __init__(
        self,
        body: bytes,
        *,
        chunk_size: int | None = None,
        chunks: list[bytes | BaseException] | None = None,
    ) -> None:
        self._body = body
        self._offset = 0
        self._chunk_size = chunk_size
        self._chunks = None if chunks is None else list(chunks)
        self.read_limits: list[int] = []

    async def read(self, limit: int = -1) -> bytes:
        self.read_limits.append(limit)
        if self._chunks is not None:
            if not self._chunks:
                return b""
            item = self._chunks.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        remaining = len(self._body) - self._offset
        if remaining <= 0:
            return b""
        count = remaining if limit < 0 else min(remaining, limit)
        if self._chunk_size is not None:
            count = min(count, self._chunk_size)
        start = self._offset
        self._offset += count
        return self._body[start : start + count]


class FakeResponse:
    def __init__(
        self,
        status: object,
        body: bytes,
        *,
        headers: Mapping[str, str] | None = None,
        content: FakeContent | None = None,
        release_error: BaseException | None = None,
    ) -> None:
        self.status = status
        self.headers = {} if headers is None else headers
        self.content = FakeContent(body) if content is None else content
        self.release_error = release_error
        self.release_calls = 0

    def release(self) -> None:
        self.release_calls += 1
        if self.release_error is not None:
            raise self.release_error


Queued = FakeResponse | BaseException | Callable[[], Awaitable[FakeResponse]]


class FakeSession:
    def __init__(self, *queued: Queued) -> None:
        self.queue = list(queued)
        self.requests: list[RecordedRequest] = []

    async def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append(RecordedRequest(url, kwargs))
        if not self.queue:
            raise AssertionError("unexpected retry")
        item = self.queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return await item()
        return item


def json_response(
    value: object,
    *,
    status: object = 200,
    headers: Mapping[str, str] | None = None,
    chunk_size: int | None = None,
) -> FakeResponse:
    body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    actual_headers = {"Content-Length": str(len(body))} if headers is None else headers
    response = FakeResponse(status, body, headers=actual_headers)
    response.content._chunk_size = chunk_size
    return response


def assert_fixed_stt_error(error: SttError, *secrets: str) -> None:
    assert type(error) is SttError
    assert error.args == (STT_ERROR_MESSAGE,)
    assert str(error) == STT_ERROR_MESSAGE
    assert error.__cause__ is None
    assert error.__context__ is None
    assert getattr(error, "__notes__", []) == []
    rendered = repr(error)
    for secret in secrets:
        assert secret not in rendered
        assert secret not in str(error)


def test_pcm_constants_bind_exact_geometry() -> None:
    assert PCM_SAMPLE_RATE_HZ == 16_000
    assert PCM_CHANNELS == 1
    assert PCM_SAMPLE_WIDTH_BYTES == 2
    assert PCM_BYTE_ORDER == "little"
    assert PCM_SIGNED is True
    assert MAX_PCM_SECONDS == 8
    assert MAX_PCM_BYTES == 16_000 * 1 * 2 * 8


def test_config_is_frozen_slotted_exact_and_secret_safe() -> None:
    endpoint = f"https://stt.invalid/{uuid4().hex}"
    token = f"token-{uuid4().hex}"
    model = f"model-{uuid4().hex}"
    config = SttConfig(endpoint=endpoint, token=token, model=model)

    assert config.endpoint is endpoint
    assert config.token is token
    assert config.model is model
    assert config.allow_insecure_http is False
    assert not hasattr(config, "__dict__")
    with pytest.raises(FrozenInstanceError):
        config.token = "changed"  # type: ignore[misc]
    rendered = repr(config)
    assert rendered == "SttConfig()"
    assert endpoint not in rendered
    assert token not in rendered
    assert model not in rendered


class StringSubclass(str):
    """An inexact string rejected at strict configuration boundaries."""


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("endpoint", None),
        ("endpoint", StringSubclass("https://localhost/v1/audio/transcriptions")),
        ("endpoint", ""),
        ("endpoint", "localhost/v1/audio/transcriptions"),
        ("endpoint", "ftp://localhost/v1/audio/transcriptions"),
        ("endpoint", "https:///v1/audio/transcriptions"),
        ("endpoint", "https://user@localhost/v1/audio/transcriptions"),
        (
            "endpoint",
            "https:" + "//user:password@localhost/v1/audio/transcriptions",
        ),
        ("endpoint", "https://localhost/v1/audio/transcriptions?mode=fast"),
        ("endpoint", "https://localhost/v1/audio/transcriptions#private"),
        ("endpoint", "https://localhost:invalid/v1/audio/transcriptions"),
        ("endpoint", "https://localhost/v1/audio transcriptions"),
        ("endpoint", "https://localhost/" + "x" * 2041),
        ("token", None),
        ("token", StringSubclass("token")),
        ("token", "x" * 2049),
        ("model", None),
        ("model", StringSubclass("model")),
        ("model", "x" * 129),
        ("allow_insecure_http", None),
        ("allow_insecure_http", 0),
        ("allow_insecure_http", 1),
        ("allow_insecure_http", "false"),
    ],
)
def test_config_rejects_invalid_exact_bounded_values(field: str, value: object) -> None:
    values: dict[str, object] = {
        "endpoint": "http://127.0.0.1/v1/audio/transcriptions",
        "token": "",
        "model": "",
        "allow_insecure_http": False,
    }
    values[field] = value

    with pytest.raises(ValueError) as raised:
        SttConfig(**values)  # type: ignore[arg-type]

    assert repr(value) not in str(raised.value)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://stt.example/v1/audio/transcriptions",
        "https://192.0.2.1/v1/audio/transcriptions",
        "https://[2001:db8::1]:8443/v1/audio/transcriptions",
    ],
)
def test_config_accepts_https_with_token_without_insecure_opt_in(endpoint: str) -> None:
    config = SttConfig(endpoint=endpoint, token="synthetic-token")

    assert config.endpoint == endpoint
    assert config.allow_insecure_http is False


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost/v1/audio/transcriptions",
        "http://LOCALHOST:8080/v1/audio/transcriptions",
        "http://127.0.0.1/v1/audio/transcriptions",
        "http://127.255.255.254/v1/audio/transcriptions",
        "http://[::1]:8080/v1/audio/transcriptions",
    ],
)
def test_config_accepts_literal_http_loopback_with_token_by_default(
    endpoint: str,
) -> None:
    config = SttConfig(endpoint=endpoint, token="synthetic-token")

    assert config.endpoint == endpoint
    assert config.token == "synthetic-token"
    assert config.allow_insecure_http is False


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://10.0.0.1/v1/audio/transcriptions",
        "http://10.255.255.254/v1/audio/transcriptions",
        "http://172.16.0.1/v1/audio/transcriptions",
        "http://172.31.255.254/v1/audio/transcriptions",
        "http://192.168.0.1/v1/audio/transcriptions",
        "http://192.168.255.254/v1/audio/transcriptions",
        "http://[fc00::1]/v1/audio/transcriptions",
        "http://[fdff:ffff::1]/v1/audio/transcriptions",
        "http://[fe80::1]/v1/audio/transcriptions",
        "http://[febf:ffff::1]/v1/audio/transcriptions",
    ],
)
def test_config_requires_explicit_tokenless_http_for_local_networks(
    endpoint: str,
) -> None:
    with pytest.raises(ValueError):
        SttConfig(endpoint=endpoint)
    with pytest.raises(ValueError):
        SttConfig(
            endpoint=endpoint,
            token="synthetic-token",
            allow_insecure_http=True,
        )

    config = SttConfig(endpoint=endpoint, allow_insecure_http=True)

    assert config.endpoint == endpoint
    assert config.token == ""
    assert config.allow_insecure_http is True
    assert repr(config) == "SttConfig()"


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://8.8.8.8/v1/audio/transcriptions",
        "http://192.0.2.1/v1/audio/transcriptions",
        "http://198.51.100.1/v1/audio/transcriptions",
        "http://203.0.113.1/v1/audio/transcriptions",
        "http://169.254.1.1/v1/audio/transcriptions",
        "http://100.64.0.1/v1/audio/transcriptions",
        "http://[2001:db8::1]/v1/audio/transcriptions",
        "http://example.com/v1/audio/transcriptions",
        "http://stt.internal/v1/audio/transcriptions",
        "http://localhost.example/v1/audio/transcriptions",
    ],
)
def test_config_never_accepts_other_plain_http_targets(endpoint: str) -> None:
    for token in ("", "synthetic-token"):
        with pytest.raises(ValueError):
            SttConfig(
                endpoint=endpoint,
                token=token,
                allow_insecure_http=True,
            )


class BytesSubclass(bytes):
    """An inexact byte string rejected at the PCM boundary."""


class InjectedBaseFailure(BaseException):
    """A non-Exception provider failure injected at the public boundary."""


@pytest.mark.parametrize(
    "pcm",
    [
        None,
        bytearray(b"\0\0"),
        memoryview(b"\0\0"),
        BytesSubclass(b"\0\0"),
        b"",
        b"\0",
        b"\0" * (MAX_PCM_BYTES + 2),
    ],
)
def test_pcm_to_wav_rejects_wrong_type_empty_unaligned_and_oversize(
    pcm: object,
) -> None:
    with pytest.raises(ValueError):
        pcm_to_wav(pcm)


def test_pcm_to_wav_is_canonical_and_preserves_little_endian_samples() -> None:
    pcm = struct.pack("<hhhh", -32768, -1, 0, 32767)

    encoded = pcm_to_wav(pcm)

    assert type(encoded) is bytes
    assert encoded[:4] == b"RIFF"
    assert encoded[8:12] == b"WAVE"
    assert len(encoded) == 44 + len(pcm)
    with wave.open(BytesIO(encoded), "rb") as wav_file:
        assert wav_file.getnchannels() == PCM_CHANNELS
        assert wav_file.getsampwidth() == PCM_SAMPLE_WIDTH_BYTES
        assert wav_file.getframerate() == PCM_SAMPLE_RATE_HZ
        assert wav_file.getcomptype() == "NONE"
        assert wav_file.getnframes() == len(pcm) // PCM_SAMPLE_WIDTH_BYTES
        assert wav_file.readframes(wav_file.getnframes()) == pcm


def test_pcm_to_wav_accepts_exact_eight_second_limit() -> None:
    wav = pcm_to_wav(b"\0" * MAX_PCM_BYTES)
    assert len(wav) == 44 + MAX_PCM_BYTES


async def test_transcribe_posts_once_with_bounded_request_local_contract() -> None:
    response = json_response({"text": " открой дверь "}, chunk_size=2)
    session = FakeSession(response)
    token = f"secret-{uuid4().hex}"
    config = SttConfig(
        endpoint="https://stt.invalid/v1/audio/transcriptions",
        token=token,
        model="whisper-large-v3",
    )
    client = SttClient(session, config)

    text = await client.transcribe_pcm(b"\x00\x80\xff\x7f")

    assert text == " открой дверь "
    assert len(session.requests) == 1
    request = session.requests[0]
    assert request.url == config.endpoint
    assert request.kwargs["allow_redirects"] is False
    assert request.kwargs["auto_decompress"] is False
    assert "timeout" not in request.kwargs
    assert request.kwargs["headers"] == {
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "Authorization": f"Bearer {token}",
    }
    assert type(request.kwargs["data"]) is FormData
    assert response.release_calls == 1
    assert len(response.content.read_limits) > 1
    assert all(
        0 < limit <= MAX_STT_RESPONSE_BYTES + 1
        for limit in response.content.read_limits
    )


async def test_empty_token_and_model_are_absent_from_request() -> None:
    response = json_response({"text": "тест"})
    session = FakeSession(response)
    client = SttClient(
        session,
        SttConfig(endpoint="http://localhost/v1/audio/transcriptions"),
    )

    assert await client.transcribe_pcm(b"\0\0") == "тест"

    assert len(session.requests) == 1
    request = session.requests[0]
    assert request.kwargs["headers"] == {
        "Accept": "application/json",
        "Accept-Encoding": "identity",
    }
    fields = request.kwargs["data"]._fields
    assert [field[0]["name"] for field in fields] == ["file", "language"]


@pytest.mark.parametrize(
    "content_encoding",
    [None, "identity", "IDENTITY", "IdEnTiTy"],
    ids=["absent", "lowercase", "uppercase", "mixed-case"],
)
async def test_absent_or_exact_identity_content_encoding_is_accepted(
    content_encoding: str | None,
) -> None:
    body = b'{"text":"accepted"}'
    headers = {"Content-Length": str(len(body))}
    if content_encoding is not None:
        headers["Content-Encoding"] = content_encoding
    response = FakeResponse(200, body, headers=headers)
    session = FakeSession(response)
    client = SttClient(session, SttConfig(endpoint="http://localhost/stt"))

    assert await client.transcribe_pcm(b"\0\0") == "accepted"

    assert len(session.requests) == 1
    assert response.content.read_limits
    assert response.release_calls == 1


@pytest.mark.parametrize(
    "content_encoding",
    [
        "",
        "gzip",
        "x-gzip",
        "br",
        " identity",
        "identity ",
        "identity,gzip",
        "identity; q=1",
    ],
)
async def test_other_content_encoding_is_rejected_before_body_read(
    content_encoding: str,
) -> None:
    response = FakeResponse(
        200,
        b'{"text":"must not be read"}',
        headers={"Content-Encoding": content_encoding},
    )
    session = FakeSession(response)
    client = SttClient(session, SttConfig(endpoint="http://localhost/stt"))

    with pytest.raises(SttError) as raised:
        await client.transcribe_pcm(b"\0\0")

    assert len(session.requests) == 1
    assert response.content.read_limits == []
    assert response.release_calls == 1
    assert_fixed_stt_error(
        raised.value, *([content_encoding] if content_encoding else [])
    )


@pytest.mark.parametrize(
    "content_encodings",
    [("identity", "identity"), ("identity", "gzip")],
)
async def test_duplicate_content_encoding_is_rejected_before_body_read(
    content_encodings: tuple[str, str],
) -> None:
    headers: CIMultiDict[str] = CIMultiDict()
    for content_encoding in content_encodings:
        headers.add("Content-Encoding", content_encoding)
    response = FakeResponse(
        200,
        b'{"text":"must not be read"}',
        headers=headers,
    )
    session = FakeSession(response)
    client = SttClient(session, SttConfig(endpoint="http://localhost/stt"))

    with pytest.raises(SttError):
        await client.transcribe_pcm(b"\0\0")

    assert response.content.read_limits == []
    assert response.release_calls == 1


@pytest.mark.parametrize(
    "pcm",
    [
        None,
        bytearray(b"\0\0"),
        BytesSubclass(b"\0\0"),
        b"",
        b"\0",
        b"\0" * (MAX_PCM_BYTES + 2),
    ],
)
async def test_invalid_pcm_fails_before_any_receipt(pcm: object) -> None:
    session = FakeSession(json_response({"text": "must not be reached"}))
    client = SttClient(session, SttConfig(endpoint="http://localhost/stt"))

    with pytest.raises(SttError) as raised:
        await client.transcribe_pcm(pcm)

    assert session.requests == []
    assert_fixed_stt_error(raised.value)


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(201, b'{"text":"no"}'),
        FakeResponse(True, b'{"text":"no"}'),
        FakeResponse(200, b"\xff"),
        FakeResponse(200, b"{"),
        FakeResponse(200, b"null"),
        FakeResponse(200, b"[]"),
        FakeResponse(200, b'{"text":null}'),
        FakeResponse(200, b'{"text":7}'),
        FakeResponse(200, b'{"other":"value"}'),
        FakeResponse(200, b'{"text":"first","text":"second"}'),
        FakeResponse(200, rb'{"text":"\ud800"}'),
    ],
)
async def test_every_malformed_http_outcome_has_one_receipt_and_fixed_error(
    response: FakeResponse,
) -> None:
    session = FakeSession(response)
    client = SttClient(session, SttConfig(endpoint="http://localhost/stt"))

    with pytest.raises(SttError) as raised:
        await client.transcribe_pcm(b"\0\0")

    assert len(session.requests) == 1
    assert response.release_calls == 1
    assert_fixed_stt_error(raised.value)


@pytest.mark.parametrize("declared", [str(MAX_STT_RESPONSE_BYTES + 1), "invalid", "-1"])
async def test_content_length_is_strictly_prechecked_without_reading(
    declared: str,
) -> None:
    response = FakeResponse(
        200,
        b'{"text":"ignored"}',
        headers={"Content-Length": declared},
    )
    session = FakeSession(response)
    client = SttClient(session, SttConfig(endpoint="http://localhost/stt"))

    with pytest.raises(SttError):
        await client.transcribe_pcm(b"\0\0")

    assert len(session.requests) == 1
    assert response.content.read_limits == []
    assert response.release_calls == 1


async def test_streaming_cap_rejects_undeclared_oversize_response() -> None:
    prefix = b'{"text":"'
    response = FakeResponse(
        200,
        prefix + b"x" * MAX_STT_RESPONSE_BYTES,
        headers={},
    )
    session = FakeSession(response)
    client = SttClient(session, SttConfig(endpoint="http://localhost/stt"))

    with pytest.raises(SttError):
        await client.transcribe_pcm(b"\0\0")

    assert len(session.requests) == 1
    assert sum(response.content.read_limits) <= MAX_STT_RESPONSE_BYTES * 5
    assert response.release_calls == 1


@pytest.mark.parametrize(
    "text",
    ["x" * (MAX_TRANSCRIPT_UTF8_BYTES + 1), "я" * 2049],
)
async def test_transcript_utf8_size_is_bounded(text: str) -> None:
    response = json_response({"text": text})
    session = FakeSession(response)
    client = SttClient(session, SttConfig(endpoint="http://localhost/stt"))

    with pytest.raises(SttError):
        await client.transcribe_pcm(b"\0\0")

    assert len(session.requests) == 1
    assert response.release_calls == 1


async def test_exact_transcript_utf8_limit_and_empty_text_are_valid() -> None:
    for text in ("", "я" * (MAX_TRANSCRIPT_UTF8_BYTES // 2)):
        response = json_response({"text": text})
        session = FakeSession(response)
        client = SttClient(session, SttConfig(endpoint="http://localhost/stt"))
        assert await client.transcribe_pcm(b"\0\0") == text
        assert len(session.requests) == 1


async def test_transport_details_are_redacted_without_retry() -> None:
    token = f"token-{uuid4().hex}"
    hostile = f"provider-{uuid4().hex}"
    original = RuntimeError(hostile, token)
    original.add_note(hostile)
    session = FakeSession(original)
    client = SttClient(
        session,
        SttConfig(endpoint=f"http://localhost/{hostile}", token=token),
    )

    with pytest.raises(SttError) as raised:
        await client.transcribe_pcm(b"\0\0")

    assert len(session.requests) == 1
    assert_fixed_stt_error(raised.value, hostile, token)


@pytest.mark.parametrize("during_read", [False, True], ids=["post", "content-read"])
async def test_asyncio_timeout_is_fixed_redacted_and_never_retried(
    during_read: bool,
) -> None:
    secret = f"timeout-{uuid4().hex}"
    timeout = asyncio.TimeoutError(secret)  # noqa: UP041 - exercise asyncio alias
    timeout.add_note(f"note-{secret}")
    if during_read:
        content = FakeContent(b"", chunks=[timeout])
        response = FakeResponse(200, b"", content=content)
        session = FakeSession(response)
    else:
        response = None
        session = FakeSession(timeout)
    client = SttClient(session, SttConfig(endpoint="http://localhost/stt"))

    with pytest.raises(SttError) as raised:
        await client.transcribe_pcm(b"\0\0")

    assert len(session.requests) == 1
    assert session.queue == []
    if response is not None:
        assert response.release_calls == 1
    assert_fixed_stt_error(raised.value, secret)


@pytest.mark.parametrize(
    "failure_type",
    [InjectedBaseFailure, SystemExit],
    ids=["base-exception", "system-exit"],
)
@pytest.mark.parametrize("during_read", [False, True], ids=["post", "content-read"])
async def test_non_cancellation_base_exceptions_are_fixed_and_redacted(
    failure_type: type[BaseException], during_read: bool
) -> None:
    secret = f"base-failure-{uuid4().hex}"
    failure = failure_type(secret)
    failure.add_note(f"note-{secret}")
    if during_read:
        content = FakeContent(b"", chunks=[failure])
        response = FakeResponse(200, b"", content=content)
        session = FakeSession(response)
    else:
        response = None
        session = FakeSession(failure)
    client = SttClient(session, SttConfig(endpoint="http://localhost/stt"))

    with pytest.raises(SttError) as raised:
        await client.transcribe_pcm(b"\0\0")

    assert len(session.requests) == 1
    assert session.queue == []
    if response is not None:
        assert response.release_calls == 1
    assert_fixed_stt_error(raised.value, secret)


@pytest.mark.parametrize(
    "failure_type",
    [InjectedBaseFailure, SystemExit],
    ids=["base-exception", "system-exit"],
)
async def test_post_failure_clears_active_outer_exception_context(
    failure_type: type[BaseException],
) -> None:
    outer_secret = f"outer-secret-{uuid4().hex}"
    provider_secret = f"provider-secret-{uuid4().hex}"
    failure = failure_type(provider_secret)
    failure.add_note(f"note-{provider_secret}")
    session = FakeSession(failure)
    client = SttClient(session, SttConfig(endpoint="http://localhost/stt"))

    try:
        raise RuntimeError(outer_secret)
    except RuntimeError:
        with pytest.raises(SttError) as raised:
            await client.transcribe_pcm(b"\0\0")

    assert len(session.requests) == 1
    assert session.queue == []
    assert_fixed_stt_error(raised.value, outer_secret, provider_secret)


@pytest.mark.parametrize(
    "failure_type",
    [InjectedBaseFailure, SystemExit],
    ids=["base-exception", "system-exit"],
)
async def test_non_cancellation_release_failures_are_fixed_and_redacted(
    failure_type: type[BaseException],
) -> None:
    secret = f"release-failure-{uuid4().hex}"
    failure = failure_type(secret)
    failure.add_note(f"note-{secret}")
    response = json_response({"text": "must not escape"})
    response.release_error = failure
    session = FakeSession(response)
    client = SttClient(session, SttConfig(endpoint="http://localhost/stt"))

    with pytest.raises(SttError) as raised:
        await client.transcribe_pcm(b"\0\0")

    assert len(session.requests) == 1
    assert session.queue == []
    assert response.release_calls == 1
    assert_fixed_stt_error(raised.value, secret)


@pytest.mark.parametrize("during_read", [False, True])
async def test_cancellation_is_preserved_unchanged(during_read: bool) -> None:
    cancellation = asyncio.CancelledError("private-cancel-detail", {"code": 7})
    cancellation.add_note("private-cancel-note")
    if during_read:
        content = FakeContent(b"", chunks=[cancellation])
        response = FakeResponse(200, b"", content=content)
        session = FakeSession(response)
    else:
        response = None
        session = FakeSession(cancellation)
    client = SttClient(session, SttConfig(endpoint="http://localhost/stt"))

    with pytest.raises(asyncio.CancelledError) as raised:
        await client.transcribe_pcm(b"\0\0")

    assert raised.value is cancellation
    assert raised.value.args == ("private-cancel-detail", {"code": 7})
    assert raised.value.__notes__ == ["private-cancel-note"]
    assert len(session.requests) == 1
    if response is not None:
        assert response.release_calls == 1


@pytest.mark.parametrize(
    "release_failure",
    [
        InjectedBaseFailure("private-release-detail"),
        SystemExit("private-release-detail"),
        asyncio.CancelledError("private-release-detail"),
    ],
    ids=["base-exception", "system-exit", "cancellation"],
)
async def test_read_cancellation_wins_over_every_release_failure(
    release_failure: BaseException,
) -> None:
    cancellation = asyncio.CancelledError("private-cancel-detail", {"code": 7})
    cancellation.add_note("private-cancel-note")
    content = FakeContent(b"", chunks=[cancellation])
    response = FakeResponse(
        200,
        b"",
        content=content,
        release_error=release_failure,
    )
    session = FakeSession(response)
    client = SttClient(session, SttConfig(endpoint="http://localhost/stt"))

    with pytest.raises(asyncio.CancelledError) as raised:
        await client.transcribe_pcm(b"\0\0")

    assert raised.value is cancellation
    assert raised.value.args == ("private-cancel-detail", {"code": 7})
    assert raised.value.__notes__ == ["private-cancel-note"]
    assert len(session.requests) == 1
    assert session.queue == []
    assert response.release_calls == 1


async def test_loopback_gzip_bomb_is_rejected_without_auto_decompression() -> None:
    inflated_body = b'{"text":"' + b"x" * (MAX_STT_RESPONSE_BYTES * 256) + b'"}'
    compressed_body = gzip.compress(inflated_body, compresslevel=9)
    assert len(inflated_body) > MAX_STT_RESPONSE_BYTES * 250
    assert len(compressed_body) < MAX_STT_RESPONSE_BYTES
    receipts: list[str | None] = []

    async def compressed_bomb(request: web.Request) -> web.StreamResponse:
        receipts.append(request.headers.get("Accept-Encoding"))
        return web.Response(
            body=compressed_body,
            headers={
                "Content-Encoding": "gzip",
                "Content-Type": "application/json",
            },
        )

    app = web.Application()
    app.router.add_post("/transcribe", compressed_bomb)
    runner = web.AppRunner(app)
    await runner.setup()
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind(("127.0.0.1", 0))
    server_socket.setblocking(False)
    port = server_socket.getsockname()[1]
    site = web.SockSite(runner, server_socket)
    await site.start()

    timeout = ClientTimeout(total=2)
    try:
        async with ClientSession(
            timeout=timeout,
            auto_decompress=True,
        ) as session:
            client = SttClient(
                session,
                SttConfig(endpoint=f"http://127.0.0.1:{port}/transcribe"),
            )
            with pytest.raises(SttError) as raised:
                await client.transcribe_pcm(b"\0\0")
    finally:
        await runner.cleanup()

    assert receipts == ["identity"]
    assert_fixed_stt_error(raised.value)


async def test_loopback_multipart_auth_model_wav_and_redirect_policy() -> None:
    receipts: list[dict[str, Any]] = []
    redirect_target_receipts = 0

    async def transcribe(request: web.Request) -> web.StreamResponse:
        reader = await request.multipart()
        parts: dict[str, Any] = {}
        while part := await reader.next():
            if part.name == "file":
                parts["filename"] = part.filename
                parts["content_type"] = part.headers.get("Content-Type")
                parts["file"] = await part.read()
            else:
                parts[part.name] = await part.text()
        receipts.append(
            {
                "authorization": request.headers.get("Authorization"),
                "parts": parts,
            }
        )
        return web.json_response({"text": "кодовая фраза"})

    async def redirect(_request: web.Request) -> web.StreamResponse:
        receipts.append({"redirect": True})
        raise web.HTTPFound("/target")

    async def target(_request: web.Request) -> web.StreamResponse:
        nonlocal redirect_target_receipts
        redirect_target_receipts += 1
        return web.json_response({"text": "unsafe"})

    app = web.Application()
    app.router.add_post("/transcribe", transcribe)
    app.router.add_post("/redirect", redirect)
    app.router.add_post("/target", target)
    runner = web.AppRunner(app)
    await runner.setup()
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind(("127.0.0.1", 0))
    server_socket.setblocking(False)
    port = server_socket.getsockname()[1]
    site = web.SockSite(runner, server_socket)
    await site.start()

    token = f"loopback-{uuid4().hex}"
    pcm = struct.pack("<hhh", -32768, 0, 32767)
    timeout = ClientTimeout(total=2)
    try:
        async with ClientSession(timeout=timeout) as session:
            client = SttClient(
                session,
                SttConfig(
                    endpoint=f"http://127.0.0.1:{port}/transcribe",
                    token=token,
                    model="synthetic-model",
                ),
            )
            assert await client.transcribe_pcm(pcm) == "кодовая фраза"

            redirecting = SttClient(
                session,
                SttConfig(endpoint=f"http://127.0.0.1:{port}/redirect"),
            )
            with pytest.raises(SttError):
                await redirecting.transcribe_pcm(pcm)
    finally:
        await runner.cleanup()

    assert len(receipts) == 2
    successful = receipts[0]
    assert successful["authorization"] == f"Bearer {token}"
    assert successful["parts"]["filename"] == "audio.wav"
    assert successful["parts"]["content_type"] == "audio/wav"
    assert successful["parts"]["language"] == "ru"
    assert successful["parts"]["model"] == "synthetic-model"
    with wave.open(BytesIO(successful["parts"]["file"]), "rb") as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getframerate() == 16_000
        assert wav_file.readframes(3) == pcm
    assert redirect_target_receipts == 0
