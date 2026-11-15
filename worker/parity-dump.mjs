/**
 * Drives worker/index.js's settle() over sample sequences for the parity test in
 * tools/test_settle_parity.py, which compares the answers with the watcher's
 * settle_label(). Not deployed (deploy.json names only index.js).
 *
 *   stdin:  {"scenarios": [{"seed": <row|null>, "steps": [{"online": bool, "at": seconds}]}]}
 *   stdout: {"results": [{"labels": [...]}], "ONLINE_CONFIRM": n, "ONLINE_GRACE": n}
 */

import { settle, ONLINE_CONFIRM, ONLINE_GRACE } from "./index.js";

const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const input = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");

const results = (input.scenarios || []).map((scenario) => {
  let previous = scenario.seed || null;
  const labels = [];
  for (const step of scenario.steps || []) {
    const result = settle(previous, Boolean(step.online), step.at);
    labels.push(result.label);
    // the row the endpoint would write back, which is the next call's `previous`
    previous = { label: result.label, streak: result.streak, offline_since: result.offlineSince };
  }
  return { labels };
});

process.stdout.write(JSON.stringify({ results, ONLINE_CONFIRM, ONLINE_GRACE }));
