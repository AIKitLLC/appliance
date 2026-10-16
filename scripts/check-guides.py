#!/usr/bin/env python3
"""guides.json names exactly the guides this checkout ships, each under its own name.

WHY THIS EXISTS. The appliance keeps its guides current by reading guides.json from
this repository: a guide missing from it is never indexed, and a guide left in it
after the file is deleted is a 404 on every appliance, every day. Nothing else
connects the file to the tree, so this does.

    scripts/check-guides.py           fail when guides.json is stale
    scripts/check-guides.py --write   rewrite it from the tree
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from embabel_setup.seed import guide_manifest  # noqa: E402

MANIFEST = os.path.join(ROOT, "guides.json")
expected = guide_manifest()

names = [guide["name"] for guide in expected["guides"]]
clashes = sorted({name for name in names if names.count(name) > 1})
# The server keys a document on its name, so two guides sharing one lose the first.
assert not clashes, f"two guides would be indexed under one name: {clashes}"
print(f"  ✓ {len(names)} guides, each with a name of its own")

if "--write" in sys.argv:
    with open(MANIFEST, "w") as f:
        json.dump(expected, f, indent=2)
        f.write("\n")
    print("  ✓ guides.json written")
    sys.exit(0)

try:
    with open(MANIFEST) as f:
        published = json.load(f)
except (OSError, ValueError):
    published = None
assert published == expected, "guides.json is stale — run: scripts/check-guides.py --write"
print("  ✓ guides.json names every guide in the tree, and nothing else")
