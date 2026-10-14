"""Asking the setup questions, and posting the answers.

The QUESTIONS themselves live in wizard.py — this is the conversation: how a
field is rendered, what a rejected answer does, and the HTTP underneath. Two
rules run through it.

A rejected answer retries IN PLACE. Ending the whole wizard because a password
was four characters long, and making somebody re-answer the steps they already
got right, is a hostile way to meet a new user.

An answer the SERVER cannot check gets confirmed before it is sent. The server
accepts a step once and then refuses to reopen setup, so a username typed with a
typo is permanent the instant Enter is pressed.
"""

from __future__ import annotations
import base64
import getpass
import http.client
import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request

from .colour import MIDDOT, TICK, bold, dim, url, warn
from .core import (APPLIANCE_DIR, BOOT_WAIT_SECONDS, AlreadySetUp, SetupError,
                   Timeout, TokenRejected, Unreachable, prompt)
from .dockerlib import (_compose, _docker, boot_failure, container_started_at,
                        container_status, find_mode_container)
from .settings import (PHONE_HOME_DOC_URL, PHONE_HOME_ENDPOINT, TRIAL_TERMS_DOC_URL,
                       api_address, console_url, embabel_command, phone_home_on,
                       resume_command, set_env_var)
from .status import STATUS, boot_phase, wait_until_serving
from .colour import heading
from .words import copy_text, say
from .seed import account_username, remember_account, remember_provider_key
from . import wizard

TOKEN_HEADER = "X-Embabel-Setup-Token"

# How long to wait on a setup call. Most are the appliance talking to itself and answer
# in milliseconds; the number is a backstop, not a budget.
CALL_TIMEOUT_SECONDS = 60

# EXCEPT THE PROVIDER STEP, which is not the appliance talking to itself. Validating a
# key means a real completion call to the provider with the provider's own client — the
# server does that deliberately, because a key that lists models can still fail the first
# real request. Sixty seconds is the appliance's budget, not OpenAI's, and a slow or
# proxied network blew through it while the appliance sat there patiently waiting: the
# installer then announced it "could not reach the appliance", which was false and sent
# somebody looking for a Docker fault.
#
# Three minutes, not the provider client's own ten: past three minutes something is
# wrong and saying so beats making a person watch a spinner for the rest of it.
PROVIDER_TIMEOUT_SECONDS = 180

# Where to prove egress, per provider — the endpoint that answers 401 fastest to an
# unauthenticated request, so a person can tell "blocked" from "bad key" in one command.
PROVIDER_PROBE_URLS = {
    "openai": "https://api.openai.com/v1/models",
    "anthropic": "https://api.anthropic.com/v1/models",
}

# A key this CLIENT adds to /complete's answer when it had to ride the restart out
# itself. NOT the server's own `restarting`, which is on every successful answer and
# means the restart is still ahead of us — reading that one as "already waited" skips
# the wait on the normal path and hands somebody a URL that dies a second later.
RODE_OUT_RESTART = "clientRodeOutTheRestart"


TOKEN_PATTERN = re.compile(r"Setup token:\s*([0-9a-f]{32,})")


# Provider -> the variable that provider's key lives in, mirroring ProviderValidator's
# PROVIDERS map on the server. If a provider is added there and not here, nothing breaks:
# it is simply not offered from the environment, and gets asked for as before.
PROVIDER_ENV = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}


# WHAT THIS APPLIANCE PREFERS where the server offers a choice and has no opinion
# worth imposing. The step definitions come from the server, which serves more
# than this deployment, so the default it ships is a general one; this is the
# appliance's own answer, applied only when the value is genuinely on offer.
#
# Pressing Enter is the commonest thing anybody does at a prompt, so the default
# is not a cosmetic detail — it is the answer most installs will actually give.
PREFERRED_DEFAULTS = {"provider": "openai"}


def provider_from_key(api_key: str | None) -> str | None:
    """Identify keys whose public prefix is unambiguous; let the operator classify the rest."""
    key = api_key or ""
    if key.startswith("sk-ant-"):
        return "anthropic"
    if key.startswith("sk-"):
        return "openai"
    return None


