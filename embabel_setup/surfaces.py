"""Where to go once the appliance is up, and how to get there.

The two doors present differently — Worlds hands you a console in a browser, Me
hands you a desktop app — but the shape is the same: say the address first,
because it is the whole answer on a headless box, and only then try to open it.

Launching the Me app also has to SEED it, or a freshly built app opens on an
empty settings file and asks the operator for a URL they were just shown.
"""

from __future__ import annotations
import json
import os
import platform
import shutil
import subprocess
import sys

from .colour import ARROW, MIDDOT, TICK, accent, bold, dim, heading, url
from .core import APPLIANCE_DIR, ME_APP_DIR, prompt
from .dockerlib import find_mode_container
from .settings import (SQL_VAR, embabel_command, console_url, instance, monitoring_enabled, port_base, ports_for,
                       sql_enabled, surface_urls)
from .versions import jar_holds
from .words import name_the_instance, say

# THE VERB, which every ending was missing.
#
# install.sh promises "after this, use the 'embabel' command" and then, minutes
# of pulls and five questions later, the closing block printed addresses and no
# verb — so the command was found by guessing. These are the same lines a bare
# `embabel` prints as its Next block; the CLI calls this rather than carrying
# its own copy, because two lists of what to do next is how they come to
# disagree.
#
# THE WORDS ARE IN copy/, the DECORATION is here. A copy file holds sentences,
# not escape codes and not column widths — so the verb column is padded and
# accented at this end and interpolated in, and an editor changing "why it is
# not working" never has to count spaces or open this file.


def verb(name: str, column: int = 16) -> str:
    """`embabel <name>` for this instance, accented and padded to [column].

    ljust BEFORE accent(): padding a string that already carries escape codes
    counts them toward the width, and every row steps left.
    """
    return accent(name_the_instance(f"embabel {name}").ljust(column))


def print_next(width: int = 58) -> None:
    """What to type next. The LAST thing any run says, so it survives the scroll."""
    print("  " + heading("Next", width))
    column = max(16, *(len(name_the_instance(f"embabel {name}")) + 2 for name in ("doctor", "--help")))
    say("next", up=verb("up", column), doctor=verb("doctor", column),
        open=verb("open", column), help=verb("--help", column))
    print()


def print_worlds_surfaces(base: str) -> None:
    """Worlds onboarding ends at the way in, not at "done": every surface a worlds
    operator reaches next, in one block. The API/MCP lines use the mode's real
    detected port; the rest are the compose defaults (.env moves them).

    The MCP servers come LAST because they are the takeaway — the last lines of a
    wizard are the ones still on screen when everything above has scrolled — and
    the graph link is gone on purpose: the browser and bolt ports are an
    implementation detail, while the console and MCP are the product's doors.

    /mcp/chat rather than the bare /mcp, because these two lines sit one above the
    other and the pair has to READ as a pair: `/mcp` beside `/mcp/code` looks like a
    general endpoint and a special one. Both spellings reach the same server.
    """
    print("  " + heading("Your Worlds surfaces", 58))
    say("surfaces-worlds",
        console=bold("Console") + "        " + url(surface_urls()["console"]),
        start_here=accent(f"{ARROW} START HERE"),
        # NOT PRESENTED AS A DESTINATION. Every line in this block is a URL in a
        # terminal that makes URLs clickable, so the only thing separating "go here"
        # from "this exists" is what the words around it say. Somebody followed this
        # one and landed on the door's own legacy web UI, which is not where anyone
        # should be — the console is the surface. The label carries that now instead
        # of leaving it to the aside.
        api="API (no UI)    " + url(base) + "  ",
        dashboards=monitoring_line(),
        mcp="MCP servers    " + url(base + "/mcp/chat"),
        mcp_code="               " + url(base + "/mcp/code"))
    print_sql_line("worlds")
    print()
    print_next()


# What tells a server that serves SQL itself: the module that brings the door, which
# arrived in the server's jar together with the setting that turns it on.
SQL_DOOR_MARKER = "BOOT-INF/lib/graph-sql-"


