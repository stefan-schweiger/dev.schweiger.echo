"""Wraps aioamazondevices and adapts it to the app's needs.

All Amazon communication goes through this service. It owns the aiohttp session
(for the REST API) and the httpx client (for the HTTP/2 push channel), performs
login (stored or interactive), subscribes to the library's volume/media push
signals, and exposes simple command methods the drivers/devices call.

Volume scale: Homey uses 0-1, the Alexa API uses 0-100.
"""

import asyncio
import functools
import json
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
from aioamazondevices.exceptions import CannotAuthenticate
from aioamazondevices.const.http import (
    AMAZON_DEVICE_TYPE,
    ARRAY_WRAPPER,
    DEFAULT_SITE,
    REFRESH_ACCESS_TOKEN,
    REFRESH_AUTH_COOKIES,
    URI_BEHAVIORS_AUTOMATIONS,
    URI_DEVICES,
    URI_REGISTER,
)
from aioamazondevices.const.sounds import SOUNDS_LIST
from aioamazondevices.implementation import http2 as amazon_http2
from aioamazondevices.structures import AmazonDevice, AmazonMediaControls

from .connection import unresolved_host_message
from .constants import DEVICES, VOICES

VOLUME_DIVISOR = 100

SINGLE_FAMILIES = {"ECHO", "KNIGHT", "ROOK"}
GROUP_FAMILY = "WHA"

# Per-device settings (screen power, brightness, …). Not exposed by
# aioamazondevices; keyed on deviceAccountId, not the serial number.
URI_DEVICE_SETTINGS = "api/v1/devices/{account_id}/settings/{name}"

# Amazon *does* push Do Not Disturb changes over the HTTP/2 channel, but
# aioamazondevices doesn't know the message type yet and drops it as unknown
# before any subscriber sees it. See _allow_dnd_push_events.
PUSH_DND_STATE_CHANGE = "PUSH_DND_STATE_CHANGE"

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

# How long a resolved Amazon address stays usable before we ask DNS again.
# aiohttp defaults to 10s, which turns one login (five different hostnames) or
# one visit to the routine picker into a burst of lookups. Some home networks
# answer the first few and then stop: a French tester's on-device test resolved
# alexa.amazon.fr four times in a row and then got "no address associated with
# hostname" for every attempt after that, and his connect log failed on the
# fifth name it looked up. The answer is a single CloudFront address with a 60s
# TTL, so every minute something in the path has to fetch a new one and an empty
# refresh leaves us with nothing. Deliberately outliving that TTL: it exists for
# load balancing, the address stays reachable far longer, and a withdrawn one
# surfaces as a connect failure that the reconnect backoff already handles.
DNS_CACHE_TTL_S = 120

# Throttles for the routine diagnostics (see list_routines / _probe_routines).
ROUTINES_LOG_INTERVAL_S = 60
ROUTINES_PROBE_INTERVAL_S = 5 * 60

# Amazon intermittently answers the email+password POST with a captcha /
# interstitial / rate-limit page instead of the OTP form; the library surfaces
# that as CannotAuthenticate with this exact message. It's transient, so we retry
# the interactive login once (see start_interactive). Genuine bad credentials
# raise a *different* message and must not be retried.
OTP_PAGE_MISSING = "MFA OTP code not found on login page"

# Amazon hands out the account id in two forms and they are NOT interchangeable:
#   - "amzn1.account.AGP2HK…"  obfuscated id, returned by /auth/register
#   - "A146V8AS9QOCRT"         directed id, carried by the device list as
#                              deviceOwnerCustomerId
# Only the directed id works in a behaviours payload. Post a sequence with the
# obfuscated one and Amazon answers 400 Bad Request, so login looks perfect and
# then nothing ever speaks. See _seed_customer_id_from_register.
OBFUSCATED_CUSTOMER_ID_PREFIX = "amzn1.account."

# The library reads Amazon's post-login redirect with a raw dict lookup
# (AmazonLogin._extract_code_from_url), so anything other than the expected
# "here is your authorization code" redirect surfaces as a bare KeyError naming
# the missing query parameter — which is what the user was shown.
AUTH_CODE_PARAM = "openid.oa2.authorization_code"

_PLAYBACK = {
    "play": AmazonMediaControls.Play,
    "pause": AmazonMediaControls.Pause,
    "next": AmazonMediaControls.Next,
    "previous": AmazonMediaControls.Previous,
}


def _allow_dnd_push_events() -> None:
    """Stop aioamazondevices discarding Amazon's DND push messages.

    Amazon pushes `PUSH_DND_STATE_CHANGE` over the AVS directive stream when a
    device's Do Not Disturb flips — from the Alexa app, by voice, or from a
    routine. The library filters every message whose type isn't in its
    AmazonPushMessage enum, logging "Unknown HTTP2 push message", so the event
    is dropped inside _process_rendering_update before any subscriber runs.
    Widening that predicate is the whole fix; the payload shape is already what
    the rest of the pipeline expects.

    Idempotent, and only ever *adds* an accepted type. If a library bump renames
    or inlines the predicate this becomes a no-op and DND simply falls back to
    the sync_dnd() heartbeat poll, which is kept for exactly that reason.
    Pinned to aioamazondevices==14.2.2 — re-check on library bumps.

    DELETE ME once https://github.com/chemelli74/aioamazondevices/pull/1010
    ships — it adds AmazonPushMessage.DoNotDisturbChange (same event value) plus
    an on_dnd_event Signal to subscribe to instead. See "Pending upstream" in
    AGENTS.md for the swap.
    """
    is_known_event_type = getattr(amazon_http2, "_is_known_event_type", None)
    if is_known_event_type is None or getattr(is_known_event_type, "_dnd_allowed", False):
        return

    def patched(push_event_type: str) -> bool:
        return push_event_type == PUSH_DND_STATE_CHANGE or is_known_event_type(
            push_event_type
        )

    patched._dnd_allowed = True
    amazon_http2._is_known_event_type = patched


