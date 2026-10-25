"""Bringing the appliance up, taking it down, and taking it away.

The three verbs differ in how much they destroy, and the difference is the whole
point of keeping them together: `down` stops containers, `--fresh` removes the
data, `--uninstall` removes the machine-local configuration as well and returns
the checkout to the state a fresh clone is in.

Images and the local embedding model survive all three, always. The embedding
artifact alone is over a gigabyte and re-downloading one that has not changed is
pure waste.
"""

from __future__ import annotations
import base64
import json
import os
import secrets
import re
import shutil
import subprocess
import sys
import threading
import time

from .agents import (MCP_SERVER_NAME, install_path_entry, remove_path_entry, shell_profile,
                     shell_profiles, unwire_coding_agents)
from .colour import MIDDOT, TICK, bold, dim, good, heading, url, warn
from .core import (APPLIANCE_DIR, BOOT_WAIT_SECONDS, MODE_CORE,
                   MODE_SERVICE, OVERRIDE_FILE, SetupError, prompt)
from .bolt import ensure_public_host
from .pins import pin_before_start
from .dockerlib import (DEFERRED_WHY, _compose, _docker, core_services, deferred_services,
                        retire_monitoring,
                        announce_github_token, appliance_containers, compose_command, compose_env,
                        find_mode_container, refresh_floating_images, running_modes,
                        stray_sandbox_containers, other_running_appliances,
                        take_everything_down)
from .settings import (embabel_command, compose_project, console_url, env_file, instance, installed_instances,
                       remember_mode, resume_command, version_pin_conflict)
from .status import STATUS
from .steps import probe

# The pre-key model warnings, matched on the app's own phrasing rather than on
# the logger name alone — so a genuinely broken model configuration, which says
# something else, still reaches the terminal.
BOOT_AWAITING_KEY = re.compile(
    r"is not registered.*(awaiting a key|falling back to the 'setup-required')")


# The server's first-boot block, recognised by what it ASKS FOR rather than by
# its wording: whichever way it is phrased, a block telling somebody to run setup
# is one this process is already the answer to.
SETUP_IS_DRIVING = re.compile(r"ACTION REQUIRED|Setup token:|worlds\.py|me\.py|setup\.py", re.IGNORECASE)


# No single log line should be able to flood a terminal. Wide enough for a real
# message, short enough that a thirty-model inventory does not arrive twice.
BOOT_LINE_MAX = 200

# NEVER CHANGE THIS STRING. It is the ownership marker carried by every shim
# this installer has ever written, including the old install.sh heredoc ones;
# uninstall must continue to recognise those after the writer moves or changes.
SHIM_OWNERSHIP_MARKER = "Written by install.sh"

# What the boot warned about, collected by the follower and reported as one line
# by [report_boot_warnings]. A plain list: appended from the pump thread and read
# once it has stopped, and list.append is atomic under the GIL.
warnings: list[str] = []


