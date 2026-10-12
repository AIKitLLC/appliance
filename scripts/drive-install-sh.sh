#!/bin/sh
# Run install.sh against a FAKE machine and assert on what it refused or did.
#
#   sh scripts/drive-install-sh.sh offline    # no network, no Docker needed
#   sh scripts/drive-install-sh.sh network    # downloads a real tarball
#   sh scripts/drive-install-sh.sh all
#   sh scripts/drive-install-sh.sh no-docker  # one scenario by name
#
# WHY STUBS AND NOT A REAL MACHINE. Every refusal in install.sh describes a
# machine somebody actually had: no docker on PATH, Rancher Desktop on
# containerd, Docker installed but stopped, a directory already holding
# something else. None of those can be produced on a runner that simply has
# Docker working, and all of them are a `command -v` away from being produced
# here. What a real install does is scripts/drive-install.py's job.
#
# WHY A DISTRO MATRIX IN CI. install.sh is #!/bin/sh, so `sh` is dash on Debian,
# busybox ash on Alpine, bash-in-posix-mode on a Mac. The bashisms this script
# must not grow are invisible until one of those runs it, which is why
# .github/workflows/installer.yml runs this file inside four distro images
# rather than four times on ubuntu-latest.
#
# EMBABEL_TEST_REF picks the ref the network scenarios download (default: main).
# CI sets it to the commit under test, so a PR that breaks the tarball fails here
# rather than after the merge.
set -eu

HERE=$(cd "$(dirname "$0")/.." && pwd)
WHICH=${1:-offline}
REF=${EMBABEL_TEST_REF:-main}

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT INT TERM

PASSED=0
FAILED=0
SKIPPED=0

# A PATH with the real tools and NOTHING ELSE, so each scenario adds back only
# what it is about.
#
# DISCOVERED, NOT HARD-CODED. This was `/usr/local/bin:/usr/bin:/bin` and left
# Git Bash — where curl lives in /mingw64/bin — with no curl at all, so every
# network scenario failed as "Could not reach GitHub" and blamed the installer
# for the harness. The tools below are the ones install.sh actually calls.
MIN_PATH="$WORK/stub"
for tool in curl tar mktemp sed grep cat chmod sleep uname; do
  path=$(command -v "$tool" 2>/dev/null) || continue
  dir=$(dirname "$path")
  case ":$MIN_PATH:" in *":$dir:"*) ;; *) MIN_PATH="$MIN_PATH:$dir" ;; esac
done

# Whether a REAL docker is unavoidable in those directories. On Linux the CLI
# often sits in /usr/bin beside curl, and dropping that directory would cost us
# curl — so the two scenarios that need docker absent say they were skipped
# rather than quietly testing nothing. In the distro images of
# .github/workflows/installer.yml there is no docker CLI at all, which is where
# these scenarios are a gate.
REAL_DOCKER=""
IFS=: ; for dir in $MIN_PATH; do
  [ "$dir" = "$WORK/stub" ] && continue
  [ -x "$dir/docker" ] && { REAL_DOCKER="$dir/docker"; break; }
done; unset IFS

stub() { # name, body — a fake executable early on PATH
  mkdir -p "$WORK/stub"
  printf '#!/bin/sh\n%s\n' "$2" > "$WORK/stub/$1"
  chmod +x "$WORK/stub/$1"
}

# The docker stub answers the three questions install.sh asks, each overridable
# so one stub covers "working", "no compose" and "not running". `model status`
# fails by default: no Model Runner is the common case and the one whose notice
# must NOT read as a failed install.
stub_docker() {
  stub docker '
case "${1:-} ${2:-}" in
  "compose version") exit ${STUB_COMPOSE_EXIT:-0} ;;
  "info "*|"info") exit ${STUB_INFO_EXIT:-0} ;;
  "model status") exit ${STUB_MODEL_EXIT:-1} ;;
esac
exit 0'
}

# EVERY candidate name find_python() tries, not just python3: the loop prefers
# python3.13 … python3.10 first, and a runner that has one of those would reach
# the real interpreter and run the real wizard. Logging argv is what lets a
# scenario assert WHICH mode script was handed off to.
stub_python() {
  for name in python3.13 python3.12 python3.11 python3.10 python3 python; do
    stub "$name" '
if [ "${1:-}" = "-c" ]; then exit 0; fi   # the >=3.9 probe, and the shim writer
printf "%s\n" "$*" >> "'"$WORK"'/python.log"
exit 0'
  done
}

reset() { # a clean fake machine between scenarios
  rm -rf "$WORK/stub" "$WORK/home" "$WORK/python.log"
  mkdir -p "$WORK/home"
}

run() { # runs install.sh, captures both streams and the status
  set +e
  env -i \
    PATH="$MIN_PATH" HOME="$WORK/home" \
    EMBABEL_HOME="${EMBABEL_HOME:-$WORK/home/embabel/worlds}" \
    EMBABEL_REF="$REF" \
    ${EMBABEL_MODE:+EMBABEL_MODE="$EMBABEL_MODE"} \
    ${STUB_COMPOSE_EXIT:+STUB_COMPOSE_EXIT="$STUB_COMPOSE_EXIT"} \
    ${STUB_INFO_EXIT:+STUB_INFO_EXIT="$STUB_INFO_EXIT"} \
    sh "$HERE/install.sh" > "$WORK/out" 2>&1
  STATUS=$?
  set -e
}

