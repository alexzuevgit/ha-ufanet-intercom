"""Pure constants and privacy-safe discovered-door identities."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from typing import Any, Final

DOMAIN: Final = "ufanet_intercom"
BASE_URL: Final = "https://dom.ufanet.ru"
AUTH_PATH: Final = "/api/v1/auth/auth_by_contract/"
REFRESH_PATH: Final = "/api/v1/auth/refresh/"
DISCOVERY_PATH: Final = "/api/v0/skud/shared/"
PLATFORMS: Final = ("button", "binary_sensor", "camera")

CONF_CONTRACT: Final = "contract"
CONF_PASSWORD: Final = "password"
CONF_IDENTITY_KEY: Final = "identity_key"
CONF_TRUSTED_BINDINGS: Final = "trusted_bindings"
CONF_REQUIRES_ACK: Final = "requires_ack"

MANUFACTURER: Final = "Ufanet"
UPDATE_INTERVAL_MINUTES: Final = 5
USER_AGENT: Final = "android/4.0.14"

DOOR_SELECTOR: Final = 0
IDENTITY_KEY_BYTES: Final = 32
MAX_CONTRACT_CHARS: Final = 512
MAX_PROVIDER_INTEGER: Final = (1 << 63) - 1
MAX_CCTV_NUMBER_CHARS: Final = 256
_ACCOUNT_FINGERPRINT_DOMAIN: Final = b"ufanet-intercom:v1:account:fingerprint\0"
_TARGET_KEY_DOMAIN: Final = b"ufanet-intercom:v1:account:shared-intercom:target\0"
_TARGET_BINDING_DOMAIN: Final = b"ufanet-intercom:v2:account:shared-intercom:binding\0"
_HMAC_HEX_RE: Final = re.compile(r"^[0-9a-f]{64}$")


def contract_fingerprint(identity_key: bytes, contract: str) -> str:
    """Return a full keyed config-entry ID without exposing the contract."""

    _validate_identity_key(identity_key)
    if (
        type(contract) is not str
        or not contract
        or len(contract) > MAX_CONTRACT_CHARS
        or contract != contract.strip()
    ):
        raise ValueError("invalid contract")
    canonical = _ACCOUNT_FINGERPRINT_DOMAIN + contract.upper().encode("utf-8", "strict")
    digest = hmac.new(identity_key, canonical, hashlib.sha256).hexdigest()
    return f"ufanet-{digest}"


def _validate_identity_key(identity_key: bytes) -> None:
    if type(identity_key) is not bytes or len(identity_key) != IDENTITY_KEY_BYTES:
        raise ValueError("invalid identity key")


def _valid_provider_integer(value: Any) -> bool:
    return type(value) is int and 0 < value <= MAX_PROVIDER_INTEGER


def _valid_nullable_provider_integer(value: Any) -> bool:
    return value is None or _valid_provider_integer(value)


def discovered_door_key(
    identity_key: bytes,
    shared_id: int,
    door: int = DOOR_SELECTOR,
) -> str:
    """Return a full keyed target ID in the account-scoped resource domain."""

    _validate_identity_key(identity_key)
    if (
        not _valid_provider_integer(shared_id)
        or type(door) is not int
        or door != DOOR_SELECTOR
    ):
        raise ValueError("invalid door identity")
    canonical = _TARGET_KEY_DOMAIN + f"{shared_id}:{door}".encode("ascii", "strict")
    return hmac.new(identity_key, canonical, hashlib.sha256).hexdigest()


def discovered_door_binding(
    identity_key: bytes,
    *,
    shared_id: int,
    door: int,
    model: int,
    house: int | None,
    contract: int | None,
    cctv_number: str,
    house_present: bool = True,
    contract_present: bool = True,
) -> str:
    """Bind command identity to exact security-relevant discovery metadata."""

    _validate_identity_key(identity_key)
    if (
        not _valid_provider_integer(shared_id)
        or type(door) is not int
        or door != DOOR_SELECTOR
        or not _valid_provider_integer(model)
        or not _valid_nullable_provider_integer(house)
        or not _valid_nullable_provider_integer(contract)
        or type(house_present) is not bool
        or type(contract_present) is not bool
        or (not house_present and house is not None)
        or (not contract_present and contract is not None)
        or type(cctv_number) is not str
        or len(cctv_number) > MAX_CCTV_NUMBER_CHARS
    ):
        raise ValueError("invalid door binding")
    canonical = json.dumps(
        [
            shared_id,
            door,
            model,
            [house_present, house],
            [contract_present, contract],
            cctv_number,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii", "strict")
    return hmac.new(
        identity_key,
        _TARGET_BINDING_DOMAIN + canonical,
        hashlib.sha256,
    ).hexdigest()


def _valid_hmac_hex(value: Any) -> bool:
    return type(value) is str and _HMAC_HEX_RE.fullmatch(value) is not None


@dataclass(frozen=True, slots=True, eq=False)
class DiscoveredDoor:
    """A bounded runtime door whose representation contains no provider metadata."""

    key: str
    shared_id: int = field(repr=False)
    door: int
    model: int
    display_name: str = field(repr=False, compare=False)
    binding: str = field(repr=False)
    openable: bool = field(compare=False)
    trusted: bool = field(compare=False)
    cctv_number: str = field(default="", repr=False, compare=False)
    house: int | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not _valid_provider_integer(self.shared_id):
            raise ValueError("invalid discovered door")
        if type(self.door) is not int or self.door != DOOR_SELECTOR:
            raise ValueError("invalid discovered door")
        if not _valid_provider_integer(self.model):
            raise ValueError("invalid discovered door")
        if type(self.display_name) is not str or not self.display_name:
            raise ValueError("invalid discovered door")
        if not _valid_hmac_hex(self.key) or not _valid_hmac_hex(self.binding):
            raise ValueError("invalid discovered door")
        if type(self.openable) is not bool or type(self.trusted) is not bool:
            raise ValueError("invalid discovered door")
        if (
            type(self.cctv_number) is not str
            or len(self.cctv_number) > MAX_CCTV_NUMBER_CHARS
            or type(self.house) not in (int, type(None))
            or (self.house is not None and not _valid_provider_integer(self.house))
        ):
            raise ValueError("invalid discovered door")

    @property
    def unique_id(self) -> str:
        """Return a stable privacy-safe Home Assistant entity identity."""

        return f"{DOMAIN}:{self.key}"

    @property
    def suggested_object_id(self) -> str:
        """Return a stable privacy-safe suggested object ID."""

        return f"ufanet_{self.key}"

    def matches_command_identity(self, other: object) -> bool:
        """Compare the exact immutable provider command identity and binding."""

        return (
            type(other) is DiscoveredDoor
            and self.shared_id == other.shared_id
            and self.door == other.door
            and self.model == other.model
            and self.binding == other.binding
        )

    def __eq__(self, other: object) -> bool:
        if type(other) is not DiscoveredDoor:
            return NotImplemented
        return self.matches_command_identity(other)

    def __hash__(self) -> int:
        return hash((self.shared_id, self.door, self.model, self.binding))

    def __repr__(self) -> str:
        return f"{type(self).__name__}(key={self.key!r}, door={self.door})"
