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
 * The rule mirrors tools/track-status.py; tools/test_settle_parity.py compares the
 * two, so a change here that is not matched there fails that test.
 */

import test from "node:test";
import assert from "node:assert/strict";

import { settle } from "./index.js";

// Written out rather than imported: a Worker entry module may only export
// functions, or workerd refuses to start it (worker/index.js records the failure
// this caused). The boundary tests below are what pin the numbers.
const ONLINE_CONFIRM = 2;
const ONLINE_GRACE = 360;

const T0 = 1_700_000_000;

/** The shape the D1 row holds, and the shape settle() expects back. */
function row(label, streak = 0, offlineSince = null) {
  return { label, streak, offline_since: offlineSince };
}

/**
 * Settle a whole run of samples, threading each answer back as the next row the
 * way the endpoint writes it. `samples` is a list of [online, at] pairs.
 */
function play(seed, samples) {
  let previous = seed;
  return samples.map(([online, at]) => {
    const result = settle(previous, online, at);
    previous = row(result.label, result.streak, result.offlineSince);
    return result;
  });
}

test("a cold start takes the first reading as the label", () => {
  assert.equal(settle(null, true, T0).label, "Online");
  assert.equal(settle(null, false, T0).label, "Offline");
});

test("online needs two agreeing samples, including after a settled offline", () => {
  const [first, second] = play(row("Offline"), [
    [true, T0],
    [true, T0 + 30],
  ]);

  assert.equal(first.label, "Offline", "one sample is not enough");
  assert.equal(first.streak, 1);
  assert.equal(first.changed, true);

  assert.equal(second.label, "Online");
  assert.equal(second.streak, 0);
  assert.equal(second.changed, true);
});

test("a steady online reading writes nothing", () => {
  const result = settle(row("Online"), true, T0);
  assert.equal(result.label, "Online");
  assert.equal(result.changed, false, "no row write when the state is unchanged");
});

test("an offline sample opens the window without believing it", () => {
  const result = settle(row("Online"), false, T0);
  assert.equal(result.label, "Online", "still online during the window");
  assert.equal(result.offlineSince, T0, "the window starts now");
  assert.equal(result.changed, true, "recording the window is a write");
});

test("the window turns offline at exactly six minutes, not a second early", () => {
  const started = row("Online", 0, T0);

  const justBefore = settle(started, false, T0 + ONLINE_GRACE - 1);
  assert.equal(justBefore.label, "Online");
  assert.equal(justBefore.offlineSince, T0, "the window keeps its original start");

  const atTheEdge = settle(started, false, T0 + ONLINE_GRACE);
  assert.equal(atTheEdge.label, "Offline");
  assert.equal(atTheEdge.offlineSince, null, "the window is cleared once believed");
  assert.equal(atTheEdge.streak, 0);
});

test("a true sample inside the window cancels it, and the next one starts fresh", () => {
  const [cancelled, later] = play(row("Online", 0, T0), [
    [true, T0 + 120],
    [false, T0 + 600],
  ]);

  assert.equal(cancelled.label, "Online");
  assert.equal(cancelled.offlineSince, null);
  assert.equal(cancelled.changed, true, "clearing the window is a write");

  assert.equal(later.label, "Online");
  assert.equal(later.offlineSince, T0 + 600, "six minutes from here, not from T0");
});

test("a flapping flag never takes the label off online", () => {
  // the real-world case: IMVU alternates almost every sample
  const samples = Array.from({ length: 40 }, (_, i) => [i % 2 === 0, T0 + i * 15]);
  const results = play(row("Online"), samples);

  results.forEach((result, i) => {
    assert.equal(result.label, "Online", `sample ${i} dropped the label`);
  });
});

test("a long offline stretch settles once and then stops writing", () => {
  const [settled, again] = play(row("Online", 0, T0), [
    [false, T0 + ONLINE_GRACE],
    [false, T0 + ONLINE_GRACE + 30],
  ]);

  assert.equal(settled.label, "Offline");
  assert.equal(settled.changed, true);

  assert.equal(again.label, "Offline");
  assert.equal(again.changed, false, "nothing to write while it stays offline");
});

test("settle does not mutate the row it is given", () => {
  const previous = row("Online", 0, T0);
  const snapshot = JSON.stringify(previous);
  settle(previous, false, T0 + 400);
  assert.equal(JSON.stringify(previous), snapshot);
});
