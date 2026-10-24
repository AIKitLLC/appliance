#!/usr/bin/env python3
"""Every install keeps the graph engine it was created on, and a new one starts on Embabel.

WHY THIS EXISTS. The engines do not share data, so an install that moves engine opens
on an empty world behind a login that works — nothing fails and nothing warns. Nothing
wrote GRAPH_TYPE for the installs made while Neo4j was the default, so they ran on
whatever the compose files fell back to, and a default is the one thing an upgrade
changes. These are the cases that decide it: an existing install with no GRAPH_TYPE is
pinned to NEO4J, a new one is given EMBABEL, a value already there is never touched,
and a value carrying an inline comment is read the way compose reads it.

Nothing here talks to Docker: volumes are answered by a patch.

    python3 scripts/check-graph-engine.py
"""
import contextlib
import io
import os
import sys
import tempfile
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from embabel_setup import settings  # noqa: E402

failures = []


def check(ok: bool, message: str) -> None:
    if not ok:
        failures.append(message)


def pinned(env_text, *, creating, volumes=(), environ=None):
    """Run the pin against a scratch checkout and return (written, .env afterwards)."""
    with tempfile.TemporaryDirectory() as checkout:
        path = os.path.join(checkout, ".env")
        if env_text is not None:
            with open(path, "w") as f:
                f.write(env_text)
        clean = {k: v for k, v in os.environ.items() if k != "GRAPH_TYPE"}
        with patch.object(settings, "APPLIANCE_DIR", checkout), \
                patch.dict(os.environ, {**clean, **(environ or {})}, clear=True), \
                patch.object(settings, "volume_exists", side_effect=lambda key: key in volumes), \
                contextlib.redirect_stderr(io.StringIO()):
            settings.use_instance(settings.DEFAULT_INSTANCE)
            written = settings.pin_graph_engine(creating=creating)
            engine = settings.graph_engine()
            after = open(path).read() if os.path.exists(path) else None
        return written, engine, after


# An existing install — a .env from before the pin — is pinned to what it has been running.
written, engine, after = pinned("EMBABEL_PORT_BASE=11042\n", creating=False)
check(written == "NEO4J" and "GRAPH_TYPE=NEO4J" in after, f"existing install, CLI entry: wrote {written!r}, .env {after!r}")
written, engine, after = pinned("EMBABEL_PORT_BASE=11042\n", creating=True)
check(written == "NEO4J", f"existing install, setup re-run: wrote {written!r}, not NEO4J")
# ...whatever the shell exports: that still wins per run, and is not a decision about the data.
written, engine, after = pinned("EMBABEL_PORT_BASE=11042\n", creating=True, environ={"GRAPH_TYPE": "EMBABEL"})
check(written == "NEO4J", f"existing install with GRAPH_TYPE exported: wrote {written!r}, not NEO4J")
check(engine == "EMBABEL", f"an exported GRAPH_TYPE no longer wins for its run: {engine!r}")

# An install whose .env is gone but whose volumes are not is still an existing install.
for volume in settings.INSTALL_VOLUMES:
    written, _engine, after = pinned(None, creating=True, volumes=(volume,))
    check(written == "NEO4J", f"no .env but {volume}: wrote {written!r}, not NEO4J")
# ...but only setup writes a .env. A status or a doctor must not create one.
written, _engine, after = pinned(None, creating=False, volumes=settings.INSTALL_VOLUMES)
check(written is None and after is None, f"a CLI verb created a .env: wrote {written!r}")

# A new instance: no .env, no volumes.
written, engine, after = pinned(None, creating=True)
check(written == "EMBABEL" and engine == "EMBABEL", f"fresh install: wrote {written!r}, engine {engine!r}")
check(after is not None and "GRAPH_TYPE=EMBABEL" in after, f"fresh install .env: {after!r}")
written, _engine, _after = pinned(None, creating=True, environ={"GRAPH_TYPE": "neo4j"})
check(written == "NEO4J", f"fresh install asking for NEO4J got {written!r}")
written, _engine, _after = pinned(None, creating=False)
check(written is None, f"fresh instance, CLI entry: wrote {written!r} instead of leaving it to setup")

# A value already there is the decision, whatever it says, and running twice changes nothing.
for value in ("NEO4J", "EMBABEL", "MEMORY", "FALKORDB"):
    text = f"EMBABEL_PORT_BASE=11042\nGRAPH_TYPE={value}\n"
    written, _engine, after = pinned(text, creating=True)
    check(written is None and after == text, f"GRAPH_TYPE={value} was rewritten: {after!r}")
written, _engine, once = pinned("EMBABEL_PORT_BASE=11042\n", creating=False)
_written, _engine, twice = pinned(once, creating=False)
check(once == twice, "pinning twice changed .env the second time")

# MEMORY is the old name of the Embabel engine; it reads as EMBABEL everywhere.
_written, engine, _after = pinned("GRAPH_TYPE=MEMORY\n", creating=False)
check(engine == "EMBABEL", f"GRAPH_TYPE=MEMORY reads as {engine!r}")

# Inline comments, quotes and spacing are read as compose reads them.
for line in ("GRAPH_TYPE=NEO4J   # pinned", "GRAPH_TYPE=NEO4J\t# pinned", 'GRAPH_TYPE="NEO4J" # pinned',
             "GRAPH_TYPE='NEO4J'", "export GRAPH_TYPE = NEO4J", "  GRAPH_TYPE=NEO4J  "):
    written, engine, after = pinned(f"EMBABEL_PORT_BASE=11042\n{line}\n", creating=True)
    check(written is None and engine == "NEO4J", f"{line!r}: engine {engine!r}, rewrote {written!r}")
_written, engine, _after = pinned("GRAPH_TYPE=EMBABEL\nGRAPH_TYPE=NEO4J\n", creating=False)
check(engine == "NEO4J", "the last assignment does not win, as it does for compose")
check(settings._dotenv_assignment("KEY=a#b") == ("KEY", "a#b"), "a # without whitespace before it started a comment")
check(settings._dotenv_assignment("KEY=a$$b") == ("KEY", "a$b"), "an escaped dollar was not unescaped")
check(settings._dotenv_assignment("KEY='a$$b'") == ("KEY", "a$$b"), "single quotes were interpolated")
check(settings._dotenv_assignment("# KEY=x") is None, "a commented-out line was read")

if failures:
    print("\n".join("FAIL " + f for f in failures))
    sys.exit(1)
print("ok — existing installs pin NEO4J, new ones get EMBABEL, and .env reads as compose reads it")