def provider_of(api_key: str | None) -> str:
    """Best available provider name for timeout messages."""
    provider = provider_from_key(api_key)
    return {"openai": "OpenAI", "anthropic": "Anthropic"}.get(provider, "your provider")


def _timed_out(base: str, seconds: int, waiting_on: str | None) -> Timeout:
    """The message for a call that ran out of patience, which must not blame the appliance.

    The appliance answering slowly is not the appliance being absent, and on the provider
    step it is usually not even the appliance's fault: it is mid-call to the provider,
    with the provider's own client, and a network that drops those packets rather than
    refusing them looks exactly like this. So when we know who is being waited on, the
    message names them and hands over the one command that settles it — run INSIDE the
    container, because that is where the blocked egress is.
    """
    if not waiting_on:
        return Timeout(
            f"The appliance did not answer within {seconds}s on {api_address(base)}.\n"
            "It is running — something is just taking longer than expected. Try again, "
            f"or look at what it is doing: {embabel_command('logs', '-f')}"
        )
    return Timeout(
        f"The appliance did not answer within {seconds}s, because it is still waiting "
        f"for {waiting_on}.\n"
        f"THE APPLIANCE IS FINE. It validates your key by making a real call to "
        f"{waiting_on}, and that call has not come back — which is what a proxy or "
        f"firewall that drops traffic looks like from in here.\n"
        f"Check whether the container can reach it at all:\n"
        f"    docker exec {find_mode_container() or '<the appliance container>'} curl -s -o /dev/null "
        f'-w "%{{http_code}}\\n" --max-time 20 {PROVIDER_PROBE_URLS.get(waiting_on.lower(), "https://api.openai.com/v1/models")}\n'
        f"A quick 401 means reachable. A hang or 000 means blocked, and no key will get "
        f"through until that does.\n"
        f"You can also skip this step (press Enter) and put the key in secrets.env later."
    )


def call(base: str, path: str, token: str, payload: dict | None = None,
         method: str | None = None, timeout: int = CALL_TIMEOUT_SECONDS,
         waiting_on: str | None = None) -> dict:
    """POST/GET a setup endpoint.

    `waiting_on` names the THIRD PARTY this call ends up waiting for, when there is one.
    It changes nothing about the request and everything about the timeout message: the
    appliance answering slowly because it is mid-call to OpenAI is a fact worth stating,
    and stating it as "could not reach the appliance" is worse than saying nothing.
    """
    url = f"{base}/api/v1/setup{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url, data=data, method=method or ("POST" if data else "GET"))
    request.add_header(TOKEN_HEADER, token)
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(body).get("detail") or json.loads(body).get("error") or body
        except ValueError:
            detail = body
        if e.code == 410:
            raise AlreadySetUp(
                "This appliance is already set up.\n"
                f"Worlds: the console at {console_url()}   ·   Me: {base}"
                "\n(Forgot the password? --reset-password recreates the account and keeps all data.)"
            )
        if e.code == 401:
            raise TokenRejected(f"The setup token was not accepted.\n{detail}")
        raise SetupError(detail)
    except urllib.error.URLError as e:
        # A timeout arrives BOTH ways: wrapped in URLError when the connect phase runs
        # out, and bare from the socket when the read does. Unwrap it here or the read
        # case gets the honest message and the connect case gets the misleading one.
        if isinstance(e.reason, socket.timeout):
            raise _timed_out(base, timeout, waiting_on)
        raise Unreachable(
            f"Could not reach the appliance ({e.reason}).\n"
            f"Its API did not answer on {api_address(base)}.\n"
            f"Is it running? Try: {embabel_command('status')}"
        )
    except socket.timeout:
        raise _timed_out(base, timeout, waiting_on)
    except (http.client.HTTPException, ConnectionError, TimeoutError, OSError) as e:
        # DURING BOOT, "up" is a spectrum: Docker's port proxy accepts the TCP
        # connection before the app listens, then hangs up — RemoteDisconnected,
        # reset, or a stalled read, none of which urllib wraps in URLError. All of
        # them mean the same thing here: not reachable YET. The boot-wait loop in
        # discover_token owns retrying; crashing out of it turned a normal first
        # boot into a stack trace.
        raise Unreachable(
            f"Could not reach the appliance ({e.__class__.__name__}: {e}).\n"
            f"Its API did not answer on {api_address(base)}.\n"
            f"Is it running? Try: {embabel_command('status')}"
        )


