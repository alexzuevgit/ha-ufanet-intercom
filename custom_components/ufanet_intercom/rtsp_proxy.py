"""Loopback-only RTSP proxy that keeps Ufanet signed URLs in memory."""

from __future__ import annotations

import ipaddress
import socket
import socketserver
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType
from typing import Any, Protocol, cast
from urllib.parse import quote, urlsplit

from .media import CameraBinding, GatewayError, MediaLease

_MAX_HEADER_BYTES = 64 * 1024
_MAX_BODY_BYTES = 256 * 1024
_MAX_BUFFER_BYTES = _MAX_HEADER_BYTES + _MAX_BODY_BYTES + 65539
_CONNECT_TIMEOUT_SECONDS = 5.0
_SOCKET_POLL_SECONDS = 1.0
_IDLE_TIMEOUT_SECONDS = 45.0
_MAX_CLIENTS = 16
_ALLOWED_METHODS = frozenset(
    {"OPTIONS", "DESCRIBE", "SETUP", "PLAY", "PAUSE", "TEARDOWN", "GET_PARAMETER"}
)


def _public_socket_addresses(
    host: str,
    port: int,
    *,
    resolver: Callable[
        ..., Sequence[tuple[int, int, int, str, tuple[object, ...]]]
    ] = socket.getaddrinfo,
) -> tuple[tuple[int, int, int, tuple[object, ...]], ...]:
    """Resolve once and retain only public provider stream addresses."""

    if type(host) is not str or type(port) is not int or isinstance(port, bool):
        raise GatewayError("upstream RTSP resolution failed")
    try:
        resolved = resolver(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise GatewayError("upstream RTSP resolution failed") from None
    if not isinstance(resolved, list) or not 1 <= len(resolved) <= 16:
        raise GatewayError("upstream RTSP resolution failed")
    accepted: list[tuple[int, int, int, tuple[object, ...]]] = []
    seen: set[tuple[int, tuple[object, ...]]] = set()
    for item in resolved:
        if type(item) is not tuple or len(item) != 5:
            raise GatewayError("upstream RTSP resolution failed")
        family, socktype, protocol, _canonical, sockaddr = item
        if (
            family not in {socket.AF_INET, socket.AF_INET6}
            or socktype != socket.SOCK_STREAM
            or type(protocol) is not int
            or type(sockaddr) is not tuple
            or not sockaddr
            or type(sockaddr[0]) is not str
        ):
            continue
        try:
            address = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if not address.is_global:
            continue
        key = (family, sockaddr)
        if key in seen:
            continue
        seen.add(key)
        accepted.append((family, socktype, protocol, sockaddr))
    if not accepted:
        raise GatewayError("upstream RTSP resolution failed")
    return tuple(accepted)


def _connect_public_host(address: tuple[str, int], timeout: float) -> socket.socket:
    """Pin validated DNS output so a second lookup cannot rebind to a LAN host."""

    host, port = address
    for family, socktype, protocol, sockaddr in _public_socket_addresses(host, port):
        connection = socket.socket(family, socktype, protocol)
        try:
            connection.settimeout(timeout)
            connection.connect(sockaddr)
            return connection
        except OSError:
            connection.close()
    raise OSError("upstream RTSP connection failed")


class _LeaseProvider(Protocol):
    def get_lease(self, binding: CameraBinding) -> MediaLease: ...


class _FrameReader:
    """Read bounded RTSP messages or RTP-over-RTSP interleaved frames."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def read(self, source: socket.socket) -> bytes:
        while True:
            frame = self._pop()
            if frame is not None:
                return frame
            chunk = source.recv(65536)
            if not chunk:
                raise EOFError
            self._buffer.extend(chunk)
            if len(self._buffer) > _MAX_BUFFER_BYTES:
                raise GatewayError("RTSP frame is invalid")

    def _pop(self) -> bytes | None:
        if not self._buffer:
            return None
        if self._buffer[0] == 0x24:  # '$' interleaved RTP/RTCP
            if len(self._buffer) < 4:
                return None
            payload_length = int.from_bytes(self._buffer[2:4], "big")
            total = 4 + payload_length
            if len(self._buffer) < total:
                return None
            frame = bytes(self._buffer[:total])
            del self._buffer[:total]
            return frame
        marker = self._buffer.find(b"\r\n\r\n")
        if marker < 0:
            if len(self._buffer) > _MAX_HEADER_BYTES:
                raise GatewayError("RTSP frame is invalid")
            return None
        header_end = marker + 4
        if header_end > _MAX_HEADER_BYTES:
            raise GatewayError("RTSP frame is invalid")
        header = bytes(self._buffer[:marker])
        content_length = _content_length(header)
        if content_length > _MAX_BODY_BYTES:
            raise GatewayError("RTSP frame is invalid")
        total = header_end + content_length
        if len(self._buffer) < total:
            return None
        frame = bytes(self._buffer[:total])
        del self._buffer[:total]
        return frame


def _content_length(header: bytes) -> int:
    length = 0
    seen = False
    for line in header.split(b"\r\n")[1:]:
        if b":" not in line:
            raise GatewayError("RTSP frame is invalid")
        name, value = line.split(b":", 1)
        if name.strip().lower() != b"content-length":
            continue
        if seen:
            raise GatewayError("RTSP frame is invalid")
        seen = True
        raw = value.strip()
        if not raw.isdigit():
            raise GatewayError("RTSP frame is invalid")
        length = int(raw)
    return length


def _parts(message: bytes) -> tuple[list[bytes], bytes]:
    if not message or message.startswith(b"$") or b"\r\n\r\n" not in message:
        raise GatewayError("RTSP frame is invalid")
    header, body = message.split(b"\r\n\r\n", 1)
    lines = header.split(b"\r\n")
    if not lines or not lines[0] or any(b"\x00" in line for line in lines):
        raise GatewayError("RTSP frame is invalid")
    if _content_length(header) != len(body):
        raise GatewayError("RTSP frame is invalid")
    return lines, body


def _rebuild(lines: list[bytes], body: bytes) -> bytes:
    rebuilt: list[bytes] = [lines[0]]
    found_length = False
    for line in lines[1:]:
        name = line.split(b":", 1)[0].strip().lower()
        if name == b"content-length":
            if found_length:
                raise GatewayError("RTSP frame is invalid")
            found_length = True
            rebuilt.append(f"Content-Length: {len(body)}".encode())
        else:
            rebuilt.append(line)
    if body and not found_length:
        rebuilt.append(f"Content-Length: {len(body)}".encode())
    return b"\r\n".join(rebuilt) + b"\r\n\r\n" + body


def _valid_tcp_transport(value: bytes) -> bool:
    if not value or b"," in value:
        return False
    parts = [part.strip().lower() for part in value.split(b";")]
    if not parts or parts[0] != b"rtp/avp/tcp":
        return False
    unicast = False
    interleaved = False
    for part in parts[1:]:
        if part == b"unicast":
            if unicast:
                return False
            unicast = True
        elif part.startswith(b"interleaved="):
            if interleaved:
                return False
            channels = part.removeprefix(b"interleaved=").split(b"-", 1)
            if (
                len(channels) != 2
                or not all(channel.isdigit() for channel in channels)
                or int(channels[0]) > 254
                or int(channels[1]) != int(channels[0]) + 1
            ):
                return False
            interleaved = True
        elif part not in {b"mode=play", b'mode="play"'}:
            return False
    return unicast and interleaved


def _rewrite_request(message: bytes, *, local_base: str, upstream_base: str) -> bytes:
    lines, body = _parts(message)
    try:
        first = lines[0].decode("ascii", "strict")
    except UnicodeError:
        raise GatewayError("RTSP request is invalid") from None
    pieces = first.split(" ")
    if len(pieces) != 3 or pieces[2] != "RTSP/1.0":
        raise GatewayError("RTSP request is invalid")
    method, uri, _ = pieces
    if method not in _ALLOWED_METHODS:
        raise GatewayError("RTSP request is invalid")
    if uri == "*" and method == "OPTIONS":
        upstream_uri = upstream_base
    elif uri == local_base:
        if method == "SETUP":
            raise GatewayError("RTSP request is invalid")
        upstream_uri = upstream_base
    elif uri.startswith(local_base + "/"):
        suffix = uri[len(local_base) :]
        if method != "SETUP" or suffix not in {"/streamid=0", "/streamid=1"}:
            raise GatewayError("RTSP request is invalid")
        upstream_uri = upstream_base + suffix
    else:
        raise GatewayError("RTSP request is invalid")
    headers: dict[bytes, bytes] = {}
    for line in lines[1:]:
        name, value = line.split(b":", 1)
        key = name.strip().lower()
        if key in headers:
            raise GatewayError("RTSP request is invalid")
        headers[key] = value.strip()
    if b"cseq" not in headers or not headers[b"cseq"].isdigit():
        raise GatewayError("RTSP request is invalid")
    if method == "SETUP" and not _valid_tcp_transport(headers.get(b"transport", b"")):
        raise GatewayError("RTSP request is invalid")
    lines[0] = f"{method} {upstream_uri} RTSP/1.0".encode()
    local = local_base.encode()
    upstream = upstream_base.encode()
    lines[1:] = [line.replace(local, upstream) for line in lines[1:]]
    body = body.replace(local, upstream)
    return _rebuild(lines, body)


def _percent_decode_once(value: bytes) -> bytes:
    decoded = bytearray()
    index = 0
    hexadecimal = b"0123456789abcdefABCDEF"
    while index < len(value):
        if (
            value[index] == 0x25
            and index + 2 < len(value)
            and value[index + 1] in hexadecimal
            and value[index + 2] in hexadecimal
        ):
            decoded.append(int(value[index + 1 : index + 3], 16))
            index += 3
        else:
            decoded.append(value[index])
            index += 1
    return bytes(decoded)


def _contains_protected_value(
    message: bytes, *, secret_token: str, camera_number: str
) -> bool:
    protected = (secret_token.encode(), camera_number.encode())
    probe = message
    for _ in range(8):
        if any(value and value in probe for value in protected):
            return True
        decoded = _percent_decode_once(probe)
        if decoded == probe:
            return False
        probe = decoded
    # Excessive nesting is itself invalid and potentially evasive.
    return True


def _rewrite_response(
    message: bytes,
    *,
    local_base: str,
    upstream_base: str,
    secret_token: str,
    camera_number: str,
) -> bytes:
    lines, body = _parts(message)
    if not lines[0].startswith(b"RTSP/1.0 "):
        raise GatewayError("RTSP response is invalid")
    upstream = upstream_base.encode()
    local = local_base.encode()
    lines = [line.replace(upstream, local) for line in lines]
    body = body.replace(upstream, local)
    rebuilt = _rebuild(lines, body)
    if _contains_protected_value(
        rebuilt, secret_token=secret_token, camera_number=camera_number
    ):
        raise GatewayError("RTSP response is invalid")
    return rebuilt


def _initial_alias(
    message: bytes, *, bind: str, port: int, aliases: frozenset[str]
) -> str:
    lines, _ = _parts(message)
    try:
        method, raw_uri, version = lines[0].decode("ascii", "strict").split(" ")
    except (UnicodeError, ValueError):
        raise GatewayError("RTSP request is invalid") from None
    if method not in {"OPTIONS", "DESCRIBE"} or version != "RTSP/1.0":
        raise GatewayError("RTSP request is invalid")
    parsed = urlsplit(raw_uri)
    if parsed.scheme != "rtsp" or parsed.hostname != bind or parsed.port != port:
        raise GatewayError("RTSP request is invalid")
    if parsed.query or parsed.fragment:
        raise GatewayError("RTSP request is invalid")
    pieces = [part for part in parsed.path.split("/") if part]
    if len(pieces) != 1 or pieces[0] not in aliases:
        raise GatewayError("RTSP request is invalid")
    return pieces[0]


class RtspGateway:
    """Acquire short-lived Ufanet leases and open token-hidden RTSP sessions."""

    def __init__(
        self,
        *,
        portal: _LeaseProvider,
        bindings: tuple[CameraBinding, ...],
        monotonic: Callable[[], float] = time.monotonic,
        connector: Callable[
            [tuple[str, int], float], socket.socket
        ] = _connect_public_host,
    ) -> None:
        if (
            type(bindings) is not tuple
            or len(bindings) > 256
            or any(type(binding) is not CameraBinding for binding in bindings)
            or len({binding.alias for binding in bindings}) != len(bindings)
        ):
            raise GatewayError("camera bindings are invalid")
        self._portal = portal
        self._bindings = bindings
        self._by_alias = {binding.alias: binding for binding in bindings}
        self._monotonic = monotonic
        self._connector = connector
        self._leases: Mapping[str, MediaLease] = MappingProxyType({})
        self._lock = threading.RLock()
        self._closed = False
        self._active_connections: set[socket.socket] = set()

    def __repr__(self) -> str:
        with self._lock:
            return f"RtspGateway(camera_count={len(self._by_alias)}, closed={self._closed})"

    @property
    def aliases(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._by_alias)

    def replace_bindings(self, bindings: tuple[CameraBinding, ...]) -> None:
        """Atomically replace the current account-derived camera bindings."""

        if (
            type(bindings) is not tuple
            or len(bindings) > 256
            or any(type(binding) is not CameraBinding for binding in bindings)
            or len({binding.alias for binding in bindings}) != len(bindings)
        ):
            raise GatewayError("camera bindings are invalid")
        replacement = {binding.alias: binding for binding in bindings}
        with self._lock:
            if self._closed:
                raise GatewayError("RTSP gateway is closed")
            retained = {
                alias: lease
                for alias, lease in self._leases.items()
                if alias in replacement
                and lease.camera_number == replacement[alias].number
            }
            self._bindings = bindings
            self._by_alias = replacement
            self._leases = MappingProxyType(retained)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._leases = MappingProxyType({})
            active = tuple(self._active_connections)
            self._active_connections.clear()
        for connection in active:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass

    def release(self, connection: socket.socket) -> None:
        with self._lock:
            self._active_connections.discard(connection)

    def _lease(self, alias: str) -> MediaLease:
        if type(alias) is not str:
            raise GatewayError("stream is not allowed")
        with self._lock:
            if self._closed:
                raise GatewayError("RTSP gateway is closed")
            binding = self._by_alias.get(alias)
            if binding is None:
                raise GatewayError("stream is not allowed")
            now = float(self._monotonic())
            lease = self._leases.get(alias)
            if (
                lease is not None
                and lease.camera_number == binding.number
                and lease.expires_monotonic > now + 60
            ):
                return lease

        lease = self._portal.get_lease(binding)
        if type(lease) is not MediaLease or lease.alias != binding.alias:
            raise GatewayError("media metadata is invalid")
        with self._lock:
            if self._closed:
                raise GatewayError("RTSP gateway is closed")
            if self._by_alias.get(alias) != binding:
                raise GatewayError("stream is not allowed")
            updated = dict(self._leases)
            updated[alias] = lease
            self._leases = MappingProxyType(updated)
            return lease

    def open(self, alias: str) -> tuple[MediaLease, str, socket.socket]:
        lease = self._lease(alias)
        upstream_base = (
            f"rtsp://{lease.server_host}/{quote(lease.camera_number, safe='')}"
            f"?token={quote(lease.token, safe='')}&tracks=v1a1"
        )
        try:
            connection = self._connector(
                (lease.server_host, 554), _CONNECT_TIMEOUT_SECONDS
            )
            connection.settimeout(_SOCKET_POLL_SECONDS)
        except OSError:
            raise GatewayError("upstream RTSP connection failed") from None
        with self._lock:
            if self._closed:
                try:
                    connection.close()
                except OSError:
                    pass
                raise GatewayError("RTSP gateway is closed")
            self._active_connections.add(connection)
        return lease, upstream_base, connection


class _RtspProxyHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        proxy_server = cast("_RtspProxyServer", self.server)
        downstream: socket.socket = self.request
        downstream.settimeout(_SOCKET_POLL_SECONDS)
        reader = _FrameReader()
        upstream: socket.socket | None = None
        stop = threading.Event()
        last_activity = [time.monotonic()]
        activity_lock = threading.Lock()
        worker: threading.Thread | None = None
        try:
            first = reader.read(downstream)
            if first.startswith(b"$"):
                raise GatewayError("RTSP request is invalid")
            address = proxy_server.server_address
            bind, port = str(address[0]), int(address[1])
            alias = _initial_alias(
                first,
                bind=str(bind),
                port=int(port),
                aliases=proxy_server.gateway.aliases,
            )
            lease, upstream_base, upstream = proxy_server.gateway.open(alias)
            relay_upstream = upstream
            local_base = f"rtsp://{bind}:{port}/{alias}"
            upstream.sendall(
                _rewrite_request(
                    first, local_base=local_base, upstream_base=upstream_base
                )
            )

            def touch() -> None:
                with activity_lock:
                    last_activity[0] = time.monotonic()

            def expired() -> bool:
                with activity_lock:
                    return time.monotonic() - last_activity[0] > _IDLE_TIMEOUT_SECONDS

            def client_to_upstream() -> None:
                local_reader = reader
                try:
                    while not stop.is_set():
                        try:
                            frame = local_reader.read(downstream)
                        except TimeoutError:
                            if expired():
                                break
                            continue
                        if frame.startswith(b"$"):
                            rewritten = frame
                        else:
                            rewritten = _rewrite_request(
                                frame,
                                local_base=local_base,
                                upstream_base=upstream_base,
                            )
                        relay_upstream.sendall(rewritten)
                        touch()
                except (EOFError, OSError, GatewayError):
                    pass
                finally:
                    stop.set()

            worker = threading.Thread(target=client_to_upstream, daemon=True)
            worker.start()
            upstream_reader = _FrameReader()
            while not stop.is_set():
                try:
                    frame = upstream_reader.read(relay_upstream)
                except TimeoutError:
                    if expired():
                        break
                    continue
                if frame.startswith(b"$"):
                    rewritten = frame
                else:
                    rewritten = _rewrite_response(
                        frame,
                        local_base=local_base,
                        upstream_base=upstream_base,
                        secret_token=lease.token,
                        camera_number=lease.camera_number,
                    )
                downstream.sendall(rewritten)
                touch()
        except (EOFError, OSError, GatewayError):
            pass
        finally:
            stop.set()
            if upstream is not None:
                proxy_server.gateway.release(upstream)
            for connection in (downstream, upstream):
                if connection is None:
                    continue
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    connection.close()
                except OSError:
                    pass
            if worker is not None:
                worker.join(timeout=2)


class _RtspProxyServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    block_on_close = True

    def __init__(self, address: tuple[str, int], gateway: RtspGateway) -> None:
        self.gateway = gateway
        self._client_slots = threading.BoundedSemaphore(_MAX_CLIENTS)
        super().__init__(address, _RtspProxyHandler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._client_slots.acquire(blocking=False):
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._client_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._client_slots.release()

    @property
    def server_port(self) -> int:
        return int(self.server_address[1])

    def handle_error(self, request: object, client_address: object) -> None:
        # Never render tracebacks: signed upstream URLs exist only in handler locals.
        return


def build_rtsp_server(bind: str, port: int, gateway: RtspGateway) -> _RtspProxyServer:
    if type(bind) is not str or not ipaddress.ip_address(bind).is_loopback:
        raise GatewayError("RTSP proxy bind must be loopback")
    if type(port) is not int or isinstance(port, bool) or not 0 <= port <= 65535:
        raise GatewayError("RTSP proxy port is invalid")
    if type(gateway) is not RtspGateway:
        raise TypeError("gateway must be an exact RtspGateway")
    try:
        return _RtspProxyServer((bind, port), gateway)
    except OSError:
        raise GatewayError("RTSP proxy failed to bind") from None


class RtspProxyRuntime:
    """Lifecycle wrapper for one config entry's loopback-only RTSP relay."""

    def __init__(
        self,
        portal: _LeaseProvider,
        bindings: tuple[CameraBinding, ...],
    ) -> None:
        self._portal = portal
        self._gateway = RtspGateway(portal=portal, bindings=bindings)
        self._server = build_rtsp_server("127.0.0.1", 0, self._gateway)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="ufanet-rtsp-loopback",
            daemon=True,
        )
        self._close_lock = threading.Lock()
        self._closed = False

    def __repr__(self) -> str:
        return (
            "RtspProxyRuntime("
            f"camera_count={len(self._gateway.aliases)}, closed={self._closed})"
        )

    @property
    def camera_count(self) -> int:
        return len(self._gateway.aliases)

    def start(self) -> None:
        with self._close_lock:
            if self._closed or self._thread.is_alive():
                if self._closed:
                    raise GatewayError("RTSP proxy is closed")
                return
            self._thread.start()

    def replace_bindings(self, bindings: tuple[CameraBinding, ...]) -> None:
        self._gateway.replace_bindings(bindings)

    def stream_url(self, alias: str) -> str | None:
        if type(alias) is not str or alias not in self._gateway.aliases:
            return None
        return f"rtsp://127.0.0.1:{self._server.server_port}/{alias}"

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self._gateway.close()
        if self._thread.is_alive():
            self._server.shutdown()
        self._server.server_close()
        close = getattr(self._portal, "close", None)
        if callable(close):
            close()
        if self._thread.is_alive():
            self._thread.join(timeout=3)
