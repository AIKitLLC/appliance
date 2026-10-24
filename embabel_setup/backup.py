"""Everything the appliance knows, copied to the host's own disk and brought
back from it.

COLD ON PURPOSE. A graph copied while it is being written is not a graph:
Community Neo4j has no online backup, and the Embabel engine's SQLite file and
journal are no safer under a live tar. So whichever mode is running stops for
the copy and starts again afterwards — including on failure, so a backup that
goes wrong is never the reason an assistant is down.
"""

from __future__ import annotations
import json
import os
import shutil
import subprocess
import time

from .colour import MIDDOT, TICK, dim
from .core import APPLIANCE_DIR, SetupError
from .dockerlib import (
    OVERRIDE_FILE, _compose, _docker, backup_mode, running_mode_names, running_modes,
)
from .settings import (
    compose_project, configured_mode, env_file, graph_engine, instance, volume_exists,
    volume_name,
)
from .versions import appliance_versions

# ── backup and restore ──────────────────────────────────────────────────────
#
# WHAT A BACKUP IS. Everything a person would grieve over lives in named volumes —
# embabel_assistant_data (worlds, documents, artifacts, credentials, and on the
# Embabel engine the graph itself, under /data/graph) and, on the engines that run
# a graph container, that container's volume — plus the host-side files that make
# this checkout THIS appliance. A backup is a plain folder holding a cold tarball
# of each volume, those files, and a manifest saying what made it. A folder rather
# than one enveloping archive on purpose: the tarballs inside are already
# compressed, and re-archiving gigabytes buys a second wait and nothing else.
#
# WHICH VOLUMES is the engine's answer, read from .env: an Embabel appliance has no
# Neo4j volume, and requiring one made its backup fail before it began. The
# manifest names the tarballs it holds, and that list — not this table — is what a
# restore needs, so a backup restores into whatever engine it was taken from.
#
# COLD ON PURPOSE. A graph copied while it is written is a graph that will not
# restore — Community Neo4j has no online backup, and a SQLite file or a journal
# caught mid-write is torn the same way. So whichever mode is running is stopped,
# the volumes are copied at rest, and the SAME mode is brought back up.
#
# The bytes never cross a bind mount: a helper container tars the volume to
# stdout and this process streams that to a file (and back, on restore). So
# Docker Desktop's file-sharing list never has an opinion about where backups
# may live, and a backup can be written to an external disk.
#
# THIS IS HOST WORK, not server work — a container cannot copy the volume it is
# running from. It lives here, beside the other lifecycle verbs, because the Me
# app's menu and `embabel backup` must not be two implementations that disagree.

# The keys as the compose files declare them; the REAL names carry the project
# prefix, which is pinned in docker-compose.yml precisely so they cannot move
# out from under an install.
DATA_VOLUME = ("embabel_assistant_data", "assistant-data.tgz", "worlds and documents")
GRAPH_VOLUMES = {
    "NEO4J": ("embabel_appliance_neo4j_data", "neo4j-data.tgz", "the knowledge graph"),
    "FALKORDB": ("embabel_appliance_falkordb_data", "falkordb-data.tgz", "the knowledge graph"),
}
# What a backup from before the manifest named its tarballs holds: Neo4j's.
LEGACY_BACKUP_FILES = ("assistant-data.tgz", "neo4j-data.tgz")


def backup_volumes() -> tuple[tuple[str, str, str], ...]:
    """The volumes this appliance's data is in, on the engine .env names."""
    graph = GRAPH_VOLUMES.get(graph_engine())
    return (DATA_VOLUME, graph) if graph else (DATA_VOLUME,)


def _volume_for(filename: str) -> tuple[str, str, str] | None:
    for entry in (DATA_VOLUME, *GRAPH_VOLUMES.values()):
        if entry[1] == filename:
            return entry
    return None


# Host-side state that shapes the appliance. Without .env a restored graph is
# unreachable — it names the engine and store the graph is in, and Neo4j's
# password lives IN its volume while the appliance's copy of it lives here. secrets.env holds the realm API credentials the compose files
# load by `env_file:`; a restore without it comes back with every authenticating
# realm silently dead, which is a worse outcome than a refusal.
def backup_config_files() -> tuple[str, ...]:
    """A function, not a constant: which settings file belongs to this appliance
    depends on which instance it is."""
    return (env_file(), OVERRIDE_FILE, "secrets.env")
# The Me app writes the override and stamps it (mounts.ts). A file WITHOUT the
# stamp was written by a person, and a restore does not eat their work.
OVERRIDE_MARKER = "# Written by Embabel Me"
# Has tar, weighs a few MB, and is pinned so a backup taken next year is cut by
# the same tool as one taken today.
BACKUP_HELPER_IMAGE = "alpine:3.22"
BACKUP_MANIFEST = "manifest.json"
# Copying a graph can legitimately take a long time. This is a backstop against
# a wedged docker CLI, not a pace expectation.
BACKUP_STREAM_TIMEOUT = 30 * 60
# Where backups go when nobody says. Under $HOME, not the checkout: --uninstall
# removes the checkout, and a backup that an uninstall deletes is not a backup.
DEFAULT_BACKUP_DIR = os.path.expanduser("~/embabel-backups")
def require_docker() -> None:
    """The volumes are only reachable through the daemon, so say that rather than
    letting each of the calls below fail separately with its own wording."""
    run = _docker("info", timeout=20)
    if run is None or run.returncode != 0:
        raise SetupError("Docker is not running — the appliance's volumes are only reachable through it.")
