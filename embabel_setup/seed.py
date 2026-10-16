"""The appliance's own documentation, into the world it just created.

A new world is empty, so the first question anybody has — what is this, and what
can I ask it — was one the product could not answer about itself. Embeddings are
local, so this costs nothing and sends nothing anywhere.
"""

from __future__ import annotations
import base64
import os
import secrets
import shutil
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import urllib.request

from .colour import MIDDOT, TICK, dim
from .core import APPLIANCE_DIR
from .status import STATUS

# ── seeding the documentation ───────────────────────────────────────────────
#
# A NEW WORLD IS EMPTY, and the first question anybody has — "what is this, and
# what can I ask it?" — is one the product could not answer about itself. The
# config views answer the structural half (what realms, what views, which model
# runs which job). This answers the prose half: the appliance's own guides, in
# the graph, searchable, on the first run.
#
# It costs nothing to send anywhere. Embeddings are local and always have been,
# so this is ~135KB through a model already running on this machine — seconds,
# no tokens, no provider key. It is the one corpus every new user wants and the
# only one we can ship without asking a question first.
#
# THE CREDENTIAL IS THE MINTED BEARER TOKEN, not the password the user just
# typed. The MCP step mints one for precisely this kind of call and hands it
# over once; setup holding a password to make an HTTP request would be a design
# smell that outlives the reason for it.
#
# BEST EFFORT, ALWAYS. Setup has succeeded by the time this runs. A world with
# no documentation in it is a smaller problem than an installer that fails at
# the finish line, so every failure here is reported and swallowed.

# The operator account, as it is created. Process memory only: never written,
# never logged, and used solely to authenticate the documentation upload below.
_ACCOUNT: tuple[str, str] | None = None
# WHAT GOES IN, AND WHY TWO THINGS CAME OUT.
#
# A design proposal ("Status: proposal. Nothing on this page is built.") was
# once seeded anyway. Asked "what is virtual cypher", the world answered from
# it — a filesystem producer with three tiers, none of which exists — because it
# was one of only two seeded files mentioning the term and the other said little.
# A retrieval corpus has no way to discount a document; whatever matches wins. A
# proposal in a knowledge base is not incomplete information, it is confident
# misinformation, and it belongs nowhere near one.
#
# AGENT_GUIDE.md came out for a smaller reason: it is instructions to a coding
# agent about how to drive an appliance, not documentation of what one is.
SEED_DOCS = ("README.md", "CLI.md", "PHONE_HOME.md", "WORLD_TEMPLATES.md")
# The skills ARE the user-facing documentation of authoring — views, handlers,
# apps, realms — and world-authoring is the only thing this repo ships that
# explains virtual Cypher at all.
SEED_DOC_DIRS = ("docs/guide", "skills")
# THE SPEC IS NOT IN THIS REPO, and it is the document that actually defines
# virtual Cypher, realms, types and composition. It lives in realm-spec, which
# worlds.embabel.com vendors the same way. Fetched over raw.githubusercontent —
# not the API, so no rate limit and no token — and skipped in silence if the
# network is not there, because a seeded world is a nicety and an installer that
# fails without internet is not.
SPEC_REPO = "https://raw.githubusercontent.com/embabel-worlds/realm-spec/main"
SPEC_DOCS = ("VIRTUAL_CYPHER.md", "VIRTUAL_CYPHER_GUIDE.md", "README.md",
             "DECLARING_TYPES.md", "LABELS_AND_COMPOSITION.md",
             "EXTERNAL_DOCUMENTS.md", "CONTEXT.md")
# Big enough for the guides, small enough that a stray file cannot become an
# ingestion job somebody did not ask for.
SEED_MAX_BYTES = 512 * 1024
# The mark on every guide the appliance ships. The server holds the same word.
SEED_TAG = "embabel-docs"
def remember_provider_key(provider: str, api_key: str) -> None:
    """Write the provider key setup just collected into .env.

    THIS IS WHAT MAKES THE ${VAR:-} FORM SAFE. The compose files hand the server
    an empty OPENAI_API_KEY when nothing supplies one, because an absent key
    throws in OpenAiModelsConfig's constructor and crash-loops a server that has
    not been asked for a key yet. An empty value would then shadow the key
    first-run setup writes into the data volume — env beats config files in
    Spring — so the variable has to stop being empty the moment a key is known.

    Writing it here means the next boot passes the real key, and every boot
    after that, without depending on the operator having exported anything.
    """
    if not api_key.strip():
        return
    from .settings import set_env_var
    variable = PROVIDER_KEY_VARS.get(provider)
    if not variable:
        return
    set_env_var(variable, api_key.strip(), (
        f"# The {provider} key you gave at setup. Kept here so the container is",
        "# handed a real key rather than an empty one — see the OPENAI_API_KEY",
        "# note in the compose files for why an empty value is not harmless.",
    ))


# Provider name as the server calls it -> the variable its config reads.
PROVIDER_KEY_VARS = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


