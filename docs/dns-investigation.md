# DNS resolution failures on some French networks

Reference notes for an investigation that ran from 2026-08-20 to 2026-08-27 and is
**not closed**. Written down because most of the effort went into ruling things
out, and that work is worthless if it has to be repeated.

Throughout, **measured** means someone ran the command and pasted the output.
**Inferred** means it follows from the measurements but has not been observed
directly. Several confident-sounding theories in this thread turned out to be
inference presented as fact, so the distinction is kept explicit.

---

## 1. The symptom

Two users cannot use the app reliably. Both are in France on Free (Freebox).

- Sign-in fails with `CannotConnect: Connection error during GET`, or
- the app connects and reports itself connected, but every Flow card fails, or
- the routine picker is empty.

Underneath, always the same thing:

```
ClientConnectorDNSError(ConnectionKey(host='alexa.amazon.nl', port=443, …),
                        gaierror(-5, 'No address associated with hostname'))
```

Switching the Amazon server setting buys a few hours to a day, then the new host
starts failing the same way. Restarting the app does **not** restore control;
reconnecting against a different regional host does.

## 2. The decisive measurement

Log `66cf03a7` (thierry_arguimbau, 2026-08-26). Three heartbeats, each pair of
requests inside the same second:

| Time | Request | Result |
|---|---|---|
| 14:31:02 | `alexa.amazon.nl/…/allDeviceVolumes` | **200** |
| 14:36:01 | `api.amazon.com/auth/token` | **200** |
| 14:36:02 | `alexa.amazon.nl/…/allDeviceVolumes` | **gaierror(-5)** |
| 14:41:01 | `api.amazon.com/auth/token` | **200** |
| 14:41:02 | `alexa.amazon.nl/…/allDeviceVolumes` | **gaierror(-5)** |
| 14:46:01 | `api.amazon.com/auth/token` | **200** |
| 14:46:02 | `alexa.amazon.nl/…/allDeviceVolumes` | **gaierror(-5)** |

`api.amazon.com` never failed. `alexa.amazon.<tld>` never succeeded. HTTP/2 pings
returned 204 at 14:40 and 14:44 throughout, because that connection was already
open — which is the whole explanation for "the app says connected but nothing
works": sign-in and token refresh use `api.amazon.com`, every command uses
`alexa.amazon.<tld>`, and only one of the two names stops resolving.

It also worked at 14:31:02 and failed at 14:36:02 with nothing changed but the
clock. Our 120 s DNS cache covered the first; the 5-minute heartbeat outran it.

## 3. Error code taxonomy

This decides what is worth investigating, and it was misread more than once.

| glibc code | Value | Meaning |
|---|---|---|
| `EAI_NONAME` | -2 | Name does not exist (NXDOMAIN), or has no record of that family |
| `EAI_AGAIN` | -3 | Timeout or SERVFAIL — nobody answered |
| `EAI_NODATA` | -5 | Something answered, and the answer carried no address |

The affected users get **-5**, consistently. So this is not packet loss, not a
dead resolver, and not a blocked name. Something replied.

Two traps:

- Node (HomeyScript, undici) flattens both -2 and -5 to `ENOTFOUND`, so on-device
  `fetch` tests cannot distinguish them.
- A **healthy** Homey returns **-2** for "no record of this family" — measured:
  `alexa.amazon.de AAAA: FAILED errno=-2`. So -5 on the A lookup is not simply
  the same condition. Unexplained. See §8.

The Python runtime is glibc, not musl: the stderr path in every log is
`cpython-3.14-linux-aarch64-gnu`.

## 4. Amazon's DNS topology (measured 2026-08-27)

```
alexa.amazon.fr / .de / .nl / .co.uk   (identical chain)
  alexa.amazon.<tld>                 900  CNAME  layla.amazon.com
  layla.amazon.com                  1800  CNAME  tp.799c43337-frontier.amazon.com
  tp.799c43337-frontier.amazon.com    60  CNAME  d3rsqup3tcxj1a.cloudfront.net
  d3rsqup3tcxj1a.cloudfront.net       60  A      99.84.94.61      (single address)

alexa.amazon.com  (Americas)
  pitangui.amazon.com → tp.5fd53c725-frontier.amazon.com → d1wg1w6p5q8555.cloudfront.net

api.amazon.com                        35  A      8 flat records, no CNAME, no AAAA
```

Facts that matter:

