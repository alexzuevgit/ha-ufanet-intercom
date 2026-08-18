from __future__ import annotations

import socket
import threading
from dataclasses import dataclass

import pytest

from custom_components.ufanet_intercom.media import (
    CameraBinding,
    GatewayError,
    MediaLease,
)
from custom_components.ufanet_intercom.rtsp_proxy import (
    RtspGateway,
    _FrameReader,
    _MAX_CLIENTS,
    _public_socket_addresses,
    _rewrite_request,
    _rewrite_response,
    build_rtsp_server,
)

SECRET = "SIGNED-LIVE-TOKEN-SECRET"
CAMERA = "PRIVATE-CAMERA-NUMBER"
LOCAL = "rtsp://127.0.0.1:18092/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
UPSTREAM = (
    "rtsp://video.ucams.ufanet.ru/" + CAMERA + "?token=" + SECRET + "&tracks=v1a1"
)


@dataclass
class FakePortal:
    calls: int = 0

    def get_lease(self, binding: CameraBinding) -> MediaLease:
        self.calls += 1
        return MediaLease(
            alias=binding.alias,
            camera_number=binding.number,
            server_host="video.ucams.ufanet.ru",
            token=SECRET,
            expires_monotonic=1000.0,
        )


def message(first: str, headers: list[str] | None = None, body: bytes = b"") -> bytes:
    values = list(headers or [])
    if body:
        values.append(f"Content-Length: {len(body)}")
    return (first + "\r\n" + "\r\n".join(values) + "\r\n\r\n").encode() + body