def follow_boot_log(container: str) -> subprocess.Popen | None:
    """Stream the app's OPERATOR CONSOLE during first boot, and nothing else.

    The app prints a designed block — bordered with box rule — carrying the setup
    token and what to do next. Around it a JVM narrates itself: the Spring banner,
    the ASCII art, sixty-odd INFO lines, and a listing of every model the machine
    can see. Piping all of that to a first-time terminal buried the one part
    written for a person, and read as something having gone wrong.

    So: print the bordered block, and any WARN or ERROR, and drop the rest. A boot
    that fails still says so; a boot that works says only what it meant to.

    EXCEPT that "any WARN" was too generous, and a fresh boot proved it. Before a
    key exists the model provider warns once PER ROLE that its default LLM is not
    registered — eleven warnings, each appending the full list of every model the
    machine can see, which on a laptop with local models is thirty entries and
    two thousand characters. Eleven of those is the wall of noise this filter was
    written to prevent, arriving through the one door left open.

    They are also, by the app's own words, expected: "This deployment is awaiting
    a key, so that is expected." Setup supplies that key about ninety seconds
    later. So they are counted rather than printed, and reported as one line —
    and any other long line is truncated, because no single log line should be
    able to flood a terminal somebody is trying to read.
    """
    try:
        # encoding + errors: same reason as _docker() in dockerlib.py -- on
        # Windows, text=True without encoding decodes the container log stream
        # as cp1252 and the pump thread dies on the first non-cp1252 byte
        # (typically the box-rule glyphs the app itself uses to frame the
        # setup-token block). errors="replace" is safe: the pump only looks
        # for ASCII substrings and Unicode borders that arrive intact in UTF-8.
        # stdin=DEVNULL because the follower does not read stdin, but on Windows
        # a Popen without an explicit stdin inherits the parent's console handle
        # and does not release it cleanly on terminate(). The wizard then asked
        # for the username with the console handle still contended, and the
        # characters somebody typed echoed back mid-prompt (observed: "to sign in"
        # rendered as "toaeign in" after typing "ae") while the newline leaked
        # to the parent shell, which read it as a command. DEVNULL costs the
        # follower nothing -- it never reads stdin -- and keeps the terminal
        # single-owner throughout, on every platform.
        proc = subprocess.Popen(
            ["docker", "logs", "-f", "--tail", "0", container],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            encoding="utf-8", errors="replace",
        )
    except (subprocess.SubprocessError, OSError):
        return None  # the log is a nicety; setup does not depend on it

    def pump() -> None:
        inside = False
        awaiting_key = 0
        block: list[str] = []
        for line in proc.stdout:
            line = line.rstrip("\n")
            border = line.strip().startswith("═") and len(line.strip()) > 8
            if border:
                inside = not inside
                # The token block is the one thing here written for a person, so
                # the tally lands just before it rather than after the wizard has
                # already moved on.
                if inside and awaiting_key:
                    STATUS.log(f"  {MIDDOT} {awaiting_key} model-role warning(s): no provider key yet. "
                               "Setup asks for one next.")
                    awaiting_key = 0
                if inside:
                    block = [line]
                    continue
                # THE BLOCK IS ADDRESSED TO SOMEBODY ELSE. The server prints
                # "ACTION REQUIRED — run ./worlds.py" for the operator who started
                # a container by hand. Relayed HERE it is addressed to the process
                # already doing it: ./worlds.py exec'd this, the next line printed
                # is "✓ Setup token read from its log", and the block hands over a
                # token it says in its own words you will not need. Correct where
                # it was written, wrong where it arrives — so setup, which IS the
                # answer to it, does not repeat the question.
                block.append(line)
                if not any(SETUP_IS_DRIVING.search(entry) for entry in block):
                    for entry in block:
                        STATUS.log(entry)
                block = []
                continue
            if inside:
                block.append(line)
                continue
            if not inside and BOOT_AWAITING_KEY.search(line):
                awaiting_key += 1
                continue
            if " ERROR " in line:
                STATUS.log(line if len(line) <= BOOT_LINE_MAX
                           else line[:BOOT_LINE_MAX] + dim(" …"))
                continue
            # A WARN IS COUNTED, NOT PRINTED. Streaming them was whack-a-mole:
            # each one that turned out to be ours rather than the operator's got
            # demoted at the source, and the next boot found another — a repo
            # naming nag, a token that 403s and falls back cleanly. None of them
            # were things the person installing could act on, and all of them
            # arrived as raw log lines in the middle of a first run, which reads
            # as the product having gone wrong.
            #
            # ERROR still prints verbatim, because a boot that fails must say so.
            # The count and the command to read them keep the ones that matter
            # reachable without putting a JVM's inner monologue on screen.
            if " WARN " in line:
                warnings.append(line)

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    return proc


def report_boot_warnings(container: str) -> None:
    """One line for whatever the boot warned about, after the log stops.

    Reported at the END rather than as they arrive: during boot the person is
    watching for the setup token, and a running tally competes with the one
    thing they are waiting for. Silent when nothing warned, which is the case
    this is meant to become.
    """
    if not warnings:
        return
    count = len(warnings)
    STATUS.log(f"  {MIDDOT} " + dim(
        f"{count} warning{'s' if count != 1 else ''} during boot, none fatal — "
        f"read them with:  docker logs {container} 2>&1 | grep WARN"))
    warnings.clear()


def announce_version_pin(mode: str) -> None:
    """Say when .env's image pin overrides the tag this checkout was written against.

    Silent unless they actually disagree, which is the only case worth a line — and
    the case that costs an afternoon: a branch carries its own compose files AND the
    server tag they expect, so an EMBABEL_VERSION left in .env from some earlier
    experiment quietly runs the branch's plumbing against a different server. Both
    halves are legitimate, which is exactly why nobody suspects either.

    A NOTE, not a refusal. The pin is the operator's own and wins on purpose; this
    only makes sure they meant it.
    """
    conflict = version_pin_conflict(mode)
    if not conflict:
        return
    pinned, expected = conflict
    print(f"  {warn('!')} .env pins EMBABEL_VERSION={pinned}, and this checkout expects {expected}.")
    print("  " + dim(f"    Your pin wins. Remove that line from .env to run the {expected} "
                     "this checkout was written against."))
    print()