- **Every European marketplace shares one chain.** Switching between `.fr`, `.de`,
  `.nl` and `.co.uk` changes nothing about the DNS shape, which is why cycling
  servers only ever buys hours.
- **The chain names are stable.** SergeP's `nslookup` on 2026-08-22 and a `dig` on
  2026-08-27 returned the identical `tp.799c43337-frontier` and
  `d3rsqup3tcxj1a.cloudfront.net`. Only the terminal address rotates.
  Do **not** hardcode them anyway — the frontier id is an Amazon internal
  identifier, and a stale hardcoded name returns NXDOMAIN, which looks exactly
  like the failure being diagnosed.
- **`www.amazon.<tld>` is also a frontier chain**, measured 2026-08-27:
  `www.amazon.com → tp.47cf2c8c9-frontier.amazon.com → cf.47cf2c8c9-frontier.amazon.com → A`.
  It has been seen failing the same way (§5a), which matters because a fresh
  interactive sign-in goes there, not to an Alexa host. So **every name observed
  failing is a `*-frontier` CNAME chain, and the only name never observed failing
  is the flat one** (`api.amazon.com`). That is the strongest structural signal in
  the investigation.
- **The AAAA answer for the chain is CNAMEs with no terminal AAAA**, i.e. a
  NODATA-shaped response. `api.amazon.com` has no AAAA either.
- **Answer sizes: 171 bytes (`api.amazon.com`) and 169 bytes (`alexa.amazon.nl`).**
  Nowhere near the 512-byte UDP limit, so truncation is not involved. An earlier
  measurement of `alexa.amazon.fr` gave 158 bytes.

## 5. Homey resolves through a local daemon

Measured on a **healthy** Homey (2026-08-27, via the in-app probe):

```
/etc/resolv.conf: search localdomain; nameserver 127.0.0.1; nameserver 1.1.1.1;
                  nameserver 8.8.8.8; nameserver 8.8.4.4; nameserver 9.9.9.9;
                  options edns0 trust-ad

alexa.amazon.de A: 13.33.55.234 in 14ms
alexa.amazon.de A: 13.33.55.234 in 2ms
alexa.amazon.de A: 13.33.55.234 in 1ms
```

What this proves:

- **A DNS listener on the Homey's own loopback is queried first.** Every app query
  goes there before anything leaves the device. This component is invisible to
  every `nslookup` run from a PC, which is why those tests kept coming back clean.
- **The router is not in the list at all.** No `192.168.x.x`, even though
  `Homey.system.getInfo()` on the affected device reports
  `1.1.1.1 192.168.0.254 1.0.0.1 192.168.0.254 8.8.8.8`. The two disagree.
- **Only the first three are used.** glibc caps at `MAXNS` = 3 (`man 5
  resolv.conf`), so entries four and five are decoration. One affected user spent
  a session testing combinations of five servers, some of which were never
  consulted.
- **Something on the device caches.** glibc has no cache of its own, so 1–2 ms
  repeats come from the loopback listener.

Not established: what that listener is (dnsmasq, systemd-resolved, something
Homey-specific), where it forwards, whether the cache can be flushed without a
reboot, and whether the affected Homeys have the same `resolv.conf`. These were
put to Athom; no answer recorded here yet.

## 5a. The probe on an affected device (log `146f2623`, 2026-08-27)

The comparison §11 called "the one test never run" — partially run, with a flaw.

```
15:50:01  auto-connect      alexa.amazon.fr   gaierror(-5)
15:50:43  interactive login www.amazon.com    gaierror(-5)   (failed in 19 ms)
15:51:31  DNS probe         alexa.amazon.com  resolved, 37 ms cold / 2 ms warm
                            every chain hop   1 ms
                            api.amazon.com    17 ms
```

What it establishes:

- **`www.amazon.com` fails too.** The failure is not confined to Alexa hosts. A
  fresh sign-in has no stored login data, so the library starts on `DEFAULT_SITE`
  (`www.amazon.com`) — meaning the "pick another Amazon server" workaround cannot
  help a first-time login at all.
- **`resolv.conf` on an affected device is the same as on a healthy one** —
  `127.0.0.1` first, then `1.1.1.1`, `8.8.8.8`, `8.8.4.4`, `9.9.9.9`,
  `options edns0 trust-ad`, and no router. So that list is Homey's own standard
  configuration, not something these users set, and §5's findings generalise.
