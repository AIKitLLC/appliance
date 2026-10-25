"""Onto the latest published build: the checkout as well as the images.

Nothing here builds. The compose files are pull-only by design, so this verb
lands on what the registry publishes and says so when that turns out to be older
than a local build it replaced.
"""

from __future__ import annotations
import hashlib
import os
import shutil
import subprocess
import tempfile
import urllib.request

from .colour import dim
from .core import APPLIANCE_DIR, ME_APP_DIR, SetupError
from .settings import embabel_command, source_ref, source_repo

from .dockerlib import _compose, _docker, find_mode_container, images_for, retire_monitoring
from .versions import image_identity, mode_image
from .bolt import ensure_public_host
from .pins import pin_before_start

def _head() -> str | None:
    run = subprocess.run(["git", "-C", APPLIANCE_DIR, "rev-parse", "HEAD"],
                         capture_output=True, text=True)
    return run.stdout.strip() if run.returncode == 0 else None
# What `curl … | sh` installs from, and what an upgrade of that install re-reads.
#
# READ PER CALL, not once at import: the ref lives in .env for an install that was made
# from a branch, and a module constant captured whatever the shell happened to export —
# so an upgrade run without that variable quietly pulled main over somebody's branch.
# [source_ref] and [source_repo] look at the environment first, then .env, then the default.

# What "the CLI" is, for deciding whether a refresh actually changed anything the
# operator would notice. Not the whole tree: docs and copy move constantly and
# saying "42 files" tells nobody whether the command they just ran is different.
CLI_PATHS = ("setup.py", "embabel", "embabel_setup", "install.sh")


def _cli_fingerprint() -> str:
    """One hash over the files that make up the command, so a refresh can say
    whether it moved rather than claiming it did."""
    digest = hashlib.sha256()
    for name in CLI_PATHS:
        path = os.path.join(APPLIANCE_DIR, name)
        files = []
        if os.path.isdir(path):
            for root, _dirs, entries in os.walk(path):
                files += [os.path.join(root, e) for e in sorted(entries) if e.endswith(".py")]
        elif os.path.exists(path):
            files = [path]
        for f in sorted(files):
            try:
                with open(f, "rb") as fh:
                    digest.update(fh.read())
            except OSError:
                continue
    return digest.hexdigest()


def refresh_from_tarball() -> tuple[bool, str]:
    """Re-download the appliance over itself, for an install that came from curl.

    A CURL INSTALL HAD NO WAY TO UPDATE ITS OWN CLI. install.sh extracts a tarball,
    so there is no `.git` to fast-forward, and upgrade said "images only" and moved
    on — which meant every fix to setup.py or the `embabel` command was unreachable
    to exactly the people who installed the recommended way. They got new images
    driven by an old installer, indefinitely.

    Extraction only ever writes files the tarball contains, so `.env`, realms/ and
    anything else local survives untouched — none of them are in it.
    """
    repo, ref = source_repo(), source_ref()
    url = f"https://codeload.github.com/{repo}/tar.gz/{ref}"
    before = _cli_fingerprint()
    workspace = tempfile.mkdtemp(prefix="embabel-upgrade-")
    tarball = os.path.join(workspace, "appliance.tar.gz")
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            if response.status != 200:
                return False, f"checkout NOT updated (GitHub answered {response.status})"
            with open(tarball, "wb") as f:
                shutil.copyfileobj(response, f)
        run = subprocess.run(
            ["tar", "xzf", tarball, "-C", APPLIANCE_DIR, "--strip-components=1"],
            capture_output=True, text=True, timeout=180,
        )
        if run.returncode != 0:
            return False, f"checkout NOT updated ({(run.stderr or '').strip()[:120]})"
    except Exception as e:
        # Never fatal: the images below are the bigger half of an upgrade, and a
        # network that failed here will have failed there too, more loudly.
        return False, f"checkout NOT updated ({e})"
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    if _cli_fingerprint() == before:
        return False, f"checkout already current ({repo}@{ref})"
    return True, (f"checkout refreshed from {repo}@{ref}"
                  "\n    the `embabel` command itself changed — this run is still the old one")


def pull_checkout() -> tuple[bool, str]:
    """Fast-forward the checkout. Returns (moved, what to tell the operator)."""
    if not os.path.isdir(os.path.join(APPLIANCE_DIR, ".git")):
        return refresh_from_tarball()
    before = _head()
    run = subprocess.run(["git", "-C", APPLIANCE_DIR, "pull", "--ff-only"],
                         capture_output=True, text=True, timeout=180)
    if run.returncode != 0:
        # The two ordinary reasons, named: local edits, or no upstream. Either
        # way this is a note, not a failure — the images below still move.
        reason = (run.stderr or run.stdout).strip().splitlines()
        return False, f"checkout NOT updated ({reason[-1] if reason else 'git declined'})"
    after = _head()
    if before and after and before != after:
        changed = subprocess.run(["git", "-C", APPLIANCE_DIR, "diff", "--name-only", f"{before}..{after}"],
                                 capture_output=True, text=True)
        files = changed.stdout.split() if changed.returncode == 0 else []
        note = f"checkout updated {before[:7]} → {after[:7]} ({len(files)} file(s))"
        if any(f.startswith(f"{ME_APP_DIR}/") for f in files):
            # dist/ is built from these, and nothing in the run path rebuilds it.
            note += "\n    the Me app changed — rebuild it: npm --prefix me-app run build"
        return True, note
    return False, "checkout already current"
