"""How much memory each container may use, decided from what Docker has.

WHY EVERY HEAVY SERVICE GETS A LIMIT. With no container limit, every process sizes
itself as if it had the whole VM: the app JVM's MaxRAMPercentage is a percentage of
ALL of Docker's memory, Neo4j sizes its page cache from the same total, and docling
takes whatever a large PDF asks for. When they peak together — an ingest has all
three busy — the VM runs out and the kernel kills the largest process, which is always
the app. That is issue #111: a 7.65GB Docker Desktop, a batch of documents, and the
console reporting a 502 while the app rebooted with no error in its log.

With a limit, each service is sized against its own share, and a spike stays inside
the service that caused it: docling dies on a huge PDF and restarts, and docker
records OOMKilled against the container that actually ran out.

HOW THE SHARES ARE DECIDED. A fixed reserve for the VM and everything uncapped (the
console, sandboxes), a fixed cap for monitoring when it is on, then every sized service gets its
minimum, the rest is split by weight, and a service stops at the maximum past which
more memory buys it nothing — its excess goes to the others. Past every maximum, the
remainder is left unallocated, which is the right answer on a large machine.

The numbers are estimates from one working appliance, not measurements at the
boundary, and are named so they can be moved as better evidence arrives.

ANYTHING SET IN .env OR THE ENVIRONMENT WINS. This only fills in what nobody chose.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache

from .settings import canonical_engine, env_file_value

MIB = 1024 ** 2
GIB = 1024 ** 3

# The VM's own kernel and dockerd, plus everything left uncapped: the console's nginx,
# code sandboxes the app launches, world-sql when enabled.
RESERVE_BYTES = GIB // 2

# Measured idle at 249MB (grafana) and 90MB (prometheus). Fixed, because neither
# grows with how much the appliance is used in any way that matters to one person.
# Held back only when monitoring is on (#112): off, it is memory for the services
# that do the work.
MONITORING_LIMITS = {"GRAFANA_MEM_LIMIT": 384 * MIB, "PROMETHEUS_MEM_LIMIT": 384 * MIB}


@dataclass(frozen=True)
class Share:
    """One sized service: the variable its compose limit reads, and its terms."""
    var: str
    minimum: int
    weight: float
    maximum: int


# The app idled at 2.47GB with an unlimited heap; a single user's chat and ingest do
# not use 8GB. Bounded, it needs far less: at MaxRAMPercentage=45 it peaked at 2.06 GiB
# of a 3.13 GiB limit during a 28-EPUB + 6-PDF bulk ingest, and its live heap after that
# ingest was ~300 MB. Neo4j under 1.75GB cannot hold a 1GB heap and a useful page cache.
# Docling's peak is one document: a 10-K-sized PDF was measured past 6GB, so it gets
# a real ceiling, and on a small machine a large PDF fails conversion rather than the
# app dying. Its minimum is one worker converting one page per request, measured inside
# 2.5 GiB; at 1.85 GiB that same bulk ingest OOM-killed it (embabel/me#1730). The gigabyte
# docling's minimum gained came from the app's.
APP = Share("APP_MEM_LIMIT", minimum=3 * GIB // 2, weight=45, maximum=8 * GIB)
NEO4J = Share("NEO4J_MEM_LIMIT", minimum=7 * GIB // 4, weight=30, maximum=8 * GIB)
DOCLING = Share("DOCLING_MEM_LIMIT", minimum=5 * GIB // 2, weight=25, maximum=8 * GIB)
# On the Embabel engine the graph is in the app's heap, so the app is sized as the app and
# the graph together: the graph's weight joins its own, and its ceiling rises by what the
# graph could have had. Same minimum — an empty graph costs the heap nothing — and an
# estimate in the same sense as the others, to be moved when a large graph is measured.
EMBABEL_APP = Share("APP_MEM_LIMIT", minimum=APP.minimum, weight=APP.weight + NEO4J.weight,
                    maximum=APP.maximum + NEO4J.maximum)

# Inside Neo4j's limit: the heap is a share of it, the page cache is what is left after
# the heap and the process's own off-heap needs (netty, metaspace, APOC).
NEO4J_HEAP_SHARE = 0.4
NEO4J_HEAP_MIN = GIB
NEO4J_HEAP_MAX = 4 * GIB
# Measured, not estimated: ingesting 28 books at once, Neo4j's JVM held ~500 MiB outside
# its heap (metaspace 152, code 70, GC 69, other 104, class 29, threads 10), and its
# page cache sits outside that again. At 384 the heap, page cache and this filled the
# limit exactly, and Neo4j was OOM-killed inside it (#111). The margin is for GC
# structures, which grow with the heap: that ingest only committed half of it.
NEO4J_OVERHEAD = 640 * MIB
NEO4J_PAGECACHE_MIN = 256 * MIB

# Inside docling's limit: its workers. Each docling-serve worker loads its own copy of the
# models and converts its own document, so the limit has to hold every worker at its peak
# at once. Docling's default is 2 at any size, which is how a 1.85 GiB docling was
# OOM-killed twice converting windows of several PDFs together (embabel/me#1730).
# Measured: with default options, 35-130 page PDFs needed more than 3 GiB per worker; one
# worker converting one page per request finished inside 2.5 GiB. 3.5 GiB per worker keeps
# a whole-document conversion (an office file, or a PDF under the app's page window) inside
# the limit, so one worker below 7 GiB, and two at docling's 8 GiB maximum.
# The app caps its requests at the same number (me#1730), so windows beyond it wait in the
# app rather than queueing inside docling against their poll deadline.
DOCLING_BYTES_PER_WORKER = 7 * GIB // 2
DOCLING_WORKERS = "DOCLING_WORKERS"


def docling_workers(limit: int) -> int:
    """How many workers a docling limit holds at their peak together: at least one."""
    return max(1, limit // DOCLING_BYTES_PER_WORKER)


def shares_for(engine: str) -> list[Share]:
    """Which services are sized. With the Embabel engine the graph lives inside the app,
    so its share is the app's to use. FalkorDB is not sized yet: its container runs
    unlimited, and the app and docling split what is left as they would without it."""
    engine = canonical_engine(engine)
    if engine == "NEO4J":
        return [APP, NEO4J, DOCLING]
    return [EMBABEL_APP, DOCLING] if engine == "EMBABEL" else [APP, DOCLING]


def split(budget: int, shares: list[Share]) -> dict[str, int]:
    """Minimums first, the rest by weight, nobody past their maximum.

    A budget below the minimums still gets the minimums: overcommitted limits still stop
    one service from taking another's memory, and refusing to start is not ours to decide.
    """
    sizes = {s.var: s.minimum for s in shares}
    rest = budget - sum(sizes.values())
    open_shares = list(shares)
    while rest > 0 and open_shares:
        total_weight = sum(s.weight for s in open_shares)
        offered = {s.var: rest * s.weight / total_weight for s in open_shares}
        rest = 0
        still_open = []
        for s in open_shares:
            room = s.maximum - sizes[s.var]
            given = min(room, offered[s.var])
            sizes[s.var] += given
            rest += offered[s.var] - given
            if given < room:
                still_open.append(s)
        open_shares = still_open
    return {var: int(size) for var, size in sizes.items()}


def neo4j_memory(limit: int, heap: int | None = None) -> tuple[int, int]:
    """Heap and page cache for a Neo4j limit. A heap set by hand is kept."""
    if heap is None:
        heap = int(min(max(limit * NEO4J_HEAP_SHARE, NEO4J_HEAP_MIN), NEO4J_HEAP_MAX))
    return heap, max(limit - heap - NEO4J_OVERHEAD, NEO4J_PAGECACHE_MIN)


def plan(docker_memory: int, engine: str = "NEO4J", monitoring: bool = False) -> dict[str, int]:
    """Every limit, in bytes, keyed by the variable the compose files read."""
    held = MONITORING_LIMITS if monitoring else {}
    budget = docker_memory - RESERVE_BYTES - sum(held.values())
    limits = {**held, **split(budget, shares_for(engine))}
    if "NEO4J_MEM_LIMIT" in limits:
        heap, pagecache = neo4j_memory(limits["NEO4J_MEM_LIMIT"])
        limits["NEO4J_HEAP"] = heap
        limits["NEO4J_PAGECACHE"] = pagecache
    return limits


_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?)i?b?\s*$", re.IGNORECASE)


def parse_size(text: str) -> int | None:
    """`2G`, `1536m`, `512MiB` — the forms compose and Neo4j accept — as bytes."""
    match = _SIZE.match(text or "")
    if not match:
        return None
    power = " kmgt".index(match.group(2).lower() or " ")
    return int(float(match.group(1)) * 1024 ** power)


def as_size(size: int) -> str:
    """Whole MiB, which both compose and Neo4j read."""
    return f"{size // MIB}m"


def chosen(var: str) -> str | None:
    """What the operator set, if anything."""
    return os.environ.get(var) or env_file_value(var)


def resolve(limits: dict[str, int]) -> dict[str, str]:
    """The plan with every hand-set value kept, as compose-ready strings.

    Neo4j's three numbers are kept consistent with whatever was set by hand: the
    troubleshooting guide tells people to set NEO4J_HEAP=1G, and a limit sized for a
    different heap would give the page cache the wrong remainder. Docling's worker count
    follows its limit the same way, hand-set or not, unless it was set itself.
    """
    resolved = {var: chosen(var) or as_size(size) for var, size in limits.items()}
    if "NEO4J_MEM_LIMIT" in limits:
        limit = parse_size(resolved["NEO4J_MEM_LIMIT"]) or limits["NEO4J_MEM_LIMIT"]
        heap = parse_size(chosen("NEO4J_HEAP") or "")
        heap, pagecache = neo4j_memory(limit, heap)
        pagecache = parse_size(chosen("NEO4J_PAGECACHE") or "") or pagecache
        # A heap and page cache set by hand that do not fit the limit are the
        # operator's stated wish; the limit grows to hold them rather than overruling it.
        needed = heap + pagecache + NEO4J_OVERHEAD
        if not chosen("NEO4J_MEM_LIMIT") and needed > limit:
            resolved["NEO4J_MEM_LIMIT"] = as_size(needed)
        resolved["NEO4J_HEAP"] = chosen("NEO4J_HEAP") or as_size(heap)
        resolved["NEO4J_PAGECACHE"] = chosen("NEO4J_PAGECACHE") or as_size(pagecache)
    if "DOCLING_MEM_LIMIT" in limits:
        limit = parse_size(resolved["DOCLING_MEM_LIMIT"]) or limits["DOCLING_MEM_LIMIT"]
        resolved[DOCLING_WORKERS] = chosen(DOCLING_WORKERS) or str(docling_workers(limit))
    return resolved


@lru_cache(maxsize=None)
def memory_env(engine: str, monitoring: bool) -> dict[str, str]:
    """The limits `docker compose` runs with, or nothing if Docker cannot be asked.

    Cached: every compose call builds its environment, and `docker info` is a round
    trip to the daemon. Nothing is a safe answer — the compose defaults are "no limit",
    which is how the appliance ran before any of this.
    """
    # Imported here: capacity reaches docker through dockerlib, which asks this
    # module for its environment — a top-level import would be a cycle.
    from .capacity import docker_capacity
    have = docker_capacity()
    if not have or not have["memory"]:
        return {}
    return resolve(plan(have["memory"], engine, monitoring))


def describe(env: dict[str, str]) -> str:
    """The split in one line a person reads."""
    names = (("APP_MEM_LIMIT", "app"), ("NEO4J_MEM_LIMIT", "graph"), ("DOCLING_MEM_LIMIT", "docling"))
    parts = [f"{label} {parse_size(env[var]) / GIB:.1f} GB" for var, label in names if var in env]
    if DOCLING_WORKERS in env and "DOCLING_MEM_LIMIT" in env:
        workers = env[DOCLING_WORKERS]
        parts[-1] += f" ({workers} worker{'' if workers == '1' else 's'})"
    return ", ".join(parts)