- **It recovered within 48 seconds** and behaved normally afterwards: 37 ms cold,
  2 ms warm, matching a healthy device. So this is intermittent, not a lasting
  poisoning.
- **The failing lookup took 19 ms**, again in the cache band rather than the
  network band (§6).
- **AAAA returns -2 here too**, same as a healthy device, so the -2/-5 split in §3
  is not a property of the affected network.

**The flaw:** the probe defaulted to `alexa.amazon.com` while the names that had
actually failed were `alexa.amazon.fr` and `www.amazon.com`, which are *different
frontier chains*. It resolved the name nobody was complaining about.

Fixed afterwards. The manual probe now resolves the host the app would really use
— live session, else pinned server, else the site stored with the last login, else
Amazon's default — and additionally probes its retail sibling
(`alexa.amazon.fr` → `www.amazon.fr`), which is where sign-in goes. One run covers
the command path and the sign-in path without guessing which of them broke. Note
the stored-login fallback matters here specifically: after a failed sign-in there
is no session and no pin, and defaulting to `amazon.com` probes a different
marketplace than the one that failed.

So the decisive question is still open: at a moment when `alexa.amazon.fr` is
failing, does DoH return an address for **that name** while the system resolver
refuses one?

## 6. Cache-speed vs network-speed

| Measurement | Time |
|---|---|
| Healthy Homey, cold lookup | 14 ms |
| Healthy Homey, cached repeat | 1–2 ms |
| **Affected Homey, failing lookup** | **6, 7, 22 ms** |

The failures land in the cached band, not the network band. **Inferred:** a local
cache is serving a negative or empty answer. This is the strongest remaining lead
and it accounts for things nothing else does — sticky until reconnect, unaffected
by restarting the app, cured temporarily by switching to a name that is not
poisoned yet, and needing a burst to start.

## 7. The burst dependence

SergeP, HomeyScript `fetch`, five seconds apart (forum post 320):

```
alexa.amazon.fr try 1-6:  reached, HTTP 302
alexa.amazon.fr try 7-10: DNS FAILED (getaddrinfo ENOTFOUND)
alexa.amazon.de try 1-10: DNS FAILED
```

Six clean lookups, then persistent failure, then a *different* hostname failing
from its first attempt. Not random loss, and not per-name: something entered a bad
state after roughly six queries and stayed there. Nobody has explained this.

It also proves the app is not implicated — HomeyScript shares only the system
resolver and the network with it.

## 8. Hypotheses raised and ruled out

| Hypothesis | Verdict |
|---|---|
| CloudFront address rotation stranding the app | **No.** The app never stores an address; it resolves per request. |
| 512-byte UDP truncation | **No.** Answers measured at 158–171 bytes. |
| All marketplaces land on one CloudFront distribution, so pinning cannot help | **No.** `dig` shows three separate chains (Europe / Americas / FE). |
| Stale session or CSRF token after the 6-hourly cookie refresh | **No.** The library's `clear_cookies()` already calls `clear_csrf_cookie()`, and our refresh mirrors upstream line for line. Killed independently by the user reporting that restarting the app does *not* restore control. |
| `api.amazon.com` is immune on affected networks | **Unproven.** It has never failed in a log, but that is a handful of data points, and the burst model predicts position in a sequence, not immunity. |
| A cache serving the CNAME chain without its terminal A record | **Not supported.** SergeP's full `nslookup` output against both `192.168.0.1` and `1.1.1.1` shows complete answers, terminal address present, every time. |
| Homey's resolver is broken | **No.** It works for effectively every other user, including one on Pi-hole + Unbound with blocking enabled who can reach `layla.amazon.com`, `alexa.amazon.fr` and `alexa.amazon.de` (post 321). Homey is the constant; the network is the variable. It takes both. |
| Two active interfaces on one subnet | **Real misconfiguration, not the cause.** The affected device has ethernet up at `192.168.0.47` and wifi associated at the same mask. That causes genuine flakiness, but random path loss should hit both hostnames, and it does not. |
| The app makes unaccounted lookups | **True and fixed.** The library fetched seven days of voice history on every `EqualizerStateChange` push — a request to `alexa.amazon.<tld>` fired whenever anyone spoke to an Echo, for data nothing consumed. Suppressed in 2.1.6. |

### The elimination trap

