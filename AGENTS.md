# AGENTS.md - AI Agent Guide for dev.schweiger.echo

This is the **single authoritative guide** for this project. `CLAUDE.md` intentionally just points here.

## What this project is

A Homey app (**Python** Apps SDK v3, runs on Homey's CPython 3.14 runtime) that integrates Amazon Echo/Alexa devices. Users control Echo speakers and displays, send TTS, trigger routines, and automate playback through Homey flows. It talks to Amazon through **`aioamazondevices`** — the same library behind Home Assistant's official "Alexa Devices" integration — over a headless OAuth login and a persistent HTTP/2 push connection.

> History: this app was previously TypeScript/Node on `alexa-remote2`. It was rewritten in Python on `aioamazondevices` in v2.0.0 for a more durable connection (Amazon was deprecating the legacy cookie web surface the old stack depended on). If you find references to `app.ts`, `lib/api.ts`, the proxy login, or `node-cache`, they are stale.

## Tech stack

- **Language/runtime:** Python (Homey runs CPython **3.14**); `"runtime": "python"`, `"pythonVersion": "3.14"` in the manifest.
- **Core dependency:** `aioamazondevices==14.1.3` (pulls `aiohttp`, `httpx[http2]`, `orjson`, `beautifulsoup4`, `h2`, `yarl`, …).
- **Platform:** Homey Apps SDK v3. **Requires Homey firmware >= 13.0.0** (Python apps).
- **No test framework.** Validate with `homey app run` against a real Homey + Amazon account.
- Type-checking (optional, local): `homey-stubs` + pyright.

## Build & run

The CLI compiles Python dependencies inside a per-architecture Docker builder and bundles them into `python_packages/`.

```sh
homey app dependencies install   # compile + bundle deps (Docker required); after editing pythonPackages
homey app run                    # dev: build, install, run, stream logs
homey app install                # persistent install (use for long runs / soak)
homey app dependencies add <pkg> # add a dependency (updates manifest pythonPackages + recompiles)
```

- **`app.json` is generated** from `.homeycompose/` + per-driver `*.compose.json`. Never edit `app.json` directly.
- **Dependencies live in the manifest field `pythonPackages`** (an array of pip specifiers) — **not** `pythonDependencies` (that field is stale docs; Athom's schema only validates `pythonPackages`). A `pyproject.toml` is generated *inside the build container* from `pythonPackages`; do not keep one in the repo.
- **Colima users:** the builder bind-mounts a temp build dir that must be inside Colima's shared `$HOME` mount, and the CLI looks for `/var/run/docker.sock`. Use:
  ```sh
  TMPDIR="$HOME/.cache/homey-build-tmp" \
    homey app dependencies install --docker-socket-path "$HOME/.colima/default/docker.sock"
  ```
  (Pass the same env/flag to `homey app run` / `install` via `DOCKER_HOST`.)
- **`homey app run --remote` log noise:** after a while the remote debug channel drifts and the app floods stderr with `IPCSocket._send_to_socket … was never awaited` / `send_system`. This is the debug stream dropping, **not** an app crash. For stable/long runs use `homey app install` (logs then live in Homey Developer Tools).

## Key files

| File | Purpose |
|------|---------|
| `app.py` | App lifecycle; deferred auto-connect from stored session; routes push events to devices (group events fan out to cluster members); periodic `sync` heartbeat; web-api methods; `error` flow trigger. Exports `homey_export = App`. |
| `api.py` | Web-API endpoints (`connect`/`status`/`disconnect`/`reset`); names match the manifest `api` map. `homey` is injected at call time — do **not** `from homey import Homey`. |
| `lib/alexa.py` | `AlexaService` — wraps `AmazonEchoApi`: interactive + stored login, HTTP/2 push subscription, command methods (say/announce/whisper/voice/command/sound/routine/volume/playback), volume scaling, pairing list, sounds/routines/voices lookups. |
| `lib/connection.py` | `ConnectionState` enum + `categorize_error()` over `aioamazondevices` exceptions. |
| `lib/constants.py` | `DEVICES` (deviceType → icon name/generation) and `VOICES` (Amazon Polly voices for "Say with Voice"). |
| `drivers/echo/driver.py` | Pairing (filters `ECHO`/`KNIGHT`/`ROOK`) + flow-action registration (incl. sound/routine/voice autocompletes). |
| `drivers/echo/device.py` | Capabilities, capability listeners, `apply_volume`/`apply_media`, album art, availability. |
| `drivers/group/*` | Speaker-group driver/device — same structure; pairing filters the `WHA` family. |
| `settings/index.html` | Sign-in UI: email/password/OTP form; three views (form / connecting / connected) driven by polling `/status`. |

## Architecture patterns

### Package & imports
The app is loaded as a package named **`app`** (entry module `app.app`). Use **relative imports**: `from .lib.alexa import AlexaService` in `app.py`; `from ...app import App` (under `TYPE_CHECKING`) in `drivers/*/`. Every entry module (`app.py`, each `driver.py`/`device.py`, `api.py`) defines `homey_export`. Do not import `Homey` from the top-level `homey` package (not exported) — endpoint functions receive `homey` as an argument.

### Cross-component messaging (no event bus)
The Python SDK has no supported custom EventEmitter. The app pushes to devices by looking them up and calling methods directly:
```python
device = self.homey.drivers.get_driver("echo").get_device({"id": serial})  # data.id == Amazon serial
await device.apply_volume(value)
```
Devices reach the service via `cast(App, self.homey.app).alexa`.

### Event flow
```
Amazon (HTTP/2 AVS push)
  → aioamazondevices on_volume_state_event / on_media_state_event
    → AlexaService callbacks (lib/alexa.py)
      → App dispatch (app.py): find device by serial; group events fan out to cluster members
        → EchoDevice/GroupDevice.apply_volume / apply_media
```

### Authentication & connection
- **Sign-in (interactive):** `AmazonEchoApi(session, email, password)` → `api.login.login_mode_interactive(otp)` runs OAuth+PKCE + `POST /auth/register`, yielding a long-lived `refresh_token` (+ `macDms`, cookies). The whole `login_data` dict is stored in Homey settings under `login_data` (email under `email`). Authenticator-app TOTP is **required** (SMS/email codes don't work).
- **Reconnect (stored):** `api.login.login_mode_stored_data()` — no credentials needed; access tokens/cookies are re-minted from the refresh token.
- **Auto-connect is deferred** off `on_init` via `set_timeout(..., 2000)` so drivers initialize first (otherwise `on_state_change` hits "Driver Not Initialized").
- **Login runs in the background** (`asyncio.create_task`): `connect()` returns immediately because Homey's settings web-api call times out at **10 s** while login takes **~15 s**. The settings page polls `/status` (`disconnected`/`connecting`/`connected`/`error`, plus `error` message) to drive the UI.
- **Push:** `start_http2_processing(httpx_client, on_reauth_required=...)` opens the AVS directive stream; it reconnects itself. A true `CannotAuthenticate` triggers re-auth.

### Homey networking gotchas (important)
The Homey app sandbox has **no IPv6 route** and **no system CA store**, so:
- Force IPv4: `aiohttp.TCPConnector(family=socket.AF_INET)` and `httpx.AsyncHTTPTransport(local_address="0.0.0.0")` — otherwise connects fail with `ENETUNREACH`.
- Provide CAs via `certifi`: `ssl.create_default_context(cafile=certifi.where())` — otherwise TLS fails with `CERTIFICATE_VERIFY_FAILED`.
- A per-request `aiohttp.ClientTimeout` keeps a stalled request from hanging forever.

### Volume scale
Homey uses 0–1, the Alexa API uses 0–100. Conversion lives in `lib/alexa.py`.

### Error categorization
`categorize_error()` (`lib/connection.py`) maps library exceptions: `CannotAuthenticate`/`CannotRegisterDevice` → `auth` (no retry, needs re-auth); `CannotConnect` → `network` (retry); `CannotRetrieveData` → `transient` (retry); else `unknown`. The `error` flow trigger fires for non-transient errors.

### SSML
`call_alexa_speak(device, text)` renders SSML if `text` is SSML markup (verified on-device). Used for **whisper** (`<amazon:effect name="whispered">`) and **Say with Voice** (`<voice name="…"><lang xml:lang="…">`). Escape message content with `xml.sax.saxutils.escape`.

## Driver capabilities

Both `echo` and `group` support: `speaker_playing`, `speaker_next`/`speaker_prev`, `speaker_track`/`speaker_artist`/`speaker_album`, `volume_set`. `speaker_shuffle`/`speaker_repeat` are present but **read-only** (`setable: false`).

## Device families

Filtered by Amazon `device_family`: `ECHO`/`KNIGHT`/`ROOK` → echo driver; `WHA` (Whole-Home Audio) → group driver. Device `data.id` is the Amazon `serial_number` (stable — existing paired devices survive the migration without re-pairing).

## Common tasks

- **Add a flow action:** define it in `drivers/echo/driver.flow.compose.json`, register it in `EchoDriver.on_init` (`get_action_card(id).register_run_listener(...)`, plus `register_argument_autocomplete_listener(name, ...)` for autocompletes returning `{"name": ..., "data": {"id": ...}}`), and implement the API call in `AlexaService`.
- **Add a capability:** add to `drivers/echo/driver.compose.json`, register the listener in `device.py:on_init`, and update it from `apply_media`/`apply_volume`.
- **Add an Echo model icon:** add an entry to `DEVICES` in `lib/constants.py` and an SVG to `drivers/echo/assets/`.

## Known limitations

- **Shuffle/repeat are read-only** — `aioamazondevices` exposes no command to set them.
- **Sounds** come from a curated static list (`SOUNDS_LIST` in the library), not a live fetch.
- **Routines are triggered by name** — old "Run Routine" flows from the TS app (which stored an automationId) need the routine re-selected.

## i18n

App UI strings in `locales/` (en, de, fr, nl). Flow-card and capability labels live inline in the compose files. When adding a flow action/voice/region, update the relevant locale entries and compose labels.