def _stream_volume(argv: list[str], path: str, *, into_volume: bool) -> tuple[bool, str]:
    """Run docker with one end of the pipe on a host file — how a volume leaves the
    machine as a tarball, and comes back, without a bind mount in between."""
    try:
        with open(path, "rb" if into_volume else "wb") as f:
            run = subprocess.run(
                ["docker", *argv],
                stdin=f if into_volume else subprocess.DEVNULL,
                stdout=subprocess.DEVNULL if into_volume else f,
                stderr=subprocess.PIPE,
                timeout=BACKUP_STREAM_TIMEOUT,
            )
        return run.returncode == 0, run.stderr.decode(errors="replace").strip()
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
        return False, str(e)
def _tail(text: str, limit: int = 300) -> str:
    """The end of a long output, visibly truncated — never chopped in silence."""
    return f"…{text[-limit:]}" if len(text) > limit else text
def backup_timestamp() -> str:
    """Local time, filesystem-plain: 2026-08-22-1430. A backup is named by when it was taken."""
    return time.strftime("%Y-%m-%d-%H%M")
def inspect_backup(backup_dir: str) -> dict:
    """Is this folder a backup we can restore? Returns its manifest.

    Read BEFORE the confirmation prompt, so the prompt can name the DATE of the
    backup about to replace an appliance. A folder name is not what somebody
    should be confirming.
    """
    try:
        with open(os.path.join(backup_dir, BACKUP_MANIFEST)) as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        raise SetupError(f"No readable {BACKUP_MANIFEST} in {backup_dir} — that is not an Embabel backup.")
    # Every volume it was taken with, or nothing: a graph without its documents (or the
    # reverse) is an appliance that contradicts itself, which is worse than a refusal.
    for filename in backup_volume_files(manifest):
        if not os.path.exists(os.path.join(backup_dir, filename)):
            raise SetupError(f"Backup is incomplete — {filename} is missing.")
    return manifest
def backup_volume_files(manifest: dict) -> tuple[str, ...]:
    """The volume tarballs a backup holds, as its manifest names them. One from before
    the manifest named them holds Neo4j's, which was the only engine anybody backed up."""
    named = manifest.get("volumes")
    return tuple(named) if isinstance(named, list) and named else LEGACY_BACKUP_FILES
def list_backups(parent: str) -> list[tuple[str, dict]]:
    """Every restorable backup directly under a folder, newest first. Unreadable
    entries are skipped rather than reported — this is a listing, not a doctor."""
    found = []
    for entry in sorted(os.listdir(parent)) if os.path.isdir(parent) else []:
        path = os.path.join(parent, entry)
        if not os.path.isdir(path):
            continue
        try:
            found.append((path, inspect_backup(path)))
        except SetupError:
            continue
    return sorted(found, key=lambda pair: pair[1].get("createdAt", ""), reverse=True)
