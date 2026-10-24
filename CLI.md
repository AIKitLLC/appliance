# The `embabel` command

The appliance as a verb rather than a directory. Everything here was already
possible — as `cd ~/embabel/worlds && ./worlds.py`, `./setup.py --uninstall`,
`docker compose -f docker-compose-worlds.yml ps` — which asks you to remember
where the product lives and which compose file today's mode uses.

It is installed onto your `PATH` by the installer, as a two-line forwarder to the
checkout's own CLI. Updating the checkout updates the command; there is no second
copy to drift.

```bash
curl -fsSL https://raw.githubusercontent.com/embabel-worlds/appliance/main/install.sh | EMBABEL_MODE=worlds sh
```

The `embabel` command goes in `~/.local/bin`. If that is not already on your
`PATH` — it is not, on macOS — the installer adds it to your shell profile in a
marked block that `embabel uninstall` takes back out. New terminals have the
command; for the one you are in, `exec $SHELL`. Set `EMBABEL_BIN_DIR` to choose
the directory yourself, and the installer leaves your profile alone.

---

Typing `embabel` on its own prints status and the verbs worth knowing next.

## The short version

```bash
embabel up            # start it, and finish setup if it has not been set up
embabel status        # what is running, what is still downloading, where to go
embabel doctor        # why it is not working
embabel open          # the console, in your browser
embabel backup        # everything it knows, copied somewhere safe
embabel version       # tag, digest, the commit it was built from
embabel bugreport     # one folder to attach to an issue, with no secrets in it
embabel sample add …  # fictional records, marked so they can be taken back out
embabel scenario run … # put the world in a named state, for a demo or a repro
embabel sandbox build  # a code-mode sandbox with your own toolchain in it
embabel embeddings use local  # turn document search on
embabel trust add ca.crt  # trust your company's certificate authority
```

---

## Reference

### `embabel up`

Start the appliance and complete first-run setup. **Safe to run at any time** —
a running mode is reconciled with the compose file rather than started twice, and
a completed setup says so instead of asking again.

| Flag | |
|---|---|
| `--worlds` | the world runtime and its console |
| `--me` | the personal-assistant door |
| `--fresh` | **delete all data first** (asks), then start over |

With neither flag it starts **the mode this machine is already running, or was last
set up as** — recorded as `EMBABEL_MODE` in `.env` the first time. Without that
record, `embabel down` then `embabel up` handed an assistant user a world runtime
on the same graph and said nothing about it. The installer now opens the Worlds
door too, which narrows the gap without closing it: `EMBABEL_MODE=me ... | sh`
installs an assistant, and this has to come back to one. Worlds is the fallback
for a machine that has set up neither.

The first run pulls roughly 0.8 GB before handing the terminal back, then
continues downloading the rest — the code sandbox, metrics, and structured
document conversion — behind you. `embabel status` says what is still arriving.

### `embabel status`

Splits what is running from what is still on its way, because during the first
quarter of an hour "not everything is up" is the normal state and a flat
container list cannot tell that apart from broken.

It ends with every surface of whichever mode is up: for Worlds the console, the
API, the MCP endpoint, the graph browser and dashboards (when monitoring is on); for Me the assistant
itself, its MCP endpoint and the graph.

### `embabel doctor`

Checks the things that have actually gone wrong for somebody, and says what to do
about each. It prints the appliance directory first, because that is what it is
reporting on.

- Docker installed, running, and Compose v2 present
- **Docker Model Runner** — embeddings run locally and need it
- whether this directory has been set up
- whether realm checkouts are linked, and whether that path is one the appliance
  can actually see (a path outside Docker Desktop's file sharing mounts **empty**,
  with nothing in any log to say why)
- stray code-sandbox containers left by a JVM that died without its shutdown hook

### `embabel logs [service]`

The appliance's own log by default; name a compose service for any other.

| Flag | |
|---|---|
| `-f`, `--follow` | follow |
| `--tail N` | lines of history, default 200 |

### `embabel compose [--me|--worlds] <args…>`

`docker compose` for this appliance, run exactly as `embabel up` runs it: its
project, settings file, compose files and overlays, port block, memory limits
and profiles. `embabel compose ps`, `embabel compose --me up -d assistant`.
Use it instead of a bare `docker compose`, which acts on the default appliance
and starts containers with no memory limits.

### `embabel open [what]`

Open a surface in your browser: `console`, `graph`, `dashboards`, `me`. With
nothing named it opens this appliance's front door — the console on Worlds, the
assistant on Me.

