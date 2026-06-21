# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**Read [`AGENTS.md`](AGENTS.md) — it is the single authoritative guide for this project** and is kept current. It covers what the app is, the tech stack, build/run commands (incl. the Colima Docker recipe), the architecture and event flow, the Amazon auth/connection model, Homey networking gotchas, common tasks, and known limitations.

To avoid two docs drifting apart, all project guidance lives in `AGENTS.md`; this file intentionally only points there.

## The essentials

- Homey **Python** app (CPython 3.14, Apps SDK v3) integrating Amazon Echo/Alexa via the `aioamazondevices` library. Requires Homey firmware ≥ 13.
- **`app.json` is generated** from `.homeycompose/` — edit the compose sources, never `app.json`.
- Dependencies go in the manifest field **`pythonPackages`**; manage them with `homey app dependencies add/install` (needs Docker). `python_packages/` and `.python_cache/` are generated.
- Build/run: `homey app run` (dev) / `homey app install` (persistent). No test framework — validate on a real Homey.
- The app loads as package `app`; use **relative imports** (`from .lib… `, `from ...app import App`) and export `homey_export` per module.

Everything else — and the *why* behind these — is in `AGENTS.md`.
