"""Which appliance this process is talking to, and everything that follows from
that: its compose project, its settings file, its port block, its URLs.

One appliance is the normal case and none of this should be visible to somebody
who has one. It is a module because a second install turns every one of these
from a constant into a question, and the answers all come from the same place.
"""

from __future__ import annotations
import json
import os
import shutil
import re
import socket

from .core import APPLIANCE_DIR, MODE_COMPOSE, SetupError

def default_base() -> str:
    """Where the Me door answers for the instance in play. A function now: the
    port moves with the instance, so a constant could only ever be right once."""
    return f"http://localhost:{ports_for(port_base())['ASSISTANT_PORT']}"
def console_url() -> str:
    """WHERE A PERSON GOES in worlds mode — not the same as where the API is. The
    worlds server also serves the old Vaadin UI, and sending a new operator there
    instead of to the console makes their first impression a surface on its way out."""
    return f"http://localhost:{ports_for(port_base())['WORLDS_CONSOLE_PORT']}"
def api_address(base: str) -> str:
    """The appliance's API as a DIAGNOSTIC, deliberately not as a link.

    Worlds mode has two ports and only one of them is a place to go: the console is the
    surface, and the worlds door beside it also serves the legacy Vaadin UI. `console_url`
    already keeps new operators away from that door on the success path — but the failure
    path handed the same address out as a bare `http://…`, in a terminal that makes URLs
    clickable, next to the words "could not reach". Somebody following it lands on the
    surface everything else is careful to steer them off.

    Dropping the scheme is what stops it linkifying, and the surrounding words say what it
    is. Which port failed is worth knowing; it is just not an invitation.
    """
    return base.split("://", 1)[-1].rstrip("/")


# The host Bolt clients on other machines dial; bolt.ensure_public_host writes it.
BOLT_HOST_VAR = "BOLT_PUBLIC_HOST"


def bolt_public_host() -> str:
    """The host About, the routing table and these URLs name: localhost on an install not yet given one."""
    return env_file_value(BOLT_HOST_VAR) or "localhost"


def surface_urls() -> dict:
    """Every address this instance publishes, by the name a person uses for it.

    `graph` is Neo4j Browser, so only an appliance that runs Neo4j has one. On the
    Embabel engine the graph is inside the server and there is no browser to open;
    `bolt` is then the server's own Bolt port, which Neo4j's tools connect to, named
    by the host other machines reach it by (BOLT_PUBLIC_HOST)."""
    p = ports_for(port_base())
    urls = {"me": f"http://localhost:{p['ASSISTANT_PORT']}",
            "worlds": f"http://localhost:{p['WORLDS_PORT']}",
            "console": console_url(),
            "dashboards": f"http://localhost:{p['GRAFANA_PORT']}",
            "metrics": f"http://localhost:{p['PROMETHEUS_PORT']}"}
    if graph_engine() == "NEO4J":
        urls["graph"] = f"http://localhost:{p['NEO4J_BROWSER_PORT']}"
        urls["bolt"] = f"bolt://localhost:{p['NEO4J_BOLT_PORT']}"
    elif graph_engine() == "EMBABEL":
        urls["bolt"] = f"bolt://{bolt_public_host()}:{p['WORLDS_BOLT_PORT']}"
    return urls