def is_directed_customer_id(value: Optional[str]) -> bool:
    """True for an id the behaviours API accepts (see the prefix constant)."""
    return bool(value) and not value.startswith(OBFUSCATED_CUSTOMER_ID_PREFIX)


def login_error_message(e: BaseException) -> str:
    """User-facing text for a failed interactive login.

    Amazon answers a rejected sign-in by simply not returning an authorization
    code — a wrong or expired 2-step code, a captcha, or an extra verification
    step all look like that — and the library turns it into
    `KeyError: 'openid.oa2.authorization_code'`. Shown verbatim in app settings,
    that reads like a crash, so users retry the same code over and over (one
    report had five attempts in four minutes). Say what to do instead.

    A sign-in that never resolved a hostname is the other one worth naming: it
    surfaces as `CannotConnect: Connection error during GET`, which two testers
    reported verbatim while hunting a problem that was never in the app.
    """
    if isinstance(e, KeyError) and AUTH_CODE_PARAM in str(e):
        return (
            "Amazon did not complete the sign-in. Check the 2-step verification code "
            "and try again — if it keeps failing, open the Amazon app or website once "
            "to clear any extra verification step."
        )
    return unresolved_host_message(e) or f"{type(e).__name__}: {e}"


def heal_stale_session(method):
    """Refresh the session once and retry a command Amazon refused to authenticate.

    The website cookies are renewed in the background every
    COOKIE_REFRESH_INTERVAL_S without the user doing anything, and a session
    Amazon stops accepting afterwards fails in a way that hides itself: reads and
    the push channel carry on, so the app still shows connected, while every
    command comes back 401/403. A user reported exactly that shape — "works for
    12 hours, then I lose control without disconnecting" — and had to restart the
    app to get it back. Do that restart for him.

    Deliberately narrow. Only CannotAuthenticate, which the library raises for
    401/403/407 and nothing else; a 404 arrives as CannotRetrieveData and is a
    perfectly normal answer to, say, a playback command sent to a device with
    nothing playing, so recovering on that would tear down and rebuild the push
    channel every time someone pressed Next on an idle Echo.

    Retrying is safe: a request Amazon refused never reached the device, so the
    second attempt can't duplicate anything. try_recover_session() is itself
    bounded (RECOVERY_MAX_ATTEMPTS), so a session Amazon keeps refusing falls
    through to the original exception instead of looping.
    """

    @functools.wraps(method)
    async def run(self, *args, **kwargs):
        try:
            return await method(self, *args, **kwargs)
        except CannotAuthenticate as e:
            self._log(
                f"{method.__name__}: Amazon rejected the session ({e}) — "
                "refreshing it and retrying once"
            )
            if not await self.try_recover_session():
                raise
            return await method(self, *args, **kwargs)

    return run


