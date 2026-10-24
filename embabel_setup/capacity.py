"""What this machine can actually give the appliance, said before it costs anything.

WHAT DOCKER HAS, NOT WHAT THE MACHINE HAS. On macOS and Windows the appliance lives
inside Docker's own VM and can never see more than that VM was given — a 32GB laptop with
Docker set to 2GB runs out of memory, and a 8GB one with Docker set to 6GB is fine. Host
RAM answers the wrong question, and answering it would tell people to buy hardware when
the fix is a slider in Settings.

NUMBERS THAT WERE MEASURED, NOT SPECIFIED. Everything below came off a working appliance
rather than out of a requirements document, and the wording says so, because a limit
nobody has tested at the boundary is a guess with a decimal point. They are a warning and
never a refusal: an install that is merely tight should proceed and be told, and only the
person running it knows whether tight is acceptable today.
"""
from __future__ import annotations

import json
import os
import shutil

from .colour import MIDDOT, dim, warn
from .dockerlib import _docker, graph_engine
from .memory import describe, plan, resolve
from .settings import monitoring_enabled

# Measured on a working worlds appliance on Neo4j, idle: worlds 2.47 GiB, neo4j 2.09 GiB,
# console 7.5 MiB. Not measured on the Embabel engine yet, so the same bar stands there.
# Rounded up, because a code sandbox is another container and nobody installs this to
# leave it idle.
CORE_MEMORY_BYTES = 5 * 1024 ** 3

# The core pull — assistant 1.71 GB, neo4j 1.09 GB, console 0.08 GB — plus the embedding
# model, which is downloaded before any of them and is about 1.1 GB.
CORE_DISK_BYTES = 4 * 1024 ** 3

# Everything, once the deferred services have arrived behind you. docling alone is 7.23 GB
# and the code sandbox 2.61 GB, which is why this is stated separately: a disk that is
# fine at the finish line can fill up an hour later, silently, in the background.
FULL_DISK_BYTES = 14 * 1024 ** 3


def gb(size: int | float) -> str:
    """A size a person reads, not a size a machine emits."""
    return f"{size / 1024 ** 3:.1f} GB"


def docker_capacity() -> dict | None:
    """Memory, CPUs and free disk as DOCKER sees them, or None if it cannot be asked.

    None is not a problem to report: `embabel doctor` has already said whether docker is
    installed and running, and a second complaint about the same thing in different words
    is how a check trains people to skim.
    """
    run = _docker("info", "--format", "{{json .}}")
    if run is None or run.returncode != 0:
        return None
    try:
        info = json.loads(run.stdout)
    except ValueError:
        return None
    return {
        "memory": info.get("MemTotal") or 0,
        "cpus": info.get("NCPU") or 0,
        **disk_free(info.get("DockerRootDir") or ""),
    }


def disk_free(docker_root: str) -> dict:
    """Free space where docker's images actually land.

    On Linux `DockerRootDir` is a host path and can be measured directly. On macOS and
    Windows it names a directory inside the VM, which this process cannot see — but the
    VM's disk image grows on the host, so the home volume bounds it just the same. Either
    way the answer is "space that images will consume as they arrive", which is the only
    thing worth reporting.
    """
    for path in (docker_root, os.path.expanduser("~")):
        if path and os.path.isdir(path):
            try:
                return {"disk": shutil.disk_usage(path).free, "disk_where": path}
            except OSError:
                continue
    return {"disk": 0, "disk_where": ""}


def capacity_notes() -> list[tuple[bool, str]]:
    """Each line to show, with whether it is a warning.

    Returned rather than printed so that setup and doctor can render them in their own
    idioms — a bullet in one, a check in the other — without two copies of the numbers.
    """
    have = docker_capacity()
    if not have:
        return []
    notes: list[tuple[bool, str]] = []
    memory, disk = have["memory"], have["disk"]

    if memory and memory < CORE_MEMORY_BYTES:
        # The heap tip only where there is a Neo4j to shrink: on the Embabel engine the
        # graph is in the app, and NEO4J_HEAP would be a line in .env that does nothing.
        tip = ("\n    Less need:  NEO4J_HEAP=1G in .env runs the graph in half the memory."
               if graph_engine() == "NEO4J" else "")
        notes.append((True,
            f"Docker has {gb(memory)} of memory. The appliance's core needs about "
            f"{gb(CORE_MEMORY_BYTES)} once it is running — measured on a working install, "
            f"not a specification, so this may still work.\n"
            f"    More room:  Docker Desktop → Settings → Resources.{tip}"))
    elif memory:
        notes.append((False, f"Docker has {gb(memory)} of memory and {have['cpus']} CPU(s)."))
    if memory:
        # The limits `embabel up` will apply (#111), from the same probe.
        notes.append((False, f"Memory limits: {describe(resolve(plan(memory, graph_engine(), monitoring_enabled())))}."))

    if disk and disk < CORE_DISK_BYTES:
        notes.append((True,
            f"Only {gb(disk)} free on {have['disk_where']}. The core images and the "
            f"embedding model come to about {gb(CORE_DISK_BYTES)}, and this install will "
            f"not finish."))
    elif disk and disk < FULL_DISK_BYTES:
        notes.append((True,
            f"{gb(disk)} free on {have['disk_where']}. Enough to start — the core is about "
            f"{gb(CORE_DISK_BYTES)} — but about {gb(FULL_DISK_BYTES)} arrives in total, and "
            f"the rest downloads in the background after setup finishes."))
    elif disk:
        notes.append((False, f"{gb(disk)} free for images, against about {gb(FULL_DISK_BYTES)} in total."))

    return notes


def report_capacity() -> None:
    """Say it, once, before the download starts.

    ONE probe, not one per line: `docker info` is a round trip to the daemon and calling
    it twice to ask the same question is a habit that ends up in a loop somewhere.
    """
    notes = capacity_notes()
    for is_warning, text in notes:
        print(f"  {warn('!')} {text}" if is_warning else f"  {MIDDOT} " + dim(text))
    if any(is_warning for is_warning, _ in notes):
        print()
