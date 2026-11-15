/**
 * HTTP-layer tests for the presence endpoint.
 *
 * worker/settle.test.mjs covers the rule; this covers the handler around it: what
 * a visitor gets when IMVU cannot be reached, when D1 misbehaves, and what headers
 * every answer carries. No deployment and no network — the upstream `fetch` is
 * stubbed and `env.DB` is a fake that can fail on a read or a write.
 *
 *   node --test worker/
 *
 * The fake accepts any SQL, so nothing here proves the statements are valid
 * SQLite. `wrangler dev` against a local D1 does (worker/wrangler.test.jsonc).
 */

import test from "node:test";
import assert from "node:assert/strict";

import worker from "./index.js";

const USER_URL = "https://api.imvu.com/user/user-106440837";
const ENDPOINT = "https://status.test/";

/** IMVU's payload, trimmed to what the endpoint reads. */
function upstream(online, username = "xr") {
  return {
    id: USER_URL,
    denormalized: { [USER_URL]: { data: { username, online } } },
  };
}

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** Replace global fetch for the duration of one test, and record the calls. */
function stubFetch(handler) {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url: String(url), init });
    return handler(String(url), init);
  };
  return {
    calls,
    restore() {
      globalThis.fetch = original;
    },
  };
}

/**
 * A fake env.DB: one row, no SQL, and the two failures that matter. `writes`
 * records every run() so a test can assert that nothing was written.
 */
function fakeEnv({ row = null, failRead = false, failWrite = false } = {}) {
  const writes = [];
  const db = {
    prepare(sql) {
      return {
        args: [],
        bind(...args) {
          this.args = args;
          return this;
        },
        async first() {
          if (failRead) throw new Error("D1 read failed");
          return row;
        },
        async run() {
          if (failWrite) throw new Error("D1 write failed");
          writes.push({ sql, args: this.args });
        },
      };
    },
  };
  return { env: { DB: db }, writes };
}

/** The writes that would change the stored label, ignoring the schema statement. */
function labelWrites(writes) {
  return writes.filter((write) => /INSERT INTO presence/.test(write.sql));
}

async function call(env, init = {}) {
  return worker.fetch(new Request(ENDPOINT, init), env);
}

async function body(response) {
  return JSON.parse(await response.text());
}

/** Everything a successful or failed answer must carry, whatever its status. */
function assertAnswer(response, label) {
  assert.equal(response.headers.get("access-control-allow-origin"), "*", "CORS " + label);
  const cache = (response.headers.get("cache-control") || "").toLowerCase();
  assert.match(cache, /no-store/, "no-store " + label);
  assert.match(cache, /max-age=0/, "max-age=0 " + label);
  assert.match(response.headers.get("content-type") || "", /application\/json/, "JSON " + label);
}

test("a good reading answers the three keys with no-store and CORS", async (t) => {
  const imvu = stubFetch(async () => jsonResponse(upstream(true)));
  t.after(() => imvu.restore());

  const response = await call(fakeEnv().env);

  assert.equal(response.status, 200);
  assertAnswer(response, "on success");
  assert.equal(response.headers.get("x-xr-state"), "settled");

  const payload = await body(response);
  assert.deepEqual(Object.keys(payload).sort(), ["source", "status", "updated_at"]);
  assert.equal(payload.status, "Online");
  assert.equal(payload.source, "public");
  assert.match(payload.updated_at, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);

  assert.equal(imvu.calls.length, 1);
  assert.equal(imvu.calls[0].url, USER_URL);
  assert.equal(imvu.calls[0].init.cf.cacheTtl, 0, "the upstream read bypasses the edge cache");
});

test("the stored row decides the label, and an unchanged row is not rewritten", async (t) => {
  const imvu = stubFetch(async () => jsonResponse(upstream(true)));
  t.after(() => imvu.restore());
  // Offline until two samples agree, so one online sample only moves the streak
  const { env, writes } = fakeEnv({ row: { label: "Offline", streak: 0, offline_since: null } });

  assert.equal((await body(await call(env))).status, "Offline");
  const [labelWrite] = labelWrites(writes);
  assert.ok(labelWrite, "the streak change is persisted");
  assert.deepEqual(labelWrite.args, ["xr", "Offline", 1, null]);

  // the row the first request left behind is now steady: same reading, no write
  writes.length = 0;
  const steady = fakeEnv({ row: { label: "Online", streak: 0, offline_since: null } });
  assert.equal((await body(await call(steady.env))).status, "Online");
  assert.deepEqual(labelWrites(steady.writes), [], "a steady reading writes no row");
});