Each layer has an alibi: not the app (fails in HomeyScript), not Homey (works for
everyone else), not the router (`nslookup` is clean), not the ISP (300+ French
installs, Free is huge). Every one of those rules out its layer as a *sufficient,
independent* cause. None rules it out as a *contributing* one. A failure needing
two or three conditions at once produces exactly this pattern, and two users out
of three hundred is what a conjunction looks like. Elimination cannot resolve this;
only the signature can.

## 9. What shipped

**2.1.4**
- `DNS_CACHE_TTL_S = 120` on the aiohttp connector, up from aiohttp's 10 s default.
  Login touches five hostnames and the routine picker several more; the default
  turned each into a burst.
- One `TCPConnector` for the app's lifetime, so its DNS cache survives a session
  rebuild and a retried login does not start from scratch.
- `unresolved_host_message()` — every surface now names the address that failed
  instead of showing `Connection error during GET`. Two testers had spent a week
  suspecting their Amazon password.

**2.1.5**
- `@heal_stale_session` — retries a command once after a session refresh when
  Amazon answers 401/403/407. Built for the "works 12 hours then stops" report,
  which turned out to be something else; kept because it is cheap and correct.
- CSRF token transition logging.

**2.1.6**
- `lib/dnsprobe.py`, `POST /dns-probe`, and a **Run network test** button. Reads
  `/etc/resolv.conf`, asks `https://1.1.1.1/dns-query` by bare IP for the chain,
  then walks every hop plus `api.amazon.com` through `getaddrinfo`, timed, with
  the EAI code. Point of the design: no test in the whole thread ever compared two
  resolution paths on the same machine at the same moment.
- Suppressed the unused voice-history fetch (see §8).

### Known limitation

`SYNC_INTERVAL_MS` is 5 minutes and `DNS_CACHE_TTL_S` is 120 s, so the TTL bump
can never help the steady state — only bursts inside a single login or picker
session. Visible directly in log `66cf03a7`: worked at 14:31:02, failed at
14:36:02.

## 10. Rejected: resolving DNS ourselves

A custom `aiohttp` `AbstractResolver` doing DNS-over-HTTPS against
`https://1.1.1.1/dns-query` was designed and **verified to work**:

- answers when addressed by bare IP, so it needs no bootstrap lookup
- the certificate validates **against certifi**, confirmed on-device by the probe,
  not just against a system trust store
- never touches port 53, so it bypasses the local listener entirely
- roughly forty lines, pure Python, no new dependencies

It was **rejected deliberately** (2026-08-26): network-layer plumbing maintained
forever inside a Homey app, for a failure the app can prove is not its own,
benefiting two users. If it is ever reconsidered it belongs behind an opt-in
setting next to the server picker, with a fallback to the system resolver.

Note that "the cause is outside the app" does not imply "the fix must be". DoH
would have worked *because* it removes the component that fails. That was not the
reason for saying no.

## 11. Open questions

1. Why does `api.amazon.com` resolve while `alexa.amazon.<tld>` does not, in the
   same second, repeatedly? The flat-vs-chain distinction is the only structural
   difference found, and no mechanism has been demonstrated.
2. What is listening on `127.0.0.1:53` on a Homey, does it cache negatives, and
   can it be flushed without a reboot?
3. Why -5 on the affected devices when a healthy one gives -2 for the
   no-record case?
4. Why does the failure need roughly six lookups to start, and why is it sticky
   afterwards?

### The one test never run

The probe from 2.1.6, taken on an affected Homey **while it is failing**.
Specifically: their `resolv.conf` line, whether the repeats come back at 1 ms
(cache) or 14 ms (network), whether the chain hops resolve when the top does not,
and whether DoH returns an address in the same run that the system resolver
refuses one. That last comparison separates "the answer is unreachable from this
device" from "only the normal path cannot get it", and nothing else in the thread
does.

## 12. References

- Forum thread: `community.homey.app/t/app-pro-amazon-echo-looking-for-testers-and-contributors/120365`,
  posts 316–334.
- Diagnostic reports: `de0cb987`, `e3f5e7ba`, `807dfcc4`, `66cf03a7`
  (thierry_arguimbau), `cb217590` (Jeremy_Gout), `146f2623` (SergeP, first probe
  from an affected device).
- Affected: thierry_arguimbau, Jeremy_Gout, SergeP — all France / Free / Freebox.
  Unaffected control: Drako74, UK, Pi-hole + Unbound with blocking on.
- `man 5 resolv.conf` for `MAXNS`.
- Amazon retired the public List Management REST API on 2024-07-01; unrelated to
  this, but noted because it comes up when reading the same endpoints.