### `embabel realms link <directory>`

Point the appliance at the directory your realm checkouts live **in** — the
parent, so adding another realm is a `git clone` rather than a change to any
config.

The path is checked before it is written: one that does not exist, is not
readable, is a realm rather than a directory of realms, or is somewhere Docker
Desktop does not share, is refused with the reason. What it found is printed.

```
  Realm checkouts: /Users/you/dev
  4 realms visible: realm-esg, realm-github, realm-legal, realm-stripe
```

The mount is **read-only, by design**. You edit on the host — where your editor,
your coding agent and your git remote already are — and the appliance only reads.
A world then loads one by path instead of by repo:

```yaml
# config/realms.yml, in the world
- name: esg
  path: /realms/realm-esg
```

One consequence to know rather than discover: a realm's declared npm/wasm build
runs as part of cloning, so it never fires for a local realm. A declarative realm
needs nothing; a realm with a build step must be built on the host first.

### `embabel realms list`

Which realms the appliance can currently see.

### `embabel version`

Which appliance this is, in the four layers that can actually differ between
two installs. There is no single number, and printing one would be a lie:

| | |
|---|---|
| **Checkout** | this repo's commit — the pin for everything in files rather than images: the compose files, `setup.py`, the skills |
| **Server** | the image **tag** as compose resolves it, and the **digest** that tag currently means. `EMBABEL_VERSION` defaults to a snapshot tag, so the tag is a name; the digest is the artifact |
| **Built from** | the commit the server's jar was built from, read out of `git.properties` inside it — with the branch, the subject line, and whether the build carried **uncommitted changes** |
| **Graph** | the Neo4j image and its digest — or, on the Embabel engine, that the graph is inside the server, whose version is its version |

A build from a dirty tree says so, loudly, because its commit names where the
build *started* rather than what is in it. A build old enough to carry only an
abbreviated SHA is marked `(abbreviated only)` rather than printing seven
characters as though they were an answer.

**It does not call the server.** `/actuator/info` carries the same build and git
blocks, but it is authenticated, and the moment anyone needs the version is the
moment the appliance will not boot, is wedged, or is halfway through an upgrade
— when an endpoint answers nothing. This reads the image and the jar instead,
and works with the container stopped (a little slower: it starts a throwaway
container to read from the image).

Reading the jar is cheap despite the jar being ~400MB: the zip index lives at
the end of the file, so it takes the tail, finds one entry's offset, and reads
a couple of hundred bytes.

| Flag | |
|---|---|
| `--json` | the same four layers, as JSON |

`embabel backup` records exactly this in each backup's `manifest.json`, so a
year-old backup can still say what wrote it.

### `embabel sample`

Fictional records, loaded into a live world and removable in one move.

```bash
embabel sample add hubspot-demo      # a name, a file, or gh:owner/repo
embabel sample list                  # what is loaded, and what is mixed with real records
embabel sample remove hubspot-demo   # that set, and nothing else
embabel sample clear                 # everything fake, before somebody sees the screen
```

The appliance marks every node a set loads, so removing it is exact rather than a
best guess at what came from where. **Sample data is the only thing this product
deletes** — removing a realm leaves its records, and so does deleting a world — which
is what makes `remove` and `clear` safe to run without reading anything first.

A bare name resolves only inside the `embabel-worlds` org, the same rule realms follow:
a short name in a mailed instruction must not be squattable. `owner/name` and full URLs
work too, and show whose data you are about to load before you load it.

Sets are JSON: this client is stdlib-only and runs on whatever `python3` you have, and a
YAML dependency would be a package to install before the first command works. A `.yml`
set is read when PyYAML happens to be installed, and says so plainly when it is not.

```json
{ "name": "hubspot-demo", "realm": "hubspot",
  "nodes": [
    {"label": "Company", "id": "acme", "displayName": "Acme Pty",
     "properties": {"website": "https://acme.example", "revenue": 84000}}
  ],
  "edges": [] }
```

`realm` is required. It is how a set refuses to load when the realm that gives its types
meaning is absent, instead of creating records nothing can interpret.

Loading the same set twice merges rather than duplicates, so a set is safe to re-run when
something did not land and there is an audience.

### `embabel sample export`

Records back out, as a set somebody else can load.

```bash
embabel sample export --source hubspot-demo --realm hubspot -o demo.json
embabel sample export --labels Company,Deal --realm hubspot -o case-1174.json
embabel sample export --source hubspot-demo --realm hubspot --with-values -o real.json
```

