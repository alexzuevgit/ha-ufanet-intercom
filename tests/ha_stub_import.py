"""Install tiny import-only Home Assistant stubs, then import every module."""

from __future__ import annotations

import enum
import importlib
import sys
import types
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def add_module(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__path__ = []  # type: ignore[attr-defined]
    sys.modules[name] = module
    if "." in name:
        parent_name, attr = name.rsplit(".", 1)
        parent = sys.modules.get(parent_name)
        if parent is not None:
            setattr(parent, attr, module)
    return module


homeassistant = add_module("homeassistant")
config_entries = add_module("homeassistant.config_entries")
core = add_module("homeassistant.core")
exceptions = add_module("homeassistant.exceptions")
setup_component = add_module("homeassistant.setup")
helpers = add_module("homeassistant.helpers")
entity_registry = add_module("homeassistant.helpers.entity_registry")
aiohttp_client = add_module("homeassistant.helpers.aiohttp_client")
update_coordinator = add_module("homeassistant.helpers.update_coordinator")
device_registry = add_module("homeassistant.helpers.device_registry")
entity_platform = add_module("homeassistant.helpers.entity_platform")
selector = add_module("homeassistant.helpers.selector")
components = add_module("homeassistant.components")
ffmpeg_component = add_module("homeassistant.components.ffmpeg")
binary_sensor_component = add_module("homeassistant.components.binary_sensor")
button_component = add_module("homeassistant.components.button")
camera_component = add_module("homeassistant.components.camera")

diagnostics_component = add_module("homeassistant.components.diagnostics")


class _Generic:
    @classmethod
    def __class_getitem__(cls, _item: object) -> type[object]:
        return cls


class ConfigEntry(_Generic):
    def __init__(
        self,
        *,
        entry_id: str = "synthetic-entry",
        data: dict[str, object] | None = None,
        options: dict[str, object] | None = None,
        title: str = "Synthetic entry",
        version: int = 1,
        minor_version: int = 1,
    ) -> None:
        self.entry_id = entry_id
        self.data = {} if data is None else data
        self.options = {} if options is None else options
        self.title = title
        self.version = version
        self.minor_version = minor_version
        self.runtime_data: object | None = None
        self.unload_callbacks: list[Any] = []
        self.update_listeners: list[Any] = []

    def async_on_unload(self, func: Any) -> None:
        """Store a cleanup callback as Home Assistant config entries do."""

        self.unload_callbacks.append(func)

    def add_update_listener(self, func: Any) -> Any:
        self.update_listeners.append(func)

        def remove() -> None:
            self.update_listeners.remove(func)

        return remove


class FlowResultType(enum.StrEnum):
    FORM = "form"
    CREATE_ENTRY = "create_entry"
    ABORT = "abort"


class _FlowBase:
    def __init__(self) -> None:
        self.hass = None
        self.context: dict[str, object] = {}
        self._unique_id: str | None = None
        self.update_reload_calls: list[dict[str, object]] = []

    def async_show_form(
        self,
        *,
        step_id: str,
        data_schema: object = None,
        errors: dict[str, str] | None = None,
        description_placeholders: dict[str, str] | None = None,
        **kwargs: object,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "type": FlowResultType.FORM,
            "step_id": step_id,
            "data_schema": data_schema,
            "errors": {} if errors is None else errors,
        }
        if description_placeholders is not None:
            result["description_placeholders"] = description_placeholders
        result.update(kwargs)
        return result

    def async_show_menu(
        self, *, step_id: str, menu_options: list[str], **kwargs: object
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "type": "menu",
            "step_id": step_id,
            "menu_options": menu_options,
        }
        result.update(kwargs)
        return result

    def async_create_entry(
        self,
        *,
        title: str,
        data: dict[str, object],
        **kwargs: object,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "type": FlowResultType.CREATE_ENTRY,
            "title": title,
            "data": data,
        }
        result.update(kwargs)
        return result

    def async_abort(
        self,
        *,
        reason: str,
        description_placeholders: dict[str, str] | None = None,
        **kwargs: object,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "type": FlowResultType.ABORT,
            "reason": reason,
        }
        if description_placeholders is not None:
            result["description_placeholders"] = description_placeholders
        result.update(kwargs)
        return result


class ConfigFlow(_FlowBase):
    def __init_subclass__(cls, **kwargs: object) -> None:
        kwargs.pop("domain", None)
        super().__init_subclass__()

    async def async_set_unique_id(
        self, unique_id: str | None, *, raise_on_progress: bool = True
    ) -> None:
        self._unique_id = unique_id
        self._raise_on_progress = raise_on_progress

    def _abort_if_unique_id_configured(self) -> None:
        return None

    def _get_reauth_entry(self) -> object:
        assert self.hass is not None
        entry_id = self.context["entry_id"]
        entry = self.hass.config_entries.async_get_entry(entry_id)
        assert entry is not None
        return entry

    def async_update_reload_and_abort(
        self,
        entry: Any,
        *,
        data_updates: dict[str, object],
        reason: str,
    ) -> dict[str, object]:
        self.update_reload_calls.append(
            {
                "entry": entry,
                "data_updates": data_updates,
                "reason": reason,
            }
        )
        entry.data = {**entry.data, **data_updates}
        return self.async_abort(reason=reason)


class OptionsFlow(_FlowBase):
    def __init__(self, config_entry: Any | None = None) -> None:
        super().__init__()
        self._config_entry = config_entry

    @property
    def config_entry(self) -> Any:
        if self._config_entry is not None:
            return self._config_entry
        assert self.hass is not None
        entry_id = self.context["entry_id"]
        entry = self.hass.config_entries.async_get_entry(entry_id)
        assert entry is not None
        return entry


class HomeAssistant:
    pass


class HomeAssistantError(Exception):
    pass


class ConfigEntryAuthFailed(Exception):
    pass


class ConfigEntryNotReady(Exception):
    pass


class DataUpdateCoordinator(_Generic):
    def __init__(self, *_args: object, **kwargs: object) -> None:
        self.data = None
        self.last_update_success = False
        self.config_entry = kwargs.get("config_entry")
        self.always_update = kwargs.get("always_update", True)
        self.listeners: list[Any] = []

    def async_add_listener(self, update_callback: Any) -> Any:
        """Register a coordinator listener and return its unsubscribe callback."""

        self.listeners.append(update_callback)

        def remove_listener() -> None:
            if update_callback in self.listeners:
                self.listeners.remove(update_callback)

        return remove_listener

    def async_set_updated_data(self, data: object) -> None:
        """Publish data and synchronously notify registered HA listeners."""

        self.data = data
        self.last_update_success = True
        for update_callback in tuple(self.listeners):
            update_callback()

    async def async_config_entry_first_refresh(self) -> None:
        try:
            self.data = await self._async_update_data()
        except BaseException:
            self.last_update_success = False
            raise
        self.last_update_success = True


class CoordinatorEntity(_Generic):
    def __init__(self, coordinator: object) -> None:
        self.coordinator = coordinator
        self.state_writes = 0

    @property
    def available(self) -> bool:
        return bool(self.coordinator.last_update_success)

    def async_write_ha_state(self) -> None:
        self.state_writes += 1

    async def async_added_to_hass(self) -> None:
        return None

    def async_on_remove(self, callback: Any) -> None:
        self._remove_callback = callback


class BinarySensorEntity:
    def __init__(self) -> None:
        self._remove_callback = None

    def async_on_remove(self, callback: Any) -> None:
        self._remove_callback = callback


class ButtonEntity:
    def __init__(self) -> None:
        self._remove_callback = None

    def async_on_remove(self, callback: Any) -> None:
        self._remove_callback = callback


class CameraEntityFeature(enum.IntFlag):
    ON_OFF = 1
    STREAM = 2


class Camera:
    def __init__(self) -> None:
        self.hass: Any = None
        self._attr_is_on = True
        self._attr_supported_features = CameraEntityFeature(0)

    @property
    def available(self) -> bool:
        return True

    async def stream_source(self) -> str | None:
        return None


class UpdateFailed(Exception):
    pass


class TextSelectorType(enum.Enum):
    PASSWORD = "password"


class TextSelectorConfig:
    def __init__(self, **kwargs: object) -> None:
        self.config = kwargs


class TextSelector:
    def __init__(self, config: object) -> None:
        self.config = config


config_entries.ConfigEntry = ConfigEntry
config_entries.ConfigFlow = ConfigFlow
config_entries.ConfigFlowResult = dict
config_entries.FlowResultType = FlowResultType
config_entries.OptionsFlow = OptionsFlow
core.HomeAssistant = HomeAssistant
core.callback = lambda func: func
exceptions.HomeAssistantError = HomeAssistantError
exceptions.ConfigEntryAuthFailed = ConfigEntryAuthFailed
exceptions.ConfigEntryNotReady = ConfigEntryNotReady
setup_component.async_setup_component = lambda *_args, **_kwargs: None
ffmpeg_component.get_ffmpeg_manager = lambda _hass: object()
aiohttp_client.async_create_clientsession = lambda _hass, **_kwargs: object()
aiohttp_client.async_get_clientsession = lambda _hass, **_kwargs: object()
update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
update_coordinator.CoordinatorEntity = CoordinatorEntity
update_coordinator.UpdateFailed = UpdateFailed
binary_sensor_component.BinarySensorEntity = BinarySensorEntity
binary_sensor_component.DOMAIN = "binary_sensor"
button_component.ButtonEntity = ButtonEntity
button_component.DOMAIN = "button"
camera_component.Camera = Camera
camera_component.CameraEntityFeature = CameraEntityFeature
device_registry.DeviceInfo = dict
device_registry.async_get = lambda _hass: types.SimpleNamespace(
    async_get_device=lambda **_kwargs: None
)

entity_platform.AddEntitiesCallback = object
entity_registry.async_get = lambda _hass: object()
entity_registry.async_entries_for_config_entry = lambda _registry, _entry_id: ()
selector.TextSelector = TextSelector
selector.TextSelectorConfig = TextSelectorConfig
selector.TextSelectorType = TextSelectorType
selector.SelectSelector = TextSelector
selector.SelectSelectorConfig = TextSelectorConfig
diagnostics_component.async_redact_data = lambda data, _redact: data

voluptuous = add_module("voluptuous")
voluptuous.Required = lambda key, **_kwargs: key
voluptuous.Optional = lambda key, **_kwargs: key
voluptuous.In = lambda values: values
voluptuous.Schema = lambda value: value

for module_name in (
    "custom_components.ufanet_intercom.const",
    "custom_components.ufanet_intercom.api",
    "custom_components.ufanet_intercom.history",
    "custom_components.ufanet_intercom.coordinator",
    "custom_components.ufanet_intercom",
    "custom_components.ufanet_intercom.config_flow",
    "custom_components.ufanet_intercom.button",
    "custom_components.ufanet_intercom.binary_sensor",
    "custom_components.ufanet_intercom.camera",
    "custom_components.ufanet_intercom.diagnostics",
):
    importlib.import_module(module_name)
