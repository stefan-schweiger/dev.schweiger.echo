"""Amazon Echo app — lifecycle, push dispatch, and web-api entrypoints."""

import asyncio
from typing import Optional

from homey import app

from .lib.alexa import AlexaService
from .lib.connection import categorize_error

SYNC_INTERVAL_MS = 5 * 60 * 1000


class App(app.App):
    async def on_init(self) -> None:
        self.alexa = AlexaService(self.log)
        self.alexa.on_state_change = self._on_state_change
        self.alexa.on_volume = self._on_volume
        self.alexa.on_media = self._on_media
        self.alexa.on_reauth = self._on_reauth
        self.alexa.on_login_data = self._persist_login_data

        login_data = self.homey.settings.get("login_data")
        email = self.homey.settings.get("email")
        if login_data and email:
            # Defer (re)connect so on_init returns fast and drivers initialize first.
            self.homey.set_timeout(
                lambda: asyncio.create_task(self._auto_connect(email, login_data)), 2000
            )
        elif self.homey.settings.get("auth"):
            # Upgraded from the old Node app: that auth blob is incompatible.
            self.log("Old auth present without login_data — user must reconnect after update")

        self._sync_interval = self.homey.set_interval(
            lambda: asyncio.create_task(self._sync()), SYNC_INTERVAL_MS
        )

    async def _auto_connect(self, email: str, login_data: dict) -> None:
        self.log("Auto-connecting from stored session …")
        try:
            await self.alexa.start_from_stored(email, login_data)
            self.log("Auto-connect complete")
        except Exception as e:  # noqa: BLE001
            self.error(f"Auto-connect failed: {type(e).__name__}: {e}")
            await self._report_error(e)

    async def on_uninit(self) -> None:
        if getattr(self, "_sync_interval", None) is not None:
            self.homey.clear_interval(self._sync_interval)
        await self.alexa.stop()

    # --- web-api entrypoints (called from api.py) ------------------------
    async def connect(self, email: str, password: str, otp: str) -> dict:
        # Login takes ~15s; run it in the background so this web-api call returns
        # immediately (Homey's settings API call times out at 10s). The settings
        # page polls /status to reflect progress and the final result.
        self.alexa.state = "connecting"
        self.alexa.last_error = None
        self._login_task = asyncio.create_task(self._do_login(email, password, otp))
        return {"started": True}

    async def _do_login(self, email: str, password: str, otp: str) -> None:
        self.log("Connect requested — starting interactive login")
        try:
            login_data = await self.alexa.start_interactive(email, password, otp)
        except Exception as e:  # noqa: BLE001
            self.error(f"Login failed: {type(e).__name__}: {e}")
            return
        await self.homey.settings.set("email", email)
        await self.homey.settings.set("login_data", login_data)
        self.log("Login successful — connected")

    async def disconnect(self) -> None:
        await self.alexa.stop()

    async def reset(self) -> None:
        await self.alexa.stop()
        await self.homey.settings.unset("login_data")
        await self.homey.settings.unset("email")
        await self.homey.settings.unset("auth")

    def status(self) -> dict:
        return {
            "connected": self.alexa.state == "connected",
            "state": self.alexa.state,
            "error": self.alexa.last_error,
        }

    # --- internals -------------------------------------------------------
    async def _persist_login_data(self, login_data: dict) -> None:
        await self.homey.settings.set("login_data", login_data)

    async def _sync(self) -> None:
        try:
            await self.alexa.sync()
        except Exception as e:  # noqa: BLE001
            await self._report_error(e)

    def _find_device(self, serial: str):
        for driver_id in ("echo", "group"):
            try:
                return self.homey.drivers.get_driver(driver_id).get_device({"id": serial})
            except Exception:  # noqa: BLE001 - NotFound
                continue
        return None

    async def _fanout(self, serial: str, action) -> None:
        device = self._find_device(serial)
        if device is not None:
            await action(device)
        amazon = self.alexa.devices.get(serial)
        if amazon is not None and len(amazon.device_cluster_members) > 1:
            for member_serial in amazon.device_cluster_members:
                if member_serial == serial:
                    continue
                member = self._find_device(member_serial)
                if member is not None:
                    await action(member)

    async def _on_volume(self, serial: str, value: float) -> None:
        await self._fanout(serial, lambda d: d.apply_volume(value))

    async def _on_media(self, serial: str, media) -> None:
        await self._fanout(serial, lambda d: d.apply_media(media))

    async def _on_state_change(self, state: str, reason: Optional[str]) -> None:
        if state == "connecting":
            return
        connected = state == "connected"
        for driver_id in ("echo", "group"):
            try:
                driver = self.homey.drivers.get_driver(driver_id)
            except Exception:  # noqa: BLE001 - driver not initialized yet
                continue
            for device in driver.get_devices():
                if connected:
                    await device.set_available()
                else:
                    await device.set_unavailable(reason or "No connection")

    async def _on_reauth(self) -> None:
        # Drop the stale session so the settings page prompts for a fresh login.
        await self.homey.settings.unset("login_data")

    async def _report_error(self, e: Exception) -> None:
        info = categorize_error(e)
        self.error(f"[{info['category']}] {info['message']}")
        if info["category"] != "transient":
            await self.homey.flow.get_trigger_card("error").trigger({"error": info["message"]})
        if info["needs_reauth"]:
            await self._on_reauth()


homey_export = App