def receive_message(sock: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(8192)
        if not chunk:
            return data
        data += chunk
    header, marker, rest = data.partition(b"\r\n\r\n")
    length = 0
    for line in header.split(b"\r\n")[1:]:
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
    while len(rest) < length:
        rest += sock.recv(8192)
    return header + marker + rest[:length]


def test_request_rewrite_uses_exact_media3_literal_control_suffix() -> None:
    request = message(
        f"SETUP {LOCAL}/streamid=0 RTSP/1.0",
        ["CSeq: 2", "Transport: RTP/AVP/TCP;unicast;interleaved=0-1"],
    )
    rewritten = _rewrite_request(request, local_base=LOCAL, upstream_base=UPSTREAM)
    assert rewritten.startswith(f"SETUP {UPSTREAM}/streamid=0 RTSP/1.0".encode())
    assert b"RTP/AVP/TCP" in rewritten


def test_complete_oversized_rtsp_header_is_rejected() -> None:
    reader = _FrameReader()
    reader._buffer.extend(
        b"OPTIONS "
        + LOCAL.encode()
        + b" RTSP/1.0\r\nX-Fill: "
        + (b"A" * (64 * 1024))
        + b"\r\n\r\n"
    )
    with pytest.raises(GatewayError, match="RTSP frame is invalid"):
        reader._pop()


@pytest.mark.parametrize(
    "wire_request",
    [
        message(f"RECORD {LOCAL} RTSP/1.0", ["CSeq: 1"]),
        message(
            f"SETUP {LOCAL}/streamid=0 RTSP/1.0",
            ["CSeq: 2", "Transport: RTP/AVP;unicast;client_port=5000-5001"],
        ),
        message(
            f"SETUP {LOCAL}/streamid=0 RTSP/1.0",
            [
                "CSeq: 2",
                (
                    "Transport: RTP/AVP;unicast;client_port=5000-5001,"
                    "RTP/AVP/TCP;unicast;interleaved=0-1"
                ),
            ],
        ),
        message(
            f"SETUP {LOCAL}/streamid=2 RTSP/1.0",
            ["CSeq: 2", "Transport: RTP/AVP/TCP;unicast;interleaved=0-1"],
        ),
        message(
            f"SETUP {LOCAL} RTSP/1.0",
            ["CSeq: 2", "Transport: RTP/AVP/TCP;unicast;interleaved=0-1"],
        ),
        message("DESCRIBE rtsp://evil.example/x RTSP/1.0", ["CSeq: 1"]),
    ],
)
def test_request_rewrite_rejects_mutation_udp_and_foreign_uri(
    wire_request: bytes,
) -> None:
    with pytest.raises(GatewayError, match="RTSP request is invalid"):
        _rewrite_request(wire_request, local_base=LOCAL, upstream_base=UPSTREAM)


def test_response_rewrite_removes_upstream_uri_token_and_camera() -> None:
    body = (
        b"v=0\r\n"
        b"m=video 0 RTP/AVP 96\r\n"
        b"a=rtpmap:96 H264/90000\r\n"
        b"a=control:streamid=0\r\n"
    )
    response = message(
        "RTSP/1.0 200 OK",
        ["CSeq: 1", f"Content-Base: {UPSTREAM}"],
        body,
    )
    rewritten = _rewrite_response(
        response,
        local_base=LOCAL,
        upstream_base=UPSTREAM,
        secret_token=SECRET,
        camera_number=CAMERA,
    )
    assert LOCAL.encode() in rewritten
    assert SECRET.encode() not in rewritten
    assert CAMERA.encode() not in rewritten
    assert b"a=control:streamid=0" in rewritten


def test_response_rewrite_fails_closed_on_unexpected_secret_occurrence() -> None:
    response = message("RTSP/1.0 200 OK", ["CSeq: 1", f"X-Token: {SECRET}"])
    with pytest.raises(GatewayError, match="RTSP response is invalid"):
        _rewrite_response(
            response,
            local_base=LOCAL,
            upstream_base=UPSTREAM,
            secret_token=SECRET,
            camera_number=CAMERA,
        )


def test_response_rewrite_fails_closed_on_percent_encoded_secret() -> None:
    secret = "signed token/with spaces"
    encoded = "signed%20token%2Fwith%20spaces"
    response = message("RTSP/1.0 200 OK", ["CSeq: 1", f"X-Token: {encoded}"])
    with pytest.raises(GatewayError, match="RTSP response is invalid"):
        _rewrite_response(
            response,
            local_base=LOCAL,
            upstream_base=UPSTREAM,
            secret_token=secret,
            camera_number=CAMERA,
        )


@pytest.mark.parametrize(
    ("secret", "camera_number", "leaked"),
    [
        ("a/b+c", CAMERA, "a%2fb%2Bc"),
        (SECRET, "PRIVATE-CAMERA-NUMBER", "PR%49VATE-CAMERA-NUMBER"),
        ("a/b+c", CAMERA, "a%252fb%252Bc"),
    ],
)
def test_response_rewrite_rejects_mixed_and_nested_percent_encoding(
    secret: str, camera_number: str, leaked: str
) -> None:
    response = message("RTSP/1.0 200 OK", ["CSeq: 1", f"X-Leak: {leaked}"])
    with pytest.raises(GatewayError, match="RTSP response is invalid"):
        _rewrite_response(
            response,
            local_base=LOCAL,
            upstream_base=UPSTREAM,
            secret_token=secret,
            camera_number=camera_number,
        )


def test_response_rewrite_rejects_deep_percent_encoding() -> None:
    leaked = "a%2fb"
    for _ in range(7):
        leaked = leaked.replace("%", "%25")
    response = message("RTSP/1.0 200 OK", ["CSeq: 1", f"X-Leak: {leaked}"])
    with pytest.raises(GatewayError, match="RTSP response is invalid"):
        _rewrite_response(
            response,
            local_base=LOCAL,
            upstream_base=UPSTREAM,
            secret_token="a/b",
            camera_number=CAMERA,
        )


def test_loopback_proxy_hides_signed_upstream_and_relays_interleaved_rtp() -> None:
    portal = FakePortal()
    upstream_proxy_side, upstream_fake_side = socket.socketpair()
    connector_calls: list[tuple[tuple[str, int], float]] = []

    def connector(address: tuple[str, int], timeout: float) -> socket.socket:
        connector_calls.append((address, timeout))
        return upstream_proxy_side

    gateway = RtspGateway(
        portal=portal,  # type: ignore[arg-type]
        bindings=(
            CameraBinding(
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                CAMERA,
            ),
        ),
        monotonic=lambda: 100.0,
        connector=connector,
    )
    server = build_rtsp_server("127.0.0.1", 0, gateway)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    fake_seen: list[bytes] = []

    def fake_upstream() -> None:
        first = receive_message(upstream_fake_side)
        fake_seen.append(first)
        assert first.startswith(f"DESCRIBE {UPSTREAM} RTSP/1.0".encode())
        sdp = (
            b"v=0\r\n"
            b"m=video 0 RTP/AVP 96\r\n"
            b"a=rtpmap:96 H264/90000\r\n"
            b"a=control:streamid=0\r\n"
        )
        upstream_fake_side.sendall(
            message(
                "RTSP/1.0 200 OK", ["CSeq: 1", "Content-Type: application/sdp"], sdp
            )
        )
        second = receive_message(upstream_fake_side)
        fake_seen.append(second)
        assert second.startswith(f"SETUP {UPSTREAM}/streamid=0 RTSP/1.0".encode())
        upstream_fake_side.sendall(
            message(
                "RTSP/1.0 200 OK",
                [
                    "CSeq: 2",
                    "Session: SAFESESSION;timeout=60",
                    "Transport: RTP/AVP/TCP;unicast;interleaved=0-1",
                ],
            )
        )
        third = receive_message(upstream_fake_side)
        fake_seen.append(third)
        assert third.startswith(f"PLAY {UPSTREAM} RTSP/1.0".encode())
        upstream_fake_side.sendall(
            message("RTSP/1.0 200 OK", ["CSeq: 3", "Session: SAFESESSION"])
        )
        upstream_fake_side.sendall(b"$\x00\x00\x04RTP!")

    fake_thread = threading.Thread(target=fake_upstream, daemon=True)
    fake_thread.start()
    client = socket.create_connection(("127.0.0.1", server.server_port), timeout=2)
    client.settimeout(2)
    local = f"rtsp://127.0.0.1:{server.server_port}/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    try:
        client.sendall(
            message(
                f"DESCRIBE {local} RTSP/1.0", ["CSeq: 1", "Accept: application/sdp"]
            )
        )
        response = receive_message(client)
        assert response.startswith(b"RTSP/1.0 200 OK")
        assert SECRET.encode() not in response and CAMERA.encode() not in response

        client.sendall(
            message(
                f"SETUP {local}/streamid=0 RTSP/1.0",
                ["CSeq: 2", "Transport: RTP/AVP/TCP;unicast;interleaved=0-1"],
            )
        )
        assert receive_message(client).startswith(b"RTSP/1.0 200 OK")
        client.sendall(
            message(f"PLAY {local} RTSP/1.0", ["CSeq: 3", "Session: SAFESESSION"])
        )
        assert receive_message(client).startswith(b"RTSP/1.0 200 OK")
        assert client.recv(8) == b"$\x00\x00\x04RTP!"
        gateway.close()
        assert client.recv(1) == b""
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        gateway.close()
        upstream_fake_side.close()
        server_thread.join(timeout=2)
        fake_thread.join(timeout=2)
    assert portal.calls == 1
    assert connector_calls == [(("video.ucams.ufanet.ru", 554), 5.0)]
    assert len(fake_seen) == 3


def test_rtsp_server_refuses_non_loopback_bind() -> None:
    gateway = RtspGateway(
        portal=FakePortal(),  # type: ignore[arg-type]
        bindings=(
            CameraBinding(
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                CAMERA,
            ),
        ),
    )
    try:
        with pytest.raises(GatewayError, match="loopback"):
            build_rtsp_server("0.0.0.0", 0, gateway)
    finally:
        gateway.close()


def test_gateway_supports_many_dynamic_account_bindings_and_atomic_removal() -> None:
    portal = FakePortal()
    bindings = tuple(
        CameraBinding(f"{index:064x}", f"SYNTHETIC-CAMERA-{index}")
        for index in range(1, 5)
    )
    gateway = RtspGateway(portal=portal, bindings=bindings)  # type: ignore[arg-type]
    try:
        assert gateway.aliases == frozenset(binding.alias for binding in bindings)
        first = gateway._lease(bindings[0].alias)
        assert first.alias == bindings[0].alias
        assert portal.calls == 1

        replacement = (
            CameraBinding(bindings[0].alias, "SYNTHETIC-CAMERA-CHANGED"),
            bindings[2],
            bindings[3],
        )
        gateway.replace_bindings(replacement)
        assert bindings[1].alias not in gateway.aliases
        with pytest.raises(GatewayError, match="stream is not allowed"):
            gateway._lease(bindings[1].alias)
        changed = gateway._lease(bindings[0].alias)
        assert changed.camera_number == "SYNTHETIC-CAMERA-CHANGED"
        assert portal.calls == 2

        rendered = repr(gateway)
        assert bindings[0].alias not in rendered
        assert "SYNTHETIC-CAMERA" not in rendered
        assert "camera_count=3" in rendered
    finally:
        gateway.close()


def test_empty_gateway_can_start_before_an_account_gains_a_camera() -> None:
    portal = FakePortal()
    gateway = RtspGateway(portal=portal, bindings=())  # type: ignore[arg-type]
    server = build_rtsp_server("127.0.0.1", 0, gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert gateway.aliases == frozenset()
        gateway.replace_bindings((CameraBinding("c" * 64, CAMERA),))
        assert gateway.aliases == frozenset({"c" * 64})
    finally:
        server.shutdown()
        server.server_close()
        gateway.close()
        thread.join(timeout=2)


def test_upstream_dns_is_pinned_only_to_public_addresses() -> None:
    def mixed_resolver(*_args: object, **_kwargs: object):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", 554)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 554)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 554)),
        ]

    assert _public_socket_addresses(
        "video.ucams.ufanet.ru", 554, resolver=mixed_resolver
    ) == ((socket.AF_INET, socket.SOCK_STREAM, 6, ("8.8.8.8", 554)),)

    def private_resolver(*_args: object, **_kwargs: object):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 554)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.4", 554)),
        ]

    with pytest.raises(GatewayError, match="resolution failed"):
        _public_socket_addresses(
            "video.ucams.ufanet.ru", 554, resolver=private_resolver
        )


def test_rtsp_server_rejects_clients_beyond_bounded_capacity() -> None:
    gateway = RtspGateway(portal=FakePortal(), bindings=())  # type: ignore[arg-type]
    server = build_rtsp_server("127.0.0.1", 0, gateway)
    rejected, peer = socket.socketpair()
    try:
        for _ in range(_MAX_CLIENTS):
            assert server._client_slots.acquire(blocking=False)
        server.process_request(rejected, ("127.0.0.1", 65000))
        assert rejected.fileno() == -1
    finally:
        for _ in range(_MAX_CLIENTS):
            server._client_slots.release()
        peer.close()
        server.server_close()
        gateway.close()
