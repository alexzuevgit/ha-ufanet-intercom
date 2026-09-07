"""User-assigned HA device names label voice choices, never route identities."""

from types import SimpleNamespace

import pytest

import test_config_flow as cf


def registry_for(monkeypatch, entries):
    calls = []

    def get_device(*, identifiers):
        calls.append(identifiers)
        key = next(iter(identifiers))[1]
        return entries.get(key)

    monkeypatch.setattr(
        cf.config_flow_module,
        "dr",
        SimpleNamespace(
            async_get=lambda _hass: SimpleNamespace(async_get_device=get_device)
        ),
        raising=False,
    )
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_name", "device_name", "expected"),
    [
        ("Мой домофон улица", "Имя провайдера", "Мой домофон улица"),
        (None, "Имя устройства HA", "Имя устройства HA"),
        ("  ", "Имя устройства HA", "Имя устройства HA"),
        (None, None, "Исходное имя провайдера"),
    ],
)
async def test_selector_and_phrase_heading_use_ha_name(
    monkeypatch, user_name, device_name, expected
):
    target = cf.voice_door(cf.DoorSpec(5200, "Исходное имя провайдера"))
    original = cf.editable_voice_options(target, enabled=False)
    entry = cf.voice_entry(target, options=original)
    registry_for(
        monkeypatch,
        {
            target.key: SimpleNamespace(
                name_by_user=user_name,
                name=device_name,
                config_entries={entry.entry_id},
            )
        },
    )
    flow, _ = cf.options_flow_for(entry)
    selector = await flow.async_step_voice_device()
    assert selector["data_schema"]["target"] == {target.key: expected}
    editor = await flow.async_step_voice_device({"target": target.key})
    assert editor["description_placeholders"]["target_name"] == expected
    assert entry.options == original
    unchanged = await flow.async_step_voice_phrases({"phrases": ""})
    assert unchanged["data"] == original


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [True, False])
async def test_missing_or_other_account_device_uses_provider_fallback(
    monkeypatch, missing
):
    target = cf.voice_door(cf.DoorSpec(5201, "Fallback entrance"))
    entry = cf.voice_entry(target, options=cf.editable_voice_options(target))
    devices = (
        {}
        if missing
        else {
            target.key: SimpleNamespace(
                name_by_user="Other account",
                name="Other",
                config_entries={"another-entry"},
            )
        }
    )
    calls = registry_for(monkeypatch, devices)
    flow, _ = cf.options_flow_for(entry)
    result = await flow.async_step_voice_device()
    assert result["data_schema"]["target"] == {target.key: target.display_name}
    assert calls == [{(cf.config_flow_module.DOMAIN, target.key)}]


@pytest.mark.asyncio
async def test_duplicate_names_disambiguate_without_changing_keys(monkeypatch):
    first = cf.voice_door(cf.DoorSpec(5202, "Provider one"))
    second = cf.voice_door(cf.DoorSpec(5203, "Provider two"))
    entry = cf.voice_entry(first, options=cf.editable_voice_options(first))
    entry.runtime_data.coordinator.data = {first.key: first, second.key: second}
    entry.runtime_data.rtsp_proxy.stream_url = lambda key: (
        f"rtsp://127.0.0.1:18092/{key}" if key in (first.key, second.key) else None
    )
    registry_for(
        monkeypatch,
        {
            t.key: SimpleNamespace(
                name_by_user="Мой домофон",
                name=t.display_name,
                config_entries={entry.entry_id},
            )
            for t in (first, second)
        },
    )
    flow, _ = cf.options_flow_for(entry)
    selected = await flow.async_step_voice_device()
    expected = {t.key: f"Мой домофон ({t.key[:8]})" for t in (first, second)}
    assert selected["data_schema"]["target"] == expected
    editor = await flow.async_step_voice_device({"target": first.key})
    assert editor["description_placeholders"]["target_name"] == expected[first.key]


@pytest.mark.asyncio
async def test_renaming_updates_form_without_changing_saved_phrase_binding(monkeypatch):
    target = cf.voice_door(cf.DoorSpec(5204, "Provider entrance"))
    original = cf.editable_voice_options(target, legacy=True, enabled=False)
    entry = cf.voice_entry(target, options=original)
    device = SimpleNamespace(
        name_by_user="Старое имя",
        name=target.display_name,
        config_entries={entry.entry_id},
    )
    registry_for(monkeypatch, {target.key: device})
    flow, _ = cf.options_flow_for(entry)
    assert (await flow.async_step_voice_device())["data_schema"]["target"][
        target.key
    ] == "Старое имя"
    device.name_by_user = "Новое имя улица"
    form = await flow.async_step_voice_device({"target": target.key})
    assert form["description_placeholders"]["target_name"] == "Новое имя улица"
    assert form["step_id"] == "voice_phrases_legacy"
    assert (await flow.async_step_voice_phrases_legacy({"phrases": ""}))[
        "data"
    ] == original
