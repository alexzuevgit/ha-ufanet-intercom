# Sanitized mobile authentication and shared-inventory contract

## Evidence scope

This record captures the sanitized protocol contract verified against the Android application `ru.ufanet.smarthome` build `4.0.14`. An authorized, schema-only probe of the read-only shared inventory returned a list of four objects with four unique integer IDs. The probe recorded endpoint shapes, field names, field types, and non-sensitive state characteristics only.

**All production values are deliberately excluded.** This document and the companion fixture contain no installation IDs, names, addresses, camera values, contract values, credentials, passwords, access tokens, or refresh tokens. The four-item JSON fixture is entirely synthetic and must not be treated as an account snapshot or physical-target mapping.

## Authentication contract

Requests use JSON bodies. Authentication material is sensitive and must remain out of source control, fixtures, logs, diagnostics, exception text, and process arguments.

### Contract login

- Method and path: `POST /api/v1/auth/auth_by_contract/`
- Request body:

  ```json
  {"contract": "<CONTRACT-UPPERCASED>", "password": "<PASSWORD>"}
  ```

  The client applies `contract.upper()` before transmission.

- Exact successful token shape:

  ```json
  {"token": {"access": "<ACCESS>", "refresh": "<REFRESH>", "exp": 1234567890}}
  ```

  `exp` is an integer expiry value; `1234567890` is an illustrative JSON integer, not a required literal or validity example.

### Token refresh

- Method and path: `POST /api/v1/auth/refresh/`
- Request body:

  ```json
  {"token": "<REFRESH>"}
  ```

- Exact successful response shape (flat, not nested):

  ```json
  {"access": "<ACCESS>", "refresh": "<REFRESH>", "exp": 1234567890}
  ```

  The backend may return changed tokens or the same still-valid values; callers must validate and atomically accept either case rather than require rotation. As above, the numeric `exp` is illustrative, not a required literal.

The placeholders above describe structure only; they are not captured or usable secrets.

## Supported and unsupported API families

### Supported public-v2 family

The currently validated read-only inventory family is:

- `GET /api/v0/skud/shared/`

Public v2 supports only this shared SKUD/intercom family until other families receive separate protocol and physical-mapping validation.

The corresponding shared physical-action shape is documented, but not implemented or invoked by this evidence task:

- `GET /api/v0/skud/shared/{shared_id}/open/?door=0`

Despite using HTTP `GET`, this is a non-idempotent physical action. An implementation must issue exactly one deliberate request, must not retry after a timeout or transport error, and must report a timeout as an unknown outcome. Inventory discovery never authorizes arbitrary IDs or door selectors, and documentation or fixtures are not physical mappings.

### Explicitly unsupported families

The following endpoints are unsupported until independently validated:

- `/api/v0/devices/`
- `/api/v0/devices/cmd/`

No shared-inventory assumptions may be transferred to that separate device/command family.

## Shared-inventory schema

All four sanitized observations were dictionaries, all IDs were unique, and every object contained every field listed below. `integer` means a JSON integer and excludes booleans. The “observed type” column distinguishes values actually present in the schema-only sample from nullable/string semantics exercised only by the synthetic fixture.

| Field | Observed type | Sanitized notes |
|---|---|---|
| `ble_support` | boolean | Feature flag. |
| `camera` | null | Null on all observed items; camera values were not retained. The fixture keeps this null. |
| `cctv_number` | string | Non-empty on all observed items; only synthetic placeholders appear in the fixture. |
| `contract` | integer or null | Production contract values were not retained; the fixture uses null. |
| `custom_name` | null | Null in the observed sample. The synthetic fixture includes one generic string to exercise the nullable display-name case without asserting that a string was observed in this probe. |
| `disable_button` | boolean | `false` on all observed items; `true` makes a synthetic fixture item non-openable. |
| `dtmf_code` | string | Sensitive-looking provider values were not retained; fixture values are explicit placeholders. |
| `frsi` | boolean | Feature flag. |
| `house` | integer | Fixture values are synthetic and are not installation identifiers. |
| `id` | integer | Unique across all four observed items; production IDs are excluded. |
| `inactivity_reason` | null | Null on all observed items. The fixture keeps this null. |
| `is_blocked` | boolean | `false` on all observed items; `true` makes a synthetic fixture item non-openable. |
| `is_fav` | boolean | Feature flag. |
| `is_support_sip_monitor` | boolean | Feature flag. |
| `model` | integer | Fixture model codes are synthetic. |
| `no_sound` | boolean | Feature flag. |
| `open_in_talk` | string | Fixture values are synthetic placeholders. |
| `open_type` | string | `http` on all observed items. |
| `private_status` | integer | Fixture values are synthetic states. |
| `relays` | list | Empty on all observed items and kept empty in the fixture. |
| `role` | object | Exact nested fields observed: `id` integer and `name` string. All four observations had `role.id = 2`; this value is treated as a non-sensitive schema enum, not a provider object/installation identifier. Role names are excluded. |
| `scope` | string | Fixture values are synthetic. |
| `string_view` | string | Production display values are excluded; fixture values are generic. |
| `supports_key_recording` | boolean | Feature flag. |
| `timeout` | integer | Fixture values are synthetic. |

Other sanitized common observations were `open_type = "http"`, `disable_button = false`, `is_blocked = false`, non-empty `cctv_number`, and empty `relays` on all four items. These observations describe the bounded probe only and must not be generalized into permanent invariants without parser tests and new evidence.

## Privacy and safety invariants

- Never commit or log credentials, raw auth responses, tokens, contract values, provider object/installation IDs, titles, addresses, camera identifiers, or physical mappings. The documented `role.id = 2` is a non-sensitive schema enum.
- Parse integer fields strictly so JSON booleans are rejected where integers are required.
- Treat disabled or blocked inventory entries as visible but non-openable.
- Keep read-only inventory handling separate from the physical-action transport.
- Never route the physical-action `GET` through a generic idempotent-method retry policy.
- After any physical request begins, do not refresh, reconnect, redirect, or retry it.
- This evidence task performs no network request and implements no physical action.
