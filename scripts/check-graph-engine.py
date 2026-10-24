#!/usr/bin/env python3
"""Every install keeps what it runs on, and a new one starts on the Embabel engine.

WHY THIS EXISTS. The graph engines do not share data, so an install that moves engine
opens on an empty world behind a login that works — nothing fails and nothing warns. The
same is true, more quietly, of the engine's store, the embedding model, the Neo4j that
wrote the files. Nothing wrote those for the installs made while they were defaults, and
a default is the one thing a release changes. So pins.py writes them into .env, and
these are the cases that decide whether it does that right: an existing install gets
what it was running, a new one gets the new choices, a line already there is never
touched, a line with an inline comment is read the way compose reads it, and each
compose fallback still says what an unpinned install runs on — because the run an
upgrade makes, with the CLI that was installed, is composed from exactly those.

Nothing here talks to Docker: volumes, containers and the volume listing are patches.

    python3 scripts/check-graph-engine.py
"""
import contextlib
import io
import os
import re
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from embabel_setup import backup, dockerlib, pins, settings  # noqa: E402

failures = []


def check(ok: bool, message: str) -> None:
    if not ok:
        failures.append(message)


def env_of(text):
    """A .env's text as compose would read it."""
    found = {}
    for line in (text or "").splitlines():
        parsed = settings._dotenv_assignment(line)
        if parsed:
            found[parsed[0]] = parsed[1]
    return found


def pinned(env_text, *, creating, volumes=(), environ=None, graph_files="", neo4j_image=None, server="sqlite"):
    """Run the pin against a scratch checkout. Returns (written, engine, .env after).
    `server` is the store the server image carries: sqlite, snapshot, or None if absent."""
    def docker(*argv, **_kwargs):
        if argv[:1] == ("run",):
            return subprocess.CompletedProcess(argv, 0, graph_files, "")
        if argv[:1] == ("inspect",):
            return subprocess.CompletedProcess(argv, 0, neo4j_image or "", "")
        return None

    with tempfile.TemporaryDirectory() as checkout:
        path = os.path.join(checkout, ".env")
        if env_text is not None:
            with open(path, "w") as f:
                f.write(env_text)
        clean = {k: v for k, v in os.environ.items() if k not in {p.key for p in pins.PINS}}
        with patch.object(settings, "APPLIANCE_DIR", checkout), \
                patch.dict(os.environ, {**clean, **(environ or {})}, clear=True), \
                patch.object(pins, "volume_exists", side_effect=lambda key: key in volumes), \
                patch.object(dockerlib, "_docker", side_effect=docker), \
                patch.object(dockerlib, "service_container", return_value="c" if neo4j_image else None), \
                patch.object(pins, "_store_the_server_has", return_value=server), \
                contextlib.redirect_stderr(io.StringIO()):
            settings.use_instance(settings.DEFAULT_INSTANCE)
            written = pins.pin_install(creating=creating)
            engine = settings.graph_engine()
            after = open(path).read() if os.path.exists(path) else None
        return written, engine, after


EXISTING = "EMBABEL_PORT_BASE=11042\n"

# ── the pin ──────────────────────────────────────────────────────────────────

# An existing install — a .env from before the pin — gets what it has been running.
written, engine, after = pinned(EXISTING, creating=False)
check(written.get("GRAPH_TYPE") == "NEO4J" and engine == "NEO4J", f"existing install: {written}")
for key in ("NEO4J_VERSION", "NEO4J_PASSWORD", "ASSISTANT_EMBEDDING_MODEL", "ASSISTANT_DOC_CONVERTER",
            "OLLAMA_BASE_URL", "LMSTUDIO_BASE_URL", "DOCKER_MODELS_BASE_URL", "EMBABEL_MONITORING"):
    check(key in env_of(after), f"existing install: {key} was not pinned")
check(not any(k.startswith(("VC_EMBABEL", "GRAPH_EMBABEL")) for k in written),
      f"a Neo4j install was given the Embabel engine's settings: {written}")
written, _engine, _after = pinned(EXISTING, creating=True)
check(written.get("GRAPH_TYPE") == "NEO4J", f"existing install, setup re-run: {written}")
# ...whatever the shell exports: that still wins per run, and is not a decision about the data.
written, engine, after = pinned(EXISTING, creating=True, environ={"GRAPH_TYPE": "EMBABEL"})
check(written.get("GRAPH_TYPE") == "NEO4J", f"existing install with GRAPH_TYPE exported: {written}")
check(engine == "EMBABEL", f"an exported GRAPH_TYPE no longer wins for its run: {engine!r}")
check("VC_EMBABEL_STORE" not in written, "an exported GRAPH_TYPE gave a Neo4j install the Embabel rows")
# The Neo4j that made the container, not the one the checkout would make it with.
written, _engine, _after = pinned(EXISTING, creating=False, neo4j_image="neo4j:2026.04-community")
check(written.get("NEO4J_VERSION") == "2026.04-community", f"running Neo4j not pinned: {written}")

