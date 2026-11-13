# Local tunnel lighting API

This reference explains backend-owned ESP32 lighting, its API and readiness
boundary. It depends on the existing FastAPI capture service, pyserial and the
unchanged `cabek-dmx-v1` firmware in the sibling `cabek-esp32` repository.

## Ownership and application integration

The inspected application uses FastAPI and React. No Laravel service or existing
process supervisor was found. One API process owns one persistent serial worker;
there is no second server or browser-owned lighting state. The existing React
`startCapture` request reaches `CaptureService.deliver_photos`: immediately before
photo delivery it enters SCANNING, and only validated, durably published completion
enters COMPLETED. AI inference/pricing completion is a separate lifecycle and does
not hold the photo-capture lights red. Existing, already completed capture replays
and browser refresh/navigation never replay BOOTING or extend the green deadline.

`python -m api` starts BOOTING before importing the application. Lifespan enters
INITIALIZING while scan/capture recovery starts. A recurring readiness probe checks:

- the scan supervisor is available and its recovery thread is alive;
- the model backend accepts its installed dependencies and model files;
- dossier storage accepts a temporary write;
- the configured mock camera provider has valid, readable photograph plans (both
  before/after directories when configured).

Only a successful probe enters READY. The check repeats every health interval;
failures leave/re-enter INITIALIZING, except during an active capture or explicit
OFF. The current repository implements only mock camera delivery: this change
does not claim physical camera readiness. AutoEstimate pricing is not a capture
readiness prerequisite; its existing errors/provisional prices remain separate.
The probe validates installed models through the backend's existing availability
check; it does not run inference as a readiness test. Hardware availability is
reported independently and cannot prevent normal application operation.

## States and transitions

| State | Desired output | Exit |
| --- | --- | --- |
| BOOTING | BLUE/OFF, alternating every 500 ms | Actual application lifespan starts initialization |
| INITIALIZING | BLUE | Required capture services pass checks |
| READY | WHITE, RGB 255/255/255 | Capture, explicit OFF or lost readiness |
| SCANNING | RED | This capture succeeds, fails or is interrupted |
| COMPLETED | GREEN | 10 monotonic seconds after photo success, then READY if healthy; otherwise INITIALIZING |
| OFF | RGB 0/0/0 | Explicit allowed state request, or a real capture |

Allowed non-idempotent transitions: OFF → BOOTING/INITIALIZING/READY;
BOOTING → INITIALIZING/OFF; INITIALIZING → READY/OFF;
READY → SCANNING/INITIALIZING/OFF;
SCANNING → COMPLETED/READY/INITIALIZING/OFF (internal shutdown only for OFF);
COMPLETED → SCANNING/READY/INITIALIZING/OFF.
A real capture may take priority from any startup/maintenance state. While capture
owns the tunnel, HTTP state changes are rejected except idempotent SCANNING;
maintenance tests are rejected. SCANNING and COMPLETED can only be entered by the
capture lifecycle; HTTP accepts only idempotent repeats of these states.

A failed/canceled/interrupted capture returns to READY if prerequisites remain
healthy, otherwise INITIALIZING, and never displays success green. A new capture
cancels a previous completion deadline and any maintenance test. Duplicate finish
events and stale capture identities have no effect. On application restart the
existing capture recovery marks interrupted captures failed; lighting starts a
new readiness cycle and never replays a saved successful capture.

## Serial reliability and failure behavior

The worker opens the port exclusively once and keeps it open. It holds DTR/RTS low,
uses configurable 115200-baud console I/O, serializes all writes and blinks using
existing `2`/`0` commands. No firmware change, reflash, GPIO change or W output is
needed. DMX remains UART2, 250000 baud, 8N2, address 001, RGB channels 1–3.

The unchanged command contract is `0` OFF, `1` RED, `2` BLUE, `3` GREEN,
`4` WHITE, `?` STATUS. Color confirmation requires the exact `OK` response plus
matching `STATUS`. The parser validates firmware, production DMX mode, baud rates,
RGB, zero DMX errors, increasing uptime and DMX frame progress. Corrupt, truncated,
unexpected, mismatched or timed-out responses disconnect the session. Health
queries occur periodically, without resending an unchanged color. Opening a port
may still produce an electrical reset on some USB adapters; the worker recovers.

After failure, the worker closes the connection, clears current acknowledgment,
backs off, reopens and queries firmware, then sends the latest desired color.
This is the retry mechanism; obsolete commands are discarded rather than queued.
Loss is detected on the next write or health query plus its timeout. A USB outage
cannot command an unpowered/disconnected ESP32: the last physical output may
remain until power loss/reconnect. Reconnection reapplies SCANNING if capture is
still active. A restarted ESP32 starts OFF according to its existing firmware.
On graceful backend shutdown, capture is interrupted/closed before a best-effort
OFF; forced termination cannot guarantee OFF. Device-side watchdog behavior would
require a separate, justified firmware change and is not added here.

## Configuration

Install `requirements-api.txt` (now includes pyserial). Lighting is disabled by
default; existing local API behavior is retained. Add these to the private backend
`.env` or the systemd environment file:

