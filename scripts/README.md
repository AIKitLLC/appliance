# scripts/

Things you run to *build* or *check* the appliance's own artifacts. Nothing here is
needed to USE the appliance — that is `../install.sh` (or `../me.py`), and it stays at
the root because a one-line installer with a path in it is not a one-line installer.

| Script | What it builds |
|---|---|
| [`build-me-app.sh`](build-me-app.sh) | The Me sensor app as a real `Embabel Me.app`, and optionally a DMG |

And the ones that check rather than build — each drives a path a user actually takes,
because the failures worth catching are the ones a person meets rather than the ones a
unit test can reach:

| Script | What it drives |
|---|---|
| [`drive-install.py`](drive-install.py) | A real install in a real terminal, asserting on what a person saw |
| [`drive-tour-share.py`](drive-tour-share.py) | Somebody exporting a tour and somebody else importing it — including that it survives a restart |
| [`check-guides.py`](check-guides.py) | `guides.json` names every guide in the tree, each under a name of its own; `--write` regenerates it |
| [`check-complete.py`](check-complete.py) · [`check-copy.py`](check-copy.py) · [`check-modules.py`](check-modules.py) | Documentation and packaging invariants |

The appliance's own container images are **not** built here — they are built and
published from the `assistant` repo (`docker/build.sh`), and this repo only ever
pulls them by tag. That split is deliberate: this repo is the appliance's
*packaging*, not its source.