def start_deferred(mode: str) -> subprocess.Popen | None:
    """Pull and start everything that is not needed to answer, behind the user.

    Started detached and NOT waited on: the wizard is the next thing to happen and
    it takes minutes, which is exactly the window this needs. Its output goes
    nowhere on purpose — two progress bars fighting over one terminal is worse
    than no progress bar, and `docker compose ps` tells the truth at any time.

    Nothing here is a boot dependency, so a failure is not fatal: the services are
    reconciled by the `up -d` that every later run performs, and the closing
    message says what was still on its way.
    """
    services = deferred_services()
    coming = ", ".join(DEFERRED_WHY[name] for name in services if name in DEFERRED_WHY)
    print(f"  Downloading in the background: {coming}.")
    print("  You can start using the appliance now — check on them any time with")
    print(f"    {embabel_command('status')}\n")
    cmd = compose_command(mode, "up", "-d", *services)
    try:
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env=compose_env())
    except (subprocess.SubprocessError, OSError):
        return None  # not fatal: the next run reconciles


def warn_if_conversion_pending() -> None:
    """Say so if docling has not arrived yet, because the difference is visible.

    Without it a PDF still ingests — the pipeline falls back to flat Tika text —
    but tables come out mangled, figures are lost, and a SCANNED document yields
    almost nothing, since the OCR lives in docling. That degradation is otherwise
    an ERROR in a log nobody has open, and the user's conclusion is that the
    product is bad at PDFs rather than that it is still downloading.
    """
    # By project label, like every other lookup: the default instance's docling
    # is not this instance's.
    run = _docker("ps", "--filter", f"label=com.docker.compose.project={compose_project()}",
                  "--filter", "label=com.docker.compose.service=docling", "--format", "{{.Names}}")
    if run and run.returncode == 0 and run.stdout.strip():
        return
    print("  Structured document conversion is still downloading (about 2GB).")
    print("  Documents added before it finishes ingest as plain text — tables flattened,")
    print("  and a scanned PDF may yield nothing. Re-add anything important afterwards.\n")


# ── upgrade ─────────────────────────────────────────────────────────────────
#
# TWO THINGS MOVE, and for a long time the CLI moved only one. The images are
# most of the appliance, but the CHECKOUT is the rest of it — the compose files,
# the Neo4j tag they pin, this script, the skills. An upgrade that pulled images
# and left the checkout behind ran new servers against old plumbing, and the Me
# app's menu (which did pull the checkout) and `embabel upgrade` meant different
# things by the same word.
#
# ONTO THE LATEST BUILD, never a local one. Nothing here builds an image: the
# compose files are pull-only by design — see the note at the top of
# docker-compose.yml — and this verb's whole job is to land on what the registry
# publishes. A locally-built image being replaced is therefore the CORRECT
# outcome, but it is still surprising, so it is reported rather than silent.
#
# --ff-only, ALWAYS. A dirty or diverged checkout is left exactly as it is and
# the images still update. An upgrade command that rebases somebody's work, or
# that refuses to do the other half because of it, is worse than one that says
# what it skipped.












































































def remove_stray_sandboxes() -> None:
    """Offer to remove them, rather than just doing it.

    A developer running an assistant from an IDE has sandbox containers carrying the
    same label and a different jvm id, and killing those mid-session is precisely
    the bug the per-jvm scoping exists to prevent ("container is not running", from
    a test jvm nuking a dev session). This script cannot tell the two apart, so it
    asks instead of guessing.
    """
    strays = stray_sandbox_containers()
    if not strays:
        return
    print(f"\n  {len(strays)} code-sandbox container(s) are still on the host:")
    for name in strays[:8]:
        print(f"    {name}")
    if len(strays) > 8:
        print(f"    … and {len(strays) - 8} more")
    print("  They are siblings of the appliance, not part of it, so `down` left them.")
    print("  If you are running an assistant from an IDE, ITS sandboxes are in this list.")
    answer = prompt("  Remove them? [Y/n]: ").strip().lower()
    if answer not in ("", "y", "yes"):
        print("  Left alone.")
        return
    run = _docker("rm", "-f", *strays, timeout=60)
    if run and run.returncode == 0:
        print(f"  Removed {len(strays)} sandbox container(s).")


