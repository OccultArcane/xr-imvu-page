#!/usr/bin/env python3
"""The hourly refresh: both halves of what the page reads from IMVU.

    python tools/refresh-hourly.py

The product row and the creator pictures are kept current by two separate tools,
and each one only knows its own subject. This runs them one after the other, so
the scheduled task stays a single action and a failure in one is logged without
taking the other down with it:

    tools/refresh-products.py   ->  assets/products.json, assets/products/*.webp
    tools/refresh-creators.py   ->  index.html, one creator card's picture

Animated avatar URLs carry a token that only the creator's avatar page shows, so
the second half is what keeps that token from going stale on its own.

Each run appends one line per tool to `%TEMP%\\xr-hourly.log` — under pythonw
there is no console to print to, and a task that silently stopped working would
leave the page claiming pictures nobody refreshed. Exit code is 0 when both
tools succeeded, 1 when either failed, so the task's own Last Result shows it.
"""

import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(tempfile.gettempdir(), "xr-hourly.log")
LOG_LIMIT = 256 * 1024

# in the order the page depends on them, name first for the log
TOOLS = (("products", "refresh-products.py"), ("creators", "refresh-creators.py"))

TIMEOUT = 300


def note(message):
    """One line of history per run, rotating at LOG_LIMIT like the watcher's log."""
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_LIMIT:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except OSError:
        pass


def last_line(text):
    """The useful part of a tool's complaint: its last non-empty line."""
    for line in reversed((text or "").strip().splitlines()):
        if line.strip():
            return line.strip()
    return ""


def run(name, script):
    """Run one tool, log how it went, and say whether it succeeded."""
    path = os.path.join(ROOT, "tools", script)
    if not os.path.exists(path):
        note("%s: failed -- %s is missing" % (name, script))
        return False

    started = time.monotonic()
    try:
        finished = subprocess.run(
            [sys.executable, path],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        note("%s: failed -- still running after %ds, stopped" % (name, TIMEOUT))
        return False
    except OSError as error:
        note("%s: failed -- could not start it (%s)" % (name, error))
        return False

    took = time.monotonic() - started
    said = last_line(finished.stdout) if finished.returncode == 0 else last_line(finished.stderr)

    if finished.returncode == 0:
        note("%s: ok in %.1fs -- %s" % (name, took, said or "no output"))
        return True

    note(
        "%s: failed in %.1fs (exit %d) -- %s"
        % (name, took, finished.returncode, said or "no output")
    )
    return False


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    failures = [name for name, script in TOOLS if not run(name, script)]
    if failures:
        print("failed: %s (see %s)" % (", ".join(failures), LOG_PATH), file=sys.stderr)
        return 1

    print("both refreshes ran; %s" % LOG_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
