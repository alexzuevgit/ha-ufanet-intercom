# Controlled rollout and rollback

This guide applies to the `2.0.0rc6` release candidate. The integration can perform a real physical opening through Home Assistant. Treat every `button.press` call as a physical-access action.

## Before installation

1. Confirm Home Assistant is `2026.7.4` or newer and uses its supported Python/runtime image.
2. Create and verify a complete Home Assistant backup. Download an encrypted copy outside the Home Assistant host.
3. Record the currently installed integration version and preserve the existing `custom_components/ufanet_intercom` directory as a stopped-state copy. Do not copy credentials or config-entry data into tickets, chat, or Git.
4. Disable every automation, script, voice-assistant exposure, remote dashboard shortcut, and third-party service that can call `button.press` for this integration.
5. Plan the first physical test with an authorized operator at the exact entrance. A timeout or network error is inconclusive and must never be followed by an automatic retry.

## Controlled installation

1. After publication, add the HACS Custom repository, enable **Show beta versions**, select **Need a different version?**, and install the exact **v2.0.0rc6** tag. Do not install this RC from the mutable default branch. Alternatively, copy only the packaged `ufanet_intercom` directory into `custom_components` while Home Assistant is stopped.
2. Start Home Assistant and inspect the log before configuring the integration. Stop if import, migration, authentication, or config-entry errors appear.
3. Verify the installed version is exactly **2.0.0rc6** before configuration or any physical test. Then add or migrate the integration through the UI. Setup and reauthentication perform authentication and read-only inventory discovery; they must not open an entrance.
4. Review the aggregate number of discovered entrances and the physical-access/privacy warning before acknowledging the initial bindings.
5. Verify that every expected entrance has exactly one Device and one Button entity, and that every trusted entrance with an available provider camera also exposes one Camera entity. Unexpected, blocked, changed, or camera-less targets must not gain unsupported entities, and no unrelated device family may be exposed.
6. Open every Camera entity from its Ufanet device and confirm that the live view uses the embedded loopback RTSP relay over TCP and responds in under 10 seconds. This check is read-only and must not call `button.press`; no Frigate/go2rtc configuration or user-supplied camera URL/ID/token is part of the acceptance path.
7. Keep all automations disabled. Perform at most one explicitly authorized manual `button.press` test for one exact entrance while an operator observes the physical result.
8. If the result is timeout, disconnect, cancellation, redirect, authentication error after transmission, or otherwise unknown, record it as unknown and do not retry for diagnosis.
9. Observe logs and availability through at least one inventory refresh interval before testing another entrance. Enable automations only after a separate policy review; face, voice, MQTT, automatic access, and auto-concierge policy are outside this transport integration.

## Acceptance checks

- Home Assistant starts without integration errors.
- Setup, reload, reauthentication, options/adoption, backup and restart produce zero physical actions.
- All expected entrances are present once; no arbitrary target can be supplied in service data.
- Removed, blocked, disabled, ambiguous or changed bindings become unavailable after refresh.
- A successful manual command is treated as provider acknowledgement until the operator confirms the physical result.
- No timeout or unknown outcome causes a second request.

## Rollback

Rollback immediately if an unexpected entrance appears, entity identity changes, setup/reload causes an action, Home Assistant becomes unstable, or any command is duplicated.

1. Disable all related automations and prevent further `button.press` calls.
2. Stop Home Assistant before replacing component files.
3. Preferred rollback: restore the verified complete Home Assistant backup made before installation. This restores both component/config-entry state and registries consistently.
4. If no config-entry migration or registry change occurred, the preserved previous component directory may be restored while Home Assistant is stopped. Do not use this shortcut after a v1-to-v2 migration; restore the complete backup instead.
5. Start Home Assistant, verify the previous integration version/state, and confirm no related automation is enabled unexpectedly.
6. Preserve only sanitized error codes and timestamps for diagnosis. Never publish credentials, contracts, provider IDs, addresses, camera identifiers, tokens, request URLs, headers, bodies, raw diagnostics, or unredacted backups.

Do not delete the pre-installation backup until the release candidate has completed the chosen observation period and rollback has been rehearsed or otherwise verified.
