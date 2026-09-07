"""Explicit native Options model choice without migrating legacy blank settings."""

from types import SimpleNamespace

import pytest

import test_config_flow as cf


@pytest.fixture
def catalog(monkeypatch):
    calls = []

    async def discover(config):
        calls.append(config)
        return SimpleNamespace(
            models=("gigaam-v3-e2e-rnnt", "large-v3", "large-v3-turbo", "small"),
            error=None,
        )

    monkeypatch.setattr(
        cf.config_flow_module, "async_discover_models", discover, raising=False
    )
    return calls


def setup_flow(*, enabled=False, model="", fresh=False):
    target = cf.voice_door(cf.DoorSpec(5300, "Synthetic model selection"))
    original = {} if fresh else cf.editable_voice_options(target, enabled=enabled)
    if not fresh:
        original[cf.VOICE_PHRASE_OPTIONS_ROOT]["model"] = model
    entry = cf.voice_entry(target, options=original)
    flow, _ = cf.options_flow_for(entry)
    return target, original, entry, flow


def service(**changes):
    return {
        "enabled": False,
        "endpoint": "https://stt.invalid/v1/audio/transcriptions",
        "token": "",
        "allow_insecure_http": False,
        **changes,
    }


@pytest.mark.asyncio
async def test_service_render_no_network_explicit_next_fetches_then_requires_model(
    catalog,
):
    _, original, entry, flow = setup_flow()
    form = await flow.async_step_voice_service()
    cf.assert_exact_schema(form, set(service()))
    assert catalog == []
    form = await flow.async_step_voice_service(service())
    cf.assert_form(form, "voice_model")
    cf.assert_exact_schema(form, {"model"})
    assert len(catalog) == 1 and catalog[0].token == "SYNTHETIC-ADMIN-TOKEN"
    assert entry.options == original
    assert flow._voice_candidate.model == ""
    selector = form["data_schema"]["model"]
    assert selector.config.config == {
        "options": ["gigaam-v3-e2e-rnnt", "large-v3", "large-v3-turbo", "small"],
        "custom_value": True,
        "mode": "dropdown",
    }
    saved = await flow.async_step_voice_model({"model": "Org/Exact_ID:v1.2"})
    assert saved["data"][cf.VOICE_PHRASE_OPTIONS_ROOT] == {
        **original[cf.VOICE_PHRASE_OPTIONS_ROOT],
        "model": "Org/Exact_ID:v1.2",
    }
    assert len(catalog) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        {},
        {"model": ""},
        {"model": " "},
        {"model": " small"},
        {"model": "small "},
        {"model": "a\nb"},
        {"model": 1},
        {"model": "default"},
        {"model": "small", "extra": True},
    ],
)
async def test_enable_and_paused_save_reject_invalid_or_implicit_model(catalog, value):
    _, original, entry, flow = setup_flow()
    await flow.async_step_voice_service(service(enabled=True))
    result = await flow.async_step_voice_model(value)
    cf.assert_form(result, "voice_model")
    assert result["errors"] == {"model": "invalid_model"}
    assert flow._voice_candidate.model == ""
    assert entry.options == original and len(catalog) == 1


@pytest.mark.asyncio
async def test_preserve_nonempty_model_as_only_default_no_first_assignment(
    catalog, monkeypatch
):
    for existing in ("", "not-in-catalog"):
        _, _, _, flow = setup_flow(model=existing)
        defaults = {}

        def required(key, **kwargs):
            defaults[key] = kwargs.get("default")
            return key

        with monkeypatch.context() as context:
            context.setattr(cf.config_flow_module.vol, "Required", required)
            await flow.async_step_voice_service(service())
        assert defaults["model"] == (existing or None)
        assert flow._voice_candidate.model == existing


@pytest.mark.asyncio
@pytest.mark.parametrize("populated", [False, True])
async def test_real_populated_disable_preserves_blank_legacy_without_network(
    catalog, populated
):
    _, original, _, flow = setup_flow(enabled=True)
    result = await flow.async_step_voice_service(
        service() if populated else {"enabled": False}
    )
    assert result["data"][cf.VOICE_PHRASE_OPTIONS_ROOT] == {
        **original[cf.VOICE_PHRASE_OPTIONS_ROOT],
        "enabled": False,
    }
    assert catalog == []


