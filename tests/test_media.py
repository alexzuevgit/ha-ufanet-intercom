"""Pure tests for account-discovered Ufanet media leases.

Synthetic values only; no test performs network I/O.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from custom_components.ufanet_intercom.media import (
    CameraBinding,
    GatewayError,
    MediaCredentials,
    UfanetMediaClient,
)

ALIAS = "a" * 64
OTHER_ALIAS = "b" * 64
CAMERA = "PRIVATE-CAMERA-NUMBER"
SECRET = "SIGNED-LIVE-TOKEN-SECRET"
CONTRACT = "PRIVATE-CONTRACT"
PASSWORD = "PRIVATE-PASSWORD"
PORTAL = "https://media.region.ufanet.ru"


def credentials() -> MediaCredentials:
    return MediaCredentials(CONTRACT, PASSWORD)


def response_json(status: int, value: object, **kwargs: object) -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps(value, separators=(",", ":"), allow_nan=False).encode(),
        headers={"content-type": "application/json"},
        **kwargs,
    )


def successful_transport(
    requests: list[httpx.Request],
    *,
    portal_statuses: list[int] | None = None,
) -> httpx.MockTransport:
    pending_statuses = list(portal_statuses or [200])

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/api/v1/auth/auth_by_contract/":
            assert request.url.host == "dom.ufanet.ru"
            body = json.loads(request.content)
            assert body == {"contract": CONTRACT, "password": PASSWORD}
            return response_json(
                200,
                {
                    "token": {
                        "access": "aaa.bbb.ccc",
                        "refresh": "ddd.eee.fff",
                        "exp": 9999999999,
                    }
                },
            )
        if path == "/api/v0/contract/":
            assert request.headers["authorization"] == "JWT aaa.bbb.ccc"
            return response_json(
                200,
                [
                    {
                        "title": "OTHER-CONTRACT",
                        "isp_org": {
                            "cams_server": {
                                "url": "https://other.region.ufanet.ru",
                                "is_active": True,
                            }
                        },
                    },
                    {
                        "title": CONTRACT.lower(),
                        "isp_org": {"cams_server": {"url": PORTAL, "is_active": True}},
                    },
                ],
            )
        if path == "/api/internal/login/":
            assert request.url.host == "media.region.ufanet.ru"
            body = request.content.decode()
            assert "PRIVATE-CONTRACT" in body and "PRIVATE-PASSWORD" in body
            return httpx.Response(
                302,
                headers={
                    "location": "/main/",
                    "set-cookie": "sessionid=PRIVATE; Path=/; Secure; HttpOnly",
                },
            )
        if path == "/api/v0/cameras/this/":
            status = pending_statuses.pop(0) if pending_statuses else 200
            if status != 200:
                return httpx.Response(status)
            assert request.headers.get("cookie") == "sessionid=PRIVATE"
            body = json.loads(request.content)
            assert body["numbers"] == [CAMERA]
            assert body["token_l_ttl"] == 300
            assert body["token_r_ttl"] == 300
            return response_json(
                200,
                {
                    "count": 1,
                    "results": [
                        {
                            "number": CAMERA,
                            "server": {"domain": "video.cams.ufanet.ru"},
                            "token_l": SECRET,
                        }
                    ],
                },
            )
        raise AssertionError(f"unexpected path: {path}")

    return httpx.MockTransport(handler)


def test_account_origin_portal_login_and_lease_are_dynamic_and_redacted() -> None:
    requests: list[httpx.Request] = []
    with UfanetMediaClient(
        credentials(), transport=successful_transport(requests), monotonic=lambda: 50.0
    ) as client:
        binding = CameraBinding(ALIAS, CAMERA)
        lease = client.get_lease(binding)
        assert lease.alias == ALIAS
        assert lease.server_host == "video.cams.ufanet.ru"
        assert lease.expires_monotonic == 350.0
        rendered = repr(credentials()) + repr(binding) + repr(lease) + repr(client)
        assert CONTRACT not in rendered
        assert PASSWORD not in rendered
        assert CAMERA not in rendered
        assert SECRET not in rendered
        assert PORTAL not in rendered

    assert [request.url.path for request in requests] == [
        "/api/v1/auth/auth_by_contract/",
        "/api/v0/contract/",
        "/api/internal/login/",
        "/api/v0/cameras/this/",
    ]
    assert all("open" not in request.url.path for request in requests)


def test_lease_cache_avoids_reauthentication_and_refreshes_near_expiry() -> None:
    requests: list[httpx.Request] = []
    now = [10.0]
    with UfanetMediaClient(
        credentials(),
        transport=successful_transport(requests),
        monotonic=lambda: now[0],
    ) as client:
        binding = CameraBinding(ALIAS, CAMERA)
        assert client.get_lease(binding) is client.get_lease(binding)
        assert [request.url.path for request in requests].count(
            "/api/v0/cameras/this/"
        ) == 1
        now[0] = 260.0
        client.get_lease(binding)
        assert [request.url.path for request in requests].count(
            "/api/v0/cameras/this/"
        ) == 2
        assert [request.url.path for request in requests].count(
            "/api/internal/login/"
        ) == 1


def test_expired_portal_auth_is_retried_once_without_mobile_relogin() -> None:
    requests: list[httpx.Request] = []
    with UfanetMediaClient(
        credentials(),
        transport=successful_transport(requests, portal_statuses=[401, 200]),
    ) as client:
        client.get_lease(CameraBinding(ALIAS, CAMERA))
    paths = [request.url.path for request in requests]
    assert paths.count("/api/internal/login/") == 2
    assert paths.count("/api/v0/cameras/this/") == 2
    assert paths.count("/api/v1/auth/auth_by_contract/") == 1


@pytest.mark.parametrize(
    "profile",
    [
        [],
        [{"title": CONTRACT, "isp_org": {"cams_server": None}}],
        [
            {
                "title": CONTRACT,
                "isp_org": {
                    "cams_server": {
                        "url": "http://media.region.ufanet.ru",
                        "is_active": True,
                    }
                },
            }
        ],
        [
            {
                "title": CONTRACT,
                "isp_org": {
                    "cams_server": {"url": "https://evil.example", "is_active": True}
                },
            }
        ],
        [
            {
                "title": CONTRACT,
                "isp_org": {"cams_server": {"url": PORTAL, "is_active": False}},
            }
        ],
        [
            {
                "title": CONTRACT,
                "isp_org": {"cams_server": {"url": PORTAL, "is_active": True}},
            },
            {
                "title": CONTRACT.lower(),
                "isp_org": {"cams_server": {"url": PORTAL, "is_active": True}},
            },
        ],
    ],
)
def test_invalid_or_ambiguous_account_media_origin_fails_closed(
    profile: object,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/auth_by_contract/":
            return response_json(
                200,
                {
                    "token": {
                        "access": "aaa.bbb.ccc",
                        "refresh": "ddd.eee.fff",
                        "exp": 9999999999,
                    }
                },
            )
        if request.url.path == "/api/v0/contract/":
            return response_json(200, profile)
        raise AssertionError("invalid profile must fail before portal login")

    with (
        UfanetMediaClient(
            credentials(), transport=httpx.MockTransport(handler)
        ) as client,
        pytest.raises(GatewayError, match=r"^media account metadata is invalid$"),
    ):
        client.get_lease(CameraBinding(ALIAS, CAMERA))


@pytest.mark.parametrize(
    "factory",
    [
        lambda: CameraBinding("not-an-opaque-key", CAMERA),
        lambda: CameraBinding(ALIAS, ""),
        lambda: CameraBinding(ALIAS, "camera/escape"),
    ],
)
def test_invalid_binding_is_rejected_before_network(
    factory: Callable[[], object],
) -> None:
    calls: list[httpx.Request] = []
    with (
        UfanetMediaClient(
            credentials(),
            transport=httpx.MockTransport(lambda request: calls.append(request)),
        ),
        pytest.raises(GatewayError),
    ):
        factory()
    assert calls == []


def test_mismatched_or_hostile_lease_metadata_is_fixed_detail() -> None:
    requests: list[httpx.Request] = []
    base = successful_transport(requests)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/v0/cameras/this/":
            return base.handle_request(request)
        return response_json(
            200,
            {
                "count": 1,
                "results": [
                    {
                        "number": CAMERA,
                        "server": {"domain": "evil.example"},
                        "token_l": SECRET,
                    }
                ],
            },
        )

    with (
        UfanetMediaClient(
            credentials(), transport=httpx.MockTransport(handler)
        ) as client,
        pytest.raises(GatewayError, match=r"^media metadata is invalid$") as caught,
    ):
        client.get_lease(CameraBinding(ALIAS, CAMERA))
    assert CAMERA not in str(caught.value)
    assert SECRET not in str(caught.value)
