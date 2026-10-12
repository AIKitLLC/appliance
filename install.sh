#!/bin/sh
# Embabel Worlds — one-command install.
#
#   curl -fsSL https://raw.githubusercontent.com/embabel-worlds/appliance/main/install.sh | sh
#
# This script is short and boring ON PURPOSE. You are being asked to pipe a
# remote script into a shell, which is a thing you should be suspicious of, so
# everything it does should fit on one screen and read plainly:
#
#   1. check Docker is installed and running
#   2. download this repo into ~/embabel/worlds
#   3. hand off to ./me.py, which owns the actual setup
#
# It installs nothing globally, needs no root, and writes only inside that one
# directory. To undo it: `docker compose down -v` in there, then delete it.
#
# Environment:
#   EMBABEL_HOME   where to install         (default: ~/embabel/worlds)
#   EMBABEL_REF    branch or tag to fetch   (default: main)
#   EMBABEL_MODE   worlds | me              (default: worlds)
#   EMBABEL_REPO   org/repo of the source   (default: embabel-worlds/appliance)
#   EMBABEL_TOKEN  GitHub token, private forks only

set -eu

REPO="${EMBABEL_REPO:-embabel-worlds/appliance}"
# TODO: default to the latest RELEASE TAG once the appliance cuts them; a branch
# means you get whatever landed this morning, which is not what an installer
# should hand a new user.
REF="${EMBABEL_REF:-main}"
# WORLDS IS THE DOOR. This script is what worlds.embabel.com hands somebody who
# has installed nothing, so the product it opens must be the one they just read
# about — Me defaulted here for historical reasons and greeted every visitor by
# naming the other product. `EMBABEL_MODE=me` still installs the assistant.
MODE="${EMBABEL_MODE:-${EMBABEL_DOOR:-worlds}}"   # EMBABEL_DOOR: the old name, still honoured
# ~/embabel/worlds — vendor, then product, then whatever the product keeps
# (realms/ lands at ~/embabel/worlds/realms). ~/embabel-me was wrong twice over:
# it named a vendor and a door with one hyphen, and it named the door you happened
# to arrive through rather than the thing installed.
#
# ONE DIRECTORY FOR BOTH DOORS, not a sibling ~/embabel/me. The compose project
# name is fixed at embabel-appliance and the two modes share one graph and one
# volume, so a second checkout would be a second .env quietly steering the same
# containers. Me opens this install; it does not get its own.
#
# An existing install is used where it is found, whichever name it has: a rename
# is not worth stranding somebody's data over.
DEFAULT_HOME="$HOME/embabel/worlds"
if [ -z "${EMBABEL_HOME:-}" ]; then
  for candidate in "$HOME/embabel-worlds" "$HOME/embabel-me"; do
    if [ -f "$candidate/setup.py" ]; then
      DEFAULT_HOME="$candidate"
      break
    fi
  done
fi
HOME_DIR="${EMBABEL_HOME:-$DEFAULT_HOME}"

# Colour, in POSIX sh and with the same restraint as the rest of the appliance:
# the sixteen basic codes, one accent, and OFF unless the terminal is really a
# terminal. NO_COLOR is honoured (no-color.org); `curl ... | sh` still gets it,
# because it is stdIN that is the pipe there, not stdout.
#
# The `-t 1` test is the whole guard. Without it, redirecting this script's
# output to a file writes escape codes into it, and the log somebody attaches to
# a bug report is unreadable.
# FORCE_COLOR matches setup.py, and earns its place here: the only way to see
# this script's output without a terminal — a preview, a CI log, a test — is to
# ask for it, and a preview that silently renders plain is a preview of nothing.
if [ -n "${NO_COLOR:-}" ]; then
  _embabel_colour=no
elif [ -n "${FORCE_COLOR:-}${CLICOLOR_FORCE:-}" ]; then
  _embabel_colour=yes
elif [ -t 1 ] && [ "${TERM:-dumb}" != "dumb" ]; then
  _embabel_colour=yes
else
  _embabel_colour=no
fi
if [ "$_embabel_colour" = yes ]; then
  C_RESET=$(printf '\033[0m'); C_BOLD=$(printf '\033[1m'); C_DIM=$(printf '\033[2m')
  C_CYAN=$(printf '\033[36m'); C_GREEN=$(printf '\033[32m'); C_YELLOW=$(printf '\033[33m')
