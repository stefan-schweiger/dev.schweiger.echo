"""Amazon Echo app — lifecycle, push dispatch, and web-api entrypoints."""

import asyncio
import json
import time
from typing import Optional

from homey import app

from .lib import dnsprobe
from .lib.alexa import AlexaService, login_error_message
from .lib.constants import AMAZON_SITES
from .lib.connection import categorize_error, explained, unresolved_host
from .lib.diagnostics import DiagnosticLogging

SYNC_INTERVAL_MS = 5 * 60 * 1000

# The DNS probe is itself a burst of lookups, which is the thing that appears to
# provoke the failure it investigates, so it must not run on every heartbeat.
DNS_PROBE_INTERVAL_S = 10 * 60


class App(app.App):
    async def on_init(self) -> None:
        # Opt-in: forward the aioamazondevices library's detailed logs into the
        # app log so they land in a user's diagnostic report. Off unless the user
        # enabled it in app settings; survives restarts via the stored setting.
        self._diagnostics = DiagnosticLogging(self.log)
        if self.homey.settings.get("debug_logging"):
            self._diagnostics.apply(True)

        self.alexa = AlexaService(self.log)
        # "" / missing = Auto (let Amazon's own /api/welcome decide).
        self.alexa.pinned_site = self.homey.settings.get("amazon_site") or None
        self.alexa.on_state_change = self._on_state_change
        self.alexa.on_volume = self._on_volume
        self.alexa.on_media = self._on_media
        self.alexa.on_reauth = self._on_reauth
        self.alexa.on_login_data = self._persist_login_data
        self.alexa.on_dnd = self._on_dnd
        self.alexa.on_list_item = self._on_list_item

        # Alexa's lists are not tied to a device, so these cards are app level.
        self._list_card = self.homey.flow.get_trigger_card("list-item-added")
        get_items = self.homey.flow.get_action_card("get-list-items")
        get_items.register_argument_autocomplete_listener("list", self._autocomplete_list)
        get_items.register_run_listener(explained(self._get_list_items))
        self.homey.flow.get_action_card("remove-list-item").register_run_listener(
            explained(self._remove_list_item)
        )
        self.homey.flow.get_action_card("complete-list-item").register_run_listener(
            explained(self._complete_list_item)
        )

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
        self._pairing_reconnect_lock = asyncio.Lock()
        self._pairing_reconnect_done = False
        self._last_dns_probe_ts = 0.0
        self._dns_probe_task: Optional[asyncio.Task] = None

    async def _auto_connect(self, email: str, login_data: dict) -> None:
        self.log("Auto-connecting from stored session …")
        try:
            await self.alexa.start_from_stored(email, login_data)
            await self._refresh_screen_state()
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
        # Store the email up front so that if the login registers but a follow-up
        # step (device fetch / push) hits a transient Amazon 503, both email and
        # login_data are on disk together and auto-connect can recover — instead
        # of losing a good session. start_interactive persists login_data as soon
        # as the device is registered.
        await self.homey.settings.set("email", email)
        try:
            await self.alexa.start_interactive(email, password, otp)
        except Exception as e:  # noqa: BLE001
            self.error(f"Login failed: {login_error_message(e)}")
            return
        await self._refresh_screen_state()
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
            "debugLogging": self._diagnostics.enabled,
            "site": self.homey.settings.get("amazon_site") or "",
            "sites": list(AMAZON_SITES),
        }

    async def set_site(self, site: str) -> dict:
        """Pin the Amazon marketplace, or "" for Auto.

        Auto is right for nearly everyone — Amazon tells us the host on sign-in —
        but it depends on that host being resolvable, and some networks cannot
        resolve the regional one (e.g. alexa.amazon.fr behind a DNS filter). This
        is the manual way out: pick a marketplace that does work.
        """
        if site and site not in AMAZON_SITES:
            raise ValueError(f"Unknown Amazon site: {site}")
        await self.homey.settings.set("amazon_site", site)
        self.alexa.pinned_site = site or None
        self.log(f"Amazon server set to {site or 'Auto'}")

        # Apply straight away when we have a session to rebuild; otherwise the
        # choice simply takes effect at the next sign-in.
        login_data = self.homey.settings.get("login_data")
        email = self.homey.settings.get("email")
        if login_data and email:
            try:
                await self.alexa.start_from_stored(email, login_data)
            except Exception as e:  # noqa: BLE001 - surfaced via /status
                self.error(f"Reconnect after server change failed: {type(e).__name__}: {e}")
                await self._report_error(e)
        return {"site": site}

    async def set_debug_logging(self, enabled: bool) -> None:
        await self.homey.settings.set("debug_logging", enabled)
        self._diagnostics.apply(enabled)

    def reset_pairing_reconnect(self) -> None:
        self._pairing_reconnect_done = False

    async def ensure_amazon_connected(self) -> bool:
        """Used by pairing: reuse live session or reconnect once from stored login_data."""
        alexa = self.alexa
        if alexa._api is not None and alexa.state in ("connected", "reconnecting"):
            return True
        if alexa.state == "connecting":
            for _ in range(30):
                await asyncio.sleep(0.5)
                if alexa.state in ("connected", "reconnecting"):
                    return True
                if alexa.state == "error":
                    return False
            return False

        login_data = self.homey.settings.get("login_data")
        email = self.homey.settings.get("email")
        if not (login_data and email):
            return False

        async with self._pairing_reconnect_lock:
            if alexa._api is not None and alexa.state in ("connected", "reconnecting"):
                return True
            if self._pairing_reconnect_done:
                return alexa._api is not None and alexa.state == "connected"

            self._pairing_reconnect_done = True
            try:
                self.log("Pairing: reconnecting from stored session …")
                # start_from_stored tears down any stale session and rebuilds
                # atomically under the connection lock — no separate stop() that
                # could race with auto-connect.
                await alexa.start_from_stored(email, login_data)
                return alexa.state == "connected"
            except Exception as e:  # noqa: BLE001
                self.error(f"Pairing reconnect failed: {type(e).__name__}: {e}")
                return False

    # --- internals -------------------------------------------------------
    async def _persist_login_data(self, login_data: dict) -> None:
        await self.homey.settings.set("login_data", login_data)

    async def _sync(self) -> None:
        try:
            await self.alexa.sync()
            await self._refresh_screen_state()
        except Exception as e:  # noqa: BLE001
            await self._report_error(e)

    async def _refresh_screen_state(self) -> None:
        """Poll display settings for screen devices — Amazon pushes none.

        Runs after connect and on every heartbeat. Only devices that advertise a
        screen do any work, and each swallows its own errors.
        """
        if self.alexa.state != "connected":
            return
        try:
            driver = self.homey.drivers.get_driver("echo")
        except Exception:  # noqa: BLE001 - driver not initialized yet
            return
        for device in driver.get_devices():
            await device.refresh_screen_state()

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

    async def _on_dnd(self, statuses: dict[str, bool]) -> None:
        # Amazon reports DND per physical device only — speaker groups never
        # appear in the list, so this walks the echo driver rather than going
        # through _fanout (which would also hit group/cluster members).
        try:
            driver = self.homey.drivers.get_driver("echo")
        except Exception:  # noqa: BLE001 - driver not initialized yet
            return
        for device in driver.get_devices():
            enabled = statuses.get(device.get_data()["id"])
            if enabled is not None:
                await device.apply_dnd(enabled)

    async def _on_state_change(self, state: str, reason: Optional[str]) -> None:
        if state in ("connecting", "reconnecting"):
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
        # Drop the stale session so the settings page prompts for a fresh login,
        # and fully tear down the service so the periodic sync doesn't keep
        # trying to revive a session Amazon has already rejected.
        await self.homey.settings.unset("login_data")
        await self.alexa.stop()

    # --- shopping / to-do lists ------------------------------------------
    async def _autocomplete_list(self, query: str, **kwargs) -> list[dict]:
        q = (query or "").lower()
        try:
            lists = await self.alexa.list_lists()
        except Exception as e:  # noqa: BLE001 - Homey can only show a list
            # Otherwise a failure is indistinguishable from "you have no lists":
            # Homey renders an empty picker either way.
            self.error(f"List autocomplete failed: {type(e).__name__}: {e}")
            return []
        return [
            {"name": item["name"], "data": {"id": item["id"]}}
            for item in lists
            if q in item["name"].lower()
        ]

    async def _get_list_items(self, args, **kwargs) -> dict:
        """Hand the whole list to the Flow and let it do the parsing.

        `items` is JSON so HomeyScript can work with it, including each item's id,
        which is what the remove and tick-off cards take. `names` is the un-ticked
        items as plain text, so announcing a shopping list needs no scripting at
        all.
        """
        list_id = args["list"]["data"]["id"]
        items = await self.alexa.list_items(list_id)
        open_names = [i["name"] for i in items if i["status"] != "COMPLETE"]
        return {
            "items": json.dumps(items, ensure_ascii=False),
            "names": ", ".join(open_names),
            "count": len(items),
            # Echoed back so the remove/tick-off cards can be fed from this one
            # without the Flow having to know the id another way.
            "list_id": list_id,
        }

    async def _remove_list_item(self, args, **kwargs) -> None:
        await self.alexa.remove_list_item(args["list"], args["item"])

    async def _complete_list_item(self, args, **kwargs) -> None:
        await self.alexa.complete_list_item(args["list"], args["item"])

    async def _on_list_item(self, info: dict) -> None:
        """Fire for any Alexa list; the list name is a token so Flows can filter.

        No per-list argument on the card: this SDK's FlowCardTrigger.trigger()
        accepts tokens only (`takes from 1 to 2 positional arguments`), so there
        is no state for a run listener to compare an argument against. Users
        narrow it down with a Logic card on the `list` token instead.

        `info` is passed straight through: AlexaService builds it with exactly the
        keys the card declares as tokens, so there is nothing to keep in sync
        here. Re-listing them by hand went wrong twice, and the SDK's diagnostic
        does not help — a *missing* token is reported as `Invalid value for token
        <name>. Expected <type> but got <class 'str'>`, because it prints
        `type(token_name)` instead of the value's type. "got str" means absent,
        not mistyped.
        """
        await self._list_card.trigger(info)

    def _configured_alexa_host(self) -> str:
        """The Alexa host this app would actually talk to, connected or not.

        In order: the live session's host, the pinned server, the server stored
        with the last login, then Amazon's default. The stored one matters — after
        a failed sign-in there is no session and no pin, and defaulting to
        amazon.com would probe a different marketplace than the one that broke.

        The probe also resolves the retail sibling of whatever this returns
        (alexa.amazon.fr → www.amazon.fr), which is where sign-in goes, so one run
        covers both paths without having to guess which of them failed.
        """
        if self.alexa.alexa_host:
            return self.alexa.alexa_host
        site = self.homey.settings.get("amazon_site")
        if not site:
            stored = self.homey.settings.get("login_data") or {}
            # Stored as "https://www.amazon.fr"; we want the bare domain.
            site = str(stored.get("site", "")).removeprefix("https://www.")
        return f"alexa.{site or 'amazon.com'}"

    def _maybe_probe_dns(self, e: Exception) -> None:
        """Fire the DNS diagnostic in the background after a resolution failure.

        Opt-in and throttled. It exists because every DNS test in the support
        thread so far either ran on a different machine or took a different path
        than the failing one, so nothing has ever compared two paths on the same
        machine at the same moment. See lib/dnsprobe.py.

        Backgrounded so a heartbeat is never held up by it, and the last chain it
        learned is persisted so a run where DoH is blocked still has real names
        to walk.
        """
        host = unresolved_host(e)
        if host is None or not self.homey.settings.get("debug_logging"):
            return
        if time.monotonic() - self._last_dns_probe_ts < DNS_PROBE_INTERVAL_S:
            return
        self._start_dns_probe(host)

    async def probe_dns(self, host: Optional[str] = None) -> dict:
        """Run the DNS probe on demand, from the settings page or a support request.

        Unlike the automatic path this ignores both the debug-logging gate and the
        throttle, because it was asked for explicitly. It still stamps the throttle
        so an automatic probe does not immediately repeat what we just measured.

        Returns as soon as the probe is running: the report goes to the app log,
        which is what a diagnostic report carries, and a Homey web-api call times
        out at 10 s while the probe can take longer.
        """
        host = host or self._configured_alexa_host()
        self._start_dns_probe(host)
        return {"started": True, "host": host}

    def _start_dns_probe(self, host: str) -> None:
        self._last_dns_probe_ts = time.monotonic()

        async def probe() -> None:
            chain = await dnsprobe.run(host, self.log, self.homey.settings.get("dns_chain"))
            if chain:
                await self.homey.settings.set("dns_chain", chain)

        # Hold a reference: a bare create_task can be garbage collected mid-run.
        self._dns_probe_task = asyncio.create_task(probe())

    async def _report_error(self, e: Exception) -> None:
        info = categorize_error(e)
        if info["needs_reauth"]:
            self.log(f"[{info['category']}] {info['message']} — attempting recovery …")
            if await self.alexa.try_recover_session():
                self.log("Session recovered after error")
                return

        self.error(f"[{info['category']}] {info['message']}")
        self._maybe_probe_dns(e)
        if info["category"] != "transient":
            await self.homey.flow.get_trigger_card("error").trigger({"error": info["message"]})
        if info["needs_reauth"]:
            await self._on_reauth()


homey_export = App