# Machine-local configuration: keys, timezone, world template, realms directory.
# Gitignored, and removed by --uninstall — which is the difference between that
# and --fresh, since a .env that survives means the next run asks nothing.
ENV_FILE = ".env"
# The compose project both mode files declare. Anything belonging to the appliance
# carries it as a label — which is the only reliable way to tell the appliance's
# containers from a developer's own stack, whose names start the same way.
# The DEFAULT project name. Everything reads compose_project() instead, which
# resolves the instance in play — this remains only as the name that instance
# `appliance` produces, and as the answer for anything asking before an
# instance has been chosen.
COMPOSE_PROJECT = "embabel-appliance"
# The collector is fixed in the appliance image. This is repeated here because setup
# must disclose the destination before it asks the operator to finish installation;
# PHONE_HOME.md and the live endpoints remain the authoritative payload views.
# The collector, and the switch that decides whether anything reaches it. BOTH
# live in this repo rather than in the image: the address so it can be read
# before it is used, and the switch so turning reporting on is a thing an
# operator does here, deliberately, rather than a default they inherit.
#
# OFF unless .env says otherwise. The compose files name the endpoint with an
# empty default, so a hand-run `docker compose up` is off too — a switch that
# only works when you go through setup.py is not a switch.
PHONE_HOME_ENDPOINT = "https://telemetry.embabel.com/v1/appliance"
PHONE_HOME_VAR = "EMBABEL_PHONE_HOME"
PHONE_HOME_DOC_URL = "https://github.com/embabel-worlds/appliance/blob/main/PHONE_HOME.md"

# The terms installing accepts. In this repository, next to PHONE_HOME.md and for
# the same reason: the document has to be readable from a terminal session with
# nothing but a URL, by someone who has not signed in anywhere yet.
TRIAL_TERMS_DOC_URL = "https://github.com/embabel-worlds/appliance/blob/main/TRIAL_TERMS.md"
def phone_home_on() -> bool:
    """Whether this appliance reports usage. False unless .env says true."""
    value = (os.environ.get(PHONE_HOME_VAR) or env_file_value(PHONE_HOME_VAR) or "").strip().lower()
    return value in ("1", "true", "yes", "on")


# The opt-in SQL endpoint (world-sql): a Postgres-wire surface onto the world for
# BI, dbt and notebooks. OFF by default — it is a power-user surface, and a default
# install should not open a second port nobody asked for. Enabling it turns on the
# `world-sql` compose profile (see dockerlib.compose_env).
SQL_VAR = "EMBABEL_SQL"


def _switched_on(var: str) -> bool:
    """An opt-in switch read from the environment or .env. False unless it says true."""
    value = (os.environ.get(var) or env_file_value(var) or "").strip().lower()
    return value in ("1", "true", "yes", "on")


def sql_enabled() -> bool:
    """Whether the SQL endpoint runs alongside the worlds mode. False unless .env says true."""
    return _switched_on(SQL_VAR)


# Prometheus and Grafana: OFF by default (embabel-worlds/appliance#112). Together they
# held back 768 MiB of a small machine's memory for dashboards most people never open,
# while docling could not convert a scan in what was left. Enabling it turns on the
# `monitoring` compose profile (see dockerlib.compose_env) and reserves their memory.
MONITORING_VAR = "EMBABEL_MONITORING"


def monitoring_enabled() -> bool:
    """Whether Prometheus and Grafana run. False unless .env says true."""
    return _switched_on(MONITORING_VAR)


def escape_for_env(value: str) -> str:
    """Double every `$`, because compose interpolates values read from .env.

    Measured: a file containing `OPENAI_API_KEY=sk-a$$$b_c` reaches the container
    as `sk-a` — compose reads `$$` as an escaped dollar and then treats `$b_c` as
    a variable reference, warns "The b_c variable is not set", and substitutes a
    blank. A provider key with a dollar in it was therefore corrupted silently,
    and the failure surfaces much later as an authentication error against the
    provider rather than anything pointing here.

    `$$` is compose's own escape for a literal dollar, so doubling is the fix
    rather than quoting, which compose does not treat as significant.
    """
    return value.replace("$", "$$")


