"""Pure normalization and protected storage for voice code phrases."""

from __future__ import annotations

import base64
import secrets
import unicodedata
from hmac import compare_digest
from typing import NoReturn

from argon2.exceptions import HashingError
from argon2.low_level import Type, hash_secret_raw

_ERROR_TEXT = "Invalid voice phrase configuration."
_KDF_ERROR_TEXT = "Voice phrase key derivation failed."
_VERSION = 1
_SALT_LEN = 16
_DIGEST_LEN = 32
_ENCODED_SALT_LEN = 24
_ENCODED_DIGEST_LEN = 44
_MIN_PHRASE_BYTES = 3
_MAX_PHRASE_BYTES = 128
_MAX_RAW_PHRASE_CODEPOINTS = 512
_MAX_PHRASES = 16
_ARGON2_VERSION = 19
_ARGON2_TIME_COST = 2
_ARGON2_MEMORY_COST = 19456
_ARGON2_PARALLELISM = 1


class PhraseConfigError(ValueError):
    """A deliberately context-free public phrase configuration error."""

    def __init__(self, *_private: object) -> None:
        """Discard all caller-provided detail and expose only fixed public text."""

        super().__init__(_ERROR_TEXT)


class PhraseKdfError(RuntimeError):
    """A deliberately context-free public phrase KDF error."""

    def __init__(self, *_private: object) -> None:
        """Discard all caller-provided detail and expose only fixed public text."""

        super().__init__(_KDF_ERROR_TEXT)


def _fail() -> NoReturn:
    """Raise the one public validation error."""

    try:
        raise PhraseConfigError()
    except PhraseConfigError as error:
        error.args = (_ERROR_TEXT,)
        error.__context__ = None
        error.__cause__ = None
        error.__notes__ = []
        raise


def _fail_kdf() -> NoReturn:
    """Raise the one public operational KDF error."""

    try:
        raise PhraseKdfError()
    except PhraseKdfError as error:
        error.args = (_KDF_ERROR_TEXT,)
        error.__context__ = None
        error.__cause__ = None
        error.__notes__ = []
        raise


def normalize_phrase(value: object) -> str:
    """Return the strict, comparison-ready form of one voice phrase."""

    if type(value) is not str or len(value) > _MAX_RAW_PHRASE_CODEPOINTS:
        _fail()

    normalized = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    normalized = " ".join(
        "".join(
            character if unicodedata.category(character)[0] in {"L", "N"} else " "
            for character in normalized
        ).split()
    )

    encoded: bytes | None = None
    try:
        encoded = normalized.encode("utf-8")
    except UnicodeError:
        pass
    if encoded is None or not _MIN_PHRASE_BYTES <= len(encoded) <= _MAX_PHRASE_BYTES:
        _fail()
    return normalized


def _encode_base64(value: bytes) -> str:
    """Encode bytes as canonical padded URL-safe Base64."""

    return base64.urlsafe_b64encode(value).decode("ascii")


def _decode_base64(value: str, expected_length: int) -> bytes:
    """Decode only canonical padded URL-safe Base64 of the exact size."""

    decoded: bytes | None = None
    try:
        decoded = base64.b64decode(value, altchars=b"-_", validate=True)
    except (UnicodeError, ValueError):
        pass
    if (
        decoded is None
        or len(decoded) != expected_length
        or _encode_base64(decoded) != value
    ):
        _fail()
    return decoded


def _derive_digest(normalized: str, salt: bytes) -> bytes:
    """Derive one fixed-parameter Argon2id digest."""

    digest: bytes | None = None
    try:
        digest = hash_secret_raw(
            normalized.encode("utf-8"),
            salt,
            time_cost=_ARGON2_TIME_COST,
            memory_cost=_ARGON2_MEMORY_COST,
            parallelism=_ARGON2_PARALLELISM,
            hash_len=_DIGEST_LEN,
            type=Type.ID,
            version=_ARGON2_VERSION,
        )
    except HashingError:
        _fail_kdf()
    if type(digest) is not bytes or len(digest) != _DIGEST_LEN:
        _fail_kdf()
    return digest


