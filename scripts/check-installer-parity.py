#!/usr/bin/env python3
"""install.sh and install.ps1 are one installer with two implementations.

The header of each promises the same environment variables with "identical
semantics", and the two scripts print the same refusals. Nothing enforced
either, and a second implementation is exactly where a promise goes quietly
stale: a variable added to the sh side is a Windows user following documented
instructions that do nothing, and a refusal reworded on one side is two
products as far as anybody comparing transcripts is concerned.

Static only — no Docker, no network, no download. What an install DOES is
scripts/drive-install-sh.sh and scripts/drive-install-ps1.ps1; this is what the
two scripts AGREE on, which is cheap to check and invisible to review.

    python3 scripts/check-installer-parity.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SH = os.path.join(HERE, "install.sh")
PS1 = os.path.join(HERE, "install.ps1")

# Variables that belong to ONE platform, with the reason. Anything else that
# appears on one side only is the drift this checker exists to catch, so this
# list is deliberately short and each entry has to earn its place.
PLATFORM_ONLY = {
    "EMBABEL_BIN_DIR": "ps1",  # the sh side's shim location is setup.py's business
    "NO_COLOR": "sh",          # no-color.org applies to ANSI; Write-Host colours differently
    "CLICOLOR_FORCE": "sh",
    "FORCE_COLOR": "sh",
    "COLUMNS": "sh",           # tput; the ps1 side reads the console width differently
    "TERM": "sh",
}

# The refusals a person can actually hit on either platform. Compared as
# SUBSTRINGS, not whole lines: the ps1 side writes "--" where the sh side writes
# an em dash (it is ASCII-only on purpose, see its header), and the two quote
# `$env:EMBABEL_REF` and `EMBABEL_REF` differently. The stem is the promise.
SHARED_REFUSALS = (
    "No 'docker' on your PATH.",
    "Docker Compose v2 is required",
    "Docker is installed but not running.",
    "exists and is not an Embabel install.",
    "Could not reach GitHub.",
    "Could not unpack the download.",
    "The download did not contain an Embabel appliance.",
    "Embabel needs Python 3.9 or newer",
)

# An existing install is adopted wherever it is found, and the candidate list is
# the only thing that decides whether somebody's data is stranded by a rename.
# Both scripts carry their own copy of it.
LEGACY_HOMES = ("embabel-worlds", "embabel-me")

problems = []


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


sh, ps1 = read(SH), read(PS1)


def documented(text):
    """The EMBABEL_* names the header's Environment table promises.

    THE TABLE, not every comment that happens to start with a variable name:
    install.sh explains EMBABEL_TOKEN in prose two hundred lines down, and
    counting that as documentation is how this checker would report a complete
    header while a Windows user reads a shorter one.
    """
    table = re.search(r"^#\s*Environment[^\n]*\n((?:#.*\n)+)", text, re.M)
    if not table:
        return set()
    return {m.group(1) for m in re.finditer(r"^#\s+(EMBABEL_[A-Z_]+)\s", table.group(1), re.M)}


def honoured(text):
    """The EMBABEL_* names the code actually reads."""
    return set(re.findall(r"EMBABEL_[A-Z_]+", text)) - {"EMBABEL_"}


# ── 1. each script documents what it reads, and reads what it documents ──────
for name, text in (("install.sh", sh), ("install.ps1", ps1)):
    doc, use = documented(text), honoured(text)
    for var in sorted(use - doc):
        # EMBABEL_DOOR is the old name of EMBABEL_MODE, honoured but deliberately
        # undocumented — a header that lists it invites new installs to use it.
        if var == "EMBABEL_DOOR":
            continue
        problems.append(f"{name}: reads {var} but its header does not document it")
    for var in sorted(doc - use):
        problems.append(f"{name}: documents {var} but never reads it")

# ── 2. the two honour the same set ───────────────────────────────────────────
sh_vars, ps1_vars = honoured(sh), honoured(ps1)
for var in sorted(sh_vars - ps1_vars):
    if PLATFORM_ONLY.get(var) == "sh":
        continue
    problems.append(f"{var} is honoured by install.sh and not by install.ps1 — "
                    "Windows follows the same instructions and gets nothing")
for var in sorted(ps1_vars - sh_vars):
    if PLATFORM_ONLY.get(var) == "ps1":
        continue
    problems.append(f"{var} is honoured by install.ps1 and not by install.sh")

# ── 3. the same refusals, in the same words ──────────────────────────────────
for refusal in SHARED_REFUSALS:
    # The ps1 side is ASCII-only, so compare against an ASCII fold of the sh
    # side rather than demanding bytes nobody can write there.
    fold = sh.replace("—", "--").replace("’", "'").replace("…", "...")
    where = [n for n, t in (("install.sh", fold), ("install.ps1", ps1)) if refusal not in t]
    if where:
        problems.append(f"{' and '.join(where)}: missing the shared refusal {refusal!r}")

# ── 4. the same installs are adopted ─────────────────────────────────────────
for legacy in LEGACY_HOMES:
    missing = [n for n, t in (("install.sh", sh), ("install.ps1", ps1)) if legacy not in t]
    if missing:
        problems.append(f"{' and '.join(missing)}: does not adopt an existing ~/{legacy} "
                        "install — the same machine would get two")

# ── 5. install.ps1 stays ASCII ───────────────────────────────────────────────
# Its own header explains why: PS 5.1 reads a .ps1 as the system ANSI codepage
# unless the file carries a UTF-8 BOM, so one em dash turns the banner into
# mojibake on a stock Windows 10. A BOM would work too — and would make the file
# unreadable to the sh-side tooling that diffs the two — so ASCII is the rule.
for lineno, line in enumerate(ps1.splitlines(), 1):
    try:
        line.encode("ascii")
    except UnicodeEncodeError as exc:
        bad = line[exc.start:exc.end]
        problems.append(f"install.ps1:{lineno}: non-ASCII {bad!r} — PS 5.1 renders it as "
                        "mojibake unless the file carries a BOM")

# ASCII markers, not tick-and-cross glyphs: this script is also run by hand on
# Windows, where a cp1252 console raises UnicodeEncodeError on them and the
# traceback replaces the findings it was printing.
if problems:
    print("\n".join(f"  x  {p}" for p in problems))
    print(f"\n  {len(problems)} installer parity problem(s).")
    sys.exit(1)
print(f"  ok install.sh and install.ps1 agree: {len(sh_vars & ps1_vars)} shared variables, "
      f"{len(SHARED_REFUSALS)} shared refusals, ASCII clean.")