**Select by `--source` where you can.** `--labels Company` takes every Company in the
world — the three you assembled for a demo and every real account beside them. Shape-only
redaction limits what leaks, not how much noise comes with it, and ten thousand blanked
companies is a useless reproduction. `--source` names exactly what one set loaded, which
is what makes "I fiddled until the demo looked right" capturable.

An export with neither is refused: it would be the whole world.

**Truncation is reported.** A caller who asks for 500 and receives 500 cannot otherwise
tell a complete answer from a cut-off one, and an export that looks finished and is not is
worse than one that failed — somebody sends it and then wonders why the reproduction will
not reproduce.

```
✓ Wrote 1 node(s) to t.json
TRUNCATED: 3 record(s) matched, 1 written.
Narrow it with --source <set>, or raise --limit.
```

**Shape-only by default**: labels, property keys and edges are kept, and the values are
replaced with blanks of the same type — a number stays a number, so a query that sorts or
counts still behaves in whoever's world it lands in. Ids are replaced too, because they
are routinely email addresses.

That default is the point. Most support reproductions need the shape and the query rather
than the content, and the safe choice should not be the one you have to remember to make.
`--with-values` gives the real thing and says so; read that file before sending it
anywhere.

What comes out is exactly what `embabel sample add` takes, so there is no conversion step
between exporting and loading, and therefore none to get wrong.

### `embabel run-view [name]`

Run a saved view by name. Bare, it lists what there is to run and what each one wants
passed.

```
embabel run-view
embabel run-view risky_dependencies --arg minScorecard=4
embabel run-view triage_dependencies --arg policy='no unpatched CVEs' --json | jq '.data'
```

A saved view is a named, parameterised, server-owned query — validated when it was
saved, and cached where it can be. Every typed client generated against an appliance is
a nicer way to make this same call; the shell is the consumer that will never have a
generated client, and it is the one CI, cron and a person at a terminal actually are.

| Flag | |
| --- | --- |
| `--arg NAME=VALUE` | one parameter; repeat for more |
| `--args-json JSON` | all of them as one object. `--arg` wins over it |
| `--json` | the whole result envelope, for a pipe |

Values go up as typed. The appliance coerces each to the parameter's **declared** type
and refuses what it cannot, naming the parameter — so there is no second, disagreeing
copy of that rule here. A parameter listed as `required` declares no default and must be
supplied.

**It reads the envelope before the rows, and so should you.** The verb distinguishes an
honest empty from a failure, because a client that prints `0 rows` for both teaches you
to believe the second one:

- `EMPTY` — the view ran and there is genuinely nothing. Exit 0. This is an answer.
- `SOURCE_UNAVAILABLE` — a backing source could not be reached. Exit 1. **Not** "no data".
- `PARTIAL` — a source capped its result; treat any count as a floor.
- Warnings print above the rows whatever the outcome, because a full-looking table drawn
  from a degraded source is the failure worth catching.

With `--json` the exit code still follows the outcome, so `embabel run-view … --json` is
safe to put in a CI step without parsing what it printed.

Running goes through the appliance's **calling tier** (`/api/v1/views/{name}/invoke`),
which never returns the underlying query and resolves identity from the authenticated
principal alone. Listing uses the admin path, because discovery needs each view's
declared parameters — a question an operator may ask and an application may not.

An intelligence view — one whose body carries an `{ai: {…}}` directive — can take its
steer as an ordinary parameter, so the judgement is yours at the point of calling:

```
embabel run-view triage_dependencies \
  --arg policy='Tolerate unmaintained packages, never an unpatched CVE.'
```

Such a view cannot be materialised (a cache is keyed per user, not per argument tuple),
so every call is a fresh model hop. Expect it to be slow and to cost something.

### `embabel diagram`

The world as an entity-relationship diagram, in Mermaid: every entity with its
typed columns and keys, every view as its own shape, a relationship wherever a
column carries another entity's key, and a mark on every column a model made.

```
embabel diagram                       # prints Mermaid; paste it where Mermaid renders
embabel diagram --out world.mmd       # or write it to a file
embabel diagram --door http://host:15480
```

It is **re-derived from the world's declarations on every read**, so it cannot
rot the way a drawn diagram does, and a diff of two runs is a diff of what
changed in the world. A view appears when it projects an entity's declared key,
dashed, because a view is a reading about that entity rather than a record of
it; a producer-backed label, which has no extent, is listed at the bottom rather
than drawn as a table that would fan out.

