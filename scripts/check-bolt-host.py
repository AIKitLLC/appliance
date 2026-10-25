#!/usr/bin/env python3
"""The Bolt URL About shows can be dialled from another machine.

WHY THIS EXISTS. The server advertises one address for its Bolt ports, as the routing
table every `neo4j://` driver follows and as the URL About offers for Neo4j Browser.
Compose used to say `localhost` and publish on 127.0.0.1, so the one URL the appliance
handed out worked only on the machine it was already on. These are the cases that keep
it otherwise: both modes publish Bolt on BOLT_BIND and advertise BOLT_PUBLIC_HOST, and
bolt.ensure_public_host writes a name the network can use — the public base URL's host,
else the machine's own name, else localhost — and never rewrites a line that is there.

Nothing here resolves a real name: resolution is a patch.

    python3 scripts/check-bolt-host.py
"""
import contextlib
import io
import os
import sys
import tempfile
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from embabel_setup import bolt, settings  # noqa: E402

failures = []


def check(ok: bool, message: str) -> None:
    if not ok:
        failures.append(message)


def written(env_text, mode="worlds", *, reachable=(), hostname="box", fqdn="box.example"):
    """Run ensure_public_host against a scratch .env. Returns (host, the .env's BOLT_PUBLIC_HOST)."""
    with tempfile.TemporaryDirectory() as checkout:
        with open(os.path.join(checkout, ".env"), "w") as f:
            f.write(env_text)
        with patch.object(settings, "APPLIANCE_DIR", checkout), \
                patch.object(bolt, "_reachable", side_effect=lambda host: host in reachable), \
                patch.object(bolt.socket, "gethostname", return_value=hostname), \
                patch.object(bolt.socket, "getfqdn", return_value=fqdn), \
                contextlib.redirect_stderr(io.StringIO()):
            settings.use_instance(settings.DEFAULT_INSTANCE)
            host = bolt.ensure_public_host(mode)
            return host, settings.env_file_value(bolt.BOLT_HOST_VAR)


BASE = "EMBABEL_PORT_BASE=11042\n"

# A line that is there is somebody's answer, even one naming a host that does not resolve.
host, line = written(BASE + "BOLT_PUBLIC_HOST=graph.lan\n", reachable={"box"})
check((host, line) == ("graph.lan", "graph.lan"), f"an existing line was rewritten: {host}, {line}")

# The public base URL's host comes first, for the mode in play.
host, line = written(BASE + "WORLDS_PUBLIC_BASE_URL=https://worlds.example.com\n", reachable={"worlds.example.com", "box"})
check(line == "worlds.example.com", f"worlds mode ignored its public base URL: {line}")
host, line = written(BASE + "ASSISTANT_PUBLIC_BASE_URL=https://me.example.com\n", "me", reachable={"me.example.com", "box"})
check(line == "me.example.com", f"me mode ignored its public base URL: {line}")
# Another mode's URL is not this mode's name.
host, line = written(BASE + "ASSISTANT_PUBLIC_BASE_URL=https://me.example.com\n", "worlds", reachable={"me.example.com", "box"})
check(line == "box", f"worlds mode took the me mode's URL: {line}")
# A public base URL of localhost names nothing another machine can reach.
host, line = written(BASE + "WORLDS_PUBLIC_BASE_URL=http://localhost:11043\n", reachable={"box"})
check(line == "box", f"a localhost public base URL was advertised: {line}")

# The machine's own name before its fully qualified one, and never a reverse-lookup name.
host, line = written(BASE, reachable={"box", "box.example"})
check(line == "box", f"the hostname did not come first: {line}")
host, line = written(BASE, reachable={"1.0.ip6.arpa"}, hostname="nothing", fqdn="1.0.ip6.arpa")
check(line == "localhost", f"a reverse-lookup name was advertised: {line}")

# Nothing resolves: localhost, written down so the next run does not guess again.
host, line = written(BASE, reachable=())
check((host, line) == ("localhost", "localhost"), f"no name found, but advertised {host}, {line}")

# The surface URL names the same host as the advertised address.
with tempfile.TemporaryDirectory() as checkout:
    with open(os.path.join(checkout, ".env"), "w") as f:
        f.write(BASE + "GRAPH_TYPE=EMBABEL\nBOLT_PUBLIC_HOST=graph.lan\n")
    with patch.object(settings, "APPLIANCE_DIR", checkout):
        settings.use_instance(settings.DEFAULT_INSTANCE)
        url = settings.surface_urls().get("bolt")
check(url == "bolt://graph.lan:11050", f"the CLI's Bolt URL is not the advertised one: {url}")

# Both modes publish Bolt on BOLT_BIND, beyond loopback by default, and advertise BOLT_PUBLIC_HOST.
for compose in ("docker-compose-worlds.yml", "docker-compose-me.yml"):
    text = open(compose).read()
    for port, container in (("WORLDS_BOLT_PORT:-11050", "7687"), ("WORLDS_PERSISTED_BOLT_PORT:-11051", "7688")):
        check(f'"${{BOLT_BIND:-0.0.0.0}}:${{{port}}}:{container}"' in text, f"{compose}: {container} is not on BOLT_BIND")
        check(f'"127.0.0.1:${{{port}}}:{container}"' not in text, f"{compose}: {container} is still loopback only")
    for var, port in (("GRAPH_BOLT_ADVERTISED_ADDRESS", "WORLDS_BOLT_PORT:-11050"),
                      ("GRAPH_BOLT_PERSISTED_ADVERTISED_ADDRESS", "WORLDS_PERSISTED_BOLT_PORT:-11051")):
        check(f"{var}=${{BOLT_PUBLIC_HOST:-localhost}}:${{{port}}}" in text, f"{compose}: {var} does not name BOLT_PUBLIC_HOST")

if failures:
    print("\n".join(f"  ✗ {f}" for f in failures))
    sys.exit(1)
print("  ✓ Bolt publishes beyond loopback and advertises a name other machines can dial")
