#!/usr/bin/env python3
"""Container memory limits are sized from Docker's memory, and a hand-set value wins.

WHY THIS EXISTS. With no limits, every service sized itself against the whole Docker
VM, and an ingest that had the app, the graph and docling busy together ran the VM
out of memory; the kernel killed the app JVM with nothing in its log (#111). The limits
only help if they add up to what Docker has, stay inside each service's minimum and
maximum, and never overrule something the operator wrote in .env.

    python3 scripts/check-memory-sizing.py
"""
import os
import sys
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from embabel_setup import dockerlib, memory  # noqa: E402

GIB = memory.GIB
SIZED = [memory.APP, memory.NEO4J, memory.DOCLING]
failures = []


def check(ok: bool, message: str) -> None:
    if not ok:
        failures.append(message)


def budget(docker_memory: int, monitoring: bool = False) -> int:
    held = sum(memory.MONITORING_LIMITS.values()) if monitoring else 0
    return docker_memory - memory.RESERVE_BYTES - held


# The crash that prompted this: 7.65GB of Docker memory. Every byte of the budget is
# handed out, and every service lands between its minimum and maximum.
small = int(7.65 * GIB)
limits = memory.split(budget(small), SIZED)
check(abs(sum(limits.values()) - budget(small)) <= len(SIZED),
      f"7.65GB: limits {limits} do not add up to the budget {budget(small)}")
for share in SIZED:
    size = limits[share.var]
    check(share.minimum < size < share.maximum,
          f"7.65GB: {share.var}={size} outside ({share.minimum}, {share.maximum})")

# A large machine: everybody at their maximum, the rest left unallocated.
large = memory.split(budget(64 * GIB), SIZED)
check(all(large[s.var] == s.maximum for s in SIZED), f"64GB: not all at maximum: {large}")

# One service capped while the others are not: its excess goes to them.
capped = [memory.Share("A", GIB, 50, 2 * GIB), memory.Share("B", GIB, 50, 100 * GIB)]
spread = memory.split(10 * GIB, capped)
check(spread == {"A": 2 * GIB, "B": 8 * GIB}, f"a capped share's excess is not passed on: {spread}")

# Less than the minimums: the minimums, not a refusal and not a proportional shrink.
tight = memory.split(GIB, SIZED)
check(all(tight[s.var] == s.minimum for s in SIZED), f"below the minimums: {tight}")

# The Embabel engine has no graph container; the graph is in the app's heap, so its share
# and its ceiling are the app's. MEMORY is its old name and sizes the same.
for engine in ("EMBABEL", "MEMORY"):
    check("NEO4J_MEM_LIMIT" not in memory.plan(small, engine), f"{engine} engine was given a neo4j limit")
    check(memory.plan(small, engine)["APP_MEM_LIMIT"] > memory.plan(small)["APP_MEM_LIMIT"],
          f"{engine} engine: the app did not absorb the graph's share")
    check(memory.plan(64 * GIB, engine)["APP_MEM_LIMIT"] > memory.APP.maximum,
          f"{engine} engine on a large machine: the app is capped as if the graph were elsewhere")
check(memory.plan(small, "EMBABEL") == memory.plan(small, "MEMORY"), "MEMORY and EMBABEL size differently")

# Neo4j's page cache is what its limit leaves after the heap and the process itself.
heap, pagecache = memory.neo4j_memory(4 * GIB)
check(heap + pagecache + memory.NEO4J_OVERHEAD == 4 * GIB, f"neo4j 4GB: heap {heap} + cache {pagecache}")

# Neo4j's own non-heap memory was measured at ~500 MiB during an ingest (#111). An
# allowance below that is how it was OOM-killed inside its limit at 384.
check(memory.NEO4J_OVERHEAD >= 512 * memory.MIB, f"NEO4J_OVERHEAD {memory.NEO4J_OVERHEAD} is below the measured need")
for monitoring in (False, True):
    sized = memory.plan(small, monitoring=monitoring)
    check(sized["NEO4J_HEAP"] + sized["NEO4J_PAGECACHE"] + memory.NEO4J_OVERHEAD <= sized["NEO4J_MEM_LIMIT"],
          f"7.65GB, monitoring={monitoring}: neo4j heap + page cache + overhead exceed its limit: {sized}")
    check(sized["NEO4J_PAGECACHE"] >= memory.NEO4J_PAGECACHE_MIN, f"7.65GB: page cache below its minimum: {sized}")

