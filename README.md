# Amazon Echo for Homey

A [Homey](https://homey.app) app that integrates Amazon Echo and Alexa devices into the Homey smart home ecosystem. Control your Echo speakers, displays, and multi-room groups through Homey's automation flows and device controls.

## Disclaimer

The name "Echo" and all related trademarks and logos are the property of Amazon.com, Inc. or its affiliates. This project is not affiliated with, endorsed by, or in any way officially connected to Amazon.com, Inc.

## Features

- **Device control** - Play/pause, skip tracks, adjust volume, and view now-playing info for Echo speakers and displays
- **Text-to-speech** - Say messages, make announcements, or whisper to your Echo devices
- **Say with Voice** - Speak (or whisper) in a specific Amazon Polly voice and language via SSML
- **Voice commands** - Send voice commands to Echo devices remotely (same as speaking to the device)
- **Sounds & routines** - Play notification sounds or trigger Alexa routines from Homey flows
- **Speaker groups** - Control multi-room audio groups as a single device
- **Real-time updates** - Volume changes, playback state, and media metadata sync via a persistent HTTP/2 push connection
- **Resilient connection** - Signs in once and keeps the session alive by refreshing it automatically

### Supported devices

40+ Echo models are recognized with specific icons, including Echo (Gen 1-4), Echo Dot (Gen 1-5), Echo Plus (Gen 1-2), Echo Show (5/8/10, various generations), Echo Spot (Gen 1-2), and Echo Pop.

### Supported regions

Works with Amazon accounts across the supported marketplaces (US, UK, Canada, Australia, Germany, Spain, France, Italy, Netherlands, Sweden, Poland, Denmark, Japan, India, Brazil, Mexico, and more). Your region is **detected automatically** from your account — there is no website to select.

### Flow actions

| Action | Description |
|--------|-------------|
| Say Message | Text-to-speech with speak, announce, or whisper mode |
| Say with Voice | Speak or whisper using a specific Amazon Polly voice (with autocomplete) |
| Tell Command | Execute a voice command on the device |
| Play Sound | Play a notification sound (with autocomplete) |
| Run Routine | Execute a saved Alexa routine (with autocomplete) |

## Signing in

In the Echo app settings, enter your **Amazon email, password, and a one-time code from your authenticator app**, then press **Connect**. The app signs in directly — there is no browser/proxy step and no region to choose. After connecting, add your Echo devices through the standard Homey pairing flow.

> **2-step verification is required.** You must have an authenticator app (e.g. Google Authenticator, Microsoft Authenticator) enabled on your Amazon account. **SMS or email codes will not work.**

## Troubleshooting

If you're having issues connecting or controlling your Echo devices, try these steps:

1. **Use an authenticator-app code** — Sign-in requires a time-based code from an authenticator app. SMS or email codes are not supported by Amazon's app login.

2. **Re-connect** — In the Echo app settings, press **Disconnect**, then **Connect** again with your email, password, and a fresh authenticator code. (Use **Reset** to fully clear the stored session first if needed.)

3. **Coming from an older version?** — After updating to v2, you must sign in again once (the previous login is not compatible). If you used the **Run Routine** flow card, re-select your routine, as routines are now matched by name.

4. **Uninstall conflicting Alexa apps** — If you have another Alexa Homey app installed (e.g. the "Alexa" app), disable or uninstall it — they can interfere with each other's authentication.

5. **Check your DNS settings** — Some routers (e.g. eero) hijack DNS and break the login. Try switching to Google DNS (`8.8.8.8`) or Cloudflare DNS (`1.1.1.1`).

6. **Update the app** — Make sure you are running the latest version of the Echo app.

> **Note:** The session is refreshed automatically and is designed to stay connected for a long time. If Amazon invalidates it (e.g. after a password change or a security event), just re-connect in the app settings.

If the issue persists after trying all steps, please [open a bug report](https://github.com/stefan-schweiger/dev.schweiger.echo/issues/new?template=bug_report.md).

## Prerequisites (development)

- [Homey Pro](https://homey.app) running firmware **>= 13.0.0** (required for Python apps)
- [Homey CLI](https://apps.developer.homey.app/the-basics/getting-started) installed globally: `npm i -g homey`
- **Docker** running locally (the CLI compiles the Python dependencies inside a build container)
- An Amazon account with at least one Echo device and authenticator-app 2FA enabled

> The app itself runs on Homey's Python runtime (CPython 3.14) — you do not install Python locally.

## Getting started

1. Clone the repository:

```sh
git clone https://github.com/stefan-schweiger/dev.schweiger.echo.git
cd dev.schweiger.echo
```

2. Compile and bundle the Python dependencies (requires Docker running):

```sh
homey app dependencies install
```

3. Run the app on your Homey:

```sh
homey app run
```

4. In the Homey app, open the Echo app settings and sign in with your Amazon email, password, and an authenticator-app code.

5. After connecting, add your Echo devices through the standard Homey pairing flow.

> **Using Colima instead of Docker Desktop?** Point the CLI at the Colima socket and keep the build context inside your home directory (which Colima shares into its VM):
> ```sh
> TMPDIR="$HOME/.cache/homey-build-tmp" \
>   homey app dependencies install --docker-socket-path "$HOME/.colima/default/docker.sock"
> ```

## Project structure

```
.
├── app.py                    # App entry point (lifecycle, auto-connect, push dispatch)
├── api.py                    # Web API endpoints (connect/disconnect/reset/status)
├── lib/
│   ├── alexa.py              # AlexaService - wraps aioamazondevices (login, push, commands)
│   ├── connection.py         # ConnectionState enum + error categorization
│   └── constants.py          # Device-icon map and Amazon Polly voice list
├── drivers/
│   ├── echo/                 # Individual Echo device driver
│   │   ├── driver.py         # Flow action registration and device pairing
│   │   ├── device.py         # Capability management and real-time state updates
│   │   ├── assets/           # Device-specific SVG icons (30+ models)
│   │   └── pair/             # Pairing HTML views
│   └── group/                # Speaker group driver (same structure, filters WHA family)
├── .homeycompose/            # Homey Compose source files (generates app.json)
├── locales/                  # i18n translations (en, de, fr, nl)
├── settings/                 # App settings UI (email/password/OTP sign-in)
└── python_packages/          # Pre-compiled Python dependencies (generated, do not edit)
```

## Architecture overview

The app is built on the Homey Python Apps SDK (SDK v3) and uses [`aioamazondevices`](https://github.com/chemelli74/aioamazondevices) — the library behind Home Assistant's official Alexa Devices integration — to communicate with Amazon.

**Sign-in:** A headless OAuth + device-registration flow using your email, password and an authenticator-app code. This yields a long-lived refresh token (stored in Homey settings) from which short-lived access tokens and website cookies are minted automatically — so the session keeps working without re-entering credentials.

**Real-time updates:** A persistent HTTP/2 push connection (Amazon's AVS directive stream) delivers volume changes, playback state, and media metadata. Events flow through `AlexaService` (`lib/alexa.py`) → `app.py` → the matching device instances (group events fan out to their cluster members).

**Connection handling:** On startup the app auto-connects from the stored session. The push channel reconnects on its own; a true authentication failure surfaces a re-auth prompt. Errors are categorized in [lib/connection.py](lib/connection.py) as `auth` / `network` / `transient` / `unknown`, and an `error` flow trigger card lets users automate responses to connection problems.

**Known limitations:** Shuffle/repeat are read-only (the underlying library exposes no command to set them). Sounds come from a curated built-in list rather than a live fetch.

## Commands

| Command | Description |
|---------|-------------|
| `homey app dependencies install` | Compile & bundle the Python dependencies (needs Docker) |
| `homey app run` | Deploy and run on Homey (development; live logs) |
| `homey app install` | Install on Homey (persistent) |

## Contributing

We welcome contributions from the community. Please read our [Code of Conduct](CODE_OF_CONDUCT.md) and [Contributing Guidelines](CONTRIBUTING.md) before submitting issues or pull requests.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
