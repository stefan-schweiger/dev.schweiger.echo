"""Opt-in diagnostic logging.

`aioamazondevices` logs a lot of useful detail at DEBUG (login step-by-step, the
device list it fetches, domain/region switches, the customer-id lookup that fails
with "Cannot find account owner customer ID"), but those records go to a plain
Python logger that nothing in the Homey sandbox captures — so they never reach a
user's diagnostic report. This bridges that logger into Homey's app log so the
detail shows up in a report.

Off by default, and for two reasons: it's verbose, and the library emits a few
short-lived secrets at DEBUG (the bearer access token inside request headers, the
CSRF cookie value). We redact those here before anything leaves the app; the
library already scrubs its structured JSON dumps (see utils.scrub_fields).

Pinned to aioamazondevices==14.2.2 — re-check the redaction patterns on library
bumps against the sensitive DEBUG lines in http_wrapper.session_request / login.
"""

import logging
import re
from typing import Callable, Optional

# The library logs under logging.getLogger(__package__) == "aioamazondevices".
LIBRARY_LOGGER = "aioamazondevices"

# Short-lived secrets the library emits at DEBUG as raw strings (not via the
# scrub_fields JSON path). Each pattern keeps the label and masks the value.
_REDACTIONS = (
    # "Adding to headers: {'Authorization': 'Bearer <access token>'}"
    (re.compile(r"(Bearer\s+)[\w.\-+/=|~]+"), r"\1[REDACTED]"),
    # "CSRF cookie value: <value> [url]"
    (re.compile(r"(CSRF cookie value:\s*<)[^>]*(>)"), r"\1[REDACTED]\2"),
    # "Adding to headers: {'csrf': 'value'}"
    (re.compile(r"('csrf'\s*:\s*')[^']*(')"), r"\1[REDACTED]\2"),
    # "Processing vocal history record: {…'transcriptText': 'turn off the lights',
    # 'personFirstName': 'Stefan'…}" — what a household said to Alexa, and who
    # said it. AlexaService._skip_unused_history_fetch() stops the library
    # fetching this at all, so normally nothing here matches; this is the backstop
    # for a library bump that renames the method that patch hooks, because these
    # reports are something we ask users to send us.
    (re.compile(r"(Processing vocal history record:).*", re.DOTALL), r"\1 [REDACTED]"),
)


def _redact(message: str) -> str:
    for pattern, replacement in _REDACTIONS:
        message = pattern.sub(replacement, message)
    return message


class _HomeyLogHandler(logging.Handler):
    """Forwards log records to Homey's app log via a sink callback."""

    def __init__(self, sink: Callable[[str], None]) -> None:
        super().__init__(level=logging.DEBUG)
        self._sink = sink
        self.setFormatter(logging.Formatter("[%(name)s] %(levelname)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._sink(_redact(self.format(record)))
        except Exception:  # noqa: BLE001 - logging must never raise into callers
            pass


class DiagnosticLogging:
    """Attaches/detaches a Homey log handler on the library logger on demand."""

    def __init__(self, sink: Callable[[str], None]) -> None:
        self._sink = sink
        self._logger = logging.getLogger(LIBRARY_LOGGER)
        self._handler: Optional[_HomeyLogHandler] = None
        self._prev_level = self._logger.level
        self._prev_propagate = self._logger.propagate
        self.enabled = False

    def apply(self, enabled: bool) -> None:
        if enabled == self.enabled:
            return
        if enabled:
            self._prev_level = self._logger.level
            self._prev_propagate = self._logger.propagate
            self._handler = _HomeyLogHandler(self._sink)
            self._logger.addHandler(self._handler)
            self._logger.setLevel(logging.DEBUG)
            # Our handler is the only sink we want; don't also let records reach
            # the root logger (avoids duplicates if the sandbox captures stderr).
            self._logger.propagate = False
            self.enabled = True
            self._sink("diagnostic logging enabled")
        else:
            self._sink("diagnostic logging disabled")
            if self._handler is not None:
                self._logger.removeHandler(self._handler)
                self._handler = None
            self._logger.setLevel(self._prev_level)
            self._logger.propagate = self._prev_propagate
            self.enabled = False