# Monitoring is opt-in (#112): off, nothing is held back for it and the services
# that do the work get that memory; on, both are capped.
off, on = memory.plan(small), memory.plan(small, monitoring=True)
check(not any(var in off for var in memory.MONITORING_LIMITS), f"monitoring off still reserves memory: {off}")
check(all(var in on for var in memory.MONITORING_LIMITS), f"monitoring on does not cap it: {on}")
check(sum(off[s.var] for s in SIZED) - sum(on[s.var] for s in SIZED) >= sum(memory.MONITORING_LIMITS.values()) - len(SIZED),
      "turning monitoring off did not pass its memory to the sized services")


def resolved_with(settings: dict[str, str], docker_memory: int = small) -> dict[str, str]:
    with patch.object(memory, "chosen", side_effect=settings.get):
        return memory.resolve(memory.plan(docker_memory))


# The troubleshooting guide's advice, NEO4J_HEAP=1G, is kept, and the page cache is
# the remainder of the limit around it.
kept = resolved_with({"NEO4J_HEAP": "1G"})
limit = memory.parse_size(kept["NEO4J_MEM_LIMIT"])
check(kept["NEO4J_HEAP"] == "1G", f"hand-set NEO4J_HEAP was replaced: {kept['NEO4J_HEAP']}")
check(memory.parse_size(kept["NEO4J_PAGECACHE"]) == limit - GIB - memory.NEO4J_OVERHEAD,
      f"page cache is not the remainder around a hand-set heap: {kept}")

# A heap too big for the computed limit is the operator's wish: the limit grows.
grown = resolved_with({"NEO4J_HEAP": "6G"})
check(memory.parse_size(grown["NEO4J_MEM_LIMIT"]) >= 6 * GIB + memory.NEO4J_OVERHEAD,
      f"the limit does not hold a hand-set 6G heap: {grown}")

# Any limit set by hand passes through untouched.
check(resolved_with({"APP_MEM_LIMIT": "5g"})["APP_MEM_LIMIT"] == "5g", "hand-set APP_MEM_LIMIT was replaced")