def set_env_var(key: str, value: str, why: tuple[str, ...] = ()) -> None:
    """Write `key=value` into .env, preserving everything else there.

    .env rather than a compose edit, always: the compose files stay pull-only, and
    a machine-local answer — a template, a checkout directory, which door you came
    through — is exactly what .env is for. An existing line is replaced where it
    sits, so the file keeps whatever shape its owner gave it; [why] is the comment
    written above a line that is new.
    """
    lines: list[str] = []
    # env_path(), not env_file(): the reader uses the absolute path and this used
    # the bare filename, so the two agreed only while the process happened to be
    # chdir'd into the appliance. The Me app and the CLI both call in from their
    # own directories, where this would have written a stray .env beside whatever
    # the user was standing in.
    if os.path.exists(env_path()):
        with open(env_path()) as f:
            lines = f.read().splitlines()
    written = escape_for_env(value)
    # The LAST assignment is replaced, found by the reader's rules: that is the one
    # compose acts on, and `KEY = value` or `export KEY=` is the same key to it.
    found = [i for i, line in enumerate(lines) if (_dotenv_assignment(line) or ("",))[0] == key]
    if found:
        lines[found[-1]] = f"{key}={written}"
    else:
        lines += ["", *why, f"{key}={written}"]
    with open(env_path(), "w") as f:
        f.write("\n".join(lines) + "\n")
def configured_mode() -> str | None:
    """Which door this appliance was last set up as, from .env.

    The installer's default is Me and the CLI's is Worlds, so without this a
    `embabel down` followed by `embabel up` quietly started the OTHER product on
    the same graph — a person who installed an assistant got a world runtime back,
    with nothing anywhere saying why.
    """
    value = os.environ.get("EMBABEL_MODE", "").strip()
    if value in MODE_COMPOSE:
        return value
    if os.path.exists(env_file()):
        with open(env_file()) as f:
            for line in f:
                if line.strip().startswith("EMBABEL_MODE="):
                    value = line.split("=", 1)[1].strip()
                    return value if value in MODE_COMPOSE else None
    return None
def recorded_mode() -> str | None:
    """The door .env records — what this INSTALL has been set up as.

    Not configured_mode(): that one answers from the environment first, which is right
    for choosing a door and wrong for "has this machine installed before?". The
    documented install names its mode (`EMBABEL_MODE=worlds … | sh`), so on a machine
    that has never installed anything configured_mode() already says "worlds".
    """
    value = (env_file_value("EMBABEL_MODE") or "").strip()
    return value if value in MODE_COMPOSE else None


# The trial terms are accepted once, at first install, and the answer is kept in .env
# so a run interrupted after accepting does not ask again on resume.
TERMS_VAR = "EMBABEL_TERMS_ACCEPTED"


def terms_accepted() -> bool:
    """Whether this install's .env records that the trial terms were accepted."""
    return bool((env_file_value(TERMS_VAR) or "").strip())


def record_terms_accepted(when: str) -> None:
    """Write the acceptance into .env. [when] is the date, YYYY-MM-DD."""
    set_env_var(TERMS_VAR, when, (
        "# When the free trial terms were accepted at install. While this line is",
        "# here setup does not ask again.",
    ))


def remember_mode(mode: str) -> None:
    """Record the door, so the CLI can come back to it rather than to its default."""
    if configured_mode() == mode:
        return
    set_env_var("EMBABEL_MODE", mode, (
        "# The door this appliance was last started as. `embabel up` returns to it",
        "# instead of its own default; starting the other mode rewrites this line.",
    ))
def source_ref() -> str:
    """Which branch or tag this install FOLLOWS — the environment, then .env, then main.

    An install pinned to a branch has to STAY on it. `embabel upgrade` re-downloads the
    checkout, so a ref that lived only in the shell that ran the installer meant the first
    upgrade silently replaced somebody's branch with main — the exact thing they were
    testing, gone, and no line anywhere saying it happened.
    """
    return (os.environ.get("EMBABEL_REF") or env_file_value("EMBABEL_REF") or "main").strip() or "main"


def source_repo() -> str:
    """Which repository this install follows. See [source_ref]; a fork is pinned the same way."""
    return (os.environ.get("EMBABEL_REPO") or env_file_value("EMBABEL_REPO")
            or "embabel-worlds/appliance").strip()