def cli_shim_paths() -> list[str]:
    """Every place setup could have written the `embabel` launcher.

    `$EMBABEL_BIN_DIR` first because an installer run with it set put the shim there, then the
    default. Both are checked rather than one, so an uninstall undoes an install that used either.
    """
    seen: list[str] = []
    for directory in (os.environ.get("EMBABEL_BIN_DIR"), os.path.join(os.path.expanduser("~"), ".local", "bin")):
        if directory and directory not in seen:
            seen.append(directory)
    return [os.path.join(d, "embabel") for d in seen]


def write_cli_shim() -> str | None:
    """Put this checkout's `embabel` forwarder on PATH without taking that name from anyone else.

    The name is not rare, so an existing file must prove that this installation wrote it before
    it is replaced. Uncertainty means refusal: leaving this appliance reachable by path is better
    than silently destroying a different tool.
    """
    path = cli_shim_paths()[0]
    directory = os.path.dirname(path)
    checkout = os.path.realpath(APPLIANCE_DIR)
    # A forwarder, not a copy: updating the checkout updates the command too,
    # instead of leaving a second CLI version to drift.
    body = f'''#!/bin/sh
# Forwards to the Embabel appliance in {checkout}. {SHIM_OWNERSHIP_MARKER}.
# Finds a Python 3.9+ each run, preferring newer. The CLI repeats this check
# as a sentence.
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
  command -v "$cand" >/dev/null 2>&1 || continue
  if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    exec "$cand" "{checkout}/embabel" "$@"
  fi
done
echo "embabel: Python 3.9+ not found (macOS: brew install python@3.12)" >&2
exit 1
'''
    if os.path.lexists(path):
        if not is_our_shim(path):
            print(f"  Left {path} alone — it is not the launcher this installation wrote.")
            print("  " + dim(f"    Run this appliance by path: {checkout}/embabel"))
            print("  " + dim("    Or set EMBABEL_BIN_DIR to your own directory and run setup again."))
            return None
        try:
            with open(path) as f:
                if f.read() == body and os.access(path, os.X_OK):
                    return path
        except OSError:
            pass
    try:
        os.makedirs(directory, exist_ok=True)
        with open(path, "w") as f:
            f.write(body)
        os.chmod(path, 0o755)
    except OSError as e:
        print(f"  Could not install the 'embabel' command to {path}: {e}")
        return None

    # Another command may legitimately win; report it rather than guessing how
    # somebody else intended their PATH to be ordered.
    existing = shutil.which("embabel")
    if existing and os.path.realpath(existing) != os.path.realpath(path):
        print(f"  {warn('!!')} another \"embabel\" already comes first on your PATH:")
        print("  " + dim(f"      {existing}"))
        print("  " + dim(f"    To use this one, put {directory} ahead of it,"))
        print("  " + dim(f"    or run it by path: {path}"))
        print()

    if any(os.path.realpath(entry) == os.path.realpath(directory)
           for entry in os.environ.get("PATH", "").split(os.pathsep) if entry):
        print(f"  Installed the 'embabel' command to {directory}.")
        print()
        return path

    # NOT ON PATH, WHICH IS THE DEFAULT ON macOS. ~/.local/bin is on PATH out of the box
    # on most Linux distributions and on no Mac, so this branch is not an edge case — it
    # is every Mac. It used to print the export line for the user to copy, immediately
    # before setup said "after this, use the 'embabel' command", which was false for them
    # and false in the same breath. So the line goes in, in a block uninstall can take
    # back out, and the message says what was done rather than what they must do.
    #
    # EXCEPT when EMBABEL_BIN_DIR named the directory. Somebody who chose where the
    # command goes has a PATH they manage themselves, and editing their profile after
    # they told us where to write would be presumption, not help.
    profile = shell_profile()
    chosen_by_hand = bool(os.environ.get("EMBABEL_BIN_DIR"))
    if profile and not chosen_by_hand and install_path_entry(directory, profile):
        shown = profile.replace(os.path.expanduser("~"), "~", 1)
        print(f"  Installed the 'embabel' command to {directory}, and added that to {shown}.")
        # THIS shell cannot be changed from here — a child process cannot alter its
        # parent's environment — so say the two things that are true right now rather
        # than leave them to discover the command still does not run.
        print("  " + dim("New terminals have it. In this one:  exec $SHELL"))
        print("  " + dim(f"Or run it by path any time:  {path}"))
    else:
        # Name the profile THEY use: sending a bash user to ~/.zshrc gives them no usable
        # instruction at all.
        shown = profile.replace(os.path.expanduser("~"), "~", 1) if profile else "your shell profile"
        print(f"  Installed the 'embabel' command to {directory}, which is NOT on your PATH.")
        print(f'  Add it to {shown}:  export PATH="{directory}:$PATH"')
        print("  " + dim(f"Until then, run it by path:  {path}"))
    print()
    return path


