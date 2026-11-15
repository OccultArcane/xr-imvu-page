#!/usr/bin/env python3
"""Differential test: the watcher's confirm/grace rule against the Worker's.

There are two implementations of one rule. tools/track-status.py decides the
label written to assets/status.json, which the page reads locally; the deployed
Worker (worker/index.js) decides the label the published page is served. If they
drift, the same moment shows one word locally and another on the site, and
nothing about either file looks wrong on its own.

So both are driven over identical sample sequences here, and every answer is
compared. The expectations below are frozen, one list per side per scenario: a
change to either implementation fails this test, and the failure says which
scenario and which step moved.

The two do not agree everywhere today. Both divergences are recorded and pinned
in test_named_divergences_are_still_the_only_ones, so they cannot widen quietly
or be fixed in one file without the other noticing:

  * the grace window is anchored differently — the watcher counts six minutes
    from the last online sample, the Worker from the first offline one, so the
    watcher gives up one poll earlier (30s at the watcher's interval);
  * the watcher ignores its own held label when an online sample is not yet
    confirmed, so it can print Offline on a true reading and Online on a false
    one around a gap, which is the flash the grace exists to prevent.

    python tools/test_settle_parity.py
"""

import importlib.util
import json
import os
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TRACK = os.path.join(HERE, "track-status.py")
DUMP = os.path.join(ROOT, "worker", "parity-dump.mjs")

T0 = 1_700_000_000  # real epoch seconds: the watcher compares against time.time()
GAP = 30  # the watcher's --watch interval

# The state both sides start from: nothing published yet, no samples behind them.
SEED = {"label": "Offline", "streak": 0, "offline_since": None}

O = "Online"
F = "Offline"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def steps(flags, gap=GAP):
    return [{"online": flag, "at": T0 + i * gap} for i, flag in enumerate(flags)]


def watcher_labels(track, sample_steps):
    """The watcher's answer at each step, threading its own state dict."""
    state = {}
    return [track.settle_label(state, step["online"], step["at"]) for step in sample_steps]


def worker_labels(flags):
    """The Worker's answers, run through the real settle() in a Node process."""
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is not installed, so the Worker cannot be run")

    payload = json.dumps({"scenarios": [{"seed": SEED, "steps": steps(flags)}]})
    completed = subprocess.run(
        [node, DUMP], input=payload, capture_output=True, text=True, timeout=120
    )
    if completed.returncode != 0:
        raise AssertionError("the Worker side would not run:\n" + completed.stderr)
    result = json.loads(completed.stdout)
    return result["results"][0]["labels"]


class ParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.track = load(TRACK, "track_status_parity")

    def labels(self, flags):
        sample_steps = steps(flags)
        return watcher_labels(self.track, sample_steps), worker_labels(flags)

    def test_the_thresholds_are_the_same_numbers(self):
        # the Worker's two thresholds cannot be imported — a Worker entry module may
        # only export functions, or workerd will not start it — so they are pinned
        # here by the sequences below, and by worker/settle.test.mjs's edges.
        self.assertEqual(self.track.ONLINE_CONFIRM, 2)
        self.assertEqual(self.track.ONLINE_GRACE, 360)

    def test_a_steady_online_session(self):
        watcher, worker = self.labels([True] * 5)
        self.assertEqual(watcher, [F, O, O, O, O])
        self.assertEqual(worker, [F, O, O, O, O])

    def test_a_steady_offline_run(self):
        watcher, worker = self.labels([False] * 4)
        self.assertEqual(watcher, [F, F, F, F])
        self.assertEqual(worker, [F, F, F, F])

    def test_recovery_after_a_settled_offline_run(self):
        watcher, worker = self.labels([False, False, True, True])
        self.assertEqual(watcher, [F, F, F, O])
        self.assertEqual(worker, [F, F, F, O])

    def test_the_grace_window_holds_a_settled_online_label(self):
        # two trues settle Online at +30; the offline samples from +60 must hold
        # it for the whole window rather than flash
        watcher, worker = self.labels([True] * 2 + [False] * 15)
        self.assertEqual(watcher.count(O), 12, "the watcher held Online through the window")
        self.assertEqual(worker.count(O), 13, "the Worker held Online through the window")
        self.assertEqual(watcher[-1], F)
        self.assertEqual(worker[-1], F)

    def test_an_isolated_true_is_not_a_session(self):
        watcher, worker = self.labels([True, False, True, True])
        self.assertEqual(watcher, [F, O, F, O], "one true is confirmed by the next two")
        self.assertEqual(worker, [F, F, F, O])

    def test_flapping_never_settles(self):
        watcher, worker = self.labels([True, False] * 6)
        self.assertEqual(worker, [F] * 12, "the Worker holds one label throughout")
        self.assertEqual(watcher, [F, O] * 6, "the watcher follows every sample")

    def test_a_gap_inside_a_session(self):
        watcher, worker = self.labels([True] * 4 + [False] + [True] * 2)
        self.assertEqual(watcher, [F, O, O, O, O, F, O])
        self.assertEqual(worker, [F, O, O, O, O, O, O])

    def test_named_divergences_are_still_the_only_ones(self):
        """Where the two differ, say exactly how — and nowhere else.

        Both entries are pinned by sequence, so making the implementations agree
        means updating this test on purpose, which is the point: the divergence
        is recorded rather than discovered later on the live page.
        """
        settled = [True] * 2 + [False] * 15
        watcher, worker = self.labels(settled)
        differing = [i for i, (a, b) in enumerate(zip(watcher, worker)) if a != b]
        self.assertEqual(differing, [13], "grace anchored at the last true (watcher) vs first false (Worker)")
        self.assertEqual(watcher[13], F, "watcher gives up at +390s, six minutes after the last true")
        self.assertEqual(worker[13], O, "Worker holds until +420s, six minutes after the first false")
        self.assertEqual(watcher[14], worker[14], "they agree from +420s on")

        stray = [True, False, True, True]
        watcher, worker = self.labels(stray)
        differing = [i for i, (a, b) in enumerate(zip(watcher, worker)) if a != b]
        self.assertEqual(differing, [1], "an unconfirmed true flips the watcher, not the Worker")


if __name__ == "__main__":
    unittest.main(verbosity=2)
