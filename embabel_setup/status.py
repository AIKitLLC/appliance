"""One self-updating line, and the three lamps on it.

ONE OBJECT OWNS THE BOTTOM LINE. The boot log prints from its own thread at the
same time, and two writers sharing a line shred each other — so every concurrent
log line goes through StatusLine.log, which erases, prints above, and redraws.
That coordination is why this is a class and not a print().
"""

from __future__ import annotations
import re
import shutil
import sys
import threading
import time

from .colour import _ANSI, _UNICODE_OK, BULLET, COLOUR, MIDDOT, accent, dim, good
from .core import BOOT_WAIT_SECONDS
from .dockerlib import (
    _answers, _docker, container_started_at, find_graph_container, image_progress, mode_of,
)
from .settings import graph_engine

# ── the status line ─────────────────────────────────────────────────────────
#
# First boot takes a minute or two, and for most of it the terminal said one
# sentence and then nothing. Silence during a long wait is indistinguishable
# from a hang, and the honest fix is not a spinner — it is telling the truth
# about what is happening, which the host can actually observe.
#
# WHAT IT SHOWS IS REAL, never a fake percentage. Three lamps, each read from
# docker or from the API on every tick:
#
#   graph    the graph container's own healthcheck — absent on the Embabel
#            engine, whose graph starts inside the server and is its lamp
#   server   the mode container's healthcheck
#   API      whether the door answers HTTP yet
#
# A boot that stalls therefore shows WHICH of the three it stalled on, which is
# the difference between "it is slow" and a support conversation.
#
# ONE WRITER FOR THE BOTTOM LINE. The boot log prints from its own thread at the
# same time, and two writers sharing a line produce shredded output — so every
# log line goes through [log], which erases the status, prints, and redraws it.
# That coordination is the whole reason this is a class and not a print().
#
# It disables itself whenever colour does: piped output must stay clean, and a
# progress animation in a CI log is thousands of lines of carriage returns.

_ESCAPES = re.compile(r"\x1b\[[0-9;]*m")


def _width() -> int:
    """Usable columns. One short of the terminal so a full-width line cannot
    wrap on the terminals that scroll at the last column rather than the first
    character past it."""
    return max(40, shutil.get_terminal_size((80, 24)).columns - 1)


SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SPINNER_ASCII = "|/-\\"
class StatusLine:
    """A single self-updating line, safe to share with a thread that prints."""

    def __init__(self) -> None:
        self.enabled = COLOUR and _UNICODE_OK is not None
        self.text = ""
        self.started = 0.0
        self.frame = 0
        self.live = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _frames(self) -> str:
        return SPINNER if _UNICODE_OK else SPINNER_ASCII

    def _render(self) -> str:
        elapsed = int(time.monotonic() - self.started)
        mark = accent(self._frames()[self.frame % len(self._frames())])
        clock = dim(f"{elapsed // 60}:{elapsed % 60:02d}")
        line = f"  {mark} {self.text}  {clock}"
        # Truncated on the VISIBLE length, which is not len(): the escape codes
        # in a coloured line are bytes the terminal never draws, so measuring the
        # raw string cuts a legible line short and lets a long one wrap anyway.
        visible = len(_ESCAPES.sub("", line))
        room = _width()
        if visible <= room:
            return line
        # Cut the plain text and repaint, rather than slicing mid-escape.
        return _ESCAPES.sub("", line)[:room - 1] + dim("…")

    def _erase(self) -> None:
        # Overwrite with spaces rather than an ANSI erase: \r plus blanks works
        # on every terminal this runs on, including the ones that ignore CSI K.
        # The TERMINAL'S width, not a guess. This was a fixed 78, and the moment
        # the line grew past it — which naming an image pull does — the tail of
        # the previous line survived every redraw as visible wreckage.
        sys.stdout.write("\r" + " " * _width() + "\r")

    def _animate(self) -> None:
        while not self._stop.wait(0.12):
            with self._lock:
                if self.live:
                    self.frame += 1
                    sys.stdout.write("\r" + self._render())
                    sys.stdout.flush()

    def start(self, text: str) -> None:
        if not self.enabled:
            print(f"  {text}")
            return
        with self._lock:
            self.text, self.started, self.live = text, time.monotonic(), True
        if self._thread is None:
            self._thread = threading.Thread(target=self._animate, daemon=True)
            self._thread.start()

    def set(self, text: str) -> None:
        """Change what the line says without restarting the clock — the elapsed
        time is of the WAIT, not of the phase, because that is the number
        somebody is deciding whether to worry about."""
        if not self.enabled:
            return
        with self._lock:
            self.text = text

    def log(self, line: str) -> None:
        """Print above the status line. Every concurrent writer uses this."""
        with self._lock:
            if self.live and self.enabled:
                self._erase()
            print(line)
            if self.live and self.enabled:
                sys.stdout.write("\r" + self._render())
                sys.stdout.flush()

    def stop(self, final: str | None = None) -> None:
        with self._lock:
            if self.live and self.enabled:
                self._erase()
            self.live = False
            if final:
                print(final)