def is_our_shim(path: str) -> bool:
    """Whether [path] is the forwarder setup wrote for THIS installation.

    NEVER delete an `embabel` on PATH just because it is called `embabel`. It is not a rare name,
    and setup warns when it finds one already ahead of ours. An uninstall that removed somebody's
    other tool because the names matched would be a far worse bug than the one this fixes.

    So: it must be a small file, it must carry the installer's ownership line, and it must name the
    directory this script is running from. Anything else is left alone.
    """
    try:
        if os.path.getsize(path) > 4096:
            return False
        # errors="replace" rather than a bare open(): on Windows the default
        # encoding is the system ANSI codepage (cp1252 for en-US), and any
        # sibling `embabel` on PATH written by another tool — a compiled .exe,
        # a UTF-8 script, anything with a non-cp1252 byte — killed uninstall
        # here with a UnicodeDecodeError. We only look for two ASCII strings
        # below, so decoding losses on unrelated files are harmless.
        with open(path, encoding="utf-8", errors="replace") as f:
            body = f.read()
    except OSError:
        return False
    # APPLIANCE_DIR, not dirname(__file__). The shim points at the CHECKOUT, and
    # this asks "is that us?" — from inside the package the two are different
    # directories, the answer would always be no, and uninstall would quietly stop
    # removing the command it put on PATH.
    here = os.path.realpath(APPLIANCE_DIR)
    return SHIM_OWNERSHIP_MARKER in body and here in body


def remove_cli_shim() -> None:
    """Take the `embabel` command off PATH — the one this installation put there.

    The checkout stays so `./worlds.py` can set up again, but uninstall promises to remove the
    machine-wide command along with the machine-local configuration. Leaving it behind would make
    the checkout look installed after the appliance and its configuration were gone.
    """
    removed = []
    for path in cli_shim_paths():
        if not os.path.exists(path):
            continue
        if not is_our_shim(path):
            print(f"  Left {path} alone — it is not the launcher this installation wrote.")
            continue
        try:
            os.remove(path)
            removed.append(path)
            print(f"  Removed {path}.")
        except OSError as e:
            print(f"  Could not remove {path}: {e}")

    # AND THE PATH LINE, wherever setup put one. Removing the command and leaving the
    # directory on PATH would leave a profile pointing at nothing — harmless, and exactly
    # the kind of residue that makes people distrust an uninstall. Every supported profile
    # is checked rather than the current shell's, because the shell that installs is not
    # always the shell that uninstalls, and only complete appliance-owned blocks are taken.
    for profile in shell_profiles():
        if remove_path_entry(profile):
            print(f"  Removed the appliance PATH line from {profile}.")

    # A DIFFERENT `embabel` may still answer, and saying so is the point: otherwise the next
    # `which embabel` shows a hit and the uninstall looks like it failed again, which is exactly
    # the confusion this function exists to end.
    if removed:
        survivor = shutil.which("embabel")
        if survivor:
            print(f"  NOTE: another 'embabel' is still on your PATH: {survivor}")
            print("        That one is not ours, and it has been left alone.")


