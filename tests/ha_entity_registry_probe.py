"""Real Home Assistant Entity Registry acceptance probe for opaque initial IDs.

Run this script in an isolated environment with one exact supported Home Assistant
version. It constructs no provider client and forbids physical actuation.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from datetime import timedelta
from importlib.metadata import version
from types import MappingProxyType

from homeassistant import loader
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform

from custom_components.ufanet_intercom.binary_sensor import (
    UfanetCallDetectedBinarySensor,
)
from custom_components.ufanet_intercom.button import UfanetDoorOpenButton
from custom_components.ufanet_intercom.camera import UfanetIntercomCamera
from custom_components.ufanet_intercom.const import DOMAIN, DiscoveredDoor
from custom_components.ufanet_intercom.history import CallHistoryManager


class _Coordinator:
    """Minimal no-I/O coordinator contract used by CoordinatorEntity."""

    def __init__(self, entry: ConfigEntry, target: DiscoveredDoor) -> None:
        self.config_entry = entry
        self.data = MappingProxyType({target.key: target})
        self.last_update_success = True
        self.last_exception = None

    def async_add_listener(self, _listener: object, context: object = None) -> object:
        return lambda: None

    async def async_request_refresh(self) -> None:
        return None


class _NoPhysicalClient:
    """Fail if any future code accidentally tries to actuate in this probe."""

    async def async_open(self, _key: str) -> None:
        raise AssertionError("physical method must not be called")


class _CameraProxy:
    """Return one token-free loopback source without provider I/O."""

    def stream_url(self, alias: str) -> str | None:
        if alias != "a" * 64:
            return None
        return f"rtsp://127.0.0.1:18092/{alias}"


async def _main() -> None:
    target = DiscoveredDoor(
        key="a" * 64,
        shared_id=40_001,
        door=0,
        model=21,
        display_name="Synthetic display",
        binding="b" * 64,
        openable=True,
        trusted=True,
        cctv_number="SYNTHETIC-CAMERA",
    )

    with tempfile.TemporaryDirectory(prefix="ha-ufanet-registry-probe-") as config_dir:
        hass = HomeAssistant(config_dir)
        loader.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        if hasattr(hass.config_entries, "async_initialize"):
            await hass.config_entries.async_initialize()
        dr.async_setup(hass)
        await dr.async_load(hass)
        await er.async_load(hass)

        entry = ConfigEntry(
            data={},
            discovery_keys=MappingProxyType({}),
            domain=DOMAIN,
            minor_version=1,
            options={},
            source="user",
            subentries_data=(),
            title="Synthetic Ufanet",
            unique_id="c" * 64,
            version=1,
        )
        # Register only in memory so Device Registry accepts the relationship.
        # ConfigEntries.async_add() is intentionally not called: that would run setup.
        hass.config_entries._entries[entry.entry_id] = entry

        platform = EntityPlatform(
            hass=hass,
            logger=logging.getLogger("ufanet-registry-probe"),
            domain="button",
            platform_name=DOMAIN,
            platform=None,
            scan_interval=timedelta(seconds=30),
            entity_namespace=None,
        )
        platform.config_entry = entry
        entity = UfanetDoorOpenButton(
            _Coordinator(entry, target), _NoPhysicalClient(), target
        )
        registry = er.async_get(hass)
        await platform._async_add_entity(entity, False, registry, None)

        expected = f"button.{target.suggested_object_id}"
        registry_entry = registry.async_get(expected)
        assert entity.entity_id == expected
        assert registry_entry is not None
        assert registry_entry.entity_id == expected
        assert registry_entry.unique_id == target.key
        assert registry_entry.suggested_object_id == target.suggested_object_id
        for private_fragment in ("private", "provider", "address", "123"):
            assert private_fragment not in registry_entry.entity_id

        history_manager = CallHistoryManager(
            [
                type(
                    "Route",
                    (),
                    {
                        "key": target.key,
                        "trusted": True,
                        "cctv_number": "synthetic-camera",
                        "house": 7001,
                    },
                )()
            ]
        )
        call_entity = UfanetCallDetectedBinarySensor(
            _Coordinator(entry, target), history_manager, target
        )
        call_platform = EntityPlatform(
            hass=hass,
            logger=logging.getLogger("ufanet-registry-probe-call"),
            domain="binary_sensor",
            platform_name=DOMAIN,
            platform=None,
            scan_interval=timedelta(seconds=30),
            entity_namespace=None,
        )
        call_platform.config_entry = entry
        await call_platform._async_add_entity(call_entity, False, registry, None)
        call_registry_entry = registry.async_get(call_entity.entity_id)
        assert (
            call_entity.entity_id
            == f"binary_sensor.{target.suggested_object_id}_call_detected"
        )
        assert call_registry_entry is not None
        assert call_registry_entry.unique_id == f"{target.key}_call_detected"
        assert call_registry_entry.device_id == registry_entry.device_id

        camera = UfanetIntercomCamera(
            _Coordinator(entry, target),
            _CameraProxy(),
            target,
        )
        camera_platform = EntityPlatform(
            hass=hass,
            logger=logging.getLogger("ufanet-registry-probe-camera"),
            domain="camera",
            platform_name=DOMAIN,
            platform=None,
            scan_interval=timedelta(seconds=30),
            entity_namespace=None,
        )
        camera_platform.config_entry = entry
        await camera_platform._async_add_entity(camera, False, registry, None)
        camera_registry_entry = registry.async_get(camera.entity_id)
        assert camera.entity_id == f"camera.{target.suggested_object_id}"
        assert camera_registry_entry is not None
        assert camera_registry_entry.unique_id == f"{target.key}_camera"
        assert camera_registry_entry.device_id == registry_entry.device_id
        assert camera.use_stream_for_stills is True
        assert await camera.stream_source() == (f"rtsp://127.0.0.1:18092/{target.key}")

        legacy_target = DiscoveredDoor(
            key="d" * 64,
            shared_id=1002,
            door=0,
            model=21,
            display_name="Synthetic changed display",
            binding="e" * 64,
            openable=True,
            trusted=True,
        )
        legacy_id = "button.existing_user_automation_id"
        existing = registry.async_get_or_create(
            "button",
            DOMAIN,
            legacy_target.key,
            config_entry=entry,
            suggested_object_id="existing_user_automation_id",
        )
        assert existing.entity_id == legacy_id

        legacy_entity = UfanetDoorOpenButton(
            _Coordinator(entry, legacy_target), _NoPhysicalClient(), legacy_target
        )
        assert legacy_entity.entity_id == (
            f"button.{legacy_target.suggested_object_id}"
        )
        await platform._async_add_entity(legacy_entity, False, registry, None)
        assert legacy_entity.entity_id == legacy_id
        assert (
            registry.async_get_entity_id("button", DOMAIN, legacy_target.key)
            == legacy_id
        )

        print(
            "HA "
            f"{version('homeassistant')} real Entity Registry PASS: "
            f"{registry_entry.entity_id}"
        )


asyncio.run(_main())
