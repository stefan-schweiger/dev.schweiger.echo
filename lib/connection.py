"""Connection state + error categorization over aioamazondevices exceptions."""

from enum import Enum

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
