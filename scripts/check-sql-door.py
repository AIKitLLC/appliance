#!/usr/bin/env python3
"""SQL is the server's own door, on by default, in the instance's port block.

The appliance used to run SQL as an opt-in sidecar on a fixed host port. The server
serves it itself now, for every install, and three things keep that true:

- Both modes' server services publish the door on WORLDS_SQL_PORT, whose compose
  fallback is the default block's port. A fixed port worked while SQL was opt-in;
  on by default, it would stop the second appliance on a machine from starting.
- EMBABEL_SQL reaches the server as GRAPH_SQL_ENABLED, unset meaning on, so an
  install that said --no-sql to the sidecar keeps SQL off.
- The sidecar is gone from the compose files and from the profiles the CLI turns
  on, and `up` removes the container an older install still has, which would
  otherwise go on answering on the old port.

Standard library only: the compose files are read as text, the way the other checks
read them, and Docker is never called.
"""
import contextlib
import io
import os
import re
import subprocess
import sys
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from embabel_setup import dockerlib, settings, surfaces  # noqa: E402

failures = []


def check(ok, message):
    if not ok:
        failures.append(message)


SERVICE = re.compile(r"^  ([A-Za-z0-9_.-]+):\s*$")


def service_lines(path):
    """Each top-level service's lines, by name."""
    found, name = {}, None
    with open(os.path.join(ROOT, path)) as f:
        for line in f:
            started = SERVICE.match(line)
            if started:
                name = started.group(1)
                found[name] = []
            elif name is not None:
                found[name].append(line.strip())
    return found


port = settings.ports_for(settings.DEFAULT_PORT_BASE)["WORLDS_SQL_PORT"]
for path, server in (("docker-compose-worlds.yml", "worlds"), ("docker-compose-me.yml", "assistant")):
    found = service_lines(path)
    check("world-sql" not in found, f"{path} still has the world-sql sidecar")
    lines = found.get(server, [])
    check(f'- "127.0.0.1:${{WORLDS_SQL_PORT:-{port}}}:15432"' in lines,
          f"{path}: {server} does not publish the SQL door on WORLDS_SQL_PORT (fallback {port})")
    check("- GRAPH_SQL_ENABLED=${EMBABEL_SQL:-true}" in lines,
          f"{path}: {server} does not take GRAPH_SQL_ENABLED from EMBABEL_SQL, on when unset")
    others = [name for name, body in found.items() if name != server and any(":15432" in b for b in body)]
    check(not others, f"{path}: something besides the server publishes the SQL port: {others}")

# On unless .env says off, in the words Spring reads as false.
for value, expected in (("", True), ("true", True), ("on", True), ("false", False), ("off", False),
                        ("no", False), ("0", False), ("FALSE", False)):
    with patch.dict(os.environ, {settings.SQL_VAR: value}), \
            patch.object(settings, "env_file_value", return_value=None):
        check(settings.sql_enabled() is expected, f"EMBABEL_SQL={value!r} reads as {settings.sql_enabled()}")

# No profile for the sidecar, whatever the switch says.
with patch.dict(os.environ, {settings.SQL_VAR: "true", "COMPOSE_PROFILES": ""}), \
        patch.object(dockerlib, "memory_env", return_value={}), \
        patch.object(dockerlib, "monitoring_enabled", return_value=False), \
        patch.object(dockerlib, "github_token", return_value=None):
    env = dockerlib.compose_env()
check("world-sql" not in env.get("COMPOSE_PROFILES", ""), f"compose_env turns on world-sql: {env.get('COMPOSE_PROFILES')}")

# An older install's sidecar is removed; with none, Docker is not asked to remove anything.
removed = []


def docker(*argv, **_kwargs):
    removed.append(argv)
    return subprocess.CompletedProcess(argv, 0, "", "")


with patch.object(dockerlib, "service_container", return_value="old-sql-sidecar"), \
        patch.object(dockerlib, "_docker", side_effect=docker), \
        contextlib.redirect_stdout(io.StringIO()):
    dockerlib.retire_sql_sidecar()
check(removed == [("rm", "--force", "old-sql-sidecar")], f"the sidecar was not removed: {removed}")
removed.clear()
with patch.object(dockerlib, "service_container", return_value=None), \
        patch.object(dockerlib, "_docker", side_effect=docker):
    dockerlib.retire_sql_sidecar()
check(not removed, f"Docker was asked to remove a sidecar that is not there: {removed}")


def sql_line(on, holds):
    out = io.StringIO()
    with patch.object(surfaces, "sql_enabled", return_value=on), \
            patch.object(surfaces, "port_base", return_value=settings.DEFAULT_PORT_BASE), \
            patch.object(surfaces, "find_mode_container", return_value="c"), \
            patch.object(surfaces, "jar_holds", return_value=holds), \
            contextlib.redirect_stdout(out):
        surfaces.print_sql_line("worlds")
    return out.getvalue()


# The address only where something answers at it.
check(f"localhost:{port}/world" in sql_line(True, True), "a server with the door is not given its address")
check(f"localhost:{port}/world" in sql_line(True, None), "an image that cannot be read hides the address")
check("localhost" not in sql_line(True, False) and "upgrade" in sql_line(True, False),
      "a server without the door is handed an address that refuses, not told to upgrade")
check("off" in sql_line(False, True) and "localhost" not in sql_line(False, True), "an off door is shown as on")

if failures:
    print("\n".join("  ✗ " + f for f in failures))
    sys.exit(1)
print(f"ok: SQL is the server's door on port {port}, on unless EMBABEL_SQL is false, and the sidecar is retired")
