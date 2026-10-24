#!/bin/sh
# Embabel — what is wrong, in one command, with nothing installed.
#
#   curl -fsSL https://raw.githubusercontent.com/embabel-worlds/appliance/main/doctor.sh | sh
#
# `embabel doctor` is the better tool and this is not trying to replace it. But it
# needs the `embabel` command, which needs a checkout, which needs the install to have
# got that far — and the failures that hurt most are the ones where it did not. This
# script needs nothing but a shell and, ideally, docker. Where docker is missing it says
# so and stops, because everything below that point would be a guess.
#
# READ-ONLY, ALWAYS. It starts nothing, stops nothing, deletes nothing, and never prints
# a password, a key or a token. Log lines are filtered to errors and warnings, and the
# report says so, because a log can carry the contents of somebody's world.
#
# Written for somebody who does not want to become a Docker expert today. Every finding
# says what to do about it, and the end says where to send the output if none of it
# helped.
set -u

# Colour only when a terminal is really there, and NO_COLOR honoured. Piping this into a
# file or an issue should give plain text.
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  R=$(printf '\033[0m'); B=$(printf '\033[1m')
  RED=$(printf '\033[31m'); YEL=$(printf '\033[33m'); GRN=$(printf '\033[32m'); DIM=$(printf '\033[2m')
else
  R=''; B=''; RED=''; YEL=''; GRN=''; DIM=''
fi

PROBLEMS=0
ADVICE_FILE=$(mktemp 2>/dev/null || echo "/tmp/embabel-doctor-advice.$$")
trap 'rm -f "$ADVICE_FILE"' EXIT INT TERM

