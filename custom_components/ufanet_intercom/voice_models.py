"""Admin-requested, bounded model metadata discovery; never starts audio work."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import urlsplit, urlunsplit

from aiohttp import ClientSession, ClientTimeout, DummyCookieJar

from .voice_stt import (
    MAX_MODEL_CHARS,
    SttConfig,
    SttError,
    _decode_json_object,
    _valid_response_content_encoding,
)

MAX_MODEL_RESPONSE_BYTES: Final = 256 * 1024
MAX_MODEL_ENTRIES: Final = 256
MODEL_DISCOVERY_TIMEOUT: Final = 10
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", re.ASCII)
_OPENAI_TRANSCRIPTION_ID = re.compile(
    r"(?:whisper-1|gpt-4o-transcribe(?:-diarize)?|gpt-4o-mini-transcribe)"
    r"(?:-[0-9]{4}-[0-9]{2}-[0-9]{2})?"
)


@dataclass(frozen=True, slots=True)
class ModelCatalog:
    """Only bounded IDs for the administrator form and fixed public error codes."""

    models: tuple[str, ...] = field(default=(), repr=False)
    error: str | None = None


def valid_model_id(value: object) -> bool:
    """Accept an exact explicit ID, never trim, coerce, or resolve default aliases."""
    return (
        type(value) is str
        and 0 < len(value) <= MAX_MODEL_CHARS
        and value.casefold() != "default"
        and _MODEL_ID.fullmatch(value) is not None
    )


def endpoint_origin(endpoint: str) -> tuple[str, str | None, int]:
    """Compare already validated URLs by scheme, host and effective port."""
    parsed = urlsplit(endpoint)
    return (
        parsed.scheme,
        parsed.hostname,
        parsed.port
        if parsed.port is not None
        else (443 if parsed.scheme == "https" else 80),
    )


def catalog_url(config: SttConfig) -> str | None:
    """Derive only the known sibling path, preserving its origin and base prefix."""
    parsed = urlsplit(config.endpoint)
    suffix = "/audio/transcriptions"
    if (
        not parsed.path.endswith(suffix)
        or "%" in parsed.path
        or "//" in parsed.path
        or any(part in (".", "..") for part in parsed.path.split("/"))
    ):
        return None
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path[: -len(suffix)] + "/models", "", "")
    )


def parse_model_catalog(body: bytes, *, openai: bool) -> tuple[str, ...]:
    """Read bounded exact IDs; generic catalogs do not prove STT compatibility."""
    if type(body) is not bytes or len(body) > MAX_MODEL_RESPONSE_BYTES:
        raise ValueError("Invalid model catalog.")
    try:
        payload = _decode_json_object(body)
    except SttError:
        raise ValueError("Invalid model catalog.") from None
    data = payload.get("data")
    if type(data) is not list or len(data) > MAX_MODEL_ENTRIES:
        raise ValueError("Invalid model catalog.")
    return tuple(
        sorted(
            {
                item["id"]
                for item in data
                if type(item) is dict
                and valid_model_id(item.get("id"))
                and (
                    not openai
                    or _OPENAI_TRANSCRIPTION_ID.fullmatch(item["id"]) is not None
                )
            }
        )
    )


async def async_discover_models(config: SttConfig) -> ModelCatalog:
    """One explicit UI GET, isolated Bearer, no retry/redirect, fixed-detail failure.

    The session and response are owned here, including on timeout/cancellation.
    Unsupported custom routes require manual entry and create no session at all.
    """
    url = catalog_url(config)
    if url is None:
        return ModelCatalog(error="models_unsupported")
    headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
    if config.token:
        headers["Authorization"] = f"Bearer {config.token}"
    try:
        async with asyncio.timeout(MODEL_DISCOVERY_TIMEOUT):
            async with ClientSession(
                timeout=ClientTimeout(total=MODEL_DISCOVERY_TIMEOUT, ceil_threshold=11),
                cookie_jar=DummyCookieJar(),
                trust_env=False,
                middlewares=(),
            ) as session:
                # aiohttp retries idempotent GETs after a dropped connection by default.
                if type(getattr(session, "_retry_connection", None)) is not bool:
                    return ModelCatalog(error="models_unavailable")
                session._retry_connection = False
                response = await session.get(
                    url, headers=headers, allow_redirects=False, auto_decompress=False
                )
                try:
                    if response.status != 200 or not _valid_response_content_encoding(
                        response.headers
                    ):
                        return ModelCatalog(error="models_unavailable")
                    length = response.headers.get("Content-Length")
                    if length is not None and (
                        not length.isascii()
                        or not length.isdecimal()
                        or len(length) > 20
                        or int(length) > MAX_MODEL_RESPONSE_BYTES
                    ):
                        return ModelCatalog(error="models_unavailable")
                    body = bytearray()
                    while True:
                        chunk = await response.content.read(
                            min(16384, MAX_MODEL_RESPONSE_BYTES + 1 - len(body))
                        )
                        if not chunk:
                            break
                        if len(body) + len(chunk) > MAX_MODEL_RESPONSE_BYTES:
                            return ModelCatalog(error="models_unavailable")
                        body.extend(chunk)
                    models = parse_model_catalog(
                        bytes(body),
                        openai=endpoint_origin(config.endpoint)
                        == ("https", "api.openai.com", 443),
                    )
                    return ModelCatalog(models, None if models else "models_empty")
                finally:
                    response.release()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - no endpoint, token, body or exception escapes
        return ModelCatalog(error="models_unavailable")