def uninstall() -> None:
    """--uninstall: back to the state a fresh clone is in.

    Everything --fresh removes, plus the machine-local configuration it leaves
    behind — which is the whole point. Re-running setup after --fresh never asks
    for a provider key, a timezone or a realms directory, because .env still has
    them, so it exercises none of the path a new user walks. That makes --fresh
    the wrong tool for testing the thing developers most need to test.

    Images and the local embedding model are KEPT, deliberately and always: the
    embedding artifact alone is over a gigabyte and re-downloading one that has
    not changed is pure waste. Nothing here offers to remove them.

    Realm checkouts are not touched either. They are their own repositories and
    somebody's work in progress; this script has no business deleting them.

    The `embabel` COMMAND does go, though — see [remove_cli_shim]. Setup puts a launcher on
    PATH, so an uninstall that left it there would leave machine-local installation state behind.
    The installation DIRECTORY stays: this script is running from it, and `./setup.py` sets up
    again from there.
    """
    if len(installed_instances()) > 1:
        print(f"  --uninstall removes the '{instance()}' appliance. Others here are untouched:")
        print(f"    {', '.join(n for n in installed_instances() if n != instance())}")
    else:
        print("  --uninstall returns this checkout to the state a fresh clone is in.")
    print("\n  DELETED:")
    print("    the appliance's entire state — account, world, graph, documents, dashboards")
    print(f"    {env_file()} — your provider key, timezone, and realms directory")
    print(f"    {OVERRIDE_FILE} — the folders shared with the assistant")
    print(f"    the '{MCP_SERVER_NAME}' MCP registration — only where it points at THIS appliance")
    print("    its marked Codex token block in supported shell profiles — only when registration ownership is verified")
    print("    its marked PATH block in supported shell profiles — the line setup added to reach the command")
    for path in cli_shim_paths():
        if os.path.exists(path) and is_our_shim(path):
            print(f"    {path} — the 'embabel' command setup put on your PATH")
            break
    print("    any stray code-sandbox container (asked separately — a dev JVM may own one)")
    print("\n  KEPT:")
    print("    images and the local embedding model — over a gigabyte, and unchanged")
    print("    realms/ and any realm checkout — your repositories, not ours")
    print("    this directory itself: `./worlds.py` sets up again from here\n")
    answer = prompt("  Type 'yes' to uninstall: ").strip().lower()
    if answer != "yes":
        raise SetupError("Not uninstalled — nothing was touched.")

    take_everything_down()
    remove_stray_sandboxes()
    # Before .env goes: unwiring verifies each registration's URL against this
    # install's ports, and those ports live in .env.
    unwire_coding_agents()
    for name in (env_file(), OVERRIDE_FILE):
        if os.path.exists(name):
            os.remove(name)
            print(f"  Removed {name}.")
        else:
            # Silence here made a re-run look like nothing happened at all.
            print(f"  No {name} to remove.")

    # The `embabel` command is one per MACHINE, not one per instance. Taking it
    # away while another appliance is still installed would uninstall one thing
    # and break a different one — so it goes only with the last of them.
    elsewhere = other_running_appliances()
    remaining = [n for n in installed_instances() if n != instance()]
    if elsewhere:
        # The command is one per machine and points at whichever checkout wrote
        # it. Removing it here left a running appliance in another directory with
        # no way to be driven except by path — an uninstall that broke something
        # it was never asked about.
        print(f"\n  Kept the 'embabel' command: an appliance is running from {elsewhere[0]}"
              + (f" (and {len(elsewhere) - 1} more)" if len(elsewhere) > 1 else "") + ".")
        print(f"  Done — instance '{instance()}' is gone.\n")
    elif remaining:
        print(f"\n  Kept the 'embabel' command: {', '.join(remaining)} still installed here.")
        print(f"  Done — instance '{instance()}' is gone.\n")
    else:
        remove_cli_shim()
        print("\n  Done — this checkout is back to the state a fresh clone is in.")
        # NOT `embabel up`: remove_cli_shim() just deleted that command, three
        # lines above. Naming it here sent people to a command this branch had
        # personally removed. ./setup.py is what survives in the checkout, and
        # it is the same entry for either door — which is right, because .env
        # is gone too, so there is no longer a mode to resume.
        print("  `./setup.py` sets it up again from here.\n")