test("IMVU being unreachable is a 502, never an offline reading", async (t) => {
  const imvu = stubFetch(async () => {
    throw new Error("getaddrinfo ENOTFOUND api.imvu.com");
  });
  t.after(() => imvu.restore());
  const { env, writes } = fakeEnv();

  const response = await call(env);

  assert.equal(response.status, 502);
  assertAnswer(response, "on 502");
  const payload = await body(response);
  assert.match(payload.error, /ENOTFOUND/);
  assert.equal(payload.status, undefined, "no status is invented");
  assert.deepEqual(writes, [], "nothing is stored when the read failed");
});

test("an HTTP error from IMVU is a 502", async (t) => {
  const imvu = stubFetch(async () => new Response("nope", { status: 503 }));
  t.after(() => imvu.restore());

  const response = await call(fakeEnv().env);

  assert.equal(response.status, 502);
  assert.match((await body(response)).error, /503/);
});

test("a payload without a presence flag is a 502", async (t) => {
  const imvu = stubFetch(async () =>
    jsonResponse({ id: USER_URL, denormalized: { [USER_URL]: { data: {} } } })
  );
  t.after(() => imvu.restore());

  const response = await call(fakeEnv().env);

  assert.equal(response.status, 502);
  assert.match((await body(response)).error, /presence flag/);
});

test("a payload for the wrong account is a 502", async (t) => {
  const imvu = stubFetch(async () => jsonResponse(upstream(true, "someone-else")));
  t.after(() => imvu.restore());

  const response = await call(fakeEnv().env);

  assert.equal(response.status, 502);
  const { error } = await body(response);
  assert.match(error, /someone-else/);
  assert.match(error, /not "xr"/);
});

test("a D1 read failure degrades to the raw reading and says so", async (t) => {
  const imvu = stubFetch(async () => jsonResponse(upstream(false)));
  t.after(() => imvu.restore());

  const response = await call(fakeEnv({ failRead: true }).env);

  assert.equal(response.status, 200, "a visitor still gets an answer");
  assertAnswer(response, "on degraded");
  assert.match(response.headers.get("x-xr-state") || "", /^degraded: D1 read failed/);
  assert.match(
    response.headers.get("access-control-expose-headers") || "",
    /x-xr-state/,
    "the browser has to be allowed to see the marker"
  );
  assert.equal((await body(response)).status, "Offline", "the raw reading, unsettled");
});

test("a D1 write failure degrades rather than failing the visitor", async (t) => {
  const imvu = stubFetch(async () => jsonResponse(upstream(true)));
  t.after(() => imvu.restore());

  const response = await call(fakeEnv({ failWrite: true }).env);

  assert.equal(response.status, 200);
  assert.match(response.headers.get("x-xr-state") || "", /^degraded: D1 write failed/);
});

test("no DB binding at all degrades to the raw reading", async (t) => {
  const imvu = stubFetch(async () => jsonResponse(upstream(true)));
  t.after(() => imvu.restore());

  const response = await call({});

  assert.equal(response.status, 200);
  assert.equal(response.headers.get("x-xr-state"), "degraded");
  assert.equal((await body(response)).status, "Online");
});

test("the CORS preflight costs no upstream read", async (t) => {
  const imvu = stubFetch(async () => jsonResponse(upstream(true)));
  t.after(() => imvu.restore());

  const response = await call(fakeEnv().env, { method: "OPTIONS" });

  assert.equal(response.status, 204);
  assert.equal(response.headers.get("access-control-allow-origin"), "*");
  assert.match(response.headers.get("access-control-allow-methods") || "", /GET/);
  assert.match(response.headers.get("access-control-allow-headers") || "", /accept/);
  assert.equal(imvu.calls.length, 0);
});

test("a write method is refused with 405 and a hint", async (t) => {
  const imvu = stubFetch(async () => jsonResponse(upstream(true)));
  t.after(() => imvu.restore());

  const response = await call(fakeEnv().env, { method: "POST" });

  assert.equal(response.status, 405);
  assert.match(response.headers.get("allow") || "", /GET/);
  assert.equal(response.headers.get("access-control-allow-origin"), "*");
  assert.equal(imvu.calls.length, 0);
});

test("HEAD answers with the headers and no body", async (t) => {
  const imvu = stubFetch(async () => jsonResponse(upstream(true)));
  t.after(() => imvu.restore());

  const response = await call(fakeEnv().env, { method: "HEAD" });

  assert.equal(response.status, 200);
  assert.equal(await response.text(), "");
  assert.match(response.headers.get("cache-control") || "", /no-store/);
});