def print_sql_line(mode: str) -> None:
    """Where SQL is, or why it is not there.

    A connection string, because that is what a BI tool or `psql` actually wants; the
    credentials are the operator's own appliance login. The image is asked, through the
    running container, because a server older than the door ignores the switch and
    publishes a port with nothing behind it — the old sidecar is gone, so the line says
    what brings SQL back rather than handing out an address that refuses.
    """
    if not sql_enabled():
        print("  " + dim("SQL (Postgres)") + " " + dim(f"off — {SQL_VAR}=true in .env, then {embabel_command('up')}"))
        return
    container = find_mode_container(mode)
    if container and jar_holds(container, True, SQL_DOOR_MARKER) is False:
        print("  " + dim("SQL (Postgres)") + " "
              + dim(f"not in this server image — {embabel_command('upgrade')} brings the SQL door"))
        return
    port = ports_for(port_base())["WORLDS_SQL_PORT"]
    print("  " + dim("SQL (Postgres)") + " " + url(f"postgresql://<user>@localhost:{port}/world")
          + "  " + dim("(BI / dbt / notebooks)"))


def monitoring_line() -> str:
    """Where the dashboards are, or how to have them. Off by default (#112), and the
    line stays either way: somebody looking for dashboards should find the switch."""
    if not monitoring_enabled():
        return "Dashboards     " + dim(f"off — EMBABEL_MONITORING=on in .env, then {embabel_command('up')}")
    return ("Dashboards     " + url(surface_urls()["dashboards"])
            + "   ·   Metrics  " + url(surface_urls()["metrics"]))


def print_me_surfaces(base: str) -> None:
    """Where a Me operator goes next.

    The worlds mode has always ended on a block like this; Me ended on the app
    offer alone, which left the assistant's own web UI and its MCP endpoints
    undiscoverable from the terminal that had just configured all three.
    """
    print("  " + heading("Your Me surfaces", 58))
    say("surfaces-me",
        assistant=bold("Assistant") + "      " + url(base),
        start_here=accent(f"{ARROW} START HERE"),
        mcp="MCP servers    " + url(base + "/mcp/chat"),
        mcp_code="               " + url(base + "/mcp/code"))
    print_sql_line("me")
    print()
    print_next()


def me_app_settings_file() -> str | None:
    """Where the Me app keeps its settings — Electron's per-app userData directory,
    named for the app itself (`app.setName('Embabel Me')`). Only the platforms the
    app runs on are mapped; anywhere else, seeding is silently skipped."""
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "Embabel Me", "settings.json")
    if sys.platform.startswith("linux"):
        config = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        return os.path.join(config, "Embabel Me", "settings.json")
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        return os.path.join(appdata, "Embabel Me", "settings.json") if appdata else None
    return None


def seed_me_app_settings(base: str, username: str | None) -> None:
    """Hand the Me app what setup already established: which mode to talk to, and
    who the user is. Retyping a URL and username the wizard just set up is pure
    friction — and the URL is worth seeding even at the default, because a
    non-default ASSISTANT_PORT would otherwise leave the app pointed at nothing.

    NEVER the password. Setup knows it, but a wizard silently writing a credential
    to a file the user did not choose is a different act from that user typing it
    into an app they can see — one keystroke of theirs is a fair price for that
    line staying honest.

    Only fills what is MISSING: an existing setting is the user's own answer, and
    a later run must not overwrite it with an assumption."""
    path = me_app_settings_file()
    if path is None:
        return
    try:
        current = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                current = json.load(f)
        if not isinstance(current, dict):
            return  # a settings file we do not recognise is not ours to rewrite
        seeded = dict(current)
        # The instance goes with the URL: the app finds its containers and volume
        # by it. Recorded only when the URL is this instance's, so an app already
        # pointed at another appliance is not told it belongs to this one.
        if not seeded.get("instance") and seeded.get("baseUrl") in (None, "", base):
            seeded["instance"] = instance()
        if not seeded.get("baseUrl"):
            seeded["baseUrl"] = base
        if username and not seeded.get("username"):
            seeded["username"] = username
        if seeded == current:
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(seeded, f, indent=2)
    except (OSError, ValueError):
        # Seeding is a courtesy; the app asks for these anyway. Never fail setup
        # over a settings file.
        pass


