# Universal account-discovered cameras implementation plan

> **Required contract:** entering any supported Ufanet account credentials discovers every supported shared intercom and creates every provider-supported function for each item: a guarded Open button and its Camera. No deployment-specific IDs, mappings, URLs, tokens, Frigate, or go2rtc configuration may be required.

## Evidence and boundaries

- `GET /api/v0/skud/shared/` supplies a non-empty `cctv_number` for each observed shared intercom.
- Authenticated `GET /api/v0/contract/` supplies the account/provider `isp_org.cams_server.url`.
- The discovered camera origin supports cookie login through `POST /api/internal/login/` and one-camera media leases through `POST /api/v0/cameras/this/`.
- A lease supplies an allowlisted media host and a 300-second live token. The token must remain in process memory.
- A 2026-08-18 read-only live probe decoded all four current-account cameras through the protocol: H.264 video plus PCMA audio; two 1920×1080 and two 1280×960.
- Opening remains a separate physical-action transport and is not exercised by camera tests.

## Security invariants

1. Never persist or expose credentials, provider IDs, `cctv_number`, media host, cookie, or lease token in entity state, attributes, diagnostics, logs, exceptions, local URLs, or child-process arguments.
2. Camera discovery is derived only from the currently authenticated shared inventory. The RTSP relay resolves an opaque account-scoped target key back to the current trusted binding; arbitrary paths never become provider requests.
3. The media origin must be HTTPS and provider-owned. The leased RTSP host must pass a strict provider suffix allowlist, canonical hostname checks, DNS resolution, and public-address validation before connection.
4. The local RTSP relay binds only to `127.0.0.1` on an ephemeral port. `stream_source()` returns only `rtsp://127.0.0.1:<port>/<opaque-key>` with TCP transport.
5. Portal login and lease fetches are read-only and may retry once only for an authentication expiry. Physical opening retains its existing no-retry/unknown-outcome semantics.
6. All response, RTSP message, header, body, frame, client-count, timeout, and cache sizes are bounded. Redirects are disabled.
7. Unload closes the local listener, active relays, media session, and cached leases; cancellation is propagated without leaking details.

## Task 1 — Prove dynamic account metadata parsing

**Files:**
- Modify `custom_components/ufanet_intercom/api.py`
- Modify `tests/test_api.py`

1. Add failing tests for bounded `/api/v0/contract/` parsing, exact account selection, unique active `cams_server.url`, malformed/ambiguous/missing values, redirects, and privacy-safe errors.
2. Add `media_origin` to the authenticated client runtime only; do not put it in config-entry data or entity attributes.
3. Fetch provider metadata as part of login/discovery using the read-only session and authenticated JWT.
4. Accept only canonical HTTPS provider-owned hosts; never accept IP literals, credentials, fragments, ports, or non-root URL paths.
5. Re-run API tests and privacy scans.

## Task 2 — Add an async media lease client

**Files:**
- Create `custom_components/ufanet_intercom/media.py`
- Create `tests/test_media.py`

1. Add failing tests for portal cookie login and one-camera lease requests using exact wire fixtures.
2. Require an exact current `DiscoveredDoor`; reject empty/malformed camera numbers before I/O.
3. Parse exactly one matching result with bounded JSON and canonical allowlisted host.
4. Cache leases by opaque target key with an early-expiry margin; serialize login/refresh and per-key fetches.
5. Retry one time only after an explicit auth failure; reject redirects and all other statuses without retries.
6. Ensure representations and exceptions contain fixed public text only.

## Task 3 — Add a token-redacting loopback RTSP relay

**Files:**
- Create `custom_components/ufanet_intercom/rtsp_proxy.py`
- Create `tests/test_rtsp_proxy.py`

1. Port only the bounded protocol logic from the independently tested `ufanet-face-unlock` relay, using asyncio streams.
2. Bind to loopback/ephemeral port and map only opaque current target keys.
3. Rewrite only the RTSP request URI to the signed upstream URI in memory; strip client authorization and reject unsupported methods/transports/paths.
4. Relay RTSP control plus interleaved RTP over TCP with strict size, connection, and idle limits.
5. Cover OPTIONS/DESCRIBE/SETUP/PLAY/TEARDOWN, malformed input, token redaction, upstream failure, cancellation, concurrent clients, and close.
6. Use mocked DNS/sockets in unit tests; no provider traffic.

## Task 4 — Integrate with Home Assistant lifecycle/entities

**Files:**
- Modify `custom_components/ufanet_intercom/__init__.py`
- Replace `custom_components/ufanet_intercom/camera.py`
- Modify `custom_components/ufanet_intercom/const.py`
- Modify `tests/test_ha_runtime.py`

1. Extend `UfanetRuntimeData` with media client/proxy references excluded from repr.
2. Start the relay before forwarding platforms; close it and the media session on every setup failure and unload path.
3. Create one camera entity for every trusted discovered door with a non-empty camera number, regardless of account or provider object ID.
4. Keep stable entity/device identity based on the existing opaque target key. Use stream-derived stills; no Frigate calls.
5. Remove `CAMERA_LINKS`, fixed entity IDs, Frigate discovery, and all alias-based logic.
6. Test two independent config entries with overlapping synthetic provider IDs/camera numbers, dynamic inventory removal/change, setup rollback, unload, and no-secret repr/state.

## Task 5 — Documentation, version, and release gates

**Files:**
- Modify `README.md`
- Modify `docs/protocol/mobile-auth-and-shared-inventory.md`
- Modify `custom_components/ufanet_intercom/manifest.json`
- Modify HACS metadata/release notes if present

1. Document the actual automatic account contract and provider dependency.
2. Bump the prerelease version; do not release or deploy yet.
3. Run formatting, lint, typing, focused tests, full tests, manifest/HACS validation, secret/private-literal scans, and diff review.
4. Perform an isolated Home Assistant runtime probe with synthetic servers.
5. Perform a read-only live lease/RTSP decode check for every currently discovered camera with tokens confined to memory.
6. Obtain independent security and code reviews; resolve all Critical/Important findings.
7. Production deployment is a separate explicit cutover gate with backup, config check, controlled restart, entity-count/stream verification, and immediate rollback on regression.

## Implementation record

- The media-origin lookup remains isolated in `media.py` and runs lazily in an
  RTSP handler thread. This deliberately avoids adding portal cookies or a
  second JWT to the asyncio physical/read clients in `api.py`.
- The relay uses the already exercised bounded threaded RTSP implementation
  rather than a new asyncio protocol implementation. Media-client creation,
  loopback bind, portal/DNS/RTSP I/O, and shutdown all run in bounded worker
  threads; the Home Assistant event loop only replaces immutable binding
  snapshots. Concurrent clients are capped at 16 per config entry.
- `2.0.0rc6` removed all fixed camera mappings and Frigate dependencies. The
  final synthetic suite has 288 tests, including two-account isolation,
  lifecycle teardown, token redaction, public-address pinning, and bounded
  clients.
- Real Entity Registry/Camera probes pass on Home Assistant 2026.7.4 and
  2026.8.1, including stream-derived stills and the opaque loopback source.
- The final read-only live gate decoded the first H.264 frame from all four
  current-account cameras through the rc6 relay in 1.58–2.59 seconds. No open
  command was invoked. Production remains unchanged pending an explicit
  cutover decision.