@pytest.mark.asyncio
@pytest.mark.parametrize("edited", ["endpoint", "token", "allow_insecure_http"])
async def test_disable_with_edits_must_not_discard_them(catalog, edited):
    _, original, entry, flow = setup_flow(enabled=True, model="small")
    value = {
        "endpoint": "https://stt.invalid/new/v1/audio/transcriptions",
        "token": "SYNTHETIC-NEW-KEY",
        "allow_insecure_http": True,
    }[edited]
    form = await flow.async_step_voice_service(service(**{edited: value}))
    cf.assert_form(form, "voice_model")
    assert len(catalog) == 1
    result = await flow.async_step_voice_model({"model": "large-v3"})
    root = result["data"][cf.VOICE_PHRASE_OPTIONS_ROOT]
    assert root[edited] == value and root["enabled"] is False
    assert root["targets"] == original[cf.VOICE_PHRASE_OPTIONS_ROOT]["targets"]
    assert entry.options == original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint",
    [
        "https://other.invalid/v1/audio/transcriptions",
        "http://stt.invalid/v1/audio/transcriptions",
        "https://stt.invalid:8443/v1/audio/transcriptions",
    ],
)
async def test_saved_token_never_follows_origin_change(catalog, endpoint):
    _, original, entry, flow = setup_flow()
    result = await flow.async_step_voice_service(service(endpoint=endpoint))
    cf.assert_form(result, "voice_service")
    assert result["errors"]
    assert catalog == [] and entry.options == original
    if endpoint.startswith("https:"):
        cf.assert_form(
            await flow.async_step_voice_service(
                service(endpoint=endpoint, token="SYNTHETIC-NEW")
            ),
            "voice_model",
        )
        assert catalog[0].token == "SYNTHETIC-NEW"


@pytest.mark.asyncio
async def test_same_origin_explicit_default_port_reuses_key(catalog):
    _, _, _, flow = setup_flow()
    cf.assert_form(
        await flow.async_step_voice_service(
            service(endpoint="https://STT.invalid:443/prefix/v1/audio/transcriptions")
        ),
        "voice_model",
    )
    assert catalog[0].token == "SYNTHETIC-ADMIN-TOKEN"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", ["models_unsupported", "models_unavailable", "models_empty"]
)
async def test_catalog_fallback_is_honest_and_manual_save_preserves_all(
    monkeypatch, error
):
    async def discover(_config):
        return SimpleNamespace(models=(), error=error)

    monkeypatch.setattr(
        cf.config_flow_module, "async_discover_models", discover, raising=False
    )
    _, original, entry, flow = setup_flow()
    form = await flow.async_step_voice_service(service())
    assert form["errors"] == {"base": error}
    assert form["data_schema"]["model"].config.config["custom_value"] is True
    assert entry.options == original
    result = await flow.async_step_voice_model({"model": "large-v3"})
    assert result["data"][cf.VOICE_PHRASE_OPTIONS_ROOT] == {
        **original[cf.VOICE_PHRASE_OPTIONS_ROOT],
        "model": "large-v3",
    }


@pytest.mark.asyncio
async def test_legacy_blank_phrase_edit_and_reset_do_not_request_catalog(catalog):
    target, original, _, flow = setup_flow()
    cf.assert_form(
        await flow.async_step_voice_device({"target": target.key}), "voice_phrases"
    )
    saved = await flow.async_step_voice_phrases({"phrases": ""})
    assert saved["data"] == original
    _, _, _, reset = setup_flow()
    await reset.async_step_voice_reset({"acknowledge": True})
    assert catalog == []


@pytest.mark.asyncio
async def test_fresh_paused_model_then_existing_device_phrase_workflow(catalog):
    target, _, entry, flow = setup_flow(fresh=True)
    cf.assert_form(
        await flow.async_step_voice_service(service(token="SYNTHETIC-NEW")),
        "voice_model",
    )
    cf.assert_form(
        await flow.async_step_voice_model({"model": "large-v3-turbo"}), "voice_device"
    )
    await flow.async_step_voice_device({"target": target.key})
    saved = await flow.async_step_voice_phrases({"phrases": "Синтетическая фраза"})
    root = saved["data"][cf.VOICE_PHRASE_OPTIONS_ROOT]
    assert root["model"] == "large-v3-turbo" and root["enabled"] is False
    assert entry.options == {}
