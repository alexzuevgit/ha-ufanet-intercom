"""Private one-page settings: synthetic HTTP boundaries, no audio/provider work."""

import asyncio
import copy
import importlib
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import test_config_flow as cf
from test_voice_model_options import service, setup_flow


@pytest.fixture
def panel(monkeypatch):
    http = ModuleType("homeassistant.components.http")
    http.HomeAssistantView = object
    http.StaticPathConfig = lambda *args: args
    monkeypatch.setitem(sys.modules, http.__name__, http)
    module = importlib.import_module("custom_components.ufanet_intercom.settings_panel")
    return module


class Request(dict):
    def __init__(self, payload=None, *, admin=True, authenticated=True, raw=None):
        super().__init__()
        if authenticated:
            self["hass_user"] = SimpleNamespace(is_admin=admin)
        body = json.dumps(payload).encode() if raw is None else raw
        self.content = SimpleNamespace(read=AsyncMock(side_effect=[body, b""]))
        self.content_length = len(body)


def environment(panel, *, enabled=False, model="small", fresh=False):
    _, _, entry, flow = setup_flow(enabled=enabled, model=model, fresh=fresh)
    entry.domain = "ufanet_intercom"
    entry.options["unrelated"] = {"preserve": True}
    hass = flow.hass
    hass.data = {}

    def update(target, **updates):
        hass.config_entries.update_calls.append((target, updates))
        for key, value in updates.items():
            setattr(target, key, value)

    hass.config_entries.async_update_entry = update
    return entry, hass, panel.SettingsView(hass), panel.ModelsView(hass)


def data(response):
    assert response.headers["Cache-Control"] == "no-store"
    return json.loads(response.text)


async def draft(view, entry, **changes):
    loaded = data(await view.get(Request(), entry.entry_id))
    return {
        **{key: loaded[key] for key in service()},
        "model": loaded["model"],
        "revision": loaded["revision"],
        "token_action": "keep",
        "confirm_token_origin": False,
        **changes,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["get", "save", "models"])
@pytest.mark.parametrize(
    "access,status",
    [("anonymous", 401), ("user", 403), ("wrong", 404), ("missing", 404)],
)
async def test_every_endpoint_auth_admin_exact_domain_before_read(
    panel, kind, access, status
):
    entry, _, view, models = environment(panel)
    if access == "wrong":
        entry.domain = "other"
    if access in ("wrong", "missing"):
        entry.options = None  # Must not touch private state on invalid entry.
    request = Request({}, admin=access != "user", authenticated=access != "anonymous")
    entry_id = "missing" if access == "missing" else entry.entry_id
    response = await (
        view.get if kind == "get" else models.post if kind == "models" else view.post
    )(request, entry_id)
    assert response.status == status
    assert set(data(response)) == {"error"}
    assert view.requires_auth is True and models.requires_auth is True


@pytest.mark.asyncio
async def test_admin_open_actual_key_no_catalog_and_paused_save_preserves_rest(
    panel, monkeypatch
):
    entry, _, view, _ = environment(panel)
    before = copy.deepcopy(entry.options)
    discover = AsyncMock(side_effect=AssertionError("No discovery on open/save"))
    monkeypatch.setattr(panel, "async_discover_models", discover)
    loaded = data(await view.get(Request(), entry.entry_id))
    assert loaded["token"] == "SYNTHETIC-ADMIN-TOKEN"
    assert loaded["has_targets"] is True
    payload = await draft(
        view,
        entry,
        model="Exact/Model:V2",
        endpoint="https://stt.invalid/new/audio/transcriptions",
    )
    response = await view.post(Request(payload), entry.entry_id)
    assert response.status == 200
    root = entry.options["voice_phrase"]
    assert root["model"] == "Exact/Model:V2" and root["enabled"] is False
    assert root["targets"] == before["voice_phrase"]["targets"]
    assert entry.options["unrelated"] == before["unrelated"]
    assert root["token"] == loaded["token"]
    assert data(response)["revision"] != loaded["revision"]
    discover.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,key,confirm,ok",
    [
        ("keep", "SYNTHETIC-ADMIN-TOKEN", False, False),
        ("replace", "SYNTHETIC-ADMIN-TOKEN", False, False),
        ("keep", "", False, False),
        ("replace", "NEW-SYNTHETIC", False, True),
        ("replace", "", False, True),
        ("keep", "SYNTHETIC-ADMIN-TOKEN", True, True),
    ],
)
@pytest.mark.parametrize("checking", [False, True])
async def test_retained_key_origin_guard_includes_unchanged_revealed_value(
    panel, monkeypatch, action, key, confirm, ok, checking
):
    entry, _, view, models = environment(panel)
    before = copy.deepcopy(entry.options)
    discover = AsyncMock(return_value=SimpleNamespace(models=("small",), error=None))
    monkeypatch.setattr(panel, "async_discover_models", discover)
    payload = await draft(
        view,
        entry,
        endpoint="https://other.invalid/v1/audio/transcriptions",
        token_action=action,
        token=key,
        confirm_token_origin=confirm,
    )
    response = await (models if checking else view).post(
        Request(payload), entry.entry_id
    )
    assert response.status == (200 if ok else 400)
    if not ok:
        assert data(response) == {"error": "token_origin_changed"}
    assert discover.await_count == int(checking and ok)
    if checking or not ok:
        assert entry.options == before


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["", "default", " a", 3])
async def test_model_required_only_on_save_check_does_not_persist(
    panel, monkeypatch, model
):
    entry, _, view, models = environment(panel, model="")
    before = copy.deepcopy(entry.options)
    monkeypatch.setattr(
        panel,
        "async_discover_models",
        AsyncMock(return_value=SimpleNamespace(models=(), error="models_unavailable")),
    )
    payload = await draft(view, entry, model=model)
    response = await view.post(Request(payload), entry.entry_id)
    assert data(response) == {"error": "invalid_model"}
    if isinstance(model, str):
        response = await models.post(Request(payload), entry.entry_id)
        assert data(response) == {"models": [], "error": "models_unavailable"}
    assert entry.options == before


