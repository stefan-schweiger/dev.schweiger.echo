"""Connection state + error categorization over aioamazondevices exceptions."""

import errno
import re
import socket
from enum import Enum
from typing import Optional

from aioamazondevices.exceptions import (
    CannotAuthenticate,
    CannotConnect,
    CannotRegisterDevice,
    CannotRetrieveData,
    MissingCountryError,
)

# aioamazondevices 16.8.1 refuses an account whose Amazon profile has no country
# of residence (MissingCountryError, which carries no message). Without one
# everything falls back to amazon.com, and in the report behind the check every
# device showed offline (home-assistant/core#186163). Only the account owner can
# fix it, and signing in again won't help until they do.
MISSING_COUNTRY_MESSAGE = (
    "Your Amazon account has no country/region set, so Amazon cannot tell which "
    "Alexa service it belongs to. Set it on the Amazon website under Manage Your "
    "Content and Devices → Preferences → Country/Region Settings, then sign in again."
)


class ConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    ERROR = "error"


def unresolved_host(e: BaseException) -> Optional[str]:
    """Hostname from a DNS-resolution failure inside the cause chain, if any.

    The library funnels every transport problem into
    `CannotConnect("Connection error during GET")`, which tells a user nothing:
    "Amazon is down" and "this network cannot resolve alexa.amazon.fr" read the
    same, and the second one has people re-entering credentials for hours. The
    detail is one level down — aiohttp raises
    `ClientConnectorDNSError(connection_key, exc) from exc`, so the chain holds
    the failing host and, beneath it, the `socket.gaierror`. Matching on
    gaierror rather than importing aiohttp's exception keeps this working across
    aiohttp versions (ClientConnectorDNSError only exists in 3.11+).
    """
    host: Optional[str] = None
    current: Optional[BaseException] = e
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        host = host or getattr(current, "host", None)
        if isinstance(current, socket.gaierror) or isinstance(
            getattr(current, "os_error", None), socket.gaierror
        ):
            return host
        current = current.__cause__ or current.__context__
    return None


def unresolved_host_message(e: BaseException) -> Optional[str]:
    """Plain-language reason when a request died in DNS, else None.

    Shared so every surface tells the same story: the state the heartbeat sets,
    app settings after a failed sign-in, and a Flow card that just refused to
    run. The library funnels all of them into `CannotConnect("Connection error
    during GET")`, and two testers spent a week suspecting their Amazon password
    while their network was simply returning no address.
    """
    host = unresolved_host(e)
    if host is None:
        return None
    return (
        f"Cannot resolve {host} — your network returns no address for it. "
        "Check your router's DNS server and any ad/tracker filtering."
    )


# connect() failures that mean something on the way refused this address, as
# opposed to a slow or flaky network. The app forces IPv4 (Homey has no IPv6
# route), so ENETUNREACH here is about an IPv4 address and not that.
_REFUSED_CONNECT = {
    errno.ENETUNREACH: "network unreachable",
    errno.EHOSTUNREACH: "no route to host",
    errno.ECONNREFUSED: "connection refused",
}

# "Connect call failed ('18.244.135.178', 443)" — asyncio puts the address in
# the message and nowhere else.
_CONNECT_ADDRESS = re.compile(r"\('([0-9a-fA-F.:]+)',\s*\d+")


def unreachable_host(
    e: BaseException,
) -> Optional[tuple[Optional[str], Optional[str], str]]:
    """(host, address, reason) when a connect() was refused, else None.

    Host and address are None when the chain doesn't carry them.

    Same chain walk as unresolved_host: aiohttp raises
    `ClientConnectorError(connection_key, os_error)`, which carries the host,
    and the OSError beneath it carries the errno and the address.
    """
    host: Optional[str] = None
    current: Optional[BaseException] = e
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        host = host or getattr(current, "host", None)
        for candidate in (current, getattr(current, "os_error", None)):
            if isinstance(candidate, OSError) and candidate.errno in _REFUSED_CONNECT:
                match = _CONNECT_ADDRESS.search(str(candidate))
                return (
                    host,
                    match.group(1) if match else None,
                    _REFUSED_CONNECT[candidate.errno],
                )
        current = current.__cause__ or current.__context__
    return None


def unreachable_host_message(e: BaseException) -> Optional[str]:
    """Plain-language reason when a connection to a resolved address was refused.

    Support log 727774ab: sign-in completed, then every request to
    alexa.amazon.co.uk died within ~20 ms with `OSError(101, "Connect call
    failed ('18.244.135.178', 443)")` while www.amazon.com and api.amazon.com
    connected from the same Homey. The user saw only `CannotConnect: Connection
    error during GET`, which reads like the app or Amazon is broken.
    """
    found = unreachable_host(e)
    if found is None:
        return None
    host, address, reason = found
    host = host or "Amazon"
    where = f"{host} ({address})" if address else host
    return (
        f"Cannot connect to {where}: {reason}. Its address was found, so something "
        "between this Homey and Amazon — a router or firewall rule, or a VPN — is "
        "blocking it."
    )


def network_failure_message(e: BaseException) -> Optional[str]:
    """The DNS or blocked-address explanation, whichever applies, else None."""
    return unresolved_host_message(e) or unreachable_host_message(e)


def explained(handler):
    """Wrap a Flow card run listener so a DNS failure says so on the card.

    Homey shows whatever a run listener raises, and everything that fails in
    transport arrives as `CannotConnect: Connection error during GET`. That text
    has cost real users days: it reads like the app is broken when the network
    never returned an address for alexa.amazon.<tld>. Only that one case is
    rewritten; anything else keeps its original exception so the app log and a
    diagnostic report still carry the exact type.

    Lives here rather than in a driver because both device cards and app-level
    cards need it.
    """

    async def run(args, **kwargs):
        try:
            return await handler(args, **kwargs)
        except Exception as e:
            explanation = network_failure_message(e)
            if explanation is None:
                raise
            raise RuntimeError(explanation) from e

    return run


def categorize_error(e: Exception) -> dict:
    """Map a library exception to a category + how the app should react.

    Returns keys: category, message, should_retry, needs_reauth.
    """
    if isinstance(e, (CannotAuthenticate, CannotRegisterDevice)):
        return {
            "category": "auth",
            "should_retry": False,
            "needs_reauth": True,
            "message": "Authentication expired — please re-authenticate in app settings",
        }
    if isinstance(e, CannotConnect):
        explanation = network_failure_message(e)
        if explanation:
            return {
                "category": "network",
                "should_retry": True,
                "needs_reauth": False,
                "message": explanation,
            }
        return {
            "category": "network",
            "should_retry": True,
            "needs_reauth": False,
            "message": "Cannot reach Amazon — will retry",
        }
    if isinstance(e, MissingCountryError):
        # Not needs_reauth: that would throw away the stored login, and the fix
        # is on Amazon's side.
        return {
            "category": "account",
            "should_retry": False,
            "needs_reauth": False,
            "message": MISSING_COUNTRY_MESSAGE,
        }
    if isinstance(e, CannotRetrieveData):
        return {
            "category": "transient",
            "should_retry": True,
            "needs_reauth": False,
            "message": "Transient error from Amazon — will retry",
        }
    return {
        "category": "unknown",
        "should_retry": False,
        "needs_reauth": False,
        # Several library exceptions carry no message (WrongMethod, for one);
        # their type is still more use in a report than "Unknown error".
        "message": str(e) or type(e).__name__,
    }