def remember_source(ref: str | None = None, repo: str | None = None) -> None:
    """Record a NON-DEFAULT branch or fork, so upgrades follow it instead of main.

    Only when it is not the default: writing `EMBABEL_REF=main` into every .env would turn
    the ordinary install into one that is pinned, and a pin nobody chose is worse than none —
    it survives the day main is what they wanted.
    """
    ref = (ref or os.environ.get("EMBABEL_REF") or "").strip()
    if ref and ref != "main" and env_file_value("EMBABEL_REF") != ref:
        set_env_var("EMBABEL_REF", ref, (
            "# The branch or tag this install follows. Written because it was installed from",
            "# one — `embabel upgrade` re-reads it, so the checkout stays on that branch.",
            "# Delete this line to go back to main on the next upgrade.",
        ))
    repo = (repo or os.environ.get("EMBABEL_REPO") or "").strip()
    if repo and repo != "embabel-worlds/appliance" and env_file_value("EMBABEL_REPO") != repo:
        set_env_var("EMBABEL_REPO", repo, (
            "# The repository this install follows — a fork or an internal mirror.",
        ))
def version_pin_conflict(mode: str) -> tuple[str, str] | None:
    """`.env` pinning a DIFFERENT image tag from the one this checkout expects.

    The compose files are part of the checkout, so a branch is a whole appliance:
    its plumbing and the server tag it was written against move together, as
    `${EMBABEL_VERSION:-<tag>}`. An EMBABEL_VERSION line in .env overrides that —
    correctly, since an explicit pin should beat a default — but silently, which
    is the problem. Somebody testing a branch would be running its compose files
    against a server image the branch never expected, and nothing on screen would
    say which half was which.

    Returns (pinned, expected) when they disagree; None when there is no pin, or
    it agrees, or the file cannot be read (a missing default is not a conflict).
    """
    pinned = (env_file_value("EMBABEL_VERSION") or "").strip()
    if not pinned:
        return None
    try:
        with open(os.path.join(APPLIANCE_DIR, MODE_COMPOSE[mode])) as f:
            found = re.search(r"\$\{EMBABEL_VERSION:-([^}]+)\}", f.read())
    except (OSError, KeyError):
        return None
    expected = (found.group(1).strip() if found else "")
    return (pinned, expected) if expected and expected != pinned else None


# ── instances ───────────────────────────────────────────────────────────────
#
# ONE APPLIANCE IS THE NORMAL CASE, and nothing about this section should be
# visible to somebody who has one. There is no --instance to learn, no name to
# invent, no flag in `embabel --help`: the default instance is called
# `appliance`, its compose project is `embabel-appliance`, its settings are
# `.env`, and that is the whole story until somebody installs a second one.
#
# A SECOND ONE is what turns the machinery on. Instances differ in exactly three
# things, and everything else follows from them:
#
#   the PROJECT     embabel-<instance> — compose prefixes every volume, network
#                   and container with it, so two instances share nothing by
#                   accident. This is also why no service may declare
#                   container_name: a fixed name is global to the docker daemon
#                   and would collide on the second install.
#   the ENV FILE    .env for the default, .env.<instance> beside it. One
#                   checkout, several settings files — rather than several
#                   checkouts, each with its own copy of this script free to
#                   drift from the others.
#   the PORT BASE   sixteen consecutive host ports, EMBABEL_PORT_BASE + offset.
#                   Allocated when the instance is created, never guessed later.
#
# WHY 11042 (see PORTS below). Babel is Genesis 11. 42 is the Answer, and the
# Babel fish is out of the same book — so the number says the product's name
# rather than punning on one syllable of it. It is unassigned by IANA, absent
# from /etc/services, and sits below the Linux ephemeral floor of 32768, which
# 0xBABE (47806) does not — that one would have failed intermittently forever.