else
  C_RESET=; C_BOLD=; C_DIM=; C_CYAN=; C_GREEN=; C_YELLOW=
fi

say()  { printf '  %s\n' "$*"; }
step() { printf '  %s%s%s %s\n' "$C_CYAN" "::" "$C_RESET" "$*"; }
ok()   { printf '  %s%s%s %s\n' "$C_GREEN" "OK" "$C_RESET" "$*"; }
note() { printf '  %s%s%s\n' "$C_DIM" "$*" "$C_RESET"; }
die()  { printf '\n  %s%s%s %s\n\n' "$C_YELLOW" "!!" "$C_RESET" "$*" >&2; exit 1; }

# The door you are actually installing, described in the SITE'S words —
# worlds-site/src/pages/index.astro. Somebody arrives here straight off that
# page, and an installer that pitches the product differently from the page
# that sent them reads as a different product.
if [ "$MODE" = "worlds" ]; then
  # The banner, when the terminal is wide enough for it — 100 columns of ASCII
  # art wrapped at 80 is not a logo, it is a mess. This is embabel-agent's
  # banner.txt, the same mark the server prints on boot; copy/banner.txt in the
  # appliance holds the original and scripts/check-copy.py fails if they drift.
  # It is duplicated here for one reason: this script runs BEFORE there is a
  # checkout to read it from.
  if [ "${COLUMNS:-$(tput cols 2>/dev/null || echo 80)}" -ge 102 ] 2>/dev/null; then
    printf '%s\n' "$C_CYAN"
    cat <<'ART'
   ___  ___    _______ .___  ___. .______        ___      .______    _______  __         ___  ___
  /  / /  /   |   ____||   \/   | |   _  \      /   \     |   _  \  |   ____||  |        \  \ \  \
 /  / /  /    |  |__   |  \  /  | |  |_)  |    /  ^  \    |  |_)  | |  |__   |  |         \  \ \  \
