"""Strict bounded client for OpenAI-compatible external speech-to-text APIs.

This module is independent of Home Assistant. Callers own the supplied HTTP session,
including its finite total timeout. Audio, transcripts, and provider error details are
kept only in memory and are never logged or persisted here.
"""

from __future__ import annotations

import asyncio
import json
import re
import wave
from collections.abc import Mapping
from dataclasses import dataclass, field
from io import BytesIO
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address
from typing import Any, Final, Protocol
from urllib.parse import urlsplit

from aiohttp import FormData

PCM_SAMPLE_RATE_HZ: Final = 16_000
PCM_CHANNELS: Final = 1
PCM_SAMPLE_WIDTH_BYTES: Final = 2
PCM_BYTE_ORDER: Final = "little"
PCM_SIGNED: Final = True
MAX_PCM_SECONDS: Final = 8
MAX_PCM_BYTES: Final = (
    PCM_SAMPLE_RATE_HZ * PCM_CHANNELS * PCM_SAMPLE_WIDTH_BYTES * MAX_PCM_SECONDS
)

MAX_STT_RESPONSE_BYTES: Final = 64 * 1024
MAX_TRANSCRIPT_UTF8_BYTES: Final = 4096
MAX_ENDPOINT_CHARS: Final = 2048
MAX_TOKEN_CHARS: Final = 2048
MAX_MODEL_CHARS: Final = 128
STT_ERROR_MESSAGE: Final = "Speech transcription failed."

_CONFIG_ERROR_MESSAGE: Final = "Invalid STT configuration."
_PCM_ERROR_MESSAGE: Final = "Invalid PCM audio."
_RESPONSE_READ_CHUNK_BYTES: Final = 16 * 1024
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_HTTP_LOOPBACK_IPV4: Final = IPv4Network("127.0.0.0/8")
_HTTP_LOOPBACK_IPV6: Final = IPv6Network("::1/128")
_HTTP_OPT_IN_IPV4: Final = (
    IPv4Network("10.0.0.0/8"),
    IPv4Network("172.16.0.0/12"),
    IPv4Network("192.168.0.0/16"),
)
_HTTP_OPT_IN_IPV6: Final = (
    IPv6Network("fc00::/7"),
    IPv6Network("fe80::/10"),
)


class _ResponseContent(Protocol):
    async def read(self, limit: int = -1) -> bytes: ...


class _Response(Protocol):
    status: int
    headers: Mapping[str, str]
    content: _ResponseContent

    def release(self) -> None: ...


class _Session(Protocol):
    async def post(self, url: str, **kwargs: Any) -> _Response: ...


@dataclass(frozen=True, slots=True, repr=False)
class SttConfig:
    """Validated external STT endpoint and secret request options."""

    endpoint: str = field(repr=False)
    token: str = field(default="", repr=False)
    model: str = field(default="", repr=False)
    allow_insecure_http: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.token) is not str
            or len(self.token) > MAX_TOKEN_CHARS
            or type(self.model) is not str
            or len(self.model) > MAX_MODEL_CHARS
            or type(self.allow_insecure_http) is not bool
            or not _valid_endpoint(
                self.endpoint,
                token=self.token,
                allow_insecure_http=self.allow_insecure_http,
            )
        ):
            raise ValueError(_CONFIG_ERROR_MESSAGE)

    def __repr__(self) -> str:
        """Return no endpoint, token, or model detail."""
        return "SttConfig()"


class SttError(RuntimeError):
    """Fixed-detail failure safe to expose outside this module."""

    def __init__(self, *_ignored: object) -> None:
        super().__init__(STT_ERROR_MESSAGE)
        _sanitize_stt_error(self)


class SttClient:
    """One-shot, no-retry client using a caller-owned bounded HTTP session."""

    __slots__ = ("_config", "_session")

    def __init__(self, session: _Session, config: SttConfig) -> None:
        if type(config) is not SttConfig:
            raise TypeError("config must be exact SttConfig")
        self._session = session
        self._config = config

    def __repr__(self) -> str:
        """Avoid rendering the retained secret configuration."""
        return "SttClient()"

    async def transcribe_pcm(self, pcm: object) -> str:
        """Transcribe one validated PCM utterance or raise a sanitized error."""
        try:
            return await self._transcribe_pcm(pcm)
        except asyncio.CancelledError:
            raise
        except BaseException:  # noqa: BLE001 - this is the public redaction boundary
            error = SttError()

        # First raise may attach an unrelated exception active in the caller.
        # Catch it locally, clear that context, then bare-raise without reattachment.
        try:
            raise error
        except SttError as final_error:
            _sanitize_stt_error(final_error)
            raise

    async def _transcribe_pcm(self, pcm: object) -> str:
        wav = pcm_to_wav(pcm)
        form = FormData()
        form.add_field(
            "file",
            wav,
            filename="audio.wav",
            content_type="audio/wav",
        )
        form.add_field("language", "ru")
        if self._config.model:
            form.add_field("model", self._config.model)

        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        if self._config.token:
            headers["Authorization"] = f"Bearer {self._config.token}"

        response = await self._session.post(
            self._config.endpoint,
            data=form,
            headers=headers,
            allow_redirects=False,
            auto_decompress=False,
        )
        cancellation: asyncio.CancelledError | None = None
        try:
            if type(response.status) is not int or response.status != 200:
                raise SttError()
            if not _valid_response_content_encoding(response.headers):
                raise SttError()
            body = await _read_bounded_response(response)
            payload = _decode_json_object(body)
            text = payload.get("text")
            if type(text) is not str:
                raise SttError()
            try:
                encoded_length = len(text.encode("utf-8", errors="strict"))
            except UnicodeEncodeError:
                raise SttError() from None
            if encoded_length > MAX_TRANSCRIPT_UTF8_BYTES:
                raise SttError()
            return text
        except asyncio.CancelledError as error:
            cancellation = error
            raise
        finally:
            try:
                response.release()
            except BaseException:
                if cancellation is None:
                    raise