def remember_client_state(base: str, token: str, notes: dict) -> None:
    """Leave a note for the next run of this installer, in the appliance's data volume.

    The server stores these and hands them back without reading them, which is
    what makes them ours: "we offered MCP and were told no" is a fact about a
    conversation the server was not part of, and it must survive `--resume`
    without the server growing an opinion about it.

    Best effort on purpose. A note that fails to save costs one repeated
    question; failing the install over it would cost the whole install.
    """
    try:
        call(base, "/state", token, notes, method="PUT")
    except SetupError:
        pass


def call_when_ready(base: str, token: str) -> dict:
    """The first real call, retried while the appliance finishes starting.

    The setup token is printed to the log EARLY — measured at ~10s before
    "Started ... in 24.977 seconds" — so finding it does not mean the HTTP surface
    is up. Calling straight through raised Unreachable, and because Unreachable is
    a SetupError that ended the whole run with "Could not reach the appliance
    (RemoteDisconnected)" moments after cheerfully announcing it had found the
    token. A first install failed on a race, and the message blamed the network.
    """
    deadline = time.monotonic() + BOOT_WAIT_SECONDS
    announced = False
    while True:
        try:
            answer = call(base, "", token)
            STATUS.stop()
            return answer
        except Unreachable:
            if time.monotonic() >= deadline:
                raise
            if not announced:
                STATUS.start("Waiting for the appliance to answer")
                announced = True
            time.sleep(2)


def complete_setup(base: str, token: str, container: str | None) -> dict:
    """Close setup, and survive the restart that closing it causes.

    THE ONE CALL WHERE A DROPPED CONNECTION IS THE EXPECTED OUTCOME. The server
    persists completion FIRST, then schedules its own `exit(0)` two seconds later so
    the model beans are rebuilt with the provider key — and it puts `"restarting": true`
    in the body precisely so a client reads a dead socket as a restart rather than a
    failure. This client used to read it as a failure: `call()` raised Unreachable, the
    caller re-raised, and an install that had ALREADY SUCCEEDED ended on "Could not
    reach the appliance (RemoteDisconnected)".

    It only ever bit people who supplied a provider key, which made it look like a key
    problem and is exactly backwards: a key is what makes the restart happen at all.
    Two seconds is enough on an idle laptop and is a race everywhere else.

    CHECKED, NEVER ASSUMED. A dropped connection could also be a crash, so this waits
    for the appliance to come back and then ASKS: setup answering 410 Gone is proof it
    completed, and anything else re-raises rather than declaring a success nobody
    verified.

    Returns /complete's body, or the part of it that can be reconstructed — the caller
    needs `signInAs`, which [account_username] still knows when the wire does not.
    """
    started_before = container_started_at(container) if container else ""
    try:
        return call(base, "/complete", token, {})
    except Unreachable as dropped:
        STATUS.start("Restarting to pick up your provider key")
        wait_until_serving(container, base, started_before)
        STATUS.stop()
        try:
            state = probe(base)
        except AlreadySetUp:
            return {"ok": True, "detail": "complete", RODE_OUT_RESTART: True,
                    "signInAs": account_username()}
        raise Unreachable(
            f"The appliance dropped the connection while finishing setup, came back, and "
            f"still reports setup as {state}.\n{dropped}"
        ) from dropped


