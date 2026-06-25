"""Wraps aioamazondevices and adapts it to the app's needs.

All Amazon communication goes through this service. It owns the aiohttp session
(for the REST API) and the httpx client (for the HTTP/2 push channel), performs
login (stored or interactive), subscribes to the library's volume/media push
signals, and exposes simple command methods the drivers/devices call.

Volume scale: Homey uses 0-1, the Alexa API uses 0-100.
"""

import asyncio
import socket
import ssl
import time
from http import HTTPMethod
from typing import Any, Awaitable, Callable, Optional
from xml.sax.saxutils import escape as escape_xml

import aiohttp
import certifi
import httpx
from yarl import URL
from aioamazondevices.api import AmazonEchoApi
from aioamazondevices.const.http import REFRESH_ACCESS_TOKEN, REFRESH_AUTH_COOKIES
from aioamazondevices.const.sounds import SOUNDS_LIST
from aioamazondevices.structures import AmazonDevice, AmazonMediaControls

from .constants import DEVICES, VOICES

VOLUME_DIVISOR = 100

SINGLE_FAMILIES = {"ECHO", "KNIGHT", "ROOK"}
GROUP_FAMILY = "WHA"

# Soft-recovery throttle. A genuine auth failure (CannotAuthenticate) makes the
# library refresh the access token and *still* get rejected, so an unbounded
# "refresh + restart push" loop would hammer Amazon's auth endpoint without ever
# succeeding. Allow a few quick attempts, then fall through to a real re-auth.
# Attempts spaced further apart than the reset window count as independent
# incidents (the connection clearly recovered in between).
RECOVERY_MAX_ATTEMPTS = 3
RECOVERY_RESET_WINDOW_S = 240

# Website/session cookies expire after ~24h; renewing them clears the whole
# aiohttp cookie jar, so do it on a slow cadence rather than every heartbeat.
COOKIE_REFRESH_INTERVAL_S = 6 * 60 * 60

_PLAYBACK = {
    "play": AmazonMediaControls.Play,
    "pause": AmazonMediaControls.Pause,
    "next": AmazonMediaControls.Next,
    "previous": AmazonMediaControls.Previous,
}


