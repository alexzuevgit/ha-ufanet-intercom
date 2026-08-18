"""Automatic read-only cameras for account-discovered Ufanet intercoms."""

from __future__ import annotations

from collections import Counter
from typing import Any

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import UfanetConfigEntry
from .button import _display_name
from .const import DOMAIN, DiscoveredDoor
from .coordinator import UfanetCoordinator
from .rtsp_proxy import RtspProxyRuntime

_CAMERA_DOMAIN = "camera"


async def async_setup_entry(
    hass: Any,
    entry: UfanetConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one camera for every currently trusted provider camera binding."""

    del hass
    runtime = entry.runtime_data
    coordinator = runtime.coordinator
    proxy = runtime.rtsp_proxy
    added_keys: set[str] = set()

    def add_new_cameras() -> None:
        snapshot = coordinator.data or {}
        trusted_doors = [door for door in snapshot.values() if door.trusted]
        name_counts = Counter(door.display_name for door in trusted_doors)
        new_doors = [
            door
            for door in trusted_doors
            if door.key not in added_keys and proxy.stream_url(door.key) is not None
        ]
        if not new_doors:
            return
        added_keys.update(door.key for door in new_doors)
        async_add_entities(
            UfanetIntercomCamera(
                coordinator,
                proxy,
                door,
                device_name=_display_name(door, name_counts[door.display_name]),
            )
            for door in new_doors
        )

    unsubscribe = coordinator.async_add_listener(add_new_cameras)
    entry.async_on_unload(unsubscribe)
    add_new_cameras()


class UfanetIntercomCamera(CoordinatorEntity[UfanetCoordinator], Camera):
    """A provider camera attached to the exact discovered intercom device."""

    _attr_has_entity_name = True
    _attr_translation_key = "camera"
    _attr_supported_features = CameraEntityFeature.STREAM
    _attr_is_on = True

    def __init__(
        self,
        coordinator: UfanetCoordinator,
        proxy: RtspProxyRuntime,
        target: DiscoveredDoor,
        *,
        device_name: str | None = None,
    ) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        Camera.__init__(self)
        self.stream_options = {"rtsp_transport": "tcp"}
        self._proxy = proxy
        self._target = target
        self._attr_unique_id = f"{target.key}_camera"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, target.key)},
            name=device_name or target.display_name,
        )
        self.entity_id = f"{_CAMERA_DOMAIN}.{target.suggested_object_id}"

    @property
    def use_stream_for_stills(self) -> bool:
        """Generate preview images from the same token-hidden RTSP stream."""

        return True

    @property
    def available(self) -> bool:
        """Require the same current trusted binding and a local relay route."""

        current = (self.coordinator.data or {}).get(self._target.key)
        return bool(
            super().available
            and current is not None
            and current.trusted
            and self._target.matches_command_identity(current)
            and self._proxy.stream_url(self._target.key) is not None
        )

    async def stream_source(self) -> str | None:
        """Return only a loopback URL containing the opaque account target key."""

        if not self.available:
            return None
        return self._proxy.stream_url(self._target.key)