def packaged_me_app() -> str | None:
    """The built app, if this machine has one — installed, or sitting in the repo's
    own release directory after `npm run package`. Returns a path `open -a` takes."""
    if sys.platform != "darwin":
        return None
    candidates = [
        "/Applications/Embabel Me.app",
        os.path.expanduser("~/Applications/Embabel Me.app"),
    ]
    release = os.path.join(ME_APP_DIR, "release")
    if os.path.isdir(release):
        for entry in sorted(os.listdir(release)):
            candidates.append(os.path.join(release, entry, "Embabel Me.app"))
    return next((c for c in candidates if os.path.isdir(c)), None)


def open_in_browser(target: str) -> bool:
    """Open a URL, if this machine has anything to open it with.

    The one moment worth doing this is the end of setup — the appliance has just
    been waited for, so the page will render rather than refuse, which is the
    difference between a finish and a broken link. Before the restart wait
    existed, opening here would have shown a dead port.

    Refuses in the cases where a browser is the wrong answer: no opener on PATH
    (a server, a container), no terminal (a script, CI), or SSH_CONNECTION set —
    over ssh the opener runs on the WRONG MACHINE, which is worse than not
    running at all. EMBABEL_NO_BROWSER opts out for anyone who just dislikes it.
    """
    if os.environ.get("EMBABEL_NO_BROWSER") or os.environ.get("SSH_CONNECTION"):
        return False
    if not (hasattr(sys.stdout, "isatty") and sys.stdout.isatty()):
        return False
    opener = next((cmd for cmd in ("open", "xdg-open", "wslview") if shutil.which(cmd)), None)
    if not opener:
        return False
    try:
        subprocess.Popen([opener, target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def launch_me_app(base: str, username: str | None = None) -> None:
    """Me onboarding ends in the Me app, the way worlds onboarding ends at the
    console: the appliance thinks, the app senses, and a new user should meet
    both. The app is plain JavaScript on Electron — no build step — so `npm
    install` (run here on first use, for Electron itself) is the whole cost."""
    if not os.path.isdir(ME_APP_DIR):
        return  # a checkout without the app (or a remote setup) — nothing to offer
    seed_me_app_settings(base, username)
    print("\n  " + heading("The Me app", 58))
    print("  Your appliance thinks; the Me app senses. It sits in your menu bar,")
    print(f"  reads local signals, and sends only what you approve to {base}.")
    # A packaged build wins when there is one: it carries the app's own identity,
    # so macOS names IT in permission prompts ("Embabel Me wants to control Google
    # Chrome") instead of naming Electron, and the user gets no terminal, no npm,
    # and no build step. `npm start` stays the developer path.
    packaged = packaged_me_app()
    if packaged:
        answer = prompt("  Start it now? [Y/n]: ").strip().lower()
        if answer not in ("", "y", "yes"):
            print(f'  Whenever you like:  open -a "{packaged}"')
            return
        subprocess.Popen(["open", "-a", packaged], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print('  Starting — look for "Me" in your menu bar. The appliance URL and your username')
        print("  are filled in already; enter your password and it will offer its first scan.")
        return

    npm = shutil.which("npm")
    if npm is None:
        print("  It needs Node.js (https://nodejs.org). Once that is installed:")
        print("      cd me-app && npm start")
        return
    answer = prompt("  Start it now? [Y/n]: ").strip().lower()
    if answer not in ("", "y", "yes"):
        print("  Whenever you like:  cd me-app && npm start")
        return
    if not os.path.isdir(os.path.join(ME_APP_DIR, "node_modules", "electron")):
        # The app's one dependency, dev-time only. Foreground, so a failure is
        # visible and a slow download narrates itself instead of hanging mute.
        print("  First run — fetching Electron (npm install)…")
        run = subprocess.run([npm, "install"], cwd=ME_APP_DIR)
        if run.returncode != 0:
            print("  npm install failed — fix the error above, then: cd me-app && npm start")
            return
    # Detached, output dropped: the app outlives this wizard, and Electron's
    # chatter has no business in the terminal being handed back.
    subprocess.Popen([npm, "start"], cwd=ME_APP_DIR, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     env=dict(os.environ, EMBABEL_INSTANCE=instance()))
    print('  Starting — look for "Me" in your menu bar. The appliance URL and your username')
    print("  are filled in already; enter your password and it will offer its first scan.")
