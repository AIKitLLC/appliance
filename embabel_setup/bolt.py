"""The host name a Bolt client on another machine dials, written into .env once.

WHY. The server tells every Bolt client where it is: a `neo4j://` driver is handed
that address as its routing table and reconnects to it, and About shows it as the URL
to paste into Neo4j Browser. Compose used to say `localhost`, which is right on the
appliance's own machine and wrong everywhere else — the URL About offered could only
ever be dialled from the machine it was already on. The ports are published beyond
loopback now (BOLT_BIND), so the name has to be one the rest of the network can use.

WHERE THE NAME COMES FROM, first that answers:
  1. BOLT_PUBLIC_HOST in .env — somebody's answer, never rewritten;
  2. the host of the mode's public base URL, when it names a real host: that is the
     name this appliance is already reached by;
  3. this machine's own name, when it resolves to an address other machines can use;
  4. localhost, said out loud, because nothing better could be found.

Written once, like a pin, so the URL in About does not move under the people who saved
it. A machine that changes its name, or moves behind a proxy, changes the line.
"""

from __future__ import annotations
import ipaddress
import socket
import sys
from urllib.parse import urlparse

from .settings import BOLT_HOST_VAR, bolt_public_host, env_file, env_file_assigns, env_file_value, set_env_var

# The public base URL each mode is reached by, when it is proxied or on another host.
PUBLIC_BASE_VAR = {"me": "ASSISTANT_PUBLIC_BASE_URL", "worlds": "WORLDS_PUBLIC_BASE_URL"}
LOCAL = "localhost"


def _reachable(host: str) -> bool:
    """Whether [host] names an address another machine could dial: it resolves, and
    not only to loopback. A name that resolves to nothing would put a routing table in
    every driver's hands that it cannot follow, which is worse than localhost."""
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except (OSError, UnicodeError):
        return False
    return any(not ipaddress.ip_address(address.split("%")[0]).is_loopback for address in addresses)


def _from_public_base(mode: str) -> str | None:
    url = env_file_value(PUBLIC_BASE_VAR.get(mode, "")) or ""
    host = urlparse(url).hostname
    return host if host and _reachable(host) else None


def _from_machine() -> str | None:
    """The machine's own name before its fully qualified one: getfqdn() falls back to a
    reverse lookup, which on a Mac is a name like `1.0…ip6.arpa` that nobody would type."""
    for host in (socket.gethostname(), socket.getfqdn()):
        if host and host != LOCAL and not host.endswith(".arpa") and _reachable(host):
            return host
    return None


def ensure_public_host(mode: str) -> str:
    """Write BOLT_PUBLIC_HOST if .env has no line for it, and return what it says."""
    if env_file_assigns(BOLT_HOST_VAR):
        return bolt_public_host()
    host = _from_public_base(mode) or _from_machine()
    if host:
        note = "the name other machines reach this appliance's Bolt ports by, found when it was written"
    else:
        host = LOCAL
        note = "no name other machines can reach this one by was found; put one here to use Bolt from elsewhere"
        # stderr: the Me app reads a verb's stdout as JSON.
        print(f"  Bolt is advertised as {LOCAL}: set {BOLT_HOST_VAR} in {env_file()} for other machines.",
              file=sys.stderr)
    set_env_var(BOLT_HOST_VAR, host, (f"# {BOLT_HOST_VAR}: {note}.",))
    return host
