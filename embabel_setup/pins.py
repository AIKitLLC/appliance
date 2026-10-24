"""What an install runs on, written into its .env so a release cannot change it.

WHY. Some defaults choose something with data or a provider behind it: which graph
engine holds the world, where its store lives, which model made the vectors in it,
which Neo4j wrote its files, where model calls go. Change one of those defaults in a
release and every install that never stated it moves with it — onto an empty graph
behind a login that works, onto vectors of a width the index was not built for, onto
somebody else's endpoint. Nothing fails at the moment it happens. So none of them may
rest on a default: each is written down, once, and after that only a person changes it.

ONE TABLE, PINNED. Each row is a key, when it means anything for this install, the
value an install that predates the row has been running on, the value a new install
gets, and why it is pinned. A later migration is a new `new` value, or a new row.

THE `legacy` VALUE NEVER CHANGES, and neither does the compose fallback that matches
it. `embabel upgrade` runs the CLI that was installed against the compose files it has
just pulled, so for that one run an install that was never pinned is composed from the
fallbacks — they have to keep meaning what those installs run on. A new choice goes in
`new`, which only setup writes, for an install it is creating.
scripts/check-graph-engine.py holds each fallback to its row.

WHEN. pin_install runs at the top of every CLI verb and every setup run (see
cli.resolve_instance and setup.main), so the first run of a CLI that has a row is the
run that pins it, before anything starts a container. Idempotent: a key that has a
line in .env — even an empty one — is somebody's answer and is never rewritten.

NOT PINNED, on purpose, because nothing stored or provided moves when they change:
image versions that are meant to move with the checkout over data they do not own
(the server, the console, docling, the sandbox, world-sql), memory and pool sizing,
ports, the Bolt server, phone-home (off unless .env says so, and a decision rather
than a default), the MCP mode and role fallback, fsync posture, metric retention.
"""

from __future__ import annotations
import os
import sys
from dataclasses import dataclass
from typing import Callable

from .settings import (
    GRAPH_TYPE_VAR, UNPINNED_ENGINE, canonical_engine, env_file, env_file_assigns, env_file_value, env_path,
    set_env_var, volume_exists, volume_name,
)

# The volumes whose presence says this instance has run before, whatever its .env says.
INSTALL_VOLUMES = ("embabel_assistant_data", "embabel_appliance_neo4j_data")
# Where the Embabel engine keeps its graph: the data volume the worlds are on.
EMBABEL_JOURNAL = "/data/graph/memory.journal"
EMBABEL_SQLITE = "/data/graph/graph.sqlite"
# The Neo4j every install that predates NEO4J_VERSION was composed with. infra.yml's
# fallback says the same, and must go on saying it — see the header.
LEGACY_NEO4J_VERSION = "2026.06-community"


@dataclass(frozen=True)
class Pin:
    key: str
    # Whether the key means anything for this install. Read after the rows above it are
    # written, so a row can depend on the engine.
    applies: Callable[[], bool]
    # What an install that predates this row runs on. None: cannot tell on this run —
    # write nothing, and ask again next time.
    legacy: Callable[[], str | None]
    # What a new install gets. The environment's value wins: that is a stated choice.
    new: str
    why: str


def _always() -> bool:
    return True


def _on(engine: str) -> Callable[[], bool]:
    """The engine .env records, not graph_engine(): a GRAPH_TYPE exported for one run
    is not what this install's data is in, and its rows are not this install's."""
    return lambda: (canonical_engine(env_file_value(GRAPH_TYPE_VAR)) or UNPINNED_ENGINE) == engine


def _constant(value: str) -> Callable[[], str]:
    return lambda: value


def _embabel_store() -> str | None:
    """Which store holds this install's Embabel graph, read off the data volume.

    The server's default store moved from its own journal file to SQLite, and an
    install made under the first boots empty under the second: there is no migration
    between them. So the file on the volume decides. Neither file means nothing has
    been written yet, which is a new graph and gets the new store.
    """
    if not volume_exists("embabel_assistant_data"):
        return "sqlite"
    # Late: backup imports this module, for restore. alpine, because backup already pins it.
    from .backup import BACKUP_HELPER_IMAGE
    from .dockerlib import _docker
    run = _docker("run", "--rm", "-v", f"{volume_name('embabel_assistant_data')}:/data:ro", BACKUP_HELPER_IMAGE,
                  "ls", "-a", os.path.dirname(EMBABEL_JOURNAL), timeout=120)
    if run is None or run.returncode not in (0, 1):
        return None
    found = set(run.stdout.split())
    if os.path.basename(EMBABEL_SQLITE) in found:
        return "sqlite"
    return "snapshot" if any(name.startswith(os.path.basename(EMBABEL_JOURNAL)) for name in found) else "sqlite"


