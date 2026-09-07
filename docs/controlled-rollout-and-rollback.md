# Controlled rollout and rollback

This guide applies to the `2.1.0b7` beta release. The integration can perform a real physical opening through Home Assistant. Treat every `button.press` call as a physical-access action.

## Before installation

Local source gates for this panel: `pytest -q -W error`, `ruff check . --isolated --select E4,E7,E9,F`, `ruff format --check . --isolated`, `node --check custom_components/ufanet_intercom/frontend/ufanet-settings.js`, and `node --test tests/settings_panel.test.mjs`. The Node tests use logic-level DOM doubles; they do **not** establish real browser required-field behavior. Run the exact-Core browser acceptance below separately before deployment.

1. Confirm Home Assistant is `2026.7.4` or newer and uses its supported Python/runtime image.
2. Create and verify a complete Home Assistant backup. Download an encrypted copy outside the Home Assistant host.
3. Record the currently installed integration version and preserve the existing `custom_components/ufanet_intercom` directory as a stopped-state copy. Do not copy credentials or config-entry data into tickets, chat, or Git.
4. Disable every automation, script, voice-assistant exposure, remote dashboard shortcut, and third-party service that can call `button.press` for this integration.
5. Plan the first physical test with an authorized operator at the exact entrance. A timeout or network error is inconclusive and must never be followed by an automatic retry.

## Controlled installation

1. After publication, add the HACS Custom repository, enable **Show beta versions**, select **Need a different version?**, and install the exact **v2.1.0b7** tag. Do not install this beta from the mutable default branch. Alternatively, copy only the packaged `ufanet_intercom` directory into `custom_components` while Home Assistant is stopped.
2. Start Home Assistant and inspect the log before configuring the integration. Stop if import, migration, authentication, or config-entry errors appear.
3. Verify the installed version is exactly **2.1.0b7** before configuration or any physical test. Then add or migrate the integration through the UI. Setup and reauthentication perform authentication and read-only inventory discovery; they must not open an entrance.
4. Review the aggregate number of discovered entrances and the physical-access/privacy warning before acknowledging the initial bindings.
5. Verify that every expected entrance has exactly one Device and one Button entity, and that every trusted entrance with an available provider camera also exposes one Camera entity. Unexpected, blocked, changed, or camera-less targets must not gain unsupported entities, and no unrelated device family may be exposed.
6. Open every Camera entity from its Ufanet device and confirm that the live view uses the embedded loopback RTSP relay over TCP and responds in under 10 seconds. This check is read-only and must not call `button.press`; no Frigate/go2rtc configuration or user-supplied camera URL/ID/token is part of the acceptance path.
7. Optional code-phrase test: in **Options → Speech recognition settings**, enter an external OpenAI-compatible STT endpoint/key. **Next** explicitly requests only model metadata; then select a **required** model from the dropdown or enter its exact ID manually. No first/default model is assigned. Catalog failures or unsupported custom paths allow manual entry without changing existing settings. A saved key is reused only on the same origin; changing servers requires entering a key again. Use **Code phrases by intercom** for one exact current trusted intercom. Entered phrases are readable administrator configuration, separate from transient audio/transcripts. Protect both phrases and the STT token in configuration and Home Assistant backups. Confirm that no extra audio FFmpeg exists before enabling, that exactly one **Code phrase detected** sensor appears in the same Device after reload, and that no Frigate or MQTT configuration is involved.
8. With every opening automation still disabled, speak one explicitly authorized test phrase. Confirm only that the observation sensor turns on for five seconds; inspect no transcript, phrase, audio, token, provider identifier, or endpoint in entity state, attributes, Recorder, diagnostics, or ordinary logs. Do not call `button.press` from this sensor.
9. Disable code-phrase recognition again and verify its audio/STT workers stop before continuing. STT/VAD/FFmpeg/camera failure must produce no physical action.
10. Keep all automations disabled. Perform at most one explicitly authorized manual `button.press` test for one exact entrance while an operator observes the physical result.
11. If the result is timeout, disconnect, cancellation, redirect, authentication error after transmission, or otherwise unknown, record it as unknown and do not retry for diagnosis.
12. Observe logs and availability through at least one inventory refresh interval before testing another entrance. Enable opening automations only after a separate policy review; face recognition, MQTT, automatic access, and auto-concierge policy are outside this integration.

## Acceptance checks