DEFAULT_INSTANCE = "appliance"
# 4242 was inherited and contended: OpenTSDB, Quassel and CrashPlan all default
# there, and CrashPlan takes 4242 AND 4243 — the assistant and the Neo4j browser.
DEFAULT_PORT_BASE = 11042
PORT_BLOCK = 16
# Offset within the block -> the variable the compose files read. Adding a
# service means taking the next free offset here; the spare tail is why there
# are sixteen and not eight.
PORT_OFFSETS = {
    "ASSISTANT_PORT": 0,        # the Me front door
    "WORLDS_PORT": 1,           # the worlds API
    "WORLDS_CONSOLE_PORT": 2,   # the Worlds front door
    "NEO4J_BROWSER_PORT": 3,
    "NEO4J_BOLT_PORT": 4,
    "GRAFANA_PORT": 5,
    "PROMETHEUS_PORT": 6,
    "OPEN_WEBUI_PORT": 7,
    "WORLDS_BOLT_PORT": 8,            # Bolt: Neo4j Browser and drivers, a world or the graph
    "WORLDS_PERSISTED_BOLT_PORT": 9,  # Bolt: persisted mode, the user's own Cypher database
}
# Instance 24 would land on 11434, which is Ollama's — and the compose files
# talk to Ollama there. A ceiling nobody will reach, stated so it cannot be hit
# by surprise.
MAX_INSTANCES = 23
# Which instance THIS process is talking to. Set once, by the CLI, before any
# verb runs. A module-level value rather than a parameter on forty functions
# because it models something true: one process, one appliance.
_instance = DEFAULT_INSTANCE
# What compose accepts as a project name: lowercase, starting alphanumeric, then
# letters, digits, underscore, hyphen. The name also becomes a FILENAME
# (.env.<name>), so the same rule keeps `--instance ../../etc/passwd` from being
# a path and `--instance "my world"` from being two shell words.
INSTANCE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
def use_instance(name: str) -> None:
    """Choose the instance for this process, refusing a name that cannot be one.

    Validated HERE rather than at the flag, because setup.py is also entered
    directly and through the environment — three doors, one gate.
    """
    global _instance
    name = (name or DEFAULT_INSTANCE).strip()
    if not INSTANCE_NAME.match(name):
        raise SetupError(
            f"'{name}' cannot be an instance name. Use lowercase letters, digits, "
            "'-' and '_', starting with a letter or digit — it becomes a docker "
            "project name and a settings filename."
        )
    if len(name) > 40:
        raise SetupError(f"'{name}' is too long for an instance name (40 characters).")
    _instance = name
def instance() -> str:
    return _instance
def compose_project(name: str | None = None) -> str:
    """The compose project, which is the real identity of an instance."""
    return f"embabel-{name or _instance}"
def env_file(name: str | None = None) -> str:
    """Settings for an instance. The default's is plain `.env`, because the
    common case must not be made to look like a special case."""
    name = name or _instance
    return ENV_FILE if name == DEFAULT_INSTANCE else f"{ENV_FILE}.{name}"
def env_path(name: str | None = None) -> str:
    return os.path.join(APPLIANCE_DIR, env_file(name))
def port_base(name: str | None = None) -> int:
    value = env_file_value("EMBABEL_PORT_BASE", name)
    try:
        return int(value)
    except (TypeError, ValueError):
        return DEFAULT_PORT_BASE
def ports_for(base: int) -> dict:
    return {var: base + offset for var, offset in PORT_OFFSETS.items()}
def installed_instances() -> list[str]:
    """Every instance this machine knows about — from its settings file, and
    from docker, because an instance whose .env was deleted still has volumes
    and still answers, and hiding it would be the unhelpful kind of tidy."""
    found = set()
    for entry in os.listdir(APPLIANCE_DIR):
        if entry == ENV_FILE:
            found.add(DEFAULT_INSTANCE)
        elif entry.startswith(f"{ENV_FILE}.") and not entry.endswith((".example", ".before-restore")):
            found.add(entry[len(ENV_FILE) + 1:])
    # LATE IMPORT, and the one cycle in this package. settings tells dockerlib
    # which project to scope to, so dockerlib imports settings; but an instance
    # whose settings file was deleted still has containers, and finding those
    # needs Docker. Importing here rather than at module scope keeps the cycle
    # from existing at import time, and marks the one place it is real.
    from .dockerlib import _docker
    run = _docker("ps", "-a", "--format", "{{.Label \"com.docker.compose.project\"}}", timeout=20)
    if run and run.returncode == 0:
        for line in run.stdout.split():
            if line.startswith("embabel-"):
                found.add(line[len("embabel-"):])
    return sorted(found)
