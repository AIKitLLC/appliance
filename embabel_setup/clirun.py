"""Running it: start, stop, and why it is not working.

The verbs somebody reaches for when the appliance is misbehaving, or when they want
it to exist at all. Nothing here reimplements anything — setup.py owns the docker
checks, the staged start and the wizard, and two copies of "is Docker running" is how
they come to disagree.
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

from .cli import HERE, _emit, _sample_target, current_mode, resolve_instance, resolved_mode, run_setup, s
from .core import prompt
from .settings import embabel_command

# ── verbs ───────────────────────────────────────────────────────────────────

def cmd_up(args) -> int:
    """Start the appliance and finish setting it up. Safe to run any time: setup.py
    reconciles a running mode rather than starting a second one."""
    # Also here, not only in `upgrade`: somebody who moved images by hand still runs this,
    # and the repair is a no-op on anything already correct.
    if s.migrate_legacy_embedding_choice():
        print(f"  {s.TICK} Embedding model recorded as the `hosted` role, which newer builds resolve")
    return run_setup(resolved_mode(args.mode), *(["--fresh"] if args.fresh else []))


def cmd_down(args) -> int:
    mode = resolved_mode(args.mode)
    if args.wipe:
        print("  --wipe DELETES the world, the graph, documents and your account.")
        if input("  Type 'yes' to wipe: ").strip().lower() != "yes":
            print("  Nothing was touched.")
            return 1
        s.take_everything_down()
        print("  Wiped.")
        return 0
    s._compose(mode, "stop")
    print(f"  Stopped the {mode} mode. Your data is untouched — `{embabel_command('up')}` brings it back.")
    return 0


def cmd_status(args) -> int:
    """What is running, what is still arriving, and where to go.

    The staged start means "not everything is up" is a NORMAL state for the first
    quarter of an hour, so this reports the two groups separately. A flat container
    list cannot tell "still downloading" from "broken", which is the only question
    being asked.
    """
    mode = current_mode()
    if not mode:
        print(f"  Nothing is running.  `{embabel_command('up')}` starts it.")
        return 0

    container = s.find_mode_container(mode)
    base = s.container_base_url(container) if container else None
    print(f"  Mode: {mode}" + (f"   API: {base}" if base else ""))

    running = set()
    run = s._docker("ps", "--format", "{{.Names}}")
    if run and run.returncode == 0:
        running = {line.strip() for line in run.stdout.splitlines() if line.strip()}

    core = [c["name"] for c in s.appliance_containers() if c["state"] == "running"]
    print(f"\n  Core            {len(core)} container(s) up")

    # Named individually because the interesting answer is WHICH one is missing.
    # By prefix: compose names a container <project>-<service>-N, never the bare service.
    pending = [s.DEFERRED_WHY[name] for name in s.deferred_services()
               if name in ("docling", *s.MONITORING_SERVICES)
               and not any(n.startswith(f"{s.compose_project()}-{name}-") for n in running)]
    if pending:
        print(f"  Still arriving  {', '.join(pending)}")
        print("                  (downloading in the background; the appliance works without them,")
        print("                   but a PDF added now ingests as flat text)")
    else:
        print("  Extras          all up")

    if base:
        print()
        (s.print_worlds_surfaces if mode == "worlds" else s.print_me_surfaces)(base)
    return 0


def docker_config_path() -> str:
    """Where the Docker CLI reads its config, which is not always `~/.docker`.

    DOCKER_CONFIG names a DIRECTORY — the CLI reads `config.json` inside it. Reading the
    default while docker reads somewhere else would make this checker pass on exactly the
    machine it exists to catch, which is worse than not checking at all.
    """
    root = os.environ.get("DOCKER_CONFIG") or os.path.join(os.path.expanduser("~"), ".docker")
    return os.path.join(root, "config.json")


def shorten_home(path: str) -> str:
    """`~/.docker/config.json` rather than the whole thing — these paths are printed in a
    terminal somebody is reading, and the home prefix is noise they already know."""
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home + os.sep) else path


def docker_credential_helpers() -> list[tuple[str, str]]:
    """Every credential helper docker's config names, with where it was named.

    `credsStore` applies to all registries; `credHelpers` maps particular ones. Both are
    checked, because a helper named for a registry this appliance never pulls from still
    cannot break anything, and one named globally breaks everything.

    Unreadable or absent config is not a problem — that is the normal state of a machine
    that has never logged in to a registry — so it yields nothing rather than a warning.
    """
    path = docker_config_path()
    shown = shorten_home(path)
    try:
        with open(path, encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(config, dict):
        return []
    named = []
    store = config.get("credsStore")
    if isinstance(store, str) and store:
        named.append((store, f"credsStore in {shown}"))
    helpers = config.get("credHelpers")
    if isinstance(helpers, dict):
        for registry, helper in sorted(helpers.items()):
            if isinstance(helper, str) and helper and helper not in [n for n, _ in named]:
                named.append((helper, f"credHelpers[{registry}] in {shown}"))
    return named


def cmd_doctor(args) -> int:
    """Everything that has actually gone wrong for somebody, checked in one place.

    Each line here is a real failure with a real diagnosis attached, because the
    ones that cost time are the ones that fail SILENTLY — a mount outside Docker's
    file sharing that resolves empty, a private image, a Model Runner that is off.
    """
    problems = 0
    print(f"  Appliance directory: {HERE}\n")

    def check(label: str, ok: bool, fix: str = "") -> None:
        nonlocal problems
        print(f"  {s.TICK if ok else s.CROSS}  {label}")
        if not ok:
            problems += 1
            if fix:
                print(f"     {s.dim(fix)}")

    # RANCHER DESKTOP DEFAULTS TO containerd, which ships nerdctl and no docker
    # command — so "install Docker Desktop" is advice to install a second
    # container runtime beside the one already here, and the actual fix is one
    # setting. Both tools together are the tell: nerdctl alone is plain
    # containerd, where nothing about Preferences applies.
    rancher = shutil.which("rdctl") is not None and shutil.which("nerdctl") is not None
    check("docker installed", shutil.which("docker") is not None,
          "Rancher Desktop is set to containerd, which has no docker CLI. Switch it:\n"
          "         rdctl set --container-engine docker\n"
          "       or Preferences → Container Engine → dockerd (moby). Nothing is deleted;\n"
          "       images pulled with nerdctl are invisible until you switch back."
          if rancher else
          "Install Docker Desktop: https://docs.docker.com/get-started/get-docker/")
    daemon = s._docker("info")
    check("docker running", bool(daemon and daemon.returncode == 0),
          "Start Rancher Desktop, then run this again." if rancher else
          "Start Docker Desktop, then run this again.")
    compose = s._docker("compose", "version")
    check("docker compose v2", bool(compose and compose.returncode == 0),
          "Update Rancher Desktop, or install the compose plugin." if rancher else
          "Update Docker Desktop, or install the compose plugin.")
    # THE CREDENTIAL HELPER, which fails in a way that looks nothing like its cause.
    #
    # `~/.docker/config.json` can name a helper binary — `credsStore: desktop` is what
    # Docker Desktop writes — and the CLI then runs that binary for EVERY registry,
    # including anonymous pulls of public images. If the binary is not on PATH the pull
    # dies with `error getting credentials - err: exec: "docker-credential-desktop":
    # executable file not found in $PATH` before a single byte is fetched, and the
    # appliance's own message on top of it is the useless "docker compose up failed".
    #
    # Docker Desktop installs the symlink into /usr/local/bin, and declining the admin
    # prompt that asks to do so is enough to leave a machine in exactly this state.
    for helper, where in docker_credential_helpers():
        check(f"docker credential helper '{helper}' on PATH", shutil.which(f"docker-credential-{helper}") is not None,
              f"Named by {where}, but docker-credential-{helper} is not on PATH, so every pull "
              f"fails before it starts.\n"
              f"       Put the bin directory that owns it on your PATH:\n"
              f"         export PATH=\"$PATH:{'$HOME/.rd/bin' if helper == 'rancher-desktop' else '/Applications/Docker.app/Contents/Resources/bin'}\"\n"
              f"       or drop the helper — these images are public and need no credentials:\n"
              f"         remove the \"credsStore\" line from {shorten_home(docker_config_path())}")

    # MODEL RUNNER IS NOT A PROBLEM WHEN IT IS ABSENT, and counting it as one
    # taught every Rancher Desktop, Colima and Intel-Mac user to distrust this
    # doctor's crosses. It is a Docker Desktop feature: those machines cannot
    # have it, the appliance does not need it, and `embeddings use hosted`
    # covers the one thing it serves. A cross is for something the reader can
    # and should fix.
    runner = s._docker("model", "status")
    if runner and runner.returncode == 0:
        print(f"  {s.TICK}  Docker Model Runner (can serve a local embedding model)")
    else:
        print(f"  {s.MIDDOT}  Docker Model Runner unavailable "
              + s.dim("— only document search uses it"))
        print("     " + s.dim(
            "Docker Desktop: enable it in Settings → AI. Linux: the docker-model-plugin\n"
            "     package. Rancher Desktop, Colima, Intel Mac: it does not exist there —\n"
            f"     use the key you already have, with `{embabel_command('embeddings')} use hosted`."))

    # THE MODEL IS CHECKED BY NAME, because the tag matters: `latest` on that
    # repository is the 4B variant with different dimensions, and embeddings are
    # sticky once written. Only asked when the appliance has been pointed at the
    # local model — an appliance ships with no embedding model at all, so a
    # missing pull is a finding only for somebody who chose `use local`.
    if runner and runner.returncode == 0 and (s.configured_embedding_model() or "").startswith(("ai/", "docker.io/ai/")):
        listed = s._docker("model", "list")
        have = listed.stdout if listed and listed.returncode == 0 else ""
        wanted = s.EMBEDDING_MODEL
        check(f"embedding model {wanted}", wanted.split("/")[-1].split(":")[0] in have
              and wanted.split(":")[-1] in have,
              f"Chosen in {s.env_file()} but not pulled, so documents will not index:\n"
              f"       docker model pull {wanted}")

    # NOT a check: a fresh checkout has no .env and is not broken. Flagging it with
    # a cross and then explaining that the cross does not mean anything is how a
    # doctor teaches people to ignore its crosses.
    env_path = s.env_path()
    if os.path.exists(env_path):
        print(f"  {s.TICK}  {s.env_file()} present")
    else:
        print(f"  {s.MIDDOT}  " + s.dim(f"no {s.env_file()} yet — this appliance has not been set up here (`{embabel_command('up')}`)"))

    # THE ENGINE, and whether .env says so. Every verb pins it (cli.resolve_instance), so
    # an install without the line is one the pin could not write. A Neo4j container left
    # beside an Embabel graph is not a fault — that graph is kept on purpose — but it is
    # somebody else's data than the world they are looking at, and worth a line.
    if os.path.exists(env_path):
        engine = s.canonical_engine(s.env_file_value(s.GRAPH_TYPE_VAR))
        check(f"graph engine {engine or s.graph_engine()}, pinned in {s.env_file()}", engine is not None,
              f"{s.env_file()} has no GRAPH_TYPE line, so an upgrade could move this appliance's graph.\n"
              f"       Any `{embabel_command('status')}` writes it; if that fails, add GRAPH_TYPE=NEO4J by hand.")
        if s.graph_engine() == "EMBABEL" and s.service_container("neo4j", stopped=True):
            print(f"  {s.MIDDOT}  " + s.dim("a Neo4j container from before is still here; the graph in it is not "
                                          "the one this appliance uses"))

    realms = os.environ.get("EMBABEL_REALMS_DIR")
    if not realms and os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                if line.startswith("EMBABEL_REALMS_DIR="):
                    realms = line.split("=", 1)[1].strip()
    if realms:
        path, found, notes = s.inspect_realms_dir(realms)
        check(f"realm checkouts at {realms}", path is not None, " ".join(notes))
        if path:
            print(f"     {len(found)} realm(s) visible")
    else:
        print(f"  {s.MIDDOT}  " + s.dim(f"no realm checkouts linked   ({embabel_command('realms')} link <dir>)"))

    # INFORMATIONAL, and last of the machine facts: doctor's crosses mean "this is
    # broken", and a tight-but-working machine is not broken. The numbers are printed
    # either way so somebody can see their headroom before they wonder about it.
    for is_warning, text in s.capacity_notes():
        print(f"  {s.warn('!') if is_warning else s.MIDDOT}  " + (text if is_warning else s.dim(text)))

    strays = s.stray_sandbox_containers()
    if strays:
        print(f"  {s.MIDDOT}  " + s.dim(f"{len(strays)} stray code-sandbox container(s) — `{embabel_command('prune')}` removes them"))

    print()
    if not problems:
        print("  " + s.good("All good."))
    else:
        print("  " + s.warn(f"{problems} problem{'s' if problems > 1 else ''} above")
              + " — each line says what to do.")
    return 1 if problems else 0


def cmd_logs(args) -> int:
    mode = resolved_mode(args.mode)
    service = args.service or s.MODE_SERVICE[mode]
    return s._compose(mode, "logs", *(["-f"] if args.follow else []), "--tail", str(args.tail), service).returncode


def cmd_compose(args) -> int:
    """docker compose for this instance, exactly as `up` runs it: its project, settings
    file, compose files and overlays, and the environment with the port block, memory
    limits and profiles. Anything else that needs compose goes through here, so a
    container is never recreated without the limits that keep it from filling Docker's memory."""
    argv = [a for a in args.compose_args]
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv:
        print("  Say what to run, e.g.  " + embabel_command("compose", "ps"))
        return 2
    return s._compose(resolved_mode(args.mode), *argv).returncode