def _write_manifest(dest: str, saved: list[str], volumes: list[str], mode: str) -> None:
    """Enough to answer, a year from now, "what is this and can I restore it here".

    The identity recorded is the one `embabel version` prints, and for the same
    reason: the TAG is a name that moves, so a manifest saying "latest"
    dates a backup to nothing. The image digest and the commit the jar was built
    from do not move, and between them they say exactly what wrote these bytes.
    """
    manifest = {
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "instance": instance(),
        "mode": mode,
        "files": saved,
        "engine": graph_engine(),
        "volumes": volumes,
        "versions": appliance_versions(mode),
    }
    with open(os.path.join(dest, BACKUP_MANIFEST), "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    with open(os.path.join(dest, "README.txt"), "w") as f:
        f.write(
            f"Embabel appliance backup — {time.strftime('%c')}\n\n"
            "Everything this appliance knew, at rest: the knowledge graph, worlds,\n"
            "documents, and the settings that shaped them. Restore it with:\n\n"
            f"    embabel --instance {instance()} restore {os.path.abspath(dest)}\n\n"
            "manifest.json records the exact images and commit that wrote these bytes.\n\n"
            "The .env and secrets.env here carry credentials — the database password,\n"
            "API keys, realm tokens. Treat this folder like the keys it holds.\n"
        )
def _copy_out(dest: str, mode: str) -> None:
    """The copy itself, between the stop and the restart."""
    saved, volumes = [], []
    for key, filename, what in backup_volumes():
        name = volume_name(key)
        if not volume_exists(key):
            raise SetupError(f"Volume {name} does not exist — has the appliance ever run on this machine?")
        ok, err = _stream_volume(
            ["run", "--rm", "-v", f"{name}:/from:ro", BACKUP_HELPER_IMAGE, "tar", "czf", "-", "-C", "/from", "."],
            os.path.join(dest, filename), into_volume=False)
        if not ok:
            raise SetupError(f"Backing up {what} failed: {_tail(err)}")
        size = os.path.getsize(os.path.join(dest, filename)) / 1e6
        print(f"    {what}  {size:,.0f} MB")
        saved.append(filename)
        volumes.append(filename)

    for filename in backup_config_files():
        if os.path.exists(os.path.join(APPLIANCE_DIR, filename)):
            shutil.copyfile(os.path.join(APPLIANCE_DIR, filename), os.path.join(dest, filename))
            saved.append(filename)
    _write_manifest(dest, saved, volumes, mode)
def back_up(dest_dir: str = DEFAULT_BACKUP_DIR) -> str:
    """Copy the appliance into a new timestamped folder under dest_dir."""
    require_docker()
    mode = backup_mode()
    # Bring-back is decided by what is running NOW, never assumed: backing up a
    # deliberately stopped appliance must not be the thing that starts it.
    was_running = running_mode_names()

    dest = os.path.join(os.path.abspath(os.path.expanduser(dest_dir)),
                        f"embabel-backup-{instance()}-{backup_timestamp()}")
    os.makedirs(dest, exist_ok=True)

    if was_running:
        print(f"  Stopping the {mode} mode — a graph copied while it is live is a graph that will not restore.")
        for stopping in was_running:
            _compose(stopping, "stop", capture=True)

    try:
        _copy_out(dest, mode)
    finally:
        # The appliance comes back whether the copy worked or not. A failed backup
        # must never be the reason somebody's assistant is down.
        for restarting in was_running:
            _compose(restarting, "up", "-d", capture=True)
    return dest
def restore(backup_dir: str) -> str:
    """Replace this appliance's data and configuration with a backup's.

    Destructive by definition, and nothing here asks — the CALLER owns the
    confirmation, because the Me app's dialog and the CLI's prompt are the same
    decision asked in two different rooms.
    """
    manifest = inspect_backup(backup_dir)
    require_docker()
    came_from = manifest.get("instance")
    if came_from and came_from != instance():
        # Allowed, and useful — cloning one appliance into a scratch one is half
        # the reason to have two. But it overwrites a DIFFERENT appliance than
        # the backup came from, so it is said out loud rather than discovered.
        print(f"  This backup was taken from '{came_from}'; restoring it into "
              f"'{instance()}'.")
    mode = manifest.get("mode") or backup_mode()
    was_running = running_mode_names()
    for stopping in was_running or [mode]:
        _compose(stopping, "stop", capture=True)

    # Config first, volumes second: .env decides the ports, the engine, and the Neo4j
    # password the restored graph was created under, so compose must be reading the
    # backup's copy by the time anything comes back up. What is replaced is set
    # ASIDE, not deleted — one .before-restore per file, kept until the next
    # restore overwrites it. Restoring means the backup's world, so a file the
    # backup does NOT have is set aside too.
    for filename in backup_config_files():
        current = os.path.join(APPLIANCE_DIR, filename)
        replacement = os.path.join(backup_dir, filename)
        if os.path.exists(current):
            if filename == OVERRIDE_FILE:
                with open(current) as f:
                    if not f.read().startswith(OVERRIDE_MARKER):
                        raise SetupError(f"{filename} was hand-written, not generated — "
                                         "move it aside yourself, then restore again.")
            os.replace(current, f"{current}.before-restore")
        if os.path.exists(replacement):
            shutil.copyfile(replacement, current)
    # A backup's .env from before the pins says nothing about what it ran on, and what
    # it ran on is in the tarballs about to land. Late: pins imports this module.
    from .pins import pin_install
    pin_install(creating=False)

    restoring = [entry for entry in map(_volume_for, backup_volume_files(manifest)) if entry]
    # A fresh machine has no volumes yet. Let COMPOSE create them: a volume made
    # by `docker run` lacks compose's project labels, and `up` on some versions
    # refuses to adopt it. `up --no-start` also pulls images, so a first restore
    # on a clean machine is a long one; on an existing install this never runs.
    if any(not volume_exists(key) for key, _filename, _what in restoring):
        print("  Creating the appliance's volumes (this pulls images — it can take a while).")
        _compose(mode, "up", "--no-start")

    for key, filename, what in restoring:
        print(f"    {what}")
        ok, err = _stream_volume(
            ["run", "--rm", "-i", "-v", f"{volume_name(key)}:/to", BACKUP_HELPER_IMAGE,
             "sh", "-c", "find /to -mindepth 1 -delete && tar xzf - -C /to"],
            os.path.join(backup_dir, filename), into_volume=True)
        if not ok:
            raise SetupError(f"Restoring {what} failed: {_tail(err)}")

    # Up unconditionally, even if nothing was running when this started. Backup
    # is the verb that must not change what is up; restore is the verb whose
    # whole point is the restored appliance, and leaving it stopped would be
    # answering "put my data back" with a machine that says nothing.
    _compose(mode, "up", "-d", capture=True)
    return manifest.get("createdAt", "an unknown time")
