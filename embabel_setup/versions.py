"""Which appliance this is: checkout, image digest, and the commit its jar was
built from.

NOT AN HTTP CALL. The server answers this too, but the moment somebody needs a
version is the moment it will not boot — so everything here reads the image and
the jar, and works with the container stopped.
"""

from __future__ import annotations
import json
import os
import re
import struct
import subprocess
import zlib

from .core import APPLIANCE_DIR
from .dockerlib import MODE_SERVICE, _compose, _docker, backup_mode, find_mode_container, service_container
from .settings import compose_project, env_file_value, graph_engine, source_ref, source_repo

# ── what is actually running ────────────────────────────────────────────────
#
# FOUR LAYERS DIFFER, and only one of them is the thing people say out loud.
# EMBABEL_VERSION defaults to `latest`, which is a name rather than an
# identity — two machines both "on latest" can be weeks apart. What
# pins an install is the image DIGEST, and what pins the code inside it is the
# commit the jar was built from.
#
# NOT AN HTTP CALL, deliberately. The server does answer this — /actuator/info
# carries the same build and git blocks — but it is authenticated, and more to
# the point the moment somebody needs the version is the moment the appliance
# will not boot, is wedged, or is halfway through an upgrade. An endpoint
# answers none of those. Everything below reads the image and the jar, and
# works with the container stopped.
#
# READING THE JAR CHEAPLY. The appliance jar is ~400MB and the container has no
# unzip, no python and a JRE with no `jar` tool. So: read the zip's central
# directory off the END of the file, find the one entry's offset, and `dd` out
# its couple of hundred bytes. Three small reads instead of copying 400MB to
# learn six lines.

# Where the git and build metadata live inside the Spring Boot jar. The Maven
# build bakes both in (git-commit-id-maven-plugin, spring-boot-maven-plugin).
JAR_PATH = "/app/assistant.jar"
JAR_GIT_ENTRY = "BOOT-INF/classes/git.properties"
JAR_BUILD_ENTRY = "META-INF/build-info.properties"
def _run_in(target: str, is_container: bool, *args: str, binary: bool = False):
    """A command against a running container if there is one, else against the
    image itself. A stopped appliance still has an image, and `version` has to
    answer for a stopped appliance — that is most of why anyone asks."""
    if is_container:
        argv = ["docker", "exec", target, *args]
    else:
        argv = ["docker", "run", "--rm", "--entrypoint", args[0], target, *args[1:]]
    try:
        return subprocess.run(argv, capture_output=True, timeout=60,
                              text=not binary)
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
def _jar_entry(target: str, is_container: bool, entry: str) -> str | None:
    """One file out of the jar, without moving the jar.

    Zip stores its index at the END, so the tail gives every entry's offset;
    then a single seek reads that entry's bytes. Zip64 because the jar is well
    past 4GB worth of entries' worth of offsets on some builds — the classic
    end-of-central-directory records 0xFFFFFFFF and defers to the Zip64 one.
    """
    tail = _run_in(target, is_container, "sh", "-c", f"tail -c 70000 {JAR_PATH}", binary=True)
    if not tail or tail.returncode != 0:
        return None
    data = tail.stdout
    marker = data.rfind(b"PK\x05\x06")
    if marker < 0:
        return None
    try:
        _size, offset = struct.unpack("<II", data[marker + 12:marker + 20])
        if offset == 0xFFFFFFFF:
            zip64 = data.rfind(b"PK\x06\x06")
            _size, offset = struct.unpack("<QQ", data[zip64 + 40:zip64 + 56])

        block = 65536
        got = _run_in(target, is_container, "dd", f"if={JAR_PATH}", f"bs={block}",
                      f"skip={offset // block}", "status=none", binary=True)
        if not got or got.returncode != 0:
            return None
        directory = got.stdout[offset % block:]
        at = directory.find(entry.encode())
        if at < 0:
            return None
        header = directory[at - 46:at]
        method = struct.unpack("<H", header[10:12])[0]
        compressed = struct.unpack("<I", header[20:24])[0]
        local = struct.unpack("<I", header[42:46])[0]

        block = 4096
        got = _run_in(target, is_container, "dd", f"if={JAR_PATH}", f"bs={block}",
                      f"skip={local // block}", "count=8", "status=none", binary=True)
        if not got or got.returncode != 0:
            return None
        raw = got.stdout[local % block:]
        name_len, extra_len = struct.unpack("<HH", raw[26:30])
        start = 30 + name_len + extra_len
        body = raw[start:start + compressed]
        # 8 is DEFLATE, 0 is STORED; a raw stream, so a negative window size.
        text = zlib.decompress(body, -15) if method == 8 else body
        return text.decode("utf8", "replace")
    except (struct.error, zlib.error, IndexError):
        return None
def parse_properties(text: str | None) -> dict:
    """A .properties file, enough for the two the jar carries. Not a general
    parser: these are machine-generated, and the only escaping in them is the
    plugin's backslash before ':' in timestamps and commit messages."""
    found = {}
    for line in (text or "").splitlines():
        if not line.strip() or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        found[key.strip()] = value.replace("\\:", ":").replace("\\=", "=").strip()
    return found