def cmd_open(args) -> int:
    """A surface, in a browser. With nothing named, the surface is THIS appliance's
    front door — the console for worlds, the assistant for me. Defaulting to the
    console regardless sent every Me user to a port with nothing behind it."""
    urls = s.surface_urls()
    what = args.what or ("me" if resolved_mode(None) == "me" else "console")
    if what == "dashboards" and not s.monitoring_enabled():
        print(f"  Dashboards are off. Set EMBABEL_MONITORING=on in .env, then run `{embabel_command('up')}`.")
        return 1
    if what not in urls:
        # Only `graph`, which is Neo4j Browser: nothing to open on an engine without Neo4j.
        bolt = urls.get("bolt")
        engine = {"EMBABEL": "the Embabel engine, inside the server", "FALKORDB": "FalkorDB"}.get(s.graph_engine())
        print(f"  This appliance's graph is {engine or s.graph_engine()}, which has no Neo4j Browser."
              + (f"\n  Neo4j's tools connect to it over Bolt at {bolt}." if bolt else ""))
        return 1
    url = urls[what]
    print(f"  {url}")
    # The URL is printed FIRST and unconditionally: on a headless box, or over ssh,
    # there is no browser to open and the address is the whole answer. The opening
    # itself is setup.py's, so `embabel open` and the end of setup agree about when
    # a browser is the wrong answer.
    s.open_in_browser(url)
    return 0


