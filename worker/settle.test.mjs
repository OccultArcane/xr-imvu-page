/**
 * Tests for the presence endpoint's settling rule.
 *
 * These cover the part that decides what visitors are told, without IMVU, a
 * database or a deployment: the two-sample online confirm and the six-minute
 * offline grace that stop the plate strobing on a flag which, measured on the
 * real account, flips True/False on nearly every 30s sample.
 *
 *   node --test worker/
 *
 * The rule itself mirrors tools/track-status.py; where the two disagree, the
 * watcher is right and this file is the bug.
 */

import test from "node:test";
import assert from "node:assert/strict";

import { settle, ONLINE_CONFIRM, ONLINE_GRACE } from "./index.js";

const T0 = 1_700_000_000;

/** The shape the D1 row holds, and the shape settle() expects back. */
function row(label, streak = 0, offlineSince = null) {
  return { label, streak, offline_since: offlineSince };
}

function store(result) {
  return { label: result.label, streak: result.streak, offline_since: result.offlineSince };
}

test("the constants are the ones the watcher uses", () => {
  assert.equal(ONLINE_CONFIRM, 2, "two agreeing samples before claiming online");
  assert.equal(ONLINE_GRACE, 360, "six minutes of offline before believing it");
});

test("a cold start takes the first reading as the label", () => {
  assert.equal(settle(null, true, T0).label, "Online");
  assert.equal(settle(null, false, T0).label, "Offline");
});

test("two agreeing online samples are needed to claim online", () => {
  const first = settle(row("Offline"), true, T0);
  assert.equal(first.label, "Offline", "one sample is not enough");
  assert.equal(first.streak, 1);
  assert.equal(first.changed, true);

  const second = settle(store(first), true, T0 + 30);
  assert.equal(second.label, "Online");
  assert.equal(second.streak, 0);
  assert.equal(second.changed, true);
});

test("a settled offline creator needs the same confirmation to come back", () => {
  const first = settle(row("Offline"), true, T0);
  assert.equal(first.label, "Offline");
  const second = settle(store(first), true, T0 + 30);
  assert.equal(second.label, "Online");
});

test("a steady online reading changes nothing and writes nothing", () => {
  const result = settle(row("Online"), true, T0);
  assert.equal(result.label, "Online");
  assert.equal(result.changed, false, "no row write when the state is unchanged");
});

test("a steady offline reading changes nothing and writes nothing", () => {
  const result = settle(row("Offline"), false, T0);
  assert.equal(result.label, "Offline");
  assert.equal(result.changed, false);
});

test("an offline sample is recorded at once but not believed", () => {
  const result = settle(row("Online"), false, T0);
  assert.equal(result.label, "Online", "still online during the grace window");
  assert.equal(result.offlineSince, T0, "the window starts now");
  assert.equal(result.changed, true, "recording the window is a write");
});

test("the grace window is six minutes, to the second", () => {
  const started = row("Online", 0, T0);

  const justBefore = settle(started, false, T0 + ONLINE_GRACE - 1);
  assert.equal(justBefore.label, "Online");
  assert.equal(justBefore.offlineSince, T0, "the window keeps its original start");

  const atTheEdge = settle(started, false, T0 + ONLINE_GRACE);
  assert.equal(atTheEdge.label, "Offline");
  assert.equal(atTheEdge.offlineSince, null, "the window is cleared once believed");
  assert.equal(atTheEdge.streak, 0);
});

test("a true sample inside the window cancels the countdown", () => {
  const result = settle(row("Online", 0, T0), true, T0 + 120);
  assert.equal(result.label, "Online");
  assert.equal(result.offlineSince, null);
  assert.equal(result.changed, true, "clearing the window is a write");

  // and a later offline stretch gets a fresh six minutes, not the old one
  const later = settle(store(result), false, T0 + 600);
  assert.equal(later.label, "Online");
  assert.equal(later.offlineSince, T0 + 600);
});

test("a flapping flag never takes the label off online", () => {
  // the real-world case: IMVU alternates almost every sample
  let state = row("Online");
  for (let i = 0; i < 40; i += 1) {
    const online = i % 2 === 0;
    const result = settle(state, online, T0 + i * 15);
    assert.equal(result.label, "Online", `sample ${i} (online=${online}) dropped the label`);
    state = store(result);
  }
});

test("a long offline stretch ends in offline once, not repeatedly", () => {
  const settled = settle(row("Online", 0, T0), false, T0 + ONLINE_GRACE);
  assert.equal(settled.label, "Offline");

  const again = settle(store(settled), false, T0 + ONLINE_GRACE + 30);
  assert.equal(again.label, "Offline");
  assert.equal(again.changed, false, "nothing to write while it stays offline");
});

test("settle does not mutate the row it is given", () => {
  const previous = row("Online", 0, T0);
  const snapshot = JSON.stringify(previous);
  settle(previous, false, T0 + 400);
  assert.equal(JSON.stringify(previous), snapshot);
});
