#!/usr/bin/env python3
"""Drive concurrent load at a running appliance, and check it is still RIGHT while it is slow.

Pointed at an instance's API port, this fires a ladder of rising concurrency at the caller-facing
surfaces — a saved view's invoke, ad-hoc Cypher through the scoped engine — and reports a latency
curve beside an exactness check. See `.claude/skills/appliance-load/SKILL.md` for what the numbers
mean and the traps that make a load run lie.

Three things here are deliberate, because each one is a way a load test reports a comfortable
number that is not true:

  - GROUND TRUTH FIRST. One sequential call establishes what the answer IS; every concurrent
    response is then compared against it. A load test that only times things cannot see the
    only failure that matters, which is a wrong answer under contention.
  - REQUESTS ARE RELEASED TOGETHER. A rung whose requests trickle measures nothing: the first
    finishes before the last begins. A barrier holds every worker until all of them are ready,
    so the concurrency named is the concurrency applied.
  - A FAILED REQUEST IS DATA. Past saturation some requests will not answer. Aborting on the
    first loses the questions that matter most — what the rest answered, and whether the box is
    still correct afterwards.

Stdlib only: this has to run on a checkout with nothing installed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Long enough that a throttled appliance is recorded as slow rather than as broken: the driver's
# connection-acquisition default is 60s, so a request CAN legitimately take about that long under
# saturation, and a client timeout below it would invent failures the appliance did not have.
REQUEST_TIMEOUT_S = 90.0

# The uncontended calls — the ground truth, and the search for a loadable view — get a much
# shorter leash. A target that cannot answer in this long with nothing else happening is not a
# load target, and finding that out should cost seconds rather than a minute per candidate.
PROBE_TIMEOUT_S = 20.0


def env_file(instance: str) -> Path:
    """The settings file for an instance — `.env` for the default one, `.env.<name>` otherwise."""
    return REPO / ".env" if instance in ("", "appliance") else REPO / f".env.{instance}"


def env_value(path: Path, key: str) -> str | None:
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        m = re.match(rf"^\s*{re.escape(key)}\s*=\s*(.*?)\s*$", line)
        if m:
            return m.group(1)
    return None


def api_base(instance: str, override: str | None) -> str:
    """The instance's API origin. The port block's base + 1 is the API, as the install prints."""
    if override:
        return override.rstrip("/")
    base = env_value(env_file(instance), "EMBABEL_PORT_BASE")
    if not base:
        sys.exit(
            f"No EMBABEL_PORT_BASE in {env_file(instance)}. Pass --base http://127.0.0.1:<port>, "
            f"or name an instance that is installed (`embabel instances`)."
        )
    return f"http://127.0.0.1:{int(base) + 1}"


class Client:
    def __init__(self, base: str, user: str, password: str, token: str | None):
        self.base = base
        if token:
            self.auth = f"Bearer {token}"
        else:
            raw = base64.b64encode(f"{user}:{password}".encode()).decode()
            self.auth = f"Basic {raw}"

    def call(
        self, method: str, path: str, body: dict | None = None, timeout: float = REQUEST_TIMEOUT_S
    ) -> tuple[int, bytes, float]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": self.auth,
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if data else {}),
            },
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read(), (time.perf_counter() - started) * 1000
        except urllib.error.HTTPError as e:
            return e.code, e.read(), (time.perf_counter() - started) * 1000
        except Exception as e:  # noqa: BLE001 — a transport failure is an outcome, not a crash
            return 0, str(e).encode()[:300], (time.perf_counter() - started) * 1000


# Per-call by design, so they must never reach a comparison. Leaving them in is not a
# theoretical risk: the first run of this script reported every response to a view as WRONG,
# including the uncontended one it was compared against, purely because `executionId` and
# `metrics.durationMs` differ on every call and the view's `data` was null rather than [].
VOLATILE_FIELDS = ("executionId", "operationId", "metrics", "timestamp", "freshness", "durationMs")


def fingerprint(payload: bytes, compare: str = "exact") -> str:
    """A stable identity for an answer, so two responses can be compared without false alarms.

    Rows are hashed as a SORTED multiset: `collect()` has no order, so a view returning the same
    rows in a different order is the same answer. An envelope with no rows at all — a refusal, an
    error — is hashed whole, because a refusal must be compared as exactly as a result, but only
    after [VOLATILE_FIELDS] are removed.

    `compare="rows"` compares the COUNT alone. Weaker, and the right choice for a view over a
    genuinely live source whose rows may legitimately differ between two calls: it still catches
    the failures that matter most under load — rows duplicated, and rows gone.
    """
    try:
        doc = json.loads(payload)
    except Exception:  # noqa: BLE001
        return "unparseable:" + hashlib.sha256(payload).hexdigest()[:16]
    rows = None
    if isinstance(doc, dict):
        # `data` PRESENT but null is an empty answer, not an unrecognised envelope.
        for key in ("data", "rows"):
            if key in doc:
                rows = doc[key] or []
                break
    if not isinstance(rows, list):
        stable = {k: v for k, v in doc.items() if k not in VOLATILE_FIELDS} if isinstance(doc, dict) else doc
        return "envelope:" + hashlib.sha256(
            json.dumps(stable, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]
    if compare == "rows":
        return f"rows={len(rows)}"
    canonical = sorted(
        json.dumps({k: v for k, v in r.items() if k not in VOLATILE_FIELDS} if isinstance(r, dict) else r,
                   sort_keys=True, default=str)
        for r in rows
    )
    digest = hashlib.sha256("\n".join(canonical).encode()).hexdigest()[:16]
    return f"rows={len(rows)}:{digest}"


class Target:
    """One request shape to put under load, and how to recognise its right answer."""

    def __init__(self, name: str, method: str, path: str, body: dict | None):
        self.name = name
        self.method = method
        self.path = path
        self.body = body

    def once(
        self, client: Client, compare: str, timeout: float = REQUEST_TIMEOUT_S
    ) -> tuple[int, str, float, str, bool | None]:
        status, payload, ms = client.call(self.method, self.path, self.body, timeout)
        return (
            status,
            fingerprint(payload, compare),
            ms,
            payload[:300].decode("utf-8", "replace"),
            cache_hit(payload),
        )


def cache_hit(payload: bytes) -> bool | None:
    """Whether the appliance served this from cache, as its own metrics block reports.

    Load over one unchanging target measures the WARM path after the first response — the view
    envelope says `cacheHit` outright, so a run that does not report it is quietly presenting
    cache-hit latency as the cost of answering.
    """
    try:
        doc = json.loads(payload)
        return bool(doc["metrics"]["cacheHit"])
    except Exception:  # noqa: BLE001
        return None


class Outcome:
    __slots__ = ("ms", "status", "fp", "detail", "cached")

    def __init__(self, ms: float, status: int, fp: str, detail: str, cached: bool | None = None):
        self.ms, self.status, self.fp, self.detail, self.cached = ms, status, fp, detail, cached

    @property
    def ok(self) -> bool:
        return self.status == 200


def run_rung(client: Client, target: Target, concurrency: int, compare: str) -> list[Outcome]:
    """Fire `concurrency` requests released together, and collect every outcome."""
    gate = threading.Barrier(concurrency, timeout=60)
    results: list[Outcome] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            gate.wait()
        except threading.BrokenBarrierError:
            return
        status, fp, ms, detail, cached = target.once(client, compare)
        with lock:
            results.append(Outcome(ms, status, fp, detail, cached))

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for f in [pool.submit(worker) for _ in range(concurrency)]:
            f.result()
    return results


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(p / 100 * (len(ordered) - 1)))]


def other_containers() -> int:
    """How much else is running on this box — results are not comparable across different answers."""
    try:
        out = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True, timeout=20
        )
        return len([n for n in out.stdout.splitlines() if n.strip()])
    except Exception:  # noqa: BLE001
        return -1


def pick_view(client: Client, chosen: str | None) -> str | None:
    """A view to load. Declared-and-parameterless is not enough — it has to ANSWER.

    Picking the first view that merely exists found one that took over a minute uncontended,
    which says nothing about load and costs a timeout to discover. So candidates are tried, on
    the short leash, and the first that answers is the target.
    """
    if chosen:
        return chosen
    status, payload, _ = client.call("GET", "/api/v1/admin/kg/views", timeout=PROBE_TIMEOUT_S)
    if status != 200:
        return None
    try:
        doc = json.loads(payload)
    except Exception:  # noqa: BLE001
        return None
    views = doc if isinstance(doc, list) else doc.get("views") or doc.get("data") or []
    tried = 0
    for v in views:
        name = v.get("name") if isinstance(v, dict) else v
        # A view with no required parameter is the one a harness can call without inventing a value.
        if isinstance(v, dict) and (v.get("parameters") or v.get("params")):
            continue
        if not name or tried >= 3:
            continue
        tried += 1
        status, _, _ = client.call("POST", f"/api/v1/views/{name}/invoke", {}, PROBE_TIMEOUT_S)
        if status == 200:
            return name
        print(f"  (skipping view {name}: uncontended invoke returned {status})")
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instance", default="appliance", help="instance name (default: appliance)")
    ap.add_argument("--base", help="API origin, overriding the instance's port block")
    ap.add_argument("--user", default="demo")
    ap.add_argument("--password", default="terminat8r")
    ap.add_argument("--token", help="bearer token instead of Basic (EMBABEL_MCP_API_TOKEN)")
    ap.add_argument("--ladder", default="1,4,16,64", help="concurrency rungs (default 1,4,16,64)")
    ap.add_argument("--rounds", type=int, default=2, help="times to repeat the whole ladder")
    ap.add_argument("--view", help="saved view to invoke (default: the first parameterless one)")
    ap.add_argument("--cypher", default="MATCH (n) RETURN count(n) AS n", help="ad-hoc query to load")
    ap.add_argument(
        "--compare",
        choices=("exact", "rows"),
        default="exact",
        help="how a response must match the uncontended answer: every row (default), or the row count "
             "only — which is what a view over a genuinely live source needs",
    )
    ap.add_argument("--json", help="write the report here")
    args = ap.parse_args()

    rungs = [int(x) for x in args.ladder.split(",") if x.strip().isdigit() and int(x) > 0]
    if not rungs:
        sys.exit("--ladder named no positive concurrency")

    base = api_base(args.instance, args.base)
    client = Client(base, args.user, args.password, args.token)

    status, _, _ = client.call("GET", "/api/v1/admin/kg/schema")
    if status == 401:
        sys.exit(f"401 from {base} — wrong credentials for this instance (see the install's output).")
    if status == 0 or status >= 500:
        where = f"--base {args.base}" if args.base else f"instance '{args.instance}'"
        sys.exit(f"{base} is not answering ({status}). Is it up? ({where}; try `embabel instances`)")

    targets = [
        Target("cypher", "POST", "/api/v1/admin/kg/execute", {"cypher": args.cypher}),
    ]
    view = pick_view(client, args.view)
    if view:
        targets.insert(0, Target(f"view:{view}", "POST", f"/api/v1/views/{view}/invoke", {}))
    else:
        print("! no parameterless saved view found — loading Cypher only. Pass --view to add one.")

    pool_max = env_value(env_file(args.instance), "NEO4J_POOL_MAX") or "50 (default)"
    report: dict = {
        "instance": args.instance,
        "base": base,
        "neo4jPoolMax": pool_max,
        "otherContainersOnBox": other_containers(),
        "ladder": rungs,
        "rounds": args.rounds,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "compare": args.compare,
        "targets": {},
    }

    print(f"\nappliance load — instance={args.instance} base={base} pool-max={pool_max}")
    print(
        f"{'target':<22} {'conc':>5} {'ok':>5} {'bad':>4} {'wrong':>6} "
        f"{'p50':>8} {'p95':>9} {'max':>9} {'cached':>7}"
    )

    wrong_anywhere = 0
    for target in targets:
        # GROUND TRUTH, uncontended, before any load: what the right answer is.
        status, truth, ms, detail, _ = target.once(client, args.compare, PROBE_TIMEOUT_S)
        if status != 200:
            print(f"{target.name:<22} SKIPPED — uncontended call returned {status}: {detail[:120]}")
            report["targets"][target.name] = {"skipped": f"{status}: {detail[:200]}"}
            continue
        rows: list[dict] = []
        for _ in range(args.rounds):
            for conc in rungs:
                outs = run_rung(client, target, conc, args.compare)
                good = [o for o in outs if o.ok]
                wrong = [o for o in good if o.fp != truth]
                wrong_anywhere += len(wrong)
                lat = [o.ms for o in good]
                row = {
                    "concurrency": conc,
                    "ok": len(good),
                    "failed": len(outs) - len(good),
                    "wrongAnswers": len(wrong),
                    "p50Ms": round(percentile(lat, 50), 1),
                    "p95Ms": round(percentile(lat, 95), 1),
                    "maxMs": round(max(lat), 1) if lat else 0.0,
                    "meanMs": round(statistics.fmean(lat), 1) if lat else 0.0,
                    "failureKinds": sorted({f"{o.status}:{o.detail[:80]}" for o in outs if not o.ok}),
                    "cacheHits": sum(1 for o in good if o.cached),
                    "cacheReported": sum(1 for o in good if o.cached is not None),
                }
                rows.append(row)
                flag = "  <-- WRONG" if wrong else ""
                cached = (
                    f"{row['cacheHits']}/{row['cacheReported']}" if row["cacheReported"] else "-"
                )
                print(
                    f"{target.name:<22} {conc:>5} {row['ok']:>5} {row['failed']:>4} "
                    f"{row['wrongAnswers']:>6} {row['p50Ms']:>7}ms {row['p95Ms']:>8}ms "
                    f"{row['maxMs']:>8}ms {cached:>7}{flag}"
                )
        report["targets"][target.name] = {"groundTruth": truth, "uncontendedMs": round(ms, 1), "rungs": rows}

    report["wrongAnswers"] = wrong_anywhere
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
        print(f"\nreport -> {args.json}")

    if wrong_anywhere:
        print(f"\nFAIL: {wrong_anywhere} response(s) differed from the uncontended answer.")
        if args.compare == "exact":
            print(
                "If a target reads a LIVE source its rows may differ between two calls for reasons "
                "that are nothing to do with load. Re-run it with --compare rows, and say so in the "
                "report: a count-only check is weaker, and still catches rows duplicated or lost."
            )
        return 1
    print("\nEvery answer under load matched the uncontended one. Latency above is the finding.")
    print(
        "`cached` is how many of the OK responses the appliance served from cache. A high count "
        "means the curve is the warm path: say so, or vary the target's parameters to pay for the "
        "cold one."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