def cmd_ps(args) -> int:
    """What this appliance has on the host right now.

    THREE GROUPS, because they fail differently. The appliance's own containers
    are the product. The deferred extras are allowed to be missing for the first
    quarter of an hour and are not a fault. Code sandboxes are siblings created
    through the docker socket, not compose services — `down` does not take them,
    which is exactly why they need somewhere to be visible.
    """
    containers = s.appliance_containers()
    if args.json:
        print(json.dumps({"containers": containers, "sandboxes": s.stray_sandbox_containers()}, indent=2))
        return 0

    if not containers:
        print(f"\n  Nothing of this appliance is on the host.  `{embabel_command('up')}` starts it.\n")
        return 0

    print()
    width = max(len(c["name"]) for c in containers)
    for c in containers:
        up = c["state"] == "running"
        mark = s.good(s.BULLET) if up else s.MIDDOT
        line = f"{c['name']:<{width}}  {c['status']}"
        print(f"  {mark} " + (line if up else s.dim(line)))

    missing = [name for name in s.deferred_services()
               if not any(name in c["name"] for c in containers)]
    if missing:
        print("\n  Not here yet   " + ", ".join(s.DEFERRED_WHY[m] for m in missing))
        print("                 (deferred; they download after the appliance is up)")

    strays = s.stray_sandbox_containers()
    if strays:
        print(f"\n  Code sandboxes   {len(strays)}")
        for name in strays[:8]:
            print(f"    {name}")
        if len(strays) > 8:
            print(f"    … and {len(strays) - 8} more")
        print(f"  Siblings of the appliance, not compose services — `{embabel_command('prune')}` clears them.")
    print()
    return 0