check() { # scenario, expected status, expected substrings…
  scenario=$1; want=$2; shift 2
  bad=""
  [ "$STATUS" = "$want" ] || bad="exited $STATUS, wanted $want"
  for needle in "$@"; do
    grep -F -q "$needle" "$WORK/out" || bad="$bad${bad:+; }missing: $needle"
  done
  if [ -n "$bad" ]; then
    FAILED=$((FAILED + 1))
    printf '  x  %s — %s\n' "$scenario" "$bad"
    sed 's/^/       | /' "$WORK/out" | tail -12
  else
    PASSED=$((PASSED + 1))
    printf '  ok %s\n' "$scenario"
  fi
}

wants() { # is this scenario in scope?
  case "$WHICH" in
    all) return 0 ;;
    offline) [ "$2" = offline ] ;;
    network) [ "$2" = network ] ;;
    *) [ "$WHICH" = "$1" ] ;;
  esac
}

# ── no Docker at all ────────────────────────────────────────────────────────
# The essay, not just the verdict: the whole argument of copy/docker-required.txt
# is that a stranger deciding whether this product is worth the trouble gets the
# reason. A refusal that shrinks back to one line is a regression.
if wants no-docker offline && [ -n "$REAL_DOCKER" ]; then
  SKIPPED=$((SKIPPED + 1))
  printf '  -- no-docker (skipped: a real docker at %s cannot be hidden)\n' "$REAL_DOCKER"
elif wants no-docker offline; then
  reset
  run
  check "no-docker" 1 \
    "No 'docker' on your PATH." \
    "Embabel needs Docker, and it is the only thing you have to install yourself."
fi

# ── Rancher Desktop on containerd ───────────────────────────────────────────
# BOTH tools present is the test install.sh makes, and the point is that this
# person gets the one command that fixes it instead of the install-Docker essay.
if wants rancher offline && [ -n "$REAL_DOCKER" ]; then
  SKIPPED=$((SKIPPED + 1))
  printf '  -- rancher-containerd (skipped: a real docker at %s cannot be hidden)\n' "$REAL_DOCKER"
elif wants rancher offline; then
  reset
  stub rdctl 'exit 0'
  stub nerdctl 'exit 0'
  run
  check "rancher-containerd" 1 \
    "Rancher Desktop is set to containerd." \
    "rdctl set --container-engine docker"
  if grep -F -q "the only thing you have to install yourself" "$WORK/out"; then
    FAILED=$((FAILED + 1))
    printf '  x  rancher-containerd — printed the install-Docker essay at somebody who has a runtime\n'
  fi
fi

# ── Docker there, Compose v1 or absent ──────────────────────────────────────
if wants compose-missing offline; then
  reset
  stub_docker
  STUB_COMPOSE_EXIT=1 run
  unset STUB_COMPOSE_EXIT
  check "compose-missing" 1 "Docker Compose v2 is required"
fi

# ── Docker installed but not started ────────────────────────────────────────
# A DIFFERENT problem from missing, and the long essay would be condescending
# here: this person already chose Docker.
if wants not-running offline; then
  reset
  stub_docker
  STUB_INFO_EXIT=1 run
  unset STUB_INFO_EXIT
  check "not-running" 1 "Docker is installed but not running."
fi

# ── the target directory holds somebody else's files ────────────────────────
if wants occupied offline; then
  reset
  stub_docker
  mkdir -p "$WORK/home/occupied"
  : > "$WORK/home/occupied/notes.txt"
  EMBABEL_HOME="$WORK/home/occupied" run
  unset EMBABEL_HOME
  check "occupied-dir" 1 "exists and is not an Embabel install."
  [ -f "$WORK/home/occupied/notes.txt" ] || {
    FAILED=$((FAILED + 1))
    printf '  x  occupied-dir — it refused AND took the files with it\n'
  }
fi

# ── a ref that does not exist ───────────────────────────────────────────────
# 404 is the one status install.sh must not retry, and the message has to name
# the two variables that can be wrong.
if wants bad-ref network; then
  reset
  stub_docker
  REF="no-such-ref-$$" run
  check "bad-ref" 1 "check EMBABEL_REF"
fi

# ── the whole way through, twice ────────────────────────────────────────────
# What this pins is the hand-off: the tarball unpacked, setup.py present, and
# the mode script that was actually launched. EMBABEL_MODE is the variable most
# likely to be read once and then forgotten on one of the two platforms.
if wants happy network; then
  for mode in worlds me; do
    reset
    stub_docker
    stub_python
    EMBABEL_MODE=$mode run
    unset EMBABEL_MODE
    check "happy-$mode" 0 "Done. Starting setup"
    [ -f "$WORK/home/embabel/worlds/setup.py" ] || {
      FAILED=$((FAILED + 1))
      printf '  x  happy-%s — no setup.py after a successful install\n' "$mode"
    }
    grep -F -q "./$mode.py" "$WORK/python.log" 2>/dev/null || {
      FAILED=$((FAILED + 1))
      printf '  x  happy-%s — handed off to %s\n' "$mode" \
        "$(cat "$WORK/python.log" 2>/dev/null || echo 'nothing')"
    }
  done

  # Re-running over an existing install is the UPDATE path, and it says so
  # rather than pretending this is a first install.
  stub_docker
  stub_python
  run
  check "happy-rerun" 0 "Updating" "your .env and data are untouched"
fi

printf '\n  %d passed, %d failed' "$PASSED" "$FAILED"
[ "$SKIPPED" = 0 ] && printf '\n' || printf ', %d skipped\n' "$SKIPPED"
[ "$FAILED" = 0 ] || exit 1