The drawing is done by the world-graphql sidecar, the same one that serves the
world as GraphQL, because the catalog it is drawn from lives in the doors rather
than in this installer. `--door` or `EMBABEL_GRAPHQL_DOOR` says where it is;
the default is a sidecar on this machine at port 15480, and the verb says how
to start one when none answers. Your appliance login is forwarded to it, the
way every door forwards a login: `EMBABEL_USER` / `EMBABEL_PASSWORD`, or the
password is asked for.

### `embabel contract generate --view <name>`

Draft an ODCS v3.1 data contract describing what one of your saved views returns.

```
embabel contract generate --view account_health
```

That reads the view's declaration, runs nothing, writes nothing, and prints the contract
for you to read. Three further flags each buy one more step, and none of them is on by
default:

| Flag | What it adds |
|---|---|
| `--sample` | runs the view once, under a row cap, to infer column types |
| `--save` | writes the draft into the world's `config/contracts` |
| `--bind` | pins the view to it in `observe` mode (implies `--save`) |
| `--output <file>` | writes the YAML to a file instead of printing it |

The listing marks every column with where its entry came from, because that is the
distinction the whole thing turns on:

```
    ~ account                      string       sampled  suggests required, unique
    ✓ last_seen                    —            declared
```

`✓ declared` was read from the view as written and is true of it. `~ sampled` was inferred
from one run and might not hold tomorrow — which is why an inferred type is written into
the contract but an inferred *constraint* is not. "This column was never null in 500 rows"
becomes a suggestion a person confirms, never a promise the appliance starts enforcing.

Nothing this command does can withhold anybody's rows. A generated contract is `draft`
and a binding it creates is `observe`, which records verdicts and returns every row.
Moving either to `active` / `enforce` is an edit you make by hand, once you believe it.

It refuses rather than guesses. A view that returns `*`, a bare node, or two columns with
the same name comes back as a refusal naming what to change — because a contract that is
wrong and later enforced is worse than no contract at all.

### `embabel scenario`

Put the world in a named state, from wherever it is now.

```bash
embabel scenario list                     # what exists, and which one you are in
embabel scenario run pipeline-at-risk     # bring the world to that state
embabel scenario next                     # the one after the one you are in
embabel scenario capture pipeline-at-risk # freeze the world as it is now
embabel scenario run … --dry-run          # say what would change, change nothing
```

A scenario **declares** what should be loaded rather than listing steps:

```json
{ "name": "pipeline-at-risk", "order": 2, "description": "a deal has stalled",
  "wants":   ["accounts-base", "deals-at-risk"],
  "without": ["pipeline-healthy"] }
```

Running it works out the difference and does the minimum — adds what is missing, removes
what should not be there, leaves everything else alone.

Declared rather than scripted, because `sample add X && sample remove Y` works right up
until somebody is watching. Then a question from the room means a step gets skipped, or
re-shown, or half-applied, and every later line of a script of CHANGES assumes a state its
predecessor no longer produced. A declaration asserts the state, so jumping straight to
the fourth scenario from anywhere lands correctly, and re-running one that is already
current does nothing.

There is no saved position. `next` works out where you are by looking at what is loaded,
because a remembered "you are on step 3" is a second source of truth that goes wrong the
moment somebody loads a set by hand — and goes wrong silently.

**`capture` is how scenarios actually get made.** Nobody writes one first: you load a set,
load another, remove the one that was wrong, look at the screen, and only then know what
you wanted. Capture turns that arrangement into something repeatable.

```
✓ Wrote scenarios/pipeline-at-risk.json
   wants:   accounts-base, deals-at-risk
   without: pipeline-healthy
```

`without` is inferred, and it is the part that matters: every set the OTHER scenarios name
that is not loaded here, so this one knows what to clear away when somebody arrives from a
sibling. Recording only `wants` would give a scenario that adds correctly and never
removes anything — which shows up as yesterday's data still on screen halfway through a
demo. A captured scenario is ordered after everything that exists, so it appends to the
walk rather than inserting itself into somebody's sequence, and it refuses to overwrite an
existing file without `--force`.

Scenarios are `.json` files in `./scenarios`, ordered by their `order` field and then by
name. A set named in `wants` is looked for beside the scenario (`scenarios/sets/<name>.json`)
before the ordinary rules apply, so a scenario and the data it needs travel together.

This is not only for demos, which is why the verb is not `demo`: the same move puts a
world into a fixed state to evaluate a realm before connecting an account, to reproduce a
support case, or to start a test from somewhere known.

