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