# An install whose .env is gone but whose volumes are not is still an existing install.
for volume in pins.INSTALL_VOLUMES:
    written, _engine, _after = pinned(None, creating=True, volumes=(volume,))
    check(written.get("GRAPH_TYPE") == "NEO4J", f"no .env but {volume}: {written}")
# ...but only setup writes a .env. A status or a doctor must not create one.
written, _engine, after = pinned(None, creating=False, volumes=pins.INSTALL_VOLUMES)
check(not written and after is None, f"a CLI verb created a .env: {written}")

# A new instance: no .env, no volumes.
written, engine, after = pinned(None, creating=True)
check(engine == "EMBABEL", f"fresh install runs {engine!r}")
check(env_of(after).get("GRAPH_TYPE") == "EMBABEL" and env_of(after).get("VC_EMBABEL_STORE") == "sqlite",
      f"fresh install .env: {after!r}")
check(env_of(after).get("GRAPH_EMBABEL_SQLITE_PATH", "").startswith("/data/"), "the SQLite file is not on the data volume")
check("NEO4J_VERSION" not in env_of(after), "a new Embabel install was given Neo4j rows")
written, _engine, after = pinned(None, creating=True, environ={"GRAPH_TYPE": "neo4j", "OLLAMA_BASE_URL": "http://o:1"})
check(written.get("GRAPH_TYPE") == "NEO4J" and "NEO4J_VERSION" in written, f"fresh install asking for NEO4J: {written}")
check(written.get("OLLAMA_BASE_URL") == "http://o:1", "a fresh install's stated value was not the one written")
written, _engine, _after = pinned(None, creating=False)
check(not written, f"fresh instance, CLI entry: wrote {written} instead of leaving it to setup")

# The Embabel engine's store is the one its volume holds; there is no migration between them.
# With a graph written, the file decides; with none, the server image does — a server without
# the SQLite store journals whatever .env says, and a `sqlite` pin over that journal strands it.
for files, server, store in (("graph.sqlite\nmemory.journal\n", "snapshot", "sqlite"),
                             ("memory.journal\n", "sqlite", "snapshot"),
                             ("memory.journal.snapshot\n", "sqlite", "snapshot"),
                             ("", "sqlite", "sqlite"), ("", "snapshot", "snapshot"), ("", None, None)):
    written, _engine, _after = pinned("GRAPH_TYPE=MEMORY\n", creating=False, volumes=("embabel_assistant_data",),
                                      graph_files=files, server=server)
    check(written.get("VC_EMBABEL_STORE") == store,
          f"graph files {files!r}, server {server!r}: store {written.get('VC_EMBABEL_STORE')!r}, not {store!r}")
    check("GRAPH_TYPE" not in written, "GRAPH_TYPE=MEMORY was rewritten")
# A new install is the same rule: the store its server image has, or nothing until it is here.
for server in ("sqlite", "snapshot", None):
    written, _engine, _after = pinned(None, creating=True, server=server)
    check(written.get("VC_EMBABEL_STORE") == server, f"fresh install, server {server!r}: {written.get('VC_EMBABEL_STORE')!r}")

# A line already there is the decision, whatever it says — empty included — and twice is once.
for value in ("NEO4J", "EMBABEL", "MEMORY", "FALKORDB", ""):
    text = EXISTING + f"GRAPH_TYPE={value}\n"
    _written, _engine, after = pinned(text, creating=True)
    check(after.startswith(text) and after.count("GRAPH_TYPE=") == 1, f"GRAPH_TYPE={value} was rewritten")
_written, _engine, after = pinned(EXISTING + "ASSISTANT_EMBEDDING_MODEL=\n", creating=False)
check(env_of(after)["ASSISTANT_EMBEDDING_MODEL"] == "", "`embeddings off` was overwritten by the pin")
_written, _engine, once = pinned(EXISTING, creating=False)
written, _engine, twice = pinned(once, creating=False)
check(once == twice and not written, f"pinning twice wrote {written} the second time")

# MEMORY is the old name of the Embabel engine; it reads as EMBABEL everywhere.
_written, engine, _after = pinned("GRAPH_TYPE=MEMORY\n", creating=False)
check(engine == "EMBABEL", f"GRAPH_TYPE=MEMORY reads as {engine!r}")