### `embabel embeddings`

Whether documents can be indexed, and with what.

```bash
embabel embeddings show          # on or off, and why
embabel embeddings use local     # ~1.1GB, runs here, nothing leaves the machine
embabel embeddings use hosted    # uses the provider key you already gave
embabel embeddings off           # no model; document features go off
```

**An appliance ships without an embedding model.** The local one is about 1.1GB and needs
Docker Model Runner, which is a Docker Desktop feature — so requiring it made every first
run pay for a capability many people never touch, and ruled out plain Docker Engine
entirely, where `docker model` is not a command at all.

So document features are **off, not broken**, and the difference is the point: upload
answers `409` with one sentence naming the command that fixes it, rather than accepting a
document it cannot index or returning an empty search that looks like an empty world.
Everything else — the graph, realms, views, handlers, chat — works untouched.

First-run setup **offers** this and defaults to **no**, so knowing the capability exists
costs nothing.

**The choice is sticky.** Vectors already stored were made by whichever model made them,
and a vector index is built at that model's width — so changing the model re-embeds
everything already indexed. The appliance does that properly (drop the indexes, re-embed
each store, rebuild at the new width, roll back on failure), but it is not free, and a
first choice on an empty appliance is the cheap moment to make it.

`use local` composes in `embeddings-local-<mode>.yml`, which is the only thing that
requires Model Runner. Without it, nothing in the compose files mentions it.

`use hosted` writes a ROLE, not a model name, and the difference is load-carrying. The
appliance image ships no provider starters, so a model name like `text-embedding-3-small`
matches nothing registered and resolves to the setup-required placeholder — document
features would stay off with nothing saying why. A role is resolved per call against
whichever key the appliance holds, so it means "embed with the key I gave you" and keeps
meaning that if you later swap providers. `use openai` still works and sets the same role;
so does typing a known hosted model name.

### `embabel trust`

Certificate authorities the appliance trusts in addition to the public ones.

```bash
embabel trust add ~/Downloads/company-root-ca.crt   # trust it, and restart the app
embabel trust list                                  # what is trusted, with fingerprints
embabel trust remove company-root-ca                # stop trusting it, and restart the app
```

You need this when an address works from your terminal and not from the appliance,
and the console says *a secure connection could not be established*. Your company
signs with its own authority: a model gateway on an internal address, or a network
that inspects outbound traffic and re-signs everything. Your machine trusts that
authority because somebody installed it in the system keychain. The appliance runs in
a container, which has only the public list.

Hand it the **authority** — the root, or the whole chain as exported — in PEM or DER.
A server's own certificate is refused: it says who the server is, not who vouches for
it, and trusting it stops working the day it is renewed. From a chain, only the
authorities are kept.

Two ways to get the file, if nobody has handed you one:

```bash
security find-certificate -a -c "<authority name>" -p > company-ca.crt          # from the macOS keychain
openssl s_client -connect <host>:443 -showcerts </dev/null > company-ca.crt     # from the server itself
```

The second yields whatever your network presents, which on an inspected network is
exactly the authority wanted — and is why the fingerprint is worth checking. Every
refusal of a file prints these two commands.

`add` prints each authority's name and SHA-256 fingerprint. Check the fingerprint
against the one your IT team publishes: whoever adds an authority decides whose word
the appliance takes for every connection it makes.

The certificates live in `certs/` in the appliance directory, and that folder is the
whole of the state — nothing is written to `.env`, and an upgrade leaves it alone.
`add` and `remove` recreate the app container (your data is untouched) and then check
that the running app holds the certificate. If it does not, the image predates
certificate import and `embabel upgrade` is the fix.

### `embabel sandbox`

The container code-mode runs in, and how to make it yours.

```bash
embabel sandbox show     # which image, why, and where it came from
embabel sandbox build    # build sandbox/Dockerfile and use it
embabel sandbox reset    # back to the shipped image
```

The shipped sandbox carries the runtimes code-mode executes — Python with the data
stack, Node and TypeScript, git, jq, sqlite, graphviz — and nothing else. Every
appliance downloads it before anybody has run a line of code, so a JDK, Maven, Prolog
and PlantUML were removed: roughly 900MB that most installs never used.

If your work needs them, the appliance builds a sandbox for you:

```bash
cp sandbox/Dockerfile.example sandbox/Dockerfile
$EDITOR sandbox/Dockerfile          # uncomment the JDK block, add what you need
embabel sandbox build
embabel down && embabel up
```