class AlexaService:
    def __init__(self, log: Callable[[str], None]):
        self._log = log
        self._session: Optional[aiohttp.ClientSession] = None
        # Outlives the session on purpose — see _shared_connector.
        self._connector: Optional[aiohttp.TCPConnector] = None
        # None until first observed — see _note_csrf_state.
        self._csrf_present: Optional[bool] = None
        self._httpx: Optional[httpx.AsyncClient] = None
        self._api: Optional[AmazonEchoApi] = None
        self._devices: dict[str, AmazonDevice] = {}
        # serial -> deviceAccountId, skimmed off the raw device-list response
        # (see _harvest_device_account_ids).
        self._device_account_ids: dict[str, str] = {}
        self._push_task: Optional[asyncio.Task] = None
        # Single mutex for everything that builds/tears down the session or
        # restarts the push channel (connect, recover, stop). Prevents two
        # triggers — auto-connect, pairing reconnect, recovery — from creating
        # parallel sessions and leaking orphaned push channels.
        self._connect_lock = asyncio.Lock()
        self._recovery_attempts = 0
        self._last_recovery_ts = 0.0
        self._last_cookie_refresh_ts = 0.0
        # Homey re-runs a Flow autocomplete on every keystroke, so the routine
        # diagnostics de-duplicate themselves: log a repeated result at most
        # once a minute, and re-probe an empty list at most every 5 minutes.
        self._last_routines_log: tuple[str, float] = ("", 0.0)
        self._last_probe_ts = 0.0
        # Marketplace the user pinned in app settings ("amazon.fr"), or None for
        # "Auto" — see _apply_pinned_site / _heal_domain_pin.
        self.pinned_site: Optional[str] = None
        self._recover_tasks: set[asyncio.Task] = set()
        self.state = "disconnected"
        self.last_error: Optional[str] = None

        # callbacks wired by app.py
        self.on_state_change: Optional[Callable[[str, Optional[str]], Awaitable[None]]] = None
        self.on_volume: Optional[Callable[[str, float], Awaitable[None]]] = None
        self.on_media: Optional[Callable[[str, Any], Awaitable[None]]] = None
        self.on_reauth: Optional[Callable[[], Awaitable[None]]] = None
        self.on_login_data: Optional[Callable[[dict], Awaitable[None]]] = None
        self.on_dnd: Optional[Callable[[dict[str, bool]], Awaitable[None]]] = None

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
                if self.pinned_site:
                    # Start on the chosen host instead of connecting to the
                    # stored one and moving afterwards.
                    login_data = {**login_data, "site": self.site_url(self.pinned_site)}
                self._build(email, "", login_data)
                await self._api.login.login_mode_stored_data()
                await self._apply_pinned_site()
                await self._heal_domain_pin()
                await self._after_login()
            except Exception:
                # Don't leave the state stuck on "connecting" — the caller
                # (auto-connect / pairing) decides how to surface the failure.
                await self._set_state("disconnected")
                raise

    async def start_interactive(self, email: str, password: str, otp: str) -> dict:
        async with self._connect_lock:
            await self._set_state("connecting")
            # Phase timings help explain a slow sign-in in diagnostic reports:
            # Amazon's OAuth flow bakes in per-request 0/2/5s back-offs when it
            # throttles the Homey's IP. (The library's own account-id lookup no
            # longer contributes — registration seeds the id, so the guard in
            # _skip_known_customer_id_lookup skips it.)
            t0 = time.monotonic()
            try:
                self._log("login: submitting credentials + OTP to Amazon …")
                try:
                    login_data = await self._interactive_login_attempt(email, password, otp)
                except CannotAuthenticate as e:
                    if OTP_PAGE_MISSING not in str(e):
                        raise
                    # The OTP wasn't submitted yet (the page it belongs to never
                    # appeared), so the same code is still valid — retry once with
                    # a fresh session, which mints a new anti-captcha cookie.
                    self._log(
                        f"login: Amazon returned no OTP page after {time.monotonic() - t0:.1f}s "
                        "(likely a captcha or interstitial) — retrying once with a fresh session …"
                    )
                    login_data = await self._interactive_login_attempt(email, password, otp)
                self._log(
                    f"login: device registered after {time.monotonic() - t0:.1f}s; "
                    "setting up push + fetching devices …"
                )
                # Persist the session the moment it's registered — before the
                # device fetch / push setup, which can hit a transient Amazon 503
                # and would otherwise throw away a perfectly good login. With it
                # stored, auto-connect/sync finishes the job on the next cycle
                # instead of forcing the user to sign in again.
                await self._persist_login_data()
                await self._apply_pinned_site()
                await self._after_login()
                self._log(f"login: complete in {time.monotonic() - t0:.1f}s")
                return login_data
            except Exception as e:
                await self._set_state("error", login_error_message(e))
                raise

    async def _interactive_login_attempt(self, email: str, password: str, otp: str) -> dict:
        # Each attempt starts from a clean session so a retry doesn't inherit
        # cookies/state from the failed one.
        if self._api is not None:
            await self._teardown()
        self._build(email, password, None)
        login = self._api.login
        if not self.pinned_site:
            return await login.login_mode_interactive(otp)

        # With a server pinned, keep the whole sign-in on amazon.com: it is where
        # the OAuth flow starts anyway (its return_to is hardcoded there) and the
        # one host that resolves everywhere, and an EU account's device list is
        # served there too. Left alone, the library's own /api/welcome sniff
        # switches to the account's regional host halfway through, and the
        # customer-id lookup and device fetch that follow then die on it — which
        # is the very failure someone pins a server to escape, so the setting
        # would be useless to anyone not already signed in. The pin is applied
        # immediately afterwards by _apply_pinned_site().
        # Pinned to aioamazondevices==14.2.2 — re-check on library bumps.
        original = login._domain_refresh_auth_cookies

        async def keep_default_domain() -> None:
            self._log(f"login: staying on {DEFAULT_SITE}, server pinned to {self.pinned_site}")

        login._domain_refresh_auth_cookies = keep_default_domain
        try:
            return await login.login_mode_interactive(otp)
        finally:
            login._domain_refresh_auth_cookies = original

    def _shared_connector(self) -> aiohttp.TCPConnector:
        """One connector for the app's lifetime, so its DNS cache outlives a rebuild.

        Every login attempt and every recovery makes a fresh `ClientSession` to
        start from clean cookies, and the resolved-address cache lives on the
        connector, not the session — so rebuilding both meant a second sign-in
        attempt looked up all five hosts again, on the very networks where the
        fifth lookup is the one that comes back empty. Cookies live in the
        session's jar, so sharing this keeps the clean slate while making a retry
        cost one lookup instead of five. Recreated if it was closed by stop().
        """
        if self._connector is None or self._connector.closed:
            # Homey has no IPv6 route (force IPv4) and no system CA store (certifi).
            self._connector = aiohttp.TCPConnector(
                family=socket.AF_INET,
                ssl=ssl.create_default_context(cafile=certifi.where()),
                ttl_dns_cache=DNS_CACHE_TTL_S,
            )
        return self._connector

    def _build(self, email: str, password: str, login_data: Optional[dict]) -> None:
        # Fresh session — let recovery have its full budget of attempts again.
        self._recovery_attempts = 0
        self._last_recovery_ts = 0.0
        self._session = aiohttp.ClientSession(
            connector=self._shared_connector(),
            connector_owner=False,
            timeout=aiohttp.ClientTimeout(total=30),
        )
        self._api = AmazonEchoApi(
            client_session=self._session,
            login_email=email,
            login_password=password,
            login_data=login_data,
            save_to_file=self._save_to_file,
        )
        self._skip_known_customer_id_lookup()
        _allow_dnd_push_events()
        self._intercept_dnd_push_events()

    def _intercept_dnd_push_events(self) -> None:
        """Handle PUSH_DND_STATE_CHANGE ourselves, delegate everything else.

        The library's own handler is the sole subscriber to the push signal and
        is attached inside start_http2_processing(), so replacing it on the api
        instance here — before the channel opens — is what gets it subscribed.
        Paired with _allow_dnd_push_events(); without that the message never
        arrives and this simply never fires.

        DELETE ME together with _allow_dnd_push_events() once upstream PR #1010
        ships — see "Pending upstream" in AGENTS.md.
        """
        push_event_handler = self._api._http2_push_event_handler

        async def handler(event_type: str, payload: dict[str, Any]) -> None:
            if event_type == PUSH_DND_STATE_CHANGE:
                await self._handle_dnd_push(payload)
                return
            await push_event_handler(event_type, payload)

        self._api._http2_push_event_handler = handler

    async def _handle_dnd_push(self, payload: dict[str, Any]) -> None:
        serial = (payload.get("dopplerId") or {}).get("deviceSerialNumber")
        enabled = payload.get("enabled")
        if serial is None or enabled is None or self.on_dnd is None:
            self._log(f"ignoring malformed DND push payload: {payload}")
            return
        await self.on_dnd({serial: bool(enabled)})

    def _skip_known_customer_id_lookup(self) -> None:
        """Don't re-derive the account customer id once we already have it.

        obtain_account_customer_id() runs on *every* login — including the
        stored-data login the heartbeat performs every few minutes — and its
        loop has no early exit: it only returns once it spots the *just-
        registered* virtual device in the device list. On accounts where Amazon
        stopped returning that entry (the same bug _seed_customer_id_from_*
        works around) it never does, so it re-fetches the whole device list
        CUSTOMER_ACCOUNT_MAX_RETRIES times before falling through — 30 as of
        aioamazondevices 14.2.2, up from 3 in 14.1.9. By then the id is long
        since in hand, so skip the lookup entirely.

        Only ever skips work: with no id known the library's own logic runs
        untouched, so accounts that aren't affected behave exactly as upstream.
        Pinned to aioamazondevices==14.2.2 — re-check on library bumps.
        """
        login = self._api.login
        obtain_account_customer_id = login.obtain_account_customer_id

        async def guarded() -> None:
            ss = self._api._session_state_data
            if is_directed_customer_id(ss.account_customer_id):
                return
            # One plain device-list fetch is all it takes — the response passes
            # through _save_to_file, which lifts the directed id off the
            # account's own "This Device" entry. Only if that somehow comes up
            # empty do we fall back to the library's 30-attempt loop.
            await self._seed_customer_id_from_device_list()
            if is_directed_customer_id(ss.account_customer_id):
                return
            await obtain_account_customer_id()

        login.obtain_account_customer_id = guarded

    async def _after_login(self) -> None:
        await self.refresh_devices()
        self._log_account_context()
        self._api.on_volume_state_event.append(self._handle_volume)
        self._api.on_volume_state_event.freeze()
        self._api.on_media_state_event.append(self._handle_media)
        self._api.on_media_state_event.freeze()
        self._log("login: devices fetched; opening HTTP/2 push channel …")
        await self._start_push_channel()
        await self._set_state("connected")
        await self.sync_dnd()
        self._note_csrf_state("after login")

    def _push_client(self) -> httpx.AsyncClient:
        """The httpx client the AVS push channel runs on, created on demand.

        Owned here rather than at login on purpose. A login that fails partway —
        a DNS failure, a transient Amazon 503 — deliberately leaves `_api` in
        place so the heartbeat can retry without throwing away stored
        credentials, but it never reaches the end of _after_login. The heartbeat
        then calls ensure_push_channel(), which used to hand the library a None
        client; the library keeps that None and dereferences it on every
        reconnect attempt, so the channel is dead for good ("AttributeError:
        'NoneType' object has no attribute 'stream'", retried 5s→600s forever)
        and live volume/media/DND updates never come back until a restart.

        local_address forces IPv4 (Homey has no IPv6 route); timeout=None keeps
        the long-lived stream open, as start_http2_processing requires.
        """
        if self._httpx is None or self._httpx.is_closed:
            self._httpx = httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(http2=True, local_address="0.0.0.0"),
                timeout=None,
            )
        return self._httpx

    async def _start_push_channel(self) -> None:
        self._push_task = await self._api.start_http2_processing(
            self._push_client(), on_reauth_required=self._handle_reauth
        )
        self._watch_push_task()

    async def stop(self) -> None:
        async with self._connect_lock:
            await self._teardown()
            if self._connector is not None:
                # Only here: _teardown runs between login attempts, and the point
                # of the shared connector is to survive that.
                await self._connector.close()
                self._connector = None
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
            self._device_account_ids = {}
            # Next session gets a fresh baseline instead of inheriting this one's.
            self._csrf_present = None

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
        jar. Pinned to aioamazondevices==14.2.2 — re-check on library bumps.
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
                await wrapper.set_cookies(
                    new_cookie, URL.build(scheme="https", host=cookie_domain)
                )
                website_cookies.update(new_cookie)
                if cookie["Name"] == "session-token":
                    ss.login_stored_data["store_authentication_cookie"] = {
                        "cookie": new_cookie_value
                    }
        self._note_csrf_state("after cookie renewal")
        return True

    def _note_csrf_state(self, context: str) -> None:
        """Log whether the library still holds a CSRF token, but only on a change.

        The wrapper sends that token as a header on every request and Amazon
        checks it on writes. clear_cookies() — which the 6-hourly cookie renewal
        calls — also drops it, and it only comes back when some later response
        happens to set one. If that never happens, reads keep working and every
        command is refused, which is invisible from the outside.

        One line per transition is enough to see that in a diagnostic report
        (present at login, cleared at the renewal, and either re-acquired on the
        next heartbeat or not) without writing a line every five minutes forever.
        Reads a private attribute; pinned to aioamazondevices==14.2.2.
        """
        if self._api is None:
            return
        present = bool(getattr(self._api._http_wrapper, "_csrf_cookie", None))
        if present == self._csrf_present:
            return
        self._csrf_present = present
        self._log(f"CSRF token {'acquired' if present else 'cleared'} ({context})")

    @staticmethod
    def site_url(domain: str) -> str:
        """"amazon.fr" → "https://www.amazon.fr" (the form the library stores)."""
        return f"https://www.{domain}"

    async def _account_site(self) -> Optional[str]:
        """Retail URL of the marketplace Amazon says this account belongs to.

        `/api/welcome` reports the account's own Alexa host — the same call the
        library's interactive login makes. Returns None when the check fails or
        the answer isn't a recognisable Alexa host, so callers keep what they
        have instead of acting on a guess.
        """
        if self._api is None:
            return None
        try:
            host = await self._api.login._get_alexa_domain()
        except Exception as e:  # noqa: BLE001 - a failed check must change nothing
            self._log(f"domain check skipped: {type(e).__name__}: {e}")
            return None
        if not isinstance(host, str) or not host.startswith("alexa.amazon."):
            self._log(f"domain check: ignoring unexpected Alexa host {host!r}")
            return None
        site = f"https://www.{host[len('alexa.'):]}"
        stored = self._api._session_state_data.login_stored_data
        if stored.get("account_site") != site:
            # Kept alongside "site" so a pinned session can still name the
            # account's own marketplace when this check fails — that is what
            # holds the spoken locale in place (see _apply_pinned_site).
            stored["account_site"] = site
            try:
                await self._persist_login_data()
            except Exception as e:  # noqa: BLE001 - harmless: re-learned next connect
                self._log(f"account marketplace not saved: {type(e).__name__}: {e}")
        return site

    def _remembered_account_site(self) -> Optional[str]:
        """The account's marketplace as last reported by Amazon, if we know it."""
        if self._api is None:
            return None
        site = self._api._session_state_data.login_stored_data.get("account_site")
        if isinstance(site, str) and site.startswith("https://www.amazon."):
            return site
        return None

    async def _apply_pinned_site(self) -> None:
        """Route traffic through the marketplace the user picked in app settings.

        Interactive login always runs on amazon.com and the library then sniffs
        `/api/welcome` to pick a host, so an explicit choice has to be applied
        afterwards; a stored login starts on the right host already (see
        start_from_stored), leaving only the locale to check here.

        The pin moves the API host, the cookie domain and the retail domain — but
        deliberately *not* the spoken locale. This setting exists so a user whose
        network can't reach their own regional host can route around it, and the
        locale rides along on `country_specific_data()` purely as a side effect:
        it lands in the `locale` field of every Speak/Announce/Sound payload
        (library `sequence.py`), so honouring the pin there would make an
        English-configured Echo speak French just because its owner had to pin
        amazon.fr. So the locale keeps following the *account's* marketplace.
        """
        if self._api is None or not self.pinned_site:
            return
        ss = self._api._session_state_data
        # Falling back to the remembered marketplace matters most in exactly the
        # case this setting exists for: if the check itself fails, the spoken
        # locale must not silently become the pinned country's.
        locale_site = await self._account_site() or self._remembered_account_site()
        if ss.alexa_website_url.host == f"alexa.{self.pinned_site}":
            self._align_locale(locale_site)
            return
        await self._repin_domain(
            self.site_url(self.pinned_site),
            "server pinned in app settings",
            locale_site=locale_site,
        )

    def _align_locale(self, locale_site: Optional[str]) -> None:
        """Point the spoken locale at `locale_site` without touching the host.

        Only the language is corrected here — cookies and tokens are scoped to
        the domain, the locale is not, so no re-mint is needed.
        """
        if self._api is None or not locale_site:
            return
        ss = self._api._session_state_data
        language = self._language_of(locale_site)
        if language is None or language == ss.language:
            return
        ss._language = language
        self._log(
            f"locale set to {language} (account marketplace), "
            f"host {ss.alexa_website_url.host}"
        )

    def _language_of(self, site: str) -> Optional[str]:
        """The locale the library would derive for `site`, without keeping it.

        `country_specific_data()` is a plain setter over country/domain/language
        with no I/O, so applying the other marketplace and putting the current
        one back is the cheapest way to ask "what locale does amazon.fr mean?"
        without duplicating the library's langcodes handling.
        Pinned to aioamazondevices==14.2.2 — re-check on library bumps.
        """
        if self._api is None:
            return None
        ss = self._api._session_state_data
        current = f"https://www.amazon.{ss.domain}"
        try:
            ss.country_specific_data(site)
            return ss.language
        except Exception as e:  # noqa: BLE001 - unknown TLD: keep the locale we have
            self._log(f"locale for {site} unavailable: {type(e).__name__}: {e}")
            return None
        finally:
            ss.country_specific_data(current)

    async def _heal_domain_pin(self) -> None:
        """Re-check which Alexa host this account belongs to, and re-pin if wrong.

        Every request goes to the host derived from `login_data["site"]`, which
        the library writes exactly *once* — at the tail of interactive login,
        after its `/api/welcome` → `alexaHostName` sniff. A stored login never
        re-checks it and the token/cookie refreshes only mutate that same dict,
        so a session that ends up pinned to amazon.com stays there forever. On a
        non-US account that means `behaviors/v2/automations` answers 200 with an
        empty body (the Flow routine list is silently empty) and the locale stays
        en-US, while devices, volume and media keep working — so nothing looks
        broken. Confirmed on an FR account whose only cure was a manual sign-out
        and sign-in; this does that automatically, once per connect.

        Deliberately one-way: it corrects *away* from the amazon.com default but
        never back to it, mirroring the library's own `login_site != DEFAULT_SITE`
        guard. A wrong or flapping answer therefore can't demote a working
        regional session.

        Best-effort throughout: any failure leaves the session on its current
        host rather than breaking a login that was otherwise fine.

        Uses the library's private `_get_alexa_domain()` and mirrors the domain
        switch of `AmazonLogin._domain_refresh_auth_cookies()` — calling that
        directly would re-mint cookies on *every* connect, because its switch
        branch fires for any non-default domain. Pinned to
        aioamazondevices==14.2.2 — re-check on library bumps.
        """
        if self._api is None:
            return
        if self.pinned_site:
            # The user chose a server explicitly; never second-guess that.
            return
        ss = self._api._session_state_data
        site = await self._account_site()
        if site is None or site == f"https://www.amazon.{ss.domain}":
            return
        if site == DEFAULT_SITE:
            self._log(f"domain check: not moving {ss.alexa_website_url.host} back to {site}")
            return
        await self._repin_domain(site, f"Amazon reports this account on {site}")

    async def _repin_domain(
        self, site: str, reason: str, locale_site: Optional[str] = None
    ) -> bool:
        """Move the session to `site` (a "https://www.amazon.xx" retail URL).

        Switches the domain, re-mints the website cookies for it and saves the
        new host into stored `login_data` so the next start begins there.
        Mirrors the switch in `AmazonLogin._domain_refresh_auth_cookies()` rather
        than calling it, because that one re-mints cookies on *every* connect for
        any non-default domain. Pinned to aioamazondevices==14.2.2.

        `locale_site` keeps the spoken locale on a *different* marketplace than
        the traffic — see _apply_pinned_site. Omit it and the locale follows the
        host, which is what the self-heal wants.
        """
        if self._api is None:
            return False
        ss = self._api._session_state_data
        previous_site = f"https://www.amazon.{ss.domain}"
        language = self._language_of(locale_site) if locale_site else None
        self._log(f"domain: {reason}, session is on {ss.alexa_website_url.host} — re-pinning")
        try:
            ss.country_specific_data(site)
            if language is not None:
                ss._language = language
            await self._api._http_wrapper.clear_csrf_cookie()
            if not await self._refresh_website_cookies():
                # _refresh_website_cookies only clears the cookie jar *after* a
                # successful mint, so nothing has been torn down yet: restoring
                # the domain leaves the session exactly as it was.
                ss.country_specific_data(previous_site)
                self._log("domain re-pin rolled back: cookie mint failed — retrying next connect")
                return False
            self._last_cookie_refresh_ts = time.monotonic()
            try:
                ss.login_stored_data["site"] = site
                await self._persist_login_data()
            except Exception as e:  # noqa: BLE001 - harmless: we re-check next connect
                self._log(f"domain re-pin not saved: {type(e).__name__}: {e}")
            self._log(f"domain re-pinned to {ss.alexa_website_url.host}, locale {ss.language}")
            return True
        except Exception as e:  # noqa: BLE001 - never break an otherwise-fine login
            ss.country_specific_data(previous_site)
            self._log(
                f"domain re-pin failed: {type(e).__name__}: {e} — "
                f"keeping {ss.alexa_website_url.host}"
            )
            return False

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
            return
        # WORKAROUND (aioamazondevices==14.2.2): seed the account customer id from
        # responses passing through here so the library's obtain_account_customer_id()
        # can't fail. It derives the id by scanning the device list for the *just-
        # registered* virtual device's serial, but Amazon stops returning that entry
        # once an account has accumulated many app registrations — so login dies with
        # "Cannot find account owner customer ID" even though registration succeeded.
        # Both the registration response (customer_id) and the device list (the
        # account's own "This Device", deviceType AMAZON_DEVICE_TYPE, carries the same
        # deviceOwnerCustomerId) hold the value; grab it from whichever arrives first.
        # Covers interactive login (register) and stored/reconnect login (device list).
        if not isinstance(raw_data, str) or self._api is None:
            return
        needs_customer_id = not is_directed_customer_id(
            self._api._session_state_data.account_customer_id
        )
        if URI_REGISTER in url:
            if needs_customer_id:
                self._seed_customer_id_from_register(raw_data)
        elif URI_DEVICES in url:
            self._harvest_device_account_ids(raw_data)
            if needs_customer_id:
                self._seed_customer_id_from_devices(raw_data)

    def _harvest_device_account_ids(self, body: str) -> None:
        """Keep the `deviceAccountId` aioamazondevices drops from AmazonDevice.

        It's the key the per-device settings endpoint is addressed by (screen
        power / brightness — see get_device_setting), and this device-list
        response is the only place Amazon hands it out. The library parses the
        same body but doesn't carry the field, so we skim it on the way past.
        """
        try:
            devices = json.loads(body).get("devices", [])
        except (json.JSONDecodeError, AttributeError):
            return
        found = {
            device["serialNumber"]: device["deviceAccountId"]
            for device in devices
            if device.get("serialNumber") and device.get("deviceAccountId")
        }
        if found and not self._device_account_ids:
            self._log(f"device settings available for {len(found)} device(s)")
        self._device_account_ids.update(found)

    async def _seed_customer_id_from_device_list(self) -> None:
        """Fetch the device list once purely to learn the directed customer id.

        Cheaper and more predictable than the library's own lookup, which polls
        the same endpoint up to 30 times waiting for the *just-registered*
        virtual device to show up — something Amazon stops doing once an account
        has collected a pile of app registrations.
        """
        if self._api is None:
            return
        try:
            await self._api._http_wrapper.session_request(
                method=HTTPMethod.GET,
                url=URL.joinpath(
                    self._api._session_state_data.alexa_website_url, URI_DEVICES
                ),
            )
        except Exception as e:  # noqa: BLE001 - the library's lookup still follows
            self._log(f"customer id lookup via device list failed: {type(e).__name__}: {e}")

    def _seed_customer_id_from_register(self, body: str) -> None:
        try:
            customer_id = json.loads(body)["response"]["success"]["customer_id"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return
        if not customer_id:
            return
        if not is_directed_customer_id(customer_id):
            # Registration returns the obfuscated form, which every sequence
            # POST rejects with 400 — leave the id unset so the device list can
            # supply the directed one.
            self._log("login: registration returned an obfuscated customer id — ignoring it")
            return
        self._api._session_state_data.account_customer_id = customer_id
        self._log("login: seeded account customer id from registration")

    def _seed_customer_id_from_devices(self, body: str) -> None:
        try:
            devices = json.loads(body).get("devices", [])
        except (json.JSONDecodeError, AttributeError):
            return
        for device in devices:
            if (
                device.get("deviceType") == AMAZON_DEVICE_TYPE
                and device.get("deviceOwnerCustomerId")
            ):
                self._api._session_state_data.account_customer_id = device[
                    "deviceOwnerCustomerId"
                ]
                self._log("login: recovered account customer id from device list")
                return

    # --- data ------------------------------------------------------------
    async def refresh_devices(self) -> dict[str, AmazonDevice]:
        """Fetch the device list (serial, name, family, capabilities, type,
        cluster members) — deliberately the *basic* list.

        get_devices_data() would also pull DND/notifications/per-device comms/
        sensor data, but this app uses none of it — media/volume state arrives via
        the push channel, and the library's command methods only need serial,
        type, cluster members and the account customer id. Those extra per-device
        calls are also slow enough on multi-device accounts to exceed Homey's 30s
        pairing timeout (or hang on a throttled comms call), so we skip them.
        """
        await self._api._device_handler.get_base_devices()
        self._devices = self._api._device_handler.devices
        return self._devices

    async def sync_dnd(self) -> None:
        """Poll Do Not Disturb state for every device and publish it.

        Live changes arrive on the push channel (see _allow_dnd_push_events), so
        this is the safety net: it seeds the state at connect and re-syncs on the
        heartbeat, covering a dropped push channel or a library bump that lands
        the push patch on the floor. One GET covers the whole account.

        Uses the library's private handler on purpose: the public
        get_devices_data() would also pull notifications/comms/sensor data,
        which refresh_devices() deliberately avoids (see its docstring).
        Pinned to aioamazondevices==14.2.2 — re-check on library bumps.

        Best-effort: a failure here must never break login or the heartbeat.
        """
        if self._api is None or self.on_dnd is None:
            return
        try:
            sensors = await self._api._dnd_handler.get_do_not_disturb_status()
            await self.on_dnd({serial: bool(s.value) for serial, s in sensors.items()})
        except Exception as e:  # noqa: BLE001
            self._log(f"DND sync failed: {type(e).__name__}: {e}")

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
        await self.sync_dnd()
        await self.ensure_push_channel()
        self._note_csrf_state("heartbeat")

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

    @heal_stale_session
    async def say(self, serial: str, message: str, mode: str = "speak") -> None:
        device = self._device(serial)
        if mode == "announce":
            await self._api.call_alexa_announcement(device, message)
        elif mode == "whisper":
            ssml = f'<speak><amazon:effect name="whispered">{escape_xml(message)}</amazon:effect></speak>'
            await self._api.call_alexa_speak(device, ssml)
        else:
            await self._api.call_alexa_speak(device, message)

    @heal_stale_session
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

    @heal_stale_session
    async def execute_command(self, serial: str, text: str) -> None:
        await self._api.call_alexa_text_command(self._device(serial), text)

    @heal_stale_session
    async def play_sound(self, serial: str, sound_id: str) -> None:
        await self._api.call_alexa_sound(self._device(serial), sound_id)

    @heal_stale_session
    async def run_routine(self, routine_name: str) -> None:
        if self._api is None:
            raise RuntimeError("Not connected to Amazon")
        # call_routine looks routines up by name in a cache only populated by the
        # autocomplete; refresh it (list_routines does) so the flow works even
        # after an app restart.
        names = await self.list_routines()
        if routine_name not in names:
            # call_routine would raise a bare KeyError on the name — say why.
            self._log(
                f"run routine: '{routine_name}' is not among the {len(names)} routine(s) "
                "Amazon returned for this account"
            )
        await self._api.call_routine(routine_name)

    @heal_stale_session
    async def set_volume(self, serial: str, value: float) -> None:
        await self._api.set_device_volume(self._device(serial), round(value * VOLUME_DIVISOR))

    @heal_stale_session
    async def playback(self, serial: str, action: str) -> None:
        await self._api.send_media_command(self._device(serial), _PLAYBACK[action])

    @heal_stale_session
    async def set_do_not_disturb(self, serial: str, enabled: bool) -> None:
        await self._api.set_do_not_disturb(self._device(serial), enabled)

    # --- device settings (screen power / brightness / …) -----------------
    # aioamazondevices doesn't wrap Amazon's per-device settings endpoint, so
    # this speaks to it directly over the library's authenticated session. It's
    # the same surface the Alexa app uses and the only way to reach an Echo
    # Show's display; see AGENTS.md for the settings that matter here.
    def _device_setting_url(self, serial: str, name: str) -> URL:
        if self._api is None:
            raise RuntimeError("Not connected to Amazon")
        account_id = self._device_account_ids.get(serial)
        if not account_id:
            raise RuntimeError(f"No deviceAccountId known for {serial}")
        return URL.joinpath(
            self._api._session_state_data.alexa_website_url,
            URI_DEVICE_SETTINGS.format(account_id=account_id, name=name),
        )

    @heal_stale_session
    async def get_device_setting(self, serial: str, name: str) -> Any:
        """Read one device setting.

        Amazon double-encodes: the response is `{"value": "\\"ON\\""}`, i.e. a
        JSON document whose `value` is itself a JSON-encoded scalar.
        """
        _, raw_resp = await self._api._http_wrapper.session_request(
            method=HTTPMethod.GET, url=self._device_setting_url(serial, name)
        )
        # content_type=None: this endpoint isn't part of the library's own
        # surface, so don't let an unexpected Content-Type header reject a
        # perfectly good JSON body.
        payload = await self._api._http_wrapper.response_to_json(
            raw_resp, f"setting {name}", content_type=None
        )
        value = payload.get("value")
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value

    @heal_stale_session
    async def set_device_setting(self, serial: str, name: str, value: Any) -> None:
        await self._api._http_wrapper.session_request(
            method=HTTPMethod.PUT,
            url=self._device_setting_url(serial, name),
            input_data={"value": json.dumps(value)},
            json_data=True,
        )

    # --- diagnostics -----------------------------------------------------
    def _log_account_context(self) -> None:
        """One line saying which Amazon "world" this session actually talks to.

        A session can be fully authenticated and still be pointed at the wrong
        regional host: the library starts every login on amazon.com and only
        re-pins the domain during *interactive* login (`/api/welcome` →
        `alexaHostName`), while the AVS push region comes from the account
        itself. When those two disagree, region-scoped data — routines above all
        — comes back empty while devices/volume/media keep working. The device
        ownership counts cover the other half of the same question: in an Amazon
        Household devices are shared but routines are per-account, so an account
        that sees devices it doesn't own may legitimately have no routines of its
        own. Both facts are invisible in a diagnostic report otherwise (the
        library redacts owner ids), and neither costs a request.
        """
        if self._api is None:
            return
        try:
            ss = self._api._session_state_data
            region = (ss.login_stored_data.get("customer_info") or {}).get("home_region", "?")
            self._log(
                f"account: host {ss.alexa_website_url.host}, country {ss.country_code.upper()}, "
                f"locale {ss.language}, AVS home region {region}"
            )
            devices = list(self._devices.values())
            own_id = ss.account_customer_id
            household = sum(1 for d in devices if d.household_device)
            if not is_directed_customer_id(own_id):
                # Comparing a directed deviceOwnerCustomerId against an
                # obfuscated account id would report every device as somebody
                # else's — say nothing rather than something false.
                self._log(
                    f"devices: {len(devices)} total, {household} shared via household "
                    "— ownership unknown (no directed customer id yet)"
                )
                return
            owned = sum(1 for d in devices if d.device_owner_customer_id == own_id)
            self._log(
                f"devices: {len(devices)} total — {owned} owned by the signed-in account, "
                f"{len(devices) - owned} owned by another account, {household} shared via household"
            )
        except Exception as e:  # noqa: BLE001 - diagnostics must never break login
            self._log(f"account context unavailable: {type(e).__name__}: {e}")

    async def _probe_routines(self) -> None:
        """Log-only diagnostic for an empty routine list.

        TEMPORARY (added 2026-08-20 while chasing "the routine list is empty").
        The library asks for automations with no query parameters and keeps only
        `status == "ENABLED"`, so an empty Flow autocomplete has three very
        different causes that look identical from here: Amazon returned nothing,
        Amazon returned rows we filtered out, or Amazon wants an explicit `limit`
        (alexa-remote sends 2000, alexapy 1000). Ask once more ourselves — plain
        and with `limit=2000` — and log the *shape* of the answer, no names. This
        changes nothing about what the card offers; delete it once a report tells
        us which cause it is.
        """
        if self._api is None:
            return
        base = URL.joinpath(
            self._api._session_state_data.alexa_website_url, URI_BEHAVIORS_AUTOMATIONS
        )
        for label, url in (("no limit", base), ("limit=2000", base.with_query({"limit": 2000}))):
            try:
                status, entries = await self._fetch_automations(url)
            except Exception as e:  # noqa: BLE001 - a probe must never raise
                self._log(f"routines probe [{label}]: failed: {type(e).__name__}: {e}")
                continue
            statuses: dict[str, int] = {}
            named = 0
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                key = str(entry.get("status"))
                statuses[key] = statuses.get(key, 0) + 1
                if isinstance(entry.get("name"), str) and entry["name"]:
                    named += 1
            histogram = ", ".join(f"{k}={v}" for k, v in sorted(statuses.items())) or "—"
            self._log(
                f"routines probe [{label}]: HTTP {status}, {len(entries)} entries, "
                f"status {histogram}, {named} with a name"
            )

    async def _fetch_automations(self, url: URL) -> tuple[int, list]:
        _, raw_resp = await self._api._http_wrapper.session_request(
            method=HTTPMethod.GET, url=url
        )
        status = raw_resp.status
        # content_type=None: this endpoint answers application/octet-stream, and
        # an unexpected header must not masquerade as "no routines". Empty
        # description keeps the library from dumping the payload a second time.
        payload = await self._api._http_wrapper.response_to_json(
            raw_resp, "", content_type=None
        )
        entries = payload.get(ARRAY_WRAPPER, [])
        return status, entries if isinstance(entries, list) else []

    # --- lookups for flow autocomplete -----------------------------------
    def list_sounds(self) -> list[dict]:
        return sorted(
            ({"id": sound_id, "name": name} for sound_id, name in SOUNDS_LIST.items()),
            key=lambda s: s["name"],
        )

    async def list_routines(self) -> list[str]:
        """Enabled routine names, re-fetched on every call (Flow autocomplete).

        Always reports what it got: "the routine list is empty" is the most
        common support report on this card, and the count plus the host it came
        from is what separates "Amazon returned nothing" from "we dropped it on
        the floor". Names that aren't usable strings are skipped rather than
        allowed to blow up the comprehension — Amazon does return unnamed
        automations, and one of them would otherwise empty the whole list.
        """
        if self._api is None:
            self._log_routine_result(f"routines: no Amazon session (state={self.state})")
            return []
        await self._api.update_routines()
        raw = list(self._api.routines)
        names = sorted((n for n in raw if isinstance(n, str) and n), key=str.lower)
        skipped = len(raw) - len(names)
        detail = f" ({skipped} without a usable name skipped)" if skipped else ""
        host = self._api._session_state_data.alexa_website_url.host
        self._log_routine_result(f"routines: {len(names)} from {host}{detail}")
        if not names and time.monotonic() - self._last_probe_ts >= ROUTINES_PROBE_INTERVAL_S:
            self._last_probe_ts = time.monotonic()
            await self._probe_routines()
        return names

    def _log_routine_result(self, summary: str) -> None:
        """Log the routine outcome, collapsing the per-keystroke repeats."""
        now = time.monotonic()
        previous, logged_at = self._last_routines_log
        if summary != previous or now - logged_at >= ROUTINES_LOG_INTERVAL_S:
            self._log(summary)
            self._last_routines_log = (summary, now)

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