# Inline comments, quotes and spacing are read as compose reads them.
for line in ("GRAPH_TYPE=NEO4J   # pinned", "GRAPH_TYPE=NEO4J\t# pinned", 'GRAPH_TYPE="NEO4J" # pinned',
             "GRAPH_TYPE='NEO4J'", "export GRAPH_TYPE = NEO4J", "  GRAPH_TYPE=NEO4J  "):
    written, engine, _after = pinned(EXISTING + line + "\n", creating=True)
    check("GRAPH_TYPE" not in written and engine == "NEO4J", f"{line!r}: engine {engine!r}, rewrote {written}")
_written, engine, _after = pinned("GRAPH_TYPE=EMBABEL\nGRAPH_TYPE=NEO4J\n", creating=False)
check(engine == "NEO4J", "the last assignment does not win, as it does for compose")
check(settings._dotenv_assignment("KEY=a#b") == ("KEY", "a#b"), "a # without whitespace before it started a comment")
check(settings._dotenv_assignment("KEY=a$$b") == ("KEY", "a$b"), "an escaped dollar was not unescaped")
check(settings._dotenv_assignment("KEY='a$$b'") == ("KEY", "a$$b"), "single quotes were interpolated")
check(settings._dotenv_assignment("# KEY=x") is None, "a commented-out line was read")

# ── the table ────────────────────────────────────────────────────────────────

keys = [p.key for p in pins.PINS]
check(len(keys) == len(set(keys)), f"a key is pinned twice: {keys}")
check(keys[0] == "GRAPH_TYPE", "GRAPH_TYPE must come first: the rows after it depend on the engine")
check(all(p.why and p.new is not None for p in pins.PINS), "a row has no reason or no new-install value")

# Each compose fallback for a pinned key is what an unpinned install runs on — the row's
# legacy value — in every file that has one.
COMPOSE = [f for f in os.listdir(ROOT) if f.endswith(".yml") and not f.startswith(".")]
fallbacks = {}
for name in COMPOSE:
    with open(os.path.join(ROOT, name)) as f:
        for key, value in re.findall(r"\$\{([A-Z0-9_]+):-([^}]*)\}", f.read()):
            fallbacks.setdefault(key, set()).add((name, value))
constant = {"VC_EMBABEL_STORE": "snapshot", "NEO4J_VERSION": pins.LEGACY_NEO4J_VERSION}
for pin in pins.PINS:
    seen = fallbacks.get(pin.key, set())
    if pin.key == "EMBABEL_MONITORING":
        check(not seen, "EMBABEL_MONITORING appeared in a compose file; the CLI owns it")
        continue
    check(bool(seen), f"{pin.key} is pinned but no compose file falls back on it")
    want = constant.get(pin.key) or pin.legacy()
    for name, value in seen:
        check(value == want, f"{name}: ${{{pin.key}:-{value}}} is not the unpinned value {want!r}")
# The CLI's own reading of monitoring agrees with the row when .env says nothing.
with tempfile.TemporaryDirectory() as checkout, patch.object(settings, "APPLIANCE_DIR", checkout), \
        patch.dict(os.environ, {}, clear=True):
    check(settings.monitoring_enabled() is False, "monitoring is not off when .env says nothing")

# ── the overlay ──────────────────────────────────────────────────────────────

for mode in ("worlds", "me"):
    with open(f"graph-embabel-{mode}.yml") as f:
        canonical = f.read()
    with open(f"graph-memory-{mode}.yml") as f:
        alias = f.read()
    check(canonical.split("\nservices:\n", 1)[1] == alias.split("\nservices:\n", 1)[1],
          f"graph-memory-{mode}.yml has drifted from graph-embabel-{mode}.yml below the header")
    body = canonical.split("\nservices:\n", 1)[1]
    check("- GRAPH_TYPE=EMBABEL\n" in body, f"{mode}: the overlay does not select EMBABEL")
    for gone in ("DRIVINE_GRAPH_TYPE", "DRIVINE_SCHEMA_ENABLED", "VC_MEMORY_JOURNAL"):
        check(gone not in body, f"{mode}: the overlay still sets {gone}")
    check("depends_on: !override []" in body, f"{mode}: the server still waits for a graph service")
    check(re.search(r"\n  neo4j:\n    profiles: \[", body) is not None, f"{mode}: neo4j is not parked")
    for engine in ("EMBABEL", "MEMORY"):
        with patch.dict(os.environ, {"GRAPH_TYPE": engine}):
            check(dockerlib.graph_overlay_file(mode) == f"graph-embabel-{mode}.yml",
                  f"GRAPH_TYPE={engine} composes {dockerlib.graph_overlay_file(mode)}")
            check("neo4j" not in dockerlib.core_services(mode), f"{mode} on {engine} still starts neo4j")
    with patch.dict(os.environ, {"GRAPH_TYPE": "NEO4J"}):
        check(dockerlib.graph_overlay_file(mode) is None, f"{mode}: Neo4j has an overlay")
        check("neo4j" in dockerlib.core_services(mode), f"{mode} on NEO4J does not start neo4j")

