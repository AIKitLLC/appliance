---
name: appliance-load
description: Put a running appliance under concurrent load and find where it stops answering well — a fresh instance, a ladder of rising concurrency against views, Cypher and the MCP doors, with every answer checked for correctness while it is slow. Use before a release, after anything that touches the engine's materialization or cleanup, the connection pool, or the web tier, and every so often by manual trigger to see whether the curve has moved.
---

A development skill for this repo. It does not ship: the product skills in `skills/` teach users'
agents, and `relentless-testing` already owns correctness against ground truth on a quiet box.
This one asks the other question — what happens when a lot of people ask at once — because
nothing else does, and because an appliance under load fails in a way that reads like success.

Not a benchmark suite. `-Pbenchmark` in the `me` repo (`VirtualCypherLoadBenchmarkTest`, the
`load-*` family) measures the ENGINE in one JVM with a faked source. This measures the APPLIANCE:
HTTP, auth, the web tier, the connection pool and the engine together, which is the only shape a
user meets. When a number here is bad and the engine family's is fine, the layer between them is
the answer.

## Load a fresh instance, never a shared one

Every appliance is an INSTANCE with its own compose project and its own block of sixteen ports, so
`EMBABEL_INSTANCE=<name>` stands one up beside the developer's (`fresh-appliance` has the install
in detail, including the headless setup-token path and the standard `demo` / `terminat8r`
credentials). Use it. Saturating a shared appliance is not a measurement:

- it has whatever realms and data someone left on it, so the figure describes that box and
  nothing else, and is not comparable to the last run;
- other sessions on this machine are using it, and 256 concurrent runs is a denial of service
  to them;
- and `embabel --instance <name> uninstall` makes the whole thing disposable, which is what lets
  a run be repeated honestly rather than argued about.

**Record what else is running.** `scripts/load-appliance.py` counts the containers on the box into
its report, because a laptop running Odoo, Lago, three Neo4js and two other appliances is not the
machine that produced last month's curve. Two runs are comparable only when that number is.

## What the appliance actually does under load

Calibration first, because it decides what a run should watch. These are the behaviours this
script can observe from outside the appliance, measured 2026-10-17:

- **It THROTTLES long before it fails.** The graph connection pool is modest — tens of
  connections, tunable with `NEO4J_POOL_MAX` — and the driver's connection-acquisition timeout is
  its default, which is a full SIXTY SECONDS. So when more requests arrive than the pool can
  serve, they queue rather than erroring. At 128 concurrent engine runs every one still answered
  CORRECTLY, just slowly (p50 around 2.3s); outright failures (`pending acquisition queue is
  full`) began somewhere between 128 and 256.
- **So an error-rate alarm will call the appliance healthy at a p95 of a minute.** Watch
  percentiles, and watch p95 detaching from p50 — that gap is the queue forming. The cliff is
  real, but it is a long way past the point where a person has given up and refreshed.
- **Saturation is inferred, not observed.** The running appliance exposes no connection-pool
  gauges, so there is no in-use/idle/timed-out count to read. What you have is latency,
  the acquisition failures in the container log, and the pool size you set. Do not claim to have
  measured the pool when you measured its symptoms.
- **Raising `NEO4J_POOL_MAX` is not free.** The graph has to serve those connections, so a bigger
  pool moves the bottleneck rather than removing it. If a run says the pool binds, that is a
  finding to take to whoever owns the server's sizing, with the curve attached.

## The traps

Every one of these has produced a believable number that was false, most of them in the run that
wrote this file.

- **Correctness is the assertion; latency is only recorded.** A load run that times things and
  checks for HTTP 200 cannot see the failure that matters. The engine's own concurrency bugs were
  duplicated rows (27 where 16 exist) and vanishing ones (`SUCCEEDED` with 1 row, then 0) — both
  of which are 200s. `load-appliance.py` takes an uncontended answer first and compares every
  concurrent response against it.
- **Volatile fields fake a failure.** `executionId` and `metrics.durationMs` differ on every call,
  and a view's `data` can be `null` rather than `[]`. Comparing whole payloads reported every
  single response as wrong, including the uncontended one it was compared against. The script
  strips them; anything else that compares answers must too.
- **A target that cannot answer uncontended is not a load target.** One of the first views tried
  took over the probe timeout with nothing else happening. Establish the ground truth on a short
  leash, and skip and SAY SO rather than loading something that was already broken.
- **Concurrency has to be forced.** Requests that trickle measure nothing — the first finishes
  before the last begins. The script releases every worker from a barrier, so the concurrency it
  names is the concurrency it applied. The same trap voids a naive `for` loop or a ramp.
- **A failed request is data, not a reason to stop.** Past saturation some will not answer; the
  run must carry on and report how many, of what kind, and whether what did answer was right.