**Extend, do not rewrite.** The example starts `FROM` the shipped image, so you inherit
whatever it gains — a new runtime, a security update — instead of maintaining a fork
that quietly falls behind. A full rewrite is still yours to make; it is just not what
this encourages.

The build writes `EMBABEL_SANDBOX_IMAGE` into `.env`, which is the one name that moves
**both** halves: the pre-pull in `infra.yml` and the container the app launches for a
session. Setting one without the other pulls a sandbox nobody uses and then fetches a
different one mid-chat.

Nothing is pushed anywhere — the image is tagged locally and stays on your machine.

A single world can override this further with `sandboxDockerimage` in its
`config/world.yml`, which wins over the appliance-wide setting: the narrower scope means
it.

### `embabel backup [directory]`

Everything the appliance knows, copied to a folder on the host: a cold tarball
of each volume the appliance's graph engine keeps data in (the data volume, and Neo4j's on a Neo4j appliance), this machine's `.env`, `secrets.env` and mounts
override, and a manifest recording when it was taken, from which images, at
which checkout commit.

Backups go under `~/embabel-backups` unless you name a directory — deliberately
**outside the checkout**, because `embabel uninstall` deletes the checkout and a
backup an uninstall removes is not a backup. Each run makes its own timestamped
folder, so backing up twice never overwrites the first one.

**The copy is cold.** Whichever mode is running stops for it and starts again
afterwards — including on a failure, so a backup that goes wrong is never the
reason your assistant is down. This is not caution: a graph copied while it is
live restores as a corrupt graph — Community Neo4j has no online backup, and the
Embabel engine's SQLite file is torn the same way mid-write.

The volume bytes never cross a bind mount — a helper container tars them to
stdout and the CLI streams that to the file — so Docker Desktop's file-sharing
list has no opinion about where a backup may live, and an external disk works.

| Flag | |
|---|---|
| `--list` | what backups are already in that directory, newest first |

The folder holds credentials: the database password, provider keys, realm
tokens. Treat it like the keys it holds — the `README.txt` written beside them
says so too.

### `embabel restore <directory>`

Put a backup back, **replacing what is on this machine**: the graph, the worlds,
the documents, and this machine's configuration. Everything added since the
backup was taken is gone. It asks first, and names the backup's date rather than
its folder — the date is what you are actually confirming.

What gets replaced is set aside rather than deleted: one `.before-restore` file
per config file, kept until the next restore overwrites it. A
`docker-compose.override.yml` the Me app did not write is a **refusal**, not a
set-aside — a hand-written override is somebody's work and a restore does not
eat it.

Restoring onto a machine with no volumes yet works, and is slow the first time:
compose has to create them, which means pulling the images.

| Flag | |
|---|---|
| `--yes` | skip the confirmation |

### `embabel ps`

What this appliance has on the host, in the three groups that fail differently:
its own containers, the deferred extras (allowed to be missing for the first
quarter of an hour, and not a fault), and **code sandboxes**.

Sandboxes are the reason this verb exists. They are created by the server
through the docker socket as *siblings* of the appliance rather than as compose
services, so `down` does not take them and `docker compose ps` cannot see them.
Until now they were visible only as one line inside `doctor`.

| Flag | |
|---|---|
| `--json` | the same, as JSON |

### `embabel prune`

Remove code-sandbox containers left on the host. Asks first, and says the thing
that makes it safe to answer: **if you run an assistant from an IDE, its
sandboxes are in this list too.** They carry the same label and nothing here can
tell an orphan from a live session, so it names them and lets you decide.

Sandboxes only — deliberately not `docker system prune`, and not dangling
images. This runs on developer machines where most of what Docker considers
garbage belongs to somebody else's work, and a cleanup verb that reaches beyond
its own project is one people learn not to run.

| Flag | |
|---|---|
| `--yes` | skip the confirmation |

### `embabel bugreport [directory]`

One folder to attach to an issue, instead of six rounds of "and can you also
send…". It holds `doctor`, `status` and `version` exactly as they printed,
`versions.json`, the container list, what Docker says about itself and its disk,
and per-container logs.

**What is deliberately not in it.** This appliance holds someone's email,
contacts and documents, so a diagnostic bundle is an exfiltration shape if it is
careless. Two rules, both enforced in code rather than left to a warning:

- **`.env` values are never copied.** `env-keys.txt` lists the keys and whether
  each holds anything — "is `OPENAI_API_KEY` set" is a real diagnostic question;
  "what is it" never is. A key that is empty and a key that is absent are
  different bugs, so both are reported.
