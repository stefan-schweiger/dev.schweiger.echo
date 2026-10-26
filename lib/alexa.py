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
from datetime import datetime, timedelta
from http import HTTPMethod, HTTPStatus
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from xml.sax.saxutils import escape as escape_xml
from zoneinfo import ZoneInfo

import aiohttp
import certifi
from aiohttp.resolver import ThreadedResolver
from dateutil.rrule import DAILY, rrule, rrulestr
import httpx
from yarl import URL
from aioamazondevices.api import AmazonEchoApi
from aioamazondevices.capabilities import DEVICE_CAPABILITIES
from aioamazondevices.exceptions import (
    CannotAuthenticate,
    CannotRetrieveData,
    MissingCountryError,
)
from aioamazondevices.const.http import (
    ARRAY_WRAPPER,
    DEFAULT_SITE,
    REFRESH_ACCESS_TOKEN,
    REFRESH_AUTH_COOKIES,
    REQUEST_AGENT,
    URI_BEHAVIORS_AUTOMATIONS,
    URI_DEVICES,
    URI_NOTIFICATIONS,
)
from aioamazondevices.const.schedules import (
    COUNTRY_GROUPS,
    NOTIFICATION_ALARM,
    NOTIFICATION_MUSIC_ALARM,
    RECURRING_PATTERNS,
    WEEKEND_EXCEPTIONS,
)
from aioamazondevices.const.sounds import SOUNDS_LIST
from aioamazondevices.structures import (
    AmazonDevice,
    AmazonListEvent,
    AmazonListEventType,
    AmazonListItemStatus,
    AmazonMediaControls,
    AmazonSaveDataConfig,
    AmazonSequenceNode,
    AmazonSequenceType,
)

from .connection import MISSING_COUNTRY_MESSAGE, unresolved_host_message
from .constants import DEVICES, SPEAK_VOICES, VOICES

VOLUME_DIVISOR = 100

# Where the library is allowed to keep its own state files. It writes exactly
# one — settings.json, holding the "default device" serial that
# set_default_device() persists, which this app never calls — so nothing lands
# here today; the same path is handed to our _save_to_file callback for every
# raw response. Required since aioamazondevices 15.0.0 (AmazonSaveDataConfig).
# /userdata is the writable mount Homey keeps across app updates, so an upstream
# version that does start writing puts it somewhere sane rather than somewhere
# read-only.
SAVE_DATA_DIR = Path("/userdata")

SINGLE_FAMILIES = {"ECHO", "KNIGHT", "ROOK"}
GROUP_FAMILY = "WHA"

# Per-device settings (screen power, brightness, …). Not exposed by
# aioamazondevices; keyed on deviceAccountId, not the serial number.
URI_DEVICE_SETTINGS = "api/v1/devices/{account_id}/settings/{name}"

# Alarms. The library reads Amazon's notification list but keeps only each
# device's next alarm and can't change one, so both happen here. Changes go to
# Amazon's alarms API on the regional Alexa API host that /api/endpoints names,
# with the bearer token instead of the website cookies. See "Alarms" in AGENTS.md.
URI_ENDPOINTS = "api/endpoints"
URI_ALARM = "v1/alerts/alarms/{alarm_id}/{action}"
ALARM_TYPES = {NOTIFICATION_ALARM, NOTIFICATION_MUSIC_ALARM}
# A snoozed alarm is still on: it rings again in a few minutes.
ALARM_ON_STATUSES = {"ON", "SNOOZED"}
# The library raises CannotRetrieveData for every status except 200, so a 204
# from the alarms API would look like a failure.
SUCCESS_PHRASES = tuple(status.phrase for status in HTTPStatus if status.is_success)

# How long a device must be missing from Amazon's media-session list before it
# counts as stopped — see _clear_ended_media_sessions.
MEDIA_IDLE_GRACE_S = 60

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

# Amazon's item fetch caps how many it will return, and the cap is below 500:
# asking for that answers 400 Bad Request. The library hardcodes limit=100, which
# is the value upstream ships and Home Assistant exercises daily, so stay on it.
# The consequence is real and worth knowing: a list longer than this is read
# truncated, and there is no pagination in the library to work around it.
LIST_READ_LIMIT = 100

# Amazon returns no listName for the two built-in lists, only a listType.
LIST_TYPE_NAMES = {"SHOP": "Shopping List", "TODO": "To-do List"}

# Throttles for the routine diagnostics (see list_routines / _probe_routines).
ROUTINES_LOG_INTERVAL_S = 60
ROUTINES_PROBE_INTERVAL_S = 5 * 60

# Amazon intermittently answers the password POST with a captcha /
# interstitial / rate-limit page instead of the OTP form; the library surfaces
# that as CannotAuthenticate("Unexpected login page '<title>', expected OTP code
# entry") — "MFA OTP code not found on login page" up to 16.8.1. It's transient,
# so we retry the interactive login once (see start_interactive). Genuine bad
# credentials raise a *different* message (since 16.9.1 usually Amazon's own
# error text) and must not be retried.
OTP_PAGE_MISSING = "expected OTP code entry"

# Amazon hands out the account id in two forms and they are NOT interchangeable:
#   - "amzn1.account.AGP2HK…"  obfuscated id, returned by /auth/register
#   - "A146V8AS9QOCRT"         directed id, carried by the device list as
#                              deviceOwnerCustomerId
# Only the directed id works in a behaviours payload. Post a sequence with the
# obfuscated one and Amazon answers 400 Bad Request, so login looks perfect and
# then nothing ever speaks. Since aioamazondevices 16.0.0 the library reads the
# directed id from /api/users/me and keeps it in login_data; the check stays for
# _log_account_context, which must not compare against the wrong form.
OBFUSCATED_CUSTOMER_ID_PREFIX = "amzn1.account."

# What AmazonLogin._extract_code_from_url raises when Amazon's post-login
# redirect carries no authorization code. It is followed by the full redirect
# URL, which is what the user was shown. Up to 16.3.0 this was a bare
# KeyError: 'openid.oa2.authorization_code'.
AUTH_CODE_MISSING = "Unable to extract authorization code"