def token_from_logs(container: str) -> str | None:
    run = _docker("logs", container)
    if run is None or run.returncode != 0:
        return None
    # BOTH streams: which one the app logs to is a packaging detail, and it must
    # never decide whether setup works.
    matches = TOKEN_PATTERN.findall(run.stdout + run.stderr)
    # Last match wins: a restarted container logs it again, and the newest is current.
    return matches[-1] if matches else None


def probe(base: str) -> str:
    """'pending' if the appliance is up and waiting for setup, 'unreachable' if not up.
    Raises AlreadySetUp — checked HERE, before any token hunting, so an appliance that
    is simply done says "sign in" instantly instead of sending anyone log-spelunking."""
    try:
        call(base, "", "probe")
        return "pending"
    except AlreadySetUp:
        raise
    except Unreachable:
        return "unreachable"
    except SetupError:
        return "pending"  # 401: up, and wants the real token


def discover_token(base: str, container: str | None, explicit: str | None) -> str:
    """The token is printed to the container log on every boot until setup completes.
    Read it from there so the usual case needs no copy-paste at all; wait out a
    container that is still booting; ask only when there is genuinely no way to know."""
    if explicit:
        return explicit

    deadline = time.monotonic() + BOOT_WAIT_SECONDS
    announced = False
    while True:
        if container:
            token = token_from_logs(container)
            if token:
                STATUS.stop(f"  {TICK} The appliance is up. Setup token read from its log.\n")
                return token
        state = probe(base)  # raises AlreadySetUp — the friendliest outcome
        if state == "pending":
            # The API answers but the current container log has no token — a remote
            # appliance with no local docker, or a log that lost it. Ask below.
            break
        # A death ends the wait immediately. Spinning out the full two minutes and
        # then blaming the token is the worst of both: slow AND wrong.
        died = boot_failure(container)
        if died:
            STATUS.stop()
            raise SetupError(
                f"The appliance failed to start.\n  {died}\n\n"
                f"  Full log:  docker logs {container}\n"
                f"  Then:      {embabel_command('doctor')}"
            )
        if container is None or time.monotonic() >= deadline:
            break
        if not announced:
            STATUS.start(boot_phase(container, base))
            announced = True
        else:
            STATUS.set(boot_phase(container, base))
        time.sleep(3)

    STATUS.stop()
    # NOBODY CAN ANSWER A QUESTION ABOUT A PROCESS THAT NEVER RAN. The token is printed
    # by the appliance at boot, so if nothing is serving there is no token anywhere to
    # find and the prompt below is a dead end — it asks for something that does not
    # exist, after the real error (a failed pull, a container that exited) has scrolled
    # away. That is what a person met when a `docker compose up` failed earlier in the
    # run: the reason was on screen, and the last thing they were shown was a prompt.
    #
    # Covers a MISSING container and a container that merely exists, which used to be
    # treated as the same as one that is up.
    if state == "unreachable":
        status = container_status(container) if container else ""
        if container is None:
            raise SetupError(
                f"No appliance is running: no mode container was found and {base} does not answer.\n"
                f"Start one first:  {embabel_command('up')}"
            )
        if status != "running":
            raise SetupError(
                f"The appliance is not running — {container} is '{status or 'not there'}', "
                f"and {base} does not answer.\n"
                f"  The setup token is printed by the appliance at boot, so there is none to find "
                f"until it starts.\n"
                f"  Why it did not start:  docker logs {container}\n"
                f"  Then:                  {embabel_command('doctor')}"
            )
    print("  Could not find the setup token automatically.")
    if container:
        print(f"  It is printed in the container log:  docker logs {container} 2>&1 | grep 'Setup token'")
    token = prompt("  Setup token: ").strip()
    if not token:
        raise SetupError("A setup token is required.")
    return token


# ── rendering ───────────────────────────────────────────────────────────────

