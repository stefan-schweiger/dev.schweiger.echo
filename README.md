# Amazon Echo for Homey

A [Homey](https://homey.app) app that integrates Amazon Echo and Alexa devices into the Homey smart home ecosystem. Control your Echo speakers, displays, and multi-room groups through Homey's automation flows and device controls.

## Disclaimer

The name "Echo" and all related trademarks and logos are the property of Amazon.com, Inc. or its affiliates. This project is not affiliated with, endorsed by, or in any way officially connected to Amazon.com, Inc.

## Features

- **Device control** - Play/pause, skip tracks, adjust volume, and view now-playing info for Echo speakers and displays
- **Text-to-speech** - Say messages, make announcements, or whisper to your Echo devices
- **Voice commands** - Send voice commands to Echo devices remotely (same as speaking to the device)
- **Sounds & routines** - Play notification sounds or trigger Alexa routines from Homey flows
- **Speaker groups** - Control multi-room audio groups as a single device
- **Real-time updates** - Volume changes, playback state, and media metadata sync via WebSocket
- **Auto-reconnect** - Exponential backoff reconnection on connection loss (up to 10 attempts)

### Supported devices

40+ Echo models are recognized with specific icons, including Echo (Gen 1-4), Echo Dot (Gen 1-5), Echo Plus (Gen 1-2), Echo Show (5/8/10, various generations), Echo Spot (Gen 1-2), and Echo Pop.

### Supported regions

Amazon US, UK, Canada, Australia, Germany, Spain, France, Italy, Netherlands, Sweden, Poland, Denmark, Japan, India, Brazil, and Mexico.

### Flow actions

| Action | Description |
|--------|-------------|
| Say Message | Text-to-speech with speak, announce, or whisper mode |
| Tell Command | Execute a voice command on the device |
| Play Sound | Play a notification sound (with autocomplete) |
| Run Routine | Execute a saved Alexa routine (with autocomplete) |

## Troubleshooting

If you're having issues connecting or controlling your Echo devices, try these steps:

1. **Enable 2FA with an authenticator app** — Amazon requires 2-step verification using an authenticator app (e.g. Google Authenticator, Microsoft Authenticator). **SMS or email-based 2FA will NOT work** — you will see an Amazon error page if this is the issue.

2. **Disconnect → Reset → Restart** — Go to the Echo app settings in Homey → press **Disconnect** → press **Reset** → **Restart the app**. This clears stored authentication and allows a fresh login.

3. **Verify you selected the correct Amazon website** — Make sure the Amazon region matches your account (e.g. amazon.de for Germany, amazon.co.uk for UK, amazon.com for US).

4. **Uninstall conflicting Alexa apps** — If you have another Alexa Homey app installed (e.g. the "Alexa" app), disable or uninstall it — they interfere with each other's authentication.

5. **Check your DNS settings** — Some routers (e.g. eero) hijack DNS and break the login. Try switching to Google DNS (`8.8.8.8`) or Cloudflare DNS (`1.1.1.1`).

6. **Update the app** — Make sure you are running the latest version of the Echo app.

> **Note:** Connection drops every few months are expected due to how Amazon's unofficial authentication works. Usually a disconnect/reset/restart cycle fixes it.

If the issue persists after trying all steps, please [open a bug report](https://github.com/stefan-schweiger/dev.schweiger.echo/issues/new?template=bug_report.md).

## Prerequisites

- [Homey Pro](https://homey.app) running firmware >= 5.0.0
- [Homey CLI](https://apps.developer.homey.app/the-basics/getting-started) installed globally: `npm i -g homey`
- Node.js 22+
- An Amazon account with at least one Echo device

## Getting started

1. Clone the repository and install dependencies:

```sh
git clone https://github.com/stefan-schweiger/dev.schweiger.echo.git
cd dev.schweiger.echo
npm install
```

2. Build the TypeScript source:

```sh
npm run build
```

3. Run the app on your Homey:

```sh
homey app run
```

4. In the Homey mobile app, go to the Echo app settings, select your Amazon region, and connect your Amazon account. The app starts a local proxy server for the OAuth login flow.

5. After connecting, add your Echo devices through the standard Homey pairing flow.

## Project structure

```
.
├── app.ts                    # Main app entry point (lifecycle, reconnection, event routing)
├── api.ts                    # HTTP API endpoint wrappers (connect/disconnect/reset/status)
├── lib/
│   ├── api.ts                # AlexaApi class - all Alexa communication (largest module)
│   ├── connection.ts         # ConnectionState enum, error categorization, reachability check
│   ├── helpers.ts            # promisify utilities and sleep helper
│   └── logger.ts             # Logging abstraction with Sentry support and PII filtering
├── drivers/
│   ├── echo/                 # Individual Echo device driver
│   │   ├── driver.ts         # Flow action registration and device pairing
│   │   ├── device.ts         # Capability management and real-time state updates
│   │   ├── assets/           # Device-specific SVG icons (30+ models)
│   │   └── pair/             # Pairing HTML views
│   └── group/                # Speaker group driver (same structure, filters WHA family)
├── .homeycompose/            # Homey Compose source files (generates app.json)
├── locales/                  # i18n translations (en, de, fr, nl)
├── settings/                 # App settings UI (Amazon region selection & connection)
├── @types/                   # TypeScript type declarations for untyped dependencies
└── .homeybuild/              # Build output (generated, do not edit)
```

## Architecture overview

The app is built on Homey SDK v3 and uses `alexa-remote2` to communicate with Amazon's Alexa API.

**Connection flow:** The app authenticates via a local proxy-based OAuth flow. On startup, if saved credentials exist, it auto-connects. Authentication cookies are persisted in Homey settings and refreshed periodically. If the connection drops, exponential backoff reconnection kicks in (30s, 1m, 2m, 4m, 8m, up to 15m, max 10 attempts).

**Real-time updates:** A persistent WebSocket connection receives volume changes, playback state updates, and media metadata from Amazon. These events flow through `AlexaApi` -> `app.ts` -> individual device instances.

**Caching:** Device lists (5 min TTL), sounds (1 hour), and routines (1 min) are cached with `node-cache` to reduce API calls.

**Error handling:** Errors from the Alexa API are categorized in [lib/connection.ts](lib/connection.ts) as `auth` (no retry), `network` (retry), `transient` (retry), or `unknown` (no retry). An `error` flow trigger card lets users automate responses to connection failures.

## Scripts

| Command | Description |
|---------|-------------|
| `npm run build` | Compile TypeScript to `.homeybuild/` |
| `npm run lint` | Run ESLint |
| `homey app run` | Deploy and run on Homey (development) |
| `homey app install` | Install on Homey (production) |

## Contributing

We welcome contributions from the community. Please read our [Code of Conduct](CODE_OF_CONDUCT.md) and [Contributing Guidelines](CONTRIBUTING.md) before submitting issues or pull requests.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