# Docling's workers follow its limit: each loads its own models, so the limit must hold
# all of them at their peak (embabel/me#1730). The appliance that OOM-killed docling had
# 7.65GB and gave docling 1.85 GiB with docling's default of two workers. One worker at
# one page per request was measured inside 2.5 GiB, so that is docling's least.
check(memory.DOCLING.minimum >= 5 * GIB // 2, f"docling's minimum {memory.DOCLING.minimum} is below one worker's measured need")
check(memory.plan(small)["DOCLING_MEM_LIMIT"] >= 5 * GIB // 2, f"7.65GB: docling below 2.5 GiB: {memory.plan(small)}")
# The minimums must fit the machine that prompted them, or the limits overcommit it.
check(sum(s.minimum for s in SIZED) <= budget(small), "7.65GB: the minimums alone exceed the budget")
# The app peaked at 2.06 GiB during that ingest; it must keep at least that on 7.65GB.
check(memory.plan(small)["APP_MEM_LIMIT"] >= int(2.06 * GIB), f"7.65GB: the app below its measured peak: {memory.plan(small)}")
check(resolved_with({})[memory.DOCLING_WORKERS] == "1", f"7.65GB: docling not at one worker: {resolved_with({})}")
check(memory.docling_workers(memory.DOCLING.minimum) == 1, "docling's minimum limit runs more than one worker")
check(memory.docling_workers(memory.DOCLING_BYTES_PER_WORKER * 2 - 1) == 1, "two workers below twice the per-worker need")
check(memory.docling_workers(memory.DOCLING_BYTES_PER_WORKER * 2) == 2, "no second worker at twice the per-worker need")
check(memory.DOCLING_BYTES_PER_WORKER > 3 * GIB,
      f"DOCLING_BYTES_PER_WORKER {memory.DOCLING_BYTES_PER_WORKER} is not above the measured 3 GiB per worker")
check(resolved_with({}, docker_memory=64 * GIB)[memory.DOCLING_WORKERS] == "2",
      f"64GB: docling at its maximum does not run two workers: {resolved_with({}, docker_memory=64 * GIB)}")
# A docling limit set by hand decides the workers; a worker count set by hand wins outright.
check(resolved_with({"DOCLING_MEM_LIMIT": "16g"})[memory.DOCLING_WORKERS] == "4",
      "hand-set DOCLING_MEM_LIMIT=16g did not size the workers from it")
check(resolved_with({"DOCLING_WORKERS": "3"})[memory.DOCLING_WORKERS] == "3", "hand-set DOCLING_WORKERS was replaced")
check("(1 worker)" in memory.describe(resolved_with({})), f"the split does not say docling's workers: {memory.describe(resolved_with({}))}")

# compose_env carries the limits, and asks docker once however often it is built.
memory.memory_env.cache_clear()
calls = []


def capacity():
    calls.append(1)
    return {"memory": small, "cpus": 4, "disk": 0, "disk_where": ""}


with patch("embabel_setup.capacity.docker_capacity", side_effect=capacity), \
        patch.object(memory, "chosen", return_value=None), \
        patch.object(dockerlib, "monitoring_enabled", return_value=False), \
        patch.object(dockerlib, "sql_enabled", return_value=False), \
        patch.object(dockerlib, "github_token", return_value=None):
    env = dockerlib.compose_env()
    dockerlib.compose_env()
    deferred_off = dockerlib.deferred_services()
check(all(var in env for var in ("APP_MEM_LIMIT", "NEO4J_MEM_LIMIT", "DOCLING_MEM_LIMIT", memory.DOCLING_WORKERS)),
      "compose_env does not carry the memory limits and docling's workers")
check(len(calls) == 1, f"docker was asked for its memory {len(calls)} times")
check("monitoring" not in env.get("COMPOSE_PROFILES", ""), f"monitoring profile on by default: {env.get('COMPOSE_PROFILES')}")
check(not set(dockerlib.MONITORING_SERVICES) & set(deferred_off),
      f"monitoring off, yet started by name (which overrides the profile): {deferred_off}")

# On, the profile is added — merged with one the operator set, never replacing it.
with patch("embabel_setup.capacity.docker_capacity", side_effect=capacity), \
        patch.object(memory, "chosen", return_value=None), \
        patch.object(dockerlib, "monitoring_enabled", return_value=True), \
        patch.object(dockerlib, "sql_enabled", return_value=False), \
        patch.object(dockerlib, "github_token", return_value=None), \
        patch.dict(os.environ, {"COMPOSE_PROFILES": "openwebui"}):
    env_on = dockerlib.compose_env()
    deferred_on = dockerlib.deferred_services()
check(env_on.get("COMPOSE_PROFILES") == "openwebui,monitoring", f"monitoring profile not merged: {env_on.get('COMPOSE_PROFILES')}")
check(set(dockerlib.MONITORING_SERVICES) <= set(deferred_on), f"monitoring on, but not started: {deferred_on}")
check("GRAFANA_MEM_LIMIT" in env_on, "monitoring on, but its memory is not capped")
memory.memory_env.cache_clear()

# Every limit the plan computes is read by a compose file; one nobody reads is a
# limit that silently does not apply.
compose_text = "".join(open(os.path.join(ROOT, f)).read()
                       for f in ("infra.yml", "docker-compose-worlds.yml", "docker-compose-me.yml"))
for var in memory.plan(small, monitoring=True):
    check(f"${{{var}:-" in compose_text, f"no compose file reads {var}")

# Docling runs the workers, and the app caps its requests at the same number: both read
# the one variable, or the cap and the workers drift apart.
infra_text = open(os.path.join(ROOT, "infra.yml")).read()
check("DOCLING_SERVE_ENG_LOC_NUM_WORKERS=${DOCLING_WORKERS:-" in infra_text, "docling does not read DOCLING_WORKERS")
for app_file in ("docker-compose-me.yml", "docker-compose-worlds.yml"):
    text = open(os.path.join(ROOT, app_file)).read()
    check("ASSISTANT_INFRASTRUCTURE_DOCUMENTPIPELINE_DOCLING_MAXCONCURRENTREQUESTS=${DOCLING_WORKERS:-" in text,
          f"{app_file}: the app's docling request cap does not read DOCLING_WORKERS")

if failures:
    print("\n".join(f"FAIL: {f}" for f in failures))
    sys.exit(1)
print("ok: container memory limits and docling's workers are sized from Docker's memory, and hand-set values win")
