# AGENTS.md - AI Agent Guide for dev.schweiger.echo

This is the **single authoritative guide** for this project. `CLAUDE.md` intentionally just points here.

## What this project is

A Homey app (**Python** Apps SDK v3, runs on Homey's CPython 3.14 runtime) that integrates Amazon Echo/Alexa devices. Users control Echo speakers and displays, send TTS, trigger routines, and automate playback through Homey flows. It talks to Amazon through **`aioamazondevices`** — the same library behind Home Assistant's official "Alexa Devices" integration — over a headless OAuth login and a persistent HTTP/2 push connection.

> History: this app was previously TypeScript/Node on `alexa-remote2`. It was rewritten in Python on `aioamazondevices` in v2.0.0 for a more durable connection (Amazon was deprecating the legacy cookie web surface the old stack depended on). If you find references to `app.ts`, `lib/api.ts`, the proxy login, or `node-cache`, they are stale.

## Tech stack

- **Language/runtime:** Python (Homey runs CPython **3.14**); `"runtime": "python"`, `"pythonVersion": "3.14"` in the manifest.
- **Core dependency:** `aioamazondevices==14.2.2` (pulls `aiohttp`, `httpx[http2]`, `orjson`, `beautifulsoup4`, `h2`, `yarl`, …).
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
| `lib/alexa.py` | `AlexaService` — wraps `AmazonEchoApi`: interactive + stored login, HTTP/2 push subscription, command methods (say/announce/whisper/voice/command/sound/routine/volume/playback/do-not-disturb), the per-device settings endpoint (screen power/brightness), volume scaling, DND polling, pairing list, sounds/routines/voices lookups. |
| `lib/connection.py` | `ConnectionState` enum + `categorize_error()` over `aioamazondevices` exceptions. |
| `lib/diagnostics.py` | Opt-in bridge from the library's Python logger into Homey's app log (redacts bearer/CSRF values). Toggled from app settings; see **Diagnostics & support reports**. |
| `lib/constants.py` | `DEVICES` (deviceType → icon name/generation) and `VOICES` (Amazon Polly voices for "Say with Voice"). |
| `drivers/echo/driver.py` | Pairing (filters `ECHO`/`KNIGHT`/`ROOK`) + flow-action registration (incl. sound/routine/voice autocompletes). |
| `drivers/echo/device.py` | Capabilities, capability listeners, `apply_volume`/`apply_media`, album art, availability. |
| `drivers/group/*` | Speaker-group driver/device — same structure; pairing filters the `WHA` family. |
| `settings/index.html` | Sign-in UI: email/password/OTP form; three views (form / connecting / connected) driven by polling `/status`. Plus two always-visible controls: the **Amazon server** picker (Auto + the marketplaces in `AMAZON_SITES`) and the diagnostic-logging switch. |

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

### Device settings (screen power / brightness) — not in the library

`aioamazondevices` doesn't wrap Amazon's per-device settings endpoint, so `AlexaService`
talks to it directly over the library's authenticated session:

```
GET  https://alexa.amazon.<domain>/api/v1/devices/<deviceAccountId>/settings/<name>
     → {"value": "\"ON\""}                 # value is itself JSON-encoded
PUT  https://alexa.amazon.<domain>/api/v1/devices/<deviceAccountId>/settings/<name>
     body {"value": "\"ON\""}
```

| Setting | Values | Requires Amazon capability | Homey capability |
|---|---|---|---|
| `displayPower` | `"ON"` / `"OFF"` | `DISPLAY_POWER_TOGGLE` | `onoff.display` |
| `brightness` | `0`–`100` | `DISPLAY_BRIGHTNESS_ADJUST` | `dim` (Homey 0–1) |
| `adaptiveBrightness` | `"ON"` / `"OFF"` | `DISPLAY_ADAPTIVE_BRIGHTNESS` | `adaptive_brightness` |

Others exist on the same endpoint but aren't wired up: `timeFormat` (`CLOCK_FORMAT_24_HR`),
`attentionSpan`, `alexaGestures`, `connectedSpeakerOption`.

Two gotchas:
- **The URL is keyed on `deviceAccountId`, not the serial number**, and `AmazonDevice` has no
  such field. It only appears in the raw `/api/devices-v2/device` response, so
  `AlexaService._harvest_device_account_ids` skims it out of the body as it passes through
  the library's `save_to_file` hook (which fires on *every* response) — no extra request.
  If that map is empty, screen reads/writes raise "No deviceAccountId known".
- **Amazon double-encodes the value**: the body is a JSON document whose `value` field is a
  JSON-encoded scalar. Encode and decode both ways.

Credit: this endpoint is mapped by `alexa-remote2` (`getDeviceSettings`/`setDeviceSettings`),
as used by the `com.amazon.alexa` Homey app.

**Screen state is polled, not pushed** — one GET per setting per screen device, after connect
(`App._refresh_screen_state`) and on each heartbeat. Non-screen devices cost nothing.

**Do Not Disturb is pushed *and* polled.** Amazon sends `PUSH_DND_STATE_CHANGE` over the
AVS stream whenever a device's DND flips (Alexa app, voice, or routine), but
`aioamazondevices` doesn't know that message type and drops it as *"Unknown HTTP2 push
message"* inside `_process_rendering_update`, before any subscriber runs.
`_allow_dnd_push_events()` widens the library's `_is_known_event_type` predicate, and
`_intercept_dnd_push_events()` replaces the api's push handler so DND events branch off to
`_handle_dnd_push` (payload: `dopplerId.deviceSerialNumber` + `enabled`) and everything else
delegates untouched. Worth upstreaming — it's one enum member plus a handler.

`sync_dnd()` (one `GET api/dnd/device-status-list`, whole account per request) stays as the
safety net: it seeds state at connect and re-syncs each heartbeat, so a dropped push channel
or a library bump that lands the patch on the floor degrades to ≤5 min lag instead of
breaking. Both paths converge on `App._on_dnd` → `EchoDevice.apply_dnd`.

### Authentication & connection
- **Sign-in (interactive):** `AmazonEchoApi(session, email, password)` → `api.login.login_mode_interactive(otp)` runs OAuth+PKCE + `POST /auth/register`, yielding a long-lived `refresh_token` (+ `macDms`, cookies). The whole `login_data` dict is stored in Homey settings under `login_data` (email under `email`). Authenticator-app TOTP is **required** (SMS/email codes don't work).
- **Reconnect (stored):** `api.login.login_mode_stored_data()` — no credentials needed; access tokens/cookies are re-minted from the refresh token.
- **Auto-connect is deferred** off `on_init` via `set_timeout(..., 2000)` so drivers initialize first (otherwise `on_state_change` hits "Driver Not Initialized").
- **Login runs in the background** (`asyncio.create_task`): `connect()` returns immediately because Homey's settings web-api call times out at **10 s** while login takes **~15 s**. The settings page polls `/status` (`disconnected`/`connecting`/`connected`/`error`, plus `error` message) to drive the UI.
- **Push:** `start_http2_processing(httpx_client, on_reauth_required=...)` opens the AVS directive stream; it reconnects itself. A true `CannotAuthenticate` triggers re-auth. The httpx client comes from `AlexaService._push_client()` and **must never be None**: the library stores whatever it is handed and dereferences it on every reconnect, so a None client kills the channel permanently (`'NoneType' object has no attribute 'stream'`, retried 5s→600s). That is why the client is created on demand there instead of during login — a login that fails partway keeps `_api` alive for the heartbeat to retry, and the heartbeat's `ensure_push_channel()` is a caller too.

### Account customer id — two formats, only one works
Amazon returns the account id in two shapes and they are not interchangeable:
`/auth/register` gives the **obfuscated** `amzn1.account.…` form, while the device list carries
the **directed** form (`A146V8AS9QOCRT`) as `deviceOwnerCustomerId`. Every behaviours payload
(Speak / Announce / Sound / routines) needs the *directed* one — post a sequence with the
obfuscated id and Amazon answers **400 Bad Request**, so sign-in looks perfect and then nothing
ever speaks. Guard with `is_directed_customer_id()` before treating an id as usable, and note
that `AlexaService._log_account_context()`'s ownership counts are meaningless without it (an
obfuscated id never matches any `deviceOwnerCustomerId`, so every device reads as somebody
else's). Seeding therefore prefers the device list; `_seed_customer_id_from_device_list()` fetches
it once explicitly rather than letting the library poll 30 times for the just-registered device.

### Amazon server selection (Auto vs. pinned)
The host every request goes to comes from `login_data["site"]`. Two things set it:

- **Auto** (default, `amazon_site` setting empty): the library pins it once during interactive
  login from its `/api/welcome` → `alexaHostName` sniff, and `AlexaService._heal_domain_pin()`
  re-checks it on every stored login (see Known limitations for why).
- **Pinned**: the user picks a marketplace in app settings → `POST /amazon-site` →
  `App.set_site()` stores `amazon_site` and reconnects. `AlexaService.pinned_site` then wins:
  `_heal_domain_pin()` returns immediately and `_apply_pinned_site()` moves the session onto the
  chosen host. This exists for networks that cannot resolve their own regional host (e.g. a DNS
  filter that breaks `alexa.amazon.fr`), not as a general preference.

Two rules worth keeping:

- **The pin moves the host, not the voice.** `country_specific_data()` derives the API host, the
  cookie/retail domain *and* `language` from one TLD, and that language lands in the `locale`
  field of every Speak/Announce/Sound payload (library `sequence.py`). So `_repin_domain()` takes
  a `locale_site` and restores the *account's* locale after the switch — otherwise someone who
  pinned `amazon.fr` for connectivity reasons would have their English Echo answer in French.
  `_language_of()` reads a marketplace's locale by applying it and putting the old one back
  (`country_specific_data` is a pure setter, no I/O).
- **Pin within the region.** `.co.uk/.de/.fr/.it/.es/.nl/.in` are all the same backend
  (`layla.amazon.com`), `.com/.ca/.com.mx/.com.br` are `pitangui`, `.co.jp/.com.au` are Japan.
  A pin inside one group is free; across groups the account's routines don't exist on the other
  backend (empty routine list). `amazon.se` and `amazon.pl` have **no** Alexa host at all and are
  therefore not offered.

### Diagnostics & support reports
What a user's diagnostic report tells you, and why these lines exist:

- **Always on** (plain `app.log`, no toggle needed):
  - `account: host …, country …, locale …, AVS home region …` — logged once per login.
    A session can be authenticated and still be pinned to the wrong regional host: the
    library starts every login on `amazon.com` and re-pins the domain **only** during
    interactive login (`/api/welcome` → `alexaHostName`, `login.py`), while the AVS region
    comes from the account. A mismatch (e.g. host `alexa.amazon.com` + region `EU`) makes
    region-scoped data — routines above all — come back empty while devices, volume and
    media keep working. The locale comes from the same pin, so it also explains a US-English
    voice on a non-US account.
  - `devices: N total — X owned by the signed-in account, …, Y shared via household` — the
    other half of the same question. In an Amazon Household devices are shared but routines
    are **per-account**, so an account that only sees devices it doesn't own may legitimately
    have zero routines. The report can't answer this otherwise: the library redacts
    `deviceOwnerCustomerId`.
  - `routines: N from <host>` on every autocomplete/run, plus a reason when the list is empty
    or a name was skipped.
  - `domain: Amazon reports this account on … — re-pinning` / `domain re-pinned to …` when the
    self-heal corrects a wrong host (see Known limitations), `domain: server pinned in app
    settings …` when the user's choice is applied, `locale set to … (account marketplace)` when
    the spoken locale is kept off the pinned host, or `domain check skipped: …` when the sniff
    itself failed.
- **Error messages worth recognising** (both from `categorize_error` / `login_error_message`):
  `Cannot resolve <host> — your network returns no address for it` means DNS, not Amazon — the
  library reports every transport failure as the same `CannotConnect`, so this walks the cause
  chain for a `socket.gaierror` and names the host. `Amazon did not complete the sign-in` is the
  library's raw `KeyError: 'openid.oa2.authorization_code'`, i.e. Amazon returned no
  authorization code: wrong/expired 2-step code, a captcha, or an extra verification step.
- **Opt-in** (settings → debug logging): the library's own DEBUG stream, incl. the full
  automations payload. Verbose — a routine-heavy account dumps every routine's sequence JSON.
- **Temporary** — `AlexaService._probe_routines()` fires only when the routine list comes back
  empty and re-asks Amazon plain and with `?limit=2000`, logging counts/status histogram (no
  names). It exists to tell apart "Amazon returned nothing", "everything was filtered as not
  `ENABLED`", and "the endpoint wanted an explicit limit". **Delete it once a report answers
  that** — see the empty-routines note under Known limitations.

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

`echo` additionally has, **only when Amazon reports the matching capability**, the screen
controls `onoff.display` / `dim` / `adaptive_brightness` (added and removed dynamically in
`EchoDevice.on_init`, never listed in `driver.compose.json` — their `capabilitiesOptions`
are, which is fine). `dim` is a system capability so Homey generates its Flow cards; the
other two don't get any (a sub-capability and a custom capability respectively), so they
ship hand-written cards whose device arg carries `"$filter": "capabilities=…"` to hide them
from screenless devices.

`echo` additionally has **`do_not_disturb`** — a *custom* capability (`.homeycompose/capabilities/do_not_disturb.json`, icon in `assets/`). Notes:
- **Echo only.** Amazon's DND list covers physical devices; speaker groups never appear in it (`aioamazondevices` filters them out too), so the `group` driver doesn't get it.
- **Custom capabilities get no automatic Flow cards.** Homey only generates those for system capabilities, so DND ships its own trigger/condition/action set in `drivers/echo/driver.flow.compose.json`, and `EchoDevice.apply_dnd` fires the triggers by hand via `EchoDriver.trigger_dnd`.
- Added unconditionally in `EchoDevice.on_init` — Amazon exposes no per-device DND capability flag. A device Amazon never reports DND for simply keeps a `None` value.

## Device families

Filtered by Amazon `device_family`: `ECHO`/`KNIGHT`/`ROOK` → echo driver; `WHA` (Whole-Home Audio) → group driver. Device `data.id` is the Amazon `serial_number` (stable — existing paired devices survive the migration without re-pairing).

## Common tasks

- **Add a flow action:** define it in `drivers/echo/driver.flow.compose.json`, register it in `EchoDriver.on_init` (`get_action_card(id).register_run_listener(...)`, plus `register_argument_autocomplete_listener(name, ...)` for autocompletes returning `{"name": ..., "data": {"id": ...}}`), and implement the API call in `AlexaService`.
- **Add a capability:** add to `drivers/echo/driver.compose.json`, register the listener in `device.py:on_init`, and update it from `apply_media`/`apply_volume`. For a **custom** capability also add `.homeycompose/capabilities/<id>.json` (+ an SVG under `assets/` if you set `icon`) and hand-write its Flow cards — Homey does not generate them (see `do_not_disturb`).
- **Add an Echo model icon:** add an entry to `DEVICES` in `lib/constants.py` and an SVG to `drivers/echo/assets/`.

## Pending upstream — local patches to delete when these land

Local workarounds that exist only because `aioamazondevices` hasn't shipped the fix yet.
**Check these on every library bump** and delete the local code once the upstream version is
released; leaving them in is not harmful (each degrades to a no-op) but they're dead weight
and they monkey-patch private internals.

| Waiting on | Local code to remove | How to verify it landed |
|---|---|---|
| [PR #1010 — *feat: move dnd to push events*](https://github.com/chemelli74/aioamazondevices/pull/1010) (open since 2026-08-05, checks green, awaiting review) | `AlexaService._allow_dnd_push_events()` and `_intercept_dnd_push_events()` / `_handle_dnd_push()` in `lib/alexa.py` | `AmazonPushMessage.DoNotDisturbChange` exists in `structures.py` |

When #1010 ships, the replacement is a proper subscription rather than a patch — the PR adds
an `on_dnd_event` Signal emitting `dict[str, bool]`, so wire it up next to the existing
volume/media signals in `_after_login`:

```python
self._api.on_dnd_event.append(self._handle_dnd_signal)   # payload: {serial: enabled}
self._api.on_dnd_event.freeze()
```

Both patches then go, and `_handle_dnd_push` collapses into that subscriber. Keep
`sync_dnd()` either way — it seeds state at connect and is the fallback if push dies. Note
the PR also moves DND out of `get_devices_data()` into `api.sync_dnd_state()`, so re-check
`sync_dnd()`'s use of `_dnd_handler.get_do_not_disturb_status()` at the same time — that
method is renamed to `sync_do_not_disturb_status()` there.

Related but not blocking us: [#625 store account customer id](https://github.com/chemelli74/aioamazondevices/pull/625)
touches the same area as the customer-id workaround in `_save_to_file` and
`_skip_known_customer_id_lookup()`. Upstream's backlog is slow (14 open PRs, oldest from
July 2025) — don't plan around any of these landing soon.

## Known limitations

- **Shuffle/repeat are read-only** — `aioamazondevices` exposes no command to set them.
- **Sounds** come from a curated static list (`SOUNDS_LIST` in the library), not a live fetch.
- **Routines are triggered by name** — old "Run Routine" flows from the TS app (which stored an automationId) need the routine re-selected.
- **A stored session can stay pinned to the wrong Amazon domain (2026-08-20)** — the host for
  every request comes from `login_data["site"]` (library `api.py`), which is written **once**, at
  the tail of interactive login (`login.py`), *after* the domain sniff: `/api/welcome` →
  `alexaHostName`. A stored login (`login_mode_stored_data`, i.e. auto-connect and the
  heartbeat) never re-checks it, and token/cookie refreshes only mutate that dict — so a
  session pinned to `amazon.com` stays there across every restart, forever.
  Consequences on a non-US account: `GET /api/behaviors/v2/automations` answers `200` with an
  empty body (routine autocomplete silently empty) while devices, volume and media keep
  working, and `language` is derived from the same pin, so TTS speaks `en-US`.
  Confirmed on one FR/EU account: a broken session ran on `alexa.amazon.com` + `en-US`, and a
  logout/login on the *same app version* sniffed `alexaHostName: alexa.amazon.fr` correctly and
  switched to `alexa.amazon.fr` + `fr-FR`. So the sniff itself works — the bug is that a bad pin
  is never revisited. How the original pin went wrong is not known from the logs (every library
  version we've shipped, 14.1.3 → 14.2.2, has the same sniff, so it is not a version
  regression). Users on an older build can cure it by signing out and in again.
  **Fixed in 2.1.1 by `AlexaService._heal_domain_pin()`** — every stored login re-runs the sniff
  and, if the pinned host disagrees, switches the domain, re-mints the website cookies, and
  writes the corrected `site` back into stored `login_data`. It is deliberately one-way (it
  corrects away from `amazon.com` but never back to it, mirroring the library's own
  `login_site != DEFAULT_SITE` guard), and it rolls the switch back if the cookie mint fails, so
  a wrong or flapping answer from Amazon can't demote a working regional session. The one case
  it won't follow is an account genuinely migrating *to* the US — that still needs a re-login.
  The v1.x TS app had an explicit Amazon-website selector (`settings/index.html` before the
  rewrite); the Python library has none — it always starts at `amazon.com`.
- **Screen controls only reach devices that advertise them** — `DISPLAY_POWER_TOGGLE` / `DISPLAY_BRIGHTNESS_ADJUST` / `DISPLAY_ADAPTIVE_BRIGHTNESS` in Amazon's capability list (Echo Show, Echo Spot, Dot with clock). Everything else gets no screen capabilities and the Flow cards filter themselves out. See **Device settings** below.
- **No LED-ring control** — upstream's rule of thumb is *"as you cannot control them via Alexa Mobile App, we cannot as well"* ([aioamazondevices #924](https://github.com/chemelli74/aioamazondevices/issues/924)).

## i18n

App UI strings in `locales/` (en, de, fr, nl). Flow-card and capability labels live inline in the compose files. When adding a flow action/voice/region, update the relevant locale entries and compose labels.