def accept_trial_terms() -> None:
    """Show the free trial terms and take an answer, before anything is installed.

    The terms are accepted by installing, which makes WHEN this runs the whole
    point: after the images are pulled and the containers are up, a person has
    already done the accepting and is only being told about it. So this sits at
    the top of the run, before any state is written.

    Declining is a clean stop rather than an error. Nothing has been installed at
    this point, so there is nothing to undo and nothing to resume.
    """
    print("\n" + heading("Free trial terms"))
    say("trial-terms", doc_url=TRIAL_TERMS_DOC_URL)

    answer = prompt("\n  Accept and continue? [Y/n]: ").strip().lower()
    if answer not in ("", "y", "yes"):
        raise SetupError(
            "Setup stopped: the free trial terms were not accepted. Nothing has "
            "been installed.\n  The terms are at: " + TRIAL_TERMS_DOC_URL
        )


def disclose_usage_reporting(base: str) -> None:
    """Put the complete report shape in the first-run path, before setup closes.

    A README and a startup log are operator surfaces, but neither proves the person
    completing an interactive install saw the disclosure. This is deliberately a
    client-side rendering rather than another setup answer: current servers already
    expose the report, and setup.py has to remain compatible with those releases.

    Successful setup is the persistence boundary. Once /complete closes the setup API
    this function is never reached again; an interrupted setup shows it again on resume,
    which is preferable to remembering an acknowledgement for an incomplete install.
    """
    # NOTHING TO DISCLOSE WHEN NOTHING IS SENT. Reporting is off by default now,
    # and a page explaining a transmission that will not happen is worse than
    # silence: it teaches people the appliance phones home when it does not.
    if not phone_home_on():
        return

    print("\n" + heading("Usage reporting"))
    say("usage-reporting", endpoint=PHONE_HOME_ENDPOINT,
        doc_url=PHONE_HOME_DOC_URL, base=base)

    answer = prompt("\n  Continue setup? [Y/n]: ").strip().lower()
    if answer not in ("", "y", "yes"):
        raise SetupError(
            "Setup paused before completion. Nothing is lost — the appliance is "
            f"installed and running.\n  Continue with:  {resume_command()}"
        )


def yes_no_options(options: list) -> tuple[str, str] | None:
    """The (yes, no) pair when these options ARE a yes/no, else None.

    Matched on the values rather than on the field name, so any server-side
    two-option boolean gets the idiomatic prompt without this file learning its
    name. Order-insensitive: "no"/"yes" is the same question.
    """
    if len(options) != 2:
        return None
    lowered = [str(option).strip().lower() for option in options]
    if set(lowered) != {"yes", "no"}:
        return None
    return (options[lowered.index("yes")], options[lowered.index("no")])


def ask(field: dict) -> str:
    label = field.get("label") or field["name"]
    # A field may carry its own prose, printed HERE — beside the question it explains
    # rather than in the step header, where a reader takes it as being about whichever
    # prompt appears first. Same rule as a step's own copy: the words live in copy/.
    if field.get("copy"):
        print()
        print(dim(copy_text(field["copy"])))
    default = field.get("default")
    options = field.get("options") or []
    required = field.get("required", True)

    # This appliance's preference wins, but only over a choice that is actually
    # offered — a preference for something the server does not list would make
    # Enter do nothing, which is worse than any default.
    preferred = PREFERRED_DEFAULTS.get(field["name"])
    if preferred and (not options or preferred in options):
        default = preferred

    # A YES/NO IS A YES/NO. The server describes this field as a CHOICE of two,
    # which is the truth about its VALUES and not an instruction about how to
    # draw it — the installer owns the questions, the appliance owns the facts
    # (84cbe67). Rendered literally it became "1) yes / 2) no / Choose 1-2",
    # the only numbered menu in a wizard whose every other yes/no is [Y/n], and
    # then it was confirmed as well.
    if field["type"] == "CHOICE" and yes_no_options(options):
        yes, no = yes_no_options(options)
        default_yes = (default or yes).lower() != no
        while True:
            raw = prompt(f"\n  {label}? " + ("[Y/n]: " if default_yes else "[y/N]: ")).strip().lower()
            if not raw:
                return yes if default_yes else no
            if raw in ("y", "yes"):
                return yes
            if raw in ("n", "no"):
                return no
            print("  Answer y or n.")

    if field["type"] == "CHOICE" and options:
        print(f"\n  {label}:")
        for index, option in enumerate(options, 1):
            marker = "  (default)" if option == default else ""
            print(f"    {index}) {option}{marker}")
        while True:
            raw = prompt(f"  Choose 1-{len(options)}" + (f" [{default}]: " if default else ": ")).strip()
            if not raw and default:
                return default
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return options[int(raw) - 1]
            if raw in options:
                return raw
            print("  Not one of the options.")

    while True:
        if field["type"] == "SECRET":
            # Never echoed, and never lands in shell history.
            value = getpass.getpass(f"  {label}: ")
        else:
            suffix = f" [{default}]: " if default else ": "
            value = prompt(f"  {label}{suffix}").strip() or (default or "")
        if value or not required:
            return value
        print("  Required.")


