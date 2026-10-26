# AGENTS.md - AI Agent Guide for dev.schweiger.echo

This is the **single authoritative guide** for this project. `CLAUDE.md` intentionally just points here.

## What this project is

A Homey app (**Python** Apps SDK v3, runs on Homey's CPython 3.14 runtime) that integrates Amazon Echo/Alexa devices. Users control Echo speakers and displays, send TTS, trigger routines, and automate playback through Homey flows. It talks to Amazon through **`aioamazondevices`** — the same library behind Home Assistant's official "Alexa Devices" integration — over a headless OAuth login and a persistent HTTP/2 push connection.

> History: this app was previously TypeScript/Node on `alexa-remote2`. It was rewritten in Python on `aioamazondevices` in v2.0.0 for a more durable connection (Amazon was deprecating the legacy cookie web surface the old stack depended on). If you find references to `app.ts`, `lib/api.ts`, the proxy login, or `node-cache`, they are stale.

## Tech stack

- **Language/runtime:** Python (Homey runs CPython **3.14**); `"runtime": "python"`, `"pythonVersion": "3.14"` in the manifest.
- **Core dependency:** `aioamazondevices==17.2.3` (pulls `aiohttp`, `httpx[http2]`, `orjson`, `anyio`, `beautifulsoup4`, `h2`, `yarl`, …).
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
| `app.py` | App lifecycle; deferred auto-connect from stored session; routes push events to devices (group media events fan out to cluster members; volume does not); periodic `sync` heartbeat; web-api methods; `error` flow trigger. Exports `homey_export = App`. |
| `api.py` | Web-API endpoints (`connect`/`status`/`disconnect`/`reset`); names match the manifest `api` map. `homey` is injected at call time — do **not** `from homey import Homey`. |
| `lib/alexa.py` | `AlexaService` — wraps `AmazonEchoApi`: interactive + stored login, HTTP/2 push subscription, command methods (say/announce/whisper/voice/command/sound/routine/volume/playback/do-not-disturb), the per-device settings endpoint (screen power/brightness), alarms (read, on/off, skip), volume scaling, DND polling, pairing list, sounds/routines/voices lookups. |
| `lib/connection.py` | `ConnectionState` enum + `categorize_error()` over `aioamazondevices` exceptions. |
| `lib/diagnostics.py` | Opt-in bridge from the library's Python logger into Homey's app log (redacts bearer/CSRF values). Toggled from app settings; see **Diagnostics & support reports**. |
| `lib/constants.py` | `DEVICES` (deviceType → icon name/generation) and `VOICES` (Amazon Polly voices for "Say/Announce with Voice") and `SPEAK_VOICES` (the subset Alexa.Speak renders). |
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
      → App dispatch (app.py): find device by serial; group *media* events fan out to
        cluster members (volume does not — see below)
        → EchoDevice/GroupDevice.apply_volume / apply_media
```

**Volume is not fanned out.** Since `aioamazondevices` 15.1.1 a member's volume push also
updates its parent speaker group's cached volume to the **average** of the members, and the
library sends its whole volume cache on every event — so the group serial now arrives in
payloads it never appeared in before. Mirroring that average back down onto the members
(which `_fanout` still does for media, and did for volume up to 2.2.3) would overwrite each
member's real volume with the average. Nothing is lost by dropping it: setting a group's volume sends the
sequence *per cluster member*, so Amazon pushes every member individually and the group tile
gets its average for free. Media still fans out — group media state is reported on the group.

**An ended media session clears `speaker_playing`.** Amazon has no "stopped" state. When
playback ends the device drops out of `np/list-media-sessions` (the library then reports
`player_state=None` for it, or an empty payload when nothing plays anywhere). Up to 2.3.0
`apply_media` ignored both, so the last PLAYING stuck on the tile, and stayed there across
restarts because Homey keeps capability values. Support log `c5021fa8`: an Echo Spot showed
"playing" and Pause got a 404. The list also drops a device briefly around track changes, so
`AlexaService._clear_ended_media_sessions()` sends `None` only after a device has been missing
for `MEDIA_IDLE_GRACE_S` across two observations (push events and the 5-min heartbeat). That
means a stale tile can take one or two heartbeats to clear. A member is never cleared while
its group is in session, and an ended session is **not** fanned out (each device is judged on
its own, so a member playing on its own isn't stopped by its idle group).
`AlexaService.playback()` treats a **404 on Pause** as success, since there is nothing to pause.
A 404 on Play/Next/Previous raises "Nothing is playing or paused on this device" instead of
the library's bare "Request failed: Not Found".

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
AVS stream whenever a device's DND flips (Alexa app, voice, or routine). Since
`aioamazondevices` 16.0.0 (upstream PR #1010) the library handles it and emits its whole
cached `{serial: enabled}` map on `on_dnd_event`, which `_after_login` subscribes to
(`_handle_dnd`). Up to 15.1.3 the message was dropped as *"Unknown HTTP2 push message"* and
the app monkey-patched the library's push predicate and handler to get at it.

`sync_dnd()` → `api.sync_dnd_state()` (one `GET api/dnd/device-status-list`, whole account per
request) stays as the safety net: it seeds state at connect and re-syncs each heartbeat, so a
dropped push channel degrades to ≤5 min lag instead of breaking. Its result arrives through the
same `on_dnd_event` signal, so both paths converge on `App._on_dnd` → `EchoDevice.apply_dnd`
(which ignores repeats). The library leaves speaker groups and stereo pairs out of the map.

### Authentication & connection
- **Sign-in (interactive):** `AmazonEchoApi(session, email, password)` → `api.login.login_mode_interactive(otp)` runs OAuth+PKCE + `POST /auth/register`, yielding a long-lived `refresh_token` (+ `macDms`, cookies). The whole `login_data` dict is stored in Homey settings under `login_data` (email under `email`). Authenticator-app TOTP is **required** (SMS/email codes don't work).
- **Reconnect (stored):** `api.login.login_mode_stored_data()` — no credentials needed; access tokens/cookies are re-minted from the refresh token. Since 16.4.0 it also re-registers the device capabilities (an extra token refresh plus a `PUT`) on every call. So connect and session recovery call it. The 5-minute heartbeat only calls it while the account customer id is still missing (a stored login that failed partway), because that lookup is all it ever did there.
- **Auto-connect is deferred** off `on_init` via `set_timeout(..., 2000)` so drivers initialize first (otherwise `on_state_change` hits "Driver Not Initialized").
- **Login runs in the background** (`asyncio.create_task`): `connect()` returns immediately because Homey's settings web-api call times out at **10 s** while login takes **~15 s**. The settings page polls `/status` (`disconnected`/`connecting`/`connected`/`error`, plus `error` message) to drive the UI.
- **Push:** `start_http2_processing(httpx_client, on_reauth_required=...)` opens the AVS directive stream; it reconnects itself. A true `CannotAuthenticate` triggers re-auth. The httpx client comes from `AlexaService._push_client()` and **must never be None**: the library stores whatever it is handed and dereferences it on every reconnect, so a None client kills the channel permanently (`'NoneType' object has no attribute 'stream'`, retried 5s→600s). That is why the client is created on demand there instead of during login — a login that fails partway keeps `_api` alive for the heartbeat to retry, and the heartbeat's `ensure_push_channel()` is a caller too.
- **Stale session self-heal:** every command method on `AlexaService` (`say`, `play_sound`, `run_routine`, `set_volume`, `playback`, DND, device settings, …) carries `@heal_stale_session`. On a `CannotAuthenticate` it calls `try_recover_session()` once and retries. It exists because the 6-hourly cookie renewal can leave a session Amazon no longer accepts while reads and the push channel keep working, so the app looks connected and only commands fail — a user reported "works for 12 hours, then I lose control without disconnecting" and had to restart the app. Only `CannotAuthenticate` (401/403/407) triggers it: a 404 arrives as `CannotRetrieveData` and is a normal answer to a playback command on an idle device, so recovering on that would rebuild the push channel every time someone pressed Next. `try_recover_session()` is bounded by `RECOVERY_MAX_ATTEMPTS`, so a permanently rejected session falls through to the real exception.
- **CSRF token logging:** `_note_csrf_state()` logs one line whenever the library's `_csrf_cookie` appears or disappears (after login, after a cookie renewal, on the heartbeat). `clear_cookies()` drops that token and it only returns when a later response sets one; if it never does, writes are refused while reads keep working. The transition-only logging is there to make that visible in a diagnostic report without a line every five minutes.

### Account customer id — two formats, only one works
Amazon returns the account id in two shapes and they are not interchangeable:
`/auth/register` gives the **obfuscated** `amzn1.account.…` form, while the device list carries
the **directed** form (`A146V8AS9QOCRT`) as `deviceOwnerCustomerId`. Every behaviours payload
(Speak / Announce / Sound / routines) needs the *directed* one — post a sequence with the
obfuscated id and Amazon answers **400 Bad Request**, so sign-in looks perfect and then nothing
ever speaks. `AlexaService._log_account_context()` guards with `is_directed_customer_id()`,
because its ownership counts are meaningless with the wrong form (an obfuscated id never matches
any `deviceOwnerCustomerId`, so every device reads as somebody else's).

**Since `aioamazondevices` 16.0.0 the library gets this right itself.** `obtain_account_customer_id()`
is one `GET api/users/me` and takes the directed id of the *signed-in* account from its `id`, so
an Amazon Household no longer matters. The id is kept in `login_data["account_customer_id"]`,
which the app persists on every heartbeat, and a stored login only looks it up when it is
missing, i.e. once after upgrading from an older version. Up to 15.1.3 the library instead
polled the device list up to 30 times for the just-registered virtual device (which Amazon stops
listing on accounts with many app registrations) and never persisted the result, so the app
seeded the id from `/auth/register` and the raw device list (matching its own registration serial
to pick the right Household member) and short-circuited the library's lookup. All of that is gone.

Still open on top of this: for a household member who genuinely does *not* own the devices, the
library sends `account_customer_id` as both `customerId` and `target.customerId`
(`implementation/sequence.py`), which is likely wrong — `AmazonDevice.device_owner_customer_id`
is already parsed and is the plausible value. Unverified; needs a household account to test.

### Amazon server selection (Auto vs. pinned)
The host every request goes to comes from `login_data["site"]`. Two things set it:

- **Auto** (default, `amazon_site` setting empty): the library pins it once during interactive
  login from its `/api/welcome` → `alexaHostName` sniff, and `AlexaService._heal_domain_pin()`
  re-checks it on every stored login (see Known limitations for why).
- **Pinned, during interactive sign-in**: `_interactive_login_attempt()` replaces the library's
  `_domain_refresh_auth_cookies()` with a no-op for the duration, so the whole sign-in stays on
  `amazon.com` (where the OAuth flow starts anyway, and which serves an EU account's device list
  fine) and `_apply_pinned_site()` moves the session afterwards. Without that, the library's sniff
  switches to the account's regional host mid-login and the customer-id lookup and device fetch
  die on it — making the setting useless to exactly the people who need it, since they cannot get
  signed in at all.
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
  library's raw `CannotAuthenticate: Unable to extract authorization code from url: …` (a bare
  `KeyError: 'openid.oa2.authorization_code'` up to 16.3.0), i.e. Amazon returned no
  authorization code: wrong/expired 2-step code, a captcha, or an extra verification step.
  `Your Amazon account has no country/region set …` is the library's `MissingCountryError`
  (16.8.1), raised by the `users/me` lookup when `countryOfResidence` is empty. Without a country
  everything falls back to amazon.com, and in home-assistant/core#186163 every device showed
  offline. Only the account owner can fix it, in Amazon's Country/Region settings. It is
  deliberately not `needs_reauth`, which would delete the stored login.
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
- Hold resolved addresses: `ttl_dns_cache=DNS_CACHE_TTL_S` (120s) on the connector, because aiohttp's 10s default makes one login or one routine picker visit a burst of lookups, and some home resolvers answer the first few and then return `gaierror(-5) No address associated with hostname` — which then sticks, apparently negatively cached. Reported by several French testers on `alexa.amazon.fr`/`.de`; an on-device test resolved the same name four times, then failed every attempt after that. Fewer lookups is the mitigation; a quick retry is not, since it hits the same cached miss. The connector itself is shared for the app's lifetime (`_shared_connector`, `connector_owner=False`) because that cache lives on the connector, not the session, and every login attempt builds a fresh session for clean cookies; only `stop()` closes it.

### Volume scale
Homey uses 0–1, the Alexa API uses 0–100. Conversion lives in `lib/alexa.py`.

### Error categorization
`categorize_error()` (`lib/connection.py`) maps library exceptions: `CannotAuthenticate`/`CannotRegisterDevice` → `auth` (no retry, needs re-auth); `CannotConnect` → `network` (retry); `CannotRetrieveData` → `transient` (retry); else `unknown`. The `error` flow trigger fires for non-transient errors. `unresolved_host_message()` in the same module owns the one sentence used whenever a request died in DNS, and every surface that shows a user an error reuses it: the connection state, `login_error_message()` for a failed sign-in, and `_explained()` in `drivers/echo/driver.py`, which wraps each action card's run listener. Anything that is not a DNS failure keeps its original exception so logs stay precise.

### Alexa lists (shopping / to-do)
The `list-item-added` app-level trigger card fires when something is added to an Alexa list. `AlexaService._handle_todo_event()` is subscribed to the library's `on_todo_event` in `_after_login`, next to the volume and media signals, and filters to `AmazonListEventType.CREATED`. List names come from `listName`, falling back to `LIST_TYPE_NAMES` since Amazon returns none for the two built-in lists. **`api.todo_lists` is not filled for free:** the library only populates it from `_refresh_basic_data()`, which only `get_devices_data()` calls, and this app never calls that (see `refresh_devices`). So `_after_login` fetches the index explicitly and `_list_name()` refetches on a miss — without that every `list` token is the raw base64 list id.

Three action cards go with it. `get-list-items` takes a list autocomplete and returns three tokens — `items` (JSON with each item's `id`, `name`, `status` and `version`), `names` (the un-ticked names as plain text, so announcing a shopping list needs no scripting) and `count`. `remove-list-item` and `complete-list-item` take the list and item IDs as **text** args, not autocompletes, because the point is to drop the trigger's `list_id` / `item_id` tokens into them; that is also why the trigger emits those ids.

Both writes are versioned — Amazon's optimistic concurrency, per item, rejected if stale — and there is no way to omit it. `_live_version()` looks it up so the version stays off the Flow cards, which take only the list and item ids. That costs one read per write.

**Read limit:** `LIST_READ_LIMIT` is 100, the value the library hardcodes and upstream/Home Assistant exercise. Reaching past it via `_todo_handler.get_list_items(list_id, limit=500)` was tried and Amazon answered **400**, and the library has no pagination. So `get-list-items` is truncated at 100 items, and a write against an item beyond that cap fails because `_live_version()` cannot see it. Accepted rather than worked around.

**Token plumbing:** `_on_list_item()` forwards the service's payload to `trigger()` verbatim, because the payload keys are the card's token names. Re-listing them by hand broke twice, and the SDK's error does not help: a *missing* token reports as `Invalid value for token <name>. Expected <type> but got <class 'str'>`, since `flow_card_trigger.py:61` prints `type(token_name)` rather than the value's type. "got str" means absent, not mistyped.

**Known edge, accepted:** the library's `_handle_item_change_event` resolves the item with an unguarded `list_items[item_id]`, so if Amazon's push ever beats its own list read the `KeyError` is swallowed by `http2.py` and the event never reaches us. An earlier version of this app intercepted the push and retried instead; it was removed because the race was raised by a review bot (upstream #893, closed without the guard landing), no user has reported it, and one missed trigger with a logged traceback is cheaper than a third push interceptor. If it ever does show up in reports, the fix belongs upstream.

**The card has no list picker on purpose.** This SDK's `FlowCardTrigger.trigger()` accepts tokens only (`TypeError: takes from 1 to 2 positional arguments but 3 were given`), so there is no state for a run listener to match an argument against — an `args` autocomplete on a trigger card cannot be filtered here. The card fires for every Alexa list and exposes the list name as a token, so Flows narrow it down with a Logic card. (`AlexaService.list_lists()` existed for that picker and was removed with it; it is in git history if a future action card needs a list autocomplete, where args do work.)

**Scope limit worth knowing:** this reaches *Alexa* lists only, via `alexashoppinglists/api/v2/lists` on `alexa.amazon.<tld>` — the Alexa app's own endpoint, which Amazon kept working for customers after retiring the public List Management REST API on 2024-07-01. The "Your Lists" feature on the Amazon retail site is a different product and is not reachable this way; upstream issue #1022 was closed as exactly this confusion.

### DNS: unpin-on-failure resolver
`_UnpinOnFailureResolver` (`lib/alexa.py`) is the connector's resolver. An IPv4-only lookup that fails is retried with `AF_UNSPEC` and the answer filtered back to IPv4. Measured on an affected device (log `7b57c33b`): three consecutive `alexa.amazon.de` A lookups failed with EAI_NODATA at 1-3 ms and the very next unpinned lookup of the same name succeeded in 38 ms, warming the cache for every chain hop after it. We still pin to IPv4 because Homey has no IPv6 route; the Alexa hosts have no AAAA records, so the unpinned query returns the same addresses. The retry calls `getaddrinfo` directly with **`flags=0`** rather than delegating to `super().resolve()`, which would add `AI_ADDRCONFIG` — that can suppress the AAAA half on a host with no global IPv6 address and turn the retry into a repeat of the failed lookup. `flags=0` is also what the probe measured and what Node's `dns.lookup` does. **This is an opportunistic retry, not a proven fix:** the unpinned form has been observed failing too, and HomeyScript (which resolves this way by default) failed for one reporter anyway. Both failure paths re-raise the *original* error so the user-facing message keeps the real host and EAI code, but **all three outcomes log a line** (retry worked / failed both ways / unpinned answer had no IPv4). A silent re-raise would make a report from an affected user indistinguishable from a pre-2.2.1 one, and the three cases need different answers. These lines go to the app log unconditionally, not behind the debug-logging setting, so any diagnostic report carries them.

### DNS probe (diagnostics only)
Background and the full investigation record live in [`docs/dns-investigation.md`](docs/dns-investigation.md) — read that before re-opening this, it lists what has already been ruled out and why the app does not resolve names itself.

`lib/dnsprobe.py` runs once a request has already failed in DNS, fired from `App._maybe_probe_dns()` in the error path. Gated on the `debug_logging` setting and throttled by `DNS_PROBE_INTERVAL_S` (10 min) because the probe is itself a burst of lookups, which is what appears to provoke the failure it investigates. It logs `/etc/resolv.conf`, then asks `https://1.1.1.1/dns-query` (bare IP, so no name resolution and no port 53; verified to validate against certifi) for the target's CNAME chain, then walks every hop of that chain plus `api.amazon.com` as a control through `getaddrinfo`, timed and reporting the EAI code. Point of the design: no test in the support thread ever compared two resolution paths on the same machine at the same moment. The chain is derived at runtime and the last good one persisted in the `dns_chain` setting as a fallback — never hardcoded, because the `tp.<id>-frontier` name is an Amazon internal identifier and a stale hardcoded name would return NXDOMAIN and look exactly like the failure being diagnosed. It resolves nothing the app then connects to; see "Known limitations" for why the app does not do its own resolution. Also triggerable on demand via `POST /dns-probe` (`App.probe_dns()`), wired to the "Run network test" button in app settings — that path ignores both the debug-logging gate and the throttle since it was asked for explicitly, defaults to `AlexaService.alexa_host` for the name to probe, and returns as soon as the probe starts because a Homey web-api call times out at 10 s.

### SSML
`call_alexa_speak(device, text)` renders SSML if `text` is SSML markup (verified on-device). Used for **whisper** (`<amazon:effect name="whispered">`) and **Say with Voice** (`<voice name="…"><lang xml:lang="…">`). Escape message content with `xml.sax.saxutils.escape`.

**Speak only renders some Polly voices.** `Alexa.Speak` honours `<voice>` only for the voices in Amazon's "Supported Amazon Polly voices" table (`SPEAK_VOICES` in `lib/constants.py`) — in any language, e.g. Brian speaks English on a German account. Any other voice is **dropped without an error** and the text is read in the account's own voice, so "Swedish - Astrid" sounded like the German Alexa. The same SSML sent as an **AlexaAnnouncement with `speak.type: "ssml"`** does render them (verified on-device: Astrid, Mads; no chime was heard), which is what the old alexa-remote2 app sent for every voice. Hence two cards rather than one card choosing per voice: **Say with Voice** only offers `SPEAK_VOICES` and keeps speaking during Do Not Disturb (verified on-device: Speak talks, the announcement doesn't), **Announce with Voice** offers every voice and is silenced by it. A Homey dropdown can't clear an autocomplete value already picked, so a single card with a Say/Announce choice could still end up as "Say + Astrid". Flows saved before the split that hold a non-Speak voice on Say with Voice are announced instead (`say_with_voice`), so they keep working. The library only builds *text* announcements, so `_announce_ssml` takes its node and flips the type (private API, pinned to aioamazondevices==17.2.3), passing the plain text as `display_text` so Echo Show screens don't print the markup. The content `locale` made no difference on-device (account locale, the voice's locale and alexa-remote2's hard-coded `de-DE` all worked).

### Routines *are* device specific

The library's own comment on `call_routine()` says routines aren't device specific. They are.
Amazon stores a routine's sequence with the placeholders `ALEXA_CURRENT_DSN` /
`ALEXA_CURRENT_DEVICE_TYPE` wherever a step was configured for **"the Alexa device you speak
to"**, and `utils.replace_routine_placeholders()` substitutes whichever device the sequence is
posted from (`implementation/sequence.py:_build_operation_node`). So a routine that replies on
the device you talked to answers on the device *we* post from. Routines with no placeholders
are genuinely device-agnostic — the payload is the routine's own sequence either way.

`api.call_routine()` hardcodes `devices[api._default_device_serial]`, which would be an Echo
the user never picked. The `run-routine` card does carry a device argument (Homey injects one
into every card in a driver's flow compose file — check `app.json` if in doubt), so
`AlexaService.run_routine(name, serial)` takes it and posts the sequence from that device via
`api._sequence_handler.send_message(device, AmazonSequenceType.Routines, name)`. That is the
only single-device entry point for a routine: `call_routine()` goes through
`_call_alexa_command_per_cluster_member()`, which would run the whole routine once per cluster
member if the chosen Echo were half of a stereo pair.

`AlexaService._seed_default_device()` covers the fallback — `call_routine()` when the card's
device is no longer in the session's device list. That serial is filled in only by the
library's `_init_default_device()`, which runs inside `get_devices_data()`, the call
`refresh_devices()` deliberately avoids (it fetches the *basic* device list; see its
docstring), so unseeded it is `""` and the fallback dies on `KeyError: ''`. It prefers the
first online device with a **single** cluster member, for the stereo-pair reason above plus
the fact that a `WHA` serial isn't accepted by the sequence endpoint at all. Up to 14.2.2
`default_device` was a lazy property with a first-online-device fallback and none of this was
needed; 15.1.0 turned it into persisted state (`settings.json` via `set_default_device()`,
which this app never calls) and moved the fallback out of our path.

### Music and Stop

**Play music** (`play-music`) is the library's public `call_alexa_music(device, phrase,
provider_id)`, with the provider picked from `api.music_providers`: the services linked in the
Alexa app that support `Alexa.Music.PlaySearchPhrase`, the default first. The library fetches
that list once per session and `call_alexa_music` checks the card's choice against the same
list, so a service linked later only appears after the app reconnects.

**Stop playback** (`stop-playback`) is `send_media_command(device, AmazonMediaControls.Stop)`,
which the library turns into an `Alexa.DeviceControls.Stop` sequence per cluster member
(stopping both halves of a stereo pair is harmless, unlike a routine running twice). Both
cards carry `$filter: capabilities=speaker_playing`, i.e. devices with `AUDIO_CONTROLS`.

**Stop playback on all devices** (`stop-playback-all`, app level) sends one Stop node
targeted at `ALEXA_ALL_DSN` / `ALEXA_ALL_DEVICE_TYPE` (`ALL_DEVICES_TARGET`), the payload
alexa-remote2 sends for `deviceStopAll`: no device or locale of its own,
`isAssociatedDevice: false`. Amazon expands the placeholder itself, so Echos that aren't
paired in Homey stop too. `AlexaService.stop_all()` builds the library's single-device Stop
node and retargets it (private API, pinned to aioamazondevices==17.2.3). Not yet verified
on-device: whether either Stop also silences a ringing alarm or timer.

### Alarms

Four Echo cards: **Turn an alarm on or off** (`set-alarm-state`, picks from that Echo's alarms),
**An alarm is set for [today/tomorrow]** (`alarm-is-set`), **Get the alarms for [day]**
(`get-alarms`; tokens `count`, `time` and `label` of the first ring, `times`) and **Skip the
alarms [day]** (`skip-alarms`). The last three are for "public holiday tomorrow → ask → skip"
flows. The holiday check and the question come from other apps.

**Reading.** The library fetches `GET alexa.amazon.<tld>/api/notifications` but keeps only each
device's next alarm, computed from the process clock. `AlexaService._alarms()` reads the same
list itself (types `Alarm`/`MusicAlarm`, the card's serial). `_alarm_rules()` turns a schedule
into dateutil rules the way the library's `_parse_next_occurrence` does: Amazon's recurrence
names from `RECURRING_PATTERNS` with the account country's weekend, `rRuleData.recurrenceRules`,
from `originalDate` at `originalTime`. Those times are the Echo's wall clock, so "now" comes from
the Homey's timezone (`AlexaService.timezone`, kept current by `app.py`; `tzdata` ships with
`python-dateutil`), never from the process clock's. "Today" only counts rings still to come.
`ON` and `SNOOZED` count as on.

**Writing** goes to Amazon's alarms API, the AVS "Alarms REST API". Amazon's doc page for it is
gone; the reference is alexa-remote2's `changeNotification`/`cancelNotification`, which
ioBroker.alexa2 has used for its alarm switches since 2022:

```
PUT <alexaApiUrl>/v1/alerts/alarms/<notificationIndex>/cancel                  # off
PUT <alexaApiUrl>/v1/alerts/alarms/<notificationIndex>/activate                # on
PUT <alexaApiUrl>/v1/alerts/alarms/<notificationIndex>/nextOccurrence/cancel   # skip the next ring
    Authorization: Bearer <login_data["access_token"]>
activate body: {"trigger": {"scheduledTime": "2026-10-27T06:30:00"}, "extensions": [],
                "assets": [{"type": "TONE", "assetId": "<sound.id> or MUSIC-<musicAlarmId>"}]}
```

- `alexaApiUrl` (`https://api.eu.amazonalexa.com`, …) comes from `GET alexa.amazon.<tld>/api/endpoints`,
  once per session. The AVS home region would point there too, but the library notes that it is
  not always right (`http2.py` `_check_avs_site`).
- The id in the path is the notification's `notificationIndex`, not its `id`.
- The bearer token is the one the heartbeat refreshes every 5 minutes. A 401 goes through
  `@heal_stale_session` like every other command.
- The library raises `CannotRetrieveData` for any status but 200, so `_alarm_request` counts every
  2xx as success (`SUCCESS_PHRASES`).
- `activate` sends the alarm's real next ring. A one-time alarm whose day has passed is set for its
  time today, or tomorrow if that's over too. alexa-remote2 sends `originalDate` plus one day,
  which can still be in the past.

**Skipping.** Amazon can only skip an alarm's *next* ring. So when the card asks for tomorrow and
an alarm still rings later today, `skip_alarms` names it and refuses rather than skipping today's
ring. A one-time alarm is cancelled outright. Every skip is remembered in `_skipped_rings` (alarm
id → ring), because a second `nextOccurrence/cancel` would skip the ring *after* the skipped one:
a Flow that fired twice would silence the next working day too. The same record keeps a skipped
ring out of `alarms_on`, so "An alarm is set for tomorrow" turns false after a skip. It lives in
memory and is lost on restart.

**Not yet verified on-device (2026-10-26).** Whether `activate` accepts this body and answers 200
or 204. Whether re-activating a repeating alarm keeps its recurrence. Whether a
`nextOccurrence/cancel` shows in `/api/notifications` at all: if it does, `_skipped_rings` is
redundant, and a skip made in the Alexa app could be read too. With debug logging on, the
notification list is dumped as `JSON 'alarms' data`; compare it before and after a skip.

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

Nothing is pending right now.

**What changed under us in 16.9.2 → 17.2.3** (fifteen releases in four days; no workaround was
dropped). The library's login, sequence, to-do, DND and history code, `capabilities.py` and the
schedule/sound constants are byte-identical, so every private call pinned to 16.9.2 (domain pin,
cookie renewal, SSML announcements, Stop all, routines, smart-home push opt-out) is unchanged.
- **A features error no longer fails the device list here.** Since 16.9.3 (upstream PR #1203)
  `update_devices()` raises `CannotRetrieveData` when the GraphQL features answer carries any
  error, where 16.9.2 built the devices without features. Still so in 17.2.3. This app uses no
  features, and `refresh_devices()` runs at every sign-in and at pairing, so
  `AlexaService._build_devices_without_features_on_error()` wraps the handler's
  `_get_endpoints_features` to answer `{}` on `CannotRetrieveData`, as 16.9.2 did. Transport and
  auth errors still raise. A local choice, not a pending fix: keep it as long as nothing here
  reads `AmazonDevice.features`.
- **Push reconnects resync state** (16.9.4): on every HTTP/2 reconnect after the first, the
  library re-reads DND and media/volume and sends them through `on_dnd_event` /
  `on_media_state_event` / `on_volume_state_event`, so a dropped stream no longer waits for the
  heartbeat. It also re-runs the endpoint features query for its sensors (failures are caught and
  logged as `Sensor resync after HTTP2 reconnect failed`); nothing here uses that.
- **Smart-home sensor pushes** (17.0.0) are handled now, but only arrive with the
  `smarthome-events` push format, which `_opt_out_of_smart_home_push()` still leaves out.
- **Speaker-group volume is the floored member average** (17.2.1), to match the Alexa app. It
  was rounded.
- **Echo Glow** devices are built from GraphQL alone (17.2.0), with the family `Endpoint Only`,
  which isn't in `SINGLE_FAMILIES`, so pairing doesn't offer them.
- `AmazonDevice` lost `sensors` and gained `primary_display_category`; `session_request`
  gained a keyword-only `fail_fast`. Nothing here used or needed either.
- **Not affected:** `get_devices_data()`, where the sensor, notification and comms changes
  live, because this app never calls it (see `refresh_devices`). `device.online` still comes
  from the devices-v2 list, as before.

**What changed under us in 16.8.1 → 16.9.2** (no workaround was dropped):
- **Amazon's new sign-in pages** (from ~2026-10-20, rolled out per account): the OAuth URL
  now answers with an identifier-first page whose form posts to the *relative* `/ax/claim?…`,
  asking for the e-mail only; the password follows on a page of its own. Up to 16.8.1 the
  library posted e-mail and password to that relative URL in one go and aiohttp refused it
  (`InvalidUrlClientError: /ax/claim?…`, upstream #1188, issue #47 here). 16.9.1 (upstream
  PR #1194) resolves form actions against the page URL and submits each field on the page
  that asks for it.
- **The "no OTP page" message changed** to `Unexpected login page '<title>', expected OTP
  code entry`, so `OTP_PAGE_MISSING` now matches that; the page title is what to look for in
  a report. A rejected e-mail, password or code now usually raises Amazon's own error text
  instead of `Unable to extract authorization code`.
- **Verified on-device (2026-10-24, amazon.de account with TOTP):** the e-mail is posted to
  `/ax/claim`. The password page and the classic `auth-mfa-otpcode` page both post to
  `/ap/signin`, so the expected `/ap/maplanding` 404 is tolerated as before. That matches
  upstream's tests. The fresh sign-in completed in ~11 s.
- **Not every account may get those pages.** One amazon.de report on #1188 saw a new
  `unified-otp-form` instead (`otpCode` without the `auth-mfa-otpcode` id, posting to
  `/ax/claim/auth`). With that page, 16.9.2 fails with `Unexpected login page '…'`. Because the
  maplanding 404 is only tolerated after a chain that started at `/ap/signin`
  (`_ignore_ap_signin_error`), it could also end in `CannotRetrieveData: Request failed: Not
  Found`. If a report shows either error, that's the cause. `login.py` is unchanged in 17.2.3,
  so this still holds.

**What changed under us in 16.3.1 → 16.8.1** (no workaround was dropped):
- **Do not run 16.3.0 with aiohttp ≥ 3.14.4.** aiohttp 3.14.4 (2026-10-05) changed cookie
  domain matching. The library set cookies against the literal host `.amazon.<tld>`, so they
  stopped reaching `alexa.amazon.<tld>` and every call answered 401 (upstream #1133 and #1132,
  fixed in 16.3.2). 16.3.0 doesn't cap aiohttp, so rebuilding its dependencies after that date
  pulls in the bug. `_refresh_website_cookies` goes through the library's `set_cookies` and
  picks up the fix.
- **The device list comes from GraphQL** (16.3.1): `_device_handler.get_base_devices()` became
  `update_devices()`. See `refresh_devices` for what that changes.
- **Stored login re-registers capabilities** (16.4.0, upstream PR #1108), and the set now
  includes `Alexa.Mobile.Push` 1.1 with the `smarthome-events` format. With that, Amazon pushes
  an `FDAL_UPDATE` for every light, plug and sensor in the household. The library doesn't handle
  them yet and nothing here wants them, so `_opt_out_of_smart_home_push()` (called from
  `_build`) puts back the 1.0 entry without that format, as registered up to 16.3.0. All other
  capability changes are kept. This is a local choice, not a pending fix: re-check it on bumps,
  and drop it if a feature ever needs smart-home events.
- **The device query still lists every endpoint.** Since 16.3.1 `listEndpoints` has no
  `displayCategory` filter, so the household's smart-home devices come back too. The library
  skips them because they have no devices-v2 entry, but they make the response bigger and show
  up in the DEBUG dump. Narrowing the query to `ALEXA_VOICE_ENABLED` (what 16.3.0 used) was
  considered and not done: GraphQL now decides which devices exist at all, so any Echo missing
  from that category would disappear from the app.
- **A missing authorization code** raises `CannotAuthenticate` instead of `KeyError`
  (`login_error_message`).
- Announcements take a `display_text` (16.7.0), which `_announce_ssml` now uses.
- **An account with no country of residence is refused** (16.8.1, upstream PR #1172):
  `MissingCountryError` from every interactive sign-in, and from a stored login that still
  has to look up the customer id. `MISSING_COUNTRY_MESSAGE` (`lib/connection.py`) tells the user
  where to set the country. This runs before `_apply_pinned_site()`, so pinning a server in app
  settings doesn't get such an account past it.

**Landed in 16.0.0** (shipped with 16.3.0 here):
- DND over push ([PR #1010](https://github.com/chemelli74/aioamazondevices/pull/1010)):
  `_allow_dnd_push_events()`, `_intercept_dnd_push_events()` and `_handle_dnd_push()` are gone,
  replaced by an `on_dnd_event` subscription (see **Do Not Disturb is pushed *and* polled**).
- The account customer id is looked up via `api/users/me` and persisted in `login_data`, so the
  seeding in `_save_to_file`, the `_seed_customer_id_from_*()` helpers and
  `_skip_known_customer_id_lookup()` are gone (see **Account customer id**).

**Landed in 15.1.3:** the voice-history fetch is gated on `on_history_event.frozen`
([our PR #1045](https://github.com/chemelli74/aioamazondevices/pull/1045)), so
`AlexaService._skip_unused_history_fetch()` is gone. Nothing here subscribes to
`on_history_event`, so the library no longer fetches seven days of voice history on every
"somebody spoke" push. The `Processing vocal history record` redaction in `lib/diagnostics.py`
is kept as a backstop for the day something does subscribe.

## Known limitations

- **Shuffle/repeat are read-only** — `aioamazondevices` exposes no command to set them.
- **Sounds** come from a curated static list (`SOUNDS_LIST` in the library), not a live fetch.
- **Routines are triggered by name** — old "Run Routine" flows from the TS app (which stored an automationId) need the routine re-selected.
- **A routine's "device you speak to" is the device on the flow card** — the `run-routine`
  card posts the sequence from the Echo it names, so steps configured for the spoken-to device
  answer there. **Fixed in 2.2.4**; up to and including 2.2.3 they answered on an arbitrary
  Echo (the library's default device). See **Routines *are* device specific**.
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
  version we've shipped, 14.1.3 → 15.1.3, has the same sniff, so it is not a version
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
- **Speaker groups (`WHA`) are a media target only** — playback, volume and now-playing work on a
  group, but Amazon's speech and command endpoints want an individual device: Speak/Announce, `Say
  with Voice`, `textCommand`, sounds and routines all need a real device serial, and a `WHA` serial
  is not accepted. That, not an oversight, is why `drivers/group/` has **no**
  `driver.flow.compose.json` and `GroupDriver.on_init` registers nothing — every card in
  `drivers/echo/driver.flow.compose.json` is scoped `driver_id=echo` for the same reason. DND is
  excluded on the same grounds (see **Device settings**). Don't "fix" this by widening the device
  filter to include groups; the cards would appear and then fail at run time.
- **No LED-ring control** — upstream's rule of thumb is *"as you cannot control them via Alexa Mobile App, we cannot as well"* ([aioamazondevices #924](https://github.com/chemelli74/aioamazondevices/issues/924)).

## i18n

App UI strings in `locales/` (en, de, fr, nl). Flow-card and capability labels live inline in the compose files. When adding a flow action/voice/region, update the relevant locale entries and compose labels.