- **Logs are filtered to warnings, errors and stack traces.** An INFO line from
  this server can carry a document title, a contact's name, or the text of a
  query somebody typed.

The bundle is left unpacked beside its `.zip` so it can be read before it is
sent. A bundle you cannot inspect is one people send blind, or not at all.

| Flag | |
|---|---|
| `--all-logs` | full logs, not just warnings and errors — the bundle's `README.txt` then says so in the first paragraph |

### `embabel reset-password`

Forgot the password. Recreates the operator account and **keeps every byte of
data** — the account lives in two small files under the volume's admin
directory, and this deletes exactly those two. The appliance refuses to reopen
setup over its API by design, permanently; whoever controls Docker on the host
already has this authority, which is why it is sound rather than a back door.

Was previously reachable only as `setup.py --reset-password`, so someone locked
out had to know to go around the CLI.

### `embabel completion <bash|zsh|fish>`

Tab completion, **generated from the parser** rather than written beside it — a
hand-maintained completion script is a list of verbs that quietly stops matching
the ones that exist, which is worse than none, because it teaches people the
command lacks a verb it has.

```bash
source <(embabel completion bash)                       # ~/.bashrc
embabel completion zsh > "${fpath[1]}/_embabel"         # zsh
embabel completion fish > ~/.config/fish/completions/embabel.fish
```

Verbs, their flags, and positional choices (`open console`, `realms link`) all
complete. Flags hidden from `--help` stay hidden here too.

### `embabel upgrade`

Onto the latest published build: **the checkout and the images**. Your data is
untouched — this is the opposite of `up --fresh`.

Both halves, because for a long time this moved only the images while the Me
app's menu moved both, and the two doors meant different things by the same
word. `setup.py` owns it now, so they cannot drift again.

- **`--ff-only`, always.** A dirty or diverged checkout is reported and left
  alone; the images still update. It names what changed, and tells you to run
  `npm --prefix me-app run build` when the pull touched `me-app/`, since nothing
  in the run path rebuilds `dist/`.
- **It verifies.** The image digest is read before and after, then the
  container's actual image id is checked — "pulled" and "the container is
  running it" are different claims.
- **The guides follow.** The appliance keeps its own documentation indexed in
  your world: it reads `guides.json` from the repository this checkout follows
  when it starts and once a day after, indexes guides that are new or changed,
  and removes ones that were withdrawn. It only ever removes documents it put
  there, so nothing you added is touched. No command, and no password.
- **It builds nothing.** The compose files are pull-only by design. If the
  published image turns out to be *older* than a local build it just replaced,
  it says so — that is what `upgrade` means, but a local build vanishing in
  silence costs somebody an afternoon.

### Working with more than one appliance

**You will not meet this until you install a second one.** With one appliance
there is no `--instance` in `embabel --help` and no `instances` verb — the flag
exists and works, but it stays out of the help until it means something.

```bash
embabel --instance client up      # a second appliance, on the next port block
embabel instances                 # every one installed here, and its ports
EMBABEL_INSTANCE=client embabel logs -f
```

An instance is a compose project (`embabel-<name>`), a settings file (`.env` for
the default, `.env.<name>` beside it), and a block of sixteen ports allocated
when it is created. With two installed, any verb that could act on either
**asks** instead of guessing.

You choose the name: lowercase letters, digits, `-` and `_`, starting with a
letter or digit. It becomes a Docker project name and a filename, so anything
else is refused rather than mangled. The default is `appliance`.

Everything scopes: `backup` writes `embabel-backup-<instance>-<timestamp>` and
records the instance in its manifest, `restore` says so when a backup came from
a different one, `uninstall` removes only the instance you name (and keeps the
`embabel` command while any other remains), and `prune` touches only the current
instance's sandboxes. Every compose command run on an instance's behalf, including
the services that finish downloading in the background after the first `up`,
carries that instance's project, settings file and ports, so installing or
working on one never recreates another's containers.

### `embabel down`

Stop the appliance, keeping everything. `embabel up` brings it back.

| Flag | |
|---|---|
| `--wipe` | also **delete all data** (asks first) |

### `embabel uninstall`

Undo the installation: the appliance's state, this machine's configuration —
`.env`, the shared-folder override, the MCP registration whose token died with the
volume — and **the `embabel` command itself**, taken back off your PATH.