def from_environment(step: dict) -> dict:
    """Answers this step can take from the shell rather than from the operator.

    A developer who already has OPENAI_API_KEY exported should not be made to paste it
    back in. Keyed off the field names rather than the step id, so it does nothing at
    all for a step that lacks them.

    Note this reads the environment of the shell running THIS script, which is not the
    container's. A key set only in `.env` reaches the appliance but not here, and is
    still asked for: the appliance reports a provider as connected once it holds the
    key itself, and an env var it was handed at boot is not that.
    """
    names = {field["name"] for field in step["fields"]}
    if not {"provider", "apiKey"} <= names:
        return {}

    available = {
        provider: os.environ[var]
        for provider, var in PROVIDER_ENV.items()
        if os.environ.get(var, "").strip()
    }
    if not available:
        return {}
    if len(available) == 1:
        provider = next(iter(available))
    else:
        # Both are set, and which one to connect first is a real choice — ask that much,
        # then still skip the key.
        print("\n  Found keys for both providers in your environment.")
        print(f"  {dim('Enter takes the default.')}")
        provider = ask(next(f for f in step["fields"] if f["name"] == "provider"))
        if provider not in available:
            return {}
    return {"provider": provider, "apiKey": available[provider]}


def confirm_answers(step: dict, answers: dict) -> str | None:
    """A last look before the step is permanent. Returns the field to redo, or None to send.

    The server accepts a step once and refuses to reopen setup (410, by design), so
    a username typed with a typo was permanent the instant Enter was pressed.

    WHAT `n` USED TO MEAN was "ask me everything again" — including the password,
    which was never in doubt, and which somebody re-typed because they wanted to
    change their display name. So the answer names the field: one number, one
    question, and the rest stand.

    A PASSWORD IS NOT SHOWN AT ALL, not even as "OK". There is nothing in it to
    check — it was typed blind, and it is the one field re-asking is cheap for —
    and a line carrying no information invites the reader to skim the ones that do.
    A KEY still shows its length, because a truncated paste is its whole failure mode.
    """
    shown = []
    for field in step["fields"]:
        name = field["name"]
        value = answers.get(name)
        if value in (None, "") or "password" in name.lower():
            continue
        label = field.get("label") or name
        if field.get("secret") or "key" in name.lower():
            shown.append((name, label, f"({len(value)} characters)"))
        else:
            shown.append((name, label, value))
    # NOTHING TO CONFIRM is not a question. One field the user just typed, or a
    # step whose only answer was a menu choice they made deliberately, does not
    # need a second opinion — and a confirmation nobody can fail is ceremony.
    if len(shown) < 2:
        return None
    print()
    for index, (_, label, rendered) in enumerate(shown, 1):
        print(f"    {index}) {label}: {rendered}")
    while True:
        raw = prompt("  Enter to continue, or a number to change: ").strip()
        if not raw:
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(shown):
            return shown[int(raw) - 1][0]
        print(f"  Enter, or 1-{len(shown)}.")


