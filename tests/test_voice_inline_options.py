"""Existing native contracts consumed by the inline panel (synthetic only)."""

import copy
from dataclasses import replace

import pytest

import test_config_flow as cf


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["fresh", "replaced", "readable", "legacy"])
async def test_inline_empty_payload_is_explicit_and_target_isolated(kind, monkeypatch):
    target = cf.voice_door(cf.DoorSpec(5300, "Synthetic inline target"))
    other = cf.voice_door(cf.DoorSpec(5301, "Synthetic other target"))
    original = cf.editable_voice_options(target, legacy=kind == "legacy", enabled=False)
    root = original[cf.VOICE_PHRASE_OPTIONS_ROOT]
    other_record = cf.editable_voice_options(other)[cf.VOICE_PHRASE_OPTIONS_ROOT][
        "targets"
    ][other.key]
    root["targets"][other.key] = other_record
    if kind == "fresh":
        del root["targets"][target.key]
    elif kind == "replaced":
        root["targets"][target.key]["binding"] = "f" * 64
    before = copy.deepcopy(original)
    entry = cf.voice_entry(target, options=original)
    flow, _ = cf.options_flow_for(entry)
    form, defaults = await cf.phrase_form_defaults(flow, target.key, monkeypatch)
    assert entry.options == before  # Autoload never persists or enables.
    if kind in ("fresh", "replaced", "legacy"):
        assert defaults["phrases"] == ""
    assert form["step_id"] == (
        "voice_phrases_legacy" if kind == "legacy" else "voice_phrases"
    )
    result = await flow.async_step_voice_phrases(
        {"phrases": "", "clear_phrases": kind != "legacy"}
    )
    assert result["type"] == cf.FlowResultType.CREATE_ENTRY
    expected = copy.deepcopy(before)
    if kind != "legacy":
        expected[cf.VOICE_PHRASE_OPTIONS_ROOT]["targets"].pop(target.key, None)
    assert result["data"] == expected
    assert entry.options == before  # Flow manager, not reads, commits the result.
    assert result["data"][cf.VOICE_PHRASE_OPTIONS_ROOT]["enabled"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["untrusted", "rebound"])
async def test_inline_clear_revalidates_loaded_binding(change, monkeypatch):
    target = cf.voice_door(cf.DoorSpec(5302, "Synthetic changing target"))
    original = cf.editable_voice_options(target, enabled=False)
    entry = cf.voice_entry(target, options=original)
    flow, _ = cf.options_flow_for(entry)
    await cf.phrase_form_defaults(flow, target.key, monkeypatch)
    changed = (
        replace(target, trusted=False)
        if change == "untrusted"
        else replace(target, binding="f" * 64)
    )
    entry.runtime_data.coordinator.data = {target.key: changed}
    result = await flow.async_step_voice_phrases({"phrases": "", "clear_phrases": True})
    assert result["type"] == cf.FlowResultType.FORM
    assert result["errors"] == {"base": "stale_target"}
    assert result["description_placeholders"] == {
        "target_name": "—",
        "phrase_count": "0",
    }
    assert entry.options == original
    assert flow._voice_target is None
