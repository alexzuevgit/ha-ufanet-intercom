"""Pure contract tests for protected voice phrase sets."""

from __future__ import annotations

import ast
import base64
import hashlib
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn, cast

import pytest
from argon2.exceptions import HashingError
from argon2.low_level import Type

from custom_components.ufanet_intercom import voice_phrase
from custom_components.ufanet_intercom.voice_phrase import (
    PhraseConfigError,
    PhraseMatcher,
    encode_phrase_set,
    normalize_phrase,
)

PUBLIC_ERROR = "Invalid voice phrase configuration."
KDF_ERROR = "Voice phrase key derivation failed."
SALT = bytes(range(16))
SALT_B64 = "AAECAwQFBgcICQoLDA0ODw=="
ZERO_DIGEST = bytes(32)
ONE_DIGEST = bytes([1]) * 32
TWO_DIGEST = bytes([2]) * 32


def b64(value: bytes) -> str:
    """Return the canonical URL-safe Base64 representation."""

    return base64.urlsafe_b64encode(value).decode("ascii")


def stored_with(*digests: bytes) -> dict[str, object]:
    """Build structurally valid storage without spending a KDF call."""

    return {
        "version": 1,
        "salt": SALT_B64,
        "digests": sorted(b64(digest) for digest in digests),
    }


def assert_fixed_error(call: Callable[[], object], secret: str = "") -> None:
    """Assert validation fails without preserving private input or exception state."""

    with pytest.raises(PhraseConfigError) as caught:
        call()
    error = caught.value
    assert type(error) is PhraseConfigError
    assert error.args == (PUBLIC_ERROR,)
    assert str(error) == PUBLIC_ERROR
    assert error.__cause__ is None
    assert error.__context__ is None
    assert not getattr(error, "__notes__", ())
    if secret:
        assert secret not in str(error)
        assert secret not in repr(error)


def assert_fixed_kdf_error(call: Callable[[], object], secret: str = "") -> None:
    """Assert a KDF failure exposes only fixed operational error text."""

    with pytest.raises(voice_phrase.PhraseKdfError) as caught:
        call()
    error = caught.value
    assert type(error) is voice_phrase.PhraseKdfError
    assert error.args == (KDF_ERROR,)
    assert str(error) == KDF_ERROR
    assert error.__cause__ is None
    assert error.__context__ is None
    assert not getattr(error, "__notes__", ())
    if secret:
        assert secret not in str(error)
        assert secret not in repr(error)


def test_validation_error_clears_unrelated_active_exception_state() -> None:
    secret = "UNRELATED-PRIVATE-EXCEPTION"

    try:
        raise RuntimeError(secret)
    except RuntimeError:
        with pytest.raises(PhraseConfigError) as caught:
            normalize_phrase(None)

    error = caught.value
    assert error.args == (PUBLIC_ERROR,)
    assert error.__context__ is None
    assert error.__cause__ is None
    assert not getattr(error, "__notes__", ())
    assert secret not in repr(error)