<  < <  <     |   __|  |  |\/|  | |   _  <    /  /_\  \   |   _  <  |   __|  |  |          >  > >  >
 \  \ \  \    |  |____ |  |  |  | |  |_)  |  /  _____  \  |  |_)  | |  |____ |  `----.    /  / /  /
  \__\ \__\   |_______||__|  |__| |______/  /__/     \__\ |______/  |_______||_______|   /__/ /__/
ART
    printf '%s' "$C_RESET"
  else
    printf '\n  %s<<  E M B A B E L  >>%s\n' "$C_BOLD$C_CYAN" "$C_RESET"
  fi
  printf '\n  %sEmbabel Worlds%s %s— the world your AI acts in%s\n' \
    "$C_BOLD" "$C_RESET" "$C_DIM" "$C_RESET"
  printf '  %sA governed, living knowledge graph of your business, derived from the\n' "$C_DIM"
  printf '  systems you already run and owned by you. Insight across the whole\n'
  printf '  business, in days.%s\n\n' "$C_RESET"
else
  printf '\n  %sEmbabel Me%s %s— your own assistant, on your own machine%s\n\n' \
    "$C_BOLD" "$C_RESET" "$C_DIM" "$C_RESET"
fi

# --- 1. Docker ------------------------------------------------------------
# The one prerequisite we cannot install for you, and the one worth failing
# early and clearly on: everything below is pointless without it.
#
# WITH THE REASON, NOT JUST THE VERDICT. "Docker is required" is true and
# useless: it reads as one more dependency somebody's tool wants, at the exact
# moment a stranger decides whether this product is worth the trouble. Docker is
# not incidental here — it is how the appliance stays local, arrives whole, and
# leaves cleanly — so the refusal says that, and the person choosing has what
# they need to choose.
#
# THE COPY IS DUPLICATED FROM copy/, like the banner above and for the same
# reason: this script runs BEFORE there is a checkout to read it from.
# copy/docker-required.txt is canonical, scripts/check-copy.py compares the two
# byte for byte, and prose edited in one place fails the build if it is not
# carried to the other.
docker_required() {
  printf '\n'
  # Indented HERE, not in the file: everything this installer prints sits two
  # spaces in, and copy/ holds words with no layout — the same division of
  # labour as wrap_copy() on the Python side. Blank lines are left blank rather
  # than becoming two spaces of trailing whitespace.
  cat <<'DOCKER_REQUIRED' | sed 's/^./  &/'
Embabel needs Docker, and it is the only thing you have to install yourself.

Embabel is not one program. It is a knowledge graph, a server, a console, a
document converter, a metrics stack and a sandbox that runs code your agents
write — six or seven pieces that have to find each other, come up in the right
order and agree on their versions. Docker is how they arrive together, already
wired, in about a command.

It is also what keeps your world YOURS. Every one of those pieces runs on this
machine: your documents are converted here, turned into vectors here by a model
that runs here, and stored in a graph here. Nothing is uploaded to us, and
there is no account to make. The only traffic that leaves is your model
provider's, when you ask a question and your own key pays for the answer.

And it is what makes this reversible. There is no installer scattering files
across your system, no Java, no Node, no database to configure and no service
left running when you are done. `embabel down` stops it; `embabel uninstall`
removes it. What is left behind is the directory you chose.

Install Docker Desktop, start it, and run this again:

    https://docs.docker.com/get-started/get-docker/
DOCKER_REQUIRED
}

rancher_containerd() {
  printf '\n'
  cat <<'RANCHER_CONTAINERD' | sed 's/^./  &/'
Rancher Desktop is installed here and set to containerd, which ships nerdctl and
no `docker` command at all. That is a setting, not a missing product, and it is
the setting Rancher Desktop installs with.

Switch it to the Docker engine:

    rdctl set --container-engine docker

or open Preferences → Container Engine and choose dockerd (moby). Then run this
again.

Nothing is deleted by switching. The two engines keep separate image stores, so
images you pulled with nerdctl go quiet until you switch back — they are still on
disk, and nothing outside those stores is touched. Rancher Desktop's own words
for it are that they are "not available on the container runtime being switched
to", which is invisibility, not loss.

Embabel needs the Docker API itself, not only a command that looks like Docker.
The appliance starts the sandbox your agents' code runs in as a sibling
container, through the docker socket, and containerd serves no such socket. On
containerd this would install, start, and then fail at the exact moment it first
ran code — which is a worse outcome than stopping here.
RANCHER_CONTAINERD
}

if ! command -v docker >/dev/null 2>&1; then
  # RANCHER DESKTOP DEFAULTS TO containerd, and the person in front of this does
  # not need persuading that containers are worth it — they chose a runtime and
  # missed a radio button. The essay below is written for somebody with nothing
  # installed; printing it here reads as "go and get the product you
  # deliberately did not get", and buries the one command that fixes this.
  #
  # BOTH tools, not either: nerdctl alone is also plain containerd, where every
  # sentence about Preferences is wrong. rdctl beside it is Rancher Desktop.
  if command -v rdctl >/dev/null 2>&1 && command -v nerdctl >/dev/null 2>&1; then
    rancher_containerd
    die "No 'docker' on your PATH — Rancher Desktop is set to containerd."
  fi
  docker_required
  # die() adds the one line this case needs on top of the explanation: what
  # exactly was looked for and not found.
  die "No 'docker' on your PATH."
fi
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required — update Docker Desktop, or install the compose plugin."
# Installed but not started is a DIFFERENT problem from missing, and the long
# explanation above would be condescending here: this person already chose
# Docker. One line, one action.
docker info >/dev/null 2>&1 || die "Docker is installed but not running. Start Docker Desktop, then run this again."

# MODEL RUNNER IS AN OPTION, NOT A PREREQUISITE, and this block said the
# opposite for as long as it existed: "Embabel needs it to start" was true of an
# older appliance that built its embedding bean at boot and refused to come up
# without one. An appliance now ships with no embedding model and says so, and
# `embeddings use hosted` needs no download at all.
#
# It misfires worst on the people who cannot act on it. Model Runner is a Docker
# Desktop feature, so on Rancher Desktop, Colima, or an Intel Mac there is
# nothing to enable — and a missing prerequisite announced at the top of an
# install that is about to work perfectly well reads as a broken install.
# Dimmed rather than yellow for the same reason: this is a fact about the
# machine, not a fault in it.
#
# Same duplication rule as above — copy/docker-model-runner.txt is canonical.
if ! docker model status >/dev/null 2>&1; then
  printf '  %s..%s\n' "$C_DIM" "$C_RESET"
  cat <<'DOCKER_MODEL_RUNNER' | sed 's/^./  &/'
No Docker Model Runner here, which decides one thing only: if you turn document
search on, the embedding model is a hosted one, set up from the key you give the
appliance and downloading nothing.

    embabel embeddings use hosted

(On Docker Desktop you can enable Model Runner instead — Settings → AI — and run
that model on this machine with `embabel embeddings use local`.)

Nothing is waiting on this. An appliance ships with no embedding model at all,
and documents are the only feature that uses one.
DOCKER_MODEL_RUNNER
  echo
fi

# --- 2. Download ----------------------------------------------------------
if [ -e "$HOME_DIR" ] && [ ! -f "$HOME_DIR/setup.py" ] && [ -n "$(ls -A "$HOME_DIR" 2>/dev/null || true)" ]; then
  die "$HOME_DIR exists and is not an Embabel install. Move it, or set EMBABEL_HOME."
fi

if [ -f "$HOME_DIR/setup.py" ]; then
  step "Updating ${HOME_DIR} $(printf '%s' "$C_DIM")(your .env and data are untouched)$(printf '%s' "$C_RESET")…"
else
  step "Installing into ${HOME_DIR}…"
fi

mkdir -p "$HOME_DIR"

# A tarball, not a file list: nothing here has to be kept in step with the repo
# layout, and the API form takes a branch, a tag or a commit alike.
#
# Downloaded to a FILE first, not piped straight into tar. In a pipeline the
# exit status is tar's, so a failed download reaches tar as an empty stream and
# the script cheerfully reports success over an empty directory — which is
# exactly what the first version of this script did.
TARBALL="$(mktemp)"
trap 'rm -f "$TARBALL"' EXIT INT TERM

# The repo is PUBLIC, so no credential is needed and none is asked for. EMBABEL_TOKEN
# stays for the one case that still needs it: pointing EMBABEL_REPO at a private fork
# or an internal mirror. An empty token is not sent, so the ordinary path is anonymous.
AUTH=""
[ -n "${EMBABEL_TOKEN:-}" ] && AUTH="Authorization: Bearer ${EMBABEL_TOKEN}"

# One attempt. No -f, because we want the STATUS rather than a bare non-zero:
# "it failed" and "it failed with a 404" call for different advice.
attempt() {
  if [ -n "$AUTH" ]; then
    curl -sSL -H "$AUTH" -o "$TARBALL" -w '%{http_code}' "$1" 2>/dev/null
  else
    curl -sSL -o "$TARBALL" -w '%{http_code}' "$1" 2>/dev/null
  fi
}

# TWO SOURCES FOR THE SAME BYTES, and the order matters. api.github.com/tarball
# only redirects to codeload anyway, so it is a hop that can fail on its own —
# and does: measured from Australia, the API answered 504 in 40ms, three times
# running, while codeload served the identical tarball with a 200. Going
# straight to codeload removes a dependency that buys nothing.
#
# The API form stays as the fallback, and goes FIRST when a token is set: it is
# the documented way to reach a private repo, which is the only reason
# EMBABEL_TOKEN exists.
CODELOAD="https://codeload.github.com/$REPO/tar.gz/$REF"
API="https://api.github.com/repos/$REPO/tarball/$REF"
if [ -n "$AUTH" ]; then SOURCES="$API $CODELOAD"; else SOURCES="$CODELOAD $API"; fi

CODE=""
for url in $SOURCES; do
  tries=0
  while [ "$tries" -lt 3 ]; do
    # `|| CODE=000`, and it is load-bearing. Under `set -eu` the exit status of
    # an assignment IS the substitution's status, so a curl that fails MID-BODY —
    # a stalled transfer, a dropped connection — killed the script right here,
    # with a bare "exit 56" and none of the diagnosis below. The retry loop never
    # ran either. A transport failure is what 000 already means, and its message
    # ("Could not reach GitHub. Check your connection or proxy") is the one to
    # print. Observed live: codeload answered 200 and then stalled mid-tarball.
    CODE="$(attempt "$url")" || CODE="000"
    [ "$CODE" = "200" ] && break 2
    # A missing ref is not a blip; retrying it just makes the person wait.
    [ "$CODE" = "404" ] && break
    tries=$((tries + 1))
    [ "$tries" -lt 3 ] && sleep 2
  done
done

if [ "$CODE" != "200" ]; then
  case "$CODE" in
    404) die "No '$REF' in $REPO — check EMBABEL_REF, or EMBABEL_REPO if you are using a fork." ;;
    401|403) die "GitHub refused the download ($CODE). Check EMBABEL_TOKEN if $REPO is private, or wait out a rate limit." ;;
    000) die "Could not reach GitHub. Check your connection or proxy." ;;
    *) die "GitHub could not serve the download ($CODE) — its problem, not yours. Try again in a minute." ;;
  esac
fi

tar xzf "$TARBALL" -C "$HOME_DIR" --strip-components=1 || die "Could not unpack the download."

# Post-condition, because a download that returns the wrong thing is worse than
# one that fails: prove the install is actually here before promising setup.
[ -f "$HOME_DIR/setup.py" ] || die "The download did not contain an Embabel appliance. Nothing was installed."

chmod +x "$HOME_DIR/me.py" "$HOME_DIR/worlds.py" "$HOME_DIR/setup.py" "$HOME_DIR/embabel" 2>/dev/null || true

# --- 3. Hand off ----------------------------------------------------------
# setup.py owns the real flow — starting the mode, streaming the first boot,
# the account and model-provider key, and the offer to open the Me app. There
# is deliberately no second implementation of any of that here.
# THE PYTHON THAT CAN RUN THIS. The setup code runs on the stock Mac 3.9 (deferred
# annotations everywhere), but prefer a NEWER interpreter when one exists, and prove
# whatever is chosen is at least 3.9 rather than dying in a traceback later.
find_python() {
  for cand in python3.13 python3.12 python3.11 python3.10 python3; do
    command -v "$cand" >/dev/null 2>&1 || continue
    if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
      command -v "$cand"
      return 0
    fi
  done
  return 1
}
PY="$(find_python)" || die "Embabel needs Python 3.9 or newer — found $(python3 -V 2>/dev/null || echo 'no python3').
  macOS:  brew install python@3.12   (then rerun this installer)
  Linux:  apt install python3.10  ·  dnf install python3.12"

# ONE WRITER, reached from both paths. Setup owns the launcher because it also
# has to restore it after uninstall; keeping a heredoc here made the curl entry
# point work while the documented ./worlds.py entry point quietly did not.
(cd "$HOME_DIR" && "$PY" -c 'from embabel_setup.lifecycle import write_cli_shim; write_cli_shim()' < /dev/null) ||
  say "Could not put the 'embabel' command on your PATH — run $HOME_DIR/embabel by path."

ok "Done. Starting setup — after this, use the 'embabel' command."
echo
cd "$HOME_DIR"

# Arguments ride through to the mode script untouched, which is what makes a
# preconfigured install a one-liner:
#   curl -fsSL https://raw.githubusercontent.com/embabel-worlds/appliance/main/install.sh | sh -s -- --world legal-world
# THE WIZARD NEEDS A TERMINAL, and `curl … | sh` has already spent stdin: the
# shell read THIS SCRIPT from that pipe, so by the time setup asks for a username
# stdin is at EOF. It died in a Python traceback on the very first question, so
# every piped install failed in the same place.
#
# Redirect on the hand-off itself rather than `exec < /dev/tty` earlier: this
# script is still being read from that pipe, and a shell that loses its own stdin
# mid-file loses the rest of its script. Only the command that needs the terminal
# gets it.
#
# No terminal (CI, a container, nohup) means no redirect, and setup.py says which
# command to run by hand instead of crashing.
# OPENING it, not `-r`. The device node is readable by permission even when the
# process has no CONTROLLING terminal — a pty without a session leader, a CI
# runner, some containers — so `-r` passed and the redirect then died with
# "/dev/tty: Device not configured", which is the raw failure this branch exists
# to prevent. Actually opening it is the only test that means anything.
#
# IN A SUBSHELL, and that is the whole difference between falling through and
# dying here. `:` is a POSIX SPECIAL built-in, and a redirection error on a
# special built-in makes a non-interactive shell exit — so on the very machines
# this branch exists for (CI, a container without -t, nohup) `{ : < /dev/tty; }`
# did not return false, it ended the install. The last thing printed was "Done.
# Starting setup", and setup never started: exit 1, no message, nothing to go on.
# A subshell's exit is the subshell's, which is what makes the test a test.
if ( : < /dev/tty ) 2>/dev/null; then
  if [ "$MODE" = "worlds" ]; then
    exec "$PY" ./worlds.py "$@" < /dev/tty
  else
    exec "$PY" ./me.py "$@" < /dev/tty
  fi
fi

if [ "$MODE" = "worlds" ]; then
  exec "$PY" ./worlds.py "$@"
else
  exec "$PY" ./me.py "$@"
fi