def ensure_port_block() -> int:
    """Give this instance a port block if it does not have one yet.

    Written at CREATION and never recomputed, because a base that is derived
    fresh each run is a base that moves when a sibling instance is removed —
    every bookmark, every registered MCP URL and every `.env` in a realm
    checkout would silently start pointing at a different appliance.
    """
    existing = env_file_value("EMBABEL_PORT_BASE")
    if existing:
        return int(existing)
    # The default instance is the ANCHOR and always owns the first block. Handing
    # it one off the free list let a sibling push the primary appliance off the
    # port every bookmark, MCP registration and realm checkout already names.
    base = DEFAULT_PORT_BASE if instance() == DEFAULT_INSTANCE else free_port_base()
    set_env_var("EMBABEL_PORT_BASE", str(base), (
        "# The sixteen host ports this appliance owns, allocated when it was",
        "# created. Everything else is this + a fixed offset. Do not edit it to",
        "# move one service — set that service's own PORT variable instead.",
    ))
    if instance() != DEFAULT_INSTANCE or base != DEFAULT_PORT_BASE:
        print(f"  Instance '{instance()}' uses ports {base}-{base + PORT_BLOCK - 1}.")
    return base
# The graph engine, as the server spells it. Which one an install runs is decided once
# and written into .env — see pins.py; these only read the answer.
GRAPH_TYPE_VAR = "GRAPH_TYPE"
# What compose falls back to when .env says nothing, and so what every install ran
# before the engine was written down. It stays NEO4J for good: an upgrade runs the
# installed CLI against the new compose files, and that run must not move anybody.
UNPINNED_ENGINE = "NEO4J"
# The Embabel engine was called MEMORY before it had a name of its own. The server still
# accepts that spelling, and so does every reader here, so no .env has to be edited.
ENGINE_ALIASES = {"MEMORY": "EMBABEL"}


def canonical_engine(value: str | None) -> str | None:
    """An engine name as the server reads it, the old spelling folded in; None for blank."""
    value = (value or "").strip().upper()
    return ENGINE_ALIASES.get(value, value) or None


def graph_engine() -> str:
    """The engine this appliance runs — GRAPH_TYPE from the environment or .env, and
    NEO4J when neither says, which is what compose falls back to as well."""
    return canonical_engine(os.environ.get(GRAPH_TYPE_VAR) or env_file_value(GRAPH_TYPE_VAR)) or UNPINNED_ENGINE


