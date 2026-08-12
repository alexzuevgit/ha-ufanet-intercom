"""Secret-redacted diagnostics for Ufanet Intercom."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import UfanetConfigEntry
from .const import CONF_REQUIRES_ACK


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: UfanetConfigEntry
) -> dict[str, Any]:
    """Return aggregate counts and fixed booleans only."""

    doors = entry.runtime_data.coordinator.data or {}
    return {
        "last_update_success": bool(entry.runtime_data.coordinator.last_update_success),
        "requires_ack": entry.data.get(CONF_REQUIRES_ACK) is True,
        "discovered_count": len(doors),
        "trusted_count": sum(door.trusted for door in doors.values()),
        "openable_trusted_count": sum(
            door.trusted and door.openable for door in doors.values()
        ),
    }