| Variable | Default | Meaning |
| --- | --- | --- |
| `CABEK_LIGHTS_ENABLED` | `0` | Set `1` to enable the bridge |
| `CABEK_LIGHTS_PORT` | `auto` | Prefer an explicit `/dev/serial/by-id/...`; auto accepts exactly one by-id device |
| `CABEK_LIGHTS_BAUD` | `115200` | Existing console baud; must match firmware |
| `CABEK_LIGHTS_TOKEN` | empty | Local status/state bearer credential, at least 32 characters |
| `CABEK_LIGHTS_MAINTENANCE_TOKEN` | empty | Distinct maintenance credential, at least 32 characters |
| `CABEK_LIGHTS_TIMEOUT` | `1` | Response/write timeout seconds, positive and ≤60 |
| `CABEK_LIGHTS_RECONNECT_SECONDS` | `2` | Reconnection retry interval seconds, positive and ≤60 |
| `CABEK_LIGHTS_HEALTH_SECONDS` | `5` | Hardware and application health interval seconds, positive and ≤60 |

Generate two independent credentials with `python -c 'import secrets;
print(secrets.token_hex(32))'` (run twice). Keep them off the frontend and out of Git.
The API uses the existing `.env` loader; process environment takes precedence.
Do not use PlatformIO Serial Monitor while the bridge owns the device.
Use one process/worker and omit `--reload` when lighting is enabled.

## HTTP contract and authorization

The launcher binds to `127.0.0.1`. All lighting endpoints require
`Authorization: Bearer <control-or-maintenance-token>`. Only the maintenance token
may use `/test`. No browser credential or CORS authorization exemption is added.
The existing dossier/capture API remains within its documented trusted local
boundary; never expose it through an unauthenticated public proxy, since capture
itself controls hardware. Remote access requires the deployment's authenticated
application gateway for **all** capture and lighting routes.

- `GET /api/v1/lights/status`: 200, desired state/color/RGB, readiness/error,
  active capture ID, maintenance-test activity and last transition time.
  Also `enabled`, `connection_status`, `esp32_available`, `synchronized`,
  `acknowledged_color`, latest firmware status, `last_successful_command`,
  `last_error`, and `last_seen_at`.
- `POST /api/v1/lights/state`, body `{"state":"OFF"}`: 202 on acceptance.
  Accepts the six state names subject to transition/readiness/capture rules.
- `POST /api/v1/lights/test`, body `{"color":"RED"}`: 202; maintenance only,
  idle healthy READY tunnel only. Accepts RED/BLUE/GREEN/WHITE/OFF. Output expires
  after 3 seconds or is superseded immediately by a capture/state change.

202 means desired state accepted, **not hardware acknowledgment**. `rgb` is desired
RGB (including the current blink phase); `synchronized` becomes true only after
serial confirmation of that output. Firmware status and last successful command
are historical when disconnected; `acknowledged_color` clears on disconnect.
A response may show `synchronized=false` while a new state is being transmitted.
These fields prove serial firmware behavior, never physical LED appearance.

Errors use the existing `{"error":{"code":"...","message":"..."}}` envelope:
401 missing/bad credential; 403 control token used for maintenance; 409 transition,
readiness or capture conflict; 422 invalid/extra JSON fields; 503 lighting disabled.
Enabled hardware outages return status normally and accept desired state without
blocking the application or pretending the ESP32 responded.

## Linux startup and manual API testing

The sibling firmware repo includes `deploy/cabek-tunnel.service` and a full Linux
installation procedure in `docs/lighting-deployment.md`. It launches the existing
backend and its embedded bridge together; install and enable it on the tunnel PC.
The sample uses `/opt/cabek-ai` and port 8000; adapt installation paths to the host.

```sh
python -m api --port 8000
```

In a separate terminal set `LIGHTS_TOKEN` and `LIGHTS_MAINTENANCE_TOKEN` locally.
`LIGHTS_BASE=http://127.0.0.1:8000/api/v1/lights`. Do not put real credentials into
saved scripts or browser bundles.

```sh
curl -i "$LIGHTS_BASE/status"
curl -sS -H "Authorization: Bearer $LIGHTS_TOKEN" "$LIGHTS_BASE/status"
curl -i -H "Authorization: Bearer $LIGHTS_TOKEN" -H 'Content-Type: application/json' \
  -d '{"color":"RED"}' "$LIGHTS_BASE/test"
curl -i -H "Authorization: Bearer $LIGHTS_MAINTENANCE_TOKEN" -H 'Content-Type: application/json' \
  -d '{"color":"RED"}' "$LIGHTS_BASE/test"
curl -i -H "Authorization: Bearer $LIGHTS_TOKEN" -H 'Content-Type: application/json' \
  -d '{"state":"OFF"}' "$LIGHTS_BASE/state"
curl -i -H "Authorization: Bearer $LIGHTS_TOKEN" -H 'Content-Type: application/json' \
  -d '{"state":"READY"}' "$LIGHTS_BASE/state"
```

Expect 401, then 200, then 403; test is 202 only when healthy/idle. Observe desired
and acknowledged values separately. Use the **existing** React capture action or
`tunnel-capture-api.md` curl flow to start real capture; fabricating SCANNING or
COMPLETED through this API is intentionally rejected. Check red throughout photo
delivery, green after every required original is validated, then white after
10 seconds. Also check a new capture during green, failure/cancellation, browser
refresh, missing services, USB reconnection and backend/ESP32 restart.
