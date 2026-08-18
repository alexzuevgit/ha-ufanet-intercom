"""Account-discovered, token-redacting Ufanet camera lease client."""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import urlsplit

import httpx

from .const import AUTH_PATH, BASE_URL, MAX_CONTRACT_CHARS, USER_AGENT

CONTRACT_PROFILE_PATH: Final = "/api/v0/contract/"
_LEASE_PATH: Final = "/api/v0/cameras/this/"
_PORTAL_LOGIN_PATH: Final = "/api/internal/login/"
_MAX_AUTH_BYTES: Final = 32 * 1024
_MAX_PROFILE_BYTES: Final = 256 * 1024
_MAX_MEDIA_BYTES: Final = 64 * 1024
_MAX_PROFILE_ITEMS: Final = 32
_MAX_TOKEN_CHARS: Final = 8192
_MAX_CAMERA_CHARS: Final = 256
_LEASE_TTL_SECONDS: Final = 300
_LEASE_REFRESH_MARGIN_SECONDS: Final = 60
_ALIAS_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_CAMERA_RE: Final = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_JWT_RE: Final = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
_HOST_RE: Final = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
_CAMERA_FIELDS: Final = [
    "address",
    "is_public",
    "number",
    "server",
    "token_l",
    "token_r",
]


class GatewayError(RuntimeError):
    """Fixed-detail fail-closed media error."""


@dataclass(frozen=True, slots=True, repr=False)
class MediaCredentials:
    """One account's credentials, deliberately excluded from representations."""

    contract: str = field(repr=False)
    password: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.contract) is not str
            or not self.contract
            or len(self.contract) > MAX_CONTRACT_CHARS
            or self.contract != self.contract.strip()
            or type(self.password) is not str
            or not self.password
            or len(self.password) > 512
            or self.password != self.password.strip()
        ):
            raise GatewayError("media credentials are invalid")

    def __repr__(self) -> str:
        return "MediaCredentials()"


@dataclass(frozen=True, slots=True)
class CameraBinding:
    """An opaque account-local target bound to one private provider camera."""

    alias: str
    number: str = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.alias) is not str or _ALIAS_RE.fullmatch(self.alias) is None:
            raise GatewayError("camera binding is invalid")
        if type(self.number) is not str or _CAMERA_RE.fullmatch(self.number) is None:
            raise GatewayError("camera binding is invalid")


@dataclass(frozen=True, slots=True)
class MediaLease:
    """A short-lived signed media route whose secret fields never render."""

    alias: str
    camera_number: str = field(repr=False)
    server_host: str = field(repr=False)
    token: str = field(repr=False)
    expires_monotonic: float

    def __post_init__(self) -> None:
        if type(self.alias) is not str or _ALIAS_RE.fullmatch(self.alias) is None:
            raise GatewayError("media metadata is invalid")
        if (
            type(self.camera_number) is not str
            or _CAMERA_RE.fullmatch(self.camera_number) is None
            or not _valid_media_host(self.server_host)
            or type(self.token) is not str
            or not self.token
            or self.token != self.token.strip()
            or len(self.token) > 4096
            or type(self.expires_monotonic) is not float
            or self.expires_monotonic <= 0
        ):
            raise GatewayError("media metadata is invalid")

    def __repr__(self) -> str:
        return (
            f"MediaLease(alias={self.alias!r}, "
            f"expires_monotonic={self.expires_monotonic!r})"
        )


def _valid_media_host(host: object) -> bool:
    return bool(
        type(host) is str
        and _HOST_RE.fullmatch(host) is not None
        and ".." not in host
        and not host.startswith(".")
        and not host.endswith(".")
        and (
            host == "ucams.ufanet.ru"
            or host.endswith(".ucams.ufanet.ru")
            or host == "cams.ufanet.ru"
            or host.endswith(".cams.ufanet.ru")
        )
    )