STATUS = StatusLine()
def boot_phase(container: str | None, base: str) -> str:
    """The three lamps, read fresh. Cheap enough for a 3-second poll: two docker
    inspects against the local daemon and nothing over the network."""
    def health(name: str) -> str:
        run = _docker("inspect", "-f",
                      "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
                      name, timeout=10)
        return run.stdout.strip() if run and run.returncode == 0 else "?"

    def lamp(ok: bool, label: str) -> str:
        return f"{good(BULLET) if ok else MIDDOT} {label if ok else dim(label)}"

    parts = []
    # No lamp for a graph with no container: one that could never light reads as a stall.
    if graph_engine() != "EMBABEL":
        graph = find_graph_container()
        parts.append(lamp(graph is not None and health(graph) == "healthy", "graph"))
    if container:
        parts.append(lamp(health(container) == "healthy", "server"))
    parts.append(lamp(_answers(base), "API"))
    line = "Starting the appliance   " + dim(" · ").join(parts)
    pulling = image_progress(mode_of(container))
    return line + ("   " + pulling if pulling else "")


def wait_until_serving(container: str | None, base: str, was_started_at: str) -> bool:
    """Wait out the restart /complete triggers, and return True when the door is open.

    NOT call_when_ready, which is the mistake this replaces. That polls
    GET /api/v1/setup — which answers 410 Gone the moment setup completes, by
    design. 410 raises AlreadySetUp, a SetupError rather than an Unreachable, so
    it is never retried: the "wait" returned instantly and setup went on to
    announce a sign-in URL, and then to OPEN it, in the middle of a 21-second
    restart. That is a 502 in the user's face at the last step of the install.
    (Measured on this machine: "Started in 42.167 seconds", then "Started in
    20.78 seconds", RestartCount 1.)

    Two conditions, and the first is the one that is easy to miss: the container
    must have RESTARTED — a poll that begins before the old process has gone
    down finds it answering, declares victory, and hands over a URL that dies a
    second later. StartedAt moving is the proof. Then, health and an actual HTTP
    answer; any status counts, including 401, because a door that refuses you is
    a door that is open.
    """
    if not container:
        return _answers(base)
    deadline = time.monotonic() + BOOT_WAIT_SECONDS
    restarted = False
    while time.monotonic() < deadline:
        STATUS.set(boot_phase(container, base))
        if not restarted:
            now = container_started_at(container)
            restarted = bool(now and was_started_at and now != was_started_at)
        elif _answers(base):
            run = _docker("inspect", "-f",
                          "{{if .State.Health}}{{.State.Health.Status}}{{else}}running{{end}}",
                          container, timeout=10)
            if run and run.returncode == 0 and run.stdout.strip() in ("healthy", "running"):
                return True
        time.sleep(2)
    return _answers(base)
