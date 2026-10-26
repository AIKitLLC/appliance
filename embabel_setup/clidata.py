"""What the world knows: realms, sample data, scenarios, and coding agents.

The verbs that change what is IN the world rather than whether it is running. Sample
data is the only thing any of them deletes — removing a realm leaves its records, and
so does deleting a world.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys

from .cli import _sample_target, current_mode, run_setup, s
from .core import SetupError
from .settings import embabel_command
from .core import prompt

def cmd_realms(args) -> int:
    if args.realms_command == "link":
        return run_setup("worlds", "--realms", args.directory)
    # list: what the appliance can actually see, which is the question worth asking.
    realms = os.environ.get("EMBABEL_REALMS_DIR")
    env_path = s.env_path()
    if not realms and os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                if line.startswith("EMBABEL_REALMS_DIR="):
                    realms = line.split("=", 1)[1].strip()
    if not realms:
        print(f"  No realm checkouts linked.  {embabel_command('realms')} link <directory>")
        return 0
    path, found, notes = s.inspect_realms_dir(realms)
    if not path:
        for note in notes:
            print(f"  {note}")
        return 1
    s.announce_realms(path, found, notes)
    return 0


def cmd_agents(args) -> int:
    """Point Claude Code and Codex at THIS appliance, again.

    WIRING ONLY EVER HAPPENED ONCE, during first-run setup, and the setup API
    closes permanently when setup completes — so there was no second chance. Every
    way that goes wrong left the operator editing client config by hand:

      · an appliance installed before the port rebase left a registration aimed at
        4242, and the next install could not replace it (`claude mcp add` refuses a
        name that exists, and this printed the refusal rather than acting on it)
      · a second appliance, or a reinstall, mints a new token and the old one in the
        client stops authenticating
      · somebody declined at setup and changed their mind

    The token is not re-minted here, and cannot be: the server returns it exactly
    once. It is READ BACK from the data volume, which is where the appliance keeps
    it for its own use — so this re-registers the credential that is actually live
    rather than inventing a rival one.
    """
    mode = current_mode() or s.configured_mode()
    if not mode:
        print(f"  Nothing is running.  `{embabel_command('up')}` starts it.")
        return 1
    container = s.find_mode_container(mode)
    base = s.container_base_url(container) if container else None
    if not container or not base:
        print(f"  The appliance is not up.  `{embabel_command('up')}` starts it.")
        return 1

    run = s._docker("exec", container, "sh", "-c",
                    "cat /data/embabel/assistant/admin/providers.env 2>/dev/null")
    token = user = None
    for line in (run.stdout if run and run.returncode == 0 else "").splitlines():
        if line.startswith("EMBABEL_SETUP_MCP_TOKEN="):
            token = line.split("=", 1)[1].strip()
        elif line.startswith("EMBABEL_SETUP_MCP_TOKEN_USER="):
            user = line.split("=", 1)[1].strip()
    if not token:
        print("  This appliance has no MCP token — it was never minted, or MCP was declined.")
        print("  " + s.dim("A token is minted during first-run setup and cannot be re-issued afterwards."))
        return 1

    if args.show_token:
        print(token)
        return 0

    print(f"  Wiring agents to {base}/mcp/code" + (f" as {user}" if user else ""))
    s.wire_coding_agents({
        "token": token,
        "url": f"{base}/mcp/chat",
        "developerUrl": f"{base}/mcp/code",
        "user": user,
    })
    return 0


def cmd_sample(args) -> int:
    """Fictional records: load them, see them, take them away, hand them on.

    The appliance marks everything loaded here so that removing it is exact. Nothing
    else this product does deletes anything, which is what makes `remove` and `clear`
    safe to run without reading the manual first.
    """
    base, auth = _sample_target(args)
    if not base:
        return 1

    if args.sample_command == "list":
        s.describe_sets(s.samples_api(base, auth))
        return 0

    if args.sample_command == "add":
        payload = s.read_set(args.source)
        name = payload.get("name") or args.source
        print(f"  Loading {s.bold(name)}" + s.dim(f" into realm {payload.get('realm', '?')}…"))
        result = s.samples_api(base, auth, "", payload)
        print(f"  {s.TICK} Loaded {result.get('nodes', 0)} node(s), {result.get('edges', 0)} edge(s).")
        print("  " + s.dim(f"Remove it with: {embabel_command('sample')} remove {name}"))
        return 0

    if args.sample_command == "remove":
        result = s.samples_api(base, auth, f"/{args.name}", method="DELETE")
        print(f"  {s.TICK} Removed {result.get('nodes', 0)} node(s) from '{args.name}'.")
        return 0

    if args.sample_command == "clear":
        # Asked, not assumed. It is a small action with a wide blast radius, and the
        # person running it before a customer demo is in a hurry.
        if not args.yes:
            answer = prompt("  Remove ALL sample data from this world? [y/N]: ").strip().lower()
            if answer not in ("y", "yes"):
                print("  Left alone.")
                return 0
        result = s.samples_api(base, auth, "", method="DELETE")
        print(f"  {s.TICK} Removed {result.get('nodes', 0)} node(s).")
        return 0

    if args.sample_command == "export":
        if not args.labels and not args.source:
            print("  Name a --source set or at least one --label.")
            print("  " + s.dim("An export with no selector is the whole world."))
            return 1
        payload = {
            "name": args.name,
            "realm": args.realm,
            "labels": [l.strip() for l in (args.labels or "").split(",") if l.strip()],
            "source": args.source,
            "limit": args.limit,
            # SHAPE_ONLY unless asked otherwise, and the appliance defaults the same way.
            # Stated here as well so the request says what it means.
            "mode": "WITH_VALUES" if args.with_values else "SHAPE_ONLY",
        }
        result = s.samples_api(base, auth, "/export", payload)
        text = json.dumps(result, indent=2)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(text + "\n")
            print(f"  {s.TICK} Wrote {len(result.get('nodes', []))} node(s) to {s.bold(args.output)}")
            # LOUDLY. A caller who asked for 500 and got 500 cannot tell a complete
            # answer from a truncated one, and an export that looks finished and is not
            # is worse than one that refused — somebody sends it and wonders why the
            # reproduction does not reproduce.
            if result.get("truncated"):
                print("  " + s.warn(
                    f"TRUNCATED: {result.get('matched', 0)} record(s) matched, "
                    f"{len(result.get('nodes', []))} written."))
                print("  " + s.dim("Narrow it with --source <set>, or raise --limit."))
            if not args.with_values:
                print("  " + s.dim("Shape only: labels, property keys and edges. No values, no ids."))
            else:
                print("  " + s.warn("Contains real values — read it before sending it anywhere."))
            print("  " + s.dim(f"Load it elsewhere with: {embabel_command('sample')} add {args.output}"))
        else:
            print(text)
        return 0

    print(f"  {embabel_command('sample')} add | remove | list | clear | export")
    return 1


def cmd_contract(args) -> int:
    """Draft an ODCS data contract for a saved view.

    Three escalating decisions, three flags, none of them the default. Reading a view's
    declaration is free and changes nothing; --sample runs the view; --save writes a
    document into the world; --bind puts it in front of the next person who queries.

    Nothing here can produce an enforcing contract. The appliance drafts in `draft`
    status and binds in `observe` mode, and promoting either is a human edit to a file —
    deliberately, because the failure this whole feature guards against is a promise
    nobody checked being believed by a machine.
    """
    if args.contract_command != "generate":
        print(f"  {embabel_command('contract')} generate --view <name>")
        return 1

    base, auth = _sample_target(args)
    if not base:
        return 1

    # --bind without --save would ask the view to pin a contract that was never written.
    persist = args.save or args.bind
    try:
        result = s.contract_api(base, auth, args.view, {
            "sample": args.sample,
            "persist": persist,
            "bind": args.bind,
        })
    except s.SetupError as e:
        print("  " + s.warn(str(e)))
        return 1

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(result.get("yaml", ""))
        print(f"  {s.TICK} Wrote the contract to {s.bold(args.output)}")
        s.describe_contract(result, persisted=persist, bound=result.get("bound", False))
        return 0

    s.describe_contract(result, persisted=persist, bound=result.get("bound", False))
    if not persist:
        print()
        print(result.get("yaml", ""))
    return 0


def cmd_run_view(args) -> int:
    """Run a saved view by name, or list what there is to run.

    Named for what it does to something that already exists: the view was authored,
    validated and saved elsewhere, and this calls it. Nothing here can create one, and
    no Cypher crosses this boundary in either direction — the argument to this verb is
    a NAME, and what comes back is the calling tier's envelope.

    Bare `embabel run-view` lists, because the first question anyone has is what they
    are allowed to call and what it wants passed. That is cheaper to answer here than
    by sending someone to a console.
    """
    base, auth = _sample_target(args)
    if not base:
        return 1

    if not args.view:
        try:
            s.describe_views(s.views_api(base, auth))
        except s.SetupError as e:
            print("  " + s.warn(str(e)))
            return 1
        print("  " + s.dim(f"{embabel_command('run-view')} <name> --arg name=value"))
        return 0

    try:
        params = s.parse_args(args.arg, args.args_json)
        result = s.run_view_api(base, auth, args.view, params)
    except s.SetupError as e:
        print("  " + s.warn(str(e)))
        return 1

    return s.describe_run(args.view, result, args.json)


def cmd_scenario(args) -> int:
    """Put the world in a named state, from wherever it is now.

    Declared rather than scripted: a scenario says what should be loaded, and running it
    works out the difference. That is what makes it safe to jump straight to the fourth
    one when somebody in the room asks to see it.
    """
    if args.scenario_command == "list" and not s.scenario_files():
        print("  " + s.dim("No scenarios found. Put .json files in ./scenarios."))
        return 0

    base, auth = _sample_target(args)
    if not base:
        return 1
    scenarios = s.load_scenarios()
    loaded = s.loaded_names(base, auth)

    if args.scenario_command == "list":
        s.describe_scenarios(scenarios, loaded)
        return 0

    if args.scenario_command == "run":
        s.apply_scenario(base, auth, s.find_scenario(args.name), loaded, dry_run=args.dry_run)
        return 0

    if args.scenario_command == "capture":
        if not loaded:
            print("  " + s.warn("Nothing is loaded — this will capture an empty world."))
        path = s.capture_scenario(args.name, loaded, scenarios,
                                  description=args.description, force=args.force)
        scenario = s.read_set(path)
        print(f"  {s.TICK} Wrote {s.bold(path)}")
        print(f"     wants:   {', '.join(scenario['wants']) or '(nothing)'}")
        if scenario["without"]:
            print(f"     without: {', '.join(scenario['without'])}")
            print("  " + s.dim("(what the other scenarios load, so this one clears it away)"))
        print("  " + s.dim(f"Replay it with: {embabel_command('scenario')} run {args.name}"))
        return 0

    if args.scenario_command == "next":
        if not scenarios:
            print("  " + s.dim("No scenarios found."))
            return 1
        here = s.current_scenario(scenarios, loaded)
        if here is None:
            # Nothing matches, so start at the beginning rather than guess.
            following = scenarios[0]
        else:
            index = next(i for i, x in enumerate(scenarios) if x["name"] == here["name"])
            if index + 1 >= len(scenarios):
                print(f"  {s.MIDDOT} " + s.dim(f"'{here['name']}' is the last one."))
                return 0
            following = scenarios[index + 1]
        s.apply_scenario(base, auth, following, loaded, dry_run=args.dry_run)
        return 0

    print(f"  {embabel_command('scenario')} list | run | next")
    return 1


def cmd_sandbox(args) -> int:
    """The code-mode sandbox: what it is, and how to make it yours.

    The shipped image is deliberately small — every appliance downloads it before
    anybody runs a line of code, so it carries the runtimes code-mode executes and
    nothing else. A Dockerfile here is how somebody who needs more pays for it
    themselves.
    """
    if args.sandbox_command in (None, "show"):
        s.describe_sandbox()
        return 0

    if args.sandbox_command == "build":
        tag = s.build_sandbox(dockerfile=args.file, tag=args.tag, no_cache=args.no_cache)
        print(f"  {s.TICK} Built {s.bold(tag)} and recorded it in .env")
        print("  " + s.warn(f"Restart to use it: {embabel_command('down')} && {embabel_command('up')}"))
        return 0

    if args.sandbox_command == "reset":
        s.reset_sandbox()
        return 0

    print(f"  {embabel_command('sandbox')} show | build | reset")
    return 1


def _applied_live(args, model: str) -> bool:
    """Put the choice into effect now, if the appliance is up and can take it.

    THE LOCAL MODEL IS ATTEMPTED TOO, and used not to be. Two separate things stopped it, and
    both are now gone: the container was never told where Docker Model Runner listens unless the
    embedder overlay was composed in (named unconditionally in both compose files now), and the
    platform enumerated a runner's models once at startup, so one pulled a minute later was
    invisible however reachable it was (resolved per call now — embabel/embabel-agent#2046).
    Neither had anything to do with the model itself, which `choose_embeddings` has always pulled
    on the HOST, outside the composition.

    Nothing here is required. A refusal, an older server that does not take a role by name, an
    appliance that is down, a runner that is not running: each leaves .env correct and the restart
    still works, so every failure path simply returns False and lets the caller print what it was
    going to print anyway.
    """
    base, auth = _sample_target(args)
    if not base:
        return False
    print("  " + s.dim("Applying to the running appliance… re-embedding anything already stored."))
    if not s.apply_now(model, base, auth):
        return False
    print(f"  {s.TICK} In effect now — no restart needed.")
    return True


def cmd_embeddings(args) -> int:
    """The embedding model, and whether document features are on.

    An appliance ships without one — the local model is a gigabyte and needs Docker
    Model Runner. Document features report themselves off until this is set, which is
    a different thing from being broken.
    """
    if args.embeddings_command == "off":
        s.clear_embeddings()
        return 0

    if args.embeddings_command == "use":
        model = s.choose_embeddings(args.choice)
        print(f"  {s.TICK} Embedding model set to {s.bold(model)}")
        print("  " + s.dim(".env now says so, which is what survives a restart."))
        if _applied_live(args, model):
            return 0
        print("  " + s.warn(f"Restart to apply: {embabel_command('down')} && {embabel_command('up')}"))
        return 0

    # show: ask the appliance rather than infer from .env, because what is running is
    # the thing that decides whether documents work.
    base, auth = _sample_target(args)
    if not base:
        chosen = s.configured_embedding_model()
        print(f"  {s.MIDDOT} " + s.dim(f"Not running. .env says: {chosen or 'no embedding model'}"))
        return 0
    s.describe_embeddings(s.embeddings_status(base, auth), s.configured_embedding_model())
    return 0


def cmd_diagram(args) -> int:
    """The world as an entity-relationship diagram: entities with their typed columns and
    keys, views as their own shape, a relationship wherever a column carries another
    entity's key, and a mark on every column a model made. Re-derived from the world's
    declarations on every read, so it cannot rot the way a drawn diagram does.

    It is rendered by world-graphql, the sidecar that also serves the world as GraphQL,
    because the catalog it is drawn from lives there, not in this installer. Mermaid
    text: paste it into a README, a wiki page or a pull request and it renders; `--out`
    writes a .mmd file.
    """
    import base64
    import urllib.error
    import urllib.request

    door = (args.door or os.environ.get("EMBABEL_GRAPHQL_DOOR") or "http://127.0.0.1:15480").rstrip("/")
    base, auth = _sample_target(args)
    if not base:
        return 1
    request = urllib.request.Request(f"{door}/catalog/diagram.mmd")
    request.add_header("Authorization", auth)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            diagram = response.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print(f"  {door} did not accept the appliance login. The door forwards YOUR login to the appliance; "
                  "set EMBABEL_USER / EMBABEL_PASSWORD to the account that can read it.")
        else:
            print(f"  {door} answered HTTP {e.code} for /catalog/diagram.mmd. Is that a world-graphql sidecar?")
        return 1
    except (urllib.error.URLError, OSError) as e:
        print(f"  No GraphQL door at {door} ({getattr(e, 'reason', e)}).")
        print("  The diagram is drawn by the world-graphql sidecar. Start one beside the appliance:")
        inside = base.replace('127.0.0.1', 'host.docker.internal').replace('localhost', 'host.docker.internal')
        print(f"    docker run --rm --add-host=host.docker.internal:host-gateway -p 15480:15480 -e APPLIANCE_BASE={inside} \\")
        print("      -e APPLIANCE_USER=<user> -e APPLIANCE_PASS=<password> ghcr.io/embabel-worlds/world-graphql")
        print("  then run this again, or point --door at one that is running.")
        return 1
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(diagram)
        entities = sum(1 for line in diagram.splitlines() if line.rstrip().endswith("{"))
        relations = sum(1 for line in diagram.splitlines() if "||--o{" in line or "||..o{" in line)
        print(f"  Wrote {args.out}: {entities} entities, {relations} relationships. Paste it where Mermaid renders.")
        return 0
    sys.stdout.write(diagram)
    return 0