- **`cacheHit` IN THE ENVELOPE IS NOT A CACHE HIT**, and believing it is the most expensive
  mistake available here. The server sets it from `apiCalls == 0 && semanticCalls == 0 && error ==
  null`, so it is true for ANY view over stored or virtual data whether a single thing was cached
  or not. The first run of this skill duly reported a view as 64/64 "cache hits" and called its
  curve the warm path — while that view was in fact paying a full probe, materialise, read and
  cleanup cycle on every one of those calls. The same misreading cost somebody ten seconds of
  re-derivation they could not find (embabel/me#2028, which added `DerivationMeter` because the two
  numbers an author had both pointed away from the cost). The script now reports the field as
  `no-ext`, meaning "reached nothing outside the appliance", which is what it says. To learn
  whether work was actually SKIPPED, read the duration against an uncontended call and look at the
  appliance log for what the run did — materialised node counts and sweep timings are there.
- **A cold box has a lower knee, so let it settle.** The first run of this skill saw wrong answers
  and failures at 256 concurrency; re-run on the same instance once it had settled, 256 was
  completely clean and only 512 failed. Nothing was fixed in between. Warming up (JIT, caches,
  pools filling) lowers the concurrency at which an appliance starts refusing, so a number taken
  minutes after setup describes the warm-up, not the appliance. Record how long since setup or
  boot beside the curve, and do not compare a cold run to a settled one. Separately: for about
  12-19 seconds after `/api/v1/setup/complete` returns, the appliance does not answer AT ALL —
  honest, but a script that calls straight on from `/complete` will see a dead socket.
- **Latency is noisy; counts are not.** Run to run on one laptop moved p50 by ~15%, which was
  enough to make a 14% "improvement" evaporate on repetition. Trust call counts, row counts and
  failure counts; treat a latency delta under about 20% as nothing until it repeats.

## The ladder

Climb in order. A rung that fails stops the climb: diagnose (`realm-doctor` for realm symptoms,
`appliance-doctor` for the box), fix, and start again, because the fix may change what load does.

**L0 — a fresh instance, healthy.** Installed beside, every container up, the app container
*healthy* and not merely running. `fresh-appliance`'s L0–L1 is the gate; there is no point loading
a box that is not serving.

**L1 — the uncontended cost of everything you will load, on a SETTLED box.** One call each,
timed, correct. This is the denominator: a p50 at concurrency 32 means nothing without it, and a
target that is already slow here is a different bug. Let the instance settle first and say how long
you waited — a cold appliance has a measurably lower knee (see the traps), so a ladder begun
straight after setup measures the warm-up.

**L2 — the ladder, on the main door.** `scripts/load-appliance.py --instance <name> --ladder
1,4,16,64 --rounds 2`. Saved-view invoke (`POST /api/v1/views/{name}/invoke`) and ad-hoc Cypher
(`POST /api/v1/admin/kg/execute`), released together, every answer compared. Read it as a curve,
not a number: where does p50 leave the uncontended cost behind, and where does p95 detach from
p50? The second is the queue forming.

**L3 — past the knee.** Keep raising concurrency until requests fail, and record where. Raising
`NEO4J_POOL_MAX` is the first thing anyone will suggest; measured, it converts refusals into
waiting rather than making anything faster, and at the appliance it did not help at all (512
concurrency, 50 -> 200: failures went 121 -> 182). So record the pool you ran at, and do not
reach for it as a fix before knowing the failure is the graph refusing work. At the
default pool size that has been somewhere between 128 and 256 concurrent engine runs; an appliance
that falls over much earlier has something else wrong, and one that sails past it is worth
understanding too. Record the pool size you ran at beside the number, or the number means nothing.
The pass is not "no failures" — it is that failures arrive as refusals and the appliance is still
CORRECT and still serving afterwards.

**L4 — the MCP doors under load.** Agents reach the appliance through `/mcp/chat` and `/mcp/code`,
not through REST, and a door handshakes per connection. Drive concurrent tool calls (a cheap one —
`available_capabilities`, a view run) and check the same two things: every answer right, and the
door still handshaking afterwards. **Handshake each door by hand first** (`fresh-appliance` L1.5):
load numbers from a door you have not proved are worthless. This rung is thinner than the others
and the script does not yet cover it — say so in the report rather than implying it passed.

**L5 — nothing left behind.** The one rung that is pure correctness, and the reason this skill
exists rather than a stopwatch. After the load has drained and the box is quiet: ask the world the
same questions again and get the uncontended answers; check no virtual nodes outlived their runs
(`MATCH (n:Virtual) RETURN count(n)` should be the idle baseline, not a pile); confirm the app
container is still healthy and the log has no abandoned-cleanup ERRORs. Load that leaves residue
overstates counts for every query that comes after it, long after the load has gone.

**L6 — tear down.** `embabel --instance <name> uninstall`. An instance holds sixteen ports and a
set of volumes; a skill that leaves one behind every month is how a machine runs out of both.

## Report, and grow the list

Report the ladder as climbed: each rung, pass or the exact failure. Give the curve as a table with
the uncontended cost beside it, the pool size it was measured at, the `no-ext` ratio, and the
container count on the box — a figure without those four is not reproducible and should not be
quoted later.

**What was NOT loaded gets stated with the same prominence.** A door not driven, a cold path not
paid for, a rung skipped for a missing credential. The temptation here is worse than in a
correctness skill, because a load report full of numbers looks complete.

Findings go in the repo or as a GitHub issue, never committed into this public repo as diagnostics.
And the list is append-only in spirit: any way an appliance has ever failed under load that this
ladder missed becomes a new rung, and any new way a run can lie becomes a new trap.
