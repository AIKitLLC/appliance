"""What every `embabel` verb needs: the facade, and the few helpers they share.

WHY setup.py IS LOADED BY PATH. It is a script at the checkout root, not a module in
this package, and it is the facade every command speaks through — `s.doctor_checks`,
`s.take_everything_down`. Loading it here rather than in each command module means it
is executed once and every verb sees the same constants.

The launcher keeps only argument parsing and main(); the verbs themselves live beside
this file, grouped by what they are about.
"""

from __future__ import annotations
import importlib.util
import os
import sys

from .settings import embabel_command

# The checkout root — the directory setup.py, the compose files and the launcher share.
# dirname twice because this file is one level down, inside the package.
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_setup():
    """setup.py as a module. It guards main() behind __name__, so importing it is
    just its constants and functions — no side effects."""
    spec = importlib.util.spec_from_file_location("embabel_setup_facade",
                                                  os.path.join(HERE, "setup.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s = load_setup()

import argparse  # noqa: E402 — after the facade, which sets sys.path up
import json      # noqa: E402
import os        # noqa: E402
import subprocess  # noqa: E402
import sys        # noqa: E402

from .core import prompt  # noqa: E402


def run_setup(*argv: str) -> int:
    """Hand off to setup.py as a child process rather than calling main().

    A child gets its own argv and its own exit code, and the wizard's prompts keep
    the terminal they expect. Calling main() in-process would mean rewriting argv
    underneath it and catching its SystemExit, for nothing.
    """
    # THE INSTANCE HAS TO CROSS THE PROCESS BOUNDARY. A child that does not know
    # which appliance it is about defaults to the first one — so `--instance green
    # up` set up `appliance`, and `--instance green uninstall` offered to delete
    # it. The environment carries it because setup.py is also run directly, by
    # ./me.py and ./worlds.py, where there is no flag to pass.
    env = dict(os.environ, EMBABEL_INSTANCE=s.instance())
    return subprocess.call([sys.executable, os.path.join(HERE, "setup.py"), *argv], env=env)


def current_mode() -> str | None:
    """Whichever mode is up, by its compose service label — not by guessing."""
    for service in s.running_modes():
        return "me" if service == "assistant" else "worlds"
    return None


def resolved_mode(requested: str | None) -> str:
    """What the user asked for, else what is running, else what was set up here,
    else worlds.

    The .env step matters more than it looks. It records the door this machine was
    set up as, so `embabel down` followed by `embabel up` returns to the SAME
    product — without it, somebody who installed an assistant got a world runtime
    back on the same graph, with nothing saying why. That the installer now also
    defaults to Worlds narrows the gap but does not close it: `EMBABEL_MODE=me`
    installs an assistant, and this must still come back to one.

    Worlds is the last resort because it is what worlds.embabel.com hands a
    developer who has installed nothing yet — the same reason install.sh opens
    that door.
    """
    return requested or current_mode() or s.configured_mode() or "worlds"


def _sample_target(args):
    """The running appliance, and a credential for it. Shared by every sample verb."""
    mode = current_mode() or s.configured_mode()
    if not mode:
        print(f"  Nothing is running.  `{embabel_command('up')}` starts it.")
        return None, None
    container = s.find_mode_container(mode)
    base = s.container_base_url(container) if container else None
    if not container or not base:
        print(f"  The appliance is not up.  `{embabel_command('up')}` starts it.")
        return None, None
    return base, s.sample_credential(container)


def _emit(as_json: bool, ok: bool, message: str, **extra) -> int:
    """One result, in whichever register the caller reads. --json exists for the Me
    app, whose menu calls these verbs rather than carrying a second implementation
    of them; a human gets a sentence."""
    if as_json:
        print(json.dumps({"ok": ok, "message": message, **extra}))
    else:
        print(f"  {message}")
    return 0 if ok else 1


def resolve_instance(args) -> int:
    """Decide which appliance every verb in this process is about.

    The order is: what was asked for, what the environment says, THE ONLY ONE
    THERE IS, else the default name. That third clause is the whole design — with
    one appliance there is nothing to disambiguate and nothing to type, whatever
    it happens to be called.

    With several and no choice made, this refuses rather than guessing. Picking
    one would be picking somebody's production graph as often as not.
    """
    asked = args.instance or os.environ.get("EMBABEL_INSTANCE") or ""
    names = s.installed_instances()
    if asked:
        s.use_instance(asked)
        return _pinned(args)
    if len(names) == 1:
        s.use_instance(names[0])
        return _pinned(args)
    if len(names) > 1 and args.command not in ("instances", "where", "completion", None):
        print(f"\n  {len(names)} appliances are installed here: {', '.join(names)}", file=sys.stderr)
        print("  Say which one:  embabel --instance <name> " + (args.command or ""), file=sys.stderr)
        print("  Or set EMBABEL_INSTANCE in your shell.\n", file=sys.stderr)
        return 2
    s.use_instance(s.DEFAULT_INSTANCE)
    return 0


def _pinned(args) -> int:
    """Pin the chosen instance's graph engine, if it predates the pin.

    HERE because every verb comes through here, and an install made before the engine
    was written down must be pinned before anything starts its containers — `upgrade`
    and `up` above all, but the first verb of a new CLI is the earliest moment there is.
    Only ever pins an install that exists (see pin_graph_engine); choosing for a new
    one is setup's. Not for the verbs that describe the command rather than an
    appliance, which have no business writing anybody's settings.
    """
    if args.command not in ("instances", "where", "completion"):
        s.pin_graph_engine(creating=False)
    return 0


def _subparsers(parser) -> dict:
    """Verb name -> (its parser, its one-line help), out of argparse's internals.

    Private API, and worth it: the alternative is a second list of verbs to keep
    in step with the first, which is the exact drift this command generates its
    completions to avoid. The help lives on the PARENT's pseudo-actions, not on
    the subparser — `add_parser(help=...)` puts it there — so both are read.
    """
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            helps = {a.dest: (a.help or "") for a in action._choices_actions}
            return {name: (sub, helps.get(name, "")) for name, sub in action.choices.items()}
    return {}


def _zsh_quote(text: str) -> str:
    """A zsh _describe entry is name:description, so a colon in the description
    ends it early. Escape those, and flatten to one line."""
    return text.strip().splitlines()[0].replace(":", "\\:").replace("'", "")


def _fish_quote(text: str) -> str:
    return text.replace("'", "").strip()