_PLAYBACK = {
    "play": AmazonMediaControls.Play,
    "pause": AmazonMediaControls.Pause,
    "next": AmazonMediaControls.Next,
    "previous": AmazonMediaControls.Previous,
    "stop": AmazonMediaControls.Stop,
}

# What alexa-remote2's "deviceStopAll" targets: Amazon fills in every device on
# the account itself.
ALL_DEVICES_TARGET = {"deviceSerialNumber": "ALEXA_ALL_DSN", "deviceType": "ALEXA_ALL_DEVICE_TYPE"}


def _opt_out_of_smart_home_push() -> None:
    """Register our virtual device without the smart-home push event format.

    Since aioamazondevices 16.4.0 (upstream PR #1108) the device registers
    `Alexa.Mobile.Push` 1.1 with the `smarthome-events` format. With that, Amazon
    sends an FDAL_UPDATE over the push channel for every light, plug and sensor
    in the household. The library doesn't handle them yet ("Unhandled push event
    type") and this app has no use for them, but on a big smart home they would
    make up most of the push traffic. So restore the 1.0 entry that 16.3.0 and
    every earlier release registered.

    The library sends this dict unchanged on every capability registration
    (interactive sign-in and, since 16.4.0, every stored login), so changing the
    shared object in place covers both. Idempotent, and a no-op if the entry
    changes shape. Pinned to aioamazondevices==17.2.3, re-check on library bumps.
    """
    for interface in DEVICE_CAPABILITIES.get("capabilities", []):
        if interface.get("interface") == "Alexa.Mobile.Push":
            interface.pop("configurations", None)
            interface["version"] = "1.0"


def is_directed_customer_id(value: Optional[str]) -> bool:
    """True for an id the behaviours API accepts (see the prefix constant)."""
    return bool(value) and not value.startswith(OBFUSCATED_CUSTOMER_ID_PREFIX)


def login_error_message(e: BaseException) -> str:
    """User-facing text for a failed interactive login.

    Amazon answers a rejected sign-in by simply not returning an authorization
    code — a wrong or expired 2-step code, a captcha, or an extra verification
    step all look like that — and the library turns it into
    `CannotAuthenticate: Unable to extract authorization code from url: …`
    (a bare `KeyError` up to 16.3.0). Shown verbatim in app settings, that
    reads like a crash, so users retry the same code over and over (one report
    had five attempts in four minutes). Say what to do instead.

    A sign-in that never resolved a hostname is the other one worth naming: it
    surfaces as `CannotConnect: Connection error during GET`, which two testers
    reported verbatim while hunting a problem that was never in the app.
    """
    if isinstance(e, CannotAuthenticate) and AUTH_CODE_MISSING in str(e):
        return (
            "Amazon did not complete the sign-in. Check the 2-step verification code "
            "and try again — if it keeps failing, open the Amazon app or website once "
            "to clear any extra verification step."
        )
    if isinstance(e, MissingCountryError):
        return MISSING_COUNTRY_MESSAGE
    return unresolved_host_message(e) or f"{type(e).__name__}: {e}"


def _voice_ssml(message: str, voice_id: str, mode: str) -> str:
    """SSML speaking `message` in the Polly voice `voice_id` ("Hans:de-DE")."""
    voice, _, lang = voice_id.partition(":")
    content = escape_xml(message)
    if mode == "whisper":
        content = f'<amazon:effect name="whispered">{content}</amazon:effect>'
    return f'<speak><voice name="{voice}"><lang xml:lang="{lang}">{content}</lang></voice></speak>'


def _alarm_on(alarm: dict) -> bool:
    return alarm.get("status") in ALARM_ON_STATUSES


def _alarm_label(alarm: dict) -> str:
    return alarm.get("alarmLabel") or alarm.get("reminderLabel") or ""


def _alarm_repeats(alarm: dict) -> bool:
    return bool(
        alarm.get("recurringPattern")
        or (alarm.get("rRuleData") or {}).get("recurrenceRules")
    )


def _rings(rules: list[rrule], start: datetime, end: datetime) -> list[datetime]:
    """Every ring in [start, end), earliest first."""
    return sorted({at for rule in rules for at in rule.between(start, end, inc=True) if at < end})


def _next_ring(rules: list[rrule], after: datetime) -> Optional[datetime]:
    upcoming = [at for rule in rules if (at := rule.after(after, inc=True)) is not None]
    return min(upcoming, default=None)


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


