"""Synthetic strict call-history and observed-pulse tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.ufanet_intercom.history import (
    HISTORY_PATH,
    CallHistoryManager,
    HistoryProtocolError,
    parse_call_history,
)

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
BASE_TIME = NOW - timedelta(seconds=1)


def row(
    uid: object = "synthetic-call-a",
    *,
    called_at: object = BASE_TIME,
    camera: object = "synthetic-camera-a",
    house: object = 7001,
    **extra: object,
) -> dict[str, object]:
    value: dict[str, object] = {
        "uuid": uid,
        "called_at": called_at.isoformat()
        if isinstance(called_at, datetime)
        else called_at,
        "camera_number": camera,
        "house_id": house,
    }
    value.update(extra)
    return value


def page(
    *rows: dict[str, object], next_: object = None, previous: object = None
) -> dict[str, object]:
    return {
        "count": len(rows),
        "next": next_,
        "previous": previous,
        "results": list(rows),
    }


def test_history_path_and_strict_parser_discards_extra_fields() -> None:
    parsed = parse_call_history(page(row(extra_private_field="discarded")))
    assert HISTORY_PATH == "/api/v1/skuds/call-history/?page=1&page_size=10"
    assert len(parsed.rows) == 1
    assert parsed.rows[0].uuid == "synthetic-call-a"
    assert parsed.rows[0].routing_key == ("synthetic-camera-a", 7001)
    assert not hasattr(parsed.rows[0], "extra_private_field")


@pytest.mark.parametrize(
    "bad",
    [
        {"count": True, "next": None, "previous": None, "results": []},
        {"count": 1, "next": 1, "previous": None, "results": []},
        {"count": 1, "next": None, "previous": [], "results": []},
        {"count": 1, "next": None, "previous": None, "results": [row(uuid="")]},
        {"count": 1, "next": None, "previous": None, "results": [row(uuid=1)]},
        {"count": 1, "next": None, "previous": None, "results": [row(called_at="bad")]},
        {
            "count": 1,
            "next": None,
            "previous": None,
            "results": [row(called_at=NOW.isoformat().replace("+00:00", ""))],
        },
        {"count": 1, "next": None, "previous": None, "results": [row(camera=" ")]},
        {"count": 1, "next": None, "previous": None, "results": [row(house=True)]},
        {"count": 1, "next": None, "previous": None, "results": [row(house=0)]},
    ],
)
def test_history_parser_rejects_malformed_contract(bad: dict[str, object]) -> None:
    with pytest.raises(HistoryProtocolError):
        parse_call_history(bad)


def test_history_parser_rejects_duplicate_keys_nan_and_oversized_values() -> None:
    with pytest.raises(HistoryProtocolError):
        parse_call_history(
            {"count": 1, "next": None, "previous": None, "results": [], "extra": 1}
        )
    with pytest.raises(HistoryProtocolError):
        parse_call_history(
            {
                "count": 1,
                "next": None,
                "previous": None,
                "results": [row(uid="x" * 129)],
            }
        )


def test_history_parser_allows_duplicate_row_ids_for_runtime_dedup() -> None:
    duplicate_rows = page(row(uid="same"), row(uid="same"))
    parsed = parse_call_history(duplicate_rows)
    assert [item.uuid for item in parsed.rows] == ["same", "same"]
    too_many = page(*(row(uid=f"id-{index}") for index in range(11)))
    with pytest.raises(HistoryProtocolError):
        parse_call_history(too_many)


def door(key: str, camera: str, house: int) -> SimpleNamespace:
    return SimpleNamespace(key=key, trusted=True, cctv_number=camera, house=house)


def manager() -> CallHistoryManager:
    return CallHistoryManager(
        mapping=(
            door("door-a", "synthetic-camera-a", 7001),
            door("door-b", "synthetic-camera-b", 7002),
        ),
        monotonic=lambda: 100.0,
    )


def test_cold_baseline_is_available_but_never_pulses() -> None:
    state = manager()
    result = state.process(page(row()), now=NOW, monotonic=100.0)
    assert result.baseline is True
    assert result.pulses == ()
    assert state.available_for("door-a") is True
    assert state.is_on_for("door-a", 100.0) is False


def test_fresh_exact_mapping_pulses_once_and_duplicate_reorder_do_not_retrigger() -> (
    None
):
    state = manager()
    state.process(
        page(row(uid="old", called_at=NOW - timedelta(seconds=2))),
        now=NOW,
        monotonic=100.0,
    )
    fresh = row(uid="new", called_at=NOW - timedelta(seconds=1))
    result = state.process(
        page(fresh, row(uid="old", called_at=NOW - timedelta(seconds=2))),
        now=NOW,
        monotonic=103.0,
    )
    assert result.pulses == ("door-a",)
    assert state.is_on_for("door-a", 103.0) is True
    duplicate = state.process(
        page(row(uid="old", called_at=NOW - timedelta(seconds=2)), fresh),
        now=NOW,
        monotonic=106.0,
    )
    assert duplicate.pulses == ()
    assert state.is_on_for("door-a", 106.0) is True
    assert state.is_on_for("door-a", 109.1) is False


def test_multiple_events_route_exactly_and_unmapped_or_ambiguous_never_pulse() -> None:
    state = CallHistoryManager(
        mapping=(
            door("door-a", "synthetic-camera-a", 7001),
            door("door-b", "synthetic-camera-a", 7001),
            door("door-c", "synthetic-camera-c", 7003),
        ),
        monotonic=lambda: 0.0,
    )
    state.process(page(row(uid="baseline")), now=NOW, monotonic=0.0)
    result = state.process(
        page(
            row(uid="unmapped", camera="synthetic-camera-z", house=7001),
            row(uid="ambiguous", camera="synthetic-camera-a", house=7001),
            row(uid="mapped", camera="synthetic-camera-c", house=7003),
            row(uid="baseline"),
        ),
        now=NOW,
        monotonic=3.0,
    )
    assert result.pulses == ("door-c",)


def test_stale_and_future_rows_are_consumed_without_pulsing() -> None:
    state = manager()
    state.process(
        page(row(uid="baseline", called_at=NOW - timedelta(seconds=2))),
        now=NOW,
        monotonic=0.0,
    )
    result = state.process(
        page(
            row(uid="stale", called_at=NOW - timedelta(seconds=31)),
            row(uid="future", called_at=NOW + timedelta(seconds=6)),
            row(uid="baseline", called_at=NOW - timedelta(seconds=2)),
        ),
        now=NOW,
        monotonic=3.0,
    )
    assert result.pulses == ()


def test_failure_clears_pulses_and_successful_rebaseline_does_not_fire() -> None:
    state = manager()
    state.process(page(row(uid="baseline")), now=NOW, monotonic=0.0)
    state.process(page(row(uid="new"), row(uid="baseline")), now=NOW, monotonic=3.0)
    assert state.is_on_for("door-a", 3.0) is True
    state.fail()
    assert state.available_for("door-a") is False
    assert state.is_on_for("door-a", 3.0) is False
    result = state.process(page(row(uid="recovery")), now=NOW, monotonic=6.0)
    assert result.baseline is True
    assert result.pulses == ()
    assert state.available_for("door-a") is True


def test_gap_and_anchor_loss_fail_closed_and_require_rebaseline() -> None:
    state = manager()
    state.process(page(row(uid="anchor"), row(uid="older")), now=NOW, monotonic=0.0)
    gap = state.process(page(row(uid="new")), now=NOW, monotonic=20.0)
    assert gap.failed is True
    assert state.available_for("door-a") is False
    recovery = state.process(page(row(uid="fresh-baseline")), now=NOW, monotonic=23.0)
    assert recovery.baseline is True
    assert recovery.pulses == ()


def test_stop_clears_availability_and_pulses() -> None:
    state = manager()
    state.process(page(row()), now=NOW, monotonic=0.0)
    state.stop()
    assert state.available_for("door-a") is False
    assert state.is_on_for("door-a", 0.0) is False


def test_mapping_refresh_rebaselines_without_synthesizing_a_pulse() -> None:
    state = manager()
    state.process(page(row()), now=NOW, monotonic=0.0)
    state.set_mapping(
        (
            door("door-a", "synthetic-camera-a", 7001),
            door("door-c", "synthetic-camera-c", 7003),
        )
    )
    result = state.process(
        page(row(uid="mapping-refresh", camera="synthetic-camera-c", house=7003)),
        now=NOW,
        monotonic=3.0,
    )
    assert result.baseline is True
    assert result.pulses == ()
