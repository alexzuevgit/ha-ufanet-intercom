"""Dynamic Ufanet door buttons for a momentary opening command."""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button import ButtonEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import UfanetConfigEntry
from .api import (
    UfanetConcurrentOpenError,
    UfanetError,
    UfanetOpenUnknownOutcome,
)
from .const import CONF_REQUIRES_ACK, DOMAIN, DiscoveredDoor
from .coordinator import UfanetCoordinator

_FALLBACK_NAME = "Ufanet intercom"
_ACKNOWLEDGEMENT_REQUIRED = "Ufanet intercom acknowledgement is required"


async def async_setup_entry(
    hass: Any,
    entry: UfanetConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add buttons for trusted doors in the current and later snapshots."""
    runtime = entry.runtime_data
    coordinator = runtime.coordinator
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
            UfanetDoorOpenButton(
                coordinator,
                runtime.client,
                door,
                device_name=_display_name(door, name_counts[door.display_name]),
            )
            for door in new_doors
        )

    unsubscribe = coordinator.async_add_listener(add_new_trusted_doors)
    entry.async_on_unload(unsubscribe)
    add_new_trusted_doors()


def _display_name(target: DiscoveredDoor, matching_names: int) -> str:
    """Return a deterministic device name without exposing provider identifiers."""
    if target.display_name != _FALLBACK_NAME and matching_names == 1:
        return target.display_name
    return f"{target.display_name} ({target.key[:8]})"


class UfanetDoorOpenButton(CoordinatorEntity[UfanetCoordinator], ButtonEntity):
    """A momentary door release with no fabricated persistent door state."""

    _attr_has_entity_name = True
    _attr_translation_key = "open_door"

    def __init__(
        self,
        coordinator: UfanetCoordinator,
        client: Any,
        target: DiscoveredDoor,
        *,
        device_name: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._client = client
        self._target = target
        self._attr_unique_id = target.key
        self._attr_name = device_name or target.display_name
        self.entity_id = f"{BUTTON_DOMAIN}.{target.suggested_object_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, target.key)},
            name=self._attr_name,
        )
        self._last_command_outcome = "none"
        self._do_not_retry = False

    @property
    def available(self) -> bool:
        """Require read-only validation for this exact target before pressing."""
        current = (self.coordinator.data or {}).get(self._target.key)
        return bool(
            self._is_acknowledged()
            and super().available
            and current is not None
            and current.trusted
            and current.openable
            and self._target.matches_command_identity(current)
        )

    @property
    def extra_state_attributes(self) -> dict[str, str | bool]:
        """Expose only sanitized command diagnostics."""
        return {
            "last_command_outcome": self._last_command_outcome,
            "do_not_retry": self._do_not_retry,
        }

    def _is_acknowledged(self) -> bool:
        """Check the config entry's current acknowledgement value exactly."""
        entry = self.coordinator.config_entry
        return entry is not None and entry.data.get(CONF_REQUIRES_ACK) is False

    async def async_press(self) -> None:
        """Send one fixed momentary Ufanet opening command."""
        if not self._is_acknowledged():
            raise HomeAssistantError(_ACKNOWLEDGEMENT_REQUIRED)

        error_message: str | None = None
        try:
            await self._client.async_open(self._target.key)
        except asyncio.CancelledError:
            if self._client.last_physical_outcome == "unknown":
                self._last_command_outcome = "unknown"
                self._do_not_retry = True
                self.async_write_ha_state()
            raise
        except UfanetOpenUnknownOutcome:
            self._last_command_outcome = "unknown"
            self._do_not_retry = True
            self.async_write_ha_state()
            error_message = (
                "Door opening outcome is unknown; the command was not repeated"
            )
        except UfanetConcurrentOpenError:
            error_message = "Another door command is already in progress"
        except UfanetError:
            if self._client.last_physical_outcome == "not_confirmed":
                self._last_command_outcome = "not_confirmed"
                self._do_not_retry = False
                self.async_write_ha_state()
            error_message = "Door opening was not confirmed"
        else:
            self._last_command_outcome = "confirmed"
            self._do_not_retry = False
            self.async_write_ha_state()
            return

        raise HomeAssistantError(error_message)