def image_identity(image: str) -> dict:
    """The tag as written, and the digest that tag currently means.

    The DIGEST is the answer to "which build is this" — the tag moves, and for
    an unpinned SNAPSHOT it moves often. `created` is the image's build time,
    which is how you tell a pull from last night from one from March.
    """
    run = _docker("image", "inspect", image, "--format",
                  "{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}\t{{.Created}}", timeout=20)
    if not run or run.returncode != 0:
        return {"image": image, "digest": None, "created": None}
    digest, _, created = run.stdout.strip().partition("\t")
    return {"image": image, "digest": digest.partition("@")[2] or None, "created": created or None}
def source_identity(mode: str) -> dict:
    """The commit the running appliance was BUILT from, read out of its jar.

    Prefers the live container — `docker exec` costs nothing when one is up.
    Falls back to the image, which starts a throwaway container for two reads.
    """
    container = find_mode_container(mode)
    target, is_container = (container, True) if container else (mode_image(mode), False)
    if not target:
        return {}
    git = parse_properties(_jar_entry(target, is_container, JAR_GIT_ENTRY))
    build = parse_properties(_jar_entry(target, is_container, JAR_BUILD_ENTRY))
    return {
        # `git.commit.id` is the full SHA; older builds carry only the abbrev,
        # because the Maven plugin was filtering the full one out by a name it
        # does not emit. Both are reported so a backup taken before that fix is
        # still readable rather than blank.
        "commit": git.get("git.commit.id") or git.get("git.commit.id.abbrev"),
        "abbrev": git.get("git.commit.id.abbrev"),
        "branch": git.get("git.branch"),
        "subject": git.get("git.commit.message.short"),
        "committed": git.get("git.commit.time"),
        # A build cut from a working tree with uncommitted changes. The commit
        # above then names where the build STARTED, not what is in it — which
        # is worth saying out loud rather than leaving to be discovered.
        "dirty": git.get("git.dirty") == "true",
        "version": build.get("build.version"),
        "built": build.get("build.time"),
    }
def mode_image(mode: str) -> str | None:
    """The image THIS mode's service runs.

    A running container is asked directly; otherwise compose resolves it, with
    its ${EMBABEL_VERSION:-...} defaults applied. The service is looked up BY
    KEY in the rendered config rather than by matching image lines — `--images`
    prints every service's image, and the worlds service and the docling image
    (ghcr.io/embabel-worlds/...) share enough of a substring that a text match
    reports the wrong one.
    """
    container = find_mode_container(mode)
    if container:
        run = _docker("inspect", container, "--format", "{{.Config.Image}}", timeout=15)
        if run and run.returncode == 0 and run.stdout.strip():
            return run.stdout.strip()
    run = _compose(mode, "config", "--format", "json", capture=True)
    if run is None or run.returncode != 0:
        return None
    try:
        return json.loads(run.stdout)["services"][MODE_SERVICE[mode]]["image"]
    except (ValueError, KeyError, TypeError):
        return None
def checkout_identity() -> dict:
    """This repo — the pin for everything that is NOT in an image: the compose
    files, the Neo4j tag they name, setup.py, the skills.

    A CURL INSTALL HAS NO `.git`, and said so as "? on ?" — which is the answer
    for the majority of installs, and useless to exactly the person testing a
    branch. install.sh extracts a tarball, so there is no commit to read; what
    there IS, since the ref became something the install remembers, is the repo
    and branch it was downloaded from. Reported as [ref]/[repo] with [tarball]
    true, so a caller can say "from embabel-worlds/appliance@my-branch" rather
    than a pair of question marks.
    """
    def git(*args: str) -> str | None:
        run = subprocess.run(["git", "-C", APPLIANCE_DIR, *args], capture_output=True, text=True)
        return run.stdout.strip() if run.returncode == 0 else None

    dirty = git("status", "--porcelain")
    commit = git("rev-parse", "HEAD")
    return {
        "commit": commit,
        "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(dirty),
        # What the download followed. Meaningful for a tarball install and true
        # of a git one too — a checkout can still be pinned to a branch by .env.
        "ref": source_ref(),
        "repo": source_repo(),
        "tarball": commit is None,
    }
def appliance_versions(mode: str | None = None) -> dict:
    """Every layer that can differ between two installs, in one dict. Shared by
    `embabel version` and the backup manifest, so a backup records exactly what
    `version` would have printed on the day it was taken."""
    mode = mode or backup_mode()
    image = mode_image(mode)
    return {
        "mode": mode,
        "checkout": checkout_identity(),
        "appliance": image_identity(image) if image else {},
        "source": source_identity(mode),
        "neo4j": image_identity(neo4j_image() or ""),
    }
def neo4j_image() -> str | None:
    """The Neo4j this install runs, or None on an engine that has no Neo4j. Its own
    container first; else the version .env pins, else the one infra.yml falls back to —
    read rather than restated here, so the CHECKOUT stays its version."""
    if graph_engine() != "NEO4J":
        return None
    container = service_container("neo4j", stopped=True)
    run = _docker("inspect", container, "--format", "{{.Config.Image}}", timeout=15) if container else None
    if run and run.returncode == 0 and run.stdout.strip():
        return run.stdout.strip()
    pinned = os.environ.get("NEO4J_VERSION") or env_file_value("NEO4J_VERSION")
    if pinned:
        return f"neo4j:{pinned}"
    with open(os.path.join(APPLIANCE_DIR, "infra.yml")) as f:
        for line in f:
            found = re.search(r"image:\s*neo4j:\$\{NEO4J_VERSION:-([^}]+)\}", line)
            if found:
                return f"neo4j:{found.group(1)}"
    return None