say()   { printf '%s\n' "$*"; }
head2() { printf '\n%s%s%s\n' "$B" "$*" "$R"; }
ok()    { printf '  %s✓%s  %s\n' "$GRN" "$R" "$*"; }
note()  { printf '  %s·%s  %s%s%s\n' "$DIM" "$R" "$DIM" "$*" "$R"; }
# A finding is a problem plus what to do about it. The advice is collected as well as
# printed, so the end of the report can repeat the actions in order without the reader
# having to scroll back through everything that was fine.
bad()   { PROBLEMS=$((PROBLEMS + 1)); printf '  %s✗%s  %s\n' "$RED" "$R" "$1"
          if [ $# -gt 1 ] && [ -n "$2" ]; then
            printf '     %s%s%s\n' "$DIM" "$2" "$R"
            # The PROBLEM and the action together: a list of bare instructions at the end,
            # detached from what each one is for, is a list nobody can act on out of order.
            printf '%s\n     %s\n' "$1" "$2" >> "$ADVICE_FILE"
          fi
          return 0; }
warn()  { printf '  %s!%s  %s\n' "$YEL" "$R" "$1"
          [ $# -gt 1 ] && printf '     %s%s%s\n' "$DIM" "$2" "$R"; return 0; }

say ""
say "  ${B}Embabel — checking this machine${R}"
say "  ${DIM}Nothing here changes anything. Copy the whole output if you need to ask for help.${R}"

# ── 1. docker ────────────────────────────────────────────────────────────────
# Everything else depends on this, so a failure here stops the script rather than
# producing twenty more failures that all mean "docker is not running".
head2 "Docker"
if ! command -v docker >/dev/null 2>&1; then
  # Rancher Desktop installs with containerd selected, which ships nerdctl and no
  # docker command. Telling that person to install Docker Desktop sends them for a
  # second container runtime when the one they have is a radio button away. Both
  # tools together are the tell — nerdctl alone is plain containerd.
  if command -v rdctl >/dev/null 2>&1 && command -v nerdctl >/dev/null 2>&1; then
    bad "Rancher Desktop is set to containerd, which has no docker command." \
        "Run:  rdctl set --container-engine docker   — or Preferences → Container Engine → dockerd (moby). Nothing is deleted: the two engines keep separate image stores, so anything you pulled with nerdctl is invisible until you switch back, not gone."
  else
    bad "Docker is not installed." "Install Docker Desktop: https://docs.docker.com/get-started/get-docker/"
  fi
  say ""
  say "  ${B}Nothing else can be checked without it.${R} Fix that, start it, and run this again."
  exit 1
fi
ok "Docker is installed."

if ! docker info >/dev/null 2>&1; then
  bad "Docker is installed but not running." "Start Docker Desktop and wait for its whale icon to stop animating, then run this again."
  say ""
  say "  ${B}Nothing else can be checked until it starts.${R}"
  exit 1
fi
ok "Docker is running."

docker compose version >/dev/null 2>&1 \
  && ok "Docker Compose is available." \
  || bad "Docker Compose is missing." "Update Docker Desktop — Compose ships with it."

# THE CREDENTIAL HELPER, which fails in a way that looks nothing like its cause: the
# pull dies with "error getting credentials" before a single byte is fetched, and the
# appliance's images are public and need no credentials at all.
CONFIG="${DOCKER_CONFIG:-$HOME/.docker}/config.json"
if [ -f "$CONFIG" ]; then
  HELPER=$(sed -n 's/.*"credsStore"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$CONFIG" | head -1)
  if [ -n "$HELPER" ]; then
    if command -v "docker-credential-$HELPER" >/dev/null 2>&1; then
      ok "Docker's credential helper ($HELPER) is where it should be."
    else
      bad "Docker's credential helper is missing, so every download fails before it starts." \
          "Run:  export PATH=\"\$PATH:/Applications/Docker.app/Contents/Resources/bin\"   then try again. If that does not help, delete the line containing \"credsStore\" from $CONFIG — the Embabel images are public and need no sign-in."
    fi
  fi
fi

# ── 2. room ──────────────────────────────────────────────────────────────────
# What DOCKER has, not what the machine has: on macOS and Windows the appliance lives in
# docker's own VM and can never exceed its allocation.
head2 "Room to run"
MEM=$(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0)
case "$MEM" in ''|*[!0-9]*) MEM=0 ;; esac
if [ "$MEM" -gt 0 ]; then
  MEMGB=$((MEM / 1024 / 1024 / 1024))
  if [ "$MEMGB" -lt 5 ]; then
    warn "Docker has about ${MEMGB} GB of memory. Embabel needs about 5 GB to run comfortably." \
         "Docker Desktop → Settings → Resources → Memory. If you cannot spare more and your .env file says GRAPH_TYPE=NEO4J, adding the line NEO4J_HEAP=1G to it makes the database use half as much."
  else
    ok "Docker has about ${MEMGB} GB of memory (about 5 GB is needed)."
  fi
fi
FREE=$(df -Pg "$HOME" 2>/dev/null | awk 'NR==2 {print $4}')
case "${FREE:-}" in ''|*[!0-9]*) FREE='' ;; esac
if [ -n "$FREE" ]; then
  if [ "$FREE" -lt 4 ]; then
    bad "Only ${FREE} GB free on this disk. The first download alone is about 4 GB." "Free up space and run the install again."
  elif [ "$FREE" -lt 14 ]; then
    warn "${FREE} GB free. Enough to start — about 14 GB arrives in total, the rest downloading quietly afterwards." ""
  else
    ok "${FREE} GB free on disk (about 14 GB is needed in total)."
  fi
fi

# ── 3. the appliance itself ──────────────────────────────────────────────────
head2 "The appliance"
# Which appliance: the default one unless EMBABEL_INSTANCE names another. Containers
# are found by their compose project label, because their names depend on it.
INSTANCE="${EMBABEL_INSTANCE:-appliance}"
PROJECT="embabel-$INSTANCE"
CONTAINERS=$(docker ps -a --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Names}}' 2>/dev/null | sort)
if [ -z "$CONTAINERS" ]; then
  note "No Embabel containers exist on this machine yet."
  note "That is normal before the first install, and expected if the install stopped early."
  say ""
  if [ "$PROBLEMS" -gt 0 ]; then
    say "  ${B}Fix the ✗ above, then install:${R}"
  else
    say "  ${B}Nothing looks wrong with this machine. To install:${R}"
  fi
  say "    curl -fsSL https://raw.githubusercontent.com/embabel-worlds/appliance/main/install.sh | sh"
  exit 0
fi

# Each container in its own words: running, restarting, exited — and how many times it
# has gone round, which is the number that matters. A container that keeps restarting
# looks "up" in a list and is the commonest silent failure there is.
for NAME in $CONTAINERS; do
  STATUS=$(docker inspect -f '{{.State.Status}}' "$NAME" 2>/dev/null)
  RESTARTS=$(docker inspect -f '{{.RestartCount}}' "$NAME" 2>/dev/null)
  HEALTH=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}}' "$NAME" 2>/dev/null)
  SHORT=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$NAME" 2>/dev/null)
  [ -n "$SHORT" ] || SHORT="$NAME"
  case "$STATUS" in
    running)
      if [ "$RESTARTS" -gt 3 ]; then
        bad "$SHORT is running, but it has restarted $RESTARTS times — something is knocking it over." \
            "Usually not enough memory. See 'Room to run' above. The full story:  docker logs $NAME"
      elif [ "$HEALTH" = "unhealthy" ]; then
        bad "$SHORT is running but reports itself unhealthy." "Look at what it says:  docker logs $NAME"
      elif [ "$HEALTH" = "starting" ]; then
        warn "$SHORT is still starting up. Give it a minute." ""
      else
        ok "$SHORT is running."
      fi
      ;;
    restarting)
      bad "$SHORT is stuck restarting over and over ($RESTARTS times so far)." \
          "It cannot start. The reason is the last few lines of:  docker logs $NAME"
      ;;
    exited|dead|created)
      # SOME CONTAINERS ARE MEANT TO STOP. One of them exists only to download an image
      # and then exits; a cross beside it is a lie, and a checker that cries wolf on a
      # healthy machine teaches people to ignore its crosses everywhere else. Asked of
      # docker rather than matched by name: "told not to restart, and left quietly" is
      # the property that makes an exit correct, whatever the service is called.
      POLICY=$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$NAME" 2>/dev/null)
      CODE=$(docker inspect -f '{{.State.ExitCode}}' "$NAME" 2>/dev/null)
      if [ "$STATUS" = "exited" ] && [ "$CODE" = "0" ] && { [ "$POLICY" = "no" ] || [ -z "$POLICY" ]; }; then
        note "$SHORT has finished and exited, which is what it is for."
      else
        bad "$SHORT is not running (it is '"'"'$STATUS'"'"')." "Why it stopped:  docker logs $NAME"
      fi
      ;;
    *)
      warn "$SHORT is in an unexpected state: $STATUS" ""
      ;;
  esac
