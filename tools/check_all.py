#!/usr/bin/env python3
"""Run every check this project has, in one go, stopping at the first failure.

    python tools/check_all.py

Cheapest first, the local D1 harness last because it starts wrangler and a
database. Each suite's output is captured: a passing suite prints one line, a
failing one prints its output and stops the run, so the exit status is the answer.
"""

import os
import re
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SUITES = [
    ("creator cards", [sys.executable, "tools/test_creator_cards.py"]),
    ("creator refresh", [sys.executable, "tools/test_refresh_creators.py"]),
    ("hourly refresh", [sys.executable, "tools/test_refresh_hourly.py"]),
    ("worker unit tests", ["node", "--test", "worker/"]),
    ("watcher tests", [sys.executable, "tools/test_track_status.py"]),
    ("description tests", [sys.executable, "tools/test_update_descriptions.py"]),
    ("catalogue walk", [sys.executable, "tools/test_catalogue.py"]),
    ("watcher/Worker parity", [sys.executable, "tools/test_settle_parity.py"]),
    ("local D1 harness", [sys.executable, "tools/check_worker_d1.py"]),
]


def detail(name, output):
    """A short count for the summary line, from whatever each suite prints."""
    if "worker unit tests" == name:
        match = re.search(r"pass (\d+)", output)
        return "%s tests" % match.group(1) if match else ""
    if "local D1 harness" == name:
        checks = len(re.findall(r"^  ok    ", output, re.MULTILINE))
        return "%d checks" % checks
    match = re.search(r"Ran (\d+) tests", output)
    return "%s tests" % match.group(1) if match else ""


def main():
    if not shutil.which("node"):
        print("node is not installed; the Worker suites need it")
        return 1

    started = time.time()
    interactive = sys.stdout.isatty()

    for name, command in SUITES:
        if interactive:
            print("%-24s ..." % name, end="", flush=True)
        began = time.time()
        completed = subprocess.run(
            command, cwd=ROOT, capture_output=True, text=True, errors="replace"
        )
        took = time.time() - began
        output = completed.stdout + completed.stderr

        if completed.returncode != 0:
            print("%s  FAIL  %-20s %5.1fs" % ("\r" if interactive else "", name, took))
            print()
            print("\n".join(output.rstrip().splitlines()[-45:]))
            print("\nstopped: %s exited %d" % (name, completed.returncode))
            return completed.returncode

        count = detail(name, output)
        print("%s  ok    %-20s %-10s %5.1fs" % ("\r" if interactive else "", name, count, took), flush=True)

    print("\nall %d suites passed in %.1fs" % (len(SUITES), time.time() - started))
    return 0


if __name__ == "__main__":
    sys.exit(main())