def cmd_prune(args) -> int:
    """Remove what the appliance left behind but does not own.

    Sandboxes only. NOT `docker system prune`, and not dangling images: this
    command runs on a developer's machine where most of what docker considers
    garbage belongs to somebody else's work, and a cleanup verb that reaches
    past its own project is a cleanup verb people learn not to run.
    """
    strays = s.stray_sandbox_containers()
    if not strays:
        elsewhere = s.stray_sandbox_containers(mine_only=False)
        if elsewhere:
            print(f"  Nothing to prune for '{s.instance()}'. "
                  f"({len(elsewhere)} sandbox(es) here belong to another instance.)")
        else:
            print("  Nothing to prune — no code-sandbox containers on the host.")
        return 0

    print(f"\n  {len(strays)} code-sandbox container(s):")
    for name in strays[:12]:
        print(f"    {name}")
    if len(strays) > 12:
        print(f"    … and {len(strays) - 12} more")
    # The warning that makes this safe. These carry a label, not an owner, and
    # this command cannot tell an orphan from a live IDE session.
    others = len(s.stray_sandbox_containers(mine_only=False)) - len(strays)
    print(f"\n  These belong to '{s.instance()}' — the server labels every sandbox with the")
    print("  appliance that created it, so no other instance's sessions are listed here.")
    if others:
        print(f"  ({others} more on this host belong elsewhere and are left alone. A server run")
        print("   from an IDE labels its own 'standalone' and is never in this list.)")
    if not args.yes and input("\n  Remove them? [y/N]: ").strip().lower() not in ("y", "yes"):
        print("  Left alone.")
        return 1
    removed = s.prune_sandboxes(strays)
    print(f"  Removed {removed} of {len(strays)}.")
    return 0 if removed == len(strays) else 1