done

# ── 4. the doors ─────────────────────────────────────────────────────────────
# Ports come from .env when there is one, because a machine that had a clash gets a
# different block and checking the defaults would be checking somebody else's appliance.
head2 "Can you reach it?"
ENVFILE=""
SETTINGS=".env"
[ "$INSTANCE" = "appliance" ] || SETTINGS=".env.$INSTANCE"
for D in "$HOME/embabel/worlds" "$HOME/embabel-worlds" "$HOME/embabel-me"; do
  [ -f "$D/$SETTINGS" ] && ENVFILE="$D/$SETTINGS" && break
done
BASE=11042
if [ -n "$ENVFILE" ]; then
  FOUND=$(sed -n 's/^EMBABEL_PORT_BASE=\([0-9]*\).*/\1/p' "$ENVFILE" | head -1)
  [ -n "$FOUND" ] && BASE="$FOUND"
  note "Using the ports recorded in $ENVFILE."
fi
WORLDS=$((BASE + 1))
CONSOLE=$((BASE + 2))

probe() {  # url -> prints the HTTP code, or 000 when nothing answered
  curl -s -o /dev/null -m 15 -w '%{http_code}' "$1" 2>/dev/null || echo 000
}

# 401 is the RIGHT answer from a door that wants a password. Anything in the 200s or
# 300s or 400s means something is listening and talking; 000 means nothing is there.
CODE=$(probe "http://localhost:$WORLDS/actuator/health")
case "$CODE" in
  2*|3*|4*) ok "The Embabel server answers on port $WORLDS." ;;
  *) bad "Nothing answers on port $WORLDS, where the Embabel server should be." \
         "If the containers above are running, it may still be starting — wait a minute and run this again." ;;