def _valid_endpoint(
    value: object,
    *,
    token: str,
    allow_insecure_http: bool,
) -> bool:
    if (
        type(value) is not str
        or not value
        or len(value) > MAX_ENDPOINT_CHARS
        or "?" in value
        or "#" in value
        or "\\" in value
        or _INVALID_PERCENT_ESCAPE.search(value) is not None
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        _port = parsed.port
    except (TypeError, ValueError):
        return False
    if not (
        parsed.scheme in ("http", "https")
        and parsed.netloc
        and type(host) is str
        and host
        and parsed.username is None
        and parsed.password is None
        and "@" not in parsed.netloc
        and not parsed.query
        and not parsed.fragment
    ):
        return False
    if parsed.scheme == "https":
        return True
    return _valid_plain_http_target(host, token, allow_insecure_http)


def _valid_plain_http_target(
    host: str,
    token: str,
    allow_insecure_http: bool,
) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        address = ip_address(host)
    except ValueError:
        return False

    if isinstance(address, IPv4Address):
        if address in _HTTP_LOOPBACK_IPV4:
            return True
        opt_in_networks = _HTTP_OPT_IN_IPV4
    elif isinstance(address, IPv6Address):
        if address in _HTTP_LOOPBACK_IPV6:
            return True
        opt_in_networks = _HTTP_OPT_IN_IPV6
    else:  # pragma: no cover - ip_address currently returns only these exact families
        return False
    return (
        allow_insecure_http
        and not token
        and any(address in network for network in opt_in_networks)
    )


def pcm_to_wav(pcm: object) -> bytes:
    """Wrap exact bounded 16 kHz mono signed little-endian PCM in a WAV file."""
    if (
        type(pcm) is not bytes
        or not pcm
        or len(pcm) % PCM_SAMPLE_WIDTH_BYTES != 0
        or len(pcm) > MAX_PCM_BYTES
    ):
        raise ValueError(_PCM_ERROR_MESSAGE)

    output = BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(PCM_CHANNELS)
        wav_file.setsampwidth(PCM_SAMPLE_WIDTH_BYTES)
        wav_file.setframerate(PCM_SAMPLE_RATE_HZ)
        wav_file.writeframes(pcm)
    return output.getvalue()


def _valid_response_content_encoding(headers: Mapping[str, str]) -> bool:
    content_encodings: list[str] = []
    for name, value in headers.items():
        if not isinstance(name, str):
            return False
        if name.casefold() == "content-encoding":
            if type(value) is not str:
                return False
            content_encodings.append(value)
    return not content_encodings or (
        len(content_encodings) == 1 and content_encodings[0].casefold() == "identity"
    )


async def _read_bounded_response(response: _Response) -> bytes:
    declared_length = response.headers.get("Content-Length")
    if declared_length is not None and (
        type(declared_length) is not str
        or not declared_length
        or not declared_length.isascii()
        or not declared_length.isdecimal()
        or len(declared_length) > 20
        or int(declared_length) > MAX_STT_RESPONSE_BYTES
    ):
        raise SttError()

    chunks: list[bytes] = []
    total = 0
    while True:
        remaining_with_sentinel = MAX_STT_RESPONSE_BYTES + 1 - total
        chunk = await response.content.read(
            min(_RESPONSE_READ_CHUNK_BYTES, remaining_with_sentinel)
        )
        if type(chunk) is not bytes:
            raise SttError()
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_STT_RESPONSE_BYTES:
            raise SttError()
        chunks.append(chunk)
    return b"".join(chunks)


def _reject_json_constant(_value: str) -> None:
    raise ValueError


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _decode_json_object(body: bytes) -> dict[str, object]:
    try:
        payload = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError, TypeError):
        raise SttError() from None
    if type(payload) is not dict:
        raise SttError()
    return payload


def _sanitize_stt_error(error: SttError) -> None:
    error.args = (STT_ERROR_MESSAGE,)
    error.__cause__ = None
    error.__context__ = None
    error.__notes__ = []