def _docker_port_bases() -> set[int]:
    """Port blocks owned by appliance containers, including other checkouts."""
    # Late for the settings <-> dockerlib cycle documented in installed_instances().
    from .dockerlib import _docker
    labels = (
        '{{.ID}}\t{{.Label "com.docker.compose.project"}}\t'
        '{{.Label "com.docker.compose.project.environment_file"}}\t'
        '{{.Label "com.docker.compose.project.working_dir"}}'
    )
    run = _docker("ps", "-a", "--filter", "label=com.docker.compose.project",
                  "--format", labels, timeout=20)
    if not run or run.returncode != 0:
        return set()
    ids = []
    used = set()
    for line in run.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 4 or not fields[1].startswith("embabel-"):
            continue
        container_id, project, environment_file, working_dir = fields
        # A missing settings file can bring the current project through this path.
        # It must be free to reclaim its own block, not reserve that block from itself.
        if (project == compose_project() and working_dir
                and os.path.realpath(working_dir) == os.path.realpath(APPLIANCE_DIR)):
            continue
        ids.append(container_id)
        # Compose records the settings source; its working directory resolves a
        # relative --env-file and identifies conventional .env when none was passed.
        path = environment_file or (os.path.join(working_dir, ENV_FILE) if working_dir else "")
        if path and not os.path.isabs(path):
            path = os.path.join(working_dir, path)
        try:
            value = _env_file_value(path, "EMBABEL_PORT_BASE") if path else None
            used.add(int(value))
        except (OSError, TypeError, ValueError):
            pass
    if not ids:
        return used
    # HostConfig keeps published bindings for stopped containers and is the fallback
    # when Compose's recorded settings path no longer exists.
    run = _docker("inspect", "--format", "{{json .HostConfig.PortBindings}}", *ids, timeout=20)
    if not run:
        return used
    limit = DEFAULT_PORT_BASE + (MAX_INSTANCES + 1) * PORT_BLOCK
    for line in run.stdout.splitlines():
        try:
            bindings = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(bindings, dict):
            continue
        for published in bindings.values():
            for binding in published or ():
                try:
                    port = int(binding["HostPort"])
                except (KeyError, TypeError, ValueError):
                    continue
                if DEFAULT_PORT_BASE <= port < limit:
                    used.add(DEFAULT_PORT_BASE
                             + ((port - DEFAULT_PORT_BASE) // PORT_BLOCK) * PORT_BLOCK)
    return used


def _port_taken(port: int) -> bool:
    """Whether something on this machine already listens on [port], on any address.

    Compose publishes on 0.0.0.0, so a bind there is the question docker will ask; a
    loopback-only listener is caught too, since the wildcard bind collides with it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("0.0.0.0", port))
        except OSError:
            return True
    return False


def _block_has_listener(base: int) -> bool:
    """Whether any port of the block at [base] is already held by a process outside
    Docker. The instances and Docker's own bindings say which blocks appliances use;
    neither sees a dev server or a proxy left running on the host, and a block
    allocated over one installs, then fails at `up` on the port it holds."""
    return any(_port_taken(base + offset) for offset in range(PORT_BLOCK))


def free_port_base() -> int:
    """The next unused block. Bases in use are read from the instances that
    exist rather than counted, so removing the second of three instances frees
    its block instead of stranding it."""
    used = {port_base(name) for name in installed_instances() if name != instance()}
    used.update(_docker_port_bases())
    used.add(DEFAULT_PORT_BASE)  # reserved for the default instance, always
    for n in range(MAX_INSTANCES + 1):
        candidate = DEFAULT_PORT_BASE + n * PORT_BLOCK
        if candidate not in used and not _block_has_listener(candidate):
            return candidate
    raise SetupError(
        f"All {MAX_INSTANCES + 1} port blocks are in use. The next one would collide "
        f"with Ollama on 11434, which this appliance talks to."
    )
def volume_name(key: str) -> str:
    """The real Docker volume name — compose prefixes every volume with the project."""
    return f"{compose_project()}_{key}"
def volume_exists(key: str) -> bool:
    # Late for the settings <-> dockerlib cycle documented in installed_instances().
    # Without it this raised NameError, and so did every backup that asked.
    from .dockerlib import _docker
    run = _docker("volume", "inspect", volume_name(key), timeout=15)
    return run is not None and run.returncode == 0
# What a double-quoted .env value may escape, as compose reads it.
_DOTENV_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"', "$": "$"}


def _dotenv_assignment(line: str) -> tuple[str, str] | None:
    """One .env line as compose reads it: (key, value), or None for anything else.

    THE SAME RULES AS COMPOSE, because the two read the same file and a value they
    disagree on is a value the appliance acts on and the container never sees — or
    the reverse. `GRAPH_TYPE=NEO4J   # pinned` is NEO4J to compose, and was the whole
    tail, comment included, to a reader that only split on `=`: an engine name
    matching nothing. So: `export ` is optional, whitespace around the key and before
    the value is not part of either, a quoted value ends at its closing quote, and an
    unquoted one ends where a `#` follows whitespace. `$$` is an escaped dollar
    everywhere except inside single quotes, which compose does not interpolate.

    Hand-written rather than a library because the installer is standard library
    only, and the format is small and fixed by compose. Multi-line quoted values are
    not read; nothing the appliance writes or reads is one.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export "):].lstrip()
    key, eq, rest = stripped.partition("=")
    key = key.strip()
    if not eq or not key:
        return None
    rest = rest.lstrip(" \t")
    if rest[:1] == "'":
        end = rest.find("'", 1)
        return key, rest[1:end] if end > 0 else rest[1:]
    if rest[:1] == '"':
        out, i = [], 1
        while i < len(rest) and rest[i] != '"':
            if rest[i] == "\\" and i + 1 < len(rest):
                i += 1
                out.append(_DOTENV_ESCAPES.get(rest[i], "\\" + rest[i]))
            else:
                out.append(rest[i])
            i += 1
        return key, "".join(out).replace("$$", "$")
    comment = re.search(r"\s#", rest)
    if comment:
        rest = rest[:comment.start()]
    return key, rest.strip().replace("$$", "$")


def _env_file_value(path: str, key: str) -> str | None:
    """The value compose would read for `key`, unescaped — what was stored is a
    doubled-dollar form for compose's benefit, not the value itself. The LAST
    assignment wins, as it does for compose."""
    if not os.path.exists(path):
        return None
    found = None
    with open(path) as f:
        for line in f:
            parsed = _dotenv_assignment(line)
            if parsed and parsed[0] == key:
                found = parsed[1] or None
    return found


def env_file_assigns(key: str) -> bool:
    """Whether this instance's .env has a line for `key` at all, empty or not. An empty
    one is still somebody's answer — `embabel embeddings off` writes exactly that."""
    if not os.path.exists(env_path()):
        return False
    with open(env_path()) as f:
        return any((_dotenv_assignment(line) or ("",))[0] == key for line in f)


def env_file_value(key: str, name: str | None = None) -> str | None:
    """One value from an instance's settings, or None. Defaults to the current
    instance; `name` is for reading ANOTHER one, which is how the port allocator
    finds out which blocks are already spoken for. The file may already be gone
    during teardown."""
    return _env_file_value(env_path(name), key)


def resume_command() -> str:
    """What to type to pick setup up again, phrased for how this person got here.

    "Re-run this command" is useless to somebody who ran a `curl … | sh`: the
    command they typed downloaded an installer that is now gone from their
    history, and the thing to run is the CLI it left behind. So look for the
    launcher on PATH first, and fall back to the checkout's own entry point —
    named for the mode, because ./worlds.py and ./me.py are different doors.
    """
    if shutil.which("embabel"):
        return embabel_command("up")
    entry = "worlds.py" if configured_mode() == "worlds" else "me.py"
    # The installer reads the instance from the environment, so a second
    # instance's way back in has to carry it or it resumes the default one.
    if _instance != DEFAULT_INSTANCE:
        entry = f"EMBABEL_INSTANCE={_instance} ./{entry}"
    else:
        entry = f"./{entry}"
    if APPLIANCE_DIR != os.getcwd():
        return f"cd {APPLIANCE_DIR} && {entry}"
    return entry


def embabel_command(*argv: str) -> str:
    """The `embabel` command line for this instance, as a person would type it.

    The default instance needs no flag; any other is named, or the command it
    suggests would act on the default one. Before the command is on PATH it is
    the checkout's own launcher, which is always there. Hints that need docker
    compose suggest `embabel compose`, so whatever a person pastes runs with the
    same settings, memory limits and profiles as `embabel up`."""
    launcher = "embabel" if shutil.which("embabel") else os.path.join(APPLIANCE_DIR, "embabel")
    flag = [] if _instance == DEFAULT_INSTANCE else ["--instance", _instance]
    return " ".join([launcher, *flag, *argv])