@pytest.mark.asyncio
async def test_legacy_pause_blank_model_and_fresh_paused_setup(panel, monkeypatch):
    monkeypatch.setattr(
        panel, "async_discover_models", AsyncMock(side_effect=AssertionError)
    )
    entry, _, view, _ = environment(panel, enabled=True, model="")
    before = copy.deepcopy(entry.options["voice_phrase"])
    response = await view.post(
        Request(await draft(view, entry, enabled=False)), entry.entry_id
    )
    assert response.status == 200
    assert entry.options["voice_phrase"] == {**before, "enabled": False}
    response = await view.post(Request(await draft(view, entry)), entry.entry_id)
    assert data(response) == {"error": "invalid_model"}
    entry, _, view, _ = environment(panel, fresh=True)
    response = await view.post(
        Request(
            await draft(
                view,
                entry,
                endpoint=service()["endpoint"],
                model="manual",
                token_action="replace",
                token="NEW-SYNTHETIC",
            )
        ),
        entry.entry_id,
    )
    assert response.status == 200
    assert entry.options["voice_phrase"]["targets"] == {}
    response = await view.post(
        Request(await draft(view, entry, enabled=True)), entry.entry_id
    )
    assert data(response) == {"error": "no_voice_targets"}


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["options", "data"])
async def test_revision_rejects_other_flow_or_account_edits_before_write(panel, target):
    entry, _, view, _ = environment(panel)
    payload = await draft(view, entry, model="new")
    getattr(entry, target)["concurrent"] = True
    before = copy.deepcopy(entry.options)
    response = await view.post(Request(payload), entry.entry_id)
    assert response.status == 409 and data(response) == {"error": "stale_revision"}
    assert entry.options == before


@pytest.mark.asyncio
async def test_check_races_busy_stale_revision_and_cancellation_release(
    panel, monkeypatch
):
    entry, _, view, models = environment(panel)
    entered, release = asyncio.Event(), asyncio.Event()

    async def discover(_config):
        entered.set()
        await release.wait()
        return SimpleNamespace(models=("old",), error=None)

    monkeypatch.setattr(panel, "async_discover_models", discover)
    payload = await draft(view, entry)
    task = asyncio.create_task(models.post(Request(payload), entry.entry_id))
    await entered.wait()
    for endpoint in (models, view):
        response = await endpoint.post(Request(payload), entry.entry_id)
        assert response.status == 409 and data(response) == {"error": "busy"}
    entry.options["concurrent"] = True
    release.set()
    assert data(await task) == {"error": "stale_revision"}
    entered.clear()
    release.clear()
    payload = await draft(view, entry)
    task = asyncio.create_task(models.post(Request(payload), entry.entry_id))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    response = await view.post(Request(payload), entry.entry_id)
    assert response.status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [b"not json", b"[]", b"x" * 17000],
    ids=["invalid-json", "array", "oversized"],
)
async def test_request_bounds_and_fixed_errors(panel, raw):
    entry, _, view, _ = environment(panel)
    response = await view.post(Request(raw=raw), entry.entry_id)
    assert response.status == 400 and data(response) == {"error": "invalid_request"}