def _neo4j_version() -> str:
    """The Neo4j this install's container was made from, else what compose would make it from."""
    from .dockerlib import _docker, service_container
    container = service_container("neo4j", stopped=True)
    run = _docker("inspect", container, "--format", "{{.Config.Image}}", timeout=15) if container else None
    image = run.stdout.strip() if run and run.returncode == 0 else ""
    return image.rpartition(":")[2] if image.startswith("neo4j:") else LEGACY_NEO4J_VERSION


PINS = (
    Pin(GRAPH_TYPE_VAR, _always, _constant(UNPINNED_ENGINE), "EMBABEL",
        "which graph holds the world; the engines do not share data, so moving is an empty world"),
    Pin("VC_EMBABEL_STORE", _on("EMBABEL"), _embabel_store, "sqlite",
        "the Embabel engine's store; the default moved once already, with no migration between them"),
    Pin("VC_EMBABEL_JOURNAL", _on("EMBABEL"), _constant(EMBABEL_JOURNAL), EMBABEL_JOURNAL,
        "where the Embabel engine's journal is, on the data volume; elsewhere is a different graph"),
    Pin("GRAPH_EMBABEL_SQLITE_PATH", _on("EMBABEL"), _constant(EMBABEL_SQLITE), EMBABEL_SQLITE,
        "where the Embabel engine's SQLite file is, on the data volume; elsewhere is a different graph"),
    Pin("NEO4J_VERSION", _on("NEO4J"), _neo4j_version, LEGACY_NEO4J_VERSION,
        "the Neo4j that writes the graph's files; a new one migrates them and an old one cannot read them"),
    Pin("NEO4J_PASSWORD", _on("NEO4J"), _constant("embabel-assistant"), "embabel-assistant",
        "Neo4j keeps its password in its volume; a different default is a graph the server cannot open"),
    Pin("ASSISTANT_EMBEDDING_MODEL", _always, _constant("all-MiniLM-L6-v2"), "all-MiniLM-L6-v2",
        "the model that made the stored vectors; another one re-embeds everything at its own width"),
    Pin("ASSISTANT_DOC_CONVERTER", _always, _constant("docling"), "docling",
        "the service documents are converted by; another one chunks the same file differently"),
    Pin("OLLAMA_BASE_URL", _always, _constant("http://host.docker.internal:11434"),
        "http://host.docker.internal:11434", "where Ollama model calls go"),
    Pin("LMSTUDIO_BASE_URL", _always, _constant("http://host.docker.internal:1234"),
        "http://host.docker.internal:1234", "where LM Studio model calls go"),
    Pin("DOCKER_MODELS_BASE_URL", _always, _constant("http://model-runner.docker.internal/engines/v1"),
        "http://model-runner.docker.internal/engines/v1", "where the local embedding model is served from"),
    Pin("EMBABEL_MONITORING", _always, _constant("false"), "false",
        "whether Prometheus and Grafana run, and so whether their memory is held back from the app"),
)


def install_exists() -> bool:
    """This instance has run before: a .env, or either of its volumes."""
    return os.path.exists(env_path()) or any(volume_exists(key) for key in INSTALL_VOLUMES)


def pin_install(creating: bool) -> dict[str, str]:
    """Write every row this install has no line for. Returns what was written.

    An install that exists gets each row's `legacy` value, because that is what it has
    been running: nothing ever wrote these, so the defaults chose. A value exported in
    the shell does not change that — it still wins for the runs it is exported in, as it
    always did, and is not mistaken for a decision about the data.

    A NEW install is the only one that gets the new choices, and only from setup
    (`creating`), before ensure_port_block writes the first line of its .env: the
    environment's value where one is set, the row's `new` otherwise. Every other entry
    passes creating=False and only pins an install whose .env is there, so a status, a
    doctor or the Me app polling never creates one.

    Docker that cannot answer reads as no volumes. The one install that misjudges is one
    whose .env was deleted while its volumes were kept, on a run where Docker is down —
    and the server refuses to start a stamped world on a store it was not made in.
    """
    exists = os.path.exists(env_path())
    if not exists and not creating:
        return {}
    fresh = creating and not install_exists()
    written = {}
    for pin in PINS:
        if env_file_assigns(pin.key) or not pin.applies():
            continue
        if fresh:
            stated = os.environ.get(pin.key, "").strip()
            value = (canonical_engine(stated) if pin.key == GRAPH_TYPE_VAR else stated) or pin.new
            note = "chosen when this appliance was created"
        else:
            value = pin.legacy()
            note = "pinned to what this appliance was already running"
        if value is None:
            continue
        set_env_var(pin.key, value, (f"# {pin.key}: {note} — {pin.why}.",))
        written[pin.key] = value
    if written:
        # stderr: the Me app reads a verb's stdout as JSON, and this can come before any verb.
        print(f"  Pinned in {env_file()}: " + ", ".join(f"{k}={v}" for k, v in written.items()), file=sys.stderr)
    return written