def provider_step(step: dict) -> bool:
    """Is this the model-provider step?

    Keyed off the server's own field names, like from_environment beside it, so a
    step added server-side is unaffected. NO env condition any more: the question
    is asked on every run, because which key this appliance connects with is the
    operator's to choose — silently taking whatever the shell exported made that
    choice for them.
    """
    return {"provider", "apiKey"} <= {field["name"] for field in step["fields"]}


def run_step(base: str, token: str, step: dict, use_environment: bool = True) -> dict:
    print("\n" + heading(step["title"]))
    # The step's own words, from copy/. `description` stays supported for a step
    # this installer does not know — the server can add one, and it arrives as a
    # string rather than as a file we ship.
    if step.get("copy"):
        say(step["copy"])
    elif step.get("description"):
        print(f"   {step['description']}")

    # A KEY IS NOT A TOLL GATE. Asking for a paid credential before anybody has
    # seen the product work is the commonest way a good tool loses an evaluator,
    # and it is unnecessary here: embeddings are local and always have been, so
    # documents, search, memory, realms, views and handlers all work with no key
    # at all. The step stays unsatisfied, truthfully — the server reports that as
    # a fact and closes setup anyway; re-running setup, or the Models tab, picks
    # it up later.
    # ONE PROMPT THAT TAKES THE KEY, not permission to ask for one.
    #
    # It used to ask "Connect a provider now? [Y/n]" two lines under a description
    # that says "Paste a key" — so a key was pasted, echoed in clear text into
    # scrollback, and then read as YES by `answer in ("n","no")`, under which
    # everything that is not "no" is yes. A prompt that CAN receive a secret must
    # hide its input, and a prompt that wants a thing should take the thing.
    #
    # Asked on EVERY run, including when a key is already exported: whose key this
    # appliance connects with is the operator's choice, and taking the shell's
    # silently made it ours.
    forced: dict | None = None
    if provider_step(step):
        say("provider-choice")
        # THE ENVIRONMENT READ DIRECTLY, not through from_environment — which asks
        # its OWN menu when both keys are exported, and a menu followed by this
        # prompt is two questions where the point was to have one. The appliance's
        # preferred provider decides, and the other is named in passing so nobody
        # wonders whether it was seen.
        exported = {p: os.environ[var] for p, var in PROVIDER_ENV.items()
                    if os.environ.get(var, "").strip()} if use_environment else {}
        chosen = (PREFERRED_DEFAULTS.get("provider") if PREFERRED_DEFAULTS.get("provider") in exported
                  else next(iter(exported), None))
        environment = {"provider": chosen, "apiKey": exported[chosen]} if chosen else {}
        source = PROVIDER_ENV[chosen] if chosen else None
        others = [PROVIDER_ENV[p] for p in exported if p != chosen]
        if others:
            print(f"\n  {MIDDOT} " + dim(f"{' and '.join(others)} also exported — paste a key here to use one instead."))
        while True:
            asked = getpass.getpass(
                f"\n  Enter to use {source} from your environment, or paste another key, "
                "or 'n' to skip: " if environment
                else "\n  Paste your model-provider key, or 'n' to skip: ").strip()
            if asked.lower() in ("n", "no"):
                print(f"\n  {TICK} Starting without a provider key.")
                return {}
            if asked:
                provider = provider_from_key(asked)
                if provider is None:
                    print("\n  This key does not identify its provider.")
                    provider = ask(next(f for f in step["fields"] if f["name"] == "provider"))
                forced = {"apiKey": asked, "provider": provider}
                break
            if environment:
                print(f"  Using {source}.")
                forced = environment
                break
            print("  A key, or 'n' to skip.")

    while True:
        # Only on the FIRST attempt. A retry means the server rejected these answers, and
        # re-offering the same environment value would loop forever on a stale key.
        prefilled = forced if forced is not None else (
            from_environment(step) if use_environment else {})
        use_environment = False
        # Cleared after one attempt: a REJECTED key must be asked for again, not
        # re-posted forever — the same rule the environment value already follows.
        forced = None
        # `in`, not `or`: a value the caller supplied stands even when it is empty,
        # and a field it did not supply is the only one worth asking about.
        answers = {
            field["name"]: prefilled[field["name"]] if field["name"] in prefilled else ask(field)
            for field in step["fields"]
        }
        if {"username", "password"} <= set(answers):
            # Held for the length of this process only, and used for exactly one
            # thing: the documentation upload after setup. The alternative was to
            # depend on the MCP step's minted token, which is not always minted —
            # so seeding silently did nothing, which is how it shipped broken.
            remember_account(answers["username"], answers["password"])
        if {"provider", "apiKey"} <= set(answers) and answers.get("apiKey"):
            # Persisted only once the SERVER has accepted it, further down — a
            # rejected key written to .env would be handed to every later boot.
            pending_key = (answers.get("provider") or "openai", answers["apiKey"])
        else:
            pending_key = None

        # A LAST LOOK BEFORE IT IS PERMANENT. The server accepts a step once and
        # refuses to reopen setup afterwards (410, by design), so a username typed
        # with a typo was permanent the instant Enter was pressed — and the only way
        # back was --reset-password, which is not a thing anybody guesses while
        # staring at their own misspelt name. Only asked where re-entry actually
        # helps: a value the SERVER rejects already loops in place below, and a
        # value only the user can judge is the one nothing else can catch.
        # ONE FIELD, not the step. `continue` here re-asked everything, which is
        # what made a display-name typo cost a re-typed password.
        while not prefilled:
            redo = confirm_answers(step, answers)
            if redo is None:
                break
            field = next(f for f in step["fields"] if f["name"] == redo)
            answers[redo] = ask(field)
            if {"username", "password"} <= set(answers):
                remember_account(answers["username"], answers["password"])

        # DECLINING IS NOT A SERVER CALL. There is no "no" endpoint any more: the
        # appliance mints a token when asked and knows nothing about having been
        # offered. So we record our own note and never post.
        if step["id"] == wizard.MCP:
            if (answers.get("enable") or "yes").strip().lower() not in ("y", "yes"):
                remember_client_state(base, token, {wizard.MCP_DECLINED: "yes"})
                print(f"\n  {TICK} MCP left off. "
                      + dim("Re-run setup if you want it later."))
                return {}
            answers = {}

        print("\n  Working…", end=" ", flush=True)
        try:
            # A step declares its own clock only when its clock is not ours: `timeout`
            # names a budget rather than carrying a number, so the durations stay in
            # one place here beside the reason for each of them.
            waits = step.get("waitsOnProvider")
            result = call(base, f"/{step['id']}", token, answers,
                          timeout=PROVIDER_TIMEOUT_SECONDS if waits else CALL_TIMEOUT_SECONDS,
                          waiting_on=provider_of(answers.get("apiKey")) if waits else None)
        except (AlreadySetUp, Unreachable, TokenRejected):
            # The appliance or the session is the problem, not the answer — no amount
            # of retyping helps.
            print("\n")
            raise
        except SetupError as e:
            # A REJECTED ANSWER: server-side validation (password rules, malformed key).
            # Retry IN PLACE. Ending the whole wizard — and making someone re-run it and
            # re-answer the steps they already got right — because a password was four
            # characters long is a hostile way to meet a new user.
            print(f"\n  {e}\n  Let's try that step again.")
            continue
        if result.get("ok"):
            if pending_key:
                remember_provider_key(*pending_key)
            print(result.get("detail", "done"))
            models = result.get("models")
            if models:
                # Proof the key really works: these came back from the provider just now.
                shown = ", ".join(models[:6])
                more = f" (+{len(models) - 6} more)" if len(models) > 6 else ""
                print(f"  {len(models)} models available: {shown}{more}")
            return result
        # A rejected answer is worth retrying in place — usually a typo'd key.
        print(f"\n  {result.get('detail', 'That did not work.')}\n  Let's try that step again.")
        if prefilled:
            print("  (that key came from your environment — you'll be asked for one now)")
