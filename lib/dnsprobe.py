"""One-shot DNS diagnostics, run only after a lookup has already failed.

Two French testers spent weeks on `gaierror(-5, 'No address associated with
hostname')` for `alexa.amazon.<tld>` while `api.amazon.com` resolved fine in the
same second. Every test either ran on a different machine (nslookup from a PC) or
took a different path (one explicit server instead of the system list), so no
measurement ever compared two paths on the same machine at the same moment. This
does that, and it is the only thing here that is genuinely new information.

Diagnostics only. Nothing in this module resolves anything the app then connects
to — the app keeps using the system resolver, on purpose (see AGENTS.md). It runs
behind the debug-logging setting, only once a request has already failed in DNS,
and throttled by the caller, so it costs nothing in the normal path.

Deliberately derives the CNAME chain at runtime rather than hardcoding it. All
European marketplaces currently share `layla.amazon.com` ->
`tp.799c43337-frontier.amazon.com` -> `d3rsqup3tcxj1a.cloudfront.net`, stable
across five days of measurements, but that frontier id is an Amazon internal
identifier. If they redeploy it, a hardcoded probe would ask for a name that no
longer exists, get NXDOMAIN, and report it as the very failure it is meant to be
diagnosing.
"""

import asyncio
import socket
import ssl
from typing import Any, Callable, Optional

import aiohttp
import certifi

# Cloudflare and Google both answer DNS-over-HTTPS on their own addresses with a
# certificate that covers the bare IP, so reaching these needs no name resolution
# and never touches port 53 — which is the whole point of asking them.
DOH_ENDPOINTS = ("https://1.1.1.1/dns-query", "https://8.8.8.8/dns-query")
DOH_TIMEOUT_S = 5

# Flat A records, no aliases, and the one name that has never failed in any
# report so far. Resolved alongside the failing name so a report says whether
# resolution was broken at that moment or only broken for that name.
CONTROL_HOST = "api.amazon.com"

# Sign-in goes to the retail host, not the Alexa one, and that has been seen
# failing too (log 146f2623: `www.amazon.com` NODATA while a probe 48s later
# resolved everything). Probed alongside so one run covers both paths.
def _retail_sibling(host: str) -> Optional[str]:
    """"alexa.amazon.fr" -> "www.amazon.fr"; None if the name isn't an Alexa host."""
    if not host.startswith("alexa."):
        return None
    return "www." + host[len("alexa.") :]

# The failing name is asked for repeatedly because the failure is sticky: one
# tester's on-device run resolved it six times and then failed every attempt
# after that, so a single result cannot tell "flickering" from "now stuck".
HOST_ATTEMPTS = 3

RESOLV_CONF = "/etc/resolv.conf"

_DNS_TYPE_A = 1
_DNS_TYPE_CNAME = 5


def _resolv_conf_lines() -> list[str]:
    """What Homey's resolver is actually configured with.

    We have never seen this. It decides how many of the DHCP-supplied servers
    survive (glibc keeps at most three), in what order they are tried, and
    whether options like `rotate` or `single-request` are in play. One tester had
    five servers configured, two of which the resolver would have discarded, and
    spent a session testing combinations that were never consulted.
    """
    try:
        with open(RESOLV_CONF, encoding="utf-8", errors="replace") as fh:
            entries = [
                line.strip()
                for line in fh
                if line.strip() and not line.lstrip().startswith("#")
            ]
    except OSError as e:
        return [f"  {RESOLV_CONF}: unreadable ({e})"]
    if not entries:
        return [f"  {RESOLV_CONF}: empty"]
    return [f"  {RESOLV_CONF}: {'; '.join(entries)}"]


async def _doh_lookup(host: str) -> tuple[list[str], list[str], Optional[str]]:
    """Ask a DoH endpoint by bare IP. Returns (cnames, addresses, error)."""
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    last_error: Optional[str] = None
    for endpoint in DOH_ENDPOINTS:
        # A fresh connector each time: this must not share state with the
        # session whose lookups are failing, or the result proves nothing.
        connector = aiohttp.TCPConnector(family=socket.AF_INET, ssl=ssl_context)
        try:
            async with aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=DOH_TIMEOUT_S),
            ) as session:
                async with session.get(
                    endpoint,
                    params={"name": host, "type": "A"},
                    headers={"accept": "application/dns-json"},
                ) as resp:
                    if resp.status != 200:
                        last_error = f"{endpoint} returned HTTP {resp.status}"
                        continue
                    payload: Any = await resp.json(content_type=None)
        except Exception as e:  # noqa: BLE001 — a probe must never raise
            last_error = f"{endpoint}: {type(e).__name__}: {e}"
            continue

        answers = payload.get("Answer") or []
        cnames = [
            str(a.get("data", "")).rstrip(".")
            for a in answers
            if a.get("type") == _DNS_TYPE_CNAME
        ]
        addresses = [
            str(a.get("data", "")) for a in answers if a.get("type") == _DNS_TYPE_A
        ]
        return cnames, addresses, None
    return [], [], last_error or "no DoH endpoint answered"


