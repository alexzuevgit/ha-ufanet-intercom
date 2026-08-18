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
    """Add one neutral call-observed pulse sensor for every trusted door."""

    runtime = entry.runtime_data
    coordinator = runtime.coordinator
    manager = runtime.history_manager
    added_keys: set[str] = set()

    def add_new_trusted_doors() -> None:
        snapshot = coordinator.data or {}
        trusted_doors = [door for door in snapshot.values() if door.trusted]
        name_counts = Counter(door.display_name for door in trusted_doors)
        new_doors = [door for door in trusted_doors if door.key not in added_keys]
        if not new_doors:
            return
        added_keys.update(door.key for door in new_doors)
        async_add_entities(
            UfanetCallDetectedBinarySensor(
                coordinator,
                manager,
                door,
                device_name=_display_name(door, name_counts[door.display_name]),
            )
            for door in new_doors
        )

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