def encode_phrase_set(
    phrases: object, *, salt: bytes | None = None
) -> dict[str, object]:
    """Normalize, deduplicate, and protect a strict list of voice phrases."""

    if type(phrases) is not list or not 1 <= len(phrases) <= _MAX_PHRASES:
        _fail()
    if salt is not None and (type(salt) is not bytes or len(salt) != _SALT_LEN):
        _fail()

    normalized_phrases: list[str] = []
    seen: set[str] = set()
    for phrase in phrases:
        if type(phrase) is not str:
            _fail()
        normalized = normalize_phrase(phrase)
        if normalized not in seen:
            seen.add(normalized)
            normalized_phrases.append(normalized)
            if len(normalized_phrases) > _MAX_PHRASES:
                _fail()
    if not normalized_phrases:
        _fail()

    actual_salt = secrets.token_bytes(_SALT_LEN) if salt is None else salt
    if type(actual_salt) is not bytes or len(actual_salt) != _SALT_LEN:
        _fail()
    digests = sorted(
        _encode_base64(_derive_digest(normalized, actual_salt))
        for normalized in normalized_phrases
    )
    return {
        "version": _VERSION,
        "salt": _encode_base64(actual_salt),
        "digests": digests,
    }


def validate_entered_phrases(phrases: object) -> int:
    """Validate bounded, recoverable UI lines; return the normalized phrase count.

    This is configuration, never an STT transcript. No KDF runs in the reader;
    the protected matcher remains authoritative for recognition.
    """

    if type(phrases) is not list or not 1 <= len(phrases) <= _MAX_PHRASES:
        _fail()
    normalized: set[str] = set()
    for phrase in phrases:
        if (
            type(phrase) is not str
            or len(phrase) > _MAX_RAW_PHRASE_CODEPOINTS
            or phrase.splitlines() != [phrase]
        ):
            _fail()
        normalized.add(normalize_phrase(phrase))
    return len(normalized)


class PhraseMatcher:
    """Match transcripts against a protected, validated phrase set."""

    __slots__ = ("_digests", "_salt")

    def __init__(self, salt: bytes, digests: tuple[bytes, ...]) -> None:
        self._salt = salt
        self._digests = digests

    @classmethod
    def from_stored(cls, value: object) -> PhraseMatcher:
        """Validate and load the exact version-one protected storage shape."""

        if (
            type(value) is not dict
            or len(value) != 3
            or any(type(key) is not str for key in value)
            or set(value) != {"version", "salt", "digests"}
        ):
            _fail()

        version = value["version"]
        encoded_salt = value["salt"]
        encoded_digests = value["digests"]
        if (
            type(version) is not int
            or version != _VERSION
            or type(encoded_salt) is not str
            or len(encoded_salt) != _ENCODED_SALT_LEN
            or not encoded_salt.isascii()
            or type(encoded_digests) is not list
            or not 1 <= len(encoded_digests) <= _MAX_PHRASES
            or any(
                type(digest) is not str
                or len(digest) != _ENCODED_DIGEST_LEN
                or not digest.isascii()
                for digest in encoded_digests
            )
        ):
            _fail()
        if encoded_digests != sorted(encoded_digests) or len(
            set(encoded_digests)
        ) != len(encoded_digests):
            _fail()

        salt = _decode_base64(encoded_salt, _SALT_LEN)
        digests = tuple(
            _decode_base64(encoded_digest, _DIGEST_LEN)
            for encoded_digest in encoded_digests
        )
        return cls(salt, digests)

    @property
    def count(self) -> int:
        """Return the number of protected phrases."""

        return len(self._digests)

    def matches(self, transcript: object) -> bool:
        """Normalize and compare one transcript without timing-shortcut membership."""

        try:
            normalized = normalize_phrase(transcript)
        except PhraseConfigError:
            return False

        candidate = _derive_digest(normalized, self._salt)
        matched = False
        for digest in self._digests:
            matched |= compare_digest(candidate, digest)
        return matched

    def __repr__(self) -> str:
        """Render only the non-secret phrase count."""

        return f"PhraseMatcher(count={self.count})"