esac

CODE=$(probe "http://localhost:$CONSOLE/api/v1/realms")
case "$CODE" in
  2*|3*|4*) ok "The console answers on port $CONSOLE, and can reach the server behind it." ;;
  502|503|504)
     bad "The console is running but cannot reach the server behind it." \
         "This usually fixes it:  docker restart $(docker ps -a --filter "label=com.docker.compose.project=$PROJECT" --filter label=com.docker.compose.service=worlds-console --format '{{.Names}}' 2>/dev/null | head -1)" ;;
  *) bad "Nothing answers on port $CONSOLE, where the console should be." \
         "Open it in a browser to confirm:  http://localhost:$CONSOLE" ;;
esac

# ── 5. what the appliance last complained about ──────────────────────────────
# ERRORS AND WARNINGS ONLY. A full log carries whatever the world has read, which is not
# something to paste into an issue by accident.
head2 "Recent errors, if any"
APP=$(printf '%s\n' $CONTAINERS | grep -E 'worlds-1$|assistant-1$' | head -1)
if [ -n "$APP" ]; then
  # ERRORS FIRST, AND WARNINGS ONLY IF THERE ARE NONE. A working appliance carries plenty
  # of warnings — a rejected optional token, a realm with a malformed skill file — and
  # eight of them under a heading like this one reads as eight things wrong to somebody
  # who has no way to tell. Errors are the ones worth interrupting for.
  LINES=$(docker logs --tail 400 "$APP" 2>&1 | grep -E "ERROR|Exception|Caused by" | tail -8)
  if [ -n "$LINES" ]; then
    printf '%s\n' "$LINES" | sed 's/^/     /'
    note "Errors only — the rest of the log is not shown, because it can contain your own documents."
  else
    ok "No errors in the recent log."
    WARNS=$(docker logs --tail 400 "$APP" 2>&1 | grep -cE "WARN" 2>/dev/null || echo 0)
    [ "${WARNS:-0}" -gt 0 ] && note "$WARNS warning(s) too, which on a working appliance are usually harmless.  docker logs $APP"
  fi
fi

# ── the verdict ──────────────────────────────────────────────────────────────
# IF THEY HAVE A CODING AGENT, it can do the rest of this for them. Printed, never run:
# this script promises to change nothing, and writing into somebody's home directory is a
# change however small. The skill is fetched on its own because the checkout that would
# normally carry it is exactly what a failed install does not leave behind.
agent_offer() {
  command -v claude >/dev/null 2>&1 || return 0
  say ""
  say "  ${DIM}You have Claude Code. It can take this from here — give it the runbook once:${R}"
  say "  ${DIM}  mkdir -p ~/.claude/skills/appliance-doctor && curl -fsSL \\${R}"
  say "  ${DIM}    https://raw.githubusercontent.com/embabel-worlds/appliance/main/skills/appliance-doctor/SKILL.md \\${R}"
  say "  ${DIM}    -o ~/.claude/skills/appliance-doctor/SKILL.md${R}"
  say "  ${DIM}then ask it why Embabel will not start.${R}"
}

say ""
if [ "$PROBLEMS" -eq 0 ]; then
  say "  ${GRN}${B}Nothing looks broken.${R}"
  say "  ${DIM}If it still is not working, the next step is:  embabel doctor${R}"
  say "  ${DIM}and the guide at https://github.com/embabel-worlds/appliance/blob/main/docs/guide/troubleshooting.md${R}"
  agent_offer
else
  say "  ${B}$PROBLEMS thing(s) to fix, in order:${R}"
  N=1
  while IFS= read -r LINE; do
    [ -n "$LINE" ] && printf '    %s. %s\n' "$N" "$LINE" && N=$((N + 1))
  done < "$ADVICE_FILE"
  agent_offer
  say ""
  say "  ${DIM}Still stuck? Copy everything this printed into an issue at${R}"
  say "  ${DIM}https://github.com/embabel-worlds/appliance/issues — it contains no passwords or keys.${R}"
fi
say ""
exit 0