def ensure_mode(mode: str) -> bool:
    """Bring the chosen mode up. Returns True if it was not already running — the
    caller then follows the container log, because the first boot is a designed
    experience (the operator console), not a thing to hide.

    Also the moment the host's timezone reaches .env: it must exist before the
    `up -d` below, and `up -d` then applies it — compose sees the changed
    environment and recreates the mode, so a UTC container heals on re-run.

    ALWAYS runs `compose up -d`, even when the mode is already up. "The
    container is running" does NOT mean "this compose file is applied": a service
    added since the last run (the console was, once) would never be created, and
    the operator sees a service in their YAML with nothing behind it. `up -d`
    is idempotent — it reconciles and leaves running containers alone."""
    ensure_timezone()
    ensure_wallet_key()
    remember_mode(mode)
    announce_github_token()
    announce_version_pin(mode)
    modes = running_modes()
    other = next(((svc, name) for svc, name in modes.items() if svc != MODE_SERVICE[mode]), None)
    if other:
        # Asked for this mode while the other one is up. Offer the precise fix rather
        # than "docker compose down", which is ambiguous (which file?) and heavier
        # than needed — it would take the shared graph and metrics down too.
        other_mode = "me" if other[0] == "assistant" else "worlds"
        print(f"  The {other_mode} mode is running ({other[1]}), and only one mode may run at a time")
        print("  — they share one graph, so two would duplicate every scheduled job.\n")
        answer = prompt(f"  Stop the {other_mode} mode and continue? [Y/n]: ").strip().lower()
        if answer not in ("", "y", "yes"):
            raise SetupError(
                f"Left the {other_mode} mode running. Stop it when you are ready:\n"
                f"    {embabel_command('compose', '--' + other_mode, 'stop', other[0])}"
            )
        _compose(other_mode, "stop", other[0])
        print()
    running = modes.get(MODE_SERVICE[mode])
    if running:
        print(f"  The {mode} mode is running — reconciling with the compose file.\n")
        # CHECKED, like the first-start path below. This reconcile swallowed its exit
        # code, so a compose failure here — a missing credential helper, an image that
        # will not pull — printed docker's error and then let setup carry on as though
        # the appliance were up. The run ended minutes later asking for a setup token
        # that no process had printed, with the real reason long scrolled away.
        ensure_public_host(mode)
        run = _compose(mode, "up", "-d")
        if run.returncode != 0:
            raise SetupError(
                f"docker compose up failed while reconciling the {mode} mode.\n"
                "  The error above is docker's. What usually causes it:\n"
            f"    {embabel_command('doctor')}   —  or `sh doctor.sh` from this folder, which needs "
            "nothing installed and works even when that command does not exist yet."
            )
        retire_monitoring(mode)
        print()
        return False
    print(f"  Starting the {mode} mode — pulling what it needs to answer.\n")
    # AND IT MUST ACTUALLY PULL. `up -d` fetches an image it does not have and
    # stops there, which for `latest` means a machine that pulled it once never
    # sees another build — so a fresh install on a machine with any history runs
    # whatever was published the first time. The line above has been claiming
    # otherwise for as long as it has existed.
    refresh_floating_images(mode, core_services(mode))
    # After the pull, before the containers exist: a new Embabel install's store is
    # pinned from the image that is about to run it (see pins.pin_before_start).
    pin_before_start(mode)
    # And the name Bolt is advertised by, which compose reads from .env (see bolt.py).
    ensure_public_host(mode)
    run = _compose(mode, "up", "-d", *core_services(mode))
    if run.returncode != 0:
        # The message names docker, because the failure is docker's and the reason is
        # already on screen above this line. Pointing at `doctor` rather than restating
        # it: the credential-helper case in particular says "error getting credentials"
        # and needs a fix nothing in this sentence could carry.
        raise SetupError(
            f"docker compose up failed for the {mode} mode.\n"
            "  The error above is docker's. What usually causes it:\n"
            f"    {embabel_command('doctor')}   —  or `sh doctor.sh` from this folder, which needs "
            "nothing installed and works even when that command does not exist yet."
        )
    retire_monitoring(mode)
    print()
    start_deferred(mode)
    return True


def reset_credentials(container: str, base: str) -> None:
    """Forgotten password: reopen first-run setup WITHOUT touching the data.

    The operator account lives in two small files under the volume's admin
    directory — the credential hashes and the completed-setup record. The
    appliance refuses to reopen setup over the API by design (410 Gone,
    forever), but whoever controls docker on this host already owns the
    appliance, so host-side recovery is honest: delete those two files, restart
    the container, and the normal wizard runs again. The graph, documents and
    memories live elsewhere on the volume and are untouched."""
    print("  Resetting the operator account: this deletes the username/password")
    print("  and walks first-run setup again. Everything the appliance knows —")
    print("  graph, documents, memories — is kept. Have your model-provider key")
    print("  handy (or exported); the wizard verifies it again.")
    answer = prompt("  Reset the account and re-run setup? [y/N]: ").strip().lower()
    if answer not in ("y", "yes"):
        raise SetupError("Not reset — nothing was touched.")
    run = _docker("exec", container, "rm", "-f",
                  f"{ADMIN_DIR}/.credentials.yml", f"{ADMIN_DIR}/.setup.json")
    if run is None or run.returncode != 0:
        detail = run.stderr.strip() if run is not None else "docker is not available"
        raise SetupError(f"Could not remove the account files from {container}:\n{detail}")
    print("  Account removed — restarting the appliance to reopen setup…")
    restart = _docker("restart", container, timeout=180)
    if restart is None or restart.returncode != 0:
        raise SetupError(f"docker restart {container} failed.")
    # Hand back to the wizard only once setup is actually answering again;
    # otherwise the token (still in the old log) is found instantly and the
    # first API call lands on a container that is mid-boot.
    deadline = time.monotonic() + BOOT_WAIT_SECONDS
    print("  Waiting for the appliance to come back…\n")
    while True:
        state = probe(base)  # AlreadySetUp here would mean the files came back
        if state == "pending":
            return
        if time.monotonic() >= deadline:
            raise SetupError(
                f"The appliance did not come back within {BOOT_WAIT_SECONDS}s.\n"
                f"Watch it with:  docker logs -f {container}"
            )
        time.sleep(3)