That last one only removes the launcher THIS installation wrote. `embabel` is not a
rare name, and setup warns when another one already comes first on your PATH;
uninstall reads the file before deleting it and leaves anything it did not write
alone. If another `embabel` still answers afterwards, it says so — otherwise the next
`which embabel` finds a hit and the uninstall looks like it failed.

It also offers to remove stray code-sandbox containers. They are created by the
app through the Docker socket as siblings of the appliance rather than as compose
services, so `down` never sees them. It **asks** rather than assumes, because an
assistant you are running from an IDE owns containers carrying the same label.

Two things it deliberately keeps:

- **Images and the local embedding model.** The embedding artifact alone is over a
  gigabyte, and re-downloading one that has not changed is waste. There is no flag
  to remove them.
- **Your realm checkouts.** They are your repositories.
- **The installation directory.** `~/embabel/worlds` (or wherever you put it) stays,
  so `./worlds.py` sets up again from it. Delete the directory yourself when you
  want it gone — this script does not remove the ground it is standing on.

### `embabel where`

Print the appliance directory.

---

## Working on a realm

The loop the CLI exists to make short:

```bash
embabel realms link ~/dev        # once
```

then, from a coding agent connected over MCP:

```
install_realm_from_path("realm-esg")   # once — by reference, nothing is cloned
… edit the files in ~/dev/realm-esg with your own tools …
realm_validate_path("realm-esg")       # would it load?
realm_refresh()                        # the world re-reads it
kg_query(…)                            # does it answer?
```

and `git push` from the checkout, to your own GitHub. Nothing in that requires the
appliance to write to your files, which is why the mount is read-only.

`realm_brief` over MCP explains the realm format and points at the
`realm-authoring` skill that walks through building one.

---

## Platforms

| | |
|---|---|
| **macOS** | Supported. `embabel` lands in `~/.local/bin`, which macOS does not put on `PATH` — so the installer adds it to `~/.zshrc` (or your shell's profile) in a marked block, and uninstall removes it. Docker Model Runner is a Docker Desktop feature and optional: without it, `embabel embeddings use hosted`. |
| **Linux** | Supported. Model Runner needs the `docker-model-plugin` package rather than a Desktop toggle. `~/.local/bin` is usually already on `PATH`. `embabel open` uses `xdg-open`; on a headless box it prints the URL and opens nothing, which is the useful behaviour over ssh. |
| **Windows** | **WSL2 only** — see below. |

### Windows, specifically

Run the installer and `embabel` inside a **WSL2** distribution — Microsoft's Linux
environment for Windows — with Docker Desktop's WSL integration enabled for it.
Docker Desktop already uses WSL2 as its engine by default, so anyone running
Docker on Windows almost certainly has it.

This is not Windows support so much as Linux support that Windows users can
reach: the installer is a POSIX shell script and the CLI assumes POSIX paths, so
neither runs in PowerShell. A native port would mean a PowerShell installer and a
CLI that understands Windows paths — a piece of work, not a flag.

Two things that will bite otherwise:

- **Keep the appliance inside the Linux filesystem** — `~/embabel/worlds`, not
  `/mnt/c/...`. Crossing the Windows/Linux filesystem boundary is dramatically
  slower, and it costs most where it hurts most: realm checkouts, which a coding
  agent reads and rewrites in a tight loop. Clone your realms under your WSL home
  directory too, and point `embabel realms link` at that.
- **`localhost` forwards through to Windows**, so `http://localhost:11044` opens
  the console in an ordinary Windows browser. `embabel open` tries `wslview` for
  exactly this.

Two things that are macOS-specific by nature rather than by neglect:

- **Docker Desktop's file sharing.** `embabel realms link` warns when a path is
  outside the shared list, because such a mount resolves **empty** with nothing in
  any log to explain it. Linux has no equivalent restriction, so the check does not
  run there.
- **The Me app**, the menu-bar sensor, is macOS today. The appliance itself runs
  anywhere Docker does, including a Linux server.

---

## Environment

The CLI reads the same `.env` as everything else. The variables it cares about:

| Variable | |
|---|---|
| `EMBABEL_REALMS_DIR` | the parent of your realm checkouts, mounted read-only at `/realms` |
| `EMBABEL_MODE` | `me` or `worlds` — the installer reads it, and `embabel up` returns to it |
| `EMBABEL_HOME` | where the installer puts the appliance, default `~/embabel/worlds` |
| `EMBABEL_BIN_DIR` | where the installer puts `embabel`, default `~/.local/bin` |

Everything else is in [`.env.example`](.env.example), which is the reference for
ports, provider keys, and tuning.