class _UnpinOnFailureResolver(ThreadedResolver):
    """IPv4 lookups, retried without the family pin when a lone A query fails.

    Measured on an affected Homey (log 7b57c33b, 2026-08-27): three consecutive
    `alexa.amazon.de` A lookups failed with EAI_NODATA at 1-3 ms, and the very
    next lookup of the *same name* with no family pinned succeeded in 38 ms —
    after which every hop of the chain resolved from cache. Something between the
    app and an answer mishandles a lone A query for a CNAME chain but copes when
    glibc asks for both families together.

    We pin to IPv4 because Homey has no IPv6 route (see _shared_connector), so
    the retry filters the result back down to IPv4 addresses only. The Alexa
    hosts have no AAAA records at all (`AAAA: FAILED errno=-2` in the same log),
    so in practice the unpinned query returns exactly the addresses the pinned
    one should have returned.

    Best-effort: if the retry also fails, the original error is raised so the
    message a user sees still names the host and the real EAI code.
    """

    def __init__(self, log: Callable[[str], None], *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._log = log

    async def resolve(self, host: str, port: int = 0, family=socket.AF_INET):
        try:
            return await super().resolve(host, port, family)
        except OSError as e:
            if family != socket.AF_INET:
                raise
            first = e

        # Log every outcome, not just the good one. A silent re-raise would make
        # a report from an affected user look exactly like one from before this
        # existed, and the three cases below need different answers.
        # flags=0 on purpose. aiohttp's own resolver passes AI_ADDRCONFIG, but the
        # call measured succeeding on an affected device used no flags, and so does
        # Node's dns.lookup (which is how HomeyScript resolves). AI_ADDRCONFIG can
        # suppress the AAAA half of the query on a host with no global IPv6
        # address, which would turn this retry into a repeat of the lookup that
        # just failed.
        try:
            infos = await self._loop.getaddrinfo(
                host, port, type=socket.SOCK_STREAM, family=socket.AF_UNSPEC, flags=0
            )
        except OSError as second:
            self._log(
                f"dns: {host} failed both ways — IPv4 ({first}), unpinned ({second})"
            )
            raise first from None

        ipv4 = [
            {
                "hostname": host,
                "host": address[0],
                "port": address[1],
                "family": socket.AF_INET,
                "proto": proto,
                "flags": socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
            }
            for fam, _, proto, _, address in infos
            if fam == socket.AF_INET
        ]
        if not ipv4:
            self._log(
                f"dns: {host} had no IPv4 answer ({first}); the unpinned retry "
                f"returned {len(infos)} address(es), none of them IPv4"
            )
            raise first from None

        self._log(
            f"dns: {host} had no IPv4 answer ({first}); "
            "retrying without the family pin worked"
        )
        return ipv4


class AlexaService:
    def __init__(self, log: Callable[[str], None]):
        self._log = log
        self._session: Optional[aiohttp.ClientSession] = None
        # Outlives the session on purpose — see _shared_connector.
        self._connector: Optional[aiohttp.TCPConnector] = None
        # None until first observed — see _note_csrf_state.
        self._csrf_present: Optional[bool] = None
        # Payload keys ARE the list-item-added card's token names, and it is
        # forwarded verbatim — keep the two in step. See _handle_todo_event.
        self.on_list_item: Optional[Callable[[dict], Awaitable[None]]] = None
        self._httpx: Optional[httpx.AsyncClient] = None
        self._api: Optional[AmazonEchoApi] = None
        self._devices: dict[str, AmazonDevice] = {}
        # serial -> deviceAccountId, skimmed off the raw device-list response
        # (see _harvest_device_account_ids).
        self._device_account_ids: dict[str, str] = {}
        # serial -> when it was first seen without a media session.
        self._media_missing_since: dict[str, float] = {}
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
        # The Homey's timezone, kept current by app.py. Alarm times are wall-clock
        # times on the Echo (see _local_now).
        self.timezone = "UTC"
        # Regional Alexa API, looked up once per session (see _resolve_alexa_api_url).
        self._alexa_api_url: Optional[URL] = None
        # alarm id -> the ring this app last skipped (see skip_alarms).
        self._skipped_rings: dict[str, datetime] = {}
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
    def alexa_host(self) -> Optional[str]:
        """The alexa.amazon.<tld> host this session talks to, or None if not connected.

        Commands go to this name while sign-in and token refresh go to
        api.amazon.com, which is why a session can look connected and still
        refuse every command: only one of the two names has to stop resolving.
        """
        if self._api is None:
            return None
        return self._api._session_state_data.alexa_website_url.host

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
            # throttles the Homey's IP. (The library's account-id lookup is a
            # single GET /api/users/me since aioamazondevices 16.0.0.)
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
        # Pinned to aioamazondevices==17.2.3 — re-check on library bumps.
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
                resolver=_UnpinOnFailureResolver(self._log),
            )
        return self._connector

    def _build(self, email: str, password: str, login_data: Optional[dict]) -> None:
        # Fresh session — let recovery have its full budget of attempts again.
        self._recovery_attempts = 0
        self._last_recovery_ts = 0.0
        _opt_out_of_smart_home_push()
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
            save_data=AmazonSaveDataConfig(
                path=SAVE_DATA_DIR, callback=self._save_to_file
            ),
        )
        self._build_devices_without_features_on_error()

    def _build_devices_without_features_on_error(self) -> None:
        """Keep a failed features query from failing the device list.

        `update_devices()` asks Amazon's GraphQL API for every Echo's features
        (switches, sensors, …) and, since aioamazondevices 16.9.3 (upstream PR
        #1203), raises CannotRetrieveData when that answer carries any GraphQL
        error, where it used to build the devices without features. This app
        uses no features, but refresh_devices() runs at every sign-in and at
        pairing, so such an error would fail the connection outright. Answer
        "no features" instead, as 16.9.2 did: the library then keeps each
        device's last known features. Transport and auth errors still raise.

        The same query also runs after every push reconnect (16.9.4's sensor
        resync), where the library catches failures itself. Private API, pinned
        to aioamazondevices==17.2.3; re-check on library bumps.
        """
        handler = self._api._device_handler
        fetch_features = handler._get_endpoints_features

        async def features_or_none(endpoint_ids: list[str]) -> dict[str, dict[str, Any]]:
            try:
                return await fetch_features(endpoint_ids)
            except CannotRetrieveData as e:
                self._log(f"devices: Amazon's feature query failed ({e}), continuing without")
                return {}

        handler._get_endpoints_features = features_or_none

    async def _handle_todo_event(self, event: AmazonListEvent) -> None:
        """Turn an item-added list event into the app's trigger.

        Subscribed in _after_login rather than intercepting the push ourselves.
        The library resolves the item for us, at the cost of one known edge: its
        _handle_item_change_event looks the item up with an unguarded
        `list_items[item_id]`, so if Amazon's push ever beats its own list read
        the KeyError is swallowed by http2.py and this event never arrives.
        Accepted deliberately — that race was raised by a review bot (upstream
        #893, closed without the guard landing) and no user has reported it in
        the months this has shipped in Home Assistant. A missed trigger with a
        logged traceback is a cheaper failure than carrying our own push
        interceptor and retry loop.

        Updates and deletions come in on the same signal; only additions have a
        card, and DELETED carries no item at all.
        """
        if self.on_list_item is None or event.type != AmazonListEventType.CREATED:
            return
        item = event.items
        if item is None:
            return
        await self.on_list_item(
            {
                "item": item.name,
                "list": await self._list_name(event.list_id),
                "item_id": item.id,
                "list_id": event.list_id,
            }
        )

    async def list_lists(self) -> list[dict]:
        """Alexa's lists, refetched so one created today actually shows up.

        `api.todo_lists` is only refilled on the library's own daily refresh, so
        reading the property alone would hide a list made this morning — the same
        trap list_routines() works around.

        Deliberately not called at login. This endpoint lives on the *retail*
        host (www.amazon.<tld>), which is one of the names that fails on the
        networks in docs/dns-investigation.md — 2.2.0 fetched it in _after_login
        and put a guaranteed failure into those users' logs for a feature they
        may never use. _list_name() refetches on a miss, so nothing needs it
        earlier than first use.
        """
        if self._api is None:
            raise RuntimeError("Not connected to Amazon")
        await self._api._todo_handler.update_lists()
        return [
            {
                "id": info.id,
                "name": info.name or LIST_TYPE_NAMES.get(info.list_type, info.list_type),
            }
            for info in self._api.todo_lists
        ]

    async def list_items(self, list_id: str) -> list[dict]:
        """Every item on a list, as plain dicts a Flow token can carry.

        `version` is included because it is what the remove/complete actions need,
        so a Flow can read once and act without a second lookup.

        Returns at most LIST_READ_LIMIT items — see that constant.
        """
        if self._api is None:
            raise RuntimeError("Not connected to Amazon")
        items = await self._api.get_todo_list_items(list_id)
        return [
            {
                "id": item.id,
                "name": item.name,
                "status": str(item.status),
                "version": item.version,
            }
            for item in items.values()
        ]

    async def _live_version(self, list_id: str, item_id: str) -> int:
        """The item's current version, which both writes require.

        Amazon versions list items for optimistic concurrency and rejects a write
        carrying a stale one, so there is no way to simply not send it. Looking it
        up here keeps it off the Flow cards — a version is not something a user
        should have to think about.

        One read per write, and the read is capped at LIST_READ_LIMIT, so an item
        on a list longer than that cannot be found and the write fails. Accepted:
        no pagination exists in the library to do better.
        """
        items = await self._api.get_todo_list_items(list_id)
        item = items.get(item_id)
        if item is None:
            raise RuntimeError(
                f"That item was not among the {len(items)} item(s) read from "
                f"{await self._list_name(list_id)} — it may already be gone, or the "
                f"list is longer than the {LIST_READ_LIMIT} items Amazon returns"
            )
        return item.version

    async def remove_list_item(self, list_id: str, item_id: str) -> None:
        if self._api is None:
            raise RuntimeError("Not connected to Amazon")
        version = await self._live_version(list_id, item_id)
        await self._api.delete_todo_list_item(list_id, item_id, version)
        self._log(f"list: removed an item from {await self._list_name(list_id)}")

    async def complete_list_item(self, list_id: str, item_id: str) -> None:
        if self._api is None:
            raise RuntimeError("Not connected to Amazon")
        version = await self._live_version(list_id, item_id)
        await self._api.set_todo_list_item_checked_status(
            list_id, item_id, True, version
        )
        self._log(f"list: ticked off an item on {await self._list_name(list_id)}")

    def _cached_list_name(self, list_id: str) -> Optional[str]:
        for info in self._api.todo_lists:
            if info.id == list_id:
                return info.name or LIST_TYPE_NAMES.get(info.list_type, info.list_type)
        return None

    async def _list_name(self, list_id: str) -> str:
        """Display name for a list id, refetching the index on a miss.

        `api.todo_lists` is filled by the library's _refresh_basic_data(), which
        only runs from get_devices_data() — a method this app never calls (see
        refresh_devices). So without a fetch of our own the cache is empty for the
        whole session and every name would come out as the raw base64 list id.
        Seeded at login; this covers a list created since then.
        """
        name = self._cached_list_name(list_id)
        if name is None:
            try:
                await self._api._todo_handler.update_lists()
            except Exception as e:  # noqa: BLE001 — a name is not worth failing for
                self._log(f"list index refresh failed: {type(e).__name__}: {e}")
            name = self._cached_list_name(list_id)
        return name or list_id

    async def _after_login(self) -> None:
        await self.refresh_devices()
        self._log_account_context()
        self._api.on_volume_state_event.append(self._handle_volume)
        self._api.on_volume_state_event.freeze()
        self._api.on_media_state_event.append(self._handle_media)
        self._api.on_media_state_event.freeze()
        self._api.on_todo_event.append(self._handle_todo_event)
        self._api.on_todo_event.freeze()
        self._api.on_dnd_event.append(self._handle_dnd)
        self._api.on_dnd_event.freeze()
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
            self._alexa_api_url = None
            self._media_missing_since = {}
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
        jar. Pinned to aioamazondevices==17.2.3 — re-check on library bumps.

        The cookie-unpacking loop below is adapted from aioamazondevices
        (https://github.com/chemelli74/aioamazondevices), Copyright the
        aioamazondevices authors, licensed under the Apache License 2.0. That
        portion remains under Apache-2.0 rather than this app's MIT license.
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
        Reads a private attribute; pinned to aioamazondevices==17.2.3.
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
        Pinned to aioamazondevices==17.2.3 — re-check on library bumps.
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
        aioamazondevices==17.2.3 — re-check on library bumps.
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
        any non-default domain. Pinned to aioamazondevices==17.2.3.

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
    async def _save_to_file(
        self,
        path,
        raw_data,
        url: str = "login_data",
        content_type: str = "application/json",
    ) -> None:
        # `path` is the AmazonSaveDataConfig directory (SAVE_DATA_DIR) and is
        # deliberately unused: this app keeps nothing on disk, login_data goes to
        # Homey's settings store through on_login_data. The callback grew that
        # leading argument in aioamazondevices 15.0.0.
        if isinstance(raw_data, dict) and url == "login_data" and self.on_login_data is not None:
            await self.on_login_data(raw_data)
            return
        if isinstance(raw_data, str) and URI_DEVICES in url:
            self._harvest_device_account_ids(raw_data)

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

        Since aioamazondevices 16.3.1 this list is built from the GraphQL
        `listEndpoints` query, enriched with `api/devices-v2/device`. That costs
        three requests instead of one. An Echo that GraphQL doesn't list is
        dropped (speaker groups excepted), and an empty GraphQL answer on the
        first load raises CannotRetrieveData("No device endpoint data received").
        devices-v2 is still fetched, so _harvest_device_account_ids still sees it.
        """
        await self._api._device_handler.update_devices()
        self._devices = self._api._device_handler.devices
        self._seed_default_device()
        return self._devices

    def _seed_default_device(self) -> None:
        """Give the library a device to fall back on for routines.

        `call_routine()` posts from `devices[self._api._default_device_serial]`,
        and that serial is only ever filled in by the library's own
        `_init_default_device()`, which runs inside `get_devices_data()` — the
        very call refresh_devices() avoids (see its docstring). Unseeded it is
        the empty string, so the fallback would die on `KeyError: ''`. Up to
        14.2.2 `default_device` was a lazy property that fell back to the first
        online device; 15.1.0 turned it into persisted state and moved that
        fallback out of our path, so do it here.

        Only a fallback: run_routine() posts from the device the flow card names,
        because the device decides where a "reply on the device you speak to"
        step lands (see its docstring). This is what happens when the card's
        device is no longer in the session's list.

        Skips speaker groups and stereo pairs deliberately: `call_routine()`
        posts once per cluster member, so a routine falling back onto a group
        would run twice, and a `WHA` serial is not accepted by the sequence
        endpoint at all (see "Speaker groups are a media target only" in
        AGENTS.md). The library's own fallback takes the first online device of
        any kind and would happily land on one.

        We never call set_default_device(), so there is no persisted choice to
        respect: first online single device is the whole policy.
        Pinned to aioamazondevices==17.2.3 — re-check on library bumps.
        """
        if self._api is None or not hasattr(self._api, "_default_device_serial"):
            return
        if self._api._default_device_serial in self._devices:
            return
        online = [d for d in self._devices.values() if d.online]
        # A group-only account gets a group anyway: better to let the routine
        # fail on Amazon's answer than on a KeyError with no explanation.
        chosen = next(
            (d for d in online if len(d.device_cluster_members) <= 1),
            online[0] if online else None,
        )
        if chosen is None:
            self._log("routine fallback device: none (no device is online)")
            return
        self._api._default_device_serial = chosen.serial_number
        self._log(f"routine fallback device: {chosen.account_name} ({chosen.serial_number})")

    async def sync_dnd(self) -> None:
        """Poll Do Not Disturb state for every device and publish it.

        Live changes arrive on the push channel (the library's on_dnd_event,
        see _handle_dnd), so this is the safety net: it seeds the state at
        connect and re-syncs on the heartbeat, covering a dropped push channel.
        One GET covers the whole account; the result reaches us through the
        same on_dnd_event signal as a push.

        Best-effort: a failure here must never break login or the heartbeat.
        """
        if self._api is None or self.on_dnd is None:
            return
        try:
            await self._api.sync_dnd_state()
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
        # A stored login only does useful work here while the account customer
        # id is missing, i.e. after a stored login that failed partway: the
        # id lookup is the one thing it did up to 16.3.0. Since 16.4.0 every call
        # also re-registers the device capabilities (another token refresh and
        # a PUT), so don't repeat it every heartbeat.
        if not self._api._session_state_data.account_customer_id:
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
        in_session = {s for s, m in payload.items() if m is not None and m.player_state}
        for serial in in_session:
            self._media_missing_since.pop(serial, None)
            await self.on_media(serial, payload[serial])
        await self._clear_ended_media_sessions(in_session)

    async def _clear_ended_media_sessions(self, in_session: set[str]) -> None:
        """Send None to every device whose media session has ended.

        Amazon reports no "stopped" state. When playback ends the device drops
        out of the media-session list, and when nothing is playing anywhere the
        library emits an empty payload. Forwarding only the sessions it reports
        left the last PLAYING on the tile for good, and across restarts too,
        because Homey keeps capability values. Support log c5021fa8: an Echo Spot
        showed "playing" and Pause got a 404 because nothing was playing.

        The list also drops a device briefly around track changes, so a device
        counts as stopped only after it has been missing for MEDIA_IDLE_GRACE_S
        across two observations (a push event or the heartbeat). A speaker-group
        member is never cleared while its group is in session: App._fanout
        mirrors the group's state onto the members, and Amazon may list only the
        group.
        """
        now = time.monotonic()
        for serial, device in self._devices.items():
            if not device.media_player_supported or serial in in_session:
                continue
            if any(parent in in_session for parent in device.parent_clusters):
                self._media_missing_since.pop(serial, None)
                continue
            since = self._media_missing_since.setdefault(serial, now)
            if now - since >= MEDIA_IDLE_GRACE_S:
                await self.on_media(serial, None)

    async def _handle_dnd(self, payload: dict[str, bool]) -> None:
        # The library sends its whole cached {serial: enabled} map on every
        # change, not just the device that flipped; apply_dnd() ignores repeats.
        if self.on_dnd is None:
            return
        await self.on_dnd(payload)

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
        device = self._device(serial)
        ssml = _voice_ssml(message, voice_id, mode)
        if voice_id.partition(":")[0] in SPEAK_VOICES:
            await self._api.call_alexa_speak(device, ssml)
            return
        # Only a Flow saved before "Announce with Voice" existed gets here: the
        # card stopped offering these voices because Speak reads them in the
        # account's own voice. Announce instead so the Flow keeps working — at
        # the cost of Do Not Disturb silencing it, as on the Announce card.
        await self._announce_ssml(device, ssml, message)

    @heal_stale_session
    async def announce_with_voice(self, serial: str, message: str, voice_id: str) -> None:
        await self._announce_ssml(self._device(serial), _voice_ssml(message, voice_id, "speak"), message)

    async def _announce_ssml(self, device: AmazonDevice, ssml: str, text: str) -> None:
        """Speak `ssml` as an SSML announcement instead of through Alexa.Speak.

        Alexa.Speak drops every Polly voice outside SPEAK_VOICES and reads the
        text in the account's own voice; an AlexaAnnouncement with
        `speak.type: "ssml"` renders them (see "SSML" in AGENTS.md). That is
        what the old alexa-remote2 based app sent. The library only builds text
        announcements, so take its node, flip the type and queue it the way
        `send_message` queues its own. Private API — pinned to
        aioamazondevices==17.2.3, re-check on library bumps.
        """
        handler = self._api._sequence_handler
        # Echo Show screens print the display body: show the words, not the markup.
        node = handler._build_operation_node(
            device, AmazonSequenceType.Announcement, ssml, display_text=text
        )
        node["operationPayload"]["content"][0]["speak"]["type"] = "ssml"
        await handler._enqueue_sequence(
            AmazonSequenceNode(
                message_type=AmazonSequenceType.Announcement,
                message_body=ssml,
                music_provider_id=None,
                device=device,
                operation_node=node,
            )
        )

    def list_voices(self, query: str = "", speak_only: bool = False) -> list[dict]:
        """Voices for a picker. `speak_only` limits it to what Alexa.Speak renders."""
        q = (query or "").lower()
        voices = [
            {"id": f"{v['id']}:{v['lang']}", "name": v["name"]}
            for v in VOICES
            if not speak_only or v["id"] in SPEAK_VOICES
        ]
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
    async def play_music(self, serial: str, search_phrase: str, provider_id: str) -> None:
        await self._api.call_alexa_music(self._device(serial), search_phrase, provider_id)

    @heal_stale_session
    async def stop_all(self) -> None:
        """Stop playback on every device on the account, in one command.

        The library only builds a Stop for one device, so take its node and
        retarget it at ALL_DEVICES_TARGET, with the same payload alexa-remote2
        sends for "deviceStopAll" (no device or locale of its own,
        `isAssociatedDevice: false`), which is what the old app's "stop all" card
        sends. Private API, pinned to aioamazondevices==17.2.3. Re-check it on
        library bumps.
        """
        if self._api is None:
            raise RuntimeError("Not connected to Amazon")
        # The node is built from any device. Its own target is replaced below.
        device = next(iter(self._devices.values()), None)
        if device is None:
            raise RuntimeError("No devices found on this Amazon account")
        handler = self._api._sequence_handler
        node = handler._build_operation_node(device, AmazonSequenceType.Stop)
        payload = node["operationPayload"]
        for key in ("deviceType", "deviceSerialNumber", "locale"):
            payload.pop(key, None)
        payload["devices"] = [ALL_DEVICES_TARGET]
        payload["isAssociatedDevice"] = False
        await handler._enqueue_sequence(
            AmazonSequenceNode(
                message_type=AmazonSequenceType.Stop,
                message_body=None,
                music_provider_id=None,
                device=device,
                operation_node=node,
            )
        )

    @heal_stale_session
    async def run_routine(self, routine_name: str, serial: Optional[str] = None) -> None:
        """Run a routine, from `serial` when the flow card named a device.

        A routine *is* device specific, whatever the library's own comment on
        call_routine() says. Amazon stores the sequence with the placeholders
        `ALEXA_CURRENT_DSN` / `ALEXA_CURRENT_DEVICE_TYPE` wherever a step was
        configured for "the Alexa device you speak to", and
        `utils.replace_routine_placeholders()` substitutes whichever device the
        sequence is posted from. So a routine whose reply is spoken on the
        device you talked to answers on *that* device — and `call_routine()`
        hardcodes the library's default device, which would be an Echo the user
        never picked. The `run-routine` card carries a device argument (Homey
        injects one into every card in a driver's flow compose file), so pass it
        through and post the sequence from there.

        Posts to the one device rather than through
        `_call_alexa_command_per_cluster_member()`, which call_routine() uses:
        that loops over cluster members, so picking a stereo-paired Echo would
        run the whole routine twice. Routines with no placeholders are
        unaffected either way — the payload is the routine's own sequence.

        Falls back to `call_routine()` when no serial is given or the device is
        unknown to this session (see _seed_default_device for what that picks).
        Uses the library's private sequence handler — the only single-device
        entry point for a routine. Pinned to aioamazondevices==17.2.3 — re-check
        on library bumps.
        """
        if self._api is None:
            raise RuntimeError("Not connected to Amazon")
        # The routine cache is only populated by the autocomplete; refresh it
        # (list_routines does) so the flow works even after an app restart.
        names = await self.list_routines()
        if routine_name not in names:
            # Both paths raise a bare KeyError on the name — say why.
            self._log(
                f"run routine: '{routine_name}' is not among the {len(names)} routine(s) "
                "Amazon returned for this account"
            )
        device = self._devices.get(serial) if serial else None
        if device is None:
            if serial:
                self._log(
                    f"run routine: device {serial} is not in this session's device list; "
                    "posting from the account's default device instead"
                )
            await self._api.call_routine(routine_name)
            return
        await self._api._sequence_handler.send_message(
            device, AmazonSequenceType.Routines, routine_name
        )

    @heal_stale_session
    async def set_volume(self, serial: str, value: float) -> None:
        await self._api.set_device_volume(self._device(serial), round(value * VOLUME_DIVISOR))

    @heal_stale_session
    async def playback(self, serial: str, action: str) -> None:
        try:
            await self._api.send_media_command(self._device(serial), _PLAYBACK[action])
        except CannotRetrieveData as e:
            # Amazon answers 404 when the device has no media session at all.
            if not str(e).endswith(HTTPStatus.NOT_FOUND.phrase):
                raise
            if action == "pause":
                # Pausing wants nothing to be playing, and that is already true.
                # Raising here meant a stale "playing" tile could not be cleared
                # from Homey (support log c5021fa8).
                self._log(f"pause: nothing playing on {serial}, treating it as paused")
                return
            raise RuntimeError("Nothing is playing or paused on this device") from e

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

    # --- alarms ------------------------------------------------------------
    # aioamazondevices reads Amazon's notification list but keeps only each
    # device's next alarm, and it can't change one. So the list is read here, and
    # alarms are changed through Amazon's alarms API: the activate/cancel calls
    # alexa-remote2 and ioBroker.alexa2 have used since 2022. See "Alarms" in
    # AGENTS.md.
    def _local_now(self) -> datetime:
        """Now as wall-clock time in the Homey's timezone, without tzinfo.

        Amazon gives alarm times as wall-clock times on the Echo, so "today" and
        "tomorrow" come from the Homey's timezone, never from the process clock's
        (Homey apps are not guaranteed to run in local time). tzdata ships with
        python-dateutil, a library dependency.
        """
        return datetime.now(ZoneInfo(self.timezone)).replace(tzinfo=None)

    def _day_window(self, day: str) -> tuple[datetime, datetime]:
        """[start, end) of "today" or "tomorrow". Today starts now: a ring that is
        already over doesn't count."""
        now = self._local_now()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if day == "today":
            return now, midnight + timedelta(days=1)
        return midnight + timedelta(days=1), midnight + timedelta(days=2)

    def _recurring_patterns(self) -> dict[str, str]:
        """Amazon's recurrence names as RFC 5545 rules, with the account's weekend.

        The same table and country adjustment as the library's
        _parse_next_occurrence.
        """
        patterns = RECURRING_PATTERNS.copy()
        country = self._api._session_state_data.country_code.upper()
        for group, countries in COUNTRY_GROUPS.items():
            if country in countries:
                patterns |= WEEKEND_EXCEPTIONS[group]
                break
        return patterns

    def _alarm_rules(self, alarm: dict) -> list[rrule]:
        """When `alarm` rings, as rules over the Echo's wall-clock time.

        Reads the schedule the way aioamazondevices does (notification.py
        _parse_next_occurrence): Amazon's recurrence names or the RFC 5545 rules
        in rRuleData, from originalDate at originalTime. A one-time alarm is a
        rule with a single ring. The library's version only answers "next ring
        after the process clock"; rules answer any window, from the Homey's "now".
        """
        try:
            hour, minute = (int(part) for part in alarm["originalTime"].split(":")[:2])
            first_day = (
                datetime.fromisoformat(alarm["originalDate"])
                if alarm.get("originalDate")
                else None
            )
        except (KeyError, AttributeError, ValueError):
            return []
        recurrences = list((alarm.get("rRuleData") or {}).get("recurrenceRules") or [])
        if alarm.get("recurringPattern"):
            recurrences.append(alarm["recurringPattern"])
        if not recurrences:
            if first_day is None:
                return []
            return [rrule(DAILY, count=1, dtstart=first_day.replace(hour=hour, minute=minute))]

        dtstart = first_day or self._local_now().replace(hour=0, minute=0, second=0, microsecond=0)
        patterns = self._recurring_patterns()
        rules: list[rrule] = []
        for recurrence in recurrences:
            rule = patterns.get(recurrence, recurrence).removesuffix(";")
            if "BYHOUR=" not in rule:
                rule += f";BYHOUR={hour}"
            if "BYMINUTE=" not in rule:
                rule += f";BYMINUTE={minute}"
            try:
                rules.append(rrulestr(rule, dtstart=dtstart))
            except ValueError:
                # Without this an alarm would silently drop out of "has an
                # alarm tomorrow", with nothing in a report to say why.
                self._log(f"alarms: ignoring unknown recurrence '{recurrence}'")
        return rules

    async def _alarms(self, serial: str) -> list[dict]:
        """The Echo's alarms, on and off, as Amazon's notification list has them."""
        if self._api is None:
            raise RuntimeError("Not connected to Amazon")
        _, raw_resp = await self._api._http_wrapper.session_request(
            method=HTTPMethod.GET,
            url=URL.joinpath(self._api._session_state_data.alexa_website_url, URI_NOTIFICATIONS),
        )
        payload = await self._api._http_wrapper.response_to_json(
            raw_resp, "alarms", content_type=None
        )
        return [
            notification
            for notification in payload.get("notifications") or []
            if notification.get("type") in ALARM_TYPES
            and notification.get("deviceSerialNumber") == serial
            and notification.get("notificationIndex")
        ]

    async def _resolve_alexa_api_url(self) -> URL:
        """The regional Alexa API (https://api.eu.amazonalexa.com, …), once per session.

        Amazon's /api/endpoints names it, which is how alexa-remote2 finds it.
        The account's AVS home region would point there too, but the library
        notes that it is not always right (http2.py _check_avs_site).
        """
        if self._alexa_api_url is None:
            _, raw_resp = await self._api._http_wrapper.session_request(
                method=HTTPMethod.GET,
                url=URL.joinpath(self._api._session_state_data.alexa_website_url, URI_ENDPOINTS),
            )
            payload = await self._api._http_wrapper.response_to_json(
                raw_resp, "endpoints", content_type=None
            )
            if not payload.get("alexaApiUrl"):
                raise RuntimeError("Amazon did not name the server that handles alarms")
            self._alexa_api_url = URL(payload["alexaApiUrl"])
            self._log(f"alarms: Alexa API at {self._alexa_api_url.host}")
        return self._alexa_api_url

    async def _alarm_request(self, alarm_id: str, action: str, body: Optional[dict] = None) -> None:
        """PUT one change to Amazon's alarms API.

        Authenticated with the bearer token, which the heartbeat refreshes, not
        the website cookies. Any 2xx answer counts as success (see
        SUCCESS_PHRASES).
        """
        url = URL.joinpath(
            await self._resolve_alexa_api_url(),
            URI_ALARM.format(alarm_id=alarm_id, action=action),
        )
        token = self._api._session_state_data.login_stored_data.get(REFRESH_ACCESS_TOKEN)
        try:
            await self._api._http_wrapper.session_request(
                method=HTTPMethod.PUT,
                url=url,
                input_data=body,
                json_data=body is not None,
                extended_headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": REQUEST_AGENT["Amazon"],
                },
            )
        except CannotRetrieveData as e:
            if not str(e).endswith(SUCCESS_PHRASES):
                raise
        self._log(f"alarm {alarm_id}: {action}")

    def _activation(self, alarm: dict) -> dict:
        """Body for turning `alarm` on: its next ring and its tone.

        The same shape alexa-remote2 sends. A one-time alarm whose day has
        passed is set for its time today, or tomorrow if that's over too.
        """
        now = self._local_now()
        at = _next_ring(self._alarm_rules(alarm), now)
        if at is None:
            hour, minute = (int(part) for part in alarm["originalTime"].split(":")[:2])
            at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if at <= now:
                at += timedelta(days=1)
        body: dict[str, Any] = {
            "trigger": {"scheduledTime": at.strftime("%Y-%m-%dT%H:%M:%S")},
            "extensions": [],
        }
        if alarm.get("musicAlarmId"):
            body["assets"] = [{"type": "TONE", "assetId": f"MUSIC-{alarm['musicAlarmId']}"}]
        elif (alarm.get("sound") or {}).get("id"):
            body["assets"] = [{"type": "TONE", "assetId": alarm["sound"]["id"]}]
        return body

    @heal_stale_session
    async def list_alarms(self, serial: str) -> list[dict]:
        """Every alarm on the Echo, by time of day, for the Flow picker."""
        now = self._local_now()
        alarms = [
            {
                "id": alarm["notificationIndex"],
                "time": (alarm.get("originalTime") or "")[:5],
                "label": _alarm_label(alarm),
                "on": _alarm_on(alarm),
                "next": _next_ring(self._alarm_rules(alarm), now) if _alarm_on(alarm) else None,
            }
            for alarm in await self._alarms(serial)
        ]
        return sorted(alarms, key=lambda a: (a["time"], a["label"].lower()))

    @heal_stale_session
    async def alarms_on(self, serial: str, day: str) -> list[dict]:
        """The rings on `day`, earliest first, as {"time": "06:30", "label": …}.

        Only alarms that are on, and not a ring this app skipped. Today counts
        only rings still to come.
        """
        start, end = self._day_window(day)
        rings = []
        for alarm in await self._alarms(serial):
            if not _alarm_on(alarm):
                continue
            skipped = self._skipped_rings.get(alarm["notificationIndex"])
            rings += [
                (at, _alarm_label(alarm))
                for at in _rings(self._alarm_rules(alarm), start, end)
                if at != skipped
            ]
        return [{"time": f"{at:%H:%M}", "label": label} for at, label in sorted(rings)]

    @heal_stale_session
    async def set_alarm_on(self, serial: str, alarm_id: str, on: bool) -> None:
        alarm = next(
            (a for a in await self._alarms(serial) if a["notificationIndex"] == alarm_id),
            None,
        )
        if alarm is None:
            raise RuntimeError("This alarm no longer exists on the Echo. Pick it again on the card.")
        if _alarm_on(alarm) == on:
            self._log(f"alarm {alarm_id}: already {'on' if on else 'off'}")
            return
        if on:
            await self._alarm_request(alarm_id, "activate", self._activation(alarm))
        else:
            await self._alarm_request(alarm_id, "cancel")
        self._skipped_rings.pop(alarm_id, None)

    @heal_stale_session
    async def skip_alarms(self, serial: str, day: str) -> int:
        """Skip every ring on `day` and return how many were skipped.

        A repeating alarm skips just that ring (Amazon's nextOccurrence/cancel)
        and stays on; a one-time alarm is turned off. Amazon can only skip an
        alarm's *next* ring, so an alarm that rings again later today can't skip
        tomorrow's yet. Those are reported instead of skipping the wrong ring.

        Skipping is remembered per alarm, because asking Amazon twice would skip
        the ring after the one already skipped: a Flow that runs twice would
        silence the next working day's alarm too.
        """
        start, end = self._day_window(day)
        now = self._local_now()
        skipped, too_early = 0, []
        for alarm in await self._alarms(serial):
            alarm_id = alarm["notificationIndex"]
            rules = self._alarm_rules(alarm)
            rings = _rings(rules, start, end)
            if not _alarm_on(alarm) or not rings:
                continue
            name = f"{rings[0]:%H:%M} {_alarm_label(alarm)}".strip()
            already = self._skipped_rings.get(alarm_id)
            if already == rings[0]:
                self._log(f"alarm {alarm_id}: the {rings[0]:%H:%M} ring {day} is already skipped")
                continue
            upcoming = _next_ring(rules, now)
            if upcoming is not None and upcoming == already:
                upcoming = _next_ring(rules, already + timedelta(minutes=1))
            if upcoming is not None and upcoming < rings[0]:
                too_early.append(name)
                continue
            await self._alarm_request(
                alarm_id, "nextOccurrence/cancel" if _alarm_repeats(alarm) else "cancel"
            )
            self._skipped_rings[alarm_id] = rings[0]
            skipped += 1
        self._log(f"alarms: skipped {skipped} ring(s) {day} on {serial}")
        if too_early:
            raise RuntimeError(
                f"Alexa can only skip an alarm's next ring, and {', '.join(too_early)} "
                "rings again later today. Skip tomorrow's ring after that one."
            )
        return skipped

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
            if not is_directed_customer_id(own_id):
                # Comparing a directed deviceOwnerCustomerId against an
                # obfuscated account id would report every device as somebody
                # else's — say nothing rather than something false.
                self._log(
                    f"devices: {len(devices)} total "
                    "— ownership unknown (no directed customer id yet)"
                )
                return
            owned = sum(1 for d in devices if d.device_owner_customer_id == own_id)
            # Distinct *foreign* owners, not AmazonDevice.household_device: that
            # flag is `owner == account_customer_id`, i.e. exactly `owned` again,
            # so printing it as a third figure said the same thing twice under a
            # name that implied the opposite. How many other accounts are in play
            # is the thing that actually distinguishes a Household from a plain
            # wrong-id (report f6a1b2a6).
            others = {
                d.device_owner_customer_id
                for d in devices
                if d.device_owner_customer_id != own_id
            }
            self._log(
                f"devices: {len(devices)} total — {owned} owned by the signed-in account, "
                f"{len(devices) - owned} owned by {len(others)} other account(s)"
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

    async def list_music_providers(self) -> list[dict]:
        """Music services linked to the account, the default one first.

        The library fetches these once per session and checks the card's choice
        against the same list, so a service linked later shows up after the app
        reconnects.
        """
        if self._api is None:
            return []
        providers = sorted(
            (await self._api.music_providers).values(),
            key=lambda p: (not p.default_provider, p.provider_name.lower()),
        )
        return [{"id": p.provider_id, "name": p.provider_name} for p in providers]

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