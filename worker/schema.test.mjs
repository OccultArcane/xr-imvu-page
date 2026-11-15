/**
 * The schema check itself: worker/index.js runs CREATE TABLE once per isolate,
 * then trusts that it is there.
 *
 * `schemaReady` is module state, so a test that shares the module with the others
 * depends on which test ran first. Each test here imports the module under a new
 * query string instead, which gives it a fresh instance and its own flag, so the
 * result is the same whatever the file order.
 *
 *   node --test worker/
 */

import test from "node:test";
import assert from "node:assert/strict";

const ENDPOINT = "https://status.test/";
const USER_URL = "https://api.imvu.com/user/user-106440837";
const CREATE = /^CREATE TABLE IF NOT EXISTS presence/i;

let instance = 0;

/** worker/index.js, imported fresh: a new module instance with schemaReady = false. */
async function freshWorker() {
  const url = new URL(`./index.js?fresh=${(instance += 1)}`, import.meta.url);
  return (await import(url)).default;
}

/** A fake D1 that records every statement it is asked to prepare, in order. */
function recordingEnv() {
  const statements = [];
  const db = {
    prepare(sql) {
      statements.push(sql.replace(/\s+/g, " ").trim());
      return {
        bind() {
          return this;
        },
        async first() {
          return null;
        },
        async run() {},
      };
    },
  };
  return { env: { DB: db }, statements };
}

function stubFetch(t) {
  const original = globalThis.fetch;
  globalThis.fetch = async () => {
    const payload = {
      id: USER_URL,
      denormalized: { [USER_URL]: { data: { username: "xr", online: true } } },
    };
    return new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };
  t.after(() => {
    globalThis.fetch = original;
  });
}

async function request(worker, env) {
  return worker.fetch(new Request(ENDPOINT), env);
}

test("the first request creates the table, later ones only read and write it", async (t) => {
  stubFetch(t);
  const worker = await freshWorker();
  const { env, statements } = recordingEnv();

  const first = await request(worker, env);
  assert.equal(first.status, 200);
  assert.equal(first.headers.get("x-xr-state"), "settled");
  assert.deepEqual(
    statements.filter((sql) => CREATE.test(sql)),
    ["CREATE TABLE IF NOT EXISTS presence (id TEXT PRIMARY KEY, label TEXT NOT NULL, streak INTEGER NOT NULL, offline_since INTEGER)"],
    "the first request creates the table exactly once"
  );

  statements.length = 0;
  const second = await request(worker, env);

  assert.equal(second.status, 200);
  assert.equal(second.headers.get("x-xr-state"), "settled");
  assert.deepEqual(statements.filter((sql) => CREATE.test(sql)), [], "no second CREATE");
  assert.match(statements[0], /^SELECT label, streak, offline_since FROM presence WHERE id = \?$/, "it still reads the row");
});

test("a fresh instance checks the schema again rather than assuming it", async (t) => {
  stubFetch(t);

  const first = recordingEnv();
  await request(await freshWorker(), first.env);

  const second = recordingEnv();
  await request(await freshWorker(), second.env);

  assert.equal(first.statements.filter((sql) => CREATE.test(sql)).length, 1);
  assert.equal(second.statements.filter((sql) => CREATE.test(sql)).length, 1, "each isolate makes its own check");
});

test("a failure before the table exists leaves the check unspent", async (t) => {
  stubFetch(t);
  const worker = await freshWorker();

  // the schema statement itself fails: schemaReady must stay false, so the next
  // request tries again rather than answering degraded forever
  const statements = [];
  const env = {
    DB: {
      prepare(sql) {
        statements.push(sql.replace(/\s+/g, " ").trim());
        return {
          bind() {
            return this;
          },
          async first() {
            return null;
          },
          async run() {
            throw new Error("D1 unavailable");
          },
        };
      },
    },
  };

  const failed = await request(worker, env);
  assert.equal(failed.status, 200);
  assert.match(failed.headers.get("x-xr-state") || "", /^degraded/);

  const retried = await request(worker, env);
  assert.match(retried.headers.get("x-xr-state") || "", /^degraded/);
  assert.equal(
    statements.filter((sql) => CREATE.test(sql)).length,
    2,
    "the second request tried the schema again"
  );
});
