"""Web-API endpoints called by the settings page. Names match the manifest `api` map.

`homey` is injected by the SDK at call time, so it needs no import here.
"""

from typing import Any


async def connect(*, homey, query: dict, params: dict, body: dict[str, Any]) -> dict:
    return await homey.app.connect(body["email"], body["password"], body["otp"])


async def status(*, homey, query: dict, params: dict, body: dict) -> dict:
    return homey.app.status()


async def disconnect(*, homey, query: dict, params: dict, body: dict) -> None:
    await homey.app.disconnect()


async def reset(*, homey, query: dict, params: dict, body: dict) -> None:
    await homey.app.reset()


async def set_debug_logging(*, homey, query: dict, params: dict, body: dict[str, Any]) -> None:
    await homey.app.set_debug_logging(bool(body.get("enabled")))


async def set_site(*, homey, query: dict, params: dict, body: dict[str, Any]) -> dict:
    return await homey.app.set_site(str(body.get("site") or ""))


async def probe_dns(*, homey, query: dict, params: dict, body: dict) -> dict:
    # Optional "host" overrides the server this session is on; useful for testing
    # a name that is expected to fail.
    return await homey.app.probe_dns(str((body or {}).get("host") or "") or None)
