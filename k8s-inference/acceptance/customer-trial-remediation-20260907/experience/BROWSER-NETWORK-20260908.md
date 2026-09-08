# Browser transport event: test-host network changes

The r03 browser's two `ERR_NETWORK_CHANGED` reads are strongly associated with network
interface address notification on the machine running Chrome, not an observed
platform HTTP error. The same association holds for all three retained r02
browser errors. These errors remain in the original receipts; this diagnosis
does not turn their failed reads into HTTP successes or change r02's failed
scientific outcome.

## Observed evidence

Root read the local `systemd-networkd` journal with precise timestamps, limited
to each existing browser-error window. No host, browser or cluster configuration
was changed. Chrome reports version `149.0.7827.114`.

| Browser error UTC | Local interface gained IPv6 link-local address UTC | Interface | Error follows notification |
| --- | --- | --- | ---: |
| Sep 7 20:11:54.990 | Sep 7 20:11:54.985379 | `vethba44c07` | 4.621 ms |
| Sep 7 20:13:05.773 | Sep 7 20:13:05.769340 | `br-e0dd06d940a4` | 3.660 ms |
| Sep 7 20:13:06.859 | Sep 7 20:13:06.857438 | `veth7548c14` | 1.562 ms |
| Sep 8 08:01:40.717 | Sep 8 08:01:40.713372 | `veth6ff58d7` | 3.628 ms |
| Sep 8 08:10:34.348 | Sep 8 08:10:34.345244 | `veth64556cf` | 2.756 ms |

All timestamps use UTC in 2026. The [selected journal receipt](browser-network-20260908.json)
records exact microsecond values, read-only command scopes and browser bindings.
The original r02 browser export remains unchanged.

For current Protenix operation `1001f32b-4ebf-4f1f-98bb-137a3ed30041`:

- The failed detail read was recorded at 08:01:40.717.
- The actual browser snapshot at 08:01:41.218 retained the application shell
  and a loading status, without a visible alert or blank body at that instant.
- The same operation's next automatic detail read succeeded at 08:01:42.121:
  recovery in 1.404 seconds, with no manual reload or browser restart.
- The page subsequently showed result finalization, automatically published
  nine validated artifacts, stopped polling, and passed the real download/hash
  check. The new restore duration was correctly displayed as 4.18 seconds.
- The observer's bounded control-plane log check found nearby HTTP 200 reads
  and no traceback, but did not provide a causal request-ID match. It is not
  represented as proof that the aborted read reached the application.

The second event affected the BindCraft detail read for operation
`65fe6dbd-3982-48cc-9454-bf7174292272`. Its next automatic read succeeded at
08:10:35.756, recovering in 1.408 seconds without reload or intervention.
There is no during-retry screenshot for this second event; do not extend the
first event's single-frame UI proof to every frame of the second.

## Source-grounded interpretation

Tavily located Chromium's primary source. The exact installed-version source
was then read directly: its Linux address tracker subscribes to IPv4, IPv6 and
link notifications and classifies newly added addresses. The notifier forwards
address changes to its observers. See the version-pinned
[address tracker](https://chromium.googlesource.com/chromium/src/+/refs/tags/149.0.7827.114/net/base/address_tracker_linux.cc#238)
and [Linux notifier](https://chromium.googlesource.com/chromium/src/+/refs/tags/149.0.7827.114/net/base/network_change_notifier_linux.cc#78).

The matching transport-pool implementation flushes connections with
`ERR_NETWORK_CHANGED` after an ordinary IP-address change; its optional exception
is specifically for randomized temporary IPv6 addresses, not a blanket exemption
for virtual interfaces. See the
[version-pinned connection handling](https://chromium.googlesource.com/chromium/src/+/refs/tags/149.0.7827.114/net/socket/transport_client_socket_pool.cc#1140).

Five independently timed interface notifications immediately preceding the five
browser errors, together with this implementation path, strongly support a
client-host network-change cause. This is an evidence-backed inference, not a
captured Chromium NetLog or packet-level causal trace. The owner or purpose of
the unrelated virtual interfaces was not investigated or changed.

## Acceptance treatment

Keep the aborted read, recovery latency and intermediate UI evidence visible.
Do not claim zero browser transport errors. The observed application recovered
automatically and its publication/download behavior passed; a full-cohort verdict
still depends on all scientific results, ordinary requests, resource release
and whole-window route evidence. No speculative production workaround or global
network/IPv6 setting change is warranted by this client-host evidence. A future
dedicated browser runner can isolate its network namespace from unrelated host
interface churn; that change was not introduced mid-cohort. After r03 closed,
the [isolated-network preflight](isolated-network-preflight-20260908.json) passed
with the same Chrome and browser script. It used a root-owned, inert network
namespace holder and a DNS bind only inside a private mount namespace; host DNS
was independently confirmed unchanged. This short preflight is not a substitute
for the next full-cohort browser runs.