# Where the appliance keeps its operator account, inside the container: the
# credential file (bcrypt hashes) and the setup record. --reset-password
# deletes exactly these two; everything else on the volume is data.
ADMIN_DIR = "/data/embabel/assistant/admin"
def host_timezone() -> str | None:
    """The host's IANA zone name (e.g. Australia/Sydney), or None if unknowable.
    /etc/localtime is a symlink into a zoneinfo tree on macOS and most Linuxes;
    Debian-family systems also write the name to /etc/timezone."""
    try:
        target = os.readlink("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    try:
        with open("/etc/timezone") as f:
            return f.read().strip() or None
    except OSError:
        return None
def ensure_timezone() -> None:
    """Hand the containers the host's timezone. The images default to UTC, which
    puts every "now" the assistant utters — and every schedule it keeps — hours
    off for most of the planet. Compose interpolates TZ from the shell or .env;
    detect the zone once and write it to .env if the operator hasn't chosen one
    (either kind of existing choice wins and is never rewritten)."""
    if os.environ.get("TZ"):
        return  # exported in the shell — compose sees it directly
    if os.path.exists(".env"):
        with open(".env") as f:
            if any(line.strip().startswith("TZ=") for line in f):
                return
    zone = host_timezone()
    if not zone:
        return  # undetectable: the containers stay on UTC, as before
    with open(".env", "a") as f:
        f.write(
            "\n# Host timezone, detected by setup.py — without it the containers run UTC\n"
            "# and the assistant's clock is hours off. Edit or remove freely.\n"
            f"TZ={zone}\n"
        )
    print(f"  Wrote TZ={zone} to .env — the appliance will keep your local time.")
def ensure_wallet_key() -> None:
    """Give this appliance a master key for its wallet, once, and keep it.

    The wallet holds what you connect WITH: an OAuth app's client id and secret,
    a realm's API key, the tokens that come back from authorizing. It is always
    encrypted, and the key is `EMBABEL_KEY_SECRET`. Without one the server
    generates a key per start — so everything you typed decrypts to nothing after
    a restart and the appliance reports an EMPTY wallet rather than an error. You
    would re-enter your GitHub credentials every time you restarted, and never be
    told why.

    Written to .env rather than held in the image, because it must outlive both:
    `--fresh` wipes the volumes and this survives, which is what makes a wallet
    written before the wipe still readable after it.

    NEVER REGENERATED. A new key does not unlock what the old one wrote — it makes
    every stored credential undecryptable — so an existing value of any kind wins,
    exactly like TZ above."""
    if os.environ.get("EMBABEL_KEY_SECRET"):
        return  # exported in the shell — compose sees it directly
    if os.path.exists(env_file()):
        with open(env_file()) as f:
            if any(line.strip().startswith("EMBABEL_KEY_SECRET=") for line in f):
                return
    # 32 bytes, base64 — AES-256, the length WalletEncryptionConfiguration validates.
    key = base64.b64encode(secrets.token_bytes(32)).decode()
    with open(env_file(), "a") as f:
        f.write(
            "\n# The key your wallet is encrypted with, generated once by setup.py.\n"
            "# KEEP IT. Changing or losing it does not lock you out of the appliance —\n"
            "# it makes every credential already stored undecryptable, and you re-enter\n"
            "# them. Back it up with anything else you would not want to retype.\n"
            f"EMBABEL_KEY_SECRET={key}\n"
        )
    os.chmod(env_file(), 0o600)
    print("  Generated EMBABEL_KEY_SECRET in .env — your saved credentials now survive a restart.")
