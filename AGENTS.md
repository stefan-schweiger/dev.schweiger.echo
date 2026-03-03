# AGENTS.md - AI Agent Guide for dev.schweiger.echo

This file provides context for AI coding agents working on this project.

## What this project is

A Homey smart home app (SDK v3) that integrates Amazon Echo/Alexa devices. Users can control Echo speakers and displays, send TTS messages, trigger routines, and automate playback through Homey flows. The app communicates with Amazon's Alexa API via the `alexa-remote2` library and maintains a persistent WebSocket connection for real-time state updates.

## Tech stack

- **Language:** TypeScript 5.9+ targeting Node.js 22
- **Platform:** Homey SDK v3 (`homey` package, app runs locally on Homey Pro hub)
- **Core dependency:** `alexa-remote2` (callback-based Amazon Alexa API client)
- **Build:** `tsc` compiles to `.homeybuild/` directory
- **Linting:** ESLint with `eslint-config-athom` (Homey's standard config)
- **Formatting:** Prettier
- **No test framework** is currently configured

## Key files and their responsibilities

| File | Purpose | LOC |
|------|---------|-----|
| `app.ts` | App lifecycle, reconnection logic, event routing between API and devices | ~195 |
| `api.ts` (root) | Thin HTTP endpoint wrappers exposing connect/disconnect/reset/status | ~17 |
| `lib/api.ts` | `AlexaApi` class - all Alexa communication, WebSocket listeners, device/sound/routine fetching, media control, TTS | ~614 |
| `lib/connection.ts` | `ConnectionState` enum, error categorization (`categorizeError`), DNS reachability check | ~108 |
| `lib/helpers.ts` | `promisify`, `promisifyWithOptions`, `sleep` utilities for callback-to-promise conversion | ~31 |
| `lib/logger.ts` | `Logger` class with Sentry integration, PII filtering, conditional diagnostic logging | ~47 |
| `drivers/echo/driver.ts` | Echo driver - registers 4 flow action cards (say, command, sound, routine), handles pairing | — |
| `drivers/echo/device.ts` | Echo device - dynamic capability management, album art, real-time state sync | — |
| `drivers/group/driver.ts` | Speaker group driver (same structure as echo, filters WHA family) | — |
| `drivers/group/device.ts` | Speaker group device (same as echo device) | — |

## Architecture patterns

### Homey Compose
Source config lives in `.homeycompose/` and per-driver compose files (`driver.compose.json`, `driver.flow.compose.json`, `driver.settings.compose.json`). The `app.json` in the root is **generated** - edit the compose sources instead.

### Event flow
```
Amazon Alexa API (WebSocket)
  → AlexaApi (lib/api.ts) emits 'device-info'
    → EchoRemoteApp (app.ts) routes to correct device via deviceEmit()
      → EchoDevice/GroupDevice updates capabilities
```

Group events propagate to member devices: when a group's state changes, `deviceEmit()` also emits to all child devices in that group.

### Connection state machine
States are defined in `lib/connection.ts`:
```
DISCONNECTED → CONNECTING → CONNECTED
CONNECTED → DISCONNECTING → DISCONNECTED
CONNECTED → RECONNECTING → CONNECTING → CONNECTED
CONNECTED → ERROR → RECONNECTING → ...
```

Auto-reconnect uses exponential backoff (30s → 1m → 2m → 4m → 8m → 15m cap, max 10 attempts). Auth failures (expired cookies) stop reconnection immediately since they require user re-authentication.

### Error categorization
`categorizeError()` in `lib/connection.ts` classifies errors from `alexa-remote2`:
- **auth** (no retry): cookie expired, 401 unauthorized
- **network** (retry): DNS failure, connection reset, socket hang up
- **transient** (retry): timeout, empty response, HTTP2 session invalid
- **unknown** (no retry): unknown device, unrecognized errors

### Callback-to-promise wrapping
`alexa-remote2` uses callback-style APIs. The helpers in `lib/helpers.ts` wrap these as promises. When adding new API calls, use `promisify()` or `promisifyWithOptions()`.

### Caching
`node-cache` with different TTLs per resource type:
- Devices: 5 minutes
- Sounds: 60 minutes
- Routines: 1 minute

Cache is cleared on disconnect/reset.

### Authentication
Uses `alexa-remote2`'s proxy-based OAuth. The app starts a local proxy server on port 3081. The user opens the proxy URL in a browser to log in to Amazon. On success, cookie data is stored in Homey settings. Cookie refresh interval is set to 4 days.

## Driver capabilities

Both `echo` and `group` drivers support these Homey capabilities:
- `speaker_playing` - play/pause
- `speaker_next` / `speaker_prev` - track navigation
- `speaker_shuffle` - shuffle toggle (supports 'disabled' state)
- `speaker_repeat` - repeat modes: track, playlist, none, disabled
- `speaker_track` / `speaker_artist` / `speaker_album` - media metadata
- `volume_set` - volume (0-1 scale, converted to 0-100 for Alexa API)
- `volume_set.notifications` - notification volume (optional, dynamically added)

## Device families
The app filters Alexa devices by `deviceFamily`:
- `ECHO`, `KNIGHT`, `ROOK` → individual Echo devices (echo driver)
- `WHA` (Whole Home Audio) → speaker groups (group driver)

40+ Echo models are mapped in `DEVICES` constant in `lib/api.ts` with name and generation number. Unknown models still work, they just don't get a specific icon.

## Supported Amazon regions
16 regions defined in `SERVERS` constant in `lib/api.ts`. Some regions use non-standard Alexa hostnames (e.g., `amazon.se` and `amazon.pl` use `layla.amazon.com`).

## Internationalization
- App UI translations in `locales/` (en, de, fr, nl)
- Flow card titles and capability labels translated in compose files
- Alexa API language passed via `LANG_MAP` in `lib/api.ts` (12 languages)

## Common tasks

### Adding a new Amazon region
1. Add the Amazon page → Alexa server mapping to `SERVERS` in `lib/api.ts`
2. Add a language mapping to `LANG_MAP` if needed
3. Add the region option to the settings UI in `settings/index.html`

### Adding a new Echo device model
Add an entry to the `DEVICES` constant in `lib/api.ts` with the device type ID, name, and generation. Optionally add an SVG icon to `drivers/echo/assets/`.

### Adding a new flow action
1. Define the action in `drivers/echo/driver.flow.compose.json` (and group equivalent if applicable)
2. Register the action handler in `drivers/echo/driver.ts` `onInit()`
3. Implement the API method in `lib/api.ts` if needed
4. Add translations to `locales/`

### Adding a new device capability
1. Add the capability to `drivers/echo/driver.compose.json`
2. Register the capability listener in `drivers/echo/device.ts` `onInit()`
3. Handle incoming state updates in the `device-info` event handler
4. Add capability options translations if needed

## Build and run

```sh
npm install          # Install dependencies
npm run build        # Compile TypeScript → .homeybuild/
npm run lint         # Run ESLint
homey app run        # Deploy to Homey for development
homey app install    # Install on Homey for production
```

## Important caveats

- **Do not edit `app.json` directly** - it is generated from `.homeycompose/` sources. Edit the compose files instead.
- **Do not edit files in `.homeybuild/`** - this is the compiled output directory.
- The `alexa-remote2` library uses callbacks with non-standard signatures (e.g., `checkAuthentication` has reversed callback params). Always verify callback signatures when wrapping new methods.
- Sentry logging (`homey-log`) is currently disabled in `app.ts` due to rate limiting. The `homeyLogger` is set to `undefined`.
- Volume values use 0-1 scale in Homey but 0-100 in the Alexa API. Conversion happens in `lib/api.ts`.
- The proxy server on port 3081 is used for OAuth only. It must be cleaned up properly on reset (handled in `cleanup()`).
- PII filtering in `lib/api.ts` (`filterLogMessage`) redacts customer name, email, ID, and address from log output.