async def _lookup(name: str, family: int, label: str) -> str:
    """One getaddrinfo, timed, reporting the EAI code rather than just a message.

    The code is the whole point: EAI_NONAME (-2) means the name does not exist,
    EAI_AGAIN (-3) means nobody answered, and EAI_NODATA (-5) means something
    answered and said the name has no address of this family. The reports we have
    show -5, so the distinction decides what is worth investigating next.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()

    def resolve() -> list:
        return socket.getaddrinfo(name, 443, family=family, type=socket.SOCK_STREAM)

    try:
        infos = await loop.run_in_executor(None, resolve)
    except OSError as e:
        ms = (loop.time() - started) * 1000
        return f"{name} {label}: FAILED errno={e.errno} {e.strerror or e} in {ms:.0f}ms"
    ms = (loop.time() - started) * 1000
    addresses = sorted({info[4][0] for info in infos})
    return f"{name} {label}: {', '.join(addresses)} in {ms:.0f}ms"


async def _aliases(name: str) -> str:
    """The alias chain as the system resolver reports it, when it resolves at all.

    getaddrinfo never exposes the chain, and gethostbyname_ex raises rather than
    returning a partial answer, so this only ever succeeds. That still makes it
    worth having: it shows the chain the resolver believes in, which we otherwise
    only know from an out-of-band source.
    """
    loop = asyncio.get_running_loop()
    try:
        canonical, alias_list, addresses = await loop.run_in_executor(
            None, socket.gethostbyname_ex, name
        )
    except OSError as e:
        return f"{name} chain: unavailable (errno={e.errno} {e.strerror or e})"
    chain = " -> ".join([*alias_list, canonical]) or canonical
    return f"{name} chain: {chain} -> {', '.join(addresses)}"


async def run(
    host: str,
    log: Callable[[str], None],
    chain_hint: Optional[list[str]] = None,
) -> Optional[list[str]]:
    """Probe `host`, log a single report, and return the chain we learned.

    The caller persists the returned chain and passes it back as `chain_hint`, so
    a run where DoH itself is blocked still has real names to walk instead of
    inventing them.

    Never raises: a diagnostic that breaks the thing it is diagnosing is worse
    than no diagnostic.
    """
    lines = [f"DNS probe for {host}"]
    chain: list[str] = []
    try:
        lines += _resolv_conf_lines()

        cnames, addresses, doh_error = await _doh_lookup(host)
        if doh_error is None:
            chain = cnames
            lines.append(
                f"  DoH: {' -> '.join([host, *cnames])} -> "
                f"{', '.join(addresses) or 'NO A RECORD'}"
            )
        else:
            lines.append(f"  DoH: FAILED ({doh_error})")
        # Fall back whenever we came away without a chain, not only when DoH
        # errored: an answer carrying no aliases is itself news worth walking a
        # known-good chain against.
        if not chain:
            # Settings come back as whatever JSON was stored, so don't trust the
            # shape — a malformed hint must not take the probe down.
            if isinstance(chain_hint, list):
                chain_hint = [n for n in chain_hint if isinstance(n, str) and n]
            else:
                chain_hint = None
            if chain_hint:
                chain = list(chain_hint)
                lines.append(f"  chain: falling back to last known {' -> '.join(chain)}")
            else:
                lines.append("  chain: unknown, probing the failing name only")

        # The failing name across all three families. If the IPv4-only lookup
        # fails where the combined one succeeds, that is a different bug from the
        # one we are chasing and it would be ours, since the app forces IPv4.
        for _ in range(HOST_ATTEMPTS):
            lines.append("  " + await _lookup(host, socket.AF_INET, "A"))
        lines.append("  " + await _lookup(host, socket.AF_INET6, "AAAA"))
        lines.append("  " + await _lookup(host, 0, "any"))

        # Each hop of the chain on its own. Where it stops is the answer: a leaf
        # that resolves under a top that does not means the resolver is failing
        # to follow the aliases.
        for name in chain:
            if name != host:
                lines.append("  " + await _lookup(name, socket.AF_INET, "A"))

        retail = _retail_sibling(host)
        if retail:
            lines.append("  " + await _lookup(retail, socket.AF_INET, "A"))

        lines.append("  " + await _lookup(CONTROL_HOST, socket.AF_INET, "A"))
        lines.append("  " + await _aliases(CONTROL_HOST))
    except Exception as e:  # noqa: BLE001
        lines.append(f"  probe aborted: {type(e).__name__}: {e}")

    log("\n".join(lines))
    return chain or None