def upgrade(mode: str) -> dict:
    """Latest checkout, latest published images, containers actually running them.

    The digests are read on both sides rather than trusting `up -d` to have done
    something: "pulled" and "the container is now on it" are different claims,
    and only the second one is the thing anybody wanted.
    """
    notes = []
    _moved, note = pull_checkout()
    notes.append(note)

    # Read BEFORE the pull, because the pull is what moves the tag underneath it.
    before = image_identity(mode_image(mode) or "")
    held = _image_ids(images_for(mode))

    if _compose(mode, "pull").returncode != 0:
        raise SetupError("docker compose pull failed — see the output above.")
    # The images are new and nothing has been recreated yet — the moment to pin a row
    # that waits on the image, before the first boot on it (pins.pin_before_start).
    pin_before_start(mode)
    # And the name Bolt is advertised by, which compose reads from .env (see bolt.py).
    ensure_public_host(mode)
    if _compose(mode, "up", "-d").returncode != 0:
        raise SetupError("docker compose up failed — see the output above.")
    retire_monitoring(mode)
    reclaimed = _remove_superseded(held, _image_ids(images_for(mode)))
    if reclaimed:
        notes.append(reclaimed)

    after = image_identity(mode_image(mode) or "")
    if before.get("digest") and after.get("digest") and before["digest"] != after["digest"]:
        notes.append(f"server image {before['digest'][7:19]} → {after['digest'][7:19]}")
        # A locally-built image can be NEWER than what the registry serves,
        # because snapshot tags publish on release rather than on every push.
        # Landing on the published build is exactly what this verb is for, so
        # this is a NOTE and not a refusal — but a local build vanishing without
        # a word is the kind of surprise people spend an afternoon on.
        if before.get("created") and after.get("created") and after["created"] < before["created"]:
            notes.append("NOTE: the published image is OLDER than the one that was running.\n"
                         "    If that was your own build, this replaced it — which is what\n"
                         "    `upgrade` means. Rebuild it if you wanted it back.")
        else:
            notes.append("give the server a moment to come back")
    elif after.get("digest"):
        notes.append("server image already current")

    # `up -d` recreates a container whose image changed, but "pulled" and "the
    # container is now running it" are different claims and only the second is
    # what anybody wanted. So check, rather than assume.
    container = find_mode_container(mode)
    if container and not _same_image(container, mode_image(mode)):
        notes.append(f"WARNING: {container} is NOT running the pulled image.\n"
                     f"    `{embabel_command('down')}` then `{embabel_command('up')}` will land it.")
    return {"mode": mode, "notes": notes, "digest": after.get("digest")}
def _same_image(container: str, image: str | None) -> bool:
    """Is this container running THAT image, by local image id rather than by tag?
    A tag is a moving name; two containers can both say `:latest` and be
    different builds, which is the whole failure this check exists to catch."""
    if not image:
        return True
    running = _docker("inspect", container, "--format", "{{.Image}}", timeout=15)
    wanted = _docker("image", "inspect", image, "--format", "{{.Id}}", timeout=15)
    if not running or not wanted or running.returncode != 0 or wanted.returncode != 0:
        return True  # cannot tell; do not cry wolf
    return running.stdout.strip() == wanted.stdout.strip()


def _image_ids(images: list[str]) -> dict[str, str]:
    """Local image id behind each reference the mode runs. A reference not pulled
    yet has no id and is simply absent — there is nothing of it to supersede."""
    ids = {}
    for ref in images:
        run = _docker("image", "inspect", ref, "--format", "{{.Id}}", timeout=15)
        if run and run.returncode == 0 and run.stdout.strip():
            ids[ref] = run.stdout.strip()
    return ids


def _remove_superseded(before: dict[str, str], after: dict[str, str]) -> str | None:
    """Delete the images this upgrade just moved the appliance's tags off.

    EVERY UPGRADE OF A `:latest` INSTALL LEFT ITS PREVIOUS IMAGES BEHIND — the server,
    the sandbox and Grafana, about 4.5 GB a time — and nothing ever took them back, so
    an appliance that kept itself current slowly filled the Docker disk.

    Only those ids, and only once nothing else wants them: no tag left on the image —
    a local build tagged `:memory` as well as `:latest` keeps its `:memory` — and no
    container, running or stopped, made from it. Both are checked here rather than
    left to `docker image rm` to refuse, because under the containerd image store it
    does not: removing by id drops every tag the image still carries. This is why the
    reclaim lives in `upgrade` rather than `prune`, which does not touch images (see
    `cmd_prune`): here the appliance knows which images were its own a minute ago.
    """
    gone = {before[ref] for ref in before if after.get(ref) and after[ref] != before[ref]}
    # A count and not a byte total: an image's size includes layers it shares with
    # its successor, so summing them would claim space that was never freed.
    removed = 0
    for image_id in sorted(gone):
        if not _unwanted(image_id):
            continue
        run = _docker("image", "rm", image_id, timeout=60)
        if run and run.returncode == 0:
            removed += 1
    return f"removed {removed} superseded image(s)" if removed else None


def _unwanted(image_id: str) -> bool:
    """No tag names it and no container uses it. Anything unreadable counts as wanted."""
    tags = _docker("image", "inspect", image_id, "--format", "{{len .RepoTags}}", timeout=15)
    if not tags or tags.returncode != 0 or tags.stdout.strip() != "0":
        return False
    users = _docker("ps", "-aq", "--filter", f"ancestor={image_id}", timeout=15)
    return bool(users) and users.returncode == 0 and not users.stdout.strip()