class AlexaService:
    def __init__(self, log: Callable[[str], None]):
        self._log = log
        self._session: Optional[aiohttp.ClientSession] = None
        self._httpx: Optional[httpx.AsyncClient] = None
        self._api: Optional[AmazonEchoApi] = None
        self._devices: dict[str, AmazonDevice] = {}
        self._push_task: Optional[asyncio.Task] = None
        # Single mutex for everything that builds/tears down the session or
        # restarts the push channel (connect, recover, stop). Prevents two
        # triggers — auto-connect, pairing reconnect, recovery — from creating
        # parallel sessions and leaking orphaned push channels.
        self._connect_lock = asyncio.Lock()
        self._recovery_attempts = 0
        self._last_recovery_ts = 0.0
        self._last_cookie_refresh_ts = 0.0
        self._recover_tasks: set[asyncio.Task] = set()
        self.state = "disconnected"
        self.last_error: Optional[str] = None

        # callbacks wired by app.py
        self.on_state_change: Optional[Callable[[str, Optional[str]], Awaitable[None]]] = None
        self.on_volume: Optional[Callable[[str, float], Awaitable[None]]] = None
        self.on_media: Optional[Callable[[str, Any], Awaitable[None]]] = None
        self.on_reauth: Optional[Callable[[], Awaitable[None]]] = None
        self.on_login_data: Optional[Callable[[dict], Awaitable[None]]] = None

    @property
    def devices(self) -> dict[str, AmazonDevice]:
        return self._devices

    @property
    def push_is_alive(self) -> bool:
        return self._push_task is not None and not self._push_task.done()

    # --- lifecycle -------------------------------------------------------
    async def start_from_stored(self, email: str, login_data: dict) -> None:
        async with self._connect_lock:
            # Another trigger may have connected while we waited for the lock.
            if self._api is not None and self.state == "connected":
                return
            if self._api is not None:
                await self._teardown()
            await self._set_state("connecting")
            try:
                self._build(email, "", login_data)
                await self._api.login.login_mode_stored_data()
                await self._after_login()
            except Exception:
                # Don't leave the state stuck on "connecting" — the caller
                # (auto-connect / pairing) decides how to surface the failure.
                await self._set_state("disconnected")
                raise

    async def start_interactive(self, email: str, password: str, otp: str) -> dict:
        async with self._connect_lock:
            await self._set_state("connecting")
            try:
                if self._api is not None:
                    await self._teardown()
                self._build(email, password, None)
                self._log("login: submitting credentials + OTP to Amazon …")
                login_data = await self._api.login.login_mode_interactive(otp)
                self._log("login: device registered; setting up push + fetching devices …")
                await self._after_login()
                self._log("login: complete")
                return login_data
            except Exception as e:
                await self._set_state("error", f"{type(e).__name__}: {e}")
                raise

    def _build(self, email: str, password: str, login_data: Optional[dict]) -> None:
        # Fresh session — let recovery have its full budget of attempts again.
        self._recovery_attempts = 0
        self._last_recovery_ts = 0.0
        # Homey has no IPv6 route (force IPv4) and no system CA store (use certifi's bundle).
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        self._session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(family=socket.AF_INET, ssl=ssl_context),
            timeout=aiohttp.ClientTimeout(total=30),
        )
        self._api = AmazonEchoApi(
            client_session=self._session,
            login_email=email,
            login_password=password,
            login_data=login_data,
            save_to_file=self._save_to_file,
        )

    async def _after_login(self) -> None:
        await self.refresh_devices()
        self._api.on_volume_state_event.append(self._handle_volume)
        self._api.on_volume_state_event.freeze()
        self._api.on_media_state_event.append(self._handle_media)
        self._api.on_media_state_event.freeze()
        self._log("login: devices fetched; opening HTTP/2 push channel …")
        if self._httpx is None or self._httpx.is_closed:
            # local_address forces IPv4 (Homey has no IPv6 route).
            self._httpx = httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(http2=True, local_address="0.0.0.0"),
                timeout=None,
            )
        await self._start_push_channel()
        await self._set_state("connected")

    async def _start_push_channel(self) -> None:
        self._push_task = await self._api.start_http2_processing(
            self._httpx, on_reauth_required=self._handle_reauth
        )
        self._watch_push_task()

    async def stop(self) -> None:
        async with self._connect_lock:
            await self._teardown()
            await self._set_state("disconnected")

    async def _teardown(self) -> None:
        """Tear down the session and push channel. Caller must hold _connect_lock."""
        try:
            if self._api is not None:
                await self._api.stop_http2_processing()
        finally:
            self._push_task = None
            if self._httpx is not None:
                await self._httpx.aclose()
                self._httpx = None
            if self._session is not None:
                await self._session.close()
                self._session = None
            self._api = None
            self._devices = {}

    # --- session maintenance ---------------------------------------------
    async def refresh_session(self, refresh_cookies: bool = True) -> bool:
        """Refresh the access token (always) and website cookies (optional).

        Returns True if the access token was renewed. Cookie renewal clears the
        cookie jar, so callers can skip it (see COOKIE_REFRESH_INTERVAL_S).
        """
        if self._api is None:
            return False

        wrapper = self._api._http_wrapper
        ok, _ = await wrapper.refresh_data(REFRESH_ACCESS_TOKEN)
        if not ok:
            self._log("session refresh: access token refresh failed")
            return False

        if refresh_cookies:
            if await self._refresh_website_cookies():
                self._last_cookie_refresh_ts = time.monotonic()
            else:
                self._log("session refresh: website cookie refresh failed (continuing)")

        await self._persist_login_data()
        return True

    async def _refresh_website_cookies(self) -> bool:
        """Renew website/session cookies — these often expire after ~24 hours.

        Mirrors aioamazondevices' private AmazonLogin._refresh_auth_cookies (no
        public equivalent), but guards on the refresh result before clearing the
        jar. Pinned to aioamazondevices==14.1.3 — re-check on library bumps.
        """
        wrapper = self._api._http_wrapper
        ss = self._api._session_state_data
        ok, json_token_resp = await wrapper.refresh_data(REFRESH_AUTH_COOKIES)
        if not ok:
            return False

        website_cookies = ss.login_stored_data["website_cookies"] = {}
        await wrapper.clear_cookies()
        cookie_json = json_token_resp["response"]["tokens"]["cookies"]
        for cookie_domain in cookie_json:
            for cookie in cookie_json[cookie_domain]:
                new_cookie_value = cookie["Value"].replace(r'"', r"")
                new_cookie = {cookie["Name"]: new_cookie_value}
                await wrapper.set_cookies(new_cookie, URL(cookie_domain))
                website_cookies.update(new_cookie)
                if cookie["Name"] == "session-token":
                    ss.login_stored_data["store_authentication_cookie"] = {
                        "cookie": new_cookie_value
                    }
        return True

    async def _persist_login_data(self) -> None:
        if self._api is not None and self.on_login_data is not None:
            await self.on_login_data(self._api._session_state_data.login_stored_data)

    async def try_recover_session(self) -> bool:
        """Attempt soft recovery: refresh tokens, restart HTTP/2 push.

        Bounded: after RECOVERY_MAX_ATTEMPTS attempts within RECOVERY_RESET_WINDOW_S
        it gives up and returns False so the caller can fall through to a real
        re-auth. Without this bound a permanently-rejected token loops forever
        (the library refreshes the token on every reconnect and still gets 403),
        bypassing its own exponential backoff.
        """
        if self._api is None:
            return False

        async with self._connect_lock:
            if self._api is None:
                return False

            now = time.monotonic()
            if now - self._last_recovery_ts > RECOVERY_RESET_WINDOW_S:
                # Spaced-out incident → the connection recovered in between.
                self._recovery_attempts = 0
            self._last_recovery_ts = now
            if self._recovery_attempts >= RECOVERY_MAX_ATTEMPTS:
                self._log(
                    f"session recovery: giving up after {self._recovery_attempts} "
                    "attempts without a stable connection"
                )
                return False
            self._recovery_attempts += 1

            await self._set_state("reconnecting")
            try:
                self._log("session recovery: refreshing tokens …")
                if not await self.refresh_session():
                    return False

                await self._api.login.login_mode_stored_data()

                if not self.push_is_alive:
                    self._log("session recovery: restarting HTTP/2 push channel …")
                    await self._api.stop_http2_processing()
                    await self._start_push_channel()

                await self._set_state("connected")
                self._log("session recovery: success")
                return True
            except Exception as e:  # noqa: BLE001
                self._log(f"session recovery failed: {type(e).__name__}: {e}")
                return False

    async def ensure_push_channel(self) -> None:
        """Restart the push channel if the background task has stopped."""
        if self._api is None or self.push_is_alive:
            return
        async with self._connect_lock:
            # Re-check under the lock: recovery may have restarted it meanwhile.
            if self._api is None or self.push_is_alive:
                return
            self._log("push channel dead — restarting …")
            await self._api.stop_http2_processing()
            await self._start_push_channel()
            if self.state != "connected":
                await self._set_state("connected")

    # --- persistence (library pushes refreshed login_data here) ----------
    async def _save_to_file(self, raw_data, url: str = "login_data", content_type: str = "application/json") -> None:
        if isinstance(raw_data, dict) and url == "login_data" and self.on_login_data is not None:
            await self.on_login_data(raw_data)

    # --- data ------------------------------------------------------------
    async def refresh_devices(self) -> dict[str, AmazonDevice]:
        self._devices = await self._api.get_devices_data()
        return self._devices

    async def sync(self) -> None:
        """Heartbeat: refresh session, sync state, keep push channel alive."""
        if self._api is None:
            return
        # Refresh the access token every heartbeat (cheap), but only renew the
        # website cookies a few times a day — renewing clears the cookie jar.
        refresh_cookies = (
            time.monotonic() - self._last_cookie_refresh_ts >= COOKIE_REFRESH_INTERVAL_S
        )
        async with self._connect_lock:
            if self._api is None:
                return
            await self.refresh_session(refresh_cookies=refresh_cookies)
        await self._api.login.login_mode_stored_data()
        await self._api.sync_media_state()
        await self.ensure_push_channel()

    # --- push handlers (library -> app) ----------------------------------
    async def _handle_volume(self, payload: dict[str, Any]) -> None:
        if self.on_volume is None:
            return
        for serial, vol in payload.items():
            if vol is not None and vol.volume is not None:
                await self.on_volume(serial, vol.volume / VOLUME_DIVISOR)

    async def _handle_media(self, payload: dict[str, Any]) -> None:
        if self.on_media is None:
            return
        for serial, media in payload.items():
            await self.on_media(serial, media)

    async def _handle_reauth(self) -> None:
        # The library calls this from *inside* the push task, right before that
        # task exits on an auth failure — so we can't restart the channel here
        # (we'd be cancelling our own task). Mark reconnecting and let the
        # push-exit watchdog drive the (bounded) recovery once the task is gone.
        self._log("HTTP/2 auth failure — deferring recovery to push-exit watchdog")
        await self._set_state("reconnecting")

    def _watch_push_task(self) -> None:
        if self._push_task is None:
            return

        def _on_done(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            if task.exception() is not None:
                self._log(f"HTTP/2 push task ended with error: {task.exception()}")
            elif self._api is not None and self.state in ("connected", "reconnecting"):
                self._log("HTTP/2 push task ended unexpectedly — scheduling recovery")
            else:
                return
            # Keep a reference so the task isn't GC'd mid-flight (asyncio only
            # holds a weak reference to bare create_task() results).
            recover = asyncio.create_task(self._recover_after_push_exit())
            self._recover_tasks.add(recover)
            recover.add_done_callback(self._recover_tasks.discard)

        self._push_task.add_done_callback(_on_done)

    async def _recover_after_push_exit(self) -> None:
        if await self.try_recover_session():
            return
        if self._api is None:
            # Session already torn down elsewhere (e.g. user disconnect) — there's
            # nothing to recover and no reason to clear stored credentials.
            return
        # Recovery exhausted/failed → terminal. on_reauth tears the session down
        # (App._on_reauth → alexa.stop) so a dead session isn't revived by sync.
        await self._set_state(
            "disconnected", "Connection lost — please re-authenticate in app settings"
        )
        if self.on_reauth is not None:
            await self.on_reauth()

    async def _set_state(self, state: str, reason: Optional[str] = None) -> None:
        self.state = state
        if state == "connected":
            self.last_error = None
        elif state == "error" and reason:
            self.last_error = reason
        if self.on_state_change is not None:
            await self.on_state_change(state, reason)

    # --- commands (app/device -> library) --------------------------------
    def _device(self, serial: str) -> AmazonDevice:
        if self._api is not None:
            live = self._api._device_handler.devices.get(serial)
            if live is not None:
                return live
        return self._devices[serial]

    async def say(self, serial: str, message: str, mode: str = "speak") -> None:
        device = self._device(serial)
        if mode == "announce":
            await self._api.call_alexa_announcement(device, message)
        elif mode == "whisper":
            ssml = f'<speak><amazon:effect name="whispered">{escape_xml(message)}</amazon:effect></speak>'
            await self._api.call_alexa_speak(device, ssml)
        else:
            await self._api.call_alexa_speak(device, message)

    async def say_with_voice(self, serial: str, message: str, voice_id: str, mode: str = "speak") -> None:
        # voice_id is "<PollyVoice>:<lang>" (e.g. "Hans:de-DE"). Rendered via SSML.
        voice, _, lang = voice_id.partition(":")
        content = escape_xml(message)
        if mode == "whisper":
            content = f'<amazon:effect name="whispered">{content}</amazon:effect>'
        ssml = f'<speak><voice name="{voice}"><lang xml:lang="{lang}">{content}</lang></voice></speak>'
        await self._api.call_alexa_speak(self._device(serial), ssml)

    def list_voices(self, query: str = "") -> list[dict]:
        q = (query or "").lower()
        voices = [{"id": f"{v['id']}:{v['lang']}", "name": v["name"]} for v in VOICES]
        return sorted(
            (v for v in voices if q in v["name"].lower()),
            key=lambda v: v["name"],
        )

    async def execute_command(self, serial: str, text: str) -> None:
        await self._api.call_alexa_text_command(self._device(serial), text)

    async def play_sound(self, serial: str, sound_id: str) -> None:
        await self._api.call_alexa_sound(self._device(serial), sound_id)

    async def run_routine(self, routine_name: str) -> None:
        # call_routine looks routines up by name in a cache only populated by the
        # autocomplete; refresh it so the flow works even after an app restart.
        await self._api.update_routines()
        await self._api.call_routine(routine_name)

    async def set_volume(self, serial: str, value: float) -> None:
        await self._api.set_device_volume(self._device(serial), round(value * VOLUME_DIVISOR))

    async def playback(self, serial: str, action: str) -> None:
        device = self._device(serial)
        # WORKAROUND (aioamazondevices==14.1.3): send_media_command builds the np/command
        # URL without a "/" separator (https://alexa.amazon.<domain>api/np/command) →
        # CannotConnect. Already fixed on upstream main (uses URL.joinpath) but unreleased;
        # drop this and use api.send_media_command once a release > 14.1.3 ships the fix.
        # Issue the request ourselves with a correctly-joined URL via the lib's session.
        ss = self._api._session_state_data
        url = URL.joinpath(ss.alexa_website_url, "api/np/command").with_query(
            deviceSerialNumber=device.serial_number,
            deviceType=device.device_type,
        )
        await self._api._http_wrapper.session_request(
            method=HTTPMethod.POST,
            url=url,
            input_data={"type": _PLAYBACK[action].value},
            json_data=True,
        )

    # --- lookups for flow autocomplete -----------------------------------
    def list_sounds(self) -> list[dict]:
        return sorted(
            ({"id": sound_id, "name": name} for sound_id, name in SOUNDS_LIST.items()),
            key=lambda s: s["name"],
        )

    async def list_routines(self) -> list[str]:
        await self._api.update_routines()
        return sorted(self._api.routines)

    # --- pairing ---------------------------------------------------------
    async def pairing_devices(self, kind: str) -> list[dict]:
        if self._api is None:
            return []
        devices = await self.refresh_devices()
        out: list[dict] = []
        for device in devices.values():
            is_group = device.device_family == GROUP_FAMILY
            if kind == "group" and not is_group:
                continue
            if kind == "echo" and (is_group or device.device_family not in SINGLE_FAMILIES):
                continue

            entry: dict = {
                "name": device.account_name,
                "data": {"id": device.serial_number},
                "store": {
                    "capabilities": list(device.capabilities),
                    "model": {"id": device.device_type},
                },
            }
            if kind == "echo":
                meta = DEVICES.get(device.device_type)
                if meta and meta.get("name") and meta.get("generation"):
                    name = str(meta["name"]).replace(" ", "")
                    entry["icon"] = f"icon-{name}-Gen{meta['generation']}.svg"
            out.append(entry)
        return out