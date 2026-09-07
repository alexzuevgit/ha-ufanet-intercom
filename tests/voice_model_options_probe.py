"""Real HA/voluptuous/selector probe, synthetic configuration and catalog only.

Run separately from import-stub pytest, on each supported HA version, for example:
PYTHONPATH=. uv run --no-project --python 3.14.5 --with homeassistant==2026.8.1 \
  --with argon2-cffi==25.1.0 python tests/voice_model_options_probe.py
No audio, provider credentials, external requests or physical actions are used.
"""

from __future__ import annotations

import asyncio
import copy
import json
import tempfile
from importlib.metadata import version
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch

import voluptuous as vol
import voluptuous_serialize
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from custom_components.ufanet_intercom import _enabled_voice_config
from custom_components.ufanet_intercom import config_flow as cf
from custom_components.ufanet_intercom.const import DOMAIN, DiscoveredDoor
from custom_components.ufanet_intercom.voice_models import ModelCatalog
from custom_components.ufanet_intercom.voice_phrase import encode_phrase_set


def serialize(form):
    return voluptuous_serialize.convert(
        form["data_schema"], custom_serializer=cv.custom_serializer
    )


async def main():
    key, binding = "a" * 64, "b" * 64
    target = DiscoveredDoor(
        key=key,
        binding=binding,
        shared_id=12345,
        door=0,
        model=21,
        display_name="Synthetic",
        trusted=True,
        openable=True,
        cctv_number="SYNTHETIC-CAMERA",
    )
    phrases = ["Синтетическая проверка"]
    original = {
        "unrelated": "keep",
        "voice_phrase": {
            "version": 1,
            "enabled": True,
            "endpoint": "https://stt.invalid/v1/audio/transcriptions",
            "token": "SYNTHETIC-ONLY",
            "model": "",
            "allow_insecure_http": False,
            "targets": {
                key: {
                    "binding": binding,
                    "phrases": encode_phrase_set(phrases),
                    "entered_phrases": phrases,
                }
            },
        },
    }
    calls = []
    catalog = ModelCatalog(
        ("gigaam-v3-e2e-rnnt", "large-v3", "large-v3-turbo", "small")
    )

    async def discover(config):
        calls.append(config)
        return catalog

    with tempfile.TemporaryDirectory(prefix="ha-model-options-") as config_dir:
        hass = HomeAssistant(config_dir)
        hass.config_entries = ConfigEntries(hass, {})
        await hass.config_entries.async_initialize()

        def flow_for(options):
            entry = ConfigEntry(
                data={},
                discovery_keys=MappingProxyType({}),
                domain=DOMAIN,
                minor_version=2,
                options=copy.deepcopy(options),
                source="user",
                subentries_data=(),
                title="Synthetic",
                unique_id=None,
                version=2,
            )
            entry.runtime_data = SimpleNamespace(
                coordinator=SimpleNamespace(data={key: target}),
                rtsp_proxy=SimpleNamespace(
                    stream_url=lambda _: f"rtsp://127.0.0.1:12345/{key}"
                ),
            )
            hass.config_entries._entries[entry.entry_id] = entry
            flow = cf.UfanetIntercomOptionsFlow()
            flow.hass = hass
            flow.handler = entry.entry_id
            return flow

        with patch.object(cf, "async_discover_models", discover):
            # REAL populated form: bool toggled, endpoint/defaults and hidden blank key.
            flow = flow_for(original)
            form = await flow.async_step_voice_service()
            service_schema = serialize(form)
            token_field = next(
                field for field in service_schema if field["name"] == "token"
            )
            assert token_field["required"] is False and token_field["default"] == ""
            values = form["data_schema"]({})
            assert values["enabled"] is True and values["token"] == ""
            assert set(values) == {
                "enabled",
                "endpoint",
                "token",
                "allow_insecure_http",
            }
            values["enabled"] = False
            del values["token"]  # Real frontend may omit optional empty strings.
            paused = (await flow.async_step_voice_service(values))["data"]
            assert paused == {
                **original,
                "voice_phrase": {**original["voice_phrase"], "enabled": False},
            }
            assert not calls and _enabled_voice_config(paused) is None
            assert "SYNTHETIC-ONLY" not in json.dumps(service_schema)

            # Current legacy-blank OFF configuration must not be treated as a pause.
            flow = flow_for(paused)
            form = await flow.async_step_voice_service()
            values = form["data_schema"]({})
            del values["token"]
            form = await flow.async_step_voice_service(values)
            assert form["step_id"] == "voice_model" and len(calls) == 1
            model_schema = serialize(form)
            (field,) = model_schema
            assert field["name"] == "model" and field["required"] is True
            assert "default" not in field
            assert field["selector"]["select"]["custom_value"] is True
            assert field["selector"]["select"]["mode"] == "dropdown"
            assert field["selector"]["select"]["options"] == list(catalog.models)
            try:
                form["data_schema"]({})
            except vol.Invalid:
                pass
            else:
                raise AssertionError("blank legacy model gained a default")
            for invalid in ("", " ", " small", "small "):
                result = await flow.async_step_voice_model(
                    form["data_schema"]({"model": invalid})
                )
                assert result["errors"] == {"model": "invalid_model"}
            saved = (
                await flow.async_step_voice_model(
                    form["data_schema"]({"model": "Org/Exact_ID:v1.2"})
                )
            )["data"]
            assert saved == {
                **paused,
                "voice_phrase": {
                    **paused["voice_phrase"],
                    "model": "Org/Exact_ID:v1.2",
                },
            }
            assert flow.config_entry.options == paused
            assert len(calls) == 1

            # Existing custom ID survives catalog omission and native schema default.
            flow = flow_for(saved)
            form = await flow.async_step_voice_service()
            form = await flow.async_step_voice_service(form["data_schema"]({}))
            assert serialize(form)[0]["default"] == "Org/Exact_ID:v1.2"
            values = form["data_schema"]({})
            assert (await flow.async_step_voice_model(values))["data"] == saved

            # Error catalog still offers real native custom entry without saving early.
            catalog = ModelCatalog(error="models_unavailable")
            flow = flow_for(paused)
            form = await flow.async_step_voice_service()
            form = await flow.async_step_voice_service(form["data_schema"]({}))
            assert form["errors"] == {"base": "models_unavailable"}
            assert serialize(form)[0]["selector"]["select"]["options"] == []
            assert flow.config_entry.options == paused
            assert (
                await flow.async_step_voice_model(
                    form["data_schema"]({"model": "small"})
                )
            )["data"]["voice_phrase"]["model"] == "small"

            # Native populated editing while OFF is saved, never silently discarded.
            flow = flow_for(paused)
            form = await flow.async_step_voice_service()
            values = form["data_schema"]({})
            values["endpoint"] = "https://other.invalid/v1/audio/transcriptions"
            before = len(calls)
            form = await flow.async_step_voice_service(values)
            assert (
                form["errors"] == {"token": "token_origin_changed"}
                and len(calls) == before
            )
            values = form["data_schema"]({"token": "SYNTHETIC-NEW"})
            form = await flow.async_step_voice_service(values)
            saved = (
                await flow.async_step_voice_model(
                    form["data_schema"]({"model": "small"})
                )
            )["data"]
            assert (
                saved["voice_phrase"]["endpoint"]
                == "https://other.invalid/v1/audio/transcriptions"
            )
            assert saved["voice_phrase"]["token"] == "SYNTHETIC-NEW"
            assert saved["voice_phrase"]["enabled"] is False
            assert saved["voice_phrase"]["targets"] == paused["voice_phrase"]["targets"]

        print(
            json.dumps(
                {
                    "ha_version": version("homeassistant"),
                    "result": "PASS",
                    "service_schema": service_schema,
                    "model_schema": model_schema,
                    "catalog_transport": "synthetic only",
                    "network_calls": 0,
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
