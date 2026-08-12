"""Read-only availability coordinator for Ufanet intercom targets."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import timedelta
from types import MappingProxyType

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    UfanetAuthenticationError,
    UfanetClient,
    UfanetConnectionError,
    UfanetDiscoveryError,
    UfanetError,
)
from .const import DOMAIN, UPDATE_INTERVAL_MINUTES, DiscoveredDoor

_LOGGER = logging.getLogger(__name__)


class UfanetCoordinator(DataUpdateCoordinator[Mapping[str, DiscoveredDoor]]):
    """Keep an immutable snapshot of dynamically discovered doors."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: UfanetClient,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
            # DiscoveredDoor equality intentionally covers immutable command identity,
            # not volatile availability/trust flags. Always notify listeners after a
            # successful inventory refresh so those flags take effect immediately.
            always_update=True,
        )
        self.client = client

    async def _async_update_data(self) -> Mapping[str, DiscoveredDoor]:
        try:
            discovered = await self.client.async_update_inventory()
            return MappingProxyType(dict(discovered))
        except UfanetAuthenticationError:
            raise ConfigEntryAuthFailed("Ufanet authentication failed") from None
        except (UfanetConnectionError, UfanetDiscoveryError, UfanetError):
            raise UpdateFailed("Ufanet door availability update failed") from None
