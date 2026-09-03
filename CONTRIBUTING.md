# Contributing

Thanks for taking the time to contribute!

This is a community app that integrates Amazon Echo and Alexa devices into Homey.
It is not affiliated with Amazon or with Athom/Homey.

## Where to go

- **A bug in the app** → [open a bug report](https://github.com/stefan-schweiger/dev.schweiger.echo/issues/new?template=bug_report.yml).
  Please work through the troubleshooting checklist in the template first — most connection problems
  are covered there, and a diagnostic report ID makes a real difference.
- **A question, or a feature idea** → the [Homey Community topic](https://community.homey.app/t/120365).
  Feature discussion happens there rather than in the issue tracker.

## Before submitting a bug

- Have you read the error message in app settings, or the one on the failing Flow card?
- Have you searched [existing issues](https://github.com/stefan-schweiger/dev.schweiger.echo/issues?q=is%3Aissue)
  (including closed ones)?
- Are you on the latest version of the app, and on Homey firmware 13 or newer?
- On **Homey Self-Hosted**: does the Homey container run in **privileged mode**? Homey's Python
  runtime does not work otherwise, so the app cannot start at all.
- Have you turned on **Enable diagnostic logging** in app settings, reproduced the problem, and sent a
  diagnostic report? Without it there is usually not enough detail in the logs to find the cause.

## Pull requests

Development setup — prerequisites, the `homey app dependencies install` step, and the Colima recipe — is in
[README.md](README.md). [AGENTS.md](AGENTS.md) documents the architecture, the Amazon auth model and the
Homey networking gotchas in depth; it is worth reading before changing anything in `lib/alexa.py`.

A good pull request:

- **Is scoped to one thing.** Unrelated changes belong in their own PR.
- **Edits the right files.** `app.json` is generated from `.homeycompose/` — never edit it directly.
  `python_packages/` and `.python_cache/` are generated too.
- **Uses relative imports.** The app loads as package `app`; see [AGENTS.md](AGENTS.md).
- **Adds new dependencies via the manifest.** Use `homey app dependencies add`, which updates
  `pythonPackages` — don't hand-edit the bundled packages.
- **Is tested on a real Homey.** There is no test framework; run `homey app run` and exercise the change.
  Say in the PR what you tested and on which Echo models.
- **Updates the docs it invalidates** — `README.md`, `AGENTS.md`, the locale files, or the changelog entry.
- **Adds locale strings for all languages** if it touches user-facing text (`locales/` has en, de, fr, nl).
  An English-only string is fine if you cannot translate it; say so and it will be filled in.

If your change affects how the app talks to Amazon, please note whether it depends on behaviour of a
specific `aioamazondevices` version — several places in `lib/alexa.py` are deliberately pinned to the
version in the manifest and carry a comment saying so.

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). By participating, you are expected
to uphold it.
