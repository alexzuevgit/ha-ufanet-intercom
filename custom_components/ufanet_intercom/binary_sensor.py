"""Per-door observed-call pulse binary sensors."""

from __future__ import annotations

from collections import Counter
from typing import Any

from homeassistant.components.binary_sensor import (
    DOMAIN as BINARY_SENSOR_DOMAIN,
)
from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import UfanetConfigEntry
from .button import _display_name
from .const import DOMAIN, DiscoveredDoor
from .coordinator import UfanetCoordinator
from .history import CallHistoryManager


async def async_setup_entry(
    hass: Any,
    entry: UfanetConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add neutral observed-call and configured code-phrase sensors."""

    runtime = entry.runtime_data
    coordinator = runtime.coordinator
    manager = runtime.history_manager
    voice_manager = getattr(runtime, "voice_manager", None)
    added_call_keys: set[str] = set()
    added_voice_keys: set[str] = set()

    def add_new_trusted_doors() -> None:
        snapshot = coordinator.data or {}
        trusted_doors = [door for door in snapshot.values() if door.trusted]
        name_counts = Counter(door.display_name for door in trusted_doors)
        new_call_doors = [
            door for door in trusted_doors if door.key not in added_call_keys
        ]
        new_voice_doors = [
            door
            for door in trusted_doors
            if voice_manager is not None
            and door.key not in added_voice_keys
            and voice_manager.configured_for(door.key, door.binding)
        ]
        if not new_call_doors and not new_voice_doors:
            return
        added_call_keys.update(door.key for door in new_call_doors)
        added_voice_keys.update(door.key for door in new_voice_doors)
        call_entities = (
            UfanetCallDetectedBinarySensor(
                coordinator,
                manager,
                door,
                device_name=_display_name(door, name_counts[door.display_name]),
            )
            for door in new_call_doors
        )
        voice_entities = (
            UfanetCodePhraseBinarySensor(
                coordinator,
                voice_manager,
                door,
                device_name=_display_name(door, name_counts[door.display_name]),
            )
            for door in new_voice_doors
        )
        async_add_entities((*call_entities, *voice_entities))

    unsubscribe = coordinator.async_add_listener(add_new_trusted_doors)
    entry.async_on_unload(unsubscribe)
    add_new_trusted_doors()


class UfanetCallDetectedBinarySensor(
    CoordinatorEntity[UfanetCoordinator], BinarySensorEntity
):
    """A short pulse proving that a provider history row was observed."""

    _attr_has_entity_name = True
    _attr_translation_key = "call_detected"

    def __init__(
        self,
        coordinator: UfanetCoordinator,
        manager: CallHistoryManager,
        target: DiscoveredDoor,
        *,
        device_name: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._history_manager = manager
        self._target = target
        self._attr_unique_id = f"{target.key}_call_detected"
        self.entity_id = (
            f"{BINARY_SENSOR_DOMAIN}.{target.suggested_object_id}_call_detected"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, target.key)},
            name=device_name or target.display_name,
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to account-level history state changes."""

        await super().async_added_to_hass()
        self.async_on_remove(
            self._history_manager.async_add_listener(self.async_write_ha_state)
        )

    @property
    def available(self) -> bool:
        """Require a successful baseline and the exact current trusted target."""

        current = (self.coordinator.data or {}).get(self._target.key)
        return bool(
            super().available
            and current is not None
            and current.trusted
            and self._target.matches_command_identity(current)
            and self._history_manager.available_for(self._target.key)
        )

    @property
    def is_on(self) -> bool:
        """Return ON only during the bounded observed-call pulse."""

        return self.available and self._history_manager.is_on_for(self._target.key)


class UfanetCodePhraseBinarySensor(
    CoordinatorEntity[UfanetCoordinator], BinarySensorEntity
):
    """Read-only pulse for an exact code-phrase match near one intercom."""

    _attr_has_entity_name = True
    _attr_translation_key = "code_phrase"

    def __init__(
        self,
        coordinator: UfanetCoordinator,
        manager: Any,
        target: DiscoveredDoor,
        *,
        device_name: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._voice_manager = manager
        self._target = target
        self._attr_unique_id = f"{target.key}_code_phrase"
        self.entity_id = (
            f"{BINARY_SENSOR_DOMAIN}.{target.suggested_object_id}_code_phrase"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, target.key)},
            name=device_name or target.display_name,
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to in-memory manager state only."""

        await super().async_added_to_hass()
        self.async_on_remove(
            self._voice_manager.add_listener(self.async_write_ha_state)
        )

    @property
    def available(self) -> bool:
        """Require the exact current trusted binding and a healthy pipeline."""

        current = (self.coordinator.data or {}).get(self._target.key)
        return bool(
            super().available
            and current is not None
            and current.trusted
            and self._target.matches_command_identity(current)
            and self._voice_manager.configured_for(current.key, current.binding)
            and self._voice_manager.available_for(current.key)
        )

    @property
    def is_on(self) -> bool:
        """Return only the manager's bounded in-memory pulse."""

        return self.available and self._voice_manager.is_on_for(self._target.key)
