"""Connection state + error categorization over aioamazondevices exceptions."""

import socket
from enum import Enum
from typing import Optional

from aioamazondevices.exceptions import (
    CannotAuthenticate,
    CannotConnect,
    CannotRegisterDevice,
    CannotRetrieveData,
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
        unresolved = unresolved_host_message(e)
        if unresolved:
            return {
                "category": "network",
                "should_retry": True,
                "needs_reauth": False,
                "message": unresolved,
            }
        return {
            "category": "network",
            "should_retry": True,
            "needs_reauth": False,
            "message": "Cannot reach Amazon — will retry",
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
        "message": str(e) or "Unknown error",
    }