def _provider_origin(value: object) -> str:
    if type(value) is not str or len(value) > 2048:
        raise GatewayError("media account metadata is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise GatewayError("media account metadata is invalid") from None
    host = parsed.hostname
    if (
        parsed.scheme != "https"
        or type(host) is not str
        or _HOST_RE.fullmatch(host) is None
        or host == "ufanet.ru"
        or not host.endswith(".ufanet.ru")
        or ".." in host
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise GatewayError("media account metadata is invalid")
    return f"https://{host}"


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _decode_json(data: bytes, message: str) -> object:
    try:
        return json.loads(
            data.decode("utf-8", "strict"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError):
        raise GatewayError(message) from None


def _stream_json(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    max_bytes: int,
    message: str,
    **kwargs: Any,
) -> tuple[int, object]:
    try:
        with client.stream(method, url, **kwargs) as response:
            if response.status_code != 200:
                return response.status_code, None
            media_type = (
                response.headers.get("content-type", "").split(";", 1)[0].lower()
            )
            if media_type != "application/json":
                raise GatewayError(message)
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise GatewayError(message)
                chunks.append(chunk)
            if not chunks:
                raise GatewayError(message)
            return response.status_code, _decode_json(b"".join(chunks), message)
    except httpx.HTTPError:
        raise GatewayError(message) from None


class UfanetMediaClient:
    """Discover the account media origin and issue one-camera live leases."""

    def __init__(
        self,
        credentials: MediaCredentials,
        *,
        transport: httpx.BaseTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if type(credentials) is not MediaCredentials:
            raise TypeError("credentials must be exact MediaCredentials")
        if not callable(monotonic):
            raise TypeError("monotonic must be callable")
        self._credentials = credentials
        self._transport = transport
        self._monotonic = monotonic
        self._mobile = httpx.Client(
            base_url=BASE_URL,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )
        self._portal: httpx.Client | None = None
        self._portal_origin: str | None = None
        self._portal_logged_in = False
        self._leases: dict[str, MediaLease] = {}
        self._lock = threading.RLock()
        self._closed = False

    def __enter__(self) -> UfanetMediaClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __repr__(self) -> str:
        with self._lock:
            return (
                "UfanetMediaClient("
                f"origin_discovered={self._portal_origin is not None}, "
                f"logged_in={self._portal_logged_in}, closed={self._closed})"
            )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            portal = self._portal
            self._portal = None
            self._portal_origin = None
            self._portal_logged_in = False
            self._leases.clear()
        if portal is not None:
            portal.cookies.clear()
            portal.close()
        self._mobile.close()

    def _discover_origin_locked(self) -> str:
        if self._portal_origin is not None:
            return self._portal_origin
        status, payload = _stream_json(
            self._mobile,
            "POST",
            AUTH_PATH,
            max_bytes=_MAX_AUTH_BYTES,
            message="media authentication failed",
            json={
                "contract": self._credentials.contract.upper(),
                "password": self._credentials.password,
            },
        )
        if status != 200 or type(payload) is not dict or set(payload) != {"token"}:
            raise GatewayError("media authentication failed")
        token = payload.get("token")
        if type(token) is not dict:
            raise GatewayError("media authentication failed")
        access = token.get("access")
        if (
            type(access) is not str
            or len(access) > _MAX_TOKEN_CHARS
            or _JWT_RE.fullmatch(access.removeprefix("JWT ")) is None
        ):
            raise GatewayError("media authentication failed")
        status, profile = _stream_json(
            self._mobile,
            "GET",
            CONTRACT_PROFILE_PATH,
            max_bytes=_MAX_PROFILE_BYTES,
            message="media account metadata is invalid",
            headers={"Authorization": "JWT " + access.removeprefix("JWT ")},
        )
        access = ""
        if (
            status != 200
            or type(profile) is not list
            or not 1 <= len(profile) <= _MAX_PROFILE_ITEMS
        ):
            raise GatewayError("media account metadata is invalid")
        matches = [
            item
            for item in profile
            if type(item) is dict
            and type(item.get("title")) is str
            and item["title"].casefold() == self._credentials.contract.casefold()
        ]
        if len(matches) != 1:
            raise GatewayError("media account metadata is invalid")
        isp_org = matches[0].get("isp_org")
        cams_server = isp_org.get("cams_server") if type(isp_org) is dict else None
        if type(cams_server) is not dict or cams_server.get("is_active") is not True:
            raise GatewayError("media account metadata is invalid")
        origin = _provider_origin(cams_server.get("url"))
        self._portal_origin = origin
        self._portal = httpx.Client(
            base_url=origin,
            headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
            follow_redirects=False,
            trust_env=False,
            transport=self._transport,
        )
        return origin

    def _login_portal_locked(self) -> None:
        self._discover_origin_locked()
        portal = self._portal
        if portal is None:
            raise GatewayError("portal authentication failed")
        try:
            with portal.stream(
                "POST",
                _PORTAL_LOGIN_PATH,
                params={"next": "/main/"},
                data={
                    "username": self._credentials.contract,
                    "password": self._credentials.password,
                },
            ) as response:
                status = response.status_code
        except httpx.HTTPError:
            raise GatewayError("portal authentication failed") from None
        if status not in {302, 303} or not portal.cookies:
            raise GatewayError("portal authentication failed")
        self._portal_logged_in = True

    def _fetch_lease_locked(self, binding: CameraBinding) -> MediaLease:
        if not self._portal_logged_in:
            self._login_portal_locked()
        portal = self._portal
        if portal is None:
            raise GatewayError("media metadata request failed")
        request_body = {
            "fields": _CAMERA_FIELDS,
            "numbers": [binding.number],
            "token_l_ttl": _LEASE_TTL_SECONDS,
            "token_r_ttl": _LEASE_TTL_SECONDS,
            "get_motion_zones": False,
            "get_links": False,
        }
        status, payload = _stream_json(
            portal,
            "POST",
            _LEASE_PATH,
            max_bytes=_MAX_MEDIA_BYTES,
            message="media metadata request failed",
            json=request_body,
        )
        if status in {401, 403}:
            self._portal_logged_in = False
            portal.cookies.clear()
            self._login_portal_locked()
            status, payload = _stream_json(
                portal,
                "POST",
                _LEASE_PATH,
                max_bytes=_MAX_MEDIA_BYTES,
                message="media metadata request failed",
                json=request_body,
            )
        if status != 200:
            raise GatewayError("media metadata request failed")
        if type(payload) is not dict or type(payload.get("results")) is not list:
            raise GatewayError("media metadata is invalid")
        results = payload["results"]
        if len(results) != 1 or type(results[0]) is not dict:
            raise GatewayError("media metadata is invalid")
        item = results[0]
        if item.get("number") != binding.number or type(item.get("server")) is not dict:
            raise GatewayError("media metadata is invalid")
        now = float(self._monotonic())  # type: ignore[operator]
        return MediaLease(
            alias=binding.alias,
            camera_number=binding.number,
            server_host=item["server"].get("domain"),
            token=item.get("token_l"),
            expires_monotonic=now + _LEASE_TTL_SECONDS,
        )

    def get_lease(self, binding: CameraBinding) -> MediaLease:
        """Return one fresh live lease for an exact opaque binding."""

        if type(binding) is not CameraBinding:
            raise GatewayError("camera binding is invalid")
        with self._lock:
            if self._closed:
                raise GatewayError("media client is closed")
            now = float(self._monotonic())  # type: ignore[operator]
            cached = self._leases.get(binding.alias)
            if (
                cached is not None
                and cached.camera_number == binding.number
                and cached.expires_monotonic > now + _LEASE_REFRESH_MARGIN_SECONDS
            ):
                return cached
            lease = self._fetch_lease_locked(binding)
            self._leases[binding.alias] = lease
            return lease