- Home Assistant starts without integration errors.
- Setup, reload, reauthentication, options/adoption, backup and restart produce zero physical actions.
- All expected entrances are present once; no arbitrary target can be supplied in service data.
- Removed, blocked, disabled, ambiguous or changed bindings become unavailable after refresh.
- Code-phrase recognition is disabled by default; when enabled it is observation-only, exact-match, transcript-free in Home Assistant, and limited to configured current camera targets.
- Disabling the optional feature reaps every additional audio/STT worker, keeps settings and all phrase lists, and never opens an entrance. Settings and phrases can also be configured or edited while disabled.
- Exercise the **real HA frontend**: the entry gear must open `/ufanet-settings?config_entry=<entry_id>`. All service fields share one page; the saved synthetic key is masked, eye reveal/hide round-trips, and an unchanged/revealed key cannot follow an origin change without explicit confirmation. Only **Проверить связь и получить модели** requests bounded metadata (10 seconds, 256 KiB, 256 entries; no redirects/retries). It works before selecting a model and does not save, enable recognition, send audio or prove STT permission/credit. Failed discovery leaves manual input usable. **Сохранить** requires an explicit model for service edits but does not fetch metadata. Save/read back paused edits; a populated pure enabled→disabled pause must preserve legacy blank model and all phrases without discovery. Opening pages, setup/reload and disabled runtime never fetch catalogs.
- Confirm the phrase, inventory-refresh and reset actions remain accessible through the panel and retain existing Options validations and HA device names. Check stale-revision errors, double-click protection, draft preservation through ordinary `hass` updates, entry-switch/unmount key clearing, and stale check results. Test 401/403 and wrong-domain/missing-entry rejection on private REST routes. Never use real credentials or audio in browser fixture captures.
- The global UI is registered by integration `async_setup`, after frontend is available (`after_dependencies: frontend`), independently of provider login. `async_register_settings_panel(hass)` is also available for isolated exact-Core fixtures. Registration is idempotent across entries and reloads. Headless HA does not bootstrap frontend/HTTP for this feature. The asset is bundled under `frontend/ufanet-settings.js` and must be present in the release artifact.
- The administrator phrase dropdown immediately loads the current HA-named intercom's editor underneath without an intermediate submit. Retain the same selector after **Сохранить** and read back the exact target through a fresh owned native flow. Emptying a visible list and saving clears only that target, without a clear-all checkbox; saving an empty fresh target is harmless. Untouched blank legacy hash-only lists must survive opening/saving, with an explanation of one-time re-entry and deliberate touched deletion. The native fallback may retain blank-to-keep/explicit-checkbox semantics. **Reset recognition settings** requires confirmation and erases only voice configuration.
- Verify exactly one **Назад**: phrases, bindings/adoption and reset (including pending requests) return to service, then service returns to the integration. Dirty device switches/Back require discard confirmation; Cancel must retain the old selection, text and owned flow without a request. Ordinary HA state updates must retain drafts.
- Delay start/menu/target/Save/readback responses and switch targets or go Back: stale results must not display/save A's list under B, or abort unrelated flows. Fail later form POSTs as well as initial/menu requests and verify owned-flow cleanup plus a usable later Save. Distinguish an acknowledged Save from a transient post-save readback failure: preserve submitted text readonly and success status, clean the failed read candidate, and offer a non-saving refresh action before allowing another edit. No retry may replay private text onto replacement hardware.
- Entered phrases remain absent from entity state/attributes, Recorder, diagnostics and ordinary logs. Protect the readable Options configuration and backups; audio and STT transcripts are not saved locally.
- A successful manual command is treated as provider acknowledgement until the operator confirms the physical result.
- No timeout or unknown outcome causes a second request.

## Rollback

Rollback immediately if an unexpected entrance appears, entity identity changes, setup/reload causes an action, Home Assistant becomes unstable, or any command is duplicated.

1. Disable all related automations and prevent further `button.press` calls.
2. Stop Home Assistant before replacing component files.
3. Preferred rollback: restore the verified complete Home Assistant backup made before installation. This restores both component/config-entry state and registries consistently.
4. If no config-entry migration or registry change occurred, the preserved previous component directory may be restored while Home Assistant is stopped. Do not use this shortcut after a v1-to-v2 migration or after saving editable phrase lists in 2.1.0b3 or newer: older strict readers do not accept the added `entered_phrases` target field. Restore the complete backup made before the upgrade instead.
5. Start Home Assistant, verify the previous integration version/state, and confirm no related automation is enabled unexpectedly.
6. Preserve only sanitized error codes and timestamps for diagnosis. Never publish credentials, contracts, provider IDs, addresses, camera identifiers, tokens, request URLs, headers, bodies, raw diagnostics, or unredacted backups.

Do not delete the pre-installation backup until the beta has completed the chosen observation period and rollback has been rehearsed or otherwise verified.
