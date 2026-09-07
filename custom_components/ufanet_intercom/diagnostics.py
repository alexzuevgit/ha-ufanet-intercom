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

    runtime = entry.runtime_data
    doors = runtime.coordinator.data or {}
    voice_manager = getattr(runtime, "voice_manager", None)
    voice_config = getattr(voice_manager, "config", None)
    voice_enabled = bool(
        voice_manager is not None and getattr(voice_config, "enabled", False) is True
    )
    return {
        "last_update_success": bool(runtime.coordinator.last_update_success),
        "requires_ack": entry.data.get(CONF_REQUIRES_ACK) is True,
        "discovered_count": len(doors),
        "trusted_count": sum(door.trusted for door in doors.values()),
        "openable_trusted_count": sum(
            door.trusted and door.openable for door in doors.values()
        ),
        "call_history_available": bool(
            getattr(getattr(runtime, "history_manager", None), "available", False)
        ),
        "call_history_poll_interval_seconds": 3,
        "voice_phrase_enabled": voice_enabled,
        "voice_phrase_configured_count": (
            getattr(voice_config, "target_count", 0) if voice_enabled else 0
        ),
        "voice_phrase_available_count": (
            getattr(voice_manager, "available_count", 0) if voice_enabled else 0
        ),
    }