def remember_account(username: str, password: str) -> None:
    """Hold the account setup just created, for the upload below and nothing else.

    A SETTER, not a `global` from another module. The wizard used to reach in
    with `global _ACCOUNT` while living in the same file; after the split that
    statement bound a name in setup.py's namespace instead, seed.py's stayed
    None, and the seeding reported "no credential" on every install — silently,
    because both halves were individually correct.
    """
    global _ACCOUNT
    _ACCOUNT = (username, password)


def account_username() -> str | None:
    """Who setup just created, when it created anyone.

    Normally the server says this back in /complete's `signInAs`. It is readable
    here too, for the one case where that answer never reaches the client — see
    [complete_setup] in steps.py. The password stays inside this module.
    """
    return _ACCOUNT[0] if _ACCOUNT else None


def seed_credential(api_token: str | None) -> str | None:
    """An Authorization header for the seed upload, or None if there is nothing.

    THE ACCOUNT, NOT THE MCP TOKEN — and the reverse of what this function did
    first. The token the MCP step mints authorizes `/mcp`; the REST surface does
    not accept it, and answers 401 to `POST /api/v1/documents/upload`. Measured
    against a live appliance: bearer 401, Basic 200 `{"status":"ingested"}`.
    Preferring the token therefore guaranteed the seed failed on exactly the
    installs where the MCP step HAD run, which is most of them.

    `api_token` is still taken when there is no account — a run driven with
    --token against an already-provisioned appliance has one and not the other.
    """
    if _ACCOUNT:
        return "Basic " + base64.b64encode(f"{_ACCOUNT[0]}:{_ACCOUNT[1]}".encode()).decode()
    if api_token:
        return f"Bearer {api_token}"
    return None
def documentation_files() -> list[str]:
    """The guides, as absolute paths. Named explicitly rather than globbed from the
    repo root: CONTRIBUTING and CLAUDE.md are instructions to people working ON
    the appliance, and a user searching their world should not get them back."""
    found = []
    for name in SEED_DOCS:
        path = os.path.join(APPLIANCE_DIR, name)
        if os.path.exists(path) and os.path.getsize(path) <= SEED_MAX_BYTES:
            found.append(path)
    for folder in SEED_DOC_DIRS:
        directory = os.path.join(APPLIANCE_DIR, folder)
        if not os.path.isdir(directory):
            continue
        # Recursive: skills/ is skills/<name>/SKILL.md, a level deeper than docs/.
        for root, _dirs, files in os.walk(directory):
            for entry in sorted(files):
                path = os.path.join(root, entry)
                if entry.endswith(".md") and os.path.getsize(path) <= SEED_MAX_BYTES:
                    found.append(path)
    return sorted(found)
def fetch_spec_documents(into: str) -> list[str]:
    """The realm spec, downloaded to a temporary directory. Best effort."""
    fetched = []
    for name in SPEC_DOCS:
        try:
            with urllib.request.urlopen(f"{SPEC_REPO}/{name}", timeout=30) as response:
                body = response.read()
        except Exception:
            continue
        if not body or len(body) > SEED_MAX_BYTES:
            continue
        # Prefixed so a spec page cannot be mistaken for an appliance page in a
        # search result — "realm-spec VIRTUAL_CYPHER.md" says where it came from.
        path = os.path.join(into, f"realm-spec {name}")
        with open(path, "wb") as f:
            f.write(body)
        fetched.append(path)
    return fetched
def upload_name(path: str) -> str:
    """The filename to upload a guide under — which is also its identity.

    The server keys a document on the name it arrives with, so two uploads that
    share one lose the first. Every skill is `skills/<name>/SKILL.md`, and there is
    a README at three levels, so a file inside a folder carries the folder's name in
    front: `world-authoring SKILL.md`, `guide README.md`. A file at the top of the
    checkout keeps its own.

    ONE RULE, SHARED. The Worlds console names the guides it uploads the same way,
    and so does guides.json, which is what the appliance reads to keep them current.
    Three names for one guide is three documents: this used to prefix only skills,
    so `docs/guide/README.md` replaced the top-level README and a world set up in a
    terminal held `clients.md` where one set up in a browser held `guide clients.md`.
    """
    relative = os.path.relpath(path, APPLIANCE_DIR)
    if relative.startswith(os.pardir):
        # Not in the checkout: a fetched spec page, already named for where it came from.
        return os.path.basename(path)
    parts = relative.split(os.sep)
    return f"{parts[-2]} {parts[-1]}" if len(parts) > 1 else parts[-1]


def former_name(path: str) -> str | None:
    """The name a terminal install up to this release indexed [path] under, when it differs.

    That installer prefixed only skills, and only with a folder below `skills/`, so
    `docs/guide/clients.md` went in as `clients.md` and `skills/VOICE.md` as `VOICE.md`.
    Those copies carry no tag, so nothing could tell them from the owner's own files;
    guides.json names them, and the appliance removes each one once. A former name that
    a current guide still uses — `README.md` — is not listed: that copy is replaced.
    """
    parts = os.path.relpath(path, APPLIANCE_DIR).split(os.sep)
    owner = parts[parts.index("skills") + 1:-1] if "skills" in parts else []
    old = f"{owner[-1]} {parts[-1]}" if owner else parts[-1]
    return old if old != upload_name(path) else None