# What is offered to a person follows the engine: no Neo4j Browser link, and no graph lamp
# waiting on a container, where there is no Neo4j.
with patch.dict(os.environ, {"GRAPH_TYPE": "EMBABEL"}):
    urls = settings.surface_urls()
    check("graph" not in urls, f"an Embabel appliance offers Neo4j Browser: {urls.get('graph')}")
    check(urls.get("bolt", "").endswith(str(settings.ports_for(settings.port_base())["WORLDS_BOLT_PORT"])),
          f"an Embabel appliance's Bolt is not the server's: {urls.get('bolt')}")
    with patch.object(dockerlib, "service_container", side_effect=AssertionError("asked docker")):
        check(dockerlib.find_graph_container() is None, "an Embabel appliance looked for a graph container")
with patch.dict(os.environ, {"GRAPH_TYPE": "NEO4J"}):
    check("graph" in settings.surface_urls(), "a Neo4j appliance lost its Neo4j Browser link")

# ── backup and restore ───────────────────────────────────────────────────────

def backed_up(engine, volumes):
    """Take a backup with the volumes stubbed, then inspect it and restore it."""
    streamed, restored = [], []

    def stream(argv, path, *, into_volume):
        (restored if into_volume else streamed).append(argv[argv.index("-v") + 1].split(":")[0])
        if not into_volume:
            with open(path, "wb") as f:
                f.write(b"tar")
        return True, ""

    with tempfile.TemporaryDirectory() as checkout, tempfile.TemporaryDirectory() as dest:
        with open(os.path.join(checkout, ".env"), "w") as f:
            f.write(f"EMBABEL_PORT_BASE=11042\nGRAPH_TYPE={engine}\n")
        clean = {k: v for k, v in os.environ.items() if k != "GRAPH_TYPE"}
        with patch.object(settings, "APPLIANCE_DIR", checkout), patch.object(backup, "APPLIANCE_DIR", checkout), \
                patch.dict(os.environ, clean, clear=True), \
                patch.object(backup, "volume_exists", side_effect=lambda key: key in volumes), \
                patch.object(pins, "volume_exists", side_effect=lambda key: key in volumes), \
                patch.object(backup, "_stream_volume", side_effect=stream), \
                patch.object(backup, "_docker", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                patch.object(backup, "_compose", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                patch.object(backup, "running_mode_names", return_value=[]), \
                patch.object(backup, "backup_mode", return_value="worlds"), \
                patch.object(backup, "appliance_versions", return_value={}), \
                patch.object(dockerlib, "_docker", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            settings.use_instance(settings.DEFAULT_INSTANCE)
            try:
                taken = backup.back_up(dest)
                manifest = backup.inspect_backup(taken)
                backup.restore(taken)
            except settings.SetupError as e:
                return str(e), [], [], {}
    return None, streamed, restored, manifest


data = settings.volume_name("embabel_assistant_data")
neo = settings.volume_name("embabel_appliance_neo4j_data")
error, streamed, restored, manifest = backed_up("EMBABEL", ("embabel_assistant_data",))
check(error is None, f"an Embabel appliance with no Neo4j volume could not be backed up: {error}")
check(streamed == [data] and restored == [data], f"Embabel backup copied {streamed}, restored {restored}")
check(manifest.get("volumes") == ["assistant-data.tgz"] and manifest.get("engine") == "EMBABEL",
      f"the manifest does not say what it holds: {manifest}")
error, streamed, restored, _manifest = backed_up("NEO4J", ("embabel_assistant_data", "embabel_appliance_neo4j_data"))
check(error is None and streamed == [data, neo] and restored == [data, neo],
      f"a Neo4j backup no longer carries the graph: {error} {streamed} {restored}")
check(backup.backup_volume_files({"files": ["x"]}) == backup.LEGACY_BACKUP_FILES,
      "a backup from before the manifest named its volumes is no longer read as Neo4j's")

if failures:
    print("\n".join("FAIL " + f for f in failures))
    sys.exit(1)
print("ok — existing installs keep what they run on, new ones get the new choices, and the fallbacks agree")