@pytest.mark.asyncio
async def test_fallback_optional_omitted_key_pauses_and_saves(monkeypatch):
    _, _, _, flow = setup_flow(enabled=True)
    payload = service()
    del payload["token"]
    result = await flow.async_step_voice_service(payload)
    assert result["type"] == "create_entry"
    _, _, _, flow = setup_flow()
    monkeypatch.setattr(
        cf.config_flow_module,
        "async_discover_models",
        AsyncMock(return_value=SimpleNamespace(models=(), error="models_empty")),
    )
    assert (await flow.async_step_voice_service(payload))["step_id"] == "voice_model"
    source = Path(cf.config_flow_module.__file__).read_text()
    assert "vol.Optional(_VOICE_TOKEN" in source


def test_panel_asset_contract():
    root = Path(__file__).resolve().parents[1] / "custom_components/ufanet_intercom"
    source = (root / "frontend/ufanet-settings.js").read_text()
    for name in ("endpoint", "token", "model", "enabled", "allow_insecure_http"):
        assert f'name="{name}"' in source
    for action in ("check", "save", "reveal", "back"):
        assert f'data-action="{action}"' in source
    for label in (
        "Проверить связь и получить модели",
        "Сохранить",
        "Кодовые фразы по домофонам",
        "Обновить список домофонов",
        "Сбросить настройки распознавания",
    ):
        assert label in source
    assert "Готово" not in source
    assert "Назад к интеграции" not in source
    assert "К настройкам распознавания" not in source
    assert "localStorage" not in source and "console." not in source
    assert 'type="button" data-action="check"' in source
    assert 'name="model"' in source and 'list="models"' in source
    assert json.loads((root / "manifest.json").read_text())["after_dependencies"] == [
        "ffmpeg",
        "frontend",
        "http",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["********", "••••••", "●●●●"])
async def test_never_persist_mask_characters(panel, token):
    entry, _, view, _ = environment(panel)
    before = copy.deepcopy(entry.options)
    response = await view.post(
        Request(await draft(view, entry, token_action="replace", token=token)),
        entry.entry_id,
    )
    assert data(response) == {"error": "invalid_stt"}
    assert entry.options == before


@pytest.mark.asyncio
async def test_panel_registration_once_across_accounts_and_reloads(panel, monkeypatch):
    from homeassistant import components

    registered = AsyncMock()
    custom = SimpleNamespace(async_register_panel=registered)
    monkeypatch.setattr(components, "panel_custom", custom, raising=False)
    views = []
    paths = AsyncMock()
    hass = SimpleNamespace(
        data={},
        http=SimpleNamespace(
            async_register_static_paths=paths, register_view=views.append
        ),
    )
    await asyncio.gather(
        panel.async_register_settings_panel(hass),
        panel.async_register_settings_panel(hass),
    )
    await panel.async_register_settings_panel(hass)
    assert paths.await_count == 1 and len(views) == 2
    assert [view.requires_auth for view in views] == [True, True]
    registered.assert_awaited_once_with(
        hass,
        frontend_url_path="ufanet-settings",
        webcomponent_name="ufanet-settings-panel",
        module_url="/ufanet_intercom/ufanet-settings.js?v=2.1.0b7",
        require_admin=True,
        config_panel_domain="ufanet_intercom",
        embed_iframe=False,
        sidebar_title=None,
    )
    assert Path(paths.call_args.args[0][0][1]).is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize("frontend_ready", [False, True])
async def test_global_setup_no_provider_or_frontend_bootstrap(
    panel, monkeypatch, frontend_ready
):
    integration = importlib.import_module("custom_components.ufanet_intercom")
    const = ModuleType("homeassistant.const")
    const.EVENT_COMPONENT_LOADED = "component_loaded"
    monkeypatch.setitem(sys.modules, const.__name__, const)
    register = AsyncMock()
    monkeypatch.setattr(panel, "async_register_settings_panel", register)
    events = []
    removed = []

    def listen(event, handler):
        events.append((event, handler))
        return lambda: removed.append(True)

    hass = SimpleNamespace(
        config=SimpleNamespace(components={"frontend"} if frontend_ready else set()),
        bus=SimpleNamespace(async_listen=listen),
    )
    assert await integration.async_setup(hass, {}) is True
    assert register.await_count == int(frontend_ready)
    if not frontend_ready:
        assert len(events) == 1
        await events[0][1](SimpleNamespace(data={"component": "http"}))
        register.assert_not_called()
        await events[0][1](SimpleNamespace(data={"component": "frontend"}))
        assert removed == [True]
    register.assert_awaited_once_with(hass)