def guide_manifest() -> dict:
    """What guides.json must say: every guide this checkout ships, and the spec pages.

    The appliance reads that file from the repository to index the guides itself, so
    it is the published form of [documentation_files]. scripts/check-guides.py fails
    when the two disagree, and rewrites the file when asked.
    """
    files = documentation_files()
    current = {upload_name(path) for path in files}
    guides = []
    for path in files:
        guide = {"name": upload_name(path), "path": os.path.relpath(path, APPLIANCE_DIR).replace(os.sep, "/")}
        old = former_name(path)
        if old and old not in current:
            guide["formerly"] = [old]
        guides.append(guide)
    guides += [{"name": f"realm-spec {name}", "url": f"{SPEC_REPO}/{name}"} for name in SPEC_DOCS]
    return {"guides": sorted(guides, key=lambda guide: guide["name"])}


def _upload_document(base: str, auth: str, path: str) -> bool:
    """One multipart POST, by hand — stdlib only, like everything else here.

    `auth` is a complete Authorization header value, Bearer or Basic, because
    which credential is available depends on which steps the wizard ran.
    """
    boundary = "----embabel" + secrets.token_hex(8)
    with open(path, "rb") as f:
        content = f.read()
    name = upload_name(path)
    # TAGGED, so `embabel docs sync` can later tell a guide this installer put here from
    # a document the owner added, and remove the first kind when it is withdrawn.
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="tags"\r\n\r\n'
        f"{SEED_TAG}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
        "Content-Type: text/markdown\r\n\r\n"
    ).encode() + content + f"\r\n--{boundary}--\r\n".encode()

    request = urllib.request.Request(
        f"{base}/api/v1/documents/upload", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "Authorization": auth},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return 200 <= response.status < 300
    except Exception:
        return False
class Seeding:
    """Documentation uploading in the background, so the wizard is not stuck behind it.

    IT CANNOT SIMPLY RUN AT THE END. The only credential that can index anything
    is the one the account step took, so the upload has to start there — but
    starting there put a half-minute wall between "account created" and the next
    question, which is a poor place to make somebody wait.

    It cannot run past the end either: completing setup RESTARTS the appliance,
    and an upload in flight when the server goes down is a document that was
    reported as indexed and is not there. So [finish] is called before /complete
    and blocks until every file has landed. What overlaps is the questions, which
    is exactly the part that was dead time.

    Nothing here writes to the terminal while it runs: the foreground is asking
    for a password, and a spinner arriving mid-prompt is worse than silence.
    """

    def __init__(self, base: str, auth: str):
        self._files: list[str] = []
        self._done = 0
        self._workspace = tempfile.mkdtemp(prefix="embabel-spec-")
        self._thread = threading.Thread(target=self._run, args=(base, auth), daemon=True)
        self._thread.start()

    def _run(self, base: str, auth: str) -> None:
        try:
            self._files = documentation_files() + fetch_spec_documents(self._workspace)
            # SIX AT A TIME, because one at a time was the whole delay. 26 uploads of
            # roughly a second each is half a minute of somebody watching a spinner,
            # and none of them depends on another. Overlapping the wizard's questions
            # only helped when there were questions left to answer — with a provider
            # key already in the environment there are almost none, so the wait simply
            # moved to the end. This removes it rather than relocating it.
            #
            # Six rather than twenty-six: each upload makes the appliance chunk and
            # embed a document, so the far end is real work on one machine, and
            # burying it does not get the guides in any sooner.
            with ThreadPoolExecutor(max_workers=6, thread_name_prefix="seed") as pool:
                for ok in pool.map(lambda path: _upload_document(base, auth, path), self._files):
                    if ok:
                        self._done += 1
        except Exception:
            pass  # best effort; the appliance is already set up and working

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def finish(self) -> None:
        """Block until every upload has landed, then say what happened."""
        if self.running:
            # Only now is there anything to look at — and only if the questions
            # did not already outlast it, which on a slow read they usually do.
            STATUS.start("Indexing the documentation")
            while self.running:
                total = len(self._files)
                STATUS.set("Indexing the documentation   "
                           + dim(f"{self._done} of {total}" if total else "reading the guides"))
                self._thread.join(0.2)
            STATUS.stop("")
        shutil.rmtree(self._workspace, ignore_errors=True)
        if self._done:
            noun = "guide" if self._done == 1 else "guides"
            print(f"  {TICK} Indexed {self._done} {noun} — ask the world about itself.")
        elif self._files:
            # Silent failure would be worse than none: somebody would search for a
            # thing the product implied was there.
            print(f"  {MIDDOT} " + dim("Could not index the documentation — "
                                       "the appliance is fine; add it later from Documents."))


def seed_documentation(base: str, auth: str) -> None:
    """Upload the guides and wait for them. The blocking form, for callers with
    nothing else to be getting on with."""
    Seeding(base, auth).finish()
