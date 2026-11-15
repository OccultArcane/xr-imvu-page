#!/usr/bin/env python3
"""Differential test: the watcher's confirm/grace rule against the Worker's.

There are two implementations of one rule. tools/track-status.py decides the
label written to assets/status.json, which the page reads locally; the deployed
Worker (worker/index.js) decides the label the published page is served. They now
share one structure — hold the published label through an unconfirmed online
sample, and start the grace window at the first offline sample — so a divergence
here means one of the two has drifted.

Both are driven over identical sample sequences, and every answer is compared with
a frozen expectation: a change to either implementation fails this test, naming
the scenario and the step that moved.

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

# The state both sides start from: Offline published, no samples behind them.
SEED_LABEL = "Offline"
SEED_STATE = {"online_streak": 0, "offline_since": None}
SEED_ROW = {"label": SEED_LABEL, "streak": 0, "offline_since": None}

O = "Online"
F = "Offline"

# name, flags, the label every sample must produce
SCENARIOS = [
    ("a steady online session", [True] * 5, [F, O, O, O, O]),
    ("a steady offline run", [False] * 4, [F, F, F, F]),
    ("recovery after a settled offline run", [False, False, True, True], [F, F, F, O]),
    # two trues settle Online at +30, then 15 offline samples: the label is held
    # until +420, which is six minutes after the first offline sample at +60
    ("a settled online label held through the window", [True] * 2 + [False] * 15, [F] + [O] * 13 + [F] * 3),
    ("one stray true", [True, False, True, True], [F, F, F, O]),
    ("flapping", [True, False] * 6, [F] * 12),
    ("a gap inside a session", [True] * 4 + [False] + [True] * 2, [F, O, O, O, O, O, O]),
]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def steps(flags, gap=GAP):
    return [{"online": flag, "at": T0 + i * gap} for i, flag in enumerate(flags)]


def watcher_labels(track, sample_steps):
    """The watcher's answer at each step, threading its state and held label."""
    state = dict(SEED_STATE)
    held = SEED_LABEL
    labels = []
    for step in sample_steps:
        held = track.settle_label(state, step["online"], step["at"], held)
        labels.append(held)
    return labels


def worker_labels(flags):
    """The Worker's answers, run through the real settle() in a Node process."""
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is not installed, so the Worker cannot be run")

    payload = json.dumps({"scenarios": [{"seed": SEED_ROW, "steps": steps(flags)}]})
    completed = subprocess.run(
        [node, DUMP], input=payload, capture_output=True, text=True, timeout=120
    )
    if completed.returncode != 0:
        raise AssertionError("the Worker side would not run:\n" + completed.stderr)
    return json.loads(completed.stdout)["results"][0]["labels"]


class ParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.track = load(TRACK, "track_status_parity")

    def labels(self, flags):
        return watcher_labels(self.track, steps(flags)), worker_labels(flags)

    def test_the_thresholds_are_the_same_numbers(self):
        # the Worker's cannot be imported — an entry module may only export
        # functions, or workerd will not start it — so they are pinned by the
        # sequences below and by worker/settle.test.mjs's edges
        self.assertEqual(self.track.ONLINE_CONFIRM, 2)
        self.assertEqual(self.track.ONLINE_GRACE, 360)

    def test_every_scenario_answers_the_same_on_both_sides(self):
        for name, flags, expected in SCENARIOS:
            with self.subTest(scenario=name):
                watcher, worker = self.labels(flags)
                self.assertEqual(watcher, expected, "watcher: " + name)
                self.assertEqual(worker, expected, "worker: " + name)

    def test_the_window_starts_at_the_first_offline_sample(self):
        # settled at +30, first offline at +60: six minutes from there is +420,
        # so +390 is still Online on both sides — anchored at the last online
        # sample it would have flipped a poll early
        flags = [True] * 2 + [False] * 15
        watcher, worker = self.labels(flags)
        self.assertEqual(watcher[13], O, "watcher flipped early at +390s")
        self.assertEqual(worker[13], O)
        self.assertEqual(watcher[14], F, "watcher should flip at +420s")
        self.assertEqual(worker[14], F)

    def test_an_unconfirmed_true_holds_the_label(self):
        # one true between gaps is not a session, and it must not claim Offline
        # on the way: flapping leaves both sides on one label throughout
        watcher, worker = self.labels([True, False] * 6)
        self.assertEqual(watcher, [F] * 12, "the watcher flashed")
        self.assertEqual(worker, [F] * 12)


if __name__ == "__main__":
    unittest.main(verbosity=2)