def test_module_is_pure_and_has_no_home_assistant_imports() -> None:
    source = Path(voice_phrase.__file__).read_text(encoding="utf-8")
    imported_roots = {
        alias.name.split(".", maxsplit=1)[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_roots.update(
        node.module.split(".", maxsplit=1)[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )
    assert "homeassistant" not in imported_roots


def test_normalize_phrase_applies_nfkc_casefold_yo_and_separators() -> None:
    assert normalize_phrase("  ЁЖИК—вперёд!!! 42_раза🙂 ") == "ежик вперед 42 раза"
    assert normalize_phrase("ＦＯＯ ﬁ Straße") == "foo fi strasse"
    assert normalize_phrase("дом\t7\nподъезд") == "дом 7 подъезд"


@pytest.mark.parametrize("bad", [None, True, 7, b"abc", ["abc"]])
def test_normalize_phrase_requires_an_exact_string(bad: object) -> None:
    assert_fixed_error(lambda: normalize_phrase(bad))


def test_normalize_phrase_rejects_string_subclasses() -> None:
    class StringLookalike(str):
        pass

    assert_fixed_error(lambda: normalize_phrase(StringLookalike("valid phrase")))


@pytest.mark.parametrize("bad", ["", "! !", "ab", "a" * 129, "я" * 65])
def test_normalize_phrase_enforces_normalized_utf8_bounds(bad: str) -> None:
    assert_fixed_error(lambda: normalize_phrase(bad), bad)


def test_normalize_phrase_accepts_inclusive_utf8_bounds() -> None:
    assert normalize_phrase("abc") == "abc"
    assert normalize_phrase("a" * 128) == "a" * 128
    assert normalize_phrase("я" * 64) == "я" * 64


def test_normalize_phrase_bounds_expansion_heavy_input_before_nfkc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_normalize = voice_phrase.unicodedata.normalize
    original_category = voice_phrase.unicodedata.category
    expansion_heavy = "\ufdfa"
    assert len(original_normalize("NFKC", expansion_heavy)) > 1
    calls: list[tuple[str, int]] = []

    def tracked_normalize(form: str, value: str) -> str:
        calls.append((form, len(value)))
        assert form == "NFKC"
        return original_normalize("NFKC", value)

    monkeypatch.setattr(
        voice_phrase,
        "unicodedata",
        SimpleNamespace(normalize=tracked_normalize, category=original_category),
    )

    at_limit = expansion_heavy * 512
    assert_fixed_error(lambda: normalize_phrase(at_limit), at_limit)
    assert calls == [("NFKC", 512)]

    calls.clear()
    over_limit = at_limit + expansion_heavy
    assert_fixed_error(lambda: normalize_phrase(over_limit), over_limit)
    assert calls == []


@pytest.mark.parametrize(
    ("phrases", "salt"),
    [
        (None, SALT),
        (True, SALT),
        (("open door",), SALT),
        ([], SALT),
        ([True], SALT),
        (["valid phrase"], b"short"),
        (["valid phrase"], bytearray(16)),
        (["valid phrase"], memoryview(bytes(16))),
    ],
)
def test_encode_phrase_set_rejects_non_exact_container_items_and_salt(
    phrases: object, salt: object
) -> None:
    assert_fixed_error(lambda: encode_phrase_set(phrases, salt=salt))  # type: ignore[arg-type]


def test_encode_phrase_set_rejects_list_string_and_bytes_subclasses() -> None:
    class ListLookalike(list[str]):
        pass

    class StringLookalike(str):
        pass

    class BytesLookalike(bytes):
        pass

    assert_fixed_error(lambda: encode_phrase_set(ListLookalike(["valid phrase"])))
    assert_fixed_error(lambda: encode_phrase_set([StringLookalike("valid phrase")]))
    assert_fixed_error(
        lambda: encode_phrase_set(["valid phrase"], salt=BytesLookalike(SALT))
    )


def test_encode_phrase_set_enforces_unique_normalized_count() -> None:
    assert_fixed_error(lambda: encode_phrase_set(["!!", "..."], salt=SALT))
    too_many = [f"phrase {index:02d}" for index in range(17)]
    assert_fixed_error(lambda: encode_phrase_set(too_many, salt=SALT))


def test_encode_phrase_set_rejects_raw_count_before_item_processing_or_kdf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    normalization_calls = 0
    kdf_calls = 0

    def unexpected_normalize(_phrase: object) -> str:
        nonlocal normalization_calls
        normalization_calls += 1
        return "valid phrase"

    def unexpected_hash(*_args: object, **_kwargs: object) -> bytes:
        nonlocal kdf_calls
        kdf_calls += 1
        return ZERO_DIGEST

    monkeypatch.setattr(voice_phrase, "normalize_phrase", unexpected_normalize)
    monkeypatch.setattr(voice_phrase, "hash_secret_raw", unexpected_hash)

    assert_fixed_error(lambda: encode_phrase_set(["valid phrase"] * 17, salt=SALT))
    assert normalization_calls == 0
    assert kdf_calls == 0

    assert encode_phrase_set(["valid phrase"] * 16, salt=SALT) == stored_with(
        ZERO_DIGEST
    )
    assert normalization_calls == 16
    assert kdf_calls == 1


def test_encode_phrase_set_normalizes_deduplicates_and_hashes_once_per_phrase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[bytes, bytes, dict[str, object]]] = []

    def fake_hash(secret: bytes, salt: bytes, **kwargs: object) -> bytes:
        calls.append((secret, salt, kwargs))
        return hashlib.sha256(secret + salt).digest()

    monkeypatch.setattr(voice_phrase, "hash_secret_raw", fake_hash)
    stored = encode_phrase_set(["ОТКРОЙ, ДВЕРЬ!", "открой дверь", "ВПЕРЁД"], salt=SALT)

    assert stored == {
        "version": 1,
        "salt": SALT_B64,
        "digests": sorted(
            b64(hashlib.sha256(secret + SALT).digest())
            for secret in ("открой дверь".encode(), "вперед".encode())
        ),
    }
    assert [secret for secret, _, _ in calls] == [
        "открой дверь".encode(),
        "вперед".encode(),
    ]
    assert all(salt == SALT for _, salt, _ in calls)
    assert all(
        kwargs
        == {
            "time_cost": 2,
            "memory_cost": 19456,
            "parallelism": 1,
            "hash_len": 32,
            "type": Type.ID,
            "version": 19,
        }
        for _, _, kwargs in calls
    )


def test_encode_phrase_set_generates_exactly_one_16_byte_salt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[int] = []

    def fake_token_bytes(length: int) -> bytes:
        requested.append(length)
        return SALT

    monkeypatch.setattr(voice_phrase.secrets, "token_bytes", fake_token_bytes)
    stored = encode_phrase_set(["open door"])
    assert requested == [16]
    assert stored["salt"] == SALT_B64


def test_encode_phrase_set_matches_deterministic_argon2id_vector() -> None:
    assert encode_phrase_set(["ОТКРОЙ—ДВЕРЬ", "Подъезд 7"], salt=SALT) == {
        "version": 1,
        "salt": SALT_B64,
        "digests": [
            "1xhIOePsrO2y4g1nASfi7cZw6fYngRVddIf4crWi5dQ=",
            "ZUDC5lzV0xC1uozHAJK07hTcB_PRi_IQbGrDvC-UTUg=",
        ],
    }


def test_documented_argon2_failure_maps_to_sanitized_operational_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plaintext = "PRIVATE VOICE PHRASE"

    def failed_hash(*_args: object, **_kwargs: object) -> bytes:
        raise HashingError(f"argon2 detail containing {plaintext}")

    monkeypatch.setattr(voice_phrase, "hash_secret_raw", failed_hash)
    assert_fixed_kdf_error(lambda: encode_phrase_set([plaintext], salt=SALT), plaintext)


@pytest.mark.parametrize("malformed", [None, b"short", bytearray(ZERO_DIGEST)])
def test_malformed_kdf_return_maps_to_sanitized_operational_error(
    malformed: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    plaintext = "PRIVATE VOICE PHRASE"

    def malformed_hash(*_args: object, **_kwargs: object) -> object:
        return malformed

    monkeypatch.setattr(voice_phrase, "hash_secret_raw", malformed_hash)
    assert_fixed_kdf_error(lambda: encode_phrase_set([plaintext], salt=SALT), plaintext)


class UnexpectedKdfRuntimeError(RuntimeError):
    """Sentinel unexpected KDF error used to prove exception identity."""


@pytest.mark.parametrize("unexpected", [UnexpectedKdfRuntimeError(), MemoryError()])
def test_unexpected_kdf_errors_remain_observable_by_identity_without_plaintext(
    unexpected: BaseException, monkeypatch: pytest.MonkeyPatch
) -> None:
    plaintext = "PRIVATE VOICE PHRASE"

    def failed_hash(*_args: object, **_kwargs: object) -> bytes:
        raise unexpected

    monkeypatch.setattr(voice_phrase, "hash_secret_raw", failed_hash)
    with pytest.raises(type(unexpected)) as caught:
        encode_phrase_set([plaintext], salt=SALT)

    assert caught.value is unexpected
    assert plaintext not in repr(caught.value)


def test_matcher_accepts_exact_storage_and_redacts_plaintext() -> None:
    phrase = "СЕКРЕТНАЯ ФРАЗА"
    stored = encode_phrase_set([phrase, "другая фраза"], salt=SALT)
    matcher = PhraseMatcher.from_stored(stored)

    assert matcher.count == 2
    assert repr(matcher) == "PhraseMatcher(count=2)"
    assert phrase.casefold() not in repr(matcher).casefold()
    assert phrase.casefold() not in repr(stored).casefold()
    assert not hasattr(matcher, "__dict__")
    assert matcher.matches("секретная—фраза") is True
    assert matcher.matches("СЕКРЕТНАЯ ФРАЗА!!!") is True
    assert matcher.matches("другая") is False


@pytest.mark.parametrize(
    "bad",
    [
        None,
        True,
        [],
        {},
        {"version": 1, "salt": SALT_B64, "digests": [], "extra": 1},
        {"version": True, "salt": SALT_B64, "digests": [b64(ZERO_DIGEST)]},
        {"version": 2, "salt": SALT_B64, "digests": [b64(ZERO_DIGEST)]},
        {"version": 1, "salt": b"not text", "digests": [b64(ZERO_DIGEST)]},
        {"version": 1, "salt": SALT_B64, "digests": (b64(ZERO_DIGEST),)},
        {"version": 1, "salt": SALT_B64, "digests": []},
        {
            "version": 1,
            "salt": SALT_B64,
            "digests": sorted(b64(bytes([index]) * 32) for index in range(17)),
        },
    ],
)
def test_matcher_rejects_malformed_stored_shape_and_exact_types(bad: object) -> None:
    assert_fixed_error(lambda: PhraseMatcher.from_stored(bad))


def test_matcher_rejects_stored_container_and_scalar_subclasses() -> None:
    class DictLookalike(dict[str, object]):
        pass

    class ListLookalike(list[str]):
        pass

    class StringLookalike(str):
        pass

    valid = stored_with(ZERO_DIGEST)
    assert_fixed_error(lambda: PhraseMatcher.from_stored(DictLookalike(valid)))
    assert_fixed_error(
        lambda: PhraseMatcher.from_stored({**valid, "salt": StringLookalike(SALT_B64)})
    )
    assert_fixed_error(
        lambda: PhraseMatcher.from_stored(
            {**valid, "digests": ListLookalike([b64(ZERO_DIGEST)])}
        )
    )
    assert_fixed_error(
        lambda: PhraseMatcher.from_stored(
            {**valid, "digests": [StringLookalike(b64(ZERO_DIGEST))]}
        )
    )


@pytest.mark.parametrize(
    ("field", "encoded"),
    [
        pytest.param("salt", "A" * 23, id="salt-under"),
        pytest.param("salt", "A" * 25, id="salt-over"),
        pytest.param("salt", "A" * 1_000_000, id="salt-huge"),
        pytest.param("salt", "é" * 24, id="salt-non-ascii"),
        pytest.param("digest", "A" * 43, id="digest-under"),
        pytest.param("digest", "A" * 45, id="digest-over"),
        pytest.param("digest", "A" * 1_000_000, id="digest-huge"),
        pytest.param("digest", "é" * 44, id="digest-non-ascii"),
    ],
)
def test_matcher_bounds_stored_encodings_before_decode_sort_or_digest_hash(
    field: str, encoded: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    encoded_digests = [b64(ZERO_DIGEST)]
    stored: dict[str, object] = {
        "version": 1,
        "salt": SALT_B64,
        "digests": encoded_digests,
    }
    if field == "salt":
        stored["salt"] = encoded
    else:
        encoded_digests.append(encoded)

    original_set = set

    def unexpected_decode(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("Base64 decoder reached for an invalid encoded length")

    def unexpected_sort(_values: object) -> NoReturn:
        raise AssertionError("digest sort reached for an invalid encoded length")

    def reject_digest_hash(values: object) -> set[object]:
        if values is encoded_digests:
            raise AssertionError("digest hashing reached for an invalid encoded length")
        return original_set(values)  # type: ignore[call-overload]

    monkeypatch.setattr(voice_phrase.base64, "b64decode", unexpected_decode)
    monkeypatch.setattr(voice_phrase, "sorted", unexpected_sort, raising=False)
    monkeypatch.setattr(voice_phrase, "set", reject_digest_hash, raising=False)

    assert_fixed_error(lambda: PhraseMatcher.from_stored(stored))


def test_matcher_accepts_exact_stored_encoding_lengths() -> None:
    stored = stored_with(ZERO_DIGEST, ONE_DIGEST)
    encoded_salt = cast(str, stored["salt"])
    encoded_digests = cast(list[str], stored["digests"])

    assert len(encoded_salt) == 24
    assert all(len(digest) == 44 for digest in encoded_digests)
    assert PhraseMatcher.from_stored(stored).count == 2


@pytest.mark.parametrize(
    "bad",
    [
        {"version": 1, "salt": SALT_B64.rstrip("="), "digests": [b64(ZERO_DIGEST)]},
        {
            "version": 1,
            "salt": "/////////////////////w==",
            "digests": [b64(ZERO_DIGEST)],
        },
        {"version": 1, "salt": "not+a+valid/salt=", "digests": [b64(ZERO_DIGEST)]},
        {"version": 1, "salt": b64(bytes(15)), "digests": [b64(ZERO_DIGEST)]},
        {"version": 1, "salt": SALT_B64, "digests": [b64(bytes(31))]},
        {
            "version": 1,
            "salt": SALT_B64,
            "digests": [b64(ONE_DIGEST), b64(ZERO_DIGEST)],
        },
        {
            "version": 1,
            "salt": SALT_B64,
            "digests": [b64(ZERO_DIGEST), b64(ZERO_DIGEST)],
        },
    ],
)
def test_matcher_rejects_noncanonical_unsorted_or_duplicate_encodings(
    bad: dict[str, object],
) -> None:
    assert_fixed_error(lambda: PhraseMatcher.from_stored(bad))


def test_matcher_uses_one_kdf_and_compares_every_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matcher = PhraseMatcher.from_stored(
        stored_with(ZERO_DIGEST, ONE_DIGEST, TWO_DIGEST)
    )
    derivations: list[tuple[bytes, bytes, dict[str, object]]] = []
    comparisons: list[tuple[bytes, bytes]] = []

    def fake_hash(secret: bytes, salt: bytes, **kwargs: object) -> bytes:
        derivations.append((secret, salt, kwargs))
        return ZERO_DIGEST

    def fake_compare(left: bytes, right: bytes) -> bool:
        comparisons.append((left, right))
        return left == right

    monkeypatch.setattr(voice_phrase, "hash_secret_raw", fake_hash)
    monkeypatch.setattr(voice_phrase, "compare_digest", fake_compare)

    assert matcher.matches("VALID—PHRASE") is True
    assert derivations == [
        (
            b"valid phrase",
            SALT,
            {
                "time_cost": 2,
                "memory_cost": 19456,
                "parallelism": 1,
                "hash_len": 32,
                "type": Type.ID,
                "version": 19,
            },
        )
    ]
    assert comparisons == [
        (ZERO_DIGEST, ZERO_DIGEST),
        (ZERO_DIGEST, ONE_DIGEST),
        (ZERO_DIGEST, TWO_DIGEST),
    ]


@pytest.mark.parametrize("bad", [None, True, 7, b"valid phrase", "x", "! !"])
def test_matcher_fails_closed_for_malformed_transcripts_without_a_kdf(
    bad: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    matcher = PhraseMatcher.from_stored(stored_with(ZERO_DIGEST))
    calls = 0

    def unexpected_hash(*args: object, **kwargs: object) -> bytes:
        nonlocal calls
        calls += 1
        return ZERO_DIGEST

    monkeypatch.setattr(voice_phrase, "hash_secret_raw", unexpected_hash)
    assert matcher.matches(bad) is False
    assert calls == 0


def test_phrase_config_error_ignores_attempted_private_message() -> None:
    secret = "DO-NOT-EXPOSE-THIS-PHRASE"
    error = PhraseConfigError(secret)
    assert error.args == (PUBLIC_ERROR,)
    assert secret not in str(error)
    assert secret not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert not getattr(error, "__notes__", ())
